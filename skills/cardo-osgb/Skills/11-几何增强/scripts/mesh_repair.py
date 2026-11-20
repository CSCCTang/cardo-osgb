#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""11 · 几何增强 · mesh.repair：b3dm 网格补洞诊断 + 副本修复（不回写模型包）。

输入：过程文件/03-OSGB转3DTiles/转换产物/cardo-lowair-model.json 的 artifacts[].path（b3dm）。
输出：过程文件/11-几何增强/mesh-repair/ 下 JSON + Markdown + 修复副本 repaired/
     （repaired/ 仅诊断用，可随时删除；原始 b3dm 一个字节都不改）。
口径（规则/输出契约.md §5.6）：
  - 每个 b3dm 取出内嵌 GLB，trimesh 只读加载；
  - 合并重复顶点后统计边界边（被 1 个面使用）、非流形边（被 >2 个面使用）、孔洞（边界边闭环数 E-V+C）；
  - 在内存副本上 trimesh.repair.fill_holes(use_fan=True) 补洞，报告补洞前后三角面数与残余孔洞；
  - 修复副本写成 GLB 落 repaired/，供人工复核，不参与后续流水线。
退出码：0 产出且 usable；2 缺模型/无 b3dm；3 产出但 usable=false。
"""
from __future__ import annotations

import io
import json
import os
import shutil
import struct
import sys

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import numpy as np  # noqa: E402

import common  # noqa: E402

ALGO = "mesh.repair"
SLUG = common.slug(ALGO)
DONE_REL = os.path.join(common.OUT_ROOT, "_done", f"{SLUG}.json")
SOURCE_NAME = "trimesh 网格修复（MIT）"
SOURCE_LINK = "https://github.com/mikedh/trimesh"


def glb_bytes(path: str) -> bytes:
    """b3dm → 内嵌 GLB 字节；只读，不改原文件。"""
    raw = open(path, "rb").read()
    if raw[:4] != b"b3dm" or len(raw) < 28:
        raise ValueError("非 b3dm 或头不足 28 B")
    declared = struct.unpack_from("<I", raw, 8)[0]
    if declared != len(raw):
        raise ValueError(f"声明 {declared} B ≠ 实际 {len(raw)} B")
    ft_json, ft_bin, bt_json, bt_bin = struct.unpack_from("<4I", raw, 12)
    off = 28 + ft_json + ft_bin + bt_json + bt_bin
    if off >= len(raw):
        raise ValueError("b3dm 头偏移越界（无可读 GLB）")
    glb = raw[off:]
    if glb[:4] != b"glTF" or len(glb) < 20:
        raise ValueError("b3dm 内无 GLB")
    return glb


def _find(parent: dict, x: int) -> int:
    while parent[x] != x:
        parent[x] = parent[parent[x]]
        x = parent[x]
    return x


def edge_stats(mesh) -> dict:
    """边界边 / 非流形边 / 孔洞环数（孔洞 = 边界边图的独立环数 E-V+C）。"""
    if len(mesh.faces) == 0 or len(mesh.edges) == 0:
        return {"boundaryEdgeCount": 0, "nonManifoldEdgeCount": 0, "holeCount": 0}
    edges = np.sort(np.asarray(mesh.edges, dtype=np.int64), axis=1)
    uniq, counts = np.unique(edges, axis=0, return_counts=True)
    boundary = uniq[counts == 1]
    bcount = int(boundary.shape[0])
    nmcount = int((counts > 2).sum())
    holes = 0
    if bcount:
        parent = {}
        for a, b in boundary:
            a, b = int(a), int(b)
            parent.setdefault(a, a)
            parent.setdefault(b, b)
            ra, rb = _find(parent, a), _find(parent, b)
            if ra != rb:
                parent[ra] = rb
        verts = len(parent)
        comps = len({_find(parent, k) for k in parent})
        holes = max(0, bcount - verts + comps)   # 闭环才有孔洞；开放链记 0
    return {"boundaryEdgeCount": bcount, "nonManifoldEdgeCount": nmcount, "holeCount": holes}


def safe_name(rel: str) -> str:
    stem = rel.replace("\\", "/").rsplit("/", 1)[-1]
    stem = os.path.splitext(stem)[0]
    prefix = rel.replace("\\", "/").replace("/", "__").rsplit("__", 1)[0]
    name = f"{prefix}__{stem}" if prefix else stem
    return name.replace(".", "_") + ".glb"


def gap(code: str, what: str, why: str, need: str) -> dict:
    return {"code": code, "what": what, "why": why, "need": need}


def render_markdown(items: list, summary: dict, params: dict, reasons: list) -> str:
    lines = [
        "# 11 · 几何增强 · 网格补洞诊断（mesh.repair）",
        "",
        "## 方法",
        "",
        "逐个读取 b3dm 内嵌 GLB（只读），trimesh 合并重复顶点后统计：",
        "",
        "- **边界边**：只被 1 个三角面使用的边；",
        "- **非流形边**：被 >2 个三角面共享的边；",
        "- **孔洞**：边界边图的独立闭环数（`E − V + C`，开放链不记为孔洞）；",
        "- **补洞**：在内存副本上 `trimesh.repair.fill_holes(use_fan=True)`，"
        "报告补洞前后三角面数与残余孔洞。",
        "",
        "**原始模型包未被修改**；修复副本仅写在 `repaired/`，供诊断复核，可随时删除。",
        "",
        "## 参数",
        "",
        f"- 顶点合并：{params['vertexMerge']}（trimesh `process=True`）",
        f"- 补洞：`{params['fillHoles']}`",
        f"- 孔洞口径：{params['holeDefinition']}",
        f"- 非流形口径：{params['nonManifoldDefinition']}",
        "",
        "## 汇总",
        "",
        "| 指标 | 数值 |",
        "|---|---:|",
    ]
    for key in ("tileCount", "loadFailedCount", "boundaryEdgeCount", "nonManifoldEdgeCount",
                "holeCount", "filledHoleCount", "trianglesBeforeFill", "trianglesAfterFill",
                "trianglesAdded", "tilesWithHoles", "repairedTileCount",
                "watertightTilesBefore", "watertightTilesAfter"):
        lines.append(f"| {key} | {summary.get(key)} |")
    lines += ["", "## 逐瓦片结果", "",
              "| 瓦片 | 三角面(前) | 三角面(后) | 边界边 | 非流形边 | 孔洞 | 补洞数 | 残余孔洞 | 加载 |",
              "|---|---:|---:|---:|---:|---:|---:|---:|:---:|"]
    for it in items:
        lines.append(
            f"| {it['path']} | {it.get('trianglesBeforeFill')} | {it.get('trianglesAfterFill')} | "
            f"{it.get('boundaryEdgeCount')} | {it.get('nonManifoldEdgeCount')} | "
            f"{it.get('holeCount')} | {it.get('filledHoleCount')} | {it.get('holeCountAfter')} | "
            f"{'ok' if it.get('loadOk') else 'fail'} |")
    lines += ["", "## 局限", ""]
    if reasons:
        lines += [f"- usable=false：{r}" for r in reasons]
    lines += [
        "- 瓦片是按 3DTiles 空间切块裁出来的，**切边处的边界边是正常开放边**，"
        "不能据此判定原始模型有孔洞；真实孔洞需回到整块 OSGB/LOD 拓扑核查。",
        "- `fill_holes(use_fan=True)` 对大孔洞用扇形三角化，非凸孔会自交/失真，"
        "副本只用于诊断，**不可回写模型包**。",
        "- 非流形边未被修复（`fill_holes` 只补边界环）；需网格重拓扑或专业修模工具。",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    # 幂等：已完成（exitCode=0）直接结束，不改任何文件
    done_path = os.path.join(DONE_REL)
    if os.path.isfile(done_path):
        try:
            done = common.read_json(done_path)
        except (OSError, ValueError):
            done = {}
        if done.get("exitCode") == 0:
            print(f"[11:{ALGO}] 已完成（{DONE_REL} exitCode=0），跳过")
            return 0

    args = common.parse_args(ALGO, "11 · 几何增强 · 网格补洞诊断 + 副本修复")
    workspace = os.path.abspath(args.workspace)

    try:
        import trimesh
    except ImportError as exc:
        trimesh = None
        trimesh_error = f"trimesh 不可用：{exc}"
    else:
        trimesh_error = ""

    model = common.load_model(args.workspace)
    files = common.b3dm_files(args.workspace, model)
    if not files:
        print(f"[11:{ALGO}] 模型契约无 b3dm 产物（{common.MODEL_REL}）", file=sys.stderr)
        return 2

    out_dir = common.out_dir_of(workspace, ALGO)
    repaired_dir = os.path.join(out_dir, "repaired")
    for stale in (f"{SLUG}.json", f"{SLUG}.md"):
        path = os.path.join(out_dir, stale)
        if os.path.isfile(path):
            os.remove(path)
    if os.path.isdir(repaired_dir):
        shutil.rmtree(repaired_dir)

    params = {
        "lib": f"trimesh {getattr(trimesh, '__version__', '?')}" if trimesh else "trimesh",
        "vertexMerge": True,
        "fillHoles": "trimesh.repair.fill_holes(use_fan=True)",
        "holeDefinition": "边界边连通图独立环数 E-V+C（开放链不记孔洞）",
        "nonManifoldDefinition": "被 >2 个三角面共享的边",
        "repairedFormat": "GLB（仅副本）",
    }
    inputs = {"model": common.MODEL_REL.replace("\\", "/"), "b3dmCount": len(files)}

    items, totals, reasons = [], {
        "tileCount": 0, "loadFailedCount": 0, "boundaryEdgeCount": 0,
        "nonManifoldEdgeCount": 0, "holeCount": 0, "filledHoleCount": 0,
        "trianglesBeforeFill": 0, "trianglesAfterFill": 0, "trianglesAdded": 0,
        "tilesWithHoles": 0, "repairedTileCount": 0,
        "watertightTilesBefore": 0, "watertightTilesAfter": 0,
    }, []

    os.makedirs(repaired_dir, exist_ok=True)
    for rel, path in files:
        totals["tileCount"] += 1
        item = {"path": rel.replace("\\", "/"), "loadOk": False, "geometryCount": None,
                "trianglesRaw": None, "trianglesBeforeFill": None, "trianglesAfterFill": None,
                "trianglesAdded": None, "boundaryEdgeCount": None, "nonManifoldEdgeCount": None,
                "holeCount": None, "holeCountAfter": None, "filledHoleCount": None,
                "watertightBefore": None, "watertightAfter": None, "repairedPath": None}
        if trimesh is None:
            item["error"] = trimesh_error
            items.append(item)
            continue
        try:
            glb = glb_bytes(path)
            scene = trimesh.load(io.BytesIO(glb), file_type="glb", force="scene", process=False)
            geometries = list(getattr(scene, "geometry", {}).values()) if hasattr(scene, "geometry") else []
            mesh = scene.to_mesh()
            item["geometryCount"] = len(geometries)
            item["trianglesRaw"] = int(len(mesh.faces))
            # 合并重复顶点后再统计拓扑（GLB 常见按面拆分顶点，不合并会把所有边当边界）
            work = trimesh.Trimesh(vertices=mesh.vertices, faces=mesh.faces, process=True)
            item["loadOk"] = True
        except Exception as exc:  # noqa: BLE001  逐瓦片失败要继续跑完其余瓦片
            item["error"] = f"{type(exc).__name__}: {exc}"
            totals["loadFailedCount"] += 1
            items.append(item)
            continue

        before = edge_stats(work)
        item["trianglesBeforeFill"] = int(len(work.faces))
        item["boundaryEdgeCount"] = before["boundaryEdgeCount"]
        item["nonManifoldEdgeCount"] = before["nonManifoldEdgeCount"]
        item["holeCount"] = before["holeCount"]
        item["watertightBefore"] = bool(work.is_watertight)

        if before["holeCount"] > 0:
            try:
                trimesh.repair.fill_holes(work, use_fan=True)
            except Exception as exc:  # noqa: BLE001
                item["fillError"] = f"{type(exc).__name__}: {exc}"
        after = edge_stats(work)
        item["trianglesAfterFill"] = int(len(work.faces))
        item["trianglesAdded"] = item["trianglesAfterFill"] - item["trianglesBeforeFill"]
        item["holeCountAfter"] = after["holeCount"]
        item["filledHoleCount"] = max(0, item["holeCount"] - after["holeCount"])
        item["watertightAfter"] = bool(work.is_watertight)

        for key in ("boundaryEdgeCount", "nonManifoldEdgeCount", "holeCount", "filledHoleCount",
                    "trianglesBeforeFill", "trianglesAfterFill", "trianglesAdded"):
            totals[key] += int(item[key] or 0)
        if item["holeCount"]:
            totals["tilesWithHoles"] += 1
        if item["watertightBefore"]:
            totals["watertightTilesBefore"] += 1
        if item["watertightAfter"]:
            totals["watertightTilesAfter"] += 1

        if item["filledHoleCount"] > 0:
            try:
                out_path = os.path.join(repaired_dir, safe_name(rel))
                with open(out_path, "wb") as fh:
                    fh.write(trimesh.exchange.gltf.export_glb(work))
                item["repairedPath"] = os.path.relpath(out_path, workspace).replace("\\", "/")
                totals["repairedTileCount"] += 1
            except Exception as exc:  # noqa: BLE001
                item["repairExportError"] = f"{type(exc).__name__}: {exc}"
        items.append(item)

    if trimesh is None:
        reasons.append(trimesh_error)
    if totals["loadFailedCount"]:
        reasons.append(f"{totals['loadFailedCount']}/{totals['tileCount']} 个 b3dm 无法被 trimesh 加载")
    usable = not reasons

    gaps = [
        gap("ENH-GAP-111",
            "边界边计数含瓦片裁切产生的合法开放边",
            "3DTiles 按空间切块裁网格，块边必然出现只被 1 个面使用的边，与真实孔洞不同源",
            "要判定真实孔洞需回到整块 OSGB/LOD 全局拓扑或原始重建网格核查"),
        gap("ENH-GAP-112",
            "大孔洞用扇形三角化补，几何仅近似有效",
            "fill_holes(use_fan=True) 对非凸孔会自交/重叠，补洞结果不代表正确曲面",
            "副本仅供诊断，需网格重拓扑或专业修模工具后才可能回填"),
        gap("ENH-GAP-113",
            "非流形边未修复",
            "trimesh.repair.fill_holes 只补边界闭环，不拆分/合并被 >2 个面共享的边",
            "需网格布尔清理或重拓扑工具处理非流形结构"),
    ]

    md = render_markdown(items, totals, params, reasons)
    doc = common.emit(args.workspace, ALGO, inputs, params, totals, items=items, gaps=gaps,
                      usable=usable, reasons=reasons, markdown=md)

    patch = {
        "id": ALGO,
        "status": "ready" if usable else "blocked",
        "step": "11",
        "impl": "自实现：trimesh 只读加载 b3dm 内嵌 GLB，统计边界边/非流形边/孔洞，"
                "内存副本 fill_holes 后导出 repaired/",
        "evidence": "Skills/11-几何增强/scripts/mesh_repair.py",
        "sourceName": SOURCE_NAME,
        "sourceLink": SOURCE_LINK,
        "license": "MIT",
        "plain": "诊断每个瓦片的边界边/非流形边/孔洞，副本补洞看面数变化，不改模型包。",
    }
    common.write_json(os.path.join(args.workspace, common.OUT_ROOT, "_patches", f"{SLUG}.json"),
                      patch)
    return common.finish(doc)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
