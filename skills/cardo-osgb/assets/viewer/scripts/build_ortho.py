# -*- coding: utf-8 -*-
"""正射影像与分类栅格的包内生成（只读模型）。

输入：项目包 project.json 的 build.rasterFromModel.manifest（模型清单）+ 同目录 b3dm
输出：build.rasterFromModel.outDir 下
  ortho_northup.png   顶面正射（north-up：行=y 递减、列=x 递增）
  class_rgba.png      四类分类栅格（0 其它透明 / 1 绿化 / 2 道路 / 3 屋面）
  ortho_georef.json / class_georef.json  六参数与像素坐标口径
坐标系与原点取自 project.json 的 crs 段，不在脚本里另写一套。

算法：tex_mosaic（细层优先、同层取最高点）+ tex_classify（可见光指数 + 形态学）。
本脚本不改动模型目录，不联网，不依赖包外路径。
"""
from __future__ import annotations

import json
import os
import sys
import time

import numpy as np
from PIL import Image

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import tex_classify  # noqa: E402
import tex_mosaic  # noqa: E402

RES = 0.25
CLASS_RGBA = {
    1: (58, 160, 74, 255),    # 绿化 #3aa04a
    2: (120, 120, 120, 255),  # 道路 #787878
    3: (200, 120, 60, 255),   # 建筑屋面 #c8783c
}
EMPTY = (150, 150, 150, 0)


def _georef(kind, x0, y0, nx, ny, res, crs, model_rel):
    x1, y1 = x0 + nx * res, y0 + ny * res
    return {
        "schema": "cardo/gis-layer-georef/v1",
        "resM": res,
        "grid": {"nx": nx, "ny": ny, "axisOrder": "north-up: row=y 递减, col=x 递增"},
        "extentLocalM": [round(x0, 3), round(y0, 3), round(x1, 3), round(y1, 3)],
        "crs": {
            "epsg": crs["epsg"],
            "name": crs["name"],
            "vertical": crs.get("vertical", ""),
            "originEN": [crs["origin"]["E"], crs["origin"]["N"]],
            "localToProjected": "E = originE + x; N = originN + y",
        },
        "pixelToLocal": {
            "x": f"x = {round(x0, 3)} + col * {res}",
            "y": f"y = {round(y0, 3)} + ({ny} * {res}) - row * {res}",
        },
        "worldFile": {"A": res, "D": 0.0, "B": 0.0, "E": -res,
                      "C": round(x0 + res / 2, 3), "F": round(y1 - res / 2, 3)},
        "source": {"kind": "generated-in-package",
                   "by": "scripts/build_ortho.py",
                   "model": model_rel},
        "layer": kind,
        "file": "ortho_northup.png" if kind == "ortho" else "class_rgba.png",
        "bandOrder": "RGB" if kind == "ortho" else "RGBA(类别色, 0 类 alpha=0)",
    }


def build(pkg_dir, cfg, force=False):
    build_cfg = (cfg.get("build") or {}).get("rasterFromModel")
    if not build_cfg:
        raise ValueError("项目包未配置 build.rasterFromModel，不能从模型生成栅格")
    res = float(build_cfg.get("resM") or RES)
    manifest_rel = build_cfg["manifest"].replace("\\", "/")
    out_rel = build_cfg["outDir"].replace("\\", "/")
    model_path = os.path.join(pkg_dir, *manifest_rel.split("/"))
    out_dir = os.path.join(pkg_dir, *out_rel.split("/"))
    ortho_png = os.path.join(out_dir, "ortho_northup.png")
    class_png = os.path.join(out_dir, "class_rgba.png")
    if not force and os.path.isfile(ortho_png) and os.path.isfile(class_png):
        return {"skipped": True, "seconds": 0.0, "reason": "栅格已存在（--rebuild 可强制重算）"}
    if not os.path.isfile(model_path):
        raise FileNotFoundError("缺模型清单：" + manifest_rel)
    os.makedirs(out_dir, exist_ok=True)

    t0 = time.perf_counter()
    ortho, zr, cnt, _asum, meta, model = tex_mosaic.build(model_path, res=res)
    _ground, height, building = tex_mosaic.ground_and_mask(zr, res)
    labels, score = tex_classify.classify(tex_classify.features(ortho), height, building)

    nx, ny = ortho.shape[0], ortho.shape[1]
    rgb = np.flipud(np.transpose(ortho, (1, 0, 2))).astype(np.uint8)
    lab = np.flipud(np.transpose(labels))
    rgba = np.zeros((ny, nx, 4), dtype=np.uint8)
    rgba[:, :] = EMPTY
    for code, color in CLASS_RGBA.items():
        rgba[lab == code] = color

    Image.fromarray(rgb, "RGB").save(ortho_png, optimize=True)
    Image.fromarray(rgba, "RGBA").save(class_png, optimize=True)

    x0, y0 = meta["extent"][0], meta["extent"][1]
    crs = cfg["crs"]
    for kind, name in (("ortho", "ortho_georef.json"), ("class", "class_georef.json")):
        with open(os.path.join(out_dir, name), "w", encoding="utf-8") as fh:
            json.dump(_georef(kind, x0, y0, nx, ny, res, crs, manifest_rel), fh,
                      ensure_ascii=False, indent=1)

    sec = round(time.perf_counter() - t0, 1)
    rec = {
        "schema": "cardo/viewer-raster-build/v1",
        "seconds": sec,
        "resM": res,
        "grid": [nx, ny],
        "tiles": meta["tiles"],
        "tilesRead": meta["tilesRead"],
        "pixelsObserved": meta["pixelsObserved"],
        "textureImages": meta["textureImages"],
        "textureFailed": len(meta["textureFailed"]),
        "lodLevels": meta["lodLevels"],
        "classPixels": score,
        "note": "只读项目包内模型；本记录不含绝对路径。",
    }
    with open(os.path.join(out_dir, "生成记录.json"), "w", encoding="utf-8") as fh:
        json.dump(rec, fh, ensure_ascii=False, indent=1)
    return {"skipped": False, "seconds": sec, "record": rec}


def main():
    root = os.path.dirname(HERE)
    force = "--rebuild" in sys.argv
    project = None
    if "--project" in sys.argv:
        i = sys.argv.index("--project")
        project = sys.argv[i + 1] if i + 1 < len(sys.argv) else None
    import project_pack
    try:
        pkg = project_pack.load(root, project)
        r = build(pkg.dir, pkg.config, force=force)
    except Exception as exc:  # 交给入口统一分档
        print(f"[FAIL] 栅格生成失败：{type(exc).__name__}: {exc}")
        return 1
    if r["skipped"]:
        print(f"[SKIP] {r['reason']}")
    else:
        print(f"[OK] 正射与分类栅格已生成，耗时 {r['seconds']} 秒"
              f"（{r['record']['tilesRead']}/{r['record']['tiles']} 瓦片，"
              f"观测像元 {r['record']['pixelsObserved']}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
