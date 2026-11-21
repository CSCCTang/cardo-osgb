#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""11 · 几何增强 · tree.single：单木分割（CHM 局部极大值 + 分水岭）。

口径见 规则/输出契约.md §5.5：
    chm = ndsm，且排除 09 建筑 mask（ndsm > 3.0 m 且观测格，close/open(3×3) 同 09）
    树冠域 = 观测格 且 非建筑 且 chm ≥ 2.0 m（分水岭只在树冠域内生长）
    skimage peak_local_max(min_distance=2.0 m, threshold_abs=2.0 m) 取种子 + watershed(-chm)
    过滤等效树冠半径 < 1.0 m 的种子（等效半径 = sqrt(面积 / π)）
输出 treeCount / crownAreaM2 / meanCrownDiameterM / maxHeightM。

产物（过程文件/11-几何增强/tree-single/）：
    tree-single.json / tree-single.md / _done；_patches/tree-single.json

用法（工作区根）：
    python <技能目录>/Skills/11-几何增强/scripts/tree_single.py --workspace .
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
    GRID_REL, MODEL_REL, OUT_ROOT, RECON_REL, emit, finish, load_grid, load_origin,
    parse_args, read_json, write_json,
)

ALGO = "tree.single"
SLUG = "tree-single"
PATCH_NAME = "tree-single.json"

BUILDING_THRESHOLD_M = 3.0      # 与 09 的 buildingThresholdM 一致（缺失时回退此值）
TREE_DOMAIN_MIN_M = 2.0         # 树冠域最低高度（= 种子阈值，只在冠层上分水岭）
PEAK_MIN_DISTANCE_M = 2.0
PEAK_THRESHOLD_ABS_M = 2.0
PEAK_SMOOTH_SIGMA = 1.0         # 与 09 的建筑分水岭一致
MIN_CROWN_RADIUS_M = 1.0


def _workspace_from_argv() -> str:
    """在 parse_args 之前取 --workspace，用于幂等短路（不改任何文件）。"""
    argv = sys.argv[1:]
    for i, item in enumerate(argv):
        if item == "--workspace" and i + 1 < len(argv):
            return argv[i + 1]
        if item.startswith("--workspace="):
            return item.split("=", 1)[1]
    return "."


def _done_ok(workspace: str, force: bool = False) -> bool:
    path = os.path.join(workspace, OUT_ROOT, "_done", f"{SLUG}.json")
    if force or not os.path.isfile(path):
        return False
    try:
        doc = read_json(path)
    except (OSError, ValueError):
        return False
    return doc.get("exitCode") == 0 and bool(doc.get("usable"))


def _has_force() -> bool:
    return any(a == "--force" or a.startswith("--force=") for a in sys.argv[1:])


def _origin_or_none(workspace: str):
    try:
        return load_origin(workspace)
    except (SystemExit, OSError, ValueError):
        return None


# ---------------------------------------------------------------- 掩膜与分割
def building_mask_09(ndsm: np.ndarray, observed: np.ndarray, threshold: float) -> np.ndarray:
    """按 09 同参数重算建筑 mask（09 未落盘 mask 栅格）：阈值 + close(3×3) + open(3×3)。"""
    raw = np.isfinite(ndsm) & (ndsm > threshold) & observed
    kernel = np.ones((3, 3))
    closed = ndimage.binary_closing(raw, structure=kernel, iterations=1) & observed
    return ndimage.binary_opening(closed, structure=kernel, iterations=1)


def split_crowns(chm: np.ndarray, domain: np.ndarray, cell: float) -> tuple:
    """局部极大值作种子 + 分水岭；返回 (labels, peaks, 诊断)。

    平滑只在 CHM 有效格上做归一化高斯（sigma 与 09 一致）：建筑格与未观测格没有 CHM，
    若按 0 参与平滑会在建筑边缘造出假低谷、把贴墙植被的峰值压掉。
    """
    from skimage.feature import peak_local_max
    from skimage.segmentation import watershed

    valid = np.isfinite(chm).astype(np.float64)
    num = ndimage.gaussian_filter(
        np.where(np.isfinite(chm), np.maximum(chm, 0.0), 0.0), sigma=PEAK_SMOOTH_SIGMA)
    den = ndimage.gaussian_filter(valid, sigma=PEAK_SMOOTH_SIGMA)
    smooth = np.where(den > 1e-6, num / np.maximum(den, 1e-12), 0.0)
    peaks = peak_local_max(
        smooth,
        min_distance=max(1, int(round(PEAK_MIN_DISTANCE_M / cell))),
        threshold_abs=PEAK_THRESHOLD_ABS_M,
        labels=domain,
        exclude_border=False,
    )
    if len(peaks) == 0:
        return np.zeros(domain.shape, dtype=np.int32), peaks, {"mode": "no-seed"}
    markers = np.zeros(domain.shape, dtype=np.int32)
    for idx, (py, px) in enumerate(peaks, start=1):
        markers[py, px] = idx
    labels = watershed(-smooth, markers, mask=domain)
    return labels, peaks, {"mode": "watershed"}


def trees_of(labels: np.ndarray, chm: np.ndarray, peaks, grid: dict, origin) -> tuple:
    """逐树统计；过滤等效半径 < 1.0 m 的种子。"""
    cell = grid["cell"]
    x0, y0 = grid["x0"], grid["y0"]
    kept, dropped = [], []
    for label_id in range(1, int(labels.max()) + 1 if labels.size else 0):
        ix, iy = np.nonzero(labels == label_id)   # 轴序：先 x 后 y（与 09 一致）
        if ix.size == 0:
            continue
        area = float(ix.size) * cell * cell
        radius = float(np.sqrt(area / np.pi))
        height = chm[ix, iy]
        height = height[np.isfinite(height)]
        if radius < MIN_CROWN_RADIUS_M:
            dropped.append({"label": int(label_id), "cells": int(ix.size),
                            "crownAreaM2": round(area, 2), "crownRadiusM": round(radius, 2)})
            continue
        local = (float(np.mean(x0 + (ix + 0.5) * cell)), float(np.mean(y0 + (iy + 0.5) * cell)))
        centroid = ([round(origin[0] + local[0], 3), round(origin[1] + local[1], 3)]
                    if origin else [round(local[0], 3), round(local[1], 3)])
        kept.append({
            "id": "",
            "label": int(label_id),
            "cells": int(ix.size),
            "crownAreaM2": round(area, 2),
            "crownRadiusM": round(radius, 2),
            "crownDiameterM": round(2.0 * radius, 2),
            "heightMaxM": round(float(np.max(height)), 2) if height.size else None,
            "heightMeanM": round(float(np.mean(height)), 2) if height.size else None,
            "centroidXY": centroid,
        })
    kept.sort(key=lambda t: t["crownAreaM2"], reverse=True)
    for i, item in enumerate(kept, start=1):
        item["id"] = f"T-{i:04d}"
    return kept, dropped, int(len(peaks))


def _mean(values) -> float | None:
    vals = [v for v in values if v is not None]
    return round(float(np.mean(vals)), 2) if vals else None


# ---------------------------------------------------------------- 主流程
def build(workspace: str) -> dict:
    recon = read_json(os.path.join(workspace, RECON_REL))
    grid = load_grid(workspace)
    origin = _origin_or_none(workspace)

    threshold = float((recon.get("params") or {}).get("buildingThresholdM") or
                      BUILDING_THRESHOLD_M)
    ndsm, observed, cell = grid["ndsm"], grid["observed"], grid["cell"]

    mask09 = building_mask_09(ndsm, observed, threshold)
    chm = np.where(observed & ~mask09 & np.isfinite(ndsm), ndsm, np.nan)
    domain = np.isfinite(chm) & (chm >= TREE_DOMAIN_MIN_M)
    labels, peaks, _ = split_crowns(chm, domain, cell)
    trees, dropped, seed_count = trees_of(labels, chm, peaks, grid, origin)
    unlabeled_cells = int((domain & (labels == 0)).sum())

    crown_area = round(float(sum(t["crownAreaM2"] for t in trees)), 2) if trees else 0.0
    domain_max = float(np.nanmax(chm[domain])) if domain.any() else None
    summary = {
        "treeCount": len(trees),
        "crownAreaM2": crown_area,
        "meanCrownDiameterM": _mean([t["crownDiameterM"] for t in trees]),
        "maxHeightM": round(float(max(t["heightMaxM"] for t in trees)), 2) if trees else None,
        "seedCount": seed_count,
        "droppedSeedCount": len(dropped),
        "treeDomainCells": int(domain.sum()),
        "treeDomainAreaM2": round(float(domain.sum()) * cell * cell, 1),
        "unlabeledTreeDomainCells": unlabeled_cells,
        "maxNdsmInTreeDomainM": round(domain_max, 2) if domain_max is not None else None,
        "buildingMaskCells": int(mask09.sum()),
        "observedCells": int(observed.sum()),
        "crownCoverRatioInDomain": round(crown_area / (float(domain.sum()) * cell * cell), 4)
        if domain.sum() else None,
    }

    gaps = [
        {
            "code": "ENH-GAP-001",
            "what": "密林会合并，treeCount 是下界估计",
            "why": f"1 m 栅格 + min_distance={PEAK_MIN_DISTANCE_M} m 的局部极大值只允许 2 m 内一个种子；"
                   "连片、树冠重叠或高度相近的树会被分水岭合成一个实例；"
                   f"另有 {summary['unlabeledTreeDomainCells']} 个树冠域格所在连通块内部没有"
                   "≥2 m 的平滑局部极大值，未生成实例（只计为缺口，不并入邻树）",
            "need": "接入高密度点云（树顶检测 + 树干分割）或亚米级正射影像分割，用真值树数校核",
        },
        {
            "code": "ENH-GAP-002",
            "what": "未与建筑附属绿化区分",
            "why": f"排除 09 建筑 mask（ndsm > {threshold:.1f} m）只挡掉屋顶主体；"
                   "贴墙的绿篱、屋顶绿化、围墙内灌木与行道树在栅格上同属高值非建筑区，被一律当独立树",
            "need": "建筑轮廓缓冲区内单独标记附属绿化，或用影像/点云语义分类区分乔木与灌木/绿篱",
        },
        {
            "code": "ENH-GAP-003",
            "what": "冠幅偏小：树冠域截在 2 m 等值线",
            "why": f"分水岭只在 chm ≥ {TREE_DOMAIN_MIN_M} m 的格内生长，冠缘低于 2 m 的部分被切掉；"
                   "crownAreaM2 与 meanCrownDiameterM 系统性偏小，低矮灌木/幼树也可能整株落在域外",
            "need": "改为点云冠幅边界或影像冠层边界，并按树种给出冠幅-树高关系校核",
        },
        {
            "code": "ENH-GAP-004",
            "what": "无真值，树数与冠幅未经精度验证",
            "why": "本项目没有人工测树（树高、冠幅、株数）样本，也没有激光点云分类结果可对照",
            "need": "提供样地实测或第三方单木分割成果，计算检出率/误检率与冠幅误差",
        },
    ]

    usable = bool(len(trees) and seed_count and int(domain.sum()))
    reasons = [] if usable else [
        "未检出可用种子或树冠域为空：非建筑区没有 ≥2 m 的植被高值格，无法出树"]

    markdown = render_md(summary, trees, dropped, gaps, threshold, cell)

    doc = emit(workspace, ALGO,
               inputs={"grid": GRID_REL.replace("\\", "/"),
                       "recon": RECON_REL.replace("\\", "/"),
                       "model": MODEL_REL.replace("\\", "/"),
                       "originUsed": bool(origin),
                       "buildingThresholdM09": threshold,
                       "buildingMaskCells09": int(mask09.sum())},
               params={"chm": "ndsm（DSM − 地面参考面），排除 09 建筑 mask",
                       "buildingMask": f"ndsm > {threshold:.1f} m 且观测格 + close(3×3) + open(3×3)",
                       "treeDomain": f"观测格 且 非建筑 且 chm ≥ {TREE_DOMAIN_MIN_M} m",
                       "seedMethod": "skimage.feature.peak_local_max",
                       "splitMethod": "skimage.segmentation.watershed(-gaussian(chm, sigma=1.0))",
                       "peakMinDistanceM": PEAK_MIN_DISTANCE_M,
                       "peakThresholdAbsM": PEAK_THRESHOLD_ABS_M,
                       "peakSmoothSigma": PEAK_SMOOTH_SIGMA,
                       "peakSmoothMode": "归一化高斯，只在 CHM 有效格上求均值（建筑/未观测格不参与）",
                       "minCrownRadiusM": MIN_CROWN_RADIUS_M,
                       "cellM": cell},
               summary=summary, items=trees, gaps=gaps, usable=usable, reasons=reasons,
               markdown=markdown)

    patches = {
        "id": ALGO, "status": "ready", "step": "11",
        "impl": "自实现：CHM 局部极大值取种子 + 分水岭分割，skimage peak_local_max/watershed",
        "evidence": "Skills/11-几何增强/scripts/tree_single.py",
        "sourceName": "CHM 局部极大值 + 分水岭，通用林业遥感方法；实现 scikit-image",
        "sourceLink": "https://scikit-image.org/docs/stable/api/skimage.segmentation.html#skimage.segmentation.watershed",
        "license": "BSD-3-Clause",
        "plain": "在 CHM 上按局部极大值定树顶、分水岭切冠，出树数、冠幅与最高树",
    }
    write_json(os.path.join(workspace, OUT_ROOT, "_patches", PATCH_NAME), patches)
    return doc


def render_md(summary, trees, dropped, gaps, threshold, cell) -> str:
    lines = ["# 单木分割（11 · tree.single）", "",
             "- 输入：09 栅格 `dsm/ground/ndsm/observed`（1.0 m/格）；"
             "`chm = ndsm`，排除 09 建筑 mask"
             f"（按 `ndsm > {threshold:.1f} m` 且观测格 + close/open(3×3) 重算）",
             f"- 树冠域：观测格且非建筑且 `chm ≥ {TREE_DOMAIN_MIN_M} m`（分水岭只在此域内生长）",
             f"- 种子：`peak_local_max(min_distance={PEAK_MIN_DISTANCE_M} m, "
             f"threshold_abs={PEAK_THRESHOLD_ABS_M} m)`，高斯平滑 σ={PEAK_SMOOTH_SIGMA}"
             "（只在 CHM 有效格上归一化，避免建筑/未观测格以 0 稀释峰值）",
             f"- 分割：`watershed(-chm, markers, mask=树冠域)`；"
             f"过滤等效树冠半径 < {MIN_CROWN_RADIUS_M} m 的实例", "",
             "## 结果", "",
             "| 指标 | 值 |", "|---|---|",
             f"| 树数 treeCount | {summary['treeCount']} |",
             f"| 树冠面积 crownAreaM2 | {summary['crownAreaM2']} m² |",
             f"| 平均冠幅 meanCrownDiameterM | {summary['meanCrownDiameterM']} m |",
             f"| 最高树 maxHeightM | {summary['maxHeightM']} m |",
             f"| 树冠域最高 CHM（诊断，含被滤掉的窄冠尖峰） | {summary['maxNdsmInTreeDomainM']} m |",
             f"| 种子数 / 被滤掉（半径<1 m） | {summary['seedCount']} / "
             f"{summary['droppedSeedCount']} |",
             f"| 树冠域 | {summary['treeDomainCells']} 格（{summary['treeDomainAreaM2']} m²） |",
             f"| 未获实例的域内格 | {summary['unlabeledTreeDomainCells']} |",
             f"| 树冠覆盖率（域内） | {summary['crownCoverRatioInDomain']} |", "",
             "### 逐树（按冠幅降序）", "",
             "| id | label | 格数 | 冠幅 m² | 半径 m | 冠径 m | 最高 m | 平均高 m |",
             "|---|---|---|---|---|---|---|---|"]
    for t in trees:
        lines.append(f"| {t['id']} | {t['label']} | {t['cells']} | {t['crownAreaM2']} | "
                     f"{t['crownRadiusM']} | {t['crownDiameterM']} | {t['heightMaxM']} | "
                     f"{t['heightMeanM']} |")
    if dropped:
        lines += ["", "被滤掉的种子（等效半径 < 1.0 m）：" + "、".join(
            f"label {d['label']}（{d['cells']} 格）" for d in dropped)]
    lines += ["", "## 局限", ""]
    for gap in gaps:
        lines.append(f"- `{gap['code']}` {gap['what']}：{gap['why']}（需：{gap['need']}）")
    lines += ["",
              "> `treeCount` 是**下界估计**：密林与重叠树冠会被合并；冠幅只统计 2 m 以上的冠层，"
              "系统性偏小。本结果未经实测或点云分类校核，不能当林业调查真值使用。", ""]
    return "\n".join(lines)


def main() -> int:
    workspace = _workspace_from_argv()
    if _done_ok(workspace, _has_force()):
        print("[11:tree.single] 已完成（_done exitCode=0），跳过，不改任何文件")
        return 0
    args = parse_args(ALGO, "11 · 单木分割（CHM 局部极大值 + 分水岭）")

    for rel in (GRID_REL, RECON_REL):
        if not os.path.isfile(os.path.join(args.workspace, rel)):
            print(f"[11:tree.single] 缺输入：{rel}（先跑 09）", file=sys.stderr)
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
