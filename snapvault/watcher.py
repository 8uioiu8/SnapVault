"""系统截图接管器：盯着系统截图工具的输出目录，把新截图自动交给 SnapVault 归档。

Windows 自带的「截图工具」是签名的 MSIX 应用（``Microsoft.ScreenSketch``），
本体改不了——改文件签名失效、应用直接起不来，还会被 Store 更新覆盖。但它每次
截图都会把文件写到一个固定目录（默认 ``%USERPROFILE%\\Pictures\\Screenshots``）。

于是这里换个思路：**不碰那个应用，盯着它的输出目录**。一旦有新图片落盘，
先等文件写完（大小稳定且能独占打开），再走 SnapVault 既有的归档流水线：

    新图片落盘 → 稳定检测 → AI 视觉描述 → 内容分类 → 命名 → 分层入库

这样系统自带截图就获得了「AI 快速命名 + 自动分类」的能力，而且：

* 不修改任何系统组件 → 不涉及签名校验，不会被应用更新冲掉；
* 归档动作复用 :meth:`snapvault.shot.ShotService.import_file`，
  分类器 / 命名器 / AI 描述 / 索引全部沿用，不存在两套逻辑；
* AI 未配 Key 或调用失败时自动降级为「日期_时间」命名，不阻塞、不丢图。

设计上刻意做成**纯同步可测**：:meth:`SystemShotWatcher.scan_once` 不依赖线程，
测试里直接把图片丢进临时目录再调一次即可，无需等待。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

#: 会被当作截图的图片扩展名
IMAGE_EXTS = frozenset({
    ".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif", ".tif", ".tiff",
})

#: 明显是临时/隐藏文件的命名特征，直接跳过
_SKIP_PREFIX = ("~$", ".")
_SKIP_SUFFIX = (".part", ".tmp", ".crdownload", ".download", ".partial", ".swp")


#: 已经提醒过的「位于图库内部」的目录（小写路径）。只提醒一次，避免刷屏。
_NOTED_HIDDEN: set = set()


def _log(msg: str, exc: bool = False) -> None:
    """把日志写进 stderr（无控制台时静默）。"""
    try:
        buf = getattr(sys.stderr, "buffer", None)
        line = f"[snapvault.watcher] {msg}\n"
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
# 事件
# --------------------------------------------------------------------------- #

@dataclass
class WatchEvent:
    """一次"被接管"的截图。"""

    source: Path                       # 原始文件（系统截图工具写下的那张）
    ok: bool = False
    record: Any = None                 # ShotRecord | None
    error: str = ""
    action: str = ""                   # kept / recycled / failed

    @property
    def filename(self) -> str:
        rec = self.record
        return getattr(rec, "filename", "") if rec is not None else ""

    @property
    def category(self) -> str:
        rec = self.record
        return getattr(rec, "category", "") if rec is not None else ""


# --------------------------------------------------------------------------- #
# 接管器
# --------------------------------------------------------------------------- #

class SystemShotWatcher:
    """轮询监视若干目录，把新出现的图片导入 SnapVault。

    参数
    ----
    cfg
        :class:`snapvault.config.Config`，只读取其中的 ``watch`` 分组与 ``storage``。
    service
        :class:`snapvault.shot.ShotService`，归档动作全部委托给它。
    on_event
        每处理完一张图回调一次（运行在线程里，GUI 侧需自行转到主线程）。
    """

    def __init__(self, cfg, service, on_event: Callable[[WatchEvent], None] | None = None) -> None:
        self.cfg = cfg
        self.service = service
        self._on_event = on_event

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

        #: path -> (size, unchanged_since)  正在等写入完成的候选
        self._pending: dict[str, tuple[int, float]] = {}
        #: path -> (size, mtime)  已经处理过的文件指纹（重启后仍生效）
        self._seen: dict[str, list[int]] = {}

        self._state_path = Path(service.vault.meta_dir) / "watched.json"
        self._heartbeat_path = Path(service.vault.meta_dir) / "watcher.heartbeat"
        self._last_beat = 0.0
        self._load_state()

    # ------------------------------------------------------------------ #
    # 目录
    # ------------------------------------------------------------------ #

    def dirs(self) -> list[Path]:
        """实际要监视的目录。

        配置为空时回落到系统截图目录。

        关于"自我吞噬"：扫描用 ``iterdir()``，**只看目录的第一层，不递归**。
        所以只要监视目录不是图库根目录本身，归档产物（落在
        ``<图库>/<分类>/`` 子目录里）就不可能被自己再扫到。

        典型布局就是把图库根目录设成 ``D:\\Pictures``、监视目录设成
        ``D:\\Pictures\\Screenshots``——监视目录确实在图库内部，但归档写的是
        ``D:\\Pictures\\网页\\`` 这一层，两者不重叠。所以这里只排除
        "监视目录 == 图库根目录" 这一种真正会死循环的情况。
        """
        from .config import default_screenshots_dir

        raw = [str(d).strip() for d in (getattr(self.cfg.watch, "dirs", []) or []) if str(d).strip()]
        if not raw:
            raw = [default_screenshots_dir()]

        root = self._vault_root()
        out: list[Path] = []
        for item in raw:
            p = Path(os.path.expandvars(item)).expanduser()
            try:
                rp = p.resolve()
            except OSError:
                continue
            if root is not None and rp == root:
                _log(f"跳过图库根目录本身（归档产物会被当成新图反复处理）：{p}")
                continue
            if root is not None and root in rp.parents:
                # 只提醒一次。dirs() 会被心跳线程按拍调用（约 1.5 秒一次），
                # 每次都打一行的话，日志里别的内容会被这条重复提示淹掉 ——
                # 实测一次启动 8 秒就能刷出 10 行一模一样的提示。
                key = str(p).lower()
                if key not in _NOTED_HIDDEN:
                    _NOTED_HIDDEN.add(key)
                    _log(f"注意：监视目录位于图库内部，归档会写到它的同级分类目录：{p}")
            out.append(p)
        return out

    def _vault_root(self) -> Path | None:
        try:
            return Path(self.service.vault.root).resolve()
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    # 一次扫描（纯同步，可测）
    # ------------------------------------------------------------------ #

    def scan_once(self, *, force: bool = False, limit: int = 0) -> list[WatchEvent]:
        """扫一遍所有监视目录。

        ``force=True`` 时跳过"等文件写完"的稳定检测，测试与手工补跑用。
        ``limit>0`` 时最多处理这么多张——手工补跑一定要设上限：
        截图目录里可能压着几百张历史图，无上限就是几百次 AI 调用（真要花钱的）。
        """
        events: list[WatchEvent] = []
        for folder in self.dirs():
            if not folder.is_dir():
                continue
            try:
                entries = sorted(folder.iterdir(), key=lambda p: p.name)
            except OSError as exc:
                _log(f"无法读取 {folder}: {exc}")
                continue
            for path in entries:
                if limit and len(events) >= limit:
                    return events
                if not self._is_candidate(path):
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if stat.st_size <= 0:
                    continue
                key = str(path)
                sig = (int(stat.st_size), int(stat.st_mtime))
                if self._is_seen(key, sig):
                    continue
                if not force and not self._is_settled(key, int(stat.st_size)):
                    continue
                events.append(self._process(path, sig))
        return events

    def pending_count(self) -> int:
        """还没接管过的候选图片数量（不含"等写完"的稳定检测）。"""
        n = 0
        for folder in self.dirs():
            if not folder.is_dir():
                continue
            try:
                entries = list(folder.iterdir())
            except OSError:
                continue
            for path in entries:
                if not self._is_candidate(path):
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if self._is_seen(str(path), (int(stat.st_size), int(stat.st_mtime))):
                    continue
                n += 1
        return n

    def _is_candidate(self, path: Path) -> bool:
        if not path.is_file():
            return False
        name = path.name
        if name.startswith(_SKIP_PREFIX):
            return False
        if name.lower().endswith(_SKIP_SUFFIX):
            return False
        return path.suffix.lower() in IMAGE_EXTS

    def _is_seen(self, key: str, sig: tuple[int, int]) -> bool:
        with self._lock:
            prev = self._seen.get(key)
        return prev is not None and tuple(prev) == sig

    def _is_settled(self, key: str, size: int) -> bool:
        """连续两次扫描大小不变、且已静置 ``settle_ms``，才认为写完了。"""
        now = time.time()
        settle_s = max(0.0, float(getattr(self.cfg.watch, "settle_ms", 1200)) / 1000.0)
        with self._lock:
            prev = self._pending.get(key)
            if prev is None or prev[0] != size:
                self._pending[key] = (size, now)
                return False
            if now - prev[1] < settle_s:
                return False
            self._pending.pop(key, None)
        return True

    # ------------------------------------------------------------------ #
    # 处理单张
    # ------------------------------------------------------------------ #

    def _process(self, path: Path, sig: tuple[int, int]) -> WatchEvent:
        try:
            result = self.service.import_file(path, tags=["系统截图"], mode="system")
        except Exception as exc:                       # 归档异常也不该打断监视
            _log(f"接管失败 {path}: {exc}", exc=True)
            self._mark_seen(str(path), sig)
            event = WatchEvent(source=path, ok=False, error=str(exc), action="failed")
            self._emit(event)
            return event

        if not result.ok:
            _log(f"无法导入 {path}: {result.error}")
            self._mark_seen(str(path), sig)            # 读不了就别每轮重试
            event = WatchEvent(source=path, ok=False, error=result.error, action="failed")
            self._emit(event)
            return event

        self._mark_seen(str(path), sig)
        action = self._handle_source(path)
        event = WatchEvent(source=path, ok=True, record=result.record, action=action)
        self._emit(event)
        return event

    def _handle_source(self, path: Path) -> str:
        """归档完成后如何处理原图。"""
        mode = str(getattr(self.cfg.watch, "source_action", "keep") or "keep").lower()
        if mode in ("keep", "copy", "none"):
            return "kept"
        if mode in ("recycle", "trash"):
            try:
                from .winapi import send_to_recycle_bin

                if send_to_recycle_bin([str(path)]):
                    return "recycled"
            except Exception:
                _log(f"移入回收站失败，保留原图：{path}", exc=True)
            return "kept"
        if mode == "delete":
            try:
                path.unlink()
                return "deleted"
            except OSError:
                _log(f"删除原图失败，保留：{path}", exc=True)
            return "kept"
        return "kept"

    def _emit(self, event: WatchEvent) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(event)
        except Exception:
            _log("事件回调异常", exc=True)

    # ------------------------------------------------------------------ #
    # 已处理指纹的持久化
    # ------------------------------------------------------------------ #

    def _mark_seen(self, key: str, sig: tuple[int, int]) -> None:
        with self._lock:
            self._seen[key] = [int(sig[0]), int(sig[1])]
            if len(self._seen) > 4000:                 # 只留最近的一批，避免状态无限膨胀
                for old in list(self._seen)[:1000]:
                    self._seen.pop(old, None)
            snapshot = dict(self._seen)
        self._save_state(snapshot)

    def _load_state(self) -> None:
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
            seen = raw.get("seen") if isinstance(raw, dict) else None
            if isinstance(seen, dict):
                self._seen = {str(k): list(v)[:2] for k, v in seen.items()}
        except FileNotFoundError:
            pass
        except Exception:
            _log("接管状态读取失败，按空状态启动")

    def _save_state(self, seen: dict[str, list[int]]) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                       "seen": seen}
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._state_path)
        except Exception:
            _log("接管状态保存失败")

    def prime_existing(self) -> int:
        """把目录里已有的图片登记为「已处理」，只接管之后新增的。

        首次启用接管时这一步很关键：截图目录里常常攒着成百上千张历史截图，
        全部送进 AI 既慢又烧额度。想连历史一起处理，先 :meth:`forget_all`
        清空记录，再扫描一次即可。

        返回被登记的图片数量。
        """
        count = 0
        with self._lock:
            for folder in self.dirs():
                if not folder.is_dir():
                    continue
                try:
                    entries = list(folder.iterdir())
                except OSError:
                    continue
                for path in entries:
                    if not self._is_candidate(path):
                        continue
                    try:
                        stat = path.stat()
                    except OSError:
                        continue
                    self._seen[str(path)] = [int(stat.st_size), int(stat.st_mtime)]
                    count += 1
            snapshot = dict(self._seen)
        if count:
            self._save_state(snapshot)
        return count

    def forget_all(self) -> int:
        """清空已处理记录（下次扫描会重新接管目录里的所有图片）。"""
        with self._lock:
            n = len(self._seen)
            self._seen.clear()
            self._pending.clear()
        self._save_state({})
        return n

    # ------------------------------------------------------------------ #
    # 线程生命周期
    # ------------------------------------------------------------------ #

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        if self.running:
            return False
        # 首次启用：先把目录里"已经存在"的图片登记为已处理，只接管之后新增的。
        # 截图目录里往往攒着成百上千张历史截图，一口气全送进 AI 既慢又烧额度。
        # （要补跑历史，先 forget_all() 清空记录再扫描。）
        if getattr(self.cfg.watch, "skip_existing", True) and not self._seen:
            skipped = self.prime_existing()
            if skipped:
                _log(f"已登记 {skipped} 张现有图片为「不用管」，只接管之后新增的截图")
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="snapvault-watcher", daemon=True)
        self._thread.start()
        self._beat(force=True)
        _log(f"接管已启动，监视：{', '.join(str(d) for d in self.dirs()) or '（无有效目录）'}")
        return True

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=timeout)
        self._thread = None

    def _loop(self) -> None:
        interval = max(0.3, float(getattr(self.cfg.watch, "interval_ms", 1500)) / 1000.0)
        while not self._stop.wait(interval):
            self._beat()
            try:
                self.scan_once()
            except Exception:
                _log("扫描异常", exc=True)

    # ------------------------------------------------------------------ #
    # 心跳（供外部进程判断"接管到底在不在跑"）
    # ------------------------------------------------------------------ #

    def _beat(self, *, force: bool = False) -> None:
        """把自身状态写进 ``.snapvault/watcher.heartbeat``。

        接管器运行在托盘进程里，命令行/另一个进程没法直接问它"你还活着吗"。
        所以留个心跳文件：任何人读一下时间戳就知道接管在不在跑。
        默认 ≥5 秒才写一次，避免每 1.5 秒都去碰磁盘。
        """
        now = time.time()
        if not force and now - self._last_beat < 5.0:
            return
        self._last_beat = now
        try:
            self._heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "pid": os.getpid(),
                "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "epoch": int(now),
                "running": self.running,
                "dirs": [str(d) for d in self.dirs()],
                "handled": len(self._seen),
                "interval_ms": int(getattr(self.cfg.watch, "interval_ms", 1500)),
            }
            tmp = self._heartbeat_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._heartbeat_path)
        except Exception:
            pass

    def read_heartbeat(self) -> dict[str, Any]:
        """读回心跳（没有文件返回空 dict）。"""
        try:
            data = json.loads(self._heartbeat_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    # ------------------------------------------------------------------ #
    # 状态摘要（给托盘提示 / 设置面板用）
    # ------------------------------------------------------------------ #

    def status(self) -> dict[str, Any]:
        dirs = self.dirs()
        return {
            "enabled": bool(getattr(self.cfg.watch, "enabled", False)),
            "running": self.running,
            "dirs": [str(d) for d in dirs],
            "missing": [str(d) for d in dirs if not d.is_dir()],
            "handled": len(self._seen),
            "source_action": str(getattr(self.cfg.watch, "source_action", "keep")),
        }


# --------------------------------------------------------------------------- #
# 便捷函数：无界面地补跑一次
# --------------------------------------------------------------------------- #

def sweep_once(cfg, service=None, *, force: bool = True, limit: int = 100,
               tags: Iterable[str] | None = None) -> list[WatchEvent]:
    """把监视目录里积压的图片接管一批（CLI / 托盘「立即接管一次」用）。

    ``limit`` 默认 100：每一次补跑都要真的调 AI，几百张连着跑既慢又花额度。
    想一次跑完就显式传 ``limit=0``。
    """
    from .shot import NullFeedback, ShotService

    svc = service or ShotService(cfg, feedback=NullFeedback())
    watcher = SystemShotWatcher(cfg, svc)
    return watcher.scan_once(force=force, limit=limit)
