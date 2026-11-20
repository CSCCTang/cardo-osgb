# 10 · 矢量入库

> 把 09 的候选轮廓做成**能直接拖进 GIS 的矢量成果**：拓扑清理 + 属性表 + 字段字典 + 入库说明。
> 只读 09 产物，不重算高度、不改原始模型；GPKG 需要 GDAL，本技能明确不做、把方法写清楚。

## 何时用

- 09 跑完、要把建筑候选交给规划/GIS 同事时；
- 需要 Excel 能直接打开的属性表、或需要跟数据一起交付的字段说明时；
- 需要一份「哪些要素被丢了、为什么」的质控记录时。

## 执行命令（工作区根）

```bash
python Skills/10-矢量入库/scripts/build_vectors.py
python Skills/10-矢量入库/scripts/build_vectors.py --min-area-m2 20 --precision 2
python Skills/10-矢量入库/scripts/build_vectors.py --force      # 产物已存在时必须加
```

总控：`python Skills/00-Cardo低空解析总控/scripts/pipeline.py run --algos vector.topology,vector.attrs`

## 输入 / 输出

| 输入 | 输出（默认 `过程文件/10-矢量入库/`） |
|---|---|
| `过程文件/09-三维要素反演/要素矢量.geojson` | `矢量成果.geojson`（清理后，EPSG:4544，外环逆时针闭合） |
| `过程文件/09-三维要素反演/逐栋属性.json`（可选，溯源） | `属性表.csv`（UTF-8 **BOM**，Excel 双击不乱码） |
| — | `字段字典.md`（18 个字段：类型/单位/来源/怎么用） |
| — | `拓扑清理.json`（`cardo/lowair-vector-topo/v1`，逐规则命中数与丢弃清单） |
| — | `入库说明.md`（QGIS/ArcGIS/PostGIS 导入步骤 + GPKG 需 GDAL 的实话） |

## 八条清理规则（详见 `规则/拓扑规则.md`）

| id | 规则 | 处理方式 |
|---|---|---|
| R1 | 坐标精度 | 统一保留 3 位小数（毫米级） |
| R2 | 环闭合 | 首尾不一致就补首点 |
| R3 | 顶点去重 | 删相邻重复点；不足 4 点判退化并丢弃 |
| R4 | 自相交 | 逐段求交，命中即**丢弃**（不做猜测式修补） |
| R5 | 外环方向 | 顺时针翻成逆时针（OGC 右手法则） |
| R6 | 面积阈值与对账 | 鞋带面积 < 阈值丢弃；与 `widthM×heightM` 偏差 >1% 记告警 |
| R7 | 重复要素 | 几何指纹相同 / 质心 <0.05 m 且面积差 <1% → 只留先出现的 |
| R8 | 属性完备 | `id/areaM2/heightP50/floorsEst/confidence` 缺任一即丢弃 |

## 闸门（回读落盘文件再自检，任一不过 → exit 3）

- `拓扑清理.json` 的 `schema == cardo/lowair-vector-topo/v1` 且 `quality.usable = true`；
- `矢量成果.geojson` 的 `schema == cardo/lowair-vector/v1`，**至少 1 个要素**；
- 每个要素：`Polygon`、外环**闭合**、顶点 ≥ 4、**逆时针**、**无自相交**、Geometry 不重复、id 不重复；
- 每个要素属性含 5 个必需键；
- `属性表.csv` 行数 == 要素数。

## 本模型实测

输入 13 个要素 → 保留 **13 个**（丢弃 0、告警 0）；占地 3,200 m²；A/B/C = 3/9/1。
说明 09 产出的几何本身已闭合、无自相交、无重复——10 的价值在「验证 + 属性化 + 交付说明」。

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 清理通过且闸门通过 |
| 2 | 缺 09 的 `要素矢量.geojson`（先跑 09） |
| 3 | 闸门不过（清理后无要素 / 产物自检失败） |
| 4 | 产物已存在且未给 `--force` |

## 为什么不出 GPKG

写 `.gpkg` 要 GDAL/OGR（`ogr2ogr`、`fiona`、`osgeo`），本机默认环境没有，也不在 `requirements.txt` 强求。
`入库说明.md` 给了两条替代路径（`ogr2ogr -f GPKG`、QGIS 导出），且**换容器不改几何与属性**。
