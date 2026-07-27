from __future__ import annotations

import numpy as np

from facade import SolveConfig
from pipeline.staged.stage2.printability import _stage2_printability_ledger_diagnostics_enabled
from pipeline.staged.zone_geometry import _zone_flat_indices


def _reference_zone_flat_indices(labels: np.ndarray) -> tuple[np.ndarray, ...]:
    zone_count = int(np.max(labels)) + 1 if labels.size else 0
    flat = labels.reshape(-1)
    return tuple(
        np.flatnonzero(flat == zone_id).astype(np.int32, copy=False)
        for zone_id in range(zone_count)
    )


def test_grouped_zone_membership_matches_repeated_scan_exactly() -> None:
    labels = np.array(
        [
            [4, 0, 4, 2, 2],
            [0, 3, 2, 4, 0],
            [3, 3, 0, 2, 4],
        ],
        dtype=np.int32,
    )

    expected = _reference_zone_flat_indices(labels)
    actual = _zone_flat_indices(labels)

    assert len(actual) == len(expected) == 5
    for expected_zone, actual_zone in zip(expected, actual):
        assert actual_zone.dtype == np.int32
        np.testing.assert_array_equal(actual_zone, expected_zone)


def test_grouped_zone_membership_preserves_empty_and_negative_label_behavior() -> None:
    labels = np.array([[-1, 2, 0, 2]], dtype=np.int64)

    expected = _reference_zone_flat_indices(labels)
    actual = _zone_flat_indices(labels)

    assert len(actual) == len(expected) == 3
    for expected_zone, actual_zone in zip(expected, actual):
        np.testing.assert_array_equal(actual_zone, expected_zone)


def test_grouped_zone_membership_handles_empty_and_all_negative_inputs() -> None:
    assert _zone_flat_indices(np.zeros((0, 0), dtype=np.int32)) == ()
    assert _zone_flat_indices(np.full((2, 2), -1, dtype=np.int32)) == ()


def test_intermediate_printability_ledger_requires_developer_diagnostics() -> None:
    normal = SolveConfig(
        palette=["red"],
        white_base="white",
        enforce_printability=True,
        emit_blueprint_printability=True,
    )
    assert _stage2_printability_ledger_diagnostics_enabled(normal) is False

    normal.emit_pressure_diagnostics = True
    assert _stage2_printability_ledger_diagnostics_enabled(normal) is True

    normal.emit_pressure_diagnostics = False
    normal.emit_geometry_attribution = True
    assert _stage2_printability_ledger_diagnostics_enabled(normal) is True
