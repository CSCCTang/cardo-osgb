# 04 · 垂直配准修正

> 工具把 `root.transform` 平移到「原点在椭球面 h=0」，整体比真实高程低 `|SRSOrigin.z|`。
> 本步沿该点 `up(lat,lon)` 补 **+SRSOrigin.z**，只动平移，不动旋转/缩放。

## 何时用

- 03 转换通过之后（`run` 第 4 步）；
- 单独复核配准：重复执行是**幂等**的（不会二次叠加）。

## 执行命令（工作区根）

```bash
python Skills/04-垂直配准修正/scripts/fix_registration.py
python Skills/04-垂直配准修正/scripts/fix_registration.py --products 过程文件/03-OSGB转3DTiles/转换产物
```

## 输入 / 输出

| 输入 | 输出 |
|---|---|
| 03 `转换日志.json`（`crs.origin` = SRSOrigin XYZ） | `过程文件/04-垂直配准修正/配准修正.json`（`cardo/lowair-registration/v1`） |
| `转换产物/tileset.json`（`root.transform` 16 元） | 改写 `tileset.json` 的 `transform[12..14]`；`.work/04-applied.json` 幂等标记 |

## 算法（可复算）

1. 取 `before = [t12, t13, t14]`（ECEF，米）；
2. ECEF→大地：WGS84（a=6378137, f=1/298.257222101）迭代 8 次得 `lat, lon`；
3. `up = [cosφcosλ, cosφsinλ, sinφ]`；
4. `dz = SRSOrigin.z`；`after = before + dz × up`；
5. `delta = dz × up`，`appliedM = dz`，`offsetM = -dz`（"偏低"记负）；
6. 校验：`dist(before, after) == |dz|`（1e-6）+ `|appliedM − |SRSOrigin.z|| ≤ 0.01`（1 cm）。

## 幂等

- 首次：`state = applied`，写入标记 `.work/04-applied.json`（before/after/dz）。
- 再跑：当前平移 == 标记的 `after` → `state = already-applied`，**不再改动**；
  JSON 仍按「修正前 → 修正后」报告（`before/after` 取标记值，`delta = [0,0,0]`），
  因此 H-05 的「施加位移可复算」在任何一次运行都成立。
- 当前平移既非 `before` 也非 `after`（例如工具重转后又被手工改过）→ **exit 4**，拒绝猜测。

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 修正已应用（或已应用过）且可复算 |
| 2 | 缺 03 日志 / `crs.origin` / tileset |
| 3 | `root.transform` 缺失或长度 ≠ 16；修正量不可复算 |
| 4 | 状态不明（平移量既不等于修正前也不等于修正后） |

## 本模型实测

`appliedM = 527.427007 m`，`lat 30.522356 / lon 104.053506`，
`before → after` 平移变化 `[-110.327, 440.743, 267.867] m`（= 527.427 × up），`recomputeOk = true`。

## 调优点

- 不要在 04 之后再手工改 `tileset.json` 的平移；需要改就重跑 03 全链。
- `axisAngleDeg` 是 `transform` 的 Z 轴列与 `up` 的夹角（本模型 0°，说明工具轴向已对齐）。
- 只按**原点高程**修正；地形起伏造成的局部偏差**不在**本步承诺范围（指标层面已写进 range/gaps）。
