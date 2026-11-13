"""NX backend tests.

Two layers, deliberately separated:

* everything here runs **without NX** — command-line quoting, the environment
  redirections, capability advertising, the journal's job handler table and wire
  protocol, and the ``JobResult`` parsing contract the journal has to satisfy.
  Those are the parts a refactor breaks silently.
* the ``live_*`` tests start real batch NX (30-180 s each).  Set ``SWNX_LIVE=1``
  to include them; a machine without NX, or a sandbox that denies the licence
  files, simply skips them.

Run with pytest::

    $env:PYTHONPATH = "D:\\27painter\\Documents\\swnx-switch\\.tools"
    python -m pytest tests/test_nx_backend.py -q

or without pytest (the repo's own runner, same tests)::

    python scripts/run_tests.py test_nx_backend.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from swnx_switch import ir  # noqa: E402
from swnx_switch.backends.base import Backend, HARD_FAILURES, Job  # noqa: E402
from swnx_switch.backends.nx import (  # noqa: E402
    NXBackend,
    _version_for_root,
    release_of,
)
from swnx_switch.errors import BackendError  # noqa: E402

SCRATCH = REPO / "work" / "nx" / "tests-scratch"
DRIVER = REPO / "tools" / "nx" / "driver.py"
PROBE_PART = Path(r"D:\27painter\Documents\nx_sw_test\probe_part.prt")
SW_STEP = Path(r"D:\27painter\Documents\sw_job\tooltest\skilltest_bracket.STEP")
LIVE = os.environ.get("SWNX_LIVE") == "1"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _scratch(name: str) -> Path:
    """A fresh directory under ``work/nx/tests-scratch``.

    ``tempfile.mkdtemp`` must not be used here: under the DSH Windows sandbox the
    0700 directory it creates is not writable by the test process (the sandbox
    re-checks ACLs per path), while a plain ``mkdir`` inherits the writable
    workspace ACL.  Scratch dirs are left behind on purpose -- deleting them is
    also denied in a restricted session, and they are inside the workspace.
    """
    SCRATCH.mkdir(parents=True, exist_ok=True)
    folder = SCRATCH / ("%s-%s" % (name, uuid.uuid4().hex[:8]))
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _load_driver():
    """Import ``tools/nx/driver.py`` in the host interpreter.

    The journal only imports NXOpen lazily, so its module body (handler table,
    wire protocol, STEP sniffer) is host-testable -- that is the point of
    keeping NXOpen imports inside ``_import_nxopen()``.
    """
    SCRATCH.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("SWNX_LOG", str(SCRATCH / "driver-import.log"))
    spec = importlib.util.spec_from_file_location("swnx_nx_driver", DRIVER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _fake_ir() -> dict:
    doc = ir.new(
        backend="nx",
        app="Siemens NX",
        app_version="2406",
        source_path=PROBE_PART if PROBE_PART.exists() else None,
        source_format="prt",
    )
    ir.set_bbox(doc, min_mm=[0, 0, 0], max_mm=[10, 20, 30])
    ir.set_mass_properties(doc, volume_mm3=6000.0, surface_area_mm2=2200.0, mass_kg=0.0471)
    ir.add_body(doc, name="Body_1", kind="solid", faces=6, edges=12, vertices=8)
    ir.add_feature(doc, name="Block(1)", type="Block")
    ir.add_surface(doc, "plane", count=6, normal=[0, 0, 1])
    ir.add_custom_properties(doc, {"Description": "fake"})
    ir.finish(doc, duration_s=0.1, driver={"journal": "fake"})
    return doc


def _fake_result(payload: dict, exit_code: int = 0) -> str:
    """A driver script good enough to exercise ``Backend.run``'s parsing."""
    body = json.dumps(payload, ensure_ascii=True)
    lines = [
        "import json, os, sys",
        "payload = json.loads(%r)" % body,
        "with open(os.environ['SWNX_RESULT'], 'w', encoding='utf-8') as handle:",
        "    json.dump(payload, handle)",
        "sys.exit(%d)" % exit_code,
    ]
    return "\n".join(lines) + "\n"


class _FakeBackend(Backend):
    """Runs a local Python script instead of NX; proves the result contract."""

    name = "nx"
    app = "Siemens NX"

    def __init__(self, script: Path, exit_code: int = 0) -> None:
        self.script = script
        self.exit_code = exit_code

    def available(self) -> bool:
        return True

    def app_version(self) -> str:
        return "2406"

    def _command(self, request, workdir):
        return [sys.executable, str(self.script)]


# --------------------------------------------------------------------------- #
# install discovery (no NX start)
# --------------------------------------------------------------------------- #
def test_release_parsing() -> None:
    assert release_of("2406.1700.0.0") == "2406"
    assert release_of("12.0.0.27") == "12.0"
    assert release_of("2506.4000.0.0") == "2506"
    assert release_of(None) is None
    assert release_of("") is None
    # The fallback reads the install folder name, which varies by installer.
    assert _version_for_root(r"D:\UG NX2406") == "2406"
    assert _version_for_root(r"C:\Program Files\Siemens\NX 12.0") == "12.0"
    assert _version_for_root(r"D:\nowhere") == "unknown"


def test_available_and_version() -> None:
    backend = NXBackend()
    if not backend.available():
        # A machine without NX must still pass the offline suite.
        return
    install = backend.install
    assert install is not None
    assert install.run_journal.exists()
    assert install.ugiicmd.exists()
    version = backend.app_version()
    assert version != "unknown", "app_version must be readable without starting NX"
    assert version in ("12.0", "2406", "2506") or version.replace(".", "").isdigit()


def test_capabilities_are_honest() -> None:
    caps = NXBackend().capabilities()
    assert caps["backend"] == "nx"
    assert caps["app"] == "Siemens NX"
    assert caps["native_formats"] == ["prt"]
    # NX 2406 has STEP 203/214/242 and Parasolid importers in DexManager, and no
    # SolidWorks importer -- advertising sldprt here would be a lie the pipeline
    # would act on.
    assert "step" in caps["import_formats"]
    assert "parasolid" in caps["import_formats"]
    assert "sldprt" not in caps["import_formats"]


# --------------------------------------------------------------------------- #
# command line and environment
# --------------------------------------------------------------------------- #
def test_command_line_quoting() -> None:
    backend = NXBackend()
    install = backend.install
    if install is None:
        return
    workdir = _scratch("cmd")
    command = backend._command(workdir / "request.json", workdir)
    # A single *string* is the whole point: cmd /s strips the first and last
    # quote, so the doubled leading quote leaves a correctly quoted command.
    assert isinstance(command, str)
    expected_inner = '"%s" "%s" && "%s" "%s"' % (
        install.ugiicmd,
        install.root,
        install.run_journal,
        backend.driver_path(),
    )
    assert command == 'cmd.exe /d /s /c "%s"' % expected_inner
    assert command.startswith('cmd.exe /d /s /c ""')
    assert command.endswith('""')
    assert "run_journal.exe" in command
    # list2cmdline escaping (\\") is exactly what makes cmd.exe reject the line.
    assert '\\"' not in command


def test_environment_redirections() -> None:
    backend = NXBackend()
    workdir = _scratch("env")
    env = backend._env(workdir)
    install = backend.install
    if install is not None:
        assert env["NX_HOME"] == str(install.root)
        assert env["UGII_BASE_DIR"] == str(install.root)
    root = str(workdir.resolve())
    # The two directories that must not fall back to the user's NX profile: an
    # unattended run would otherwise write there (and, on a locked-down machine,
    # fail to start).
    for key in ("UGII_USER_PROFILE_DIR", "UGII_TMP_DIR"):
        assert key in env, "the journal needs %s redirected" % key
        assert Path(env[key]).is_dir(), "%s must exist before NX starts" % key
        assert str(Path(env[key]).resolve()).startswith(root), "%s must be inside the workdir" % key
    assert env["PYTHONUNBUFFERED"] == "1"
    assert env["PYTHONIOENCODING"] == "utf-8"
    assert str(REPO / "src") in env.get("PYTHONPATH", "")
    assert env.get("SWNX_NX_RELEASE")


def test_probe_environment_is_nx_free() -> None:
    from swnx_switch.backends.nx import probe_environment

    info = probe_environment()
    assert "available" in info
    if info["available"]:
        assert info["run_journal"].lower().endswith("run_journal.exe")
        assert info["driver"].lower().endswith("driver.py")
        assert info["release"] != "unknown"


# --------------------------------------------------------------------------- #
# journal (imported, never run under NX here)
# --------------------------------------------------------------------------- #
def test_driver_registers_every_job_kind() -> None:
    driver = _load_driver()
    assert sorted(driver.HANDLERS) == ["export", "import", "probe", "read"]


def test_step_strategy_table_matches_verified_batch_behaviour() -> None:
    """The table encodes what real batch NX 2406 did on this machine.

    Every STEP strategy writes metadata-only output here (0 ADVANCED_FACE / 0
    solids input), so the ordered attempts are a best effort before the Parasolid
    fallback.  AP242 ED2 must be first (this install only ships the AP242_ED2
    translator def) and every attempt must select objects explicitly — the bare
    default wrote a header-only file as well.
    """
    driver = _load_driver()
    strategies = driver.STEP_STRATEGIES
    assert strategies, "no STEP strategies"
    labels = [row["label"] for row in strategies]
    assert len(labels) == len(set(labels))
    assert strategies[0].get("ap242_ed2") is True
    assert strategies[0].get("settings", "").lower().endswith("ugstep242.def")
    # AP214 + EntirePart is the combination verified to at least write a file.
    ap214_whole_part = [
        row for row in strategies
        if not row.get("ap242") and not row.get("ap242_ed2")
        and row.get("scope") == "EntirePart" and row.get("export_from") == "DisplayPart"
    ]
    assert len(ap214_whole_part) == 1
    assert labels.index(ap214_whole_part[0]["label"]) <= 1
    # No strategy may rely on the (empty) default selection.
    assert all(row["scope"] for row in strategies)
    assert len(strategies) <= 4, "each STEP attempt costs ~11 s in batch NX"


def test_parasolid_import_prefers_uf_over_dex() -> None:
    """Order matters: DEX ParasolidImporter is a silent no-op in batch NX 2406.

    A dedicated probe journal on this machine showed
    ``CreateParasolidImporter().Commit()`` returning without error while the work
    part stayed at 0 bodies, and ``ufs.Ps.ImportData(file)`` (one argument)
    putting the bodies into the work part immediately.  The journal must try UF
    first and keep DEX only as a fallback.
    """
    source = DRIVER.read_text(encoding="utf-8")
    call_uf = source.index("ufs.Ps.ImportData(src)")
    call_dex = source.index("session.DexManager.CreateParasolidImporter()")
    assert call_uf < call_dex, "UF PS import must be attempted before the DEX importer"


def test_normalize_release() -> None:
    driver = _load_driver()
    # NX 2406 reports UGII_VERSION as "v2406"; the IR wants "2406".
    assert driver._normalize_release("v2406") == "2406"
    assert driver._normalize_release("V2406") == "2406"
    assert driver._normalize_release("2406") == "2406"
    assert driver._normalize_release("") == "unknown"


def test_import_to_enum_member_resolves_workpart_and_newpart() -> None:
    driver = _load_driver()

    class _Option:
        WorkPart = "work-part"
        NewPart = "new-part"

    class _Importer:
        ImportToOption = _Option

    class _NxOpen:
        Step203Importer = _Importer

    assert driver._import_to_enum_member(_NxOpen, "WorkPart") == "work-part"
    assert driver._import_to_enum_member(_NxOpen, "NewPart") == "new-part"
    assert driver._import_to_enum_member(_NxOpen, "Nope") is None

    class _Empty:
        pass

    assert driver._import_to_enum_member(_Empty, "WorkPart") is None


def test_import_nxopen_loads_the_uf_submodule() -> None:
    """Regression: ``import NXOpen`` alone leaves ``NXOpen.UF`` unset.

    Real batch NX failed with ``AttributeError: module 'NXOpen' has no attribute
    'UF'`` on the first journal action.  A stub package reproduces the bug's
    precondition (no UF attribute after a bare import) and proves the driver's
    central import loads it.
    """
    driver = _load_driver()
    folder = _scratch("fake-nx")
    package = folder / "NXOpen"
    package.mkdir()
    (package / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (package / "UF.py").write_text(
        "class UFSession:\n"
        "    @staticmethod\n"
        "    def GetUFSession():\n"
        "        return 'ufs'\n",
        encoding="utf-8",
    )

    def drop_nxopen():
        for name in [n for n in sys.modules if n == "NXOpen" or n.startswith("NXOpen.")]:
            del sys.modules[name]

    sys.path.insert(0, str(folder))
    try:
        drop_nxopen()
        import NXOpen  # noqa: PLC0415

        assert not hasattr(NXOpen, "UF"), "stub must reproduce the unloaded submodule"
        module = driver._import_nxopen()
        assert hasattr(module, "UF")
        assert module.UF.UFSession.GetUFSession() == "ufs"
    finally:
        sys.path.remove(str(folder))
        drop_nxopen()


def test_driver_wire_protocol_round_trip() -> None:
    driver = _load_driver()
    folder = _scratch("wire")
    request_path = folder / "request.json"
    result_path = folder / "result.json"
    log_path = folder / "log.txt"
    request = Job(kind="read", input="in.prt", output="out.json").as_dict()
    request_path.write_text(json.dumps(request), encoding="utf-8")

    old = {key: os.environ.get(key) for key in ("SWNX_REQUEST", "SWNX_RESULT", "SWNX_LOG", "SWNX_WORKDIR")}
    try:
        os.environ["SWNX_REQUEST"] = str(request_path)
        os.environ["SWNX_RESULT"] = str(result_path)
        os.environ["SWNX_LOG"] = str(log_path)
        os.environ["SWNX_WORKDIR"] = str(folder)
        driver.REQUEST_PATH = str(request_path)
        driver.RESULT_PATH = str(result_path)
        driver.LOG_PATH = str(log_path)
        driver.WORKDIR = str(folder)
        assert driver.read_request() == request
        driver.say("hello")
        payload = driver.new_payload("read")
        payload["ok"] = True
        payload["ir"] = _fake_ir()
        driver.write_result(payload)
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    assert log_path.read_text(encoding="utf-8").splitlines()[0] == "hello"
    written = json.loads(result_path.read_text(encoding="utf-8"))
    assert written["ok"] is True
    assert written["backend"] == "nx"
    assert ir.validate(written["ir"]) == []


def test_driver_never_emits_fatal_log_tokens() -> None:
    source = DRIVER.read_text(encoding="utf-8")
    # Backend.run fails a run whose log contains these; the journal must not
    # print them for a handled per-section failure.
    for token in HARD_FAILURES:
        if token == "Traceback (most recent call last)":
            # Only the renaming call in caught() may contain it.
            assert source.count(token) == 1, token
        else:
            assert token not in source, token


def test_driver_is_ascii() -> None:
    # run_journal.exe runs the journal on a console with a non-UTF-8 code page;
    # a stray edit with a smart quote turns into a syntax error there.
    DRIVER.read_bytes().decode("ascii")


def test_driver_step_schema_sniffer() -> None:
    driver = _load_driver()
    folder = _scratch("schema")
    ap203 = folder / "ap203.step"
    ap203.write_text(
        "ISO-10303-21;\nHEADER;\nFILE_SCHEMA (( 'CONFIG_CONTROL_DESIGN' ));\nENDSEC;\n",
        encoding="utf-8",
    )
    ap242 = folder / "ap242.step"
    ap242.write_text(
        "ISO-10303-21;\nHEADER;\nFILE_SCHEMA (( 'AP242_MANAGED_MODEL_BASED_3D_ENGINEERING_MIM_LF' ));\n",
        encoding="utf-8",
    )
    ap214 = folder / "ap214.step"
    ap214.write_text("ISO-10303-21;\nFILE_SCHEMA (( 'AUTOMOTIVE_DESIGN' ));\n", encoding="utf-8")
    assert driver._step_schema(str(ap203)) == "ap203"
    assert driver._step_schema(str(ap242)) == "ap242"
    assert driver._step_schema(str(ap214)) == "ap214"
    assert driver._step_schema(str(folder / "missing.step")) == "unknown"
    if SW_STEP.exists():
        # The real SolidWorks export this project converts; verified by header.
        assert driver._step_schema(str(SW_STEP)) == "ap203"


def test_face_type_table_covers_classic_uf_codes() -> None:
    driver = _load_driver()
    # Classic UF_MODL_ask_face_data type codes (22 = planar is confirmed by the
    # live probe on this machine; the rest are the documented UF constants).
    assert driver.FACE_TYPES[22] == "plane"
    assert driver.FACE_TYPES[16] == "cylinder"
    assert driver.FACE_TYPES[17] == "cone"
    assert driver.FACE_TYPES[18] == "sphere"
    assert driver.FACE_TYPES[19] == "torus"
    assert set(driver.FACE_TYPES.values()) <= set(ir.SURFACE_KINDS)


def test_solid_face_type_is_the_authoritative_classifier() -> None:
    driver = _load_driver()

    class _Face:
        def __init__(self, label):
            self.SolidFaceType = label

    assert driver._solid_face_kind(_Face("Plane")) == "plane"
    assert driver._solid_face_kind(_Face("Cylindrical")) == "cylinder"
    assert driver._solid_face_kind(_Face("Conical")) == "cone"
    assert driver._solid_face_kind(_Face("Spherical")) == "sphere"
    # Face.FaceType has no Toroidal member (NX calls those parametric faces), so
    # the caller falls back to the UF type code -- 19 -- to classify them.
    assert driver._solid_face_kind(_Face("Toroidal")) is None
    assert driver.FACE_TYPES[19] == "torus"
    assert driver._solid_face_kind(_Face("Blending")) is None

    class _NoType:
        pass

    assert driver._solid_face_kind(_NoType()) is None


def test_feature_type_and_attribute_helpers() -> None:
    driver = _load_driver()

    class _Feature:
        FeatureType = 70
        Suppressed = False

    assert driver._feature_type_label(_Feature()) == "type_70"

    class _StringFeature:
        FeatureType = "BLOCK"

    assert driver._feature_type_label(_StringFeature()) == "BLOCK"

    class _Attr:
        Type = "String"
        StringValue = "hello"
        RealValue = 0.5
        IntegerValue = 3
        BooleanValue = False

    assert driver._attribute_value(_Attr()) == "hello"

    class _RealAttr:
        Type = "Real"
        StringValue = ""
        RealValue = 12.5
        IntegerValue = 12
        BooleanValue = False

    assert driver._attribute_value(_RealAttr()) == 12.5


# --------------------------------------------------------------------------- #
# the result-document contract Backend.run applies to the NX journal
# --------------------------------------------------------------------------- #
def test_backend_parses_a_well_formed_driver_result() -> None:
    folder = _scratch("parse-ok")
    script = folder / "fake_driver.py"
    payload = {
        "ok": True,
        "kind": "read",
        "backend": "nx",
        "app": "Siemens NX",
        "app_version": "2406",
        "outputs": [str(folder / "out.json")],
        "ir": _fake_ir(),
        "warnings": ["one warning"],
        "error": None,
    }
    script.write_text(_fake_result(payload, exit_code=3), encoding="utf-8")
    # Exit code 3 is the *point*: NX batch exits 0xC0000409 on a clean shutdown
    # and run_journal can exit non-zero after a swallowed exception, so the
    # verdict must come from the result file.
    result = _FakeBackend(script, exit_code=3).run(Job(kind="read"), workdir=folder, timeout=120)
    assert result.ok is True
    assert result.exit_code == 3
    assert result.ir is not None
    assert ir.validate(result.ir) == []
    assert ir.summarize(result.ir)["bodies"] == 1
    assert result.warnings == ["one warning"]
    assert result.app_version == "2406"
    assert result.log and Path(result.log).exists()


def test_backend_reports_a_driver_failure() -> None:
    folder = _scratch("parse-fail")
    script = folder / "fake_driver.py"
    payload = {
        "ok": False,
        "kind": "export",
        "backend": "nx",
        "app_version": "2406",
        "error": "STEP export produced no ADVANCED_FACE entities",
        "warnings": [],
    }
    script.write_text(_fake_result(payload, exit_code=0), encoding="utf-8")
    result = _FakeBackend(script).run(Job(kind="export"), workdir=folder, timeout=120)
    assert result.ok is False
    assert "ADVANCED_FACE" in (result.error or "")
    try:
        result.require_ok()
    except BackendError:
        pass
    else:  # pragma: no cover
        raise AssertionError("a failed result must raise through require_ok()")


def test_backend_without_result_file_raises() -> None:
    folder = _scratch("parse-none")
    script = folder / "fake_driver.py"
    script.write_text("import sys\nsys.exit(1)\n", encoding="utf-8")
    try:
        _FakeBackend(script, exit_code=1).run(Job(kind="probe"), workdir=folder, timeout=120)
    except BackendError as exc:
        assert "no result file" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("a driver that writes no result must raise BackendError")


# --------------------------------------------------------------------------- #
# live (real batch NX; SWNX_LIVE=1)
# --------------------------------------------------------------------------- #
def _live_backend() -> NXBackend | None:
    if not LIVE:
        return None
    backend = NXBackend()
    return backend if backend.available() else None


def test_live_probe() -> None:
    """One NX start: capability facts.  ``SWNX_LIVE=1`` only."""
    backend = _live_backend()
    if backend is None:
        return
    result = backend.probe(PROBE_PART if PROBE_PART.exists() else None, workdir=REPO / "work" / "nx")
    assert result.ok, result.error
    probe = result.raw["probe"]
    importers = probe["session"]["dex_importers"]
    assert any("Step" in name for name in importers), importers
    assert any("Parasolid" in name for name in importers), importers


def test_live_read() -> None:
    """``probe_part.prt`` -> schema-valid IR, without touching the input file."""
    backend = _live_backend()
    if backend is None or not PROBE_PART.exists():
        return
    before = PROBE_PART.stat().st_size, PROBE_PART.stat().st_mtime
    out = REPO / "work" / "nx" / "out" / "live-probe_part.ir.json"
    result = backend.read(PROBE_PART, output=out, workdir=REPO / "work" / "nx")
    assert result.ok, result.error
    assert result.ir is not None
    assert ir.validate(result.ir) == []
    assert ir.summarize(result.ir)["bodies"] >= 1
    assert ir.summarize(result.ir)["volume_mm3"]
    assert out.exists() and out.stat().st_size > 0
    assert (PROBE_PART.stat().st_size, PROBE_PART.stat().st_mtime) == before


def test_live_export_import_round_trip() -> None:
    """STEP AP242 export -> import into a new .prt with bodies > 0."""
    backend = _live_backend()
    if backend is None or not PROBE_PART.exists():
        return
    folder = REPO / "work" / "nx" / "out"
    folder.mkdir(parents=True, exist_ok=True)
    step = folder / "live-probe_part.step"
    reimported = folder / "live-probe_part-reimport.prt"
    exported = backend.export(PROBE_PART, step, workdir=REPO / "work" / "nx")
    assert exported.ok, exported.error
    data = step.read_bytes()
    assert len(data) > 1000
    assert data.count(b"ADVANCED_FACE") > 0
    imported = backend.import_(step, reimported, workdir=REPO / "work" / "nx")
    assert imported.ok, imported.error
    assert (imported.raw.get("import") or {}).get("bodies", 0) >= 1
    assert reimported.exists() and reimported.stat().st_size > 0


def test_live_import_solidworks_step() -> None:
    """The cross-CAD case: a SolidWorks-produced AP203 STEP becomes an NX part."""
    backend = _live_backend()
    if backend is None or not SW_STEP.exists():
        return
    folder = REPO / "work" / "nx" / "out"
    target = folder / "live-skilltest_bracket.prt"
    result = backend.import_(SW_STEP, target, workdir=REPO / "work" / "nx")
    assert result.ok, result.error
    stats = result.raw.get("import") or {}
    assert stats.get("bodies", 0) >= 1
    assert target.exists() and target.stat().st_size > 0
