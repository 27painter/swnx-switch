# 社区与厂商现状调研 / Community & Vendor Landscape

> 主题：SolidWorks `.sldprt` 与 Siemens NX `.prt` 互读、互转、以及"AI 读 CAD 特征"今天到底能做到什么。
> 检索时间：2026-11-13。本机环境：SOLIDWORKS 2025 SP05（`D:\Program Files\SOLIDWORKS Corp\SOLIDWORKS`，`SLDWORKS.exe` 文件版本 33.5.0.0053）、Siemens NX 2406（`D:\UG NX2406`）。
> 标注约定：真实打开并读过的来源直接引用；只拿到标题/摘要、或本机网络无法访问的来源，在句中标注 `未验证`。

## 1 现状总览

### 1.1 一句话结论

没有任何开源项目能可靠地把 `.sldprt` 与 `.prt` 互相转换；能买到/能用的路线只有三条——**厂商自己的 API**、**厂商自带的中性格式翻译器**、**商业转换器**。三条路线交付的都是 **B-Rep 几何 + 装配结构 + 属性 + 部分 PMI**，没有一条交付"可编辑的参数化特征树"。本项目架构里"让每个 CAD 应用自己说话 + 中性格式中转"的选择，与整个行业的实际做法一致。

### 1.2 四条路线对比

| 路线 | 代表 | 读得到 | 写得出 | 特征 | 结论 |
|---|---|---|---|---|---|
| 原生格式逆向解析 | cadmpeg、openswx、sldkit | 容器/元数据/部分几何 | 无 | 无 | 实验性，不可用于生产（见 §2.1） |
| 厂商 API | SolidWorks COM、NXOpen | 完整模型树、质量属性、B-Rep | 原生另存 | 只在各自应用内可编辑 | 本项目采用 |
| 厂商中性格式 | STEP AP242 / Parasolid `.x_t` / JT | B-Rep、名字、颜色 | 同上 | 丢失 | 行业标准做法 |
| 商业转换器 | CAD Exchanger、Datakit、TransMagic 等 | B-Rep、装配、PMI | 中性格式或受限原生 | 丢失 | 贵，但成熟（见 §3） |

### 1.3 原生格式为什么不能直接读

- `.sldprt` 没有公开规范。本机 NX 自带的模板 `D:\UG NX2406\UGII\SWschema\dtk.SLDPRT` 前 8 字节为 `D0 CF 11 E0 A1 B1 1A E1`，即 **OLE2 / Compound File Binary** 容器；容器内部数据布局 Dassault 从未公开。社区只能靠逆向（§2.1）。
- `.prt`（NX）同样是闭源二进制，Siemens 只通过 NXOpen/translator 对外提供读写，不发布格式规范。厂商文档把"转换"定义为走中性格式：NX 帮助明确写"部件特征和参数无法转换。已转换的模型包含无参数的体"（[Siemens NX 帮助：将模型从 SolidWorks 迁移到 NX](https://docs.sw.siemens.com/documentation/external/PL20230425446868577/zh-CN/nx_help/nx/2312/nx_help/zh-CN/xid913496/xid913497/xid914134.html)）。
- 连商业转换器也不写原生格式：CAD Exchanger 的格式矩阵里 SOLIDWORKS 与 Siemens NX 都是 **只读（R）**，可写（RW）的是 STEP / Parasolid / JT / ACIS 等（[CAD Exchanger Supported Formats](https://new.cadexchanger.com/formats/)）。这说明"直接生成可用的 `.sldprt`/`.prt`"在商业上也不是常规能力。

## 2 开源生态

### 2.1 原生 `.sldprt` 读取/逆向

| 项目 | 它能做什么 | 边界 | 链接 |
|---|---|---|---|
| cadmpeg | 声称整理 `.sldprt` 格式规范（`docs/formats/sldprt.md`） | 文档型逆向成果；本机网络打不开 GitHub，内容细节 `未验证` | [cadmpeg](https://github.com/cadmpeg/cadmpeg/blob/main/docs/formats/sldprt.md) |
| openswx | C++20 工具包，宣称无需安装 SolidWorks 即可读 `.SLDPRT/.SLDASM/.SLDDRW`，带零依赖 parser、递归 BOM、Web 检查服务 | 仓库描述聚焦读取与 BOM；是否导出 B-Rep 几何 `未验证` | [schwitters/openswx](https://github.com/schwitters/openswx) |
| sldkit | PyPI 0.3.3，"experimental, source-faithful parser for SolidWorks files"，Rust 核心 + PyO3，做容器识别（OLE2/CFB 与现代 chunk） | 自称实验性；解析基础而非 CAD 语义 | [sldkit on PyPI](https://pypi.org/project/sldkit/) |
| FreeCAD SolidWorks 插件 | FreeCAD addon，宣称打开/导入 `.SLDPRT`、`.SLDASM` 与 Parasolid | 依赖外部转换后端；可用性与许可 `未验证` | [djbarrow/freecad_solidworks_plugin](https://github.com/djbarrow/freecad_solidworks_plugin) |

结论：开源世界对 `.sldprt` 的进展集中在**容器与元数据**（BOM、属性、缩略图），不是可用的 B-Rep，更没有 writer。FreeCAD 本身没有官方 `.sldprt` 导入器（`未验证`，但社区插件存在本身就说明原生支持缺失）。

### 2.2 中性格式与几何内核

- **Open CASCADE (OCCT)**：STEP 处理器支持读写属性（颜色、图层、材料、validation properties、GD&T、保存视图、用户属性），见 [OCCT STEP 用户指南](https://dev.opencascade.org/doc/overview/html/occt_user_guides__step.html)。OCCT 组件页里 **Parasolid 只有 Import，没有 Export**（[OCCT Parasolid Import](https://www.opencascade.com/components/parasolid-import/)），JT 是 Import-Export（[OCCT JT](https://www.opencascade.com/components/jt-exchange/)）。这点很关键：开源栈可以读 `.x_t`，但不能合法导出 `.x_t`。
- **OCCT 的特征识别**：OCCT 产品线单独卖 Catalog-Based Feature Recognition、Canonical Recognition 组件（`components/feature-recognition/`，本机仅见导航，细节 `未验证`），说明"从 B-Rep 反推特征"是独立商业能力，不是内核自带。
- **OCP / pythonocc / CadQuery**：都是 OCCT 的 Python 绑定或其上层；CadQuery 的导入导出走 OCCT 的 STEP/DXF/STL 接口（具体页面 `未验证`）。它们能给 AI 的是 B-Rep 拓扑（face/edge），不是原生特征树。
- **steputils**：纯 Python 的 STEP 文档对象模型（读/写 Part 21 文本），MIT，项目自述处于 planning state（[steputils on PyPI](https://pypi.org/project/steputils/)）。适合做 STEP 文本级检查（比如校验 AP242 里有没有 PMI 实体），不适合当几何内核。

### 2.3 CAD 自动化与"AI 读 CAD"

| 项目 | 作用 | 链接 |
|---|---|---|
| nxlib | "Library and toolkit for software development with the NXOpen Python API"，对 NXOpen 的 Python 封装 | [cfs-energy/nxlib](https://github.com/cfs-energy/nxlib) |
| solidworks-mcp | 用 Python COM 把 SolidWorks 包成 MCP server，供 AI 调用 | [XD4REAL/solidworks-mcp](https://github.com/XD4REAL/solidworks-mcp) |
| FreeCAD MCP Bridge | FreeCAD 的 MCP 桥接 workbench，把 FreeCAD 暴露给 AI 客户端 | [FreeCAD Wiki: Robust MCP Bridge](https://wiki.freecad.org/Robust_MCP_Bridge_Workbench) |
| SolidWorks Document Manager API | Dassault 官方的文档读取 API（读取属性/结构，不建模） | [xarial/codestack](https://github.com/xarial/codestack/tree/master/solidworks-document-manager-api) |

这一类项目与 swnx-switch 最接近：它们的共同套路是**不碰原生格式，包住官方 API，输出结构化 JSON 给 LLM**。差异在于它们大多只覆盖一个 CAD，且不解决"换到另一个 CAD"。

## 3 商业工具

### 3.1 通用转换器

| 工具 | 与本项目相关的点 | 来源 |
|---|---|---|
| CAD Exchanger | SOLIDWORKS / NX 只读，STEP/Parasolid/JT/ACIS 可读写；PMI 分 graphical / semantic / 与 B-Rep 关联三层 | [formats](https://new.cadexchanger.com/formats/) |
| Datakit CrossCad/Plug-in | "UG NX 3D to SOLIDWORKS"：不需要 NX 许可证，转 PRT，映射表覆盖装配、属性、几何、拓扑、线框、网格、PMI（尺寸/注释/符号/文本/气球），**没有特征/历史映射** | [Datakit UG NX 3D to SOLIDWORKS](https://www.datakit.com/cad-convertors/ug-nx-3d-to-solidworks-3d/12-ug-nx-3d-to-solidworks.html) |
| TransMagic PRO | 多 CAD 互转产品线；具体能力页被站点防火墙拦截，细节 `未验证` | [TransMagic](https://transmagic.com/pro-3d-cad-translator/) |
| Theorem Solutions | CADverter 系列（Creo/NX 方向文档存在）；具体 SW↔NX 承诺 `未验证` | [Theorem](https://www.theorem.com/) |
| Elysium / Okino PolyTrans / CoreTechnologie 3D_Evolution | 同类通用转换/修复工具；能力与许可 `未验证` | [Elysium](https://www.elysiuminc.com/) / [Okino](https://www.okino.com/conv/conv.htm) / [CoreTechnologie](https://www.coretechnologie.com/) |
| HOOPS Exchange | 商业 CAD 导入 SDK（含 PMI），常用于自研转换器 | [Tech Soft 3D](https://www.techsoft3d.com/products/hoops/exchange/) |
| Capvidia MBDVidia | 主打 MBD/PMI 转换与校验 | [Capvidia](https://www.capvidia.com/products/mbdvidia) |

### 3.2 它们到底交换什么

- 共同点：**B-Rep 几何 + 装配结构 + 名字/颜色/属性 + 部分 PMI**，没有一家在公开页面承诺把 SolidWorks 特征树翻译成 NX 可编辑特征（反向宣称 `未验证`，即没有找到）。
- Datakit 的 mapping 表是最诚实的"能力清单"：它逐项列出哪些实体能过（Solid/Face/Edge/Loop/Vertex、解析曲面、NURBS 曲线、PMI 文本），**表中没有"特征/参数/草图约束"这一层**。这张表可以直接当作本项目 IR 字段的检查表。
- CAD Exchanger 明确说 mesh→B-Rep 转换"价值很低、不推荐"，强行转换会得到"每个三角面一个 B-Rep 面"的不可建模体（[formats 页脚注](https://new.cadexchanger.com/formats/)）。这为"拒绝 STL 回灌成实体"提供了厂商级依据。

### 3.3 价格与部署

公开价格不多：Datakit 单个插件的页面标价 2000 EUR，年维护 400 EUR（[同上](https://www.datakit.com/cad-convertors/ug-nx-3d-to-solidworks-3d/12-ug-nx-3d-to-solidworks.html)）。CAD Exchanger 按 SDK/Lab 授权，价格需询价（`未验证`）。共同部署形态是 Windows 桌面/服务端 SDK，都不提供"免费开源 CLI"。

## 4 厂商原生路线

### 4.1 SolidWorks 2025 的 3D Interconnect（版本矩阵，已核实）

SOLIDWORKS 2025 SP05 帮助页的完整矩阵（[SOLIDWORKS 3D Interconnect](https://help.solidworks.com/2025/english/SolidWorks/sldworks/c_sw_3d_interconnect.htm)）：

| 格式 | 支持版本 | 关联更新 |
|---|---|---|
| NX `.prt` | **UG 11 – NX 2406** | 有（与 CATIA/Creo/Solid Edge 同组图标） |
| STEP | AP203 / AP214 / AP242 | 无 |
| JT | 8.x, 9.x, 10, 10.1, 10.2, 10.3, 10.5, 10.6, 10.7 | 无 |
| IGES | 到 5.3 | 无 |
| ACIS | R1 – 2025 1.0 | 无 |
| CATIA V5 | R8 – V5-6 R2023 | 有 |
| PTC Creo | Pro/ENGINEER 16 – Creo 10.0 | 有 |
| Solid Edge | V18 – SE 2025 | 有 |
| Inventor | 11 – 2025 | 无 |
| IFC | IFC 2x3, IFC 4 | 无 |

关键点：**本机这套 SW 2025 SP05 官方支持直接打开 NX 2406 的 `.prt`**。但这不等于转换——SW 帮助对 Unigraphics 文件写得很清楚：

> "The Unigraphics translator imports the Parasolid information of a Unigraphics part or assembly into a SOLIDWORKS part or assembly document. Only the Parasolid information is extracted, not the proprietary feature information of a Unigraphics part."
> —— [SOLIDWORKS Help: Unigraphics Files](https://help.solidworks.com/2025/english/Solidworks/sldworks/t_Unigraphics_Files.htm)

也就是说：3D Interconnect 打开 NX 件 = 把 NX 里的 Parasolid B-Rep 拿过来做派生体（derived body），**没有 NX 特征**。这与本项目 limitations.md 里"NX 特征树 → SW 导入实体"的描述一致。反方向（SW→NX）NX 也有对称能力，见 §4.2。

### 4.2 NX 2406 的本机翻译器（实测目录）

在 `D:\UG NX2406` 下实际存在的翻译器目录：

| 目录 | 作用 | 本机证据 |
|---|---|---|
| `STEP203UG/`、`STEP214UG/` | STEP AP203 / AP214 外部翻译器 | 含 `step203ug.exe`、`step214ug.exe`，且**两个目录都带 `ap242_managed_model_based_3d_engineering_mim_lf.*` schema** |
| `TRANSLATORS/step242/` | STEP AP242 外部翻译器 | `step242.exe` + `step242ug.def` |
| `IGES/` | IGES 翻译器 | `iges.exe`、`libiges.dll` |
| `VDA/` | VDAFS 曲面格式 | 目录存在 |
| `DXFDWG/` | DXF/DWG（内含 ACIS 相关 DLL） | `dxfdwg.exe`、`SpaACIS.dll` |
| `CATIAV5/`、`UGCATIA/` | CATIA V5 / V4 | `catiav5.exe`、`catiav4.exe` |
| `NXCREO/` | Creo/ProE 导入 | `nxproe.exe`、`psproe*.dll` |
| `UGTO2D/` | 2D Exchange | `ugto2d.exe` |
| `TRANSLATORS/{3dxml,g3d,ifc,nxacis,revit,rhino,wavefrontobj}/` | 3DXML、G3D、IFC、ACIS、Revit、Rhino、OBJ | 各自 `*.exe` |
| `TRANSLATORS/solidworks/` | **SW → NX 翻译器配置** | 仅 `swnx.def`，文件头写明 "This file contains all user settings for the SW -> NX Translator"，`MODULES_MASK = Solids,Surfaces,Wireframe` |
| `UGII/SWschema/` | SolidWorks 导入 schema/模板 | `dtk.SLDPRT`、`dtk.SLDASM`、`sch_*.s_t`、`dtk_xsl/` |
| `PVTRANS/` | 轻量化/可视化 | `ugtopv.exe`、`tessUG.config` |

其他观察：

- 本机**没有独立的 JT 翻译器目录**；JT 以库形式内建（`NXBIN` 下有 `Jt-10.6.0.dll`、`JtTk116.dll`、`libjt2nx.dll`、`jtpostprocessor.exe`），说明 JT 走 NX 内部导出，不是外部 exe。
- 本机**没有发现任何 `.sldprt` 写出器**（目录/exe 名里没有 sldprt/SolidWorks export），与"NX 只导入 SolidWorks"的定位一致。
- NX 帮助的格式清单里明确列出 SolidWorks、JT、STEP 203/214/242、CATIA、Creo、ACIS、IGES（[Siemens NX: Translating data](https://docs.sw.siemens.com/documentation/external/PL20241101461013487/zh-CN/nx_help/dataexchange/xid458357.html)），并说"可将各 SolidWorks 文件直接导入 NX"（[迁移文档](https://docs.sw.siemens.com/documentation/external/PL20230425446868577/zh-CN/nx_help/nx/2312/nx_help/zh-CN/xid913496/xid913497/xid914134.html)）。
- `swnx.def` 只给了配置文件，**没有找到对应的独立可执行文件**；NX 2406 具体支持哪些 SolidWorks 版本、是否需要额外 license，均 `未验证`。

### 4.3 Parasolid 为什么是这两个软件之间的关键

- CAD Exchanger 对 Parasolid 的说明是："Recommended for data exchange with SolidWorks, SolidEdge, NX, OnShape and other Parasolid-based applications"（[formats](https://new.cadexchanger.com/formats/)）。这句同时确认了 SW 与 NX 都属 Parasolid 系。
- SW 自己的帮助页也把"打开 UG 件"实现为"提取 Parasolid 信息"（§4.1 引文）。
- 因此 `.x_t` 路线的价值不是"又一个中性格式"，而是**同一个几何内核之间交换 B-Rep**：解析曲面（平面/圆柱/圆锥/球/环面/NURBS）不需要重新拟合，装配实例和体结构保留得最好。代价是 PMI、草图、特征、名字这些"非几何语义"基本靠边站。
- 反向约束：OCCT 只有 Parasolid Import，没有 Export（§2.2），所以**开源工具链无法生成 `.x_t`**；要靠 SW/NX 自己的导出器。

### 4.4 STEP AP242 的位置

- 双方都支持：SW 2025 读 AP242（§4.1 矩阵）；NX 2406 本机同时装了 STEP214 翻译器（含 AP242 schema）与独立的 `step242` 翻译器（§4.2）。
- AP242 是唯一同时携带 **B-Rep + 装配 + 颜色/名字 + PMI/MBD** 的开放格式；但双方对 AP242 PMI 的实现都不完整，实际传不传取决于导出选项与版本。NX 帮助把"转换"定义为 B-Rep 传递、特征与参数不可转（§1.3 引文）。
- 结论：**STEP AP242 做默认路线，Parasolid 做几何兜底**，与商业转换器的推荐组合一致（CAD Exchanger 把 STEP 列为通用推荐、Parasolid 列为 Parasolid 系应用间推荐）。

## 5 特征级 vs 几何级

### 5.1 三个保真度档位

| 档位 | 内容 | 谁能做到 |
|---|---|---|
| 几何级 | B-Rep（体/面/边/顶点）、装配结构、单位 | STEP / Parasolid / JT / 所有商业转换器 |
| 语义级 | 上述 + 名字、颜色、材质、自定义属性、PMI（部分） | STEP AP242、3D Interconnect 的附加读取项（SW 帮助列出 reference plane/axis、unconsumed sketches、custom properties、material）、Datakit 映射表 |
| 特征级 | 可编辑的参数化特征树、草图约束、父子关系 | 只有**迁移（migration）**类方案：Teamcenter CMM / NX Migration 等受管流程，且 NX 文档写明"部件特征不一定迁移"（[迁移文档](https://docs.sw.siemens.com/documentation/external/PL20230425446868577/zh-CN/nx_help/nx/2312/nx_help/zh-CN/xid913496/xid913497/xid914134.html)） |

跨 CAD 的特征级互转，在公开资料里**没有可复现的产品**；能买到的是"特征识别"（从 B-Rep 猜孔/槽/型腔），它恢复的是**制造特征标签**，不是原始建模历史与参数。

### 5.2 "AI 读 CAD 特征"今天能做什么

- **API 路线（现实、可靠）**：从 COM/NXOpen 读特征树、草图、质量属性、B-Rep 摘要，整理成 IR。这正是本项目 `swnx.ir/1` 的做法（见 `docs/architecture.md`）。社区同类：solidworks-mcp、FreeCAD MCP、nxlib。
- **B-Rep 上的机器学习（研究、可用但窄）**：BRepNet 用拓扑消息传递在 B-Rep 上做面分类/分割（[arXiv 2104.00706](https://arxiv.org/abs/2104.00706)）；UV-Net 用 UV 网格 + 图网络学习 B-Rep（[arXiv 2006.10211](https://arxiv.org/abs/2006.10211)）；DeepCAD 从 CAD 命令序列学习生成建模序列（[arXiv 2105.09492](https://arxiv.org/abs/2105.09492)）。这些工作的输入输出都是**面/边/命令序列**，不是"在 NX 里可编辑的特征树"。
- **数据集**：ABC（[deep-geometry.github.io/abc-dataset](https://deep-geometry.github.io/abc-dataset/)）、Fusion 360 Gallery（[AutodeskAILab/Fusion360GalleryDataset](https://github.com/AutodeskAILab/Fusion360GalleryDataset)）、以及 CAD 几何深度学习综述（[arXiv 2402.17695](https://arxiv.org/abs/2402.17695)）。它们支撑训练，不解决"换 CAD"。
- **商业特征识别**：CAD Exchanger 的产品横幅直接卖 "DFM checks & feature recognition"（[cadexsoft.com](https://cadexsoft.com/)）；OCCT 也单独卖 feature recognition 组件。这说明"识别特征"是另一门生意，且与格式转换分开卖。

### 5.3 对本项目的含义

AI 能读的"特征"要分层说：

1. 原生特征树（名字、类型、抑制、父子、参数）——只能在各自应用内读，**跨不了 CAD**，但能让 AI 理解设计意图。本项目 IR 的 `model.features[]` 属于这一层，且明确"不参与转换"（见 limitations.md）。
2. 几何摘要（体数、面型分布、孔/圆柱半径、包围盒、质量属性）——跨 CAD 可校验，是转换质量的证据。
3. B-Rep 拓扑图（面/边邻接）——可以喂 GNN 做分割/分类，但需要训练数据和模型，不属于本版范围。
4. 可编辑特征重建——当前没有可信产品；不要承诺。

## 6 对本项目的启示

### 6.1 可直接借鉴的思路清单

1. **坚持"应用自己说话"**：所有成熟方案（商业 + 社区）都不解析原生格式，只走 API / 中性格式。本项目的架构选择已被行业验证。
2. **默认 STEP AP242，兜底 Parasolid `.x_t`**：SW 与 NX 都自带两边导出/导入（本机目录已核实），Parasolid 保 B-Rep 解析精度，STEP 保名字/颜色/PMI 通道。
3. **把 3D Interconnect 当"只读对照组"而不是转换主路径**：本机 SW 2025 官方支持 NX 2406（§4.1），可用于"源端 IR vs SW 直读 IR"的交叉校验；但它产出派生体、需要 SW 在环，不能作为转换交付。
4. **IR 字段照抄 Datakit 映射表**：Solid/Face/Edge/Loop/Vertex、解析曲面、NURBS 曲线、PMI 文本、属性、单位、可见性——这些是商业工具认为"能过"的东西，正好是 IR 应该记录的字段。
5. **拒绝"STL 回灌"**：CAD Exchanger 明确说 mesh→B-Rep 不可建模；本项目应把 STL 标为"仅可视化，不参与往返校验"。
6. **报告里给三档保真度标签**（几何/语义/特征），不要一个 pass 掩盖全部；这与 limitations.md 的诚实边界一致，也是商业工具不说清楚的地方。
7. **用厂商文档原文兜住"不做承诺"的部分**：例如 SW 帮助的 "Only the Parasolid information is extracted, not the proprietary feature information" 可以直接引用在 README，堵住"为什么特征没了"的质疑。
8. **串行 + 子进程隔离**（本项目已有）与社区 MCP 项目的做法一致：NX 无 COM、SW COM 会拖崩进程，子进程 + 结果文件是被验证过的工程模式。
9. **PMI 单独定级**：CAD Exchanger 把 PMI 分为 graphical / semantic / 与 B-Rep 关联三层，本项目可以照此在报告里区分"看到标注"和"标注带语义"。
10. **不要发明 benchmark**：商业工具公开页也不给精度数字；本项目只报"同一零件、同一容差、这次跑出来的差值"，这与现状一致。

### 6.2 明确不要做的事

- 不要写 `.sldprt`/`.prt` 解析器或 writer：社区只有实验性逆向，商业工具也不写原生格式（§1.3、§3.1）。
- 不要承诺跨 CAD 的"特征可编辑"：连 NX 官方迁移都写"不一定迁移"（§5.1）。
- 不要把 3D Interconnect 当"无损转换"：它的 Unigraphics 通道就是 Parasolid 提取（§4.1）。
- 不要用 IGES 做主线：IGES 只传曲面/线框，不传实体拓扑，SW 矩阵里 IGES 只到 5.3（§4.1）。
- 不要在文档里写"SW 2025 读不了 NX 2406"：官方矩阵写的是 UG 11 – NX 2406，现有 limitations.md 的这句话需要修正（`docs/limitations.md` 属其他人的写入范围，见交付消息）。

## 参考链接

厂商与规范：

- [SOLIDWORKS 3D Interconnect（2025 版本矩阵）](https://help.solidworks.com/2025/english/SolidWorks/sldworks/c_sw_3d_interconnect.htm)
- [SOLIDWORKS Help: Unigraphics Files](https://help.solidworks.com/2025/english/Solidworks/sldworks/t_Unigraphics_Files.htm)
- [SOLIDWORKS Help: Opening Third-Party CAD Files](https://help.solidworks.com/2025/english/SolidWorks/sldworks/t_opening_third_party_cad_sw.htm)
- [SOLIDWORKS Help: Additional Information from Third-Party CAD Files](https://help.solidworks.com/2025/english/SolidWorks/sldworks/c_info_supported_thirdparty_cad_files.htm)
- [SOLIDWORKS Help: File Types](https://help.solidworks.com/2025/english/SolidWorks/sldworks/c_File_Types.htm)
- [Siemens NX 帮助：将模型从 SolidWorks 迁移到 NX](https://docs.sw.siemens.com/documentation/external/PL20230425446868577/zh-CN/nx_help/nx/2312/nx_help/zh-CN/xid913496/xid913497/xid914134.html)
- [Siemens NX 帮助：Translating data](https://docs.sw.siemens.com/documentation/external/PL20241101461013487/zh-CN/nx_help/dataexchange/xid458357.html)

商业工具：

- [CAD Exchanger Supported Formats](https://new.cadexchanger.com/formats/)
- [CAD Exchanger SOLIDWORKS](https://cadexchanger.com/solidworks/)
- [Datakit: UG NX 3D to SOLIDWORKS](https://www.datakit.com/cad-convertors/ug-nx-3d-to-solidworks-3d/12-ug-nx-3d-to-solidworks.html)
- [Datakit CrossManager](https://www.datakit.com/en/cross_manager.php)
- [TransMagic PRO 3D CAD Translator](https://transmagic.com/pro-3d-cad-translator/)
- [Theorem Solutions](https://www.theorem.com/)
- [Elysium](https://www.elysiuminc.com/)
- [Okino PolyTrans](https://www.okino.com/conv/conv.htm)
- [CoreTechnologie](https://www.coretechnologie.com/)
- [Tech Soft 3D HOOPS Exchange](https://www.techsoft3d.com/products/hoops/exchange/)
- [Capvidia MBDVidia](https://www.capvidia.com/products/mbdvidia)

开源项目与文档：

- [cadmpeg: SolidWorks .sldprt format spec](https://github.com/cadmpeg/cadmpeg/blob/main/docs/formats/sldprt.md)
- [schwitters/openswx](https://github.com/schwitters/openswx)
- [sldkit on PyPI](https://pypi.org/project/sldkit/)
- [djbarrow/freecad_solidworks_plugin](https://github.com/djbarrow/freecad_solidworks_plugin)
- [FreeCAD Wiki: Robust MCP Bridge Workbench](https://wiki.freecad.org/Robust_MCP_Bridge_Workbench)
- [steputils on PyPI](https://pypi.org/project/steputils/)
- [OCCT STEP User Guide](https://dev.opencascade.org/doc/overview/html/occt_user_guides__step.html)
- [OCCT Parasolid Import](https://www.opencascade.com/components/parasolid-import/)
- [OCCT JT Import-Export](https://www.opencascade.com/components/jt-exchange/)
- [cfs-energy/nxlib](https://github.com/cfs-energy/nxlib)
- [XD4REAL/solidworks-mcp](https://github.com/XD4REAL/solidworks-mcp)
- [xarial/codestack: SOLIDWORKS Document Manager API](https://github.com/xarial/codestack/tree/master/solidworks-document-manager-api)

AI / 机器学习：

- [BRepNet (arXiv 2104.00706)](https://arxiv.org/abs/2104.00706)
- [UV-Net (arXiv 2006.10211)](https://arxiv.org/abs/2006.10211)
- [DeepCAD (arXiv 2105.09492)](https://arxiv.org/abs/2105.09492)
- [Geometric Deep Learning for CAD: A Survey (arXiv 2402.17695)](https://arxiv.org/abs/2402.17695)
- [ABC dataset](https://deep-geometry.github.io/abc-dataset/)
- [Autodesk Fusion 360 Gallery Dataset](https://github.com/AutodeskAILab/Fusion360GalleryDataset)
- [CAD Exchanger MTK（含 feature recognition）](https://cadexsoft.com/)
