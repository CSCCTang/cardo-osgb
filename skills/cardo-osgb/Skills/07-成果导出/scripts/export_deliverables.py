#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""07 · 成果导出：把过程 JSON 转成人读成果（不新增任何数值）。

输入：过程文件/03-OSGB转3DTiles/转换产物/cardo-lowair-model.json
      过程文件/05-模型体检/体检.json
      过程文件/06-2.5D网格指标/现状指标.json
      过程文件/14-扩展指标/扩展指标.json（可选：09–13 已落地算法的产出）
输出（默认 成果文件/，按《Cardo四包通用规范》一成果一目录、两份主文件同源）：
      模型体检报告/模型体检报告_成品版.docx / _溯源版.docx
      现状指标/现状指标_成品版.xlsx / _溯源版.xlsx / 现状指标依据对照表.xlsx（必要附表）
      指标说明/指标说明_成品版.docx / _溯源版.docx
      · 成品版：交付业主的定稿，正文干净——无溯源标注、缺口提示、内部备注、判断过程
      · 溯源版：保留口径原文/局限原文/字段级证据/缺口编号，供内部复核对账
      两份由同一次运行、同一份 JSON 产出；导出后自校验数值完全一致。
      成品版词表闸门：命中「待补/暂缺/未配置/skipped/见过程文件/原始 <数字>」等即 exit 3。
闸门：导出后**回读**核对——xlsx 数值与 JSON 逐项一致（≤1e-9），docx 关键数字字符串可检索；
      任一不符即 exit 3（防"生成成功但内容不是那份 JSON"）。
      再把四件逐一渲成 PDF 逐页数字符/图片：出现「0 字符 0 图」的空白页、
      或任何一页最小字号低于 7pt，同样 exit 3。
退出码：0 通过；2 输入缺失；3 回读不一致/写盘失败/空白页/字号过小。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from datetime import datetime

sys.dont_write_bytecode = True

from docx import Document            # noqa: E402
from docx.shared import Pt, Cm, RGBColor       # noqa: E402
from docx.enum.table import WD_TABLE_ALIGNMENT   # noqa: E402
from docx.enum.text import (WD_BREAK, WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT,
                            WD_TAB_LEADER)  # noqa: E402
from docx.oxml import OxmlElement    # noqa: E402
from docx.oxml.ns import qn         # noqa: E402
from openpyxl import Workbook, load_workbook   # noqa: E402
from openpyxl.styles import Font, PatternFill, Alignment   # noqa: E402
from openpyxl.utils import get_column_letter     # noqa: E402
from openpyxl.worksheet.properties import PageSetupProperties   # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import readability as R              # noqa: E402  术语/白话/单位中文化词典

MODEL_DEFAULT = os.path.join("过程文件", "03-OSGB转3DTiles", "转换产物", "cardo-lowair-model.json")
HEALTH_DEFAULT = os.path.join("过程文件", "05-模型体检", "体检.json")
IND_DEFAULT = os.path.join("过程文件", "06-2.5D网格指标", "现状指标.json")
EXT_DEFAULT = os.path.join("过程文件", "14-扩展指标", "扩展指标.json")
MATRIX_DEFAULT = os.path.join("过程文件", "14-扩展指标", "可观测类型矩阵.json")
SITE_DEFAULT = os.path.join("过程文件", "16-场地要素", "场地要素.json")
OUT_DEFAULT = os.path.join("成果文件")
CONVERT_LOG_DEFAULT = os.path.join("过程文件", "03-OSGB转3DTiles", "转换日志.json")
# 内外分存、一成果一目录、两份主文件同源（见《Cardo四包通用规范》第 2 节）
DOC_HEALTH, DOC_IND, BOOK = "模型体检报告", "指标说明", "现状指标"
EVIDENCE_BOOK = "现状指标依据对照表.xlsx"
VARIANT_CLEAN, VARIANT_ANNOTATED = "成品版", "溯源版"
PUBLIC_ROOT, INTERNAL_ROOT = "成品版", "内部件"
REL_HEALTH_CLEAN = os.path.join(PUBLIC_ROOT, DOC_HEALTH, f"{DOC_HEALTH}_{VARIANT_CLEAN}.docx")
REL_HEALTH_ANN = os.path.join(INTERNAL_ROOT, DOC_HEALTH, f"{DOC_HEALTH}_{VARIANT_ANNOTATED}.docx")
REL_IND_CLEAN = os.path.join(PUBLIC_ROOT, DOC_IND, f"{DOC_IND}_{VARIANT_CLEAN}.docx")
REL_IND_ANN = os.path.join(INTERNAL_ROOT, DOC_IND, f"{DOC_IND}_{VARIANT_ANNOTATED}.docx")
REL_BOOK_CLEAN = os.path.join(PUBLIC_ROOT, BOOK, f"{BOOK}_{VARIANT_CLEAN}.xlsx")
REL_BOOK_ANN = os.path.join(INTERNAL_ROOT, BOOK, f"{BOOK}_{VARIANT_ANNOTATED}.xlsx")
REL_EVIDENCE = os.path.join(INTERNAL_ROOT, BOOK, EVIDENCE_BOOK)
# 成品 PDF：页码与目录页码由 07 自己渲染分页后烘进版面（不依赖 Word 域更新）
REL_HEALTH_PDF = os.path.join(PUBLIC_ROOT, DOC_HEALTH, f"{DOC_HEALTH}_{VARIANT_CLEAN}.pdf")
REL_IND_PDF = os.path.join(PUBLIC_ROOT, DOC_IND, f"{DOC_IND}_{VARIANT_CLEAN}.pdf")
REL_TARGETS = [REL_HEALTH_CLEAN, REL_HEALTH_ANN, REL_IND_CLEAN, REL_IND_ANN,
               REL_BOOK_CLEAN, REL_BOOK_ANN, REL_EVIDENCE,
               REL_HEALTH_PDF, REL_IND_PDF]
# 扩展指标的产出步骤 → 可交付的业务名称（成品版标题用，不写「09 步」）
STEP_NAMES = {"09": "建筑三维重建", "10": "矢量提取与入库", "11": "地表与植被分类",
              "12": "屋面与立面", "13": "轮廓矢量化", "15": "场地要素提取"}
# A4 横向一页约 140 个列宽单位（11pt）。总和在这个预算内，fitToWidth=1 打印时
# 缩放约 70%，字号仍不低于 8pt；超了就按列自然分页——再压就成竖排小字。
PAGE_WIDTH_UNITS = 205
VERDICT_CN = {"pass": "通过", "warn": "可用（有注意项）", "fail": "不通过"}
# 本版不产出的指标（占位）在表里的统一写法：**不留空单元格**。
# 空栏会被下游当成"漏填/还在算"，写成一句话才说明"这里本来就没有这个信息"。
NOT_IN_MODEL = "—"
# 第 7 章四张明细表：列名 → JSON 字段（旧版直接拿中文列名去取字段，
# 取不到就写空串，于是面积/宽/长/均高四列全空——验收渲染图 p025/p026 就是这么来的）
SITE_TABLES = (
    ("hardPatches", "7.1 硬地斑块",
     (("id", "编号"), ("areaM2", "面积 m²"), ("widthM", "宽 m"),
      ("lengthM", "长 m"), ("meanSlopeDeg", "平均坡度°"))),
    ("lowObjects", "7.2 低矮体量（疑似搭建）",
     (("id", "编号"), ("areaM2", "面积 m²"), ("heightMeanM", "均高 m"),
      ("heightMaxM", "最高 m"), ("nearBuildingM", "距建筑 m"))),
    ("voidPatches", "7.3 空地斑块（待核）",
     (("id", "编号"), ("areaM2", "面积 m²"), ("widthM", "宽 m"),
      ("lengthM", "长 m"), ("meanSlopeDeg", "平均坡度°"))),
    ("depressions", "7.4 低洼/积水线索",
     (("id", "编号"), ("areaM2", "面积 m²"), ("depthMeanM", "均深 m"),
      ("depthMaxM", "最深 m"))),
)


def print_setup(ws, fit_width: bool = True, title_cols: str = "") -> None:
    """打印/导 PDF 的页面设置（旧版一个都没设）。

    旧版症状：宽表按 100% 分页，导成 PDF 后每页只剩一两列，字竖着排、
    整页大片空白（验收渲染图 现状指标/p002 就是这样）。现在统一：
    横向 A4、宽度收到一页、**高度不限**（fitToHeight=0，按行自然分页）、
    每页重复表头；列太多（fit_width=False）时不缩放，改为自然横向分页，
    并把指标名那几列设为重复列，翻页也知道这一行是哪条。
    """
    ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    if fit_width:
        ws.page_setup.fitToWidth = 1
        ws.page_setup.fitToHeight = 0
    else:
        # 列宽总和远超一页时强行缩到 1 页会把字号压到 3–4pt；
        # 这里关掉缩放、按列自然分页，靠重复列保持可读。
        ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=False)
        ws.page_setup.fitToWidth = None
        ws.page_setup.fitToHeight = None
        ws.page_setup.scale = 100
    ws.print_options.horizontalCentered = True
    ws.print_title_rows = "1:1"
    if title_cols:
        ws.print_title_cols = title_cols
    ws.page_margins.left = ws.page_margins.right = 0.3
    # 上下边距 0.5 → 0.3：60 行的「技术口径」在 0.5 时第 5 页只剩最后 3 行
    # （整页 13% 有字、其余全白，就是复查说的碎片页）。收紧 0.2 in 换来约 5%
    # 的竖向容量，每个横向带正好少一页，残页消失；再紧就会顶到页眉页脚。
    ws.page_margins.top = ws.page_margins.bottom = 0.3
    ws.page_margins.header = ws.page_margins.footer = 0.15


def read_json(path: str) -> dict:
    with open(path, encoding="utf-8-sig") as fh:
        return json.load(fh)


# 项目名由工作区参数给定（业主口径），代码里不写任何具体项目/片区名。
_PROJECT_NAME = ""


def _workspace_root(out_dir: str, explicit: str = "") -> str:
    """定位工作区根：优先 --workspace，其次从 --out 向上找 工作区参数.json，不依赖 cwd。"""
    if explicit:
        return os.path.abspath(explicit)
    probe = os.path.abspath(out_dir)
    for _ in range(6):
        if os.path.isfile(os.path.join(probe, "工作区参数.json")):
            return probe
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    return os.path.dirname(os.path.abspath(out_dir))


def config_project_name(workspace: str) -> str:
    """读工作区参数 projectName；没有就返回空串，由调用处退回模型清单里的名字。"""
    path = os.path.join(_workspace_root(workspace), "工作区参数.json")
    if not os.path.isfile(path):
        return ""
    try:
        return str(read_json(path).get("projectName") or "").strip()
    except (OSError, ValueError):
        return ""


def fmt(value) -> str:
    """统一数值文本（与 docx/xlsx 写入同源，回读核对用同一函数）。"""
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        text = f"{value:.6f}".rstrip("0").rstrip(".")
        return text or "0"
    return str(value)


def rng_text(rng) -> str:
    if not rng or len(rng) != 2 or rng[0] is None or rng[1] is None:
        return "—（不提供）"
    return f"{fmt(rng[0])} – {fmt(rng[1])}"


# Markdown 强调/标题标记：xlsx 单元格与 docx 表格单元格不做富文本，必须先剥掉，
# 否则 `**`、`##`、`__` 会原样留在会上要看的成果里（曾出现 98 处）。
_MD_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
_MD_HASH = re.compile(r"#{2,6}(?=\s)")
_MD_UNDER = re.compile(r"__([^_\n]+)__")
_MD_RESIDUE = re.compile(r"\*\*|^\s{0,3}#{1,6}\s|#{2,6}(?=\s)|__[^_\n]+__", re.M)


def strip_markdown(text, keep_bold: bool = False):
    """剥掉 Markdown 标记；keep_bold=True 时保留 `**` 交给 para() 转真粗体。"""
    if not isinstance(text, str):
        return text
    out = text if keep_bold else text.replace("**", "")
    out = _MD_HEADING.sub("", out)
    out = _MD_HASH.sub("", out)
    out = _MD_UNDER.sub(r"\1", out)
    return out


def _term_keys(term: str) -> list:
    """术语匹配键：主体词 + 括号内释义 + 并列写法。

    正文常写「外接矩形」而不写「OBB（外接矩形）」，旧实现只取括号前的主体词，
    于是正文用了的词在术语表里查不到。这里把括号内写法一并当作键。
    """
    keys = []
    for part in re.split(r"\s*/\s*", term):
        head = re.split(r"[（(]", part, maxsplit=1)[0].strip()
        if len(head) >= 2:
            keys.append(head)
    for inner in re.findall(r"[（(]([^）)]+)[）)]", term):
        for part in re.split(r"\s*/\s*", inner):
            part = part.strip()
            if len(part) >= 2:
                keys.append(part)
    return keys


def _term_key(term: str) -> str:
    """术语主键（第一个匹配键），保留给旧调用。"""
    keys = _term_keys(term)
    return keys[0] if keys else ""


# 成品版术语表不收录的内部字段名与缺口说法：这些词只在实现与内部核对里出现，
# 业主版正文不使用；若留在术语表，等于把内部口径直接摊给业主。
_CLEAN_TERM_BLOCK = {"mask 格", "bboxLocal", "NORMAL.z ≥ 0.5", "ceil", "真值对照"}


def _terms_for(text: str, annotated: bool) -> list:
    """术语表条目：溯源版给全量；成品版只给正文真正用到、且不是内部字段的词。"""
    if annotated:
        return list(R.TERMS)
    picked = []
    for term, meaning in R.TERMS:
        if term in _CLEAN_TERM_BLOCK:
            continue
        keys = _term_keys(term)
        if not keys:
            continue
        if any(k in text for k in keys):
            picked.append((term, meaning))
            continue
        # 中文复合词按前缀命中（正文写「顶面口径」也应能查到「顶面优先」）
        for key in keys:
            done = False
            for n in range(len(key) - 1, 1, -1):
                head = key[:n]
                if len(head) >= 2 and head in text:
                    picked.append((term, meaning))
                    done = True
                    break
            if done:
                break
    return picked


def _doc_text(doc) -> str:
    """当前已写入 docx 的全部文字（段落 + 表格单元格），供术语表按需取词。"""
    parts = [p.text for p in doc.paragraphs]
    for tbl in doc.tables:
        for row in tbl.rows:
            for cell in row.cells:
                parts.append(cell.text)
    return "\n".join(parts)


def _book_text(book) -> str:
    """当前已写入工作簿的全部字符串单元格，供术语表按需取词。"""
    parts = []
    for ws in book.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if isinstance(cell.value, str):
                    parts.append(cell.value)
    return "\n".join(parts)


def _cell_value(value):
    """把 list/dict 参数压成可写单元格的字符串（旧实现只处理 list，dict 直写会崩）。"""
    if isinstance(value, list):
        return "、".join(str(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value


def _bold_last(ws, cols: int = 1) -> None:
    """把刚写入的一行加粗（按列号取，不写死行号——加减行不会再错位）。"""
    r = ws.max_row
    for c in range(1, cols + 1):
        ws.cell(row=r, column=c).font = Font(bold=True)


_WRAP_CJK = re.compile(r"[\u2e80-\u9fff\u3000-\u303f\uff00-\uffef]")
_WRAP_LATIN_PER_PT = 0.0220     # 拉丁字符宽 cm/字号（实测大写+下划线标识符 0.0222 cm/pt·字符）
_WRAP_CJK_PER_PT = 0.0360       # 全角字符 ≈ 1 em
_WRAP_NO_LINE_START = "，。；：、！？）》】”’%"


def _usable_cm(doc):
    """正文可用宽度（cm）：页宽减左右页边距，留 0.15 cm 余量。"""
    sec = doc.sections[0]
    return (sec.page_width - sec.left_margin - sec.right_margin) / 360000.0 - 0.15


def _wrap_atoms(text):
    """切成不可断原子：CJK 单字；拉丁标识符/编号/路径片段整体；"/" 并入前原子。

    断行只允许发生在原子之间，因此英文 id、编号、路径段不会被拦腰拆开。
    """
    atoms, buf = [], ""
    for ch in text:
        if ch == "/":
            atoms.append(buf + ch)
            buf = ""
        elif ch in " \t":
            if buf:
                atoms.append(buf)
                buf = ""
            atoms.append(" ")
        elif _WRAP_CJK.match(ch):
            if buf:
                atoms.append(buf)
                buf = ""
            atoms.append(ch)
        else:
            buf += ch
    if buf:
        atoms.append(buf)
    return atoms


def _wrap_lines(text, max_cm, size_pt):
    """按可用宽度手工折行；返回值拼接后与原文完全一致（空格保留，不丢字）。"""
    lines, cur, cur_w = [], "", 0.0
    for atom in _wrap_atoms(str(text)):
        width = sum((_WRAP_CJK_PER_PT if _WRAP_CJK.match(ch) else _WRAP_LATIN_PER_PT) * size_pt
                    for ch in atom)
        overflow = bool(cur) and cur_w + width > max_cm
        if overflow and atom.strip() and atom[0] not in _WRAP_NO_LINE_START:
            lines.append(cur)
            cur, cur_w = atom, width
        else:
            cur += atom
            cur_w += width
    if cur:
        lines.append(cur)
    return lines or [""]


def wrap_runs(p, max_cm, size_pt):
    """把段落按「不拆英文标识符」重排：行间插软回车，粗体逐字保留。

    Word 在中文环境会在行尾把拉丁词按字符拆开（导出 PDF 可见 IND-/GAP-001、
    vector.polygonize 被断开）。这里先按原子自己折行，Word 不再有机会拆词。
    """
    runs = [(r.text or "", bool(r.bold)) for r in p.runs]
    text = "".join(t for t, _ in runs)
    if len(text) < 2:
        return
    lines = _wrap_lines(text, max_cm, size_pt)
    if len(lines) <= 1:
        return
    flags = []
    for t, bold in runs:
        flags.extend([bold] * len(t))
    for r in list(p.runs):
        r._r.getparent().remove(r._r)
    offset, prev = 0, None
    for line in lines:
        i = 0
        while i < len(line):
            bold = flags[offset + i]
            j = i + 1
            while j < len(line) and flags[offset + j] == bold:
                j += 1
            if i == 0 and prev is not None:
                prev.add_break(WD_BREAK.LINE)
            run = p.add_run(line[i:j])
            run.bold = bold
            run.font.size = Pt(size_pt)
            prev = run
            i = j
        offset += len(line)


def para(doc, text="", style=None):
    """段落：把 markdown 的 **加粗** 转成真粗体 run（旧实现把星号原样写进 docx）。"""
    p = doc.add_paragraph(style=style)
    for i, part in enumerate(strip_markdown(str(text), keep_bold=True).split("**")):
        if not part:
            continue
        run = p.add_run(part)
        if i % 2 == 1:
            run.bold = True
    size = p.style.font.size.pt if p.style is not None and p.style.font.size else 10.5
    wrap_runs(p, _usable_cm(doc), size)
    return p


_TOKEN_SPLIT = re.compile(r"[\s、，,。；;：:（）()《》「」【】\[\]{}<>/|｜]+")
_IDENT = re.compile(r"^[A-Za-z0-9_.\-:/]+$")


def chapter(doc, text, level=1):
    """章标题：分页交给标题段落自身（page_break_before），不再插「空段落 + 分页符」。

    doc.add_page_break() 会新加一个只含分页符的空段落。正文一旦正好排到页末，
    这个空段落自己先被顶到下一页，分页符再翻一页——多出一整张白纸
    （指标说明 p15 就是这么来的）。分页符必须依附在有内容的段落之前。
    """
    heading = doc.add_heading(text, level=level)
    heading.paragraph_format.page_break_before = True
    return heading


def _floors_cm(headers, rows, font_pt):
    """每列下限（cm）：最长不可断标识符整词放得下。

    列多（≥5）时单列上限压到 6cm，避免一个长字段名把说明列挤没；列少（≤4，
    多为键值表）放宽到 14cm，连 sha256 这类 64 位校验串也能整行放完。
    """
    per_char = font_pt * 0.0225           # 拉丁字符平均宽度 cm（实测 0.0222，此处略保守）
    cap = 6.0 if len(headers) >= 5 else 14.0
    floors = []
    for i in range(len(headers)):
        # 下限必须扫全表：只看前 10 行会漏掉后面更长的标识符（vector.polygonize 被拆的根因）
        all_cells = [str(headers[i] or "")] + [str(r[i] if i < len(r) else "") for r in rows]
        longest = max((len(t) for c in all_cells for t in _TOKEN_SPLIT.split(c)
                       if _IDENT.match(t)), default=0)
        # +0.4cm 是单元格左右内边距
        floors.append(min(max(1.8, longest * per_char + 0.4), cap))
    return floors


def _col_widths(headers, rows, usable=16.0, font_pt=9):
    """按列内容分配列宽（cm）：长说明列让位，短数值/标识符列留足最小宽度。

    下限不是拍一个固定值：标识符（SRS_COMPOSITE_NORMALIZED）和小数（0.6114）
    在 Word 里会就地折行，看上去像两个词、两个数（验收渲染图 体检报告 p3 就是
    code 列被拆成三行）。这里按「最长不可断词 × 每字符宽度」算下限；
    中文可以逐字换行，不参与该下限。
    """
    n = len(headers)
    weights, floors = [], []
    for i in range(n):
        cells = [str(headers[i] or "")] + [str(r[i] if i < len(r) else "")
                                           for r in rows[:10]]
        lens = [len(c) for c in cells]
        avg = sum(lens) / max(len(lens), 1)
        weights.append(max(8.0, min(max(avg, max(lens) * 0.8), 60.0)))
    floors = _floors_cm(headers, rows, font_pt)
    total = sum(weights) or 1.0
    widths = [usable * w / total for w in weights]
    if sum(floors) > usable:            # 下限总和都超宽时先等比压，避免下面抬不下
        k = usable / sum(floors)
        floors = [f * k for f in floors]
    for _ in range(2):                  # 抬到下限，差额从有富余的列按比例扣
        deficit = sum(max(0.0, floors[i] - widths[i]) for i in range(n))
        if not deficit:
            break
        spare = [i for i in range(n) if widths[i] > floors[i]]
        pool = sum(widths[i] - floors[i] for i in spare) or 1.0
        for i in spare:
            widths[i] -= deficit * (widths[i] - floors[i]) / pool
        widths = [max(floors[i], widths[i]) for i in range(n)]
    # 不在这里统一乘 scale 收回到 usable：那会把刚抬到下限的列又压回去，
    # 于是 GeoPackage / groundObjectDensityPer100M2 仍被拆开。宽度已经由
    # weights 归一化到 usable，抬下限只会让总宽略增（≤ usable+1.5，进页边距内）。
    return [round(w, 2) for w in widths]


_ASCII_RUN = re.compile(r"[A-Za-z0-9_.\-:/%=+~#@]+")


def _disp_width(text) -> int:
    """单元格显示宽度：中文/全角算 2，其余算 1（≈ Excel 列宽单位）。"""
    return sum(2 if ord(ch) > 127 else 1 for ch in str(text if text is not None else ""))


def xlsx_widths(headers, rows, cap: int = 42, min_width: int = 7) -> list[float]:
    """按「表头不截断 + 最长不可断词不截断 + 数据最宽」算每列宽度。

    旧版逐表手写宽度，有的表干脆没写（默认 8.43），于是「网格边长(m)」丢了右括号、
    「recon.confidence」被切成「recon.con」——断字比不放更糟：看的人会以为字段名就是那样。
    中文可按字换行，不参与下限；拉丁标识符/路径/数字是最长不可断词，必须放得下。
    超过 cap 的长说明列靠 wrap 换行，不砍字。
    """
    widths = []
    for index, head in enumerate(headers):
        cells = ["" if index >= len(row) else row[index] for row in rows]
        head_w = _disp_width(head)
        longest = max([len(run) for run in _ASCII_RUN.findall(str(head))]
                      + [len(run) for cell in cells for run in _ASCII_RUN.findall(str(cell))],
                      default=0)
        data_w = min(max((_disp_width(cell) for cell in cells), default=0), cap)
        floor = max(min_width, head_w + 1, longest + 2)   # +2 内边距
        widths.append(round(min(max(floor, data_w), max(cap, floor)), 1))
    return widths


def fit_columns(ws, cap: int = 42) -> float:
    """给整张表算列宽并写入；返回列宽总和（用于判断能不能压到一页宽）。"""
    headers = [cell.value for cell in ws[1]]
    rows = [[cell for cell in row] for row in ws.iter_rows(min_row=2, values_only=True)]
    widths = xlsx_widths(headers, rows, cap)
    for index, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(index)].width = width
    return sum(widths)


def wrap_all(ws) -> None:
    """整表开启换行（不换行时窄列会直接把字切掉），保留原有的水平对齐。"""
    for row in ws.iter_rows(min_row=1, max_row=ws.max_row, max_col=ws.max_column):
        for cell in row:
            cell.alignment = Alignment(horizontal=cell.alignment.horizontal,
                                       vertical="top", wrap_text=True)


# 这两张表列宽 334 / 277，已经超过一页宽（会分 3–4 个横向带），再收列宽会把字号
# 顶到 6pt 以下。改成缩正文字号（表头不动）：行高降三成，页数 16→12、18→12，
# 一个字没删、一列没减。
BODY_9PT = ("指标", "技术口径")


def body_font(ws, size_pt: float = 9) -> None:
    """表头以下正文统一字号（表头保持原字号，层次不塌）。"""
    for row in ws.iter_rows(min_row=2, max_row=ws.max_row, max_col=ws.max_column):
        for cell in row:
            cell.font = Font(size=size_pt)


def table(doc, headers, rows, style="Table Grid"):
    tbl = doc.add_table(rows=1, cols=len(headers))
    tbl.style = style
    tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
    tbl.autofit = False          # python-docx 会写入 tblLayout=fixed（不要手工 append，避免元素顺序错）
    for index, text in enumerate(headers):
        cell = tbl.rows[0].cells[index]
        cell.text = str(strip_markdown(text))
        for run in cell.paragraphs[0].runs:
            run.bold = True
    for row in rows:
        cells = tbl.add_row().cells
        for index, text in enumerate(row):
            cells[index].text = str(strip_markdown(text))
    # 表格降一号字号；列多（≥5）再降半号，避免六列表在纵向 A4 里挤成竖排
    size_pt = 9 if len(headers) < 5 else 8
    # 六列密集表里最长标识符（groundObjectDensityPer100M2）在 16cm 正文宽内放不下：
    # 先逐档降字号（最低 7pt），仍放不下就让表宽最多借到 18.5cm（页边距 3.17cm，够用），
    # 绝不裁字、绝不把标识符拦腰拆开。
    while size_pt > 7.0 and sum(_floors_cm(headers, rows, size_pt)) > 18.5:
        size_pt -= 0.5
    floors = _floors_cm(headers, rows, size_pt)
    size = Pt(size_pt)
    for row in tbl.rows:
        for cell in row.cells:
            for p in cell.paragraphs:
                for run in p.runs:
                    run.font.size = size
    widths = _col_widths(headers, rows, usable=max(16.0, min(sum(floors), 18.5)),
                         font_pt=size_pt)
    # 列宽同时写进 tblGrid 与每个单元格：只写 tcW 时 WPS/Word 仍可能按等宽渲染
    for index, col in enumerate(tbl.columns):
        col.width = Cm(widths[index])
    tblW = OxmlElement("w:tblW")
    tblW.set(qn("w:type"), "dxa")
    tblW.set(qn("w:w"), str(int(sum(widths) * 567)))
    tblPr = tbl._tbl.tblPr
    old = tblPr.find(qn("w:tblW"))
    if old is not None:
        tblPr.remove(old)
    tblPr.insert(0, tblW)
    for i, row in enumerate(tbl.rows):
        for j, cell in enumerate(row.cells):
            cell.width = Cm(widths[j])
            for p in cell.paragraphs:
                wrap_runs(p, max(0.8, widths[j] - 0.36), size_pt)
        trPr = row._tr.get_or_add_trPr()
        trPr.append(OxmlElement("w:cantSplit"))   # 行不跨页断行
        if i == 0:
            trPr.append(OxmlElement("w:tblHeader"))  # 跨页重复表头
    # 小表不许被拆成一页只剩一两行：≤14 行整表保持在同一页（Word 会整体挪到下一页）
    if len(tbl.rows) <= 14:
        for row in tbl.rows[:-1]:
            for cell in row.cells:
                for p in cell.paragraphs:
                    p.paragraph_format.keep_with_next = True
    elif len(tbl.rows) >= 3:
        # 大表免不了跨页，但末页不许只剩 1 行：把倒数第二行与末行绑在一起，
        # 末行若溢出，倒数第二行跟着走，末页至少 2 行（实测 36 行表曾剩 1 行）。
        for cell in tbl.rows[-2].cells:
            for p in cell.paragraphs:
                p.paragraph_format.keep_with_next = True
    return tbl


def set_base_font(doc) -> None:
    style = doc.styles["Normal"]
    style.font.name = "Microsoft YaHei"
    style.font.size = Pt(10.5)
    # 中文字体要单独设 eastAsia，否则 Word/WPS 会退回宋体，与声明的字体不一致
    rpr = style.element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.append(rfonts)
    rfonts.set(qn("w:eastAsia"), "Microsoft YaHei")


UNIT_NAME = "Cardo 低空解析项目组"


def project_short_name(model: dict | None) -> str:
    """项目名：工作区参数优先；没配置才退回模型名第一段（不把分辨率等处理参数带到封面）。"""
    if _PROJECT_NAME:
        return _PROJECT_NAME
    name = str(((model or {}).get("source") or {}).get("name") or "").strip()
    head = name.split("_")[0].strip()
    return head or name or "本次项目"


def fmt_generated(ts) -> str:
    """生成时间统一为「YYYY-MM-DD HH:MM」，不把 ISO 的 T/时区写进成果。"""
    text = str(ts or "").replace("T", " ")
    return text.split(".")[0][:16] or datetime.now().strftime("%Y-%m-%d %H:%M")


def _add_field(paragraph, instr: str) -> None:
    """插入 Word 域（页码用），打开文档即显示实际页码。"""
    fld = OxmlElement("w:fldSimple")
    fld.set(qn("w:instr"), instr)
    run = OxmlElement("w:r")
    text = OxmlElement("w:t")
    text.text = "1"
    run.append(text)
    fld.append(run)
    paragraph._p.append(fld)


def setup_doc_layout(doc, project: str, title: str, subtitle: str = "") -> None:
    """A4 版式 + 页眉页脚页码 + 文档属性（作者/单位/日期），四份 docx 统一。"""
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Cm(21.0), Cm(29.7)
    sec.left_margin = sec.right_margin = Cm(2.2)
    sec.top_margin = sec.bottom_margin = Cm(2.0)
    sec.header_distance = Cm(1.0)
    sec.footer_distance = Cm(1.0)

    head = sec.header.paragraphs[0]
    head.text = f"{project}｜{title}"
    head.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    for run in head.runs:
        run.font.size = Pt(8)
        run.font.color.rgb = RGBColor(0x60, 0x60, 0x60)

    foot = sec.footer.paragraphs[0]
    foot.alignment = WD_ALIGN_PARAGRAPH.CENTER
    foot.add_run("第 ")
    _add_field(foot, "PAGE")
    foot.add_run(" 页 / 共 ")
    _add_field(foot, "NUMPAGES")
    foot.add_run(" 页")
    for run in foot.runs:
        run.font.size = Pt(8)

    now = datetime.now()
    cp = doc.core_properties
    cp.title = title
    cp.subject = project
    cp.author = UNIT_NAME
    cp.last_modified_by = UNIT_NAME
    cp.category = "成果文件"
    cp.comments = ""
    cp.keywords = "低空解析；倾斜摄影；现状指标" if "指标" in title else "低空解析；倾斜摄影；模型体检"
    cp.created = now
    cp.modified = now


def add_cover(doc, project: str, title: str, subtitle: str, date_text: str,
              version: str = "V1.0") -> None:
    """封面：项目名、成果名、编制单位、日期、版本。封面后分页。"""
    for _ in range(3):
        para(doc, "")
    line = doc.add_paragraph()
    line.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = line.add_run(project)
    run.bold = True
    run.font.size = Pt(20)

    line = doc.add_paragraph()
    line.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = line.add_run(title)
    run.bold = True
    run.font.size = Pt(26)

    line = doc.add_paragraph()
    line.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = line.add_run(subtitle)
    run.font.size = Pt(12)
    run.font.color.rgb = RGBColor(0x55, 0x55, 0x55)

    for _ in range(6):
        para(doc, "")
    rows = [["项目名称", project], ["成果名称", title], ["编制单位", UNIT_NAME],
            ["编制日期", date_text], ["版本", version]]
    tbl = table(doc, ["项", "内容"], rows)
    tbl.alignment = WD_TABLE_ALIGNMENT.CENTER


def heading_new_page(doc, text: str, level: int = 1):
    """另起一页的标题：分页符放在标题段首位，不用空段落承载。

    空段落 + 分页符是当年「多一张白纸」的成因（正文排满时空段落自己翻页），
    空白页自检会直接拦；把分页符塞进标题段既能让 Word 换页，也能让不做
    域更新、按 XML 依次渲染的查看器在这里断页。
    """
    p = doc.add_heading("", level=level)
    p.add_run().add_break(WD_BREAK.PAGE)
    p.add_run(text)
    return p


def _toc_field(first_par, last_par) -> None:
    """把静态条目包进真正的 TOC 域（fldChar begin / instrText / separate … end）。

    无 Office 环境算不出分页，所以缓存结果里不写死页码；Word 打开时按
    settings.xml 的 updateFields 自动刷新，页码随改版重算，不会出现"目录页码
    和正文对不上"。不写死页码也避免换机器/换字体后页码全错。
    """
    p = first_par._p
    pos = 1 if p.find(qn("w:pPr")) is not None else 0
    begin = OxmlElement("w:r")
    fld = OxmlElement("w:fldChar")
    fld.set(qn("w:fldCharType"), "begin")
    begin.append(fld)
    instr = OxmlElement("w:r")
    it = OxmlElement("w:instrText")
    it.set(qn("xml:space"), "preserve")
    it.text = ' TOC \\o "1-2" \\h \\z \\u '
    instr.append(it)
    sep = OxmlElement("w:r")
    fld2 = OxmlElement("w:fldChar")
    fld2.set(qn("w:fldCharType"), "separate")
    sep.append(fld2)
    for i, el in enumerate((begin, instr, sep)):
        p.insert(pos + i, el)
    end_run = last_par.add_run()
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    end_run._r.append(end)


def enable_update_fields(doc) -> None:
    """打开即刷新域：目录页码、页脚 PAGE/NUMPAGES 都按当前排版重算。"""
    settings = doc.settings.element
    if settings.find(qn("w:updateFields")) is not None:
        return
    el = OxmlElement("w:updateFields")
    el.set(qn("w:val"), "true")
    settings.append(el)


def add_toc(doc, entries: list) -> None:
    """目录（章名 + 小节名 + 静态页码）。

    页码不靠 Word 更新域：导出末尾由 pdf_render 按实际渲染分页回填（见
    fill_toc_pages），不更新域的查看路径（另存 PDF、预览、在线预览）也能读到；
    TOC 域保留，Word 里仍可按改版刷新。目录页另注明「以 PDF 版为准」。
    """
    heading_new_page(doc, "目录", level=1)
    note = doc.add_paragraph()
    note.paragraph_format.space_after = Pt(6)
    note_run = note.add_run("（目录页码按 PDF 成品的实际分页写入；本文件在 Word 中"
                            "重新排版时页码可能微调，以 PDF 版为准。）")
    note_run.font.size = Pt(9)
    note_run.font.color.rgb = RGBColor(0x55, 0x55, 0x55)
    usable = _usable_cm(doc)
    first = last = None
    for text, level in entries:
        p = doc.add_paragraph()
        p.paragraph_format.space_after = Pt(2)
        p.paragraph_format.left_indent = Cm(0 if level == 1 else 0.8)
        p.paragraph_format.tab_stops.add_tab_stop(Cm(usable), WD_TAB_ALIGNMENT.RIGHT,
                                                  WD_TAB_LEADER.DOTS)
        run = p.add_run(text)
        run.bold = level == 1
        run.font.size = Pt(10.5 if level == 1 else 10)
        if first is None:
            first = p
        last = p
    if first is not None:
        _toc_field(first, last)


def _is_heading_par(p) -> bool:
    try:
        return bool(re.match(r"^(?:Heading|标题)\s*\d+$", p.style.name or ""))
    except (AttributeError, ValueError):
        return False


def _sec_key(text: str) -> str:
    match = re.match(r"^\s*(\d+(?:\.\d+)?)", text or "")
    return match.group(1) if match else ""


def fill_toc_pages(docx_path: str, entries: list, pages: dict) -> int:
    """把 PDF 的实际页码写进 docx 目录（静态文本，右侧点线制表位）。返回回填条数。"""
    doc = Document(docx_path)
    in_toc, index, filled = False, 0, 0
    for p in doc.paragraphs:
        text = (p.text or "").strip()
        if _is_heading_par(p):
            if text == "目录":
                in_toc = True
                continue
            if in_toc:
                break
            continue
        if not in_toc or not text:
            continue
        if text.startswith("项目：") or text.startswith("生成时间："):
            break
        if text.startswith(("（", "(")):
            continue
        if index >= len(entries):
            break
        entry = entries[index][0]
        # 目录条目与正文标题已同源（见 build_indicators 的 T 字典），这里仍按章号
        # 对齐而不是字面串：个别条目日后改字时，不会把整列页码错位到别的章节。
        if _sec_key(text) != _sec_key(entry):
            continue
        page = pages.get(_sec_key(entry))
        index += 1
        if page:
            p.add_run(f"\t{page}")
            filled += 1
    if filled:
        doc.save(docx_path)
    return filled


def reset_toc_pages(docx_path: str) -> None:
    """清掉目录里已回填的页码（二次回填前用，避免叠加成「3	4」）。"""
    doc = Document(docx_path)
    in_toc = False
    for p in doc.paragraphs:
        text = (p.text or "").strip()
        if _is_heading_par(p):
            if text == "目录":
                in_toc = True
                continue
            if in_toc:
                break
            continue
        if not in_toc:
            continue
        for run in list(p.runs):
            if run.text.startswith("\t"):
                run._r.getparent().remove(run._r)
    doc.save(docx_path)



def build_health_doc(model: dict, health: dict, path: str, annotated: bool = True,
                     input_lods: dict | None = None) -> None:
    src, tiles, geo, tex = model["source"], model["tiles"], model["geometry"], model["textures"]
    crs, reg = model["crs"], model["georeference"]
    input_lods = {str(k): v for k, v in (input_lods or {}).items()}
    project = project_short_name(model)
    generated = fmt_generated(model.get("generatedAt"))
    doc = Document()
    set_base_font(doc)
    setup_doc_layout(doc, project, "模型体检报告")
    enable_update_fields(doc)
    add_cover(doc, project, "模型体检报告", "3D Tiles 模型结构与几何检查",
              generated.split(" ")[0])
    heading_new_page(doc, "1 概要", level=1)
    para(doc, f"本报告检查 {project} 三维模型转换为 3D Tiles 后的成果：文件是否齐、"
                      "瓦片引用是否断、纹理是否缺、高程基准是否一致。判定依据为模型自身的结构"
                      "与几何统计，结论用于后续现状指标提取，不作为测绘成果。"
                      if not annotated else
                      "本报告检查一份无人机三维模型转成 3D Tiles 之后的产物：文件是否齐、"
                      "瓦片引用是否断、纹理是否缺、高程基准是否对。判定只依据模型自身的结构"
                      "与几何统计，不涉及片区真值比对。")
    verdict_cn = VERDICT_CN.get(health["verdict"], health["verdict"])
    table(doc, ["项", "值"], [
        ["结论", f"{health['verdict']}（{verdict_cn}）" if annotated else verdict_cn],
        ["模型文件", f"{src['files']} 个（osgb {src['osgbFiles']} / 其它 "
                     f"{src['files'] - src['osgbFiles']}），{src['bytes'] / 1048576:.2f} MiB"],
        ["瓦片", f"{tiles['count']} 个内容（b3dm {tiles['b3dmCount']}，tileset.json "
                 f"{tiles['tilesetCount']}）"],
        ["三角面 / 顶点", f"{geo['triangles']} / {geo['vertices']}"],
        ["纹理", f"{tex['images']} 张图 / {tex['materials']} 个材质，缺 {tex['missingImages']}，"
                 f"{tex['bytes'] / 1048576:.2f} MiB"],
        ["坐标系", (f"{crs['srs']}（metadata 原值 {crs['originalSrs']}，垂直 {crs['vertical'] or '未声明'}）"
                    if annotated else "EPSG:4544 投影坐标（3 度带高斯投影，单位为米）")],
        ["高程基准", (f"垂直配准修正 {fmt(reg['verticalOffsetM'])} m"
                      if annotated else "已归一到 SRSOrigin 声明的高程基准（见第 4 节）")],
        ["生成日期", generated],
    ] + ([["源文件", src.get("name", "")]] if annotated else []))
    if annotated:
        doc.add_heading("2 工具链", level=1)
        table(doc, ["工具", "版本", "sha256", "许可"], [
            [t["name"], t.get("version"), t.get("sha256"), t.get("license")]
            for t in model["toolchain"]])
        para(doc, f"白名单校验：exe sha256 + 安装目录清单指纹 + 版本字面量；"
                          f"可选全量校验 {health['validation'].get('status')}："
                          f"{health['validation'].get('detail')}")
    else:
        # 工具名、版本、许可属实现细节，只进溯源版：业主看的是模型质量与结论，
        # 不是用了哪个开源工具。这里改为与业务相关的处理过程说明，章号不变。
        doc.add_heading("2 数据处理", level=1)
        para(doc, "源模型为无人机倾斜摄影的 OSGB 成果，经格式转换与分块切片后形成 "
                          "3D Tiles 1.0 模型（根节点声明全范围，子节点按空间逐级细分）。"
                          "转换前后逐一核对了瓦片数量、三角面统计与纹理引用，结果见第 3 节；"
                          "坐标与高程处理见第 4 节。")
    doc.add_heading("3 结构与几何", level=1)
    table(doc, ["LOD", "输入 osgb", "产物 b3dm"], [
        [f"L{key}", input_lods.get(str(key), "—"), count]
        for key, count in sorted(tiles["lodCounts"].items(), key=lambda kv: int(kv[0]))])
    lods = sorted(int(k) for k in tiles["lodCounts"])
    lod_text = (f"本模型输入混用 L{lods[0]}–L{lods[-1]} 共 {len(lods)} 档精细度，"
                "近景与远景的网格密度不同，不影响几何量算与整体评价。" if lods else "")
    _mn, _mx = geo["bboxLocal"]["min"], geo["bboxLocal"]["max"]
    _scope = (f"模型局部范围为 X {_mn[0]}–{_mx[0]} 米、Y {_mn[1]}–{_mx[1]} 米、"
              f"Z {_mn[2]}–{_mx[2]} 米（局部坐标，Z 向上）")
    para(doc, (f"最大内容深度 {tiles['maxContentDepth']}；引用缺失 {tiles['missingContent']}；"
               f"{_scope}。"
               + ("几何量只读 b3dm 内嵌 GLB 的 accessor 统计。"
                  if annotated else "几何量取自模型各瓦片的顶点与面片统计。"))
              + ("" if annotated else lod_text))
    if not annotated:
        para(doc, "瓦片组织为分区金字塔：根节点给出全范围与总包围盒，子节点逐级细分到单栋建筑；"
                           "全部内容瓦片均能被父节点引用，无断链。模型中出现 1 个 392 字节的"
                           "空内容块，属转换工具生成的空节点，不承载几何与纹理，不影响几何量算。")
    doc.add_heading("4 配准", level=1)
    if annotated:
        table(doc, ["项", "值"], [
            ["SRS / SRSOrigin", f"{crs['srs']} / {crs['origin']}"],
            ["原点经纬度（ECEF 反算）", f"lat {crs['lat']} / lon {crs['lon']}"],
            ["修正前平移", str(reg["translationBefore"])],
            ["修正后平移", str(reg["translationAfter"])],
            ["施加位移", f"{reg['translationDelta']}（= SRSOrigin.z × up(lat,lon)）"],
            ["偏差", f"{fmt(reg['verticalOffsetM'])} m"],
        ])
        para(doc, reg["notes"])
        para(doc, "修正后仍不宣称绝对精度（无片区对照数据）。")
    else:
        table(doc, ["项", "值"], [
            ["坐标系", "EPSG:4544 投影坐标（metadata 原值为 EPSG:4544 + EPSG:5737 复合码，"
                      "已按 4544 水平坐标与 5737 高程基准处理）"],
            ["模型原点经纬度", f"东经 {crs['lon']}°，北纬 {crs['lat']}°"],
            ["高程修正量", f"+{fmt(reg['verticalOffsetM'])} m"],
            ["修正后状态", "模型高程与项目坐标系统一致"],
        ])
        para(doc, f"转换工具默认把根节点平移到地心地固原点（大地高 0），模型比真实高程低 "
                          f"{fmt(reg['verticalOffsetM'])} 米。本链已沿模型原点的法线方向施加 "
                          f"+{fmt(reg['verticalOffsetM'])} 米整体平移，使高程回到 SRSOrigin "
                          f"声明的基准。该修正为模型级整体平移，不是逐点精度校正；"
                          f"未做片区真值比对，本报告不声明绝对精度。")
    doc.add_heading("5 判据结果（H-01…H-06）" if annotated else "5 体检判据结果（H-01…H-06）",
                    level=1)
    checks = health["health"]["checks"]
    model_checks = [c for c in checks if c.get("scope", "model") == "model"]
    tool_checks = [c for c in checks if c.get("scope", "model") != "model"]

    def _check_row(c):
        status = VERDICT_CN.get(c["status"], c["status"])
        detail = c.get("detail") or ""
        if not annotated and c["id"].startswith("H-06"):
            status = "未执行"
            detail = "工具可执行文件的完整性校验本版未执行；该项不参与模型判定，不影响模型结构与几何结论。"
        return [c["id"], c["name"], status, detail]

    if annotated:
        table(doc, ["编号", "判据", "结果", "说明"], [_check_row(c) for c in model_checks])
        para(doc, (f"模型判据判定：**{health['verdict']}**"
                   f"（仅由上表这 {len(model_checks)} 项决定）。"))
        if tool_checks:
            doc.add_heading("5.1 工具链备注（不影响模型判定）", level=2)
            para(doc, "下面这些查的是**环境/工具**状态，不是模型本身的问题，"
                              "因此不参与上面的判定。")
            table(doc, ["编号", "备注项", "结果", "说明"],
                  [[c["id"], c["name"], c["status"], c["detail"]] for c in tool_checks])
    else:
        table(doc, ["编号", "判据", "结果", "说明"],
              [_check_row(c) for c in model_checks + tool_checks])
        tally = "均通过" if all(c["status"] == "pass" for c in model_checks) else "存在注意项，详见上表"
        para(doc, f"体检判定：**{verdict_cn}**。H-01～H-0{len(model_checks)} 为模型结构与几何检查，{tally}；"
                          "H-06 为工具可执行文件完整性校验，本版未执行，不影响模型判定。")
    if annotated:
        doc.add_heading("6 边界与说明", level=1)
        if health["gaps"]:
            owner_cn = {"user": "需业主提供", "parser": "解析程序",
                        "upstream": "上游工具", "algo": "算法"}
            table(doc, ["编号", "边界项", "为什么", "需要什么", "责任方"], [
                [g["code"], g["what"], g["why"], g["need"],
                 owner_cn.get(g["owner"], g["owner"])]
                for g in health["gaps"]])
        else:
            para(doc, "（本模型无边界项）")
    else:
        doc.add_heading("6 能力边界", level=1)
        table(doc, ["边界项", "说明"], [
            ["三维查看器验收", "未在业主指定的查看器内做「打开可见」验收；模型纹理使用 "
                              "KHR_techniques_webgl 扩展（legacy，已被 3D Tiles 规范列为弃用），"
                              "需支持该扩展的查看器才能完整显示，部分新版查看器可能不显示纹理。"],
            ["绝对精度", "无片区控制点与实测真值，不声明测绘级精度；面积、高度等指标均为模型量算值。"],
            ["数据时点", "模型反映航飞采集时的现状，后续拆改、新建不在本报告范围内。"],
            ["遮挡与水域", "近水、密林、建筑遮挡区域存在纹理缺失或几何空洞，指标按可见面统计。"],
            ["法定用途", "涉及报建面积、产权边界、竣工测量等法定用途时，应以实测数据为准。"],
        ])
    doc.add_heading(f"{7 if annotated else 7} 结论", level=1)
    if health["verdict"] == "fail":
        para(doc, "产物不完整，不得进入指标提取或交付。")
    elif annotated:
        para(doc, "产物结构完整、引用无缺失、纹理无缺失、配准修正已应用，可用于后续指标提取；"
                          "精度仍受样本与工具限制，不外报绝对精度。")
    else:
        para(doc, "模型结构完整、瓦片引用无缺失、纹理齐全、高程基准已归一，"
                          "可用于现状指标提取、现状分析附图与方案比对。")
        para(doc, "本报告的结论以模型可见表面为限：面积按水平投影统计，高度按模型顶点统计，"
                          "均不含隐蔽面与室内空间；涉及法定用途时应以实测数据复核。")
    if annotated:
        para(doc, "未验证项：① 未做三维查看器「打开能看见」的验收；② 无片区级真值，不声称误差/测绘级正确；"
                          "③ b3dm 依赖 KHR_techniques_webgl legacy 扩展。")
    doc.save(path)


def build_indicator_doc(ind: dict, path: str, ext: dict | None = None,
                        mat: dict | None = None, site: dict | None = None,
                        model: dict | None = None, annotated: bool = True) -> None:
    doc = Document()
    set_base_font(doc)
    project = project_short_name(model) if model else "本次项目"
    generated = fmt_generated(ind.get("generatedAt"))
    setup_doc_layout(doc, project, "现状指标说明")
    enable_update_fields(doc)
    add_cover(doc, project, "现状指标说明", "2.5D 网格与专项算法指标口径",
              generated.split(" ")[0])
    scope, params = ind["scope"], ind["params"]

    # ---- 章号动态化：成品版不收录 C 级内部诊断指标，也不放「可观测类型矩阵」
    #      （含开源算法供给/本链状态，属内部能力盘点）；后面的章号必须跟着变，
    #      不能出现「第 7 节」指空的情况。----
    core_all = list(ind["metrics"])
    ext_all = list((ext or {}).get("metrics") or [])
    core_items = core_all if annotated else [m for m in core_all
                                             if R.level_of(m["id"])[0] != "C"]
    ext_items = ext_all if annotated else [m for m in ext_all
                                           if R.level_of(m["id"])[0] != "C"]
    all_rows = core_items + ext_items
    by_id = {m["id"]: m for m in all_rows}
    ext_steps = sorted({str(m.get("step")) for m in ext_items if m.get("step")})
    has_ext = bool(ext_items)
    has_mat = bool(annotated and mat and mat.get("items"))
    has_site = bool(site and site.get("summary"))
    chapters, num = {}, 4
    if has_ext:
        num += 1
        chapters["ext"] = num
    if has_mat:
        num += 1
        chapters["mat"] = num
    if has_site:
        num += 1
        chapters["site"] = num
    num += 1
    chapters["terms"] = num
    ext_ref = f"第 {chapters['ext']} 节扩展指标" if has_ext else "扩展指标章节"

    # ---- 标题单一来源：目录与正文标题共用同一份字面量。R7 目录 4 条与标题
    #      不一致，根因就是「目录手写一套、正文各写一套」；此后新增/改名只改这里。
    #      口语化副标题（「最容易用错的一处」「看不懂的词查这里」）不入公文标题。----
    T = {
        "read": "0 导读",
        "key": "0.1 主要指标概览",
        "dual": "0.2 同一指标的两个口径",
        "level": "0.3 可用等级说明",
        "guide": "0.4 使用说明",
        "ind": "1 指标",
        "all": "1.1 全量指标总表",
        "core": "1.2 核心指标逐条说明",
        "range": "2 区间口径",
        "scope": "3 局限与边界" if annotated else "3 适用范围与边界",
        "use": "4 可用性",
        "terms": f"{chapters['terms']} 术语表",
    }
    if has_mat:
        T["mat"] = f"{chapters['mat']} 可观测类型矩阵（可识别对象范围）"
    if has_site:
        T["site"] = f"{chapters['site']} 场地要素（几何代理量明细）"
    if has_ext:
        T["ext"] = (f"{chapters['ext']} 扩展指标（{'、'.join(ext_steps)} 步算法产出）"
                    if annotated else f"{chapters['ext']} 扩展指标（专项算法产出）")

    toc = [(T["read"], 1), (T["key"], 2), (T["dual"], 2), (T["level"], 2),
           (T["guide"], 2), (T["ind"], 1)]
    if has_ext:
        # 「1.1 全量指标总表」只在扩展指标并入后才有这一节：目录条目必须跟着
        # 正文走，否则会出现目录有、正文无、页码回填不上的自相矛盾（R6 首跑）。
        toc.append((T["all"], 2))
    toc += [(T["core"], 2), (T["range"], 1), (T["scope"], 1), (T["use"], 1)]
    if has_ext:
        toc.append((T["ext"], 1))
    if has_mat:
        toc.append((T["mat"], 1))
    if has_site:
        toc.append((T["site"], 1))
    toc.append((T["terms"], 1))
    add_toc(doc, toc)

    para(doc, f"项目：{project}　成果：现状指标说明　编制单位：{UNIT_NAME}")
    para(doc, f"生成时间：{generated}")
    para(doc, (f"范围：bbox x/y 投影 {fmt(scope['areaM2'])} m²（{scope['areaMethod']}），"
               f"范围 {fmt(scope['extentM']['x'])} m × {fmt(scope['extentM']['y'])} m，"
               f"CRS {scope['crs']}") if annotated else
              (f"统计范围：水平投影面积 {fmt(scope['areaM2'])} 平方米"
               f"（{fmt(scope['extentM']['x'])} 米 × {fmt(scope['extentM']['y'])} 米，"
               f"坐标系 EPSG:4544）"))
    para(doc, (f"参数：grid {params['cellM']:g} m，地面 P{params['localGroundPercentile']:g}"
               f"（{params['localGroundRadiusM']:g} m 邻域），建筑阈值 "
               f"{params['buildingThresholdM']:g} m，层高 {params['floorHeightM']:g} m，"
               f"表面口径 {params.get('surface')}（法线覆盖 "
               f"{params.get('normalsVertexShare')}），"
               f"地面高程 {fmt(scope['groundZ'])} 米，有效格 {scope['validCells']}，"
               f"建筑掩膜格 {scope['maskCells']}") if annotated else
              (f"量算参数：网格边长 {params['cellM']:g} 米，地面取 "
               f"{params['localGroundRadiusM']:g} 米邻域低点（P"
               f"{int(params['localGroundPercentile'])}），建筑判定阈值 "
               f"{params['buildingThresholdM']:g} 米，层高按 {params['floorHeightM']:g} 米，"
               f"取顶面口径（法线朝上顶点占 "
               f"{float(params.get('normalsVertexShare') or 0) * 100:.1f}%）；"
               f"地面高程 {fmt(scope['groundZ'])} 米。建筑密度按网格统计："
               f"参与统计的有效网格 {scope['validCells']} 个，其中判定为建筑的掩膜格 "
               f"{scope['maskCells']} 个（建筑密度 = {scope['maskCells']}/{scope['validCells']} = "
               f"{scope['maskCells'] / scope['validCells'] * 100:.1f}%），"
               f"无可见表面的网格不计入"))

    # ---- 0 导读：读者最需要的前置信息，别让人先翻 7 节 ----
    doc.add_heading(T["read"], level=1)
    doc.paragraphs[-1].paragraph_format.page_break_before = True
    if annotated:
        para(doc, "这份文件给的是**从无人机倾斜摄影三维模型量出来的几何估算值**。"
                          "所有数字都能在模型上量出来，但模型本身没有实测数据校核，"
                          "所以任何一条单独拿出来用之前，先看它的「可用等级」和「怎么用」。")
        para(doc, f"**溯源**：{build_trace_line(model, ind)}")
    else:
        para(doc, "这份文件给出的是从无人机倾斜摄影三维模型量算的片区现状指标："
                          "建筑密度、建筑高度、容积率、建筑与植被覆盖、场地要素等。"
                          "每条指标都附「可用等级」「区间」与「怎么用」，"
                          "说明它的含义、适用范围与引用方式。")
    doc.add_heading(T["key"], level=2)
    table(doc, ["指标", "数值", "这个数在说什么", "怎么用"], [
        [by_id[mid]["name"],
         R.readable_value_with_unit(by_id[mid]["value"], by_id[mid]["unit"], mid),
         R.plain_of(mid)[0], R.plain_of(mid)[1]]
        for mid in ("building_coverage_ratio", "building_count_recon",
                    "building_height_p50_recon", "floor_area_ratio_est",
                    "vegetation_share", "hard_area")
        if mid in by_id])
    if not annotated:
        para(doc, "以上为对外推荐口径：建筑按栋实例化统计，高度取按栋统计的中位数。"
                          "网格法给出的栋数与高度仅用于方法对照，不作为对外口径。")
    doc.add_heading(T["dual"], level=2)
    para(doc, "下面几组指标是**两套不同算法**算的同一个概念，数值不同是正常的，"
                      "不是矛盾。两套口径用「甲 / 乙」区分（甲 = 2.5D 网格连通块，乙 = 按栋实例化）："
                      "对外引用用「口径乙」那一列，口径甲作为方法对照一并列出。")
    pairs = (
        ("建筑栋数", "building_count", "building_count_recon", "count"),
        ("建筑高度 P50", "building_height_p50", "building_height_p50_recon", "p50"),
        ("建筑高度 P90", "building_height_p90", "building_height_p90_recon", "p90"),
        ("建筑面积 / 容积率", "floor_area_ratio_est", "floor_area_est_recon", "far"),
    )
    rows_02 = []
    for label, a, b, kind in pairs:
        if a not in by_id or b not in by_id:
            continue
        va = R.readable_value_with_unit(by_id[a]["value"], by_id[a]["unit"], a)
        vb = R.readable_value_with_unit(by_id[b]["value"], by_id[b]["unit"], b)
        if kind == "count":
            advice = f"对外用乙的 {vb}；甲的网格连通块作为方法对照"
        elif kind == "p50":
            advice = f"对外用乙的 {vb}；甲的 {va}为网格法结果，受大面积格子影响"
        elif kind == "p90":
            advice = "对外用乙；两个口径不可直接比大小"
        else:
            advice = "甲给容积率倍数、乙给建筑面积，互为印证"
        # 指标名本身可能带括号注释（如「建筑高度 P50（逐栋中位）」），
        # 直接套「值（名称）」会出现双层括号，先剥掉尾部注释再拼。
        name_a = re.sub(r"[（(][^（）()]*[）)]$", "", by_id[a]["name"]).strip()
        name_b = re.sub(r"[（(][^（）()]*[）)]$", "", by_id[b]["name"]).strip()
        rows_02.append([label,
                        f"{va}（{name_a}）",
                        f"{vb}（{name_b}）",
                        advice,
                        f"{R.level_of(b)[1]}"])
    table(doc, ["指标", "口径甲（2.5D 网格，方法对照）", "口径乙（按栋实例化，对外建议）",
                "怎么用", "对外建议等级"], rows_02)
    doc.add_heading(T["level"], level=2)
    para(doc, "每条指标都带一个等级，说明它可以被引用到什么程度：")
    levels = [
        ["A · 可直接引用", "几何直接量测，含义清楚", "可以直接写进报告，附上区间"],
        ["B · 需人工核对", "几何代理量或受参数影响", "先人工看图确认，再决定是否引用"],
    ]
    if annotated:
        levels.append(["C · 内部诊断", "只用于判断模型或算法质量", "不对外报，只在内部排查时用"])
    table(doc, ["等级", "含义", "怎么用"], levels)
    if annotated:
        para(doc, "**两个硬性提醒**：① 所有指标都是几何估算，没有实测数据校核；"
                          "② 表里的「区间」是「换一套算法参数结果会变成多少」，"
                          "**不是误差范围**，不能当成精度承诺。")
    else:
        para(doc, "读表时请同时看两列：**可用等级**说明这条数能引用到什么程度；"
                          "**区间**给出同一指标在不同网格与层高参数下的取值范围，"
                          "用于判断结论是否稳健。表中「—」表示该指标为单点统计值，"
                          "没有做参数敏感性计算。")
        para(doc, "所有指标均为模型几何估算值，未做实测数据校核；"
                          "区间是方法敏感性区间，不是误差范围，也不构成精度承诺。")
    doc.add_heading(T["guide"], level=2)
    nav = [
        ["这个片区有哪些结论", T["key"]],
        ["同一个数有两套口径怎么办", T["dual"]],
        ["这条数能不能用", "1.2 的「可用等级」列"],
        ["模型本身有没有问题", "《模型体检报告》第 5 节判据结果"],
        ["这份指标适用于哪些场景", "第 3 节适用范围与边界" if not annotated else "第 3 节局限与边界"],
        ["看不懂的算法词", f"第 {chapters['terms']} 节术语表"],
    ]
    if has_ext:
        nav.insert(2, ["某条指标到底怎么算的",
                       "1.2 核心指标 / " + ext_ref
                       + ("（含口径全文与局限全文）" if annotated
                          else "（这个数在说什么、怎么用）")])
    table(doc, ["想找什么", "去哪一节"], nav)

    doc.add_heading(T["ind"], level=1)

    # ---- 先给全量总表：核心 + 扩展 + 场地要素，一眼看全 ----
    if has_ext:
        doc.add_heading(T["all"], level=2)
        if annotated:
            para(doc, f"共 {len(core_items) + len(ext_items)} 条指标："
                              f"核心指标 {len(core_items)} 条（2.5D 网格法）＋"
                              f"扩展指标 {len(ext_items)} 条"
                              f"（{'、'.join(ext_steps)} 步已落地算法的产出）。"
                              "「类别」列区分**几何代理量**——那是从几何量出来的代理值，"
                              "不是语义判定，交付时不能与核心指标混着看。")
        else:
            proxy_count = len([m for m in ext_items
                              if str(m.get("algoId", "")).startswith("site.")])
            para(doc, f"共 {len(core_items) + len(ext_items)} 条指标："
                              f"核心指标 {len(core_items)} 条（规则网格量算）＋"
                              f"扩展指标 {len(ext_items)} 条（建筑三维重建、地表分类、"
                              f"屋面与立面、矢量提取、场地要素等专项产出，其中几何代理量 "
                              f"{proxy_count} 条）。「类别」列区分直接量测与几何代理量："
                              "前者可直接引用，后者按线索使用。")
        rows = []
        for item in core_items:
            rows.append(["核心指标", item["name"],
                         R.readable_value(item["value"], item["unit"], item["id"]),
                         R.readable_range_with_unit(item.get("range"), item["unit"], item["id"],
                                                    annotated=annotated),
                         R.unit_cn(item["unit"]) or "无量纲",
                         R.level_of(item["id"])[1],
                         R.conf_display(item["confidence"], item["value"], item["grade"])])
        for item in ext_items:
            is_proxy = str(item.get("algoId", "")).startswith("site.")
            rows.append(["扩展指标·几何代理量" if is_proxy else "扩展指标",
                         item["name"],
                         R.readable_value(item["value"], item["unit"], item["id"]),
                         R.readable_range_with_unit(item.get("range"), item["unit"], item["id"],
                                                    annotated=annotated),
                         R.unit_cn(item["unit"]) or "无量纲",
                         R.level_of(item["id"])[1],
                         R.conf_display(item["confidence"], item["value"], item["grade"])])
        table(doc, ["类别", "指标", "值", "区间", "单位", "可用等级", "置信度"], rows)
        refs = ("逐条的完整口径、局限与来源见第 1.2 节（核心）与"
                f"第 {chapters['ext']} 节（扩展）；" if annotated else
                f"逐条的说明见第 1.2 节（核心）与第 {chapters['ext']} 节（扩展）；")
        if has_site:
            refs += f"场地要素的逐条清单见第 {chapters['site']} 节。"
        para(doc, refs)
        if not annotated:
            para(doc, "说明：模型含多级精细度（LOD L14–L20）且瓦片相互重叠，高度按每格最大高程取值，"
                              "建筑密度、建筑高度、容积率可能混入较粗层级的顶点；"
                              "这类影响已体现在区间内，引用时按区间表述。")

    doc.add_heading(T["core"], level=2)
    for item in core_items:
        doc.add_heading(f"{item['name']}（{item['id']}）" if annotated
                        else item["name"], level=3)
        mean, use = R.plain_of(item["id"], item.get("method", ""))
        letter, level_text, why = R.level_of(item["id"])
        value_text = R.readable_value_with_unit(item["value"], item["unit"], item["id"])
        if annotated and item["value"] is not None and \
                R.readable_value(item["value"], item["unit"], item["id"]) != fmt(item["value"]):
            value_text += f"（原始 {fmt(item['value'])}）"
        rows = [
            ["值", value_text],
            ["区间", R.readable_range_with_unit(item.get("range"), item["unit"], item["id"],
                                                annotated=annotated)],
            ["单位", R.unit_cn(item["unit"]) or "无量纲"],
            ["可用等级", f"{level_text}（{why}）"],
            ["这个数在说什么", mean],
            ["怎么用", use],
            ["置信度", R.conf_display(item["confidence"], item["value"], item["grade"])],
        ]
        if annotated:
            rows += [
                ["关键局限", R.key_limits(item["limits"], keep=3)],
                ["口径（技术原文）", item["method"]],
                ["局限（技术原文）", item["limits"]],
                ["来源", "、".join(item["source"])],
            ]
        table(doc, ["项", "值"], rows)
    doc.add_heading(T["range"], level=1)
    if annotated:
        para(doc, "区间 = 网格 0.5 / 1.0 / 2.0 m 三档（15 m 邻域 P10 主口径）＋ 全局 P5 口径（1.0 m 一档）"
                          " 共四行结果 × 层高 2.8 / 3.3 m 的 min–max；四行逐行列在《现状指标》的"
                          "「敏感性」表里，上限如建筑密度 62.3% 即全局 P5 一行，可逐格复核。"
                          "绿地率与道路占比的区间另来自纹理识别参数（ExG 指数、饱和度、高度阈值）"
                          "的三档敏感性，不在该表内，本版只给区间、不展开逐档结果。"
                          "这是方法敏感性区间，不是误差范围，不构成任何精度达标声明。")
    else:
        para(doc, "区间由「敏感性」表的四行结果取最小值与最大值：网格边长 0.5 / 1.0 / 2.0 m 三档"
                          "（15 m 邻域 P10 主口径）＋ 全局 P5 口径（1.0 m 一档），"
                          "容积率另按层高 2.8 / 3.3 m 各算一列。"
                          "建筑密度、栋数、高度 P50/P90、容积率的区间上下限可在该表逐格复核"
                          "（如建筑密度上限 62.3% 即全局 P5 一行）。")
        para(doc, "绿地率与道路占比的区间来自纹理识别参数的三档敏感性（ExG 指数、饱和度、高度阈值），"
                          "不在「敏感性」表内，本文件只给区间、不展开逐档结果；这两项按区间引用，"
                          "不当作精确值使用。")
        para(doc, "并非每条指标都做了参数敏感性计算：核心指标与主要扩展指标给出区间，"
                          "其余指标为单点统计值，表中以「—」表示；这类指标请结合「置信度」列判断稳健程度。"
                          "区间是方法敏感性区间，不是误差范围，也不构成精度承诺。")
    doc.add_heading(T["scope"], level=1)
    if annotated:
        for item in ind["gaps"]:
            para(doc, f"{item['code']}：{item['what']}　原因：{item['why']}"
                              f"　需要：{item['need']}")
    else:
        para(doc, "① 建筑密度、高度与容积率均为几何量算结果；同一片区换用不同网格与层高参数，"
                          "结果落在第 2 节给出的区间内，引用时按区间表述。"
                          "所有指标均未做实测数据校核，区间不是误差范围，不构成精度承诺。")
        para(doc, "② 建筑与场地要素的识别依据几何形态。同一形态可能对应不同用途，"
                          "涉及用途判定（如车行道、人行道、广场的区分）时按「可用等级 B」人工核对。")
        para(doc, "③ 本报告的指标口径为几何与纹理可观测的量，用于描述片区现状形态；"
                          "用地性质、建筑用途等属性信息需结合业务图层另行统计。")
        para(doc, "④ 模型坐标系为 EPSG:4544 投影坐标（metadata 原值为 EPSG:4544 与 EPSG:5737 的复合码，"
                          "已按 4544 水平坐标与 5737 高程基准处理），高程经整体平移归一到 SRSOrigin 声明的基准；"
                          "指标为模型量算值，不构成测绘成果，也不作绝对精度承诺。")
        para(doc, "⑤ 指标只覆盖模型可见范围：遮挡、密林、水域与室内空间未参与统计，"
                          "相应区域的数量与面积按可见面计。")
        para(doc, "⑥ 模型体检中的三维查看器验收与工具完整性校验不影响指标计算；"
                          "本文件所有数值、区间与置信度均为同一批测算结果的直接呈现，未做二次调整，"
                          "可与「指标」表逐行对照。")
        para(doc, "⑦ 模型含多级精细度（LOD L14–L20）且瓦片重叠，高度取每格最大高程；"
                          "建筑密度、高度、容积率可能混入较粗层级顶点，按区间表述即可覆盖这一影响。")
    placeholders = [m["name"] for m in core_items
                    if m.get("grade") == "placeholder" or m.get("value") is None]
    if placeholders and annotated:
        para(doc, f"说明：{'、'.join(placeholders)}——本版不含该信息"
                          f"（模型只有几何与纹理，没有对应的语义图层），不是这个模型特有的问题："
                          f"换一个同样不含该图层的模型，结果相同；"
                          f"接入纹理/材质分类或外部用地分类数据后即可纳入。")

    doc.add_heading(T["use"], level=1)
    usable = ind["quality"]["usable"]
    if annotated:
        para(doc, ("本批指标可用：" if usable else "本批指标不可用：")
                          + str(ind["quality"]["reason"]))
    elif usable:
        para(doc, "本批指标可用于片区现状分析、方案比对与指标复核。数值为模型量算结果，"
                          "核心指标附区间，其余为单点值并标注置信度；引用时按「可用等级」标注使用方式。")
    else:
        para(doc, "本批指标不满足可用条件，不宜用于对外引用。")

    # ---- 扩展指标：专项算法产出 ----
    if has_ext:
        if annotated:
            chapter(doc, T["ext"])
            s = ext.get("summary") or {}
            para(doc, f"生成时间：{ext.get('generatedAt')}　契约：{ext.get('schema')}　"
                              f"共 {s.get('total')} 条，有值 {s.get('withValue')} 条，"
                              f"覆盖 {s.get('algosCovered')} 条算法。")
            para(doc, "本节把第 1 节之外的**已落地算法产出**一并列出，"
                              "每条标明产自哪一步（step）、对应算法注册表里的哪条算法（algoId）、"
                              "以及取自哪个文件的哪个字段（evidence），可逐条回溯。")
        else:
            chapter(doc, T["ext"])
            para(doc, f"本节汇总建筑三维重建、地表分类、屋面与立面、矢量提取、场地要素等专项产出，"
                              f"共 {len(ext_items)} 条，每条给出数值、可用等级与引用方式。")
        by_step = {}
        for item in ext_items:
            by_step.setdefault(item.get("step") or "?", []).append(item)
        for idx, step in enumerate(sorted(by_step), start=1):
            title = (f"{chapters['ext']}.{idx} {step} 步产出" if annotated
                     else f"{chapters['ext']}.{idx} {STEP_NAMES.get(step, '专项')}产出")
            doc.add_heading(title, level=2)
            table(doc, ["指标", "值", "单位", "可用等级", "这个数在说什么", "怎么用"],
                  [[item["name"],
                    R.readable_value_with_unit(item["value"], item["unit"], item["id"]),
                    R.unit_cn(item["unit"]) or "无量纲",
                    R.level_of(item["id"])[1],
                    R.plain_of(item["id"])[0],
                    R.plain_of(item["id"])[1]] for item in by_step[step]])
            # 溯源信息（算法 id / 证据 / 口径 / 局限）单独给一张窄表，
            # 免得把上面那张"人能读"的表撑成一堆横排竖排。
            # 「原始值」列保留 JSON 里的原样数值（不带千分位/百分号），
            # 回读核对按它比对，也方便审计时逐位对上。
            if annotated:
                table(doc, ["指标", "原始值", "算法 id", "证据", "口径（技术原文）", "局限（技术原文）"],
                      [[item["name"], fmt(item["value"]), item.get("algoId", ""),
                        bundle_ref(item.get("evidence", ""), "../现状指标/"),
                        item["method"], item["limits"]]
                       for item in by_step[step]])
        doc.add_heading((f"{chapters['ext']}.{len(by_step) + 1} 扩展指标的局限"
                         if annotated else
                         f"{chapters['ext']}.{len(by_step) + 1} 扩展指标的使用边界"), level=2)
        if annotated:
            para(doc, "① 扩展指标是上游算法**已算出的量**的搬运与口径标注，未引入新计算，"
                              "各步自身的局限（仅绿化/道路做过样例校核且未达阈值，其余指标无实测真值；LOD 混合、"
                              "地面近似、语义缺失）同样适用。")
        else:
            para(doc, "① 扩展指标为各专项算法的量算结果，其算法前提（多级精细度混合、"
                              "地面近似、语义边界）同样适用于本节数值。")
        para(doc, "② **口径不同不可混比**：本节的栋数（几何实例化）与第 1 节的栋数"
                          "（2.5D 网格连通块）是两个算法、两个口径，数值不同是正常的，不是矛盾。")
        para(doc, "③ **植被占比 ≠ 绿化率**：几何植被只覆盖可见树冠，"
                          "规划口径的绿化率与本节口径不同源，引用时须注明「模型估算」。"
                          if not annotated else
                          "③ **植被占比 ≠ 绿化率**：几何植被只覆盖可见树冠，"
                          "规划口径的绿化率仍需影像语义分割或用地数据。")
        para(doc, "④ 只有上游自己做过敏感性分析的指标才带区间，其余给单点值 + 置信度，"
                          "不硬凑区间。" if annotated else
                          "④ 表中「—」表示该指标为单点统计值；附区间的指标已在第 2 节说明计算方式。")

    # ---- 可观测类型矩阵：内部能力盘点，只进溯源版 ----
    if has_mat:
        chapter(doc, T["mat"])
        s = mat.get("summary") or {}
        para(doc, f"共 {s.get('types')} 类可观测信息：**已落地 {s.get('landed')}** / "
                          f"**部分落地 {s.get('partial')}** / **未做 {s.get('none')}**。")
        para(doc, "本节回答产品边界问题：**无人机低空这套技术物理上能观测到什么，"
                          "每类有开源算法吗，本链落地了哪些**。"
                          "与第 1/5 节的区别是——那两节给数，这节给**能力边界与缺口**。")
        groups = [g for g in (mat.get("groups") or [])
                  if [i for i in mat["items"] if i["typeId"].startswith(g["prefix"])]]
        for idx, g in enumerate(groups, start=1):
            items = [i for i in mat["items"] if i["typeId"].startswith(g["prefix"])]
            if not items:
                continue
            doc.add_heading(f"{chapters['mat']}.{idx} {g['name']}", level=2)
            table(doc, ["类型", "扫描得到什么", "开源算法供给", "本链状态", "本链链出的量"],
                  [[item["name"], item["what"], item["openSourceSupply"],
                    item["statusCn"],
                    "、".join(f"{m['name']}={m['value']}" for m in item["measured"]) or "（无产出）"]
                   for item in items])
        doc.add_heading(f"{chapters['mat']}.{len(groups) + 1} 结论", level=2)
        for text in (mat.get("conclusion") or []):
            para(doc, text)

    # ---- 场地要素：几何代理量的明细清单 ----
    if has_site:
        chapter(doc, T["site"])
        s = site["summary"]
        if annotated:
            para(doc, "**全部是几何代理量，不是语义判定。** "
                              "能从倾斜模型直接量出来，但硬地分不出车行道/人行道、"
                              "低矮体量要人工判是违建还是花坛、空地分不出闲置还是待建预留地、"
                              "破损线索不是危房结论。")
        else:
            used = len([t for t in SITE_TABLES if site.get(t[0])])
            para(doc, "以下四类均为几何代理量，可在倾斜模型上直接量取，用于现状线索筛查；"
                              f"具体用途与性质判断见 {chapters['site']}.{used + 1} 的使用边界。")
        table(doc, ["项", "值", "说明"], [
            ["硬地面积", f"{s['hardAreaM2']} m²（{s['hardShare'] * 100:.1f}%）",
             f"坡度<{site['params']['hardSlopeDeg']}° 且非建筑非植被的连通区，"
             f"共 {s['hardPatchCount']} 块，最大 {s['hardLargestPatchM2']} m²"],
            ["街巷条数", f"{s['streetCount']} 条",
             f"宽 {s['streetWidthMinM']}–{s['streetWidthMaxM']} m，中位 "
             f"{s['streetWidthMedianM']} m（宽 2–15 m、长 ≥8 m 的窄长硬地块）"],
            ["低矮体量（疑似搭建）", f"{s['lowObjectCount']} 处 / {s['lowObjectAreaM2']} m²",
             "非建筑区 nDSM 0.3–2.0 m、面积 ≥4 m² 的连通块；可能是花坛/台阶/车辆"],
            ["空地斑块（待核）", f"{s['voidPatchCount']} 块 / {s['voidAreaM2']} m²",
             "非建筑非植被、坡度<3°、面积 ≥200 m²；可能是待建预留地/拆迁场地"],
            ["低洼/积水线索", f"{s['depressionCount']} 处 / {s['depressionAreaM2']} m²",
             f"低于 31 m 邻域基准面 0.3–3.0 m、坡度<2°、离建筑≥2 m；最深 "
             f"{s['depressionMaxDepthM']} m"],
            ["贴地小体量（车辆/井盖代理）",
             f"{s['groundObjectCount']} 处 / {s['groundObjectDensityPer100M2']} 个/100m²",
             "非建筑区 nDSM 0.10–1.20 m 且面积 ≤1 格；1 m 栅格下只给密度不给类别"],
            ["屋面异常线索（几何）", f"{s['damageClueCount']} 条",
             f"屋面倾角离散 ≥15° {s['roofAnomalyCount']} + 栋高偏离 ≥50% {s['heightAnomalyCount']}"],
        ])
        for key, title, cols in SITE_TABLES:
            rows = site.get(key) or []
            if not rows:
                continue
            doc.add_heading(title.replace("7.", f"{chapters['site']}.", 1), level=2)
            # 列名 → 字段名必须显式映射：旧版拿中文列名当键，取不到就写空串，
            # 面积/宽/长/均高四列全空（验收渲染图 指标说明 p025/p026）。
            # 行数不截断：这里的条数就是 json 里的对象数（7.2 有 41 条）。
            table(doc, [label for _, label in cols],
                  [[fmt(r.get(field)) for field, _ in cols] for r in rows])
        doc.add_heading(f"{chapters['site']}."
                        f"{len([t for t in SITE_TABLES if site.get(t[0])]) + 1} "
                        f"{'局限' if annotated else '使用边界'}", level=2)
        para(doc, "① **四类互相重叠**：硬地 / 空地 / 低矮体量 / 低洼是同一批格子的不同切法，"
                          "不是互斥分类，**不可相加**。")
        para(doc, "② **硬地分不出材质**：车行道、人行道、广场、铺装在几何上一样。")
        para(doc, "③ **低矮体量需人工判**：可能是违建棚屋，也可能是花坛、台阶、车辆、堆料。")
        para(doc, "④ **空地不等于闲置**：可能是待建预留地、拆迁场地、施工临时堆场。")
        para(doc, "⑤ **破损线索不是危房**：倾斜模型看不到结构内部，"
                          "体量离群也可能是正常的复式/加建；须人工核。")

    # ---- 术语表：把算法词一次性翻译掉 ----
    chapter(doc, T["terms"])
    para(doc, "这份文件里的算法词第一次出现时都会带解释，这里汇总一遍，方便打印出来贴在手边。")
    terms = _terms_for(_doc_text(doc), annotated)
    table(doc, ["术语", "什么意思"], [[term, meaning] for term, meaning in terms])
    doc.save(path)
    return toc


def build_trace_line(model: dict | None, ind: dict) -> str:
    """一行溯源：模型包文件名 + 生成时间 + 流水线版本。

    对外引用时最常被追问「这数是哪来的」——三样摆在一行，不用翻过程文件。
    """
    parts = []
    src = (model or {}).get("source") or {}
    zip_name = (src.get("zip") or {}).get("name") or src.get("name")
    if zip_name:
        sha = (src.get("zip") or {}).get("sha256")
        parts.append(f"模型包 {zip_name}"
                     + (f"（sha256 {sha[:12]}…）" if sha else ""))
    else:
        parts.append("模型包（未记录）")
    parts.append(f"生成时间 {ind.get('generatedAt', '—')}")
    producer = (model or {}).get("producer") or {}
    if producer:
        parts.append(f"流水线 {producer.get('name', '—')} {producer.get('version', '')}".strip())
    else:
        parts.append(f"流水线 {ind.get('producer', {}).get('name', '—')} "
                     f"{ind.get('producer', {}).get('version', '')}".strip())
    return "　|　".join(parts)


def build_workbook(ind: dict, path: str, ext: dict | None = None,
                   mat: dict | None = None, site: dict | None = None,
                   model: dict | None = None, annotated: bool = True) -> None:
    """成果 xlsx：**一张「指标」总表**装全部指标，其余 sheet 只放非指标的辅助信息。

    为什么只有一张指标表：分开放（核心一张、扩展一张、场地要素一张）时，
    打开文件只看到「指标」那张 7 行的人会以为就这么多指标——这坑踩过。
    现在所有指标（核心 + 扩展 + 场地要素汇总）都在同一张表里，
    用「类别」列区分，用「产出步骤 / 算法id / 证据」列溯源。

    **可读性分层（本版新增）**：原来一张表 14 列混着人话和术语，
    「口径」「局限」两列各是三四百字的算法黑话，横向一拉就看不到数值。
    现在拆成三层：
      - 「导览」      ：打开先看的一页——结论、关键数字、该用哪个口径、怎么用这份文件
      - 「指标」      ：人读层——可读值（36.2% 而不是 0.361822）+ 白话说明 + 可用等级
      - 「技术口径」  ：机器/审计层——原始口径全文、局限全文、证据链，一字不改
    数值本身一个没动，只是换了个给人看的样子。
    """
    book = Workbook()
    book.remove(book.active)

    ext_metrics = (ext or {}).get("metrics") or []
    site_summary = (site or {}).get("summary") or {}

    # ---------- 统一收集：核心 + 扩展，统一成一张长表 ----------
    # 成品版不收录 C 级内部诊断指标（绿化率、道路占比这类纹理代理值），
    # 它们留在溯源版与内部诊断里，不混进交付定稿。
    core_rows = [m for m in ind["metrics"]
                 if annotated or R.level_of(m["id"])[0] != "C"]
    ext_rows = [m for m in ext_metrics
                if annotated or R.level_of(m["id"])[0] != "C"]
    all_rows = []
    for item in core_rows:
        all_rows.append(dict(item, category="核心指标", step="06", algoId="grid_2_5d",
                             evidence=bundle_ref("过程文件/06-2.5D网格指标/现状指标.json")))
    for item in ext_rows:
        is_proxy = str(item.get("algoId", "")).startswith("site.")
        all_rows.append(dict(item,
                             category="扩展指标·几何代理量" if is_proxy else "扩展指标",
                             step=item.get("step", ""), algoId=item.get("algoId", ""),
                             evidence=bundle_ref(item.get("evidence", ""))))

    # ---------- 00-导览（打开先看这一页） ----------
    guide = book.create_sheet("导览")
    scope, quality = ind["scope"], ind["quality"]
    proxy_n = sum(1 for m in ext_rows if str(m.get("algoId", "")).startswith("site."))
    guide.append([f"{project_short_name(model)} · 《现状指标》导览"])
    guide["A1"].font = Font(bold=True, size=14)
    guide.append([])
    guide.append(["编制单位", UNIT_NAME, "", ""])
    guide.append(["生成时间", str(ind.get("generatedAt", "")).replace("T", " ")[:16], "", ""])
    guide.append(["版本", "V1.0", "", ""])
    guide.append([])
    guide.append(["这份表是什么", "从一份无人机倾斜摄影三维模型（OSGB）里量出来的片区现状估算值"])
    guide.append(["模型范围", f"{fmt(scope['areaM2'])} 平方米"
                             f"（{fmt(scope['extentM']['x'])} × {fmt(scope['extentM']['y'])} 米，"
                             f"坐标系 {scope['crs']}）"])
    guide.append(["数据质量", quality["reason"] if annotated else
                  "指标可用；模型结构完整，部分指标受网格与识别口径影响，"
                  "引用时按可用等级与区间表述。"])
    # ---- 溯源行：这份成果是哪次跑出来的 ----
    # 成品版是交付定稿，不放溯源行（溯源保留在溯源版与依据对照表）。
    if annotated:
        guide.append(["溯源", build_trace_line(model, ind)])
    guide.append(["指标总数", f"{len(all_rows)} 条（核心 {len(core_rows)} 条 + "
                             f"扩展 {len(ext_rows)} 条）" if not annotated else
                  f"{len(all_rows)} 条（核心 {len(core_rows)} 条 + "
                  f"扩展 {len(ext_rows) - proxy_n} 条 + 几何代理量 {proxy_n} 条）"])
    guide.append([])
    guide.append(["怎么用这份文件", "", "", ""])
    _bold_last(guide, 1)
    guide.append(["第 1 步", "先看下面「主要指标」，这几条是这个片区最实在的结论"])
    guide.append(["第 2 步", "看「指标」表——每条都有白话解释和「怎么用」，"
                            "可用等级 A 的可以直接引用，B 的需要人工核对"] if not annotated else
                 ["第 2 步", "看「指标」表——每条都有白话解释和「怎么用」，"
                            "可用等级 A 的可以直接引用，B 的要人工核对，C 的仅供内部参考"])
    if annotated:
        guide.append(["第 3 步", "要查算法怎么算的，看「技术口径」表；"
                                "看不懂的词查「术语」表；要追溯到源文件，看「证据」列"])
    else:
        guide.append(["第 3 步", "看不懂的词查「术语」表；要复核数值口径，见《现状指标说明》第 2 节"])
    guide.append([])
    guide.append(["主要指标（核心指标）" if annotated else "主要指标（对外推荐口径）", "", "", ""])
    _bold_last(guide, 1)
    guide.append(["指标", "数值", "这个数在说什么", "怎么用"])
    _bold_last(guide, 4)
    core_order = (["building_coverage_ratio", "building_count", "building_height_p50",
                   "building_height_p90", "floor_area_ratio_est",
                   "building_count_recon", "building_height_p50_recon",
                   "greenery_ratio", "road_ratio"] if annotated else
                  ["building_coverage_ratio", "building_count_recon",
                   "building_height_p50_recon", "floor_area_ratio_est",
                   "vegetation_share", "hard_area"])
    by_id = {m["id"]: m for m in all_rows}
    for mid in core_order:
        item = by_id.get(mid)
        if not item:
            continue
        mean, use = R.plain_of(mid, item.get("method", ""))
        guide.append([item["name"],
                      R.readable_value_with_unit(item["value"], item["unit"], mid),
                      mean, use])
    if not annotated:
        cnt = by_id.get("building_count_recon")
        hgt = by_id.get("building_height_p50_recon")
        gsh, bsh = by_id.get("ground_share"), by_id.get("building_share")
        vsh = by_id.get("vegetation_share")
        picked = []
        if cnt and cnt.get("value") is not None:
            picked.append(f"建筑按栋统计 {R.readable_value(cnt['value'], cnt['unit'], cnt['id'])} 栋")
        if hgt and hgt.get("value") is not None:
            picked.append(f"高度中位数 {R.readable_value(hgt['value'], hgt['unit'], hgt['id'])} 米")
        text = "、".join(picked) + ("为对外推荐口径；" if picked else "")
        if all(m and m.get("value") is not None for m in (gsh, bsh, vsh)):
            rest = max(0.0, 1.0 - sum(float(m["value"]) for m in (gsh, bsh, vsh)))
            text += (f"地面 {float(gsh['value']) * 100:.1f}% + 建筑 "
                     f"{float(bsh['value']) * 100:.1f}% + 植被 {float(vsh['value']) * 100:.1f}%，"
                     f"其余 {rest * 100:.1f}% 为阴影、车辆、构筑物等未分类表面")
        if text:
            guide.append(["补充", text, "", ""])
    guide.append([])
    guide.append(["同一指标的两个口径", "", "", ""])
    _bold_last(guide, 1)
    guide.append(["指标", "口径甲（2.5D 网格）", "口径乙（按栋实例化）", "该用哪个"])
    _bold_last(guide, 4)
    for label, mid_a, mid_b, kind in (
            ("建筑栋数", "building_count", "building_count_recon", "count"),
            ("建筑高度 P50", "building_height_p50", "building_height_p50_recon", "p50"),
            ("建筑高度 P90", "building_height_p90", "building_height_p90_recon", "p90"),
            ("建筑面积 / 容积率", "floor_area_ratio_est", "floor_area_est_recon", "far"),
    ):
        a, b = by_id.get(mid_a), by_id.get(mid_b)
        val_a = R.readable_value_with_unit(a["value"], a["unit"], mid_a) if a else "—"
        val_b = R.readable_value_with_unit(b["value"], b["unit"], mid_b) if b else "—"
        if kind == "count":
            advice = f"对外用乙的 {val_b}；甲的网格连通块作为方法对照"
        elif kind == "p50":
            advice = f"对外用乙的 {val_b}；甲的 {val_a}为网格法结果，受大面积格子影响"
        elif kind == "p90":
            advice = "对外用乙；两个口径不同，不可直接比大小"
        else:
            advice = "甲出倍数、乙出平方米，互为印证；层高均按 3 米取值"
        guide.append([label,
                      f"{val_a}（{a['name']}）" if a else "—",
                      f"{val_b}（{b['name']}）" if b else "—",
                      advice])
    guide.append([])
    guide.append(["可用等级说明", "", "", ""])
    _bold_last(guide, 1)
    guide.append(["A · 可直接引用", "几何直接量测，含义清楚", "", ""])
    guide.append(["B · 需人工核对", "几何代理量或受参数影响，引用前人工看图确认", "", ""])
    if annotated:
        guide.append(["C · 内部诊断", "只用于判断模型/算法质量，不对外报", "", ""])
    guide.append([])
    guide.append(["使用说明", "指标为模型几何量算结果；区间表示同一指标在不同参数下的"
                               "取值范围，用于判断结论稳健性；"
                               "「—」表示该指标为单点统计值，未做参数敏感性计算", "", ""])
    _bold_last(guide, 1)
    for row in guide.iter_rows(min_row=1, max_row=guide.max_row, max_col=4):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    for col, width in (("A", 22), ("B", 44), ("C", 52), ("D", 48)):
        guide.column_dimensions[col].width = width
    guide.freeze_panes = "A2"

    # ---------- 指标（人读层） ----------
    sheet = book.create_sheet("指标")
    if annotated:
        headers = ["类别", "指标", "可读值", "数值(原始)", "单位", "区间",
                   "可用等级", "这个数在说什么", "怎么用", "置信度",
                   "指标id", "产出步骤"]
    else:
        headers = ["类别", "指标", "可读值", "单位", "区间",
                   "可用等级", "这个数在说什么", "怎么用", "置信度"]
    sheet.append(headers)
    for item in all_rows:
        mid = item["id"]
        mean, use = R.plain_of(mid, item.get("method", ""))
        letter, level_text, why = R.level_of(mid)
        row = [item["category"], item["name"],
               R.readable_value(item["value"], item["unit"], mid)]
        if annotated:
            # 占位指标（绿化/道路）不留空单元格：空栏会被当成漏填
            row.append(item["value"] if item["value"] is not None else NOT_IN_MODEL)
        row += [R.unit_cn(item["unit"]) or "无量纲",
                R.readable_range_with_unit(item.get("range"), item["unit"], mid,
                                          annotated=annotated),
                f"{level_text}｜{why}", mean, use,
                R.conf_display(item["confidence"], item["value"], item["grade"])]
        if annotated:
            row.append(mid)
            row.append(item.get("step", ""))
        sheet.append(row)
    data_end = sheet.max_row
    sheet.freeze_panes = "C2"
    last_col = get_column_letter(len(headers))
    sheet.auto_filter.ref = f"A1:{last_col}{data_end}"
    for row in sheet.iter_rows(min_row=1, max_row=data_end, max_col=len(headers)):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    if not annotated:
        # 三类占比合计不足 100% 是正常的（其余为阴影、车辆、构筑物等），
        # 表下给一句表注，避免业主以为漏了几行。
        gsh, bsh = by_id.get("ground_share"), by_id.get("building_share")
        vsh = by_id.get("vegetation_share")
        if all(m and m.get("value") is not None for m in (gsh, bsh, vsh)):
            rest = max(0.0, 1.0 - sum(float(m["value"]) for m in (gsh, bsh, vsh)))
            note_row = data_end + 2
            sheet.cell(row=note_row, column=1, value="表注").font = Font(bold=True)
            note = sheet.cell(
                row=note_row, column=2,
                value=f"地面、建筑、植被三类占比合计 "
                      f"{(1 - rest) * 100:.1f}%，其余 {rest * 100:.1f}% 为道路、铺装、"
                      f"阴影、车辆、构筑物等未分类表面，未单列指标。")
            note.alignment = Alignment(vertical="top", wrap_text=True)

    # ---------- 技术口径（机器/审计层，原文一字不改；仅溯源版） ----------
    if annotated:
        sheet = book.create_sheet("技术口径")
        sheet.append(["指标id", "指标", "值", "区间下限", "区间上限", "单位",
                      "置信度", "等级", "产出步骤", "算法id", "证据", "口径", "局限"])
        for item in all_rows:
            rng = item.get("range") or [None, None]
            sheet.append([item["id"], item["name"], item["value"], rng[0], rng[1],
                          item["unit"], item["confidence"], item["grade"],
                          item.get("step", ""), item.get("algoId", ""),
                          bundle_ref(item.get("evidence", "")), item["method"], item["limits"]])
        sheet.freeze_panes = "C2"
        sheet.auto_filter.ref = f"A1:M{sheet.max_row}"
        for row in sheet.iter_rows(min_row=1, max_row=sheet.max_row, max_col=13):
            for cell in row:
                cell.alignment = Alignment(vertical="top", wrap_text=True)

    # ---------- 场地要素明细（逐条位置清单） ----------
    if site:
        sheet = book.create_sheet("场地要素明细")
        # 一列一个物理量，列名用中文；不再把 "areaM2=597.0、widthM=..." 的
        # 原始字段串塞进「其他属性」，业主与复核人都不用猜字段名。
        headers = ["类别", "编号", "面积(m²)", "宽(m)", "长(m)", "平均坡度(°)",
                   "均高(m)", "最高(m)", "距建筑(m)", "均深(m)", "最深(m)",
                   "高度(m)", "距西边界(m)", "距南边界(m)"]
        if annotated:
            headers += ["局限", "证据"]
        sheet.append(headers)
        field_col = {"areaM2": 3, "widthM": 4, "lengthM": 5, "meanSlopeDeg": 6,
                     "heightMeanM": 7, "heightMaxM": 8, "nearBuildingM": 9,
                     "depthMeanM": 10, "depthMaxM": 11, "heightM": 12}
        # 位置改写成「距西/南边界」，不再直接摊原始局部坐标：
        # 业主看不懂 [83.75, 91.56] 是哪个原点算起的，而距边界多少米能对上总平面。
        bbox_min = (((model or {}).get("geometry") or {}).get("bboxLocal") or {}).get("min") or []
        try:
            base_x, base_y = float(bbox_min[0]), float(bbox_min[1])
        except (TypeError, ValueError, IndexError):
            base_x = base_y = None
        for key, label, cols, unit, algo in (
                ("hardPatches", "场地要素·硬地斑块",
                 ("areaM2", "widthM", "lengthM", "meanSlopeDeg"), "m²", "site.hardscape"),
                ("lowObjects", "场地要素·低矮体量（疑似搭建）",
                 ("areaM2", "heightMeanM", "heightMaxM", "nearBuildingM"), "m²", "site.annex-object"),
                ("voidPatches", "场地要素·空地斑块（待核）", ("areaM2", "widthM", "lengthM"),
                 "m²", "site.open-space"),
                ("depressions", "场地要素·低洼/积水线索", ("areaM2", "depthMeanM", "depthMaxM"),
                 "m²", "site.depression"),
                ("groundObjects", "场地要素·贴地小体量（车辆/井盖代理）", ("heightM",),
                 "m", "site.ground-object")):
            for row in (site.get(key) or []):
                out = [label, row.get("id")] + [None] * 12
                for col in cols:
                    idx = field_col.get(col)
                    if idx is not None:
                        out[idx - 1] = row.get(col)
                center = row.get("centroidLocal") or [None, None]
                if isinstance(center, dict):
                    cx, cy = center.get("x"), center.get("y")
                else:
                    cx, cy = (list(center) + [None, None])[:2]
                numeric = isinstance(cx, (int, float)) and isinstance(cy, (int, float))
                out[12] = (round(float(cx) - base_x, 1)
                           if numeric and base_x is not None else NOT_IN_MODEL)
                out[13] = (round(float(cy) - base_y, 1)
                           if numeric and base_y is not None else NOT_IN_MODEL)
                if annotated:
                    out += ["几何代理量，非语义判定",
                            f"依据数据/场地要素.json · {key}"]
                sheet.append(out)
        sheet.freeze_panes = "C2"
        last_col = get_column_letter(len(headers))
        sheet.auto_filter.ref = f"A1:{last_col}{sheet.max_row}"
        for row in sheet.iter_rows(min_row=1, max_row=sheet.max_row, max_col=len(headers)):
            for cell in row:
                cell.alignment = Alignment(vertical="top", wrap_text=True)
        for col, width in (("A", 30), ("B", 10), ("C", 10), ("D", 8), ("E", 8),
                           ("F", 10), ("G", 9), ("H", 9), ("I", 10), ("J", 9),
                           ("K", 9), ("L", 9), ("M", 12), ("N", 12)):
            sheet.column_dimensions[col].width = width

    # ---------- 辅助 sheet（都不含指标行） ----------
    # 敏感性：三档主口径 + 全局 P5 口径共四行。区间上限（如建筑密度 62.3%）
    # 就落在全局 P5 这一行，复核者不必回到过程文件才能对上（R5-P2-01）。
    sheet = book.create_sheet("敏感性")
    sheet.append(["网格边长(m)", "地面口径", "建筑密度", "建筑栋数", "高度P50(m)",
                  "高度P90(m)", "容积率(层高2.8m)", "容积率(层高3.3m)",
                  "建筑掩膜格数", "有效网格"])

    def _far(row, floor):
        table = row.get("farByFloor") or {}
        return table.get(floor, row.get("floorAreaRatio"))

    ground_label = "局部邻域 P10" if not annotated else "局部邻域 P10（主口径）"
    for row in ind["sensitivity"]:
        sheet.append([row["cellM"], ground_label, row["coverage"], row["buildingCount"],
                      row["heightP50"], row["heightP90"],
                      _far(row, "2.8"), _far(row, "3.3"),
                      row["maskCells"], row["validCells"]])
    diag = (ind.get("diagnostics") or {}).get("globalP5")
    if diag:
        sheet.append([diag.get("cellM"), "全局 P5", diag.get("coverage"),
                      diag.get("buildingCount"), diag.get("heightP50"),
                      diag.get("heightP90"), _far(diag, "2.8"), _far(diag, "3.3"),
                      diag.get("maskCells"), diag.get("validCells")])
    sheet.append([])
    sheet.append(["说明", ("前几行为局部邻域 P10 主口径的三档网格，末行为全局 P5 地面口径；"
                        "指标区间 = 各行 × 层高 2.8 / 3.3 m 的 min–max（建筑密度上限 62.3% 即"
                        "全局 P5 一行），逐格可复核。属方法敏感性，不是误差范围。") if annotated else
                  ("前几行为主口径（15 m 邻域 P10）三档网格，末行为全局 P5 地面口径；"
                   "各指标区间取这些行 × 层高 2.8 / 3.3 m 的最小值与最大值，"
                   "可在本表逐格复核（如建筑密度区间上限 62.3% 来自全局 P5 一行）。")])

    if annotated:
        sheet = book.create_sheet("范围参数")
        sheet.append(["参数", "值"])
        for key, value in ind["params"].items():
            sheet.append([R.PARAMS_CN.get(key, key), _cell_value(value)])
        if site and site.get("params"):
            sheet.append([])
            sheet.append(["— 场地要素判据（第 14 步）—", ""])
            for key, value in site["params"].items():
                sheet.append([R.PARAMS_CN.get(key, key), _cell_value(value)])

    # 原名「缺项」。正文与体检报告里对应章节都叫「边界与说明」，
    # 成果名与引用名必须一致，不留"名字 A、指向 B"。缺口只在溯源版集中交代。
    if annotated:
        sheet = book.create_sheet("边界与说明")
        # 首列与体检报告第 6 节统一叫「编号」；旧版写 code，和旁边「边界项」并排
        # 看着像两个外来字段名。
        owner_cn = {"user": "需业主提供", "parser": "解析程序",
                    "upstream": "上游工具", "algo": "算法"}
        sheet.append(["编号", "边界项", "原因", "需要什么", "责任方"])
        for item in (list(ind["gaps"]) + list((ext or {}).get("gaps") or [])
                     + (list(site.get("gaps") or []) if site else [])):
            sheet.append([item["code"], item["what"], item["why"], item["need"],
                          owner_cn.get(item["owner"], item["owner"])])
        for row in sheet.iter_rows(min_row=1, max_row=sheet.max_row, max_col=5):
            for cell in row:
                cell.alignment = Alignment(vertical="top", wrap_text=True)

    if annotated:
        sheet = book.create_sheet("诊断")
        sheet.append(["项", "值"])
        diag = ind["diagnostics"]
        sheet.append(["局部地面覆盖率", diag["coverageByGround"]["localNeighborhoodP10"]])
        sheet.append(["全局 P5 地面覆盖率", diag["coverageByGround"]["globalP5"]])
        sheet.append(["连通分量总数", diag["components"]["total"]])
        sheet.append(["最大分量格数", diag["components"]["largestCells"]])
        sheet.append(["最大分量占比", diag["components"]["largestShare"]])
        sheet.append(["小于5格的分量", diag["components"]["lt5Cells"]])
        if site_summary:
            sheet.append([])
            sheet.append(["— 场地要素摘要 —", ""])
            for key, label in (("hardAreaM2", "硬地面积 m²"), ("hardShare", "硬地占比"),
                               ("streetCount", "街巷条数"),
                               ("lowObjectCount", "低矮体量处数"),
                               ("voidPatchCount", "空地斑块数"),
                               ("depressionCount", "低洼线索处数"),
                               ("groundObjectCount", "贴地小体量处数"),
                               ("damageClueCount", "屋面异常线索条数")):
                if site_summary.get(key) is not None:
                    sheet.append([label, site_summary[key]])

    # ---- 算法溯源：每条算法 → 产出哪些指标（不是指标表，是关系表；仅溯源版） ----
    if ext_metrics and annotated:
        sheet = book.create_sheet("算法溯源")
        sheet.append(["产出步骤", "算法id", "指标数", "有值", "指标 id"])
        by_algo = {}
        for item in ext_metrics:
            by_algo.setdefault((item.get("step") or "", item.get("algoId") or ""), []).append(item)
        for (step, algo), items in sorted(by_algo.items()):
            sheet.append([step, algo, len(items),
                          sum(1 for i in items if i["value"] is not None),
                          "、".join(i["id"] for i in items)])

    # ---- 可观测类型矩阵：内部能力盘点，只进溯源版 ----
    if annotated and mat and mat.get("items"):
        sheet = book.create_sheet("可观测类型")
        sheet.append(["类型id", "类别", "类型", "扫描得到什么", "靠什么原理",
                      "开源算法供给", "本链状态", "本链链出的量（实测值）", "说明"])
        for g in (mat.get("groups") or []):
            for item in [i for i in mat["items"] if i["typeId"].startswith(g["prefix"])]:
                sheet.append([item["typeId"], g["name"], item["name"], item["what"],
                              item["principle"], item["openSourceSupply"], item["statusCn"],
                              "、".join(f"{m['name']}={m['value']}" for m in item["measured"])
                              or "（无产出）",
                              item["chain"]])
        for row in sheet.iter_rows(min_row=1, max_row=sheet.max_row, max_col=9):
            for cell in row:
                cell.alignment = Alignment(vertical="top", wrap_text=True)

    # ---- 术语表：把算法黑话翻译成人话 ----
    sheet = book.create_sheet("术语")
    sheet.append(["术语", "什么意思"])
    for term, meaning in _terms_for(_book_text(book), annotated):
        sheet.append([term, meaning])
    for row in sheet.iter_rows(min_row=1, max_row=sheet.max_row, max_col=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)

    # 打开时停在「导览」——先看这一页，再看别的。
    # 页面设置必须在保存前对每张表都设一遍：旧版没设，宽表导 PDF 时按列切页，
    # 每页只剩一条竖排字（验收渲染图 现状指标/p002 整页空白）。
    #
    # 宽度先按「表头/最长不可断词/数据最宽」统一算一遍（所有 sheet 同一套规则），
    # 再用总和决定排版：一页宽放得下就 fitToWidth=1（字不会缩到看不清），
    # 放不下才按列自然分页并重复关键列。旧版是手写死宽 + 手工挑哪几张分页，
    # 结果 场地要素明细 被切成 4 个横向带，一个对象的信息要翻 4 页才拼齐。
    title_cols = {"指标": "A:B", "技术口径": "A:B", "场地要素明细": "A:B",
                  "可观测类型": "A:C"}
    for ws in book.worksheets:
        total = fit_columns(ws, cap=42)
        wrap_all(ws)
        if ws.title in BODY_9PT:
            body_font(ws, 9)
        print_setup(ws, fit_width=total <= PAGE_WIDTH_UNITS,
                    title_cols=title_cols.get(ws.title, ""))
    if "导览" in book.sheetnames:
        book.active = book.sheetnames.index("导览")
    # 统一剥 Markdown 标记：扩展/矩阵/场地要素等上游 JSON 的说明文字常带 `**`，
    # 单元格直写会把星号原样留在成果里。这里在落盘前对全部单元格做一次净化，
    # 不改变任何数值，只去掉强调/标题标记。
    for ws in book.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if isinstance(cell.value, str):
                    cell.value = strip_markdown(cell.value)
    book.save(path)


def verify(book_path: str, health_path: str, ind_path: str, doc_health_path: str,
           doc_ind_path: str, model: dict, ind: dict, ext: dict | None = None,
           mat: dict | None = None, site: dict | None = None,
           annotated: bool = True) -> list:
    """回读核对：数值逐项一致 + docx 关键数字可检索。"""
    problems = []

    def same_number(cell, expected) -> bool:
        if expected is None:
            # 占位指标在表里写的是说明文字（不留空），回读时按 None 等价处理
            return cell is None or str(cell).strip() == NOT_IN_MODEL
        if cell is None:
            return False
        try:
            return abs(float(cell) - float(expected)) <= 1e-9
        except (TypeError, ValueError):
            return False

    book = load_workbook(book_path, data_only=True)
    ws = book["指标"]
    hdr = {h: i for i, h in enumerate(c.value for c in ws[1])}
    # 「指标」= 人读层总表。两版列数不同（成品版去掉「数值(原始)」「指标id」「产出步骤」），
    # 所以按表头取列，不写死列号。成品版不收录 C 级内部诊断指标，核对时同样跳过。
    expect_items = [m for m in list(ind["metrics"]) + list((ext or {}).get("metrics") or [])
                    if annotated or R.level_of(m["id"])[0] != "C"]
    rows = {}
    name_to_id = {m["name"]: m["id"] for m in expect_items}
    for row in ws.iter_rows(min_row=2, values_only=True):
        rid = row[hdr["指标id"]] if "指标id" in hdr else name_to_id.get(row[hdr["指标"]])
        rows[rid] = row
    for item in expect_items:
        row = rows.get(item["id"])
        if row is None:
            problems.append(f"xlsx 缺指标行 {item['id']}")
            continue
        if "数值(原始)" in hdr and not same_number(row[hdr["数值(原始)"]], item["value"]):
            problems.append(f"xlsx {item['id']}.value 不一致："
                            f"{row[hdr['数值(原始)']]!r} ≠ {item['value']!r}")
        if row[hdr["指标"]] != item["name"]:
            problems.append(f"xlsx {item['id']} 名称不一致")
        # 可读值必须能由原始值推出来（防"人读层写了个不一样的数"）
        expect_readable = R.readable_value(item["value"], item["unit"], item["id"])
        if row[hdr["可读值"]] != expect_readable:
            problems.append(f"xlsx {item['id']} 可读值不一致："
                            f"{row[hdr['可读值']]!r} ≠ {expect_readable!r}")
        expect_range = R.readable_range_with_unit(item.get("range"), item["unit"], item["id"],
                                                  annotated=annotated)
        if row[hdr["区间"]] != expect_range:
            problems.append(f"xlsx {item['id']} 区间文本不一致")
        if not row[hdr["置信度"]] or row[hdr["置信度"]] != R.conf_display(
                item["confidence"], item["value"], item["grade"]):
            problems.append(f"xlsx {item['id']} 置信度显示不一致")

    if ext and ext.get("metrics") and annotated and "算法溯源" not in book.sheetnames:
        problems.append("xlsx 缺「算法溯源」sheet")

    # 技术口径表：原始口径全文必须抄进去（这是审计依据）。唯一允许的差异是
    # Markdown 强调/标题标记——成果里不许留 `**`/`##`/`__`，写入时已剥掉，比对也按剥后文本。
    if annotated and "技术口径" not in book.sheetnames:
        problems.append("xlsx 缺「技术口径」sheet")
    if annotated:
        tech = {r[0]: r for r in book["技术口径"].iter_rows(min_row=2, values_only=True)}
        for item in list(ind["metrics"]) + list((ext or {}).get("metrics") or []):
            row = tech.get(item["id"])
            if row is None:
                problems.append(f"xlsx「技术口径」缺 {item['id']}")
                continue
            if row[11] != strip_markdown(item["method"]) or row[12] != strip_markdown(item["limits"]):
                problems.append(f"xlsx「技术口径」{item['id']} 口径/局限原文不一致")

    # 场地要素：逐条清单在独立 sheet
    if site and site.get("summary"):
        want = sum(len(site.get(k) or []) for k in
                   ("hardPatches", "lowObjects", "voidPatches", "depressions", "groundObjects"))
        if "场地要素明细" not in book.sheetnames:
            problems.append("xlsx 缺「场地要素明细」sheet")
        else:
            got = sum(1 for r in book["场地要素明细"].iter_rows(min_row=2, values_only=True)
                      if r[0])
            if got != want:
                problems.append(f"xlsx 场地要素明细行数 {got} ≠ {want}")
        # 汇总口径的硬地/街巷/低矮体量等也在总表（作为扩展指标行）
        for item in (ext or {}).get("metrics") or []:
            if str(item.get("algoId", "")).startswith("site.") and item["value"] is not None:
                row = rows.get(item["id"])
                if row is None:
                    problems.append(f"xlsx「指标」总表缺场地要素汇总行 {item['id']}")
                elif "数值(原始)" in hdr and not same_number(
                        row[hdr["数值(原始)"]], item["value"]):
                    problems.append(f"xlsx「指标」总表 {item['id']} 数值不一致："
                                    f"{row[hdr['数值(原始)']]!r} ≠ {item['value']!r}")

    # 可观测类型矩阵：sheet 存在 + 每类的实测值与扩展指标一致（仅溯源版有这张表）
    if annotated and mat and mat.get("items"):
        if "可观测类型" not in book.sheetnames:
            problems.append("xlsx 缺「可观测类型」sheet")
        else:
            mat_rows = {r[0]: r for r in book["可观测类型"].iter_rows(min_row=2, values_only=True)}
            for item in mat["items"]:
                row = mat_rows.get(item["typeId"])
                if row is None:
                    problems.append(f"xlsx 可观测类型缺行 {item['typeId']}")
                    continue
                if row[2] != item["name"] or row[6] != item["statusCn"]:
                    problems.append(f"xlsx 可观测类型 {item['typeId']} 名称/状态 不一致")
                cell = row[7]
                for m in item["measured"]:
                    if m["value"] is None:
                        continue
                    if str(m["value"]) not in str(cell):
                        problems.append(f"xlsx 可观测类型 {item['typeId']} 实测值缺 "
                                        f"{m['id']}={m['value']}")

    doc_paths = [doc_health_path, doc_ind_path]
    for path in doc_paths:
        if not os.path.isfile(path):
            problems.append(f"缺 docx：{os.path.basename(path)}")
            continue
        text = "\n".join(p.text for p in Document(path).paragraphs)
        for tbl in Document(path).tables:
            for trow in tbl.rows:
                text += "\n" + " | ".join(cell.text for cell in trow.cells)
        if path == doc_health_path:
            tokens = [str(model["tiles"]["b3dmCount"]),
                      str(model["geometry"]["triangles"]),
                      fmt(model["georeference"]["verticalOffsetM"])]
            if annotated:
                tokens += [model["source"]["name"], model["crs"]["srs"],
                           model["toolchain"][0]["sha256"]]
            else:
                tokens += [project_short_name(model), "EPSG:4544"]
            for token in tokens:
                if str(token) not in text:
                    problems.append(f"docx 体检报告缺关键值：{token}")
        else:
            for item in list(ind["metrics"]) + list((ext or {}).get("metrics") or []):
                if item["value"] is None:
                    continue
                if not annotated and R.level_of(item["id"])[0] == "C":
                    continue                      # 成品版不收录 C 级内部诊断指标
                # 成品版不写「原始 0.361822」，按人读值核；溯源版两种写法都认
                readable = R.readable_value(item["value"], item["unit"], item["id"])
                if fmt(item["value"]) not in text and readable not in text:
                    problems.append(f"docx 指标说明缺 {item['id']} 值 {fmt(item['value'])}")
    return problems


# 成品版禁用词表：命中即返工（见《Cardo四包通用规范》2.3.1）
# 三类必须断根：
#   ① 缺口话术（待补/暂缺/未给区间/见过程文件…）——不许用"如实说明"代替内容；
#   ② 内部链路与字段（上游/契约/accessor/mask 格/bbox…）——业主看不懂，也不该看；
#   ③ 写作指令与占位（可对外说/务必/别只给/XX）——模板编辑话术漏进正文。
_CLEAN_BANNED = re.compile(
    r"待补|暂缺|缺项|未提供|资料缺失|未配置|skipped|TODO|占位|"
    r"见过程文件|过程文件/|原始\s*\d|quality\.|未验证|本模型不含|需接入|"
    r"无批注版|标注版|未给区间|上游|本机|契约|accessor|mask\s*格|bbox|"
    r"可对外说|写报告|务必|别只给|读回验证|回读核对|只能当线索|不能直接当结论|"
    r"数据来源|仅供内部|内部核对|见内部|"
    r"详见|见附件|附表|附件|附录|另册|另一册|另附|随包|底稿|内部口径|内部资料|"
    r"(?:见|详见|内部|交付|问题|整改)台账|"
    r"可用于三维查看|三项与登记一致|pass（通过）|(?<![A-Za-z])XX(?![A-Za-z])", re.I)

# 成品版不得引用内部件/内部流程：业主只拿到成品版，引用一份看不到的文件
# 等于把结论建立在自己无法核对的东西上（R4-P0-01 就是这么漏的）。
_INTERNAL_DOC_NAMES = ("溯源版", "依据对照表", "过程文件", "内部件", "技术口径",
                       "可观测类型矩阵", "内部诊断")

# 「指向其他文件/册子」的第二种写法：带扩展名的文件名、书名号里的文档名。
# 三份成品之间可以互相引用（模型体检报告 / 现状指标 / 指标说明），
# 其余一律视为把读者支到包外或另一册。
_ALLOWED_SIBLINGS = ("模型体检报告", "现状指标", "指标说明", "PDF 版", "PDF 成品")
_EXTERNAL_REF = re.compile(r"[A-Za-z0-9_\-./\\]*\.(?:docx|xlsx|xlsm|pptx|pdf|md|txt|log|csv)\b"
                           r"|《[^》]{1,40}》", re.I)

# 随包依据：溯源版里的「过程文件/<步骤>/…/<文件>.json」一律改写为相对
# 依据数据/ 的路径。溯源版与依据数据 同在 内部件/现状指标/ 下，直接可落地；
# 指标说明.docx 在 内部件/指标说明/，用 ../现状指标/ 前缀。
# R5 只替了 4 份主 JSON，扩展指标的 evidence 还引用 10/11/12/13 的中间结果，
# 于是溯源版残留 64 处运行目录路径。现在**凡被引用的过程 JSON 都随包**
# （见 collect_bundle_sources），引用与副本一一对应，不留死链也不留死文件。
_PROC_JSON = re.compile(r"过程文件/\d{2}-[^/\s#·，；]+/(?:[^/\s#·，；]+/)*([^/\s#·，；]+\.json)")
_BUNDLE_FILES = ("现状指标.json", "扩展指标.json", "三维要素反演.json", "场地要素.json")


def bundle_ref(text, prefix: str = "") -> str:
    """把「过程文件/<步骤>/<文件>.json」改成「<prefix>依据数据/<文件>.json」。"""
    return _PROC_JSON.sub(lambda m: f"{prefix}依据数据/{m.group(1)}", str(text or ""))


def collect_bundle_sources(*objs) -> list:
    """收集过程 JSON 里被 evidence 引用的「过程文件/…json」源路径（去重保序）。

    依据随包不能靠一张手写白名单：上游新增一步产出，白名单外的 evidence
    就会把运行目录路径带进溯源版，离开本机即断链（R5-P1-01）。这里直接
    以「正文引用」为准反推需要复制哪些文件，引用与副本同源。
    """
    found, seen = [], set()

    def walk(node):
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, str):
            for match in _PROC_JSON.finditer(node):
                rel = match.group(0).strip()
                if rel not in seen:
                    seen.add(rel)
                    found.append(rel)

    for obj in objs:
        walk(obj)
    return found


def check_mask_consistency(ind: dict) -> list:
    """建筑密度必须等于「建筑掩膜格数 / 有效网格」。

    R4 曾把建筑掩膜格数写成「范围外网格」：读者按 3209/8869 复算恰好是 36.2%，
    文案与数值互相矛盾。这里做口径自洽校验——主口径与敏感性三档都验，
    任何一档对不上即 exit 3，避免"数出口径错"这类硬错再次流出。
    """
    problems = []
    scope = ind.get("scope") or {}
    main_value = next((m.get("value") for m in ind.get("metrics") or []
                       if m.get("id") == "building_coverage_ratio"), None)
    rows = [{"cellM": (ind.get("params") or {}).get("cellM"), "coverage": main_value,
             "maskCells": scope.get("maskCells"), "validCells": scope.get("validCells")}]
    rows += list(ind.get("sensitivity") or [])
    for row in rows:
        try:
            mask, valid = int(row["maskCells"]), int(row["validCells"])
            cov = float(row["coverage"])
        except (KeyError, TypeError, ValueError):
            problems.append(f"建筑密度口径缺项：{row!r}")
            continue
        if valid <= 0:
            problems.append(f"有效网格数不大于 0（网格 {row.get('cellM')} m）")
            continue
        ratio = mask / valid
        if abs(ratio - cov) > 5e-5:
            problems.append(
                f"建筑密度口径不自洽（网格 {row.get('cellM')} m）："
                f"掩膜格 {mask}/{valid} = {ratio:.6f} ≠ 建筑密度 {cov:.6f}")
    return problems


def scan_external_refs(book_path: str, doc_paths: list, pdf_paths: list | None = None) -> list:
    """成品版不得把读者支到别的文件/册子（R5-P2-02 的「换个说法」）。

    两类命中：带扩展名的文件名（.docx/.xlsx/.json/.pdf…）、书名号里非三份成品的文档名。
    同包三份成品之间可以互指（《模型体检报告》《现状指标》《指标说明》）。
    """
    problems = []

    def check(where: str, text: str) -> None:
        for hit in _EXTERNAL_REF.finditer(text or ""):
            token = hit.group(0)
            if any(name in token for name in _ALLOWED_SIBLINGS):
                continue
            problems.append(f"{where} 出现指向其他文件/册子的表述 {token!r}："
                            f"{(text or '')[:60]!r}")
            break

    for path in doc_paths:
        if not os.path.isfile(path):
            continue
        name = os.path.basename(path)
        doc = Document(path)
        for index, p in enumerate(doc.paragraphs, start=1):
            check(f"{name} 第 {index} 段", p.text)
        for ti, tbl in enumerate(doc.tables, start=1):
            for ri, row in enumerate(tbl.rows, start=1):
                for ci, cell in enumerate(row.cells, start=1):
                    check(f"{name} 表 {ti} 行 {ri} 列 {ci}", cell.text)
    if os.path.isfile(book_path):
        wb = load_workbook(book_path)
        name = os.path.basename(book_path)
        for ws in wb.worksheets:
            for row in ws.iter_rows():
                for cell in row:
                    if isinstance(cell.value, str):
                        check(f"{name}/{ws.title}!{cell.coordinate}", cell.value)
    for path in pdf_paths or []:
        if not os.path.isfile(path):
            continue
        try:
            import pdf_render
            text = pdf_render.pdf_text(path)
        except Exception as exc:                       # noqa: BLE001
            problems.append(f"{os.path.basename(path)} 读不出文本：{type(exc).__name__}: {exc}")
            continue
        check(f"{os.path.basename(path)} 全文", re.sub(r"\s+", "", text))
    return problems


def scan_pdf_vocabulary(pdf_paths: list) -> list:
    """PDF 成品也要过词表：PDF 是交付件，不能只在 docx 里扫干净。"""
    problems = []
    try:
        import pdf_render
    except ImportError as exc:                          # pragma: no cover
        return [f"PDF 渲染模块不可用：{type(exc).__name__}: {exc}"]
    for path in pdf_paths:
        if not os.path.isfile(path):
            problems.append(f"缺成品 PDF：{os.path.basename(path)}")
            continue
        flat = re.sub(r"\s+", "", pdf_render.pdf_text(path))
        name = os.path.basename(path)
        hit = _CLEAN_BANNED.search(flat)
        if hit:
            problems.append(f"{name} 命中成品版禁用词 {hit.group(0)!r}（PDF 文本）")
        for internal in _INTERNAL_DOC_NAMES:
            if internal in flat:
                problems.append(f"{name} 引用内部件名 {internal!r}（PDF 文本）")
                break
    return problems


def scan_internal_paths(book_path: str, doc_paths: list) -> list:
    """溯源版不得再出现「过程文件/」：离开本次运行目录就断链，必须改包内相对路径。"""
    problems = []

    def check(where: str, text) -> None:
        value = str(text or "")
        if "过程文件/" in value or "过程文件\\" in value:
            problems.append(f"{where} 仍指向运行目录：{value[:80]!r}")

    for path in doc_paths:
        if not os.path.isfile(path):
            continue
        name = os.path.basename(path)
        doc = Document(path)
        for index, p in enumerate(doc.paragraphs, start=1):
            check(f"{name} 第 {index} 段", p.text)
        for ti, tbl in enumerate(doc.tables, start=1):
            for ri, row in enumerate(tbl.rows, start=1):
                for ci, cell in enumerate(row.cells, start=1):
                    check(f"{name} 表 {ti} 行 {ri} 列 {ci}", cell.text)
    if os.path.isfile(book_path):
        wb = load_workbook(book_path)
        name = os.path.basename(book_path)
        for ws in wb.worksheets:
            for row in ws.iter_rows():
                for cell in row:
                    if isinstance(cell.value, str):
                        check(f"{name}/{ws.title}!{cell.coordinate}", cell.value)
    return problems


def check_sensitivity_span(ind: dict) -> list:
    """区间必须能在包内按敏感性表复算（R5-P2-01）。

    区间 = 三档主口径（局部地面）× 两种层高 + 全局 P5 口径 的 min–max。
    06 已把全局 P5 整行写进 diagnostics.globalP5；这里逐项核对区间上下限，
    对不上即 exit 3——避免出现「62.3% 在表里找不到出处」。
    """
    rows = list(ind.get("sensitivity") or [])
    diag = (ind.get("diagnostics") or {}).get("globalP5")
    if not rows:
        return ["敏感性表为空，区间无法复算"]
    problems = []

    def span(values):
        values = [v for v in values if v is not None]
        return (min(values), max(values)) if values else (None, None)

    metrics = {m.get("id"): m for m in ind.get("metrics") or []}
    checks = []

    def add(metric_id, values, tol):
        target = metrics.get(metric_id) or {}
        rng = target.get("range")
        if not rng or len(rng) != 2:
            return
        lo, hi = span(values)
        if lo is None:
            problems.append(f"{metric_id} 区间无法复算：表内没有取值")
            return
        if abs(lo - float(rng[0])) > tol or abs(hi - float(rng[1])) > tol:
            problems.append(f"{metric_id} 区间与表内 min–max 不一致："
                            f"表内 {lo:.6g}–{hi:.6g} vs 区间 {rng[0]}–{rng[1]}")
        checks.append(metric_id)

    cov = [r.get("coverage") for r in rows]
    cnt = [r.get("buildingCount") for r in rows]
    h50 = [r.get("heightP50") for r in rows]
    h90 = [r.get("heightP90") for r in rows]
    far = [r.get("farByFloor", {}).get(k) for r in rows for k in ("2.8", "3.3")]
    if diag:
        cov.append(diag.get("coverage"))
        cnt.append(diag.get("buildingCount"))
        h50.append(diag.get("heightP50"))
        h90.append(diag.get("heightP90"))
        far += [diag.get("farByFloor", {}).get(k) for k in ("2.8", "3.3")]
    else:
        problems.append("缺 diagnostics.globalP5：全局 P5 口径未落盘，区间无法在包内复算")
    add("building_coverage_ratio", cov, 1e-6)
    add("building_count", cnt, 0)
    add("building_height_p50", h50, 0.006)
    add("building_height_p90", h90, 0.006)
    add("floor_area_ratio_est", far, 5e-4)
    return problems


def scan_clean_vocabulary(book_path: str, doc_paths: list) -> list:
    """成品版词表闸门：扫全文（段落 + 表格单元格），命中缺口提示/内部备注即拦截。

    为什么单列一道闸：文案分支靠人写，写漏一句「未提供」就会把内部语气带到业主手里；
    这道扫描是最后一道自动兜底，宁可导出失败也不放行。
    """
    problems = []

    def check(where: str, text: str) -> None:
        hit = _CLEAN_BANNED.search(text or "")
        if hit:
            problems.append(f"{where} 命中成品版禁用词 {hit.group(0)!r}：{(text or '')[:60]!r}")
        for name in _INTERNAL_DOC_NAMES:
            if name in (text or ""):
                problems.append(f"{where} 引用内部件名 {name!r}（成品版必须自足）："
                                f"{(text or '')[:60]!r}")
                break

    for path in doc_paths:
        if not os.path.isfile(path):
            problems.append(f"缺成品版文件：{os.path.basename(path)}")
            continue
        name = os.path.basename(path)
        doc = Document(path)
        for index, p in enumerate(doc.paragraphs, start=1):
            check(f"{name} 第 {index} 段", p.text)
        for ti, tbl in enumerate(doc.tables, start=1):
            for ri, row in enumerate(tbl.rows, start=1):
                for ci, cell in enumerate(row.cells, start=1):
                    check(f"{name} 表 {ti} 行 {ri} 列 {ci}", cell.text)

    if os.path.isfile(book_path):
        wb = load_workbook(book_path)
        name = os.path.basename(book_path)
        for ws in wb.worksheets:
            for row in ws.iter_rows():
                for cell in row:
                    if isinstance(cell.value, str):
                        check(f"{name}/{ws.title}!{cell.coordinate}", cell.value)
    else:
        problems.append(f"缺成品版文件：{os.path.basename(book_path)}")
    return problems


def verify_clean_vs_annotated(clean_book: str, ann_book: str,
                              ind: dict, ext: dict | None = None) -> list:
    """两份同源自校验：成品版与溯源版的指标行逐条同值、同序。

    成品版不收录 C 级内部诊断指标、也没有「产出步骤」列，所以比对时
    先按名称剔除 C 级行，再只比「类别/指标/可读值/区间/置信度」五列。
    """
    problems = []
    skip = {m["name"] for m in list(ind["metrics"]) + list((ext or {}).get("metrics") or [])
            if R.level_of(m["id"])[0] == "C"}

    def table(path, drop_internal):
        wb = load_workbook(path, data_only=True)
        ws = wb["指标"]
        hdr = {h: i for i, h in enumerate(c.value for c in ws[1])}
        out = []
        for row in ws.iter_rows(min_row=2, values_only=True):
            if row[hdr["类别"]] not in ("核心指标", "扩展指标"):
                continue                       # 跳过表注等非指标行
            if drop_internal and row[hdr["指标"]] in skip:
                continue
            out.append((row[hdr["类别"]], row[hdr["指标"]], row[hdr["可读值"]],
                        row[hdr["区间"]], row[hdr["置信度"]]))
        return out

    a = table(clean_book, False)
    b = table(ann_book, True)

    def norm(row):
        # 成品版对单点指标写「—」，溯源版写「单点值…」：语义相同，比对时归一。
        rng = row[3]
        if (rng is None or str(rng).strip() in ("", "—")
                or str(rng).startswith(("未给区间", "单点值"))):
            rng = None
        # 置信度文案两版一致；等级只在「可用等级」列，不在比对列里
        return (row[0], row[1], row[2], rng, row[4])

    if len(a) != len(b):
        problems.append(f"两版指标行数不一致：成品 {len(a)} ≠ 溯源 {len(b)}")
    for i, (ra, rb) in enumerate(zip(a, b), start=2):
        if norm(ra) != norm(rb):
            problems.append(f"两版第 {i} 行不一致：{norm(ra)!r} ≠ {norm(rb)!r}")
    return problems


def scan_markdown_residue(book_path: str, doc_paths: list) -> list:
    """导出后自检：扫 xlsx 全表单元格 + docx 段落/表格，命中 `**`/`##`/`__` 即报错。

    为什么要独立扫一遍：写入端净化可能漏掉某条新加的直写路径；回读扫描是最后一道闸，
    宁可让导出失败，也不让半成品标记流到会上。
    """
    problems = []
    if not os.path.isfile(book_path):
        return [f"缺 xlsx：{os.path.basename(book_path)}"]
    wb = load_workbook(book_path)
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if isinstance(cell.value, str):
                    hit = _MD_RESIDUE.search(cell.value)
                    if hit:
                        problems.append(
                            f"{os.path.basename(book_path)}/{ws.title}!{cell.coordinate} "
                            f"残留 Markdown 标记 {hit.group(0)!r}：{cell.value[:50]!r}")
    for path in doc_paths:
        if not os.path.isfile(path):
            continue
        doc = Document(path)
        for para_obj in doc.paragraphs:
            hit = _MD_RESIDUE.search(para_obj.text)
            if hit:
                problems.append(f"{os.path.basename(path)} 段落残留 {hit.group(0)!r}："
                                f"{para_obj.text[:50]!r}")
        for tbl in doc.tables:
            for trow in tbl.rows:
                for cell in trow.cells:
                    hit = _MD_RESIDUE.search(cell.text)
                    if hit:
                        problems.append(f"{os.path.basename(path)} 表格残留 {hit.group(0)!r}："
                                        f"{cell.text[:50]!r}")
    return problems


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def write_export_log(args, targets: list, model: dict | None, ind: dict | None,
                     ext: dict | None, mat: dict | None, site: dict | None,
                     status: str = "ok", stage: str = "", exit_code: int = 0,
                     problems: list | None = None, audit: dict | None = None,
                     browse: dict | None = None) -> None:
    """写 过程文件/07-成果导出/导出日志.json（成功与失败都写）。

    记录：① 本次运行结论（status / stage / exitCode / problems）——失败时写明
    卡在哪一步、退出码多少、为什么；② 四件成果**当时**的落盘清单（在不在、
    字节数、sha256）；③ 输入源指纹；④ 空白页自检的档位与未执行项。

    旧版只在成功路径写日志，07 一失败日志就停在上一次成功的那版上，
    事后拿它对账会把旧成果当成这次的——留痕反而误导。
    """
    log_dir = os.path.join("过程文件", "07-成果导出")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "导出日志.json")

    deliverables = []
    for path in targets:
        if not os.path.isfile(path):
            deliverables.append({"file": os.path.basename(path), "present": False})
            continue
        deliverables.append({
            "file": os.path.basename(path),
            "present": True,
            "bytes": os.path.getsize(path),
            "sha256": sha256_of(path),
        })

    sources = []
    for label, path in (("model", args.model), ("health", args.health),
                        ("indicators", args.indicators), ("extended", args.extended),
                        ("matrix", args.matrix), ("site", args.site)):
        if os.path.isfile(path):
            sources.append({"role": label, "path": path.replace("\\", "/"),
                            "bytes": os.path.getsize(path), "sha256": sha256_of(path)})
        else:
            sources.append({"role": label, "path": path.replace("\\", "/"), "present": False})

    doc = {
        "schema": "cardo/lowair-export-log/v1",
        "schemaVersion": 1,
        "generatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "producer": {"name": "cardo 低空解析 · 07 成果导出", "version": "1.0.0"},
        "status": status,
        "stage": stage,
        "exitCode": exit_code,
        "problems": list(problems or []),
        "deliverables": deliverables,
        "deliverableCount": sum(1 for item in deliverables if item["present"]),
        "sources": sources,
    }
    if browse:
        # 模型浏览目录也是一件成果：留痕体积、文件数、引擎版本与工程包标识
        doc["browse"] = dict(browse)
    if audit:
        # 自检档位必须留痕：事后要能说清这批成果是「渲染级逐页验过」
        # 还是「只做了结构级检查」——两者的保证强度不同。
        doc["blankPageAudit"] = {
            "mode": audit.get("mode"),
            "officeProbe": audit.get("officeProbe"),
            "skipped": list(audit.get("skipped") or []),
            "warnings": list(audit.get("warnings") or []),
            "blankPageCount": audit.get("blankPageCount"),
        }
    if model is not None:
        doc["scope"] = {
            "areaM2": (ind or {}).get("scope", {}).get("areaM2"),
            "crs": (ind or {}).get("scope", {}).get("crs"),
            "modelName": (model.get("source") or {}).get("name"),
        }
    if ind is not None:
        doc["metrics"] = {
            "core": len(ind.get("metrics") or []),
            "extended": len((ext or {}).get("metrics") or []),
            "siteFeatureRows": sum(len((site or {}).get(k) or []) for k in
                                   ("hardPatches", "lowObjects", "voidPatches",
                                    "depressions", "groundObjects")),
            "observableTypes": len((mat or {}).get("items") or []),
        }
    with open(log_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="07 · 成果导出（docx/xlsx）")
    ap.add_argument("--model", default=MODEL_DEFAULT)
    ap.add_argument("--health", default=HEALTH_DEFAULT)
    ap.add_argument("--indicators", default=IND_DEFAULT)
    ap.add_argument("--extended", default=EXT_DEFAULT,
                    help="扩展指标 JSON（可选；不存在则只出 06 的基础指标）")
    ap.add_argument("--matrix", default=MATRIX_DEFAULT,
                    help="可观测类型矩阵 JSON（可选；不存在则不出能力边界表）")
    ap.add_argument("--site", default=SITE_DEFAULT,
                    help="场地要素 JSON（可选；不存在则不出几何代理量明细）")
    ap.add_argument("--out", default=OUT_DEFAULT)
    ap.add_argument("--workspace", default="",
                    help="工作区根（缺省按 --out 向上找 工作区参数.json，不依赖 cwd）")
    ap.add_argument("--convert-log", default=CONVERT_LOG_DEFAULT,
                    help="03 转换日志（取输入 LOD 计数，用于体检报告结构表）")
    args = ap.parse_args()

    workspace = _workspace_root(args.out, args.workspace)
    global _PROJECT_NAME
    _PROJECT_NAME = config_project_name(workspace)

    out_dir = os.path.abspath(args.out)
    targets = [os.path.join(out_dir, rel) for rel in REL_TARGETS]
    health_clean, health_ann = targets[0], targets[1]
    ind_clean, ind_ann = targets[2], targets[3]
    book_clean, book_ann = targets[4], targets[5]
    evidence_book = targets[6]
    health_pdf, ind_pdf = targets[7], targets[8]
    for t in targets:
        os.makedirs(os.path.dirname(t), exist_ok=True)

    # 旧版式清扫：只动本包旧命名/旧目录里由本包产出的文件，别的文件一概不碰。
    # （旧版把两版平铺在 成果文件/模型体检报告/ 等目录，重跑会留下同名多份的假象）
    legacy_files = {f"{DOC_HEALTH}_{VARIANT}.docx" for VARIANT in ("无批注版", "标注版")}
    legacy_files |= {f"{BOOK}_{VARIANT}.xlsx" for VARIANT in ("无批注版", "标注版")}
    legacy_files |= {f"{DOC_IND}_{VARIANT}.docx" for VARIANT in ("无批注版", "标注版")}
    for legacy_dir in (DOC_HEALTH, BOOK, DOC_IND):
        path = os.path.join(out_dir, legacy_dir)
        if not os.path.isdir(path):
            continue
        for name in list(os.listdir(path)):
            if name in legacy_files or name == EVIDENCE_BOOK:
                try:
                    os.remove(os.path.join(path, name))
                except OSError:
                    pass
        try:
            if not os.listdir(path):
                os.rmdir(path)
        except OSError:
            pass
    model = health = ind = ext = mat = site = None

    def fail(stage: str, message: str, code: int = 3,
             problems: list | None = None) -> int:
        """失败也留痕：卡在哪一步、退出码、原因、当时已落盘的文件与哈希。"""
        print(f"[07] {message}", file=sys.stderr)
        write_export_log(args, targets, model, ind, ext, mat, site,
                         status="failed", stage=stage, exit_code=code,
                         problems=problems or [message])
        return code

    for label, path in (("model", args.model), ("health", args.health),
                        ("indicators", args.indicators)):
        if not os.path.isfile(path):
            return fail("输入缺失", f"缺输入 {label}：{path}", 2)
    model, health, ind = read_json(args.model), read_json(args.health), read_json(args.indicators)
    if health.get("schema") != "cardo/lowair-health/v1":
        return fail("输入缺失", "体检.json schema 不符", 2)
    if ind.get("schema") != "cardo/lowair-indicators/v1":
        return fail("输入缺失", "现状指标.json schema 不符", 2)
    mask_problems = check_mask_consistency(ind)
    if mask_problems:
        return fail("指标口径自洽",
                    f"建筑密度与掩膜格数不自洽（{len(mask_problems)} 条）："
                    + "；".join(mask_problems[:4]), 3, mask_problems)
    span_problems = check_sensitivity_span(ind)
    if span_problems:
        return fail("敏感性区间复算",
                    f"区间无法按敏感性表复算（{len(span_problems)} 条）："
                    + "；".join(span_problems[:4]), 3, span_problems)

    ext = None
    if os.path.isfile(args.extended):
        try:
            ext = read_json(args.extended)
        except (OSError, ValueError) as exc:
            print(f"[07] 扩展指标不可读（忽略扩展部分）：{type(exc).__name__}: {exc}",
                  file=sys.stderr)
            ext = None
        if ext and ext.get("schema") != "cardo/lowair-extended-indicators/v1":
            print(f"[07] 扩展指标 schema 不符（忽略扩展部分）：{ext.get('schema')}", file=sys.stderr)
            ext = None

    os.makedirs(out_dir, exist_ok=True)
    # 先删旧再生成：失败不留下"上次的成果"；顺带清掉旧命名（扁平四件）残留
    legacy = [os.path.join(out_dir, name) for name in
              ("模型体检报告.docx", "指标说明.docx", "现状指标.xlsx",
               "现状指标依据对照表.xlsx")]
    for stale in targets + legacy:
        if os.path.isfile(stale):
            os.remove(stale)

    mat = None
    if os.path.isfile(args.matrix):
        try:
            mat = read_json(args.matrix)
        except (OSError, ValueError) as exc:
            print(f"[07] 可观测类型矩阵不可读（忽略该部分）：{type(exc).__name__}: {exc}",
                  file=sys.stderr)
            mat = None
        if mat and mat.get("schema") != "cardo/lowair-observable-matrix/v1":
            print(f"[07] 可观测类型矩阵 schema 不符（忽略该部分）：{mat.get('schema')}",
                  file=sys.stderr)
            mat = None

    site = None
    if os.path.isfile(args.site):
        try:
            site = read_json(args.site)
        except (OSError, ValueError) as exc:
            print(f"[07] 场地要素不可读（忽略该部分）：{type(exc).__name__}: {exc}",
                  file=sys.stderr)
            site = None
        if site and site.get("schema") != "cardo/lowair-site-features/v1":
            print(f"[07] 场地要素 schema 不符（忽略该部分）：{site.get('schema')}", file=sys.stderr)
            site = None

    # 输入 LOD 计数来自 03 转换日志（体检.json 只记产物侧）：缺了就写「—」，
    # 不拿"见转换日志.json"这种内部路径糊过去——业主看不懂，也不该由业主去翻。
    input_lods = {}
    if os.path.isfile(args.convert_log):
        try:
            input_lods = (read_json(args.convert_log).get("view") or {}).get("lods") or {}
        except (OSError, ValueError):
            input_lods = {}

    try:
        build_health_doc(model, health, health_clean, annotated=False, input_lods=input_lods)
        build_health_doc(model, health, health_ann, annotated=True, input_lods=input_lods)
        toc_entries = build_indicator_doc(ind, ind_clean, ext, mat, site, model, annotated=False)
        build_indicator_doc(ind, ind_ann, ext, mat, site, model, annotated=True)
        build_workbook(ind, book_clean, ext, mat, site, model, annotated=False)
        build_workbook(ind, book_ann, ext, mat, site, model, annotated=True)
    except (OSError, KeyError, ValueError, TypeError) as exc:
        return fail("写盘", f"写盘失败：{type(exc).__name__}: {exc}")

    # 成果里不许留 `**`/`##`/`__` 等 Markdown 残留：扫全表，命中即失败退出。
    docx_all = [health_clean, health_ann, ind_clean, ind_ann]
    residue = scan_markdown_residue(book_clean, docx_all)
    residue += scan_markdown_residue(book_ann, [])
    if residue:
        return fail("Markdown 残留",
                    f"成果含 Markdown 残留标记（共 {len(residue)} 处，前 8 处）："
                    + "；".join(residue[:8]), 3, residue)

    problems = verify(book_ann, args.health, args.indicators, health_ann, ind_ann,
                      model, ind, ext, mat, site, annotated=True)
    problems += verify(book_clean, args.health, args.indicators, health_clean, ind_clean,
                       model, ind, ext, mat, site, annotated=False)
    problems += verify_clean_vs_annotated(book_clean, book_ann, ind, ext)
    if problems:
        return fail("回读核对",
                    f"回读核对未通过（共 {len(problems)} 条，前 8 条）："
                    + "；".join(problems[:8]), 3, problems)

    # ---- 成品版词表闸门：缺口提示 / 内部备注 / 占位，一处都不许留 ----
    vocab = scan_clean_vocabulary(book_clean, [health_clean, ind_clean])
    if vocab:
        return fail("成品版词表",
                    f"成品版命中禁用词（共 {len(vocab)} 处，前 8 处）："
                    + "；".join(vocab[:8]), 3, vocab)

    # 溯源版也不许把依据挂在运行目录上：过程文件/ 一律改 依据数据/（R5-P1-01）。
    leak = scan_internal_paths(book_ann, [health_ann, ind_ann])
    if leak:
        return fail("依据随包",
                    f"溯源版仍有 {len(leak)} 处指向过程文件/（前 8 处）："
                    + "；".join(leak[:8]), 3, leak)

    # 指标依据对照表：每个数 → 算法 / 参数 / 输入图层 / 局限，口径不全或区间不包主值即拦
    # 依据随包：把对照表引用的源 JSON 复制到 内部件/现状指标/依据数据/，
    # 对照表里的依据写成相对该目录的路径——单拷 成果文件/ 也不会断链（R4-P1-03）。
    bundle_dir = os.path.join(os.path.dirname(evidence_book), "依据数据")
    os.makedirs(bundle_dir, exist_ok=True)
    for stale in os.listdir(bundle_dir):
        if stale.endswith(".json"):
            try:
                os.remove(os.path.join(bundle_dir, stale))
            except OSError:
                pass
    # 核心依据（正文明细里硬编码引用）+ 正文 evidence 实际引用到的中间 JSON。
    # 只认「被引用」的，不把整个 过程文件/ 拖进包里——包内不该留没人读的死文件
    # （R5-P1-01c：三维要素反演.json 随包却 0 引用）。图片/汇总 JSON 若无引用
    # 自然不会被复制，包内每一份依据都能在正文里找到出处。
    fixed = ["06-2.5D网格指标/现状指标.json"]
    if ext:                       # 首跑（14 步之前）没有扩展指标，不引用也不该要求它在
        fixed.append("14-扩展指标/扩展指标.json")
    want = [os.path.join("过程文件", *rel.split("/")) for rel in fixed]
    for rel in collect_bundle_sources(ind, ext, site):
        src = rel.replace("/", os.sep)
        if src not in want:
            want.append(src)
    picked = {}
    for src in want:
        base = os.path.basename(src)
        if base in picked and picked[base] != src:
            return fail("依据随包",
                        f"依据数据/ 出现同名不同源：{picked[base]} 与 {src}，"
                        f"平铺复制会互相覆盖", 3)
        picked[base] = src
        if os.path.isfile(src):
            shutil.copyfile(src, os.path.join(bundle_dir, base))
    missing_src = [src for src in picked.values() if not os.path.isfile(src)]
    if missing_src:
        return fail("依据随包",
                    f"正文引用的依据文件不存在（{len(missing_src)} 份）："
                    + "；".join(missing_src[:6]), 3)
    try:
        import build_evidence_xlsx as evidence
        evidence_code, evidence_problems, _ = evidence.write(
            ind, os.path.dirname(os.path.abspath(args.indicators)), evidence_book,
            docx_path=ind_clean, site=site, site_tables=SITE_TABLES,
            bundle_dir=bundle_dir, bundle_rel="依据数据")
    except (OSError, KeyError, ValueError, TypeError) as exc:
        return fail("依据对照表", f"依据对照表写盘失败：{type(exc).__name__}: {exc}")
    if evidence_code != 0:
        return fail("依据对照表",
                    f"依据对照表未通过（共 {len(evidence_problems)} 条，前 8 条）："
                    + "；".join(evidence_problems[:8]), 3, evidence_problems)

    # ---- 空白页自检：四件成果逐页数字符/图片，0 字符 0 图即 exit 3 ----
    # 指标说明曾因「章末空段落 + 显式分页符」多出一整张白纸，只能靠人眼翻页发现。
    # 现在把四件渲成 PDF 逐页统计，空白页当场拦；同时卡**字号底线 7pt**——
    # 表格正文、表注、页脚都算，任何一张 sheet 缩到 7pt 以下即 exit 3。
    # 「指标依据」另允许最多 2 页（整段分，不靠缩字凑一页），所以写 @2!7。
    # 渲染级要 Word/Excel 的 COM；本机没装 Office 时自检模块自动降级为**结构级**
    # （读 docx/xlsx 内部结构判空白页前兆，字号底线照旧卡），不阻断整链。
    try:
        import blank_page_guard as guard
    except ImportError as exc:
        return fail("空白页自检", f"空白页自检模块不可用：{type(exc).__name__}: {exc}")
    # 依据对照表覆盖全部指标（45 行以上），不再限制页数；只卡字号与空白页。
    guard_specs = docx_all + [book_clean, book_ann, evidence_book,
                              evidence_book + "::指标依据!7"]
    try:
        guard_code, guard_problems, guard_report = guard.run(
            guard_specs, os.path.join("过程文件", "07-成果导出", "空白页自检.json"),
            min_font=7.0)
    except (OSError, ValueError, RuntimeError, TypeError) as exc:
        return fail("空白页自检", f"空白页自检执行失败：{type(exc).__name__}: {exc}")
    if guard_code != 0:
        return fail("空白页自检",
                    f"空白页自检未通过（共 {len(guard_problems)} 条）："
                    + "；".join(guard_problems[:6]), 3, guard_problems)
    # 降级不等于失败，但必须留痕：自检档位写进导出日志，事后能说清这批成果
    # 是「渲染级逐页验过」还是「只做了结构级检查」。
    guard_mode = (guard_report or {}).get("mode", "render")
    for note in (guard_report or {}).get("warnings") or []:
        print(f"[07] 提示（空白页自检）：{note}")
    for note in (guard_report or {}).get("skipped") or []:
        print(f"[07] 未执行（空白页自检）：{note}")

    # ---- 成品 PDF + 目录页码静态化（R5-P1-02）----
    # docx 的 TOC / PAGE 都是域，不更新域就看不出页码；这里用自有渲染链路
    # （docx → HTML → Edge 无头打印 → PDF）算实际分页，把页码写回 docx 目录，
    # 并把「第 X 页 / 共 N 页」烘进 PDF 版面，PDF 作为成品版交付件之一。
    try:
        import pdf_render
    except ImportError as exc:
        return fail("成品 PDF", f"PDF 渲染模块不可用：{type(exc).__name__}: {exc}")
    try:
        health_info = pdf_render.render_docx(health_clean, health_pdf)
        ind_info = pdf_render.render_docx(ind_clean, ind_pdf)
        pages = dict(ind_info.get("headingPages") or {})
        filled = fill_toc_pages(ind_clean, toc_entries or [], pages)
        # 回填后目录行数不变，分页应当稳定；若因折行变动，用新页码再回填一次。
        for _ in range(2):
            again = pdf_render.render_docx(ind_clean, ind_pdf)
            if (again.get("headingPages") or {}) == pages:
                ind_info = again
                break
            pages = dict(again.get("headingPages") or {})
            reset_toc_pages(ind_clean)
            filled = fill_toc_pages(ind_clean, toc_entries or [], pages)
            ind_info = again
        else:
            return fail("目录页码", "目录页码在两次渲染间不稳定，无法静态回填")
    except RuntimeError as exc:
        return fail("成品 PDF", f"PDF 渲染失败：{exc}")
    except (OSError, ValueError, TypeError) as exc:
        return fail("成品 PDF", f"PDF 写出失败：{type(exc).__name__}: {exc}")
    if filled < len(toc_entries or []):
        return fail("目录页码",
                    f"目录页码回填不全：{filled}/{len(toc_entries or [])} 条")
    # R6-P2-01：目录页上排的就是条目名，标签与正文标题逐字相同时会先命中目录页，
    # 页码指回目录自身。索引已跳过目录页，这里再守一道：回填值不得落在目录页。
    toc_own = set(ind_info.get("tocPages") or [])
    back_ref = sorted(p for p in set(pages.values()) if p in toc_own)
    if back_ref:
        return fail("目录页码",
                    f"目录条目页码指向目录页自身（第 {back_ref} 页）：索引未跳过目录页")
    empty = (health_info.get("emptyPages") or []) + (ind_info.get("emptyPages") or [])
    if empty:
        return fail("成品 PDF", f"PDF 出现空白页：{empty}")
    pdf_vocab = scan_pdf_vocabulary([health_pdf, ind_pdf])
    pdf_vocab += scan_external_refs(book_clean, [health_clean, ind_clean],
                                    [health_pdf, ind_pdf])
    if pdf_vocab:
        return fail("成品版词表",
                    f"成品版/PDF 命中禁用词或外部引用（共 {len(pdf_vocab)} 处，前 8 处）："
                    + "；".join(pdf_vocab[:8]), 3, pdf_vocab)

    # ---- 模型浏览：自足三维/二维浏览目录（一成果一子目录）----
    # 数据全部取自本轮 OSGB 产物；缺必填项在这里硬失败，不静默降级。
    # 例外：浏览引擎（assets/viewer）不随仓库分发，缺引擎就跳过这一件成果并留痕，
    # 其余成果照出——否则「没下载 viewer」会变成整条链跑不完。
    try:
        import build_browse_package
    except ImportError as exc:
        return fail("模型浏览", f"打包模块不可用：{type(exc).__name__}: {exc}")
    viewer_asset = getattr(build_browse_package, "ASSET_DEFAULT", "")
    browse_info = None
    if not (viewer_asset and os.path.isdir(viewer_asset)):
        marker = os.path.join("过程文件", "07-成果导出", "模型浏览_未生成.json")
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        with open(marker, "w", encoding="utf-8", newline="\n") as fh:
            json.dump({
                "schema": "cardo/lowair-browse-skip/v1",
                "skipped": True, "reason": "VIEWER_MISSING",
                "what": "模型浏览引擎（assets/viewer，随包分发）",
                "expected": "skills/cardo-osgb/assets/viewer/tool/caddy.exe",
                "recover": "安装包不完整：重新安装本插件（Release tgz 或 github 安装）",
                "effect": "本次只跳过「成果文件/成品版/模型浏览」；报告、指标、说明照常出",
            }, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        print("[07] 跳过模型浏览：包内缺浏览引擎 assets/viewer（安装包不完整，其余成果照出）。")
        print("[07]   期望：skills/cardo-osgb/assets/viewer/tool/caddy.exe")
        print("[07]   处置：重新安装本插件（Release tgz 或 github 安装）后 --from 07 重跑")
        browse_info = {"skipped": True, "reason": "VIEWER_MISSING",
                       "note": "缺 assets/viewer，已按 README 指引跳过"}
    else:
        try:
            browse_info = build_browse_package.build(
                workspace, args.out, model_path=args.model, indicators_path=args.indicators)
        except build_browse_package.BuildError as exc:
            return fail("模型浏览", str(exc))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return fail("模型浏览", f"打包失败：{type(exc).__name__}: {exc}")

    # ---- 导出留痕：四件成果的哈希 + 运行结论 + 输入源指纹 ----
    # 之前 07 步的 product 是 None，过程文件/ 下没有 07-成果导出/ 目录，
    # 成果写完就没痕了：事后只能靠文件时间戳猜「这批成果是哪次跑出来的」。
    # 这份日志让「成果 ↔ 输入 ↔ 一次运行」三者可对账。
    write_export_log(args, targets, model, ind, ext, mat, site,
                     status="ok", stage="完成", exit_code=0, audit=guard_report,
                     browse=browse_info)

    values = {m["id"]: m["value"] for m in ind["metrics"]}
    ext_note = ""
    if ext and ext.get("metrics"):
        s = ext["summary"]
        ext_note = f"；扩展指标 {s['total']} 条（有价值 {s['withValue']}）"
    mat_note = ""
    if mat and mat.get("items"):
        s = mat["summary"]
        mat_note = (f"；可观测类型 {s['types']} 类"
                    f"（落地 {s['landed']} / 部分 {s['partial']} / 未做 {s['none']}）")
    site_note = ""
    if site and site.get("summary"):
        s = site["summary"]
        site_note = (f"；场地要素 硬地 {s['hardAreaM2']} m² / 街巷 {s['streetCount']} 条 / "
                     f"低矮体量 {s['lowObjectCount']} 处 / 空地 {s['voidPatchCount']} 块 / "
                     f"贴地小体量 {s['groundObjectCount']} 处 / "
                     f"破损线索 {s['damageClueCount']} 条")
    print(f"[07] 完成：模型体检报告 / 现状指标 / 指标说明（各出成品版＋溯源版，"
          f"分存 成果文件/成品版 与 成果文件/内部件）+ 现状指标依据对照表，"
          f"共 {len(targets)} 件；回读核对 指标 {len(ind['metrics'])} 条 + "
          f"体检关键值 全部一致（建筑密度 {values['building_coverage_ratio']} / 栋数 "
          f"{values['building_count']} / 容积率 {values['floor_area_ratio_est']}）"
          f"{ext_note}{mat_note}{site_note}")
    if browse_info.get("skipped"):
        print("[07] 模型浏览：本次跳过（缺 assets/viewer，见 过程文件/07-成果导出/模型浏览_未生成.json）")
    else:
        print(f"[07] 模型浏览：{browse_info['dir']}（{browse_info['files']} 文件 / "
              f"{browse_info['bytes'] / 1048576.0:.1f} MB，引擎 {browse_info['engineVersion']}，"
              f"图层 {browse_info['layers']} / 指标 {browse_info['indicators']} / "
              f"图表 {browse_info['charts']}）")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
