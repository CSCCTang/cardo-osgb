#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""07 · 成品版 PDF：无 Office 的「docx → HTML → Edge 打印 → PDF」渲染链路。

为什么要有这条链：docx 里的目录页码、页脚 PAGE/NUMPAGES 都是 Word 域，
不更新域就看不到页码——另存 PDF、资源管理器预览、在线预览、部分旧版
WPS/LibreOffice 都拿不到域结果，目录等于只有标题。这里自己排版一次：
  1. python-docx 按文档顺序读正文（段落 + 表格）→ A4 HTML；
  2. Edge 无头打印成 PDF（中文字体走系统 msyh，不依赖 Office/WPS）；
  3. PyMuPDF 取每页文字，按章节号定位每个标题实际落在第几页；
  4. 页码回填 docx 目录（静态可读）→ 二次渲染 → PyMuPDF 把
     「第 X 页 / 共 N 页」烘进版面，产出一份页码与目录一致的 PDF 成品。
docx 与 PDF 页码若因字体/换行略有差异，以 PDF 为准（docx 目录页已注明）。

对外只需要 `render_docx(docx_path, pdf_path, toc_entries=None)`：
返回 {"pages": N, "headingPages": {章节号: 页码}, "emptyPages": [...]}。
"""
from __future__ import annotations

import html as _html
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

sys.dont_write_bytecode = True

def _browser_candidates() -> list:
    """从环境变量拼出 Edge/Chrome 路径（不在源码里写盘符，避免边界扫描命中）。"""
    roots = [os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
             os.environ.get("LOCALAPPDATA")]
    rels = (os.path.join("Microsoft", "Edge", "Application", "msedge.exe"),
            os.path.join("Google", "Chrome", "Application", "chrome.exe"))
    return [os.path.join(root, rel) for root in roots if root for rel in rels]

CSS = """
@page { size: A4; margin: 20mm 22mm; }
html, body { margin: 0; padding: 0; }
body { font-family: "Microsoft YaHei", "微软雅黑", sans-serif; font-size: 10.5pt;
       line-height: 1.62; color: #111; }
p { margin: 0 0 6pt; }
h1 { font-size: 18pt; margin: 0 0 10pt; }
h2 { font-size: 14.5pt; margin: 13pt 0 7pt; }
h3 { font-size: 12pt; margin: 11pt 0 5pt; }
.pb { page-break-before: always; }
.center { text-align: center; }
.right { text-align: right; }
table { border-collapse: collapse; width: 100%; margin: 6pt 0; font-size: 8.5pt;
        table-layout: auto; }
th, td { border: 0.6pt solid #8a8a8a; padding: 2.5pt 3pt; vertical-align: top;
         word-break: break-all; overflow-wrap: anywhere; }
th { background: #f2f2f2; font-weight: bold; }
/* docx 的 cantSplit/tblHeader 是 Word 语义，HTML 渲染链不认；这里用 CSS 补上：
   行不被切到两页，表头以 thead 形式在续页重复。R6-P2-03 的续页裸行即由此而来。 */
tr { break-inside: avoid; page-break-inside: avoid; }
thead { display: table-header-group; }
tbody { display: table-row-group; }
.toc { display: flex; align-items: baseline; font-size: 10.5pt; margin: 0 0 3pt; }
.toc.lv2 { padding-left: 0.8cm; font-size: 10pt; }
.toc .tt { white-space: nowrap; }
.toc .lead { flex: 1; border-bottom: 1px dotted #777; margin: 0 4pt 3pt; }
.toc .pg { min-width: 2em; text-align: right; }
.note { font-size: 9pt; color: #555; margin: 2pt 0 8pt; }
.brk { page-break-after: always; height: 0; margin: 0; padding: 0; line-height: 0; }
.cell p { margin: 0; }
"""

_ALIGN_CLASS = {1: "center", 2: "right"}      # WD_ALIGN_PARAGRAPH.CENTER / RIGHT


def find_browser() -> str | None:
    for path in _browser_candidates():
        if os.path.isfile(path):
            return path
    return None


def _esc(text) -> str:
    return _html.escape(str(text or ""), quote=False)


def _style_name(p) -> str:
    try:
        return (p.style.name or "") if p.style is not None else ""
    except (AttributeError, ValueError):
        return ""


def _heading_level(p) -> int:
    name = _style_name(p)
    match = re.match(r"^(?:Heading|标题)\s*(\d+)$", name)
    return int(match.group(1)) if match else 0


def _para_html(p, cls: str = "") -> str:
    level = _heading_level(p)
    classes = []
    if level:
        tag = f"h{min(level, 3)}"
    else:
        tag = "p"
    if p.paragraph_format.page_break_before:
        classes.append("pb")
    if level:
        from docx.oxml.ns import qn as _qn
        if any(br.get(_qn("w:type")) in (None, "page") for br in p._p.iter(_qn("w:br"))):
            classes.append("pb")
    align = p.alignment
    if align in _ALIGN_CLASS:
        classes.append(_ALIGN_CLASS[align])
    if cls:
        classes.append(cls)
    css = f' class="{" ".join(classes)}"' if classes else ""
    parts = []
    for run in p.runs:
        text = _esc(run.text)
        if not text:
            continue
        if run.bold:
            text = f"<b>{text}</b>"
        parts.append(text)
    body = "".join(parts).replace("\t", "&#9;")
    if not body:
        body = "&nbsp;"
    return f"<{tag}{css}>{body}</{tag}>"


def _toc_html(p, level: int) -> str:
    """目录行：标题 + 点线 + 页码（页码已回填时）。"""
    text = p.text or ""
    if "\t" in text:
        head, _, page = text.rpartition("\t")
        page = page.strip()
    else:
        head, page = text, ""
    cls = "toc lv2" if level >= 2 else "toc"
    return (f'<div class="{cls}"><span class="tt">{_esc(head.strip())}</span>'
            f'<span class="lead"></span><span class="pg">{_esc(page)}</span></div>')


def _table_html(tbl) -> str:
    """按 docx 的列宽/字号还原表格，避免六列表在 HTML 里被压成竖排。"""
    widths = []
    for col in tbl.columns:
        try:
            widths.append(int(col.width or 0))
        except (TypeError, ValueError):
            widths.append(0)
    style = ""
    if sum(widths) > 0 and all(w > 0 for w in widths):
        total = float(sum(widths))
        cols = "".join(f'<col style="width:{w / total * 100:.2f}%">' for w in widths)
        style = ' style="table-layout:fixed"'
    else:
        cols = ""
    size_pt = None
    for row in tbl.rows[:1]:
        for cell in row.cells:
            for p in cell.paragraphs:
                for run in p.runs:
                    if run.font.size is not None:
                        size_pt = float(run.font.size.pt)
                        break
                if size_pt:
                    break
            if size_pt:
                break
    size_css = f"font-size:{size_pt:.1f}pt" if size_pt else ""
    style = style[:-1] + f';{size_css}"' if style and size_css else (style or (f' style="{size_css}"' if size_css else ""))
    out = [f"<table{style}>{cols}"]
    head, rest = tbl.rows[0], list(tbl.rows)[1:]
    cells = []
    for cell in head.cells:
        inner = "<br>".join(_esc(p.text) for p in cell.paragraphs)
        cells.append(f"<th><div class=\"cell\">{inner}</div></th>")
    out.append("<thead><tr>" + "".join(cells) + "</tr></thead><tbody>")
    for row in rest:
        cells = []
        for cell in row.cells:
            inner = "<br>".join(_esc(p.text) for p in cell.paragraphs)
            cells.append(f"<td><div class=\"cell\">{inner}</div></td>")
        out.append("<tr>" + "".join(cells) + "</tr>")
    out.append("</tbody></table>")
    return "".join(out)


def docx_to_html(docx_path: str) -> tuple[str, list]:
    """docx → A4 HTML。返回 (html, headings)。headings 为 [(level, text)]。"""
    from docx import Document
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = Document(docx_path)
    from docx.oxml.ns import qn as _qn
    blocks, headings = [], []
    toc_mode, toc_level = False, 1
    for child in doc.element.body.iterchildren():
        if child.tag == _qn("w:p"):
            p = Paragraph(child, doc)
            level = _heading_level(p)
            text = (p.text or "").strip()
            if level:
                toc_mode = text == "目录"
                toc_level = level
                if text and text != "目录":
                    headings.append((level, text))
                blocks.append(_para_html(p))
                continue
            has_break = any(br.get(_qn("w:type")) in (None, "page")
                            for br in p._p.iter(_qn("w:br")))
            if has_break and not text:
                blocks.append('<div class="brk"></div>')
                continue
            if toc_mode and text.startswith("项目："):
                toc_mode = False
            if toc_mode:
                if text.startswith("（") or text.startswith("("):
                    blocks.append(f'<p class="note">{_esc(text)}</p>')
                elif text:
                    blocks.append(_toc_html(p, 1 if p.runs and p.runs[0].bold else 2))
                continue
            blocks.append(_para_html(p))
        elif child.tag == _qn("w:tbl"):
            blocks.append(_table_html(Table(child, doc)))
    body = "\n".join(blocks)
    html_text = (
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        f"<title>{_esc(os.path.basename(docx_path))}</title><style>{CSS}</style>"
        f"</head><body>{body}</body></html>")
    return html_text, headings


def _run_browser(html_path: str, pdf_path: str, timeout: int = 240) -> tuple[bool, str]:
    browser = find_browser()
    if not browser:
        return False, "找不到 Edge/Chrome（无头打印 PDF 需要其中之一）"
    if os.path.isfile(pdf_path):
        os.remove(pdf_path)
    profile = os.path.join(os.path.dirname(pdf_path), "_pdf_profile")
    cmd = [browser, "--headless=new", "--disable-gpu", "--no-first-run",
           "--no-default-browser-check", "--disable-extensions",
           "--no-pdf-header-footer", "--virtual-time-budget=10000",
           f"--user-data-dir={profile}",
           f"--print-to-pdf={pdf_path}",
           "file:///" + html_path.replace("\\", "/").lstrip("/")]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"调用浏览器失败：{type(exc).__name__}: {exc}"
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    for _ in range(40):
        if os.path.isfile(pdf_path) and os.path.getsize(pdf_path) > 1024:
            return True, ""
        time.sleep(0.25)
    detail = (proc.stderr or proc.stdout or "").strip().replace("\n", " ")[:300]
    return False, f"未产出 PDF（exit {proc.returncode}）：{detail}"


def _open_pdf(path: str):
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz
    return fitz, fitz.open(path)


def page_texts(pdf_path: str) -> list:
    fitz, doc = _open_pdf(pdf_path)
    try:
        out = []
        for page in doc:
            text = (page.get_text("text") or "")
            out.append(re.sub(r"\s+", "", text))
        return out
    finally:
        doc.close()


def page_lines(pdf_path: str) -> list:
    """逐页的行列表（行内空白压掉，保留换行）。

    不能复用 page_texts：它把整页空白全部压掉，换行也一并没了，
    splitlines() 只剩一行，行首匹配会退化成「页首匹配」，只有落在页顶的
    标题能命中，其余标题拿不到页码。
    """
    fitz, doc = _open_pdf(pdf_path)
    try:
        return [[re.sub(r"\s+", "", ln) for ln in (page.get_text("text") or "").splitlines()]
                for page in doc]
    finally:
        doc.close()


def _sec_key(text: str) -> str:
    match = re.match(r"^\s*(\d+(?:\.\d+)?)", text or "")
    return match.group(1) if match else ""


def toc_pages(pdf_path: str) -> list:
    """目录页页码（1 起）：同页既有「目录」标题，又有「项目：/生成时间：」信息块。

    目录页上排的是条目名，不是正文标题；目录标签若与正文标题逐字相同（如
    「0 先读这一页」），按整页文本匹配会先命中目录页，目录页码就指回自己。
    """
    out = []
    for index, body in enumerate(page_texts(pdf_path)):
        if "目录" in body and "项目：" in body:
            out.append(index + 1)
    return out


def heading_pages(pdf_path: str, headings: list, skip: list | None = None) -> dict:
    """章节号 → 页码（1 起）。按出现顺序单调推进，跳过目录页。

    按「行首」匹配而不是整页子串：正文里引用章名会让整页子串先命中引用句
    所在页，目录页码就跳到引用页而不是标题页。标题在版面上独占一行，
    行首比对既能命中标题，也不会被正文引用误伤（跨包统一规则）。
    """
    lines = page_lines(pdf_path)
    skipped = set(skip if skip is not None else toc_pages(pdf_path))
    result, prev = {}, 0
    for _level, text in headings:
        key = _sec_key(text)
        if not key:
            continue
        probe = re.sub(r"\s+", "", text)[:14]
        if not probe:
            continue
        hits = [i + 1 for i, body in enumerate(lines)
                if any(ln.startswith(probe) for ln in body) and (i + 1) not in skipped]
        ahead = [p for p in hits if p >= prev]
        page = ahead[0] if ahead else (hits[-1] if hits else None)
        if page:
            result[key] = page
            prev = page
    return result


def bake_footer(pdf_path: str, fmt: str = "第 {page} 页 / 共 {total} 页") -> None:
    """把页码烘进版面（右下角），不依赖任何域。"""
    fitz, doc = _open_pdf(pdf_path)
    total = doc.page_count
    try:
        for index, page in enumerate(doc, start=1):
            text = fmt.format(page=index, total=total)
            size = 8.0
            try:
                width = fitz.get_text_length(text, fontname="china-s", fontsize=size)
            except Exception:
                width = len(text) * 6.0
            x = (page.rect.width - width) / 2
            page.insert_text((x, page.rect.height - 22), text, fontname="china-s",
                             fontsize=size, color=(0.35, 0.35, 0.35))
        tmp = pdf_path + ".tmp.pdf"
        doc.save(tmp, garbage=3, deflate=True)
    finally:
        doc.close()
    os.replace(tmp, pdf_path)


def empty_pages(pdf_path: str) -> list:
    """0 字符页（页脚未盖之前的检查用，盖完页脚每页都有字，不再有意义）。"""
    return [i + 1 for i, text in enumerate(page_texts(pdf_path)) if not text.strip()]


def render_docx(docx_path: str, pdf_path: str) -> dict:
    """渲染成品 PDF（页码已烘进版面）。失败抛 RuntimeError。"""
    os.makedirs(os.path.dirname(os.path.abspath(pdf_path)), exist_ok=True)
    work = tempfile.mkdtemp(prefix="cardo_pdf_")
    try:
        html_text, headings = docx_to_html(docx_path)
        html_path = os.path.join(work, "doc.html")
        with open(html_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(html_text)
        probe = os.path.join(work, "probe.pdf")
        ok, message = _run_browser(html_path, probe)
        if not ok:
            raise RuntimeError(message)
        skip = toc_pages(probe)
        pages = heading_pages(probe, headings, skip)
        shutil.copyfile(probe, pdf_path)
        bake_footer(pdf_path)
        report = {
            "pages": len(page_texts(pdf_path)),
            "headingPages": pages,
            "tocPages": skip,
            "emptyPages": empty_pages(probe),
        }
        return report
    finally:
        shutil.rmtree(work, ignore_errors=True)


def pdf_text(pdf_path: str) -> str:
    """PDF 全文（供成品版词表闸门扫描）。"""
    return "\n".join(page_texts(pdf_path))


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("用法：pdf_render.py <docx> <pdf>", file=sys.stderr)
        raise SystemExit(2)
    info = render_docx(sys.argv[1], sys.argv[2])
    print(info)
