"""``python -m snapvault`` 入口：无参数时启动托盘常驻，带参数时走命令行。"""

from __future__ import annotations

import argparse
import sys


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="snapvault",
        description="SnapVault —— 自动命名与分类归档的截图工具",
    )
    p.add_argument("--gallery", "--show", dest="gallery", action="store_true",
                   help="启动时直接打开图库窗口")
    p.add_argument("--minimized", dest="minimized", action="store_true",
                   help="只显示托盘图标，不弹出窗口")
    p.add_argument("--capture", choices=["region", "window", "fullscreen",
                                         "all_screens", "repeat_last", "clipboard"],
                   help="不开界面，直接按指定模式截一张图后就退出")
    p.add_argument("--import", dest="import_paths", nargs="+", metavar="FILE",
                   help="把一个或多个图片文件导入图库")
    p.add_argument("--tags", default="", help="配合 --import/--capture 使用的标签，逗号分隔")
    p.add_argument("--config", metavar="FILE", help="使用指定配置文件")
    p.add_argument("--vault", metavar="DIR", help="临时指定图库目录（不写回配置）")
    p.add_argument("--selftest", action="store_true", help="运行无界面自检后退出")
    p.add_argument("--stats", action="store_true", help="打印图库统计后退出")
    p.add_argument("--check-ai", dest="check_ai", action="store_true",
                   help="用一张合成图验证 AI 配置（Key / 接口地址 / 模型）是否可用")
    p.add_argument("--doctor", action="store_true",
                   help="体检：托盘是否在跑、盯的目录对不对、AI 通不通、有没有积压")
    p.add_argument("--version", action="store_true", help="打印版本号")
    return p


def _check_ai(cfg) -> int:
    """用一张合成图验证 AI 配置是否可用（只发一次请求，不写任何文件）。"""
    from PIL import Image, ImageDraw

    from .ai import AiError, QwenVision, normalize_endpoint

    ai = cfg.ai
    endpoint = normalize_endpoint(ai.endpoint)
    key = ai.api_key.strip()

    print("SnapVault AI 配置自检")
    print(f"  接口  : {endpoint}")
    print(f"  模型  : {ai.model}")
    print(f"  Key   : {(key[:8] + '…') if key else '未配置'}")

    if not key:
        print("\n未配置 API Key。打开「设置 → AI 命名」填进去，再跑一次本命令验证。")
        print("提示：百炼的 API Key 只在创建时显示一次，建完请立刻复制保存。")
        return 2

    img = Image.new("RGB", (720, 420), (255, 255, 255))
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, 720, 46), fill=(28, 92, 196))
    d.text((16, 16), "SnapVault AI connection check", fill=(255, 255, 255))
    for i in range(9):
        y = 90 + i * 32
        d.rectangle((36, y, 36 + (i * 97) % 460, y + 12), fill=(72, 76, 88))
    d.rectangle((520, 260, 690, 380), fill=(236, 240, 248))

    client = QwenVision(
        api_key=key, model=ai.model, endpoint=endpoint,
        prompt=ai.prompt, timeout=max(10.0, float(ai.timeout)),
        max_desc_len=ai.max_desc_len,
    )
    print("\n请求中…")
    try:
        res = client.describe(img)
    except AiError as exc:
        print(f"\n失败：{exc}")
        print("排查顺序：")
        print("  1) Key 是否完整（sk- 开头；百炼的 Key 只在创建时显示一次）")
        print("  2) 该模型是否已在百炼控制台开通")
        print("  3) 接口地址是否与控制台上的一致（业务空间专属域名 vs 公共域名）")
        print("  4) 网络 / 代理是否可达")
        return 1
    except Exception as exc:
        print(f"\n失败：{exc}")
        return 1

    print(f"\n成功：模型返回「{res.description}」（{res.elapsed_ms} ms）")
    print("AI 命名链路可用。")
    return 0


def _split_tags(raw: str) -> list[str]:
    return [t.strip() for t in raw.replace("，", ",").split(",") if t.strip()]


def _doctor(cfg, *, interval_slack: float = 8.0) -> int:
    """体检「系统截图接管」这一条链路，逐环报告并给出结论。

    要判断的其实是那句最朴素的问题：**我现在截一张图，它会被自动命名分类吗？**
    这条链路有五个环节，任何一环断了都不会有反应：

        托盘进程在跑 → 接管线程在轮询 → 盯的是系统截图目录 → AI 能用 → 分类目录可写
    """
    import time
    from pathlib import Path

    from .config import default_screenshots_dir, pictures_dir
    from .shot import NullFeedback, ShotService
    from .watcher import SystemShotWatcher

    problems: list[str] = []
    warns: list[str] = []

    def line(ok, title: str, detail: str = "") -> None:
        mark = "OK  " if ok else ("!!  " if ok is False else "·   ")
        print(f"[{mark}] {title:<26} {detail}".rstrip())

    print("SnapVault 接管体检")
    print("=" * 68)

    # --- 1. 目录：盯的到底是不是系统截图落盘的那个目录 ---
    real_pics = pictures_dir()
    shots_default = default_screenshots_dir()
    print(f"系统「图片」文件夹（注册表）: {real_pics}")
    print(f"默认截图目录              : {shots_default}")
    print(f"图库根目录                : {cfg.vault_path}")
    print(f"分类目录模板              : {cfg.storage.folder_pattern or '(平铺，不分子目录)'}")
    print("-" * 68)

    # --- 2. 接管开关与目录 ---
    line(bool(cfg.watch.enabled), "接管开关", "已开启" if cfg.watch.enabled else "关闭中（设置 → 截图接管）")
    if not cfg.watch.enabled:
        problems.append("接管开关是关的")

    service = ShotService(cfg, feedback=NullFeedback())
    watcher = SystemShotWatcher(cfg, service)
    try:
        dirs = watcher.dirs()
        raw_dirs = [str(d) for d in (cfg.watch.dirs or [])]
        if raw_dirs and not dirs:
            line(False, "监视目录", "配置的目录被拦下了（监视目录 == 图库根目录会死循环）")
            problems.append("监视目录无效")
        elif not dirs:
            line(None, "监视目录", "未配置，将回落到默认截图目录")
        for d in dirs:
            exists = d.is_dir()
            line(exists, "监视目录", f"{d}{'' if exists else '  ← 不存在！'}")
            if not exists:
                problems.append(f"监视目录不存在：{d}")
        # 盯错目录是最常见的坑（「图片」文件夹被重定向到别的盘）
        default_resolved = Path(shots_default).resolve()
        if dirs and default_resolved not in [p.resolve() for p in dirs]:
            line(None, "目录一致性",
                 f"监视的不是默认截图目录；若截图落在 {shots_default} 就会漏掉")
            warns.append("监视目录与系统默认截图目录不一致")
        else:
            line(True, "目录一致性", "监视的就是系统默认截图目录")

        # --- 3. 接管线程：靠心跳文件判断（跨进程） ---
        hb = watcher.read_heartbeat()
        if not hb:
            line(False, "接管进程",
                 "没有心跳记录 —— 托盘程序没启动"
                 "（刚双击 run.bat 的话等 10 秒再跑一次，启动要几秒）")
            problems.append("托盘程序没在运行")
        else:
            age = time.time() - float(hb.get("epoch") or 0)
            interval = float(hb.get("interval_ms") or 1500) / 1000.0
            fresh = age <= interval * 3 + interval_slack
            line(fresh, "接管进程",
                 f"pid {hb.get('pid')}，{age:.0f} 秒前还在轮询"
                 + ("" if fresh else "  ← 心跳过期，进程可能已退出"))
            line(bool(hb.get("running")), "接管线程", f"心跳里记录 running={hb.get('running')}")
            if not fresh:
                problems.append("接管进程不在运行（心跳过期）")
        line(None, "已登记（不用管）", f"{len(watcher._seen)} 张")
        try:
            pending = watcher.pending_count()
        except Exception:
            pending = -1
        line(True, "待接管", f"{pending} 张" + ("（会调 AI，注意额度）" if pending > 20 else ""))

        # --- 4. AI ---
        ai = cfg.ai
        if not ai.enabled:
            line(None, "AI 命名", "未开启（会退回「日期_时间_分类」命名）")
        elif not ai.api_key.strip():
            line(False, "AI 命名", "已开启但没有 API Key")
            problems.append("AI 开着却没配 Key")
        else:
            from .ai import normalize_endpoint

            line(True, "AI 命名", f"{ai.model} @ {normalize_endpoint(ai.endpoint)}")
            line(None, "分类判定", "AI 同时判分类" if ai.classify_with_ai else "只用本地视觉规则")
            line(None, "AI 连通性", "跑 `--check-ai` 实际发一次请求验证")

        # --- 5. 分类目录可写 ---
        try:
            cfg.vault_path.mkdir(parents=True, exist_ok=True)
            probe = cfg.vault_path / ".snapvault-write-test"
            probe.write_text("x", encoding="utf-8")
            probe.unlink()
            line(True, "图库目录可写", str(cfg.vault_path))
        except Exception as exc:
            line(False, "图库目录可写", f"{cfg.vault_path} → {exc}")
            problems.append("图库目录不可写")

        # --- 6. 自启动 ---
        try:
            from .winapi import get_autostart_command

            cmd = get_autostart_command()
            line(bool(cmd), "开机自启", cmd or "未注册（设置 → 界面与启动 里勾选）")
        except Exception:
            pass

        # --- 7. 最近归档 ---
        rows = []
        try:
            rows = service.vault.search("", limit=5)
        except Exception:
            rows = []
        if rows:
            print("-" * 68)
            print("最近归档：")
            for r in rows:
                print(f"  {r.rel_path}")
    finally:
        service.vault.close()

    print("=" * 68)
    if problems:
        print("结论：接管【没在正常工作】，需要处理：")
        for p in problems:
            print(f"  - {p}")
        return 1
    if warns:
        print("结论：接管已就绪，但有提醒：")
        for w in warns:
            print(f"  - {w}")
        return 0
    print("结论：接管已就绪 —— 现在按 Win+Shift+S 截图，几秒后就会出现在分类文件夹里。")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    from . import __version__
    from .config import Config, load_config, save_config

    if args.version:
        print(f"SnapVault {__version__}")
        return 0

    if args.selftest:
        from .selftest import main as selftest_main

        return selftest_main([])

    cfg = load_config(args.config)
    if args.vault:
        cfg.storage.vault_dir = args.vault

    if args.stats:
        from .vault import Vault
        from .naming import human_size

        vault = Vault(cfg)
        stats = vault.stats()
        print("SnapVault 图库统计")
        print(f"  目录    : {cfg.vault_path}")
        print(f"  总数    : {stats.get('total', 0)}")
        print(f"  占用    : {human_size(stats.get('bytes', 0))}")
        # category_counts() 返回 {分类: 数量}，month_counts()/tag_counts() 返回 [(键, 数)]
        cats = stats.get("categories") or {}
        label_of = cfg.category_label
        for cat, count in sorted(cats.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"    {label_of(cat):<6} {cat:<10} {count:>4}")
        months = stats.get("months") or []
        if months:
            print("  最近月份:")
            for ym, count in months[:6]:
                print(f"    {ym}  {count:>4}")
        tags = stats.get("top_tags") or []
        if tags:
            print("  常用标签:")
            print("    " + "  ".join(f"{tag}({uses})" for tag, uses in tags[:12]))
        return 0

    if args.check_ai:
        return _check_ai(cfg)

    if args.doctor:
        return _doctor(cfg)

    tags = _split_tags(args.tags)

    if args.import_paths:
        from .shot import NullFeedback, ShotService

        service = ShotService(cfg, feedback=NullFeedback())
        failed = 0
        for path in args.import_paths:
            result = service.import_file(path, tags=tags)
            if result.ok:
                rec = result.record
                print(f"已归档 {rec.filename}  [{cfg.category_label(rec.category)} "
                      f"{rec.confidence:.0%}]  {rec.rel_path}")
            else:
                failed += 1
                print(f"失败   {path}: {result.error}", file=sys.stderr)
        service.vault.close()
        return 1 if failed else 0

    if args.capture:
        from .shot import NullFeedback, ShotService, play_sound

        feedback = NullFeedback()
        service = ShotService(cfg, feedback=feedback)
        if args.capture == "clipboard":
            result = service.capture_clipboard(tags=tags)
        elif args.capture == "repeat_last":
            result = service.capture_last_region(tags=tags)
        elif args.capture == "window":
            result = service.capture_window(tags=tags)
        elif args.capture == "all_screens":
            result = service.capture_all_screens(tags=tags)
        elif args.capture == "fullscreen":
            result = service.capture_fullscreen(tags=tags)
        else:
            print("区域截图需要交互界面，请直接运行 snapvault 使用热键。", file=sys.stderr)
            return 2
        if result.ok:
            rec = result.record
            print(f"{rec.rel_path}")
            if cfg.storage.play_sound:
                play_sound("shot")
        else:
            print(f"截图失败：{result.error}", file=sys.stderr)
        service.vault.close()
        return 0 if result.ok else 1

    # 默认：托盘常驻
    from .app import main as app_main

    if not any(a in ("--gallery", "--show", "--minimized") for a in (argv or sys.argv[1:])):
        if cfg.ui.start_minimized:
            argv = list(argv or sys.argv[1:]) + ["--minimized"]
    return app_main(list(argv if argv is not None else sys.argv[1:]))


if __name__ == "__main__":
    raise SystemExit(main())
