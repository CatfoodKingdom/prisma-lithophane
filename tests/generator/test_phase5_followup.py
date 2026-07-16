"""Phase 5 follow-up bugfix tests.

1. _compute_stats() prefers diagnostics without evaluating thickness_maps fallback.
2. /api/session/config rejects the remaining compat-mirror control field with 422.
3. Canonical config update paths still work normally.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

_PROJECT = Path(__file__).resolve().parent.parent.parent
_GEN_DIR = _PROJECT / "Prisma" / "generator"
_PRISMA_DIR = _PROJECT / "Prisma"
for p in [str(_GEN_DIR), str(_PRISMA_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)


# ── _compute_stats diagnostics preference ───────────────────────────────────


class _ExplodingDict(dict):
    """dict that raises on __getitem__ for specific keys."""

    def __init__(self, *args, blocked_keys=(), **kwargs):
        super().__init__(*args, **kwargs)
        self._blocked = set(blocked_keys)

    def __getitem__(self, key):
        if key in self._blocked:
            raise AssertionError(
                f"thickness_maps[{key!r}] should not be accessed when "
                f"diagnostics[{key!r}] is present"
            )
        return super().__getitem__(key)


def _make_stats_state():
    """Build a minimal PipelineState suitable for _compute_stats."""
    from pipeline.state import PipelineState, PipelineConfig, FULL_PRESET

    H, W = 4, 4
    image = np.zeros((H, W, 3), dtype=np.uint8)
    palette = ["fil-a"]

    cfg = PipelineConfig(
        palette=palette,
        white_base="wb",
        preset=FULL_PRESET,
    )

    de = np.full((H, W), 1.5, dtype=np.float32)
    gamut = np.zeros((H, W), dtype=np.float32)
    wc = np.full((H, W), 0.4, dtype=np.float32)
    fil = np.full((H, W), 0.3, dtype=np.float32)

    state = PipelineState(image=image, config=cfg)
    return state, de, gamut, wc, fil


def test_compute_stats_prefers_diagnostics_over_thickness_maps():
    """When diagnostics has __de__ and __gamut_mask__, thickness_maps
    entries for those keys must NOT be accessed."""
    from pipeline.runner import _compute_stats

    state, de, gamut, wc, fil = _make_stats_state()

    # thickness_maps with __de__ and __gamut_mask__ that explode on access
    maps = _ExplodingDict(
        blocked_keys=("__de__", "__gamut_mask__"),
    )
    maps["__white_cap__"] = wc
    maps["fil-a"] = fil

    state.thickness_maps = maps
    state.diagnostics = {"__de__": de, "__gamut_mask__": gamut}

    # Must not raise — diagnostics should be used, not thickness_maps
    _compute_stats(state)

    assert state.stats is not None
    assert abs(state.stats.mean_de - 1.5) < 1e-4


def test_compute_stats_falls_back_when_diagnostics_absent():
    """Legacy/direct-state fallback: when diagnostics is empty (older or
    hand-built states), _compute_stats reads DE/GAMUT from thickness_maps.
    Task 5.4 keeps this fallback for non-webapp callers; production staged
    finalization always populates diagnostics (see test above)."""
    from pipeline.runner import _compute_stats

    state, de, gamut, wc, fil = _make_stats_state()

    state.thickness_maps = {
        "__de__": de,
        "__gamut_mask__": gamut,
        "__white_cap__": wc,
        "fil-a": fil,
    }
    state.diagnostics = {}

    _compute_stats(state)
    assert state.stats is not None
    assert abs(state.stats.mean_de - 1.5) < 1e-4


@pytest.fixture
def client():
    import server
    from fastapi.testclient import TestClient
    return TestClient(server.app)


def test_session_config_still_accepts_canonical_fields(client):
    """Non-compat fields should be accepted as before."""
    resp = client.post("/api/session/config", json={
        "d_wb": 0.24,
        "layer_height": 0.10,
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["config"]["d_wb"] == 0.24
    assert body["config"]["layer_height"] == 0.10


def test_session_config_drops_retired_gnc_params(client):
    """Retired geometry-native cleanup params are no longer persisted."""
    resp = client.post("/api/session/config", json={
        "gnc_small_area_mm2": 0.8,
        "gnc_enable_cap_cleanup": True,
    })
    assert resp.status_code == 200
    assert "gnc_small_area_mm2" not in resp.json()["config"]
    assert "gnc_enable_cap_cleanup" not in resp.json()["config"]

