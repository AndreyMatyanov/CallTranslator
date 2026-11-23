"""Захват звука (Windows WASAPI): микрофон и системный звук (loopback).

Каждый поток приводится к 16 кГц моно float32 и отдаётся в callback.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable

import numpy as np

TARGET_SR = 16000
FRAME = 480  # 30 мс при 16 кГц


# ---------------------------------------------------------------- devices
def _pa():
    import pyaudiowpatch as pyaudio  # Windows-only форк PyAudio с loopback
    return pyaudio


def list_devices() -> None:
    pyaudio = _pa()
    p = pyaudio.PyAudio()
    try:
        wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
        print("WASAPI-устройства (для config.toml достаточно части названия):\n")
        for i in range(p.get_device_count()):
            d = p.get_device_info_by_index(i)
            if d["hostApi"] != wasapi["index"] or d["maxInputChannels"] <= 0:
                continue
            kind = "СИСТЕМНЫЙ ЗВУК (loopback)" if d.get("isLoopbackDevice") else "микрофон"
            print(f"  [{i:>3}] {kind:<26} {d['name']}")
    finally:
        p.terminate()


def enumerate_devices() -> dict:
    """{"mics": [...], "loopbacks": [...], "outputs": [...]} — названия WASAPI-устройств."""
    pyaudio = _pa()
    p = pyaudio.PyAudio()
    try:
        wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
        res = {"mics": [], "loopbacks": [], "outputs": []}
        for i in range(p.get_device_count()):
            d = p.get_device_info_by_index(i)
            if d["hostApi"] != wasapi["index"]:
                continue
            if d.get("isLoopbackDevice"):
                res["loopbacks"].append(d["name"])
            elif d["maxInputChannels"] > 0:
                res["mics"].append(d["name"])
            elif d["maxOutputChannels"] > 0:
                res["outputs"].append(d["name"])
        res["outputs"].sort(key=lambda n: (0 if "cable" in n.lower() or "voicemeeter" in n.lower() else 1, n))
        return res
    finally:
        p.terminate()


def find_output(p, hint: str):
    pyaudio = _pa()
    wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
    for i in range(p.get_device_count()):
        d = p.get_device_info_by_index(i)
        if (d["hostApi"] == wasapi["index"] and d["maxOutputChannels"] > 0
                and not d.get("isLoopbackDevice") and hint.lower() in d["name"].lower()):
            return d
    raise RuntimeError(f"Устройство вывода не найдено: «{hint}»")


def find_devices(p, mic_hint: str, loop_hint: str):
    pyaudio = _pa()
    wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
    devs = [p.get_device_info_by_index(i) for i in range(p.get_device_count())]
    devs = [d for d in devs if d["hostApi"] == wasapi["index"] and d["maxInputChannels"] > 0]

    def pick(hint: str, loopback: bool):
        cands = [d for d in devs if bool(d.get("isLoopbackDevice")) == loopback]
        for d in cands:
            if hint.lower() in d["name"].lower():
                return d
        raise RuntimeError(f"Устройство не найдено: «{hint}». Запустите list_devices.bat")

    if mic_hint:
        mic = pick(mic_hint, False)
    else:
        mic = p.get_device_info_by_index(wasapi["defaultInputDevice"])

    if loop_hint:
        loop = pick(loop_hint, True)
    else:
        try:
            loop = p.get_default_wasapi_loopback()
        except Exception:
            spk = p.get_device_info_by_index(wasapi["defaultOutputDevice"])
            loop = next(d for d in devs if d.get("isLoopbackDevice") and spk["name"] in d["name"])
    return mic, loop


class Capture:
    """Один входной поток WASAPI → 16 кГц моно → on_audio(np.ndarray)."""

    def __init__(self, p, device: dict, on_audio: Callable[[np.ndarray], None], on_raw=None):
        import soxr

        self.on_raw = on_raw  # моно с исходной частотой (для Passthrough)
        self.p = p
        self.dev = device
        self.on_audio = on_audio
        self.channels = max(1, int(device["maxInputChannels"]))
        self.rate = int(device["defaultSampleRate"])
        self.resampler = soxr.ResampleStream(self.rate, TARGET_SR, 1, dtype="float32")
        self.stream = None

    def _cb(self, in_data, frame_count, time_info, status):
        pyaudio = _pa()
        try:
            data = np.frombuffer(in_data, dtype=np.float32)
            if self.channels > 1:
                data = data.reshape(-1, self.channels).mean(axis=1)
            if self.on_raw:
                self.on_raw(data)
            out = self.resampler.resample_chunk(np.ascontiguousarray(data, dtype=np.float32))
            if out.size:
                self.on_audio(out)
        except Exception as e:  # никогда не роняем аудиопоток
            print("audio callback error:", e)
        return (None, pyaudio.paContinue)

    def start(self):
        pyaudio = _pa()
        self.stream = self.p.open(
            format=pyaudio.paFloat32,
            channels=self.channels,
            rate=self.rate,
            input=True,
            input_device_index=self.dev["index"],
            frames_per_buffer=max(256, int(self.rate * 0.03)),
            stream_callback=self._cb,
        )
        self.stream.start_stream()

    def stop(self):
        if self.stream:
            try:
                self.stream.stop_stream()
                self.stream.close()
            except Exception:
                pass


class Passthrough:
    """Пропускает микрофон в виртуальный кабель (CABLE Input). muted=True — собеседник вас не слышит."""

    def __init__(self, p, out_dev: dict, in_rate: int):
        import soxr

        pyaudio = _pa()
        self.rate = int(out_dev["defaultSampleRate"])
        self.ch = max(1, min(2, int(out_dev["maxOutputChannels"])))
        self.rs = soxr.ResampleStream(in_rate, self.rate, 1, dtype="float32")
        self.buf = np.zeros(0, np.float32)
        self.lock = threading.Lock()
        self.muted = False
        self.stream = p.open(
            format=pyaudio.paFloat32, channels=self.ch, rate=self.rate, output=True,
            output_device_index=out_dev["index"], frames_per_buffer=max(128, int(self.rate * 0.01)),
            stream_callback=self._cb,
        )
        self.stream.start_stream()

    def push(self, mono: np.ndarray):
        y = self.rs.resample_chunk(np.ascontiguousarray(mono, dtype=np.float32))
        if self.muted:
            y = np.zeros_like(y)
        with self.lock:
            self.buf = np.concatenate([self.buf, y])
            if len(self.buf) > int(self.rate * 0.12):  # не даём задержке накапливаться
                self.buf = self.buf[-int(self.rate * 0.04):]

    def _cb(self, in_data, frame_count, time_info, status):
        pyaudio = _pa()
        with self.lock:
            out = self.buf[:frame_count]
            self.buf = self.buf[frame_count:]
        if len(out) < frame_count:
            out = np.concatenate([out, np.zeros(frame_count - len(out), np.float32)])
        data = np.repeat(out[:, None], self.ch, axis=1).astype(np.float32).tobytes()
        return (data, pyaudio.paContinue)

    def stop(self):
        try:
            self.stream.stop_stream()
            self.stream.close()
        except Exception:
            pass


# ---------------------------------------------------------------- segmenter
class Segmenter:
    """Нарезает поток на фразы по энергии (простая VAD с адаптивным порогом).

    on_final(audio, utt_id)    — законченная фраза
    on_partial(audio, utt_id)  — промежуточный буфер для «черновика»
    """

    def __init__(
        self,
        on_final,
        on_partial=None,
        silence_ms: int = 600,
        max_phrase_s: float = 12.0,
        min_phrase_s: float = 0.4,
        threshold: float = 0.0,
        min_threshold: float = 0.006,
        partial_interval_s: float = 1.0,
    ):
        self.on_final = on_final
        self.on_partial = on_partial
        self.silence_frames = max(1, silence_ms // 30)
        self.max_frames = int(max_phrase_s / 0.03)
        self.min_s = min_phrase_s
        self.fixed_thr = threshold
        self.min_thr = min_threshold
        self.partial_interval = partial_interval_s
        self.lock = threading.Lock()
        self.buf = np.zeros(0, np.float32)
        self.pre: deque = deque(maxlen=10)  # 300 мс до начала речи
        self.speech: list[np.ndarray] = []
        self.in_speech = False
        self.silent = 0
        self.loud_run = 0
        self.noise = min_threshold / 3  # стартовая оценка шума: не выше порога
        self.last_audio = time.monotonic()
        self.last_partial = 0.0
        self.utt = 0
        self.enabled = True
        self.last_sound = 0.0  # когда последний раз был звук громче порога (для индикатора)
        # статистика для diag.log (сбрасывается при каждой записи)
        self.st_samples = 0
        self.st_peak_rms = 0.0
        self.st_phrases = 0

    def threshold(self) -> float:
        return self.fixed_thr or max(self.noise * 3.0, self.min_thr)

    def feed(self, samples: np.ndarray):
        with self.lock:
            self.last_audio = time.monotonic()
            self.st_samples += len(samples)
            if not self.enabled:
                return
            self.buf = np.concatenate([self.buf, samples])
            n = len(self.buf) // FRAME
            for i in range(n):
                self._frame(self.buf[i * FRAME:(i + 1) * FRAME])
            self.buf = self.buf[n * FRAME:]

    def tick(self):
        """Вызывать периодически: loopback молчит без звука, фразу надо закрыть по таймеру."""
        with self.lock:
            if self.in_speech and time.monotonic() - self.last_audio > self.silence_frames * 0.03:
                self._finish(keep_going=False)

    def set_enabled(self, on: bool, flush: bool = False):
        with self.lock:
            if not on and flush and self.in_speech:
                self._finish(keep_going=False)  # клавишу отпустили — отдаём сказанное
            self.enabled = on
            if not on:
                self.in_speech = False
                self.speech = []
                self.pre.clear()

    def _frame(self, f: np.ndarray):
        rms = float(np.sqrt(np.mean(f * f)) + 1e-9)
        if rms > self.st_peak_rms:
            self.st_peak_rms = rms
        loud = rms > self.threshold()
        if loud:
            self.last_sound = time.monotonic()
        if not self.in_speech:
            self.pre.append(f)
            if loud:
                self.loud_run += 1
            else:
                self.loud_run = 0
                self.noise = max(1e-4, 0.98 * self.noise + 0.02 * rms)
            if self.loud_run >= 3:
                self.in_speech = True
                self.utt += 1
                self.speech = list(self.pre)
                self.pre.clear()
                self.silent = 0
                self.last_partial = time.monotonic()
            return

        self.speech.append(f)
        self.silent = 0 if loud else self.silent + 1
        if self.silent >= self.silence_frames:
            self._finish(keep_going=False)
        elif len(self.speech) >= self.max_frames:
            self._finish(keep_going=True)
        elif (
            self.on_partial
            and self.partial_interval > 0
            and len(self.speech) * 0.03 >= 1.0
            and time.monotonic() - self.last_partial >= self.partial_interval
        ):
            self.last_partial = time.monotonic()
            self.on_partial(np.concatenate(self.speech), self.utt)

    def _finish(self, keep_going: bool):
        frames = self.speech
        trailing = self.silent
        utt = self.utt
        self.speech = []
        self.silent = 0
        self.loud_run = 0
        if keep_going:  # слишком длинная фраза — режем, но продолжаем слушать
            self.utt += 1
            self.last_partial = time.monotonic()
        else:
            self.in_speech = False
        if not frames:
            return
        keep = len(frames) - max(0, trailing - 7)  # оставляем ~200 мс тишины в конце
        audio = np.concatenate(frames[:keep])
        voiced = (len(frames) - trailing) * 0.03
        # None = фраза слишком короткая (кашель, щелчок) — только убрать черновик
        if voiced >= self.min_s:
            self.st_phrases += 1
        self.on_final(audio if voiced >= self.min_s else None, utt)
