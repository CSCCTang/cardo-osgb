#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""14 · 扩展指标汇总：把 09–13 已落地算法的产出汇成**可交付的指标**。

背景：注册表登记「已落地 34 条算法」，但《现状指标》只有 06 步的 7 条（其中 2 条
还是 null 占位）——**落了地的算法没有变成指标**，交付物看不出用了什么、算出了什么。

本步只做一件事：读 09–13 各步过程 JSON 的 summary，映射成一张**扩展指标表**。
- 不新算任何几何（不引入第二套实现），只做「已算出的量 → 指标」的搬运与口径标注；
- 每条扩展指标都带 `algoId`（对应注册表里的哪条算法）、`step`（产自哪一步）、
  `evidence`（哪个文件哪个字段），可回溯；
- 某个上游没跑或没有该字段 → 该指标 value=null + gaps 条目，**不编数、不猜**。

输出：过程文件/14-扩展指标/扩展指标.json（schema cardo/lowair-extended-indicators/v1）
      过程文件/14-扩展指标/扩展指标.md（人读版）
退出码：0 至少一条扩展指标；2 上游产物全缺；3 闸门未过（指标结构不对）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

sys.dont_write_bytecode = True

SCHEMA = "cardo/lowair-extended-indicators/v1"
OUT_DIR = os.path.join("过程文件", "14-扩展指标")
JSON_NAME = "扩展指标.json"
MD_NAME = "扩展指标.md"

# METRIC_KEYS 与 06 步一致（同样的 10 个必填键），另加 3 个溯源键。
METRIC_KEYS = ("id", "name", "value", "range", "unit", "method", "confidence",
               "limits", "source", "grade")
EXTRA_KEYS = ("algoId", "step", "evidence")

# 各步产物：相对工作区根的路径
SRC = {
    "09": os.path.join("过程文件", "09-三维要素反演", "三维要素反演.json"),
    "10": os.path.join("过程文件", "10-矢量入库", "拓扑清理.json"),
    "11c": os.path.join("过程文件", "11-几何增强", "pc-classify", "pc-classify.json"),
    "11t": os.path.join("过程文件", "11-几何增强", "tree-single", "tree-single.json"),
    "11g": os.path.join("过程文件", "11-几何增强", "pc-ground-filter", "pc-ground-filter.json"),
    "11m": os.path.join("过程文件", "11-几何增强", "mesh-repair", "mesh-repair.json"),
    "12r": os.path.join("过程文件", "12-屋顶与立面", "roof-segment", "roof-segment.json"),
    "12f": os.path.join("过程文件", "12-屋顶与立面", "facade-normal", "facade-normal.json"),
    "13p": os.path.join("过程文件", "13-矢量增强", "vector-polygonize", "vector-polygonize.json"),
    "13g": os.path.join("过程文件", "13-矢量增强", "vector-gpkg", "vector-gpkg.json"),
    "15s": os.path.join("过程文件", "16-场地要素", "场地要素.json"),
}

# 指标定义表：(id, 名称, 上游键, 上游字段, 单位, 算法id, 步骤, 置信度, 口径, 局限)
# value 与 range 都从上游 summary 里**直接取**，不在这里做二次计算。
DEFS = [
    # ---- 09 三维要素反演 ----
    ("building_count_recon", "建筑栋数（几何实例化）", "09", "buildingCount", "栋",
     "recon.instance", "09", "medium",
     "对 nDSM>3 m 的建筑候选格做闭运算+开运算，再用 nDSM 平滑面局部极大值做种子、"
     "分水岭把连在一起的裙房/连廊切开，逐个连通块算一栋（09 的实例化结果，非 06 的网格计数）",
     "分水岭种子间距 6 m：太小会把一栋切两半，太大又合回去；栋数对格距与阈值敏感，"
     "树冠与构筑物也会被选中；无实测真值，是候选栋数而非报建口径"),
    ("building_area_recon", "建筑基底面积（实例化）", "09", "buildingAreaM2", "m²",
     "recon.instance", "09", "medium",
     "各栋建筑实例的格数 × 格面积（1 m²）求和（09 summary 的 buildingAreaM2）",
     "格化边界误差约 ±1 格/周长；面积是栅格格数和，不是实测轮廓面积"),
    ("building_height_p50_recon", "建筑高度 P50（逐栋中位）", "09", "heightP50", "m",
     "recon.object-height", "09", "medium",
     "先算每栋内部 nDSM 的 P50，再对各栋 P50 取中位（栋间中位，区别于 06 的格级 P50）",
     "含女儿墙、屋顶设备、树冠等非结构高点；屋顶设备多的楼会被拉高"),
    ("building_height_p90_recon", "建筑高度 P90（逐栋）", "09", "heightP90", "m",
     "recon.object-height", "09", "medium",
     "各栋 nDSM 的 P90，再对各栋取 P90（栋间 P90）",
     "P90 对小样本与异常高点敏感；与 06 的格级 P90 口径不同，两者不可直接比大小"),
    ("floor_area_est_recon", "建筑面积（估算）", "09", "floorAreaEstM2", "m²",
     "recon.object-height", "09", "low",
     "各栋按格高 ÷ 3.0 m 层高向上取整得层数，逐格累计建筑面积（09 summary 的 floorAreaEstM2）",
     "层高 3.0 m 是未标定假设；ceil 取整系统性偏高；不含地下层与退台；非报建口径"),
    ("building_grade_a", "置信度 A 级建筑数", "09", "gradeA", "栋",
     "recon.confidence", "09", "medium",
     "按面积/贴合率/高度离散/边界/空洞五项打 A/B/C，A 可直接进台账（09 summary 的 gradeA）",
     "分级规则为本链自定，非国家或行业标准；A 级也只代表几何自洽，不代表功能正确"),
    ("building_grade_b", "置信度 B 级建筑数", "09", "gradeB", "栋",
     "recon.confidence", "09", "medium",
     "B 级：几何基本可信，但需人工看（09 summary 的 gradeB）",
     "同上"),
    ("building_grade_c", "置信度 C 级建筑数", "09", "gradeC", "栋",
     "recon.confidence", "09", "medium",
     "C 级：只能当线索（09 summary 的 gradeC）",
     "同上"),

    # ---- 10 矢量入库 ----
    ("vector_kept", "入库矢量要素数", "10", "keptCount", "个",
     "vector.topology", "10", "high",
     "09 输出的逐栋要素经 R1–R8 八条拓扑规则清理后保留的个数（10 summary 的 keptCount）",
     "只是几何清理后的计数；要素本身的属性质量取决于 09 的反演"),
    ("vector_dropped", "拓扑清理剔除数", "10", "droppedCount", "个",
     "vector.topology", "10", "high",
     "被清理规则剔除的要素数（面积过小/环不闭合/完全重叠等，10 summary 的 droppedCount）",
     "剔除意味着这些栋不再进入矢量成果，但不代表它们不存在"),
    ("vector_warning", "拓扑清理告警数", "10", "warningCount", "个",
     "vector.topology", "10", "high",
     "清理后仍留有疑问（如精度边界）的要素数（10 summary 的 warningCount）",
     "告警项需人工复核后才能进正式台账"),

    # ---- 11 几何增强：点云分类 ----
    ("ground_share", "地面格占比", "11c", "groundShare", "1",
     "pc.classify", "11", "medium",
     "按 nDSM 高度 + 3×3 dsm 粗糙度把栅格分成地面/建筑/植被/其它；此处为地面类格数占比"
     "（11 summary 的 groundShare）",
     "纯几何启发式，无点云语义分类（PDAL）与影像分割；坡地、台阶、临建设施会误分；"
     "分类阈值未经实测标定"),
    ("building_share", "建筑格占比", "11c", "buildingShare", "1",
     "pc.classify", "11", "medium",
     "分类为建筑类的格数占比（11 summary 的 buildingShare）",
     "同上；与 06 的建筑密度口径不同（这里是分类结果，那里是 nDSM 阈值）"),
    ("vegetation_share", "植被格占比", "11c", "vegetationShare", "1",
     "pc.classify", "11", "medium",
     "分类为植被类的格数占比（11 summary 的 vegetationShare）——**这是当前唯一能出数的"
     "「绿」相关量**，它是几何植被（树冠）占比，不等于用地口径的绿化率",
     "只覆盖几何可见的树冠，低矮灌木/草坪/垂直绿化不在其中；与规划口径的绿化率不可互相替代"),
    ("other_share", "其它类格占比", "11c", "otherShare", "1",
     "pc.classify", "11", "medium",
     "既非地面/建筑/植被的格数占比（11 summary 的 otherShare）",
     "「其它」是残余类，含道路、铺装、水面、设备等，无法进一步细分"),
    ("classify_precision_vs_recon", "分类对 09 掩膜的精确率", "11c", "buildingPrecisionVs09", "1",
     "pc.classify", "11", "medium",
     "分类为建筑的格中，与 09 建筑掩膜重叠的比例（11 summary 的 buildingPrecisionVs09）",
     "参照系是 09 自己的掩膜，不是真值；高精确率只说明两套几何口径一致"),
    ("classify_recall_vs_recon", "分类对 09 掩膜的召回率", "11c", "buildingRecallVs09", "1",
     "pc.classify", "11", "medium",
     "09 建筑掩膜中被分类为建筑的格所占比例（11 summary 的 buildingRecallVs09）",
     "召回偏低说明几何分类漏掉了大量 09 候选格，多因树冠/附属结构高度介于阈值之间"),

    # ---- 11 几何增强：单木 ----
    ("tree_count", "单木数量", "11t", "treeCount", "棵",
     "tree.single", "11", "medium",
     "在 CHM（= nDSM 去掉建筑掩膜）上取局部极大值作树顶种子，分水岭切冠，每块算一棵"
     "（11 summary 的 treeCount）",
     "只识别几何上能独立成冠的乔木；行列树、密林会漏计或并成一棵；不区分树种与健康状况"),
    ("tree_crown_area", "树冠覆盖面积", "11t", "crownAreaM2", "m²",
     "tree.single", "11", "medium",
     "各单木冠幅格数之和（11 summary 的 crownAreaM2）",
     "冠幅为 1 m 栅格格数，不扣除冠内空隙；与实测郁闭度不等价"),
    ("tree_mean_crown_diameter", "平均冠幅直径", "11t", "meanCrownDiameterM", "m",
     "tree.single", "11", "medium",
     "各单木等效冠幅直径的均值（11 summary 的 meanCrownDiameterM）",
     "格化离散误差 ±1.4 m；冠幅形状差异未反映"),
    ("tree_max_height", "最高树高度", "11t", "maxHeightM", "m",
     "tree.single", "11", "medium",
     "树域内 nDSM 的最大值（11 summary 的 maxHeightM）",
     "是树域内的最高点，可能包含灌木或附属物，不必然是树高"),

    # ---- 11 几何增强：地面滤波 ----
    ("ground_filter_agree1m", "PMF 地面与 P10 地面一致率", "11g", "agreeWithin1m", "1",
     "pc.ground-filter", "11", "medium",
     "PMF 渐进形态学滤波地面与 09 局部 P10 地面差值在 ±1 m 内的格点占比"
     "（11 summary 的 agreeWithin1m）",
     "两个口径都是近似（形态学滤波 vs 邻域分位），差值反映口径差异，**不是精度误差**"),
    ("ground_filter_meandiff", "两种地面口径平均差", "11g", "meanAbsDiffM", "m",
     "pc.ground-filter", "11", "medium",
     "PMF 地面与 P10 地面的平均绝对差（11 summary 的 meanAbsDiffM）",
     "同上；无实测 DTM/检查点校核，不构成精度声明"),
    ("ground_filter_p95diff", "两种地面口径 P95 差", "11g", "p95AbsDiffM", "m",
     "pc.ground-filter", "11", "medium",
     "PMF 地面与 P10 地面的 P95 绝对差（11 summary 的 p95AbsDiffM）",
     "同上；P95 反映最不一致的区域（多为建筑密集区）"),

    # ---- 11 几何增强：网格修复 ----
    ("mesh_hole_count", "模型孔洞数", "11m", "holeCount", "个",
     "mesh.repair", "11", "high",
     "逐瓦片统计网格边界洞的数量（11 summary 的 holeCount）",
     "是**诊断**指标：孔洞是建模产物不闭合，不等于实地缺测"),
    ("mesh_hole_filled", "已补孔洞数", "11m", "filledHoleCount", "个",
     "mesh.repair", "11", "high",
     "在内存副本上执行 fill_holes 补上的孔洞数（11 summary 的 filledHoleCount）",
     "补洞是几何缝合，可能填出不存在的面；副本操作，**原模型包未改动**"),
    ("mesh_nonmanifold_edges", "非流形边数", "11m", "nonManifoldEdgeCount", "条",
     "mesh.repair", "11", "high",
     "被 3 个以上三角面共用的边（11 summary 的 nonManifoldEdgeCount）",
     "非流形边影响后续布尔运算与法向连续性，量级大说明模型拓扑质量一般"),
    ("mesh_watertight_after", "补洞后水密瓦片数", "11m", "watertightTilesAfter", "个",
     "mesh.repair", "11", "high",
     "补洞后无边界边的瓦片数（11 summary 的 watertightTilesAfter），对照补洞前 "
     "watertightTilesBefore",
     "水密只说明边界闭合，不代表几何正确或可量测"),

    # ---- 12 屋顶与立面 ----
    ("roof_area", "屋顶面总面积", "12r", "roofAreaM2", "m²",
     "roof.segment", "12", "medium",
     "按顶点法向半球量化（方位角 15°/倾角 5°）+ 平面偏移聚类切出屋面面片，求面积合计"
     "（12 summary 的 roofAreaM2）",
     "未用 RANSAC，对窄条面与曲面屋顶鲁棒性弱；斜屋面面积偏大；与占地面积不是一回事"),
    ("roof_plane_count", "屋面面片数", "12r", "roofPlaneCount", "个",
     "roof.segment", "12", "medium",
     "切出的屋面平面片数量（12 summary 的 roofPlaneCount）",
     "面片数随法向分箱与聚类阈值变化，不是物理屋面数"),
    ("roof_mean_tilt", "屋面平均倾角", "12r", "meanTiltDeg", "°",
     "roof.segment", "12", "medium",
     "各屋面片法向倾角的面积加权平均（12 summary 的 meanTiltDeg）",
     "无实测坡度校核；小面积片权重影响大"),
    ("roof_flat_share", "平屋面占比", "12r", "flatPlaneShare", "1",
     "roof.segment", "12", "medium",
     "倾角 ≤5° 的屋面面积占屋面总面积的比例（12 summary 的 flatPlaneShare）",
     "阈值是本链自定；缓坡与噪声法向会误分"),
    ("facade_area", "立面总面积", "12f", "facadeAreaM2", "m²",
     "facade.normal", "12", "medium",
     "法向近水平（NORMAL.z ≤ 0.4）的三角面按方位角 15° 分箱 + 3 m 高度层统计面积"
     "（12 summary 的 facadeAreaM2）",
     "立面与坡屋面、附属物的划分依赖法向阈值；遮挡区未建模，立面为可见面"),
    ("facade_edge_length", "墙脚接触边长", "12f", "footprintEdgeM", "m",
     "facade.normal", "12", "medium",
     "立面底部与地面的接触边总长（12 summary 的 footprintEdgeM）",
     "是可见立面轮廓的近似周长，与实测建筑周长不等价"),
    ("facade_mean_height", "立面平均高度", "12f", "meanFacadeHeightM", "m",
     "facade.normal", "12", "medium",
     "各立面片高度层的加权平均（12 summary 的 meanFacadeHeightM）",
     "同立面面积的局限"),

    # ---- 13 矢量增强 ----
    ("polygon_count", "真实轮廓多边形数", "13p", "polygonCount", "个",
     "vector.polygonize", "13", "medium",
     "从 09 建筑栅格用 marching squares 提取真实边界轮廓并简化（13 summary 的 polygonCount）",
     "只取到 L19 层；相邻建筑若在栅格上连通仍会并成一个多边形"),
    ("polygon_area", "真实轮廓总面积", "13p", "polygonAreaM2", "m²",
     "vector.polygonize", "13", "medium",
     "矢量化轮廓的面积合计（13 summary 的 polygonAreaM2）",
     "轮廓化后与 OBB 面积不同（见 areaRatioVsObb），不可混用"),
    ("polygon_area_ratio_vs_obb", "轮廓面积 / 外接矩形面积", "13p", "areaRatioVsObb", "1",
     "vector.polygonize", "13", "high",
     "真实轮廓面积与 09 的最小外接矩形面积之比（13 summary 的 areaRatioVsObb）——"
     "**比值明显小于 1 说明这些楼是 L 形/异形，不能按矩形入库**",
     "比值受栅格分辨率影响；分母本身是近似外接矩形"),
    ("gpkg_feature_count", "GeoPackage 要素数", "13g", "featureCount", "个",
     "vector.gpkg", "13", "high",
     "写入 GeoPackage 的要素数（13 summary 的 featureCount）",
     "数量对得上不代表几何对；本机无 GDAL，未做第三方读回验证"),
    ("gpkg_area_mismatch", "GPKG 面积最大偏差", "13g", "areaMismatchMax", "m²",
     "vector.gpkg", "13", "high",
     "写入前后要素面积的最大偏差（13 summary 的 areaMismatchMax）——"
     "**自检值，为 0 表示序列化未丢精度**",
     "是自洽性检查，不是对外部基准的精度验证"),

    # ---- 16 场地要素（几何代理量）----
    ("hard_area", "硬地面积（几何代理）", "15s", "hardAreaM2", "m²",
     "site.hardscape", "15", "low",
     "坡度<3° 且非建筑非植被的连通区面积合计（16 summary 的 hardAreaM2）——"
     "**几何代理量，不是路面语义**：车行道/人行道/广场/铺装在几何上一样",
     "分不出路面材质；与「空地」「低矮体量」是同一批格子的不同切法，**不可相加**"),
    ("hard_share", "硬地占比（几何代理）", "15s", "hardShare", "1",
     "site.hardscape", "15", "low",
     "硬地面积 ÷ 观测面积（16 summary 的 hardShare）",
     "同上；且空洞格不进分母，覆盖率随缺失位置变化"),
    ("street_count", "街巷条数（几何代理）", "15s", "streetCount", "条",
     "site.street-network", "15", "low",
     "硬地连通块中宽度 2–15 m 且长度 ≥8 m 的窄长通道计数（16 summary 的 streetCount）",
     "按几何连通性判定；断头路、小区内部道路受遮挡影响；宽度是外接矩形短边近似，"
     "弯道会高估、喇叭口会低估"),
    ("street_width_median", "街巷路幅中位数（几何代理）", "15s", "streetWidthMedianM", "m",
     "site.street-network", "15", "low",
     "上述街巷的外接矩形短边宽度中位数（16 summary 的 streetWidthMedianM）",
     "是包围盒宽度不是真实路幅；沿街建筑退让造成的喇叭口会高估"),
    ("low_object_count", "低矮体量处数（违建候选）", "15s", "lowObjectCount", "处",
     "site.annex-object", "15", "low",
     "非建筑区中 nDSM 0.3–2.0 m 且面积 ≥4 m² 的连通块计数（16 summary 的 lowObjectCount）",
     "**可能是违建棚屋，也可能是花坛、台阶、车辆、堆料**——是候选不是判定，须人工看"),
    ("low_object_area", "低矮体量总面积（违建候选）", "15s", "lowObjectAreaM2", "m²",
     "site.annex-object", "15", "low",
     "上述低矮体量的面积合计（16 summary 的 lowObjectAreaM2）",
     "同上；含大量台阶与矮墙时会把面积放大"),
    ("void_patch_count", "空地斑块数（闲置候选）", "15s", "voidPatchCount", "块",
     "site.open-space", "15", "low",
     "非建筑非植被、坡度<3°、面积 ≥200 m² 的连续区计数（16 summary 的 voidPatchCount）",
     "**空地不等于闲置**：可能是待建预留地、拆迁场地、施工临时堆场"),
    ("void_area", "空地总面积（闲置候选）", "15s", "voidAreaM2", "m²",
     "site.open-space", "15", "low",
     "上述空地斑块的面积合计（16 summary 的 voidAreaM2）",
     "同上；与硬地高度重叠，二者不可相加"),
    ("depression_count", "低洼/积水候选处数", "15s", "depressionCount", "处",
     "site.depression", "15", "low",
     "低于 31 m 邻域基准面 0.3–3.0 m、坡度<2°、离建筑≥2 m 的连通区计数"
     "（16 summary 的 depressionCount）",
     "几何代理量；已排除深于 3 m（地面拟合误差）与贴墙 2 m 内（nDSM 边界效应），"
     "但**洼地与积水仍需现场核对**"),
    ("damage_clue_count", "破损线索条数（几何异常）", "15s", "damageClueCount", "条",
     "site.damage-clue", "15", "low",
     "屋面倾角离散 ≥15° + 栋高偏离片区中位 ≥50% 的栋数合计（16 summary 的 damageClueCount）",
     "**是几何异常线索，不是危房结论**：倾斜模型看不到结构内部，"
     "体量离群也可能是正常的复式/加建；须人工核"),
    ("ground_object_count", "贴地小体量处数（车辆/井盖代理）", "15s", "groundObjectCount", "处",
     "site.ground-object", "15", "low",
     "非建筑区 nDSM 0.10–1.20 m 且面积 ≤1 格（1 m²）的连通块计数"
     "（16 summary 的 groundObjectCount）——车辆、井盖、灯杆、隔离墩的密度量级",
     "1 m 栅格下这类物体只剩一个采样点，**只给密度不给类别**；"
     "识别「是什么」要影像；数量受采样率影响，不是真物体数"),
    ("ground_object_density", "贴地小体量密度", "15s", "groundObjectDensityPer100M2", "个/100m²",
     "site.ground-object", "15", "low",
     "贴地小体量处数 ÷（观测面积/100 m²）（16 summary 的 groundObjectDensityPer100M2）",
     "同上；是**密度代理**不是车辆数"),
]

# 分组（用于表头与 md 分节）
GROUPS = [
    ("09", "建筑反演", "recon.instance / recon.object-height / recon.confidence"),
    ("10", "矢量入库", "vector.topology"),
    ("11c", "点云分类", "pc.classify"),
    ("11t", "单木分割", "tree.single"),
    ("11g", "地面滤波", "pc.ground-filter"),
    ("11m", "网格修复", "mesh.repair"),
    ("12r", "屋顶面", "roof.segment"),
    ("12f", "立面", "facade.normal"),
    ("13p", "轮廓矢量化", "vector.polygonize"),
    ("13g", "GeoPackage", "vector.gpkg"),
    ("15s", "场地要素（几何代理）", "site.hardscape / site.street-network / site.annex-object / "
                                   "site.open-space / site.depression / site.damage-clue"),
]


def read_json(path):
    with open(path, encoding="utf-8-sig") as fh:
        return json.load(fh)


def pick(doc: dict, field: str):
    """从上游 JSON 取一个字段。

    各步的落位不完全一致：多数在 `summary`，10 步在 `cleaning`，09 的分级在 `result`。
    这里按 `summary → cleaning → result` 的顺序找，**找到第一个就停**，
    避免同名字段在不同段里含义不同（比如 10 的 featureCount 与 09 的 buildingCount 同为 13 但不是一回事）。
    """
    if not isinstance(doc, dict):
        return None
    for section in ("summary", "cleaning", "result"):
        block = doc.get(section)
        if isinstance(block, dict) and field in block:
            return block[field]
    return None


def write_json(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def build(workspace: str, out_dir: str) -> dict:
    docs, missing = {}, []
    for key, rel in SRC.items():
        path = os.path.join(workspace, rel)
        if os.path.isfile(path):
            try:
                docs[key] = read_json(path)
            except (OSError, ValueError) as exc:
                missing.append({"key": key, "file": rel, "why": f"读取失败：{exc}"})
        else:
            missing.append({"key": key, "file": rel, "why": "文件不存在（该步未跑或未产出）"})

    metrics, gaps = [], []
    for mid, name, key, field, unit, algo, step, conf, method, limits in DEFS:
        doc = docs.get(key)
        value = pick(doc, field)
        item = {
            "id": mid, "name": name, "value": value, "range": None, "unit": unit,
            "method": method, "confidence": conf, "limits": limits,
            "source": [algo], "grade": "estimated" if value is not None else "placeholder",
            "algoId": algo, "step": step,
            "evidence": f"{SRC[key].replace(chr(92), '/')} · {field}",
        }
        metrics.append(item)
        if value is None:
            gaps.append({"code": "EXT-GAP-001",
                         "what": f"扩展指标 {mid}（{name}）无值",
                         "why": f"上游 {key} 里没有字段 {field}"
                                + ("（该步未跑）" if key not in docs else ""),
                         "need": f"先跑 {step} 步再汇总", "owner": "pipeline"})

    # 区间：只有上游自己给了区间才用（09 的 buildingCountRange 等），否则不给区间
    rng_map = {
        "building_count_recon": ("09", "buildingCountRange"),
        "building_area_recon": ("09", "buildingAreaRange"),
        "building_height_p50_recon": ("09", "heightP50Range"),
        "building_height_p90_recon": ("09", "heightP90Range"),
    }
    for mid, (key, field) in rng_map.items():
        src_range = pick(docs.get(key), field)
        if isinstance(src_range, (list, tuple)) and len(src_range) == 2:
            for item in metrics:
                if item["id"] == mid:
                    item["range"] = [src_range[0], src_range[1]]

    # 上游整体没跑 → 该步的指标全空，写一条汇总 gap
    for key, group_name, algo in GROUPS:
        if key not in docs:
            gaps.append({"code": "EXT-GAP-002",
                         "what": f"{group_name}（{algo}）未产出，相应扩展指标为 null",
                         "why": f"{SRC[key].replace(chr(92), '/')} 不存在",
                         "need": f"先跑 {key[:2]} 步（几何增强/矢量增强等）", "owner": "pipeline"})

    doc = {
        "schema": SCHEMA, "schemaVersion": 1,
        "generatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "producer": {"name": "cardo 低空解析 · 14 扩展指标汇总", "version": "1.0.0"},
        "method": "只搬运 09–13 各步 summary 里已算出的量，不做二次几何计算；"
                  "每条指标带 algoId / step / evidence 三重溯源",
        "sources": [{"key": k, "file": SRC[k].replace(chr(92), "/"),
                     "present": k in docs} for k in SRC],
        "metrics": metrics,
        "gaps": gaps,
        "summary": {
            "total": len(metrics),
            "withValue": sum(1 for m in metrics if m["value"] is not None),
            "placeholder": sum(1 for m in metrics if m["value"] is None),
            "upstreamPresent": len(docs),
            "upstreamTotal": len(SRC),
            "algosCovered": len({m["algoId"] for m in metrics if m["value"] is not None}),
        },
        "quality": {
            "usable": any(m["value"] is not None for m in metrics),
            "reason": "扩展指标为上游已算结果的搬运与口径标注，未引入新计算，"
                      "与各步 summary 一一对应；真值校核仍缺（同 06 的口径）",
        },
    }
    return doc


def render_md(doc: dict) -> str:
    lines = ["# 扩展指标（09–13 已落地算法的产出）", "",
             f"生成时间：{doc['generatedAt']}　契约：{doc['schema']}", "",
             f"本表把 **09–13 步已落地算法的产出**汇成指标。每条都标了 "
             "`algoId`（对应算法注册表里的哪条算法）、`step`（产自哪一步）、"
             "`evidence`（哪个文件的哪个字段），可逐条回溯。", "",
             f"共 {doc['summary']['total']} 条：有值 {doc['summary']['withValue']} 条，"
             f"占位 {doc['summary']['placeholder']} 条；覆盖 "
             f"{doc['summary']['algosCovered']} 条算法；"
             f"上游产物 {doc['summary']['upstreamPresent']}/{doc['summary']['upstreamTotal']} 在位。", ""]
    by_step = {}
    for item in doc["metrics"]:
        by_step.setdefault(item["step"], []).append(item)
    for step in sorted(by_step):
        items = by_step[step]
        lines += [f"## {step} 步产出", "",
                  "| 指标 | 值 | 单位 | 置信度 | 算法 id | 口径 |",
                  "|---|---|---|---|---|---|"]
        for item in items:
            val = "—" if item["value"] is None else item["value"]
            lines.append(f"| {item['name']} | {val} | {item['unit']} | "
                         f"{item['confidence']} | `{item['algoId']}` | {item['method']} |")
        lines.append("")
    if doc["gaps"]:
        lines += ["## 缺项", "", "| code | 缺什么 | 为什么 | 需要什么 | owner |",
                  "|---|---|---|---|---|"]
        for gap in doc["gaps"]:
            lines.append(f"| {gap['code']} | {gap['what']} | {gap['why']} | "
                         f"{gap['need']} | {gap['owner']} |")
        lines.append("")
    lines += ["## 局限（必须随表一起读）", "",
              "1. **扩展指标不等于新精度**：它们是上游算法已算出的量的搬运，"
              "各步自身的局限（无真值、LOD 混合、地面近似等）同样适用。",
              "2. **口径不同不可混比**：本表的栋数（实例化）与 06 步的栋数（网格连通块）"
              "是两个算法、两个口径，数值不同是正常的，不是矛盾。",
              "3. **植被占比 ≠ 绿化率**：几何植被只覆盖可见树冠，"
              "规划口径的绿化率仍需影像语义分割或用地数据。",
              "4. **区间只有部分指标有**：只有上游自己做过敏感性分析的指标才带区间，"
              "其余给单点值 + 置信度，不硬凑区间。", ""]
    return "\n".join(lines)


def gate(doc: dict) -> list:
    problems = []
    for item in doc.get("metrics") or []:
        missing = [k for k in METRIC_KEYS + EXTRA_KEYS if k not in item]
        if missing:
            problems.append(f"{item.get('id')} 缺键 {missing}")
        if item.get("value") is None and item.get("grade") != "placeholder":
            problems.append(f"{item.get('id')} 值为 null 但 grade≠placeholder")
        if item.get("value") is not None and item.get("grade") == "placeholder":
            problems.append(f"{item.get('id')} 有值但 grade=placeholder")
    if not doc.get("quality", {}).get("usable"):
        problems.append("quality.usable=false（上游全缺）")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description="14 · 扩展指标汇总（09–13 已落地算法的产出）")
    ap.add_argument("--workspace", default=".")
    ap.add_argument("--out", default=OUT_DIR)
    args = ap.parse_args()

    root = os.path.abspath(args.workspace)
    out_dir = os.path.abspath(args.out) if os.path.isabs(args.out) else os.path.join(root, args.out)
    os.makedirs(out_dir, exist_ok=True)
    json_path = os.path.join(out_dir, JSON_NAME)
    md_path = os.path.join(out_dir, MD_NAME)
    for stale in (json_path, md_path):
        if os.path.isfile(stale):
            os.remove(stale)

    doc = build(root, out_dir)
    write_json(json_path, doc)
    with open(md_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(render_md(doc))

    problems = gate(doc)
    if problems:
        print("[14] 闸门未通过：" + "；".join(problems[:8]), file=sys.stderr)
        return 3
    s = doc["summary"]
    print(f"[14] 完成：扩展指标 {s['total']} 条（有价值 {s['withValue']} / 占位 "
          f"{s['placeholder']}），覆盖 {s['algosCovered']} 条算法；上游产物 "
          f"{s['upstreamPresent']}/{s['upstreamTotal']} 在位")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
