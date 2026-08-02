from __future__ import annotations

import base64
import os
import time
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
import Prisma.generator.source_images as source_images_module

from Prisma.generator.source_images import (
    ImageImportManager,
    NATIVE_EXTENSIONS,
    NORMALIZED_EXTENSIONS,
    SUPPORTED_EXTENSIONS,
    SourceImageError,
    SourceImageService,
    normalize_to_srgb,
)


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "heif"
HDR_GAIN_MAP_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "hdr_gain_map"
    / "apple_gain_map_mpo.jpg.b64.txt"
)

_APPLE_GAIN_MAP_XMP = b"""<x:xmpmeta xmlns:x="adobe:ns:meta/">
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
<rdf:Description rdf:about="" xmlns:HDRGainMap="http://ns.apple.com/HDRGainMap/1.0/"
 xmlns:apdi="http://ns.apple.com/pixeldatainfo/1.0/">
<HDRGainMap:HDRGainMapVersion>131072</HDRGainMap:HDRGainMapVersion>
<apdi:AuxiliaryImageType>urn:com:apple:photo:2020:aux:hdrgainmap</apdi:AuxiliaryImageType>
</rdf:Description></rdf:RDF></x:xmpmeta>"""

_ANDROID_PRIMARY_XMP = b"""<x:xmpmeta xmlns:x="adobe:ns:meta/">
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
<rdf:Description rdf:about="" xmlns:hdrgm="http://ns.adobe.com/hdr-gain-map/1.0/"
 hdrgm:Version="1.0" />
</rdf:RDF></x:xmpmeta>"""


def _write_mpo_jpeg(
    path: Path,
    *,
    primary_xmp: bytes | None = None,
    auxiliary_xmp: bytes | None = None,
    auxiliary_mode: str = "L",
) -> None:
    primary = Image.new("RGB", (8, 6), (210, 30, 20))
    auxiliary = Image.new(auxiliary_mode, (4, 3), 128 if auxiliary_mode == "L" else "blue")
    if primary_xmp is not None:
        primary.encoderinfo = {"xmp": primary_xmp}
    if auxiliary_xmp is not None:
        auxiliary.encoderinfo = {"xmp": auxiliary_xmp}
    primary.save(path, format="MPO", save_all=True, append_images=[auxiliary])


def _write_heif(path: Path) -> None:
    encoded = (FIXTURES / "RGB_8__29x100.heif.b64").read_text(encoding="ascii")
    path.write_bytes(base64.b64decode(encoded))


def _wait_for_terminal(manager: ImageImportManager, batch_id: str) -> dict:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        status = manager.status(batch_id)
        if status["status"] in {"complete", "partial", "failed"}:
            return status
        time.sleep(0.01)
    raise AssertionError("image import did not finish")


def test_supported_extensions_cover_phone_and_jpeg_aliases() -> None:
    assert {".heic", ".heif", ".hif", ".avif"} <= NORMALIZED_EXTENSIONS
    assert {".jfif", ".jpe"} <= NATIVE_EXTENSIONS
    assert SUPPORTED_EXTENSIONS == NATIVE_EXTENSIONS | NORMALIZED_EXTENSIONS
    assert not ({".gif", ".dng", ".mov"} & SUPPORTED_EXTENSIONS)


def test_real_heif_fixture_is_normalized_and_reused(tmp_path: Path) -> None:
    images = tmp_path / "Images"
    images.mkdir()
    source = images / "phone.heic"
    _write_heif(source)
    service = SourceImageService(images, tmp_path / "cache")

    resolved = service.prepare("phone.heic")
    reused = service.prepare("phone.heic")

    assert resolved.normalized is True
    assert resolved.source_format == "HEIF"
    assert (resolved.width, resolved.height) == (29, 100)
    assert resolved.working_path.suffix == ".png"
    assert resolved.working_path == reused.working_path
    assert source.read_bytes() == base64.b64decode(
        (FIXTURES / "RGB_8__29x100.heif.b64").read_text(encoding="ascii")
    )
    with Image.open(resolved.working_path) as prepared:
        assert prepared.mode == "RGB"
        assert prepared.size == (29, 100)


def test_avif_change_invalidates_cache_without_changing_original(tmp_path: Path) -> None:
    images = tmp_path / "Images"
    images.mkdir()
    source = images / "download.avif"
    Image.new("RGB", (12, 8), (240, 20, 10)).save(source, format="AVIF", quality=100)
    original = source.read_bytes()
    service = SourceImageService(images, tmp_path / "cache")

    first = service.prepare(source.name)
    assert source.read_bytes() == original

    Image.new("RGB", (9, 7), (20, 220, 40)).save(source, format="AVIF", quality=100)
    stat = source.stat()
    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    second = service.prepare(source.name)

    assert second.working_path != first.working_path
    assert (second.width, second.height) == (9, 7)
    assert not first.working_path.exists()


def test_native_jpeg_alias_preserves_direct_working_path(tmp_path: Path) -> None:
    images = tmp_path / "Images"
    images.mkdir()
    source = images / "camera.jfif"
    Image.new("RGB", (7, 5), (10, 20, 30)).save(source, format="JPEG")
    service = SourceImageService(images, tmp_path / "cache")

    resolved = service.prepare(source.name)

    assert resolved.normalized is False
    assert resolved.working_path == source
    assert (resolved.width, resolved.height) == (7, 5)


def test_apple_hdr_gain_map_jpeg_is_normalized_from_primary_frame(tmp_path: Path) -> None:
    images = tmp_path / "Images"
    images.mkdir()
    source = images / "phone.jpg"
    source.write_bytes(base64.b64decode(HDR_GAIN_MAP_FIXTURE.read_text(encoding="ascii")))
    original = source.read_bytes()
    with Image.open(source) as image:
        assert image.format == "MPO"
        assert image.n_frames == 2
    service = SourceImageService(images, tmp_path / "cache")

    ready, pending = service.discover()
    resolved = service.prepare(source.name)

    assert ready == []
    assert pending == [source]
    assert resolved.normalized is True
    assert resolved.source_format == "JPEG"
    assert resolved.source_variant == "hdr_gain_map"
    assert resolved.working_path.suffix == ".png"
    assert resolved.working_path != source
    assert source.read_bytes() == original
    with Image.open(resolved.working_path) as prepared:
        prepared.load()
        assert prepared.format == "PNG"
        assert prepared.mode == "RGB"
        assert prepared.size == (8, 6)
        assert prepared.getpixel((0, 0))[0] > prepared.getpixel((0, 0))[2]
    assert service.discover()[0] == [source]
    assert resolved.provenance()["source_variant"] == "hdr_gain_map"


def test_android_ultra_hdr_jpeg_is_normalized_even_when_pillow_calls_it_jpeg(
    tmp_path: Path,
) -> None:
    images = tmp_path / "Images"
    images.mkdir()
    source = images / "android.jpg"
    _write_mpo_jpeg(source, primary_xmp=_ANDROID_PRIMARY_XMP)
    with Image.open(source) as image:
        assert image.format == "JPEG"
        assert getattr(image, "n_frames", 1) == 1
    service = SourceImageService(images, tmp_path / "cache")

    resolved = service.prepare(source.name)

    assert resolved.normalized is True
    assert resolved.source_format == "JPEG"
    assert resolved.source_variant == "hdr_gain_map"
    assert resolved.working_path.suffix == ".png"


def test_general_mpo_renamed_jpeg_is_rejected_as_multi_picture(tmp_path: Path) -> None:
    images = tmp_path / "Images"
    images.mkdir()
    source = images / "stereo.jpg"
    _write_mpo_jpeg(source, auxiliary_mode="RGB")
    service = SourceImageService(images, tmp_path / "cache")

    with pytest.raises(SourceImageError, match="multiple pictures"):
        service.prepare(source.name)


def test_incidental_hdr_gain_map_text_does_not_authorize_mpo(tmp_path: Path) -> None:
    images = tmp_path / "Images"
    images.mkdir()
    source = images / "untrusted.jpg"
    incidental_xmp = b"""<x:xmpmeta xmlns:x="adobe:ns:meta/">
    <note>urn:com:apple:photo:2020:aux:hdrgainmap</note>
    </x:xmpmeta>"""
    _write_mpo_jpeg(source, auxiliary_xmp=incidental_xmp, auxiliary_mode="RGB")
    service = SourceImageService(images, tmp_path / "cache")

    with pytest.raises(SourceImageError, match="cannot safely identify"):
        service.prepare(source.name)


def test_jpeg_switches_between_native_and_gain_map_cache_entries(tmp_path: Path) -> None:
    images = tmp_path / "Images"
    images.mkdir()
    source = images / "changing.jpg"
    cache = tmp_path / "cache"
    Image.new("RGB", (7, 5), "green").save(source, format="JPEG")
    service = SourceImageService(images, cache)

    native = service.prepare(source.name)
    assert native.normalized is False
    assert source.name in service._manifest["native_entries"]

    time.sleep(0.001)
    _write_mpo_jpeg(source, auxiliary_xmp=_APPLE_GAIN_MAP_XMP)
    gain_map = service.prepare(source.name)
    assert gain_map.normalized is True
    assert source.name in service._manifest["entries"]
    assert source.name not in service._manifest["native_entries"]

    time.sleep(0.001)
    Image.new("RGB", (9, 4), "blue").save(source, format="JPEG")
    restored_native = service.prepare(source.name)
    assert restored_native.normalized is False
    assert source.name not in service._manifest["entries"]
    assert source.name in service._manifest["native_entries"]
    assert not list(cache.glob("*.png"))


@pytest.mark.parametrize(
    ("format_name", "filename", "error"),
    [
        ("PNG", "animated.png", "Animated PNG"),
        ("WEBP", "animated.webp", "Animated WebP"),
        ("TIFF", "pages.tiff", "Multi-page TIFF"),
    ],
)
def test_native_multi_frame_sources_are_rejected(
    tmp_path: Path,
    format_name: str,
    filename: str,
    error: str,
) -> None:
    images = tmp_path / "Images"
    images.mkdir()
    source = images / filename
    Image.new("RGB", (8, 6), "red").save(
        source,
        format=format_name,
        save_all=True,
        append_images=[Image.new("RGB", (8, 6), "blue")],
    )
    service = SourceImageService(images, tmp_path / "cache")

    with pytest.raises(SourceImageError, match=error):
        service.prepare(source.name)


def test_native_digest_is_indexed_and_reused_without_rehashing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    images = tmp_path / "Images"
    images.mkdir()
    source = images / "portrait.jpg"
    Image.new("RGB", (9, 6), (10, 20, 30)).save(source, format="JPEG")
    service = SourceImageService(images, tmp_path / "cache")
    real_hash = source_images_module._sha256_file
    calls = []

    def count_hash(path: Path) -> str:
        calls.append(Path(path))
        return real_hash(path)

    monkeypatch.setattr(source_images_module, "_sha256_file", count_hash)
    first = service.prepare(source.name)
    second = service.prepare(source.name)
    matches = service.paths_with_digest(first.fingerprint)

    assert first.fingerprint == second.fingerprint
    assert calls == [source]
    assert matches == [source]
    reloaded = SourceImageService(images, tmp_path / "cache")
    assert reloaded.prepare(source.name).fingerprint == first.fingerprint
    assert calls == [source]


def test_nclx_display_p3_converts_and_hdr_only_is_rejected() -> None:
    p3 = Image.new("RGB", (1, 1), (0, 255, 0))
    p3.info["nclx_profile"] = {
        "color_primaries": 12,
        "transfer_characteristics": 13,
        "matrix_coefficients": 0,
        "full_range_flag": True,
    }
    converted = normalize_to_srgb(p3)
    assert converted.mode == "RGB"
    assert np.asarray(converted)[0, 0].tolist() == [0, 255, 0]

    hdr = Image.new("RGB", (1, 1), (128, 128, 128))
    hdr.info["nclx_profile"] = {
        "color_primaries": 9,
        "transfer_characteristics": 16,
        "matrix_coefficients": 9,
        "full_range_flag": False,
    }
    with pytest.raises(SourceImageError, match="HDR-only"):
        normalize_to_srgb(hdr)


def test_corrupt_convertible_is_not_listed_ready(tmp_path: Path) -> None:
    images = tmp_path / "Images"
    images.mkdir()
    corrupt = images / "broken.heic"
    corrupt.write_bytes(b"not a heif image")
    service = SourceImageService(images, tmp_path / "cache")

    ready, pending = service.discover()

    assert ready == []
    assert pending == [corrupt]
    with pytest.raises(SourceImageError, match="Cannot decode"):
        service.prepare(corrupt.name)


def test_misleading_extension_is_rejected_by_signature(tmp_path: Path) -> None:
    images = tmp_path / "Images"
    images.mkdir()
    source = images / "disguised.png"
    Image.new("RGB", (3, 2), "red").save(source, format="JPEG")
    service = SourceImageService(images, tmp_path / "cache")

    with pytest.raises(SourceImageError, match="do not match"):
        service.prepare(source.name)

    ready, pending = service.discover()
    assert ready == []
    assert pending == [source]


def test_corrupt_prepared_png_is_rebuilt(tmp_path: Path) -> None:
    images = tmp_path / "Images"
    images.mkdir()
    source = images / "phone.avif"
    Image.new("RGB", (8, 6), "green").save(source, format="AVIF")
    service = SourceImageService(images, tmp_path / "cache")
    first = service.prepare(source.name)
    first.working_path.write_bytes(b"corrupt")

    rebuilt = service.prepare(source.name)

    assert rebuilt.working_path == first.working_path
    with Image.open(rebuilt.working_path) as image:
        image.verify()


def test_lru_eviction_is_persisted_in_manifest(tmp_path: Path) -> None:
    images = tmp_path / "Images"
    images.mkdir()
    Image.new("RGB", (8, 6), "red").save(images / "first.avif", format="AVIF")
    Image.new("RGB", (8, 6), "blue").save(images / "second.avif", format="AVIF")
    cache = tmp_path / "cache"
    service = SourceImageService(images, cache, cache_limit_bytes=1)
    first = service.prepare("first.avif")
    second = service.prepare("second.avif")

    assert not first.working_path.exists()
    assert second.working_path.exists()
    reloaded = SourceImageService(images, cache, cache_limit_bytes=1)
    with pytest.raises(SourceImageError, match="still being prepared"):
        reloaded.resolve("first.avif", prepare=False)
    assert reloaded.resolve("second.avif", prepare=False).working_path == second.working_path


def test_source_mutation_during_conversion_is_not_published(
    tmp_path: Path,
    monkeypatch,
) -> None:
    images = tmp_path / "Images"
    images.mkdir()
    source = images / "changing.avif"
    Image.new("RGB", (8, 6), "purple").save(source, format="AVIF")
    service = SourceImageService(images, tmp_path / "cache")
    original_decode = service._decode_normalized

    def mutate_after_decode(path: Path, **kwargs):
        decoded = original_decode(path, **kwargs)
        path.write_bytes(path.read_bytes() + b"changed")
        return decoded

    monkeypatch.setattr(service, "_decode_normalized", mutate_after_decode)
    with pytest.raises(SourceImageError, match="changed while"):
        service.prepare(source.name)
    assert not list((tmp_path / "cache").glob("*.png"))


def test_import_manager_reports_partial_batches_and_collision_names(tmp_path: Path) -> None:
    images = tmp_path / "Images"
    images.mkdir()
    Image.new("RGB", (2, 2), "white").save(images / "photo.png")
    staging = tmp_path / "staging"
    staging.mkdir()
    valid = staging / ".upload-valid.png"
    invalid = staging / ".upload-invalid.heic"
    Image.new("RGB", (4, 3), "red").save(valid)
    invalid.write_bytes(b"broken")
    service = SourceImageService(images, tmp_path / "cache")
    manager = ImageImportManager(service, staging)
    try:
        batch = manager.import_staged([("photo.png", valid), ("bad.heic", invalid)])
        result = _wait_for_terminal(manager, batch["batch_id"])
    finally:
        manager.stop()

    assert result["status"] == "partial"
    assert (result["succeeded"], result["failed"]) == (1, 1)
    assert result["items"][0]["stored_name"] == "photo_1.png"
    assert (images / "photo_1.png").is_file()
    assert not (images / "bad.heic").exists()
    assert "staging_path" not in result["items"][0]


def test_upload_original_is_not_published_until_preparation_succeeds(
    tmp_path: Path,
) -> None:
    images = tmp_path / "Images"
    images.mkdir()
    staging = tmp_path / "staging"
    staging.mkdir()
    invalid = staging / ".upload-invalid.heic"
    invalid.write_bytes(b"broken")
    service = SourceImageService(images, tmp_path / "cache")
    manager = ImageImportManager(service, staging)
    try:
        batch = manager.import_staged([("bad.heic", invalid)])
        result = _wait_for_terminal(manager, batch["batch_id"])
    finally:
        manager.stop()

    assert result["status"] == "failed"
    assert not (images / "bad.heic").exists()
    assert not invalid.exists()


def test_changed_gain_map_upload_is_not_published_or_cached(
    tmp_path: Path,
    monkeypatch,
) -> None:
    images = tmp_path / "Images"
    images.mkdir()
    staging = tmp_path / "staging"
    staging.mkdir()
    staged = staging / ".upload-phone.jpg"
    destination = images / "phone.jpg"
    _write_mpo_jpeg(staged, auxiliary_xmp=_APPLE_GAIN_MAP_XMP)
    service = SourceImageService(images, tmp_path / "cache")
    original_decode = service._decode_gain_map_primary

    def mutate_after_decode(path: Path):
        decoded = original_decode(path)
        path.write_bytes(path.read_bytes() + b"changed")
        return decoded

    monkeypatch.setattr(service, "_decode_gain_map_primary", mutate_after_decode)

    with pytest.raises(SourceImageError, match="changed while"):
        service.publish_staged(staged, destination)

    assert not destination.exists()
    assert not list((tmp_path / "cache").glob("*.png"))


def test_import_manager_coalesces_active_scans(tmp_path: Path) -> None:
    images = tmp_path / "Images"
    images.mkdir()
    _write_heif(images / "phone.heic")
    service = SourceImageService(images, tmp_path / "cache")
    manager = ImageImportManager(service, tmp_path / "staging", priority_busy=lambda: True)
    try:
        first = manager.refresh()
        second = manager.refresh()
        assert second["batch_id"] == first["batch_id"]
    finally:
        manager.stop()


def test_clear_removes_only_disposable_cache(tmp_path: Path) -> None:
    images = tmp_path / "Images"
    images.mkdir()
    _write_heif(images / "phone.heic")
    service = SourceImageService(images, tmp_path / "cache")
    resolved = service.prepare("phone.heic")

    removed = service.clear()

    assert removed >= 2
    assert (images / "phone.heic").is_file()
    assert not resolved.working_path.exists()
    assert service.discover()[1] == [images / "phone.heic"]


def test_service_rejects_paths_outside_images_folder(tmp_path: Path) -> None:
    images = tmp_path / "Images"
    images.mkdir()
    outside = tmp_path / "outside.png"
    Image.new("RGB", (1, 1)).save(outside)
    service = SourceImageService(images, tmp_path / "cache")

    with pytest.raises(SourceImageError, match="outside"):
        service.prepare(outside)
