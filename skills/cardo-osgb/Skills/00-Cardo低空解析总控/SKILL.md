# 00 · Cardo 低空解析总控

> 唯一入口：把 `01→10` 串成一条链，每步用**独立子进程**跑该技能的脚本，退出码即闸门。
> 不 import 任何技能脚本（避免跨目录耦合）；日志与报告落 `过程文件/00-总控/`。
> **算法不写死**：`算法注册表.json` 登记 53 个算法（人话说明 + 要什么输入 + 产出什么 + 能否跑），
> 通过 `algos` 查看、`run --select` 交互选择、`run --algos` 直接点名。

## 何时用

- 第一次跑或换模型后重跑：`run`
- 只跑某一段（例如改了导出格式）：`run --from 07 --to 07`
- 想知道「有哪些算法能用、某个算法是干什么的」：`algos`
- 想让用户自己挑算法：`run --select`（勾完自动换算步骤、缺前置自动补跑）
- 到现场前看状态：`status`
- 复制出第二个工作区验证「换模型也能跑」：`scaffold`

## 执行命令（工作区根）

```bash
python Skills/00-Cardo低空解析总控/scripts/pipeline.py run --workspace .
python Skills/00-Cardo低空解析总控/scripts/pipeline.py run --workspace . --from 05 --to 08
python Skills/00-Cardo低空解析总控/scripts/pipeline.py status --workspace .
python Skills/00-Cardo低空解析总控/scripts/pipeline.py scaffold --target ../CardoOSGB-B

python Skills/00-Cardo低空解析总控/scripts/pipeline.py algos                 # 现在能跑的算法（人话）
python Skills/00-Cardo低空解析总控/scripts/pipeline.py algos --all           # 全部，含不可用与原因
python Skills/00-Cardo低空解析总控/scripts/pipeline.py algos --group recon --status ready
python Skills/00-Cardo低空解析总控/scripts/pipeline.py run --select            # 交互勾选
python Skills/00-Cardo低空解析总控/scripts/pipeline.py run --algos recon.ndsm,vector.topology --dry-run
python Skills/00-Cardo低空解析总控/scripts/pipeline.py run --algos recon.ndsm,imagery.3dgs --skip-unavailable
python Skills/00-Cardo低空解析总控/scripts/export_algo_table.py --workspace .   # 导出 算法清单与来源.xlsx
```

`run` 参数：`--tools-dir <工具根>`（传 02/03/05）、`--input-zip <相对路径>`（传 01）、`--force`（01/09/10 覆盖已有产物）、
`--timeout`（单步秒数，默认 1800）。
算法选择参数：`--algos <id 或编号,逗号分隔>`、`--select`（交互）、`--dry-run`（只打印计划）、
`--skip-unavailable`（点到需影像/两期/待实现的算法时跳过并说明）。

## 步骤表

步号与落盘目录**不完全同名**（历史原因：场地要素那步的目录名定得早，后来步骤号又插了扩展指标与能力矩阵两步）。
**以本表为准**；`运行报告.md` 里也会自动生成同一张映射表。

| 步 | 脚本 | 落盘目录 | 关键产物 |
|---|---|---|---|
| 01 | `Skills/01-模型包接入/scripts/unpack_model.py` | `过程文件/01-模型包接入/` | `输入清单.json` + `解包日志.md` |
| 02 | `Skills/02-工具探测/scripts/probe_tools.py` | `过程文件/02-工具探测/` | `工具探测.json` |
| 03 | `Skills/03-OSGB转3DTiles/scripts/convert.py` | `过程文件/03-OSGB转3DTiles/` | `转换日志.json` + `转换产物/` |
| 04 | `Skills/04-垂直配准修正/scripts/fix_registration.py` | `过程文件/04-垂直配准修正/` | `配准修正.json` |
| 05 | `Skills/05-模型体检/scripts/inspect_model.py` | `过程文件/05-模型体检/` | `体检.json` + `模型体检.md` |
| 06 | `Skills/06-2.5D网格指标/scripts/build_indicators.py` | `过程文件/06-2.5D网格指标/` | `现状指标.json` |
| 07 | `Skills/07-成果导出/scripts/export_deliverables.py` | `过程文件/07-成果导出/` | `导出日志.json`（运行结论 status/stage/exitCode + 四件成果的落盘清单与 sha256；失败也写） |
| 08 | `Skills/08-验证与成稿/scripts/independent_verify.py` | `过程文件/08-验证与成稿/` | `验证报告.json` |
| 09 | `Skills/09-三维要素反演/scripts/run_reconstruct.py` | `过程文件/09-三维要素反演/` | `三维要素反演.json` + `要素矢量.geojson` |
| 10 | `Skills/10-矢量入库/scripts/build_vectors.py` | `过程文件/10-矢量入库/` | `拓扑清理.json` + `矢量成果.geojson` |
| 11 | `Skills/11-几何增强/scripts/run_enhance.py` | `过程文件/11-几何增强/` | 地面滤波 / 点云分类 / 单木 / 网格修复 |
| 12 | `Skills/12-屋顶与立面/scripts/run_roof_facade.py` | `过程文件/12-屋顶与立面/` | 屋面分割 + 立面统计 |
| 13 | `Skills/13-矢量增强/scripts/run_vector.py` | `过程文件/13-矢量增强/` | 轮廓矢量化 + GeoPackage |
| 14 | `Skills/16-场地要素/scripts/extract_site_features.py` | `过程文件/16-场地要素/` | `场地要素.json`（几何代理量） |
| 15 | `Skills/14-扩展指标/scripts/build_extended_metrics.py` | `过程文件/14-扩展指标/` | `扩展指标.json` |
| 16 | `Skills/14-扩展指标/scripts/build_observable_matrix.py` | `过程文件/14-扩展指标/` | `可观测类型矩阵.json` |

`run` 默认跑 `01→14`；14 的钩子会自动续跑 15/16 并重跑 07/08（扩展指标要并进成果，边界与基线也得重核）。
日志里 `to` 是**请求范围**，`effectiveTo` 是**实际跑到的最后一步**，两者可能不同（`--to 14` → `effectiveTo 16`）。

## 运行锁

同一工作区**不允许两条流水线并行**：两条 run 会互相覆盖 `过程文件/00-总控/运行日志.json`，
事后分不清哪次是真结果。`run` 入口会在工作区建 `.cardo/run.lock`（记 PID + 启动时间 + 工作区），
已有运行未结束时直接 **exit 3** 并打印：

```text
[00] 已有运行 PID 6856，自 2026-11-01T16:17:13+08:00 起，工作区 <工作区路径>
```

上次崩溃留下的残锁（持锁进程已死）会自动清理，不让工作区永久卡死。


## 算法注册表（`算法注册表.json`）

- schema `cardo/lowair-algo-registry/v1`；每条含 `id/name/group/step/status/default/plain/input/output/source/priority`
  + 来源五字段 `sourceName/sourceLink/license/impl/evidence`；
- `status`：`ready`（可跑）/ `needs-imagery`（缺航摄原片）/ `needs-2epoch`（缺第二期）/ `needs-truth`（缺真值）/ `planned`；
- 不可用的必须写 `reason`：「缺什么、补上就能跑」，不写「不支持」；
- 每条还填 `sourceName/sourceLink/license/impl/evidence`（具体项目或论文、链接、许可、自实现还是调库、落在哪个文件），
  供 `export_algo_table.py` 生成 `<工作区>/算法清单与来源.xlsx`（四表：表内21项对照 / 本链已落地 / 待条件 / 依赖与版本）；
- 人读说明见 [`算法说明.md`](算法说明.md)；加算法的步骤同上（改表 → 实现脚本 → `algos` + `--dry-run` 自检）。

## 闸门与纪律

- **闸门 = 子进程退出码 0**；脚本内部还会做「先删旧产物 → 再生成」和契约/回读/复算自校验，所以不存在「旧文件假通过」。
- 任一步非 0 即停，打印该步 stderr 末段（≤2000 字），并写入 `运行报告.md` 的「失败定位」。
- 日志：`过程文件/00-总控/运行日志.json`（结构 + stdout/stderr 末段）；报告：`过程文件/00-总控/运行报告.md`
  （数值全部摘自各步 JSON，不做二次加工）。
- 路径脱敏：日志与报告中工作区绝对路径统一替换为 `<workspace>`，不落盘符。
- `status` 只读：列每步产物是否就位 + 最近一次运行的逐步退出码。

## 退出码

对外四档。各步脚本自己的 0/2/3/4/5 由 `public_exit()` 折成这四档，
不再一律压成 1——这样"料不对"和"程序崩了"在调用方看来不再同码。

| 码 | 含义 | 处置 |
|---|---|---|
| 0 | 所选步骤全部通过 | — |
| 1 | 某步真失败（转换/回读/验证不通过） | 看 `运行报告.md` 的失败定位；修好后 `--from <该步>` 重跑 |
| 2 | 环境或结构问题（工具缺失/指纹不符、参数或工作区不合法） | 按提示修正 |
| 3 | 料不对 / 待人工介入（缺料、SRS 不合规、布局不合规、模型体检 fail） | 按提示改料，再 `--from <该步>` 重跑 |

`运行日志.json` 增记 `failedStep`（停在哪一步）与 `failedStepExit`（该步自己的退出码），
供上层准确定位，不必再猜。

## 调优点

- 只重跑受影响的段（04 之后：`--from 04`），03 昂贵时避免整链重跑。
- `--tools-dir` 指向临时工具树做「换版本回归」；正式运行前务必回到自带 `tools/`。
- 大模型：03 只在 `.work/` 下生成视图，可删；不要把 `.work/` 拷进交付。
