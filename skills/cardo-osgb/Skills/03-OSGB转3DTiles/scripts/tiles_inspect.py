#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""3D Tiles 产物自校验与配准修正（本链内部模块，由 03 convert.py / 05 inspect_model.py 调用）。

只读标准容器：b3dm 头 / GLB / glTF JSON / tileset.json —— **不解析 OSGB 字节**。
提供：scan_products（引用完整性 + 几何/纹理统计）、fix_transform（垂直配准修正）、
run_validator（可选 3d-tiles-tools 全量校验）、health_of（H-01…H-06 判据）、
artifacts_of（产物清单 + sha256）、render_health_md（模板渲染 模型体检.md）。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import struct
import subprocess
import time

VALIDATOR_CLI = ("3d-tiles-tools", "node_modules", "3d-tiles-tools", "build", "src", "cli.mjs")
VALIDATOR_PKG = ("3d-tiles-tools", "node_modules", "3d-tiles-tools", "package.json")
WGS84_A, WGS84_F = 6378137.0, 1.0 / 298.257222101
WGS84_E2 = 2 * WGS84_F - WGS84_F ** 2
INF = float("inf")
TEMPLATE_MD = ("..", "模板", "模型体检.md")
VERDICT_TEXT = {
    "pass": "产物结构完整、引用无缺失、纹理无缺失、配准修正已应用；可用于后续指标提取。"
            "精度仍受样本与工具限制，**不外报绝对精度**。",
    "warn": "产物可用，但存在需注意项（见 §5 判据与 §6 gaps）；进入指标提取前请确认其影响。",
    "fail": "产物不完整（见 §5/§6），**不得**进入指标提取或交付。",
}


def sha256(path):
    """文件 sha256（大写 hex）；产物清单与工具指纹共用。"""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest().upper()


INDEX_TYPES = {5121: ("<B", 1), 5123: ("<H", 2), 5125: ("<I", 4)}


def _png_size(data):
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    return None


def _jpeg_size(data):
    """扫 JPEG marker 找 SOF 段，返回 (宽, 高)；只读头部，不解码像素。"""
    if data[:2] != b"\xff\xd8":
        return None
    pos, limit = 2, len(data)
    while pos + 9 <= limit:
        if data[pos] != 0xFF:
            pos += 1
            continue
        marker = data[pos + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        length = int.from_bytes(data[pos + 2:pos + 4], "big")
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            return (int.from_bytes(data[pos + 7:pos + 9], "big"),
                    int.from_bytes(data[pos + 5:pos + 7], "big"))
        if length < 2:
            break
        pos += 2 + length
    return None


def _image_size(data):
    return _png_size(data) or _jpeg_size(data)


def _degenerate_count(acc, views, binary):
    """重复索引退化三角形数（i0==i1 等）；零面积需顶点坐标，这里只给下界，口径写进文档。"""
    kind = INDEX_TYPES.get(acc.get("componentType"))
    bvi = acc.get("bufferView")
    if not kind or not isinstance(bvi, int) or not 0 <= bvi < len(views) or not binary:
        return 0
    fmt, size = kind
    count = int(acc.get("count") or 0)
    view = views[bvi]
    off = int(view.get("byteOffset", 0)) + int(acc.get("byteOffset", 0))
    stride = int(view.get("byteStride") or size)
    if count < 3 or off + (count - 1) * stride + size > len(binary):
        return 0
    reader = struct.Struct(fmt)
    bad = 0
    for tri in range(0, count - 2, 3):
        a = reader.unpack_from(binary, off + tri * stride)[0]
        b = reader.unpack_from(binary, off + (tri + 1) * stride)[0]
        c = reader.unpack_from(binary, off + (tri + 2) * stride)[0]
        if a == b or b == c or a == c:
            bad += 1
    return bad


def glb_of(path):
    """返回 (featureTable, glTF JSON, BIN 字节)；b3dm→GLB 的容器解析只此一份。"""
    raw = open(path, "rb").read()
    if raw[:4] != b"b3dm" or len(raw) < 28:
        raise ValueError("不是 b3dm 容器或头不足 28 字节")
    _version, byte_length, ftj, ftb, btj, btb = [int.from_bytes(raw[i:i + 4], "little")
                                                for i in range(4, 28, 4)]
    if byte_length != len(raw):
        raise ValueError(f"头部声明 {byte_length} B，实际 {len(raw)} B（疑似截断）")
    feature = json.loads(raw[28:28 + ftj].decode("utf-8")) if ftj else {}
    glb = raw[28 + ftj + ftb + btj + btb:]
    if glb[:4] != b"glTF" or len(glb) < 20:
        raise ValueError("b3dm 内未找到 GLB")
    glb_len = int.from_bytes(glb[8:12], "little")
    if glb_len != len(glb):
        raise ValueError(f"GLB 声明 {glb_len} B，实际 {len(glb)} B")
    pos, gltf, binary = 12, None, b""
    while pos + 8 <= glb_len:
        size = int.from_bytes(glb[pos:pos + 4], "little")
        kind = glb[pos + 4:pos + 8]
        if kind == b"JSON":
            gltf = json.loads(glb[pos + 8:pos + 8 + size].decode("utf-8"))
        elif kind[:3] == b"BIN":
            binary = glb[pos + 8:pos + 8 + size]
        pos += 8 + size
    if gltf is None:
        raise ValueError("GLB 缺 JSON chunk")
    return feature, gltf, binary


def parse_b3dm(path):
    """读 b3dm 头 / featureTable / GLB JSON+BIN，统计几何/纹理/兼容性（不碰 OSGB 字节）。"""
    feature, gltf, binary = glb_of(path)
    accessors, views = gltf.get("accessors") or [], gltf.get("bufferViews") or []
    triangles = vertices = primitives = with_normal = with_uv = degenerate = 0
    low, high = [INF] * 3, [-INF] * 3
    for mesh in gltf.get("meshes") or []:
        for prim in mesh.get("primitives") or []:
            primitives += 1
            attrs = prim.get("attributes") or {}
            with_normal += 1 if attrs.get("NORMAL") is not None else 0
            with_uv += 1 if attrs.get("TEXCOORD_0") is not None else 0
            index = attrs.get("POSITION")
            if index is None or index >= len(accessors):
                continue
            acc = accessors[index]
            vertices += acc.get("count", 0)
            if "min" in acc and "max" in acc:
                for axis in range(3):
                    low[axis] = min(low[axis], acc["min"][axis])
                    high[axis] = max(high[axis], acc["max"][axis])
            idx = prim.get("indices")
            if idx is not None and idx < len(accessors):
                triangles += accessors[idx].get("count", 0) // 3
                degenerate += _degenerate_count(accessors[idx], views, binary)
            else:
                triangles += acc.get("count", 0) // 3

    missing = texture_bytes = 0
    sizes, mimes = [], {}
    for image in gltf.get("images") or []:
        mime = image.get("mimeType") or "unknown"
        mimes[mime] = mimes.get(mime, 0) + 1
        bv = image.get("bufferView")
        if isinstance(bv, int) and 0 <= bv < len(views) and views[bv].get("byteLength", 0) > 0:
            view = views[bv]
            texture_bytes += view["byteLength"]
            dim = _image_size(binary[view.get("byteOffset", 0):
                                     view.get("byteOffset", 0) + view["byteLength"]]) if binary else None
            if dim and dim[0] and dim[1]:
                sizes.append([dim[0], dim[1]])
        elif not image.get("uri"):
            missing += 1
    return {"triangles": triangles, "vertices": vertices, "images": len(gltf.get("images") or []),
            "materials": len(gltf.get("materials") or []), "missingImages": missing,
            "textureBytes": texture_bytes, "bboxMin": low if vertices else None,
            "bboxMax": high if vertices else None,
            "primitives": primitives, "normalsPrimitives": with_normal, "uvPrimitives": with_uv,
            "degenerateTriangles": degenerate, "textureSizes": sizes, "imageMimes": mimes,
            "extensionsUsed": sorted(gltf.get("extensionsUsed") or []),
            "asset": gltf.get("asset") or {}, "rtcCenter": feature.get("RTC_CENTER"),
            "batchLength": feature.get("BATCH_LENGTH")}


def walk_tileset(root_dir):
    """从根 tileset.json 递归收集 content.uri（含外部块 tileset），返回内容/缺失/最大深度。"""
    contents, missing, depth_max = [], [], 0

    def load(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def walk(node, base, depth):
        nonlocal depth_max
        uri = (node.get("content") or {}).get("uri")
        if uri and "://" not in uri:
            rel = os.path.normpath(os.path.join(base, uri)).replace("\\", "/")
            target = os.path.join(root_dir, rel)
            if uri.lower().endswith(".json"):
                if os.path.isfile(target):
                    walk(load(target).get("root") or {}, os.path.dirname(rel), depth + 1)
                else:
                    missing.append(rel)
            elif not os.path.isfile(target):
                missing.append(rel)
            else:
                contents.append({"uri": rel, "depth": depth})
                depth_max = max(depth_max, depth)
        for child in node.get("children") or []:
            walk(child, base, depth + 1)

    walk(load(os.path.join(root_dir, "tileset.json")).get("root") or {}, "", 0)
    return {"contents": contents, "missing": missing, "maxContentDepth": depth_max}


def _iou_xy(box_a, box_b):
    """两个局部包围盒在 x/y 平面的 IoU（LOD 瓦片空间重叠度，不看 z）。"""
    ax0, ay0, ax1, ay1 = box_a[0][0], box_a[0][1], box_a[1][0], box_a[1][1]
    bx0, by0, bx1, by1 = box_b[0][0], box_b[0][1], box_b[1][0], box_b[1][1]
    iw = min(ax1, bx1) - max(ax0, bx0)
    ih = min(ay1, by1) - max(ay0, by0)
    if iw <= 0 or ih <= 0:
        return 0.0
    inter = iw * ih
    union = (ax1 - ax0) * (ay1 - ay0) + (bx1 - bx0) * (by1 - by0) - inter
    return inter / union if union > 0 else 0.0


def _tile_overlap(details):
    """瓦片两两 x/y IoU：重叠对（≥0.5）与重叠对均值，用于判断 LOD 空间分区是否重叠。"""
    boxes = [(d["bboxMin"], d["bboxMax"]) for d in details if d["bboxMin"] and d["bboxMax"]]
    strong, overlapping, total = 0, 0, 0.0
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            iou = _iou_xy(boxes[i], boxes[j])
            if iou > 0:
                overlapping += 1
                total += iou
                if iou >= 0.5:
                    strong += 1
    return {"overlapPairs": strong, "overlappingPairs": overlapping,
            "meanIou": round(total / overlapping, 4) if overlapping else 0.0}


def _lod_stats(details, out_dir):
    """按文件名 `_L<level>_` 分组统计瓦片/三角面/顶点/字节（看 LOD 金字塔的实际占比）。"""
    groups = {}
    for detail in details:
        match = re.search(r"_L(\d{1,2})_", os.path.basename(detail["uri"]))
        key = f"L{match.group(1)}" if match else "root"
        item = groups.setdefault(key, {"tiles": 0, "triangles": 0, "vertices": 0, "images": 0,
                                      "bytes": 0})
        item["tiles"] += 1
        item["triangles"] += detail["triangles"]
        item["vertices"] += detail["vertices"]
        item["images"] += detail["images"]
        try:
            item["bytes"] += os.path.getsize(os.path.join(out_dir, detail["uri"]))
        except OSError:
            pass
    total_triangles = sum(item["triangles"] for item in groups.values()) or 1
    for item in groups.values():
        item["triangleShare"] = round(item["triangles"] / total_triangles, 4)
    return {key: groups[key] for key in sorted(groups, key=lambda k: (k != "root", int(k[1:]) if k[1:].isdigit() else 0))}


def scan_products(out_dir):
    """自校验：引用完整性 + 逐 b3dm 解析统计 + 磁盘枚举（b3dm / tileset / LOD / 包围盒）。"""
    tree = walk_tileset(out_dir)
    details, failed = [], []
    for item in tree["contents"]:
        try:
            details.append({**item, **parse_b3dm(os.path.join(out_dir, item["uri"]))})
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            failed.append({"path": item["uri"], "error": f"{type(exc).__name__}: {exc}"})
    disk_b3dm, disk_tileset = [], []
    for dirpath, dirs, files in os.walk(out_dir):
        dirs[:] = [d for d in dirs if d not in (".work", "prev")]
        for fname in files:
            rel = os.path.relpath(os.path.join(dirpath, fname), out_dir).replace("\\", "/")
            if fname.lower().endswith(".b3dm"):
                disk_b3dm.append(rel)
            elif fname == "tileset.json":
                disk_tileset.append(rel)
    lods = {}
    for rel in disk_b3dm:
        match = re.search(r"_L(\d{1,2})_", os.path.basename(rel))
        if match:
            lods[match.group(1)] = lods.get(match.group(1), 0) + 1
    low = [min(d["bboxMin"][k] for d in details if d["bboxMin"]) for k in range(3)]
    high = [max(d["bboxMax"][k] for d in details if d["bboxMax"]) for k in range(3)]
    mimes = {}
    for detail in details:
        for mime, count in detail["imageMimes"].items():
            mimes[mime] = mimes.get(mime, 0) + count
    sizes = [size for detail in details for size in detail["textureSizes"]]
    return {"contents": tree["contents"], "missing": tree["missing"],
            "maxContentDepth": tree["maxContentDepth"], "failed": failed,
            "b3dmDisk": len(disk_b3dm), "tilesetDisk": len(disk_tileset), "lods": lods,
            "rootTiles": len(disk_b3dm) - sum(lods.values()),
            "triangles": sum(d["triangles"] for d in details),
            "vertices": sum(d["vertices"] for d in details),
            "images": sum(d["images"] for d in details),
            "materials": sum(d["materials"] for d in details),
            "missingImages": sum(d["missingImages"] for d in details),
            "textureBytes": sum(d["textureBytes"] for d in details),
            "bboxMin": [round(v, 3) for v in low], "bboxMax": [round(v, 3) for v in high],
            "primitives": sum(d["primitives"] for d in details),
            "normalsPrimitives": sum(d["normalsPrimitives"] for d in details),
            "uvPrimitives": sum(d["uvPrimitives"] for d in details),
            "degenerateTriangles": sum(d["degenerateTriangles"] for d in details),
            "textureSizes": sizes, "imageMimes": mimes,
            "extensionsUsed": sorted({e for d in details for e in d["extensionsUsed"]}),
            "rtcTiles": sum(1 for d in details if d["rtcCenter"]),
            "batchTiles": sum(1 for d in details if d["batchLength"]),
            "assetGenerators": sorted({str((d["asset"] or {}).get("generator") or "")
                                       for d in details if (d["asset"] or {}).get("generator")}),
            "lodStats": _lod_stats(details, out_dir),
            "tileOverlap": _tile_overlap(details)}


def inverse_geodetic(x, y, z):
    """ECEF → 大地经纬度（WGS84 椭球迭代；不依赖投影库）。"""
    lon, p = math.atan2(y, x), math.hypot(x, y)
    lat = math.atan2(z, p * (1.0 - WGS84_E2))
    for _ in range(8):
        n = WGS84_A / math.sqrt(1.0 - WGS84_E2 * math.sin(lat) ** 2)
        h = p / math.cos(lat) - n
        lat = math.atan2(z, p * (1.0 - WGS84_E2 * n / (n + h)))
    return lat, lon


def geodetic_up(lat, lon):
    """geodetic 坐标的 up 单位向量（ENU 第三轴）。"""
    return [math.cos(lat) * math.cos(lon), math.cos(lat) * math.sin(lon), math.sin(lat)]


def fix_transform(out_dir, origin):
    """平移 += SRSOrigin.z × up(lat,lon)：修正工具"未计 Z、整体低 SRSOrigin.z"的缺陷。"""
    path = os.path.join(out_dir, "tileset.json")
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    root = data.get("root") or {}
    transform = root.get("transform")
    if not isinstance(transform, list) or len(transform) != 16:
        return {"fixed": False, "reason": "root.transform 缺失或长度不为 16"}
    before = [transform[12], transform[13], transform[14]]
    lat, lon = inverse_geodetic(*before)
    up, dz = geodetic_up(lat, lon), float(origin[2])
    after = [before[k] + dz * up[k] for k in range(3)]
    transform[12], transform[13], transform[14] = after
    root["transform"] = transform
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    column = [transform[8], transform[9], transform[10]]
    dot = max(-1.0, min(1.0, sum(column[k] * up[k] for k in range(3))))
    return {"fixed": True, "lat": lat, "lon": lon, "up": up, "before": before, "after": after,
            "delta": [dz * up[k] for k in range(3)], "offsetM": -dz, "appliedM": dz,
            "axisAngleDeg": math.degrees(math.acos(dot))}


def _anon(text, out_dir, tools_dir):
    """把绝对路径换成占位符（日志/JSON 里不出现盘符路径）。"""
    for raw, token in ((out_dir, "<out>"), (tools_dir or "", "<tools-dir>")):
        if raw:
            text = text.replace(raw, token).replace(raw.replace("\\", "/"), token)
    return text


def run_validator(tools_dir, out_dir):
    """（可选）3d-tiles-tools convert 整树 → .3tz：独立实现的第二道完整性校验。"""
    cli, pkg = os.path.join(tools_dir, *VALIDATOR_CLI), os.path.join(tools_dir, *VALIDATOR_PKG)
    if not os.path.isfile(cli) or not os.path.isfile(pkg):
        return {"status": "skipped", "detail": "未安装 3d-tiles-tools（可选校验降级为自校验）"}
    try:
        with open(pkg, encoding="utf-8") as fh:
            version = json.load(fh).get("version")
    except (OSError, ValueError):
        version = None
    node = shutil.which("node") or os.environ.get("CARDO_OSGB_NODE")
    if not node:
        return {"status": "skipped", "detail": "node 不在 PATH，且未设 CARDO_OSGB_NODE"}
    target = os.path.join(out_dir, ".work", "validated.3tz")
    if os.path.isfile(target):
        os.remove(target)
    started = time.time()
    try:
        proc = subprocess.run([node, cli, "convert", "-i", out_dir, "-o", target, "--logLevel", "warn"],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=300)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "fail", "tool": f"3d-tiles-tools@{version}", "mode": "convert",
                "detail": _anon(f"{type(exc).__name__}: {exc}", out_dir, tools_dir)}
    elapsed = int((time.time() - started) * 1000)
    if proc.returncode == 0 and os.path.isfile(target):
        size = os.path.getsize(target)
        os.remove(target)
        return {"status": "pass", "tool": f"3d-tiles-tools@{version}", "mode": "convert",
                "exitCode": 0, "durationMs": elapsed, "outputBytes": size,
                "detail": "整树 convert → .3tz 成功（临时包已删除）"}
    detail = _anon(((proc.stderr or "") + (proc.stdout or "")).strip()[-600:], out_dir, tools_dir)
    return {"status": "fail", "tool": f"3d-tiles-tools@{version}", "mode": "convert",
            "exitCode": proc.returncode, "durationMs": elapsed, "detail": detail}


def health_of(view, scan, fix, probe_ok, validation):
    """H-01…H-06 判据（口径见 规则/体检判据.md）。"""
    expected = sum(view["blocks"].values())
    top_bytes = sum(item["bytes"] for item in view["topLevel"])
    lod_in = {key: view["lods"][key] for key in sorted(view["lods"])}
    lod_out = {key: scan["lods"][key] for key in sorted(scan["lods"])}
    checks = []

    def add(cid, name, status, detail):
        checks.append({"id": cid, "name": name, "status": status, "detail": detail})

    count_ok = scan["b3dmDisk"] == expected
    add("H-01", "文件数一致", "pass" if count_ok and top_bytes <= 4096
        else ("warn" if count_ok else "fail"),
        f"osgb {view['osgb']}（块目录 {expected} / 顶层 {len(view['topLevel'])} 个 {top_bytes} B）"
        f"→ 产物 b3dm {scan['b3dmDisk']}")
    add("H-02", "瓦片引用完整", "pass" if not scan["missing"] and not scan["failed"] else "fail",
        f"内容 {len(scan['contents'])} 条 / 引用缺失 {len(scan['missing'])} / "
        f"解析失败 {len(scan['failed'])}")
    if lod_in == lod_out:
        lod_status = "pass"
    elif any(lod_in.get(k, 0) > lod_out.get(k, 0) for k in lod_in):
        lod_status = "fail"
    else:
        lod_status = "warn"
    add("H-03", "LOD 计数", lod_status,
        "输入 " + ", ".join(f"L{k}={v}" for k, v in lod_in.items())
        + " / 产物 " + ", ".join(f"L{k}={v}" for k, v in lod_out.items()))
    add("H-04", "纹理缺失", "warn" if scan["images"] == 0
        else ("pass" if scan["missingImages"] == 0 else "fail"),
        f"{scan['images']} 张图 / {scan['materials']} 材质 / 缺 {scan['missingImages']} / "
        f"{scan['textureBytes']} B")
    displacement = math.dist(fix["before"], fix["after"]) if fix.get("fixed") else 0.0
    applied_ok = fix.get("fixed") and abs(displacement - abs(fix["appliedM"])) < 1e-6
    add("H-05", "配准修正量已应用", "pass" if applied_ok else "fail",
        f"平移 +{fix.get('appliedM'):.3f} m × up；检测偏差 {fix.get('offsetM'):.3f} m"
        if fix.get("fixed") else f"未修正：{fix.get('reason')}")
    add("H-06", "工具版本/完整性", "pass" if probe_ok and validation.get("status") == "pass"
        else ("warn" if probe_ok and validation.get("status") == "skipped" else "fail"),
        "白名单命中" + ("；全量校验通过" if validation.get("status") == "pass"
                        else f"；校验器 {validation.get('status')}"))
    verdict = "fail" if any(c["status"] == "fail" for c in checks) else (
        "warn" if any(c["status"] == "warn" for c in checks) else "pass")
    return {"verdict": verdict, "checks": checks}


def artifacts_of(out_dir):
    """产物清单（相对路径）：tileset.json + Data/**.b3dm；排除 .work 与本适配层自己的输出。"""
    items = []
    for dirpath, dirs, files in os.walk(out_dir):
        dirs[:] = [d for d in dirs if d not in (".work", "prev")]
        for fname in files:
            if fname in ("cardo-lowair-model.json", "模型体检.md") or fname.endswith(".prev"):
                continue
            if fname == "tileset.json" or fname.lower().endswith(".b3dm"):
                full = os.path.join(dirpath, fname)
                items.append({"path": os.path.relpath(full, out_dir).replace("\\", "/"),
                              "bytes": os.path.getsize(full), "sha256": sha256(full)})
    return sorted(items, key=lambda item: item["path"])


def _texture_color_text(texture) -> str:
    """纹理颜色诊断的人读摘要（诊断-only，不进指标）。"""
    if not isinstance(texture, dict) or texture.get("status") != "ok":
        reason = (texture or {}).get("reason") if isinstance(texture, dict) else None
        return f"skipped：{reason or '未启用/不可用'}"
    green = texture.get("greenShare") or {}
    gray = texture.get("grayShare") or {}
    text = (f"{texture.get('images')} 张 / {texture.get('sampledPixels')} 像素采样；"
            f"绿 P50 {green.get('p50')}、按字节加权 {green.get('byteWeighted')}；"
            f"灰 P50 {gray.get('p50')}、加权 {gray.get('byteWeighted')}（仅诊断，未映射平面面积）")
    by_cell = texture.get("byCell") or {}
    if by_cell.get("status") == "ok":
        per_cell = by_cell.get("greenSharePerCell") or {}
        text += (f"；按格 UV 采样 {by_cell.get('cellsWithSamples')} 格 / {by_cell.get('samples')} 样本："
                 f"绿 {by_cell.get('greenShare')}、灰 {by_cell.get('grayShare')}，"
                 f"格内绿 P50 {per_cell.get('p50')}")
    elif by_cell:
        text += f"；按格 UV 采样 skipped：{by_cell.get('reason')}"
    return text


def render_health_md(path, report, view, scan, fix, source_bytes, elapsed_ms):
    """渲染 模型体检.md（模板 token 替换；模板见 模板/模型体检.md）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    template_path = os.path.normpath(os.path.join(here, *TEMPLATE_MD))
    template = open(template_path, encoding="utf-8").read() if os.path.isfile(template_path) else (
        "# 模型体检 · {{sampleName}}\n\n（模板缺失）\n")
    quality = report.get("quality") or {}
    resolution = quality.get("textureResolution") or {}
    overlap = quality.get("tileOverlap") or {}
    rows = [
        ("质量结论", quality.get("status")),
        ("图元 / 法线覆盖 / UV 覆盖", f"{quality.get('primitiveCount')} / "
                                      f"{quality.get('normalsCoverage')} / {quality.get('uvCoverage')}"),
        ("退化三角形（重复索引下界）", f"{quality.get('degenerateTriangles')}（{quality.get('degenerateRatio')}）"),
        ("纹理分辨率", f"{resolution.get('min')} – {resolution.get('max')}，"
                     f"{resolution.get('unique')} 种规格，中位 {resolution.get('medianPixels')} 像素"),
        ("图像格式", quality.get("imageMimes")),
        ("glTF 扩展", quality.get("extensionsUsed")),
        ("RTC_CENTER / batch", f"{quality.get('rtcTiles')} / {quality.get('batchTiles')} 个瓦片"),
        ("瓦片重叠（x/y IoU≥0.5）", f"{overlap.get('overlapPairs', 0)} 对，均值 {overlap.get('meanIou', 0)}"),
        ("LOD 分组（瓦片 / 三角面占比）",
         "；".join(f"{key} {item.get('tiles')} / {item.get('triangleShare')}"
                   for key, item in (quality.get("lodStats") or {}).items()) or "—"),
        ("纹理颜色诊断（未校验）", _texture_color_text(quality.get("textureColor"))),
    ]
    quality_table = "| 项 | 值 |\n|---|---|\n" + "\n".join(f"| {k} | {v} |" for k, v in rows)
    tokens = {
        "sampleName": report["source"]["name"], "generatedAt": report["generatedAt"],
        "verdict": report["health"]["verdict"],
        "verdictParagraph": VERDICT_TEXT[report["health"]["verdict"]],
        "sourceFiles": report["source"]["files"], "sourceOsgb": report["source"]["osgbFiles"],
        "sourceOther": report["source"]["files"] - report["source"]["osgbFiles"],
        "sourceMiB": f"{source_bytes / 1048576:.2f}",
        "tileCount": report["tiles"]["count"], "b3dmCount": report["tiles"]["b3dmCount"],
        "tilesetCount": report["tiles"]["tilesetCount"],
        "maxContentDepth": report["tiles"]["maxContentDepth"],
        "triangles": report["geometry"]["triangles"], "vertices": report["geometry"]["vertices"],
        "bboxMin": report["geometry"]["bboxLocal"]["min"],
        "bboxMax": report["geometry"]["bboxLocal"]["max"],
        "images": report["textures"]["images"], "materials": report["textures"]["materials"],
        "missingImages": report["textures"]["missingImages"],
        "texMiB": f"{report['textures']['bytes'] / 1048576:.2f}",
        "srs": report["crs"]["srs"], "srsOriginal": report["crs"]["originalSrs"],
        "vertical": report["crs"]["vertical"] or "未声明", "origin": report["crs"]["origin"],
        "lat": report["crs"]["lat"], "lon": report["crs"]["lon"],
        "verticalOffsetM": report["georeference"]["verticalOffsetM"],
        "before": report["georeference"]["translationBefore"],
        "after": report["georeference"]["translationAfter"],
        "delta": [round(v, 3) for v in fix["delta"]],
        "toolchainTable": "\n".join(
            f"| {t['name']} | {t.get('version')} | `{t.get('sha256') or '—'}` | {t.get('license')} |"
            for t in report["toolchain"]),
        "healthTable": "\n".join(
            f"| {c['id']} | {c['name']} | {c['status']} | {c['detail']} |"
            for c in report["health"]["checks"]),
        "lodTable": "| LOD | 输入 osgb | 产物 b3dm |\n|---|---|---|\n" + "\n".join(
            f"| L{key} | {view['lods'].get(key, 0)} | {scan['lods'].get(key, 0)} |"
            for key in sorted(set(view["lods"]) | set(scan["lods"]), key=int)),
        "gapsTable": ("| code | 缺什么 | 需要什么 | owner |\n|---|---|---|---|\n" + "\n".join(
            f"| {g['code']} | {g['what']} | {g['need']} | {g['owner']} |"
            for g in report["gaps"])) if report["gaps"] else "（无 gaps）",
        "validationLine": f"{report['validation'].get('status')}：{report['validation'].get('detail')}",
        "elapsed": f"{elapsed_ms / 1000:.1f}",
    }
    for key, value in tokens.items():
        template = template.replace("{{" + key + "}}", str(value))
    template = (template.replace("{{qualityTable}}", quality_table)
                .replace("{{qualityLine}}", "；".join(str(n) for n in quality.get("notes") or [])
                         or "无额外质量提示。"))
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(template)