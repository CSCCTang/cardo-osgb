#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""11 · 几何增强 · pc.ground-filter —— 纯 Python 渐进形态学滤波（PMF, Zhang et al. 2003）。

口径见 规则/输出契约.md §5.1：
  - 输入 09 栅格 dsm / observed（axis0=+x、axis1=+y，1.0 m/格）；
  - 结构元窗口 3→33 格（步长 2），开运算 = 窗口内 min 再 max（方形、对称，保证开运算 ≤ 原值）；
  - 高差阈值 dh = 0.5 + 0.02·w²；某格开运算相对上一窗口抬升 > dh 时判为非地面，沿用上一窗口地面；
  - 只用 observed 格：空洞在腐蚀时按 +inf、膨胀时按 -inf 剔除，边界按 nearest 延拓；
  - 产出 ground_pmf（.npz），并与 09 的 P10 地面逐格对比（meanAbsDiffM / p95AbsDiffM / agreeWithin1m）；
  - 统计 09 建筑 mask 内的负 nDSM 格数（negativeNdsmCells），必须为 0 或写 gap 解释。
不用 PDAL、不装第三方包（仅 numpy + scipy.ndimage）。
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import os

import numpy as np
from scipy.ndimage import maximum_filter, minimum_filter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import (parse_args, load_grid, emit, finish, read_json, write_json,  # noqa: E402
                    GRID_REL, OUT_ROOT, slug)

ALGO = "pc.ground-filter"
DONE_REL = os.path.join(OUT_ROOT, "_done", "pc-ground-filter.json")
NPZ_NAME = "ground_pmf.npz"

WIN_START, WIN_END, WIN_STEP = 3, 33, 2
DH_BASE, DH_SLOPE = 0.5, 0.02
BUILDING_NDSM_M = 3.0        # 与 09 默认 --building-threshold-m 一致，用来还原 09 建筑 mask


# ---------------------------------------------------------------- 幂等预检
def _workspace_from_argv(argv) -> str:
    ws = "."
    for i, a in enumerate(argv):
        if a == "--workspace" and i + 1 < len(argv):
            ws = argv[i + 1]
        elif a.startswith("--workspace="):
            ws = a.split("=", 1)[1]
    return ws


def already_done(ws: str):
    p = os.path.join(ws, DONE_REL)
    if not os.path.isfile(p):
        return None
    try:
        d = read_json(p)
    except (OSError, ValueError):
        return None
    return p if int(d.get("exitCode", -1)) == 0 else None


# ---------------------------------------------------------------- PMF 核心
def dh_of(w: int) -> float:
    """Zhang et al. 2003 高差阈值（w = 窗口格数）。"""
    return DH_BASE + DH_SLOPE * w * w


def opening(z: np.ndarray, w: int, observed: np.ndarray) -> np.ndarray:
    """方形结构元开运算：先腐蚀（窗口最小值）再膨胀（窗口最大值）。

    空洞（未观测）不参与：腐蚀按 +inf 剔除，膨胀按 -inf 剔除；
    边界用 nearest 延拓（等价于窗口贴边裁切后取极值）。
    返回 float 数组，未观测格为 NaN。
    """
    f = np.where(observed, z, np.inf)
    eroded = minimum_filter(f, size=w, mode="nearest")
    valid = np.isfinite(eroded)
    fill = np.where(valid, eroded, -np.inf)
    dilated = maximum_filter(fill, size=w, mode="nearest")
    out = np.full(z.shape, np.nan, dtype=np.float64)
    ok = np.isfinite(dilated) & observed
    out[ok] = dilated[ok]
    return out


def pmf(dsm: np.ndarray, observed: np.ndarray):
    """返回 (ground_pmf, per-window 统计列表)。"""
    ground = None
    prev = None
    stats = []
    for w in range(WIN_START, WIN_END + 1, WIN_STEP):
        dh = dh_of(w)
        op = opening(dsm, w, observed)
        if prev is None:
            ground = op.copy()
            kept = 0
        else:
            rise = op - prev
            # rise 为 NaN（未观测格）时比较结果为 False → 取 op（NaN），与空洞一致
            obj = np.where(np.isnan(rise), False, rise > dh)
            ground = np.where(obj, ground, op)
            kept = int(np.nansum(obj))
        prev = op
        stats.append({
            "windowCells": w, "windowM": round(w * 1.0, 3),
            "dhM": round(dh, 4),
            "keptAsObjectCells": kept,
            "observedOpenedCells": int(np.isfinite(op).sum()),
        })
    return ground, stats


# ---------------------------------------------------------------- Markdown
def render_md(summary, stats, gaps, params_up) -> str:
    lines = [
        "# 11 · 几何增强 · 点云地面滤波（pc.ground-filter / PMF）",
        "",
        "- 生成：`Skills/11-几何增强/scripts/pc_ground_filter.py`　输入：`过程文件/09-三维要素反演/栅格/栅格.npz`",
        "- 输出：`过程文件/11-几何增强/pc-ground-filter/ground_pmf.npz`（键 `ground_pmf`，未观测格为 NaN）",
        "",
        "## 方法",
        "",
        "纯 numpy + scipy.ndimage 实现 Zhang et al. (2003) 渐进形态学滤波（不用 PDAL）：",
        "",
        "1. **开运算**：方形结构元先腐蚀（窗口内最小值）后膨胀（窗口内最大值）；结构元对称，"
        "因此开运算恒 ≤ 原 DSM（nDSM 不会为负）；",
        "2. **窗口递增**：3 → 33 格、步长 2（1.0 m/格，即 3 → 33 m）；",
        "3. **高差阈值**：`dh = 0.5 + 0.02·w²`；新窗口开运算相对上一窗口抬升 > dh 的格判为非地面"
        "（建筑/植被），沿用上一窗口地面；否则接受当前开运算；",
        "4. **只用观测格**：空洞在腐蚀中按 `+inf` 剔除、膨胀中按 `-inf` 剔除，边界按 nearest 延拓；",
        "5. **对比**：与 09 的局部 P10 地面（±15 m 窗口、仅观测格）逐格做绝对差。",
        "",
        "## 参数",
        "",
        "| 项 | 值 |",
        "|---|---|",
        f"| 窗口（格） | {WIN_START} → {WIN_END}，步长 {WIN_STEP} |",
        f"| 格距 | {params_up['cellM']} m |",
        f"| dh 公式 | `0.5 + 0.02·w²`（w 为窗口格数） |",
        f"| 结构元 | 方形 {WIN_START}×{WIN_START} … {WIN_END}×{WIN_END}，nearest 边界 |",
        f"| 建筑 mask 口径 | 09 `nDSM > {BUILDING_NDSM_M} m` 且观测（原始 mask，未做形态学） |",
        "",
        "## 结果",
        "",
        "| 指标 | 值 |",
        "|---|---|",
        f"| 观测格数 | {summary['observedCells']} |",
        f"| ground_pmf 有值格数 | {summary['groundCells']} |",
        f"| 与 09 P10 平均绝对差 | {summary['meanAbsDiffM']} m |",
        f"| 与 09 P10 P95 绝对差 | {summary['p95AbsDiffM']} m |",
        f"| 差异 ≤ 1 m 占比 | {round(summary['agreeWithin1m'] * 100, 2)}% |",
        f"| 建筑 mask 格数（09，nDSM>{BUILDING_NDSM_M}） | {summary['buildingMaskCells']} |",
        f"| 建筑 mask 内负 nDSM 格数 | {summary['negativeNdsmCells']} |",
        f"| 全部观测格负 nDSM 格数 | {summary['negativeNdsmCellsAllObserved']} |",
        f"| ground_pmf P1 / P50 / P99（观测格） | {summary['groundP01M']} / {summary['groundP50M']} / {summary['groundP99M']} m |",
        "",
        "## 逐窗口",
        "",
        "| 窗口(格) | 窗口(m) | dh(m) | 判为非地面沿用前值格数 | 有值格数 |",
        "|---|---|---|---|---|",
    ]
    for s in stats:
        lines.append(f"| {s['windowCells']} | {s['windowM']} | {s['dhM']} | "
                     f"{s['keptAsObjectCells']} | {s['observedOpenedCells']} |")
    lines += [
        "",
        "## 局限",
        "",
        "- PMF 输出的是**滤波面**，不是实测 DTM：无真值可校核，"
        "与 09 的 P10 差值中 p95 偏大主要来自窗口口径不同（P10 在建筑附近被屋面抬高），"
        "不代表哪一方更接近真实地面；",
        "- 形态学滤波会把小台地/挡墙/沟渠抹平，陡坡与悬崖处地面会偏低（经典 PMF 缺陷）；",
        "- 空洞按未观测处理：窗口内无观测的格输出 NaN，不做插值，避免凭空造值；",
        "- 无点云原始回波，无法拿“噪声点/低矮植被”再校核，只能基于 DSM 网格。",
        "",
    ]
    if gaps:
        lines.append("## gaps")
        lines.append("")
        for g in gaps:
            lines.append(f"- `{g['code']}` {g['what']}｜{g['why']}｜{g['need']}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- 主流程
def main() -> int:
    done = already_done(_workspace_from_argv(sys.argv[1:]))
    if done:
        print(f"[11:pc.ground-filter] 已完成（{done}），跳过")
        return 0

    args = parse_args(ALGO, "11 · pc.ground-filter：渐进形态学滤波（PMF）地面")
    ws = args.workspace
    if not os.path.isfile(os.path.join(ws, GRID_REL)):
        print(f"[11] 缺输入：{GRID_REL}（先跑 09）", file=sys.stderr)
        return 2

    grid = load_grid(ws)
    dsm, observed, p10 = grid["dsm"], grid["observed"].astype(bool), grid["ground"]
    ndsm09 = grid["ndsm"]
    cell = grid["cell"]

    if observed.sum() == 0:
        print("[11] 栅格无观测格", file=sys.stderr)
        return 2

    ground, stats = pmf(dsm, observed)

    # ground 理论上恒 ≤ dsm（开运算反扩张性）；负 nDSM 只在极端数值下才可能出现
    ground_cells = int(np.isfinite(ground).sum())
    safe_ground = np.where(observed, np.where(np.isnan(ground), dsm, ground), np.nan)
    neg_all = int(np.nansum(observed & (dsm - safe_ground < 0)))

    mask09 = observed & (ndsm09 > BUILDING_NDSM_M)      # 09 建筑原始 mask（3209 格量级）
    neg_in_build = observed & mask09 & (dsm - safe_ground < 0)
    neg_build = int(np.nansum(neg_in_build))

    diff = np.abs(safe_ground - p10)
    diff = diff[observed & np.isfinite(diff)]
    mean_abs = float(diff.mean()) if diff.size else None
    p95 = float(np.percentile(diff, 95)) if diff.size else None
    agree = float((diff <= 1.0).mean()) if diff.size else None

    gvals = safe_ground[observed & np.isfinite(safe_ground)]
    summary = {
        "observedCells": int(observed.sum()),
        "groundCells": ground_cells,
        "buildingMaskCells": int(mask09.sum()),
        "negativeNdsmCells": neg_build,
        "negativeNdsmCellsAllObserved": neg_all,
        "meanAbsDiffM": round(mean_abs, 4) if mean_abs is not None else None,
        "p95AbsDiffM": round(p95, 4) if p95 is not None else None,
        "agreeWithin1m": round(agree, 4) if agree is not None else None,
        "groundP01M": round(float(np.percentile(gvals, 1)), 4) if gvals.size else None,
        "groundP50M": round(float(np.percentile(gvals, 50)), 4) if gvals.size else None,
        "groundP99M": round(float(np.percentile(gvals, 99)), 4) if gvals.size else None,
    }

    gaps = []
    if ground is not None and np.isnan(ground[observed]).any():
        gaps.append({"code": "ENH-GAP-001", "what": "部分观测格未得到 ground_pmf",
                     "why": "开运算窗口内观测格不足", "need": "检查 observed 空洞分布后重跑"})
    if neg_build > 0:
        gaps.append({"code": "ENH-GAP-002", "what": f"建筑 mask 内有 {neg_build} 格负 nDSM",
                     "why": "开运算并非严格反扩张（边界/inf 剔除导致的数值边界效应）",
                     "need": "检查 ground_pmf 与 DSM 的逐格差；若为局部毛刺可接受并降级"})
    if not gaps:
        gaps.append({
            "code": "ENH-GAP-001",
            "what": "PMF 地面是滤波面，无实测 DTM 校核",
            "why": "PMF 与 09 的局部 P10 是两种近似口径，P95 差值反映口径差异（窗口/分位 vs 形态学），不代表精度",
            "need": "提供实测地面点/检查点后跑精度评估；或接入点云原始回波做 CSF/SMRF 对照",
        })

    usable = (summary["groundCells"] == summary["observedCells"]) and neg_build == 0
    reasons = []
    if summary["groundCells"] != summary["observedCells"]:
        reasons.append(f"ground_pmf 覆盖 {summary['groundCells']} ≠ 观测格 {summary['observedCells']}")
    if neg_build != 0:
        reasons.append(f"建筑 mask 内负 nDSM {neg_build} 格（见 gap）")

    out = os.path.join(ws, OUT_ROOT, slug(ALGO))
    os.makedirs(out, exist_ok=True)
    npz_path = os.path.join(out, NPZ_NAME)
    np.savez_compressed(
        npz_path,
        ground_pmf=ground.astype(np.float64),
        observed=observed,
        cell=np.array([cell], dtype=np.float64),
        x0=np.array([grid["x0"]], dtype=np.float64),
        y0=np.array([grid["y0"]], dtype=np.float64),
        dsm=dsm.astype(np.float64),
        ground_p10=grid["ground"].astype(np.float64),
    )

    doc = emit(
        ws, ALGO,
        {"grid": GRID_REL,
         "dsm": "栅格.npz:dsm",
         "observed": "栅格.npz:observed",
         "groundP10": "栅格.npz:ground",
         "ndsm09": "栅格.npz:ndsm"},
        {"windowStartCells": WIN_START, "windowEndCells": WIN_END,
         "windowStepCells": WIN_STEP, "cellM": cell,
         "dhFormula": "0.5 + 0.02*w^2", "dhBase": DH_BASE, "dhSlope": DH_SLOPE,
         "structuringElement": "square(binary, symmetric)",
         "boundaryMode": "nearest", "holePolicy": "exclude(+inf erode / -inf dilate)",
         "buildingThresholdM": BUILDING_NDSM_M, "windows": stats},
        summary, items=stats, gaps=gaps, usable=usable, reasons=reasons,
        markdown=render_md(summary, stats, gaps, {"cellM": cell}),
        extra_files=[os.path.join("过程文件", "11-几何增强", slug(ALGO), NPZ_NAME)])

    patch_dir = os.path.join(ws, OUT_ROOT, "_patches")
    os.makedirs(patch_dir, exist_ok=True)
    write_json(os.path.join(patch_dir, f"{slug(ALGO)}.json"), {
        "id": ALGO,
        "status": "ready",
        "step": "11",
        "impl": "自实现：numpy + scipy.ndimage 方形结构元开运算 + 逐窗 dh 判阈（不用 PDAL）",
        "evidence": "Skills/11-几何增强/scripts/pc_ground_filter.py",
        "sourceName": "Zhang et al. 2003 渐进形态学滤波（Progressive Morphological Filter）",
        "sourceLink": "",
        "license": "论文方法 + 本链自实现",
        "plain": "用 3→33 格形态学开运算滤出地面面，和 09 P10 地面逐格对账并查负 nDSM",
    })

    return finish(doc)


if __name__ == "__main__":
    raise SystemExit(main())
