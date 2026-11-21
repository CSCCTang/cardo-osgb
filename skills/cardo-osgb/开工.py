#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cardo 低空解析（OSGB → 3D Tiles）· 一条命令开工。

依次做四件事：
  依赖自检（缺就在包根建 .venv，按 阿里云 → 清华 → USTC → 官方源 回退安装；首次较慢）
  → 建目录并核对自带转换工具 → 校验模型包（唯一 zip / 布局 / SRS=EPSG:4544）与参数 → 调 01→08 流水线。

缺料不黑箱：会说明（缺什么 / 放到哪 / 怎么准备）、留 待补料.json，并且只停这一次；
不做轮询、不做后台守候、不自动扫描目录变化，等用户回一句「放好了」再一口气跑完。

退出码：0 全过；3 缺料或料不对（可指导修正）；1 流水线硬失败；2 环境或结构问题。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import traceback

# 运行期不写 __pycache__：Skills/ 是只读交付物，写进 pyc 会污染交付库
# （实测交付库里混进过 19 个 pyc）。这里在导入任何本地模块之前就关掉。
sys.dont_write_bytecode = True

import zipfile
from datetime import datetime
from pathlib import Path

SKILL = Path(__file__).resolve().parent      # 技能包：只读，运行期不写入
RUNTIME_PY = SKILL / "runtime" / "python" / ("python.exe" if os.name == "nt" else "python")
ROOT = SKILL.parent
CACHE_BASE = Path(os.environ.get("CARDO_CACHE_HOME") or os.environ.get("LOCALAPPDATA") or str(Path.home()))
def venv_dir() -> Path:
    """派生仓库：虚拟环境放用户缓存，不写安装目录。"""
    return CACHE_BASE / "cardo" / "venv-osgb"
                          # 工作区：技能包所在的上一级目录
# 工作区是否已成功解析（resolve_workspace 会抛 SystemExit；未解析时不能拿默认 ROOT 去写看板状态）
WORKSPACE_RESOLVED = False

# 看板状态注入标记（开工.py 每次运行刷新这一段；看板读不到工作区状态）
STATUS_BEGIN = "<!-- CARDO-STATUS:BEGIN -->"
STATUS_END = "<!-- CARDO-STATUS:END -->"

TITLE = "Cardo 低空解析"
PY_MIN = (3, 9)

DIRS = ["输入文件", "输入文件/01-模型包", "过程文件", "成果文件", "记忆与规则"]
ASSETS = []
INPUT_TEMPLATES = []
TOOLS = [
    ("3dtile.exe（OSGB→3D Tiles，随包自带）",
     "Skills/02-工具探测/tools/fanvanzh-3dtiles/v0.4/3dtile.exe"),
]
# 两件资产（浏览引擎 + 转换器）都随包分发，装完即用；缺了就是安装包不完整，不是「让你去下载」。
VIEWER_HINT = ("包内缺模型浏览引擎 assets/viewer（安装包不完整）：请重新安装本插件"
               "（Release tgz 或 github:CSCCTang/cardo-osgb#v0.1.4）。")


def viewer_ready() -> bool:
    """浏览引擎是否就位（看两个必需件，不看到目录空壳）。"""
    viewer = SKILL / "assets" / "viewer"
    return ((viewer / "tool" / "caddy.exe").is_file()
            and (viewer / "viewer" / "3d" / "index.html").is_file())


IMPORTS = ["docx", "openpyxl", "numpy", "scipy", "PIL", "trimesh", "skimage", "cv2", "fitz"]

PARAMS = "工作区参数.json"
SCHEMA = "cardo/lowair-workspace/v1"
DEFAULTS = {"projectName": "", "modelSourceName": "", "sceneId": "model-01"}
# 骨架里的键说明：跟着工作区参数.json 一起落盘，用户照着填即可；键名以 _ 开头，不参与流程。
PARAMS_HELP = {
    "projectName": "项目名（业主口径）：报告抬头、模型浏览页标题、project.json、指标卡、附件清单都用它；"
                   "留空则取本轮模型输入名",
    "modelSourceName": "模型来源行，例如「无人机航测模型」；写进 project.json 与成品说明；"
                       "留空则取本轮模型输入名",
    "sceneId": "项目包目录标识，纯 ASCII 字母数字 . _ -，默认 model-01",
}
ALLOWED_SRS = "EPSG:4544"

PIPELINE = "Skills/00-Cardo低空解析总控/scripts/pipeline.py"
DELIVERABLES = [
    "成果文件/成品版/模型体检报告/模型体检报告_成品版.docx",
    "成果文件/成品版/模型体检报告/模型体检报告_成品版.pdf",
    "成果文件/成品版/现状指标/现状指标_成品版.xlsx",
    "成果文件/成品版/指标说明/指标说明_成品版.docx",
    "成果文件/成品版/指标说明/指标说明_成品版.pdf",
    "成果文件/内部件/模型体检报告/模型体检报告_溯源版.docx",
    "成果文件/内部件/现状指标/现状指标_溯源版.xlsx",
    "成果文件/内部件/现状指标/现状指标依据对照表.xlsx",
    "成果文件/内部件/指标说明/指标说明_溯源版.docx",
    "成果文件/成品版/模型浏览/Start.bat",
]
GUIDES = {
    "01": "看 过程文件/01-模型包接入/解包日志.md 的「坐标系（SRS）」「目录布局」两节；SRS 不合规或布局缺根瓦片，需先重投影再导出合规包（不替代、不伪造）。",
    "02": "工具缺失或被替换时不要「换个版本试试」：把 Skills/02-工具探测/tools 恢复为随包原件后再跑。",
    "05": "看 过程文件/05-模型体检/体检.json 的 verdict 与 gaps：H 项 fail 属模型自身缺口（如纹理缺失），成果会据实写明，不补造。",
    "08": "看 过程文件/08-验证与成稿/验证报告.json；有 fail 时按报告定位到具体指标或边界违规文件。",
}

# ---------------------------------------------------------------- 收料引导
INTAKE_ITEMS = [{'key': 'model', 'name': 'OSGB 模型包 zip（单根目录 + metadata.xml）', 'required': True, 'dst': '输入文件/01-模型包/', 'patterns': ['*.zip'], 'how': '整个 OSGB 目录打成 zip，解压后只有一个根目录且含 metadata.xml；SRS 只收 EPSG:4544', 'why': '低空解析的唯一输入；多 zip 或坐标系不合规在预检直接 exit 3'}, {'key': 'others', 'name': '其他材料（有什么放什么）', 'required': False, 'catch_all': True, 'dst': '输入文件/（任意层级）', 'patterns': ['*'], 'how': '统计年鉴、上级规划、新闻剪报、访谈录音转写、现场台账、CardoStatus 跑出来的《现状分析报告》等，凡是你觉得用得上的都能放，文件夹和 zip 也行', 'why': '留档备查；01–08 的指标只来自模型本体，不从这些材料取数'}]


def intake_scan():
    """逐项探测落位情况；返回 [(item, [命中文件])]。

    catch_all 项（「其他材料」）只统计没被任何具体项认领的文件，避免把已知材料重复计数。
    """
    import fnmatch

    base = ROOT / "输入文件"
    pool = [p for p in base.rglob("*")
            if p.is_file() and not p.name.startswith("~$") and "空白" not in p.name] if base.is_dir() else []
    claimed, rows = set(), []

    for item in INTAKE_ITEMS:
        if item.get("catch_all"):
            continue
        hits = [p for p in pool if any(fnmatch.fnmatch(p.name.lower(), pat.lower())
                                       for pat in item["patterns"])]
        claimed.update(hits)
        rows.append((item, sorted(set(hits))))

    for item in INTAKE_ITEMS:
        if item.get("catch_all"):
            rows.append((item, [p for p in pool if p not in claimed]))

    order = {id(item): index for index, item in enumerate(INTAKE_ITEMS)}
    return sorted(rows, key=lambda pair: order[id(pair[0])])


def write_intake_list(rows) -> Path:
    """生成人读的《放料清单.md》：要什么、放哪（绝对路径）、怎么命名、现在到没到。"""
    target = ROOT / "放料清单.md"
    ready = sum(1 for item, hits in rows if hits)
    lines = ["# 放料清单 · {}".format(SKILL.name), "",
             "> 本清单由 开工.py 自动生成，每次运行都会刷新。工作区：`{}`".format(norm(ROOT)),
             "> 放好文件后不用改本文件，重跑一次开工.py 即可。", "",
             "## 1. 现在到哪一步", "",
             "| 项 | 是否必需 | 状态 | 落位（绝对路径） |", "|---|---|---|---|"]
    for item, hits in rows:
        if hits:
            where = "、".join(norm(path.relative_to(ROOT)) if ROOT in path.parents else norm(path)
                              for path in hits[:2])
            status = "已到位（{}）".format(where)
        else:
            status = "**缺**" if item["required"] else "未提供（可选）"
        lines.append("| {} | {} | {} | `{}` |".format(
            item["name"], "必需" if item["required"] else "可选", status,
            norm(ROOT / item["dst"])))
    lines += ["", "已到位 {} / {} 项。".format(ready, len(rows)), "",
              "## 2. 每一项怎么准备", ""]
    for index, item in enumerate(INTAKE_ITEMS, start=1):
        lines += ["### {}. {}".format(index, item["name"]),
                  "- 放到：`{}`".format(norm(ROOT / item["dst"])),
                  "- 怎么来：{}".format(item["how"]),
                  "- 干什么用：{}".format(item["why"]),
                  "- 缺失后果：{}".format("不开工，提示补料" if item["required"]
                                          else "照常开工，产物给初步判定（推断＋置信度），缺口进数据缺口清单"), ""]
    lines += ["## 3. 下一步", "",
              "```text",
              "python <技能目录>/scripts/cardo.py            # 料放好后重跑，一口气跑完".format(SKILL.name),
              "```",
              "",
              "缺料时只停这一次：按上面「放到」的绝对路径放好，回一句「放好了」再重跑即可；",
              "不做轮询、不做后台守候、不自动扫描目录变化。", ""]
    target.write_text("\n".join(lines), encoding="utf-8")
    return target


MIRRORS = [
    ("aliyun", "https://mirrors.aliyun.com/pypi/simple/", "mirrors.aliyun.com"),
    ("tsinghua", "https://pypi.tuna.tsinghua.edu.cn/simple", "pypi.tuna.tsinghua.edu.cn"),
    ("ustc", "https://mirrors.ustc.edu.cn/pypi/simple", "mirrors.ustc.edu.cn"),
    ("pypi", "https://pypi.org/simple", "pypi.org"),
]



# ---------------------------------------------------------------- 小工具

def say(msg: str = "") -> None:
    print(msg, flush=True)


def norm(p: Path) -> str:
    return str(p).replace("\\", "/")


def read_json(path: Path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def missing_mods(py: str, mods: list[str]) -> list[str]:
    code = ("import importlib.util,json,sys\n"
            "mods=json.loads(sys.argv[1])\n"
            "print(json.dumps([m for m in mods if importlib.util.find_spec(m) is None]))")
    try:
        r = subprocess.run([py, "-c", code, json.dumps(mods)], capture_output=True,
                           text=True, timeout=90)
        return json.loads(r.stdout.strip().splitlines()[-1]) if r.returncode == 0 else list(mods)
    except Exception:
        return list(mods)


def venv_python() -> Path:
    name = "python.exe" if os.name == "nt" else "python"
    sub = "Scripts" if os.name == "nt" else "bin"
    return venv_dir() / sub / name


def can_make_venv(exe: str) -> bool:
    """这个解释器能不能建 venv。

    embeddable 版 Python（包内便携 runtime 就是）**不带 venv 模块**，
    `python -m venv` 会直接报 No module named 'venv'。先探一下，
    免得走到建 venv 才发现根本建不了。
    """
    try:
        r = subprocess.run([exe, "-c", "import venv"], capture_output=True,
                           text=True, timeout=30)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def system_pythons() -> list[str]:
    """探测机器上的**系统** Python（不含包内便携版）。

    为什么需要：便携 runtime（embeddable 版）**没有 venv 模块**。当用户是用
    `runtime/python/python.exe 开工.py` 调起时，`sys.executable` 就是便携版本身——
    它缺一个依赖就会走到「建 .cardo/venv-osgb」分支，而 `python -m venv` 直接失败
    （No module named 'venv'），**无解**。

    所以在决定建 venv 之前，先把系统 Python 找出来：命中且依赖齐全就直接用它继续，
    根本不用建 venv。

    探测顺序（Windows）：
      1. `py -0p` 启动器列出的所有版本
      2. PATH 里的 python / python3 / py
      3. 常见安装路径（Program Files / Local Programs / Python3xx）
    非 Windows：PATH + python3 / python3.9+。
    """
    found, seen = [], set()

    def add(exe: str | None) -> None:
        if not exe:
            return
        exe = str(exe).strip()
        if not exe or exe in seen:
            return
        # 排除包内便携版（它不是系统 Python，建不了 venv 也没意义）
        try:
            if Path(exe).resolve() == RUNTIME_PY.resolve():
                return
        except (OSError, ValueError):
            pass
        seen.add(exe)
        found.append(exe)

    if os.name == "nt":
        # 1) py 启动器
        try:
            r = subprocess.run(["py", "-0p"], capture_output=True, text=True, timeout=20)
            if r.returncode == 0:
                for line in r.stdout.splitlines():
                    line = line.strip()
                    # 形如  -V:3.13        C:\Python313\python.exe
                    if "python.exe" in line.lower():
                        add(line.split()[-1])
        except (OSError, subprocess.SubprocessError):
            pass
        # 2) PATH
        for name in ("python", "python3", "py"):
            try:
                r = subprocess.run(["where", name], capture_output=True, text=True, timeout=15)
                if r.returncode == 0:
                    for line in r.stdout.splitlines():
                        add(line.strip())
            except (OSError, subprocess.SubprocessError):
                pass
        # 3) 常见安装路径
        roots = [os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                 os.environ.get("LOCALAPPDATA", ""),
                 os.environ.get("PYTHONHOME", "")]
        for base in roots:
            if not base:
                continue
            for sub in ("Python313", "Python312", "Python311", "Python310", "Python39",
                        "Python3.13", "Python3.12", "Python3.11", "Python3.10", "Python3.9"):
                add(os.path.join(base, sub, "python.exe"))
            add(os.path.join(base, "Python", "python.exe"))
            add(os.path.join(base, "Programs", "Python", "Python313", "python.exe"))
    else:
        for name in ("python3", "python"):
            add(shutil.which(name))

    # 存在的、能跑起来的才算
    alive = []
    for exe in found:
        # py.exe 是「启动器」不是解释器，-c 会被它转发，误判为可用——直接跳过
        if Path(exe).stem.lower() in ("py", "pyw"):
            continue
        try:
            r = subprocess.run([exe, "-c", "import sys;print(sys.version_info[:2])"],
                               capture_output=True, text=True, timeout=20)
            if r.returncode == 0:
                major, minor = eval(r.stdout.strip().splitlines()[-1])  # (3, 13)
                if (major, minor) >= PY_MIN:
                    alive.append(exe)
        except (OSError, subprocess.SubprocessError, ValueError):
            continue
    return alive


def ensure_env(no_install: bool) -> tuple[str | None, bool]:
    # 1) 包内便携环境：拷包即用，不联网、不建 venv
    if RUNTIME_PY.is_file() and not missing_mods(str(RUNTIME_PY), IMPORTS):
        return str(RUNTIME_PY), False
    # 2) 系统 Python（**当前解释器**）已有依赖
    if not missing_mods(sys.executable, IMPORTS):
        return sys.executable, False
    # 3) 机器上别的系统 Python（便携版没 venv 模块，建不了虚拟环境时的活路）
    for exe in system_pythons():
        if not missing_mods(exe, IMPORTS):
            say(f"  用系统 Python：{exe}（依赖齐全）")
            return exe, False
    # 4) 已建好的 venv
    py = venv_python()
    if py.is_file() and not missing_mods(str(py), IMPORTS):
        return str(py), False
    if no_install:
        say(f"  缺依赖：{'、'.join(missing_mods(sys.executable, IMPORTS))}"
            f"（已指定 --no-install，不安装）")
        return None, False
    # 5) 建 venv —— 但只有当**当前解释器**能建 venv 时才值得试
    if not can_make_venv(sys.executable):
        # 便携版没有 venv 模块，且没找到可用的系统 Python：这条路走不通，
        # 必须明确告诉用户怎么办，而不是丢一句"创建 .venv 失败"就退出。
        say("  当前解释器无法创建虚拟环境（缺 venv 模块，通常是包内便携 Python）。")
        candidates = system_pythons()
        if candidates:
            say(f"  机器上有系统 Python，但依赖不全：{'、'.join(candidates)}")
            say(f"  请在其中装齐这 {len(IMPORTS)} 个包后重跑："
                f"pip install -r requirements.txt")
        else:
            say(f"  这台机器上没有找到可用的系统 Python（需要 3.9+）。")
            say(f"  请安装 Python 3.9+ 并执行：pip install -r requirements.txt")
            say("  装好后重跑本命令即可；或把包内 runtime/ 整个目录补齐（便携环境自带全部依赖）。")
        return None, False
    if not py.is_file():
        say("  建虚拟环境 .cardo/venv-osgb …")
        venv_dir().parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run([sys.executable, "-m", "venv", str(venv_dir())],
                           cwd=str(ROOT), capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            say("  创建 .venv 失败：")
            say((r.stderr or r.stdout or "")[-800:])
            return None, False
    if not install(str(py)):
        say("  依赖安装失败。离线环境可手动拷 wheel 后执行：")
        say("  python -m pip install --no-index --find-links <目录> -r requirements.txt")
        return None, False
    left = missing_mods(str(py), IMPORTS)
    if left:
        say(f"  安装后仍缺：{'、'.join(left)}")
        return None, False
    return str(py), True


DELIVERY_MARKER = ".cardo_delivery_marker"


def _is_delivery_root(ws: Path) -> bool:
    """判断工作区是否为「交付库根」：交付库只读出包，不能在里面开工。

    唯一判据：**当前目录本身**存在标记文件 .cardo_delivery_marker。
    为什么不看「根下有 ≥2 个 Cardo* 目录」：用户经常把几个技能包并列放在
    同一个项目目录（如 测试-全流程贯通\）里使用，那是正常用法，不能拦。
    """
    try:
        return (ws / DELIVERY_MARKER).exists()
    except OSError:
        return False


def _has_multi_packages(ws: Path) -> bool:
    """并列多个 Cardo* 目录只作提示，不作拒绝条件。"""
    try:
        pkgs = [d for d in ws.iterdir() if d.is_dir() and d.name.startswith("Cardo")]
        return len(pkgs) >= 2
    except OSError:
        return False


def resolve_workspace(value: str) -> Path:
    """工作区 = 技能包所在的上一级目录（可 --workspace 覆盖）；技能包只读。"""
    ws = Path(value).resolve() if value else SKILL.parent
    if _is_delivery_root(ws):
        say(f"[错误] 这是交付库根目录，不能在这里开工：{ws}")
        say("        交付库是只读的发布产物，开工会在库里生成 过程文件/成果文件。")
        say("        正确做法：把技能包复制到项目目录后再运行，例如")
        say("          <项目目录> 下放 CardoOSGB\\，再在该目录运行开工.py")
        raise SystemExit(2)
    if ws == SKILL or SKILL in ws.parents:
        say(f"[错误] 工作区不能是技能包目录或其子目录：{ws}")
        raise SystemExit(2)
    if not ws.is_dir():
        say(f"[错误] 工作区不存在：{ws}")
        raise SystemExit(2)
    if _has_multi_packages(ws):
        say("[提示] 当前目录并列了多个 Cardo* 技能包；未发现 .cardo_delivery_marker，按普通工作区继续。")
    if len(ws.parts) <= 1:
        # 只有当工作区本身就是盘符根（如 D://）时才拦；判断看自身层级，
        # 不能用 ws.parent.name —— 那样 D://任意文件夹 的父目录也是 D://，会被误拦。
        say("[错误] 技能包不能直接放在盘符根目录；请放进一个项目文件夹后再跑。")
        raise SystemExit(2)
    return ws


# ---------------------------------------------------------------- 环境

def make_dirs() -> list[str]:
    made = []
    for d in DIRS:
        p = ROOT / d
        if not p.is_dir():
            p.mkdir(parents=True, exist_ok=True)
            made.append(d)
    return made


def release_assets() -> list[str]:
    problems = []
    for a in ASSETS:
        src, dst = SKILL / a["src"], ROOT / a["dst"]
        if src.is_file() and not dst.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            say(f"  释放模板：{a['dst']}")
    return problems


def install(py: str) -> bool:
    req = SKILL / "requirements.txt"
    if not req.is_file():
        return True
    for name, url, host in MIRRORS:
        say(f"  安装依赖（{name}）…首次较慢，请等待")
        r = subprocess.run([py, "-m", "pip", "install", "-r", str(req), "-i", url,
                            "--trusted-host", host, "--disable-pip-version-check", "--no-input"],
                           cwd=str(ROOT), capture_output=True, text=True, timeout=1200)
        if r.returncode == 0:
            return True
        say(f"  {name} 失败，换下一个源。")
    return False


# ---------------------------------------------------------------- 参数与输入

def load_params() -> dict:
    data = read_json(ROOT / PARAMS, {}) or {}
    data.setdefault("schema", SCHEMA)
    for k, v in DEFAULTS.items():
        data.setdefault(k, v)
    data.setdefault("_说明", dict(PARAMS_HELP))
    return data


def find_zips() -> list[Path]:
    """找 输入文件/01-模型包/ 下的 zip（递归，与 pipeline.find_input_zip 同口径）。"""
    root = ROOT / "输入文件/01-模型包"
    if not root.is_dir():
        return []
    return sorted([p for p in root.rglob("*.zip")
                   if p.is_file() and not p.name.startswith("~$")])


def write_pending(rows, blockers: list[str], notes: list[str]) -> Path:
    """缺料时落一份机器可读的《待补料.json》：让 AI/人一眼看懂现在缺什么、放哪、怎么重跑。"""
    target = ROOT / "过程文件/00-总控/待补料.json"
    pending = []
    for item, hits in rows:
        if not hits:
            pending.append({
                "项": item["name"],
                "必需": bool(item["required"]),
                "放到": norm(ROOT / item["dst"]),
                "怎么准备": item["how"],
                "为什么需要": item["why"],
            })
    write_json(target, {
        "schema": "cardo/lowair-pending/v1",
        "生成时间": datetime.now().astimezone().isoformat(timespec="seconds"),
        "工作区": norm(ROOT),
        "结论": "缺料，尚未开工" if pending else "无缺料",
        "待补": pending,
        "已到位": [{"项": item["name"], "文件": [norm(h) for h in hits[:3]]}
                   for item, hits in rows if hits],
        "阻塞原因": blockers,
        "说明": notes,
        "补齐后重跑": "python <技能目录>/scripts/cardo.py".format(SKILL.name),
    })
    return target


def check_zip(p: Path) -> tuple[bool, str]:
    """预检模型包：判据与 01 步实跑完全同源（调 01 步的 --check-only）。

    这样"预检说通过"就等于"实跑不会因布局/坐标系被拦"。
    """
    script = SKILL / "Skills/01-模型包接入/scripts/unpack_model.py"
    try:
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1",
                   PYTHONDONTWRITEBYTECODE="1")
        # 包内便携 runtime 的 python313._pth 会锁死 sys.path（脚本目录永不入 path），
        # 显式给 PYTHONPATH 才能让同目录 import 生效。
        env["PYTHONPATH"] = os.pathsep.join(
            [str(script.parent)] + ([os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else []))
        proc = subprocess.run([sys.executable, str(script), "--check-only", "--zip", str(p)],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=300, env=env)
    except Exception as exc:
        return False, "调用 01 步校验失败：{}: {}".format(type(exc).__name__, exc)
    raw = proc.stdout or ""
    start = raw.find("{")
    if start < 0:
        return False, "01 步校验没有返回结果（{}）".format(
            (proc.stderr or "").strip()[-200:] or "无输出")
    try:
        verdict = json.loads(raw[start:])
    except ValueError as exc:
        return False, "解析 01 步校验结果失败：{}".format(exc)
    if verdict.get("ok"):
        srs = verdict.get("srs") or ""
        extra = "，垂直码 {} 会被归一（垂直位移由 04 步补偿）".format(
            verdict["vertical"]) if verdict.get("vertical") else ""
        return True, "布局与坐标系校验通过（SRS={}{}）".format(srs, extra)
    why = verdict.get("why") or "校验未通过"
    fix = verdict.get("fix") or ""
    return False, "{}；怎么改：{}".format(why, fix) if fix else why


def check_package_manifest() -> list[str]:
    """核对包完整性，返回问题清单（空 = 通过）。

    为什么要独立一个模块（package_manifest.py）而不是写在开工.py 里：
    校验逻辑要在**任何 Python 环境下**都能跑——包括便携 runtime 缺依赖时。
    所以这里只用标准库，且**以子进程**方式调用，脚本自己解析包根路径。

    注意：本函数**不 import** package_manifest（那会绑死包根路径解析），
    而是用当前解释器跑它，避免便携 runtime 的 sys.path 隔离问题。
    """
    script = SKILL / "Skills" / "00-Cardo低空解析总控" / "scripts" / "package_manifest.py"
    manifest = SKILL / "PACKAGE_MANIFEST.json"
    if not manifest.is_file():
        return [f"缺包完整性清单 PACKAGE_MANIFEST.json（包根应有；可用 "
                f"package_manifest.py generate 重建）"]
    if not script.is_file():
        return ["缺 package_manifest.py（包被裁剪）"]
    try:
        proc = subprocess.run(
            [sys.executable, str(script), "verify", "--root", str(SKILL)],
            cwd=str(SKILL), capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.SubprocessError) as exc:
        return [f"包完整性校验无法执行：{type(exc).__name__}: {exc}"]
    if proc.returncode == 0:
        return []
    # 把校验脚本的输出压成一行行的问题（去掉前缀与空行）
    problems = []
    for line in (proc.stderr or proc.stdout or "").splitlines():
        text = line.strip()
        if not text or text.startswith("[manifest]"):
            continue
        if text.startswith("·"):
            text = text.lstrip("· ").strip()
        if text.startswith("-"):
            text = text.lstrip("- ").strip()
        if text:
            problems.append(text)
    return problems or [f"包完整性校验未通过（退出码 {proc.returncode}）"]


def precheck(params: dict) -> tuple[list[str], list[str]]:
    """开工前一次性把所有问题查出来。

    设计原则：**一屏列出全部缺失项**。
    之前包被裁剪时，这里先报"自带工具缺失"、跑起来才报"依赖缺失"，
    两次报错分两轮，用户得来回试。现在把三件事（包完整性 / 自带工具 / 输入料）
    全查完再一次性报出来。
    """
    blockers, notes = [], []
    scene = params.get("sceneId") or "model-01"
    if not re.fullmatch(r"[A-Za-z0-9._-]+", scene):
        blockers.append("参数 sceneId 只能是 ASCII 字母数字 . _ -（它决定 01 的解包目录名）")

    # ---- 1) 整包完整性（Skills 脚本 / tools 二进制 / runtime 依赖）----
    # 包被裁剪是实战里真发生过的：整个 Skills/ 没复制过去、runtime 缺 trimesh，
    # 都是跑起来才暴露。清单随包走，拷包时一起带走，开工前就能查出来。
    manifest_problems = check_package_manifest()
    if manifest_problems:
        blockers.append(f"包完整性不通过（{len(manifest_problems)} 项，见下方逐条）")
        for item in manifest_problems:
            blockers.append("  · " + item)
    else:
        notes.append("包完整性：通过（清单内关键文件均就位且哈希一致）")

    # ---- 2) 自带工具 ----
    # 转换器与浏览引擎都随包分发；缺了就是安装不完整，用 CARDO_OSGB_TOOLS 可临时顶一份。
    external_tools = os.environ.get("CARDO_OSGB_TOOLS", "")
    for label, rel in TOOLS:
        if (SKILL / rel).is_file():
            continue
        if external_tools and Path(external_tools).is_dir():
            notes.append(f"包内缺转换器，使用 CARDO_OSGB_TOOLS：{external_tools}")
        else:
            notes.append("包内缺转换器 fanvanzh-3dtiles v0.4（安装包不完整）："
                         f"应位于 {norm(SKILL / 'Skills/02-工具探测/tools/fanvanzh-3dtiles/v0.4/3dtile.exe')}；"
                         "请重新安装本插件，或设 CARDO_OSGB_TOOLS 指向自备的 fanvanzh-3dtiles 目录")

    # ---- 2.5) 模型浏览引擎（assets/viewer，随包分发）----
    if not viewer_ready():
        notes.append(VIEWER_HINT)

    # ---- 3) 输入料 ----
    zips = find_zips()
    if not zips:
        blockers.append("缺必需输入：OSGB 模型包 zip → 放进 输入文件/01-模型包/（只放一个）")
    elif len(zips) > 1:
        names = "、".join(p.name for p in zips)
        blockers.append(f"模型包只能放一个，现有 {len(zips)} 个（{names}）：移走多余的再重跑")
    else:
        ok, msg = check_zip(zips[0])
        (notes if ok else blockers).append(f"{zips[0].name}：{msg}")

    base = ROOT / "输入文件"
    stats = [p for p in base.rglob("*") if p.is_file()] if base.is_dir() else []
    notes.append(f"输入区文件数：{len(stats)}")
    return blockers, notes


# ---------------------------------------------------------------- 主流程

def run_pipeline(cmd: list[str], env: dict) -> tuple[int, str]:
    """边跑边打印，同时留全文供判定停顿/失败位置。"""
    lines = []
    p = subprocess.Popen(cmd, cwd=str(ROOT), env=env, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    for line in p.stdout:
        say(line.rstrip("\n"))
        lines.append(line)
    return p.wait(), "".join(lines)


def cmd_hint(*args: str) -> str:
    """给用户看的一条可直接粘贴的命令。

    优先用包内便携 python（runtime/python）—— 没装 Python 的机器也能跑；
    包里没有 runtime 时才退回系统 python。
    """
    if RUNTIME_PY.is_file():
        try:
            exe = str(RUNTIME_PY.relative_to(ROOT))
        except ValueError:
            exe = str(RUNTIME_PY)
        if os.name == "nt":
            exe = exe.replace("/", "\\")
    else:
        exe = "python"
    if RUNTIME_PY.is_file():
        parts = [exe, "{}\\开工.py".format(SKILL.name)]
    else:
        parts = ["python", "<技能目录>/scripts/cardo.py"]
    parts.extend(args)
    return " ".join(parts)


def write_board_status() -> None:
    """把工作区真实进度写进看板的 WORKSPACE_STATUS 块（顶栏据此显示真实状态）。

    看板是单文件 HTML，双击打开读不到工作区里的文件，所以状态只能由 开工.py 注入。
    只替换标记之间的那一段，其余字节原样保留；写不进去（包只读、看板被换掉等）
    就跳过 —— 看板会退回手动勾选计数，不影响主流程。
    """
    if not WORKSPACE_RESOLVED:
        return                      # 工作区没解析成功，别拿默认 ROOT 写错状态
    board = SKILL / "开工看板.html"
    if not board.is_file():
        return
    try:
        with open(board, "r", encoding="utf-8", newline="") as f:
            text = f.read()
    except OSError:
        return
    if STATUS_BEGIN not in text or STATUS_END not in text:
        return
    try:
        status = {
            "updatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
            "modelFound": any((ROOT / "输入文件/01-模型包").glob("*.zip")),
            "convertDone": (ROOT / "过程文件/03-OSGB转3DTiles/转换日志.json").is_file(),
            "deliverables": sum(1 for pat in DELIVERABLES for _ in ROOT.glob(pat)),
        }
    except Exception:
        return
    # </script> 出现在 JSON 里会提前闭合脚本块 —— 转义 "<" 即可
    payload = json.dumps(status, ensure_ascii=False, indent=2).replace("<", "\\u003c")
    eol = "\r\n" if "\r\n" in text else "\n"
    block = (STATUS_BEGIN + eol + "<script>window.WORKSPACE_STATUS = "
             + payload + ";</script>" + eol + STATUS_END)
    head, _, rest = text.partition(STATUS_BEGIN)
    _, _, tail = rest.partition(STATUS_END)
    try:
        with open(board, "w", encoding="utf-8", newline="") as f:
            f.write(head + block + tail)
    except OSError:
        pass


CRASH_LOG = "过程文件/00-总控/异常日志.log"

# 异常类型 → (缺什么, 建议怎么处理)。只覆盖用户真会遇到的几类，
# 其余一律按「程序内部错误」处理，保留日志交给维护人。
_CRASH_HINTS = {
    "ModuleNotFoundError": ("缺少 Python 依赖（包内运行时可能不完整）",
                            "确认 runtime/ 与 Skills/ 是整体复制的；或用系统 Python 按 requirements.txt 装依赖"),
    "ImportError": ("运行环境不完整（缺模块或版本不符）", "重新完整复制 runtime/ 后重跑"),
    "FileNotFoundError": ("需要的文件不存在", "按上面给出的路径补齐后重跑"),
    "PermissionError": ("文件被占用或没有写权限",
                        "关掉正在打开该文件的 Word / Excel / 资源管理器预览窗格后重跑"),
    "UnicodeDecodeError": ("文件不是 UTF-8 编码", "用记事本另存为 UTF-8 后重跑"),
    "KeyError": ("输入表缺少必需的列或字段", "对照 放料清单.md 补齐该列后重跑"),
    "JSONDecodeError": ("JSON 文件内容损坏", "删掉该文件让程序重新生成，或恢复备份"),
}


def _last_step_hint() -> str:
    """从运行日志里找最后一个失败/进行中的步骤，回答「哪一步失败」。

    OSGB 的 运行日志.json 里 steps 是**数组**（code / name / exitCode），
    与 Status/Invest 的字典结构不同，这里按本包真实结构解析。
    """
    path = ROOT / "过程文件/00-总控/运行日志.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "开工准备阶段"
    if isinstance(data, dict):
        failed = str(data.get("failedStep") or "").strip()
        if failed:
            return failed
        for item in reversed(data.get("steps") or []):
            if not isinstance(item, dict):
                continue
            if item.get("exitCode") not in (0, None):
                return "{} {}".format(item.get("code") or "", item.get("name") or "").strip()
    return "开工准备阶段"


def crash_report(exc: BaseException) -> int:
    """未预期异常的统一出口：人话四段 + 落日志；--debug 时保留原始堆栈。"""
    # 用 format_exception 而不是 format_exc：后者不在 except 块里调用时只会写出
    # "NoneType: None"，日志等于没记。这里始终带上 exc 自身的堆栈。
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    log = ROOT / CRASH_LOG
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as f:
            f.write("\n===== {} =====\n".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            f.write("技能包：{}\n".format(SKILL.name))
            f.write("命令：{}\n".format(" ".join(sys.argv)))
            f.write(tb)
    except OSError:
        log = None
    if "--debug" in sys.argv or os.environ.get("CARDO_DEBUG"):
        raise exc
    name = type(exc).__name__
    what, action = _CRASH_HINTS.get(
        name, ("程序内部错误", "把异常日志发给维护人；急用可先 --from <上一步> 续跑"))
    say("")
    say("[异常] 程序没有正常跑完")
    say("  哪一步失败：{}".format(_last_step_hint()))
    say("  缺什么：{}（{}: {}）".format(what, name, exc))
    say("  建议怎么处理：{}".format(action))
    say("  日志在哪：{}".format(norm(log.relative_to(ROOT)) if log else "（日志写入失败）"))
    say("  要看完整堆栈：命令末尾加 --debug 重跑")
    return 1


def main(argv=None) -> int:
    """跑完（含各种提前返回）后刷新看板顶栏的真实状态。"""
    try:
        return _main(argv)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        say("\n[中断] 已停止；已完成步骤的产物保留，续跑用 --from <步>。")
        return 130
    except BaseException as exc:  # noqa: BLE001
        return crash_report(exc)
    finally:
        try:
            write_board_status()
        except Exception:
            pass


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=f"{TITLE} · 一条命令开工")
    ap.add_argument("--project", default="", help="项目/片区名称（仅记录）")
    ap.add_argument("--scene-id", default="", help="模型场景 ID（纯 ASCII，默认 model-01）")
    ap.add_argument("--from", dest="start_from", default="", help="从第几步开始（续跑用，如 01）")
    ap.add_argument("--to", dest="stop_at", default="", help="跑到第几步为止（如 02 冒烟）")
    ap.add_argument("--check", action="store_true", help="只做结构与输入校验，不跑流水线")
    ap.add_argument("--no-install", action="store_true", help="缺依赖时不安装")
    ap.add_argument("--dry-run", action="store_true", help="只打印将要执行的命令")
    ap.add_argument("--workspace", default="", help="工作区根（缺省为技能包所在目录，即上一级）")
    ap.add_argument("--debug", action="store_true",
                    help="出错时打印完整堆栈（默认只给人话提示）")
    ap.add_argument("--intake-only", action="store_true",
                    help="只建目录并核对放料，生成 放料清单.md / 待补料.json 后返回（不装依赖、不跑流水线）")
    ap.add_argument("passthrough", nargs=argparse.REMAINDER, help="其余参数原样传给流水线")
    args = ap.parse_args(argv)

    if not os.environ.get("CARDO_REEXEC"):
        say(f"== {TITLE} · 开工 ==")
    if sys.version_info < PY_MIN:
        say(f"[环境] 需要 Python 3.{PY_MIN[1]} 及以上，当前 {sys.version.split()[0]}。")
        return 2

    global ROOT, WORKSPACE_RESOLVED
    ROOT = resolve_workspace(args.workspace)
    WORKSPACE_RESOLVED = True

    if args.intake_only:
        # 空工作区首跑：不装依赖、不跑流水线，只建目录 + 落《放料清单.md》《待补料.json》。
        # 文档承诺「缺料就 exit 3 且生成这两份清单」，启动器在无料时调这里，保证口径一致。
        say(f"[0/4] 工作区：{ROOT}（只做放料检查，不跑流水线）")
        made = make_dirs()
        say(f"[1/4] 目录：{'新建 ' + '、'.join(made) if made else '已就绪'}")
        rows = intake_scan()
        ready = sum(1 for item, hits in rows if hits)
        list_path = write_intake_list(rows)
        say(f"  放料清单：{norm(list_path.relative_to(ROOT))}（已到位 {ready}/{len(rows)} 项）")
        blockers, notes = precheck(load_params())
        for n in notes:
            say(f"  · {n}")
        for b in blockers:
            say(f"  ✗ {b}")
        pending = write_pending(rows, blockers, notes)
        say(f"  待补料清单：{norm(pending.relative_to(ROOT))}")
        return 3 if blockers else 0

    if not os.environ.get("CARDO_REEXEC"):
        say(f"[0/4] 工作区：{ROOT}（技能包只读：{SKILL.name}）")
    board = SKILL / "开工看板.html"
    if os.environ.get("CARDO_REEXEC"):
        board = Path("")  # 重启时不重复提示
    if board.is_file():
        say("      开工看板：{}（双击打开：步骤、放料清单、可复制提示词）".format(norm(board)))

    py, installed = ensure_env(args.no_install)
    if py is None:
        return 2
    if py != sys.executable:
        say(f"[1/4] 依赖：改用 {py}")
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1",
                   PYTHONDONTWRITEBYTECODE="1", CARDO_REEXEC="1")
        r = subprocess.run([py, str(Path(__file__).resolve())] + list(sys.argv[1:]),
                           cwd=str(ROOT), env=env)
        return r.returncode
    if py == str(RUNTIME_PY):
        say("[1/4] 依赖：就位（包内便携环境，无需联网）")
    else:
        say("[1/4] 依赖：就位" + ("（已装入 .venv）" if installed else ""))

    made = make_dirs()
    problems = release_assets()
    if problems:
        for p in problems:
            say(f"  ✗ {p}")
        return 2
    say(f"[2/4] 目录：{'新建 ' + '、'.join(made) if made else '已就绪'}；"
        f"3dtile.exe {'在位' if (SKILL / TOOLS[0][1]).is_file() else '包内缺（03 会跳过转换）'}")
    if not viewer_ready():
        say("")
        say("  [提示] 包内缺模型浏览引擎（assets/viewer）：安装包不完整，07 会跳过「模型浏览」。")
        say(f"    期望：{norm(SKILL / 'assets/viewer/tool/caddy.exe')}")
        say("    处置：重新安装本插件（Release tgz 或 github:CSCCTang/cardo-osgb#v0.1.4）")
        say("")
    else:
        say(f"      模型浏览引擎：在位（{norm(SKILL / 'assets/viewer')}）")

    rows = intake_scan()
    ready = sum(1 for item, hits in rows if hits)
    list_path = write_intake_list(rows)
    say(f"  放料清单：{norm(list_path.relative_to(ROOT))}（已到位 {ready}/{len(rows)} 项）")

    say("[3/4] 参数与输入")
    params = load_params()
    if args.project:
        params["projectName"] = args.project
    if args.scene_id:
        params["sceneId"] = args.scene_id
    blockers, notes = precheck(params)

    for n in notes:
        say(f"  · {n}")
    if blockers:
        for b in blockers:
            say(f"  ✗ {b}")
        say(f"  本轮到此为止，只停这一次：请把 OSGB 模型包 zip 放到 {norm(ROOT / '输入文件/01-模型包')}，"
            f"放好后回一句「放好了」再重跑 {cmd_hint()}。")
        say("  不做轮询、不做后台守候、不自动扫描目录变化。")
        rows = intake_scan()
        pending = write_pending(rows, blockers, notes)
        say(f"  待补料清单：{norm(pending.relative_to(ROOT))}（缺什么、放哪、怎么重跑，都在里面）")
        return 3
    write_json(ROOT / PARAMS, params)
    say(f"  场景：{params['sceneId']}" + (f"｜项目：{params['projectName']}" if params.get("projectName") else ""))

    if args.check:
        say("  校验通过（--check 不跑流水线）。")
        return 0

    say("[4/4] 流水线")
    cmd = [py, str(SKILL / PIPELINE), "run", "--workspace", ".",
           "--tools-dir", str(SKILL / "Skills/02-工具探测/tools")]
    if args.start_from:
        cmd += ["--from", args.start_from]
    if args.stop_at:
        cmd += ["--to", args.stop_at]
    cmd += list(args.passthrough)
    say("  " + " ".join(cmd))
    if args.dry_run:
        return 0

    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1",
               PYTHONDONTWRITEBYTECODE="1", CARDO_SKILLS_ROOT=str(SKILL / "Skills"),
               CARDO_OSGB_TOOLS=str(SKILL / "Skills/02-工具探测/tools"))
    code, text = run_pipeline(cmd, env)

    if code == 0:
        say("[完成] 全链通过。成果：")
        for pat in DELIVERABLES:
            for p in sorted(ROOT.glob(pat)):
                say(f"  - {norm(p.relative_to(ROOT))}")
        return 0

    # 定位失败的那一步：先读总控的运行日志（字段是 steps / code / exitCode）
    failed = ""
    try:
        doc = read_json(ROOT / "过程文件/00-总控/运行日志.json", {}) or {}
        failed = (doc.get("failedStep") or "") or ""
        if not failed:
            steps = doc.get("steps") or []
            bad = next((s for s in reversed(steps)
                        if s.get("exitCode") not in (0, None)), None)
            if bad:
                failed = bad.get("code") or ""
    except Exception:
        failed = ""
    stop = re.search(r"\[STOP\]\s*\[(\d{2})\]", text)
    if not failed and stop:
        failed = stop.group(1)

    if code == 3 or stop:
        say(f"[待确认] 停在需要人工介入的位置（步骤 {failed or '?'}）。")
        g = GUIDES.get(failed)
        if g:
            say(f"  做什么：{g}")
        say(f"  办完后续跑：{cmd_hint('--from ' + (failed or '01'))}")
        return 3

    say(f"[失败] 流水线退出码 {code}（步骤 {failed or '?'}）。")
    g = GUIDES.get(failed)
    if g:
        say(f"  做什么：{g}")
    report = ROOT / "过程文件/00-总控/运行报告.md"
    if report.is_file():
        say("  运行报告末段：")
        for line in report.read_text(encoding="utf-8", errors="replace").splitlines()[-20:]:
            say("  " + line)
    say(f"  修好后续跑：{cmd_hint('--from ' + (failed or '01'))}（不要跳过失败步骤）")
    return code if code in (1, 2) else 1


if __name__ == "__main__":
    sys.exit(main())
