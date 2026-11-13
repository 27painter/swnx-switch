# 限制与诚实边界 / Limits

这份文档的目的是让用户在**第一次转换之前**就知道什么能保住、什么保不住。
凡是这里写了"不保留"的，报告里也不会假装通过。

## 1. 参数化特征不保留

转换路线是 **B-Rep 几何 + 中性 exchange 格式**，不是特征翻译。因此：

| 源端 | 目标端实际得到 |
|---|---|
| SolidWorks 特征树（Extrude/Fillet/Hole/Pattern…） | NX 里的一个或多个导入实体（dumb body），特征树只剩"导入"特征 |
| NX 特征树（Block/Extrude/Hole/Pattern…） | SolidWorks 的导入实体（`Imported1`），无建模历史 |
| 草图与草图尺寸 | 不传递（B-Rep 里没有草图） |
| 特征参数、父子关系、抑制状态 | 不传递；IR 里能读到，用于让 AI 理解意图，但不参与转换 |

想要"在目标端继续改参数"的需求，本项目给不了，也不打算用"猜特征"的方式假装给。
这是所有基于 STEP/Parasolid 的互转的共同上限（商业工具亦然，除少数做特征识别的昂贵方案）。

## 2. 工程图与 PMI 是部分支持

- 本版**以零件（.sldprt / .prt）为主**。`.slddrw` / `.sldasm` / NX 图纸属于"能读、能导出，
  但转换不保证"的范围，报告会明确标 `warn`。
- STEP AP242 可以携带 PMI/MBD 标注，但双方对 AP242 PMI 的支持都不完整，
  实际是否传递取决于导出器版本与选项，**不做承诺**。
- 尺寸公差、表面粗糙度、焊接符号这类 2D 标注：不保留。

## 3. 中间格式的可用性（本机实测，很重要）

本项目默认"A 路线 STEP、B 路线 Parasolid 兜底"，但**本机 NX 2406 批处理的 STEP 翻译器不可用**：

| 方向 | 现象 | 证据 |
|---|---|---|
| NX 导出 STEP | 生成的文件只有元数据，`0` 个 `ADVANCED_FACE`；AP242 策略甚至不落盘 | 翻译器日志 `Total number of solids input for this translation : 0`；文件 2.2 KB |
| NX 导入 STEP | 翻译器读完实体摘要后创建 `0` 个体；`Unable to import selected file to work part` | 4 种导入策略全失败 |
| NX 导出/导入 Parasolid | **正常**（导入用 `ufs.Ps.ImportData`，1 体 25 面） | 体积误差 8.6e-10 |

因此 `--mode auto` 对**经过 NX 的路线自动改为 Parasolid 优先**（`.x_t` 是 NX 的内核格式，不需要翻译器许可），
STEP 只作为兜底；SolidWorks 侧的 STEP 导入/导出正常（86 KB / `ADVANCED_FACE` 存在）。
这条限制是**本机许可/翻译器状态**导致的，换一台 STEP 翻译器正常授权的 NX 就只影响速度、不影响正确性；
代码不会因此做假——NX 导出 STEP 失败时会降级为 Parasolid 并在结果里写 warning。

## 4. 几何保真的准确说法

- **B-Rep 是精确的**：STEP/Parasolid 传的是解析曲面（平面/圆柱/NURBS），不是三角网格，
  所以不存在"网格化误差"。体积/面积在数值精度内一致（实测 叉形接头 体积相对偏差 < 1e-6）。
- **面/边 ID 会变**：目标端的面编号、边编号与源端无关；依赖"Face37"的下游脚本必须重选。
- **颜色/外观/材质**：STEP 通常能带颜色与名称，材质与贴图不保证；报告里 `warn`。
- **单位**：驱动层统一换算为 mm；不会出现"英寸零件变毫米"这类缩放错误（校验会先抓住）。

## 5. 不解析原生文件格式

`sldprt` / `prt` 是闭源二进制格式。本项目**不做**逆向解析，只通过
SolidWorks COM / NX NXOpen 这两个官方接口读写，因此：

- 必须有本机安装且可用的 SolidWorks / NX（含许可证）。
- 版本兼容性由厂商决定。SolidWorks 2025 SP05 的 3D Interconnect 官方矩阵支持直接打开
  UG 11 – NX 2406 的 `.prt`（带更新链接），但 SolidWorks 自己的帮助明确说明它只提取
  Parasolid 信息、"不包括专有特征信息"——即原生直开是**只读的 B-Rep 派生**，
  不是转换路线。因此本项目仍然走 exchange，不把"能打开"当成"能转换"。
- 不做"免安装转换"承诺。

## 6. 性能与并发

- 一次 SW ↔ NX 转换通常 **1–5 分钟**：NX 批处理启动就要 20–60 秒，SolidWorks COM 首次启动也要几十秒。
- 实测单方向（含两端各一次启动）：SolidWorks → NX **34 s**；NX → SolidWorks 类似量级。
- 同一时刻只允许一个 NX 任务、一个 SolidWorks 任务（`lock.py` 排队）。多 agent 并发会等待，不会损坏文件。
- 大装配件（>1000 体）不在本版验证范围；内存与时间都会显著上升。

## 7. 已验证 / 未验证

| 能力 | 状态 |
|---|---|
| SolidWorks 零件 → IR（质量属性/包围盒/体/特征/曲面摘要） | 已验证（本机 SW2025 SP05，叉形接头：45 特征 / 25 面 / 8 草图） |
| NX 零件 → IR（同上） | 已验证（本机 NX 2406，probe_part.prt：3 特征 / 7 面） |
| SW → NX（`.sldprt` → `.prt`，经 Parasolid） | 已验证：体积/面积/包围盒/质心/质量/拓扑全部 pass，体积偏差 3.7e-07 mm³，见 `out/e2e/evidence.json` |
| NX → SW（`.prt` → `.sldprt`，经 Parasolid） | 已验证：回程同样 14 项 pass，体积偏差 3.7e-07 mm³，产物 160 KB |
| STEP 路线 | SolidWorks 侧可用（86 KB，含 `ADVANCED_FACE`）；NX 2406 批处理 STEP 翻译器本机不可用（见 §3） |
| Parasolid `.x_t` 路线 | 已验证，且为经过 NX 的首选路线（内核格式，无需翻译器许可） |
| 装配体 / 工程图语义转换 | 未验证（pilot，报告会标 warn） |
| PMI / 公差 / 2D 标注传递 | 未验证（不承诺） |
| 特征级重建（在目标端恢复可编辑特征树） | 未实现（见 §1） |

## 8. 许可证与合规

- 使用本机已授权的 SolidWorks / NX 许可证执行自动化；本项目不绕过任何授权机制。
- NX 批处理会占用一个许可证席位；连续调用会串行等待。
- 转换产物的知识产权归属与合规责任在用户侧。

## 9. 在 DSH / 沙箱里运行的额外要求

SolidWorks COM（跨进程 COM 启动）和 NX 批处理（读 `C:\ProgramData\Siemens` 许可、
写 `%LOCALAPPDATA%\Siemens\NX2406`）都需要工作区之外的访问权限。在 DSH 的
workspace-write 沙箱里直接跑会失败：

| 症状 | 原因 |
|---|---|
| `pywintypes.com_error: -2146959355 CO_E_SERVER_EXEC_FAILURE` | 沙箱拦住了 COM 服务器的跨进程启动 |
| NX 日志只有 `failed to initialize UFUN 948792` | NX 读不到许可文件 / 用户配置目录 |

解决办法：在 DSH 里用 `danger-full-access` 跑转换命令（一次授权一条命令），
或者在普通终端里直接运行 `swnx-switch.cmd convert ...`——本项目本体不依赖 DSH，
沙箱只是 DSH 的组织方式。

### 两个驱动层面的坑（已绕过，勿改回去）

* SolidWorks：`OpenDoc6` 打开外来格式（`.prt`/`.x_t`）会让 COM 服务器崩溃
  （`-2147023170 RPC failed`）。必须走 `GetImportFileData` + `LoadFile4`。
* NX：`DexManager.CreateParasolidImporter().Commit()` 在批处理里静默不导入；
  必须用 `ufs.Ps.ImportData(path)`（单参数）。
