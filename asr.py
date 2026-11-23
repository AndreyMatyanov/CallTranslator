"""Локальное распознавание речи: faster-whisper (CUDA, при неудаче — CPU)."""
from __future__ import annotations

import importlib.util
import os
import re
import sys
import threading
from collections import deque
from typing import Callable

import numpy as np

# Безобидные предупреждения Hugging Face при скачивании модели (нет symlinks в Windows без прав
# администратора, загрузка без аккаунта) только пугают — отключаем их.
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_VERBOSITY", "error")
os.environ.setdefault("HF_HUB_DISABLE_IMPLICIT_TOKEN", "1")

# Модели Whisper храним рядом с программой, а не в C:\Users\...\.cache
MODELS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")


def _add_cuda_dll_dirs():
    """Делаем видимыми DLL из pip-пакетов nvidia-cublas-cu12 / nvidia-cudnn-cu12."""
    if sys.platform != "win32":
        return
    for sub in ("cublas", "cudnn", "cuda_nvrtc"):
        try:
            spec = importlib.util.find_spec(f"nvidia.{sub}")
        except Exception:
            spec = None
        if not spec or not spec.submodule_search_locations:
            continue
        for loc in spec.submodule_search_locations:
            b = os.path.join(loc, "bin")
            if os.path.isdir(b):
                os.add_dll_directory(b)
                os.environ["PATH"] = b + os.pathsep + os.environ.get("PATH", "")


# Типичные «галлюцинации» Whisper на тишине/музыке
_JUNK = {
    "thank you", "thanks for watching", "thank you for watching", "you", "bye",
    "subtitles by the amara org community", "please subscribe",
    "продолжение следует", "спасибо за просмотр", "субтитры сделал dimatorzok",
    "редактор субтитров а синецкая корректор а егорова", "субтитры создавал dimatorzok",
}


def _norm(s: str) -> str:
    return re.sub(r"[^\w ]+", "", s.lower()).strip()


def gpu_vram_mb() -> int | None:
    """Объём видеопамяти первой NVIDIA-карты (МБ) через nvidia-smi."""
    import subprocess
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
            creationflags=0x08000000 if sys.platform == "win32" else 0,
        ).stdout
        return int(out.strip().splitlines()[0])
    except Exception:
        return None


class ASREngine:
    def __init__(self, cfg: dict, status: Callable[[str], None]):
        self.cfg = cfg
        self.status = status
        self.model = None
        self.device = "cpu"
        self.lock = threading.Lock()

    def load(self):
        _add_cuda_dll_dirs()
        import warnings
        warnings.filterwarnings("ignore", module="huggingface_hub")
        try:
            from huggingface_hub.utils import logging as hf_logging
            hf_logging.set_verbosity_error()
        except Exception:
            pass
        from faster_whisper import WhisperModel

        dev = self.cfg.get("device", "auto")
        if dev == "auto":
            try:
                import ctranslate2
                dev = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
            except Exception:
                dev = "cpu"

        attempts = []
        if dev == "cuda":
            ct = self.cfg.get("compute_type", "auto")
            if ct == "auto":
                vram = gpu_vram_mb()
                # До 8 ГБ: int8 экономит ~1 ГБ видеопамяти — её хватит и Whisper, и модели перевода
                ct = "int8_float16" if vram and vram <= 8192 else "float16"
            attempts.append(("cuda", self.cfg.get("model", "large-v3-turbo"), ct))
        attempts.append(("cpu", self.cfg.get("model_cpu", "small"), "int8"))

        last_err = None
        for device, name, ctype in attempts:
            try:
                self.status(f"Загрузка распознавания речи «{name}» ({device})… при первом запуске модель скачивается (~1,6 ГБ), подождите")
                m = WhisperModel(name, device=device, compute_type=ctype, download_root=MODELS_DIR)
                # прогрев: на этом шаге всплывают ошибки cuDNN/cuBLAS
                list(m.transcribe(np.zeros(16000, np.float32), language="en", beam_size=1)[0])
                self.model, self.device = m, device
                self.status(f"Распознавание речи готово ({name}, {device}, {ctype})")
                return
            except Exception as e:
                last_err = e
                self.status(f"Не удалось на {device}: {e}")
        raise RuntimeError(f"Whisper не загрузился: {last_err}")

    def transcribe(self, audio: np.ndarray, languages: list[str], beam: int) -> tuple[str, str]:
        """Возвращает (текст, язык). languages: один язык или список допустимых."""
        peak = float(np.max(np.abs(audio))) if audio.size else 0.0
        if 1e-4 < peak < 0.3:  # тихий звонок: нормализуем громкость (не больше чем в 30 раз)
            audio = audio * min(30.0, 0.3 / peak)
        kw = dict(
            beam_size=beam,
            vad_filter=True,
            condition_on_previous_text=False,
            without_timestamps=True,
        )
        with self.lock:
            if len(languages) == 1:
                lang = languages[0]
                segs, _ = self.model.transcribe(audio, language=lang, **kw)
            else:
                segs, info = self.model.transcribe(audio, language=None, **kw)
                lang = info.language
                if lang not in languages:
                    probs = dict(info.all_language_probs or [])
                    lang = max(languages, key=lambda l: probs.get(l, 0.0))
                    segs, _ = self.model.transcribe(audio, language=lang, **kw)
            parts = []
            for s in segs:
                if s.no_speech_prob > 0.6 and s.avg_logprob < -1.0:
                    continue
                parts.append(s.text.strip())
        text = " ".join(p for p in parts if p).strip()
        if _norm(text) in _JUNK:
            text = ""
        return text, lang


class ASRWorker(threading.Thread):
    """Очередь распознавания: законченные фразы важнее черновиков.

    on_result(source, kind, text, lang, utt)  kind: "final" | "partial" | "clear"
    """

    def __init__(self, engine: ASREngine, languages: dict[str, list[str]], beam: int, on_result):
        super().__init__(daemon=True)
        self.engine = engine
        self.languages = languages
        self.beam = beam
        self.on_result = on_result
        self.cv = threading.Condition()
        self.finals: deque = deque()
        self.partials: dict[str, tuple] = {}
        self.done_utt: dict[str, int] = {}
        self.last_error = ""

    def submit_final(self, source: str, audio, utt: int):
        with self.cv:
            self.partials.pop(source, None)
            self.finals.append((source, audio, utt))
            self.cv.notify()

    def submit_partial(self, source: str, audio, utt: int):
        with self.cv:
            self.partials[source] = (audio, utt)
            self.cv.notify()

    def run(self):
        while True:
            with self.cv:
                while not self.finals and not self.partials:
                    self.cv.wait()
                if self.finals:
                    source, audio, utt = self.finals.popleft()
                    kind = "final"
                else:
                    source = next(iter(self.partials))
                    audio, utt = self.partials.pop(source)
                    kind = "partial"
                    if utt <= self.done_utt.get(source, 0):
                        continue
            if kind == "final":
                with self.cv:
                    self.done_utt[source] = max(utt, self.done_utt.get(source, 0))
                if audio is None:
                    self.on_result(source, "clear", "", "", utt)
                    continue
            try:
                beam = self.beam if kind == "final" else 1
                text, lang = self.engine.transcribe(audio, self.languages[source], beam)
            except Exception as e:
                print("ASR error:", e)
                self.last_error = str(e)
                continue
            if kind == "partial":
                with self.cv:
                    if utt <= self.done_utt.get(source, 0):
                        continue
            if kind == "final" and not text:
                kind = "clear"
            self.on_result(source, kind, text, lang, utt)
