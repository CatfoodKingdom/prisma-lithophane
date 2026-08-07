from __future__ import annotations

import inspect
from pathlib import Path

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


def test_solve_pitch_guard_uses_effective_guide_printer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        server,
        "_effective_printers_data",
        lambda: {
            "printers": [{"id": "tutorial-printer", "nozzles": [0.2]}],
            "active_printer_id": "tutorial-printer",
            "active_nozzle_size": 0.2,
        },
    )

    server._validate_solve_pitch_for_nozzle(_config(0.2))


def test_production_solve_reuses_one_effective_guide_printer_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Reserved(RuntimeError):
        pass

    class FakeSolveConfig:
        palette = ["cyan"]

        @staticmethod
        def color_slots() -> int:
            return 4

    effective = {
        "printers": [{
            "id": "tutorial-printer",
            "name": "Tutorial Printer",
            "nozzle_profiles": [{
                "size": 0.2,
                "min_line_length_multiplier": 2,
            }],
        }],
        "active_printer_id": "tutorial-printer",
        "active_nozzle_size": 0.2,
    }
    captured: dict = {}

    monkeypatch.setitem(server.session["solve"], "status", "idle")
    monkeypatch.setattr(server, "_require_model_library", lambda: None)
    monkeypatch.setattr(server, "_effective_printers_data", lambda: effective)
    monkeypatch.setattr(server, "load_filament_order_registry", lambda: {})
    monkeypatch.setattr(server, "canonical_palette_order", lambda palette, _registry: list(palette))
    monkeypatch.setattr(server, "_build_solve_start_diagnostics", lambda *_args, **_kwargs: {})

    def build_config(_cfg: dict, **kwargs: dict) -> FakeSolveConfig:
        captured["active_printer"] = kwargs["active_printer"]
        return FakeSolveConfig()

    monkeypatch.setattr(server, "_build_solve_config", build_config)
    monkeypatch.setattr(
        server,
        "_reserve_model_job",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(Reserved()),
    )

    config = {
        **_config(0.2),
        "image_path": "tutorial.png",
        "palette": ["cyan"],
    }
    with pytest.raises(Reserved):
        server._start_full_solve_job(
            server.SolveStartPayload(card_id="guide-run"),
            config_override=config,
            image_path_override=Path("tutorial.png"),
        )

    assert captured["active_printer"]["printer"]["id"] == "tutorial-printer"
    assert captured["active_printer"]["nozzle"]["size"] == 0.2


def test_solve_start_validates_pitch_before_reserving_a_job() -> None:
    source = inspect.getsource(server._start_full_solve_job)
    assert "active_printer =" in source
    assert "_effective_printers_data()" in source
    assert "active=active_printer" in source
    assert "_build_solve_config(\n        cfg,\n        active_printer=active_printer," in source
    assert "active_printer=active_printer_override" not in source
    assert source.index("_validate_solve_pitch_for_nozzle(") < source.index("_reserve_model_job(")
