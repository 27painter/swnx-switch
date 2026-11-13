# 可行性备忘 / Feasibility Memo（本机裁决版）

> 结论先行：本机（SW 2025 SP05 + NX 2406）走 **STEP AP242 默认 + Parasolid `.x_t` 兜底** 的 B-Rep 级互转是可行的；
> 特征级互转不可行，原生格式直读不可行，二者都不要写进 README 的承诺里。
> 检索/实测时间：2026-11-13。写入范围：本文件仅记录决策与边界，不重复架构细节（见 `architecture.md`）。

## 1 本机事实（已核实）

| 项 | 值 | 证据 |
|---|---|---|
| SolidWorks | 2025 SP05，`33.5.0.0053`，装在 `D:\Program Files\SOLIDWORKS Corp\SOLIDWORKS` | `SLDWORKS.exe` 版本信息；安装源 `SolidWorks 2025 SP5(64bit)` |
| NX | 2406，装在 `D:\UG NX2406` | 目录内容；文件时间戳 2026-10-31 |
| NX STEP 翻译器 | `STEP203UG/`、`STEP214UG/`（均带 AP242 schema）、`TRANSLATORS/step242/` | 目录实测 |
| NX 其他翻译器 | IGES、VDA、DXFDWG、CATIAV5、UGCATIA、NXCREO、UGTO2D、3DXML、G3D、IFC、ACIS、Revit、Rhino、OBJ | 目录实测 |
| NX JT | 无独立目录；`NXBIN` 内有 `Jt-10.6.0.dll`、`JtTk116.dll`、`libjt2nx.dll` | 目录实测 |
| NX 对 SW 的导入 | 只有配置 `TRANSLATORS/solidworks/swnx.def`（`MODULES_MASK = Solids,Surfaces,Wireframe`）+ `UGII/SWschema/` 模板；**未找到可执行 translator，也未找到 .sldprt 写出器** | 目录实测 |
| SW 对 NX 的直读 | 3D Interconnect 官方矩阵：NX `.prt` 支持 **UG 11 – NX 2406**，带关联更新 | [SW 3D Interconnect](https://help.solidworks.com/2025/english/SolidWorks/sldworks/c_sw_3d_interconnect.htm) |

关键含义：本机两边都"能打开对方的件"只是**派生/提取 B-Rep**，不是转换；SW 帮助原文写明只提取 Parasolid 信息、不取特征（[Unigraphics Files](https://help.solidworks.com/2025/english/Solidworks/sldworks/t_Unigraphics_Files.htm)）。

## 2 推荐路线

| 路线 | 方向 | 导出 | 导入 | 保真 | 用途 |
|---|---|---|---|---|---|
| A（默认） | SW→NX / NX→SW | STEP AP242 | NX `step242` / SW STEP | B-Rep + 装配 + 名字/颜色（+ 部分 PMI） | 首选 |
| B（兜底） | 双向 | Parasolid `.x_t` | 对方 Parasolid 导入 | B-Rep 最高保真；语义少 | A 出现 0 实体/0 体/校验失败时 |
| C（对照） | NX→SW 只读 | — | SW 3D Interconnect 直开 `.prt` | 派生 B-Rep | 交叉校验、人工查看；不进交付 |
| D（不推荐） | 双向 | IGES / STL / JT | 同上 | 曲面/网格，无实体拓扑或无语义 | 仅当客户强制要求 |

选择理由（均有出处，详见 `research-community.md`）：

- STEP AP242 是双方都自带、开放、且唯一能带 PMI/MBD 的通道；NX 本机同时有 `step242` 与带 AP242 schema 的 STEP214 翻译器。
- Parasolid 是 SW 与 NX 共同的几何内核（CAD Exchanger 把两家都归为 Parasolid-based），`.x_t` 是同内核交换 B-Rep，解析曲面不需要重新拟合。
- 开源栈（OCCT）**只能 import Parasolid、不能 export**，所以 `.x_t` 必须由两家自己的导出器产生，这正好是本项目的做法。

## 3 触发与回退规则（给 pipeline 的硬规则）

1. 先 A；若源端导出报告 0 实体，或目标端导入后体数为 0，切 B。
2. 若 A 的校验项（体数、体积、表面积、包围盒、质心）超容差，先重试 A 一次（确认不是导出选项问题），再切 B。
3. 若 B 也失败，判 `fail`，保留两份日志与两端 IR，不静默降级到 STL。
4. STL/网格只允许走"可视化"分支，且报告标 `warn`；厂商也明确说 mesh→B-Rep 不可建模（[CAD Exchanger formats](https://new.cadexchanger.com/formats/)）。
5. 3D Interconnect 直读只用于校验脚本，不允许出现在"交付目标件"路径里。

## 4 README 必须诚实写出的限制

- **特征不保留**：目标端得到 dumb body / 导入实体，无参数、无草图、无父子关系；NX 官方也写"部件特征和参数无法转换"（[Siemens 迁移文档](https://docs.sw.siemens.com/documentation/external/PL20230425446868577/zh-CN/nx_help/nx/2312/nx_help/zh-CN/xid913496/xid913497/xid914134.html)）。
- **PMI 部分支持**：STEP AP242 通道存在，但双方实现不完整；报告只能写"看到了什么"，不能写"保证了什么"。
- **面/边 ID 会变**：目标端拓扑编号与源端无关，下游按 ID 选面的脚本必须重选。
- **颜色/材质/贴图**：颜色与名字通常可过，材质/贴图不保证，报告 `warn`。
- **装配体/工程图**：`.sldasm`、NX 装配、`.slddrw`、NX 图纸属于"能读能导，转换不保证"，报告 `warn`。
- **不解析原生格式**：必须有本机授权且可用的 SW/NX；不承诺免安装、不逆向、不绕过许可。
- **耗时**：NX 批处理启动与 SW COM 首启是主要成本，单次转换分钟级（本机实测范围见 `limitations.md`，不要外推成 benchmark）。
- **原生直读不等于无损**：SW 打开 NX 2406 的 `.prt` 是 Parasolid 派生体，NX 打开 SW 件同样只取 B-Rep（本机 `swnx.def` 的 module mask 只列 Solids/Surfaces/Wireframe）。

## 5 本机待确认项（`未验证`，不要写进 README 承诺）

1. NX 2406 的 `swnx.def` 对应哪个可执行程序、支持哪些 SolidWorks 版本、是否需要单独 license——本机只找到配置，没有 binary。
2. SW 2025 导出/导入 Parasolid 的 schema 版本上下限（帮助页有版本文档，但本次未逐条核对）。
3. SW 3D Interconnect 对 NX 2406 的 PMI/自定义属性可读范围（帮助页只给了通用能力清单）。
4. JT 在本机的实际导出入口与选项（库存在，菜单路径未实测）。
5. NX 2406 是否还支持比 2406 更新的 SW 件（反方向版本上限）。

## 6 对现有文档的一处修正建议

`limitations.md` §4 写"SolidWorks 2025 读 NX 2406 的原生件通常不行（3D Interconnect 有新版本上限）"——与官方矩阵相矛盾：SW 2025 SP05 明确支持 UG 11 – NX 2406。
建议改为："原生直读在版本矩阵内可行，但产出的是 Parasolid 派生体而非可编辑特征，因此不作为转换路线。"
（该文件不在本任务写入范围内，需由负责人修改。）

## 7 验收建议（本机可执行）

1. 用同一零件跑 A、B 两条路线，各生成两端 IR，`validate.py` 对比体数/体积/面积/包围盒/质心。
2. 把路线 C（SW 直开 NX 件）的 IR 作为第三方参照，检查 A/B 的 B-Rep 是否落在同一容差带（仅用于诊断，不作为通过条件）。
3. 三种典型件各一遍：单实体带孔板、多实体、含解析曲面（圆柱/锥/球/环）。薄壁/大装配不在本版。
4. 报告保留每步的中间文件哈希与日志路径，失败可复现；不发布跨机器"精度数字"。

## 8 实测补充（2026-11-13，本项目首轮真机验证）

调研阶段的路线判断被实测修正了三点，记录在此以免后续误读：

1. **STEP 在本机 NX 2406 批处理里不可用**：导出只写元数据（ translators 日志
   `Total number of solids input for this translation : 0`，0 个 `ADVANCED_FACE`；
   AP242 策略甚至不落盘），导入返回 0 体（`Unable to import selected file to work part`）。
   SolidWorks 侧 STEP 正常（86 KB，含 `ADVANCED_FACE`，可回读 1 体）。
   因此"经过 NX 的路线优先 Parasolid"不是偏好，是本机可用性结论。
2. **NX 导入 Parasolid 必须走 UF**：`NXOpen.DexManager.CreateParasolidImporter().Commit()`
   在批处理里是**静默 no-op**（不报错、不导体）；`ufs.Ps.ImportData(path)`（单参数）
   才能真的导进来（1 体 25 面）。这条是 2026-11-13 用独立探针 journal 确认的。
3. **SolidWorks COM 打开外来格式会崩**：`OpenDoc6` 打开 `.prt` / `.x_t` 会让 COM 服务器
   返回 `-2147023170 RPC failed` 并整体死亡（3 个文件 × 隐藏/可见窗口 × 3 次独立实例复现）。
   STEP 的导入走 `GetImportFileData` + `LoadFile4` 是正常的，所以方向选择上：
   SW→NX 已完整验证（体积相对偏差 < 1e-9）；NX→SW 的 Parasolid 导入在 SW 2025 SP05
   的 COM 会话里未打通，需在桌面会话或换 SW 版本复核。
