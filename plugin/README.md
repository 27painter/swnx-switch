# swnx-switch · DSH 插件包

把 `swnx-switch` 注册成一个 MCP 服务器，让 DSH / Codex / Claude Desktop 这类客户端直接拿到
6 个工具（`swnx_probe` / `swnx_read_part` / `swnx_convert_part` / `swnx_compare_parts` /
`swnx_part_brief` / `swnx_list_routes`），不用每次让 AI 手写脚本。

## 这个包是什么

只有 Node 侧的三件事：找到 Python 包、注册一行 MCP、给 `bin/swnx-switch.mjs` 的
`doctor` / `where` / `mcp` 用。真正的读件、转换、校验都在 Python 包
`src/swnx_switch/` 里，所以插件升级不用重装 Python 依赖，Python 改完重启 dsh 即可。

## 安装（DSH）

1. 确认 Python 3.10+ 在 PATH（或者设 `SWNX_PYTHON` 指向解释器）。
2. 把本目录链接进 profile 依赖，例如编辑
   `C:\Users\<you>\.dsh\profiles\web\package.json`：

   ```json
   {
     "dependencies": {
       "swnx-switch": "link:D:/27painter/Documents/swnx-switch/plugin"
     },
     "dsh": {
       "profile": {
         "bundles": ["...", "swnx-switch"]
       }
     }
   }
   ```

3. 在该 profile 目录执行 `pnpm install`，然后重启 dsh。
4. 验证：`node "<repo>/plugin/bin/swnx-switch.mjs" doctor`。

插件在加载时会做三件事，任一不满足就**抛错**而不是静默注册：找到含
`src/swnx_switch/ai/mcp_server.py` 的项目目录、找到 Python、`python -m swnx_switch.cli probe`
能跑通。

项目位置解析顺序：`SWNX_SWITCH_HOME` → 本机默认 checker out
`D:\27painter\Documents\swnx-switch` → 相对本包的 `..` / `../..`。

## 安装（非 DSH 的任意 MCP 客户端）

不需要本插件包，直接用仓库根的 Python 包：

```jsonc
{
  "mcpServers": {
    "swnx-switch": {
      "command": "python",
      "args": ["-m", "swnx_switch.ai.mcp_server"],
      "env": {
        "PYTHONPATH": "D:/27painter/Documents/swnx-switch/src",
        "PYTHONIOENCODING": "utf-8"
      }
    }
  }
}
```

## 环境变量

| 变量 | 作用 |
|---|---|
| `SWNX_SWITCH_HOME` | 项目根目录（含 `src/swnx_switch`） |
| `SWNX_PYTHON` | Python 解释器路径（默认在 PATH 里找 `python` / `py -3`） |
| `SWNX_ENABLE_MCP=0` | 加载插件但不注册工具 |
| `SWNX_WORKDIR` / `SWNX_OUTDIR` | 运行目录 / 输出目录（默认项目下 `work` / `out`） |
| `SWNX_TIMEOUT` | 单个 CAD 任务超时秒数（默认 900） |

## 注意

* Windows only：SolidWorks 走 COM，NX 走批处理 `run_journal.exe`。
* 转换是长任务：SolidWorks ↔ NX 一次通常 1–5 分钟，`toolCallTimeoutMs` 已设为 30 分钟。
* 同一时刻只允许一个 NX 任务和一个 SolidWorks 任务（`src/swnx_switch/lock.py` 做了跨进程锁）。
