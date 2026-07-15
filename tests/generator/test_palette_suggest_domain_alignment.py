"""Domain-alignment tests for palette suggestion target extraction."""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import numpy as np
from PIL import Image

from model import image_to_target, load_profile, to_oklab
from palette.suggest import (
    MODEL_OKLAB_DOMAIN,
    PaletteCandidate,
    solve_target_oklab_for_signature,
)
from pipeline.derived_views import project_observation_to_solve_grid
from pipeline.runner import _apply_target_gamut_mapping
from pipeline.target_cloud import compute_solve_target_cloud
from Prisma.generator import server


def _identity_ingress_lut(path):
    axis = np.linspace(0.0, 1.0, 33, dtype=np.float32)
    rr, gg, bb = np.meshgrid(axis, axis, axis, indexing="ij")
    np.savez(path, lut=np.stack([rr, gg, bb], axis=-1).astype(np.float32))
    return path


def _cfg(*, ingress, lut_path, white_rescale=False):
    return SimpleNamespace(
        d_wb=0.20,
        image_sample_pitch_mm=0.20,
        solver_fine_pitch_mm=0.20,
        model_domain_ingress=bool(ingress),
        model_domain_ingress_lut_path=lut_path,
        gamut_white_rescale=bool(white_rescale),
        gamut_mode="none",
        de_threshold=0.02,
        white_base="w",
        white_cap="w",
        d_wc_min=0.08,
    )


class _PaperWhiteProvider:
    def predict_stack_appearance_linear_rgb_batch(self, requests):
        return np.asarray([[0.82, 0.83, 0.73]], dtype=np.float32)


def test_target_cloud_helper_matches_runner_pre_refactor_formula(tmp_path):
    image = np.arange(5 * 7 * 3, dtype=np.uint8).reshape(5, 7, 3)
    wb = load_profile("panchroma-matte-cotton-white")
    lut_path = _identity_ingress_lut(tmp_path / "identity_lut.npz")

    for ingress in (False, True):
        cfg = _cfg(ingress=ingress, lut_path=lut_path)
        cloud = compute_solve_target_cloud(image, wb, cfg)
        t_target = image_to_target(
            image,
            wb,
            cfg.d_wb,
            model_domain_ingress=cfg.model_domain_ingress,
            model_domain_ingress_lut_path=cfg.model_domain_ingress_lut_path,
        )
        observed = to_oklab(t_target.reshape(image.shape[0] * image.shape[1], 3))
        expected = project_observation_to_solve_grid(
            observed,
            obs_h=image.shape[0],
            obs_w=image.shape[1],
            image_sample_pitch_mm=cfg.image_sample_pitch_mm,
            solver_fine_pitch_mm=cfg.solver_fine_pitch_mm,
        )

        np.testing.assert_array_equal(cloud.observed_oklab, observed)
        np.testing.assert_array_equal(cloud.solve_oklab, expected)
        assert cloud.domain == MODEL_OKLAB_DOMAIN


def test_suggestion_target_cloud_equals_solve_target_cloud_with_frame_crop(tmp_path):
    source = np.zeros((8, 10, 3), dtype=np.uint8)
    source[:, :5] = [255, 40, 20]
    source[:, 5:] = [20, 180, 240]
    image_path = tmp_path / "framed.png"
    Image.fromarray(source).save(image_path)
    lut_path = _identity_ingress_lut(tmp_path / "identity_lut.npz")
    cfg = {
        **server._DEFAULT_CONFIG,
        "image_sample_pitch_mm": 0.20,
        "max_dim_mm": 100.0,
        "frame": {
            "width_mm": 4.0,
            "height_mm": 4.0,
            "scale": 120.0,
            "pan_x": 0.25,
            "pan_y": -0.25,
        },
        "model_domain_ingress": True,
        "model_domain_ingress_lut_path": str(lut_path),
        "gamut_white_rescale": False,
    }
    wb = load_profile("panchroma-matte-cotton-white")
    loaded = server._load_run_source_image(image_path, cfg, max_dim_mm=100.0)
    solve_cfg = server._build_solve_config(cfg)

    cloud = compute_solve_target_cloud(loaded, wb, solve_cfg)
    suggest_target, stats = solve_target_oklab_for_signature(
        loaded,
        wb_profile=wb,
        config=solve_cfg,
    )

    np.testing.assert_array_equal(suggest_target, cloud.solve_oklab)
    assert stats["signature_domain"] == cloud.domain
    assert stats["model_domain_ingress"] is True


def test_suggestion_white_rescale_matches_runner_stage(tmp_path):
    image = np.array([[[245, 245, 240], [40, 70, 90]]], dtype=np.uint8)
    wb = load_profile("panchroma-matte-cotton-white")
    cfg = _cfg(
        ingress=False,
        lut_path=_identity_ingress_lut(tmp_path / "identity_lut.npz"),
        white_rescale=True,
    )
    cloud = compute_solve_target_cloud(image, wb, cfg)
    state = SimpleNamespace(
        config=cfg,
        solve_target_oklab=cloud.solve_oklab.copy(),
        luts=[],
        appearance_provider=_PaperWhiteProvider(),
        diagnostics={},
    )

    _apply_target_gamut_mapping(state, shape=cloud.solve_shape)
    suggest_target, stats = solve_target_oklab_for_signature(
        image,
        wb_profile=wb,
        config=cfg,
        white_rescale_provider=_PaperWhiteProvider(),
    )

    np.testing.assert_array_equal(suggest_target, state.solve_target_oklab)
    assert stats["gamut_white_rescale_applied"] is True


def test_suggestion_response_schema_is_honest_about_metrics():
    response = server._format_candidate_response(
        [
            PaletteCandidate(
                filament_ids=["cyan"],
                mean_de=0.01234,
                max_de=0.045,
                pct_above_threshold=25.0,
                gamut_points=10,
            )
        ],
        signature_stats={"signature_domain": MODEL_OKLAB_DOMAIN},
        model_metadata={
            "provider_fingerprint": "unit-provider",
            "signature_domain": MODEL_OKLAB_DOMAIN,
            "model_domain_ingress": True,
        },
    )

    candidate = response["candidates"][0]
    assert candidate["suggestion_mean_de"] == 0.0123
    assert "source_rms_de" not in candidate
    assert response["model_metadata"]["provider_fingerprint"] == "unit-provider"
    assert response["model_metadata"]["signature_domain"] == MODEL_OKLAB_DOMAIN
    assert response["model_metadata"]["model_domain_ingress"] is True


def test_historical_suggestion_model_threads_run_geometry():
    cfg = deepcopy(server._DEFAULT_CONFIG)
    cfg.update({
        "appearance_model_provider": "historical_spline",
        "layer_height": 0.04,
        "max_layers": 12,
        "d_wb": 0.24,
        "d_wc_min": 0.12,
        "t_max": 1.40,
    })

    backend, metadata, kwargs = server._build_palette_suggestion_model(cfg)

    assert backend is None
    assert metadata["gamut_domain"] == MODEL_OKLAB_DOMAIN
    assert kwargs["layer_height"] == 0.04
    assert kwargs["max_layers"] == server._build_solve_config(cfg).effective_max_layers()
    assert kwargs["d_wc_min"] == 0.12
    assert kwargs["t_max"] == 1.16
