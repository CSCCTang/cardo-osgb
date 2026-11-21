# -*- coding: utf-8 -*-
"""07 · 模型浏览：把 OSGB 自己的产物打成一个自足的三维/二维浏览目录。

产出（成果文件/成品版/模型浏览/）：
  Start.bat            纯 ASCII 双击入口（CRLF、无 BOM），起包内服务并打开三维页
  viewer/              浏览引擎快照（页面 + Cesium/Leaflet + 项目包契约 + BUILD.json 哈希清单）
  scripts/             栅格生成脚本快照（可追溯正射/分类栅格怎么来的）
  tool/                包内 caddy（免安装静态服务）+ 许可
  projects/<id>/       project.json + model/ + raster/ + vector/
  许可登记.json         组件与许可
  说明.txt              一页使用说明（版本、入口、离线说明）

原则：
  · 数据只取 OSGB 自己的产物，不另造数；缺必填项直接报错并给字段路径；
  · 输出件不引用 Cardo交付、测试目录或任何绝对路径（打包后逐文件扫描）；
  · 引擎只做快照，运行时目录不手改。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
ASSET_DEFAULT = os.path.normpath(os.path.join(HERE, "..", "..", "..", "assets", "viewer"))

MODEL_REL = os.path.join("过程文件", "03-OSGB转3DTiles", "转换产物", "cardo-lowair-model.json")
IND_REL = os.path.join("过程文件", "06-2.5D网格指标", "现状指标.json")
VEC_OBB_REL = os.path.join("过程文件", "10-矢量入库", "矢量成果.geojson")
VEC_OUTLINE_REL = os.path.join("过程文件", "13-矢量增强", "vector-polygonize", "建筑轮廓面.geojson")
PARAMS_REL = "工作区参数.json"

PUBLIC_ROOT = "成品版"
DEMO_NAME = "模型浏览"
RASTER_DIR = "raster"
RASTER_RES = 0.25
CLASS_COLORS = (
    ("绿化", (58, 160, 74), "#3aa04a"),
    ("道路", (120, 120, 120), "#787878"),
    ("建筑屋面", (200, 120, 60), "#c8783c"),
)
EPSG_NAMES = {
    4544: ("CGCS2000 / 3-degree Gauss-Kruger CM 105E", "CGCS2000 3°GK CM105E"),
}
# 输出件里一旦出现这些字样，就等于把构建现场的路径带给了业主
FORBIDDEN_REFS = ("Cardo交付", "Cardo测试", "2026-11-13", "file:///")
TEXT_EXT = (".json", ".txt", ".html", ".js", ".css", ".bat", ".md", ".xml", ".py")


class BuildError(Exception):
    """打包失败；message 里带字段路径或文件名，直接给业主/复核人看。"""


class BrowseLocked(BuildError):
    """旧版浏览包被别的进程占用，替换不了；调用方据此跳过本件成果并留痕。"""


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8-sig") as fh:
            return json.load(fh)
    except OSError as exc:
        raise BuildError("读不到 %s：%s" % (path, exc))
    except ValueError as exc:
        raise BuildError("%s 不是合法 JSON：%s" % (path, exc))


def _write_json(path: str, value) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(value, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- 占用预检 / 复用判定
def _ps_quote(text: str) -> str:
    return "'" + str(text).replace("'", "''") + "'"


def _own_server_pids(root_dir: str) -> list:
    """列出以 root_dir 内可执行文件启动的浏览服务进程（caddy / python http.server）。

    只认「镜像路径就在旧浏览包里」的进程——那是我们自己的 Start.bat 拉的，可以安全结束；
    镜像在别处的 caddy/python 一律不碰（可能是用户别的服务，越界操作必须停下来问）。
    """
    if os.name != "nt":
        return []
    script = (
        "$root = " + _ps_quote(os.path.abspath(root_dir)) + "\n"
        "$names = @('caddy.exe','python.exe','pythonw.exe')\n"
        "try {\n"
        "  Get-CimInstance Win32_Process -ErrorAction Stop |\n"
        "    Where-Object { $_.ExecutablePath -and $names -contains $_.Name -and\n"
        "      $_.ExecutablePath.StartsWith($root, 'OrdinalIgnoreCase') } |\n"
        "    ForEach-Object { [Console]::Out.WriteLine($_.ProcessId) }\n"
        "} catch { }\n")
    try:
        proc = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    pids = []
    for line in (proc.stdout or "").splitlines():
        text = line.strip()
        if text.isdigit():
            pids.append(int(text))
    return pids


def _probe_replaceable(path: str) -> bool:
    """在不破坏目录的前提下判断能不能整目录替换。

    两个探针：①把目录改名再改回来（目录被某进程当 cwd 时会失败）；
    ②把 tool/caddy.exe 改名再改回来（镜像文件被运行时也会失败）。
    """
    parent, base = os.path.dirname(path), os.path.basename(path)
    probe = os.path.join(parent, ".probe-%d-%s" % (os.getpid(), base))
    try:
        os.rename(path, probe)
    except OSError:
        return False
    try:
        os.rename(probe, path)
    except OSError:
        return False
    exe = os.path.join(path, "tool", "caddy.exe")
    if os.path.isfile(exe):
        try:
            os.rename(exe, exe + ".probe")
        except OSError:
            return False
        try:
            os.rename(exe + ".probe", exe)
        except OSError:
            return False
    return True


def ensure_replaceable(final: str, log=print) -> tuple[bool, str]:
    """旧浏览包能不能替换：先结束自己拉起的服务，再探针复检。

    返回 (可替换?, 说明)。不可替换时不抛错——调用方跳过浏览包重建并留痕，
    不让一件可选的成果把整条链卡死（实测：用户看完模型没关 caddy，07 卡 90 秒后整链中断）。
    """
    if not os.path.isdir(final):
        return True, ""
    notes = []
    pids = _own_server_pids(final)
    if pids:
        for pid in pids:
            try:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, timeout=20)
            except (OSError, subprocess.SubprocessError):
                pass
        time.sleep(0.6)
        notes.append("已结束上次遗留的浏览服务（PID %s）" % "、".join(str(p) for p in pids))
    if _probe_replaceable(final):
        return True, "；".join(notes)
    notes.append("旧版模型浏览正被占用（不是本链拉起的进程）：请关闭浏览页/服务窗口，"
                 "或运行 成果文件/成品版/模型浏览/Start.bat stop 后重跑 07")
    return False, "；".join(notes)


FINGERPRINT_INPUTS = (
    ("model", MODEL_REL), ("indicators", IND_REL),
    ("obb", VEC_OBB_REL), ("outline", VEC_OUTLINE_REL),
)


def fingerprint(workspace: str, asset: str, model_path: str | None = None,
                indicators_path: str | None = None) -> tuple[str, dict]:
    """浏览包输入指纹：模型/指标/矢量/参数/引擎版本/构建脚本自身。

    任一输入变了就重建；全没变就复用——07 在 14 之后要重跑一次，
    过去这第二跑会把 99 MB 浏览包原样重建一遍（实测 99.9 s，占全链一半）。
    """
    workspace = os.path.abspath(workspace)
    parts = {
        "builder": _sha256(os.path.abspath(__file__)),
        "engine": _asset_version(asset),
        "rasterRes": RASTER_RES,
        "params": {key: _config_value(workspace, key)
                   for key in ("projectName", "modelSourceName", "sceneId")},
        "inputs": {},
    }
    for key, rel in FINGERPRINT_INPUTS:
        if key == "model" and model_path:
            path = os.path.abspath(model_path)
        elif key == "indicators" and indicators_path:
            path = os.path.abspath(indicators_path)
        else:
            path = os.path.join(workspace, rel)
        parts["inputs"][key] = _sha256(path) if os.path.isfile(path) else None
    digest = hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True)
                            .encode("utf-8")).hexdigest()
    return digest, parts


def reusable(workspace: str, out_dir: str, asset: str, model_path: str | None = None,
             indicators_path: str | None = None) -> dict | None:
    """旧浏览包是否可原样复用（输入指纹一致且关键件都在）。可复用则返回摘要。"""
    final = os.path.join(os.path.abspath(out_dir), PUBLIC_ROOT, DEMO_NAME)
    build_json = os.path.join(final, "viewer", "BUILD.json")
    if not (os.path.isdir(final) and os.path.isfile(build_json)
            and os.path.isfile(os.path.join(final, "Start.bat"))
            and os.path.isfile(os.path.join(final, "tool", "caddy.exe"))):
        return None
    try:
        doc = _read_json(build_json)
    except BuildError:
        return None
    digest, _parts = fingerprint(workspace, asset, model_path, indicators_path)
    if doc.get("fingerprint") != digest:
        return None
    summary = doc.get("summary") or {}
    return {
        "dir": os.path.join(PUBLIC_ROOT, DEMO_NAME).replace("\\", "/"),
        "project": doc.get("project"),
        "engineVersion": (doc.get("engine") or {}).get("version"),
        "files": doc.get("fileCount"),
        "bytes": doc.get("bytes"),
        "engineFiles": doc.get("engineFileCount"),
        "engineBytes": doc.get("engineBytes"),
        "layers": summary.get("layers"),
        "indicators": summary.get("indicators"),
        "charts": summary.get("charts"),
        "reused": True,
        "fingerprint": digest[:12],
    }


def _display_name(raw: str) -> str:
    """把「某片区_网格_质量低_简化50_osgbs」这类技术名收回业务名。"""
    name = re.sub(r"[_\-](网格|质量|简化|osgb|osgbs|模型|model).*$", "", raw or "",
                  flags=re.IGNORECASE).strip()
    return name or (raw or "").strip()


def _scene_id(workspace: str) -> str:
    path = os.path.join(workspace, PARAMS_REL)
    scene = ""
    if os.path.isfile(path):
        scene = str(_read_json(path).get("sceneId") or "").strip()
    if not scene:
        scene = "model-01"
    clean = re.sub(r"[^A-Za-z0-9-]+", "-", scene).strip("-")
    if not clean:
        raise BuildError("project.json → project.id：工作区 sceneId 无法转成纯 ASCII 标识（%r）" % scene)
    return clean


def _crs_names(epsg: int) -> tuple:
    return EPSG_NAMES.get(epsg, ("EPSG:%d" % epsg, "EPSG:%d" % epsg))


def _num(value, digits=2) -> str:
    return ("{:,.%df}" % digits).format(float(value))


def _class_shares(png_path: str) -> list:
    """分类栅格像元占比（只统计已分类像元），用于指标卡图表。"""
    try:
        import numpy as np
        from PIL import Image
    except ImportError as exc:
        raise BuildError("分类占比需要 numpy/Pillow：%s" % exc)
    with Image.open(png_path) as im:
        arr = np.array(im.convert("RGBA"))
    alpha = arr[:, :, 3]
    total = int((alpha > 0).sum())
    if total <= 0:
        raise BuildError("分类栅格 raster/class_rgba.png 没有任何已分类像元，无法生成分类占比")
    out = []
    for label, rgb, color in CLASS_COLORS:
        mask = ((arr[:, :, 0] == rgb[0]) & (arr[:, :, 1] == rgb[1]) &
                (arr[:, :, 2] == rgb[2]) & (alpha > 0))
        out.append({"label": label, "value": round(float(mask.sum()) / total * 100.0, 2),
                    "color": color})
    if sum(item["value"] for item in out) <= 0:
        raise BuildError("分类栅格 raster/class_rgba.png 的类别色与约定不一致，无法统计占比")
    return out


def _convert_geometry(node, e0: float, n0: float):
    if isinstance(node, list) and node and isinstance(node[0], (int, float)):
        return [round(float(node[0]) - e0, 3), round(float(node[1]) - n0, 3)]
    if isinstance(node, list):
        return [_convert_geometry(item, e0, n0) for item in node]
    return node


def _local_geojson(src: str, e0: float, n0: float, keys: tuple, note: str) -> dict:
    doc = _read_json(src)
    features = []
    for feat in doc.get("features") or []:
        props = feat.get("properties") or {}
        keep = {k: props[k] for k in keys if k in props}
        if "id" in props and "id" not in keep:
            keep["id"] = props["id"]
        features.append({
            "type": "Feature",
            "id": feat.get("id"),
            "properties": keep,
            "geometry": {"type": (feat.get("geometry") or {}).get("type"),
                         "coordinates": _convert_geometry(
                             (feat.get("geometry") or {}).get("coordinates"), e0, n0)},
        })
    return {
        "type": "FeatureCollection",
        "schema": "cardo/browse-vector-local/v1",
        "name": os.path.basename(src),
        "metadata": {
            "note": note,
            "coordinateMode": "local-metres",
            "originEN": [e0, n0],
            "featureCount": len(features),
            "source": os.path.basename(src),
        },
        "features": features,
    }


def _copy_engine(asset: str, staging: str) -> None:
    need = [
        os.path.join(asset, "viewer", "project.js"),
        os.path.join(asset, "viewer", "project.schema.json"),
        os.path.join(asset, "viewer", "2d", "index.html"),
        os.path.join(asset, "viewer", "3d", "index.html"),
        os.path.join(asset, "tool", "caddy.exe"),
        os.path.join(asset, "tool", "LICENSE-caddy.txt"),
        os.path.join(asset, "VERSION.json"),
        os.path.join(asset, "许可登记.json"),
    ]
    missing = [p for p in need if not os.path.isfile(p)]
    if missing:
        raise BuildError("引擎资产不完整，缺：%s" % "；".join(os.path.relpath(p, asset) for p in missing))
    shutil.copytree(os.path.join(asset, "viewer"), os.path.join(staging, "viewer"))
    shutil.copytree(os.path.join(asset, "scripts"), os.path.join(staging, "scripts"))
    shutil.copytree(os.path.join(asset, "tool"), os.path.join(staging, "tool"))
    shutil.copy2(os.path.join(asset, "VERSION.json"), os.path.join(staging, "viewer", "VERSION.json"))
    shutil.copy2(os.path.join(asset, "许可登记.json"), os.path.join(staging, "许可登记.json"))


def _write_start_bat(path: str) -> None:
    text = r"""@echo off
setlocal EnableExtensions
pushd "%~dp0"

title Cardo Model Browser

rem ============================================================
rem  Cardo model browser - local static server + browser
rem  Start : double-click this file.
rem  Stop  : run "Start.bat stop" (reads server.pid), or close
rem          the minimized server window.
rem  No install needed.
rem ============================================================

if /i "%~1"=="stop" goto stop_server
if /i "%~1"=="--stop" goto stop_server

rem ---- 0. clear a stale pid file from a previous session ----
if exist server.pid del server.pid >nul 2>nul

rem ---- 1. pick a free port (8817 -> 8820) ----
set "PORT="
for %%P in (8817 8818 8819 8820) do (
    if not defined PORT (
        netstat -ano | findstr /r /c:":%%P .*LISTENING" >nul 2>nul
        if errorlevel 1 set "PORT=%%P"
    )
)
if not defined PORT goto no_port
echo [1/4] port %PORT% is free

rem ---- 2. find a server: bundled caddy first, local python second ----
if exist "tool\caddy.exe" goto use_caddy

set "PY="
where python >nul 2>nul
if not errorlevel 1 python -c "import http.server" >nul 2>nul
if not errorlevel 1 set "PY=python"
if not defined PY (
    where py >nul 2>nul
    if not errorlevel 1 py -c "import http.server" >nul 2>nul
    if not errorlevel 1 set "PY=py"
)
if not defined PY goto no_server

echo [2/4] server: local python (bundled caddy not found)
start "Cardo Model Browser Server - close this window to stop" /min "%PY%" -m http.server %PORT% --bind 127.0.0.1
goto wait_ready

:use_caddy
echo [2/4] server: bundled caddy (no install needed)
start "Cardo Model Browser Server - close this window to stop" /min "tool\caddy.exe" file-server --root "." --listen 127.0.0.1:%PORT%

rem ---- 3. wait until the port really answers (max 10s) ----
:wait_ready
set "READY="
for /L %%I in (1,1,10) do (
    if not defined READY (
        netstat -ano | findstr /r /c:":%PORT% .*LISTENING" >nul 2>nul
        if not errorlevel 1 set "READY=1"
        if not defined READY timeout /t 1 /nobreak >nul 2>nul
    )
)
if not defined READY goto start_failed
echo [3/4] server is up

rem ---- 3b. remember the server PID so "Start.bat stop" can stop it ----
set "SRVPID="
for /f "tokens=5" %%A in ('netstat -ano ^| findstr /r /c:":%PORT% .*LISTENING"') do (
    if not defined SRVPID set "SRVPID=%%A"
)
if defined SRVPID (echo %SRVPID%)>server.pid

rem ---- 4. open 3D first, then 2D in a second tab ----
echo [4/4] opening browser
start "" "http://127.0.0.1:%PORT%/viewer/3d/index.html"
timeout /t 1 /nobreak >nul 2>nul
start "" "http://127.0.0.1:%PORT%/viewer/2d/index.html"
echo.
echo   3D page : http://127.0.0.1:%PORT%/viewer/3d/index.html
echo   2D page : http://127.0.0.1:%PORT%/viewer/2d/index.html
echo   Server  : keep the minimized window open while browsing.
echo   If a page is blank, wait 3 seconds and press F5 once.
echo.
echo   This window closes itself in 8 seconds.
timeout /t 8 >nul 2>nul
popd
exit /b 0

:start_failed
echo.
echo   [ERROR] The server did not start on port %PORT%.
echo   Try again: double-click Start.bat once more (it will pick another port).
echo   If it keeps failing, check antivirus / firewall is not blocking tool\caddy.exe.
echo.
pause
popd
exit /b 1

:no_port
echo.
echo   [ERROR] Ports 8817-8820 are all in use.
echo   Close other browser sessions and run this again.
echo.
pause
popd
exit /b 1

:no_server
echo.
echo   [ERROR] tool\caddy.exe is missing and no Python was found.
echo   Copy the whole folder again, or restore tool\caddy.exe.
echo.
pause
popd
exit /b 1

:stop_server
echo.
echo   [stop] stopping the Cardo model browser server ...
if not exist server.pid (
    echo   No server.pid in this folder - nothing started from here.
    popd
    exit /b 0
)
set "SRVPID="
set /p SRVPID=<server.pid
if not defined SRVPID (
    echo   server.pid is empty - nothing to stop.
    del server.pid >nul 2>nul
    popd
    exit /b 0
)
echo   PID %SRVPID%
taskkill /PID %SRVPID% /T /F >nul 2>nul
if errorlevel 1 (
    echo   Process %SRVPID% is not running any more.
) else (
    echo   Server stopped.
)
del server.pid >nul 2>nul
echo.
popd
exit /b 0
"""
    data = text.replace("\r\n", "\n").replace("\n", "\r\n").encode("ascii")
    if data.startswith(b"\xef\xbb\xbf"):
        raise BuildError("Start.bat 不得带 BOM")
    with open(path, "wb") as fh:
        fh.write(data)


def _write_help(path: str, project: dict, version: str, built_at: str) -> None:
    text = """模型浏览 · 使用说明

版本：{version}
生成：{built_at}
项目：{name}

一、怎么打开
  1. 双击 Start.bat，浏览器会自动打开三维页，并另开一个二维页标签。
  2. 包内自带免安装服务（tool\\caddy.exe），不需要联网、不需要安装软件、不需要账号。
  3. 停止服务：运行 `Start.bat stop`（按 server.pid 精确结束），或直接关闭启动时最小化的
     服务窗口。再次双击会自动换端口；查看模型时请勿删除 server.pid。

二、看什么
  三维页：整体模型、贴图、LOD 逐级加载；鼠标左键旋转、滚轮缩放。
  二维页：正射影像、建筑分类、建筑轮廓面/外接矩形；点选建筑可看面积、高度、层数等属性；
          右侧为指标卡与指标说明。

三、口径
  指标为 2.5D 网格几何估算，附区间与置信度；绿化率、道路占比为纹理覆盖代理值，
  分母含建筑屋面，不是平面用地面积。指标数值与《指标说明》同源。

四、目录
  Start.bat        启动入口（纯 ASCII）
  viewer\\          浏览引擎快照（页面 + Cesium/Leaflet + 项目包契约）
  projects\\        项目数据（project.json、三维模型、正射/分类栅格、矢量）
  tool\\            包内静态服务 caddy 2.10.0（Apache-2.0，许可随附）
  scripts\\         正射/分类栅格的生成脚本快照
  许可登记.json     第三方组件与许可
  server.pid        运行期记录的服务进程号（双击 Start.bat 后生成，停服后可删）

五、版本与校验
  引擎版本见 viewer\\VERSION.json；本次快照的体积、文件数与 sha256 清单见 viewer\\BUILD.json。
""".format(version=version, built_at=built_at, name=project.get("name", ""))
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def _self_containment_scan(staging: str, project_dir: str) -> list:
    """扫描运行时会读的文本文件：不得出现构建现场路径/绝对路径/外链文件的引用。"""
    hits = []
    targets = []
    for base, _dirs, files in os.walk(staging):
        for name in files:
            if not name.lower().endswith(TEXT_EXT):
                continue
            path = os.path.join(base, name)
            rel = os.path.relpath(path, staging).replace("\\", "/")
            if rel.startswith("viewer/vendor/") or rel == "许可登记.json":
                continue
            targets.append(path)
    abs_re = re.compile(r"(?i)(^|[\s\"'(=])([a-z]:[\\/]|\\\\[a-z0-9_.-]+[\\/])")
    for path in targets:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        rel = os.path.relpath(path, staging).replace("\\", "/")
        for token in FORBIDDEN_REFS:
            if token in text:
                hits.append("%s 含 %r" % (rel, token))
        for m in abs_re.finditer(text):
            hits.append("%s 含绝对路径 %r" % (rel, m.group(0).strip()))
    # 三维瓦片内容路径：tileset 内的 uri 必须是相对路径
    tileset = os.path.join(project_dir, "model", "tileset.json")
    if os.path.isfile(tileset):
        doc = _read_json(tileset)
        stack = [doc]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                uri = node.get("uri")
                if isinstance(uri, str) and (uri.startswith("file:") or re.match(r"(?i)^[a-z]:", uri)):
                    hits.append("model/tileset.json 含绝对 uri %r" % uri)
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
    return hits


def _engine_manifest(staging: str, version: str, built_at: str, project_rel: str,
                     fingerprint: str = "", summary: dict | None = None) -> dict:
    """全包逐件清单：08 按它核对文件数与 sha256，因此必须覆盖整个浏览目录。"""
    skip = {"viewer/BUILD.json", "viewer/BUILD-SHA256.txt"}
    engine_roots = ("viewer", "tool", "scripts")
    files, engine_files = [], []
    for root, _dirs, names in os.walk(staging):
        for name in sorted(names):
            path = os.path.join(root, name)
            rel = os.path.relpath(path, staging).replace("\\", "/")
            if rel in skip:
                continue
            item = {"path": rel, "bytes": os.path.getsize(path), "sha256": _sha256(path)}
            files.append(item)
            if rel.split("/", 1)[0] in engine_roots:
                engine_files.append(item)
    files.sort(key=lambda item: item["path"])
    engine_files.sort(key=lambda item: item["path"])
    manifest = {
        "schema": "cardo/viewer-build/v1",
        "scope": "package",
        "engine": {
            "name": "模型浏览引擎",
            "version": version,
            "snapshot": "CardoViewer 项目包查看器",
            "source": "CardoOSGB assets/viewer",
        },
        "builtAt": built_at,
        "project": project_rel,
        # 输入指纹：07 重跑时据此判断能否原样复用（见 reusable()）
        "fingerprint": fingerprint,
        "summary": dict(summary or {}),
        "fileCount": len(files),
        "bytes": sum(item["bytes"] for item in files),
        "engineFileCount": len(engine_files),
        "engineBytes": sum(item["bytes"] for item in engine_files),
        "files": files,
    }
    _write_json(os.path.join(staging, "viewer", "BUILD.json"), manifest)
    # 清单含中文文件名（如 说明.txt），按 UTF-8 写；机器核对以 BUILD.json 为准
    with open(os.path.join(staging, "viewer", "BUILD-SHA256.txt"), "w",
              encoding="utf-8", newline="\n") as fh:
        for item in files:
            fh.write("%s  %s\n" % (item["sha256"], item["path"]))
    return manifest


def build(workspace: str, out_dir: str, asset_dir: str | None = None,
          model_path: str | None = None, indicators_path: str | None = None,
          project_name: str = "", log=print) -> dict:
    """生成自足浏览目录，返回可写进导出日志的摘要。"""
    workspace = os.path.abspath(workspace)
    out_dir = os.path.abspath(out_dir)
    asset = os.path.abspath(asset_dir or ASSET_DEFAULT)
    model_path = os.path.abspath(model_path or os.path.join(workspace, MODEL_REL))
    ind_path = os.path.abspath(indicators_path or os.path.join(workspace, IND_REL))
    for label, path in (("model", model_path), ("indicators", ind_path)):
        if not os.path.isfile(path):
            raise BuildError("缺输入 %s：%s" % (label, path))

    model = _read_json(model_path)
    ind = _read_json(ind_path)
    if model.get("schema") != "cardo/lowair-model/v1":
        raise BuildError("project.json → model：模型清单 schema 不符（%s）" % model.get("schema"))
    if ind.get("schema") != "cardo/lowair-indicators/v1":
        raise BuildError("project.json → indicators：现状指标 schema 不符（%s）" % ind.get("schema"))

    try:
        epsg = int(str((model.get("crs") or {}).get("srs", "")).split(":")[-1])
    except ValueError:
        raise BuildError("project.json → crs.epsg：模型清单 crs.srs 不是 EPSG 代码")
    origin = (model.get("crs") or {}).get("origin") or []
    if len(origin) < 2:
        raise BuildError("project.json → crs.origin.E/N：模型清单 crs.origin 不完整")
    e0, n0 = float(origin[0]), float(origin[1])
    vertical = str((model.get("crs") or {}).get("vertical") or "")
    crs_name, crs_short = _crs_names(epsg)

    bbox = (((model.get("geometry") or {}).get("bboxLocal") or {}).get("min"),
            ((model.get("geometry") or {}).get("bboxLocal") or {}).get("max"))
    if not bbox[0] or not bbox[1]:
        raise BuildError("project.json → crs.extentLocalM：模型清单缺 geometry.bboxLocal")

    raw_name = project_name or _config_project_name(workspace) or \
        str((model.get("source") or {}).get("name") or "")
    if not raw_name.strip():
        raise BuildError("project.json → project.name：项目名称无来源（工作区参数与模型清单均为空）")
    name = _display_name(raw_name)
    # 模型来源同样只从配置或本轮模型清单取：换一个模型包，来源行跟着换，不在代码里写死。
    source_label = _config_value(workspace, "modelSourceName") or \
        str((model.get("source") or {}).get("name") or "").strip()
    scene = _scene_id(workspace)
    built_at = _now()

    staging = os.path.join(out_dir, PUBLIC_ROOT, ".%s_build" % DEMO_NAME)
    final = os.path.join(out_dir, PUBLIC_ROOT, DEMO_NAME)
    if os.path.isdir(staging):
        shutil.rmtree(staging)
    # 先做占用预检：旧版浏览页还开着（tool\caddy.exe 在跑）时，先结束自己拉起的服务；
    # 结束不了就明确报「不可替换」，由调用方跳过本件成果并留痕——不再静默卡 90 秒后中断全链。
    ok, note = ensure_replaceable(final, log)
    if note:
        log("[07] 模型浏览：" + note)
    if not ok:
        raise BrowseLocked(note.replace("；", "；\n  "))
    # 先删旧成品：本轮任何一步失败都不留下「看起来像新成果」的旧目录
    if os.path.isdir(final):
        try:
            shutil.rmtree(final)
        except PermissionError:
            raise BrowseLocked(
                "旧版模型浏览正在被占用，无法替换：%s\n"
                "  多为浏览页还开着（tool\\caddy.exe 在运行）。请先运行 Start.bat stop，"
                "或关闭浏览页与服务窗口，再重跑本步骤。" % final)
    os.makedirs(staging)
    _copy_engine(asset, staging)

    project_rel = "projects/%s" % scene
    pkg_dir = os.path.join(staging, *project_rel.split("/"))
    os.makedirs(pkg_dir)
    _copy_model_dir(os.path.dirname(model_path), os.path.join(pkg_dir, "model"))
    tileset_rel = "model/tileset.json"
    manifest_rel = "model/cardo-lowair-model.json"
    manifest_copy = os.path.join(pkg_dir, *manifest_rel.split("/"))
    if not os.path.isfile(manifest_copy):
        raise BuildError("project.json → model.tileset：转换产物缺 cardo-lowair-model.json")
    # 模型清单是随模型带过来的原始文件名（含输入包名），成品里统一改成本轮认定的来源名，
    # 避免把输入侧的技术文件名带进业主可读的浏览目录。
    if source_label:
        man = _read_json(manifest_copy)
        src = man.get("source")
        if isinstance(src, dict):
            src["name"] = source_label
            if isinstance(src.get("zip"), dict):
                src["zip"]["name"] = source_label
            _write_json(manifest_copy, man)

    tiles = model.get("tiles") or {}
    area = float((ind.get("scope") or {}).get("areaM2") or 0.0)
    description = "三维瓦片 %s 个 · 正射影像 %.2f m/px · 覆盖 %s m²" % (
        _num(tiles.get("count") or 0, 0), RASTER_RES, _num(area, 0))
    project_info = {
        "id": scene,
        "name": name,
        "subtitle": "三维模型与二维图层",
        "description": description,
    }
    cfg = {
        "schema": "cardo/project-package/v1",
        "project": project_info,
        "crs": {
            "epsg": epsg,
            "name": crs_name,
            "shortName": crs_short,
            "vertical": vertical,
            "origin": {"E": e0, "N": n0, "h": float(origin[2]) if len(origin) > 2 else 0.0},
            "extentLocalM": [float(bbox[0][0]), float(bbox[0][1]),
                             float(bbox[1][0]), float(bbox[1][1])],
        },
        "model": {"tileset": tileset_rel},
        "layers": _raster_layers(),
        "indicators": _indicators(ind),
        "text": _text(model, ind),
        "dataSource": {
            "generatedBy": "CardoOSGB 07 成果导出",
            "generatedAt": built_at,
            "notes": ("模型来源：%s；" % source_label if source_label else "") +
                     "三维瓦片、正射/分类栅格、矢量与指标均取自本次 OSGB 运行。",
        },
        "build": {"rasterFromModel": {"manifest": manifest_rel, "outDir": RASTER_DIR,
                                      "resM": RASTER_RES}},
    }
    _write_json(os.path.join(pkg_dir, "project.json"), cfg)

    # 正射 + 分类栅格：用资产里的同一套脚本从模型现算，避免另写一套算法
    _build_rasters(asset, pkg_dir, cfg)

    ortho_geo = _read_json(os.path.join(pkg_dir, RASTER_DIR, "ortho_georef.json"))
    class_geo = _read_json(os.path.join(pkg_dir, RASTER_DIR, "class_georef.json"))
    ext = ortho_geo.get("extentLocalM")
    if not (isinstance(ext, list) and len(ext) == 4):
        raise BuildError("project.json → crs.extentLocalM：正射配准缺 extentLocalM")
    if class_geo.get("extentLocalM") != ext:
        raise BuildError("project.json → layers：正射与分类栅格范围不一致")
    cfg["crs"]["extentLocalM"] = ext

    layers = _raster_layers()
    layers += _vector_layers(workspace, pkg_dir, e0, n0, log)
    cfg["layers"] = layers

    charts = _charts(ind)
    shares = _class_shares(os.path.join(pkg_dir, RASTER_DIR, "class_rgba.png"))
    charts.append({"title": "分类栅格像元占比（已分类像元）", "unit": "%", "series": shares})
    cfg["charts"] = charts

    _write_json(os.path.join(pkg_dir, "project.json"), cfg)
    with open(os.path.join(staging, "viewer", "default-project.txt"), "w",
              encoding="ascii", newline="\n") as fh:
        fh.write(project_rel + "\n")

    _write_start_bat(os.path.join(staging, "Start.bat"))
    _write_help(os.path.join(staging, "说明.txt"), project_info,
                _asset_version(asset), built_at)

    # 契约校验：与页面/开工.py 同一份 schema、同一套资产检查
    _validate(asset, staging, project_rel)

    version = _asset_version(asset)
    digest, _parts = fingerprint(workspace, asset, model_path, ind_path)
    manifest = _engine_manifest(staging, version, built_at, project_rel, digest, {
        "layers": len(cfg["layers"]),
        "indicators": len(cfg["indicators"]["items"]),
        "charts": len(cfg["charts"]),
    })
    hits = _self_containment_scan(staging, pkg_dir)
    if hits:
        raise BuildError("自足性检查未通过（%d 处）：%s" % (len(hits), "；".join(hits[:8])))

    os.replace(staging, final)

    stats = _tree_stats(final)
    log("[07] 模型浏览：%s（%d 文件，%.1f MB；引擎 %d 文件，%.1f MB）" % (
        os.path.join(PUBLIC_ROOT, DEMO_NAME), stats["files"], stats["bytes"] / 1048576.0,
        manifest["engineFileCount"], manifest["engineBytes"] / 1048576.0))
    return {
        "dir": os.path.join(PUBLIC_ROOT, DEMO_NAME).replace("\\", "/"),
        "project": project_rel,
        "engineVersion": version,
        "files": stats["files"],
        "bytes": stats["bytes"],
        "engineFiles": manifest["engineFileCount"],
        "engineBytes": manifest["engineBytes"],
        "layers": len(cfg["layers"]),
        "indicators": len(cfg["indicators"]["items"]),
        "charts": len(cfg["charts"]),
        "reused": False,
        "fingerprint": digest[:12],
    }


def _config_value(workspace: str, key: str) -> str:
    """读工作区参数里的单个字段；没有配置文件就返回空串（不猜、不兜底别的项目名）。"""
    path = os.path.join(workspace, PARAMS_REL)
    if not os.path.isfile(path):
        return ""
    return str(_read_json(path).get(key) or "").strip()


def _config_project_name(workspace: str) -> str:
    return _config_value(workspace, "projectName")


def _asset_version(asset: str) -> str:
    path = os.path.join(asset, "VERSION.json")
    if not os.path.isfile(path):
        raise BuildError("引擎资产缺 VERSION.json")
    version = str(_read_json(path).get("version") or "").strip()
    if not version:
        raise BuildError("引擎资产 VERSION.json 缺 version")
    return version


def _copy_model_dir(src: str, dst: str) -> None:
    if not os.path.isdir(src):
        raise BuildError("project.json → model.tileset：转换产物目录不存在（%s）" % src)
    # .work/ 是 04 步的幂等标记（配准位移记录），属于过程文件，不进成品；
    # 日志/备份一并不带，保证浏览目录里只有能直接给业主的东西。
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(
        "*.pyc", "__pycache__", ".work", "*.log", "~$*"))
    if not os.path.isfile(os.path.join(dst, "tileset.json")):
        raise BuildError("project.json → model.tileset：转换产物缺 tileset.json")


def _raster_layers() -> list:
    return [
        {
            "id": "ortho",
            "name": "正射影像",
            "type": "raster-image",
            "file": RASTER_DIR + "/ortho_northup.png",
            "georef": RASTER_DIR + "/ortho_georef.json",
            "default": True,
            "opacity": 1.0,
        },
        {
            "id": "class",
            "name": "建筑分类",
            "type": "raster-image",
            "file": RASTER_DIR + "/class_rgba.png",
            "georef": RASTER_DIR + "/class_georef.json",
            "default": False,
            "opacity": 0.75,
            "opacityControl": True,
            "legend": [{"color": color, "label": label} for label, _rgb, color in CLASS_COLORS],
        },
    ]


def _build_rasters(asset: str, pkg_dir: str, cfg: dict) -> None:
    scripts = os.path.join(asset, "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    try:
        import build_ortho
    except ImportError as exc:
        raise BuildError("引擎资产缺栅格脚本：%s" % exc)
    try:
        build_ortho.build(pkg_dir, cfg, force=True)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise BuildError("正射/分类栅格生成失败：%s: %s" % (type(exc).__name__, exc))


def _vector_layers(workspace: str, pkg_dir: str, e0: float, n0: float, log) -> list:
    layers = []
    outline_src = os.path.join(workspace, VEC_OUTLINE_REL)
    if os.path.isfile(outline_src):
        doc = _local_geojson(
            outline_src, e0, n0,
            ("id", "areaM2", "holeCount", "vertexCount"),
            "坐标为项目包局地米制（投影坐标已减 crs.origin）；来源 13 建筑轮廓面。")
        _write_json(os.path.join(pkg_dir, "vector", "buildings-outline.geojson"), doc)
        layers.append({
            "id": "buildings-outline",
            "name": "建筑轮廓面",
            "type": "vector-polygon",
            "file": "vector/buildings-outline.geojson",
            "default": True,
            "labelKey": "id",
            "style": {"color": "#8b3a2e", "weight": 1.4, "fill": True,
                      "fillColor": "#c8783c", "fillOpacity": 0.18},
            "popup": {
                "titleKey": "id",
                "fields": [
                    {"key": "areaM2", "label": "面积", "unit": "m²", "digits": 1},
                    {"key": "vertexCount", "label": "轮廓顶点", "digits": 0},
                    {"key": "holeCount", "label": "洞数", "digits": 0},
                ],
                "source": "来源：13 矢量增强 · 建筑轮廓面",
            },
        })
    obb_src = os.path.join(workspace, VEC_OBB_REL)
    if os.path.isfile(obb_src):
        doc = _local_geojson(
            obb_src, e0, n0,
            ("id", "areaM2", "heightP50", "heightP90", "heightMax", "floorsEst",
             "confidence", "fillRatio", "widthM", "heightM", "angleDeg"),
            "坐标为项目包局地米制（投影坐标已减 crs.origin）；来源 10 矢量成果（OBB 外接矩形）。")
        _write_json(os.path.join(pkg_dir, "vector", "buildings-obb.geojson"), doc)
        layers.append({
            "id": "buildings-obb",
            "name": "建筑外接矩形",
            "type": "vector-polygon",
            "file": "vector/buildings-obb.geojson",
            "default": False,
            "labelKey": "id",
            "style": {"color": "#2e6f8b", "weight": 1.0, "fill": False},
            "popup": {
                "titleKey": "id",
                "fields": [
                    {"key": "areaM2", "label": "面积", "unit": "m²", "digits": 1},
                    {"key": "heightP50", "label": "高度 P50", "unit": "m", "digits": 2},
                    {"key": "heightP90", "label": "高度 P90", "unit": "m", "digits": 2},
                    {"key": "floorsEst", "label": "估算层数", "unit": "层", "digits": 0},
                    {"key": "confidence", "label": "置信度"},
                    {"key": "fillRatio", "label": "填充率", "digits": 2},
                ],
                "source": "来源：10 矢量入库 · 建筑外接矩形",
            },
        })
    if not layers:
        log("[07] 模型浏览：本次运行尚无 10/13 矢量产物，二维页仅含正射与分类栅格")
    return layers


def _indicator_item(metric: dict) -> dict:
    mid = metric.get("id")
    value = metric.get("value")
    unit = metric.get("unit") or ""
    rng = metric.get("range") or []
    name = metric.get("name") or mid
    note = ""
    percent_ids = ("building_coverage_ratio", "greenery_ratio", "road_ratio")
    if mid in percent_ids:
        shown = float(value) * 100.0
        text = _num(shown, 2)
        unit = "%"
        if len(rng) == 2:
            note += "区间 %s–%s%%" % (_num(float(rng[0]) * 100, 2), _num(float(rng[1]) * 100, 2))
    elif unit == "栋":
        text = _num(value, 0)
        if len(rng) == 2:
            note += "区间 %s–%s 栋" % (_num(rng[0], 0), _num(rng[1], 0))
    elif unit == "m":
        text = _num(value, 2)
        if len(rng) == 2:
            note += "区间 %s–%s m" % (_num(rng[0], 2), _num(rng[1], 2))
    else:
        text = _num(value, 2)
        if unit == "1":
            unit = ""
        if len(rng) == 2:
            note += "区间 %s–%s" % (_num(rng[0], 2), _num(rng[1], 2))
    if mid in ("greenery_ratio", "road_ratio"):
        note = "纹理覆盖代理；" + note
    elif mid == "building_count":
        note = "候选栋数；" + note
    elif mid in ("building_coverage_ratio", "building_height_p50", "building_height_p90",
                 "floor_area_ratio_est"):
        note = "2.5D 网格估算；" + note
    return {
        "label": name,
        "value": text,
        "unit": unit,
        "note": note.rstrip("；"),
        "basis": "估算" if metric.get("grade") == "estimated" else "",
    }


def _indicators(ind: dict) -> dict:
    items = [_indicator_item(m) for m in ind.get("metrics") or []]
    if not items:
        raise BuildError("project.json → indicators.items：现状指标为空")
    return {"title": "现状指标（2.5D 网格估算）", "items": items}


def _text(model: dict, ind: dict) -> dict:
    scope = ind.get("scope") or {}
    tiles = model.get("tiles") or {}
    geom = model.get("geometry") or {}
    textures = model.get("textures") or {}
    area = float(scope.get("areaM2") or 0.0)
    xy = scope.get("extentM") or {}
    vertical = str((model.get("crs") or {}).get("vertical") or "")
    overview = [
        {"title": "数据范围",
         "body": "模型覆盖范围 %s m²（%.3f m × %.3f m），%s。二维正射影像与分类栅格按 %.2f m/px 由模型顶面纹理生成。" % (
             _num(area, 2), float(xy.get("x") or 0), float(xy.get("y") or 0),
             scope.get("crs") or "", RASTER_RES)},
        {"title": "三维模型",
         "body": "三维瓦片 %s 个（b3dm %s 个），顶点 %s 个，纹理 %s 张；高程基准 %s。" % (
             _num(tiles.get("count") or 0, 0), _num(tiles.get("b3dmCount") or 0, 0),
             _num(geom.get("vertices") or 0, 0), _num(textures.get("images") or 0, 0),
             vertical or "未声明")},
        {"title": "指标口径",
         "body": "指标为 2.5D 网格几何估算，均附区间与置信度；绿化率、道路占比为纹理覆盖代理值，"
                 "分母含建筑屋面，不是平面用地面积。"},
    ]
    return {
        "overview": overview,
        "footer": "本页为离线浏览；指标为估算值并附区间，不构成精度达标结论。",
    }


def _charts(ind: dict) -> list:
    values = {m.get("id"): m for m in ind.get("metrics") or []}
    charts = []
    green = values.get("greenery_ratio")
    road = values.get("road_ratio")
    if green and road:
        g = round(float(green["value"]) * 100, 2)
        r = round(float(road["value"]) * 100, 2)
        other = round(max(0.0, 100.0 - g - r), 2)
        charts.append({
            "title": "覆盖构成（纹理覆盖代理，同一分母）",
            "unit": "%",
            "series": [
                {"label": "绿化", "value": g, "color": "#3aa04a"},
                {"label": "道路", "value": r, "color": "#787878"},
                {"label": "其他（屋面/硬质等）", "value": other, "color": "#b9b2a6"},
            ],
        })
    p50 = values.get("building_height_p50")
    p90 = values.get("building_height_p90")
    if p50 and p90:
        charts.append({
            "title": "建筑高度分位",
            "unit": " m",
            "series": [
                {"label": "P50", "value": round(float(p50["value"]), 2), "color": "#5aa46a"},
                {"label": "P90", "value": round(float(p90["value"]), 2), "color": "#c8783c"},
            ],
        })
    if not charts:
        raise BuildError("project.json → charts：指标不足，无法生成图表")
    return charts


def _validate(asset: str, staging: str, project_rel: str) -> None:
    scripts = os.path.join(asset, "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    try:
        import project_pack
    except ImportError as exc:
        raise BuildError("引擎资产缺 project_pack.py：%s" % exc)
    try:
        pkg = project_pack.load(staging, project_rel)
    except project_pack.PackageError as exc:
        problems = [str(p) for p in (exc.problems or [])] or [exc.message]
        raise BuildError("项目包契约校验未通过（%d 处）：%s" % (len(problems), "；".join(problems[:8])))
    fails = [msg for level, msg in project_pack.check_assets(pkg) if level == "fail"]
    if fails:
        raise BuildError("项目包资产校验未通过（%d 处）：%s" % (len(fails), "；".join(fails[:8])))


def _tree_stats(root: str) -> dict:
    files = 0
    size = 0
    for base, _dirs, names in os.walk(root):
        for name in names:
            files += 1
            size += os.path.getsize(os.path.join(base, name))
    return {"files": files, "bytes": size}


def main() -> int:
    ap = argparse.ArgumentParser(description="07 · 模型浏览：生成自足三维/二维浏览目录")
    ap.add_argument("--workspace", default=".")
    ap.add_argument("--out", default=os.path.join("成果文件"))
    ap.add_argument("--asset", default=ASSET_DEFAULT)
    ap.add_argument("--model", default="")
    ap.add_argument("--indicators", default="")
    ap.add_argument("--project-name", default="")
    args = ap.parse_args()
    try:
        info = build(args.workspace, args.out, args.asset or None,
                     args.model or None, args.indicators or None, args.project_name)
    except BuildError as exc:
        print("[FAIL] " + str(exc), file=sys.stderr)
        return 3
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
