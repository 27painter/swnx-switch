---
name: swnx-switch
description: 在 SolidWorks (.sldprt) 与 Siemens NX (.prt) 之间读取零件数据、生成给模型看的零件简报、做带容差校验的双向格式转换。当用户说"把这个 sldprt 转成 NX 的 prt""把 NX 零件转成 SolidWorks""读一下这个零件的体积/孔径/特征""核对这两个文件是不是同一个零件"时使用。触发词：sldprt 转 prt、prt 转 sldprt、SW 转 NX、NX 转 SW、零件互转、CAD 格式转换、读零件数据、零件特征、零件对比、swnx-switch；English triggers: convert sldprt to prt, NX to SolidWorks part conversion, SolidWorks NX interoperability, read CAD part features, compare CAD parts, STEP/Parasolid exchange.
---

# swnx-switch：SolidWorks ⇄ NX 零件读取与互转

本技能让 AI 通过本机已安装的 **SolidWorks 2025** 与 **Siemens NX 2406** 读取 `.sldprt` / `.prt`
的数据特征，并在两种原生格式之间做**带容差校验**的双向转换。项目位于
`D:\27painter\Documents\swnx-switch`。

**交换的是 B-Rep 几何，不保留参数化特征树。** 目标端得到导入实体/哑体，这是行业上限，
报告里会标 `feature_tree: warn`，不要把它说成失败，也不要向用户承诺"转过去还能改参数"。

## 1. 先判环境（不要跳过）

有 MCP 工具时先调 `swnx_probe`；没有工具时跑 CLI：

```bat
D:\27painter\Documents\swnx-switch\swnx-switch.cmd probe
```

看两个 backend 是否 `available=true`：

- `available=false` → 停下来，把 hint 转告用户（NX 需 `NX_HOME`，SolidWorks 需 `SWNX_SLDPRT_EXE`）。
- **`available=true` 只证明 exe 与 ProgID 存在，不证明现在能启动**。用户报过"probe 说可用、
  实际 SW 起不来"：要确认可运行，用 `swnx_probe {"deep": true}` 或 `probe --deep`
  （5–20 秒，会真的启动/连接一次），看 `launchable` / `reachable` / `last_error`。
- 在 DSH 沙箱里，启动 SW COM / NX 批处理需要 **danger-full-access**（许可和用户配置在工作区外）。
  命令被沙箱拒绝时按这个解释，不要反复重试同一条。
- 普通终端里直接跑 CLI 没有这层限制。

## 2. 标准工作流

1. **读件**（可选但推荐，尤其用户问"这个零件长什么样/多大/多少孔"）：
   - MCP：`swnx_read_part {"path": "..."}`
   - CLI：`swnx-switch.cmd read "<file>" --out out\a.ir.json`
   - 得到 `*.ir.json`（中性模型）+ 中文零件简报（体积/面积/包围盒/质心/实体/特征树/曲面摘要/自定义属性）。
   - 返回值里只有摘要；IR 全文在 `ir_path` 指向的文件里，需要细节时读文件，别要求工具回传全文。
2. **转换**（长任务优先用异步两段式）：
   - MCP 异步（推荐）：`swnx_convert_start {"source": "<a.sldprt>", "target": "<a.prt>"}`
     拿到 `job_id` 后轮询 `swnx_job_status {"job_id": "..."}`。
     阶段依次是 `source_read → export → import → target_read → validate → report`，
     每 5–10 秒查一次即可，**不要对同一个转换重复 start**。
     阶段项自带 `delta_s`（每段耗时）与 `backend` 标签，直接读出哪一步慢，不用自己做减法。
   - MCP 同步（短任务/客户端不支持轮询）：`swnx_convert_part {...}`。
   - CLI：`swnx-switch.cmd convert "<源码件>" "<目标件>"`（阶段会打到 stderr）。
   - 默认 `--mode auto`：经过 NX 的路线优先 Parasolid `.x_t`（本机 NX 批处理 STEP 翻译器不可用）。
3. **判读结论**（见 §4），把 pass/warn/fail、关键数字和报告路径给用户。
4. 需要复核时用 `swnx_compare_parts`（两个 IR 或两个原生件）。

**一次转换是长任务**：实测单方向 34–35 秒，冷启动可能到 1–2 分钟。工具超时是 30 分钟。
**不要对同一个转换重复发起**，先等结果或看日志。

## 3. 参数与常见用法

| 需求 | 用法 |
|---|---|
| 目标文件已存在 | 加 `--force`（或工具参数 `"force": true`）；默认拒绝覆盖 |
| 指定中间格式 | `--mode step` / `--mode parasolid`；`--step-variant ap242\|ap214\|ap203` |
| 放宽/收紧容差 | `--tolerance volume=2e-3 --tolerance bbox_abs=0.1`（短名会映射到规范键） |
| 只要中间格式 | 目标写 `.step` / `.x_t`，等价于"导出" |
| 只要读数据 | `read`，不要 convert |
| 连续转换多个零件 | 加 `keep_app=true` / `--keep-app`，复用同一个 SolidWorks 实例，避免反复冷启动 |
| 先确认环境能跑再转 | `preflight=true` / `--preflight`，环境不可用时几秒内失败 |
| 报告放哪 | 默认写在目标文件旁 `<目标名>.validation.md`；可用 `--report-dir` / 工具参数 `report_dir` 集中存放 |
| 实际写出的文件名 | 看返回值 `target_written`：SolidWorks 可能把 `.sldprt` 存成 `.SLDPRT`（Windows 不区分大小写，脚本匹配要注意） |
| 同目录同名旧文件 | 转换成功会提示同名不同后缀的文件，别把它当成本次产物 |
| 装配/工程图 | 能读能导，转换只标 `warn`，不要承诺语义正确 |

## 4. 结论怎么判读

工具/CLI 返回 `verdict`：

| 结论 | 含义 | 该怎么说 |
|---|---|---|
| `pass` | 所有必需检查通过 | 可以说"转换成功，几何在容差内一致" |
| `warn` | 只有可选/信息项异常：特征树丢失、面数变化，或**材质密度这类元数据**不匹配（几何必需项仍全过） | 可以说"几何一致；参数化特征/材质元数据未保留" |
| `fail` | 任一必需项失败或无法确认 | 不能说成功；给出失败项 + 日志路径 |
| `unverified` | 源是中间格式，没有独立参照 | 只报告目标件的属性，不做一致性声明 |

检查项：体数、体积、表面积、包围盒每轴+角点、质心、质量/密度一致性、面/边数（info）、特征树（info）。
`unavailable` 不算通过——缺数据时如实说"无法确认"，不要编数字。
正常转换的体积相对偏差应在 1e-9 量级；出现 1e-3 以上偏差先怀疑丢失实体或单位错误。

## 5. 已知坑（本机实测，直接照做）

1. **NX 2406 批处理 STEP 翻译器不可用**：导出只写元数据（0 个 `ADVANCED_FACE`），导入返回 0 体。
   所以经过 NX 的路线走 Parasolid `.x_t`。用户强行要 STEP 时用 `--mode step` 并说明本机限制。
2. **NX 导入 Parasolid 必须 `ufs.Ps.ImportData`**；`DexManager.CreateParasolidImporter().Commit()`
   在批处理里静默不导入。已在驱动里处理，不要把调用改回去。
3. **SolidWorks 不能用 `OpenDoc6` 打开外来 `.prt`/`.x_t`**——会让 COM 服务器崩溃
   （`-2147023170 RPC failed`）。唯一通道是 `GetImportFileData` + `LoadFile4`。已处理。
4. **DSH 沙箱**：workspace-write 下 SW COM 启动失败（`-2146959355 CO_E_SERVER_EXEC_FAILURE`）、
   NX 批处理报 `failed to initialize UFUN 948792`。需要 danger-full-access，或普通终端跑 CLI。
5. **中文路径**没问题；但 `.bat` 必须是纯 ASCII（cmd 按 GBK 解析）。

## 6. 失败怎么处理

1. 看返回里的 `log` 路径（`work/.../log-*.txt`）和 `error` 文本，不要猜。
2. 典型错误对应：
   - `no exchange route succeeded` → 两条路线都失败；看每次 attempt 的 export/import 日志。
   - `STEP import produced 0 bodies` → 本机 NX STEP 限制（坑 1）。
   - `target already exists` → 加 `--force`。
   - `environment error` → 应用没装/没授权，按 hint 修。
3. 失败后保留 `work/` 目录（里面有 request/result/log/中间文件），这是复现和排错的唯一证据。
4. 不要用 STL/三角网格冒充转换；不要手写 STEP 解析去"绕过"CAD 应用。

## 7. 给用户汇报的模板

```
转换：a.sldprt → a.prt
结论：pass（14 项通过，1 项信息性：特征树不保留）
体积：461736.370946 → 461736.3709456 mm³（偏差 3.7e-07）
包围盒：188.5 × 80 × 110 mm（一致）
目标文件：D:\...\a.prt（106,616 B，34 秒）
报告：D:\...\validation.md
注意：目标端是导入实体，参数化特征/草图没有保留。
```

## 8. 环境事实

| 项 | 值 |
|---|---|
| 项目根 | `D:\27painter\Documents\swnx-switch` |
| CLI | `swnx-switch.cmd`（或 `python -m swnx_switch.cli`，需 `PYTHONPATH=<repo>\src`） |
| MCP 工具 | `swnx_probe` / `swnx_read_part` / `swnx_convert_part` / `swnx_compare_parts` / `swnx_part_brief` / `swnx_list_routes` |
| NX | 2406，`D:\UG NX2406`（`NX_HOME`），内嵌 Python 3.10 |
| SolidWorks | 2025 SP05（33.5.0.53） |
| 宿主 Python | 3.12 + pywin32 |
| 测试 | `python scripts\run_tests.py`（92 项，无需 CAD） |
| 端到端 | `python scripts\verify_e2e.py --mode parasolid`（真机，需 1 次授权） |
