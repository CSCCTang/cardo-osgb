#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""09 · 三维要素反演：从 b3dm 顶点反推 DSM / DTM / nDSM，再出逐栋建筑与 GeoJSON。

只读标准容器（b3dm 头 / GLB / glTF accessor），**不解析 OSGB 字节**；不跨技能 import。
算法全部是确定性几何（无深度学习、无随机数），每一步的参数都写进产物 JSON。

流程：
    顶点（只取 NORMAL.z ≥ 0.5 的朝上顶点）→ 1 m 栅格（每格最高 z = DSM、最低 z、观测数）→
    最近邻补洞 → 地面参考面 = 窗口内每格最高点的 P10（±15 m，与 06 一致）→
    nDSM = DSM − 地面参考面 → mask = nDSM > 阈值 → 闭运算 + 开运算去噪 →
    分水岭实例化（种子 = nDSM 平滑面局部极大值，间距 6 m）→ 面积过滤 →
    逐栋统计 / 凸包 + 最小外接矩形 → GeoJSON(EPSG:4544) + 逐栋属性.json +
    栅格/栅格.npz + 三维要素反演.json + 反演报告.md

用法（工作区根）：
    python Skills/09-三维要素反演/scripts/run_reconstruct.py
    python Skills/09-三维要素反演/scripts/run_reconstruct.py --cell-m 0.5 --ground-window-m 18
退出码：0 通过；2 缺 cardo-lowair-model.json；3 反演不可用或闸门不过；4 产物已存在未给 --force。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import warnings

sys.dont_write_bytecode = True
from datetime import datetime

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
from scipy import ndimage

SCHEMA, SCHEMA_VERSION = "cardo/lowair-reconstruct/v1", 1
GEOJSON_SCHEMA = "cardo/lowair-vector/v1"
PRODUCER, PRODUCER_VERSION = "cardo-osgb", "0.1.0"
METHOD_VERSION = "reconstruct-v1"
MODEL_DEFAULT = os.path.join("过程文件", "03-OSGB转3DTiles", "转换产物", "cardo-lowair-model.json")
OUT_DEFAULT = os.path.join("过程文件", "09-三维要素反演")
NEG_INF = -1e30
POS_INF = 1e30
DTM_WINDOWS_M = (12.0, 15.0, 18.0)   # 区间来源：三档地面窗口
GROUND_Q = 10.0                      # 地面参考面：窗口内每格最高点的 P10
SPLIT_MIN_DIST_M = 6.0               # 分水岭种子最小间距（米）
FLOOR_HEIGHTS = (2.8, 3.3)
DEFAULT_FLOOR_M = 3.0
BUILDING_KEYS = ("id", "areaM2", "heightP50", "heightP90", "heightMax", "floorsEst",
                 "floorsRange", "centroidXY", "footprint", "confidence", "gradeReasons")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def rel_ref(path, base):
    """相对引用（正斜杠）；跨盘失败时退回文件名，绝不落盘符绝对路径。"""
    try:
        rel = os.path.relpath(os.path.abspath(path), os.path.abspath(base))
    except ValueError:
        return os.path.basename(path)
    return rel.replace("\\", "/")


def r2(x): return None if x is None else round(float(x), 2)
def r3(x): return None if x is None else round(float(x), 3)
def r6(x): return None if x is None else round(float(x), 6)


def pct(values, p):
    """最近秩分位（对同一输入确定，不引入插值浮动）。"""
    if len(values) == 0:
        return None
    arr = np.sort(np.asarray(values, dtype=np.float64))
    k = int(round((arr.size - 1) * p / 100.0))
    return float(arr[max(0, min(arr.size - 1, k))])


def stat(values) -> dict:
    arr = np.asarray(values, dtype=np.float64)
    if arr.size == 0:
        return {"n": 0, "p50": None, "p90": None, "max": None, "mean": None, "std": None}
    return {"n": int(arr.size), "p50": r2(np.percentile(arr, 50)), "p90": r2(np.percentile(arr, 90)),
            "max": r2(arr.max()), "mean": r2(arr.mean()), "std": r2(arr.std())}


# ---------------------------------------------------------------- 标准容器读取
def gltf_of_b3dm(path: str):
    """读 b3dm 头 → 内嵌 GLB → (glTF JSON, BIN 字节)。只碰标准容器层。"""
    with open(path, "rb") as fh:
        raw = fh.read()
    if raw[:4] != b"b3dm" or len(raw) < 28:
        raise ValueError("不是 b3dm 容器或头不足 28 B")
    declared = int.from_bytes(raw[8:12], "little")
    if declared != len(raw):
        raise ValueError(f"头部声明 {declared} B，实际 {len(raw)} B（疑似截断）")
    ftj, ftb, btj, btb = (int.from_bytes(raw[i:i + 4], "little") for i in range(12, 28, 4))
    glb = raw[28 + ftj + ftb + btj + btb:]
    if glb[:4] != b"glTF" or len(glb) < 20:
        raise ValueError("b3dm 内未找到 GLB")
    total, pos, gltf, binary = int.from_bytes(glb[8:12], "little"), 12, None, b""
    while pos + 8 <= min(total, len(glb)):
        size = int.from_bytes(glb[pos:pos + 4], "little")
        kind, body = glb[pos + 4:pos + 8], glb[pos + 8:pos + 8 + size]
        if kind == b"JSON":
            gltf = json.loads(body.decode("utf-8"))
        elif kind[:3] == b"BIN":
            binary = body
        pos += 8 + size
    if gltf is None:
        raise ValueError("GLB 缺 JSON chunk")
    return gltf, binary


def attribute_view(gltf, binary, index):
    """float32 VEC3 accessor → (memoryview/flat, count)；只支持本机小端。"""
    if sys.byteorder != "little":
        raise SystemExit("仅支持小端主机（float32 视图按本机字节序解释）")
    accs, views = gltf.get("accessors") or [], gltf.get("bufferViews") or []
    if not isinstance(index, int) or not 0 <= index < len(accs):
        return None
    acc, bvi = accs[index], accs[index].get("bufferView")
    if acc.get("componentType") != 5126 or acc.get("type") != "VEC3":
        return None
    if not isinstance(bvi, int) or not 0 <= bvi < len(views):
        return None
    count = int(acc.get("count") or 0)
    bv = views[bvi]
    off = int(bv.get("byteOffset", 0)) + int(acc.get("byteOffset", 0))
    stride = int(bv.get("byteStride") or 12)
    if count <= 0 or off + (count - 1) * stride + 12 > len(binary):
        return None
    if stride == 12:
        return memoryview(binary)[off:off + count * 12].cast("f"), count
    mv, flat = memoryview(binary), []
    for i in range(count):
        flat.extend(mv[off + i * stride:off + i * stride + 12].cast("f"))
    return flat, count


def _as_array(view, count):
    arr = (np.frombuffer(view, dtype="<f4", count=count * 3)
           if isinstance(view, memoryview) else np.asarray(view, dtype=np.float32))
    return arr.reshape(-1, 3)


def primitive_arrays(gltf, binary):
    """[(POSITION (N,3), NORMAL (N,3) 或 None)] —— 顶点与法线一一对应。"""
    out = []
    for mesh in gltf.get("meshes") or []:
        for prim in mesh.get("primitives") or []:
            attrs = prim.get("attributes") or {}
            pos = attribute_view(gltf, binary, attrs.get("POSITION"))
            if not pos:
                continue
            nrm = attribute_view(gltf, binary, attrs.get("NORMAL"))
            out.append((_as_array(*pos), _as_array(*nrm) if nrm and nrm[1] == pos[1] else None))
    return out


# ---------------------------------------------------------------- 栅格
def build_grids(model: dict, model_path: str, cell_m: float) -> dict:
    """一次扫描同时得到每格最高 z / 最低 z / 顶点数。

    DSM 只用朝上的顶点（NORMAL.z ≥ 0.5），与 06 口径一致：墙面顶点会把相邻建筑在格子里
    连成一片，导致连通域整街区合并、逐栋高度被拉低。
    """
    bbox = model["geometry"]["bboxLocal"]
    x0, y0 = bbox["min"][0], bbox["min"][1]
    x1, y1 = bbox["max"][0], bbox["max"][1]
    nx = max(1, int(math.ceil((x1 - x0) / cell_m)))
    ny = max(1, int(math.ceil((y1 - y0) / cell_m)))
    zmax = np.full((nx, ny), NEG_INF, dtype=np.float64)
    zmin = np.full((nx, ny), POS_INF, dtype=np.float64)
    count = np.zeros((nx, ny), dtype=np.int64)
    root = os.path.dirname(os.path.abspath(model_path))
    tiles = [a["path"] for a in model.get("artifacts") or []
             if str(a.get("path", "")).lower().endswith(".b3dm")]
    parsed, failures, vertices, top_vertices, outside, no_normal = 0, [], 0, 0, 0, 0
    for rel in tiles:
        try:
            gltf, binary = gltf_of_b3dm(os.path.join(root, rel))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            failures.append({"path": rel, "error": f"{type(exc).__name__}: {exc}"})
            continue
        parsed += 1
        for pos, nrm in primitive_arrays(gltf, binary):
            vertices += int(pos.shape[0])
            if nrm is None:                 # 没法线就全用（并在 gaps 里说明口径降级）
                no_normal += int(pos.shape[0])
                picked = pos
            else:
                picked = pos[nrm[:, 2] >= 0.5]
            top_vertices += int(picked.shape[0])
            inside = ((picked[:, 0] >= x0) & (picked[:, 0] <= x1)
                      & (picked[:, 1] >= y0) & (picked[:, 1] <= y1))
            outside += int((~inside).sum())
            if not inside.any():
                continue
            xs, ys, zs = picked[inside, 0], picked[inside, 1], picked[inside, 2]
            ix = np.clip(((xs - x0) / cell_m).astype(np.int64), 0, nx - 1)
            iy = np.clip(((ys - y0) / cell_m).astype(np.int64), 0, ny - 1)
            np.maximum.at(zmax, (ix, iy), zs)
            np.minimum.at(zmin, (ix, iy), zs)
            np.add.at(count, (ix, iy), 1)
    return {"nx": nx, "ny": ny, "x0": x0, "y0": y0, "x1": x1, "y1": y1, "cell": float(cell_m),
            "zmax": zmax, "zmin": zmin, "count": count,
            "parsed": parsed, "failures": failures, "vertices": vertices,
            "topVertices": top_vertices, "noNormalVertices": no_normal, "outside": outside,
            "tiles": len(tiles)}


def nearest_fill(values: np.ndarray, observed: np.ndarray) -> np.ndarray:
    """空洞格用最近观测格的值填（为滤波提供一个连续面，不参与统计）。"""
    if observed.all():
        return values
    idx = ndimage.distance_transform_edt(~observed, return_distances=False, return_indices=True)
    return values[tuple(idx)]


def ground_of(dsm: np.ndarray, observed: np.ndarray, window_cells: int) -> np.ndarray:
    """地面参考面 = 窗口内「每格最高点」的 P10，**只统计有观测的格**。

    为什么不用全局/大窗口形态学开运算：开运算窗口一旦大于建筑，整片屋顶会被压成「地面」，
    nDSM 归零、建筑整体消失（实测踩过）。P10 是分位而不是极值，对坡地与粗模都更稳。
    为什么排除空洞格而不是用常数填：空洞占 29% 时若把空洞当最低值参与分位，地面会被系统性
    拉低、建筑候选暴涨（实测 3996 格 vs 06 的 3209 格）。排除后与 06「±15 m 邻域 P10」同口径。
    """
    obs_z = dsm[observed]
    if obs_z.size == 0:
        raise ValueError("没有观测格，无法估计地面")
    fallback = float(np.percentile(obs_z, GROUND_Q))
    work = np.where(observed, dsm, np.nan)
    pad = max(1, window_cells // 2)
    padded = np.pad(work, ((pad, pad), (pad, pad)), mode="constant", constant_values=np.nan)
    win = sliding_window_view(padded, (window_cells, window_cells))
    out = np.empty(dsm.shape, dtype=np.float64)
    step = 16                                  # 分块，避免一次性展开上千万个窗口值
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)   # 全 NaN 窗口在下面用 fallback 兜
        for i in range(0, dsm.shape[0], step):
            block = win[i:i + step].reshape(win[i:i + step].shape[0], dsm.shape[1], -1)
            out[i:i + step] = np.nanpercentile(block, GROUND_Q, axis=2)
    return np.where(np.isfinite(out), out, fallback)


# ---------------------------------------------------------------- 几何
def convex_hull(points) -> list:
    """Andrew 单调链凸包；points 为 (x, y) 列表。"""
    pts = sorted(set((round(float(x), 6), round(float(y), 6)) for x, y in points))
    if len(pts) <= 2:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def min_area_rect(hull: list) -> dict:
    """旋转卡壳：对每条凸包边做投影，取面积最小的外接矩形。"""
    if len(hull) < 3:
        return {"area": 0.0, "width": 0.0, "height": 0.0, "angleDeg": 0.0,
                "corners": [], "inscribed": False}
    best = None
    m = len(hull)
    for i in range(m):
        p0, p1 = hull[i], hull[(i + 1) % m]
        dx, dy = p1[0] - p0[0], p1[1] - p0[1]
        length = math.hypot(dx, dy)
        if length < 1e-9:
            continue
        ux, uy = dx / length, dy / length
        vx, vy = -uy, ux
        us = [(p[0] - p0[0]) * ux + (p[1] - p0[1]) * uy for p in hull]
        vs = [(p[0] - p0[0]) * vx + (p[1] - p0[1]) * vy for p in hull]
        umin, umax, vmin, vmax = min(us), max(us), min(vs), max(vs)
        area = (umax - umin) * (vmax - vmin)
        if best is None or area < best["area"]:
            best = {"area": area, "umin": umin, "umax": umax, "vmin": vmin, "vmax": vmax,
                    "p0": p0, "u": (ux, uy), "v": (vx, vy), "length": length}
    if best is None:
        return {"area": 0.0, "width": 0.0, "height": 0.0, "angleDeg": 0.0,
                "corners": [], "inscribed": False}
    p0, (ux, uy), (vx, vy) = best["p0"], best["u"], best["v"]
    umin, umax, vmin, vmax = best["umin"], best["umax"], best["vmin"], best["vmax"]

    def point(u, v):
        return [round(p0[0] + u * ux + v * vx, 3), round(p0[1] + u * uy + v * vy, 3)]

    corners = [point(umin, vmin), point(umax, vmin), point(umax, vmax), point(umin, vmax)]
    return {"area": best["area"], "width": umax - umin, "height": vmax - vmin,
            "angleDeg": round(math.degrees(math.atan2(uy, ux)), 3),
            "corners": corners, "inscribed": True}


def ring_area(ring: list) -> float:
    """鞋带公式（绝对面积）。"""
    total = 0.0
    for i in range(len(ring) - 1):
        x1, y1 = ring[i]
        x2, y2 = ring[i + 1]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def segments_cross(a, b, c, d) -> bool:
    """两条线段是否真相交（不含仅端点相接）。矩形轮廓用，规模很小。"""
    def orient(p, q, r):
        val = (q[1] - p[1]) * (r[0] - q[0]) - (q[0] - p[0]) * (r[1] - q[1])
        return 0 if abs(val) < 1e-12 else (1 if val > 0 else -1)

    o1, o2 = orient(a, b, c), orient(a, b, d)
    o3, o4 = orient(c, d, a), orient(c, d, b)
    return o1 != o2 and o3 != o4


def ring_self_intersects(ring: list) -> bool:
    """闭合环自相交检测（O(n²)，n=5 的矩形只需常数时间）。"""
    pts = ring[:-1] if ring and ring[0] == ring[-1] else ring
    n = len(pts)
    if n < 4:
        return False
    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        for j in range(i + 2, n):
            if (j + 1) % n == i:
                continue
            c, d = pts[j], pts[(j + 1) % n]
            if segments_cross(a, b, c, d):
                return True
    return False


# ---------------------------------------------------------------- 实例与属性
def building_mask(ndsm: np.ndarray, observed: np.ndarray, threshold: float) -> tuple:
    """nDSM 阈值 + 形态学（闭运算补洞、开运算去毛刺）→ 布尔掩膜与中间诊断。"""
    raw = (ndsm > threshold) & observed
    closed = ndimage.binary_closing(raw, structure=np.ones((3, 3)), iterations=1) & observed
    cleaned = ndimage.binary_opening(closed, structure=np.ones((3, 3)), iterations=1)
    return cleaned, {"rawCells": int(raw.sum()), "closedCells": int(closed.sum()),
                     "cleanedCells": int(cleaned.sum())}


def instances(ndsm: np.ndarray, mask: np.ndarray, threshold: float,
              split_m: float, cell_m: float) -> tuple:
    """建筑实例：优先分水岭（nDSM 平滑后的局部极大值为种子），退回连通域。

    为什么必须拆：顶面连成一片的裙房/连廊会让整个街区变成一个连通块（本模型实测出现过
    2,900 m² 的单块）。分水岭按「屋顶高点的分水岭脊」切开，是 nDSM 建筑提取的常规做法。
    scikit-image 缺失或 --split-m 0 时退回连通域，并在产物里写明用了哪种。
    """
    if split_m > 0:
        try:
            from skimage.feature import peak_local_max
            from skimage.segmentation import watershed
        except ImportError:
            peak_local_max = watershed = None
        if peak_local_max is not None:
            smooth = ndimage.gaussian_filter(
                np.where(np.isfinite(ndsm), np.maximum(ndsm, 0.0), 0.0), sigma=1.0)
            peaks = peak_local_max(smooth, min_distance=max(1, int(round(split_m / cell_m))),
                                   threshold_abs=threshold, labels=mask, exclude_border=False)
            if len(peaks):
                markers = np.zeros(mask.shape, dtype=np.int32)
                for idx, (py, px) in enumerate(peaks, start=1):
                    markers[py, px] = idx
                labels = watershed(-smooth, markers, mask=mask)
                return labels, {"mode": "watershed", "seedCount": int(len(peaks)),
                                "minDistanceM": float(split_m)}
            return ndimage.label(mask)[0], {"mode": "watershed-no-seed", "seedCount": 0,
                                            "minDistanceM": float(split_m)}
    labels, _ = ndimage.label(mask)
    return labels, {"mode": "connected-component", "seedCount": None, "minDistanceM": 0.0}


def building_of(label_id: int, cells_ix, cells_iy, grid: dict, ndsm: np.ndarray,
                observed: np.ndarray, floor_m: float, origin) -> dict:
    """把一个连通域的格集变成一栋建筑的所有属性。"""
    n = len(cells_ix)
    cell = grid["cell"]
    x0, y0 = grid["x0"], grid["y0"]
    nx, ny = grid["nx"], grid["ny"]
    heights = ndsm[cells_ix, cells_iy]
    heights = heights[np.isfinite(heights)]
    area = n * cell * cell
    h50 = pct(heights, 50)
    h90 = pct(heights, 90)
    hmax = float(np.max(heights)) if heights.size else None
    cv = float(np.std(heights) / np.mean(heights)) if heights.size and np.mean(heights) > 0 else 1.0
    floor_est = max(1, int(round(h50 / floor_m))) if h50 else 1
    floor_low = max(1, int(math.ceil(h50 / FLOOR_HEIGHTS[1]))) if h50 else 1
    floor_high = max(1, int(round(h50 / FLOOR_HEIGHTS[0]))) if h50 else 1
    # 轮廓：格子四角 → 凸包 → 最小外接矩形
    pts = []
    for ix, iy in zip(cells_ix, cells_iy):
        px, py = x0 + ix * cell, y0 + iy * cell
        pts.extend([(px, py), (px + cell, py), (px, py + cell), (px + cell, py + cell)])
    hull = convex_hull(pts)
    rect = min_area_rect(hull)
    fill_ratio = (area / rect["area"]) if rect["area"] > 0 else None
    edge = bool(np.any((cells_ix == 0) | (cells_iy == 0)
                       | (cells_ix == nx - 1) | (cells_iy == ny - 1)))
    # 遮挡邻格：与本栋 4 邻接、但没有观测的空洞格（这些格可能属于本栋，只是被遮挡）
    hole_cells = 0
    mx0, mx1 = int(cells_ix.min()), int(cells_ix.max())
    my0, my1 = int(cells_iy.min()), int(cells_iy.max())
    sub = np.zeros((mx1 - mx0 + 3, my1 - my0 + 3), dtype=bool)
    sub[cells_ix - mx0 + 1, cells_iy - my0 + 1] = True
    ring = ndimage.binary_dilation(sub, structure=ndimage.generate_binary_structure(2, 1)) & ~sub
    obs_sub = np.pad(observed, 1, mode="edge")[mx0:mx1 + 3, my0:my1 + 3]
    hole_cells = int((ring & ~obs_sub).sum())
    reasons = []
    if area < 20:
        reasons.append(f"面积 {area:.0f} m² < 20 m²")
    if fill_ratio is not None and fill_ratio < 0.40:
        reasons.append(f"轮廓贴合率 {fill_ratio:.2f} < 0.40（异形）")
    if edge:
        reasons.append("压在 bbox 边界上（可能是被裁断的物体）")
    if cv > 0.6:
        reasons.append(f"高度离散 CV {cv:.2f} > 0.60（可能混入树冠/附着物）")
    if hole_cells >= max(5, 0.25 * n):
        reasons.append(f"与本体 4 邻接的遮挡空洞 {hole_cells} 格（占本体 {hole_cells / n:.0%}）")
    grade = "C" if reasons else ("A" if (area >= 60 and (fill_ratio or 0) >= 0.60 and cv <= 0.30)
                                 else "B")
    if grade == "A":
        reasons = ["面积/贴合率/高度稳定性均达标，无边界与空洞问题"]
    centroid_local = (float(np.mean([x0 + (ix + 0.5) * cell for ix in cells_ix])),
                      float(np.mean([y0 + (iy + 0.5) * cell for iy in cells_iy])))
    centroid = ([round(origin[0] + centroid_local[0], 3), round(origin[1] + centroid_local[1], 3)]
                if origin else [round(centroid_local[0], 3), round(centroid_local[1], 3)])
    footprint = {"type": "obb", "widthM": r2(rect["width"]), "heightM": r2(rect["height"]),
                 "angleDeg": rect["angleDeg"], "fillRatio": r2(fill_ratio),
                 "corners": rect["corners"]}
    return {
        "label": int(label_id), "cells": n, "areaM2": r2(area),
        "heightP50": r2(h50), "heightP90": r2(h90), "heightMax": r2(hmax),
        "heightCv": r2(cv), "floorsEst": int(floor_est),
        "floorsRange": [int(floor_low), int(floor_high)],
        "centroidXY": centroid, "footprint": footprint,
        "edgeTouch": edge, "holeCells": hole_cells,
        "confidence": grade, "gradeReasons": reasons,
    }


def analyze(model: dict, model_path: str, cell_m: float, ground_window_m: float,
            threshold: float, min_area_m2: float, floor_m: float, split_m: float) -> dict:
    grid = build_grids(model, model_path, cell_m)
    observed = grid["count"] > 0
    if not observed.any():
        raise ValueError("栅格为空：没有落进 bboxLocal 的 POSITION 顶点")
    dsm = np.where(observed, grid["zmax"], np.nan)
    dsm_fill = nearest_fill(np.where(observed, grid["zmax"], 0.0), observed)
    window_cells = max(3, int(round(ground_window_m / cell_m)) * 2 + 1)
    ground = ground_of(dsm_fill, observed, window_cells)
    ndsm = np.where(observed, np.maximum(dsm_fill - ground, 0.0), np.nan)
    mask, mask_diag = building_mask(ndsm, observed, threshold)
    labels, split_info = instances(ndsm, mask, threshold, split_m, cell_m)

    buildings, dropped = [], []
    for label_id in range(1, int(labels.max()) + 1 if labels.size else 0):
        cells_ix, cells_iy = np.nonzero(labels == label_id)   # 轴序：先 x 后 y（与 build_grids 一致）
        if cells_ix.size == 0:
            continue
        area = len(cells_ix) * cell_m * cell_m
        if area < min_area_m2:
            dropped.append({"label": int(label_id), "cells": int(len(cells_ix)), "areaM2": r2(area)})
            continue
        item = building_of(label_id, cells_ix, cells_iy, grid, ndsm, observed, floor_m,
                           (model.get("crs") or {}).get("origin"))
        buildings.append(item)
    buildings.sort(key=lambda b: b["areaM2"], reverse=True)
    for i, item in enumerate(buildings, start=1):
        item["id"] = f"B-{i:04d}"
    return {"grid": grid, "observed": observed, "dsm": dsm, "dsmFill": dsm_fill,
            "ground": ground, "ndsm": ndsm,
            "maskDiag": mask_diag, "split": split_info,
            "components": int(labels.max()) if labels.size else 0,
            "buildings": buildings, "dropped": dropped,
            "windowCells": window_cells}


def summarize(buildings: list) -> dict:
    if not buildings:
        return {"buildingCount": 0, "buildingAreaM2": 0.0, "heightP50": None, "heightP90": None,
                "floorAreaEstM2": 0.0, "gradeA": 0, "gradeB": 0, "gradeC": 0}
    areas = np.array([b["areaM2"] for b in buildings])
    h50 = np.array([b["heightP50"] for b in buildings])
    h90 = np.array([b["heightP90"] for b in buildings])
    floors = np.array([b["floorsEst"] for b in buildings])
    grades = {g: sum(1 for b in buildings if b["confidence"] == g) for g in ("A", "B", "C")}
    return {"buildingCount": len(buildings), "buildingAreaM2": r2(areas.sum()),
            "heightP50": r2(float(np.median(h50))), "heightP90": r2(float(np.median(h90))),
            "floorAreaEstM2": r2(float(np.sum(areas * floors))),
            "gradeA": grades["A"], "gradeB": grades["B"], "gradeC": grades["C"]}


def to_geojson(analysis: dict, model: dict, name: str) -> dict:
    origin = (model.get("crs") or {}).get("origin")
    features = []
    for item in analysis["buildings"]:
        corners = item["footprint"]["corners"]
        ring = [[round(p[0] + origin[0], 3), round(p[1] + origin[1], 3)] for p in corners] \
            if origin else [[round(p[0], 3), round(p[1], 3)] for p in corners]
        if not ring:
            continue
        ring.append(ring[0])   # 闭合
        # 统一逆时针（外环）
        signed = sum((ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1])
                     for i in range(len(ring) - 1))
        if signed < 0:
            ring = list(reversed(ring))
        props = {k: item[k] for k in ("id", "areaM2", "heightP50", "heightP90", "heightMax",
                                     "floorsEst", "floorsRange", "centroidXY", "confidence")}
        props["fillRatio"] = item["footprint"]["fillRatio"]
        props["widthM"] = item["footprint"]["widthM"]
        props["heightM"] = item["footprint"]["heightM"]
        props["angleDeg"] = item["footprint"]["angleDeg"]
        props["gradeReasons"] = "；".join(item["gradeReasons"])
        features.append({"type": "Feature", "id": item["id"],
                         "geometry": {"type": "Polygon", "coordinates": [ring]},
                         "properties": props})
    crs = (model.get("crs") or {})
    return {
        "type": "FeatureCollection", "schema": GEOJSON_SCHEMA, "name": name,
        "crs": ({"type": "name", "properties": {"name": f"urn:ogc:def:crs:EPSG::{crs['srs'].split(':')[1]}"}}
                if crs.get("srs", "").startswith("EPSG:") else None),
        "metadata": {
            "generatedAt": now_iso(), "producer": f"{PRODUCER}/{PRODUCER_VERSION}",
            "methodVersion": METHOD_VERSION, "featureCount": len(features),
            "geometry": "每栋的最小外接矩形（OBB），不是真实轮廓；异形建筑请用 fillRatio 判断",
            "warning": ("坐标是投影坐标（米），不是 RFC 7946 的 WGS84 经纬度；"
                        "本文件用 crs 成员声明坐标系，按 RFC 7946 属于「非标准 GeoJSON」。") if crs else
                       "没有 crs.origin，坐标为 bboxLocal 局部坐标（米）。",
            "coordinateMapping": ("WGS84→EPSG:4544 平面：X = origin[0] + localX，Y = origin[1] + localY；"
                                  "axisAngleDeg=0，无旋转") if origin else None,
        },
        "features": features,
    }


# ---------------------------------------------------------------- 人读报告
def render_md(doc: dict) -> str:
    scope = doc["scope"]
    summary = doc["summary"]
    p = doc["params"]
    lines = ["# 三维要素反演报告（09）", "",
             f"- 生成：{doc['generatedAt']}　方法版本：{doc['methodVersion']}",
             f"- 范围：{scope['areaM2']} m²（{scope['extentM']['x']} × {scope['extentM']['y']} m），"
             f"栅格 {scope['nx']} × {scope['ny']} @ {p['cellM']} m，有效格 {scope['observedCells']}",
             f"- 建筑候选：**{summary['buildingCount']} 栋**（面积 {summary['buildingAreaM2']} m²；"
             f"A/B/C = {summary['gradeA']}/{summary['gradeB']}/{summary['gradeC']}）",
             "", "## 方法（全部确定性几何，无深度学习）", "",
             f"1. **DSM**：每格顶点最高 z（{p['cellM']} m 格），只用朝上的顶点"
             f"（NORMAL.z ≥ 0.5，覆盖 {p['surfaceVertices']} / {p['allVertices']} 顶点）；"
             "用全部顶点会让墙面把相邻建筑连成一片。",
             f"2. **地面参考面（DTM 近似）**：窗口内「每格最高点」的 P{p['groundPercentile']}，"
             f"窗口 ±{p['groundWindowM']} m（{p['groundWindowCells']} 格）。不用大窗口形态学开运算——"
             "窗口一旦大于建筑，整片屋顶会被压成地面、nDSM 归零。",
             "3. **nDSM** = DSM − 地面参考面（离地高度）。",
             f"4. **建筑候选** = nDSM > {p['buildingThresholdM']} m；3×3 闭运算补洞 + 3×3 开运算去毛刺；"
             + (f"**分水岭拆分**（种子最小间距 {p['splitMinDistM']} m，共 {doc['diagnostics']['split']['seedCount']} 个种子）"
                if p['splitMode'] == 'watershed' else "连通域（未拆分）")
             + f"；面积 < {p['minAreaM2']} m² 的块丢弃（共丢 {doc['diagnostics']['droppedCount']} 块）。",
             f"5. **逐栋高度**：该栋 nDSM 的 P50 / P90 / 最大值；层数 = round(P50 / {p['floorHeightM']} m)，"
             f"区间按层高 {FLOOR_HEIGHTS[0]}–{FLOOR_HEIGHTS[1]} m 给出。",
             "6. **轮廓**：格四角 → 凸包 → 旋转卡壳最小外接矩形（OBB）；贴合率 = 格面积 / 矩形面积，"
             "低贴合率说明是 L 形/异形，不能当矩形入库。",
             "7. **置信度**：面积、贴合率、高度离散 CV、是否压边界、内部空洞五项定 A/B/C。",
             "", "## 结果", "",
             "| 指标 | 值 |", "|---|---|",
             f"| 建筑候选栋数 | {summary['buildingCount']} |",
             f"| 建筑占地面积 | {summary['buildingAreaM2']} m² |",
             f"| 逐栋高度 P50（栋间中位） | {summary['heightP50']} m |",
             f"| 逐栋高度 P90（栋间中位） | {summary['heightP90']} m |",
             f"| 逐栋层数累加建筑面积 | {summary['floorAreaEstM2']} m² |",
             f"| 置信度 A / B / C | {summary['gradeA']} / {summary['gradeB']} / {summary['gradeC']} |",
             "", "## 敏感性（地面窗口三档）", "",
             "| 窗口 (m) | 栋数 | 占地面积 (m²) | 高度 P50 (m) | 高度 P90 (m) | A/B/C |",
             "|---|---|---|---|---|---|"]
    for item in doc["sensitivity"]:
        lines.append(f"| {item['groundWindowM']} | {item['buildingCount']} | {item['buildingAreaM2']} | "
                     f"{item['heightP50']} | {item['heightP90']} | "
                     f"{item['gradeA']}/{item['gradeB']}/{item['gradeC']} |")
    lines += ["", "> 区间是**方法敏感性区间**（换地面窗口重算的 min–max），不是精度；"
                  "没有真值对照，不构成精度达标声明。", "",
              "## 逐栋（前 20，按面积降序）", "",
              "| id | 面积 m² | 高度P50 | 高度P90 | 层数 | 贴合率 | 置信度 | 理由 |",
              "|---|---|---|---|---|---|---|---|"]
    for item in doc["buildings"][:20]:
        lines.append(f"| {item['id']} | {item['areaM2']} | {item['heightP50']} | {item['heightP90']} | "
                     f"{item['floorsEst']} | {item['footprint']['fillRatio']} | {item['confidence']} | "
                     f"{'；'.join(item['gradeReasons'])[:60]} |")
    lines += ["", "## 缺项与局限（gaps）", ""]
    for gap in doc["gaps"]:
        lines.append(f"- **{gap['code']}** {gap['what']}：{gap['why']}　→ 需要：{gap['need']}")
    lines += ["", "## 产物", "",
              "- `要素矢量.geojson`：逐栋 OBB 轮廓（EPSG:4544），可拖进 QGIS/ArcGIS 叠红线。",
              "- `逐栋属性.json`：每栋全部字段 + 置信度理由。",
              "- `栅格/栅格.npz`：DSM/DTM/nDSM/观测掩膜（numpy 可直接 load，供 10 或后续算法复用）。",
              "- `三维要素反演.json`：机器可读全量结果（本报告的数值都出自它）。", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------- 主流程
def build(model_path: str, out_dir: str, cell_m: float, ground_window_m: float,
          threshold: float, min_area_m2: float, floor_m: float, split_m: float) -> dict:
    with open(model_path, encoding="utf-8") as fh:
        model = json.load(fh)
    if model.get("schema") != "cardo/lowair-model/v1":
        raise ValueError(f"model schema 非 cardo/lowair-model/v1：{model.get('schema')}")
    analysis = analyze(model, model_path, cell_m, ground_window_m, threshold, min_area_m2,
                       floor_m, split_m)
    grid = analysis["grid"]
    origin = (model.get("crs") or {}).get("origin")
    extent_x = grid["x1"] - grid["x0"]
    extent_y = grid["y1"] - grid["y0"]
    area = extent_x * extent_y
    summary = summarize(analysis["buildings"])

    sensitivity = []
    for wm in DTM_WINDOWS_M:
        if abs(wm - ground_window_m) < 1e-9:
            other = analysis["buildings"]
        else:
            other = analyze(model, model_path, cell_m, wm, threshold, min_area_m2,
                            floor_m, split_m)["buildings"]
        item = summarize(other)
        sensitivity.append({"groundWindowM": wm, "buildingCount": item["buildingCount"],
                            "buildingAreaM2": item["buildingAreaM2"],
                            "heightP50": item["heightP50"], "heightP90": item["heightP90"],
                            "gradeA": item["gradeA"], "gradeB": item["gradeB"],
                            "gradeC": item["gradeC"]})

    def span(key):
        vals = [s[key] for s in sensitivity if s[key] is not None]
        return [min(vals), max(vals)] if vals else [None, None]

    gaps = [
        {"code": "REC-GAP-001", "what": "建筑候选 ≠ 建筑：树冠/构筑物同样会被 nDSM 阈值选中",
         "why": "只有几何，没有语义分类；本机未装点云分类（PDAL），也没有影像分割",
         "need": "接入 pc.classify（PDAL 地面/植被分类）或影像建筑分割后替换", "owner": "upstream"},
        {"code": "REC-GAP-002", "what": "轮廓是最小外接矩形，不是真实边界",
         "why": "OBB 对 L 形/弧形建筑会外扩；fillRatio 低时面积偏大",
         "need": "接 GDAL polygonize 或 BuildingRegulariser 做直角/45°规则化", "owner": "upstream"},
        {"code": "REC-GAP-003", "what": "地面参考面是局部分位近似，不是真地面",
         "why": f"±{ground_window_m} m 窗内 P{GROUND_Q} 会把沟渠/台地抹平；"
                "档位差异已进 sensitivity，坡地陡变处会偏",
         "need": "接入 CSF/SMRF 点云地面滤波（PDAL）或提供实测 DTM", "owner": "upstream"},
        {"code": "REC-GAP-004", "what": "无真值，绝对精度未知",
         "why": "没有实测建筑轮廓/高度/层数可对照",
         "need": "提供检查点或人工标注，跑 eval.accuracy", "owner": "user"},
        {"code": "REC-GAP-005", "what": "LOD 重叠未消歧",
         "why": f"{grid['tiles']} 个 b3dm 含多级 LOD，格内取 max z 会混入粗模",
         "need": "按 LOD 过滤或只取单层 LOD", "owner": "upstream"},
        {"code": "REC-GAP-006", "what": "遮挡空洞按无观测处理",
         "why": f"有效格 {int(analysis['observed'].sum())} / 总格 {grid['nx'] * grid['ny']}；"
                "空洞处不参与实例与统计，被裁断的建筑会落在边界上（置信度降级）",
         "need": "补测或提供采集覆盖范围", "owner": "user"},
    ]
    for item in grid["failures"]:
        gaps.append({"code": "REC-GAP-007", "what": f"瓦片解析失败：{item['path']}",
                     "why": item["error"], "need": "检查该 b3dm 完整性", "owner": "upstream"})
    if grid.get("noNormalVertices"):
        gaps.append({"code": "REC-GAP-009",
                     "what": f"{grid['noNormalVertices']} 个顶点没有 NORMAL，DSM 口径降级为「全部顶点」",
                     "why": "部分 primitive 未导出法线，无法按「朝上顶点」过滤，墙面会抬高 DSM",
                     "need": "导出带法线的 b3dm，或改用点云法线估计", "owner": "upstream"})
    if not origin:
        gaps.append({"code": "REC-GAP-008", "what": "缺 crs.origin，矢量坐标是局部坐标",
                     "why": "模型契约没有 crs.origin 字段",
                     "need": "在 05 的契约里补 crs.origin", "owner": "upstream"})

    usable = bool(analysis["buildings"])
    doc = {
        "schema": SCHEMA, "schemaVersion": SCHEMA_VERSION, "generatedAt": now_iso(),
        "producer": {"name": PRODUCER, "version": PRODUCER_VERSION},
        "methodVersion": METHOD_VERSION,
        "source": {"model": rel_ref(model_path, out_dir), "tiles": grid["tiles"],
                   "parsed": grid["parsed"], "vertices": grid["vertices"],
                   "outsideVertices": grid["outside"]},
        "params": {"cellM": float(cell_m), "surfaceMode": "top(NORMAL.z>=0.5)",
                   "surfaceVertices": int(grid["topVertices"]), "allVertices": int(grid["vertices"]),
                   "groundMethod": "local_percentile(dsm,p10)", "groundPercentile": GROUND_Q,
                   "groundWindowM": float(ground_window_m),
                   "groundWindowCells": analysis["windowCells"],
                   "buildingThresholdM": float(threshold), "minAreaM2": float(min_area_m2),
                   "floorHeightM": float(floor_m),
                   "splitMode": analysis["split"]["mode"],
                   "splitMinDistM": analysis["split"]["minDistanceM"],
                   "splitSeeds": analysis["split"]["seedCount"],
                   "morphology": "close(3x3)+open(3x3)", "crs": (model.get("crs") or {}).get("srs"),
                   "originOffset": origin},
        "scope": {"areaM2": r2(area), "nx": grid["nx"], "ny": grid["ny"],
                  "cellM": float(cell_m), "observedCells": int(analysis["observed"].sum()),
                  "extentM": {"x": r3(extent_x), "y": r3(extent_y)},
                  "bboxLocal": model["geometry"]["bboxLocal"]},
        "summary": {**summary,
                    "buildingCountRange": span("buildingCount"),
                    "buildingAreaRange": span("buildingAreaM2"),
                    "heightP50Range": span("heightP50"),
                    "heightP90Range": span("heightP90")},
        "sensitivity": sensitivity,
        "diagnostics": {"components": analysis["components"],
                        "split": analysis["split"],
                        "droppedCount": len(analysis["dropped"]),
                        "dropped": analysis["dropped"][:20],
                        "mask": analysis["maskDiag"],
                        "rasterStats": {"dsm": stat(analysis["dsm"][analysis["observed"]]),
                                        "ground": stat(analysis["ground"][analysis["observed"]]),
                                        "ndsm": stat(analysis["ndsm"][analysis["observed"]])}},
        "buildings": analysis["buildings"],
        "gaps": gaps,
        "quality": {"usable": usable,
                    "reason": (f"几何反演可用：{summary['buildingCount']} 栋候选，"
                               f"高度 P50 {summary['heightP50']} m（栋间中位），"
                               f"全部为确定性几何估算，未做真值校核")
                               if usable else "nDSM 阈值内没有可用连通域，产物不可用",
                    "methodVersion": METHOD_VERSION, "parsedTiles": grid["parsed"],
                    "failedTiles": len(grid["failures"]), "vertices": grid["vertices"]},
    }
    for item in doc["buildings"]:
        missing = [k for k in BUILDING_KEYS if k not in item]
        if missing:
            raise ValueError(f"{item.get('id')} 缺字段 {missing}")

    os.makedirs(os.path.join(out_dir, "栅格"), exist_ok=True)
    np.savez_compressed(os.path.join(out_dir, "栅格", "栅格.npz"),
                        dsm=analysis["dsm"], ground=analysis["ground"], ndsm=analysis["ndsm"],
                        observed=analysis["observed"], cell=np.array([cell_m]),
                        x0=np.array([grid["x0"]]), y0=np.array([grid["y0"]]))
    geo = to_geojson(analysis, model, "cardo-building-candidates")
    write_json(os.path.join(out_dir, "要素矢量.geojson"), geo)
    write_json(os.path.join(out_dir, "逐栋属性.json"),
               {"schema": "cardo/lowair-building-attrs/v1", "generatedAt": now_iso(),
                "params": doc["params"], "crs": (model.get("crs") or {}).get("srs"),
                "count": len(doc["buildings"]), "buildings": doc["buildings"]})
    write_json(os.path.join(out_dir, "三维要素反演.json"), doc)
    with open(os.path.join(out_dir, "反演报告.md"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(render_md(doc))
    return doc


OUTPUT_NAMES = ("三维要素反演.json", "要素矢量.geojson", "逐栋属性.json", "反演报告.md")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="09 · 三维要素反演（DSM/DTM/nDSM + 逐栋建筑 + GeoJSON）")
    ap.add_argument("--model", default=MODEL_DEFAULT)
    ap.add_argument("--out", default=OUT_DEFAULT)
    ap.add_argument("--cell-m", type=float, default=1.0)
    ap.add_argument("--ground-window-m", type=float, default=15.0,
                    help="地面参考面窗口半径（米），默认 15（与 06 的 ±15 m 口径一致）")
    ap.add_argument("--building-threshold-m", type=float, default=3.0,
                    help="nDSM 建筑阈值（米），默认 3.0（与 06 一致）")
    ap.add_argument("--split-m", type=float, default=SPLIT_MIN_DIST_M,
                    help="分水岭种子最小间距（米），默认 6；给 0 退回连通域")
    ap.add_argument("--min-area-m2", type=float, default=10.0, help="建筑最小占地（m²），默认 10")
    ap.add_argument("--floor-height-m", type=float, default=DEFAULT_FLOOR_M)
    ap.add_argument("--force", action="store_true", help="允许覆盖已有产物")
    args = ap.parse_args(argv)

    if args.cell_m <= 0 or args.ground_window_m <= 0 or args.building_threshold_m <= 0 \
            or args.min_area_m2 < 0 or args.floor_height_m <= 0 or args.split_m < 0:
        print("[09] 参数越界：cell>0、窗口>0、阈值>0、minArea>=0、层高>0、split>=0", file=sys.stderr)
        return 3
    if not os.path.isfile(args.model):
        print(f"[09] 缺模型契约：{args.model}（先跑 05）", file=sys.stderr)
        return 2
    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)
    existing = [n for n in OUTPUT_NAMES if os.path.exists(os.path.join(out_dir, n))]
    if existing and not args.force:
        print(f"[09] 输出已存在且未给 --force，不静默覆盖：{existing}", file=sys.stderr)
        return 4
    for name in OUTPUT_NAMES:            # 先删旧再生成，防「上次的栋数」假通过
        path = os.path.join(out_dir, name)
        if os.path.isfile(path):
            os.remove(path)

    try:
        doc = build(args.model, out_dir, args.cell_m, args.ground_window_m,
                    args.building_threshold_m, args.min_area_m2, args.floor_height_m, args.split_m)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"[09] 反演失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 3

    summary = doc["summary"]
    print(f"[09] 完成：{summary['buildingCount']} 栋候选 / 占地 {summary['buildingAreaM2']} m² / "
          f"高度 P50 {summary['heightP50']} m / P90 {summary['heightP90']} m / "
          f"A·B·C {summary['gradeA']}·{summary['gradeB']}·{summary['gradeC']}；"
          f"地面窗口 {doc['params']['groundWindowM']} m（{doc['params']['groundWindowCells']} 格）；"
          f"拆分 {doc['params']['splitMode']}（{doc['params']['splitSeeds']} 种子）；"
          f"usable={doc['quality']['usable']}")
    print("[09] 产物：三维要素反演.json / 要素矢量.geojson / 逐栋属性.json / 栅格/栅格.npz / 反演报告.md")
    return 0 if doc["quality"]["usable"] else 3


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
