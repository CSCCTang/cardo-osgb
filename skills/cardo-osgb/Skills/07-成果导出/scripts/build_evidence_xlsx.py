#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build_evidence_xlsx.py -- 《现状指标依据对照表》：每个指标 → 算法 / 参数 / 图层 / 局限。

现状指标.xlsx 回答「数是多少」，这张表回答「这个数是怎么算出来的」：
  指标名 / 值 / 区间 / 单位 / 等级 / 算法（method）/ 输入图层（source）/ 参数（params）/ 局限（limits）
并做八条自检（不自称精度，只查"写没写清、对不对得上"）：
  G-1 每个指标口径齐全：method / limits / grade / source 缺一即不通过
  G-2 区间包住主值：range[0] ≤ value ≤ range[1]
  G-3 占位不许装数：value 为 null 的指标必须有对应 gaps 缺项
  G-4 参数与口径一致：在 method / 参数里的网格、地面口径必须与 params 一致
  G-5 依据随包可回指：源 JSON 复制进成果包 依据数据/，依据路径相对该目录可落到条目
  G-6 成稿洁净：指标说明.docx 正文 0 处「来源：/（数据来源：…）」来源行
  G-7 正文数可回指：指标说明.docx 表格里的每个数都能在源 JSON 里找到。
      7.x 场地要素明细按对象绑定——行首编号命中对象 id 时，只拿这个对象
      在明细表里渲染的那几个字段做比对池；其余表仍用现状指标/扩展指标
  G-8 对照表一一对应：指标与对照表行数、ID 一一对上

用法（一般由 export_deliverables.py 调用）：
    python build_evidence_xlsx.py --ind 过程文件/06-2.5D网格指标/现状指标.json [--docx 成果文件/成品版/指标说明/指标说明_成品版.docx] --out 成果文件/内部件/现状指标/现状指标依据对照表.xlsx
退出码：0 通过；1 自检不通过；2 输入缺失。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys

sys.dont_write_bytecode = True     # 交付包里不留 __pycache__（08 的 E-04 会判失败）

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.properties import PageSetupProperties
from openpyxl.worksheet.pagebreak import Break

# 运行时是 python313._pth 模式，脚本目录不进 sys.path：兄弟模块必须显式加进来
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 列宽与「现状指标.xlsx」用同一套算法：表头/最长不可断词不许截断。
from export_deliverables import (PAGE_WIDTH_UNITS, body_font, fit_columns,  # noqa: E402
                                 wrap_all, xlsx_widths)

HEAD_FILL = PatternFill("solid", fgColor="1F3864")
HEAD_FONT = Font(color="FFFFFF", bold=True)
BAD_FILL = PatternFill("solid", fgColor="FCE4E4")
NAME = "现状指标依据对照表.xlsx"
# 字号底线 7pt：9pt 正文经 fitToWidth 缩放后不得低于 7pt。整页 205 单位时缩到
# 0.70（6.3pt，打出来看不清）；168 单位实测缩放 0.86 → 正文 7.7pt，留了余量。
EVIDENCE_WIDTH_UNITS = 168
# 自检页逐条列出的口径（与模块头部 G-1…G-8 同源；放在这里是为了导出时能写进表）
CHECKS = (
    ("G-1 口径齐全", "每个指标都有 method / limits / grade / source，缺一即不通过"),
    ("G-2 区间包住主值", "range[0] ≤ value ≤ range[1]"),
    ("G-3 占位不许装数", "value 为 null 的指标必须有对应 gaps 缺项"),
    ("G-4 参数与口径一致", "method 里写的网格、地面口径必须与 params 一致"),
    ("G-5 依据随包可回指", "内部件/现状指标/依据数据 下的源 JSON + 指标 id + 字段，能落到具体条目"),
    ("G-6 成稿洁净", "指标说明.docx 正文 0 处「来源：」来源行"),
    ("G-7 正文数可回指", "指标说明.docx 表格里的每个数都能在源 JSON 里找到"),
    ("G-8 一一对应", "指标与对照表行数、ID 一一对上（含内部诊断指标；成品版为其中 45 条）"),
)


SOURCE_PREFIX = ("来源：", "来源:", "（数据来源", "(数据来源", "数据来源：", "数据来源:")


def _sha256(path: str) -> str:
    """文件 SHA256（不随包的运行输入靠它留指纹）。读不到返回空串。"""
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return ""


def number_forms(obj, out=None):
    """把 JSON 里所有数值摊成「可能出现在正文里的写法」集合。"""
    out = set() if out is None else out
    if isinstance(obj, dict):
        for value in obj.values():
            number_forms(value, out)
    elif isinstance(obj, list):
        for value in obj:
            number_forms(value, out)
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        v = float(obj)
        out |= {f"{v:g}", f"{v:.1f}", f"{v:.2f}", f"{v:.0f}"}
        out |= {f"{v * 100:g}%", f"{v * 100:.1f}%", f"{v * 100:.2f}%"}
    return out


def site_ref_index(site, site_tables=None):
    """对象编号 → [(分组键, 分组标题, {字段: 值}, {列名: 字段})]。

    只收 07 正文 7.x 明细表实际渲染的字段：site_tables 由调用方传入（与
    export_deliverables.SITE_TABLES 同源）；没传时退化为该对象自带的数值字段。
    **不把整份 场地要素.json 摊进比对池**——那样任何一个数都能命中，闸门等于废了。
    """
    if not site:
        return {}
    if site_tables:
        groups = [(key, title, tuple(field for field, _ in cols),
                   {label: field for field, label in cols})
                  for key, title, cols in site_tables]
    else:
        groups = []
        for key, rows in site.items():
            if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
                continue
            if "id" not in rows[0]:
                continue
            fields = tuple(k for k, v in rows[0].items()
                           if isinstance(v, (int, float)) and not isinstance(v, bool))
            groups.append((key, key, fields, {f: f for f in fields}))
    index = {}
    for key, title, fields, labels in groups:
        for row in site.get(key) or []:
            oid = str(row.get("id") or "").strip()
            if not oid:
                continue
            index.setdefault(oid, []).append(
                (key, title, {f: row.get(f) for f in fields if f in row}, labels))
    return index


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _exact(text: str, value) -> bool:
    """写法精确命中（597 / 597.0 / 0.76 / 76% …）。"""
    return _is_number(value) and text in number_forms(value)


def _rounded(text: str, value) -> bool:
    """整数显示：允许四舍五入 ±0.5（小数与百分数必须精确，不放宽）。"""
    if not _is_number(value) or "." in text:
        return False
    target = float(value) * 100 if text.endswith("%") else float(value)
    return abs(float(text.rstrip("%")) - target) < 0.5


def _pick_site(text: str, label: str, bound):
    """在一个对象的字段里给这个数找来源，优先按列名对应的字段。

    列名优先是为了不错认：同一对象里「宽 30 / 长 30」两个字段值相同时，
    靠列名才能回指到正确的那个；整数 ±0.5 只在精确匹配都对不上时兜底。
    """
    for key, title, fields, labels in bound:
        field = labels.get(label)
        if field is not None and _exact(text, fields.get(field)):
            return key, title, field
    for key, title, fields, labels in bound:
        for field, value in fields.items():
            if _exact(text, value):
                return key, title, field
    for key, title, fields, labels in bound:
        for field, value in fields.items():
            if _rounded(text, value):
                return key, title, field
    return None


def clean_doc_checks(docx_path, known, site_index=None, bundle_rel=""):
    """G-6 正文 0 处来源行；G-7 表格里的数都能回指到源 JSON。

    比对池按表绑定：行首是场地要素对象编号（H-001/L-032/V-002/W-001 等）时，
    只拿这个对象在 7.x 明细表里渲染的那几个字段做比对，命中写入逐数明细，
    能回指到「哪个文件 · 哪个对象 id · 哪个字段」；其余表仍用现状指标/扩展指标的整体池。
    """
    problems, detail = [], []
    if not docx_path or not os.path.isfile(docx_path):
        return ([f"未找到指标说明 docx：{docx_path or '未给 --docx'}"
                 f"（无法核对正文洁净与数值回指）"], detail)
    from docx import Document
    doc = Document(docx_path)
    hits = [(p.text or "").strip()[:60] for p in doc.paragraphs
            if (p.text or "").strip().startswith(SOURCE_PREFIX)]
    if hits:
        problems.append(f"指标说明正文仍有 {len(hits)} 处来源行：{'；'.join(hits[:3])}")
    index = site_index or {}
    for ti, table in enumerate(doc.tables):
        if not table.rows:
            continue
        headers = [cell.text.strip() for cell in table.rows[0].cells]
        for line in table.rows:
            cells = [(cell.text or "").strip().replace(",", "") for cell in line.cells]
            if not cells:
                continue
            bound = index.get(cells[0])
            context = " | ".join(cells)[:60]
            for col, text in enumerate(cells[1:], start=1):
                if not re.fullmatch(r"-?\d+(?:\.\d+)?%?", text):
                    continue
                label = headers[col] if col < len(headers) else f"第{col + 1}列"
                if bound:
                    hit = _pick_site(text, label, bound)
                    if hit:
                        key, title, field = hit
                        detail.append([title, cells[0], label, text,
                                       f"{bundle_rel + '/' if bundle_rel else ''}"
                                       f"场地要素.json#{key}[{cells[0]}].{field}"])
                        continue
                    problems.append(
                        f"指标说明 表{ti + 1} 的 {text} 不在场地要素.json 中 {cells[0]} "
                        f"的明细字段里（无源数值）：{context}")
                    continue
                if text in known:
                    continue
                if "." not in text and any(abs(float(text) - float(k.rstrip("%"))) < 0.5
                                           for k in known if k.rstrip("%").replace(".", "", 1).isdigit()):
                    continue            # 整数显示：允许四舍五入到整数
                problems.append(f"指标说明 表{ti + 1} 的 {text} 不在现状指标/扩展指标里（无源数值）：{context}")
    return problems, detail


def _fmt(value):
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.6f}".rstrip("0").rstrip(".")
    return str(value)


def _range_text(rng) -> str:
    """区间文本：空区间只写一个「—」。

    旧版把两个空值连起来写成「— – —」，看着像两个并列的空值/漏填。
    """
    if not rng or len(rng) != 2 or rng[0] is None or rng[1] is None:
        return "—"
    return f"{_fmt(rng[0])} – {_fmt(rng[1])}"


def rebalance(ws, grow=(8, 9), budget: float = PAGE_WIDTH_UNITS,
              floors_override: list | None = None) -> float:
    """短列只取下限，剩下的预算全给 grow 那几列长文本。

    fit_columns 给每列按数据最宽算，长文本列一封顶、短列又各占一点，
    结果宽的地方被浪费、窄的地方拼命折行。这里反过来分配：短列拿到
    「表头 + 最长不可断词」的下限就够，余量按长文本列均分。

    floors_override：整表必须一页时，短列可以再收一点（表头/取值都放得下），
    省出来的横向预算全给算法/局限两列——行数少了，整表才压得进一页，
    字号也不会被 fitToHeight 压到 6pt 以下。
    """
    headers = [cell.value for cell in ws[1]]
    rows = [[cell for cell in row] for row in ws.iter_rows(min_row=2, values_only=True)]
    floors = xlsx_widths(headers, rows, cap=24)
    if floors_override and len(floors_override) == len(floors):
        floors = [keep if keep else floor for keep, floor in zip(floors_override, floors)]
    grow_idx = [index - 1 for index in grow]
    fixed = sum(width for index, width in enumerate(floors) if index not in grow_idx)
    if fixed >= budget:               # 短列下限就吃满预算：退回原算法，别把长文本挤没
        return fit_columns(ws, cap=26)
    share = (budget - fixed) / len(grow_idx)
    widths = [share if index in grow_idx else floors[index]
              for index in range(len(floors))]
    for index, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(index)].width = round(width, 1)
    return sum(widths)


def _print_setup(ws, fit_width: bool = True, title_cols: str = "") -> None:
    """打印/导 PDF：横向 A4、宽度一页、高度不限；列太多则不缩放、按列分页并重复指标列。"""
    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    if fit_width:
        ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
        ws.page_setup.fitToWidth = 1
        ws.page_setup.fitToHeight = 0
    else:
        ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=False)
        ws.page_setup.fitToWidth = None
        ws.page_setup.fitToHeight = None
        ws.page_setup.scale = 100
    ws.print_options.horizontalCentered = True
    ws.print_title_rows = "1:1"
    if title_cols:
        ws.print_title_cols = title_cols
    ws.page_margins.left = ws.page_margins.right = 0.2
    ws.page_margins.top = ws.page_margins.bottom = 0.25
    ws.page_margins.header = ws.page_margins.footer = 0.15


def build(doc: dict, base_dir: str, ext: dict | None = None,
          project: str = "", bundle_dir: str = "",
          bundle_rel: str = "") -> tuple[list, list, list]:
    """返回 (rows, detail, problems)。base_dir 用于解析 source[].ref 相对路径。

    指标集合 = 现状指标.json 的指标 + 扩展指标.json 的指标（去重）。
    旧版只吃前者（7 条），附表看着"一一对应"，实际漏了 38 条扩展指标。
    依据写成「随包文件 + 指标 id + 字段」：给了 bundle_dir 时，源 JSON 先复制到
    成果包内的 依据数据/，依据路径也改成相对该目录的写法——单拷 成果文件/
    仍然指得到，不再挂在运行目录的 过程文件/ 上。
    """
    root = project or os.path.dirname(os.path.dirname(os.path.abspath(base_dir)))
    if bundle_rel:
        core_ref = f"{bundle_rel}/现状指标.json"
        ext_ref = f"{bundle_rel}/扩展指标.json"
        core_path = os.path.join(bundle_dir or root, "现状指标.json")
        ext_path = os.path.join(bundle_dir or root, "扩展指标.json")
    else:
        core_ref = "过程文件/06-2.5D网格指标/现状指标.json"
        ext_ref = "过程文件/14-扩展指标/扩展指标.json"
        core_path = os.path.join(root, *core_ref.split("/"))
        ext_path = os.path.join(root, *ext_ref.split("/"))
    rows, detail, problems = [], [], []
    params = doc.get("params") or {}
    gaps = doc.get("gaps") or []
    core_metrics = list(doc.get("metrics") or [])
    metrics = list(core_metrics)
    seen = {str(m.get("id", "")) for m in metrics}
    ext_metrics = []
    for metric in (ext or {}).get("metrics") or []:
        if str(metric.get("id", "")) not in seen:
            ext_metrics.append(metric)
            seen.add(str(metric.get("id", "")))
    metrics += ext_metrics
    ext_ids = {str(m.get("id", "")) for m in ext_metrics}
    for metric in metrics:
        mid, name = metric.get("id", ""), metric.get("name", "")
        src_json = ext_ref if mid in ext_ids else core_ref
        value, rng = metric.get("value"), metric.get("range") or [None, None]
        layers = "、".join(metric.get("source") or []) or "—"
        bad = []
        # G-1 口径齐全
        missing = [key for key in ("method", "limits", "grade", "source")
                   if not metric.get(key)]
        if missing:
            bad.append("缺口径：" + "、".join(missing))
        # G-2 区间包主值
        if value is not None and None not in rng:
            if not (float(rng[0]) - 1e-12 <= float(value) <= float(rng[1]) + 1e-12):
                bad.append(f"主值 {_fmt(value)} 不在区间 {_fmt(rng[0])}–{_fmt(rng[1])} 内")
        # G-3 占位必须有缺项说明（有 gaps 即合规，不因存在占位就判失败）
        if value is None and not gaps:
            bad.append("占位值但 gaps 为空：没写清缺什么")
        rows.append([name, mid, _fmt(value), _range_text(rng),
                     metric.get("unit", "—"), metric.get("grade", "—"),
                     layers, metric.get("method", "—"), metric.get("limits", "—"),
                     "通过" if not bad else "；".join(bad)])
        detail.append([name, mid, "值", _fmt(value), f"{src_json} · metrics[{mid}].value"])
        detail.append([name, mid, "下限", _fmt(rng[0]), f"{src_json} · metrics[{mid}].range[0]"])
        detail.append([name, mid, "上限", _fmt(rng[1]), f"{src_json} · metrics[{mid}].range[1]"])
        if metric.get("grade") == "placeholder" and value is None:
            continue
        if any("不在区间" in item or "缺口径" in item for item in bad):
            problems.append(f"{name}（{mid}）：" + "；".join(bad))

    # G-4 参数与口径一致
    cells = params.get("rangeCells") or []
    if cells and params.get("cellM") is not None and \
            not any(abs(float(params["cellM"]) - float(c)) < 1e-9 for c in cells):
        problems.append(f"主网格 {params['cellM']} m 不在敏感性三档 {cells} 里")
    if params.get("localGroundPercentile") is None and params.get("groundMode") == "local_neighborhood_p10":
        problems.append("地面口径写的是邻域 P10，但参数里没有 localGroundPercentile")
    if params.get("groundMode") == "local_neighborhood_p10" and params.get("localGroundRadiusM") is None:
        problems.append("地面口径是邻域 P10，但没给邻域半径 localGroundRadiusM")
    if not params:
        problems.append("现状指标.json 没有 params：无法说明参数口径")

    # G-5 输入来源可回指：依据必须随包可达（给了 bundle_dir 就查包内副本）
    refs = [(core_ref, core_path)] + ([(ext_ref, ext_path)] if ext else [])
    for rel, path in refs:
        exists = os.path.isfile(path)
        detail.append(["输入来源", "指标源", "文件", rel,
                       f"{rel}#metrics[]（{'随包可达' if exists else '缺失'}）"])
        if not exists:
            problems.append(f"依据文件未随包：{rel}")
    for src in doc.get("source") or []:
        ref = src.get("ref") or ""
        if not ref:
            problems.append(f"source 缺 ref（kind={src.get('kind')}）")
            continue
        # 这两条是上游模型/瓦片的运行期输入，不是逐数依据：只登记，不要求随包。
        # 既然不随包，就不能再写「运行时可回指」这种只有本次运行目录才成立的话；
        # 如实给「文件名 + sha256」，复核者拿不到原件也能确认是哪一份。
        path = ref if os.path.isabs(ref) else os.path.normpath(os.path.join(base_dir, ref))
        name = os.path.basename(ref.replace("\\", "/"))
        digest = _sha256(path)
        label = f"{name}（运行输入，不随包" + (f"，sha256 {digest[:16]}…" if digest else "") + "）"
        detail.append(["运行输入", src.get("kind", ""), "文件", label,
                       "现状指标.json#source（运行输入，不随包）"])
    return rows, detail, problems


def _common_suffix(texts: list[str]) -> str:
    """所有行结尾完全相同的部分（从末尾往前比）。"""
    if not texts:
        return ""
    first = texts[0]
    size = len(first)
    for other in texts[1:]:
        size = min(size, len(other))
        i = 0
        while i < size and first[-1 - i] == other[-1 - i]:
            i += 1
        size = i
    return first[-size:] if size else ""


def dedupe_common_limits(rows: list, col: int = 8):
    """把每行重复的同一段「共性局限」抽到表下写一次，返回 (新 rows, 表注文字)。

    7 条指标的局限里挂着同一段「真值状态 / 表面口径 / LOD 混合 / 顶点越界 /
    解析失败」，逐行重复 7 遍，把 8 行的表顶成 2 页（第 2 页只剩两行）。
    这段文字对所有行都一字不差，抽出来在表下注一次：字数一个不少，
    行高按各自特有的局限算，整表落回一页。
    """
    if len(rows) < 3:
        return rows, ""
    texts = [str(row[col] or "") for row in rows]
    raw = _common_suffix(texts)
    if not raw:
        return rows, ""
    if raw[0] in "。；，:： ":
        tail = raw.lstrip("。；，:： ")
    else:
        # 共性尾不是从句子边界开始：说明多咬住了半句，从第一个句末标点之后算起
        cut = [i for i in (raw.find("。"), raw.find("；")) if i >= 0]
        tail = raw[min(cut) + 1:] if cut else ""
    if len(tail) < 40:
        return rows, ""
    kept_rows = []
    for row in rows:
        text = str(row[col] or "")
        kept = text[:len(text) - len(tail)].rstrip("。；")
        new_row = list(row)
        new_row[col] = (kept + "。共同局限见表下注。") if kept else "见表下注。"
        kept_rows.append(new_row)
    return kept_rows, tail


_NOTE_MARK = "①②③④⑤⑥⑦⑧⑨"


def _long_segments(text: str) -> list[str]:
    """按「；」切句，只留够长的（短句抽出去反而更难读）。"""
    return [seg.strip() for seg in str(text or "").split("；") if len(seg.strip()) >= 40]


def dedupe_shared_phrases(rows: list, col: int, min_rows: int = 3):
    """把 ≥3 行一字不差出现的长句抽到表注，行内改成「见注①」。

    建筑密度/栋数/P50/P90 四条的「区间 = cell 0.5 / 1.0 / 2.0 m 三档网格 ×
    地面口径（…）的 min–max（每次运行都跑三档…）」是完全相同的一句，逐行重复
    把算法列顶到十几行。抽到表注写一次，行里只留「见注①」——话没少，只是
    不重复说四遍。
    """
    texts = [str(row[col] or "") for row in rows]
    candidates = []
    for text in texts:
        for seg in _long_segments(text):
            seg = seg.rstrip("。")
            if seg and seg not in candidates:
                candidates.append(seg)
    picked = []
    for seg in sorted(candidates, key=len, reverse=True):
        hits = [i + 1 for i, text in enumerate(texts) if seg in text]
        if len(hits) >= min_rows and not any(seg in item["text"] for item in picked):
            picked.append({"text": seg, "rows": hits})
    if not picked:
        return rows, []
    new_rows = []
    for row in rows:
        row = list(row)
        text = str(row[col] or "")
        for index, item in enumerate(picked):
            if item["text"] in text:
                text = text.replace(item["text"], f"见注{_NOTE_MARK[index % len(_NOTE_MARK)]}", 1)
        row[col] = text
        new_rows.append(row)
    return new_rows, picked


def write(doc: dict, base_dir: str, out_path: str,
          docx_path: str = "", project_dir: str = "",
          site: dict | None = None, site_tables=None,
          ext: dict | None = None, bundle_dir: str = "",
          bundle_rel: str = "") -> tuple[int, list, int]:
    project = project_dir or os.path.dirname(os.path.dirname(os.path.abspath(base_dir)))
    ext_path = os.path.join(project, "过程文件", "14-扩展指标", "扩展指标.json")
    if ext is None and os.path.isfile(ext_path):
        with open(ext_path, encoding="utf-8-sig") as fh:
            ext = json.load(fh)
    rows, detail, problems = build(doc, base_dir, ext, project, bundle_dir, bundle_rel)
    # G-6/G-7：成稿洁净 + 正文数可回指（"正文"= 指标说明.docx，数都要能在源 JSON 找到）
    known = number_forms(doc)
    if ext:
        known |= number_forms(ext)
    doc_problems, doc_detail = clean_doc_checks(
        docx_path or os.path.join(project, "成果文件", "成品版", "指标说明",
                                  "指标说明_成品版.docx"), known,
        site_ref_index(site, site_tables), bundle_rel)
    problems += doc_problems
    detail += doc_detail
    # G-8：指标与对照表行一一对应（按「现状指标 + 扩展指标」合并后的全集核对）
    ids = [str(m.get("id", "")) for m in doc.get("metrics") or []]
    ids += [str(m.get("id", "")) for m in (ext or {}).get("metrics") or []
            if str(m.get("id", "")) not in set(ids)]
    row_ids = [row[1] for row in rows]
    if len(ids) != len(set(ids)) or row_ids != ids:
        problems.append(f"对照表行与指标不一一对应：指标 {len(ids)} 条 / 对照表 {len(rows)} 行")
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    # 7 条指标的局限里挂着同一段共性文字（真值状态/口径/LOD/顶点/解析失败），
    # 逐行重复把 8 行的表顶成 2 页。抽到表下注一次，一个字不少，整表回到一页。
    rows, common_limits = dedupe_common_limits(rows)
    # 算法列里「区间 = cell 0.5 / 1.0 / 2.0 m 三档网格 × 地面口径……」这句在
    # 4 条指标里一字不差，逐行重复把算法列各撑高两三行；抽到表注写一次，
    # 行内留「见注①」。行高是整表能否压进一页的真正约束，削掉重复句
    # 才让宽度（而不是高度）成为唯一缩放约束，字号不再卡在 6pt 临界上抖。
    rows, note_phrases = dedupe_shared_phrases(rows, 7)
    wb = Workbook()
    ws = wb.active
    ws.title = "指标依据"
    headers = ["指标", "指标ID", "值", "区间", "单位", "等级", "输入图层", "算法（口径）", "局限", "自检"]
    ws.append(headers)
    for cell in ws[1]:
        cell.fill, cell.font = HEAD_FILL, HEAD_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center")
    for row in rows:
        ws.append(row)
        if row[-1] != "通过":
            for cell in ws[ws.max_row]:
                cell.fill = BAD_FILL
    if common_limits or note_phrases:
        # 表注只占一行高：算法列放注①（区间口径），局限列放共性局限。
        # 局限那格本来就比注①长得多，注①并进去不会抬高这一行。
        note = ["表注（以上全部指标适用）"] + [""] * (len(headers) - 1)
        if note_phrases:
            note[7] = "；".join(f"注{_NOTE_MARK[i]} {item['text']}"
                               for i, item in enumerate(note_phrases))
        if common_limits:
            note[8] = common_limits
        ws.append(note)
        for cell in ws[ws.max_row]:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    # 列宽按「短列只取下限、余下的全给长文本列（算法口径/局限）」重新分配。
    # 宽度预算收到 EVIDENCE_WIDTH_UNITS（不是整页预算）：整页 205 单位时
    # Excel 按宽度缩到 70%，9pt 正文落到 6.3pt——A4 打出来看不清。7pt 是底线，
    # 缩放不得低于 7/9≈0.78，所以总宽最多 176 单位；放不进一页就分两页，
    # 靠「表头 + 前 4 条 / 后 3 条 + 表注」整段分（见下方手动分页），不靠缩字。
    total = rebalance(ws, grow=(8, 9), budget=EVIDENCE_WIDTH_UNITS,
                      floors_override=[16, 20, 9, 12, 6, 9, 30, None, None, 5])
    wrap_all(ws)
    body_font(ws, 9)
    ws.freeze_panes = "A2"

    ws2 = wb.create_sheet("逐数明细")
    ws2.append(["指标", "指标ID", "项", "数值", "依据"])
    for cell in ws2[1]:
        cell.fill, cell.font = HEAD_FILL, HEAD_FONT
    for row in detail:
        ws2.append(row)
    fit_columns(ws2, cap=42)
    wrap_all(ws2)

    # 自检页：无问题时也把 G-1…G-8 逐条列出来（一行「无」的纸太像碎片页，
    # 也看不出到底查了什么）；有问题时逐条列问题。
    # 自检页：每条给「通过 n/n」的实算覆盖数，不写没有分母的「通过」。
    # 旧版只报"一一对应、通过"，实际只核了 7 条、漏了 38 条扩展指标——自检放水比不查更坏。
    ws3 = wb.create_sheet("自检")
    ws3.append(["检查项", "结论", "口径"])
    for cell in ws3[1]:
        cell.fill, cell.font = HEAD_FILL, HEAD_FONT
    total_rows = len(rows)
    g1_ok = sum(1 for row in rows if "缺口径" not in str(row[9]))
    g2_app = [row for row in rows if str(row[3]) not in ("—", "", "None")]
    g2_ok = sum(1 for row in g2_app if "不在区间" not in str(row[9]))
    g3_app = [row for row in rows if str(row[2]) in ("—", "", "None")]
    g3_ok = sum(1 for row in g3_app if "占位" not in str(row[9]))
    param_bad = sum(1 for item in problems if item.startswith(("主网格", "地面口径", "现状指标.json 没有 params")))
    src_rows = [item for item in detail if item[0] == "输入来源"]
    src_ok = sum(1 for item in src_rows if "随包可达" in str(item[4]))

    def _reachable(item) -> bool:
        if item[0] == "运行输入":
            return False
        ref = re.split(r"[#·]", str(item[4] or ""))[0].strip()
        if not ref:
            return False
        if bundle_rel and ref.startswith(bundle_rel + "/"):
            return os.path.isfile(os.path.join(bundle_dir, ref[len(bundle_rel) + 1:]))
        if os.path.isabs(ref):
            return os.path.isfile(ref)
        return os.path.isfile(os.path.join(project, *ref.split("/")))

    # G-5 按逐数明细全量计：分母是全部依据行（含 2 条运行输入），不是文件数。
    # 旧版写「通过 2/2」——那是两个指标 JSON，不是 400 条依据，属报小分母。
    g5_total = len(detail)
    g5_ok = sum(1 for item in detail if _reachable(item))
    g5_skip = g5_total - g5_ok
    g5_text = (f"通过 {g5_ok}/{g5_total}"
               + (f"（{g5_skip} 条为运行输入，不随包）" if g5_skip else ""))
    doc_bad = len(doc_problems)
    row_ok = 1 if row_ids == ids else 0
    stats = [
        ("G-1 口径齐全", f"通过 {g1_ok}/{total_rows}", CHECKS[0][1]),
        ("G-2 区间包住主值", (f"通过 {g2_ok}/{len(g2_app)}（有区间的指标）"
                          if g2_app else "不适用（无指标给区间）"), CHECKS[1][1]),
        ("G-3 占位不许装数", (f"通过 {g3_ok}/{len(g3_app)}（占位指标）"
                          if g3_app else "不适用（无占位指标）"), CHECKS[2][1]),
        ("G-4 参数与口径一致", "通过" if param_bad == 0 else f"不通过 {param_bad} 项", CHECKS[3][1]),
        ("G-5 依据随包可回指", g5_text, CHECKS[4][1]),
        ("G-6/G-7 成稿洁净与数值回指", "通过" if doc_bad == 0 else f"不通过 {doc_bad} 项",
         CHECKS[5][1] + "；" + CHECKS[6][1]),
        ("G-8 指标与对照表一一对应", f"通过 {len(row_ids)}/{len(ids)}" if row_ok else
         f"不通过：指标 {len(ids)} 条 / 对照表 {len(row_ids)} 行", CHECKS[7][1]),
    ]
    for name, result, rule in stats:
        ws3.append([name, result, rule])
    if problems:
        ws3.append([])
        ws3.append(["未通过明细", "", ""])
        for problem in problems:
            ws3.append(["未通过项", "不通过", problem])
    fit_columns(ws3, cap=100)
    wrap_all(ws3)
    # 导 PDF：三张表都先试一页宽；只有实在放不下（列宽总和超预算）才按列自然分页
    _print_setup(ws, fit_width=total <= PAGE_WIDTH_UNITS,
                 title_cols="A:B" if total > PAGE_WIDTH_UNITS else "")
    # 「指标依据」字号 ≥7pt（宽度已按 168 单位收好），一页装不下就整段分两页：
    # 第 1 页 = 表头 + 前 5 条指标，第 2 页 = 后 2 条 + 表注（表头由 print_title_rows
    # 自动重复）。分页点落在两条指标之间、不落在表末，两页填充实测都是 51%；
    # 绝不再用 fitToHeight=1 把字号压到 6pt 去凑一页。
    data_rows = ws.max_row - 1          # 不含表头
    if data_rows > 5:
        ws.row_breaks.append(Break(id=1 + (data_rows + 2) // 2))
    _print_setup(ws2, fit_width=True)
    _print_setup(ws3, fit_width=True)
    wb.save(out_path)
    return (0 if not problems else 1), problems, len(rows)


def main(argv=None):
    ap = argparse.ArgumentParser(description="OSGB 指标依据对照表")
    ap.add_argument("--ind", required=True, help="现状指标.json")
    ap.add_argument("--out", default=os.path.join("成果文件", NAME))
    ap.add_argument("--docx", default="", help="指标说明.docx（缺省按项目目录推）")
    ap.add_argument("--site", default="",
                    help="场地要素.json（给了才做 7.x 明细表的对象级回指）")
    args = ap.parse_args(argv)
    if not os.path.isfile(args.ind):
        print(f"[OSGB-EVID] 找不到现状指标：{args.ind}（先跑 06）", file=sys.stderr)
        return 2
    with open(args.ind, encoding="utf-8-sig") as f:
        doc = json.load(f)
    site = None
    if args.site and os.path.isfile(args.site):
        with open(args.site, encoding="utf-8-sig") as f:
            site = json.load(f)
    site_tables = None
    if site:
        try:    # 列名 → 字段的映射与 07 正文明细表同源，避免这里另抄一份
            from export_deliverables import SITE_TABLES as site_tables
        except ImportError:
            site_tables = None
    code, problems, total = write(doc, os.path.dirname(os.path.abspath(args.ind)), args.out,
                                  docx_path=args.docx, site=site, site_tables=site_tables)
    print(f"[17] 指标依据对照表 → {args.out}（{total} 条指标，"
          f"{'通过' if code == 0 else f'不通过，共 {len(problems)} 条问题'}）")
    for problem in problems[:12]:
        print("   -", problem)
    if len(problems) > 12:
        print(f"   …… 其余 {len(problems) - 12} 条见对照表「未通过」sheet")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
