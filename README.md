# cardo-osgb

> 安装（二选一）：
> `dsh plugin --profile <profile> add github:CSCCTang/cardo-osgb#v0.1.3`
> `dsh plugin --profile <profile> add <本地路径>\cardo-osgb-0.1.3.tgz`

低空解析：给一个 OSGB 倾斜模型包，产出 3D Tiles、模型体检报告、现状指标与模型浏览页。

## 需要提供什么

| 输入 | 形态 | 最小样例 |
|---|---|---|
| OSGB 模型包 | 一个 zip，解压后单根目录，含 `metadata.xml` 与 `Data\`；坐标系 EPSG:4544 | 本包不附模型；任选一个单瓦片 OSGB 包即可起步 |
| 工作区 | 一个独立空文件夹 | 新建空目录即可 |

放料方式：把 zip 放进工作区的 `输入文件\01-模型包\`。多个 zip 或坐标系不符会被直接判为不合格并说明原因。

## 装完怎么调

在 dsh 对话里说「用 cardo-osgb 跑，工作区是 `D:\proj\osgb-01`」。

工作区是独立空文件夹：`输入文件\` 放料、`过程文件\` 留痕、`成果文件\` 出活。转换器与模型浏览引擎都在包内，不用另装。

## 会得到什么

`成果文件\`：

- 3D Tiles 模型包
- 模型体检报告（docx）
- 现状指标（xlsx）
- 指标说明（docx）
- 模型浏览页（`Start.bat` 一键打开）

## 许可证与版本

- 版本 `0.1.3`；许可证 **AGPL-3.0-or-later**，见 [LICENSE](LICENSE) 与 [NOTICE](NOTICE)。
- 第三方组件与许可证见 [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md)。
- 运行环境：Windows；Python ≥ 3.11；dsh ≥ 0.2.2-rc.5。

## 文档

- [docs/01_使用流程.md](docs/01_使用流程.md)
- [docs/02_输入准备.md](docs/02_输入准备.md)
- [docs/03_常见失败.md](docs/03_常见失败.md)
