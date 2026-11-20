# -*- coding: utf-8 -*-
"""项目包契约的 Python 侧实现（与 viewer/project.js 同一套规则）。

用途：
  1. 开工.py 起服务前先校验一次，不合格直接退出码 4，不让浏览器打开一个必然报错的页面；
  2. 自检时核对包内文件是否齐、栅格尺寸与配准是否对得上。

用法：
  python scripts/project_pack.py                      校验默认包（viewer/default-project.txt）
  python scripts/project_pack.py --project projects/x  校验指定包
  python scripts/project_pack.py --list                列出 projects/ 下所有包
"""
from __future__ import annotations

import json
import os
import sys

SCHEMA_REL = "viewer/project.schema.json"
DEFAULT_REL = "viewer/default-project.txt"


class Problem(object):
    def __init__(self, path, kind, expect, actual):
        self.path = path
        self.kind = kind
        self.expect = expect
        self.actual = actual

    def __str__(self):
        return "project.json → %s：%s（期望 %s，实际 %s）" % (self.path, self.kind, self.expect, self.actual)


class PackageError(Exception):
    def __init__(self, message, problems=None, config_path=None):
        Exception.__init__(self, message)
        self.message = message
        self.problems = problems or []
        self.config_path = config_path


def _describe(v):
    if v is None:
        return "（未提供）"
    if isinstance(v, bool):
        return "布尔 %s" % v
    if isinstance(v, str):
        return "空字符串" if v.strip() == "" else "字符串「%s」" % v
    if isinstance(v, list):
        return "数组（%d 项）" % len(v)
    if isinstance(v, dict):
        return "对象"
    return "%s %s" % (type(v).__name__, v)


def _type_ok(kind, v):
    if kind == "string":
        return isinstance(v, str) and v.strip() != ""
    if kind == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    if kind == "integer":
        return isinstance(v, int) and not isinstance(v, bool)
    if kind == "boolean":
        return isinstance(v, bool)
    if kind == "array":
        return isinstance(v, list)
    if kind == "object":
        return isinstance(v, dict)
    return True


def _get(node, dotted):
    for key in dotted.split("."):
        if not isinstance(node, dict):
            return None, False
        if key not in node:
            return None, False
        node = node[key]
    return node, True


def _walk(node, tokens, prefix, out):
    """把 "layers[].file" 展开成 [(具体路径, 值, 父对象)]"""
    tok, rest = tokens[0], tokens[1:]
    is_arr = tok.endswith("[]")
    key = tok[:-2] if is_arr else tok
    path = (prefix + "." if prefix else "") + key
    if not isinstance(node, dict):
        if not rest:
            out.append((path, None, None))
        return
    value = node.get(key)
    if is_arr:
        if not isinstance(value, list):
            if not rest:
                out.append((path, value, node))
            return
        for i, item in enumerate(value):
            if not rest:
                out.append(("%s[%d]" % (path, i), item, value))
            else:
                _walk(item, rest, "%s[%d]" % (path, i), out)
        return
    if not rest:
        out.append((path, value, node))
    else:
        _walk(value, rest, path, out)


def validate(cfg, rules):
    problems = []
    for rule in rules:
        entries = []
        _walk(cfg, rule["path"].split("."), "", entries)
        for path, value, parent in entries:
            required = rule.get("required", True)
            when = rule.get("requiredWhenPresent")
            if when:
                present_value, present = _get(cfg, when)
                required = required and present and present_value is not None
            same = rule.get("requireIfSame")
            if same:
                required = isinstance(parent, dict) and parent.get(same["path"]) == same["equals"]

            expect = rule.get("type", "any")
            if rule.get("enum"):
                expect += "，取值 " + " / ".join(rule["enum"])
            if rule.get("length"):
                expect += "，长度 %d" % rule["length"]
            if rule.get("minItems"):
                expect += "，至少 %d 项" % rule["minItems"]

            if value is None or (isinstance(value, str) and value.strip() == ""):
                if required:
                    problems.append(Problem(path, "缺少必填字段", expect, _describe(value)))
                continue
            if rule.get("type") and not _type_ok(rule["type"], value):
                problems.append(Problem(path, "类型不符", expect, _describe(value)))
                continue
            if rule.get("enum") and value not in rule["enum"]:
                problems.append(Problem(path, "取值不在允许范围", expect, _describe(value)))
                continue
            if isinstance(value, list):
                if rule.get("length") and len(value) != rule["length"]:
                    problems.append(Problem(path, "长度不符", expect, _describe(value)))
                    continue
                if rule.get("minItems") and len(value) < rule["minItems"]:
                    problems.append(Problem(path, "条目不足", expect, _describe(value)))
                    continue
                if rule.get("items"):
                    bad = [x for x in value if not _type_ok(rule["items"], x)]
                    if bad:
                        problems.append(Problem(path, "元素类型不符", rule["items"], _describe(bad[0])))
    kept = []
    for p in problems:
        covered = any(k.kind == "缺少必填字段" and p.path.startswith(k.path + ".") for k in kept)
        if not covered:
            kept.append(p)
    return kept


def load_schema(root):
    with open(os.path.join(root, SCHEMA_REL), encoding="utf-8") as fh:
        return json.load(fh)


class Package(object):
    def __init__(self, root, rel, base_dir, config):
        self.root = root
        self.rel = rel
        self.dir = base_dir
        self.config = config

    def resolve(self, rel_path):
        return os.path.normpath(os.path.join(self.dir, *str(rel_path).split("/")))

    @property
    def name(self):
        return self.config.get("project", {}).get("name", self.rel)


def default_project(root):
    path = os.path.join(root, DEFAULT_REL)
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read().strip()
    except OSError:
        raise PackageError("默认项目包未配置：读不到 " + DEFAULT_REL)
    if not text:
        raise PackageError("默认项目包未配置：" + DEFAULT_REL + " 为空")
    return text.replace("\\", "/").strip("/")


def discover(root):
    base = os.path.join(root, "projects")
    if not os.path.isdir(base):
        return []
    return sorted(d for d in os.listdir(base)
                  if os.path.isfile(os.path.join(base, d, "project.json")))


def load(root, project=None):
    rel = (project or default_project(root)).replace("\\", "/").rstrip("/")
    base = rel if os.path.isabs(rel) else os.path.join(root, *rel.split("/"))
    config_path = os.path.join(base, "project.json")
    if not os.path.isdir(base):
        raise PackageError("项目包目录不存在：" + rel, config_path=config_path)
    if not os.path.isfile(config_path):
        raise PackageError("项目包缺配置文件 project.json：" + rel, config_path=config_path)
    try:
        with open(config_path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except ValueError as exc:
        raise PackageError("project.json 不是合法 JSON：%s" % exc, config_path=config_path)
    schema = load_schema(root)
    problems = validate(cfg, schema.get("rules", []))
    if problems:
        raise PackageError("项目包配置有 %d 处不满足契约" % len(problems), problems, config_path)
    return Package(root, rel, base, cfg)


def _abs_paths(node, trail=""):
    """找出配置里写死的盘符/绝对路径"""
    hits = []
    if isinstance(node, dict):
        for k, v in node.items():
            hits += _abs_paths(v, trail + "." + k if trail else k)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            hits += _abs_paths(v, "%s[%d]" % (trail, i))
    elif isinstance(node, str):
        if (len(node) > 2 and node[1] == ":") or node.startswith("\\\\") or node.startswith("/"):
            hits.append((trail, node))
    return hits


def check_assets(pkg):
    """返回 [(级别, 说明)]；级别 info 表示如实标注的未执行项。"""
    out = []
    cfg = pkg.config
    for path, value in _abs_paths(cfg):
        out.append(("fail", "配置写了绝对路径：%s = %s" % (path, value)))

    for i, layer in enumerate(cfg.get("layers", [])):
        fp = pkg.resolve(layer["file"])
        if not os.path.isfile(fp):
            out.append(("fail", "layers[%d].file 不存在：%s" % (i, layer["file"])))
            continue
        out.append(("ok", "图层 %s → %s" % (layer["id"], layer["file"])))
        if layer["type"] == "raster-image":
            gp = pkg.resolve(layer["georef"])
            if not os.path.isfile(gp):
                out.append(("fail", "layers[%d].georef 不存在：%s" % (i, layer["georef"])))
                continue
            try:
                with open(gp, encoding="utf-8") as fh:
                    geo = json.load(fh)
                nx, ny = geo["grid"]["nx"], geo["grid"]["ny"]
                with_image = _png_size(fp)
                if with_image is None:
                    out.append(("info", "栅格 %s 尺寸未核对（无 PIL）" % layer["file"]))
                elif with_image != (nx, ny):
                    out.append(("fail", "栅格 %s 尺寸 %s 与配准 %s 不一致" % (layer["file"], with_image, (nx, ny))))
                else:
                    out.append(("ok", "栅格 %s 尺寸与配准一致 %s" % (layer["file"], (nx, ny))))
            except (KeyError, ValueError, TypeError) as exc:
                out.append(("fail", "配准文件 %s 结构不对：%s" % (layer["georef"], exc)))

    model = cfg.get("model") or {}
    if model.get("tileset"):
        tp = pkg.resolve(model["tileset"])
        if not os.path.isfile(tp):
            out.append(("fail", "model.tileset 不存在：%s" % model["tileset"]))
        else:
            n = _count_b3dm(os.path.dirname(tp))
            out.append(("ok" if n else "fail", "三维瓦片 b3dm %d 个（%s）" % (n, model["tileset"])))
    else:
        out.append(("info", "本包未提供三维模型（三维页会明确提示）"))

    for i, att in enumerate(cfg.get("attachments") or []):
        if not os.path.isfile(pkg.resolve(att["href"])):
            out.append(("fail", "attachments[%d].href 不存在：%s" % (i, att["href"])))

    return out


def _png_size(path):
    try:
        from PIL import Image
        with Image.open(path) as im:
            return im.size
    except Exception:
        return None


def _count_b3dm(folder):
    n = 0
    for _, _, files in os.walk(folder):
        n += sum(1 for f in files if f.lower().endswith(".b3dm"))
    return n


def main(argv):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if "--list" in argv:
        for d in discover(root):
            print(d)
        return 0
    project = None
    if "--project" in argv:
        i = argv.index("--project")
        if i + 1 >= len(argv):
            print("[参数错] --project 需要一个目录")
            return 2
        project = argv[i + 1]
    try:
        pkg = load(root, project)
    except PackageError as exc:
        print("[拒收] " + exc.message)
        for p in exc.problems:
            print("       " + str(p))
        return 4
    print("项目包：%s（%s）" % (pkg.rel, pkg.name))
    bad = 0
    for level, msg in check_assets(pkg):
        print("  [%s] %s" % ({"ok": "OK", "info": "说明", "fail": "FAIL"}[level], msg))
        bad += 1 if level == "fail" else 0
    print("结论：" + ("通过" if not bad else "不通过（%d 项）" % bad))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
