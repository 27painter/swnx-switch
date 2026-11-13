# 验证证据 / Verification evidence

本目录是 README 第 12 节数字的原始出处，均为 2026-11-13 在本机
（SolidWorks 2025 SP05 / NX 2406 / Python 3.12 + pywin32）实跑产生，不是手写。

| 文件 | 内容 |
|---|---|
| `e2e-evidence.json` | `scripts/verify_e2e.py --mode parasolid` 的完整记录：环境、两条方向的逐步耗时、路径、大小、逐项检查 |
| `validation-sw-to-nx.md` | SolidWorks → NX 的中文验证报告（15 项检查 / 整体 pass） |
| `validation-nx-to-sw.md` | NX → SolidWorks 回程的验证报告（同样 pass） |
| `sw-acceptance.json` | SolidWorks 驱动独立验收（probe / read / STEP 导出 / STEP 回读 / Parasolid 导出 / 外来 STEP 导入 / 回读） |
| `nx-native-ir.json` | NX 原生件 `probe_part.prt` 读出的 IR（AI 简报的输入） |
| `nx-import-ir.json` | SolidWorks 的 `.x_t` 被 NX 导入后读回的 IR |

## 复跑方法

```bat
:: 需要一次 danger-full-access 授权（DSH 沙箱）/ 或直接在普通终端运行
python scripts\verify_e2e.py --mode parasolid --force
```

退出码 0 表示两条方向都进入 `pass`/`warn`。脚本会把新的 `evidence.json` 连同每步报告
写回 `out/e2e/`，可以与本目录的存档逐项对比。

## 关键数字（摘要）

| 指标 | SolidWorks 源件 | NX 结果 | NX 回程 SolidWorks | 偏差 |
|---|---|---|---|---|
| 实体数 | 1 | 1 | 1 | 0 |
| 体积 mm³ | 461 736.370946 | 461 736.3709456 | 461 736.3709463 | 3.7e-07 |
| 表面积 mm² | 87 883.2766 | 87 883.2766 | 87 883.2766 | 4.7e-07 |
| 包围盒 mm | 188.5 × 80 × 110 | 188.5 × 80 × 110 | 188.5 × 80 × 110 | 0 |
| 面 / 边 | 25 / 45 | 25 / 45 | 25 / 45 | 0 |
| 耗时 | — | 34.2 s | 35.2 s | — |

体积相对偏差约 **8e-10**，远小于默认容差 `1e-3`。这不是"看起来一样"，是两端 CAD
各自导出的质量属性在数值精度内一致。
