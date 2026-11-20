# 03 · OSGB → 3D Tiles

> 调 `fanvanzh/3dtiles v0.4` 把解包视图转成 b3dm + tileset.json，并**自己扫一遍产物**（不信工具自报）。
> 转换是黑盒，所以边界很清楚：本步不解析 OSGB 字节，只读 b3dm 头 + GLB。

## 何时用

- 01 通过、02 白名单命中之后；
- 想单独重转（改了输入/工具）时：`--from 03`。

## 执行命令（工作区根）

```bash
python Skills/03-OSGB转3DTiles/scripts/convert.py
python Skills/03-OSGB转3DTiles/scripts/convert.py --tools-dir ../shared/osgb-tools
python Skills/03-OSGB转3DTiles/scripts/convert.py --input 过程文件/01-模型包接入/输入清单.json --out 过程文件/03-OSGB转3DTiles
```

## 输入 / 输出

| 输入 | 输出（默认 `过程文件/03-OSGB转3DTiles/`） |
|---|---|
| 01 `输入清单.json`（SRS/SRSOrigin/块/LOD） | `转换产物/`：`tileset.json`（根）+ `Data/<块>/tileset.json` + `Data/<块>/*.b3dm` |
| 02 `工具探测.json`（exe sha256） | `转换日志.json`（`cardo/lowair-convert/v1`）：view / crs / tool / products / gaps / ok |
| `Skills/02-工具探测/tools/…/3dtile.exe` | `.work/in-osgb/`（规范化输入视图，可删） |

调用：`3dtile.exe -f osgb -i <work>/in-osgb -o <out>/转换产物`，`cwd` = exe 目录，超时 600 s。

## 闸门（任一不过 → exit 3）

1. 02 探测通过，且 exe sha256 与 02 登记**完全一致**（拒绝静默换版本）。
2. 转换进程退出码 = 0，且 `转换产物/tileset.json` 存在。
3. 产物自扫：b3dm 数 = 输入块内 osgb 数；磁盘 b3dm 数 = tileset 引用数；引用缺失 0；解析失败 0；
   纹理缺失 0；三角面 > 0。
4. 先删旧：每次运行前 `rmtree(转换产物)` 并删旧 `转换日志.json`（失败不留"上次的成果"）。

## 关键统计（本模型实测）

44 b3dm / 3 tileset.json / `maxContentDepth=8`；589,802 三角面 / 689,435 顶点；
176 图 / 176 材质 / 缺 0 / 8,295,568 B；耗时约 3.5 s；峰值工作集 ~40 MB（`peakMemoryBytes` 采样）；
磁盘放大 1.343×（`diskAmplification` = 产物字节 / 输入视图字节）；
块 `BlockA=31、BlockX=13`，LOD L14–L20。

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 转换与自校验通过 |
| 2 | 缺 01 清单 / 解包视图 / SRSOrigin |
| 3 | 工具不可用、sha256 不符、转换失败、产物不完整 |

## 调优点

- `.work/in-osgb/` 是**规范化视图**（只拷 osgb + metadata），块名与 `Data/<Tile>/` 结构必须保留，否则 3dtile 找不到子块。
- 大模型慢在磁盘 I/O：`.work` 与产物同盘可减半；不要用网络盘。
- 转换日志里的 `stdoutTail/stderrTail` 已脱敏（`<work>/in-osgb`、`<out>/转换产物`），排障时对照 `.work/` 看。
