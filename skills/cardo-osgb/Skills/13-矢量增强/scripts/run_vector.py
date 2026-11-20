#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""13 · 矢量增强 · 跑批入口：按序执行 vector.polygonize → vector.gpkg。

两个算法相互独立（gpkg 只读 10 的 GeoJSON，不依赖 polygonize），但按序跑便于
一次产出全部 13 的成果；任一非 0 即整体失败（返回 1）。

用法（工作区根）：
    python Skills/13-矢量增强/scripts/run_vector.py --workspace .
    python Skills/13-矢量增强/scripts/run_vector.py --workspace . --only vector.polygonize
    python Skills/13-矢量增强/scripts/run_vector.py --workspace . --only vector-gpkg --force

退出码：全部成功 0；否则 1（子脚本各自的 0/2/3/4 会原样打印）。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

sys.dont_write_bytecode = True

STEPS = [
    ("vector.polygonize", "vector_polygonize.py"),
    ("vector.gpkg", "vector_gpkg.py"),
]
ALIASES = {
    "vector.polygonize": "vector.polygonize", "vector-polygonize": "vector.polygonize",
    "polygonize": "vector.polygonize",
    "vector.gpkg": "vector.gpkg", "vector-gpkg": "vector.gpkg", "gpkg": "vector.gpkg",
}


def parse_only(value):
    if not value:
        return [algo for algo, _ in STEPS]
    wanted = []
    for raw in value.split(","):
        token = raw.strip().lower()
        if not token:
            continue
        algo = ALIASES.get(token)
        if algo is None:
            raise SystemExit(f"[13:run] 未知算法：{raw}（可用 vector.polygonize / vector.gpkg）")
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
    ap = argparse.ArgumentParser(description="13 · 矢量增强跑批（polygonize + gpkg）")
    ap.add_argument("--workspace", default=".", help="工作区根（默认当前目录）")
    ap.add_argument("--only", default="", help="只跑指定算法，逗号分隔（vector.polygonize / vector.gpkg）")
    ap.add_argument("--force", action="store_true", help="覆盖已有产物")
    args = ap.parse_args()

    workspace = os.path.abspath(args.workspace)
    skills_root = resolve_skills_root(workspace)
    if skills_root is None:
        print(f"[13:run] 找不到技能根（既无 CARDO_SKILLS_ROOT，工作区里也没有 Skills/）："
              f"{args.workspace}", file=sys.stderr)
        return 1

    scripts_dir = os.path.dirname(os.path.abspath(__file__))
    selected = parse_only(args.only)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", CARDO_SKILLS_ROOT=skills_root)
    failed = []
    for algo, filename in STEPS:
        if algo not in selected:
            continue
        cmd = [sys.executable, os.path.join(scripts_dir, filename), "--workspace", workspace]
        if args.force:
            cmd.append("--force")
        print(f"[13:run] >>> {algo}：{os.path.join('Skills', '13-矢量增强', 'scripts', filename)}")
        proc = subprocess.run(cmd, env=env, cwd=workspace,
                              capture_output=True, text=True, encoding="utf-8", errors="replace")
        if proc.stdout:
            sys.stdout.write(proc.stdout)
        if proc.stderr:
            sys.stderr.write(proc.stderr)
        print(f"[13:run] <<< {algo} exit={proc.returncode}")
        if proc.returncode != 0:
            failed.append((algo, proc.returncode))

    if failed:
        print("[13:run] 失败：" + "；".join(f"{a}(exit {c})" for a, c in failed), file=sys.stderr)
        return 1
    print("[13:run] 全部成功")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
