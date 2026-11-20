# 09 · 三维要素反演

> 从 b3dm 顶点反推 **DSM / 地面参考面 / nDSM**，再按离地高度切出**逐栋建筑候选**，出轮廓 + 高度 + 层数 + 置信度。
> **只读标准容器（b3dm 头 / GLB / glTF accessor），不解析 OSGB 字节；不编数，算不出的进 gaps。**

## 何时用

- 已经有 3D Tiles（03/04 通过），想要「哪里有楼、多高、几层、占地多大」的**几何反演结果**；
- 要把模型变成能拖进 GIS 的矢量（09 出 GeoJSON，10 做拓扑清理与属性表）；
- 想验证 06 的栋数/高度量级是否自洽（09 与 06 用同一套地面口径，可互相对账）。

## 执行命令（工作区根）

```bash
python Skills/09-三维要素反演/scripts/run_reconstruct.py
python Skills/09-三维要素反演/scripts/run_reconstruct.py --cell-m 0.5 --ground-window-m 18
python Skills/09-三维要素反演/scripts/run_reconstruct.py --split-m 0          # 退回连通域（不拆分水岭）
python Skills/09-三维要素反演/scripts/run_reconstruct.py --force              # 产物已存在时必须加
```

也可由总控按算法选：`python Skills/00-Cardo低空解析总控/scripts/pipeline.py run --algos recon.nDSM,recon.instance`

## 输入 / 输出

| 输入 | 输出（默认 `过程文件/09-三维要素反演/`） |
|---|---|
| `过程文件/03-OSGB转3DTiles/转换产物/cardo-lowair-model.json` | `三维要素反演.json`（`cardo/lowair-reconstruct/v1`） |
| `artifacts[].path` 指向的 b3dm（GLB POSITION + NORMAL） | `要素矢量.geojson`（`cardo/lowair-vector/v1`，EPSG:4544） |
| — | `逐栋属性.json`（每栋全字段 + 降级理由） |
| — | `栅格/栅格.npz`（dsm / ground / ndsm / observed / cell / x0 / y0） |
| — | `反演报告.md`（人读：方法 / 结果 / 敏感性 / 逐栋 / gaps） |

## 方法（`reconstruct-v1`，详见 `规则/口径.md`）

1. **DSM**：只取 `NORMAL.z ≥ 0.5` 的**朝上顶点**投到 1 m 格，每格取最大 z（法线缺失的 primitive 全用并记
   `REC-GAP-009`）；
2. **地面参考面**：窗口 ±15 m 内**有观测格**的 max_z 的 **P10**（排除空洞格，不用大窗口形态学开运算）；
3. **nDSM** = DSM − 地面参考面（离地高度）；
4. **mask** = `nDSM > 3.0 m`（与 06 同阈值）+ 3×3 闭运算补洞 + 3×3 开运算去毛刺；
5. **实例**：分水岭（nDSM 高斯平滑 → `peak_local_max`（种子间距 6 m）→ `watershed`），
   没有 scikit-image 或 `--split-m 0` 时退回连通域，产物里写明用了哪种；
6. **逐栋属性**：面积 / P50 / P90 / max / CV / 层数（`round(P50/3.0)`，区间 2.8–3.3 m）/ 质心（+origin）；
7. **轮廓**：格四角 → 凸包 → 旋转卡壳**最小外接矩形（OBB）**，贴合率 = 格面积 / OBB 面积；
8. **置信度 A/B/C**：面积、贴合率、高度 CV、是否压边界、遮挡邻格五项判级，理由写进 `gradeReasons`。

## 闸门（任一不过 → exit 3）

- `quality.usable = true`（至少一栋候选且 nDSM 有效）；
- 每栋建筑 11 个必填键齐全：`id/areaM2/heightP50/heightP90/heightMax/floorsEst/floorsRange/centroidXY/footprint/confidence/gradeReasons`；
- `schema == cardo/lowair-reconstruct/v1`；GeoJSON 的 `crs` 与模型 `crs.srs` 一致。

## 本模型实测（1 m 格，地面窗口 15 m，阈值 3.0 m，分水岭 6 m）

有效格 8,869 / 总格 12,423；mask 3,209 格（与 06 的 `maskCells` 完全一致）；**13 栋**候选，
占地 3,200 m²；逐栋 H50 中位 9.18 m、H90 中位 13.48 m；A/B/C = 3/9/1；
最大一栋 938 m²、H50 8.68 m（分水岭把 06 里 2,819 格的大连通块拆成了多栋）。

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 反演可用且闸门通过 |
| 2 | 缺 `cardo-lowair-model.json`（先跑 05） |
| 3 | 反演不可用（`usable=false`）或闸门不过 |
| 4 | 产物已存在且未给 `--force`（不静默覆盖） |

## 调优点

- 栋数对 `--split-m` 最敏感：6 m 是实测能拆开裙房又不把单栋切碎的值；调到 0 就退回连通域（本模型会合成 1 个 2,800 格的大块）。
- 地面窗口 12/15/18 m 的差异已进 `sensitivity`，**它是方法敏感性区间，不是精度**。
- 09 与 06 的栋数不能直接比：06 是连通域口径，09 是分水岭口径；比就比 `maskCells` 与高度量级。
