"""Task 4B zero-residual guard.

Fails if any high-signal old photo-stack *contract* identity token survives in the
renamed surface (generator / calibration / lib / tests / scripts).

Scope notes:
- The vendored research engine ``v63_fit_engine/**`` and the historical planning
  docs are intentionally preserved and skipped.
- Research-engine import-path lines (the only place the old research arc name may
  appear) are allowlisted by marker substring.
- Lines that intentionally name a legacy token to prove it is rejected carry the
  ``photo-stack-v2-allow`` marker and are skipped.
- Bare ``v63`` / ``V63`` is deliberately NOT policed here: it appears legitimately
  in research provenance. The high-signal contract tokens below are the real gate.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]

_SCAN_DIRS = ("Prisma/generator", "Prisma/calibration", "Prisma/lib", "tests", "scripts")
_SCAN_SUFFIXES = (".py", ".js", ".json", ".html", ".css", ".md")
_SKIP_DIR_PARTS = {"v63_fit_engine", "docs", "__pycache__", ".tmp", "node_modules"}

# Allowlisted line markers: research-engine import paths + intentional legacy mentions.
_ALLOW_MARKERS = (
    "v63_fit_engine",
    "research_arc_v63",
    "run_td_full_license_probe_v63",
    "photo-stack-v2-allow",
)

_FORBIDDEN = (
    "non_ml_photo_stack",
    "prisma_non_ml_photo",
    "prisma_v63_color_pair_corrections_v1",
    "td_full_license_probe_v63",
    "v63_direct_context",
    "runtime_bundle_v63",
    "correction_layer_direct_context",
    "photo_stack_v63_vectorized",
    "/api/photo-stack/non-ml",
)

# Case-insensitive family tokens (catch non_ml / non-ml / Non-ML / non-ML anywhere).
_FORBIDDEN_CI = (
    "non_ml",
    "non-ml",
)

_THIS_FILE = Path(__file__).name


def _iter_scan_files():
    for rel in _SCAN_DIRS:
        base = _ROOT / rel
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if path.suffix not in _SCAN_SUFFIXES:
                continue
            if path.name == _THIS_FILE:
                continue
            if any(part in _SKIP_DIR_PARTS for part in path.parts):
                continue
            yield path


def test_no_old_photo_stack_contract_tokens_remain() -> None:
    leaks: list[str] = []
    for path in _iter_scan_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if any(marker in line for marker in _ALLOW_MARKERS):
                continue
            for token in _FORBIDDEN:
                if token in line:
                    rel = path.relative_to(_ROOT).as_posix()
                    leaks.append(f"{rel}:{lineno}: {token!r} -> {line.strip()[:100]}")
            low = line.lower()
            for token in _FORBIDDEN_CI:
                if token in low:
                    rel = path.relative_to(_ROOT).as_posix()
                    leaks.append(f"{rel}:{lineno}: {token!r} (ci) -> {line.strip()[:100]}")

    assert not leaks, "old photo-stack contract tokens survive the rename:\n" + "\n".join(leaks)
