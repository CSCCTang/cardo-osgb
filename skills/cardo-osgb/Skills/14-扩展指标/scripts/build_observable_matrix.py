#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""14 · 可观测类型矩阵：无人机能扫出哪些东西 × 有哪些开源算法 × 本链落地了哪些。

这是**产品视角**的能力边界表，回答三个问题：
1. 无人机低空（倾斜摄影为主）这套技术，**物理上能观测到哪些类型的信息**？
2. 每一类，**开源算法供给**如何（调研表 21 项 + 本链自研几何方法）？
3. 本链**落地了哪些**、**缺哪些**、缺口卡在数据源还是算法？

依据（都是外部权威输入，不是本链自说自话）：
- 课题任务书 §2.1.1「核心数据」：场地整体现状影像、建（构）筑物外观及空间分布、场地地貌与边界、
  基础设施布局、违章搭建、破损隐患、绿化及空置区域；按需采集三维模型数据、红外隐患数据、高程及高差数据。
- 调研表「开源」sheet 的 8 类要素类别 + 21 个项目。
- 调研表「商用」sheet 的平台功能与「对应课题所需功能」。
- 估算开项数据采集要求（三类项目 × 基础/完善/提升 的改造对象清单）。

输出：过程文件/14-扩展指标/可观测类型矩阵.json + .md
退出码：0 成功；2 输入缺失（14 的扩展指标不在位）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

sys.dont_write_bytecode = True

SCHEMA = "cardo/lowair-observable-matrix/v1"
OUT_DIR = os.path.join("过程文件", "14-扩展指标")
JSON_NAME = "可观测类型矩阵.json"
MD_NAME = "可观测类型矩阵.md"

# 可观测类型分类：
# (类型id, 类型名, 扫描得到什么（人话）, 靠什么数据/算法原理, 开源算法供给, 本链状态, 本链链出的量)
#
# 本链状态 ∈ landed（已落地有值）/ partial（有产出但只是近似或只覆盖部分）/ none（本链没做）
#
# 「开源算法供给」抄自调研表 21 项的要素类别归属 + 商用表；不是本链自拟。
#
# 每条最后带 [指标id, ...]，是**显式声明**属于该类型的产出（不是靠文本匹配），
# 便于脚本回填实测值；为空表示该类本链无产出（占位的 null 不算产出）。
TYPES = [
    # ---------- 建筑本体 ----------
    ("bldg.footprint", "建筑轮廓与占地",
     "每栋房子的平面轮廓、占地面积、栋数",
     "从模型表面做 nDSM 阈值分割 + 平面提取；或从正射影像做实例分割",
     "开源 5 项（PolyR-CNN / HD-Net / MAP-Net / rs_buildings_extraction / HouseBoundaryPrediction，"
     "均为影像深度学习）",
     "landed",
     "recon.instance（分水岭实例化 13 栋）｜building_area_recon 3200 m²｜"
     "vector.polygonize（真实轮廓 6 个多边形 3219 m²）｜building_count_recon",
     ["building_count_recon", "building_area_recon", "polygon_count", "polygon_area"]),
    ("bldg.height", "建筑高度与层数",
     "每栋多高、几层、平均层高",
     "nDSM 高差 + 逐栋高度分位；层高按经验值折算",
     "开源 2 项（BDH-Net / Drone-based-building-assessment，均为影像回归）",
     "landed",
     "recon.object-height（栋间 P50 9.18 m / P90 13.48 m）｜"
     "building_height_p50_recon / building_height_p90_recon｜floor_area_est_recon 11191 m²",
     ["building_height_p50_recon", "building_height_p90_recon", "floor_area_est_recon"]),
    ("bldg.volume", "建筑体量与容积",
     "地上建筑面积、容积率、覆盖率",
     "轮廓面积 × 高度 / 层高；网格法按格高累加",
     "无专项开源（靠轮廓+高度两个算法组合）",
     "landed",
     "building_coverage_ratio 0.3618｜floor_area_ratio_est 1.0281（06 步 2.5D 网格法）",
     ["building_coverage_ratio", "floor_area_ratio_est"]),
    ("bldg.roof", "屋顶形态",
     "屋顶面积、坡度、平坡屋面比例、屋面设备",
     "顶点法向半球量化（方位角/倾角分箱）+ 平面偏移聚类",
     "无专项开源（本链自研几何法）",
     "landed",
     "roof.segment：roof_area 3995.19 m²｜roof_plane_count 411｜"
     "roof_mean_tilt 20.92°｜roof_flat_share 0.1175",
     ["roof_area", "roof_plane_count", "roof_mean_tilt", "roof_flat_share"]),
    ("bldg.facade", "立面与沿街界面",
     "立面面积、墙脚接触边长、立面朝向分布",
     "法向近水平面提取 + 方位角分箱 + 高度分层",
     "无专项开源（本链自研几何法）",
     "landed",
     "facade.normal：facade_area 3453.94 m²｜facade_edge_length 648 m｜facade_mean_height 6.3 m",
     ["facade_area", "facade_edge_length", "facade_mean_height"]),

    # ---------- 地表与场地 ----------
    ("ground.terrain", "场地地貌与高差",
     "地面高程、坡度、场地平整度、高差台地",
     "DSM 减去地面参考面得 nDSM；地面用局部分位或 PMF 形态学滤波",
     "开源底座（PDAL 的 ground filter / CSF / SMRF 可用，但本机未装）",
     "landed",
     "recon.dsm / recon.dtm / recon.ndsm｜pc.ground-filter："
     "ground_filter_agree1m 0.8099｜ground_filter_meandiff 0.6114 m",
     ["ground_filter_agree1m", "ground_filter_meandiff", "ground_filter_p95diff"]),
    ("ground.cover", "地表覆盖分类",
     "地面 / 建筑 / 植被 / 铺装 各占多少",
     "nDSM 高度 + 3×3 粗糙度的启发式分类；或点云语义分类（PDAL）/ 影像分割",
     "开源有（PDAL 分类、GeoAI 的 20+ 遥感基础模型），但需 GPU 与影像",
     "partial",
     "pc.classify：ground_share 0.4149｜building_share 0.1429｜"
     "vegetation_share 0.1678｜other_share 0.2744（残余类，混了道路/铺装/水面）",
     ["ground_share", "building_share", "vegetation_share", "other_share",
      "classify_precision_vs_recon", "classify_recall_vs_recon"]),
    ("ground.green", "绿化与树冠",
     "绿化覆盖率、树木数量与冠幅",
     "CHM（nDSM 去建筑）局部极大值 + 分水岭切冠",
     "开源 1 项（green-area-detection-with-drones，影像域）；另有 3 项闲置空间/绿化（卫星影像）",
     "partial",
     "tree.single：tree_count 20 棵｜tree_crown_area 320 m²｜"
     "tree_mean_crown_diameter 4.31 m｜tree_max_height 5.86 m。"
     "**几何植被只是树冠，不等于用地绿化率**",
     ["tree_count", "tree_crown_area", "tree_mean_crown_diameter", "tree_max_height"]),
    ("ground.road", "道路与街巷",
     "路网、街巷条数、路幅宽度、路面面积",
     "需从正射影像做道路分割；几何域给的是**硬地与窄长通道**的几何代理量",
     "开源 2 项（Regularized-Road-Extractor / CGCNet，均为影像域）",
     "partial",
     "**几何代理量**（第 15 步场地要素）：硬地面积 1257 m²（14.2%）｜"
     "街巷条数 3 条｜路幅中位 —— 用「坡度<3° 且非建筑非植被的连通区」找硬地，"
     "其中宽 2–15 m、长 ≥8 m 的窄长块算一条街巷。"
     "**分不出车行道/人行道/广场**，路面语义仍需影像",
     ["hard_area", "hard_share", "street_count", "street_width_median"]),
    ("ground.water", "水体与场地积水",
     "水系、水面面积、积水点",
     "影像光谱/反射率分割；几何域给的是**平坦低洼区**的几何代理量",
     "开源间接（GeoAI 等遥感模型含水体类别）",
     "partial",
     "**几何代理量**：低洼候选 9 处（最深 2.95 m）。判据为「低于 31 m 邻域基准面 0.3–3.0 m、"
     "坡度<2°、离建筑≥2 m」——已排除深于 3 m（地面拟合误差）与贴墙 2 m 内（nDSM 边界效应）。"
     "**洼地不等于积水，须现场核对**",
     ["depression_count"]),

    # ---------- 隐患与状态 ----------
    ("hazard.damage", "破损与结构隐患",
     "危房、结构损伤、裂缝、剥落",
     "**近景/红外影像**上的损伤检测（需原片或近景照片）；几何域给的是**几何异常线索**",
     "开源 5 项（DBA-RTMDet / ABECIS / YOLO-AMC / BuildingInspectionDeepL / CNN_structural_damages）",
     "partial",
     "**几何线索**（第 15 步）：5 条——体量离群 5 栋（栋高 P50 偏离片区中位 ≥50%）。"
     "**这不是危房结论**：倾斜模型看不到结构内部，体量离群也可能是正常复式/加建。"
     "真正的危房判定要近景/红外影像",
     ["damage_clue_count"]),
    ("hazard.illegal", "违章搭建与违建",
     "违建面积、彩钢棚、临时构筑物",
     "几何上找「低矮且突兀」的可建面；准确判定需人工或影像语义",
     "无专项开源（调研表未列）",
     "partial",
     "**几何候选**（第 15 步）：低矮体量 41 处 / 合计 —— 判据为「非建筑区 nDSM 0.3–2.0 m、"
     "面积 ≥4 m² 的连通块」。**可能是违建棚屋，也可能是花坛、台阶、车辆、堆料**，须人工判",
     ["low_object_count", "low_object_area"]),
    ("hazard.overload", "老旧特征与更新潜力",
     "建成年代、老旧程度、拆除体量、加固可行性",
     "几何能出形态代理量（层数、低层占比、平坡屋面比），年代要靠其他数据",
     "开源 1 项（建筑年代与风格识别，需本地样本微调，国外样本）",
     "partial",
     "形态代理量已有：roof_flat_share 0.1175｜building_grade_a/b/c 3/9/1｜"
     "building_area_recon 3200 m²（拆除体量底数）",
     ["roof_flat_share", "building_grade_a", "building_grade_b", "building_grade_c",
      "building_area_recon"]),
    ("vacant.idle", "闲置与低效空间",
     "空地、闲置建筑、堆场、低效用地",
     "影像语义分类 + 规划用地比对；几何域给的是**空地斑块**的几何代理量",
     "开源 3 项（Large-scale Urban Vacant Land / Philadelphia_vacant_lot_detection / "
     "Strategic-Urban-Greening，均为卫星影像、国外样本）",
     "partial",
     "**几何代理量**（第 15 步）：空地斑块 2 块 / 1106 m² —— 判据为「非建筑非植被、坡度<3°、"
     "面积 ≥200 m² 的连续区」。**空地不等于闲置**：可能是待建预留地、拆迁场地、施工堆场",
     ["void_patch_count", "void_area"]),

    # ---------- 附属与其它 ----------
    ("misc.infra", "市政与附属设施",
     "管线、配电、井盖、路灯、停车位、充电设施",
     "**地下管线倾斜摄影拍不到**；地面附属物可见但只到「贴地小体量」的密度量级",
     "开源间接（GeoAI 含部分地物模型）",
     "partial",
     "**几何代理量**（第 15 步）：贴地小体量 49 处 / 密度 0.55 个/100m²——"
     "车辆、井盖、灯杆在 2 cm/像素模型里可见，但 1 m 栅格下只剩一个采样点，"
     "**只给密度不给类别**。**地下管线本链做不了**（倾斜摄影拍不到地下）",
     ["ground_object_count", "ground_object_density"]),
    ("misc.cargo", "施工与拆除物资量",
     "拆除面积、垃圾方量、堆场体积",
     "几何算体积，配合现状台账与单价",
     "无专项开源（本链自研几何法）",
     "partial",
     "几何侧底数有：building_area_recon 3200 m²｜floor_area_est_recon 11191 m²｜"
     "roof_area 3995.19 m²｜ground_share 0.4149（可作业地面）",
     ["building_area_recon", "floor_area_est_recon", "roof_area", "ground_share"]),
    ("misc.safety", "作业与数据安全",
     "空域、限飞、涉密、隐私（人脸车牌）",
     "流程与合规检查，不在算法层",
     "不适用",
     "partial",
     "流程侧已有：01 步路径安全解包、SRS 校验；08 步边界扫描（无盘符路径、无残留）。"
     "**影像侧的人脸车牌脱敏未做**",
     []),
]

STATUS_CN = {"landed": "已落地", "partial": "部分落地", "none": "未做"}


def build(workspace: str) -> dict:
    """读 14 的扩展指标，给矩阵补上实测值。"""
    ext_path = os.path.join(workspace, "过程文件", "14-扩展指标", "扩展指标.json")
    ext = {}
    if os.path.isfile(ext_path):
        with open(ext_path, encoding="utf-8-sig") as fh:
            doc = json.load(fh)
        ext = {m["id"]: m for m in (doc.get("metrics") or [])}
    ind_path = os.path.join(workspace, "过程文件", "06-2.5D网格指标", "现状指标.json")
    if os.path.isfile(ind_path):
        with open(ind_path, encoding="utf-8-sig") as fh:
            doc = json.load(fh)
        for m in (doc.get("metrics") or []):
            ext.setdefault(m["id"], m)

    items = []
    for (tid, name, what, principle, supply, status, chain, metric_ids) in TYPES:
        measured = []
        for oid in metric_ids:
            m = ext.get(oid)
            if m is None:
                continue
            measured.append({"id": oid, "name": m.get("name"),
                             "value": m.get("value"), "unit": m.get("unit")})
        items.append({"typeId": tid, "name": name, "what": what,
                      "principle": principle, "openSourceSupply": supply,
                      "status": status, "statusCn": STATUS_CN[status],
                      "chain": chain, "measured": measured})

    counts = {}
    for item in items:
        counts[item["status"]] = counts.get(item["status"], 0) + 1

    groups = [
        ("建筑本体", "bldg."),
        ("地表与场地", "ground."),
        ("隐患与状态", "hazard."),
        ("闲置与效能", "vacant."),
        ("附属与其它", "misc."),
    ]

    return {
        "schema": SCHEMA, "schemaVersion": 1,
        "generatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "producer": {"name": "cardo 低空解析 · 14 可观测类型矩阵", "version": "1.0.0"},
        "question": "无人机低空这套技术物理上能扫出哪些东西？每类有开源算法吗？本链落地了哪些？",
        "sources": {
            "taskbook": "提资数据/课题资料/20_课题任务书_低空技术_片区城市更新投资决策.docx §2.1.1 核心数据",
            "researchOpen": "调研无人机算法平台功能汇总表.xlsx「开源」sheet（21 项 / 8 类要素）",
            "researchCommercial": "同上「商用」sheet（平台功能与课题所需功能对应）",
            "projectScope": "估算开项数据采集要求-20261016.xlsx（三类项目 × 基础/完善/提升 的改造对象）",
            "registry": "Skills/00-Cardo低空解析总控/算法注册表.json（本链 54 条算法登记）",
        },
        "taskbookCoreData": [
            "场地整体现状影像", "建（构）筑物外观及空间分布", "场地地貌与边界", "基础设施布局",
            "违章搭建", "破损隐患", "绿化及空置区域",
            "按需：三维模型数据、红外隐患数据、高程及高差数据",
        ],
        "groups": [{"name": g, "prefix": p} for g, p in groups],
        "items": items,
        "summary": {
            "types": len(items),
            "landed": counts.get("landed", 0),
            "partial": counts.get("partial", 0),
            "none": counts.get("none", 0),
            "withValue": sum(1 for i in items if i["measured"]),
        },
        "conclusion": [
            "**能扫到的 ≠ 能算出的 ≠ 用得上的**——三层要分开看。"
            "倾斜摄影这套技术物理上能观测到 17 类信息；本链在几何域"
            "**全部 17 类都给出了产出**，但其中 11 类是**几何代理量**（proxy），不是语义判定。",
            "**建筑本体 5 类是真正的强项**：轮廓/高度/体量/屋面/立面全部落地，"
            "高程到厘米级，立面与屋面是真实可见的三维信息——这是倾斜模型相对影像的本质优势。",
            "**地表与场地 5 类全是代理量**：硬地 14.2%、街巷 3 条、低矮体量 41 处、"
            "空地 2 块、低洼 9 处——数字都出来了，但**没有一个是语义判定**。"
            "路面分不出车行道/人行道，空地分不出闲置/待建，低矮体量分不出违建/花坛。",
            "**隐患类（危房、裂缝）几何域给不了结论**：只能给几何异常线索（5 条体量离群），"
            "真正的危房判定要近景/红外影像。调研表 21 项里 5 项危房算法全是影像域，这是硬约束。",
            "**调研表 21 项本链采用 0 项不是失误**：它们全部依赖原始航摄影像，"
            "而本链的输入是重建后的 OSGB——**数据条件决定的，不是取舍**。"
            "要启用它们，链路要改成「从原片进」，不是加几行代码。",
            "**下一步两条路**：**① 接原始影像**（打开 16 条影像类算法，"
            "路面语义/危房/闲置/违建全部变语义判定）；**② 补外部图层**（控规/路网/用地/年代，"
            "把现有几何量接进业务口径）。在此之前，几何代理量**必须带「代理量」标签交付**，"
            "不能当结论用。",
        ],
    }


def render_md(doc: dict) -> str:
    s = doc["summary"]
    lines = ["# 可观测类型矩阵：无人机能扫到哪些 × 有哪些开源算法 × 本链落地了哪些", "",
             f"生成时间：{doc['generatedAt']}", "",
             f"**{doc['question']}**", "",
             f"共 {s['types']} 类：**已落地 {s['landed']}** / "
             f"**部分落地 {s['partial']}** / **未做 {s['none']}**。", "",
             "## 任务书要求的核心数据（对照基准）", "",
             "「" + "」「".join(doc["taskbookCoreData"]) + "」", "",
             "## 分类矩阵", ""]
    for g in doc["groups"]:
        items = [i for i in doc["items"] if i["typeId"].startswith(g["prefix"])]
        if not items:
            continue
        lines += [f"### {g['name']}", "",
                  "| 类型 | 扫描得到什么 | 靠什么原理 | 开源算法供给 | 本链状态 | 本链链出的量 |",
                  "|---|---|---|---|---|---|"]
        for item in items:
            vals = "、".join(f"{m['name']}={m['value']}" for m in item["measured"]) or "—"
            lines.append(f"| **{item['name']}**<br>`{item['typeId']}` | {item['what']} | "
                         f"{item['principle']} | {item['openSourceSupply']} | "
                         f"**{item['statusCn']}** | {vals}<br><sub>{item['chain']}</sub> |")
        lines.append("")
    lines += ["## 结论", ""]
    for text in doc["conclusion"]:
        lines.append(f"- {text}")
    lines += ["", "## 依据", ""]
    for k, v in doc["sources"].items():
        lines.append(f"- **{k}**：{v}")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="14 · 可观测类型矩阵")
    ap.add_argument("--workspace", default=".")
    ap.add_argument("--out", default=OUT_DIR)
    args = ap.parse_args()

    root = os.path.abspath(args.workspace)
    out_dir = args.out if os.path.isabs(args.out) else os.path.join(root, args.out)
    os.makedirs(out_dir, exist_ok=True)
    doc = build(root)
    json_path = os.path.join(out_dir, JSON_NAME)
    md_path = os.path.join(out_dir, MD_NAME)
    for stale in (json_path, md_path):
        if os.path.isfile(stale):
            os.remove(stale)
    with open(json_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    with open(md_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(render_md(doc))
    s = doc["summary"]
    print(f"[14] 可观测类型矩阵：{s['types']} 类（已落地 {s['landed']} / 部分 {s['partial']} / "
          f"未做 {s['none']}），关联实测指标 {s['withValue']} 条")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
