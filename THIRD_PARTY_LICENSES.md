# 第三方组件与许可

SnapVault 本体（`snapvault/` 下的源代码）由本仓库作者授权，见仓库根目录的 `LICENSE`。
本文件收录**随仓库分发的第三方作品**的版权声明与完整许可文本。

| 组件 | 用途 | 许可 | 是否需要署名 |
| --- | --- | --- | --- |
| HarmonyOS Sans SC | 界面字体（`snapvault/assets/fonts/`） | HarmonyOS Sans Fonts License Agreement | **是，且必须在软件中声明** |
| Lucide | 图标图形（内联在 `snapvault/icons.py`，无独立文件） | ISC（其中部分源自 Feather，MIT） | 是（保留版权声明） |
| PySide6 / Qt for Python | GUI 框架（依赖，未随仓库分发） | LGPL v3 | 见文末「PySide6」一节 |

---

## 1. HarmonyOS Sans Fonts

随包分发了两个**未修改**的字体文件：

```
snapvault/assets/fonts/HarmonyOS_Sans_SC_Regular.ttf
snapvault/assets/fonts/HarmonyOS_Sans_SC_Medium.ttf
```

许可要点（据 2.GRANT OF LICENSE，逐条对应）：

| 许可条款 | SnapVault 的做法 |
| --- | --- |
| 允许 use / copy / merge / embed / bundle / **redistribute** / sell 未修改副本，但须与软件一起 | 字体随 `snapvault/assets/fonts/` 一起分发，不单独提供下载 |
| ① 必须在软件中作出显著声明，说明使用了 HarmonyOS Sans Fonts | 设置面板「界面与启动 → 关于」有一条第三方声明；本文件亦为声明 |
| ② 不得对字体或其组件作任何修改 | 两个字重均为华为发布的原始文件，未做子集化或改名 |
| ③ 不得**脱离本软件**单独再分发或销售字体 | 未单独提供字体下载；`packaging/` 只把字体打进 exe |
| ④ 任何副本中须保留版权声明与本协议 | 本节即版权声明与协议全文 |

### 版权声明与许可全文

```
License Notice

Copyright 2021 Huawei Device Co., Ltd.

HarmonyOS Sans Fonts Software is licensed under HarmonyOS Sans Fonts License Agreement.

--------------------------------------------------------------------------------

HarmonyOS Sans Fonts License Agreement

THIS HARMONYOS SANS FONTS LICENSE AGREEMENT ("AGREEMENT") IS A LEGAL AGREEMENT
BETWEEN YOU (EITHER A SINGLE INDIVIDUAL, OR SINGLE LEGAL ENTITY) AND HUAWEI
DEVICE CO., LTD. ("LICENSOR") FOR THE USE OF THE HARMONYOS SANS FONTS
ACCOMPANYING THIS AGREEMENT. BY DOWNLOADING, COPYING OR OTHERWISE USING
HARMONYOS SANS FONTS YOU INDICATE THAT YOU AGREE TO BE BOUND BY ALL OF THE TERMS
OF THIS AGREEMENT. IF YOU DO NOT AGREE TO THE TERMS OF THIS AGREEMENT, YOU MAY
NOT DOWNLOAD, COPY OR OTHERWISE USE THE HARMONYOS SANS FONTS, AND YOU SHALL
PROMPTLY DESTROY AND DELETE ALL THE HARMONYOS SANS FONTS.

1. Definition
"HarmonyOS Sans Fonts" shall mean the collection of fonts software components
provided by Licensor under this Agreement and clearly marked as "HarmonyOS Sans".
"YOU" shall mean an individual or legal entity exercising permissions granted by
this Agreement.

2. GRANT OF LICENSE
Subject to the terms and conditions of this Agreement, Licensor hereby grant YOU
a non-transferable, non-exclusive, royalty-free, revocable, worldwide copyright
license to use, copy, merge, embed, bundle, redistribute and/or sell unmodified
copies of HarmonyOS Sans Fonts with any software except for fonts software,
subject to the following conditions:
  1) YOU shall make a prominent notice in the software to state that HarmonyOS
     Sans Fonts are used.
  2) YOU may not make any modifications to HarmonyOS Sans Fonts or any of their
     individual components.
  3) Neither HarmonyOS Sans Fonts nor any of their individual components may be
     redistributed or sold in a stand-alone base. This limitation does not apply
     to any work created by using HarmonyOS Sans Fonts. You can freely distribute
     or sell your work, such as materials, logos, application software etc.
     created by using HarmonyOS Sans Fonts.
  4) YOU shall retain the copyright notice and this Agreement in any copies of
     HarmonyOS Sans Fonts.

3. Termination
This Agreement will be automatically terminated if YOU breach any of this
Agreement.

4. NO WARRANTY
YOU AGREE THAT THE HARMONYOS SANS FONTS ARE PROVIDED BY LICENSOR ON AN "AS IS"
BASIS. LICENSOR MAKES NO WARRANTY, EXPRESSED OR IMPLIED OR STATUTORY, WITH
RESPECT TO ANY OF THE HARMONYOS SANS FONTS, INCLUDING WITHOUT LIMITATION, ANY
IMPLIED WARRANTIES OF MERCHANTABILITY, SATISFACTORY QUALITY, FITNESS FOR A
PARTICULAR PURPOSE OR NON-INFRINGEMENT.

5. NO LIABILITY
IN NO EVENT SHALL LICENSOR BE LIABLE FOR ANY DIRECT OR INDIRECT, SPECIAL,
INCIDENTAL OR CONSEQUENTIAL DAMAGES (INCLUDING WITHOUT LIMITATION, PROCUREMENT
OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
INTERRUPTION) ARISING IN ANY WAY OUT OF THE USE OF OR INABILITY TO USE HARMONYOS
SANS FONTS, WHETHER UNDER THEORY OF CONTRACT, TORT (INCLUDING NEGLIGENCE),
STRICT LIABILITY OR OTHER LEGAL THEORY, EVEN IF LICENSOR HAS BEEN ADVISED OF THE
POSSIBILITY OF SUCH DAMAGE. YOU EXPRESSLY ASSUME ALL LIABILITIES AND RISKS FOR
USE OF HARMONYOS SANS FONTS. SHOULD THE HARMONYOS SANS FONTS PROVE DEFECTIVE,
YOU ASSUME THE ENTIRE COST OF ALL NECESSARY SERVICING, REPAIR OR CORRECTION.
```

> 删除 `snapvault/assets/fonts/` 即可完全移除本许可带来的义务，程序会自动降级到系统字体
> （见 `theme.load_fonts()`）。届时本文件第 1 节也应一并删除。

---

## 2. Lucide

`snapvault/icons.py` 内联了 39 颗图标的 SVG 路径数据，取自
[lucide-icons/lucide](https://github.com/lucide-icons/lucide) 的官方发布包
`lucide-static`。图形本身是数据，以内联字符串形式随本仓库分发。

```
ISC License

Copyright (c) 2026 Lucide Icons and Contributors

Permission to use, copy, modify, and/or distribute this software for any
purpose with or without fee is hereby granted, provided that the above
copyright notice and this permission notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

--------------------------------------------------------------------------------

The following Lucide icons are derived from the Feather project:

airplay, alert-circle, alert-octagon, alert-triangle, aperture, arrow-down-circle,
arrow-down-left, arrow-down-right, arrow-down, arrow-left-circle, arrow-left,
arrow-right-circle, arrow-right, arrow-up-circle, arrow-up-left, arrow-up-right,
arrow-up, at-sign, calendar, cast, check, chevron-down, chevron-left,
chevron-right, chevron-up, chevrons-down, chevrons-left, chevrons-right,
chevrons-up, circle, clipboard, clock, code, columns, command, compass,
corner-down-left, corner-down-right, corner-left-down, corner-left-up,
corner-right-down, corner-right-up, corner-up-left, corner-up-right, crosshair,
database, divide-circle, divide-square, dollar-sign, download, external-link,
feather, frown, hash, headphones, help-circle, info, italic, key, layout,
life-buoy, link-2, link, loader, lock, log-in, log-out, maximize, meh, minimize,
minimize-2, minus-circle, minus-square, minus, monitor, moon, more-horizontal,
more-vertical, move, music, navigation-2, navigation, octagon, pause-circle,
percent, plus-circle, plus-square, plus, power, radio, rss, search, server,
share, shopping-bag, sidebar, smartphone, smile, square, table-2, tablet,
target, terminal, trash-2, trash, triangle, tv, type, upload, x-circle,
x-octagon, x-square, x, zoom-in, zoom-out

The MIT License (MIT) (for the icons listed above)

Copyright (c) 2013-present Cole Bemis

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

---

## 3. PySide6 / Qt for Python

`PySide6`（实测版本 6.11.2）以 **LGPL v3** 授权，是**依赖**而非随本仓库分发的作品 ——
`requirements.txt` 只是声明它，源码仓库本身不含 Qt 的任何文件。

需要留意的是**打包产物**（`dist/SnapVault/`）会含 Qt 的动态库：

- LGPL v3 允许闭源软件动态链接 Qt，条件是**用户能够替换 Qt 库**；
  PyInstaller 打包属于动态链接，通常认为满足该条件；
- 需要随产物提供 LGPL v3 全文与 Qt 的版权声明，并说明如何获取 Qt 源码；
- 若不想承担这些义务，可行做法是**不发布 exe、只发布源码**，
  让用户自己 `pip install PySide6`。

> 本仓库的 `.gitignore` 默认排除 `dist/` 与 `build/`，
> 因此**默认只发布源码，不涉及 Qt 的分发义务**。

---

## 附：本文件怎么维护

新增任何第三方代码 / 数据 / 字体 / 图标时：

1. 在这里加一行表格，写清来源、用途、许可、是否需署名；
2. 需要保留版权声明的，把**原文**贴进对应小节（不要只写"MIT"两个字母）；
3. 运行 `python -m snapvault --selftest` —— 自检里有一条会检查引用的图标名
   在 `icons.py` 里真实存在，避免出现"声明了却没用到"或反过来。
