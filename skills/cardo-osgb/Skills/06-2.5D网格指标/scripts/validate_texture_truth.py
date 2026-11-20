# -*- coding: utf-8 -*-
"""纹理/材质 P0 真值校核工具。

用途：
  1) 生成 1 m 正射影像（每格平均 RGB）与逐格分类图，供人工勾绘真值；
  2) 读取人工真值（cardo/texture-truth/v1），在人工片区内计算绿化/道路 IoU。

真值 JSON 示例：
{
  "schema": "cardo/texture-truth/v1",
  "cellM": 1.0,
  "domain": {"rect": [x0, y0, x1, y1]},
  "regions": [
    {"id": "lawn-1", "class": "green", "rect": [x0, y0, x1, y1]},
    {"id": "path-1", "class": "road", "polygon": [[x, y], ...]}
  ]
}
坐标与 cardo-lowair-model/v1 的 geometry.bboxLocal 同系（米）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.dont_write_bytecode = True
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    from PIL import Image, ImageDraw
except ImportError:  # pragma: no cover
    Image = ImageDraw = None

import texture_materials


def _in_rect(x, y, rect):
    x0, y0, x1, y1 = rect
    return min(x0, x1) <= x <= max(x0, x1) and min(y0, y1) <= y <= max(y0, y1)


def _in_poly(x, y, poly):
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if ((y1 > y) != (y2 > y)) and (x < (x2 - x1) * (y - y1) / (y2 - y1 + 1e-12) + x1):
            inside = not inside
    return inside


def _in_region(x, y, region):
    if "rect" in region:
        return _in_rect(x, y, region["rect"])
    if "polygon" in region:
        return _in_poly(x, y, region["polygon"])
    return False


def _iou(tp, fp, fn):
    den = tp + fp + fn
    return (tp / den) if den else None


def _render(result, out_dir, cell_m):
    per = result.get("perCell") or []
    if not per or Image is None:
        return None
    xs = [c["x"] for c in per]
    ys = [c["y"] for c in per]
    x0, x1 = min(xs) - cell_m / 2, max(xs) + cell_m / 2
    y0, y1 = min(ys) - cell_m / 2, max(ys) + cell_m / 2
    nx = max(1, int(round((x1 - x0) / cell_m)))
    ny = max(1, int(round((y1 - y0) / cell_m)))

    rgb = Image.new("RGB", (nx, ny), (235, 235, 235))
    cls = Image.new("RGB", (nx, ny), (235, 235, 235))
    pr = rgb.load()
    pc = cls.load()
    for c in per:
        ix = int(round((c["x"] - x0) / cell_m - 0.5))
        iy = int(round((c["y"] - y0) / cell_m - 0.5))
        if not (0 <= ix < nx and 0 <= iy < ny):
            continue
        r, g, b = c["rgb"]
        pr[ix, ny - 1 - iy] = (r, g, b)
        if c.get("building"):
            color = (70, 70, 70)
        elif c["greenShare"] >= 0.5:
            color = (40, 170, 60)
        elif c["roadShare"] >= 0.5:
            color = (80, 110, 170)
        else:
            color = (205, 180, 90)
        pc[ix, ny - 1 - iy] = color

    scale = max(1, int(round(720.0 / max(nx, ny))))
    big = rgb.resize((nx * scale, ny * scale), Image.NEAREST)
    bigc = cls.resize((nx * scale, ny * scale), Image.NEAREST)
    draw = ImageDraw.Draw(big)
    drawc = ImageDraw.Draw(bigc)
    step = max(1, int(round(10 / cell_m)))
    for i in range(0, nx + 1, step):
        X = i * scale
        draw.line([(X, 0), (X, ny * scale)], fill=(255, 0, 0), width=1)
        drawc.line([(X, 0), (X, ny * scale)], fill=(255, 0, 0), width=1)
        if i < nx:
            draw.text((X + 2, 2), f"{x0 + i * cell_m:.0f}", fill=(255, 0, 0))
    for j in range(0, ny + 1, step):
        Y = (ny - j) * scale
        draw.line([(0, Y), (nx * scale, Y)], fill=(255, 0, 0), width=1)
        drawc.line([(0, Y), (nx * scale, Y)], fill=(255, 0, 0), width=1)
        if j < ny:
            draw.text((2, max(0, Y - 12)), f"{y0 + j * cell_m:.0f}", fill=(255, 0, 0))
    p1 = os.path.join(out_dir, "正射影像.png")
    p2 = os.path.join(out_dir, "分类影像.png")
    big.save(p1)
    bigc.save(p2)
    return {"orthophoto": p1, "classification": p2, "grid": [nx, ny], "extent": [x0, y0, x1, y1],
            "legend": {"green": "greenShare≥0.5", "blue": "roadShare≥0.5",
                       "dark": "建筑掩膜", "yellow": "其它"}}


def _truth_eval(result, truth_path):
    with open(truth_path, encoding="utf-8") as fh:
        truth = json.load(fh)
    if truth.get("schema") != "cardo/texture-truth/v1":
        raise ValueError(f"truth schema 非 cardo/texture-truth/v1：{truth.get('schema')}")
    per = result.get("perCell") or []
    domain = truth.get("domain") or {}
    regions = truth.get("regions") or []
    stats = {k: {"tp": 0, "fp": 0, "fn": 0, "truth": 0, "pred": 0} for k in ("green", "road")}
    pix = {k: {"tp": 0.0, "fp": 0.0, "fn": 0.0, "truth": 0.0, "pred": 0.0} for k in ("green", "road")}
    used = 0
    for c in per:
        x, y = c["x"], c["y"]
        if "rect" in domain and not _in_rect(x, y, domain["rect"]):
            continue
        if "polygon" in domain and not _in_poly(x, y, domain["polygon"]):
            continue
        used += 1
        t_road = any(r.get("class") == "road" and _in_region(x, y, r) for r in regions)
        t_green = (not t_road) and any(r.get("class") == "green" and _in_region(x, y, r) for r in regions)
        for cls in ("green", "road"):
            t = t_green if cls == "green" else t_road
            p = (c["greenShare"] >= 0.5) if cls == "green" else (c["roadShare"] >= 0.5)
            s = stats[cls]
            s["truth"] += int(t)
            s["pred"] += int(p)
            if t and p:
                s["tp"] += 1
            elif p and not t:
                s["fp"] += 1
            elif t and not p:
                s["fn"] += 1
            # 像素级（面积加权）IoU：greenShare/roadShare 是格内面积占比，权重取该格面积
            n = float(c.get("areaM2") or c["samples"])
            frac = (c["greenShare"] if cls == "green" else c["roadShare"]) * n
            q = pix[cls]
            q["pred"] += frac
            if t:
                q["tp"] += frac
                q["fn"] += n - frac
                q["truth"] += n
            else:
                q["fp"] += frac
    out = {"schema": "cardo/texture-truth-result/v1", "domainCells": used,
           "cellM": truth.get("cellM", 1.0), "classes": {}, "pixel": {}}
    for cls, s in stats.items():
        iou = _iou(s["tp"], s["fp"], s["fn"])
        precision = s["tp"] / s["pred"] if s["pred"] else None
        recall = s["tp"] / s["truth"] if s["truth"] else None
        out["classes"][cls] = {"iou": None if iou is None else round(iou, 4),
                               "precision": None if precision is None else round(precision, 4),
                               "recall": None if recall is None else round(recall, 4),
                               **s}
    for cls, q in pix.items():
        iou = _iou(q["tp"], q["fp"], q["fn"])
        out["pixel"][cls] = {
            "iou": None if iou is None else round(iou, 4),
            "precision": round(q["tp"] / q["pred"], 4) if q["pred"] else None,
            "recall": round(q["tp"] / q["truth"], 4) if q["truth"] else None,
            "tp": round(q["tp"]), "fp": round(q["fp"]), "fn": round(q["fn"]),
            "truthSamples": round(q["truth"]), "predSamples": round(q["pred"]),
        }
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="纹理/材质 P0 真值校核")
    ap.add_argument("--model", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--truth", default=None)
    ap.add_argument("--cell-m", type=float, default=1.0)
    args = ap.parse_args(argv)
    os.makedirs(args.out, exist_ok=True)
    result = texture_materials.analyze(args.model, return_cells=True, cell_m=args.cell_m)
    if result.get("status") != "ok":
        print(f"[truth] 纹理分析未出数：{result.get('reason')}", file=sys.stderr)
        return 3
    with open(os.path.join(args.out, "逐格纹理分类.json"), "w", encoding="utf-8") as fh:
        json.dump({"greenery": result["greenery"], "road": result["road"],
                   "params": result["params"], "diagnostics": result["diagnostics"],
                   "perCell": result["perCell"]}, fh, ensure_ascii=False, indent=2)
    rendered = _render(result, args.out, args.cell_m)
    print(f"[truth] 正射影像：{rendered['orthophoto'] if rendered else '未生成'}")
    print(f"[truth] 分类影像：{rendered['classification'] if rendered else '未生成'}")
    if args.truth:
        ev = _truth_eval(result, args.truth)
        with open(os.path.join(args.out, "真值校核结果.json"), "w", encoding="utf-8") as fh:
            json.dump(ev, fh, ensure_ascii=False, indent=2)
        print(f"[truth] 像素级 绿化 IoU={ev['pixel']['green']['iou']}（阈值≥0.70）"
              f"｜格级 IoU={ev['classes']['green']['iou']}")
        print(f"[truth] 像素级 道路 IoU={ev['pixel']['road']['iou']}（阈值≥0.60）"
              f"｜格级 IoU={ev['classes']['road']['iou']}")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
