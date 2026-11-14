# swnx-switch

**中文 | [English](README.md)**

**SolidWorks `.sldprt` ⇄ Siemens NX `.prt` 零件互通：把零件读成 AI 能理解的结构，再做带容差校验的双向格式转换。**


本项目调用本机已安装的 **SolidWorks 2025** 与 **Siemens NX 2406** 的官方 API（COM / NXOpen），
把零件读成中立的 JSON 模型，再通过 **Parasolid `.x_t`（首选）/ STEP（备选）** 在两种原生格式间互转，
**每次转换都自动对比源件与结果并给出 `pass` / `warn` / `fail` 结论**。

> **边界声明（重要）**：本项目不解析闭源的 `.sldprt` / `.prt` 二进制结构，不做特征逆向。
> 交换的是 **B-Rep 几何**（精确解析曲面，不是三角网格），因此**目标端得到的是无参数的实体/导入特征**，
> 参数化特征树、草图、建模历史不会保留。这是 STEP/Parasolid 路线的行业上限，详见
> [docs/limitations.md](docs/limitations.md)。

---

## 目录

1. [这是什么 / 不是什么](#1-这是什么--不是什么)
2. [环境要求](#2-环境要求)
3. [安装与快速开始](#3-安装与快速开始)
4. [命令行参考](#4-命令行参考)
5. [AI 集成：MCP 与 skill](#5-ai-集成mcp-与-skill)
6. [数据模型（IR）](#6-数据模型ir)
7. [转换路线与原理](#7-转换路线与原理)
8. [校验机制](#8-校验机制)
9. [实测结果](#9-实测结果)
10. [已知限制](#10-已知限制)
11. [排错手册](#11-排错手册)
12. [架构与目录](#12-架构与目录)
13. [开发与测试](#13-开发与测试)
14. [社区调研摘要](#14-社区调研摘要)
15. [版本与路线图](#15-版本与路线图)
16. [许可与合规](#16-许可与合规)
17. [参考](#17-参考)

## 1. 这是什么 / 不是什么

| 能做 | 不能做 |
|---|---|
| 读 `.sldprt` / `.prt` 的质量属性、包围盒、实体、特征树、草图、曲面摘要、自定义属性 | 不保留参数化特征树 / 草图 / 尺寸关系（目标端是导入实体） |
| 在 `.sldprt` ⇄ `.prt` 之间转换，几何在 1e-9 量级一致 | 不做特征级"翻译"（Extrude→Extrude 这类重建） |
| 读/导 `.step` / `.x_t`，把中间格式导入成原生件 | 不支持装配语义转换、工程图标注/PMI 的完整传递 |
| 每次转换自动做 15 项容差校验并出中英文报告 | 不解析原生二进制格式、不绕过 CAD 许可证 |
| 通过 MCP 给任意 AI 客户端提供 8 个工具（含异步作业与轮询） | 不是 GUI 软件（有双击菜单，但不是建模界面） |

**设计取舍**：与其"猜特征"（不可靠、也无法验证），不如把 B-Rep 精确搬过去，并诚实报告丢了什么。

## 2. 环境要求

| 项 | 要求 | 本机实测 |
|---|---|---|
| 操作系统 | Windows 10/11 x64 | Windows 11 x64 |
| Python | 3.10+（宿主） | 3.12.10 |
| pywin32 | SolidWorks 驱动必需 | 已安装 |
| SolidWorks | 2020+（COM 自动化），实测 2025 | 2025 SP05（33.5.0.53） |
| Siemens NX | 12 / 2406 / 2506（批处理 `run_journal.exe`），实测 2406 | 2406（`D:\UG NX2406`） |
| 许可证 | 本机已授权可用的 SW / NX | 已验证 |
| 磁盘 | 每次转换临时文件约 1–30 MB，`work/` 可随时清 | — |

Python 侧**零第三方 CAD 依赖**（不需要 OCCT / pythonocc / FreeCAD）。测试可选 `pytest`，
不装也能用 `scripts/run_tests.py` 跑全部 92 项测试。

## 3. 安装与快速开始

### 方式 A：直接用仓库（推荐）

```bat
git clone <this repo> swnx-switch
cd swnx-switch
swnx-switch.cmd probe
```

### 方式 B：装成全局命令

```bat
python -m pip install -e ".[windows]"
swnx-switch probe          :: 任意目录可用
```

### 方式 C：DSH 插件（把工具交给 agent）

按 [plugin/README.md](plugin/README.md) 把 `plugin/` 链接进 DSH profile 的依赖与 bundles，
`pnpm install` 后重启 DSH，agent 即可调用 `swnx_convert_part` 等 6 个工具。

### 方式 D：Skill（告诉 AI 何时用、怎么判读）

[skills/swnx-switch/SKILL.md](skills/swnx-switch/SKILL.md) 是给 AI 的操作手册（工作流、结论判读、
已知坑、失败处理、汇报模板）。装到 skill 目录即可被 DSH 按需加载：

```bat
copy /Y skills\swnx-switch\SKILL.md D:\cad-agents\dsh-skills\swnx-switch\SKILL.md
```

### 第一次使用：双击菜单

双击仓库根 **`swnx-switch-menu.cmd`**（桌面快捷方式「swnx-switch 转换器」），中文菜单：

```
1 环境探测        2 读取零件 → IR + 零件简报
3 转换零件        4 对比两个零件
5 查看已有简报    6 打开输出目录 out        0 退出
```

选文件用 Windows 文件对话框；转换完窗口不会闪退。
（直接双击 `swnx-switch.cmd` 只会打印用法并 `exit 2` —— 它是命令行入口，不是双击程序。）

### 命令行示例

```bat
:: 环境探测
swnx-switch.cmd probe

:: 读 SolidWorks 零件 → IR + 简报（约 8 秒）
swnx-switch.cmd read "D:\path\part.SLDPRT" --out out\part.ir.json
swnx-switch.cmd brief out\part.ir.json

:: SolidWorks → NX（实测 34 秒；自动校验并出报告）
swnx-switch.cmd convert "D:\path\part.SLDPRT" "D:\path\part.prt"

:: NX → SolidWorks
swnx-switch.cmd convert "D:\path\part.prt" "D:\path\part.back.SLDPRT"
```

## 4. 命令行参考

| 命令 | 作用 | 关键参数 |
|---|---|---|
| `probe` | 探测环境与两个 backend | `--json`、`--deep`（真实启动/连接一次，5–20 s） |
| `read <file>` | 读件 → IR JSON | `--out`、`--via nx\|solidworks`（读 STEP/Parasolid 时用哪个应用）、`--keep-app`、`--json` |
| `brief <ir.json>` | IR → 中文零件简报 | `--out` |
| `convert <src> <dst>` | 转换 + 校验 + 报告 | `--mode`、`--step-variant`、`--force`、`--tolerance`、`--no-report`、`--report-dir`、`--keep-app`、`--preflight`、`--drop-intermediate`、`--json` |
| `compare <ref> <cand>` | 对比两个 IR 或两个原生件 | `--tolerance`、`--json` |
| `routes` | 列出支持的转换路线 | `--json` |
| `mcp` | 以 MCP stdio server 运行 | `--workdir`、`--outdir` |

`convert` 现在会在**开始前**校验请求（源=目标、同格式、目标已存在、`--mode` 与目标扩展名冲突），
这些错误在毫秒级返回，不会再让两个 CAD 都启动完才发现（0.2.0）。

**退出码**：`0` 成功（或 verdict 为 pass/warn）；`1` 转换/校验失败；`2` 环境错误（找不到 CAD、缺依赖）。

**环境变量**：

| 变量 | 作用 | 默认 |
|---|---|---|
| `NX_HOME` / `UGII_BASE_DIR` | NX 安装根 | 自动探测注册表 |
| `SWNX_SLDPRT_EXE` / `SOLIDWORKS_EXE` | SLDWORKS.exe 路径 | 自动探测注册表 |
| `SWNX_TIMEOUT` | 单个 CAD 任务超时秒数 | 900 |
| `SWNX_NX_JOURNAL` | 覆盖 NX 驱动脚本 | `tools/nx/driver.py` |
| `SWNX_WORKDIR` / `SWNX_OUTDIR` | 运行目录 / 输出目录 | `<repo>/work`、`<repo>/out` |
| `SWNX_LIVE=1` | 启用需要真机的测试 | 关闭 |
| `SWNX_KEEP_APP=1` | 转换/读件结束后保持 CAD 运行，供下次复用 | 关闭 |
| `SWNX_SW_ATTACH_TIMEOUT` | 启动 SLDWORKS.exe 后等待 COM ROT 的秒数 | 90 |
| `SWNX_SWITCH_HOME` / `SWNX_PYTHON` | 插件定位项目 / Python | 自动 |
| `SWNX_ENABLE_MCP=0` | 加载插件但不注册工具 | 开启 |

## 5. AI 集成：MCP 与 skill

MCP 是接口，插件是投放方式，skill 是操作手册——三者可独立使用：

```bat
:: 任意 MCP 客户端（Claude Desktop / Codex / Cursor）直接接
python -m swnx_switch.ai.mcp_server
```

| 工具 | 入参 | 返回 | 耗时 |
|---|---|---|---|
| `swnx_probe` | `deep?` | 环境 + backend 状态；`deep=true` 时包含 launchable/reachable/last_error | <1 s / 5–20 s |
| `swnx_read_part` | `path`、`via?`、`save_ir?`、`brief?`、`keep_app?` | 摘要 + IR 路径 + 简报（不再回传 IR 全文） | 8–60 s |
| `swnx_convert_part` | `source`、`target`、`mode?`、`step_variant?`、`force?`、`report?`、`tolerance?`、`keep_app?`、`preflight?`、`report_dir?` | verdict + 关键检查 + 目标 sha256 + 实际落盘路径 + 报告路径 | 1–5 min |
| `swnx_convert_start` | 同上 | 立即返回 `job_id`，后台执行 | 毫秒级返回 |
| `swnx_job_status` | `job_id` | state / stage / elapsed_s / 各阶段时间点与 `delta_s`（带 backend 标签）/ 完成后精简结果 | 毫秒级 |
| `swnx_compare_parts` | `reference`、`candidate`、`tolerance?` | 对比结果 | 秒级 |
| `swnx_part_brief` | `ir` | Markdown 简报 | <1 s |
| `swnx_list_routes` | — | 路线表 | <1 s |

长转换建议用 `swnx_convert_start` + `swnx_job_status` 轮询：能看到
`source_read → export → import → target_read → validate → report` 的实时阶段，
不会在同步等待里"卡住几分钟不知道发生了什么"（0.2.0，见 [docs/reliability.md](docs/reliability.md)）。

典型对话（DSH 里装了插件之后）：

> 把 `D:\...\叉形接头.SLDPRT` 转成 NX 的 `.prt`，告诉我几何有没有变。

AI 会依次调 `swnx_probe` →（可选 `swnx_read_part`）→ `swnx_convert_start` → 轮询
`swnx_job_status`，然后汇报 verdict、体积/包围盒偏差、报告路径，并说明"参数化特征未保留"。

## 6. 数据模型（IR）

`swnx.ir/1`，纯 JSON，单位统一 **mm**（SolidWorks 用米，驱动层负责 ×1000；NX 按零件单位）。
下面是真实零件（叉形接头）的 `read` 结果节选，数值来自实跑、字段有删节：

```jsonc
{
  "schema": "swnx.ir/1",
  "source": {
    "path": "D:\\...\\叉形接头.SLDPRT", "format": "sldprt", "size_bytes": 194118,
    "sha256": "9e2390bf…", "app": "SolidWorks 2025", "app_version": "33.5.0.53", "readonly": true
  },
  "model": {
    "name": "叉形接头", "kind": "part", "units": "mm",
    "material": {},
    "mass_properties": {
      "volume_mm3": 461736.370946, "surface_area_mm2": 87883.276607,
      "mass_kg": 0.461736, "density_kg_m3": 1000.0,
      "centroid_mm": [26.304407, -0.193964, 0.0]
    },
    "bounding_box": { "min_mm": [-55, -40, -55], "max_mm": [133.5, 40, 55], "size_mm": [188.5, 80, 110] },
    "bodies": [{ "name": "切割-实体5", "type": "solid", "faces": 25, "edges": 45, "vertices": 20,
                 "volume_mm3": 461736.370946, "area_mm2": 87883.276607 }],
    "features": [{ "name": "Extrude1", "type": "Extrude", "suppressed": false }],   // 本件 45 项，含子特征/文件夹
    "sketches": [{ "name": "Sketch1", "plane": "前视基准面", "entities": 4 }],      // 本件 8 个
    "geometry_digest": {
      "plane":    [{ "kind": "plane", "normal": [0, 0, 1], "area_mm2": 8304.7 }],   // 10 个
      "cylinder": [{ "kind": "cylinder", "radius_mm": 5, "axis": [0, 0, 1] }],      // 10 个：R5/R10/R20/R27.5/R30/R45/R55
      "torus":    [{ "kind": "torus", "area_mm2": 7397.9 }]                          // 5 个
    },
    "custom_properties": { "材料": "45钢", "图号": "SW-NX-001" },                   // 本件 28 项
    "configurations": [{ "name": "默认", "active": true }]
  },
  "diagnostics": { "warnings": [], "limits": [] }
}
```

字段约定：`*_mm`、`*_mm2`、`*_mm3` 一律按单位后缀解读；读不到的字段**不出现**，
同时在 `diagnostics.warnings` / `diagnostics.limits` 里写原因，绝不编数
（例：NX 不暴露惯量 → `limits: ["moments of inertia are not exposed by MeasureBodies"]`）。

`brief` 会把同一份 IR 渲染成中文简报（关键指标表 / 曲面摘要 / 实体表 / 特征树 / 草图 /
自定义属性 / 转换提示 / 警告），可直接塞进模型上下文。

## 7. 转换路线与原理

```
源件 ──源应用导出──▶ .x_t / .step ──目标应用导入──▶ 目标件
  │                                                    │
  └──── 读 IR（源应用 API）── compare(15 项) ── 读 IR（目标应用 API）──┘
```

| 源 | 目标 | 首选路线 | 备选 |
|---|---|---|---|
| `.sldprt` | `.prt` | SW 导出 Parasolid → NX `ufs.Ps.ImportData` 导入 → 另存 | STEP（本机 NX 批处理 STEP 不可用） |
| `.prt` | `.sldprt` | NX 导出 Parasolid → SW `LoadFile4` 导入 → 另存 | STEP |
| 原生 | `.step` / `.x_t` | 源应用直接导出 | — |
| `.step` / `.x_t` | 原生 | 目标应用导入并另存 | — |

**为什么 Parasolid 优先**：两家都是 Parasolid 内核，`.x_t` 交换 B-Rep 不需要重新拟合曲面，
保真最高、且**不需要 STEP 翻译器许可**。本机 NX 2406 批处理的 STEP 翻译器实测不可用
（导出只写元数据、导入 0 体，见 [docs/limitations.md](docs/limitations.md) §3），
所以 `--mode auto` 对经过 NX 的路线走 Parasolid；`--mode step` 可在 STEP 正常授权的机器上强制走 STEP。

**回退规则**：`auto` 按路线表依次尝试；导出产 0 实体或导入后 0 体即判定该路线失败，
换下一条；全部失败则 `fail` 并保留两端 IR、中间文件和日志，不静默降级到 STL/网格。

## 8. 校验机制

每次 `convert` 都做同样的事：读源件 IR → 导出 → 导入 → 读目标件 IR → 逐项对比。
对比双方是**两个 CAD 应用各自报告的数值**，不是对 exchange 文件的文本猜测。

| 检查项 | 默认容差 | 判定 |
|---|---|---|
| IR 结构 / 单位 | 必须合法且为 mm | 必需 |
| 实体数量 | 必须相等 | 必需 |
| 体积 | 相对 1e-3（绝对底 1e-3 mm³） | 必需 |
| 表面积 | 相对 2e-3 | 必需 |
| 包围盒 每轴 / 角点 | 0.05 mm / 0.1 mm（相对 1e-3） | 必需 |
| 质心 | 0.05 mm | 必需（两侧都有时） |
| 质量 / 密度一致性 | 相对 1e-3 / 1e-6 | 必需（有材料时） |
| 面/边数量 | 仅信息 | 可选 |
| 特征树差异 | 仅信息 | 可选 |

**结论语义**：`pass`（必需项全过）/ `warn`（只有可选/信息项异常，例如特征树丢失；
材质密度这类**元数据**不匹配也归入 warn——几何必需项仍全部通过，密度不是形状）
/ `fail`（任一**必需项**失败或**无法确认**）。`unavailable` 不算通过——缺数据的必需检查判 `fail`，
这是刻意设计：不能因为没读到就假装一致。

> 0.2.0 起，`mass`/`density` 是 optional：它们失败只降级为 `warn`，并写明"元数据项不匹配（几何不受影响）"。
> 依据：一次真机转换中 SW 导出的密度属性是 1 而不是 1000，几何 14 项全部 pass。

容差可覆盖：`--tolerance volume=2e-3 --tolerance bbox_abs=0.1`（短名会映射到规范键：
`volume_rel` / `area_rel` / `bbox_abs_mm` / `bbox_rel` / `bbox_corner_abs_mm` / `centroid_abs_mm` /
`mass_rel` / `density_rel`）。

报告：`work/.../validation.md`（中文表格：检查项 / 实测 / 期望 / 偏差 / 容差 / 结论）
+ `report.json`（机器可读）。

## 9. 实测结果

环境：SolidWorks 2025 SP05（33.5.0.53）× NX 2406（`D:\UG NX2406`）× Python 3.12.10 + pywin32。
测试件：`叉形接头.SLDPRT`——1 实体 / 25 面 / 45 特征 / 8 草图；体积 461 736.370946 mm³；
包围盒 188.5 × 80 × 110 mm。

**SolidWorks → NX**（`scripts/verify_e2e.py --mode parasolid`）：

| 检查 | 实测 | 期望 | 偏差 | 结论 |
|---|---|---|---|---|
| 实体数 | 1 | 1 | 0 | pass |
| 体积 mm³ | 461 736.3709456 | 461 736.370946 | −3.7e−07 | pass |
| 表面积 mm² | 87 883.2766 | 87 883.2766 | −4.7e−07 | pass |
| 包围盒 mm | 188.5 × 80 × 110 | 188.5 × 80 × 110 | 0 | pass |
| 质心 mm | 26.3044, −0.1940, 0.0 | 同左 | 9.9e−08 | pass |
| 质量 kg | 0.461736 | 0.461736 | 0 | pass |
| 面/边 | F25 / E45 | F25 / E45 | 0 | pass |
| 特征树 | 2（导入体） | 45（参数化） | −43 | warn（预期） |

耗时 **34.2 s**（SW 导出 7 s + NX 导入 10 s + 两端读件与校验）；产物 `out/e2e/叉形接头.prt`（106 616 B）。

**NX → SolidWorks**（同一脚本的回程）：体积偏差 +3.7e−07 mm³，表面积 +4.7e−07 mm²，
包围盒/质心/面边全部 pass；耗时 **35.2 s**；产物 `out/e2e/叉形接头.roundtrip.SLDPRT`（160 391 B）。

原始证据（可逐项核对、可复跑）：[docs/evidence/README.md](docs/evidence/README.md)
（完整 `e2e-evidence.json` + 两份验证报告 + 两端驱动验收记录）。

### 可靠性改进（0.2.0，来自一次真实使用报告）

一位调用方在真机上连续跑了两次转换，报告了三个 P0 与若干 P1/P2 问题。0.2.0 逐项修掉，
完整对照（现象 → 根因 → 修复 → 验证方式）见 [docs/reliability.md](docs/reliability.md)。摘要：

| 问题 | 0.2.0 的处理 |
|---|---|
| 转换成功，工具却返回 `invalid output: value is not lossless JSON` | 出口统一走 [wire.py](src/swnx_switch/ai/wire.py)：`allow_nan=False` + 非有限数清洗成 `null`；`data` 只带 verdict/关键检查/sha256/报告路径，不再透传完整 comparison |
| 第二次转换静默等 429 s 后失败 | COM 激活退避重试（2/5/10 s）+ 启动后每 2 s 轮询 ROT（默认 90 s，`SWNX_SW_ATTACH_TIMEOUT`），失败提示带日志尾与错误码含义 |
| 连续启停后 SolidWorks 起不来 | `--keep-app` / `keep_app=true` 复用实例；退出后等进程真正消失再静置 1.5 s；连接失败时写 `diagnose.md` |
| 长任务没有进度 | `swnx_convert_start` + `swnx_job_status`（或 CLI 的 stderr 阶段输出）：`source_read → export → import → target_read → validate → report` |
| `probe` 的 available 名不副实 | 拆成 `installed` / `launchable` / `reachable` / `last_error`；`probe --deep` 会真的启动/连接一次 |
| 返回内容过重 | `data` 最小化；IR 全文只在 `ir_path` 文件里，完整对比只在报告文件里 |
| 报告埋在 `work/` 深处 | 在目标文件旁另写一份 `<目标名>.validation.md` / `.json`，并在返回值里给出绝对路径 |
| 目标扩展名与模式冲突要转完才发现 | 入口毫秒级校验源=目标、同格式、已存在、`--mode` 冲突 |
| 预期差异不前置 | 每次转换的返回文本第一行就是"几何 1:1，参数化特征不保留" |

## 10. 已知限制

1. **参数化特征不保留**：目标端是导入实体/哑体，无草图、参数、父子关系（行业上限）。
2. **装配体 / 工程图 / PMI 属 pilot**：能读能导，语义转换只标 `warn`；PMI/公差/2D 标注不承诺。
3. **面/边 ID 会变**：按 `Face37` 选面的下游脚本需重选。
4. **必须有本机授权可用的 SolidWorks / NX**：不逆向格式、不绕过许可、不承诺免安装。
5. **慢**：单次转换 30 s–2 min（NX 批处理与 SW COM 启动是主要成本），同 backend 串行排队。
6. **大件未验证**：>1000 实体装配不在本版验证范围。

完整清单与"已验证 / 未验证"表见 [docs/limitations.md](docs/limitations.md)。

## 11. 排错手册

| 症状 | 原因 | 处理 |
|---|---|---|
| `environment error` / backend unavailable | 找不到 NX/SW 或缺 pywin32 | 设 `NX_HOME` / `SWNX_SLDPRT_EXE`；`pip install pywin32` |
| `pywintypes.com_error -2146959355 CO_E_SERVER_EXEC_FAILURE` | DSH 沙箱拦住了 COM 跨进程启动 | 用 `danger-full-access` 跑，或普通终端跑 CLI |
| NX 日志只有 `failed to initialize UFUN 948792` | NX 读不到许可/用户配置目录 | 同上；沙箱外运行 |
| `no exchange route succeeded` | 两条路线都失败 | 看每次 attempt 的 export/import 日志 |
| `STEP import produced 0 bodies` | 本机 NX STEP 翻译器不可用 | 用 `--mode parasolid`（默认 auto 已优先） |
| `target already exists` | 目标文件已存在 | 加 `--force` |
| 转换成功但 `feature_tree: warn` | 参数化特征未保留 | 这是预期，不是失败 |
| 体积/包围盒偏差过大（>1e-3） | 丢体、单位错、模型本身不同 | 看报告逐项；确认源件 sha256 |
| 第二次转换卡住约 7 分钟才报 `CO_E_SERVER_EXEC_FAILURE` | 旧版：SW 刚退出就再激活，且只做一次 420 s 长等 | 0.2.0 已改为退避重试 + 90 s ROT 轮询；仍失败看该作业目录下的 `diagnose.md` |
| 连续转换后 `SLDWORKS.exe` 启动即退出 | 反复 COM 启停触发单实例/许可释放竞争 | 加 `--keep-app`（或 `keep_app=true`）复用同一实例；必要时手动启动一次 SW |
| `probe` 说 available 但实际起不来 | 旧版 available 只证明 exe/ProgID 存在 | 用 `probe --deep`：会返回 launchable/reachable/last_error |
| `probe --deep` 报 `value is not lossless JSON` | 0.2.0 的出口清洗漏了 `-0.0`（deep 探针 payload 里有 NX 面轴的 `-0.0`） | 0.2.1 已修：`-0.0` 归一为 `0.0`，超 2^53 整数转字符串；`wire.scan()` 做回归 |

**驱动层三个坑（已绕过，勿改回）**：
SolidWorks 不能用 `OpenDoc6` 打开外来 `.prt`/`.x_t`（会打死 COM 服务器，必须
`GetImportFileData` + `LoadFile4`）；NX 批处理 `DexManager.CreateParasolidImporter().Commit()`
是静默 no-op（必须 `ufs.Ps.ImportData`）；NX 批处理 STEP 翻译器本机不可用。

## 12. 架构与目录

```
CLI / MCP / DSH 插件 / skill
        │
   pipeline.py   转换编排：读件 → 导出 → 导入 → 读回 → 校验 → 报告（带阶段进度）
   validate.py   15 项容差对比（纯 stdlib，不需要 CAD）
   report.py     中文 Markdown + JSON 报告
   ir.py         中性模型 + 构造器 + schema 校验
   jobs.py       后台作业表（convert_start / job_status）
   ai/wire.py    工具出口的严格 JSON（拒绝 NaN/Infinity）
        │  Job(JSON) → 子进程 → JobResult(JSON)
   backends/base.py   子进程协议、超时、日志、按 backend 跨进程锁
   backends/nx.py  → tools/nx/driver.py   NXOpen journal（NX 内嵌 Python 3.10）
   backends/sw.py  → tools/sw/driver.py   pywin32 COM（宿主 Python 3.12，keep-app/诊断）
```

| 路径 | 作用 |
|---|---|
| `src/swnx_switch/` | ir / pipeline / validate / report / cli / lock / paths / errors / jobs |
| `src/swnx_switch/ai/` | brief（简报）/ tools（工具注册表）/ mcp_server（stdio）/ wire（严格 JSON） |
| `src/swnx_switch/backends/` | base（协议+锁）/ nx.py / sw.py |
| `tools/nx/driver.py` | NX 批处理 journal（纯 ASCII，3.10 兼容） |
| `tools/sw/driver.py` | SolidWorks COM 驱动（独立进程，失败也写结果 JSON；含 COM 重试/keep-app/diagnose） |
| `plugin/` | DSH 插件包（注册一行 MCP；含 linked 插件的 peer 解析） |
| `skills/swnx-switch/` | 给 AI 的操作手册 |
| `scripts/` | `verify_e2e.py` / `run_tests.py` / `menu.ps1` |
| `tests/` | IR、MCP 协议、严格 JSON、作业、校验器（138 项，无需 CAD） |
| `docs/` | 架构 / 可行性 / 社区调研 / 限制 / 可靠性 / 证据 |

扩展新 CAD：实现 `Backend` 子类（`available` / `app_version` / `_command`）→ 写读 `SWNX_REQUEST`、
写 `SWNX_RESULT` 的驱动脚本 → 在 `format_owner()` / `BackendRegistry` 登记格式 → 加 `--live` 测试。

## 13. 开发与测试

```bat
:: 全量测试（无第三方依赖，138 项）
python scripts\run_tests.py

:: 有 pytest 时
python -m pytest tests -q

:: 真机端到端（会启动 SW 和 NX，约 70 秒；DSH 里需 danger-full-access）
python scripts\verify_e2e.py --mode parasolid --force
```

测试分层：IR 模型、MCP 协议（真起子进程走 JSON-RPC）、严格 JSON 出口、后台作业状态机、
校验器（fixtures 覆盖 pass/warn/fail/unavailable）、两个驱动的离线契约（命令串、env、结果解析）、
以及 `SWNX_LIVE=1` 的真机读件测试。端到端脚本把环境、耗时、文件大小、逐项校验写进
`out/e2e/evidence.json`，README 数字出自那里。

## 14. 社区调研摘要

完整调研（248 行、逐条带出处）见 [docs/research-community.md](docs/research-community.md)，
本机裁决见 [docs/feasibility.md](docs/feasibility.md)。五条直接影响设计的结论：

1. **没有能可靠互转 `.sldprt` ⇄ `.prt` 的开源项目**；开源对 `.sldprt` 只到容器/元数据级
   （cadmpeg、openswx、sldkit），没有可用 B-Rep，更没有 writer。
2. **商业转换器也不写原生格式**：CAD Exchanger 把 SW/NX 列为只读（R），可写的是 STEP/Parasolid/JT/ACIS；
   Datakit 的能力表里没有"特征/参数/草图"这一层。
3. **厂商自己的路线也是中性格式**：Siemens 帮助原文"部件特征和参数无法转换，已转换的模型包含无参数的体"；
   SW 3D Interconnect 虽能直开 NX 2406 的 `.prt`，但只提取 Parasolid 信息，是只读派生而非转换。
4. **Parasolid 是这对软件的最短路径**：两家同内核；开源栈（OCCT）只能 import 不能 export `.x_t`，
   所以必须用厂商自己的导出器——这正是本项目的做法。
5. **"AI 读 CAD 特征"的现实边界**：nxlib / solidworks-mcp / FreeCAD MCP 的共同套路是
   "包官方 API、输出结构化 JSON"；ML 侧（BRepNet/UV-Net/DeepCAD）做 B-Rep 分割/生成，不等于可编辑特征识别。

## 15. 版本与路线图

**0.2.1（当前）**：0.2.1 修掉 `probe --deep` 的唯一遗留报错（出口清洗漏 `-0.0`），并把阶段耗时、实际落盘名、同名旧文件提示、`report_dir` 补齐；138 项测试。\n\n**0.2.0**：在 0.1.0 的双向转换/校验之上，按真实使用报告补可靠性：
严格 JSON 出口与瘦身返回值、异步作业（`convert_start` + `job_status`）、SolidWorks COM 重试与
ROT 轮询、`--keep-app` 实例复用、`diagnose.md`、三态 `probe --deep`、报告就近、入口校验。
131 项自动化测试。

**0.1.0**：IR 读取、双向转换（Parasolid 路线已验证）、15 项容差校验、双语报告、
CLI / 双击菜单 / MCP / DSH 插件 / skill。

**计划中**：装配体 BOM/实例映射；STEP 正常授权环境下的 `--mode step` 验证；
工程图视图/标注的只读提取；批处理（目录级）转换与增量重跑；作业取消。

**明确不做**：特征级重建（没有可验证的方法，商业工具也不承诺）；原生格式逆向；
绕过许可证；把 STL/网格当转换格式。

## 16. 许可与合规

MIT（见 [LICENSE](LICENSE)）。使用本机已授权的 SolidWorks / NX 许可证运行，不绕过任何授权机制，
不逆向原生文件格式。转换产物的知识产权与合规责任在用户侧。NX 批处理会占用一个许可证席位。

## 17. 参考

- 调研与出处：[docs/research-community.md](docs/research-community.md)
- 本机路线裁决：[docs/feasibility.md](docs/feasibility.md)
- 架构：[docs/architecture.md](docs/architecture.md) · 限制：[docs/limitations.md](docs/limitations.md)
- 实测证据：[docs/evidence/](docs/evidence/README.md)
- 同类思路：nxlib（NXOpen 封装）、solidworks-mcp（COM → MCP）、FreeCAD MCP Bridge、
  CAD Exchanger / Datakit（商业转换器的能力边界）
