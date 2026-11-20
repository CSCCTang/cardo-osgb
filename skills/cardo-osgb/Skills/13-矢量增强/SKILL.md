# 13 · 矢量增强

把 09 的栅格建筑 mask 变成「真实边界轮廓面」，并把 10 的矢量成果写成可直接拖进 GIS
的 GeoPackage。本技能只读 09/10 产物，不重新解析 OSGB，不改 09/10 的任何文件。

## 算法

| 算法 id | 脚本 | 干什么 | 产物 |
|---|---|---|---|
| `vector.polygonize` | `scripts/vector_polygonize.py` | 09 建筑 mask → 等值线轮廓 → Douglas-Peucker 简化 → EPSG:4544 轮廓面 | `建筑轮廓面.geojson` |
| `vector.gpkg` | `scripts/vector_gpkg.py` | 10 的 13 个面要素 → 最小合规 GeoPackage（std lib sqlite3） | `矢量成果.gpkg` |

两个算法互不依赖；`vector.gpkg` 直接读 10 的 `矢量成果.geojson`。

## 输入（全部已有产物）

| 输入 | 路径 | 用途 |
|---|---|---|
| 模型契约 | `过程文件/03-OSGB转3DTiles/转换产物/cardo-lowair-model.json` | `crs.origin`（EPSG:4544 原点） |
| 09 栅格 | `过程文件/09-三维要素反演/栅格/栅格.npz` | `ndsm / observed / cell / x0 / y0`（axis0=+x，axis1=+y，1.0 m/格） |
| 09 反演结果 | `过程文件/09-三维要素反演/三维要素反演.json` | 阈值、mask 诊断、逐栋属性 |
| 09 逐栋属性 | `过程文件/09-三维要素反演/逐栋属性.json` | 栋属性溯源（可选） |
| 10 矢量成果 | `过程文件/10-矢量入库/矢量成果.geojson` | 13 个 OBB 面、EPSG:4544 |

## 产物

目录：`过程文件/13-矢量增强/<slug>/`（`slug = id.replace(".", "-")`）

| 文件 | 内容 |
|---|---|
| `vector-polygonize/vector-polygonize.json` | schema `cardo/lowair-enhance/v1`，含 summary/items/gaps/quality |
| `vector-polygonize/vector-polygonize.md` | 人读报告（方法、结果表、局限） |
| `vector-polygonize/建筑轮廓面.geojson` | FeatureCollection + `crs` 声明 `EPSG:4544` |
| `vector-gpkg/vector-gpkg.json` / `.md` | 同 schema；含 `selfCheckPass` |
| `vector-gpkg/矢量成果.gpkg` | GeoPackage 1.2（13 要素，srs_id=4544） |
| `_done/<slug>.json` | `emit` 自动写；`exitCode=0` 表示可用 |
| `_patches/<slug>.json` | 注册表补丁（id/status/step/impl/evidence/sourceName/sourceLink/license/plain） |

## 用法

```bash
# 两个算法按序跑
python Skills/13-矢量增强/scripts/run_vector.py --workspace .

# 只跑一个
python Skills/13-矢量增强/scripts/run_vector.py --workspace . --only vector.polygonize
python Skills/13-矢量增强/scripts/run_vector.py --workspace . --only vector.gpkg --force

# 单独跑（便于排障）
python Skills/13-矢量增强/scripts/vector_polygonize.py --workspace .
python Skills/13-矢量增强/scripts/vector_gpkg.py --workspace .
```

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 产出且 `usable=true` |
| 2 | 缺输入（栅格/反演/10 矢量/模型契约） |
| 3 | 产出但 `usable=false`（`quality.reasons` 写明原因） |
| 4 | 产物已存在且未给 `--force` |
| `run_vector.py` | 子脚本全 0 → 0；任一非 0 → 1 |

## 依赖与并发纪律

- **只用 stdlib**：`vector.gpkg` 只用 `sqlite3`；`vector_polygonize` 优先用
  `skimage.measure.find_contours`，本机无 numpy/scikit-image 时自动改用内置
  marching squares（同 level=0.5 口径），并把引擎写进 JSON 与 gap。
- **不 pip install、不改 requirements.txt**；不改注册表 / README / 其他技能 / 09、10 产物。
- 不写盘符绝对路径；产物与日志只用工作区相对路径。
- `sys.dont_write_bytecode = True`，不产生 `__pycache__`。

## 已知局限（详见各 md 的 gaps）

- 轮廓沿 1 m 栅格格边呈锯齿，未做直角/45° 规则化。
- 09 只落盘二值 mask，未落盘逐栋 label 栅格；本技能按连通域提轮廓，相邻建筑会粘成一个面
  （`ENH-GAP-002`）；未覆盖的零散 mask 格会形成无 OBB 对应的小面（`ENH-GAP-003`）。
- GeoPackage 的几何是 10 的 OBB，不是真实轮廓；GPKG 只换容器，不改几何。
- 高度/面积均来自几何反演，**无真值校核**，不构成精度声明。
