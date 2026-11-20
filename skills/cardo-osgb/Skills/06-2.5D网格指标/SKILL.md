# 06 · 2.5D 网格指标

> 从 `cardo-lowair-model.json` + b3dm 顶点算 7 项现状指标：**估算值 + 方法敏感性区间 + 置信度 + 局限**。
> 绿化率、道路占比由 **纹理/材质 P0**（植被指数 + UV 采样 + 建筑掩膜）给出带区间与置信度的代理值；
> 无可用纹理才 `null` + placeholder + gaps，**绝不编数**。真值校核未达产品纳入阈值，按**诊断口径**标注。

## 何时用

- 05 通过、要出「现状指标」时；
- 调网格/地面/阈值做敏感性分析时（改 `--cell-m` 只影响点值，区间恒按三档重算）。

## 执行命令（工作区根）

```bash
python Skills/06-2.5D网格指标/scripts/build_indicators.py
python Skills/06-2.5D网格指标/scripts/build_indicators.py --cell-m 0.5 --surface top
python Skills/06-2.5D网格指标/scripts/build_indicators.py --surface all   # v1 全顶点口径（对照/回退）
```

也可单独跑底层引擎（写同名产物，不跑闸门）：

```bash
python Skills/06-2.5D网格指标/scripts/run_indicators.py --model <cardo-lowair-model.json> --out <目录> \
    [--cell-m 1.0] [--surface auto|top|all] --force
```

## 输入 / 输出

| 输入 | 输出（默认 `过程文件/06-2.5D网格指标/`） |
|---|---|
| `过程文件/03-OSGB转3DTiles/转换产物/cardo-lowair-model.json` | `现状指标.json`（`cardo/lowair-indicators/v1`） |
| `artifacts[].path` 指向的 b3dm（GLB POSITION + NORMAL） | `指标说明.md`（人读口径/敏感性/缺项） |

## 方法（v1.1，详见 `规则/指标口径.md`）

1. 顶点投到 `geometry.bboxLocal` 的 x/y 平面，每格取 **最大 z**（默认 1.0 m）；
   **有 numpy 时走向量化**（`np.maximum.at`，`params.engine=numpy`），无 numpy 自动退回逐点循环；
2. **表面口径**：`auto` 在「法线顶点占比 ≥30% 且 ≥90% 观测格有顶面顶点」时用 `top`
   （只收 `NORMAL.z ≥ 0.5` 的顶点），否则 `all`（v1 口径）；本模型 57.8% / 99.94% → `top`；
3. 地面：15 m 邻域内观测格 max_z 的 **P10**（局部，处理坡地）；
4. mask：`格 max_z − 该格地面 > 3.0 m`；分母只计有观测格；
5. 栋数 = mask 做 **4 邻域 ×1 格闭运算**（只在观测格内）后的连通分量数；覆盖率/高度/容积率用未闭运算的 mask；
6. 高度 = mask 格高度 P50/P90；容积率 = `Σ(格面积 × ceil(格高 / 3.0)) / bbox 投影面积`（**非报建口径**）；
7. 区间 = 网格 0.5/1/2 m × 地面口径（邻域 P10 / 全局 P5）× 层高 2.8/3.3 m 的 min–max。

## 纹理/材质 P0（v1.2，详见 `规则/纹理材质识别.md`）

1. 每个朝上三角面（`nz ≥ 0.5`）取重心 UV，按 UV `mod 1` 采 b3dm 内嵌贴图（解码上限 256 px）；
2. 判绿：`ExG = 2g−r−b`（另有 VARI/GLI），`ExG ≥` Otsu 阈值（clamp 0.03–0.15）+ HSV `H40–130 / S≥50 / V≥30`；
3. 判道路：非绿、`S<40`、`V35–215`、`ExG<0.35×阈值`、离地 `≤0.8 m`（敏感性 0.45/0.8/1.3）、且不在 2.5D 建筑掩膜内；
4. 分母（写死）：**模型覆盖范围内全部朝上表面三角面（含建筑屋面，按三角面面积加权）**，不扣屋顶；
5. 区间 = 网格 0.5/1/2 m × ExG 阈值三档（道路再加饱和/高度三档）；置信度 `medium/low`，P0 不声称 `high`。
6. 真值校核：公园样例 50×50 m 混合块，两轮均未达标——第 1 轮绿化 0.3850 / 道路 0.4282（格级）；
   第 2 轮（冠芯补判 + 多数票补洞 + 道路放宽高差 + 面积加权）绿化 0.3914 / 道路 0.4469（格级），
   像素级绿化 0.3935 / 道路 0.4115；仍低于 0.70 / 0.60 → **保留诊断口径，不纳入产品能力**，不再做第三轮。

## 闸门（任一不过 → exit 3）

- `schema == cardo/lowair-indicators/v1`；
- 7 个指标 id 齐全、无多余；
- 每条指标 10 个必填键：`id/name/value/range/unit/method/confidence/limits/source/grade`；
- `range` 必须是二元组；`value=null` 的必须 `grade=placeholder`；
- `quality.usable = true`。

## 本模型实测（v1.1）

范围 12,248.99 m²（122.053 × 100.358 m），有效格 8,869，mask 格 3,209，地面 z = −59.64 m；
表面口径 `top`（法线顶点 57.8%、顶面格 99.94%）；
建筑密度 `0.361822 [0.361822, 0.623407] medium`；栋数 `18 [10, 77] low`（闭运算前 27 个分量）；
P50 `9.89 [6.58, 10.15] m`；P90 `17.03 [16.81, 17.49] m`；容积率 `1.0281 [0.8404, 1.7019] low`；
绿化率 `0.37666 [0.373386, 0.378129] medium`、道路占比 `0.127849 [0.087233, 0.15379] medium`
（纹理代理·诊断口径，不是用地面积）。v1→v1.1 差异见 `规则/指标口径.md §5`。

## 诊断（不进区间，但必读）

`diagnostics.surface`（模式/法线覆盖/顶面格）、`diagnostics.mask`（闭运算前后格数与分量数）、
`diagnostics.buildings`（前 5 个候选：格数 / 高度 P50 / 最高点 / 质心）、`diagnostics.components`。

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 指标可用且闸门通过 |
| 2 | 缺 `cardo-lowair-model.json`（先跑 05） |
| 3 | 输入不可用（`usable=false`，仍落盘降级产物）或闸门不过 |

## 调优点

- 点值随 `--cell-m` 变，区间**不**收窄；不要用点值差异去当"精度"。
- 栋数对格距极敏感（0.5/1/2 m → 45/27/13），报告中必须带区间与"候选值"字样。
- LOD 重叠：网格取 max z 会混合粗/精模，未按 LOD 过滤（已记 `IND-GAP-003`）。
