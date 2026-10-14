"""图像内容分析：从截图提取可用于分类的统计特征.

设计原则
--------
* 只用 ``numpy`` + ``Pillow``，不引入 OpenCV / PyTorch 等重型依赖；
* 所有指标一次性向量化计算，1080p 截图耗时约 10-40ms；
* 输出扁平 ``dict``，便于规则分类器打分、也便于落库做后续分析。

特征速查
--------
``white_ratio``      近白像素比例 —— 文档 / 网页正文
``dark_ratio``       近黑像素比例 —— 深色主题代码、终端
``sat_mean``         平均饱和度   —— 彩色照片 / 视频 / 游戏
``dom_ratio``        最大同色簇占比 —— UI 纯色底板
``edge_density``     强边缘像素比例 —— 文字量
``stroke_mean``      笔画粗细（像素）—— 字号大小
``row_activity``     活跃行比例
``text_line_ratio``  连续文本行占比
``colorfulness``     颜色多样性（量化色簇数量）
``top_band_lightness`` / ``has_toolbar`` —— 顶部工具条信号
``left_band_*``      左侧栏信号（聊天列表 / 代码行号 / 导航）
``center_sat`` / ``center_edge`` —— 中心区域是否为"一张图"
``flat_ratio``       大片纯色区域占比
``photo_like``       统计意义上的照片似然
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from typing import Any

from PIL import Image

try:  # numpy 在个别环境下 ABI 不匹配，降级为纯 PIL 慢路径
    import numpy as np

    _HAS_NUMPY = True
except Exception:  # pragma: no cover
    np = None  # type: ignore
    _HAS_NUMPY = False


_LUMA = (0.299, 0.587, 0.114)


# --------------------------------------------------------------------------- #
# 特征容器
# --------------------------------------------------------------------------- #

@dataclass
class Features:
    width: int = 0
    height: int = 0
    aspect: float = 1.0

    lum_mean: float = 0.0
    lum_std: float = 0.0
    sat_mean: float = 0.0
    sat_std: float = 0.0

    white_ratio: float = 0.0
    dark_ratio: float = 0.0
    dom_color: tuple[int, int, int] = (0, 0, 0)
    dom_ratio: float = 0.0

    edge_density: float = 0.0
    edge_strength: float = 0.0
    stroke_mean: float = 1.0
    row_activity: float = 0.0
    text_line_ratio: float = 0.0
    line_pitch: float = 0.0

    colorfulness: int = 0
    flat_ratio: float = 0.0
    bg_lum: float = 0.0           # 主导亮度峰的位置（背景底色明度）
    bg_dominance: float = 0.0     # 落在主导亮度峰内的像素占比
    bg_purity: float = 0.0        # 主导亮度峰内"同一种颜色"的占比（纸张纯度）

    top_band_lightness: float = 0.0
    top_band_flat: float = 0.0
    has_toolbar: float = 0.0
    left_band_lightness: float = 0.0
    left_band_edge: float = 0.0
    has_sidebar: float = 0.0

    center_sat: float = 0.0
    center_edge: float = 0.0
    frame_edge: float = 0.0
    center_flat: float = 0.0

    rows_analyzed: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["dom_color"] = "#%02X%02X%02X" % self.dom_color
        return d


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #

def analyze(image: Image.Image, max_edge_rows: int = 420) -> Features:
    """分析一张 PIL 图像，返回 :class:`Features`。"""
    img = image.convert("RGB")
    w, h = img.size
    f = Features(width=w, height=h, aspect=(w / h) if h else 1.0)
    if w < 4 or h < 4:
        return f

    if not _HAS_NUMPY:
        return _analyze_pil(img, f, max_edge_rows)

    # 注意：必须用 int32 累加，int16 在 255*587 时就会溢出（曾导致暗图/亮图特征错乱）
    arr = np.asarray(img, dtype=np.int32)          # (h, w, 3)
    lum = (arr[..., 0] * 299 + arr[..., 1] * 587 + arr[..., 2] * 114) // 1000
    lumf = lum.astype(np.float32)

    f.lum_mean = float(lumf.mean())
    f.lum_std = float(lumf.std())
    f.white_ratio = float((lum >= 244).mean())
    f.dark_ratio = float((lum <= 26).mean())

    mx = arr.max(axis=2).astype(np.float32)
    mn = arr.min(axis=2).astype(np.float32)
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1.0), 0.0) * 255.0
    f.sat_mean = float(sat.mean())
    f.sat_std = float(sat.std())

    # 主色（量化到 4bit/通道统计众数）
    q = (arr >> 4).astype(np.int32)
    keys = (q[..., 0] << 8) | (q[..., 1] << 4) | q[..., 2]
    counts = np.bincount(keys.ravel(), minlength=4096)
    top = int(counts.argmax())
    f.dom_ratio = float(counts[top] / keys.size)
    f.colorfulness = int((counts > keys.size * 0.0004).sum())
    f.dom_color = (((top >> 8) & 0xF) * 17, ((top >> 4) & 0xF) * 17, (top & 0xF) * 17)

    # ---------------- 边缘 / 笔画 ----------------
    step = max(1, h // max_edge_rows)
    sub = lumf[::step, :]
    f.rows_analyzed = sub.shape[0]
    gx = np.abs(np.diff(sub, axis=1))
    gy = np.abs(np.diff(sub, axis=0))
    gx = np.pad(gx, ((0, 0), (0, 1)))
    gy = np.pad(gy, ((0, 1), (0, 0)))
    grad = np.maximum(gx, gy)

    strong = grad >= 26
    f.edge_density = float(strong.mean())
    f.edge_strength = float(grad[strong].mean()) if strong.any() else 0.0

    # 笔画宽度：强边缘像素的连通水平宽度近似 -> 用 run-length 估计
    if strong.any():
        widths = _run_lengths(strong)
        f.stroke_mean = float(np.median(widths)) if widths.size else 1.0

    row_hits = strong.sum(axis=1) / max(1, strong.shape[1])
    active = row_hits > 0.012
    f.row_activity = float(active.mean()) if active.size else 0.0
    f.text_line_ratio, f.line_pitch = _line_structure(active, step, h)
    # 表格网格：整行都是边缘线的行（横线贯穿整宽）→ 行距周期 + 规则性
    f.extra["row_period"], f.extra["row_regularity"] = _grid_signal(row_hits, step)

    # 主导亮度峰：文档=亮白底，深色代码=暗底，照片/视频=无峰
    hist, _ = np.histogram(lumf, bins=32, range=(0, 256))
    peak = int(hist.argmax())
    span = max(1, int(hist.sum() * 0.001))
    lo = peak
    while lo - 1 >= 0 and hist[lo - 1] >= span:
        lo -= 1
    hi = peak
    while hi + 1 < hist.size and hist[hi + 1] >= span:
        hi += 1
    f.bg_lum = (lo + hi) * 4.0
    f.bg_dominance = float(hist[lo:hi + 1].sum() / max(1, hist.sum()))
    # 纸张纯度：主导亮度带内"最单一颜色"的占比。
    # 文档/表格是一张近乎单色的纸（实测 ≥0.83），而桌面壁纸虽然亮度也集中在
    # 一个峰里，同一亮度下颜色千变万化（实测 0.35）——这是区分"纸"和"壁纸"
    # 最直接的信号，单靠亮度直方图区分不了。
    bg_half = max(3.0, (hi - lo + 1) * 2.0)
    bg_mask = np.abs(lumf - f.bg_lum) <= bg_half
    bg_total = int(bg_mask.sum())
    if bg_total:
        bg_counts = np.bincount(keys[bg_mask], minlength=4096)
        f.bg_purity = float(bg_counts.max() / bg_total)
    # 纯 PIL 降级路径没有这些统计量，用主导色占比近似，保证字段始终有值
    if not f.bg_purity:
        f.bg_purity = float(f.dom_ratio)

    # 大片纯色（UI 底板）比例：16 级量化后的众数占比，比"等于左上角像素"稳健得多
    qf = arr >> 4
    f.flat_ratio = float(counts[top] / keys.size)

    # ---------------- 区域信号 ----------------
    band_h = max(4, int(h * 0.08))
    band_w = max(4, int(w * 0.18))
    top = lumf[:band_h, :]
    f.top_band_lightness = float(top.mean())
    f.top_band_flat = _flat_ratio(arr[:band_h, :])
    f.has_toolbar = float(
        (abs(f.top_band_lightness - f.lum_mean) > 22) or (f.top_band_flat > 0.35)
    )

    left = lumf[:, :band_w]
    f.left_band_lightness = float(left.mean())
    left_grad = np.abs(np.diff(left, axis=1))
    f.left_band_edge = float((left_grad >= 26).mean()) if left_grad.size else 0.0
    f.has_sidebar = float(abs(f.left_band_lightness - f.lum_mean) > 16)

    cy0, cy1 = int(h * 0.25), int(h * 0.75)
    cx0, cx1 = int(w * 0.25), int(w * 0.75)
    f.center_sat = float(sat[cy0:cy1, cx0:cx1].mean())
    cen_grad = grad[int(h * 0.25 / step):max(int(h * 0.75 / step), 1), cx0:cx1]
    f.center_edge = float((cen_grad >= 26).mean()) if cen_grad.size else 0.0
    f.center_flat = float(_flat_ratio(arr[cy0:cy1, cx0:cx1]))

    # 整幅宽的水平线密度（表格网格 / 分隔线 / 代码缩进参考线）
    full_rows = row_hits >= 0.55
    f.frame_edge = float(full_rows.mean()) if full_rows.size else 0.0

    f.extra["photo_like"] = _photo_like(f)
    return f


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #

def _run_lengths(mask: "np.ndarray"):
    """水平方向连续 True 的游程长度（用于估计笔画宽度）。"""
    flat = mask.reshape(-1)
    if flat.size == 0:
        return np.zeros(0)
    idx = np.flatnonzero(flat)
    if idx.size == 0:
        return np.zeros(0)
    breaks = np.flatnonzero(np.diff(idx) > 1)
    starts = np.concatenate(([0], breaks + 1))
    ends = np.concatenate((breaks, [idx.size - 1]))
    return (idx[ends] - idx[starts] + 1).astype(np.float32)


def _line_structure(active: "np.ndarray", step: int, height: int) -> tuple[float, float]:
    """返回（连续文本行占比, 行间距像素）。"""
    if active.size == 0:
        return 0.0, 0.0
    runs: list[int] = []
    cur = 0
    for a in active:
        if a:
            cur += 1
        elif cur:
            runs.append(cur)
            cur = 0
    if cur:
        runs.append(cur)
    long_runs = [r for r in runs if r >= 2]
    ratio = (sum(long_runs) / active.size) if active.size else 0.0
    if len(runs) >= 3:
        gap = float(np.median(np.diff(runs))) if len(runs) > 1 else 0.0
    else:
        gap = 0.0
    return float(ratio), gap * step


def _grid_signal(row_hits: "np.ndarray", step: int) -> tuple[float, float]:
    """从"整行边缘占比"序列里找周期性横线（表格 / 表单）。

    返回 ``(行距像素, 规则性 0~1)``。规则性 = 相邻横线间距的稳定程度：
    真实表格的横线等距（接近 1），自然图片或文字段落的"横线"则杂乱。
    """
    if row_hits.size < 6:
        return 0.0, 0.0
    rows = np.flatnonzero(row_hits >= 0.45)
    if rows.size < 3:
        return 0.0, 0.0
    # 相邻横线合并成单条
    keep = np.concatenate(([True], np.diff(rows) > 1))
    rows = rows[keep]
    if rows.size < 3:
        return 0.0, 0.0
    pitch = np.diff(rows).astype(np.float32) * step
    if pitch.size < 2:
        return 0.0, 0.0
    med = float(np.median(pitch))
    if med <= 0:
        return 0.0, 0.0
    regular = 1.0 - min(1.0, float(np.std(pitch)) / med)
    return med, float(regular)


def _flat_ratio(arr: "np.ndarray") -> float:
    """区域内众数颜色（16 级量化）的占比。"""
    if arr.size == 0:
        return 0.0
    q = arr >> 4
    keys = (q[..., 0] << 8) | (q[..., 1] << 4) | q[..., 2]
    counts = np.bincount(keys.ravel(), minlength=4096)
    return float(counts.max() / keys.size)


def _photo_like(f: Features) -> float:
    """照片似然：颜色多样、饱和适中、边缘零散、无纯色底板。"""
    score = 0.0
    score += min(f.colorfulness / 220.0, 1.0) * 0.35
    score += min(f.sat_mean / 58.0, 1.0) * 0.25
    score += (1.0 - min(f.white_ratio / 0.25, 1.0)) * 0.15
    score += (1.0 - min(abs(f.lum_std - 62) / 62.0, 1.0)) * 0.10
    score += (1.0 - min(f.dom_ratio / 0.35, 1.0)) * 0.15
    return round(score, 4)


def _analyze_pil(img: Image.Image, f: Features, max_rows: int) -> Features:
    """numpy 不可用时的降级路径（精度略低，速度较慢）。"""
    w, h = img.size
    small = img
    if h > max_rows:
        small = img.resize((max(1, int(w * max_rows / h)), max_rows), Image.BILINEAR)
    gray = small.convert("L")
    hist = gray.histogram()
    total = sum(hist) or 1
    f.lum_mean = sum(i * c for i, c in enumerate(hist)) / total
    var = sum(((i - f.lum_mean) ** 2) * c for i, c in enumerate(hist)) / total
    f.lum_std = math.sqrt(max(var, 0.0))
    f.white_ratio = sum(hist[244:]) / total
    f.dark_ratio = sum(hist[:27]) / total
    hsv = small.convert("HSV")
    sat_hist = hsv.getchannel("S").histogram()
    f.sat_mean = sum(i * c for i, c in enumerate(sat_hist)) / total
    colors = small.getcolors(maxcolors=1 << 20) or []
    if colors:
        colors.sort(reverse=True)
        cnt, col = colors[0]
        f.dom_ratio = cnt / total
        f.dom_color = col[:3] if isinstance(col, tuple) else (0, 0, 0)
        f.colorfulness = len(colors)
    f.rows_analyzed = small.size[1]
    f.extra["photo_like"] = _photo_like(f)
    f.extra["degraded"] = True
    return f
