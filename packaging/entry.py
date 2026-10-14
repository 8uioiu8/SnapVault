#!/usr/bin/env python
"""PyInstaller 的冻结入口。**只在打包产物里跑**，源码运行不走这里。

为什么不直接拿 ``snapvault.pyw`` 当入口
---------------------------------------
两者职责不同，而且这里有一段**不能依赖 snapvault 包本身**的代码：

1. 异常兜底必须在 ``import snapvault`` 失败时仍然有效 —— 包都导不进来（依赖
   缺失 / 归档损坏）正是最需要弹窗告诉用户的情况。所以 ``_report`` 是刻意的
   重复实现，不复用 ``snapvault.pyw``。
2. 命令行要完整。``snapvault.pyw`` 只认 ``--gallery/--show/--minimized/--version``；
   打包出的 exe 应该和 ``python -m snapvault`` 一样支持 ``--selftest`` /
   ``--doctor`` / ``--stats`` / ``--import`` / ``--capture``，否则用户装完 exe
   反而少了一半能力。

控制台的处理
------------
窗口态 exe 没有控制台，``SnapVault.exe --version`` 会一个字都不打。所以带参数
启动时先把进程挂到调用方 cmd 的窗口上（``AttachConsole``），stdout 才有去处。
**但只对"跑完就退"的命令这么做** —— 给一个常驻托盘进程挂上父控制台，
关掉那个 cmd 窗口时它会收到 CTRL_CLOSE_EVENT，有被一起带走的风险。
"""

from __future__ import annotations

import ctypes
import os
import sys
import traceback
from pathlib import Path

#: 这几个参数意味着"常驻"，不要挂控制台。其余带参数启动都视为一次性命令。
_LONG_RUNNING = {"--minimized", "--gallery", "--show"}

_ATTACH_PARENT_PROCESS = -1


def _log_path() -> Path:
    """与 snapvault.pyw 保持同一个日志位置，两个入口的报错都落在同一个文件里。"""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or os.getcwd()
    folder = Path(base) / "SnapVault"
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except Exception:
        folder = Path(os.getcwd())
    return folder / "snapvault.log"


def _report(exc: BaseException) -> None:
    """写日志 + 弹窗。无控制台的 exe 里弹窗是用户唯一能看到的失败提示。"""
    text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    try:
        with _log_path().open("a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    except Exception:
        pass
    try:
        ctypes.windll.user32.MessageBoxW(
            None, f"SnapVault 启动失败：\n\n{exc}\n\n详细日志：{_log_path()}",
            "SnapVault", 0x10)
    except Exception:
        pass


def _console_encoding() -> str:
    """控制台自己的输出代码页。cmd 默认 936，``chcp 65001`` 之后是 65001。"""
    try:
        cp = ctypes.windll.kernel32.GetConsoleOutputCP()
    except Exception:
        cp = 0
    if not cp or cp == 65001:
        return "utf-8"
    return f"cp{cp}"


def _has_stdout() -> bool:
    """当前进程有没有可用的标准输出（控制台 / 管道 / 重定向都算）。

    判据取自 Win32 而不是 ``sys.stdout is not None`` —— 后者在窗口态 exe 里
    还可能是真，但句柄是废的。``GetFileType`` 返回 0（FILE_TYPE_UNKNOWN）
    才是"确实没地方写"。
    """
    if sys.stdout is None:
        return False
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)          # STD_OUTPUT_HANDLE
        if handle in (0, -1, None):                  # NULL / INVALID_HANDLE_VALUE
            return False
        return kernel32.GetFileType(handle) != 0
    except Exception:
        return True


def _setup_io() -> None:
    """让输出有地方去、且编码对。这两件事必须一起做，所以合成一个函数。

    1. **去处**：窗口态 exe 双击启动时没有控制台。带参数启动（``--version``
       / ``--selftest`` / ``--doctor``）时挂到调用方 cmd 的窗口上，stdout 才
       有去处。只在真的没地方写的时候才挂 —— 一旦挂上就把 ``sys.stdout`` 换成
       ``CONOUT$``，而 ``CONOUT$`` 只往控制台送、不进调用方的管道 / 重定向，
       于是 ``SnapVault.exe --version > out.txt`` 会得到一个空文件。

       常驻参数（``--minimized`` 等）**不挂**：给一个长期活着的托盘进程挂上
       父控制台，关掉那个 cmd 窗口时它会收到 CTRL_CLOSE_EVENT，有被带走的风险。

    2. **编码**：源码版 ``python -m snapvault`` 输出 UTF-8，同一份代码打成 exe
       却按 locale 的 cp936 写，管道里读出来就是乱码（``--stats`` 的中文全花）。
       所以显式定死：接控制台 → 用控制台代码页；接管道 / 文件 → UTF-8。
    """
    args = list(sys.argv[1:])
    has_out = _has_stdout()
    is_console = False

    if has_out:
        try:
            is_console = ctypes.windll.kernel32.GetFileType(
                ctypes.windll.kernel32.GetStdHandle(-11)) == 2   # FILE_TYPE_CHAR
        except Exception:
            is_console = False

    if not has_out and sys.platform.startswith("win") and args and not (
            _LONG_RUNNING & set(args)):
        try:
            if ctypes.windll.kernel32.AttachConsole(_ATTACH_PARENT_PROCESS):
                enc = _console_encoding()
                # 必须重开 CONOUT$：窗口态 exe 里 sys.stdout 是 None，
                # 光挂上控制台也不会往那里写。
                sys.stdout = open("CONOUT$", "w", encoding=enc, errors="replace",
                                  buffering=1)
                sys.stderr = open("CONOUT$", "w", encoding=enc, errors="replace",
                                  buffering=1)
                return
        except Exception:
            pass

    enc = _console_encoding() if is_console else "utf-8"
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding=enc, errors="replace")
        except Exception:
            pass


def main() -> int:
    _setup_io()
    args = list(sys.argv[1:])
    try:
        from snapvault.__main__ import main as cli_main

        return cli_main(args)
    except SystemExit as exc:
        return int(exc.code or 0)
    except BaseException as exc:            # noqa: BLE001 - 顶层兜底必须有
        _report(exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
