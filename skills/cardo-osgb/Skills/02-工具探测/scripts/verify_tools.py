#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cardo-osgb 外部工具完整性校验（T9/T14 用；只用标准库）。

用途：把"工具版本/完整性白名单"落成可执行判据——
  1) `3dtile.exe` sha256 必须等于记录值；
  2) 安装目录的**文件清单指纹**必须等于记录值（换/改任意 DLL 都会变）；
  3) `3dtile -V` 输出字面量必须等于记录值；
  4)（可选）`3d-tiles-tools` 的 `package.json` version 必须等于记录值。

指纹口径（**必须按此实现，否则不同实现会算出不同值**）：
  · 基准目录：`<tools-dir>/fanvanzh-3dtiles/v0.4/`
  · 递归枚举全部普通文件；相对路径统一用 `/` 分隔
  · 每行 `相对路径|字节数|SHA256(文件字节，大写 hex)`
  · 按「相对路径」做 **Unicode 码点序（ordinal）** 升序排序
  · 行间用 `\\n` 连接，**末尾不加换行**
  · 对整段 UTF-8 字节取 sha256，输出大写 hex

用法（工作区根为 cwd）：
    python Skills/02-工具探测/scripts/verify_tools.py
    python Skills/02-工具探测/scripts/verify_tools.py --tools-dir <工具根>
    python Skills/02-工具探测/scripts/verify_tools.py --json
退出码：0 全部通过；2 工具缺失；3 校验不符。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys

# —— 白名单（与 数据/回归工作区/osgb-tools/环境记录.md §1 同步；改工具必须同时改这里并升记录）——
EXPECTED_EXE_SHA256 = "C7F5C7EED5165B2ECACBB25D0B459C33C20304245897B793D09141715BB379DC"
EXPECTED_MANIFEST_SHA256 = "583F54FC2F56838F981133BB9BCE03DF2706501DE09F5C2015C7F709959F162F"
EXPECTED_MANIFEST_FILES = 178
EXPECTED_VERSION_BANNER = "Make 3dtile program 1.0"
EXPECTED_3D_TILES_TOOLS_VERSION = "0.5.4"


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest().upper()


def manifest(path: str) -> tuple[str, int]:
    """按文档口径计算目录清单指纹，返回 (指纹, 文件数)。"""
    rows: list[tuple[str, int, str]] = []
    for dirpath, _dirs, files in os.walk(path):
        for name in files:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, path).replace("\\", "/")
            rows.append((rel, os.path.getsize(full), sha256_file(full)))
    rows.sort(key=lambda item: item[0])  # Unicode 码点序（ordinal）
    text = "\n".join(f"{rel}|{size}|{digest}" for rel, size, digest in rows)
    return hashlib.sha256(text.encode("utf-8")).hexdigest().upper(), len(rows)


def check(tools_dir: str) -> dict:
    problem: list[str] = []
    result: dict = {"toolsDir": tools_dir, "checks": [], "ok": False}
    exe = os.path.join(tools_dir, "fanvanzh-3dtiles", "v0.4", "3dtile.exe")
    install_dir = os.path.dirname(exe)
    if not os.path.isfile(exe):
        result["reason"] = "TOOL_MISSING"
        result["checks"].append({"name": "3dtile.exe 存在", "ok": False, "detail": exe})
        return result

    exe_sha = sha256_file(exe)
    exe_ok = exe_sha == EXPECTED_EXE_SHA256
    result["checks"].append({"name": "exe sha256", "ok": exe_ok, "actual": exe_sha,
                             "expected": EXPECTED_EXE_SHA256})
    if not exe_ok:
        problem.append("TOOL_VERSION_MISMATCH:exe")

    fingerprint, count = manifest(install_dir)
    man_ok = fingerprint == EXPECTED_MANIFEST_SHA256 and count == EXPECTED_MANIFEST_FILES
    result["checks"].append({"name": "安装目录清单指纹", "ok": man_ok, "files": count,
                             "actual": fingerprint, "expected": EXPECTED_MANIFEST_SHA256})
    if not man_ok:
        problem.append("TOOL_VERSION_MISMATCH:manifest")

    try:
        proc = subprocess.run([exe, "-V"], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=60)
        banner = (proc.stdout or proc.stderr or "").strip().splitlines()
        banner = banner[0] if banner else ""
    except (OSError, subprocess.SubprocessError) as exc:
        banner = f"<运行失败: {type(exc).__name__}>"
    banner_ok = banner == EXPECTED_VERSION_BANNER
    result["checks"].append({"name": "-V 字面量", "ok": banner_ok, "actual": banner,
                             "expected": EXPECTED_VERSION_BANNER})
    if not banner_ok:
        problem.append("TOOL_VERSION_MISMATCH:banner")

    # 可选校验器：存在才校验版本
    pkg = os.path.join(tools_dir, "3d-tiles-tools", "node_modules", "3d-tiles-tools", "package.json")
    if os.path.isfile(pkg):
        try:
            with open(pkg, encoding="utf-8") as fh:
                version = json.load(fh).get("version")
        except (OSError, ValueError):
            version = None
        ver_ok = version == EXPECTED_3D_TILES_TOOLS_VERSION
        result["checks"].append({"name": "3d-tiles-tools version", "ok": ver_ok,
                                 "actual": version, "expected": EXPECTED_3D_TILES_TOOLS_VERSION})
        if not ver_ok:
            problem.append("TOOL_VERSION_MISMATCH:validator")
    else:
        result["checks"].append({"name": "3d-tiles-tools 存在（可选）", "ok": None,
                                 "detail": "未安装：产物校验降级为自校验"})

    result["ok"] = not problem
    if problem:
        result["reason"] = ";".join(problem)
    return result


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    default_tools = os.path.normpath(os.path.join(here, "..", "..", "..", "数据", "回归工作区", "osgb-tools"))
    parser = argparse.ArgumentParser(description="外部工具完整性/版本校验")
    parser.add_argument("--tools-dir", default=default_tools)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = check(args.tools_dir)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        for item in report["checks"]:
            flag = "PASS" if item["ok"] else ("SKIP" if item["ok"] is None else "FAIL")
            if item["name"] == "安装目录清单指纹":
                detail = f"files={item.get('files')} {item.get('actual')}"
            else:
                detail = item.get("actual") or item.get("files") or item.get("detail") or ""
            print(f"[{flag}] {item['name']}: {detail}")
        print(f"[verify-tools] {'全部通过' if report['ok'] else '校验失败：' + report.get('reason', '')}")
    if report.get("reason") == "TOOL_MISSING":
        return 2
    return 0 if report["ok"] else 3


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())