from __future__ import annotations

import hashlib

import numpy as np


def _digest_array(arr: np.ndarray) -> str:
    data = np.ascontiguousarray(arr)
    h = hashlib.sha256()
    h.update(str(data.dtype).encode("utf-8"))
    h.update(str(data.shape).encode("utf-8"))
    h.update(data.tobytes())
    return h.hexdigest()


def test_canonical_palette_order_uses_dark_first_luma_tie_break_and_fallback():
    from filament_order import DEFAULT_LUMA, canonical_palette_order, filament_luma

    registry = {
        "light": {"hex": "#FFFFFF"},
        "dark": {"hex": "#000000"},
        "tie-b": {"hex": "#808080"},
        "tie-a": {"hex": "#808080"},
        "bad": {"hex": "not-a-color"},
    }
    palette = ["missing", "tie-b", "light", "dark", "tie-a", "bad"]

    assert filament_luma("missing", registry) == DEFAULT_LUMA
    assert filament_luma("bad", registry) == DEFAULT_LUMA
    assert canonical_palette_order(palette, registry) == [
        "dark",
        "bad",
        "missing",
        "tie-a",
        "tie-b",
        "light",
    ]

    ordered = canonical_palette_order(palette, registry)
    assert canonical_palette_order(ordered, registry) == ordered


def test_solve_config_materialization_canonicalizes_palette():
    from facade import SolveConfig
    from filament_order import canonical_palette_order, load_filament_order_registry

    palette = ["bambu-basic-yellow", "bambu-basic-cyan", "bambu-basic-magenta"]
    cfg = SolveConfig(
        palette=palette,
        white_base="panchroma-matte-cotton-white",
        appearance_model_provider="historical_spline",
    )

    assert cfg.palette == canonical_palette_order(
        palette,
        load_filament_order_registry(),
    )


def test_pipeline_config_materialization_canonicalizes_palette():
    from filament_order import canonical_palette_order, load_filament_order_registry
    from pipeline.state import PipelineConfig

    palette = ["bambu-basic-yellow", "bambu-basic-cyan", "bambu-basic-magenta"]
    cfg = PipelineConfig(
        palette=palette,
        white_base="panchroma-matte-cotton-white",
        appearance_model_provider="historical_spline",
    )

    assert cfg.palette == canonical_palette_order(
        palette,
        load_filament_order_registry(),
    )


def test_server_solve_config_materialization_canonicalizes_palette_override():
    import server
    from filament_order import canonical_palette_order, load_filament_order_registry

    palette = ["bambu-basic-yellow", "bambu-basic-cyan", "bambu-basic-magenta"]
    cfg = {
        **server._DEFAULT_CONFIG,
        "palette": ["bambu-basic-blue"],
        "appearance_model_provider": "historical_spline",
    }

    solve_cfg = server._build_solve_config(cfg, palette_override=palette)

    assert solve_cfg.palette == canonical_palette_order(
        palette,
        load_filament_order_registry(),
    )


def test_spline_lut_cache_key_includes_order_convention():
    from lut import _cache_key

    profile = {
        "knots_mm": [0.0, 0.08],
        "T_r": [1.0, 0.9],
        "T_g": [1.0, 0.9],
        "T_b": [1.0, 0.9],
    }
    profiles = {"a": profile, "b": profile}
    kwargs = dict(
        color_profiles=profiles,
        wb_profile=profile,
        wc_profile=profile,
        layer_height=0.08,
        max_layers=2,
        d_wb=0.2,
        d_wc_min=0.08,
        d_wc_max=0.16,
        k_max=2,
        t_max=0.6,
        corrections=None,
        chroma_weight=1.0,
    )

    assert _cache_key(["a", "b"], **kwargs) != _cache_key(["b", "a"], **kwargs)


def test_solve_preview_artifacts_are_invariant_to_input_palette_order():
    from data_paths import DATA_DIR
    from facade import SolveConfig, solve_preview

    profiles_dir = DATA_DIR / "filaments" / "profiles"
    palette_a = ["bambu-basic-yellow", "bambu-basic-cyan", "bambu-basic-magenta"]
    palette_b = list(reversed(palette_a))
    image = np.zeros((4, 4, 3), dtype=np.uint8)
    image[..., 0] = np.arange(4, dtype=np.uint8)[None, :] * 50
    image[..., 1] = np.arange(4, dtype=np.uint8)[:, None] * 50
    image[..., 2] = 120

    base = dict(
        white_base="panchroma-matte-cotton-white",
        layer_height=0.08,
        d_wb=0.20,
        d_wc_min=0.08,
        t_max=1.0,
        k_max=2,
        de_threshold=0.01,
        smooth_kernel=0,
        ams_slots=4,
        white_slots=1,
        use_corrections=False,
        profiles_dir=profiles_dir,
        appearance_model_provider="historical_spline",
        model_domain_ingress=False,
    )

    result_a = solve_preview(image, SolveConfig(palette=palette_a, **base))
    result_b = solve_preview(image, SolveConfig(palette=palette_b, **base))

    assert result_a.config.palette == result_b.config.palette
    thickness_hashes_a = {
        fid: _digest_array(result_a.thickness_maps[fid])
        for fid in result_a.config.palette
    }
    thickness_hashes_b = {
        fid: _digest_array(result_b.thickness_maps[fid])
        for fid in result_b.config.palette
    }
    assert thickness_hashes_a == thickness_hashes_b

    assert result_a.solved_plan is not None
    assert result_b.solved_plan is not None
    assert _digest_array(result_a.solved_plan.segment_stack_id) == _digest_array(
        result_b.solved_plan.segment_stack_id
    )
    assert result_a.solved_plan.stack_table == result_b.solved_plan.stack_table
