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

Reliability features that came out of a real user report
-------------------------------------------------------
* **Progress milestones** (``progress`` callback): a 30 s – 5 min conversion must
  not be a black box.  Stages are emitted as ``source_read``, ``export``,
  ``import``, ``target_read``, ``validate``, ``report``.
* **Request validation before any CAD starts**: a target that already exists, a
  same-format source/target pair, or a ``mode`` that contradicts the target
  extension fails in milliseconds instead of after a conversion.
* **``keep_app``**: leave the CAD application running between jobs so the next
  job attaches instead of cold-starting it (the failure mode where SolidWorks
  refuses to start after repeated COM start/stop).
* **Report next to the target**: a copy of ``validation.md`` is written beside
  the produced part, so the user does not have to dig through ``work/``.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import ir
from .backends import BackendRegistry, Job, detect_format, format_owner
from .errors import ConversionError, SwnxError
from .paths import ensure_outdir, ensure_workdir

EXCHANGE_EXT = {"step": ".step", "parasolid": ".x_t"}

#: Progress callback: ``cb(stage, info)``; never raises into the pipeline.
Progress = Callable[[str, dict], None]


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
            "target_sha256": self.paths.get("target_sha256"),
            "run_dir": str(self.run_dir),
            "reference_ir": self.paths.get("reference_ir"),
            "candidate_ir": self.paths.get("candidate_ir"),
            "report_json": self.paths.get("report_json"),
            "report_md": self.paths.get("report_md"),
            "report_md_near": self.paths.get("report_md_near"),
            "report_json_near": self.paths.get("report_json_near"),
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
        # Per-run state (one Pipeline instance is used by one conversion/job).
        self._progress: Progress | None = None
        self._keep_app = False
        self._report_dir: Path | None = None

    # ------------------------------------------------------------------ #
    # reading
    # ------------------------------------------------------------------ #
    def read(
        self,
        path: str | os.PathLike,
        *,
        via: str | None = None,
        label: str = "read",
        keep_app: bool = False,
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
        self._keep_app = bool(keep_app)
        fmt = detect_format(source)
        if fmt in ("step", "parasolid"):
            backend = self._pick_backend(via)
            run_dir = self._run_dir(f"{label}-{'import'}")
            native = run_dir / f"{source.stem}.{'prt' if backend.name == 'nx' else 'SLDPRT'}"
            native_fmt = "prt" if backend.name == "nx" else "sldprt"
            result = backend.run(
                Job(
                    "import",
                    input=str(source),
                    output=str(native),
                    format=native_fmt,
                    options=self._options(),
                ),
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
            Job("read", input=str(path), output=None, format=None, options=self._options()),
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
        keep_app: bool = False,
        preflight: bool = False,
        report_dir: str | os.PathLike | None = None,
        progress: Progress | None = None,
    ) -> ConversionResult:
        started = time.time()
        src = Path(source).resolve()
        dst = Path(target).resolve()
        self._progress = progress
        self._keep_app = bool(keep_app)
        self._report_dir = Path(report_dir).resolve() if report_dir else None

        src_fmt = detect_format(src)
        dst_fmt = detect_format(dst)
        result = ConversionResult(
            ok=False,
            source=src,
            target=dst,
            source_format=src_fmt,
            target_format=dst_fmt,
            run_dir=self.workdir,
            paths={},
        )

        try:
            self._validate_request(src, dst, src_fmt, dst_fmt, mode, force)
            dst.parent.mkdir(parents=True, exist_ok=True)
            run_dir = self._run_dir(f"convert-{src_fmt}-to-{dst_fmt}")
            result.run_dir = run_dir
            self._emit("request_validated", source=src_fmt, target=dst_fmt, mode=mode)

            if preflight:
                self._preflight(src_fmt, dst_fmt)

            if src_fmt in ("step", "parasolid"):
                self._import_only(src, dst, dst_fmt, step_variant, run_dir, result)
            elif dst_fmt in ("step", "parasolid"):
                self._export_only(src, src_fmt, dst, dst_fmt, step_variant, run_dir, result)
            else:
                self._native_to_native(
                    src, dst, src_fmt, dst_fmt, mode, step_variant, run_dir, result, keep_intermediate
                )

            result.seconds = round(time.time() - started, 3)
            if result.ok and dst.exists():
                try:
                    result.paths["target_sha256"] = ir.sha256_of(dst)
                except OSError as exc:  # pragma: no cover - unreadable target
                    result.warnings.append(f"could not hash the target: {exc}")
            self._verify(result, tolerances=tolerances, report=report, task=task)
        except SwnxError as exc:
            result.error = exc.message
            result.warnings.extend(filter(None, [exc.detail, exc.hint]))
            result.ok = False
            result.verdict = "fail"
            result.seconds = round(time.time() - started, 3)
        finally:
            self._progress = None
            self._keep_app = False
            self._report_dir = None
        if result.ok:
            self._emit("done", verdict=result.verdict, seconds=result.seconds)
        return result

    # -- the three shapes of a conversion --------------------------------- #
    def _native_to_native(
        self, src, dst, src_fmt, dst_fmt, mode, step_variant, run_dir, result, keep_intermediate: bool = True
    ) -> None:
        src_backend = self.registry.for_format(src_fmt)
        dst_backend = self.registry.for_format(dst_fmt)
        result.reference_ir = self._dump_ir(src_backend, src, run_dir, "source", result)
        result.paths["reference_ir"] = str(run_dir / "ir-source.json")
        self._emit("source_read", app=src_backend.name)

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
                        options=self._options({"step_variant": step_variant}),
                    ),
                    workdir=run_dir / src_backend.name,
                    timeout=self.timeout,
                )
                attempt.export_ok = exported.ok
                attempt.export_seconds = exported.duration_s
                attempt.log = exported.log
                result.warnings.extend(f"export {exchange}: {w}" for w in exported.warnings)
                self._emit(
                    "export",
                    exchange=exchange,
                    ok=exported.ok,
                    seconds=round(exported.duration_s, 2),
                    error=exported.error,
                )
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
                    Job(
                        "import",
                        input=str(exported_path),
                        output=str(dst),
                        format=dst_fmt,
                        options=self._options(),
                    ),
                    workdir=run_dir / dst_backend.name,
                    timeout=self.timeout,
                )
                attempt.import_ok = imported.ok
                attempt.import_seconds = imported.duration_s
                attempt.log = imported.log or attempt.log
                result.warnings.extend(f"import {exchange}: {w}" for w in imported.warnings)
                self._emit(
                    "import",
                    exchange=exchange,
                    ok=imported.ok,
                    seconds=round(imported.duration_s, 2),
                    error=imported.error,
                )
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
            self._emit("target_read", app=dst_backend.name)
        except SwnxError as exc:
            result.warnings.append(f"could not read the converted part back: {exc.message}")

        if not keep_intermediate:
            for attempt in result.attempts:
                if attempt.file and Path(attempt.file).exists():
                    Path(attempt.file).unlink(missing_ok=True)

    def _import_only(self, src, dst, dst_fmt, step_variant, run_dir, result) -> None:
        dst_backend = self.registry.for_format(dst_fmt)
        imported = dst_backend.run(
            Job(
                "import",
                input=str(src),
                output=str(dst),
                format=dst_fmt,
                options=self._options({"step_variant": step_variant}),
            ),
            workdir=run_dir / dst_backend.name,
            timeout=self.timeout,
        )
        imported.require_ok()
        self._emit("import", exchange=src.suffix.lstrip("."), ok=True, seconds=round(imported.duration_s, 2))
        result.ok = True
        result.attempts.append(
            ConversionAttempt(exchange=src.suffix.lstrip("."), ok=True, import_ok=True, import_seconds=imported.duration_s, file=str(src), log=imported.log)
        )
        candidate = self._read_native(dst_backend, dst, run_dir)
        result.candidate_ir = candidate
        result.paths["candidate_ir"] = str(ir.dump(candidate, run_dir / "ir-target.json"))
        self._emit("target_read", app=dst_backend.name)
        result.warnings.append("source is an exchange format: no independent reference part, so property comparison is informational only")

    def _export_only(self, src, src_fmt, dst, dst_fmt, step_variant, run_dir, result) -> None:
        src_backend = self.registry.for_format(src_fmt)
        result.reference_ir = self._dump_ir(src_backend, src, run_dir, "source", result)
        result.paths["reference_ir"] = str(run_dir / "ir-source.json")
        self._emit("source_read", app=src_backend.name)
        exported = src_backend.run(
            Job(
                "export",
                input=str(src),
                output=str(dst),
                format=dst_fmt,
                options=self._options({"step_variant": step_variant, "export_only": True}),
            ),
            workdir=run_dir / src_backend.name,
            timeout=self.timeout,
        )
        exported.require_ok()
        self._emit("export", exchange=dst_fmt, ok=True, seconds=round(exported.duration_s, 2))
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
        self._emit("validate")
        comparison = compare(result.reference_ir, result.candidate_ir, tolerances=tolerances)
        result.comparison = comparison
        result.verdict = comparison.get("verdict", "unknown")
        result.ok = result.verdict in ("pass", "warn")
        self._emit("validated", verdict=result.verdict)
        if report:
            self._write_report(result, task=task)

    def _write_report(self, result: ConversionResult, *, task: str | None) -> None:
        self._emit("report")
        meta = {
            "source": str(result.source),
            "target": str(result.target),
            "target_sha256": result.paths.get("target_sha256"),
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

        # A copy beside the produced part: a user should not have to dig into
        # work/<run-id>/ to find out whether the conversion was good (report P2-1).
        target_path = result.target.resolve()
        near_dir = self._report_dir or target_path.parent
        near_dir.mkdir(parents=True, exist_ok=True)
        stem = target_path.stem
        for key, suffix in (("report_md_near", ".validation.md"), ("report_json_near", ".validation.json")):
            source_path = result.paths.get("report_md" if suffix.endswith(".md") else "report_json")
            if not source_path:
                continue
            try:
                import shutil  # noqa: PLC0415 - only needed here

                destination = near_dir / f"{stem}{suffix}"
                shutil.copyfile(source_path, destination)
                result.paths[key] = str(destination)
            except OSError as exc:  # pragma: no cover - unwritable target dir
                result.warnings.append(f"could not copy the report next to the target: {exc}")

    # ------------------------------------------------------------------ #
    # request validation / preflight
    # ------------------------------------------------------------------ #
    def _validate_request(self, src: Path, dst: Path, src_fmt: str, dst_fmt: str, mode: str, force: bool) -> None:
        """Fail in milliseconds for requests that cannot possibly work.

        Everything here used to surface only after both CAD applications had
        started (report P2-2), which turns a typo into a two-minute wait.
        """
        if not src.exists():
            raise SwnxError(f"source file not found: {src}")
        if src == dst:
            raise ConversionError("source and target are the same file", hint="choose a different target path")
        if dst.exists() and not force:
            raise ConversionError(f"target already exists: {dst}", hint="pass force=true / --force to overwrite")
        if src_fmt == dst_fmt:
            raise ConversionError(
                f"source and target are both {src_fmt}; there is nothing to convert",
                hint="target must be a different format (e.g. .sldprt -> .prt)",
            )
        if src_fmt in ("step", "parasolid") and dst_fmt in ("step", "parasolid"):
            raise ConversionError("source and target are both exchange formats; nothing to convert")
        if mode not in ("auto", *EXCHANGE_EXT):
            raise ConversionError(f"unknown conversion mode {mode!r}", hint="mode: auto | step | parasolid")
        if dst_fmt in EXCHANGE_EXT and mode != "auto" and mode != dst_fmt:
            raise ConversionError(
                f"mode={mode} cannot produce a .{EXCHANGE_EXT[dst_fmt].lstrip('.')} target",
                hint=f"use mode={dst_fmt} or mode=auto",
            )

    def _preflight(self, src_fmt: str, dst_fmt: str) -> None:
        """Ask each involved driver for a *deep* probe before doing any work.

        A deep probe only reports what the driver can actually do; it is the
        cheapest way to turn "starts, runs for 90 s, fails" into "fails in 5 s
        with the reason" (report P0-2/P0-3).
        """
        from .backends.base import format_owner  # noqa: PLC0415

        wanted = {format_owner(src_fmt), format_owner(dst_fmt)} - {None}
        for name in sorted(wanted):
            backend = self.registry.get(name)
            if not backend.available():
                raise SwnxError(
                    f"preflight: {name} is not available",
                    hint="run `swnx-switch probe --deep` for details",
                )
            try:
                probe = backend.run(
                    # keep_app: a successful deep probe should leave the instance it
                    # started for the conversion that follows, otherwise preflight
                    # pays for one cold start and the conversion pays for another.
                    Job("probe", options={"deep": True, "keep_app": True}),
                    workdir=self.workdir / f"preflight-{name}",
                    timeout=min(self.timeout, 180.0),
                )
            except SwnxError as exc:
                raise SwnxError(f"preflight: {name} is not usable: {exc.message}") from exc
            if not probe.ok:
                raise SwnxError(
                    f"preflight: {name} is not usable: {probe.error}",
                    hint=f"driver log: {probe.log}",
                )
            self._emit("preflight", backend=name, ok=True)

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def _emit(self, stage: str, **info: Any) -> None:
        if self._progress is None:
            return
        try:
            self._progress(stage, dict(info))
        except Exception:  # pragma: no cover - a bad callback must not break conversion
            pass

    def _options(self, extra: dict | None = None) -> dict:
        options = dict(extra or {})
        if self._keep_app:
            options["keep_app"] = True
        return options

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

    An explicit ``mode`` always wins: the user asked for that format, so trying
    the other one first would be second-guessing them.
    """
    if mode == "auto":
        if format_owner(src_fmt) == "nx" or format_owner(dst_fmt) == "nx":
            return ["parasolid", "step"]
        return ["step", "parasolid"]
    if mode in EXCHANGE_EXT:
        return [mode]
    raise ConversionError(f"unknown conversion mode {mode!r}", hint="mode: auto | step | parasolid")


def probe_environment(deep: bool = False, timeout: float = 180.0) -> dict:
    """Full environment report used by ``swnx-switch probe`` and the MCP layer.

    ``deep=True`` runs a ``probe`` job in each available driver: that is the only
    honest way to answer "can this application actually start right now" — the
    shallow check only proves the executable and ProgID exist (report P1-3).

    Backends that implement an honest tri-state (``capabilities(deep=True)``, see
    :mod:`swnx_switch.backends.sw`) are asked directly; the others get a plain
    ``probe`` job whose payload is attached as ``deep_probe``.
    """
    from .paths import describe_environment  # noqa: PLC0415

    env = describe_environment()
    registry = BackendRegistry()
    backends: list[dict] = []
    for name in ("nx", "solidworks"):
        try:
            backend = registry.get(name)
        except SwnxError as exc:  # pragma: no cover - broken install
            backends.append({"backend": name, "available": False, "error": exc.message})
            continue
        info: dict | None = None
        if deep:
            try:
                info = backend.capabilities(deep=True)  # type: ignore[call-arg]
            except TypeError:
                info = None
        if info is None:
            info = backend.capabilities()
            if deep and info.get("available"):
                try:
                    probe = backend.run(
                        Job("probe", options={"deep": True}),
                        workdir=ensure_workdir() / f"probe-{name}",
                        timeout=timeout,
                    )
                    info["deep_probe"] = {
                        "ok": probe.ok,
                        "seconds": round(probe.duration_s, 2),
                        "error": probe.error,
                        "log": probe.log,
                        "payload": probe.raw.get("probe") if isinstance(probe.raw, dict) else None,
                    }
                except SwnxError as exc:  # pragma: no cover - driver start failure
                    info["deep_probe"] = {"ok": False, "error": exc.message}
        backends.append(info)
    env["backends"] = backends
    env["deep"] = bool(deep)
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
