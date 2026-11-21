#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""11 · 几何增强 · pc.classify：栅格几何启发式分类（地面/建筑/植被/其它）。

口径见 规则/输出契约.md §5.2：
    ndsm ≤ 0.5                          → ground
    ndsm > 3.0 且 粗糙度 ≤ 0.5（3×3）   → building
    ndsm > 1.5 且 粗糙度 > 0.8（3×3）   → vegetation
    其余观测格                          → other
粗糙度 = 3×3 dsm 标准差（只用观测格统计，NaN 不参与）；坡度只做诊断，不进规则。
建筑 mask 按 09 同参数重算（ndsm > 3.0 + close(3×3) + open(3×3)），用于算 buildingRecallVs09。

产物（过程文件/11-几何增强/pc-classify/）：
    pc-classify.json / pc-classify.md / _done；_patches/pc-classify.json

用法（工作区根）：
    python <技能目录>/Skills/11-几何增强/scripts/pc_classify.py --workspace .
退出码：0 可用；2 缺输入；3 产出但不可用；4 产物已存在未给 --force。
"""
from __future__ import annotations

import os
import sys

sys.dont_write_bytecode = True

import numpy as np
from scipy import ndimage

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (  # noqa: E402
    GRID_REL, OUT_ROOT, RECON_REL, emit, finish, load_grid, parse_args, read_json,
    write_json,
)

ALGO = "pc.classify"
SLUG = "pc-classify"
ATTR_REL = os.path.join("过程文件", "09-三维要素反演", "逐栋属性.json")
PATCH_NAME = "pc-classify.json"

NDSM_GROUND_MAX = 0.5
NDSM_BUILDING_MIN = 3.0
ROUGH_BUILDING_MAX = 0.5
NDSM_VEG_MIN = 1.5
ROUGH_VEG_MIN = 0.8
ROUGH_WIN = 3
THRESH_DEFAULT_M = 3.0
CLASSES = ("ground", "building", "vegetation", "other")


def _workspace_from_argv() -> str:
    """在 parse_args 之前取 --workspace，用于幂等短路（不改任何文件）。"""
    argv = sys.argv[1:]
    for i, item in enumerate(argv):
        if item == "--workspace" and i + 1 < len(argv):
            return argv[i + 1]
        if item.startswith("--workspace="):
            return item.split("=", 1)[1]
    return "."


def _done_ok(workspace: str) -> bool:
    path = os.path.join(workspace, OUT_ROOT, "_done", f"{SLUG}.json")
    if not os.path.isfile(path):
        return False
    try:
        doc = read_json(path)
    except (OSError, ValueError):
        return False
    return doc.get("exitCode") == 0 and bool(doc.get("usable"))


# ---------------------------------------------------------------- 几何量
def window_roughness(dsm: np.ndarray, observed: np.ndarray, win: int) -> np.ndarray:
    """3×3 dsm 标准差（只用观测格；窗内无观测则 NaN）。"""
    obs = observed.astype(np.float64)
    d = np.where(observed, dsm, 0.0)
    w = ndimage.uniform_filter(obs, win, mode="constant")      # 观测格权重（已除以窗面积）
    mu = ndimage.uniform_filter(d, win, mode="constant") / np.maximum(w, 1e-12)
    sq = np.where(observed, (dsm - mu) ** 2, 0.0)
    var = np.maximum(ndimage.uniform_filter(sq, win, mode="constant")
                     / np.maximum(w, 1e-12), 0.0)
    rough = np.sqrt(var)
    rough[w <= 0] = np.nan
    return rough


def slope_deg(dsm: np.ndarray, observed: np.ndarray, cell: float) -> np.ndarray:
    """坡度（度）；未观测格 NaN，邻接空洞的格梯度为 NaN（不参与统计）。"""
    d = np.where(observed, dsm, np.nan)
    gy, gx = np.gradient(d, cell)
    return np.degrees(np.arctan(np.sqrt(gx ** 2 + gy ** 2)))


def classify(ndsm: np.ndarray, rough: np.ndarray, observed: np.ndarray) -> np.ndarray:
    """按 §5.2 阈值分四类；未观测格标 unobserved（不计入分母）。"""
    cls = np.full(ndsm.shape, "other", dtype=object)
    cls[observed & (ndsm <= NDSM_GROUND_MAX)] = "ground"
    building = observed & (ndsm > NDSM_BUILDING_MIN) & (rough <= ROUGH_BUILDING_MAX)
    cls[building] = "building"
    vegetation = observed & (ndsm > NDSM_VEG_MIN) & (rough > ROUGH_VEG_MIN)
    cls[vegetation] = "vegetation"
    cls[~observed] = "unobserved"
    return cls


def building_mask_09(ndsm: np.ndarray, observed: np.ndarray, threshold: float) -> np.ndarray:
    """按 09 同参数重算建筑 mask（09 未落盘 mask 栅格）。"""
    raw = np.isfinite(ndsm) & (ndsm > threshold) & observed
    kernel = np.ones((3, 3))
    closed = ndimage.binary_closing(raw, structure=kernel, iterations=1) & observed
    return ndimage.binary_opening(closed, structure=kernel, iterations=1)


def _mean(values, cells) -> float | None:
    vals = values[cells]
    vals = vals[np.isfinite(vals)]
    return round(float(vals.mean()), 2) if vals.size else None


# ---------------------------------------------------------------- 主流程
def build(workspace: str) -> dict:
    recon = read_json(os.path.join(workspace, RECON_REL))
    attrs = read_json(os.path.join(workspace, ATTR_REL))
    grid = load_grid(workspace)

    threshold = float((recon.get("params") or {}).get("buildingThresholdM") or THRESH_DEFAULT_M)
    rec_mask_cells = ((recon.get("diagnostics") or {}).get("mask") or {}).get("cleanedCells")

    ndsm, observed, cell = grid["ndsm"], grid["observed"], grid["cell"]
    rough = window_roughness(grid["dsm"], observed, ROUGH_WIN)
    slope = slope_deg(grid["dsm"], observed, cell)
    cls = classify(ndsm, rough, observed)
    mask09 = building_mask_09(ndsm, observed, threshold)

    observed_cells = int(observed.sum())
    mask_cells = int(mask09.sum())
    building_cells = int((cls == "building").sum())
    hit = int((mask09 & (cls == "building")).sum())
    recall = round(hit / mask_cells, 4) if mask_cells else None
    precision = round(hit / building_cells, 4) if building_cells else None

    items = []
    for name in CLASSES:
        cells = observed & (cls == name)
        n = int(cells.sum())
        items.append({
            "class": name,
            "cells": n,
            "share": round(n / observed_cells, 4) if observed_cells else None,
            "areaM2": round(n * cell * cell, 1),
            "meanNdsmM": _mean(ndsm, cells),
            "meanRoughnessM": _mean(rough, cells),
            "meanSlopeDeg": _mean(slope, cells),
        })

    summary = {
        "observedCells": observed_cells,
        "groundCells": items[0]["cells"],
        "groundShare": items[0]["share"],
        "buildingCells": items[1]["cells"],
        "buildingShare": items[1]["share"],
        "vegetationCells": items[2]["cells"],
        "vegetationShare": items[2]["share"],
        "otherCells": items[3]["cells"],
        "otherShare": items[3]["share"],
        "buildingRecallVs09": recall,
        "buildingPrecisionVs09": precision,
        "maskCells09": rec_mask_cells,
        "maskCellsRecomputed": mask_cells,
        "meanSlopeDeg": _mean(slope, observed),
    }

    gaps = [{
        "code": "ENH-GAP-001",
        "what": "纯几何启发式分类，不是语义真值",
        "why": "只有 nDSM + 3×3 粗糙度，没有影像分割、也没有点云语义分类（PDAL/PointNet 等），"
               "树冠与建筑屋顶在高度/粗糙度上会互相混淆",
        "need": "接入影像建筑分割或 PDAL 点云分类（地面/建筑/植被）替换本分类",
    }, {
        "code": "ENH-GAP-002",
        "what": "建筑边缘格被分到植被/其它，buildingRecallVs09 偏低",
        "why": "1 m 栅格 + 3×3 dsm 标准差在墙脚/屋面边缘会把地面高差算成高粗糙度；"
               "建筑内部起伏（坡屋顶、设备）也会超过 0.5 m 阈值",
        "need": "建筑 mask 内做形态学闭合/邻域投票，或改用更细栅格/法向一致性特征",
    }, {
        "code": "ENH-GAP-003",
        "what": "坡度只做诊断，未进分类规则",
        "why": "契约 §5.2 未给坡度阈值；坡地、挡墙、护坡与植被坡面无法区分",
        "need": "提供实测地形或带坡度阈值的口径后再用",
    }, {
        "code": "ENH-GAP-004",
        "what": "无真值，各类占比只是本链口径下的几何估计",
        "why": "没有人工标注的地面/建筑/植被样本，无法算混淆矩阵或总体精度",
        "need": "提供抽样标注或区域真值图，跑精度评估",
    }]

    usable = bool(observed_cells and mask_cells and building_cells and recall is not None)
    reasons = [] if usable else ["观测格/建筑格/09 mask 为空，比值无法计算"]

    markdown = render_md(summary, items, gaps, threshold)

    doc = emit(workspace, ALGO,
               inputs={"grid": GRID_REL.replace("\\", "/"),
                       "recon": RECON_REL.replace("\\", "/"),
                       "attrs09": ATTR_REL.replace("\\", "/"),
                       "buildingCount09": attrs.get("count"),
                       "maskCells09": rec_mask_cells},
               params={"ndsmGroundMaxM": NDSM_GROUND_MAX,
                       "ndsmBuildingMinM": NDSM_BUILDING_MIN,
                       "roughnessBuildingMaxM": ROUGH_BUILDING_MAX,
                       "ndsmVegetationMinM": NDSM_VEG_MIN,
                       "roughnessVegetationMinM": ROUGH_VEG_MIN,
                       "roughnessWindow": f"{ROUGH_WIN}x{ROUGH_WIN}",
                       "roughnessMetric": "dsm 标准差（仅观测格）",
                       "slopeUse": "仅诊断，不进规则",
                       "buildingThresholdM09": threshold,
                       "maskMorphology09": "close(3x3)+open(3x3)",
                       "cellM": cell},
               summary=summary, items=items, gaps=gaps, usable=usable, reasons=reasons,
               markdown=markdown)

    patches = {
        "id": ALGO, "status": "ready", "step": "11",
        "impl": "自实现：nDSM 阈值 + 3×3 dsm 标准差粗糙度（+ 诊断坡度）规则分类",
        "evidence": "Skills/11-几何增强/scripts/pc_classify.py",
        "sourceName": "本链自定几何启发式",
        "sourceLink": "",
        "license": "自研代码",
        "plain": "按高度与粗糙度把栅格分成地面/建筑/植被/其它，给下游做掩膜",
    }
    write_json(os.path.join(workspace, OUT_ROOT, "_patches", PATCH_NAME), patches)
    return doc


def render_md(summary, items, gaps, threshold) -> str:
    lines = ["# 点云分类（11 · pc.classify）", "",
             "- 输入：09 栅格 `dsm/ground/ndsm/observed`（1.0 m/格）与 09 建筑 mask"
             f"（按 `ndsm > {threshold:.1f} m` + close/open(3×3) 重算）",
             "- 规则：`ndsm ≤ 0.5`→ground；`ndsm > 3.0 且 3×3 dsm 标准差 ≤ 0.5`→building；"
             "`ndsm > 1.5 且标准差 > 0.8`→vegetation；其余→other；未观测格不计入",
             "- 坡度只做诊断列，未参与判定", "",
             "## 结果（观测格 {obs} 格）".format(obs=summary["observedCells"]), "",
             "| 类别 | 格数 | 占比 | 面积 m² | 平均 nDSM m | 平均粗糙度 m | 平均坡度 ° |",
             "|---|---|---|---|---|---|---|"]
    for item in items:
        lines.append(f"| {item['class']} | {item['cells']} | {item['share']} | "
                     f"{item['areaM2']} | {item['meanNdsmM']} | {item['meanRoughnessM']} | "
                     f"{item['meanSlopeDeg']} |")
    lines += ["",
              f"- `buildingRecallVs09` = **{summary['buildingRecallVs09']}**"
              f"（09 mask {summary['maskCells09']} 格，重算 {summary['maskCellsRecomputed']} 格）；"
              f"`buildingPrecisionVs09` = {summary['buildingPrecisionVs09']}",
              f"- 平均坡度 {summary['meanSlopeDeg']}°（全观测格）",
              "",
              "## 局限", ""]
    for gap in gaps:
        lines.append(f"- `{gap['code']}` {gap['what']}：{gap['why']}（需：{gap['need']}）")
    lines += ["",
              "> 本分类是**几何启发式**，不是语义真值；建筑召回偏低主要来自屋面边缘与坡屋顶的"
              "高粗糙度，不能据此判定分类精度。", ""]
    return "\n".join(lines)


def main() -> int:
    workspace = _workspace_from_argv()
    if _done_ok(workspace):
        print("[11:pc.classify] 已完成（_done exitCode=0），跳过，不改任何文件")
        return 0
    args = parse_args(ALGO, "11 · 点云分类（几何启发式：地面/建筑/植被/其它）")

    for rel in (GRID_REL, RECON_REL, ATTR_REL):
        if not os.path.isfile(os.path.join(args.workspace, rel)):
            print(f"[11:pc.classify] 缺输入：{rel}（先跑 09）", file=sys.stderr)
            return 2

    doc = build(args.workspace)
    return finish(doc)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
