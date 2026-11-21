#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""13 · 矢量增强 · vector.polygonize：栅格 → 建筑轮廓面。

从 09 的建筑 mask（`ndsm > 阈值 & observed`，3×3 闭运算/开运算，与 09 同参数）
用等值线 level=0.5 提取轮廓；格坐标按
    X = origin[0] + x0 + ix·cell
    Y = origin[1] + y0 + iy·cell
转到 EPSG:4544；自实现 Douglas-Peucker（容差 0.5 m）+ 环闭合 + 外环逆时针 +
洞顺时针 + 面积 < 10 m² 丢弃。与 10 的 OBB 按质心归属对比面积比。

等值线引擎：本机装有 scikit-image 时用 `skimage.measure.find_contours`；
未安装时用内置 marching squares（同一 level=0.5 口径，纯 stdlib），并把引擎写进
`params.contourEngine` 与 gap，绝不假装装了 skimage。

产物（过程文件/13-矢量增强/vector-polygonize/）：
    建筑轮廓面.geojson（EPSG:4544；外环逆时针、洞顺时针、首尾闭合）
    vector-polygonize.json / .md / _done；_patches/vector-polygonize.json

用法（工作区根）：
    python <技能目录>/Skills/13-矢量增强/scripts/vector_polygonize.py --workspace .
退出码：0 可用；2 缺输入；3 产出但不可用；4 产物已存在未给 --force。
"""
from __future__ import annotations

import math
import os
import sys
from collections import defaultdict

sys.dont_write_bytecode = True

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:                                            # 有 numpy 时复用主线 common
    from common import (GRID_REL, MODEL_REL, RECON_REL, VECTOR_REL,  # noqa: E402
                        emit, finish, load_grid, load_origin, parse_args, read_json, write_json)
    COMMON_IMPL = "common"
except ImportError:                             # 本机无 numpy：纯 stdlib 兜底
    from common_fallback import (GRID_REL, MODEL_REL, RECON_REL, VECTOR_REL,  # noqa: E402
                                 emit, finish, load_grid, load_origin, parse_args,
                                 read_json, write_json)
    COMMON_IMPL = "common_fallback"

try:                                            # 等值线引擎（可选）
    import numpy as _np
    from skimage import measure as _measure
    CONTOUR_ENGINE = "skimage.measure.find_contours"
except ImportError:
    _np = None
    _measure = None
    CONTOUR_ENGINE = "builtin-marching-squares"

ALGO = "vector.polygonize"
SLUG = "vector-polygonize"
OUT_GEOJSON = "建筑轮廓面.geojson"
PATCH_NAME = "vector-polygonize.json"
LEVEL = 0.5
TOL_M = 0.5
MIN_AREA_M2 = 10.0
THRESH_DEFAULT_M = 3.0
PRECISION = 3
GEOJSON_SCHEMA = "cardo/lowair-vector/v1"


def _rel(path: str) -> str:
    return str(path).replace("\\", "/")


def _as_list(value) -> list:
    """栅格 → **扁平** Python 列表（本脚本一律按 i*ny+j 访问，二维数组须先 ravel）。"""
    if hasattr(value, "ravel"):
        return value.ravel().tolist()
    flat = []
    for item in value:
        if isinstance(item, (list, tuple)):
            flat.extend(item)
        else:
            flat.append(item)
    return flat


# ---------------------------------------------------------------- 几何
def signed_area(ring) -> float:
    """鞋带公式：>0 逆时针（世界坐标 x=东、y=北）。ring 需闭合。"""
    total = 0.0
    for i in range(len(ring) - 1):
        total += ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1]
    return total / 2.0


def ring_area(ring) -> float:
    return abs(signed_area(ring))


def ring_centroid(ring):
    """面积加权质心；退化时退回顶点均值。"""
    a = signed_area(ring)
    if abs(a) < 1e-9:
        pts = ring[:-1]
        return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
    cx = cy = 0.0
    for i in range(len(ring) - 1):
        cross = ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1]
        cx += (ring[i][0] + ring[i + 1][0]) * cross
        cy += (ring[i][1] + ring[i + 1][1]) * cross
    return (cx / (6.0 * a), cy / (6.0 * a))


def point_in_ring(pt, ring) -> bool:
    """射线法；ring 需闭合。"""
    x, y = pt
    inside = False
    n = len(ring) - 1
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[i + 1]
        if (y1 > y) != (y2 > y):
            if x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
                inside = not inside
    return inside


def _seg_dist(p, a, b) -> float:
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    if dx == 0.0 and dy == 0.0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def dp_open(pts, tol):
    """Douglas-Peucker（开放折线，栈式避免深递归）。"""
    n = len(pts)
    if n < 3:
        return list(pts)
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        s, e = stack.pop()
        if e - s < 2:
            continue
        dmax, idx = -1.0, -1
        for i in range(s + 1, e):
            d = _seg_dist(pts[i], pts[s], pts[e])
            if d > dmax:
                dmax, idx = d, i
        if dmax > tol:
            keep[idx] = True
            stack.append((s, idx))
            stack.append((idx, e))
    return [pts[i] for i in range(n) if keep[i]]


def dedupe(pts):
    out = []
    for p in pts:
        if not out or p != out[-1]:
            out.append(p)
    return out


def dp_ring(ring, tol):
    """闭合环简化：在离首点最远处切成两条开放折线，分别简化后拼回。"""
    pts = dedupe([[float(p[0]), float(p[1])] for p in ring[:-1]])
    if len(pts) < 3:
        return []
    if len(pts) == 3:
        return dedupe(pts + [pts[0]])
    k, dmax = 0, -1.0
    for i in range(1, len(pts)):
        d = math.hypot(pts[i][0] - pts[0][0], pts[i][1] - pts[0][1])
        if d > dmax:
            dmax, k = d, i
    head = dp_open(pts[:k + 1], tol)
    tail = dp_open(pts[k:] + [pts[0]], tol)
    out = dedupe(head[:-1] + tail[:-1])
    if len(out) < 3:
        out = pts
    return dedupe(out + [out[0]])


# ---------------------------------------------------------------- mask（复刻 09）
def dilate(mask, nx, ny):
    out = [False] * (nx * ny)
    for i in range(nx):
        for j in range(ny):
            if not mask[i * ny + j]:
                continue
            for di in (-1, 0, 1):
                ii = i + di
                if not 0 <= ii < nx:
                    continue
                base = ii * ny
                for dj in (-1, 0, 1):
                    jj = j + dj
                    if 0 <= jj < ny:
                        out[base + jj] = True
    return out


def erode(mask, nx, ny):
    out = [False] * (nx * ny)
    for i in range(nx):
        for j in range(ny):
            if not mask[i * ny + j]:
                continue
            ok = True
            for di in (-1, 0, 1):
                ii = i + di
                if not 0 <= ii < nx:
                    ok = False
                    break
                base = ii * ny
                for dj in (-1, 0, 1):
                    jj = j + dj
                    if not 0 <= jj < ny or not mask[base + jj]:
                        ok = False
                        break
                if not ok:
                    break
            if ok:
                out[i * ny + j] = True
    return out


def build_mask(ndsm, observed, nx, ny, threshold):
    """`(ndsm > thr) & observed` → 3×3 闭运算(& observed) → 3×3 开运算。"""
    raw = [bool(observed[k]) and math.isfinite(ndsm[k]) and float(ndsm[k]) > threshold
           for k in range(nx * ny)]
    closed = erode(dilate(raw, nx, ny), nx, ny)          # 闭运算（方形 3×3）
    closed = [c and bool(observed[k]) for k, c in enumerate(closed)]
    cleaned = dilate(erode(closed, nx, ny), nx, ny)      # 开运算
    return raw, closed, cleaned


# ---------------------------------------------------------------- 等值线
def _marching_pure(mask, nx, ny):
    """marching squares（level 0.5）；返回 padded 坐标下的闭合环。"""
    h, w = nx + 2, ny + 2

    def val(a, b):
        if 0 <= a - 1 < nx and 0 <= b - 1 < ny:
            return 1.0 if mask[(a - 1) * ny + (b - 1)] else 0.0
        return 0.0

    segments = []
    for i in range(h - 1):
        for j in range(w - 1):
            case = (val(i, j) * 8 + val(i, j + 1) * 4
                    + val(i + 1, j + 1) * 2 + val(i + 1, j))
            t, rr, bb, ll = (i, j + 0.5), (i + 0.5, j + 1), (i + 1, j + 0.5), (i + 0.5, j)
            if case in (0, 15):
                continue
            if case == 1:
                segments.append((ll, bb))
            elif case == 2:
                segments.append((bb, rr))
            elif case == 3:
                segments.append((ll, rr))
            elif case == 4:
                segments.append((t, rr))
            elif case == 5:                            # fully_connected='low'
                segments.append((t, rr))
                segments.append((ll, bb))
            elif case == 6:
                segments.append((t, bb))
            elif case == 7:
                segments.append((t, ll))
            elif case == 8:
                segments.append((t, ll))
            elif case == 9:
                segments.append((t, bb))
            elif case == 10:
                segments.append((t, ll))
                segments.append((bb, rr))
            elif case == 11:
                segments.append((t, rr))
            elif case == 12:
                segments.append((ll, rr))
            elif case == 13:
                segments.append((bb, rr))
            else:                                      # case 14
                segments.append((ll, bb))

    adj = defaultdict(list)
    for idx, (a, b) in enumerate(segments):
        adj[a].append((b, idx))
        adj[b].append((a, idx))
    used = [False] * len(segments)
    loops = []
    for start in range(len(segments)):
        if used[start]:
            continue
        used[start] = True
        a, b = segments[start]
        loop = [a, b]
        cur = b
        while True:
            nxt = None
            for pt, idx in adj[cur]:
                if not used[idx]:
                    nxt = (pt, idx)
                    break
            if nxt is None:
                break
            loop.append(nxt[0])
            used[nxt[1]] = True
            cur = nxt[0]
            if cur == loop[0]:
                break
        if len(loop) >= 4 and loop[-1] == loop[0]:
            loops.append(loop)
    return loops


def _marching_skimage(mask, nx, ny):
    """skimage.measure.find_contours（padded，level=0.5）。"""
    arr = _np.array([1.0 if v else 0.0 for v in mask], dtype=float).reshape(nx, ny)
    padded = _np.pad(arr, 1, mode="constant")
    loops = []
    for contour in _measure.find_contours(padded, LEVEL):
        loops.append([(float(p[0]), float(p[1])) for p in contour])
    return loops


def contour_loops(mask, nx, ny):
    if CONTOUR_ENGINE.startswith("skimage"):
        return _marching_skimage(mask, nx, ny)
    return _marching_pure(mask, nx, ny)


def to_world(loop, ox, oy, x0, y0, cell):
    """padded 下标（像素中心）→ 世界坐标；边界落在 09 格边线上。"""
    pts = []
    for (r, c) in loop:
        ix = r - 0.5
        iy = c - 0.5
        pts.append([round(ox + x0 + ix * cell, PRECISION),
                    round(oy + y0 + iy * cell, PRECISION)])
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    return dedupe(pts)


# ---------------------------------------------------------------- 10 的 OBB
def load_obbs(workspace: str) -> list:
    doc = read_json(os.path.join(workspace, VECTOR_REL))
    out = []
    for feat in doc.get("features") or []:
        geom = feat.get("geometry") or {}
        rings = geom.get("coordinates") or []
        if geom.get("type") != "Polygon" or not rings:
            continue
        props = feat.get("properties") or {}
        area = props.get("areaRecalcM2")
        if area is None:
            w, h = props.get("widthM"), props.get("heightM")
            area = float(w) * float(h) if w is not None and h is not None else None
        ring = rings[0]
        out.append({"id": feat.get("id") or props.get("id"), "ring": ring,
                    "area": float(area) if area else None,
                    "centroid": ring_centroid(ring)})
    return out


def match_obbs(outer, obbs):
    inside = [o for o in obbs if point_in_ring(o["centroid"], outer)]
    return inside


def group_rings(rings):
    """按包含关系定内外；返回 (外环列表, 洞字典)。"""
    n = len(rings)
    parent = [None] * n
    for j in range(n):
        holders = [i for i in range(n)
                   if rings[i]["area"] > rings[j]["area"]
                   and point_in_ring(rings[j]["ring"][0], rings[i]["ring"])]
        if holders:
            parent[j] = min(holders, key=lambda i: rings[i]["area"])
    depth = [0] * n
    for j in range(n):
        p, d = parent[j], 0
        while p is not None:
            d, p = d + 1, parent[p]
        depth[j] = d
    holes_of = {}
    for j in range(n):
        if depth[j] % 2 == 1 and parent[j] is not None:
            holes_of.setdefault(parent[j], []).append(j)
    return [j for j in range(n) if depth[j] % 2 == 0], holes_of


# ---------------------------------------------------------------- 主流程
def build(workspace: str) -> tuple:
    recon = read_json(os.path.join(workspace, RECON_REL))
    grid = load_grid(workspace)
    raw_ndsm = grid["ndsm"]
    shape = getattr(raw_ndsm, "shape", None) or grid.get("shape")
    if shape is None:
        raise ValueError("栅格缺 shape")
    nx, ny = int(shape[0]), int(shape[1])
    ndsm = _as_list(raw_ndsm)
    observed = _as_list(grid["observed"])
    cell = float(grid["cell"])
    x0 = float(grid["x0"])
    y0 = float(grid["y0"])

    params09 = recon.get("params") or {}
    threshold = float(params09.get("buildingThresholdM") or THRESH_DEFAULT_M)
    mask_cells09 = ((recon.get("diagnostics") or {}).get("mask") or {}).get("cleanedCells")
    labeled_cells09 = sum(int(b.get("cells") or 0) for b in (recon.get("buildings") or [])) or None

    raw, closed, mask = build_mask(ndsm, observed, nx, ny, threshold)
    mask_cells = sum(1 for v in mask if v)

    ox, oy, origin_note = 0.0, 0.0, None
    try:
        origin = load_origin(workspace)
    except (SystemExit, OSError, ValueError):
        origin = None
    if origin:
        ox, oy = float(origin[0]), float(origin[1])
    else:
        origin_note = "模型契约缺 crs.origin，退回局部坐标（与 09/10 世界坐标不可直接叠加）"

    rings = []
    dropped = []
    for li, loop in enumerate(contour_loops(mask, nx, ny)):
        pts = to_world(loop, ox, oy, x0, y0, cell)
        if len(pts) < 3:
            dropped.append({"loop": li, "why": f"轮廓仅 {len(pts)} 个不同点，退化"})
            continue
        ring = dp_ring(pts + [pts[0]], TOL_M)
        if len(ring) < 4:
            dropped.append({"loop": li, "why": f"简化后仅 {len(ring)} 点，退化"})
            continue
        area = ring_area(ring)
        rings.append({"ring": ring, "area": area, "before": len(pts), "after": len(ring)})

    obbs = load_obbs(workspace)
    outers, holes_of = group_rings(rings)
    polygons = []
    vb_total = va_total = 0
    for oi in outers:
        outer = rings[oi]["ring"]
        hole_rings = []
        for h in holes_of.get(oi, []):
            hr = rings[h]["ring"]
            if signed_area(hr) > 0:                    # 洞统一顺时针
                hr = list(reversed(hr))
            hole_rings.append(hr)
        net = rings[oi]["area"] - sum(ring_area(r) for r in hole_rings)
        if net < MIN_AREA_M2:
            dropped.append({"loop": oi, "why": f"净面积 {net:.2f} m² < {MIN_AREA_M2} m²"})
            continue
        if signed_area(outer) < 0:                     # 外环统一逆时针
            outer = list(reversed(outer))
        before = rings[oi]["before"] + sum(rings[h]["before"] for h in holes_of.get(oi, []))
        after = rings[oi]["after"] + sum(rings[h]["after"] for h in holes_of.get(oi, []))
        matched = match_obbs(outer, obbs)
        obb_area = sum(o["area"] for o in matched) if matched else None
        polygons.append({
            "outer": outer, "holes": hole_rings, "area": net,
            "before": before, "after": after, "centroid": ring_centroid(outer),
            "obbIds": [o["id"] for o in matched], "obbArea": obb_area,
            "ratio": (net / obb_area) if obb_area else None,
        })
        vb_total += before
        va_total += after

    polygons.sort(key=lambda p: (-p["area"], p["centroid"][0]))
    ratios = [p["ratio"] for p in polygons if p["ratio"] is not None]
    area_ratio = round(float(sorted(ratios)[len(ratios) // 2]) if len(ratios) % 2
                       else (sorted(ratios)[len(ratios) // 2 - 1] + sorted(ratios)[len(ratios) // 2]) / 2,
                       4) if ratios else None

    features = []
    for i, poly in enumerate(polygons, 1):
        fid = f"P-{i:04d}"
        props = {
            "id": fid,
            "areaM2": round(poly["area"], 2),
            "holeCount": len(poly["holes"]),
            "vertexCount": poly["after"],
            "centroidXY": [round(poly["centroid"][0], 3), round(poly["centroid"][1], 3)],
            "obbIds": poly["obbIds"],
            "obbAreaM2": round(poly["obbArea"], 2) if poly["obbArea"] else None,
            "areaRatioVsObb": round(poly["ratio"], 4) if poly["ratio"] is not None else None,
            "matchedBy": "centroid-inside" if poly["obbIds"] else None,
        }
        features.append({"type": "Feature", "id": fid,
                         "geometry": {"type": "Polygon",
                                      "coordinates": [poly["outer"]] + poly["holes"]},
                         "properties": props})

    geojson = {
        "type": "FeatureCollection",
        "schema": GEOJSON_SCHEMA,
        "name": "cardo-building-outline",
        "crs": {"type": "name",
                "properties": {"name": "urn:ogc:def:crs:EPSG::4544"}},
        "metadata": {
            "generatedAt": None,          # emit 后回填
            "featureCount": len(features),
            "geometry": "建筑轮廓面（栅格等值线 + Douglas-Peucker），非 OBB 外接矩形",
            "warning": "坐标为 EPSG:4544 投影坐标（米），不是 RFC 7946 的 WGS84 经纬度；"
                       "为兼容 GIS 另带 crs 成员（RFC 7946 的非标准扩展）",
            "method": f"{CONTOUR_ENGINE}(level={LEVEL}) + 自实现 Douglas-Peucker({TOL_M} m)",
            "orientation": "外环逆时针、内环（洞）顺时针",
            "maskSource": f"09 建筑 mask 按 ndsm>{threshold} & observed + close/open(3×3) 重算",
        },
        "features": features,
    }

    summary = {
        "polygonCount": len(features),
        "polygonAreaM2": round(sum(p["area"] for p in polygons), 2),
        "areaRatioVsObb": area_ratio,
        "vertexCountBefore": vb_total,
        "vertexCountAfter": va_total,
    }

    gaps = [{
        "code": "ENH-GAP-001",
        "what": "轮廓沿栅格格边呈锯齿（marching squares 在格边 ±0.5 m 处折返）",
        "why": "只有 1 m 栅格与几何简化，没做直角/45° 规则化；斜墙与弧形墙都被画成阶梯",
        "need": "接 BuildingRegulariser 或带直角约束的 polygonize 后处理",
    }]
    merged = [p for p in polygons if len(p["obbIds"]) > 1]
    if merged:
        gaps.append({
            "code": "ENH-GAP-002",
            "what": (f"mask 连通域把相邻建筑粘成一个面（最大面覆盖 {max(len(p['obbIds']) for p in merged)} 栋 OBB），"
                     "未做逐栋实例拆分"),
            "why": "09 只落盘二值 mask，未落盘逐栋 label 栅格；本脚本不重跑分水岭，按连通域提轮廓",
            "need": "09 落盘 label 栅格（或本链接分水岭拆分）后可按栋出图",
        })
    if labeled_cells09 and mask_cells09 and labeled_cells09 < mask_cells09:
        gaps.append({
            "code": "ENH-GAP-003",
            "what": (f"09 建筑实例只覆盖 {labeled_cells09} 格，mask 有 {mask_cells09} 格；"
                     f"{mask_cells09 - labeled_cells09} 格未被任何栋占用"),
            "why": "未达 09 建栋条件的零散 mask 格仍参与连通域轮廓，可能形成无 OBB 对应的小面",
            "need": "核对这些小块是否树冠/构筑物；必要时在 09 提高最小面积或加语义分类",
        })
    if origin_note:
        gaps.append({"code": "ENH-GAP-004", "what": "缺 EPSG:4544 原点",
                     "why": origin_note, "need": "在 03/05 模型契约补 crs.origin 后重跑"})
    if not CONTOUR_ENGINE.startswith("skimage"):
        gaps.append({
            "code": "ENH-GAP-005",
            "what": "本机未安装 scikit-image，等值线改用内置 marching squares",
            "why": "禁止 pip install；内置实现按同一 level=0.5 口径（0/1 场在格边中点取等值线）",
            "need": "环境装上 scikit-image 后脚本自动改用 skimage.measure.find_contours",
        })

    problems = validate(features)
    usable = bool(features) and not problems
    reasons = problems if problems else ([] if features else ["没有满足条件的轮廓面"])

    items = [{
        "id": f["id"],
        "areaM2": f["properties"]["areaM2"],
        "obbIds": f["properties"]["obbIds"],
        "obbAreaM2": f["properties"]["obbAreaM2"],
        "areaRatioVsObb": f["properties"]["areaRatioVsObb"],
        "holeCount": f["properties"]["holeCount"],
        "vertexCount": f["properties"]["vertexCount"],
        "matchedBy": f["properties"]["matchedBy"],
    } for f in features]

    markdown = render_md(summary, polygons, gaps, threshold, mask_cells, mask_cells09,
                         labeled_cells09, origin_note, dropped)

    inputs = {
        "grid": _rel(GRID_REL), "recon": _rel(RECON_REL),
        "vector10": _rel(VECTOR_REL), "model": _rel(MODEL_REL),
        "origin": [ox, oy] if origin_note is None else None,
        "maskCells": mask_cells, "maskCells09": mask_cells09,
        "labeledCells09": labeled_cells09,
        "maskRawCells": sum(1 for v in raw if v),
        "maskClosedCells": sum(1 for v in closed if v),
    }
    params = {
        "level": LEVEL, "toleranceM": TOL_M, "minAreaM2": MIN_AREA_M2,
        "buildingThresholdM": threshold, "morphology": "close(3x3)+open(3x3)",
        "instanceSplit": "none",
        "cellM": cell, "origin": [ox, oy] if origin_note is None else None,
        "simplify": "Douglas-Peucker(自实现)", "orientation": "outer-ccw",
        "contourEngine": CONTOUR_ENGINE, "commonImpl": COMMON_IMPL,
    }

    os.makedirs(os.path.join(workspace, "过程文件", "13-矢量增强", SLUG), exist_ok=True)
    geojson["metadata"]["generatedAt"] = None
    doc = emit(workspace, ALGO, inputs=inputs, params=params, summary=summary,
               items=items, gaps=gaps, usable=usable, reasons=reasons,
               markdown=markdown, extra_files=[OUT_GEOJSON])
    geojson["metadata"]["generatedAt"] = doc["generatedAt"]
    write_json(os.path.join(workspace, "过程文件", "13-矢量增强", SLUG, OUT_GEOJSON), geojson)
    write_patch(workspace)
    return doc


def validate(features) -> list:
    problems = []
    for feat in features:
        fid = feat.get("id")
        rings = (feat.get("geometry") or {}).get("coordinates") or []
        if not rings:
            problems.append(f"{fid} 没有外环")
            continue
        for k, ring in enumerate(rings):
            label = "外环" if k == 0 else f"洞{k}"
            if not ring or ring[0] != ring[-1]:
                problems.append(f"{fid} {label}未闭合")
            if len(ring) < 4:
                problems.append(f"{fid} {label}顶点 <4")
            if any(not math.isfinite(p[0]) or not math.isfinite(p[1]) for p in ring):
                problems.append(f"{fid} {label}含非有限坐标")
            area = signed_area(ring)
            if k == 0 and area <= 0:
                problems.append(f"{fid} 外环不是逆时针")
            if k > 0 and area >= 0:
                problems.append(f"{fid} {label}不是顺时针")
    return problems


def write_patch(workspace: str) -> None:
    patch = {
        "id": ALGO,
        "status": "ready",
        "step": "13",
        "impl": "自实现：skimage.measure.find_contours + Douglas-Peucker(0.5 m) + 环闭合/方向统一"
                "（无 skimage 时用内置 marching squares 等价实现）",
        "evidence": "Skills/13-矢量增强/scripts/vector_polygonize.py",
        "sourceName": "marching squares 等值线 + Douglas-Peucker 简化（Douglas & Peucker 1973）；"
                      "实现 scikit-image + 本链自实现简化",
        "sourceLink": "",
        "license": "BSD-3-Clause",
        "plain": "从 09 建筑栅格提取真实边界轮廓面，替代 10 的 OBB 外接矩形",
    }
    write_json(os.path.join(workspace, "过程文件", "13-矢量增强", "_patches", PATCH_NAME), patch)


def render_md(summary, polygons, gaps, threshold, mask_cells, mask_cells09,
              labeled_cells09, origin_note, dropped) -> str:
    lines = ["# 建筑轮廓面（13 · vector.polygonize）", "",
             f"- 建筑 mask：`ndsm > {threshold:.1f} m` + close/open(3×3)，{mask_cells} 格"
             + (f"（09 记录 {mask_cells09} 格）" if mask_cells09 is not None else ""),
             "- mask 来源：09 未落盘 mask 栅格，本脚本按 09 同参数重算（格数/诊断与 09 对齐）",
             f"- 等值线：`{CONTOUR_ENGINE}`（level={LEVEL}）；Douglas-Peucker 容差 {TOL_M} m；"
             f"净面积 < {MIN_AREA_M2} m² 丢弃",
             f"- 坐标：`EPSG:4544`（米）" + ("；**缺原点，当前为局部坐标**" if origin_note else ""),
             f"- 实例：按 mask 连通域提取，未做逐栋拆分"
             + (f"（09 逐栋实例覆盖 {labeled_cells09} 格）" if labeled_cells09 else ""),
             "",
             "## 结果", "",
             "| 面 | 面积 m² | 洞 | 顶点(前→后) | 对应 OBB | OBB 面积合 m² | 面积比 |",
             "|---|---|---|---|---|---|---|"]
    for i, poly in enumerate(polygons, 1):
        obbs = "、".join(poly["obbIds"]) if poly["obbIds"] else "—"
        ratio = f"{poly['ratio']:.3f}" if poly["ratio"] is not None else "—"
        lines.append(f"| P-{i:04d} | {poly['area']:.2f} | {len(poly['holes'])} | "
                     f"{poly['before']}→{poly['after']} | {obbs} | "
                     f"{poly['obbArea']:.2f} | {ratio} |" if poly["obbArea"] else
                     f"| P-{i:04d} | {poly['area']:.2f} | {len(poly['holes'])} | "
                     f"{poly['before']}→{poly['after']} | {obbs} | — | — |")
    n_ratio = sum(1 for p in polygons if p["ratio"] is not None)
    lines += ["", "## summary", "",
              f"- 面数 `{summary['polygonCount']}`；总面积 `{summary['polygonAreaM2']}` m²；"
              f"与 10 的 OBB 面积比中位数 `{summary['areaRatioVsObb']}`"
              f"（{n_ratio} 个面有 OBB 质心对应，其余为 null）；"
              f"顶点 `{summary['vertexCountBefore']}` → `{summary['vertexCountAfter']}`", "",
              "## 局限", ""]
    for gap in gaps:
        lines.append(f"- `{gap['code']}` {gap['what']}：{gap['why']}（需：{gap['need']}）")
    lines += ["- 轮廓来自 1 m 栅格等值线，边界锯齿；面积是**占地投影**，不是建筑面积。",
              "- 与 10 的面积比 < 1 说明 OBB 外扩；比值只反映形状贴合，不是精度。", ""]
    if dropped:
        lines += ["## 被丢弃的轮廓", ""]
        for item in dropped[:20]:
            lines.append(f"- 环 {item.get('loop', '?')}：{item['why']}")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    try:
        args = parse_args(ALGO, "13 · 栅格→建筑轮廓面（等值线 + Douglas-Peucker）")
    except SystemExit as exc:
        code = exc.code
        if isinstance(code, int):
            return code
        if code:
            print(str(code), file=sys.stderr)
        return 4 if code and "产物已存在" in str(code) else 2
    for rel in (GRID_REL, RECON_REL, VECTOR_REL):
        if not os.path.isfile(os.path.join(args.workspace, rel)):
            print(f"[13:vector.polygonize] 缺输入：{rel}（先跑 09/10）", file=sys.stderr)
            return 2
    try:
        doc = build(args.workspace)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"[13:vector.polygonize] 失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 3
    return finish(doc)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
