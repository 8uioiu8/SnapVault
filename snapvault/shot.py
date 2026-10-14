"""截图流水线：一次"捕获 → 分析 → 分类 → 命名 → 归档 → 反馈"的编排。

UI 层（悬浮层 / 图库 / 托盘）只负责拿到用户意图，真正的落盘逻辑集中在这里，
这样"截一张图"可以在没有界面的情况下被自动化测试完整驱动。

反馈（提示气泡 / 声音 / 剪贴板）通过 :class:`Feedback` 协议注入：GUI 用
:class:`QtFeedback` 弹气泡，测试用 :class:`NullFeedback` 收口，双方互不依赖。
"""

from __future__ import annotations

import os
import sys
import contextlib
import json
import os
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Protocol

from PIL import Image

from . import capture, winapi
from .analyzer import Features, analyze
from .classifier import Prediction, ShotContext, build_classifier
from .config import Config
from .vault import ShotRecord, Vault

# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #

def _log(msg: str, exc: bool = False) -> None:
    try:
        buf = getattr(sys.stderr, "buffer", None)
        line = f"[snapvault.shot] {msg}\n"
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


# --------------------------------------------------------------------------- #
# 反馈通道
# --------------------------------------------------------------------------- #

class Feedback(Protocol):
    """截图完成后的用户反馈（托盘气泡 / 声音 / 剪贴板提示）。"""

    def saved(self, record: ShotRecord, *, copied: bool, tagged: list[str]) -> None: ...
    def failed(self, reason: str) -> None: ...
    def info(self, message: str) -> None: ...


class NullFeedback:
    """静默反馈：测试与无人值守场景用。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []

    def saved(self, record: ShotRecord, *, copied: bool, tagged: list[str]) -> None:
        self.events.append(("saved", (record.filename, copied, list(tagged))))

    def failed(self, reason: str) -> None:
        self.events.append(("failed", reason))

    def info(self, message: str) -> None:
        self.events.append(("info", message))


# --------------------------------------------------------------------------- #
# 结果
# --------------------------------------------------------------------------- #

@dataclass
class ShotResult:
    ok: bool
    record: ShotRecord | None = None
    copied: bool = False
    error: str = ""
    elapsed_ms: int = 0
    cancelled: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def filename(self) -> str:
        return self.record.filename if self.record else ""

    @property
    def category(self) -> str:
        return self.record.category if self.record else ""


# --------------------------------------------------------------------------- #
# 服务
# --------------------------------------------------------------------------- #

class ShotService:
    """把"截一张图"的完整流程封装成一个可调用对象。"""

    def __init__(self, cfg: Config | None = None, vault: Vault | None = None,
                 feedback: Feedback | None = None,
                 classifier: Any | None = None) -> None:
        self.cfg = cfg or Config()
        self.vault = vault or Vault(self.cfg)
        self.feedback: Feedback = feedback or NullFeedback()
        self._classifier = classifier
        #: 最近一次成功归档的记录（供"重复上次区域"使用）
        self.last: ShotRecord | None = None
        self.last_rect: tuple[int, int, int, int] | None = None
        self.last_mode: str = "region"
        #: 由 GUI 层注入的 QApplication；存在时截图会真正写进系统剪贴板
        self.qapp: Any = None
        self._lock = threading.RLock()
        # 坐标正确性的前提：必须在本进程里开启 DPI 感知，否则 GetSystemMetrics
        # 返回的是被系统缩放过的假坐标，截出来的图会只有屏幕的一部分。
        capture.prepare()
        if self.cfg.capture.remember_last_rect:
            self.last_rect = self._load_last_rect()

    # -- 分类器 ------------------------------------------------------------ #

    @property
    def classifier(self):
        if self._classifier is None:
            self._classifier = build_classifier(
                "rules",
                categories=list(self.cfg.classify.__dict__.get("categories", []) or []) or None,
                confidence_floor=self.cfg.classify.confidence_floor,
                use_app_hints=self.cfg.classify.app_hints,
                user_rules=list(getattr(self.cfg, "category_rules", []) or []),
            )
        return self._classifier

    def predict(self, image: Image.Image, ctx: ShotContext) -> tuple[Prediction, Features | None]:
        """分析 + 分类。分类关闭时返回 other/0 与 None。"""
        if not self.cfg.classify.enabled:
            return Prediction(category="other", confidence=0.0, scores={}, reason="分类已关闭"), None
        features = analyze(image, max_edge_rows=self.cfg.classify.probe_rows)
        try:
            prediction = self.classifier.predict(features, ctx)
        except Exception:
            _log("分类失败，回退 other", exc=True)
            prediction = Prediction(category="other", confidence=0.0, scores={},
                                    reason="分类器异常")
        return prediction, features

    # -- 归档 -------------------------------------------------------------- #

    def archive(self, image: Image.Image, ctx: ShotContext | None = None,
                *, mode: str = "", tags: list[str] | None = None,
                title_hint: str = "", copied: bool = False,
                when: datetime | None = None,
                suppress_feedback: bool = False) -> ShotResult:
        """把一张已经抓到的图归档，并触发反馈。"""
        started = datetime.now()
        if image is None or image.width <= 0 or image.height <= 0:
            return ShotResult(ok=False, error="空图像")
        ctx = ctx or ShotContext()
        if mode:
            ctx.source = mode
        if not ctx.captured_at:
            ctx.captured_at = when or datetime.now()

        try:
            with self._lock:
                prediction, features = self.predict(image, ctx)
                record = self.vault.save(
                    image,
                    prediction=prediction,
                    ctx=ctx,
                    features=features,
                    when=when,
                    tags=tags,
                    title_hint=title_hint,
                    mode=mode or ctx.source or "region",
                    copied=copied,
                )
        except Exception as exc:
            _log("归档失败", exc=True)
            if not suppress_feedback:
                self.feedback.failed(f"保存失败：{exc}")
            return ShotResult(ok=False, error=str(exc))

        copied = bool(copied and self.cfg.storage.copy_to_clipboard)
        if copied:
            copy_record_to_clipboard(self.vault, record, self.qapp)
        elapsed = int((datetime.now() - started).total_seconds() * 1000)
        if not suppress_feedback:
            self.feedback.saved(record, copied=copied, tagged=list(record.tags or []))

        with self._lock:
            self.last = record
            self.last_mode = record.mode or mode or "region"
        return ShotResult(ok=True, record=record, copied=copied, elapsed_ms=elapsed,
                          extra={"prediction": prediction})

    # -- 各种截图入口 ------------------------------------------------------ #

    def from_capture(self, result: capture.CaptureResult, *, copied: bool = False,
                     tags: list[str] | None = None,
                     when: datetime | None = None) -> ShotResult:
        """从一次捕获结果归档（会记住区域，便于"重复上次区域"）。"""
        started = datetime.now()
        shot = self.archive(
            result.image,
            self._context_for(result),
            mode=result.mode,
            tags=tags,
            title_hint=result.title,
            copied=copied,
            when=when or result.captured_at,
        )
        shot.elapsed_ms = int((datetime.now() - started).total_seconds() * 1000)
        if shot.ok:
            with self._lock:
                self.last_rect = result.rect
                self.last_mode = result.mode
            self._save_last_rect(result.rect)
        return shot

    #: 前台窗口覆盖捕获矩形的比例达到这个值，就认为"截的就是这个窗口"
    CONTEXT_OVERLAP = 0.5

    def _context_for(self, result: capture.CaptureResult) -> ShotContext:
        """给一次捕获挑最合适的应用上下文。

        全屏 / 多屏截图本身没有窗口信息，但"谁在最前面"是分类器最强的信号，
        所以按下面的顺序猜：

        1. 捕获时已经知道窗口（窗口截图、区域截图命中的窗口）→ 直接用；
        2. 前台窗口覆盖了这张图 ``CONTEXT_OVERLAP`` 以上 → 认为截的就是它；
        3. 否则取矩形中心点下的顶层窗口；
        4. 都不行就只带 mode 与显示器。
        """
        win = result.window
        if win is None:
            rect = result.rect if result.rect and result.rect[2] > result.rect[0] else None
            fg = None
            try:
                fg = winapi.foreground_window()
            except Exception:
                fg = None
            if fg is not None and _overlaps(fg.rect, rect) >= self.CONTEXT_OVERLAP:
                win = fg
            elif rect is not None:
                cx = (rect[0] + rect[2]) // 2
                cy = (rect[1] + rect[3]) // 2
                try:
                    win = winapi.top_window_at(cx, cy)
                except Exception:
                    win = None

        ctx = result.context()
        if win is not None:
            ctx.exe = win.exe or ctx.exe
            ctx.title = win.title or ctx.title
            ctx.monitor = win.monitor or ctx.monitor
        ctx.source = result.mode or ctx.source
        return ctx

    def capture_region(self, rect: tuple[int, int, int, int],
                       frozen: capture.FrozenDesktop | None = None,
                       *, copied: bool = False, tags: list[str] | None = None,
                       with_cursor: bool = False) -> ShotResult:
        res = capture.capture_region(rect, frozen=frozen, with_cursor=with_cursor)
        if res is None:
            self.feedback.failed("区域截图失败（可能是受保护窗口或权限不足）")
            return ShotResult(ok=False, error="capture failed")
        # 区域截图的上下文来自该矩形下最靠前的窗口（若调用方没给）
        if res.window is None:
            win = self._window_under(rect)
            if win is not None:
                res.window = win
        return self.from_capture(res, copied=copied, tags=tags)

    def capture_window(self, win: winapi.WindowInfo | int | None = None, *,
                       copied: bool = False, tags: list[str] | None = None,
                       with_cursor: bool = False) -> ShotResult:
        if win is None:
            win = winapi.foreground_window()
        if win is None:
            self.feedback.failed("没有找到可截取的窗口")
            return ShotResult(ok=False, error="no window")
        res = capture.capture_window(win, shadow=True, with_cursor=with_cursor)
        if res is None:
            self.feedback.failed("窗口截图失败")
            return ShotResult(ok=False, error="capture failed")
        return self.from_capture(res, copied=copied, tags=tags)

    def capture_fullscreen(self, monitor: int | None = None, *, copied: bool = False,
                           tags: list[str] | None = None,
                           with_cursor: bool = False) -> ShotResult:
        res = capture.capture_fullscreen(monitor=monitor, with_cursor=with_cursor)
        if res is None:
            self.feedback.failed("全屏截图失败")
            return ShotResult(ok=False, error="capture failed")
        return self.from_capture(res, copied=copied, tags=tags)

    def capture_all_screens(self, *, copied: bool = False,
                            tags: list[str] | None = None) -> ShotResult:
        res = capture.capture_all_screens()
        if res is None:
            self.feedback.failed("多屏截图失败")
            return ShotResult(ok=False, error="capture failed")
        return self.from_capture(res, copied=copied, tags=tags)

    def capture_last_region(self, *, copied: bool = False,
                            tags: list[str] | None = None) -> ShotResult:
        """重复上一次的区域截图。"""
        with self._lock:
            rect = self.last_rect
        if not rect:
            self.feedback.info("还没有可重复的区域，请先框选一次")
            return ShotResult(ok=False, error="no last region")
        res = capture.capture_region(rect)
        if res is None:
            self.feedback.failed("重复区域截图失败")
            return ShotResult(ok=False, error="capture failed")
        return self.from_capture(res, copied=copied, tags=tags)

    def capture_clipboard(self, *, tags: list[str] | None = None) -> ShotResult:
        """把剪贴板里的图片存进图库。"""
        img = winapi.clipboard_image()
        if img is None:
            self.feedback.info("剪贴板里没有图片")
            return ShotResult(ok=False, error="clipboard empty")
        ctx = ShotContext(source="clipboard", captured_at=datetime.now())
        return self.archive(img, ctx, mode="clipboard", tags=tags, title_hint="clipboard")

    def import_file(self, path: str | Path, *, tags: list[str] | None = None,
                    mode: str = "file") -> ShotResult:
        """把磁盘上的图片导入图库（拖拽 / 批量整理用）。"""
        p = Path(path)
        try:
            with Image.open(p) as im:
                img = im.convert("RGB").copy()
        except Exception as exc:
            # 文件不存在 / 不是图片都走这里，是预期内的用户输入错误，
            # 所以只记一行，不打印 traceback（否则 CLI 批量导入时刷屏）。
            _log(f"导入失败 {p}: {exc}")
            self.feedback.failed(f"无法读取 {p.name}：{exc}")
            return ShotResult(ok=False, error=str(exc))
        ctx = ShotContext(source=mode, captured_at=datetime.fromtimestamp(p.stat().st_mtime),
                          title=p.stem)
        # 关键：把「原文件的修改时间」当成归档时间传下去。
        # 不传的话 vault.save() 会退回 datetime.now()，于是批量规整老截图时
        # 每张都会被盖上"今天"的时间戳——文件名和索引里的日期全废。
        # 对系统截图接管来说，mtime 正是截图工具写下这张图的时刻，也是对的。
        return self.archive(img, ctx, mode=mode, tags=tags, title_hint=p.stem,
                            when=ctx.captured_at)

    # -- 辅助 -------------------------------------------------------------- #

    def _window_under(self, rect: tuple[int, int, int, int]) -> winapi.WindowInfo | None:
        cx = (rect[0] + rect[2]) // 2
        cy = (rect[1] + rect[3]) // 2
        try:
            win = winapi.top_window_at(cx, cy)
        except Exception:
            win = None
        return win

    def last_summary(self) -> str:
        """给托盘气泡用的一句话摘要。"""
        rec = self.last
        if rec is None:
            return "还没有截图"
        label = self.cfg.category_label(rec.category)
        return f"{rec.filename}  ·  {label}"

    # -- 上次选区持久化 ---------------------------------------------------- #

    def _state_path(self) -> Path:
        """UI 状态放在图库里（不属于配置），删掉图库就一起消失。"""
        return Path(self.vault.meta_dir) / "state.json"

    def _load_last_rect(self) -> tuple[int, int, int, int] | None:
        """从图库状态里恢复上次选区，让"重复上次区域"跨重启仍然可用。"""
        try:
            raw = json.loads(self._state_path().read_text(encoding="utf-8"))
            x0, y0, x1, y1 = (int(v) for v in raw.get("last_rect", []))
        except Exception:
            return None
        if x1 - x0 < 8 or y1 - y0 < 8:
            return None
        return (x0, y0, x1, y1)

    def _save_last_rect(self, rect: tuple[int, int, int, int]) -> None:
        if not rect:
            return
        path = self._state_path()
        try:
            state: dict[str, Any] = {}
            if path.exists():
                with contextlib.suppress(Exception):
                    state = json.loads(path.read_text(encoding="utf-8"))
            state["last_rect"] = [int(v) for v in rect]
            state["last_mode"] = self.last_mode
            state["updated_at"] = datetime.now().isoformat(timespec="seconds")
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, path)
        except Exception:
            _log("保存上次选区失败")

    def open_last(self) -> bool:
        rec = self.last
        if rec is None:
            return False
        try:
            os.startfile(str(self.vault.abs_path(rec)))   # type: ignore[attr-defined]
            return True
        except Exception:
            _log("打开文件失败", exc=True)
            return False


# --------------------------------------------------------------------------- #
# 剪贴板 / 声音 / 打开（GUI 无关的部分）
# --------------------------------------------------------------------------- #

def copy_image_to_clipboard(image: Image.Image, qapp: Any = None) -> bool:
    """把 PIL 图像放进系统剪贴板。

    GUI 进程里走 Qt（Qt 会自己持有剪贴板数据并在退出时交接），
    没有 Qt 时退回纯 Win32 的 CF_DIB 写入。
    """
    if qapp is None:
        try:
            from PySide6.QtWidgets import QApplication

            if QApplication.instance() is not None:
                qapp = QApplication.instance()
        except Exception:
            qapp = None
    if qapp is not None:
        try:
            from PySide6.QtGui import QImage

            raw, w, h, stride = pil_to_qimage_bytes(image)
            qimg = QImage(raw, w, h, stride, QImage.Format_RGBA8888).copy()
            qapp.clipboard().setImage(qimg)
            return True
        except Exception:
            _log("Qt 写剪贴板失败，回退 Win32", exc=True)
    try:
        from .winapi import set_clipboard_image

        return bool(set_clipboard_image(image))
    except Exception:
        _log("Win32 写剪贴板失败", exc=True)
        return False


def copy_record_to_clipboard(vault: Vault, record: ShotRecord, qapp: Any = None) -> bool:
    """把已归档的文件重新读出来写进剪贴板（保证与磁盘上的一致）。"""
    try:
        with Image.open(vault.abs_path(record)) as im:
            img = im.convert("RGB").copy()
    except Exception:
        return False
    return copy_image_to_clipboard(img, qapp)


def pil_to_qimage_bytes(image: Image.Image) -> tuple[bytes, int, int, int]:
    """返回 ``(字节, 宽, 高, 每行字节)``，供 Qt 构造剪贴板图像。

    PIL 没有 RGB→BGRA 的打包器（``ValueError: No packer found from RGB to BGRA``），
    但 RGBA 的原始字节在小端机器上与 Qt 的 ``Format_ARGB32`` 内存布局完全一致，
    所以这里统一走 RGBA，Qt 侧用 ``Format_RGBA8888``/``Format_ARGB32`` 都能直接读。
    """
    rgba = image if image.mode == "RGBA" else image.convert("RGBA")
    raw = rgba.tobytes("raw", "RGBA")
    return raw, rgba.width, rgba.height, rgba.width * 4


def _overlaps(win_rect: tuple[int, int, int, int] | None,
              rect: tuple[int, int, int, int] | None) -> float:
    """窗口矩形覆盖 ``rect`` 的比例（0~1）。任一为空则返回 0。"""
    if not win_rect or not rect:
        return 0.0
    x0 = max(win_rect[0], rect[0])
    y0 = max(win_rect[1], rect[1])
    x1 = min(win_rect[2], rect[2])
    y1 = min(win_rect[3], rect[3])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    area = max(1, (rect[2] - rect[0]) * (rect[3] - rect[1]))
    return ((x1 - x0) * (y1 - y0)) / area


def play_sound(kind: str = "shot") -> None:
    """播放系统提示音（Windows 自带 wav，无需额外资源）。"""
    if sys.platform != "win32":
        return
    sounds = {
        "shot": r"C:\Windows\Media\Snap.wav",
        "copy": r"C:\Windows\Media\Windows Notify System Generic.wav",
        "error": r"C:\Windows\Media\Windows Critical Stop.wav",
    }
    path = sounds.get(kind, sounds["shot"])
    if not os.path.exists(path):
        path = r"C:\Windows\Media\notify.wav"
    if not os.path.exists(path):
        return
    try:
        import winsound

        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
    except Exception:
        pass


def reveal(path: str | Path) -> None:
    from .config import reveal as _reveal

    _reveal(path)


# --------------------------------------------------------------------------- #
# 便捷函数
# --------------------------------------------------------------------------- #

def quick_capture(cfg: Config | None = None, mode: str = "fullscreen",
                  feedback: Feedback | None = None) -> ShotResult:
    """无界面地截一张图（CLI / 定时任务用）。"""
    service = ShotService(cfg, feedback=feedback)
    if mode == "fullscreen":
        return service.capture_fullscreen()
    if mode == "all_screens":
        return service.capture_all_screens()
    if mode == "window":
        return service.capture_window()
    if mode == "clipboard":
        return service.capture_clipboard()
    return service.capture_last_region()
