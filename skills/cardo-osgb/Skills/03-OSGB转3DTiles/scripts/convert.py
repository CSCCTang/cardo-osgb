#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""03 · OSGB→3D Tiles：调用开源 3dtile.exe 生成 b3dm + tileset.json，并做产物自扫描。

输入：01 的 输入清单.json（含 SRS/SRSOrigin）、02 的 工具探测.json（含 exe sha256）
输出（默认 过程文件/03-OSGB转3DTiles/）：
    转换产物/Data/**/*.b3dm、*/tileset.json、tileset.json
    转换日志.json      cardo/lowair-convert/v1（视图/LOD/工具/耗时/产物统计）
退出码：0 成功；2 输入缺失；3 工具不可用/转换失败/产物不完整；4 工具缺失（跳过语义，不是崩溃）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.dont_write_bytecode = True
sys.path.insert(0, HERE)
import tiles_inspect  # noqa: E402

SCHEMA = "cardo/lowair-convert/v1"
SKILL_LOCAL_TOOLS = os.path.join("Skills", "02-工具探测", "tools")
EXE_REL = os.path.join("fanvanzh-3dtiles", "v0.4", "3dtile.exe")
TOOL_TIMEOUT = 600


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def read_json(path: str) -> dict:
    with open(path, encoding="utf-8-sig") as fh:
        return json.load(fh)


def write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def resolve_tools_dir(cli: str | None) -> str | None:
    if cli:
        return os.path.abspath(cli)
    local = os.path.abspath(SKILL_LOCAL_TOOLS)
    if os.path.isdir(local):
        return local
    env = os.environ.get("CARDO_OSGB_TOOLS")
    return os.path.abspath(env) if env and os.path.isdir(env) else None


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def build_view(scene_dir: str, work_dir: str) -> tuple[str, dict]:
    """规范化输入视图：in-osgb/metadata.xml + Data/<块>/<瓦片>.osgb（只拷 osgb）。"""
    in_dir = os.path.join(work_dir, "in-osgb")
    shutil.rmtree(in_dir, ignore_errors=True)
    data_dir = os.path.join(in_dir, "Data")
    os.makedirs(data_dir)
    shutil.copyfile(os.path.join(scene_dir, "metadata.xml"), os.path.join(in_dir, "metadata.xml"))
    info = {"blocks": {}, "lods": {}, "topLevel": [], "osgb": 0, "bytes": 0}
    for name in sorted(os.listdir(scene_dir)):
        full = os.path.join(scene_dir, name)
        if os.path.isdir(full):
            dest = os.path.join(data_dir, name)
            os.makedirs(dest, exist_ok=True)
            count = 0
            for dirpath, _dirs, files in os.walk(full):
                rel = os.path.relpath(dirpath, full)
                for fname in sorted(files):
                    if not fname.lower().endswith(".osgb"):
                        continue
                    target = os.path.join(dest, fname if rel == "." else os.path.join(rel, fname))
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    shutil.copyfile(os.path.join(dirpath, fname), target)
                    count += 1
                    info["bytes"] += os.path.getsize(target)
                    match = re.search(r"_L(\d{1,2})_", fname)
                    if match:
                        info["lods"][match.group(1)] = info["lods"].get(match.group(1), 0) + 1
            info["blocks"][name] = count
            info["osgb"] += count
        elif name.lower().endswith(".osgb"):
            target = os.path.join(data_dir, name)
            shutil.copyfile(full, target)
            info["topLevel"].append({"name": name, "bytes": os.path.getsize(target)})
            info["osgb"] += 1
            info["bytes"] += os.path.getsize(target)
    return in_dir, info


def peak_working_set(pid: int) -> int | None:
    """子进程峰值工作集（字节）：Windows 用 GetProcessMemoryInfo，其它平台读 /proc VmHWM。"""
    if os.name != "nt":
        try:
            with open(f"/proc/{pid}/status", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("VmHWM:"):
                        return int(line.split()[1]) * 1024
        except (OSError, ValueError, IndexError):
            return None
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        handle = kernel32.OpenProcess(0x0400 | 0x0010, False, pid)   # QUERY_INFORMATION | VM_READ
        if not handle:
            return None
        try:
            counters = Counters()
            counters.cb = ctypes.sizeof(Counters)
            if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                return None
            return int(counters.PeakWorkingSetSize)
        finally:
            kernel32.CloseHandle(handle)
    except Exception:   # 无 psapi/权限不足：不记录，不阻断转换
        return None


def run_tool(exe: str, in_dir: str, out_dir: str) -> dict:
    started = time.time()
    peak = [None]
    try:
        proc = subprocess.Popen([exe, "-f", "osgb", "-i", in_dir, "-o", out_dir],
                                cwd=os.path.dirname(exe), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                errors="replace")
    except OSError as exc:
        return {"exitCode": -1, "stdoutTail": "", "stderrTail": f"启动失败：{type(exc).__name__}",
                "durationMs": int((time.time() - started) * 1000), "peakMemoryBytes": None}

    def watch():
        while proc.poll() is None:
            value = peak_working_set(proc.pid)
            if value and (peak[0] is None or value > peak[0]):
                peak[0] = value
            time.sleep(0.05)

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    try:
        out, err = proc.communicate(timeout=TOOL_TIMEOUT)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        code, err = -9, f"超时（>{TOOL_TIMEOUT}s）"
    watcher.join(timeout=1.0)
    return {"exitCode": code, "stdoutTail": (out or "").strip()[-1500:],
            "stderrTail": (err or "").strip()[-3000:],
            "durationMs": int((time.time() - started) * 1000),
            "peakMemoryBytes": peak[0]}


def gap(code: str, what: str, why: str, need: str, owner: str = "user") -> dict:
    return {"code": code, "what": what, "why": why, "need": need, "owner": owner}


TOOL_UPSTREAM = "https://github.com/fanvanzh/3dtiles"


def tool_missing(args, tools_dir: str | None) -> int:
    """缺转换器：打印「缺什么 / 应在哪 / 版本 / 怎么恢复」，写转换日志留痕，返回 4（跳过）。

    转换器随包分发，正常情况下不该缺；缺了说明安装包被裁剪或损坏，所以给「重装」而不是
    「去上游下载」。为什么是 4 不是 3：3 是「工具在但坏了/转换失败」，4 是「工具缺失」，
    总控据此记「跳过」而不是「失败」。
    """
    target = (os.path.join(tools_dir, EXE_REL) if tools_dir
              else os.path.join(SKILL_LOCAL_TOOLS, EXE_REL))
    doc = {
        "schema": SCHEMA, "schemaVersion": 1, "generatedAt": now_iso(),
        "status": "missing-tool", "ok": False,
        "reason": "TOOL_MISSING",
        "missing": {
            "what": "3dtile.exe（OSGB→3D Tiles 转换器，fanvanzh-3dtiles，随包分发）",
            "upstream": TOOL_UPSTREAM,
            "version": "v0.4（目录名必须是 v0.4，内含 3dtile.exe 与 osg/gdal 相关 DLL）",
            "target": target.replace("\\", "/"),
            "recover": "安装包不完整：重新安装本插件（Release tgz 或 github 安装）",
            "alternative": "或设环境变量 CARDO_OSGB_TOOLS 指向自备的 fanvanzh-3dtiles 目录",
        },
        "gaps": [gap("TOOL_MISSING", "包内缺 3dtile.exe，03 转换未执行",
                     "解析依赖转换器；本链不自研 OSGB 解析器",
                     "重装插件，或按 missing 字段用 CARDO_OSGB_TOOLS 指向 v0.4", "user")],
    }
    write_json(os.path.join(args.out, "转换日志.json"), doc)
    print("[03] 跳过：包内缺转换器，03 及之后不能跑（01/02 已完成的部分保留）。")
    print("[03]   缺什么：3dtile.exe（fanvanzh-3dtiles）")
    print("[03]   应在哪：%s" % target)
    print("[03]   需要的版本：v0.4（目录名必须为 v0.4）")
    print("[03]   怎么恢复：安装包不完整，重新安装本插件；或设 CARDO_OSGB_TOOLS 指向自备的 "
          "fanvanzh-3dtiles 目录（上游 %s）" % TOOL_UPSTREAM)
    print("[03]   恢复后重跑：python <技能目录>/Skills/00-Cardo低空解析总控/scripts/pipeline.py "
          "run --workspace . --from 03")
    print("[03] 转换日志（标记 missing-tool）：%s" % os.path.join(args.out, "转换日志.json"))
    return 4


def main() -> int:
    ap = argparse.ArgumentParser(description="03 · OSGB→3D Tiles 转换")
    ap.add_argument("--input", default=os.path.join("过程文件", "01-模型包接入", "输入清单.json"))
    ap.add_argument("--tools", default=os.path.join("过程文件", "02-工具探测", "工具探测.json"))
    ap.add_argument("--out", default=os.path.join("过程文件", "03-OSGB转3DTiles"))
    ap.add_argument("--tools-dir", default=None)
    args = ap.parse_args()

    if not os.path.isfile(args.input):
        print(f"[03] 缺 01 输入清单：{args.input}", file=sys.stderr)
        return 2
    manifest = read_json(args.input)
    tool_doc = read_json(args.tools) if os.path.isfile(args.tools) else {}
    tools_dir = resolve_tools_dir(args.tools_dir)
    exe = os.path.join(tools_dir, EXE_REL) if tools_dir else ""
    if not tools_dir or not os.path.isfile(exe):
        return tool_missing(args, tools_dir)
    expected_sha = next((t.get("sha256") for t in tool_doc.get("tools", [])
                         if t.get("name") == "3dtile"), None)
    actual_sha = sha256_file(exe)
    if expected_sha and actual_sha != expected_sha:
        print("[03] 工具 sha256 与 02 步登记不符（拒绝静默换版本）", file=sys.stderr)
        return 3
    if not tool_doc.get("ok", False):
        print("[03] 02 步工具探测未通过，拒绝转换", file=sys.stderr)
        return 3

    scene_dir = os.path.join("过程文件", "01-模型包接入", manifest["scene"]["dir"])
    if not os.path.isdir(scene_dir):
        print(f"[03] 解包样本目录不存在：{scene_dir}", file=sys.stderr)
        return 2

    out_dir = os.path.abspath(args.out)
    products = os.path.join(out_dir, "转换产物")
    log_path = os.path.join(out_dir, "转换日志.json")
    # 闸门纪律：先删旧产物与旧日志，失败不留下"上次的成果"
    shutil.rmtree(products, ignore_errors=True)
    if os.path.isfile(log_path):
        os.remove(log_path)
    work = os.path.join(out_dir, ".work")
    os.makedirs(products, exist_ok=True)

    in_dir, view = build_view(scene_dir, work)
    origin = (manifest.get("metadata") or {}).get("srsOrigin")
    if not origin:
        print("[03] 输入清单缺 SRSOrigin，无法定位", file=sys.stderr)
        return 2

    tool = run_tool(exe, in_dir, products)
    started_tail = f"[03] 3dtile.exe exit={tool['exitCode']} duration={tool['durationMs']}ms"
    print(started_tail)
    if tool["exitCode"] != 0 or not os.path.isfile(os.path.join(products, "tileset.json")):
        print(f"[03] 转换失败：{(tool['stderrTail'] or tool['stdoutTail'])[-800:]}", file=sys.stderr)
        return 3

    scan = tiles_inspect.scan_products(products)
    expected = sum(view["blocks"].values())
    problems = []
    if scan["missing"]:
        problems.append(f"tileset 引用缺失 {len(scan['missing'])} 条")
    if scan["failed"]:
        problems.append(f"b3dm 解析失败 {len(scan['failed'])} 个")
    if scan["b3dmDisk"] != expected:
        problems.append(f"b3dm 数 {scan['b3dmDisk']} ≠ 应转 {expected}")
    if scan["b3dmDisk"] != len(scan["contents"]):
        problems.append(f"磁盘 b3dm {scan['b3dmDisk']} ≠ tileset 引用 {len(scan['contents'])}")
    if scan["missingImages"]:
        problems.append(f"纹理缺失 {scan['missingImages']} 张")
    if scan["triangles"] == 0:
        problems.append("三角面为 0")

    meta = manifest.get("metadata") or {}
    bytes_on_disk = sum(a["bytes"] for a in tiles_inspect.artifacts_of(products))
    amplification = (round(bytes_on_disk / view["bytes"], 3) if view["bytes"] else None)
    doc = {
        "schema": SCHEMA, "schemaVersion": 1, "generatedAt": now_iso(),
        "producer": {"name": "cardo-osgb", "version": "0.1.0"},
        "input": {"manifest": args.input.replace("\\", "/"),
                  "sceneId": manifest["scene"]["sceneId"], "sceneDir": manifest["scene"]["dir"],
                  "counts": manifest.get("counts")},
        "crs": {"srs": meta.get("srs"), "vertical": meta.get("vertical"),
                "originalSrs": meta.get("srsOriginal"),
                "origin": [round(float(v), 6) for v in origin]},
        "view": view,
        "tool": {"name": "3dtile", "version": (tool_doc.get("tools") or [{}])[0].get("version"),
                 "sha256": actual_sha, "invocation": ["3dtile.exe", "-f", "osgb",
                                                       "-i", "<work>/in-osgb", "-o", "<out>/转换产物"],
                 "exitCode": tool["exitCode"], "durationMs": tool["durationMs"],
                 "peakMemoryBytes": tool.get("peakMemoryBytes"),
                 "stdoutTail": tile_scrub(tool["stdoutTail"], in_dir, products),
                 "stderrTail": tile_scrub(tool["stderrTail"], in_dir, products)},
        "products": {"dir": "转换产物", "b3dmCount": scan["b3dmDisk"],
                     "tilesetCount": scan["tilesetDisk"],
                     "contents": len(scan["contents"]), "maxContentDepth": scan["maxContentDepth"],
                     "lodCounts": scan["lods"], "rootTiles": scan["rootTiles"],
                     "triangles": scan["triangles"], "vertices": scan["vertices"],
                     "images": scan["images"], "materials": scan["materials"],
                     "missingImages": scan["missingImages"], "textureBytes": scan["textureBytes"],
                     "bboxMin": scan["bboxMin"], "bboxMax": scan["bboxMax"],
                     "bytesOnDisk": bytes_on_disk, "diskAmplification": amplification},
        "gaps": [gap("TOOL_FAILED", "转换或产物自校验未通过", "；".join(problems),
                     "检查输入瓦片完整性与工具版本后重跑", "user")] if problems else [],
        "ok": not problems,
    }
    write_json(log_path, doc)
    if problems:
        print("[03] 产物不完整：" + "；".join(problems), file=sys.stderr)
        return 3
    print(f"[03] 完成：{scan['b3dmDisk']} b3dm / {scan['tilesetDisk']} tileset / "
          f"{scan['triangles']} 三角面 / {scan['images']} 纹理 / {tool['durationMs']} ms / "
          f"峰值 {tool.get('peakMemoryBytes')} B / 磁盘放大 {amplification}×")
    return 0


def tile_scrub(text: str, in_dir: str, products: str) -> str:
    for raw, token in ((in_dir, "<work>/in-osgb"), (products, "<out>/转换产物")):
        if raw:
            text = text.replace(raw, token).replace(raw.replace("\\", "/"), token)
    return text


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
