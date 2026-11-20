#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""06 · 2.5D 网格指标：调本技能内 run_indicators 计算，并守「7 条指标 × 10 键」闸门。

输入：过程文件/03-OSGB转3DTiles/转换产物/cardo-lowair-model.json（05 产出）
输出（默认 过程文件/06-2.5D网格指标/）：现状指标.json（cardo/lowair-indicators/v1）、指标说明.md
闸门：7 个指标 id 齐全；每条 10 键（id/name/value/range/unit/method/confidence/limits/source/grade）；
      区间为二元组；算不出的必须是 null + gaps[]（不编数）。
退出码：0 通过；2 输入缺失；3 指标不可用（quality.usable=false）或闸门不过。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.dont_write_bytecode = True
sys.path.insert(0, HERE)
import indicator_report  # noqa: E402
import run_indicators  # noqa: E402

SCHEMA = "cardo/lowair-indicators/v1"
EXPECTED_IDS = ("building_coverage_ratio", "building_count", "building_height_p50",
                "building_height_p90", "floor_area_ratio_est", "greenery_ratio", "road_ratio")
MODEL_DEFAULT = os.path.join("过程文件", "03-OSGB转3DTiles", "转换产物", "cardo-lowair-model.json")
OUT_DEFAULT = os.path.join("过程文件", "06-2.5D网格指标")


def write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def gate(doc: dict) -> list:
    """返回问题清单（空 = 通过）。"""
    problems = []
    if doc.get("schema") != SCHEMA:
        problems.append(f"schema 非 {SCHEMA}")
    ids = [m.get("id") for m in doc.get("metrics") or []]
    if sorted(ids) != sorted(EXPECTED_IDS):
        problems.append(f"指标 id 不全/多余：{ids}")
    for metric in doc.get("metrics") or []:
        missing = [k for k in run_indicators.METRIC_KEYS if k not in metric]
        if missing:
            problems.append(f"{metric.get('id')} 缺键 {missing}")
        rng = metric.get("range")
        if not (isinstance(rng, (list, tuple)) and len(rng) == 2):
            problems.append(f"{metric.get('id')}.range 不是二元组")
        if metric.get("value") is None and metric.get("grade") != "placeholder":
            problems.append(f"{metric.get('id')} 值为 null 但 grade≠placeholder（缺数必须 null+gap）")
        if metric.get("value") is not None:
            lo, hi = rng
            if isinstance(lo, (int, float)) and isinstance(hi, (int, float)) \
                    and not (lo <= metric["value"] <= hi):
                problems.append(f"{metric.get('id')} 值 {metric['value']} 不在区间 {rng}")
    if not doc.get("quality", {}).get("usable"):
        problems.append("quality.usable=false")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description="06 · 2.5D 网格指标（7 条 + 区间 + 局限）")
    ap.add_argument("--model", default=MODEL_DEFAULT)
    ap.add_argument("--out", default=OUT_DEFAULT)
    ap.add_argument("--cell-m", type=float, default=1.0)
    ap.add_argument("--ground-percentile", type=float, default=5.0)
    ap.add_argument("--building-threshold-m", type=float, default=3.0)
    ap.add_argument("--surface", choices=("auto", "top", "all"), default="auto",
                    help="表面口径：auto（法线覆盖足够时用顶面）/ top / all")
    args = ap.parse_args()

    if not os.path.isfile(args.model):
        print(f"[06] 缺模型契约：{args.model}（先跑 05）", file=sys.stderr)
        return 2

    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)
    json_path = os.path.join(out_dir, "现状指标.json")
    md_path = os.path.join(out_dir, "指标说明.md")
    for stale in (json_path, md_path):   # 先删旧再生成，防"上次的指标"假通过
        if os.path.isfile(stale):
            os.remove(stale)

    status = 0
    try:
        doc = run_indicators.build(args.model, out_dir, args.cell_m,
                                   args.ground_percentile, args.building_threshold_m,
                                   args.surface)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"[06] 输入不可用：{type(exc).__name__}: {exc}", file=sys.stderr)
        doc, status = run_indicators.degraded_doc(args.model, out_dir, exc), 3

    write_json(json_path, doc)
    with open(md_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(indicator_report.render_md(doc))

    problems = gate(doc)
    if problems:
        print("[06] 闸门未通过：" + "；".join(problems), file=sys.stderr)
        return 3
    if status:
        return status
    values = {m["id"]: m["value"] for m in doc["metrics"]}
    scope = doc["scope"]

    def fmt(v):
        return "null" if v is None else (f"{v:.2%}" if isinstance(v, float) else str(v))

    print(f"[06] 完成：范围 {scope['areaM2']} m² / 有效格 {scope['validCells']} / mask 格 {scope['maskCells']}"
          f" / 地面 z={scope['groundZ']} m；建筑密度 {values['building_coverage_ratio']} / "
          f"栋数 {values['building_count']} / P50 {values['building_height_p50']} m / "
          f"P90 {values['building_height_p90']} m / 容积率 {values['floor_area_ratio_est']} / "
          f"绿化 {fmt(values['greenery_ratio'])} / 道路 {fmt(values['road_ratio'])}；"
          f"usable={doc['quality']['usable']}")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
