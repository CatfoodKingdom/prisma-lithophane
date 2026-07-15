from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np
import pytest

_GEN_DIR = Path(__file__).resolve().parent.parent.parent / "Prisma" / "generator"
if str(_GEN_DIR) not in sys.path:
    sys.path.insert(0, str(_GEN_DIR))

from pipeline.material_exposure import (  # noqa: E402
    audit_colored_filament_exposure_from_layer_counts,
    audit_colored_filament_exposure_from_thickness_maps,
    exposure_relevant_material_keys,
    lateral_boundary_shield_floor_layers,
    positive_layer_counts,
)

from pipeline.staged_runner import (  # noqa: E402
    _apply_stage2_exterior_white_guard,
    _infer_implied_cap_heights,
)


def test_two_column_color_cliff_requires_shield_only_on_lower_side() -> None:
    color = np.asarray([[1, 3]], dtype=np.int32)

    floor = lateral_boundary_shield_floor_layers(color)

    assert floor.tolist() == [[2, 0]]
    before = audit_colored_filament_exposure_from_layer_counts(
        color,
        np.zeros_like(color),
    )
    after = audit_colored_filament_exposure_from_layer_counts(color, floor)
    assert before.lateral_internal_face_count == 2
    assert after.lateral_internal_face_count == 0


def test_equal_height_neighbors_require_no_lateral_shield() -> None:
    color = np.asarray([[2, 2], [2, 2]], dtype=np.int32)

    floor = lateral_boundary_shield_floor_layers(color)
    audit = audit_colored_filament_exposure_from_layer_counts(
        color,
        np.full_like(color, 1),
    )

    assert np.count_nonzero(floor) == 0
    assert audit.lateral_internal_face_count == 0


def test_exterior_color_boundary_is_counted_even_when_topped_by_white() -> None:
    color = np.asarray([[2]], dtype=np.int32)
    white = np.asarray([[1]], dtype=np.int32)

    audit = audit_colored_filament_exposure_from_layer_counts(color, white)

    assert audit.top_face_count == 0
    assert audit.exterior_face_count == 8
    assert audit.passes is False


def test_same_column_cap_does_not_shield_exterior_side_wall() -> None:
    color = np.asarray([[3, 0], [0, 0]], dtype=np.int32)
    white = np.asarray([[3, 0], [0, 0]], dtype=np.int32)

    audit = audit_colored_filament_exposure_from_layer_counts(color, white)

    assert audit.top_face_count == 0
    assert audit.exterior_face_count == 6
    assert audit.passes is False


def test_real_white_guard_neighbor_can_shield_color_from_exterior() -> None:
    color = np.zeros((3, 3), dtype=np.int32)
    color[1, 1] = 3
    white = np.full((3, 3), 3, dtype=np.int32)
    white[1, 1] = 1

    audit = audit_colored_filament_exposure_from_layer_counts(color, white)

    assert audit.total_exposed_face_count == 0
    assert audit.passes is True


def test_top_color_exposure_is_counted_independently() -> None:
    color = np.asarray([[1]], dtype=np.int32)

    exposed = audit_colored_filament_exposure_from_layer_counts(
        color,
        np.asarray([[0]], dtype=np.int32),
    )
    shielded = audit_colored_filament_exposure_from_layer_counts(
        color,
        np.asarray([[1]], dtype=np.int32),
    )

    assert exposed.top_face_count == 1
    assert shielded.top_face_count == 0


def test_thickness_map_audit_uses_palette_and_white_cap_maps() -> None:
    thickness_maps = {
        "red": np.asarray([[0.08, 0.24]], dtype=np.float32),
        "blue": np.zeros((1, 2), dtype=np.float32),
        "__white_cap__": np.asarray([[0.16, 0.0]], dtype=np.float32),
        "__white_base__": np.full((1, 2), 0.2, dtype=np.float32),
    }

    audit = audit_colored_filament_exposure_from_thickness_maps(
        thickness_maps,
        layer_height_mm=0.08,
        color_filament_ids=["red", "blue"],
    )

    assert audit.lateral_internal_face_count == 0
    assert audit.top_face_count == 1
    assert audit.max_color_layers == 3
    assert audit.max_white_cap_layers == 2


def test_thickness_map_audit_infers_real_non_white_materials() -> None:
    thickness_maps = {
        "red": np.asarray([[0.0, 0.08]], dtype=np.float32),
        "panchroma-translucent-natural": np.asarray([[0.08, 0.0]], dtype=np.float32),
        "bambu-tough-white": np.asarray([[9.0, 9.0]], dtype=np.float32),
        "__white_cap__": np.asarray([[0.0, 0.0]], dtype=np.float32),
        "__de__": np.asarray([[9.0, 9.0]], dtype=np.float32),
        "__gamut_mask__": np.asarray([[1.0, 1.0]], dtype=np.float32),
    }

    keys = exposure_relevant_material_keys(
        thickness_maps,
        excluded_material_ids=["bambu-tough-white"],
    )
    audit = audit_colored_filament_exposure_from_thickness_maps(
        thickness_maps,
        layer_height_mm=0.08,
        excluded_material_ids=["bambu-tough-white"],
    )

    # Real non-white materials (including a real translucent filament used as a
    # normal palette material) are audited; the removed solve-only
    # __translucent_underfill__ placeholder is not.
    assert keys == ["red", "panchroma-translucent-natural"]
    assert audit.max_color_layers == 1
    assert audit.top_face_count == 2


def test_positive_layer_counts_matches_half_open_minimum_positive_semantics() -> None:
    values = np.asarray([0.0, 0.001, 0.039, 0.041, 0.08, 0.12], dtype=np.float32)

    counts = positive_layer_counts(values, 0.08)

    assert counts.tolist() == [0, 1, 1, 1, 1, 2]


def test_exposed_colored_faces_are_not_exempted() -> None:
    audit = audit_colored_filament_exposure_from_layer_counts(
        np.asarray([[1]], dtype=np.int32),
        np.asarray([[0]], dtype=np.int32),
    )

    assert audit.total_exposed_face_count > 0
    assert audit.passes is False
    summary = audit.to_summary()
    assert "fixed_thickness_exempt" not in summary
    assert "skipped_reason" not in summary


def test_stage2_implied_cap_inference_obeys_mandatory_floor() -> None:
    targets = np.asarray([[0.0, 0.0, 0.0]], dtype=np.float32)
    # Cap step 0 is optically perfect, but the mandatory floor requires step 2.
    all_oklabs = np.asarray(
        [
            [
                [0.0, 0.0, 0.0],
                [10.0, 0.0, 0.0],
                [20.0, 0.0, 0.0],
            ]
        ],
        dtype=np.float32,
    )

    cap = _infer_implied_cap_heights(
        fine_shape=(1, 1),
        targets=targets,
        fine_stack_id_map=np.asarray([[0]], dtype=np.int32),
        all_oklabs=all_oklabs,
        cap_values=np.asarray([0.0, 0.08, 0.16], dtype=np.float32),
        minimum_cap_height_mm=np.asarray([[0.16]], dtype=np.float32),
    )

    assert cap.tolist() == [[pytest.approx(0.16)]]


def test_stage2_exterior_guard_marks_perimeter_without_deleting_color_stack() -> None:
    white_stack_id = 1
    stack_map = np.asarray(
        [
            [0, 0, 0],
            [0, 0, 0],
            [0, 0, 0],
        ],
        dtype=np.int32,
    )

    guarded, guard_map, guard_pixels, changed_pixels = _apply_stage2_exterior_white_guard(
        fine_stack_id_map=stack_map,
        white_guard_stack_id=white_stack_id,
        config=SimpleNamespace(cap_mode="smooth_variable"),
    )

    assert guard_pixels == 8
    assert changed_pixels == 0
    assert np.array_equal(guarded, stack_map)
    assert guarded[1, 1] == 0
    assert guard_map is not None
    assert np.count_nonzero(guard_map) == 8

    audit = audit_colored_filament_exposure_from_layer_counts(
        np.full_like(guarded, 3),
        np.zeros_like(guarded),
    )
    assert audit.exterior_face_count > 0
    assert audit.passes is False


@pytest.mark.parametrize("cap_mode", ["smooth_variable", "appearance_bounded_smooth"])
def test_stage2_exterior_guard_applies_for_supported_cap_modes(cap_mode: str) -> None:
    stack_map = np.zeros((3, 3), dtype=np.int32)

    guarded, guard_map, guard_pixels, changed_pixels = _apply_stage2_exterior_white_guard(
        fine_stack_id_map=stack_map,
        white_guard_stack_id=1,
        config=SimpleNamespace(cap_mode=cap_mode),
    )

    assert np.array_equal(guarded, stack_map)
    assert guard_map is not None
    assert np.count_nonzero(guard_map) == 8
    assert guard_pixels == 8
    assert changed_pixels == 0
