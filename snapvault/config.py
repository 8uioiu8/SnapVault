"""配置模型与持久化.

配置以 JSON 形式存放于 ``%APPDATA%/SnapVault/config.json``，
所有字段都有安全默认值，缺失字段自动回填，损坏文件自动重建备份。
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any


# --------------------------------------------------------------------------- #
# 默认值
# --------------------------------------------------------------------------- #

DEFAULT_CATEGORIES = [
    "code",       # 代码 / 终端
    "document",   # 文档 / 网页阅读
    "webpage",    # 通用网页 / 应用界面
    "chat",       # 聊天 / 会话
    "table",      # 表格 / 数据
    "image",      # 图片 / 照片
    "design",     # 图表 / 设计稿
    "video",      # 视频画面
    "game",       # 游戏画面
    "other",      # 未识别
]

DEFAULT_CATEGORY_LABELS = {
    "code": "代码",
    "document": "文档",
    "webpage": "网页",
    "chat": "聊天",
    "table": "表格",
    "image": "图片",
    "design": "设计",
    "video": "视频",
    "game": "游戏",
    "other": "其他",
}

#: 文件名模板可用占位符：{date} {time} {stamp} {category} {slug} {tags} {hash} {seq} {app}
DEFAULT_TEMPLATE = "{date}_{time}_{category}_{slug}"


def pictures_dir() -> Path:
    """用户「图片」文件夹的真实路径。

    Windows 允许把这个文件夹整体重定向到别的盘（注册表
    ``User Shell Folders\\My Pictures``）。直接拼 ``%USERPROFILE%\\Pictures``
    会指到错误的位置——本机就踩过这个坑（图片文件夹被移到了 D 盘，
    于是一直盯着一个空的 C 盘目录）。
    """
    try:
        import winreg  # type: ignore

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "My Pictures")
        candidate = Path(os.path.expandvars(str(value)))
        if candidate.is_dir():
            return candidate
    except Exception:
        pass
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    return home / "Pictures"


def default_vault_dir() -> str:
    """默认归档根目录：``<图片>/SnapVault``。"""
    pics = pictures_dir()
    if not pics.is_dir():
        pics = Path(os.environ.get("USERPROFILE") or Path.home())
    return str(pics / "SnapVault")


def default_screenshots_dir() -> str:
    """系统截图工具的默认输出目录：``<图片>/Screenshots``。

    Windows 11 的「截图工具」把自动保存的截图写进"图片"文件夹下的
    Screenshots 子目录，所以这里必须跟着"图片"文件夹走——它被重定向到
    哪个盘，截图就在哪个盘。接管器默认就盯这个目录。
    """
    return str(pictures_dir() / "Screenshots")


def config_path() -> Path:
    base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "SnapVault" / "config.json"


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #

@dataclass
class HotkeyConfig:
    """全局热键，值形如 ``Ctrl+Alt+A`` / ``PrtSc`` / 空字符串表示禁用。"""

    region: str = "Ctrl+Alt+A"     # 区域截图
    window: str = "Ctrl+Alt+W"     # 窗口截图（光标所在窗口）
    fullscreen: str = "Ctrl+Alt+F"  # 全屏截图（光标所在显示器）
    all_screens: str = ""          # 全部显示器
    repeat_last: str = "Ctrl+Alt+R"  # 重复上次区域
    gallery: str = "Ctrl+Alt+G"    # 打开图库


@dataclass
class NamingConfig:
    template: str = DEFAULT_TEMPLATE
    # 24 对中文 AI 描述太紧了（"WorkBuddy任务管理界面与AI代码编辑器并排"正好 25 字，
    # 会被砍掉最后一个字变成"…编辑器并"）。放宽到 30，留出余量。
    slug_max_len: int = 30
    slug_lang: str = "auto"        # auto | cn | en | off （中文标题是否保留汉字）
    annotate_project: bool = True  # 附加窗口标题里的项目名
    hash_prefix: bool = False      # 是否在文件名加入内容短哈希
    hash_len: int = 6
    lowercase_slug: bool = True
    separator: str = "_"
    include_tags: bool = False     # 是否把 top 标签写入文件名


@dataclass
class StorageConfig:
    vault_dir: str = field(default_factory=default_vault_dir)
    folder_pattern: str = "{yyyy}/{MM}"     # 相对 vault_dir 的子目录模板
    flat: bool = False                      # True 则不分子目录
    copies_subdir: str = "Copies"           # 复制文件统一存放
    image_format: str = "png"               # png | jpg | webp
    jpg_quality: int = 92
    thumb_size: int = 384
    keep_index: bool = True                 # 维护 SQLite 索引
    keep_jsonl: bool = True                 # 维护 JSONL 审计日志
    # 重建索引时只认"文件名像 SnapVault 归档"（YYYYMMDD_HHMMSS_…）的图片。
    # 图库根目录若被设成 D:\Pictures 这种大目录，能避免把用户自己的照片一起卷进来。
    rebuild_only_named: bool = True
    dedupe: bool = False                    # 内容重复时加 -2 -3 后缀
    copy_to_clipboard: bool = True
    open_editor_after: bool = False         # 截图后立刻进入标注
    play_sound: bool = False
    notify: bool = True


@dataclass
class CaptureConfig:
    show_magnifier: bool = True
    magnifier_zoom: int = 6
    show_crosshair: bool = True
    snap_to_window: bool = True     # 选区吸附近似窗口边界
    snap_tolerance: int = 12
    remember_last_rect: bool = True
    dim_alpha: int = 90             # 遮罩不透明度 0-255
    default_mode: str = "region"    # region | window | fullscreen | all_screens


@dataclass
class ClassifyConfig:
    enabled: bool = True
    probe_rows: int = 420           # 特征采样行上限（加速）
    app_hints: bool = True          # 使用进程名 / 窗口标题加权
    confidence_floor: float = 0.34  # 低于该值归入 other
    ocr: bool = False               # 可选 OCR（Windows.Media.Ocr 后端）


@dataclass
class AiConfig:
    """AI 视觉理解（默认通义千问 Qwen-VL，也支持本地 Ollama）。

    一次请求同时拿到「描述」和「分类」：描述做文件名，分类（可选）覆盖本地
    视觉规则分类——AI 看懂了内容，判定通常比纯图像特征更准；但它失败时一律
    静默回落规则分类，绝不阻塞截图。

    ``provider`` 决定默认端点 / 模型 / 要不要 Key：

    * ``bailian`` 阿里云百炼（默认，云端，要 Key）
    * ``ollama``  本地 Ollama（``http://localhost:11434/v1``，**不需要 Key**，
      图片不出本机——隐私诉求的出口）
    * ``custom``  任何 OpenAI 兼容端点（自建代理 / 别的云）

    端点和模型永远可改：预设只是把常用值填进去，不锁死。
    """

    enabled: bool = False           # 是否用 AI 生成描述作为文件名主体
    provider: str = "bailian"       # bailian | ollama | custom
    api_key: str = ""               # 云端供应商的 Key（Ollama 留空即可）
    model: str = "qwen3-vl-plus"    # 可视化模型名
    endpoint: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    prompt: str = ""                # 自定义提示词（留空用内置默认）
    timeout: float = 15.0           # 请求超时（秒）
    max_desc_len: int = 30          # 描述最长字符数（超过截断；留足空间避免切在词中间）
    include_in_name: bool = True    # 描述是否写入文件名（否则仅入库供检索）
    classify_with_ai: bool = True   # 是否采信 AI 给出的分类（覆盖视觉规则；不合法则回落）


#: 供应商预设。``needs_key`` 为 False 时（Ollama）不填 Key 也能用。
#: ``models`` 只作为下拉里的候选，用户照样可以手打别的模型名。
AI_PROVIDERS: dict[str, dict[str, Any]] = {
    "bailian": {
        "label": "阿里云百炼",
        "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": ["qwen3-vl-plus", "qwen3-vl-flash"],
        "needs_key": True,
        "hint": "在阿里云百炼控制台开通 DashScope 后取 API Key。",
    },
    "ollama": {
        "label": "Ollama 本地",
        "endpoint": "http://localhost:11434/v1",
        "models": ["qwen2.5vl", "llama3.2-vision", "minicpm-v"],
        "needs_key": False,
        "hint": "先在本机安装 Ollama 并拉一个视觉模型：ollama pull qwen2.5vl。"
                "图片不出本机。",
    },
    "custom": {
        "label": "自定义（OpenAI 兼容）",
        "endpoint": "",
        "models": [],
        "needs_key": True,
        "hint": "任何实现了 /chat/completions 的端点，填 Base URL 即可。",
    },
}


def provider_preset(name: str) -> dict[str, Any]:
    """取供应商预设；未知名字回落百炼。"""
    return AI_PROVIDERS.get(name or "", AI_PROVIDERS["bailian"])


def provider_needs_key(name: str) -> bool:
    return bool(provider_preset(name).get("needs_key", True))


@dataclass
class CategoryRule:
    """用户自定义关键词规则：命中就把对应分类加分。

    给"高自由度分类"用的——内置视觉规则覆盖不到的情况（比如某个内部工具的
    界面长得就像网页，但你希望它永远归 ``work``），在这里加一条关键词就行。

    匹配对象：进程名、窗口标题、文件名（大小写不敏感的子串）。
    ``weight`` 是加分档位：1.0 与内置 exe 提示同档，2.0 起视觉证据基本翻不了。
    """

    keyword: str = ""              # 匹配用关键词（子串，大小写不敏感）
    category: str = "other"        # 命中后偏向的分类键
    weight: float = 1.5            # 加分权重
    enabled: bool = True

    def matches(self, *texts: str) -> bool:
        """任一文本包含该关键词即算命中（忽略大小写与首尾空白）。"""
        kw = (self.keyword or "").strip().lower()
        if not kw:
            return False
        return any(kw in (t or "").lower() for t in texts)


@dataclass
class WatchConfig:
    """系统截图接管：监视系统截图工具的输出目录，新图自动 AI 命名 + 分类归档。

    Windows 自带截图工具的 MSIX 包改不了（签名 + 更新覆盖），所以不去动它本体，
    改成盯它的输出目录——效果一样，风险为零。
    """

    enabled: bool = False           # 默认关闭：开启即表示同意"新截图被自动接管"
    dirs: list[str] = field(default_factory=list)   # 空 = 用系统默认截图目录
    source_action: str = "keep"     # keep | recycle | delete（原图处理方式）
    settle_ms: int = 1200           # 文件大小静置多久才算写完
    interval_ms: int = 1500         # 轮询间隔
    skip_existing: bool = True      # 首次启用时跳过目录里已存在的图片（只接管之后新增的）


@dataclass
class UIConfig:
    theme: str = "dark"             # dark | light
    thumb_size: int = 220
    language: str = "zh"
    start_minimized: bool = True
    autostart: bool = False
    close_to_tray: bool = True
    # 关掉所有过渡动画（窗口淡入、内容切换、hover 插值），全部改成瞬切。
    # 给两类人用：晕动症 / 前庭功能敏感的用户，和"我就想看它别跟我演戏"的用户。
    # 默认 False —— 150 ms 的 OutCubic 对绝大多数人是手感而不是干扰。
    reduce_motion: bool = False


@dataclass
class Config:
    hotkeys: HotkeyConfig = field(default_factory=HotkeyConfig)
    naming: NamingConfig = field(default_factory=NamingConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    classify: ClassifyConfig = field(default_factory=ClassifyConfig)
    ai: AiConfig = field(default_factory=AiConfig)
    watch: WatchConfig = field(default_factory=WatchConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    categories: list[str] = field(default_factory=lambda: list(DEFAULT_CATEGORIES))
    category_labels: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_CATEGORY_LABELS))
    #: 分类图标覆盖：键 -> 图标名。没配的走 theme.CATEGORY_ICONS 内置表。
    category_icons: dict[str, str] = field(default_factory=dict)
    #: 用户自定义关键词分类规则（见 CategoryRule）。
    category_rules: list[CategoryRule] = field(default_factory=list)

    # ---------------- 读写 ---------------- #

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Config":
        cfg = cls()
        for f in dataclasses.fields(cls):
            if f.name not in raw:
                continue
            value = raw[f.name]
            current = getattr(cfg, f.name)
            if dataclasses.is_dataclass(current) and isinstance(value, dict):
                for sub in dataclasses.fields(current):
                    if sub.name in value:
                        setattr(current, sub.name, value[sub.name])
            elif f.name == "category_rules":
                # 列表里装的是 dataclass，逐条重建；坏条目跳过而不是让整个配置崩
                rules: list[CategoryRule] = []
                for item in value or []:
                    if isinstance(item, CategoryRule):
                        rules.append(item)
                    elif isinstance(item, dict):
                        try:
                            rules.append(CategoryRule(**{
                                k: v for k, v in item.items()
                                if k in {x.name for x in dataclasses.fields(CategoryRule)}
                            }))
                        except Exception:
                            continue
                cfg.category_rules = rules
            else:
                setattr(cfg, f.name, value)
        cfg.migrate()
        return cfg

    def migrate(self) -> None:
        """把旧配置无损升级到当前结构。

        只做"补默认值 / 推供应商预设"这类不丢信息的改动；用户改过的值一律不动。
        """
        # 没选过供应商 → 按有没有 Key 猜一个，别让老用户升级后 AI 突然不可用
        prov = (self.ai.provider or "").strip().lower()
        if prov not in AI_PROVIDERS:
            self.ai.provider = "bailian" if self.ai.api_key else "ollama" \
                if "11434" in (self.ai.endpoint or "") else "bailian"
        # 端点还是百炼默认、但用户切到了 ollama → 补上本地端点
        preset = provider_preset(self.ai.provider)
        if self.ai.provider == "ollama" and not (self.ai.endpoint or "").strip():
            self.ai.endpoint = preset["endpoint"]
        # 分类列表里不该有空键 / 重复键（用户手改 config.json 完全可能）
        seen: set[str] = set()
        cleaned: list[str] = []
        for key in self.categories:
            k = str(key or "").strip()
            if k and k not in seen:
                seen.add(k)
                cleaned.append(k)
        if not cleaned:
            cleaned = list(DEFAULT_CATEGORIES)
            self.category_labels = dict(DEFAULT_CATEGORY_LABELS)
        self.categories = cleaned
        # 规则里引用了不存在的分类 → 改成 other，别让一条野规则把结果带偏
        for rule in self.category_rules:
            if rule.category not in seen:
                rule.category = "other"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    # ---------------- 便捷访问 ---------------- #

    @property
    def vault_path(self) -> Path:
        return Path(os.path.expandvars(self.storage.vault_dir)).expanduser()

    def category_label(self, key: str) -> str:
        return self.category_labels.get(key, key)

    def category_icon(self, key: str) -> str:
        """分类图标：用户覆盖优先，否则内置表，再否则落到通用图标。"""
        from . import theme as _theme

        return self.category_icons.get(key) or _theme.CATEGORY_ICONS.get(key, "file")

    def rule_hint(self, exe: str = "", title: str = "", filename: str = "") -> tuple[str, float]:
        """用户关键词规则命中判定。返回 (分类键, 权重)，没命中返回 ("", 0)。

        规则之间是"取权重最高的一条"，不是累加 —— 累加会让两条弱规则盖过
        一条强规则，行为不可预测。
        """
        best, weight = "", 0.0
        for rule in self.category_rules:
            if not rule.enabled or not rule.category:
                continue
            if rule.matches(exe, title, filename) and rule.weight > weight:
                best, weight = rule.category, float(rule.weight)
        return best, weight

    def folder_for(self, when: datetime) -> Path:
        """按 folder_pattern 计算归档子目录。"""
        if self.storage.flat:
            return self.vault_path
        pattern = self.storage.folder_pattern or "{yyyy}/{MM}"
        rel = (
            pattern.replace("{yyyy}", f"{when:%Y}")
            .replace("{yy}", f"{when:%y}")
            .replace("{MM}", f"{when:%m}")
            .replace("{dd}", f"{when:%d}")
            .replace("{category}", "{category}")  # 分类目录由 vault 层填充
        )
        rel = rel.strip("/\\")
        return self.vault_path / rel if rel else self.vault_path


# --------------------------------------------------------------------------- #
# 载入 / 保存
# --------------------------------------------------------------------------- #

def load_config(path: Path | None = None) -> Config:
    p = Path(path) if path else config_path()
    if not p.exists():
        cfg = Config()
        save_config(cfg, p)
        return cfg
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return Config.from_dict(raw)
    except Exception:
        # 损坏则备份后重建，绝不因为配置问题挡住截图主流程
        try:
            shutil.copy2(p, p.with_suffix(f".broken-{datetime.now():%Y%m%d%H%M%S}.json"))
        except Exception:
            pass
        cfg = Config()
        save_config(cfg, p)
        return cfg


def save_config(cfg: Config, path: Path | None = None) -> Path:
    p = Path(path) if path else config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    return p


def reveal(path: Path) -> None:
    """在资源管理器中定位文件（失败静默）。"""
    try:
        if sys.platform == "win32":
            import subprocess

            if path.is_dir():
                subprocess.Popen(["explorer", str(path)])
            else:
                subprocess.Popen(["explorer", "/select,", str(path)])
    except Exception:
        pass
