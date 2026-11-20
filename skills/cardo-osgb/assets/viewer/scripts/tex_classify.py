# -*- coding: utf-8 -*-
"""纹理与材质识别 · 分类与聚合。

分类只用可见光 RGB（本项目无近红外、无多光谱），算法均为公开的可见光植被指数与形态学算子：
  ExG  = 2g − r − b                （Woebbecke et al. 1995）
  ExR  = 1.4r − g                  （Meyer & Neto 2008）
  VARI = (g − r) / (g + r − b)      （Gitelson et al. 2002）
  GLI  = (2g − r − b) / (2g + r + b)（Louhaichi et al. 2001）
  RGBVI= (g² − r·b) / (g² + r·b)    （Bendig et al. 2015）
  NGRDI= (g − r) / (g + r)          （Tucker 1979）
全部为逐像元算术，无模型权重、无训练、无第三方许可负担。
"""
from __future__ import annotations

import sys

import numpy as np

sys.dont_write_bytecode = True

CLASS_NAMES = {0: "其它", 1: "绿化", 2: "道路", 3: "建筑屋面"}


# ---------------------------------------------------------------- 特征

def features(rgb_u8):
    """uint8 RGB → 归一化通道与六个可见光指数（全部 float32）。"""
    a = rgb_u8.astype(np.float32) / 255.0
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    mx = a.max(axis=2)
    mn = a.min(axis=2)
    sat = np.where(mx > 1e-6, (mx - mn) / np.maximum(mx, 1e-6), 0.0)
    exg = 2.0 * g - r - b
    exr = 1.4 * r - g
    vari = (g - r) / np.maximum(g + r - b, 1e-6)
    gli = (2.0 * g - r - b) / np.maximum(2.0 * g + r + b, 1e-6)
    rgbvi = (g * g - r * b) / np.maximum(g * g + r * b, 1e-6)
    ngrdi = (g - r) / np.maximum(g + r, 1e-6)
    return {"r": r, "g": g, "b": b, "val": mx, "sat": sat, "exg": exg, "exr": exr,
            "vari": vari, "gli": gli, "rgbvi": rgbvi, "ngrdi": ngrdi}


# ---------------------------------------------------------------- 形态学

def _shift(a, dx, dy, fill):
    out = np.full_like(a, fill)
    xs0, xs1 = max(0, dx), a.shape[0] + min(0, dx)
    ys0, ys1 = max(0, dy), a.shape[1] + min(0, dy)
    xd0, xd1 = max(0, -dx), a.shape[0] + min(0, -dx)
    yd0, yd1 = max(0, -dy), a.shape[1] + min(0, -dy)
    out[xd0:xd1, yd0:yd1] = a[xs0:xs1, ys0:ys1]
    return out


def dilate(mask, k=1):
    out = mask.copy()
    for dx in range(-k, k + 1):
        for dy in range(-k, k + 1):
            out |= _shift(mask, dx, dy, False)
    return out


def erode(mask, k=1):
    out = mask.copy()
    for dx in range(-k, k + 1):
        for dy in range(-k, k + 1):
            out &= _shift(mask, dx, dy, True)
    return out


def majority(mask, k=1, min_neighbors=4):
    """少数服从多数：邻域内同类像元 ≥ min_neighbors 才保留。"""
    cnt = np.zeros(mask.shape, dtype=np.int16)
    for dx in range(-k, k + 1):
        for dy in range(-k, k + 1):
            cnt += _shift(mask, dx, dy, False).astype(np.int16)
    return cnt >= min_neighbors


# ---------------------------------------------------------------- 分类

def roughness(height, k=2):
    """高程局部标准差（k 格窗口），用于区分树冠与平屋面。"""
    a = np.where(np.isfinite(height), height, 0.0)
    s1 = np.zeros_like(a)
    s2 = np.zeros_like(a)
    n = 0.0
    pad = np.pad(a, k, mode="edge")
    for dx in range(-k, k + 1):
        for dy in range(-k, k + 1):
            sl = pad[k + dx:k + dx + a.shape[0], k + dy:k + dy + a.shape[1]]
            s1 += sl
            s2 += sl * sl
            n += 1.0
    m = s1 / n
    return np.sqrt(np.maximum(s2 / n - m * m, 0.0))


DEFAULT_PARAMS = {
    "green": {"exrMax": 0.20, "satMin": 0.03, "valMax": 0.575, "variMin": 0.0,
              "bMinusGMax": 0.02},
    "canopy": None,
    "road": {"satMax": 0.09, "valMin": 0.52, "exrMin": 0.21, "heightMaxM": 1.2},
    "morph": {"open": True, "close": True, "majority": True, "majorityMin": 4},
}


def classify(feat, height, building, params=None):
    """返回 (labels uint8, score dict)。labels: 0 其它 / 1 绿化 / 2 道路 / 3 建筑屋面。

    判定顺序：绿化优先（含树冠规则）→ 道路 → 建筑屋面 → 其它。
    绿化优先于屋面，是因为树冠高程同样高于地面；把屋面规则放在前面会把林冠整片判成屋顶。
    代价：绿色屋面会被判为绿化，属已知边界。
    """
    p = DEFAULT_PARAMS if params is None else params
    g, rd = p["green"], p["road"]
    obs = np.isfinite(height)
    green = ((feat["exr"] <= g["exrMax"]) & (feat["sat"] >= g["satMin"])
             & (feat["val"] <= g["valMax"]) & (feat["vari"] >= g["variMin"]))
    if g.get("bMinusGMax") is not None:
        # 绿通道须为主导：蓝灰铺装（b>g）不算绿化
        green = green & ((feat["b"] - feat["g"]) <= g["bMinusGMax"])
    if g.get("exgMin") is not None:
        green = green & (feat["exg"] >= g["exgMin"])
    can = p.get("canopy")
    if can:
        rough = roughness(height, int(can.get("roughK", 2)))
        canopy = ((rough >= can["roughMin"]) & (feat["exr"] <= can["exrMax"])
                  & (feat["val"] <= can["valMax"]) & obs)
        green = green | canopy
    road = ((~green) & (feat["sat"] <= rd["satMax"]) & (feat["val"] >= rd["valMin"])
            & (feat["exr"] >= rd["exrMin"]) & (height <= rd["heightMaxM"]))
    m = p["morph"]
    if m.get("open"):
        green = dilate(erode(green, 1), 1)
        road = dilate(erode(road, 1), 1)
    if m.get("close"):
        green = erode(dilate(green, 1), 1)
        road = erode(dilate(road, 1), 1)
    if m.get("majority"):
        green = majority(green, 1, int(m.get("majorityMin", 4)))
        road = majority(road, 1, int(m.get("majorityMin", 4)))
    road = road & ~green
    labels = np.zeros(feat["r"].shape, dtype=np.uint8)
    labels[obs & road] = 2
    labels[obs & green] = 1
    labels[obs & building & ~green & ~road] = 3
    labels[~obs] = 0
    return labels, {"greenPx": int((labels == 1).sum()), "roadPx": int((labels == 2).sum()),
                    "buildingPx": int((labels == 3).sum()), "observedPx": int(obs.sum())}


# ---------------------------------------------------------------- 聚合

def aggregate(labels, z, cnt, extent, model_cell=1.0, res=0.25):
    """把像元分类聚合到 model_cell 米网格：每格各类占比 + 观测覆盖率 + 置信度。

    返回 (cells, stats)。cells 为逐格 dict 列表；stats 为全局口径。
    """
    x0, y0, x1, y1 = extent
    nx = int(np.ceil((x1 - x0) / model_cell))
    ny = int(np.ceil((y1 - y0) / model_cell))
    k = int(round(model_cell / res))
    nxr, nyr = labels.shape
    nx2, ny2 = (nxr // k) * k, (nyr // k) * k
    if nx2 == 0 or ny2 == 0:
        raise ValueError("网格边长大于影像范围，无法聚合")

    lab = labels[:nx2, :ny2].reshape(nx2 // k, k, ny2 // k, k)
    obs = np.isfinite(z[:nx2, :ny2]).reshape(nx2 // k, k, ny2 // k, k)
    cnts = cnt[:nx2, :ny2].reshape(nx2 // k, k, ny2 // k, k)
    obs_n = obs.sum(axis=(1, 3))
    tot = float(k * k)
    share = {}
    for code in (1, 2, 3):
        share[code] = (lab == code).sum(axis=(1, 3)) / tot
    med_samples = np.median(np.where(obs, cnts, 0), axis=(1, 3))
    cov = obs_n / tot

    cells = []
    for i in range(share[1].shape[0]):
        for j in range(share[1].shape[1]):
            if obs_n[i, j] == 0:
                continue
            gs, rs, bs = float(share[1][i, j]), float(share[2][i, j]), float(share[3][i, j])
            cov_ij = float(cov[i, j])
            best_code, best_share = max(((1, gs), (2, rs), (3, bs)), key=lambda t: t[1])
            # 置信度：主导类别占比 × 观测覆盖率（启发式分值，非标定概率）
            conf = round(best_share * cov_ij, 3)
            if cov_ij >= 0.85 and best_share >= 0.70:
                grade = "高"
            elif cov_ij >= 0.60 and best_share >= 0.50:
                grade = "中"
            else:
                grade = "低"
            c = {"ix": i, "iy": j,
                 "x": round(x0 + (i + 0.5) * model_cell, 3),
                 "y": round(y0 + (j + 0.5) * model_cell, 3),
                 "greenShare": round(gs, 4), "roadShare": round(rs, 4),
                 "buildingShare": round(bs, 4),
                 "coverage": round(cov_ij, 4),
                 "medianSamplesPerPixel": int(med_samples[i, j]),
                 "label": CLASS_NAMES[best_code] if best_share >= 0.5 else "其它",
                 "confidence": conf if best_share >= 0.5 else round(1.0 - best_share, 3),
                 "grade": grade}
            cells.append(c)

    observed = np.isfinite(z)
    obs_px = float(observed.sum())

    def frac(code):
        return round(float((labels == code).sum()) / obs_px, 4) if obs_px else None

    stats = {
        "observedPixels": int(obs_px),
        "observedAreaM2": round(obs_px * res * res, 1),
        "cells": len(cells),
        "green": frac(1),
        "road": frac(2),
        "building": frac(3),
        "other": round(float((observed & (labels == 0)).sum()) / obs_px, 4) if obs_px else None,
        "denominator": "模型覆盖范围内全部可观测朝上表面像元（含建筑屋面），按像元面积加权",
    }
    return cells, stats
