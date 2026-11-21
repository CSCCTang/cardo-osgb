#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""08 · 验证与边界：**独立实现**复算指标 + 基线对账 + 输入/成果扩展名扫描。

独立性：本脚本不 import `run_indicators.py` / `tiles_inspect.py`，也不读它们的中间结果做输入；
自己用 `struct` 逐个顶点解析 b3dm→GLB→POSITION，自己实现网格/地面/连通分量/分位，
只按 `规则/指标口径.md` 的**方法描述**复算，再与 06 的 现状指标.json 对账。

口径：复算取默认 cell=1.0 m（主口径）；区间用「点值落在声明区间内 + 全局 P5 地面 +
层高 2.8/3.3 极值落在区间内」做独立佐证（不是逐格重跑三档，避免复刻实现细节）。

基线：`记忆与规则/验证基线-CardoOSGB.json` 存在则逐项对账；整份删除后只做结构自洽（跳过项不算失败）。

边界：输入文件 / 成果文件 / Skills 的扩展名白名单扫描 + 无盘符路径 + 无 __pycache__。
输出：过程文件/08-验证与成稿/{验证报告.json, 独立验证结果.json}
退出码：0 全部通过（或基线缺失仅跳过）；2 输入缺失；3 有失败项。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import struct
import sys
from datetime import datetime

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA = "cardo/lowair-verify/v1"
IND_SCHEMA = "cardo/lowair-indicators/v1"
MODEL_DEFAULT = os.path.join("过程文件", "03-OSGB转3DTiles", "转换产物", "cardo-lowair-model.json")
IND_DEFAULT = os.path.join("过程文件", "06-2.5D网格指标", "现状指标.json")
HEALTH_DEFAULT = os.path.join("过程文件", "05-模型体检", "体检.json")
# 基线按包命名空间隔离：四包可能同机共存，文件名带包名 + 内容带 package，
# 避免 A 包的基线被 B 包当成自己的。
PACKAGE = "CardoOSGB"
BASELINE_DEFAULT = os.path.join("记忆与规则", "验证基线-CardoOSGB.json")
OUT_DEFAULT = os.path.join("过程文件", "08-验证与成稿")

CELL_M = 1.0
RADIUS_M = 15.0
LOCAL_Q = 10.0
GROUND_PCT = 5.0
THR_M = 3.0
FLOOR_M = 3.0
NORMAL_Z = 0.5
NEG_INF = -1e30


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


INPUT_EXT = {".osgb", ".zip", ".xml", ".json", ".txt", ".xlsx", ".docx"}
OUTPUT_EXT = {".xlsx", ".docx", ".pdf"}
SKILL_EXT = {".py", ".md", ".yaml", ".json"}   # .json：00 的算法注册表是技能资产（CLI 直接读）
# tools/ 是第三方工具自包含目录：允许其自带任意数据/二进制，但**禁止**混入模型或转换产物
MODEL_EXT = {".osgb", ".b3dm", ".glb", ".gltf", ".3dtiles", ".3tz", ".las", ".laz", ".ply", ".obj"}
DRIVE_RE = re.compile(r"(^|[^A-Za-z0-9])[A-Za-z]:[\\/]")
# 模型浏览是「一成果一子目录」的自足浏览包：引擎（页面/脚本/样式）、三维瓦片、栅格、矢量、
# 包内静态服务（caddy.exe）各有自己的扩展名，不能套 docx/xlsx/pdf 的白名单。
BROWSE_REL = "成品版/模型浏览/"
BROWSE_REL_ROOT = "成果文件/" + BROWSE_REL
BROWSE_EXT = {".xlsx", ".docx", ".pdf",
              ".html", ".js", ".cjs", ".css", ".json", ".txt", ".bat", ".py", ".md", ".xml",
              ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
              ".b3dm", ".glb", ".gltf", ".bin", ".wasm", ".exe", ".geojson"}
BROWSE_TEXT_EXT = {".html", ".js", ".cjs", ".css", ".json", ".txt", ".bat", ".py", ".md", ".xml"}


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


# ---------- 独立解析（只读标准容器，不解析 OSGB 字节） ----------

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


def accessor_layout(doc, index):
    """(字节偏移, 步长, 顶点数) 或 None；只认 float32 VEC3。"""
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


def accumulate_b3dm(path, grid_all, grid_top, bbox, normal_z=NORMAL_Z):
    """独立流式累加：同一次遍历填 `all`（全部顶点）与 `top`（NORMAL.z≥阈值）两套 1.0 m 网格。"""
    doc, binary = gltf_and_bin(path)
    accessors, views = doc.get("accessors") or [], doc.get("bufferViews") or []
    x0, y0, x1, y1 = bbox["min"][0], bbox["min"][1], bbox["max"][0], bbox["max"][1]
    nx = max(1, int(math.ceil((x1 - x0) / CELL_M)))
    ny = max(1, int(math.ceil((y1 - y0) / CELL_M)))
    stats = {"vertices": 0, "topVertices": 0, "triangles": 0, "images": 0,
             "missingImages": 0, "textureBytes": 0, "outsideBbox": 0}
    for mesh in doc.get("meshes") or []:
        for prim in mesh.get("primitives") or []:
            attrs = prim.get("attributes") or {}
            pos = accessor_layout(doc, attrs.get("POSITION"))
            if not pos or not binary:
                continue
            offset, stride, count = pos
            if count <= 0 or offset + (count - 1) * stride + 12 > len(binary):
                continue
            nrm = accessor_layout(doc, attrs.get("NORMAL"))
            if nrm and nrm[2] != count:
                nrm = None
            for i in range(count):
                x, y, z = struct.unpack_from("<3f", binary, offset + i * stride)
                stats["vertices"] += 1
                if not (x0 <= x <= x1 and y0 <= y <= y1):
                    stats["outsideBbox"] += 1
                    continue
                key = (min(nx - 1, int((x - x0) / CELL_M)), min(ny - 1, int((y - y0) / CELL_M)))
                if z > grid_all.get(key, NEG_INF):
                    grid_all[key] = z
                if nrm:
                    nz = struct.unpack_from("<3f", binary, nrm[0] + i * nrm[1])[2]
                    if nz >= normal_z:
                        stats["topVertices"] += 1
                        if z > grid_top.get(key, NEG_INF):
                            grid_top[key] = z
            idx = prim.get("indices")
            stats["triangles"] += (accessors[idx].get("count", 0)
                                   if isinstance(idx, int) and idx < len(accessors) else count) // 3
    images = doc.get("images") or []
    stats["images"] = len(images)
    stats["missingImages"] = sum(
        1 for image in images
        if not (isinstance(image.get("bufferView"), int) and 0 <= image["bufferView"] < len(views)
                and views[image["bufferView"]].get("byteLength", 0) > 0)
        and not image.get("uri"))
    stats["textureBytes"] = sum(views[image["bufferView"]].get("byteLength", 0) for image in images
                                if isinstance(image.get("bufferView"), int)
                                and 0 <= image["bufferView"] < len(views))
    return stats


def closing(keys, domain):
    """独立实现的二值闭运算（4 邻域 ×1 格，限定在观测格内）。"""
    def around(cell):
        return ((cell[0] + 1, cell[1]), (cell[0] - 1, cell[1]),
                (cell[0], cell[1] + 1), (cell[0], cell[1] - 1))

    grown = set(keys)
    for cell in keys:
        grown.update(nb for nb in around(cell) if nb in domain)
    return {cell for cell in grown if all(nb in grown for nb in around(cell))}


def percentile(values, p):
    if not values:
        return None
    ordered = sorted(values)
    k = int(round((len(ordered) - 1) * p / 100.0))
    return ordered[max(0, min(len(ordered) - 1, k))]


def ground_local(grid, cell):
    """15 m 邻域内观测格 z 的 P10（全窗口，不抽样）。"""
    step = max(1, int(round(RADIUS_M / cell)))
    out = {}
    for (ix, iy), z in grid.items():
        window = [grid[(ix + dx, iy + dy)] for dx in range(-step, step + 1)
                  for dy in range(-step, step + 1) if (ix + dx, iy + dy) in grid]
        out[(ix, iy)] = percentile(window, LOCAL_Q) if window else z
    return out


def component_count(keys):
    remain, count = set(keys), 0
    while remain:
        count += 1
        stack = [remain.pop()]
        while stack:
            ix, iy = stack.pop()
            for nb in ((ix + 1, iy), (ix - 1, iy), (ix, iy + 1), (ix, iy - 1)):
                if nb in remain:
                    remain.remove(nb)
                    stack.append(nb)
    return count


def recompute(model: dict, model_dir: str) -> dict:
    bbox = model["geometry"]["bboxLocal"]
    rels = [a["path"] for a in model.get("artifacts") or [] if str(a.get("path", "")).lower().endswith(".b3dm")]
    grid_all, grid_top = {}, {}
    total = {"vertices": 0, "topVertices": 0, "triangles": 0, "images": 0,
             "missingImages": 0, "textureBytes": 0, "outsideBbox": 0}
    failed = []
    for rel in rels:
        try:
            stats = accumulate_b3dm(os.path.join(model_dir, rel), grid_all, grid_top, bbox)
        except (OSError, ValueError, KeyError, struct.error, json.JSONDecodeError) as exc:
            failed.append({"path": rel, "error": f"{type(exc).__name__}: {exc}"})
            continue
        for key, value in stats.items():
            total[key] += value
    if not grid_all:
        raise ValueError("网格为空：无 POSITION 顶点")
    normal_share = (total["topVertices"] / total["vertices"]) if total["vertices"] else 0.0
    top_cell_share = (len(grid_top) / len(grid_all)) if grid_all else 0.0
    mode = "top" if (normal_share >= 0.3 and top_cell_share >= 0.9) else "all"
    grid = grid_top if mode == "top" else grid_all
    zs = list(grid.values())
    ground = percentile(zs, GROUND_PCT)
    local = ground_local(grid, CELL_M)
    mask = [k for k, z in grid.items() if z - local[k] > THR_M]
    closed = closing(mask, set(grid))
    heights = [grid[k] - local[k] for k in mask]
    area = (bbox["max"][0] - bbox["min"][0]) * (bbox["max"][1] - bbox["min"][1])
    if area <= 0:
        raise ValueError("bbox 投影面积非正")
    far = {}
    for floor in (FLOOR_M, 2.8, 3.3):
        far[floor] = sum(CELL_M * CELL_M * math.ceil((grid[k] - local[k]) / floor) for k in mask) / area
    return {
        "b3dm": len(rels), "parsed": len(rels) - len(failed), "failed": failed,
        "vertices": total["vertices"], "triangles": total["triangles"],
        "images": total["images"], "missingImages": total["missingImages"],
        "textureBytes": total["textureBytes"], "outsideBbox": total["outsideBbox"],
        "surface": {"mode": mode, "normalsVertexShare": round(normal_share, 4),
                    "topCellShare": round(top_cell_share, 4),
                    "topVertices": total["topVertices"]},
        "validCells": len(zs), "maskCells": len(mask), "closedMaskCells": len(closed),
        "rawComponents": component_count(mask), "closedComponents": component_count(closed),
        "areaM2": area, "groundZ": ground,
        "coverage": len(mask) / len(zs), "count": component_count(closed),
        "heightP50": percentile(heights, 50), "heightP90": percentile(heights, 90),
        "far": far,
        "maskCoverageGlobal": sum(1 for k, z in grid.items() if z - ground > THR_M) / len(zs),
        "cellM": CELL_M,
    }


# ---------- 对账 ----------

def check(checks, cid, name, ok, detail, skipped=False):
    checks.append({"id": cid, "name": name, "status": ("skip" if skipped else ("pass" if ok else "fail")),
                   "detail": detail})


def near(value, expected, tol) -> bool:
    if value is None or expected is None:
        return value == expected
    return abs(float(value) - float(expected)) <= tol


def compare_with_indicators(rec: dict, ind: dict, checks: list) -> None:
    metrics = {m["id"]: m for m in ind.get("metrics") or []}

    def check_metric(mid, label, got, tol):
        item = metrics.get(mid)
        if item is None:
            check(checks, f"V-{label}", f"复算 {mid}", False, "现状指标.json 缺该指标")
            return
        check(checks, f"V-{label}", f"复算 {mid}", near(got, item["value"], tol),
              f"独立复算 {got} / 06 声明 {item['value']}（容差 {tol:g}）")
        low, high = item["range"][0], item["range"][1]
        if low is None or high is None:
            check(checks, f"V-{label}r", f"{mid} 区间占位", item["value"] is None,
                  f"value={item['value']} / range={item['range']}")
        else:
            inside = low - 1e-9 <= float(got) <= high + 1e-9
            check(checks, f"V-{label}r", f"{mid} 落在声明区间内", inside,
                  f"复算 {got} ∈ [{low}, {high}]")

    check_metric("building_coverage_ratio", "01", round(rec["coverage"], 6), 0.002)
    check_metric("building_count", "02", rec["count"], 0)
    check_metric("building_height_p50", "03", round(rec["heightP50"], 2), 0.011)
    check_metric("building_height_p90", "04", round(rec["heightP90"], 2), 0.011)
    check_metric("floor_area_ratio_est", "05", round(rec["far"][FLOOR_M], 4), 0.0011)

    # 区间佐证：全局 P5 地面覆盖率、层高 2.8/3.3 的容积率都应落在声明区间内
    cov = metrics.get("building_coverage_ratio") or {}
    low, high = (cov.get("range") or [None, None])[:2]
    ok = low is not None and high is not None and low - 1e-6 <= rec["maskCoverageGlobal"] <= high + 1e-6
    check(checks, "V-06", "区间佐证：全局 P5 地面覆盖率在区间内", ok,
          f"全局 P5 覆盖率 {rec['maskCoverageGlobal']:.6f} ∈ [{low}, {high}]")
    far = metrics.get("floor_area_ratio_est") or {}
    low, high = (far.get("range") or [None, None])[:2]
    lo2, hi2 = rec["far"][2.8], rec["far"][3.3]
    ok = (low is not None and high is not None
          and low - 1e-6 <= min(lo2, hi2) and max(lo2, hi2) <= high + 1e-6)
    check(checks, "V-07", "区间佐证：层高 2.8/3.3 容积率在区间内", ok,
          f"FAR(2.8)={lo2:.4f} / FAR(3.3)={hi2:.4f} 均应 ∈ [{low}, {high}]")

    scope = ind.get("scope") or {}
    check(checks, "V-08", "范围/格数与 06 一致",
          near(scope.get("areaM2"), round(rec["areaM2"], 2), 0.02)
          and scope.get("validCells") == rec["validCells"]
          and scope.get("maskCells") == rec["maskCells"]
          and near(scope.get("groundZ"), round(rec["groundZ"], 3), 0.002),
          f"独立 area {rec['areaM2']:.2f} / 有效格 {rec['validCells']} / mask {rec['maskCells']} / "
          f"ground {rec['groundZ']:.3f}；06 area {scope.get('areaM2')} / "
          f"{scope.get('validCells')} / {scope.get('maskCells')} / {scope.get('groundZ')}")
    quality = ind.get("quality") or {}
    check(checks, "V-09", "06 记录 b3dm/顶点/三角面与独立解析一致",
          quality.get("parsedTiles") == rec["parsed"] and quality.get("vertices") == rec["vertices"],
          f"独立 b3dm {rec['parsed']} / 顶点 {rec['vertices']}；06 {quality.get('parsedTiles')} / "
          f"{quality.get('vertices')}")
    check(checks, "V-10", "无解析失败的 b3dm", not rec["failed"],
          f"失败 {len(rec['failed'])} 个" + (f"：{rec['failed'][:2]}" if rec["failed"] else ""))
    def texture_proxy_ok(mid):
        """纹理代理指标：有值必须区间包主值 + 置信度/等级/口径合规；无值必须 null + IND-GAP-001。"""
        metric = metrics.get(mid) or {}
        value = metric.get("value")
        gaps = {g.get("code") for g in (ind.get("gaps") or [])}
        if value is None:
            return metric.get("grade") == "placeholder" and "IND-GAP-001" in gaps
        rng = list(metric.get("range") or [])
        lo, hi = (rng + [None, None])[:2]
        method = metric.get("method") or ""
        return (isinstance(value, (int, float))
                and isinstance(lo, (int, float)) and isinstance(hi, (int, float))
                and lo <= value <= hi
                and metric.get("confidence") in ("low", "medium")
                and metric.get("grade") == "estimated"
                and any(token in method for token in ("纹理", "植被", "UV"))
                and "IND-GAP-001" in gaps)

    check(checks, "V-11", "纹理代理指标：有值则区间包主值且置信度/等级合规，无值则 null+gap",
          texture_proxy_ok("greenery_ratio") and texture_proxy_ok("road_ratio"),
          f"greenery={metrics.get('greenery_ratio', {}).get('value')} / "
          f"road={metrics.get('road_ratio', {}).get('value')}")
    params = ind.get("params") or {}
    diag = ind.get("diagnostics") or {}
    check(checks, "V-12", "表面口径与法线覆盖复算一致",
          params.get("surface") == rec["surface"]["mode"]
          and near(params.get("normalsVertexShare"), rec["surface"]["normalsVertexShare"], 0.01)
          and near(params.get("topCellShare"), rec["surface"]["topCellShare"], 0.01),
          f"06 surface {params.get('surface')}（法线 {params.get('normalsVertexShare')} / 顶面格 "
          f"{params.get('topCellShare')}）；独立 {rec['surface']['mode']}（{rec['surface']['normalsVertexShare']} / "
          f"{rec['surface']['topCellShare']}）")
    mask_diag = diag.get("mask") or {}
    check(checks, "V-13", "栋数闭运算口径可复算",
          params.get("countClosing") == "4-neighborhood-1cell"
          and mask_diag.get("rawComponents") == rec["rawComponents"]
          and mask_diag.get("closedComponents") == rec["closedComponents"],
          f"06 原始分量 {mask_diag.get('rawComponents')} → 闭运算 {mask_diag.get('closedComponents')}；"
          f"独立 {rec['rawComponents']} → {rec['closedComponents']}")


def baseline_from(rec: dict, ind: dict, model: dict) -> dict:
    """由本次运行的独立复算结果构造基线内容。

    字段与 compare_baseline 的对账口径一一对应（metrics / tiles / scope / registration）。
    """
    values = {m["id"]: m.get("value") for m in ind.get("metrics") or []}
    return {
        "schema": "cardo/lowair-baseline/v1",
        "package": PACKAGE,
        "recordedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "model": (model.get("source") or {}).get("name"),
        "metrics": values,
        "tiles": {
            "b3dmCount": (model.get("tiles") or {}).get("b3dmCount"),
            "triangles": (model.get("geometry") or {}).get("triangles"),
            "vertices": (model.get("geometry") or {}).get("vertices"),
        },
        "scope": {
            "areaM2": (ind.get("scope") or {}).get("areaM2"),
            "validCells": (ind.get("scope") or {}).get("validCells"),
        },
        "registration": {
            "verticalOffsetM": (model.get("georeference") or {}).get("verticalOffsetM"),
        },
    }


def compare_baseline(rec: dict, ind: dict, model: dict, baseline_path: str, checks: list,
                      mode: str = "auto") -> dict:
    """基线对账。mode ∈ auto / write / check / none。

    auto：基线不存在就写一份（首跑自举），存在则对账；
    write：强制重写；check：只对账不写；none：跳过。
    """
    if mode == "none":
        check(checks, "B-00", "基线对账", True, "mode=none（跳过）", skipped=True)
        return {"mode": "none", "status": "skipped", "reason": "按 --baseline none 跳过"}

    if mode == "write" or (mode == "auto" and not os.path.isfile(baseline_path)):
        os.makedirs(os.path.dirname(os.path.abspath(baseline_path)), exist_ok=True)
        write_json(baseline_path, baseline_from(rec, ind, model))
        check(checks, "B-00", "基线对账", True,
              f"基线已写入（{'重写' if mode == 'write' else '首跑自举'}）："
              f"{baseline_path.replace(chr(92), '/')}")
        return {"mode": mode, "status": "written",
                "source": baseline_path.replace("\\", "/"),
                "recordedAt": datetime.now().astimezone().isoformat(timespec="seconds")}

    if not os.path.isfile(baseline_path):
        check(checks, "B-00", "基线对账", True, f"基线不存在（{baseline_path.replace(chr(92), '/')}），跳过不算失败",
              skipped=True)
        return {"mode": mode, "status": "skipped", "reason": "验证基线-CardoOSGB.json 不存在（可整份删除，跳过项不算失败）"}
    baseline = read_json(baseline_path)
    if (baseline or {}).get("package") != PACKAGE:
        check(checks, "B-00", "基线对账", True,
              "非本包基线（package={}），按首跑跳过".format((baseline or {}).get("package")),
              skipped=True)
        return {"mode": mode, "status": "skipped", "reason": "foreign-baseline"}
    values = {m["id"]: m["value"] for m in ind.get("metrics") or []}
    metrics = baseline.get("metrics") or {}
    for mid, tol in (("building_coverage_ratio", 0.002), ("building_count", 0),
                     ("building_height_p50", 0.02), ("building_height_p90", 0.02),
                     ("floor_area_ratio_est", 0.002)):
        check(checks, f"B-{mid[:12]}", f"基线 {mid}", near(values.get(mid), metrics.get(mid), tol),
              f"本次 {values.get(mid)} / 基线 {metrics.get(mid)}（容差 {tol:g}）")
    tiles = baseline.get("tiles") or {}
    check(checks, "B-model", "基线 产物规模", 
          tiles.get("b3dmCount") == model["tiles"]["b3dmCount"]
          and tiles.get("triangles") == model["geometry"]["triangles"]
          and tiles.get("vertices") == model["geometry"]["vertices"],
          f"本次 b3dm {model['tiles']['b3dmCount']} / {model['geometry']['triangles']} 面 / "
          f"{model['geometry']['vertices']} 点；基线 {tiles.get('b3dmCount')} / "
          f"{tiles.get('triangles')} / {tiles.get('vertices')}")
    scope = baseline.get("scope") or {}
    check(checks, "B-scope", "基线 范围",
          near(scope.get("areaM2"), (ind.get("scope") or {}).get("areaM2"), 0.02)
          and scope.get("validCells") == (ind.get("scope") or {}).get("validCells"),
          f"本次 {ind.get('scope')} / 基线 {scope}")
    reg = baseline.get("registration") or {}
    check(checks, "B-reg", "基线 垂直修正量",
          near(reg.get("verticalOffsetM"), model["georeference"]["verticalOffsetM"], 0.01),
          f"本次 {model['georeference']['verticalOffsetM']} / 基线 {reg.get('verticalOffsetM')}（±0.01 m）")
    return {"status": "checked", "source": baseline_path.replace("\\", "/"),
            "recordedAt": baseline.get("recordedAt"), "model": baseline.get("model")}


def resolve_skills_root(workspace: str, given: str | None = None) -> str | None:
    """技能根在哪。

    顺序：显式参数 → CARDO_SKILLS_ROOT 环境变量 → 本脚本所在位置向上两级（技能包内嵌/外置都成立）
    → <工作区>/Skills。

    为什么不能只靠环境变量：交付包里技能包在工作区**之外**，工作区里没有 Skills/；
    而直接跑 pipeline.py（不经 开工.py）时环境变量可能没设。
    本脚本在 <技能根>/08-验证与成稿/scripts/ 下，所以 HERE/../.. 恒为技能根。
    """
    if given and os.path.isdir(given):
        return os.path.abspath(given)
    env = os.environ.get("CARDO_SKILLS_ROOT")
    if env and os.path.isdir(env):
        return os.path.abspath(env)
    derived = os.path.abspath(os.path.join(HERE, "..", ".."))
    if os.path.isdir(derived):
        return derived
    local = os.path.join(os.path.abspath(workspace), "Skills")
    return os.path.abspath(local) if os.path.isdir(local) else None


def boundary_scan(root: str, checks: list, skills_root: str | None = None) -> dict:
    """输入/成果/技能扩展名白名单 + 盘符路径 + __pycache__。"""
    violations, counts = [], {}
    for folder, allowed in (("输入文件", INPUT_EXT), ("成果文件", OUTPUT_EXT), ("Skills", SKILL_EXT)):
        base = skills_root if folder == "Skills" and skills_root else os.path.join(root, folder)
        counts[folder] = 0
        if not os.path.isdir(base):
            violations.append(f"{folder}/ 不存在")
            continue
        for dirpath, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d != "__pycache__"]   # 交给 E-04 单独判
            in_tools = "\\tools\\" in (os.path.abspath(dirpath) + "\\").replace("/", "\\").lower()
            for name in files:
                if name.endswith(".pyc"):
                    continue
                full = os.path.join(dirpath, name)
                if folder == "Skills" and skills_root:
                    # 技能链在工作区之外：前缀仍写 Skills/，保持报告口径一致
                    rel = "Skills/" + os.path.relpath(full, base).replace("\\", "/")
                else:
                    rel = os.path.relpath(full, root).replace("\\", "/")
                counts[folder] += 1
                ext = os.path.splitext(name)[1].lower()
                if in_tools:
                    if ext in MODEL_EXT:
                        violations.append(f"{rel}（tools/ 内不得放模型/转换产物）")
                    continue
                if folder == "成果文件" and rel.startswith(BROWSE_REL_ROOT):
                    # 模型浏览目录按自己的扩展名白名单判定；vendor/ 是第三方原样快照，
                    # 只查扩展名不扫盘符（与 07 打包时的自足性扫描同一口径）。
                    if ext not in BROWSE_EXT:
                        violations.append(f"{rel}（模型浏览内扩展名 {ext or '无'} 不在白名单）")
                    elif ext in BROWSE_TEXT_EXT and "/viewer/vendor/" not in rel:
                        text = open(full, encoding="utf-8", errors="replace").read()
                        if DRIVE_RE.search(text):
                            violations.append(f"{rel} 含盘符/绝对路径")
                    continue
                if ext not in allowed and not (
                        folder == "成果文件" and ext == ".json" and "/依据数据/" in rel):
                    violations.append(f"{rel}（扩展名 {ext or '无'} 不在白名单）")
                elif ext in SKILL_EXT:
                    text = open(full, encoding="utf-8", errors="replace").read()
                    if DRIVE_RE.search(text):
                        violations.append(f"{rel} 含盘符/绝对路径")
    skip_dirs = {".venv", "venv", ".trial", "node_modules", ".cardo"}
    # 便携 Python 运行时是**随包分发的只读原件**（<技能包>/runtime/），
    # 其 site-packages 自带 .pyc 属构建期产物，不是本次运行残留。
    # E-04 要管的是"工作区不得残留运行产生的字节码"，所以按**绝对路径**豁免它——
    # 不能按目录名豁免，否则会误伤用户在工作区自建的同名业务目录。
    # 该 runtime 另配了 sitecustomize.pyc，本身不再写字节码（见 runtime/README.txt）。
    #
    # 豁免路径按**技能根**定位（技能根的同级 runtime），不按本脚本的相对层级——
    # 后者在包被多套一层、或直接调 pipeline.py 时就会指错位置，把随包 runtime 误判成残留。
    runtime_dirs = set()
    if skills_root:
        pkg_dir = os.path.dirname(os.path.abspath(skills_root))   # 技能根的上一级 = 技能包目录
        runtime_dirs.add(os.path.join(pkg_dir, "runtime"))
    runtime_dirs.add(os.path.abspath(os.path.join(HERE, "..", "..", "..", "runtime")))
    runtime_dirs = {os.path.abspath(p) for p in runtime_dirs}
    pycache = []
    for dirpath, dirs, files in os.walk(root):
        here = os.path.abspath(dirpath)
        # 测试工作区常整包拷一份技能包（scaffold 产物），那份 runtime/ 也是随包原件：
        # 按「runtime 的同级有 PACKAGE_MANIFEST.json / Skills/」识别，一并豁免。
        if os.path.basename(here).lower() == "runtime":
            pkg = os.path.dirname(here)
            if (os.path.isfile(os.path.join(pkg, "PACKAGE_MANIFEST.json"))
                    or os.path.isdir(os.path.join(pkg, "Skills"))):
                runtime_dirs.add(here)
        if here in runtime_dirs:
            dirs[:] = []
            continue
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for name in dirs + files:
            if name == "__pycache__" or name.endswith(".pyc"):
                pycache.append(os.path.join(dirpath, name))
    violations.extend(os.path.relpath(p, root).replace("\\", "/") + "（__pycache__/.pyc）" for p in pycache)
    check(checks, "E-01", "输入文件扩展名白名单", not any("输入文件/" in v for v in violations),
          f"{counts.get('输入文件', 0)} 个文件；越界 {sum(1 for v in violations if v.startswith('输入文件/'))} 个")
    check(checks, "E-02", "成果文件扩展名白名单", not any("成果文件/" in v for v in violations),
          f"{counts.get('成果文件', 0)} 个文件；越界 {sum(1 for v in violations if v.startswith('成果文件/'))} 个")
    check(checks, "E-03", "Skills 扩展名白名单（tools/ 可含二进制）",
          not any(v.startswith("Skills/") for v in violations),
          f"{counts.get('Skills', 0)} 个文件；越界 "
          f"{sum(1 for v in violations if v.startswith('Skills/'))} 个")
    check(checks, "E-04", "无 __pycache__ / .pyc", not pycache, f"命中 {len(pycache)} 个")
    out_dir = os.path.join(root, "成果文件")
    names = []
    browse_files = []
    if os.path.isdir(out_dir):
        for dirpath, dirs, files in os.walk(out_dir):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in files:
                rel = os.path.relpath(os.path.join(dirpath, name), out_dir).replace("\\", "/")
                # 模型浏览是整目录交付的一件成果（自带引擎与瓦片，文件数随模型规模变化），
                # 不参与「九件」逐件清点；它的完整性由下面的必需项单独判。
                if rel.startswith(BROWSE_REL):
                    browse_files.append(rel)
                    continue
                names.append(rel)
    names.sort()
    browse_files.sort()
    expect = sorted([
        "成品版/模型体检报告/模型体检报告_成品版.docx",
        "成品版/模型体检报告/模型体检报告_成品版.pdf",
        "成品版/现状指标/现状指标_成品版.xlsx",
        "成品版/指标说明/指标说明_成品版.docx",
        "成品版/指标说明/指标说明_成品版.pdf",
        "内部件/模型体检报告/模型体检报告_溯源版.docx",
        "内部件/现状指标/现状指标_溯源版.xlsx",
        "内部件/现状指标/现状指标依据对照表.xlsx",
        "内部件/指标说明/指标说明_溯源版.docx",
    ])
    # 依据随包：对照表引用的两个指标源 JSON 必须落在 内部件/现状指标/依据数据/，
    # 这样单拷 成果文件/ 也不断链；除这 9 件 + 依据数据/*.json 外不许多文件。
    # 两份 PDF 是页码烘进版面的成品（docx 目录页码与之一致，以 PDF 为准）。
    bundle_need = ["内部件/现状指标/依据数据/现状指标.json"]
    # 扩展指标在第 15 步才产出：首跑 08 时还没有，只有它存在过才要求随包
    if os.path.isfile(os.path.join(root, "过程文件", "14-扩展指标", "扩展指标.json")):
        bundle_need.append("内部件/现状指标/依据数据/扩展指标.json")
    extra = [n for n in names if n not in expect]
    extra_ok = all(n.startswith("内部件/现状指标/依据数据/") and n.endswith(".json")
                   for n in extra)
    # 模型浏览包：按 07 生成的全包哈希清单 viewer/BUILD.json 逐件核对（文件数 + 每件
    # bytes/sha256 + 是否有清单外文件）；扩展名白名单只判「该不该出现在这里」，不免除逐件核对。
    build_rel = BROWSE_REL + "viewer/BUILD.json"
    build_path = os.path.join(out_dir, *build_rel.split("/"))
    browse_ok = False
    browse_note = f"；模型浏览 {len(browse_files)} 件"
    # 07 主动跳过浏览包时（缺 assets/viewer / 延后到重跑 / 旧包被占用）会留一份标记；
    # 此时不把浏览包当必需件。跳过原因写在标记里，报告直接带出来。
    viewer_skip_rel = os.path.join("过程文件", "07-成果导出", "模型浏览_未生成.json")
    viewer_skip = os.path.isfile(os.path.join(root, viewer_skip_rel))
    if viewer_skip:
        browse_ok = True
        reason = ""
        try:
            with open(os.path.join(root, viewer_skip_rel), encoding="utf-8") as fh:
                reason = str((json.load(fh) or {}).get("reason") or "")
        except (OSError, ValueError):
            pass
        browse_note += "，07 已说明跳过%s（已留痕）" % ("（%s）" % reason if reason else "")
    elif not os.path.isfile(build_path):
        browse_note += "，缺 viewer/BUILD.json 哈希清单"
    else:
        try:
            with open(build_path, encoding="utf-8") as fh:
                build_doc = json.load(fh)
        except (OSError, ValueError) as exc:
            build_doc = None
            browse_note += f"，viewer/BUILD.json 无法解析（{exc}）"
        if isinstance(build_doc, dict):
            listed_map = {}
            for item in (build_doc.get("files") or []):
                if not isinstance(item, dict):
                    continue
                rel_path = str(item.get("path") or "").replace("\\", "/")
                if rel_path:
                    listed_map[BROWSE_REL + rel_path] = item
            skip = {build_rel, BROWSE_REL + "viewer/BUILD-SHA256.txt",
                    BROWSE_REL + "server.pid"}
            actual = [n for n in browse_files if n not in skip]
            problems = []
            if build_doc.get("fileCount") != len(actual):
                problems.append("文件数 清单 %s ≠ 实际 %s"
                                % (build_doc.get("fileCount"), len(actual)))
            missing = sorted(set(listed_map) - set(actual))
            unexpected = sorted(set(actual) - set(listed_map))
            if missing:
                problems.append("清单有、实际无 %d 件（如 %s）" % (len(missing), missing[0]))
            if unexpected:
                problems.append("实际有、清单无 %d 件（如 %s）" % (len(unexpected), unexpected[0]))
            bad = []
            for rel_path, item in listed_map.items():
                full = os.path.join(out_dir, *rel_path.split("/"))
                if not os.path.isfile(full):
                    continue
                if os.path.getsize(full) != item.get("bytes") or _sha256(full) != item.get("sha256"):
                    bad.append(rel_path)
            if bad:
                problems.append("%d 件大小/哈希不符（如 %s）" % (len(bad), bad[0]))
            need = ["Start.bat", "tool/caddy.exe", "viewer/3d/index.html",
                    "viewer/2d/index.html", "viewer/project.js"]
            gone = [n for n in need if (BROWSE_REL + n) not in listed_map]
            if gone:
                problems.append("清单缺必需件 " + "、".join(gone))
            if not any(p.startswith(BROWSE_REL + "projects/") and p.endswith("/project.json")
                       for p in listed_map):
                problems.append("清单缺 projects/*/project.json")
            browse_ok = not problems
            total = sum(os.path.getsize(os.path.join(out_dir, *p.split("/"))) for p in actual)
            browse_note += "（%.1f MB）逐件核对%s" % (
                total / 1048576.0, "通过" if browse_ok else "未过：" + "；".join(problems))
    check(checks, "E-05", "成果九件齐全＋依据随包（成品版/内部件分存，三成果两版＋对照表＋两份成品 PDF）",
          sorted(expect) == sorted(set(names) - set(extra)) and extra_ok
          and all(n in names for n in bundle_need) and browse_ok,
          f"成果 {len(names)} 件（依据 {len(extra)} 个）{browse_note}；清单：{names}")
    clean_files = [n for n in names
                   if n.startswith("成品版/") and n.endswith(("_成品版.docx", "_成品版.xlsx"))]
    check(checks, "E-06", "成品版至少一件且可直接交付", bool(clean_files),
          # detail 只在失败时有意义：旧版写死一句「缺成品版…」，pass 时也跟着输出，
          # 报告里就成了「状态 pass、说明说缺件」的自相矛盾。
          f"成品版 {len(clean_files)} 件：" + "、".join(clean_files) if clean_files
          else "缺成品版目录或成品版文件（成品版/*_成品版.docx 或 .xlsx）")
    return {"violations": violations, "counts": counts, "outputs": names}


def main() -> int:
    ap = argparse.ArgumentParser(description="08 · 独立复算 + 基线对账 + 边界扫描")
    ap.add_argument("--workspace", default=".")
    ap.add_argument("--model", default=MODEL_DEFAULT)
    ap.add_argument("--indicators", default=IND_DEFAULT)
    ap.add_argument("--health", default=HEALTH_DEFAULT)
    ap.add_argument("--baseline", default=BASELINE_DEFAULT)
    ap.add_argument("--baseline-mode", choices=("auto", "write", "check", "none"), default="auto",
                    help="基线处理：auto=不存在则首跑自举并对账（默认）；write=强制重写；"
                         "check=只对账不写；none=跳过")
    ap.add_argument("--out", default=OUT_DEFAULT)
    ap.add_argument("--skills-root", default=os.environ.get("CARDO_SKILLS_ROOT", ""),
                    help="技能链 Skills/ 所在目录（技能包在工作区之外时由总控传入）")
    args = ap.parse_args()

    root = os.path.abspath(args.workspace)
    for label, path in (("model", args.model), ("indicators", args.indicators)):
        if not os.path.isfile(os.path.join(root, path)):
            print(f"[08] 缺输入 {label}：{path}", file=sys.stderr)
            return 2
    model = read_json(os.path.join(root, args.model))
    ind = read_json(os.path.join(root, args.indicators))
    if ind.get("schema") != IND_SCHEMA:
        print("[08] 现状指标 schema 不符", file=sys.stderr)
        return 2

    checks = []
    model_dir = os.path.dirname(os.path.abspath(os.path.join(root, args.model)))
    try:
        rec = recompute(model, model_dir)
    except (OSError, ValueError, KeyError, struct.error) as exc:
        print(f"[08] 独立复算失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 3

    compare_with_indicators(rec, ind, checks)
    baseline = compare_baseline(rec, ind, model, os.path.join(root, args.baseline), checks,
                                mode=args.baseline_mode)
    boundary = boundary_scan(root, checks, resolve_skills_root(root, args.skills_root))

    health = read_json(os.path.join(root, args.health)) if os.path.isfile(
        os.path.join(root, args.health)) else {}
    failed = [c for c in checks if c["status"] == "fail"]
    passed = sum(1 for c in checks if c["status"] == "pass")
    skipped = sum(1 for c in checks if c["status"] == "skip")

    daily = {
        "schema": "cardo/lowair-verify-independent/v1", "schemaVersion": 1,
        "generatedAt": now_iso(), "producer": {"name": "cardo-osgb", "version": "0.1.0"},
        "method": "独立实现：struct 逐点解 b3dm→GLB POSITION；1.0 m 网格取 max z；"
                  "15 m 邻域 P10 地面；4 邻域连通分量；最近秩分位；层高 3.0/2.8/3.3 m",
        "cellM": CELL_M, "radiusM": RADIUS_M, "thresholdM": THR_M,
        "recomputed": rec, "checks": checks,
        "summary": {"pass": passed, "fail": len(failed), "skip": skipped},
    }
    report = {
        "schema": SCHEMA, "schemaVersion": 1, "generatedAt": now_iso(),
        "producer": {"name": "cardo-osgb", "version": "0.1.0"},
        "inputs": {"model": args.model.replace("\\", "/"), "indicators": args.indicators.replace("\\", "/"),
                   "health": args.health.replace("\\", "/") if health else None,
                   "baseline": args.baseline.replace("\\", "/")},
        "healthVerdict": ((health.get("health") or {}).get("verdict")),
        "baseline": baseline, "boundary": boundary,
        "independent": {"file": "独立验证结果.json", "recomputed": rec},
        "checks": checks,
        "summary": {"total": len(checks), "pass": passed, "fail": len(failed), "skip": skipped},
        "ok": not failed,
    }
    out_dir = os.path.join(root, args.out)
    write_json(os.path.join(out_dir, "独立验证结果.json"), daily)
    write_json(os.path.join(out_dir, "验证报告.json"), report)

    print(f"[08] 验证 {len(checks)} 项：pass {passed} / fail {len(failed)} / skip {skipped}；"
          f"复算 建筑密度 {rec['coverage']:.6f} / 栋数 {rec['count']} / P50 {rec['heightP50']:.2f} m / "
          f"P90 {rec['heightP90']:.2f} m / 容积率 {rec['far'][FLOOR_M]:.4f}")
    if failed:
        for item in failed[:8]:
            print(f"[08] 失败：{item['id']} {item['name']} — {item['detail']}", file=sys.stderr)
        return 3
    print(f"[08] 基线：{baseline['status']}；边界越界 {len(boundary['violations'])} 条；"
          f"成果 {len(boundary['outputs'])} 件")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
