#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_yaml_lite.py -- 极简 YAML 子集解析器（用标准库实现，保证可移植性）。

为什么自带 lite 解析：规则文件是受控子集，标准库实现可保证剪切后立即可用；**不排斥第三方**——
需要完整 YAML 语义时可直接引入 PyYAML（pip，MIT）替换本文件，只要同步 requirements 与回归即可。
本插件规则文件的 YAML 是**受控子集**，够用即可，不追求通用。

支持的语法（仅此而已）：
  · 块状映射       key: value / key:（下一层缩进为子节点）
  · 块状列表       - value / -（下一层缩进为子节点）/ - key: value（列表项是映射）
  · 标量           整数、浮点、true/false、null、单/双引号字符串、裸字符串
  · 注释           整行以 # 开头
  · 顶层必须是映射

**不支持**（遇到即报错，不静默猜）：
  · 行内 flow 集合（{} / []）、锚点/别名（&*）、多行块（| >）、行尾注释、Tab 缩进、重复键。

用法：
    from _yaml_lite import loads, load_file, YamlLiteError
    data = loads(text)
    data = load_file(path)

退出码约定（CLI）：0 解析成功并打印 JSON；1 语法错；2 文件不可读。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

__version__ = "0.1.0"


class YamlLiteError(ValueError):
    """YAML 子集语法错误（带行号）。"""


_KEY_RE = re.compile(r"^([A-Za-z0-9_\u4e00-\u9fff.\-]+):(?:[ \t]+(.*))?$")


def _reject_unsupported(text: str) -> None:
    """拒绝 YAML 集合/多行语法（只在"值整体形态"上判断，允许正文出现 * & | > 等符号）。"""
    if text[:1] in ("&", "*"):
        raise YamlLiteError(f"值「{text[:40]}」以 {text[:1]} 开头：锚点/别名不支持")
    if text in ("|", ">", "|-", ">-", "|+", ">+"):
        raise YamlLiteError(f"值「{text}」：多行块（| >）不支持")
    if text[:1] in ("{", "["):
        raise YamlLiteError(f"值「{text[:40]}」以 {text[:1]} 开头：行内 flow 集合不支持，请用块状写法")


def _strip_comment(raw: str) -> str:
    # 只支持整行注释：内容里出现 # 一律当普通字符（规则文件里不要写行尾注释）
    return raw if not raw.lstrip().startswith("#") else ""


def _tokenize(text: str) -> list[tuple[int, str]]:
    lines: list[tuple[int, str]] = []
    for lineno, raw in enumerate(text.replace("\r\n", "\n").replace("\r", "\n").split("\n"), start=1):
        raw = raw.rstrip()
        if not raw.strip():
            continue
        stripped = _strip_comment(raw)
        if not stripped:
            continue
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            raise YamlLiteError(f"第 {lineno} 行：缩进用了 Tab（只允许空格）")
        indent = len(raw) - len(raw.lstrip(" "))
        content = stripped.strip()
        lines.append((indent, content))
    return lines


def _scalar(text: str):
    text = text.strip()
    if text == "" or text == "~" or text.lower() == "null":
        return None
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1]
    low = text.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if re.fullmatch(r"[-+]?\d+", text):
        return int(text)
    if re.fullmatch(r"[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?", text):
        return float(text)
    _reject_unsupported(text)
    return text


class _Parser:
    def __init__(self, lines: list[tuple[int, str]]):
        self.lines = lines
        self.i = 0

    def peek(self):
        return self.lines[self.i] if self.i < len(self.lines) else None

    def parse(self):
        if not self.lines:
            return {}
        node = self.parse_block(self.lines[0][0])
        if self.i != len(self.lines):
            indent, content = self.lines[self.i]
            raise YamlLiteError(f"第 {self.i + 1} 条语句无法归位（缩进 {indent}）：{content[:40]}")
        return node

    def parse_block(self, indent: int):
        line = self.peek()
        if line is None:
            return None
        if line[1].startswith("- "):
            return self.parse_list(indent)
        if line[1] == "-":
            return self.parse_list(indent)
        return self.parse_map(indent)

    def parse_map(self, indent: int) -> dict:
        result: dict = {}
        while True:
            line = self.peek()
            if line is None or line[0] != indent:
                break
            _, content = line
            if content.startswith("- "):
                break
            match = _KEY_RE.match(content)
            if not match:
                raise YamlLiteError(f"无法解析映射行（缩进 {indent}）：{content[:60]}")
            key, rest = match.group(1), (match.group(2) or "")
            if key in result:
                raise YamlLiteError(f"重复键：{key}")
            self.i += 1
            if rest.strip() == "":
                nxt = self.peek()
                if nxt is not None and nxt[0] > indent:
                    result[key] = self.parse_block(nxt[0])
                elif nxt is not None and nxt[0] == indent and nxt[1].startswith("- "):
                    result[key] = self.parse_list(indent)
                else:
                    result[key] = None
            else:
                result[key] = _scalar(rest)
        return result

    def parse_list(self, indent: int) -> list:
        items: list = []
        while True:
            line = self.peek()
            if line is None or line[0] != indent:
                break
            _, content = line
            if content == "-":
                self.i += 1
                nxt = self.peek()
                items.append(self.parse_block(nxt[0]) if nxt is not None and nxt[0] > indent else None)
                continue
            if not content.startswith("- "):
                break
            rest = content[2:].strip()
            self.i += 1
            match = _KEY_RE.match(rest)
            if match and not (rest[0] in ("'", '"')):
                # 列表项是映射：第一行是 "key: value"，后续同级条目缩进到 indent+2
                key, tail = match.group(1), (match.group(2) or "")
                entry: dict = {}
                item_indent = indent + 2
                if tail.strip() == "":
                    nxt = self.peek()
                    entry[key] = self.parse_block(nxt[0]) if nxt is not None and nxt[0] > item_indent else None
                else:
                    entry[key] = _scalar(tail)
                while True:
                    nxt = self.peek()
                    if nxt is None or nxt[0] != item_indent or nxt[1].startswith("- "):
                        break
                    more = self.parse_map(item_indent)
                    entry.update(more)
                items.append(entry)
            else:
                items.append(_scalar(rest))
        return items


def loads(text: str):
    """解析 YAML 子集，返回 Python 对象（顶层必须是映射）。"""
    stripped = text.lstrip("\ufeff")
    parser = _Parser(_tokenize(stripped))
    result = parser.parse()
    if not isinstance(result, dict):
        raise YamlLiteError("顶层必须是映射（key: value）")
    return result


def load_file(path: "str | Path"):
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"文件不存在：{p}")
    return loads(p.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("用法：python _yaml_lite.py <file.yaml> [--json]", file=sys.stderr)
        return 2
    path = argv[0]
    try:
        data = load_file(path)
    except FileNotFoundError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except YamlLiteError as exc:
        print(f"YAML 子集语法错误：{exc}", file=sys.stderr)
        return 1
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())