#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""00 · Cardo 低空解析总控：唯一入口（run / algos / status / scaffold）。

用法（一律在工作区根执行）：
    python <技能目录>/Skills/00-Cardo低空解析总控/scripts/pipeline.py run --workspace .
    python <技能目录>/Skills/00-Cardo低空解析总控/scripts/pipeline.py run --workspace . --from 03 --to 06
    python <技能目录>/Skills/00-Cardo低空解析总控/scripts/pipeline.py algos                     # 看现在能跑哪些算法
    python <技能目录>/Skills/00-Cardo低空解析总控/scripts/pipeline.py algos --all               # 含不可用项与原因
    python <技能目录>/Skills/00-Cardo低空解析总控/scripts/pipeline.py run --select              # 交互式挑算法
    python <技能目录>/Skills/00-Cardo低空解析总控/scripts/pipeline.py run --algos recon.ndsm,recon.instance
    python <技能目录>/Skills/00-Cardo低空解析总控/scripts/pipeline.py status --workspace .
    python <技能目录>/Skills/00-Cardo低空解析总控/scripts/pipeline.py scaffold --target ../CardoOSGB-B

纪律：
· 每步用**独立子进程**跑该技能 scripts 下的脚本（不跨技能 import），cwd 固定为工作区根；
· 闸门 = 子进程退出码 0（脚本内部还会做产物先删旧、契约/回读/复算等自校验）；
· 失败即停，打印该步 stderr 末段（≤2000 字），并把失败写进 运行日志.json / 运行报告.md；
· 日志与报告中不出现盘符绝对路径（工作区替换为 <workspace>）；
· 算法清单只认 `算法注册表.json`（同一份表既供 CLI 读，也供人读），不在代码里另写一份。
退出码：0 全链通过；1 某步真失败；2 环境或结构问题；3 料不对/待人工介入。
      各步自己的 0/2/3/4/5 由 public_exit() 折成这四档，不一律压成 1。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
# 技能链根：优先环境变量（工作区可在技能包之外），缺省为本包 Skills/
SKILLS = os.path.abspath(os.environ.get("CARDO_SKILLS_ROOT")
                         or os.path.join(HERE, "..", ".."))
sys.dont_write_bytecode = True

STEPS = [
    ("01", "模型包接入", "01-模型包接入", "unpack_model.py", "输入清单.json", "解包：安全解包 + ASCII 归一 + SRS"),
    ("02", "工具探测", "02-工具探测", "probe_tools.py", "工具探测.json", "白名单 sha256/清单指纹/版本字面量"),
    ("03", "OSGB→3D Tiles", "03-OSGB转3DTiles", "convert.py", "转换日志.json", "调 3dtile.exe 生成 b3dm + tileset"),
    ("04", "垂直配准修正", "04-垂直配准修正", "fix_registration.py", "配准修正.json", "SRSOrigin.z × up 叠加进 root.transform"),
    ("05", "模型体检", "05-模型体检", "inspect_model.py", "体检.json", "H-01…H-06 + 模型契约"),
    ("06", "2.5D 网格指标", "06-2.5D网格指标", "build_indicators.py", "现状指标.json", "7 条指标 × 10 键 + 区间"),
    ("07", "成果导出", "07-成果导出", "export_deliverables.py", "导出日志.json", "docx ×2 + xlsx ×1，回读核对"),
    ("08", "验证与边界", "08-验证与成稿", "independent_verify.py", "验证报告.json", "独立复算 + 基线对账 + 边界扫描"),
    ("09", "三维要素反演", "09-三维要素反演", "run_reconstruct.py", "三维要素反演.json",
     "DSM/DTM/nDSM + 逐栋建筑 + GeoJSON（只吃 b3dm）"),
    ("10", "矢量入库", "10-矢量入库", "build_vectors.py", "拓扑清理.json",
     "拓扑清理 + 属性表 + 字段字典（读 09 的矢量化）"),
    ("11", "几何增强", "11-几何增强", "run_enhance.py", None,
     "地面滤波/栅格分类/单木分割/网格修复（只吃 09 产物）"),
    ("12", "屋顶与立面", "12-屋顶与立面", "run_roof_facade.py", None,
     "屋面分割 + 立面法向统计（只吃 b3dm 与 09 产物）"),
    ("13", "矢量增强", "13-矢量增强", "run_vector.py", None,
     "轮廓矢量化 + GeoPackage 入库（只吃 09/10 产物）"),
    ("14", "场地要素提取", "16-场地要素", "extract_site_features.py", "场地要素.json",
     "硬地/街巷/低矮体量/空地/低洼/破损线索——几何代理量（不是语义判定）"),
    ("15", "扩展指标汇总", "14-扩展指标", "build_extended_metrics.py", "扩展指标.json",
     "把 09–14 已落地算法的产出汇成扩展指标（可回溯到算法 id / 步骤 / 字段）"),
    ("16", "可观测类型矩阵", "14-扩展指标", "build_observable_matrix.py", "可观测类型矩阵.json",
     "无人机能扫到哪些类型 × 有哪些开源算法 × 本链落地了哪些（能力边界表）"),
]
# 14/15 步之后要**重跑**这两步：扩展指标要并进《现状指标.xlsx》《指标说明.docx》，
# 成果变了边界扫描与基线对账也得跟着重跑一次。
REFRESH_AFTER_14 = ("07", "08")
STEP_KEYS = {item[0]: {"name": item[1], "folder": item[2], "script": item[3],
                       "product": item[4], "note": item[5]} for item in STEPS}
CODES = [item[0] for item in STEPS]
PROCESS_DIR = "过程文件"
LOG_DIR = os.path.join(PROCESS_DIR, "00-总控")
OUTPUTS = ["成果文件/成品版/模型体检报告/模型体检报告_成品版.docx",
           "成果文件/成品版/现状指标/现状指标_成品版.xlsx",
           "成果文件/成品版/指标说明/指标说明_成品版.docx",
           "成果文件/内部件/模型体检报告/模型体检报告_溯源版.docx",
           "成果文件/内部件/现状指标/现状指标_溯源版.xlsx",
           "成果文件/内部件/现状指标/现状指标依据对照表.xlsx",
           "成果文件/内部件/指标说明/指标说明_溯源版.docx",
           "成果文件/成品版/模型浏览/Start.bat"]
INPUT_DIR = os.path.join("输入文件", "01-模型包")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# --------------------------------------------------------------------------- 运行锁
# 为什么需要：同一工作区同时起两条流水线时，双方都会往
# `过程文件/00-总控/运行日志.json` 写，**后者覆盖前者**——
# 实测就出现过「一条 01 步失败日志 + 一条全链成功日志」互相覆盖，
# 事后完全分不清哪次是真结果。锁把这件事从「静默覆盖」变成「明确拒绝」。
#
# 锁放**工作区**（.cardo/run.lock）而不是包根：技能包是只读的、且可能被多个
# 工作区共用，锁必须按工作区隔离。
LOCK_DIR = ".cardo"
LOCK_NAME = "run.lock"


def _pid_alive(pid: int) -> bool:
    """这个 PID 还活着吗（跨平台，不依赖第三方库）。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                 capture_output=True, text=True, timeout=15)
            return str(pid) in (out.stdout or "")
        except (OSError, subprocess.SubprocessError):
            return True     # 查不了就当活着（宁可多拦一次，别双写日志）
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


def acquire_run_lock(root: str) -> tuple[bool, str]:
    """抢运行锁。返回 (拿到?, 失败原因文本)。

    用 O_CREAT|O_EXCL 原子创建：并发时只有一个进程能建成功。
    若锁文件已存在，先看持锁 PID 还活着吗——
      - 活着 → 真的有另一个 run 在跑，拒绝（exit 3）
      - 死了 → 上次崩溃留下的残锁，自动清掉重抢（不让一次崩溃永久卡死工作区）
    """
    lock_dir = os.path.join(root, LOCK_DIR)
    os.makedirs(lock_dir, exist_ok=True)
    lock_path = os.path.join(lock_dir, LOCK_NAME)
    payload = json.dumps({
        "pid": os.getpid(),
        "startedAt": now_iso(),
        "workspace": root,
        "command": " ".join(sys.argv),
    }, ensure_ascii=False, indent=2)

    for attempt in range(2):
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, payload.encode("utf-8"))
            finally:
                os.close(fd)
            return True, payload
        except FileExistsError:
            pass
        except OSError as exc:
            return False, f"无法创建运行锁：{exc}"

        # 锁已存在：判断是不是崩溃残留
        holder = {}
        try:
            with open(lock_path, encoding="utf-8") as fh:
                holder = json.load(fh)
        except (OSError, ValueError):
            holder = {}
        pid = int(holder.get("pid") or 0)
        if _pid_alive(pid):
            return False, json.dumps(holder, ensure_ascii=False, indent=2)
        # 持锁进程已死 → 残锁，清掉重抢（只重试一次）
        try:
            os.remove(lock_path)
        except OSError:
            return False, json.dumps(holder, ensure_ascii=False, indent=2)
    return False, "运行锁获取失败"


def release_run_lock(root: str) -> None:
    """释放运行锁（只删自己那份——PID 对不上就不动）。"""
    lock_path = os.path.join(root, LOCK_DIR, LOCK_NAME)
    try:
        with open(lock_path, encoding="utf-8") as fh:
            holder = json.load(fh)
        if int(holder.get("pid") or 0) != os.getpid():
            return
    except (OSError, ValueError):
        pass
    try:
        os.remove(lock_path)
    except OSError:
        pass


def write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def scrub(text: str, root: str) -> str:
    """把工作区绝对路径换成占位符（日志不落盘符）。"""
    if not text:
        return ""
    variants = {root, root.replace("\\", "/"), os.path.abspath(root)}
    for raw in variants:
        text = text.replace(raw, "<workspace>")
    return text


def find_input_zip(root: str, explicit: str | None) -> str | None:
    """未给 --input-zip 时，从 输入文件/01-模型包 里取**唯一** zip（不猜多个）。

    递归找（子目录里放也算），与 开工.py 的 find_zips() 口径完全一致——
    两层找包口径必须相同，否则会出现"预检说通过、实跑说缺料"。
    """
    if explicit:
        return explicit.replace("\\", "/")
    folder = os.path.join(root, INPUT_DIR)
    if not os.path.isdir(folder):
        return None
    found = []
    for dirpath, _dirs, names in os.walk(folder):
        for name in names:
            if not name.lower().endswith(".zip"):
                continue
            if name.startswith("~$"):
                continue
            rel = os.path.relpath(os.path.join(dirpath, name), root)
            found.append(rel.replace("\\", "/"))
    return found[0] if len(found) == 1 else None


# 各步脚本自己的退出码 → 总控对外退出码。
# 对外只有四档：0 全过 / 1 真失败 / 2 环境或结构问题 / 3 料不对（可指导修正）。
# 这样"料不对"和"程序崩了"在调用方看来不再同码。
STEP_EXIT_MAP = {
    "01": {2: 3, 3: 3, 4: 2, 5: 3},   # zip 损坏/布局/安全条目=料(3)；解包目录已存在=结构(2)
    "02": {2: 2, 3: 2, 4: 2},          # 工具缺失/指纹不符/参数错=环境(2)
    "03": {2: 1, 3: 1},                # 输入缺失/转换失败=真失败(1)
    "04": {2: 1, 3: 1, 4: 1},
    "05": {2: 1, 3: 1},
    "06": {2: 1, 3: 1},
    "07": {2: 1, 3: 1},
    "08": {2: 1, 3: 1},
}


def public_exit(code: str, raw: int) -> int:
    """把步内退出码折成对外四档；未登记的按"真失败"处理。"""
    return STEP_EXIT_MAP.get(code, {}).get(raw, 1)


def tail_line(stdout: str) -> str:
    """取脚本 stdout 最后一行，并剥掉它自己的步骤标签。

    各步脚本打印的是「目录编号」（如 16-场地要素 打 `[16]`、14-扩展指标 打
    `[14]`），而总控的步骤号是排产顺序（场地要素=14、扩展指标汇总=15）。
    两者并列会出现 `<<< 14 … [16] 完成` 这种看着像错位的日志。这里只剥打印
    用的标签，JSON 里仍保留脚本原话。
    """
    line = (stdout.splitlines() or [""])[-1]
    return re.sub(r"^\[\d+(?::[^\]]*)?\]\s*", "", line).strip()


def step_command(args, code: str, root: str) -> list:
    path = os.path.join(SKILLS, STEP_KEYS[code]["folder"], "scripts", STEP_KEYS[code]["script"])
    cmd = [sys.executable, path]
    if code in ("01", "09", "10", "11", "12", "13"):
        # 这些步都是「先删旧再生成」的幂等重建，总控重跑时直接给 --force
        cmd += ["--force"]
    if code == "01":
        zip_rel = find_input_zip(root, args.input_zip)
        if zip_rel:
            cmd += ["--zip", zip_rel]
    if code in ("02", "03", "05") and args.tools_dir:
        cmd += ["--tools-dir", args.tools_dir]
    return cmd


def step_env(code: str) -> dict:
    """子进程环境。

    关键：把**该步脚本所在目录**加进 PYTHONPATH。
    包内便携 runtime 用 python313._pth 把 sys.path 锁死（脚本目录永不入 path），
    于是「import 同目录模块」会失败——02/03/05/06 步都依赖这种 import。
    显式给 PYTHONPATH 就能跨过这个隔离，且对系统 Python 同样无害。
    """
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1",
               PYTHONDONTWRITEBYTECODE="1")
    script_dir = os.path.join(SKILLS, STEP_KEYS[code]["folder"], "scripts")
    existing = env.get("PYTHONPATH", "")
    parts = [script_dir] + ([existing] if existing else [])
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return env


def product_path(code: str, root: str) -> str | None:
    product = STEP_KEYS[code]["product"]
    if code == "07" or not product:
        return None
    return os.path.join(root, PROCESS_DIR, STEP_KEYS[code]["folder"], product)


def products_of(code: str, root: str) -> list:
    folder = STEP_KEYS[code]["folder"]
    if code == "01":
        return [os.path.join(root, PROCESS_DIR, folder, name) for name in ("输入清单.json", "解包日志.md")]
    if code == "07":
        return [os.path.join(root, "成果文件", name) for name in OUTPUTS] + [
            os.path.join(root, PROCESS_DIR, folder, "导出日志.json")]
    if code == "03":
        return [os.path.join(root, PROCESS_DIR, folder, "转换日志.json"),
                os.path.join(root, PROCESS_DIR, folder, "转换产物", "tileset.json")]
    if code == "08":
        return [os.path.join(root, PROCESS_DIR, folder, name)
                for name in ("验证报告.json", "独立验证结果.json")]
    if code == "09":
        return [os.path.join(root, PROCESS_DIR, folder, name)
                for name in ("三维要素反演.json", "要素矢量.geojson", "逐栋属性.json")]
    if code == "10":
        return [os.path.join(root, PROCESS_DIR, folder, name)
                for name in ("拓扑清理.json", "矢量成果.geojson", "属性表.csv", "字段字典.md")]
    if code == "11":
        return [os.path.join(root, PROCESS_DIR, folder, slug, f"{slug}.json")
                for slug in ("pc-ground-filter", "pc-classify", "tree-single", "mesh-repair")]
    if code == "12":
        return [os.path.join(root, PROCESS_DIR, folder, slug, f"{slug}.json")
                for slug in ("roof-segment", "facade-normal")]
    if code == "13":
        return [os.path.join(root, PROCESS_DIR, folder, "vector-polygonize", "vector-polygonize.json"),
                os.path.join(root, PROCESS_DIR, folder, "vector-polygonize", "建筑轮廓面.geojson"),
                os.path.join(root, PROCESS_DIR, folder, "vector-gpkg", "vector-gpkg.json"),
                os.path.join(root, PROCESS_DIR, folder, "vector-gpkg", "矢量成果.gpkg")]
    if code == "14":
        return [os.path.join(root, PROCESS_DIR, folder, name)
                for name in ("场地要素.json", "场地要素.md")]
    if code == "15":
        return [os.path.join(root, PROCESS_DIR, folder, name)
                for name in ("扩展指标.json", "扩展指标.md")]
    if code == "16":
        return [os.path.join(root, PROCESS_DIR, folder, name)
                for name in ("可观测类型矩阵.json", "可观测类型矩阵.md")]
    return [product_path(code, root)]


def step_inputs(code: str, root: str) -> list:
    """该步**必须先存在**的输入（相对路径）。缺了就得从更早的步骤补跑。"""
    def P(*parts):
        return os.path.join(root, PROCESS_DIR, *parts)

    table = {
        "01": [] if find_input_zip(root, None) else ["输入文件/01-模型包/<唯一 .zip>"],
        "02": [P("01-模型包接入", "输入清单.json")],
        "03": [P("01-模型包接入", "输入清单.json"), P("02-工具探测", "工具探测.json")],
        "04": [P("03-OSGB转3DTiles", "转换产物", "tileset.json")],
        "05": [P("03-OSGB转3DTiles", "转换日志.json"), P("04-垂直配准修正", "配准修正.json")],
        "06": [P("03-OSGB转3DTiles", "转换产物", "cardo-lowair-model.json")],
        "07": [P("05-模型体检", "体检.json"), P("06-2.5D网格指标", "现状指标.json")],
        "08": [P("06-2.5D网格指标", "现状指标.json")],
        "09": [P("03-OSGB转3DTiles", "转换产物", "cardo-lowair-model.json")],
        "10": [P("09-三维要素反演", "要素矢量.geojson")],
        "11": [P("03-OSGB转3DTiles", "转换产物", "cardo-lowair-model.json"),
               P("09-三维要素反演", "三维要素反演.json"), P("09-三维要素反演", "栅格", "栅格.npz")],
        "12": [P("03-OSGB转3DTiles", "转换产物", "cardo-lowair-model.json"),
               P("09-三维要素反演", "三维要素反演.json"), P("09-三维要素反演", "栅格", "栅格.npz")],
        "13": [P("03-OSGB转3DTiles", "转换产物", "cardo-lowair-model.json"),
               P("09-三维要素反演", "栅格", "栅格.npz"), P("10-矢量入库", "矢量成果.geojson")],
        # 14 场地要素：读 09 的栅格与反演结果（+12 的屋面平面，缺失则跳过屋面线索）
        "14": [P("09-三维要素反演", "栅格", "栅格.npz"), P("09-三维要素反演", "三维要素反演.json")],
        # 15 扩展指标汇总：把 09–14 的产出汇成指标，至少要 09 的反演结果
        "15": [P("09-三维要素反演", "三维要素反演.json")],
        # 16 可观测类型矩阵：读 15 的扩展指标（外加 06 的核心指标）
        "16": [P("14-扩展指标", "扩展指标.json")],
    }
    return table.get(code, [])


def step_ready(code: str, root: str) -> bool:
    """输入齐了 = 这一步现在能直接跑（不保证一定成功，只是不缺料）。"""
    return all(os.path.isfile(path) for path in step_inputs(code, root))


def resolve_steps(codes, root: str) -> list:
    """选中的步骤 → 实际执行序列：缺输入就自动从更早的步骤补跑（最多补到 01）。"""
    need = set(codes)
    for code in sorted(set(codes), key=CODES.index):
        missing = [c for c in CODES[:CODES.index(code)] if not step_ready(c, root)]
        if missing:
            head = min(CODES.index(c) for c in missing)
            need.update(CODES[head: CODES.index(code) + 1])
    return [c for c in CODES if c in need]


REGISTRY_PATH = os.path.join(os.path.dirname(HERE), "算法注册表.json")
REGISTRY_SCHEMA = "cardo/lowair-algo-registry/v1"
WIDE = set("　、。，；：？！（）《》“”‘’…—·")   # 终端宽度按 2 计的字符（近似）


def disp_width(text: str) -> int:
    """中英混排的显示宽度（CJK 与全角标点算 2）。"""
    return sum(2 if ord(ch) > 0x2E80 or ch in WIDE else 1 for ch in text)


def pad(text: str, width: int) -> str:
    return text + " " * max(0, width - disp_width(text))


def wrap_cn(text: str, width: int) -> list:
    """按显示宽度折行（中文逐字断行，英文按字符累积；只是缩进说明，不做排版承诺）。"""
    lines, cur, cur_w = [], "", 0
    for ch in text:
        w = 2 if (ord(ch) > 0x2E80 or ch in WIDE) else 1
        if cur_w + w > width and cur:
            lines.append(cur)
            cur, cur_w = ch, w
        else:
            cur += ch
            cur_w += w
    if cur:
        lines.append(cur)
    return lines or [""]


def load_registry():
    """读算法注册表；返回 (doc, 错误说明)。"""
    if not os.path.isfile(REGISTRY_PATH):
        return None, f"缺算法注册表：{REGISTRY_PATH}"
    try:
        with open(REGISTRY_PATH, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as exc:
        return None, f"算法注册表不可读：{type(exc).__name__}: {exc}"
    if doc.get("schema") != REGISTRY_SCHEMA:
        return None, f"算法注册表 schema 非 {REGISTRY_SCHEMA}：{doc.get('schema')}"
    if not isinstance(doc.get("algos"), list) or not doc["algos"]:
        return None, "算法注册表 algos 为空"
    return doc, None


def number_algos(doc) -> dict:
    """注册表顺序 → 编号（从 1 开始），供 --select 用。"""
    return {item["id"]: i + 1 for i, item in enumerate(doc["algos"])}


def find_algos(doc, tokens) -> tuple:
    """--algos / --select 的输入 → (选中的 algo 列表, 未识别项)。支持 id、编号、名称。"""
    by_id = {item["id"]: item for item in doc["algos"]}
    by_no = {str(no): item["id"] for item, no in
             ((item, i + 1) for i, item in enumerate(doc["algos"]))}
    picked, unknown = [], []
    for raw in tokens:
        token = raw.strip()
        if not token:
            continue
        if token in by_id:
            picked.append(by_id[token])
        elif token in by_no:
            picked.append(by_id[by_no[token]])
        else:
            hit = [item for item in doc["algos"] if item["name"] == token]
            if len(hit) == 1:
                picked.append(hit[0])
            else:
                unknown.append(token)
    return picked, unknown


def cmd_algos(args) -> int:
    root = os.path.abspath(args.workspace)
    doc, err = load_registry()
    if doc is None:
        print(f"[00] {err}", file=sys.stderr)
        return 2
    numbers = number_algos(doc)
    groups = {g["id"]: g for g in doc.get("groups") or []}
    statuses = ("ready", "needs-imagery", "needs-2epoch", "needs-truth", "planned")
    counts = {s: sum(1 for item in doc["algos"] if item.get("status") == s) for s in statuses}
    print(f"[算法清单] {doc['title']}（{len(doc['algos'])} 项）"
          f"　可跑 {counts['ready']} / 需影像 {counts['needs-imagery']} / 需两期 "
          f"{counts['needs-2epoch']} / 需真值 {counts['needs-truth']} / 待实现 {counts['planned']}")
    print(f"[状态] " + "　".join(f"{s}={doc['statusLegend'].get(s, '')}" for s in statuses))
    print()

    wanted_status = args.status
    wanted_group = args.group
    show = [item for item in doc["algos"]
            if (args.all or wanted_status or wanted_group)
            and (not wanted_status or item.get("status") == wanted_status)
            and (not wanted_group or item.get("group") == wanted_group)]
    if not (args.all or wanted_status or wanted_group):
        show = [item for item in doc["algos"] if item.get("status") == "ready"]

    current_group = None
    for item in show:
        gid = item.get("group")
        if gid != current_group:
            current_group = gid
            group = groups.get(gid) or {"name": gid, "note": ""}
            print(f"== {group['name']} ==　{group.get('note', '')}")
        no = numbers[item["id"]]
        suffix = ""
        if item.get("status") == "ready" and item.get("step"):
            suffix = "（输入齐，可直接跑）" if step_ready(item["step"], root) else "（缺输入，会自动补跑前置步骤）"
        status = item.get("status")
        mark = {"ready": "可跑", "needs-imagery": "需影像", "needs-2epoch": "需两期",
                "needs-truth": "需真值", "planned": "待实现"}.get(status, status)
        print(f"#{no:<3} {pad(item['id'], 26)}{pad(item['name'], 30)} [{mark}]"
              + (f"  {item['step']}" if item.get("step") else "") + suffix)
        for line in wrap_cn("干什么：" + item.get("plain", ""), 96):
            print("     " + line)
        for line in wrap_cn("需要：" + item.get("input", "") + "　产出：" + item.get("output", ""), 96):
            print("     " + line)
        if status != "ready" and item.get("reason"):
            for line in wrap_cn("为什么不可用：" + item["reason"], 96):
                print("     " + line)
        print()

    if not (args.all or wanted_status or wanted_group):
        print("以上是**现在就能跑**的算法。看全部（含不可用项与原因）："
              "`algos --all`；只看某类：`algos --status needs-imagery` 或 `algos --group planned`。")
    print("选择算法跑：`run --select`（交互编号）或 `run --algos <id,id>`；只列计划不执行：加 `--dry-run`。")
    return 0


def run_step(args, code: str) -> dict:
    root = os.path.abspath(args.workspace)
    cmd = step_command(args, code, root)
    started = time.time()
    env = step_env(code)
    try:
        proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=args.timeout,
                              env=env)
        exit_code, out, err = proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired:
        exit_code, out, err = -9, "", f"超时（>{args.timeout}s）"
    except OSError as exc:
        exit_code, out, err = -1, "", f"启动失败：{type(exc).__name__}: {exc}"
    elapsed = int((time.time() - started) * 1000)
    return {"code": code, "name": STEP_KEYS[code]["name"], "cmd": [scrub(c, root) for c in cmd],
            "exitCode": exit_code, "durationMs": elapsed,
            "stdoutTail": scrub(out.strip()[-1500:], root),
            "stderrTail": scrub(err.strip()[-2000:], root)}


def collect_numbers(root: str) -> dict:
    """从过程 JSON 摘关键值，供 运行报告.md 用（缺文件就省略，不编数）。"""
    found = {}

    def load(rel_path):
        path = os.path.join(root, rel_path)
        if not os.path.isfile(path):
            return None
        try:
            with open(path, encoding="utf-8-sig") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    health = load(os.path.join(PROCESS_DIR, "05-模型体检", "体检.json"))
    if health:
        found["health"] = {"verdict": health.get("verdict"),
                           "counts": health.get("counts"),
                           "contractOk": (health.get("contract") or {}).get("ok"),
                           "gaps": [(g.get("code"), g.get("what")) for g in health.get("gaps") or []]}
    ind = load(os.path.join(PROCESS_DIR, "06-2.5D网格指标", "现状指标.json"))
    if ind:
        found["indicators"] = {"scope": ind.get("scope"),
                               "values": {m["id"]: [m.get("value"), m.get("range")]
                                          for m in ind.get("metrics") or []}}
    verify = load(os.path.join(PROCESS_DIR, "08-验证与成稿", "验证报告.json"))
    if verify:
        found["verify"] = {"summary": verify.get("summary"),
                           "baseline": (verify.get("baseline") or {}).get("status"),
                           "violations": len((verify.get("boundary") or {}).get("violations") or [])}
    model = load(os.path.join(PROCESS_DIR, "03-OSGB转3DTiles", "转换产物", "cardo-lowair-model.json"))
    if model:
        found["model"] = {"name": (model.get("source") or {}).get("name"),
                          "tiles": model.get("tiles"), "geometry": model.get("geometry"),
                          "registration": model.get("georeference")}
    return found


def write_report(root: str, run_doc: dict) -> None:
    numbers = collect_numbers(root)
    lines = ["# 运行报告", "",
             f"- 开始：{run_doc['startedAt']}　结束：{run_doc['finishedAt']}　"
             f"总耗时：{run_doc['durationMs'] / 1000:.1f} s",
             f"- 结果：**{'通过' if run_doc['ok'] else ('未完成（缺工具）' if run_doc.get('skipped') else '失败')}**"
             f"（{run_doc['passed']}/{len(run_doc['steps'])} 步通过，退出码 {run_doc['exitCode']}）",
             f"- 工作区：`{root}`　请求范围：{run_doc['from']} → {run_doc['to']}"
             f"　实际跑到：{run_doc.get('effectiveTo', run_doc['to'])}",
             "", "## 步骤 → 目录 对应表", "",
             "步骤号与落盘目录**不完全同名**（历史原因：场地要素那步的目录名定得早，"
             "后来步骤号又插了扩展指标与能力矩阵两步）。**按这张表找文件**：", "",
             "| 步骤 | 名称 | 落盘目录 |", "|---|---|---|"]
    for code, name, folder, _script, _product, _note in STEPS:
        lines.append(f"| {code} | {name} | `过程文件/{folder}/` |")
    lines += ["", "> 注意：14 步（场地要素提取）的目录是 `过程文件/16-场地要素/`；"
              "15、16 两步共用 `过程文件/14-扩展指标/`（前者写 `扩展指标.json`，"
              "后者写 `可观测类型矩阵.json`）。脚本内部打印的 `[16]`/`[14]` 是脚本自己的编号，"
              "以本表为准。", ""]
    lines += ["## 步骤", "",
              "| 步骤 | 名称 | 退出码 | 耗时 (s) | 说明 |", "|---|---|---|---|---|"]
    for item in run_doc["steps"]:
        lines.append(f"| {item['code']} | {item['name']} | {item['exitCode']} | "
                     f"{item['durationMs'] / 1000:.1f} | {tail_line(item['stdoutTail'])[:120]} |")
    health = numbers.get("health")
    if health:
        lines += ["", "## 体检", "",
                  f"- verdict：`{health['verdict']}`　H 项 pass/warn/fail："
                  f"{health['counts']['pass']}/{health['counts']['warn']}/{health['counts']['fail']}　"
                  f"契约自检：{'通过' if health['contractOk'] else '未通过'}",
                  "- gaps：" + ("；".join(f"`{c}` {w}" for c, w in health["gaps"]) or "（无）")]
    ind = numbers.get("indicators")
    if ind:
        scope = ind["scope"]
        lines += ["", "## 指标（估算值 + 方法敏感性区间）", "",
                  f"- 范围 {scope['areaM2']} m²（bbox 投影），有效格 {scope['validCells']}，"
                  f"mask 格 {scope['maskCells']}，地面 z = {scope['groundZ']} m",
                  "", "| 指标 | 值 | 区间 |", "|---|---|---|"]
        for mid, (value, rng) in ind["values"].items():
            text = "null（占位）" if value is None else str(value)
            span = ("—（不提供）" if not rng or rng[0] is None or rng[1] is None
                    else f"{rng[0]} – {rng[1]}")
            lines.append(f"| `{mid}` | {text} | {span} |")
    verify = numbers.get("verify")
    if verify:
        lines += ["", "## 验证", "",
                  f"- 独立复算 + 对账：pass {verify['summary']['pass']} / fail {verify['summary']['fail']}"
                  f" / skip {verify['summary']['skip']}；基线 `{verify['baseline']}`；"
                  f"边界越界 {verify['violations']} 条"]
    bad = [item for item in run_doc["steps"] if item["exitCode"] not in (0, 4)]
    skip = [item for item in run_doc["steps"] if item["exitCode"] == 4]
    if skip:
        lines += ["", "## 未完成（缺工具，不是崩溃）", ""]
        for item in skip:
            lines += [f"### {item['code']} {item['name']}（跳过）", "", "```text",
                      item["stdoutTail"] or "(无输出)", "```", "",
                      "按上面列出的上游地址/版本/目标目录装好后，"
                      f"用 `--from {item['code']}` 续跑；已完成的步骤不会重做。"]
    if bad:
        lines += ["", "## 失败定位", ""]
        for item in bad:
            lines += [f"### {item['code']} {item['name']}（exit {item['exitCode']}）", "", "```text",
                      item["stderrTail"] or "(无 stderr)", "```", ""]
        lines.append("修法：按上面 stderr 指出的闸门重跑该步（`--from <步>`）；关键产物已按纪律先删旧，"
                     "不存在「旧文件假通过」。")
    lines += ["", "> 本报告由 00 总控生成，数值摘自各步 JSON；不要在报告里写未经 JSON 记录的结论。", ""]
    with open(os.path.join(root, LOG_DIR, "运行报告.md"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines))


def split_tokens(text: str) -> list:
    return [t for t in text.replace("，", ",").replace(" ", ",").split(",") if t.strip()]


def status_mark(status: str) -> str:
    return {"ready": "可跑", "needs-imagery": "需影像", "needs-2epoch": "需两期",
            "needs-truth": "需真值", "planned": "待实现"}.get(status, status or "?")


def plan_from_algos(tokens, root: str, doc, skip_unavailable: bool):
    """选中的算法 → (执行步骤序列, 算法 id 列表)；出错返回 (None, None) 并已打印原因。"""
    picked, unknown = find_algos(doc, tokens)
    if unknown:
        print(f"[00] 未识别的算法：{unknown}（用 `algos` 查正确编号/id）", file=sys.stderr)
        return None, None
    if not picked:
        print("[00] 没有选中任何算法", file=sys.stderr)
        return None, None
    ready = [item for item in picked if item.get("status") == "ready" and item.get("step")]
    blocked = [item for item in picked if not (item.get("status") == "ready" and item.get("step"))]
    if blocked:
        print("[00] 选中的算法里有现在用不了的：")
        for item in blocked:
            print(f"   · {item['id']}（{item['name']}）[{status_mark(item.get('status'))}]："
                  f"{item.get('reason') or '不可用'}")
        if not skip_unavailable:
            print("[00] 已中止，未执行任何步骤。只跑能用的：加 --skip-unavailable。", file=sys.stderr)
            return None, None
        print("[00] 按 --skip-unavailable 跳过上面这些，继续跑可用的。")
    if not ready:
        print("[00] 选中的算法全部不可用，没有可执行步骤", file=sys.stderr)
        return None, None
    steps = resolve_steps({item["step"] for item in ready}, root)
    print("[00] 选中算法 → 步骤：")
    for item in ready:
        print(f"   {pad(item['id'], 26)}{pad(item['name'], 30)} → {item['step']}")
    print("[00] 执行序列：" + " → ".join(steps))
    return steps, [item["id"] for item in ready]


def interactive_select(doc, root: str):
    """交互式挑算法：只列可跑项，输入编号；回车取消。返回 token 列表或 None。"""
    numbers = number_algos(doc)
    groups = {g["id"]: g for g in doc.get("groups") or []}
    print("[选择算法] 现在可跑（编号即序号；可输入多个，逗号/空格分隔；直接回车取消）：")
    current = None
    for item in doc["algos"]:
        if item.get("status") != "ready":
            continue
        if item["group"] != current:
            current = item["group"]
            print(f"\n== {(groups.get(current) or {}).get('name', current)} ==")
        print(f"  #{numbers[item['id']]:<3} {pad(item['id'], 26)}{pad(item['name'], 28)} → {item['step']}")
    print("\n（需影像/两期/待实现的算法见 `algos --all`，本工作区现在跑不了。）")
    try:
        raw = input("选择：")
    except (EOFError, KeyboardInterrupt):
        print("\n[00] 未输入，已取消")
        return None
    raw = raw.strip()
    if not raw:
        print("[00] 未选择，已取消")
        return None
    return split_tokens(raw)


def cmd_run(args) -> int:
    root = os.path.abspath(args.workspace)
    if not os.path.isdir(SKILLS):
        print(f"[00] 找不到技能链（CARDO_SKILLS_ROOT）：{SKILLS}", file=sys.stderr)
        return 2

    # ---- 运行锁：同一工作区不允许两条流水线并行 ----
    # 不加锁时，两条 run 会互相覆盖 过程文件/00-总控/运行日志.json，
    # 事后分不清哪次是真结果（实战踩过）。
    locked, lock_info = acquire_run_lock(root)
    if not locked:
        holder = {}
        try:
            holder = json.loads(lock_info)
        except (OSError, ValueError):
            holder = {}
        pid = holder.get("pid", "?")
        started = holder.get("startedAt", "?")
        print(f"[00] 已有运行 PID {pid}，自 {started} 起，工作区 {root}", file=sys.stderr)
        print("[00] 拒绝并发运行（否则两份运行日志互相覆盖，事后分不清结果）。"
              "等它跑完再重试。", file=sys.stderr)
        return 3
    try:
        return _run_locked(args, root)
    finally:
        release_run_lock(root)


def _run_locked(args, root: str) -> int:
    """真正的 run 主体（已持有运行锁）。"""
    tokens, algo_ids = None, []
    if args.select is not None or args.algos:
        doc, err = load_registry()
        if doc is None:
            print(f"[00] {err}", file=sys.stderr)
            return 2
        if args.select == "__interactive__":
            tokens = interactive_select(doc, root)
            if tokens is None:
                return 0
        else:
            tokens = split_tokens(args.select if args.select is not None else args.algos)
        selected, algo_ids = plan_from_algos(tokens, root, doc, args.skip_unavailable)
        if selected is None:
            return 2
        if args.dry_run:
            print("[00] --dry-run：以上只是计划，未执行任何步骤")
            return 0
        from_step, to_step = selected[0], selected[-1]
    else:
        codes = CODES
        if args.from_step not in codes or args.to_step not in codes:
            print(f"[00] --from/--to 必须是 {codes} 之一", file=sys.stderr)
            return 2
        selected = codes[codes.index(args.from_step): codes.index(args.to_step) + 1]
        if not selected:
            print("[00] --from 晚于 --to，无可执行步骤", file=sys.stderr)
            return 2
        from_step, to_step = args.from_step, args.to_step
        if args.dry_run:
            print("[00] 执行序列：" + " → ".join(selected))
            print("[00] --dry-run：以上只是计划，未执行任何步骤")
            return 0

    if "01" in selected and not find_input_zip(root, args.input_zip):
        print("[00] 输入文件/01-模型包 需要**恰好一个** .zip（或用 --input-zip 指定相对路径）",
              file=sys.stderr)
        return 3

    os.makedirs(os.path.join(root, LOG_DIR), exist_ok=True)
    started = time.time()
    steps, failed, skipped = [], False, []
    failed_code, failed_raw = "", 0
    executed = set()          # 已跑过的步骤号（幂等判断用）
    for code in selected:
        if code in executed:
            continue
        print(f"[00] >>> {code} {STEP_KEYS[code]['name']} ...", flush=True)
        item = run_step(args, code)
        steps.append(item)
        executed.add(code)
        tail = tail_line(item["stdoutTail"])
        print(f"[00] <<< {code} exit={item['exitCode']} {item['durationMs'] / 1000:.1f}s {tail}", flush=True)
        if item["exitCode"] == 4:
            # 工具没装 ≠ 程序崩了。记「跳过」，把该步打印的安装指引原样转给用户，
            # 然后停下（后续步骤依赖它的产物，硬跑只会产生假成果）。
            item["skipped"] = True
            skipped.append(code)
            print(item["stdoutTail"] or "", flush=True)
            print(f"[SKIP] [{code}] {STEP_KEYS[code]['name']}：缺工具，本步与后续依赖步未执行。"
                  "按上面的地址/版本/目录装好后，用 --from {} 续跑。".format(code), flush=True)
            break
        if item["exitCode"] != 0:
            failed = True
            failed_code, failed_raw = code, item["exitCode"]
            print(f"[STOP] [{code}] {STEP_KEYS[code]['name']} 退出码 {item['exitCode']}", flush=True)
            print("[00] 失败，停止后续步骤；stderr 末段：", file=sys.stderr)
            print(item["stderrTail"], file=sys.stderr)
            break
        # 14 汇总完扩展指标 → 15 出能力边界表 → 成果文件要重出 → 边界与基线也得重核
        # 幂等：15/16 若已在本次运行中跑过（例如 --to 16），这里不再重复执行。
        if code == "14":
            for mid in ("15", "16"):
                if mid in executed:
                    print(f"[00] >>> {mid} {STEP_KEYS[mid]['name']}（本次已执行，跳过）...")
                    continue
                print(f"[00] >>> {mid} {STEP_KEYS[mid]['name']} ...", flush=True)
                m_item = run_step(args, mid)
                m_item["code"] = mid
                steps.append(m_item)
                executed.add(mid)
                mtail = tail_line(m_item["stdoutTail"])
                print(f"[00] <<< {mid} exit={m_item['exitCode']} "
                      f"{m_item['durationMs'] / 1000:.1f}s {mtail}", flush=True)
                if m_item["exitCode"] != 0:
                    failed = True
                    failed_code, failed_raw = mid, m_item["exitCode"]
                    print(f"[STOP] [{mid}] 退出码 {m_item['exitCode']}", flush=True)
                    print(m_item["stderrTail"], file=sys.stderr)
                    break
            if failed:
                break
            # 07/08 的重跑同样幂等：--to 16 时它们已在 selected 里跑过
            for again in REFRESH_AFTER_14:
                if again in executed:
                    print(f"[00] >>> {again} {STEP_KEYS[again]['name']}"
                          f"（本次已执行过首跑，仍需重跑：扩展指标已并入）...", flush=True)
                else:
                    print(f"[00] >>> {again} {STEP_KEYS[again]['name']}"
                          f"（扩展指标并入成果后重跑）...", flush=True)
                ref = run_step(args, again)
                ref["code"] = again
                ref["refresh"] = True          # 标记：这是 14 之后的重跑，不是首跑
                steps.append(ref)
                executed.add(again)
                rtail = tail_line(ref["stdoutTail"])
                print(f"[00] <<< {again} exit={ref['exitCode']} "
                      f"{ref['durationMs'] / 1000:.1f}s {rtail}", flush=True)
                if ref["exitCode"] != 0:
                    failed = True
                    failed_code, failed_raw = again, ref["exitCode"]
                    print(f"[STOP] [{again}] 重跑失败，退出码 {ref['exitCode']}", flush=True)
                    print(ref["stderrTail"], file=sys.stderr)
                    break
            if failed:
                break

    # 对外退出码：0 全过 / 1 真失败 / 2 环境或结构问题 / 3 料不对或待备工具（可指导修正）
    if failed:
        exit_code = public_exit(failed_code, failed_raw)
    elif skipped:
        exit_code = 3
    else:
        exit_code = 0
    # effectiveTo = 本次**实际执行到的**最后一步编号。
    # `to` 只是请求范围（--to 14 时钩子会自动续跑 15/16 并重跑 07/08），
    # 两者不同，日志里必须都写清楚，否则「to:14 / steps:18」看着像 bug。
    effective_to = to_step
    for item in steps:
        if item.get("code"):
            effective_to = max(effective_to, item["code"])
    run_doc = {
        "schema": "cardo/lowair-run/v1", "schemaVersion": 1,
        "workspace": "<workspace>", "startedAt": now_iso(), "finishedAt": now_iso(),
        "durationMs": int((time.time() - started) * 1000),
        "from": from_step, "to": to_step, "effectiveTo": effective_to,
        "toNote": ("to 是请求的步骤范围；14 步的钩子会自动续跑 15/16 并重跑 07/08，"
                   "所以实际执行步数可能多于 to 区间——effectiveTo 是实际跑到的最后一步"),
        "selectedBy": ("algos" if algo_ids else "range"),
        "algoIds": algo_ids,
        "steps": steps, "passed": sum(1 for item in steps if item["exitCode"] == 0),
        "skipped": skipped,
        "ok": not failed and not skipped, "exitCode": exit_code,
        "failedStep": failed_code, "failedStepExit": failed_raw,
        "python": sys.version.split()[0],
    }
    write_json(os.path.join(root, LOG_DIR, "运行日志.json"), run_doc)
    write_report(root, run_doc)
    if skipped:
        print(f"[00] 未完成（缺工具，非崩溃）：{run_doc['passed']}/{len(steps)} 步通过，"
              f"跳过 {', '.join(skipped)}；日志 过程文件/00-总控/运行日志.json；"
              f"报告 过程文件/00-总控/运行报告.md", flush=True)
    else:
        print(f"[00] {'通过' if not failed else '失败'}：{run_doc['passed']}/{len(steps)} 步；"
              f"日志 过程文件/00-总控/运行日志.json；报告 过程文件/00-总控/运行报告.md")
    return run_doc["exitCode"]


def cmd_status(args) -> int:
    root = os.path.abspath(args.workspace)
    if not os.path.isdir(root):
        print(f"[00] 工作区不存在：{root}", file=sys.stderr)
        return 2
    last = None
    log_path = os.path.join(root, LOG_DIR, "运行日志.json")
    if os.path.isfile(log_path):
        try:
            with open(log_path, encoding="utf-8") as fh:
                last = json.load(fh)
        except (OSError, ValueError):
            last = None
    print(f"工作区：<workspace>（{root}）")
    print(f"{'步':<4}{'名称':<14}{'产物':<44}{'状态'}")
    for code, name, _folder, _script, _product, _note in STEPS:
        files = products_of(code, root)
        exists = [os.path.isfile(path) for path in files]
        state = "全部就位" if all(exists) else ("部分缺失" if any(exists) else "未生成")
        rels = ", ".join(os.path.relpath(path, root).replace("\\", "/") for path in files)
        print(f"{code:<4}{name:<14}{rels[:42]:<44}{state}")
    if last:
        print(f"\n最近一次运行：{last.get('startedAt')} → {last.get('finishedAt')}，"
              f"{last.get('passed')}/{len(last.get('steps') or [])} 步通过，"
              f"结果 {'通过' if last.get('ok') else '失败'}")
        for item in last.get("steps") or []:
            print(f"  {item['code']} exit={item['exitCode']} {item['durationMs'] / 1000:.1f}s")
    else:
        print("\n最近一次运行：无记录（先执行 run）")
    return 0


def cmd_scaffold(args) -> int:
    root = os.path.abspath(args.workspace)
    target = os.path.abspath(args.target)
    if os.path.abspath(root) == target:
        print("[00] --target 不能是当前工作区", file=sys.stderr)
        return 2
    if not os.path.isdir(os.path.join(root, "Skills")):
        print(f"[00] 源工作区缺 Skills/：{root}", file=sys.stderr)
        return 2
    if os.path.exists(target) and not args.force:
        print(f"[00] 目标已存在：{target}（加 --force 覆盖）", file=sys.stderr)
        return 2
    if args.force and os.path.exists(target):
        shutil.rmtree(target, ignore_errors=True)

    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".work")
    shutil.copytree(os.path.join(root, "Skills"), os.path.join(target, "Skills"), ignore=ignore)
    for folder in ("输入文件", "过程文件", "成果文件", "记忆与规则"):
        os.makedirs(os.path.join(target, folder), exist_ok=True)
    shutil.copytree(os.path.join(root, INPUT_DIR), os.path.join(target, INPUT_DIR),
                    ignore=ignore, dirs_exist_ok=True)
    for name in ("README.md", "工作区参数.json", "requirements.txt"):
        src = os.path.join(root, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(target, name))
    note = os.path.join(target, "SCAFFOLD.md")
    with open(note, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join([
            "# 新工作区（scaffold 生成）", "",
            f"- 生成时间：{now_iso()}　来源：`<workspace>`",
            "- 已复制：`Skills/`（含 `02-工具探测/tools/` 自包含工具与 LICENSE）、`输入文件/01-模型包/`、"
            "`README.md`、`工作区参数.json`。",
            "- **未复制**：`过程文件/`、`成果文件/`、`记忆与规则/验证基线-CardoOSGB.json`（基线属于具体模型，"
            "新工作区首跑后应重新生成自己的基线）。",
            "", "## 怎么跑", "", "```bash",
            "python <技能目录>/Skills/00-Cardo低空解析总控/scripts/pipeline.py run --workspace .",
            "```", "",
            "换模型：把新模型 zip 放到 `输入文件/01-模型包/`（只放一个），或 `--input-zip <相对路径>`。", ""]))
    print(f"[00] scaffold 完成：{target}")
    print("[00] 下一步：把新模型 zip 放进 输入文件/01-模型包/ 后在该目录执行 run")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="00 · Cardo 低空解析总控（run / algos / status / scaffold）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="按序执行所选步骤（默认 01→08），每步过闸门，失败即停")
    run.add_argument("--workspace", default=".", help="工作区根（默认当前目录）")
    run.add_argument("--from", dest="from_step", default="01", choices=[c for c, *_ in STEPS])
    run.add_argument("--to", dest="to_step", default="14", choices=[c for c, *_ in STEPS],
                     help="跑到哪一步为止（默认 14 = 含 09–13 的几何反演与扩展指标汇总）")
    run.add_argument("--tools-dir", default=None, help="工具根覆盖（优先于自带 tools/）")
    run.add_argument("--input-zip", default=None, help="指定模型 zip（相对工作区根），传给 01")
    run.add_argument("--force", action="store_true", help="允许 01 覆盖已有解包目录")
    run.add_argument("--timeout", type=int, default=1800, help="单步超时秒数（默认 1800）")
    run.add_argument("--algos", default=None,
                     help="按算法选（逗号分隔的 id 或名称）：只跑这些算法所需的步骤，缺输入自动补跑前置步骤")
    run.add_argument("--select", nargs="?", const="__interactive__", default=None,
                     help="选算法：不给值=交互式编号选择；给值=编号/ id（逗号分隔）")
    run.add_argument("--skip-unavailable", action="store_true",
                     help="选中的算法里有不可用的（需影像/两期/待实现）时，跳过它们继续跑可用的")
    run.add_argument("--dry-run", action="store_true", help="只打印将执行的步骤，不实际执行")

    algos = sub.add_parser("algos", help="列算法清单：干什么 / 需要什么 / 产出 / 现在能不能跑")
    algos.add_argument("--workspace", default=".", help="工作区根（用来判断输入是否就位）")
    algos.add_argument("--all", action="store_true", help="连不可用项一起列（含不可用原因）")
    algos.add_argument("--status", default=None,
                       choices=["ready", "needs-imagery", "needs-2epoch", "needs-truth", "planned"],
                       help="只看某一类")
    algos.add_argument("--group", default=None,
                       help="只看某一组：ingest/check/metric/recon/vector/store/enhance/imagery/twoepoch/planned")

    status = sub.add_parser("status", help="看各步产物与最近一次运行")
    status.add_argument("--workspace", default=".")

    scaffold = sub.add_parser("scaffold", help="复制全链与输入到新工作区")
    scaffold.add_argument("--target", required=True, help="新工作区目录")
    scaffold.add_argument("--workspace", default=".", help="源工作区根（默认当前目录）")
    scaffold.add_argument("--force", action="store_true", help="目标存在时覆盖")

    args = ap.parse_args()
    if args.cmd == "run":
        return cmd_run(args)
    if args.cmd == "algos":
        return cmd_algos(args)
    if args.cmd == "status":
        return cmd_status(args)
    return cmd_scaffold(args)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
