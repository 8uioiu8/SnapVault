"""应用装配：托盘图标 + 全局热键 + 悬浮层 + 图库 + 设置。

Qt 只允许在主线程操作界面，而全局热键的回调跑在 winapi 自己的消息循环线程里，
所以这里用一个 :class:`HotkeyBridge` 把回调转成 Qt 信号再投递到主线程；
托盘菜单、悬浮层返回的信号同理。
"""

from __future__ import annotations

import os
import sys
import threading
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QRect, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (QApplication, QMenu, QMessageBox, QProgressDialog,
                               QSystemTrayIcon, QWidget)

from . import capture, feedback, icons, theme, winapi
from .config import Config, load_config, save_config
from .gallery import GalleryWindow
from .settings_ui import SettingsDialog
from .shot import ShotService, play_sound
from .vault import ShotRecord, Vault
from .watcher import SystemShotWatcher

APP_NAME = "SnapVault"
APP_TITLE = "SnapVault 截图归档"

#: 「立即接管一次」单次处理上限。补跑是真的在调 AI，几百张连跑又慢又花额度，
#: 所以默认一批 100 张，剩下的再点一次继续。
SWEEP_BATCH = 100

# 主题（颜色 / 字号 / 圆角 / 间距 / 动效）全部搬去了 :mod:`snapvault.theme`。
# 这里原来躺着两段各 70 行的 DARK_QSS / LIGHT_QSS 和一个 theme_palette()，
# 它们的问题不是"写得不好"，而是**没有单一来源**：同一个青色在这两个字符串里
# 出现了 5 次且各不相同，改一处忘一处。
#
# 现在改主题只改 theme.TOKENS 一处，QSS 由 theme.build_qss() 生成。

# --------------------------------------------------------------------------- #
# 托盘图标
# --------------------------------------------------------------------------- #

def app_icon(size: int = 64) -> QIcon:
    """程序图标：强调色圆角块 + 取景框（Lucide ``scan-line``）。

    为什么是"实心色块 + 深色字形"：任务栏和托盘可能压在深色或浅色背景上，
    一个纯描边的图标在其中一种背景上一定会糊掉。填一块强调色的底，
    字形用 accent_ink 压上去，两种背景上都站得住。

    图形本身取自 Lucide（ISC 许可），不自己画 —— 见 :mod:`snapvault.icons`。
    """
    t = theme.tokens("dark")            # 图标固定用深色主题的强调色，与界面主题无关
    pix = QPixmap(size, size)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(t["accent"]))
    radius = size * 0.22                # ≈ Win11 图标的标准圆角比例
    p.drawRoundedRect(0, 0, size, size, radius, radius)

    inset = int(size * 0.24)
    icons.paint(p, "scan-line",
                QRect(inset, inset, size - inset * 2, size - inset * 2),
                t["accent_ink"], width=2.0)
    p.end()
    return QIcon(pix)


# --------------------------------------------------------------------------- #
# 热键 -> 主线程桥
# --------------------------------------------------------------------------- #

def _affects_records(old: Config, new: Config) -> bool:
    """这次设置改动是否影响图库里**已显示**的内容。

    只有两种值得全量 reload：换了图库目录（整个库都不一样了）、改了界面
    缩略图尺寸（缓存里的位图是按旧尺寸画的，不重画会糊）。其余改动 ——
    主题、热键、命名模板、AI、分类的名称 / 图标 / 排序 / 规则 —— 记录一张
    都没变，走 :meth:`GalleryWindow.refresh_chrome` 就够。

    分类规则改了也不会追溯改动已有记录 —— 那是一键重归类的事，用户点之前
    老归类就该保持原样。
    """
    return (old.storage.vault_dir != new.storage.vault_dir
            or old.ui.thumb_size != new.ui.thumb_size)


class HotkeyBridge(QObject):
    """把 winapi 热键线程的回调变成 Qt 信号，保证槽在主线程执行。"""

    triggered = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)

    def make(self, action: str):
        def _cb() -> None:
            self.triggered.emit(action)
        return _cb


class WatchBridge(QObject):
    """把接管线程的事件转到主线程（Qt 的界面操作只能在主线程做）。"""

    archived = Signal(object)          # 载荷是 snapvault.watcher.WatchEvent


class ReclassifyBridge(QObject):
    """一键重归类的跨线程信使。

    重归类要逐张解码 + 提特征（890 张几十秒），必须放工作线程，否则主线程
    卡住连进度框都刷不出来。进度和结果都走信号转回主线程。
    """

    progress = Signal(int, int)        # done, total
    finished = Signal(int, int, str)   # done, changed, 错误信息（空=成功）

    def __init__(self, vault, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.vault = vault

    def run(self) -> None:
        try:
            done, changed = self.vault.reclassify_all(
                progress=lambda d, t: self.progress.emit(d, t))
            self.finished.emit(done, changed, "")
        except Exception as exc:                        # noqa: BLE001
            self.finished.emit(-1, -1, f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- #
# 反馈实现：托盘气泡 + 声音 + 剪贴板
# --------------------------------------------------------------------------- #

class TrayFeedback:
    """反馈调度中心：决定"什么事件走哪一级"。

    三级的具体实现见 :mod:`snapvault.feedback`，这里只做路由：

    ================  ====================================================
    事件              走哪一级
    ================  ====================================================
    成功归档一张      **L0 静默** —— 托盘图标脉冲 320ms + tooltip 更新 +
                      图库卡片亮一下。**一个窗口都不出现。**
    失败              **L1 浮层** —— 唯一允许打断的情况。不弹就等于把错误
                      吞了，用户会以为"它是不是没在跑"。
    「立即接管」      L1 浮层 —— 长任务该有个收尾交代，但仍然不抢焦点、
                      不进通知中心。
    已暂停/已开启     L1 浮层 —— 这是用户主动做的操作，需要明确回执。
    调试信息          **L0 静默** —— 只写 stderr，不打扰。
    ================  ====================================================

    改版前这四种事件全都走 ``QSystemTrayIcon.showMessage``，于是：
    通知中心攒了几十条"已归档"、失败消息被专注助手一起吞掉、
    样式被系统写死到只能塞 4 行纯文本。
    """

    def __init__(self, app: "SnapVaultApp") -> None:
        self.app = app
        self.hub = feedback.FeedbackHub()
        self._batch: feedback.Toast | None = None

    def bind_tray(self, tray, base_icon) -> None:
        """托盘建好后把它接上 —— 反馈需要往托盘图标上做微动效。"""
        self.hub.tray = tray
        self.hub.pulse = feedback.TrayPulse(tray, base_icon)

    def set_theme(self, name: str) -> None:
        feedback.set_theme_guess(name)
        current = self.hub.toasts.current
        if current is not None:
            current.set_theme(name)

    # -- L0：成功，静默 ---------------------------------------------------- #

    def saved(self, record: ShotRecord, *, copied: bool = False,
              tagged: list | None = None) -> None:
        """一张截图处理完了。**不弹任何窗口。**

        用户能感知到的只有三件事，都不打断他：
        1. 托盘图标上浮出一个小绿点，320ms 后自己消失；
        2. 鼠标悬到托盘上能看到"上次归档"是哪张；
        3. 图库如果开着，那张新卡片自己亮一圈强调色描边。
        """
        cfg = self.app.cfg
        if cfg.storage.play_sound:
            play_sound("shot")
        label = cfg.category_label(record.category)
        self.hub.archived(record.filename, label, record.confidence,
                          quiet_tooltip=self._tooltip(record, label))
        self.app.reveal_new_shot(record)

    def failed(self, reason: str) -> None:
        """失败必须看见 —— 这是唯一允许打断的情况。"""
        play_sound("error")
        self.hub.failed(reason, where="SnapVault 这一步没成功")

    def info(self, message: str) -> None:
        """用户主动操作的回执（比如"剪贴板里没有图片"），走浮层但不刺眼。"""
        self.hub.info("SnapVault", message)

    # -- L1：长任务 -------------------------------------------------------- #

    def batch_start(self, total: int) -> None:
        self._batch = self.hub.start_batch(total)

    def batch_progress(self, done: int, total: int) -> None:
        self.hub.batch_progress(self._batch, done, total)

    def batch_done(self, done: int, failed: int = 0) -> None:
        self.hub.batch_done(self._batch, done, failed)
        self._batch = None

    # -- 内部 -------------------------------------------------------------- #

    @staticmethod
    def _tooltip(record: ShotRecord, label: str) -> str:
        """托盘 tooltip —— 这是"不弹窗"的兜底信息出口。"""
        conf = f"（{record.confidence:.0%}）" if record.confidence else ""
        return (f"{APP_TITLE}\n"
                f"上次归档 {record.time_human}\n"
                f"{label}{conf}\n"
                f"{record.filename[:60]}\n"
                f"—— 右键打开图库")


# --------------------------------------------------------------------------- #
# 主应用
# --------------------------------------------------------------------------- #

class SnapVaultApp(QObject):
    """托盘常驻的截图归档服务。"""

    def __init__(self, qapp: QApplication, cfg: Config | None = None) -> None:
        super().__init__()
        self.qapp = qapp
        self.cfg = cfg or load_config()
        self.vault = Vault(self.cfg)
        self.tray: QSystemTrayIcon | None = None
        self.gallery: GalleryWindow | None = None
        self._overlay = None                     # 悬浮层单例（惰性创建）
        self._hotkeys = winapi.GlobalHotkeyManager()
        self._hotkey_failures: list[tuple[str, str]] = []
        self._settings_open = False
        self._busy = threading.Lock()
        #: 索引是否已建（惰性，见 ensure_index）
        self._index_ready = False

        self.bridge = HotkeyBridge(self)
        self.bridge.triggered.connect(self._on_hotkey, Qt.QueuedConnection)

        #: 系统截图接管器（惰性创建，按配置启停）
        self.watcher: SystemShotWatcher | None = None
        self.watch_bridge = WatchBridge(self)
        self.watch_bridge.archived.connect(self._on_watch_event, Qt.QueuedConnection)

        self.feedback = TrayFeedback(self)
        self.service = ShotService(self.cfg, vault=self.vault, feedback=self.feedback)
        self.service.qapp = qapp                 # 让流水线能用 Qt 剪贴板

        capture.prepare()
        self.apply_theme()
        self._build_tray()
        self.register_hotkeys()
        self.sync_autostart()
        # rebuild_index() 不在这里调用 —— 见 defer_index_rebuild() 的说明。
        # 启动时逐个解码 800 多张图建索引，会让常驻进程的峰值内存停在 500MB 以上，
        # 而绝大多数时候用户根本不会打开图库。改成第一次打开图库时再建。
        self.sync_watcher()
        self._schedule_qss_warmup()

    # ------------------------------------------------------------------ #
    # 主题 / 图标
    # ------------------------------------------------------------------ #

    def apply_theme(self) -> None:
        """套用主题。

        三件事按顺序做，顺序不能换：

        1. :func:`snapvault.theme.apply` —— 注册随包字体（必须在 QSS 之前，
           否则 ``font-family: "HarmonyOS Sans SC"`` 认不出来）、设 Fusion
           （Windows 默认风格会自作主张画原生控件）、设调色板 + QSS + 全局字体；
        2. 通知自绘控件（缩略图委托 / 自绘按钮 / 浮层）换主题 ——
           QSS 管不到 ``QStyledItemDelegate`` 和 ``paintEvent``；
        3. unpolish/polish 顶层窗口 —— 样式表只在控件被 polish 时重新求值，
           已经在屏幕上的窗口不会自动重画，浅色主题下会残留深色卡片。
        """
        name = "light" if self.cfg.ui.theme == "light" else "dark"
        theme.set_reduce_motion(self.cfg.ui.reduce_motion)
        # 换主题前先给可见窗口拍一张 —— 换完再拍就拍到新配色了，
        # 交叉淡出会变成"自己淡自己"。见 theme.snapshot_for_cross_fade。
        shots = theme.snapshot_for_cross_fade(self.qapp.topLevelWidgets())
        theme.apply(self.qapp, name)
        self.feedback.set_theme(name)
        if self.gallery is not None:
            self.gallery.set_theme(name)
            self.gallery.set_watch_state(bool(self.cfg.watch.enabled),
                                         len(self.watcher.dirs()) if self.watcher
                                         else 0)
        for widget in self.qapp.topLevelWidgets():
            style = widget.style()
            style.unpolish(widget)
            style.polish(widget)
            # 自绘控件不吃 QSS，得自己重刷。设置面板就是这种情况 ——
            # 而且用户换主题的那个下拉框**就在设置面板里**，所以它一定是
            # 开着的：不刷的话左栏 6 行导航会停在旧配色上。
            if isinstance(widget, SettingsDialog):
                widget.set_theme(name)
            for child in widget.findChildren(QWidget):
                cstyle = child.style()
                cstyle.unpolish(child)
                cstyle.polish(child)
            widget.update()
        # 全部落定后再贴旧样子淡出，否则淡出的还是没重排完的旧布局。
        theme.cross_fade(shots)

    def icon(self, size: int = 64) -> QIcon:
        return app_icon(size)

    def _schedule_qss_warmup(self) -> None:
        """把 QSS 编译费挪到启动后的空闲一拍（只做一次）。

        实测：第一个样式化控件要付 598 ms 一次性编译费。不挪的话这笔钱
        落在"用户点开图库"那一刻 —— 刚点完、正等着看图的那一下，最难受。
        挪到托盘起来之后的空闲时刻：托盘图标照常秒出，账也已经结清了。

        为什么不在 :meth:`apply_theme` 里直接调：那条路主题切换也要走，
        切换时缓存本来就热，再预热一次等于让每次换主题平白多卡 600 ms。
        """
        QTimer.singleShot(0, self._warmup_qss)

    def _warmup_qss(self) -> None:
        if getattr(self, "_qss_warm", False):
            return
        self._qss_warm = True
        try:
            theme.warmup(self.qapp)
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # 托盘
    # ------------------------------------------------------------------ #

    def _build_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            print("[snapvault] 系统托盘不可用，将以窗口模式运行", file=sys.stderr)
            return
        self.tray = QSystemTrayIcon(self.icon(64), self)
        self.tray.setToolTip(f"{APP_TITLE}\n给系统截图自动命名并分类")
        self.tray.activated.connect(self._on_tray_activated)
        self._rebuild_tray_menu()
        self.tray.show()
        # 托盘建好才谈得上"在图标上做微动效"，所以反馈和托盘的绑定放在这里。
        self.feedback.bind_tray(self.tray, lambda: self.icon(64))

    def _rebuild_tray_menu(self) -> None:
        """托盘菜单。只留「命名与分类」这一条线。

        区域/窗口/全屏/重复截图、从剪贴板导入这些入口全部拿掉了 ——
        截图交给系统自带的 Win+Shift+S，SnapVault 只负责落盘之后的命名与分类。
        """
        if self.tray is None:
            return
        menu = QMenu()

        gal = QAction("打开图库", menu)
        gal.triggered.connect(self.open_gallery)
        menu.addAction(gal)

        menu.addSeparator()

        watch_act = QAction("接管系统截图（AI 命名 + 分类）", menu)
        watch_act.setCheckable(True)
        watch_act.setChecked(bool(self.cfg.watch.enabled))
        watch_act.toggled.connect(self.toggle_watch)
        menu.addAction(watch_act)

        sweep_act = QAction("立即接管一次", menu)
        sweep_act.triggered.connect(self.sweep_now)
        menu.addAction(sweep_act)

        menu.addSeparator()

        settings = QAction("设置…", menu)
        settings.triggered.connect(self.open_settings)
        menu.addAction(settings)

        folder = QAction("打开图库目录", menu)
        folder.triggered.connect(self.open_vault_dir)
        menu.addAction(folder)

        menu.addSeparator()

        quit_act = QAction("退出 SnapVault", menu)
        quit_act.triggered.connect(self.quit)
        menu.addAction(quit_act)

        self._tray_menu = menu          # 防止被回收
        self.tray.setContextMenu(menu)

    def _on_tray_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.open_gallery()

    def notify(self, title: str, message: str, msecs: int = 3200) -> None:
        """兼容层：把原来"托盘气泡"的调用点接到自绘浮层上。

        改版前这里是 ``QSystemTrayIcon.showMessage`` —— 会进通知中心、
        会被专注助手吞、样式被系统写死，只能塞 4 行纯文本。
        现在统一走右下角自绘浮层：不抢焦点、不进通知中心、能塞结构化信息。
        """
        self.feedback.hub.info(title, message)

    # ------------------------------------------------------------------ #
    # 热键
    # ------------------------------------------------------------------ #

    def register_hotkeys(self) -> list[tuple[str, str]]:
        """只注册「打开图库」一个热键。

        以前还注册了区域/窗口/全屏/重复截图四个全局热键，和系统自带的
        ``Win+Shift+S`` 抢键位、还占着后台钩子。现在截图归系统，热键就只剩这一个。
        """
        self._hotkeys.stop()
        bindings: dict[str, object] = {}
        spec = (getattr(self.cfg.hotkeys, "gallery", "") or "").strip()
        if spec:
            bindings[spec] = self.bridge.make("gallery")
        registered, failed = self._hotkeys.start(bindings)
        self._hotkey_failures = failed
        self._registered = registered
        return failed

    def _on_hotkey(self, action: str) -> None:
        self.perform(action)

    # ------------------------------------------------------------------ #
    # 动作分发
    # ------------------------------------------------------------------ #

    def perform(self, action: str) -> None:
        """执行一个截图/界面动作（托盘菜单、热键、图库按钮共用）。"""
        from PySide6.QtWidgets import QApplication as _QApp

        if action == "gallery":
            self.open_gallery()
            return
        if action == "settings":
            self.open_settings()
            return
        if action == "clipboard":
            self.service.capture_clipboard()
            return
        if not self._busy.acquire(blocking=False):
            self.notify("SnapVault", "上一张截图还在处理，请稍候")
            return
        try:
            _QApp.processEvents()
            result = None
            if action == "region":
                result = self.run_region_selection()
            elif action == "window":
                result = self.run_window_pick()
            elif action == "fullscreen":
                result = self.service.capture_fullscreen(copied=self.cfg.storage.copy_to_clipboard)
            elif action == "all_screens":
                result = self.service.capture_all_screens(copied=self.cfg.storage.copy_to_clipboard)
            elif action in ("repeat_last", "last"):
                result = self.service.capture_last_region(copied=self.cfg.storage.copy_to_clipboard)
            else:
                print(f"[snapvault] 未知动作 {action}", file=sys.stderr)
            if result is not None and result.ok:
                self.after_capture(result)
        finally:
            self._busy.release()

    def run_region_selection(self):
        """弹出区域选择悬浮层，用户确认后归档。"""
        from .overlay import OverlayMode, SelectionOverlay

        frozen = capture.freeze()
        if frozen is None:
            self.feedback.failed("无法捕获屏幕内容（可能是受保护区域）")
            return None
        overlay = SelectionOverlay(frozen, OverlayMode.REGION, cfg=self.cfg)
        outcome = overlay.run()
        if outcome is None or not outcome.ok:
            return None
        # 悬浮层在区域模式下也可能命中一个窗口（用户在窗口上单击），
        # 此时 rect 就是窗口矩形，直接按窗口截图更完整（带阴影、修掉最小化）。
        if outcome.window is not None:
            return self.service.capture_window(outcome.window,
                                               copied=self.cfg.storage.copy_to_clipboard)
        rect = outcome.rect
        if not rect or rect[2] - rect[0] < 1 or rect[3] - rect[1] < 1:
            return None
        return self.service.capture_region(rect, frozen=frozen,
                                           copied=self.cfg.storage.copy_to_clipboard)

    def run_window_pick(self):
        """窗口模式：让用户点一个窗口（悬浮层里可切换）。"""
        from .overlay import OverlayMode, SelectionOverlay

        frozen = capture.freeze()
        if frozen is None:
            self.feedback.failed("无法捕获屏幕内容")
            return None
        overlay = SelectionOverlay(frozen, OverlayMode.WINDOW, cfg=self.cfg)
        outcome = overlay.run()
        if outcome is None or not outcome.ok:
            return None
        if outcome.window is not None:
            return self.service.capture_window(outcome.window,
                                               copied=self.cfg.storage.copy_to_clipboard)
        if outcome.rect:
            return self.service.capture_region(outcome.rect, frozen=frozen,
                                               copied=self.cfg.storage.copy_to_clipboard)
        return None

    def after_capture(self, result) -> None:
        """截图成功后的附加动作（打开编辑器等）。反馈气泡已在 TrayFeedback 里。"""
        rec = result.record
        if rec is None:
            return
        if self.cfg.storage.open_editor_after:
            try:
                os.startfile(str(self.vault.abs_path(rec)))
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # 系统截图接管
    # ------------------------------------------------------------------ #

    def sync_watcher(self) -> dict:
        """按配置启停接管器，返回状态摘要。

        接管器是常驻对象：``cfg`` 每次同步（设置改了目录/间隔能即时生效），
        只按 ``watch.enabled`` 决定线程起停。
        """
        if self.watcher is None:
            self.watcher = SystemShotWatcher(
                self.cfg, self.service,
                on_event=self.watch_bridge.archived.emit,
            )
        else:
            self.watcher.cfg = self.cfg
        if self.cfg.watch.enabled:
            self.watcher.start()
        else:
            self.watcher.stop()
        return self.watcher.status()

    def toggle_watch(self, checked: bool) -> None:
        """托盘菜单里的接管开关。"""
        self.cfg.watch.enabled = bool(checked)
        save_config(self.cfg)
        status = self.sync_watcher()
        self._sync_watch_state()
        # 开关是用户主动做的动作，必须给回执；但走浮层，不抢焦点。
        if not checked:
            self.feedback.hub.info("接管已暂停", "系统截图不再被自动处理")
            return
        if status["missing"]:
            self.feedback.hub.warn("接管已开启（目录暂不存在）",
                                   "监视：" + "、".join(status["missing"]))
        elif status["dirs"]:
            self.feedback.hub.info("接管已开启",
                                   "监视 " + "、".join(status["dirs"]))
        else:
            self.feedback.hub.warn("接管未生效", "没有可监视的目录，请在设置里指定")

    def sweep_now(self) -> None:
        """立刻把监视目录里积压的图片接管一批（不必等轮询）。

        单次上限 :data:`SWEEP_BATCH` 张，积压多的时候先弹窗确认——补跑是真的在调 AI。
        """
        self.sync_watcher()
        if self.watcher is None:
            self.notify("接管未生效", "接管器未创建")
            return
        if not self.watcher.dirs():
            self.notify("接管未生效", "没有可监视的目录，请在设置里指定")
            return

        pending = 0
        try:
            pending = self.watcher.pending_count()
        except Exception:
            pass
        if pending > SWEEP_BATCH:
            answer = QMessageBox.question(
                self.gallery, "接管积压截图",
                f"监视目录里还有 {pending} 张没处理过的截图。\n\n"
                f"每张都会调用一次 AI 识别（消耗额度），本次先处理前 {SWEEP_BATCH} 张，"
                "要全部处理就再点几次。\n\n继续吗？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
            )
            if answer != QMessageBox.Yes:
                return

        try:
            events = self.watcher.scan_once(force=True, limit=SWEEP_BATCH)
        except Exception as exc:
            self.feedback.hub.failed(str(exc), where="扫描失败")
            return
        ok = sum(1 for e in events if e.ok)
        if not events:
            self.feedback.hub.info("立即接管", "监视目录里没有新的截图")
            return
        failed = len(events) - ok
        left = ""
        try:
            rest = self.watcher.pending_count()
            if rest:
                left = f"，还剩 {rest} 张"
        except Exception:
            pass
        self.feedback.batch_done(ok, failed)
        if left:
            self.feedback.hub.info(f"已处理 {ok} 张{left}", "再点一次「立即接管」继续")

    def _on_watch_event(self, event) -> None:
        """接管线程处理完一张图（信号已把它投递到主线程）。"""
        if not event.ok:
            # 失败必须可见，走浮层。
            self.feedback.hub.failed(f"{event.source.name}：{event.error}",
                                     where="接管系统截图失败")
            return
        rec = event.record
        if rec is None:
            return
        # 成功**完全静默**：托盘图标脉冲一下就够了，不弹任何窗口。
        # 这是"每次执行时默默完成任务"最直接的落点 —— 用户在连续截十几张图时，
        # 屏幕上不会出现十几个先后跳出来的气泡。
        label = self.cfg.category_label(rec.category)
        self.feedback.hub.archived(rec.filename, label, rec.confidence,
                                   quiet_tooltip=self.feedback._tooltip(rec, label))
        self.reveal_new_shot(rec)

    # ------------------------------------------------------------------ #
    # 图库 / 设置
    # ------------------------------------------------------------------ #

    def open_gallery(self) -> None:
        # 索引推迟到这里才建 —— 见 defer_index_rebuild()。
        self.ensure_index()
        if self.gallery is None:
            self.gallery = GalleryWindow(self.vault, self.cfg)
            self.gallery.set_theme(self.cfg.ui.theme)
            self.gallery.setWindowIcon(self.icon(64))
            self.gallery.capture_requested.connect(self.perform)
            self.gallery.settings_requested.connect(self.open_settings)
            self.gallery.quit_requested.connect(self.quit)
            self.gallery.on_paste = lambda: self.perform("clipboard")
        else:
            self.gallery.cfg = self.cfg
        self._sync_watch_state()
        self.gallery.reload()
        # 淡入只给"从无到有"的那一次：窗口已经开着再点托盘菜单，只是唤到前台，
        # 不该再演一遍。判据用 show() 之前的可见性。
        appeared = not self.gallery.isVisible()
        self.gallery.show()
        self.gallery.raise_()
        self.gallery.activateWindow()
        if appeared:
            theme.fade_window_in(self.gallery)

    def refresh_gallery_if_open(self) -> None:
        if self.gallery is not None and self.gallery.isVisible():
            self.gallery.reload()

    def reveal_new_shot(self, record: ShotRecord) -> None:
        """新截图入库：图库开着就选中它并让它亮一下 —— 这是成功时唯一的视觉反馈。"""
        if self.gallery is not None and self.gallery.isVisible():
            self.gallery.notify_saved(record)

    def _sync_watch_state(self) -> None:
        """把接管状态同步到图库状态栏那颗呼吸的圆点。"""
        if self.gallery is None:
            return
        dirs = 0
        if self.watcher is not None:
            try:
                dirs = len(self.watcher.dirs())
            except Exception:
                dirs = 0
        self.gallery.set_watch_state(bool(self.cfg.watch.enabled), dirs)

    # ------------------------------------------------------------------ #
    # 索引：从"启动就建"改成"要用才建"
    # ------------------------------------------------------------------ #

    def ensure_index(self) -> None:
        """确保索引存在。惰性建，且只建一次。

        **为什么要改**：改版前 ``__init__`` 里直接调 ``vault.rebuild_index()``。
        实测常驻进程启动后 23 分钟内存停在 **536 MB**（启动瞬间 84 MB），
        而同期图库窗口一次都没打开过。原因是 rebuild_index 会逐个解码
        800 多张 1280×800 的截图，每张解出来约 4 MB 的 RGB 缓冲，
        CPython 的分配器不会把这些大块还给操作系统 —— 峰值就此留住。

        而磁盘上的缩略图缓存一共才 11.8 MB，说明这 500MB 是纯浪费。

        现在：启动只跑 watcher（它对内存几乎无要求），索引推迟到第一次
        真正要看图的时候。代价是首次打开图库慢一点点，换来常驻内存从
        500MB 量级回到 100MB 以内 —— 这正是"轻量化"该有的样子。
        """
        if self._index_ready:
            return
        try:
            self.vault.rebuild_index()
        except Exception as exc:
            print(f"[snapvault] 建索引失败：{exc}", file=sys.stderr)
        self._index_ready = True

    def open_vault_dir(self) -> None:
        try:
            os.startfile(str(self.vault.root))
        except Exception as exc:
            self.notify("无法打开目录", str(exc))

    def open_settings(self) -> None:
        if self._settings_open:
            return
        self._settings_open = True
        try:
            dlg = SettingsDialog(self.cfg, on_save=self.apply_settings,
                                 on_reclassify=self.run_reclassify,
                                 parent=self.gallery)
            dlg.set_category_usage(self._category_usage())
            dlg.exec()
        finally:
            self._settings_open = False

    def _category_usage(self) -> dict[str, int]:
        """各分类当前有多少张图 —— 删分类时提示影响面用。"""
        usage: dict[str, int] = {}
        try:
            for rec in self.vault.search("", limit=1_000_000):
                usage[rec.category] = usage.get(rec.category, 0) + 1
        except Exception:
            pass
        return usage

    def run_reclassify(self, cats: list[str], rules: list) -> None:
        """设置面板发来的一键重归类请求。

        用面板里**刚改好但还没保存**的分类设置跑（用户期望的就是"按我刚配的
        来"），所以先把它们灌进 cfg 和 vault，跑完不落盘 —— 用户点不点保存
        是他自己的决定，重归类只是预览效果。
        """
        self.cfg.categories = list(cats)
        self.cfg.category_rules = list(rules)
        self.vault.rebind(self.cfg)
        total = self.vault.count()

        dlg = QProgressDialog("正在重新归类…", "取消", 0, max(1, total), self.gallery)
        dlg.setWindowTitle("重新归类")
        dlg.setMinimumDuration(0)
        dlg.setValue(0)

        bridge = ReclassifyBridge(self.vault)
        thread = QThread()
        bridge.moveToThread(thread)
        thread.started.connect(bridge.run)
        bridge.progress.connect(lambda d, t: (
            dlg.setMaximum(max(1, t)), dlg.setValue(d)))
        bridge.finished.connect(lambda d, c, err: self._reclassify_done(
            dlg, thread, bridge, d, c, err))
        dlg.canceled.connect(thread.requestInterruption)
        # 线程和信使都要留住，否则跑一半被垃圾回收就前功尽弃
        self._reclass_thread = thread
        self._reclass_bridge = bridge
        thread.start()

    def _reclassify_done(self, dlg, thread, bridge, done: int,
                         changed: int, err: str) -> None:
        try:
            if err:
                QMessageBox.warning(self.gallery, "重归类失败", err)
            else:
                dlg.setValue(dlg.maximum())
                self.notify("SnapVault", f"重新归类完成：{changed} 张分类有变化")
        finally:
            thread.quit()
            thread.wait(3000)
            dlg.deleteLater()
            self._reclass_thread = None
            self._reclass_bridge = None
            if self.gallery is not None:
                self.gallery.reload()

    def apply_settings(self, cfg: Config) -> None:
        """设置保存后：落盘、重建热键、刷新图库、同步开机启动。"""
        old = self.cfg
        self.cfg = cfg
        save_config(cfg)
        # rebind 而不是裸写 cfg：图库目录 / 命名模板都是构造时派生进 vault 的，
        # 只换 cfg 的话改了目录还往旧处写、改了模板还用旧名。
        self.vault.rebind(cfg)
        self.service.cfg = cfg
        if self.gallery is not None:
            self.gallery.cfg = cfg
        self.apply_theme()
        self.sync_autostart()
        self.sync_watcher()
        failed = self.register_hotkeys()
        if self.tray is not None:
            self.tray.setToolTip(f"{APP_TITLE}\n热键：{cfg.hotkeys.region or '未绑定'}")
        self._rebuild_tray_menu()
        if self.gallery is not None:
            # 记录一张都没变的情况下不做全量 reload（890 张要 76–400 ms），
            # 只把"外壳"刷新一遍 —— 分类行的名称 / 图标 / 顺序、卡片重绘。
            if _affects_records(old, cfg):
                self.gallery.reload()
            else:
                self.gallery.refresh_chrome()
        if failed:
            detail = "\n".join(f"  {spec} → {reason}" for spec, reason in failed)
            QMessageBox.warning(self.gallery, "部分热键未生效",
                                f"以下热键被系统或其他程序占用：\n{detail}")
            self.notify("SnapVault", f"{len(failed)} 个热键被占用")
        else:
            self.notify("SnapVault", "设置已保存")

    def sync_autostart(self) -> None:
        """按配置增删开机启动项。"""
        try:
            script = Path(__file__).resolve().parent.parent / "snapvault.pyw"
            exe = Path(sys.executable)
            if getattr(sys, "frozen", False):
                exe = Path(sys.executable)
                command = f'"{exe}" --minimized'
            else:
                command = f'"{exe}" "{script}"'
            winapi.set_autostart(bool(self.cfg.ui.autostart), command)
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    def start(self, show_gallery: bool = False) -> None:
        if self._hotkey_failures:
            detail = "；".join(f"{spec}（{reason}）" for spec, reason in self._hotkey_failures)
            self.notify("部分热键被占用", detail, 6000)
        if show_gallery or self.tray is None:
            self.open_gallery()

    def quit(self) -> None:
        if self.watcher is not None:
            try:
                self.watcher.stop()
            except Exception:
                pass
        try:
            self._hotkeys.stop()
        except Exception:
            pass
        if self.gallery is not None:
            self.gallery.close()
        if self.tray is not None:
            self.tray.hide()
        try:
            self.vault.close()
        except Exception:
            pass
        self.qapp.quit()


def build_qapplication(argv: list[str] | None = None) -> QApplication:
    """创建 QApplication 并设定高 DPI 策略（必须在 QApplication 之前调用）。

    这里同时做两件"必须在任何窗口出现之前"的事：

    1. **任务栏身份**（``SetCurrentProcessExplicitAppUserModelID``）。
       进程是 pythonw.exe 时，没有显式 AppID，任务栏按钮就用 pythonw 的
       Python logo，而且多个 pythonw 实例互不分组。必须在第一个窗口
       创建之前调用，晚一步按钮已按"无 AppID"登记，改不回去。
    2. **应用级图标**：设在 QApplication 上，所有窗口（含还没建的设置面板、
       消息框）自动继承，不用逐个 setWindowIcon。
    """
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    winapi.set_app_user_model_id()
    qapp = QApplication(argv if argv is not None else sys.argv)
    qapp.setApplicationName(APP_NAME)
    qapp.setApplicationDisplayName(APP_TITLE)
    qapp.setQuitOnLastWindowClosed(False)
    qapp.setWindowIcon(app_icon(64))
    return qapp


def _opt_value(raw: list[str], flag: str) -> str | None:
    """从 argv 里取 ``--flag VALUE`` 或 ``--flag=VALUE`` 的值。"""
    for i, a in enumerate(raw):
        if a == flag:
            return raw[i + 1] if i + 1 < len(raw) else None
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
    return None


def resolve_config(raw: list[str]) -> Config:
    """按命令行解析出配置：``--config FILE`` 换配置文件，``--vault DIR`` 临时换图库目录。

    为什么要把这两行单独抽成函数
    ----------------------------
    原来这里是裸的 ``cfg = load_config()``，于是 ``--config`` 和 ``--vault``
    **被静默丢掉**。坑在于它只影响常驻分支：``__main__.py`` 里已经正确解析过一次
    （``--capture`` / ``--stats`` / ``--doctor`` 这些一次性命令走的是对的那份），
    但托盘分支又把那个 cfg 扔掉、重新读了一遍默认配置 ——
    也就是说 ``SnapVault.exe --config X --minimized`` 会安静地按真实配置跑，
    不报错、不警告。这个 bug 是打包后做无副作用启动测试时踩出来的。

    抽出来之后成为一个纯函数，自检可以直接断言这条路径，
    不用真去起一个托盘进程。
    """
    cfg = load_config(_opt_value(raw, "--config"))
    vault = _opt_value(raw, "--vault")
    if vault:
        cfg.storage.vault_dir = vault
    return cfg


def main(argv: list[str] | None = None) -> int:
    """入口：``python -m snapvault`` 与 ``snapvault.pyw`` 都走这里。

    识别 ``--gallery``/``--show``（启动即开图库）、``--minimized``（只留托盘）、
    ``--config``（指定配置文件）、``--vault``（临时指定图库目录）
    与 ``--version``（打印版本后直接退出，方便脚本探测解释器是否装齐依赖）。
    """
    raw = list(argv if argv is not None else sys.argv[1:])
    if any(a in ("--version", "-V") for a in raw):
        from . import __version__

        print(f"SnapVault {__version__}")
        return 0

    show_gallery = any(a in ("--gallery", "--show") for a in raw)
    minimized = "--minimized" in raw

    qapp = build_qapplication([sys.argv[0]] + [a for a in raw if a.startswith("--")])
    cfg = resolve_config(raw)
    app = SnapVaultApp(qapp, cfg)
    app.start(show_gallery=show_gallery or (not minimized and not cfg.ui.start_minimized))
    return qapp.exec()
