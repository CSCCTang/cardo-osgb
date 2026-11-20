#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""11 · 几何增强 · 共享工具（只读契约，**所有子算法脚本都 import 本模块**）。

本文件由主线维护，定时任务只读、不改。提供：
  - 读模型契约 / 遍历 b3dm→GLB→POSITION+NORMAL（不解析 OSGB 二进制）
  - 读 09 的栅格（dsm/ground/ndsm/observed/cell/x0/y0）
  - 结果落盘（JSON + Markdown + done 标记），退出码统一 0/2/3/4

用法（各子脚本）：
    from common import (parse_args, load_model, b3dm_files, mesh_primitives,
                        load_grid, emit, out_dir_of)
"""
from __future__ import annotations

import json
import os
import struct
import sys
from datetime import datetime

sys.dont_write_bytecode = True

import numpy as np

SCHEMA = "cardo/lowair-enhance/v1"
OUT_ROOT = os.path.join("过程文件", "11-几何增强")
MODEL_REL = os.path.join("过程文件", "03-OSGB转3DTiles", "转换产物", "cardo-lowair-model.json")
RECON_REL = os.path.join("过程文件", "09-三维要素反演", "三维要素反演.json")
GRID_REL = os.path.join("过程文件", "09-三维要素反演", "栅格", "栅格.npz")
VECTOR_REL = os.path.join("过程文件", "10-矢量入库", "矢量成果.geojson")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def slug(algo: str) -> str:
    """roof.segment -> roof-segment（目录名不用点号）。"""
    return algo.replace(".", "-")


def read_json(path: str):
    with open(path, encoding="utf-8-sig") as fh:
        return json.load(fh)


def write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


# ---------------------------------------------------------------- 模型与网格
def load_model(workspace: str) -> dict:
    path = os.path.join(workspace, MODEL_REL)
    if not os.path.isfile(path):
        raise SystemExit(f"[11] 缺模型契约：{MODEL_REL}（先跑 03/05）")
    return read_json(path)


def load_origin(workspace: str):
    """EPSG:4544 全局原点 [x, y]；世界坐标 = origin + 局部坐标。缺失返回 None。"""
    return (load_model(workspace).get("crs") or {}).get("origin")


def b3dm_files(workspace: str, model: dict):
    """返回 [(相对路径, 绝对路径)]；相对模型目录解析，拒绝 `..`。"""
    model_dir = os.path.dirname(os.path.join(workspace, MODEL_REL))
    out = []
    for art in model.get("artifacts") or []:
        rel = str(art.get("path") or "")
        if not rel.lower().endswith(".b3dm") or ".." in rel.replace("\\", "/").split("/"):
            continue
        out.append((rel, os.path.join(model_dir, rel)))
    return out


def load_grid(workspace: str) -> dict:
    """09 栅格：dsm/ground/ndsm/observed，axis0=x 轴、axis1=y 轴，单位米。"""
    path = os.path.join(workspace, GRID_REL)
    if not os.path.isfile(path):
        raise SystemExit(f"[11] 缺栅格：{GRID_REL}（先跑 09）")
    z = np.load(path)
    return {"dsm": z["dsm"], "ground": z["ground"], "ndsm": z["ndsm"],
            "observed": z["observed"], "cell": float(z["cell"][0]),
            "x0": float(z["x0"][0]), "y0": float(z["y0"][0])}


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


def _accessor(doc, index):
    """(byteOffset, byteStride, count) 或 None；只认 float32 VEC3。"""
    accessors, views = doc.get("accessors") or [], doc.get("bufferViews") or []
    if not isinstance(index, int) or not 0 <= index < len(accessors):
        return None
    acc = accessors[index]
    bvi = acc.get("bufferView")
    if acc.get("componentType") != 5126 or acc.get("type") != "VEC3":
        return None
    if not isinstance(bvi, int) or not 0 <= bvi < len(views):
        return None
    view = views[bvi]
    return (int(view.get("byteOffset", 0)) + int(acc.get("byteOffset", 0)),
            int(view.get("byteStride") or 12), int(acc.get("count") or 0))


def _vec3(binary, layout):
    if not layout or not binary:
        return None
    off, stride, count = layout
    if count <= 0 or off + (count - 1) * stride + 12 > len(binary):
        return None
    try:
        arr = np.ndarray(shape=(count, 3), dtype="<f4", buffer=binary,
                         offset=off, strides=(stride, 4))
    except (ValueError, TypeError):
        return None
    return np.array(arr, dtype=np.float64)   # 复制，避免持有整块 buffer


def mesh_primitives(path: str):
    """逐 primitive 产出 (pos[n,3], nrm[n,3] 或 None)。"""
    doc, binary = gltf_and_bin(path)
    out = []
    for mesh in doc.get("meshes") or []:
        for prim in mesh.get("primitives") or []:
            attrs = prim.get("attributes") or {}
            pos = _vec3(binary, _accessor(doc, attrs.get("POSITION")))
            if pos is None:
                continue
            nrm = _vec3(binary, _accessor(doc, attrs.get("NORMAL")))
            if nrm is not None and nrm.shape != pos.shape:
                nrm = None
            out.append((pos, nrm))
    return out


# ---------------------------------------------------------------- 结果落盘
def out_dir_of(workspace: str, algo: str) -> str:
    return os.path.join(workspace, OUT_ROOT, slug(algo))


def check_existing(workspace: str, algo: str, force: bool) -> None:
    target = os.path.join(out_dir_of(workspace, algo), f"{slug(algo)}.json")
    if os.path.exists(target) and not force:
        raise SystemExit(f"[11] 产物已存在：{target}（加 --force 覆盖）")


def emit(workspace: str, algo: str, inputs: dict, params: dict, summary: dict,
         items=None, gaps=None, usable: bool = True, reasons=None,
         markdown: str = "", extra_files=None) -> dict:
    """写 <out>/<slug>.json + .md + _done/<slug>.json，返回 doc。"""
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
    print(f"[11:{doc['algo']}] usable={s.get('usable')} "
          f"{json.dumps(doc.get('summary') or {}, ensure_ascii=False)[:300]}")
    return 0 if s.get("usable") else 3


# ---------------------------------------------------------------- CLI
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
    import argparse
    ap = argparse.ArgumentParser(description=description)
    ap.add_argument("--workspace", default=".", help="工作区根（默认当前目录）")
    ap.add_argument("--force", action="store_true", help="覆盖已有产物")
    args = ap.parse_args()
    if resolve_skills_root(args.workspace) is None:
        print(f"[11] 找不到技能根（既无 CARDO_SKILLS_ROOT，工作区里也没有 Skills/）："
              f"{args.workspace}", file=sys.stderr)
        raise SystemExit(2)
    check_existing(args.workspace, algo, args.force)
    return args
