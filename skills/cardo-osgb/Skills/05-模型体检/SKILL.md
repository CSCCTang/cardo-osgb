# 05 · 模型体检

> 回答「这份转换产物能不能用」：六项判据 H-01…H-06 + `cardo/lowair-model/v1` 契约 + 人读明细。
> 本步**不改** tileset/b3dm（04 已改过），只读复扫；产物位置见下。

## 何时用

- 04 之后；指标提取之前；
- 想拿一份可机读的「模型描述」给下游（06/08 都读 `cardo-lowair-model.json`）。

## 执行命令（工作区根）

```bash
python Skills/05-模型体检/scripts/inspect_model.py
python Skills/05-模型体检/scripts/inspect_model.py --tools-dir ../shared/osgb-tools
python Skills/05-模型体检/scripts/check_contract.py --model 过程文件/03-OSGB转3DTiles/转换产物/cardo-lowair-model.json --json
```

## 输入 / 输出

| 输入 | 输出 |
|---|---|
| 01 `输入清单.json`、02 `工具探测.json`、03 `转换日志.json`、04 `配准修正.json` | `过程文件/03-OSGB转3DTiles/转换产物/cardo-lowair-model.json`（**契约就放在产物根**，artifacts 相对它解析） |
| `转换产物/`（复扫，不信日志自报） | `过程文件/05-模型体检/体检.json`（`cardo/lowair-health/v1`：H 项 + 质量 + 一致性 + 契约自检） |
| | `过程文件/05-模型体检/模型体检.md`、`契约自检.json`、`纹理颜色诊断.json`（逐图）、`纹理按格颜色诊断.json`（逐格） |

> 契约 JSON 放在产物根不是"越界写 03 目录"：`cardo/lowair-model/v1` 的语义就是「模型包的描述文件与
> `tileset.json`、`Data/**.b3dm` 同根」。`check_contract` 硬性要求 artifacts 不含 `..`、且在 model 同目录下真实存在。

## 六项判据（口径见 `规则/体检判据.md`）

| 编号 | 判据 | 本模型 |
|---|---|---|
| H-01 | 文件数一致（b3dm = 块内 osgb；顶层根瓦片 ≤ 4096 B） | pass（45 osgb → 44 b3dm，顶层 392 B） |
| H-02 | 瓦片引用完整（无缺失、无解析失败） | pass（44 条内容） |
| H-03 | LOD 计数（输入 vs 产物逐层比对） | pass（L14–L20 全对齐） |
| H-04 | 纹理缺失 | pass（176 图 / 176 材质 / 缺 0） |
| H-05 | 配准修正量已应用（`Δ平移 == appliedM`） | pass（527.427 m × up） |
| H-06 | 工具版本/完整性（白名单 + 可选全量校验） | **warn**（`3d-tiles-tools` 未装，降级自校验）——**工具范围，不参与 verdict** |

**判据分两个范围**（`scope` 字段）：

| scope | 含哪些 | 是否决定 verdict |
|---|---|---|
| `model` | H-01…H-05（模型自身状态） | **是** |
| `tool` | H-06（工具链 / 可选校验器装没装） | 否，只作备注 |

`verdict` 只由 `model` 范围的判据决定：任一 fail → fail；否则任一 warn → warn；全 pass → pass。
本模型模型判据 H-01…H-05 全 pass → **`verdict = pass`**；H-06 的 warn 记在
`toolNotes` 里，报告的「5.1 工具链备注」单列，**不与模型判据混列**。

为什么这么分：H-06 查的是「可选校验器装没装」，那是**环境**问题不是模型缺陷。
混在一起会让「模型体检 warn」被误读成「模型有问题」——对外交付时这个歧义代价很大。

## 05 的附加一致性（同样硬闸）

C-01 b3dm 数 = 块内 osgb；C-02 修正量 = |SRSOrigin.z|（≤1 cm）；C-03 修正量可复算；
C-04 产物规模与 03 日志一致（04 重写根 tileset 允许 ≤4096 B 格式差）；C-05 exe 与 02 同源。

## 质量与兼容（v1.1 新增，`quality` 块）

不止"文件在不在"，还看**这份几何值不值得算**：

| 项 | 口径 | 本模型 |
|---|---|---|
| 图元 / 法线 / UV 覆盖 | 有 NORMAL / TEXCOORD_0 的 primitive 占比 | 176 / 1.0 / 1.0 |
| 退化三角形 | 重复索引（i0==i1 等）的下界计数与占比 | 0（0.00%） |
| 纹理分辨率 | 从 JPEG SOF / PNG IHDR 头解析（不解码像素） | 128×128 – 1024×1024，4 种规格，中位 262,144 像素 |
| 图像格式 | `images[].mimeType` 分布 | image/jpeg ×176 |
| glTF 扩展 | `extensionsUsed` | `KHR_techniques_webgl`（legacy，需注意查看器兼容） |
| RTC_CENTER / batch | b3dm featureTable | 0 / 44 个瓦片有 BATCH_LENGTH |
| 瓦片重叠 | 两两 x/y IoU（≥0.5 计重叠对） | 40 对（LOD 空间重叠，取 max z 会混层） |
| LOD 分组 | 按文件名 `_L<level>_` 统计瓦片 / 三角面 / 顶点 / 字节 / 面占比 | L19 37.6%、L20 23.0%、L18 21.6% …（金字塔分布） |
| 纹理颜色诊断（Pillow/numpy） | 解码内嵌 JPEG 64×64 缩略图，统计绿/灰/其它占比与色相直方图 | 绿 P50 7.0%（字节加权 11.4%）、灰 P50 56.4%（加权 55.9%） |
| 纹理按格颜色（Pillow+numpy+trimesh+scikit-image） | 朝上三角面重心 UV 采样 → 1.0 m 网格逐格累计绿/灰/其它；绿色斑块先闭运算 + 连通域 ≥8 px 去噪 | 8,958 格 / 353,385 样本：绿 **17.0%**、灰 **60.1%**、其它 22.9%；4,813 个绿斑块；格内绿 P90 43.8% |

> 两项纹理诊断都是 `usage=diagnostic-only / notValidated=true`：**不进 7 条指标**、不映射平面用地面积；
> 明细分别落 `纹理颜色诊断.json`（176 条逐图）与 `纹理按格颜色诊断.json`（逐格）。

`quality.status`：退化率 >30% 或纹理缺失 → fail；>10%、法线/UV <60%、纹理分辨率解析不出 → warn；否则 pass。
**`fail` 会阻断 05（exit 3）**；warn 只写进报告与 `notes`。

## 闸门与退出码

| 码 | 含义 |
|---|---|
| 0 | verdict ∈ {pass, warn} 且契约自检 0 错误、一致性 5/5 |
| 2 | 缺输入 / schema 不符 |
| 3 | H 项 fail / 契约自检不过 / 一致性不过 |

## 调优点

- 只装了转换器也能跑：`3d-tiles-tools` 缺失只把 H-06 降为 warn，不影响其余判据。
- 想复看产物清单：`cardo-lowair-model.json.artifacts[]` 已含每个 b3dm/tileset 的相对路径、字节数、sha256。
- 契约自检失败时先看 `契约自检.json` 的 `errors`（会列出前 5 条缺失文件/字段）。
