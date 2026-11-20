#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""02 · 外部工具探测：定位工具目录 → 校验 sha256/清单指纹/版本 → 输出 工具探测.json。

探测顺序（写死，不静默降级）：
  1) --tools-dir 显式覆盖
  2) 本技能自带 tools/（Skills/02-工具探测/tools）
  3) 环境变量 CARDO_OSGB_TOOLS
  4) 报 TOOL_MISSING（exit 2）

校验复用同目录 verify_tools.py（exe sha256 + 178 文件清单指纹 + `-V` + 可选校验器版本）。
登记字段（来源/版本/许可证/分发范围/调用方式）来自 规则/外部工具配置.yaml。

退出码：0 可用；2 工具缺失；3 版本/指纹不符；4 参数错误。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.dont_write_bytecode = True  # 不往技能目录写 __pycache__

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
LOCAL_TOOLS = os.path.join(SKILL_DIR, "tools")
CONFIG_REL = os.path.join("规则", "外部工具配置.yaml")
OUT_DEFAULT = os.path.join("过程文件", "02-工具探测", "工具探测.json")
SCHEMA = "cardo/lowair-tools/v1"

# 包内便携 runtime 用 python313._pth 锁死 sys.path（脚本目录永不入 path），
# 这里显式补上，否则同目录 import 一定失败。
if HERE not in sys.path:
    sys.path.insert(0, HERE)

try:
    import _yaml_lite  # noqa: E402
    import verify_tools  # noqa: E402
except ImportError as exc:  # pragma: no cover
    print(f"[02] 缺少同目录依赖（_yaml_lite.py / verify_tools.py）：{exc}", file=sys.stderr)
    raise SystemExit(4)


def load_config() -> dict:
    path = os.path.join(SKILL_DIR, CONFIG_REL)
    if not os.path.isfile(path):
        return {}
    try:
        return _yaml_lite.load_file(path)
    except Exception as exc:  # 配置坏 = 参数错误，不能静默
        print(f"[02] 工具配置无法解析：{exc}", file=sys.stderr)
        raise SystemExit(4)


def resolve_tools_dir(cli_value: str | None) -> tuple[str | None, str]:
    if cli_value:
        return os.path.abspath(cli_value), "--tools-dir"
    if os.path.isdir(LOCAL_TOOLS):
        return LOCAL_TOOLS, "Skills/02-工具探测/tools"
    env = os.environ.get("CARDO_OSGB_TOOLS")
    if env and os.path.isdir(env):
        return os.path.abspath(env), "env:CARDO_OSGB_TOOLS"
    return None, "missing"


def anon(text: str, tools_dir: str) -> str:
    if not isinstance(text, str) or not text:
        return text
    for raw in (tools_dir, tools_dir.replace("\\", "/")):
        text = text.replace(raw, "<tools-dir>")
    return text


def gap(code: str, what: str, why: str, need: str, owner: str = "user") -> dict:
    return {"code": code, "what": what, "why": why, "need": need, "owner": owner}


def probe(tools_dir: str | None) -> dict:
    cfg = load_config()
    tools_cfg = {item["name"]: item for item in (cfg.get("tools") or []) if item.get("name")}
    resolved, source = resolve_tools_dir(tools_dir)
    if not resolved:
        return {"ok": False, "reason": "TOOL_MISSING", "toolsDir": None, "toolsDirSource": source,
                "tools": [], "licenseVerified": False,
                "gaps": [gap("TOOL_MISSING", "未找到外部工具目录",
                             "解析依赖 3dtile.exe；本技能不自研 OSGB 解析器",
                             "把 fanvanzh/3dtiles v0.4 放到 Skills/02-工具探测/tools/，"
                             "或用 --tools-dir / 环境变量 CARDO_OSGB_TOOLS 指定", "user")]}

    report = verify_tools.check(resolved)
    checks = {item.get("name"): item for item in report.get("checks", [])}
    exe_check = checks.get("exe sha256", {})
    man_check = checks.get("安装目录清单指纹", {})
    banner_check = checks.get("-V 字面量", {})
    validator_check = checks.get("3d-tiles-tools version", {})
    reg = tools_cfg.get("3dtile", {})
    exe_rel = str(reg.get("executable") or "fanvanzh-3dtiles/v0.4/3dtile.exe")
    exe_present = os.path.isfile(os.path.join(resolved, *exe_rel.split("/")))
    exe_ok = exe_present and all(item.get("ok") for item in (exe_check, man_check, banner_check))

    tools = [{
        "name": "3dtile", "role": "converter",
        "version": reg.get("version") or "v0.4",
        "versionBanner": banner_check.get("actual"),
        "path": "<tools-dir>/" + (reg.get("executable") or "fanvanzh-3dtiles/v0.4/3dtile.exe"),
        "sha256": exe_check.get("actual"),
        "sha256Expected": reg.get("sha256"),
        "manifestFiles": man_check.get("files"),
        "manifestSha256": man_check.get("actual"),
        "manifestSha256Expected": (reg.get("manifest") or {}).get("sha256"),
        "license": reg.get("license") or "Apache-2.0",
        "licenseFile": reg.get("licenseFile"),
        "distribution": reg.get("distribution") or "internal-only",
        "invocation": reg.get("invocation") or {},
        "ok": bool(exe_ok), "optional": False,
    }]
    gaps: list[dict] = []
    if not exe_present:
        gaps.append(gap("TOOL_MISSING",
                        f"工具目录内缺 3dtile.exe（{reg.get('executable') or 'fanvanzh-3dtiles/v0.4/3dtile.exe'}）",
                        "解析依赖转换器；缺可执行无法转换，且本链不自研 OSGB 解析器",
                        "把登记版本放回 tools/，或用 fetch_tools.py 按清单重建", "user"))
    elif not exe_ok:
        gaps.append(gap("TOOL_VERSION_MISMATCH",
                        "3dtile.exe / 安装目录 / `-V` 与白名单不符"
                        f"（exe={'命中' if exe_check.get('ok') else '不符'}，"
                        f"清单={'命中' if man_check.get('ok') else '不符'}，"
                        f"-V={'命中' if banner_check.get('ok') else '不符'}）",
                        "换/改任意 exe 或 DLL 都会改变转换行为，白名单是唯一可信判据",
                        "恢复登记版本（或按新版本重录白名单并全链回归）", "user"))

    validator_cfg = tools_cfg.get("3d-tiles-tools", {})
    validator_installed = bool(report.get("toolsDir") and os.path.isfile(
        os.path.join(resolved, "3d-tiles-tools", "node_modules", "3d-tiles-tools", "package.json")))
    validator = {
        "installed": validator_installed, "optional": True,
        "version": validator_check.get("actual"), "versionExpected": validator_check.get("expected"),
        "ok": None if not validator_installed else bool(validator_check.get("ok")),
        "distribution": validator_cfg.get("distribution") or "local-only",
        "fetch": validator_cfg.get("fetch") or "fetch_tools.py",
        "note": "可选：未安装时 05 步的整树独立校验降级为自校验（H-06=warn），不影响转换",
    }
    if validator_installed and not validator_check.get("ok"):
        gaps.append(gap("TOOL_VERSION_MISMATCH",
                        f"3d-tiles-tools 版本 {validator_check.get('actual')} 与白名单 "
                        f"{validator_check.get('expected')} 不符",
                        "校验器版本不同会改变判据口径", "安装登记版本，或卸载后接受自校验降级", "user"))

    license_file = os.path.join(resolved, "licenses",
                                os.path.basename(str(reg.get("licenseFile") or "licenses")))
    license_ok = os.path.isfile(license_file)
    if not license_ok:
        gaps.append(gap("LICENSE_UNVERIFIED",
                        "工具许可原文缺失（LICENSE 副本不在 tools/licenses/）",
                        "许可未核验时不得进入对外交付说明",
                        "把上游 LICENSE 原文放回 tools/licenses/", "user"))

    ok = bool(exe_ok)
    reason = "" if ok else ("TOOL_MISSING" if not exe_present else "TOOL_VERSION_MISMATCH")
    return {
        "schema": SCHEMA, "schemaVersion": 1,
        "toolsDir": "<tools-dir>", "toolsDirSource": source,
        "probeOrder": ["--tools-dir", "Skills/02-工具探测/tools", "env:CARDO_OSGB_TOOLS"],
        "tools": tools, "validator": validator,
        "licenseVerified": license_ok,
        "ok": ok, "reason": reason,
        "checks": [{k: anon(str(v), resolved) if isinstance(v, str) else v for k, v in item.items()}
                   for item in report.get("checks", [])],
        "gaps": gaps,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="02 · 外部工具探测（不解析模型）")
    ap.add_argument("--tools-dir", default=None, help="工具根目录（优先于自带 tools/）")
    ap.add_argument("--out", default=OUT_DEFAULT, help="输出 工具探测.json")
    ap.add_argument("--json", action="store_true", help="只打印 JSON，不落盘")
    args = ap.parse_args()

    doc = probe(args.tools_dir)
    if args.json:
        print(json.dumps(doc, ensure_ascii=False, indent=2))
    else:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        for tool in doc["tools"]:
            print(f"[{'OK' if tool['ok'] else 'FAIL'}] {tool['name']} {tool['version']} "
                  f"({tool['role']}, {tool['license']}, {tool['distribution']}) {tool['path']}")
        v = doc["validator"]
        print(f"[{'OK' if v['ok'] else ('SKIP' if not v['installed'] else 'FAIL')}] "
              f"3d-tiles-tools {v['version'] or '未安装'}（可选校验器）")
        for item in doc["gaps"]:
            print(f"[GAP] {item['code']}: {item['what']} → {item['need']}")
        print(f"[02] {'可用' if doc['ok'] else '不可用：' + doc['reason']}；输出 {args.out}")
    if doc["ok"]:
        return 0
    if doc["reason"] == "TOOL_MISSING":
        # 不阻塞：本步的职责是「探测并如实登记」，装没装由 03 决定跑不跑。
        # 在这里 exit 2 会让整条链停在 02，用户看不到 03 的缺件诊断。
        print("[02] 包内缺转换器（安装不完整）：03 会打印缺件诊断并跳过转换。")
        return 0
    return 3


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
