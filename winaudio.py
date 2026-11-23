"""Звук отдельного приложения (WASAPI process loopback) и список окон.

Работает на Windows 11 и Windows 10 сборки 20348+. Захватывается звук процесса
и всех его дочерних процессов (у Discord, Chrome, Teams звук идёт из дочернего).
"""
from __future__ import annotations

import ctypes
import functools
import os
import threading
import time
from ctypes import POINTER, byref, c_void_p, wintypes

import numpy as np

CALL_APPS = [
    "discord", "zoom", "ms-teams", "teams", "telegram", "whatsapp", "skype", "slack",
    "chrome", "msedge", "firefox", "opera", "browser", "brave", "vivaldi",
]


# ---------------------------------------------------------------- окна и процессы
def _exe_name(pid: int) -> str:
    import psutil
    try:
        return psutil.Process(pid).name()
    except Exception:
        return ""


def root_pid(pid: int) -> int:
    """Поднимаемся к верхнему процессу с тем же именем exe (главный процесс приложения)."""
    import psutil
    try:
        p = psutil.Process(pid)
        name = p.name().lower()
        while True:
            par = p.parent()
            if par is None or par.name().lower() != name:
                return p.pid
            p = par
    except Exception:
        return pid


def find_root_by_exe(exe: str) -> int | None:
    import psutil
    exe = exe.lower()
    roots = []
    for p in psutil.process_iter(["name", "ppid", "create_time"]):
        try:
            if (p.info["name"] or "").lower() != exe:
                continue
            par = psutil.Process(p.info["ppid"]) if p.info["ppid"] else None
            if par is None or par.name().lower() != exe:
                roots.append((p.info["create_time"] or 0, p.pid))
        except Exception:
            continue
    return sorted(roots)[0][1] if roots else None


def list_windows() -> list[dict]:
    """Видимые окна верхнего уровня: [{"title","exe","pid"}], приложения для звонков — первыми."""
    user32 = ctypes.windll.user32
    me = os.getpid()
    found: dict[int, dict] = {}
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def child_pid(hwnd, host_pid):
        res = []

        def cb(h, _):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(h, byref(pid))
            if pid.value != host_pid:
                res.append(pid.value)
                return False
            return True

        user32.EnumChildWindows(hwnd, WNDENUMPROC(cb), 0)
        return res[0] if res else host_pid

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, 4):  # 4 = GW_OWNER
            return True
        if user32.GetWindowLongW(hwnd, -20) & 0x80:  # WS_EX_TOOLWINDOW
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n == 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, byref(pid))
        p = pid.value
        exe = _exe_name(p)
        if exe.lower() == "applicationframehost.exe":  # UWP-приложение: настоящий процесс внутри
            p = child_pid(hwnd, p)
            exe = _exe_name(p)
        if not exe or p == me or exe.lower() in ("explorer.exe", "textinputhost.exe", "shellexperiencehost.exe"):
            return True
        r = root_pid(p)
        if r not in found:
            found[r] = {"title": buf.value, "exe": exe, "pid": r}
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)

    def rank(w):
        n = w["exe"].lower()
        for i, k in enumerate(CALL_APPS):
            if n.startswith(k):
                return (0, i, w["title"].lower())
        return (1, 0, w["title"].lower())

    return sorted(found.values(), key=rank)


# ---------------------------------------------------------------- COM-определения
@functools.lru_cache(maxsize=1)
def _com():
    import comtypes
    from comtypes import COMMETHOD, GUID, HRESULT, IUnknown, COMObject

    class WAVEFORMATEX(ctypes.Structure):
        _fields_ = [
            ("wFormatTag", wintypes.WORD), ("nChannels", wintypes.WORD),
            ("nSamplesPerSec", wintypes.DWORD), ("nAvgBytesPerSec", wintypes.DWORD),
            ("nBlockAlign", wintypes.WORD), ("wBitsPerSample", wintypes.WORD), ("cbSize", wintypes.WORD),
        ]

    REFTIME = ctypes.c_longlong

    class IAudioCaptureClient(IUnknown):
        _iid_ = GUID("{C8ADBD64-E71E-48a0-A4DE-185C395CD317}")
        _methods_ = [
            COMMETHOD([], HRESULT, "GetBuffer",
                      (["out"], POINTER(POINTER(ctypes.c_byte)), "ppData"),
                      (["out"], POINTER(wintypes.UINT), "pNumFramesToRead"),
                      (["out"], POINTER(wintypes.DWORD), "pdwFlags"),
                      (["out"], POINTER(ctypes.c_ulonglong), "pu64DevicePosition"),
                      (["out"], POINTER(ctypes.c_ulonglong), "pu64QPCPosition")),
            COMMETHOD([], HRESULT, "ReleaseBuffer", (["in"], wintypes.UINT, "NumFramesRead")),
            COMMETHOD([], HRESULT, "GetNextPacketSize", (["out"], POINTER(wintypes.UINT), "pNumFrames")),
        ]

    class IAudioClient(IUnknown):
        _iid_ = GUID("{1CB9AD4C-DBFA-4c32-B178-C2F568A703B2}")
        _methods_ = [
            COMMETHOD([], HRESULT, "Initialize",
                      (["in"], wintypes.DWORD, "ShareMode"), (["in"], wintypes.DWORD, "StreamFlags"),
                      (["in"], REFTIME, "hnsBufferDuration"), (["in"], REFTIME, "hnsPeriodicity"),
                      (["in"], POINTER(WAVEFORMATEX), "pFormat"), (["in"], POINTER(GUID), "AudioSessionGuid")),
            COMMETHOD([], HRESULT, "GetBufferSize", (["out"], POINTER(wintypes.UINT), "n")),
            COMMETHOD([], HRESULT, "GetStreamLatency", (["out"], POINTER(REFTIME), "lat")),
            COMMETHOD([], HRESULT, "GetCurrentPadding", (["out"], POINTER(wintypes.UINT), "pad")),
            COMMETHOD([], HRESULT, "IsFormatSupported", (["in"], wintypes.DWORD, "m"),
                      (["in"], POINTER(WAVEFORMATEX), "f"), (["out"], POINTER(POINTER(WAVEFORMATEX)), "c")),
            COMMETHOD([], HRESULT, "GetMixFormat", (["out"], POINTER(POINTER(WAVEFORMATEX)), "f")),
            COMMETHOD([], HRESULT, "GetDevicePeriod", (["out"], POINTER(REFTIME), "a"), (["out"], POINTER(REFTIME), "b")),
            COMMETHOD([], HRESULT, "Start"),
            COMMETHOD([], HRESULT, "Stop"),
            COMMETHOD([], HRESULT, "Reset"),
            COMMETHOD([], HRESULT, "SetEventHandle", (["in"], wintypes.HANDLE, "h")),
            COMMETHOD([], HRESULT, "GetService", (["in"], POINTER(GUID), "riid"), (["out"], POINTER(c_void_p), "ppv")),
        ]

    class IActivateAudioInterfaceAsyncOperation(IUnknown):
        _iid_ = GUID("{72A22D78-CDE4-431D-B8CC-843A71199B6D}")
        _methods_ = [
            COMMETHOD([], HRESULT, "GetActivateResult",
                      (["out"], POINTER(ctypes.c_long), "hr"), (["out"], POINTER(POINTER(IUnknown)), "itf")),
        ]

    class IActivateAudioInterfaceCompletionHandler(IUnknown):
        _iid_ = GUID("{41D949AB-9862-444A-80F6-C261334DA5EB}")
        _methods_ = [
            COMMETHOD([], HRESULT, "ActivateCompleted",
                      (["in"], POINTER(IActivateAudioInterfaceAsyncOperation), "op")),
        ]

    class IAgileObject(IUnknown):  # обязателен: иначе Windows вернёт E_ILLEGAL_METHOD_CALL
        _iid_ = GUID("{94ea2b94-e9cc-49e0-c0ff-ee64ca8f5b90}")
        _methods_ = []

    class Handler(COMObject):
        _com_interfaces_ = [IActivateAudioInterfaceCompletionHandler, IAgileObject]

        def __init__(self):
            super().__init__()
            self.done = threading.Event()

        def ActivateCompleted(self, op):
            self.done.set()

    class BLOB(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.ULONG), ("pBlobData", c_void_p)]

    class PROPVARIANT(ctypes.Structure):
        _fields_ = [("vt", wintypes.USHORT), ("r1", wintypes.WORD), ("r2", wintypes.WORD),
                    ("r3", wintypes.WORD), ("blob", BLOB)]

    class LOOPBACK_PARAMS(ctypes.Structure):
        _fields_ = [("TargetProcessId", wintypes.DWORD), ("ProcessLoopbackMode", ctypes.c_int)]

    class ACTIVATION_PARAMS(ctypes.Structure):
        _fields_ = [("ActivationType", ctypes.c_int), ("Loopback", LOOPBACK_PARAMS)]

    act = ctypes.WinDLL("Mmdevapi.dll").ActivateAudioInterfaceAsync
    act.argtypes = [wintypes.LPCWSTR, POINTER(GUID), POINTER(PROPVARIANT),
                    POINTER(IActivateAudioInterfaceCompletionHandler),
                    POINTER(POINTER(IActivateAudioInterfaceAsyncOperation))]
    act.restype = HRESULT
    return locals()


def init_com_mta():
    """COM в режиме MTA для текущего потока.

    comtypes при первом импорте сам инициализирует импортирующий поток как STA, поэтому
    импортируем его заранее в главном потоке (см. main.py), а здесь терпим уже выбранный режим.
    """
    import comtypes
    try:
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
    except OSError as e:
        if (getattr(e, "winerror", None) or 0) & 0xFFFFFFFF != 0x80010106:  # RPC_E_CHANGED_MODE
            raise


class AppLoopbackCapture:
    """Захват звука приложения по имени exe → on_audio(16 кГц моно float32).

    Если приложение перезапустится — переподключается само.
    on_fail(err) вызывается, если захват по окну не поддерживается системой.
    """

    RATE = 48000

    def __init__(self, exe: str, on_audio, status, on_fail):
        self.exe = exe
        self.on_audio = on_audio
        self.status = status
        self.on_fail = on_fail
        self.running = False
        self.thread = None

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False

    def _run(self):
        try:
            init_com_mta()
        except Exception as e:
            self.on_fail(e)
            return
        waiting_shown = False
        while self.running:
            pid = find_root_by_exe(self.exe)
            if not pid:
                if not waiting_shown:
                    self.status(f"{self.exe} не запущен — жду…")
                    waiting_shown = True
                time.sleep(2)
                continue
            waiting_shown = False
            try:
                self._capture(pid)
            except Exception as e:
                if self.running:
                    self.on_fail(e)
                return

    def _capture(self, pid: int):
        import psutil
        import soxr
        from comtypes import GUID

        c = _com()
        params = c["ACTIVATION_PARAMS"](1, c["LOOPBACK_PARAMS"](pid, 0))  # 1=PROCESS_LOOPBACK, 0=включая дочерние
        pv = c["PROPVARIANT"]()
        pv.vt = 65  # VT_BLOB
        pv.blob.cbSize = ctypes.sizeof(params)
        pv.blob.pBlobData = ctypes.cast(byref(params), c_void_p)

        handler = c["Handler"]()
        hptr = handler.QueryInterface(c["IActivateAudioInterfaceCompletionHandler"])
        op = POINTER(c["IActivateAudioInterfaceAsyncOperation"])()
        iid = c["IAudioClient"]._iid_
        c["act"]("VAD\\Process_Loopback", byref(iid), byref(pv), hptr, byref(op))
        if not handler.done.wait(5):
            raise RuntimeError("система не ответила на запрос захвата")
        hr, unk = op.GetActivateResult()
        if hr != 0:
            raise OSError(f"ActivateAudioInterfaceAsync: 0x{hr & 0xFFFFFFFF:08X}")
        client = unk.QueryInterface(c["IAudioClient"])

        wfx = c["WAVEFORMATEX"](1, 2, self.RATE, self.RATE * 4, 4, 16, 0)  # PCM 16 бит стерео
        LOOPBACK, EVENTCB, AUTOCONVERT = 0x00020000, 0x00040000, 0x80000000
        client.Initialize(0, LOOPBACK | EVENTCB | AUTOCONVERT, 200000, 0, byref(wfx), None)
        k32 = ctypes.windll.kernel32
        k32.CreateEventW.restype = wintypes.HANDLE
        evt = k32.CreateEventW(None, False, False, None)
        client.SetEventHandle(evt)
        cap_ptr = client.GetService(byref(c["IAudioCaptureClient"]._iid_))
        cap = ctypes.cast(c_void_p(cap_ptr), POINTER(c["IAudioCaptureClient"]))
        cap.AddRef()  # comtypes сделает Release при сборке мусора
        rs = soxr.ResampleStream(self.RATE, 16000, 1, dtype="float32")
        client.Start()
        self.status(f"Слушаю {self.exe}")
        last_check = time.monotonic()
        try:
            while self.running:
                k32.WaitForSingleObject(evt, 100)
                n = cap.GetNextPacketSize()
                while n:
                    data, frames, flags, _, _ = cap.GetBuffer()
                    if flags & 0x2 or not frames:  # AUDCLNT_BUFFERFLAGS_SILENT
                        mono = np.zeros(frames, np.float32)
                    else:
                        raw = np.ctypeslib.as_array(ctypes.cast(data, POINTER(ctypes.c_int16)), shape=(frames * 2,))
                        mono = raw.reshape(-1, 2).astype(np.float32).mean(axis=1) / 32768.0
                    cap.ReleaseBuffer(frames)
                    out = rs.resample_chunk(mono)
                    if out.size:
                        self.on_audio(out)
                    n = cap.GetNextPacketSize()
                if time.monotonic() - last_check > 2:
                    last_check = time.monotonic()
                    if not psutil.pid_exists(pid):
                        self.status(f"{self.exe} закрыт — жду перезапуска…")
                        return
        finally:
            try:
                client.Stop()
            except Exception:
                pass
            k32.CloseHandle(evt)
