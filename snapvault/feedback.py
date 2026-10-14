"""反馈：默默做完，只在真出事的时候开口。

设计原则（来自明确的产品要求）
------------------------------
「这个设计思路主要还是轻量化为主，就是每次执行时默默的完成任务,
不需要老土的弹窗」—— 所以**成功必须静默，只有失败才允许打断**。

原来的做法是 :meth:`QSystemTrayIcon.showMessage`，它有三宗罪：

1. **会进通知中心**，攒一堆历史，用户回头看到几十条"已归档"；
2. **会被专注助手 / 勿扰模式吞掉** —— 成功时无所谓，但失败消息也一起没了，
   这才是真问题；
3. **样式被系统写死**，塞不下结构化的信息，现在硬挤了 4 行纯文本，很难看。

三级设计
--------
* **L0 静默**（默认，成功走这条）：托盘图标做一个 320ms 的徽标脉冲 +
  tooltip 实时更新。**不出现任何窗口**。如果图库开着，新卡片自身淡入一圈
  强调色描边 —— 反馈发生在"东西出现的地方"，而不是屏幕角落的弹窗里。
* **L1 自绘浮层**（失败 / 长任务结束）：右下角滑入一张卡片。
  核心技巧是 :attr:`Qt.WA_ShowWithoutActivating` + :attr:`Qt.WindowDoesNotAcceptFocus`
  —— **它浮在最上面，但绝不抢焦点**，你正在打的字不会断。
  毛玻璃底 + 200ms 滑入 + 自动消失。
* **L2 系统通知**（可选，默认关）：真要原生弹窗时走 `win11toast`（MIT），
  而不是 Qt 的 showMessage，至少样式和 Windows 一致。

自绘而不是用 QSystemTrayIcon.showMessage，是为了"能控制"：能控制圆角、
能控制几秒消失、能控制不抢焦点、能控制不进通知中心。
"""

from __future__ import annotations

import sys
from typing import Callable

from PySide6.QtCore import (QEasingCurve, QObject, QPoint, QPropertyAnimation,
                            QRect, QSize, Qt, QTimer, Signal)
from PySide6.QtGui import (QColor, QFont, QFontMetrics, QIcon, QPainter,
                           QPainterPath, QPixmap)
from PySide6.QtWidgets import QApplication, QWidget

from . import icons, theme

#: 浮层宽度。322 = 320 内容宽 + 2 描边，够放"分类名（92%）+ 一行标签"不换行。
TOAST_W = 322

#: 自动消失延时（毫秒）。错误给久一点，因为要读；成功类一闪而过就够。
DURATION = {"ok": 2600, "info": 3200, "warn": 4200, "err": 6000}

#: 浮层距离屏幕右下角的边距。
MARGIN = 16


# --------------------------------------------------------------------------- #
# L0：托盘图标徽标脉冲
# --------------------------------------------------------------------------- #

class TrayPulse(QObject):
    """让托盘图标"呼吸一下"。

    这是"成功了但我不想被打扰"的唯一反馈通道：图标上浮出一个小圆点，
    从 0 涨到满、再退回去，全程 320ms。不进通知中心、不抢焦点、不弹窗。

    实现上只能靠换图标帧 —— Qt 没有在托盘图标上做动画的接口。
    帧数刻意压到 6 帧：再密也看不出差别，但每帧都要过一次 Windows 的
    托盘 IPC，太多帧反而会卡。
    """

    FRAMES = 6
    INTERVAL = 54                      # 6 × 54ms ≈ 324ms

    def __init__(self, tray, base_icon: Callable[[], QIcon], parent=None) -> None:
        super().__init__(parent)
        self.tray = tray
        self._base = base_icon
        self._frame = 0
        self._color = theme.TOKENS["dark"]["ok"]
        self._timer = QTimer(self)
        self._timer.setInterval(self.INTERVAL)
        self._timer.timeout.connect(self._tick)

    def pulse(self, color: str | None = None) -> None:
        """弹一下。`color` 传强调色令牌值，默认成功绿。"""
        if self.tray is None:
            return
        self._color = color or theme.TOKENS["dark"]["ok"]
        self._frame = 0
        self._timer.start()

    def _tick(self) -> None:
        if self._frame >= self.FRAMES * 2:
            self._timer.stop()
            self.tray.setIcon(self._base())
            return
        # 前半程涨、后半程退 —— 一个完整的呼吸。
        n = self._frame if self._frame < self.FRAMES else self.FRAMES * 2 - self._frame
        ratio = n / float(self.FRAMES)
        self.tray.setIcon(self._frame_icon(ratio))
        self._frame += 1

    def _frame_icon(self, ratio: float) -> QIcon:
        pix = self._base().pixmap(64, 64)
        p = QPainter(pix)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = 7 + 4 * ratio
        cx, cy = 64 - 11, 11
        dot = QColor(self._color)
        dot.setAlpha(int(255 * (0.35 + 0.65 * ratio)))
        p.setPen(Qt.NoPen)
        # 先铺一圈同色低透明度的光晕，再点实心 —— 小圆点在 16px 托盘里才看得见。
        halo = QColor(self._color)
        halo.setAlpha(int(70 * ratio))
        p.setBrush(halo)
        p.drawEllipse(QPoint(cx, cy), int(r + 4), int(r + 4))
        p.setBrush(dot)
        p.drawEllipse(QPoint(cx, cy), int(r * 0.62), int(r * 0.62))
        p.end()
        return QIcon(pix)


# --------------------------------------------------------------------------- #
# L1：右下角浮层
# --------------------------------------------------------------------------- #

class Toast(QWidget):
    """右下角滑入的浮层。

    **绝不能抢焦点**是它的立身之本 —— 否则用户正在别处打字，浮层一冒出来
    输入就断了，这比弹窗还烦。三个 flag 一起上才保险：

    * ``WA_ShowWithoutActivating``：show() 时不去激活自己；
    * ``WindowDoesNotAcceptFocus``：压根不接受焦点；
    * ``NoFocus``：内部控件也不接受 Tab 焦点。

    另外它是 ``Qt.Tool`` 而不是 ``Qt.Popup`` —— Tool 不会进任务栏，
    也不会像 Popup 那样一点外面就关掉。
    """

    dismissed = Signal()

    def __init__(self, *, kind: str = "info", title: str = "",
                 body: str = "", icon_name: str = "info",
                 progress: int | None = None) -> None:
        super().__init__(None,
                         Qt.Tool | Qt.FramelessWindowHint
                         | Qt.WindowStaysOnTopHint
                         | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.NoFocus)
        self.setFixedWidth(TOAST_W)
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)

        self.kind = kind
        self.title = title
        self.body = body
        self.icon_name = icon_name
        self.progress = progress
        self._slide: QPropertyAnimation | None = None
        self._fade: QPropertyAnimation | None = None
        self._closer: QTimer | None = None
        self._on_click: Callable[[], None] | None = None
        self._dismissed = False

        self._theme = "dark"
        self.set_theme(theme_guess())

    # -- 主题 ------------------------------------------------------------- #

    def set_theme(self, name: str) -> None:
        self._theme = "light" if name == "light" else "dark"
        self.update()

    # -- 尺寸 ------------------------------------------------------------- #

    def _metrics(self) -> tuple[QFontMetrics, QFontMetrics]:
        title_fm = QFontMetrics(theme.ui_font(14, QFont.DemiBold))
        body_fm = QFontMetrics(theme.ui_font(12, QFont.Normal))
        return title_fm, body_fm

    def height_for_width(self) -> int:
        """按内容算高度：标题一行、正文最多两行、进度条可选。"""
        title_fm, body_fm = self._metrics()
        pad = theme.S3
        avail = TOAST_W - 4 - pad * 2 - 20 - theme.S2      # 扣掉图标和间距
        h = pad * 2 + title_fm.height()
        if self.body:
            _first, rest = _split_two_lines(self.body, body_fm, avail)
            h += theme.S1 + body_fm.height() * (2 if rest else 1)
        if self.progress is not None:
            h += theme.S2 + 6
        return max(56, h)

    # -- 内容 ------------------------------------------------------------- #

    def paintEvent(self, _event) -> None:              # noqa: N802
        t = theme.tokens(self._theme)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        # 卡片四周留 2px，给浅色主题的软投影用（不然会被窗口边界裁掉）。
        card = QRect(2, 2, self.width() - 4, self.height() - 4)

        # 浅色主题：手画两层递减的软投影（Qt 的 QSS 没有 box-shadow，
        # 而 QGraphicsDropShadowEffect 在无边框顶层窗口上会被裁掉）。
        # 深色主题：不画投影，靠更亮的面做层级 —— Fluent 的 tonal elevation。
        if self._theme == "light":
            for i, a in enumerate((14, 24)):
                p.setPen(Qt.NoPen)
                p.setBrush(theme.alpha("#101828", a))
                p.drawRoundedRect(card.adjusted(-i, -i + 1, i, i + 1),
                                  theme.R_CARD + i, theme.R_CARD + i)
            bg = t["surface"]
            stroke = t["stroke"]
        else:
            bg = t["surface2"]
            stroke = theme.alpha(t["stroke"], 210)

        p.setPen(Qt.NoPen)
        p.setBrush(QColor(bg))
        p.drawRoundedRect(card, theme.R_CARD, theme.R_CARD)

        # 危险态给一整圈红边，这是"必须看见"的视觉承诺。
        p.setBrush(Qt.NoBrush)
        pen = p.pen()
        pen.setWidthF(1.0)
        pen.setColor(QColor(t["danger"] if self.kind == "err" else stroke))
        p.setPen(pen)
        p.drawRoundedRect(card, theme.R_CARD, theme.R_CARD)

        # --- 左图标 ---
        accent = {"ok": t["ok"], "err": t["danger"],
                  "warn": t["warn"], "info": t["accent"]}.get(self.kind, t["accent"])
        irect = QRect(card.left() + theme.S3, card.top() + theme.S3, 20, 20)
        icons.paint(p, self.icon_name, irect, accent, width=2.0)

        # --- 文字 ---
        text_left = irect.right() + theme.S2
        right_pad = theme.S3
        avail = card.right() - right_pad - text_left

        title_fm, body_fm = self._metrics()
        p.setFont(theme.ui_font(14, QFont.DemiBold))
        p.setPen(QColor(t["fg1"]))
        p.drawText(QRect(text_left, card.top() + theme.S3,
                         avail, title_fm.height()),
                   Qt.AlignLeft | Qt.AlignVCenter,
                   title_fm.elidedText(self.title, Qt.ElideRight, avail))

        if self.body:
            p.setFont(theme.ui_font(12, QFont.Normal))
            p.setPen(QColor(t["fg2"]))
            top = card.top() + theme.S3 + title_fm.height() + theme.S1
            # 两行截断：Qt 没有多行省略，手动按宽度切两段。
            first, rest = _split_two_lines(self.body, body_fm, avail)
            p.drawText(QRect(text_left, top, avail, body_fm.height()),
                       Qt.AlignLeft | Qt.AlignVCenter, first)
            if rest:
                p.drawText(QRect(text_left, top + body_fm.height(), avail,
                                 body_fm.height()),
                           Qt.AlignLeft | Qt.AlignVCenter,
                           body_fm.elidedText(rest.replace("\n", " "),
                                              Qt.ElideRight, avail))

        # --- 进度条（长任务用） ---
        if self.progress is not None:
            bar = QRect(text_left, card.bottom() - theme.S2 - 3,
                        avail, 3)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(t["surface3"]))
            p.drawRoundedRect(bar, 2, 2)
            p.setBrush(QColor(accent))
            done = QRect(bar.left(), bar.top(),
                         max(3, int(bar.width() * max(0.0, min(1.0, self.progress)))),
                         bar.height())
            p.drawRoundedRect(done, 2, 2)

        p.end()

    # -- 交互 ------------------------------------------------------------- #

    def on_click(self, callback: Callable[[], None]) -> "Toast":
        """点一下浮层做点什么（通常是"打开图库"）。"""
        self._on_click = callback
        return self

    def mousePressEvent(self, event) -> None:          # noqa: N802
        if event.button() == Qt.LeftButton:
            if callable(self._on_click):
                self._on_click()
            self.dismiss()
        event.accept()

    # -- 出场 / 退场 ------------------------------------------------------ #

    def popup(self, screen_rect: QRect, offset_y: int = 0,
              duration: int | None = None) -> None:
        """滑入。`screen_rect` 通常是主屏的 availableGeometry 减去任务栏。"""
        self.setFixedHeight(self.height_for_width())
        end_x = screen_rect.right() - self.width() - MARGIN + 1
        end_y = screen_rect.bottom() - self.height() - MARGIN + 1 - offset_y
        self.move(end_x + 16, end_y)
        self.setWindowOpacity(0.0)
        self.show()
        self.raise_()

        self._slide = QPropertyAnimation(self, b"pos", self)
        self._slide.setDuration(theme.MOTION["normal"])
        self._slide.setEasingCurve(QEasingCurve.OutCubic)
        self._slide.setStartValue(QPoint(end_x + 16, end_y))
        self._slide.setEndValue(QPoint(end_x, end_y))
        self._slide.start()

        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(theme.MOTION["normal"])
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.start()

        # 「正在处理中」不自动消失 —— 它要一直待到自己被 update() 掉。
        if self.progress is not None:
            return
        self._closer = QTimer(self)
        self._closer.setSingleShot(True)
        self._closer.timeout.connect(self.dismiss)
        self._closer.start(duration or DURATION.get(self.kind, 3000))

    def update_progress(self, title: str, body: str = "",
                        progress: float | None = None) -> None:
        """原地改内容，不重放动画 —— 进度更新要是每次都滑进来，那才叫吵。"""
        self.title = title
        if body:
            self.body = body
        if progress is not None:
            self.progress = progress
        self.setFixedHeight(self.height_for_width())
        self.update()

    def dismiss(self) -> None:
        """淡出后销毁。重复调用安全。"""
        if self._dismissed:
            return
        self._dismissed = True
        if self._closer is not None:
            self._closer.stop()
        out = QPropertyAnimation(self, b"windowOpacity", self)
        out.setDuration(theme.MOTION["fast"])
        out.setStartValue(self.windowOpacity())
        out.setEndValue(0.0)
        out.finished.connect(self._finish)
        self._out = out
        out.start()

    def _finish(self) -> None:
        self.dismissed.emit()
        self.hide()
        self.deleteLater()


def _split_two_lines(text: str, fm: QFontMetrics, width: int) -> tuple[str, str]:
    """把一段文字切成"塞得进 width 的两行"。返回 (第一行, 剩下的)。

    Qt 没有内建的多行省略号，自己按字宽累加是唯一稳的做法。
    """
    if fm.horizontalAdvance(text) <= width:
        return text, ""
    acc = ""
    cut = 0
    for ch in text:
        if fm.horizontalAdvance(acc + ch) > width:
            break
        acc += ch
        cut += 1
    return acc, text[cut:]


# --------------------------------------------------------------------------- #
# 浮层管家：保证同时最多一张，且永远从右下角往上叠
# --------------------------------------------------------------------------- #

class ToastManager(QObject):
    """管理浮层的显示 / 替换 / 关闭。

    刻意**只保留一张**：需求是"轻量化、别打扰"，那就不该有"消息堆成一片"
    的场面。新消息直接顶掉旧的（旧的那条本来就是过期的进度/提示）。
    """

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._current: Toast | None = None

    @property
    def current(self) -> Toast | None:
        return self._current

    def close_current(self) -> None:
        if self._current is not None:
            self._current.dismiss()
            self._current = None

    def show(self, *, kind: str, title: str, body: str = "",
             icon_name: str = "info", duration: int | None = None,
             on_click: Callable[[], None] | None = None) -> Toast:
        self.close_current()
        toast = Toast(kind=kind, title=title, body=body, icon_name=icon_name)
        if callable(on_click):
            toast.on_click(on_click)
        toast.dismissed.connect(lambda: self._forget(toast))
        self._current = toast
        toast.popup(_screen_rect())
        return toast

    def busy(self, *, title: str, body: str = "") -> Toast:
        """开一张"处理中"浮层（进度条 0，不自动消失）。"""
        self.close_current()
        toast = Toast(kind="info", title=title, body=body,
                      icon_name="loader-circle", progress=0.0)
        toast.dismissed.connect(lambda: self._forget(toast))
        self._current = toast
        toast.popup(_screen_rect())
        return toast

    def _forget(self, toast: Toast) -> None:
        if self._current is toast:
            self._current = None


def _screen_rect() -> QRect:
    """主屏可用区域（已扣掉任务栏）。浮层贴着它右下角放。"""
    app = QApplication.instance()
    screen = app.primaryScreen() if app is not None else None
    if screen is None:
        return QRect(0, 0, 1280, 720)
    return screen.availableGeometry()


# --------------------------------------------------------------------------- #
# 主题探测（浮层可能在主窗口还没建好时就要显示）
# --------------------------------------------------------------------------- #

def theme_guess() -> str:
    """猜当前主题。托盘程序里拿不到主窗口时用它兜底。"""
    name = getattr(theme_guess, "_cached", None)
    return name or "dark"


def set_theme_guess(name: str) -> None:
    theme_guess._cached = "light" if name == "light" else "dark"   # noqa: SLF001


# --------------------------------------------------------------------------- #
# 统一入口
# --------------------------------------------------------------------------- #

class FeedbackHub:
    """把三级反馈收成一个门面，调用方只说"发生了什么"。

    调用方（归档流水线）不需要知道"成功走静默、失败走浮层"这套规则 ——
    规则在这里，改规则只改这里。
    """

    def __init__(self, tray=None, base_icon: Callable[[], QIcon] | None = None,
                 toasts: ToastManager | None = None) -> None:
        self.tray = tray
        self.toasts = toasts or ToastManager()
        self.pulse = (TrayPulse(tray, base_icon) if tray is not None
                      and base_icon is not None else None)

    # -- 成功：静默 ------------------------------------------------------- #

    def archived(self, filename: str, category_label: str,
                 confidence: float = 0.0, *, quiet_tooltip: str = "") -> None:
        """一张截图处理完了。

        **不弹任何东西。** 托盘图标脉冲一下，tooltip 更新成最新一张的信息。
        如果图库开着，卡片自己会亮一下（由图库负责，见 ``flash`` 参数）。
        """
        if self.pulse is not None:
            self.pulse.pulse(theme.TOKENS["dark"]["ok"])
        if self.tray is not None and quiet_tooltip:
            self.tray.setToolTip(quiet_tooltip)

    # -- 失败：必须看见 --------------------------------------------------- #

    def failed(self, reason: str, *, where: str = "") -> None:
        """失败走浮层。

        这是**唯一允许打断**的情况：不弹就等于把错误吞了，
        用户会以为"它是不是没在跑"。
        """
        self.toasts.show(kind="err", title=where or "这一步没成功",
                         body=reason, icon_name="circle-alert")

    def warn(self, title: str, body: str = "") -> None:
        self.toasts.show(kind="warn", title=title, body=body,
                         icon_name="circle-alert")

    def info(self, title: str, body: str = "") -> None:
        self.toasts.show(kind="info", title=title, body=body, icon_name="info")

    # -- 长任务 ----------------------------------------------------------- #

    def start_batch(self, total: int) -> Toast:
        """长任务开始：开一张带进度的浮层。点它打开图库。"""
        task = self.toasts.busy(title=f"正在处理 0/{total} 张",
                                body="AI 命名与分类进行中…")
        return task

    def batch_progress(self, toast: Toast | None, done: int,
                       total: int) -> None:
        if toast is None:
            return
        toast.update_progress(f"正在处理 {done}/{total} 张",
                              progress=(done / total) if total else 0.0)

    def batch_done(self, toast: Toast | None, done: int, failed: int = 0) -> None:
        """长任务结束。

        有失败就转成错误浮层留着；全成功则两秒后自己消失 —— 长任务该有个
        收尾交代，但它仍然不抢焦点、不进通知中心。
        """
        if toast is not None:
            toast.dismiss()
        if failed:
            self.toasts.show(kind="err", title=f"完成 {done} 张，{failed} 张没成功",
                             body="点这里打开图库看看是哪几张",
                             icon_name="circle-alert", duration=7200)
        else:
            self.toasts.show(kind="ok", title=f"已处理 {done} 张",
                             body="命名与分类完成", icon_name="check",
                             duration=2400)
