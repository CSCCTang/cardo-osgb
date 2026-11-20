#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""现状指标（T11 草稿）：从 cardo-lowair-model/v1 + 3D Tiles 产物估算 2.5D 规划指标。

读 model JSON → 按 artifacts[].path 定位 b3dm → 读 GLB accessor POSITION → 顶点投到 bboxLocal 的
x/y 网格（每格取最大 z）→ ground = 有观测格 z 的 P5 → mask = (格 z − ground) > 阈值 → 覆盖/栋数/
高度分位/容积率；绿化与道路由 texture_materials 从 b3dm 内嵌纹理做 P0 材质识别，产出带区间与置信度的
纹理覆盖代理值。只读标准容器（b3dm 头 / GLB / glTF JSON），**不解析 OSGB 字节**，
依赖按需（允许联网/装包，须登记）。区间由网格三档 0.5/1.0/2.0 m（容积率另加层高 2.8/3.3 m）重算取 min/max，不做精度承诺。
用法：python skill/scripts/run_indicators.py \
        --model <cardo-lowair-model.json> --out <目录>
        [--cell-m 1.0] [--ground-percentile 5] [--building-threshold-m 3.0] [--force]
退出码 0 成功 / 3 输入不可用（此时仍落盘「输入检查与口径说明」降级产物，quality.usable=false）
        / 4 输出目录已有同名产物且未给 --force（不静默覆盖）。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys

sys.dont_write_bytecode = True  # 不生成 __pycache__（保持插件目录纯净）
# 包内便携 runtime 用 python313._pth 锁死 sys.path（脚本目录永不入 path），
# 这里显式补上，否则同目录 import indicator_report 一定失败。
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
from datetime import datetime

import indicator_report
import texture_materials
from indicator_report import render_md  # noqa: F401

try:   # 有 numpy 就走向量化（快一个量级）；没有则退回纯 Python 逐点循环
    import numpy as np
except ImportError:   # pragma: no cover
    np = None

SCHEMA, SCHEMA_VERSION = "cardo/lowair-indicators/v1", 1
PRODUCER, PRODUCER_VERSION = "cardo-osgb", "0.1.0"
METHOD_VERSION = "indicators-v1"
SENSITIVITY_CELLS = (0.5, 1.0, 2.0)   # 区间来源：三档网格重算
FLOOR_HEIGHTS = (2.8, 3.0, 3.3)       # 区间来源：层高假设带
DEFAULT_FLOOR_M = 3.0
LOCAL_GROUND_M = 15.0                 # 邻域地面半径（主口径，形态学近似）
LOCAL_Q = 10.0                        # 邻域地面分位
NORMAL_Z = 0.5                        # 顶面判定：NORMAL.z ≥ 0.5（朝上）
SURFACE_MODES = ("auto", "top", "all")
NEG_INF = -1e30
METRIC_KEYS = ("id", "name", "value", "range", "unit", "method", "confidence", "limits", "source", "grade")
LOD_RE = re.compile(r"_L(\d+)_", re.IGNORECASE)   # 与 eval_indicators.py 的 LOD 口径一致


def lod_text(tiles):
    """从瓦片文件名动态描述 LOD 组成（不写死任何样本的瓦片数/层级）。"""
    levels = sorted({int(m.group(1)) for rel in tiles
                     for m in [LOD_RE.search(os.path.basename(rel))] if m})
    if levels:
        return f"{len(tiles)} 个 b3dm 含 {len(levels)} 级 LOD（L{levels[0]}–L{levels[-1]}）且相互重叠"
    return f"{len(tiles)} 个 b3dm（文件名未标注 LOD 分级）"


def pct(sorted_vals, p):
    """最近秩分位（对同一输入确定，避免插值浮动）。"""
    if not sorted_vals: return None
    k = int(round((len(sorted_vals) - 1) * p / 100.0))
    return sorted_vals[max(0, min(len(sorted_vals) - 1, k))]


def r6(x): return None if x is None else round(x, 6)


def r2(x): return None if x is None else round(x, 2)


def rel_ref(path, base):
    """相对引用（正斜杠），跨盘等失败时退回文件名，绝不落盘符绝对路径。"""
    try:
        rel = os.path.relpath(os.path.abspath(path), os.path.abspath(base))
    except ValueError:
        return os.path.basename(path)
    return rel.replace("\\", "/")


def gltf_of_b3dm(path):
    """读 b3dm 头 → GLB，返回 (glTF JSON, BIN 字节)；仅标准容器层。"""
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
    """取 float32 VEC3 accessor 的可下标视图（stride=12 时零拷贝）。"""
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


def primitive_views(gltf, binary):
    """[(POSITION 视图, NORMAL 视图或 None, 顶点数)]；NORMAL 用于顶面（朝上）过滤。"""
    out = []
    for mesh in gltf.get("meshes") or []:
        for prim in mesh.get("primitives") or []:
            attrs = prim.get("attributes") or {}
            pos = attribute_view(gltf, binary, attrs.get("POSITION"))
            if not pos:
                continue
            nrm = (attribute_view(gltf, binary, attrs.get("NORMAL"))
                   if attrs.get("NORMAL") is not None else None)
            if nrm and nrm[1] != pos[1]:
                nrm = None
            out.append((pos[0], nrm[0] if nrm else None, pos[1]))
    return out


def accumulate(prims, grids, bbox, cells, normal_z=0.5):
    """顶点投到各档网格：`all` 取全部顶点的 max z，`top` 只取法线朝上（nz≥阈值）的顶点。

    返回 (bbox 外顶点数, 顶点数, 朝上顶点数)。两个口径在同一次遍历里算完，避免重复解包。
    """
    x0, y0, x1, y1 = bbox[0][0], bbox[0][1], bbox[1][0], bbox[1][1]
    dims = {c: (max(1, int(math.ceil((x1 - x0) / c))), max(1, int(math.ceil((y1 - y0) / c))))
            for c in cells}
    outside = vertices = top_vertices = 0
    for pos, nrm, count in prims:
        vertices += count
        for i in range(count):
            x, y, z = pos[3 * i], pos[3 * i + 1], pos[3 * i + 2]
            if not (x0 <= x <= x1 and y0 <= y <= y1):
                outside += 1
                continue
            for cell in cells:
                nx, ny = dims[cell]
                key = (min(nx - 1, int((x - x0) / cell)), min(ny - 1, int((y - y0) / cell)))
                if z > grids[cell]["all"].get(key, NEG_INF):
                    grids[cell]["all"][key] = z
            if nrm is not None and nrm[3 * i + 2] >= normal_z:
                top_vertices += 1
                for cell in cells:
                    nx, ny = dims[cell]
                    key = (min(nx - 1, int((x - x0) / cell)), min(ny - 1, int((y - y0) / cell)))
                    if z > grids[cell]["top"].get(key, NEG_INF):
                        grids[cell]["top"][key] = z
    return outside, vertices, top_vertices


def dense_grids(bbox, cells):
    """numpy 快路径：每档网格两张稠密数组（all/top），初值 -inf。"""
    x0, y0, x1, y1 = bbox[0][0], bbox[0][1], bbox[1][0], bbox[1][1]
    out = {}
    for cell in cells:
        nx = max(1, int(math.ceil((x1 - x0) / cell)))
        ny = max(1, int(math.ceil((y1 - y0) / cell)))
        out[cell] = {"all": np.full((nx, ny), NEG_INF, dtype=np.float64),
                     "top": np.full((nx, ny), NEG_INF, dtype=np.float64)}
    return out


def primitive_arrays(gltf, binary):
    """numpy 视图：[(POSITION (N,3) float32, NORMAL (N,3) 或 None)]。"""
    out = []
    for mesh in gltf.get("meshes") or []:
        for prim in mesh.get("primitives") or []:
            attrs = prim.get("attributes") or {}
            pos = attribute_view(gltf, binary, attrs.get("POSITION"))
            if not pos:
                continue
            pos_view, count = pos
            pos_arr = (np.frombuffer(pos_view, dtype="<f4", count=count * 3)
                       if isinstance(pos_view, memoryview)
                       else np.asarray(pos_view, dtype=np.float32))
            nrm = (attribute_view(gltf, binary, attrs.get("NORMAL"))
                   if attrs.get("NORMAL") is not None else None)
            if nrm and nrm[1] != count:
                nrm = None
            nrm_arr = None
            if nrm:
                nrm_arr = (np.frombuffer(nrm[0], dtype="<f4", count=count * 3)
                           if isinstance(nrm[0], memoryview)
                           else np.asarray(nrm[0], dtype=np.float32))
            out.append((pos_arr.reshape(-1, 3), None if nrm_arr is None else nrm_arr.reshape(-1, 3)))
    return out


def accumulate_arrays(prims, grids, bbox, cells, normal_z=0.5):
    """向量化累加（与 `accumulate` 同语义，只是把逐点循环交给 numpy）。"""
    x0, y0, x1, y1 = bbox[0][0], bbox[0][1], bbox[1][0], bbox[1][1]
    outside = vertices = top_vertices = 0
    for pos, nrm in prims:
        vertices += int(pos.shape[0])
        inside = ((pos[:, 0] >= x0) & (pos[:, 0] <= x1)
                  & (pos[:, 1] >= y0) & (pos[:, 1] <= y1))
        outside += int((~inside).sum())
        if not inside.any():
            continue
        xs, ys, zs = pos[inside, 0], pos[inside, 1], pos[inside, 2]
        for cell in cells:
            grid = grids[cell]["all"]
            nx, ny = grid.shape
            ix = np.clip(((xs - x0) / cell).astype(np.int64), 0, nx - 1)
            iy = np.clip(((ys - y0) / cell).astype(np.int64), 0, ny - 1)
            np.maximum.at(grid, (ix, iy), zs)
        if nrm is None:
            continue
        up = nrm[inside, 2] >= normal_z
        top_vertices += int(up.sum())
        if not up.any():
            continue
        xs, ys, zs = xs[up], ys[up], zs[up]
        for cell in cells:
            grid = grids[cell]["top"]
            nx, ny = grid.shape
            ix = np.clip(((xs - x0) / cell).astype(np.int64), 0, nx - 1)
            iy = np.clip(((ys - y0) / cell).astype(np.int64), 0, ny - 1)
            np.maximum.at(grid, (ix, iy), zs)
    return outside, vertices, top_vertices


def dense_to_dict(grid):
    """稠密数组 → {(ix,iy): z}（只保留有观测的格）。"""
    xs, ys = np.nonzero(grid > NEG_INF / 2)
    return {(int(i), int(j)): float(grid[i, j]) for i, j in zip(xs, ys)}


def components(keys):
    """4 邻域连通分量，返回各分量格数（降序）。"""
    remain, sizes = set(keys), []
    while remain:
        n, stack = 0, [remain.pop()]
        while stack:
            ix, iy = stack.pop()
            n += 1
            for nb in ((ix + 1, iy), (ix - 1, iy), (ix, iy + 1), (ix, iy - 1)):
                if nb in remain:
                    remain.remove(nb)
                    stack.append(nb)
        sizes.append(n)
    return sorted(sizes, reverse=True)


def closing(keys, domain):
    """二值闭运算（4 邻域 ×1 格，只在观测格 domain 内）：先膨胀再腐蚀，抹掉 1 格孔洞与孤立噪点。"""
    def neighbours(cell):
        ix, iy = cell
        return ((ix + 1, iy), (ix - 1, iy), (ix, iy + 1), (ix, iy - 1))

    dilated = set(keys) | {nb for cell in keys for nb in neighbours(cell) if nb in domain}
    return {cell for cell in dilated if all(nb in dilated for nb in neighbours(cell))}


def components_detail(keys, grid, floor_at, cell, bbox, top=5):
    """闭运算后 mask 的前 N 个建筑候选：格数 / 高度分位 / 最高点 / 质心（局部米）。"""
    remain, groups = set(keys), []
    while remain:
        stack, cells = [remain.pop()], []
        while stack:
            ix, iy = stack.pop()
            cells.append((ix, iy))
            for nb in ((ix + 1, iy), (ix - 1, iy), (ix, iy + 1), (ix, iy - 1)):
                if nb in remain:
                    remain.remove(nb)
                    stack.append(nb)
        groups.append(cells)
    groups.sort(key=len, reverse=True)
    out = []
    for cells in groups[:top]:
        hs = sorted(grid[k] - floor_at(k) for k in cells)
        out.append({
            "cells": len(cells),
            "heightP50": round(pct(hs, 50), 2),
            "heightMax": round(hs[-1], 2),
            "centroidXY": [round(bbox["min"][0] + (sum(c[0] for c in cells) / len(cells) + 0.5) * cell, 2),
                           round(bbox["min"][1] + (sum(c[1] for c in cells) / len(cells) + 0.5) * cell, 2)],
        })
    return out


def local_ground(grid, cell, radius_m, q=10.0):
    """诊断用局部地面：每格取窗口内邻格 z 的 Pq（近似形态学开运算）。"""
    step = max(1, int(round(radius_m / cell))); hop = max(1, step // 10)  # 大网格按 1/10 步长抽样，窗口不变
    out = {}
    for (ix, iy), z in grid.items():
        nb = sorted(grid[(ix + dx, iy + dy)] for dx in range(-step, step + 1, hop)
                    for dy in range(-step, step + 1, hop) if (ix + dx, iy + dy) in grid)
        out[(ix, iy)] = pct(nb, q) if nb else z
    return out


def grid_metrics(grid, cell, ground_pct, thr, floors, local=True):
    """单档网格的指标原值（未取整）；local=True 用分块局部地面，False 用全局 P。"""
    if not grid:
        return None
    zs = sorted(grid.values())
    ground = pct(zs, ground_pct)
    loc = local_ground(grid, cell, LOCAL_GROUND_M, LOCAL_Q) if local else None
    floor_at = (lambda k: loc[k]) if loc else (lambda k: ground)
    mask = [k for k, z in sorted(grid.items()) if z - floor_at(k) > thr]
    closed = closing(mask, set(grid))
    hs = sorted(grid[k] - floor_at(k) for k in mask)
    sizes = components(closed)
    far = {fh: sum(cell * cell * math.ceil((grid[k] - floor_at(k)) / fh) for k in mask) for fh in floors}
    return {"ground": ground, "valid": len(zs), "mask": len(mask), "closedMask": len(closed),
            "coverage": len(mask) / len(zs), "count": len(sizes), "sizes": sizes,
            "h50": pct(hs, 50), "h90": pct(hs, 90), "far": far, "zP50": pct(zs, 50)}


def metric(*vals):
    """按 METRIC_KEYS 顺序装配，保证 10 个必填键齐全。"""
    return dict(zip(METRIC_KEYS, vals))


def build(model_path, out_dir, cell_m, ground_pct, thr, surface="auto"):
    with open(model_path, encoding="utf-8") as fh:
        model = json.load(fh)
    if model.get("schema") != "cardo/lowair-model/v1":
        raise ValueError(f"model schema 非 cardo/lowair-model/v1：{model.get('schema')}")
    bbox = model["geometry"]["bboxLocal"]["min"], model["geometry"]["bboxLocal"]["max"]
    area = (bbox[1][0] - bbox[0][0]) * (bbox[1][1] - bbox[0][1])
    root = os.path.dirname(os.path.abspath(model_path))
    tiles = [a["path"] for a in model.get("artifacts") or [] if str(a.get("path", "")).lower().endswith(".b3dm")]
    if not tiles: raise ValueError("model.artifacts 中没有 b3dm")
    cells = sorted(set(SENSITIVITY_CELLS) | {float(cell_m)})
    engine = "numpy" if np is not None else "python"
    grids = dense_grids(bbox, cells) if engine == "numpy" else {c: {"all": {}, "top": {}} for c in cells}
    parsed, failures, verts, outside, top_verts = 0, [], 0, 0, 0
    for rel in tiles:
        try:
            gltf, binary = gltf_of_b3dm(os.path.join(root, rel))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            failures.append({"path": rel, "error": f"{type(exc).__name__}: {exc}"})
            continue
        parsed += 1
        if engine == "numpy":
            out_i, verts_i, top_i = accumulate_arrays(primitive_arrays(gltf, binary), grids, bbox, cells)
        else:
            out_i, verts_i, top_i = accumulate(primitive_views(gltf, binary), grids, bbox, cells)
        outside += out_i
        verts += verts_i
        top_verts += top_i
    if engine == "numpy":
        grids = {c: {"all": dense_to_dict(grids[c]["all"]), "top": dense_to_dict(grids[c]["top"])}
                 for c in cells}

    base_cell = float(cell_m)
    all_cells_n = len(grids[base_cell]["all"])
    top_cells_n = len(grids[base_cell]["top"])
    if not all_cells_n:
        raise ValueError("网格为空：产物无可用 POSITION 顶点")
    normal_share = (top_verts / verts) if verts else 0.0
    top_cell_share = (top_cells_n / all_cells_n) if all_cells_n else 0.0
    if surface == "top":
        use_top = True
    elif surface == "all":
        use_top = False
    else:   # auto：NORMAL 存在（顶点占比≥30%）且 90% 以上的观测格都有顶面顶点 → 用顶面
        use_top = normal_share >= 0.3 and top_cell_share >= 0.9
    surface_mode = "top" if use_top else "all"
    used = {c: grids[c][surface_mode] for c in cells}
    if not used[base_cell]:
        raise ValueError("所选表面口径的网格为空")

    per = {c: grid_metrics(used[c], c, ground_pct, thr, FLOOR_HEIGHTS, local=True) for c in cells}
    per_global = {c: grid_metrics(used[c], c, ground_pct, thr, FLOOR_HEIGHTS, local=False) for c in cells}
    point, sens = per[base_cell], {c: per[c] for c in SENSITIVITY_CELLS if per.get(c)}
    point_g, sens_g = per_global[base_cell], {c: per_global[c] for c in SENSITIVITY_CELLS
                                              if per_global.get(c)}
    loc_base = local_ground(used[base_cell], base_cell, LOCAL_GROUND_M, LOCAL_Q)
    floor_base = lambda k: loc_base[k]
    raw_mask = [k for k, z in used[base_cell].items() if z - floor_base(k) > thr]
    closed_mask = closing(raw_mask, set(used[base_cell]))
    raw_components = len(components(raw_mask))
    buildings = components_detail(closed_mask, used[base_cell], floor_base, base_cell,
                                  model["geometry"]["bboxLocal"])
    surf_txt = ("顶面优先（NORMAL.z ≥ 0.5）" if surface_mode == "top"
                else "全顶点（NORMAL 缺失或覆盖不足）")

    def span(getter, include_global=True):
        vals = [getter(v) for v in sens.values() if getter(v) is not None]
        if include_global and getter(point_g) is not None:
            vals.append(getter(point_g))
        return (min(vals), max(vals)) if vals else (None, None)

    cov_rng, cnt_rng = span(lambda v: v["coverage"]), span(lambda v: v["count"])
    h50_rng, h90_rng = span(lambda v: v["h50"]), span(lambda v: v["h90"])
    far_vals = [v["far"][fh] / area for v in sens.values() for fh in (2.8, 3.3)]
    far_vals += [point_g["far"][fh] / area for fh in (2.8, 3.3)]
    far_rng = (min(far_vals), max(far_vals)) if far_vals else (None, None)
    far_point = point["far"][DEFAULT_FLOOR_M] / area
    cov_global = point_g["coverage"]
    big = point["sizes"][0] if point["sizes"] else 0
    cell_txt = f"{cell_m:g}"
    gt = (f"区间 = cell 0.5 / 1.0 / 2.0 m 三档网格 × 地面口径（{LOCAL_GROUND_M:g} m 邻域 P{LOCAL_Q:g} 主口径 vs 全局 P{ground_pct:g}）的 min–max（每次运行都跑三档，区间不随 --cell-m 变）")
    limits_common = (f"真值状态：无逐项目实测真值；仅绿化/道路做过样例片区两轮真值校核且未达阈值"
                     f"（格级 IoU 0.391 / 0.447 < 0.70 / 0.60），按诊断口径使用、未纳入产品能力；"
                     f"表面口径 = {surf_txt}；"
                     f"{lod_text(tiles)}，网格取最大 z 会混合粗模与精模（若确为多级 LOD）；"
                     f"{outside} 个顶点落在 bboxLocal 外已丢弃；{len(failures)} 个 b3dm 解析失败（见 gaps）。")

    # ---- 纹理/材质 P0：绿化率 / 道路占比（带区间与置信度）----
    tex = texture_materials.analyze(model_path, cell_m=base_cell)
    tex_ok = tex.get("status") == "ok"
    tex_g = tex.get("greenery") or {}
    tex_r = tex.get("road") or {}
    tex_p = tex.get("params") or {}
    tex_d = tex.get("diagnostics") or {}
    tex_reason = tex.get("reason") or "未知原因"
    tex_vege = tex_p.get("vegetation") or {}
    tex_hsv = tex_vege.get("hsv") or {}
    tex_road = tex_p.get("road") or {}
    tex_samp = tex_p.get("sampling") or {}
    tex_thrs = tex_vege.get("exgSensitivityThresholds") or []
    tex_cov = tex_g.get("coverage")
    tex_med = tex_g.get("medianSamplesPerCell")
    tex_conf = tex_g.get("confidence") or "low"
    tex_cov_txt = f"{tex_cov:.0%}" if isinstance(tex_cov, (int, float)) else "n/a"
    tex_med_txt = f"{tex_med:g}" if isinstance(tex_med, (int, float)) else "n/a"
    if tex_ok:
        greenery_metric = metric(
            "greenery_ratio", "绿化率（纹理覆盖代理）", r6(tex_g.get("value")),
            tuple(r6(v) for v in (tex_g.get("range") or [None, None])), "1",
            f"纹理/材质 P0：朝上三角面（几何法线 nz ≥ {NORMAL_Z:g}）重心 UV 采贴图像素；"
            f"可见光植被指数 ExG=2g−r−b（Otsu 自动阈值，clamp {tex_vege.get('exgThresholdClamp')}，"
            f"实际 {tex_vege.get('exgThreshold')}）+ HSV 绿相 {tex_hsv.get('hueRange')}（Pillow 0–255）"
            f"/饱和≥{tex_hsv.get('saturationMin')}/亮度≥{tex_hsv.get('valueMin')} 判绿；"
            f"分母 = 模型覆盖范围内全部朝上表面三角面（含建筑屋面，按三角面面积加权）；"
            f"value = 绿面积 / 全部面积；区间 = cell {tex_samp.get('sensitivityCellM')} × "
            f"ExG 阈值三档 {tex_thrs} min–max。",
            tex_conf,
            f"【诊断口径·两轮真值最高 IoU 0.391 < 0.70，未纳入产品能力】"
            f"纹理覆盖代理值，不是平面用地面积；分母含建筑屋面（按拍板口径），绿色屋面/绿色围挡/"
            f"阴影下的草地/树冠遮挡/贴图拉伸都会误判；采样覆盖 {tex_cov_txt}（每格中位 {tex_med_txt} 样本），"
            f"置信度 {tex_conf}；非屋面口径诊断值 {tex_d.get('nonRoofGreenShare')}，仅作对照；"
            f"{tex_d.get('textureImages')} 张贴图、{tex_d.get('primitivesUsed')} 个 primitive 参与采样，"
            f"{len(tex_d.get('failures') or [])} 个 b3dm 解析失败。" + limits_common,
            ["3dtiles_artifact", "texture_uv_sample", "visible_band_vegetation_index"], "estimated")
        road_metric = metric(
            "road_ratio", "道路占比（纹理覆盖代理）", r6(tex_r.get("value")),
            tuple(r6(v) for v in (tex_r.get("range") or [None, None])), "1",
            f"纹理/材质 P0：非绿样本中，低饱和灰度（sat<{tex_road.get('saturationSensitivity')} 中档，"
            f"V {tex_road.get('valueRange')}）、ExG < {tex_road.get('exgRatioMaxOfThreshold')}×阈值、"
            f"距 15 m 邻域地面高度 ≤{tex_road.get('heightSensitivityM')} 中档，且不在 2.5D 建筑掩膜内判为道路；"
            f"分母同绿化率（模型覆盖范围全部朝上表面三角面，含屋面，面积加权）；"
            f"区间 = cell {tex_samp.get('sensitivityCellM')} × ExG 阈值三档 × 饱和三档 "
            f"{tex_road.get('saturationSensitivity')} × 高度三档 {tex_road.get('heightSensitivityM')} min–max。",
            tex_conf,
            f"【诊断口径·两轮真值最高 IoU 0.447 < 0.60，未纳入产品能力】"
            f"道路占比是纹理覆盖代理值，不是道路面积；窄巷、树冠遮挡、阴影、反光路面、"
            f"铺装与屋顶同色都会误判；分母含建筑屋面，分子已排除建筑掩膜与离地>中档高度的样本；"
            f"采样覆盖 {tex_cov_txt}，置信度 {tex_conf}。" + limits_common,
            ["3dtiles_artifact", "texture_uv_sample", "near_ground_gray_surface"], "estimated")
    else:
        greenery_metric = metric(
            "greenery_ratio", "绿化率（纹理覆盖代理）", None, (None, None), "1",
            f"纹理/材质 P0 未取得可用样本：{tex_reason}；本模型无法从贴图判定绿化，不出估计值。",
            "low",
            "本模型没有可用的纹理/UV 输入（或贴图解码失败），不是能力未覆盖；"
            "换带贴图的 b3dm 可出带区间的代理值。",
            ["none"], "placeholder")
        road_metric = metric(
            "road_ratio", "道路占比（纹理覆盖代理）", None, (None, None), "1",
            f"纹理/材质 P0 未取得可用样本：{tex_reason}；本模型无法从贴图判定道路，不出估计值。",
            "low",
            "本模型没有可用的纹理/UV 输入（或贴图解码失败），不是能力未覆盖；"
            "换带贴图的 b3dm 可出带区间的代理值。",
            ["none"], "placeholder")

    metrics = [
        metric("building_coverage_ratio", "建筑密度", r6(point["coverage"]), (r6(cov_rng[0]), r6(cov_rng[1])), "1",
               f"2.5D 网格法：顶点投到 bboxLocal 的 x/y 平面（{surf_txt}，每格取最大 z，cell={cell_txt} m），"
               f"ground = {LOCAL_GROUND_M:g} m 邻域内观测格 max_z 的 P{LOCAL_Q:g}（形态学近似地面，处理坡地/台地），"
               f"mask = (格 max_z − 该格地面) > {thr:g} m；"
               f"value = mask 格数 / 有观测格数；{gt}。",
               "medium",
               f"地面口径敏感：改回单一全局 P{ground_pct:g} 地面时覆盖率为 {cov_global:.3f}"
               f"（邻域 P{LOCAL_Q:g} 为 {point['coverage']:.3f}），range 已包含该口径差异；"
               f"场地有高差（全局 ground {point['ground']:.2f} m，格 z 中位 {point['zP50']:.2f} m），"
               f"分块地面只是近似，仍非 DTM；"
               f"分母只计有观测格，空洞格不计；顶面口径已滤掉立面顶点，但女儿墙/设备/树冠仍计入。" + limits_common,
               ["3dtiles_artifact", "grid_2_5d"], "estimated"),
        metric("building_count", "建筑栋数（候选）", int(point["count"]), (int(cnt_rng[0]), int(cnt_rng[1])), "栋",
               f"mask（4 邻域闭运算 ×1 格去噪后）的 4 邻域连通分量计数，cell={cell_txt} m；{gt}，区间为整数上下界。",
               "low",
               f"计数对格距极敏感，仍受碎片影响：闭运算前分量 {raw_components} 个、闭运算后 "
               f"{point['count']} 个；最大分量 {big} 格占 mask {point['mask']} 格的 "
               f"{100.0 * big / max(1, point['mask']):.1f}%；间距小于 1 格的相邻建筑会误合并，"
               f"中庭/裙房/连廊并成一体；该数是候选栋数，非报建口径。" + limits_common,
               ["3dtiles_artifact", "grid_2_5d"], "estimated"),
        metric("building_height_p50", "建筑高度 P50", r2(point["h50"]), (r2(h50_rng[0]), r2(h50_rng[1])), "m",
               f"mask 格高度（{surf_txt}的格 max_z − 分块地面）的 P50（最近秩）；"
               f"ground = {LOCAL_GROUND_M:g} m 邻域 P{LOCAL_Q:g}；{gt}。",
               "low",
               "格高度是「格内最高顶点 − ground」，含女儿墙/屋顶设备/树冠等非结构高点；"
               "顶面过滤后立面顶点不再抬高高度，但树冠/设备仍可能计入；"
               "LOD 混合使同一建筑在不同瓦片高度不一致；全局地面在坡地上会压低或抬高整体。" + limits_common,
               ["3dtiles_artifact", "grid_2_5d"], "estimated"),
        metric("building_height_p90", "建筑高度 P90", r2(point["h90"]), (r2(h90_rng[0]), r2(h90_rng[1])), "m",
               f"同 P50 口径，取 mask 格高度分布的 P90（最近秩）；{gt}。",
               "low",
               "P90 对 mask 小样本更敏感，易被少数异常高点拉动；其余同 P50。" + limits_common,
               ["3dtiles_artifact", "grid_2_5d"], "estimated"),
        metric("floor_area_ratio_est", "容积率（估算）", round(far_point, 4),
               (round(far_rng[0], 4), round(far_rng[1], 4)), "1",
               f"Σ(格面积 × ceil(格高度 / 层高)) / 范围面积：格面积 = {cell_txt}×{cell_txt} m²，"
               f"格高度 = {surf_txt}的格 max_z − 邻域地面，层高 {DEFAULT_FLOOR_M:g} m，"
               f"分母 = bbox x/y 投影面积 {area:.2f} m²；"
               f"区间 = 三档网格 × 地面口径（邻域 P{LOCAL_Q:g} / 全局 P{ground_pct:g}）× 层高 2.8 / 3.3 m 全组合 min–max。",
               "low",
               "层高 3.0 m 是未标定经验假设，ceil 取整系统性偏高；每格独立计层，重叠 LOD 不叠加（取 max）；"
               "分母是 bbox 而非红线/净用地；地下室、退台、坡屋顶未建模，因此这是占地强度估算而非报建容积率。"
               + limits_common,
               ["3dtiles_artifact", "grid_2_5d"], "estimated"),
        greenery_metric,
        road_metric,
    ]
    if tex_ok:
        gap_001 = {"code": "IND-GAP-001", "what": "绿化率/道路占比为纹理覆盖代理值，非平面用地面积",
                   "why": "P0 用可见光植被指数 + HSV + 建筑掩膜做像元级材质识别，未做语义分割；"
                          "绿色屋面、绿色围挡、阴影、树冠遮挡、贴图拉伸都会误判",
                   "need": "用外部绿地/道路矢量或逐项目人工真值进一步校核", "owner": "user"}
    else:
        gap_001 = {"code": "IND-GAP-001", "what": f"绿化率/道路占比未出数：{tex_reason}",
                   "why": "模型没有可用纹理/UV，或贴图解码失败；P0 已实现纹理/材质识别，但本模型缺输入",
                   "need": "提供带贴图的 b3dm 或外部用地分类数据", "owner": "upstream"}
    gaps = [
        gap_001,
        {"code": "IND-GAP-009", "what": "纹理代理值仅有样例片区真值校核，未逐项目校核",
         "why": "P0 校核样本为公园类片区（绿化连片、道路规整），对老旧街区碎片化绿化/窄巷道路的代表性有限",
         "need": "逐项目人工勾绘真值或外部用地矢量", "owner": "user"},
        {"code": "IND-GAP-002", "what": "建筑密度/高度/容积率无实测真值（绿化/道路的样例校核未达阈值，见 IND-GAP-001）",
         "why": "样本不含规划指标真值或红线边界，无法校核绝对精度",
         "need": "提供红线/报建指标或人工标注样本", "owner": "user"},
        {"code": "IND-GAP-003", "what": "LOD 混合未消歧",
         "why": f"{lod_text(tiles)}；瓦片 x/y 重叠 "
                f"{(model.get('quality') or {}).get('tileOverlap', {}).get('overlapPairs', 0)} 对（IoU≥0.5），"
                f"网格取 max z 混合了粗模与精模",
         "need": "按 LOD 过滤或只取单层 LOD 的瓦片", "owner": "upstream"},
        {"code": "IND-GAP-004",
         "what": ("顶面口径用顶点法线阈值近似" if surface_mode == "top" else "顶面/立面未区分"),
         "why": (f"按顶点 NORMAL.z ≥ {NORMAL_Z:g} 过滤，法线插值/瓦片边缘可能失真，坡屋顶与曲面仍会漏计"
                 if surface_mode == "top"
                 else "未找到可用 NORMAL 或覆盖不足，立面顶点同样参与格高度"),
         "need": ("按三角面几何法线（重心法）重新分类，或提供分类网格"
                  if surface_mode == "top" else "按法线过滤后再投影"),
         "owner": "upstream"},
        {"code": "IND-GAP-005", "what": "遮挡/空洞按无观测处理",
         "why": "空洞格不计入分母，覆盖率随之偏高或偏低取决于缺失位置",
         "need": "提供采集覆盖范围或补测数据", "owner": "user"},
        {"code": "IND-GAP-006", "what": "坡地/台地地面为近似（15 m 分块 P10），非 DTM",
         "why": f"分块地面覆盖率 {point['coverage']:.3f} vs 全局 P5 {cov_global:.3f}，差异已进 range；"
                f"分块边界/块内坡面仍会误判",
         "need": "提供 DTM/DEM 或分层地面后替换为逐格地面", "owner": "upstream"},
    ]
    for item in failures:
        gaps.append({"code": "IND-GAP-007", "what": f"瓦片解析失败：{item['path']}",
                     "why": item["error"], "need": "检查该 b3dm 完整性", "owner": "upstream"})
    usable = bool(parsed and point["mask"] > 0)
    doc = {
        "schema": SCHEMA, "schemaVersion": SCHEMA_VERSION,
        "generatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "producer": {"name": PRODUCER, "version": PRODUCER_VERSION},
        "source": [{"kind": "model", "ref": rel_ref(model_path, out_dir), "schema": model.get("schema"),
                    "schemaVersion": model.get("schemaVersion")},
                   {"kind": "3dtiles", "ref": rel_ref(os.path.join(root, "tileset.json"), out_dir),
                    "tiles": len(tiles), "parsed": parsed, "vertices": verts}],
        "params": {"cellM": float(cell_m), "groundMode": "local_neighborhood_p10",
                   "localGroundRadiusM": LOCAL_GROUND_M, "localGroundPercentile": LOCAL_Q,
                   "buildingThresholdM": float(thr), "floorHeightM": DEFAULT_FLOOR_M,
                   "surface": surface_mode, "surfaceRequested": surface, "normalZ": NORMAL_Z,
                   "engine": engine,
                   "normalsVertexShare": round(normal_share, 4),
                   "topCellShare": round(top_cell_share, 4),
                   "countClosing": "4-neighborhood-1cell",
                   "rangeCells": list(SENSITIVITY_CELLS), "rangeFloorHeights": [2.8, 3.3],
                   "texture": (tex_p if tex_ok else {"status": "skipped", "reason": tex_reason})},
        "gaps": gaps,
        "scope": {"areaM2": round(area, 2), "areaMethod": "bbox_footprint",
                  "crs": (model.get("crs") or {}).get("srs"),
                  "extentM": {"x": round(bbox[1][0] - bbox[0][0], 3), "y": round(bbox[1][1] - bbox[0][1], 3)},
                  "groundZ": round(point["ground"], 3), "validCells": point["valid"], "maskCells": point["mask"]},
        "metrics": metrics,
        "sensitivity": [{"cellM": c, "coverage": r6(per[c]["coverage"]), "buildingCount": int(per[c]["count"]),
                         "heightP50": r2(per[c]["h50"]), "heightP90": r2(per[c]["h90"]),
                         "floorAreaRatio": round(per[c]["far"][DEFAULT_FLOOR_M] / area, 4),
                         "farByFloor": {f"{fh:g}": round(per[c]["far"][fh] / area, 4)
                                        for fh in FLOOR_HEIGHTS},
                         "maskCells": per[c]["mask"], "validCells": per[c]["valid"]} for c in sorted(per)],
        "diagnostics": {"coverageByGround": {"localNeighborhoodP10": r6(point["coverage"]),
                                             "globalP5": r6(cov_global)},
                        # 全局 P5 口径的整行原值：区间上下限里凡超出三档主口径的，
                        # 都来自这一行（如密度 62.3%）。随指标一起落盘，包内可复算。
                        "globalP5": {"cellM": float(base_cell),
                                     "coverage": r6(point_g["coverage"]),
                                     "buildingCount": int(point_g["count"]),
                                     "heightP50": r2(point_g["h50"]),
                                     "heightP90": r2(point_g["h90"]),
                                     "maskCells": point_g["mask"],
                                     "validCells": point_g["valid"],
                                     "farByFloor": {f"{fh:g}": round(point_g["far"][fh] / area, 4)
                                                    for fh in FLOOR_HEIGHTS}},
                        "surface": {"mode": surface_mode, "requested": surface, "normalZ": NORMAL_Z,
                                    "normalsVertexShare": round(normal_share, 4),
                                    "topCellShare": round(top_cell_share, 4),
                                    "topVertices": top_verts, "vertices": verts},
                        "mask": {"rawCells": len(raw_mask), "closedCells": len(closed_mask),
                                 "rawComponents": raw_components,
                                 "closedComponents": int(point["count"])},
                        "buildings": buildings,
                        "components": {"total": int(point["count"]), "largestCells": int(big),
                                       "largestShare": round(big / max(1, len(closed_mask)), 4),
                                       "lt5Cells": sum(1 for s in point["sizes"] if s < 5)},
                        "cellZ": {"p5": r2(point["ground"]), "p50": r2(point["zP50"])},
                        "texture": (tex_d if tex_ok else {"status": "skipped", "reason": tex_reason})},
        "quality": {"usable": usable,
                    "reason": (f"几何可用、容器完整；表面口径 {surface_mode}"
                               f"（法线覆盖 {normal_share:.0%}）；指标为 2.5D 网格估算"
                               f"（估算值 + 区间 + 置信度），未做精度校核；"
                               + ("绿化/道路为纹理覆盖代理值（带区间/置信度）" if tex_ok
                                  else "绿化/道路因无可用纹理未出数"))
                               if usable else "网格内无建筑候选格或产物不可用",
                    "methodVersion": METHOD_VERSION, "parsedTiles": parsed, "failedTiles": len(failures),
                    "vertices": verts},
    }
    for item in metrics:
        missing = [k for k in METRIC_KEYS if k not in item]
        if missing:
            raise ValueError(f"{item['id']} 缺字段 {missing}")
    return doc


def degraded_doc(model_path, out_dir, err):
    """输入不可用时的降级产物：只做输入检查与口径说明，不编数（quality.usable=false + gaps[]，metrics=[]）。"""
    return {
        "schema": SCHEMA, "schemaVersion": SCHEMA_VERSION,
        "generatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "producer": {"name": PRODUCER, "version": PRODUCER_VERSION},
        "source": [{"kind": "model", "ref": rel_ref(model_path, out_dir), "schema": None, "schemaVersion": None},
                   {"kind": "3dtiles", "ref": None, "tiles": 0, "parsed": 0, "vertices": 0}],
        "params": {"cellM": 1.0, "groundMode": "local_neighborhood_p10",
                   "localGroundRadiusM": LOCAL_GROUND_M, "localGroundPercentile": LOCAL_Q,
                   "buildingThresholdM": 3.0, "floorHeightM": DEFAULT_FLOOR_M,
                   "surface": "n/a", "surfaceRequested": "auto", "normalZ": NORMAL_Z,
                   "normalsVertexShare": 0.0, "topCellShare": 0.0,
                   "countClosing": "4-neighborhood-1cell",
                   "rangeCells": list(SENSITIVITY_CELLS), "rangeFloorHeights": [2.8, 3.3]},
        "gaps": [{"code": "IND-GAP-008", "what": "输入模型不可用，未产出任何指标",
                  "why": f"{type(err).__name__}: {err}",
                  "need": "修复 cardo-lowair-model.json 及其产物后重跑", "owner": "user"}],
        "scope": {"areaM2": None, "areaMethod": "unknown", "crs": None,
                  "extentM": {"x": None, "y": None}, "groundZ": None, "validCells": 0, "maskCells": 0},
        "metrics": [], "sensitivity": [],
        "diagnostics": {"coverageByGround": {"localNeighborhoodP10": None, "globalP5": None},
                        "globalP5": None,
                        "surface": {"mode": "n/a", "requested": "auto", "normalZ": NORMAL_Z,
                                    "normalsVertexShare": 0.0, "topCellShare": 0.0,
                                    "topVertices": 0, "vertices": 0},
                        "mask": {"rawCells": 0, "closedCells": 0,
                                 "rawComponents": 0, "closedComponents": 0},
                        "buildings": [],
                        "components": {"total": 0, "largestCells": 0, "largestShare": 0.0, "lt5Cells": 0},
                        "cellZ": {"p5": None, "p50": None}},
        "quality": {"usable": False,
                    "reason": "输入模型缺失/损坏（schema 不对、缺 geometry、产物不可读等）：只做输入检查与口径说明，不编数。",
                    "methodVersion": METHOD_VERSION, "parsedTiles": 0, "failedTiles": 0, "vertices": 0},
    }


OUTPUT_NAMES = ("现状指标.json", "指标说明.md")


def refuse_overwrite(out_dir, force):
    """同名文件不静默覆盖：已存在且未给 --force 时返回 True。"""
    if force:
        return False
    existing = [n for n in OUTPUT_NAMES if os.path.exists(os.path.join(out_dir, n))]
    if existing:
        print(f"[run_indicators] 输出已存在且未给 --force，不静默覆盖：{existing}（重跑请加 --force 或换 --out）",
              file=sys.stderr)
        return True
    return False


def main(argv=None):
    ap = argparse.ArgumentParser(description="从 cardo-lowair-model/v1 估算现状指标（2.5D 网格法草稿）")
    ap.add_argument("--model", required=True, help="cardo-lowair-model.json 路径")
    ap.add_argument("--out", default=".", help="输出目录（写 现状指标.json + 指标说明.md）")
    ap.add_argument("--cell-m", type=float, default=1.0, help="网格边长（米），默认 1.0")
    ap.add_argument("--ground-percentile", type=float, default=5.0, help="地面分位，默认 5")
    ap.add_argument("--building-threshold-m", type=float, default=3.0, help="建筑判定高差阈值（米），默认 3.0")
    ap.add_argument("--surface", choices=SURFACE_MODES, default="auto",
                    help="表面口径：auto（法线覆盖≥60%% 且顶面格≥50%% 用 top）/ top / all")
    ap.add_argument("--force", action="store_true", help="允许覆盖输出目录已有的同名产物")
    args = ap.parse_args(argv)
    if args.cell_m <= 0 or not 0 <= args.ground_percentile <= 100 or args.building_threshold_m <= 0:
        print("[run_indicators] 参数越界：cell>0、0≤percentile≤100、threshold>0", file=sys.stderr); return 3
    if refuse_overwrite(args.out, args.force):
        return 4
    try:
        doc = build(args.model, args.out, args.cell_m, args.ground_percentile,
                    args.building_threshold_m, args.surface)
        status = 0
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"[run_indicators] 输入不可用：{type(exc).__name__}: {exc}", file=sys.stderr)
        doc, status = degraded_doc(args.model, args.out, exc), 3
    os.makedirs(args.out, exist_ok=True)
    for name, text in (("现状指标.json", json.dumps(doc, ensure_ascii=False, indent=2) + "\n"),
                       ("指标说明.md", render_md(doc))):
        with open(os.path.join(args.out, name), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    if status == 3:
        print("[run_indicators] 已落盘「输入检查与口径说明」降级产物：quality.usable=false、metrics=[]、"
              "缺项见 gaps（IND-GAP-008）；未计算任何数值。")
        return 3
    val = {m["id"]: m["value"] for m in doc["metrics"]}
    print(f"[run_indicators] 写完 {os.path.join(args.out, '现状指标.json')} / 指标说明.md；表面 "
          f"{doc['params'].get('surface')}（法线覆盖 {doc['params'].get('normalsVertexShare')}）；建筑密度 "
          f"{val['building_coverage_ratio']} / 栋数 {val['building_count']} / P50 {val['building_height_p50']} m / "
          f"P90 {val['building_height_p90']} m / 容积率 {val['floor_area_ratio_est']} / usable={doc['quality']['usable']}")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    sys.exit(main())


