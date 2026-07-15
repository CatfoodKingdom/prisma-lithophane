"""Wing B fingerprint tests (consensus §R6.E, §R6.F).

The session-config dict is the canonical invalidation surface — not
`PipelineConfig`. These tests exercise the real server-side
`_solve_owned_fingerprint(cfg)` digest to verify:

  - Adding `source_resample_kernel` to `_SOLVE_OWNED_KEYS` actually causes a
    kernel-change to invalidate the cache (consensus §R6.C).

(The cleanup_* raster params are retired — 2.2a reassign_mode/search_radius_mm,
2.2b min_width/min_area. Wing-B feature scale is now nozzle-derived; nozzle-
change cache invalidation rides on `__active_nozzle_printability__`, covered in
`test_fingerprint.py`.)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PRISMA = Path(__file__).resolve().parents[2] / "Prisma"
sys.path.insert(0, str(_PRISMA))
sys.path.insert(0, str(_PRISMA / "generator"))


@pytest.fixture()
def _patched_modules(monkeypatch, tmp_path):
    """Isolate module-state file from the live session during fingerprinting."""
    import server
    modules_path = tmp_path / "modules.json"
    modules_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(server, "_MODULES_PATH", modules_path)
    return modules_path


def _base_config() -> dict:
    """Minimal normalized session config for fingerprint tests."""
    return {
        "palette": ["bambu-basic-cyan", "bambu-basic-magenta"],
        "base_filament": "panchroma-matte-cotton-white",
        "cap_filament": "__same__",
        "d_wb": 0.20,
        "d_wc_min": 0.08,
        "t_max": 2.5,
        "k_max": 3,
        "de_threshold": 0.05,
        "gamut_mode": "hull",
        "chroma_weight": 1.0,
        "use_corrections": True,
        "layer_height": 0.08,
        "image_sample_pitch_mm": 0.20,
        "solver_fine_pitch_mm": 0.20,
        "color_region_target_mm": 0.60,
        "image_path": "test.jpg",
        "image_adjust": None,
        "max_dim_mm": 130.0,
        "frame": None,
        "smooth_kernel": 15.0,
        "smooth_iters": 3,
        "source_resample_kernel": "lanczos",
        "border": False,
        "border_width_mm": 3.0,
    }


# ── source_resample_kernel (Wing B / B7) ─────────────────────────────────────


def test_solve_owned_fingerprint_includes_kernel(_patched_modules):
    """Two session dicts identical except for source_resample_kernel produce
    distinct digests. Exercises the real server-side invalidation surface."""
    from server import _solve_owned_fingerprint

    cfg1 = _base_config()
    cfg2 = {**cfg1, "source_resample_kernel": "area"}

    assert _solve_owned_fingerprint(cfg1) != _solve_owned_fingerprint(cfg2)


def test_solve_owned_fingerprint_unchanged_when_kernel_unchanged(_patched_modules):
    """Determinism across repeated calls — same config, same digest."""
    from server import _solve_owned_fingerprint

    cfg = _base_config()
    digest_1 = _solve_owned_fingerprint(cfg)
    digest_2 = _solve_owned_fingerprint(cfg)
    assert digest_1 == digest_2


# ── _SOLVE_OWNED_KEYS surface sanity ─────────────────────────────────────────


def test_solve_owned_keys_contains_wing_b_fields():
    """Guard: the Wing-B fields are actually registered as solve-owned."""
    from server import _SOLVE_OWNED_KEYS

    required = {
        "source_resample_kernel",
    }
    missing = required - set(_SOLVE_OWNED_KEYS)
    assert not missing, f"Wing B keys missing from _SOLVE_OWNED_KEYS: {missing}"
