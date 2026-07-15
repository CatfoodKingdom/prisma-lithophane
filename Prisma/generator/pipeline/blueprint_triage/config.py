from __future__ import annotations

from dataclasses import dataclass

from preprocessing.feature_scale import _FEATURE_WIDTHS_PER_NOZZLE


def _first_positive(*values: object) -> float | None:
    for value in values:
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number > 0.0:
            return number
    return None


@dataclass
class TriageConfig:
    """Analyzer policy knobs.

    Physical printer constraints stay on ``PipelineConfig``. This type only holds
    triage-owned thresholds and ranking weights.
    """

    cliff_warn_depth_mm: float = 0.08
    cliff_block_depth_mm: float = 0.16
    hard_min_feature_area_mm2: float = 0.04
    soft_min_feature_area_mm2: float = 0.16
    hard_min_feature_width_mm: float = 0.20
    soft_min_feature_width_mm: float = 0.40
    hard_min_line_width_mm: float = 0.20
    soft_min_line_width_mm: float = 0.40
    layer_height_mm: float = 0.08
    skyscraper_prominence_mm: float = 0.20
    skyscraper_isolation_threshold: float = 0.75
    cap_floor_cluster_tolerance_mm: float = 0.08
    skyscraper_footprint_area_mm2: float = 0.36
    skyscraper_footprint_width_mm: float = 0.60
    smooth_radius_mm: float = 0.60
    hybrid_split_ratio: float = 0.5
    image_cost_weight: float = 1.0
    material_cost_weight: float = 0.25
    printability_gain_weight: float = 1.0
    shape_cost_weight: float = 0.50
    workflow_cost_weight: float = 0.25
    fidelity_prior_gradient_weight: float = 0.5
    counterfactual_top_n_per_hazard: int = 3

    @classmethod
    def from_pipeline_config(cls, config: object) -> "TriageConfig":
        hard_width = _first_positive(
            getattr(config, "printability_minimum_extrusion_width_mm", None),
            getattr(config, "printer_min_line_width_mm", None),
            0.40,
        )
        assert hard_width is not None
        # Soft (warning) thresholds use the reliably-reproduced feature scale:
        # Wing-B's nozzle-derived scale (_FEATURE_WIDTHS_PER_NOZZLE x
        # nozzle_diameter; see preprocessing/feature_scale.py). This replaces the
        # retired cleanup_min_width_mm / cleanup_min_area_mm2 fields, whose
        # defaults WERE that feature scale — so default-config triage behavior
        # (and the soft warning band above the hard extrusion floor) is unchanged.
        nozzle = _first_positive(getattr(config, "nozzle_diameter", None)) or 0.20
        feature_scale = _FEATURE_WIDTHS_PER_NOZZLE * nozzle
        soft_width = max(feature_scale, hard_width)
        soft_area = max(feature_scale * feature_scale, hard_width * hard_width)
        layer_height = float(getattr(config, "layer_height", 0.08))
        return cls(
            cliff_warn_depth_mm=layer_height,
            cliff_block_depth_mm=max(2.0 * layer_height, layer_height),
            hard_min_feature_area_mm2=hard_width * hard_width,
            soft_min_feature_area_mm2=soft_area,
            hard_min_feature_width_mm=hard_width,
            soft_min_feature_width_mm=soft_width,
            hard_min_line_width_mm=hard_width,
            soft_min_line_width_mm=soft_width,
            layer_height_mm=layer_height,
            skyscraper_prominence_mm=max(hard_width, 2.0 * layer_height),
            cap_floor_cluster_tolerance_mm=layer_height,
            skyscraper_footprint_area_mm2=(3.0 * hard_width) ** 2,
            skyscraper_footprint_width_mm=3.0 * hard_width,
            smooth_radius_mm=3.0 * hard_width,
            hybrid_split_ratio=0.5,
            fidelity_prior_gradient_weight=0.5,
            counterfactual_top_n_per_hazard=3,
        )
