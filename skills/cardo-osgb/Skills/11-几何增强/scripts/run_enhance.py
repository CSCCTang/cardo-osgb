#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""11 · 几何增强 · 跑批入口：按序执行本技能的 4 个子算法。

顺序：pc.ground-filter → pc.classify → tree.single → mesh.repair
（另外 4 个算法已移交独立技能：12 屋顶与立面、13 矢量增强，各自的 run_*.py 负责）

用 `sys.executable` 起独立子进程（不换解释器、不装包、不改环境），cwd 固定为工作区根；
`--workspace` / `--force` 原样透传给子脚本；`--only` 只跑指定算法（逗号分隔）。

用法（工作区根）：
    python Skills/11-几何增强/scripts/run_enhance.py --workspace .
    python Skills/11-几何增强/scripts/run_enhance.py --workspace . --only pc.classify --force
    python Skills/11-几何增强/scripts/run_enhance.py --workspace . --skip-missing

退出码：全部子算法 0 → 0；任一非 0 → 1；参数/工作区不合法 → 2。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))

STEPS = [
    ("pc.ground-filter", "pc_ground_filter.py"),
    ("pc.classify", "pc_classify.py"),
    ("tree.single", "tree_single.py"),
    ("mesh.repair", "mesh_repair.py"),
]
ALIASES = {
    "pc.ground-filter": "pc.ground-filter", "pc-ground-filter": "pc.ground-filter",
    "ground": "pc.ground-filter", "ground-filter": "pc.ground-filter",
    "pc.classify": "pc.classify", "pc-classify": "pc.classify", "classify": "pc.classify",
    "tree.single": "tree.single", "tree-single": "tree.single", "tree": "tree.single",
    "mesh.repair": "mesh.repair", "mesh-repair": "mesh.repair", "mesh": "mesh.repair",
}


def parse_only(value) -> list:
    if not value:
        return [algo for algo, _ in STEPS]
    wanted = []
    for raw in value.split(","):
        token = raw.strip().lower()
        if not token:
            continue
        algo = ALIASES.get(token)
        if algo is None:
            raise SystemExit(f"[11:run] 未知算法：{raw}（可用 "
                             "pc.ground-filter / pc.classify / tree.single / mesh.repair）")
        if algo not in wanted:
            wanted.append(algo)
    return wanted or [algo for algo, _ in STEPS]


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
    ap = argparse.ArgumentParser(description="11 · 几何增强跑批（ground-filter / classify / tree / mesh-repair）")
    ap.add_argument("--workspace", default=".", help="工作区根（默认当前目录）")
    ap.add_argument("--only", default="", help="只跑指定算法，逗号分隔")
    ap.add_argument("--force", action="store_true", help="覆盖已有产物（透传给子脚本）")
    ap.add_argument("--skip-missing", action="store_true", help="脚本缺失时跳过，不算失败")
    args = ap.parse_args()

    workspace = os.path.abspath(args.workspace)
    skills_root = resolve_skills_root(workspace)
    if skills_root is None:
        print(f"[11:run] 找不到技能根（既无 CARDO_SKILLS_ROOT，工作区里也没有 Skills/）："
              f"{args.workspace}", file=sys.stderr)
        return 2

    selected = parse_only(args.only)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", CARDO_SKILLS_ROOT=skills_root)
    failed, ran = [], 0
    for algo, filename in STEPS:
        if algo not in selected:
            continue
        script = os.path.join(HERE, filename)
        if not os.path.isfile(script):
            if args.skip_missing:
                print(f"[11:run] 跳过 {algo}：脚本缺失 {filename}")
                continue
            print(f"[11:run] 脚本缺失：{filename}", file=sys.stderr)
            failed.append((algo, 2))
            continue
        cmd = [sys.executable, script, "--workspace", workspace]
        if args.force:
            cmd.append("--force")
        print(f"[11:run] >>> {algo}：{os.path.join('Skills', '11-几何增强', 'scripts', filename)}")
        proc = subprocess.run(cmd, env=env, cwd=workspace, capture_output=True,
                              text=True, encoding="utf-8", errors="replace")
        if proc.stdout:
            sys.stdout.write(proc.stdout)
        if proc.stderr:
            sys.stderr.write(proc.stderr)
        print(f"[11:run] <<< {algo} exit={proc.returncode}")
        ran += 1
        if proc.returncode != 0:
            failed.append((algo, proc.returncode))

    if failed:
        print("[11:run] 失败：" + "；".join(f"{a}(exit {c})" for a, c in failed), file=sys.stderr)
        return 1
    if not ran:
        print("[11:run] 没有可执行的算法（全部缺失且给了 --skip-missing）", file=sys.stderr)
        return 2
    print("[11:run] 全部成功")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
