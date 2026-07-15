"""No-reintroduction guards for the thickness_maps reserved-key contract (Task 5.1).

* ``MapKey`` holds exactly the 5 webapp/facade reserved sentinels — never the
  retired ``__translucent_underfill__`` nor the legacy CLI-only ``__filler__``.
* ``__filler__`` is written into a thickness map only by the legacy CLI filler
  path in ``pipeline_cli.py``.
* ``__translucent_underfill__`` (retired by Task 2.3) is never accessed as a
  live thickness_maps key — doc/comment mentions are fine, subscript/.get/in use
  is not.

These are static guards over tracked source; the behavioral guarantee that a
solved result never contains ``__translucent_underfill__`` is pinned separately
by ``test_staged_backend.py``.
"""
from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

from thickness_maps import MapKey


_ROOT = Path(__file__).resolve().parents[2]

_EXPECTED_MAPKEY = {
    "WHITE_CAP": "__white_cap__",
    "WHITE_BOUNDARY_CAP": "__white_boundary_cap__",
    "WHITE_DETAIL_CAP": "__white_detail_cap__",
    "DE": "__de__",
    "GAMUT_MASK": "__gamut_mask__",
}

_SCOPE_PREFIXES = ("Prisma/generator/", "scripts/")


def _source_py_files():
    out = subprocess.check_output(["git", "ls-files"], cwd=_ROOT, text=True).splitlines()
    files = []
    for rel in out:
        if not rel.endswith(".py"):
            continue
        if rel.startswith("Prisma/generator/docs/"):
            continue
        if not any(rel.startswith(p) for p in _SCOPE_PREFIXES):
            continue
        if not (_ROOT / rel).exists():
            continue
        files.append(rel)
    return files


def test_mapkey_has_exactly_the_five_reserved_sentinels():
    assert {m.name: m.value for m in MapKey} == _EXPECTED_MAPKEY


def test_retired_and_cli_keys_are_not_mapkey_members():
    values = {m.value for m in MapKey}
    assert "__translucent_underfill__" not in values  # retired by Task 2.3
    assert "__filler__" not in values


def test_filler_subscript_not_written_by_live_code():
    pattern = re.compile(r"""\[\s*['"]__filler__['"]\s*\]""")
    offenders = sorted(
        rel for rel in _source_py_files()
        if pattern.search((_ROOT / rel).read_text(encoding="utf-8", errors="ignore"))
    )
    assert offenders == []


def test_solveresult_reserved_caps_go_through_accessors():
    # Task 5.2: outside facade.py, the SolveResult white-cap family must be read
    # via the typed accessors (result.cap_map / .boundary_cap / .detail_cap /
    # .de_map / .gamut_mask), never raw ``result.thickness_maps[MapKey...]``.
    # Detail-cap smoothing now runs inside Stage 4 before SolveResult exists, so
    # there is no server-side mutation exception to this rule.
    pattern = re.compile(
        r"""\b(?:result|sr)\.thickness_maps\s*"""
        r"""(?:\[\s*MapKey\.|\.get\(\s*MapKey\.)"""
    )
    offenders = {}
    for rel in _source_py_files():
        if rel == "Prisma/generator/facade.py":
            continue
        hits = pattern.findall((_ROOT / rel).read_text(encoding="utf-8", errors="ignore"))
        if hits:
            offenders[rel] = len(hits)
    assert not offenders, (
        "SolveResult white-cap reserved-key access must use the typed accessors "
        f"(cap_map/boundary_cap/detail_cap/de_map/gamut_mask), not raw "
        f"thickness_maps: {offenders}"
    )


def _is_de_or_gamut_key(node: ast.AST) -> bool:
    """True if an AST subscript-key node names __de__ / __gamut_mask__ — as a
    string literal, ``MapKey.DE`` / ``MapKey.GAMUT_MASK``, or ``....value`` of one."""
    if isinstance(node, ast.Constant) and node.value in {"__de__", "__gamut_mask__"}:
        return True
    if isinstance(node, ast.Attribute):
        if node.attr in {"DE", "GAMUT_MASK"} and isinstance(node.value, ast.Name) and node.value.id == "MapKey":
            return True
        if node.attr == "value":  # MapKey.DE.value
            return _is_de_or_gamut_key(node.value)
    return False


def _thickness_map_alias_names(tree: ast.AST) -> set[str]:
    """Local names bound to ``<x>.thickness_maps`` (direct attr or getattr form)."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        val = node.value
        is_alias = (
            (isinstance(val, ast.Attribute) and val.attr == "thickness_maps")
            or (
                isinstance(val, ast.Call)
                and isinstance(val.func, ast.Name)
                and val.func.id == "getattr"
                and len(val.args) >= 2
                and isinstance(val.args[1], ast.Constant)
                and val.args[1].value == "thickness_maps"
            )
        )
        if is_alias:
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    names.add(tgt.id)
    return names


def test_no_webapp_thickness_maps_diagnostic_writes():
    """Task 5.4: the webapp/staged production files must not WRITE
    ``__de__``/``__gamut_mask__`` into ``state.thickness_maps`` — those rasters
    are diagnostics (``state.diagnostics``) on the webapp path.

    Catches both the direct form (``state.thickness_maps[MapKey.DE] = ...``) and
    local-alias forms (``maps = state.thickness_maps; maps[MapKey.DE] = ...`` and
    the ``getattr`` alias). It only inspects assignment *targets*, so reads — the
    facade fallback, ``_compute_stats`` fallback, and the read-only
    ``application.py`` legacy promotion that copies an old key INTO diagnostics —
    are not flagged. The legacy CLI carriers (solve.py / pipeline_cli.py) are out
    of scope by construction.
    """
    targets = [
        "Prisma/generator/pipeline/runner.py",
        "Prisma/generator/pipeline/blueprint_triage/application.py",
    ]
    offenders: list[str] = []
    for rel in targets:
        tree = ast.parse((_ROOT / rel).read_text(encoding="utf-8"))
        alias_names = _thickness_map_alias_names(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for tgt in node.targets:
                if not isinstance(tgt, ast.Subscript):
                    continue
                base = tgt.value
                base_is_tm = (
                    (isinstance(base, ast.Attribute) and base.attr == "thickness_maps")
                    or (isinstance(base, ast.Name) and base.id in alias_names)
                )
                if base_is_tm and _is_de_or_gamut_key(tgt.slice):
                    offenders.append(f"{rel}:{node.lineno}")
    assert offenders == [], (
        "Task 5.4: webapp/staged production code must not write "
        f"__de__/__gamut_mask__ into thickness_maps; found writes at: {offenders}"
    )


def test_translucent_underfill_is_never_accessed_as_a_live_key():
    access = re.compile(
        r"""\[\s*['"]__translucent_underfill__['"]\s*\]"""        # subscript
        r"""|\.get\(\s*['"]__translucent_underfill__['"]"""        # .get(
        r"""|['"]__translucent_underfill__['"]\s+in\b"""           # membership
    )
    offenders = sorted(
        rel for rel in _source_py_files()
        if access.search((_ROOT / rel).read_text(encoding="utf-8", errors="ignore"))
    )
    assert offenders == [], (
        "__translucent_underfill__ was retired (Task 2.3) and must never be "
        f"accessed as a live thickness_maps key; found in: {offenders}"
    )
