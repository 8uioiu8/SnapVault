"""屏幕捕获层：Win32 GDI 抓屏 + PrintWindow 抓窗口 + 冻结桌面。

为什么不用 ``QScreen.grabWindow``
--------------------------------
Qt 抓屏返回的像素尺寸与坐标都要经过 devicePixelRatio 换算，多显示器
**混合 DPI** 时虚拟桌面的逻辑坐标会被各屏独立缩放，区域选择器算出的
矩形与真实像素无法一一对应，截出来的图会错位或缺边。

因此这里统一走 GDI：先把整个虚拟桌面按物理像素抓到内存位图，之后所有
模式（区域 / 窗口 / 全屏 / 多屏）都只是这块位图上的裁剪 + 两三个兜底
策略。坐标系统只有一套（物理像素，左上角为虚拟桌面原点），
:func:`virtual_rect` 会把逻辑坐标换算成物理坐标，两套坐标的换算只在此处发生。

冻结桌面
--------
区域选择期间整个屏幕会被一张静止的位图覆盖（:func:`freeze`），这样
用户拖动选区时背景不会跳动，同时避免"边选边抓"导致的闪烁；代价是冻结
之后才出现的窗口不会被截到，所以冻结必须发生在按下热键的瞬间。
"""

from __future__ import annotations

import ctypes
import math
import sys
from dataclasses import dataclass, field
from ctypes import wintypes
from datetime import datetime
from typing import Any

from PIL import Image

from . import winapi
from .winapi import (
    BITMAPINFO,
    BITMAPINFOHEADER,
    IS_WINDOWS,
    _DIB_RGB_COLORS,
    _SRCCOPY,
    WindowInfo,
    set_dpi_awareness,
)

#: BitBlt 时把分层窗口（少量 DirectComposition 覆盖层）一起合入。
#: 默认开启，否则某些半透明浮层会被当成黑色矩形。
_CAPTUREBLT = 0x40000000

#: SM_* 虚拟屏幕度量索引
_SM_XVIRTUALSCREEN = 76
_SM_YVIRTUALSCREEN = 77
_SM_CXVIRTUALSCREEN = 78
_SM_CYVIRTUALSCREEN = 79

IS_MINIMIZED = 1


# --------------------------------------------------------------------------- #
# 语言 / 日志
# --------------------------------------------------------------------------- #

def _log(msg: str, exc: bool = False) -> None:
    """轻量日志。GUI 频繁抓屏时不需要每帧都刷屏，故默认只在失败时记录。"""
    stream = sys.stderr
    try:
        line = f"[snapvault.capture] {msg}\n"
        buf = getattr(stream, "buffer", None)
        if buf is not None:            # 未配置 UTF-8 时也能输出中文
            buf.write(line.encode("utf-8", "replace"))
            buf.flush()
        else:
            stream.write(line)
            stream.flush()
    except Exception:
        pass
    if exc:
        import traceback

        traceback.print_exc()


# --------------------------------------------------------------------------- #
# 坐标
# --------------------------------------------------------------------------- #

def _metric(index: int) -> int:
    try:
        return int(winapi.user32.GetSystemMetrics(index))
    except Exception:
        return 0


def logical_rect() -> tuple[int, int, int, int]:
    """虚拟桌面在 DPI 感知坐标系下的矩形（DPI 感知进程里即物理像素）。"""
    if IS_WINDOWS:
        rect = _measure_virtual_rect()
        if rect:
            return rect
    return (0, 0, 1920, 1080)


def virtual_rect() -> tuple[int, int, int, int]:
    """虚拟桌面的**物理像素**矩形 ``(left, top, right, bottom)``。

    支持负坐标（副屏在主屏左/上方）。虚拟屏幕度量只反映主屏的缩放比例，
    混合 DPI 下需要按每个显示器实际分辨率推算，否则副屏会被截短。"""
    if not IS_WINDOWS:
        return logical_rect()
    logical = logical_rect()
    monitors = winapi.monitor_rects()
    if not monitors:
        return logical
    for _name, rect in monitors:
        w = rect[2] - rect[0]
        h = rect[3] - rect[1]
        if w > logical[2] - logical[0] or h > logical[3] - logical[1]:
            return physical_union(monitors)
    return logical


def physical_rect(monitor: tuple[int, int, int, int]) -> tuple[int, int, int, int] | None:
    """把 GDI 报出的显示器矩形换算成物理像素矩形。"""
    if not IS_WINDOWS:
        return None
    try:
        x0, y0, x1, y1 = monitor
        mon = winapi.user32.MonitorFromPoint(winapi.POINT(x0, y0), winapi.MONITOR_DEFAULTTONEAREST)
        if not mon:
            return None
        dpi_x = wintypes.UINT()
        dpi_y = wintypes.UINT()
        if not winapi.shcore:
            return None
        if winapi.shcore.GetDpiForMonitor(wintypes.HMONITOR(mon), 0,
                                           ctypes.byref(dpi_x), ctypes.byref(dpi_y)) != 0:
            return None
        scale = dpi_x.value / 96.0
        if scale <= 0:
            return None
        return (round(x0 * scale), round(y0 * scale),
                round(x1 * scale), round(y1 * scale))
    except Exception:
        return None


def physical_union(monitors: list[tuple[str, tuple[int, int, int, int]]]) -> tuple[int, int, int, int]:
    """所有显示器物理矩形的并集（虚拟桌面物理边界）。"""
    lefts, tops, rights, bottoms = [], [], [], []
    for _name, rect in monitors:
        phys = physical_rect(rect) or rect
        lefts.append(phys[0])
        tops.append(phys[1])
        rights.append(phys[2])
        bottoms.append(phys[3])
    if not lefts:
        return logical_rect()
    return (min(lefts), min(tops), max(rights), max(bottoms))


def _measure_virtual_rect() -> tuple[int, int, int, int] | None:
    left = _metric(_SM_XVIRTUALSCREEN)
    top = _metric(_SM_YVIRTUALSCREEN)
    width = _metric(_SM_CXVIRTUALSCREEN)
    height = _metric(_SM_CYVIRTUALSCREEN)
    if width <= 0 or height <= 0:
        return None
    return (left, top, left + width, top + height)


def desktop_origin() -> tuple[int, int]:
    x0, y0, _x1, _y1 = virtual_rect()
    return (x0, y0)


def _clamp_rect(rect: tuple[int, int, int, int],
                bounds: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """把矩形裁剪到虚拟桌面范围内（支持负坐标）。"""
    bx0, by0, bx1, by1 = bounds
    x0 = max(int(rect[0]), bx0)
    y0 = max(int(rect[1]), by0)
    x1 = min(int(rect[2]), bx1)
    y1 = min(int(rect[3]), by1)
    if x1 <= x0 or y1 <= y0:
        return (0, 0, 0, 0)
    return (x0, y0, x1, y1)


# --------------------------------------------------------------------------- #
# 抓屏
# --------------------------------------------------------------------------- #

def _screen_scale() -> float:
    """主屏缩放比例（1.25 = 125%），仅用于报错时给出有用的提示。"""
    return winapi.scale_for_point(0, 0)


def grab_rect(rect: tuple[int, int, int, int] | None = None,
              include_layered: bool = True) -> Image.Image | None:
    """抓取屏幕矩形（物理像素，支持负坐标），返回 PIL RGB 图像。"""
    if not IS_WINDOWS:
        return None
    bounds = virtual_rect()
    if rect is None:
        target = bounds
    else:
        target = _clamp_rect(rect, bounds)
        if target[2] - target[0] <= 0 or target[3] - target[1] <= 0:
            _log(f"抓屏区域无效: {rect} 不在虚拟桌面 {bounds} 内")
            return None

    x0, y0, x1, y1 = target
    width = x1 - x0
    height = y1 - y0

    hdc_screen = winapi.user32.GetDC(None)
    if not hdc_screen:
        _log("GetDC(None) 失败，无法抓屏")
        return None
    hdc_mem = None
    hbitmap = None
    try:
        hdc_mem = winapi.gdi32.CreateCompatibleDC(hdc_screen)
        hbitmap = winapi.gdi32.CreateCompatibleBitmap(hdc_screen, width, height)
        if not hdc_mem or not hbitmap:
            _log("CreateCompatibleDC/Bitmap 失败")
            return None
        winapi.gdi32.SelectObject(hdc_mem, hbitmap)
        flags = _SRCCOPY | (_CAPTUREBLT if include_layered else 0)
        ok = winapi.gdi32.BitBlt(hdc_mem, 0, 0, width, height, hdc_screen, x0, y0, flags)
        if not ok:
            _log("BitBlt 失败（可能是 UAC 提权窗口或受保护的 DRM 区域）")
            return None

        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = width
        bmi.bmiHeader.biHeight = -height          # 负数 = 自上而下
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0            # BI_RGB
        buf = ctypes.create_string_buffer(width * height * 4)
        got = winapi.gdi32.GetDIBits(hdc_mem, hbitmap, 0, height, buf,
                                     ctypes.byref(bmi), _DIB_RGB_COLORS)
        if not got:
            _log("GetDIBits 失败")
            return None
        return Image.frombuffer("RGBA", (width, height), buf, "raw", "BGRA", 0, 1).convert("RGB")
    except Exception:
        _log("抓屏异常", exc=True)
        return None
    finally:
        if hbitmap:
            winapi.gdi32.DeleteObject(hbitmap)
        if hdc_mem:
            winapi.gdi32.DeleteDC(hdc_mem)
        winapi.user32.ReleaseDC(None, hdc_screen)


@dataclass
class FrozenDesktop:
    """冻结的虚拟桌面位图，附带坐标换算能力。"""

    image: Image.Image
    bounds: tuple[int, int, int, int]

    @property
    def origin(self) -> tuple[int, int]:
        return (self.bounds[0], self.bounds[1])

    @property
    def size(self) -> tuple[int, int]:
        return self.image.size

    def to_local(self, x: int, y: int) -> tuple[int, int]:
        """物理屏幕坐标 -> 位图内坐标。"""
        return (x - self.bounds[0], y - self.bounds[1])

    def to_screen(self, x: int, y: int) -> tuple[int, int]:
        """位图内坐标 -> 物理屏幕坐标。"""
        return (x + self.bounds[0], y + self.bounds[1])

    def crop(self, rect: tuple[int, int, int, int]) -> Image.Image:
        """按物理屏幕坐标裁剪冻结位图（自动裁剪到边界内）。"""
        x0, y0 = self.to_local(rect[0], rect[1])
        x1, y1 = self.to_local(rect[2], rect[3])
        x0 = max(0, min(x0, self.image.width))
        y0 = max(0, min(y0, self.image.height))
        x1 = max(0, min(x1, self.image.width))
        y1 = max(0, min(y1, self.image.height))
        if x1 <= x0 or y1 <= y0:
            return Image.new("RGB", (1, 1), (0, 0, 0))
        return self.image.crop((x0, y0, x1, y1)).copy()


def freeze(rect: tuple[int, int, int, int] | None = None) -> FrozenDesktop | None:
    """冻结整个虚拟桌面（或指定矩形），返回 :class:`FrozenDesktop`。"""
    bounds = virtual_rect() if rect is None else _clamp_rect(rect, virtual_rect())
    img = grab_rect(bounds)
    if img is None:
        return None
    return FrozenDesktop(img, bounds)


def frozen_or_live(rect: tuple[int, int, int, int]) -> tuple[Image.Image | None, str]:
    """先尝试冻结位图裁剪，失败退回实时抓屏。返回 ``(图像, 来源)``。"""
    snapped = grab_rect(rect)
    if snapped is not None:
        return snapped, "gdi"
    return None, "failed"


# --------------------------------------------------------------------------- #
# 光标
# --------------------------------------------------------------------------- #

def draw_cursor(image: Image.Image, bounds: tuple[int, int, int, int],
                hotspot: tuple[int, int] | None = None) -> Image.Image:
    """把当前鼠标光标画到图像上（尽力而为，失败返回原图）。

    先用 ``GetCursorInfo`` 拿到系统光标句柄，再用 ``DrawIconEx`` 画到一块
    与图像等大的内存位图上——比手工解析光标位图可靠得多，且天然支持
    PNG 主题光标。
    """
    if not IS_WINDOWS:
        return image
    try:
        class CURSORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                        ("hCursor", wintypes.HANDLE), ("ptScreenPos", winapi.POINT)]

        info = CURSORINFO()
        info.cbSize = ctypes.sizeof(CURSORINFO)
        if not winapi.user32.GetCursorInfo(ctypes.byref(info)):
            return image
        if not (info.flags & 0x00000001) or not info.hCursor:   # CURSOR_SHOWING
            return image

        px, py = (info.ptScreenPos.x, info.ptScreenPos.y) if hotspot is None else hotspot
        lx, ly = px - bounds[0], py - bounds[1]
        if not (0 <= lx < image.width and 0 <= ly < image.height):
            return image

        w = image.width
        h = image.height
        hdc_screen = winapi.user32.GetDC(None)
        hdc_mem = winapi.gdi32.CreateCompatibleDC(hdc_screen)
        hbitmap = winapi.gdi32.CreateCompatibleBitmap(hdc_screen, w, h)
        winapi.gdi32.SelectObject(hdc_mem, hbitmap)
        winapi.user32.DrawIconEx(hdc_mem, lx, ly, info.hCursor, 0, 0, 0, None, 0x0003)  # DI_NORMAL

        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0
        buf = ctypes.create_string_buffer(w * h * 4)
        if not winapi.gdi32.GetDIBits(hdc_mem, hbitmap, 0, h, buf, ctypes.byref(bmi),
                                      _DIB_RGB_COLORS):
            return image
        layer = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1)
        winapi.gdi32.DeleteObject(hbitmap)
        winapi.gdi32.DeleteDC(hdc_mem)
        winapi.user32.ReleaseDC(None, hdc_screen)

        # 光标层与冻结桌面做"取较亮像素"合成：光标通常是白色箭头 + 黑色描边，
        # 直接 alpha 叠加在半透明化后的背景上会看不清。
        base = image.convert("RGB")
        return Image.composite(layer.convert("RGB"), base, layer.split()[3])
    except Exception:
        return image


# --------------------------------------------------------------------------- #
# 捕获结果
# --------------------------------------------------------------------------- #

@dataclass
class CaptureResult:
    """一次捕获的产物，可直接交给 :class:`snapvault.vault.Vault` 归档。"""

    image: Image.Image
    mode: str = "region"                     # region / window / fullscreen / all_screens
    rect: tuple[int, int, int, int] = (0, 0, 0, 0)
    window: WindowInfo | None = None
    captured_at: datetime = field(default_factory=datetime.now)
    source: str = "gdi"                      # gdi / printwindow / clipboard
    notes: list[str] = field(default_factory=list)

    @property
    def title(self) -> str:
        if self.window is not None:
            return self.window.title
        return ""

    @property
    def exe(self) -> str:
        if self.window is not None:
            return self.window.exe
        return ""

    @property
    def monitor(self) -> str:
        if self.window is not None and self.window.monitor:
            return self.window.monitor
        return monitor_name_for(self.rect)

    def context(self):
        """构造分类器需要的 :class:`~snapvault.classifier.ShotContext`。"""
        from .classifier import ShotContext

        return ShotContext(
            exe=self.exe,
            title=self.title,
            monitor=self.monitor,
            source=self.mode,
            captured_at=self.captured_at,
        )


def monitor_name_for(rect: tuple[int, int, int, int]) -> str:
    """矩形中心点所在显示器的设备名。"""
    cx = (rect[0] + rect[2]) // 2
    cy = (rect[1] + rect[3]) // 2
    for name, mon in winapi.monitor_rects():
        if mon[0] <= cx < mon[2] and mon[1] <= cy < mon[3]:
            return name
    return ""


# --------------------------------------------------------------------------- #
# 各种模式
# --------------------------------------------------------------------------- #

def capture_region(rect: tuple[int, int, int, int],
                   frozen: FrozenDesktop | None = None,
                   with_cursor: bool = False) -> CaptureResult | None:
    """区域截图：优先用冻结位图裁剪（避免选完再抓导致的画面变化）。"""
    target = _clamp_rect(rect, virtual_rect())
    if target == (0, 0, 0, 0):
        return None
    image: Image.Image | None = None
    source = "gdi"
    if frozen is not None:
        image = frozen.crop(target)
        source = "frozen"
    if image is None:
        image, source = frozen_or_live(target)
    if image is None:
        return None
    if with_cursor:
        image = draw_cursor(image, target)
    return CaptureResult(image=image, mode="region", rect=target, source=source,
                         captured_at=datetime.now())


def _restore_if_minimized(hwnd: int) -> bool:
    """若窗口最小化则还原并短暂等待，返回是否做过还原。"""
    try:
        if winapi.user32.IsIconic(wintypes.HWND(hwnd)):
            winapi.user32.ShowWindow(wintypes.HWND(hwnd), 9)   # SW_RESTORE
            import time

            time.sleep(0.18)
            return True
    except Exception:
        pass
    return False


def capture_window(win: WindowInfo | int,
                   shadow: bool = True,
                   prefer_printwindow: bool = True,
                   with_cursor: bool = False) -> CaptureResult | None:
    """窗口截图。``PrintWindow`` 优先（被遮挡也能截到内容），失败退回屏幕区域抓取。"""
    info = win if isinstance(win, WindowInfo) else None
    hwnd = info.hwnd if info is not None else int(win)
    if not hwnd:
        return None
    if info is None:
        from .classifier import ShotContext  # noqa: F401  (保持导入顺序稳定)

        rect = winapi.window_rect(hwnd, extended=True) or winapi.window_rect(hwnd)
        info = WindowInfo(hwnd=hwnd, title=winapi.window_title(hwnd),
                          exe=winapi.exe_for_pid(winapi._pid_of(hwnd)),
                          rect=rect, frame=rect,
                          monitor=monitor_name_for(rect))

    restored = _restore_if_minimized(hwnd)
    notes: list[str] = []
    image: Image.Image | None = None
    source = "screen"

    if prefer_printwindow:
        image = winapi.capture_hwnd(hwnd)
        if image is not None:
            source = "printwindow"
        else:
            notes.append("PrintWindow 失败，改用屏幕区域抓取")
    if image is None:
        frame = winapi.window_rect(hwnd, extended=True) or info.rect
        image = grab_rect(frame)
        if image is None:
            _log(f"窗口截图失败: {info.title!r}")
            return None
        if shadow:
            image = _add_shadow(image)
    elif shadow:
        image = _add_shadow(image)

    if restored:
        notes.append("窗口此前处于最小化，已还原后截图")
    if with_cursor:
        image = draw_cursor(image, info.rect)

    return CaptureResult(image=image, mode="window", rect=info.rect, window=info,
                         source=source, notes=notes, captured_at=datetime.now())


def _add_shadow(image: Image.Image, margin: int = 18) -> Image.Image:
    """给窗口图补一层近似 DWM 的柔和阴影（观感更像系统原生截图）。"""
    from PIL import ImageDraw, ImageFilter

    if margin <= 0:
        return image
    canvas = Image.new("RGB", (image.width + margin * 2, image.height + margin * 2), (28, 28, 32))
    shadow = Image.new("L", canvas.size, 0)
    ImageDraw.Draw(shadow).rounded_rectangle(
        (margin - 4, margin - 1, canvas.width - margin + 4, canvas.height - margin + 7),
        radius=12, fill=170,
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(7))
    canvas.paste(Image.new("RGB", canvas.size, (12, 12, 14)), (0, 0), shadow)
    canvas.paste(image, (margin, margin))
    return canvas


def capture_fullscreen(with_cursor: bool = False,
                       monitor: int | None = None) -> CaptureResult | None:
    """全屏截图：默认整块虚拟桌面；``monitor`` 给定时只抓该显示器序号。"""
    monitors = winapi.monitor_rects()
    if monitor is not None and 0 <= monitor < len(monitors):
        name, raw = monitors[monitor]
        target = physical_rect(raw) or raw
        mode = "fullscreen"
    else:
        target = virtual_rect()
        name = ""
        mode = "all_screens" if len(monitors) > 1 else "fullscreen"
    image = grab_rect(target)
    if image is None:
        return None
    if with_cursor:
        image = draw_cursor(image, target)
    return CaptureResult(image=image, mode=mode, rect=target, source="gdi",
                         captured_at=datetime.now())


def capture_all_screens(**kwargs: Any) -> CaptureResult | None:
    """多屏拼接截图（等价于整块虚拟桌面，含各屏相对位置）。"""
    kwargs.pop("monitor", None)
    result = capture_fullscreen(monitor=None, **kwargs)
    if result is not None:
        result.mode = "all_screens"
    return result


def capture_active_window(**kwargs: Any) -> CaptureResult | None:
    """抓当前前台窗口（热键触发时前台窗口通常仍是我们自己的悬浮层，故由调用方先隐藏）。"""
    win = winapi.foreground_window()
    if win is None:
        return None
    return capture_window(win, **kwargs)


def capture_monitor(index: int = 0, **kwargs: Any) -> CaptureResult | None:
    return capture_fullscreen(monitor=index, **kwargs)


# --------------------------------------------------------------------------- #
# 显示器辅助
# --------------------------------------------------------------------------- #

def monitors_summary() -> list[dict[str, Any]]:
    """显示器清单（调试 / 设置面板用）。"""
    out: list[dict[str, Any]] = []
    for i, (name, raw) in enumerate(winapi.monitor_rects()):
        phys = physical_rect(raw) or raw
        out.append({
            "index": i,
            "device": name,
            "rect": list(raw),
            "physical": list(phys),
            "size": [phys[2] - phys[0], phys[3] - phys[1]],
            "scale": round(winapi.scale_for_point(raw[0] + 1, raw[1] + 1), 3),
        })
    return out


def screen_scale_note() -> str:
    scale = _screen_scale()
    if abs(scale - 1.0) < 0.01:
        return "100%"
    return f"{round(scale * 100)}%（已开启 Per-Monitor-V2 DPI 感知）"


def prepare() -> str:
    """进程启动时调用一次：开启 DPI 感知，返回生效模式名。"""
    mode = set_dpi_awareness()
    return mode


# --------------------------------------------------------------------------- #
# 估算辅助
# --------------------------------------------------------------------------- #

def estimate_text_size(lines: int, width: int, height: int) -> float:
    """粗略估算截图里的正文行高（放大镜 / 自动化测试用，不参与分类）。"""
    if lines <= 0 or height <= 0:
        return 0.0
    return height / lines * (1.0 if width else 1.0)


def rect_area(rect: tuple[int, int, int, int]) -> int:
    return max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])


def rect_center(rect: tuple[int, int, int, int]) -> tuple[int, int]:
    return ((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2)


def distance(a: tuple[int, int], b: tuple[int, int]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])
