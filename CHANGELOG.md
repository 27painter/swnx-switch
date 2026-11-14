# Changelog

## 0.2.1 — 2026-11-14

第二次真机使用报告（14:20–14:33，NX → SolidWorks 一次成功、PASS）的收尾：`swnx_probe {deep:true}`
仍被判过一次 `invalid output: value is not lossless JSON`，另有 5 条小改进。逐条对照见
[docs/reliability.md](docs/reliability.md) 第 9 节。

### 修复

- **`probe` deep 仍报 not lossless JSON**：真凶是 **`-0.0`**。宿主把 `-0` 视为非无损 JSON；
  `json.dumps` 会原样写出 `-0.0`，而旧 `wire.dumps` 的"先直接序列化、失败再清洗"快路径因此漏掉它
  （`-0.0` 在 Python 里是合法 JSON，不会触发清洗）。`wire.sanitize` 现在把 `-0.0` 归一为 `0.0`、
  把超过 2^53 的整数转成字符串，`wire.dumps` 改为**总是**先清洗再序列化。
  复现/验证脚本：`work/verify/probe_wire_scan.py`（修复前 deep 负载里 6 处 `-0.0`，修复后 clean）。
- 新增 `wire.scan()`：按宿主规则逐值检查（NaN / Infinity / `-0.0` / 超范围整数），测试与验证脚本共用同一规则。

### 改进（来自报告建议 2–6）

- `swnx_job_status` 的阶段列表直接给出 **`delta_s`**（每段耗时），并带 `backend` / `exchange` 标签，
  不需要调用方自己做减法（建议 2、3）。
- 转换结果回显**实际落盘路径** `target_written`：SolidWorks 可能把 `x.sldprt` 存成 `x.SLDPRT`，
  脚本按名字精确匹配时不再踩坑（建议 4）。
- 目标目录存在**同名不同后缀**的文件时给 warning（旧产物容易点错，建议 5）。
- MCP 工具参数新增 **`report_dir`**（批量场景集中放报告，建议 6）。
- 新增**协议级回归测试**：每个工具的真实 MCP 响应逐行按无损 JSON 规则扫描（建议 1）。

### 测试

138 项（新增 7 项：`-0.0`/超范围整数、scan、阶段标签、全工具协议扫描、`delta_s`、落盘名与同名文件）。

## 0.2.0 — 2026-11-14

可靠性版本：按一份真实调用方的使用报告（桌面 `swnx-switch使用体验与改进建议报告.md`）逐项修复。
详细对照见 [docs/reliability.md](docs/reliability.md)。

### 新增

- `swnx_convert_start` + `swnx_job_status`：异步转换与阶段轮询
  （`source_read → export → import → target_read → validate → report`）。
- CLI `probe --deep`：真实尝试启动/连接 CAD，返回 `installed / launchable / reachable / last_error`。
- CLI/工具 `keep_app`（`--keep-app`）：作业结束后保持 CAD 运行，供下一次转换复用实例。
- CLI `--preflight` / 工具 `preflight`：转换前深度探测，环境不可用时快速失败。
- CLI `--report-dir`：报告副本目录（默认放目标文件旁）。
- `diagnose.md`：SolidWorks 连接失败时自动生成（进程/ROT/错误码/日志尾）。
- `docs/reliability.md`、`tests/test_jobs.py`、`tests/test_ai_output.py`。

### 修复

- **P0** 成功却返回 `invalid output: value is not lossless JSON`：工具出口统一严格 JSON
  （`allow_nan=False` + 非有限数清洗），`structuredContent` 二次清洗。
- **P0** 第二次转换静默等 429 s：COM 激活退避重试（2/5/10 s）+ 启动后每 2 s 轮询 ROT
  （默认 90 s，`SWNX_SW_ATTACH_TIMEOUT`），失败 hint 带日志尾与错误码解码。
- **P0** 连续启停后 SolidWorks 起不来：`keep_app` 复用实例；退出后等进程消失再静置 ≥1.5 s。
- **P1** 长任务无进度：后台作业 + 阶段事件（CLI 同步输出阶段到 stderr）。
- **P1** 返回值过重：`data` 最小化（verdict/关键检查/sha256/报告路径），IR 与完整对比留在磁盘。
- **P1** `probe` 的 `available` 不实：三态 + `last_error`；默认轻量、`deep` 才真探测。
- **P1** 日志措辞 `processes before connect: unavailable` → `unknown (enumerate failed: ...)`。
- **P2** 报告埋在 `work/`：目标文件旁另写 `<目标名>.validation.md/.json`，返回绝对路径。
- **P2** 目标扩展名与模式冲突要转完才发现：入口毫秒级校验（源=目标、同格式、已存在、mode 冲突）。
- **P2** 预期差异不前置：转换结果文本第一行固定为"几何 1:1，参数化特征不保留"。
- 校验语义：`mass`/`density`（optional）失败不再判 `fail`，降级为 `warn` 并注明"元数据项不匹配（几何不受影响）"；
  新增测试 `test_metadata_only_mismatch_is_warn_not_fail`。
- COM attach：`pythoncom.GetActiveObject` 返回的是 `PyIUnknown`，必须先 `QueryInterface(IID_IDispatch)`
  再包 dynamic dispatch，否则 attach 路径读件会报 `'PyIUnknown' object has no attribute 'GetIDsOfNames'`。

### 变更（客户端可见）

- `swnx_read_part` 的 `data` 不再含 IR 全文（改 `ir_path`）。
- `swnx_convert_part` 的 `data` 不再含完整 `comparison`（改 `report_json`）。
- 工具数 6 → 8。旧工具名/参数保持兼容。

### 真机验证（2026-11-14，`work/verify/reliability_live.ps1`，证据见 `docs/evidence/reliability-live*`）

- `probe --deep`：NX 9.8 s；SW `installed=True reachable=True launchable=None`（有实例可 attach）。
- 两次转换 + 一次读件全部 exit 0：`--preflight --keep-app` 35.4 s、`--keep-app` 25.6 s、`read --keep-app` 4.0 s；
  keep_app 生效（SW 进程始终是同一个 pid，没有反复冷启动）。
- 报告就近：`live1/live2.validation.md/.json` 均生成。
- 附带发现并修正：attach 到用户实例后 SW 导出的 Parasolid 密度属性为 1（全新实例为 1000），
  导致 mass/density 不一致而几何 14 项全过 → 校验语义改为 optional 失败降级 warn。

### 测试

- `tests/test_ai_output.py`：严格 JSON、瘦身负载、MCP 子进程级 NaN 回归、异步作业工具。
- `tests/test_jobs.py`：作业状态机、阶段记录、失败捕获、列表裁剪。
- `tests/test_pipeline.py`：入口校验、报告就近、进度回调、keep_app 选项。
- 全量：107 项通过（无第三方依赖）。

## 0.1.0 — 2026-11-13

首个版本：中立 IR 读取、SolidWorks ⇄ NX 双向转换（Parasolid 路线）、15 项容差校验、
双语报告、CLI / 双击菜单 / MCP / DSH 插件 / skill、端到端实测（体积偏差 3.7e-07 mm³）。
