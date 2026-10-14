"""自动命名规则引擎.

命名目标
--------
1. **人眼可读**：能在资源管理器里一眼看出"什么时候、什么内容"。
2. **机器可检索**：时间戳前缀保证目录内天然时序；类别词保证按前缀搜索；
   slug 压缩过的标题片段让全文搜索能命中项目名 / 页面名。
3. **跨平台安全**：过滤 Windows 保留字符与保留设备名，长度受控，绝不重名。

默认模板 ``{date}_{time}_{category}_{slug}`` 生成形如::

    20260518_142233_code_snapvault-classifier_a1b2c3.png
    └─日期──┘└─时间─┘└分类┘└───内容 slug────┘└短哈希(可选)┘

可用占位符：``{date} {time} {stamp} {yyyymmdd} {hhmmss} {category} {label}
{slug} {title} {tags} {hash} {seq} {app} {monitor}``
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

from .classifier import CATEGORY_SLUG, Prediction, ShotContext
from .config import NamingConfig

# Windows 非法字符 / 保留名
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}
_SPACE_RUN = re.compile(r"[\s_]+")
_DASH_RUN = re.compile(r"-{2,}")
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]")
# 明显无信息量的标题片段
_NOISE = re.compile(
    r"^(untitled|无标题|new tab|新标签页|screenshot|截图|screen shot|"
    r"administrator|admin|desktop|program manager|programmanager|"
    r"microsoft\u200b?windows|windows)+$",
    re.I,
)


# --------------------------------------------------------------------------- #
# slug 工具
# --------------------------------------------------------------------------- #

def slugify(
    text: str,
    max_len: int = 24,
    keep_cjk: bool = True,
    lowercase: bool = True,
    separator: str = "-",
) -> str:
    """把任意标题压缩为可读、安全的文件名片段。

    >>> slugify("main.py - SnapVault — Visual Studio Code")
    'main-py-snapvault'
    >>> slugify("百度一下，你就知道", max_len=8)
    '百度一下-你就知道'
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = _ILLEGAL.sub(" ", text)
    # 去掉常见分隔噪音
    text = re.sub(r"[\[\](){}<>«»“”\"'`~!@#$%^&*+=,;:。，、！？…]+", " ", text)
    # 扩展名切分：`.py` 这类后缀单独成词。
    # 注意这里用 (?![A-Za-z0-9]) 而不是 \b——中文也算 \w，`ai.py代码` 用 \b 判定时
    # "y" 与 "代" 之间不算词边界，规则不生效，`.` 会在下面被当噪音删掉，
    # 结果 "ai.py" 被粘成 "aipy"。改成"后面不是字母数字即可"就正常切成 ai / py。
    text = re.sub(r"\.(py|ts|tsx|js|jsx|java|go|rs|cpp|cs|md|json|ya?ml|html|css|txt|log|ipynb)(?![A-Za-z0-9])",
                  lambda m: " " + m.group(1) + " ", text, flags=re.I)
    text = text.replace("/", " ").replace("\\", " ")

    tokens: list[str] = []
    for raw in _SPACE_RUN.split(text):
        raw = raw.strip("-_.")
        if not raw or _NOISE.match(raw):
            continue
        if _CJK.search(raw):
            if not keep_cjk:
                continue
            # 中文按标点切分后整段保留（不做拼音转换）
            raw = re.sub(r"[^\w\u3400-\u9fff]+", "", raw)
            if raw:
                tokens.append(raw)
        else:
            raw = re.sub(r"[^0-9A-Za-z]+", "-", raw)
            raw = _DASH_RUN.sub("-", raw).strip("-")
            if raw:
                tokens.append(raw)
        if len("-".join(tokens)) >= max_len:
            break

    slug = separator.join(tokens)
    if lowercase:
        slug = slug.lower() if not _CJK.search(slug) else slug
    slug = _DASH_RUN.sub(separator, re.sub(rf"{re.escape(separator)}{{2,}}", separator, slug))
    slug = slug.strip(separator + "-_.")
    if len(slug) > max_len:
        slug = slug[:max_len].rstrip(separator + "-_.")
    return slug


def content_hash(data: bytes, length: int = 6) -> str:
    """内容短哈希（sha1 前 N 位），用于去重与文件名唯一性。"""
    return hashlib.sha1(data).hexdigest()[:length]


def normalize_tags(tags: Iterable[str], limit: int = 12) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for t in tags:
        t = unicodedata.normalize("NFKC", str(t)).strip().strip("#")
        t = _ILLEGAL.sub("", t)
        if not t or len(t) > 24:
            continue
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
        if len(out) >= limit:
            break
    return out


def human_size(num_bytes: int | float) -> str:
    """把字节数格式化成 ``1.2 MB`` 这样的短串。"""
    n = float(num_bytes or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def safe_filename(name: str, max_len: int = 120) -> str:
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    stem = _ILLEGAL.sub("", stem).strip(" .")
    if stem.lower() in _RESERVED:
        stem = f"_{stem}"
    stem = stem[:max_len].rstrip(" .")
    return f"{stem}.{ext}" if ext else stem


# --------------------------------------------------------------------------- #
# 命名器
# --------------------------------------------------------------------------- #

@dataclass
class NamePlan:
    """命名结果：文件名各组成部分，便于 UI 展示与二次编辑。"""

    filename: str
    stem: str
    ext: str
    date: str
    time: str
    category: str
    category_slug: str
    slug: str
    tags: list[str] = field(default_factory=list)
    hash: str = ""
    seq: int = 0
    template: str = ""

    @property
    def parts(self) -> dict[str, Any]:
        return {
            "filename": self.filename, "stem": self.stem, "ext": self.ext,
            "date": self.date, "time": self.time, "category": self.category,
            "category_slug": self.category_slug, "slug": self.slug,
            "tags": self.tags, "hash": self.hash, "seq": self.seq,
        }


class AutoNamer:
    """按模板生成截图文件名。"""

    def __init__(self, cfg: NamingConfig | None = None) -> None:
        self.cfg = cfg or NamingConfig()

    # ------------------------------------------------------------------ #

    def build(
        self,
        when: datetime,
        prediction: Prediction | None = None,
        ctx: ShotContext | None = None,
        image_bytes: bytes = b"",
        ext: str = "png",
        seq: int = 0,
        title_override: str = "",
        no_slug: bool = False,
    ) -> NamePlan:
        cfg = self.cfg
        prediction = prediction or Prediction()
        ctx = ctx or ShotContext()

        category = prediction.category or "other"
        cat_slug = CATEGORY_SLUG.get(category, slugify(category, 12) or "shot")

        if no_slug:
            slug = ""
        else:
            title = title_override or self._pick_title(ctx)
            slug = slugify(
                title,
                max_len=cfg.slug_max_len,
                keep_cjk=(cfg.slug_lang in ("auto", "cn")),
                lowercase=cfg.lowercase_slug,
                separator="-",
            )
            if not slug:
                slug = slugify(cat_slug, 16) or "shot"
            if cfg.annotate_project and not title_override:
                proj = ctx.project_slug_hint()
                proj_slug = slugify(proj, max(8, cfg.slug_max_len // 2),
                                    keep_cjk=(cfg.slug_lang in ("auto", "cn")))
                if proj_slug and proj_slug not in slug:
                    slug = f"{slug}-{proj_slug}"[: cfg.slug_max_len + 8].strip("-")

        tags = normalize_tags(prediction.tags, 8)
        digest = content_hash(image_bytes, cfg.hash_len) if image_bytes else ""

        values = {
            "date": f"{when:%Y%m%d}",
            "time": f"{when:%H%M%S}",
            "stamp": f"{when:%Y%m%d_%H%M%S}",
            "yyyymmdd": f"{when:%Y%m%d}",
            "hhmmss": f"{when:%H%M%S}",
            "category": cat_slug,
            "label": slugify(category, 12) or "shot",
            "slug": slug,
            "title": slug,
            "tags": "-".join(tags[:3]),
            "hash": digest,
            "seq": f"{seq:02d}" if seq else "",
            "app": slugify(ctx.exe.rsplit(".", 1)[0], 12) if ctx.exe else "",
            "monitor": slugify(ctx.monitor, 10),
        }

        template = cfg.template or "{date}_{time}_{category}_{slug}"
        stem = self._render(template, values, cfg.separator)
        if cfg.hash_prefix and digest:
            stem = f"{digest}{cfg.separator}{stem}"
        if cfg.include_tags and tags:
            stem = f"{stem}{cfg.separator}{'-'.join(tags[:2])}"
        stem = self._tidy(stem, cfg.separator)
        ext = (ext or "png").lstrip(".").lower()
        filename = safe_filename(f"{stem}.{ext}" if not stem.lower().endswith(f".{ext}") else stem)
        return NamePlan(
            filename=filename,
            stem=filename.rsplit(".", 1)[0],
            ext=ext,
            date=values["date"],
            time=values["time"],
            category=category,
            category_slug=cat_slug,
            slug=slug,
            tags=tags,
            hash=digest,
            seq=seq,
            template=template,
        )

    # ------------------------------------------------------------------ #

    def _pick_title(self, ctx: ShotContext) -> str:
        """优先用项目名，其次用窗口标题主段，最后退回 URL。"""
        proj = ctx.project_slug_hint()
        if proj and not _NOISE.match(proj) and len(proj) > 2:
            return proj
        title = ctx.title or ""
        title = re.split(r"\s+[-–—|]\s+", title)[0].strip() or title
        if title and not _NOISE.match(title):
            return title
        if ctx.url:
            from urllib.parse import urlparse

            p = urlparse(ctx.url)
            host = (p.netloc or "").replace("www.", "")
            path = p.path.strip("/").split("/")[-1]
            return f"{host}-{path}".strip("-")
        return ctx.exe.rsplit(".", 1)[0] if ctx.exe else ""

    def _render(self, template: str, values: dict[str, Any], separator: str) -> str:
        out = template
        for key, val in values.items():
            out = out.replace("{" + key + "}", str(val))
        # 清理未识别占位符与多余分隔符
        out = re.sub(r"\{[a-z_]+\}", "", out)
        out = re.sub(rf"{re.escape(separator)}+", separator, out)
        return out.strip(separator + " .-_")

    def _tidy(self, stem: str, separator: str) -> str:
        stem = _ILLEGAL.sub("", stem)
        stem = re.sub(r"\s+", separator, stem)
        stem = re.sub(rf"{re.escape(separator)}{{2,}}", separator, stem)
        stem = re.sub(r"-{2,}", "-", stem)
        return stem.strip(separator + " .-_")


def unique_path(path, suffix_fmt: str = "{stem}-{n}{ext}"):
    """若目标已存在则追加 ``-2/-3``，返回可用的 :class:`pathlib.Path`。"""
    from pathlib import Path

    path = Path(path)
    if not path.exists():
        return path
    for n in range(2, 1000):
        cand = path.with_name(suffix_fmt.format(stem=path.stem, n=n, ext=path.suffix))
        if not cand.exists():
            return cand
    raise FileExistsError(f"cannot find free name for {path}")
