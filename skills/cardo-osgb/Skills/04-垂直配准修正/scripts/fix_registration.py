#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""04 · 垂直配准修正：把 SRSOrigin.z × up(lat,lon) 叠加进 root.transform 平移。

背景：3dtile.exe 按 ECEF(原点, h=0) 写 root.transform，整体比真实高程低 |SRSOrigin.z|；
本步沿该点 up 方向补 +z，并把修正量写成可复算的 JSON。

输入：03 的 转换日志.json（crs.origin）+ 转换产物/tileset.json
输出：过程文件/04-垂直配准修正/配准修正.json（before/after/delta/up/applied/offset/夹角）
幂等：同一产物重复执行不会二次叠加（比对修正前后平移量）。
退出码：0 成功；2 输入缺失；3 transform 缺失/异常；4 状态不明（平移量既不等于修正前也不等于修正后）。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys

WGS84_A, WGS84_F = 6378137.0, 1.0 / 298.257222101
WGS84_E2 = 2 * WGS84_F - WGS84_F ** 2
TOL_M = 0.01
SCHEMA = "cardo/lowair-registration/v1"


def read_json(path: str) -> dict:
    with open(path, encoding="utf-8-sig") as fh:
        return json.load(fh)


def write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def inverse_geodetic(x: float, y: float, z: float) -> tuple[float, float]:
    lon, p = math.atan2(y, x), math.hypot(x, y)
    lat = math.atan2(z, p * (1.0 - WGS84_E2))
    for _ in range(8):
        n = WGS84_A / math.sqrt(1.0 - WGS84_E2 * math.sin(lat) ** 2)
        h = p / math.cos(lat) - n
        lat = math.atan2(z, p * (1.0 - WGS84_E2 * n / (n + h)))
    return lat, lon


def geodetic_up(lat: float, lon: float) -> list[float]:
    return [math.cos(lat) * math.cos(lon), math.cos(lat) * math.sin(lon), math.sin(lat)]


def main() -> int:
    ap = argparse.ArgumentParser(description="04 · 垂直配准修正")
    ap.add_argument("--convert-log", default=os.path.join("过程文件", "03-OSGB转3DTiles", "转换日志.json"))
    ap.add_argument("--products", default=os.path.join("过程文件", "03-OSGB转3DTiles", "转换产物"))
    ap.add_argument("--out", default=os.path.join("过程文件", "04-垂直配准修正", "配准修正.json"))
    args = ap.parse_args()

    if not os.path.isfile(args.convert_log):
        print(f"[04] 缺 03 转换日志：{args.convert_log}", file=sys.stderr)
        return 2
    log = read_json(args.convert_log)
    origin = (log.get("crs") or {}).get("origin")
    if not origin or len(origin) != 3:
        print("[04] 转换日志缺 crs.origin（三维 SRSOrigin）", file=sys.stderr)
        return 2
    tileset = os.path.join(args.products, "tileset.json")
    if not os.path.isfile(tileset):
        print(f"[04] 缺 {tileset}", file=sys.stderr)
        return 2
    with open(tileset, encoding="utf-8") as fh:
        data = json.load(fh)
    root = data.get("root") or {}
    transform = root.get("transform")
    if not isinstance(transform, list) or len(transform) != 16:
        print("[04] root.transform 缺失或长度不为 16", file=sys.stderr)
        return 3

    before = [float(transform[12]), float(transform[13]), float(transform[14])]
    lat, lon = inverse_geodetic(*before)
    up = geodetic_up(lat, lon)
    dz = float(origin[2])
    after = [before[k] + dz * up[k] for k in range(3)]
    delta = [dz * up[k] for k in range(3)]

    marker = os.path.join(args.products, ".work", "04-applied.json")
    applied = "applied"
    if os.path.isfile(marker):
        prev = read_json(marker)
        same_before = all(abs(before[k] - prev["before"][k]) < 1e-6 for k in range(3))
        same_after = all(abs(before[k] - prev["after"][k]) < 1e-6 for k in range(3))
        if same_after and not same_before:
            applied = "already-applied"
            # 报告口径统一为「修正前 → 修正后」：before 取原始平移，after 取当前平移；
            # delta 记本次实际改动量（0），appliedM 仍记该修正的模长，便于 H-05 复核。
            before, after = list(prev["before"]), list(prev["after"])
            delta = [0.0, 0.0, 0.0]
        elif not same_before:
            print("[04] 产物平移量既非修正前也非修正后，拒绝二次修正", file=sys.stderr)
            return 4

    if applied == "applied":
        transform[12], transform[13], transform[14] = after
        root["transform"] = transform
        data["root"] = root
        with open(tileset, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        write_json(marker, {"before": before, "after": after, "offsetM": dz})

    column = [transform[8], transform[9], transform[10]]
    dot = max(-1.0, min(1.0, sum(column[k] * up[k] for k in range(3))))
    displacement = math.dist(before, after)
    recompute_ok = (all(abs(before[k] + dz * up[k] - after[k]) < 1e-9 for k in range(3))
                    and abs(displacement - abs(dz)) < 1e-6)

    doc = {
        "schema": SCHEMA, "schemaVersion": 1,
        "generatedAt": __import__("datetime").datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": {"convertLog": args.convert_log.replace("\\", "/"),
                   "tileset": (args.products.replace("\\", "/") + "/tileset.json")},
        "state": applied,
        "crs": {"srs": (log.get("crs") or {}).get("srs"),
                "vertical": (log.get("crs") or {}).get("vertical"),
                "origin": [round(v, 6) for v in origin]},
        "fixed": True,
        "lat": round(math.degrees(lat), 6), "lon": round(math.degrees(lon), 6),
        "up": up,
        "before": before, "after": after, "delta": delta,
        "appliedM": dz,
        "offsetM": -dz,
        "display": {"before": [round(v, 3) for v in before],
                    "after": [round(v, 3) for v in after],
                    "delta": [round(v, 3) for v in delta],
                    "appliedM": round(dz, 3),
                    "up": [round(v, 9) for v in up]},
        "axisAngleDeg": round(math.degrees(math.acos(dot)), 4),
        "toleranceM": TOL_M,
        "recomputeOk": bool(recompute_ok),
        "notes": (f"工具将 root.transform 平移到 ECEF(原点, h=0)，比真实高程低 {abs(dz):.3f} m"
                  f"（= SRSOrigin.z）；沿 up(lat,lon) 施加 +{dz:.3f} m 后，平移量 {before} → {after}。"
                  f"容差 {TOL_M} m（ECEF→大地坐标迭代与六位舍入可致 ≤1 mm 差）。"),
    }
    write_json(args.out, doc)
    if not recompute_ok:
        print("[04] 修正量不可复算", file=sys.stderr)
        return 3
    print(f"[04] 完成：{applied}，垂直修正 {dz:.3f} m，up={['%.6f' % v for v in up]}，"
          f"平移 {before} → {after}")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.dont_write_bytecode = True
    raise SystemExit(main())
