#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""07 · 空白页自检：把成果渲成 PDF，逐页数「字符数 / 图片数」，空白页直接拦。

为什么要有这道闸门：指标说明曾多出一整张白纸（章末「空段落 + 显式分页符」被正文
顶到页尾，空段落自己翻了一页，分页符又翻一页），38 页的交付件里夹一页空白，
过去只能靠人眼一页页翻。这里把「翻页」变成可执行的检查：

  1. 用系统 Office（Word/Excel）把 docx/xlsx 导成 PDF——只写临时目录，
     不动成果文件本身；
  2. 用 PyMuPDF 逐页统计可读字符数、图片数、最小字号；
  3. 出现「0 字符 且 0 图」的页 → exit 3；页数超上限、字号低于下限同样 exit 3。

**分层降级（不依赖 Office 也能跑）**

渲染级检查要靠 Word/Excel 的 COM。客户的机器上不一定有 Office，也不该被要求
装 WPS——所以本脚本分两档：

  第一档 · 渲染级（有 Office COM 时）
      docx/xlsx → PDF → 逐页数字符/图片/字号。空白页、页数、字号三样都实测。
  第二档 · 结构级（没有 Office COM 时，包内 Python 直接读文件结构）
      不渲染，从 docx 的 word/document.xml、xlsx 的单元格样式直接判：
        · 空白页的结构性前兆：空段落自带显式分页符、空段落带 pageBreakBefore、
          文档尾部连续空段落——前两种正是当年那次多出白纸的成因；
        · 最小字号：docx 走样式继承链（显式 run 字号 → 字符样式 → 段落样式 →
          basedOn 链 → docDefaults），xlsx 读单元格 font.size，**字号底线照旧卡**；
        · 空 sheet / 空表格（提示级）。
      只有页数上限与逐页字符统计做不了——它们必须真的排版一遍才知道，
      报告里如实标「未执行」，**不拿「查不到」冒充「查过了没问题」**。

关键口径：**渲染不了 ≠ 有空白页**。
旧版把两者混成一个 problem 直接 exit 3，于是「没装 Office」被当成「成果有毛病」，
07 步一挂就卡住 08~16 全链。现在渲染失败只降级不阻断，只有真的查出问题才拦。

目标写法（可重复给出）：
    路径[::工作表][@页数上限][!字号下限]
例：
    成果文件/成品版/指标说明/指标说明_成品版.docx
    成果文件/内部件/现状指标/现状指标依据对照表.xlsx::指标依据@1!6

退出码：0 通过；2 目标缺失；3 真的查出问题（空白页 / 页数或字号不符 / 文件结构读不出）。
依赖：Windows + Python 即可。装了 Office 能做渲染级，没装就自动走结构级。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime

sys.dont_write_bytecode = True

SCHEMA = "cardo/lowair-blank-page-audit/v1"
SPEC_RE = re.compile(r"^(?P<path>[^@!]+?)(?:::(?P<sheet>[^@!]+))?"
                     r"(?:@(?P<pages>\d+))?(?:!(?P<font>\d+(?:\.\d+)?))?$")

# docx 命名空间
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


# ---------------------------------------------------------------- COM 能力探测
def _progid_exists(progid: str) -> bool:
    """ProgID 是否已注册（只查注册表，不启动 Office）。"""
    try:
        import winreg
    except ImportError:
        return False
    for root, path in ((winreg.HKEY_CLASSES_ROOT, progid + r"\CLSID"),
                       (winreg.HKEY_CURRENT_USER, r"Software\Classes\\" + progid + r"\CLSID")):
        try:
            with winreg.OpenKey(root, path):
                return True
        except OSError:
            continue
    return False


def probe_office() -> dict:
    """探测本机有没有 Word / Excel 的 COM。

    返回 {"word": bool, "excel": bool}。没装 Office（或只剩 WPS 且没注册
    Word/Excel 这两个 ProgID）就是 False——那就走结构级自检。
    """
    return {"word": _progid_exists("Word.Application"),
            "excel": _progid_exists("Excel.Application")}


# ---------------------------------------------------------------- 渲染（Office COM）
def _ps_quote(text: str) -> str:
    """PowerShell 单引号字符串：内部的 ' 要写成两个。"""
    return "'" + str(text).replace("'", "''") + "'"


def _render_script(src: str, dst: str, sheet: str = "") -> str:
    """生成导出脚本。Word 走 Documents.Open+ExportAsFixedFormat，Excel 走 ExportAsFixedFormat。

    两条导出路径都保留 SaveAs 兜底：WPS 的 COM 在个别版本上只实现了其中一支，
    缺哪支就换另一支，不让「导不出 PDF」被当成「没有空白页」。
    """
    src_q, dst_q = _ps_quote(src), _ps_quote(dst)
    if src.lower().endswith(".docx"):
        body = f"""
$app = New-Object -ComObject Word.Application
$app.Visible = $false
$app.DisplayAlerts = 0
try {{
  $doc = $app.Documents.Open({src_q}, $false, $true)
  try {{ $doc.ExportAsFixedFormat({dst_q}, 17) }}
  catch {{ $doc.SaveAs([ref]{dst_q}, [ref]17) | Out-Null }}
  $doc.Close(0)
}} finally {{
  try {{ $app.Quit() }} catch {{ }}
}}
"""
    else:
        body = f"""
$app = New-Object -ComObject Excel.Application
$app.Visible = $false
$app.DisplayAlerts = $false
try {{
  $wb = $app.Workbooks.Open({src_q}, 0, $true)
"""
        if sheet:
            body += f"""
  $ws = $wb.Worksheets.Item({_ps_quote(sheet)})
  try {{ $ws.ExportAsFixedFormat(0, {dst_q}) }}
  catch {{ $ws.SaveAs([ref]{dst_q}, [ref]57) | Out-Null }}
"""
        else:
            body += f"""
  try {{ $wb.ExportAsFixedFormat(0, {dst_q}) }}
  catch {{ $wb.SaveAs([ref]{dst_q}, [ref]57) | Out-Null }}
"""
        body += """
  $wb.Close($false)
} finally {
  try { $app.Quit() } catch { }
}
"""
    return ("$ErrorActionPreference = 'Stop'\n"
            "try {\n" + body + "\n  exit 0\n} catch {\n"
            "  [Console]::Error.WriteLine($_.Exception.Message)\n  exit 1\n}\n")


def render_pdf(src: str, dst: str, sheet: str = "", timeout: int = 600) -> tuple[bool, str]:
    """把 docx/xlsx 导成 PDF。返回 (成功与否, 说明)。"""
    work = os.path.dirname(os.path.abspath(dst)) or "."
    os.makedirs(work, exist_ok=True)
    if os.path.isfile(dst):
        os.remove(dst)
    ps1 = os.path.join(work, f"_render_{abs(hash((src, sheet, dst))) % 10 ** 8}.ps1")
    with open(ps1, "w", encoding="utf-8-sig", newline="\r\n") as fh:
        fh.write(_render_script(os.path.abspath(src), os.path.abspath(dst), sheet))
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps1],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"调用 Office 导出失败：{type(exc).__name__}: {exc}"
    finally:
        if os.path.isfile(ps1):
            os.remove(ps1)
    if proc.returncode != 0 or not os.path.isfile(dst):
        detail = (proc.stderr or proc.stdout or "").strip().replace("\n", " ")[:300]
        return False, f"Office 导出未产出 PDF（exit {proc.returncode}）：{detail}"
    return True, ""


# ---------------------------------------------------------------- 逐页统计（渲染级）
def audit_pdf(pdf: str) -> list[dict]:
    """逐页统计：字符数 / 图片数 / 最小字号。"""
    try:
        import pymupdf as fitz            # PyMuPDF ≥ 1.24 的新名字
    except ImportError:                   # 老版本只有 fitz
        import fitz                       # type: ignore
    doc = fitz.open(pdf)
    pages = []
    try:
        for index, page in enumerate(doc, start=1):
            text = (page.get_text("text") or "").strip()
            sizes = []
            for block in (page.get_text("dict") or {}).get("blocks", []) or []:
                for line in block.get("lines", []) or []:
                    for span in line.get("spans", []) or []:
                        if (span.get("text") or "").strip():
                            sizes.append(round(float(span.get("size") or 0), 1))
            pages.append({
                "page": index,
                "chars": len(text),
                "images": len(page.get_images(full=True)),
                "minFontPt": min(sizes) if sizes else None,
                "head": text[:40].replace("\n", " "),
            })
    finally:
        doc.close()
    return pages


# ---------------------------------------------------------------- 结构级自检（不依赖 Office）
def _half_points(val: str | None) -> float | None:
    """w:sz 的单位是半磅，返回磅。"""
    try:
        return float(val) / 2.0
    except (TypeError, ValueError):
        return None


def _attr(node, name: str) -> str | None:
    """安全取属性。

    不能用 `node or fallback`：xml.etree 的 Element 按**子元素数**判真假，
    `<w:sz w:val="21"/>` 没有子元素就是 falsy，`or` 会把它换成 fallback，
    于是所有显式字号都被读成 None，字号底线形同虚设。
    """
    if node is None:
        return None
    return node.get(W + name)


def _style_size_resolver(styles_root):
    """返回 resolve(styleId) -> 字号(磅)：显式 rPr/sz → basedOn 链 → docDefaults。

    为什么自己解析而不用 python-docx：python-docx 拿不到「某个 run 实际用的
    字号」——它只给默认样式。字号底线要卡得准，就得自己走继承链。
    """
    default = None
    doc_defaults = styles_root.find(W + "docDefaults") if styles_root is not None else None
    if doc_defaults is not None:
        rpr_default = doc_defaults.find(W + "rPrDefault")
        if rpr_default is not None:
            rpr = rpr_default.find(W + "rPr")
            if rpr is not None:
                default = _half_points(_attr(rpr.find(W + "sz"), "val"))
                if default is None:
                    default = _half_points(_attr(rpr.find(W + "szCs"), "val"))

    raw: dict[str, tuple] = {}
    if styles_root is not None:
        for style in styles_root.findall(W + "style"):
            sid = style.get(W + "styleId")
            if not sid:
                continue
            rpr = style.find(W + "rPr")
            size = None
            if rpr is not None:
                size = _half_points(_attr(rpr.find(W + "sz"), "val"))
                if size is None:
                    size = _half_points(_attr(rpr.find(W + "szCs"), "val"))
            based = style.find(W + "basedOn")
            raw[sid] = (size, based.get(W + "val") if based is not None else None)

    cache: dict[str, float | None] = {}

    def resolve(sid: str | None, depth: int = 0) -> float | None:
        if not sid or depth > 16:
            return default
        if sid in cache:
            return cache[sid]
        if sid not in raw:
            cache[sid] = default
            return default
        size, based = raw[sid]
        if size is None:
            size = resolve(based, depth + 1)
        cache[sid] = size
        return size

    return resolve, default


def audit_docx_structure(path: str) -> dict:
    """结构级 docx 自检：空白页前兆 + 最小字号。不渲染，不需要 Office。

    返回 {"ok", "error", "minFontPt", "emptyBreakParas", "breakBeforeEmptyParas",
          "trailingEmptyParas", "emptyTables", "paragraphs"}
    """
    result = {"ok": True, "error": None, "minFontPt": None, "emptyBreakParas": 0,
              "breakBeforeEmptyParas": 0, "trailingEmptyParas": 0, "emptyTables": 0,
              "paragraphs": 0}
    try:
        import zipfile
        from xml.etree import ElementTree as ET
    except ImportError as exc:                       # pragma: no cover
        result.update(ok=False, error=f"结构级自检不可用：{exc}")
        return result

    try:
        with zipfile.ZipFile(path) as archive:
            document = ET.fromstring(archive.read("word/document.xml"))
            try:
                styles = ET.fromstring(archive.read("word/styles.xml"))
            except KeyError:
                styles = None
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        result.update(ok=False, error=f"docx 结构读取失败：{type(exc).__name__}: {exc}")
        return result

    resolve, default_size = _style_size_resolver(styles)
    body = document.find(W + "body")
    if body is None:
        result.update(ok=False, error="docx 缺少 body 节点，文件可能不是合法 docx")
        return result

    # ---- 最小字号：逐 run 解析实际生效的字号 ----
    sizes: list[float] = []
    for para in document.iter(W + "p"):
        result["paragraphs"] += 1
        ppr = para.find(W + "pPr")
        para_style = None
        if ppr is not None:
            pstyle = ppr.find(W + "pStyle")
            if pstyle is not None:
                para_style = pstyle.get(W + "val")
        for run in para.iter(W + "r"):
            text = "".join(t.text or "" for t in run.iter(W + "t"))
            if not text.strip():
                continue
            rpr = run.find(W + "rPr")
            size = None
            if rpr is not None:
                size = _half_points(_attr(rpr.find(W + "sz"), "val"))
                if size is None:
                    size = _half_points(_attr(rpr.find(W + "szCs"), "val"))
                if size is None:
                    rstyle = rpr.find(W + "rStyle")
                    if rstyle is not None:
                        size = resolve(rstyle.get(W + "val"))
            if size is None:
                size = resolve(para_style)
            if size is None:
                size = default_size
            if size:
                sizes.append(round(size, 1))
    result["minFontPt"] = min(sizes) if sizes else None

    # ---- 空白页的结构性前兆（只看 body 顶层段落，表格内的分页不算） ----
    top_level = [el for el in body if el.tag == W + "p"]
    for para in top_level:
        has_text = any((t.text or "").strip() for t in para.iter(W + "t"))
        has_drawing = (para.find(".//" + W + "drawing") is not None
                       or para.find(".//" + W + "pict") is not None)
        has_page_break = any(br.get(W + "type") == "page" for br in para.iter(W + "br"))
        ppr = para.find(W + "pPr")
        break_before = (ppr is not None and ppr.find(W + "pageBreakBefore") is not None)
        if not has_text and not has_drawing:
            if has_page_break:
                result["emptyBreakParas"] += 1
            elif break_before:
                result["breakBeforeEmptyParas"] += 1

    # ---- 文档尾部连续空段落 ----
    trailing = 0
    for para in reversed(top_level):
        has_text = any((t.text or "").strip() for t in para.iter(W + "t"))
        has_drawing = (para.find(".//" + W + "drawing") is not None
                       or para.find(".//" + W + "pict") is not None)
        has_page_break = any(br.get(W + "type") == "page" for br in para.iter(W + "br"))
        if has_text or has_drawing or has_page_break:
            break
        trailing += 1
    result["trailingEmptyParas"] = trailing

    # ---- 全空表格（一个字的都没有）----
    for table in body.iter(W + "tbl"):
        if not any((t.text or "").strip() for t in table.iter(W + "t")):
            result["emptyTables"] += 1

    return result


def audit_xlsx_structure(path: str, sheet: str = "") -> dict:
    """结构级 xlsx 自检：最小字号 + 空 sheet。不渲染，不需要 Office。"""
    result = {"ok": True, "error": None, "minFontPt": None, "emptySheets": [],
              "sheetsChecked": [], "cellsScanned": 0}
    try:
        from openpyxl import load_workbook
    except ImportError as exc:                       # pragma: no cover
        result.update(ok=False, error=f"结构级自检不可用（缺 openpyxl）：{exc}")
        return result

    try:
        workbook = load_workbook(path, data_only=False, read_only=False)
    except (OSError, KeyError, ValueError) as exc:
        result.update(ok=False, error=f"xlsx 结构读取失败：{type(exc).__name__}: {exc}")
        return result

    try:
        targets = [workbook[sheet]] if sheet else list(workbook.worksheets)
        sizes: list[float] = []
        for worksheet in targets:
            result["sheetsChecked"].append(worksheet.title)
            valued = 0
            for row in worksheet.iter_rows():
                for cell in row:
                    if cell.value is None:
                        continue
                    valued += 1
                    font = cell.font
                    if font is not None and font.size:
                        sizes.append(round(float(font.size), 1))
            result["cellsScanned"] += valued
            if valued == 0:
                result["emptySheets"].append(worksheet.title)
        result["minFontPt"] = min(sizes) if sizes else None
    finally:
        try:
            workbook.close()
        except Exception:
            pass
    return result


def structural_audit(path: str, sheet: str = "") -> dict:
    """按扩展名分派结构级自检。"""
    if path.lower().endswith(".docx"):
        return audit_docx_structure(path)
    return audit_xlsx_structure(path, sheet)


# ---------------------------------------------------------------- 编排
def parse_target(spec: str) -> dict:
    hit = SPEC_RE.match(spec.strip())
    if not hit:
        raise ValueError(f"目标写法不认识：{spec}（应形如 路径::工作表@页数!字号）")
    return {
        "spec": spec.strip(),
        "path": hit.group("path").strip(),
        "sheet": (hit.group("sheet") or "").strip(),
        "maxPages": int(hit.group("pages")) if hit.group("pages") else None,
        "minFontLimit": float(hit.group("font")) if hit.group("font") else None,
    }


def _label(target: dict) -> str:
    return (os.path.basename(target["path"])
            + (f"::{target['sheet']}" if target["sheet"] else ""))


def run(specs: list[str], out_path: str = "", workdir: str = "",
        keep_pdf: str = "", min_font: float = 0.0) -> tuple[int, list, dict]:
    """逐目标渲染 + 审计。返回 (退出码, 问题列表, 报告)。

    min_font：全局字号下限（所有目标都适用，含逐页表格正文/表注/页脚）。
    目标自带 `!n` 时取两者较大的那个，避免某条 spec 写小了就绕过底线。

    降级：没有 Office COM（或渲染失败）时，改为结构级自检——
    空白页前兆与字号底线照旧卡，页数上限与逐页字符统计如实标「未执行」。
    """
    problems, targets = [], []
    warnings: list[str] = []
    skipped: list[str] = []
    temp_dir = workdir or tempfile.mkdtemp(prefix="cardo-blank-")
    os.makedirs(temp_dir, exist_ok=True)
    missing = False
    office = probe_office()
    modes: set[str] = set()

    for spec in specs:
        target = parse_target(spec)
        item = {**target, "present": os.path.isfile(target["path"])}
        if not item["present"]:
            problems.append(f"目标文件不存在：{target['path']}")
            missing = True
            targets.append(item)
            continue
        label = _label(target)
        is_docx = target["path"].lower().endswith(".docx")
        need_office = office["word"] if is_docx else office["excel"]

        # ---------- 第一档：渲染级（有 Office COM 且渲染成功）----------
        render_ok = False
        render_error = ""
        if need_office:
            pdf = os.path.join(temp_dir, re.sub(r"[^\w.\-]+", "_", label) + ".pdf")
            render_ok, render_error = render_pdf(target["path"], pdf, target["sheet"])
        else:
            render_error = ("本机未注册 " + ("Word.Application" if is_docx else "Excel.Application")
                            + "，未做渲染级检查")

        if render_ok:
            modes.add("render")
            pages = audit_pdf(pdf)
            blanks = [p["page"] for p in pages if p["chars"] <= 0 and p["images"] <= 0]
            fonts = [p["minFontPt"] for p in pages if p["minFontPt"]]
            item.update({
                "mode": "render",
                "pages": len(pages),
                "blankPages": blanks,
                "minFontPt": min(fonts) if fonts else None,
                "pageDetail": pages,
                "notVerified": [],
            })
            if keep_pdf:
                os.makedirs(keep_pdf, exist_ok=True)
                shutil.copy2(pdf, os.path.join(keep_pdf, os.path.basename(pdf)))
            if blanks:
                problems.append(f"{label} 有 {len(blanks)} 张空白页（0 字符 0 图）：第 "
                                + "、".join(str(n) for n in blanks) + " 页")
            if item["maxPages"] and len(pages) > item["maxPages"]:
                problems.append(f"{label} 共 {len(pages)} 页，超过上限 {item['maxPages']} 页")
            floor = max(item["minFontLimit"] or 0.0, min_font or 0.0)
            if floor and item["minFontPt"] and item["minFontPt"] < floor:
                problems.append(f"{label} 最小字号 {item['minFontPt']}pt，低于下限 {floor:g}pt")
            targets.append(item)
            continue

        # ---------- 第二档：结构级（没有 Office COM，或渲染失败）----------
        modes.add("structural")
        if render_error:
            warnings.append(f"{label} 未做渲染级检查（{render_error}），已改用结构级自检")
        structure = structural_audit(target["path"], target["sheet"])
        if not structure.get("ok"):
            problems.append(f"{label} 结构级自检失败，成果文件可能已损坏：{structure.get('error')}")
            targets.append({**item, "mode": "structural", "pages": None,
                            "error": structure.get("error")})
            continue

        not_verified = []
        if item["maxPages"]:
            not_verified.append(f"页数上限 {item['maxPages']} 页（需排版渲染才能数）")
        if not_verified:
            skipped.append(f"{label}：{'、'.join(not_verified)}")
        item.update({
            "mode": "structural",
            "pages": None,
            "blankPages": None,
            "minFontPt": structure.get("minFontPt"),
            "notVerified": not_verified,
        })

        # 空白页的结构性前兆——这几种正是当年多出白纸的成因，命中就拦
        if structure.get("emptyBreakParas"):
            problems.append(f"{label} 有 {structure['emptyBreakParas']} 个空段落自带显式分页符，"
                            "很可能多出空白页（应改用标题段落的 pageBreakBefore）")
        if structure.get("breakBeforeEmptyParas"):
            problems.append(f"{label} 有 {structure['breakBeforeEmptyParas']} 个空段落设了 "
                            "pageBreakBefore，很可能多出空白页")
        if structure.get("trailingEmptyParas", 0) >= 2:
            warnings.append(f"{label} 文档尾部有 {structure['trailingEmptyParas']} 个连续空段落，"
                            "留意末页是否留白")
        if structure.get("emptyTables"):
            warnings.append(f"{label} 有 {structure['emptyTables']} 个完全空的表格")
        if structure.get("emptySheets"):
            warnings.append(f"{label} 有空 sheet：{'、'.join(structure['emptySheets'])}")

        # 字号底线在结构级照样卡
        floor = max(item["minFontLimit"] or 0.0, min_font or 0.0)
        if floor and item["minFontPt"] and item["minFontPt"] < floor:
            problems.append(f"{label} 最小字号 {item['minFontPt']}pt，低于下限 {floor:g}pt")
        targets.append(item)

    if not workdir:
        shutil.rmtree(temp_dir, ignore_errors=True)

    code = 2 if missing else (3 if problems else 0)
    mode = "render" if modes == {"render"} else ("structural" if modes == {"structural"} else "mixed")
    report = {
        "schema": SCHEMA,
        "schemaVersion": 2,
        "generatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "producer": {"name": "cardo 低空解析 · 07 空白页自检", "version": "1.1.0"},
        "status": "ok" if code == 0 else "failed",
        "exitCode": code,
        "mode": mode,
        "officeProbe": office,
        "rule": "逐页统计可读字符数与图片数：字符 0 且图片 0 即为空白页",
        "modeNote": ("渲染级：Office COM 可用，逐页实测" if mode == "render" else
                     "结构级：无 Office COM，改从文件结构判定空白页前兆与字号（页数与逐页字符统计未执行）"
                     if mode == "structural" else
                     "混合：部分目标渲染级、部分目标结构级"),
        "targets": [{k: v for k, v in item.items() if k != "pageDetail"} for item in targets],
        "blankPageCount": sum(len(item.get("blankPages") or []) for item in targets),
        "problems": problems,
        "warnings": warnings,
        "skipped": skipped,
    }
    if out_path:
        os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
        with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
    return code, problems, report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="07 · 空白页自检（导出后逐页数字符/图片）")
    ap.add_argument("specs", nargs="+", help="路径[::工作表][@页数上限][!字号下限]")
    ap.add_argument("--out", default=os.path.join("过程文件", "07-成果导出", "空白页自检.json"),
                    help="自检报告 JSON（默认写进过程文件）")
    ap.add_argument("--keep-pdf", default="", help="保留渲染出的 PDF 到该目录（排查用）")
    ap.add_argument("--min-font", type=float, default=0.0,
                    help="全局字号下限（pt）：任何目标的最小字号低于它即 exit 3")
    args = ap.parse_args(argv)
    code, problems, report = run(args.specs, args.out, keep_pdf=args.keep_pdf,
                                 min_font=args.min_font)
    for item in report["targets"]:
        label = os.path.basename(item["path"]) + (f"::{item['sheet']}" if item.get("sheet") else "")
        if item.get("pages") is None:
            if item.get("error"):
                print(f"[空白页] {label}：未检查（{item['error']}）")
            else:
                print(f"[空白页] {label}：结构级自检，最小字号 {item.get('minFontPt')}pt")
            continue
        print(f"[空白页] {label}：{item['pages']} 页，空白 {len(item['blankPages'])} 张，"
              f"最小字号 {item['minFontPt']}pt")
    for problem in problems:
        print(f"[空白页] {problem}", file=sys.stderr)
    for warning in report.get("warnings") or []:
        print(f"[空白页] 提示：{warning}", file=sys.stderr)
    for skip in report.get("skipped") or []:
        print(f"[空白页] 未执行：{skip}", file=sys.stderr)
    print(f"[空白页] 自检档位：{report['mode']}（{report['modeNote']}）")
    print(f"[空白页] {'通过' if code == 0 else '未通过'}（exit {code}）："
          f"空白页 {report['blankPageCount']} 张")
    return code


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
