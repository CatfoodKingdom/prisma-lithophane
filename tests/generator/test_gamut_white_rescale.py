"""White-point rescale: pure-helper and runner-stage tests.

Tests for the current whitepoint-rescale integration contract.
"""
import numpy as np
import pytest
from types import SimpleNamespace

from model import to_oklab
from solve import compute_paper_white_rgb, rescale_oklab_targets

_PAPER_WHITE = np.asarray([0.82, 0.83, 0.73])


class _PhotoStyleProvider:
    """Mimics PhotoStackBundleAppearanceProvider's appearance-batch API."""

    def predict_stack_appearance_linear_rgb_batch(self, requests):
        assert len(requests) == 1
        return np.asarray([_PAPER_WHITE], dtype=np.float32)


class _HistoricStyleProvider:
    """Mimics HistoricalSplineAppearanceProvider (no appearance-batch method)."""

    def predict_stack_linear_rgb_batch(self, requests):
        return np.asarray([[0.90, 0.91, 0.84]], dtype=np.float32)


class _Cfg:
    white_base = "bambu-tough-white"
    white_cap = "bambu-tough-white"
    d_wb = 0.20
    d_wc_min = 0.08


def test_paper_white_uses_photo_appearance_batch():
    white = compute_paper_white_rgb(_PhotoStyleProvider(), _Cfg())
    assert np.allclose(white, _PAPER_WHITE, atol=1e-6)


def test_paper_white_falls_back_to_transmission_batch():
    white = compute_paper_white_rgb(_HistoricStyleProvider(), _Cfg())
    assert np.allclose(white, [0.90, 0.91, 0.84], atol=1e-6)


def test_paper_white_degenerate_unit_white_returns_none():
    class _Unit:
        def predict_stack_appearance_linear_rgb_batch(self, requests):
            return np.ones((1, 3), dtype=np.float32)

    assert compute_paper_white_rgb(_Unit(), _Cfg()) is None


def test_paper_white_missing_white_base_returns_none():
    class _NoBase(_Cfg):
        white_base = ""

    assert compute_paper_white_rgb(_PhotoStyleProvider(), _NoBase()) is None


def test_rescale_maps_source_white_to_paper_white():
    white = _PAPER_WHITE
    targets = to_oklab(np.asarray([[1.0, 1.0, 1.0]], dtype=np.float32))
    out = rescale_oklab_targets(targets, white)
    expected = to_oklab(white.reshape(1, 3).astype(np.float32))
    assert np.allclose(out, expected, atol=1e-4)


def test_rescale_identity_for_unit_white():
    rng = np.random.default_rng(7)
    lin = rng.uniform(0.02, 1.0, size=(64, 3)).astype(np.float32)
    targets = to_oklab(lin)
    out = rescale_oklab_targets(targets, np.ones(3))
    assert np.allclose(out, targets, atol=1e-5)


def test_rescale_brings_pale_tints_within_paper_white_range():
    # The whiteout mechanism: pale tints above paper white are unreachable and
    # get hull-clipped to white. After rescale every channel is <= paper white,
    # so the bright corner is reachable and tint relationships survive.
    from lib.photo_stack_model.predictor import oklab_to_linear_rgb

    white = _PAPER_WHITE
    pale = np.asarray([[0.95, 0.97, 1.0], [0.88, 0.99, 0.91]], dtype=np.float32)
    out = rescale_oklab_targets(to_oklab(pale), white)
    lin_out = oklab_to_linear_rgb(np.asarray(out, dtype=np.float64))
    assert np.all(lin_out <= white.reshape(1, 3) + 1e-4)
    # tint ordering preserved per channel (monotone scale): pixel 0 has higher
    # blue than pixel 1 in source (1.0 > 0.91); the same must hold after rescale.
    assert lin_out[0, 2] > lin_out[1, 2]


def test_oklab_inverse_consistent_with_model_to_oklab():
    # rescale_oklab_targets uses predictor.oklab_to_linear_rgb as the
    # inverse of model.to_oklab — this pins that they really are inverses.
    rng = np.random.default_rng(11)
    lin = rng.uniform(0.02, 1.0, size=(256, 3))
    from lib.photo_stack_model.predictor import oklab_to_linear_rgb

    back = oklab_to_linear_rgb(np.asarray(to_oklab(lin.astype(np.float64)), dtype=np.float64))
    assert np.allclose(back, lin, atol=1e-4)


# ---------------------------------------------------------------------------
# Runner-stage tests (Task 2)
# ---------------------------------------------------------------------------


def _runner_state(*, toggle, gamut_mode="none", provider=None):
    targets = to_oklab(
        np.asarray([[0.95, 0.95, 0.90], [0.20, 0.30, 0.40]], dtype=np.float32)
    )
    cfg = SimpleNamespace(
        gamut_mode=gamut_mode,
        gamut_white_rescale=toggle,
        de_threshold=0.05,
        white_base="w",
        white_cap="w",
        d_wb=0.20,
        d_wc_min=0.08,
    )
    return SimpleNamespace(
        config=cfg,
        diagnostics={},
        solve_target_oklab=targets.copy(),
        luts=[],
        appearance_provider=provider or _PhotoStyleProvider(),
    )


def test_runner_toggle_off_never_computes_paper_white(monkeypatch):
    from pipeline.runner import _apply_target_gamut_mapping
    import solve

    def _boom(*args, **kwargs):
        raise AssertionError("compute_paper_white_rgb must not be called when toggle is off")

    monkeypatch.setattr(solve, "compute_paper_white_rgb", _boom)
    state = _runner_state(toggle=False)
    before = state.solve_target_oklab.copy()
    _apply_target_gamut_mapping(state, shape=(1, 2))
    assert np.array_equal(state.solve_target_oklab, before)
    diag = state.diagnostics["__target_gamut_mapping__"]
    assert diag["white_rescale_enabled"] is False
    assert diag["white_rgb"] is None


def test_runner_gamut_mapping_requires_luts_for_enabled_mode():
    from pipeline.runner import _apply_target_gamut_mapping

    state = _runner_state(toggle=False, gamut_mode="hull")
    state.luts = []

    with pytest.raises(RuntimeError, match="requires LUTs"):
        _apply_target_gamut_mapping(state, shape=(1, 2))


def test_runner_toggle_on_rescales_even_with_gamut_mode_none():
    from pipeline.runner import _apply_target_gamut_mapping

    state = _runner_state(toggle=True, gamut_mode="none")
    before = state.solve_target_oklab.copy()
    _apply_target_gamut_mapping(state, shape=(1, 2))
    assert not np.array_equal(state.solve_target_oklab, before)
    expected = rescale_oklab_targets(before, _PAPER_WHITE)
    assert np.allclose(state.solve_target_oklab, expected, atol=1e-5)
    diag = state.diagnostics["__target_gamut_mapping__"]
    assert diag["white_rescale_enabled"] is True
    assert np.allclose(diag["white_rgb"], _PAPER_WHITE, atol=1e-6)


def test_runner_hull_mode_maps_rescaled_targets_and_counts_pre(monkeypatch):
    from pipeline import runner as runner_mod

    def _fake_build_hull(luts):
        return "HULL"

    calls = []

    def _fake_map(targets, luts, hull, de_threshold):
        calls.append(np.asarray(targets).copy())
        mask = np.zeros(len(targets), dtype=bool)
        mask[0] = True
        return np.asarray(targets, dtype=np.float32), mask

    import lut as lut_mod
    import solve as solve_mod

    monkeypatch.setattr(lut_mod, "build_hull_from_luts", _fake_build_hull)
    monkeypatch.setattr(solve_mod, "gamut_map_hull_batch", _fake_map)

    state = _runner_state(toggle=True, gamut_mode="hull")
    state.luts = [object()]
    before = state.solve_target_oklab.copy()
    runner_mod._apply_target_gamut_mapping(state, shape=(1, 2))

    # two map calls: one pre-rescale count pass on RAW targets, one real pass on RESCALED
    assert len(calls) == 2
    expected = rescale_oklab_targets(before, _PAPER_WHITE)
    assert np.allclose(calls[0], before, atol=1e-5)        # raw counted first
    assert np.allclose(calls[1], expected, atol=1e-5)      # rescaled actually mapped
    diag = state.diagnostics["__target_gamut_mapping__"]
    assert diag["pre_rescale_out_of_gamut"] == 1


# ---------------------------------------------------------------------------
# Server settings surface tests (Task 3)
# ---------------------------------------------------------------------------

def test_gamut_white_rescale_is_solve_owned_and_fingerprinted():
    import server

    assert "gamut_white_rescale" in server._SOLVE_OWNED_KEYS
    base = dict(server._DEFAULT_CONFIG)
    assert base.get("gamut_white_rescale") is False
    on = dict(base, gamut_white_rescale=True)
    assert server._solve_owned_fingerprint(base) != server._solve_owned_fingerprint(on)


def test_config_payload_accepts_toggle():
    import server

    payload = server.ConfigPayload(gamut_white_rescale=True)
    assert payload.gamut_white_rescale is True
    assert server.ConfigPayload().gamut_white_rescale is False


# ---------------------------------------------------------------------------
# Rider A: Real PipelineState variant (Task 3 rider)
# ---------------------------------------------------------------------------

def test_runner_rescale_works_with_real_pipeline_state():
    from pipeline.runner import _apply_target_gamut_mapping
    from pipeline.state import PipelineConfig, PipelineState

    config = PipelineConfig(
        palette=["w"],
        white_base="w",
        white_cap="w",
        gamut_mode="none",
        gamut_white_rescale=True,
    )
    targets = to_oklab(np.asarray([[0.95, 0.95, 0.90]], dtype=np.float32))
    image = np.zeros((1, 1, 3), dtype=np.float32)
    state = PipelineState(image=image, config=config)
    state.appearance_provider = _PhotoStyleProvider()
    state.solve_target_oklab = targets.copy()
    state.luts = []
    _apply_target_gamut_mapping(state, shape=(1, 1))
    expected = rescale_oklab_targets(targets, _PAPER_WHITE)
    assert np.allclose(state.solve_target_oklab, expected, atol=1e-5)


def test_paper_white_none_cap_uses_base_filament_at_d_wc_min():
    # white_cap=None means "cap uses the base filament", NOT "no cap":
    # the thinnest printable stack always carries >= d_wc_min of cap.
    captured = {}

    class _Recorder:
        def predict_stack_appearance_linear_rgb_batch(self, requests):
            captured["request"] = requests[0]
            return np.asarray([_PAPER_WHITE], dtype=np.float32)

    class _NoneCap(_Cfg):
        white_cap = None

    white = compute_paper_white_rgb(_Recorder(), _NoneCap())
    assert np.allclose(white, _PAPER_WHITE, atol=1e-6)
    req = captured["request"]
    assert req.white_cap == ("bambu-tough-white", 0.08)
