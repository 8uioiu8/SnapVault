"""SnapVault - 智能截图归档工具.

对标 Windows 自带 Win+Shift+S 的截图体验，额外提供：
自动命名、内容分类、标签归档、可检索图库。

模块划分::

    config.py      配置模型与持久化
    analyzer.py    图像特征提取（纯 numpy + Pillow，无重型依赖）
    classifier.py  规则式内容分类（可插拔 ML 后端）
    naming.py      自动命名规则引擎（模板 + slug + 标签）
    vault.py       归档存储（日期目录 / SQLite 索引 / JSONL 审计 / 缩略图）
    capture.py     截图捕获（虚拟桌面 / 区域 / 窗口 / PrintWindow）
    winapi.py      Win32 绑定（全局热键 / 窗口枚举 / DPI / 剪贴板）
    overlay.py     区域选择悬浮层（放大镜 / 窗口高亮 / 工具条）
    shot.py        截图流水线（捕获 → 分析 → 分类 → 命名 → 归档 → 反馈）
    settings_ui.py 设置面板
    gallery.py     图库主界面（搜索 / 分类 / 标签 / 预览）
    app.py         托盘 + 热键 + 主装配
    __main__.py    命令行入口
"""

from .config import Config, load_config, config_path

__version__ = "1.0.0"
__all__ = ["Config", "load_config", "config_path", "__version__"]
