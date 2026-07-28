from __future__ import annotations

import inspect

import pytest
from fastapi import HTTPException

from Prisma.generator import server


def _active_nozzle(size: float) -> dict:
    return {"printer": {"id": "test"}, "nozzle": {"size": size}}


def _config(pitch: float) -> dict:
    return {
        "image_sample_pitch_mm": pitch,
        "solver_fine_pitch_mm": pitch,
    }


@pytest.mark.parametrize("pitch", [0.4, 0.400001, 0.4 - 0.5e-6])
def test_solve_pitch_guard_allows_equal_coarser_and_within_tolerance(pitch: float) -> None:
    server._validate_solve_pitch_for_nozzle(_config(pitch), _active_nozzle(0.4))


def test_solve_pitch_guard_rejects_pitch_below_nozzle() -> None:
    with pytest.raises(HTTPException) as exc_info:
        server._validate_solve_pitch_for_nozzle(_config(0.2), _active_nozzle(0.4))

    assert exc_info.value.status_code == 400
    assert "Solve Pitch (0.2 mm)" in str(exc_info.value.detail)
    assert "nozzle diameter (0.4 mm)" in str(exc_info.value.detail)
    assert "Increase Solve Pitch or choose a smaller nozzle" in str(exc_info.value.detail)


def test_solve_start_validates_pitch_before_reserving_a_job() -> None:
    source = inspect.getsource(server._start_full_solve_job)
    assert source.index("_validate_solve_pitch_for_nozzle(") < source.index("_reserve_model_job(")
