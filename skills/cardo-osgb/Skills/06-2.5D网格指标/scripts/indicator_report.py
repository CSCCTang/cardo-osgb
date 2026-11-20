"""指标说明 Markdown 渲染（从 run_indicators.py 拆出，保持单文件 <=400 行）。

调优（2026-10-25）：对降级产物（输入不可用 → quality.usable=false、metrics=[]、部分字段 None）
容错渲染：None 显示「—」，无指标时输出口径说明，不抛 KeyError。
"""
import math  # noqa: F401

LOCAL_GROUND_M = 15.0   # 与 run_indicators.py 保持一致（邻域半径）
LOCAL_Q = 10.0          # 邻域地面分位


def _d(v):
    """None → 「—」，其余原样（渲染降级产物用）。"""
    return "—" if v is None else v


def render_md(doc):
    p, s = doc["params"], doc["scope"]
    rows = ["| 指标 | 值 | 区间 | 单位 | 置信度 | 等级 | 口径 | 局限 |", "|---|---|---|---|---|---|---|---|"]
    for m in doc["metrics"]:
        val = "—（占位）" if m["value"] is None else f"{m['value']}"
        rng = "—（不提供）" if None in (m["range"][0], m["range"][1]) else f"{m['range'][0]} – {m['range'][1]}"
        rows.append(f"| {m['name']}（`{m['id']}`） | {val} | {rng} | {m['unit']} | {m['confidence']} | "
                    f"{m['grade']} | {m['method']} | {m['limits']} |")
    if not doc["metrics"]:
        rows.append("_（本次未产出任何指标：输入模型不可用，只做输入检查与口径说明，不编数；见「缺项与局限」。）_")
    sens = ["| 网格边长 (m) | 建筑密度 | 建筑栋数 | 高度 P50 (m) | 高度 P90 (m) | 容积率估算 | mask 格 / 有效格 |", "|---|---|---|---|---|---|---|"]
    for row in doc["sensitivity"]:
        sens.append(f"| {row['cellM']:g} | {row['coverage']} | {row['buildingCount']} | {row['heightP50']} | "
                    f"{row['heightP90']} | {row['floorAreaRatio']} | {row['maskCells']} / {row['validCells']} |")
    if not doc["sensitivity"]:
        sens.append("_（无敏感性数据：输入不可用。）_")
    gaps = [f"- `{g['code']}` {g['what']}｜{g['why']}｜需要：{g['need']}" for g in doc["gaps"]]
    q, d = doc["quality"], doc["diagnostics"]
    src = doc["source"]
    tex = p.get("texture") or {}
    tex_diag = d.get("texture") or {}
    tex_lines = []
    if tex.get("vegetation"):
        tv, ts, tr = tex["vegetation"], tex.get("sampling") or {}, tex.get("road") or {}
        hv = tv.get("hsv") or {}
        tex_lines = [
            f"- 纹理/材质 P0（{tex.get('methodVersion') or 'texture-p0'}）：采样 cell {_d(ts.get('sensitivityCellM'))}，"
            f"贴图≤{_d(ts.get('textureDecodeMaxPx'))} px，ExG 阈值 {_d(tv.get('exgThreshold'))}"
            f"（{_d(tv.get('exgThresholdMode'))}，clamp {_d(tv.get('exgThresholdClamp'))}），"
            f"敏感性三档 {_d(tv.get('exgSensitivityThresholds'))}；"
            f"HSV 绿相 {_d(hv.get('hueRange'))}（Pillow 0–255）/饱和≥{_d(hv.get('saturationMin'))}"
            f"/亮度≥{_d(hv.get('valueMin'))}",
            f"- 纹理采样：{_d(tex_diag.get('samples'))} 个样本 / {_d(tex_diag.get('sampledCells'))} 格"
            f"（覆盖 {_d(tex_diag.get('coverage'))}，每格中位 {_d(tex_diag.get('medianSamplesPerCell'))} 样本），"
            f"{_d(tex_diag.get('textureImages'))} 张贴图、{_d(tex_diag.get('primitivesUsed'))} 个 primitive；"
            f"全样本绿占比（面积加权）{_d(tex_diag.get('greenShareAll'))}，非屋面口径诊断 {_d(tex_diag.get('nonRoofGreenShare'))}",
            f"- 明度分层与补洞：暗部样本 {_d(tex_diag.get('darkVegetationSamples'))}、"
            f"亮部低饱和冠芯样本 {_d(tex_diag.get('canopyVegetationSamples'))}、"
            f"多数票补洞格 {_d(tex_diag.get('greenFillCells'))}；"
            f"道路离地高度三档 {_d(tr.get('heightSensitivityM'))} m；"
            f"建筑掩膜按 2.5D 网格（离地 >3 m）排除屋面/立面",
            f"- 置信度：{_d((tex.get('confidenceRule') or {}).get('medium'))} → medium；否则 low。"
            f"P0 不声称 high；真值校核结论见《纹理材质识别.md》第 6 节。",
        ]
    else:
        tex_lines = [f"- 纹理/材质 P0 未出数：{_d(tex.get('reason') or tex.get('status'))}"]
    final_note = ("绿化率 / 道路占比为纹理覆盖代理值（分母含建筑屋面，按三角面面积加权），不是平面用地面积。"
                  if tex.get("vegetation") else "绿化率 / 道路占比本次未出数，原因见诊断。")
    return "\n".join([
        "# 现状指标说明", "",
        f"- 生成时间：{doc['generatedAt']}　生成器：{doc['producer']['name']} {doc['producer']['version']}（`{doc['schema']}`）",
        f"- 输入模型：`{src[0]['ref']}`　产物：`{_d(src[1]['ref'])}`"
        f"（{src[1]['tiles']} 个 b3dm，解析 {src[1]['parsed']}，顶点 {src[1]['vertices']}）",
        f"- 范围：bbox x/y 投影 {_d(s['areaM2'])} m²（`{s['areaMethod']}`），范围 {_d(s['extentM']['x'])} m × "
        f"{_d(s['extentM']['y'])} m，CRS {_d(s['crs'])}",
        f"- 参数：cell {p['cellM']:g} m，ground P{p['localGroundPercentile']:g}，建筑阈值 {p['buildingThresholdM']:g} m，"
        f"表面口径 {p.get('surface')}（法线覆盖 {p.get('normalsVertexShare')}），"
        f"ground z = {_d(s['groundZ'])} m，有效格 {s['validCells']}，mask 格 {s['maskCells']}",
        "", "## 指标", "", *rows, "",
        "## 敏感性（区间来源）", "", *sens, "",
        "区间 = 三档网格 × 地面口径（邻域 P10 / 全局 P5）× 层高 2.8 / 3.3 m 的 min–max，**不是**精度估计。", "",
        "## 诊断（不进入区间）", "",
        f"- 地面口径（主）：{LOCAL_GROUND_M:g} m 邻域 P{LOCAL_Q:g} 局部地面覆盖率 {_d(d['coverageByGround']['localNeighborhoodP10'])}；"
        f"对照：全局 P5 地面覆盖率 {_d(d['coverageByGround']['globalP5'])}（差异已计入 range）"
        f"（格 z：P5 {_d(d['cellZ']['p5'])} m / P50 {_d(d['cellZ']['p50'])} m）",
        f"- 连通分量：{d['components']['total']} 个，最大 {d['components']['largestCells']} 格"
        f"（占 mask {100.0 * d['components']['largestShare']:.1f}%），小于 5 格的分量 {d['components']['lt5Cells']} 个",
        *tex_lines, "",
        "## 缺项与局限", "", *gaps, "",
        "## 可用性", "", f"- `quality.usable = {q['usable']}`：{q['reason']}", "",
        "> 本表为几何估算，只代表「估算值 + 区间 + 置信度 + 局限」，不含任何精度达标声明；"
        + final_note, ""])
