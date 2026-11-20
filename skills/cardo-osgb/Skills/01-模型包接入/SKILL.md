# 01 · 模型包接入

> 把「一个 zip 或一个目录」变成**安全、ASCII、结构可判**的解包视图 + 可复算清单。
> 只做输入侧的事：不解模型、不转格式、不改原始数据。

## 何时用

- 拿到新模型包，先过这一步；产物 `输入清单.json` 是后面 03/04/05 的唯一输入源。
- 怀疑压缩包有路径穿越 / 中文路径 / 复合 SRS 时，先跑它看 `safety`、`layout`、`metadata`。

## 执行命令（工作区根）

```bash
python Skills/01-模型包接入/scripts/unpack_model.py --zip 输入文件/01-模型包/<模型>.zip
python Skills/01-模型包接入/scripts/unpack_model.py --zip <zip> --scene-id model-02 --force
python Skills/01-模型包接入/scripts/unpack_model.py --dir <已解开的模型目录>
```

参数：`--out`（默认 `过程文件/01-模型包接入`）、`--scene-id`（ASCII 目录名，默认 `model-01`）、
`--max-bytes`（单条目上限）、`--force`（覆盖已有解包目录）。

## 输入 / 输出

| 输入 | 输出 |
|---|---|
| `.zip`（只读）或已解开的目录 | `过程文件/01-模型包接入/解包/<sceneId>/`（`metadata.xml` + `Data/<块>/*.osgb`） |
| | `输入清单.json`（`cardo/lowair-input/v1`）：source/scene/counts/blocks/lodLevels/topLevel/metadata/layout/safety/files[] |
| | `解包日志.md`（人读摘要 + 安全裁决） |

## 闸门（任一不过即非 0 退出）

1. **路径安全**：拒绝绝对路径、`..` 穿越、符号链接、设备名（CON/NUL…）、超限条目；大小写冲突记 renamed；`violations = 0`。
2. **ASCII 归一**：非 ASCII 路径改写成稳定 ASCII 并记进 `renamed`；写入全用 UTF-8（无 BOM）。
3. **布局**：`Data/<Tile>/` 结构存在，`topLevel` 只允许根节点 osgb（体积异常小则提示，不阻断）。
4. **SRS**：只收水平单码 `EPSG:4544`；复合码 `EPSG:4544+5737` 归一为水平码并把垂直码记到 `metadata.vertical`
   （信息码 `SRS_COMPOSITE_NORMALIZED`）。其它水平码 → `SRS_UNSUPPORTED`。
5. **可复算**：`counts.files / bytes / osgb` 与 `files[]` 逐条 sha256 一致（后续步会用它们对账）。

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 解包与校验通过 |
| 2 | 输入缺失/参数不可用/zip 损坏 |
| 3 | 路径安全违规（越界/符号链接/设备名/炸弹） |
| 4 | 输出已存在且未加 `--force` |
| 5 | 布局不合规（多根目录 / 缺 metadata.xml / 缺根瓦片 / 缺 SRSOrigin）或 SRS 不合规（非 EPSG:4544） |

`--check-only`：只读体检模型包，不解包、不落盘，输出 JSON 判决
（`ok` / `code` / `why` / `fix` / `srs` / `vertical` / `root` / `blocks` / `layoutProblems`）。
`开工.py` 的预检直接调它——**判据与实跑同源**，所以"预检说通过"就等于"实跑不会因布局/坐标系被拦"。

| code | 含义 |
|---|---|
| OK | 通过 |
| LAYOUT_NOT_SINGLE_ROOT | 包内不是单一根目录（顶层有多个目录） |
| NO_METADATA | 根目录下没有 metadata.xml |
| SRS_UNSUPPORTED | 水平坐标系不在白名单 |
| LAYOUT_INVALID | 缺根瓦片 / 无 Data 块 / 缺 SRSOrigin |
| UNSAFE_ENTRY | 路径安全违规 |
| EMPTY / TOO_LARGE / BAD_ZIP | 空包 / 超限 / zip 损坏 |

## 调优点

- 中文文件名不是错误：归一后仍可在 `输入清单.json.files[].path` 找回映射。
- 顶层 `Block.osgb` 只有几百字节属正常（块索引），不会被单独转 b3dm（见 05 的 `TILE_NOT_CONVERTED`）。
- 大包解包慢是磁盘问题：`--max-bytes` 只防炸弹，不加速；可先把 zip 放到本地盘。
