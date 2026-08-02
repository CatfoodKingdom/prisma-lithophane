from __future__ import annotations

import numpy as np

from Prisma.generator.pipeline.staged.stage2.contracts import _ZoneCandidateSet
from Prisma.generator.pipeline.staged.stage2.neutral_field_protection import (
    _apply_neutral_field_protection,
)


def _candidate_set(
    candidate_ids: list[int],
    local_scores: list[float],
) -> _ZoneCandidateSet:
    return _ZoneCandidateSet(
        candidate_ids=np.asarray(candidate_ids, dtype=np.int32),
        local_scores=np.asarray(local_scores, dtype=np.float32),
        total_thickness_mm=np.full(len(candidate_ids), 0.16, dtype=np.float32),
    )


def _two_zone_fixture(*, local_scores: list[float] | None = None) -> dict:
    scores = local_scores or [0.010, 0.012]
    return {
        "selected_stack_ids": np.asarray([0, 0], dtype=np.int32),
        "candidate_sets": (
            _candidate_set([0, 1], scores),
            _candidate_set([2, 3], scores),
        ),
        "zone_flat_indices": (
            np.asarray([0], dtype=np.int64),
            np.asarray([1], dtype=np.int64),
        ),
        "targets": np.asarray(
            [[0.80, 0.001, 0.001], [0.80, 0.001, 0.001]],
            dtype=np.float32,
        ),
        "all_oklabs": np.asarray(
            [
                [[0.80, -0.020, 0.020]],
                [[0.80, 0.000, 0.000]],
                [[0.80, 0.020, -0.020]],
                [[0.80, 0.000, 0.000]],
            ],
            dtype=np.float32,
        ),
        "adjacency_edges": ((0, 1),),
        "adjacency_edge_lengths_px": np.asarray([8], dtype=np.float32),
        "neutral_chroma_cutoff": 0.020,
    }


def test_neutral_field_protection_reduces_hue_seam_with_small_local_tradeoff() -> None:
    result = _apply_neutral_field_protection(**_two_zone_fixture())

    assert result.eligible_edge_count == 1
    assert result.changed_zone_count == 2
    assert result.selected_stack_ids.tolist() == [1, 1]
    assert result.mean_output_edge_de_after < result.mean_output_edge_de_before
    assert result.mean_excess_edge_de_after == 0.0


def test_neutral_field_protection_does_not_gate_saturated_source_edges() -> None:
    case = _two_zone_fixture()
    case["targets"] = np.asarray(
        [[0.70, 0.10, 0.05], [0.70, 0.10, 0.05]],
        dtype=np.float32,
    )

    result = _apply_neutral_field_protection(**case)

    assert result.eligible_edge_count == 0
    assert result.changed_zone_count == 0
    assert result.selected_stack_ids.tolist() == [0, 0]


def test_neutral_field_protection_respects_local_error_budget() -> None:
    result = _apply_neutral_field_protection(
        **_two_zone_fixture(local_scores=[0.010, 0.030])
    )

    assert result.eligible_edge_count == 1
    assert result.changed_zone_count == 0
    assert result.selected_stack_ids.tolist() == [0, 0]


def test_neutral_field_protection_rejects_coherent_chroma_overshoot_basin() -> None:
    candidate_sets = tuple(
        _candidate_set([0, 1], [0.0198, 0.0199]) for _ in range(3)
    )
    result = _apply_neutral_field_protection(
        selected_stack_ids=np.asarray([0, 0, 0], dtype=np.int32),
        candidate_sets=candidate_sets,
        zone_flat_indices=tuple(
            np.asarray([index], dtype=np.int64) for index in range(3)
        ),
        targets=np.asarray(
            [[0.535, 0.010, 0.007]] * 3,
            dtype=np.float32,
        ),
        all_oklabs=np.asarray(
            [
                [[0.535, 0.026, 0.019]],
                [[0.537, -0.009, 0.003]],
            ],
            dtype=np.float32,
        ),
        adjacency_edges=((0, 1), (1, 2)),
        adjacency_edge_lengths_px=np.asarray([8, 8], dtype=np.float32),
        neutral_chroma_cutoff=0.020,
    )

    assert result.changed_zone_count == 3
    assert result.selected_stack_ids.tolist() == [1, 1, 1]
    assert result.mean_chroma_overshoot_after < result.mean_chroma_overshoot_before


def test_neutral_field_cutoff_controls_scope_at_threshold() -> None:
    case = _two_zone_fixture()
    case["targets"] = np.asarray(
        [[0.535, 0.016, 0.012], [0.535, 0.016, 0.012]],
        dtype=np.float32,
    )

    below = _apply_neutral_field_protection(
        **{**case, "neutral_chroma_cutoff": 0.0199}
    )
    at_cutoff = _apply_neutral_field_protection(
        **{**case, "neutral_chroma_cutoff": 0.0200}
    )

    assert below.eligible_edge_count == 0
    assert at_cutoff.eligible_edge_count == 1


def test_neutral_field_protection_preserves_legitimate_warm_skin_gradient() -> None:
    candidate_sets = (
        _candidate_set([0, 1], [0.010, 0.020]),
        _candidate_set([0, 1], [0.012, 0.010]),
        _candidate_set([0, 1], [0.010, 0.020]),
    )
    result = _apply_neutral_field_protection(
        selected_stack_ids=np.asarray([0, 1, 0], dtype=np.int32),
        candidate_sets=candidate_sets,
        zone_flat_indices=tuple(
            np.asarray([index], dtype=np.int64) for index in range(3)
        ),
        targets=np.asarray(
            [
                [0.90, 0.025, 0.005],
                [0.91, 0.012, 0.003],
                [0.90, 0.025, 0.005],
            ],
            dtype=np.float32,
        ),
        all_oklabs=np.asarray(
            [
                [[0.90, 0.025, 0.005]],
                [[0.91, 0.012, 0.003]],
            ],
            dtype=np.float32,
        ),
        adjacency_edges=((0, 1), (1, 2)),
        adjacency_edge_lengths_px=np.asarray([8, 8], dtype=np.float32),
        neutral_chroma_cutoff=0.035,
    )

    assert result.eligible_edge_count == 2
    assert result.changed_zone_count == 0
    assert result.selected_stack_ids.tolist() == [0, 1, 0]


def test_neutral_field_protection_cannot_replace_a_missing_candidate() -> None:
    candidate_sets = tuple(
        _candidate_set([0], [0.020]) for _ in range(2)
    )
    result = _apply_neutral_field_protection(
        selected_stack_ids=np.asarray([0, 0], dtype=np.int32),
        candidate_sets=candidate_sets,
        zone_flat_indices=(
            np.asarray([0], dtype=np.int64),
            np.asarray([1], dtype=np.int64),
        ),
        targets=np.asarray(
            [[0.55, 0.002, 0.001], [0.55, 0.002, 0.001]],
            dtype=np.float32,
        ),
        all_oklabs=np.asarray(
            [[[0.55, 0.030, 0.020]]],
            dtype=np.float32,
        ),
        adjacency_edges=((0, 1),),
        adjacency_edge_lengths_px=np.asarray([8], dtype=np.float32),
        neutral_chroma_cutoff=0.020,
    )

    assert result.eligible_edge_count == 1
    assert result.changed_zone_count == 0
    assert result.selected_stack_ids.tolist() == [0, 0]
    assert result.mean_chroma_overshoot_after > 0.0
