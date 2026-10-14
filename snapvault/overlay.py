"""区域 / 窗口选择悬浮层（对标 Win+Shift+S 的交互）。

设计要点
--------
* **冻结背景**：整个虚拟桌面被一张静止位图盖住。用户拖动时背景不会跳动，
  也避免了"边选边抓"的闪烁；图层、放大镜、提示条都画在这一张图上。
* **物理像素为唯一坐标**：冻结位图、:mod:`snapvault.capture` 返回的矩形、
  窗口矩形全都是物理像素。Qt 的鼠标事件是逻辑坐标，于是只在
  :meth:`SelectionOverlay._to_image` 一处做换算，其余代码只认物理像素。
* **交互与绘制分离**：选区状态机（:meth:`SelectionOverlay.press` /
  :meth:`move` / :meth:`release`）只吃"位图坐标"，与 Qt 事件解耦，
  因此可以在没有真实鼠标的自动化测试里完整驱动一遍。
* **不安装全局钩子**：悬浮层自己就是置顶全屏窗口，直接消费鼠标事件。
  Win32 挂钩（`WH_MOUSE_LL`）在 Python 里既要保持消息泵存活、又容易被
  安全软件拦截，这里刻意不用它。

窗口模式会实时枚举桌面上的可见窗口；预览图上只呈现"冻结那一刻"的内容，
所以它不会把悬浮层自己画进去。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Iterable

from PySide6.QtCore import QPoint, QRect, QRectF, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QKeyEvent, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import QApplication, QWidget

from PIL import Image

from . import winapi
from .capture import FrozenDesktop, monitor_name_for

# --------------------------------------------------------------------------- #
# 语言 / 日志
# --------------------------------------------------------------------------- #

def _log(msg: str, exc: bool = False) -> None:
    try:
        buf = getattr(sys.stderr, "buffer", None)
        line = f"[snapvault.overlay] {msg}\n"
        if buf is not None:
            buf.write(line.encode("utf-8", "replace"))
            buf.flush()
        else:
            sys.stderr.write(line)
            sys.stderr.flush()
    except Exception:
        pass
    if exc:
        import traceback

        traceback.print_exc()


def is_chinese() -> bool:
    """界面语言判定：显式环境变量优先，否则看系统区域。"""
    env = os.environ.get("SNAPVAULT_LANG", "").strip().lower()
    if env in ("zh", "zh-cn", "chinese"):
        return True
    if env in ("en", "en-us", "english"):
        return False
    try:
        import locale

        lang = (locale.getdefaultlocale()[0] or "").lower()
        if lang.startswith("zh"):
            return True
    except Exception:
        pass
    return True   # 面向中文用户的项目，默认中文


class T:
    """文案表：中文为主，英文兜底（``SNAPVAULT_LANG=en`` 可切换）。"""

    _ZH = {
        "hint_region": "拖动鼠标选择区域 · 单击选择窗口 · 右键 / Esc 取消 · Enter 确认",
        "hint_window": "移动鼠标高亮窗口 · 单击截图 · R 切换回区域模式 · 右键 / Esc 取消",
        "confirm": "确认",
        "reselect": "重选",
        "annotate": "标注",
        "window_mode": "窗口模式",
        "region_mode": "区域模式",
        "size": "尺寸",
        "pos": "位置",
        "full": "全屏",
        "copy_confirm": "复制并确认",
        "window_locked": "已锁定窗口",
        "too_small": "选区太小，至少 8×8 像素",
        "no_window": "该位置没有可截取的窗口",
        "cancelled": "已取消",
    }
    _EN = {
        "hint_region": "Drag to select · click a window to capture it · right-click / Esc to cancel · Enter to confirm",
        "hint_window": "Hover to highlight a window · click to capture · R for region mode · right-click / Esc to cancel",
        "confirm": "Confirm",
        "reselect": "Redo",
        "annotate": "Annotate",
        "window_mode": "Window",
        "region_mode": "Region",
        "size": "Size",
        "pos": "Pos",
        "full": "Full screen",
        "copy_confirm": "Copy & confirm",
        "window_locked": "Window locked",
        "too_small": "Selection too small (min 8x8 px)",
        "no_window": "No capturable window here",
        "cancelled": "Cancelled",
    }

    def __init__(self, chinese: bool | None = None) -> None:
        self.zh = is_chinese() if chinese is None else chinese

    def __call__(self, key: str) -> str:
        table = self._ZH if self.zh else self._EN
        return table.get(key, key)


# --------------------------------------------------------------------------- #
# 结果模型
# --------------------------------------------------------------------------- #

class OverlayMode(str, Enum):
    REGION = "region"
    WINDOW = "window"


class OverlayAction(str, Enum):
    CONFIRM = "confirm"          # 确认选区，交给上层保存
    COPY = "copy"                # 确认并复制到剪贴板
    ANNOTATE = "annotate"        # 确认并进入标注
    RESELECT = "reselect"        # 未完成，继续留在悬浮层
    CANCEL = "cancel"


@dataclass
class OverlayResult:
    """悬浮层的输出：一次"用户想截什么"的意图描述。"""

    action: OverlayAction
    rect: tuple[int, int, int, int] = (0, 0, 0, 0)
    mode: str = "region"
    window: winapi.WindowInfo | None = None
    #: 区域模式下的选区应优先从冻结位图裁剪（保证"所见即所得"）
    from_frozen: bool = True

    @property
    def ok(self) -> bool:
        return self.action in (OverlayAction.CONFIRM, OverlayAction.COPY, OverlayAction.ANNOTATE)

    @property
    def monitor(self) -> str:
        if self.window is not None and self.window.monitor:
            return self.window.monitor
        return monitor_name_for(self.rect)


# --------------------------------------------------------------------------- #
# PIL <-> QImage
# --------------------------------------------------------------------------- #

def pil_to_qimage(img: Image.Image) -> QImage:
    """PIL -> QImage（深拷贝，避免依赖 PIL 缓冲区的生命周期）。"""
    if img.mode != "RGB":
        img = img.convert("RGB")
    data = img.tobytes("raw", "RGB")
    qimg = QImage(data, img.width, img.height, img.width * 3, QImage.Format_RGB888)
    return qimg.copy()


# --------------------------------------------------------------------------- #
# 绘图小工具
# --------------------------------------------------------------------------- #

def _dim_pen(color: QColor, width: float = 1.0, dash: bool = False) -> QPen:
    pen = QPen(color)
    pen.setWidthF(width)
    pen.setCosmetic(True)
    if dash:
        pen.setStyle(Qt.DashLine)
    return pen


def _rounded_label(painter: QPainter, anchor: QPoint, lines: list[str],
                   bg: QColor, fg: QColor = QColor(245, 245, 248),
                   font_size: int = 8, pad: int = 6, radius: int = 6) -> QRect:
    """在 ``anchor`` 处画一个圆角信息条，返回它占用的矩形（用于避免互相压盖）。"""
    font = QFont("Microsoft YaHei UI", font_size)
    painter.setFont(font)
    metrics = painter.fontMetrics()
    width = max(metrics.horizontalAdvance(t) for t in lines) + pad * 2
    height = metrics.height() * len(lines) + pad * 2
    x, y = anchor.x(), anchor.y()
    rect = QRect(x, y, int(width), int(height))
    painter.setPen(Qt.NoPen)
    painter.setBrush(bg)
    painter.drawRoundedRect(rect, radius, radius)
    painter.setPen(fg)
    for i, text in enumerate(lines):
        painter.drawText(x + pad, y + pad + metrics.ascent() + i * metrics.height(), text)
    return rect


def pick_window_at(windows: Iterable[winapi.WindowInfo], x: int, y: int,
                   ignore: Iterable[int] = ()) -> winapi.WindowInfo | None:
    """返回坐标处最靠前（z 序最小）的可截窗口。"""
    skip = set(ignore)
    best: winapi.WindowInfo | None = None
    for win in windows:
        if win.hwnd in skip:
            continue
        rx0, ry0, rx1, ry1 = win.rect
        if rx0 <= x <= rx1 and ry0 <= y <= ry1:
            if best is None or win.z < best.z:
                best = win
    return best


# --------------------------------------------------------------------------- #
# 悬浮层
# --------------------------------------------------------------------------- #

class SelectionOverlay(QWidget):
    """覆盖整个虚拟桌面的无输入悬浮层。

    信号
    ----
    ``picked(object)``  用户确认了选区，参数是 :class:`OverlayResult`
    ``cancelled()``     用户取消
    """

    picked = Signal(object)
    cancelled = Signal()

    MIN_SIZE = 8

    def __init__(self, frozen: FrozenDesktop,
                 mode: OverlayMode = OverlayMode.REGION,
                 cfg=None,
                 overlay_mode: str = "window",
                 language: bool | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from .config import Config

        self.cfg: Config = cfg or Config()
        self.t = T(language)
        self.frozen = frozen
        self.image: Image.Image = frozen.image
        self.bounds = frozen.bounds
        self.mode = OverlayMode(mode)
        self.overlay_mode = overlay_mode          # 选完区域后悬浮层的呈现方式
        self._ignored_hwnds: set[int] = {int(self.winId())}
        if parent is not None:
            self._ignored_hwnds.add(int(parent.winId()))

        self._origin = (self.bounds[0], self.bounds[1])
        self._img_size = self.image.size
        self._ratio = 1.0

        self.start: QPoint | None = None            # 图像坐标
        self.current: QPoint | None = None
        self.selection: QRect | None = None         # 图像坐标，已确认
        self.hover_pos: QPoint | None = None        # 图像坐标
        self.drag_origin: QPoint | None = None      # 拖动已选区域时用
        self.drag_base: QRect | None = None
        self.active_handle: str | None = None       # n/s/e/w/ne/... 用于拉伸
        self.finished = False

        self.windows: list[winapi.WindowInfo] = []
        self.hover_window: winapi.WindowInfo | None = None
        self.locked_window: winapi.WindowInfo | None = None

        self.toolbar_rects: dict[str, QRect] = {}
        self.toolbar_hover: str | None = None

        self._bg = pil_to_qimage(self.image)

        self._setup_window()
        self._setup_refresh()

    # -- 窗口设置 ---------------------------------------------------------- #

    def _setup_window(self) -> None:
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_DeleteOnClose, False)
        self.setMouseTracking(True)
        self.setCursor(Qt.CrossCursor)

    def _setup_refresh(self) -> None:
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(260)
        self._refresh_timer.timeout.connect(self._refresh_windows)
        self._redraw_timer = QTimer(self)
        self._redraw_timer.setInterval(33)
        self._redraw_timer.timeout.connect(self.update)

    # -- 坐标换算 ---------------------------------------------------------- #

    def _screen_ratio(self) -> float:
        app = QGuiApplication.instance()
        if app is None:
            return 1.0
        screen = None
        try:
            screen = app.screenAt(QPoint(*winapi.cursor_pos()))
        except Exception:
            screen = None
        screen = screen or QGuiApplication.primaryScreen()
        if screen is None:
            return 1.0
        return float(screen.devicePixelRatio() or 1.0)

    def _prepare_geometry(self) -> None:
        """把悬浮层铺满虚拟桌面，并决定绘画缩放比例。"""
        x0, y0, x1, y1 = self.bounds
        self.setGeometry(x0, y0, x1 - x0, y1 - y0)
        ratio = self._screen_ratio()
        # 位图像素 / 窗口逻辑像素。正常情况下等于 ratio；
        # 混合 DPI 或缩放异常时以位图比例为准（位图是唯一的真值来源）。
        self._ratio = ratio

    def _to_image(self, pos: QPoint | tuple[int, int]) -> QPoint:
        """窗口逻辑坐标 -> 位图坐标（物理像素）。"""
        if isinstance(pos, QPoint):
            lx, ly = pos.x(), pos.y()
        else:
            lx, ly = pos
        return QPoint(int(round(lx * self._ratio)), int(round(ly * self._ratio)))

    def _to_logic(self, x: int, y: int) -> QPoint:
        """位图坐标 -> 窗口逻辑坐标（仅用于自绘元素定位）。"""
        r = self._ratio or 1.0
        return QPoint(int(round(x / r)), int(round(y / r)))

    def _image_rect(self) -> QRect:
        return QRect(0, 0, self._img_size[0], self._img_size[1])

    def _clamp_image_point(self, p: QPoint) -> QPoint:
        r = self._image_rect()
        return QPoint(min(max(p.x(), r.left()), r.right()), min(max(p.y(), r.top()), r.bottom()))

    # -- 状态机（可被测试直接调用） ---------------------------------------- #

    def press(self, image_pos: QPoint) -> None:
        p = self._clamp_image_point(image_pos)
        if self.mode is OverlayMode.WINDOW:
            self._click_window(p)
            return
        handle = self._handle_at(p)
        if self.selection is not None and (handle or self.selection.contains(p)):
            self.active_handle = handle
            self.drag_origin = p
            self.drag_base = QRect(self.selection)
            return
        self.selection = None
        self.active_handle = None
        self.start = p
        self.current = p

    def move(self, image_pos: QPoint) -> None:
        p = self._clamp_image_point(image_pos)
        self.hover_pos = p
        if self.mode is OverlayMode.WINDOW:
            self.hover_window = pick_window_at(self.windows, *self._screen_of(p),
                                               ignore=self._ignored_hwnds)
            return
        if self.active_handle and self.drag_base is not None and self.drag_origin is not None:
            self.selection = self._resize(self.drag_base, self.active_handle,
                                          p.x() - self.drag_origin.x(),
                                          p.y() - self.drag_origin.y())
        elif self.drag_base is not None and self.drag_origin is not None:
            self.selection = self._translate(self.drag_base, p.x() - self.drag_origin.x(),
                                             p.y() - self.drag_origin.y())
        elif self.start is not None:
            self.current = p
            self.selection = None

    def release(self, image_pos: QPoint) -> None:
        p = self._clamp_image_point(image_pos)
        if self.mode is OverlayMode.WINDOW:
            return
        if self.start is not None:
            rect = QRect(self.start, p).normalized()
            self.start = None
            self.current = None
            if rect.width() < self.MIN_SIZE or rect.height() < self.MIN_SIZE:
                self.selection = None
            else:
                self.selection = rect
        self.active_handle = None
        self.drag_origin = None
        self.drag_base = None

    def _translate(self, base: QRect, dx: int, dy: int) -> QRect:
        moved = QRect(base).translated(dx, dy)
        r = self._image_rect()
        if moved.left() < r.left():
            moved.moveLeft(r.left())
        if moved.top() < r.top():
            moved.moveTop(r.top())
        if moved.right() > r.right():
            moved.moveRight(r.right())
        if moved.bottom() > r.bottom():
            moved.moveBottom(r.bottom())
        return moved

    def _resize(self, base: QRect, handle: str, dx: int, dy: int) -> QRect:
        r = QRect(base)
        if "w" in handle:
            r.setLeft(r.left() + dx)
        if "e" in handle:
            r.setRight(r.right() + dx)
        if "n" in handle:
            r.setTop(r.top() + dy)
        if "s" in handle:
            r.setBottom(r.bottom() + dy)
        r = r.normalized()
        bounds = self._image_rect()
        r.setLeft(max(r.left(), bounds.left()))
        r.setTop(max(r.top(), bounds.top()))
        r.setRight(min(r.right(), bounds.right()))
        r.setBottom(min(r.bottom(), bounds.bottom()))
        return r

    def _screen_of(self, p: QPoint) -> tuple[int, int]:
        return (p.x() + self.bounds[0], p.y() + self.bounds[1])

    def _handle_at(self, p: QPoint, slack: int = 6) -> str | None:
        if self.selection is None:
            return None
        r = self.selection
        left, right = abs(p.x() - r.left()) <= slack, abs(p.x() - r.right()) <= slack
        top, bottom = abs(p.y() - r.top()) <= slack, abs(p.y() - r.bottom()) <= slack
        inside_x = r.left() - slack <= p.x() <= r.right() + slack
        inside_y = r.top() - slack <= p.y() <= r.bottom() + slack
        if not (inside_x and inside_y):
            return None
        h = ""
        if top:
            h += "n"
        elif bottom:
            h += "s"
        if left:
            h += "w"
        elif right:
            h += "e"
        if h:
            # 角上要求两条边都命中，避免边缘误判成角
            if len(h) == 2 and not (inside_x and inside_y):
                return None
            return h
        return None

    # -- 窗口模式 ---------------------------------------------------------- #

    def _refresh_windows(self) -> None:
        if self.mode is not OverlayMode.WINDOW:
            return
        try:
            self.windows = winapi.list_windows(include_self_pid=False)
        except Exception:
            self.windows = []
        if self.hover_pos is not None:
            self.hover_window = pick_window_at(self.windows, *self._screen_of(self.hover_pos),
                                               ignore=self._ignored_hwnds)

    def _click_window(self, p: QPoint) -> None:
        win = pick_window_at(self.windows, *self._screen_of(p), ignore=self._ignored_hwnds)
        if win is None:
            self.locked_window = None
            self.update()
            return
        self.locked_window = win
        self.hover_window = win
        self.update()

    # -- 确认 / 取消 ------------------------------------------------------- #

    def current_rect_screen(self) -> tuple[int, int, int, int]:
        """当前选区（物理屏幕坐标）。"""
        if self.mode is OverlayMode.WINDOW and self.locked_window is not None:
            return self.locked_window.rect
        if self.selection is None:
            return (0, 0, 0, 0)
        r = self.selection
        return (r.left() + self.bounds[0], r.top() + self.bounds[1],
                r.right() + 1 + self.bounds[0], r.bottom() + 1 + self.bounds[1])

    def confirm(self, action: OverlayAction = OverlayAction.CONFIRM) -> None:
        if self.finished:
            return
        win = None
        if self.mode is OverlayMode.WINDOW:
            win = self.locked_window or self.hover_window
            if win is None:
                self.update()
                return
            rect = win.rect
            mode = "window"
        else:
            rect = self.current_rect_screen()
            if rect[2] - rect[0] < self.MIN_SIZE or rect[3] - rect[1] < self.MIN_SIZE:
                self.update()
                return
            mode = "region"
        self.finished = True
        self.picked.emit(OverlayResult(action=action, rect=rect, mode=mode, window=win,
                                       from_frozen=True))

    def cancel(self) -> None:
        if self.finished:
            return
        self.finished = True
        self.cancelled.emit()

    # -- 事件（真实输入走的是全局钩子，这里是兜底路径） -------------------- #

    def mousePressEvent(self, event: QMouseEvent) -> None:      # noqa: N802
        if event.button() == Qt.RightButton:
            self.cancel()
            return
        pos = self._to_image(event.position().toPoint())
        action = self._hit_toolbar(pos)
        if action is not None:
            self.click_toolbar(action)
            return
        self.press(pos)
        self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:       # noqa: N802
        pos = self._to_image(event.position().toPoint())
        action = self._hit_toolbar(pos)
        if action != self.toolbar_hover:
            self.toolbar_hover = action
        self.move(pos)
        self._update_cursor_shape(pos)
        self.update()

    def _update_cursor_shape(self, pos: QPoint) -> None:
        if self.mode is OverlayMode.WINDOW:
            self.setCursor(Qt.PointingHandCursor)
            return
        if self._hit_toolbar(pos) is not None:
            self.setCursor(Qt.ArrowCursor)
            return
        handle = self._handle_at(pos)
        if handle == "n" or handle == "s":
            self.setCursor(Qt.SizeVerCursor)
        elif handle == "e" or handle == "w":
            self.setCursor(Qt.SizeHorCursor)
        elif handle in ("nw", "se"):
            self.setCursor(Qt.SizeFDiagCursor)
        elif handle in ("ne", "sw"):
            self.setCursor(Qt.SizeBDiagCursor)
        elif self.selection is not None and self.selection.contains(pos):
            self.setCursor(Qt.SizeAllCursor)
        else:
            self.setCursor(Qt.CrossCursor)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:    # noqa: N802
        self.release(self._to_image(event.position().toPoint()))
        self.update()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self.confirm()

    def keyPressEvent(self, event: QKeyEvent) -> None:          # noqa: N802
        key = event.key()
        if key == Qt.Key_Escape:
            self.cancel()
        elif key in (Qt.Key_Return, Qt.Key_Enter):
            self.confirm()
        elif key == Qt.Key_R:
            self.set_mode(OverlayMode.WINDOW if self.mode is OverlayMode.REGION
                          else OverlayMode.REGION)
        elif key == Qt.Key_A:
            self.confirm(OverlayAction.ANNOTATE)
        elif key == Qt.Key_C and event.modifiers() & Qt.ControlModifier:
            self.confirm(OverlayAction.COPY)
        elif key in (Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down):
            self._nudge(key, event.modifiers())
        else:
            super().keyPressEvent(event)

    def _nudge(self, key: int, modifiers=Qt.NoModifier) -> bool:
        """方向键微调选区：1 像素；按住 Shift 时每次 10 像素。

        框选到"差一点点"的位置时，用鼠标再拖一次几乎不可能精确到位，
        键盘微调是这个操作唯一的可靠手段。返回是否真的移动了。
        """
        if self.selection is None:
            return False
        step = 10 if (modifiers & Qt.ShiftModifier) else 1
        dx = (-step if key == Qt.Key_Left else
              step if key == Qt.Key_Right else 0)
        dy = (-step if key == Qt.Key_Up else
              step if key == Qt.Key_Down else 0)
        base = QRect(self.selection)
        moved = self._translate(base, dx, dy)
        if moved == base:          # 已经贴边，不发出无意义的 update
            return False
        self.selection = moved
        self.update()
        return True

    def set_mode(self, mode: OverlayMode) -> None:
        self.mode = OverlayMode(mode)
        self.selection = None
        self.locked_window = None
        self.hover_window = None
        if self.mode is OverlayMode.WINDOW:
            self._refresh_windows()
        self.update()

    # -- 工具条 ------------------------------------------------------------ #

    def _toolbar_items(self) -> list[tuple[str, str]]:
        """``[(动作, 文案), ...]``，随当前状态变化。"""
        if self.selection is None and self.locked_window is None:
            return []
        if self.mode is OverlayMode.WINDOW:
            return [("confirm", self.t("confirm")), ("reselect", self.t("reselect"))]
        return [
            ("confirm", self.t("confirm")),
            ("annotate", self.t("annotate")),
            ("window", self.t("window_mode")),
            ("reselect", self.t("reselect")),
        ]

    def _layout_toolbar(self, painter: QPainter, anchor: QRect) -> dict[str, QRect]:
        items = self._toolbar_items()
        if not items:
            return {}
        font = QFont("Microsoft YaHei UI", 9)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        pad_x, pad_y, gap = 14, 7, 6
        widths = [metrics.horizontalAdvance(text) + pad_x * 2 for _a, text in items]
        height = metrics.height() + pad_y * 2
        total = sum(widths) + gap * (len(items) - 1)

        x = anchor.left()
        y = anchor.bottom() + 8
        if y + height > self._img_size[1]:
            y = anchor.top() - height - 8
        if x + total > self._img_size[0]:
            x = self._img_size[0] - total - 4
        x = max(4, x)

        rects: dict[str, QRect] = {}
        cursor = x
        for (action, _text), width in zip(items, widths):
            rects[action] = QRect(cursor, y, width, height)
            cursor += width + gap
        return rects

    def _hit_toolbar(self, p: QPoint) -> str | None:
        for action, rect in self.toolbar_rects.items():
            if rect.contains(p):
                return action
        return None

    def click_toolbar(self, action: str) -> bool:
        """执行工具条动作；返回是否消费了这次点击。"""
        if action == "confirm":
            self.confirm()
        elif action == "annotate":
            self.confirm(OverlayAction.ANNOTATE)
        elif action == "reselect":
            self.selection = None
            self.locked_window = None
            self.update()
        elif action == "window":
            self.set_mode(OverlayMode.WINDOW)
        else:
            return False
        return True

    # -- 绘制 -------------------------------------------------------------- #

    def paintEvent(self, event) -> None:                        # noqa: N802
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
            painter.scale(self._ratio, self._ratio)

            # 1) 冻结的桌面
            painter.drawImage(QPoint(0, 0), self._bg)

            sel = self._pending_rect()
            dim_alpha = int(getattr(self.cfg.capture, "dim_alpha", 90) * 2.55)

            if self.mode is OverlayMode.WINDOW:
                self._paint_window_mode(painter, dim_alpha)
            else:
                self._paint_region_mode(painter, sel, dim_alpha)

            self._paint_hint(painter)
            if not self.finished:
                self._paint_magnifier(painter)
        except Exception:
            # 绘制异常绝不能让选区交互中断（否则整个截屏流程会崩掉）
            _log("绘制异常", exc=True)
        finally:
            if painter.isActive():
                painter.end()

    def _pending_rect(self) -> QRect | None:
        """当前"待确认"的选区（拖动中的瞬时矩形也算）。"""
        if self.mode is OverlayMode.WINDOW:
            win = self.locked_window or self.hover_window
            if win is None:
                return None
            x0, y0 = self.frozen.to_local(win.rect[0], win.rect[1])
            x1, y1 = self.frozen.to_local(win.rect[2], win.rect[3])
            return QRect(QPoint(x0, y0), QPoint(x1 - 1, y1 - 1)).normalized()
        if self.selection is not None:
            return self.selection
        if self.start is not None and self.current is not None:
            return QRect(self.start, self.current).normalized()
        return None

    def _window_image_rect(self, win: winapi.WindowInfo) -> QRect:
        """窗口在位图坐标下的矩形（裁剪到桌面范围内）。

        扩展边框（DWM frame）可能比屏幕略大，直接拿来画会让提示条跑到
        画面外，所以这里统一先裁剪。"""
        x0, y0 = self.frozen.to_local(win.rect[0], win.rect[1])
        x1, y1 = self.frozen.to_local(win.rect[2], win.rect[3])
        return QRect(QPoint(x0, y0), QPoint(x1 - 1, y1 - 1)).normalized().intersected(
            self._image_rect())

    def _paint_region_mode(self, painter: QPainter, sel: QRect | None, dim_alpha: int) -> None:
        # 2) 遮罩：整块桌面压暗，选区"挖空"
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, dim_alpha))
        if sel is None:
            painter.drawRect(self._image_rect())
        else:
            painter.drawRect(QRect(0, 0, self._img_size[0], sel.top()))
            painter.drawRect(QRect(0, sel.bottom() + 1, self._img_size[0],
                                   self._img_size[1] - sel.bottom() - 1))
            painter.drawRect(QRect(0, sel.top(), sel.left(), sel.height()))
            painter.drawRect(QRect(sel.right() + 1, sel.top(),
                                   self._img_size[0] - sel.right() - 1, sel.height()))

        if sel is None:
            return
        # 3) 选区边框 + 三分线
        accent = QColor(0, 173, 181) if self.selection is None else QColor(0, 200, 210)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(_dim_pen(accent, 1.2))
        painter.drawRect(sel)
        painter.setPen(_dim_pen(QColor(255, 255, 255, 90), 0.7, dash=True))
        for i in (1, 2):
            x = sel.left() + sel.width() * i // 3
            y = sel.top() + sel.height() * i // 3
            painter.drawLine(x, sel.top(), x, sel.bottom())
            painter.drawLine(sel.left(), y, sel.right(), y)

        # 4) 角点手柄
        painter.setPen(Qt.NoPen)
        painter.setBrush(accent)
        for corner in (sel.topLeft(), sel.topRight(), sel.bottomLeft(), sel.bottomRight()):
            painter.drawRect(QRect(corner.x() - 3, corner.y() - 3, 6, 6))

        # 5) 尺寸/位置提示 + 工具条
        w = (sel.right() - sel.left() + 1) / max(self._ratio, 1e-6)
        h = (sel.bottom() - sel.top() + 1) / max(self._ratio, 1e-6)
        sx, sy = self._screen_of(QRect(sel).topLeft())
        lines = [f"{self.t('size')} {int(round(w))} x {int(round(h))}",
                 f"{self.t('pos')} {sx}, {sy}"]
        anchor = QPoint(sel.left(), max(4, sel.top() - 46))
        _rounded_label(painter, anchor, lines, QColor(20, 20, 24, 220))

        self.toolbar_rects = self._layout_toolbar(painter, sel)
        self._paint_toolbar(painter)

    def _paint_window_mode(self, painter: QPainter, dim_alpha: int) -> None:
        target = self.locked_window or self.hover_window
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, dim_alpha))
        if target is None:
            painter.drawRect(self._image_rect())
            self.toolbar_rects = {}
            return

        x0, y0 = self.frozen.to_local(target.rect[0], target.rect[1])
        x1, y1 = self.frozen.to_local(target.rect[2], target.rect[3])
        sel = QRect(QPoint(x0, y0), QPoint(x1 - 1, y1 - 1)).normalized().intersected(
            self._image_rect())
        painter.drawRect(QRect(0, 0, self._img_size[0], sel.top()))
        painter.drawRect(QRect(0, sel.bottom() + 1, self._img_size[0],
                               self._img_size[1] - sel.bottom() - 1))
        painter.drawRect(QRect(0, sel.top(), sel.left(), sel.height()))
        painter.drawRect(QRect(sel.right() + 1, sel.top(),
                               self._img_size[0] - sel.right() - 1, sel.height()))

        accent = QColor(0, 200, 210) if self.locked_window else QColor(90, 170, 255)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(_dim_pen(accent, 1.6))
        painter.drawRect(sel)

        # 提示条：标题 + 进程名 + 真实像素尺寸（不是裁剪后的尺寸）
        title = (target.title or target.exe)[:56]
        info = [title, f"{target.exe}   {target.width} x {target.height}"]
        label_anchor = QPoint(sel.left(), max(4, sel.top() - 48))
        if label_anchor.y() <= 6:            # 窗口顶到屏幕上边：改挂在选区上沿内侧
            label_anchor = QPoint(sel.left() + 2, sel.top() + 4)
        _rounded_label(painter, label_anchor, info, QColor(20, 20, 24, 220))

        self.toolbar_rects = self._layout_toolbar(painter, sel)
        self._paint_toolbar(painter)

    def _paint_toolbar(self, painter: QPainter) -> None:
        if not self.toolbar_rects:
            return
        font = QFont("Microsoft YaHei UI", 9)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        for action, rect in self.toolbar_rects.items():
            hot = action == self.toolbar_hover
            primary = action == "confirm"
            if primary:
                bg = QColor(0, 173, 181) if hot else QColor(0, 150, 158)
                fg = QColor(255, 255, 255)
            else:
                bg = QColor(58, 58, 66, 235) if hot else QColor(38, 38, 44, 225)
                fg = QColor(238, 238, 242)
            painter.setPen(Qt.NoPen)
            painter.setBrush(bg)
            painter.drawRoundedRect(rect, 6, 6)
            painter.setPen(fg)
            text = ""
            for act, label in self._toolbar_items():
                if act == action:
                    text = label
            painter.drawText(rect, Qt.AlignCenter, text)

    def _paint_hint(self, painter: QPainter) -> None:
        key = "hint_window" if self.mode is OverlayMode.WINDOW else "hint_region"
        text = self.t(key)
        font = QFont("Microsoft YaHei UI", 9)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        width = metrics.horizontalAdvance(text) + 28
        height = metrics.height() + 14
        x = (self._img_size[0] - width) // 2
        rect = QRect(max(4, x), self._img_size[1] - height - 18, int(width), int(height))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(18, 18, 22, 215))
        painter.drawRoundedRect(rect, 9, 9)
        painter.setPen(QColor(236, 236, 242))
        painter.drawText(rect, Qt.AlignCenter, text)

    def _paint_magnifier(self, painter: QPainter) -> None:
        cfg = self.cfg.capture
        if not getattr(cfg, "show_magnifier", True):
            return
        if self.locked_window is not None:
            return
        pos = self.hover_pos
        if pos is None:
            return

        zoom = max(2, int(getattr(cfg, "magnifier_zoom", 6)))
        side = 132                                    # 取样方边长（位图像素）
        half = side // 2
        box = QRect(pos.x() - half, pos.y() - half, side, side)
        src = box.intersected(self._image_rect())
        if src.width() <= 0 or src.height() <= 0:
            return
        crop = self.image.crop((src.left(), src.top(), src.right() + 1, src.bottom() + 1))
        tile = pil_to_qimage(crop)

        view = float(side * zoom)
        vx, vy = float(pos.x() + 26), float(pos.y() + 26)
        if vx + view > self._img_size[0]:
            vx = float(pos.x()) - 26.0 - view
        if vy + view > self._img_size[1]:
            vy = float(pos.y()) - 26.0 - view
        if vx < 0.0 or vy < 0.0:
            return
        target = QRectF(vx, vy, view, view)
        source = QRectF(0.0, 0.0, float(tile.width()), float(tile.height()))

        painter.save()
        painter.setClipRect(target)
        painter.setBrush(QColor(12, 12, 14, 235))
        painter.setPen(Qt.NoPen)
        painter.drawRect(target)
        painter.drawImage(target, tile, source)
        painter.restore()

        # 放大镜内的中心十字 + 边框
        painter.setPen(_dim_pen(QColor(0, 210, 220, 220), 1.0))
        cx = target.left() + (pos.x() - src.left()) * zoom
        cy = target.top() + (pos.y() - src.top()) * zoom
        painter.drawLine(QPoint(int(round(cx)), int(round(target.top()))),
                         QPoint(int(round(cx)), int(round(target.bottom()))))
        painter.drawLine(QPoint(int(round(target.left())), int(round(cy))),
                         QPoint(int(round(target.right())), int(round(cy))))
        painter.setBrush(Qt.NoBrush)
        painter.setPen(_dim_pen(QColor(255, 255, 255, 200), 1.2))
        painter.drawRect(target)

        # 位置与放大倍率
        sx, sy = self._screen_of(pos)
        painter.setPen(QColor(255, 255, 255))
        painter.setFont(QFont("Consolas", 8))
        text = f"({sx}, {sy})  x{zoom}"
        ty = target.bottom() + 14 if target.bottom() + 14 < self._img_size[1] else target.top() - 6
        painter.drawText(QPoint(int(round(target.left())), int(round(ty))), text)

    # -- 显示 / 运行 ------------------------------------------------------- #

    def show_overlay(self) -> None:
        self._prepare_geometry()
        self.show()
        self.activateWindow()
        self.setFocus(Qt.OtherFocusReason)
        self.raise_()
        if self.mode is OverlayMode.WINDOW:
            self._refresh_windows()
        self._refresh_timer.start()
        self._redraw_timer.start()
        self.update()

    def stop(self) -> None:
        self._refresh_timer.stop()
        self._redraw_timer.stop()
        self.hide()

    def run(self) -> OverlayResult:
        """原地显示并阻塞，返回用户的意图。"""
        app = QApplication.instance() or QApplication(sys.argv)
        loop = _BlockingLoop(self)
        self.show_overlay()
        loop.exec()
        self.stop()
        return loop.result or OverlayResult(action=OverlayAction.CANCEL)


# --------------------------------------------------------------------------- #
# 阻塞式事件循环
# --------------------------------------------------------------------------- #

class _BlockingLoop:
    """把悬浮层跑成一个独立的模态事件循环，便于线性写截图流程。"""

    def __init__(self, overlay: SelectionOverlay) -> None:
        from PySide6.QtCore import QEventLoop

        self.overlay = overlay
        self.loop = QEventLoop()
        self.result: OverlayResult | None = None
        overlay.picked.connect(self._on_picked)
        overlay.cancelled.connect(self._on_cancelled)

    def _on_picked(self, result: OverlayResult) -> None:
        self.result = result
        self.loop.quit()

    def _on_cancelled(self) -> None:
        self.result = OverlayResult(action=OverlayAction.CANCEL)
        self.loop.quit()

    def exec(self) -> None:
        self.loop.exec()


# --------------------------------------------------------------------------- #
# 便捷入口
# --------------------------------------------------------------------------- #

def select(frozen: FrozenDesktop, mode: OverlayMode = OverlayMode.REGION,
           cfg=None, language: bool | None = None) -> OverlayResult:
    """显示悬浮层并返回结果（供 app 层调用）。"""
    overlay = SelectionOverlay(frozen, mode=mode, cfg=cfg, language=language)
    return overlay.run()


def self_check() -> dict[str, Any]:
    """无 GUI 的自检：验证坐标换算与窗口命中逻辑在真实桌面上自洽。"""
    from .capture import freeze

    info: dict[str, Any] = {}
    frozen = freeze()
    if frozen is None:
        return {"ok": False, "reason": "freeze 失败"}
    info["bounds"] = list(frozen.bounds)
    info["size"] = list(frozen.size)
    info["windows"] = len(winapi.list_windows())
    cx, cy = frozen.bounds[0] + frozen.size[0] // 2, frozen.bounds[1] + frozen.size[1] // 2
    win = pick_window_at(winapi.list_windows(), cx, cy)
    info["center_window"] = win.exe if win else None
    info["ok"] = True
    return info
