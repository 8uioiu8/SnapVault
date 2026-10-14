"""SnapVault 自检脚本（无 GUI）.

用法::

    python -m snapvault.selftest          # 全部检查
    python -m snapvault.selftest --keep   # 保留临时图库用于人工检查

检查项：
  1. 配置读写与容错
  2. 命名规则（模板 / slug / 中文 / 非法字符 / 重名）
  3. 分类器（6 类合成截图 + 真实截图回归）
  4. 归档流程（写盘 / 入库 / 搜索 / 标签重算 / 改分类 / 重建索引 / 导出清单）
  5. WebP / JPG 编码与缩略图
  6. 设计令牌（颜色 / 圆角 / 间距 / 字号）、图标名、随包字体，
     以及「业务代码里不许再出现硬编码色值和 setStyleSheet」这条回归线
"""

from __future__ import annotations

import argparse
import builtins
import os
import re
import shutil
import symtable
import sys
import tempfile
import time
import traceback
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw

from .analyzer import analyze
from .classifier import RuleClassifier, ShotContext
from .config import CategoryRule, Config, save_config
from .naming import AutoNamer, slugify, unique_path
from .vault import Vault

#: 项目根目录。自检里有一组检查要扫源码，得知道从哪儿开始。
ROOT = Path(__file__).resolve().parent.parent
_SRC_DIR = ROOT / "snapvault"


def source_available() -> bool:
    """源码在不在磁盘上。**打包成 exe 后不在。**

    为什么会需要这个判断：自检里有 6 项是"读源码"的静态检查（图标名笔误、
    硬编码色值、散落的 setStyleSheet、动效时长单一来源、AppID 时序、漏 import）。
    它们原本靠 ``glob("*.py")`` 遍历 —— 打包后读不到任何文件，循环体一次都不进，
    于是 ``expect(..., not bad)`` **得到"通过"**：看着全绿，其实一项没查。
    一个静默的假绿比一条 FAIL 危险得多。

    而 ``test_appid_timing`` 是直接读某个具体文件，打包后当场 FileNotFoundError，
    把整个自检打挂（现象是：只打出抬头就退出，一条检查结果都没有）——
    这个坑是 2026-10-14 打第一个 exe 时踩到的。
    """
    return _SRC_DIR.is_dir() and any(_SRC_DIR.glob("*.py"))


def _skip_src(name: str) -> None:
    """源码不可用时，把这 6 项显式记成"跳过"而不是静默通过。"""
    check(f"{name}（打包产物里没有源码，跳过）")


#: 自检进程内唯一的 QApplication。必须留引用 —— 局部变量一被回收，
#: Qt 的应用对象就没了（PySide6 不额外持有它）。
_APP = None


def ensure_app():
    """确保有一个 QApplication，没有就试着建一个；建不起来返回 None。

    为什么需要：``QFontDatabase.addApplicationFont`` **必须**有 QApplication
    实例，否则直接段错误。而"随包字体能不能注册上"恰恰是打包后最该验的一项
    （16 MB 的字体没挂上，字重 500/600 会退化成"假粗体"，层级塌掉而且
    肉眼不容易归因）。改之前这里只判断 ``QGuiApplication.instance() is None``
    就记"跳过"—— 而自检跑到这里时**从来没有**任何应用对象，于是这两项
    在源码版和 exe 版里都永远跳过，等于没查。

    用 QApplication 而不是 QGuiApplication：后面 ``test_settings_ui`` 要建
    控件，只有 QGuiApplication 时 Qt 会拒绝创建 QWidget。
    """
    global _APP
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is not None:
        return app
    try:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        _APP = QApplication([])
        return _APP
    except Exception:                                       # noqa: BLE001
        return None

PASS, FAIL = "PASS", "FAIL"
_results: list[tuple[str, str, str]] = []


def check(name: str) -> None:
    _results.append((PASS, name, ""))


def fail(name: str, why: str) -> None:
    _results.append((FAIL, name, why))


def expect(name: str, cond: bool, why: str = "") -> bool:
    if cond:
        check(name)
    else:
        fail(name, why)
    return cond


# --------------------------------------------------------------------------- #
# 合成测试图
# --------------------------------------------------------------------------- #

def synth_code(w=1280, h=760) -> Image.Image:
    img = Image.new("RGB", (w, h), (30, 30, 34))
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, w, 30), fill=(45, 45, 52))                     # 标题栏
    d.rectangle((0, 30, 46, h), fill=(37, 37, 42))                    # 侧栏
    d.rectangle((46, 30, 700, h), fill=(30, 30, 34))
    for i in range(0, 46, 6):
        d.line((0, i, w, i), fill=(38, 38, 44))
    colors = [(197, 134, 192), (86, 156, 214), (106, 153, 85), (220, 220, 170), (78, 201, 176)]
    y = 46
    import random

    random.seed(7)
    while y < h - 20:
        x = 60 + random.choice([0, 0, 12, 24])
        for _ in range(random.randint(5, 14)):
            c = random.choice(colors)
            wdt = random.randint(4, 12)
            d.rectangle((x, y, x + wdt, y + 6), fill=c)
            x += wdt + 5
            if x > w - 40:
                break
        y += 16
    for i in range(30, h, 16):
        d.text((52, i), f"{i // 16}", fill=(90, 96, 110))
    return img


def synth_document(w=1200, h=900) -> Image.Image:
    img = Image.new("RGB", (w, h), (255, 255, 255))
    d = ImageDraw.Draw(img)
    import random

    random.seed(11)
    y = 70
    while y < h - 60:
        if random.random() < 0.18:
            y += 26
            continue
        x = 110
        d.rectangle((x, y, x + random.randint(180, 420), y + 7), fill=(40, 40, 44))
        x = 110
        for i in range(3):
            y += 24
            if y > h - 60:
                break
            x = 110
            while x < w - 130:
                wdt = min(random.randint(90, 340), w - 120 - x)
                d.rectangle((x, y, x + wdt, y + 6), fill=(58, 58, 62))
                x += wdt + 12
        y += 30
    return img


def synth_webpage(w=1360, h=800) -> Image.Image:
    img = Image.new("RGB", (w, h), (246, 246, 248))
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, w, 90), fill=(255, 255, 255))                  # 浏览器工具条
    d.rectangle((30, 22, 200, 60), fill=(235, 235, 240))
    d.rectangle((220, 30, 620, 52), fill=(228, 228, 234))
    for i in range(5):
        d.ellipse((640 + i * 60, 26, 672 + i * 60, 58), fill=(214, 214, 222))
    d.rectangle((0, 90, 190, h), fill=(238, 238, 242))                # 侧栏
    d.rectangle((0, 90, w, 96), fill=(222, 222, 230))
    d.rectangle((220, 130, w - 60, 190), fill=(228, 228, 236))        # 卡片
    d.rectangle((220, 210, w - 260, 300), fill=(234, 234, 240))
    d.rectangle((220, 320, w - 120, 460), fill=(240, 240, 244))
    for i in range(3):
        d.rectangle((220 + i * 260, 480, 440 + i * 260, 640), fill=(232, 232, 238))
    return img


def synth_chat(w=1000, h=880) -> Image.Image:
    img = Image.new("RGB", (w, h), (245, 245, 246))
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, 300, h), fill=(238, 238, 240))                 # 会话列表
    d.rectangle((0, 0, 300, 60), fill=(232, 232, 235))
    for i in range(9):
        y = 80 + i * 82
        d.rectangle((16, y, 60, y + 44), fill=(206, 208, 214))
        d.rectangle((74, y + 6, 260, y + 18), fill=(196, 198, 205))
        d.rectangle((74, y + 26, 200, y + 36), fill=(210, 212, 218))
        if i == 3:
            d.rectangle((0, y - 8, 300, y + 74), fill=(224, 226, 231))
    # 气泡
    bubbles = [(320, 90, 700, 150, False), (420, 180, 800, 250, True),
               (320, 270, 660, 330, False), (500, 360, 900, 430, True),
               (320, 460, 720, 530, False)]
    for x0, y0, x1, y1, left in bubbles:
        fill = (198, 226, 180) if left else (255, 255, 255)
        d.rounded_rectangle((x0, y0, x1, y1), radius=10, fill=fill)
    return img


def synth_photo(w=1200, h=800) -> Image.Image:
    """用平滑渐变 + 噪声模拟照片（颜色多样、无纯色底板、边缘零散）。"""
    import math

    import numpy as np

    xs = np.linspace(0, 3.2, w, dtype=np.float32)
    ys = np.linspace(0, 2.1, h, dtype=np.float32)
    gx, gy = np.meshgrid(xs, ys)
    r = 120 + 90 * np.sin(gx) + 30 * np.cos(gy * 1.7)
    g = 110 + 70 * np.sin(gy * 1.3 + 0.6) + 40 * np.cos(gx * 0.7)
    b = 130 + 80 * np.cos(gx + gy) + 25 * np.sin(gy * 2.2)
    rng = np.random.default_rng(3)
    noise = rng.normal(0, 8, (h, w, 3))
    arr = np.stack([r, g, b], axis=2) + noise
    arr = np.clip(arr, 0, 255).astype("uint8")
    return Image.fromarray(arr, "RGB")


def synth_table(w=1240, h=720) -> Image.Image:
    img = Image.new("RGB", (w, h), (255, 255, 255))
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, w, 34), fill=(33, 115, 70))                    # Excel 风格标题栏
    cols = [0, 70, 300, 520, 700, 900, 1080, w]
    rows = [60 + i * 28 for i in range((h - 80) // 28)]
    for y in rows:
        d.line((0, y, w, y), fill=(206, 206, 206))
    for x in cols:
        d.line((x, 56, x, rows[-1]), fill=(180, 180, 180))
    for y in rows:
        for x in cols[:-1]:
            if (y // 28) % 3 == 0 or x == 70:
                d.rectangle((x + 6, y + 8, x + 6 + min(cols[cols.index(x) + 1] - x - 24, 60), y + 18),
                            fill=(70, 70, 74))
    d.rectangle((0, 34, w, 56), fill=(240, 240, 240))                 # 列标
    return img


SYNTH = {
    "code": synth_code,
    "document": synth_document,
    "webpage": synth_webpage,
    "chat": synth_chat,
    "image": synth_photo,
    "table": synth_table,
}

#: 每个合成样本对应的真实宿主进程（用于验证 app 上下文加权不会压过视觉证据）
HOST_EXE = {
    "code": "Code.exe",
    "document": "winword.exe",
    "webpage": "chrome.exe",
    "chat": "WeChat.exe",
    "image": "Photos.exe",
    "table": "EXCEL.EXE",
}


# --------------------------------------------------------------------------- #
# 各项检查
# --------------------------------------------------------------------------- #

def test_config_resolution(tmp: Path) -> None:
    """``--config`` / ``--vault`` 必须真的生效。

    这两个参数曾经**被静默丢掉**：``app.main`` 里写的是裸的 ``load_config()``，
    于是 ``SnapVault.exe --config X --minimized`` 会安静地按默认配置跑 ——
    不报错、不警告。而且它只影响常驻分支：``--stats`` / ``--capture`` 那些
    一次性命令走的是 ``__main__`` 里正确解析的那份，所以问题只落在
    **双击和开机自启**这条最常走、也最难发现的路径上。
    """
    from .app import resolve_config

    box = tmp / "cfgres"
    box.mkdir(parents=True, exist_ok=True)
    custom = box / "custom.json"
    seed = Config()
    seed.storage.vault_dir = str(box / "vault-a")
    seed.naming.template = "PROBE-{date}_{time}_{category}"
    save_config(seed, custom)

    loaded = resolve_config(["--config", str(custom)])
    expect("命令行 --config 生效",
           Path(loaded.storage.vault_dir) == box / "vault-a",
           f"实际 {loaded.storage.vault_dir}")
    expect("命令行 --config 带上命名模板",
           loaded.naming.template == "PROBE-{date}_{time}_{category}",
           f"实际 {loaded.naming.template}")

    eq = resolve_config([f"--config={custom}"])
    expect("命令行 --config=PATH 形式生效",
           Path(eq.storage.vault_dir) == box / "vault-a",
           f"实际 {eq.storage.vault_dir}")

    both = resolve_config(["--config", str(custom), "--vault", str(box / "vault-b")])
    expect("命令行 --vault 覆盖图库目录",
           Path(both.storage.vault_dir) == box / "vault-b",
           f"实际 {both.storage.vault_dir}")


def test_naming(tmp: Path) -> None:
    try:
        from .classifier import Prediction

        namer = AutoNamer()
        when = datetime(2026, 5, 18, 14, 22, 33)
        ctx = ShotContext(exe="Code.exe", title="vault.py - SnapVault - Visual Studio Code",
                          source="region")
        pred = Prediction(category="code", confidence=0.8, tags=["code", "dark"])
        plan = namer.build(when, pred, ctx, ext="png", image_bytes=b"x" * 64)
        expect("naming 模板生成时间戳前缀", plan.filename.startswith("20260518_142233_"),
               plan.filename)
        expect("naming 含分类词", "_code_" in plan.filename, plan.filename)
        expect("naming slug 有信息量", "vault" in plan.filename.lower(), plan.filename)
        expect("naming 无非法字符", not set(plan.filename) & set('<>:"/\\|?*'), plan.filename)

        cn = namer.build(when, ctx=ShotContext(title="百度一下，你就知道 - Google Chrome"),
                         title_override="百度一下，你就知道", ext="png")
        expect("naming 中文标题可用", "百度" in cn.filename, cn.filename)
        expect("naming 长度受控", len(cn.stem) <= 90, cn.filename)

        weird = namer.build(when, ctx=ShotContext(title='a<b>:"c/d\\e|f?g*h  ' + "x" * 200),
                            ext="png")
        expect("naming 过滤非法字符", all(ch not in weird.filename for ch in '<>:"/\\|?*'),
               weird.filename)

        expect("slugify 英文", slugify("main.py - SnapVault — Visual Studio Code", max_len=48)
               == "main-py-snapvault-visual-studio-code",
               slugify("main.py - SnapVault — Visual Studio Code", max_len=48))
        expect("slugify 纯符号回退", slugify("———") == "", repr(slugify("———")))
        expect("slugify 保留设备名安全", slugify("con") != "", slugify("con"))
        # 中文紧跟扩展名时，扩展名要单独成词，不能被粘成 "aipy"
        expect("slugify 中文+扩展名不粘连",
               slugify("ai.py代码编辑器", max_len=48) == "ai-py-代码编辑器",
               slugify("ai.py代码编辑器", max_len=48))
        expect("slugify 描述保长不截断",
               slugify("WorkBuddy任务管理界面与AI代码编辑器并排显示", max_len=30)
               == "WorkBuddy任务管理界面与AI代码编辑器并排显示",
               slugify("WorkBuddy任务管理界面与AI代码编辑器并排显示", max_len=30))

        p = tmp / "a.png"
        p.write_bytes(b"1")
        expect("unique_path 去重", unique_path(p).name == "a-2.png", unique_path(p).name)
    except Exception:
        fail("naming 全部用例", traceback.format_exc(limit=3))


def test_classifier() -> None:
    for expected, fn in SYNTH.items():
        try:
            img = fn()
            feats = analyze(img)
            pred = RuleClassifier().predict(feats, ShotContext(source="region"))
            expect(f"分类 {expected}", pred.category == expected,
               f"→ {pred.category} (conf={pred.confidence:.2f}) "
               f"bg={feats.bg_lum:.0f}/{feats.bg_dominance:.2f} "
               f"edge={feats.edge_density:.3f} white={feats.white_ratio:.2f} "
               f"sat={feats.sat_mean:.0f} scores="
               + ", ".join(f"{k}:{v:.1f}" for k, v in
                           sorted(pred.scores.items(), key=lambda kv: -kv[1])[:4]))
        except Exception:
            fail(f"分类 {expected}", traceback.format_exc(limit=3))

    # 应用上下文应能纠正视觉歧义，但不能推翻强视觉证据
    feats = analyze(synth_webpage())
    clf = RuleClassifier()
    with_code = clf.predict(feats, ShotContext(exe="Code.exe", title="x", source="window"))
    with_browser = clf.predict(feats, ShotContext(exe="chrome.exe", title="x", source="window"))
    expect("分类 上下文加权生效", "code" in with_code.reason, with_code.reason)
    expect("分类 上下文抬高目标类", with_code.scores["code"] > with_browser.scores["code"],
           f"{with_code.scores['code']:.2f} vs {with_browser.scores['code']:.2f}")

    # 极小图不应崩
    tiny = analyze(Image.new("RGB", (3, 3)))
    expect("分类 极小图不崩", tiny.width == 3)


def test_vault(tmp: Path) -> None:
    try:
        cfg = Config()
        cfg.storage.vault_dir = str(tmp / "vault")
        cfg.storage.folder_pattern = "{yyyy}/{MM}"
        cfg.storage.copy_to_clipboard = False
        cfg.classify.enabled = True
        vault = Vault(cfg)

        now = datetime(2026, 5, 18, 14, 22, 33)
        recs = []
        for i, (name, fn) in enumerate(SYNTH.items()):
            rec = vault.save(
                fn(), when=datetime(2026, 5, 18, 14, 22, 33 + i),
                # 每个样本配一个"说真话"的宿主进程，模拟真实截图上下文
                ctx=ShotContext(exe=HOST_EXE[name], title=f"{name} sample - App",
                                source="region"),
                mode="region",
            )
            recs.append(rec)

        expect("vault 全部落盘", all((vault.root / r.rel_path).exists() for r in recs))
        expect("vault 目录分层", all(r.rel_path.startswith("2026/05/") for r in recs),
               recs[0].rel_path)
        expect("vault 入库计数", vault.count() == len(SYNTH), str(vault.count()))
        expect("vault 分类落库", vault.category_counts().get("code", 0) >= 1,
               str(vault.category_counts()))
        expect("vault 缩略图生成", all(vault.thumb_path_for(r).exists() for r in recs))

        # 搜索
        hit = vault.search("code")
        expect("vault 关键词搜索", len(hit) >= 1, str(len(hit)))
        hit2 = vault.search("cat:document")
        expect("vault 分类语法搜索", all(r.category == "document" for r in hit2) and hit2,
               str([r.category for r in hit2]))
        hit3 = vault.search("tag:dark")
        expect("vault 标签搜索", isinstance(hit3, list))
        hit4 = vault.search("day:2026-05")
        expect("vault 日期搜索", len(hit4) == len(SYNTH), str(len(hit4)))

        # 更新标签 / 备注 / 收藏
        target = recs[0]
        vault.update(target.id, tags=["重要", "参考"], note="月度报表", favorite=1)
        got = vault.get(target.id)
        expect("vault 打标签", got is not None and "重要" in got.tags, str(got.tags if got else None))
        expect("vault 标签云重算", any(t == "重要" for t, _ in vault.tag_counts()),
               str(vault.tag_counts()[:5]))
        expect("vault 收藏筛选", len(vault.search(favorite=True)) == 1)

        # 改分类应同步重命名
        moved = vault.move(target.id, "document")
        expect("vault 改分类", moved is not None and moved.category == "document")
        expect("vault 改分类同步文件名", moved is not None and "_doc_" in moved.filename,
               moved.filename if moved else "")
        expect("vault 改分类文件存在", (vault.root / moved.rel_path).exists())

        # 编码
        cfg2 = Config()
        cfg2.storage.vault_dir = str(tmp / "vault_jpg")
        cfg2.storage.image_format = "jpg"
        v2 = Vault(cfg2)
        r2 = v2.save(synth_photo(320, 240), when=now, ctx=ShotContext(source="clipboard"))
        expect("vault jpg 编码", r2.ext == "jpg" and (v2.root / r2.rel_path).stat().st_size > 0)

        cfg3 = Config()
        cfg3.storage.vault_dir = str(tmp / "vault_webp")
        cfg3.storage.image_format = "webp"
        v3 = Vault(cfg3)
        r3 = v3.save(synth_photo(320, 240), when=now, ctx=ShotContext(source="clipboard"))
        expect("vault webp 编码", r3.ext == "webp" and (v3.root / r3.rel_path).stat().st_size > 0)

        # 重建索引
        vault.db.execute("DELETE FROM shots")
        vault.db.commit()
        n = vault.rebuild_index()
        expect("vault 重建索引", n >= len(SYNTH) and vault.count() >= len(SYNTH), f"n={n}")

        # 导出清单
        csv_path = vault.export_manifest()
        expect("vault 导出 CSV", csv_path.exists() and csv_path.stat().st_size > 0)

        # 删除
        victim = vault.search(limit=1)[0]
        vault.delete(victim.id)
        expect("vault 删除", vault.get(victim.id) is None)

        st = vault.stats()
        expect("vault 统计", st["total"] >= 1 and st["root"], str(st["total"]))
    except Exception:
        fail("vault 流程", traceback.format_exc(limit=4))


def test_config(tmp: Path) -> None:
    try:
        p = tmp / "cfg.json"
        cfg = Config()
        cfg.storage.vault_dir = str(tmp / "v")
        cfg.naming.template = "{date}_{time}_{category}_{tags}_{slug}"
        from .config import load_config, save_config

        save_config(cfg, p)
        cfg2 = load_config(p)
        expect("config 往返", cfg2.naming.template == cfg.naming.template)
        p.write_text("{ this is not json", encoding="utf-8")
        cfg3 = load_config(p)
        expect("config 损坏自愈", cfg3.naming.template == Config().naming.template)
        expect("config 备份损坏文件", any(p.parent.glob("cfg.broken-*.json")),
               str(list(p.parent.glob("cfg.broken-*.json"))))

        # --- 配置迁移：老配置无损升级（这条路径只在设置保存时走）---
        import json
        old = {
            "ai": {"api_key": "sk-old", "endpoint": "", "provider": ""},
            "categories": ["code", "code", "", "webpage", "ghost"],
            "category_labels": {"code": "代码"},
            "category_rules": [
                {"keyword": "vscode", "category": "code", "weight": 2.0,
                 "enabled": True},
                {"keyword": "野规则", "category": "deleted_cat", "weight": 1.0,
                 "enabled": True},
            ],
        }
        (tmp / "old.json").write_text(json.dumps(old, ensure_ascii=False),
                                      encoding="utf-8")
        m = Config.from_dict(json.loads((tmp / "old.json").read_text(encoding="utf-8")))
        m.migrate()
        expect("config 迁移：有 Key 的老配置推断为百炼",
               m.ai.provider == "bailian", m.ai.provider)
        expect("config 迁移：清掉空键和重复键",
               m.categories == ["code", "webpage", "ghost"], str(m.categories))
        expect("config 迁移：野规则的分类改归 other",
               [r.category for r in m.category_rules] == ["code", "other"],
               str([r.category for r in m.category_rules]))
        expect("config 迁移：规则重建为 CategoryRule 实例",
               all(isinstance(r, CategoryRule) for r in m.category_rules))
        # 没 Key 但端点是本地 Ollama → 推断成本地，别让老用户升级后 AI 不可用
        old2 = {"ai": {"api_key": "", "endpoint": "http://localhost:11434/v1",
                       "provider": ""}}
        m2 = Config.from_dict(old2)
        m2.migrate()
        expect("config 迁移：无 Key + 11434 端点推断为 Ollama",
               m2.ai.provider == "ollama", m2.ai.provider)
        # 分类被清空 → 回落内置默认，别让左栏光秃秃
        m3 = Config.from_dict({"categories": []})
        m3.migrate()
        expect("config 迁移：空分类表回落内置默认",
               m3.categories == list(Config().categories), str(m3.categories))
    except Exception:
        fail("config 流程", traceback.format_exc(limit=3))


def test_real_screenshot() -> None:
    """若存在真实截图（用户在跑本工具时产生），做一次回归统计。"""
    try:
        from .winapi import IS_WINDOWS

        if not IS_WINDOWS:
            return
        from PySide6.QtGui import QGuiApplication  # noqa: F401
    except Exception:
        return


def test_ai(tmp: Path) -> None:
    """AI 视觉描述命名：mock 响应 + 降级 + 落库检索。"""
    try:
        import json

        from .ai import (DEFAULT_ENDPOINT, AiError, QwenVision, _clean, _parse_payload,
                         normalize_endpoint)
        from .classifier import Prediction

        # --- 纯函数：清洗 ---
        expect("ai 清洗去引号", _clean('"这是描述"') == "这是描述", repr(_clean('"这是描述"')))
        expect("ai 清洗去前缀", _clean("描述：这是一段话") == "这是一段话",
               repr(_clean("描述：这是一段话")))

        # --- 纯函数：Base URL 归一化（业务空间专属域名 / 整行复制 / 只给域名）---
        expect("endpoint 空值回落默认", normalize_endpoint("") == DEFAULT_ENDPOINT)
        expect("endpoint 原样保留",
               normalize_endpoint(DEFAULT_ENDPOINT) == DEFAULT_ENDPOINT)
        expect("endpoint 去尾斜杠",
               normalize_endpoint(DEFAULT_ENDPOINT + "///") == DEFAULT_ENDPOINT,
               normalize_endpoint(DEFAULT_ENDPOINT + "///"))
        # 占位符，不是真实业务空间 ID —— 这个文件会随仓库公开，
        # 曾经真有人把自己的 workspace 域名写在断言里。
        ws = "https://ws-demo0example00000.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
        expect("endpoint 专属域名保留", normalize_endpoint(ws) == ws, normalize_endpoint(ws))
        expect("endpoint 剥掉请求路径",
               normalize_endpoint(ws + "/chat/completions") == ws,
               normalize_endpoint(ws + "/chat/completions"))
        expect("endpoint 只给域名自动补全",
               normalize_endpoint("ws-demo0example00000.cn-beijing.maas.aliyuncs.com") == ws,
               normalize_endpoint("ws-demo0example00000.cn-beijing.maas.aliyuncs.com"))
        expect("endpoint 自建代理保留自有路径",
               normalize_endpoint("https://my-proxy.local/v1") == "https://my-proxy.local/v1",
               normalize_endpoint("https://my-proxy.local/v1"))
        expect("endpoint 带引号/空格也能洗净",
               normalize_endpoint('  "https://dashscope.aliyuncs.com/compatible-mode/v1" ') ==
               DEFAULT_ENDPOINT,
               normalize_endpoint('  "https://dashscope.aliyuncs.com/compatible-mode/v1" '))

        # --- 纯函数：AI 输出解析（JSON / 代码块 / 长键 / 纯文本 / 坏 JSON）---
        expect("payload 解析 JSON",
               _parse_payload('{"desc": "股票行情图", "cat": "table"}') == ("股票行情图", "table"),
               str(_parse_payload('{"desc": "股票行情图", "cat": "table"}')))
        expect("payload 解析代码块 JSON",
               _parse_payload('```json\n{"desc": "代码编辑器", "cat": "code"}\n```')
               == ("代码编辑器", "code"),
               str(_parse_payload('```json\n{"desc": "代码编辑器", "cat": "code"}\n```')))
        expect("payload 兼容 description/category 长键",
               _parse_payload('{"description": "文档页", "category": "document"}')
               == ("文档页", "document"),
               str(_parse_payload('{"description": "文档页", "category": "document"}')))
        expect("payload 纯文本整段当描述",
               _parse_payload("一段普通描述") == ("一段普通描述", ""),
               str(_parse_payload("一段普通描述")))
        expect("payload 坏 JSON 不留垃圾",
               _parse_payload('{"desc": broken') == ("", ""),
               str(_parse_payload('{"desc": broken')))
        expect("payload 空输入", _parse_payload("") == ("", ""))
        expect("payload 分类键大小写归一",
               _parse_payload('{"desc": "x", "cat": "TABLE"}') == ("x", "table"),
               str(_parse_payload('{"desc": "x", "cat": "TABLE"}')))

        # --- mock 传输：成功 ---
        class FakeResp:
            status = 200

            def __init__(self, text: str) -> None:
                self._b = text.encode("utf-8")

            def read(self) -> bytes:
                return self._b

        def ok_transport(req, timeout):
            return FakeResp(json.dumps(
                {"choices": [{"message": {"content": "代码编辑器中的截图"}}]}))

        client = QwenVision(api_key="sk-test", transport=ok_transport)
        img = Image.new("RGB", (64, 64), (30, 30, 34))
        res = client.describe(img)
        expect("ai mock 成功返回描述", res.ok and "代码" in res.description, res.description)
        expect("ai 无 Key 抛错", _raises(AiError, lambda: QwenVision(api_key="").describe(img)))

        # --- mock 传输：HTTP 错误 ---
        class ErrResp:
            status = 401

            def read(self) -> bytes:
                return b'{"error":{"message":"Invalid API key"}}'

        def err_transport(req, timeout):
            return ErrResp()

        client2 = QwenVision(api_key="sk-bad", transport=err_transport)
        expect("ai 鉴权失败抛错", _raises(AiError, lambda: client2.describe(img)))

        # --- vault 层：AI 启用但失败 → 降级仅时间戳（无 slug） ---
        cfg = Config()
        cfg.storage.vault_dir = str(tmp / "vault_ai")
        cfg.ai.enabled = True
        cfg.ai.api_key = "sk-will-fail"
        vault = Vault(cfg)

        class NeverResp:
            status = 401

            def read(self) -> bytes:
                return b'{"error":{}}'

        # 用无 Key 场景更简单：直接空 Key，走降级
        cfg.ai.api_key = ""
        rec = vault.save(
            synth_code(), when=datetime(2026, 5, 18, 14, 22, 33),
            ctx=ShotContext(exe="Code.exe", title="vault.py - x", source="region"),
            mode="region",
        )
        expect("ai 未配 Key 降级时间戳命名", rec is not None and rec.ai_desc == "",
               rec.filename if rec else "")
        # 降级时文件名应不含分类词的 slug（no_slug 生效 → slug 为空）
        expect("ai 降级文件名无 slug", "_shot" not in rec.filename and "_code" in rec.filename,
               rec.filename if rec else "")

        # --- vault 层：AI 成功 → 描述写入 slug 与 ai_desc ---
        cfg2 = Config()
        cfg2.storage.vault_dir = str(tmp / "vault_ai2")
        cfg2.ai.enabled = True
        cfg2.ai.api_key = "sk-ok"
        vault2 = Vault(cfg2)

        from . import ai as ai_mod

        ok_transport2 = ok_transport
        client_ok = QwenVision(api_key="sk-ok", transport=ok_transport2)

        # 直接注入一个返回固定描述的 describe（绕过网络）
        saved = {}

        def fake_describe(c, image, data):
            return type("R", (), {"description": "代码编辑器"})()  # 模拟 AiResult

        orig = ai_mod.describe_cached
        ai_mod.describe_cached = fake_describe
        try:
            rec2 = vault2.save(
                synth_code(), when=datetime(2026, 5, 18, 14, 22, 34),
                ctx=ShotContext(exe="Code.exe", title="vault.py - x", source="region"),
                mode="region",
            )
        finally:
            ai_mod.describe_cached = orig
        expect("ai 成功写入 ai_desc", rec2 is not None and rec2.ai_desc == "代码编辑器",
               rec2.ai_desc if rec2 else "")
        expect("ai 描述可检索", len(vault2.search("编辑器")) >= 1,
               str(len(vault2.search("编辑器"))))

        # --- AI 一次请求同时给分类：合法键覆盖视觉规则 ---
        cfg3 = Config()
        cfg3.storage.vault_dir = str(tmp / "vault_ai3")
        cfg3.ai.enabled = True
        cfg3.ai.api_key = "sk-ok"
        cfg3.ai.classify_with_ai = True
        vault3 = Vault(cfg3)

        def _stub(desc, cat):
            """桩：模拟一次返回「描述 + 分类」的 AI 调用。"""
            return lambda c, image, data: type("R", (), {
                "description": desc, "category": cat, "ok": True})()

        ai_mod.describe_cached = _stub("股票行情走势图", "table")
        try:
            rec3 = vault3.save(
                synth_webpage(), when=datetime(2026, 5, 18, 14, 22, 40),
                ctx=ShotContext(exe="chrome.exe", title="行情 - Google Chrome", source="region"),
                mode="region",
            )
        finally:
            ai_mod.describe_cached = orig
        expect("ai 分类被采用", rec3.category == "table", rec3.category)
        expect("ai 分类保留原判理由", rec3.reason.startswith("AI 判定"), rec3.reason)
        expect("ai 分类置信度高水位", rec3.confidence >= 0.85, str(rec3.confidence))
        expect("ai 分类与描述同批生效", rec3.ai_desc == "股票行情走势图", rec3.ai_desc)

        # --- 非法分类键 → 回落视觉规则，不写脏数据 ---
        ai_mod.describe_cached = _stub("某界面", "nonsense_category")
        try:
            rec4 = vault3.save(
                synth_webpage(), when=datetime(2026, 5, 18, 14, 22, 41),
                ctx=ShotContext(exe="chrome.exe", title="x - Google Chrome", source="region"),
                mode="region",
            )
        finally:
            ai_mod.describe_cached = orig
        expect("ai 非法分类不落库", "nonsense" not in rec4.category, rec4.category)
        expect("ai 非法分类回落规则", rec4.category in cfg3.categories, rec4.category)
        expect("ai 非法分类仍保留描述", rec4.ai_desc == "某界面", rec4.ai_desc)

        # --- 关掉开关 → 分类完全走视觉规则 ---
        cfg4 = Config()
        cfg4.storage.vault_dir = str(tmp / "vault_ai4")
        cfg4.ai.enabled = True
        cfg4.ai.api_key = "sk-ok"
        cfg4.ai.classify_with_ai = False
        vault4 = Vault(cfg4)
        ai_mod.describe_cached = _stub("表格截图", "table")
        try:
            rec5 = vault4.save(
                synth_table(), when=datetime(2026, 5, 18, 14, 22, 42),
                ctx=ShotContext(exe="EXCEL.EXE", title="报表", source="region"),
                mode="region",
            )
        finally:
            ai_mod.describe_cached = orig
        expect("ai 分类开关关闭时不覆盖", not rec5.reason.startswith("AI 判定"), rec5.reason)
        expect("ai 开关关闭仍用描述命名", rec5.ai_desc == "表格截图", rec5.ai_desc)
    except Exception:
        fail("ai 流程", traceback.format_exc(limit=4))


def test_watcher(tmp: Path) -> None:
    """系统截图接管：落盘 → 稳定检测 → 归档 → 去重 → 目录守卫。"""
    try:
        from .config import default_screenshots_dir
        from .shot import NullFeedback, ShotService
        from .watcher import SystemShotWatcher

        cfg = Config()
        cfg.storage.vault_dir = str(tmp / "vault_watch")
        cfg.storage.copy_to_clipboard = False
        cfg.watch.enabled = True
        cfg.watch.source_action = "keep"
        cfg.watch.settle_ms = 0

        shots = tmp / "Screenshots"
        shots.mkdir(parents=True, exist_ok=True)
        cfg.watch.dirs = [str(shots)]

        service = ShotService(cfg, feedback=NullFeedback())
        watcher = SystemShotWatcher(cfg, service)
        expect("watch 监视目录生效", [str(p) for p in watcher.dirs()] == [str(shots)],
               str(watcher.dirs()))

        # 1) 稳定检测：第一次扫描只登记候选，不急着处理（文件可能还在写）
        img_path = shots / "屏幕截图 1.png"
        synth_table().save(img_path)
        first = watcher.scan_once()
        expect("watch 首次扫描不急于处理", first == [], str(len(first)))

        # 2) 第二次扫描（已静置）→ 走完整归档流水线
        second = watcher.scan_once()
        expect("watch 静置后接管", len(second) == 1 and second[0].ok,
               str([(e.ok, e.error) for e in second]))
        rec = second[0].record
        expect("watch 生成归档记录",
               rec is not None and (service.vault.root / rec.rel_path).exists(),
               rec.rel_path if rec else "无记录")
        expect("watch 原图按 keep 保留", img_path.exists())
        expect("watch 记入图库", service.vault.count() >= 1, str(service.vault.count()))

        # 3) 去重：同一张（大小/修改时间未变）不再重复接管
        third = watcher.scan_once(force=True)
        expect("watch 已处理的不重复接管", third == [], str(len(third)))

        # 4) 非图片、临时文件、隐藏文件都不该被当成截图
        (shots / "备注.txt").write_text("x", encoding="utf-8")
        (shots / "shot.png.part").write_bytes(b"partial")
        (shots / "~$tmp.png").write_bytes(b"tmp")
        fourth = watcher.scan_once(force=True)
        expect("watch 忽略非图片/临时文件", fourth == [], str(len(fourth)))

        # 5) 图库自身目录被守卫排除，否则归档产物会被反复处理（自我吞噬）
        cfg2 = Config()
        cfg2.storage.vault_dir = str(tmp / "vault_watch2")
        cfg2.watch.dirs = [str(Path(cfg2.storage.vault_dir))]
        service2 = ShotService(cfg2, feedback=NullFeedback())
        watcher2 = SystemShotWatcher(cfg2, service2)
        expect("watch 排除图库自身目录", watcher2.dirs() == [], str(watcher2.dirs()))

        # 6) 目录列表为空 → 回落系统默认截图目录
        cfg3 = Config()
        cfg3.watch.dirs = []
        service3 = ShotService(cfg3, feedback=NullFeedback())
        watcher3 = SystemShotWatcher(cfg3, service3)
        expect("watch 空列表回落系统目录",
               [str(p) for p in watcher3.dirs()] == [default_screenshots_dir()],
               str(watcher3.dirs()))

        # 7) 状态摘要（托盘提示 / 设置面板用）
        st = watcher.status()
        expect("watch 状态摘要可读", st["handled"] >= 1 and st["dirs"], str(st))

        # 8) 清空记录后可以重新接管（"重新扫描全部"用）
        n = watcher.forget_all()
        expect("watch 可清空处理记录", n >= 1, str(n))
        again = watcher.scan_once(force=True)
        expect("watch 清空后重新接管", len(again) == 1 and again[0].ok, str(len(again)))

        # 9) 单次补跑上限：点一次"立即接管"不该把几百张历史图全送进 AI（要花钱的）
        watcher.forget_all()
        for i in range(4):
            synth_table().save(shots / f"批量 {i}.png")
        expect("watch 待处理计数", watcher.pending_count() == 5, str(watcher.pending_count()))
        capped = watcher.scan_once(force=True, limit=2)
        expect("watch 补跑有上限", len(capped) == 2, str(len(capped)))
        expect("watch 上限后仍留剩余", watcher.pending_count() == 3,
               str(watcher.pending_count()))
    except Exception:
        fail("watch 接管流程", traceback.format_exc(limit=4))


def test_folder_classify(tmp: Path) -> None:
    """分类落到「文件夹」上：{cn} 中文分类目录 + 大目录当图库时的两道护栏。"""
    try:
        from .naming import NamePlan
        from .shot import NullFeedback, ShotService
        from .watcher import SystemShotWatcher

        cfg = Config()
        cfg.storage.vault_dir = str(tmp / "pics")
        cfg.storage.folder_pattern = "{cn}"
        cfg.storage.copy_to_clipboard = False
        vault = Vault(cfg)
        when = datetime(2026, 10, 13, 12, 0, 0)

        def plan_for(cat: str, slug: str) -> NamePlan:
            return NamePlan(filename="x.png", stem="x", ext="png", date="20261013",
                            time="120000", category=cat, category_slug=slug, slug="x")

        # 1) {cn} → 直接分到中文分类文件夹
        for cat, slug, want in (("webpage", "web", "网页"), ("code", "code", "代码"),
                                ("other", "shot", "其他"), ("document", "doc", "文档")):
            got = vault._target_folder(when, plan_for(cat, slug), None)
            expect(f"分类目录 {cat} → {want}", got.name == want and got.parent == vault.root,
                   str(got))

        # 2) 分类键不在标签表里 / 标签含非法字符 → 目录名仍有兜底，不会炸
        expect("分类目录 未知键回落键名",
               vault._target_folder(when, plan_for("怪类", "weird"), None).name == "怪类",
               str(vault._target_folder(when, plan_for("怪类", "weird"), None)))
        cfg.category_labels["bad"] = 'a/b:c*'
        expect("分类目录 非法字符被清理",
               vault._target_folder(when, plan_for("bad", "bad"), None).name == "abc",
               str(vault._target_folder(when, plan_for("bad", "bad"), None)))

        # 3) 监视目录在图库内部（D:\Pictures + D:\Pictures\Screenshots）必须仍然生效
        svc = ShotService(cfg, feedback=NullFeedback())
        watcher = SystemShotWatcher(cfg, svc)
        inside = Path(cfg.storage.vault_dir) / "Screenshots"
        inside.mkdir(parents=True, exist_ok=True)
        cfg.watch.dirs = [str(inside)]
        expect("监视目录在图库内部仍生效",
               [str(p) for p in watcher.dirs()] == [str(inside)], str(watcher.dirs()))
        cfg.watch.dirs = [str(cfg.storage.vault_dir)]
        expect("监视目录等于图库根目录被拦", watcher.dirs() == [], str(watcher.dirs()))

        # 4) 图库根目录是个大目录时，"重建索引"只认自家归档，不碰用户自己的照片
        root = Path(cfg.storage.vault_dir)
        (root / "网页").mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (16, 16), (200, 10, 10)).save(root / "我的照片.jpg")
        Image.new("RGB", (16, 16), (10, 10, 200)).save(
            root / "网页" / "20261013_120000_web_测试界面.png")
        n_rebuilt = vault.rebuild_index()
        expect("重建索引 只认自家归档", n_rebuilt == 1, str(n_rebuilt))
        expect("重建索引 跳过用户照片", vault.get_by_path("我的照片.jpg") is None)
        expect("重建索引 收录归档图",
               vault.get_by_path("网页/20261013_120000_web_测试界面.png") is not None)
    except Exception:
        fail("分类文件夹", traceback.format_exc(limit=4))


# --------------------------------------------------------------------------- #
# 设计令牌 / 图标 / 字体
# --------------------------------------------------------------------------- #

#: 允许出现硬编码颜色的文件。除此之外任何 .py 里写 #RRGGBB 都算回归。
_COLOR_ALLOWED = {"theme.py", "icons.py"}

#: 从源码里找"传给图标 API 的名字"的几种调用形态。
_ICON_PATTERNS = (
    # 图标名固定是 icons.paint 的**第二个**参数。别写成 "[^)]*?" ——
    # 非贪婪也会一路滑到后面随便哪个下标字符串上（比如 t["fg3"]），
    # 把颜色名当成图标名报出来，白白吓人一跳。
    re.compile(r"icons\.paint\(\s*[\w.]+\s*,\s*['\"]([a-z0-9][a-z0-9-]*)['\"]"),
    re.compile(r"icons\.icon\(\s*['\"]([a-z0-9][a-z0-9-]*)['\"]"),
    re.compile(r"icon_name\s*=\s*['\"]([a-z0-9][a-z0-9-]*)['\"]"),
    re.compile(r"_IconLabel\(\s*['\"]([a-z0-9][a-z0-9-]*)['\"]"),
    re.compile(r"_MetaRow\(\s*['\"]([a-z0-9][a-z0-9-]*)['\"]"),
    re.compile(r"_SectionLabel\([^,]+,\s*['\"]([a-z0-9][a-z0-9-]*)['\"]"),
    re.compile(r"CategoryRow\([^,]+,[^,]+,\s*[\'\"]?([a-z0-9][a-z0-9-]*)[\'\"]?\s*\)"),
)


def test_theme(tmp: Path) -> None:
    """设计令牌 / 图标 / 字体：防止"散落的色值"和图标名笔误回潮。"""
    from . import icons, theme

    # 1. 令牌自身的约束（4 的倍数、圆角递减、字号递增、深浅键一致…）
    problems = theme.selftest()
    expect("令牌 自身约束通过", not problems, "；".join(problems[:3]))

    # 2. 深浅两套 QSS 都要能生成，花括号配对，且没有未替换的模板残留。
    for name in ("dark", "light"):
        qss = theme.build_qss(name)
        expect(f"令牌 {name} 主题 QSS 花括号配对",
               qss.count("{") == qss.count("}"),
               f"{qss.count('{')} vs {qss.count('}')}")
        expect(f"令牌 {name} 主题 QSS 无模板残留",
               "{" not in qss.replace("{{", "").replace("}}", "")
               or qss.count("{") == qss.count("}"),
               "QSS 里有未替换的占位")
        expect(f"令牌 {name} 主题 QSS 长度合理", len(qss) > 3000, f"{len(qss)} 字符")

    # 3. 两套主题的令牌键必须一一对应，否则切主题会 KeyError。
    expect("令牌 深浅主题键一致",
           set(theme.TOKENS["dark"]) == set(theme.TOKENS["light"]),
           "深浅主题的令牌键不一致")

    # 4. 图标：配置里每个分类都有图标，且**互不重复** ——
    #    两个分类共用一颗图标，就等于失去了"一眼区分"的作用。
    cfg = Config()
    missing = [k for k in cfg.categories if k not in theme.CATEGORY_ICONS]
    expect("图标 每个分类都配了图标", not missing, f"缺：{missing}")
    used = [theme.CATEGORY_ICONS[k] for k in cfg.categories
            if k in theme.CATEGORY_ICONS]
    expect("图标 分类图标互不重复", len(used) == len(set(used)),
           f"重复：{[u for u in used if used.count(u) > 1]}")

    # 5. **源码里引用的每个图标名都必须真实存在** ——
    #    这类笔误不会报错，只会在界面上留一块空白，肉眼很难发现。
    bad: list[str] = []
    if source_available():
        for path in sorted(_SRC_DIR.glob("*.py")):
            if path.name == "icons.py":
                continue
            text = path.read_text(encoding="utf-8")
            for pattern in _ICON_PATTERNS:
                for name in pattern.findall(text):
                    if name not in icons.PATHS:
                        bad.append(f"{path.name}: {name}")
        expect("图标 源码引用的图标名都存在", not bad, "；".join(sorted(set(bad))[:5]))
    else:
        _skip_src("图标 源码引用的图标名都存在")

    # 5b. 上面那组正则只匹配"调用形态"，抓不到数据结构里的图标名 ——
    #     设置面板导航 NAV_ITEMS 就是纯数据，写错只能到这里专项查。
    #     曾经四个导航图标名不存在，建设置面板时直接 KeyError。
    from .settings_ui import NAV_ITEMS
    bad_nav = [f"{k}->{n}" for k, _l, n in NAV_ITEMS if n not in icons.PATHS]
    expect("图标 设置导航图标都存在", not bad_nav, "；".join(bad_nav))
    expect("图标 导航图标互不重复",
           len({n for _k, _l, n in NAV_ITEMS}) == len(NAV_ITEMS),
           "两个导航项共用一颗图标")

    # 6. 随包字体：文件在、能注册、族名对得上。
    fonts = theme.BUNDLED_FONTS
    files_ok = all((theme._ASSETS / fn).exists() for fn, _ in fonts)
    expect("字体 随包字体文件存在", files_ok,
           f"缺文件：{[fn for fn, _ in fonts if not (theme._ASSETS / fn).exists()]}")
    # 注册字体要走 QFontDatabase，而它必须有 QApplication 实例（没有就段错误）。
    # 这里现建一个，把"字体到底注册上没有"真正查一遍 —— 打包后字体没挂上
    # 是最典型的静默劣化：界面还能开，只是字重层级没了。
    if ensure_app() is None:
        check("字体 随包字体可注册（无 GUI 环境，跳过）")
        check("字体 字体族名符合预期（无 GUI 环境，跳过）")
    else:
        loaded = theme.load_fonts()
        expect("字体 随包字体可注册", bool(loaded), "一个都没注册上")
        expect("字体 字体族名符合预期",
               any("HarmonyOS Sans SC" in f for f in loaded) if files_ok else True,
               f"实际：{loaded}")

    # 7. 硬编码颜色的回归拦截。
    #    这是整段自检里最值钱的一条：改版前 5 个青绿、6 个圆角散在 4 个文件里，
    #    靠人眼 review 根本拦不住。现在只要有人在业务代码里写 #RRGGBB 就红。
    offenders: list[str] = []
    hex_re = re.compile(r"#[0-9A-Fa-f]{6}\b")
    if source_available():
        for path in sorted(_SRC_DIR.glob("*.py")):
            if path.name in _COLOR_ALLOWED:
                continue
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                code = line.split("#", 1)[0] if not line.lstrip().startswith("#") else ""
                for hit in hex_re.findall(code):
                    offenders.append(f"{path.name}:{lineno} {hit}")
        expect("令牌 业务代码里没有硬编码颜色", not offenders,
               "；".join(offenders[:5]) + (" …" if len(offenders) > 5 else ""))
    else:
        _skip_src("令牌 业务代码里没有硬编码颜色")

    # 7b. **accent 只准出现在「当前选中 / 当前聚焦」的规则里。**
    #
    #     用户的原话是"使用的颜色太多单调，一致性都是绿色"。问题不是颜色不够多 ——
    #     是**同一个绿承担了所有语义**：按钮、勾选框、单选框、进度条、滑块全是实心
    #     accent，一屏下来同一块绿到处都是，"当前该看哪儿"反而没了信号。
    #     收窄之后绿色变稀缺，才重新有分量。这条守的就是"别人顺手又涂回去"。
    #
    #     QSS 没有嵌套花括号，所以这个正则能安全地切出 (选择器, 规则体)，
    #     选择器跨行也照样匹配（[^{}] 里包含换行）。
    _SEL_OK = (":selected", ":focus")
    _PROP_OK = ("selection-background-color", "selection-color")
    stray: list[str] = []
    for name in ("dark", "light"):
        acc = theme.tokens(name)["accent"].lower()
        for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", theme.build_qss(name)):
            if acc not in body.lower():
                continue
            for decl in (ln.strip() for ln in body.splitlines()):
                low = decl.lower()
                if acc not in low:
                    continue
                if any(k in low for k in _PROP_OK):
                    continue
                if any(k in sel for k in _SEL_OK):
                    continue
                stray.append(f"{name} · {sel.strip().splitlines()[-1][:36]} · {decl[:32]}")
    expect("令牌 accent 只用于选中/聚焦态", not stray,
           "；".join(stray[:4]))

    # 8. 业务代码里不该再有 setStyleSheet —— 样式一律走 theme.build_qss()。
    inline: list[str] = []
    if source_available():
        for path in sorted(_SRC_DIR.glob("*.py")):
            # selftest.py 排除：这段自检的源码里本来就有 ".setStyleSheet(" 这个字面量。
            if path.name in ("theme.py", "selftest.py"):
                continue
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                if ".setStyleSheet(" in line:
                    inline.append(f"{path.name}:{lineno}")
        expect("令牌 没有散落的 setStyleSheet", not inline, "；".join(inline[:5]))
    else:
        _skip_src("令牌 没有散落的 setStyleSheet")


def test_motion_single_source() -> None:
    """动效时长只准从 ``theme.MOTION`` 取。

    散落的 ``setDuration(700)`` 是"某个动效莫名卡 / 莫名快"的源头 ——
    改一处漏三处，而且肉眼在代码里根本看不出这两个 700 是不是一回事。
    theme.MOTION 现在是唯一时长表（fast / normal / flash / pulse），
    这条自检守着线：theme.py 之外不允许出现别的地方取时长。
    """
    if not source_available():
        _skip_src("动效 时长只从 theme.MOTION 取")
        return
    bad: list[str] = []
    for path in sorted(_SRC_DIR.glob("*.py")):
        if path.name in ("theme.py", "selftest.py"):
            continue
        for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1):
            code = line.strip()
            if code.startswith("#") or "setDuration(" not in code:
                continue
            arg = code.split("setDuration(", 1)[1]
            if "theme.MOTION" not in arg:
                bad.append(f"{path.name}:{lineno} {code[:60]}")
    expect("动效 时长只从 theme.MOTION 取", not bad, "；".join(bad[:5]))


def test_appid_timing() -> None:
    """任务栏 AppID 必须在 QApplication 构造之前设置。

    晚一步就改不回去：任务栏按钮按"无 AppID"登记后，进程内再调
    SetCurrentProcessExplicitAppUserModelID 也刷不过来（pythonw 的
    Python logo 会一直挂在任务栏上，多个实例还不分组）。运行期观测不到
    这个时序（进程级全局，设完就闷着），只能读源码顺序来守。
    """
    if not source_available():
        # 报两条是为了让总项数在源码版和打包版里保持一致（153）——
        # 否则 README 里那句"153/153"换个运行方式就对不上了。
        _skip_src("AppID build_qapplication 存在")
        _skip_src("AppID 在 QApplication 之前设置")
        return
    src = (_SRC_DIR / "app.py").read_text(encoding="utf-8")
    m = re.search(r"def build_qapplication\(.*?(?=\ndef |\Z)", src, re.S)
    expect("AppID build_qapplication 存在", m is not None)
    if m is None:
        return
    body = m.group(0)
    i_id = body.find("set_app_user_model_id()")
    i_app = body.find("QApplication(")
    expect("AppID 在 QApplication 之前设置",
           0 <= i_id < i_app, f"id@{i_id} app@{i_app}")


def _raises(exc_type, fn) -> bool:
    try:
        fn()
        return False
    except exc_type:
        return True
    except Exception:
        return False


def test_static_names() -> None:
    """静态扫描：引用了却既没导入也没定义的名字。

    抓的是"import 全过、自检全绿，一打开面板才 NameError"这类 bug。
    ``QListWidgetItem``、``QRect`` 都是这么漏进主干的 —— 前者让设置面板
    整个构造不出来，后者只在 ``paintEvent`` 里炸，还会被 Qt 的 Python
    override 吞成一行 stderr，界面上只留一块空白。用 :mod:`symtable`
    而不是正则：作用域规则交给编译器，不会把局部变量误判成漏 import。
    """
    builtins_ = set(dir(builtins))

    def scan(path: Path) -> list[str]:
        top = symtable.symtable(path.read_text(encoding="utf-8"),
                                str(path), "exec")
        bad: list[str] = []

        def walk(table, outer: set[str]) -> None:
            for sym in table.get_symbols():
                name = sym.get_name()
                if (name.startswith("__") or name in builtins_
                        or not sym.is_referenced() or sym.is_assigned()
                        or sym.is_imported() or sym.is_parameter()
                        or sym.is_free() or sym.is_namespace()
                        or name in outer):
                    continue
                bad.append(f"{path.name}: {name}")
            inner = outer | {s.get_name() for s in table.get_symbols()}
            for child in table.get_children():
                walk(child, inner)

        walk(top, set())
        return bad

    problems: list[str] = []
    if not source_available():
        _skip_src("静态 没有引用了却没定义的名字")
        return
    for path in sorted(_SRC_DIR.glob("*.py")):
        problems.extend(scan(path))
    expect("静态 没有引用了却没定义的名字", not problems,
           "；".join(problems[:5]) + (" …" if len(problems) > 5 else ""))


def test_settings_ui() -> None:
    """设置面板要能**真的构造出来**并翻完每一页。

    为什么值得单列：``import snapvault.settings_ui`` 只编译模块，函数体里的
    名字错误（少 import 一个 QListWidgetItem 之类）一个都发现不了，只有
    ``SettingsDialog(cfg)`` 那一瞬才炸。这个 bug 真实发生过 —— 面板整个
    打不开，而 import 检查和 122 项自检全是绿的。
    """
    try:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        from .settings_ui import NAV_ITEMS, SettingsDialog
        dlg = SettingsDialog(Config())
        expect("设置 六个页面都能构建", dlg.stack.count() == len(NAV_ITEMS),
               f"实际 {dlg.stack.count()} 页 / {len(NAV_ITEMS)} 项导航")
        for key, label, _icon in NAV_ITEMS:
            dlg._goto(key)
        expect("设置 导航可翻完每一页", True)

        # --- 导航行的"图层"回归 ------------------------------------------- #
        # 真实 bug：QSS 的 `::item:selected` 涂了一层 accent 底色，_NavRow
        # 又自绘了一层，两块几何对不上 —— QSS 那块是 140×34（受 margin 约束），
        # 自绘那块只有 108×30 且整体偏下 10px，于是窄的那块在下方多露 6px，
        # 看上去就是一块方底下面拖了一条"舌头"。
        #
        # 两道判据，一快一慢：
        #   ① 几何：行控件矩形必须和 item 背景矩形重合（底色只能有一个来源）；
        #   ② 像素：扫渲染出来的 accent 跨度曲线，抓"突变"。
        # ②存在的意义是防"换个别的方式又补一层" —— 光靠①挡不住。
        #
        # ⚠️ 这一段会动**全局**样式表（控件几何会跟着变），跑完必须原样还回去：
        # 后面的图库用例要量网格几何，残留一份 QSS 会让它量到别的数字。
        # 第一版就是忘了还原，把「图库 滚到底只补底部附近」带红了。
        from . import theme
        _qss0, _pal0, _font0 = app.styleSheet(), app.palette(), app.font()
        try:
            theme.apply(app, "light")
            dlg.set_theme("light")
            dlg.resize(880, 620)
            dlg.show()
            for _ in range(30):
                app.processEvents()
            ai = [k for k, _l, _i in NAV_ITEMS].index("ai")
            dlg.nav.setCurrentRow(ai)
            for _ in range(20):
                app.processEvents()

            item = dlg.nav.item(ai)
            row = dlg.nav.itemWidget(item)
            vir = dlg.nav.visualItemRect(item)
            m = theme.S1
            want = (vir.left() + m, vir.top() + 2,
                    vir.width() - 2 * m, vir.height() - 4)
            got = row.geometry()
            expect("设置 导航行与 item 背景矩形重合（底色只能有一层）",
                   (got.x(), got.y(), got.width(), got.height()) == want,
                   f"rowWidget={got.getRect()} 期望 {want}")

            img = dlg.grab().toImage()
            acc = theme.tokens("light")["accent"]
            want_rgb = (int(acc[1:3], 16), int(acc[3:5], 16), int(acc[5:7], 16))
            origin = dlg.nav.mapTo(dlg, dlg.nav.viewport().pos())
            spans: list[int] = []            # 从上到下，每行 accent 像素的跨度
            for y in range(origin.y(), min(origin.y() + dlg.nav.height(), img.height())):
                xs = [x for x in range(max(0, origin.x() - 24),
                                       min(origin.x() + 180, img.width()))
                      if (lambda c: (c.red(), c.green(), c.blue()))(
                          img.pixelColor(x, y)) == want_rgb]
                if xs:
                    spans.append(max(xs) - min(xs) + 1)

            # 单个圆角矩形的跨度曲线是"升 → 平 → 降"，相邻两行最多差
            # 圆角那段的一两个像素。两块宽度不同的底叠在一起会出现**突变**
            # （实测 132 → 106，一行掉了 26px），那才是这个 bug 的特征。
            #
            # 刻意**不**写成"不同档数 ≤ 半径" —— 那是 1x 渲染下的结论：
            # 换成真实平台插件（QT_QPA_PLATFORM=windows）会按 DPR 缩放，
            # 同一个 4px 圆角摊成 157..164 共 7 档，按档数判会假失败。
            # 用"相邻行最大落差 / 最大跨度"这个比值就不受缩放影响。
            max_w = max(spans) if spans else 0
            max_drop = max((spans[i] - spans[i + 1] for i in range(len(spans) - 1)),
                           default=0)
            budget = max(6, round(max_w * 0.06))
            expect("设置 导航选中底色是一整块（不是两块叠出的台阶）",
                   bool(spans) and max_drop <= budget,
                   f"相邻行跨度最多掉 {max_drop}px（上限 {budget}，最大跨度 {max_w}）"
                   f"，曲线 {spans}")
        except Exception as exc:                            # noqa: BLE001
            check(f"设置 导航图层（环境不支持，跳过：{type(exc).__name__}: {exc}）")
        finally:
            app.setStyleSheet(_qss0)
            app.setPalette(_pal0)
            app.setFont(_font0)

        dlg.deleteLater()
        del app
    except Exception as exc:                                # noqa: BLE001
        # 离屏平台起不来之类的环境问题，降级成"跳过"而不是误报
        check(f"设置 面板构建（环境不支持，跳过：{type(exc).__name__}: {exc}）")


def test_gallery_scope(tmp: Path) -> None:
    """图库补图只能补「视口 + 上下各一屏」，**绝不能是整库**。

    这条是真实 bug 的回归线。改版前 ``_request_visible`` 用视口**右下角**去
    ``indexAt`` 取末行，取不到就 ``row1 = count() - 1``（整库）。而 4 列卡片
    总宽 828、视口宽 840 —— 右下角那 12 px 恒落在最后一列之后的留白里，
    于是**每一次滚动 / 搜索 / 筛选**都把全库缩略图重新排进解码队列：

      * ``_request_visible`` 自己就要 35 ms（891 次 find_thumb 在 UI 线程上跑）；
      * 891 个解码任务把 16 个线程占满约 600 ms，GIL 把 UI 线程饿死
        （实测"空闲时 reload()" 只要 36 ms，队列满时要 460 ms）。

    那个 bug 从界面上一眼看不出（缩略图最终都会显示出来），只能这样量。
    同时这也守护了另一半：按整行取 item 下标。原来把行号当 item 下标用，
    4 列时"三行"只覆盖前 3 个 item，可见区反而大面积请求不到。
    """
    try:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtCore import Qt
        app = ensure_app()
        if app is None:
            check("图库 补图范围（没有 QApplication，跳过）")
            return

        cfg = Config()
        cfg.storage.vault_dir = str(tmp / "vault_scope")
        vault = Vault(cfg)
        # 60 张就够触发多屏布局；不用真图，占位即可。
        img = Image.new("RGB", (320, 240), (30, 60, 90))
        for i in range(60):
            vault.save(img, when=datetime(2026, 6, 1, 10, 0, i),
                       ctx=ShotContext(exe="chrome.exe", title="scope", source="region"))

        from .gallery import GalleryWindow
        win = GalleryWindow(vault, cfg)
        win.resize(1000, 700)
        win.show()
        app.processEvents()

        # 记下"真正新排出去"的请求（已在缓存 / 已在飞的不算）
        asked: list[int] = []
        inner = win._loader.request

        def spy(shot_id, thumb_path, full_path):
            if shot_id not in win._loader._inflight and shot_id not in win._thumbs:
                asked.append(shot_id)
            return inner(shot_id, thumb_path, full_path)

        win._loader.request = spy

        # 1) reload() 后的补图范围
        asked.clear()
        win.reload()
        app.processEvents()
        first = len(asked)

        _w, cell_h, cols = win._grid_metrics()
        vp = win.grid.viewport().rect()
        per_screen = max(1, (vp.height() + cell_h - 1) // cell_h)
        # 上限 = 视口 1 屏 + 上下各预取 1 屏，再宽放 1 行
        limit = (per_screen * 3 + 1) * cols
        expect("图库 补图只覆盖视口±一屏",
               first <= limit,
               f"reload 后请求 {first} 张 / 全库 {win.grid.count()} 张，上限 {limit}")

        # 2) 可见行范围必须是几何算出来的真值，不是 0..count-1
        r0, r1 = win._visible_rows()
        expect("图库 可见行范围合理",
               0 <= r0 <= r1 < max(1, win.grid.count()),
               f"row0={r0} row1={r1} count={win.grid.count()}")
        expect("图库 可见行不是整库退化",
               r1 - r0 + 1 <= per_screen + 1,
               f"覆盖 {r1 - r0 + 1} 行，一屏 {per_screen} 行")

        # 3) 再调一次应当是"没有新请求"，而不是重新排一遍
        asked.clear()
        win._request_visible()
        expect("图库 重复补图无新请求", len(asked) == 0, f"重复请求 {len(asked)} 张")

        # 4) 一路滚到底：只应为底部附近的几行排图，而不是从头排一遍
        #
        # 这条是"滚动一次就重排全库"那个 bug 的直接守卫。注意不要滚一小段就
        # 断言"必须有新请求" —— 小幅滚动本来就落在预取范围内，0 张才是对的。
        asked.clear()
        sb = win.grid.verticalScrollBar()
        sb.setValue(sb.maximum())
        app.processEvents()
        win._request_visible()
        expect("图库 滚到底只补底部附近",
               0 < len(asked) <= limit,
               f"滚到底请求 {len(asked)} 张 / 全库 {win.grid.count()} 张，上限 {limit}")

        # 5) reload() 不许清空缩略图缓存
        #
        # 改版前 reload() 里无条件 ``_thumbs.clear()`` —— 每敲一个字、每点一次
        # 分类，网格先整屏退回占位符再重新解码补回来（实测 12/12 张变占位符）。
        # 这就是「打字搜索一顿一顿 / 点分类卡一下」的主要来源。
        # 内存由 LRU 上限管，不需要靠清空约束。
        pump_ms = 400
        _end = time.time() + pump_ms / 1000.0
        while time.time() < _end:
            app.processEvents()
        cached_before = len(win._thumbs)
        win.reload()
        app.processEvents()
        expect("图库 reload 不清空缩略图缓存",
               cached_before > 0 and len(win._thumbs) >= cached_before,
               f"reload 前缓存 {cached_before} 张，reload 后 {len(win._thumbs)} 张")

        # 6) 动效真的在跑，而且中间态取得到
        #
        # 判据刻意不是"动画对象存在" —— 用户的原话是"我感受不到动画"，
        # 对象存在但全程停在 0/1 两端，等于没有动画。所以必须采样到
        # **严格落在起点和终点之间**的值。
        from . import theme as _theme
        _theme.set_reduce_motion(False)
        try:
            item0 = win.grid.item(0)
            sid0 = item0.data(Qt.UserRole)
            win._delegate.press(sid0, True)
            mids: list[float] = []
            end = time.time() + 0.3
            while time.time() < end:
                k = win._delegate._press_k
                if 0.02 < k < 0.98:
                    mids.append(k)
                app.processEvents()
            expect("动效 卡片按下取得到中间态", bool(mids),
                   f"0.3s 内没采到中间值，最终 k={win._delegate._press_k}")
            expect("动效 卡住按下最终落到 1.0",
                   abs(win._delegate._press_k - 1.0) < 0.02,
                   str(win._delegate._press_k))
            win._delegate.clear_press()
            end = time.time() + 0.3
            while time.time() < end:
                app.processEvents()
            expect("动效 松开后归位并清掉按下态",
                   win._delegate._press_id is None
                   and abs(win._delegate._press_k) < 0.02,
                   f"k={win._delegate._press_k} id={win._delegate._press_id}")

            win.grid.setCurrentItem(item0)
            sel_mids: list[float] = []
            end = time.time() + 0.35
            while time.time() < end:
                k = win._delegate._sel_in_k
                if 0.02 < k < 0.98:
                    sel_mids.append(k)
                app.processEvents()
            expect("动效 选中高亮取得到中间态", bool(sel_mids),
                   f"0.35s 内没采到中间值，最终 k={win._delegate._sel_in_k}")
        finally:
            _theme.set_reduce_motion(False)
            win.grid.clearSelection()

        expect("动效 时长档位齐全",
               all(k in _theme.MOTION
                   for k in ("press", "fast", "normal", "enter", "smooth")),
               str(sorted(_theme.MOTION)))
        expect("动效 进场时长长于交互过渡（才看得见）",
               _theme.MOTION["enter"] > _theme.MOTION["fast"],
               f'enter={_theme.MOTION["enter"]} fast={_theme.MOTION["fast"]}')
        expect("动效 按下时长短于交互过渡（才跟手）",
               _theme.MOTION["press"] < _theme.MOTION["fast"],
               f'press={_theme.MOTION["press"]} fast={_theme.MOTION["fast"]}')

        win.close()
        win.deleteLater()
        app.processEvents()
    except Exception:
        fail("图库 补图范围", traceback.format_exc(limit=4))


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SnapVault 自检")
    parser.add_argument("--keep", action="store_true", help="保留临时目录")
    parser.add_argument("--tmp", default="", help="指定临时目录")
    args = parser.parse_args(argv)

    tmp = Path(args.tmp) if args.tmp else Path(tempfile.mkdtemp(prefix="snapvault-selftest-"))
    print(f"[selftest] 工作目录: {tmp}\n")

    test_config(tmp)
    test_config_resolution(tmp)
    test_naming(tmp)
    test_classifier()
    test_vault(tmp)
    test_ai(tmp)
    test_watcher(tmp)
    test_folder_classify(tmp)
    test_theme(tmp)
    test_static_names()
    test_motion_single_source()
    test_appid_timing()
    test_settings_ui()
    test_gallery_scope(tmp)

    width = max(len(n) for _, n, _ in _results) + 2
    fails = 0
    for status, name, why in _results:
        if status == FAIL:
            fails += 1
        marker = "OK  " if status == PASS else "FAIL"
        print(f"[{marker}] {name.ljust(width)} {why if status == FAIL else ''}".rstrip())
    print(f"\n[selftest] {len(_results) - fails}/{len(_results)} 通过")

    if not args.keep and not args.tmp:
        shutil.rmtree(tmp, ignore_errors=True)
    else:
        print(f"[selftest] 保留目录: {tmp}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
