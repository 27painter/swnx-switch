# 架构 / Architecture

## 1. 一句话

原生件 → 中性 IR（给 AI 读）→ STEP/Parasolid 中转 → 原生件 → 容差校验。
四个环节各自独立可测，任一环节失败都能定位到具体是哪一步。

## 2. 为什么是这个形状

`sldprt` 和 `prt` 都是**闭源二进制格式**，没有公开规范；逆向解析既不稳定也有法律风险。
所以本项目**不解析原生格式**，而是让每个 CAD 应用自己说话：

| 能力 | SolidWorks | Siemens NX |
|---|---|---|
| 读原生件 | COM（`SldWorks.Application`，pywin32） | NXOpen journal（`run_journal.exe` 批处理） |
| 读几何/质量属性/特征树 | `ModelDoc2` / `Extension` / `Body2` / `Face2` API | `NXOpen.Part` / `NXOpen.Features` / `UF` API |
| 转出中性格式 | STEP / Parasolid 导出器 | STEP（DexManager）/ Parasolid（`ufs.Ps`） |
| 读入中性格式 | STEP / Parasolid 导入器 | STEP（DexManager）/ Parasolid |

两个应用的几何内核都是 **Parasolid**（SolidWorks 是 Parasolid 内核，NX 是 Parasolid 的东家），
所以 B-Rep 在这一对软件之间交换时**不需要重新拟合曲面**——这是本项目能宣称
"几何级无损（到容差内）"的根本原因。STEP 是首选中转格式（双方都自带、可读、带名字/颜色），
Parasolid `.x_t` 是 B-Rep 保真的兜底路线。

## 3. 分层

```
┌────────────────────────────────────────────────────────────────────┐
│ 消费者                                                              │
│   CLI (swnx-switch convert/read/compare/brief)                      │
│   MCP stdio server  → swnx_probe / swnx_read_part / swnx_convert_part│
│   DSH 插件 (plugin/)  → 把上面这条注册成一行 MCP                     │
└───────────────┬────────────────────────────────────────────────────┘
                │
┌───────────────▼────────────────────────────────────────────────────┐
│ pipeline.py  转换编排：读源件 → 导出 → 导入 → 读目标件 → 校验 → 报告  │
│ validate.py  容差对比（纯 stdlib，不需要 CAD）                        │
│ report.py    Markdown/JSON 报告                                      │
│ ir.py        中性文档模型 + 构造器 + schema 校验                      │
└───────────────┬────────────────────────────────────────────────────┘
                │  Job(JSON) → 子进程 → JobResult(JSON)
┌───────────────▼────────────────────────────────────────────────────┐
│ backends/base.py   子进程协议、超时、日志、按后端串行锁               │
│ backends/nx.py     → tools/nx/driver.py    (NXOpen journal, Py3.10) │
│ backends/sw.py     → tools/sw/driver.py    (pywin32 COM, Py3.12)    │
└────────────────────────────────────────────────────────────────────┘
```

## 4. 驱动协议：一次任务 = 一个子进程

`base.Backend.run(job)` 把请求写成 `<workdir>/request-<kind>-<uuid>.json`，导出
`SWNX_REQUEST` / `SWNX_RESULT` / `SWNX_LOG` 环境变量，起子进程，等结果文件。

这样做不是为了好看，是三个约束逼出来的：

1. **NX 没有 COM**。只能用 `run_journal.exe` 起批处理 NX；一次 journal 就是一次进程启动。
2. **SolidWorks COM 会把异常进程一起带走**。COM 调用崩了，转换器不该跟着崩；子进程隔离后
   调用方还能拿到日志并重试。
3. **可调试**。请求、日志、结果都留在磁盘上，AI 和人看到的是同一份证据。

判定规则（和 `dsh-nx` 一致）：**不看退出码**。NX 批处理正常结束会返回 `0xC0000409`，
journal 吞掉异常时退出码仍是 0。所以结论来自结果文件 `ok` + 日志里的硬失败 token
（`General Fault Exception` / `FATAL ERROR` / `_FAIL` / `Traceback ...`）。

## 5. 中性模型（IR）：`swnx.ir/1`

给 AI 看的不是几千个面，而是能支撑决策的字段：

| 段 | 内容 | 用途 |
|---|---|---|
| `source` | 路径、格式、字节数、sha256、应用与版本、只读标记 | 溯源、报告、防止"转的是另一份文件" |
| `model.mass_properties` | 体积 mm³、表面积 mm²、质量 kg、密度、质心、惯量 | 校验的主体 |
| `model.bounding_box` | min/max/size（mm） | 单位错误、漏体、缩放错误的直接证据 |
| `model.bodies[]` | 名称、类型、面/边/点数、单体积/面积、包围盒 | 判断是否丢体、多体拆分错 |
| `model.features[]` | 特征名、类型、抑制状态、父子、参数 | 让 AI 读懂建模意图（参数不参与转换） |
| `model.sketches[]` | 草图名、基准面、实体数、尺寸 | 同上 |
| `model.geometry_digest` | plane/cylinder/cone/sphere/torus 分组的半径/轴/原点/面积 | "这个零件有哪些孔、多大"的可读摘要 |
| `model.custom_properties` | 材料、图号等用户属性 | 工程语义 |
| `diagnostics` | warnings / limits / 耗时 / driver 日志路径 | 诚实性：读不到就写警告，不编数 |

单位约定：**所有 `*_mm` 一律毫米**。SolidWorks API 用米，驱动层负责 ×1000；
NX 按零件单位 mm。IR 里出现 `units: "m"` 会被 schema 校验直接拒绝。

## 6. 转换与校验

```
源件 --(源应用导出)--> out.step/.x_t --(目标应用导入+另存)--> 目标件
  │                                                              │
  └── 读 IR（源应用） ──── compare(容差) ──── 读 IR（目标应用） ──┘
```

- `mode=auto`：先 STEP AP242，失败（导出 0 实体 / 导入 0 体）再 Parasolid。
- 校验项：体数、体积（相对 1e-3）、表面积（2e-3）、包围盒（0.05 mm / 1e-3）、质心（0.05 mm）、
  质量/密度一致性；面对数变化只作为 info（STEP 允许重排面），特征树丢失明确列为 informational。
- 结论：`pass` / `warn` / `fail` / `unverified`（源是中间格式、没有独立参照时）。

## 7. 串行化

NX 一个许可证席位、SolidWorks 一个 COM 会话。`lock.py` 用 `msvcrt.locking` 在
`work/.lock-<backend>` 上加跨进程锁：CLI、MCP 工具、多 agent 同时调用时排队而不是互相踩。
进程崩溃时内核自动释放，不会留死锁。

## 8. 扩展一个新 CAD

1. 实现 `backends/base.Backend` 子类：`available()` / `app_version()` / `_command()`。
2. 写驱动脚本，读 `SWNX_REQUEST`，写 `SWNX_RESULT`，`read` 分支用 `ir.py` 构造器填 IR。
3. 在 `backends/base.py` 的 `format_owner()` / `BackendRegistry.get()` 里登记格式。
4. 加一个 `--live` 测试 + 跑 `scripts/verify_e2e.py`。

## 9. 目录

```
src/swnx_switch/    ir / pipeline / validate / report / cli / lock / paths / errors
src/swnx_switch/ai/ brief（零件简报）/ tools（工具注册表）/ mcp_server（stdio 协议）
src/swnx_switch/backends/ base（协议+锁）/ nx.py / sw.py
tools/nx/driver.py  NXOpen journal（NX 内嵌 Python 3.10 执行）
tools/sw/driver.py  pywin32 COM 驱动（宿主 Python 3.12 执行）
plugin/             DSH 插件包（注册 MCP 一行）
scripts/            verify_e2e.py / run_tests.py
tests/              IR、MCP 协议、校验器（不需 CAD 即可跑）
docs/               架构 / 可行性 / 社区调研 / 限制
```
