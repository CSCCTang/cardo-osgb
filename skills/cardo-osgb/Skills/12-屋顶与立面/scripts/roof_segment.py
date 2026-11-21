#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""12 · 屋顶与立面 · roof.segment：b3dm 三角面 → 屋面子段（法向半球量化 + 平面偏移聚类）。

口径（12 自己的 规则/口径.md §5.1，承接 11 输出契约 §5.3）：
  - 逐个 b3dm 读内嵌 GLB（POSITION / NORMAL / indices），三角面法向取三顶点 NORMAL 的面积加权均值
    （缺失或退化时退回叉积几何法向）；
  - 取 nz ≥ 0.7 的屋面三角面，再用 09 建筑 mask（ndsm>阈值 & observed + close/open 3×3）
    挡掉地形/树冠的朝上面；
  - 法向按「方位角 15° + 倾角 5°」半球量化分箱，箱内按平面偏移 d = n·centroid 排序，
    相邻差 > 0.5 m 即切开，得到屋面子段；
  - 每段统计面积 / 倾角 / 平面度 rmsM / 面数 / 质心，按 09 逐栋 OBB 包含或最近质心（≤60 m）归属建筑；
  - 3D Tiles 的 LOD 金字塔每层覆盖同一场景，先按面统计再挑「mask 内屋面面积 ≥ 0.85×占地」的最细层，
    只用该层，避免逐层重复计数。

本机有 numpy 时用 `common`（主线只读契约）；没有时自动用纯 stdlib 的 `common_fallback`，
两条路径的算法完全一致（数组统一用 Python 列表），不装包、不产生 `__pycache__`。

产物（过程文件/12-屋顶与立面/roof-segment/）：
    roof-segment.json / roof-segment.md / _done；_patches/roof-segment.json

用法（工作区根）：
    python <技能目录>/Skills/12-屋顶与立面/scripts/roof_segment.py --workspace .
退出码：0 可用；2 缺输入；3 产出但不可用；4 产物已存在未给 --force。
"""
from __future__ import annotations

import math
import os
import re
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

from common_fallback import gltf_and_bin, read_indices, read_vec3  # noqa: E402

ALGO = "roof.segment"
SLUG = slug(ALGO)
ATTRS_REL = os.path.join("过程文件", "09-三维要素反演", "逐栋属性.json")

NZ_MIN = 0.7               # 屋面法向 z 分量下限（≈ 倾角 ≤ 45.6°）
AZ_BIN_DEG = 15.0          # 法向方位角量化步长
TILT_BIN_DEG = 5.0         # 法向倾角量化步长
PLANE_TOL_M = 0.5          # 同一法向箱内平面偏移聚类容差
MIN_SEG_AREA_M2 = 2.0      # 子段最小面积
FLAT_TILT_DEG = 5.0        # 「平屋面」倾角上限
MAX_MATCH_M = 60.0         # 建筑归属最近质心距离上限
ITEM_LIMIT = 20
THRESH_DEFAULT_M = 3.0     # 09 建筑阈值（ndsm）


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


def _tile_lod(rel: str):
    """从 b3dm 路径取 LOD 层级（`..._L19_2.b3dm` → 19）；无层级标记返回 None。"""
    m = re.search(r"_L(\d+)_", rel.replace("\\", "/"))
    return int(m.group(1)) if m else None


def collect_faces(workspace: str, model: dict) -> dict:
    """汇总全部 b3dm 的三角面：重心、面积、法向、LOD 层级（含解析失败清单）。"""
    cents, areas, normals, levels = [], [], [], []
    files, failures, vertices, no_normal = 0, [], 0, 0
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
                    no_normal += len(pos)
                for k in range(0, len(idx) - 2, 3):
                    ia, ib, ic = idx[k], idx[k + 1], idx[k + 2]
                    ax, ay, az = pos[ia]
                    bx, by, bz = pos[ib]
                    cx, cy, cz = pos[ic]
                    ux, uy, uz = bx - ax, by - ay, bz - az
                    vx, vy, vz = cx - ax, cy - ay, cz - az
                    nx_, ny_, nz_ = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
                    mag = math.sqrt(nx_ * nx_ + ny_ * ny_ + nz_ * nz_)
                    if mag <= 1e-9:
                        continue
                    area = 0.5 * mag
                    cent = ((ax + bx + cx) / 3.0, (ay + by + cy) / 3.0, (az + bz + cz) / 3.0)
                    if nrm is not None:
                        na, nb, nc = nrm[ia], nrm[ib], nrm[ic]
                        vnx = na[0] + nb[0] + nc[0]
                        vny = na[1] + nb[1] + nc[1]
                        vnz = na[2] + nb[2] + nc[2]
                        ln = math.sqrt(vnx * vnx + vny * vny + vnz * vnz)
                        if ln > 1e-9:
                            nx_, ny_, nz_ = vnx / ln, vny / ln, vnz / ln
                        else:
                            nx_, ny_, nz_ = nx_ / mag, ny_ / mag, nz_ / mag
                    else:
                        nx_, ny_, nz_ = nx_ / mag, ny_ / mag, nz_ / mag
                    cents.append(cent)
                    areas.append(area)
                    normals.append((nx_, ny_, nz_))
                    levels.append(lv)
    return {"centroids": cents, "areas": areas, "normals": normals, "levels": levels,
            "files": files, "failures": failures, "vertices": vertices,
            "noNormalVertices": no_normal, "tileTotal": files}


def choose_level(faces: dict, inside: list, mask_area: float):
    """按「mask 内 nz≥0.7 的屋面面积 ≈ 建筑占地」挑一个 LOD 层。

    3D Tiles 的 LOD 金字塔每层覆盖同一场景，全部相加会重复计数；
    逐层算一遍 mask 内屋面面积，取「达到 0.85×占地」的最细层（细层不完整时退回面积最大层）。
    """
    stats = {}
    for lv, ar, nr, ins in zip(faces["levels"], faces["areas"], faces["normals"], inside):
        if nr[2] >= NZ_MIN and ins:
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


# ---------------------------------------------------------------- 09 建筑 mask
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


# ---------------------------------------------------------------- 建筑归属
def point_in_ring(pt, ring) -> bool:
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


def load_buildings(workspace: str, ox: float, oy: float) -> list:
    """09 逐栋：质心转局部坐标 + OBB 环（局部坐标，已闭合）。"""
    path = os.path.join(workspace, ATTRS_REL)
    if not os.path.isfile(path):
        return []
    out = []
    for item in (read_json(path).get("buildings") or []):
        cent = item.get("centroidXY")
        fp = item.get("footprint") or {}
        corners = fp.get("corners") or []
        ring = []
        if len(corners) >= 3:
            ring = [[float(p[0]), float(p[1])] for p in corners]
            ring.append(ring[0])
        out.append({"id": item.get("id"), "label": item.get("label"),
                    "areaM2": float(item.get("areaM2") or 0.0),
                    "centroid": [float(cent[0]) - ox, float(cent[1]) - oy] if cent else None,
                    "ring": ring})
    return out


def assign_building(x: float, y: float, buildings: list):
    if not buildings:
        return None, None
    inside = [b for b in buildings if len(b["ring"]) >= 4 and point_in_ring((x, y), b["ring"])]
    if inside:
        best = min(inside, key=lambda b: b["areaM2"])
        return best["id"], "obb-inside"
    near = [b for b in buildings if b["centroid"] is not None]
    if not near:
        return None, None
    best, dist = None, MAX_MATCH_M
    for b in near:
        d = math.hypot(b["centroid"][0] - x, b["centroid"][1] - y)
        if d < dist:
            best, dist = b, d
    if best is None:
        return None, None
    if dist <= MAX_MATCH_M:
        return best["id"], f"nearest-centroid {dist:.1f} m"
    return None, None


# ---------------------------------------------------------------- 聚类
def segment(faces: dict, buildings: list):
    """法向量化 + 平面偏移聚类 → 子段列表（按面积降序）。"""
    c, a, n = faces["centroids"], faces["areas"], faces["normals"]
    sel = [i for i in range(len(a)) if n[i][2] >= NZ_MIN]
    if not sel:
        return [], {"roofFaces": 0, "subMinSegments": 0}

    groups = {}
    for i in sel:
        az = math.degrees(math.atan2(n[i][0], n[i][1])) % 360.0
        tilt = math.degrees(math.acos(max(-1.0, min(1.0, n[i][2]))))
        code = (int(az // AZ_BIN_DEG) % 24, int(round(tilt / TILT_BIN_DEG)))
        groups.setdefault(code, []).append(i)

    segments, sub_min = [], 0
    for code in sorted(groups):
        pick = groups[code]
        mx = my = mz = 0.0
        for i in pick:
            w = a[i]
            mx += n[i][0] * w
            my += n[i][1] * w
            mz += n[i][2] * w
        ln = math.sqrt(mx * mx + my * my + mz * mz)
        if ln < 1e-9:
            continue
        m = (mx / ln, my / ln, mz / ln)
        ds = [c[i][0] * m[0] + c[i][1] * m[1] + c[i][2] * m[2] for i in pick]
        order = sorted(range(len(pick)), key=lambda t: ds[t])

        runs, start = [], 0
        for t in range(1, len(order) + 1):
            if t == len(order) or ds[order[t]] - ds[order[t - 1]] > PLANE_TOL_M:
                runs.append((start, t))
                start = t
        for st, en in runs:
            ii = [pick[order[t]] for t in range(st, en)]
            area = sum(a[t] for t in ii)
            if area <= 0:
                continue
            dmean = sum(ds[order[t]] * a[pick[order[t]]] for t in range(st, en)) / area
            rms = math.sqrt(sum((ds[order[t]] - dmean) ** 2 * a[pick[order[t]]]
                                for t in range(st, en)) / area)
            cxx = sum(c[t][0] * a[t] for t in ii) / area
            cyy = sum(c[t][1] * a[t] for t in ii) / area
            czz = sum(c[t][2] * a[t] for t in ii) / area
            if area < MIN_SEG_AREA_M2:
                sub_min += 1
                continue
            bid, how = assign_building(cxx, cyy, buildings)
            segments.append({
                "areaM2": area,
                "tiltDeg": math.degrees(math.acos(max(-1.0, min(1.0, m[2])))),
                "azimuthDeg": math.degrees(math.atan2(m[0], m[1])) % 360.0,
                "rmsM": rms, "planeOffsetM": dmean, "normal": list(m),
                "centroid": [cxx, cyy, czz], "faceCount": len(ii),
                "buildingId": bid, "matchHow": how,
            })
    segments.sort(key=lambda s: -s["areaM2"])
    return segments, {"roofFaces": len(sel), "subMinSegments": sub_min}


# ---------------------------------------------------------------- 主流程
def build(workspace: str) -> dict:
    model = load_model(workspace)
    grid = grid_lists(load_grid(workspace))
    recon = read_json(os.path.join(workspace, RECON_REL))
    threshold = float((recon.get("params") or {}).get("buildingThresholdM") or THRESH_DEFAULT_M)
    origin = load_origin(workspace) or [0.0, 0.0, 0.0]
    ox, oy = float(origin[0]), float(origin[1])

    faces = collect_faces(workspace, model)
    mask = building_mask(grid, threshold)
    mask_cells = sum(1 for row in mask for v in row if v)
    mask_area = mask_cells * grid["cell"] ** 2
    level, level_rule, nz_only = None, "", 0
    if faces["areas"]:
        inside_all = mask_at(faces["centroids"], grid, mask)
        level, _stats, level_rule = choose_level(faces, inside_all, mask_area)
        keep = [lv == level for lv in faces["levels"]]
        faces = subset_faces(faces, keep)
        nz_only = sum(1 for nr in faces["normals"] if nr[2] >= NZ_MIN)
        faces = subset_faces(faces, mask_at(faces["centroids"], grid, mask))
    buildings = load_buildings(workspace, ox, oy)
    segments, counts = segment(faces, buildings)

    roof_area = mean_tilt = flat_share = None
    roof_faces = [(faces["areas"][i], faces["normals"][i][2])
                  for i in range(len(faces["areas"])) if faces["normals"][i][2] >= NZ_MIN]
    if roof_faces:
        roof_area = sum(w for w, _ in roof_faces)
        tilts = [math.degrees(math.acos(max(-1.0, min(1.0, nz)))) for _, nz in roof_faces]
        mean_tilt = sum(t * w for t, (w, _) in zip(tilts, roof_faces)) / roof_area
        flat_share = sum(w for t, (w, _) in zip(tilts, roof_faces)
                         if t <= FLAT_TILT_DEG) / roof_area

    summary = {
        "roofPlaneCount": len(segments),
        "roofAreaM2": round(roof_area, 2) if roof_area is not None else None,
        "meanTiltDeg": round(mean_tilt, 2) if mean_tilt is not None else None,
        "flatPlaneShare": round(flat_share, 4) if flat_share is not None else None,
        "segmentedAreaM2": round(sum(s["areaM2"] for s in segments), 2),
        "roofTriangleCount": counts["roofFaces"],
        "nzRoofTriangleCount": nz_only,
        "subMinSegmentCount": counts["subMinSegments"],
        "buildingMatchedPlaneCount": sum(1 for s in segments if s["buildingId"]),
        "unassignedPlaneCount": sum(1 for s in segments if not s["buildingId"]),
        "maskCells": mask_cells,
        "lodLevel": level,
        "b3dmCount": faces["files"],
        "b3dmTotalCount": faces["tileTotal"],
        "b3dmFailedCount": len(faces["failures"]),
    }

    gaps = [{
        "code": "ENH-GAP-001",
        "what": "未做 RANSAC 随机采样",
        "why": "屋面平面用「法向分箱 + 平面偏移聚类」确定性划分，不是 RANSAC 迭代；"
               "对窄条面、曲面屋顶与噪声法向的鲁棒性弱于随机一致集",
        "need": "接入 RANSAC 平面拟合（Fischler & Bolles 1981）做交叉复核，重点看倾斜屋面",
    }, {
        "code": "ENH-GAP-002",
        "what": "屋面附属物未剔除",
        "why": "只按几何（nz ≥ 0.7）与法向聚类，女儿墙压顶、天窗、空调/设备顶、树冠顶面、"
               "棚顶都会并入屋面子段，面积与倾角被拉偏",
        "need": "用纹理颜色 / 语义分割或「距屋面主平面高差」过滤附属物后重算",
    }, {
        "code": "ENH-GAP-003",
        "what": "建筑归属用 OBB 包含 + 最近质心 ≤60 m，跨栋与贴边屋顶可能误判",
        "why": "09 未落盘逐格 label 栅格，只能按逐栋 OBB/质心近似；相邻建筑共墙处的屋顶面"
               "会被分到同一栋或错栋",
        "need": "09 落盘逐栋 label 栅格，按面重心所在格直接归属",
    }]
    if faces["noNormalVertices"]:
        gaps.append({"code": "ENH-GAP-004",
                     "what": f"{faces['noNormalVertices']} 个顶点的 primitive 缺 NORMAL",
                     "why": "该部分三角面退回叉积几何法向，绕向不一致时法向可能反向",
                     "need": "源模型补法线或统一三角面绕向"})
    if not buildings:
        gaps.append({"code": "ENH-GAP-005", "what": "缺 09 逐栋属性",
                     "why": f"未找到 {ATTRS_REL.replace(os.sep, '/')}，全部子段建筑归属为 null",
                     "need": "先跑 09 生成逐栋属性"})
    if summary["roofAreaM2"] is None or summary["meanTiltDeg"] is None:
        gaps.append({"code": "ENH-GAP-006", "what": "部分 summary 为 null",
                     "why": "没有可用的屋面子段（面积或倾角算不出）",
                     "need": "检查 b3dm 是否含屋面三角面与法向"})
    gaps.append({"code": "ENH-GAP-007",
                 "what": "屋面候选先按 09 建筑 mask 过滤（1 m 栅格，close/open 3×3 近似）",
                 "why": "地形、树冠、棚顶的朝上面同样满足 nz ≥ 0.7；不过滤会把整个场景的朝上面"
                        "当成屋面（实测未过滤时面积远大于占地）",
                 "need": "09 落盘逐栋 label 栅格；或接语义分割区分真屋面与地形/植被"})
    gaps.append({"code": "ENH-GAP-008",
                 "what": f"只用 LOD 第 `{level}` 层，未做跨层去重",
                 "why": "3D Tiles 的金字塔每层都覆盖同一场景，把 44 个 b3dm 全加进来会重复计数"
                        "（实测面积约为单层的 5–6 倍）；细层可能只覆盖局部，按 mask 内屋面面积挑完整层",
                 "need": "按视锥/屏幕误差选层，或先做跨层几何去重再统计"})
    if COMMON_IMPL != "common":
        gaps.append({"code": "ENH-GAP-009",
                     "what": "本机未装 numpy，使用纯 stdlib 兜底实现（common_fallback）",
                     "why": "算法与 numpy 路径一致，但三角面逐面循环更慢；数值口径不变",
                     "need": "在工作区 Python 环境安装 numpy 后重跑可获得同一结果"})

    usable = bool(segments) and (summary["roofAreaM2"] or 0) > 0
    reasons = [] if usable else ["没有面积 ≥ 2 m² 的屋面候选子段"]

    items = [{
        "id": f"R-{i:04d}",
        "areaM2": round(s["areaM2"], 2),
        "tiltDeg": round(s["tiltDeg"], 2),
        "azimuthDeg": round(s["azimuthDeg"], 1),
        "rmsM": round(s["rmsM"], 3),
        "planeOffsetM": round(s["planeOffsetM"], 3),
        "normal": [round(v, 4) for v in s["normal"]],
        "centroidLocal": [round(v, 3) for v in s["centroid"]],
        "faceCount": s["faceCount"],
        "buildingId": s["buildingId"],
        "buildingMatch": s["matchHow"],
    } for i, s in enumerate(segments[:ITEM_LIMIT], 1)]

    markdown = render_md(summary, items, gaps, buildings, faces, threshold, level, level_rule)

    doc = emit(workspace, ALGO,
               inputs={"model": MODEL_REL.replace(os.sep, "/"),
                       "attrs": ATTRS_REL.replace(os.sep, "/"),
                       "grid": GRID_REL.replace(os.sep, "/"),
                       "recon": RECON_REL.replace(os.sep, "/"),
                       "origin": [ox, oy],
                       "b3dm": faces["files"],
                       "vertices": faces["vertices"],
                       "maskCells": mask_cells,
                       "commonImpl": COMMON_IMPL,
                       "normalSource": "vertex-normal-mean" if not faces["noNormalVertices"]
                       else "vertex-normal-mean + face-cross-product(兜底)"},
               params={"normalZMin": NZ_MIN, "azimuthBinDeg": AZ_BIN_DEG,
                       "tiltBinDeg": TILT_BIN_DEG, "planeTolM": PLANE_TOL_M,
                       "minSegmentAreaM2": MIN_SEG_AREA_M2, "flatTiltDeg": FLAT_TILT_DEG,
                       "buildingMatchMaxM": MAX_MATCH_M, "buildingThresholdM": threshold,
                       "lodLevel": level, "lodSelection": level_rule,
                       "buildingMask": "ndsm>阈值 & observed + close/open(3×3)（自实现）",
                       "method": f"法向半球量化（方位角15°+倾角5°）+ 平面偏移聚类（{PLANE_TOL_M} m）"},
               summary=summary, items=items, gaps=gaps, usable=usable, reasons=reasons,
               markdown=markdown)

    write_json(os.path.join(workspace, OUT_ROOT, "_patches", f"{SLUG}.json"), {
        "id": ALGO, "status": "ready", "step": "12",
        "impl": "自实现：b3dm 三角面顶点法向均值 + 法向半球量化 + 平面偏移聚类",
        "evidence": "Skills/12-屋顶与立面/scripts/roof_segment.py",
        "sourceName": "RANSAC 平面拟合一族方法（Fischler & Bolles 1981），本链用聚类实现，未用随机采样",
        "sourceLink": "",
        "license": "自研代码",
        "plain": "把屋面三角面按朝向与高度切成屋面子段，量出面积、倾角、平面度并归到 09 的栋",
    })
    return doc


def render_md(summary, items, gaps, buildings, faces, threshold, level, level_rule) -> str:
    lines = [
        "# 屋面子段（12 · roof.segment）", "",
        "## 方法", "",
        "- 逐个 b3dm 读内嵌 GLB 的 `POSITION / NORMAL / indices`，三角面法向取三顶点法向的面积加权均值"
        "（缺 `NORMAL` 时退回叉积几何法向）；",
        f"- 取 `nz ≥ {NZ_MIN}` 的屋面三角面，再用 09 建筑 mask（`ndsm>{threshold:.1f} & observed`"
        " + close/open 3×3）把地形/树冠朝上面挡在外面；",
        f"- 按「方位角 {AZ_BIN_DEG:.0f}° + 倾角 {TILT_BIN_DEG:.0f}°」量化分箱；",
        f"- 箱内用平面偏移 `d = n·centroid` 排序，相邻差值 > {PLANE_TOL_M} m 即切开；"
        f"面积 < {MIN_SEG_AREA_M2} m² 的子段不计入平面清单；",
        "- 每段统计面积、倾角、平面度 `rmsM`、质心；建筑归属按 09 逐栋 OBB 包含，"
        f"否则最近质心 ≤ {MAX_MATCH_M:.0f} m，仍无则 `null`。", "",
        "## 参数", "",
        f"- 读 `{faces['files']}` 个 b3dm（共 `{faces['tileTotal']}`；按面统计后只取 LOD `{level}` 层：{level_rule}；"
        f"失败 `{len(faces['failures'])}`）；顶点 `{faces['vertices']}`；"
        f"该层 nz 过滤后 `{summary['nzRoofTriangleCount']}` 个三角面，"
        f"再过建筑 mask 后参与聚类 `{summary['roofTriangleCount']}` 个（mask `{summary['maskCells']}` 格）；",
        f"- 后端：`{COMMON_IMPL}`；重建 `EPSG:4544`；坐标为「原点 + 局部」，质心列的是局部坐标（米）。", "",
        "## summary", "",
        f"- 屋面子段 `{summary['roofPlaneCount']}` 个；屋面面积 `{summary['roofAreaM2']}` m²"
        f"（mask 内 nz≥{NZ_MIN} 三角面实测总面积）；平均倾角 `{summary['meanTiltDeg']}`°；"
        f"平屋面（≤{FLAT_TILT_DEG:.0f}°）面积占比 `{summary['flatPlaneShare']}`",
        f"- 子段覆盖面积 `{summary['segmentedAreaM2']}` m²；面积 < {MIN_SEG_AREA_M2} m² 未计入子段的面片 "
        f"`{summary['subMinSegmentCount']}` 个（碎面来自网格噪声与檐口/天窗）；"
        f"归到 09 栋的子段 `{summary['buildingMatchedPlaneCount']}` 个，未归属 `{summary['unassignedPlaneCount']}` 个", "",
        "## 前 20 段", "",
        "| 段 | 面积 m² | 倾角 ° | 方位 ° | rms m | 面数 | 建筑 | 归属依据 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for it in items:
        lines.append(f"| {it['id']} | {it['areaM2']:.2f} | {it['tiltDeg']:.2f} | "
                     f"{it['azimuthDeg']:.1f} | {it['rmsM']:.3f} | {it['faceCount']} | "
                     f"{it['buildingId'] or '—'} | {it['buildingMatch'] or '—'} |")
    lines += ["", "## 局限", ""]
    for gap in gaps:
        lines.append(f"- `{gap['code']}` {gap['what']}：{gap['why']}（需：{gap['need']}）")
    lines += [
        "- 面积是三角面实际面积（3D），不是占地投影；共墙/贴边屋顶会跨栋。",
        "- 屋面含附属物时倾角与 `rmsM` 会被拉偏；`rmsM` 大说明该子段不那么「平」。", ""]
    if faces["failures"]:
        lines += ["## 解析失败的 b3dm", ""]
        for f in faces["failures"][:10]:
            lines.append(f"- `{f['path']}`：{f['error']}")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    workspace = _workspace_from_argv()
    if _done_ok(workspace):
        print("[12:roof.segment] 已完成（_done exitCode=0），跳过，不改任何文件")
        return 0
    if not os.path.isfile(os.path.join(workspace, MODEL_REL)):
        print(f"[12:roof.segment] 缺输入：{MODEL_REL}（先跑 03/05）", file=sys.stderr)
        return 2
    args = parse_args(ALGO, "12 · 屋面子段（法向量化 + 平面偏移聚类）")
    doc = build(args.workspace)
    return finish(doc)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
