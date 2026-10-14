#!/usr/bin/env python
"""SnapVault 无控制台启动脚本（双击运行 / 开机自启用）。

用 .pyw 后缀让 Windows 关联到 pythonw.exe：没有黑框，异常写到
``%LOCALAPPDATA%\\SnapVault\\snapvault.log``，方便排查。
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))


def _log_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or str(HERE)
    folder = Path(base) / "SnapVault"
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except Exception:
        folder = Path(HERE)
    return folder / "snapvault.log"


def _report(exc: BaseException) -> None:
    text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    try:
        with _log_path().open("a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    except Exception:
        pass
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(
            None, f"SnapVault 启动失败：\n\n{exc}\n\n详细日志：{_log_path()}",
            "SnapVault", 0x10)
    except Exception:
        pass


def main() -> int:
    try:
        from snapvault import __version__
        from snapvault.app import main as app_main

        return app_main(sys.argv[1:])
    except SystemExit as exc:
        return int(exc.code or 0)
    except BaseException as exc:            # noqa: BLE001 - 顶层兜底必须有
        _report(exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
