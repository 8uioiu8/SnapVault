"""Win32 绑定：全局热键、窗口枚举、PrintWindow、DPI、光标与剪贴板辅助.

全部通过 ``ctypes`` 直接调用系统 API，不依赖 pywin32 / keyboard 等第三方包，
只保证 Windows 平台可用（其余平台导入时不会报错，只是功能不可用）。
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    try:
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    except OSError:  # pragma: no cover
        shell32 = None
    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
    except OSError:  # pragma: no cover - 老系统
        shcore = None
    try:
        dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
    except OSError:  # pragma: no cover
        dwmapi = None
else:  # pragma: no cover
    user32 = gdi32 = kernel32 = shcore = dwmapi = shell32 = None


# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
WM_DESTROY = 0x0002
WM_APP = 0x8000

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
WS_EX_APPWINDOW = 0x00040000

DWMWA_CLOAKED = 14
DWMWA_EXTENDED_FRAME_BOUNDS = 9

PW_RENDERFULLCONTENT = 0x00000002

SPI_GETWORKAREA = 0x0030
MONITOR_DEFAULTTONEAREST = 0x00000002

_SRCCOPY = 0x00CC0020
_DIB_RGB_COLORS = 0

VK_CODES: dict[str, int] = {
    "BACKSPACE": 0x08, "TAB": 0x09, "CLEAR": 0x0C, "ENTER": 0x0D, "RETURN": 0x0D,
    "SHIFT": 0x10, "CTRL": 0x11, "CONTROL": 0x11, "ALT": 0x12, "MENU": 0x12,
    "PAUSE": 0x13, "CAPSLOCK": 0x14, "ESC": 0x1B, "ESCAPE": 0x1B, "SPACE": 0x20,
    "PAGEUP": 0x21, "PRIOR": 0x21, "PAGEDOWN": 0x22, "NEXT": 0x22, "END": 0x23,
    "HOME": 0x24, "LEFT": 0x25, "UP": 0x26, "RIGHT": 0x27, "DOWN": 0x28,
    "SELECT": 0x29, "PRINT": 0x2A, "PRTSC": 0x2C, "PRINTSCREEN": 0x2C, "SNAPSHOT": 0x2C,
    "INSERT": 0x2D, "INS": 0x2D, "DELETE": 0x2E, "DEL": 0x2E, "HELP": 0x2F,
    "NUMPAD0": 0x60, "NUMPAD1": 0x61, "NUMPAD2": 0x62, "NUMPAD3": 0x63,
    "NUMPAD4": 0x64, "NUMPAD5": 0x65, "NUMPAD6": 0x66, "NUMPAD7": 0x67,
    "NUMPAD8": 0x68, "NUMPAD9": 0x69, "MULTIPLY": 0x6A, "ADD": 0x6B,
    "SUBTRACT": 0x6D, "DECIMAL": 0x6E, "DIVIDE": 0x6F,
    **{f"F{i}": 0x6F + i for i in range(1, 25)},
    "NUMLOCK": 0x90, "SCROLLLOCK": 0x91,
    ";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF, "`": 0xC0,
    "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE,
    **{chr(c): c for c in range(0x30, 0x3A)},              # 0-9
    **{chr(c): c for c in range(0x41, 0x5B)},              # A-Z
}
for _i in range(10):
    VK_CODES.setdefault(str(_i), 0x30 + _i)


# --------------------------------------------------------------------------- #
# 结构体
# --------------------------------------------------------------------------- #

if IS_WINDOWS:
    class RECT(ctypes.Structure):
        _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                    ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

        @property
        def width(self) -> int:
            return self.right - self.left

        @property
        def height(self) -> int:
            return self.bottom - self.top

        def as_tuple(self) -> tuple[int, int, int, int]:
            return (self.left, self.top, self.right, self.bottom)

    class POINT(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

    class BITMAPINFOHEADER(ctypes.Structure):
        # 注意：ctypes 默认对齐会在 biBitCount(WORD) 之后补 2 字节，整个结构体
        # sizeof() == 44，而 Windows 的 BITMAPINFOHEADER 是 40 字节。多出来的
        # 尾部填充不影响 GetDIBits（它按字段读，且 BITMAPINFO 总长 44 仍够用），
        # 但写 CF_DIB 时必须只取前 40 字节 —— 见 _dib_header_bytes()。
        _fields_ = [
            ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
            ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
            ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]

    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
    user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    user32.PrintWindow.restype = wintypes.BOOL
    user32.WindowFromPoint.argtypes = [POINT]
    user32.WindowFromPoint.restype = wintypes.HWND
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
    user32.RegisterHotKey.restype = wintypes.BOOL
    user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]

    # 剪贴板 / 全局内存：句柄是 64 位指针，必须显式声明，否则会被截断成
    # 32 位 int（实测 GlobalLock 直接返回 NULL，剪贴板读回全部失败）。
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.restype = ctypes.c_void_p

    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    user32.IsClipboardFormatAvailable.restype = wintypes.BOOL

GW_OWNER = 4
GA_ROOT = 2


# --------------------------------------------------------------------------- #
# DPI
# --------------------------------------------------------------------------- #

def set_dpi_awareness() -> str:
    """开启 Per-Monitor-V2 DPI 感知，返回实际生效的模式名。

    截图的坐标正确性完全依赖于此：非感知进程会被系统 DPI 虚拟化，
    拿到的是缩放后的假坐标，导致多显示器 + 高 DPI 下选区错位。
    """
    if not IS_WINDOWS:
        return "n/a"
    try:
        ctx = ctypes.c_void_p(-4)  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        if user32.SetProcessDpiAwarenessContext(ctx):
            return "per-monitor-v2"
    except Exception:
        pass
    try:
        if shcore and shcore.SetProcessDpiAwareness(2) == 0:  # PROCESS_PER_MONITOR_DPI_AWARE
            return "per-monitor"
    except Exception:
        pass
    try:
        if user32.SetProcessDPIAware():
            return "system"
    except Exception:
        pass
    return "unaware"


def scale_for_window(hwnd: int | None) -> float:
    """窗口所在显示器的 DPI 缩放（1.0 / 1.25 / 1.5 ...）。"""
    if not IS_WINDOWS or not hwnd:
        return 1.0
    try:
        return user32.GetDpiForWindow(wintypes.HWND(hwnd)) / 96.0
    except Exception:
        return 1.0


def scale_for_point(x: int, y: int) -> float:
    if not IS_WINDOWS:
        return 1.0
    try:
        mon = user32.MonitorFromPoint(POINT(x, y), MONITOR_DEFAULTTONEAREST)
        dpi_x = wintypes.UINT()
        dpi_y = wintypes.UINT()
        if shcore.GetDpiForMonitor(wintypes.HMONITOR(mon), 0, ctypes.byref(dpi_x),
                                   ctypes.byref(dpi_y)) == 0:
            return dpi_x.value / 96.0
    except Exception:
        pass
    return 1.0


# --------------------------------------------------------------------------- #
# 窗口信息
# --------------------------------------------------------------------------- #

@dataclass
class WindowInfo:
    hwnd: int
    title: str
    exe: str
    rect: tuple[int, int, int, int]      # 物理像素 LTRB
    frame: tuple[int, int, int, int]     # 含阴影/边框的扩展矩形
    z: int = 0
    process_id: int = 0
    class_name: str = ""
    monitor: str = ""

    @property
    def width(self) -> int:
        return self.rect[2] - self.rect[0]

    @property
    def height(self) -> int:
        return self.rect[3] - self.rect[1]

    @property
    def size(self) -> tuple[int, int]:
        return self.width, self.height

    @property
    def center(self) -> tuple[int, int]:
        return (self.rect[0] + self.width // 2, self.rect[1] + self.height // 2)


def _pid_of(hwnd: int) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    return int(pid.value)


def exe_for_pid(pid: int) -> str:
    """进程名（含扩展名）。优先 QueryFullProcessImageNameW，失败退回 psutil。"""
    if pid <= 0:
        return ""
    if IS_WINDOWS:
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if h:
            try:
                buf = ctypes.create_unicode_buffer(1024)
                size = wintypes.DWORD(len(buf))
                if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                    return os.path.basename(buf.value)
            except Exception:
                pass
            finally:
                kernel32.CloseHandle(h)
    try:  # 兜底
        import psutil  # type: ignore

        return psutil.Process(pid).name()
    except Exception:
        return ""


def window_title(hwnd: int) -> str:
    if not IS_WINDOWS:
        return ""
    length = user32.GetWindowTextLengthW(wintypes.HWND(hwnd))
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 2)
    user32.GetWindowTextW(wintypes.HWND(hwnd), buf, length + 2)
    return buf.value


def class_name(hwnd: int) -> str:
    if not IS_WINDOWS:
        return ""
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(wintypes.HWND(hwnd), buf, 256)
    return buf.value


def _is_cloaked(hwnd: int) -> bool:
    if not dwmapi:
        return False
    try:
        val = ctypes.c_int(0)
        dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd), DWMWA_CLOAKED, ctypes.byref(val),
                                     ctypes.sizeof(val))
        return bool(val.value)
    except Exception:
        return False


def window_rect(hwnd: int, extended: bool = False) -> tuple[int, int, int, int]:
    if not IS_WINDOWS or not hwnd:
        return (0, 0, 0, 0)
    r = RECT()
    ok = False
    if extended and dwmapi:
        try:
            ok = dwmapi.DwmGetWindowAttribute(
                wintypes.HWND(hwnd), DWMWA_EXTENDED_FRAME_BOUNDS,
                ctypes.byref(r), ctypes.sizeof(r)) == 0
        except Exception:
            ok = False
    if not ok:
        if not user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(r)):
            return (0, 0, 0, 0)
    return r.as_tuple()


def is_alt_tab_window(hwnd: int) -> bool:
    """是否为"用户可感知的顶层窗口"（排除工具窗口 / 无标题 / 隐藏窗口）。"""
    if not IS_WINDOWS or not hwnd:
        return False
    if not user32.IsWindowVisible(wintypes.HWND(hwnd)):
        return False
    if user32.GetWindow(wintypes.HWND(hwnd), GW_OWNER):
        return False
    if user32.GetWindowTextLengthW(wintypes.HWND(hwnd)) == 0:
        return False
    ex = user32.GetWindowLongW(wintypes.HWND(hwnd), GWL_EXSTYLE)
    if ex & WS_EX_TOOLWINDOW:
        return False
    if _is_cloaked(hwnd):
        return False
    rect = window_rect(hwnd)
    if rect[2] - rect[0] < 40 or rect[3] - rect[1] < 30:
        return False
    if rect[2] <= -30000 or rect[0] >= 30000:  # 最小化后的离屏坐标
        return False
    return True


def list_windows(include_self_pid: bool | None = None, limit: int = 400) -> list[WindowInfo]:
    """枚举 Z 序顶层窗口（索引 0 为最前），供窗口列表与高亮使用。"""
    if not IS_WINDOWS:
        return []
    my_pid = os.getpid()
    out: list[WindowInfo] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _enum(h, _lparam):
        if len(out) >= limit:
            return False
        if h and is_alt_tab_window(h):
            pid = _pid_of(h)
            if include_self_pid is False and pid == my_pid:
                return True
            rect = window_rect(h)
            out.append(WindowInfo(
                hwnd=int(h), title=window_title(h), exe=exe_for_pid(pid),
                rect=rect, frame=window_rect(h, extended=True), z=len(out),
                process_id=pid, class_name=class_name(h),
            ))
        return True

    user32.EnumWindows(_enum, 0)
    return out


def top_window_at(x: int, y: int) -> WindowInfo | None:
    """返回屏幕坐标 (x, y) 处最上层窗口，过滤掉工具窗口与自身。"""
    if not IS_WINDOWS:
        return None
    hwnd = user32.WindowFromPoint(POINT(int(x), int(y)))
    if not hwnd:
        return None
    root = user32.GetAncestor(wintypes.HWND(hwnd), GA_ROOT)
    hwnd = root or hwnd
    if not is_alt_tab_window(hwnd):
        return None
    pid = _pid_of(hwnd)
    return WindowInfo(
        hwnd=int(hwnd), title=window_title(hwnd), exe=exe_for_pid(pid),
        rect=window_rect(hwnd), frame=window_rect(hwnd, extended=True),
        process_id=pid, class_name=class_name(hwnd),
    )


def foreground_window() -> WindowInfo | None:
    if not IS_WINDOWS:
        return None
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = _pid_of(hwnd)
    return WindowInfo(
        hwnd=int(hwnd), title=window_title(hwnd), exe=exe_for_pid(pid),
        rect=window_rect(hwnd), frame=window_rect(hwnd, extended=True),
        process_id=pid, class_name=class_name(hwnd),
    )


def cursor_pos() -> tuple[int, int]:
    if not IS_WINDOWS:
        return (0, 0)
    p = POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return (p.x, p.y)


def monitor_rects() -> list[tuple[str, tuple[int, int, int, int]]]:
    """所有显示器的物理矩形：``[(设备名, LTRB), ...]``。"""
    if not IS_WINDOWS:
        return []
    out: list[tuple[str, tuple[int, int, int, int]]] = []

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT),
                    ("rcWork", RECT), ("dwFlags", wintypes.DWORD),
                    ("szDevice", ctypes.c_wchar * 32)]

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                        ctypes.POINTER(RECT), wintypes.LPARAM)
    def _cb(hmon, _hdc, _rect, _lp):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            r = info.rcMonitor
            out.append((info.szDevice, (r.left, r.top, r.right, r.bottom)))
        return True

    user32.EnumDisplayMonitors(None, None, _cb, 0)
    return out


# --------------------------------------------------------------------------- #
# PrintWindow 抓窗口（对 GPU 合成窗口也能拿到内容）
# --------------------------------------------------------------------------- #

def capture_hwnd(hwnd: int, client_only: bool = False):
    """用 PrintWindow + PW_RENDERFULLCONTENT 抓取窗口位图，返回 PIL.Image 或 None。"""
    if not IS_WINDOWS or not hwnd:
        return None
    from PIL import Image

    rect = window_rect(hwnd, extended=True) or window_rect(hwnd)
    w, h = rect[2] - rect[0], rect[3] - rect[1]
    if w <= 0 or h <= 0:
        rect = window_rect(hwnd)
        w, h = rect[2] - rect[0], rect[3] - rect[1]
    if w <= 0 or h <= 0:
        return None

    hdc_win = user32.GetWindowDC(wintypes.HWND(hwnd))
    if not hdc_win:
        hdc_win = user32.GetDC(None)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_win)
    bmp = gdi32.CreateCompatibleBitmap(hdc_win, w, h)
    gdi32.SelectObject(hdc_mem, bmp)
    try:
        ok = user32.PrintWindow(wintypes.HWND(hwnd), hdc_mem, PW_RENDERFULLCONTENT)
        if not ok:
            ok = user32.PrintWindow(wintypes.HWND(hwnd), hdc_mem, 0)
        if not ok:
            # 最后兜底：BitBlt 屏幕对应区域
            screen_dc = user32.GetDC(None)
            gdi32.BitBlt(hdc_mem, 0, 0, w, h, screen_dc, rect[0], rect[1], _SRCCOPY)
            user32.ReleaseDC(None, screen_dc)

        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h          # 负数 = 自上而下
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0      # BI_RGB
        buf = ctypes.create_string_buffer(w * h * 4)
        got = gdi32.GetDIBits(hdc_mem, bmp, 0, h, buf, ctypes.byref(bmi), _DIB_RGB_COLORS)
        if not got:
            return None
        img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB")
        return img
    except Exception:
        return None
    finally:
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(wintypes.HWND(hwnd), hdc_win)


def capture_hwnd_with_shadow(hwnd: int, margin: int = 24):
    """抓窗口并在四周补透明边距（近似 Windows 原生 DWM 阴影的观感）。"""
    from PIL import Image, ImageFilter

    img = capture_hwnd(hwnd)
    if img is None:
        return None
    if margin <= 0:
        return img
    canvas = Image.new("RGB", (img.width + margin * 2, img.height + margin * 2), (32, 32, 36))
    # 简易阴影：暗色圆角矩形 + 模糊
    shadow = Image.new("L", canvas.size, 0)
    from PIL import ImageDraw

    ImageDraw.Draw(shadow).rounded_rectangle(
        (margin - 6, margin - 2, canvas.width - margin + 6, canvas.height - margin + 10),
        radius=14, fill=150,
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(9))
    canvas.paste(Image.new("RGB", canvas.size, (0, 0, 0)), (0, 0), shadow)
    canvas.paste(img, (margin, margin))
    return canvas


# --------------------------------------------------------------------------- #
# 全局热键
# --------------------------------------------------------------------------- #

class HotkeyParseError(ValueError):
    pass


def parse_hotkey(spec: str) -> tuple[int, int]:
    """``"Ctrl+Alt+A"`` → ``(MOD_CONTROL|MOD_ALT|MOD_NOREPEAT, 0x41)``。"""
    parts = [p.strip() for p in (spec or "").replace(" ", "").split("+") if p.strip()]
    if not parts:
        raise HotkeyParseError("empty hotkey")
    mods = MOD_NOREPEAT
    key: int | None = None
    for part in parts:
        up = part.upper()
        if up in ("CTRL", "CONTROL"):
            mods |= MOD_CONTROL
        elif up == "ALT":
            mods |= MOD_ALT
        elif up == "SHIFT":
            mods |= MOD_SHIFT
        elif up in ("WIN", "META", "SUPER"):
            mods |= MOD_WIN
        else:
            vk = VK_CODES.get(up) or VK_CODES.get(up.replace("PRINTSCREEN", "PRTSC"))
            if vk is None:
                raise HotkeyParseError(f"unknown key: {part!r}")
            key = vk
    if key is None:
        raise HotkeyParseError(f"no main key in: {spec!r}")
    return mods, key


def format_hotkey(spec: str) -> str:
    """规范化显示：``ctrl+alt+a`` → ``Ctrl+Alt+A``。"""
    names = {v: k.title() for k, v in VK_CODES.items() if len(k) > 1}
    out: list[str] = []
    for part in (spec or "").split("+"):
        up = part.strip().upper()
        if up in ("CTRL", "CONTROL"):
            out.append("Ctrl")
        elif up == "ALT":
            out.append("Alt")
        elif up == "SHIFT":
            out.append("Shift")
        elif up in ("WIN", "META"):
            out.append("Win")
        elif up:
            out.append(names.get(VK_CODES.get(up, -1), up.title()))
    return "+".join(out)


class GlobalHotkeyManager:
    """在独立线程跑 Win32 消息循环注册全局热键。

    回调在线程内触发，调用方负责切回 UI 线程（Qt 侧用 ``QMetaObject`` 或信号）。
    """

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._bindings: dict[int, Callable[[], None]] = {}
        self._specs: dict[int, str] = {}
        self._failed: list[tuple[str, str]] = []
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ #

    def start(self, bindings: dict[str, Callable[[], None]]) -> tuple[list[str], list[tuple[str, str]]]:
        """启动线程并注册热键，返回 ``(已注册的 spec 列表, 失败的 (spec, reason) 列表)``。"""
        if not IS_WINDOWS:
            return [], [("all", "hotkeys are only supported on Windows")]
        if self._thread and self._thread.is_alive():
            self.stop()
        self._bindings.clear()
        self._specs.clear()
        self._failed.clear()

        key_id = 1
        for spec, callback in bindings.items():
            if not spec:
                continue
            try:
                mods, vk = parse_hotkey(spec)
            except HotkeyParseError as exc:
                self._failed.append((spec, str(exc)))
                continue
            self._bindings[key_id] = callback
            self._specs[key_id] = (spec, mods, vk)
            key_id += 1

        self._stop.clear()
        self._ready.clear()
        self._thread = threading.Thread(target=self._pump, name="SnapVaultHotkeys", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=2.0)
        return self.registered, list(self._failed)

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            # 唤醒消息循环
            if IS_WINDOWS:
                user32.PostThreadMessageW(self._thread.ident, WM_QUIT, 0, 0)
            self._thread.join(timeout=1.5)
        self._thread = None

    @property
    def failures(self) -> list[tuple[str, str]]:
        return list(self._failed)

    @property
    def registered(self) -> list[str]:
        return [spec for kid, (spec, _m, _v) in self._specs.items() if kid in self._registered_ids]

    # ------------------------------------------------------------------ #

    def _pump(self) -> None:
        self._registered_ids = set()
        for key_id, (spec, mods, vk) in self._specs.items():
            if user32.RegisterHotKey(None, key_id, mods, vk):
                self._registered_ids.add(key_id)
            else:
                self._failed.append((spec, "已被其他程序占用或不可用"))
        self._ready.set()

        msg = wintypes.MSG()
        try:
            while not self._stop.is_set():
                ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret in (0, -1):
                    break
                if msg.message == WM_HOTKEY:
                    cb = self._bindings.get(int(msg.wParam))
                    if cb:
                        try:
                            cb()
                        except Exception:
                            pass
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            for key_id in list(self._registered_ids):
                try:
                    user32.UnregisterHotKey(None, key_id)
                except Exception:
                    pass


# --------------------------------------------------------------------------- #
# 剪贴板（DIB → PIL，避免依赖 Qt）
# --------------------------------------------------------------------------- #

def clipboard_image():
    """读取剪贴板中的位图，返回 PIL.Image 或 None。"""
    if not IS_WINDOWS:
        return None
    from PIL import Image

    CF_DIB = 8
    CF_DIBV5 = 17
    if not user32.OpenClipboard(None):
        return None
    try:
        for fmt in (CF_DIBV5, CF_DIB):
            handle = user32.GetClipboardData(fmt)
            if not handle:
                continue
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                continue
            try:
                header = ctypes.cast(ptr, ctypes.POINTER(BITMAPINFOHEADER)).contents
                w, h = header.biWidth, header.biHeight
                bpp = header.biBitCount
                compression = header.biCompression
                top_down = h < 0
                h = abs(h)
                if w <= 0 or h <= 0 or bpp not in (24, 32):
                    continue
                if compression not in (0, 3):  # BI_RGB / BI_BITFIELDS
                    continue
                if fmt == CF_DIBV5:
                    offset = 40 + (ctypes.sizeof(wintypes.DWORD) * 4)
                else:
                    # 标准 DIB 头固定 40 字节（ctypes 结构体有尾部填充，不能直接用
                    # sizeof），BI_BITFIELDS 后面还要跟 3 个颜色掩码
                    offset = 40
                    if compression == 3:
                        offset += ctypes.sizeof(wintypes.DWORD) * 3
                stride = ((w * bpp + 31) // 32) * 4
                base = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_ubyte))
                raw = ctypes.string_at(ctypes.addressof(base.contents) + offset, stride * h)
                # 32 位 DIB 的第 4 字节是保留位，Pillow 用 BGRX 忽略它
                mode = "BGRX" if bpp == 32 else "BGR"
                img = Image.frombuffer("RGB", (w, h), raw, "raw", mode, stride, 1)
                if not top_down:
                    img = img.transpose(Image.FLIP_TOP_BOTTOM)
                return img.copy()
            finally:
                kernel32.GlobalUnlock(handle)
        return None
    except Exception:
        return None
    finally:
        user32.CloseClipboard()


def clipboard_files() -> list[str]:
    """读取剪贴板中的文件列表（CF_HDROP）。"""
    if not IS_WINDOWS:
        return []
    CF_HDROP = 15
    if not user32.OpenClipboard(None):
        return []
    try:
        handle = user32.GetClipboardData(CF_HDROP)
        if not handle:
            return []
        count = shell32.DragQueryFileW(handle, 0xFFFFFFFF, None, 0)
        out = []
        for i in range(count):
            buf = ctypes.create_unicode_buffer(1024)
            shell32.DragQueryFileW(handle, i, buf, 1024)
            out.append(buf.value)
        return out
    except Exception:
        return []
    finally:
        user32.CloseClipboard()


# --------------------------------------------------------------------------- #
# 写剪贴板
# --------------------------------------------------------------------------- #

def _to_bgra(rgb_bytes: bytes) -> bytes:
    """RGB 字节流 → BGRA（Pillow 没有 RGB→BGRA 的 packer，自己换通道）。

    Windows 剪贴板的 CF_DIB 是 32 位 BGRA：先转成可变字节数组，把每个像素的
    第 0/2 字节对调，最后把每 4 字节的第 4 位（alpha）留为 0 即可 —— DIB 的
    32 位模式下该字节被忽略。
    """
    buf = bytearray(rgb_bytes)
    buf[0::3], buf[2::3] = buf[2::3], buf[0::3]
    out = bytearray(len(buf) // 3 * 4)
    out[0::4] = buf[0::3]
    out[1::4] = buf[1::3]
    out[2::4] = buf[2::3]
    return bytes(out)


def _dib_header_bytes(width: int, height: int, bpp: int, stride: int) -> bytes:
    """拼出**标准 40 字节** BITMAPINFOHEADER（不依赖 ctypes 的结构体对齐）。

    剪贴板里的 CF_DIB 必须是紧凑的 40 字节头 + 像素，ctypes 结构体尾部会多出
    4 字节填充，所以这里手工打包，保证跨进程读取方（画图、Word、微信）能认。
    """
    import struct

    return struct.pack(
        "<IiiHHIIiiII",
        40,                  # biSize
        int(width),
        int(height),         # 正数 = 自下而上
        1,                   # biPlanes
        int(bpp),
        0,                   # BI_RGB
        int(stride * abs(height)),   # biSizeImage
        2835, 2835,          # 约 72 DPI
        0, 0,
    )


def set_clipboard_image(image) -> bool:
    """把 PIL 图像写进系统剪贴板（CF_DIB，兼容 Pain/Word/微信等）。

    纯 Win32 的兜底实现：先用 ``SaveDIB`` 风格手工拼一个 32 位 BITMAPINFOHEADER
    加 BGRA 像素的 HGLOBAL，再 ``SetClipboardData``。剪贴板里的句柄从此归系统所有，
    不能再 free，所以失败路径要分开处理。
    """
    if not IS_WINDOWS:
        return False
    try:
        img = image if image.mode == "RGB" else image.convert("RGB")
    except Exception:
        return False
    w, h = img.size
    if w <= 0 or h <= 0:
        return False
    bgra = _to_bgra(img.tobytes("raw", "RGB"))
    stride = w * 4
    header_bytes = _dib_header_bytes(w, h, 32, stride)
    total = len(header_bytes) + len(bgra)

    GMEM_MOVEABLE = 0x0002
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, total)
    if not handle:
        return False
    ptr = kernel32.GlobalLock(handle)
    if not ptr:
        kernel32.GlobalFree(handle)
        return False
    try:
        buffer = (ctypes.c_ubyte * total).from_address(ptr)
        buffer[:len(header_bytes)] = header_bytes
        buffer[len(header_bytes):] = bgra
    finally:
        kernel32.GlobalUnlock(handle)

    CF_DIB = 8
    if not user32.OpenClipboard(None):
        kernel32.GlobalFree(handle)
        return False
    try:
        user32.EmptyClipboard()
        ok = bool(user32.SetClipboardData(CF_DIB, handle))
        if not ok:
            kernel32.GlobalFree(handle)
        return ok
    except Exception:
        kernel32.GlobalFree(handle)
        return False
    finally:
        user32.CloseClipboard()


# --------------------------------------------------------------------------- #
# 任务栏身份（AppUserModelID）
# --------------------------------------------------------------------------- #

#: SnapVault 的任务栏身份。Windows 7+ 靠它决定任务栏按钮归属哪个"应用"：
#: 没有显式 AppID 时，任务栏直接用进程 exe 的图标 —— 用 pythonw.exe 启动的
#: 话就是那个蓝黄 Python logo，而且多个 pythonw 实例在任务栏互不分组。
#: 已实测：调用返回 S_OK，GetCurrentProcessExplicitAppUserModelID 读回一致。
APP_ID = "SnapVault.SnapVault.1"


def set_app_user_model_id(app_id: str = APP_ID) -> bool:
    """把当前进程的任务栏身份设为 ``app_id``。

    **必须在第一个窗口创建之前调用** —— 晚一步任务栏按钮已经按"无 AppID"
    登记完毕，再改也压不回去。失败只返回 False（Win7 以下没有这个 API），
    不抛异常：任务栏图标是锦上添花，不能让它挡住启动。
    """
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            ctypes.c_wchar_p(app_id))
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# 自启动
# --------------------------------------------------------------------------- #

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_APP_KEY = "SnapVault"


def set_autostart(enabled: bool, command: str) -> bool:
    """写注册表实现开机自启，返回是否成功。"""
    if not IS_WINDOWS:
        return False
    try:
        import winreg  # type: ignore

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, _APP_KEY, 0, winreg.REG_SZ, command)
            else:
                try:
                    winreg.DeleteValue(key, _APP_KEY)
                except FileNotFoundError:
                    pass
        return True
    except Exception:
        return False


def get_autostart_command() -> str:
    if not IS_WINDOWS:
        return ""
    try:
        import winreg  # type: ignore

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, _APP_KEY)
            return str(value)
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
# 回收站
# --------------------------------------------------------------------------- #

class _SHFILEOPSTRUCTW(ctypes.Structure):
    """``SHFileOperationW`` 的参数结构（x64 下 56 字节，ctypes 自动对齐）。"""

    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", ctypes.c_uint),
        ("pFrom", ctypes.c_wchar_p),
        ("pTo", ctypes.c_wchar_p),
        ("fFlags", ctypes.c_ushort),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", ctypes.c_wchar_p),
    ]


_FO_DELETE = 0x0003
_FOF_ALLOWUNDO = 0x0040          # 进回收站而不是真删
_FOF_NOCONFIRMATION = 0x0010
_FOF_SILENT = 0x0004
_FOF_NOERRORUI = 0x0400


def send_to_recycle_bin(paths) -> bool:
    """把一个或多个文件送进回收站（可恢复），成功返回 True。

    刻意用 ``SHFileOperationW`` 而不是 ``os.remove``：接管系统截图时若用户反悔，
    还能从回收站捞回来——"宁进回收站，不真删"。

    .. note::
       ``SHFileOperationW`` 的返回值**不可信**：实测它会把"已经搬进回收站"的
       成功操作报成 ``rc=2``（ERROR_FILE_NOT_FOUND）。所以这里不迷信返回值——
       原位置真的空了就算成功（可用文件系统直接验证，不受 API 怪癖影响）。
    """
    if not IS_WINDOWS:
        return False
    items = [str(p) for p in (paths or []) if str(p).strip()]
    if not items:
        return False
    try:
        shell = ctypes.windll.shell32
    except Exception:
        return False

    joined = "\0".join(items) + "\0\0"      # 需要双 null 结尾
    op = _SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = _FO_DELETE
    op.pFrom = joined
    op.pTo = None
    op.fFlags = (_FOF_ALLOWUNDO | _FOF_NOCONFIRMATION | _FOF_SILENT | _FOF_NOERRORUI)
    try:
        shell.SHFileOperationW.restype = ctypes.c_int
        shell.SHFileOperationW.argtypes = [ctypes.POINTER(_SHFILEOPSTRUCTW)]
        rc = shell.SHFileOperationW(ctypes.byref(op))
    except Exception:
        return False
    if rc == 0 and not op.fAnyOperationsAborted:
        return True
    # 返回值说失败，但原位置已经空了 → 实际上搬成功了
    return not any(Path(p).exists() for p in items)
