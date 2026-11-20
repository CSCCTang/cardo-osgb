# 第三方依赖与许可证清单（cardo-osgb）

## Python 依赖（requirements.txt）

| 依赖 | 用途 | 许可证 |
|---|---|---|
| openpyxl | 指标 xlsx 导出 | MIT |
| python-docx | 体检报告 docx | MIT |
| numpy | 网格向量化、纹理像素统计 | BSD-3-Clause |
| scipy | 形态学/滤波/连通域/最近邻补洞 | BSD-3-Clause |
| Pillow | 纹理解码与颜色统计 | HPND（BSD 兼容） |
| trimesh | 网格分析（面法线、连通分量、简化） | MIT |
| scikit-image | 分水岭建筑实例化、纹理聚类 | BSD-3-Clause |
| opencv-python | 纹理解码与轮廓加速 | Apache-2.0 |
| PyMuPDF | PDF 页码与页数核对 | AGPL-3.0-or-later |

## 包内二进制资产

| 组件 | 版本 | 用途 | 上游 | 许可证 |
|---|---|---|---|---|
| fanvanzh-3dtiles | v0.4 | OSGB → 3D Tiles 转换 | <https://github.com/fanvanzh/3dtiles> | 见上游仓库 |
| CesiumJS | 1.147.0 | 三维浏览 | <https://github.com/CesiumGS/cesium> | Apache-2.0 |
| Leaflet | 1.9.4 | 二维浏览 | <https://github.com/Leaflet/Leaflet> | BSD-2-Clause |
| Caddy | 2.10.0 | 本地静态服务 | <https://github.com/caddyserver/caddy> | Apache-2.0 |

- 资产位置：`skills/cardo-osgb/assets/viewer/`、`skills/cardo-osgb/Skills/02-工具探测/tools/fanvanzh-3dtiles/`。
- 各组件 LICENSE 文件随资产保留；CesiumJS/Leaflet/Caddy 的登记见 `assets/viewer/许可登记.json`。
- Python 依赖由使用者从 PyPI 安装。
- 四个包统一以 AGPL-3.0-or-later 发布。
