#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""05 · 模型体检：H-01…H-06 + `cardo-lowair-model/v1` 契约 + 人读体检明细。

输入（默认，均相对工作区根）：
    过程文件/01-模型包接入/输入清单.json      01 的解包清单（来源/SRS/文件数）
    过程文件/02-工具探测/工具探测.json        02 的白名单校验结果（H-06 依据）
    过程文件/03-OSGB转3DTiles/转换日志.json   03 的视图/LOD/产物统计（H-01/H-03 依据）
    过程文件/03-OSGB转3DTiles/转换产物/       转换结果（本步复扫，不信日志自报）
    过程文件/04-垂直配准修正/配准修正.json    04 的修正量与前后平移（H-05 依据）
输出：
    <转换产物>/cardo-lowair-model.json   契约（artifacts 相对本目录，供 06/08 消费）
    过程文件/05-模型体检/体检.json        cardo/lowair-health/v1（六项 + 一致性 + 契约自检）
    过程文件/05-模型体检/契约自检.json     check_contract 的逐项结果
    过程文件/05-模型体检/模型体检.md      人读明细（模板 模板/模型体检.md）

纪律：产物先删旧再生成；六项判据逐条 pass/warn/fail 并给 verdict；契约或一致性不过即 exit 3。
本步不重写 tileset/b3dm（04 已改过），只读复扫 —— 体检不制造产物。
退出码：0 pass/warn；2 输入缺失；3 fail（H 项 fail / 契约自检不过 / 一致性不过）。
"""
from __future__ import annotations

import argparse
import io
import json
import math
import os
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.dont_write_bytecode = True
sys.path.insert(0, HERE)
import check_contract  # noqa: E402
import tiles_inspect  # noqa: E402

SCHEMA = "cardo/lowair-model/v1"
HEALTH_SCHEMA = "cardo/lowair-health/v1"
PRODUCER, PRODUCER_VERSION = "cardo-osgb", "0.1.0"
MODEL_NAME = "cardo-lowair-model.json"
NEG_INF = -1e30
MIN_GREEN_PATCH_PX = 8       # 绿色斑点最小面积（128×128 缩略图像素），小于即视为噪点
SKILL_LOCAL_TOOLS = os.path.join("Skills", "02-工具探测", "tools")
DEFAULTS = {
    "input": os.path.join("过程文件", "01-模型包接入", "输入清单.json"),
    "tools": os.path.join("过程文件", "02-工具探测", "工具探测.json"),
    "convert": os.path.join("过程文件", "03-OSGB转3DTiles", "转换日志.json"),
    "products": os.path.join("过程文件", "03-OSGB转3DTiles", "转换产物"),
    "registration": os.path.join("过程文件", "04-垂直配准修正", "配准修正.json"),
    "out": os.path.join("过程文件", "05-模型体检"),
}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def read_json(path: str) -> dict:
    with open(path, encoding="utf-8-sig") as fh:
        return json.load(fh)


def write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def rel(path: str) -> str:
    """工作区内的相对路径（正斜杠）；不落盘符绝对路径。"""
    return os.path.relpath(os.path.abspath(path), os.path.abspath(".")).replace("\\", "/")


def resolve_tools_dir(cli: str | None) -> str | None:
    """--tools-dir > Skills/02-工具探测/tools > 环境变量（与 02/03 同序）。"""
    if cli:
        return os.path.abspath(cli)
    local = os.path.abspath(SKILL_LOCAL_TOOLS)
    if os.path.isdir(local):
        return local
    env = os.environ.get("CARDO_OSGB_TOOLS")
    return os.path.abspath(env) if env and os.path.isdir(env) else None


def gap(code: str, what: str, why: str, need: str, owner: str = "parser") -> dict:
    return {"code": code, "what": what, "why": why, "need": need, "owner": owner}


def build_gaps(manifest: dict, view: dict, scan: dict, health: dict, probe: dict) -> list:
    """只登记可解释、可行动的缺项；码表见 规则/体检判据.md（9 个登记码）。"""
    gaps, meta = [], manifest.get("metadata") or {}
    if meta.get("srsOriginal") and meta.get("srs") and meta["srsOriginal"] != meta["srs"]:
        gaps.append(gap(
            "SRS_COMPOSITE_NORMALIZED",
            f"metadata SRS 为复合码 {meta['srsOriginal']}，已归一为水平 {meta['srs']}",
            "工具只按水平单码建 tileset；垂直码若混入会被误当水平基准",
            f"垂直分量 {meta.get('vertical') or '未声明'} 已记入 crs.vertical，下游按需引用",
            "parser"))
    unconverted = view.get("osgb", 0) - scan["b3dmDisk"]
    if unconverted > 0:
        tops = ", ".join(f"{t['name']}({t['bytes']} B)" for t in view.get("topLevel") or [])
        gaps.append(gap(
            "TILE_NOT_CONVERTED",
            f"{unconverted} 个 osgb 未单独产出 b3dm（顶层：{tops or '未记录'}）",
            "顶层根瓦片只作块索引/低面数占位，3dtile 把其几何并入子块",
            "若需逐文件映射，按 输入清单.json 的 files[] 与 转换日志.json 的 view 对账后单独核对",
            "upstream"))
    if scan["missing"] or scan["failed"]:
        gaps.append(gap(
            "ARTIFACT_INCOMPLETE",
            f"tileset 引用缺失 {len(scan['missing'])} 条 / b3dm 解析失败 {len(scan['failed'])} 个",
            "产物不完整或有截断，引用链断开会造成加载缺块",
            "重跑 03（先删旧产物）并比对 转换日志.json 的 stdout/stderr",
            "parser"))
    if scan["images"] and scan["missingImages"]:
        gaps.append(gap(
            "ARTIFACT_INCOMPLETE",
            f"纹理缺失 {scan['missingImages']} 张（共 {scan['images']} 张）",
            "b3dm 内 image 无 bufferView 也无 uri，贴图会丢失",
            "检查原始 OSGB 的 texture 目录是否完整后重跑 03",
            "upstream"))
    if not probe.get("ok", False):
        gaps.append(gap(
            "TOOL_MISSING",
            "02 工具探测未通过（工具缺失/指纹不符）",
            probe.get("reason") or "白名单校验未通过，产物可信度不足",
            "按 规则/外部工具配置.yaml 恢复 tools/ 或运行 fetch_tools.py",
            "user"))
    for check in health["checks"]:
        if check["status"] != "fail":
            continue
        code = "TOOL_VERSION_MISMATCH" if check["id"] == "H-06" else "ARTIFACT_INCOMPLETE"
        gaps.append(gap(code, f"{check['id']} {check['name']} 未通过", check["detail"],
                        "按体检判据定位并重跑对应技能", "parser"))
    return gaps


def consistency(manifest: dict, view: dict, scan: dict, fix: dict, artifacts: list,
                convert: dict, probe: dict) -> list:
    """05 的附加自洽项（不进 H-01…H-06，但同样硬闸）。"""
    checks = []

    def add(cid, name, ok, detail):
        checks.append({"id": cid, "name": name, "status": "pass" if ok else "fail", "detail": detail})

    blocks = sum((view.get("blocks") or {}).values())
    add("C-01", "b3dm 数 = 输入块内 osgb 数", scan["b3dmDisk"] == blocks,
        f"块内 osgb {blocks} / 产物 b3dm {scan['b3dmDisk']}")
    origin_z = (manifest.get("metadata") or {}).get("srsOrigin") or [0, 0, 0]
    tol = float(fix.get("toleranceM") or 0.01)
    diff = abs(abs(float(fix.get("appliedM") or 0.0)) - abs(float(origin_z[2])))
    add("C-02", "配准修正量 = |SRSOrigin.z|（≤ 容差）", diff <= tol,
        f"修正 {abs(float(fix.get('appliedM') or 0.0)):.6f} m / SRSOrigin.z "
        f"{abs(float(origin_z[2])):.6f} m / 差 {diff:.2e} m（容差 {tol:g} m）")
    add("C-03", "配准修正量可复算", bool(fix.get("recomputeOk")),
        f"state={fix.get('state')}，before→after 与 up×z 一致：{fix.get('recomputeOk')}")
    declared = (convert.get("products") or {}).get("bytesOnDisk")
    actual = sum(a["bytes"] for a in artifacts)
    declared_b3dm = (convert.get("products") or {}).get("b3dmCount")
    drift = None if declared is None else actual - int(declared)
    add("C-04", "产物规模与 03 日志一致",
        scan["b3dmDisk"] == declared_b3dm and drift is not None and abs(drift) <= 4096,
        f"b3dm {scan['b3dmDisk']} / 03 声明 {declared_b3dm}；字节 03 声明 {declared} / 现算 {actual}"
        f"（差 {drift} B：04 会重写 root tileset.json，允许 ≤4096 B 格式化差异；b3dm 未被改动）")
    expected_sha = next((t.get("sha256") for t in probe.get("tools") or []
                         if t.get("name") == "3dtile"), None)
    add("C-05", "转换所用 exe 与 02 白名单同源", bool(expected_sha),
        f"3dtile sha256 {expected_sha or '未登记'}")
    return checks


def _pct(values: list, p: float):
    if not values:
        return None
    ordered = sorted(values)
    k = int(round((len(ordered) - 1) * p / 100.0))
    return ordered[max(0, min(len(ordered) - 1, k))]


def texture_color_stats(root: str, artifacts: list, sample_px: int = 64) -> tuple:
    """解码 b3dm 内嵌纹理缩略图，统计颜色占比（**仅诊断**，不映射平面面积、不进 7 条指标）。

    返回 (summary, per_image)。缺 Pillow/numpy 时返回 skipped（不阻断体检）。
    """
    try:
        import numpy as np
        from PIL import Image
    except ImportError as exc:   # 依赖未装：降级为 skipped，不静默造数
        return {"status": "skipped", "reason": f"未安装 Pillow/numpy：{exc}"}, []

    rows, failures = [], []
    hue_hist = np.zeros(12, dtype="float64")
    for item in artifacts:
        if not str(item.get("path", "")).lower().endswith(".b3dm"):
            continue
        try:
            _feature, gltf, binary = tiles_inspect.glb_of(os.path.join(root, item["path"]))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            failures.append({"path": item["path"], "error": f"{type(exc).__name__}: {exc}"})
            continue
        views = gltf.get("bufferViews") or []
        for image in gltf.get("images") or []:
            bv = image.get("bufferView")
            if not isinstance(bv, int) or not 0 <= bv < len(views):
                continue
            view = views[bv]
            off, length = int(view.get("byteOffset", 0)), int(view.get("byteLength", 0))
            if length <= 0:
                continue
            try:
                im = Image.open(io.BytesIO(binary[off:off + length]))
                im.draft("RGB", (sample_px * 4, sample_px * 4))   # JPEG 按 DCT 缩略解，省时
                im = im.convert("RGB")
                im.thumbnail((sample_px, sample_px))
                hsv = np.asarray(im.convert("HSV"), dtype="uint8")
            except (OSError, ValueError) as exc:
                failures.append({"path": item["path"], "error": f"decode: {type(exc).__name__}: {exc}"})
                continue
            hue = hsv[..., 0].astype("float64")
            sat = hsv[..., 1].astype("float64")
            val = hsv[..., 2].astype("float64")
            green = (hue >= 40) & (hue <= 130) & (sat >= 50) & (val >= 30)   # 绿色植被近似
            gray = (sat < 40) & (val >= 35) & (val <= 215)                   # 低饱和灰（道路/屋面近似）
            other = ~(green | gray)
            chromatic = sat >= 25
            if chromatic.any():
                idx = np.clip((hue[chromatic] / 256.0 * 12).astype(int), 0, 11)
                hue_hist += np.bincount(idx, minlength=12)
            rows.append({
                "tile": item["path"], "bytes": length, "width": im.width, "height": im.height,
                "greenShare": round(float(green.mean()), 4),
                "grayShare": round(float(gray.mean()), 4),
                "otherShare": round(float(other.mean()), 4),
                "meanValue": round(float(val.mean()), 1),
            })
    if not rows:
        return {"status": "skipped", "reason": "没有可解码的内嵌纹理", "failures": failures[:5]}, rows

    total_bytes = sum(row["bytes"] for row in rows) or 1
    total_pixels = sum(row["width"] * row["height"] for row in rows)
    green = [row["greenShare"] for row in rows]
    gray = [row["grayShare"] for row in rows]
    hist_total = float(hue_hist.sum()) or 1.0
    summary = {
        "status": "ok", "usage": "diagnostic-only", "notValidated": True,
        "samplePx": sample_px, "images": len(rows), "sampledPixels": total_pixels,
        "greenShare": {"p10": _pct(green, 10), "p50": _pct(green, 50), "p90": _pct(green, 90),
                       "byteWeighted": round(sum(row["greenShare"] * row["bytes"] for row in rows)
                                             / total_bytes, 4)},
        "grayShare": {"p10": _pct(gray, 10), "p50": _pct(gray, 50), "p90": _pct(gray, 90),
                      "byteWeighted": round(sum(row["grayShare"] * row["bytes"] for row in rows)
                                            / total_bytes, 4)},
        "hueHistogram": [{"fromDeg": i * 30, "toDeg": (i + 1) * 30,
                          "share": round(float(count) / hist_total, 4)}
                         for i, count in enumerate(hue_hist)],
        "failures": failures[:5],
        "note": ("仅统计纹理像素颜色占比（绿/灰/其它），**不映射到平面面积**（缺 UV 栅格化与标注），"
                 "未进入 greenery_ratio / road_ratio；用于判断“纹理语义是否值得做”的可行性证据。"),
    }
    return summary, rows


def _float_accessor(gltf, binary, index, width):
    """float32 VEC2/VEC3 → (N,width) ndarray；步长非紧凑时逐点 unpack。"""
    import numpy as np
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
    import numpy as np
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
    """取材质的漫反射贴图索引：优先 KHR_techniques_webgl.u_diffuse，其次 pbr baseColorTexture。"""
    if not isinstance(material, dict):
        return None
    technique = ((material.get("extensions") or {}).get("KHR_techniques_webgl") or {})
    values = technique.get("values") or {}
    for key in ("u_diffuse", "diffuse"):
        item = values.get(key)
        if isinstance(item, dict) and isinstance(item.get("index"), int):
            return item["index"]
    pbr = material.get("pbrMetallicRoughness") or {}
    base = pbr.get("baseColorTexture") or {}
    return base.get("index") if isinstance(base.get("index"), int) else None


def texture_cell_color(root: str, artifacts: list, bbox: dict, cell: float = 1.0,
                       sample_px: int = 128, normal_z: float = 0.5) -> tuple:
    """按 UV 采样纹理颜色并投到 1.0 m 网格（**仅诊断**，不是平面面积占比）。

    做法：每个**朝上三角面**（几何法线 nz≥阈值）取重心 x/y 投格、重心 UV 取贴图像素；
    逐格累计 绿/灰/其它 样本数。依赖：numpy + Pillow（trimesh 可用时用于面法线）。
    返回 (summary, per_cell)。
    """
    try:
        import numpy as np
        from PIL import Image
    except ImportError as exc:
        return {"status": "skipped", "reason": f"未安装 numpy/Pillow：{exc}"}, []
    try:
        import trimesh
    except ImportError:
        trimesh = None
    try:
        from skimage import measure, morphology   # 斑点清洗：闭运算 + 连通域面积过滤
    except ImportError:
        measure = morphology = None

    x0, y0 = bbox["min"][0], bbox["min"][1]
    nx = max(1, int(math.ceil((bbox["max"][0] - x0) / cell)))
    ny = max(1, int(math.ceil((bbox["max"][1] - y0) / cell)))
    greens = np.zeros((nx, ny), dtype=np.int32)
    grays = np.zeros((nx, ny), dtype=np.int32)
    others = np.zeros((nx, ny), dtype=np.int32)
    zmax = np.full((nx, ny), NEG_INF, dtype=np.float64)
    decoded_tiles = prim_used = prim_skipped = total_patches = 0
    failures = []
    for item in artifacts:
        if not str(item.get("path", "")).lower().endswith(".b3dm"):
            continue
        try:
            _feature, gltf, binary = tiles_inspect.glb_of(os.path.join(root, item["path"]))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            failures.append({"path": item["path"], "error": f"{type(exc).__name__}: {exc}"})
            continue
        views = gltf.get("bufferViews") or []
        textures = {}
        for tex_index, texture in enumerate(gltf.get("textures") or []):
            source = texture.get("source") if isinstance(texture.get("source"), int) else None
            images = gltf.get("images") or []
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
                im.draft("RGB", (sample_px * 2, sample_px * 2))
                im = im.convert("RGB")
                im.thumbnail((sample_px, sample_px))
                hsv = np.asarray(im.convert("HSV"), dtype="uint8")
            except (OSError, ValueError):
                continue
            hue = hsv[..., 0].astype("float64")
            sat = hsv[..., 1].astype("float64")
            val = hsv[..., 2].astype("float64")
            green_mask = (hue >= 40) & (hue <= 130) & (sat >= 50) & (val >= 30)
            gray_mask = (sat < 40) & (val >= 35) & (val <= 215)
            patches = None
            if morphology is not None:
                green_mask = morphology.closing(green_mask, morphology.disk(1))
                labels = measure.label(green_mask.astype("uint8"), connectivity=1)
                if labels.max() > 0:
                    areas = np.bincount(labels.ravel())
                    keep_ids = np.nonzero(areas >= MIN_GREEN_PATCH_PX)[0]
                    green_mask = np.isin(labels, keep_ids) & (labels > 0)
                    patches = int(sum(1 for label_id in keep_ids if label_id != 0))
                else:
                    patches = 0
                total_patches += patches or 0
            textures[tex_index] = {
                "green": green_mask, "gray": gray_mask, "patches": patches,
                "w": int(hsv.shape[1]), "h": int(hsv.shape[0]),
            }
        if textures:
            decoded_tiles += 1
        for mesh in gltf.get("meshes") or []:
            for prim in mesh.get("primitives") or []:
                attrs = prim.get("attributes") or {}
                pos = _float_accessor(gltf, binary, attrs.get("POSITION"), 3)
                uv = _float_accessor(gltf, binary, attrs.get("TEXCOORD_0"), 2)
                faces = _index_array(gltf, binary, prim.get("indices"))
                material = (gltf.get("materials") or [None])[prim.get("material")] \
                    if isinstance(prim.get("material"), int) else None
                tex_index = _diffuse_texture(material)
                if pos is None or uv is None or faces is None or tex_index not in textures:
                    prim_skipped += 1
                    continue
                tri = faces.reshape(-1, 3)
                p0, p1, p2 = pos[tri[:, 0]], pos[tri[:, 1]], pos[tri[:, 2]]
                if trimesh is not None:
                    face_normals = trimesh.Trimesh(vertices=pos, faces=tri,
                                                   process=False).face_normals
                else:
                    cross = np.cross(p1 - p0, p2 - p0)
                    norm = np.linalg.norm(cross, axis=1, keepdims=True)
                    face_normals = cross / np.where(norm == 0, 1, norm)
                up = face_normals[:, 2] >= normal_z
                if not up.any():
                    continue
                centroid = (p0 + p1 + p2) / 3.0
                uv_c = (uv[tri[:, 0]] + uv[tri[:, 1]] + uv[tri[:, 2]]) / 3.0
                inside = ((centroid[:, 0] >= x0) & (centroid[:, 0] <= bbox["max"][0])
                          & (centroid[:, 1] >= y0) & (centroid[:, 1] <= bbox["max"][1]))
                keep = up & inside
                if not keep.any():
                    continue
                cx, cy, cz = centroid[keep, 0], centroid[keep, 1], centroid[keep, 2]
                uv_k = uv_c[keep]
                tex = textures[tex_index]
                px = np.clip((np.mod(uv_k[:, 0], 1.0) * tex["w"]).astype(np.int64), 0, tex["w"] - 1)
                py = np.clip((np.mod(uv_k[:, 1], 1.0) * tex["h"]).astype(np.int64), 0, tex["h"] - 1)
                green = tex["green"][py, px]
                gray = tex["gray"][py, px]
                other = ~(green | gray)
                ix = np.clip(((cx - x0) / cell).astype(np.int64), 0, nx - 1)
                iy = np.clip(((cy - y0) / cell).astype(np.int64), 0, ny - 1)
                np.add.at(greens, (ix, iy), green.astype(np.int32))
                np.add.at(grays, (ix, iy), (~green & gray).astype(np.int32))
                np.add.at(others, (ix, iy), other.astype(np.int32))
                np.maximum.at(zmax, (ix, iy), cz)
                prim_used += 1
    total = greens + grays + others
    sampled = total > 0
    if not sampled.any():
        return {"status": "skipped", "reason": "没有可采样的朝上三角面/贴图",
                "failures": failures[:5]}, []
    total_samples = int(total[sampled].sum())
    green_share = float(greens[sampled].sum()) / total_samples
    gray_share = float(grays[sampled].sum()) / total_samples
    other_share = float(others[sampled].sum()) / total_samples
    per_cell_green = (greens[sampled] / total[sampled])
    summary = {
        "status": "ok", "usage": "diagnostic-only", "notValidated": True,
        "gridCellM": cell, "samplePx": sample_px, "normalZ": normal_z,
        "cellsWithSamples": int(sampled.sum()), "gridCells": int(nx * ny),
        "samples": total_samples, "decodedTiles": decoded_tiles,
        "primitivesUsed": prim_used, "primitivesSkipped": prim_skipped,
        "greenPatchCleanup": {"engine": "scikit-image" if morphology is not None else "none",
                              "minPatchPx": MIN_GREEN_PATCH_PX,
                              "greenPatches": total_patches},
        "greenShare": round(green_share, 4), "grayShare": round(gray_share, 4),
        "otherShare": round(other_share, 4),
        "greenSharePerCell": {"p10": round(float(np.percentile(per_cell_green, 10)), 4),
                              "p50": round(float(np.percentile(per_cell_green, 50)), 4),
                              "p90": round(float(np.percentile(per_cell_green, 90)), 4)},
        "areaProxyM2": round(float(sampled.sum()) * cell * cell, 2),
        "failures": failures[:5],
        "note": ("按**朝上三角面重心**的 UV 采纹理颜色并投到网格：是“纹理覆盖面积”的代理，"
                 "分母是有样本的格，**不是**平面用地面积；未做超像素/语义分割，未进 7 条指标。"),
    }
    per_cell = []
    for ix, iy in zip(*np.nonzero(sampled)):
        n = int(total[ix, iy])
        per_cell.append({
            "ix": int(ix), "iy": int(iy), "samples": n,
            "greenShare": round(float(greens[ix, iy]) / n, 4),
            "grayShare": round(float(grays[ix, iy]) / n, 4),
            "zMax": round(float(zmax[ix, iy]), 2),
        })
    return summary, per_cell


def build_quality(scan: dict) -> dict:
    """几何/纹理质量与查看器兼容性（比 H-01…H-06 更进一步，但不改六项判据）。"""
    prims = int(scan.get("primitives") or 0)
    triangles = int(scan.get("triangles") or 0)
    degenerate = int(scan.get("degenerateTriangles") or 0)
    ratio = (degenerate / triangles) if triangles else 0.0
    normals = (scan.get("normalsPrimitives") or 0) / prims if prims else 0.0
    uvs = (scan.get("uvPrimitives") or 0) / prims if prims else 0.0
    sizes = scan.get("textureSizes") or []
    pixels = sorted(w * h for w, h in sizes)
    resolution = {
        "count": len(sizes),
        "unique": len({(w, h) for w, h in sizes}),
        "min": [min(w for w, _ in sizes), min(h for _, h in sizes)] if sizes else None,
        "max": [max(w for w, _ in sizes), max(h for _, h in sizes)] if sizes else None,
        "medianPixels": pixels[len(pixels) // 2] if pixels else None,
        "unparsed": max(0, int(scan.get("images") or 0) - len(sizes)),
    }
    extensions = scan.get("extensionsUsed") or []
    overlap = scan.get("tileOverlap") or {}
    notes, status = [], "pass"
    if ratio > 0.3:
        status = "fail"
        notes.append(f"退化三角形占比 {ratio:.1%}（>30%，几何质量不可用）")
    elif ratio > 0.1:
        status = "warn"
        notes.append(f"退化三角形占比 {ratio:.1%}（>10%，建议重转或清理）")
    if scan.get("missingImages"):
        status = "fail"
        notes.append(f"纹理缺失 {scan['missingImages']} 张")
    if prims and normals < 0.6:
        status = status if status == "fail" else "warn"
        notes.append(f"法线覆盖 {normals:.0%}（<60%）：指标只能退回全顶点口径")
    if prims and uvs < 0.6:
        status = status if status == "fail" else "warn"
        notes.append(f"UV 覆盖 {uvs:.0%}（<60%）：无法做纹理采样/语义分类")
    if not sizes:
        status = status if status == "fail" else "warn"
        notes.append("纹理分辨率未解析（非常见 PNG/JPEG 或外链）")
    if "KHR_techniques_webgl" in extensions:
        notes.append("依赖 legacy 扩展 KHR_techniques_webgl：Cesium 可读，部分现代查看器不支持")
    if overlap.get("overlapPairs"):
        notes.append(f"LOD 瓦片 x/y 重叠 {overlap['overlapPairs']} 对（IoU≥0.5），"
                     "网格取 max z 会混合粗/精模")
    lod_stats = scan.get("lodStats") or {}
    if lod_stats:
        shares = "，".join(f"{key} {item['triangleShare']:.1%}" for key, item in lod_stats.items())
        notes.append(f"LOD 面占比：{shares}")
    return {
        "status": status, "primitiveCount": prims, "normalsCoverage": round(normals, 4),
        "uvCoverage": round(uvs, 4), "degenerateTriangles": degenerate,
        "degenerateRatio": round(ratio, 6), "textureResolution": resolution,
        "imageMimes": scan.get("imageMimes") or {}, "extensionsUsed": extensions,
        "rtcTiles": scan.get("rtcTiles"), "batchTiles": scan.get("batchTiles"),
        "legacyTechniquesWebgl": "KHR_techniques_webgl" in extensions,
        "lodStats": lod_stats, "tileOverlap": overlap, "notes": notes,
    }


def build_model(manifest: dict, convert: dict, fix: dict, probe: dict, scan: dict,
                artifacts: list, health: dict, validation: dict, gaps: list,
                quality: dict) -> dict:
    meta = manifest.get("metadata") or {}
    origin = [float(v) for v in (meta.get("srsOrigin") or [0.0, 0.0, 0.0])]
    zip_info = (manifest.get("source") or {}).get("zip") or {}
    name = os.path.splitext(str(zip_info.get("name") or ""))[0] or manifest["scene"]["sceneId"]
    products = convert.get("products") or {}
    display = fix.get("display") or {}
    up = display.get("up") or fix.get("up") or [0.0, 0.0, 0.0]
    applied_m = round(float(fix.get("appliedM") or 0.0), 3)
    delta_applied = [round(applied_m * float(u), 3) for u in up]
    toolchain = [{
        "name": t.get("name"), "role": t.get("role"), "version": t.get("version"),
        "sha256": t.get("sha256"), "license": t.get("license"),
        "licenseFile": t.get("licenseFile"), "distribution": t.get("distribution"),
        "path": t.get("path"), "versionBanner": t.get("versionBanner"),
    } for t in probe.get("tools") or [] if t.get("name")]

    return {
        "schema": SCHEMA, "schemaVersion": 1, "generatedAt": now_iso(),
        "producer": {"name": PRODUCER, "version": PRODUCER_VERSION},
        "source": {
            "name": name, "kind": (manifest.get("source") or {}).get("kind"),
            "zip": zip_info, "sceneId": manifest["scene"]["sceneId"],
            "sceneDir": manifest["scene"]["dir"],
            "files": int((manifest.get("counts") or {}).get("files") or 0),
            "bytes": int((manifest.get("counts") or {}).get("bytes") or 0),
            "osgbFiles": int(convert.get("view", {}).get("osgb") or 0),
            "manifest": rel(DEFAULTS["input"]),
        },
        "crs": {
            "srs": meta.get("srs"), "vertical": meta.get("vertical"),
            "originalSrs": meta.get("srsOriginal"),
            "origin": origin,
            "lat": fix.get("lat"), "lon": fix.get("lon"),
        },
        "tiles": {
            "count": len(scan["contents"]), "b3dmCount": scan["b3dmDisk"],
            "tilesetCount": scan["tilesetDisk"], "missingContent": len(scan["missing"]),
            "maxContentDepth": scan["maxContentDepth"], "lodCounts": scan["lods"],
            "rootTiles": scan["rootTiles"], "failedContent": len(scan["failed"]),
        },
        "geometry": {
            "triangles": scan["triangles"], "vertices": scan["vertices"],
            "bboxLocal": {"min": scan["bboxMin"], "max": scan["bboxMax"]},
            "note": "只读 b3dm 内嵌 GLB 的 accessor 统计（不解析 OSGB 字节）；LOD 互相重叠取 max",
        },
        "textures": {
            "images": scan["images"], "materials": scan["materials"],
            "missingImages": scan["missingImages"], "bytes": scan["textureBytes"],
        },
        "georeference": {
            "transformFixed": bool(fix.get("fixed")),
            "state": fix.get("state"),
            "verticalOffsetM": applied_m,
            "translationBefore": display.get("before"), "translationAfter": display.get("after"),
            "translationDelta": delta_applied,
            "runDelta": display.get("delta"),
            "translationToleranceM": float(fix.get("toleranceM") or 0.01),
            "up": up,
            "axisAngleDeg": fix.get("axisAngleDeg"),
            "notes": fix.get("notes") or "04 未提供说明",
        },
        "artifacts": artifacts,
        "quality": quality,
        "toolchain": toolchain,
        "health": health,
        "validation": {"status": validation.get("status"), "detail": validation.get("detail"),
                       "tool": validation.get("tool"), "durationMs": validation.get("durationMs")},
        "conversion": {"exitCode": (convert.get("tool") or {}).get("exitCode"),
                       "durationMs": (convert.get("tool") or {}).get("durationMs"),
                       "peakMemoryBytes": (convert.get("tool") or {}).get("peakMemoryBytes"),
                       "diskAmplification": products.get("diskAmplification"),
                       "bytesOnDisk": sum(a["bytes"] for a in artifacts),
                       "declaredBytesOnDisk": products.get("bytesOnDisk")},
        "gaps": gaps,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="05 · 模型体检（H-01…H-06 + 模型契约）")
    for key, value in DEFAULTS.items():
        ap.add_argument(f"--{key}", default=value)
    ap.add_argument("--tools-dir", default=None)
    ap.add_argument("--json", action="store_true", help="只打印体检 JSON 摘要")
    args = ap.parse_args()

    started = time.time()
    for key in ("input", "tools", "convert", "registration"):
        if not os.path.isfile(getattr(args, key)):
            print(f"[05] 缺输入 {key}：{getattr(args, key)}", file=sys.stderr)
            return 2
    if not os.path.isdir(args.products):
        print(f"[05] 缺转换产物目录：{args.products}", file=sys.stderr)
        return 2

    manifest = read_json(args.input)
    probe = read_json(args.tools)
    convert = read_json(args.convert)
    fix = read_json(args.registration)
    if manifest.get("schema") != "cardo/lowair-input/v1":
        print("[05] 输入清单 schema 不符", file=sys.stderr)
        return 2
    if fix.get("schema") != "cardo/lowair-registration/v1":
        print("[05] 配准修正 schema 不符", file=sys.stderr)
        return 2

    out_dir = os.path.abspath(args.out)
    model_path = os.path.join(os.path.abspath(args.products), MODEL_NAME)
    health_path = os.path.join(out_dir, "体检.json")
    md_path = os.path.join(out_dir, "模型体检.md")
    contract_path = os.path.join(out_dir, "契约自检.json")
    texture_path = os.path.join(out_dir, "纹理颜色诊断.json")
    cell_texture_path = os.path.join(out_dir, "纹理按格颜色诊断.json")
    # 闸门纪律：先删旧再生成，失败不留下上次的"体检通过"
    for stale in (model_path, health_path, md_path, contract_path, texture_path, cell_texture_path):
        if os.path.isfile(stale):
            os.remove(stale)

    view = convert.get("view") or {}
    scan = tiles_inspect.scan_products(os.path.abspath(args.products))
    tools_dir = resolve_tools_dir(args.tools_dir)
    validation = (tiles_inspect.run_validator(tools_dir, os.path.abspath(args.products))
                  if tools_dir else {"status": "skipped", "detail": "未找到工具目录"})
    health = tiles_inspect.health_of(view, scan, fix, bool(probe.get("ok")), validation)
    artifacts = tiles_inspect.artifacts_of(os.path.abspath(args.products))
    texture, texture_rows = texture_color_stats(os.path.abspath(args.products), artifacts)
    cell_color, cell_rows = texture_cell_color(
        os.path.abspath(args.products), artifacts,
        {"min": scan["bboxMin"], "max": scan["bboxMax"]})
    quality = build_quality(scan)
    quality["textureColor"] = texture
    quality["textureColor"]["byCell"] = cell_color
    write_json(os.path.join(out_dir, "纹理颜色诊断.json"), {
        "schema": "cardo/lowair-texture/v1", "schemaVersion": 1, "generatedAt": now_iso(),
        "producer": {"name": PRODUCER, "version": PRODUCER_VERSION},
        "summary": texture, "perImage": texture_rows,
    })
    write_json(os.path.join(out_dir, "纹理按格颜色诊断.json"), {
        "schema": "cardo/lowair-texture-cell/v1", "schemaVersion": 1, "generatedAt": now_iso(),
        "producer": {"name": PRODUCER, "version": PRODUCER_VERSION},
        "summary": cell_color, "perCell": cell_rows,
    })
    gaps = build_gaps(manifest, view, scan, health, probe)
    if quality["status"] == "fail":
        gaps.append(gap("ARTIFACT_INCOMPLETE", "几何/纹理质量未达可用阈值",
                        "；".join(quality["notes"]) or "退化三角形或纹理缺失过多",
                        "重转或修复输入后重跑 03→05", "upstream"))
    con = consistency(manifest, view, scan, fix, artifacts, convert, probe)
    model = build_model(manifest, convert, fix, probe, scan, artifacts, health, validation, gaps,
                        quality)
    write_json(model_path, model)

    contract = check_contract.check(model, os.path.dirname(model_path))
    write_json(contract_path, contract)

    # counts 分两口径统计：
    #   counts      —— 只数 model 范围的 check（决定 verdict 的那一组）
    #   toolCounts  —— tool 范围的 check（H-06 工具链/可选校验器），只作备注
    # 混在一起数会让「H-06 warn」看起来像「模型有 1 项不通过」，对外易误读。
    model_checks = [c for c in health["checks"] if c.get("scope", "model") == "model"]
    tool_checks = [c for c in health["checks"] if c.get("scope", "model") != "model"]
    counts = {status: sum(1 for c in model_checks if c["status"] == status)
              for status in ("pass", "warn", "fail")}
    tool_counts = {status: sum(1 for c in tool_checks if c["status"] == status)
                   for status in ("pass", "warn", "fail")}
    ok = (health["verdict"] != "fail" and contract["ok"]
          and quality["status"] != "fail" and all(c["status"] == "pass" for c in con))
    doc = {
        "schema": HEALTH_SCHEMA, "schemaVersion": 1, "generatedAt": now_iso(),
        "producer": {"name": PRODUCER, "version": PRODUCER_VERSION},
        "source": {"manifest": rel(args.input), "tools": rel(args.tools),
                   "convertLog": rel(args.convert), "registration": rel(args.registration),
                   "products": rel(args.products)},
        "model": {"path": rel(model_path), "schema": model["schema"],
                  "schemaVersion": model["schemaVersion"], "artifacts": len(artifacts),
                  "artifactsBytes": sum(a["bytes"] for a in artifacts)},
        "health": health, "quality": quality, "consistency": con, "validation": validation,
        "contract": contract, "counts": counts, "toolCounts": tool_counts,
        "toolNotes": health.get("toolNotes") or [],
        "gaps": gaps,
        "verdict": health["verdict"], "ok": bool(ok),
        "durationMs": int((time.time() - started) * 1000),
    }
    write_json(health_path, doc)

    source_bytes = int((manifest.get("counts") or {}).get("bytes") or 0)
    elapsed_ms = int((convert.get("tool") or {}).get("durationMs") or 0)
    # md 的「施加位移」= SRSOrigin.z × up（与 state 无关）；runDelta 记本次实际改动量
    md_fix = {**fix, "delta": model["georeference"]["translationDelta"]}
    tiles_inspect.render_health_md(md_path, model, view, scan, md_fix, source_bytes, elapsed_ms)

    print(f"[05] 体质检 {health['verdict']}（几何质量 {quality['status']}）：H pass {counts['pass']}/"
          f"warn {counts['warn']}/fail {counts['fail']}；一致性 "
          f"{sum(1 for c in con if c['status'] == 'pass')}/{len(con)}；"
          f"契约 {len(contract['checks'])} 项 / 错误 {len(contract['errors'])}；"
          f"产物 {len(artifacts)} 个 / {doc['model']['artifactsBytes']} B；"
          f"退化三角形 {quality['degenerateTriangles']}（{quality['degenerateRatio']:.2%}）")
    # 工具链备注单独一行：H-06 是「可选校验器装没装」，属环境，不属模型缺陷。
    for note in doc["toolNotes"]:
        print(f"[05] 工具备注（不影响模型判定）：{note['id']} {note['name']} = "
              f"{note['status']}｜{note['detail']}")
    if not contract["ok"]:
        for item in contract["errors"][:5]:
            print(f"[05] 契约错误：{item}", file=sys.stderr)
    for item in con:
        if item["status"] != "pass":
            print(f"[05] 一致性未通过：{item['id']} {item['detail']}", file=sys.stderr)
    if not ok:
        print("[05] 体检未通过（fail 项或契约/一致性不过）", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
