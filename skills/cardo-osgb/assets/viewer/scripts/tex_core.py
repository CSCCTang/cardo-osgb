# -*- coding: utf-8 -*-
"""纹理与材质识别 · 核心：b3dm 解析 + 顶面正射栅格化。

只读标准容器（b3dm 头 / GLB chunk / glTF JSON），不解析 OSGB 私有字节。
依赖：numpy、Pillow，无其他第三方库。

坐标：瓦片顶点已在 `cardo-lowair-model.json` 的 `geometry.bboxLocal` 局部米制系内；
若 glTF 带 `RTC_CENTER`，按平移处理。
"""
from __future__ import annotations

import io
import json
import math
import os
import sys

import numpy as np
from PIL import Image

sys.dont_write_bytecode = True

EPS = 1e-9


# ------------------------------------------------------------------ 容器

def read_b3dm(path):
    """b3dm → (glTF JSON, BIN 字节)。头部长度不符直接报错，不静默截断。"""
    with open(path, "rb") as fh:
        raw = fh.read()
    if raw[:4] != b"b3dm" or len(raw) < 28:
        raise ValueError("不是 b3dm 容器（魔数或头部长度不符）")
    declared = int.from_bytes(raw[8:12], "little")
    if declared != len(raw):
        raise ValueError(f"b3dm 头部声明 {declared} B，实际 {len(raw)} B（疑似截断）")
    ftj, ftb, btj, btb = (int.from_bytes(raw[i:i + 4], "little") for i in range(12, 28, 4))
    glb = raw[28 + ftj + ftb + btj + btb:]
    if glb[:4] != b"glTF" or len(glb) < 20:
        raise ValueError("b3dm 内未找到 GLB chunk")
    total, pos, gltf, binary = int.from_bytes(glb[8:12], "little"), 12, None, b""
    while pos + 8 <= min(total, len(glb)):
        size = int.from_bytes(glb[pos:pos + 4], "little")
        kind = glb[pos + 4:pos + 8]
        body = glb[pos + 8:pos + 8 + size]
        if kind == b"JSON":
            gltf = json.loads(body.decode("utf-8"))
        elif kind[:3] == b"BIN":
            binary = body
        pos += 8 + size
    if gltf is None:
        raise ValueError("GLB 缺 JSON chunk")
    return gltf, binary


def _accessor(gltf, binary, index, width):
    """float32 VEC2/VEC3 访问器 → (N,width)；越界或类型不符返回 None。"""
    if not isinstance(index, int):
        return None
    accessors = gltf.get("accessors") or []
    views = gltf.get("bufferViews") or []
    if not 0 <= index < len(accessors):
        return None
    acc = accessors[index]
    want = {2: "VEC2", 3: "VEC3"}[width]
    if acc.get("componentType") != 5126 or acc.get("type") != want:
        return None
    bvi = acc.get("bufferView")
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


def _indices(gltf, binary, index):
    if not isinstance(index, int):
        return None
    accessors = gltf.get("accessors") or []
    views = gltf.get("bufferViews") or []
    if not 0 <= index < len(accessors):
        return None
    acc = accessors[index]
    dtype = {5121: "<u1", 5123: "<u2", 5125: "<u4"}.get(acc.get("componentType"))
    bvi = acc.get("bufferView")
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
    """取漫反射贴图索引：KHR_techniques_webgl.u_diffuse 优先，其次 pbr baseColorTexture。"""
    if not isinstance(material, dict):
        return None
    tech = ((material.get("extensions") or {}).get("KHR_techniques_webgl") or {})
    for key in ("u_diffuse", "diffuse"):
        item = (tech.get("values") or {}).get(key)
        if isinstance(item, dict) and isinstance(item.get("index"), int):
            return item["index"]
    base = (material.get("pbrMetallicRoughness") or {}).get("baseColorTexture") or {}
    return base.get("index") if isinstance(base.get("index"), int) else None


def decode_textures(gltf, binary):
    """解码内嵌贴图为 {索引: uint8 RGB 数组}；外链贴图/损坏图跳过并计数。"""
    views = gltf.get("bufferViews") or []
    images = gltf.get("images") or []
    out, failed = {}, []
    for tex_index, texture in enumerate(gltf.get("textures") or []):
        source = texture.get("source")
        if not isinstance(source, int) or not 0 <= source < len(images):
            failed.append({"texture": tex_index, "reason": "非内嵌 image（外链丢图）"})
            continue
        bv = images[source].get("bufferView")
        if not isinstance(bv, int) or not 0 <= bv < len(views):
            failed.append({"texture": tex_index, "reason": "image 无 bufferView"})
            continue
        view = views[bv]
        off = int(view.get("byteOffset", 0))
        length = int(view.get("byteLength", 0))
        if length <= 0:
            failed.append({"texture": tex_index, "reason": "bufferView 空"})
            continue
        try:
            im = Image.open(io.BytesIO(binary[off:off + length])).convert("RGB")
        except (OSError, ValueError) as exc:
            failed.append({"texture": tex_index, "reason": f"{type(exc).__name__}"})
            continue
        out[tex_index] = np.asarray(im, dtype=np.uint8)
    return out, failed


def rtc_offset(gltf):
    """b3dm/glTF 的 RTC_CENTER 平移；无则零向量。"""
    rtc = gltf.get("RTC_CENTER") or (((gltf.get("asset") or {}).get("extras") or {}).get("RTC_CENTER"))
    if isinstance(rtc, (list, tuple)) and len(rtc) == 3:
        return np.asarray(rtc, dtype=np.float64)
    return np.zeros(3, dtype=np.float64)


def lod_level(rel_path, gltf=None):
    """瓦片细分层级：优先文件名 `_L<数字>_`，其次 glTF extras，最后 99（视为最粗）。"""
    base = os.path.basename(str(rel_path))
    for token in base.replace(".", "_").split("_"):
        if token.startswith("L") and token[1:].isdigit():
            return int(token[1:])
    extras = (gltf or {}).get("asset", {}).get("extras") or {}
    if isinstance(extras.get("lod"), int):
        return int(extras["lod"])
    return 99


# ------------------------------------------------------------------ 采样

def _bary_lattice(a, b, c, res, step_px=0.7):
    """在像素坐标三角形内做重心格点采样，返回重心权重 (K,3)。"""
    lo = np.floor(np.minimum(np.minimum(a, b), c)).astype(np.int64) - 1
    hi = np.ceil(np.maximum(np.maximum(a, b), c)).astype(np.int64) + 1
    w = int(hi[0] - lo[0] + 1)
    h = int(hi[1] - lo[1] + 1)
    if w <= 0 or h <= 0 or w * h > 40000:
        return np.array([[1 / 3, 1 / 3, 1 / 3]], dtype=np.float64)
    gx = lo[0] + np.arange(0, w, step_px)
    gy = lo[1] + np.arange(0, h, step_px)
    px, py = np.meshgrid(gx, gy, indexing="ij")
    px = px.ravel()
    py = py.ravel()
    v0 = b - a
    v1 = c - a
    den = v0[0] * v1[1] - v1[0] * v0[1]
    if abs(den) < EPS:
        return np.array([[1 / 3, 1 / 3, 1 / 3]], dtype=np.float64)
    vx = px - a[0]
    vy = py - a[1]
    u = (vx * v1[1] - v1[0] * vy) / den
    v = (v0[0] * vy - vx * v0[1]) / den
    ok = (u >= -1e-6) & (v >= -1e-6) & (u + v <= 1 + 1e-6)
    if not ok.any():
        return np.array([[1 / 3, 1 / 3, 1 / 3]], dtype=np.float64)
    u, v = u[ok], v[ok]
    return np.stack([1.0 - u - v, u, v], axis=1)


def _push(px_out, py_out, z_out, rgb_out, area_out, tex_out,
          bx, by, bz, buv, area, tex, tw, th, tex_index):
    """把一组样本点的 UV 映射到贴图像素并追加到缓冲。"""
    tx = np.clip((np.mod(buv[:, 0], 1.0) * tw).astype(np.int64), 0, tw - 1)
    ty = np.clip((np.mod(buv[:, 1], 1.0) * th).astype(np.int64), 0, th - 1)
    px_out.append(bx.astype(np.float32))
    py_out.append(by.astype(np.float32))
    z_out.append(bz.astype(np.float32))
    rgb_out.append(tex[ty, tx])
    area_out.append(area.astype(np.float32))
    tex_out.append(np.full(bx.shape, tex_index, dtype=np.int32))


def tile_samples(tile_path, bbox_min, res, normal_z=0.5):
    """单个瓦片 → 朝上三角面样本：像素坐标、z、RGB、面积、贴图索引。

    返回 dict（全部 ndarray）或抛 ValueError。大三角形走重心格点，小三角形取重心。
    """
    gltf, binary = read_b3dm(tile_path)
    textures, tex_failed = decode_textures(gltf, binary)
    off = rtc_offset(gltf)

    px_out, py_out, z_out, rgb_out, area_out, tex_out = [], [], [], [], [], []
    tri_total = tri_up = 0
    node_bad = 0
    for node in (gltf.get("nodes") or []):
        if node.get("matrix") or node.get("translation") or node.get("rotation") or node.get("scale"):
            node_bad += 1

    for mesh in gltf.get("meshes") or []:
        for prim in mesh.get("primitives") or []:
            if prim.get("mode") not in (None, 4):
                continue
            attrs = prim.get("attributes") or {}
            pos = _accessor(gltf, binary, attrs.get("POSITION"), 3)
            uv = _accessor(gltf, binary, attrs.get("TEXCOORD_0"), 2)
            if pos is None or uv is None or len(pos) < 3:
                continue
            idx = _indices(gltf, binary, prim.get("indices"))
            if idx is None:
                if len(pos) % 3:
                    continue
                idx = np.arange(len(pos), dtype=np.uint32)
            tri = idx.reshape(-1, 3)
            if tri.size == 0 or int(tri.max()) >= len(pos):
                continue
            mat_index = prim.get("material")
            material = (gltf.get("materials") or [])[mat_index] if isinstance(mat_index, int) else None
            tex_index = _diffuse_texture(material)
            if tex_index not in textures:
                tri_total += len(tri)
                continue
            tex = textures[tex_index]
            th, tw = tex.shape[0], tex.shape[1]

            p = pos.astype(np.float64) + off
            t0, t1, t2 = p[tri[:, 0]], p[tri[:, 1]], p[tri[:, 2]]
            cross = np.cross(t1 - t0, t2 - t0)
            norm = np.linalg.norm(cross, axis=1)
            area = 0.5 * norm
            tri_total += len(tri)
            up = norm > EPS
            nz = np.where(up, cross[:, 2] / np.where(norm == 0, 1, norm), -1.0)
            keep = up & (nz >= normal_z) & (area > 1e-6)
            if not keep.any():
                continue
            tri_up += int(keep.sum())
            pt, at = tri[keep], area[keep]
            q0, q1, q2 = p[pt[:, 0]], p[pt[:, 1]], p[pt[:, 2]]
            u0, u1, u2 = uv[pt[:, 0]], uv[pt[:, 1]], uv[pt[:, 2]]

            # 2D 像素坐标
            sx = (q0[:, 0] - bbox_min[0]) / res
            sy = (q0[:, 1] - bbox_min[1]) / res
            sx1 = (q1[:, 0] - bbox_min[0]) / res
            sy1 = (q1[:, 1] - bbox_min[1]) / res
            sx2 = (q2[:, 0] - bbox_min[0]) / res
            sy2 = (q2[:, 1] - bbox_min[1]) / res
            span = np.maximum.reduce([np.abs(sx1 - sx), np.abs(sy1 - sy),
                                      np.abs(sx2 - sx), np.abs(sy2 - sy),
                                      np.abs(sx2 - sx1), np.abs(sy2 - sy1)])
            small = span <= 1.0

            # 小三角形：重心
            if small.any():
                sel = np.nonzero(small)[0]
                bx = ((sx[sel] + sx1[sel] + sx2[sel]) / 3.0)
                by = ((sy[sel] + sy1[sel] + sy2[sel]) / 3.0)
                bz = ((q0[sel, 2] + q1[sel, 2] + q2[sel, 2]) / 3.0)
                buv = (u0[sel] + u1[sel] + u2[sel]) / 3.0
                _push(px_out, py_out, z_out, rgb_out, area_out, tex_out,
                      bx, by, bz, buv, at[sel], tex, tw, th, tex_index)
            # 大三角形：重心格点
            for i in np.nonzero(~small)[0]:
                a2 = np.array([sx[i], sy[i]])
                b2 = np.array([sx1[i], sy1[i]])
                c2 = np.array([sx2[i], sy2[i]])
                bary = _bary_lattice(a2, b2, c2, res)
                bx = bary @ np.array([sx[i], sx1[i], sx2[i]])
                by = bary @ np.array([sy[i], sy1[i], sy2[i]])
                bz = bary @ np.array([q0[i, 2], q1[i, 2], q2[i, 2]])
                buv = bary @ np.stack([u0[i], u1[i], u2[i]])
                rep = np.full(len(bary), at[i] / max(1, len(bary)))
                _push(px_out, py_out, z_out, rgb_out, area_out, tex_out,
                      bx, by, bz, buv, rep, tex, tw, th, tex_index)

    if not px_out:
        return {"px": np.zeros(0), "py": np.zeros(0), "z": np.zeros(0),
                "rgb": np.zeros((0, 3), np.uint8), "area": np.zeros(0),
                "tex": np.zeros(0, np.int32), "triTotal": tri_total, "triUp": tri_up,
                "textures": len(textures), "texFailed": tex_failed, "nonIdentityNodes": node_bad}
    return {
        "px": np.concatenate(px_out), "py": np.concatenate(py_out),
        "z": np.concatenate(z_out),
        "rgb": np.concatenate(rgb_out, axis=0),
        "area": np.concatenate(area_out),
        "tex": np.concatenate(tex_out),
        "triTotal": tri_total, "triUp": tri_up,
        "textures": len(textures), "texFailed": tex_failed, "nonIdentityNodes": node_bad,
    }
