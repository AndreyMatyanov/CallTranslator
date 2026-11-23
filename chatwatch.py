"""Слежение за текстовым чатом: захват области окна → распознавание текста (OCR) → новые сообщения.

- Область привязана к окну приложения (Discord, Telegram…): если окно сдвинуть, область поедет следом.
- Окно снимается через PrintWindow — работает, даже если чат перекрыт другим окном (но не свёрнут).
- OCR: встроенное распознавание Windows (английский), иначе RapidOCR с английской моделью.
- Новые сообщения: сравнение с уже виденными строками, подтверждение двумя кадрами подряд,
  защита от прокрутки, фильтр служебных строк (время, «печатает…»), пропуск ваших сообщений.
"""
from __future__ import annotations

import ctypes
import difflib
import os
import re
import sys
import threading
import time
import urllib.request
from collections import deque
from ctypes import byref, wintypes

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
OCR_DIR = os.path.join(HERE, "models", "ocr")
EN_REC_URL = "https://huggingface.co/SWHL/RapidOCR/resolve/main/PP-OCRv3/en_PP-OCRv3_rec_infer.onnx"
EN_DICT_URL = "https://raw.githubusercontent.com/PaddlePaddle/PaddleOCR/release/2.7/ppocr/utils/en_dict.txt"


# ================================================================ окна (Win32)
def _u32():
    return ctypes.windll.user32


def window_rect(hwnd) -> tuple[int, int, int, int] | None:
    r = wintypes.RECT()
    if not _u32().GetWindowRect(hwnd, byref(r)):
        return None
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def window_exe(hwnd) -> str:
    import psutil
    pid = wintypes.DWORD()
    _u32().GetWindowThreadProcessId(hwnd, byref(pid))
    try:
        return psutil.Process(pid.value).name()
    except Exception:
        return ""


def window_title(hwnd) -> str:
    n = _u32().GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    _u32().GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def window_at(x: int, y: int):
    """Окно верхнего уровня в физической точке экрана."""
    u = _u32()
    u.WindowFromPoint.restype = wintypes.HWND
    u.WindowFromPoint.argtypes = [wintypes.POINT]
    u.GetAncestor.restype = wintypes.HWND
    h = u.WindowFromPoint(wintypes.POINT(x, y))
    return u.GetAncestor(h, 2) if h else None  # GA_ROOT


def find_window(exe: str, prefer_hwnd=None):
    """Главное видимое окно приложения: сохранённое, если ещё живо, иначе самое большое."""
    u = _u32()
    if prefer_hwnd and u.IsWindow(prefer_hwnd) and window_exe(prefer_hwnd).lower() == exe.lower():
        return prefer_hwnd
    best, best_area = None, 0
    PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(h, _):
        nonlocal best, best_area
        if u.IsWindowVisible(h) and not u.GetWindow(h, 4) and window_exe(h).lower() == exe.lower():
            r = window_rect(h)
            if r and r[2] * r[3] > best_area:
                best, best_area = h, r[2] * r[3]
        return True

    u.EnumWindows(PROC(cb), 0)
    return best


# ================================================================ захват картинки (GDI)
class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


def _grab(src_dc_fn, w: int, h: int, draw) -> np.ndarray | None:
    g, u = ctypes.windll.gdi32, _u32()
    for fn, res in ((g.CreateCompatibleDC, wintypes.HDC), (g.CreateCompatibleBitmap, wintypes.HBITMAP),
                    (g.SelectObject, wintypes.HGDIOBJ), (u.GetDC, wintypes.HDC), (u.GetWindowDC, wintypes.HDC)):
        fn.restype = res
    src, hwnd_for_release = src_dc_fn()
    if not src:
        return None
    mem = g.CreateCompatibleDC(src)
    bmp = g.CreateCompatibleBitmap(src, w, h)
    old = g.SelectObject(mem, bmp)
    try:
        if not draw(mem, src):
            return None
        bi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        buf = (ctypes.c_ubyte * (w * h * 4))()
        if not g.GetDIBits(mem, bmp, 0, h, buf, byref(bi), 0):
            return None
        return np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4).copy()  # BGRA
    finally:
        g.SelectObject(mem, old)
        g.DeleteObject(bmp)
        g.DeleteDC(mem)
        u.ReleaseDC(hwnd_for_release, src)


def capture_window(hwnd) -> np.ndarray | None:
    """Снимок всего окна, даже если оно перекрыто (PW_RENDERFULLCONTENT — для Chromium/Electron)."""
    r = window_rect(hwnd)
    if not r or r[2] <= 0 or r[3] <= 0:
        return None
    u = _u32()
    return _grab(lambda: (u.GetWindowDC(hwnd), hwnd), r[2], r[3],
                 lambda mem, src: bool(u.PrintWindow(hwnd, mem, 2)))


def capture_screen(x: int, y: int, w: int, h: int) -> np.ndarray | None:
    """Снимок участка экрана (оверлей в него не попадает — он исключён из захвата)."""
    g, u = ctypes.windll.gdi32, _u32()
    return _grab(lambda: (u.GetDC(None), None), w, h,
                 lambda mem, src: bool(g.BitBlt(mem, 0, 0, w, h, src, x, y, 0x00CC0020)))  # SRCCOPY


# ================================================================ распознавание текста
class OcrEngine:
    """read(BGRA) -> [(текст, y_верх, y_низ)] сверху вниз."""

    def __init__(self, status):
        self.status = status
        self.kind = ""
        self._win = None
        self._rapid = None

    def load(self, prefer: str = "auto"):
        if prefer in ("auto", "windows") and sys.platform == "win32":
            try:
                self._load_windows()
                self.kind = "windows"
                return
            except Exception as e:
                if prefer == "windows":
                    raise
                print("Windows OCR недоступен:", e)
        self._load_rapid()
        self.kind = "rapid"

    def _load_windows(self):
        from winrt.windows.globalization import Language
        from winrt.windows.media.ocr import OcrEngine as WinOcr
        lang = Language("en-US")
        if not WinOcr.is_language_supported(lang):
            raise RuntimeError("в Windows не установлен английский язык распознавания")
        self._win = WinOcr.try_create_from_language(lang)
        if self._win is None:
            raise RuntimeError("не удалось создать распознаватель")

    def _load_rapid(self):
        os.makedirs(OCR_DIR, exist_ok=True)
        rec = os.path.join(OCR_DIR, "en_PP-OCRv3_rec_infer.onnx")
        keys = os.path.join(OCR_DIR, "en_dict.txt")
        for url, path in ((EN_REC_URL, rec), (EN_DICT_URL, keys)):
            if not os.path.exists(path) or os.path.getsize(path) < 100:
                self.status("Скачиваю модель распознавания текста (~9 МБ, один раз)…")
                tmp = path + ".part"
                urllib.request.urlretrieve(url, tmp)
                os.replace(tmp, path)
        from rapidocr_onnxruntime import RapidOCR
        self._rapid = RapidOCR(rec_model_path=rec, rec_keys_path=keys, use_angle_cls=False, use_cls=False)

    @staticmethod
    def _upscale(img: np.ndarray) -> tuple[np.ndarray, float]:
        """Мелкий шрифт чата (13–15 px) распознаётся заметно лучше после увеличения в 2 раза."""
        if img.shape[0] > 1400:
            return img, 1.0
        try:
            import cv2
            return cv2.resize(img, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC), 2.0
        except Exception:
            return np.repeat(np.repeat(img, 2, axis=0), 2, axis=1), 2.0

    def read(self, bgra: np.ndarray) -> list[tuple[str, float, float]]:
        img, k = self._upscale(bgra)
        if self.kind == "windows":
            return [(t, y1 / k, y2 / k) for t, y1, y2 in self._read_windows(img)]
        bgr = np.ascontiguousarray(img[:, :, :3])
        res, _ = self._rapid(bgr)
        out = []
        for box, text, _score in res or []:
            ys = [p[1] for p in box]
            out.append((str(text), min(ys) / k, max(ys) / k))
        out.sort(key=lambda r: r[1])
        return out

    def _read_windows(self, img: np.ndarray):
        import asyncio
        from winrt.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap
        h, w = img.shape[:2]
        data = np.ascontiguousarray(img).tobytes()
        try:  # pywinrt 3+: принимает байты напрямую
            sb = SoftwareBitmap.create_copy_from_buffer(data, BitmapPixelFormat.BGRA8, w, h,
                                                        BitmapAlphaMode.PREMULTIPLIED)
        except TypeError:
            from winrt.windows.storage.streams import DataWriter
            dw = DataWriter()
            dw.write_bytes(data)
            sb = SoftwareBitmap.create_copy_from_buffer(dw.detach_buffer(), BitmapPixelFormat.BGRA8, w, h,
                                                        BitmapAlphaMode.PREMULTIPLIED)

        async def run():
            return await self._win.recognize_async(sb)

        res = asyncio.run(run())
        out = []
        for line in res.lines:
            rects = [wd.bounding_rect for wd in line.words]
            if not rects:
                continue
            out.append((line.text, min(r.y for r in rects), max(r.y + r.height for r in rects)))
        out.sort(key=lambda r: r[1])
        return out


# ================================================================ новые сообщения
TIME_RE = re.compile(
    r"((today|yesterday|сегодня|вчера)\s*(at|в)?\s*)?\b\d{1,2}[:.]\d{2}\s*([ap]\.?\s?m\.?)?\b|"
    r"\b\d{1,2}/\d{1,2}/\d{2,4}\b", re.I)
JUNK_RE = re.compile(r"(is typing|are typing|several people are typing|печатает|набирает сообщение|"
                     r"^\(?edited\)?$|^new messages?$|^новые сообщения$|^jump to present|^message @|"
                     r"^написать сообщение|^write a message|^type a message)", re.I)
CYR_RE = re.compile(r"[а-яё]", re.I)
LAT_RE = re.compile(r"[a-z]", re.I)


def _key(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())


class ChatWatcher(threading.Thread):
    """Следит за областью окна и сообщает о новых английских сообщениях: on_message(text)."""

    def __init__(self, region: dict, on_message, status, state, is_own=None, interval: float = 1.5,
                 ocr_pref: str = "auto"):
        super().__init__(daemon=True)
        self.region = region
        self.on_message = on_message
        self.status = status
        self.state = state  # state("ok" | "wait" | "off") — точка в шапке
        self.is_own = is_own or (lambda t: False)
        self.interval = interval
        self.ocr_pref = ocr_pref
        self.running = True
        self.hwnd = region.get("hwnd")
        self.seen: deque = deque(maxlen=400)
        self.pending: dict[str, int] = {}
        self.baseline_done = False
        self.prev_thumb = None

    def stop(self):
        self.running = False
        self.state("off")

    # ------------------------------------------------------------ основной цикл
    def run(self):
        exe = self.region.get("exe", "")
        try:
            self.ocr = OcrEngine(self.status)
            self.ocr.load(self.ocr_pref)
        except Exception as e:
            self.status(f"⚠ Распознавание текста чата не запустилось: {e}")
            self.state("off")
            return
        engine = "встроенное распознавание Windows" if self.ocr.kind == "windows" else "RapidOCR"
        self.status(f"Слежу за чатом {exe} ({engine})")
        last_problem = ""
        while self.running:
            t0 = time.monotonic()
            problem = ""
            try:
                problem = self._tick(exe)
            except Exception as e:
                problem = f"ошибка чтения чата: {e}"
            if problem != last_problem:
                last_problem = problem
                if problem:
                    self.status(f"Чат: {problem}")
                self.state("wait" if problem else "ok")
            time.sleep(max(0.2, self.interval - (time.monotonic() - t0)))

    def _crop(self) -> tuple[np.ndarray | None, str]:
        exe = self.region["exe"]
        self.hwnd = find_window(exe, self.hwnd)
        if not self.hwnd:
            return None, f"окно {exe} не найдено — откройте его"
        if _u32().IsIconic(self.hwnd):
            return None, f"окно {exe} свёрнуто"
        wr = window_rect(self.hwnd)
        dx, dy, w, h = (int(self.region[k]) for k in ("dx", "dy", "w", "h"))
        x0, y0 = max(0, dx), max(0, dy)
        x1, y1 = min(wr[2], dx + w), min(wr[3], dy + h)
        if x1 - x0 < 20 or y1 - y0 < 20:
            return None, "область чата вне окна — выберите её заново (Ctrl+Alt+R)"
        img = capture_window(self.hwnd)
        crop = img[y0:y1, x0:x1] if img is not None else None
        if crop is None or (crop[:, :, :3].max() < 12):  # окно не отдало картинку — снимем экран
            crop = capture_screen(wr[0] + x0, wr[1] + y0, x1 - x0, y1 - y0)
        if crop is None:
            return None, "не удалось снять окно"
        return crop, ""

    def _tick(self, exe: str) -> str:
        crop, problem = self._crop()
        if crop is None:
            return problem
        thumb = crop[::6, ::6, 1].astype(np.int16)
        if self.prev_thumb is not None and self.prev_thumb.shape == thumb.shape \
                and np.abs(thumb - self.prev_thumb).mean() < 0.6 and not self.pending:
            return ""  # в чате ничего не изменилось — не тратим время на распознавание
        self.prev_thumb = thumb
        lines = self.ocr.read(crop)
        self._process(lines)
        return ""

    # ------------------------------------------------------------ отбор новых строк
    @staticmethod
    def _clean(text: str) -> tuple[str, bool]:
        """Убираем время/дату; второй результат — была ли в строке метка времени (значит, это шапка)."""
        had_time = bool(TIME_RE.search(text))
        t = TIME_RE.sub(" ", text)
        return re.sub(r"\s+", " ", t).strip(" -—·•|"), had_time

    def _is_seen(self, key: str) -> bool:
        if key in self.seen:
            return True
        for s in list(self.seen)[-150:]:
            if abs(len(s) - len(key)) <= max(4, len(key) // 5) and difflib.SequenceMatcher(None, s, key).ratio() >= 0.86:
                return True
        return False

    def _process(self, lines: list[tuple[str, float, float]]):
        cands = []  # (текст, y1, y2, key)
        for text, y1, y2 in lines:
            t, had_time = self._clean(text)
            if not t or JUNK_RE.search(t):
                continue
            n_lat, n_cyr = len(LAT_RE.findall(t)), len(CYR_RE.findall(t))
            if n_lat < 2 or n_cyr > n_lat * 0.3:  # не английский (например, ваши сообщения по-русски)
                continue
            if had_time and len(t.split()) <= 3:  # «Имя  Today at 4:37 PM» — шапка сообщения
                continue
            key = _key(t)
            if len(key) < 2:
                continue
            cands.append((t, y1, y2, key))

        if not self.baseline_done:  # при запуске всё, что уже есть в чате, считаем прочитанным
            for c in cands:
                self.seen.append(c[3])
            self.baseline_done = True
            return

        fresh = [c for c in cands if not self._is_seen(c[3])]
        confirmed = []
        new_pending = {}
        for c in fresh:
            cnt = self.pending.get(c[3], 0) + 1
            if cnt >= 2:  # тот же текст два кадра подряд — сообщение дописано и стабильно
                confirmed.append(c)
            else:
                new_pending[c[3]] = cnt
        self.pending = new_pending
        if not confirmed:
            return
        for c in confirmed:
            self.seen.append(c[3])
        if len(confirmed) > 8:  # резко появилось много строк — это прокрутка, а не новые сообщения
            self.status("Чат прокручен — старые сообщения пропущены")
            return

        # соседние строки (перенос длинного сообщения) склеиваем в одно сообщение
        groups, cur = [], []
        for c in confirmed:
            if cur:
                prev = cur[-1]
                line_h = max(8.0, prev[2] - prev[1])
                if c[1] - prev[2] > line_h * 1.3:  # шапка другого автора даёт разрыв больше
                    groups.append(cur)
                    cur = []
            cur.append(c)
        if cur:
            groups.append(cur)
        for g in groups:
            msg = " ".join(c[0] for c in g).strip()
            if msg and not self.is_own(msg):
                self.on_message(msg)


# ================================================================ выбор области (Qt)
def pick_region(on_done):
    """Затемняет все экраны; пользователь обводит рамкой окно чата. on_done(region | None, сообщение)."""
    from PyQt6.QtCore import QPoint, QRect, Qt
    from PyQt6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPen
    from PyQt6.QtWidgets import QWidget

    selectors = []

    def finish(region, msg):
        for s in selectors:
            s.close()
        on_done(region, msg)

    class Selector(QWidget):
        def __init__(self, screen):
            super().__init__()
            self.scr = screen
            self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                                | Qt.WindowType.Tool)
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            self.setCursor(Qt.CursorShape.CrossCursor)
            self.setGeometry(screen.geometry())
            self.a = self.b = None

        def paintEvent(self, e):
            p = QPainter(self)
            p.fillRect(self.rect(), QColor(0, 0, 0, 110))
            f = QFont()
            f.setPixelSize(18)
            p.setFont(f)
            p.setPen(QColor(255, 255, 255))
            p.drawText(self.rect().adjusted(0, 40, 0, 0), Qt.AlignmentFlag.AlignHCenter,
                       "Обведите рамкой область с сообщениями чата (Discord, Telegram, Zoom…)\n"
                       "Esc — отмена")
            if self.a and self.b:
                r = QRect(self.a, self.b).normalized()
                p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
                p.fillRect(r, Qt.GlobalColor.transparent)
                p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
                p.setPen(QPen(QColor(122, 162, 247), 2))
                p.drawRect(r)

        def keyPressEvent(self, e):
            if e.key() == Qt.Key.Key_Escape:
                finish(None, "Выбор области отменён")

        def mousePressEvent(self, e):
            self.a = self.b = e.position().toPoint()
            self.update()

        def mouseMoveEvent(self, e):
            if self.a:
                self.b = e.position().toPoint()
                self.update()

        def mouseReleaseEvent(self, e):
            if not self.a:
                return
            r = QRect(self.a, e.position().toPoint()).normalized()
            if r.width() < 40 or r.height() < 40:
                self.a = self.b = None
                self.update()
                return
            # логические координаты Qt → физические пиксели Windows (с учётом масштаба экрана)
            dpr = self.scr.devicePixelRatio()
            o = self.scr.geometry().topLeft()
            g = r.translated(self.geometry().topLeft() - o)
            px = int(o.x() + g.x() * dpr)
            py = int(o.y() + g.y() * dpr)
            pw, ph = int(g.width() * dpr), int(g.height() * dpr)
            for s in selectors:
                s.hide()
            QGuiApplication.processEvents()
            time.sleep(0.15)  # чтобы наша затемняющая заставка точно исчезла
            hwnd = window_at(px + pw // 2, py + ph // 2)
            if not hwnd or window_exe(hwnd).lower() in ("python.exe", "pythonw.exe", "explorer.exe", ""):
                finish(None, "⚠ Под рамкой не найдено окно приложения — откройте чат и попробуйте ещё раз")
                return
            wr = window_rect(hwnd)
            region = {"exe": window_exe(hwnd), "title": window_title(hwnd), "hwnd": int(hwnd),
                      "dx": px - wr[0], "dy": py - wr[1], "w": pw, "h": ph}
            finish(region, "")

    for scr in QGuiApplication.screens():
        s = Selector(scr)
        selectors.append(s)
        s.show()
        s.raise_()
        s.activateWindow()
