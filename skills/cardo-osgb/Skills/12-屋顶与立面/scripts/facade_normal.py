#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""12 · 屋顶与立面 · facade.normal：竖向三角面 → 立面面积（方位角分箱 + 高度层）。

口径（12 自己的 规则/口径.md §5.2，承接 11 输出契约 §5.4）：
  - 逐个 b3dm 读内嵌 GLB（POSITION / NORMAL / indices），三角面法向取三顶点法向均值；
  - 取 |nz| < 0.4 的三角面（近立面），面积 = 三角形 3D 面积；
  - 按方位角 15° 分箱（0° = 正北 = +y，顺时针），归并成 北/东/南/西 四方位占比；
  - 每 3 m 高度层统计立面面积，高度 = 面重心 z − 09 地面（缺值记 unknown）；
  - 与 09 建筑 mask 的格邻接边长 footprintEdgeM：按 09 同参数重算 mask
    （ndsm > 阈值 & observed + close/open 3×3），数 4 邻接界面 × 格宽；
  - 与 roof.segment 同一 LOD 选层口径：只取「mask 内屋面面积 ≥ 0.85×占地」的最细层。

本机有 numpy 时用 `common`；没有时自动用纯 stdlib 的 `common_fallback`，
两条路径算法一致，不装包、不产生 `__pycache__`。

产物（过程文件/12-屋顶与立面/facade-normal/）：
    facade-normal.json / facade-normal.md / _done；_patches/facade-normal.json

用法（工作区根）：
    python <技能目录>/Skills/12-屋顶与立面/scripts/facade_normal.py --workspace .
退出码：0 可用；2 缺输入；3 产出但不可用；4 产物已存在未给 --force。
"""
from __future__ import annotations

import math
import os
import sys

sys.dont_write_bytecode = True

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:                                            # 有 numpy 时复用主线 common
    from common import (GRID_REL, MODEL_REL, OUT_ROOT, RECON_REL, b3dm_files,  # noqa: E402
                        emit, finish, load_grid, load_model, load_origin, parse_args,
                        read_json, write_json, slug)
    COMMON_IMPL = "common"
except ImportError:                             # 本机无 numpy：纯 stdlib 兜底
    from common_fallback import (GRID_REL, MODEL_REL, OUT_ROOT, RECON_REL, b3dm_files,  # noqa: E402
                                 emit, finish, load_grid, load_model, load_origin,
                                 parse_args, read_json, write_json, slug)
    COMMON_IMPL = "common_fallback"

from common_fallback import gltf_and_bin, read_indices, read_vec3, _tile_lod  # noqa: E402,F401

ALGO = "facade.normal"
SLUG = slug(ALGO)

NZ_MAX = 0.4               # |nz| < 0.4 视为立面朝向
AZ_BIN_DEG = 15.0          # 方位角分箱步长
LAYER_M = 3.0              # 高度层厚度
THRESH_DEFAULT_M = 3.0     # 09 建筑阈值（ndsm）
ROOF_NZ_MIN = 0.7          # 选 LOD 层时判「屋面」的法向阈值（与 roof.segment 一致）
N_AZ_BINS = 24

_SECTORS = [("north", 315.0, 45.0), ("east", 45.0, 135.0),
            ("south", 135.0, 225.0), ("west", 225.0, 315.0)]


def _workspace_from_argv() -> str:
    argv = sys.argv[1:]
    for i, item in enumerate(argv):
        if item == "--workspace" and i + 1 < len(argv):
            return argv[i + 1]
        if item.startswith("--workspace="):
            return item.split("=", 1)[1]
    return "."


def _done_ok(workspace: str) -> bool:
    if "--force" in sys.argv:          # 显式 --force 时不短路，重算覆盖
        return False
    path = os.path.join(workspace, OUT_ROOT, "_done", f"{SLUG}.json")
    if not os.path.isfile(path):
        return False
    try:
        doc = read_json(path)
    except (OSError, ValueError):
        return False
    return doc.get("exitCode") == 0 and bool(doc.get("usable"))


# ---------------------------------------------------------------- 栅格 / 面
def grid_lists(grid: dict) -> dict:
    """numpy 数组统一转成「按 x 行、按 y 列」的嵌套列表，两种后端共用同一算法。"""
    def conv(arr):
        return arr.tolist() if hasattr(arr, "tolist") else arr

    dsm = conv(grid["dsm"])
    return {"dsm": dsm, "ground": conv(grid["ground"]), "ndsm": conv(grid["ndsm"]),
            "observed": conv(grid["observed"]), "cell": float(grid["cell"]),
            "x0": float(grid["x0"]), "y0": float(grid["y0"]),
            "nx": len(dsm), "ny": len(dsm[0])}


def collect_faces(workspace: str, model: dict) -> dict:
    """汇总三角面：重心、面积、朝向法向（顶点法向均值优先）+ LOD 层级。"""
    cents, areas, normals, levels = [], [], [], []
    files, failures, vertices, no_normal, has_normal = 0, [], 0, 0, True
    for rel, path in b3dm_files(workspace, model):
        files += 1
        lv = _tile_lod(rel)
        if lv is None:
            lv = -1
        try:
            doc, binary = gltf_and_bin(path)
        except (OSError, ValueError) as exc:
            failures.append({"path": rel.replace("\\", "/"),
                             "error": f"{type(exc).__name__}: {exc}"})
            continue
        for mesh in doc.get("meshes") or []:
            for prim in mesh.get("primitives") or []:
                attrs = prim.get("attributes") or {}
                pos = read_vec3(doc, binary, attrs.get("POSITION"))
                if not pos or len(pos) < 3:
                    continue
                idx = read_indices(doc, binary, prim.get("indices"), len(pos))
                if len(idx) < 3:
                    continue
                idx = idx[:(len(idx) // 3) * 3]
                vertices += len(pos)
                nrm = read_vec3(doc, binary, attrs.get("NORMAL"))
                if nrm is not None and len(nrm) != len(pos):
                    nrm = None
                if nrm is None:
                    has_normal = False
                    no_normal += len(pos)
                for k in range(0, len(idx) - 2, 3):
                    ia, ib, ic = idx[k], idx[k + 1], idx[k + 2]
                    ax, ay, az = pos[ia]
                    bx, by, bz = pos[ib]
                    cx, cy, cz = pos[ic]
                    ux, uy, uz = bx - ax, by - ay, bz - az
                    vx, vy, vz = cx - ax, cy - ay, cz - az
                    gx, gy, gz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
                    mag = math.sqrt(gx * gx + gy * gy + gz * gz)
                    if mag <= 1e-9:
                        continue
                    if nrm is not None:
                        na, nb, nc = nrm[ia], nrm[ib], nrm[ic]
                        vnx = na[0] + nb[0] + nc[0]
                        vny = na[1] + nb[1] + nc[1]
                        vnz = na[2] + nb[2] + nc[2]
                        ln = math.sqrt(vnx * vnx + vny * vny + vnz * vnz)
                        if ln > 1e-9:
                            nx_, ny_, nz_ = vnx / ln, vny / ln, vnz / ln
                        else:
                            nx_, ny_, nz_ = gx / mag, gy / mag, gz / mag
                    else:
                        nx_, ny_, nz_ = gx / mag, gy / mag, gz / mag
                    cents.append(((ax + bx + cx) / 3.0, (ay + by + cy) / 3.0,
                                  (az + bz + cz) / 3.0))
                    areas.append(0.5 * mag)
                    normals.append((nx_, ny_, nz_))
                    levels.append(lv)
    return {"centroids": cents, "areas": areas, "normals": normals, "levels": levels,
            "hasNormal": has_normal, "files": files, "failures": failures,
            "vertices": vertices, "noNormalVertices": no_normal, "tileTotal": files}


def choose_level(faces: dict, inside: list, mask_area: float):
    """与 roof.segment 同一选层口径：mask 内 nz≥0.7 屋面面积 ≈ 建筑占地的最细层。"""
    stats = {}
    for lv, ar, nr, ins in zip(faces["levels"], faces["areas"], faces["normals"], inside):
        if nr[2] >= ROOF_NZ_MIN and ins:
            stats[lv] = stats.get(lv, 0.0) + ar
    if not stats:
        return None, stats, ""
    target = 0.85 * mask_area
    for x in sorted(stats, reverse=True):
        if stats[x] >= target:
            return x, stats, f"取最细完整层：该层 mask 内屋面面积 ≥ 0.85×占地 {mask_area:.0f} m²"
    x = max(stats, key=lambda k: stats[k])
    return x, stats, f"无层达到 0.85×占地，取屋面面积最大层：{stats[x]:.0f} m²"


def subset_faces(faces: dict, keep: list) -> dict:
    out = dict(faces)
    for key in ("centroids", "areas", "normals", "levels"):
        src = faces[key]
        out[key] = [src[i] for i in range(len(src)) if keep[i]]
    return out


# ---------------------------------------------------------------- 09 mask 重算
def _dilate(mask, nx, ny):
    out = [[False] * ny for _ in range(nx)]
    for i in range(nx):
        row = mask[i]
        for j in range(ny):
            if not row[j]:
                continue
            for di in (-1, 0, 1):
                ii = i + di
                if ii < 0 or ii >= nx:
                    continue
                for dj in (-1, 0, 1):
                    jj = j + dj
                    if 0 <= jj < ny:
                        out[ii][jj] = True
    return out


def _erode(mask, nx, ny):
    """与 scipy.ndimage 默认 border_value=0 一致：栅格外按 False。"""
    out = [[False] * ny for _ in range(nx)]
    for i in range(nx):
        for j in range(ny):
            ok = True
            for di in (-1, 0, 1):
                ii = i + di
                if ii < 0 or ii >= nx:
                    ok = False
                    break
                for dj in (-1, 0, 1):
                    jj = j + dj
                    if jj < 0 or jj >= ny or not mask[ii][jj]:
                        ok = False
                        break
                if not ok:
                    break
            out[i][j] = ok
    return out


def building_mask(grid: dict, threshold: float):
    """复刻 09：raw = ndsm>阈值 & observed；close(3×3) 后再 &observed；open(3×3)。"""
    nx, ny = grid["nx"], grid["ny"]
    ndsm, obs = grid["ndsm"], grid["observed"]
    raw = [[bool(obs[i][j] and math.isfinite(ndsm[i][j]) and ndsm[i][j] > threshold)
            for j in range(ny)] for i in range(nx)]
    closed = _erode(_dilate(raw, nx, ny), nx, ny)
    closed = [[bool(closed[i][j] and obs[i][j]) for j in range(ny)] for i in range(nx)]
    return _dilate(_erode(closed, nx, ny), nx, ny)


def mask_at(points: list, grid: dict, mask: list) -> list:
    """点（局部 x,y）落到 09 栅格所在格，返回该格是否属于建筑 mask。"""
    nx, ny, cell, x0, y0 = grid["nx"], grid["ny"], grid["cell"], grid["x0"], grid["y0"]
    out = []
    for p in points:
        ix = int((p[0] - x0) / cell)
        iy = int((p[1] - y0) / cell)
        ix = 0 if ix < 0 else (nx - 1 if ix >= nx else ix)
        iy = 0 if iy < 0 else (ny - 1 if iy >= ny else iy)
        out.append(bool(mask[ix][iy]))
    return out


def footprint_edge(mask: list, cell: float) -> float:
    """建筑 mask 的 4 邻接界面总长（含栅格外边界）× 格宽；每条界面只数一次。"""
    nx, ny = len(mask), len(mask[0])
    edge = 0
    for i in range(nx):
        for j in range(ny):
            if not mask[i][j]:
                continue
            if i == 0 or not mask[i - 1][j]:
                edge += 1
            if i == nx - 1 or not mask[i + 1][j]:
                edge += 1
            if j == 0 or not mask[i][j - 1]:
                edge += 1
            if j == ny - 1 or not mask[i][j + 1]:
                edge += 1
    return float(edge * cell)


# ---------------------------------------------------------------- 主流程
def build(workspace: str) -> dict:
    model = load_model(workspace)
    grid = grid_lists(load_grid(workspace))
    recon = read_json(os.path.join(workspace, RECON_REL))
    threshold = float((recon.get("params") or {}).get("buildingThresholdM") or THRESH_DEFAULT_M)
    rec_cleaned = (((recon.get("diagnostics") or {}).get("mask") or {}).get("cleanedCells"))
    origin = load_origin(workspace) or [0.0, 0.0, 0.0]
    ox, oy = float(origin[0]), float(origin[1])

    faces = collect_faces(workspace, model)
    mask = building_mask(grid, threshold)
    mask_cells = sum(1 for row in mask for v in row if v)
    mask_area = mask_cells * grid["cell"] ** 2

    if not faces["areas"]:
        doc = emit(workspace, ALGO,
                   inputs={"model": MODEL_REL.replace(os.sep, "/"),
                           "grid": GRID_REL.replace(os.sep, "/"),
                           "commonImpl": COMMON_IMPL},
                   params={"normalZMax": NZ_MAX}, summary={
                       "facadeAreaM2": None, "footprintEdgeM": None,
                       "northShare": None, "eastShare": None,
                       "southShare": None, "westShare": None},
                   items=[], gaps=[{"code": "ENH-GAP-001",
                                    "what": "没有可用的三角面",
                                    "why": "b3dm 解析失败或全部三角形退化",
                                    "need": "检查 03 产物与 b3dm 完整性"}],
                   usable=False, reasons=["没有可用的立面三角面"],
                   markdown="# 立面朝向（12 · facade.normal）\n\n没有可用的三角面。\n")
        write_patch(workspace)
        return doc

    level, _stats, level_rule = choose_level(
        faces, mask_at(faces["centroids"], grid, mask), mask_area)
    faces = subset_faces(faces, [lv == level for lv in faces["levels"]])
    c, a, n = faces["centroids"], faces["areas"], faces["normals"]

    edge_m = footprint_edge(mask, grid["cell"])

    bin_area = [0.0] * N_AZ_BINS
    bin_count = [0] * N_AZ_BINS
    sector_area = {name: 0.0 for name, _, _ in _SECTORS}
    layer_area, layer_count = {}, {}
    z_sum, z_a, z_max, unknown_area, total, kept = 0.0, 0.0, None, 0.0, 0.0, 0

    for i in range(len(a)):
        nz = n[i][2]
        if abs(nz) >= NZ_MAX:
            continue
        ar = a[i]
        total += ar
        kept += 1
        az = math.degrees(math.atan2(n[i][0], n[i][1])) % 360.0
        b = int(az // AZ_BIN_DEG) % N_AZ_BINS
        bin_area[b] += ar
        bin_count[b] += 1
        for name, lo, hi in _SECTORS:
            hit = (lo <= az < hi) if lo < hi else (az >= lo or az < hi)
            if hit:
                sector_area[name] += ar
                break
        gx = c[i][0]
        gy = c[i][1]
        ii = int((gx - grid["x0"]) / grid["cell"])
        jj = int((gy - grid["y0"]) / grid["cell"])
        ii = 0 if ii < 0 else (grid["nx"] - 1 if ii >= grid["nx"] else ii)
        jj = 0 if jj < 0 else (grid["ny"] - 1 if jj >= grid["ny"] else jj)
        g = grid["ground"][ii][jj]
        if math.isfinite(g):
            z_above = c[i][2] - g
            if z_above < 0:
                z_above = 0.0
            li = int(z_above // LAYER_M)
            layer_area[li] = layer_area.get(li, 0.0) + ar
            layer_count[li] = layer_count.get(li, 0) + 1
            z_sum += z_above * ar
            z_a += ar
            z_max = z_above if z_max is None or z_above > z_max else z_max
        else:
            unknown_area += ar

    bins = [{
        "kind": "azimuthBin", "binIndex": b,
        "azimuthFromDeg": round(b * AZ_BIN_DEG, 1),
        "azimuthToDeg": round((b + 1) * AZ_BIN_DEG, 1),
        "azimuthCenterDeg": round(b * AZ_BIN_DEG + AZ_BIN_DEG / 2.0, 1),
        "areaM2": round(bin_area[b], 2),
        "share": round(bin_area[b] / total, 4) if total > 0 else None,
        "faceCount": bin_count[b],
    } for b in range(N_AZ_BINS)]

    layers = [{
        "kind": "heightLayer", "layerIndex": li,
        "zFromM": round(li * LAYER_M, 2),
        "zToM": round((li + 1) * LAYER_M, 2),
        "areaM2": round(layer_area[li], 2),
        "faceCount": layer_count[li],
    } for li in sorted(layer_area)]

    dom_name, dom_area = max(sector_area.items(), key=lambda kv: kv[1])
    dom_bin = max(range(N_AZ_BINS), key=lambda b: bin_area[b])
    summary = {
        "facadeAreaM2": round(total, 2),
        "footprintEdgeM": round(edge_m, 2),
        "northShare": round(sector_area["north"] / total, 4) if total > 0 else None,
        "eastShare": round(sector_area["east"] / total, 4) if total > 0 else None,
        "southShare": round(sector_area["south"] / total, 4) if total > 0 else None,
        "westShare": round(sector_area["west"] / total, 4) if total > 0 else None,
        "dominantAzimuthDeg": round(dom_bin * AZ_BIN_DEG + AZ_BIN_DEG / 2.0, 1)
        if total > 0 else None,
        "dominantShare": round(bin_area[dom_bin] / total, 4) if total > 0 else None,
        "dominantSectorShare": round(dom_area / total, 4) if total > 0 else None,
        "facadeTriangleCount": kept,
        "meanFacadeHeightM": round(z_sum / z_a, 2) if z_a > 0 else None,
        "maxFacadeHeightM": round(z_max, 2) if z_max is not None else None,
        "heightUnknownAreaM2": round(unknown_area, 2),
        "heightLayerCount": len(layers),
        "maskCells": mask_cells,
        "maskCells09": rec_cleaned,
        "lodLevel": level,
        "b3dmCount": faces["files"],
        "b3dmTotalCount": faces["tileTotal"],
        "b3dmFailedCount": len(faces["failures"]),
    }

    gaps = [{
        "code": "ENH-GAP-001",
        "what": "立面面积含山墙、围墙、灌木/树冠侧面，未区分建筑与附属",
        "why": "只按 |nz| < 0.4 的法向筛竖向面，没有语义/纹理分类，也没有剔除植被与围挡",
        "need": "接纹理颜色或语义分割剔除植被/围墙，再做「建筑立面」口径重算",
    }, {
        "code": "ENH-GAP-002",
        "what": "三角面朝向外侧由顶点法向均值决定；法向缺失时退回叉积，绕向不一致会翻向",
        "why": "GLB 的 NORMAL 与三角面绕向不保证一致，四方位占比对翻向敏感",
        "need": "统一三角面绕向或按「面重心指向体心之外」重定向",
    }, {
        "code": "ENH-GAP-003",
        "what": "高度层依赖 09 地面栅格，未观测/空洞格无法定高度层",
        "why": f"09 地面只在观测格有值；本层实测缺有效地面值的立面面积 {summary['heightUnknownAreaM2']} m²",
        "need": "09 落盘连续地面面（最近邻填充）供高度分层使用",
    }, {
        "code": "ENH-GAP-004",
        "what": "footprintEdgeM 是重算 mask 的格邻接边长（1 m 栅格锯齿），不是真实墙脚线",
        "why": "09 未落盘 mask，本脚本按同参数重算，边界呈阶梯",
        "need": "09 落盘 mask 栅格；需精确墙脚长度时改用 11 的轮廓面边长",
    }]
    if not faces["hasNormal"]:
        gaps.append({"code": "ENH-GAP-005",
                     "what": f"{faces['noNormalVertices']} 个顶点所在 primitive 缺 NORMAL",
                     "why": "只能按几何叉积法向判定竖向面，朝向可能整体反向",
                     "need": "源模型补法线"})
    if rec_cleaned is not None and mask_cells != int(rec_cleaned):
        gaps.append({"code": "ENH-GAP-006",
                     "what": f"重算 mask {mask_cells} 格 ≠ 09 记录 {rec_cleaned} 格",
                     "why": "09 未落盘 mask，按参数重算；形态学边界处理差异会造成格数偏差",
                     "need": "09 落盘 mask 栅格供 12 直接读取"})
    gaps.append({"code": "ENH-GAP-007",
                 "what": f"只用 LOD 第 `{level}` 层（{level_rule}），未做跨层去重",
                 "why": "3D Tiles 金字塔每层都覆盖同一场景，全部 b3dm 相加会把同一面墙重复计数",
                 "need": "按视锥/屏幕误差选层，或先做跨层几何去重再统计"})
    if COMMON_IMPL != "common":
        gaps.append({"code": "ENH-GAP-008",
                     "what": "本机未装 numpy，使用纯 stdlib 兜底实现（common_fallback）",
                     "why": "算法与 numpy 路径一致，但三角面逐面循环更慢；数值口径不变",
                     "need": "在工作区 Python 环境安装 numpy 后重跑可获得同一结果"})

    usable = total > 0
    reasons = [] if usable else ["没有 |nz| < 0.4 的立面三角面"]
    orientation_cn = {"north": "北", "east": "东", "south": "南", "west": "西"}[dom_name]

    items = bins + layers
    markdown = render_md(summary, bins, layers, gaps, faces, orientation_cn,
                         threshold, level, level_rule, total)

    doc = emit(workspace, ALGO,
               inputs={"model": MODEL_REL.replace(os.sep, "/"),
                       "grid": GRID_REL.replace(os.sep, "/"),
                       "recon": RECON_REL.replace(os.sep, "/"),
                       "origin": [ox, oy],
                       "b3dm": faces["files"],
                       "vertices": faces["vertices"],
                       "maskCells09": rec_cleaned,
                       "commonImpl": COMMON_IMPL},
               params={"normalZMax": NZ_MAX, "azimuthBinDeg": AZ_BIN_DEG,
                       "heightLayerM": LAYER_M, "buildingThresholdM": threshold,
                       "lodLevel": level, "lodSelection": level_rule,
                       "morphology": "close(3x3)+open(3x3)（自实现）",
                       "azimuthZero": "正北(+y)，顺时针",
                       "faceNormalRule": "三顶点法向均值（缺 NORMAL 用叉积）"},
               summary=summary, items=items, gaps=gaps, usable=usable, reasons=reasons,
               markdown=markdown)

    write_patch(workspace)
    return doc


def write_patch(workspace: str) -> None:
    write_json(os.path.join(workspace, OUT_ROOT, "_patches", f"{SLUG}.json"), {
        "id": ALGO, "status": "ready", "step": "12",
        "impl": "自实现：b3dm 三角面顶点法向均值 + 方位角 15° 分箱 + 3 m 高度层",
        "evidence": "Skills/12-屋顶与立面/scripts/facade_normal.py",
        "sourceName": "本链自定法向分箱",
        "sourceLink": "",
        "license": "自研代码",
        "plain": "按朝向与高度统计竖向面面积，给出四方位占比与墙脚接触边长",
    })


def render_md(summary, bins, layers, gaps, faces, orientation_cn,
              threshold, level, level_rule, total) -> str:
    lines = [
        "# 立面朝向（12 · facade.normal）", "",
        "## 方法", "",
        "- 逐个 b3dm 读内嵌 GLB 的 `POSITION / NORMAL / indices`，三角面法向 = 三顶点法向均值；",
        f"- 取 `|nz| < {NZ_MAX}` 的三角面（近立面），面积用三角形 3D 面积；",
        f"- 方位角以正北（+y）为 0°、顺时针，按 {AZ_BIN_DEG:.0f}° 分箱；"
        "归并成 北(315–45°)/东(45–135°)/南(135–225°)/西(225–315°) 四方位占比；",
        f"- 高度层厚 {LAYER_M:.0f} m：高度 = `max(面重心 z − 09 地面, 0)`（最近格，缺值记 unknown）；",
        f"- `footprintEdgeM`：按 09 参数重算建筑 mask（`ndsm>{threshold:.1f} & observed`"
        " + close/open 3×3），数 4 邻接界面 × 格宽。", "",
        "## summary", "",
        f"- 立面面积 `{summary['facadeAreaM2']}` m²；建筑 mask 格邻接边长 `{summary['footprintEdgeM']}` m；",
        f"- 四方位占比 北 `{summary['northShare']}` / 东 `{summary['eastShare']}` / "
        f"南 `{summary['southShare']}` / 西 `{summary['westShare']}`；四方位最大 **{orientation_cn}**"
        f"（`{summary['dominantSectorShare']}`）；最集中的 15° 箱 方位 `{summary['dominantAzimuthDeg']}`°，"
        f"占比 `{summary['dominantShare']}`",
        f"- 选层：LOD `{level}`（{level_rule}）；读 `{faces['files']}`/`{faces['tileTotal']}` 个 b3dm；",
        f"- 后端：`{COMMON_IMPL}`；立面三角面 `{summary['facadeTriangleCount']}`；"
        f"平均立面高度 `{summary['meanFacadeHeightM']}` m，最高 `{summary['maxFacadeHeightM']}` m；"
        f"高度层 `{summary['heightLayerCount']}` 层；缺地面面积 `{summary['heightUnknownAreaM2']}` m²", "",
        "## 方位角分箱（15°）", "",
        "| 方位区间 ° | 中心 ° | 面积 m² | 占比 | 面数 |", "|---|---|---|---|---|",
    ]
    for b in bins:
        if b["areaM2"] <= 0:
            continue
        lines.append(f"| {b['azimuthFromDeg']:.0f}–{b['azimuthToDeg']:.0f} | "
                     f"{b['azimuthCenterDeg']:.1f} | {b['areaM2']:.2f} | "
                     f"{b['share']:.4f} | {b['faceCount']} |")
    lines += ["", "## 高度层（3 m）", "", "| 层 | 高程区间 m | 面积 m² | 面数 |", "|---|---|---|---|"]
    for layer in layers:
        lines.append(f"| {layer['layerIndex']} | {layer['zFromM']:.2f}–{layer['zToM']:.2f} | "
                     f"{layer['areaM2']:.2f} | {layer['faceCount']} |")
    lines += ["", "## 局限", ""]
    for gap in gaps:
        lines.append(f"- `{gap['code']}` {gap['what']}：{gap['why']}（需：{gap['need']}）")
    lines += [
        "- 立面面积是三角面 3D 面积，**包含山墙/围墙/植被侧面**，不是「建筑外墙面积」。",
        "- 四方位只按法向水平投影分箱；弧形墙、45° 斜墙会被分到相邻两箱。",
        "- `footprintEdgeM` 是 1 m 栅格 mask 的格邻接边长，与真实墙脚长度有锯齿偏差。", ""]
    if faces["failures"]:
        lines += ["## 解析失败的 b3dm", ""]
        for f in faces["failures"][:10]:
            lines.append(f"- `{f['path']}`：{f['error']}")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    workspace = _workspace_from_argv()
    if _done_ok(workspace):
        print("[12:facade.normal] 已完成（_done exitCode=0），跳过，不改任何文件")
        return 0
    for rel in (MODEL_REL, GRID_REL, RECON_REL):
        if not os.path.isfile(os.path.join(workspace, rel)):
            print(f"[12:facade.normal] 缺输入：{rel}（先跑 03/09）", file=sys.stderr)
            return 2
    args = parse_args(ALGO, "12 · 立面朝向（方位角分箱 + 高度层）")
    doc = build(args.workspace)
    return finish(doc)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
