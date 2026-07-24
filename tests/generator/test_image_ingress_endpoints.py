from __future__ import annotations

import base64
import io
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import data_paths
import run_archive
import server
from source_images import ImageImportManager, SourceImageService


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "heif"


def _heif_bytes() -> bytes:
    return base64.b64decode(
        (FIXTURES / "RGB_8__29x100.heif.b64").read_text(encoding="ascii")
    )


def _png_bytes(size=(6, 4), color="blue") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, format="PNG")
    return output.getvalue()


def _wait(client: TestClient, batch: dict) -> dict:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        status = client.get(f"/api/images/imports/{batch['batch_id']}").json()
        if status["status"] in {"complete", "partial", "failed"}:
            return status
        time.sleep(0.01)
    raise AssertionError("image import did not finish")


@pytest.fixture
def ingress(tmp_path: Path, monkeypatch):
    images = tmp_path / "Images"
    cache = tmp_path / "cache"
    staging = cache / "image-imports"
    images.mkdir()
    service = SourceImageService(images, cache / "source-images")
    manager = ImageImportManager(service, staging)
    monkeypatch.setattr(server, "_IMAGES_DIR", images)
    monkeypatch.setattr(server, "_SOURCE_IMAGES", service)
    monkeypatch.setattr(server, "_IMAGE_IMPORTS", manager)
    monkeypatch.setattr(data_paths, "CACHE_DIR", cache)
    monkeypatch.setattr(data_paths, "RUN_CACHE_DIR", cache / "runs")
    monkeypatch.setattr(data_paths, "LUT_CACHE_DIR", cache / "luts")
    monkeypatch.setattr(data_paths, "AUTO_RUNS_DIR", cache / "auto-runs")
    monkeypatch.setattr(data_paths, "SOURCE_IMAGE_IMPORT_DIR", staging)
    try:
        yield TestClient(server.app), images, cache
    finally:
        manager.stop()


def test_folder_refresh_prepares_heic_and_preserves_original(ingress) -> None:
    client, images, _cache = ingress
    original = _heif_bytes()
    (images / "phone.HEIC").write_bytes(original)

    before = client.get("/api/images")
    batch = client.post("/api/images/refresh")
    terminal = _wait(client, batch.json())
    after = client.get("/api/images")

    assert before.json() == []
    assert terminal["status"] == "complete"
    assert terminal["items"][0]["stored_name"] == "phone.HEIC"
    assert after.json()[0]["filename"] == "phone.HEIC"
    assert after.json()[0]["normalized"] is True
    assert (after.json()[0]["width"], after.json()[0]["height"]) == (29, 100)
    assert (images / "phone.HEIC").read_bytes() == original
    preview = client.get("/api/images/preview/phone.HEIC")
    assert preview.status_code == 200
    assert preview.headers["content-type"] == "image/jpeg"


def test_folder_refresh_reports_known_unsupported_images_but_ignores_live_photo_video(
    ingress,
) -> None:
    client, images, _cache = ingress
    (images / "animation.gif").write_bytes(b"GIF89a")
    (images / "IMG_0001.mov").write_bytes(b"video companion")
    (images / "notes.txt").write_text("not an image", encoding="utf-8")

    terminal = _wait(client, client.post("/api/images/refresh").json())

    assert terminal["status"] == "failed"
    assert [item["requested_name"] for item in terminal["items"]] == ["animation.gif"]
    assert "Unsupported image format" in terminal["items"][0]["error"]


def test_batch_upload_reports_partial_and_publishes_only_valid_files(ingress) -> None:
    client, images, _cache = ingress

    response = client.post(
        "/api/images/import",
        files=[
            ("files", ("good.png", _png_bytes(), "image/png")),
            ("files", ("bad.heic", b"broken", "image/heic")),
        ],
    )
    terminal = _wait(client, response.json())

    assert response.status_code == 202
    assert terminal["status"] == "partial"
    assert (terminal["succeeded"], terminal["failed"]) == (1, 1)
    assert (images / "good.png").is_file()
    assert not (images / "bad.heic").exists()
    assert [item["filename"] for item in client.get("/api/images").json()] == ["good.png"]


def test_legacy_upload_contract_accepts_jpeg_alias(ingress) -> None:
    client, images, _cache = ingress
    output = io.BytesIO()
    Image.new("RGB", (8, 5), "green").save(output, format="JPEG")

    response = client.post(
        "/api/images/upload",
        files={"file": ("phone.jpe", output.getvalue(), "image/jpeg")},
    )

    assert response.status_code == 200
    assert response.json() == {"filename": "phone.jpe", "width": 8, "height": 5}
    assert (images / "phone.jpe").is_file()


def test_batch_upload_rejects_unsupported_without_publishing_it(ingress) -> None:
    client, images, _cache = ingress

    response = client.post(
        "/api/images/import",
        files=[("files", ("animation.gif", b"GIF89a", "image/gif"))],
    )
    terminal = _wait(client, response.json())

    assert terminal["status"] == "failed"
    assert "Unsupported image format" in terminal["items"][0]["error"]
    assert not (images / "animation.gif").exists()


def test_clear_temp_refuses_while_import_is_active(tmp_path: Path, monkeypatch) -> None:
    images = tmp_path / "Images"
    cache = tmp_path / "cache"
    images.mkdir()
    (images / "phone.heic").write_bytes(_heif_bytes())
    service = SourceImageService(images, cache / "source-images")
    manager = ImageImportManager(service, cache / "imports", priority_busy=lambda: True)
    monkeypatch.setattr(server, "_SOURCE_IMAGES", service)
    monkeypatch.setattr(server, "_IMAGE_IMPORTS", manager)
    monkeypatch.setattr(data_paths, "CACHE_DIR", cache)
    try:
        manager.refresh()
        response = TestClient(server.app).post("/api/cache/clear-all")
    finally:
        manager.stop()

    assert response.status_code == 409
    assert "images are being prepared" in response.json()["detail"]


def test_clear_temp_removes_prepared_copy_but_preserves_original(ingress) -> None:
    client, images, cache = ingress
    original = _heif_bytes()
    (images / "phone.heic").write_bytes(original)
    terminal = _wait(client, client.post("/api/images/refresh").json())
    assert terminal["status"] == "complete"
    assert list((cache / "source-images").glob("*.png"))

    response = client.post("/api/cache/clear-all")

    assert response.status_code == 200
    assert (images / "phone.heic").read_bytes() == original
    assert not list((cache / "source-images").glob("*.png"))


def test_saved_run_embeds_exact_solve_snapshot_as_compatible_png(
    tmp_path: Path,
    monkeypatch,
) -> None:
    images = tmp_path / "Images"
    run_root = tmp_path / "runs"
    images.mkdir()
    run_dir = run_root / "run-heic"
    run_dir.mkdir(parents=True)
    (images / "phone.heic").write_bytes(b"newer bytes that must not enter the archive")
    snapshot = _png_bytes(size=(11, 7), color="purple")
    (run_dir / "source-image.png").write_bytes(snapshot)
    monkeypatch.setattr(server, "_IMAGES_DIR", images)
    monkeypatch.setattr(data_paths, "RUN_CACHE_DIR", run_root)
    solve = {
        "thickness_maps": {},
        "debug_maps": {},
        "export_maps": {},
        "result": {},
        "source_asset": {
            "original_source_name": "phone.heic",
            "original_source_format": "HEIF",
            "normalized_source_name": "digest-v1.png",
            "source_digest": "abc123",
            "normalization_version": 1,
            "normalized": True,
            "snapshot_name": "source-image.png",
        },
    }
    cfg = {"image_path": "phone.heic", "palette": []}

    pieces = server._build_archive_inputs(
        "run-heic",
        solve,
        cfg,
        label="HEIC run",
        saved_at="20260724-120000",
    )
    run_json, arrays, image_bytes, image_name, solve_state, run_cache = pieces
    packed = run_archive.pack_run_archive(
        run_json=run_json,
        thickness_arrays=arrays,
        image_bytes=image_bytes,
        image_name=image_name,
        solve_state=solve_state,
        run_cache_files=run_cache,
    )
    parsed = run_archive.read_run_archive(packed)

    assert parsed.image_name == "phone.prisma-source.png"
    assert parsed.image_bytes == snapshot
    assert run_json["source_image_name"] == "phone.heic"
    assert run_json["source_asset"]["source_digest"] == "abc123"
    assert "source-image.png" not in parsed.run_cache_files
    with Image.open(io.BytesIO(parsed.image_bytes)) as image:
        assert image.size == (11, 7)
