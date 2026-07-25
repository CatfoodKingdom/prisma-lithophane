import copy
import hashlib
import io
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

import auto_run_store
import data_paths
import run_archive
import run_store
import server
from source_images import SourceImageService
from thickness_maps import MapKey
from white_cap_contract import (
    PHYSICAL_GEOMETRY_METADATA_KEY,
    WHITE_CAP_FIELD_TARGET_METADATA_KEY,
    WHITE_CAP_FIELD_TARGET_SCHEMA,
    WHITE_CAP_FIELD_TARGET_UPPER_SURFACE_KEY,
)

_TPL = copy.deepcopy(server.session)
client = TestClient(server.app)


def _export_contract_for_shape(shape: tuple[int, int]) -> tuple[dict, dict]:
    return (
        {WHITE_CAP_FIELD_TARGET_UPPER_SURFACE_KEY: np.full(shape, 0.20, np.float32)},
        {
            PHYSICAL_GEOMETRY_METADATA_KEY: {
                "pitch_mm": 0.20,
                "solver_fine_pitch_mm": 0.20,
                "layer_height_mm": 0.08,
                "d_wb_mm": 0.20,
                "d_wc_min_mm": 0.08,
                "t_max_mm": 4.0,
                "luminance_mode": "standard",
                "cap_mode": "smooth_variable",
            },
            WHITE_CAP_FIELD_TARGET_METADATA_KEY: {
                "schema": WHITE_CAP_FIELD_TARGET_SCHEMA,
                "field_key": WHITE_CAP_FIELD_TARGET_UPPER_SURFACE_KEY,
                "policy": "standard_smooth_variable_canonical",
                "solve_mode": "standard",
                "luminance_mode": "standard",
                "cap_mode": "smooth_variable",
                "detail_smoothing_applied": False,
                "effective_d_wc_max_mm": 0.16,
                "effective_boundary_d_wc_max_mm": 0.16,
                "required_cover_floor_mm": 0.08,
            },
        },
    )


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    server.session.update(copy.deepcopy(_TPL))
    monkeypatch.setattr(data_paths, "SAVED_RUNS_DIR", tmp_path / "saved")
    monkeypatch.setattr(data_paths, "RUN_CACHE_DIR", tmp_path / "runs")
    monkeypatch.setattr(server, "_IMAGES_DIR", tmp_path / "photos")
    (tmp_path / "saved").mkdir(); (tmp_path / "runs").mkdir(); (tmp_path / "photos").mkdir()
    image_buf = io.BytesIO()
    Image.new("RGB", (8, 6), (32, 96, 160)).save(image_buf, format="JPEG")
    (server._IMAGES_DIR / "steve.jpg").write_bytes(image_buf.getvalue())
    monkeypatch.setattr(
        server,
        "_SOURCE_IMAGES",
        SourceImageService(server._IMAGES_DIR, tmp_path / "source-cache"),
    )
    yield
    server.session.update(copy.deepcopy(_TPL))


def _seed_cached_solve(card_id="run-1"):
    run_dir = data_paths.RUN_CACHE_DIR / card_id
    (run_dir / "post_solve_export_bundle").mkdir(parents=True, exist_ok=True)
    (run_dir / "predicted.png").write_bytes(b"png")
    (run_dir / "cap_map_contour.bin").write_bytes(b"\x00\x01\x02\x03")
    (run_dir / "post_solve_export_bundle" / "arrays.npz").write_bytes(b"bundle")
    export_maps, export_metadata = _export_contract_for_shape((4, 4))
    server.session["solve_cache"][card_id] = {
        "config": {"image_path": "steve.jpg", "d_wb": 0.2, "layer_height": 0.08, "palette": ["a"]},
        "solve": {
            "status": "complete", "card_id": card_id,
            # Reserved keys MUST be the exact MapKey strings (MapKey subclasses str).
            "thickness_maps": {MapKey.WHITE_CAP: np.zeros((4, 4), np.float32),
                               MapKey.WHITE_BOUNDARY_CAP: np.zeros((4, 4), np.float32),
                               MapKey.WHITE_DETAIL_CAP: np.zeros((4, 4), np.float32),
                               "a": np.ones((4, 4), np.float32)},
            "debug_maps": {"de_map": np.full((4, 4), 0.3, np.float32)},
            "export_maps": export_maps,
            "export_metadata": export_metadata,
            "image_domain_width_mm": 100.0, "image_domain_height_mm": 80.0,
            "grouping": None, "solved_plan": None,
            "result": {"mean_de": 1.2, "max_de": 4.0, "card_id": card_id,
                       "predicted_url": f"/api/run-cache/files/predicted.png?run={card_id}&t=1",
                       "filament_bin_urls": {"a": f"/api/run-cache/files/filament_a.bin?run={card_id}&t=1"}},
        },
    }


def test_save_uses_canonical_archive_helper(tmp_path):
    _seed_cached_solve("run-1")
    r = client.post("/api/runs/save", json={"card_id": "run-1", "label": "My Run"})
    assert r.status_code == 200
    save_id = r.json()["save_id"]
    sidecar = run_store.list_saves()[0]
    assert sidecar["save_id"] == save_id
    assert sidecar["tier"] == "saved"


def test_save_then_list(tmp_path):
    _seed_cached_solve("run-1")
    r = client.post("/api/runs/save", json={"card_id": "run-1", "label": "My Run"})
    assert r.status_code == 200
    save_id = r.json()["save_id"]
    listed = client.get("/api/runs/saved").json()
    assert any(s["save_id"] == save_id and s["label"] == "My Run" for s in listed)


@pytest.mark.parametrize("label", ["Run 7", " run 007 ", "rUn 9"])
def test_save_rejects_reserved_automatic_run_labels(label):
    _seed_cached_solve("run-1")

    response = client.post(
        "/api/runs/save",
        json={"card_id": "run-1", "label": label},
    )

    assert response.status_code == 422
    assert "reserved for automatic run labels" in response.text


@pytest.mark.parametrize("label", ["Run 7", " run 007 ", "rUn 9"])
def test_rename_rejects_reserved_automatic_run_labels(label):
    _seed_cached_solve("run-1")
    save_id = client.post(
        "/api/runs/save",
        json={"card_id": "run-1", "label": "Named run"},
    ).json()["save_id"]

    response = client.post(
        f"/api/runs/saved/{save_id}/rename",
        json={"label": label},
    )

    assert response.status_code == 422
    assert "reserved for automatic run labels" in response.text
    assert run_store.list_saves()[0]["label"] == "Named run"


def test_legacy_reserved_label_still_loads_without_becoming_identity():
    _seed_cached_solve("run-1")
    solve, config = server._cached_solve_or_409("run-1")
    save_id, zip_bytes, sidecar = server._pack_completed_run_archive(
        "run-1",
        solve,
        config,
        label="Run 7",
        saved_at="20260724-120000",
        root=data_paths.SAVED_RUNS_DIR,
        tier="saved",
    )
    run_store.write_save(save_id, zip_bytes, sidecar)

    response = client.post("/api/runs/load", json={"save_id": save_id})

    assert response.status_code == 200
    assert response.json()["label"] == "Run 7"
    assert response.json()["card_id"] != save_id


def test_settings_only_load_preserves_run_metadata_without_rehydrating(tmp_path):
    _seed_cached_solve("run-1")
    (data_paths.RUN_CACHE_DIR / "run-1" / "run.json").write_text(
        '{"recipe_snapshot":{"profile_snapshot":{"name":"Portrait","settings":{"layer_height":0.12},"modules":{"a1_bilateral_denoise":true}}}}',
        encoding="utf-8",
    )
    save_id = client.post("/api/runs/save", json={"card_id": "run-1", "label": "Portrait"}).json()["save_id"]
    before_cache = set(server.session["solve_cache"])
    before_images = {path.name for path in server._IMAGES_DIR.iterdir()}
    response = client.post("/api/runs/settings", json={"save_id": save_id, "tier": "saved"})

    assert response.status_code == 200
    body = response.json()
    assert body["run_metadata"]["recipe_snapshot"]["profile_snapshot"]["name"] == "Portrait"
    assert body["config"]["image_path"] == "steve.jpg"
    assert "image_source_ref" not in body["config"]
    assert "image_source_ref" not in str(body["run_metadata"])
    assert set(server.session["solve_cache"]) == before_cache
    assert {path.name for path in server._IMAGES_DIR.iterdir()} == before_images


def test_save_409_when_card_not_cached():
    r = client.post("/api/runs/save", json={"card_id": "nope"})
    assert r.status_code == 409


def test_download_returns_zip(tmp_path):
    _seed_cached_solve("run-1")
    save_id = client.post("/api/runs/save", json={"card_id": "run-1"}).json()["save_id"]
    r = client.get(f"/api/runs/saved/{save_id}/download")
    assert r.status_code == 200 and r.content[:2] == b"PK"


def test_saved_run_preview_returns_thumbnail_from_archived_source_image():
    _seed_cached_solve("run-1")
    image_path = server._IMAGES_DIR / "steve.jpg"
    buf = io.BytesIO()
    Image.new("RGB", (640, 360), (32, 96, 160)).save(buf, format="JPEG")
    image_path.write_bytes(buf.getvalue())
    save_id = client.post("/api/runs/save", json={"card_id": "run-1", "label": "Preview"}).json()["save_id"]

    response = client.get(f"/api/runs/saved/{save_id}/preview")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/jpeg")
    with Image.open(io.BytesIO(response.content)) as preview:
        assert preview.format == "JPEG"
        assert preview.size == (320, 180)


def test_saved_run_preview_missing_archive_is_not_a_generic_server_error():
    response = client.get("/api/runs/saved/does-not-exist/preview")
    assert response.status_code == 404


def test_rename_then_delete(tmp_path):
    _seed_cached_solve("run-1")
    save_id = client.post("/api/runs/save", json={"card_id": "run-1"}).json()["save_id"]
    assert client.post(f"/api/runs/saved/{save_id}/rename", json={"label": "Renamed"}).status_code == 200
    assert any(s["label"] == "Renamed" for s in client.get("/api/runs/saved").json())
    assert client.request("DELETE", f"/api/runs/saved/{save_id}").status_code == 200
    assert all(s["save_id"] != save_id for s in client.get("/api/runs/saved").json())


def test_delete_locked_save_returns_4xx_not_500(tmp_path, monkeypatch):
    """A locked/open zip makes unlink raise PermissionError. The DELETE endpoint
    must return a clean, informative 4xx (not an unhandled 500)."""
    _seed_cached_solve("run-1")
    save_id = client.post("/api/runs/save", json={"card_id": "run-1"}).json()["save_id"]

    def _boom(self, *a, **k):
        raise PermissionError("file is in use")

    monkeypatch.setattr(Path, "unlink", _boom)
    r = client.request("DELETE", f"/api/runs/saved/{save_id}")
    assert 400 <= r.status_code < 500, f"expected 4xx, got {r.status_code}"
    assert r.status_code != 500
    detail = r.json()["detail"]
    assert save_id in detail and "in use" in detail.lower()


def test_save_409_when_source_image_missing(tmp_path):
    _seed_cached_solve("run-1")
    (server._IMAGES_DIR / "steve.jpg").unlink()  # image gone -> save must fail loud
    assert client.post("/api/runs/save", json={"card_id": "run-1"}).status_code == 409


def test_load_from_list_rehydrates_fresh_card(tmp_path):
    _seed_cached_solve("run-1")
    save_id = client.post("/api/runs/save", json={"card_id": "run-1"}).json()["save_id"]
    server.session["solve_cache"].clear()  # simulate a fresh session
    r = client.post("/api/runs/load", json={"save_id": save_id})
    assert r.status_code == 200
    body = r.json()
    new_card = body["card_id"]
    assert new_card and new_card != "run-1"
    assert new_card in server.session["solve_cache"]
    entry = server.session["solve_cache"][new_card]["solve"]
    assert entry["status"] == "complete"
    assert entry["image_domain_width_mm"] == 100.0  # required field rehydrated
    # diagnostic URL rebased to the fresh card — top-level AND nested maps
    assert f"run={new_card}" in body["result"]["predicted_url"]
    assert f"run={new_card}" in body["result"]["filament_bin_urls"]["a"]
    # the WHOLE run-cache subtree is restored under the fresh card (png + bin + nested bundle)
    assert (data_paths.RUN_CACHE_DIR / new_card / "predicted.png").exists()
    assert (data_paths.RUN_CACHE_DIR / new_card / "cap_map_contour.bin").exists()
    assert (data_paths.RUN_CACHE_DIR / new_card / "post_solve_export_bundle" / "arrays.npz").exists()


def test_load_retains_source_privately_without_mutating_image_library(tmp_path):
    _seed_cached_solve("run-1")
    save_id = client.post("/api/runs/save", json={"card_id": "run-1"}).json()["save_id"]
    server.session["solve_cache"].clear()
    archived = run_archive.read_run_archive(run_store.read_zip_bytes(save_id))
    different = io.BytesIO()
    Image.new("RGB", (8, 6), (180, 24, 50)).save(different, format="JPEG")
    (server._IMAGES_DIR / "steve.jpg").write_bytes(different.getvalue())
    before = {path.name: path.read_bytes() for path in server._IMAGES_DIR.iterdir()}
    body = client.post("/api/runs/load", json={"save_id": save_id}).json()
    new_card = body["card_id"]
    cached = server.session["solve_cache"][new_card]
    private = cached["solve"]["_loaded_source"]
    private_path = data_paths.RUN_CACHE_DIR / new_card / private["relative_path"]

    assert body["config"]["image_path"] == "steve.jpg"
    assert body["config"]["image_source_ref"] == f"loaded-run:{new_card}"
    assert body["source_image"]["temporary"] is True
    assert body["source_image"]["source_ref"] == f"loaded-run:{new_card}"
    assert private_path.read_bytes() == archived.image_bytes
    assert {path.name: path.read_bytes() for path in server._IMAGES_DIR.iterdir()} == before

    second = client.post("/api/runs/load", json={"save_id": save_id})
    assert second.status_code == 200
    assert {path.name: path.read_bytes() for path in server._IMAGES_DIR.iterdir()} == before


def test_load_reuses_exact_same_name_library_source_but_keeps_private_snapshot():
    _seed_cached_solve("run-1")
    save_id = client.post("/api/runs/save", json={"card_id": "run-1"}).json()["save_id"]
    archived = run_archive.read_run_archive(run_store.read_zip_bytes(save_id))
    server.session["solve_cache"].clear()

    response = client.post("/api/runs/load", json={"save_id": save_id})

    assert response.status_code == 200
    body = response.json()
    assert body["source_image"]["filename"] == "steve.jpg"
    assert body["source_image"]["source_ref"] is None
    assert body["source_image"]["temporary"] is False
    cached = server.session["solve_cache"][body["card_id"]]
    assert cached["config"]["image_source_ref"] == f"loaded-run:{body['card_id']}"
    private = cached["solve"]["_loaded_source"]
    private_path = data_paths.RUN_CACHE_DIR / body["card_id"] / private["relative_path"]
    assert private_path.read_bytes() == archived.image_bytes


def test_load_reuses_deterministic_differently_named_exact_library_source():
    _seed_cached_solve("run-1")
    original = (server._IMAGES_DIR / "steve.jpg").read_bytes()
    save_id = client.post("/api/runs/save", json={"card_id": "run-1"}).json()["save_id"]
    server.session["solve_cache"].clear()
    (server._IMAGES_DIR / "steve.jpg").unlink()
    (server._IMAGES_DIR / "z-copy.jpg").write_bytes(original)
    (server._IMAGES_DIR / "A-copy.jpg").write_bytes(original)

    response = client.post("/api/runs/load", json={"save_id": save_id})

    assert response.status_code == 200
    source = response.json()["source_image"]
    assert source["filename"] == "A-copy.jpg"
    assert source["source_ref"] is None
    assert source["temporary"] is False


@pytest.mark.parametrize("library_mutation", ["modify", "delete"])
def test_loaded_run_resave_preserves_archived_image_name_and_bytes(library_mutation):
    _seed_cached_solve("run-1")
    first_save_id = client.post(
        "/api/runs/save",
        json={"card_id": "run-1", "label": "Original"},
    ).json()["save_id"]
    first = run_archive.read_run_archive(run_store.read_zip_bytes(first_save_id))
    server.session["solve_cache"].clear()
    loaded = client.post("/api/runs/load", json={"save_id": first_save_id}).json()
    assert loaded["source_image"]["temporary"] is False

    if library_mutation == "modify":
        replacement = io.BytesIO()
        Image.new("RGB", (8, 6), (220, 190, 20)).save(replacement, format="JPEG")
        (server._IMAGES_DIR / "steve.jpg").write_bytes(replacement.getvalue())
    else:
        (server._IMAGES_DIR / "steve.jpg").unlink()
    second_save_id = client.post(
        "/api/runs/save",
        json={"card_id": loaded["card_id"], "label": "Resaved"},
    ).json()["save_id"]
    second = run_archive.read_run_archive(run_store.read_zip_bytes(second_save_id))

    assert second.image_name == first.image_name
    assert second.image_bytes == first.image_bytes
    assert "image_source_ref" not in str(second.run_json)
    assert not any(
        rel == server._LOADED_SOURCE_PRIVATE_DIR
        or rel.startswith(f"{server._LOADED_SOURCE_PRIVATE_DIR}/")
        for rel in second.run_cache_files
    )


def test_private_loaded_source_preview_is_resized_and_generic_cache_route_is_blocked():
    _seed_cached_solve("run-1")
    save_id = client.post("/api/runs/save", json={"card_id": "run-1"}).json()["save_id"]
    different = io.BytesIO()
    Image.new("RGB", (8, 6), (180, 24, 50)).save(different, format="JPEG")
    (server._IMAGES_DIR / "steve.jpg").write_bytes(different.getvalue())
    body = client.post("/api/runs/load", json={"save_id": save_id}).json()
    ref = body["source_image"]["source_ref"]

    preview = client.get(
        f"/api/images/preview/steve.jpg?image_source_ref={ref}"
    )
    direct = client.get(
        f"/api/run-cache/files/{server._LOADED_SOURCE_PRIVATE_DIR}/steve.jpg"
        f"?run={body['card_id']}"
    )

    assert preview.status_code == 200
    assert preview.headers["content-type"].startswith("image/jpeg")
    assert direct.status_code == 404


def test_loaded_source_reference_validation_and_reserved_archive_namespace():
    assert client.get(
        "/api/images/preview/steve.jpg?image_source_ref=loaded-run:../escape"
    ).status_code == 400
    assert client.get(
        "/api/images/preview/steve.jpg?image_source_ref=loaded-run:missing"
    ).status_code == 404

    _seed_cached_solve("run-1")
    save_id = client.post("/api/runs/save", json={"card_id": "run-1"}).json()["save_id"]
    parsed = run_archive.read_run_archive(run_store.read_zip_bytes(save_id))
    hostile = run_archive.pack_run_archive(
        run_json=parsed.run_json,
        thickness_arrays=parsed.thickness_arrays,
        image_bytes=parsed.image_bytes,
        image_name=parsed.image_name,
        solve_state=parsed.solve_state,
        run_cache_files={
            **parsed.run_cache_files,
            f"{server._LOADED_SOURCE_PRIVATE_DIR.upper()}/injected.jpg": parsed.image_bytes,
        },
    )

    response = client.post(
        "/api/runs/load-upload",
        files={"file": ("reserved.zip", hostile, "application/zip")},
    )
    assert response.status_code == 400
    assert "reserved loaded-source" in response.text


def test_private_reference_requires_its_archived_display_name():
    _seed_cached_solve("run-1")
    save_id = client.post("/api/runs/save", json={"card_id": "run-1"}).json()["save_id"]
    different = io.BytesIO()
    Image.new("RGB", (8, 6), (180, 24, 50)).save(different, format="JPEG")
    (server._IMAGES_DIR / "steve.jpg").write_bytes(different.getvalue())
    body = client.post("/api/runs/load", json={"save_id": save_id}).json()

    response = client.get(
        "/api/images/preview/not-steve.jpg"
        f"?image_source_ref={body['source_image']['source_ref']}"
    )

    assert response.status_code == 400
    assert "does not match" in response.text


def test_normalized_archive_provenance_uses_original_digest_and_legacy_falls_back_private():
    original_digest = "a" * 64
    parsed = SimpleNamespace(
        image_name="portrait.prisma-source.png",
        image_bytes=b"normalized PNG bytes",
    )
    run_json = {
        "source_image_name": "portrait.heic",
        "source_asset": {
            "original_source_name": "portrait.heic",
            "source_digest": original_digest,
            "normalized": True,
        },
    }

    assert server._loaded_archive_was_normalized(parsed, run_json, "portrait.heic") is True
    assert server._loaded_archive_source_digest(
        parsed,
        run_json,
        normalized=True,
        trust_normalized_provenance=True,
    ) == original_digest
    assert server._loaded_archive_source_digest(
        parsed,
        run_json,
        normalized=True,
        trust_normalized_provenance=False,
    ) is None
    assert server._loaded_archive_source_digest(
        parsed,
        {"source_image_name": "portrait.heic"},
        normalized=True,
        trust_normalized_provenance=True,
    ) is None
    assert server._loaded_archive_source_digest(
        parsed,
        run_json,
        normalized=False,
        trust_normalized_provenance=False,
    ) == hashlib.sha256(parsed.image_bytes).hexdigest()


@pytest.mark.parametrize(
    ("source_asset", "display_name", "member_name", "message"),
    [
        ({"normalized": False}, "portrait.heic", "portrait.prisma-source.png", "native-source"),
        ({"normalized": True}, "portrait.jpg", "portrait.png", "normalized-source"),
        ({"normalized": "false"}, "portrait.jpg", "portrait.jpg", "must be boolean"),
    ],
)
def test_loaded_archive_rejects_contradictory_source_provenance(
    source_asset,
    display_name,
    member_name,
    message,
):
    from fastapi import HTTPException

    parsed = SimpleNamespace(image_name=member_name, image_bytes=b"image")
    with pytest.raises(HTTPException, match=message):
        server._loaded_archive_was_normalized(
            parsed,
            {"source_asset": source_asset},
            display_name,
        )


def test_uploaded_normalized_archive_does_not_trust_original_digest(monkeypatch):
    image = io.BytesIO()
    Image.new("RGB", (5, 4), (12, 34, 56)).save(image, format="PNG")
    raw = run_archive.pack_run_archive(
        run_json={
            "schema_version": run_archive.SCHEMA_VERSION,
            "source_image_name": "phone.heic",
            "config": {},
            "palette": [],
            "result": {},
            "source_asset": {
                "original_source_name": "phone.heic",
                "source_digest": "a" * 64,
                "normalized": True,
            },
        },
        thickness_arrays={},
        image_bytes=image.getvalue(),
        image_name="phone.prisma-source.png",
    )
    observed = []
    original = server._matching_library_source

    def capture_match(**kwargs):
        observed.append(kwargs["expected_digest"])
        return original(**kwargs)

    monkeypatch.setattr(server, "_matching_library_source", capture_match)
    response = client.post(
        "/api/runs/load-upload",
        files={"file": ("normalized.zip", raw, "application/zip")},
    )

    assert response.status_code == 200
    assert response.json()["source_image"]["temporary"] is True
    assert observed == [None]


def test_load_409_while_solve_running(tmp_path):
    _seed_cached_solve("run-1")
    save_id = client.post("/api/runs/save", json={"card_id": "run-1"}).json()["save_id"]
    server.session["solve"]["status"] = "running"
    assert client.post("/api/runs/load", json={"save_id": save_id}).status_code == 409


def test_load_from_upload(tmp_path):
    _seed_cached_solve("run-1")
    save_id = client.post("/api/runs/save", json={"card_id": "run-1"}).json()["save_id"]
    zip_bytes = run_store.read_zip_bytes(save_id)
    server.session["solve_cache"].clear()
    r = client.post("/api/runs/load-upload", files={"file": ("x.zip", zip_bytes, "application/zip")})
    assert r.status_code == 200 and r.json()["card_id"] in server.session["solve_cache"]


@pytest.mark.parametrize(
    "archived_config",
    [
        {"cap_mode": "fixed"},
        {"cap_fixed_thickness_mm": None},
        {"printability_preferred_line_length_mm": None},
        {"detail_cap_enabled": False},
    ],
)
def test_load_rejects_retired_or_disabled_cap_archive_before_side_effects(archived_config):
    from fastapi import HTTPException

    parsed = SimpleNamespace(
        run_json={"config": archived_config},
        image_name="steve.jpg",
        image_bytes=b"stale image",
    )
    before_images = sorted(path.name for path in server._IMAGES_DIR.iterdir())

    with pytest.raises(HTTPException) as excinfo:
        server._rehydrate_loaded_archive(parsed)

    assert excinfo.value.status_code == 422
    assert server.session["solve_cache"] == {}
    assert sorted(path.name for path in server._IMAGES_DIR.iterdir()) == before_images


@pytest.mark.parametrize(
    "parsed_kwargs",
    [
        {"thickness_arrays": {"dbg__blueprint_printability_soft_warn": np.zeros((1, 1))}},
        {
            "run_json": {
                "config": {},
                "result": {
                    "debug_map_urls": {
                        "blueprint_printability_soft_warn": "/api/run-cache/files/x.png"
                    }
                },
            }
        },
        {
            "run_json": {
                "config": {},
                "run_metadata": {
                    "staged_metrics": {
                        "blueprint_printability_soft_warn_pixels": 1
                    }
                },
            }
        },
        {
            "run_json": {
                "config": {},
                "export_metadata": {
                    "blueprint_printability_preferred_line_length_mm": 0.6
                },
            }
        },
        {"run_cache_files": {"debug/blueprint_printability_soft_warn.png": b""}},
    ],
)
def test_load_rejects_retired_preferred_length_archive_artifacts_before_side_effects(
    parsed_kwargs,
):
    from fastapi import HTTPException

    parsed_data = {
        "run_json": {"config": {}},
        "image_name": "steve.jpg",
        "image_bytes": b"stale image",
        "thickness_arrays": {},
        "run_cache_files": {},
    }
    parsed_data.update(parsed_kwargs)
    parsed = SimpleNamespace(**parsed_data)
    before_images = sorted(path.name for path in server._IMAGES_DIR.iterdir())

    with pytest.raises(HTTPException) as excinfo:
        server._rehydrate_loaded_archive(parsed)

    assert excinfo.value.status_code == 422
    assert excinfo.value.detail["error"] == "retired_archive_artifact"
    assert server.session["solve_cache"] == {}
    assert sorted(path.name for path in server._IMAGES_DIR.iterdir()) == before_images


def test_load_upload_413_when_too_large(tmp_path, monkeypatch):
    import run_archive
    monkeypatch.setattr(run_archive, "MAX_UPLOAD_BYTES", 16)  # tiny cap
    blob = b"PK\x03\x04" + b"\0" * 4096  # > cap; rejected before archive validation
    r = client.post("/api/runs/load-upload", files={"file": ("x.zip", blob, "application/zip")})
    assert r.status_code == 413


def test_maybe_write_auto_run_writes_loadable_auto_record(tmp_path, monkeypatch):
    monkeypatch.setattr(data_paths, "AUTO_RUNS_DIR", tmp_path / "auto")
    _seed_cached_solve("run-1")
    cached = server.session["solve_cache"]["run-1"]

    server._maybe_write_auto_run("run-1", cached["solve"], cached["config"], saved_at="20260616-101500")

    listed = auto_run_store.list_auto_runs()
    assert len(listed) == 1
    raw = auto_run_store.read_auto_zip_bytes(listed[0]["save_id"])
    parsed = run_archive.read_run_archive(raw)
    assert parsed.run_json["palette"] == ["a"]
    assert "tm____white_cap__" in parsed.thickness_arrays
    assert f"ex__{WHITE_CAP_FIELD_TARGET_UPPER_SURFACE_KEY}" in parsed.thickness_arrays
    assert WHITE_CAP_FIELD_TARGET_METADATA_KEY in parsed.run_json["export_metadata"]


def _server_function_source(*names: str) -> str:
    """Return source for a top-level function or nested function by AST span."""
    import ast
    source = Path(server.__file__).read_text(encoding="utf-8")
    lines = source.splitlines()
    nodes = ast.parse(source).body
    current = None
    for name in names:
        current = next(
            node for node in nodes
            if isinstance(node, ast.FunctionDef) and node.name == name
        )
        nodes = current.body
    return "\n".join(lines[current.lineno - 1:current.end_lineno])


def test_solve_worker_uses_single_completed_cache_assignment_seam():
    helper_source = _server_function_source("_write_completed_solve_cache_entry")
    worker_source = _server_function_source("start_solve", "_run_solve")
    load_source = _server_function_source("_rehydrate_loaded_archive")
    assignment = 'session["solve_cache"][card_id] = {'

    assert assignment in helper_source
    assert "_write_completed_solve_cache_entry(" in worker_source
    assert assignment not in worker_source
    assert assignment in load_source

    full_source = Path(server.__file__).read_text(encoding="utf-8")
    assert full_source.count(assignment) == 2


def test_maybe_write_auto_run_swallows_pack_failure(monkeypatch):
    _seed_cached_solve("run-1")
    cached = server.session["solve_cache"]["run-1"]

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(server, "_pack_completed_run_archive", boom)
    server._maybe_write_auto_run("run-1", cached["solve"], cached["config"])
    assert server.session["solve_cache"]["run-1"]["solve"]["status"] == "complete"


def test_solve_completion_hook_receives_completed_cache_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(data_paths, "RUN_CACHE_DIR", tmp_path / "runs")
    monkeypatch.setattr(data_paths, "AUTO_RUNS_DIR", tmp_path / "auto")
    _seed_cached_solve("run-1")
    run_dir = data_paths.RUN_CACHE_DIR / "run-1"
    (run_dir / "predicted.png").write_bytes(b"png")
    (run_dir / "run.json").write_text("{}", encoding="utf-8")
    captured = {}

    def capture(card_id, solve, cfg, *, saved_at=None):
        captured["card_id"] = card_id
        captured["solve"] = solve
        captured["cfg"] = cfg
        captured["has_predicted"] = (data_paths.RUN_CACHE_DIR / card_id / "predicted.png").exists()
        captured["has_run_json"] = (data_paths.RUN_CACHE_DIR / card_id / "run.json").exists()

    monkeypatch.setattr(server, "_maybe_write_auto_run", capture)
    cached = server.session["solve_cache"]["run-1"]
    result = SimpleNamespace(
        thickness_maps=cached["solve"]["thickness_maps"],
        grouping=cached["solve"].get("grouping"),
        debug_maps=cached["solve"].get("debug_maps") or {},
    )

    entry = server._write_completed_solve_cache_entry("run-1", cached["config"], cached["solve"], result)
    server._maybe_write_auto_run("run-1", entry["solve"], entry["config"])

    assert captured["card_id"] == "run-1"
    assert captured["solve"]["status"] == "complete"
    assert captured["solve"]["result"]
    assert captured["solve"]["thickness_maps"]
    assert captured["solve"]["image_domain_width_mm"] is not None
    assert captured["solve"]["image_domain_height_mm"] is not None
    assert captured["has_predicted"] is True
    assert captured["has_run_json"] is True


# --- S3.2: merged saved+auto list with tiers ---------------------------------


def test_list_saved_runs_returns_saved_and_auto_with_tiers(tmp_path, monkeypatch):
    monkeypatch.setattr(data_paths, "AUTO_RUNS_DIR", tmp_path / "auto")
    run_store.write_save("saved-1", b"ZIP", {"save_id": "saved-1", "label": "Saved", "saved_at": "20260616-101500", "tier": "saved"})
    auto_run_store.write_auto_run("auto-1", b"ZIP", {"save_id": "auto-1", "label": "Auto", "saved_at": "20260616-101600", "tier": "auto"})

    rows = client.get("/api/runs/saved").json()

    assert [(r["save_id"], r["tier"]) for r in rows] == [("auto-1", "auto"), ("saved-1", "saved")]


# --- S3.3: tiered auto load + download ---------------------------------------


def test_load_auto_run_uses_same_rehydrate_path(tmp_path, monkeypatch):
    monkeypatch.setattr(data_paths, "AUTO_RUNS_DIR", tmp_path / "auto")
    _seed_cached_solve("run-1")
    cached = server.session["solve_cache"]["run-1"]
    server._maybe_write_auto_run("run-1", cached["solve"], cached["config"], saved_at="20260616-101500")
    server.session["solve_cache"].clear()
    auto_id = auto_run_store.list_auto_runs()[0]["save_id"]

    r = client.post("/api/runs/load", json={"save_id": auto_id, "tier": "auto"})

    assert r.status_code == 200
    assert r.json()["card_id"] in server.session["solve_cache"]


def test_load_rejects_unknown_tier_with_422():
    # tier is Literal["saved","auto"]; anything else is rejected at the boundary.
    r = client.post("/api/runs/load", json={"save_id": "whatever", "tier": "bogus"})
    assert r.status_code == 422


def test_download_auto_run_returns_zip(tmp_path, monkeypatch):
    monkeypatch.setattr(data_paths, "AUTO_RUNS_DIR", tmp_path / "auto")
    auto_run_store.write_auto_run("auto-1", b"PKZIP", {"save_id": "auto-1", "label": "Auto", "saved_at": "20260616-101500", "tier": "auto"})
    r = client.get("/api/runs/auto/auto-1/download")
    assert r.status_code == 200
    assert r.content == b"PKZIP"


# --- S3.4: promote auto run --------------------------------------------------


def test_promote_auto_moves_to_saved_and_removes_auto(tmp_path, monkeypatch):
    monkeypatch.setattr(data_paths, "AUTO_RUNS_DIR", tmp_path / "auto")
    monkeypatch.setattr(data_paths, "SAVED_RUNS_DIR", tmp_path / "saved")
    auto_run_store.write_auto_run(
        "20260616-101500-steve",
        b"PKZIP",
        {"save_id": "20260616-101500-steve", "label": "Auto", "saved_at": "20260616-101500", "source_image_name": "steve.jpg", "tier": "auto"},
    )

    r = client.post("/api/runs/auto/20260616-101500-steve/promote")

    assert r.status_code == 200
    promoted = r.json()
    assert promoted["tier"] == "saved"
    assert run_store.read_zip_bytes(promoted["save_id"]) == b"PKZIP"
    assert auto_run_store.list_auto_runs() == []


def test_delete_auto_run_removes_autosave(tmp_path, monkeypatch):
    monkeypatch.setattr(data_paths, "AUTO_RUNS_DIR", tmp_path / "auto")
    auto_run_store.write_auto_run(
        "auto-1",
        b"PKZIP",
        {"save_id": "auto-1", "label": "Auto", "saved_at": "20260616-101500", "source_image_name": "steve.jpg", "tier": "auto"},
    )

    r = client.request("DELETE", "/api/runs/auto/auto-1")

    assert r.status_code == 200
    assert r.json() == {"deleted": "auto-1", "tier": "auto"}
    assert auto_run_store.list_auto_runs() == []


def test_promote_auto_409_while_job_running(tmp_path, monkeypatch):
    monkeypatch.setattr(data_paths, "AUTO_RUNS_DIR", tmp_path / "auto")
    auto_run_store.write_auto_run("auto-1", b"PKZIP", {"save_id": "auto-1", "label": "Auto", "saved_at": "20260616-101500", "source_image_name": "steve.jpg", "tier": "auto"})
    server.session["solve"]["status"] = "running"
    assert client.post("/api/runs/auto/auto-1/promote").status_code == 409


# --- S3.5: stale auto rows return clean 404 ----------------------------------


def test_stale_auto_id_after_eviction_returns_404(tmp_path, monkeypatch):
    monkeypatch.setattr(data_paths, "AUTO_RUNS_DIR", tmp_path / "auto")
    monkeypatch.setattr(data_paths, "SAVED_RUNS_DIR", tmp_path / "saved")
    for i in range(2):
        auto_run_store.write_auto_run(
            f"auto-{i}",
            b"PKZIP",
            {"save_id": f"auto-{i}", "label": f"Auto {i}", "saved_at": f"20260616-10150{i}", "source_image_name": "steve.jpg", "tier": "auto"},
            limit=1,
        )

    stale = "auto-0"
    assert client.post("/api/runs/load", json={"save_id": stale, "tier": "auto"}).status_code == 404
    assert client.get(f"/api/runs/auto/{stale}/download").status_code == 404
    assert client.post(f"/api/runs/auto/{stale}/promote").status_code == 404


def test_stale_auto_id_after_clear_returns_404(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    monkeypatch.setattr(data_paths, "CACHE_DIR", cache)
    monkeypatch.setattr(data_paths, "RUN_CACHE_DIR", cache / "runs")
    monkeypatch.setattr(data_paths, "LUT_CACHE_DIR", cache / "luts")
    monkeypatch.setattr(data_paths, "AUTO_RUNS_DIR", cache / "auto_runs")
    monkeypatch.setattr(data_paths, "SAVED_RUNS_DIR", tmp_path / "saved")
    for d in (data_paths.RUN_CACHE_DIR, data_paths.LUT_CACHE_DIR, data_paths.AUTO_RUNS_DIR, data_paths.SAVED_RUNS_DIR):
        d.mkdir(parents=True, exist_ok=True)
    auto_run_store.write_auto_run(
        "auto-1",
        b"PKZIP",
        {"save_id": "auto-1", "label": "Auto", "saved_at": "20260616-101500", "source_image_name": "steve.jpg", "tier": "auto"},
    )

    assert client.post("/api/cache/clear-all").status_code == 200
    assert client.post("/api/runs/load", json={"save_id": "auto-1", "tier": "auto"}).status_code == 404
    assert client.get("/api/runs/auto/auto-1/download").status_code == 404
    assert client.post("/api/runs/auto/auto-1/promote").status_code == 404
