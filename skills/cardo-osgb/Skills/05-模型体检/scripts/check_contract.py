#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`cardo-lowair-model.json` 契约自检（T9 薄适配层草稿；只用标准库）。

校验项：schema / schemaVersion / 必填键与类型 / gaps 元素五段式（+ 码表） / 产物相对路径 /
**产物文件存在（相对 model 所在目录）** / **tiles.b3dmCount 与 artifacts 内 .b3dm 数一致** /
无盘符绝对路径 / tiles.count > 0。只读 JSON，不改产物；退出码 0 通过、2 失败。

> 完整性口径：`check_contract` 做**结构 + 文件存在**双重校验，artifacts 逐条必须在
> `dirname(model)` 下真实存在，且 `.b3dm` 条数必须等于 `tiles.b3dmCount`；任一缺失即 exit 2
> （错误信息列出前 5 条）。它不重算几何/不做几何真值校验，那部分由 `health_of` 的 H-01…H-06 兜住。

用法（工作区根为 cwd；本脚本与 05 的 `inspect_model.py` 同目录）：
    python <技能目录>/Skills/05-模型体检/scripts/check_contract.py --model "<产物根>/cardo-lowair-model.json"
    python <技能目录>/Skills/05-模型体检/scripts/check_contract.py --model "<产物根>/cardo-lowair-model.json" --json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

SCHEMA = "cardo/lowair-model/v1"
SCHEMA_VERSION = 1
OWNERS = {"user", "parser", "upstream", "lead"}
ERROR_CODES = {"TOOL_MISSING", "TOOL_VERSION_MISMATCH", "TOOL_FAILED", "ARTIFACT_INCOMPLETE",
               "LICENSE_UNVERIFIED", "SRS_UNSUPPORTED"}
INFO_CODES = {"SRS_COMPOSITE_NORMALIZED", "TILE_NOT_CONVERTED", "ASCII_PATH_WARN"}
KNOWN_CODES = ERROR_CODES | INFO_CODES
DRIVE_RE = re.compile(r"(^|[^A-Za-z0-9])[A-Za-z]:[\\/]")
HEX_RE = re.compile(r"^[0-9A-Fa-f]{64}$")


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _strings(node, path="$"):
    """递归产出 (路径, 字符串) 对，用于全量路径扫描。"""
    if isinstance(node, str):
        yield path, node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield from _strings(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _strings(value, f"{path}[{index}]")


def check(model, base_dir):
    """返回 {ok, errors, warnings, checks}。"""
    errors, warnings, checks = [], [], []

    def expect(condition, name, detail=""):
        checks.append({"name": name, "ok": bool(condition), "detail": detail})
        if not condition:
            errors.append(f"{name}：{detail}" if detail else name)
        return bool(condition)

    def warn(condition, name, detail=""):
        checks.append({"name": name, "ok": bool(condition), "detail": detail, "warn": True})
        if not condition:
            warnings.append(f"{name}：{detail}" if detail else name)

    if not expect(isinstance(model, dict), "顶层是 JSON 对象", type(model).__name__):
        return {"ok": False, "errors": errors, "warnings": warnings, "checks": checks}

    expect(model.get("schema") == SCHEMA, "schema", f"期望 {SCHEMA}，实际 {model.get('schema')!r}")
    expect(model.get("schemaVersion") == SCHEMA_VERSION, "schemaVersion",
           f"期望整数 {SCHEMA_VERSION}，实际 {model.get('schemaVersion')!r}")
    expect(isinstance(model.get("generatedAt"), str) and "T" in str(model.get("generatedAt")),
           "generatedAt", "应为本地时区 ISO-8601 字符串")

    producer = model.get("producer") or {}
    expect(isinstance(producer, dict) and producer.get("name") and producer.get("version"),
           "producer.name/version", "缺失或为空")

    source = model.get("source") or {}
    expect(isinstance(source.get("name"), str) and source.get("name"), "source.name", "缺失")
    for key in ("files", "bytes", "osgbFiles"):
        expect(isinstance(source.get(key), int) and source[key] > 0, f"source.{key}",
               f"应为正整数，实际 {source.get(key)!r}")

    crs = model.get("crs") or {}
    expect(isinstance(crs.get("srs"), str) and crs["srs"].upper().startswith("EPSG:"), "crs.srs",
           f"实际 {crs.get('srs')!r}")
    expect(crs.get("vertical") is None or isinstance(crs.get("vertical"), str), "crs.vertical",
           "应为字符串或 null")
    origin = crs.get("origin")
    expect(isinstance(origin, list) and len(origin) == 3 and all(_is_number(v) for v in origin),
           "crs.origin", "应为三个数（SRSOrigin）")
    expect(_is_number(crs.get("lat")) and -90 <= crs["lat"] <= 90, "crs.lat", f"实际 {crs.get('lat')!r}")
    expect(_is_number(crs.get("lon")) and -180 <= crs["lon"] <= 180, "crs.lon", f"实际 {crs.get('lon')!r}")

    tiles = model.get("tiles") or {}
    expect(isinstance(tiles.get("count"), int) and tiles["count"] > 0, "tiles.count",
           f"必须 > 0，实际 {tiles.get('count')!r}")
    for key in ("b3dmCount", "tilesetCount", "missingContent", "maxContentDepth"):
        expect(isinstance(tiles.get(key), int) and tiles[key] >= 0, f"tiles.{key}",
               f"应为非负整数，实际 {tiles.get(key)!r}")
    expect(isinstance(tiles.get("lodCounts"), dict) and tiles["lodCounts"], "tiles.lodCounts",
           "应为非空对象")

    geometry = model.get("geometry") or {}
    for key in ("triangles", "vertices"):
        expect(isinstance(geometry.get(key), int) and geometry[key] >= 0, f"geometry.{key}",
               f"应为非负整数，实际 {geometry.get(key)!r}")
    bbox = geometry.get("bboxLocal") or {}
    for edge in ("min", "max"):
        values = bbox.get(edge)
        expect(isinstance(values, list) and len(values) == 3 and all(_is_number(v) for v in values),
               f"geometry.bboxLocal.{edge}", "应为三个数")

    textures = model.get("textures") or {}
    for key in ("images", "materials", "missingImages", "bytes"):
        expect(isinstance(textures.get(key), int) and textures[key] >= 0, f"textures.{key}",
               f"应为非负整数，实际 {textures.get(key)!r}")

    geo = model.get("georeference") or {}
    expect(isinstance(geo.get("transformFixed"), bool), "georeference.transformFixed", "应为布尔")
    expect(_is_number(geo.get("verticalOffsetM")), "georeference.verticalOffsetM", "应为数值")
    expect(isinstance(geo.get("notes"), str) and geo["notes"], "georeference.notes", "缺失")
    if "translationToleranceM" in geo:
        expect(_is_number(geo["translationToleranceM"]) and geo["translationToleranceM"] > 0,
               "georeference.translationToleranceM", f"应为正数（米），实际 {geo['translationToleranceM']!r}")

    artifacts = model.get("artifacts")
    expect(isinstance(artifacts, list) and artifacts, "artifacts", "应为非空数组")
    rels, missing_files = [], []
    for index, item in enumerate(artifacts or []):
        label = f"artifacts[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{label}：应为对象")
            continue
        rel = item.get("path")
        expect(isinstance(rel, str) and rel, f"{label}.path", "缺失")
        expect(isinstance(item.get("bytes"), int) and item["bytes"] >= 0, f"{label}.bytes",
               f"应为非负整数，实际 {item.get('bytes')!r}")
        expect(isinstance(item.get("sha256"), str) and HEX_RE.match(item["sha256"]),
               f"{label}.sha256", "应为 64 位 hex")
        if isinstance(rel, str) and rel:
            rels.append(rel)
            expect(not os.path.isabs(rel) and not DRIVE_RE.search(rel), f"{label}.path 相对路径",
                   f"{rel!r} 不能是绝对路径/盘符路径")
            expect(".." not in rel.split("/"), f"{label}.path 无上跳", f"{rel!r} 不能含 ..")
            if not os.path.isfile(os.path.join(base_dir, rel)):
                missing_files.append(rel)
    # 硬闸：artifacts 必须落到 model 所在目录（不再是 WARN）→ 缺失即 exit 2
    expect(not missing_files, "artifacts 文件存在（相对 model 所在目录）",
           f"缺失 {len(missing_files)}/{len(rels)} 个（前 5：{missing_files[:5]}）")
    b3dm_in_artifacts = sum(1 for rel in rels if rel.lower().endswith(".b3dm"))
    expect(isinstance(tiles.get("b3dmCount"), int) and tiles.get("b3dmCount") == b3dm_in_artifacts,
           "tiles.b3dmCount 与 artifacts 内 .b3dm 数一致",
           f"JSON 声明 {tiles.get('b3dmCount')!r}，artifacts 里 .b3dm {b3dm_in_artifacts} 条")
    warn(tiles.get("count") == b3dm_in_artifacts, "tiles.count 与 artifacts 内 .b3dm 条数一致",
         f"tiles.count={tiles.get('count')!r}，artifacts .b3dm {b3dm_in_artifacts} 条（结构提示）")

    toolchain = model.get("toolchain")
    expect(isinstance(toolchain, list) and toolchain, "toolchain", "应为非空数组")
    for index, item in enumerate(toolchain or []):
        for key in ("name", "version", "sha256", "license"):
            expect(isinstance(item, dict) and item.get(key), f"toolchain[{index}].{key}", "缺失或为空")

    gaps = model.get("gaps")
    expect(isinstance(gaps, list), "gaps", "应为数组（无缺项 = []）")
    for index, item in enumerate(gaps or []):
        label = f"gaps[{index}]"
        expect(isinstance(item, dict), label, "应为对象")
        if not isinstance(item, dict):
            continue
        for key in ("code", "what", "why", "need", "owner"):
            expect(isinstance(item.get(key), str) and item[key].strip(), f"{label}.{key}", "缺失或为空")
        expect(item.get("owner") in OWNERS, f"{label}.owner 取值",
               f"应为 {sorted(OWNERS)} 之一，实际 {item.get('owner')!r}")
        expect(item.get("code") in KNOWN_CODES, f"{label}.code 在码表内",
               f"{item.get('code')!r} 不在 9 个登记码内（错误码 {len(ERROR_CODES)} / 信息码 "
               f"{len(INFO_CODES)}，见 Skills/05-模型体检/规则/体检判据.md §gaps 码表）")

    bad_paths = [(path, text) for path, text in _strings(model) if DRIVE_RE.search(text)]
    expect(not bad_paths, "无盘符绝对路径",
           f"命中 {len(bad_paths)} 处：{bad_paths[:3]}")

    ok = not errors
    return {"ok": ok, "errors": errors, "warnings": warnings, "checks": checks}


def main():
    parser = argparse.ArgumentParser(description="cardo/lowair-model/v1 契约自检")
    # 不设默认路径：产物在用户项目目录或回归工作区，脚本无法猜（不写死任何机器/目录假设）
    parser.add_argument("--model", required=True, help="待校验的 cardo-lowair-model.json（必需）")
    parser.add_argument("--json", action="store_true", help="输出 JSON 结果")
    args = parser.parse_args()

    path = os.path.abspath(args.model)
    if not os.path.isfile(path):
        print(f"[check_contract] 文件不存在：{os.path.basename(path)}", file=sys.stderr)
        return 2
    try:
        with open(path, encoding="utf-8") as fh:
            model = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"[check_contract] JSON 读取失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    report = check(model, os.path.dirname(path))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        # 每条不通过的检查只打一次：WARN 走 stdout、FAIL 走 stderr（P3-6 去重）
        for item in report["checks"]:
            if not item["ok"] and item.get("warn"):
                print(f"[WARN] {item['name']}：{item['detail']}")
        for text in report["errors"]:
            print(f"[FAIL] {text}", file=sys.stderr)
        failed = sum(1 for item in report["checks"] if not item["ok"] and not item.get("warn"))
        print(f"[check_contract] {'通过' if report['ok'] else '不通过'}"
              f"（{len(report['checks'])} 项检查 / {failed} 项失败 / {len(report['warnings'])} 项警告）")
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    raise SystemExit(main())