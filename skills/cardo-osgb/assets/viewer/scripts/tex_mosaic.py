# -*- coding: utf-8 -*-
"""纹理与材质识别 · 顶面正射镶嵌。

把 44 个 b3dm 的朝上三角面按「细层级优先、同级取最高点」规则压成一张正射影像：
  · 按 LOD 从粗到细逐层覆盖（painter's algorithm），细层覆盖粗层 → 消除同一块地被多份 LOD 重复计权；
  · 同层同像素取 z 最大者（顶面可见）；
  · 每个像素记录样本数与样本面积，供后续置信度分级。
"""
from __future__ import annotations

import json
import math
import os
import sys

import numpy as np

sys.dont_write_bytecode = True

from tex_core import lod_level, tile_samples  # noqa: E402


def _grid_shape(model, res):
    bb = model["geometry"]["bboxLocal"]
    # 栅格原点向下取整到整米，保证 1 m 网格与绝对坐标对齐（复核/出表时不产生半格漂移）
    x0, y0 = float(math.floor(float(bb["min"][0]))), float(math.floor(float(bb["min"][1])))
    x1, y1 = float(bb["max"][0]), float(bb["max"][1])
    nx = max(1, int(math.ceil((x1 - x0) / res)))
    ny = max(1, int(math.ceil((y1 - y0) / res)))
    return (x0, y0, x1, y1), nx, ny


def _tiles(model, root):
    out = []
    for a in model.get("artifacts") or []:
        rel = str(a.get("path", ""))
        if not rel.lower().endswith(".b3dm"):
            continue
        full = os.path.join(root, rel.replace("/", os.sep))
        if not os.path.isfile(full):
            continue
        out.append((rel, full, lod_level(rel)))
    return out


def build(model_path, res=0.25, normal_z=0.5, progress=None):
    """返回 (ortho_rgb, z_raster, sample_count, area_sum, meta)。"""
    model_path = os.path.abspath(model_path)
    with open(model_path, encoding="utf-8") as fh:
        model = json.load(fh)
    if model.get("schema") != "cardo/lowair-model/v1":
        raise ValueError(f"模型 schema 非 cardo/lowair-model/v1：{model.get('schema')}")
    root = os.path.dirname(model_path)
    (x0, y0, x1, y1), nx, ny = _grid_shape(model, res)
    tiles = _tiles(model, root)
    if not tiles:
        raise ValueError("模型清单里没有可读的 b3dm 瓦片")
    tiles.sort(key=lambda t: t[2])  # 粗 → 细

    px_all, py_all, z_all, rgb_all, lod_all, area_all = [], [], [], [], [], []
    per_tile = []
    tri_total = tri_up = 0
    tex_images = 0
    tex_failed = []
    bad_nodes = 0
    failures = []

    for i, (rel, full, lod) in enumerate(tiles, 1):
        try:
            s = tile_samples(full, (x0, y0), res, normal_z=normal_z)
        except (OSError, ValueError, json.JSONDecodeError, KeyError) as exc:
            failures.append({"path": rel, "error": f"{type(exc).__name__}: {exc}"})
            continue
        tri_total += s["triTotal"]
        tri_up += s["triUp"]
        tex_images += s["textures"]
        tex_failed.extend(s["texFailed"])
        bad_nodes += s["nonIdentityNodes"]
        if s["px"].size == 0:
            per_tile.append({"path": rel, "lod": lod, "samples": 0})
            continue
        # 像素取整 + 边界裁剪
        ix = np.clip(np.round(s["px"]).astype(np.int64), 0, nx - 1)
        iy = np.clip(np.round(s["py"]).astype(np.int64), 0, ny - 1)
        px_all.append(ix * ny + iy)
        z_all.append(s["z"])
        rgb_all.append(s["rgb"])
        area_all.append(s["area"])
        lod_all.append(np.full(ix.shape, lod, dtype=np.int16))
        per_tile.append({"path": rel, "lod": lod, "samples": int(ix.size)})
        if progress:
            progress(i, len(tiles), rel, int(ix.size))

    if not px_all:
        raise ValueError("所有瓦片都没有可用的朝上纹理样本")

    flat = np.concatenate(px_all)
    z = np.concatenate(z_all)
    rgb = np.concatenate(rgb_all, axis=0)
    area = np.concatenate(area_all)
    lod = np.concatenate(lod_all)

    # 细层优先、同层取最高：按 (lod, z) 升序后，每个像素保留最后一条
    order = np.lexsort((z, lod))
    flat, z, rgb, area = flat[order], z[order], rgb[order], area[order]
    rev = flat[::-1]
    _, first_rev = np.unique(rev, return_index=True)
    keep = (flat.size - 1) - first_rev
    keep.sort()
    sel = keep

    npix = nx * ny
    ortho = np.full((npix, 3), 0, dtype=np.uint8)
    zr = np.full(npix, np.nan, dtype=np.float32)
    cnt = np.zeros(npix, dtype=np.int32)
    asum = np.zeros(npix, dtype=np.float32)
    ortho[flat[sel]] = rgb[sel]
    zr[flat[sel]] = z[sel]
    cnt[:] = np.bincount(flat, minlength=npix)
    asum[:] = np.bincount(flat, weights=area.astype(np.float64), minlength=npix)

    meta = {
        "resM": res,
        "grid": [nx, ny],
        "extent": [x0, y0, x1, y1],
        "tiles": len(tiles),
        "tilesRead": sum(1 for t in per_tile if t["samples"] > 0),
        "trianglesTotal": tri_total,
        "trianglesUpward": tri_up,
        "samplesTotal": int(flat.size),
        "samplesUsed": int(sel.size),
        "pixelsObserved": int(np.isfinite(zr).sum()),
        "textureImages": tex_images,
        "textureFailed": tex_failed[:10],
        "nonIdentityNodes": bad_nodes,
        "failures": failures[:10],
        "lodLevels": sorted({int(v) for v in lod}),
        "perTile": per_tile,
    }
    return (ortho.reshape(nx, ny, 3), zr.reshape(nx, ny),
            cnt.reshape(nx, ny), asum.reshape(nx, ny), meta, model)


# ---------------------------------------------------------------- 地面与掩膜

def _min_filter(a, radius, fill):
    """二维最小值滤波（可分离：先按行、再按列），用于形态学地面估计。"""
    b = np.where(np.isfinite(a), a, fill)
    w = 2 * radius + 1
    if w >= 2 * min(b.shape):
        return np.full_like(b, np.nanmin(b))
    pad = np.pad(b, ((radius, radius), (0, 0)), mode="edge")
    b = np.lib.stride_tricks.sliding_window_view(pad, w, axis=0).min(axis=-1)
    pad = np.pad(b, ((0, 0), (radius, radius)), mode="edge")
    return np.lib.stride_tricks.sliding_window_view(pad, w, axis=1).min(axis=-1)


def _mean_filter(a, radius):
    w = 2 * radius + 1
    if w >= 2 * min(a.shape):
        return np.full_like(a, float(a.mean()))
    pad = np.pad(a, ((radius, radius), (0, 0)), mode="edge")
    b = np.lib.stride_tricks.sliding_window_view(pad, w, axis=0).mean(axis=-1)
    pad = np.pad(b, ((0, 0), (radius, radius)), mode="edge")
    return np.lib.stride_tricks.sliding_window_view(pad, w, axis=1).mean(axis=-1)


def ground_and_mask(zr, res, window_m=15.0, smooth_cells=2, building_h=3.0):
    """形态学地面滤波（Zhang et al. 2003 的开运算思路）+ 建筑掩膜。

    未观测像元不参与最小值统计（填 +inf 而非全局最低，避免空洞把地面拉低）。
    返回 (ground, height, building)：ground/height 单位米，building 为布尔阵。
    """
    obs = np.isfinite(zr)
    radius = max(1, int(round(window_m / res / 2.0)))
    ground = _min_filter(zr, radius, np.inf)
    bad = ~np.isfinite(ground)
    if bad.any():
        ground[bad] = float(np.nanmedian(zr)) if obs.any() else 0.0
    if smooth_cells > 0:
        ground = _mean_filter(ground, smooth_cells)
    height = np.where(obs, zr - ground, np.nan)
    building = obs & np.isfinite(height) & (height > building_h)
    return ground, height, building
