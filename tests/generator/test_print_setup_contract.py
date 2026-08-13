from __future__ import annotations

from copy import deepcopy

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from config.print_setup import (
    PrintSetupValueError,
    maximum_solve_pitch_multiplier,
    mm_to_um,
    resolved_print_setup_from_active,
)
import server


def _active(*, nozzle_um: int = 200, width_um: int = 220, line_multiplier: int = 3) -> dict:
    nozzle = {
        "id": "nozzle-a",
        "diameter_um": nozzle_um,
        "min_layer_height_um": 50,
        "max_layer_height_um": 150,
        "max_extrusion_width_um": max(nozzle_um, width_um),
        "minimum_line_length_multiplier": line_multiplier,
    }
    width = {"width_um": width_um}
    return {
        "printer": {"id": "printer-a"},
        "nozzle": nozzle,
        "extrusion_width": width,
        "printability": server._resolve_nozzle_printability(
            nozzle, width_um, printer_id="printer-a"
        ),
    }


def test_millimeter_input_round_trips_to_integer_micrometers() -> None:
    assert mm_to_um(0.22, field="width") == 220
    assert mm_to_um("0.333", field="width") == 333
    with pytest.raises(PrintSetupValueError, match="3 decimal"):
        mm_to_um(0.3333, field="width")


def test_resolver_uses_width_for_pitch_and_minimum_line_length() -> None:
    setup = resolved_print_setup_from_active(_active(), 4)
    assert setup.nozzle_diameter_um == 200
    assert setup.extrusion_width_um == 220
    assert setup.effective_solve_pitch_um == 880
    assert setup.minimum_line_length_um == 660
    assert setup.to_dict()["minimum_component_area_mm2"] == pytest.approx(0.1452)


def test_width_switch_preserves_multiplier_and_changes_effective_pitch() -> None:
    cfg = {
        **deepcopy(server._DEFAULT_CONFIG),
        "solve_pitch_extrusion_width_multiplier": 3,
    }
    first = server._with_active_printer_printability(cfg, active=_active(width_um=200))
    second = server._with_active_printer_printability(first, active=_active(width_um=400, nozzle_um=400))
    assert first["solve_pitch_extrusion_width_multiplier"] == 3
    assert second["solve_pitch_extrusion_width_multiplier"] == 3
    assert first["solver_fine_pitch_mm"] == pytest.approx(0.6)
    assert second["solver_fine_pitch_mm"] == pytest.approx(1.2)


def test_damaged_multiplier_is_clamped_with_structured_notice() -> None:
    width_um = 400
    result = server._with_active_printer_printability(
        {**server._DEFAULT_CONFIG, "solve_pitch_extrusion_width_multiplier": 10**9},
        active=_active(width_um=width_um, nozzle_um=400),
    )
    maximum = maximum_solve_pitch_multiplier(width_um)
    assert result["solve_pitch_extrusion_width_multiplier"] == maximum
    assert result["print_setup_repair"] == {
        "code": "solve_pitch_multiplier_clamped",
        "requested": 10**9,
        "effective": maximum,
    }


@pytest.mark.parametrize(
    ("payload", "adding_width"),
    [
        (
            {
                "intent_kind": "select_printer",
                "active_printer_id": "printer-a",
                "active_nozzle_id": "nozzle-a",
            },
            False,
        ),
        (
            {
                "intent_kind": "add_and_select_extrusion_width",
                "active_nozzle_id": "nozzle-a",
                "width_um": 220,
            },
            False,
        ),
    ],
)
def test_print_setup_intent_rejects_ambiguous_or_wrong_endpoint_mutations(
    payload: dict, adding_width: bool,
) -> None:
    with pytest.raises(HTTPException) as exc_info:
        server._print_setup_intent_kind(payload, adding_width=adding_width)
    assert exc_info.value.status_code == 422


def test_layer_height_conflict_does_not_mutate_until_explicit_acceptance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = deepcopy(server.session["config"])
    try:
        server.session["config"] = {**original, "layer_height": 0.20}
        active = _active()
        with pytest.raises(HTTPException) as exc_info:
            server._accept_or_reject_layer_height_transition(active, accept_correction=False)
        assert exc_info.value.status_code == 409
        assert server.session["config"]["layer_height"] == pytest.approx(0.20)
        correction = server._accept_or_reject_layer_height_transition(
            active, accept_correction=True, expected_layer_height_mm=0.20
        )
        assert correction == pytest.approx(0.15)
        assert server.session["config"]["layer_height"] == pytest.approx(0.20)
    finally:
        server.session["config"] = original


def test_numeric_width_shortcut_lifecycle_keeps_current_width_independent(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    printers_path = tmp_path / "config" / "printers.json"
    monkeypatch.setattr(server, "_PRINTERS_PATH", printers_path)
    original_guide = deepcopy(server.session["guide"])
    original_config = deepcopy(server.session["config"])
    server.session["guide"]["ghost_printer_mounted"] = False
    server.session["guide"]["printer_setup_overlay"] = None
    server.session["config"] = deepcopy(server._DEFAULT_CONFIG)
    client = TestClient(server.app)
    try:
        initial = client.get("/api/printers").json()
        assert initial["revision"] == 1

        selected_payload = {
                "expected_revision": 1,
                "active_printer_id": "bambu-x1c",
                "active_nozzle_id": "nozzle-200",
                "current_width_um": 220,
                "intent_kind": "select_extrusion_width",
                "mutation_id": "select-width-220",
        }
        selected_preview = client.put("/api/printers/active", json=selected_payload).json()
        selected = client.put(
            "/api/printers/active",
            json={**selected_payload, "acceptance_token": selected_preview["acceptance_token"]},
        )
        assert selected.status_code == 200, selected.text
        selected_data = selected.json()["printers_data"]
        selected_state = selected_data["printer_setup_state"]["bambu-x1c"]["nozzle_width_state"]["nozzle-200"]
        assert selected_data["revision"] == 2
        assert selected_state == {"current_width_um": 220, "saved_widths_um": [200]}

        added_payload = {
                "expected_revision": 2,
                "active_printer_id": "bambu-x1c",
                "active_nozzle_id": "nozzle-200",
                "width_um": 230,
                "intent_kind": "add_and_select_extrusion_width",
                "mutation_id": "add-width-230",
        }
        added_preview = client.post("/api/printers/width-shortcuts", json=added_payload).json()
        added = client.post(
            "/api/printers/width-shortcuts",
            json={**added_payload, "acceptance_token": added_preview["acceptance_token"]},
        )
        assert added.status_code == 200, added.text
        added_data = added.json()["printers_data"]
        added_state = added_data["printer_setup_state"]["bambu-x1c"]["nozzle_width_state"]["nozzle-200"]
        assert added_data["revision"] == 3
        assert added_state == {"current_width_um": 230, "saved_widths_um": [200, 230]}

        removed = client.request(
            "DELETE",
            "/api/printers/width-shortcuts",
            json={
                "expected_revision": 3,
                "active_printer_id": "bambu-x1c",
                "active_nozzle_id": "nozzle-200",
                "width_um": 230,
            },
        )
        assert removed.status_code == 200, removed.text
        removed_data = removed.json()["printers_data"]
        removed_state = removed_data["printer_setup_state"]["bambu-x1c"]["nozzle_width_state"]["nozzle-200"]
        assert removed_data["revision"] == 4
        assert removed_state == {"current_width_um": 230, "saved_widths_um": [200]}

        stale = client.put(
            "/api/printers/active",
            json={"expected_revision": 3, "active_nozzle_id": "nozzle-400"},
        )
        assert stale.status_code == 409
        detail = stale.json()["detail"]
        assert detail["error"] == "printer_revision_conflict"
        assert detail["printers_data"]["revision"] == 4
        assert detail["printers_data"]["printer_setup_state"]["bambu-x1c"]["active_nozzle_id"] == "nozzle-200"
    finally:
        server.session["guide"] = original_guide
        server.session["config"] = original_config


def test_print_setup_review_previews_all_consequences_before_atomic_acceptance(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    printers_path = tmp_path / "config" / "printers.json"
    monkeypatch.setattr(server, "_PRINTERS_PATH", printers_path)
    original_guide = deepcopy(server.session["guide"])
    original_config = deepcopy(server.session["config"])
    server._PRINT_SETUP_PENDING_REVIEWS.clear()
    server._PRINT_SETUP_ACCEPTED_MUTATIONS.clear()
    server.session["guide"]["ghost_printer_mounted"] = False
    server.session["guide"]["printer_setup_overlay"] = None
    server.session["config"] = {
        **deepcopy(server._DEFAULT_CONFIG),
        "layer_height": 0.05,
        "t_max": 2.95,
        "frame": {"width_mm": 100.1, "height_mm": 100.0},
    }
    client = TestClient(server.app)
    payload = {
        "expected_revision": 1,
        "intent_kind": "select_nozzle",
        "active_printer_id": "bambu-x1c",
        "active_nozzle_id": "nozzle-400",
        "mutation_id": "review-nozzle-400",
    }
    try:
        preview = client.put("/api/printers/active", json=payload)
        assert preview.status_code == 200, preview.text
        body = preview.json()
        assert body["status"] == "review_required"
        assert client.get("/api/printers").json()["revision"] == 1
        assert server.session["config"]["layer_height"] == pytest.approx(0.05)

        review = body["review"]
        assert review["intent"] == {"kind": "select_nozzle"}
        assert [item["field"] for item in review["requested_changes"]] == ["nozzle"]
        assert {item["field"] for item in review["dependent_changes"]} >= {
            "extrusion_width",
            "layer_height",
        }
        assert {item["field"] for item in review["derived_consequences"]} >= {
            "solve_pitch",
            "minimum_line_length",
        }
        assert review["attention_items"][0]["code"] == "image_dimensions_not_solve_pitch_aligned"
        assert review["attention_items"][0]["affected"] == ["width"]
        assert "settings_context_requires_attention" in {
            item["code"] for item in review["attention_items"]
        }

        accepted_payload = {**payload, "acceptance_token": body["acceptance_token"]}
        accepted = client.put("/api/printers/active", json=accepted_payload)
        assert accepted.status_code == 200, accepted.text
        applied = accepted.json()
        assert applied["status"] == "applied"
        assert applied["printers_data"]["revision"] == 2
        assert applied["nozzle"]["id"] == "nozzle-400"
        assert server.session["config"]["layer_height"] == pytest.approx(0.08)

        replay = client.put("/api/printers/active", json=accepted_payload)
        assert replay.status_code == 200
        assert replay.json()["printers_data"]["revision"] == 2
        assert client.get("/api/printers").json()["revision"] == 2
    finally:
        server._PRINT_SETUP_PENDING_REVIEWS.clear()
        server._PRINT_SETUP_ACCEPTED_MUTATIONS.clear()
        server.session["guide"] = original_guide
        server.session["config"] = original_config


def test_print_setup_review_rejects_changed_settings_context(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server, "_PRINTERS_PATH", tmp_path / "config" / "printers.json")
    original_guide = deepcopy(server.session["guide"])
    original_config = deepcopy(server.session["config"])
    server._PRINT_SETUP_PENDING_REVIEWS.clear()
    server.session["guide"]["ghost_printer_mounted"] = False
    server.session["guide"]["printer_setup_overlay"] = None
    server.session["config"] = deepcopy(server._DEFAULT_CONFIG)
    client = TestClient(server.app)
    payload = {
        "expected_revision": 1,
        "intent_kind": "select_extrusion_width",
        "active_printer_id": "bambu-x1c",
        "active_nozzle_id": "nozzle-200",
        "current_width_um": 220,
        "mutation_id": "stale-width-review",
    }
    try:
        preview = client.put("/api/printers/active", json=payload).json()
        assert preview["status"] == "review_required"
        server.session["config"]["solve_pitch_extrusion_width_multiplier"] = 2
        stale = client.put(
            "/api/printers/active",
            json={**payload, "acceptance_token": preview["acceptance_token"]},
        )
        assert stale.status_code == 200
        assert stale.json()["status"] == "stale"
        assert client.get("/api/printers").json()["revision"] == 1
    finally:
        server._PRINT_SETUP_PENDING_REVIEWS.clear()
        server.session["guide"] = original_guide
        server.session["config"] = original_config


def test_print_setup_acceptance_stays_stale_when_recomputed_consequences_disappear(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = server._normalize_printers_data(deepcopy(server._DEFAULT_PRINTERS))
    payload = {
        "expected_revision": data["revision"],
        "intent_kind": "select_printer",
        "active_printer_id": data["active_printer_id"],
        "mutation_id": "consequences-disappeared",
    }
    required_review = {
        "schema_version": 1,
        "intent": {"kind": "select_printer"},
        "requested_changes": [],
        "dependent_changes": [],
        "derived_consequences": [
            {"field": "filament_capacity", "before_slots": 4, "after_slots": 8}
        ],
        "attention_items": [],
    }
    empty_review = {
        **required_review,
        "derived_consequences": [],
    }
    reviews = iter([(required_review, None), (empty_review, None)])
    monkeypatch.setattr(server, "_print_setup_transition_review", lambda *_args, **_kwargs: next(reviews))
    monkeypatch.setattr(
        server,
        "_finish_printer_mutation",
        lambda *_args, **_kwargs: pytest.fail("a changed accepted proposal must not commit"),
    )
    server._PRINT_SETUP_PENDING_REVIEWS.clear()
    try:
        preview = server._review_or_finish_printer_mutation(
            data, deepcopy(data), payload, persistent=False
        )
        stale = server._review_or_finish_printer_mutation(
            data,
            deepcopy(data),
            {**payload, "acceptance_token": preview["acceptance_token"]},
            persistent=False,
        )
        assert stale["status"] == "stale"
        assert stale["reason"] == "review_context_changed"
    finally:
        server._PRINT_SETUP_PENDING_REVIEWS.clear()
