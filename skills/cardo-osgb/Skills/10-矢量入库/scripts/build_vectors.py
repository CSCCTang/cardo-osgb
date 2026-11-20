#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""10 · 矢量入库：把 09 的候选轮廓做成「能拖进 GIS 直接用的」矢量成果。

读 09 的 `要素矢量.geojson` + `逐栋属性.json`，做**确定性拓扑清理**，然后落盘：
    矢量成果.geojson（EPSG:4544，外环逆时针、闭合、无自相交、无重复）
    属性表.csv（UTF-8 BOM，Excel 双击不乱码）
    字段字典.md（每个字段：类型/单位/来源/怎么用）
    拓扑清理.json（每条规则的输入/输出/丢弃清单，机器可读）
    入库说明.md（QGIS/ArcGIS/PostGIS 怎么导；GPKG 需 GDAL 的实话）

清理规则（全部确定性，不放宽阈值去「凑数」）：
    R1 坐标精度：统一保留 --precision 位小数（默认 3 = 毫米级），坐标在 SQLite/GIS 里无浮点噪声
    R2 环闭合：首尾点不一致就补上首点
    R3 顶点去重：删相邻重复点；去重后不足 4 点（含闭合点）→ 判为退化，丢弃
    R4 自相交：逐段两两求交，命中即判为非法，丢弃（不做猜测式修补）
    R5 方向：外环统一逆时针（RFC 7946 / OGC 右手法则），用鞋带公式判符号后翻转
    R6 面积阈值：鞋带公式重算面积 < --min-area-m2 丢弃；与属性 areaM2 偏差 > 1% 记告警
    R7 重复：几何指纹（顶点序列四舍五入后哈希）相同、或质心距离 < 0.05 m 且面积差 < 1% → 保留先出现的
    R8 属性完备：id / areaM2 / heightP50 / floorsEst / confidence 缺任何一个 → 丢弃（不猜）

只读 09 产物，**不重算高度、不改原始模型**；不跨技能 import。
用法（工作区根）：
    python Skills/10-矢量入库/scripts/build_vectors.py
    python Skills/10-矢量入库/scripts/build_vectors.py --min-area-m2 20 --precision 2 --force
退出码：0 通过；2 缺 09 产物；3 闸门不过（含：清理后一栋不剩、或产物自检失败）；4 输出已存在未给 --force。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import sys

sys.dont_write_bytecode = True
from datetime import datetime

SCHEMA = "cardo/lowair-vector-topo/v1"
SCHEMA_VERSION = 1
GEOJSON_SCHEMA = "cardo/lowair-vector/v1"
PRODUCER, PRODUCER_VERSION = "cardo-osgb", "0.1.0"
METHOD_VERSION = "vector-ingest-v1"
SRC_GEOJSON = os.path.join("过程文件", "09-三维要素反演", "要素矢量.geojson")
SRC_ATTRS = os.path.join("过程文件", "09-三维要素反演", "逐栋属性.json")
OUT_DEFAULT = os.path.join("过程文件", "10-矢量入库")
OUTPUT_NAMES = ("矢量成果.geojson", "属性表.csv", "字段字典.md", "拓扑清理.json", "入库说明.md")
REQUIRED_PROPS = ("id", "areaM2", "heightP50", "floorsEst", "confidence")

FIELDS = [
    ("id", "文本", "—", "09 逐栋编号（B-0001…）",
     "唯一键；同一次运行内稳定，换模型会重排"),
    ("areaM2", "实数", "m²", "格面积累加（1 m 格）",
     "占地投影面积，不是建筑面积；含闭运算补进的格"),
    ("heightP50", "实数", "m", "本栋 nDSM 的 P50",
     "平均高度口径；层数按它算"),
    ("heightP90", "实数", "m", "本栋 nDSM 的 P90", "高区口径，看塔楼/退台"),
    ("heightMax", "实数", "m", "本栋 nDSM 最大值", "受单点噪声影响，只作参考"),
    ("floorsEst", "整数", "层", "round(heightP50 / 3.0)",
     "估算层数，非报建层数"),
    ("floorsRangeLow", "整数", "层", "ceil(heightP50 / 3.3)", "层高 3.3 m 下的下限"),
    ("floorsRangeHigh", "整数", "层", "round(heightP50 / 2.8)", "层高 2.8 m 下的上限"),
    ("centroidX", "实数", "m", "EPSG:4544 东向坐标", "与红线/地形图同一平面坐标系"),
    ("centroidY", "实数", "m", "EPSG:4544 北向坐标", "同上"),
    ("widthM", "实数", "m", "最小外接矩形长边方向尺寸", "OBB 的宽，不是真实开间"),
    ("heightM", "实数", "m", "最小外接矩形短边方向尺寸", "OBB 的高"),
    ("angleDeg", "实数", "度", "OBB 长边相对 X 轴方位角", "正向建筑可用来对齐"),
    ("fillRatio", "实数", "—", "格面积 / OBB 面积",
     "<0.6 说明是 L 形/异形，别当矩形用"),
    ("confidence", "文本", "—", "A/B/C 置信分级", "A 可直用；B 需抽检；C 建议人工看"),
    ("gradeReasons", "文本", "—", "降级理由（分号分隔）", "空串 = 未触发任何降级条件"),
    ("geometryType", "文本", "—", "本步写入：Polygon（OBB 外环）", "均为单环，无洞"),
    ("areaRecalcM2", "实数", "m²", "本步鞋带公式重算的 OBB 外环面积",
     "**不等于** areaM2（占地格面积）；两者比值即 fillRatio 的倒数口径"),
]


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def rel_ref(path, base):
    """相对引用（正斜杠）；跨盘失败时退回文件名，绝不落盘符绝对路径。"""
    try:
        return os.path.relpath(os.path.abspath(path), os.path.abspath(base)).replace("\\", "/")
    except ValueError:
        return os.path.basename(path)


def r2(x):
    return None if x is None else round(float(x), 2)


# ---------------------------------------------------------------- 几何
def ring_area(ring) -> float:
    """鞋带公式（有符号面积的绝对值）。ring 需闭合。"""
    total = 0.0
    for i in range(len(ring) - 1):
        x1, y1 = ring[i]
        x2, y2 = ring[i + 1]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def signed_area(ring) -> float:
    """有符号面积：>0 逆时针，<0 顺时针。"""
    total = 0.0
    for i in range(len(ring) - 1):
        total += ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1]
    return total / 2.0


def orient(p, q, r) -> int:
    val = (q[1] - p[1]) * (r[0] - q[0]) - (q[0] - p[0]) * (r[1] - q[1])
    if abs(val) < 1e-12:
        return 0
    return 1 if val > 0 else -1


def on_segment(p, q, r) -> bool:
    return (min(p[0], r[0]) - 1e-12 <= q[0] <= max(p[0], r[0]) + 1e-12
            and min(p[1], r[1]) - 1e-12 <= q[1] <= max(p[1], r[1]) + 1e-12)


def segments_intersect(a, b, c, d) -> bool:
    """线段 ab 与 cd 是否相交（含端点落在对方线段上；用于判自相交，O(n²) 但 n≤5）。"""
    o1, o2 = orient(a, b, c), orient(a, b, d)
    o3, o4 = orient(c, d, a), orient(c, d, b)
    if o1 != o2 and o3 != o4:
        return True
    if o1 == 0 and on_segment(a, c, b):
        return True
    if o2 == 0 and on_segment(a, d, b):
        return True
    if o3 == 0 and on_segment(c, a, d):
        return True
    if o4 == 0 and on_segment(c, b, d):
        return True
    return False


def self_intersects(ring) -> bool:
    """闭合环自相交：跳过相邻边（共享端点）与首尾相邻边。"""
    pts = ring[:-1] if len(ring) > 1 and ring[0] == ring[-1] else list(ring)
    n = len(pts)
    if n < 4:
        return False
    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        for j in range(i + 1, n):
            if j == i or (j + 1) % n == i or (i + 1) % n == j:
                continue
            c, d = pts[j], pts[(j + 1) % n]
            if segments_intersect(a, b, c, d):
                return True
    return False


def round_ring(ring, nd):
    return [[round(float(p[0]), nd), round(float(p[1]), nd)] for p in ring]


def dedupe_ring(ring):
    out = []
    for p in ring:
        if not out or p != out[-1]:
            out.append(p)
    return out


def fingerprint(ring, nd) -> str:
    """几何指纹：坐标精度化后的顶点序列哈希（用来抓完全重复的要素）。"""
    payload = ";".join(f"{p[0]},{p[1]}" for p in round_ring(ring, nd))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- 清理
def clean(features, precision: int, min_area: float, area_tol: float) -> tuple:
    """逐要素清理 + 去重。返回 (保留要素, 丢弃清单, 告警清单)。"""
    kept, dropped, warnings, seen_fp, seen_key = [], [], [], {}, set()
    for item in features:
        fid = item.get("id") or (item.get("properties") or {}).get("id") or "?"
        geom = item.get("geometry") or {}
        props = item.get("properties") or {}
        reasons = []

        missing = [k for k in REQUIRED_PROPS if props.get(k) is None]
        if missing:
            dropped.append({"id": fid, "rule": "R8", "why": f"缺必需属性 {missing}"})
            continue
        if geom.get("type") != "Polygon":
            dropped.append({"id": fid, "rule": "R0", "why": f"几何类型 {geom.get('type')} 不是 Polygon"})
            continue
        rings = geom.get("coordinates") or []
        if not rings:
            dropped.append({"id": fid, "rule": "R0", "why": "没有外环"})
            continue

        ring = round_ring(rings[0], precision)            # R1 精度
        ring = dedupe_ring(ring)                           # R3 相邻重复点
        closed = list(ring)                                # R2 环闭合
        if closed and closed[0] != closed[-1]:
            closed.append(closed[0])
        closed = dedupe_ring(closed)
        if len(closed) < 4:
            dropped.append({"id": fid, "rule": "R3", "why": f"去重后仅 {len(closed)} 点，几何退化"})
            continue
        if self_intersects(closed):                        # R4 自相交
            dropped.append({"id": fid, "rule": "R4", "why": "外环自相交"})
            continue
        if signed_area(closed) < 0:                        # R5 外环逆时针
            closed = list(reversed(closed))
            reasons.append("外环已翻转为逆时针")

        area_calc = ring_area(closed)                      # R6 面积阈值 + 对账
        if area_calc < min_area:
            dropped.append({"id": fid, "rule": "R6",
                            "why": f"重算面积 {area_calc:.2f} m² < 阈值 {min_area} m²"})
            continue
        # 对账口径：多边形是 OBB，所以拿「widthM × heightM」对，而不是拿格面积 areaM2 对
        if props.get("widthM") is not None and props.get("heightM") is not None:
            claimed = float(props["widthM"]) * float(props["heightM"])
            if claimed > 0 and abs(area_calc - claimed) / claimed > area_tol:
                warnings.append({"id": fid, "rule": "R6",
                                 "why": f"鞋带面积 {area_calc:.2f} m² 与 OBB 尺寸乘积 {claimed:.2f} m² "
                                        f"偏差 {abs(area_calc - claimed) / claimed:.1%} > {area_tol:.0%}"})

        fp = fingerprint(closed, precision)                # R7 重复几何
        if fp in seen_fp:
            dropped.append({"id": fid, "rule": "R7", "why": f"几何与 {seen_fp[fp]} 完全重复"})
            continue
        cx = sum(p[0] for p in closed[:-1]) / max(1, len(closed) - 1)
        cy = sum(p[1] for p in closed[:-1]) / max(1, len(closed) - 1)
        dup_of = None
        for other_id, (ox, oy, oarea) in seen_key:
            if (math.hypot(cx - ox, cy - oy) < 0.05
                    and abs(oarea - area_calc) / max(area_calc, 1e-9) < 0.01):
                dup_of = other_id
                break
        if dup_of:
            dropped.append({"id": fid, "rule": "R7", "why": f"与 {dup_of} 质心/面积重合"})
            continue
        seen_fp[fp] = fid
        seen_key.add((fid, (cx, cy, area_calc)))

        props = dict(props)
        props["geometryType"] = "Polygon"
        props["areaRecalcM2"] = r2(area_calc)
        kept.append({"type": "Feature", "id": fid,
                     "geometry": {"type": "Polygon", "coordinates": [closed]},
                     "properties": props,
                     "_notes": reasons})
    return kept, dropped, warnings


def build_geojson(kept, src) -> dict:
    crs = src.get("crs")
    return {
        "type": "FeatureCollection",
        "schema": GEOJSON_SCHEMA,
        "name": "cardo-buildings-vector",
        "crs": crs,
        "metadata": {
            "generatedAt": now_iso(),
            "producer": f"{PRODUCER}/{PRODUCER_VERSION}",
            "methodVersion": METHOD_VERSION,
            "featureCount": len(kept),
            "geometry": "每栋的最小外接矩形（OBB）外环，单环无洞；不是建筑真实边界",
            "warning": (src.get("metadata") or {}).get("warning"),
            "coordinateMapping": (src.get("metadata") or {}).get("coordinateMapping"),
            "topology": "外环逆时针、首尾闭合、无自相交、无重复、坐标已按固定精度取整",
        },
        "features": [{"type": "Feature", "id": f["id"],
                      "geometry": f["geometry"],
                      "properties": {k: v for k, v in f["properties"].items()}} for f in kept],
    }


def csv_rows(kept) -> list:
    rows = []
    for item in kept:
        p = item["properties"]
        ring = item["geometry"]["coordinates"][0]
        cx = sum(q[0] for q in ring[:-1]) / max(1, len(ring) - 1)
        cy = sum(q[1] for q in ring[:-1]) / max(1, len(ring) - 1)
        floors = p.get("floorsRange") or [None, None]
        reasons = p.get("gradeReasons") or ""
        rows.append({
            "id": p["id"], "areaM2": p["areaM2"], "heightP50": p["heightP50"],
            "heightP90": p["heightP90"], "heightMax": p["heightMax"],
            "floorsEst": p["floorsEst"],
            "floorsRangeLow": floors[0], "floorsRangeHigh": floors[1],
            "centroidX": round(cx, 3), "centroidY": round(cy, 3),
            "widthM": p.get("widthM"), "heightM": p.get("heightM"),
            "angleDeg": p.get("angleDeg"), "fillRatio": p.get("fillRatio"),
            "confidence": p["confidence"],
            "gradeReasons": reasons if isinstance(reasons, str) else "；".join(reasons),
            "geometryType": p.get("geometryType"), "areaRecalcM2": p.get("areaRecalcM2"),
        })
    return rows


def write_csv(path: str, rows) -> None:
    keys = [name for name, *_ in FIELDS]
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:   # BOM：Excel 双击不乱码
        writer = csv.DictWriter(fh, fieldnames=keys)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k) for k in keys})


def render_field_dict(doc) -> str:
    lines = ["# 字段字典（10 · 矢量入库）", "",
             f"- 生成：{doc['generatedAt']}　要素数：{doc['result']['featureCount']}",
             f"- 坐标系：`{doc['crs']}`　几何：OBB 外环（逆时针、闭合）",
             "", "| 字段 | 类型 | 单位 | 来源 | 怎么用 |", "|---|---|---|---|---|"]
    for name, typ, unit, src, how in FIELDS:
        lines.append(f"| `{name}` | {typ} | {unit} | {src} | {how} |")
    lines += ["", "## 口径提醒", "",
              "- `areaM2` 是**占地投影面积**，不是建筑面积；建筑面积看 `floorsEst × areaM2`（粗估）。",
              "- `areaRecalcM2` 是本步按多边形鞋带公式重算的 **OBB 外环面积**，通常大于 `areaM2`"
              "（OBB 把 L 形外扩了）；两者不要混用。",
              "- 高度全部来自 b3dm 顶点的 nDSM（离地高度），**没有真值校核**；不要当竣工实测。",
              "- `confidence=C` 的要素建议人工确认后再入正式库；`gradeReasons` 写了为什么降级。",
              "- 轮廓是 OBB：L 形/弧形建筑会被外扩，`fillRatio` < 0.6 时面积偏大更明显。", ""]
    return "\n".join(lines)


def render_readme(doc) -> str:
    r = doc["result"]
    clean = doc["cleaning"]
    lines = ["# 入库说明（10 · 矢量入库）", "",
             f"- 生成：{doc['generatedAt']}　来源：`{doc['source']['geojson']}`",
             f"- 要素：**{r['featureCount']} 个**（输入 {clean['inputCount']} 个，丢弃 {clean['droppedCount']} 个，"
             f"告警 {clean['warningCount']} 条）",
             f"- 坐标系：`{doc['crs']}`（投影坐标，单位米；不是 WGS84 经纬度）",
             "", "## 产物", "",
             "| 文件 | 给谁用 |", "|---|---|",
             "| `矢量成果.geojson` | QGIS / ArcGIS / PostGIS / 前端 Mapbox-GL 直接读 |",
             "| `属性表.csv` | Excel 打开（UTF-8 BOM，不乱码）；也方便丢进数据库 |",
             "| `字段字典.md` | 交付时随数据一起给，说明每个字段什么意思 |",
             "| `拓扑清理.json` | 质控记录：每条规则处理了什么、丢了谁、为什么 |",
             "", "## 拓扑清理做了什么", ""]
    for rule in doc["rules"]:
        lines.append(f"- **{rule['id']} {rule['name']}**：{rule['detail']}　本次命中 {rule['hits']} 条")
    if clean["droppedCount"]:
        lines += ["", "### 被丢弃的要素", "", "| id | 规则 | 原因 |", "|---|---|---|"]
        for item in clean["dropped"][:50]:
            lines.append(f"| {item['id']} | {item['rule']} | {item['why']} |")
    else:
        lines += ["", "> 本次没有要素被丢弃（输入已基本干净）。"]
    if clean["warningCount"]:
        lines += ["", "### 告警（保留但需注意）", "", "| id | 规则 | 原因 |", "|---|---|---|"]
        for item in clean["warnings"][:50]:
            lines.append(f"| {item['id']} | {item['rule']} | {item['why']} |")
    lines += ["", "## 怎么导进 GIS", "",
              "### QGIS（最省事）",
              "1. 图层 → 添加图层 → 添加矢量图层 → 选 `矢量成果.geojson`；",
              "2. 图层属性 → 源 → 确认坐标系为 `EPSG:4544`（若提示坐标是经纬度，说明 crs 声明没被识别，"
              "手动指定 4544 即可）；",
              "3. 属性表可直接按 `confidence` 过滤，先看 A/B。", "",
              "### ArcGIS Pro",
              "1. 地图 → 添加数据 → 选 GeoJSON（Pro 3.x 原生支持）；",
              "2. 投影设为 CGCS2000 3-degree Gauss-Kruger CM 117E（即 EPSG:4544）；", "",
              "### PostGIS",
              "```sql",
              "-- 需 ogr2ogr（GDAL）导入，或用 QGIS 导出为 SQL",
              "SELECT ST_Area(geom) FROM buildings;  -- 与 areaM2 对账，单位 m²",
              "```", "",
              "## 关于 GPKG（GeoPackage，.gpkg）", "",
              "**本技能不生成 GPKG**：写 GPKG 需要 GDAL/OGR（Python 侧是 `fiona`/`osgeo`，"
              "或命令行 `ogr2ogr`），本机默认环境没有装，也不在 `requirements.txt` 里强求。",
              "要的话两条路：",
              "1. `ogr2ogr -f GPKG 矢量成果.gpkg 矢量成果.geojson`（装好 GDAL 后一条命令）；",
              "2. QGIS 里右键图层 → 导出 → 要素另存为 → 格式选 GeoPackage。",
              "两者都**不会**改变几何与属性，只是换容器。", "",
              "## 已知局限（别忽略）", "",
              "- 几何是 **OBB 最小外接矩形**，不是建筑真实轮廓；异形建筑面积偏大。",
              "- 高度来自几何反演，**无真值校核**；报告里的区间是方法敏感性，不是精度。",
              "- 同一地块的相邻建筑可能被分水岭拆开或合并，`confidence=C` 的请人工核。",
              "- 坐标是投影坐标（EPSG:4544），不是 RFC 7946 的 WGS84；部分 Web 工具会拒绝"
              "（Mapbox/Leaflet 需先转 4326）。", ""]
    return "\n".join(lines)


RULES = [
    {"id": "R1", "name": "坐标精度", "detail": "坐标统一保留固定小数位（默认 3 位 = 毫米级），去掉浮点尾噪声"},
    {"id": "R2", "name": "环闭合", "detail": "外环首尾点不一致时补上首点，保证闭合"},
    {"id": "R3", "name": "顶点去重", "detail": "删相邻重复点；去重后不足 4 点判为退化并丢弃"},
    {"id": "R4", "name": "自相交", "detail": "逐段两两求交，命中即丢弃（不做猜测式修补）"},
    {"id": "R5", "name": "外环方向", "detail": "外环统一逆时针（OGC 右手法则），顺时针的翻转"},
    {"id": "R6", "name": "面积阈值与对账",
     "detail": "鞋带公式重算 OBB 面积，太小丢弃；与 widthM×heightM 偏差 >1% 记告警"},
    {"id": "R7", "name": "重复要素", "detail": "几何指纹相同、或质心 <0.05 m 且面积差 <1%，只保留先出现的"},
    {"id": "R8", "name": "属性完备", "detail": "id/areaM2/heightP50/floorsEst/confidence 缺任一即丢弃"},
]


# ---------------------------------------------------------------- 闸门
def gate(doc, geojson, rows) -> list:
    """产物自检（回读后的对象，不是内存里的中间态）。"""
    problems = []
    if doc.get("schema") != SCHEMA:
        problems.append(f"schema 非 {SCHEMA}")
    if not doc.get("quality", {}).get("usable"):
        problems.append("quality.usable=false")
    if geojson.get("schema") != GEOJSON_SCHEMA:
        problems.append(f"geojson schema 非 {GEOJSON_SCHEMA}")
    feats = geojson.get("features") or []
    if not feats:
        problems.append("清理后一个要素都不剩")
    ids, fps = set(), set()
    for feat in feats:
        fid = feat.get("id")
        props = feat.get("properties") or {}
        if fid in ids:
            problems.append(f"id 重复：{fid}")
        ids.add(fid)
        missing = [k for k in REQUIRED_PROPS if props.get(k) is None]
        if missing:
            problems.append(f"{fid} 缺属性 {missing}")
        rings = (feat.get("geometry") or {}).get("coordinates") or []
        if not rings:
            problems.append(f"{fid} 没有外环")
            continue
        ring = rings[0]
        if ring[0] != ring[-1]:
            problems.append(f"{fid} 外环未闭合")
        if len(ring) < 4:
            problems.append(f"{fid} 外环顶点 <4")
        if signed_area(ring) <= 0:
            problems.append(f"{fid} 外环不是逆时针")
        if self_intersects(ring):
            problems.append(f"{fid} 外环自相交")
        fp = fingerprint(ring, 6)
        if fp in fps:
            problems.append(f"{fid} 几何重复")
        fps.add(fp)
    if len(rows) != len(feats):
        problems.append(f"属性表行数 {len(rows)} ≠ 要素数 {len(feats)}")
    return problems


def crs_label(src: dict, attrs: dict) -> str | None:
    """从 09 产物的 crs 成员/属性里取「EPSG:xxxx」这种可读写法（不猜坐标系）。"""
    name = (((src.get("crs") or {}).get("properties") or {}).get("name") or "").strip()
    if name:
        tail = name.rsplit(":", 1)[-1].strip()
        return f"EPSG:{tail}" if tail.isdigit() else name
    raw = (attrs.get("crs") or {}).get("srs") if isinstance(attrs.get("crs"), dict) else attrs.get("crs")
    return str(raw) if raw else None


def run(src_geojson: str, src_attrs: str, out_dir: str, precision: int,
        min_area: float, area_tol: float) -> dict:
    with open(src_geojson, encoding="utf-8") as fh:
        src = json.load(fh)
    attrs = {}
    if os.path.isfile(src_attrs):
        with open(src_attrs, encoding="utf-8") as fh:
            loaded = json.load(fh)
        attrs = loaded if isinstance(loaded, dict) else {"buildings": loaded}
    features = src.get("features") or []
    kept, dropped, warnings = clean(features, precision, min_area, area_tol)
    hits = {rule["id"]: 0 for rule in RULES}
    for item in dropped:
        hits[item["rule"]] = hits.get(item["rule"], 0) + 1
    hits["R6"] += sum(1 for w in warnings if w["rule"] == "R6")

    geojson = build_geojson(kept, src)
    rows = csv_rows(kept)
    doc = {
        "schema": SCHEMA, "schemaVersion": SCHEMA_VERSION, "generatedAt": now_iso(),
        "producer": {"name": PRODUCER, "version": PRODUCER_VERSION},
        "methodVersion": METHOD_VERSION,
        "source": {"geojson": rel_ref(src_geojson, out_dir),
                   "attrs": rel_ref(src_attrs, out_dir) if os.path.isfile(src_attrs) else None,
                   "sourceFeatureCount": len(features),
                   "sourceMethodVersion": (src.get("metadata") or {}).get("methodVersion")},
        "crs": crs_label(src, attrs),
        "params": {"precision": precision, "minAreaM2": min_area, "areaTolerance": area_tol},
        "rules": [dict(rule, hits=hits.get(rule["id"], 0)) for rule in RULES],
        "cleaning": {"inputCount": len(features), "keptCount": len(kept),
                     "droppedCount": len(dropped), "warningCount": len(warnings),
                     "dropped": dropped, "warnings": warnings},
        "result": {"featureCount": len(kept),
                   "areaSumM2": r2(sum(p["areaM2"] for p in (f["properties"] for f in kept))),
                   "gradeA": sum(1 for f in kept if f["properties"]["confidence"] == "A"),
                   "gradeB": sum(1 for f in kept if f["properties"]["confidence"] == "B"),
                   "gradeC": sum(1 for f in kept if f["properties"]["confidence"] == "C")},
        "artifacts": {"geojson": "矢量成果.geojson", "csv": "属性表.csv",
                      "fieldDict": "字段字典.md", "readme": "入库说明.md"},
        "quality": {"usable": bool(kept),
                    "reason": (f"拓扑清理通过：{len(kept)} 个要素可入库"
                               f"（A/B/C = {sum(1 for f in kept if f['properties']['confidence'] == 'A')}"
                               f"/{sum(1 for f in kept if f['properties']['confidence'] == 'B')}"
                               f"/{sum(1 for f in kept if f['properties']['confidence'] == 'C')}）；"
                               "几何为 OBB 外环，未做真值校核")
                              if kept else "清理后没有可用要素"},
    }
    if doc["crs"] is None:
        doc["gaps"] = [{"code": "VEC-GAP-001", "what": "缺 crs 声明",
                        "why": "09 产物没有 crs 成员（模型契约缺 crs.origin）",
                        "need": "在 05 的契约里补 crs.origin 后重跑 09"}]
    else:
        doc["gaps"] = []

    os.makedirs(out_dir, exist_ok=True)
    write_json(os.path.join(out_dir, "矢量成果.geojson"), geojson)
    write_csv(os.path.join(out_dir, "属性表.csv"), rows)
    write_json(os.path.join(out_dir, "拓扑清理.json"), doc)
    with open(os.path.join(out_dir, "字段字典.md"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(render_field_dict(doc))
    with open(os.path.join(out_dir, "入库说明.md"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(render_readme(doc))
    return doc


def read_back(out_dir: str) -> tuple:
    """回读落盘产物再自检（不信内存里的中间态）。"""
    with open(os.path.join(out_dir, "矢量成果.geojson"), encoding="utf-8") as fh:
        geojson = json.load(fh)
    with open(os.path.join(out_dir, "拓扑清理.json"), encoding="utf-8") as fh:
        doc = json.load(fh)
    rows = []
    with open(os.path.join(out_dir, "属性表.csv"), encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    return doc, geojson, rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="10 · 矢量入库（拓扑清理 + 属性表 + 字段字典）")
    ap.add_argument("--geojson", default=SRC_GEOJSON, help="09 的要素矢量.geojson")
    ap.add_argument("--attrs", default=SRC_ATTRS, help="09 的逐栋属性.json（可选，仅用于溯源）")
    ap.add_argument("--out", default=OUT_DEFAULT)
    ap.add_argument("--precision", type=int, default=3, help="坐标保留小数位，默认 3（毫米级）")
    ap.add_argument("--min-area-m2", type=float, default=10.0, help="入库最小面积，默认 10 m²")
    ap.add_argument("--area-tolerance", type=float, default=0.01,
                    help="鞋带面积与属性面积的最大允许偏差，默认 0.01（1%%）")
    ap.add_argument("--force", action="store_true", help="允许覆盖已有产物")
    args = ap.parse_args(argv)

    if args.precision < 0 or args.precision > 6:
        print("[10] --precision 必须在 0–6", file=sys.stderr)
        return 3
    if args.min_area_m2 < 0 or not 0 <= args.area_tolerance <= 1:
        print("[10] --min-area-m2 >= 0、--area-tolerance ∈ [0,1]", file=sys.stderr)
        return 3
    if not os.path.isfile(args.geojson):
        print(f"[10] 缺 09 产物：{args.geojson}（先跑 09）", file=sys.stderr)
        return 2
    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)
    existing = [n for n in OUTPUT_NAMES if os.path.exists(os.path.join(out_dir, n))]
    if existing and not args.force:
        print(f"[10] 输出已存在且未给 --force，不静默覆盖：{existing}", file=sys.stderr)
        return 4
    for name in OUTPUT_NAMES:                 # 先删旧再生成，防「上次的要素数」假通过
        path = os.path.join(out_dir, name)
        if os.path.isfile(path):
            os.remove(path)

    try:
        run(args.geojson, args.attrs, out_dir, args.precision,
            args.min_area_m2, args.area_tolerance)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"[10] 入库失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 3

    doc, geojson, rows = read_back(out_dir)
    problems = gate(doc, geojson, rows)
    if problems:
        print("[10] 闸门未通过：" + "；".join(problems[:8]), file=sys.stderr)
        return 3
    result = doc["result"]
    print(f"[10] 完成：{result['featureCount']} 个要素 / 占地 {result['areaSumM2']} m² / "
          f"A·B·C {result['gradeA']}·{result['gradeB']}·{result['gradeC']}；"
          f"输入 {doc['cleaning']['inputCount']}，丢弃 {doc['cleaning']['droppedCount']}，"
          f"告警 {doc['cleaning']['warningCount']}；usable={doc['quality']['usable']}")
    print("[10] 产物：矢量成果.geojson / 属性表.csv / 字段字典.md / 拓扑清理.json / 入库说明.md")
    return 0 if doc["quality"]["usable"] else 3


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
