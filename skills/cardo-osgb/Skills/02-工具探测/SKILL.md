# 02 · 工具探测

> 回答一个问题：**这台机器上现在这份转换工具，是不是登记过的那一份？**
> 不解析模型、不改工具；只做定位 + 指纹 + 版本字面量 + 许可证在位。

## 何时用

- 每次跑全链前（`run` 的第 2 步）；
- 换机器 / 剪切工作区后；
- 怀疑「工具被替换了」时（`--tools-dir` 指向可疑目录，看是否 exit 3）。

## 执行命令（工作区根）

```bash
python Skills/02-工具探测/scripts/probe_tools.py
python Skills/02-工具探测/scripts/probe_tools.py --tools-dir ../shared/osgb-tools --json
python Skills/02-工具探测/scripts/verify_tools.py --tools-dir Skills/02-工具探测/tools
python Skills/02-工具探测/scripts/fetch_tools.py --from <本机源目录>      # 按清单重建后立即校验
python Skills/02-工具探测/scripts/fetch_tools.py --url <zip 或 http(s)>    # 从压缩包重建
```

## 探测顺序（不静默降级）

1. `--tools-dir` 显式覆盖
2. `Skills/02-工具探测/tools/`（自带，默认）
3. 环境变量 `CARDO_OSGB_TOOLS`
4. 都没有 → `TOOL_MISSING`，**exit 2**

找到后逐项校验，任一不符 → **exit 3**（`TOOL_VERSION_MISMATCH` / `LICENSE_UNVERIFIED`），**绝不"用相近版本试试"**。

## 校验项

| 项 | 口径 | 登记处 |
|---|---|---|
| 可执行存在 | `tools/fanvanzh-3dtiles/v0.4/3dtile.exe` | `规则/外部工具配置.yaml` → `executable` |
| exe sha256 | 全值比对 `C7F5…79DC` | `sha256` |
| 安装目录清单指纹 | 178 个文件的 `路径|字节|sha256` 排序后再 sha256 | `manifest.files / sha256` |
| 版本字面量 | `-V` 输出含 `Make 3dtile program 1.0` | `versionBanner` |
| 许可证副本 | `tools/licenses/fanvanzh-3dtiles-LICENSE.txt` 在位 | `licenseFile`（Apache-2.0，`distribution: internal-only`） |
| 可选校验器 | `3d-tiles-tools 0.5.4`（体积大不随仓库，缺失只降级不失败） | `validator` 段 |

## 输入 / 输出

- 输入：工具目录（按上面顺序解析）+ `规则/外部工具配置.yaml`
- 输出：`过程文件/02-工具探测/工具探测.json`（`cardo/lowair-tools/v1`）：tools[]/validator/checks/licenseVerified/ok/reason/gaps

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 工具在位且全部命中 |
| 2 | 找不到工具目录 / 可执行缺失（`TOOL_MISSING`） |
| 3 | sha256 / 清单指纹 / 版本字面量 / 许可证不符（`TOOL_VERSION_MISMATCH`、`LICENSE_UNVERIFIED`） |

## 调优点

- 选型依据与候选工具矩阵见 `规则/开源工具调研.md`（15 个候选：许可/体积/OSGB 支持/结论）；
  换工具前先读它，再改 `规则/外部工具配置.yaml` 并全链回归。
- 换版本流程：替换 `tools/<vendor>/<version>/` → 改 yaml 的 `version / sha256 / manifest / executable` →
  全链 `run` 回归 → 只在回归通过后更新 `记忆与规则/验证基线-CardoOSGB.json`。
- `--json` 只打印不落盘，适合在 CI 里做前置探测。
- 工具目录**不要**加进系统 PATH；`cwd` 固定为 exe 所在目录（yaml 的 `invocation.cwd: exeDir`）。
