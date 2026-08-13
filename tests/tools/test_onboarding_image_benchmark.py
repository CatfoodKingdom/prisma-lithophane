from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


_MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "tools"
    / "generator"
    / "evaluation"
    / "onboarding_image_benchmark.py"
)
_SPEC = importlib.util.spec_from_file_location(
    "onboarding_image_benchmark",
    _MODULE_PATH,
)
assert _SPEC is not None and _SPEC.loader is not None
benchmark = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = benchmark
_SPEC.loader.exec_module(benchmark)


def test_choose_dimensions_preserves_standard_photo_orientation():
    landscape = benchmark.choose_dimensions(4032, 3024)
    portrait = benchmark.choose_dimensions(3024, 4032)

    assert landscape is not None
    assert portrait is not None
    assert landscape["aspect_label"] == "4:3"
    assert (landscape["width_mm"], landscape["height_mm"]) == (120, 90)
    assert (portrait["width_mm"], portrait["height_mm"]) == (90, 120)
    assert landscape["width_solve_px"] == 300
    assert landscape["height_solve_px"] == 225


def test_choose_dimensions_sets_aside_nonstandard_panorama():
    assert benchmark.choose_dimensions(1600, 718) is None


def test_choose_dimensions_accepts_near_standard_crop_with_integer_mm():
    result = benchmark.choose_dimensions(2787, 3678)

    assert result is not None
    assert result["aspect_label"] == "4:3"
    assert 80 <= min(result["width_mm"], result["height_mm"]) <= 100
    assert result["width_solve_px"] * 0.4 == result["width_mm"]
    assert result["height_solve_px"] * 0.4 == result["height_mm"]
    assert result["target_ratio_error_pct"] <= 0.25


def test_summarize_records_rewards_joint_accuracy_and_stability():
    def record(image_id: str, error: float, variation: float) -> dict:
        return {
            "status": "complete",
            "image": {
                "id": image_id,
                "relative_path": f"{image_id}.jpg",
                "width_mm": 120,
                "height_mm": 90,
                "aspect_label": "4:3",
            },
            "source_assets": {
                "thumbnail": f"assets/{image_id}/source-thumb.jpg",
                "large": f"assets/{image_id}/source-large.jpg",
                "original_uri": f"file:///{image_id}.jpg",
            },
            "groups": {
                str(size): {
                    "solves": [
                        {
                            "status": "complete",
                            "source_rms_de": error,
                        }
                        for _ in range(5)
                    ],
                    "metrics": {
                        "mean_source_rms_de": error,
                        "max_source_rms_de": error,
                        "mean_pairwise_output_rms_de": variation,
                        "max_pairwise_output_rms_de": variation,
                    },
                }
                for size in (3, 4, 5)
            },
        }

    summaries = benchmark.summarize_records(
        [
            record("best", 0.02, 0.01),
            record("middle", 0.03, 0.02),
            record("worst", 0.04, 0.03),
        ]
    )

    assert [item["id"] for item in summaries] == ["best", "middle", "worst"]
    assert summaries[0]["foolproof_score"] == 100.0
    assert summaries[-1]["foolproof_score"] == 0.0


def test_per_palette_size_rankings_sorts_each_metric_independently():
    def record(image_id: str, errors: tuple[float, float, float], variations: tuple[float, float, float]) -> dict:
        return {
            "status": "complete",
            "image": {
                "id": image_id,
                "relative_path": f"{image_id}.jpg",
                "width_mm": 120,
                "height_mm": 90,
            },
            "source_assets": {
                "thumbnail": f"assets/{image_id}/source-thumb.jpg",
            },
            "groups": {
                str(size): {
                    "metrics": {
                        "mean_source_rms_de": errors[index],
                        "max_source_rms_de": errors[index] + 0.01,
                        "mean_pairwise_output_rms_de": variations[index],
                        "max_pairwise_output_rms_de": variations[index] + 0.01,
                    }
                }
                for index, size in enumerate((3, 4, 5))
            },
        }

    rankings = benchmark.per_palette_size_rankings(
        [
            record("accurate", (0.01, 0.02, 0.03), (0.30, 0.20, 0.10)),
            record("stable", (0.30, 0.20, 0.10), (0.01, 0.02, 0.03)),
        ]
    )

    assert rankings["3"]["lowest_error"][0]["id"] == "accurate"
    assert rankings["3"]["least_variation"][0]["id"] == "stable"
    assert rankings["5"]["lowest_error"][0]["id"] == "accurate"
    assert rankings["5"]["least_variation"][0]["id"] == "stable"


def test_luminance_profile_uses_nine_layers_and_suggested_shading_balance():
    class Server:
        _DEFAULT_CONFIG = {
            "source_resample_kernel": "lanczos",
            "detail_cap_max_layers": 5,
            "gamut_white_rescale": False,
        }

    config = benchmark._benchmark_config(
        Server(),
        {"width_mm": 120, "height_mm": 90},
        {
            "printer": {"slots_per_ams": 4},
            "nozzle": {},
            "printability": {},
        },
        palette_mode="luminance_detail",
        shading_balance=0.85,
        ams_units=2,
    )

    assert config["luminance_mode"] == "luminance_detail"
    assert config["detail_cap_max_layers"] == 9
    assert config["luminance_base_shading_limit_fraction"] == 0.85
    assert config["luminance_handler_optical_authority_fraction"] == 0.85
    assert config["gamut_white_rescale"] is False
    assert config["n_ams_units"] == 2
    assert config["ams_slots"] == 8


def test_detail_report_has_three_aligned_rows_per_palette_size():
    def solve(rank: int, *, white_point: bool) -> dict:
        return {
            "rank": rank,
            "appearance_asset": (
                f"assets/image/3-color-rank-{rank}"
                f"{'-white-point' if white_point else ''}.png"
            ),
            "source_rms_de": 0.02,
            "suggestion_rank_score": 0.03,
            "palette_fit_rms_de": 0.01,
            "solve_seconds": 1.0,
            "palette": [
                {"id": "filament", "name": "Filament", "hex": "#123456"}
            ],
        }

    metrics = {
        "mean_source_rms_de": 0.02,
        "max_source_rms_de": 0.02,
        "mean_pairwise_output_rms_de": 0.01,
        "max_pairwise_output_rms_de": 0.01,
    }
    record = {
        "image": {
            "relative_path": "image.jpg",
            "width_mm": 120,
            "height_mm": 90,
            "width_px": 1200,
            "height_px": 900,
            "width_solve_px": 300,
            "height_solve_px": 225,
            "aspect_label": "4:3",
        },
        "settings": {
            "palette_mode_key": "luminance_detail",
            "shading_balance": 0.85,
        },
        "source_assets": {
            "large": "assets/image/source-large.jpg",
            "solve": "assets/image/source-solve.png",
            "original_uri": "file:///image.jpg",
        },
        "groups": {
            str(size): {
                "solves": [solve(rank, white_point=False) for rank in range(1, 6)],
                "white_point_solves": [
                    solve(rank, white_point=True) for rank in range(1, 6)
                ],
                "metrics": metrics,
                "white_point_metrics": metrics,
            }
            for size in (3, 4, 5)
        },
    }
    summary = {
        "rank": 1,
        "foolproof_score": 100.0,
        "mean_source_rms_de": 0.02,
        "worst_source_rms_de": 0.02,
        "mean_pairwise_output_rms_de": 0.01,
        "worst_pairwise_output_rms_de": 0.01,
    }

    rendered = benchmark._detail_html(record, summary)

    assert rendered.count('class="comparison-row"') == 9
    assert rendered.count('class="solve-tile"') == 30
    assert rendered.count('class="palette-tile"') == 15
    assert "Luminance profile (9 detail layers, suggested shading balance)" in rendered
