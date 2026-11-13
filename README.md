# swnx-switch

**SolidWorks `.sldprt` ⇄ Siemens NX `.prt` 零件互通：先让 AI 读懂零件，再做带容差校验的格式转换。**

一个 Windows 上的开源工具 + MCP 工具层：调用本机 **SolidWorks 2025** 与 **Siemens NX 2406**
的官方 API，把零件读成一份中立的 JSON 模型（质量属性、包围盒、实体、特征树、几何特征摘要），
再通过 STEP AP242 / Parasolid `.x_t` 在两种原生格式之间互转，**每次转换都自动对比源件与结果并给出 pass/warn/fail 结论**。

> 本项目不解析闭源的 `.sldprt` / `.prt` 二进制结构，不做特征逆向。
> 它交换的是 **B-Rep 几何**（精确曲面，不是网格），因此**目标端得到的是无参数的实体/导入特征**。
> 这条边界在 [docs/limitations.md](docs/limitations.md) 里逐条写清。

---

## 1. 它做什么

| 能力 | 命令 / 工具 | 说明 |
|---|---|---|
| 环境探测 | `swnx-switch probe` | 找到 NX 安装根、SolidWorks 版本、pywin32、两个 backend 是否可用 |
| 读零件 → 中性模型 | `swnx-switch read part.sldprt` | 一次读出质量属性/包围盒/实体/特征树/草图/曲面摘要/自定义属性，落成 `*.ir.json` |
| 给 AI 看的零件简报 | `swnx-switch brief part.ir.json` | Markdown 简报：这个零件多大、多少孔、半径多少、有哪些特征 |
| 双向转换 | `swnx-switch convert a.sldprt b.prt` | STEP AP242 优先、Parasolid 兜底；自动导入目标应用并另存 |
| 容差校验 | `swnx-switch compare ref.ir.json out.ir.json` | 15 项检查 + 整体结论 + Markdown/JSON 报告 |
| 给 agent 用 | `swnx-switch mcp` | MCP stdio server，6 个工具；DPH 插件见 [plugin/](plugin/) |

## 2. 快速开始

### 不想敲命令：双击图形菜单

双击仓库根的 **`swnx-switch-menu.cmd`**（桌面已建快捷方式「swnx-switch 转换器」），
出现中文菜单，选文件用 Windows 文件对话框，跑完窗口不会闪退：

```
1 环境探测        2 读取零件 → IR + 零件简报
3 转换零件        4 对比两个零件
5 查看已有简报    6 打开输出目录 out        0 退出
```

> 为什么直接双击 `swnx-switch.cmd` 会"打不开"：它是纯命令行入口，不带子命令时只打印用法并
> 立刻退出（`exit 2`），双击时窗口一闪就没了。交互请用上面的菜单；自动化请按下面在终端里带子命令跑。

### 命令行（脚本/自动化）

```bat
:: 0) 不安装也能跑，仓库根的启动器会自己设 PYTHONPATH
swnx-switch.cmd probe

:: 1) 读 SolidWorks 零件（约 20-60 秒，取决于 SW 是否已启动）
swnx-switch.cmd read  "D:\path\part.SLDPRT" --out out\part.ir.json

:: 2) 看 AI 简报
swnx-switch.cmd brief out\part.ir.json

:: 3) SolidWorks -> NX（1-5 分钟；自动导出 STEP、导入 NX、另存 .prt、校验）
swnx-switch.cmd convert "D:\path\part.SLDPRT" "D:\path\part.prt"

:: 4) NX -> SolidWorks
swnx-switch.cmd convert "D:\path\part.prt" "D:\path\part.back.SLDPRT"
```

`convert` 成功时退出码 0，并打印每条路线的耗时和逐项检查；失败时退出码 1，日志与中间文件都留在 `work/` 下。

## 3. 支持路线

| 源 | 目标 | 路线 |
|---|---|---|
| `.sldprt` | `.prt` | SolidWorks 导出 Parasolid `.x_t` → NX 用 `ufs.Ps.ImportData` 导入 → 另存 `.prt` |
| `.prt` | `.sldprt` | NX 导出 Parasolid `.x_t` → SolidWorks 打开 Parasolid → 另存 `.sldprt` |
| `.sldprt` / `.prt` | `.step` / `.x_t` | 源应用直接导出 |
| `.step` / `.x_t` | `.sldprt` / `.prt` | 目标应用导入并另存 |
| 任意两者 | 对比 | 读两端 IR → `validate.compare` |

`--mode auto`（默认）对**经过 NX 的路线优先 Parasolid**，因为本机 NX 2406 批处理的 STEP
翻译器不可用（导出只有元数据、导入 0 体，见 [docs/limitations.md](docs/limitations.md) §3）；
Parasolid 是 NX 的内核格式，不需要翻译器许可，几何保真也最高。SolidWorks 侧的 STEP
导入/导出正常，`--mode step` 可以在 STEP 正常授权的机器上强制走 STEP。

> 想换成 STEP 优先也很简单：`choose_exchanges()` 是一张显式的路线表，
> `--mode step` 直接锁定 STEP，代码里没有硬编码的"只能 Parasolid"。

## 4. AI 怎么"读图纸数据"

读出来的不是一堆三角面，而是能支撑下一步决策的结构。`read` 产出的 IR（节选）：

```jsonc
{
  "schema": "swnx.ir/1",
  "source": { "format": "sldprt", "path": "...", "sha256": "…", "app": "SOLIDWORKS", "app_version": "33.5.0.53" },
  "model": {
    "name": "叉形接头", "kind": "part", "units": "mm",
    "mass_properties": { "volume_mm3": 31415.9, "surface_area_mm2": 6283.2, "mass_kg": 0.246, "centroid_mm": [0,0,0] },
    "bounding_box": { "min_mm": [-40,-20,0], "max_mm": [40,20,25], "size_mm": [80,40,25] },
    "bodies": [{ "name": "Body1", "type": "solid", "faces": 42, "edges": 96, "volume_mm3": 31415.9 }],
    "features": [{ "name": "Extrude1", "type": "Extrude", "parameters": { "depth_mm": 25 } }],
    "geometry_digest": {
      "plane": [{ "kind": "plane", "area_mm2": 1200.0, "normal": [0,0,1] }],
      "cylinder": [{ "kind": "cylinder", "radius_mm": 8, "axis": [0,0,1], "area_mm2": 1256.6 }]
    },
    "custom_properties": { "材料": "45钢", "图号": "SW-NX-001" }
  },
  "diagnostics": { "warnings": [], "limits": [] }
}
```

`brief` 把同一份数据渲染成中文 Markdown 简报（关键指标表 / 几何特征摘要 / 特征树 / 草图 /
自定义属性 / 转换提示 / 警告），直接喂给模型即可，不必让它读原始 CAD 文件。

## 5. 转换质量怎么保证

每次 `convert` 都做同样的事：**读源件 IR → 导出 → 导入 → 读目标件 IR → 逐项对比**。
对比的双方是"SolidWorks 自己说的"和"NX 自己说的"，不是对 STEP 文本的猜测。

| 检查项 | 默认容差 | 说明 |
|---|---|---|
| 实体数量 | 必须相等 | 丢体是最严重的转换错误 |
| 体积 | 相对 1e-3 | B-Rep 精确交换，正常远小于容差 |
| 表面积 | 相对 2e-3 | 同上 |
| 包围盒（每轴 / 角点） | 0.05 mm / 0.1 mm | 抓刚性偏移、旋转错位、单位缩放 |
| 质心 | 0.05 mm | 体积与包围盒都对但质心错 → 拓扑被改 |
| 质量 / 密度一致性 | 相对 1e-3 / 1e-6 | 抓材料读取错误 |
| 面/边数量 | 仅 info | STEP 允许重排或切分面，不作为失败条件 |
| 特征树 | 仅 info | 几何转换不保留参数化历史，明确报告丢失了哪些特征 |

整体结论：`pass`（全部必需项通过）/ `warn`（只有可选或信息项异常）/ `fail`（任一必需项失败或无法确认）。
"无法确认"不算通过——缺字段的必需检查会判 `fail` 并写进报告。

## 6. 社区与厂商现状（借鉴了什么）

完整调研（248 行、带出处）见 [docs/research-community.md](docs/research-community.md)，本机裁决见
[docs/feasibility.md](docs/feasibility.md)。五条直接影响了本项目的结论：

1. **没有能可靠互转 `.sldprt` ⇄ `.prt` 的开源项目**。开源对 `.sldprt` 的进展集中在容器/元数据
   （cadmpeg、openswx、sldkit），不是可用的 B-Rep，更没有 writer。
2. **商业转换器也不写原生格式**：CAD Exchanger 把 SOLIDWORKS / NX 都列为只读（R），可写的是
   STEP / Parasolid / JT / ACIS。它们交付的是 B-Rep + 装配 + 属性 + 部分 PMI，没有可编辑特征。
3. **厂商自己的路线也是中性格式**：Siemens 帮助直接写"部件特征和参数无法转换，已转换的模型包含无参数的体"。
   SolidWorks 3D Interconnect 虽能打开 NX 件（2025 SP05 支持 UG 11 – NX 2406），但帮助原文说明
   只提取 Parasolid 信息、"不包括专有特征信息"——那是只读派生，不是转换。
4. **Parasolid 是这一对软件的最短路径**：两家都是 Parasolid 内核，`.x_t` 交换 B-Rep 不需要重新拟合曲面；
   而开源栈（OCCT）只能 import Parasolid、不能 export，所以 `.x_t` 必须由两家自己的导出器产生
   ——这正是本项目的做法。
5. **"AI 读 CAD 特征"的现实边界**：nxlib / solidworks-mcp / FreeCAD MCP 这类项目的共同套路是
   "包住官方 API、输出结构化 JSON 给 LLM"；ML 侧（BRepNet/UV-Net/DeepCAD）做的是 B-Rep 分割与生成，
   不等于可编辑特征识别。所以本项目选"官方 API + 中立 IR"，而不是"猜特征"。

## 7. 限制（先说清楚）

- **参数化特征不保留**：目标端得到导入实体/哑体，没有草图、参数、父子关系。
- **工程图 / 装配 / PMI 是部分支持**：以零件为主；`.slddrw`、`.sldasm`、NX 图纸能读能导，转换仅标 `warn`。
- **面/边 ID 会变**：下游按 `Face37` 选面的脚本必须重选。
- **必须有本机授权可用的 SolidWorks / NX**：不逆向格式、不绕过许可、不承诺免安装。
- **慢**：NX 批处理与 SW COM 启动是主要成本，单次转换通常 1–5 分钟，会话内按 backend 串行排队。

逐条细节与"已验证 / 未验证"清单见 [docs/limitations.md](docs/limitations.md)。

## 8. 安装

### 方式 A：直接用仓库（推荐先试这个）

```bat
git clone <this repo> swnx-switch
cd swnx-switch
swnx-switch.cmd probe
```

依赖：Windows + Python 3.10+ + `pywin32`（`pip install pywin32`）+ 本机 SolidWorks / NX。
不需要安装任何第三方 CAD 库。

### 方式 B：`pip install -e .` 后得到 `swnx-switch` 命令

```bat
python -m pip install -e ".[windows]"
swnx-switch probe
```

### 方式 C：作为 DSH 插件（把工具直接交给 agent）

```bat
cd plugin && node bin\swnx-switch.mjs doctor
```

按 [plugin/README.md](plugin/README.md) 把 `plugin/` 链接进 DSH profile 的依赖与 bundles，
重启 DSH 后 agent 就能直接调用 `swnx_read_part` / `swnx_convert_part` 等工具。
任意 MCP 客户端也可以直接 `python -m swnx_switch.ai.mcp_server`（stdio）。

### 方式 D：加一个 skill（让 agent 知道何时用、怎么读结论）

[skills/swnx-switch/SKILL.md](skills/swnx-switch/SKILL.md) 是给 AI 的操作手册：先 probe、
再 read/brief/convert、怎么判读 pass/warn/fail、已知坑、失败怎么处理、汇报模板。
装到 skill 目录即可被 DSH 按需加载（本机已装到 `D:\cad-agents\dsh-skills\swnx-switch\`）：

```bat
copy /Y skills\swnx-switch\SKILL.md D:\cad-agents\dsh-skills\swnx-switch\SKILL.md
```

插件给"能力"，skill 给"判断力"，两者可以独立使用。

## 9. MCP 工具

| 工具 | 用途 |
|---|---|
| `swnx_probe` | 环境探测；`available=false` 时不要继续 |
| `swnx_read_part` | 读文件为 IR（+ 简报），STEP/Parasolid 会先导入到 `via` 指定的应用 |
| `swnx_convert_part` | 转换 + 校验（长任务，1–5 分钟，别重复发起） |
| `swnx_compare_parts` | 对比两个 IR 或两个原生件 |
| `swnx_part_brief` | 把 IR 渲染成零件简报 |
| `swnx_list_routes` | 列出支持的路线（不需要 CAD） |

## 10. 架构与目录

分层、驱动协议、IR 字段、串行化与扩展方式见 [docs/architecture.md](docs/architecture.md)。

```
src/swnx_switch/          ir / pipeline / validate / report / cli / lock / paths
src/swnx_switch/ai/       brief（简报）/ tools（工具注册表）/ mcp_server（stdio）
src/swnx_switch/backends/ base（子进程协议+锁）/ nx.py / sw.py
tools/nx/driver.py        NXOpen journal（NX 内嵌 Python 3.10）
tools/sw/driver.py        pywin32 COM 驱动（宿主 Python 3.12）
plugin/                   DSH 插件包（注册 MCP 一行）
scripts/                  verify_e2e.py / run_tests.py
tests/                    IR、MCP 协议、校验器（无需 CAD）
docs/                     架构 / 可行性 / 社区调研 / 限制
```

## 11. 开发与测试

```bat
:: 不需要任何第三方测试库
python scripts\run_tests.py

:: 有 pytest 时
python -m pytest tests -q

:: 真机端到端（会启动 SolidWorks 和 NX，1-5 分钟）
python scripts\verify_e2e.py
```

端到端脚本会把环境、每步耗时、中间文件大小、逐项校验结果写到 `out/e2e/evidence.json`，
README 里的实测数字就来自那份证据，可以自己复跑核对。

## 12. 已验证环境

| 项 | 值 |
|---|---|
| OS | Windows 11 x64 |
| SolidWorks | 2025 SP05（33.5.0.53），`D:\Program Files\SOLIDWORKS Corp\SOLIDWORKS` |
| Siemens NX | 2406，`D:\UG NX2406`（NXOpen 内嵌 Python 3.10） |
| 宿主 Python | 3.12.10 + pywin32 |
| 测试零件 | `叉形接头.SLDPRT`（1 体 / 25 面 / 45 特征；体积 461 736.37 mm³；包围盒 188.5 × 80 × 110 mm） |

### 实测记录（`scripts/verify_e2e.py --mode parasolid`）

SolidWorks → NX：

| 检查 | 实测 | 期望 | 偏差 | 结论 |
|---|---|---|---|---|
| 体数 | 1 | 1 | 0 | pass |
| 体积 mm³ | 461 736.3709456 | 461 736.370946 | -3.7e-07 | pass |
| 表面积 mm² | 87 883.2766 | 87 883.2766 | -4.7e-07 | pass |
| 包围盒 mm | 188.5 × 80 × 110 | 188.5 × 80 × 110 | 0 | pass |
| 质心 mm | 26.3044, -0.xx, 55.0 | 同左 | 9.9e-08 | pass |
| 质量 kg | 0.461736 | 0.461736 | 0 | pass |
| 面/边 | F25 / E45 | F25 / E45 | 0 | pass |
| 特征树 | 2（导入体） | 45（参数化） | -43 | warn（预期，几何转换不保留特征） |

单方向耗时 **34 s**（两端各启动一次应用；SW 导出 7 s + NX 导入 10 s，其余为读件与校验）。
产物：`out/e2e/叉形接头.prt`（106 616 B）、报告 `work/e2e/.../validation.md`、证据 `out/e2e/evidence.json`。

NX → SolidWorks（同一次 `verify_e2e.py` 的回程）：

| 检查 | 实测 | 期望 | 偏差 | 结论 |
|---|---|---|---|---|
| 体数 | 1 | 1 | 0 | pass |
| 体积 mm³ | 461 736.3709456 | 461 736.370946 | +3.7e-07 | pass |
| 表面积 mm² | 87 883.2766 | 87 883.2766 | +4.7e-07 | pass |
| 包围盒 mm | 188.5 × 80 × 110 | 同左 | 0 | pass |
| 质心 mm | 同左 | 同左 | 9.9e-08 | pass |
| 面/边 | F25 / E45 | F25 / E45 | 0 | pass |

耗时 **35 s**，产物 `out/e2e/叉形接头.roundtrip.SLDPRT`（160 391 B）。
两条方向合计 15 项检查，14 项 pass、1 项 info-warn（特征树不保留，预期内）。

原始证据归档在 [docs/evidence/](docs/evidence/README.md)（完整 JSON + 两份中文验证报告 +
SolidWorks/NX 各自驱动的验收记录），可以逐项核对，也可以按里面的命令自己复跑。

### 已知问题（诚实记录）

* **本机 NX 2406 批处理的 STEP 翻译器不可用**（导出只有元数据、导入 0 体），所以经过 NX
  的路线默认走 Parasolid `.x_t`（NX 内核格式，无需翻译器许可）。SolidWorks 侧 STEP 正常
  （86 KB，`ADVANCED_FACE` 存在，可回读 1 体）。换一台 STEP 正常授权的 NX，`--mode step` 即可。
* **SolidWorks 2025 SP05 用 `OpenDoc6` 打开外来格式（`.prt` / `.x_t`）会让 COM 服务器崩溃**
  （`-2147023170 RPC failed`，3 个文件 × 2 种窗口状态 × 独立实例复现）。本项目改为
  `GetImportFileData` + `LoadFile4` 通道后，Parasolid 导入正常（本次实测 1 体、体积一致）。
  这条坑已在 `tools/sw/driver.py` 里注释记录，后续不要改回 `OpenDoc6`。
* NX 的 DEX `CreateParasolidImporter().Commit()` 在批处理里是静默 no-op，必须用
  `ufs.Ps.ImportData(path)`——同样已写进 `tools/nx/driver.py` 的注释与测试。

<!-- VERIFY-RESULTS -->

## 13. 参考

- 调研与出处汇总：[docs/research-community.md](docs/research-community.md)
- 本机路线裁决：[docs/feasibility.md](docs/feasibility.md)
- 架构：[docs/architecture.md](docs/architecture.md) · 限制：[docs/limitations.md](docs/limitations.md)
- 同类思路：nxlib（NXOpen 封装）、solidworks-mcp（COM → MCP）、FreeCAD MCP Bridge、
  CAD Exchanger / Datakit（商业转换器的能力边界）

## License

MIT（见 `LICENSE`）。使用本机已授权的 SolidWorks / NX 许可证运行，不绕过任何授权机制。
