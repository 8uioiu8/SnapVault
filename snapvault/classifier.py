"""内容分类器：规则打分 + 应用上下文加权（可插拔 ML 后端）.

分类思路
--------
不依赖任何在线模型，用 :mod:`snapvault.analyzer` 的统计特征对每个候选类别打分，
再叠加"进程名 / 窗口标题"这一强上下文信号（例如 ``Code.exe`` 直接加 ``code`` 分）。
最终分数最高的类别作为结果，分数过低则归入 ``other``。

可扩展点：实现 ``Classifier`` 协议的 ``predict(features, context) -> (category, confidence)``，
在 :data:`BACKENDS` 注册即可（例如接入 ONNX 小模型做截图内容识别）。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .analyzer import Features

# --------------------------------------------------------------------------- #
# 应用上下文提示表：可执行文件名 -> 类别
# --------------------------------------------------------------------------- #

EXE_HINTS: dict[str, str] = {
    "code.exe": "code",
    "code - insiders.exe": "code",
    "cursor.exe": "code",
    "windsurf.exe": "code",
    "pycharm64.exe": "code",
    "idea64.exe": "code",
    "webstorm64.exe": "code",
    "goland64.exe": "code",
    "clion64.exe": "code",
    "rider64.exe": "code",
    "devenv.exe": "code",
    "sublime_text.exe": "code",
    "notepad++.exe": "code",
    "notepad.exe": "document",
    "windowsterminal.exe": "code",
    "wt.exe": "code",
    "cmd.exe": "code",
    "powershell.exe": "code",
    "pwsh.exe": "code",
    "conhost.exe": "code",
    "mintty.exe": "code",
    "git-bash.exe": "code",
    "wezterm-gui.exe": "code",
    "alacritty.exe": "code",
    "postman.exe": "code",
    "dbeaver.exe": "table",
    "navicat.exe": "table",
    "excel.exe": "table",
    "et.exe": "table",
    "wps.exe": "table",
    "powerpnt.exe": "design",
    "winword.exe": "document",
    "wpspdf.exe": "document",
    "acrord32.exe": "document",
    "sumatrapdf.exe": "document",
    "foxitpdfreader.exe": "document",
    "obsidian.exe": "document",
    "typora.exe": "document",
    "onenote.exe": "document",
    "notion.exe": "document",
    "explorer.exe": "webpage",
    "chrome.exe": "webpage",
    "msedge.exe": "webpage",
    "firefox.exe": "webpage",
    "brave.exe": "webpage",
    "opera.exe": "webpage",
    "360se.exe": "webpage",
    "qqbrowser.exe": "webpage",
    "sogouexplorer.exe": "webpage",
    "wechat.exe": "chat",
    "weixin.exe": "chat",
    "qq.exe": "chat",
    "tim.exe": "chat",
    "dingtalk.exe": "chat",
    "wework.exe": "chat",
    "wxwork.exe": "chat",
    "feishu.exe": "chat",
    "lark.exe": "chat",
    "telegram.exe": "chat",
    "discord.exe": "chat",
    "slack.exe": "chat",
    "skype.exe": "chat",
    "zoom.exe": "chat",
    "teams.exe": "chat",
    "ms-teams.exe": "chat",
    "photos.exe": "image",
    "figma.exe": "design",
    "photoshop.exe": "design",
    "illustrator.exe": "design",
    "blender.exe": "design",
    "xd.exe": "design",
    "sketch.exe": "design",
    "inkscape.exe": "design",
    "gimp-2.10.exe": "design",
    "krita.exe": "design",
    "vlc.exe": "video",
    "potplayer.exe": "video",
    "potplayermini64.exe": "video",
    "mpc-hc64.exe": "video",
    "mpv.exe": "video",
    "bilibili.exe": "video",
    "youku.exe": "video",
    "iqiyi.exe": "video",
    "acfun.exe": "video",
    "steam.exe": "game",
    "steamwebhelper.exe": "game",
    "epicgameslauncher.exe": "game",
    "genshinimpact.exe": "game",
    "yuanshen.exe": "game",
    "cs2.exe": "game",
    "dota2.exe": "game",
    "league of legends.exe": "game",
}

#: 窗口标题关键词 -> 类别（弱信号，低于 exe 提示）
TITLE_HINTS: list[tuple[str, str]] = [
    (r"\bvisual studio code\b|\bvscode\b|\bcursor\b|\bpycharm\b|\bintellij\b", "code"),
    (r"github|gitlab|stack overflow|docs\.|documentation|api reference", "code"),
    (r"terminal|powershell|命令提示符|终端", "code"),
    (r"\.py\b|\.ts\b|\.java\b|\.go\b|\.rs\b|\.cpp\b|\.cs\b", "code"),
    (r"excel|wps 表格|工作表|\.xlsx|\.csv\b|google sheets", "table"),
    (r"word|wps 文字|\.docx?\b|google docs", "document"),
    (r"powerpoint|wps 演示|\.pptx\b|keynote", "design"),
    (r"figma|photoshop|illustrator|sketch|canva|axure|mastergo", "design"),
    (r"youtube|bilibili|哔哩哔哩|netflix|爱奇艺|腾讯视频|播放器", "video"),
    (r"wechat|微信|telegram|discord|slack|飞书|钉钉|teams", "chat"),
    (r"chrome|edge|firefox|浏览器|browser|safari", "webpage"),
    (r"照片|photos|gallery|图片查看", "image"),
]

#: 中英文类别标签（写入文件名的 slug 词典）
CATEGORY_SLUG = {
    "code": "code",
    "document": "doc",
    "webpage": "web",
    "chat": "chat",
    "table": "sheet",
    "image": "pic",
    "design": "design",
    "video": "video",
    "game": "game",
    "other": "shot",
}


# --------------------------------------------------------------------------- #
# 上下文与结果
# --------------------------------------------------------------------------- #

@dataclass
class ShotContext:
    """截图时的上下文，用于命名与分类加权。"""

    exe: str = ""
    title: str = ""
    url: str = ""
    monitor: str = ""
    source: str = "region"       # region | window | fullscreen | all_screens | file | clipboard
    captured_at: Any = None

    def hint_category(self) -> tuple[str, float]:
        exe = (self.exe or "").strip().lower()
        title = self.title or ""
        if exe in EXE_HINTS:
            return EXE_HINTS[exe], 1.0
        for pattern, cat in TITLE_HINTS:
            if re.search(pattern, title, re.I):
                return cat, 0.55
        return "", 0.0

    def project_slug_hint(self) -> str:
        """从窗口标题里猜项目名：``main.py - myproj - Visual Studio Code``。"""
        title = self.title or ""
        if not title:
            return ""
        parts = [p.strip() for p in re.split(r"[|\-–—·»]+", title) if p.strip()]
        if not parts:
            return ""
        generic = {
            "visual studio code", "vscode", "cursor", "google chrome", "chrome",
            "microsoft edge", "edge", "firefox", "notepad", "记事本", "微信", "wechat",
            "新标签页", "new tab", "untitled", "无标题",
        }
        for part in parts:
            if part.lower() in generic:
                continue
            if re.fullmatch(r"[\d\W]+", part):
                continue
            return part
        return parts[0]


@dataclass
class Prediction:
    category: str = "other"
    confidence: float = 0.0
    scores: dict[str, float] = field(default_factory=dict)
    reason: str = ""
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "confidence": round(self.confidence, 3),
            "scores": {k: round(v, 2) for k, v in self.scores.items()},
            "reason": self.reason,
            "tags": list(self.tags),
        }


class Classifier(Protocol):
    def predict(self, f: Features, ctx: ShotContext) -> Prediction:  # pragma: no cover
        ...


# --------------------------------------------------------------------------- #
# 规则分类器
# --------------------------------------------------------------------------- #

def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


class RuleClassifier:
    """基于统计特征的加权打分分类器。"""

    name = "rules"

    def __init__(
        self,
        categories: list[str] | None = None,
        labels: dict[str, str] | None = None,
        confidence_floor: float = 0.34,
        use_app_hints: bool = True,
        user_rules: list | None = None,
    ) -> None:
        self.categories = categories or list(CATEGORY_SLUG)
        self.labels = labels or {}
        self.confidence_floor = confidence_floor
        self.use_app_hints = use_app_hints
        #: 用户自定义关键词规则（config.CategoryRule 的实例列表）。
        #: 用鸭子类型而不是 import config —— classifier 被 config 的上层依赖，
        #: 直接 import 会成环。规则对象只要有 matches()/category/weight/enabled
        #: 四个成员就能用。
        self.user_rules = [r for r in (user_rules or [])
                           if getattr(r, "enabled", True)
                           and getattr(r, "category", "")
                           and getattr(r, "keyword", "").strip()]

    # ------------------------------------------------------------------ #

    def predict(self, f: Features, ctx: ShotContext | None = None) -> Prediction:
        ctx = ctx or ShotContext()
        s: dict[str, float] = {}

        aspect = f.aspect or 1.0
        is_portrait = aspect < 0.86
        is_wide = aspect > 1.5
        texty = f.edge_density
        is_dark = f.lum_mean < 96
        is_light = f.lum_mean > 190
        colorful = f.sat_mean > 42
        photo_like = float(f.extra.get("photo_like", 0.0))
        tiny_glyphs = 1.0 <= f.stroke_mean <= 2.6
        big_glyphs = f.stroke_mean > 4.2
        # 主导亮度峰：文档是"亮白底 + 高占比"，代码/终端是"暗底 + 高占比"，
        # 照片 / 视频 / 图表没有明显单峰。
        bg_is_paper = f.bg_lum >= 195 and f.bg_dominance >= 0.42
        bg_is_ink = f.bg_lum <= 90 and f.bg_dominance >= 0.38
        peaked_bg = f.bg_dominance >= 0.40
        dense_text = f.edge_density > 0.17
        # 表格网格：等距横线（行距稳定）是表格最强的排他信号
        period = float(f.extra.get("row_period", 0.0) or 0.0)
        regular = float(f.extra.get("row_regularity", 0.0) or 0.0)
        grid_score = regular if 8.0 <= period <= 80.0 else 0.0
        if f.frame_edge > 0.05 and period > 0:
            grid_score = max(grid_score, 0.45 * regular)
        # 版面成行成列的程度（代码每行都是符号块，文档隔行断开）
        text_layout = f.row_activity * f.row_activity
        page_flat = f.flat_ratio > 0.55

        # --- code / 终端 -------------------------------------------------
        s["code"] = 0.0
        if is_dark:
            s["code"] += 2.6
        s["code"] += _clamp(f.dark_ratio / 0.25) * 1.6
        s["code"] += _clamp(texty / 0.16) * 1.4
        s["code"] += _clamp((40 - f.sat_mean) / 40) * 0.9
        s["code"] += 1.0 if tiny_glyphs else 0.0
        s["code"] += _clamp(f.row_activity / 0.35) * 1.2
        s["code"] += 1.1 if bg_is_ink else 0.0                        # 深色代码底 / 终端
        s["code"] += 0.6 if is_dark and f.bg_lum < 60 else 0.0
        s["code"] += 0.8 if f.left_band_edge > 0.05 else 0.0          # 行号 / git 边栏
        s["code"] += 0.6 if is_dark and bg_is_ink else 0.0
        s["code"] -= _clamp(f.dom_ratio / 0.5) * 1.0                  # 大片纯色更像 UI
        s["code"] -= 1.2 if photo_like > 0.66 else 0.0
        s["code"] -= 0.6 if f.white_ratio > 0.5 else 0.0
        s["code"] -= 1.0 if bg_is_paper and not bg_is_ink else 0.0    # 纸张底不是代码

        # --- document / 阅读 --------------------------------------------
        # 文档 = 亮底 + 无色 + 正文成行（重排版式信号，轻底色信号）
        s["document"] = 0.0
        if is_light:
            s["document"] += 1.6
        s["document"] += _clamp(f.white_ratio / 0.45) * 1.2
        s["document"] += _clamp(f.text_line_ratio / 0.28) * 3.0
        s["document"] += _clamp((30 - f.sat_mean) / 30) * 0.9
        s["document"] += _clamp(texty / 0.20) * 1.5
        s["document"] += 1.6 if bg_is_paper and f.edge_density > 0.03 else 0.0  # 白纸+正文
        s["document"] += 1.0 if bg_is_paper else 0.0
        s["document"] += 0.7 if is_light and bg_is_paper and f.lum_std < 60 else 0.0
        s["document"] += 0.6 if f.has_toolbar and f.top_band_flat > 0.6 else 0.0  # 阅读器标题栏
        s["document"] -= _clamp(text_layout / 0.25) * 1.3             # 每行都成块=代码/终端
        s["document"] -= 1.1 if f.left_band_edge > 0.10 else 0.0      # 代码侧栏
        s["document"] -= 1.4 if is_dark else 0.0
        s["document"] -= 1.6 * grid_score                             # 等距横线=表格
        s["document"] -= 1.0 if bg_is_paper and f.text_line_ratio < 0.04 else 0.0
        # 说明：曾有两条针对"桌面壁纸误判成文档"的 flat_ratio/purity 抑制项，
        # 但合成文档（86% 纯白纸面）与壁纸在这一维上无法区分，压制代价是
        # 连真实文档一起掉分。最终改为在置信度阶段统一降级（见下方
        # "context-free 纯视觉判定"），保持打分项只描述"像不像这个类别"。

        # --- webpage / 应用界面 ------------------------------------------
        # 网页 = 有工具条/侧栏的"外壳"，靠 UI 分块取胜，不靠底色
        s["webpage"] = 0.0
        s["webpage"] += _clamp(f.has_toolbar) * 1.5
        s["webpage"] += _clamp(f.flat_ratio / 0.30) * 1.1
        s["webpage"] += _clamp(f.dom_ratio / 0.28) * 1.0
        s["webpage"] += _clamp((46 - f.sat_mean) / 46) * 0.6
        s["webpage"] += 0.8 if is_wide else 0.0
        s["webpage"] += 0.7 if f.colorfulness < 160 else 0.0
        s["webpage"] += 0.6 if dense_text and not photo_like else 0.0  # 富文本正文页
        s["webpage"] += _clamp(f.top_band_flat / 0.9) * 1.2           # 顶部 chrome/工具条
        s["webpage"] += 0.8 if f.has_sidebar else 0.0
        s["webpage"] += 0.6 if f.has_sidebar and f.has_toolbar else 0.0
        s["webpage"] -= 1.2 if photo_like > 0.7 else 0.0
        s["webpage"] -= 0.9 if f.text_line_ratio > 0.20 else 0.0
        s["webpage"] -= 2.0 * grid_score                              # 等距横线=表格
        s["webpage"] -= 1.2 if f.dark_ratio > 0.25 else 0.0           # 深色底不是网页
        s["webpage"] -= 0.8 if is_dark and f.bg_dominance > 0.55 else 0.0
        s["webpage"] -= 1.6 if bg_is_paper and f.text_line_ratio > 0.12 else 0.0

        # --- chat --------------------------------------------------------
        # 聊天 = 左侧会话列表 + 气泡色块（少量高饱和像素），边缘密度低于文档
        s["chat"] = 0.0
        s["chat"] += _clamp(f.left_band_edge / 0.12) * 1.6
        s["chat"] += 1.2 if f.has_sidebar else 0.0
        s["chat"] += _clamp((44 - f.sat_mean) / 44) * 0.6
        s["chat"] += 1.0 if is_portrait else 0.0
        s["chat"] += _clamp(f.white_ratio / 0.5) * 1.0
        s["chat"] += 0.9 if f.dom_ratio > 0.2 else 0.0
        s["chat"] += 0.6 if f.bg_lum >= 200 and f.bg_dominance >= 0.75 else 0.0
        s["chat"] += 1.4 if f.white_ratio > 0.18 and f.sat_mean < 30 else 0.0
        s["chat"] += 0.9 if f.flat_ratio < 0.75 else 0.0              # 色块打散了大面积底板
        s["chat"] += 0.8 if 0.9 < aspect < 1.35 else 0.0
        s["chat"] += 0.9 if f.text_line_ratio < 0.08 else 0.0
        s["chat"] -= 1.8 * grid_score
        s["chat"] -= 0.9 if is_wide else 0.0
        s["chat"] -= 1.0 if is_dark else 0.0

        # --- table -------------------------------------------------------
        # 表格 = 等距横线网格（row_period + 规则性），辅以白底、低彩、稀疏文字
        s["table"] = 0.0
        s["table"] += grid_score * 5.0
        s["table"] += _clamp(f.frame_edge / 0.10) * 1.2
        s["table"] += _clamp(f.white_ratio / 0.55) * 0.8
        s["table"] += _clamp(f.flat_ratio / 0.6) * 1.0 if page_flat else 0.0
        s["table"] += _clamp((45 - f.sat_mean) / 45) * 0.6
        s["table"] += 0.5 if f.colorfulness < 120 else 0.0
        s["table"] -= 1.0 if is_dark else 0.0
        s["table"] -= 1.4 if f.text_line_ratio > 0.45 else 0.0

        # --- image / 照片 ------------------------------------------------
        s["image"] = photo_like * 3.4
        s["image"] += _clamp((f.sat_mean - 30) / 90) * 1.1
        s["image"] += _clamp(f.center_sat / 80) * 0.8
        s["image"] += 0.9 if f.center_edge < 0.06 else 0.0
        s["image"] += 0.8 if f.white_ratio < 0.06 else 0.0
        s["image"] += 0.6 if f.colorfulness > 220 else 0.0
        s["image"] += 0.7 if not peaked_bg else 0.0
        s["image"] -= 1.1 if f.has_toolbar > 0 and f.top_band_flat > 0.6 else 0.0
        s["image"] -= 0.9 if texty > 0.24 else 0.0

        # --- design / 图表 -----------------------------------------------
        s["design"] = 0.0
        s["design"] += _clamp(f.colorfulness / 300) * 1.2 if f.colorfulness > 90 else 0.0
        s["design"] += _clamp(f.center_flat / 0.55) * 1.1
        s["design"] += 0.9 if f.sat_mean > 24 else 0.0
        s["design"] += 0.8 if f.left_band_edge > 0.03 and f.has_toolbar else 0.0
        s["design"] += 0.7 if 0.9 < aspect < 2.4 else 0.0
        s["design"] -= 1.0 if photo_like > 0.72 else 0.0
        s["design"] -= 0.7 if f.text_line_ratio > 0.4 else 0.0

        # --- video -------------------------------------------------------
        s["video"] = 0.0
        s["video"] += _clamp((f.sat_mean - 34) / 80) * 1.5
        s["video"] += _clamp(f.center_sat / 85) * 1.1
        s["video"] += 0.7 if f.lum_std > 52 else 0.0
        s["video"] += 0.5 if is_wide else 0.0
        s["video"] -= 1.2 if texty > 0.2 else 0.0
        s["video"] -= 0.6 if f.white_ratio > 0.12 else 0.0

        # --- game --------------------------------------------------------
        s["game"] = 0.0
        s["game"] += _clamp((f.sat_mean - 46) / 70) * 1.4
        s["game"] += _clamp((f.lum_std - 45) / 45) * 0.9
        s["game"] += 0.8 if is_wide and f.dom_ratio < 0.2 else 0.0
        s["game"] += 0.6 if f.colorfulness > 260 else 0.0
        s["game"] -= 1.4 if f.white_ratio > 0.2 else 0.0
        s["game"] -= 1.0 if texty > 0.22 else 0.0

        # --- 应用上下文加权 -------------------------------------------------
        # 进程/标题只是"加分项"而不是"决定项"：给 2.2 上限，保证视觉证据能推翻
        # 上下文（例如在浏览器里看 PDF / 在记事本里写代码）。
        reason = []
        hint_cat, hint_conf = ("", 0.0)
        if self.use_app_hints:
            hint_cat, hint_conf = ctx.hint_category()
            if hint_cat and hint_cat in s:
                boost = 2.2 * hint_conf
                s[hint_cat] += boost
                reason.append(f"app={ctx.exe or ctx.title[:28]!r}->{hint_cat}(+{boost:.1f})")

        # --- 用户关键词规则 -------------------------------------------------
        # 内置 app 提示上限 2.2（保证视觉证据能推翻它）；用户规则上限放到 3.0 ——
        # 这是用户**明确要求**"这个应用的图都归某类"，优先级该比内置猜测更高。
        # 仍然封顶，避免一条手滑填错的关键词把整个分类带跑偏。
        for rule in self.user_rules:
            cat = rule.category
            if cat not in s:
                continue
            try:
                if not rule.matches(ctx.exe, ctx.title, ctx.url):
                    continue
            except Exception:
                continue
            boost = min(3.0, max(0.0, float(getattr(rule, "weight", 1.5))))
            s[cat] += boost
            reason.append(f"rule={rule.keyword!r}->{cat}(+{boost:.1f})")

        # ---------------- 归一化 ----------------
        best = max(s, key=lambda k: s[k])
        ordered = sorted(s.values(), reverse=True)
        top = ordered[0]
        second = ordered[1] if len(ordered) > 1 else 0.0
        if top <= 0:
            best, top, second = "other", 0.0, 0.0
        # 置信度 = 得分强度 + 领先优势（margin 用相对差距，避免分数整体抬高时惩罚过重）
        strength = _clamp(top / 7.0)
        margin = _clamp((top - second) / max(1.2, top * 0.35))
        conf = _clamp(0.55 * strength + 0.45 * margin)
        if second < 1.0:
            conf = min(1.0, conf + 0.12)         # 没有像样的竞争者 → 更可信
        if top < 1.6:
            conf = min(conf, 0.25)
        # 上下文为空时的"纯视觉"判定：没有任何进程/标题佐证，而画面呈现
        # "中间调的单峰背景 + 颜色杂乱（低纸张纯度）+ 大片纯色"——这是桌面壁纸
        # 的特征（实测 purity=0.43 / bg_lum=204 / flat=0.74），与真纸不同：
        # 真纸是接近纯白的高亮底（Word/表格/合成文档 bg_lum 236~248、
        # purity 0.55~1.00）。此时候选类别之间的差距本就不可靠，宁可把置信度
        # 压到 floor 以下归入 other，让用户在图库里手动改分类，也不要输出一个
        # "确信的错误标签"——错标比未分类更难被发现。
        if (not hint_cat and f.bg_purity < 0.50 and f.flat_ratio > 0.35
                and f.bg_lum < 220):
            conf = min(conf, self.confidence_floor * 0.9)
            reason.append(f"paper-like=low(purity={f.bg_purity:.2f}, bg={f.bg_lum:.0f})->other?")
        if conf < self.confidence_floor:
            best = "other"

        tags = self._derive_tags(f, ctx, best)
        return Prediction(
            category=best,
            confidence=conf,
            scores=s,
            reason="; ".join(reason) or f"visual(top={top:.1f}, 2nd={second:.1f})",
            tags=tags,
        )

    # ------------------------------------------------------------------ #

    def _derive_tags(self, f: Features, ctx: ShotContext, category: str) -> list[str]:
        tags: list[str] = [category]
        r, g, b = f.dom_color
        if f.dark_ratio > 0.35 or f.lum_mean < 80:
            tags.append("dark")
        elif f.white_ratio > 0.45:
            tags.append("light")
        if f.sat_mean > 60:
            tags.append("colorful")
        elif f.sat_mean < 12:
            tags.append("mono")
        if f.aspect < 0.86:
            tags.append("portrait")
        elif f.aspect > 1.9:
            tags.append("wide")
        if f.edge_density > 0.24:
            tags.append("text-heavy")
        if r > 150 and g < 110 and b < 110:
            tags.append("warm")
        elif b > 150 and r < 130:
            tags.append("cool")
        if ctx.exe:
            tags.append(os.path.splitext(ctx.exe)[0].lower()[:18])
        tags.append(ctx.source)
        return _uniq(tags)


def _uniq(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for i in items:
        k = i.strip().lower()
        if k and k not in seen:
            seen.add(k)
            out.append(i.strip())
    return out


# --------------------------------------------------------------------------- #
# 后端注册
# --------------------------------------------------------------------------- #

BACKENDS: dict[str, Callable[..., Classifier]] = {
    "rules": RuleClassifier,
}


def build_classifier(name: str = "rules", **kwargs: Any) -> Classifier:
    if name not in BACKENDS:
        raise KeyError(f"unknown classifier backend: {name!r} (available: {list(BACKENDS)})")
    return BACKENDS[name](**kwargs)


def classify(
    features: Features,
    ctx: ShotContext | None = None,
    **options: Any,
) -> Prediction:
    """一次性便捷函数。"""
    return RuleClassifier(**options).predict(features, ctx)
