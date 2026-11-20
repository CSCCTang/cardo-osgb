#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""12 · 屋顶与立面 · 无 numpy 兜底（本机 `python` 未装 numpy 时自动启用）。

接口与 `scripts/common.py` 对齐（SCHEMA / OUT_ROOT / emit / parse_args / finish /
load_model / load_origin / b3dm_files / load_grid），另外提供纯 stdlib 的
GLB 顶点/索引读取（`gltf_and_bin / read_vec3 / read_indices`），两个子算法共用。

不安装任何第三方包：09 的 `栅格.npz` 用 zipfile + npy 头解析；b3dm 用 struct 读。
"""
from __future__ import annotations

import argparse
import array
import ast
import json
import os
import struct
import sys
import zipfile
from datetime import datetime

sys.dont_write_bytecode = True

SCHEMA = "cardo/lowair-enhance/v1"
OUT_ROOT = os.path.join("过程文件", "12-屋顶与立面")
MODEL_REL = os.path.join("过程文件", "03-OSGB转3DTiles", "转换产物", "cardo-lowair-model.json")
RECON_REL = os.path.join("过程文件", "09-三维要素反演", "三维要素反演.json")
GRID_REL = os.path.join("过程文件", "09-三维要素反演", "栅格", "栅格.npz")
VECTOR_REL = os.path.join("过程文件", "10-矢量入库", "矢量成果.geojson")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def slug(algo: str) -> str:
    return algo.replace(".", "-")


def read_json(path: str):
    with open(path, encoding="utf-8-sig") as fh:
        return json.load(fh)


def write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def load_model(workspace: str) -> dict:
    path = os.path.join(workspace, MODEL_REL)
    if not os.path.isfile(path):
        raise SystemExit(f"[12] 缺模型契约：{MODEL_REL}（先跑 03/05）")
    return read_json(path)


def load_origin(workspace: str):
    return (load_model(workspace).get("crs") or {}).get("origin")


def b3dm_files(workspace: str, model: dict):
    """[(相对路径, 绝对路径)]；相对模型目录解析，拒绝 `..`。"""
    model_dir = os.path.dirname(os.path.join(workspace, MODEL_REL))
    out = []
    for art in model.get("artifacts") or []:
        rel = str(art.get("path") or "")
        if not rel.lower().endswith(".b3dm") or ".." in rel.replace("\\", "/").split("/"):
            continue
        out.append((rel, os.path.join(model_dir, rel)))
    return out


# ---------------------------------------------------------------- npy / npz
def _read_npy(raw: bytes):
    """最小 .npy 读取：返回 (Python 列表, shape)；支持 <f8/<f4/|b1/<i4/<i2/u1。"""
    if raw[:6] != b"\x93NUMPY":
        raise ValueError("不是 .npy")
    major = raw[6]
    if major == 1:
        hlen = struct.unpack_from("<H", raw, 8)[0]
        hstart = 10
    else:
        hlen = struct.unpack_from("<I", raw, 8)[0]
        hstart = 12
    header = ast.literal_eval(raw[hstart:hstart + hlen].decode("latin1"))
    dtype, shape = header["descr"], header["shape"]
    body = raw[hstart + hlen:]
    count = 1
    for dim in shape:
        count *= int(dim)
    if dtype == "|b1":
        arr = array.array("B")
        arr.frombytes(body[:count])
        return [bool(v) for v in arr], shape
    if dtype in ("<f8", ">f8"):
        arr = array.array("d")
        arr.frombytes(body[:8 * count])
        return list(arr[:count]), shape
    if dtype in ("<f4", ">f4"):
        arr = array.array("f")
        arr.frombytes(body[:4 * count])
        return list(arr[:count]), shape
    if dtype in ("<i4", ">i4"):
        arr = array.array("i")
        arr.frombytes(body[:4 * count])
        return list(arr[:count]), shape
    if dtype in ("<i2", ">i2"):
        arr = array.array("h")
        arr.frombytes(body[:2 * count])
        return list(arr[:count]), shape
    raise ValueError(f"不支持的 dtype：{dtype}")


def _nested(vals, shape):
    if len(shape) != 2:
        return vals
    nx, ny = int(shape[0]), int(shape[1])
    return [vals[i * ny:(i + 1) * ny] for i in range(nx)]


def load_grid(workspace: str) -> dict:
    """纯 stdlib 读 09 栅格；dsm/ground/ndsm/observed 为「按 x 行、按 y 列」的嵌套列表。"""
    path = os.path.join(workspace, GRID_REL)
    if not os.path.isfile(path):
        raise SystemExit(f"[12] 缺栅格：{GRID_REL}（先跑 09）")
    with zipfile.ZipFile(path) as zf:
        out, shape = {}, None
        for name in zf.namelist():
            key = name[:-4] if name.endswith(".npy") else name
            vals, shp = _read_npy(zf.read(name))
            out[key] = _nested(vals, shp)
            if shape is None and len(shp) == 2:
                shape = shp
    if shape is None:
        raise ValueError("栅格.npz 没有二维数组")
    return {"dsm": out["dsm"], "ground": out["ground"], "ndsm": out["ndsm"],
            "observed": out["observed"], "cell": float(out["cell"][0]),
            "x0": float(out["x0"][0]), "y0": float(out["y0"][0]),
            "nx": int(shape[0]), "ny": int(shape[1])}


def _tile_lod(rel: str):
    """从 b3dm 路径取 LOD 层级（`..._L19_2.b3dm` → 19）；无层级标记返回 None。"""
    import re
    m = re.search(r"_L(\d+)_", rel.replace("\\", "/"))
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------- b3dm / GLB
def gltf_and_bin(path: str):
    raw = open(path, "rb").read()
    if raw[:4] != b"b3dm" or len(raw) < 28:
        raise ValueError("非 b3dm 或头不足 28 B")
    total = struct.unpack_from("<I", raw, 8)[0]
    if total != len(raw):
        raise ValueError(f"声明 {total} B ≠ 实际 {len(raw)} B")
    ft_json, ft_bin, bt_json, bt_bin = struct.unpack_from("<4I", raw, 12)
    glb = raw[28 + ft_json + ft_bin + bt_json + bt_bin:]
    if glb[:4] != b"glTF" or len(glb) < 20:
        raise ValueError("b3dm 内无 GLB")
    glb_len = struct.unpack_from("<I", glb, 8)[0]
    pos, doc, binary = 12, None, b""
    while pos + 8 <= min(glb_len, len(glb)):
        size, kind = struct.unpack_from("<I", glb, pos)[0], glb[pos + 4:pos + 8]
        body = glb[pos + 8:pos + 8 + size]
        if kind == b"JSON":
            doc = json.loads(body.decode("utf-8"))
        elif kind[:3] == b"BIN":
            binary = body
        pos += 8 + size
    if doc is None:
        raise ValueError("GLB 缺 JSON chunk")
    return doc, binary


def _layout(doc, index, want_type: str, want_components: int):
    """accessor → (byteOffset, byteStride, count, componentType)；不合法返回 None。"""
    accessors, views = doc.get("accessors") or [], doc.get("bufferViews") or []
    if not isinstance(index, int) or not 0 <= index < len(accessors):
        return None
    acc = accessors[index]
    bvi = acc.get("bufferView")
    if acc.get("type") != want_type or acc.get("componentType") is None:
        return None
    if not isinstance(bvi, int) or not 0 <= bvi < len(views):
        return None
    view = views[bvi]
    comp = int(acc.get("componentType"))
    size = {5120: 1, 5121: 1, 5122: 2, 5123: 2, 5125: 4, 5126: 4}[comp]
    stride = int(view.get("byteStride") or size * want_components)
    return (int(view.get("byteOffset", 0)) + int(acc.get("byteOffset", 0)),
            stride, int(acc.get("count") or 0), comp)


_VEC3_CODES = {5126: "f"}


def read_vec3(doc, binary, index):
    """VEC3 float32 → [(x, y, z)]；不可用返回 None。"""
    layout = _layout(doc, index, "VEC3", 3)
    if not layout or not binary:
        return None
    off, stride, count, comp = layout
    code = _VEC3_CODES.get(comp)
    if code is None or count <= 0 or off + (count - 1) * stride + 12 > len(binary):
        return None
    if stride == 12:
        arr = array.array(code)
        arr.frombytes(binary[off:off + 12 * count])
        vals = list(arr[:3 * count])
        return [(vals[i], vals[i + 1], vals[i + 2]) for i in range(0, 3 * count, 3)]
    out = []
    for i in range(count):
        out.append(struct.unpack_from("<3f", binary, off + i * stride))
    return out


_INDEX_CODES = {5121: "B", 5123: "H", 5125: "I"}
_INDEX_SIZE = {5121: 1, 5123: 2, 5125: 4}


def read_indices(doc, binary, index, count):
    """SCALAR 索引 → [int]；不可用返回 range(count)。"""
    layout = _layout(doc, index, "SCALAR", 1)
    if not layout or not binary:
        return list(range(count))
    off, stride, n, comp = layout
    code = _INDEX_CODES.get(comp)
    if code is None or n <= 0 or off + (n - 1) * stride + _INDEX_SIZE[comp] > len(binary):
        return list(range(count))
    if stride == _INDEX_SIZE[comp]:
        arr = array.array(code)
        arr.frombytes(binary[off:off + _INDEX_SIZE[comp] * n])
        return list(arr[:n])
    return [struct.unpack_from("<" + code.lower(), binary, off + i * stride)[0]
            for i in range(n)]


# ---------------------------------------------------------------- 结果落盘
def out_dir_of(workspace: str, algo: str) -> str:
    return os.path.join(workspace, OUT_ROOT, slug(algo))


def check_existing(workspace: str, algo: str, force: bool) -> None:
    target = os.path.join(out_dir_of(workspace, algo), f"{slug(algo)}.json")
    if os.path.exists(target) and not force:
        raise SystemExit(f"[12] 产物已存在：{target}（加 --force 覆盖）")


def emit(workspace: str, algo: str, inputs: dict, params: dict, summary: dict,
         items=None, gaps=None, usable: bool = True, reasons=None,
         markdown: str = "", extra_files=None) -> dict:
    doc = {
        "schema": SCHEMA, "algo": algo, "generatedAt": now_iso(),
        "inputs": inputs, "params": params, "summary": summary,
        "items": items or [], "gaps": gaps or [],
        "quality": {"usable": bool(usable), "reasons": reasons or []},
    }
    d = out_dir_of(workspace, algo)
    os.makedirs(d, exist_ok=True)
    write_json(os.path.join(d, f"{slug(algo)}.json"), doc)
    if markdown:
        with open(os.path.join(d, f"{slug(algo)}.md"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(markdown.rstrip() + "\n")
    done = {"algo": algo, "exitCode": 0 if usable else 3, "generatedAt": doc["generatedAt"],
            "summary": summary, "usable": bool(usable)}
    write_json(os.path.join(workspace, OUT_ROOT, "_done", f"{slug(algo)}.json"), done)
    return doc


def finish(doc: dict) -> int:
    s = doc.get("quality", {})
    print(f"[12:{doc['algo']}] usable={s.get('usable')} "
          f"{json.dumps(doc.get('summary') or {}, ensure_ascii=False)[:300]}")
    return 0 if s.get("usable") else 3


def resolve_skills_root(workspace: str) -> str | None:
    """技能根在哪：优先 CARDO_SKILLS_ROOT（技能包在工作区**之外**的交付包模式），
    其次 <工作区>/Skills（技能包内嵌模式）。

    交付包里 CardoOSGB/ 与工作区是**平级**的，工作区里根本没有 Skills/——
    所以判据不能是「工作区里有没有 Skills/」。
    """
    env = os.environ.get("CARDO_SKILLS_ROOT")
    if env and os.path.isdir(env):
        return os.path.abspath(env)
    local = os.path.join(os.path.abspath(workspace), "Skills")
    return os.path.abspath(local) if os.path.isdir(local) else None


def parse_args(algo: str, description: str):
    ap = argparse.ArgumentParser(description=description)
    ap.add_argument("--workspace", default=".", help="工作区根（默认当前目录）")
    ap.add_argument("--force", action="store_true", help="覆盖已有产物")
    args = ap.parse_args()
    if resolve_skills_root(args.workspace) is None:
        print(f"[12] 找不到技能根（既无 CARDO_SKILLS_ROOT，工作区里也没有 Skills/）："
              f"{args.workspace}", file=sys.stderr)
        raise SystemExit(2)
    check_existing(args.workspace, algo, args.force)
    return args
