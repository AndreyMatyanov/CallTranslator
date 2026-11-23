"""Оверлей: прозрачное окно поверх всех окон, «сквозные» клики, скрытие из демонстрации экрана.

Вид: карточки реплик с цветной полосой (собеседник / вы / подсказка), тихая шапка с индикаторами,
панель вариантов ответа, скрываемое поле ввода, плашка для предупреждений, темы.
"""
from __future__ import annotations

import ctypes
import html
import json
import os
import sys

from PyQt6.QtCore import QObject, QPoint, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QFont, QFontDatabase, QGuiApplication, QPainter, QPainterPath
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLineEdit, QMenu, QPushButton,
    QScrollArea, QSizeGrip, QSizePolicy, QVBoxLayout, QWidget,
)

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "overlay_state.json")

# ---------------------------------------------------------------- темы
THEMES = {
    "dark": dict(
        bg=(17, 19, 24), text="#E6E9EF", muted="#8A93A3", faint="#5B6372",
        border="rgba(255,255,255,0.09)", card="rgba(255,255,255,0.045)", active="rgba(122,162,247,0.16)",
        key_bg="rgba(255,255,255,0.10)", input_bg="rgba(255,255,255,0.06)", btn_hover="rgba(255,255,255,0.10)",
        them="#7AA2F7", me="#9ECE6A", hint="#E0AF68", warn="#E0AF68", ok="#73D19A",
        banner="rgba(224,175,104,0.12)",
    ),
    "light": dict(
        bg=(248, 249, 251), text="#1F2328", muted="#57606A", faint="#8C959F",
        border="rgba(0,0,0,0.10)", card="rgba(0,0,0,0.035)", active="rgba(47,111,235,0.12)",
        key_bg="rgba(0,0,0,0.07)", input_bg="rgba(0,0,0,0.045)", btn_hover="rgba(0,0,0,0.07)",
        them="#2F6FEB", me="#1A7F37", hint="#9A6700", warn="#9A6700", ok="#1A7F37",
        banner="rgba(154,103,0,0.10)",
    ),
    "contrast": dict(
        bg=(0, 0, 0), text="#FFFFFF", muted="#C9D1D9", faint="#8B949E",
        border="rgba(255,255,255,0.22)", card="rgba(255,255,255,0.07)", active="rgba(130,170,255,0.25)",
        key_bg="rgba(255,255,255,0.18)", input_bg="rgba(255,255,255,0.10)", btn_hover="rgba(255,255,255,0.16)",
        them="#82AAFF", me="#A6E22E", hint="#FFD866", warn="#FFD866", ok="#A6E22E",
        banner="rgba(255,216,102,0.16)",
    ),
}
THEME_NAMES = [("dark", "Тёмная"), ("light", "Светлая"), ("contrast", "Контрастная")]
DEFAULT_UI = {
    "theme": "dark", "font_size": 20, "opacity": 0.88, "max_lines": 6,
    "show_original": True, "show_sugg_ru": True,
}

FONT_FAMILIES = ["Segoe UI Variable Text", "Segoe UI", "Inter", "Noto Sans", "DejaVu Sans"]
# Иконки: на Windows — системный шрифт значков, иначе обычные символы
ICONS = {
    "settings": ("", "⚙"), "edit": ("", "✎"), "lock": ("", "▣"),
    "unlock": ("", "▢"), "close": ("", "✕"),
}
PROBLEM_WORDS = ("⚠", "ошибка", "не установлена", "не запущена", "не удалось", "недоступен",
                 "не отвечает", "не загрузилась", "не найден", "слишком долго")
RESOLVED_WORDS = ("готов", "запущена", "скачана", "слушаю ")


def _icon_font() -> str | None:
    fams = set(QFontDatabase.families())
    return next((f for f in ("Segoe Fluent Icons", "Segoe MDL2 Assets") if f in fams), None)


class Bridge(QObject):
    """Сигналы из рабочих потоков в GUI-поток."""
    entry = pyqtSignal(dict)            # новая или обновлённая запись (по id)
    partial = pyqtSignal(str, str)      # source, текст черновика ("" — убрать)
    suggestions = pyqtSignal(list, str) # [{"en","ru"}], вопрос, на который отвечаем ("" — общий случай)
    suggestion_active = pyqtSignal(int, bool)  # какой вариант вы читаете, дочитан ли он
    status = pyqtSignal(str)
    indicators = pyqtSignal(dict)       # mic_text / source / device / levels
    hotkey = pyqtSignal(str)


# ---------------------------------------------------------------- элементы
class IconButton(QPushButton):
    def __init__(self, icon: str, tip: str, icon_font: str | None):
        super().__init__()
        self.icon_font = icon_font
        self.setObjectName("iconbtn")
        self.setToolTip(tip)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(26, 24)
        self.set_icon(icon)

    def set_icon(self, name: str):
        glyph, fallback = ICONS[name]
        if self.icon_font:
            f = QFont(self.icon_font)
            f.setPointSizeF(9.5)
            self.setFont(f)
            self.setText(glyph)
        else:
            self.setText(fallback)


class Card(QFrame):
    """Одна реплика: полоса слева цвета говорящего, подпись, крупный текст."""

    def __init__(self, ov: "Overlay", e: dict):
        super().__init__()
        self.ov = ov
        self.setObjectName("card")
        self.setProperty("kind", e["kind"])
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 6, 12, 7)
        lay.setSpacing(2)
        self.meta, self.main, self.sub = QLabel(), QLabel(), QLabel()
        for lbl, name in ((self.meta, "meta"), (self.main, "main"), (self.sub, "sub")):
            lbl.setObjectName(name)
            lbl.setWordWrap(True)
            lbl.setTextFormat(Qt.TextFormat.RichText)
            lay.addWidget(lbl)
        self.e = e
        self.render()

    def update_entry(self, e: dict):
        self.e = e
        self.render()

    def render(self):
        t, e, ui = self.ov.t, self.e, self.ov.ui
        k = e["kind"]
        src = html.escape(e.get("src", ""))
        tr = e.get("tr")
        cursor = f'<span style="color:{t["them"]}"> ▍</span>' if tr and e.get("streaming") else ""
        pending = f'<span style="color:{t["faint"]}">…</span>'
        trh = (html.escape(tr) + cursor) if tr else pending

        def tag(text, color):
            return f'<span style="color:{color};font-weight:600">{text}</span>'

        self.sub.hide()
        if k in ("them", "them_chat"):
            orig = f'&nbsp;&nbsp;<span style="color:{t["muted"]}">{src}</span>' if ui["show_original"] else ""
            label = "Собеседник" if k == "them" else "Собеседник · чат"
            self.meta.setText(tag(label, t["them"]) + orig)
            self.main.setText(trh)
        elif k == "me_en":
            self.meta.setText(tag("Вы", t["me"]))
            self.main.setText(f'<span style="color:{t["muted"]}">{src}</span>')
        elif k == "hint":
            self.meta.setText(tag("Как сказать", t["hint"]) + f'&nbsp;&nbsp;<span style="color:{t["muted"]}">{src}</span>')
            self.main.setText(f'<span style="color:{t["hint"]}">{trh}</span>' if tr else trh)
        elif k == "check":
            self.meta.setText(tag("Проверка", t["muted"]))
            self.main.setText(src)
            self.sub.setText(f'<span style="color:{t["muted"]}">= {trh}</span>')
            self.sub.show()


class SuggCard(QFrame):
    """Вариант ответа: «клавиша» с номером, английский текст, перевод. Клик — скопировать."""

    def __init__(self, ov: "Overlay", i: int, item: dict):
        super().__init__()
        self.ov, self.i = ov, i
        self.setObjectName("sugg")
        self.setProperty("active", False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(f"Клик или Ctrl+Alt+{i} — скопировать")
        row = QHBoxLayout(self)
        row.setContentsMargins(8, 6, 10, 6)
        row.setSpacing(10)
        self.key = QLabel(str(i))
        self.key.setObjectName("key")
        self.key.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.key.setFixedSize(24, 24)
        row.addWidget(self.key, 0, Qt.AlignmentFlag.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(1)
        self.en = QLabel(html.escape(item["en"]))
        self.en.setObjectName("sen")
        self.ru = QLabel(html.escape(item.get("ru", "")))
        self.ru.setObjectName("sru")
        for lbl in (self.en, self.ru):
            lbl.setWordWrap(True)
            lbl.setTextFormat(Qt.TextFormat.RichText)
            col.addWidget(lbl)
        self.ru.setVisible(bool(item.get("ru")) and ov.ui["show_sugg_ru"])
        row.addLayout(col, 1)

    def set_state(self, active: bool, dim: bool, said: bool):
        self.setProperty("active", active)
        self.setProperty("dim", dim)
        self.key.setText("✓" if (active and said) else str(self.i))
        for w in (self, self.key, self.en, self.ru):
            w.style().unpolish(w)
            w.style().polish(w)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.ov.action.emit(f"copy{self.i}")
            self.key.setText("⧉")
            QTimer.singleShot(700, lambda: self.key.setText(str(self.i)))
        else:
            super().mousePressEvent(e)


# ---------------------------------------------------------------- окно
class Overlay(QWidget):
    typed = pyqtSignal(str)
    action = pyqtSignal(str)

    def __init__(self, cfg: dict, bridge: Bridge):
        super().__init__()
        self.cfg = cfg
        self.ui = dict(DEFAULT_UI)
        for k in ("font_size", "opacity", "max_lines"):
            if k in cfg:
                self.ui[k] = cfg[k]
        self.t = THEMES[self.ui["theme"]]
        self.fs = int(self.ui["font_size"])
        self.cards: dict[int, Card] = {}
        self.order: list[int] = []
        self.partials: dict[str, str] = {}
        self.sugg_cards: list[SuggCard] = []
        self.sugg_q = ""
        self.click_through = False
        self.mic_text, self.source, self.device = "микрофон", "", ""
        self.levels: dict = {}
        self._status = "Запуск…"
        self._drag: QPoint | None = None

        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle("Call Translator")
        self.setMinimumSize(380, 200)
        f = QFont()
        f.setFamilies(FONT_FAMILIES)
        self.setFont(f)
        icon_font = _icon_font()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 12)  # место под тень
        self.panel = QFrame()
        self.panel.setObjectName("panel")
        outer.addWidget(self.panel)
        # Тень рисуем сами в paintEvent: QGraphicsDropShadowEffect на полупрозрачном окне Windows
        # выходит за его края и даёт «UpdateLayeredWindowIndirect failed» и сбои перерисовки.

        lay = QVBoxLayout(self.panel)
        lay.setContentsMargins(14, 8, 10, 12)
        lay.setSpacing(8)

        # --- шапка
        head = QHBoxLayout()
        head.setSpacing(6)
        self.src_pill = QLabel()
        self.src_pill.setObjectName("pill")
        self.mic_pill = QLabel()
        self.mic_pill.setObjectName("pill")
        self.chat_pill = QLabel()
        self.chat_pill.setObjectName("pill")
        self.chat_pill.hide()
        self.chat_state = "off"
        self.status_lbl = QLabel()
        self.status_lbl.setObjectName("status")
        self.status_lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.btn_input = IconButton("edit", "Написать фразу (Ctrl+Alt+I)", icon_font)
        self.btn_lock = IconButton("unlock", "Закрепить: клики проходят сквозь оверлей (Ctrl+Alt+T)", icon_font)
        self.btn_settings = IconButton("settings", "Настройки (Ctrl+Alt+O)", icon_font)
        self.btn_input.clicked.connect(self.toggle_input)
        self.btn_lock.clicked.connect(lambda: self.action.emit("toggle_clickthrough"))
        self.btn_settings.clicked.connect(lambda: self.action.emit("settings"))
        for w in (self.src_pill, self.mic_pill, self.chat_pill):
            head.addWidget(w)
        head.addWidget(self.status_lbl, 1)
        for b in (self.btn_input, self.btn_lock, self.btn_settings):
            head.addWidget(b)
        lay.addLayout(head)

        # --- плашка предупреждений
        self.banner = QFrame()
        self.banner.setObjectName("banner")
        bl = QHBoxLayout(self.banner)
        bl.setContentsMargins(10, 6, 4, 6)
        self.banner_lbl = QLabel()
        self.banner_lbl.setObjectName("bannertext")
        self.banner_lbl.setWordWrap(True)
        bclose = IconButton("close", "Скрыть", icon_font)
        bclose.clicked.connect(self.banner.hide)
        bl.addWidget(self.banner_lbl, 1)
        bl.addWidget(bclose, 0, Qt.AlignmentFlag.AlignTop)
        self.banner.hide()
        lay.addWidget(self.banner)

        # --- лента реплик
        self.scroll = QScrollArea()
        self.scroll.setObjectName("feed")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.feed = QWidget()
        self.feed.setObjectName("feedbody")
        self.feed_lay = QVBoxLayout(self.feed)
        self.feed_lay.setContentsMargins(0, 0, 4, 0)
        self.feed_lay.setSpacing(6)
        self.feed_lay.addStretch(1)  # реплики прижаты к низу, как в мессенджере
        self.empty = QLabel("Субтитры появятся, когда собеседник заговорит.\n"
                            "Скажите или напишите фразу по-русски — покажу, как сказать её по-английски.")
        self.empty.setObjectName("empty")
        self.empty.setWordWrap(True)
        self.feed_lay.addWidget(self.empty)
        self.draft = QLabel()
        self.draft.setObjectName("draft")
        self.draft.setWordWrap(True)
        self.draft.hide()
        self.feed_lay.addWidget(self.draft)
        self.scroll.setWidget(self.feed)
        self.scroll.verticalScrollBar().rangeChanged.connect(
            lambda _a, b: self.scroll.verticalScrollBar().setValue(b))  # всегда показываем последнее
        self.scroll.viewport().installEventFilter(self)  # при изменении высоты — заново подобрать реплики
        lay.addWidget(self.scroll, 1)

        # --- варианты ответа
        self.sugg_box = QFrame()
        self.sugg_box.setObjectName("suggbox")
        sl = QVBoxLayout(self.sugg_box)
        sl.setContentsMargins(0, 2, 0, 0)
        sl.setSpacing(5)
        self.sugg_title = QLabel()
        self.sugg_title.setObjectName("suggtitle")
        self.sugg_title.setTextFormat(Qt.TextFormat.RichText)
        self.sugg_title.setWordWrap(True)
        sl.addWidget(self.sugg_title)
        self.sugg_list = QVBoxLayout()
        self.sugg_list.setSpacing(4)
        sl.addLayout(self.sugg_list)
        self.sugg_box.hide()
        lay.addWidget(self.sugg_box)

        # --- поле ввода (по кнопке ✎ / Ctrl+Alt+I)
        self.input = QLineEdit()
        self.input.setObjectName("input")
        self.input.setPlaceholderText("Напишите по-русски — покажу по-английски. Enter — перевести, Esc — скрыть")
        self.input.returnPressed.connect(self._on_enter)
        self.input.hide()
        lay.addWidget(self.input)

        self.grip = QSizeGrip(self.panel)
        self.grip.setFixedSize(14, 14)
        self.grip.setStyleSheet("background:transparent")

        bridge.entry.connect(self.on_entry)
        bridge.partial.connect(self.on_partial)
        bridge.suggestions.connect(self.on_suggestions)
        bridge.suggestion_active.connect(self.on_suggestion_active)
        bridge.status.connect(self.on_status)
        bridge.indicators.connect(self.on_indicators)

        self._restore_geometry()
        self.apply_ui(self.ui)

    # ---------------------------------------------------------------- оформление
    def apply_ui(self, ui: dict):
        """Тема, размер шрифта, прозрачность и прочее — применяется сразу (в т.ч. для предпросмотра)."""
        self.ui = {**DEFAULT_UI, **self.ui, **(ui or {})}
        self.t = t = THEMES.get(self.ui["theme"], THEMES["dark"])
        self.fs = fs = int(self.ui["font_size"])
        op = float(self.ui["opacity"])
        r, g, b = t["bg"]
        alpha = op * (0.92 if self.click_through else 1.0)
        meta, small, sugg_en, sugg_ru, head = (max(11, int(fs * k)) for k in (0.62, 0.72, 0.82, 0.66, 0.6))
        border = t["border"]
        # только для панели: дочерние окна (настройки) не задеваем
        self.panel.setStyleSheet(f"""
            QFrame#panel {{ background: rgba({r},{g},{b},{alpha:.3f}); border: 1px solid {border}; border-radius: 14px; }}
            QLabel {{ color: {t['text']}; background: transparent; }}
            QLabel#pill {{ color: {t['muted']}; font-size: {head}px; padding: 2px 8px; border-radius: 10px;
                           background: {t['card']}; }}
            QLabel#status {{ color: {t['faint']}; font-size: {head}px; padding-left: 4px; }}
            QPushButton#iconbtn {{ color: {t['muted']}; background: transparent; border: none; border-radius: 6px; }}
            QPushButton#iconbtn:hover {{ background: {t['btn_hover']}; color: {t['text']}; }}
            QFrame#banner {{ background: {t['banner']}; border-left: 3px solid {t['warn']}; border-radius: 8px; }}
            QLabel#bannertext {{ color: {t['text']}; font-size: {small}px; }}
            QScrollArea#feed, QWidget#feedbody {{ background: transparent; }}
            QFrame#card {{ background: {t['card']}; border-radius: 8px; border-left: 3px solid {t['faint']}; }}
            QFrame#card[kind="them"] {{ border-left-color: {t['them']}; }}
            QFrame#card[kind="them_chat"] {{ border-left: 3px dashed {t['them']}; }}
            QFrame#card[kind="me_en"] {{ border-left-color: {t['me']}; background: transparent; }}
            QFrame#card[kind="hint"] {{ border-left-color: {t['hint']}; }}
            QFrame#card[kind="check"] {{ border-left-color: {t['muted']}; }}
            QLabel#meta {{ color: {t['muted']}; font-size: {meta}px; }}
            QLabel#main {{ color: {t['text']}; font-size: {fs}px; }}
            QFrame#card[kind="me_en"] QLabel#main {{ font-size: {small}px; }}
            QLabel#sub {{ font-size: {small}px; }}
            QLabel#empty {{ color: {t['faint']}; font-size: {small}px; padding: 4px 2px; }}
            QLabel#draft {{ color: {t['faint']}; font-size: {small}px; font-style: italic; padding: 0 2px; }}
            QLabel#suggtitle {{ color: {t['muted']}; font-size: {meta}px; padding: 0 2px; }}
            QFrame#sugg {{ background: {t['card']}; border: 1px solid transparent; border-radius: 9px; }}
            QFrame#sugg:hover {{ border: 1px solid {t['border']}; }}
            QFrame#sugg[active="true"] {{ background: {t['active']}; border: 1px solid {t['them']}; }}
            QLabel#key {{ background: {t['key_bg']}; color: {t['muted']}; border-radius: 6px;
                          font-size: 12px; font-weight: 600; }}
            QFrame#sugg[active="true"] QLabel#key {{ background: {t['them']}; color: rgb({r},{g},{b}); }}
            QLabel#sen {{ color: {t['me']}; font-size: {sugg_en}px; }}
            QFrame#sugg[dim="true"] QLabel#sen {{ color: {t['faint']}; }}
            QLabel#sru {{ color: {t['muted']}; font-size: {sugg_ru}px; }}
            QFrame#sugg[dim="true"] QLabel#sru {{ color: {t['faint']}; }}
            QLineEdit#input {{ background: {t['input_bg']}; color: {t['text']}; border: 1px solid {t['border']};
                               border-radius: 8px; padding: 6px 10px; font-size: {small}px; }}
            QLineEdit#input:focus {{ border: 1px solid {t['them']}; }}
            QToolTip {{ color: {t['text']}; background: rgb({r},{g},{b}); border: 1px solid {t['border']}; }}
        """)
        for c in self.cards.values():
            c.render()
        for sc in self.sugg_cards:
            sc.ru.setVisible(bool(sc.ru.text()) and self.ui["show_sugg_ru"])
        self._apply_max_lines()
        self._update_head()
        self._render_sugg_title()
        self._render_draft()

    # ---------------------------------------------------------------- окно: трюки Windows
    def showEvent(self, e):
        super().showEvent(e)
        if sys.platform == "win32" and self.cfg.get("hide_from_screen_share", True):
            try:  # WDA_EXCLUDEFROMCAPTURE: собеседник не увидит оверлей при демонстрации экрана
                ctypes.windll.user32.SetWindowDisplayAffinity(int(self.winId()), 0x11)
            except Exception:
                pass

    def set_click_through(self, on: bool):
        self.click_through = on
        if sys.platform == "win32":
            hwnd = int(self.winId())
            u = ctypes.windll.user32
            GWL_EXSTYLE, WS_EX_TRANSPARENT, WS_EX_LAYERED = -20, 0x20, 0x80000
            style = u.GetWindowLongW(hwnd, GWL_EXSTYLE)
            style = (style | WS_EX_LAYERED | WS_EX_TRANSPARENT) if on else (style & ~WS_EX_TRANSPARENT)
            u.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
        else:
            self.setWindowFlag(Qt.WindowType.WindowTransparentForInput, on)
            self.show()
        self.btn_lock.set_icon("lock" if on else "unlock")
        if on:
            self.input.hide()
        self.apply_ui(self.ui)

    def toggle_input(self):
        if self.input.isVisible():
            self.input.hide()
        else:
            self.focus_input()

    def focus_input(self):
        if self.click_through:
            self.set_click_through(False)
        self.input.show()
        self.show()
        self.raise_()
        self.activateWindow()
        if sys.platform == "win32":
            try:
                ctypes.windll.user32.SetForegroundWindow(int(self.winId()))
            except Exception:
                pass
        self.input.setFocus()

    def paintEvent(self, e):
        """Мягкая тень вокруг панели — строго внутри окна."""
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        g = self.panel.geometry()
        steps = 8
        for i in range(steps, 0, -1):
            a = int(34 * (1 - i / (steps + 1)) ** 2)  # плотнее у края панели, прозрачнее наружу
            r = g.adjusted(-i, -i + 2, i, i + 3).intersected(self.rect())
            path = QPainterPath()
            path.addRoundedRect(r.toRectF() if hasattr(r, "toRectF") else r, 14 + i, 14 + i)
            p.fillPath(path, QColor(0, 0, 0, a))
        p.end()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape and self.input.isVisible():
            self.input.hide()
        else:
            super().keyPressEvent(e)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag is not None:
            self.move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, e):
        self._drag = None
        self._save_geometry()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.grip.move(self.panel.width() - self.grip.width() - 3, self.panel.height() - self.grip.height() - 3)
        self._fit_later()
        self._update_head()

    def eventFilter(self, obj, ev):
        if obj is self.scroll.viewport() and ev.type() == ev.Type.Resize:
            self._fit_later()
        return False

    def contextMenuEvent(self, e):
        m = QMenu(self)
        for title, act in [
            ("Настройки…", "settings"),
            ("Подсказать ответ", "suggest"),
            ("Написать фразу", "type"),
            ("Следить за чатом: выбрать область…", "chat_region"),
            ("Микрофон вкл/выкл", "toggle_mic"),
            ("Очистить", "clear"),
            ("Закрепить (сквозные клики)", "toggle_clickthrough"),
            (None, None),
            ("Выход", "quit"),
        ]:
            if title is None:
                m.addSeparator()
                continue
            a = QAction(title, m)
            a.triggered.connect(lambda _=False, x=act: self.action.emit(x))
            m.addAction(a)
        m.exec(e.globalPos())

    def closeEvent(self, e):
        self._save_geometry()
        super().closeEvent(e)

    def _restore_geometry(self):
        w, h = int(self.cfg.get("width", 760)), int(self.cfg.get("height", 420))
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                s = json.load(f)
            self.setGeometry(s["x"], s["y"], s["w"], s["h"])
            return
        except Exception:
            pass
        scr = QGuiApplication.primaryScreen().availableGeometry()
        self.setGeometry(scr.x() + (scr.width() - w) // 2, scr.y() + scr.height() - h - 40, w, h)

    def _save_geometry(self):
        g = self.geometry()
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump({"x": g.x(), "y": g.y(), "w": g.width(), "h": g.height()}, f)
        except Exception:
            pass

    # ---------------------------------------------------------------- шапка и статус
    def on_status(self, s: str):
        low = s.lower()
        if any(w in low for w in PROBLEM_WORDS):
            self.banner_lbl.setText(s.lstrip("⚠ ").strip())
            self.banner.show()
            return  # в шапке не дублируем
        elif self.banner.isVisible() and any(w in low for w in RESOLVED_WORDS) and "⚠" not in self.banner_lbl.text():
            self.banner.hide()
        self._status = s
        self._update_head()

    def on_indicators(self, d: dict):
        self.mic_text = d.get("mic_text", self.mic_text).replace("🎙", "").strip() or self.mic_text
        self.source = d.get("source", self.source)
        self.device = d.get("device", self.device)
        self.levels = d.get("levels", self.levels)
        self.chat_state = d.get("chat", self.chat_state)
        self._update_head()

    def _update_head(self):
        t = self.t

        def dot(on):
            return f'<span style="color:{t["ok"] if on else t["faint"]}">●</span>'

        if self.source:
            self.src_pill.setText(f'{dot(self.levels.get("them"))}&nbsp;{html.escape(self.source)}')
            self.src_pill.setToolTip("Звук собеседника — точка горит, когда он доходит до программы")
            self.src_pill.show()
        else:
            self.src_pill.hide()
        self.mic_pill.setText(f'{dot(self.levels.get("me"))}&nbsp;{html.escape(self.mic_text)}')
        self.mic_pill.setToolTip("Ваш микрофон (Ctrl+Alt+M — вкл/выкл)")
        if self.chat_state == "off":
            self.chat_pill.hide()
        else:
            col = t["ok"] if self.chat_state == "ok" else t["warn"]
            self.chat_pill.setText(f'<span style="color:{col}">●</span>&nbsp;чат')
            self.chat_pill.setToolTip("Слежу за окном чата" if self.chat_state == "ok"
                                      else "Окно чата сейчас не видно (закрыто, свёрнуто или не найдено)")
            self.chat_pill.show()
        status = self._status
        if self.device:
            status = f"{self.device.upper()} · {status}"
        fm = self.status_lbl.fontMetrics()
        width = max(40, self.status_lbl.width() - 6)
        self.status_lbl.setText(fm.elidedText(status, Qt.TextElideMode.ElideRight, width))
        self.status_lbl.setToolTip(status)

    # ---------------------------------------------------------------- лента
    def _on_enter(self):
        txt = self.input.text().strip()
        if txt:
            self.typed.emit(txt)
            self.input.clear()

    def on_entry(self, e: dict):
        c = self.cards.get(e["id"])
        if c:
            c.update_entry(e)
            self._fit_later()  # перевод мог стать длиннее
            return
        c = Card(self, e)
        self.cards[e["id"]] = c
        self.order.append(e["id"])
        self.feed_lay.insertWidget(self.feed_lay.count() - 1, c)  # перед черновиком
        self.empty.hide()
        while len(self.order) > 60:  # старые реплики удаляем совсем
            old = self.cards.pop(self.order.pop(0))
            old.setParent(None)
            old.deleteLater()
        self._apply_max_lines()

    def _apply_max_lines(self):
        self._fit_later()

    def _fit_later(self):
        QTimer.singleShot(0, self._fit_cards)

    def _fit_cards(self):
        """Показываем столько последних реплик, сколько помещается ЦЕЛИКОМ (не больше max_lines).

        Обрезанная наполовину старая реплика сверху режет глаз — лучше её не показывать вовсе.
        """
        vp = self.scroll.viewport()
        width = max(100, vp.width() - self.feed_lay.contentsMargins().right())
        avail = vp.height()
        sp = self.feed_lay.spacing()
        if self.draft.isVisible():
            avail -= self.draft.heightForWidth(width) + sp
        n_max = max(1, int(self.ui["max_lines"]))
        used, shown = 0, 0
        for cid in reversed(self.order):
            c = self.cards[cid]
            h = c.layout().totalHeightForWidth(width) + sp
            fits = shown == 0 or (shown < n_max and used + h <= avail)
            c.setVisible(fits)
            if fits:
                used += h
                shown += 1
            else:  # всё, что старше, тоже прячем
                for older in self.order[: self.order.index(cid)]:
                    self.cards[older].setVisible(False)
                break

    def on_partial(self, source: str, text: str):
        if text:
            self.partials[source] = text
        else:
            self.partials.pop(source, None)
        self._render_draft()

    def _render_draft(self):
        if not self.partials:
            if self.draft.isVisible():
                self.draft.hide()
                self._fit_later()
            return
        parts = []
        for source, txt in self.partials.items():
            who = "собеседник" if source.startswith("them") else "вы"
            parts.append(f"{who}: {txt}…")
        self.draft.setText("   ".join(parts))
        if not self.draft.isVisible():
            self.draft.show()
            self._fit_later()

    def clear(self):
        for c in self.cards.values():
            c.setParent(None)
            c.deleteLater()
        self.cards.clear()
        self.order.clear()
        self.partials.clear()
        self._render_draft()
        self.empty.show()
        self.on_suggestions([], "")

    # ---------------------------------------------------------------- варианты ответа
    def on_suggestions(self, items: list, question: str = ""):
        for sc in self.sugg_cards:
            sc.setParent(None)
            sc.deleteLater()
        self.sugg_cards = []
        self.sugg_q = question
        if not items:
            self.sugg_box.hide()
            return
        for i, it in enumerate(items, 1):
            sc = SuggCard(self, i, it)
            self.sugg_list.addWidget(sc)
            self.sugg_cards.append(sc)
        self._render_sugg_title()
        self.sugg_box.show()

    def _render_sugg_title(self):
        t = self.t
        if self.sugg_q:
            q = self.sugg_q if len(self.sugg_q) <= 90 else self.sugg_q[:87] + "…"
            self.sugg_title.setText(f'<span style="color:{t["hint"]};font-weight:600">Как ответить</span>'
                                    f'&nbsp;&nbsp;<span style="color:{t["muted"]}">на «{html.escape(q)}»</span>')
        else:
            self.sugg_title.setText(f'<span style="color:{t["hint"]};font-weight:600">Варианты ответа</span>')

    def on_suggestion_active(self, idx: int, said: bool):
        """Какой вариант вы сейчас читаете вслух (-1 — никакой)."""
        for j, sc in enumerate(self.sugg_cards):
            sc.set_state(active=(j == idx), dim=(idx >= 0 and j != idx), said=said)
