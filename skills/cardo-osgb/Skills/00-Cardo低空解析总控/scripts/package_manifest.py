#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""包完整性清单：生成 / 校验 PACKAGE_MANIFEST.json。

为什么要有这个：交付包是「拷走就能跑」的设计，但拷贝过程可能丢东西——
测试机上就出现过整个 `Skills/` 目录没复制过去、`runtime/python` 缺 trimesh，
都是**跑起来才暴露**，而且分两次报错（先说工具缺失、再说依赖缺失）。

这个清单把「包该有什么」固化成一份可校验的基线：
- `generate`：给当前包生成一份基线（改包后重新生成）
- `verify`  ：拿现有清单核对当前包，缺什么、哪个哈希对不上，一次全列出来

**清单本身随包走**：拷包时它和包一起走，所以「包被裁剪」这件事在**开工前**
就能查出来，而不是跑到一半才炸。

只读包，不写包（除 generate 模式显式写清单文件）。
退出码：0 通过 / 1 校验不过 / 2 用法或环境问题。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

SCHEMA = "cardo/lowair-package-manifest/v1"

# --- 关键文件清单 -----------------------------------------------------------
# 只列「缺了就跑不动」的东西，不列 runtime 的全部 .pyd（几千个文件，
# 清单会大到没法看，而且那些是 Python 自带的，不算交付内容）。

# 每一批流水线要用的脚本（缺一个，那一步就 exit 2）
SKILL_SCRIPTS = [
    "Skills/00-Cardo低空解析总控/scripts/pipeline.py",
    "Skills/00-Cardo低空解析总控/scripts/export_algo_table.py",
    "Skills/01-模型包接入/scripts/unpack_model.py",
    "Skills/02-工具探测/scripts/probe_tools.py",
    "Skills/02-工具探测/scripts/fetch_tools.py",
    "Skills/02-工具探测/scripts/verify_tools.py",
    "Skills/03-OSGB转3DTiles/scripts/convert.py",
    "Skills/04-垂直配准修正/scripts/fix_registration.py",
    "Skills/05-模型体检/scripts/inspect_model.py",
    "Skills/05-模型体检/scripts/check_contract.py",
    "Skills/06-2.5D网格指标/scripts/build_indicators.py",
    "Skills/06-2.5D网格指标/scripts/run_indicators.py",
    "Skills/06-2.5D网格指标/scripts/texture_materials.py",
    "Skills/06-2.5D网格指标/scripts/validate_texture_truth.py",
    "Skills/07-成果导出/scripts/export_deliverables.py",
    "Skills/07-成果导出/scripts/build_browse_package.py",
    "Skills/07-成果导出/scripts/readability.py",
    # 07 的两个子模块：依据对照表与空白页自检，缺一个 07 就 exit 2/3
    "Skills/07-成果导出/scripts/build_evidence_xlsx.py",
    "Skills/07-成果导出/scripts/blank_page_guard.py",
    # 07 的成品 PDF 渲染链（docx→HTML→Edge 打印→回填目录页码），缺了 07 出不了 PDF
    "Skills/07-成果导出/scripts/pdf_render.py",
    "Skills/08-验证与成稿/scripts/independent_verify.py",
    "Skills/09-三维要素反演/scripts/run_reconstruct.py",
    "Skills/10-矢量入库/scripts/build_vectors.py",
    "Skills/11-几何增强/scripts/run_enhance.py",
    "Skills/12-屋顶与立面/scripts/run_roof_facade.py",
    "Skills/13-矢量增强/scripts/run_vector.py",
    "Skills/14-扩展指标/scripts/build_extended_metrics.py",
    "Skills/14-扩展指标/scripts/build_observable_matrix.py",
    "Skills/16-场地要素/scripts/extract_site_features.py",
]

# 每个技能的规则文件与契约（缺了能跑但口径会退）
SKILL_ASSETS = [
    "Skills/06-2.5D网格指标/规则/指标口径.md",
    "Skills/06-2.5D网格指标/规则/纹理材质参数.json",
    "Skills/06-2.5D网格指标/规则/纹理材质识别.md",
    "Skills/00-Cardo低空解析总控/算法注册表.json",
    # 07 模型浏览引擎资产：缺任一就跑不出自足浏览目录
    "assets/viewer/VERSION.json",
    "assets/viewer/许可登记.json",
    "assets/viewer/viewer/project.schema.json",
    "assets/viewer/viewer/2d/index.html",
    "assets/viewer/viewer/3d/index.html",
    "assets/viewer/scripts/build_ortho.py",
    "assets/viewer/scripts/project_pack.py",
    "assets/viewer/tool/caddy.exe",
]

# 随包二进制工具（禁止替换版本，只校验完整性）
TOOL_BINARIES = [
    "Skills/02-工具探测/tools/fanvanzh-3dtiles/v0.4/3dtile.exe",
    "Skills/02-工具探测/tools/fanvanzh-3dtiles/v0.4/gdal201.dll",
    "Skills/02-工具探测/tools/fanvanzh-3dtiles/v0.4/osg130-osg.dll",
    "Skills/02-工具探测/tools/fanvanzh-3dtiles/v0.4/osg130-osgDB.dll",
    "Skills/02-工具探测/tools/fanvanzh-3dtiles/v0.4/osg130-osgUtil.dll",
    "Skills/02-工具探测/tools/fanvanzh-3dtiles/v0.4/osgPlugins-3.4.0/osgdb_serializers_osg.dll",
]

# runtime 依赖包（pip 包名 → site-packages 里的目录名）
RUNTIME_DEPS = {
    "docx": "docx",
    "openpyxl": "openpyxl",
    "numpy": "numpy",
    "scipy": "scipy",
    "Pillow": "PIL",
    "trimesh": "trimesh",
    "scikit-image": "skimage",
    "opencv-python-headless": "cv2",
    "PyYAML": "yaml",
}

RUNTIME_DIR = os.path.join("runtime", "python")


def sha256_file(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def package_root() -> str:
    """包根 = 本脚本上溯 3 级：scripts → 00-总控 → Skills → 包根。"""
    return os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "..", "..", ".."))


def site_packages(root: str) -> str:
    return os.path.join(root, RUNTIME_DIR, "Lib", "site-packages")


def collect(root: str) -> dict:
    """扫当前包，产出清单内容。"""
    files = {}
    for rel in SKILL_SCRIPTS + SKILL_ASSETS + TOOL_BINARIES:
        full = os.path.join(root, rel.replace("/", os.sep))
        if not os.path.isfile(full):
            continue
        files[rel] = {"bytes": os.path.getsize(full), "sha256": sha256_file(full)}

    sp = site_packages(root)
    deps = {}
    for pkg, dirname in RUNTIME_DEPS.items():
        full = os.path.join(sp, dirname)
        if os.path.isdir(full):
            # 用 __init__.py 的哈希代表整个包（够用，且清单小）
            init = os.path.join(full, "__init__.py")
            marker = init if os.path.isfile(init) else full
            deps[pkg] = {
                "dir": dirname,
                "present": True,
                "sha256": sha256_file(marker) if os.path.isfile(marker) else "",
            }
        else:
            deps[pkg] = {"dir": dirname, "present": False, "sha256": ""}

    runtime_py = os.path.join(root, RUNTIME_DIR,
                              "python.exe" if os.name == "nt" else "python")
    return {
        "schema": SCHEMA,
        "schemaVersion": 1,
        "generator": "Skills/00-Cardo低空解析总控/scripts/package_manifest.py",
        "files": files,
        "runtimeDeps": deps,
        "runtimePython": {
            "rel": os.path.relpath(runtime_py, root).replace("\\", "/"),
            "present": os.path.isfile(runtime_py),
            "bytes": os.path.getsize(runtime_py) if os.path.isfile(runtime_py) else 0,
        },
    }


def cmd_generate(args) -> int:
    root = os.path.abspath(args.root)
    doc = collect(root)
    out = os.path.join(root, args.out)
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print(f"[manifest] 已生成 {os.path.relpath(out, root)}："
          f"{len(doc['files'])} 个关键文件 + {len(doc['runtimeDeps'])} 个 runtime 依赖")
    return 0


def cmd_verify(args) -> int:
    """核对当前包与清单。

    报两类问题：
      - 缺失：清单里有、当前包没有（包被裁剪）
      - 不符：当前包有、但 sha256 对不上（包被改动/损坏）
    一次性全列出来，不分批报错。
    """
    root = os.path.abspath(args.root)
    manifest_path = os.path.join(root, args.manifest)
    if not os.path.isfile(manifest_path):
        print(f"[manifest] 缺清单 {args.manifest}（包根应有此文件；"
              f"可用 package_manifest.py generate 生成）", file=sys.stderr)
        return 2
    try:
        with open(manifest_path, encoding="utf-8-sig") as fh:
            base = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"[manifest] 清单不可读：{exc}", file=sys.stderr)
        return 2

    missing, mismatch = [], []
    for rel, meta in (base.get("files") or {}).items():
        full = os.path.join(root, rel.replace("/", os.sep))
        if not os.path.isfile(full):
            missing.append(f"{rel}（{meta.get('bytes', 0)} B）")
            continue
        if os.path.getsize(full) != meta.get("bytes"):
            mismatch.append(f"{rel}（大小 {os.path.getsize(full)} ≠ {meta.get('bytes')}）")
            continue
        if sha256_file(full) != meta.get("sha256"):
            mismatch.append(f"{rel}（sha256 不符）")

    missing_deps, bad_deps = [], []
    rp = base.get("runtimePython") or {}
    # 派生仓库不带便携 runtime：依赖由 开工.py 按 requirements.txt 建 venv 校验，
    # 这里不再按包内 site-packages 判缺失。
    dep_items = (base.get("runtimeDeps") or {}).items() if rp.get("present", True) else []
    for pkg, meta in dep_items:
        dirname = meta.get("dir") or pkg
        full = os.path.join(site_packages(root), dirname)
        if not os.path.isdir(full):
            missing_deps.append(f"{pkg}（{dirname}/）")
            continue
        init = os.path.join(full, "__init__.py")
        want = meta.get("sha256")
        if want and os.path.isfile(init) and sha256_file(init) != want:
            bad_deps.append(f"{pkg}（{dirname}/ 版本不符）")

    if rp.get("present") and not os.path.isfile(os.path.join(root, RUNTIME_DIR,
                                                            os.path.basename(rp["rel"]))):
        missing.append(f"{rp['rel']}（包内便携 Python）")

    total = len(missing) + len(mismatch) + len(missing_deps) + len(bad_deps)
    if not total:
        print(f"[manifest] 完整性通过：{len(base.get('files') or {})} 个关键文件、"
              f"{len(base.get('runtimeDeps') or {})} 个 runtime 依赖全部就位且哈希一致")
        return 0

    print(f"[manifest] 完整性不通过，共 {total} 项问题：", file=sys.stderr)
    if missing:
        print(f"  · 缺失（{len(missing)}）：包被裁剪，需从完整包恢复", file=sys.stderr)
        for item in missing:
            print(f"      - {item}", file=sys.stderr)
    if mismatch:
        print(f"  · 不符（{len(mismatch)}）：文件被改动或损坏", file=sys.stderr)
        for item in mismatch:
            print(f"      - {item}", file=sys.stderr)
    if missing_deps:
        print(f"  · 缺 runtime 依赖（{len(missing_deps)}）：便携环境装不全", file=sys.stderr)
        for item in missing_deps:
            print(f"      - {item}", file=sys.stderr)
    if bad_deps:
        print(f"  · runtime 依赖版本不符（{len(bad_deps)}）", file=sys.stderr)
        for item in bad_deps:
            print(f"      - {item}", file=sys.stderr)
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="包完整性清单：生成 / 校验")
    ap.add_argument("mode", choices=("generate", "verify"))
    ap.add_argument("--root", default=package_root(), help="包根目录")
    ap.add_argument("--out", default="PACKAGE_MANIFEST.json", help="清单文件名（相对包根）")
    ap.add_argument("--manifest", default="PACKAGE_MANIFEST.json", help="清单文件名（相对包根）")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if args.mode == "generate":
        return cmd_generate(args)
    return cmd_verify(args)


if __name__ == "__main__":
    raise SystemExit(main())
