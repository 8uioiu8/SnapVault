# -*- mode: python ; coding: utf-8 -*-
"""SnapVault 的 PyInstaller 规格（onedir）。

构建::

    D:/python/python.exe -m PyInstaller packaging/SnapVault.spec --noconfirm

或直接双击 ``packaging/build.bat``（会先补图标再打包）。

产物::

    dist/SnapVault/SnapVault.exe     ← 双击即用，无控制台，常驻托盘
    dist/SnapVault/_internal/        ← 依赖，整个文件夹要一起带走

为什么是 onedir 而不是 onefile
------------------------------
onefile 每次启动都要把上百 MB 解压到 %TEMP%，冷启动慢好几秒，而且会在临时
目录里反复写。这是开机常驻的托盘工具，启动速度比"只有一个文件"重要。

体积是怎么从 169 MB 压到 ~114 MB 的
-----------------------------------
不靠猜。把产物里每个 DLL 的 PE 导入表读出来做依赖闭包后发现：PySide6 的钩子
会让 ``Qt6Network / Qt6Qml / Qt6Quick / Qt6Pdf / Qt6VirtualKeyboard / Qt6OpenGL``
一整簇 DLL 跟进来，而它们**只互相依赖**，没有任何一条边连到本程序真正用到的
``Qt6Core / Qt6Gui / Qt6Widgets / Qt6Svg`` 上 —— 是块孤岛，可以整块摘掉。
（校验方法见 README「打包」一节。）
"""

import fnmatch
from pathlib import Path

SPEC_DIR = Path(SPECPATH).resolve()
ROOT = SPEC_DIR.parent

# ── 磁盘资产：随包字体 + 第三方许可文本 ─────────────────────────────────
# 不需要带任何图标文件 —— icons.py 把 Lucide 的 SVG 路径内嵌成了 Python
# 字符串，运行时用 QSvgRenderer 现画。落点 snapvault/assets 是刻意对齐
# theme._fonts_dir() 的查找约定。
#
# THIRD_PARTY_LICENSES.md **必须随包分发**，不是可有可无的文档：
# HarmonyOS Sans Fonts License Agreement 第 2 条第 4 款要求
#   "YOU shall retain the copyright notice and this Agreement in any copies
#    of HarmonyOS Sans Fonts."
# exe 里打包了字体文件，这个 exe 就是"copies of HarmonyOS Sans Fonts"，
# 所以协议全文得跟着走 —— 光在设置面板里写一句声明只满足第 1 款。
# 放到 exe 同级的 dist 根目录，用户找得到、也不用去翻源码仓库。
datas = [
    (str(ROOT / "snapvault" / "assets"), "snapvault/assets"),
    (str(ROOT / "THIRD_PARTY_LICENSES.md"), "."),
]

# ── 排除用不到的 Python 模块 ────────────────────────────────────────────
# 项目只 import 四个 Qt 模块：QtCore / QtGui / QtWidgets / QtSvg。
excludes = [
    "PySide6.QtNetwork", "PySide6.QtQml", "PySide6.QtQuick",
    "PySide6.QtQuick3D", "PySide6.QtQuickWidgets", "PySide6.QtQuickControls2",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick", "PySide6.QtWebChannel", "PySide6.QtWebSockets",
    "PySide6.QtWebView", "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic", "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning",
    "PySide6.QtLocation", "PySide6.QtSerialPort", "PySide6.QtSerialBus",
    "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtDesigner", "PySide6.QtHelp",
    "PySide6.QtUiTools", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets", "PySide6.QtOpenGLFunctions",
    "PySide6.QtSensors", "PySide6.QtScxml", "PySide6.QtStateMachine",
    "PySide6.QtRemoteObjects", "PySide6.QtTextToSpeech",
    "PySide6.QtSpatialAudio", "PySide6.QtHttpServer", "PySide6.QtConcurrent",
    "PySide6.QtDBus", "PySide6.QtPrintSupport",
    # AVIF 编解码器 7.5 MB。截图是 PNG，用不上；Pillow 的插件加载
    # （preinit/init）对 ImportError 是 catch 的，摘掉不会崩。
    "PIL._avif", "PIL.AvifImagePlugin",
    # 想再瘦 ~31 MB：把下面这行的注释去掉。analyzer.py 里 numpy 是
    # try/except 可选依赖，排掉不会崩，只是图像特征提取走纯 PIL 慢路径
    # —— 890 张图重归类会明显变慢，所以默认保留。
    # "numpy",
    # 标准库 / 构建期残留，运行时用不到
    "tkinter", "unittest", "doctest", "pydoc", "test", "distutils",
    "setuptools", "pip", "wheel", "pkg_resources", "IPython",
    "matplotlib", "scipy", "pandas", "pytest",
]

# ── 从产物里直接摘掉的二进制 / 数据 ─────────────────────────────────────
# 这些是 PySide6 钩子"顺手拖进来"的，`excludes` 管不到（它们是 DLL，
# 不是 Python 模块）。左边注释是省下的体积。
PRUNE = [
    "PySide6/opengl32sw.dll",                       # 19.7 MB 软件 OpenGL 兜底
    "PySide6/Qt6Quick.dll",                         #  6.3 MB
    "PySide6/Qt6Qml.dll",                           #  5.1 MB
    "PySide6/Qt6Pdf.dll",                           #  4.4 MB
    "PySide6/Qt6OpenGL.dll",                        #  1.9 MB
    "PySide6/Qt6Network.dll",                       #  1.7 MB
    "PySide6/Qt6QmlModels.dll",                     #  0.9 MB
    "PySide6/Qt6VirtualKeyboard.dll",               #  0.4 MB
    "PySide6/Qt6QmlMeta.dll",                       #  0.2 MB
    "PySide6/Qt6QmlWorkerScript.dll",               #  0.1 MB
    "PySide6/plugins/platforminputcontexts/*",      #     虚拟键盘输入法插件
    "PySide6/plugins/imageformats/qpdf.dll",        #     PDF 图像格式
    "PySide6/translations/*",                       #  6.7 MB 项目从不装 QTranslator
    "PIL/_avif*.pyd",                               #  7.5 MB
]

hiddenimports = [
    # 入口是 packaging/entry.py，里面是 import 语句不是字面量模块名，
    # 模块图未必能自己发现。
    "snapvault.__main__",
]


def _prune(entries, patterns):
    """按产物内的落点路径丢掉条目。返回 (保留, 摘掉的)。

    **必须还回同样的 TOC 类型**：``COLLECT`` 对参数做 ``isinstance(arg, TOC)``
    判断，喂普通 list 进去会被**静默忽略**（不报错、不警告，裁剪白做）。
    这个坑第一次打包时踩到了 —— spec 打印"省下 47.1 MB"，dist 里一个字节没少。
    """
    kept, dropped = [], []
    for item in entries:
        name = str(item[0]).replace("\\", "/")
        (dropped if any(fnmatch.fnmatch(name, p) for p in patterns) else kept).append(item)
    return type(entries)(kept), dropped


_ico = SPEC_DIR / "SnapVault.ico"

a = Analysis(
    [str(SPEC_DIR / "entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

a.binaries, _dropped_bin = _prune(a.binaries, PRUNE)
a.datas, _dropped_data = _prune(a.datas, PRUNE)
_saved = sum(Path(i[1]).stat().st_size for i in _dropped_bin + _dropped_data
             if Path(i[1]).exists()) / 1048576
print(f"[spec] 摘掉 {len(_dropped_bin) + len(_dropped_data)} 项，省下 {_saved:.1f} MB")
for _i in _dropped_bin + _dropped_data:
    print(f"[spec]   - {str(_i[0]).replace(chr(92), '/')}")

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SnapVault",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                      # UPX 压过的 Qt DLL 常被杀软误报，不值
    console=False,                  # 托盘工具，不要黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(_ico) if _ico.exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="SnapVault",
)
