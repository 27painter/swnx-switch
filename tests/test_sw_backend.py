"""Tests for the SolidWorks backend and its pywin32 driver.

The default run is **offline**: it checks the command line, the child
environment, the driver's result-file contract and the pure parsing helpers.
Nothing here launches SolidWorks, and no pytest fixtures are used so the file
also runs under the repo's plain-``pywin32`` runner::

    python -m pytest tests/test_sw_backend.py -q
    python scripts/run_tests.py test_sw_backend.py

Live check
----------
Set ``SWNX_SW_LIVE=1`` to add one real ``read`` job against ``SWNX_SW_PART``
(default: the 叉形接头 test part)::

    set SWNX_SW_LIVE=1
    python -m pytest tests/test_sw_backend.py -q

or run this file directly (its own ``--live`` flag)::

    python tests/test_sw_backend.py --live --part "D:\\...\\叉形接头.SLDPRT"
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

try:  # pytest is optional: scripts/run_tests.py runs these functions too
    import pytest
except ImportError:  # pragma: no cover - the plain-runner path
    pytest = None  # type: ignore[assignment]

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:  # the repo is a source checkout, not an install
    sys.path.insert(0, str(SRC))

from swnx_switch import ir  # noqa: E402
from swnx_switch.backends.sw import DRIVER_PATH, SolidWorksBackend  # noqa: E402

DEFAULT_PART = Path(r"D:\27painter\Documents\sw_job\build_final\叉形接头.SLDPRT")
LIVE_WORKDIR = REPO_ROOT / "work" / "sw" / "pytest-live"
SCRATCH_ROOT = REPO_ROOT / "work" / "sw" / "pytest-tmp"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _load_driver_module():
    """Import tools/sw/driver.py as a module (no COM work at import time)."""
    spec = importlib.util.spec_from_file_location("swnx_sw_driver", DRIVER_PATH)
    assert spec and spec.loader, f"cannot load {DRIVER_PATH}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


DRIVER = _load_driver_module()


@contextlib.contextmanager
def _expect_raises(exception_type):
    """pytest.raises without requiring pytest (the repo runner has no pytest)."""
    try:
        yield
    except exception_type:
        return
    raise AssertionError(f"expected {exception_type.__name__} to be raised")


def _live_marker(func):
    """Mark the live test as skipped under pytest; the function self-guards otherwise."""
    if pytest is None:  # pragma: no cover - plain runner
        return func
    return pytest.mark.skipif(not _live_enabled(), reason="set SWNX_LIVE=1 to run the live SolidWorks read")(func)


@contextlib.contextmanager
def scratch(name: str):
    """Per-test scratch directory inside the repo work tree.

    pytest's built-in ``tmp_path`` roots itself in %TEMP%, which the DSH file
    sandbox refuses to scan (WinError 5); a work-tree directory stays writable
    both under the sandbox and on a normal desktop.
    """
    directory = SCRATCH_ROOT / f"{name}-{uuid.uuid4().hex[:8]}"
    directory.mkdir(parents=True, exist_ok=True)
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _live_enabled() -> bool:
    return os.environ.get("SWNX_LIVE") in ("1", "true", "yes") or (
        os.environ.get("SWNX_SW_LIVE") in ("1", "true", "yes")
    )


def _driver_env(**extra: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "PYTHONPATH": os.pathsep.join([str(SRC), env.get("PYTHONPATH", "")]).strip(os.pathsep),
            "PYTHONIOENCODING": "utf-8",
            "SWNX_SW_DRIVER": str(DRIVER_PATH),
        }
    )
    env.update(extra)
    return env


# --------------------------------------------------------------------------- #
# backend metadata and process contract (offline)
# --------------------------------------------------------------------------- #
def test_backend_identity() -> None:
    backend = SolidWorksBackend()
    assert backend.name == "solidworks"
    assert backend.app == "SOLIDWORKS"
    assert backend.supports_native == ("sldprt", "slddrw", "sldasm")
    assert set(backend.supports_import) == {"step", "parasolid"}


def test_driver_path_exists() -> None:
    assert DRIVER_PATH.is_file(), DRIVER_PATH
    assert DRIVER_PATH.name == "driver.py"
    assert DRIVER_PATH.parent.name == "sw"


def test_command_line_shape() -> None:
    with scratch("test_command_line_shape") as tmp:
        backend = SolidWorksBackend(python=r"C:\fake\python.exe")
        command = backend._command(tmp / "request.json", tmp)
        assert command == [r"C:\fake\python.exe", str(DRIVER_PATH)]
        assert Path(command[0]).name.lower().startswith("python")
        # the request travels in the environment, not argv (base.run sets it)
        assert all("request" not in part for part in command)


def test_environment_carries_request_vars() -> None:
    with scratch("test_environment_carries_request_vars") as tmp:
        backend = SolidWorksBackend()
        # Backend.run() injects the request variables *after* _env(); _env() must
        # not invent them.  Other test modules may leave them in os.environ, so
        # clear them around the call to make the assertion independent of order.
        keys = ("SWNX_REQUEST", "SWNX_RESULT", "SWNX_LOG", "SWNX_WORKDIR")
        saved = {key: os.environ.pop(key, None) for key in keys}
        try:
            env = backend._env(tmp)
        finally:
            for key, value in saved.items():
                if value is not None:
                    os.environ[key] = value
        assert str(SRC) in env["PYTHONPATH"].split(os.pathsep)
        assert env["SWNX_SW_DRIVER"] == str(DRIVER_PATH)
        for name in keys:
            assert name not in env
        install = backend.install
        if install is not None:
            assert env["SWNX_SW_EXE"] == str(install.exe)
            assert Path(env["SWNX_SW_EXE"]).is_file()


def test_run_injects_request_environment() -> None:
    """`Backend.run` (not `_env`) is what exports the SWNX_* request variables."""
    from swnx_switch.backends.base import Backend, Job

    with scratch("test_run_injects_request_environment") as tmp:
        child = tmp / "env_probe_child.py"
        child.write_text(
            "import json, os, pathlib\n"
            "workdir = pathlib.Path(os.environ['SWNX_WORKDIR'])\n"
            "(workdir / 'env-capture.json').write_text(json.dumps(dict(os.environ)), encoding='utf-8')\n"
            "pathlib.Path(os.environ['SWNX_RESULT']).write_text(\n"
            "    json.dumps({'ok': True, 'kind': 'probe'}), encoding='utf-8')\n",
            encoding="utf-8",
        )

        class ProbeBackend(Backend):
            name = "probe-backend"
            app = "probe"

            def available(self) -> bool:
                return True

            def app_version(self) -> str:
                return "0"

            def _command(self, request: Path, workdir: Path) -> list[str]:
                return [sys.executable, str(child)]

        result = ProbeBackend().run(Job(kind="probe"), workdir=tmp, timeout=120)
        assert result.ok, result.error
        captured = json.loads((tmp / "env-capture.json").read_text(encoding="utf-8"))
        for name in ("SWNX_REQUEST", "SWNX_RESULT", "SWNX_LOG"):
            assert name in captured, name
            assert Path(captured[name]).parent == tmp, (name, captured[name])
        assert captured["SWNX_REQUEST"].endswith(".json")
        assert captured["SWNX_RESULT"].endswith(".json")
        assert captured["SWNX_LOG"].endswith(".txt")
        assert captured["SWNX_WORKDIR"] == str(tmp)
        assert captured["PYTHONUNBUFFERED"] == "1"


def test_python_executable_is_absolute() -> None:
    backend = SolidWorksBackend()
    python = backend.python_executable
    assert Path(python).is_absolute()
    assert Path(python).exists()
    # an explicit interpreter wins over the caller's own interpreter
    assert SolidWorksBackend(python=r"C:\other\python.exe").python_executable == r"C:\other\python.exe"


def test_available_is_boolean_and_version_parses() -> None:
    backend = SolidWorksBackend()
    available = backend.available()
    assert isinstance(available, bool)
    version = backend.app_version()
    assert isinstance(version, str) and version
    if available and version != "unknown":
        parts = version.split(".")
        assert len(parts) >= 3 and all(part.isdigit() for part in parts[:3])


def test_capabilities_report_driver_and_python() -> None:
    backend = SolidWorksBackend()
    caps = backend.capabilities()
    assert caps["backend"] == "solidworks"
    assert caps["driver"].endswith("driver.py")
    assert caps["available"] is backend.available()
    assert caps["native_formats"] == ["sldprt", "slddrw", "sldasm"]


# --------------------------------------------------------------------------- #
# driver module helpers (offline)
# --------------------------------------------------------------------------- #
def test_driver_job_kinds() -> None:
    assert set(DRIVER.JOB_KINDS) == {"probe", "read", "export", "import"}
    assert DRIVER.PROGID == "SldWorks.Application"


def test_driver_unit_constants() -> None:
    assert DRIVER.SW_DOC_TYPE[".sldprt"] == 1
    assert DRIVER.SW_DOC_TYPE[".sldasm"] == 2
    assert DRIVER.SW_DOC_TYPE[".slddrw"] == 3
    assert DRIVER.SW_OPEN_SILENT == 1
    assert DRIVER.SW_OPEN_READONLY == 2
    assert DRIVER.SW_BODY_ALL == -1
    assert DRIVER.SW_BODY_SOLID == 0
    # swStepAP stores the AP number itself (203/214/242), verified from the
    # exported FILE_SCHEMA in work/sw/out/acceptance_summary.json.
    assert DRIVER.STEP_AP_VALUES == {"ap203": 203, "ap214": 214, "ap242": 242}
    assert DRIVER.STEP_AP_NAMES[242] == "ap242"


def test_driver_metre_to_millimetre_conversions() -> None:
    assert DRIVER.mm_from_m(0.025) == 25.0
    assert DRIVER.mm2_from_m2(0.0004) == 400.0
    assert DRIVER.mm3_from_m3(1e-6) == 1000.0
    assert DRIVER.mm_from_m(None) is None
    assert DRIVER._round_vec([0.1, 0.2, 0.3, 0.4]) == [0.1, 0.2, 0.3]


def test_driver_surface_code_mapping() -> None:
    mapping = DRIVER.SURFACE_KINDS
    assert mapping[4001] == "plane"
    assert mapping[4002] == "cylinder"
    assert mapping[4003] == "cone"
    assert mapping[4004] == "sphere"
    assert mapping[4005] == "torus"
    assert mapping[4006] == "other"


def test_driver_open_result_unpacking() -> None:
    class _Ref:
        def __init__(self, value: int = 0) -> None:
            self.value = value

    model, errors, warnings = DRIVER._unpack_open_result("MODEL", _Ref(), _Ref())
    assert model == "MODEL" and errors.value == 0
    model, errors, warnings = DRIVER._unpack_open_result(("MODEL", 7, 3), _Ref(), _Ref())
    assert model == "MODEL"
    assert errors.value == 7
    assert warnings.value == 3


def test_driver_member_helpers() -> None:
    class PropertyFeature:
        Name = "Boss-Extrude1"
        GetTypeName2 = "Extrusion"

    class MethodFeature:
        def Name(self):  # noqa: N802 - mimics a COM method
            return "Sketch1"

    assert DRIVER.try_member(PropertyFeature(), "Name") == "Boss-Extrude1"
    assert DRIVER.try_member(PropertyFeature(), "GetTypeName2") == "Extrusion"
    assert DRIVER.try_member(MethodFeature(), "Name") == "Sketch1"
    assert DRIVER.try_member(PropertyFeature(), "MissingMember", default="fallback") == "fallback"
    assert DRIVER.as_list(None) == []
    assert DRIVER.as_list(("a", "b")) == ["a", "b"]
    assert DRIVER._f("2.5") == 2.5
    assert DRIVER._f(object()) is None


def test_driver_member_does_not_call_object_valued_properties() -> None:
    """Regression for the read-job failure: `model.Extension` is a property.

    pywin32 hands back the CDispatch itself, and CDispatch is callable (its
    default member), so calling the value raised DISP_E_MEMBERNOTFOUND.  member()
    must return an object-valued property untouched and still call real methods.
    """

    class DefaultMemberObject:
        _oleobj_ = object()  # makes it look like a COM object to the helper
        calls = 0

        def __call__(self):  # pywin32 default-member invocation
            type(self).calls += 1
            raise AssertionError("member() must not call an object-valued property")

    class Model:
        def __init__(self) -> None:
            self.extension_value = DefaultMemberObject()

        @property
        def Extension(self):
            return self.extension_value

        def GetTitle(self) -> str:
            return "part.SLDPRT"

        def GetPartBox(self, use_doc_units: bool):
            return (0.0, 0.0, 0.0, use_doc_units and 1.0 or 1.0, 1.0, 1.0)

    model = Model()
    extension = DRIVER.member(model, "Extension")
    assert extension is model.extension_value
    assert DefaultMemberObject.calls == 0
    assert DRIVER.member(model, "GetTitle") == "part.SLDPRT"
    assert DRIVER.member(model, "GetPartBox", True) == (0.0, 0.0, 0.0, 1.0, 1.0, 1.0)


def test_step_schema_mapping() -> None:
    """The AP is derived from FILE_SCHEMA, which is how the export is judged."""
    with scratch("test_step_schema_mapping") as tmp:
        cases = {
            "CONFIG_CONTROL_DESIGN": "ap203",
            "AUTOMOTIVE_DESIGN": "ap214",
            "AP242_MANAGED_MODEL_BASED_3D_ENGINEERING_MIM_LF": "ap242",
        }
        for schema, expected in cases.items():
            step = tmp / f"{expected}.step"
            step.write_text(
                f"ISO-10303-21;\nHEADER;\nFILE_SCHEMA(('{schema}'));\nENDSEC;\nDATA;\nENDSEC;\n",
                encoding="utf-8",
            )
            found_schema, ap, head = DRIVER._step_schema_of(step)
            assert found_schema == schema
            assert ap == expected, (schema, ap)
            assert "ISO-10303" in head


def test_verify_step_accepts_real_header() -> None:
    with scratch("test_verify_step_accepts_real_header") as tmp:
        step = tmp / "sample.step"
        step.write_text(
            "ISO-10303-21;\n"
            "HEADER;\n"
            "FILE_DESCRIPTION(('x'),'2;1');\n"
            "FILE_NAME('sample.step','2026-01-01T00:00:00',(''),(''),'SolidWorks','','');\n"
            "FILE_SCHEMA(('AP242_MANAGED_MODEL_BASED_3D_ENGINEERING_MIM_LF { 1 0 10303 442 1 1 4 }'));\n"
            "ENDSEC;\n"
            "DATA;\n"
            "#1 = ADVANCED_FACE('',(#2),#3,.T.);\n"
            "ENDSEC;\n"
            "END-ISO-10303-21;\n",
            encoding="utf-8",
        )
        report = DRIVER.verify_step(step)
        assert report["has_advanced_face"] is True
        assert report["step_ap"] == "AP242"
        assert report["size_bytes"] == step.stat().st_size


def test_verify_step_rejects_empty_and_non_step() -> None:
    with scratch("test_verify_step_rejects_empty_and_non_step") as tmp:
        empty = tmp / "empty.step"
        empty.write_bytes(b"")
        with _expect_raises(DRIVER.DriverError):
            DRIVER.verify_step(empty)
        fake = tmp / "fake.step"
        fake.write_text("not a step file", encoding="utf-8")
        with _expect_raises(DRIVER.DriverError):
            DRIVER.verify_step(fake)
        without_faces = tmp / "faces.step"
        without_faces.write_text(
            "ISO-10303-21;\nHEADER;\nFILE_SCHEMA(('AP214'));\nENDSEC;\nDATA;\nENDSEC;\n", encoding="utf-8"
        )
        with _expect_raises(DRIVER.DriverError):
            DRIVER.verify_step(without_faces)


def test_process_liveness_helpers() -> None:
    """A stale PID must read as gone so ExitApp does not cost 20 s per job."""
    assert DRIVER._process_alive(0) is False
    assert DRIVER._wait_for_process_exit(0, 1.0) is True


def test_wait_step_valid_retries_transient_invalid_file() -> None:
    """A SaveAs file the exporter is still closing must not fail a good export."""
    import threading

    valid = (
        "ISO-10303-21;\nHEADER;\nFILE_SCHEMA(('AUTOMOTIVE_DESIGN'));\nENDSEC;\n"
        "DATA;\n#1 = ADVANCED_FACE('',(),.T.);\nENDSEC;\nEND-ISO-10303-21;\n"
    )
    with scratch("test_wait_step_valid_retries") as tmp:
        step = tmp / "late.step"
        step.write_text("still being written", encoding="utf-8")

        def finish_writing() -> None:
            time.sleep(0.4)
            step.write_text(valid, encoding="utf-8")

        thread = threading.Thread(target=finish_writing)
        thread.start()
        try:
            report, problem = DRIVER._wait_step_valid(step, timeout=3.0)
        finally:
            thread.join()
        assert problem is None, problem
        assert report is not None and report["has_advanced_face"] is True
        assert report["step_ap"] == "AP214"

    with scratch("test_wait_step_valid_times_out") as tmp:
        broken = tmp / "broken.step"
        broken.write_text("not a STEP file", encoding="utf-8")
        report, problem = DRIVER._wait_step_valid(broken, timeout=0.3)
        assert report is None
        assert problem and "ISO-10303" in problem


def test_verify_parasolid() -> None:
    with scratch("test_verify_parasolid") as tmp:
        x_t = tmp / "part.x_t"
        x_t.write_text("**ABCDEFGHIJKLMNOPQRSTUVWXYZ\nTRANSMIT FILE created by test\n", encoding="utf-8")
        report = DRIVER.verify_parasolid(x_t)
        assert report["has_transmit_file"] is True
        with _expect_raises(DRIVER.DriverError):
            DRIVER.verify_parasolid(tmp / "missing.x_t")


# --------------------------------------------------------------------------- #
# driver process contract: always write SWNX_RESULT, also on failure (offline)
# --------------------------------------------------------------------------- #
def _run_driver(tmp: Path, request: dict | None, *, with_request_env: bool = True) -> tuple[int, dict | None, str]:
    result = tmp / "result.json"
    log = tmp / "driver.log"
    request_path = tmp / "request.json"
    if request is not None:
        request_path.write_text(json.dumps(request), encoding="utf-8")
    env = _driver_env()
    env["SWNX_RESULT"] = str(result)
    env["SWNX_LOG"] = str(log)
    if with_request_env:
        env["SWNX_REQUEST"] = str(request_path)
    else:
        env.pop("SWNX_REQUEST", None)
    completed = subprocess.run(
        [sys.executable, str(DRIVER_PATH)],
        env=env,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=120,
    )
    payload = json.loads(result.read_text(encoding="utf-8")) if result.exists() else None
    return completed.returncode, payload, log.read_text(encoding="utf-8", errors="replace")


def test_driver_unknown_kind_writes_failure_result() -> None:
    with scratch("test_driver_unknown_kind") as tmp:
        code, payload, log = _run_driver(tmp, {"kind": "not-a-job"})
        assert code == 1
        assert payload is not None, log
        assert payload["ok"] is False
        assert payload["kind"] == "not-a-job"
        assert payload["backend"] == "solidworks"
        assert "unknown job kind" in (payload["error"] or "")
        assert "supported kinds" in (payload.get("hint") or "")
        assert "RESULT ok=False" in log


def test_driver_missing_request_env_writes_failure_result() -> None:
    with scratch("test_driver_missing_request") as tmp:
        code, payload, log = _run_driver(tmp, None, with_request_env=False)
        assert code == 1
        assert payload is not None, log
        assert payload["ok"] is False
        assert "SWNX_REQUEST" in (payload["error"] or "")


def test_driver_invalid_request_json_writes_failure_result() -> None:
    with scratch("test_driver_invalid_request") as tmp:
        result = tmp / "result.json"
        log = tmp / "driver.log"
        request = tmp / "request.json"
        request.write_text("{not json", encoding="utf-8")
        env = _driver_env()
        env.update({"SWNX_RESULT": str(result), "SWNX_LOG": str(log), "SWNX_REQUEST": str(request)})
        completed = subprocess.run(
            [sys.executable, str(DRIVER_PATH)],
            env=env,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=120,
        )
        assert completed.returncode == 1
        payload = json.loads(result.read_text(encoding="utf-8"))
        assert payload["ok"] is False
        assert "request JSON is invalid" in payload["error"]


def test_driver_request_arg_overrides_env() -> None:
    with scratch("test_driver_request_arg") as tmp:
        result = tmp / "result.json"
        log = tmp / "driver.log"
        request = tmp / "request.json"
        request.write_text(json.dumps({"kind": "not-a-job"}), encoding="utf-8")
        env = _driver_env()
        env.update({"SWNX_RESULT": str(result), "SWNX_LOG": str(log)})
        completed = subprocess.run(
            [sys.executable, str(DRIVER_PATH), "--request", str(request)],
            env=env,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=120,
        )
        assert completed.returncode == 1
        assert json.loads(result.read_text(encoding="utf-8"))["kind"] == "not-a-job"


# --------------------------------------------------------------------------- #
# live: one real read job (opt-in)
# --------------------------------------------------------------------------- #
@_live_marker
def test_live_read_generates_valid_ir() -> None:
    if not _live_enabled():  # plain runner (scripts/run_tests.py) does not use pytest markers
        return
    part = Path(os.environ.get("SWNX_SW_PART", str(DEFAULT_PART)))
    assert part.is_file(), f"test part not found: {part}"
    LIVE_WORKDIR.mkdir(parents=True, exist_ok=True)
    output = LIVE_WORKDIR / f"{part.stem}.ir.json"
    backend = SolidWorksBackend()
    assert backend.available(), "SolidWorks is not available on this machine"
    started = time.time()
    result = backend.read(part, output=output, workdir=LIVE_WORKDIR, timeout=900.0)
    duration = time.time() - started
    assert result.ok, f"read failed after {duration:.1f}s: {result.error}\n{result.log}"
    assert result.log and Path(result.log).is_file()
    log_text = Path(result.log).read_text(encoding="utf-8", errors="replace")
    assert "TOKEN READ_OK" in log_text
    doc = result.ir
    assert doc is not None
    assert ir.validate(doc) == []
    summary = ir.summarize(doc)
    assert summary["bodies"] >= 1, summary
    assert summary["faces"] > 0, summary
    assert summary["features"] > 0, summary
    assert summary["volume_mm3"] and summary["volume_mm3"] > 0, summary
    assert summary["bbox_size_mm"] and len(summary["bbox_size_mm"]) == 3, summary
    assert output.is_file() and output.stat().st_size > 0
    assert ir.load(output)["schema"] == doc["schema"]
    print(
        f"\nlive read: {part.name} -> {output} ({output.stat().st_size} bytes) in {duration:.1f}s\n"
        f"summary: {json.dumps(summary, ensure_ascii=False)}"
    )


# --------------------------------------------------------------------------- #
# direct runner: python tests/test_sw_backend.py --live
# --------------------------------------------------------------------------- #
def _run_cli(argv: list[str]) -> int:
    import argparse

    if pytest is None:  # pragma: no cover - pytest is the runner for this path
        print("pytest is not installed; use scripts/run_tests.py instead", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description="offline/live tests for the SolidWorks backend")
    parser.add_argument("--live", action="store_true", help="also run one real SolidWorks read")
    parser.add_argument("--part", default=str(DEFAULT_PART), help="part used by the live read")
    args = parser.parse_args(argv)
    pytest_args = [__file__, "-q"]
    if args.live:
        os.environ["SWNX_LIVE"] = "1"
        os.environ["SWNX_SW_PART"] = args.part
        pytest_args += ["-s", "-k", "live"]
    return pytest.main(pytest_args)


if __name__ == "__main__":
    sys.exit(_run_cli(sys.argv[1:]))
