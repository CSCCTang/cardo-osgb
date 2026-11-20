# -*- coding: utf-8 -*-
"""纹理/材质识别（P0）：从 b3dm 内嵌纹理估算绿化率与道路占比（带区间、置信度）。

算法（与《规则/纹理材质参数.json》一一对应）：
  1) 每个朝上三角面（几何法线 nz ≥ 0.5）取重心 x/y 与重心 UV；
  2) 按 UV 采贴图像素，得到 RGB 与 HSV；
  3) 可见光植被指数 ExG=2g-r-b / VARI / GLI，ExG 阈值由 Otsu 自动定（带上下限）；
  4) 绿 = ExG≥阈值 且 HSV 在绿相/饱和/亮度范围内；
  5) 道路 = 非绿 且 低饱和灰度 且 近地面 且 不在 2.5D 建筑掩膜内；
  6) 绿化率分母 = 模型覆盖范围内全部朝上表面纹理样本（含建筑屋面，按样本数加权）；
     道路占比同分母，分子只计非建筑、近地面样本；
  7) 区间 = 网格 0.5/1.0/2.0 m × ExG 阈值三档（× 道路饱和/高度三档）的 min–max；
  8) 置信度按纹理覆盖与每格样本数定级（low/medium，P0 不声称 high）。

只读标准容器（b3dm 头 / GLB / glTF JSON），不解析 OSGB 字节；不依赖外部影像或权重。
依赖：numpy + Pillow（必需），trimesh（可用时用于面法线，与 05 诊断一致），scikit-image 不需要。

用法（作为模块）：
    from texture_materials import analyze
    result = analyze(model_path, return_cells=False)
    # result["status"] == "ok" / "skipped"
"""
from __future__ import annotations

import io
import json
import math
import os
import sys
from datetime import datetime

sys.dont_write_bytecode = True

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PARAMS = os.path.join(os.path.dirname(HERE), "规则", "纹理材质参数.json")
NEG_INF = -1e30
EPS = 1e-6


# ---------------------------------------------------------------- 参数

def load_params(path=None):
    with open(path or DEFAULT_PARAMS, encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------- 容器解析

def _gltf_of_b3dm(path):
    """b3dm 头 → GLB → (glTF JSON, BIN 字节)；仅标准容器层，与 06 主脚本一致。"""
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


def _float_accessor(gltf, binary, index, width):
    """float32 VEC2/VEC3 → (N,width) ndarray；步长非紧凑时逐点 unpack。"""
    accessors, views = gltf.get("accessors") or [], gltf.get("bufferViews") or []
    if not isinstance(index, int) or not 0 <= index < len(accessors):
        return None
    acc = accessors[index]
    bvi = acc.get("bufferView")
    want = "VEC2" if width == 2 else "VEC3"
    if acc.get("componentType") != 5126 or acc.get("type") != want:
        return None
    if not isinstance(bvi, int) or not 0 <= bvi < len(views):
        return None
    count = int(acc.get("count") or 0)
    view = views[bvi]
    off = int(view.get("byteOffset", 0)) + int(acc.get("byteOffset", 0))
    stride = int(view.get("byteStride") or width * 4)
    if count <= 0 or off + (count - 1) * stride + width * 4 > len(binary):
        return None
    if stride == width * 4:
        return np.frombuffer(binary, dtype="<f4", count=count * width,
                             offset=off).reshape(-1, width)
    rows = [np.frombuffer(binary, dtype="<f4", count=width, offset=off + i * stride)
            for i in range(count)]
    return np.asarray(rows, dtype=np.float32)


def _index_array(gltf, binary, index):
    accessors, views = gltf.get("accessors") or [], gltf.get("bufferViews") or []
    if not isinstance(index, int) or not 0 <= index < len(accessors):
        return None
    acc = accessors[index]
    bvi = acc.get("bufferView")
    dtypes = {5121: "<u1", 5123: "<u2", 5125: "<u4"}
    dtype = dtypes.get(acc.get("componentType"))
    if not dtype or not isinstance(bvi, int) or not 0 <= bvi < len(views):
        return None
    count = int(acc.get("count") or 0)
    view = views[bvi]
    off = int(view.get("byteOffset", 0)) + int(acc.get("byteOffset", 0))
    size = np.dtype(dtype).itemsize
    stride = int(view.get("byteStride") or size)
    if count < 3 or off + (count - 1) * stride + size > len(binary):
        return None
    if stride == size:
        return np.frombuffer(binary, dtype=dtype, count=count, offset=off)
    rows = [np.frombuffer(binary, dtype=dtype, count=1, offset=off + i * stride)[0]
            for i in range(count)]
    return np.asarray(rows, dtype=dtype)


def _diffuse_texture(material):
    """材质漫反射贴图索引：KHR_techniques_webgl.u_diffuse 优先，其次 pbr baseColorTexture。"""
    if not isinstance(material, dict):
        return None
    technique = ((material.get("extensions") or {}).get("KHR_techniques_webgl") or {})
    for key in ("u_diffuse", "diffuse"):
        item = (technique.get("values") or {}).get(key)
        if isinstance(item, dict) and isinstance(item.get("index"), int):
            return item["index"]
    pbr = material.get("pbrMetallicRoughness") or {}
    base = pbr.get("baseColorTexture") or {}
    return base.get("index") if isinstance(base.get("index"), int) else None


def _decode_textures(gltf, binary, max_px):
    """解码 b3dm 内嵌贴图为 {textureIndex: {rgb, hsv, w, h}}；失败/外链贴图跳过。"""
    views = gltf.get("bufferViews") or []
    images = gltf.get("images") or []
    out = {}
    for tex_index, texture in enumerate(gltf.get("textures") or []):
        source = texture.get("source") if isinstance(texture.get("source"), int) else None
        if source is None or not 0 <= source < len(images):
            continue
        bv = images[source].get("bufferView")
        if not isinstance(bv, int) or not 0 <= bv < len(views):
            continue
        view = views[bv]
        off, length = int(view.get("byteOffset", 0)), int(view.get("byteLength", 0))
        if length <= 0:
            continue
        try:
            im = Image.open(io.BytesIO(binary[off:off + length]))
            im.draft("RGB", (max_px * 2, max_px * 2))
            im = im.convert("RGB")
            im.thumbnail((max_px, max_px))
            rgb = np.asarray(im, dtype=np.float32) / 255.0
            hsv = np.asarray(im.convert("HSV"), dtype=np.uint8)
        except (OSError, ValueError):
            continue
        out[tex_index] = {"rgb": rgb, "hsv": hsv, "w": int(hsv.shape[1]), "h": int(hsv.shape[0])}
    return out


# ---------------------------------------------------------------- 统计工具

def _otsu(values, lo=-1.0, hi=2.0, bins=256):
    """一维 Otsu 阈值；样本不足或全同值时返回 None。"""
    vals = np.asarray(values, dtype=np.float64)
    vals = vals[np.isfinite(vals)]
    if vals.size < 100:
        return None
    hist, edges = np.histogram(vals, bins=bins, range=(lo, hi))
    hist = hist.astype(np.float64)
    total = hist.sum()
    if total <= 0:
        return None
    prob = hist / total
    centers = (edges[:-1] + edges[1:]) / 2.0
    omega = np.cumsum(prob)
    mu = np.cumsum(prob * centers)
    mu_t = mu[-1]
    denom = np.maximum(omega * (1.0 - omega), 1e-12)
    sigma_b = (mu_t * omega - mu) ** 2 / denom
    return float((edges[int(np.nanargmax(sigma_b))] + edges[int(np.nanargmax(sigma_b)) + 1]) / 2.0)


def _local_ground(z_grid, nx, ny, cell, radius_m, q):
    """15 m 邻域内观测格 z 的 P10（与 06 主脚本同口径）；返回 {(ix,iy): ground_z}。"""
    step = max(1, int(round(radius_m / cell)))
    arr = np.full((nx, ny), np.nan, dtype=np.float64)
    for (ix, iy), z in z_grid.items():
        arr[ix, iy] = z
    out = {}
    for ix in range(nx):
        for iy in range(ny):
            if not np.isfinite(arr[ix, iy]):
                continue
            win = arr[max(0, ix - step):ix + step + 1, max(0, iy - step):iy + step + 1]
            vals = win[np.isfinite(win)]
            if vals.size:
                out[(ix, iy)] = float(np.percentile(vals, q))
    return out


def _ratio(counts, total):
    den = float(total.sum())
    return (float(counts.sum()) / den) if den > 0 else None


# ---------------------------------------------------------------- 主流程

def analyze(model_path, params_path=None, return_cells=False, cell_m=None):
    """对单个 cardo-lowair-model/v1 产物做纹理/材质识别。

    返回 dict：
      status ok / skipped
      greenery: {value, range, confidence, samples, coverage, medianSamplesPerCell}
      road: 同上
      params: 实际使用的阈值（写进 现状指标.json 便于追溯）
      diagnostics: 采样/解码/分布等诊断
      perCell: 仅 return_cells=True 时返回，供真值校核与正射图使用
    """
    if np is None or Image is None:
        return {"status": "skipped", "reason": "未安装 numpy/Pillow，无法做纹理采样"}
    params = load_params(params_path)
    with open(model_path, encoding="utf-8") as fh:
        model = json.load(fh)
    if model.get("schema") != "cardo/lowair-model/v1":
        return {"status": "skipped", "reason": f"model schema 非 cardo/lowair-model/v1：{model.get('schema')}"}
    bbox = model["geometry"]["bboxLocal"]
    x0, y0 = float(bbox["min"][0]), float(bbox["min"][1])
    x1, y1 = float(bbox["max"][0]), float(bbox["max"][1])
    root = os.path.dirname(os.path.abspath(model_path))
    tiles = [a["path"] for a in model.get("artifacts") or []
             if str(a.get("path", "")).lower().endswith(".b3dm")]
    if not tiles:
        return {"status": "skipped", "reason": "model.artifacts 中没有 b3dm"}

    samp = params["sampling"]
    max_px = int(samp["textureDecodeMaxPx"])
    normal_z = float(samp["normalZ"])
    base_cell = float(cell_m if cell_m is not None else samp["gridCellM"])
    cells = sorted(set(float(c) for c in samp["sensitivityCellM"]) | {base_cell})
    vege = params["vegetation"]
    road_p = params["road"]
    hsv_p = vege["hsv"]

    # 采样容器
    buf = {k: [] for k in ("x", "y", "z", "r", "g", "b", "hue", "sat", "val", "exg", "vari", "gli", "area")}
    failures = []
    decoded_tiles = tex_images = prim_used = prim_skipped = 0

    for rel in tiles:
        try:
            gltf, binary = _gltf_of_b3dm(os.path.join(root, rel))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            failures.append({"path": rel, "error": f"{type(exc).__name__}: {exc}"})
            continue
        textures = _decode_textures(gltf, binary, max_px)
        if textures:
            decoded_tiles += 1
            tex_images += len(textures)
        for mesh in gltf.get("meshes") or []:
            for prim in mesh.get("primitives") or []:
                if prim.get("mode") not in (None, 4):
                    prim_skipped += 1
                    continue
                attrs = prim.get("attributes") or {}
                pos = _float_accessor(gltf, binary, attrs.get("POSITION"), 3)
                uv = _float_accessor(gltf, binary, attrs.get("TEXCOORD_0"), 2)
                if pos is None or uv is None or len(pos) < 3:
                    prim_skipped += 1
                    continue
                faces = _index_array(gltf, binary, prim.get("indices"))
                if faces is None:
                    if len(pos) % 3:
                        prim_skipped += 1
                        continue
                    faces = np.arange(len(pos), dtype=np.uint32)
                tri = faces.reshape(-1, 3)
                if tri.size == 0 or int(tri.max()) >= len(pos):
                    prim_skipped += 1
                    continue
                material = (gltf.get("materials") or [None])[prim.get("material")] \
                    if isinstance(prim.get("material"), int) else None
                tex_index = _diffuse_texture(material)
                if tex_index not in textures:
                    prim_skipped += 1
                    continue
                p0, p1, p2 = pos[tri[:, 0]], pos[tri[:, 1]], pos[tri[:, 2]]
                cross_all = np.cross(p1 - p0, p2 - p0)
                tri_area_all = 0.5 * np.linalg.norm(cross_all, axis=1)
                try:
                    import trimesh
                    face_normals = trimesh.Trimesh(vertices=pos, faces=tri,
                                                   process=False).face_normals
                except Exception:
                    norm = np.linalg.norm(cross_all, axis=1, keepdims=True)
                    face_normals = cross_all / np.where(norm == 0, 1, norm)
                up = face_normals[:, 2] >= normal_z
                if not up.any():
                    continue
                centroid = (p0 + p1 + p2) / 3.0
                uv_c = (uv[tri[:, 0]] + uv[tri[:, 1]] + uv[tri[:, 2]]) / 3.0
                inside = ((centroid[:, 0] >= x0) & (centroid[:, 0] <= x1)
                          & (centroid[:, 1] >= y0) & (centroid[:, 1] <= y1))
                keep = up & inside
                if not keep.any():
                    continue
                cx, cy, cz = centroid[keep, 0], centroid[keep, 1], centroid[keep, 2]
                uv_k = uv_c[keep]
                tex = textures[tex_index]
                px = np.clip((np.mod(uv_k[:, 0], 1.0) * tex["w"]).astype(np.int64), 0, tex["w"] - 1)
                py = np.clip((np.mod(uv_k[:, 1], 1.0) * tex["h"]).astype(np.int64), 0, tex["h"] - 1)
                rgb = tex["rgb"][py, px]
                hsv = tex["hsv"][py, px]
                r, g, b = rgb[:, 0], rgb[:, 1], rgb[:, 2]
                exg = 2.0 * g - r - b
                vari = (g - r) / np.maximum(g + r - b, EPS)
                gli = (2.0 * g - r - b) / np.maximum(2.0 * g + r + b, EPS)
                buf["x"].append(cx.astype(np.float32))
                buf["y"].append(cy.astype(np.float32))
                buf["z"].append(cz.astype(np.float32))
                buf["r"].append(r.astype(np.float32))
                buf["g"].append(g.astype(np.float32))
                buf["b"].append(b.astype(np.float32))
                buf["hue"].append(hsv[:, 0].astype(np.float32))
                buf["sat"].append(hsv[:, 1].astype(np.float32))
                buf["val"].append(hsv[:, 2].astype(np.float32))
                buf["exg"].append(exg.astype(np.float32))
                buf["vari"].append(vari.astype(np.float32))
                buf["gli"].append(gli.astype(np.float32))
                buf["area"].append(tri_area_all[keep].astype(np.float32))
                prim_used += 1

    if not buf["x"]:
        return {"status": "skipped", "reason": "没有可采样的朝上三角面/贴图",
                "diagnostics": {"decodedTiles": decoded_tiles, "textureImages": tex_images,
                                "primitivesUsed": prim_used, "primitivesSkipped": prim_skipped,
                                "failures": failures[:5]}}
    a = {k: np.concatenate(v) for k, v in buf.items()}
    n_samples = int(a["x"].size)
    nx = max(1, int(math.ceil((x1 - x0) / base_cell)))
    ny = max(1, int(math.ceil((y1 - y0) / base_cell)))
    ix = np.clip(((a["x"] - x0) / base_cell).astype(np.int64), 0, nx - 1)
    iy = np.clip(((a["y"] - y0) / base_cell).astype(np.int64), 0, ny - 1)
    flat = ix * ny + iy

    # 1 m 网格 zmax → 邻域地面 → 建筑掩膜
    z_grid = {}
    for f, z in zip(flat, a["z"]):
        key = int(f)
        if key not in z_grid or z > z_grid[key]:
            z_grid[key] = float(z)
    ground = _local_ground({(int(k // ny), int(k % ny)): v for k, v in z_grid.items()},
                           nx, ny, base_cell, 15.0, 10.0)
    global_ground = float(np.percentile(a["z"], 10)) if n_samples else 0.0
    heights = np.empty(n_samples, dtype=np.float32)
    in_building = np.zeros(n_samples, dtype=bool)
    building_cells = set()
    for i, (f, z) in enumerate(zip(flat, a["z"])):
        gx, gy = int(f // ny), int(f % ny)
        gz = ground.get((gx, gy), global_ground)
        heights[i] = float(z) - gz
    for key, z in z_grid.items():
        gx, gy = int(key // ny), int(key % ny)
        if z - ground.get((gx, gy), global_ground) > 3.0:
            building_cells.add(key)
    for i, f in enumerate(flat):
        if int(f) in building_cells:
            in_building[i] = True

    # ExG 阈值：Otsu + 上下限；敏感性三档
    lo, hi = [float(v) for v in vege["exgThresholdClamp"]]
    otsu = _otsu(a["exg"], -1.0, 2.0)
    exg_thr = float(vege["exgThresholdFallback"]) if otsu is None else min(max(otsu, lo), hi)
    factors = [float(v) for v in vege["exgSensitivityFactors"]]
    exg_thrs = [min(max(exg_thr * f, 0.02), 0.25) for f in factors]
    hue_ok = ((a["hue"] >= hsv_p["hueRange"][0]) & (a["hue"] <= hsv_p["hueRange"][1])
              & (a["sat"] >= hsv_p["saturationMin"]) & (a["val"] >= hsv_p["valueMin"]))
    # 明度分层：暗部树冠 + 亮部低饱和冠芯（冠芯常是灰绿，饱和度不足 50，旧规则只抓到冠缘）
    dark = vege.get("darkVegetation") or {}
    dark_green = np.zeros(n_samples, dtype=bool)
    if dark.get("enabled"):
        dark_green = ((a["val"] < float(dark["valMax"]))
                      & (a["sat"] >= float(dark["saturationMin"]))
                      & (a["hue"] >= float(dark["hueRange"][0]))
                      & (a["hue"] <= float(dark["hueRange"][1]))
                      & (a["exg"] >= float(dark["exgMin"])))
    canopy = vege.get("canopy") or {}
    if canopy.get("enabled"):
        canopy_ratio = float(canopy.get("exgRatioOfThreshold", 0.5))
        canopy_green = [((a["sat"] >= float(canopy["saturationMin"]))
                         & (a["val"] >= float(canopy["valMin"]))
                         & (a["hue"] >= float(canopy["hueRange"][0]))
                         & (a["hue"] <= float(canopy["hueRange"][1]))
                         & (a["exg"] >= t * canopy_ratio)) for t in exg_thrs]
    else:
        canopy_green = [np.zeros(n_samples, dtype=bool) for _ in exg_thrs]
    greens = [(((a["exg"] >= t) & hue_ok) | dark_green | canopy_green[i])
              for i, t in enumerate(exg_thrs)]

    # 道路：非绿 + 低饱和灰度 + ExG 远低于阈值 + 近地面 + 不在建筑掩膜
    sat_maxs = [float(v) for v in road_p["saturationSensitivity"]]
    v_lo, v_hi = [float(v) for v in road_p["valueRange"]]
    height_maxs = [float(v) for v in road_p["heightSensitivityM"]]
    road_masks = {}
    for gi, green in enumerate(greens):
        base_gray = ((~green) & (a["sat"] < max(sat_maxs)) & (a["val"] >= v_lo)
                     & (a["val"] <= v_hi) & (a["exg"] < exg_thrs[gi] * float(road_p["exgRatioMaxOfThreshold"]))
                     & (~in_building))
        for sj, sat_max in enumerate(sat_maxs):
            gray = base_gray & (a["sat"] < sat_max)
            for hj, h_max in enumerate(height_maxs):
                road_masks[(gi, sj, hj)] = gray & (heights <= h_max)

    # 混合像元软化：格内按三角面面积加权（不是数面片），再对绿色做多数票补洞
    fill_cfg = vege.get("greenFill") or {}
    fill_enabled = bool(fill_cfg.get("enabled", True))
    fill_min_nb = int(fill_cfg.get("minNeighbors", 5))

    def cell_shares(cell, mask):
        w = max(1, int(math.ceil((x1 - x0) / cell)))
        h = max(1, int(math.ceil((y1 - y0) / cell)))
        cx = np.clip(((a["x"] - x0) / cell).astype(np.int64), 0, w - 1)
        cy = np.clip(((a["y"] - y0) / cell).astype(np.int64), 0, h - 1)
        idx = cx * h + cy
        weight = a["area"].astype(np.float64)
        total = np.bincount(idx, weights=weight, minlength=w * h).astype(np.float64)
        if mask is None:
            return total, w, h
        counts = np.bincount(idx, weights=weight * mask.astype(np.float64), minlength=w * h)
        share = np.divide(counts, total, out=np.zeros_like(total), where=total > 0)
        return share, w, h

    def fill_holes(share, w, h, total):
        """3×3 多数票补洞：非绿格若 ≥N 个邻居是绿，按 0.5 下限软填充（只补内部空洞）。"""
        if not fill_enabled or share.size == 0:
            return share
        grid = share.reshape(w, h)
        obs = total.reshape(w, h) > 0
        green = (grid >= 0.5) & obs
        nb = np.zeros_like(green, dtype=np.int16)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if dx == 0 and dy == 0:
                    continue
                shifted = np.zeros_like(green)
                xs0, xs1 = max(0, dx), w + min(0, dx)
                ys0, ys1 = max(0, dy), h + min(0, dy)
                xd0, xd1 = max(0, -dx), w + min(0, -dx)
                yd0, yd1 = max(0, -dy), h + min(0, -dy)
                shifted[xd0:xd1, yd0:yd1] = green[xs0:xs1, ys0:ys1]
                nb += shifted
        fill = obs & (~green) & (nb >= fill_min_nb)
        out = grid.copy()
        out[fill] = np.maximum(out[fill], 0.5)
        return out.reshape(-1)

    def global_ratio(share, total):
        den = float(total.sum())
        return float((share * total).sum()) / den if den > 0 else None

    totals = {c: cell_shares(c, None) for c in cells}
    green_vals = {}
    base_green_share = None
    for cell in cells:
        for gi, green in enumerate(greens):
            share, w, h = cell_shares(cell, green)
            share = fill_holes(share, w, h, totals[cell][0])
            green_vals[(cell, gi)] = global_ratio(share, totals[cell][0])
            if cell == base_cell and gi == 1:
                base_green_share = share
    road_vals = {}
    base_road_share = None
    for cell in cells:
        for gi in range(len(greens)):
            for sj in range(len(sat_maxs)):
                for hj in range(len(height_maxs)):
                    share, _, _ = cell_shares(cell, road_masks[(gi, sj, hj)])
                    road_vals[(cell, gi, sj, hj)] = global_ratio(share, totals[cell][0])
                    if cell == base_cell and gi == 1 and sj == 1 and hj == 1:
                        base_road_share = share

    def safe_vals(vals):
        return [v for v in vals if v is not None]

    g_all = safe_vals([green_vals[(c, gi)] for c in cells for gi in range(len(greens))])
    r_all = safe_vals([road_vals[(c, gi, sj, hj)] for c in cells
                       for gi in range(len(greens)) for sj in range(len(sat_maxs))
                       for hj in range(len(height_maxs))])
    g_main = green_vals[(base_cell, 1)]
    r_main = road_vals[(base_cell, 1, 1, 1)]
    if g_main is None or r_main is None:
        return {"status": "skipped", "reason": "聚合分母为空，无法形成占比"}

    # 采样覆盖与置信度
    sampled_cells = int(np.unique(flat).size)
    grid_cells = int(nx * ny)
    coverage = sampled_cells / grid_cells if grid_cells else 0.0
    per_cell_total = np.bincount(flat, minlength=nx * ny)
    per_cell_total = per_cell_total[per_cell_total > 0]
    median_samples = float(np.median(per_cell_total)) if per_cell_total.size else 0.0
    conf_rule = params["confidence"]
    confidence = "medium" if (coverage >= 0.60 and median_samples >= 8) else "low"

    # 诊断：全样本绿占比、非屋面绿占比、道路样本量（面积加权）
    area_all = float(a["area"].sum())
    green_all = float((greens[1] * a["area"]).sum()) / max(1e-9, area_all)
    ground_domain = ~(in_building & (~greens[1]))  # 仅诊断：排除非绿屋面，保留树冠/绿色屋面
    ground_area = float(a["area"][ground_domain].sum())
    non_roof_green = (float((greens[1] & ground_domain) @ a["area"]) / max(1e-9, ground_area))
    road_samples = int(road_masks[(1, 1, 1)].sum())
    building_sample_share = float(in_building.sum()) / n_samples
    canopy_samples = int(canopy_green[1].sum()) if canopy.get("enabled") else 0
    fill_cells = 0
    if base_green_share is not None:
        base_count = np.bincount(flat, minlength=nx * ny)
        raw_green = np.bincount(flat, weights=greens[1].astype(np.float64), minlength=nx * ny)
        fill_cells = int(((base_green_share >= 0.5) & (base_count > 0)
                          & (raw_green < 0.5 * base_count)).sum())

    # 正射图/逐格结果（仅按需）
    per_cell = None
    if return_cells:
        base_total, bw, bh = totals[base_cell]
        all_cell_total = np.bincount(flat, minlength=bw * bh)
        rr = np.bincount(flat, weights=a["r"].astype(np.float64), minlength=bw * bh)
        gg = np.bincount(flat, weights=a["g"].astype(np.float64), minlength=bw * bh)
        bb = np.bincount(flat, weights=a["b"].astype(np.float64), minlength=bw * bh)
        hh = np.bincount(flat, weights=heights.astype(np.float64), minlength=bw * bh)
        per_cell = []
        for cell_idx in np.nonzero(all_cell_total)[0]:
            n = int(all_cell_total[cell_idx])
            gx, gy = int(cell_idx // bh), int(cell_idx % bh)
            per_cell.append({
                "ix": gx, "iy": gy, "samples": n,
                "areaM2": round(float(base_total[cell_idx]), 2),
                "greenShare": round(float(base_green_share[cell_idx]), 4),
                "roadShare": round(float(base_road_share[cell_idx]), 4),
                "meanHeight": round(float(hh[cell_idx]) / n, 2),
                "building": cell_idx in building_cells,
                "rgb": [int(round(float(rr[cell_idx]) / n * 255)),
                        int(round(float(gg[cell_idx]) / n * 255)),
                        int(round(float(bb[cell_idx]) / n * 255))],
                "x": round(x0 + (gx + 0.5) * base_cell, 2),
                "y": round(y0 + (gy + 0.5) * base_cell, 2),
            })

    return {
        "status": "ok",
        "methodVersion": params["methodVersion"],
        "generatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "greenery": {
            "value": round(g_main, 6),
            "range": [round(min(g_all), 6), round(max(g_all), 6)],
            "confidence": confidence,
            "samples": int(n_samples),
            "coverage": round(coverage, 4),
            "medianSamplesPerCell": round(median_samples, 2),
            "denominator": "模型覆盖范围内全部朝上表面三角面（含建筑屋面），按三角面面积加权",
        },
        "road": {
            "value": round(r_main, 6),
            "range": [round(min(r_all), 6), round(max(r_all), 6)],
            "confidence": confidence,
            "samples": int(n_samples),
            "roadSamples": road_samples,
            "coverage": round(coverage, 4),
            "medianSamplesPerCell": round(median_samples, 2),
            "denominator": "同绿化率分母；分子仅计非建筑掩膜、近地面三角面（面积加权）",
        },
        "params": {
            "input": "b3dm 内嵌纹理 + TEXCOORD_0 + POSITION/NORMAL",
            "sampling": {
                "normalZ": normal_z,
                "textureDecodeMaxPx": max_px,
                "gridCellM": base_cell,
                "sensitivityCellM": cells,
                "greenPatchCleanup": bool(samp.get("greenPatchCleanup", False)),
            },
            "vegetation": {
                "indices": vege["indices"],
                "exgThreshold": round(exg_thr, 6),
                "exgThresholdMode": vege["exgThresholdMode"],
                "exgThresholdClamp": [lo, hi],
                "exgSensitivityThresholds": [round(t, 6) for t in exg_thrs],
                "hsv": {"hueRange": hsv_p["hueRange"], "hueRangeDegApprox": hsv_p["hueRangeDegApprox"],
                        "saturationMin": hsv_p["saturationMin"], "valueMin": hsv_p["valueMin"]},
                "darkVegetation": {
                    "enabled": bool(dark.get("enabled")),
                    "valMax": dark.get("valMax"),
                    "saturationMin": dark.get("saturationMin"),
                    "hueRange": dark.get("hueRange"),
                    "exgMin": dark.get("exgMin"),
                    "basis": dark.get("basis"),
                },
                "canopy": {
                    "enabled": bool(canopy.get("enabled")),
                    "valMin": canopy.get("valMin"),
                    "saturationMin": canopy.get("saturationMin"),
                    "hueRange": canopy.get("hueRange"),
                    "exgRatioOfThreshold": canopy.get("exgRatioOfThreshold"),
                    "basis": canopy.get("basis"),
                },
                "greenFill": {
                    "enabled": fill_enabled,
                    "minNeighbors": fill_min_nb,
                    "basis": fill_cfg.get("basis"),
                },
            },
            "road": {
                "saturationSensitivity": sat_maxs,
                "valueRange": [v_lo, v_hi],
                "exgRatioMaxOfThreshold": float(road_p["exgRatioMaxOfThreshold"]),
                "heightSensitivityM": height_maxs,
            },
            "buildingMask": {"definition": params["buildingMask"]["definition"],
                             "thresholdM": 3.0, "heightAboveGroundMaxM": height_maxs[1]},
            "denominator": params["denominator"],
            "confidenceRule": {"medium": conf_rule["medium"], "low": conf_rule["low"]},
        },
        "diagnostics": {
            "samples": n_samples,
            "decodedTiles": decoded_tiles,
            "textureImages": tex_images,
            "primitivesUsed": prim_used,
            "primitivesSkipped": prim_skipped,
            "failures": failures[:5],
            "gridCells": grid_cells,
            "sampledCells": sampled_cells,
            "coverage": round(coverage, 4),
            "medianSamplesPerCell": round(median_samples, 2),
            "exgPercentiles": {"p10": round(float(np.percentile(a["exg"], 10)), 4),
                               "p50": round(float(np.percentile(a["exg"], 50)), 4),
                               "p90": round(float(np.percentile(a["exg"], 90)), 4)},
            "variMean": round(float(np.mean(a["vari"])), 4),
            "gliMean": round(float(np.mean(a["gli"])), 4),
            "greenShareAll": round(green_all, 4),
            "darkVegetationSamples": int(dark_green.sum()),
            "canopyVegetationSamples": canopy_samples,
            "greenFillCells": fill_cells,
            "nonRoofGreenShare": round(non_roof_green, 4),
            "roadSamples": road_samples,
            "buildingSampleShare": round(building_sample_share, 4),
            "buildingCells": len(building_cells),
            "heightP50": round(float(np.median(heights)), 2),
            "note": ("绿化率/道路占比是纹理覆盖代理值，不是平面用地面积；绿色屋面、绿色围挡、阴影、"
                     "树冠遮挡、贴图拉伸都会造成误判。P0 未做语义分割，置信度不声称 high。"),
        },
        "perCell": per_cell,
    }
