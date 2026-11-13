"""Run the whole test suite without pytest.

The repo targets machines where only ``pywin32`` is guaranteed, and the DSH file
sandbox can refuse pytest's cache/temp handling.  This runner imports every
``tests/test_*.py``, calls each ``test_*`` function, prints a per-test verdict and
exits non-zero if anything failed.  ``pytest`` still works when it is installed.
"""

from __future__ import annotations

import importlib.util
import sys
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
TESTS = REPO / "tests"


def load_module(path: Path):
    spec = importlib.util.spec_from_file_location(f"test_{path.stem}", path)
    if spec is None or spec.loader is None:  # pragma: no cover
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    pattern = argv[0] if argv else "test_*.py"
    files = sorted(TESTS.glob(pattern))
    if not files:
        print(f"no tests matched {pattern} in {TESTS}", file=sys.stderr)
        return 2

    sys.path.insert(0, str(SRC))
    passed = failed = skipped = 0
    failures: list[tuple[str, str]] = []
    for path in files:
        try:
            module = load_module(path)
        except Exception:
            failed += 1
            failures.append((path.name, traceback.format_exc()))
            print(f"FAIL {path.name}: import failed")
            continue
        names = [name for name in dir(module) if name.startswith("test_") and callable(getattr(module, name))]
        if not names:
            print(f"skip {path.name}: no test_* functions")
            skipped += 1
            continue
        print(f"\n{path.name}")
        for name in names:
            func = getattr(module, name)
            args = func.__code__.co_varnames[: func.__code__.co_argcount]
            if args:  # fixtures are a pytest concept; unsupported here
                print(f"  SKIP {name} (needs pytest fixtures: {', '.join(args)})")
                skipped += 1
                continue
            try:
                func()
                print(f"  ok   {name}")
                passed += 1
            except Exception:
                print(f"  FAIL {name}")
                failures.append((f"{path.name}::{name}", traceback.format_exc()))
                failed += 1

    print(f"\n{passed} passed, {failed} failed, {skipped} skipped")
    for label, detail in failures:
        print(f"\n--- {label} ---\n{detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
