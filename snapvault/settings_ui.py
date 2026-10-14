"""设置面板：编辑 :class:`snapvault.config.Config` 并写回 JSON。

面板不直接改运行中的对象：它深拷贝一份配置让用户改，点"保存"才通过
``on_save`` 回调交回给 :class:`snapvault.app.SnapVaultApp`（由 app 负责落盘、
重建热键、刷新图库），这样"取消"天然安全，不会留下半改状态。
"""

from __future__ import annotations

import copy
import dataclasses
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Property, QPropertyAnimation, QRect, Qt
from PySide6.QtCore import QSize, Signal
from PySide6.QtGui import QColor, QFont, QKeySequence, QPainter
from PySide6.QtWidgets import (QAbstractItemView, QAbstractSpinBox, QApplication,
                               QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFileDialog, QFormLayout, QFrame,
                               QGroupBox, QHBoxLayout, QHeaderView, QInputDialog,
                               QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMessageBox, QPlainTextEdit, QPushButton,
                               QScrollArea, QSpinBox, QStackedWidget,
                               QTableWidget, QTableWidgetItem, QVBoxLayout,
                               QWidget)

from . import icons, theme, winapi
from .ai import DEFAULT_ENDPOINT, QwenVision, normalize_endpoint
from .config import (AI_PROVIDERS, CategoryRule, Config, default_screenshots_dir,
                     provider_needs_key, provider_preset, save_config)

FORMATS = [("png", "PNG（无损，体积大）"), ("jpg", "JPEG（体积小）"),
           ("webp", "WebP（体积最小）")]

THEMES = [("dark", "深色"), ("light", "浅色")]


class _SpinBox(QSpinBox):
    """纯数值输入框：去掉上/下箭头。

    先试过 QSS：把 ``QSpinBox::up-button`` 设成 ``width: 0`` —— **没用**，
    实测箭头照样画出来，在浅色主题的输入框右端留下一个小三角加一段 L 形
    边框残迹（A/B 渲染确认过：补丁前后两张图逐像素一样）。原因是按钮宽为 0
    时 Qt 走基础样式而不是 QSS，``::up-arrow`` 那条规则根本没人接。

    所以用原生解法 ``NoButtons``。输入靠键盘上下键 / Home / End / 鼠标滚轮，
    设置面板够用了，换来的是一个干净的输入框。
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setButtonSymbols(QAbstractSpinBox.NoButtons)


class _DoubleSpinBox(QDoubleSpinBox):
    """小数输入框：权重这种 1.5 / 2.5 步进的值得用它。

    和 :class:`_SpinBox` 分开而不是让后者继承 ``QDoubleSpinBox`` ——
    整数框的 ``value()`` 必须是 int，混用会把 24 写成 24.0 落进 JSON。
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setButtonSymbols(QAbstractSpinBox.NoButtons)

#: 接管后原图怎么处理
SOURCE_ACTIONS = [("keep", "保留原图（不动）"),
                  ("recycle", "移入回收站（可恢复）"),
                  ("delete", "直接删除（不推荐）")]


class HotkeyEdit(QLineEdit):
    """按下组合键即录制的输入框，输出 ``Ctrl+Alt+A`` 形式。"""

    def __init__(self, text: str = "", parent=None) -> None:
        super().__init__(text, parent)
        self.setPlaceholderText("点击后按下组合键")
        self.setReadOnly(True)
        self._valid = True

    def keyPressEvent(self, event) -> None:                     # noqa: N802
        key = event.key()
        if key in (Qt.Key_Control, Qt.Key_Alt, Qt.Key_Shift, Qt.Key_Meta,
                   Qt.Key_unknown):
            return
        if key in (Qt.Key_Backspace, Qt.Key_Delete, Qt.Key_Escape):
            self.setText("")
            self._set_valid(True)
            return
        mods = event.modifiers()
        parts: list[str] = []
        if mods & Qt.ControlModifier:
            parts.append("Ctrl")
        if mods & Qt.AltModifier:
            parts.append("Alt")
        if mods & Qt.ShiftModifier:
            parts.append("Shift")
        if mods & Qt.MetaModifier:
            parts.append("Win")
        letter = QKeySequence(key).toString()
        if not letter:
            return
        if not parts:
            # 裸键会让热键抢占全局输入，强制要求至少一个修饰键
            self._set_valid(False, "热键至少要带一个修饰键（Ctrl/Alt/Shift/Win）")
            return
        parts.append(letter.upper() if len(letter) == 1 else letter)
        self.setText("+".join(parts))
        self._set_valid(True)

    def _set_valid(self, ok: bool, tip: str = "") -> None:
        self._valid = ok
        # 校验态走动态属性 + QSS，颜色由 theme.TOKENS["danger"] 给。
        # 改版前这里写死了 #E06C75 —— 浅色主题下对比度不够，
        # 而且和另外 5 个青绿一样，属于"散落的色值"。
        self.setProperty("invalid", "false" if ok else "true")
        # 动态属性变了必须重新 polish，否则 QSS 不会重算这一条。
        self.style().unpolish(self)
        self.style().polish(self)
        self.setToolTip(tip)

    def is_valid(self) -> bool:
        text = self.text().strip()
        if not text:
            return True                       # 空 = 不绑定该热键
        try:
            winapi.parse_hotkey(text)
            return True
        except Exception as exc:
            self._set_valid(False, str(exc))
            return False


class _NavRow(QWidget):
    """设置面板左侧导航的一行：图标 + 名称。

    为什么不用 QListWidgetItem 自带的外观：选中态要上 accent 底 + 反色文字，
    而 QIcon 是栅格化好的位图，QSS 改不了它的颜色 —— 会出现"底色变了、
    图标还是灰的"这种半截状态。自己画就能让图标和文字一起跟着选中/悬停走，
    也顺带复用主题里的 hover 插值（和主界面左栏分类行同一套手感）。
    """

    clicked = Signal(str)

    ROW_H = 34

    def __init__(self, key: str, label: str, icon_name: str, parent=None) -> None:
        super().__init__(parent)
        self.key = key
        self.label = label
        self.icon_name = icon_name
        self.selected = False
        self._hover = False
        self._hover_k = 0.0
        self._theme = "dark"
        self.setFixedHeight(self.ROW_H)
        self.setCursor(Qt.PointingHandCursor)
        self.setMouseTracking(True)

    def set_theme(self, name: str) -> None:
        self._theme = name
        self.update()

    def set_selected(self, on: bool) -> None:
        if on != self.selected:
            self.selected = on
            self.update()

    # -- hover 进度（可动画属性，与图库分类行同一套） ---------------------- #

    def _get_hover_k(self) -> float:
        return self._hover_k

    def _set_hover_k(self, value: float) -> None:
        self._hover_k = value
        self.update()

    hoverK = Property(float, _get_hover_k, _set_hover_k)

    def _animate_hover(self, on: bool) -> None:
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
        self._hover_anim = anim

    def enterEvent(self, event) -> None:                          # noqa: N802
        self._hover = True
        self._animate_hover(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:                          # noqa: N802
        self._hover = False
        self._animate_hover(False)
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:                     # noqa: N802
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.key)
        event.accept()

    # -- 绘制 -------------------------------------------------------------- #

    def paintEvent(self, _event) -> None:                         # noqa: N802
        t = theme.tokens(self._theme)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        # 按**整块**画，不能再 adjusted 收一圈。
        # 这里曾经写过 adjusted(S1, 2, -S1, -2)：当时 QSS 也给 ::item:selected
        # 涂了 accent，两块颜色一样、几何不一样 —— QSS 那块宽 140 高 34，
        # 自绘这块只有 108×30 且整体偏下，窄的那块在下方多露 6px，
        # 看上去就是个"⌐ 形台阶"。现在底色只有这一个来源，收一圈反而会让
        # 高亮比行本身窄一圈。QSS 那边的 ::item 已经 padding: 0，
        # widget 矩形 == item 背景矩形（见 theme.build_qss 里的说明）。
        rect = self.rect()

        if self.selected:
            # 选中：实心 accent 底 + 反色文字（Win11 设置面板的导航样式）
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(t["accent"]))
            p.drawRoundedRect(rect, theme.R_CTL, theme.R_CTL)
            icon_color, fg = t["accent_ink"], t["accent_ink"]
        else:
            # 未选中：hover 底色按进度插值，图标和文字同步从 fg2 走向 fg1
            if self._hover_k > 0.001:
                p.setPen(Qt.NoPen)
                p.setBrush(theme.alpha(t["surface2"], int(255 * self._hover_k)))
                p.drawRoundedRect(rect, theme.R_CTL, theme.R_CTL)
            icon_color = theme.mix(t["fg2"], t["accent"], self._hover_k).name()
            fg = theme.mix(t["fg2"], t["fg1"], self._hover_k).name()

        glyph = 16
        g_rect = QRect(rect.left() + theme.S3,
                       rect.top() + (rect.height() - glyph) // 2, glyph, glyph)
        icons.paint(p, self.icon_name, g_rect, icon_color, width=2.0)

        text_left = g_rect.right() + theme.S2
        p.setFont(theme.ui_font(13, QFont.Medium))
        p.setPen(QColor(fg))
        p.drawText(QRect(text_left, rect.top(),
                         rect.right() - theme.S2 - text_left, rect.height()),
                   Qt.AlignLeft | Qt.AlignVCenter, self.label)


#: 导航项：(键, 名称, 图标)。顺序即左侧从上到下的顺序。
#: 图标名必须存在于 icons.PATHS —— 自检里有专项检查，写错会当场报出来。
NAV_ITEMS = [
    ("naming", "命名", "file-text"),
    ("categories", "分类管理", "tag"),
    ("ai", "AI 命名", "sparkles"),
    ("watch", "截图接管", "folder-clock"),
    ("storage", "存储", "archive"),
    ("ui", "界面与启动", "settings-2"),
]


class SettingsDialog(QDialog):
    """SnapVault 设置窗口。

    布局是**左侧导航 + 右侧内容**，和主界面（图库）同一种信息结构：
    左边一列可点的面、右边跟着换内容。改版前是顶上的 QTabWidget —— 六个页签
    挤在一行里，"分类管理"这种长名字直接被省略号吃掉，而且和主界面左栏的
    视觉语言完全不搭。
    """

    def __init__(self, cfg: Config, on_save: Callable[[Config], None] | None = None,
                 on_reclassify: Callable[[list[str], list], None] | None = None,
                 parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("SnapVault 设置")
        self.resize(760, 600)
        self.cfg = copy.deepcopy(cfg)
        self._on_save = on_save
        #: 一键重归类的回调。设置面板自己手上没有 vault，只能交给 app；
        #: 参数是 (分类键列表, 关键词规则列表)，让 app 按用户刚改完的设置跑。
        self._on_reclassify = on_reclassify
        #: 各分类当前有多少张图（删除分类时提示用）。由 app 通过
        #: set_category_usage() 注入；拿不到就全是 0，只是提示少一句。
        self._cat_usage: dict[str, int] = {}
        #: 分类管理页的"工作副本"：用户在界面上增删改，保存时才写回 cfg。
        #: 直接改 cfg 会让"取消"也留下痕迹（__init__ 里那份深拷贝就是为这个）。
        self._cats: list[str] = list(self.cfg.categories)
        self._labels: dict[str, str] = dict(self.cfg.category_labels)
        self._icons: dict[str, str] = dict(self.cfg.category_icons)
        self._rules: list[CategoryRule] = [copy.copy(r) for r in self.cfg.category_rules]

        lay = QHBoxLayout(self)
        lay.setContentsMargins(theme.S3, theme.S3, theme.S3, theme.S3)
        lay.setSpacing(theme.S3)

        # --- 左：导航 ---
        self.nav = QListWidget()
        self.nav.setProperty("role", "nav")
        self.nav.setFixedWidth(148)
        self.nav.setSpacing(0)
        self._nav_rows: dict[str, _NavRow] = {}
        for key, label, icon_name in NAV_ITEMS:
            item = QListWidgetItem(self.nav)
            item.setSizeHint(QSize(148, _NavRow.ROW_H + 4))
            row = _NavRow(key, label, icon_name)
            row.set_theme("light" if self.cfg.ui.theme == "light" else "dark")
            row.clicked.connect(self._goto)
            self.nav.setItemWidget(item, row)
            self._nav_rows[key] = row
        self.nav.currentItemChanged.connect(self._on_nav_changed)
        lay.addWidget(self.nav, 0)

        # --- 右：内容（每页一个滚动区，窗口矮了也能全碰到） ---
        self.stack = QStackedWidget()
        lay.addWidget(self.stack, 1)
        for key, _label, _icon in NAV_ITEMS:
            page = QScrollArea()
            page.setWidgetResizable(True)
            page.setFrameShape(QFrame.NoFrame)
            body = getattr(self, f"_tab_{key}")()
            page.setWidget(body)
            self.stack.addWidget(page)

        self.nav.setCurrentRow(0)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel
                                   | QDialogButtonBox.RestoreDefaults)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        # QDialogButtonBox 默认走系统语言，混着中文界面很突兀，统一改中文。
        buttons.button(QDialogButtonBox.Save).setText("保存")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.button(QDialogButtonBox.RestoreDefaults).setText("恢复默认")
        buttons.button(QDialogButtonBox.RestoreDefaults).clicked.connect(self._restore)
        lay.addWidget(buttons)

    # ------------------------------------------------------------------ #
    # 导航
    # ------------------------------------------------------------------ #

    def _goto(self, key: str) -> None:
        for i, (k, _l, _i) in enumerate(NAV_ITEMS):
            if k == key:
                self.nav.setCurrentRow(i)
                return

    def set_theme(self, name: str) -> None:
        """换主题时被 app 回调 —— 左栏导航是自绘的，不重刷就停在旧配色上。

        用户改主题的入口**就在这个面板里**（「界面与启动」页），所以这不是
        理论问题：改完主题、面板还开着，那 6 行导航会保持旧主题的颜色，
        直到关掉重开。
        """
        for row in self._nav_rows.values():
            row.set_theme(name)

    def set_category_usage(self, usage: dict[str, int]) -> None:
        """由 app 注入各分类的图片张数，删分类时用来提示影响面。"""
        self._cat_usage = dict(usage or {})

    def _on_nav_changed(self, current, _previous) -> None:
        if current is None:
            return
        idx = self.nav.row(current)
        self.stack.setCurrentIndex(idx)
        for key, row in self._nav_rows.items():
            row.set_selected(key == NAV_ITEMS[idx][0])
        # 换页时内容淡入 —— 和详情面板换内容同一套 150ms OutCubic。
        # 只在真的换了页时播（初始化也会触发一次，那一页本来就在淡入，无妨）。
        theme.fade_in(self.stack.currentWidget().widget())

    # ------------------------------------------------------------------ #
    # 各标签页
    # ------------------------------------------------------------------ #

    def _tab_naming(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)

        naming = QGroupBox("文件名")
        nform = QFormLayout(naming)
        n = self.cfg.naming
        self.template = QLineEdit(n.template)
        nform.addRow("命名模板", self.template)
        hint = QLabel("可用占位符：{date} {time} {stamp} {yyyymmdd} {hhmmss} "
                      "{category} {label} {slug} {title} {tags} {hash} {seq} {app} {monitor}")
        hint.setWordWrap(True)
        hint.setProperty("role", "hint")
        nform.addRow("", hint)

        self.slug_max = _SpinBox()
        self.slug_max.setRange(4, 80)
        self.slug_max.setValue(n.slug_max_len)
        nform.addRow("标题片段最长", self.slug_max)

        self.separator = QLineEdit(n.separator)
        self.separator.setMaxLength(3)
        nform.addRow("词间分隔符", self.separator)

        self.lowercase = QCheckBox("标题片段转小写")
        self.lowercase.setChecked(n.lowercase_slug)
        nform.addRow(self.lowercase)

        self.hash_prefix = QCheckBox("文件名带内容哈希")
        self.hash_prefix.setChecked(n.hash_prefix)
        nform.addRow(self.hash_prefix)

        self.include_tags = QCheckBox("文件名带标签")
        self.include_tags.setChecked(n.include_tags)
        nform.addRow(self.include_tags)

        v.addWidget(naming)
        v.addStretch(1)
        return page

    def _tab_ai(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        a = self.cfg.ai

        box = QGroupBox("AI 智能命名")
        form = QFormLayout(box)

        self.ai_enabled = QCheckBox("截图后用 AI 生成一句话描述作为文件名")
        self.ai_enabled.setChecked(a.enabled)
        form.addRow(self.ai_enabled)

        # --- 供应商预设胶囊 ---
        self.ai_provider = QComboBox()
        for key, preset in AI_PROVIDERS.items():
            self.ai_provider.addItem(preset["label"], key)
        idx = self.ai_provider.findData(a.provider)
        self.ai_provider.setCurrentIndex(max(0, idx))
        self.ai_provider.currentIndexChanged.connect(self._on_provider_changed)
        form.addRow("供应商", self.ai_provider)

        self.ai_provider_tip = QLabel("")
        self.ai_provider_tip.setWordWrap(True)
        self.ai_provider_tip.setProperty("role", "hint")
        form.addRow("", self.ai_provider_tip)

        self.ai_key = QLineEdit(a.api_key)
        self.ai_key.setEchoMode(QLineEdit.Password)
        self.ai_key.setPlaceholderText("sk-...")
        self.ai_key_label = QLabel("API Key")
        form.addRow(self.ai_key_label, self.ai_key)

        self.ai_endpoint = QLineEdit(a.endpoint)
        self.ai_endpoint.setPlaceholderText(DEFAULT_ENDPOINT)
        form.addRow("接口地址", self.ai_endpoint)
        ep_tip = QLabel("粘贴控制台给的 Base URL 即可（可直接整行复制，末尾的 "
                        "/chat/completions 和多余斜杠会自动清理）。")
        ep_tip.setWordWrap(True)
        ep_tip.setProperty("role", "hint")
        form.addRow("", ep_tip)

        # --- 模型：可编辑下拉（预设候选 + 手打任意模型名） ---
        self.ai_model = QComboBox()
        self.ai_model.setEditable(True)
        self.ai_model.setInsertPolicy(QComboBox.NoInsert)
        for m in self._provider_models(a.provider):
            self.ai_model.addItem(m, m)
        # 用户当前用的模型不在候选里也要显示出来
        if self.ai_model.findData(a.model) < 0:
            self.ai_model.addItem(a.model, a.model)
        self.ai_model.setCurrentIndex(max(0, self.ai_model.findData(a.model)))
        form.addRow("模型", self.ai_model)
        model_tip = QLabel("下拉里是常用候选，也可以直接打字填任意模型名。"
                           "选「Ollama 本地」时可用右侧「测试连接」拉取本机已安装的模型。")
        model_tip.setWordWrap(True)
        model_tip.setProperty("role", "hint")
        form.addRow("", model_tip)

        self.ai_timeout = _SpinBox()
        self.ai_timeout.setRange(3, 120)
        self.ai_timeout.setSuffix(" 秒")
        self.ai_timeout.setValue(int(a.timeout))
        form.addRow("超时", self.ai_timeout)

        self.ai_max_len = _SpinBox()
        self.ai_max_len.setRange(8, 60)
        self.ai_max_len.setValue(a.max_desc_len)
        form.addRow("描述最长字符", self.ai_max_len)

        self.ai_in_name = QCheckBox("描述写入文件名（否则仅入库供检索）")
        self.ai_in_name.setChecked(a.include_in_name)
        form.addRow(self.ai_in_name)

        self.ai_classify = QCheckBox("同时用 AI 判定分类（比视觉规则准；不合法会自动回落）")
        self.ai_classify.setChecked(bool(getattr(a, "classify_with_ai", True)))
        form.addRow(self.ai_classify)

        # --- 测试连接 ---
        test_row = QHBoxLayout()
        self.ai_test_btn = QPushButton("测试连接")
        self.ai_test_btn.clicked.connect(self._ai_test)
        test_row.addWidget(self.ai_test_btn)
        self.ai_test_result = QLabel("")
        self.ai_test_result.setWordWrap(True)
        self.ai_test_result.setProperty("role", "hint")
        test_row.addWidget(self.ai_test_result, 1)
        form.addRow("", test_row)

        v.addWidget(box)

        # --- 提示词编辑器 ---
        pbox = QGroupBox("提示词")
        pv = QVBoxLayout(pbox)
        ptip = QLabel("留空用内置默认。默认提示词会让模型一次返回 "
                      "{\"desc\": 描述, \"cat\": 分类键}，两边都省一次请求。"
                      "改了输出格式的话，描述仍会尽量提取，但分类可能失效。")
        ptip.setWordWrap(True)
        ptip.setProperty("role", "hint")
        pv.addWidget(ptip)
        self.ai_prompt = QPlainTextEdit()
        self.ai_prompt.setPlainText(a.prompt or "")
        self.ai_prompt.setMinimumHeight(120)
        pv.addWidget(self.ai_prompt)
        pbtns = QHBoxLayout()
        default_btn = QPushButton("恢复默认提示词")
        default_btn.clicked.connect(self._ai_prompt_reset)
        pbtns.addWidget(default_btn)
        pbtns.addStretch(1)
        pv.addLayout(pbtns)
        v.addWidget(pbox)

        tip = QLabel("未配置或调用失败时，自动回退为「日期_时间」命名，不影响截图。"
                     "图片会上传到所选供应商 —— 选「Ollama 本地」则不出本机。")
        tip.setWordWrap(True)
        tip.setProperty("role", "hint")
        v.addWidget(tip)
        v.addStretch(1)
        self._on_provider_changed()          # 按当前供应商刷新可见性
        return page

    # -- AI 页的小操作 ---------------------------------------------------- #

    def _provider_models(self, provider: str) -> list[str]:
        return list(provider_preset(provider).get("models") or [])

    def _on_provider_changed(self) -> None:
        """换供应商：刷新提示、按需隐藏 Key 行、把端点/模型填成预设值。

        端点只在用户没动过（还是某个预设值或空）时才覆盖 —— 用户手打的
        自定义地址必须保留，不然切一下供应商就把他的配置冲了。
        """
        provider = self.ai_provider.currentData() or "bailian"
        preset = provider_preset(provider)
        self.ai_provider_tip.setText(preset.get("hint", ""))
        needs_key = bool(preset.get("needs_key", True))
        # Ollama 不需要 Key：把那一行藏起来，而不是留一个必填却无效的输入框
        self.ai_key_label.setVisible(needs_key)
        self.ai_key.setVisible(needs_key)
        if not needs_key:
            self.ai_key.setText("")

        current_ep = self.ai_endpoint.text().strip().rstrip("/")
        known = {p.get("endpoint", "").rstrip("/")
                 for p in AI_PROVIDERS.values() if p.get("endpoint")}
        if not current_ep or current_ep in known:
            self.ai_endpoint.setText(preset.get("endpoint", ""))

        current_model = self.ai_model.currentText().strip()
        models = self._provider_models(provider)
        self.ai_model.clear()
        for m in models:
            self.ai_model.addItem(m, m)
        if current_model and current_model not in models:
            self.ai_model.addItem(current_model, current_model)
        self.ai_model.setCurrentIndex(max(0, self.ai_model.findData(current_model)
                                          if current_model else 0))

    def _ai_prompt_reset(self) -> None:
        from .ai import DEFAULT_PROMPT

        self.ai_prompt.setPlainText(DEFAULT_PROMPT)

    def _ai_test(self) -> None:
        """探活。网络请求可能等十几秒，所以先禁用按钮 + 转等待光标。"""
        provider = self.ai_provider.currentData() or "bailian"
        client = QwenVision(
            api_key=self.ai_key.text().strip(),
            model=self.ai_model.currentText().strip(),
            endpoint=self.ai_endpoint.text().strip(),
            prompt=self.ai_prompt.toPlainText(),
            timeout=max(10.0, float(self.ai_timeout.value())),
            requires_key=provider_needs_key(provider),
        )
        self.ai_test_btn.setEnabled(False)
        self.ai_test_result.setText("正在连接…")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            ok, msg = client.test_connection()
            # 连接成功时顺手把服务端的模型列表拉进下拉，省得用户手打
            if ok and provider == "ollama":
                models = client.list_models()
                if models:
                    current = self.ai_model.currentText().strip()
                    self.ai_model.clear()
                    for m in models:
                        self.ai_model.addItem(m, m)
                    if current and self.ai_model.findData(current) < 0:
                        self.ai_model.addItem(current, current)
                    self.ai_model.setCurrentIndex(
                        max(0, self.ai_model.findData(current) if current else 0))
                    msg += f"；已载入 {len(models)} 个模型"
        finally:
            QApplication.restoreOverrideCursor()
            self.ai_test_btn.setEnabled(True)
        self.ai_test_result.setText(("✓ " if ok else "✗ ") + msg)
        self.ai_test_result.setProperty("ok", "true" if ok else "false")
        self.ai_test_result.style().unpolish(self.ai_test_result)
        self.ai_test_result.style().polish(self.ai_test_result)

    def _tab_categories(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)

        # --- 1. 自动分类开关（从「命名」页搬来，和分类放一起才找得到） ---
        cls = QGroupBox("自动分类")
        cform = QFormLayout(cls)
        cl = self.cfg.classify
        self.classify_enabled = QCheckBox("按截图内容自动分类")
        self.classify_enabled.setChecked(cl.enabled)
        cform.addRow(self.classify_enabled)

        self.app_hints = QCheckBox("结合进程名 / 窗口标题（更准，但会读取窗口信息）")
        self.app_hints.setChecked(cl.app_hints)
        cform.addRow(self.app_hints)

        self.conf_floor = _SpinBox()
        self.conf_floor.setRange(0, 100)
        self.conf_floor.setSuffix(" %")
        self.conf_floor.setValue(int(round(cl.confidence_floor * 100)))
        cform.addRow("低于该置信度归入「其他」", self.conf_floor)

        self.probe_rows = _SpinBox()
        self.probe_rows.setRange(80, 2000)
        self.probe_rows.setSingleStep(20)
        self.probe_rows.setValue(cl.probe_rows)
        cform.addRow("分析采样行数", self.probe_rows)
        v.addWidget(cls)

        # --- 2. 分类列表：增删 / 改名 / 换图标 / 排序 ---
        box = QGroupBox("分类列表")
        bv = QVBoxLayout(box)
        tip = QLabel("改名或删除只影响以后的判定与显示，已归档的图片文件原地不动"
                     "（需要刷新库里的归类看下面「重新归类」）。")
        tip.setWordWrap(True)
        tip.setProperty("role", "hint")
        bv.addWidget(tip)

        self.cat_table = QTableWidget(0, 3)
        self.cat_table.setHorizontalHeaderLabels(["图标", "键", "名称"])
        self.cat_table.verticalHeader().setVisible(False)
        self.cat_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.cat_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.cat_table.setMinimumHeight(180)
        self.cat_table.horizontalHeader().setStretchLastSection(True)
        # 图标列给到 164px：最长的图标名 "message-square" 加图标能完整显示。
        # 窄了会被省略成 "message-sql" 这种半截字，而且每个下拉框的固有宽度
        # 还会把整表挤出横向滚动条。
        self.cat_table.setColumnWidth(0, 164)
        bv.addWidget(self.cat_table)

        row = QHBoxLayout()
        add_btn = QPushButton("添加分类…")
        add_btn.clicked.connect(self._cat_add)
        row.addWidget(add_btn)
        up_btn = QPushButton("上移")
        up_btn.clicked.connect(lambda: self._cat_move(-1))
        row.addWidget(up_btn)
        down_btn = QPushButton("下移")
        down_btn.clicked.connect(lambda: self._cat_move(1))
        row.addWidget(down_btn)
        del_btn = QPushButton("删除")
        del_btn.clicked.connect(self._cat_delete)
        row.addWidget(del_btn)
        row.addStretch(1)
        bv.addLayout(row)
        v.addWidget(box)

        # --- 3. 关键词规则：命中就把对应分类加分（DIY 的主入口） ---
        rbox = QGroupBox("关键词规则")
        rv = QVBoxLayout(rbox)
        rtip = QLabel("进程名 / 窗口标题里出现某个关键词时，把对应分类加分。"
                      "权重 1.5 与内置程序提示同档，3.0 起基本能压过画面特征。"
                      "多条命中取权重最高的一条，不叠加。")
        rtip.setWordWrap(True)
        rtip.setProperty("role", "hint")
        rv.addWidget(rtip)

        self.rule_table = QTableWidget(0, 4)
        self.rule_table.setHorizontalHeaderLabels(["关键词", "归入分类", "权重", "启用"])
        self.rule_table.verticalHeader().setVisible(False)
        self.rule_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.rule_table.setMinimumHeight(140)
        self.rule_table.horizontalHeader().setStretchLastSection(False)
        # 关键词列吃剩余宽度，其余三列按内容给固定值 ——
        # 默认均分会把 "归入分类" 挤成一咕噜，权重列又空一大块。
        self.rule_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.Stretch)
        self.rule_table.setColumnWidth(1, 130)
        self.rule_table.setColumnWidth(2, 70)
        self.rule_table.setColumnWidth(3, 60)
        rv.addWidget(self.rule_table)

        rrow = QHBoxLayout()
        radd = QPushButton("添加规则")
        radd.clicked.connect(self._rule_add)
        rrow.addWidget(radd)
        rdel = QPushButton("删除选中")
        rdel.clicked.connect(self._rule_delete)
        rrow.addWidget(rdel)
        rrow.addStretch(1)
        rv.addLayout(rrow)
        v.addWidget(rbox)

        # --- 4. 一键重归类 ---
        rebox = QGroupBox("重新归类已有截图")
        rev = QVBoxLayout(rebox)
        self.reclass_tip = QLabel(
            "按上面的分类设置和关键词规则，重新判定库内所有截图的分类。"
            "只更新库里的记录，不移动文件、不改文件名。890 张约需几十秒。")
        self.reclass_tip.setWordWrap(True)
        self.reclass_tip.setProperty("role", "hint")
        rev.addWidget(self.reclass_tip)
        self.reclass_btn = QPushButton("一键重归类")
        self.reclass_btn.clicked.connect(self._reclassify)
        rev.addWidget(self.reclass_btn, 0)
        v.addWidget(rebox)

        v.addStretch(1)
        self._reload_cat_table()
        self._reload_rule_table()
        return page

    # -- 分类列表的小操作 ------------------------------------------------ #

    def _harvest_tables(self) -> None:
        """把表格里正在编辑的文本收进工作副本。

        表格是"画出来给人看"的，``self._labels`` / ``self._rules`` 才是真相。
        用户在名称列、关键词列里打字的那一刻，工作副本还是旧的 —— 此时
        任何一次重建（添加 / 移动 / 删除都会整表重建）都会把没保存的编辑
        冲掉。所以每次动表格之前先收一遍，``_save`` 也一样。
        """
        if getattr(self, "cat_table", None) is not None:
            for i, key in enumerate(self._cats):
                item = self.cat_table.item(i, 2)
                if item is not None and item.text().strip():
                    self._labels[key] = item.text().strip()
        if getattr(self, "rule_table", None) is not None:
            for i, rule in enumerate(self._rules):
                item = self.rule_table.item(i, 0)
                if item is not None and item.text().strip():
                    rule.keyword = item.text().strip()

    def _reload_cat_table(self) -> None:
        """把 self._cats 画进表格。图标列是下拉框，改完即时生效。"""
        self.cat_table.setRowCount(0)
        theme_name = "light" if self.cfg.ui.theme == "light" else "dark"
        fg = theme.tokens(theme_name)["fg2"]
        for i, key in enumerate(self._cats):
            self.cat_table.insertRow(i)
            icon_combo = QComboBox()
            current = self.cfg.category_icon(key)
            for name in icons.names():
                icon_combo.addItem(icons.icon(name, fg, 15), name, name)
            idx = icon_combo.findData(current)
            icon_combo.setCurrentIndex(max(0, idx))
            icon_combo.currentIndexChanged.connect(
                lambda _i, k=key, c=icon_combo: self._icons.__setitem__(k, c.currentData()))
            self.cat_table.setCellWidget(i, 0, icon_combo)

            key_item = QTableWidgetItem(key)
            key_item.setFlags(key_item.flags() & ~Qt.ItemIsEditable)
            self.cat_table.setItem(i, 1, key_item)

            name_item = QTableWidgetItem(self._labels.get(key, key))
            self.cat_table.setItem(i, 2, name_item)
        self.cat_table.resizeRowsToContents()

    def _cat_add(self) -> None:
        self._harvest_tables()
        key, ok = QInputDialog.getText(self, "添加分类", "分类键（英文，如 work）：")
        if not ok:
            return
        key = "".join(ch for ch in key.strip().lower() if ch.isalnum() or ch in "-_")
        if not key:
            QMessageBox.warning(self, "无法添加", "分类键只能是字母、数字、- 和 _。")
            return
        if key in self._cats:
            QMessageBox.warning(self, "已存在", f"分类 {key!r} 已经在了。")
            return
        label, ok = QInputDialog.getText(self, "添加分类", "显示名称（如 工作）：")
        if not ok:
            return
        self._cats.append(key)
        self._labels[key] = label.strip() or key
        self._reload_cat_table()

    def _cat_delete(self) -> None:
        self._harvest_tables()
        row = self.cat_table.currentRow()
        if row < 0 or row >= len(self._cats):
            return
        key = self._cats[row]
        if key == "other":
            QMessageBox.warning(self, "不能删除",
                                "「其他」是兜底分类，删了之后分不准的图会无处可去。")
            return
        n = self._cat_usage.get(key, 0)
        msg = f"删除分类「{self._labels.get(key, key)}」？\n\n只影响以后的判定与显示，"
        if n:
            msg += (f"库内还有 {n} 张图归在该类下，它们会保留原分类直到你点"
                    f"「一键重归类」。")
        else:
            msg += "库里没有图归在该类下。"
        if QMessageBox.question(self, "确认删除", msg) != QMessageBox.Yes:
            return
        self._cats.pop(row)
        self._labels.pop(key, None)
        self._icons.pop(key, None)
        self._rules = [r for r in self._rules if r.category != key]
        self._reload_cat_table()
        self._reload_rule_table()

    def _cat_move(self, delta: int) -> None:
        self._harvest_tables()
        row = self.cat_table.currentRow()
        target = row + delta
        if row < 0 or target < 0 or target >= len(self._cats):
            return
        self._cats[row], self._cats[target] = self._cats[target], self._cats[row]
        self._reload_cat_table()
        self.cat_table.setCurrentCell(target, 0)

    # -- 关键词规则的小操作 ---------------------------------------------- #

    def _reload_rule_table(self) -> None:
        self.rule_table.setRowCount(0)
        theme_name = "light" if self.cfg.ui.theme == "light" else "dark"
        fg = theme.tokens(theme_name)["fg2"]
        for i, rule in enumerate(self._rules):
            self.rule_table.insertRow(i)

            kw = QTableWidgetItem(rule.keyword)
            self.rule_table.setItem(i, 0, kw)

            combo = QComboBox()
            for key in self._cats:
                combo.addItem(self._labels.get(key, key), key)
            idx = combo.findData(rule.category)
            combo.setCurrentIndex(max(0, idx))
            combo.currentIndexChanged.connect(
                lambda _i, r=rule, c=combo: setattr(r, "category", c.currentData()))
            self.rule_table.setCellWidget(i, 1, combo)

            spin = _DoubleSpinBox()
            spin.setRange(0.5, 3.0)
            spin.setSingleStep(0.5)
            spin.setDecimals(1)
            spin.setSuffix(" ×")
            spin.setValue(float(rule.weight))
            spin.valueChanged.connect(
                lambda v, r=rule: setattr(r, "weight", float(v)))
            self.rule_table.setCellWidget(i, 2, spin)

            chk = QCheckBox()
            chk.setChecked(rule.enabled)
            chk.stateChanged.connect(
                lambda st, r=rule: setattr(r, "enabled", bool(st)))
            self.rule_table.setCellWidget(i, 3, chk)
        self.rule_table.resizeRowsToContents()

    def _rule_add(self) -> None:
        self._harvest_tables()
        kw, ok = QInputDialog.getText(self, "添加关键词规则",
                                      "关键词（匹配进程名 / 窗口标题）：")
        if not ok or not kw.strip():
            return
        rule = CategoryRule(keyword=kw.strip(),
                            category=self._cats[0] if self._cats else "other",
                            weight=1.5, enabled=True)
        self._rules.append(rule)
        self._reload_rule_table()
        self.rule_table.setCurrentCell(len(self._rules) - 1, 0)

    def _rule_delete(self) -> None:
        self._harvest_tables()
        row = self.rule_table.currentRow()
        if 0 <= row < len(self._rules):
            self._rules.pop(row)
            self._reload_rule_table()

    def _reclassify(self) -> None:
        """一键重归类：丢给 app 去做（它手上有 vault，而且能弹进度）。"""
        if callable(self._on_reclassify):
            self._on_reclassify(self._cats, self._rules)
            return
        QMessageBox.information(self, "暂不可用", "当前运行环境不支持重归类。")

    def _tab_watch(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)

        box = QGroupBox("接管系统截图（自动 AI 命名 + 分类）")
        form = QFormLayout(box)
        w = self.cfg.watch

        self.watch_enabled = QCheckBox("监视截图输出目录，新截图自动命名并分类归档")
        self.watch_enabled.setChecked(w.enabled)
        form.addRow(self.watch_enabled)

        self.watch_dirs = QListWidget()
        self.watch_dirs.setFixedHeight(96)
        for d in (w.dirs or []):
            self.watch_dirs.addItem(d)
        form.addRow("监视目录", self.watch_dirs)

        row = QHBoxLayout()
        add_btn = QPushButton("添加目录…")
        add_btn.clicked.connect(self._watch_add_dir)
        row.addWidget(add_btn)
        sys_btn = QPushButton("加入系统截图目录")
        sys_btn.clicked.connect(self._watch_add_system_dir)
        row.addWidget(sys_btn)
        rm_btn = QPushButton("移除选中")
        rm_btn.clicked.connect(self._watch_remove_dir)
        row.addWidget(rm_btn)
        row.addStretch(1)
        form.addRow("", row)

        tip = QLabel("列表为空时自动使用系统默认截图目录 "
                     "%USERPROFILE%\\Pictures\\Screenshots。图库自身目录会被自动排除，"
                     "避免归档产物被反复处理。")
        tip.setWordWrap(True)
        tip.setProperty("role", "hint")
        form.addRow("", tip)

        self.watch_action = QComboBox()
        for key, label in SOURCE_ACTIONS:
            self.watch_action.addItem(label, key)
        idx = self.watch_action.findData(w.source_action)
        self.watch_action.setCurrentIndex(max(0, idx))
        form.addRow("原图处理", self.watch_action)

        self.watch_settle = _SpinBox()
        self.watch_settle.setRange(0, 15000)
        self.watch_settle.setSingleStep(100)
        self.watch_settle.setSuffix(" ms")
        self.watch_settle.setValue(int(w.settle_ms))
        form.addRow("写入静置判定", self.watch_settle)

        self.watch_interval = _SpinBox()
        self.watch_interval.setRange(300, 60000)
        self.watch_interval.setSingleStep(100)
        self.watch_interval.setSuffix(" ms")
        self.watch_interval.setValue(int(w.interval_ms))
        form.addRow("扫描间隔", self.watch_interval)

        v.addWidget(box)

        note = QLabel("原理：Windows 自带截图工具是签名的 MSIX 应用，改不了本体，"
                      "但它的输出目录是公开的。SnapVault 盯住这个目录，"
                      "新截图一落盘就自动走 AI 描述 → 分类 → 命名 → 入库，"
                      "不修改任何系统组件，也不会被商店更新覆盖。")
        note.setWordWrap(True)
        note.setProperty("role", "hint")
        v.addWidget(note)
        v.addStretch(1)
        return page

    # -- 接管目录列表的小操作 ------------------------------------------- #

    def _watch_list(self) -> list[str]:
        return [self.watch_dirs.item(i).text() for i in range(self.watch_dirs.count())]

    def _watch_add_dir(self) -> None:
        start = str(Path.home() / "Pictures")
        chosen = QFileDialog.getExistingDirectory(self, "选择要监视的目录", start)
        if chosen and chosen not in self._watch_list():
            self.watch_dirs.addItem(chosen)

    def _watch_add_system_dir(self) -> None:
        d = default_screenshots_dir()
        if d not in self._watch_list():
            self.watch_dirs.addItem(d)

    def _watch_remove_dir(self) -> None:
        for item in self.watch_dirs.selectedItems():
            self.watch_dirs.takeItem(self.watch_dirs.row(item))

    def _tab_storage(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        box = QGroupBox("归档位置")
        form = QFormLayout(box)
        s = self.cfg.storage

        row = QHBoxLayout()
        self.vault_dir = QLineEdit(s.vault_dir)
        row.addWidget(self.vault_dir, 1)
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._pick_dir)
        row.addWidget(browse)
        form.addRow("图库目录", row)

        self.folder_pattern = QLineEdit(s.folder_pattern)
        form.addRow("子目录模板", self.folder_pattern)
        tip = QLabel("例如 {yyyy}/{MM}、{yyyy}/{category}，或 {cn} 按中文分类分文件夹"
                     "（代码/文档/网页…）；留空则平铺")
        tip.setWordWrap(True)
        tip.setProperty("role", "hint")
        form.addRow("", tip)

        self.image_format = QComboBox()
        for key, label in FORMATS:
            self.image_format.addItem(label, key)
        idx = self.image_format.findData(s.image_format)
        self.image_format.setCurrentIndex(max(0, idx))
        form.addRow("图片格式", self.image_format)

        self.jpg_quality = _SpinBox()
        self.jpg_quality.setRange(40, 100)
        self.jpg_quality.setValue(s.jpg_quality)
        form.addRow("有损格式质量", self.jpg_quality)

        self.thumb_size = _SpinBox()
        self.thumb_size.setRange(128, 1024)
        self.thumb_size.setSingleStep(32)
        self.thumb_size.setValue(s.thumb_size)
        form.addRow("缩略图边长", self.thumb_size)
        v.addWidget(box)

        more = QGroupBox("行为")
        mform = QFormLayout(more)
        self.copy_clipboard = QCheckBox("截图后复制到剪贴板")
        self.copy_clipboard.setChecked(s.copy_to_clipboard)
        mform.addRow(self.copy_clipboard)

        self.dedupe = QCheckBox("内容重复时自动加序号（-2、-3）")
        self.dedupe.setChecked(s.dedupe)
        mform.addRow(self.dedupe)

        self.notify = QCheckBox("截图后弹出提示")
        self.notify.setChecked(s.notify)
        mform.addRow(self.notify)

        self.play_sound = QCheckBox("截图后播放提示音")
        self.play_sound.setChecked(s.play_sound)
        mform.addRow(self.play_sound)

        self.keep_jsonl = QCheckBox("维护 JSONL 审计日志")
        self.keep_jsonl.setChecked(s.keep_jsonl)
        mform.addRow(self.keep_jsonl)
        v.addWidget(more)
        v.addStretch(1)
        return page

    def _tab_ui(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        box = QGroupBox("界面")
        form = QFormLayout(box)
        u = self.cfg.ui

        self.theme = QComboBox()
        for key, label in THEMES:
            self.theme.addItem(label, key)
        idx = self.theme.findData(u.theme)
        self.theme.setCurrentIndex(max(0, idx))
        form.addRow("主题", self.theme)

        self.ui_thumb = _SpinBox()
        self.ui_thumb.setRange(96, 320)
        self.ui_thumb.setSingleStep(8)
        self.ui_thumb.setValue(u.thumb_size)
        form.addRow("缩略图目标尺寸", self.ui_thumb)

        self.reduce_motion = QCheckBox("减少动态效果（关掉淡入与过渡动画）")
        self.reduce_motion.setChecked(u.reduce_motion)
        form.addRow(self.reduce_motion)
        tip_motion = QLabel("窗口出场、切换选中、鼠标悬停都不再播动画，全部瞬时完成。"
                            "晕动症敏感或不喜欢过渡效果的用户可以打开。")
        tip_motion.setProperty("role", "hint")
        tip_motion.setWordWrap(True)
        form.addRow(tip_motion)

        self.start_minimized = QCheckBox("启动时只显示托盘图标")
        self.start_minimized.setChecked(u.start_minimized)
        form.addRow(self.start_minimized)

        self.close_to_tray = QCheckBox("关闭窗口时最小化到托盘（不退出）")
        self.close_to_tray.setChecked(u.close_to_tray)
        form.addRow(self.close_to_tray)

        self.autostart = QCheckBox("开机自动启动")
        self.autostart.setChecked(u.autostart)
        form.addRow(self.autostart)
        v.addWidget(box)

        hot = QGroupBox("图库热键")
        hform = QFormLayout(hot)
        self.hk_edits: dict[str, HotkeyEdit] = {}
        gal_edit = HotkeyEdit(self.cfg.hotkeys.gallery)
        self.hk_edits["gallery"] = gal_edit
        hform.addRow("打开图库", gal_edit)
        tip_hk = QLabel("截图不需要热键 —— 用系统自带的 Win+Shift+S 就行。")
        tip_hk.setProperty("role", "hint")
        tip_hk.setWordWrap(True)
        hform.addRow(tip_hk)
        v.addWidget(hot)

        info = QGroupBox("配置文件")
        iform = QFormLayout(info)
        from .config import config_path

        path_label = QLabel(str(config_path()))
        path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        path_label.setWordWrap(True)
        iform.addRow("位置", path_label)
        v.addWidget(info)

        # 「关于」——不只是好看，**是许可要求**。
        # HarmonyOS Sans Fonts License Agreement 第 2 条第 1 款原文：
        #   "YOU shall make a prominent notice in the software to state that
        #    HarmonyOS Sans Fonts are used."
        # 只在 README 里写不满足"in the software"。随包带了字体文件就必须声明。
        about = QGroupBox("关于")
        aform = QFormLayout(about)
        from . import __version__
        ver_label = QLabel(f"SnapVault {__version__}")
        ver_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        aform.addRow("版本", ver_label)
        credits = QLabel(
            "界面字体 <b>HarmonyOS Sans SC</b> © Huawei Device Co., Ltd.，"
            "依《HarmonyOS Sans Fonts License Agreement》随本软件分发（未修改字体文件）。"
            "图标取自 <b>Lucide</b>（ISC License, © Lucide Contributors）。"
            "完整许可文本为程序目录下的 <code>THIRD_PARTY_LICENSES.md</code>，"
            "源码仓库根目录同名文件。"
        )
        credits.setProperty("role", "hint")
        credits.setWordWrap(True)
        credits.setTextFormat(Qt.RichText)
        aform.addRow("第三方", credits)
        v.addWidget(about)

        v.addStretch(1)
        return page

    # ------------------------------------------------------------------ #
    # 交互
    # ------------------------------------------------------------------ #

    def _pick_dir(self) -> None:
        current = self.vault_dir.text().strip() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "选择图库目录", current)
        if chosen:
            self.vault_dir.setText(chosen)

    def _restore(self) -> None:
        if QMessageBox.question(self, "恢复默认", "把所有设置恢复为默认值？") != QMessageBox.Yes:
            return
        self.cfg = Config()
        self.accept()
        if self._on_save:
            self._on_save(self.cfg)

    def _save(self) -> None:
        # 热键合法性
        for name, edit in self.hk_edits.items():
            if not edit.is_valid():
                QMessageBox.warning(self, "热键无效",
                                    f"{name} 的热键无法解析：{edit.text()}\n请重新录制。")
                return
        specs = {name: edit.text().strip() for name, edit in self.hk_edits.items()}
        filled = [s for s in specs.values() if s]
        if len(filled) != len(set(filled)):
            QMessageBox.warning(self, "热键重复", "有多个功能绑定了同一个热键，请修改。")
            return

        cfg = self.cfg
        for name, spec in specs.items():
            setattr(cfg.hotkeys, name, spec)

        n = cfg.naming
        n.template = self.template.text().strip() or "{date}_{time}_{category}_{slug}"
        n.slug_max_len = self.slug_max.value()
        n.separator = self.separator.text() or "_"
        n.lowercase_slug = self.lowercase.isChecked()
        n.hash_prefix = self.hash_prefix.isChecked()
        n.include_tags = self.include_tags.isChecked()

        cl = cfg.classify
        cl.enabled = self.classify_enabled.isChecked()
        cl.app_hints = self.app_hints.isChecked()
        cl.confidence_floor = self.conf_floor.value() / 100.0
        cl.probe_rows = self.probe_rows.value()

        a = cfg.ai
        a.enabled = self.ai_enabled.isChecked()
        a.provider = self.ai_provider.currentData() or "bailian"
        a.api_key = self.ai_key.text().strip()
        a.endpoint = normalize_endpoint(self.ai_endpoint.text())
        # 可编辑下拉：用户手打的模型名在 currentText 里，currentData 可能为空
        a.model = (self.ai_model.currentText().strip()
                   or self.ai_model.currentData() or a.model)
        a.prompt = self.ai_prompt.toPlainText().strip()
        a.timeout = float(self.ai_timeout.value())
        a.max_desc_len = self.ai_max_len.value()
        a.include_in_name = self.ai_in_name.isChecked()
        a.classify_with_ai = self.ai_classify.isChecked()
        # 不需要 Key 的供应商（Ollama）不留空串，免得旧版读配置时犯迷糊
        if not provider_needs_key(a.provider):
            a.api_key = ""

        s = cfg.storage
        s.vault_dir = self.vault_dir.text().strip() or s.vault_dir
        s.folder_pattern = self.folder_pattern.text().strip()
        s.flat = not bool(s.folder_pattern)
        s.image_format = self.image_format.currentData()
        s.jpg_quality = self.jpg_quality.value()
        s.thumb_size = self.thumb_size.value()
        s.copy_to_clipboard = self.copy_clipboard.isChecked()
        s.dedupe = self.dedupe.isChecked()
        s.notify = self.notify.isChecked()
        s.play_sound = self.play_sound.isChecked()
        s.keep_jsonl = self.keep_jsonl.isChecked()

        w = cfg.watch
        w.enabled = self.watch_enabled.isChecked()
        w.dirs = self._watch_list()
        w.source_action = self.watch_action.currentData() or "keep"
        w.settle_ms = self.watch_settle.value()
        w.interval_ms = self.watch_interval.value()

        u = cfg.ui
        u.theme = self.theme.currentData()
        u.thumb_size = self.ui_thumb.value()
        u.reduce_motion = self.reduce_motion.isChecked()
        u.start_minimized = self.start_minimized.isChecked()
        u.close_to_tray = self.close_to_tray.isChecked()
        u.autostart = self.autostart.isChecked()

        # --- 分类管理页的工作副本写回 ---
        # 名称列 / 关键词列是原地编辑的，先把表格里的文本收进来再整体写回
        self._harvest_tables()
        cfg.categories = list(self._cats)
        cfg.category_labels = dict(self._labels)
        cfg.category_icons = dict(self._icons)
        cfg.category_rules = [copy.copy(r) for r in self._rules]
        cfg.migrate()

        save_config(cfg)
        if self._on_save:
            self._on_save(cfg)
        self.accept()


def import_config(path: str | Path) -> Config:
    """从任意 JSON 读一份配置（用于导入他人配置）。"""
    import json

    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return Config.from_dict(raw)


def config_as_text(cfg: Config) -> str:
    """把配置渲染成可读文本（"关于/诊断"用）。"""
    lines: list[str] = []
    for f in dataclasses.fields(cfg):
        value = getattr(cfg, f.name)
        if dataclasses.is_dataclass(value):
            lines.append(f"[{f.name}]")
            for sub in dataclasses.fields(value):
                lines.append(f"  {sub.name} = {getattr(value, sub.name)!r}")
        else:
            lines.append(f"{f.name} = {value!r}")
    return "\n".join(lines)
