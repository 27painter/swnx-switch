"""``swnx-switch`` command line.

The CLI is the same code path the MCP tools use, so anything an agent can do a
human can reproduce by hand — which is the only way to debug a converter.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__, ir
from .errors import EnvironmentError_, SwnxError
from .pipeline import Pipeline, probe_environment, supported_pairs

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_ENV = 2


def _print_json(payload: dict) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")

TOLERANCE_ALIASES = {
    "volume": "volume_rel",
    "area": "area_rel",
    "bbox": "bbox_abs_mm",
    "bbox_abs": "bbox_abs_mm",
    "bbox_rel": "bbox_rel",
    "corner": "bbox_corner_abs_mm",
    "centroid": "centroid_abs_mm",
    "mass": "mass_rel",
    "density": "density_rel",
}


def _parse_tolerances(pairs: list[str] | None) -> dict | None:
    """Turn ``--tolerance volume=2e-3`` into validate.py's canonical keys.

    The short names are what a user types; the canonical names (``volume_rel`` …)
    are what :mod:`swnx_switch.validate` merges.  Unknown short names are
    rejected here rather than silently ignored by the validator.
    """
    if not pairs:
        return None
    from .validate import DEFAULT_TOLERANCES  # noqa: PLC0415 - optional layer

    allowed = set(DEFAULT_TOLERANCES) | set(TOLERANCE_ALIASES)
    out: dict = {}
    for item in pairs:
        if "=" not in item:
            raise SwnxError(
                f"--tolerance expects key=value, got {item!r}",
                hint="keys: " + ", ".join(sorted(TOLERANCE_ALIASES)),
            )
        key, value = item.split("=", 1)
        key = key.strip()
        if key not in allowed:
            raise SwnxError(f"unknown tolerance key {key!r}", hint="keys: " + ", ".join(sorted(TOLERANCE_ALIASES)))
        try:
            out[TOLERANCE_ALIASES.get(key, key)] = float(value)
        except ValueError as exc:
            raise SwnxError(f"tolerance {key} is not a number: {value!r}") from exc
    return out


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_probe(args: argparse.Namespace) -> int:
    env = probe_environment(deep=bool(getattr(args, "deep", False)))
    if args.json:
        _print_json(env)
        return EXIT_OK
    print("swnx-switch 环境探测 (environment probe)" + ("  [deep]" if env.get("deep") else ""))
    print(f"  swnx-switch   {env.get('swnx_switch', __version__)}")
    print(f"  python        {env['python']['version']}  {env['python']['executable']}")
    print(f"  pywin32       {'yes' if env['pywin32'] else 'NO (SolidWorks driver will not run)'}")
    nx = env.get("nx")
    if nx:
        print(f"  NX            {nx['root']}  (from {nx['source']}, NXOpen python {nx['nxopen_python']})")
    else:
        print("  NX            NOT FOUND  (set NX_HOME)")
    sw = env.get("solidworks")
    if sw:
        print(f"  SolidWorks    {sw['version']}  {sw['exe']}  (COM registered: {'yes' if sw['com_registered'] else 'no'})")
    else:
        print("  SolidWorks    NOT FOUND  (set SWNX_SLDPRT_EXE)")
    for backend in env.get("backends", []):
        state = "ok" if backend.get("available") else f"unavailable: {backend.get('error', 'not installed')}"
        print(f"  backend {backend['backend']:<12} {state}")
        if backend.get("installed") is not None:
            print(
                f"    installed={backend.get('installed')} launchable={backend.get('launchable')} "
                f"reachable={backend.get('reachable')}"
                + (f"  last_error={backend['last_error']}" if backend.get("last_error") else "")
            )
        deep = backend.get("deep_probe")
        if deep:
            # ``deep_probe`` is either the driver report {ok,seconds,error} or, for
            # backends with an honest tri-state, the connectivity block which nests
            # the report under "deep_probe".
            report = deep.get("deep_probe") if isinstance(deep.get("deep_probe"), dict) else deep
            print(
                f"    deep_probe ok={report.get('ok')} {report.get('seconds')}s"
                + (f"  {report.get('error')}" if report.get("error") else "")
            )
    print(f"  workdir       {env['workdir']}")
    if not env.get("deep"):
        print("  提示: 需要确认 CAD 现在真的能启动，用 `probe --deep`（会实际尝试启动/连接一次）。")
    return EXIT_OK


def cmd_read(args: argparse.Namespace) -> int:
    pipeline = Pipeline(workdir=args.workdir, outdir=args.outdir, timeout=args.timeout)
    doc = pipeline.read(args.file, via=args.via, keep_app=bool(getattr(args, "keep_app", False)))
    out = Path(args.out) if args.out else Path(args.outdir or pipeline.outdir) / f"{Path(args.file).stem}.ir.json"
    ir.dump(doc, out)
    summary = ir.summarize(doc)
    if args.json:
        _print_json({"ok": True, "ir_path": str(out), "summary": summary})
    else:
        print(f"已读取 (read) {args.file}")
        print(f"  IR          {out}")
        _print_summary(summary)
    return EXIT_OK


def cmd_convert(args: argparse.Namespace) -> int:
    pipeline = Pipeline(workdir=args.workdir, outdir=args.outdir, timeout=args.timeout)

    def progress(stage: str, info: dict) -> None:
        detail = ", ".join(f"{key}={value}" for key, value in info.items() if value is not None)
        print(f"  [{stage}] {detail}".rstrip(), file=sys.stderr, flush=True)

    result = pipeline.convert(
        args.source,
        args.target,
        mode=args.mode,
        step_variant=args.step_variant,
        force=args.force,
        keep_intermediate=not args.drop_intermediate,
        tolerances=_parse_tolerances(args.tolerance),
        report=not args.no_report,
        keep_app=bool(getattr(args, "keep_app", False)),
        preflight=bool(getattr(args, "preflight", False)),
        report_dir=getattr(args, "report_dir", None),
        progress=progress,
    )
    payload = result.summary()
    if args.json:
        _print_json(payload)
    else:
        print("预期差异：几何 1:1 一致；参数化特征树/草图不保留（跨内核固有限制，不是转换失败）。")
        print(f"转换 (convert) {result.source_format} -> {result.target_format}")
        print(f"  源 source   {result.source}")
        print(f"  目标 target {result.target}")
        for attempt in result.attempts:
            state = "ok" if attempt.ok else "FAILED"
            print(
                f"  route {attempt.exchange:<10} {state:<7} "
                f"export {attempt.export_seconds:6.1f}s import {attempt.import_seconds:6.1f}s"
                + (f"  {attempt.error}" if attempt.error else "")
            )
        print(f"  结论 verdict {result.verdict}   ({result.seconds:.1f}s)")
        if result.comparison:
            _print_comparison(result.comparison)
        for warning in result.warnings:
            print(f"  warning: {warning}")
        report_path = result.paths.get("report_md_near") or result.paths.get("report_md")
        if report_path:
            print(f"  报告 report  {report_path}")
        elif result.paths.get("report_json"):
            print(f"  报告 report  {result.paths['report_json']}")
        if result.error:
            print(f"  error: {result.error}")
    return EXIT_OK if result.ok else EXIT_FAIL


def cmd_compare(args: argparse.Namespace) -> int:
    from .validate import compare

    reference = ir.load(args.reference)
    candidate = ir.load(args.candidate)
    comparison = compare(reference, candidate, tolerances=_parse_tolerances(args.tolerance))
    if args.json:
        _print_json(comparison)
    else:
        print(f"对比 (compare) verdict={comparison.get('verdict')}")
        _print_comparison(comparison)
    return EXIT_OK if comparison.get("verdict") in ("pass", "warn") else EXIT_FAIL


def cmd_brief(args: argparse.Namespace) -> int:
    from .ai.brief import render

    doc = ir.load(args.ir)
    text = render(doc)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"written: {args.out}")
    else:
        print(text)
    return EXIT_OK


def cmd_routes(args: argparse.Namespace) -> int:
    payload = {"pairs": supported_pairs(), "version": __version__}
    if args.json:
        _print_json(payload)
    else:
        print("支持的转换路线 (supported routes)")
        for pair in supported_pairs():
            print(f"  {pair['source']:<18} -> {pair['target']:<18} {pair['route']}")
    return EXIT_OK


def cmd_mcp(args: argparse.Namespace) -> int:
    from .ai.mcp_server import serve

    return serve(workdir=args.workdir, outdir=args.outdir)


# --------------------------------------------------------------------------- #
# rendering helpers
# --------------------------------------------------------------------------- #
def _print_summary(summary: dict) -> None:
    def fmt(value, digits=3, unit=""):
        if value is None:
            return "-"
        if isinstance(value, float):
            return f"{value:,.{digits}f}{unit}"
        return f"{value}{unit}"

    print(f"  名称 name   {summary.get('name')}   ({summary.get('kind')}, {summary.get('source_format')})")
    print(f"  实体 bodies {summary.get('bodies')}   面 faces {summary.get('faces')}   特征 features {summary.get('features')}")
    print(f"  体积 volume {fmt(summary.get('volume_mm3'), 3, ' mm3')}")
    print(f"  面积 area   {fmt(summary.get('surface_area_mm2'), 3, ' mm2')}")
    if summary.get("mass_kg") is not None:
        print(f"  质量 mass   {fmt(summary.get('mass_kg'), 6, ' kg')}")
    if summary.get("bbox_size_mm"):
        size = " x ".join(f"{v:.3f}" for v in summary["bbox_size_mm"])
        print(f"  包围盒 bbox {size} mm")
    if summary.get("surfaces"):
        parts = ", ".join(f"{k}:{v}" for k, v in summary["surfaces"].items())
        print(f"  曲面 surfaces {parts}")


def _print_comparison(comparison: dict) -> None:
    checks = comparison.get("checks") or []
    if not checks:
        return
    print(f"  {'check':<22} {'status':<12} {'actual':>14} {'expected':>14} {'delta':>12}")
    for check in checks:
        actual = _short(check.get("actual"))
        expected = _short(check.get("expected"))
        delta = _short(check.get("delta"))
        print(f"  {str(check.get('id'))[:22]:<22} {str(check.get('status')):<12} {actual:>14} {expected:>14} {delta:>12}")


def _short(value) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.6g}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(f"{v:.4g}" if isinstance(v, float) else str(v) for v in value) + "]"
    return str(value)[:14]


# --------------------------------------------------------------------------- #
# argument parsing
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="swnx-switch",
        description="SolidWorks .sldprt <-> Siemens NX .prt 零件互通：读取数据特征 + 容差校验的格式转换。",
    )
    parser.add_argument("--version", action="version", version=f"swnx-switch {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser, *, timeout: bool = True) -> None:
        p.add_argument("--workdir", help="运行目录 (default: <repo>/work)")
        p.add_argument("--outdir", help="输出目录 (default: <repo>/out)")
        if timeout:
            p.add_argument("--timeout", type=float, default=None, help="单个 CAD 任务超时秒数 (default 900)")

    p_probe = sub.add_parser("probe", help="探测本机 NX/SolidWorks 环境")
    p_probe.add_argument("--json", action="store_true")
    p_probe.add_argument("--deep", action="store_true", help="深度探测：实际尝试启动/连接一次 CAD（5-20 秒）")
    p_probe.set_defaults(func=cmd_probe)

    p_read = sub.add_parser("read", help="读取零件 -> 中性 IR JSON")
    p_read.add_argument("file")
    p_read.add_argument("--out", help="IR 输出路径")
    p_read.add_argument("--via", choices=["nx", "solidworks"], help="读取 STEP/Parasolid 时用哪个应用导入")
    p_read.add_argument("--keep-app", action="store_true", help="读完后保持 CAD 运行，供后续作业复用")
    p_read.add_argument("--json", action="store_true")
    common(p_read)
    p_read.set_defaults(func=cmd_read)

    p_convert = sub.add_parser("convert", help="在 .sldprt / .prt / .step / .x_t 之间转换并校验")
    p_convert.add_argument("source")
    p_convert.add_argument("target")
    p_convert.add_argument(
        "--mode",
        choices=["auto", "step", "parasolid"],
        default="auto",
        help="中间格式 (default auto: 经过 NX 的路线优先 Parasolid，其余优先 STEP)",
    )
    p_convert.add_argument("--step-variant", choices=["ap203", "ap214", "ap242"], default="ap242")
    p_convert.add_argument("--force", action="store_true", help="覆盖已存在的目标文件")
    p_convert.add_argument("--no-report", action="store_true", help="不生成 Markdown 报告")
    p_convert.add_argument("--report-dir", help="报告副本目录（默认放目标文件旁边）")
    p_convert.add_argument("--drop-intermediate", action="store_true", help="转换成功后删除中间 STEP/Parasolid")
    p_convert.add_argument("--tolerance", action="append", metavar="KEY=VALUE", help="覆盖容差，如 --tolerance volume=2e-3")
    p_convert.add_argument("--keep-app", action="store_true", help="转换结束后保持 SolidWorks 运行，供下一次转换复用（避免反复冷启动）")
    p_convert.add_argument("--preflight", action="store_true", help="开始前深度探测一次，环境不可用时快速失败")
    p_convert.add_argument("--json", action="store_true")
    common(p_convert)
    p_convert.set_defaults(func=cmd_convert)

    p_cmp = sub.add_parser("compare", help="对比两个 IR JSON（源 vs 转换结果）")
    p_cmp.add_argument("reference")
    p_cmp.add_argument("candidate")
    p_cmp.add_argument("--tolerance", action="append", metavar="KEY=VALUE")
    p_cmp.add_argument("--json", action="store_true")
    p_cmp.set_defaults(func=cmd_compare)

    p_brief = sub.add_parser("brief", help="把 IR 渲染成给大模型看的零件简报")
    p_brief.add_argument("ir")
    p_brief.add_argument("--out")
    p_brief.set_defaults(func=cmd_brief)

    p_routes = sub.add_parser("routes", help="列出支持的转换路线")
    p_routes.add_argument("--json", action="store_true")
    p_routes.set_defaults(func=cmd_routes)

    p_mcp = sub.add_parser("mcp", help="以 MCP stdio 服务器方式运行（供 AI 客户端调用）")
    common(p_mcp, timeout=False)
    p_mcp.set_defaults(func=cmd_mcp)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except EnvironmentError_ as exc:
        print(f"environment error: {exc.message}", file=sys.stderr)
        if exc.hint:
            print(f"hint: {exc.hint}", file=sys.stderr)
        return EXIT_ENV
    except SwnxError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        if exc.detail:
            print(f"detail: {exc.detail}", file=sys.stderr)
        if exc.hint:
            print(f"hint: {exc.hint}", file=sys.stderr)
        return EXIT_FAIL


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
