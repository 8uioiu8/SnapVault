"""归档存储层：日期目录 + SQLite 索引 + JSONL 审计 + 缩略图缓存.

目录布局::

    <vault_dir>/
        2026/05/                                     # 由 folder_pattern 决定
            20260518_142233_code_snapvault_a1b2c3.png
            ...
        Copies/                                      # 剪贴板路径落盘
        .snapvault/
            index.db        SQLite 元数据索引（搜索/标签/统计的唯一事实源）
            index.jsonl     追加式审计日志（可重建 index.db）
            thumbs/         缩略图缓存（按内容哈希命名）
            .gitignore

文件名即元数据（时间戳 + 分类 + slug），数据库保存标签、窗口标题、特征分数等
无法编码进文件名的信息，二者互为补充：删掉数据库仍可按文件名重建可读归档。
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import sqlite3
import threading
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from PIL import Image

from .analyzer import Features, analyze
from .classifier import Prediction, ShotContext
from .config import Config, provider_needs_key
from .naming import AutoNamer, NamePlan, human_size, normalize_tags, unique_path

META_DIR = ".snapvault"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS shots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    rel_path      TEXT NOT NULL UNIQUE,
    filename      TEXT NOT NULL,
    name_stem     TEXT NOT NULL,
    ext           TEXT NOT NULL,
    captured_at   TEXT NOT NULL,           -- ISO8601
    day           TEXT NOT NULL,           -- YYYY-MM-DD
    year_month    TEXT NOT NULL,           -- YYYY-MM
    category      TEXT NOT NULL,
    category_slug TEXT NOT NULL,
    slug          TEXT,
    title         TEXT,
    ai_desc       TEXT,
    tags          TEXT NOT NULL DEFAULT '[]',
    note          TEXT NOT NULL DEFAULT '',
    favorite      INTEGER NOT NULL DEFAULT 0,
    confidence    REAL NOT NULL DEFAULT 0,
    reason        TEXT,
    features      TEXT NOT NULL DEFAULT '{}',
    width         INTEGER NOT NULL DEFAULT 0,
    height        INTEGER NOT NULL DEFAULT 0,
    bytes         INTEGER NOT NULL DEFAULT 0,
    sha1          TEXT,
    source        TEXT,
    app           TEXT,
    window_title  TEXT,
    monitor       TEXT,
    mode          TEXT,
    copied        INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_shots_day        ON shots(day DESC);
CREATE INDEX IF NOT EXISTS idx_shots_category   ON shots(category);
CREATE INDEX IF NOT EXISTS idx_shots_captured   ON shots(captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_shots_sha1       ON shots(sha1);
CREATE INDEX IF NOT EXISTS idx_shots_favorite   ON shots(favorite);

CREATE TABLE IF NOT EXISTS tags (
    tag     TEXT PRIMARY KEY,
    uses    INTEGER NOT NULL DEFAULT 0,
    last_at TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


# --------------------------------------------------------------------------- #
# 记录模型
# --------------------------------------------------------------------------- #

@dataclass
class ShotRecord:
    rel_path: str = ""
    filename: str = ""
    name_stem: str = ""
    ext: str = "png"
    captured_at: str = ""
    day: str = ""
    year_month: str = ""
    category: str = "other"
    category_slug: str = "shot"
    slug: str = ""
    title: str = ""
    ai_desc: str = ""
    tags: list[str] = field(default_factory=list)
    note: str = ""
    favorite: int = 0
    confidence: float = 0.0
    reason: str = ""
    features: dict[str, Any] = field(default_factory=dict)
    width: int = 0
    height: int = 0
    bytes: int = 0
    sha1: str = ""
    source: str = ""
    app: str = ""
    window_title: str = ""
    monitor: str = ""
    mode: str = ""
    copied: int = 0
    created_at: str = ""
    updated_at: str = ""
    id: int = 0

    @property
    def size_human(self) -> str:
        return human_size(self.bytes)

    @property
    def time_human(self) -> str:
        try:
            return datetime.fromisoformat(self.captured_at).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return self.captured_at


def _row_to_record(row: sqlite3.Row) -> ShotRecord:
    d = dict(row)
    d["tags"] = json.loads(d.get("tags") or "[]")
    d["features"] = json.loads(d.get("features") or "{}")
    allowed = ShotRecord.__dataclass_fields__.keys()
    return ShotRecord(**{k: v for k, v in d.items() if k in allowed})


# --------------------------------------------------------------------------- #
# 主存储类
# --------------------------------------------------------------------------- #

class Vault:
    """一次截图从保存到入库的完整流程封装。"""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.root = cfg.vault_path
        self.meta_dir = self.root / META_DIR
        self.thumb_dir = self.meta_dir / "thumbs"
        self.namer = AutoNamer(cfg.naming)
        self._lock = threading.RLock()
        self._db: sqlite3.Connection | None = None
        # 缩略图目录的文件名缓存：find_thumb 的存在性检查用它，不再逐个 stat。
        # 实测 889 次逐个 stat 要 79–560 ms，一次 listdir 只要 0.9 ms。
        # 写入新缩略图时往集合里补（make_thumb），无需整体失效。
        self._thumb_names: set[str] | None = None
        self._ensure_dirs()

    def rebind(self, cfg: Config) -> None:
        """换一份配置（设置保存后由 app 调用）。

        只写 ``self.cfg = cfg`` 是不够的：``root`` / ``meta_dir`` /
        ``thumb_dir`` / ``namer`` 都是构造时从配置衍生的快照。用户在设置里
        改了**图库目录**之后不重新派生，vault 会继续往旧目录写文件、读旧库；
        改了**命名模板**，新截图还是用旧模板命名。图库目录没变时只换 cfg，
        不碰数据库连接（重开一次库是要花钱的）。
        """
        self.cfg = cfg
        self.namer = AutoNamer(cfg.naming)
        new_root = cfg.vault_path
        if new_root == self.root:
            return
        self.close()                          # 旧库的连接先关，别留着句柄
        self.root = new_root
        self.meta_dir = self.root / META_DIR
        self.thumb_dir = self.meta_dir / "thumbs"
        self._thumb_names = None              # 新目录的文件名缓存作废
        self._ensure_dirs()

    # ------------------------------------------------------------------ #
    # 目录与数据库
    # ------------------------------------------------------------------ #

    def _ensure_dirs(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.meta_dir.mkdir(parents=True, exist_ok=True)
        self.thumb_dir.mkdir(parents=True, exist_ok=True)
        gi = self.meta_dir / ".gitignore"
        if not gi.exists():
            gi.write_text("*\n", encoding="utf-8")

    @property
    def db(self) -> sqlite3.Connection:
        with self._lock:
            if self._db is None:
                self._db = sqlite3.connect(self.meta_dir / "index.db", check_same_thread=False)
                self._db.row_factory = sqlite3.Row
                self._db.executescript(_SCHEMA)
                self._migrate()
                self._db.commit()
            return self._db

    def _migrate(self) -> None:
        """给旧库补齐新增列（幂等，重复执行无害）。"""
        cols = {r["name"] for r in self._db.execute("PRAGMA table_info(shots)").fetchall()}
        if "ai_desc" not in cols:
            self._db.execute("ALTER TABLE shots ADD COLUMN ai_desc TEXT")

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    # ------------------------------------------------------------------ #
    # 保存一张截图
    # ------------------------------------------------------------------ #

    def save(
        self,
        image: Image.Image,
        prediction: Prediction | None = None,
        ctx: ShotContext | None = None,
        features: Features | None = None,
        when: datetime | None = None,
        tags: Sequence[str] | None = None,
        title_hint: str = "",
        mode: str = "region",
        subdir: str | None = None,
        copied: bool = False,
    ) -> ShotRecord:
        """编码 → 命名 → 落盘 → 入库，返回入库记录。"""
        when = when or datetime.now()
        ctx = ctx or ShotContext()
        if not ctx.source:
            ctx.source = mode

        img = image if image.mode in ("RGB", "RGBA") else image.convert("RGB")
        data, ext = self.encode(img)
        if features is None and self.cfg.classify.enabled:
            features = analyze(img, max_edge_rows=self.cfg.classify.probe_rows)
        if prediction is None:
            from .classifier import RuleClassifier

            prediction = RuleClassifier(
                confidence_floor=self.cfg.classify.confidence_floor,
                use_app_hints=self.cfg.classify.app_hints,
                user_rules=list(getattr(self.cfg, "category_rules", []) or []),
            ).predict(features, ctx)
        if tags:
            prediction.tags = normalize_tags(list(prediction.tags) + list(tags))

        # --- AI 视觉理解（可选）：一次请求同时拿「描述 + 分类」--------------
        # 启用且成功 → 描述作文件名主体、分类（若合法）覆盖视觉规则判定；
        # 失败       → no_slug=True（仅时间戳），分类保持规则结果；
        # 未启用     → 维持原有窗口标题命名，行为不变。
        ai_desc = ""
        ai_category = ""
        no_slug = False
        if self.cfg.ai.enabled:
            ai_result = self._ai_describe(img, data)
            desc = getattr(ai_result, "description", "") or ""
            if desc:
                ai_desc = desc
                title_hint = ai_desc
                if getattr(self.cfg.ai, "classify_with_ai", False):
                    ai_category = (getattr(ai_result, "category", "") or "").strip().lower()
            else:
                no_slug = True          # AI 失败/未配 Key → 回退仅时间戳

        if ai_category and ai_category in self.cfg.categories:
            # AI 看懂了画面内容，判定通常比纯图像特征准；置信度给个高水位，
            # 同时在 reason 里保留原规则结论——图库详情面板能看出这次是谁定的。
            prediction = replace(
                prediction,
                category=ai_category,
                confidence=max(float(prediction.confidence or 0.0), 0.85),
                reason=(f"AI 判定为「{self.cfg.category_label(ai_category)}」"
                        f"（视觉规则原判：{prediction.category}）"),
            )

        plan = self._plan_name(when, prediction, ctx, data, ext, title_hint, no_slug=no_slug)

        folder = self._target_folder(when, plan, subdir)
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / plan.filename
        if self.cfg.storage.dedupe or target.exists():
            target = unique_path(target)
        tmp = target.with_suffix(target.suffix + ".part")
        tmp.write_bytes(data)
        os.replace(tmp, target)

        rel = target.relative_to(self.root).as_posix()
        rec = self.record_for(target, plan, prediction, ctx, img, data, when, mode, copied,
                              ai_desc=ai_desc)
        self.upsert(rec)                       # 先入库：缩略图缓存键依赖 sha1
        self._append_jsonl(rec)
        self.make_thumb(target)
        return rec

    # ------------------------------------------------------------------ #

    def _ai_describe(self, image: Image.Image, data: bytes):
        """调用 AI 拿「描述（+ 可能的分类键）」；任何失败静默返回 ``None``。

        返回 :class:`snapvault.ai.AiResult`（鸭子类型即可，测试里用轻量对象桩）。
        调用方负责降级，这里绝不抛异常、绝不阻塞截图主流程。
        """
        # Ollama 这类本地端点不需要 Key，不能再用"没 Key 就返回"一刀切
        if not self.cfg.ai.api_key.strip() and provider_needs_key(self.cfg.ai.provider):
            return None
        try:
            from .ai import QwenVision, describe_cached

            client = QwenVision(
                api_key=self.cfg.ai.api_key,
                model=self.cfg.ai.model,
                endpoint=self.cfg.ai.endpoint,
                prompt=self.cfg.ai.prompt,
                timeout=self.cfg.ai.timeout,
                max_desc_len=self.cfg.ai.max_desc_len,
                requires_key=provider_needs_key(self.cfg.ai.provider),
            )
            return describe_cached(client, image, data)
        except Exception:
            # 网络/鉴权/解析失败都不影响截图主流程，降级即可
            return None

    def encode(self, image: Image.Image) -> tuple[bytes, str]:
        """按配置编码为 png / jpg / webp 字节流。"""
        fmt = (self.cfg.storage.image_format or "png").lower()
        buf = io.BytesIO()
        if fmt in ("jpg", "jpeg"):
            image.convert("RGB").save(buf, "JPEG", quality=int(self.cfg.storage.jpg_quality),
                                      optimize=True, progressive=True)
            return buf.getvalue(), "jpg"
        if fmt == "webp":
            image.convert("RGB").save(buf, "WEBP", quality=int(self.cfg.storage.jpg_quality),
                                      method=4)
            return buf.getvalue(), "webp"
        image.save(buf, "PNG", optimize=True)
        return buf.getvalue(), "png"

    def _plan_name(
        self,
        when: datetime,
        prediction: Prediction,
        ctx: ShotContext,
        data: bytes,
        ext: str,
        title_hint: str,
        no_slug: bool = False,
    ) -> NamePlan:
        seq = self._next_seq(when)
        return self.namer.build(
            when, prediction, ctx, image_bytes=data, ext=ext, seq=seq,
            title_override=title_hint, no_slug=no_slug,
        )

    def _next_seq(self, when: datetime) -> int:
        """同一秒内的第 N 张截图（1 → 空，2 → 02 ...）。"""
        try:
            row = self.db.execute(
                "SELECT COUNT(*) AS c FROM shots WHERE captured_at >= ? AND captured_at < ?",
                (when.strftime("%Y-%m-%dT%H:%M:%S"), when.strftime("%Y-%m-%dT%H:%M:%S.999999")),
            ).fetchone()
            return int(row["c"]) + 1 if row and row["c"] else 0
        except Exception:
            return 0

    @staticmethod
    def _dir_safe(name: str) -> str:
        """目录名安全化：剔除 Windows 非法字符与首尾空白/点。"""
        cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", str(name or "")).strip(" .")
        return cleaned or "未分类"

    def _target_folder(self, when: datetime, plan: NamePlan, subdir: str | None) -> Path:
        if subdir:
            return self.root / subdir
        pattern = self.cfg.storage.folder_pattern or ""
        if self.cfg.storage.flat or not pattern:
            return self.root
        rel = (
            pattern.replace("{yyyy}", f"{when:%Y}")
            .replace("{yy}", f"{when:%y}")
            .replace("{MM}", f"{when:%m}")
            .replace("{dd}", f"{when:%d}")
            .replace("{category}", plan.category_slug)
            .replace("{label}", plan.category_slug)
            # {cn} = 分类的中文名（代码 / 文档 / 网页 …）。
            # 用 {cn} 当模板，图就直接按中文分类落到文件夹里：
            #   D:\Pictures\网页\20261013_183000_web_xxx.png
            .replace("{cn}", self._dir_safe(self.cfg.category_label(plan.category)))
        )
        rel = rel.strip("/\\")
        if not rel:
            return self.root
        # 防呆：模板里出现 .. 时不许跳出示意图库根目录
        parts = [p for p in re.split(r"[\\/]+", rel) if p and p != "."]
        if any(p == ".." for p in parts):
            return self.root
        return self.root.joinpath(*parts)

    def record_for(
        self,
        path: Path,
        plan: NamePlan,
        prediction: Prediction,
        ctx: ShotContext,
        image: Image.Image,
        data: bytes,
        when: datetime,
        mode: str,
        copied: bool,
        ai_desc: str = "",
    ) -> ShotRecord:
        import hashlib

        now = datetime.now().isoformat(timespec="seconds")
        feats = {}
        if prediction.scores:
            feats["scores"] = prediction.scores
        if prediction.category:
            feats["predicted"] = prediction.category
        return ShotRecord(
            rel_path=path.relative_to(self.root).as_posix(),
            filename=path.name,
            name_stem=path.stem,
            ext=path.suffix.lstrip(".").lower(),
            captured_at=when.isoformat(timespec="seconds"),
            day=when.strftime("%Y-%m-%d"),
            year_month=when.strftime("%Y-%m"),
            category=prediction.category,
            category_slug=plan.category_slug,
            slug=plan.slug,
            title=ctx.title or plan.slug,
            ai_desc=ai_desc,
            tags=normalize_tags(prediction.tags),
            note="",
            favorite=0,
            confidence=prediction.confidence,
            reason=prediction.reason,
            features=feats,
            width=image.width,
            height=image.height,
            bytes=len(data),
            sha1=hashlib.sha1(data).hexdigest(),
            source=ctx.source or mode,
            app=ctx.exe,
            window_title=ctx.title,
            monitor=ctx.monitor,
            mode=mode,
            copied=1 if copied else 0,
            created_at=now,
            updated_at=now,
        )

    # ------------------------------------------------------------------ #
    # 入库 / 更新 / 查询
    # ------------------------------------------------------------------ #

    def upsert(self, rec: ShotRecord) -> ShotRecord:
        payload = asdict(rec)
        payload.pop("id", None)
        payload["tags"] = json.dumps(rec.tags, ensure_ascii=False)
        payload["features"] = json.dumps(rec.features, ensure_ascii=False)
        cols = list(payload.keys())
        placeholders = ",".join("?" for _ in cols)
        with self._lock:
            cur = self.db.execute(
                f"INSERT INTO shots ({','.join(cols)}) VALUES ({placeholders}) "
                f"ON CONFLICT(rel_path) DO UPDATE SET "
                + ",".join(f"{c}=excluded.{c}" for c in cols if c not in ("rel_path", "created_at")),
                [payload[c] for c in cols],
            )
            self.db.commit()
            if not rec.id:
                rec.id = int(cur.lastrowid or 0)
                if not rec.id:
                    row = self.db.execute(
                        "SELECT id FROM shots WHERE rel_path=?", (rec.rel_path,)
                    ).fetchone()
                    rec.id = int(row["id"]) if row else 0
        self._recount_tags()
        return rec

    def update(self, shot_id: int, **changes: Any) -> ShotRecord | None:
        allowed = {"tags", "note", "favorite", "category", "category_slug", "title", "rel_path",
                   "filename", "name_stem"}
        sets, vals = [], []
        for k, v in changes.items():
            if k not in allowed:
                continue
            if k == "tags":
                v = json.dumps(normalize_tags(v), ensure_ascii=False)
            sets.append(f"{k}=?")
            vals.append(v)
        if not sets:
            return self.get(shot_id)
        sets.append("updated_at=?")
        vals.append(datetime.now().isoformat(timespec="seconds"))
        vals.append(shot_id)
        with self._lock:
            self.db.execute(f"UPDATE shots SET {','.join(sets)} WHERE id=?", vals)
            self.db.commit()
        self._recount_tags()
        return self.get(shot_id)

    def get(self, shot_id: int) -> ShotRecord | None:
        row = self.db.execute("SELECT * FROM shots WHERE id=?", (shot_id,)).fetchone()
        return _row_to_record(row) if row else None

    # ------------------------------------------------------------------ #
    # 一键重归类
    # ------------------------------------------------------------------ #

    def reclassify_all(self, progress: Any = None,
                       only_category: str = "") -> tuple[int, int]:
        """按当前分类设置重新判定所有已有截图的分类，返回 ``(处理数, 变更数)``。

        **只改库里的 category 字段，不移动、不重命名任何文件** —— 这是用户
        明确的取舍：分类规则改了，历史图片安安稳稳待在原地，只是库里的归类
        被刷新。（真要连文件一起搬，风险和耗时都上一个量级，另做。）

        图片打不开就跳过，单张失败不影响整体。这是个重操作（890 张要逐张
        解码 + 提特征，实测几十秒），调用方应放工作线程里跑，并通过
        ``progress(done, total)`` 报进度。
        """
        from .classifier import RuleClassifier, ShotContext

        clf = RuleClassifier(
            confidence_floor=self.cfg.classify.confidence_floor,
            use_app_hints=self.cfg.classify.app_hints,
            user_rules=list(getattr(self.cfg, "category_rules", []) or []),
        )
        records = self.search("", limit=1_000_000)
        if only_category:
            records = [r for r in records if r.category == only_category]
        total = len(records)
        done = changed = 0
        for i, rec in enumerate(records):
            try:
                path = self.abs_path(rec)
                if path is not None and path.exists():
                    with Image.open(path) as im:
                        feats = analyze(im.convert("RGB"),
                                        max_edge_rows=self.cfg.classify.probe_rows)
                    ctx = ShotContext(exe=rec.app or "", title=rec.window_title or "",
                                      source=(rec.mode or rec.source or "region"))
                    pred = clf.predict(feats, ctx)
                    if pred.category and pred.category != rec.category:
                        self.update(rec.id, category=pred.category)
                        changed += 1
            except Exception:
                pass                                  # 单张坏图不阻断整体
            done += 1
            if progress is not None and (i % 10 == 0 or i == total - 1):
                try:
                    progress(done, total)
                except Exception:
                    pass
        return done, changed

    def get_by_path(self, rel_path: str) -> ShotRecord | None:
        row = self.db.execute("SELECT * FROM shots WHERE rel_path=?", (rel_path,)).fetchone()
        return _row_to_record(row) if row else None

    def search(
        self,
        text: str = "",
        category: str = "",
        tags: Sequence[str] = (),
        day_from: str = "",
        day_to: str = "",
        favorite: bool | None = None,
        limit: int = 500,
        offset: int = 0,
        order: str = "captured_at DESC",
    ) -> list[ShotRecord]:
        """查询索引。``text`` 支持 ``cat:code tag:dark day:2026-05 关键词`` 语法。"""
        where: list[str] = []
        params: list[Any] = []
        terms, parsed = self._parse_query(text)
        if parsed.get("category"):
            category = parsed["category"]
        if parsed.get("tags"):
            tags = tuple(list(tags) + list(parsed["tags"]))
        if parsed.get("day"):
            where.append("day LIKE ?")
            params.append(parsed["day"] + "%")
        if category and category != "all":
            where.append("category=?")
            params.append(category)
        for t in tags:
            where.append("tags LIKE ?")
            params.append(f'%"{t}"%')
        if day_from:
            where.append("day >= ?")
            params.append(day_from)
        if day_to:
            where.append("day <= ?")
            params.append(day_to)
        if favorite is not None:
            where.append("favorite=?")
            params.append(1 if favorite else 0)
        for term in terms:
            where.append(
                "(filename LIKE ? OR slug LIKE ? OR title LIKE ? OR note LIKE ? "
                "OR tags LIKE ? OR ai_desc LIKE ?)"
            )
            like = f"%{term}%"
            params.extend([like] * 6)
        sql = "SELECT * FROM shots"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += f" ORDER BY {order} LIMIT ? OFFSET ?"
        params.extend([max(1, limit), max(0, offset)])
        rows = self.db.execute(sql, params).fetchall()
        return [_row_to_record(r) for r in rows]

    @staticmethod
    def _parse_query(text: str) -> tuple[list[str], dict[str, Any]]:
        parsed: dict[str, Any] = {}
        terms: list[str] = []
        for token in (text or "").split():
            low = token.lower()
            if low.startswith("cat:") or low.startswith("分类:"):
                parsed["category"] = token.split(":", 1)[1]
            elif low.startswith("tag:") or low.startswith("标签:"):
                parsed.setdefault("tags", []).append(token.split(":", 1)[1])
            elif low.startswith("day:") or low.startswith("日:"):
                parsed["day"] = token.split(":", 1)[1]
            else:
                terms.append(token)
        return terms, parsed

    # ------------------------------------------------------------------ #
    # 增量收窄：打字时不再每个键都全量查库
    # ------------------------------------------------------------------ #

    @staticmethod
    def _text_fields(rec: ShotRecord) -> tuple[str, ...]:
        """:meth:`search` 的 SQL 会 LIKE 的那六个字段。

        ``tags`` 必须用库里的 JSON 原文（``["a", "b"]``）而不是列表 ——
        SQL LIKE 的就是这一列，拿列表重新拼会得到不同的匹配结果。
        """
        return (rec.filename, rec.slug, rec.title, rec.note,
                json.dumps(rec.tags, ensure_ascii=False), rec.ai_desc)

    @classmethod
    def record_matches(cls, rec: ShotRecord, terms: Sequence[str]) -> bool:
        """与 :meth:`search` 的文本 WHERE 等价的内存判定。

        语义和 SQL 完全对齐：每个词都要能在六个字段**之一**里作为子串找到
        （词与词之间是 AND）。大小写不敏感 —— SQLite 的 LIKE 对 ASCII
        大小写不敏感，这里用 ``lower()`` 对齐。
        """
        if not terms:
            return True
        fields = cls._text_fields(rec)
        for term in terms:
            needle = term.lower()
            if not any(needle in f.lower() for f in fields):
                return False
        return True

    @classmethod
    def can_narrow(cls, old_text: str, new_text: str) -> bool:
        """``new_text`` 的结果集是否一定是 ``old_text`` 结果集的子集。

        判据（分词后逐词比，前面的词必须一字不差）：

        * 最后一个词**只是变长**（``co`` → ``code``）—— SQL 里那个 LIKE
          模式只会更严格（``%co%`` → ``%code%``）；
        * 或者末尾**追加了新词**（``foo`` → ``foo bar``）—— 每个词都是 AND，
          多加一个只会更严；
        * 旧查询一个自由词都没有时，新查询的任何条件都是在全表上收窄。

        反例（一律 False，回退全量查询）：删字符（结果会变多）、
        ``cat:code`` → ``cat:codex``（限定符本身换了，不是变严格）、
        改动中间某个词、两次查询完全相同（那是重新查库，不是收窄）。
        宁可多查一次库，也不能给出比真实结果少的列表。
        """
        old_t, old_p = cls._parse_query(old_text)
        new_t, new_p = cls._parse_query(new_text)
        if old_p != new_p:
            return False                     # cat: / tag: / day: 限定符动过
        n = len(old_t)
        if n == 0:
            return True                      # 旧查询无自由词，怎么加都是收窄
        if new_t[:n - 1] != old_t[:n - 1]:
            return False                     # 除最后一个词外的词被动过
        if len(new_t) == n:
            return new_t[-1].startswith(old_t[-1])
        if len(new_t) > n:
            return new_t[:n] == old_t        # 旧词全部保留，后面追加新词
        return False                         # 词变少了

    def narrow(self, old_text: str, old_records: Sequence[ShotRecord],
               new_text: str, limit: int = 1000) -> list[ShotRecord] | None:
        """能收窄就返回过滤后的新结果，不能就返回 ``None``（调用方去查库）。

        ``old_records`` 必须是**没被 limit 截断**的完整旧结果，否则收窄出来
        的列表会缺行 —— 这个前提由调用方检查（见 GalleryWindow.reload）。
        """
        if not self.can_narrow(old_text, new_text):
            return None
        terms, _ = self._parse_query(new_text)
        return [r for r in old_records if self.record_matches(r, terms)][:limit]

    def count(self, **kwargs: Any) -> int:
        day_from = kwargs.get("day_from", "")
        category = kwargs.get("category", "")
        where, params = [], []
        if day_from:
            where.append("day >= ?")
            params.append(day_from)
        if category and category != "all":
            where.append("category=?")
            params.append(category)
        sql = "SELECT COUNT(*) AS c FROM shots" + (
            " WHERE " + " AND ".join(where) if where else ""
        )
        return int(self.db.execute(sql, params).fetchone()["c"])

    def category_counts(self) -> dict[str, int]:
        rows = self.db.execute(
            "SELECT category, COUNT(*) AS c FROM shots GROUP BY category ORDER BY c DESC"
        ).fetchall()
        return {r["category"]: int(r["c"]) for r in rows}

    def tag_counts(self, limit: int = 40) -> list[tuple[str, int]]:
        rows = self.db.execute(
            "SELECT tag, uses FROM tags ORDER BY uses DESC, tag ASC LIMIT ?", (limit,)
        ).fetchall()
        return [(r["tag"], int(r["uses"])) for r in rows]

    def month_counts(self) -> list[tuple[str, int]]:
        rows = self.db.execute(
            "SELECT year_month, COUNT(*) AS c FROM shots GROUP BY year_month ORDER BY year_month DESC"
        ).fetchall()
        return [(r["year_month"], int(r["c"])) for r in rows]

    def _recount_tags(self) -> None:
        with self._lock:
            self.db.execute("DELETE FROM tags")
            self.db.execute(
                """
                INSERT INTO tags(tag, uses, last_at)
                SELECT j.value, COUNT(*), MAX(s.captured_at)
                FROM shots s, json_each(s.tags) j
                GROUP BY j.value
                """
            )
            self.db.commit()

    def delete(self, shot_id: int, remove_file: bool = True) -> bool:
        rec = self.get(shot_id)
        if not rec:
            return False
        if remove_file:
            try:
                (self.root / rec.rel_path).unlink(missing_ok=True)
            except Exception:
                pass
        with self._lock:
            self.db.execute("DELETE FROM shots WHERE id=?", (shot_id,))
            self.db.commit()
        self._recount_tags()
        return True

    def move(self, shot_id: int, new_category: str) -> ShotRecord | None:
        """改分类并同步重命名文件（保持文件名即元数据的一致性）。"""
        rec = self.get(shot_id)
        if not rec or rec.category == new_category:
            return rec
        from .classifier import CATEGORY_SLUG

        new_slug = CATEGORY_SLUG.get(new_category, new_category)
        old_path = self.root / rec.rel_path
        new_name = rec.filename.replace(f"_{rec.category_slug}_", f"_{new_slug}_")
        if new_name == rec.filename:
            new_name = f"{rec.name_stem}-{new_slug}{'.' + rec.ext}"
        new_path = unique_path(old_path.with_name(new_name))
        try:
            if old_path.exists():
                shutil.move(str(old_path), str(new_path))
        except Exception:
            new_path = old_path
        new_rel = new_path.relative_to(self.root).as_posix()
        return self.update(
            shot_id,
            category=new_category,
            category_slug=new_slug,
            rel_path=new_rel,
            filename=new_path.name,
            name_stem=new_path.stem,
        )

    # ------------------------------------------------------------------ #
    # 缩略图 / 重新导入
    # ------------------------------------------------------------------ #

    def thumb_path_for(self, rec: ShotRecord) -> Path:
        """缩略图主路径（按内容哈希命名）。"""
        key = (rec.sha1 or rec.rel_path.replace("/", "_"))[:32]
        return self.thumb_dir / f"{key}.jpg"

    def _thumb_exists(self, name: str) -> bool:
        """缩略图文件是否已存在。查内存集合，不碰磁盘。

        目录内容可能被外部改动（用户手动清理缓存等），所以集合只缓存
        **一轮会话**：每次 Vault 构造时是空的，第一次访问时 listdir 一次。
        """
        if self._thumb_names is None:
            try:
                self._thumb_names = set(os.listdir(self.thumb_dir))
            except OSError:
                self._thumb_names = set()
        return name in self._thumb_names

    def find_thumb(self, rec: ShotRecord) -> Path | None:
        """查找已存在的缩略图（兼容早期按文件名命名的缓存）。

        改版前是逐个 ``Path.exists()`` —— 889 张图就是 889 次 ``nt.stat``
        系统调用，实测 79–560 ms；现在查一次 ``listdir`` 建的内存集合，
        整体 0.9 ms 量级。
        """
        for name in (
            f"{(rec.sha1 or rec.rel_path.replace('/', '_'))[:32]}.jpg",
            f"{(rec.sha1 or '')[:24]}.jpg",
            f"{rec.name_stem[:32]}.jpg",
        ):
            if name and name != ".jpg" and self._thumb_exists(name):
                return self.thumb_dir / name
        return None

    def make_thumb(self, path: Path, size: int | None = None) -> Path | None:
        """生成缩略图；先尝试用库里已有的 sha1 作为缓存键（更快命中）。"""
        try:
            size = size or self.cfg.storage.thumb_size
            rec = self.get_by_path(path.relative_to(self.root).as_posix())
            key = (rec.sha1 if rec and rec.sha1 else None) or path.stem
            out = self.thumb_dir / f"{key[:32]}.jpg"
            if out.exists() and out.stat().st_mtime >= path.stat().st_mtime:
                self._thumb_note(out.name)
                return out
            from PIL import ImageOps

            with Image.open(path) as im:
                im = ImageOps.exif_transpose(im).convert("RGB")
                im.thumbnail((size, size), Image.LANCZOS)
                im.save(out, "JPEG", quality=82)
            self._thumb_note(out.name)
            if rec is None:
                # 索引尚未写入：同时补一份基于文件名的缓存，供下一次命中
                alt = self.thumb_dir / f"{path.stem[:32]}.jpg"
                if not alt.exists():
                    shutil.copy2(out, alt)
                self._thumb_note(alt.name)
            return out
        except Exception:
            return None

    def _thumb_note(self, name: str) -> None:
        """把新写入的缩略图名补进缓存集合（集合未初始化就不用动）。"""
        if self._thumb_names is not None:
            self._thumb_names.add(name)

    def _library_dirs(self) -> list[Path] | None:
        """重建索引时该扫哪些目录；返回 ``None`` 表示「只看图库根第一层」。

        图库根可能被设成 ``D:\\Pictures`` 这种"什么都有"的大目录。直接
        ``rglob("*")`` 会把用户自己的素材、相册、作业一起算进来 —— 实测该目录下
        1842 个文件里有 829 个文件名恰好长得像归档，其中 700 多个根本不是 SnapVault
        产的。把它们读进内存算 sha1、再逐个生成缩略图，既污染图库，又让程序启动
        慢了几十秒（托盘起来后要等 40 多秒才开始接管，看起来就像"没生效"）。

        所以只扫自家会写的那几个位置：

        * 分类标签目录（``代码 / 文档 / 网页 / …``）—— 模板含 ``{cn}`` 时；
        * 二到四位的数字目录（``2025 / 202510``）—— 模板含 ``{yyyy}`` / ``{yy}`` 时。

        平铺模式（模板为空或 ``flat``）返回 ``None``，只看图库根第一层的文件。
        """
        import re

        pattern = str(getattr(self.cfg.storage, "folder_pattern", "") or "")
        if not pattern or bool(getattr(self.cfg.storage, "flat", False)):
            return None
        labels = {self.cfg.category_label(c) for c in self.cfg.categories}
        wants_year = "{yyyy}" in pattern or "{yy}" in pattern
        out: list[Path] = []
        try:
            entries = list(self.root.iterdir())
        except OSError:
            return None
        for entry in entries:
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            if entry.name in labels:
                out.append(entry)
            elif wants_year and re.fullmatch(r"\d{2,4}", entry.name):
                out.append(entry)
        return out

    def rebuild_index(self, *, only_named: bool | None = None) -> int:
        """扫描磁盘重建索引（文件名即元数据，可恢复分类/时间/标题）。

        ``only_named`` 默认取 ``storage.rebuild_only_named``。它设两道闸门，
        保证图库根被设成 ``D:\\Pictures`` 这类大目录时不会把用户自己的东西卷进来：

        1. **只扫自家目录**（见 :meth:`_library_dirs`），不再递归整个图库根；
        2. **文件名必须是自家命名** ``YYYYMMDD_HHMMSS_<分类slug>_<描述>`` ——
           分类段得是 ``code`` / ``webpage`` 这些已知 slug。用户自己的
           ``20241014_160929_数据_KMeans 手肘法 Elbow Curve 图.png`` 虽然也长得像，
           但分类段是中文「数据」、不在 slug 表里，于是被挡在门外。
        """
        import hashlib
        import re

        from .classifier import CATEGORY_SLUG

        if only_named is None:
            only_named = bool(getattr(self.cfg.storage, "rebuild_only_named", True))

        slug2cat = {v: k for k, v in CATEGORY_SLUG.items()}
        exts = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
        pattern = re.compile(r"^(?P<d>\d{8})_(?P<t>\d{6})_(?P<cat>[a-z0-9]+)_?(?P<slug>.*)$")

        # --- 收集候选：只在自家目录里找，绝不递归整个图库根 ---
        dirs = self._library_dirs()
        targets: list[Path] = []
        try:
            if dirs is None:
                targets = [p for p in self.root.iterdir() if p.is_file()]
            else:
                for d in dirs:
                    targets.extend(p for p in d.rglob("*") if p.is_file())
        except OSError:
            targets = []

        n = 0
        for p in sorted(targets):
            if p.suffix.lower() not in exts:
                continue
            if META_DIR in p.parts:
                continue
            if only_named:
                m0 = re.match(r"^\d{8}_\d{6}_([A-Za-z0-9]+)", p.stem)
                if not m0 or m0.group(1).lower() not in slug2cat:
                    continue               # 分类段不是自家 slug，别碰
            rel = p.relative_to(self.root).as_posix()
            if self.get_by_path(rel):
                continue
            data = p.read_bytes()
            m = pattern.match(p.stem)
            cat_slug = m.group("cat") if m else "shot"
            category = slug2cat.get(cat_slug, "other")
            when = datetime.fromtimestamp(p.stat().st_mtime)
            if m:
                try:
                    when = datetime.strptime(m.group("d") + m.group("t"), "%Y%m%d%H%M%S")
                except ValueError:
                    pass
            with Image.open(p) as im:
                w, h = im.size
            rec = ShotRecord(
                rel_path=rel, filename=p.name, name_stem=p.stem,
                ext=p.suffix.lstrip(".").lower(),
                captured_at=when.isoformat(timespec="seconds"),
                day=when.strftime("%Y-%m-%d"), year_month=when.strftime("%Y-%m"),
                category=category, category_slug=cat_slug,
                slug=(m.group("slug") if m else p.stem), title=(m.group("slug") if m else ""),
                tags=[category], bytes=len(data),
                sha1=hashlib.sha1(data).hexdigest(), width=w, height=h,
                source="rebuild", mode="rebuild",
                created_at=datetime.now().isoformat(timespec="seconds"),
                updated_at=datetime.now().isoformat(timespec="seconds"),
            )
            self.upsert(rec)
            self.make_thumb(p)
            n += 1
        with self._lock:
            self.db.execute(
                "INSERT INTO meta(key,value) VALUES('last_rebuild',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (datetime.now().isoformat(timespec="seconds"),),
            )
            self.db.commit()
        return n

    def _append_jsonl(self, rec: ShotRecord) -> None:
        if not self.cfg.storage.keep_jsonl:
            return
        try:
            line = json.dumps(asdict(rec), ensure_ascii=False)
            with (self.meta_dir / "index.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # 便捷
    # ------------------------------------------------------------------ #

    def abs_path(self, rec: ShotRecord) -> Path:
        return self.root / rec.rel_path

    def stats(self) -> dict[str, Any]:
        total = self.count()
        size = self.db.execute("SELECT COALESCE(SUM(bytes),0) AS s FROM shots").fetchone()["s"]
        return {
            "root": str(self.root),
            "total": total,
            "bytes": int(size or 0),
            "categories": self.category_counts(),
            "months": self.month_counts()[:12],
            "top_tags": self.tag_counts(12),
        }

    def export_manifest(self, path: Path | None = None) -> Path:
        """导出 CSV 清单，便于用 Excel / pandas 做二次整理。"""
        import csv

        path = path or (self.root / f"manifest-{datetime.now():%Y%m%d}.csv")
        rows = self.db.execute("SELECT * FROM shots ORDER BY captured_at DESC").fetchall()
        with path.open("w", encoding="utf-8-sig", newline="") as fh:
            if rows:
                writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
                writer.writeheader()
                for r in rows:
                    writer.writerow(dict(r))
        return path
