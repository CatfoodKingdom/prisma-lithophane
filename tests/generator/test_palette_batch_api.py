from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

import server
from source_images import ResolvedSource


@pytest.fixture(autouse=True)
def _restore_batch_state():
    original_batch = deepcopy(server.session["palette_batch"])
    original_solve = server.session["solve"]
    original_cache = server.session["solve_cache"]
    server.session["solve"] = deepcopy(server.session["solve"])
    server.session["solve_cache"] = {}
    try:
        yield
    finally:
        server.session["palette_batch"] = original_batch
        server.session["solve"] = original_solve
        server.session["solve_cache"] = original_cache


def _deck_palette(
    position: int,
    filament_ids: list[str] | None = None,
    *,
    name: str | None = None,
) -> dict:
    return {
        "deck_card_id": f"deck-{position}",
        "deck_card_name": name if name is not None else f"Deck palette {position}",
        "filament_ids": filament_ids or [f"color-{position}", "shared"],
    }


def _payload(**overrides) -> server.PaletteBatchStartPayload:
    data = {
        "image_path": "source.png",
        "deck_palettes": [_deck_palette(1), _deck_palette(2)],
        "profile_ref": {"kind": "system", "id": "system-default"},
        "profile_name_at_solve": "Default",
        "recipe_snapshot": {
            "palette": ["untrusted"],
            "profile_snapshot": {
                "name": "Default",
                "settings": {"t_max": 99},
                "modules": {"wrong": True},
            },
        },
    }
    data.update(overrides)
    return server.PaletteBatchStartPayload(**data)


def _item(position: int, palette: list[str] | None = None) -> dict:
    palette = palette or [f"color-{position}", "shared"]
    return {
        "position": position,
        "result_id": f"batch-batchjob-i{position:02d}",
        "deck_card_id": f"deck-{position}",
        "label": f"Deck palette {position}",
        "palette": list(palette),
        "status": "queued",
        "elapsed_s": 0.0,
        "result_available": False,
        "error": None,
        "recipe_snapshot": {"palette": list(palette)},
        "profile_ref": {"kind": "system", "id": "system-default"},
        "profile_name_at_solve": "Default",
        "is_profile_modified_at_solve": False,
    }


def _prime_batch(payload: server.PaletteBatchStartPayload) -> None:
    cfg = deepcopy(server._DEFAULT_CONFIG)
    cfg["image_path"] = payload.image_path
    items = [
        _item(position, requested.filament_ids)
        for position, requested in enumerate(payload.deck_palettes, start=1)
    ]
    server.session["palette_batch"].update({
        "status": "running",
        "job_id": "batch-job",
        "job_kind": "palette_batch",
        "phase": "preparing_source",
        "progress": {},
        "elapsed_s": 0.0,
        "started_monotonic": None,
        "item_count": len(items),
        "current_position": None,
        "items": items,
        "cancel_requested": False,
        "config_snapshot": cfg,
        "module_state": {"a1_bilateral_denoise": True},
        "active_printer": {
            "printer": {"id": "test"},
            "nozzle": {"size": 0.4, "min_line_length_multiplier": 2},
            "printability": {
                "minimum_extrusion_width_mm": 0.4,
                "minimum_line_length_mm": 0.8,
                "minimum_component_area_mm2": 0.32,
            },
        },
    })


def test_batch_payload_requires_two_to_ten_ordered_deck_items():
    assert len(_payload().deck_palettes) == 2
    assert len(_payload(deck_palettes=[_deck_palette(i) for i in range(1, 11)]).deck_palettes) == 10
    with pytest.raises(ValidationError):
        _payload(deck_palettes=[_deck_palette(1)])
    with pytest.raises(ValidationError):
        _payload(deck_palettes=[_deck_palette(i) for i in range(1, 12)])


def test_batch_payload_rejects_abandoned_suggestion_fields():
    for field, value in (
        ("top_k", 5),
        ("filament_ids", ["red", "blue"]),
        ("n_filaments", 4),
        ("max_swaps", 1),
        ("palette_mode", "standard"),
        ("improvement_threshold", 2.0),
        ("force_all_tiers", True),
    ):
        with pytest.raises(ValidationError):
            _payload(**{field: value})


def test_batch_payload_requires_unique_card_ids_but_keeps_equivalent_palettes():
    equivalent = ["red", "blue"]
    payload = _payload(deck_palettes=[
        _deck_palette(1, equivalent),
        _deck_palette(2, equivalent),
    ])
    assert [item.filament_ids for item in payload.deck_palettes] == [
        equivalent,
        equivalent,
    ]
    with pytest.raises(ValidationError):
        _payload(deck_palettes=[
            _deck_palette(1),
            {**_deck_palette(2), "deck_card_id": "deck-1"},
        ])
    with pytest.raises(ValidationError):
        _payload(deck_palettes=[
            _deck_palette(1, ["red", "red"]),
            _deck_palette(2),
        ])


def test_batch_labels_are_sanitized_with_position_fallback():
    assert server._sanitize_palette_batch_label("\x00  A\n  palette\t", 1) == "A palette"
    assert server._sanitize_palette_batch_label(" \n ", 7) == "Palette 7"


def test_item_materialization_reports_card_specific_model_errors(monkeypatch):
    payload = _payload(deck_palettes=[
        _deck_palette(1, ["available"]),
        _deck_palette(2, ["missing"]),
    ])
    cfg = deepcopy(server._DEFAULT_CONFIG)
    cfg["image_path"] = payload.image_path
    monkeypatch.setattr(server, "_load_registry", lambda: {})
    monkeypatch.setattr(
        server,
        "_runtime_profile_exists",
        lambda filament_id: filament_id != "missing",
    )
    monkeypatch.setattr(server, "_build_solve_config", lambda *_args, **_kwargs: object())

    with pytest.raises(server.HTTPException) as error:
        server._validate_palette_batch_items(
            job_id="batch-job",
            payload=payload,
            frozen_cfg=cfg,
            module_state={},
            active_printer={},
        )

    assert error.value.status_code == 422
    assert error.value.detail["items"] == [{
        "position": 2,
        "deck_card_id": "deck-2",
        "label": "Deck palette 2",
        "message": "missing calibration profile: missing",
    }]


def test_item_materialization_preserves_equivalent_explicit_cards(monkeypatch):
    palette = ["red", "blue"]
    payload = _payload(deck_palettes=[
        _deck_palette(1, palette, name=""),
        _deck_palette(2, palette, name="Second"),
    ])
    cfg = deepcopy(server._DEFAULT_CONFIG)
    cfg["image_path"] = payload.image_path
    monkeypatch.setattr(server, "_load_registry", lambda: {})
    monkeypatch.setattr(server, "_runtime_profile_exists", lambda _filament_id: True)
    monkeypatch.setattr(server, "_build_solve_config", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        server,
        "canonical_palette_order",
        lambda filament_ids, _registry: list(filament_ids),
    )

    items = server._validate_palette_batch_items(
        job_id="batch-job",
        payload=payload,
        frozen_cfg=cfg,
        module_state={"module": True},
        active_printer={},
    )

    assert [item["deck_card_id"] for item in items] == ["deck-1", "deck-2"]
    assert [item["palette"] for item in items] == [palette, palette]
    assert [item["label"] for item in items] == ["Palette 1", "Second"]
    assert [item["result_id"] for item in items] == [
        "batch-batch-jo-i01",
        "batch-batch-jo-i02",
    ]


def test_batch_start_captures_server_authority_and_materializes_before_reservation(
    monkeypatch,
):
    payload = _payload()
    server_config = deepcopy(server._DEFAULT_CONFIG)
    server_config.update({
        "image_path": payload.image_path,
        "t_max": 3.25,
        "detail_cap_enabled": True,
        "image_sample_pitch_mm": 0.4,
        "solver_fine_pitch_mm": 0.4,
    })
    module_state = {
        "a1_bilateral_denoise": {
            "enabled": True,
            "params": {"sigma": 0.5},
        },
    }
    active_printer = {
        "printer": {"id": "printer-a", "ams_units": 2, "slots_per_ams": 4},
        "nozzle": {"size": 0.4, "min_line_length_multiplier": 2},
        "printability": {
            "minimum_extrusion_width_mm": 0.4,
            "minimum_line_length_mm": 0.8,
            "minimum_component_area_mm2": 0.32,
        },
    }
    captured = {"order": []}

    class FakeThread:
        def __init__(self, *, target, args, name, daemon):
            captured["thread"] = (target, args, name, daemon)

        def start(self):
            captured["started"] = True

    def fake_materialize(**kwargs):
        captured["order"].append("validate")
        assert kwargs["frozen_cfg"]["t_max"] == 3.25
        return [_item(1), _item(2)]

    def fake_reserve(state):
        captured["order"].append("reserve")
        captured["state"] = state
        server.session["palette_batch"].update(state)

    monkeypatch.setattr(server, "_require_model_library", lambda: None)
    monkeypatch.setattr(server, "_cfg", lambda: server_config)
    monkeypatch.setattr(server, "load_module_state", lambda _path: module_state)
    monkeypatch.setattr(server, "get_active_printer", lambda: active_printer)
    monkeypatch.setattr(server, "_validate_palette_batch_items", fake_materialize)
    monkeypatch.setattr(server, "_reserve_palette_batch", fake_reserve)
    monkeypatch.setattr(server.threading, "Thread", FakeThread)

    response = server.start_palette_batch(payload)
    state = captured["state"]

    assert captured["order"] == ["validate", "reserve"]
    assert response["job_kind"] == "palette_batch"
    assert response["items"][0]["deck_card_id"] == "deck-1"
    assert captured["started"] is True
    assert state["config_snapshot"]["t_max"] == 3.25
    assert state["module_state"] == module_state
    assert state["active_printer"] == active_printer
    assert state["active_model_library_id"] == server._ACTIVE_MODEL_LIBRARY_ID
    assert ("suggestion_" + "snapshot") not in state

    server_config["t_max"] = 8.0
    module_state["a1_bilateral_denoise"]["params"]["sigma"] = 9.0
    active_printer["nozzle"]["size"] = 0.8
    assert state["config_snapshot"]["t_max"] == 3.25
    assert state["module_state"]["a1_bilateral_denoise"]["params"]["sigma"] == 0.5
    assert state["active_printer"]["nozzle"]["size"] == 0.4


def test_invalid_item_rejects_before_reservation(monkeypatch):
    payload = _payload()
    cfg = deepcopy(server._DEFAULT_CONFIG)
    cfg["image_path"] = payload.image_path
    reserved = False

    monkeypatch.setattr(server, "_require_model_library", lambda: None)
    monkeypatch.setattr(server, "_cfg", lambda: cfg)
    monkeypatch.setattr(server, "load_module_state", lambda _path: {})
    monkeypatch.setattr(server, "get_active_printer", lambda: {})
    monkeypatch.setattr(
        server,
        "_validate_palette_batch_items",
        lambda **_kwargs: (_ for _ in ()).throw(
            server.HTTPException(
                422,
                detail={
                    "error": "invalid_palette_batch",
                    "items": [{"deck_card_id": "deck-2", "message": "missing profile"}],
                },
            )
        ),
    )

    def reserve(_state):
        nonlocal reserved
        reserved = True

    monkeypatch.setattr(server, "_reserve_palette_batch", reserve)
    with pytest.raises(server.HTTPException) as error:
        server.start_palette_batch(payload)
    assert error.value.status_code == 422
    assert error.value.detail["items"][0]["deck_card_id"] == "deck-2"
    assert reserved is False


def test_authoritative_recipe_replaces_client_execution_carriers():
    cfg = deepcopy(server._DEFAULT_CONFIG)
    cfg["t_max"] = 3.25
    recipe = server._authoritative_batch_recipe(
        _payload().recipe_snapshot,
        cfg=cfg,
        module_state={"a1_bilateral_denoise": True},
        palette=["red", "blue"],
        profile_ref={"kind": "system", "id": "system-default"},
        profile_name="Default",
    )

    assert recipe["palette"] == ["red", "blue"]
    assert recipe["config"]["palette"] == ["red", "blue"]
    assert recipe["profile_snapshot"]["settings"]["t_max"] == 3.25
    assert recipe["profile_snapshot"]["modules"] == {
        "a1_bilateral_denoise": True
    }


def test_batch_source_is_private_and_immutable(tmp_path, monkeypatch):
    original = tmp_path / "source.png"
    original.write_bytes(b"original")
    resolved = ResolvedSource(
        original_path=original,
        working_path=original,
        display_name=original.name,
        source_format="PNG",
        fingerprint="digest",
        normalized=False,
        width=10,
        height=10,
    )
    monkeypatch.setattr(server.data_paths, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(server, "_resolve_config_image_source", lambda _cfg: original)
    monkeypatch.setattr(server, "_resolve_run_source_image", lambda _path: resolved)
    monkeypatch.setattr(
        server,
        "_source_provenance_for_config",
        lambda _cfg, _resolved: {"source_digest": "digest", "normalized": False},
    )

    _path, pinned, provenance = server._pin_palette_batch_source(
        "job",
        {"image_path": original.name},
    )
    original.write_bytes(b"changed")

    assert pinned.working_path.read_bytes() == b"original"
    assert provenance["batch_source_snapshot"] is True
    assert provenance["prepared_source_digest"]


def _pinned_source() -> ResolvedSource:
    return ResolvedSource(
        original_path=Path("source.png"),
        working_path=Path("pinned.png"),
        display_name="source.png",
        source_format="PNG",
        fingerprint="digest",
        normalized=False,
        width=10,
        height=10,
    )


def test_batch_runs_deck_items_sequentially_with_one_frozen_snapshot(monkeypatch):
    payload = _payload()
    _prime_batch(payload)
    calls = []
    pinned = _pinned_source()
    monkeypatch.setattr(
        server,
        "_pin_palette_batch_source",
        lambda *_args: (Path("source.png"), pinned, {"source_digest": "digest"}),
    )

    def fake_solve(solve_payload, **kwargs):
        calls.append((deepcopy(solve_payload), kwargs))
        inner_job_id = f"solve-{len(calls)}"
        server.session["solve"].update({
            "job_id": inner_job_id,
            "status": "complete",
            "progress": {"stage_label": "Complete", "overall_pct": 100},
        })
        server.session["solve_cache"][solve_payload.card_id] = {
            "config": deepcopy(kwargs["config_override"]),
            "solve": {"result": {"card_id": solve_payload.card_id}},
        }
        return {"job_id": inner_job_id, "status": "running"}

    monkeypatch.setattr(server, "_start_full_solve_job", fake_solve)
    server._run_palette_batch("batch-job", payload)

    assert server.session["palette_batch"]["status"] == "complete"
    assert len(calls) == 2
    assert [call[0].palette for call in calls] == [
        payload.deck_palettes[0].filament_ids,
        payload.deck_palettes[1].filament_ids,
    ]
    assert all(call[1]["auto_archive"] is False for call in calls)
    assert all(call[1]["resolved_source_override"] is pinned for call in calls)
    assert all(
        call[1]["module_state_override"] == {"a1_bilateral_denoise": True}
        for call in calls
    )
    assert all(
        call[1]["active_printer_override"] == calls[0][1]["active_printer_override"]
        for call in calls
    )
    first_cfg = {**calls[0][1]["config_override"], "palette": None}
    second_cfg = {**calls[1][1]["config_override"], "palette": None}
    assert first_cfg == second_cfg
    assert all(item["result_available"] for item in server.session["palette_batch"]["items"])


@pytest.mark.parametrize("fail_at_start", [False, True])
def test_failed_item_does_not_abort_later_items(monkeypatch, fail_at_start):
    payload = _payload()
    _prime_batch(payload)
    monkeypatch.setattr(
        server,
        "_pin_palette_batch_source",
        lambda *_args: (Path("source.png"), _pinned_source(), {"source_digest": "digest"}),
    )
    calls = []

    def fake_solve(solve_payload, **kwargs):
        calls.append((solve_payload.card_id, kwargs["config_override"]["palette"]))
        inner_job_id = f"solve-{len(calls)}"
        if len(calls) == 1:
            if fail_at_start:
                raise RuntimeError("Deliberate item start failure")
            server.session["solve"].update({
                "job_id": inner_job_id,
                "status": "error",
                "progress": {"stage_label": "Deliberate item failure"},
            })
        else:
            server.session["solve"].update({
                "job_id": inner_job_id,
                "status": "complete",
                "progress": {"stage_label": "Complete", "overall_pct": 100},
            })
            server.session["solve_cache"][solve_payload.card_id] = {
                "config": deepcopy(kwargs["config_override"]),
                "solve": {"result": {"card_id": solve_payload.card_id}},
            }
        return {"job_id": inner_job_id, "status": "running"}

    monkeypatch.setattr(server, "_start_full_solve_job", fake_solve)
    server._run_palette_batch("batch-job", payload)

    assert len(calls) == 2
    assert server.session["palette_batch"]["status"] == "partial"
    assert [item["status"] for item in server.session["palette_batch"]["items"]] == [
        "error",
        "complete",
    ]
    expected = "Deliberate item start failure" if fail_at_start else "Deliberate item failure"
    assert server.session["palette_batch"]["items"][0]["error"] == expected


def test_shared_preparation_failure_settles_every_item(monkeypatch):
    payload = _payload()
    _prime_batch(payload)
    monkeypatch.setattr(
        server,
        "_pin_palette_batch_source",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("source snapshot failed")),
    )

    server._run_palette_batch("batch-job", payload)

    assert server.session["palette_batch"]["status"] == "error"
    assert [item["status"] for item in server.session["palette_batch"]["items"]] == [
        "error",
        "error",
    ]
    assert all(
        item["error"] == "source snapshot failed"
        for item in server.session["palette_batch"]["items"]
    )


def test_cancellation_preserves_completed_item_and_marks_queue(monkeypatch):
    payload = _payload()
    _prime_batch(payload)
    monkeypatch.setattr(
        server,
        "_pin_palette_batch_source",
        lambda *_args: (Path("source.png"), _pinned_source(), {"source_digest": "digest"}),
    )

    def fake_solve(solve_payload, **kwargs):
        server.session["solve"].update({
            "job_id": "solve-1",
            "status": "complete",
            "progress": {"stage_label": "Complete", "overall_pct": 100},
        })
        server.session["solve_cache"][solve_payload.card_id] = {
            "config": deepcopy(kwargs["config_override"]),
            "solve": {"result": {"card_id": solve_payload.card_id}},
        }
        server.session["palette_batch"]["cancel_requested"] = True
        return {"job_id": "solve-1", "status": "running"}

    monkeypatch.setattr(server, "_start_full_solve_job", fake_solve)
    server._run_palette_batch("batch-job", payload)

    assert server.session["palette_batch"]["status"] == "cancelled"
    assert [item["status"] for item in server.session["palette_batch"]["items"]] == [
        "complete",
        "cancelled",
    ]
    assert server.session["palette_batch"]["items"][0]["result_available"] is True


def test_batch_status_is_lightweight_and_result_is_fetched_separately():
    payload = _payload()
    _prime_batch(payload)
    item = server.session["palette_batch"]["items"][0]
    item.update({
        "status": "complete",
        "elapsed_s": 2.0,
        "result_available": True,
    })
    server.session["solve_cache"][item["result_id"]] = {
        "config": {**deepcopy(server._DEFAULT_CONFIG), "palette": item["palette"]},
        "solve": {"result": {"card_id": item["result_id"], "mean_de": 0.1}},
    }

    status = server._serialize_palette_batch_status(server.session["palette_batch"])
    assert "result" not in status["items"][0]
    assert "recipe_snapshot" not in status["items"][0]
    assert status["items"][0]["deck_card_id"] == "deck-1"

    result = server.palette_batch_result("batch-job", item["result_id"])
    assert result["result"]["mean_de"] == 0.1
    assert result["recipe_snapshot"]["palette"] == item["palette"]
    assert result["position"] == 1

    with pytest.raises(server.HTTPException) as unrelated:
        server.palette_batch_result("batch-job", "batch-batchjob-i99")
    assert unrelated.value.status_code == 404


def test_palette_batch_blocks_other_model_and_image_jobs():
    payload = _payload()
    _prime_batch(payload)

    with pytest.raises(server.HTTPException, match="palette batch"):
        server._reserve_model_job(
            "suggest",
            already_running="Suggestion already running",
            state={"status": "running"},
        )
    with pytest.raises(server.HTTPException, match="palette batch"):
        server._assert_palette_batch_inactive("refresh images")


def test_palette_batch_cancellation_requires_current_job_id():
    payload = _payload()
    _prime_batch(payload)
    server.session["solve"].update({
        "status": "running",
        "job_id": "inner-solve",
        "cancel_requested": False,
    })

    with pytest.raises(server.HTTPException, match="no longer matches"):
        server.cancel_solve("stale-batch")

    assert server.session["palette_batch"]["cancel_requested"] is False
    assert server.session["solve"]["cancel_requested"] is False

    response = server.cancel_solve("batch-job")
    assert response == {"requested": True, "job_id": "batch-job"}
    assert server.session["palette_batch"]["cancel_requested"] is True
    assert server.session["solve"]["cancel_requested"] is True
