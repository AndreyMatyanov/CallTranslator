"""Окно настроек: источник звука собеседника и голосовой ввод. Сохраняется в user_settings.json."""
from __future__ import annotations

import json
import os
import sys
import threading

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QSlider, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(HERE, "user_settings.json")

MIC_MODES = [
    ("off", "Выключен"),
    ("always", "Всегда слушать"),
    ("ptt", "Только пока держу клавишу"),
]
SUGGEST_MODES = [
    ("questions", "Автоматически, когда собеседник задаёт вопрос"),
    ("every", "Автоматически после каждой фразы собеседника"),
    ("hotkey", "Только по клавише Ctrl+Alt+S"),
]
LANGS = [
    (["ru", "en"], "Русский и английский (авто)"),
    (["ru"], "Только русский"),
    (["en"], "Только английский"),
]


def defaults_from_config(cfg: dict) -> dict:
    a = cfg.get("audio", {})
    src = {"type": "device", "name": a["loopback_device"]} if a.get("loopback_device") else {"type": "all"}
    return {
        "them_source": src,
        "mic_device": a.get("mic_device", ""),
        "mic_mode": "always" if a.get("mic_enabled", True) else "off",
        "ptt_key": "right ctrl",
        "my_languages": list(a.get("my_languages", ["ru", "en"])),
        "passthrough": False,
        "passthrough_device": "",
        "suggest_mode": "every" if cfg.get("suggest", {}).get("auto") else "questions",
        "about_me": "",
    }


def load_settings(cfg: dict) -> dict:
    s = defaults_from_config(cfg)
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            s.update(json.load(f))
    except Exception:
        pass
    return s


def save_settings(s: dict):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)


def source_label(src: dict) -> str:
    t = src.get("type")
    if t == "app":
        return src.get("exe", "").removesuffix(".exe")
    if t == "device":
        return src.get("name", "")
    if t == "all":
        return "все устройства"
    return "весь звук"


class SettingsDialog(QDialog):
    _hotkey_read = pyqtSignal(str)

    def __init__(self, parent, current: dict):
        super().__init__(parent)
        self.setWindowTitle("Call Translator — настройки")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setMinimumWidth(560)
        self.cur = dict(current)
        self._hotkey_read.connect(self._on_hotkey_read)

        # --- звук собеседника
        g1 = QGroupBox("Звук собеседника (что переводить)")
        self.src = QComboBox()
        refresh = QPushButton("Обновить")
        refresh.clicked.connect(self._fill_sources)
        row = QHBoxLayout()
        row.addWidget(self.src, 1)
        row.addWidget(refresh)
        test = QPushButton("Проверка звука…")
        test.setToolTip("Показывает уровень звука на каждом устройстве — видно, куда идёт звонок")
        test.clicked.connect(self._sound_test)
        row.addWidget(test)
        note1 = QLabel(
            "«Все устройства вывода» слушает наушники, динамики и гарнитуру одновременно — так звук звонка "
            "найдётся, даже если Windows переключила гарнитуру в режим Hands-Free или звонок идёт на «устройство связи».\n"
            "Ещё точнее — выбрать окно звонка (Discord, Zoom, Telegram, браузер с Meet…): тогда видео и игры "
            "не попадут в субтитры. Захват окна работает на Windows 11; если не поддерживается — "
            "автоматически включатся все устройства. Для браузера захватывается звук всех его вкладок.\n"
            "Значок 🔊 в шапке оверлея загорается зелёным, когда звук собеседника доходит до программы."
        )
        note1.setWordWrap(True)
        note1.setStyleSheet("color:#777")
        l1 = QVBoxLayout(g1)
        l1.addLayout(row)
        l1.addWidget(note1)

        # --- голосовой ввод
        g2 = QGroupBox("Голосовой ввод (ваш микрофон)")
        self.mic = QComboBox()
        self.mode = QComboBox()
        for k, t in MIC_MODES:
            self.mode.addItem(t, k)
        self.ptt = QLineEdit()
        self.ptt.setPlaceholderText("например: right ctrl, caps lock, ctrl+alt+space")
        rec = QPushButton("Назначить…")
        rec.clicked.connect(self._record_hotkey)
        self.rec_btn = rec
        prow = QHBoxLayout()
        prow.addWidget(self.ptt, 1)
        prow.addWidget(rec)
        self.lang = QComboBox()
        for codes, t in LANGS:
            self.lang.addItem(t, codes)
        self.pass_cb = QCheckBox("Скрывать русскую речь от собеседника (через виртуальный кабель)")
        self.pass_dev = QComboBox()
        note2 = QLabel(
            "Как это работает: установите бесплатный VB-Cable (vb-audio.com/Cable), ниже выберите "
            "«CABLE Input», а в Discord/Zoom/Telegram выберите микрофоном «CABLE Output». "
            "Ваш голос пойдёт в звонок через оверлей, а пока вы держите клавишу — собеседник вас "
            "не слышит, и можно спокойно сказать фразу по-русски для подсказки.\n"
            "Пока оверлей закрыт, собеседник вас тоже не слышит — верните обычный микрофон в звонке."
        )
        note2.setWordWrap(True)
        note2.setStyleSheet("color:#777")
        v2 = QVBoxLayout(g2)
        f2 = QFormLayout()
        v2.addLayout(f2)
        f2.addRow("Микрофон:", self.mic)
        f2.addRow("Режим:", self.mode)
        f2.addRow("Клавиша:", prow)
        f2.addRow("Ваш язык:", self.lang)
        f2.addRow(self.pass_cb)
        f2.addRow("Вывод в кабель:", self.pass_dev)
        v2.addWidget(note2)
        self.mode.currentIndexChanged.connect(self._sync)
        self.pass_cb.toggled.connect(self._sync)

        # --- подсказки ответов
        g3 = QGroupBox("Подсказки: как ответить")
        self.sug_mode = QComboBox()
        for k, t in SUGGEST_MODES:
            self.sug_mode.addItem(t, k)
        self.about = QLineEdit()
        self.about.setPlaceholderText("например: разработчик в YWA Interactive, отвечаю за геймплей")
        note3 = QLabel(
            "Когда собеседник задаёт вопрос, под субтитрами появятся 3 варианта ответа: короткий, "
            "развёрнутый и вежливый способ взять паузу или уточнить. Ctrl+Alt+1…3 — скопировать вариант.\n"
            "«О себе» помогает отвечать на вопросы про вас; то, чего модель не знает, она оставит в [скобках]."
        )
        note3.setWordWrap(True)
        note3.setStyleSheet("color:#777")
        v3 = QVBoxLayout(g3)
        f3 = QFormLayout()
        f3.addRow("Когда:", self.sug_mode)
        f3.addRow("О себе:", self.about)
        v3.addLayout(f3)
        v3.addWidget(note3)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        btns.button(QDialogButtonBox.StandardButton.Save).setText("Сохранить")
        btns.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)

        # Вкладки: окно не вырастает выше экрана ноутбука
        tabs = QTabWidget()
        g4 = self._build_look_tab()
        g5 = self._build_chat_tab()
        for title, group in (("Звук собеседника", g1), ("Голосовой ввод", g2), ("Чат (текст)", g5),
                             ("Подсказки ответов", g3), ("Вид", g4)):
            page = QWidget()
            pl = QVBoxLayout(page)
            pl.addWidget(group)
            pl.addStretch(1)
            tabs.addTab(page, title)
        lay = QVBoxLayout(self)
        lay.addWidget(tabs)
        lay.addWidget(btns)

        self._fill_sources()
        self._fill_devices()
        self._load()
        self._sync()

    # ------------------------------------------------------------ заполнение
    def _fill_sources(self):
        keep = self.src.currentData() or self.cur.get("them_source")
        self.src.clear()
        self.src.addItem("🔊 Все устройства вывода (рекомендуется)", {"type": "all"})
        self.src.addItem("🔈 Только устройство вывода по умолчанию", {"type": "system"})
        if sys.platform == "win32":
            try:
                from winaudio import list_windows
                for w in list_windows():
                    title = w["title"] if len(w["title"]) < 60 else w["title"][:57] + "…"
                    self.src.addItem(f"🪟 {title}  —  {w['exe']}", {"type": "app", "exe": w["exe"], "title": w["title"]})
            except Exception as e:
                self.src.addItem(f"(не удалось получить список окон: {e})", {"type": "system"})
            try:
                from audio import enumerate_devices
                for n in enumerate_devices()["loopbacks"]:
                    self.src.addItem(f"🎧 Устройство: {n}", {"type": "device", "name": n})
            except Exception:
                pass
        self._select(self.src, keep, saved_missing_label=True)

    def _fill_devices(self):
        devs = {"mics": [], "outputs": []}
        if sys.platform == "win32":
            try:
                from audio import enumerate_devices
                devs = enumerate_devices()
            except Exception:
                pass
        self.mic.clear()
        self.mic.addItem("Микрофон по умолчанию", "")
        for n in devs["mics"]:
            self.mic.addItem(n, n)
        self.pass_dev.clear()
        for n in devs["outputs"]:
            self.pass_dev.addItem(n, n)
        if self.pass_dev.count() == 0:
            self.pass_dev.addItem("(устройства не найдены)", "")

    def _select(self, combo: QComboBox, value, saved_missing_label=False):
        if value is None:
            return
        for i in range(combo.count()):
            d = combo.itemData(i)
            if d == value or (isinstance(d, dict) and isinstance(value, dict)
                              and d.get("type") == value.get("type") == "app"
                              and d.get("exe", "").lower() == value.get("exe", "").lower()):
                combo.setCurrentIndex(i)
                return
        if saved_missing_label and isinstance(value, dict) and value.get("type") == "app":
            # приложение сейчас закрыто — оставляем выбор, оверлей дождётся его запуска
            combo.addItem(f"🪟 {value.get('exe')} (сейчас не запущено)", value)
            combo.setCurrentIndex(combo.count() - 1)

    # ------------------------------------------------------------ вкладка «Чат (текст)»
    def _build_chat_tab(self) -> QGroupBox:
        self.want_region = False
        c = self.cur.get("chat") or {}
        g = QGroupBox("Сообщения, которые собеседник пишет в чат")
        v = QVBoxLayout(g)
        self.chat_on = QCheckBox("Следить за окном чата и переводить новые сообщения")
        self.chat_on.setChecked(bool(c.get("enabled")))
        reg = c.get("region")
        self.chat_region_lbl = QLabel(
            f"Выбрано: {reg.get('exe', '?')} — «{(reg.get('title') or '')[:50]}», область {reg.get('w')}×{reg.get('h')}"
            if reg else "Область ещё не выбрана.")
        self.chat_region_lbl.setWordWrap(True)
        pick = QPushButton("Выбрать область чата…  (Ctrl+Alt+R)")
        pick.clicked.connect(self._pick_region)
        f = QFormLayout()
        self.chat_interval = QComboBox()
        for sec, t in ((1.0, "каждую секунду"), (1.5, "раз в 1,5 с (рекомендуется)"), (3.0, "раз в 3 с (меньше нагрузка)")):
            self.chat_interval.addItem(t, sec)
        self._select(self.chat_interval, float(c.get("interval", 1.5)))
        self.chat_ocr = QComboBox()
        for k, t in (("auto", "Авто (Windows, иначе RapidOCR)"), ("windows", "Встроенное распознавание Windows"),
                     ("rapid", "RapidOCR (работает без языковых пакетов)")):
            self.chat_ocr.addItem(t, k)
        self._select(self.chat_ocr, c.get("ocr", "auto"))
        f.addRow("Проверять:", self.chat_interval)
        f.addRow("Распознавание:", self.chat_ocr)
        note = QLabel(
            "Обведите рамкой область с сообщениями в окне Discord, Telegram, Zoom и т.п. Оверлей будет следить "
            "за ней, даже если окно сдвинуть или перекрыть (но не свернуть). Новые английские сообщения появятся "
            "с пометкой «чат», на вопросы — варианты ответа.\n"
            "Свои сообщения пропускаются: русские — всегда, английские — если они из подсказок или поля ✎. "
            "Изменили размер окна чата — выберите область заново."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color:#777")
        v.addWidget(self.chat_on)
        v.addWidget(self.chat_region_lbl)
        v.addWidget(pick, 0, Qt.AlignmentFlag.AlignLeft)
        v.addLayout(f)
        v.addWidget(note)
        return g

    def _pick_region(self):
        """Окно настроек закрывается с сохранением, затем запускается выбор области."""
        self.want_region = True
        self.chat_on.setChecked(True)
        self.accept()

    # ------------------------------------------------------------ вкладка «Вид» (предпросмотр сразу на оверлее)
    def _build_look_tab(self) -> QGroupBox:
        from overlay import DEFAULT_UI, THEME_NAMES
        self.ui_orig = {**DEFAULT_UI, **(getattr(self.parent(), "ui", {}) or {}), **(self.cur.get("ui") or {})}
        g = QGroupBox("Как выглядит оверлей")
        f = QFormLayout(g)
        self.theme = QComboBox()
        for k, t in THEME_NAMES:
            self.theme.addItem(t, k)
        self.font_sl, self.font_val = self._slider(14, 34, int(self.ui_orig["font_size"]), "{} px")
        self.op_sl, self.op_val = self._slider(45, 100, int(round(float(self.ui_orig["opacity"]) * 100)), "{} %")
        self.lines = QSpinBox()
        self.lines.setRange(1, 12)
        self.lines.setValue(int(self.ui_orig["max_lines"]))
        self.show_orig = QCheckBox("Показывать английский оригинал над переводом")
        self.show_sru = QCheckBox("Показывать перевод вариантов ответа")
        self.show_orig.setChecked(bool(self.ui_orig["show_original"]))
        self.show_sru.setChecked(bool(self.ui_orig["show_sugg_ru"]))
        f.addRow("Тема:", self.theme)
        f.addRow("Размер текста:", self._row(self.font_sl, self.font_val))
        f.addRow("Непрозрачность фона:", self._row(self.op_sl, self.op_val))
        f.addRow("Реплик на экране, до:", self.lines)
        f.addRow(self.show_orig)
        f.addRow(self.show_sru)
        note = QLabel("Изменения сразу видны на оверлее. «Отмена» вернёт как было.")
        note.setStyleSheet("color:#777")
        f.addRow(note)
        self._select(self.theme, self.ui_orig["theme"])
        for w in (self.theme,):
            w.currentIndexChanged.connect(self._preview_ui)
        for w in (self.font_sl, self.op_sl):
            w.valueChanged.connect(self._preview_ui)
        self.lines.valueChanged.connect(self._preview_ui)
        for w in (self.show_orig, self.show_sru):
            w.toggled.connect(self._preview_ui)
        return g

    def _slider(self, lo, hi, val, fmt):
        sl = QSlider(Qt.Orientation.Horizontal)
        sl.setRange(lo, hi)
        sl.setValue(val)
        lbl = QLabel(fmt.format(val))
        lbl.setMinimumWidth(48)
        sl.valueChanged.connect(lambda v: lbl.setText(fmt.format(v)))
        return sl, lbl

    @staticmethod
    def _row(*widgets):
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        for x in widgets:
            h.addWidget(x, 1 if isinstance(x, QSlider) else 0)
        return w

    def ui_settings(self) -> dict:
        return {
            "theme": self.theme.currentData(),
            "font_size": self.font_sl.value(),
            "opacity": self.op_sl.value() / 100,
            "max_lines": self.lines.value(),
            "show_original": self.show_orig.isChecked(),
            "show_sugg_ru": self.show_sru.isChecked(),
        }

    def _preview_ui(self, *_):
        ov = self.parent()
        if hasattr(ov, "apply_ui"):
            ov.apply_ui(self.ui_settings())

    def reject(self):
        ov = self.parent()
        if hasattr(ov, "apply_ui"):  # «Отмена» — вернуть вид как был
            ov.apply_ui(self.ui_orig)
        super().reject()

    def _load(self):
        self._select(self.mic, self.cur.get("mic_device", ""))
        self._select(self.mode, self.cur.get("mic_mode", "always"))
        self.ptt.setText(self.cur.get("ptt_key", "right ctrl"))
        self._select(self.lang, self.cur.get("my_languages", ["ru", "en"]))
        self.pass_cb.setChecked(bool(self.cur.get("passthrough")))
        if self.cur.get("passthrough_device"):
            self._select(self.pass_dev, self.cur["passthrough_device"])
        self._select(self.sug_mode, self.cur.get("suggest_mode", "questions"))
        self.about.setText(self.cur.get("about_me", ""))

    def _sync(self):
        self.ptt.setEnabled(self.mode.currentData() == "ptt")
        self.rec_btn.setEnabled(self.mode.currentData() == "ptt")
        self.pass_dev.setEnabled(self.pass_cb.isChecked())

    def _sound_test(self):
        dlg = SoundTestDialog(self, self.mic.currentData() or "")
        dlg.exec()
        picked = dlg.picked
        if picked:  # пользователь нажал «Слушать это устройство»
            self._select(self.src, {"type": "device", "name": picked})
            if (self.src.currentData() or {}).get("name") != picked:
                self.src.addItem(f"🎧 Устройство: {picked}", {"type": "device", "name": picked})
                self.src.setCurrentIndex(self.src.count() - 1)

    # ------------------------------------------------------------ клавиша
    def _record_hotkey(self):
        try:
            import keyboard
        except ImportError:
            return
        self.rec_btn.setText("Нажмите…")
        self.rec_btn.setEnabled(False)

        def worker():
            try:
                hk = keyboard.read_hotkey(suppress=False)
            except Exception:
                hk = ""
            self._hotkey_read.emit(hk)

        threading.Thread(target=worker, daemon=True).start()

    def _on_hotkey_read(self, hk: str):
        if hk:
            self.ptt.setText(hk)
        self.rec_btn.setText("Назначить…")
        self._sync()

    # ------------------------------------------------------------ результат
    def result_settings(self) -> dict:
        s = dict(self.cur)
        s["them_source"] = self.src.currentData() or {"type": "system"}
        s["mic_device"] = self.mic.currentData() or ""
        s["mic_mode"] = self.mode.currentData()
        s["ptt_key"] = self.ptt.text().strip() or "right ctrl"
        s["my_languages"] = self.lang.currentData()
        s["passthrough"] = self.pass_cb.isChecked() and bool(self.pass_dev.currentData())
        s["passthrough_device"] = self.pass_dev.currentData() or ""
        s["suggest_mode"] = self.sug_mode.currentData()
        s["about_me"] = self.about.text().strip()
        s["ui"] = self.ui_settings()
        s["chat"] = {**(self.cur.get("chat") or {}), "enabled": self.chat_on.isChecked(),
                     "interval": self.chat_interval.currentData(), "ocr": self.chat_ocr.currentData()}
        return s


class SoundTestDialog(QDialog):
    """Живые индикаторы уровня для всех устройств вывода и микрофона.

    Включите звонок (или видео со звуком) и посмотрите, какая полоска двигается, когда говорит собеседник.
    """

    def __init__(self, parent, mic_hint: str = ""):
        from PyQt6.QtCore import QTimer
        from PyQt6.QtWidgets import QGridLayout, QProgressBar

        super().__init__(parent)
        self.setWindowTitle("Проверка звука")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setMinimumWidth(620)
        self.picked = ""
        self.levels: dict[int, float] = {}
        self.peaks: dict[int, float] = {}
        self.streams = []
        self.rows = []

        info = QLabel(
            "Пусть собеседник что-нибудь скажет (или включите любое видео со звуком). "
            "Полоска того устройства, куда идёт звук, начнёт двигаться. Если двигается не "
            "«Наушники/Динамики по умолчанию», нажмите «Слушать это» напротив неё."
        )
        info.setWordWrap(True)
        grid = QGridLayout()
        lay = QVBoxLayout(self)
        lay.addWidget(info)
        lay.addLayout(grid)
        self.err = QLabel("")
        self.err.setStyleSheet("color:#c0392b")
        self.err.setWordWrap(True)
        lay.addWidget(self.err)
        close = QPushButton("Закрыть")
        close.clicked.connect(self.reject)
        lay.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)

        try:
            import pyaudiowpatch as pyaudio
            self.pa = pyaudio.PyAudio()
            wasapi = self.pa.get_host_api_info_by_type(pyaudio.paWASAPI)
            devs = []
            for i in range(self.pa.get_device_count()):
                d = self.pa.get_device_info_by_index(i)
                if d["hostApi"] == wasapi["index"] and d.get("isLoopbackDevice"):
                    devs.append(("🔊", d))
            mic = None
            for i in range(self.pa.get_device_count()):
                d = self.pa.get_device_info_by_index(i)
                if (d["hostApi"] == wasapi["index"] and not d.get("isLoopbackDevice") and d["maxInputChannels"] > 0
                        and (mic_hint.lower() in d["name"].lower() if mic_hint else i == wasapi["defaultInputDevice"])):
                    mic = d
                    break
            if mic:
                devs.append(("🎙", mic))
        except Exception as e:
            self.err.setText(f"Не удалось получить список устройств: {e}")
            devs = []

        for r, (icon, d) in enumerate(devs):
            name = d["name"].replace(" [Loopback]", "")
            lbl = QLabel(f"{icon} {name}")
            lbl.setWordWrap(True)
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setTextVisible(False)
            bar.setFixedWidth(180)
            grid.addWidget(lbl, r, 0)
            grid.addWidget(bar, r, 1)
            if icon == "🔊":
                b = QPushButton("Слушать это")
                b.clicked.connect(lambda _=False, n=d["name"]: self._pick(n))
                grid.addWidget(b, r, 2)
            self.rows.append((d["index"], bar, lbl))
            self._open(d)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(80)

    def _open(self, d: dict):
        import math

        import numpy as np
        import pyaudiowpatch as pyaudio

        idx, ch = d["index"], max(1, int(d["maxInputChannels"]))

        def cb(data, n, ti, st):
            a = np.frombuffer(data, dtype=np.float32)
            rms = float(np.sqrt(np.mean(a * a))) if a.size else 0.0
            db = 20 * math.log10(rms + 1e-9)
            self.levels[idx] = max(0.0, min(100.0, (db + 60) * 100 / 60))  # -60 дБ..0 дБ → 0..100
            return (None, pyaudio.paContinue)

        try:
            st = self.pa.open(format=pyaudio.paFloat32, channels=ch, rate=int(d["defaultSampleRate"]), input=True,
                              input_device_index=idx, frames_per_buffer=int(d["defaultSampleRate"] * 0.05),
                              stream_callback=cb)
            st.start_stream()
            self.streams.append(st)
        except Exception as e:
            self.err.setText(self.err.text() + f"\nНе открылось «{d['name']}»: {e}")

    def _tick(self):
        for idx, bar, lbl in self.rows:
            v = self.levels.get(idx, 0.0)
            self.levels[idx] = v * 0.6  # плавное затухание, если устройство замолчало (loopback без звука молчит)
            bar.setValue(int(v))
            if v > 25:
                self.peaks[idx] = 1.0
            lbl.setStyleSheet("color:#1e8e3e;font-weight:bold" if self.peaks.get(idx) else "")

    def _pick(self, name: str):
        self.picked = name
        self.accept()

    def done(self, r):
        self.timer.stop()
        for st in self.streams:
            try:
                st.stop_stream()
                st.close()
            except Exception:
                pass
        try:
            self.pa.terminate()
        except Exception:
            pass
        super().done(r)
