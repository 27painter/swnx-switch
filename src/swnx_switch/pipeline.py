"""The conversion pipeline: native -> exchange -> native, then verify.

A conversion is five steps, and every one of them can fail on its own:

1. **read the source** into the neutral IR (this is also what the AI layer shows
   the model, and what the verification compares against);
2. **export** the source part to an exchange format from its own application;
3. **import** that file into the other application and **save** the native file;
4. **read the target** back into the IR;
5. **compare** source and target within tolerance and write a report.

Doing (1) and (4) through the *applications'* own APIs rather than by parsing the
exchange file is deliberate: the report then says "SolidWorks says this part is
31.4 cm3 and NX says the imported part is 31.4 cm3", which is the claim a user
actually cares about.  Parsing STEP text would only prove the file is text.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import ir
from .backends import BackendRegistry, Job, detect_format, format_owner
from .errors import ConversionError, SwnxError
from .paths import ensure_outdir, ensure_workdir

EXCHANGE_EXT = {"step": ".step", "parasolid": ".x_t"}


@dataclass
class ConversionAttempt:
    exchange: str
    ok: bool = False
    export_ok: bool = False
    import_ok: bool = False
    export_seconds: float = 0.0
    import_seconds: float = 0.0
    file: str | None = None
    error: str | None = None
    log: str | None = None

    def as_dict(self) -> dict:
        return {
            "exchange": self.exchange,
            "ok": self.ok,
            "export_ok": self.export_ok,
            "import_ok": self.import_ok,
            "export_seconds": round(self.export_seconds, 2),
            "import_seconds": round(self.import_seconds, 2),
            "file": self.file,
            "error": self.error,
            "log": self.log,
        }


@dataclass
class ConversionResult:
    ok: bool
    source: Path
    target: Path
    source_format: str
    target_format: str
    run_dir: Path
    reference_ir: dict | None = None
    candidate_ir: dict | None = None
    comparison: dict | None = None
    verdict: str = "unknown"
    attempts: list[ConversionAttempt] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    paths: dict = field(default_factory=dict)
    error: str | None = None
    seconds: float = 0.0

    def summary(self) -> dict:
        return {
            "ok": self.ok,
            "verdict": self.verdict,
            "mode": f"{self.source_format}->{self.target_format}",
            "source": str(self.source),
            "target": str(self.target),
            "run_dir": str(self.run_dir),
            "reference_ir": self.paths.get("reference_ir"),
            "candidate_ir": self.paths.get("candidate_ir"),
            "report_json": self.paths.get("report_json"),
            "report_md": self.paths.get("report_md"),
            "attempts": [a.as_dict() for a in self.attempts],
            "warnings": self.warnings,
            "error": self.error,
            "seconds": round(self.seconds, 2),
            "comparison": self.comparison,
        }


class Pipeline:
    """Orchestrates drivers, IR sidecars, verification and reporting."""

    def __init__(
        self,
        *,
        workdir: str | os.PathLike | None = None,
        outdir: str | os.PathLike | None = None,
        timeout: float | None = None,
        registry: BackendRegistry | None = None,
    ) -> None:
        self.workdir = ensure_workdir(workdir)
        self.outdir = ensure_outdir(outdir)
        self.timeout = float(timeout or os.environ.get("SWNX_TIMEOUT") or 900)
        self.registry = registry or BackendRegistry()

    # ------------------------------------------------------------------ #
    # reading
    # ------------------------------------------------------------------ #
    def read(
        self,
        path: str | os.PathLike,
        *,
        via: str | None = None,
        label: str = "read",
    ) -> dict:
        """Read any supported file into the IR.

        Native files go straight to their own application.  Exchange files
        (STEP/Parasolid) are imported into a temporary native part by ``via``
        (default: whichever app is available), because both applications can
        describe a STEP file far better than any Python parser in this repo can.
        """
        source = Path(path).resolve()
        if not source.exists():
            raise SwnxError(f"input file not found: {source}")
        fmt = detect_format(source)
        if fmt in ("step", "parasolid"):
            backend = self._pick_backend(via)
            run_dir = self._run_dir(f"{label}-{'import'}")
            native = run_dir / f"{source.stem}.{'prt' if backend.name == 'nx' else 'SLDPRT'}"
            native_fmt = "prt" if backend.name == "nx" else "sldprt"
            result = backend.run(
                Job("import", input=str(source), output=str(native), format=native_fmt),
                workdir=run_dir / backend.name,
                timeout=self.timeout,
            )
            result.require_ok()
            doc = self._read_native(backend, native, run_dir)
            doc["source"]["original_path"] = str(source)
            doc["source"]["via"] = f"{source.suffix} imported into {native_fmt}"
            return doc

        backend = self.registry.for_format(fmt)
        run_dir = self._run_dir(label)
        return self._read_native(backend, source, run_dir)

    def _read_native(self, backend, path: Path, run_dir: Path) -> dict:
        result = backend.run(
            Job("read", input=str(path), output=None, format=None),
            workdir=run_dir / backend.name,
            timeout=self.timeout,
        )
        result.require_ok()
        doc = result.ir
        if not isinstance(doc, dict):
            raise SwnxError(f"{backend.name} returned no IR for {path}")
        problems = ir.validate(doc)
        if problems:
            raise SwnxError(
                f"{backend.name} returned a malformed IR for {path}",
                detail="; ".join(problems),
                hint=f"driver log: {result.log}",
            )
        for warning in result.warnings:
            ir.warn(doc, warning)
        doc.setdefault("diagnostics", {}).setdefault("driver", {})["log"] = result.log
        return doc

    # ------------------------------------------------------------------ #
    # conversion
    # ------------------------------------------------------------------ #
    def convert(
        self,
        source: str | os.PathLike,
        target: str | os.PathLike,
        *,
        mode: str = "auto",
        step_variant: str = "ap242",
        force: bool = False,
        keep_intermediate: bool = True,
        tolerances: dict | None = None,
        report: bool = True,
        task: str | None = None,
    ) -> ConversionResult:
        started = time.time()
        src = Path(source).resolve()
        dst = Path(target).resolve()
        if not src.exists():
            raise SwnxError(f"source file not found: {src}")
        if dst.exists() and not force:
            raise ConversionError(
                f"target already exists: {dst}",
                hint="pass --force to overwrite",
            )
        dst.parent.mkdir(parents=True, exist_ok=True)

        src_fmt = detect_format(src)
        dst_fmt = detect_format(dst)
        run_dir = self._run_dir(f"convert-{src_fmt}-to-{dst_fmt}")
        result = ConversionResult(
            ok=False,
            source=src,
            target=dst,
            source_format=src_fmt,
            target_format=dst_fmt,
            run_dir=run_dir,
            paths={},
        )

        try:
            if src_fmt in ("step", "parasolid") and dst_fmt in ("step", "parasolid"):
                raise ConversionError("source and target are both exchange formats; nothing to convert")
            if src_fmt in ("step", "parasolid"):
                self._import_only(src, dst, dst_fmt, step_variant, run_dir, result)
            elif dst_fmt in ("step", "parasolid"):
                self._export_only(src, src_fmt, dst, dst_fmt, step_variant, run_dir, result)
            else:
                self._native_to_native(
                    src, dst, src_fmt, dst_fmt, mode, step_variant, run_dir, result, keep_intermediate
                )
            result.seconds = time.time() - started
            self._verify(result, tolerances=tolerances, report=report, task=task)
        except SwnxError as exc:
            result.error = exc.message
            result.warnings.extend(filter(None, [exc.detail, exc.hint]))
            result.ok = False
            result.verdict = "fail"
            result.seconds = time.time() - started
        return result

    # -- the three shapes of a conversion --------------------------------- #
    def _native_to_native(
        self, src, dst, src_fmt, dst_fmt, mode, step_variant, run_dir, result, keep_intermediate: bool = True
    ) -> None:
        src_backend = self.registry.for_format(src_fmt)
        dst_backend = self.registry.for_format(dst_fmt)
        result.reference_ir = self._dump_ir(src_backend, src, run_dir, "source", result)
        result.paths["reference_ir"] = str(run_dir / "ir-source.json")

        exchanges = choose_exchanges(mode, src_fmt, dst_fmt)
        for exchange in exchanges:
            attempt = ConversionAttempt(exchange=exchange)
            exchange_file = run_dir / f"exchange-{exchange}{EXCHANGE_EXT[exchange]}"
            attempt.file = str(exchange_file)
            try:
                exported = src_backend.run(
                    Job(
                        "export",
                        input=str(src),
                        output=str(exchange_file),
                        format=exchange,
                        options={"step_variant": step_variant},
                    ),
                    workdir=run_dir / src_backend.name,
                    timeout=self.timeout,
                )
                attempt.export_ok = exported.ok
                attempt.export_seconds = exported.duration_s
                attempt.log = exported.log
                result.warnings.extend(f"export {exchange}: {w}" for w in exported.warnings)
                if not exported.ok:
                    attempt.error = exported.error or "export failed"
                    result.attempts.append(attempt)
                    continue
                # Trust the file the driver actually wrote: when a driver falls
                # back (e.g. NX writes a metadata-only STEP and switches to
                # Parasolid) the useful artefact is the one in ``outputs``, not
                # the path we asked for.
                exported_path = Path(exported.outputs[0]) if exported.outputs else exchange_file
                if not exported_path.exists():
                    exported_path = exchange_file
                attempt.file = str(exported_path)
                imported = dst_backend.run(
                    Job("import", input=str(exported_path), output=str(dst), format=dst_fmt),
                    workdir=run_dir / dst_backend.name,
                    timeout=self.timeout,
                )
                attempt.import_ok = imported.ok
                attempt.import_seconds = imported.duration_s
                attempt.log = imported.log or attempt.log
                result.warnings.extend(f"import {exchange}: {w}" for w in imported.warnings)
                if not imported.ok:
                    attempt.error = imported.error or "import failed"
                    result.attempts.append(attempt)
                    continue
                attempt.ok = True
                result.attempts.append(attempt)
                break
            except SwnxError as exc:
                attempt.error = exc.message
                result.attempts.append(attempt)
                continue

        if not any(a.ok for a in result.attempts):
            tried = ", ".join(a.exchange for a in result.attempts) or "none"
            result.error = f"no exchange route succeeded ({tried})"
            return
        result.ok = True

        try:
            candidate = self._read_native(dst_backend, dst, run_dir)
            result.candidate_ir = candidate
            result.paths["candidate_ir"] = str(ir.dump(candidate, run_dir / "ir-target.json"))
        except SwnxError as exc:
            result.warnings.append(f"could not read the converted part back: {exc.message}")

        if not keep_intermediate:
            for attempt in result.attempts:
                if attempt.file and Path(attempt.file).exists():
                    Path(attempt.file).unlink(missing_ok=True)

    def _import_only(self, src, dst, dst_fmt, step_variant, run_dir, result) -> None:
        dst_backend = self.registry.for_format(dst_fmt)
        imported = dst_backend.run(
            Job("import", input=str(src), output=str(dst), format=dst_fmt),
            workdir=run_dir / dst_backend.name,
            timeout=self.timeout,
        )
        imported.require_ok()
        result.ok = True
        result.attempts.append(
            ConversionAttempt(exchange=src.suffix.lstrip("."), ok=True, import_ok=True, import_seconds=imported.duration_s, file=str(src), log=imported.log)
        )
        candidate = self._read_native(dst_backend, dst, run_dir)
        result.candidate_ir = candidate
        result.paths["candidate_ir"] = str(ir.dump(candidate, run_dir / "ir-target.json"))
        result.warnings.append("source is an exchange format: no independent reference part, so property comparison is informational only")

    def _export_only(self, src, src_fmt, dst, dst_fmt, step_variant, run_dir, result) -> None:
        src_backend = self.registry.for_format(src_fmt)
        result.reference_ir = self._dump_ir(src_backend, src, run_dir, "source", result)
        result.paths["reference_ir"] = str(run_dir / "ir-source.json")
        exported = src_backend.run(
            Job(
                "export",
                input=str(src),
                output=str(dst),
                format=dst_fmt,
                options={"step_variant": step_variant, "export_only": True},
            ),
            workdir=run_dir / src_backend.name,
            timeout=self.timeout,
        )
        exported.require_ok()
        result.ok = True
        result.attempts.append(
            ConversionAttempt(exchange=dst_fmt, ok=True, export_ok=True, export_seconds=exported.duration_s, file=str(dst), log=exported.log)
        )
        result.warnings.append("target is an exchange format: the parametric history is not carried, only the B-Rep")

    # ------------------------------------------------------------------ #
    # verification + reporting
    # ------------------------------------------------------------------ #
    def _verify(self, result: ConversionResult, *, tolerances: dict | None, report: bool, task: str | None) -> None:
        if not result.ok or not result.candidate_ir:
            result.verdict = "fail"
            return
        if not result.reference_ir:
            result.verdict = "warn"
            return
        try:
            from .validate import compare  # noqa: PLC0415 - optional layer during development
        except ImportError:
            result.verdict = "unverified"
            result.warnings.append("validation layer is not installed (src/swnx_switch/validate.py missing)")
            return
        comparison = compare(result.reference_ir, result.candidate_ir, tolerances=tolerances)
        result.comparison = comparison
        result.verdict = comparison.get("verdict", "unknown")
        result.ok = result.verdict in ("pass", "warn")
        if report:
            self._write_report(result, task=task)

    def _write_report(self, result: ConversionResult, *, task: str | None) -> None:
        meta = {
            "source": str(result.source),
            "target": str(result.target),
            "mode": f"{result.source_format} -> {result.target_format}",
            "task": task,
            "attempts": [a.as_dict() for a in result.attempts],
            "warnings": result.warnings,
            "run_dir": str(result.run_dir),
            "seconds": round(result.seconds, 2),
        }
        report_json = result.run_dir / "report.json"
        payload = {
            "schema": "swnx.convert-report/1",
            "ok": result.ok,
            "verdict": result.verdict,
            "meta": meta,
            "comparison": result.comparison,
        }
        report_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        result.paths["report_json"] = str(report_json)
        try:
            from .report import write_report  # noqa: PLC0415
        except ImportError:
            return
        try:
            written = write_report(result.comparison, meta, out_dir=result.run_dir, stem="validation")
        except Exception as exc:  # a broken report must not lose the conversion
            result.warnings.append(f"report renderer failed: {exc}")
            return
        if isinstance(written, dict):
            for key in ("markdown", "json"):
                if written.get(key):
                    result.paths[f"report_{'md' if key == 'markdown' else 'json'}"] = str(written[key])
        elif written:
            result.paths["report_md"] = str(written)

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def _dump_ir(self, backend, path: Path, run_dir: Path, label: str, result: ConversionResult) -> dict:
        """Read a native part into the IR and keep the JSON next to the run."""
        doc = self._read_native(backend, path, run_dir)
        ir.dump(doc, run_dir / f"ir-{label}.json")
        return doc

    def _pick_backend(self, via: str | None):
        if via:
            return self.registry.get(via)
        for name in ("solidworks", "nx"):
            backend = self.registry.get(name)
            if backend.available():
                return backend
        raise SwnxError("neither SolidWorks nor NX is available to import an exchange file")

    def _run_dir(self, label: str) -> Path:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = self.workdir / f"{label}-{stamp}-{uuid.uuid4().hex[:6]}"
        path.mkdir(parents=True, exist_ok=True)
        return path


def choose_exchanges(mode: str, src_fmt: str, dst_fmt: str) -> list[str]:
    """Exchange formats to try, in order.

    ``auto`` is deliberately asymmetric once NX is involved: NX's *batch* STEP
    translator is unreliable (on NX 2406 it writes a metadata-only STEP and
    imports zero bodies, because the STEP translator needs a licence/feature the
    batch session does not have).  Parasolid needs no translator at all — it is
    NX's own kernel format — so every route through NX tries Parasolid first and
    keeps STEP as the fallback.  Pairs that do not involve NX still prefer STEP,
    which carries names, colours and PMI that Parasolid does not.

    An explicit ``--mode`` always wins: the user asked for that format, so trying
    the other one first would be second-guessing them.
    """
    if mode == "auto":
        if format_owner(src_fmt) == "nx" or format_owner(dst_fmt) == "nx":
            return ["parasolid", "step"]
        return ["step", "parasolid"]
    if mode in EXCHANGE_EXT:
        return [mode]
    raise ConversionError(f"unknown conversion mode {mode!r}", hint="mode: auto | step | parasolid")


def probe_environment() -> dict:
    """Full environment report used by ``swnx-switch probe`` and the MCP layer."""
    from .paths import describe_environment  # noqa: PLC0415

    env = describe_environment()
    env["backends"] = BackendRegistry().describe()
    try:
        from . import __version__

        env["swnx_switch"] = __version__
    except Exception:  # pragma: no cover - version is cosmetic
        pass
    return env


def supported_pairs() -> list[dict]:
    """Which (source, target) pairs are meaningful, for docs and tool schemas."""
    return [
        {"source": "sldprt", "target": "prt", "route": "SolidWorks -> STEP/Parasolid -> NX"},
        {"source": "prt", "target": "sldprt", "route": "NX -> STEP/Parasolid -> SolidWorks"},
        {"source": "sldprt", "target": "step|parasolid", "route": "SolidWorks export"},
        {"source": "prt", "target": "step|parasolid", "route": "NX export"},
        {"source": "step|parasolid", "target": "sldprt|prt", "route": "import into the target app"},
    ]


def detect_direction(source: str | os.PathLike, target: str | os.PathLike) -> str:
    src, dst = detect_format(source), detect_format(target)
    return f"{format_owner(src) or 'exchange'}->{format_owner(dst) or 'exchange'}"


__all__ = [
    "Pipeline",
    "ConversionResult",
    "ConversionAttempt",
    "choose_exchanges",
    "probe_environment",
    "supported_pairs",
    "detect_direction",
]
