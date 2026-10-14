"""图库主界面：缩略图网格 + 检索 + 分类/标签筛选 + 详情预览。

设计要点
--------
* 数据全部来自 :class:`snapvault.vault.Vault`，界面不直接碰 SQLite；
* 缩略图在后台线程池里解码（``_probe`` 实测：2500×1700 的 PNG 全尺寸解码要
  200ms 以上，主线程解码 20 张就会明显卡顿），主线程只做绘制；
* 搜索框直接复用 :meth:`Vault.search` 的查询语法（``cat:`` / ``tag:`` / ``day:``），
  所以图库的检索能力和 CLI 完全一致。
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections import OrderedDict
from datetime import date, datetime, timedelta
from pathlib import Path

from PySide6.QtCore import (QEasingCurve, QEvent, QObject, QPoint, QPropertyAnimation,
                            QRect, QRunnable, QSize, Qt, QThreadPool, QTimer,
                            QVariantAnimation, Property, Signal)
from PySide6.QtGui import (QAction, QColor, QFont, QFontMetrics, QIcon, QImage,
                           QImageReader, QKeySequence, QPainter, QPainterPath,
                           QPalette, QPen, QPixmap)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox,
                               QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
                               QMenu, QMessageBox, QPlainTextEdit, QPushButton,
                               QScrollArea, QSizePolicy, QSplitter, QStyle,
                               QStyledItemDelegate, QStyleOptionViewItem, QToolBar,
                               QToolButton, QVBoxLayout, QWidget)

from . import icons, theme
from .config import Config
from .naming import normalize_tags
from .vault import ShotRecord, Vault

# 分类不再用颜色区分，改用图标 —— 见 theme.CATEGORY_ICONS 的说明。
# 这里保留一个别名，免得调用方到处 import theme。
category_icon = theme.category_icon


# --------------------------------------------------------------------------- #
# 后台缩略图加载
# --------------------------------------------------------------------------- #

class _ThumbSignals(QObject):
    done = Signal(int, object)          # shot_id, QImage | None


class ThumbnailLoader(QObject):
    """线程池缩略图加载器：优先读 vault 生成的小图，回退解码原图。

    同一张图同时只会有一个请求在飞，避免快速滚动时重复解码。
    """

    loaded = Signal(int, object)        # shot_id, QImage

    def __init__(self, vault: Vault, pool: QThreadPool, size: int = 384,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.vault = vault
        self.pool = pool
        self.size = max(64, int(size))
        self._inflight: set[int] = set()
        self._signals = _ThumbSignals(self)
        self._signals.done.connect(self._on_done)

    def request(self, shot_id: int, thumb_path: Path | None, full_path: Path) -> None:
        if shot_id in self._inflight:
            return
        candidates = [p for p in (thumb_path, full_path) if p is not None]
        if not candidates:
            return
        self._inflight.add(shot_id)
        signals, size = self._signals, self.size

        def work() -> None:
            img = QImage()
            for candidate in candidates:
                try:
                    if not Path(candidate).exists():
                        continue
                    reader = QImageReader(str(candidate))
                    reader.setAutoTransform(True)
                    src = reader.size()
                    if src.isValid() and src.width() > size:
                        ratio = size / float(src.width())
                        reader.setScaledSize(QSize(size, max(1, int(src.height() * ratio))))
                    img = reader.read()
                except Exception:
                    img = QImage()
                if not img.isNull():
                    break
            if not img.isNull() and img.width() > size:
                img = img.scaledToWidth(size, Qt.SmoothTransformation)
            signals.done.emit(shot_id, None if img.isNull() else img)

        self.pool.start(_Runnable(work))

    def _on_done(self, shot_id: int, img: object) -> None:
        self._inflight.discard(shot_id)
        if img is not None:
            self.loaded.emit(shot_id, img)

    def clear(self) -> None:
        self._inflight.clear()


class _Runnable(QRunnable):
    """把任意可调用对象包成 QRunnable。"""

    def __init__(self, fn) -> None:
        super().__init__()
        self._fn = fn
        self.setAutoDelete(True)

    def run(self) -> None:                  # pragma: no cover - 线程内执行
        try:
            self._fn()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# 缩略图委托：自己画，保证暗色主题下的卡片效果一致
# --------------------------------------------------------------------------- #

class ThumbDelegate(QStyledItemDelegate):
    """绘制「缩略图 + 两行标题 + 图标化 meta」的卡片。

    改版前后的区别，逐条都能说出为什么：

    卡片圆角      10px（比外层容器的 8 还大）     8px，与外层一致（嵌套 8→6→4）
    选中态        浅色下白字压浅绿底，等于看不见  强调色低透明填充 + 一圈强调色边，
                                                 文字色永远跟着 fg1 走
    标题          硬截到 26 个字符                按像素宽度两行省略，不出现半截字
    meta 行       10 种分类色的小字               分类**图标** + 分类名 + 时间，
                                                 统一 fg3 灰，只有图标带强调色
    占位符        一个「…」                       image-off 图标，语义明确
    收藏          「★」字符（字体里常显成方框）   Lucide star 图标
    新归档        无反馈                          一圈强调色描边 700ms 淡出
    """

    #: 新归档卡片的强调色描边持续时间见 theme.MOTION["flash"]。

    #: 缩略图边长的上下界。按视口宽度反算列数时用。
    MIN_THUMB = 108
    MAX_THUMB = 240
    #: 反算列数时的"目标边长"。取 190：在这个尺寸下 3–5 列都好看。
    TARGET_THUMB = 190

    #: 缩略图舞台的宽高比。
    #:
    #: 取 1.6（16:10）—— 图库里绝大多数是 Windows 截图，实测这批的比例是
    #: 2544×1620 ≈ 1.57、2520×1608 ≈ 1.567，1.6 基本贴合。
    #: 改版前舞台是正方形，16:10 的图上下各空掉约 37% 的高度，
    #: 而且空多少随每张图自己的比例变 —— 一屏看过去就是"高矮参差"。
    STAGE_ASPECT = 1.6

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.thumb = self.TARGET_THUMB
        self.padding = theme.S2
        self._theme = "dark"
        #: 目标边长。默认 190，可被 cfg.ui.thumb_size 覆盖（设置面板里改）。
        self.target_thumb = self.TARGET_THUMB
        #: 缩略图查询回调，窗口装配时注入 ``self._thumbs.get``（见上）。
        self.image_of = lambda _id: None
        #: 正在"亮一下"的卡片：shot_id + 0.0–1.0 的强度
        self._flash_id: int | None = None
        self._flash: QVariantAnimation | None = None
        self._flash_value = 0.0
        #: 按下回弹：正在按住的 shot_id 与 0.0–1.0 的缩放进度。
        self._press_id: int | None = None
        self._press_k = 0.0
        #: 选中高亮过渡：刚进入 / 刚离开选中的那几张，以及各自的 0..1 进度。
        self._sel_in_ids: set[int] = set()
        self._sel_out_ids: set[int] = set()
        self._sel_in_k = 1.0
        self._sel_out_k = 1.0
        self._sel_anim: QVariantAnimation | None = None

        # --- 绘制缓存 ---------------------------------------------------- #
        # 这些缓存是"滚起来卡不卡"的全部来源。改版前 paint() 每次都现做：
        #   QFont x2 + QFontMetrics x2（每帧 48 次构造）
        #   icons.paint() —— 也就是每颗图标都重新跑一遍 QSvgRenderer
        #                    （每帧 288 次 render）
        #   _wrap() —— 逐字 horizontalAdvance，还是 O(n²) 的写法
        # 24 张可见卡片下，一帧全视口重绘要 10.5 ms，其中九成是这些。
        # 缓存之后只剩纯绘制动作。
        self._fm_key = None
        self._title_font: QFont | None = None
        self._meta_font: QFont | None = None
        self._title_fm: QFontMetrics | None = None
        self._meta_fm: QFontMetrics | None = None
        self._icon_cache: dict = {}
        self._wrap_cache: dict = {}
        #: 缩略图舞台的位图缓存：(shot_id, 宽, 高, dpr) → QPixmap。
        #: 免掉每帧每卡的 ``QImage.scaled()``。
        self._stage_cache: dict = {}

    def _metrics(self) -> tuple[QFont, QFontMetrics, QFont, QFontMetrics]:
        """(标题字体, 标题度量, meta 字体, meta 度量)，按主题缓存。

        ``theme.ui_font()`` 每次调用都要解析字重、设字体族、设 hinting，
        是个实打实的构造过程；``QFontMetrics`` 更要向系统要一次度量。
        而重绘时这两个东西每张卡片都要用 —— 不缓存就是白烧 CPU。
        """
        if self._fm_key != self._theme:
            self._title_font = theme.ui_font(12, QFont.DemiBold)
            self._meta_font = theme.ui_font(12, QFont.Normal)
            self._title_fm = QFontMetrics(self._title_font)
            self._meta_fm = QFontMetrics(self._meta_font)
            self._fm_key = self._theme
        return (self._title_font, self._title_fm,
                self._meta_font, self._meta_fm)

    def _icon(self, painter: QPainter, name: str, color: str,
              size: int, width: bool = 2.0) -> QPixmap:
        """取一颗已栅格化的图标。``icons.paint()`` 是每次都重跑 SVG 渲染器，
        这里换成缓存好的位图 —— 高分屏按 painter 的 dpr 单独缓存一份。"""
        dpr = 2.0
        dev = painter.device()
        if dev is not None:
            try:
                dpr = float(dev.devicePixelRatioF()) or 1.0
            except Exception:                       # noqa: BLE001
                pass
        key = (name, color, int(size), float(width), dpr)
        pm = self._icon_cache.get(key)
        if pm is None:
            pm = icons.pixmap(name, color, int(size), float(width), dpr)
            self._icon_cache[key] = pm
        return pm

    def _stage_pixmap(self, shot_id: int, img: QImage, box: QSize) -> QPixmap:
        """把解码好的 QImage 按舞台尺寸缩放一次，之后每帧直接贴位图。

        改版前是每一帧、每张卡片都现跑一次 ``QImage.scaled()`` ——
        那是一次完整的新分配 + 重采样。现在按 ``(shot_id, 舞台尺寸)`` 缓存，
        同一张卡片在尺寸不变的前提下只算一次；窗口拉宽 / 换主题会清缓存
        （见 ``set_theme`` 与 ``invalidate_stage_cache``）。

        缓存用 FIFO 上限兜底：``_thumbs`` 的 LRU 只有 120 张，
        尺寸最多几种，160 条足够覆盖一屏还多。
        """
        dpr = 1.0
        try:
            app = QApplication.instance()
            if app is not None:
                scr = app.primaryScreen()
                if scr is not None:
                    dpr = float(scr.devicePixelRatio()) or 1.0
        except Exception:                           # noqa: BLE001
            dpr = 1.0
        key = (int(shot_id), box.width(), box.height(), dpr)
        pm = self._stage_cache.get(key)
        if pm is not None:
            return pm
        scaled = img.scaled(box, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        pm = QPixmap.fromImage(scaled)
        if len(self._stage_cache) >= 160:
            self._stage_cache.clear()
        self._stage_cache[key] = pm
        return pm

    def invalidate_stage_cache(self) -> None:
        """卡片边长 / 缩放比变了，缓存的位图尺寸就作废了。"""
        self._stage_cache.clear()

    def fit_columns(self, viewport_w: int) -> None:
        """按视口宽度反算缩略图边长，让每一行刚好排满。

        固定边长在宽窗口下会在右侧留出将近一列宽的空白 ——
        那种"右边缺一块"的锯齿边正是廉价感的来源。做法是把列数从 1 开始往上试，
        留下边长落在 ``[MIN_THUMB, MAX_THUMB]`` 里的那些组合，
        再挑一个**最接近目标边长**的。因为边长是整除摊下来的，
        任何一种列数的右缘余量都小于列数本身（几像素），看不出锯齿。

        早先是 ``round(视口 / 单位宽)`` 估列数 —— ``round(4.5)`` 在 Python 里
        是 4 而不是 5，再被 MAX_THUMB 一钳，1600px 宽的窗口下右侧空出 44px。
        现在改成直接枚举比较，没有取整歧义。
        """
        if viewport_w <= 0:
            return
        gap = theme.S1
        best_thumb, best_diff = None, None
        for cols in range(1, 65):
            thumb = (viewport_w - (cols - 1) * gap) // cols - self.padding * 2
            if thumb < self.MIN_THUMB:
                break                       # 再加列就放不下了
            if thumb > self.MAX_THUMB:
                continue                    # 这一列太宽，往下加列
            diff = abs(thumb - self.target_thumb)
            if best_diff is None or diff < best_diff:
                best_diff, best_thumb = diff, thumb
        if best_thumb is None:              # 视口极窄：一列都摆不下 MAX
            best_thumb = max(self.MIN_THUMB, viewport_w - self.padding * 2)
        self.thumb = best_thumb

    # -- 缩略图来源 --------------------------------------------------------- #
    # 图片不存进 item：早期版本 ``item.setData(UserRole+2, img)``，结果是
    # 滚过的每一张都被 item 永久持有，_thumbs_limit 的 LRU 完全被绕过
    # —— 缓存上限形同虚设，滚一遍图库照样几百 MB。
    # 现在委托通过 ``image_of`` **现查**窗口的 LRU 缓存，item 里只有 id 和记录。
    # 注意必须是实例属性：类属性上的普通函数会被绑定 self，签名就错了。

    # -- 主题 -------------------------------------------------------------- #

    def set_theme(self, name: str) -> None:
        """lazy 加载期间切换主题时，卡片配色也要跟着变。

        改版后这里不再缓存色值 —— 一律现查 :mod:`snapvault.theme`。
        之前把 6 个颜色缓存成实例属性，结果就是"改了令牌忘了改缓存"这类不一致，
        深色卡片残留在浅色窗口上。

        **但绘制缓存必须显式作废**：字体度量跟着主题走（浅色主题的字号/字重
        可能不同），图标位图是按主题色栅格化的 —— 不清理就会把深色的图标
        留在浅色界面上，正好是上面那段注释想避免的事。
        """
        self._theme = "light" if name == "light" else "dark"
        self._fm_key = None
        self._icon_cache.clear()
        self._wrap_cache.clear()
        # 舞台位图是按当前尺寸和缩放着色的，换主题后不能留。
        self._stage_cache.clear()

    def _t(self) -> dict:
        return theme.tokens(self._theme)

    # -- 新归档高亮 -------------------------------------------------------- #

    def flash(self, shot_id: int) -> None:
        """让某张卡片亮一下。


        这是"默默完成任务"的反馈落点 —— 不弹窗，而是**在东西出现的地方**
        给一个 700ms 的强调色描边。用户余光就能看到"刚那张进去了"。
        只有真正的新归档才触发，所以不会被刷成噪音。
        """
        if self._flash is not None:
            self._flash.stop()
        self._flash_id = shot_id
        anim = QVariantAnimation(self)
        anim.setStartValue(1.0)
        anim.setEndValue(0.0)
        anim.setDuration(theme.MOTION["flash"])
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.valueChanged.connect(self._on_flash_tick)
        anim.finished.connect(self._on_flash_done)
        self._flash = anim
        anim.start()

    def _on_flash_tick(self, value) -> None:
        self._flash_value = float(value)
        self._repaint_view()

    def _on_flash_done(self) -> None:
        self._flash_id = None
        self._flash_value = 0.0
        self._repaint_view()

    def _repaint_view(self) -> None:
        view = self.parent()
        if view is not None and hasattr(view, "viewport"):
            view.viewport().update()

    # -- 按下回弹 ---------------------------------------------------------- #

    #: 按下时整张卡片缩多少。3% 是"看得出、但不夸张"的量级 ——
    #: 再大就会明显挤到相邻卡片，从"跟手"变成廉价玩具感。
    PRESS_SCALE = 0.03

    def press(self, shot_id: int | None, on: bool) -> None:
        """按下/松开卡片时的缩放反馈。

        用户选的方向是"做跟手感"。**按下完全没有形变**，卡片就只是一张贴图；
        给一点点缩放，手感立刻从"点了张图"变成"按下去一个东西"。

        时长走 ``MOTION["press"]``（110ms）而不是 fast/normal ——
        这类反馈比"看清"更靠时间感，超过 ~120ms 就从"跟手"变成"迟钝"。
        """
        if shot_id is None:
            return
        self._press_id = shot_id
        if not theme.animating():
            self._press_k = 1.0 if on else 0.0
            if not on:
                self._press_id = None
            self._repaint_view()
            return

        prev = getattr(self, "_press_anim", None)
        if prev is not None:
            prev.stop()
        anim = QVariantAnimation(self)
        anim.setStartValue(self._press_k)
        anim.setEndValue(1.0 if on else 0.0)
        anim.setDuration(theme.MOTION["press"])
        anim.setEasingCurve(theme.EASE)
        anim.valueChanged.connect(self._on_press_tick)
        if not on:
            anim.finished.connect(self._on_press_done)
        self._press_anim = anim          # 必须持有，否则动画中途被回收
        anim.start()

    def _on_press_tick(self, value) -> None:
        self._press_k = float(value)
        self._repaint_view()

    def _on_press_done(self) -> None:
        self._press_id = None
        self._press_k = 0.0
        self._repaint_view()

    def clear_press(self) -> None:
        """松开在别处（滚出视口、切筛选）时把按下态收掉。"""
        if self._press_id is not None:
            self.press(self._press_id, False)

    # -- 选中高亮淡入 ------------------------------------------------------ #

    def _sel_k_of(self, shot_id: int, selected: bool) -> float:
        """这张卡片此刻的选中强度 0..1。

        正在做过渡的卡片取动画进度，其余的按真实状态直接给 0 / 1 ——
        这样只有"刚变的那一两张"在动，画别的卡片不需要查表。
        """
        if shot_id in self._sel_in_ids:
            return self._sel_in_k
        if shot_id in self._sel_out_ids:
            return 1.0 - self._sel_out_k
        return 1.0 if selected else 0.0

    def animate_selection(self, entering, leaving) -> None:
        """选中高亮淡入 / 淡出。

        点卡片是图库里最频繁的交互，而改版前那块"强调色底 + 一圈描边"是
        **硬切**的：点一下，底色和描边同时啪地出现。用户说"用起来还是很生硬"，
        这一处占的比重最大。

        只动"刚变的那几张"：整组进入共用一条 0→1 动画，整组离开共用另一条，
        所以 300 张一起取消选中也不会变成 300 个动画对象。
        """
        entering, leaving = set(entering), set(leaving)
        prev = self._sel_anim
        if prev is not None:
            prev.stop()
        self._sel_in_ids, self._sel_out_ids = entering, leaving
        if not theme.animating() or not (entering or leaving):
            self._sel_in_ids, self._sel_out_ids = set(), set()
            self._sel_in_k = self._sel_out_k = 1.0
            self._repaint_view()
            return
        self._sel_in_k = self._sel_out_k = 0.0
        anim = QVariantAnimation(self)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setDuration(theme.MOTION["normal"])
        anim.setEasingCurve(theme.EASE)
        anim.valueChanged.connect(self._on_sel_tick)
        anim.finished.connect(self._on_sel_done)
        self._sel_anim = anim                  # 持有，否则动画中途被回收
        anim.start()

    def _on_sel_tick(self, value) -> None:
        self._sel_in_k = self._sel_out_k = float(value)
        self._repaint_view()

    def _on_sel_done(self) -> None:
        # 收尾交还给真实状态：id 集合清空后 _sel_k_of 直接读 option.state。
        self._sel_in_ids, self._sel_out_ids = set(), set()
        self._sel_in_k = self._sel_out_k = 1.0
        self._repaint_view()

    # -- 尺寸 -------------------------------------------------------------- #

    def _fonts(self) -> tuple[QFontMetrics, QFontMetrics]:
        return self._metrics()[1], self._metrics()[3]

    def meta_box_h(self) -> int:
        """缩略图下方文字区的高度：两行标题 + 一行 meta。

        **不要再写死成 40。** 改版前这里是常量 ``meta_h = 40``，而
        "两行标题(34) + 间距(4) + meta 行(16)" 实际要 54px —— 结果 paint() 里的
        边界判断永远为假，卡片下面那行「分类图标 + 分类名 + 时间」**根本没画出来**。
        预览图上看着"只是少了一行小字"，实际是整块信息丢了。

        现在按字体实际行高算，字体换了也不会算错。
        """
        title_fm, meta_fm = self._fonts()
        return theme.S2 + title_fm.height() * 2 + theme.S1 + meta_fm.height()

    def stage_h(self) -> int:
        """缩略图舞台的高度 —— 按 ``STAGE_ASPECT`` 从宽算出来。

        改版前舞台是**正方形**（thumb × thumb），而图库里绝大多数截图是
        16:10。按比例缩进去以后，每张图上下各留一大条空白，而且留多少
        完全取决于那张图自己的比例 —— 一屏看过去高矮参差，非常不利落。

        改成贴合截图真实比例之后，16:10 的截图几乎填满舞台，卡片一眼看上去
        是齐的。舞台尺寸仍然是**所有卡片完全一致**的，参差感来自图片本身这一点
        已经消掉了。
        """
        return max(1, int(round(self.thumb / self.STAGE_ASPECT)))

    def sizeHint(self, option, index) -> QSize:
        return QSize(self.thumb + self.padding * 2,
                     self.padding * 2 + self.stage_h() + self.meta_box_h())

    # -- 绘制 -------------------------------------------------------------- #

    def paint(self, painter: QPainter, option: QStyleOptionViewItem,
              index) -> None:
        t = self._t()
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)

        # 卡片之间留 3px，滚动时不会糊成一片。
        rect = option.rect.adjusted(3, 3, -3, -3)
        rec: ShotRecord | None = index.data(Qt.UserRole + 1)
        shot_id = index.data(Qt.UserRole)
        selected = bool(option.state & QStyle.State_Selected)
        hovered = bool(option.state & QStyle.State_MouseOver)

        # --- 按下回弹 ---
        # 整张卡片（含缩略图、文字、选中描边）一起缩，所以放在最前面：
        # 一旦开了缩放变换，后面所有按 rect 画的东西自动跟着缩。
        if (self._press_id is not None and shot_id == self._press_id
                and self._press_k > 0.0):
            k = 1.0 - self.PRESS_SCALE * self._press_k
            center = rect.center()
            painter.translate(center)
            painter.scale(k, k)
            painter.translate(-center)

        # 平时**什么都不画** —— 简约风的关键，不是给每张卡片都套方块 + 描边。
        if hovered and not selected:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(t["surface2"]))
            painter.drawRoundedRect(rect, theme.R_CARD, theme.R_CARD)

        # 选中 = 强调色低透明填充 + 一圈强调色描边，**按选中进度插值**。
        # 改版前这里是 `if selected:` 硬画满 —— 点一下就整块跳出来。
        sel_k = self._sel_k_of(shot_id, selected)
        if sel_k > 0.002:
            sel_alpha = theme.SEL_ALPHA_DARK if self._theme == "dark" \
                else theme.SEL_ALPHA_LIGHT
            painter.setPen(Qt.NoPen)
            painter.setBrush(theme.alpha(t["accent"], int(sel_alpha * sel_k)))
            painter.drawRoundedRect(rect, theme.R_CARD, theme.R_CARD)
            painter.setBrush(Qt.NoBrush)
            pen = QPen(theme.alpha(t["accent"], int(255 * sel_k)))
            pen.setWidthF(1.4)
            painter.setPen(pen)
            painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1),
                                    theme.R_CARD - 1, theme.R_CARD - 1)

        # --- 缩略图舞台 ---
        pad = self.padding
        img_rect = QRect(rect.left() + pad, rect.top() + pad,
                         rect.width() - pad * 2, self.stage_h())
        # 舞台底色用专门的 ``stage`` 令牌，不用 surface3。
        # 浅色下 surface3(#EFEFF2) 和窗口底(#F3F3F5) 只差 4 级，
        # 白底截图压上去等于没有边界（见 theme.TOKENS 里 stage 的说明）。
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(t["stage"]))
        painter.drawRoundedRect(img_rect, theme.R_NEST, theme.R_NEST)

        img = self.image_of(index.data(Qt.UserRole))
        if isinstance(img, QImage) and not img.isNull():
            pm = self._stage_pixmap(shot_id, img, img_rect.size())
            painter.drawPixmap(
                img_rect.left() + (img_rect.width() - pm.width()) // 2,
                img_rect.top() + (img_rect.height() - pm.height()) // 2,
                pm)
        else:
            # 占位：一个明确的 image-off 图标，比一个「…」说得多。
            side = 22
            icon_rect = QRect(0, 0, side, side)
            icon_rect.moveCenter(img_rect.center())
            painter.drawPixmap(icon_rect,
                               self._icon(painter, "image-off", t["fg3"], side, 1.6))

        # --- 新归档的那一圈光 ---
        if self._flash_id is not None and shot_id == self._flash_id \
                and self._flash_value > 0:
            pen = QPen(theme.alpha(t["accent"], int(220 * self._flash_value)))
            pen.setWidthF(2.0)
            painter.setBrush(Qt.NoBrush)
            painter.setPen(pen)
            painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1),
                                    theme.R_CARD - 1, theme.R_CARD - 1)

        title_font, title_fm, meta_font, meta_fm = self._metrics()

        # --- 标题：两行，按像素省略 ---
        meta_top = img_rect.bottom() + theme.S2
        meta_rect = QRect(rect.left() + pad, meta_top,
                          rect.width() - pad * 2, rect.bottom() - meta_top)
        title_rect = QRect(meta_rect.left(), meta_rect.top(),
                           meta_rect.width(), title_fm.height() * 2)

        painter.setFont(title_font)
        painter.setPen(QColor(t["fg1"]))
        # 标题用 slug（描述本身），不用完整文件名 —— 日期前缀在下面一行已经有了，
        # 卡片才 150px 宽，把 20261013_204814_shot_ 这 20 个字符再重复一遍纯属浪费。
        name = (rec.slug or rec.name_stem or rec.filename) if rec else ""
        for i, line in enumerate(self._wrapped(name, title_fm, title_rect.width(), 2)):
            painter.drawText(QRect(title_rect.left(),
                                   title_rect.top() + i * title_fm.height(),
                                   title_rect.width(), title_fm.height()),
                             Qt.AlignLeft | Qt.AlignVCenter, line)

        # --- meta：分类图标 + 分类名 + 时间 ---
        # 高度由 meta_box_h() 保证够，这里不再做"塞得下才画"的静默降级 ——
        # 那个降级正是改版前 meta 行整块消失的原因。
        row_y = title_rect.bottom() + theme.S1
        row_h = meta_fm.height()
        x = meta_rect.left()
        if rec:
            glyph = 12
            g_rect = QRect(x, row_y + (row_h - glyph) // 2, glyph, glyph)
            # 分类图标带强调色 —— 全屏里只有这一个小色点是"有颜色"的，
            # 一眼就能扫出分类，又不会把屏幕染花。
            # 图标名走 cfg.category_icon() 而不是 theme.category_icon()：
            # 用户在设置里给某个分类换过图标的话，只有前者认那笔覆盖。
            cfg = self._find_cfg()
            icon_name = (cfg.category_icon(rec.category)
                         if isinstance(cfg, Config) else category_icon(rec.category))
            painter.drawPixmap(
                g_rect,
                self._icon(painter, icon_name,
                           theme.mix(t["fg2"], t["accent"], sel_k).name(),
                           glyph, 2.0))
            x = g_rect.right() + theme.S1

        painter.setFont(meta_font)
        # 选中的卡片底色是强调色低透明填充，fg3 压上去对比度不够 ——
        # 提一档到 fg2。这里也按选中进度插值，跟底色同步走过去。
        painter.setPen(theme.mix(t["fg3"], t["fg2"], sel_k))
        cat = self._cat_label(rec) if rec else ""
        # 时间只要「月-日 时:分」，年份和秒在卡片上没意义。
        when = (str(rec.captured_at or "")[5:16].replace("T", " ")
                if rec else "")
        tail = f"{cat}  ·  {when}" if when else cat

        avail = meta_rect.right() - x
        if rec is not None and rec.favorite:
            star = 12
            star_rect = QRect(meta_rect.right() - star,
                              row_y + (row_h - star) // 2, star, star)
            painter.drawPixmap(star_rect,
                               self._icon(painter, "star", t["warn"], star, 2.0))
            avail -= star + theme.S1
        painter.drawText(QRect(x, row_y, max(0, avail), row_h),
                         Qt.AlignLeft | Qt.AlignVCenter,
                         meta_fm.elidedText(tail, Qt.ElideRight, max(0, avail)))

        painter.restore()

    # -- 小工具 ------------------------------------------------------------ #

    @staticmethod
    def _wrap(text: str, fm: QFontMetrics, width: int, max_lines: int) -> list:
        """按像素宽度把文字折成最多 `max_lines` 行，最后一行带省略号。

        改版前是 ``name[:26]`` —— 中英文混排时 26 个字符的宽度能差一倍，
        所以有时留一大截空白、有时照样溢出。按像素算才稳定。
        """
        if width <= 0 or not text:
            return [""]
        lines: list = []
        rest = text
        for i in range(max_lines):
            if fm.horizontalAdvance(rest) <= width:
                lines.append(rest)
                return lines
            if i == max_lines - 1:
                lines.append(fm.elidedText(rest, Qt.ElideRight, width))
                return lines
            acc = ""
            for ch in rest:
                if fm.horizontalAdvance(acc + ch) > width:
                    break
                acc += ch
            lines.append(acc)
            rest = rest[len(acc):].lstrip()
        return lines

    def _wrapped(self, text: str, fm: QFontMetrics, width: int,
                 max_lines: int) -> list:
        """``_wrap`` 的记忆版。

        同一张卡片在滚动、悬停、缩略图陆续到齐时会**反复重画**，而折行结果
        只和 (文字, 可用宽度, 主题) 有关 —— 每次重画都重算一遍纯属浪费，
        实测这部分占一帧全视口重绘的 11%。

        刻意**没有**去改 ``_wrap`` 内部的算法：那里是逐字
        ``horizontalAdvance(acc + ch)``，是 O(n²) 的写法，改成累加能再快一点，
        但那个写法把字距（kerning）也算进去了，换成加法会让折行位置偶尔差一个字。
        这里只做记忆化，不动行为。
        """
        key = (text, width, max_lines, self._fm_key)
        hit = self._wrap_cache.get(key)
        if hit is None:
            hit = self._wrap(text, fm, width, max_lines)
            if len(self._wrap_cache) > 2048:
                self._wrap_cache.clear()      # 简单粗暴，但不会无界增长
            self._wrap_cache[key] = hit
        return hit

    def _cat_label(self, rec: ShotRecord) -> str:
        """分类的中文名。

        **踩过的坑**：原来是 ``getattr(self.parent(), "cfg", None)``。委托的
        parent 是 ``QListWidget``，而 ``cfg`` 挂在顶层窗口 ``GalleryWindow`` 上 ——
        所以这个查找永远是 None，卡片 meta 行一直显示英文分类键
        （``image`` / ``code`` / ``webpage``），而不是配置里的中文名。
        这里沿父链往上找到带 cfg 的那一层。
        """
        cfg = self._find_cfg()
        if isinstance(cfg, Config):
            return cfg.category_label(rec.category)
        return rec.category

    def _find_cfg(self):
        """沿父链找带 ``cfg`` 的祖先，结果缓存 —— 每张卡片每次重绘都要用。"""
        cached = getattr(self, "_cfg_cache", None)
        if cached is not None:
            return cached
        widget = self.parent()
        for _ in range(8):                       # 深度上限，避免环形父链死循环
            if widget is None:
                break
            cfg = getattr(widget, "cfg", None)
            if isinstance(cfg, Config):
                self._cfg_cache = cfg
                return cfg
            widget = widget.parent()
        return None

# 侧栏筛选
# --------------------------------------------------------------------------- #
# 侧栏筛选 / 搜索
# --------------------------------------------------------------------------- #

class CategoryRow(QWidget):
    """左栏的一行分类：图标 + 名称 + 计数。

    为什么不用 QCheckBox：Win11 的侧栏导航不是"复选框列表"，每一项是一整行
    可点击的面，hover 浮起、选中染色、计数右对齐。用 QCheckBox 要同时改
    indicator、文字色、内边距、禁用色，四处都是脏活，而且**每一项都得
    setStyleSheet** —— 这正是改版前"颜色散得到处都是"的来源之一。

    自己画反而更短：一个 paintEvent 说清全部状态。
    """

    toggled = Signal(str, bool)

    #: 行高 32px —— 与 Win11 的控件高度一致。
    ROW_H = theme.CONTROL_H

    def __init__(self, key: str, label: str, icon_name: str,
                 count: int = 0, parent=None) -> None:
        super().__init__(parent)
        self.key = key
        self.label = label
        self.icon_name = icon_name
        self.count = count
        self.checked = False
        self._hover = False
        #: hover 进度 0..1，由动画驱动。paintEvent 用它把底色从透明插值到
        #: surface2 —— 直接二选一的话鼠标划过会"闪"，Win11 里没有这种跳变。
        self._hover_k = 0.0
        #: 选中进度 0..1，同样由动画驱动。
        #:
        #: 改版前选中态是硬切：点一下，整行底色+图标+文字**同时瞬变**。
        #: 用户反馈"感受不到动画、用起来很生硬"，这条是主因之一 ——
        #: 10 行长的一列里点来点去，每一次都是硬闪。
        #: 现在底色、图标色、文字色三者一起按这个进度走过去。
        self._sel_k = 0.0
        self._theme = "dark"
        self.setFixedHeight(self.ROW_H)
        self.setCursor(Qt.PointingHandCursor)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_Hover, True)

    # -- hover 进度（可动画属性） ------------------------------------------ #

    def _get_hover_k(self) -> float:
        return self._hover_k

    def _set_hover_k(self, value: float) -> None:
        self._hover_k = value
        self.update()                      # 每帧重画，插值才看得出来

    #: 暴露给 QPropertyAnimation 的浮点属性。Qt 属性动画要求属性必须注册在
    #: 元对象里，普通实例属性它找不到。
    hoverK = Property(float, _get_hover_k, _set_hover_k)

    def _animate_hover(self, on: bool) -> None:
        """hover 进度平滑到 0 / 1。用户关了动效就瞬切。"""
        target = 1.0 if on else 0.0
        if not theme.animating() or self._hover_k == target:
            self._hover_k = target
            self.update()
            return
        anim = getattr(self, "_hover_anim", None)
        if anim is not None:
            try:
                anim.stop()
            except Exception:
                pass
        anim = QPropertyAnimation(self, b"hoverK", self)
        anim.setDuration(theme.MOTION["fast"])
        anim.setStartValue(self._hover_k)
        anim.setEndValue(target)
        anim.setEasingCurve(theme.EASE)
        anim.start()
        self._hover_anim = anim             # 持有，别被回收

    def _get_sel_k(self) -> float:
        return self._sel_k

    def _set_sel_k(self, value: float) -> None:
        self._sel_k = value
        self.update()

    selK = Property(float, _get_sel_k, _set_sel_k)

    def _animate_sel(self, on: bool) -> None:
        """选中进度平滑到 0 / 1。

        用 ``MOTION["normal"]``(200ms) 而不是 hover 的 fast(150ms)：
        选中是"状态变了"，比"鼠标划过"更值得被看见，时长长一档才看得出来。
        """
        target = 1.0 if on else 0.0
        if not theme.animating() or self._sel_k == target:
            self._sel_k = target
            self.update()
            return
        prev = getattr(self, "_sel_anim", None)
        if prev is not None:
            try:
                prev.stop()
            except Exception:
                pass
        anim = QPropertyAnimation(self, b"selK", self)
        anim.setDuration(theme.MOTION["normal"])
        anim.setStartValue(self._sel_k)
        anim.setEndValue(target)
        anim.setEasingCurve(theme.EASE)
        anim.start()
        self._sel_anim = anim

    def set_theme(self, name: str) -> None:
        self._theme = name
        self.update()

    def set_count(self, count: int) -> None:
        if count != self.count:
            self.count = count
            self.update()

    def set_label(self, label: str, icon_name: str) -> None:
        """换显示名 / 图标（用户在设置里改了分类，左栏要跟着变）。

        只改不 update 的话屏幕上还是旧字 —— paintEvent 读的就是这两个字段。
        """
        if label != self.label or icon_name != self.icon_name:
            self.label = label
            self.icon_name = icon_name
            self.update()

    def set_checked(self, checked: bool) -> None:
        if checked != self.checked:
            self.checked = bool(checked)
            # 选中态画的是 accent 底，跟 hover 底互斥。取消选中时如果鼠标
            # 还停在行上，hover 进度得补回来 —— 不然会停在一块没底的中间态。
            self._animate_hover(not checked and self._hover)
            self._animate_sel(checked)

    # -- 交互 -------------------------------------------------------------- #

    def mousePressEvent(self, event) -> None:                     # noqa: N802
        if event.button() == Qt.LeftButton:
            self.set_checked(not self.checked)
            self.toggled.emit(self.key, self.checked)
        event.accept()

    def enterEvent(self, event) -> None:                          # noqa: N802
        self._hover = True
        self._animate_hover(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:                          # noqa: N802
        self._hover = False
        self._animate_hover(False)
        super().leaveEvent(event)

    # -- 绘制 -------------------------------------------------------------- #

    def paintEvent(self, _event) -> None:                         # noqa: N802
        t = theme.tokens(self._theme)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        rect = self.rect().adjusted(0, 1, 0, -1)

        # 底色：选中是 accent 低透明填充，hover 是 surface2；两者都按各自
        # 的进度插值透明度。**不用 if/elif 二选一** —— 硬切就是"生硬"的来源。
        k_sel = self._sel_k
        k_hov = self._hover_k * (1.0 - k_sel)     # 选中走高时 hover 让位
        if k_sel > 0.001 or k_hov > 0.001:
            p.setPen(Qt.NoPen)
            sel_alpha = theme.SEL_ALPHA_DARK if self._theme == "dark" \
                else theme.SEL_ALPHA_LIGHT
            if k_sel > 0.001:
                p.setBrush(theme.alpha(t["accent"], int(sel_alpha * k_sel)))
                p.drawRoundedRect(rect, theme.R_CTL, theme.R_CTL)
            if k_hov > 0.001:
                p.setBrush(theme.alpha(t["surface2"], int(255 * k_hov)))
                p.drawRoundedRect(rect, theme.R_CTL, theme.R_CTL)

        glyph = 16
        g_rect = QRect(rect.left() + theme.S2,
                       rect.top() + (rect.height() - glyph) // 2, glyph, glyph)
        # 图标色也按选中进度从"常态色"插到强调色，跟底色同步 ——
        # 底色在渐变、图标"啪"地变绿，比两个都瞬切更显假。
        base_icon = t["fg3"] if self.count == 0 else t["fg2"]
        icon_color = theme.mix(base_icon, t["accent"], k_sel).name()
        icons.paint(p, self.icon_name, g_rect, icon_color, width=2.0)

        # 计数右对齐、等宽数字 —— 数字列对不齐是最容易被忽略、又最显廉价的细节。
        count_text = str(self.count)
        count_fm = QFontMetrics(theme.mono_font(12))
        count_w = max(28, count_fm.horizontalAdvance(count_text) + theme.S2)

        text_left = g_rect.right() + theme.S2
        text_w = rect.right() - theme.S3 - count_w - text_left
        label_fm = QFontMetrics(theme.ui_font(13, QFont.Medium))
        p.setFont(theme.ui_font(13, QFont.Medium))
        # 文字同理：从常态色插到 fg1，走得比底色略前一点，读起来才跟手。
        base_fg = t["fg3"] if self.count == 0 else t["fg2"]
        fg = theme.mix(base_fg, t["fg1"], max(k_sel, k_hov)).name()
        p.setPen(QColor(fg))
        p.drawText(QRect(text_left, rect.top(), max(0, text_w), rect.height()),
                   Qt.AlignLeft | Qt.AlignVCenter,
                   label_fm.elidedText(self.label, Qt.ElideRight, max(0, text_w)))

        p.setFont(theme.mono_font(12))
        p.setPen(QColor(t["fg2"] if self.checked else t["fg3"]))
        p.drawText(QRect(rect.right() - theme.S3 - count_w, rect.top(),
                         count_w, rect.height()),
                   Qt.AlignRight | Qt.AlignVCenter, count_text)
        p.end()


class FilterPanel(QWidget):
    """左侧分类栏：一张干净的分类清单。

    这里原来还有「标签云」和「时间范围」两整块。标签云展示的是分类器自动打的
    视觉特征（light / dark / mono / wide / colorful …），对"命名与分类"这件事
    没有任何帮助，却常年占掉半屏、还挤得分类列表看不清；时间筛选同理。
    两块都拿掉了，只留分类。

    改版后每行是自绘的 :class:`CategoryRow`，不再有 10 种分类色 ——
    分类由**图标**区分，全栏只有选中那一行带强调色。
    """

    changed = Signal()

    def __init__(self, vault: Vault, cfg: Config, parent=None) -> None:
        super().__init__(parent)
        self.vault = vault
        self.cfg = cfg
        self._rows: dict[str, CategoryRow] = {}
        self._all_row: CategoryRow | None = None
        self._total = 0
        self._theme = "dark"
        # 以下三个字段是 GalleryWindow 拼查询条件时读的。标签/时间筛选已经移除，
        # 保留空值免得调用方到处判空。
        self.active_tags: set[str] = set()
        self.day_from: str = ""
        self.day_to: str = ""

        lay = QVBoxLayout(self)
        lay.setContentsMargins(theme.S3, theme.S3, theme.S3, theme.S3)
        lay.setSpacing(0)

        self._header = _SectionLabel("分类", "list-filter", self)
        lay.addWidget(self._header)
        lay.addSpacing(theme.S2)

        # 「全部」放在最上面：它是"清空筛选"的默认落点，没有它就只能一个个取消勾选。
        self._all_row = CategoryRow("", "全部", "images")
        self._all_row.toggled.connect(self._on_all_toggled)
        lay.addWidget(self._all_row)

        self._cat_box = QVBoxLayout()
        self._cat_box.setSpacing(0)
        lay.addLayout(self._cat_box)
        lay.addStretch(1)

        self.refresh()

    def set_theme(self, name: str) -> None:
        self._theme = name
        self._header.set_theme(name)
        self._all_row.set_theme(name)
        for row in self._rows.values():
            row.set_theme(name)

    # -- 刷新计数 ---------------------------------------------------------- #

    def refresh(self) -> None:
        counts = self.vault.category_counts()
        order = list(self.cfg.categories)
        for key in counts:
            if key not in order:
                order.append(key)
        self._total = sum(counts.values())

        for key in list(self._rows):
            if key not in order:
                row = self._rows.pop(key)
                row.setParent(None)
                row.deleteLater()

        for pos, key in enumerate(order):
            row = self._rows.get(key)
            if row is None:
                row = CategoryRow(key, self.cfg.category_label(key),
                                  self.cfg.category_icon(key))
                row.toggled.connect(self._on_row_toggled)
                self._rows[key] = row
                self._cat_box.addWidget(row)
            else:
                # 已存在的行也要把名称 / 图标刷新一遍 —— 用户在设置里改了名、
                # 换了图标，左栏不跟着变的话等于"设置没生效"。
                row.set_label(self.cfg.category_label(key),
                              self.cfg.category_icon(key))
            # 视觉顺序跟着 cfg.categories 走（设置里调了序，这里要跟上）。
            # _cat_box 里只有分类行， SectionLabel 和「全部」在外层布局里。
            if self._cat_box.indexOf(row) != pos:
                self._cat_box.removeWidget(row)
                self._cat_box.insertWidget(pos, row)
            row.set_count(counts.get(key, 0))

        self._all_row.set_count(self._total)
        self._sync_all_row()

    def _on_row_toggled(self, _key: str, _checked: bool) -> None:
        self._sync_all_row()
        self.changed.emit()

    def _on_all_toggled(self, _key: str, checked: bool) -> None:
        if checked:
            for row in self._rows.values():
                row.set_checked(False)
        self._sync_all_row()
        self.changed.emit()

    def _sync_all_row(self) -> None:
        """「全部」= 一个分类都没勾。它自己不参与查询，是个视觉状态量。"""
        self._all_row.set_checked(not self.selected_categories())

    # -- 供窗口读取 -------------------------------------------------------- #

    def selected_categories(self) -> list[str]:
        return [k for k, r in self._rows.items() if r.checked]

    def selected_tags(self) -> list[str]:
        return []


class _SectionLabel(QWidget):
    """小节标题：小号灰字 + 一颗图标。

    用自绘而不是 QLabel，是因为它要"图标和文字基线对齐" ——
    QLabel 里塞 SVG 得现做一个 QPixmap 再 setPixmap，
    还要拿 QFontMetrics 手算垂直居中，其实比自己画更麻烦。
    """

    def __init__(self, text: str, icon_name: str, parent=None) -> None:
        super().__init__(parent)
        self.text = text
        self.icon_name = icon_name
        self._theme = "dark"
        self.setFixedHeight(20)

    def set_theme(self, name: str) -> None:
        self._theme = name
        self.update()

    def paintEvent(self, _event) -> None:                         # noqa: N802
        t = theme.tokens(self._theme)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        glyph = 13
        g_rect = QRect(theme.S2, (self.height() - glyph) // 2, glyph, glyph)
        icons.paint(p, self.icon_name, g_rect, t["fg3"], width=2.0)
        p.setFont(theme.ui_font(12, QFont.DemiBold))
        p.setPen(QColor(t["fg3"]))
        p.drawText(QRect(g_rect.right() + theme.S1, 0,
                         self.width() - g_rect.right() - theme.S1,
                         self.height()),
                   Qt.AlignLeft | Qt.AlignVCenter, self.text)
        p.end()


# --------------------------------------------------------------------------- #
# 搜索框
# --------------------------------------------------------------------------- #

class SearchBox(QLineEdit):
    """带前置放大镜的搜索框。

    图标用 paintEvent 画在文字左边，而不是 addAction ——
    addAction 会生成一个可点击的 QToolButton，点了没反应会让人困惑；
    搜索图标本来就只是个装饰。
    """

    ICON_W = 30

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        # 提示里不再出现 tag: —— 标签那块已经从界面上拿掉了，
        # 留个提示会让人以为还能按标签筛。
        self.setPlaceholderText("搜描述词、分类名或日期…")
        self.setClearButtonEnabled(True)
        self.setMinimumHeight(theme.CONTROL_H)
        # 给左侧图标让位，文字不会压到放大镜上。
        self.setTextMargins(self.ICON_W, 0, 0, 0)
        self._theme = "dark"

    def set_theme(self, name: str) -> None:
        self._theme = "light" if name == "light" else "dark"
        self.update()

    def paintEvent(self, event) -> None:                          # noqa: N802
        super().paintEvent(event)
        t = theme.tokens(self._theme)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        glyph = 15
        g_rect = QRect(theme.S3, (self.height() - glyph) // 2, glyph, glyph)
        icons.paint(p, "search", g_rect,
                    t["fg1"] if self.hasFocus() else t["fg3"], width=2.0)
        p.end()


# --------------------------------------------------------------------------- #
# 空态
# --------------------------------------------------------------------------- #

class _EmptyState(QWidget):
    """网格空着时盖在上面的一句话。

    一张什么都没有的网格，是界面上最容易显廉价的画面 —— 看起来像加载失败。
    给一颗图标 + 一句人话，把"这里本来就该是空的"说清楚。

    它**不吃鼠标事件**（``WA_TransparentForMouseEvents``），
    否则盖在网格上会挡住框选。
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._theme = "dark"
        self._title = "这里还没有截图"
        self._hint = "用 Win+Shift+S 截图，SnapVault 会自动命名并分类"

    def set_message(self, title: str, hint: str) -> None:
        if (title, hint) != (self._title, self._hint):
            self._title, self._hint = title, hint
            self.update()

    def set_theme(self, name: str) -> None:
        self._theme = "light" if name == "light" else "dark"
        self.update()

    def paintEvent(self, _event) -> None:                          # noqa: N802
        t = theme.tokens(self._theme)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        cx, cy = self.width() // 2, int(self.height() * 0.38)
        glyph = 40
        g_rect = QRect(0, 0, glyph, glyph)
        g_rect.moveCenter(QPoint(cx, cy))
        icons.paint(p, "images", g_rect, t["stroke"], width=1.4)

        p.setFont(theme.ui_font(14, QFont.Medium))
        p.setPen(QColor(t["fg2"]))
        p.drawText(QRect(0, g_rect.bottom() + theme.S4, self.width(), 24),
                   Qt.AlignHCenter | Qt.AlignTop, self._title)

        p.setFont(theme.ui_font(12, QFont.Normal))
        p.setPen(QColor(t["fg3"]))
        p.drawText(QRect(0, g_rect.bottom() + theme.S4 + 26, self.width(), 20),
                   Qt.AlignHCenter | Qt.AlignTop, self._hint)
        p.end()


# --------------------------------------------------------------------------- #
# 状态指示灯
# --------------------------------------------------------------------------- #

class _StatusDot(QWidget):
    """状态栏左侧的常驻状态位：一颗会呼吸的点 + 一句状态文字。

    它是「不弹窗」这个决定能够成立的**前提**。既然成功时不弹任何东西，
    就必须有一个常驻的、一眼可见的地方告诉用户"它还在跑"。
    把这句话放在状态栏，比任何托盘气泡都可靠 —— 不会被专注助手吞掉，
    也不会进通知中心攒一堆。

    * 接管中：点做 2.4 秒一次的呼吸（theme.MOTION["pulse"]），颜色用成功色；
    * 已暂停：点变成静态的弱灰色。
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._theme = "dark"
        self._active = False
        self._watched = 0
        self._phase = 0.0
        self.setFixedHeight(18)
        self._anim = QVariantAnimation(self)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setDuration(theme.MOTION["pulse"])
        self._anim.setLoopCount(-1)
        self._anim.valueChanged.connect(self._on_tick)
        self._refresh_width()

    def set_theme(self, name: str) -> None:
        self._theme = "light" if name == "light" else "dark"
        self.update()

    def set_active(self, active: bool, watched: int = 0) -> None:
        self._active = bool(active)
        self._watched = int(watched)
        if self._active and self._anim.state() != QVariantAnimation.Running:
            self._anim.start()
        elif not self._active:
            self._anim.stop()
            self._phase = 0.0
        self._refresh_width()
        self.update()

    def _on_tick(self, value) -> None:
        self._phase = float(value)
        self.update()

    def _refresh_width(self) -> None:
        text = self._text()
        fm = QFontMetrics(theme.ui_font(12))
        self.setFixedWidth(theme.S6 + theme.S1 + fm.horizontalAdvance(text) + theme.S2)

    def _text(self) -> str:
        if not self._active:
            return "接管已暂停"
        if self._watched > 1:
            return f"接管中 · 监视 {self._watched} 个目录"
        return "接管中"

    def paintEvent(self, _event) -> None:                          # noqa: N802
        t = theme.tokens(self._theme)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        # 呼吸：sin 曲线，0 → 1 → 0。用 0.5 - 0.5*cos 保证首尾都在 0，
        # 循环时不会突然跳一下。
        import math
        wave = 0.5 - 0.5 * math.cos(2 * math.pi * self._phase) if self._active else 0.0

        color = QColor(t["ok"] if self._active else t["fg3"])
        cx = theme.S2 + 3
        cy = self.height() // 2
        # 光晕：接管中才画，半径跟着呼吸。
        if self._active:
            halo = QColor(t["ok"])
            halo.setAlpha(int(90 * (1.0 - wave)))
            p.setPen(Qt.NoPen)
            p.setBrush(halo)
            p.drawEllipse(QPoint(cx, cy), int(3 + 4 * wave), int(3 + 4 * wave))
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        p.drawEllipse(QPoint(cx, cy), 3, 3)

        p.setFont(theme.ui_font(12))
        p.setPen(QColor(t["fg3"]))
        p.drawText(QRect(theme.S2 + theme.S1 + 2, 0,
                         self.width() - theme.S2 - theme.S1 - 2, self.height()),
                   Qt.AlignLeft | Qt.AlignVCenter, self._text())
        p.end()


# --------------------------------------------------------------------------- #
# 主窗口
# --------------------------------------------------------------------------- #

class GalleryWindow(QMainWindow):
    """SnapVault 图库窗口。"""

    capture_requested = Signal(str)      # region / window / fullscreen / all_screens / last
    settings_requested = Signal()
    quit_requested = Signal()

    def __init__(self, vault: Vault, cfg: Config, parent=None) -> None:
        super().__init__(parent)
        self.vault = vault
        self._cfg = cfg
        self.setWindowTitle("SnapVault 图库")
        self.resize(1180, 720)
        self.setMinimumSize(880, 520)

        self._pool = QThreadPool.globalInstance()
        self._max_threads = self._pool.maxThreadCount()
        self._thumbs: "OrderedDict[int, QImage]" = OrderedDict()
        # 缓存上限实测账：400 张 × 384px 解码 ≈ 384 MB 常驻 —— 这是图库窗口
        # 内存的大头（Vault.search 只花 55 ms，索引根本不是问题）。
        # 一屏 4×3=12 张，上下各预取一屏 = 36 张；120 张是"猛滚动两屏"的量，
        # 够用，且比 400 张省 ~290 MB。
        self._thumbs_limit = 120
        self._records: dict[int, ShotRecord] = {}
        #: shot_id → QListWidgetItem。缩略图到齐时要找到对应 item 让它重画，
        #: 原来是遍历全部 item 线性找（891 条库下每次都是一整遍）。
        self._item_of: dict[int, QListWidgetItem] = {}
        #: 当前选中的 shot_id 集合。用来算"这次选中变化进了哪些、出了哪些"，
        #: 好只给真正变的那几张做高亮过渡。
        self._selected_ids: set[int] = set()
        self._current: ShotRecord | None = None
        # 加载尺寸不再用 cfg.storage.thumb_size(384)：卡片实际只显示 ~200px，
        # 多解码的那一倍像素纯属浪费内存。_refit_grid() 会按当前卡片边长
        # 同步 loader.size —— 缩略图本来就按 384 生成的，小卡片直接缩读即可。
        self._loader = ThumbnailLoader(vault, self._pool, size=256)
        self._loader.loaded.connect(self._on_thumb_loaded)
        self._pending_query = QTimer(self)
        self._pending_query.setSingleShot(True)
        self._pending_query.setInterval(120)
        self._pending_query.timeout.connect(self.reload)
        #: 缩略图补图节流器。
        #:
        #: 改版前是两处各自的 ``QTimer.singleShot``：``verticalScrollBar`` 的
        #: valueChanged 排一个 40 ms，``wheelEvent`` 再排一个 60 ms。滚轮滚一下
        #: 会同时触发这两条（一格滚轮 = 1 个 wheel 事件 + 1 次 valueChanged），
        #: 连续滚动就是几十个定时器一起到期 —— 而每一次 ``_request_visible``
        #: 当时要遍历全库（见该方法的注释），于是 UI 线程被反复占满。
        #: 现在合并成一个「已在计时就不重启」的节流器：无论来多少事件，
        #: 每 50 ms 最多补一次图。
        self._req_timer = QTimer(self)
        self._req_timer.setSingleShot(True)
        self._req_timer.setInterval(50)
        self._req_timer.timeout.connect(self._request_visible)
        #: 平滑滚动的目标位与动画（见 _smooth_scroll_by）。惰性创建。
        self._scroll_target = 0.0
        self._scroll_anim: QPropertyAnimation | None = None
        #: 上一次查询的 (文本, 完整结果, 筛选签名, 结果条数)。
        #: 供 _fetch() 做增量收窄用；None 表示还没查过。
        self._last_fetch: tuple[str, list[ShotRecord], tuple, int] | None = None

        self._build_ui()
        self.set_theme(cfg.ui.theme)
        self.reload()

    # ------------------------------------------------------------------ #
    # 配置
    # ------------------------------------------------------------------ #

    @property
    def cfg(self) -> Config:
        return self._cfg

    @cfg.setter
    def cfg(self, value: Config) -> None:
        """换配置（设置保存后由 app 调用）。

        不能只存窗口自己这一份：:class:`FilterPanel` 构造时也存了一份 cfg
        引用，不跟着换的话，左栏的分类名 / 图标 / 顺序会一直用**旧配置** ——
        用户在设置里改完保存，左栏毫无变化，等于"设置没生效"。
        委托（:meth:`_find_cfg` 沿父链找）和详情面板本来就现查，不受影响。
        """
        self._cfg = value
        filters = getattr(self, "filters", None)
        if filters is not None:
            filters.cfg = value

    def set_theme(self, name: str) -> None:
        """主题切换时同步所有自绘控件的配色。

        QSS 管不到 :class:`QStyledItemDelegate`、自绘的 :class:`CategoryRow`
        和自绘按钮图标，它们得自己知道换了主题。改版后所有自绘代码都是
        **现查** :mod:`snapvault.theme`，所以这里只做"通知 + 重建图标"。
        """
        self._theme = "light" if name == "light" else "dark"
        self._delegate.set_theme(name)
        self.filters.set_theme(name)
        self.detail.set_theme(name)
        self._empty.set_theme(name)
        self._watch_dot.set_theme(name)
        self._toolbar_icons()
        self.grid.viewport().update()

    def _toolbar_icons(self) -> None:
        """按当前主题重建工具条图标。

        QIcon 是位图快照，换主题必须重建 —— 否则浅色主题下会留下深色的灰图标。
        """
        t = theme.tokens(self._theme)
        for action, name in self._icon_actions:
            action.setIcon(icons.icon(name, t["fg2"], 18))
        self.search.set_theme(self._theme)

    # ------------------------------------------------------------------ #
    # 界面构建
    # ------------------------------------------------------------------ #

    def _build_ui(self) -> None:
        self._theme = "light" if self.cfg.ui.theme == "light" else "dark"
        #: [(QAction, 图标名)] —— 换主题时按这个表重建图标
        self._icon_actions: list[tuple[QAction, str]] = []

        # --- 工具条：只剩「看图库」需要的动作 ---
        # 区域/窗口/全屏/重复这四个截图按钮已经拿掉 —— 截图归系统自带的
        # Win+Shift+S，这个窗口只负责看命名与分类的结果。
        tb = QToolBar("主工具条")
        tb.setMovable(False)
        tb.setIconSize(QSize(18, 18))
        # 图标 + tooltip，不带文字：三个按钮留白就够了，能少一列文字噪声。
        tb.setToolButtonStyle(Qt.ToolButtonIconOnly)
        self.addToolBar(tb)

        def act(icon_name: str, text: str, tip: str, slot,
                shortcut: str = "") -> QAction:
            a = QAction(self)
            a.setText(text)                 # 无障碍/菜单里仍然需要文字
            a.setToolTip(f"{tip}　{shortcut}" if shortcut else tip)
            if shortcut:
                a.setShortcut(QKeySequence(shortcut))
            a.triggered.connect(slot)
            tb.addAction(a)
            self._icon_actions.append((a, icon_name))
            return a

        act("refresh-cw", "刷新", "重新读取图库", self.reload, "F5")
        act("settings-2", "设置", "打开设置",
            lambda: self.settings_requested.emit())
        tb.addSeparator()
        act("folder-open", "打开图库目录", "在资源管理器中打开", self._reveal_root)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)

        self.search = SearchBox()
        self.search.setFixedWidth(322)      # 322 = 320 内容 + 2 描边
        self.search.textChanged.connect(lambda _: self._pending_query.start())
        self.search.returnPressed.connect(self.reload)
        tb.addWidget(self.search)

        # --- 中央三栏 ---
        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setHandleWidth(1)
        self.filters = FilterPanel(self.vault, self.cfg)
        self.filters.setMinimumWidth(168)
        self.filters.setMaximumWidth(320)
        self.filters.changed.connect(self._on_filters_changed)
        self.splitter.addWidget(self.filters)

        self.grid = QListWidget()
        self.grid.setViewMode(QListWidget.IconMode)
        self.grid.setResizeMode(QListWidget.Adjust)
        self.grid.setMovement(QListWidget.Static)
        self.grid.setUniformItemSizes(True)
        self.grid.setSpacing(4)
        # 缩略图不要贴着分类栏和窗口边缘，留一圈呼吸空间。
        self.grid.setViewportMargins(theme.S3, theme.S2, theme.S2, theme.S2)
        self.grid.setMouseTracking(True)
        self.grid.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.grid.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.grid.setContextMenuPolicy(Qt.CustomContextMenu)
        self.grid.customContextMenuRequested.connect(self._on_context_menu)
        self.grid.itemSelectionChanged.connect(self._on_selection_changed)
        self.grid.itemDoubleClicked.connect(lambda _: self._open_current())
        self.grid.verticalScrollBar().valueChanged.connect(
            lambda _v: self._schedule_request())
        # 按下回弹要在**视口**上听鼠标：QListWidget 的 item 命中测试只有视口
        # 坐标可用，装事件过滤器比自己派生子类再覆写 mousePressEvent 轻。
        self.grid.viewport().installEventFilter(self)
        self._delegate = ThumbDelegate(self.grid)
        # 缩略图唯一来源是窗口的 LRU 缓存 —— 委托画卡片时现查，
        # 不再把 QImage 塞进 item（那会让 LRU 失效，见 ThumbDelegate.image_of）。
        self._delegate.image_of = self._thumbs.get
        self.grid.setItemDelegate(self._delegate)
        # 列数不是固定的：按当前视口宽度反算（见 ThumbDelegate.fit_columns）。
        # 原来是读 cfg.ui.thumb_size 写死一个边长，宽窗口下右侧会空出将近一列。
        self.grid.setGridSize(self._delegate.sizeHint(None, None))
        self.splitter.addWidget(self.grid)

        # 空态：一张空网格是最显廉价的画面 —— 看着像坏了。
        # 给一句人话 + 一颗图标，明确"这里本来就该是空的"。
        self._empty = _EmptyState(self.grid.viewport())
        self._empty.hide()
        # 焦点给网格而不是搜索框 —— 否则一打开图库，搜索框就一直是聚焦态
        # （那条强调色描边会让用户以为"它在等我打字"），而且方向键 / Delete
        # 也不能直接用。
        self.grid.setFocus()

        self.detail = DetailPanel(self)
        self.detail.setMinimumWidth(320)
        self.detail.setMaximumWidth(460)
        self.detail.action = self._on_detail_action
        self.splitter.addWidget(self.detail)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setStretchFactor(2, 0)
        self.setCentralWidget(self.splitter)

        # --- 状态栏 ---
        self.status = self.statusBar()
        self._watch_dot = _StatusDot()
        self.status.addWidget(self._watch_dot)
        self._status_label = QLabel("")
        self._status_label.setProperty("role", "caption")
        self.status.addWidget(self._status_label)
        self._count_label = QLabel("")
        self._count_label.setProperty("role", "caption")
        self.status.addPermanentWidget(self._count_label)

    def resizeEvent(self, event) -> None:                          # noqa: N802
        super().resizeEvent(event)
        self._empty.setGeometry(self.grid.viewport().rect())
        self._refit_grid()
        # 拉宽窗口会多出新的列（也可能多出一整行），那些格子里还没有缩略图。
        # 不排一次补图的话，右侧会一直留着占位符，直到用户滚动才补上。
        self._schedule_request()

    def eventFilter(self, obj, event) -> bool:                     # noqa: N802
        """视口上的按下/松开 —— 卡片回弹反馈的触发点。

        只认左键，而且只在真的命中某张卡片时才动 —— 点在空白处不该有任何形变。
        ``Leave`` 也收掉：按住卡片往外拖出视口的话，没有这一条会永远停在
        "按下"的缩小状态。
        """
        if obj is self.grid.viewport():
            et = event.type()
            if et == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                item = self.grid.itemAt(event.position().toPoint())
                if item is not None:
                    self._delegate.press(item.data(Qt.UserRole), True)
            elif et in (QEvent.MouseButtonRelease, QEvent.Leave):
                self._delegate.clear_press()
        return super().eventFilter(obj, event)

    def _refit_grid(self) -> None:
        """视口宽度变了就重算缩略图边长，让每一行排满。

        窗口拉宽 / 收起侧栏 / 拖分栏都会走到这里。``setGridSize`` 之后
        QListWidget 会自己重新排布，不用动数据。
        """
        w = self.grid.viewport().width()
        if w <= 0:
            return
        # 目标边长跟着设置走。这里每次现读，改完设置不用重启图库。
        self._delegate.target_thumb = max(96, min(320, int(self.cfg.ui.thumb_size)))
        before = self._delegate.thumb
        self._delegate.fit_columns(w)
        # 加载尺寸同步成卡片边长的 1.3 倍（抗高分屏缩放），上限 256。
        # 多解码的像素只浪费内存：400 张 384px 解码 ≈ 384 MB 常驻。
        want = min(256, int(self._delegate.thumb * 1.3))
        if want != self._loader.size:
            self._loader.size = want
        if self._delegate.thumb == before:
            return
        # 卡片边长变了，舞台上缓存的位图尺寸也就作废了。
        self._delegate.invalidate_stage_cache()
        self.grid.setIconSize(QSize(self._delegate.thumb, self._delegate.thumb))
        self.grid.setGridSize(self._delegate.sizeHint(None, None))

    # ------------------------------------------------------------------ #
    # 数据加载
    # ------------------------------------------------------------------ #

    #: 查询结果上限。同时是"这次结果有没有被截断"的判据。
    _LIMIT = 1000

    def _fetch(self, text: str, cats: list[str], tags: list[str]) -> list[ShotRecord]:
        """按当前筛选取记录，能增量收窄就不碰数据库。

        实测账（890 条库，SQLite 页缓存冷的时候）：每按一个键全量查一次
        ``vault.search`` 要 200–730 ms —— 打字时连续几个键就是连续几次
        卡顿，这正是"用起来很卡"最直接的来源。而"接着上一个词往后打"时，
        新结果一定是旧结果的子集（见 :meth:`Vault.can_narrow`），在内存里
        按同样规则过滤只要 1–3 ms。

        缓存只在**文本变化、筛选条件没变**时有效。任何其他原因触发的
        reload（新截图入库、改了分类、切了筛选）都走全量查询并刷新缓存 ——
        否则新入库的图会迟迟不出现。
        """
        sig = (tuple(cats), tuple(tags),
               self.filters.day_from, self.filters.day_to)
        prev = self._last_fetch
        if prev is not None:
            old_text, old_records, old_sig, old_n = prev
            if (old_sig == sig and old_n < self._LIMIT
                    and old_text != text):
                narrowed = self.vault.narrow(old_text, old_records, text,
                                             limit=self._LIMIT)
                if narrowed is not None:
                    # 收窄结果不可能比旧结果多，所以也不会超 limit
                    self._last_fetch = (text, narrowed, sig, len(narrowed))
                    return narrowed

        try:
            if len(cats) == 1:
                records = self.vault.search(text, category=cats[0], tags=tags,
                                            day_from=self.filters.day_from,
                                            day_to=self.filters.day_to,
                                            limit=self._LIMIT)
            elif cats:
                seen: dict[str, ShotRecord] = {}
                for c in cats:
                    for r in self.vault.search(text, category=c, tags=tags,
                                               day_from=self.filters.day_from,
                                               day_to=self.filters.day_to,
                                               limit=self._LIMIT):
                        seen[r.rel_path] = r
                records = sorted(seen.values(),
                                 key=lambda r: r.captured_at or "", reverse=True)
            else:
                records = self.vault.search(text, tags=tags,
                                            day_from=self.filters.day_from,
                                            day_to=self.filters.day_to,
                                            limit=self._LIMIT)
        except Exception as exc:               # 图库损坏时不要让窗口崩掉
            self._status_label.setText(f"读取图库失败：{exc}")
            records = []

        self._last_fetch = (text, records, sig, len(records))
        return records

    def refresh_chrome(self) -> None:
        """设置保存后的轻量刷新：只更新"外壳"，不动记录。

        全量 :meth:`reload` 要清空缩略图缓存、重建几百个 item，实测 890 张
        76–400 ms。但改个主题、改个热键、改个分类名称之后，记录本身一张没变
        —— 这种情况走这里：左栏分类行的名称 / 图标 / 计数 / 顺序重排一遍
        （0.1 ms），卡片重绘（委托每次 paint 都现读 cfg，新图标新名字直接生效）。
        屏幕上连闪都不闪。
        """
        self.filters.refresh()
        self.grid.viewport().update()

    def reload(self) -> None:
        text = self.search.text().strip()
        cats = self.filters.selected_categories()
        tags = self.filters.selected_tags()
        records = self._fetch(text, cats, tags)
        self._records = {r.id: r for r in records}
        # **不清空缩略图缓存**。
        #
        # 改版前这里是 ``self._thumbs.clear()`` + ``self._loader.clear()``，
        # 每敲一个字、每点一次分类都会执行。后果是用户直接能看到的：
        # 网格先整屏退回「image-off」占位符，再一张张解码补回来 ——
        # 「打字搜索一顿一顿」「点分类卡一下」主要就是这一下。
        # 而且这批图上一秒才解过，纯属白干。
        #
        # 内存不需要靠清空来约束：``_thumbs`` 是 ``_thumbs_limit``(120) 上限的
        # LRU（见 ``_on_thumb_loaded``），不管切多少次筛选都不会涨过上限。
        # ``_loader.clear()`` 也一样该去掉 —— 它只清 ``_inflight`` 集合，
        # 在飞的解码任务并不会被取消，清掉只会让同一张图被重复排一次。
        self._refit_grid()

        # 重建 891 个 item 期间关掉重绘：
        #  1) ``sizeHint`` 提到循环外 —— 它内部要取字体度量，原来每张卡片算一次，
        #     891 次全是同一个值；
        #  2) ``setUpdatesEnabled(False)`` 让 addItem 不触发逐项重绘 ——
        #     否则边加边画，加完 891 个等于把整个网格画了好几遍。
        hint = self._delegate.sizeHint(None, None)
        self.grid.setUpdatesEnabled(False)
        try:
            self.grid.clear()
            self._item_of = {}
            for rec in records:
                item = QListWidgetItem()
                item.setData(Qt.UserRole, rec.id)
                item.setData(Qt.UserRole + 1, rec)
                item.setToolTip(f"{rec.filename}\n{rec.category}"
                                f"  {rec.time_human}\n{rec.rel_path}")
                item.setSizeHint(hint)
                self.grid.addItem(item)
                self._item_of[rec.id] = item
        finally:
            self.grid.setUpdatesEnabled(True)

        total = self.vault.count()
        # 没筛东西时不重复报一遍数字（"887 张" 和 "图库共 887 张" 并排显示很蠢）。
        self._status_label.setText(
            f"筛选出 {len(records)} 张" if len(records) != total else "")
        self._count_label.setText(f"共 {total} 张")
        self.filters.refresh()
        self._update_empty()
        self._request_visible()
        # 滚动条出现会把视口吃掉十几像素，等这一轮布局落定再fit一次，
        # 否则右侧会留下一条滚动条宽的锯齿边。
        QTimer.singleShot(0, self._refit_grid)

    def _update_empty(self) -> None:
        """空态：区分「图库真的没有」和「筛没了」—— 两种情况的下一步不一样。

        改版前两种情况都是一个空网格，用户分不清是自己筛错了还是程序没工作。
        """
        self._empty.setGeometry(self.grid.viewport().rect())
        if self.grid.count():
            self._empty.hide()
            return
        if self.filters.selected_categories() or self.search.text().strip():
            self._empty.set_message("没有匹配的截图",
                                    "换个关键词，或点左栏的「全部」")
        else:
            self._empty.set_message("这里还没有截图",
                                    "用 Win+Shift+S 截图，会自动命名并分类")
        self._empty.show()
        self._empty.raise_()

    def _schedule_request(self) -> None:
        """排一次补图，50 ms 内多次调用只生效一次。

        「已在计时就不重启」而不是 ``start()``：连续拖动滚动条时 valueChanged
        会不停触发，若每次重启就永远等不到到期，拖动期间一张图都不补。
        """
        if not self._req_timer.isActive():
            self._req_timer.start()

    def _grid_metrics(self) -> tuple[int, int, int]:
        """返回 ``(格子宽, 格子高, 每行列数)`` —— 全部按当前几何现算。

        ``setGridSize(sizeHint())`` 保证每个格子恰好是 sizeHint 那么大，
        所以 ``viewport 宽 // 格子宽`` 就是列数（fit_columns 反算边长时
        已经把右缘余量摊平了）。
        """
        hint = self._delegate.sizeHint(None, None)
        cell_w = max(1, hint.width())
        cell_h = max(1, hint.height())
        vp = self.grid.viewport().rect()
        cols = max(1, max(1, vp.width()) // cell_w)
        return cell_w, cell_h, cols

    def _visible_rows(self) -> tuple[int, int]:
        """当前视口覆盖的行号范围（闭区间）。

        改版前是拿视口**右下角**去 ``indexAt`` 取末行，取不到就退回
        ``count()-1``。实测 4 列卡片总宽 828、视口宽 840 —— 右下角那 12 px
        恒落在最后一列之后的留白里，``indexAt`` 每次都返回无效索引，
        于是每次滚动/搜索/筛选都退化成「遍历全库 891 张」。
        现在直接用几何算，不依赖布局是否已经跑过 ——
        ``reload()`` 末尾调用它时 item 刚 add 完还没布局，这一点很关键。
        """
        n = self.grid.count()
        if n <= 0:
            return 0, -1
        _w, cell_h, cols = self._grid_metrics()
        vp = self.grid.viewport().rect()
        top = self.grid.verticalScrollBar().value()
        row0 = top // cell_h
        row1 = (top + max(1, vp.height()) - 1) // cell_h
        last_row = (n - 1) // cols
        return max(0, row0), min(last_row, row1)

    def _request_visible(self) -> None:
        """请求视口内（以及上下各一屏）的缩略图。"""
        n = self.grid.count()
        if n == 0:
            return
        row0, row1 = self._visible_rows()
        if row1 < row0:
            return
        _w, cell_h, cols = self._grid_metrics()
        vp = self.grid.viewport().rect()
        per_screen = max(1, (max(1, vp.height()) + cell_h - 1) // cell_h)
        last_row = (n - 1) // cols
        # 上下各预取一屏，滚起来才不会先看到占位符再补图。
        r0 = max(0, row0 - per_screen)
        r1 = min(last_row, row1 + per_screen)
        # 按**整行**取 item 下标。
        # 改版前这里是 ``for row in range(start, end + 1): grid.item(row)`` ——
        # 把行号当成 item 下标用了，4 列时"三行"其实只覆盖前 3 个 item
        # （不到一行的四分之三），真正的可见区反而大面积请求不到。
        start = r0 * cols
        end = min(n - 1, (r1 + 1) * cols - 1)
        for i in range(start, end + 1):
            item = self.grid.item(i)
            if item is None:
                continue
            shot_id = item.data(Qt.UserRole)
            if shot_id in self._thumbs:
                continue
            rec = self._records.get(shot_id)
            if rec is None:
                continue
            self._loader.request(shot_id, self.vault.find_thumb(rec),
                                 self.vault.abs_path(rec))

    def _on_thumb_loaded(self, shot_id: int, img: QImage) -> None:
        self._thumbs[shot_id] = img
        while len(self._thumbs) > self._thumbs_limit:
            self._thumbs.popitem(last=False)
        # 图片不进 item（见 ThumbDelegate.image_of 的说明）。
        # **不再遍历全部 item 找 shot_id**：891 条库下，每有一张缩略图到齐就要
        # 走一遍 891 次 ``item.data()``，而滚动一屏会到齐几十张 ——
        # 一次滚动就是几万次 Python↔Qt 边界调用，全在 UI 线程上。
        # 用 reload 时建好的 id→item 字典，O(1)。
        item = self._item_of.get(shot_id)
        if item is not None:
            self.grid.update(self.grid.indexFromItem(item))

    # ------------------------------------------------------------------ #
    # 事件
    # ------------------------------------------------------------------ #

    def _on_filters_changed(self) -> None:
        self._pending_query.start()

    def _on_selection_changed(self) -> None:
        items = self.grid.selectedItems()
        now = {it.data(Qt.UserRole) for it in items}
        # 只把"刚变的那几张"交给委托去做淡入淡出 —— 整组一起动，
        # 全选 300 张也不会变成 300 个动画对象（见 ThumbDelegate.animate_selection）。
        entering = now - self._selected_ids
        leaving = self._selected_ids - now
        self._selected_ids = now
        self._delegate.animate_selection(entering, leaving)

        rec = self._records.get(items[0].data(Qt.UserRole)) if items else None
        self._current = rec
        self.detail.show_record(rec)
        if len(items) > 1:
            self._status_label.setText(f"已选 {len(items)} 张")

    #: 一格滚轮滚几行。1 行 = 一张卡片的高度。
    WHEEL_ROWS = 1.0

    def wheelEvent(self, event) -> None:                       # noqa: N802
        # 不再自己排 singleShot：滚动条的 valueChanged 已经会排。
        # 两边各排一个的话，一格滚轮就是两个定时器同时到期。
        dy = event.angleDelta().y()
        if not theme.animating() or dy == 0:
            super().wheelEvent(event)
            self._schedule_request()
            return
        # 滚轮改成"平滑追过去"，不再交给 Qt 瞬移 —— 见 _smooth_scroll_by。
        _w, cell_h, _cols = self._grid_metrics()
        self._smooth_scroll_by(-(dy / 120.0) * cell_h * self.WHEEL_ROWS)
        event.accept()
        self._schedule_request()

    def _smooth_scroll_by(self, amount: float) -> None:
        """把滚动位置**带缓动地**推向目标，而不是一格一跳。

        这是用户选的"跟手感"的另一半。Qt 默认滚轮是瞬移：画面直接跳一格，
        而缩略图还在陆续补进来 —— 两个东西节奏对不上，主观感受就是"一顿一顿"。
        加上缓动之后滚动本身有了速度感，补图和移动是一起完成的。

        实现上维护一个浮点目标位，每次滚轮**只更新目标**，动画负责追：
        连续滚动时表现为指数平滑，不会因为反复重启动画而抖。

        空闲时先把目标对齐到滚动条真实位置 —— 否则用户手动拖过滚动条、
        或者 reload() 把位置重置到 0 之后，下一次滚轮会先跳回旧目标。
        """
        sb = self.grid.verticalScrollBar()
        anim = self._scroll_anim
        if anim is None or anim.state() != QPropertyAnimation.Running:
            target = float(sb.value())
        else:
            target = self._scroll_target
        target = max(float(sb.minimum()),
                     min(float(sb.maximum()), target + amount))
        self._scroll_target = target

        if anim is None:
            anim = QPropertyAnimation(sb, b"value", self)
            anim.setEasingCurve(theme.EASE)
            self._scroll_anim = anim          # 持有，否则动画中途被回收
        anim.stop()                            # 从当前位重新起，避免叠加
        anim.setDuration(theme.MOTION["smooth"])
        anim.setStartValue(sb.value())
        anim.setEndValue(int(round(target)))
        anim.start()

    def keyPressEvent(self, event) -> None:                    # noqa: N802
        key = event.key()
        if key == Qt.Key_Delete:
            self._delete_selected()
            return
        if key in (Qt.Key_Return, Qt.Key_Enter):
            self._open_current()
            return
        if event.matches(QKeySequence.Copy):
            self._copy_current()
            return
        if event.matches(QKeySequence.Paste):
            self.paste_requested()
            return
        if key == Qt.Key_F5:
            self.reload()
            self._request_visible()
            return
        if key == Qt.Key_Escape:
            self.grid.clearSelection()
            return
        super().keyPressEvent(event)

    # ------------------------------------------------------------------ #
    # 动作
    # ------------------------------------------------------------------ #

    def paste_requested(self) -> None:
        """由外部（app.py）注入的"保存剪贴板图片"回调。"""
        cb = getattr(self, "on_paste", None)
        if callable(cb):
            cb()

    def _selected_records(self) -> list[ShotRecord]:
        out = []
        for item in self.grid.selectedItems():
            rec = self._records.get(item.data(Qt.UserRole))
            if rec is not None:
                out.append(rec)
        return out

    def _open_current(self) -> None:
        rec = self._current
        if rec is None:
            return
        try:
            os.startfile(str(self.vault.abs_path(rec)))
        except Exception as exc:
            self._status_label.setText(f"打开失败：{exc}")

    def _reveal_root(self) -> None:
        try:
            os.startfile(str(self.vault.root))
        except Exception as exc:
            self._status_label.setText(f"打开目录失败：{exc}")

    def _copy_current(self) -> None:
        rec = self._current
        if rec is None:
            return
        img = QImage(str(self.vault.abs_path(rec)))
        if img.isNull():
            self._status_label.setText("复制失败：无法读取图片")
            return
        QApplication.clipboard().setImage(img)
        self._status_label.setText(f"已复制到剪贴板：{rec.filename}")

    def _delete_selected(self) -> None:
        recs = self._selected_records()
        if not recs:
            return
        names = "、".join(r.filename for r in recs[:3])
        more = f" 等 {len(recs)} 个文件" if len(recs) > 3 else ""
        box = QMessageBox(self)
        box.setWindowTitle("删除截图")
        box.setText(f"确定删除 {names}{more}？")
        box.setInformativeText("文件会被移到回收站（可从回收站恢复）。")
        yes = box.addButton("删除", QMessageBox.DestructiveRole)
        box.addButton("取消", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is not yes:
            return
        for rec in recs:
            try:
                self.vault.delete(rec.id, remove_file=True)
            except Exception as exc:
                self._status_label.setText(f"删除 {rec.filename} 失败：{exc}")
        self.reload()
        self._status_label.setText(f"已删除 {len(recs)} 张")

    def _set_category(self, category: str) -> None:
        recs = self._selected_records()
        if not recs:
            return
        for rec in recs:
            try:
                self.vault.move(rec.id, category)
            except Exception as exc:
                self._status_label.setText(f"改分类失败：{exc}")
        self.reload()

    def _toggle_favorite(self) -> None:
        for rec in self._selected_records():
            try:
                self.vault.update(rec.id, favorite=0 if rec.favorite else 1)
            except Exception as exc:
                self._status_label.setText(f"更新失败：{exc}")
        self.reload()

    def _on_detail_action(self, action: str, payload) -> None:
        rec = self._current
        if rec is None:
            return
        if action == "open":
            self._open_current()
        elif action == "copy":
            self._copy_current()
        elif action == "reveal":
            try:
                subprocess.Popen(["explorer", "/select,", str(self.vault.abs_path(rec))])
            except Exception as exc:
                self._status_label.setText(f"定位失败：{exc}")
        elif action == "favorite":
            self._toggle_favorite()
        elif action == "delete":
            self._delete_selected()
        elif action == "category":
            self.vault.move(rec.id, str(payload))
            self.reload()
        elif action == "tags":
            tags = normalize_tags(payload)
            self.vault.update(rec.id, tags=tags)
            self.reload()
        elif action == "note":
            self.vault.update(rec.id, note=str(payload))
            self._status_label.setText("备注已保存")

    def _on_context_menu(self, pos: QPoint) -> None:
        item = self.grid.itemAt(pos)
        if item is not None and not item.isSelected():
            self.grid.clearSelection()
            item.setSelected(True)
        menu = QMenu(self)
        menu.addAction("打开", self._open_current)
        menu.addAction("复制图片", self._copy_current)
        menu.addAction("在资源管理器中定位", lambda: self._on_detail_action("reveal", None))
        menu.addSeparator()
        cat_menu = menu.addMenu("改为分类")
        for key in self.cfg.categories:
            a = cat_menu.addAction(self.cfg.category_label(key))
            a.triggered.connect(lambda _=False, k=key: self._set_category(k))
        menu.addAction("★ 收藏 / 取消", self._toggle_favorite)
        menu.addSeparator()
        menu.addAction("删除", self._delete_selected)
        menu.exec(self.grid.viewport().mapToGlobal(pos))

    # ------------------------------------------------------------------ #
    # 外部调用
    # ------------------------------------------------------------------ #

    def notify_saved(self, record: ShotRecord) -> None:
        """有新截图入库时由 app 调用：刷新、选中、并让它亮一下。

        「亮一下」（:meth:`ThumbDelegate.flash`）是整套"默默完成任务"设计里
        最重要的那一环 —— 它是**成功时唯一的视觉反馈**，而且发生在
        "东西出现的地方"，不是屏幕角落跳出来的弹窗。
        """
        self.reload()
        for row in range(self.grid.count()):
            item = self.grid.item(row)
            if item is not None and item.data(Qt.UserRole) == record.id:
                self.grid.setCurrentItem(item)
                self.grid.scrollToItem(item, QAbstractItemView.PositionAtCenter)
                self._delegate.flash(record.id)
                break

    def set_watch_state(self, active: bool, watched: int = 0) -> None:
        """状态栏的接管状态。让用户**随时能确认它还在跑**，不用等弹窗。

        这也是"不弹窗"能成立的另一半前提：既然不用弹窗汇报"我做完了"，
        就得有个常驻的、一眼可见的状态位来替代它。
        """
        self._watch_dot.set_active(active, watched)

    def show_and_raise(self) -> None:
        self.show()
        self.setWindowState(self.windowState() & ~Qt.WindowMinimized | Qt.WindowActive)
        self.raise_()
        self.activateWindow()


class _Preview(QWidget):
    """详情面板顶部的预览图。

    为什么不用 QLabel + setPixmap：QLabel 画不了圆角。图片四个角会是直角，
    贴在一张圆角卡片里，边角就"露馅"了 —— 这类 1px 级别的不整齐正是廉价感的
    来源。这里用 QPainterPath 裁出圆角再画图，边角干净。

    高度**贴着图片走**（见 ``_pref_height``）。改版前这里是
    ``lay.addWidget(self.preview, 3)``，等于把面板剩余高度全给它 —— 一张
    2520×1680 的网页截图等比缩到 150px 宽只剩 100px 高，剩下四百多像素是
    一整块灰底板。这才是"廉价"的真正来源：不是颜色不对，是空间在浪费。
    """

    #: 预览图再大也不许超过这个高度 —— 剩下的空间要留给文件名和元信息。
    MAX_PREVIEW_H = 240

    #: 空态里列的快捷键。**必须和 GalleryWindow.keyPressEvent 保持一致** ——
    #: 空态面板是用户唯一会认真读这块文字的时刻，这里写错就是教错。
    #: 内容取自 keyPressEvent 里实际处理的按键。
    SHORTCUTS = (
        ("Enter", "打开这张"),
        ("Ctrl+C", "复制图片"),
        ("Ctrl+V", "存入剪贴板图片"),
        ("Del", "删除选中"),
        ("F5", "刷新图库"),
    )

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._theme = "dark"
        self._pixmap: QPixmap | None = None
        self._empty_text = "选择一张截图"
        self._last_pref = -1
        self.setMinimumHeight(96)
        # 竖直方向用 Preferred 而不是 Expanding：空态时 sizeHint 返回一个极大的
        # 值，预览区就占满整块面板（"这里在等你选一张"）；有图时 sizeHint 按
        # 等比缩放后的真实高度给，面板贴着图片走，不再有灰底板。
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

    def set_theme(self, name: str) -> None:
        self._theme = "light" if name == "light" else "dark"
        self.update()

    def set_image(self, pixmap: QPixmap | None, empty_text: str) -> None:
        self._pixmap = pixmap if pixmap is not None and not pixmap.isNull() else None
        self._empty_text = empty_text
        self.updateGeometry()          # 高度换了，让父布局重排
        self.update()

    # -- 高度贴着图片走 ---------------------------------------------------- #

    def _pref_height(self) -> int:
        """这张图在**当前宽度**下等比缩放后需要多高。"""
        if self._pixmap is None:
            return 1 << 20            # 空态：占满面板
        w = self.width() - 2
        if w <= 0:
            return 180
        scaled = self._pixmap.scaled(w, self.MAX_PREVIEW_H, Qt.KeepAspectRatio,
                                     Qt.FastTransformation)
        # 完全贴住图片：不要再留"上下灰边"。96 是下限，防止极端细长的图
        # （比如一条很宽的状态栏）缩成一根线。
        return max(96, min(self.MAX_PREVIEW_H, scaled.height()))

    def sizeHint(self) -> QSize:
        return QSize(-1, self._pref_height())

    def minimumSizeHint(self) -> QSize:
        return QSize(140, 140)

    def resizeEvent(self, event) -> None:                        # noqa: N802
        super().resizeEvent(event)
        h = self._pref_height()
        if h != self._last_pref:      # 只在真的变了的时候重排，避免抖动
            self._last_pref = h
            self.updateGeometry()

    def paintEvent(self, _event) -> None:                          # noqa: N802
        t = theme.tokens(self._theme)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)

        box = self.rect().adjusted(0, 0, -1, -1)
        path = QPainterPath()
        path.addRoundedRect(box, theme.R_NEST, theme.R_NEST)
        p.setClipPath(path)
        p.fillPath(path, QColor(t["surface2"]))

        if self._pixmap is None:
            self._paint_empty(p, box, t)
        else:
            scaled = self._pixmap.scaled(box.size(), Qt.KeepAspectRatio,
                                         Qt.SmoothTransformation)
            x = box.left() + (box.width() - scaled.width()) // 2
            y = box.top() + (box.height() - scaled.height()) // 2
            p.drawPixmap(x, y, scaled)

        # 1px 描边。没有它，浅色主题下这块和窗口底色糊成一片，
        # 深色主题下又像"少了一块没画"。
        p.setPen(QPen(QColor(t["stroke"]), 1.0))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)
        p.end()

    def _paint_empty(self, p: QPainter, box: QRect, t: dict) -> None:
        """空态：图标 + 一句提示 + 一张快捷键速查。

        改版前这里只有居中的一行「选择一张截图」，面板剩下将近四百像素
        全是空的 —— 用户的原话是"详情面板空态太空"。

        补什么很关键：**加装饰只是把空变得不那么空**，所以这里补的是
        真正有用的东西 —— 图库的快捷键。空态是用户唯一会认真读这块面板的
        时刻，把快捷键摆在最显眼的位置，比给三个按钮加阴影有意义得多。

        整块内容垂直居中：原来图标是"居中往上挪 S3"的写法，加了内容之后
        会整体偏上，所以改成先算总高再定位。
        """
        fm_title = QFontMetrics(theme.ui_font(12))
        font_key = theme.mono_font(11)
        fm_key = QFontMetrics(font_key)
        font_label = theme.ui_font(12)
        fm_label = fm_title                     # 芯片与标签同一行高基准

        glyph = 26
        title_h = 20
        # 芯片宽按最长的那颗键算，保证标签左缘对齐
        chip_w = max(fm_key.horizontalAdvance(k) for k, _ in self.SHORTCUTS) + theme.S2 * 2
        chip_w = max(chip_w, 52)
        row_h = max(fm_key.height(), fm_label.height()) + theme.S1
        list_h = row_h * len(self.SHORTCUTS)

        block_h = (glyph + theme.S2 + title_h + theme.S4 + list_h)
        top = box.center().y() - block_h // 2

        g_rect = QRect(0, 0, glyph, glyph)
        g_rect.moveCenter(QPoint(box.center().x(), top + glyph // 2))
        icons.paint(p, "image-off", g_rect, t["fg3"], width=1.6)

        p.setFont(theme.ui_font(12))
        p.setPen(QColor(t["fg2"]))
        p.drawText(QRect(box.left(), g_rect.bottom() + theme.S2, box.width(), title_h),
                   Qt.AlignHCenter | Qt.AlignTop, self._empty_text)

        # 速查表整体居中，但不跟着面板一起变得很宽 —— 固定一个舒适的列宽。
        col_w = min(box.width() - theme.S4 * 2, chip_w + theme.S4 + 96)
        if col_w <= chip_w:                       # 面板极窄：只留图标和提示
            return
        left = box.center().x() - col_w // 2
        y = g_rect.bottom() + theme.S2 + title_h + theme.S4

        p.setFont(font_key)
        for key, label in self.SHORTCUTS:
            chip = QRect(left, y + (row_h - fm_key.height()) // 2 - 1,
                         chip_w, fm_key.height() + 2)
            # 芯片用 stage：它比预览区的 surface2 明确差一档，浅色深色都能
            # 看清边界。不用描边 —— 层级由面与底的分差表达。
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(t["stage"]))
            p.drawRoundedRect(chip, theme.R_CTL, theme.R_CTL)
            p.setPen(QColor(t["fg2"]))
            p.drawText(chip, Qt.AlignCenter, key)

            p.setFont(font_label)
            p.setPen(QColor(t["fg3"]))
            p.drawText(QRect(chip.right() + theme.S3, y,
                             col_w - chip_w - theme.S3, row_h),
                       Qt.AlignLeft | Qt.AlignVCenter, label)
            p.setFont(font_key)
            y += row_h


class DetailPanel(QWidget):
    """右侧详情：预览 + AI 描述 + 分类 + 三个动作。

    信息层级按"哪一条最该先被看到"排：**AI 描述在最上面**（文件名就是照它
    生成的），然后是文件名本身、再是元信息、最后才是操作按钮。
    改版前是 文件名 → 描述 → 元信息 平铺三行，字号只差 1px，
    等于没有层级 —— 眼睛不知道该先看哪。

    这块面板只讲"命名与分类"一件事：收藏 / 标签 / 备注都不在这条线上，
    全部拿掉了。
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.action = None                      # (action, payload) -> None
        self.rec: ShotRecord | None = None
        self._theme = "dark"

        lay = QVBoxLayout(self)
        lay.setContentsMargins(theme.S4, theme.S4, theme.S4, theme.S4)
        lay.setSpacing(theme.S3)

        self.preview = _Preview()
        lay.addWidget(self.preview)

        # 没选中任何一张时，下面这一整块收起来 —— 否则会出现"一片空白 +
        # 三行只有标签没有值的元信息"，看着像加载失败。
        # 收起之后预览区自己占满面板，语义明确：这里在等你选一张。
        self._info = QWidget()
        info = QVBoxLayout(self._info)
        info.setContentsMargins(0, 0, 0, 0)
        info.setSpacing(theme.S3)

        # --- AI 描述：用 sparkles 图标点出"这是 AI 说的" ---
        self._desc_row = QWidget()
        desc_lay = QHBoxLayout(self._desc_row)
        desc_lay.setContentsMargins(0, 0, 0, 0)
        desc_lay.setSpacing(theme.S2)
        self._desc_icon = _IconLabel("sparkles", theme.S2)
        desc_lay.addWidget(self._desc_icon, 0, Qt.AlignTop)
        self.desc = QLabel("")
        self.desc.setWordWrap(True)
        self.desc.setProperty("role", "hero")
        self.desc.setFont(theme.ui_font(14, QFont.Medium))
        desc_lay.addWidget(self.desc, 1)
        info.addWidget(self._desc_row)

        # --- 文件名：等宽 + 可选中，因为用户经常要复制它 ---
        self.title = QLabel("")
        self.title.setWordWrap(True)
        self.title.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.title.setProperty("role", "path")
        self.title.setFont(theme.mono_font(12))
        info.addWidget(self.title)

        info.addWidget(_Rule())

        # --- 元信息：四行图标 + 值。 ---
        # 为什么拆成四行：原来是三行，其中一行要同时塞"尺寸 · 大小 · 置信度"，
        # 在 276px 宽的面板里必然被省略号吃掉末尾 —— 而"置信度"恰恰是用户
        # 判断"这张分类得准不准"最需要的一个数。拆开之后每行只放一件事。
        self.meta_cat = _MetaRow("tag", "分类")
        self.meta_size = _MetaRow("image", "尺寸")
        self.meta_when = _MetaRow("folder-clock", "时间")
        self.meta_from = _MetaRow("camera", "来源")
        for row in (self.meta_cat, self.meta_size, self.meta_when, self.meta_from):
            info.addWidget(row)

        info.addWidget(_Rule())

        # --- 换分类：紧凑组合框，前面带图标 ---
        cat_row = QHBoxLayout()
        cat_row.setContentsMargins(0, 0, 0, 0)
        cat_row.setSpacing(theme.S2)
        self._cat_label = QLabel("改为")
        self._cat_label.setProperty("role", "caption")
        cat_row.addWidget(self._cat_label)
        self.cat = QComboBox()
        self.cat.currentIndexChanged.connect(self._on_cat_changed)
        cat_row.addWidget(self.cat, 1)
        info.addLayout(cat_row)

        lay.addWidget(self._info)
        # 剩余空间留给底部。不要让它往上挤成一块灰底板 —— 见 _Preview 的说明。
        lay.addStretch(1)

        # --- 动作：图标 + 文字，ghost 样式（平时无底，hover 才浮起来） ---
        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 0, 0, 0)
        btn_row.setSpacing(theme.S1)
        self._buttons = []
        for icon_name, text, act in (("external-link", "打开", "open"),
                                     ("copy", "复制", "copy"),
                                     ("folder-search", "定位", "reveal")):
            b = QPushButton(text)
            b.setProperty("variant", "ghost")
            b.setIconSize(QSize(15, 15))
            b.clicked.connect(lambda _=False, a=act: self._emit(a, None))
            btn_row.addWidget(b)
            self._buttons.append((b, icon_name))
        btn_row.addStretch(1)
        lay.addLayout(btn_row)

        self.set_theme("dark")
        # 初始态必须显式走一遍 show_record(None)：``_info`` 是新 QWidget，
        # 默认可见，而 show_record 只在"选中变化"时才被调用。不主动初始化的话，
        # 打开图库、一张都没选时，面板上会挂着六行只有标签没有值的元信息，
        # 三个按钮还能点 —— 实测截图抓到的，不是假想。
        self.show_record(None)

    # -- 主题 -------------------------------------------------------------- #

    def set_theme(self, name: str) -> None:
        self._theme = "light" if name == "light" else "dark"
        t = theme.tokens(self._theme)
        self.preview.set_theme(name)
        for row in (self.meta_cat, self.meta_size, self.meta_when, self.meta_from):
            row.set_theme(name)
        self._desc_icon.set_theme(name)
        # 之前这里是 10 个 setStyleSheet，每个都写死颜色 —— 主题切换要靠
        # 手工同步 4 组色值，漏一个就是"改了主题还剩一条深色的字"。
        # 现在颜色走 QSS 的角色选择器，字体族走 ui_font()（逐字回退链）。
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        """禁用态要把图标一起调暗。

        QSS 的 ``:disabled`` 只能改文字色，管不了 QIcon 里已经栅格化好的位图 ——
        所以禁用态只靠 QSS 的话，图标还是亮的那一档，一眼看不出"点不了"。
        """
        t = theme.tokens(self._theme)
        for button, icon_name in self._buttons:
            color = t["fg3"] if not button.isEnabled() else t["fg2"]
            button.setIcon(icons.icon(icon_name, color, 15))

    # -- 交互 -------------------------------------------------------------- #

    def _emit(self, action: str, payload) -> None:
        if callable(self.action):
            self.action(action, payload)

    def _on_cat_changed(self, _index: int) -> None:
        if self.rec is None:
            return
        key = self.cat.currentData()
        if key and key != self.rec.category:
            self._emit("category", key)

    # -- 展示 -------------------------------------------------------------- #

    def show_record(self, rec: ShotRecord | None) -> None:
        was_empty = self.rec is None
        self.rec = rec
        if rec is None:
            self.preview.set_image(None, "选择一张截图")
            self._info.setVisible(False)
            for button, _n in self._buttons:
                button.setEnabled(False)
            self._sync_buttons()
            return

        self._info.setVisible(True)
        # 空态 → 有内容：整块淡入，这是"选中了一张"最该被看见的时刻。
        # 已经有内容再换一张（方向键连着翻）：只从 62% 回到全显 ——
        # 每次都归零的话会闪成一片白，比不加动画更难受。
        # 时长走 MOTION["enter"]：用户说"感受不到动画"，150ms 的浅色插值
        # 眼睛抓不到，进场合该长一档。
        if was_empty:
            theme.fade_in(self.preview, theme.MOTION["enter"])
            theme.fade_in(self._info, theme.MOTION["enter"])
        else:
            theme.fade_in(self.preview, theme.MOTION["fast"], start=0.62)

        parent = self.window()
        vault = getattr(parent, "vault", None)
        cfg = getattr(parent, "cfg", None)
        path = vault.abs_path(rec) if vault is not None else None

        img = QImage(str(path)) if path is not None else QImage()
        if img.isNull():
            self.preview.set_image(None, "无法读取图片")
        else:
            self.preview.set_image(QPixmap.fromImage(img), "")

        # AI 描述是这块面板的主角，没有就整行收起来，不留一条空行。
        self._desc_row.setVisible(bool(rec.ai_desc))
        self.desc.setText(rec.ai_desc or "")

        self.title.setText(rec.filename)
        self.title.setToolTip(rec.rel_path)

        self.cat.setEnabled(True)
        for button, _n in self._buttons:
            button.setEnabled(True)
        self._sync_buttons()

        self.cat.blockSignals(True)
        self.cat.clear()
        if isinstance(cfg, Config):
            for key in cfg.categories:
                self.cat.addItem(icons.icon(cfg.category_icon(key),
                                            theme.tokens(self._theme)["fg2"], 15),
                                 cfg.category_label(key), key)
        else:
            self.cat.addItem(rec.category, rec.category)
        idx = self.cat.findData(rec.category)
        if idx >= 0:
            self.cat.setCurrentIndex(idx)
        self.cat.blockSignals(False)

        label = (cfg.category_label(rec.category)
                 if isinstance(cfg, Config) else rec.category)
        conf = f"· AI 置信度 {rec.confidence:.0%}" if rec.confidence else ""
        self.meta_cat.set_value(f"{label}  {conf}".strip())
        bits = [f"{rec.width}×{rec.height}" if rec.width else "", rec.size_human]
        self.meta_size.set_value("  ·  ".join(b for b in bits if b))
        self.meta_when.set_value(rec.time_human)
        src = [rec.source or "", rec.app or ""]
        self.meta_from.set_value("  ·  ".join(s for s in src if s))

        tip = [rec.reason or "", rec.rel_path]
        if rec.window_title:
            tip.insert(0, rec.window_title)
        self.meta_cat.setToolTip("\n".join(t for t in tip if t))

        # 换选中项时内容淡入 —— 否则六行文字"啪"地整块换掉，眼睛要重新找
        # 刚才在看的那一行。150ms OutCubic 刚好够看出"换了"又不耽误读。
        theme.fade_in(self._info)

    def resizeEvent(self, event) -> None:              # noqa: N802
        super().resizeEvent(event)
        if self.rec is not None:
            self.show_record(self.rec)


class _IconLabel(QWidget):
    """固定尺寸的一颗图标。"""

    def __init__(self, icon_name: str, size: int = 14, parent=None) -> None:
        super().__init__(parent)
        self.icon_name = icon_name
        self._theme = "dark"
        self.setFixedSize(size, size)

    def set_theme(self, name: str) -> None:
        self._theme = "light" if name == "light" else "dark"
        self.update()

    def paintEvent(self, _event) -> None:                          # noqa: N802
        t = theme.tokens(self._theme)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        icons.paint(p, self.icon_name, self.rect(), t["accent"], width=2.0)
        p.end()


class _MetaRow(QWidget):
    """一行元信息：左侧图标 + 名称，右侧值。固定 22px 高，右列能对齐。"""

    def __init__(self, icon_name: str, label: str, parent=None) -> None:
        super().__init__(parent)
        self.icon_name = icon_name
        self.label = label
        self.value = ""
        self._theme = "dark"
        self.setFixedHeight(22)

    def set_theme(self, name: str) -> None:
        self._theme = "light" if name == "light" else "dark"
        self.update()

    def set_value(self, value: str) -> None:
        if value != self.value:
            self.value = value
            self.update()

    def paintEvent(self, _event) -> None:                          # noqa: N802
        t = theme.tokens(self._theme)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        glyph = 13
        g_rect = QRect(0, (self.height() - glyph) // 2, glyph, glyph)
        icons.paint(p, self.icon_name, g_rect, t["fg3"], width=2.0)

        label_w = 40
        p.setFont(theme.ui_font(12))
        p.setPen(QColor(t["fg3"]))
        p.drawText(QRect(g_rect.right() + theme.S1, 0, label_w, self.height()),
                   Qt.AlignLeft | Qt.AlignVCenter, self.label)

        left = g_rect.right() + theme.S1 + label_w
        avail = self.width() - left
        if avail <= 0:
            p.end()
            return
        fm = QFontMetrics(theme.ui_font(12))
        p.setPen(QColor(t["fg2"]))
        p.drawText(QRect(left, 0, avail, self.height()),
                   Qt.AlignLeft | Qt.AlignVCenter,
                   fm.elidedText(self.value, Qt.ElideRight, avail))
        p.end()


class _Rule(QFrame):
    """一条 1px 的分隔线。用它而不是给每个块加边框 —— 少一层视觉噪声。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(1)
        self.setProperty("role", "hline")


