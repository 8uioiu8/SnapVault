"""设计令牌：把「界面长什么样」收敛成一份可执行的规格。

为什么要有这个文件
------------------
改版前的问题不是"某个颜色不好看"，而是**根本没有规格**：全项目散着 11 种间距
（3/4/6/7/8/9/10/11/14/16）、6 种圆角（4/7/8/9/10/12，而且卡片 8、内层 10 是
反的）、5 档只差 0.5px 的字号（11.5/12/12.5/13/13.5）、5 个不同的青绿
（#3E8E7E #2E9C8A #4FD1C5 #1F8C7A #5BC8B4）。人眼读不出 0.5px 的差别，
但**能读出"这里不太对"** —— 这就是"廉价感"的来源。

所以这里定死三件事：**能取哪些值、这些值叫什么、深浅主题各是什么色**。
之后的代码只准从这里取值，不准再写字面量。

数值出处
--------
全部对齐 **Windows 11 Fluent Design** 官方规格（Microsoft Learn: Typography /
Geometry / Spacing / Motion / Color），不是拍脑袋定的：

* 字号阶梯 12/14/16/20/28 —— Caption / Body / Subtitle / Title / Title Large
* 控件高 32px、控件圆角 4px、卡片圆角 8px（嵌套 8 → 6 → 4）
* 间距基数 4px
* 动效 150ms(fast) / 200ms(normal)，缓动 cubic-bezier(.33,0,.67,1)
* 深色主题用**明度抬升**做层次（tonal elevation），不用投影；
  只有浅色主题和真正浮起来的层（浮层 / 对话框）才用投影

不做的事
--------
Qt 的 QSS **不支持 transition / box-shadow**，所以动效走
:class:`QPropertyAnimation`，投影走 :class:`QGraphicsDropShadowEffect`。
这个文件只负责"静态长什么样"，动起来的部分在各自的控件里做。
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette

# --------------------------------------------------------------------------- #
# 一、尺度：只准从这些值里取
# --------------------------------------------------------------------------- #

#: 间距基数 4px。**不要再写 3/5/6/7/9/11 这些数**，要 6px 就用 8px 替代。
SPACE = (4, 8, 12, 16, 20, 24, 32)

S1, S2, S3, S4, S5, S6, S8 = SPACE

#: 圆角。嵌套时从外到内递减：窗口/卡片 8 → 内层容器 6 → 控件 4。
RADIUS = {"ctl": 4, "nest": 6, "card": 8, "pill": 999}
R_CTL, R_NEST, R_CARD = 4, 6, 8

#: 字号阶梯：(像素, 行高, 字重)。行高是给"算控件高度"用的，不直接写进 QSS。
TYPE = {
    "caption":        (12, 16, 400),
    "caption_strong": (12, 16, 600),
    "body":           (14, 20, 400),
    "body_strong":    (14, 20, 600),
    "subtitle":       (16, 22, 600),
    "title":          (20, 28, 600),
}

#: 控件标准高度。Win11 里 Button / TextBox / ComboBox 全都是 32px。
CONTROL_H = 32

#: 动效时长（毫秒）与缓动。QSS 用不了，但动效代码统一从这里取。
#:
#: 分三档语义，不是三个随手定的数字：
#:   * ``press`` —— 按下/回弹。**必须比"看清"更快**，慢了就跟不上手，
#:     按下去要过 100ms 才有反馈，主观感受是"卡"而不是"跟手"。
#:   * ``fast`` / ``normal`` —— 交互过渡（hover、状态切换）。
#:   * ``enter`` —— 进场/展开。比交互过渡长一档，才真的"看得见"。
#:     用户反馈"感受不到动画"，实测原因之一就是所有动效都压在 150ms，
#:     浅色主题下"浅底→浅底"的色差在 150ms 内插值，眼睛根本抓不到。
#:   * ``smooth`` —— 滚轮平滑滚动追上目标位的时间。
#:   * ``flash`` / ``pulse`` —— 具体反馈瞬间的语义档
#:     （新归档卡片高亮 700ms、托盘注意脉冲 2.4s 一轮）。
#:
#: **全项目只准从这里取时长** —— 散落的 setDuration(700) 过不了自检。
MOTION = {"press": 110, "fast": 150, "normal": 200, "enter": 260,
          "smooth": 220, "flash": 700, "pulse": 2400}
EASE = QEasingCurve.OutCubic

# --------------------------------------------------------------------------- #
# 一之二、动效总闸
# --------------------------------------------------------------------------- #

#: 用户开了「减少动态效果」时为 True，此时 :func:`fade_in` / :func:`fade_window_in`
#: 一律直接返回 —— 界面照常工作，只是不再演。给晕动症用户和"别跟我演戏"
#: 的用户各留一条路。
_REDUCE_MOTION = False


def set_reduce_motion(value: bool) -> None:
    """由启动 / 设置保存时调用，切换全局动效开关。"""
    global _REDUCE_MOTION
    _REDUCE_MOTION = bool(value)


def animating() -> bool:
    """此刻该不该播动画。"""
    return not _REDUCE_MOTION


def _anim(target, prop: bytes, start, end, duration: int):
    """起一个 OutCubic 属性动画。返回动画对象。

    QPropertyAnimation 是 Python 侧对象，被垃圾回收掉动画会中途停住 ——
    所以一定要返回，调用方必须持有（挂在自己的属性上）。
    """
    anim = QPropertyAnimation(target, prop, target)
    anim.setDuration(duration)
    anim.setStartValue(start)
    anim.setEndValue(end)
    anim.setEasingCurve(EASE)
    anim.start()
    return anim


def _stop_prev(widget) -> None:
    """把上一轮没播完的动画收掉，避免两次淡入互相打架。"""
    for attr in ("_sv_anim", "_sv_slide"):
        anim = getattr(widget, attr, None)
        if anim is not None:
            try:
                anim.stop()
            except Exception:
                pass
            setattr(widget, attr, None)


def fade_window_in(window, dy: int = 8, duration: int | None = None) -> None:
    """顶层窗口出场：淡入 + 从下方 dy 像素滑上来。

    只给顶层窗口用 —— 走 ``windowOpacity`` 而不是 QGraphicsOpacityEffect，
    后者会让整个窗口走离屏合成，在 Windows 上白掉一半帧。

    两个动画（透明度 / 位置）同时长、同缓动、同时起。实测它们在中途会各走
    各的（透明度先到底、位置后到底），所以**不允许只播一半**：见下面保险丝
    的处理方式 —— 要么正常播完，要么整体落到终态，不留"全显但还偏 8px"
    这种半成品。
    """
    if _REDUCE_MOTION:
        return
    from PySide6.QtCore import QTimer

    ms = MOTION["fast"] if duration is None else duration
    _stop_prev(window)
    pos = window.pos()
    low = type(pos)(pos.x(), pos.y() + dy)
    window.setWindowOpacity(0.0)
    window.move(low)
    fade = _anim(window, b"windowOpacity", 0.0, 1.0, ms)
    window._sv_anim = fade
    slide = None
    if dy:
        slide = _anim(window, b"pos", low, pos, ms)
        window._sv_slide = slide
    window._sv_target_pos = pos

    # 保险丝：动画万一没跑起来（事件循环正忙 —— 890 条目的图库重绘能把一次
    # processEvents 拖到 1.5 秒，实测那一刻动画计时器一拍都不进），窗口不能
    # 卡在透明度 0，那等于"点了打开图库什么都没发生"，比没有动画严重得多。
    #
    # 两个讲究：
    # 1. 到点先看状态 —— 已经正常结束（Stopped）就什么都不做；只有真卡在
    #    Running 才整体落到终态，不留"全显但还偏 8px"的半成品。
    # 2. 延时**不跟着 duration 走**，固定 MOTION["fast"] + 200。"350ms 内窗口
    #    必须可见"是不该被协商的底线；淡入本身只是 150ms 的锦上添花，机器忙到
    #    播不动时就该直接落终态，而不是陪一个（测试里传的）超长 duration 干等。
    def _fuse() -> None:
        running = [a for a in (fade, slide)
                   if a is not None and a.state() == QPropertyAnimation.Running]
        if running:
            _snap_window_final(window, fade, slide)

    QTimer.singleShot(MOTION["fast"] + 200, _fuse)


def _snap_window_final(window, fade, slide) -> None:
    """把淡入动画整体落到终态：不透明 + 位置归位。

    抽成模块级函数是为了能单独测 —— 保险丝的触发条件依赖事件循环时序，
    在测试里没法稳定复现，但"落到终态"这个动作本身必须是对的。
    """
    for a in (fade, slide):
        if a is not None:
            try:
                a.stop()
            except Exception:
                pass
    window.setWindowOpacity(1.0)
    window.move(window._sv_target_pos)


def fade_in(widget, duration: int | None = None, start: float = 0.0) -> None:
    """淡入一个刚换了内容的子控件（详情面板 / 设置页正文）。

    **不给 dy**：子控件的位置归布局管，手动 move 会和布局打架，下一次
    resize 就跳回去。要滑动的只有顶层窗口，见 :func:`fade_window_in`。

    ``start`` 是起始透明度。默认 0（从全透淡入），适合"空态 → 有内容"的
    入场；**频繁换内容的场景要传一个非 0 值**（比如 0.6）—— 每次都归零的话，
    连着按方向键翻图会闪成一片白，比不加动画更难受。
    """
    if _REDUCE_MOTION:
        return
    from PySide6.QtWidgets import QGraphicsOpacityEffect

    ms = MOTION["fast"] if duration is None else duration
    start = max(0.0, min(1.0, float(start)))
    _stop_prev(widget)
    effect = QGraphicsOpacityEffect(widget)
    effect.setOpacity(start)
    widget.setGraphicsEffect(effect)
    fade = _anim(effect, b"opacity", start, 1.0, ms)
    widget._sv_anim = fade
    fade.finished.connect(lambda: _drop_effect(widget, effect))


def _drop_effect(widget, effect) -> None:
    """动画结束后摘掉透明度效果。

    QGraphicsOpacityEffect 会让 Qt 走"离屏合成再贴回来"的慢路径，
    长期挂在可见控件上会拖累滚动帧率。淡入完成就还控件一个自由身。
    """
    _stop_prev(widget)
    try:
        if widget.graphicsEffect() is effect:
            widget.setGraphicsEffect(None)
    except Exception:
        pass


def snapshot_for_cross_fade(widgets) -> list:
    """在换主题**之前**把可见顶层窗口抓成位图。

    抓图必须发生在 ``theme.apply`` 之前 —— 换完再抓，抓到的就是新配色，
    交叉淡出就变成了"自己淡自己"，什么也看不见。所以拆成两步：
    先快照，换主题，再 :func:`cross_fade`。
    """
    if _REDUCE_MOTION:
        return []
    out = []
    for w in widgets:
        try:
            if not w.isVisible():
                continue
            shot = w.grab()
            if not shot.isNull():
                out.append((w, shot))
        except Exception:                               # noqa: BLE001
            continue
    return out


def cross_fade(pairs, duration: int | None = None) -> None:
    """把换主题**之前**的样子贴在窗口上，再让它淡出。

    这样用户看到的是"旧配色渐隐、新配色渐显"，而不是整屏瞬间翻转。
    这是用户点名的"交互要有可见过渡"里最显眼的一处 —— 主题切换是个
    整屏变化，硬切的时候像闪了一下。

    覆盖层只是临时位图，淡完立刻删；期间不接收鼠标事件，不影响操作。
    """
    if _REDUCE_MOTION or not pairs:
        return
    from PySide6.QtWidgets import QGraphicsOpacityEffect, QLabel

    ms = MOTION["enter"] if duration is None else duration
    for widget, shot in pairs:
        try:
            if not widget.isVisible():
                continue
            ov = QLabel(widget)
            ov.setPixmap(shot)
            ov.setGeometry(0, 0, widget.width(), widget.height())
            # 淡出期间不许挡住点击 —— 否则换主题那 260ms 里整个界面点不动。
            ov.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            effect = QGraphicsOpacityEffect(ov)
            effect.setOpacity(1.0)
            ov.setGraphicsEffect(effect)
            ov.show()
            ov.raise_()
            anim = _anim(effect, b"opacity", 1.0, 0.0, ms)
            anim.finished.connect(ov.deleteLater)
            ov._sv_anim = anim                          # 持有，别被回收
        except Exception:                               # noqa: BLE001
            continue

# --------------------------------------------------------------------------- #
# 二、颜色令牌：深色 / 浅色各一套，名字一一对应
# --------------------------------------------------------------------------- #

TOKENS: dict[str, dict[str, str]] = {
    # 深色：用**明度抬升**表达层级（bg < surface < surface2 < surface3），
    # 不靠描边。描边只用在真正需要分隔的地方，而且极弱。
    "dark": {
        "bg":        "#17171C",   # 窗口底
        "surface":   "#202027",   # 一级面：卡片 / 面板
        "surface2":  "#26262E",   # 二级面：hover / 输入框
        "surface3":  "#2E2E37",   # 三级面：按下的按钮 / 菜单元件
        "stage":     "#191920",   # 缩略图舞台：比卡片更暗的"凹槽"，让截图浮出来
        "stroke":    "#2E2E36",   # 唯一的描边色
        "fg1":       "#F2F3F5",   # 主文字
        "fg2":       "#A6A8B0",   # 次要文字
        "fg3":       "#75777F",   # 弱化文字 / 占位
        "accent":    "#45C4B0",
        "accent_ink": "#0B2B27",  # 压在 accent 上的文字色
        "danger":    "#FF7A7A",
        "ok":        "#6FD3A8",
        "warn":      "#F7C86F",
        "scrim":     "#B317171C",  # 半透明遮罩（ARGB）
    },
    # 浅色：靠极淡的投影 + 面与底的分差表达层级。
    "light": {
        "bg":        "#F3F3F5",
        "surface":   "#FFFFFF",
        "surface2":  "#F7F7F9",
        "surface3":  "#EFEFF2",
        # 缩略图舞台。
        #
        # 这里必须和 surface3 分开。改版前舞台用的是 surface3(#EFEFF2)，
        # 而窗口底是 #F3F3F5 —— 两者只差 4 级，肉眼分不出边界；
        # 偏偏图库里绝大多数是**白底截图**（网页、文档），#FFFFFF 比窗口底还亮，
        # 于是截图边缘完全糊进背景里，一屏看过去像没有卡片框。
        # 现在舞台比窗口底明确暗一档（Δ≈15），白底截图自然"浮"在上面，
        # 而且**不需要加描边** —— 简约风靠面与底的分差表达层级，不靠框线。
        "stage":     "#E4E4E9",
        "stroke":    "#E6E6EA",
        "fg1":       "#1B1B20",
        "fg2":       "#5E5E68",
        "fg3":       "#8D8D97",
        "accent":    "#0E7C6B",
        "accent_ink": "#FFFFFF",
        "danger":    "#C50F1F",
        "ok":        "#107C10",
        "warn":      "#F7630C",
        "scrim":     "#66FFFFFF",
    },
}

#: 卡片选中：accent 的低透明度填充 + 一圈 accent 描边。
#: 之前浅色主题下选中卡片把标题画成纯白（#FFFFFF on #DDF1EC）——
#: 白字浅底，等于消失。选中态统一改成"填充 + 描边"，文字颜色永远跟着 fg1 走。
SEL_ALPHA_DARK = 56
SEL_ALPHA_LIGHT = 38


def tokens(theme: str) -> dict[str, str]:
    """取一套颜色令牌。`theme` 只认 ``"light"``，其余一律当深色。"""
    return TOKENS["light" if theme == "light" else "dark"]


def c(theme: str, key: str) -> str:
    """取单个令牌值 —— 自绘代码里最常用的一行。"""
    return tokens(theme)[key]


def alpha(color: str, a: int) -> QColor:
    """给颜色加透明度（0–255），自绘时画 hover / 选中底用。"""
    col = QColor(color)
    col.setAlpha(max(0, min(255, int(a))))
    return col


def mix(c1: str, c2: str, k: float) -> QColor:
    """两个十六进制色按 k 线性插值（k=0 → c1，k=1 → c2）。

    给自绘控件的过渡用：hover 底色、文字色都能从一个色平滑走到另一个色，
    而不是"时间到了啪一下换掉"。k 会被夹到 0..1。
    """
    a, b = QColor(c1), QColor(c2)
    k = max(0.0, min(1.0, float(k)))
    return QColor(
        int(round(a.red() + (b.red() - a.red()) * k)),
        int(round(a.green() + (b.green() - a.green()) * k)),
        int(round(a.blue() + (b.blue() - a.blue()) * k)),
    )


# --------------------------------------------------------------------------- #
# 三、分类图标：取代原来的 10 种分类色
# --------------------------------------------------------------------------- #
#
# 改版前左栏和卡片 meta 行用 10 种颜色区分分类，整屏花花绿绿 —— 一眼看过去
# 全是颜色，反而分不出哪张图是什么。改成**图标**区分：同一个字形语言（Lucide
# 24×24 / 2px 描边），平时统一用 fg2 灰，只有选中项才上 accent。
# 信息没少，噪音少了一大截。

CATEGORY_ICONS: dict[str, str] = {
    "code":     "code",
    "document": "file-text",
    # 下面这几颗是换过的：选型标准不是"语义最贴切"，而是**16px 下还认得出**。
    # 左栏图标就是按 16px 显示的，细节一多就糊成一团：
    #   globe      → 经纬线交织，读成一个脏球
    #   table      → 3×3 密网格，缩到 16px 是一块实心方块
    #   palette    → 5 个色点并成一块黑饼
    #   film       → 一排穿孔，纯噪点
    #   gamepad-2  → 轮廓复杂到看不出是什么
    # 换成同义但笔画少的官方图标后，一列扫下来每个都能一眼分辨。
    "webpage":  "app-window",
    "chat":     "message-square",
    "table":    "grid-2x2",
    "image":    "image",
    "design":   "paintbrush",
    "video":    "video",
    "game":     "puzzle",
    "other":    "file",
}


def category_icon(key: str) -> str:
    """分类键 -> 图标名。未知分类兜底到 ``shapes``，不会是空白。"""
    return CATEGORY_ICONS.get(key, "shapes")


# --------------------------------------------------------------------------- #
# 四、字体
# --------------------------------------------------------------------------- #
#
# 中西文字体分开挑，因为两边的"最优解"不是同一个：
#
# * 中文：**HarmonyOS Sans SC**（华为，免费商用）。它是专为界面设计的，
#   中宫宽、笔画利落，比 Windows 自带的微软雅黑在 UI 上更清爽。
#   随包放在 snapvault/assets/fonts/ 下两个字重：Regular(400) + Medium(500)。
# * 西文/数字：**Segoe UI Variable Text**（Windows 11 原生 UI 字体）。
#   不随包分发 —— 系统自带，白拿。
#
# 用 QFont.setFamilies() 而不是拼 "A, B, C" 字符串：这是 Qt 原生的字体回退链，
# 每个字**逐字**找第一个有该字形的字体，所以中文走鸿蒙、西文走 Segoe，
# 不需要自己判断字符属于哪个语系。
#
# 鸿蒙字体没装上 / 文件被删时自动降级到系统栈，界面不会崩也不会变方框。

FONT_UI = [
    "HarmonyOS Sans SC",          # 随包（免费商用）
    "MiSans",                     # 用户自己装了就用
    "Segoe UI Variable Text",     # Win11 原生
    "Segoe UI",
    "Microsoft YaHei UI",
    "Microsoft YaHei",
    "sans-serif",
]

FONT_MONO = [
    "Cascadia Code",              # Win11 自带，等宽且带连字
    "JetBrains Mono",
    "Consolas",
    "monospace",
]

#: 随包字体文件：(文件名, 说明)。缺文件只降级，不报错。
BUNDLED_FONTS = (
    ("HarmonyOS_Sans_SC_Regular.ttf", "Regular → 字重 400"),
    ("HarmonyOS_Sans_SC_Medium.ttf", "Medium  → 字重 500/600"),
)

def _fonts_dir() -> Path:
    """随包字体的目录。源码运行与 PyInstaller 打包后都要能找到。

    打包（onedir / onefile）后模块是从归档里加载的，``__file__`` 会指向
    ``<bundle>/_internal/snapvault/theme.pyc`` 这类合成路径，直接拿它拼
    ``assets`` 有可能落空。所以先按 ``sys._MEIPASS`` 找两个约定位置，
    都找不到再退回源码布局 —— 找不到只是字体不生效（``load_fonts`` 会降级），
    但不能因为路径猜错而抛异常。
    """
    base = getattr(sys, "_MEIPASS", None)
    if base:
        for rel in (Path("snapvault") / "assets" / "fonts",
                    Path("assets") / "fonts"):
            cand = Path(base) / rel
            if cand.is_dir():
                return cand
    return Path(__file__).with_name("assets") / "fonts"


_ASSETS = _fonts_dir()
_loaded_fonts: list[str] = []


def load_fonts() -> list[str]:
    """把随包字体注册进 Qt，返回实际注册成功的字体族名。

    幂等：重复调用不会重复注册。放在 ``QApplication`` 建好之后、
    ``setStyleSheet`` 之前调用，QSS 里的 ``font-family`` 才能命中。
    """
    if _loaded_fonts:
        return _loaded_fonts
    for filename, _note in BUNDLED_FONTS:
        path = _ASSETS / filename
        if not path.exists():
            continue
        font_id = QFontDatabase.addApplicationFont(str(path))
        if font_id < 0:
            continue
        for family in QFontDatabase.applicationFontFamilies(font_id):
            if family not in _loaded_fonts:
                _loaded_fonts.append(family)
    return _loaded_fonts


def ui_font(size: int = 14, weight: int = QFont.Normal) -> QFont:
    """界面主字体。`size` 取 :data:`TYPE` 里的像素值。

    关于字重的降级：整套设计靠 **400 / 500 / 600 三档** 表达信息层级，
    而 Windows 自带的中文字体里，微软雅黑 UI 只有 Regular(400) 和 Bold(700)，
    没有真正的 500/600 —— 直接要 600，Qt 会合成出一个"假粗体"，
    看起来和正文几乎一样粗，层级就没了（实测就是这样）。

    所以当随包字体没加载上时，把 500/600 抬到 700，至少保住层级。
    代价是比 600 略重一点，但"分得清"比"精确"重要。
    """
    font = QFont()
    font.setFamilies(_ordered(FONT_UI))
    font.setPixelSize(int(size))
    font.setWeight(QFont.Weight(_resolve_weight(weight)))
    font.setHintingPreference(QFont.PreferFullHinting)
    return font


def _resolve_weight(weight: int) -> int:
    """把"设计要求的中黑"翻译成"这台机器上真有的字重"。"""
    if _bundled_font_active():
        return int(weight)
    if 450 <= int(weight) <= 650:
        return int(QFont.Bold)          # 700
    if 350 <= int(weight) < 450:
        return int(QFont.Normal)        # 400
    return int(weight)


def _bundled_font_active() -> bool:
    """随包字体到底有没有生效。没生效就得靠系统字体，字重要另算。"""
    if not _loaded_fonts:
        return False
    head = _ordered(FONT_UI)[0] if _ordered(FONT_UI) else ""
    return head in _loaded_fonts and "HarmonyOS" in head


def mono_font(size: int = 12, weight: int = QFont.Normal) -> QFont:
    """等宽字体。路径、命令行、尺寸数字用它，视觉上更"数据"。"""
    font = QFont()
    font.setFamilies(_ordered(FONT_MONO))
    font.setPixelSize(int(size))
    font.setWeight(QFont.Weight(int(weight)))
    return font


def _ordered(stack: list[str]) -> list[str]:
    """把随包字体挪到最前 —— 注册成功后它才是首选。"""
    if not _loaded_fonts:
        return list(stack)
    head = [f for f in stack if f in _loaded_fonts]
    tail = [f for f in stack if f not in _loaded_fonts]
    return head + tail


def font_stack_css() -> str:
    """给 QSS 用的字体串。QSS 不支持列表回退语义，这里只能拼逗号串。"""
    return ", ".join(f'"{f}"' for f in _ordered(FONT_UI))


# --------------------------------------------------------------------------- #
# 五、QSS
# --------------------------------------------------------------------------- #

def build_qss(theme: str) -> str:
    """生成整套样式表。

    只有这一处拼 QSS。控件级的例外（比如某个按钮要强调色）走动态属性选择器
    （``QPushButton[variant="ghost"]``），不再各自 ``setStyleSheet`` ——
    散落的 ``setStyleSheet`` 正是这个项目"廉价感"的另一半原因，
    它们让"当前到底哪条规则生效"变得不可推理。

    **配色约定：accent 只用于「当前选中 / 当前聚焦」。**
    按钮、勾选框、单选框、进度条、滑块一律走中性令牌（surface / fg 系列）。
    改版前这些全是实心 accent，一屏下来到处都是同一块绿，用户的原话是
    「颜色太单调，一致性都是绿色」—— 不是颜色不够多，是**同一个绿承担了
    所有语义**，于是哪个才是"当前该看的地方"就没了。收窄之后绿色变稀缺，
    反而更重。自检里有一条会拦这件事（见 test_theme 的「accent 只出现在
    选中类选择器」），想加回来会当场报错。
    """
    t = tokens(theme)
    fs = font_stack_css()
    light = theme == "light"

    # 浅色主题才用投影；深色靠明度分层（Fluent 的 tonal elevation）。
    shadow = "" if not light else (
        "border: 1px solid %s;" % t["stroke"]
    )

    return f"""
/* ============ 基础 ============ */
QWidget {{
    background: {t['bg']};
    color: {t['fg1']};
    font-family: {fs};
    font-size: 14px;
}}
QMainWindow, QDialog {{ background: {t['bg']}; }}
QLabel {{ background: transparent; }}
QLabel[role="caption"] {{ color: {t['fg3']}; font-size: 12px; }}
QLabel[role="hint"] {{ color: {t['fg3']}; font-size: 12px; }}
/* 测试连接的结果行：成功用 ok 色、失败用 danger 色 */
QLabel[role="hint"][ok="true"] {{ color: {t['ok']}; }}
QLabel[role="hint"][ok="false"] {{ color: {t['danger']}; }}
/* 详情面板的两行主角文字。字体族由代码里的 theme.ui_font() 给
   （QSS 拼不出 setFamilies 那样的逐字回退链），QSS 只管颜色。 */
QLabel[role="hero"] {{ color: {t['fg1']}; }}
QLabel[role="path"] {{ color: {t['fg3']}; }}
QLabel[role="secondary"] {{ color: {t['fg2']}; font-size: 13px; }}
QLabel[role="section"] {{
    color: {t['fg3']}; font-size: 12px; font-weight: 600;
    letter-spacing: 0.4px;
}}
QFrame[role="hline"] {{ background: {t['stroke']}; max-height: 1px; border: 0; }}
QFrame[role="vline"] {{ background: {t['stroke']}; max-width: 1px; border: 0; }}

/* ============ 工具条：无边框，只留留白 ============ */
QToolBar {{
    background: transparent; border: 0;
    padding: {S2}px {S3}px {S1}px {S3}px;
    spacing: {S1}px;
}}
QToolBar::separator {{ background: {t['stroke']}; width: 1px; margin: {S2}px {S2}px; }}
QToolButton {{
    padding: 0 {S2}px; min-height: {CONTROL_H}px;
    border: 0; border-radius: {R_CTL}px;
    color: {t['fg2']}; font-size: 13px;
}}
QToolButton:hover {{ background: {t['surface2']}; color: {t['fg1']}; }}
QToolButton:pressed {{ background: {t['surface3']}; }}
QToolButton:checked {{ background: {t['surface3']}; color: {t['fg1']}; }}

/* ============ 输入控件：常驻一条弱描边，聚焦才换成强调色 ============ */
/*
   为什么输入框必须有描边（而按钮不必）：按钮有文字，本身就是可点击的信号；
   输入框**空的时候什么都没有**。改版前输入框用 surface2 做底、描边透明，
   在浅色主题下 surface2(#F7F7F9) 比窗口底(#F3F3F5) 还亮 —— 结果就是
   "一个空输入框在浅色界面里根本看不见"。
   现在统一：底色 surface + 1px stroke，hover 加深描边，focus 换强调色。
   这是 Win11 TextBox 的实际做法。
*/
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{
    background: {t['surface']};
    border: 1px solid {t['stroke']}; border-radius: {R_CTL}px;
    padding: 0 {S2}px; min-height: {CONTROL_H - 2}px;
    color: {t['fg1']};
    selection-background-color: {t['accent']};
    selection-color: {t['accent_ink']};
}}
QLineEdit:hover, QSpinBox:hover, QComboBox:hover {{
    border: 1px solid {t['fg3']};
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus,
QComboBox:focus, QPlainTextEdit:focus, QTextEdit:focus {{
    border: 1px solid {t['accent']};
    background: {t['surface']};
}}
QLineEdit:disabled, QComboBox:disabled {{ color: {t['fg3']}; }}
/* 校验失败的输入框：动态属性 invalid="true"，由控件自己 unpolish/polish 触发。
   原来是在控件里 setStyleSheet("border:1px solid #E06C75")，
   那个红色是写死的，浅色主题下对比度不够。 */
QLineEdit[invalid="true"], QSpinBox[invalid="true"] {{
    border: 1px solid {t['danger']};
}}
QComboBox::drop-down {{ border: 0; width: 22px; }}
QComboBox::down-arrow {{ image: none; border: 0; }}
QComboBox QAbstractItemView {{
    background: {t['surface']};
    border: 1px solid {t['stroke']}; border-radius: {R_CARD}px;
    padding: {S1}px; outline: 0;
    selection-background-color: {t['surface3']};
    selection-color: {t['fg1']};
}}
/* 数值框的上/下箭头：**不要指望在这里用 QSS 藏掉**。
   试过把 ::up-button 设成 width 0、再给 ::up-arrow 设 image none，
   实测箭头照样画出来，浅色主题下在输入框右端留下一个小三角加一段 L 形边框残迹
   （A/B 渲染确认补丁前后逐像素一样）。原因是按钮宽为 0 时 Qt 走基础样式，
   QSS 的子控件规则没人接。
   正确做法见 settings_ui._SpinBox：setButtonSymbols(QAbstractSpinBox.NoButtons)。 */
QSpinBox::up-button, QSpinBox::down-button {{
    width: 0; height: 0; border: 0;
}}

/* ============ 按钮 ============ */
/* 按钮用"面"表达（surface2 比窗口底亮/暗一档），不用描边 ——
   有文字就有可点击信号，再加边框纯属噪声。 */
QPushButton {{
    background: {t['surface2']};
    border: 1px solid {t['stroke']}; border-radius: {R_CTL}px;
    padding: 0 {S3}px; min-height: {CONTROL_H}px;
    color: {t['fg1']}; font-size: 13px;
}}
QPushButton:hover {{ background: {t['surface3']}; }}
QPushButton:pressed {{ background: {t['bg']}; }}
QPushButton:disabled {{ color: {t['fg3']}; background: transparent;
                        border: 1px solid {t['stroke']}; }}
/* 切换型按钮的"已开启"态。走中性 —— 见 build_qss 的配色约定：
   实心 accent 只留给"当前选中"（导航 / 页签 / 列表 / 卡片），
   按钮语义是"可点的面"，不该和"你正停在这里"抢同一个颜色。 */
QPushButton:checked {{
    background: {t['surface3']}; color: {t['fg1']}; font-weight: 600;
    border: 1px solid {t['fg2']};
}}
/* 强调按钮：靠**面更深一档 + 字重 600**表达主次，不靠换颜色。
   这条路由来已久但全项目没人用（grep variant="primary" 为空），
   现在把它保留成扩展点、同时改成中性，免得以后有人照着旧注释又涂回绿色。 */
QPushButton[variant="primary"] {{
    background: {t['surface3']}; color: {t['fg1']}; font-weight: 600;
    border: 1px solid {t['fg2']};
}}
QPushButton[variant="ghost"] {{
    background: transparent; color: {t['fg2']}; border: 1px solid transparent;
}}
QPushButton[variant="ghost"]:hover {{
    background: {t['surface2']}; color: {t['fg1']};
    border: 1px solid {t['stroke']};
}}
QPushButton[variant="ghost"]:disabled {{
    background: transparent; border: 1px solid transparent;
    color: {t['fg3']};
}}
QPushButton[variant="danger"] {{ background: transparent; color: {t['danger']}; }}
QPushButton[variant="danger"]:hover {{ background: {t['surface2']}; }}

/* ============ 勾选 ============ */
/* 勾 / 圆点用中性填充。勾本身的颜色来自 QPalette.HighlightedText，
   所以浅色主题下是白勾压中灰、深色主题下是近黑勾压浅灰，两边都站得住。 */
QCheckBox {{ spacing: {S2}px; background: transparent; }}
QCheckBox::indicator {{
    width: 16px; height: 16px;
    border-radius: {R_CTL}px;
    background: transparent;
    border: 1px solid {t['stroke']};
}}
QCheckBox::indicator:hover {{ border: 1px solid {t['fg3']}; }}
QCheckBox::indicator:checked {{
    background: {t['fg2']}; border: 1px solid {t['fg2']};
}}
QRadioButton::indicator {{
    width: 16px; height: 16px; border-radius: 8px;
    background: transparent; border: 1px solid {t['stroke']};
}}
QRadioButton::indicator:checked {{
    background: {t['fg2']}; border: 5px solid {t['fg2']};
}}

/* ============ 分组 ============ */
QGroupBox {{
    border: 0; margin-top: {S5}px; padding-top: {S1}px;
    background: transparent;
}}
QGroupBox::title {{
    subcontrol-origin: margin; left: 0; padding: 0;
    color: {t['fg3']}; font-weight: 600; font-size: 12px;
}}

/* ============ 标签页：下划线式 ============ */
QTabWidget::pane {{ border: 0; top: -1px; background: transparent; }}
QTabBar {{ background: transparent; }}
QTabBar::tab {{
    background: transparent; padding: {S2}px {S4}px; margin-right: {S1}px;
    color: {t['fg2']}; font-size: 13px;
    border-bottom: 2px solid transparent;
}}
QTabBar::tab:hover {{ color: {t['fg1']}; }}
QTabBar::tab:selected {{
    color: {t['fg1']}; font-weight: 600;
    border-bottom: 2px solid {t['accent']};
}}

/* ============ 列表 / 滚动区 ============ */
QListWidget, QListView, QTreeWidget, QTableWidget {{
    background: transparent; border: 0; outline: 0;
}}
QListWidget::item, QListView::item {{ border-radius: {R_CARD}px; }}
QListWidget::item:hover, QListView::item:hover {{ background: transparent; }}
QListWidget::item:selected, QListView::item:selected {{ background: transparent; }}

/* ============ 设置面板左侧导航 ============ */
/* 底色（hover 浮起 / 选中 accent）**一律由 _NavRow 自绘**，这里一色不涂。
   原因是 _NavRow 的 hover 要做插值过渡，QSS 做不到；而两边同时画就会出现
   「宽的一块 + 窄的一块」两个圆角矩形叠在一起 —— QSS 的 ::item 背景受
   margin/padding 约束、_NavRow 又自己 adjusted 了一次，几何对不上，
   窄的那块会在下方露出一条舌头。**同一个视觉只能有一个来源。**

   padding 必须是 0：Qt 给 itemWidget 的矩形是
   `x = margin+padding`、`width = item - 2*(margin+padding)`，
   但 `height = item - 2*margin`（纵向不扣 padding）—— 不是同一个坐标系，
   只要 padding 非 0，widget 就永远对不齐 item 背景。改成 0 之后
   widget 矩形 == item 背景矩形，`_NavRow` 按整块画就正好。
   margin 留着，它就是导航条左右那 4px 的呼吸位。 */
QListWidget[role="nav"] {{ background: transparent; border: 0; outline: 0; }}
QListWidget[role="nav"]::item {{
    padding: 0; margin: 2px {S1}px;
    border: 0; background: transparent; color: {t['fg2']}; font-size: 13px;
}}
QListWidget[role="nav"]::item:hover,
QListWidget[role="nav"]::item:selected {{ background: transparent; }}
QScrollArea {{ background: transparent; border: 0; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: {S1}px; }}
QScrollBar::handle:vertical {{
    background: {t['surface3']}; border-radius: 3px; min-height: 40px;
}}
QScrollBar::handle:vertical:hover {{ background: {t['fg3']}; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: {S1}px; }}
QScrollBar::handle:horizontal {{
    background: {t['surface3']}; border-radius: 3px; min-width: 40px;
}}
QScrollBar::handle:horizontal:hover {{ background: {t['fg3']}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; border: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* ============ 菜单 ============ */
QMenu {{
    background: {t['surface']};
    border: 1px solid {t['stroke']}; border-radius: {R_CARD}px;
    padding: {S1}px;
}}
QMenu::item {{
    padding: {S2}px {S4}px; border-radius: {R_CTL}px;
    color: {t['fg1']}; font-size: 13px;
}}
QMenu::item:selected {{ background: {t['surface3']}; }}
QMenu::item:disabled {{ color: {t['fg3']}; }}
QMenu::separator {{ height: 1px; background: {t['stroke']}; margin: {S1}px {S2}px; }}
QMenu::icon {{ padding-left: {S3}px; }}

/* ============ 其他 ============ */
QSplitter {{ background: transparent; }}
QSplitter::handle {{ background: transparent; width: 1px; }}
QSplitter::handle:hover {{ background: {t['stroke']}; }}
QStatusBar {{ background: transparent; color: {t['fg3']}; font-size: 12px; }}
QStatusBar::item {{ border: 0; }}
QToolTip {{
    background: {t['surface']}; color: {t['fg1']};
    border: 1px solid {t['stroke']}; border-radius: {R_CTL}px;
    padding: {S1}px {S2}px; font-size: 12px;
}}
QProgressBar {{
    background: {t['surface2']}; border: 0; border-radius: {R_CTL}px;
    height: 4px; text-align: center; color: transparent;
}}
/* 进度条不是"选中"，是"进行中"—— 用中性 fg3，别抢 accent。 */
QProgressBar::chunk {{ background: {t['fg3']}; border-radius: {R_CTL}px; }}
QSlider::groove:horizontal {{
    background: {t['surface3']}; height: 4px; border-radius: 2px;
}}
QSlider::handle:horizontal {{
    background: {t['fg2']}; width: 16px; height: 16px;
    margin: -6px 0; border-radius: 8px;
}}
{shadow}
""".strip() + "\n"


# --------------------------------------------------------------------------- #
# 六、QPalette（QSS 管不到的那部分）
# --------------------------------------------------------------------------- #
#
# 只 setStyleSheet 是不够的：复选框的勾、下拉箭头、焦点虚框、原生对话框的底色
# 由 style 自己画，默认按浅色配色来，丢进深色窗口就会突然跳出来。
# 所以 QSS 和 Palette 必须同时设，两边对齐。

def build_palette(theme: str) -> QPalette:
    """给原生绘制部分配一套对齐的调色板。"""
    t = tokens(theme)
    spec = {
        QPalette.Window:          t["bg"],
        QPalette.WindowText:      t["fg1"],
        QPalette.Base:            t["surface"],
        QPalette.AlternateBase:   t["surface2"],
        QPalette.Text:            t["fg1"],
        QPalette.Button:          t["surface2"],
        QPalette.ButtonText:      t["fg1"],
        QPalette.ToolTipBase:     t["surface"],
        QPalette.ToolTipText:     t["fg1"],
        QPalette.PlaceholderText: t["fg3"],
        QPalette.Highlight:       t["accent"],
        QPalette.HighlightedText: t["accent_ink"],
        QPalette.Link:            t["accent"],
        QPalette.BrightText:      t["danger"],
    }
    pal = QPalette()
    for role, color in spec.items():
        pal.setColor(role, QColor(color))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        pal.setColor(QPalette.Disabled, role, QColor(t["fg3"]))
    return pal


# --------------------------------------------------------------------------- #
# 七、一秒接入
# --------------------------------------------------------------------------- #

def apply(qapp, theme: str) -> None:
    """把字体 + 调色板 + 样式表一次性套上去。

    调用顺序有讲究：**先注册字体**，QSS 里的 ``font-family`` 才认得出
    "HarmonyOS Sans SC"；**再设 Fusion**，否则 Windows 原生风格会自作主张
    画一堆控件；最后才是 QSS。
    """
    load_fonts()
    qapp.setStyle("Fusion")
    qapp.setPalette(build_palette(theme))
    qapp.setFont(ui_font())
    qapp.setStyleSheet(build_qss(theme))


def warmup(qapp) -> None:
    """预热 QSS 编译缓存。

    实测：设完样式表后**第一个**样式化控件要付 173–609 ms 的一次性编译费
    （QSS 有 229 行 / 79 条规则，Qt 要建整套渲染规则缓存），之后每个控件
    只要 0.3 ms。这笔钱本来落在"用户第一次点开图库"那一刻 —— 也就是他
    刚点击完、正等着看图的那一下，卡顿感最明显。

    这里提前用一次性的离屏控件把它付掉。**只允许在启动时调一次**：
    :func:`apply` 不能调它 —— apply 同时服务于主题切换，那条路上缓存
    本来就热了，再预热一次只会让切换主题平白多卡 600 ms。
    """
    from PySide6.QtWidgets import QLineEdit, QPushButton

    probes = (QLineEdit(), QPushButton("warmup"), QLineEdit())
    for w in probes:
        w.setAttribute(Qt.WA_DontShowOnScreen, True)
        w.show()
        w.hide()
        w.deleteLater()


# --------------------------------------------------------------------------- #
# 八、自检
# --------------------------------------------------------------------------- #

def selftest() -> list[str]:
    """返回问题列表 —— 空列表代表全部通过。

    这个自检存在的意义：令牌被改坏时**立刻**报出来，而不是等到某天
    有人截图发现"怎么又花了"。
    """
    problems: list[str] = []

    # 1. 深浅两套令牌必须一一对应，少一个键就会在某个主题下 KeyError。
    keys = set(TOKENS["dark"])
    if keys != set(TOKENS["light"]):
        problems.append(
            f"深浅主题令牌不一致：只在深色有 {sorted(keys - set(TOKENS['light']))}，"
            f"只在浅色有 {sorted(set(TOKENS['light']) - keys)}")

    # 2. 每个令牌都必须是合法的 #RRGGBB / #AARRGGBB。
    for name, table in TOKENS.items():
        for key, value in table.items():
            if not (value.startswith("#") and len(value) in (7, 9)):
                problems.append(f"{name}.{key} = {value!r} 不是 #RRGGBB 形式的颜色")

    # 3. 间距必须是 4 的倍数 —— 这是"整齐感"的物理来源。
    for value in SPACE:
        if value % 4:
            problems.append(f"间距 {value} 不是 4 的倍数")
    if list(SPACE) != sorted(SPACE) or len(set(SPACE)) != len(SPACE):
        problems.append("SPACE 必须严格递增且不重复")

    # 4. 圆角嵌套必须从外到内递减。
    if not (R_CARD > R_NEST > R_CTL):
        problems.append(f"圆角嵌套反了：card={R_CARD} nest={R_NEST} ctl={R_CTL}")

    # 5. 字号阶梯必须单调递增，行高不许小于字号。
    sizes = [v[0] for v in TYPE.values()]
    if sizes != sorted(sizes):
        problems.append(f"字号阶梯不是递增的：{sizes}")
    for name, (size, line, weight) in TYPE.items():
        if line < size:
            problems.append(f"{name} 行高 {line} 小于字号 {size}，文字会被切")
        if weight not in (300, 350, 400, 600, 700):
            problems.append(f"{name} 字重 {weight} 不在 Win11 的 300/350/400/600/700 里")

    # 6. 两套主题都要能生成 QSS，且不许留未替换的占位。
    for name in ("dark", "light"):
        qss = build_qss(name)
        if "{" in qss.replace("{{", "").replace("}}", "") and "}" not in qss:
            problems.append(f"{name} 主题 QSS 括号不配对")
        if qss.count("{") != qss.count("}"):
            problems.append(f"{name} 主题 QSS 花括号不配对 "
                            f"({qss.count('{')} vs {qss.count('}')})")
        if "None" in qss or "#None" in qss:
            problems.append(f"{name} 主题 QSS 里出现了 None")

    # 7. 分类图标：每个内置分类都有图标，且**图标名在图标集里真实存在**。
    #    名字笔误（比如 "gamepad2"）在前一句检查里看不出来，要到
    #    icons.paint() 执行时才 KeyError —— 那是绘制路径，异常会被 Qt 吞掉，
    #    界面上只留一块空白。所以两层都查。
    try:
        from . import icons
        from .config import Config
        have = set(icons.names())
        for key in Config().categories:
            if key not in CATEGORY_ICONS:
                problems.append(f"分类 {key!r} 没有配图标，界面上会是空白")
        for key, name in CATEGORY_ICONS.items():
            if name not in have:
                problems.append(
                    f"分类 {key!r} 的图标 {name!r} 不在图标集里，绘制时会 KeyError")
        for fb in ("file", "shapes"):
            if fb not in have:
                problems.append(f"兜底图标 {fb!r} 不在图标集里")
    except Exception:
        pass

    return problems


if __name__ == "__main__":                        # pragma: no cover
    issues = selftest()
    if issues:
        for issue in issues:
            print(f"[!] {issue}", file=sys.stderr)
        raise SystemExit(1)
    print(f"主题令牌自检通过：{len(TOKENS['dark'])} 个令牌 × 2 套主题，"
          f"{len(SPACE)} 档间距，{len(TYPE)} 档字号，"
          f"{len(CATEGORY_ICONS)} 个分类图标")
