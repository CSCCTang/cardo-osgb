#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""01 · 模型包接入：安全解包 + ASCII 路径归一 + Data/<Tile>/ 布局检查 + metadata 去 BOM + SRS 校验。

输入：OSGB 模型 zip（或已解开的目录，--dir）；只读，绝不回写输入。
输出（默认落 过程文件/01-模型包接入/）：
    解包/<scene-id>/            归一化后的样本树（只含 ASCII 路径段）
    输入清单.json               cardo/lowair-input/v1（文件/字节/块/LOD/metadata/SRS/文件清单）
    解包日志.md                 人读日志（安全检查、归一化映射、布局与 SRS 处置）

退出码：0 成功；2 参数/IO/zip 损坏；3 不安全条目；4 输出已存在未加 --force；5 布局或 SRS 不合规。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
import zipfile
from datetime import datetime, timedelta, timezone

SCHEMA = "cardo/lowair-input/v1"
TZ = timezone(timedelta(hours=8))
MAX_ENTRIES = 20000
MAX_ENTRY_BYTES = 256 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024
MAX_RATIO = 200
ALLOWED_SRS = "EPSG:4544"
_LOD_RE = re.compile(r"_L(\d{1,2})_")
_WIN_DEVICE = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10)),
}
OUT_DIR_DEFAULT = os.path.join("过程文件", "01-模型包接入")


class UnsafeEntry(Exception):
    """安全风险：路径穿越 / 绝对路径 / 符号链接 / 压缩炸弹 / 设备名等。"""

    def __init__(self, message, exit_code=3):
        super().__init__(message)
        self.exit_code = exit_code


def promote_dir(staging: str, scene_dir: str, retries: int = 5) -> dict:
    """把 staging 目录搬到 scene_dir。

    为什么不用裸的 `os.replace`：Windows 上把一个**刚写完几十 MB 文件**的目录
    改名成另一个名字，可能瞬时返回 WinError 5（拒绝访问）——
    杀毒软件/索引服务/资源管理器预览此时正持有该目录内文件的句柄。
    这是环境竞态，不是数据问题，但裸 replace 会直接判失败退出 2，把一次正常的开工打断。

    三级降级：
      1. `os.replace`（原子改名，最快，通常一次成功）
      2. 退避重试（给扫描器/杀软释放句柄的时间）
      3. 退化为「逐文件拷贝进目标目录」+ 删 staging（不依赖目录改名）

    返回处置记录（写进解包日志.md）：用了哪一级、重试几次、原始错误是什么。
    最终目标不变：scene_dir 里是完整解包结果；失败则半成品一定被清理。
    """
    delay = 0.25
    last = None
    for attempt in range(retries):
        try:
            os.replace(staging, scene_dir)
            return {"strategy": "replace", "attempts": attempt + 1, "error": None}
        except OSError as exc:      # WinError 5 / 32 等瞬时占用
            last = exc
            if os.path.exists(scene_dir):
                shutil.rmtree(scene_dir, ignore_errors=True)
            time.sleep(delay)
            delay *= 2
    # 最后降级：不用目录改名，逐文件搬
    try:
        shutil.copytree(staging, scene_dir, dirs_exist_ok=True)
    except OSError as exc:
        raise OSError(f"{exc}（重试 {retries} 次后仍失败，原始错误：{last}）") from exc
    shutil.rmtree(staging, ignore_errors=True)
    return {"strategy": "copytree", "attempts": retries, "error": str(last) if last else None}


class LayoutError(Exception):
    """布局不合规：打包方式错（多根目录、metadata 不在根下、块结构缺失等）。

    与 UnsafeEntry 分开——它不是安全风险，只是包打错了，给出改法即可。
    """

    def __init__(self, message, fix=""):
        super().__init__(message)
        self.fix = fix
        self.exit_code = 5


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def write_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def write_json(path: str, data) -> None:
    write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def safe_segment(seg: str) -> str:
    if not seg or seg in (".", ".."):
        raise UnsafeEntry(f"非法路径段：{seg!r}")
    if any(ch in seg for ch in ("\x00", ":", "*", "?", '"', "<", ">", "|", "/", "\\")):
        raise UnsafeEntry(f"路径段含非法字符：{seg!r}")
    seg = seg.rstrip(" .")
    if not seg:
        raise UnsafeEntry("路径段清洗后为空")
    if seg.split(".")[0].upper() in _WIN_DEVICE:
        raise UnsafeEntry(f"Windows 设备名路径段：{seg!r}")
    if len(seg.encode("utf-8")) > 200:
        raise UnsafeEntry(f"路径段过长：{seg[:40]!r}…")
    return seg


def normalize_zip_name(raw: str) -> list[str]:
    name = raw.replace("\\", "/")
    if "\x00" in name:
        raise UnsafeEntry("条目名含 NUL 字节")
    if re.match(r"^[A-Za-z]:", name):
        raise UnsafeEntry(f"绝对路径条目：{name!r}")
    if name.startswith("//"):
        raise UnsafeEntry(f"UNC 路径条目：{name!r}")
    if name.startswith("/"):
        raise UnsafeEntry(f"绝对路径条目：{name!r}")
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts:
        raise UnsafeEntry("条目名归一化后为空")
    return [safe_segment(p) for p in parts]


def is_symlink(info: zipfile.ZipInfo) -> bool:
    return ((info.external_attr >> 16) & 0o170000) == 0o120000


def ascii_segment(seg: str, used: set[str], index: int) -> tuple[str, bool]:
    """把非 ASCII 段改写为稳定 ASCII（原名只进映射，不进路径）。"""
    if all(ord(ch) < 128 for ch in seg):
        return seg, False
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", seg).strip("-._") or f"item-{index:02d}"
    candidate, n = base, 1
    while candidate.lower() in used:
        n += 1
        candidate = f"{base}-{n}"
    used.add(candidate.lower())
    return candidate, True


def parse_metadata(path: str) -> dict:
    raw = open(path, "rb").read()
    has_bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig", errors="replace")
    info: dict = {"present": True, "path": "metadata.xml", "hasBom": has_bom}

    def grab(tag: str):
        match = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.S)
        return match.group(1).strip() if match else None

    info["rootTag"] = (re.search(r"<([A-Za-z0-9_]+)[ >]", text) or [None, None])[1]
    info["srsOriginal"] = grab("SRS")
    info["colorSource"] = grab("ColorSource")
    origin = grab("SRSOrigin")
    try:
        nums = [float(v) for v in origin.split(",")] if origin else None
    except ValueError:
        nums = None
    info["originRaw"] = origin
    info["srsOrigin"] = nums if nums and len(nums) == 3 else None
    return info


def normalize_srs(srs: str | None) -> dict:
    """只收单码 EPSG:4544；复合码归一记录信息项，其它码直接不合规。"""
    if not srs:
        return {"ok": False, "code": "SRS_UNSUPPORTED", "srs": None, "vertical": None,
                "why": "metadata.xml 缺 <SRS>"}
    parts = [p.strip() for p in srs.split("+") if p.strip()]
    horizontal = parts[0].upper().replace(" ", "")
    vertical = parts[1].upper() if len(parts) > 1 else None
    if vertical and not vertical.startswith("EPSG:"):
        vertical = "EPSG:" + vertical
    if horizontal != ALLOWED_SRS:
        return {"ok": False, "code": "SRS_UNSUPPORTED", "srs": horizontal, "vertical": vertical,
                "why": f"SRS={srs} 不在白名单（只收单码 {ALLOWED_SRS}）"}
    return {"ok": True, "srs": ALLOWED_SRS, "vertical": vertical, "original": srs,
            "composite": bool(vertical)}


def detect_root(file_parts: list[list[str]]) -> str | None:
    roots = {parts[0] for parts in file_parts}
    if len(roots) == 1 and all(len(parts) > 1 for parts in file_parts):
        return roots.pop()
    return None


def strip_bom(path: str) -> bool:
    raw = open(path, "rb").read()
    if not raw.startswith(b"\xef\xbb\xbf"):
        return False
    with open(path, "wb") as fh:
        fh.write(raw[3:])
    return True


def rewrite_srs(path: str, srs: str) -> None:
    text = open(path, encoding="utf-8", errors="replace").read()
    text = re.sub(r"<SRS>.*?</SRS>", f"<SRS>{srs}</SRS>", text, count=1, flags=re.S)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def summarize_entries(entries: list[tuple[str, int, str | None]]) -> dict:
    """统一统计模型树的块 / LOD / 顶层 osgb / 文件清单。

    entries = [(相对路径, 字节数, 绝对路径或 None)]。
    build_manifest（已解包目录）与 inspect_zip（只读 zip）**共用这一份**，
    保证"预检查的布局"与"实跑检查的布局"是同一个口径。
    """
    blocks: dict[str, dict] = {}
    lod_levels: set[int] = set()
    top_level: list[dict] = []
    files: list[dict] = []
    for rel, size, full in entries:
        files.append({"path": rel, "bytes": size,
                      "sha256": sha256_file(full) if full else ""})
        name = rel.rsplit("/", 1)[-1]
        match = _LOD_RE.search(name)
        if match:
            lod_levels.add(int(match.group(1)))
        if "/" not in rel:
            if name.lower().endswith(".osgb"):
                top_level.append({"name": name, "bytes": size})
            continue
        first = rel.split("/")[0]
        info = blocks.setdefault(first, {"name": first, "osgb": 0, "files": 0, "bytes": 0,
                                         "lod": set(), "rootTile": False})
        info["files"] += 1
        info["bytes"] += size
        if name.lower().endswith(".osgb"):
            info["osgb"] += 1
            if name[:-5] == first:
                info["rootTile"] = True
    for info in blocks.values():
        info["lod"] = sorted(info["lod"])
    return {"blocks": sorted(blocks.values(), key=lambda item: item["name"]),
            "lodLevels": sorted(lod_levels), "topLevel": top_level, "files": files}


def build_manifest(scene_dir: str) -> dict:
    entries = []
    for dirpath, _dirs, filenames in os.walk(scene_dir):
        rel_dir = os.path.relpath(dirpath, scene_dir).replace("\\", "/")
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            entries.append((rel, os.path.getsize(full), full))
    return summarize_entries(entries)


def inspect_zip(path: str, max_bytes: int = MAX_TOTAL_BYTES) -> dict:
    """**只读**体检模型包：不解包、不落盘、不改任何字节。

    判据与实跑完全同源（同一套 normalize_zip_name / detect_root / normalize_srs /
    summarize_entries），所以"预检说通过"就意味着"实跑不会因布局被拦"。
    开工.py 的预检直接调它。

    返回 {ok, code, why, fix, srs, vertical, root, blocks, layoutProblems, entries}。
    code ∈ OK / EMPTY / TOO_LARGE / LAYOUT_NOT_SINGLE_ROOT / NO_METADATA /
    SRS_UNSUPPORTED / LAYOUT_INVALID / UNSAFE_ENTRY / BAD_ZIP。
    """
    out = {"ok": False, "code": "", "why": "", "fix": "", "srs": None, "vertical": None,
           "root": None, "blocks": [], "layoutProblems": [], "entries": 0}
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            parsed = [(info, normalize_zip_name(info.filename)) for info in infos]
            file_parts = [(info, parts) for info, parts in parsed if not info.is_dir()]
            if not file_parts:
                out.update(code="EMPTY", why="压缩包内没有文件条目",
                           fix="确认打包的是模型目录，而不是空目录")
                return out
            total = sum(info.file_size for info, _ in file_parts)
            if total > max_bytes:
                out.update(code="TOO_LARGE",
                           why=f"解压总量 {total} 字节超过上限 {max_bytes}",
                           fix="换一个更小的模型包，或确认没有把无关目录打进去")
                return out

            root = detect_root([parts for _info, parts in file_parts])
            if root is None:
                roots = sorted({parts[0] for _info, parts in file_parts})
                out.update(
                    code="LAYOUT_NOT_SINGLE_ROOT",
                    why="包内不是单一根目录（顶层有 {} 个目录：{}）".format(
                        len(roots), "、".join(roots[:8])),
                    fix="Smart3D 要求包内只有一个根目录：把这 {} 个目录的内容合并到同一个文件夹下，"
                        "再把那个文件夹整体打成 zip".format(len(roots)))
                return out
            out["root"] = root

            entries = [("/".join(parts[1:]), info.file_size, None)
                       for info, parts in file_parts]
            manifest = summarize_entries(entries)

            meta_info = next((info for info, parts in file_parts
                              if len(parts) == 2 and parts[1].lower() == "metadata.xml"), None)
            if meta_info is None:
                out.update(code="NO_METADATA",
                           why="压缩包内未找到 {}/metadata.xml（不是标准 Smart3D 布局）".format(root),
                           fix="从建模软件重新导出整包（含 metadata.xml 与 Data/），不要只打包 Data 目录")
                return out
            raw = zf.read(meta_info).decode("utf-8-sig", errors="replace")

            srs_match = re.search(r"<SRS>(.*?)</SRS>", raw, re.S)
            srs = normalize_srs(srs_match.group(1).strip() if srs_match else None)
            if not srs.get("ok"):
                out.update(code="SRS_UNSUPPORTED", why=srs.get("why", ""),
                           fix="请在建模软件里把水平坐标系重投影到 {} 后重新导出打包".format(ALLOWED_SRS),
                           srs=srs.get("srs"), vertical=srs.get("vertical"))
                return out
            out["srs"] = srs.get("srs")
            out["vertical"] = srs.get("vertical")

            problems: list[str] = []
            if not manifest["blocks"]:
                problems.append("没有 Data/<Tile>/ 块子目录（所有 osgb 都在根或单层）")
            for block in manifest["blocks"]:
                if not block["rootTile"]:
                    problems.append("块 {} 缺根瓦片 {}.osgb".format(block["name"], block["name"]))
            origin_match = re.search(r"<SRSOrigin>(.*?)</SRSOrigin>", raw, re.S)
            origin = origin_match.group(1).strip() if origin_match else ""
            nums = None
            if origin:
                try:
                    nums = [float(v) for v in origin.split(",")]
                except ValueError:
                    nums = None
            if not nums or len(nums) != 3:
                problems.append("metadata.xml 缺可解析的 SRSOrigin（三值）")

            out["blocks"] = manifest["blocks"]
            out["layoutProblems"] = problems
            out["entries"] = len(infos)
            if problems:
                out.update(code="LAYOUT_INVALID", why="；".join(problems),
                           fix="用建模软件重新导出完整模型包（要含 Data/ 结构、每个块的根瓦片、"
                               "以及可解析的三值 SRSOrigin）")
                return out
            out.update(ok=True, code="OK")
            return out
    except UnsafeEntry as exc:
        out.update(code="UNSAFE_ENTRY", why="包里有不安全条目：{}".format(exc),
                   fix="重新打包：不要带绝对路径 / .. 穿越 / 符号链接 / 设备名")
        return out
    except (zipfile.BadZipFile, EOFError, OSError) as exc:
        out.update(code="BAD_ZIP", why="读取 zip 失败：{}: {}".format(type(exc).__name__, exc),
                   fix="zip 损坏或读不了，重新打包一次")
        return out


def main() -> int:
    ap = argparse.ArgumentParser(description="01 · 模型包接入（安全解包 + ASCII 归一 + 布局/SRS 校验）")
    ap.add_argument("--zip", default=None, help="模型 zip 路径（只读）")
    ap.add_argument("--dir", default=None, help="已解开的模型目录（与 --zip 二选一）")
    ap.add_argument("--out", default=OUT_DIR_DEFAULT, help="过程输出目录")
    ap.add_argument("--scene-id", default="model-01", help="归一化后的样本目录名（纯 ASCII）")
    ap.add_argument("--force", action="store_true", help="覆盖已有解包目录")
    ap.add_argument("--max-bytes", type=int, default=MAX_TOTAL_BYTES)
    ap.add_argument("--check-only", action="store_true",
                    help="只读体检模型包（不解包、不落盘）；输出 JSON 判决")
    args = ap.parse_args()

    # --check-only：只读体检。不解包、不落盘、不改输入的任何一个字节。
    if args.check_only:
        if not args.zip:
            print("[01] --check-only 必须配 --zip", file=sys.stderr)
            return 2
        if not os.path.isfile(args.zip):
            print("[01] zip 不存在：{}".format(args.zip), file=sys.stderr)
            return 2
        verdict = inspect_zip(args.zip, args.max_bytes)
        print(json.dumps(verdict, ensure_ascii=False, indent=2))
        return 0 if verdict["ok"] else 3

    scene_id = args.scene_id
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", scene_id or ""):
        print("[01] --scene-id 必须是 ASCII（字母数字 . _ -）", file=sys.stderr)
        return 2
    out_dir = os.path.abspath(args.out)
    scene_dir = os.path.join(out_dir, "解包", scene_id)
    if os.path.exists(scene_dir) and not args.force:
        print(f"[01] 解包目录已存在：{os.path.relpath(scene_dir)}（加 --force 覆盖）", file=sys.stderr)
        return 4

    safety = {"entries": 0, "rejected": [], "bomStripped": False, "renamed": []}
    zip_info = None
    # staging 名不用点开头：Windows 对 `.xxx` 目录有特殊处理（隐藏属性 + 索引器
    # 单独扫描），刚写完大文件时更容易撞上句柄占用。纯 ASCII 普通名更稳。
    staging = os.path.join(out_dir, "解包", f"_tmp-{os.getpid()}")
    shutil.rmtree(staging, ignore_errors=True)

    try:
        if args.zip:
            if not os.path.isfile(args.zip):
                print(f"[01] zip 不存在：{args.zip}", file=sys.stderr)
                return 2
            zip_info = {"name": os.path.basename(args.zip), "bytes": os.path.getsize(args.zip),
                        "sha256": sha256_file(args.zip)}
            os.makedirs(staging, exist_ok=True)
            with zipfile.ZipFile(args.zip) as zf:
                infos = zf.infolist()
                safety["entries"] = len(infos)
                if len(infos) > MAX_ENTRIES:
                    raise UnsafeEntry(f"条目数 {len(infos)} 超过上限 {MAX_ENTRIES}")
                parsed = [(info, normalize_zip_name(info.filename)) for info in infos]
                file_parts = [parts for info, parts in parsed if not info.is_dir()]
                if not file_parts:
                    print("[01] zip 内没有文件条目", file=sys.stderr)
                    return 2
                root = detect_root(file_parts)
                if root is None:
                    roots = sorted({parts[0] for parts in file_parts})
                    raise LayoutError(
                        "未识别出唯一样本根目录（包内顶层有 {} 个目录：{}）".format(
                            len(roots), "、".join(roots[:8])),
                        "把这 {} 个目录的内容合并到同一个文件夹下，再把那个文件夹整体打成 zip".format(len(roots)))
                total = sum(info.file_size for info, _p in parsed if not info.is_dir())
                if total > args.max_bytes:
                    raise UnsafeEntry(f"解压总量 {total} 超过上限 {args.max_bytes}")

                plan, seen, used = [], {}, set()
                for index, (info, parts) in enumerate(parsed):
                    if is_symlink(info):
                        raise UnsafeEntry(f"符号链接条目：{info.filename!r}")
                    if info.is_dir():
                        continue
                    if info.file_size > MAX_ENTRY_BYTES:
                        raise UnsafeEntry(f"单条过大：{info.filename!r} = {info.file_size}")
                    if info.compress_size > 0 and info.file_size / info.compress_size > MAX_RATIO:
                        raise UnsafeEntry(f"压缩比异常（疑似 zip 炸弹）：{info.filename!r}")
                    segs, renamed = [], False
                    for seg in parts[1:]:
                        new, changed = ascii_segment(seg, used, index)
                        if changed:
                            renamed = True
                        segs.append(new)
                    rel = "/".join(segs)
                    if renamed:
                        safety["renamed"].append({"original": "/".join(parts[1:]), "normalized": rel})
                    lowered = rel.lower()
                    if lowered in seen:
                        raise UnsafeEntry(f"归一化路径冲突（大小写不敏感）：{rel!r}")
                    seen[lowered] = rel
                    plan.append((info, segs, rel))
                for info, segs, rel in plan:
                    dest = os.path.join(staging, *segs)
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    with zf.open(info) as src, open(dest, "wb") as dst:
                        shutil.copyfileobj(src, dst, 1 << 20)
        elif args.dir:
            if not os.path.isdir(args.dir):
                print(f"[01] 目录不存在：{args.dir}", file=sys.stderr)
                return 2
            os.makedirs(staging, exist_ok=True)
            used = set()
            for dirpath, _dirs, filenames in os.walk(args.dir):
                rel_dir = os.path.relpath(dirpath, args.dir)
                for name in sorted(filenames):
                    rel_parts = [] if rel_dir == "." else rel_dir.split(os.sep)
                    segs, renamed = [], False
                    for index, seg in enumerate([*rel_parts, name]):
                        new, changed = ascii_segment(seg, used, index)
                        renamed = renamed or changed
                        segs.append(new)
                    if renamed:
                        safety["renamed"].append({"original": "/".join([*rel_parts, name]),
                                                  "normalized": "/".join(segs)})
                    dest = os.path.join(staging, *segs)
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    shutil.copyfile(os.path.join(dirpath, name), dest)
            safety["entries"] = sum(1 for _d, _s, fs in os.walk(staging) for _f in fs)
        else:
            print("[01] 必须给 --zip 或 --dir", file=sys.stderr)
            return 2
    except LayoutError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        print(f"[01] 布局不合规：{exc}", file=sys.stderr)
        if exc.fix:
            print(f"[01] 怎么改：{exc.fix}", file=sys.stderr)
        return exc.exit_code
    except UnsafeEntry as exc:
        shutil.rmtree(staging, ignore_errors=True)
        print(f"[01] 拒绝（不安全条目）：{exc}", file=sys.stderr)
        return 3
    except (zipfile.BadZipFile, EOFError, OSError) as exc:
        shutil.rmtree(staging, ignore_errors=True)
        print(f"[01] 解包失败（已清理半成品）：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # zlib.error 等损坏流
        shutil.rmtree(staging, ignore_errors=True)
        print(f"[01] 解包失败（损坏数据）：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    try:
        if os.path.exists(scene_dir):
            shutil.rmtree(scene_dir)
        os.makedirs(os.path.dirname(scene_dir), exist_ok=True)
        promote = promote_dir(staging, scene_dir)
    except OSError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        print(f"[01] 写入目标目录失败：{exc}", file=sys.stderr)
        return 2

    meta_path = os.path.join(scene_dir, "metadata.xml")
    if not os.path.isfile(meta_path):
        print("[01] 解包后缺 metadata.xml（Smart3D 布局要求根目录带 metadata.xml）", file=sys.stderr)
        return 5
    safety["bomStripped"] = strip_bom(meta_path)
    meta = parse_metadata(meta_path)
    srs = normalize_srs(meta.get("srsOriginal"))
    if srs.get("ok"):
        rewrite_srs(meta_path, srs["srs"])
    meta["srs"] = srs.get("srs") if srs.get("ok") else None
    meta["vertical"] = srs.get("vertical")
    meta["srsOk"] = bool(srs.get("ok"))
    meta["srsWhy"] = srs.get("why") if not srs.get("ok") else ""

    manifest = build_manifest(scene_dir)
    total_bytes = sum(f["bytes"] for f in manifest["files"])
    problems: list[str] = []
    if not manifest["blocks"]:
        problems.append("没有 Data/<Tile>/ 块子目录（所有 osgb 都在根或单层）")
    for block in manifest["blocks"]:
        if not block["rootTile"]:
            problems.append(f"块 {block['name']} 缺根瓦片 {block['name']}.osgb")
    if meta.get("srsOrigin") is None:
        problems.append("metadata.xml 缺可解析的 SRSOrigin（三值）")

    block_txt = "; ".join(f"{b['name']}（{b['osgb']} osgb，LOD {b['lod']}）"
                          for b in manifest["blocks"]) or "无"
    log = ["# 01 · 模型包接入日志", "",
           f"- 生成时间：{now_iso()}",
           f"- 来源：{'zip ' + (zip_info or {}).get('name', '') if zip_info else '目录 ' + os.path.basename(os.path.abspath(args.dir))}",
           f"- 归一化样本目录：`{os.path.relpath(scene_dir)}`",
           f"- 匿名/ASCII 场景 ID：`{scene_id}`",
           "",
           "## 安全与归一化", "",
           f"- zip 条目数：{safety['entries']}；拒绝条目：{len(safety['rejected'])}",
           f"- 路径段改写（非 ASCII → ASCII）：{len(safety['renamed'])} 条",
           "- 校验项：绝对路径 / `..` 穿越 / 符号链接 / Windows 设备名 / 单条与总量上限 / 压缩比 / 大小写冲突",
           f"- metadata.xml 去 BOM：{'是' if safety['bomStripped'] else '否（原本无 BOM）'}",
           "",
           "## 坐标系（SRS）", "",
           f"- 原始 SRS：`{meta.get('srsOriginal')}`；归一后：`{meta.get('srs')}`",
           f"- 垂直码：`{meta.get('vertical') or '未声明'}`",
           f"- SRSOrigin：{meta.get('srsOrigin')}",
           f"- 结论：{'合规（复合码已归一为单码，垂直位移由 04 步按 SRSOrigin.z 补偿）' if meta.get('srsOk') else '不合规：' + meta.get('srsWhy', '')}",
           "",
           "## 目录布局与规模", "",
           f"- 文件 {len(manifest['files'])} 个 / {total_bytes} B；osgb {sum(b['osgb'] for b in manifest['blocks'])} 个",
           f"- 块：{block_txt}",
           f"- LOD 档：{manifest['lodLevels']}",
           f"- 顶层 osgb（不参与转换）：{[i['name'] for i in manifest['topLevel']] or '无'}",
           f"- 布局问题：{len(problems)} 项" + ("：" + "；".join(problems) if problems else ""),
           "",
           "## 落盘处置（目标被占用时会记录在这里）", "",
           f"- 落盘方式：{'原子改名（os.replace）' if promote.get('strategy') == 'replace' else '退化为逐文件拷贝（copytree）'}",
           f"- 尝试次数：{promote.get('attempts', 1)} 次",
           ]
    if promote.get("error"):
        lines_note = [f"- 遇到过目标被占用（WinError 5 一类）：{promote['error']}",
                      "- 常见原因：资源管理器正打开该目录、杀毒软件正在扫描、或另一进程占用句柄；",
                      "  本次已自动绕开（重试后改用逐文件拷贝），结果完整，无需人工处理。",
                      "- 若下次仍失败：关掉占用该目录的窗口后重跑 01 步即可。"]
        log += lines_note
    else:
        log += ["- 本次未遇到目标被占用，一次落盘成功。"]
    log += [""]
    write_text(os.path.join(out_dir, "解包日志.md"), "\n".join(log))

    doc = {
        "schema": SCHEMA, "schemaVersion": 1, "generatedAt": now_iso(),
        "producer": {"name": "cardo-osgb", "version": "0.1.0"},
        "source": {"kind": "zip" if zip_info else "dir", "zip": zip_info,
                   "dirName": os.path.basename(os.path.abspath(args.dir)) if args.dir else None},
        "scene": {"sceneId": scene_id, "dir": "解包/" + scene_id},
        "counts": {"files": len(manifest["files"]), "bytes": total_bytes,
                   "osgb": sum(b["osgb"] for b in manifest["blocks"]),
                   "blocks": len(manifest["blocks"]), "topLevelOsgb": len(manifest["topLevel"])},
        "blocks": manifest["blocks"], "lodLevels": manifest["lodLevels"],
        "topLevel": manifest["topLevel"],
        "metadata": meta,
        "layout": {"ok": not problems, "problems": problems},
        "safety": {"entries": safety["entries"], "rejected": safety["rejected"],
                   "renamed": safety["renamed"], "bomStripped": safety["bomStripped"],
                   "violations": 0},
        "files": manifest["files"],
    }
    write_json(os.path.join(out_dir, "输入清单.json"), doc)
    print(f"[01] 完成：{len(manifest['files'])} 文件 / {total_bytes} B / "
          f"{len(manifest['blocks'])} 块 / LOD {manifest['lodLevels']} / SRS={meta.get('srs')}")
    if problems:
        print("[01] 布局/元数据问题：" + "；".join(problems), file=sys.stderr)
        return 5
    if not meta.get("srsOk"):
        print(f"[01] SRS 不合规：{meta.get('srsWhy')}", file=sys.stderr)
        return 5
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.dont_write_bytecode = True
    raise SystemExit(main())
