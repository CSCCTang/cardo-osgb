#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""16 · 场地要素提取：从几何里挖出「路面/硬地」「低矮体量」「空地」「水体」「破损线索」。

为什么有这一步：13 步之前的链路能出建筑形态，但有 5 类信息一直是空的——
道路与街巷、水体与场地积水、破损与结构隐患、违章搭建、闲置与低效空间。
这些在**语义层面**确实要影像（调研表 21 项里 5 项危房算法全是影像域），
但**几何代理量是能算的**，而且对投资决策有用：

| 要的东西 | 语义上靠什么 | 本步给的几何代理量 |
|---|---|---|
| 道路与街巷 | 影像道路分割 | **硬地面积占比 + 街巷条数 + 路幅宽度分布**（平坦连通体的几何特征） |
| 水体与积水 | 影像光谱 | **平坦低洼区**（高程低于邻域且坡度极低） |
| 破损隐患 | 近景/红外影像 | **异常几何线索**（屋面坡度突变、立面法向离群、模型缺陷） |
| 违章搭建 | 影像语义 | **低矮体量清单**（nDSM 0.3–2.0 m 的孤立块，带面积/高度/位置） |
| 闲置低效 | 影像语义 + 用地图 | **空地斑块**（连续硬地/裸地且无建筑的成片区域） |

**所有代理量都标注为「几何代理量」**，不是语义判定：
- 硬地 = 平坦 + 非建筑 + 非植被，**分不出车行道/人行道/广场**；
- 低矮体量 = 可能是违建，也可能是花坛、台阶、车辆、堆料，**要人工判**；
- 空地 = 无建筑的开阔地，**可能是场地，也可能是待建预留地**。

输入：过程文件/09-三维要素反演/栅格/栅格.npz（dsm/ground/ndsm/observed）
      过程文件/09-三维要素反演/三维要素反演.json（建筑实例、逐栋属性）
      过程文件/12-屋顶与立面/roof-segment/roof-segment.json（屋面平面，判破损线索）
输出：过程文件/16-场地要素/场地要素.json + .md
退出码：0 可用；2 缺输入；3 产出但不可用。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

sys.dont_write_bytecode = True

import numpy as np
from scipy import ndimage

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

SCHEMA = "cardo/lowair-site-features/v1"
OUT_DIR = os.path.join("过程文件", "16-场地要素")
JSON_NAME = "场地要素.json"
MD_NAME = "场地要素.md"

GRID_REL = os.path.join("过程文件", "09-三维要素反演", "栅格", "栅格.npz")
RECON_REL = os.path.join("过程文件", "09-三维要素反演", "三维要素反演.json")
ROOF_REL = os.path.join("过程文件", "12-屋顶与立面", "roof-segment", "roof-segment.json")

# ---- 判据参数（全部写进产物，可复核） ----
BUILDING_THRESHOLD_M = 3.0     # 与 09 的 buildingThresholdM 一致
MORPH = np.ones((3, 3), dtype=bool)

HARD_SLOPE_DEG = 3.0            # 硬地：坡度上限（°）
HARD_MIN_AREA_CELLS = 20        # 硬地连通块最小格数（20 m²）
STREET_MIN_WIDTH_M = 2.0        # 街巷最小宽度（m）
STREET_MAX_WIDTH_M = 15.0       # 街巷最大宽度（m，超出算广场/空地而非街巷）
STREET_MIN_LENGTH_M = 8.0       # 街巷最小长度（m）

LOW_MIN_H_M = 0.3               # 低矮体量：最低高度（m）
LOW_MAX_H_M = 2.0               # 低矮体量：最高高度（m）
LOW_MIN_CELLS = 4               # 低矮体量连通块最小格数（4 m²）

GROUND_OBJ_MIN_H_M = 0.10       # 贴地小体量：最低高度（m）——车辆/井盖/灯杆量级
GROUND_OBJ_MAX_H_M = 1.20       # 贴地小体量：最高高度（m）
GROUND_OBJ_MAX_CELLS = 1        # 贴地小体量：单块最大格数（1 m²）——小到只剩一个采样点

VOID_MIN_AREA_M2 = 200.0        # 空地斑块最小面积（m²）

DEPRESSION_DEPTH_M = 0.3        # 水体/积水：低于邻域的深度阈值（m）
DEPRESSION_FLAT_DEG = 2.0       # 水体/积水：坡度上限（°）
DEPRESSION_MIN_CELLS = 6        # 水体/积水连通块最小格数
DEPRESSION_MAX_DEPTH_M = 3.0    # 水体/积水：最大深度（m）——超过此值多半是地面参考面误差
DEPRESSION_MIN_DIST_BUILDING_M = 2.0   # 水体/积水：离建筑至少这么远（贴建筑的是边缘误差）


def read_json(path):
    with open(path, encoding="utf-8-sig") as fh:
        return json.load(fh)


def write_json(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def slope_deg(dsm: np.ndarray, cell: float) -> np.ndarray:
    """地面坡度（度）。用 np.gradient 求高程梯度，等价于有限差分中心差分。"""
    gy, gx = np.gradient(dsm, cell)
    return np.degrees(np.arctan(np.hypot(gx, gy)))


def building_mask(ndsm: np.ndarray, observed: np.ndarray) -> np.ndarray:
    """与 09 一致的建筑掩膜：ndsm>3.0 m 且观测格，close(3×3)+open(3×3)。"""
    mask = (ndsm > BUILDING_THRESHOLD_M) & observed
    mask = ndimage.binary_closing(mask, MORPH)
    return ndimage.binary_opening(mask, MORPH)


def vegetation_mask(ndsm: np.ndarray, rough: np.ndarray, observed: np.ndarray,
                    building: np.ndarray) -> np.ndarray:
    """植被代理：非建筑 + ndsm≥1.5 m + 3×3 粗糙度≥0.8 m（与 11 步 pc.classify 同口径）。"""
    return (ndsm >= 1.5) & (rough >= 0.8) & observed & (~building)


def window_roughness(dsm: np.ndarray, observed: np.ndarray, win: int = 3) -> np.ndarray:
    """3×3 窗口内 dsm 标准差（与 11 步 pc.classify 同口径），仅观测格有值。"""
    from numpy.lib.stride_tricks import sliding_window_view
    sw = sliding_window_view(dsm, (win, win))
    mw = sliding_window_view(observed, (win, win))
    valid = mw.all(axis=(2, 3))
    out = np.full(dsm.shape, np.nan)
    h = win // 2
    std = sw.std(axis=(2, 3))
    out[h:h + sw.shape[0], h:h + sw.shape[1]] = np.where(valid, std, np.nan)
    return out


def local_mean(values: np.ndarray, mask: np.ndarray, size: int) -> np.ndarray:
    """掩膜内局部均值（用 nan 屏蔽非观测格）。"""
    vals = np.where(mask, values, np.nan)
    kernel = np.ones((size, size))
    count = ndimage.convolve(np.isfinite(vals).astype(float), kernel, mode="nearest")
    total = ndimage.convolve(np.nan_to_num(vals), kernel, mode="nearest")
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(count > 0, total / np.maximum(count, 1), np.nan)


def components(mask: np.ndarray):
    """连通块标记 + 各块格数。"""
    labels, n = ndimage.label(mask)
    if n == 0:
        return labels, []
    sizes = ndimage.sum(mask, labels, range(1, n + 1))
    return labels, list(sizes)


def analyse(workspace: str) -> dict:
    grid_path = os.path.join(workspace, GRID_REL)
    recon_path = os.path.join(workspace, RECON_REL)
    for label, path in (("栅格", grid_path), ("反演结果", recon_path)):
        if not os.path.isfile(path):
            raise SystemExit(f"[16] 缺{label}：{path}（先跑 09）")
    z = np.load(grid_path)
    dsm, ground, ndsm = z["dsm"], z["ground"], z["ndsm"]
    observed, cell = z["observed"], float(z["cell"][0])
    x0, y0 = float(z["x0"][0]), float(z["y0"][0])
    recon = read_json(recon_path)

    slope = slope_deg(dsm, cell)
    rough = window_roughness(dsm, observed)
    building = building_mask(ndsm, observed)
    vegetation = vegetation_mask(ndsm, rough, observed, building)
    non_building = observed & (~building)

    observed_n = int(observed.sum())
    gaps = []

    # ---------- 1) 硬地（路面/铺装代理量） ----------
    hard = non_building & (~vegetation) & (slope <= HARD_SLOPE_DEG)
    labels, sizes = components(hard)
    hard_patches = []
    for i, size in enumerate(sizes, start=1):
        if size < HARD_MIN_AREA_CELLS:
            continue
        m = labels == i
        ys, xs = np.nonzero(m)
        # 等效矩形宽度：面积 / 主轴长度。用 extent 的短边近似宽度更稳。
        h_ext = ys.max() - ys.min() + 1
        w_ext = xs.max() - xs.min() + 1
        hard_patches.append({
            "id": f"H-{len(hard_patches) + 1:03d}",
            "areaM2": round(float(size) * cell * cell, 2),
            "cells": int(size),
            "widthM": round(float(min(h_ext, w_ext)) * cell, 2),
            "lengthM": round(float(max(h_ext, w_ext)) * cell, 2),
            "centroidLocal": [round(float(xs.mean()) * cell + x0, 2),
                              round(float(ys.mean()) * cell + y0, 2)],
            "meanSlopeDeg": round(float(slope[m].mean()), 2),
        })
    hard_patches.sort(key=lambda p: -p["areaM2"])
    hard_area = sum(p["areaM2"] for p in hard_patches)

    # 街巷：窄而有一定长度的硬地连片区（宽 ≤15 m 且长 ≥8 m）
    streets = [p for p in hard_patches
               if STREET_MIN_WIDTH_M <= p["widthM"] <= STREET_MAX_WIDTH_M
               and p["lengthM"] >= STREET_MIN_LENGTH_M]
    street_widths = sorted(p["widthM"] for p in streets)

    # ---------- 2) 低矮体量（违建/附属构筑物候选） ----------
    low = (ndsm > LOW_MIN_H_M) & (ndsm < LOW_MAX_H_M) & non_building
    low_labels, low_sizes = components(low)
    low_items = []
    for i, size in enumerate(low_sizes, start=1):
        if size < LOW_MIN_CELLS:
            continue
        m = low_labels == i
        ys, xs = np.nonzero(m)
        low_items.append({
            "id": f"L-{len(low_items) + 1:03d}",
            "areaM2": round(float(size) * cell * cell, 2),
            "cells": int(size),
            "heightMeanM": round(float(ndsm[m].mean()), 2),
            "heightMaxM": round(float(ndsm[m].max()), 2),
            "centroidLocal": [round(float(xs.mean()) * cell + x0, 2),
                              round(float(ys.mean()) * cell + y0, 2)],
            "nearBuildingM": round(float(np.min(
                ndimage.distance_transform_edt(~building)[m])) * cell, 2),
        })
    low_items.sort(key=lambda p: -p["areaM2"])

    # ---------- 3) 空地斑块（闲置/低效候选） ----------
    # 定义：无建筑、无植被、坡度平缓、面积够大的连续区
    void_candidate = non_building & (~vegetation) & (slope <= HARD_SLOPE_DEG)
    v_labels, v_sizes = components(void_candidate)
    voids = []
    for i, size in enumerate(v_sizes, start=1):
        area = float(size) * cell * cell
        if area < VOID_MIN_AREA_M2:
            continue
        m = v_labels == i
        ys, xs = np.nonzero(m)
        voids.append({
            "id": f"V-{len(voids) + 1:03d}",
            "areaM2": round(area, 2),
            "cells": int(size),
            "widthM": round(float(xs.max() - xs.min() + 1) * cell, 2),
            "lengthM": round(float(ys.max() - ys.min() + 1) * cell, 2),
            "centroidLocal": [round(float(xs.mean()) * cell + x0, 2),
                              round(float(ys.mean()) * cell + y0, 2)],
            "meanSlopeDeg": round(float(slope[m].mean()), 2),
        })
    voids.sort(key=lambda p: -p["areaM2"])

    # ---------- 4) 水体/积水（平坦低洼代理量） ----------
    # 三重排除，否则会把建筑边缘的地面参考面误差当成水体：
    #   ① 深度 >3 m —— 场地整体高差能到几十米，这种深度是地面拟合误差不是积水；
    #   ② 离建筑 <2 m —— 贴墙的凹陷是 nDSM 的边界效应；
    #   ③ 要求坡度极低 —— 斜坡上的"洼"是地形不是积水。
    dist_building = ndimage.distance_transform_edt(~building) * cell
    base = local_mean(dsm, observed, size=31)          # 约 31 m 邻域基准面
    depth = base - dsm
    depression = (observed & (depth > DEPRESSION_DEPTH_M) & (depth <= DEPRESSION_MAX_DEPTH_M)
                  & (slope <= DEPRESSION_FLAT_DEG)
                  & (dist_building >= DEPRESSION_MIN_DIST_BUILDING_M))
    d_labels, d_sizes = components(depression)
    depressions = []
    for i, size in enumerate(d_sizes, start=1):
        if size < DEPRESSION_MIN_CELLS:
            continue
        m = d_labels == i
        ys, xs = np.nonzero(m)
        depressions.append({
            "id": f"W-{len(depressions) + 1:03d}",
            "areaM2": round(float(size) * cell * cell, 2),
            "cells": int(size),
            "depthMeanM": round(float((base[m] - dsm[m]).mean()), 2),
            "depthMaxM": round(float((base[m] - dsm[m]).max()), 2),
            "centroidLocal": [round(float(xs.mean()) * cell + x0, 2),
                              round(float(ys.mean()) * cell + y0, 2)],
        })
    depressions.sort(key=lambda p: -p["areaM2"])

    # ---------- 5) 贴地小体量（车辆/井盖/灯杆的密度代理） ----------
    # 管线拍不到（地下），但**地面上的小体量**在 2 cm/像素的模型里是可见的：
    # 车辆、井盖、灯杆、隔离墩都落在「贴地 + 极小面积」这个格子里。
    # 这里只给**密度**（每百平米多少个），不给「是什么」——识别类别要影像。
    ground_obj = ((ndsm > GROUND_OBJ_MIN_H_M) & (ndsm < GROUND_OBJ_MAX_H_M)
                  & non_building)
    go_labels, go_sizes = components(ground_obj)
    ground_objects = []
    for i, size in enumerate(go_sizes, start=1):
        if size > GROUND_OBJ_MAX_CELLS:
            continue
        m = go_labels == i
        ys, xs = np.nonzero(m)
        ground_objects.append({
            "id": f"G-{len(ground_objects) + 1:03d}",
            "cells": int(size),
            "heightM": round(float(ndsm[m].mean()), 2),
            "centroidLocal": [round(float(xs.mean()) * cell + x0, 2),
                              round(float(ys.mean()) * cell + y0, 2)],
        })
    observed_area = observed_n * cell * cell
    ground_obj_density = round(len(ground_objects) / (observed_area / 100.0), 2) \
        if observed_area else None

    # ---------- 6) 破损线索（几何异常，非危房判定） ----------
    roof_path = os.path.join(workspace, ROOF_REL)
    roof_items = read_json(roof_path).get("items") or [] if os.path.isfile(roof_path) else []
    if not roof_items:
        gaps.append({"code": "SITE-GAP-001", "what": "无屋面平面数据，屋面异常线索未算",
                     "why": f"{ROOF_REL} 不存在或无 items（先跑 12）", "need": "跑 12 步后重跑",
                     "owner": "pipeline"})
    # 屋面形态离群：同一栋内屋面倾角离散大 → 屋面形态复杂/变形嫌疑
    # 判据用**倾角离散**为主（几何形状异常），rms 仅作参考记录不单独触发——
    # rms 大可能只是屋面本身复杂（如折线屋顶），不是变形。
    ROOF_TILT_SPREAD_DEG = 15.0
    roof_by_bld = {}
    for item in roof_items:
        bid = item.get("buildingId")
        if bid:
            roof_by_bld.setdefault(bid, []).append(item)
    roof_anomalies = []
    for bid, planes in roof_by_bld.items():
        tilts = [p.get("tiltDeg") or 0.0 for p in planes]
        rms = [p.get("rmsM") or 0.0 for p in planes]
        if len(tilts) < 2:
            continue
        spread = float(np.std(tilts))
        mean_rms = float(np.mean(rms))
        if spread >= ROOF_TILT_SPREAD_DEG:
            roof_anomalies.append({"buildingId": bid, "planeCount": len(planes),
                                    "tiltSpreadDeg": round(spread, 2),
                                    "meanPlaneRmsM": round(mean_rms, 3),
                                    "reason": f"同栋屋面倾角离散 {spread:.1f}° ≥ {ROOF_TILT_SPREAD_DEG}°，"
                                              f"屋面形态异常（塌陷/破损嫌疑，需人工看）"})
    roof_anomalies.sort(key=lambda a: -a["tiltSpreadDeg"])

    # 逐栋高度离群：栋高 P50 显著偏离片区中位 → 体量异常（大体量突兀）
    heights = [(b.get("id"), b.get("heightP50")) for b in recon.get("buildings") or []
               if b.get("heightP50") is not None]
    height_anomalies = []
    if len(heights) >= 3:
        med = float(np.median([h for _, h in heights]))
        for bid, h in heights:
            if med > 0 and abs(h - med) / med >= 0.5:
                height_anomalies.append({"buildingId": bid, "heightP50M": round(h, 2),
                                         "areaMedianP50M": round(med, 2),
                                         "deviation": round((h - med) / med, 3),
                                         "reason": f"栋高 P50 {h:.1f} m 偏离片区中位 {med:.1f} m "
                                                   f"达 {abs((h - med) / med):.0%}，体量异常"
                                                   f"（拆除量/加建嫌疑，需人工看）"})

    damage_clues = len(roof_anomalies) + len(height_anomalies)

    summary = {
        "observedCells": observed_n,
        "observedAreaM2": round(observed_n * cell * cell, 2),
        "buildingCells": int(building.sum()),
        "vegetationCells": int(vegetation.sum()),
        # 硬地
        "hardAreaM2": round(hard_area, 2),
        "hardShare": round(hard_area / (observed_n * cell * cell), 4),
        "hardPatchCount": len(hard_patches),
        "hardLargestPatchM2": hard_patches[0]["areaM2"] if hard_patches else 0.0,
        # 街巷
        "streetCount": len(streets),
        "streetWidthMinM": street_widths[0] if street_widths else None,
        "streetWidthMedianM": street_widths[len(street_widths) // 2] if street_widths else None,
        "streetWidthMaxM": street_widths[-1] if street_widths else None,
        # 低矮体量
        "lowObjectCount": len(low_items),
        "lowObjectAreaM2": round(sum(p["areaM2"] for p in low_items), 2),
        "lowObjectLargestM2": low_items[0]["areaM2"] if low_items else 0.0,
        "lowObjectHeightMaxM": max((p["heightMaxM"] for p in low_items), default=None),
        # 贴地小体量（车辆/井盖/灯杆密度代理）
        "groundObjectCount": len(ground_objects),
        "groundObjectDensityPer100M2": ground_obj_density,
        "groundObjectHeightMaxM": max((p["heightM"] for p in ground_objects), default=None),
        # 空地
        "voidPatchCount": len(voids),
        "voidAreaM2": round(sum(p["areaM2"] for p in voids), 2),
        "voidShare": round(sum(p["areaM2"] for p in voids) / (observed_n * cell * cell), 4),
        "voidLargestM2": voids[0]["areaM2"] if voids else 0.0,
        # 水体
        "depressionCount": len(depressions),
        "depressionAreaM2": round(sum(p["areaM2"] for p in depressions), 2),
        "depressionMaxDepthM": max((p["depthMaxM"] for p in depressions), default=None),
        # 破损线索
        "damageClueCount": damage_clues,
        "roofAnomalyCount": len(roof_anomalies),
        "heightAnomalyCount": len(height_anomalies),
    }

    return {
        "schema": SCHEMA, "schemaVersion": 1,
        "generatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "producer": {"name": "cardo-osgb", "version": "0.1.0"},
        "methodVersion": "site-features-v1",
        "inputs": {"grid": GRID_REL.replace("\\", "/"), "recon": RECON_REL.replace("\\", "/"),
                   "roof": ROOF_REL.replace("\\", "/") if os.path.isfile(roof_path) else None},
        "params": {
            "cellM": cell, "buildingThresholdM": BUILDING_THRESHOLD_M,
            "morphology": "close(3x3)+open(3x3)",
            "hardSlopeDeg": HARD_SLOPE_DEG, "hardMinAreaCells": HARD_MIN_AREA_CELLS,
            "streetWidthRangeM": [STREET_MIN_WIDTH_M, STREET_MAX_WIDTH_M],
            "streetMinLengthM": STREET_MIN_LENGTH_M,
            "lowHeightRangeM": [LOW_MIN_H_M, LOW_MAX_H_M], "lowMinCells": LOW_MIN_CELLS,
            "groundObjHeightRangeM": [GROUND_OBJ_MIN_H_M, GROUND_OBJ_MAX_H_M],
            "groundObjMaxCells": GROUND_OBJ_MAX_CELLS,
            "voidMinAreaM2": VOID_MIN_AREA_M2,
            "roofTiltSpreadThresholdDeg": ROOF_TILT_SPREAD_DEG,
            "depressionDepthM": DEPRESSION_DEPTH_M, "depressionFlatDeg": DEPRESSION_FLAT_DEG,
            "depressionMinCells": DEPRESSION_MIN_CELLS,
            "depressionMaxDepthM": DEPRESSION_MAX_DEPTH_M,
            "depressionMinDistBuildingM": DEPRESSION_MIN_DIST_BUILDING_M,
            "depressionExclusions": "深度>3 m（场地高差非积水）、离建筑<2 m（nDSM 边界效应）、"
                                    "坡度>2°（斜坡非积水）",
            "vegetationRule": "非建筑 & ndsm>=1.5 m & 3x3 粗糙度>=0.8 m（同 11 步 pc.classify）",
        },
        "summary": summary,
        "hardPatches": hard_patches,
        "lowObjects": low_items,
        "groundObjects": ground_objects,
        "voidPatches": voids,
        "depressions": depressions,
        "roofAnomalies": roof_anomalies,
        "heightAnomalies": height_anomalies,
        "gaps": gaps,
        "quality": {
            "usable": True,
            "reason": "全部为**几何代理量**：能从倾斜模型直接量出，但不等同于语义判定。"
                      "硬地分不出车行道/人行道/广场；低矮体量需人工判是违建还是花坛/车辆；"
                      "空地可能是待建预留地；破损线索只是几何异常，不是危房结论。",
        },
        "interpretNote": "硬地/空地/低矮体量/水体四类**互相重叠**（同一批格子可同时算进硬地与空地），"
                         "不是互斥分类，不可相加。",
    }


def render_md(doc: dict) -> str:
    s = doc["summary"]
    lines = ["# 场地要素提取（几何代理量）", "",
             f"生成时间：{doc['generatedAt']}　契约：{doc['schema']}", "",
             "> **全部是几何代理量，不是语义判定。** "
             "能从倾斜模型直接量出来，但硬地分不出车行道/人行道、低矮体量要人工判、"
             "破损线索不是危房结论。", "",
             "## 摘要", "",
             "| 项 | 值 | 说明 |", "|---|---|---|",
             f"| 硬地面积 | {s['hardAreaM2']} m²（{s['hardShare'] * 100:.1f}%） | "
             f"坡度<{doc['params']['hardSlopeDeg']}° 且非建筑非植被的连通区，共 {s['hardPatchCount']} 块，"
             f"最大 {s['hardLargestPatchM2']} m² |",
             f"| 街巷条数 | {s['streetCount']} 条 | 宽 "
             f"{s['streetWidthMinM']}–{s['streetWidthMaxM']} m，中位 {s['streetWidthMedianM']} m |",
             f"| 低矮体量 | {s['lowObjectCount']} 处 / {s['lowObjectAreaM2']} m² | "
             f"nDSM {doc['params']['lowHeightRangeM'][0]}–{doc['params']['lowHeightRangeM'][1]} m 的孤立块，"
             f"最大 {s['lowObjectLargestM2']} m² |",
             f"| 贴地小体量 | {s['groundObjectCount']} 处 / 密度 {s['groundObjectDensityPer100M2']} 个/100m² | "
             f"nDSM {doc['params']['groundObjHeightRangeM'][0]}–{doc['params']['groundObjHeightRangeM'][1]} m "
             f"且面积 ≤{doc['params']['groundObjMaxCells']} 格——车辆/井盖/灯杆的**密度代理** |",
             f"| 空地斑块 | {s['voidPatchCount']} 块 / {s['voidAreaM2']} m² | "
             f"（{s['voidShare'] * 100:.1f}%），最大 {s['voidLargestM2']} m² |",
             f"| 低洼/水体候选 | {s['depressionCount']} 处 / {s['depressionAreaM2']} m² | "
             f"最深 {s['depressionMaxDepthM']} m |",
             f"| 破损线索 | {s['damageClueCount']} 条 | 屋面倾角离群 {s['roofAnomalyCount']} + "
             f"栋高离群 {s['heightAnomalyCount']} |",
             ""]
    if doc.get("hardPatches"):
        lines += ["## 硬地斑块（前 10）", "",
                  "| id | 面积 m² | 宽 m | 长 m | 平均坡度° | 中心（局部 m） |", "|---|---|---|---|---|---|"]
        for p in doc["hardPatches"][:10]:
            lines.append(f"| {p['id']} | {p['areaM2']} | {p['widthM']} | {p['lengthM']} | "
                         f"{p['meanSlopeDeg']} | {p['centroidLocal']} |")
        lines.append("")
    if doc.get("lowObjects"):
        lines += ["## 低矮体量候选（前 15）", "",
                  "| id | 面积 m² | 均高 m | 最高 m | 距建筑 m | 中心（局部 m） |", "|---|---|---|---|---|---|"]
        for p in doc["lowObjects"][:15]:
            lines.append(f"| {p['id']} | {p['areaM2']} | {p['heightMeanM']} | {p['heightMaxM']} | "
                         f"{p['nearBuildingM']} | {p['centroidLocal']} |")
        lines.append("")
    if doc.get("groundObjects"):
        lines += ["## 贴地小体量（车辆/井盖/灯杆密度代理）", "",
                  f"共 {len(doc['groundObjects'])} 处。1 m 栅格下只剩一个采样点，"
                  "**只给密度，不给类别**——识别是什么要影像。", "",
                  "| id | 高度 m | 中心（局部 m） |", "|---|---|---|"]
        for p in doc["groundObjects"][:20]:
            lines.append(f"| {p['id']} | {p['heightM']} | {p['centroidLocal']} |")
        lines.append("")
    if doc.get("voidPatches"):
        lines += ["## 空地斑块（前 10）", "", "| id | 面积 m² | 宽 m | 长 m | 中心（局部 m） |",
                  "|---|---|---|---|---|"]
        for p in doc["voidPatches"][:10]:
            lines.append(f"| {p['id']} | {p['areaM2']} | {p['widthM']} | {p['lengthM']} | "
                         f"{p['centroidLocal']} |")
        lines.append("")
    if doc.get("depressions"):
        lines += ["## 低洼/水体候选", "", "| id | 面积 m² | 均深 m | 最深 m | 中心（局部 m） |",
                  "|---|---|---|---|---|"]
        for p in doc["depressions"]:
            lines.append(f"| {p['id']} | {p['areaM2']} | {p['depthMeanM']} | {p['depthMaxM']} | "
                         f"{p['centroidLocal']} |")
        lines.append("")
    if doc.get("roofAnomalies") or doc.get("heightAnomalies"):
        lines += ["## 破损线索（几何异常，非危房判定）", ""]
        if doc.get("roofAnomalies"):
            lines += ["**屋面形态离群**", "", "| 建筑 | 屋面片数 | 倾角离散° | 平均拟合残差 m |",
                      "|---|---|---|---|"]
            for a in doc["roofAnomalies"]:
                lines.append(f"| {a['buildingId']} | {a['planeCount']} | {a['tiltSpreadDeg']} | "
                             f"{a['meanPlaneRmsM']} |")
            lines.append("")
        if doc.get("heightAnomalies"):
            lines += ["**体量离群**", "", "| 建筑 | 栋高 P50 m | 片区中位 m | 偏离 |", "|---|---|---|---|"]
            for a in doc["heightAnomalies"]:
                lines.append(f"| {a['buildingId']} | {a['heightP50M']} | {a['areaMedianP50M']} | "
                             f"{a['deviation']:+.0%} |")
            lines.append("")
    lines += ["## 局限（必须随表一起读）", "",
              "1. **四类互相重叠**：硬地 / 空地 / 低矮体量 / 水体是同一批格子的不同切法，不是互斥分类，不可相加。",
              "2. **硬地分不出材质**：车行道、人行道、广场、屋顶平台的铺装在几何上一样。",
              "3. **低矮体量需人工判**：可能是违建棚屋，也可能是花坛、台阶、车辆、堆料。",
              "4. **空地不等于闲置**：可能是待建预留地、拆迁场地、施工临时堆场。",
              "5. **破损线索不是危房**：倾斜模型看不到结构内部，几何异常也可能是建模问题。",
              "6. **街巷条数是几何连通性**：能识别成片硬地的窄通道，但断头路、小区内部道路的连通判定受遮挡影响。",
              ""]
    if doc.get("gaps"):
        lines += ["## 缺项", "", "| code | 缺什么 | 为什么 | 需要什么 | owner |",
                  "|---|---|---|---|---|"]
        for g in doc["gaps"]:
            lines.append(f"| {g['code']} | {g['what']} | {g['why']} | {g['need']} | {g['owner']} |")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="16 · 场地要素提取（几何代理量）")
    ap.add_argument("--workspace", default=".")
    ap.add_argument("--out", default=OUT_DIR)
    args = ap.parse_args()

    root = os.path.abspath(args.workspace)
    out_dir = args.out if os.path.isabs(args.out) else os.path.join(root, args.out)
    os.makedirs(out_dir, exist_ok=True)
    json_path = os.path.join(out_dir, JSON_NAME)
    md_path = os.path.join(out_dir, MD_NAME)
    for stale in (json_path, md_path):
        if os.path.isfile(stale):
            os.remove(stale)

    doc = analyse(root)
    write_json(json_path, doc)
    with open(md_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(render_md(doc))

    s = doc["summary"]
    print(f"[16] 完成：硬地 {s['hardAreaM2']} m²（{s['hardShare'] * 100:.1f}%）/ "
          f"街巷 {s['streetCount']} 条 / 低矮体量 {s['lowObjectCount']} 处 / "
          f"空地 {s['voidPatchCount']} 块 {s['voidAreaM2']} m² / "
          f"低洼 {s['depressionCount']} 处 / 贴地小体量 {s['groundObjectCount']} 处 / "
          f"破损线索 {s['damageClueCount']} 条")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
