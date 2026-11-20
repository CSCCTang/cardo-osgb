#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""13 · 矢量增强 · vector.gpkg：把 10 的矢量成果写成最小合规 GeoPackage。

只用 stdlib `sqlite3`，不依赖 GDAL/fiona/ogr2ogr。产出
`过程文件/13-矢量增强/vector-gpkg/矢量成果.gpkg`：

  PRAGMA application_id=1196444487、user_version=10200
  gpkg_spatial_ref_sys（4326 / -1 / 0 必填 + 4544 自定义）
  gpkg_contents（data_type='features'）
  gpkg_geometry_columns（geom / POLYGON / srs_id=4544 / z=0 / m=0）
  buildings（fid INTEGER PRIMARY KEY, geom POLYGON + 属性列）

几何 blob = b'GP' + version 0 + flags 1（小端、无 envelope）+ srs_id(int32 LE=4544)
            + WKB Polygon（外环逆时针、内环顺时针）。

写完回读自检：三张元数据表存在、要素数=13、geom 前 2 字节为 b'GP'、srs_id=4544、
面积与 10 的 GeoJSON 偏差 < 1%。自检不过就删除坏文件、usable=false + gap，
绝不留一个坏 GPKG 冒充成果。

用法（工作区根）：
    python Skills/13-矢量增强/scripts/vector_gpkg.py --workspace .
退出码：0 可用；2 缺输入；3 产出但自检不过；4 产物已存在未给 --force。
"""
from __future__ import annotations

import os
import sqlite3
import struct
import sys
from datetime import datetime, timezone

sys.dont_write_bytecode = True

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from common import (VECTOR_REL, emit, finish, parse_args, read_json, write_json)  # noqa: E402
    COMMON_IMPL = "common"
except ImportError:
    from common_fallback import (VECTOR_REL, emit, finish, parse_args, read_json, write_json)  # noqa: E402
    COMMON_IMPL = "common_fallback"

ALGO = "vector.gpkg"
SLUG = "vector-gpkg"
OUT_GPKG = "矢量成果.gpkg"
PATCH_NAME = "vector-gpkg.json"
SRS_ID = 4544
APPLICATION_ID = 1196444487              # 0x47504B47 = 'GPKG'
USER_VERSION = 10200                     # GeoPackage 1.2.0
AREA_TOL = 0.01                          # 面积偏差上限 1%

SRS_ROWS = [
    ("Undefined geographic SRS", 0, "NONE", 0, "undefined",
     "undefined geographic coordinate reference system"),
    ("Undefined Cartesian SRS", -1, "NONE", -1, "undefined",
     "undefined cartesian coordinate reference system"),
    ("WGS 84 geodetic", 4326, "EPSG", 4326,
     'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563,'
     'AUTHORITY["EPSG","7030"]],AUTHORITY["EPSG","6326"]],PRIMEM["Greenwich",0,'
     'AUTHORITY["EPSG","8901"]],UNIT["degree",0.0174532925199433,AUTHORITY["EPSG","9122"]],'
     'AUTHORITY["EPSG","4326"]]', "WGS 84"),
    ("CGCS2000 / 3-degree Gauss-Kruger CM 117E", SRS_ID, "EPSG", SRS_ID,
     'PROJCS["CGCS2000 / 3-degree Gauss-Kruger CM 117E",GEOGCS["China 2000",'
     'DATUM["China_2000",SPHEROID["CGCS2000",6378137,298.257222101,AUTHORITY["EPSG","1024"]],'
     'AUTHORITY["EPSG","1043"]],PRIMEM["Greenwich",0,AUTHORITY["EPSG","8901"]],'
     'UNIT["degree",0.0174532925199433,AUTHORITY["EPSG","9122"]],AUTHORITY["EPSG","4490"]],'
     'PROJECTION["Transverse_Mercator"],PARAMETER["latitude_of_origin",0],'
     'PARAMETER["central_meridian",117],PARAMETER["scale_factor",1],'
     'PARAMETER["false_easting",500000],PARAMETER["false_northing",0],'
     'UNIT["metre",1,AUTHORITY["EPSG","9001"]],AXIS["Easting",EAST],'
     'AXIS["Northing",NORTH],AUTHORITY["EPSG","4544"]]', "CGCS2000 3-degree GK CM 117E"),
]

ATTR_COLUMNS = [
    ("id", "TEXT"), ("areaM2", "REAL"), ("heightP50", "REAL"), ("heightP90", "REAL"),
    ("heightMax", "REAL"), ("floorsEst", "INTEGER"), ("floorsRangeLow", "INTEGER"),
    ("floorsRangeHigh", "INTEGER"), ("centroidX", "REAL"), ("centroidY", "REAL"),
    ("widthM", "REAL"), ("heightM", "REAL"), ("angleDeg", "REAL"), ("fillRatio", "REAL"),
    ("confidence", "TEXT"), ("gradeReasons", "TEXT"), ("geometryType", "TEXT"),
    ("areaRecalcM2", "REAL"),
]


def _rel(path: str) -> str:
    return str(path).replace("\\", "/")


def _now_change() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def signed_area(ring) -> float:
    total = 0.0
    for i in range(len(ring) - 1):
        total += ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1]
    return total / 2.0


def ring_area(ring) -> float:
    return abs(signed_area(ring))


def close_ring(ring):
    pts = [[float(p[0]), float(p[1])] for p in ring]
    if not pts:
        return pts
    if pts[0] != pts[-1]:
        pts.append(pts[0])
    return pts


def clean_rings(geom):
    """外环逆时针、内环顺时针、首尾闭合；返回环列表。"""
    rings = geom.get("coordinates") or []
    out = []
    for k, ring in enumerate(rings):
        rr = close_ring(ring)
        if len(rr) < 4:
            continue
        if k == 0 and signed_area(rr) < 0:
            rr = list(reversed(rr))
        if k > 0 and signed_area(rr) > 0:
            rr = list(reversed(rr))
        out.append(rr)
    return out


def polygon_area(rings) -> float:
    if not rings:
        return 0.0
    total = ring_area(rings[0])
    for hole in rings[1:]:
        total -= ring_area(hole)
    return total


def wkb_polygon(rings) -> bytes:
    """WKB Polygon（小端）：byteorder 1 + type 3 + 环数 + 每环点数/坐标。"""
    parts = [struct.pack("<B", 1), struct.pack("<I", 3), struct.pack("<I", len(rings))]
    for ring in rings:
        parts.append(struct.pack("<I", len(ring)))
        for x, y in ring:
            parts.append(struct.pack("<dd", float(x), float(y)))
    return b"".join(parts)


def gpkg_blob(rings) -> bytes:
    """GeoPackage geometry blob：GP + version0 + flags1 + srs_id + WKB。"""
    header = b"GP" + struct.pack("<B", 0) + struct.pack("<B", 1) + struct.pack("<i", SRS_ID)
    return header + wkb_polygon(rings)


def parse_gpkg_blob(blob: bytes) -> dict:
    """回读 GPKG blob：magic/version/flags/srs_id + WKB Polygon。"""
    if blob[:2] != b"GP":
        raise ValueError("geom 前 2 字节不是 b'GP'")
    version = struct.unpack_from("<B", blob, 2)[0]
    flags = struct.unpack_from("<B", blob, 3)[0]
    srs_id = struct.unpack_from("<i", blob, 4)[0]
    env_code = (flags >> 1) & 0x07
    env_bytes = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}.get(env_code)
    if env_bytes is None:
        raise ValueError(f"非法 envelope 标志 {env_code}")
    pos = 8 + env_bytes
    byte_order = struct.unpack_from("<B", blob, pos)[0]
    if byte_order != 1:
        raise ValueError("WKB 不是小端")
    gtype = struct.unpack_from("<I", blob, pos + 1)[0] & 0xFF
    if gtype != 3:
        raise ValueError(f"WKB 类型 {gtype} 不是 Polygon")
    nrings = struct.unpack_from("<I", blob, pos + 5)[0]
    offset = pos + 9
    rings = []
    for _ in range(nrings):
        npts = struct.unpack_from("<I", blob, offset)[0]
        offset += 4
        ring = []
        for _ in range(npts):
            x, y = struct.unpack_from("<dd", blob, offset)
            offset += 16
            ring.append([x, y])
        rings.append(ring)
    return {"version": version, "flags": flags, "srsId": srs_id,
            "envelopeCode": env_code, "rings": rings, "byteOrder": byte_order}


def feature_rows(features):
    rows = []
    for idx, feat in enumerate(features, 1):
        props = feat.get("properties") or {}
        rings = clean_rings(feat.get("geometry") or {})
        floors = props.get("floorsRange") or [None, None]
        centroid = props.get("centroidXY")
        if not centroid and rings:
            ring = rings[0][:-1]
            centroid = [sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring)]
        rows.append({
            "fid": idx, "id": feat.get("id") or props.get("id"),
            "rings": rings,
            "areaM2": props.get("areaM2"), "heightP50": props.get("heightP50"),
            "heightP90": props.get("heightP90"), "heightMax": props.get("heightMax"),
            "floorsEst": props.get("floorsEst"),
            "floorsRangeLow": floors[0] if len(floors) > 0 else None,
            "floorsRangeHigh": floors[1] if len(floors) > 1 else None,
            "centroidX": centroid[0] if centroid else None,
            "centroidY": centroid[1] if centroid else None,
            "widthM": props.get("widthM"), "heightM": props.get("heightM"),
            "angleDeg": props.get("angleDeg"), "fillRatio": props.get("fillRatio"),
            "confidence": props.get("confidence"), "gradeReasons": props.get("gradeReasons"),
            "geometryType": props.get("geometryType"), "areaRecalcM2": props.get("areaRecalcM2"),
        })
    return rows


def create_schema(con):
    con.execute(f"PRAGMA application_id = {APPLICATION_ID}")
    con.execute(f"PRAGMA user_version = {USER_VERSION}")
    con.execute("""CREATE TABLE gpkg_spatial_ref_sys (
        srs_name TEXT NOT NULL, srs_id INTEGER NOT NULL PRIMARY KEY,
        organization TEXT NOT NULL, organization_coordsys_id INTEGER NOT NULL,
        definition TEXT NOT NULL, description TEXT)""")
    con.execute("""CREATE TABLE gpkg_contents (
        table_name TEXT NOT NULL PRIMARY KEY, data_type TEXT NOT NULL,
        identifier TEXT UNIQUE, description TEXT DEFAULT '',
        last_change DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        min_x DOUBLE, min_y DOUBLE, max_x DOUBLE, max_y DOUBLE, srs_id INTEGER,
        CONSTRAINT fk_gc_r_srs_id FOREIGN KEY (srs_id)
            REFERENCES gpkg_spatial_ref_sys(srs_id))""")
    con.execute("""CREATE TABLE gpkg_geometry_columns (
        table_name TEXT NOT NULL, column_name TEXT NOT NULL,
        geometry_type_name TEXT NOT NULL, srs_id INTEGER NOT NULL,
        z TINYINT NOT NULL, m TINYINT NOT NULL,
        CONSTRAINT pk_geom_cols PRIMARY KEY (table_name, column_name),
        CONSTRAINT uk_gc_table_name UNIQUE (table_name),
        CONSTRAINT fk_gc_tn FOREIGN KEY (table_name) REFERENCES gpkg_contents(table_name),
        CONSTRAINT fk_gc_srs FOREIGN KEY (srs_id)
            REFERENCES gpkg_spatial_ref_sys(srs_id))""")
    con.execute("CREATE TABLE buildings (fid INTEGER PRIMARY KEY, geom BLOB NOT NULL, "
                + ", ".join(f"{name} {typ}" for name, typ in ATTR_COLUMNS) + ")")
    con.executemany("INSERT INTO gpkg_spatial_ref_sys "
                    "(srs_name, srs_id, organization, organization_coordsys_id, "
                    "definition, description) VALUES (?,?,?,?,?,?)", SRS_ROWS)


def write_gpkg(path: str, rows, bbox) -> None:
    con = sqlite3.connect(path)
    try:
        create_schema(con)
        con.execute(
            "INSERT INTO gpkg_contents (table_name, data_type, identifier, description, "
            "last_change, min_x, min_y, max_x, max_y, srs_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("buildings", "features", "cardo-buildings",
             "10 的 EPSG:4544 建筑矢量（OBB Polygon）", _now_change(),
             bbox[0], bbox[1], bbox[2], bbox[3], SRS_ID))
        con.execute(
            "INSERT INTO gpkg_geometry_columns "
            "(table_name, column_name, geometry_type_name, srs_id, z, m) VALUES (?,?,?,?,?,?)",
            ("buildings", "geom", "POLYGON", SRS_ID, 0, 0))
        keys = ["fid", "id", "areaM2", "heightP50", "heightP90", "heightMax", "floorsEst",
                "floorsRangeLow", "floorsRangeHigh", "centroidX", "centroidY", "widthM",
                "heightM", "angleDeg", "fillRatio", "confidence", "gradeReasons",
                "geometryType", "areaRecalcM2"]
        sql = "INSERT INTO buildings (fid, geom, " + ", ".join(keys[1:]) + ") VALUES (" \
              + ", ".join(["?"] * (len(keys) + 1)) + ")"
        for row in rows:
            con.execute(sql, [row["fid"], gpkg_blob(row["rings"])]
                        + [row[k] for k in keys[1:]])
        con.commit()
    finally:
        con.close()


def self_check(path: str, rows, features) -> tuple:
    """回读 SQLite/GPKG 自检；返回 (ok, items, problems, fileBytes)。"""
    problems = []
    items = []
    file_bytes = os.path.getsize(path) if os.path.isfile(path) else None
    con = sqlite3.connect(path)
    try:
        app_id = con.execute("PRAGMA application_id").fetchone()[0]
        user_version = con.execute("PRAGMA user_version").fetchone()[0]
        if app_id != APPLICATION_ID:
            problems.append(f"application_id={app_id} ≠ {APPLICATION_ID}")
        if user_version != USER_VERSION:
            problems.append(f"user_version={user_version} ≠ {USER_VERSION}")
        names = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for required in ("gpkg_spatial_ref_sys", "gpkg_contents",
                         "gpkg_geometry_columns", "buildings"):
            if required not in names:
                problems.append(f"缺表 {required}")
        if "gpkg_spatial_ref_sys" in names:
            got = {r[0] for r in con.execute("SELECT srs_id FROM gpkg_spatial_ref_sys")}
            for required in (-1, 0, 4326, SRS_ID):
                if required not in got:
                    problems.append(f"gpkg_spatial_ref_sys 缺 srs_id {required}")
        if "gpkg_contents" in names:
            row = con.execute("SELECT data_type, srs_id FROM gpkg_contents "
                              "WHERE table_name='buildings'").fetchone()
            if not row:
                problems.append("gpkg_contents 无 buildings")
            elif row[0] != "features" or row[1] != SRS_ID:
                problems.append(f"gpkg_contents {row} 不是 features/{SRS_ID}")
        if "gpkg_geometry_columns" in names:
            row = con.execute("SELECT geometry_type_name, srs_id, z, m FROM "
                              "gpkg_geometry_columns WHERE table_name='buildings'").fetchone()
            if not row:
                problems.append("gpkg_geometry_columns 无 buildings")
            elif row != ("POLYGON", SRS_ID, 0, 0):
                problems.append(f"gpkg_geometry_columns {row} 不符")
        count = con.execute("SELECT COUNT(*) FROM buildings").fetchone()[0]
        if count != len(rows):
            problems.append(f"要素数 {count} ≠ 预期 {len(rows)}")

        geo_by_id = {}
        for feat in features:
            props = feat.get("properties") or {}
            geo_by_id[feat.get("id") or props.get("id")] = polygon_area(
                clean_rings(feat.get("geometry") or {}))
        max_dev = 0.0
        for fid, blob, bid, area_recalc in con.execute(
                "SELECT fid, geom, id, areaRecalcM2 FROM buildings ORDER BY fid"):
            if blob[:2] != b"GP":
                problems.append(f"fid {fid} geom 前 2 字节不是 b'GP'")
                continue
            parsed = parse_gpkg_blob(blob)
            if parsed["srsId"] != SRS_ID:
                problems.append(f"fid {fid} header srs_id={parsed['srsId']} ≠ {SRS_ID}")
            if parsed["version"] != 0:
                problems.append(f"fid {fid} GPKG version={parsed['version']} ≠ 0")
            if parsed["envelopeCode"] != 0:
                problems.append(f"fid {fid} envelopeCode={parsed['envelopeCode']} ≠ 0")
            area_gpkg = polygon_area(parsed["rings"])
            area_geo = geo_by_id.get(bid)
            dev = None if area_geo in (None, 0) else abs(area_gpkg - area_geo) / area_geo
            if dev is not None:
                max_dev = max(max_dev, dev)
                if dev >= AREA_TOL:
                    problems.append(f"fid {fid} 面积偏差 {dev:.4%} ≥ {AREA_TOL:.0%}")
            items.append({
                "fid": fid, "id": bid, "blobBytes": len(blob),
                "headerSrsId": parsed["srsId"],
                "areaGpkgM2": round(area_gpkg, 3),
                "areaGeojsonM2": round(area_geo, 3) if area_geo is not None else None,
                "areaRecalcM2": area_recalc,
                "deviation": round(dev, 6) if dev is not None else None,
            })
        return (not problems), items, problems, file_bytes, max_dev
    finally:
        con.close()


def build(workspace: str) -> tuple:
    src_path = os.path.join(workspace, VECTOR_REL)
    src = read_json(src_path)
    features = src.get("features") or []
    rows = feature_rows(features)
    if not rows:
        raise ValueError("10 的矢量成果没有要素")

    coords = [p for row in rows for ring in row["rings"] for p in ring]
    bbox = (min(p[0] for p in coords), min(p[1] for p in coords),
            max(p[0] for p in coords), max(p[1] for p in coords))

    out_dir = os.path.join(workspace, "过程文件", "13-矢量增强", SLUG)
    os.makedirs(out_dir, exist_ok=True)
    gpkg_path = os.path.join(out_dir, OUT_GPKG)
    if os.path.exists(gpkg_path):
        os.remove(gpkg_path)                     # 先删旧再生成，避免旧文件冒充
    write_gpkg(gpkg_path, rows, bbox)

    ok, items, problems, file_bytes, max_dev = self_check(gpkg_path, rows, features)
    if not ok:
        os.remove(gpkg_path)                     # 坏文件不留在成果里
        file_bytes = None

    summary = {
        "featureCount": len(items),
        "fileBytes": file_bytes,
        "srsId": SRS_ID,
        "areaMismatchMax": round(max_dev, 6) if items else None,
        "selfCheckPass": bool(ok),
    }
    gaps = [] if ok else [{
        "code": "ENH-GAP-001",
        "what": "GeoPackage 回读自检未通过，已删除坏文件",
        "why": "；".join(problems[:6]),
        "need": "修好后重跑 vector_gpkg.py；不允许用坏 GPKG 冒充成果",
    }]
    usable = bool(ok)

    markdown = render_md(summary, items, gaps, src, bbox, rows)
    inputs = {"vector10": _rel(VECTOR_REL), "featureCount": len(features)}
    params = {
        "engine": "sqlite3(stdlib)", "applicationId": APPLICATION_ID,
        "userVersion": USER_VERSION, "srsId": SRS_ID, "geometryType": "POLYGON",
        "geometryColumn": "geom", "envelope": False, "wkbByteOrder": "little",
        "gpkgVersion": "1.2", "areaTolerance": AREA_TOL, "bbox": [round(v, 3) for v in bbox],
        "columns": [{"name": n, "type": t} for n, t in ATTR_COLUMNS],
        "commonImpl": COMMON_IMPL,
    }

    doc = emit(workspace, ALGO, inputs=inputs, params=params, summary=summary,
               items=items, gaps=gaps, usable=usable,
               reasons=problems if not ok else [],
               markdown=markdown, extra_files=[OUT_GPKG])
    write_patch(workspace)
    return doc


def write_patch(workspace: str) -> None:
    patch = {
        "id": ALGO,
        "status": "ready",
        "step": "13",
        "impl": "自实现：stdlib sqlite3 建 GPKG 元数据表 + GeoPackage geometry blob（GP 头 + WKB Polygon）",
        "evidence": "Skills/13-矢量增强/scripts/vector_gpkg.py",
        "sourceName": "GeoPackage 1.2 规范（OGC 12-128r18），sqlite3 自实现",
        "sourceLink": "https://www.geopackage.org/spec/",
        "license": "自研代码",
        "plain": "把 10 的 13 个 EPSG:4544 面要素写成可直接用 GIS 打开的 GeoPackage",
    }
    write_json(os.path.join(workspace, "过程文件", "13-矢量增强", "_patches", PATCH_NAME), patch)


def render_md(summary, items, gaps, src, bbox, rows) -> str:
    lines = ["# 矢量成果 GeoPackage（13 · vector.gpkg）", "",
             f"- 来源：`{_rel(VECTOR_REL)}`（{len(src.get('features') or [])} 要素）",
             f"- 输出：`{OUT_GPKG}`，{summary['fileBytes'] if summary['fileBytes'] is not None else '—'} B",
             "- 坐标系：`EPSG:4544`；几何：Polygon（外环逆时针、洞顺时针）",
             "- 写入：stdlib `sqlite3`；不依赖 GDAL/fiona/ogr2ogr", "",
             "## 自检", "",
             "| 项 | 值 |", "|---|---|",
             f"| application_id | {APPLICATION_ID} |",
             f"| user_version | {USER_VERSION} |",
             f"| 要素数 | {summary['featureCount']} |",
             f"| srs_id | {summary['srsId']} |",
             f"| 面积最大相对偏差 | {summary['areaMismatchMax']} |",
             f"| selfCheckPass | {summary['selfCheckPass']} |", "",
             "## 要素", "",
             "| fid | id | blob B | GPKG srs_id | GPKG 面积 m² | GeoJSON 面积 m² | 偏差 |",
             "|---|---|---|---|---|---|---|"]
    for item in items:
        lines.append(f"| {item['fid']} | {item['id']} | {item['blobBytes']} | "
                     f"{item['headerSrsId']} | {item['areaGpkgM2']} | "
                     f"{item['areaGeojsonM2']} | {item['deviation']} |")
    lines += ["", "## 局限", ""]
    if gaps:
        for gap in gaps:
            lines.append(f"- `{gap['code']}` {gap['what']}：{gap['why']}（需：{gap['need']}）")
    else:
        lines.append("- 几何是 10 的 **OBB 最小外接矩形**，不是建筑真实轮廓；GPKG 只换容器，不改几何。")
        lines.append("- 属性高度来自 09 的几何反演，无真值校核；`confidence=C` 建议人工确认。")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    try:
        args = parse_args(ALGO, "13 · 矢量成果写 GeoPackage（stdlib sqlite3）")
    except SystemExit as exc:
        code = exc.code
        if isinstance(code, int):
            return code
        if code:
            print(str(code), file=sys.stderr)
        return 4 if code and "产物已存在" in str(code) else 2
    src = os.path.join(args.workspace, VECTOR_REL)
    if not os.path.isfile(src):
        print(f"[13:vector.gpkg] 缺输入：{VECTOR_REL}（先跑 10）", file=sys.stderr)
        return 2
    gpkg_path = os.path.join(args.workspace, "过程文件", "13-矢量增强", SLUG, OUT_GPKG)
    if os.path.exists(gpkg_path) and not args.force:
        print(f"[13:vector.gpkg] 产物已存在：{gpkg_path}（加 --force 覆盖）", file=sys.stderr)
        return 4
    try:
        doc = build(args.workspace)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        print(f"[13:vector.gpkg] 失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 3
    return finish(doc)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
