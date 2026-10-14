#!/usr/bin/env python
"""从代码里画出来的程序图标导出 ``SnapVault.ico``。

为什么要这一步
--------------
``snapvault.app.app_icon()`` 是用 QPainter 现画的（强调色圆角块 + Lucide
``scan-line`` 取景框），项目里**没有任何 .ico 文件**。源码运行时这没问题 ——
图标在每个窗口上现画；但 PyInstaller 给 exe 设文件图标时只认磁盘上的
``.ico``，运行时画的它拿不到。所以先离线渲一张出来，交给 spec 用。

为什么自己拼 ICO 而不调库
------------------------
Qt 的 ICO 写入端只写单帧，Pillow 的 ICO 写入端会自己重采样。两边都不好控制
"每一档都用 Qt 单独渲染"（16px 下必须单独渲，直接缩 256px 会糊）。
ICO 容器格式本身很简单，自己拼反而最稳：PNG 压缩帧，Windows Vista+ 原生支持。

用法::

    D:/python/python.exe packaging/make_ico.py
"""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Qt 在没有显示器时也要能渲位图。必须在 import QGuiApplication 之前设好。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QByteArray, QIODevice  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402

#: 每一档单独渲染。0 表示不放大到 256 的"较小档"，这里全给真实尺寸。
SIZES = (256, 128, 64, 48, 32, 24, 16)


def png_bytes(size: int) -> bytes:
    """在给定边长下用 Qt 渲染图标，返回 PNG 字节。"""
    from snapvault.app import app_icon

    pix = app_icon(size).pixmap(size, size)
    # QByteArray 必须先绑到变量上：写成 QBuffer(QByteArray()) 的话那个临时对象
    # 在构造完就被 Python 回收，缓冲区指向已释放内存，pix.save 直接 access violation。
    storage = QByteArray()
    buf = QBuffer(storage)
    buf.open(QIODevice.WriteOnly)
    if not pix.save(buf, "PNG"):
        raise RuntimeError(f"{size}px 渲染失败")
    data = bytes(buf.data())
    buf.close()
    return data


def write_ico(frames: list[tuple[int, bytes]], out: Path) -> None:
    """把 ``[(边长, PNG 字节)]`` 拼成 ICO 容器。

    目录项里宽/高各占 1 字节，所以 256 只能写 0（格式规定的"0 即 256"）。
    """
    count = len(frames)
    header = struct.pack("<HHH", 0, 1, count)          # reserved / type=icon / count
    offset = 6 + 16 * count                            # 目录紧跟在 6 字节头之后
    entries, blobs = [], []
    for size, blob in frames:
        dim = 0 if size >= 256 else size
        entries.append(struct.pack(
            "<BBBBHHII",
            dim, dim,          # 宽 / 高（0 表示 256）
            0,                 # 调色板颜色数（真彩填 0）
            0,                 # reserved
            1,                 # color planes
            32,                # bits per pixel
            len(blob), offset,
        ))
        blobs.append(blob)
        offset += len(blob)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(header + b"".join(entries) + b"".join(blobs))


def main() -> int:
    # QPixmap 依赖一个 QGuiApplication；offscreen 平台下不需要真实窗口。
    app = QGuiApplication.instance() or QGuiApplication([])  # noqa: F841

    frames = [(size, png_bytes(size)) for size in SIZES]
    out = HERE / "SnapVault.ico"
    write_ico(frames, out)

    kb = out.stat().st_size / 1024
    print(f"已写出 {out}")
    print(f"  帧：{' / '.join(str(s) for s, _ in frames)} 共 {len(frames)} 档")
    print(f"  大小：{kb:.1f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
