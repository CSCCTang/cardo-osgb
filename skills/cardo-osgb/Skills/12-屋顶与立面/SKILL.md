# 12 · 屋顶与立面

> 独立技能：只写 `Skills/12-屋顶与立面/` 与 `过程文件/12-屋顶与立面/`，
> 不改注册表 / README / pipeline.py / 其他技能，不装包、不跑 08。

## 何时用

要在**只有三维模型**（3D Tiles / b3dm）、没有影像与点云分类的情况下，量出两类几何：

- 屋顶：这栋楼分成几块屋面、多少屋面面积、多陡、平不平（`roof.segment`）；
- 立面：竖向面一共多少面积、朝哪边多、各高度层多少、墙脚接触边长（`facade.normal`）。

典型场景：低空航测模型的屋顶形态核查、立面朝向分布、屋面/立面面积做粗算基数。
**不能**当作建筑面积、外墙面积或语义真值使用（见「局限」）。

## 命令

```powershell
# 两条一起跑（工作区根）
python Skills/12-屋顶与立面/scripts/run_roof_facade.py --workspace .

# 只跑一条 / 覆盖重算
python Skills/12-屋顶与立面/scripts/run_roof_facade.py --workspace . --only roof
python Skills/12-屋顶与立面/scripts/run_roof_facade.py --workspace . --force

# 单算法直跑
python Skills/12-屋顶与立面/scripts/roof_segment.py --workspace . [--force]
python Skills/12-屋顶与立面/scripts/facade_normal.py --workspace . [--force]
```

`run_roof_facade.py` 用 `sys.executable` 起子进程，全部 exit 0 才返回 0，否则 1。
脚本先尝试 `from common import …`（主线只读契约）；**本机 `python` 没装 numpy 时自动改用
`scripts/common_fallback.py`**（纯 stdlib 读 npz/GLB），两条路径算法一致、结果一致，不装包。
`params`/`inputs` 里的 `commonImpl` 会写明实际用的是 `common` 还是 `common_fallback`。

## 输入

| 输入 | 路径 | 用途 |
|---|---|---|
| 模型契约 | `过程文件/03-OSGB转3DTiles/转换产物/cardo-lowair-model.json` | `artifacts[].path` 列 b3dm；`crs.origin` 给 EPSG:4544 原点 |
| 反演结果 | `过程文件/09-三维要素反演/三维要素反演.json` | 建筑阈值 `buildingThresholdM`、mask 格数核对 |
| 栅格 | `过程文件/09-三维要素反演/栅格/栅格.npz` | `ndsm/observed/ground`，1 m 格，axis0=+x、axis1=+y |
| 逐栋属性 | `过程文件/09-三维要素反演/逐栋属性.json` | 建筑质心（世界坐标）与 OBB 角点（局部坐标） |

只读，不重新解析 OSGB 二进制；坐标系 **EPSG:4544**，世界坐标 = `origin` + 局部坐标。

## 输出

`过程文件/12-屋顶与立面/<slug>/`（`slug = id.replace(".", "-")`）：

| 文件 | 说明 |
|---|---|
| `roof-segment/roof-segment.json` + `.md` | schema `cardo/lowair-enhance/v1`，`common.emit` 生成 |
| `facade-normal/facade-normal.json` + `.md` | 同上 |
| `_done/<slug>.json` | `common.emit` 自动写；`exitCode` 0=可用、3=不可用 |
| `_patches/<slug>.json` | 注册表补丁（9 个字段，见 `规则/口径.md` §4） |

`summary` 全部是实测值，算不出写 `null` 并在 `gaps[]` 说明；键名英文小驼峰。

## 口径（详见 `规则/口径.md`）

- **LOD 选层**：3D Tiles 金字塔每层都覆盖同一场景，全部 b3dm 相加会重复计数（实测约 5–6 倍）。
  先按面统计，再挑「mask 内屋面面积 ≥ 0.85×建筑占地」的最细层，只用该层。
- **屋面**：三角面法向取三顶点 `NORMAL` 面积加权均值（缺法向用叉积）；`nz ≥ 0.7`；
  再用 09 建筑 mask（`ndsm>阈值 & observed` + close/open 3×3，纯 numpy 自实现）挡掉地形与树冠；
  法向按方位角 15° + 倾角 5° 量化分箱，箱内平面偏移排序，相邻差 > 0.5 m 切开。
- **立面**：`|nz| < 0.4` 的三角面，方位角以正北（+y）为 0°、顺时针 15° 分箱；
  归并北/东/南/西四方位；高度层 3 m，高度 = 面重心 z − 09 地面；
  `footprintEdgeM` 是重算 mask 的 4 邻接界面总长（含栅格外边界）× 格宽。

## 闸门（`quality.usable`）

- `roof.segment`：至少 1 个面积 ≥ 2 m² 的子段，且屋面实测面积 > 0；b3dm 无解析失败（有失败则写 gap）。
- `facade.normal`：立面实测面积 > 0。
- 不过闸 → `usable=false` + `reasons[]`，退出码 3；**不伪造产物**。

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 产出且 `usable=true` |
| 2 | 缺输入（模型契约 / 栅格 / 反演结果） |
| 3 | 产出但 `usable=false`（`reasons` 必写） |
| 4 | 产物已存在且未给 `--force` |
| 1 | `run_roof_facade.py` 有子算法非 0 |

已有 `_done/<slug>.json` 且 `exitCode=0` 时脚本直接跳过、不改文件（显式 `--force` 例外）。

## 局限（全部写进 gaps，禁止抹平）

- 未做 RANSAC 随机采样，平面用确定性分箱 + 偏移聚类；窄条面、曲面屋顶鲁棒性弱。
- 屋面附属物（女儿墙、天窗、设备顶、树冠顶、棚顶）未剔除，会并入屋面子段。
- 屋面候选经 1 m 建筑 mask 过滤，mask 是近似（09 未落盘逐栋 label），贴边屋顶可能被切。
- 立面面积**含山墙/围墙/灌木侧面**，未区分建筑与附属，不是「建筑外墙面积」。
- 建筑归属用 09 OBB 包含 + 最近质心 ≤ 60 m，跨栋/共墙可能误判，无法判定写 `null`。
- 只统计单层 LOD，未做跨层几何去重；不同 LOD 层会给出不同的面积。
