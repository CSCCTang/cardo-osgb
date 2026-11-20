#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""13 · 矢量增强 · common 的无 numpy 兜底实现。

只在 `import common` 因本机未安装 numpy 失败时启用；接口、落盘格式与
`scripts/common.py` 保持一致（SCHEMA / OUT_ROOT / emit / parse_args / finish）。
不安装任何第三方包，纯 stdlib 读取 09 的 `栅格.npz`（zip + npy 头解析）。
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
OUT_ROOT = os.path.join("过程文件", "13-矢量增强")
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
        raise SystemExit(f"[13] 缺模型契约：{MODEL_REL}（先跑 03/05）")
    return read_json(path)


def load_origin(workspace: str):
    """EPSG:4544 全局原点 [x, y]；缺失返回 None（不猜）。"""
    return (load_model(workspace).get("crs") or {}).get("origin")


# ---------------------------------------------------------------- npy / npz
def _read_npy(raw: bytes):
    """最小 .npy 读取：返回 (Python 列表, shape)。支持 <f8/<f4/|b1/<i4。"""
    if raw[:6] != b"\x93NUMPY":
        raise ValueError("不是 .npy")
    major = raw[6]
    if major == 1:
        hlen = struct.unpack_from("<H", raw, 8)[0]
        hstart = 10
    else:                                   # v2/v3：uint32 头长度
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
    raise ValueError(f"不支持的 dtype：{dtype}")


def load_grid(workspace: str) -> dict:
    """纯 stdlib 读 09 栅格；返回列表 + shape/nx/ny（与 common 的 dict 键兼容）。"""
    path = os.path.join(workspace, GRID_REL)
    if not os.path.isfile(path):
        raise SystemExit(f"[13] 缺栅格：{GRID_REL}（先跑 09）")
    with zipfile.ZipFile(path) as zf:
        out, shape = {}, None
        for name in zf.namelist():
            key = name[:-4] if name.endswith(".npy") else name
            vals, shp = _read_npy(zf.read(name))
            out[key] = vals
            if shape is None and len(shp) == 2:
                shape = shp
    if shape is None:
        raise ValueError("栅格.npz 没有二维数组")
    out.update({"cell": float(out["cell"][0]), "x0": float(out["x0"][0]),
                "y0": float(out["y0"][0]), "shape": tuple(int(v) for v in shape),
                "nx": int(shape[0]), "ny": int(shape[1])})
    return out


# ---------------------------------------------------------------- 结果落盘
def out_dir_of(workspace: str, algo: str) -> str:
    return os.path.join(workspace, OUT_ROOT, slug(algo))


def check_existing(workspace: str, algo: str, force: bool) -> None:
    target = os.path.join(out_dir_of(workspace, algo), f"{slug(algo)}.json")
    if os.path.exists(target) and not force:
        raise SystemExit(f"[13] 产物已存在：{target}（加 --force 覆盖）")


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
    print(f"[13:{doc['algo']}] usable={s.get('usable')} "
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
        print(f"[13] 找不到技能根（既无 CARDO_SKILLS_ROOT，工作区里也没有 Skills/）："
              f"{args.workspace}", file=sys.stderr)
        raise SystemExit(2)
    check_existing(args.workspace, algo, args.force)
    return args
