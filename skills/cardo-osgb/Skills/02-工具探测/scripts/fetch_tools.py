#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""02 · 按清单重建工具：把 `tools/` 里的自包含工具从本地源/压缩包恢复，并立刻校验。

用法（工作区根执行）：
    python Skills/02-工具探测/scripts/fetch_tools.py --from <本机源目录>        # 从解压好的目录复制
    python Skills/02-工具探测/scripts/fetch_tools.py --url <zip 路径或 http(s) 地址>
    python Skills/02-工具探测/scripts/fetch_tools.py --verify-only             # 只校验现有 tools/

`--from` 接受两种源：① 含 `v0.4/` 的 fanvanzh-3dtiles 目录；② 直接是 `v0.4/` 目录。
`--url` 支持本地 zip 路径与 http(s)（需联网；解压后定位含 3dtile.exe 的版本目录）。
恢复后复用 `verify_tools.check()`：exe sha256 + 178 文件清单指纹 + `-V` 字面量，任一不符 exit 3
（不静默换版本）。依赖：标准库。分发范围见 规则/外部工具配置.yaml（internal-only）。
退出码：0 通过；1 参数/源不可用；3 恢复后校验不符。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.dont_write_bytecode = True
sys.path.insert(0, HERE)
import verify_tools  # noqa: E402

VENDOR, VERSION = "fanvanzh-3dtiles", "v0.4"
DEFAULT_TOOLS = os.path.join("Skills", "02-工具探测", "tools")
EXE_NAME = "3dtile.exe"


def find_version_dir(src: str) -> str | None:
    """在源目录里定位含 3dtile.exe 的版本目录。"""
    direct = os.path.join(src, EXE_NAME)
    if os.path.isfile(direct):
        return src
    nested = os.path.join(src, VERSION)
    if os.path.isfile(os.path.join(nested, EXE_NAME)):
        return nested
    for dirpath, _dirs, files in os.walk(src):
        if EXE_NAME in files:
            return dirpath
    return None


def install_from_dir(src: str, tools_dir: str) -> str:
    version_dir = find_version_dir(src)
    if not version_dir:
        raise FileNotFoundError(f"源目录内找不到 {EXE_NAME}：{src}")
    dest = os.path.join(tools_dir, VENDOR, VERSION)
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(version_dir, dest)
    return dest


def install_from_url(url: str, tools_dir: str) -> str:
    with tempfile.TemporaryDirectory(prefix="cardo-tools-") as tmp:
        zip_path = os.path.join(tmp, "tools.zip")
        if url.lower().startswith(("http://", "https://")):
            print(f"[02/fetch] 下载 {url}")
            urllib.request.urlretrieve(url, zip_path)
        else:
            if not os.path.isfile(url):
                raise FileNotFoundError(f"zip 不存在：{url}")
            shutil.copyfile(url, zip_path)
        unpacked = os.path.join(tmp, "unpacked")
        with zipfile.ZipFile(zip_path) as archive:
            for member in archive.infolist():
                target = os.path.abspath(os.path.join(unpacked, member.filename))
                if not target.startswith(os.path.abspath(unpacked)):
                    raise ValueError(f"zip 条目越界：{member.filename}")
            archive.extractall(unpacked)
        return install_from_dir(unpacked, tools_dir)


def main() -> int:
    ap = argparse.ArgumentParser(description="02 · 按清单重建 tools/（并校验）")
    ap.add_argument("--from", dest="src", default=None, help="本机已解压的工具源目录")
    ap.add_argument("--url", default=None, help="工具 zip（本地路径或 http(s)）")
    ap.add_argument("--tools-dir", default=DEFAULT_TOOLS, help="目标 tools/ 目录")
    ap.add_argument("--verify-only", action="store_true", help="只校验，不复制")
    args = ap.parse_args()

    tools_dir = os.path.abspath(args.tools_dir)
    os.makedirs(tools_dir, exist_ok=True)
    if not args.verify_only:
        if not (args.src or args.url):
            print("[02/fetch] 需要 --from <目录> 或 --url <zip>（或 --verify-only）", file=sys.stderr)
            return 1
        try:
            dest = install_from_url(args.url, tools_dir) if args.url else install_from_dir(args.src, tools_dir)
        except (OSError, ValueError, zipfile.BadZipFile, urllib.request.URLError) as exc:
            print(f"[02/fetch] 恢复失败：{type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        try:
            shown = os.path.relpath(dest, os.path.abspath("."))
        except ValueError:          # 跨盘无法求相对路径：退回目录名，不落盘符
            shown = os.path.join("<tools-dir>", VENDOR, VERSION)
        print(f"[02/fetch] 已恢复到 {shown.replace(chr(92), '/')}")

    report = verify_tools.check(tools_dir)
    for item in report["checks"]:
        flag = "PASS" if item.get("ok") else ("SKIP" if item.get("ok") is None else "FAIL")
        print(f"  [{flag}] {item['name']}")
    if not report["ok"]:
        print("[02/fetch] 校验未通过（sha256/清单指纹/版本文本不符，拒绝使用）", file=sys.stderr)
        return 3
    print("[02/fetch] 校验通过：可执行 + 清单指纹 + 版本字面量 全部命中")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
