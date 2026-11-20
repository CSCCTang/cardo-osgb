#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""00 · 算法清单与来源导出：把 `算法注册表.json` 渲染成 xlsx（含表内 21 项对照）。

三张主表：
  1) 表内21项对照 —— 调研表里的 21 个开源项目，本链用了几个、为什么没用、现有替代
  2) 本链已落地   —— 现在就能跑的 26 个算法/能力：做什么、输入输出、来源、许可、实现方式、落点
  3) 待条件       —— 27 个现在跑不了的：缺什么、补上什么就能跑
  4) 依赖与版本   —— pip 依赖与第三方工具，含许可与本链用途

单一真源：算法注册表.json（本脚本只读，不改）。表内 21 项摘自
`提资数据/课题资料/调研无人机算法平台功能汇总表.xlsx` 的「开源」表，逐项对照。

用法（工作区根）：
  python Skills/00-Cardo低空解析总控/scripts/export_algo_table.py --workspace .
退出码：0 成功；2 注册表缺失 / 参数非法。
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
# 技能根：本脚本在 <技能根>/00-Cardo低空解析总控/scripts/ 下，
# 所以技能根 = HERE/../..（交付包模式下技能包在工作区**之外**，不能靠工作区找 Skills/）。
SKILLS_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
REG_REL = os.path.join("Skills", "00-Cardo低空解析总控", "算法注册表.json")
# 注册表的**权威位置**：技能包内（永远存在），工作区内的副本只作兼容。
REG_IN_PACKAGE = os.path.join(SKILLS_ROOT, "00-Cardo低空解析总控", "算法注册表.json")
OUT_NAME = "算法清单与来源.xlsx"

# 表内 21 项（摘自调研表的「开源」sheet，字段顺序同原表）
# (要素类别, 项目/算法, 链接, 原表适配度, 本链是否采用, 不采用原因, 本链现有替代)
TABLE_INTERNAL = [
    ("① 建筑轮廓（栋数）", "PolyR-CNN", "https://github.com/HeinzJiao/PolyR-CNN", "★★★★★ 高分辨率航拍首选",
     "否", "需要高分辨率正射影像与 GPU 权重；本工作区只有重建后的 OSGB，没有原片",
     "recon.instance：几何实例化出 13 栋 + 置信度（轮廓是最小外接矩形，不是真实边界）"),
    ("① 建筑轮廓（栋数）", "HD-Net", "https://github.com/danfenghong/ISPRS_HD-Net", "★★★★☆",
     "否", "同上：需要影像与权重", "recon.instance"),
    ("① 建筑轮廓（栋数）", "MAP-Net", "https://github.com/lehaifeng/MAPNet", "★★★★☆",
     "否", "同上：需要影像与权重", "recon.instance"),
    ("① 建筑轮廓（栋数）", "rs_buildings_extraction", "https://github.com/geocompass/rs_buildings_extraction", "★★★★★ 可直接进业务系统",
     "否", "Web 工具 + 影像训练/预测链路，需要底图与样本", "recon.instance + 10 的矢量拓扑清理"),
    ("① 建筑轮廓（栋数）", "HouseBoundaryPrediction", "https://github.com/CVLABLUMS/HouseBoundaryPrediction", "★★★★☆",
     "否", "需要影像，且训练集为南亚城市样本", "recon.instance"),
    ("② 层数 / 高度 / 建筑面积", "BDH-Net", "https://github.com/wishx98/BDHNet", "★★★★★",
     "否", "单目影像回归层数/高度，需影像与权重；仓库多为训练脚本", "recon.object-height：nDSM 的 P50/P90 + 3.0 m 层高，给 2.8/3.3 m 区间"),
    ("② 层数 / 高度 / 建筑面积", "Drone-based-building-assessment", "https://github.com/Dhruv2012/Drone-based-building-assessment", "★★★★★ 原生无人机项目",
     "否", "需要无人机原片；无法从 OSGB 反推照片", "recon.object-height"),
    ("③ 危房 / 结构隐患", "DBA-RTMDet", "https://github.com/Kangsan-Y/UAV-based-Damaged-Buildings-Instance-Segmentation-model-DBA-RTMDet", "★★★★★",
     "否", "需要震损无人机影像与 GPU 权重", "无（需近景影像，几何反演做不了结构损伤）"),
    ("③ 危房 / 结构隐患", "ABECIS", "https://github.com/Pi-31415/ABECIS", "★★★★★ 开箱即用",
     "否", "需要近景墙面照片/视频，倾斜摄影分辨率不够", "无（imagery.crack 已登记，待近景数据）"),
    ("③ 危房 / 结构隐患", "YOLO-AMC", "https://github.com/CY-Tsai24/YOLO-AMC", "★★★★☆",
     "否", "需要裂缝近景影像与权重", "无"),
    ("③ 危房 / 结构隐患", "BuildingInspectionDeepL", "https://github.com/96korbi/BuildingInspectionDeepL", "★★★☆☆",
     "否", "需要视频流/近景影像", "无"),
    ("③ 危房 / 结构隐患", "CNN_structural_damages", "https://github.com/fnex/CNN_structural_damages", "★★★★☆",
     "否", "需要影像与 .h5 权重，识别的是废墟瓦砾", "无"),
    ("④ 历史保护要素", "建筑年代与风格识别", "https://github.com/renhai-lab/Paper_Replication--Understanding-architecture-age-and-style-through-deep-learning", "★★★☆☆ 需本地样本微调",
     "否", "需要街景影像；样本为国外城市", "无"),
    ("⑤ 低效 / 闲置空间", "Large-scale Urban Vacant Land", "https://github.com/SkydustZ/Large-scale-Automatic-Identification-of-Urban-Vacant-Land", "★★★★★ 大规模城市空地",
     "否", "需要正射影像；附深圳/北京/兰州权重", "无（imagery.vacant-land 已登记，待影像）"),
    ("⑤ 低效 / 闲置空间", "Philadelphia_vacant_lot_detection", "https://github.com/teresachang11/Philadelphia_vacant_lot_detection", "★★★★☆ 卫星图像、国外样本",
     "否", "卫星影像 + 国外样本", "无"),
    ("⑤ 低效 / 闲置空间", "Strategic-Urban-Greening", "https://github.com/AryanSahu021/Strategic-Urban-Greening-using-AI-Driven-Semantic-Segmentation-of-Satellite-Imagery", "★★★★☆ 1 m RGB、国外样本",
     "否", "卫星影像 + 国外样本", "无"),
    ("⑥ 绿化覆盖斑块", "green-area-detection-with-drones", "https://github.com/tyrerodr/green-area-detection-with-drones", "★★★★★",
     "否", "需要 5–30 m 航高影像；纯几何分不开草地/铺装",
     "05 纹理颜色诊断只给参考（绿 17.0%），不进指标；绿化率仍为 null"),
    ("⑦ 街巷条数 / 路幅宽度", "Regularized-Road-Extractor", "https://github.com/jingjing-yan-8118/Regularized-Road-Extractor", "★★★★★",
     "否", "需要影像 + 中心线先验 + 权重", "无（几何反演无法可靠出路幅）"),
    ("⑦ 街巷条数 / 路幅宽度", "CGCNet", "https://github.com/LPeng625/CGCNet", "★★★★☆",
     "否", "需要影像与权重", "无"),
    ("⑧ 底座工具链", "GeoAI", "https://github.com/opengeos/geoai", "★★★★★",
     "否", "集成 20+ 遥感基础模型，需要影像与 GPU；本链只读了重建结果",
     "部分能力用纯几何复现：建筑掩膜走 nDSM（recon.instance）"),
    ("⑧ 底座工具链", "OpenDroneMap / WebODM", "https://github.com/OpenDroneMap/ODM", "★★★★★",
     "否", "需要原始照片 + POS + 相机参数；ODM 不能从 OSGB 反推原片",
     "03 用 3dtile.exe 把 OSGB 转 3D Tiles，替代「看得见模型」这一段；重建本身不可替代"),
]

# 依赖与版本（版本取自 requirements.txt 约束 + 本机实测）
DEPS = [
    ("3dtile.exe（fanvanzh/3dtiles v0.4）", "Make 3tile program 1.0", "Apache-2.0", "03 OSGB→3D Tiles（唯一转换器）", "必需（缺则 exit 2）"),
    ("numpy", ">=2.0,<3（实测 2.5.4）", "BSD-3-Clause", "05/06/09 栅格与分位统计", "必需"),
    ("scipy", ">=1.14,<2（实测 1.18.1）", "BSD-3-Clause", "09 形态学/连通域/sliding_window_view", "必需（09）"),
    ("scikit-image", ">=0.24,<0.27（实测 0.26.0）", "BSD-3-Clause", "09 分水岭/局部极大值；05 斑点清洗", "必需（09）"),
    ("Pillow", ">=10,<12（实测 11.3.0）", "HPND", "05 纹理解码与颜色统计", "必需（05 纹理诊断）"),
    ("trimesh", ">=4,<6（实测 5.2.0）", "MIT", "05 面法线/几何质量", "可选（缺失走降级）"),
    ("openpyxl", ">=3.1,<4（实测 3.1.5）", "MIT", "07 xlsx 出件；本脚本导出 xlsx", "必需（07）"),
    ("python-docx", ">=1.1,<2（实测 1.2.0）", "MIT", "07 docx 出件", "必需（07）"),
    ("opencv-python", ">=4.10,<6（实测 5.0.0.93）", "Apache-2.0", "登记为纹理解码备选；当前代码未 import，Pillow 已在用", "未使用（备选）"),
    ("PDAL", "未安装", "BSD-3-Clause", "点云地面滤波/分类（pc.ground-filter / pc.classify）", "缺失（相关算法 planned）"),
    ("GDAL / OGR", "未安装", "MIT", "GPKG/SHP 入库、栅格→矢量（vector.gpkg / vector.polygonize）", "缺失（相关算法 planned）"),
]

STATUS_CN = {
    "ready": "现在可跑",
    "needs-imagery": "缺航摄原片",
    "needs-2epoch": "缺第二期数据",
    "needs-truth": "缺真值",
    "planned": "缺依赖/待实现",
}


def load_registry(workspace: str) -> dict:
    """读算法注册表。

    优先**技能包内**的那份（交付包模式下技能包在工作区之外，工作区里没有 Skills/）；
    其次才找工作区内的副本（技能包内嵌的旧布局）。
    """
    candidates = [REG_IN_PACKAGE, os.path.join(workspace, REG_REL)]
    path = next((p for p in candidates if os.path.isfile(p)), None)
    if path is None:
        raise SystemExit(f"[00] 找不到算法注册表（试过："
                         f"{'、'.join(p.replace(chr(92), '/') for p in candidates)}）")
    with open(path, encoding="utf-8-sig") as fh:
        return json.load(fh)


def build_workbook(reg: dict, out_path: str) -> dict:
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
        from openpyxl.utils import get_column_letter
    except ImportError:
        raise SystemExit("[00] 缺少 openpyxl：python -m pip install -r requirements.txt "
                         "-i https://mirrors.aliyun.com/pypi/simple/")

    groups = {g["id"]: g["name"] for g in reg.get("groups") or []}
    head_fill = PatternFill("solid", fgColor="1F4E79")
    head_font = Font(color="FFFFFF", bold=True, size=10)
    body_font = Font(size=9)
    thin = Side(style="thin", color="BFBFBF")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    wrap = Alignment(wrap_text=True, vertical="top")

    wb = Workbook()
    wb.remove(wb.active)

    def add_sheet(title, header, rows, widths):
        ws = wb.create_sheet(title)
        ws.append(header)
        for cell in ws[1]:
            cell.fill, cell.font, cell.alignment, cell.border = head_fill, head_font, wrap, border
        for row in rows:
            ws.append(list(row))
        for r in ws.iter_rows(min_row=2, max_row=ws.max_row):
            for cell in r:
                cell.font, cell.alignment, cell.border = body_font, wrap, border
        for i, w in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = f"A1:{get_column_letter(len(header))}{ws.max_row}"
        return ws

    # 1) 表内 21 项
    rows1 = []
    for i, (cat, name, link, fit, used, why, alt) in enumerate(TABLE_INTERNAL, start=1):
        rows1.append((i, cat, name, link, fit, used, why, alt))
    add_sheet("表内21项对照",
              ("序号", "要素类别", "项目 / 算法", "开源链接", "原表适配度", "本链是否采用", "不采用原因", "本链现有替代（可跑）"),
              rows1, (6, 20, 30, 46, 18, 12, 44, 52))

    ready = [a for a in reg["algos"] if a.get("status") == "ready"]
    pending = [a for a in reg["algos"] if a.get("status") != "ready"]

    # 2) 已落地
    rows2 = []
    for i, a in enumerate(ready, start=1):
        rows2.append((i, groups.get(a["group"], a["group"]), a["name"], a.get("plain", ""),
                      a.get("input", ""), a.get("output", ""),
                      a.get("sourceName") or a.get("source", ""), a.get("sourceLink") or "",
                      a.get("license") or "", a.get("impl") or "",
                      f"{a.get('step') or '-'} · {a.get('evidence') or a.get('source', '')}",
                      a.get("priority", "")))
    add_sheet("本链已落地",
              ("序号", "分组", "算法 / 能力", "做什么（本链口径）", "输入", "输出", "来源",
               "链接", "许可证", "实现方式", "落地位置（步骤 · 文件）", "优先级"),
              rows2, (6, 14, 26, 60, 30, 40, 42, 42, 18, 30, 52, 8))

    # 3) 待条件
    rows3 = []
    for i, a in enumerate(pending, start=1):
        rows3.append((i, groups.get(a["group"], a["group"]), a["name"], STATUS_CN.get(a.get("status"), a.get("status")),
                      a.get("reason", ""), a.get("plain", ""), a.get("sourceName") or a.get("source", ""),
                      a.get("sourceLink") or "", a.get("license") or "", a.get("priority", "")))
    add_sheet("待条件",
              ("序号", "分组", "算法 / 能力", "状态", "为什么现在跑不了（缺什么）", "补上就能干什么",
               "来源", "链接", "许可证", "优先级"),
              rows3, (6, 16, 30, 16, 52, 52, 40, 42, 20, 8))

    # 4) 依赖
    add_sheet("依赖与版本",
              ("依赖", "版本", "许可证", "本链用途", "状态"),
              DEPS, (34, 26, 18, 56, 20))

    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    if os.path.exists(out_path):
        os.remove(out_path)   # 先删旧再生成
    wb.save(out_path)
    return {"ready": len(ready), "pending": len(pending), "internal": len(TABLE_INTERNAL),
            "deps": len(DEPS), "sheets": [ws.title for ws in wb.worksheets]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="00 · 算法清单与来源导出（xlsx）")
    ap.add_argument("--workspace", default=".")
    ap.add_argument("--out", default=None, help=f"输出路径，默认 <工作区>/{OUT_NAME}")
    ap.add_argument("--package", action="store_true",
                    help="导出到技能包内的 assets/算法表/（交付包自带的那份）")
    args = ap.parse_args(argv)

    ws_root = os.path.abspath(args.workspace)
    # 工作区判据：交付包模式下技能包在工作区**之外**，工作区里没有 Skills/。
    # 只要工作区本身存在即可；注册表从技能包里读。
    if not os.path.isdir(ws_root):
        print(f"[00] 工作区不存在：{args.workspace}", file=sys.stderr)
        return 2
    reg = load_registry(ws_root)
    if args.package:
        out = os.path.join(SKILLS_ROOT, "..", "assets", "算法表", OUT_NAME)
    else:
        out = args.out or os.path.join(ws_root, OUT_NAME)
    info = build_workbook(reg, out)
    print(f"[00] 已生成 {os.path.basename(out)}：表内 {info['internal']} 项 / "
          f"已落地 {info['ready']} 项 / 待条件 {info['pending']} 项 / 依赖 {info['deps']} 条"
          f"（{datetime.datetime.now().astimezone().strftime('%Y-%m-%d %H:%M')}）")
    print(f"[00] 工作表：{'、'.join(info['sheets'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
