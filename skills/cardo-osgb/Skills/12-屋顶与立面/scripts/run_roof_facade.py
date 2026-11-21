#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""12 · 屋顶与立面 · run_roof_facade：按序跑 roof.segment → facade.normal。

用 `sys.executable` 起子进程（不换解释器、不装包、不改环境），
参数原样透传 `--workspace` / `--force`；`--only` 只跑一个算法。

用法（工作区根）：
    python <技能目录>/Skills/12-屋顶与立面/scripts/run_roof_facade.py --workspace .
    python <技能目录>/Skills/12-屋顶与立面/scripts/run_roof_facade.py --workspace . --only roof --force
退出码：全部子算法 exit 0 → 0；任一非 0 → 1。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))

STEPS = [
    ("roof", "roof_segment.py"),
    ("facade", "facade_normal.py"),
]
ALIAS = {"roof-segment": "roof", "facade-normal": "facade", "all": "all"}


def parse_args():
    ap = argparse.ArgumentParser(description="12 · 屋顶与立面 · 一键跑 roof.segment / facade.normal")
    ap.add_argument("--workspace", default=".", help="工作区根（默认当前目录）")
    ap.add_argument("--only", default="all", help="只跑一个：roof | facade | all")
    ap.add_argument("--force", action="store_true", help="覆盖已有产物（透传给子脚本）")
    return ap.parse_args()


def resolve_skills_root(workspace: str) -> str | None:
    """技能根在哪：优先 CARDO_SKILLS_ROOT（技能包在工作区**之外**的交付包模式），
    其次 <工作区>/Skills（技能包内嵌模式）。

    交付包里 CardoOSGB/ 与工作区是**平级**的，工作区里根本没有 Skills/——
    所以判据不能是「工作区里有没有 Skills/」。
    """
    env = os.environ.get("CARDO_SKILLS_ROOT")
    if env and os.path.isdir(env):
        return os.path.abspath(env)
    local = os.path.join(os.path.abspath(workspace), "Skills")
    return os.path.abspath(local) if os.path.isdir(local) else None


def main() -> int:
    args = parse_args()
    workspace = os.path.abspath(args.workspace)
    skills_root = resolve_skills_root(workspace)
    if skills_root is None:
        print(f"[12] 找不到技能根（既无 CARDO_SKILLS_ROOT，工作区里也没有 Skills/）："
              f"{args.workspace}", file=sys.stderr)
        return 2
    only = ALIAS.get(args.only, args.only)
    if only != "all" and only not in {k for k, _ in STEPS}:
        print(f"[12] --only 只支持 roof/facade/all，收到：{args.only}", file=sys.stderr)
        return 2

    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", CARDO_SKILLS_ROOT=skills_root)
    codes = {}
    for key, script in STEPS:
        if only != "all" and only != key:
            continue
        cmd = [sys.executable, os.path.join(HERE, script), "--workspace", workspace]
        if args.force:
            cmd.append("--force")
        print(f"[12] 运行 {key}：{script}")
        codes[key] = subprocess.run(cmd, cwd=workspace, env=env).returncode

    print("[12] 退出码：" + ", ".join(f"{k}={v}" for k, v in codes.items()))
    return 0 if all(v == 0 for v in codes.values()) else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
