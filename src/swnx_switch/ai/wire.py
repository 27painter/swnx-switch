"""Strict JSON for the tool boundary.

The host validates every tool return value as **lossless JSON**.  That check is
stricter than ``json.dumps``: it rejects ``NaN``/``Infinity`` (not JSON at all),
``-0.0`` (JSON-valid, but ``Object.is(value, -0)`` is true and a JS number would
normalise it), and integers outside the IEEE-754 safe range (a JS number cannot
hold them without changing the value).

Two real failures drove this module:

* 2026-11-14 13:5x — a conversion succeeded but ``swnx_convert_part`` returned
  ``invalid output: value is not lossless JSON`` because a comparison carried
  ``NaN``.
* 2026-11-14 14:2x — ``swnx_probe {deep: true}`` failed the same way *after* that
  first fix, because the deep-probe payload embeds raw driver JSON and NX face
  axes contain ``-0.0``.  ``json.dumps`` happily writes ``-0.0``, so the old
  ``dumps`` fast path let it through unsanitised.

Rules now:

1. :func:`sanitize` normalises everything the host rejects: non-finite float ->
   ``None``, negative zero -> ``0.0``, unsafe integer -> its decimal string,
   non-string mapping keys -> ``str``.
2. :func:`dumps` **always** sanitises before serialising (no fast path: the fast
   path was the bug), and still passes ``allow_nan=False`` as a belt.
3. :func:`loads_strict` / :func:`scan` are the test-side mirror of the host rule,
   so a regression is caught in CI instead of by the user.
"""

from __future__ import annotations

import json
import math
from typing import Any

MAX_DEPTH = 40
#: Integers a JavaScript number can represent exactly (IEEE-754 double).
MAX_SAFE_INT = 2**53


def sanitize(value: Any, _depth: int = 0) -> Any:
    """Return a copy that satisfies the host's lossless-JSON rules.

    Unknown objects are stringified rather than dropped, because a tool result
    that silently loses a field is harder to debug than one with ``"<Path>"``
    in it.
    """
    if _depth > MAX_DEPTH:
        return None
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        if abs(value) > MAX_SAFE_INT:
            # Not representable as a JS number: keep the exact value as text
            # rather than silently rounding it at the client.
            return str(value)
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        if value == 0.0:
            return 0.0  # folds -0.0; the host rejects negative zero
        return value
    if isinstance(value, dict):
        return {key if isinstance(key, str) else str(key): sanitize(item, _depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [sanitize(item, _depth + 1) for item in value]
    # Path is the one non-JSON type that shows up in every payload.
    try:
        from pathlib import Path

        if isinstance(value, Path):
            return str(value)
    except Exception:  # pragma: no cover - Path is always importable
        pass
    return str(value)


def dumps(value: Any, **kwargs: Any) -> str:
    """``json.dumps`` that can never emit a value the host rejects."""
    kwargs.setdefault("ensure_ascii", False)
    return json.dumps(sanitize(value), allow_nan=False, **kwargs)


def parse_constant(token: str) -> Any:
    """``json.loads`` hook that rejects NaN/Infinity instead of accepting them."""
    raise ValueError(f"non-finite JSON token {token!r}")


def loads_strict(text: str) -> Any:
    """Strict parse: rejects NaN/Infinity, the way the host does."""
    return json.loads(text, parse_constant=parse_constant)


def scan(value: Any, path: str = "$") -> list[tuple[str, str, str]]:
    """List every value the host's lossless check would reject.

    Returns ``[(path, kind, shown), ...]``.  Used by the tool-boundary tests and
    by ``scripts/verify_wire.py``; keeping the rule in one place means the tests
    cannot drift from the sanitiser.
    """
    problems: list[tuple[str, str, str]] = []

    def walk(item: Any, where: str) -> None:
        if isinstance(item, bool) or item is None or isinstance(item, str):
            if isinstance(item, str):
                try:
                    item.encode("utf-8")
                except UnicodeEncodeError as exc:  # lone surrogates
                    problems.append((where, "unencodable string", str(exc)))
            return
        if isinstance(item, int):
            if abs(item) > MAX_SAFE_INT:
                problems.append((where, "int outside the safe range", repr(item)[:40]))
            return
        if isinstance(item, float):
            if not math.isfinite(item):
                problems.append((where, "non-finite float", repr(item)))
            elif item == 0.0 and math.copysign(1.0, item) < 0:
                problems.append((where, "negative zero", repr(item)))
            return
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    problems.append((where, "non-string mapping key", repr(key)))
                walk(child, f"{where}.{key}")
            return
        if isinstance(item, (list, tuple)):
            for index, child in enumerate(item):
                walk(child, f"{where}[{index}]")
            return
        problems.append((where, "not JSON at all", type(item).__name__))

    walk(value, path)
    return problems


__all__ = ["sanitize", "dumps", "parse_constant", "loads_strict", "scan", "MAX_SAFE_INT"]