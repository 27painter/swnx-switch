# 可靠性改进记录 / Reliability notes (0.2.0)

来源：一份真实调用方的使用报告（2026-11-14，桌面 `swnx-switch使用体验与改进建议报告.md`）。
报告里有三个 P0、四个 P1、三个 P2。本文逐条记录**现象 → 根因 → 修复 → 如何验证**，
并明确哪些部分是自动化测试覆盖的、哪些依赖现场环境。

## 1. P0-1 转换成功了，工具层却返回 `invalid output`

**现象**：`swnx_convert_part` 返回
`returned invalid output: value is not lossless JSON`，而 `.prt` 已落盘、`validation.md` 是 PASS。

**根因**：宿主用"无损 JSON"规则校验工具返回值；`NaN` / `Infinity` 不是 JSON。
`ai/mcp_server.py` 与 `ai/tools.py` 当时用默认参数的 `json.dumps`（`allow_nan=True`），
一旦 comparison 里出现非有限数（例如校验层把缺失值算成 NaN 的派生量）就会写成裸 `NaN`。
而 `pipeline.ConversionResult.summary()` 又把 `comparison` **原样**带出，绕过了校验层自己
的 `_clean()`。

**修复**：

- 新增 `src/swnx_switch/ai/wire.py`：`sanitize()`（非有限浮点 → `null`）、`dumps()`（`allow_nan=False`，
  失败则清洗后重序列化）、`loads_strict()`（拒绝 NaN/Infinity 的解析器）。
- `mcp_server._send()` 与 `structuredContent` 统一走 `wire`；`tools` 的所有 `json.dumps` 也换掉。
- 返回值瘦身（见 P1-2）：`data` 不再包含完整 `comparison` 或 IR 全文，从源头上减少非有限数泄漏面。

**验证**：`tests/test_ai_output.py`

- `test_sanitize_replaces_non_finite_floats` / `test_dumps_never_emits_nan_tokens` / `test_loads_strict_rejects_nan`
- `test_slim_payload_is_small_and_strict_json`：把 NaN/Infinity 塞进校验项，断言出口仍是严格 JSON 且变成 `null`
- `test_nan_in_ir_never_reaches_the_wire`：真起 MCP 子进程，喂一个含 `NaN` 的 IR 文件给
  `swnx_compare_parts`，逐行用严格解析器读 stdout

## 2. P0-2 第二次转换静默等 429 s 然后失败

**现象**（报告时间线）：SW 刚被上一个作业 `ExitApp` 关掉，下一次 `DispatchEx` 依次失败
`-2147221021` / `-2146959355`，驱动随后直接启动 `SLDWORKS.exe` 并**只等待一次 420 s**，
全程无输出，最后失败且提示只有一句"手动启动 SolidWorks"。

**根因**：COM 激活是瞬态失败却没有重试；启动 exe 后的等待是单次长等而不是轮询；
失败信息没有带日志尾。

**修复**（`tools/sw/driver.py`）：

- `DispatchEx` 失败按 **2 s / 5 s / 10 s 退避重试**，只对 `CO_E_SERVER_EXEC_FAILURE` /
  `MK_E_UNAVAILABLE` 这类瞬态错误重试。
- 启动 exe 后**每 2 s 轮询 COM ROT**，默认 90 s（`SWNX_SW_ATTACH_TIMEOUT` 可调）；
  launcher 进程已退出则立即失败。
- 区分 "launcher 没起来" 与 "起来了但没进 ROT"，两种给不同 hint；失败 hint 带**本作业日志尾 20 行**
  与错误码含义。
- 连接失败时在作业目录写 `diagnose.md`（进程列表、ROT 探测结果、错误码解码、许可提示、日志尾）。

**验证**：`tests/test_sw_backend.py` 的离线测试用注入的假时钟/假连接验证重试序列、轮询节奏、
快速失败分支与 `diagnose.md` 内容；**真实 COM 的瞬态失败序列需要现场复现**（本机本轮验证见 §7）。

## 3. P0-3 连续启停后 SolidWorks 起不来

**现象**：13:44 之后直接 `Start-Process SLDWORKS.exe` 5 s 后进程消失、退出码 1，
COM 持续 `-2146959355`；报告怀疑与前面 3 次 COM 批量启停有关。

**修复**：

- `keep_app` 选项（CLI `--keep-app` / 工具参数 `keep_app=true` / 环境 `SWNX_KEEP_APP=1`）：
  作业结束后**不** `ExitApp`，下一次作业直接 `GetActiveObject` 复用（驱动本来就是 attach-first）。
- 正常退出时：轮询进程真正消失（≤10 s），再静置 ≥1.5 s，避免下次启动撞上许可/单实例锁释放。
- 连接失败写 `diagnose.md`（同 P0-2）。
- 报告建议的"退出后静默一段时间"已内置，用户无需再手动等待。

**验证**：离线测试覆盖 keep-app 退出决策与静置逻辑；真机连跑两次转换验证实例复用（见 §7）。

## 4. P1 级改进

| 编号 | 问题 | 0.2.0 |
|---|---|---|
| P1-1 | 长任务无进度、无法取消 | `swnx_convert_start` 立即返回 `job_id`；`swnx_job_status` 返回 state/stage/elapsed 与阶段列表（`source_read → export → import → target_read → validate → report`）。CLI 把同样阶段打到 stderr。取消仍待做（roadmap）。 |
| P1-2 | 返回值过重 | `data` 只带 `ok/verdict/target/target_sha256/seconds/key_checks(≤8 项)/report_md/run_dir/warnings/attempts`；`read` 不再回传 IR 全文，只给 `ir_path`。 |
| P1-3 | `probe` 的 available 名不副实 | 拆成 `installed`（exe+驱动+ProgID）/ `launchable`（实际启动过）/ `reachable`（当前有实例可 attach）/ `last_error`；`probe --deep` 与工具 `deep=true` 才会真启动/连接。默认轻量探测不启动 CAD。 |
| P1-4 | 日志措辞 "processes before connect: unavailable" | 改为 `unknown (enumerate failed: <err>)`，能枚举时打印 `[]` 或 `[pid ...]`。 |

## 5. P2 级改进

| 编号 | 问题 | 0.2.0 |
|---|---|---|
| P2-1 | 报告埋在 work 深目录 | 目标文件旁写 `<目标名>.validation.md` / `.json`；`--report-dir` 可改位置；返回值给绝对路径 `report_md`/`report_json`。 |
| P2-2 | 扩展名与模式冲突要转完才发现 | `_validate_request()` 入口校验：源=目标、源与目标同格式、两者都是中间格式、目标已存在、`mode` 与目标扩展名矛盾——毫秒级报错。 |
| P2-3 | 预期差异不前置 | 每次转换返回文本第一行：`预期差异：几何 1:1 一致；参数化特征树/草图不保留（跨内核固有限制，不是转换失败）。` |

## 6. 兼容性说明（客户端需要知道）

- **新增工具**：`swnx_convert_start`、`swnx_job_status`（共 8 个工具）。旧工具名与参数不变。
- **`data` 变瘦**：`swnx_read_part` 不再返回 `ir` 全文（用 `ir_path` 读文件）；
  `swnx_convert_part` 不再返回完整 `comparison`（用 `report_json` 读）。依赖旧字段的客户端需要调整。
- **默认行为**：报告副本会写到目标文件旁（可能需要目标目录可写）；`keep_app` 默认仍为关闭。
- **版本**：0.2.0。`probe` 的 backend 字段新增三态键，旧键保留。

## 7. 真机验证记录

环境：SolidWorks 2025 SP05（33.5.0.53）× NX 2406 × Python 3.12.10 + pywin32；脚本
`work/verify/reliability_live.ps1`（纯 ASCII，避免 PowerShell 代码页把中文路径读坏），
日志 `work/verify/live.log`，产物 `work/verify/live1.prt` / `live2.prt`。
`2026-11-14 14:08` 实跑，**验证时机器上已有一个用户自己启动的 SLDWORKS 实例**（pid 27736），
所以这次走的是 attach 路径，冷启动路径见下方"未覆盖"。

| 项 | 结果 |
|---|---|
| `probe --deep` | NX 9.8 s ok；SolidWorks `installed=True reachable=True launchable=None`（有实例可 attach，未需要启动），deep_probe ok |
| `convert --preflight --keep-app`（第 1 次） | exit 0，**verdict warn**，35.4 s；阶段 `request_validated → preflight(nx,solidworks) → source_read → export 1.6 s → import 9.8 s → target_read → validate → report → validated` |
| 第 1 次后 SW 进程 | 仍是同一个 pid 27736（keep_app 生效，没有 ExitApp） |
| `convert --keep-app`（第 2 次） | exit 0，warn，**25.6 s**（未重新冷启动，attach 复用；比第 1 次少约 10 s） |
| `read --keep-app` | exit 0，**4.0 s**（attach；冷启动约 15–20 s） |
| 报告就近 | `live1.validation.md/.json`、`live2.validation.md/.json` 均存在 |
| `probe --deep`（实例运行中） | `reachable=True launchable=None`，符合预期 |
| `diagnose.md` | 无（没有连接失败） |

**这次实跑还抓到一个真实数据问题（已按语义处理）**：attach 到用户实例后，SW 导出的 Parasolid
密度属性是 `1` 而不是 `1000`（同一个零件在全新实例里导出是 `1e3`），于是 NX 侧质量变成 1/1000。
几何 14 项全部 pass，只有 mass/density 不一致。处理：

- 校验层把 **optional 严重度的 fail 降级为 warn**（几何必需项才是 fail），报告依据写
  "元数据项不匹配（几何不受影响）: mass, density"；
- 密度检查的 detail 明确写出"密度是材质元数据，交换格式可能不保留"；
- 这条语义变更已有测试：`test_metadata_only_mismatch_is_warn_not_fail`。

> 结论：几何转换仍然 pass；质量/密度这类材质元数据要么在目标端重新赋材料，要么接受 warn。
> 复现记录：`work/verify/live1.validation.md`。

### 未覆盖 / 需要现场复现的部分

1. **P0-2 的失败路径**（SW 被关掉后立刻再激活、DispatchEx 瞬态失败 → 退避重试成功）：
   需要先制造"SW 刚退出"的状态；本次验证时 SW 一直在运行，走的是 attach，未触发重试分支。
   该分支有离线注入测试（假时钟/假 HRESULT），但没有真实 COM 复现。
2. **冷启动 + keep_app**（`launchable` 从 None 变 True）：同样因为已有实例在跑，未覆盖。
3. **P0-3 的"连续启停后起不来"**：不主动制造（会把机器弄坏）；靠 settle 与 keep-app 规避。
4. **diagnose.md 的真实内容**：只做了离线内容测试，没有真实连接失败样本。

## 8. 仍然未解决 / 未验证

- **作业取消**：`convert_start` 只支持轮询，不支持取消（roadmap）。
- **真实瞬态 COM 失败序列**：报告里的 429 s 序列是现场观察；自动化测试用注入式模拟，
  未在本机人为制造"SW 起不来"状态去复现（避免把机器弄坏）。
- **`keep_app` 的许可影响**：保持 SW 常驻会持续占用许可证席位，长时间不用需要手动关闭。
- **NX 侧**：本报告只涉及 SolidWorks 侧；NX 的 `keep_app` 无意义（批处理进程本来就一次性）。

## 9. 第二次使用报告（2026-11-14 14:2x，NX → SolidWorks）

第二次真机任务是 NX `.prt` → SolidWorks `.SLDPRT`：37.3 s、15 项检查 14 pass / 1 info warn / 0 fail。
报告确认了几件事，也提出了一个新问题。

### 9.1 保留并被验证的设计

- **异步两段式**（`convert_start` + `job_status`）是报告认为"0.2.0 相比上一版最明显的提升"：
  立即拿到 job id、阶段时间线能看出卡在哪一步。
- **preflight + 实例复用**：预检启动的 SW 被后续转换复用，导入段只需 3.9 s，没有启动类问题。
- **双内核交叉回读校验**：NX 读源件、SW 读转换件再比对，PASS 的含义是"几何保持"而不是"文件存在"。
- **报告就近落盘**、**中文路径无障碍**。

### 9.2 唯一遗留问题：`swnx_probe {deep:true}` 仍报 not lossless JSON

报告现象：`tool "mcp__swnx-switch__swnx_probe" returned invalid output: value is not lossless JSON`。
报告判断"大概率是某个嵌套字段绕过了清洗"——判断正确，但根因不是 `NaN`。

**根因（已复现）**：deep 探针的 payload 里嵌了驱动的原始 JSON，NX 面采样轴里带 **`-0.0`**。
宿主把 `-0` 也算作"非无损 JSON"（`Object.is(value, -0)`），而 `json.dumps(-0.0)` 会合法地写出
`-0.0`；旧 `wire.dumps` 的策略是"先直接序列化，失败再清洗"，于是这条路径根本没有触发清洗。

复现脚本 `work/verify/probe_wire_scan.py` 在修复前打印：

```
HOST-REJECT negative zero at structuredContent.backends[0].deep_probe.payload.part.face_samples[0].axis[1] = -0.0
...（共 6 处）
```

**修复**：`wire.sanitize` 把 `-0.0` 归一为 `0.0`，把超 2^53 的整数转成字符串；`wire.dumps` 去掉
快路径，**总是**先清洗；新增 `wire.scan()` 作为宿主规则的唯一镜像。修复后同一脚本输出
`structuredContent: clean for the host's lossless rules`（deep 负载 51 115 字节）。

### 9.3 采纳的小建议（报告第四节 2–6）

| 建议 | 实现 |
|---|---|
| 2 阶段列表直接给每段耗时 | 阶段项新增 `delta_s`，`job_status` 文本形如 `export@10.0s(+1.6s)` |
| 3 阶段带 backend 名 | 标签追加 `[backend=solidworks]` / `[exchange=parasolid]` |
| 4 目标扩展名大小写 + 回显实际落盘名 | `paths["target_written"]`，与请求不同时给 warning（Windows 不区分大小写但脚本匹配区分） |
| 5 提示同目录同名不同后缀的旧文件 | 成功后扫描同 stem 的其它 CAD 扩展名并 warning |
| 6 `report_dir` 选项 | MCP 工具参数与 CLI `--report-dir`（0.2.0 已有 CLI，0.2.1 补齐工具参数） |
| 1 协议级回归测试 | `test_every_tool_response_is_lossless_json`：真起 MCP 子进程，逐个工具调用并按 `wire.scan` 校验 |

### 9.4 仍未覆盖

- `probe deep` 的 NX 段（10 s）依赖本机 NX 许可；`work/verify/probe_wire_scan.py` 需要真机跑。
- 报告建议的"超长整数"拒绝规则：本机没有出现，已按规则实现（>2^53 → 字符串）并有单测。
