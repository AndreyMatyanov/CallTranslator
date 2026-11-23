"""Call Translator — оверлей-переводчик для звонков (полностью локально).

Собеседник (окно звонка или системный звук, EN) → субтитры на русском.
Вы (микрофон/ввод, RU) → английская фраза, которую можно сказать.
"""
from __future__ import annotations

import itertools
import os
import re
import sys
import threading
import time
import tomllib

HERE = os.path.dirname(os.path.abspath(__file__))
CYR = re.compile(r"[а-яё]", re.I)


QWORDS = {
    "what", "what's", "whats", "how", "how's", "why", "when", "where", "who", "whom", "whose", "which",
    "do", "does", "did", "are", "is", "was", "were", "can", "could", "would", "will", "should", "shall",
    "have", "has", "had", "may", "might", "am", "isn't", "aren't", "don't", "doesn't", "didn't", "won't",
    "can't", "couldn't", "wouldn't", "any", "anything",
}


FILLERS = {"so", "and", "but", "okay", "ok", "well", "um", "uh", "then", "also", "oh", "hey", "right", "alright", "now"}


def is_question(text: str) -> bool:
    """Вопрос ли это: по знаку «?» (Whisper его обычно ставит) или по вопросительному слову в начале."""
    t = text.strip()
    if not t:
        return False
    if t.endswith("?") or "?" in t[-12:]:
        return True
    last = re.split(r"(?<=[.!])\s+", t)[-1]  # последнее предложение
    words = re.findall(r"[a-z']+", last.lower())
    while words and words[0] in FILLERS:  # «so how long…», «okay and what…»
        words = words[1:]
    return len(words) >= 3 and words[0] in QWORDS


def load_config() -> dict:
    with open(os.path.join(HERE, "config.toml"), "rb") as f:
        return tomllib.load(f)


class Controller:
    def __init__(self, cfg: dict, bridge, overlay, app):
        from llm import LLMWorker, Ollama
        from settings import load_settings

        self.cfg = cfg
        self.bridge = bridge
        self.overlay = overlay
        self.app = app
        self.ids = itertools.count(1)
        self.history: list[dict] = []
        self.hlock = threading.Lock()
        self.sugg: list[dict] = []
        self.a = cfg["audio"]
        self.s = load_settings(cfg)
        self.mic_mode_saved = self.s["mic_mode"] if self.s["mic_mode"] != "off" else "always"
        self.segs: dict = {}
        self.captures: list = []
        self.passthrough = None
        self.pa = None
        self.asr = None
        self.engine = None
        self.audio_lock = threading.Lock()
        self.ptt_down = False
        self.me_last = 0.0              # когда вы последний раз что-то говорили
        self.pending_sugg = None        # новые подсказки ждут, пока вы договорите
        self.chat_watcher = None
        from collections import deque
        self.own_recent: deque = deque(maxlen=40)  # ваши английские фразы (для фильтра чата)
        self.diag: dict[str, dict] = {}  # счётчики распознавания для diag.log
        self._diag_t = time.monotonic()
        self._read_cover: dict[int, set] = {}  # какие слова варианта вы уже прочитали

        # Запомненная «маленькая» модель действует, пока в config.toml не выбрали другую основную
        auto_model = self.s.get("llm_model") if self.s.get("llm_model_for") == cfg["llm"]["model"] else None
        self.ollama = Ollama(cfg["llm"]["url"], auto_model or cfg["llm"]["model"],
                             status=lambda t: self.bridge.status.emit(t))
        self.whisper_ready = threading.Event()
        self._ollama_economy_tried = False
        self.llm = LLMWorker(self.ollama, int(cfg["llm"].get("context_lines", 3)))
        self.llm.start()

        overlay.typed.connect(self.on_typed)
        overlay.action.connect(self.on_action)
        bridge.hotkey.connect(self.on_action)
        bridge.suggestions.connect(self._on_new_suggestions)

    # ------------------------------------------------------------ startup
    def start(self):
        threading.Thread(target=self._boot, daemon=True).start()
        threading.Thread(target=self._ticker, daemon=True).start()

    def status(self, text: str):
        self.bridge.status.emit(text)

    FALLBACK_MODEL = "qwen2.5:3b"  # ~1,9 ГБ — если основная модель не помещается в видеопамять

    def _prepare_llm(self):
        """Запуск Ollama и (при необходимости) скачивание модели — параллельно с загрузкой Whisper.

        В видеопамять модель перевода грузим только ПОСЛЕ Whisper: если перевод займёт память первым,
        распознавание уйдёт на процессор — это намного хуже, чем медленный перевод.
        """
        self.llm_problem = self.ollama.check()
        if self.llm_problem:
            self.status(self.llm_problem)
            return
        self.whisper_ready.wait(timeout=900)
        self.llm.warmup(on_loaded=self._on_llm_loaded)

    def _on_llm_loaded(self, placement) -> bool:
        """Модель не поместилась в видеопамять → сначала экономный перезапуск Ollama, потом модель поменьше."""
        from asr import gpu_vram_mb
        if not placement or not placement[0] or placement[1] >= placement[0]:
            return False  # поместилась целиком (или узнать нельзя)
        vram = gpu_vram_mb()
        if not vram or vram > 8192:
            return False  # большая карта: просто покажем предупреждение
        fits = placement[1] * 100 // placement[0]
        if not self._ollama_economy_tried:
            self._ollama_economy_tried = True
            self.status(f"Модель перевода поместилась на {fits}% — перезапускаю Ollama в экономном режиме…")
            if self.ollama.restart_economy():
                self.llm.warmup(on_loaded=self._on_llm_loaded)
                return True
        if self.ollama.model != self.FALLBACK_MODEL:
            old = self.ollama.model
            self.status(f"{old} не помещается в {vram // 1024} ГБ видеопамяти — переключаюсь на {self.FALLBACK_MODEL} "
                        "(скачается один раз, ~2 ГБ)…")
            self.ollama.unload(old)
            self.ollama.model = self.FALLBACK_MODEL
            self.s["llm_model"] = self.FALLBACK_MODEL  # запомним, чтобы в следующий раз не грузить большую
            self.s["llm_model_for"] = self.cfg["llm"]["model"]
            try:
                from settings import save_settings
                save_settings(self.s)
            except Exception:
                pass
            if self.ollama.pull():
                self.llm.warmup(on_loaded=self._on_llm_loaded)
            return True
        return False

    def _boot(self):
        self.llm_problem = None
        threading.Thread(target=self._prepare_llm, daemon=True).start()

        from asr import ASREngine, ASRWorker
        self.engine = ASREngine(self.cfg["asr"], self.status)
        try:
            self.engine.load()
        except Exception as e:
            self.status(str(e))
            self.whisper_ready.set()
            return
        self.whisper_ready.set()
        langs = {"them": [self.a.get("their_language", "en")], "me": list(self.s["my_languages"])}
        self.asr = ASRWorker(self.engine, langs, int(self.cfg["asr"].get("beam_size", 3)), self.on_asr)
        self.asr.start()
        self.restart_audio()
        self.start_chat_watch()
        if self.llm_problem or self.ollama.warning:  # чтобы сообщение об Ollama не затёрлось «Слушаю»
            self.status(self.llm_problem or self.ollama.warning)

    def _indicators(self):
        from settings import source_label
        mode = self.s["mic_mode"]
        if mode == "off":
            mic = "🎙 выкл"
        elif mode == "always":
            mic = "🎙 всегда"
        else:
            mic = ("🔴 говорите…" if self.ptt_down else f"🎙 по клавише {self.s['ptt_key']}")
        if self.s.get("passthrough"):
            mic += " · 🔇скрыто" if (mode == "ptt" and self.ptt_down) else " · кабель"
        self.bridge.indicators.emit({
            "mic_text": mic,
            "device": self.engine.device if self.engine else "",
            "source": source_label(self.s["them_source"]),
        })

    # ------------------------------------------------------------ audio pipeline
    def stop_audio(self):
        for c in self.captures:
            c.stop()
        self.captures = []
        if self.passthrough:
            self.passthrough.stop()
            self.passthrough = None
        self.segs = {}
        if self.pa:
            try:
                self.pa.terminate()
            except Exception:
                pass
            self.pa = None

    def restart_audio(self):
        if not self.asr:
            return
        with self.audio_lock:
            self.stop_audio()
            try:
                self._start_audio()
                self.status("Слушаю")
            except Exception as e:
                self.status(f"Ошибка звука: {e}")
        self._indicators()

    def _make_segmenter(self, source: str):
        from audio import Segmenter
        a = self.a
        partials = self.engine.device == "cuda"
        return Segmenter(
            on_final=lambda audio, utt, s=source: self.asr.submit_final(s, audio, utt),
            on_partial=(lambda audio, utt, s=source: self.asr.submit_partial(s, audio, utt)) if partials else None,
            silence_ms=int(a.get("silence_ms", 600)),
            max_phrase_s=float(a.get("max_phrase_s", 12)),
            min_phrase_s=float(a.get("min_phrase_s", 0.4)),
            threshold=float(a.get("energy_threshold", 0.0)),
            # системный звук без шума: в паузах там почти цифровая тишина, а голос в звонке
            # бывает тихим (громкость приложения убавлена) — поэтому порог ниже, чем у микрофона
            min_threshold=0.0015 if source.startswith("them") else 0.006,
            partial_interval_s=float(a.get("partial_interval_s", 1.0)),
        )

    def _start_audio(self):
        import pyaudiowpatch as pyaudio
        from audio import Capture, Passthrough, find_devices, find_output

        s = self.s
        self.asr.languages["me"] = list(s["my_languages"])
        self.pa = pyaudio.PyAudio()
        src = s["them_source"]
        loop_hint = src.get("name", "") if src.get("type") == "device" else ""
        mic, loop = find_devices(self.pa, s.get("mic_device", ""), loop_hint)

        # --- собеседник
        if src.get("type") == "app" and sys.platform == "win32":
            from winaudio import AppLoopbackCapture
            seg_them = self._make_segmenter("them")
            self.segs["them"] = seg_them
            cap = AppLoopbackCapture(src["exe"], seg_them.feed, self.status,
                                     on_fail=lambda e: self._fallback_system(e))
            cap.start()
            self.captures.append(cap)
        elif src.get("type") == "all":
            self._start_loopbacks(self._all_loopbacks())
        else:
            self._start_loopbacks([loop])

        # --- микрофон
        seg_me = self._make_segmenter("me")
        seg_me.set_enabled(s["mic_mode"] == "always")
        self.segs["me"] = seg_me
        on_raw = None
        if s.get("passthrough") and s.get("passthrough_device"):
            out = find_output(self.pa, s["passthrough_device"])
            self.passthrough = Passthrough(self.pa, out, int(mic["defaultSampleRate"]))
            on_raw = self.passthrough.push
        mcap = Capture(self.pa, mic, seg_me.feed, on_raw=on_raw)
        mcap.start()
        self.captures.append(mcap)
        print("Микрофон:", mic["name"])

    def _all_loopbacks(self) -> list[dict]:
        """Все устройства вывода (loopback), кроме виртуального кабеля, куда идёт ваш микрофон."""
        import pyaudiowpatch as pyaudio
        wasapi = self.pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        skip = (self.s.get("passthrough_device") or "").lower()
        devs = []
        for i in range(self.pa.get_device_count()):
            d = self.pa.get_device_info_by_index(i)
            if d["hostApi"] != wasapi["index"] or not d.get("isLoopbackDevice"):
                continue
            if skip and skip in d["name"].lower():
                continue
            devs.append(d)
        return devs

    def _start_loopbacks(self, devices: list[dict]):
        """Каждое устройство слушаем отдельно (свой нарезчик фраз) — смешивать потоки не нужно."""
        from audio import Capture
        their = [self.a.get("their_language", "en")]
        n = 0
        for d in devices:
            key = "them" if n == 0 else f"them{n}"
            seg = self._make_segmenter(key)
            self.asr.languages[key] = their
            try:
                cap = Capture(self.pa, d, seg.feed)
                cap.start()
            except Exception as e:
                print("Не удалось открыть", d["name"], e)
                continue
            self.segs[key] = seg
            self.captures.append(cap)
            n += 1
            print("Звук собеседника:", d["name"])
        if n == 0:
            raise RuntimeError("не удалось открыть ни одно устройство вывода")

    def _fallback_system(self, err):
        self.status(f"Захват окна недоступен ({err}) — слушаю все устройства вывода")
        try:
            with self.audio_lock:
                self.segs.pop("them", None)
                self._start_loopbacks(self._all_loopbacks())
        except Exception as e:
            self.status(f"Ошибка звука: {e}")

    def _ticker(self):
        """Таймер: закрывает фразы при тишине и опрашивает клавишу «говорить»."""
        try:
            import keyboard
        except ImportError:
            keyboard = None
        while True:
            time.sleep(0.03)
            for seg in list(self.segs.values()):
                seg.tick()
            now = time.monotonic()
            if now - getattr(self, "_lvl_t", 0) > 0.2:  # индикаторы звука в шапке
                self._lvl_t = now
                segs = list(self.segs.items())
                lv = {
                    "them": any(k.startswith("them") and now - sg.last_sound < 0.5 for k, sg in segs),
                    "me": any(k == "me" and sg.enabled and now - sg.last_sound < 0.5 for k, sg in segs),
                }
                if lv != getattr(self, "_lvl", None):
                    self._lvl = lv
                    self.bridge.indicators.emit({"levels": lv})
            if now - self._diag_t > 10:
                self._diag_t = now
                self._write_diag()
            if self.pending_sugg and not self._user_speaking():
                items, header = self.pending_sugg
                self.pending_sugg = None
                self.bridge.suggestions.emit(items, header)
            if self.s["mic_mode"] != "ptt" or keyboard is None:
                if self.ptt_down:
                    self.ptt_down = False
                    if self.passthrough:
                        self.passthrough.muted = False
                continue
            try:
                down = keyboard.is_pressed(self.s["ptt_key"])
            except Exception:
                down = False
            if down != self.ptt_down:
                self.ptt_down = down
                seg = self.segs.get("me")
                if seg:
                    seg.set_enabled(down, flush=True)
                if self.passthrough:
                    self.passthrough.muted = down
                self._indicators()

    def _write_diag(self):
        """Раз в 10 с пишет в diag.log, что происходит со звуком — чтобы найти, где он теряется."""
        import math
        lines = [time.strftime("%H:%M:%S") + f"  источник={self.s.get('them_source')}  режим_мик={self.s.get('mic_mode')}"
                 f"  whisper={getattr(self.engine, 'device', '?')}"]
        for key, sg in sorted(self.segs.items()):
            secs = sg.st_samples / 16000
            db = 20 * math.log10(sg.st_peak_rms + 1e-9)
            thr_db = 20 * math.log10(sg.threshold() + 1e-9)
            d = self.diag.get(key, {})
            lines.append(
                f"   {key:<6} звук_получен={secs:5.1f}с  пик={db:6.1f}дБ  порог={thr_db:6.1f}дБ  "
                f"фраз={sg.st_phrases}  распознано={d.get('final', 0)}  пусто={d.get('empty', 0)}  "
                f"посл=«{d.get('last', '')}»"
            )
            sg.st_samples, sg.st_peak_rms, sg.st_phrases = 0, 0.0, 0
        if self.asr and getattr(self.asr, "last_error", ""):
            lines.append(f"   ошибка распознавания: {self.asr.last_error}")
            self.asr.last_error = ""
        if not self.segs:
            lines.append("   звук не открыт (см. строку статуса)")
        try:
            path = os.path.join(HERE, "diag.log")
            if os.path.exists(path) and os.path.getsize(path) > 300_000:  # не даём файлу разрастись
                os.replace(path, path + ".old")
            with open(path, "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except Exception:
            pass

    # ------------------------------------------------------------ settings
    def open_settings(self):
        from settings import SettingsDialog, save_settings
        if self.overlay.click_through:
            self.overlay.set_click_through(False)
        dlg = SettingsDialog(self.overlay, self.s)
        if dlg.exec():
            old_chat = dict(self.s.get("chat") or {})
            self.s = dlg.result_settings()
            save_settings(self.s)
            if self.s["mic_mode"] != "off":
                self.mic_mode_saved = self.s["mic_mode"]
            self.status("Применяю настройки…")
            threading.Thread(target=self.restart_audio, daemon=True).start()
            if dlg.want_region:
                self.select_chat_region()
            elif (self.s.get("chat") or {}) != old_chat:
                self.start_chat_watch()

    # ------------------------------------------------------------ pipeline
    def _context(self) -> list[str]:
        with self.hlock:
            lines = []
            for e in self.history[-10:]:
                if e["kind"] == "them":
                    lines.append(f"Them: {e['src']}")
                elif e["kind"] == "them_chat":
                    lines.append(f"Them (in chat): {e['src']}")
                elif e["kind"] == "me_en":
                    lines.append(f"Me: {e['src']}")
                elif e["kind"] == "hint" and e.get("tr"):
                    lines.append(f"Me: {e['tr']}")
            return lines

    def _add(self, kind: str, src: str) -> dict:
        e = {"id": next(self.ids), "kind": kind, "src": src, "tr": None}
        with self.hlock:
            self.history.append(e)
            self.history = self.history[-60:]
        self.bridge.entry.emit(dict(e))
        return e

    def _set_tr(self, e: dict, tr: str):
        e["tr"] = tr
        e["streaming"] = False
        if e["kind"] == "hint":
            self._remember_own(tr)
        self.bridge.entry.emit(dict(e))

    def _stream_tr(self, e: dict):
        """Колбэк для потокового перевода: показываем текст по мере генерации."""
        def upd(partial: str):
            if e.get("streaming") is False and e.get("tr"):
                return  # перевод уже готов — запоздавший кусок не показываем
            e["tr"] = partial
            e["streaming"] = True
            self.bridge.entry.emit(dict(e))
        return upd

    def on_asr(self, source: str, kind: str, text: str, lang: str, utt: int):
        st = self.diag.setdefault(source, {"final": 0, "empty": 0, "last": ""})
        if kind == "final":
            st["final"] += 1
            st["last"] = text[:80]
        elif kind == "clear":
            st["empty"] += 1
        if source == "me" and kind != "clear":
            self.me_last = time.monotonic()
            self._match_suggestion(text, final=(kind == "final"))
        if kind == "partial":
            self.bridge.partial.emit(source, text)
            return
        self.bridge.partial.emit(source, "")
        if kind == "clear":
            return
        their = self.a.get("their_language", "en")
        if source.startswith("them"):  # them, them1, them2… — разные устройства вывода
            self._handle_them(text, "them")
        elif lang == their:
            self._add("me_en", text)  # подсказки не убираем: вы можете ещё дочитывать
            self._remember_own(text)
        else:
            ctx = self._context()
            e = self._add("hint", text)
            self.llm.translate(text, "ru-en", ctx, lambda tr, e=e: self._set_tr(e, tr), on_partial=self._stream_tr(e))

    def _handle_them(self, text: str, kind: str):
        """Реплика собеседника: голосом (them) или текстом в чате (them_chat)."""
        ctx = self._context()  # контекст берём ДО добавления новой фразы
        e = self._add(kind, text)
        self.llm.translate(text, "en-ru", ctx, lambda tr, e=e: self._set_tr(e, tr),
                           on_partial=self._stream_tr(e))
        mode = self.s.get("suggest_mode", "questions")
        q = is_question(text)
        if mode == "every" or (mode == "questions" and q):
            # перевод в очереди первым, подсказки — сразу за ним
            self.request_suggestions(question_entry=e if q else None, auto=True)

    # ------------------------------------------------------------ текстовый чат (OCR)
    def _remember_own(self, text: str):
        """Ваши английские фразы — чтобы не принять их в чате за сообщения собеседника."""
        if text:
            self.own_recent.append(re.sub(r"[^a-z0-9]+", "", text.lower()))

    def _is_own(self, text: str) -> bool:
        import difflib
        k = re.sub(r"[^a-z0-9]+", "", text.lower())
        return any(o and (k in o or o in k or difflib.SequenceMatcher(None, k, o).ratio() >= 0.8)
                   for o in list(self.own_recent))

    def _chat_state(self, st: str):
        self.bridge.indicators.emit({"chat": st})

    def start_chat_watch(self):
        from chatwatch import ChatWatcher
        self.stop_chat_watch()
        c = self.s.get("chat") or {}
        if not (c.get("enabled") and c.get("region")) or sys.platform != "win32":
            self._chat_state("off")
            return
        self.chat_watcher = ChatWatcher(
            c["region"], on_message=lambda t: self._handle_them(t, "them_chat"), status=self.status,
            state=self._chat_state, is_own=self._is_own, interval=float(c.get("interval", 1.5)),
            ocr_pref=c.get("ocr", "auto"))
        self._chat_state("wait")
        self.chat_watcher.start()

    def stop_chat_watch(self):
        if self.chat_watcher:
            self.chat_watcher.stop()
            self.chat_watcher = None

    def select_chat_region(self):
        """Ctrl+Alt+R: обвести рамкой окно чата, после чего оверлей начнёт за ним следить."""
        from chatwatch import pick_region
        if sys.platform != "win32":
            self.status("Слежение за чатом работает только в Windows")
            return
        was_visible = self.overlay.isVisible()
        self.overlay.hide()

        def done(region, msg):
            if was_visible:
                self.overlay.show()
            if not region:
                if msg:
                    self.status(msg)
                return
            from settings import save_settings
            self.s["chat"] = {**(self.s.get("chat") or {}), "enabled": True, "region": region}
            save_settings(self.s)
            self.status(f"Чат: {region['exe']}, область {region['w']}×{region['h']} — начинаю следить")
            self.start_chat_watch()

        pick_region(done)

    def on_typed(self, text: str):
        ctx = self._context()
        if CYR.search(text):
            e = self._add("hint", text)
            self.llm.translate(text, "ru-en", ctx, lambda tr, e=e: self._set_tr(e, tr), on_partial=self._stream_tr(e))
        else:  # английский текст — проверить, что он значит
            self._remember_own(text)
            e = self._add("check", text)
            self.llm.translate(text, "en-ru", ctx, lambda tr, e=e: self._set_tr(e, tr), on_partial=self._stream_tr(e))

    def _on_new_suggestions(self, items: list, _header: str):
        self.sugg = items
        self._read_cover: dict[int, set] = {}

    def _user_speaking(self) -> bool:
        seg = self.segs.get("me")
        return bool(seg and seg.in_speech) or time.monotonic() - self.me_last < 2.5

    def _match_suggestion(self, said: str, final: bool):
        """Определяем, какой вариант вы читаете вслух (в т.ч. по частям), чтобы подсветить его."""
        if not self.sugg or not said:
            return
        import difflib
        sw = re.findall(r"[a-z0-9']+", said.lower())
        if len(sw) < 2:
            return
        best = (0.0, -1, None)
        for i, it in enumerate(self.sugg):
            tw = re.findall(r"[a-z0-9']+", it["en"].lower())
            if not tw:
                continue
            m = difflib.SequenceMatcher(None, sw, tw, autojunk=False)
            blocks = m.get_matching_blocks()
            precision = sum(bl.size for bl in blocks) / len(sw)  # какая часть сказанного есть в варианте
            if precision > best[0]:
                covered = {bl.b + k for bl in blocks for k in range(bl.size)}
                best = (precision, i, (covered, len(tw)))
        prec, idx, cov = best
        if prec < 0.6:
            return
        covered, total = cov
        if final:  # копим прочитанные слова между паузами
            acc = self._read_cover.setdefault(idx, set())
            acc |= covered
            covered = acc
        else:
            covered = covered | self._read_cover.get(idx, set())
        self.bridge.suggestion_active.emit(idx, len(covered) / total >= 0.7)

    def _last_them(self) -> dict | None:
        with self.hlock:
            for e in reversed(self.history):
                if e["kind"] in ("them", "them_chat"):
                    return e
                if e["kind"] == "me_en":
                    return None  # после вопроса вы уже что-то ответили
        return None

    def request_suggestions(self, question_entry: dict | None = None, auto: bool = False):
        if question_entry is None and not auto:  # по клавише: отвечаем на последний вопрос, если он был
            last = self._last_them()
            if last and is_question(last["src"]):
                question_entry = last
        self.status("Подбираю, как ответить…" if question_entry else "Подбираю варианты ответа…")

        def cb(items, _q, qe=question_entry):
            header = ""
            if qe:
                header = qe.get("tr") or qe["src"]  # показываем вопрос по-русски, если перевод готов
            if self._user_speaking():  # не подменяем подсказки, пока вы их читаете вслух
                self.pending_sugg = (items, header)
            else:
                self.bridge.suggestions.emit(items, header)
            self.status("Слушаю")

        self.llm.suggest(self._context(), int(self.cfg["suggest"].get("count", 3)), cb,
                         question=question_entry["src"] if question_entry else None,
                         about=self.s.get("about_me", ""))

    # ------------------------------------------------------------ actions
    def on_action(self, name: str):
        from PyQt6.QtWidgets import QApplication

        if name == "toggle_clickthrough":
            self.overlay.set_click_through(not self.overlay.click_through)
        elif name == "settings":
            self.open_settings()
        elif name == "chat_region":
            self.select_chat_region()
        elif name == "suggest":
            self.request_suggestions()
        elif name == "type":
            self.overlay.focus_input()
        elif name == "toggle_mic":
            self.s["mic_mode"] = "off" if self.s["mic_mode"] != "off" else self.mic_mode_saved
            seg = self.segs.get("me")
            if seg:
                seg.set_enabled(self.s["mic_mode"] == "always")
            self._indicators()
        elif name == "toggle_visible":
            self.overlay.setVisible(not self.overlay.isVisible())
        elif name == "clear":
            with self.hlock:
                self.history.clear()
            self.overlay.clear()
        elif name.startswith("copy"):
            i = int(name[4:]) - 1
            if 0 <= i < len(self.sugg):
                QApplication.clipboard().setText(self.sugg[i]["en"])
                self._remember_own(self.sugg[i]["en"])
                self.status(f"Вариант {i+1} скопирован")
        elif name == "quit":
            self.stop_chat_watch()
            self.stop_audio()
            self.overlay.close()
            self.app.quit()


def register_hotkeys(cfg: dict, bridge):
    try:
        import keyboard
    except ImportError:
        print("Модуль keyboard не установлен — горячие клавиши отключены")
        return
    hk = dict(cfg.get("hotkeys", {}))
    hk.setdefault("settings", "ctrl+alt+o")
    hk.setdefault("chat_region", "ctrl+alt+r")
    for name, combo in hk.items():
        if combo:
            keyboard.add_hotkey(combo, lambda n=name: bridge.hotkey.emit(n), suppress=False)
    for i in range(1, 6):
        keyboard.add_hotkey(f"ctrl+alt+{i}", lambda n=i: bridge.hotkey.emit(f"copy{n}"), suppress=False)


def install_excepthook():
    """Ошибка в обработчике не должна закрывать программу: пишем её в errors.log и консоль."""
    import traceback

    def hook(exc_type, exc, tb):
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        print(text, file=sys.stderr)
        try:
            with open(os.path.join(HERE, "errors.log"), "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + text + "\n")
        except Exception:
            pass

    sys.excepthook = hook
    threading.excepthook = lambda a: hook(a.exc_type, a.exc_value, a.exc_traceback)


def main():
    if "--list-devices" in sys.argv:
        from audio import list_devices
        list_devices()
        return

    os.chdir(HERE)
    sys.path.insert(0, HERE)
    from PyQt6.QtWidgets import QApplication
    from overlay import Bridge, Overlay

    cfg = load_config()
    if sys.platform == "win32":
        try:  # comtypes инициализирует COM в потоке, где его впервые импортировали, — пусть это будет главный
            import comtypes  # noqa: F401
        except Exception:
            pass
    app = QApplication(sys.argv)
    # Оверлей — окно типа Tool, Qt не считает его «главным» и завершает программу,
    # когда закрывается последнее обычное окно (например, настройки). Выходим только явно.
    app.setQuitOnLastWindowClosed(False)
    install_excepthook()
    bridge = Bridge()
    ov = Overlay(cfg["overlay"], bridge)
    ov.show()
    if cfg["overlay"].get("start_click_through"):
        ov.set_click_through(True)
    ctl = Controller(cfg, bridge, ov, app)
    ov.apply_ui(ctl.s.get("ui") or {})  # тема, шрифт и прочее из настроек
    register_hotkeys(cfg, bridge)
    ctl.start()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
