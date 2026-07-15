from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

import maintenance_reextract
import server
import processing.extraction_result as extraction_result_module
import processing.extraction_publication as extraction_publication_module
from processing.artifact_sinks import LiveThumbnailSink, SampleArtifactDirectorySink
from processing import manual as manual_module
from processing import processor
from models import EvidenceBinding, MethodProvenance, ProcessingConfidence, ProcessingResult, SwatchMeasurement
from sqlite_data_access import SQLiteDataStore
from tests.calibration.test_sqlite_stage4_extraction_writes import _result, _store


def _wait_reextract_job(client: TestClient, job_id: str, *, timeout: float = 3.0) -> dict:
    deadline = time.time() + timeout
    payload: dict = {}
    while time.time() < deadline:
        response = client.get(f"/api/maintenance/reextract-sample-images/jobs/{job_id}")
        assert response.status_code == 200, response.text
        payload = response.json()
        if payload.get("status") in {"succeeded", "failed", "cancelled"}:
            return payload
        time.sleep(0.025)
    raise AssertionError(f"Timed out waiting for re-extraction job {job_id}: {payload}")


def _scope() -> dict:
    return {
        "domain_mode": "appearance_only",
        "segmentation_mode": "existing_coordinates",
        "sample_scope": {"kind": "all_accepted"},
    }


def test_live_thumbnail_sink_accepts_only_durable_sample_visuals(tmp_path: Path) -> None:
    sink = LiveThumbnailSink(tmp_path / "thumbnails")
    image = np.full((4, 4, 3), 128, dtype=np.uint8)

    sink.write_image("exp-001", "source", image)
    sink.write_image("exp-001", "strip", image)

    for kind in ("blank", "appearance", "transmission_roi"):
        with pytest.raises(ValueError, match="review-only"):
            sink.write_image("exp-001", kind, image)
        assert not (tmp_path / "thumbnails" / "exp-001" / f"{kind}.jpg").exists()


def _scope_for(domain_mode: str, segmentation_mode: str = "existing_coordinates") -> dict:
    return {
        "domain_mode": domain_mode,
        "segmentation_mode": segmentation_mode,
        "sample_scope": {"kind": "all_accepted"},
    }


def _scope_for_samples(
    sample_ids: list[str],
    domain_mode: str = "appearance_only",
    segmentation_mode: str = "existing_coordinates",
) -> dict:
    return {
        "domain_mode": domain_mode,
        "segmentation_mode": segmentation_mode,
        "sample_scope": {"kind": "sample_ids", "sample_ids": sample_ids},
    }


def _mark_candidate_save(store: SQLiteDataStore, candidate_set_id: str, sample_id: str = "exp-001") -> dict:
    return maintenance_reextract.update_candidate_review(
        store,
        candidate_set_id,
        sample_id,
        decision="save",
    )


def test_reextract_candidate_storage_is_under_managed_data_and_validates_ids(tmp_path: Path) -> None:
    store = _store(tmp_path, materialize_assets=True)

    manifest, set_path = maintenance_reextract.create_candidate_set(
        store,
        _scope(),
        plan_digest="digest-001",
        job_id="job-001",
    )

    expected_root = store.root / "maintenance" / "reextract_sample_images"
    assert set_path.parent == expected_root
    assert set_path.is_relative_to(store.root)
    assert manifest.candidate_set_id.startswith("rext_")
    assert (set_path / "manifest.json").exists()
    assert maintenance_reextract.load_manifest(store, manifest.candidate_set_id).job_id == "job-001"
    assert [row["candidate_set_id"] for row in maintenance_reextract.list_candidate_sets(store)] == [
        manifest.candidate_set_id
    ]

    with pytest.raises(ValueError, match="Invalid candidate set ID"):
        maintenance_reextract.candidate_set_path(store, "../escape")


def test_new_reextract_candidate_set_supersedes_previous_set(tmp_path: Path) -> None:
    store = _store(tmp_path, materialize_assets=True)

    first, _first_path = maintenance_reextract.create_candidate_set(
        store,
        _scope(),
        plan_digest="digest-001",
    )
    time.sleep(0.01)
    second, _second_path = maintenance_reextract.create_candidate_set(
        store,
        _scope(),
        plan_digest="digest-002",
    )

    assert [row["candidate_set_id"] for row in maintenance_reextract.list_candidate_sets(store)] == [
        second.candidate_set_id
    ]
    assert not maintenance_reextract.candidate_set_path(store, first.candidate_set_id).exists()


def test_reextract_candidate_prune_expires_abandoned_newest_set(tmp_path: Path) -> None:
    store = _store(tmp_path)
    manifest, set_path = maintenance_reextract.create_candidate_set(
        store,
        _scope_for_samples(["exp-001"]),
        plan_digest="expired",
    )
    payload = json.loads((set_path / "manifest.json").read_text(encoding="utf-8"))
    payload["created_at"] = "2020-01-01T00:00:00+00:00"
    payload["updated_at"] = "2020-01-01T00:00:00+00:00"
    (set_path / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")

    result = maintenance_reextract.prune_candidate_sets(
        store,
        now=datetime(2026, 7, 12, tzinfo=timezone.utc).timestamp(),
    )

    assert result["deleted"] == [manifest.candidate_set_id]
    assert not set_path.exists()


def test_reextract_candidate_prune_removes_old_partial_valid_set(tmp_path: Path) -> None:
    store = _store(tmp_path)
    partial = maintenance_reextract.reextract_root(store) / ("rext_" + "a" * 32)
    partial.mkdir(parents=True)
    old = datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp()
    os.utime(partial, (old, old))

    result = maintenance_reextract.prune_candidate_sets(
        store,
        now=datetime(2026, 7, 12, tzinfo=timezone.utc).timestamp(),
    )

    assert result["deleted"] == [partial.name]
    assert not partial.exists()


def test_candidate_artifact_sink_writes_only_to_candidate_directory(tmp_path: Path) -> None:
    store = _store(tmp_path, materialize_assets=True)
    manifest, set_path = maintenance_reextract.create_candidate_set(
        store,
        _scope(),
        plan_digest="digest-001",
    )
    sample_dir = set_path / "candidates" / "exp-001"
    sink = SampleArtifactDirectorySink(sample_dir)

    path = sink.write_image("exp-001", "appearance", np.full((12, 24, 3), 128, dtype=np.uint8))

    assert path == sample_dir / "appearance_review.jpg"
    assert path.exists()
    assert not (store.root / "thumbnails" / "exp-001" / "appearance.jpg").exists()
    assert maintenance_reextract.load_manifest(store, manifest.candidate_set_id).candidate_set_id == manifest.candidate_set_id

    with pytest.raises(ValueError, match="invalid extraction artifact kind"):
        sink.write_image("exp-001", "../escape", np.full((4, 4, 3), 255, dtype=np.uint8))


def test_appearance_thumbnail_writer_can_target_candidate_sink_without_live_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path, materialize_assets=True)
    manifest, set_path = maintenance_reextract.create_candidate_set(
        store,
        _scope(),
        plan_digest="digest-001",
    )
    sample_dir = set_path / "candidates" / "exp-001"
    sink = SampleArtifactDirectorySink(sample_dir)
    monkeypatch.setattr(
        processor,
        "build_appearance_strip_visual",
        lambda **_kwargs: np.full((10, 20, 3), 200, dtype=np.uint8),
    )

    wrote = processor._save_appearance_strip_thumbnail(  # type: ignore[attr-defined]
        sample_id="exp-001",
        thumb_dir=store.root / "thumbnails" / "exp-001",
        cr2_path=store.root / "images" / "imported" / "img-sample" / "sample.CR2",
        swatches=[
            SwatchMeasurement(
                swatch_index=0,
                nominal_thickness_mm=0.2,
                hex="#000000",
                R=0,
                G=0,
                B=0,
                R_linear=0.1,
                G_linear=0.2,
                B_linear=0.3,
            )
        ],
        method_provenance=MethodProvenance(),
        evidence_binding=EvidenceBinding(sample_image_asset_id="img-sample"),
        artifact_sink=sink,
    )

    assert wrote is True
    assert (sample_dir / "appearance_review.jpg").exists()
    assert not (store.root / "thumbnails" / "exp-001" / "appearance.jpg").exists()
    assert maintenance_reextract.load_manifest(store, manifest.candidate_set_id).candidate_set_id == manifest.candidate_set_id


def test_manual_preview_payload_writes_appearance_to_candidate_sink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path, materialize_assets=True)
    sample = store.get_sample("exp-001")
    raw_path = tmp_path / "sample.CR2"
    raw_path.write_bytes(b"raw")
    sample_dir = tmp_path / "candidate" / "exp-001"
    sink = SampleArtifactDirectorySink(sample_dir)
    calls: list[dict] = []

    def fake_build_extraction_result(**kwargs):
        assert kwargs["appearance_strip_sample_boxes"] == {0: (1, 2, 3, 4)}
        assert kwargs["appearance_strip_sample_shape_hw"] == (10, 20)
        return _result(result_id="ext-manual-preview").model_copy(update={"method": "manual"})

    def fake_save_appearance_strip_thumbnail(**kwargs):
        calls.append(kwargs)
        kwargs["artifact_sink"].write_image(
            kwargs["sample_id"],
            "appearance",
            np.full((10, 20, 3), 180, dtype=np.uint8),
        )
        return True

    monkeypatch.setattr(manual_module, "build_extraction_result", fake_build_extraction_result)
    monkeypatch.setattr(manual_module, "_save_appearance_strip_thumbnail", fake_save_appearance_strip_thumbnail)

    result = manual_module._commit_manual(  # type: ignore[attr-defined]
        sample=sample,
        measurements=sample.measurements,
        confidence=ProcessingConfidence(detection_strategy="manual", contour_found=True),
        raw_path=raw_path,
        raw_corners=[{"x": 0, "y": 0}, {"x": 9, "y": 0}, {"x": 9, "y": 9}, {"x": 0, "y": 9}],
        preview_scale=1.0,
        preview_width=10,
        preview_height=10,
        store=store,
        commit=False,
        artifact_sink=sink,
        build_extraction_payload=True,
        appearance_strip_sample_boxes={0: (1, 2, 3, 4)},
        appearance_strip_sample_shape_hw=(10, 20),
    )

    assert result.status == "success"
    assert result.extraction_result_payload is not None
    assert calls
    assert calls[0]["artifact_sink"] is sink
    assert (sample_dir / "appearance_review.jpg").exists()
    assert not (store.root / "thumbnails" / "exp-001" / "appearance.jpg").exists()


def test_manual_extraction_does_not_use_brightness_flip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path, materialize_assets=True)
    sample = store.get_sample("exp-001")
    assert sample is not None
    raw_path = store.get_image_path(sample.assigned_image)
    blank_path = store.get_blank_storage_path(sample.assigned_blank_id)
    assert raw_path is not None
    assert blank_path is not None
    cfg = manual_module._build_swatch_config(sample)
    strip_w = cfg.num_swatches * 12

    bgr = np.full((48, 96, 3), 120, dtype=np.uint8)
    # Make the right side brighter so the removed heuristic would have flipped.
    bgr[:, strip_w:] = 230
    linear = np.full((48, 96, 3), 0.5, dtype=np.float32)
    flatfield = np.ones((48, 96, 3), dtype=np.float32)

    def forbidden_flip(_strip):
        raise AssertionError("manual extraction must not use brightness-based strip flipping")

    monkeypatch.setattr(manual_module, "_detect_strip_needs_flip", forbidden_flip)
    monkeypatch.setattr(manual_module, "load_raw_both", lambda _path: (bgr.copy(), linear.copy()))
    monkeypatch.setattr(manual_module, "load_preview_jpeg", lambda *_args, **_kwargs: bgr.copy())
    monkeypatch.setattr(manual_module, "match_flatfield_orientation", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(manual_module, "register_flatfield_strict", lambda *_args, **_kwargs: flatfield.copy())
    monkeypatch.setattr(manual_module, "detect_swatch_extent", lambda *_args, **_kwargs: (0, 0, strip_w, 12))
    monkeypatch.setattr(
        manual_module,
        "find_swatch_boundaries",
        lambda *_args, **_kwargs: [idx * 12 for idx in range(cfg.num_swatches + 1)],
    )

    result = manual_module.extract_strip_manual(
        sample=sample,
        raw_path=raw_path,
        blank_path=blank_path,
        corners=[{"x": 0, "y": 0}, {"x": strip_w, "y": 0}, {"x": strip_w, "y": 24}, {"x": 0, "y": 24}],
        orientation=sample.orientation_rots,
        preview_scale=1.0,
        store=store,
        commit=False,
        preview_width=96,
        preview_height=48,
        artifact_sink=SampleArtifactDirectorySink(tmp_path / "manual-artifacts" / sample.sample_id),
    )

    assert result.status == "success"
    assert result.measurements is not None
    assert len(result.measurements.swatches) == cfg.num_swatches


def test_committed_sqlite_manual_extraction_keeps_live_visuals_old_until_semantics_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path, materialize_assets=True)
    sample = store.get_sample("exp-001")
    assert sample is not None
    raw_path = store.get_image_path(sample.assigned_image)
    blank_path = store.get_blank_storage_path(sample.assigned_blank_id)
    assert raw_path is not None
    assert blank_path is not None
    prior_result_id = store.get_extraction_result(sample.sample_id)["extraction_result_id"]
    live_dir = store.root / "thumbnails" / sample.sample_id
    live_dir.mkdir(parents=True, exist_ok=True)
    old_source = b"old-live-source"
    old_strip = b"old-live-strip"
    (live_dir / "source.jpg").write_bytes(old_source)
    (live_dir / "strip.jpg").write_bytes(old_strip)

    cfg = manual_module._build_swatch_config(sample)
    strip_w = cfg.num_swatches * 12
    bgr = np.full((48, 96, 3), 120, dtype=np.uint8)
    linear = np.full((48, 96, 3), 0.5, dtype=np.float32)
    flatfield = np.ones((48, 96, 3), dtype=np.float32)
    monkeypatch.setattr(manual_module, "load_raw_both", lambda _path: (bgr.copy(), linear.copy()))
    monkeypatch.setattr(manual_module, "load_preview_jpeg", lambda *_args, **_kwargs: bgr.copy())
    monkeypatch.setattr(manual_module, "match_flatfield_orientation", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(manual_module, "register_flatfield_strict", lambda *_args, **_kwargs: flatfield.copy())
    monkeypatch.setattr(manual_module, "detect_swatch_extent", lambda *_args, **_kwargs: (0, 0, strip_w, 12))
    monkeypatch.setattr(
        manual_module,
        "find_swatch_boundaries",
        lambda *_args, **_kwargs: [idx * 12 for idx in range(cfg.num_swatches + 1)],
    )

    real_publish = manual_module.publish_extraction_update

    def checked_publish(*args, **kwargs):
        assert (live_dir / "source.jpg").read_bytes() == old_source
        assert (live_dir / "strip.jpg").read_bytes() == old_strip
        semantic_commit = kwargs["semantic_commit"]

        def checked_semantic_commit():
            assert (live_dir / "source.jpg").read_bytes() == old_source
            assert (live_dir / "strip.jpg").read_bytes() == old_strip
            committed = semantic_commit()
            assert (live_dir / "source.jpg").read_bytes() == old_source
            assert (live_dir / "strip.jpg").read_bytes() == old_strip
            return committed

        kwargs["semantic_commit"] = checked_semantic_commit
        return real_publish(*args, **kwargs)

    monkeypatch.setattr(manual_module, "publish_extraction_update", checked_publish)

    result = manual_module.extract_strip_manual(
        sample=sample,
        raw_path=raw_path,
        blank_path=blank_path,
        corners=[{"x": 0, "y": 0}, {"x": strip_w, "y": 0}, {"x": strip_w, "y": 24}, {"x": 0, "y": 24}],
        orientation=sample.orientation_rots,
        preview_scale=1.0,
        store=store,
        commit=True,
        preview_width=96,
        preview_height=48,
    )

    assert result.status == "success"
    assert store.get_extraction_result(sample.sample_id)["extraction_result_id"] != prior_result_id
    assert (live_dir / "source.jpg").read_bytes() != old_source
    assert (live_dir / "strip.jpg").read_bytes() != old_strip
    publication_root = store.root / "_system" / "extraction_publications"
    visual_stage_root = store.root / "_system" / "extraction_visual_stages"
    assert not publication_root.exists() or not any(publication_root.iterdir())
    assert not visual_stage_root.exists() or not any(visual_stage_root.iterdir())


def test_committed_sqlite_automatic_extraction_keeps_live_visuals_old_until_semantics_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path, materialize_assets=True)
    sample = store.get_sample("exp-001")
    assert sample is not None
    image_path = store.get_image_path(sample.assigned_image)
    blank_path = store.get_blank_storage_path(sample.assigned_blank_id)
    assert image_path is not None
    assert blank_path is not None
    prior_result_id = store.get_extraction_result(sample.sample_id)["extraction_result_id"]
    live_dir = store.root / "thumbnails" / sample.sample_id
    live_dir.mkdir(parents=True, exist_ok=True)
    old_source = b"old-auto-source"
    old_strip = b"old-auto-strip"
    (live_dir / "source.jpg").write_bytes(old_source)
    (live_dir / "strip.jpg").write_bytes(old_strip)

    cfg = processor._build_swatch_config(sample)
    strip_w = cfg.num_swatches * 12
    preview = np.full((48, 96, 3), 120, dtype=np.uint8)
    full_bgr = np.full((48, 96, 3), 110, dtype=np.uint8)
    full_linear = np.full((48, 96, 3), 0.5, dtype=np.float32)
    strip_bgr = np.full((24, strip_w, 3), 100, dtype=np.uint8)
    strip_linear = np.full((24, strip_w, 3), 0.5, dtype=np.float32)
    contour = np.array([[[1, 1]], [[strip_w, 1]], [[strip_w, 24]], [[1, 24]]], dtype=np.int32)

    monkeypatch.setattr(processor, "load_preview_jpeg", lambda *_args, **_kwargs: preview.copy())
    monkeypatch.setattr(processor, "load_raw_both", lambda _path: (full_bgr.copy(), full_linear.copy()))
    monkeypatch.setattr(processor, "load_flatfield_linear", lambda _path: full_linear.copy())
    monkeypatch.setattr(processor, "match_flatfield_orientation", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(processor, "find_strip_contour", lambda *_args, **_kwargs: contour.copy())
    monkeypatch.setattr(processor, "deskew_strip", lambda *_args, **_kwargs: strip_bgr.copy())
    monkeypatch.setattr(processor, "deskew_strip_linear", lambda *_args, **_kwargs: strip_linear.copy())
    monkeypatch.setattr(processor, "register_flatfield", lambda *_args, **_kwargs: full_linear.copy())
    monkeypatch.setattr(processor, "apply_flatfield", lambda linear, _flat: linear)
    monkeypatch.setattr(processor, "detect_swatch_extent", lambda *_args, **_kwargs: (0, 0, strip_w, 12))
    monkeypatch.setattr(
        processor,
        "find_swatch_boundaries",
        lambda *_args, **_kwargs: [idx * 12 for idx in range(cfg.num_swatches + 1)],
    )
    monkeypatch.setattr(processor, "get_spine_gut_check_enabled", lambda: False)

    real_publish = processor.publish_extraction_update

    def checked_publish(*args, **kwargs):
        assert (live_dir / "source.jpg").read_bytes() == old_source
        assert (live_dir / "strip.jpg").read_bytes() == old_strip
        semantic_commit = kwargs["semantic_commit"]

        def checked_semantic_commit():
            assert (live_dir / "source.jpg").read_bytes() == old_source
            assert (live_dir / "strip.jpg").read_bytes() == old_strip
            committed = semantic_commit()
            assert (live_dir / "source.jpg").read_bytes() == old_source
            assert (live_dir / "strip.jpg").read_bytes() == old_strip
            return committed

        kwargs["semantic_commit"] = checked_semantic_commit
        return real_publish(*args, **kwargs)

    monkeypatch.setattr(processor, "publish_extraction_update", checked_publish)

    result = processor.process_sample(
        sample,
        image_path,
        blank_path,
        sample.orientation_rots,
        store,
        commit=True,
    )

    assert result.status == "success"
    assert store.get_extraction_result(sample.sample_id)["extraction_result_id"] != prior_result_id
    assert (live_dir / "source.jpg").read_bytes() != old_source
    assert (live_dir / "strip.jpg").read_bytes() != old_strip
    publication_root = store.root / "_system" / "extraction_publications"
    visual_stage_root = store.root / "_system" / "extraction_visual_stages"
    assert not publication_root.exists() or not any(publication_root.iterdir())
    assert not visual_stage_root.exists() or not any(visual_stage_root.iterdir())


def _install_fake_appearance(
    monkeypatch: pytest.MonkeyPatch,
    colors: dict[int, tuple[float, float, float]],
    *,
    flipped: bool = True,
    order_correlation: float = 0.99,
    decode_environment: dict[str, str] | None = None,
) -> None:
    boxes = {index: (2 + index * 8, 2, 8 + index * 8, 8) for index in colors}
    strip_rgb = np.full((10, 24, 3), 180, dtype=np.uint8)
    monkeypatch.setattr(
        maintenance_reextract,
        "_embedded_jpeg_extraction",
        lambda **_kwargs: SimpleNamespace(
            colors_by_swatch_index={
                index: np.array(value, dtype=np.float64)
                for index, value in colors.items()
            },
            appearance_source=maintenance_reextract.APPEARANCE_SOURCE_PROVENANCE_QUAD,
            flipped=flipped,
            order_correlation=order_correlation,
            strip_rgb=strip_rgb,
            boxes_by_swatch_index=boxes,
        ),
    )
    monkeypatch.setattr(
        maintenance_reextract,
        "_source_strip_and_sampling_boxes_from_target",
        lambda *_args, **_kwargs: (
            np.full((10, 24, 3), 120, dtype=np.uint8),
            {0: (2, 2, 8, 8), 1: (10, 2, 16, 8)},
            {"coordinate_space": "test"},
        ),
    )
    monkeypatch.setattr(
        maintenance_reextract,
        "_decode_environment",
        lambda: decode_environment or {"rawpy": "test-reextract"},
    )


def _install_fake_existing_coordinate_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    bgr = np.zeros((30, 30, 3), dtype=np.uint8)
    bgr[:, :15] = (30, 80, 180)
    bgr[:, 15:] = (80, 140, 220)
    linear = np.full((30, 30, 3), 0.5, dtype=np.float32)
    flat = np.ones((30, 30, 3), dtype=np.float32)

    monkeypatch.setattr(maintenance_reextract, "load_raw_both", lambda _path: (bgr.copy(), linear.copy()))
    monkeypatch.setattr(maintenance_reextract, "match_flatfield_orientation", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(maintenance_reextract, "register_flatfield", lambda *_args, **_kwargs: flat.copy())
    monkeypatch.setattr(maintenance_reextract, "register_flatfield_strict", lambda *_args, **_kwargs: flat.copy())
    monkeypatch.setattr(maintenance_reextract, "detect_swatch_extent", lambda *_args, **_kwargs: (0, 0, 10, 20))
    monkeypatch.setattr(maintenance_reextract, "find_swatch_boundaries", lambda *_args, **_kwargs: [0, 3, 6, 10])
    monkeypatch.setattr(
        maintenance_reextract,
        "build_appearance_strip_visual",
        lambda **_kwargs: np.full((10, 20, 3), 160, dtype=np.uint8),
    )

    class _FakeAppearance:
        colors_by_swatch_index = {
            0: (11.0, 22.0, 33.0),
            1: (44.0, 55.0, 66.0),
            2: (77.0, 88.0, 99.0),
        }
        appearance_source = maintenance_reextract.APPEARANCE_SOURCE_PROVENANCE_QUAD
        order_correlation = 1.0
        flipped = False

    monkeypatch.setattr(extraction_result_module, "_extract_appearance", lambda *_args, **_kwargs: _FakeAppearance())
    monkeypatch.setattr(extraction_result_module, "_decode_environment", lambda: {"rawpy": "test-replay"})


def _accepted_store_with_model_fits(
    tmp_path: Path,
    *,
    appearance_source: str = "embedded_jpeg",
    appearance_flipped: bool = False,
    appearance_order_correlation: float = 0.95,
    decode_environment: dict[str, str] | None = None,
):
    store = _store(tmp_path, materialize_assets=True)
    accepted = _result(result_id="ext-original").model_copy(update={"review_state": "accepted"})
    payload = accepted.model_dump()
    for swatch in payload["measurements"]["swatches"]:
        swatch["appearance"]["source"] = appearance_source
    payload["diagnostics"]["appearance_orientation_flipped"] = appearance_flipped
    payload["diagnostics"]["appearance_order_correlation"] = appearance_order_correlation
    payload["diagnostics"]["appearance_order_correlation_state"] = "finite"
    payload["diagnostics"]["decode_environment"] = decode_environment or {"rawpy": "test", "pillow": "test"}
    store.save_extraction_result("exp-001", payload)
    contributor = [{"sample_id": "exp-001", "extraction_result_id": "ext-original", "included_swatch_count": 2}]
    for kind in ("camera_transform", "legacy_spline", "photo_stack_v2"):
        store.publish_model_fit(model_kind=kind, model_fit_id=f"fit-{kind}", contributors=contributor)
    return store


def _ready_saved_complete_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[SQLiteDataStore, str]:
    store = _accepted_store_with_model_fits(tmp_path)
    _install_fake_existing_coordinate_replay(monkeypatch)
    scope = _scope_for("complete")
    preflight = maintenance_reextract.preflight_reextract_sample_images(store, scope)
    report = maintenance_reextract.generate_reextract_candidates(store, scope, preflight=preflight)
    candidate_set_id = report["summary"]["candidate_set_id"]
    _mark_candidate_save(store, candidate_set_id)
    return store, candidate_set_id


def test_complete_existing_coordinate_candidates_apply_full_sidecar_and_stale_all_models(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _accepted_store_with_model_fits(tmp_path)
    _install_fake_existing_coordinate_replay(monkeypatch)

    scope = _scope_for("complete")
    preflight = maintenance_reextract.preflight_reextract_sample_images(store, scope)
    assert preflight["enabled"] is True
    report = maintenance_reextract.generate_reextract_candidates(store, scope, preflight=preflight)
    candidate_set_id = report["summary"]["candidate_set_id"]
    candidate_set_dir = maintenance_reextract.candidate_set_path(store, candidate_set_id)
    candidate = maintenance_reextract.load_candidate_sample(store, candidate_set_id, "exp-001")

    assert candidate["status"] == "ready_changed"
    assert candidate["review"]["decision"] == "pending"
    assert candidate["review"]["accepted"] is False
    assert candidate["replacement_extraction_result"]["review_state"] == "accepted"
    assert set(candidate["artifacts"]) == {"source", "blank", "strip", "appearance", "transmission_roi"}
    for kind in candidate["artifacts"]:
        assert maintenance_reextract.candidate_artifact_path(store, candidate_set_id, "exp-001", kind).exists()
    assert store.get_extraction_result("exp-001")["extraction_result_id"] == "ext-original"

    with pytest.raises(ValueError, match="Choose Save or Skip"):
        maintenance_reextract.apply_reextract_candidates(store, candidate_set_id)
    _mark_candidate_save(store, candidate_set_id)
    apply_report = maintenance_reextract.apply_reextract_candidates(store, candidate_set_id)

    assert apply_report["candidate_set_deleted"] is True
    assert apply_report["candidate_set_cleanup_warning"] == ""
    assert not candidate_set_dir.exists()
    assert apply_report["summary"]["applied_changed"] == 1
    after = store.get_extraction_result("exp-001")
    assert after["extraction_result_id"] != "ext-original"
    assert after["measurements"]["swatches"][0]["appearance"]["source"] == maintenance_reextract.APPEARANCE_SOURCE_PROVENANCE_QUAD
    assert store.get_model_fit("fit-camera_transform")["currentness_state"] == "stale"
    assert store.get_model_fit("fit-legacy_spline")["currentness_state"] == "stale"
    assert store.get_model_fit("fit-photo_stack_v2")["currentness_state"] == "stale"


def test_reextract_apply_cancels_before_starting_the_sample_publication_unit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, candidate_set_id = _ready_saved_complete_candidate(tmp_path, monkeypatch)
    action = ""

    def progress_cb(**payload):
        nonlocal action
        action = str(payload.get("action") or "")

    report = maintenance_reextract.apply_reextract_candidates(
        store,
        candidate_set_id,
        progress_cb=progress_cb,
        should_cancel=lambda: action == "publish_sample",
    )

    assert report["status"] == "cancelled"
    assert store.get_extraction_result("exp-001")["extraction_result_id"] == "ext-original"
    assert maintenance_reextract.load_candidate_sample(store, candidate_set_id, "exp-001")["status"] == "ready_changed"
    root = store.root / "_system" / "extraction_publications"
    assert not root.exists() or not any(root.iterdir())


def test_reextract_cancel_on_final_sample_completes_wholly_new_when_unit_already_started(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, candidate_set_id = _ready_saved_complete_candidate(tmp_path, monkeypatch)
    cancel_requested = False
    real_publish = maintenance_reextract.publish_extraction_update

    def publish_and_request_cancel(*args, **kwargs):
        semantic_commit = kwargs["semantic_commit"]

        def commit_then_cancel():
            nonlocal cancel_requested
            result = semantic_commit()
            cancel_requested = True
            return result

        kwargs["semantic_commit"] = commit_then_cancel
        return real_publish(*args, **kwargs)

    monkeypatch.setattr(maintenance_reextract, "publish_extraction_update", publish_and_request_cancel)
    report = maintenance_reextract.apply_reextract_candidates(
        store,
        candidate_set_id,
        should_cancel=lambda: cancel_requested,
    )

    assert cancel_requested is True
    assert report["status"] == "completed"
    assert report["summary"]["applied_changed"] == 1
    assert store.get_extraction_result("exp-001")["extraction_result_id"] != "ext-original"


def test_reextract_live_replace_failure_keeps_journal_visuals_current_and_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, candidate_set_id = _ready_saved_complete_candidate(tmp_path, monkeypatch)
    live_dir = store.root / "thumbnails" / "exp-001"
    live_dir.mkdir(parents=True, exist_ok=True)
    old_source = b"old-source"
    old_strip = b"old-strip"
    (live_dir / "source.jpg").write_bytes(old_source)
    (live_dir / "strip.jpg").write_bytes(old_strip)
    real_publish_files = extraction_publication_module.publish_staged_files

    def fail_after_first_replace(replacements, *, boundary_hook=None):
        replaced = 0

        def combined(event, path):
            nonlocal replaced
            if boundary_hook is not None:
                boundary_hook(event, path)
            if event == "after_live_replace":
                replaced += 1
                if replaced == 1:
                    raise RuntimeError("injected re-extraction replace failure")

        return real_publish_files(replacements, boundary_hook=combined)

    monkeypatch.setattr(extraction_publication_module, "publish_staged_files", fail_after_first_replace)
    report = maintenance_reextract.apply_reextract_candidates(store, candidate_set_id)

    assert report["status"] == "completed"
    assert report["summary"]["visual_artifacts_pending_recovery"] == 2
    assert (live_dir / "source.jpg").read_bytes() == old_source
    assert (live_dir / "strip.jpg").read_bytes() == old_strip
    assert extraction_publication_module.resolve_visual_path(store, "exp-001", "source").read_bytes() != old_source
    assert extraction_publication_module.resolve_visual_path(store, "exp-001", "strip").read_bytes() != old_strip

    monkeypatch.setattr(extraction_publication_module, "publish_staged_files", real_publish_files)
    first = extraction_publication_module.reconcile_publications(store)
    second = extraction_publication_module.reconcile_publications(store)
    assert first["pending_finalization"] == []
    assert second["pending_finalization"] == []
    assert (live_dir / "source.jpg").read_bytes() != old_source
    assert (live_dir / "strip.jpg").read_bytes() != old_strip


def test_reextract_startup_recovery_finishes_candidate_after_semantic_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, candidate_set_id = _ready_saved_complete_candidate(tmp_path, monkeypatch)
    real_complete = maintenance_reextract.complete_reextract_publication

    def interrupt_candidate_completion(*_args, **_kwargs):
        raise RuntimeError("injected stop before candidate completion")

    monkeypatch.setattr(maintenance_reextract, "complete_reextract_publication", interrupt_candidate_completion)
    report = maintenance_reextract.apply_reextract_candidates(store, candidate_set_id)

    assert report["status"] == "failed"
    assert store.get_extraction_result("exp-001")["extraction_result_id"] != "ext-original"
    assert maintenance_reextract.load_candidate_sample(store, candidate_set_id, "exp-001")["status"] == "ready_changed"

    recovery = extraction_publication_module.reconcile_publications(store)
    assert len(recovery["pending_finalization"]) == 1
    real_complete(store, recovery["pending_finalization"][0])
    assert maintenance_reextract.load_candidate_sample(store, candidate_set_id, "exp-001")["status"] == "applied"
    assert extraction_publication_module.reconcile_publications(store)["pending_finalization"] == []


@pytest.mark.parametrize("failing_write_name", ["_write_review", "_write_manifest"])
def test_reextract_candidate_completion_converges_after_intermediate_file_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failing_write_name: str,
) -> None:
    store, candidate_set_id = _ready_saved_complete_candidate(tmp_path, monkeypatch)
    real_write = getattr(maintenance_reextract, failing_write_name)
    failed_once = False

    def fail_once(*args, **kwargs):
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise RuntimeError(f"injected {failing_write_name} interruption")
        return real_write(*args, **kwargs)

    monkeypatch.setattr(maintenance_reextract, failing_write_name, fail_once)
    report = maintenance_reextract.apply_reextract_candidates(store, candidate_set_id)

    assert report["status"] == "failed"
    assert failed_once is True
    assert store.get_extraction_result("exp-001")["extraction_result_id"] != "ext-original"
    recovery = extraction_publication_module.reconcile_publications(store)
    assert len(recovery["pending_finalization"]) == 1

    monkeypatch.setattr(maintenance_reextract, failing_write_name, real_write)
    maintenance_reextract.complete_reextract_publication(store, recovery["pending_finalization"][0])
    completed = maintenance_reextract.load_candidate_sample(store, candidate_set_id, "exp-001")
    assert completed["status"] == "applied"
    assert completed["review"]["status"] == "applied"
    assert completed["review"]["decision"] == "skip"
    assert extraction_publication_module.reconcile_publications(store)["pending_finalization"] == []


def _isolate_startup_publication_housekeeping(
    monkeypatch: pytest.MonkeyPatch,
    store: SQLiteDataStore,
) -> None:
    monkeypatch.setattr(server, "remove_all_manual_review_visuals", lambda _root: None)
    monkeypatch.setattr(store, "prune_superseded_model_fits", lambda: [])
    monkeypatch.setattr(server, "_portable_calibration_layout_configured", lambda _store: False)
    monkeypatch.setattr(server, "_run_backup_temporary_housekeeping", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(server, "_maintenance_startup_scan_interrupted_temp", lambda _store: [])
    monkeypatch.setattr(server, "_run_sqlite_restore_point_startup", lambda _store: None)


def test_startup_completes_reextract_publication_before_candidate_pruning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, candidate_set_id = _ready_saved_complete_candidate(tmp_path, monkeypatch)
    real_complete = maintenance_reextract.complete_reextract_publication
    monkeypatch.setattr(
        maintenance_reextract,
        "complete_reextract_publication",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("injected stop before completion")),
    )
    assert maintenance_reextract.apply_reextract_candidates(store, candidate_set_id)["status"] == "failed"
    monkeypatch.setattr(maintenance_reextract, "complete_reextract_publication", real_complete)
    _isolate_startup_publication_housekeeping(monkeypatch, store)
    prune_observations: list[str] = []

    def observe_prune(_store):
        candidate = maintenance_reextract.load_candidate_sample(store, candidate_set_id, "exp-001")
        prune_observations.append(candidate["status"])
        return {"deleted": []}

    monkeypatch.setattr(server, "_prune_reextract_candidate_sets", observe_prune)
    server._run_post_store_startup_checks(store)  # type: ignore[attr-defined]

    assert prune_observations == ["applied"]
    assert extraction_publication_module.reconcile_publications(store)["pending_finalization"] == []


def test_startup_defers_candidate_pruning_when_reextract_finalization_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, candidate_set_id = _ready_saved_complete_candidate(tmp_path, monkeypatch)
    real_complete = maintenance_reextract.complete_reextract_publication
    monkeypatch.setattr(
        maintenance_reextract,
        "complete_reextract_publication",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("injected stop before completion")),
    )
    assert maintenance_reextract.apply_reextract_candidates(store, candidate_set_id)["status"] == "failed"
    monkeypatch.setattr(maintenance_reextract, "complete_reextract_publication", real_complete)
    _isolate_startup_publication_housekeeping(monkeypatch, store)
    prune_calls: list[bool] = []
    monkeypatch.setattr(
        server,
        "_complete_reextract_publication",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("injected startup finalization failure")),
    )
    monkeypatch.setattr(
        server,
        "_prune_reextract_candidate_sets",
        lambda _store: prune_calls.append(True) or {"deleted": []},
    )

    server._run_post_store_startup_checks(store)  # type: ignore[attr-defined]

    assert prune_calls == []
    assert maintenance_reextract.load_candidate_sample(store, candidate_set_id, "exp-001")["status"] == "ready_changed"


def test_existing_coordinate_replay_keeps_automatic_brightness_flip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _accepted_store_with_model_fits(tmp_path)
    _install_fake_existing_coordinate_replay(monkeypatch)
    monkeypatch.setattr(maintenance_reextract, "_detect_strip_needs_flip", lambda _strip: True)

    scope = _scope_for("complete")
    report = maintenance_reextract.generate_reextract_candidates(
        store,
        scope,
        preflight=maintenance_reextract.preflight_reextract_sample_images(store, scope),
    )
    candidate = maintenance_reextract.load_candidate_sample(store, report["summary"]["candidate_set_id"], "exp-001")

    assert candidate["diagnostics"]["strip_orientation_flipped"] is True


def test_existing_coordinate_replay_preserves_manual_orientation_without_brightness_flip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _accepted_store_with_model_fits(tmp_path)
    original = _result(result_id="ext-original").model_copy(update={"review_state": "accepted"})
    manual_original = original.model_copy(
        update={
            "method": "manual",
            "method_provenance": original.method_provenance.model_copy(
                update={
                    "strip_location_source": "manual_corner_selection",
                    "coordinate_space": maintenance_reextract.MANUAL_FULL_COORDINATE_SPACE,
                }
            ),
        }
    )
    store.save_extraction_result("exp-001", manual_original.model_dump())
    _install_fake_existing_coordinate_replay(monkeypatch)
    monkeypatch.setattr(maintenance_reextract, "_detect_strip_needs_flip", lambda _strip: True)

    scope = _scope_for("complete")
    report = maintenance_reextract.generate_reextract_candidates(
        store,
        scope,
        preflight=maintenance_reextract.preflight_reextract_sample_images(store, scope),
    )
    candidate = maintenance_reextract.load_candidate_sample(store, report["summary"]["candidate_set_id"], "exp-001")

    assert candidate["diagnostics"]["strip_orientation_flipped"] is False


def test_selected_sample_scope_limits_reextract_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _accepted_store_with_model_fits(tmp_path)
    _install_fake_existing_coordinate_replay(monkeypatch)

    scope = _scope_for_samples(["exp-001", "exp-missing"], domain_mode="complete")
    preflight = maintenance_reextract.preflight_reextract_sample_images(store, scope)

    assert preflight["enabled"] is True
    assert preflight["scope"]["sample_scope"] == {
        "kind": "sample_ids",
        "sample_ids": ["exp-001", "exp-missing"],
    }
    assert preflight["summary"]["sample_scope_kind"] == "sample_ids"
    assert preflight["summary"]["requested_samples"] == 2
    assert preflight["summary"]["targets"] == 1
    assert preflight["summary"]["blocked"] == 1
    assert preflight["blocked"][0]["target"] == "exp-missing"
    assert preflight["blocked"][0]["category"] == "accepted_extraction_not_found"

    report = maintenance_reextract.generate_reextract_candidates(store, scope, preflight=preflight)

    assert report["summary"]["targets"] == 1
    assert report["summary"]["ready_changed"] == 1
    candidate_set_id = report["summary"]["candidate_set_id"]
    assert [row["sample_id"] for row in maintenance_reextract.list_candidate_samples(store, candidate_set_id)] == ["exp-001"]


def test_selected_sample_scope_requires_at_least_one_id(tmp_path: Path) -> None:
    store = _accepted_store_with_model_fits(tmp_path)

    preflight = maintenance_reextract.preflight_reextract_sample_images(store, _scope_for_samples([]))

    assert preflight["enabled"] is False
    assert preflight["blocked"][0]["category"] == "mode_not_implemented"
    assert preflight["warnings"] == ["Enter at least one sample ID."]


def test_selected_redetect_scope_lists_manual_samples_for_manual_corners(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _accepted_store_with_model_fits(tmp_path)
    original = _result(result_id="ext-original").model_copy(update={"review_state": "accepted"})
    manual_original = original.model_copy(
        update={
            "method": "manual",
            "method_provenance": original.method_provenance.model_copy(
                update={
                    "strip_location_source": "manual_corner_selection",
                    "coordinate_space": maintenance_reextract.MANUAL_FULL_COORDINATE_SPACE,
                }
            ),
        }
    )
    store.save_extraction_result("exp-001", manual_original.model_dump())
    manual_result = _result(result_id="ext-manual-replacement").model_copy(update={"review_state": "accepted"})

    def fake_manual(**kwargs):
        assert kwargs["commit"] is False
        assert kwargs["build_extraction_payload"] is True
        sink = kwargs["artifact_sink"]
        sample = kwargs["sample"]
        for kind in ("source", "blank", "strip", "appearance", "transmission_roi"):
            sink.write_image(sample.sample_id, kind, np.full((10, 20, 3), 120, dtype=np.uint8))
        payload = manual_result.model_dump()
        payload["method"] = "manual"
        payload["measurements"]["swatches"][0]["transmission"]["R_linear"] = 0.654
        return ProcessingResult(
            sample_id=sample.sample_id,
            status="success",
            confidence=ProcessingConfidence(detection_strategy="manual", contour_found=True),
            extraction_result_payload=payload,
        )

    monkeypatch.setattr(maintenance_reextract, "extract_strip_manual", fake_manual)
    monkeypatch.setattr(maintenance_reextract, "_preview_scale_for_candidate_source", lambda **_kwargs: 1.0)
    scope = _scope_for_samples(["exp-001"], domain_mode="complete", segmentation_mode="redetect_from_scratch")
    preflight = maintenance_reextract.preflight_reextract_sample_images(store, scope)

    assert preflight["summary"]["targets"] == 0
    assert preflight["summary"]["expected_candidates"] == 1
    assert preflight["summary"]["manual_required"] == 1
    assert preflight["summary"]["blocked"] == 0
    assert preflight["summary"]["unsupported_provenance"] == 0

    report = maintenance_reextract.generate_reextract_candidates(store, scope, preflight=preflight)
    candidate_set_id = report["summary"]["candidate_set_id"]
    rows = maintenance_reextract.list_candidate_samples(store, candidate_set_id)

    assert report["summary"]["manual_required"] == 1
    assert report["summary"]["blocked"] == 0
    assert [(row["sample_id"], row["status"]) for row in rows] == [("exp-001", "manual_required")]
    assert rows[0]["review"]["accepted"] is False

    candidate = maintenance_reextract.generate_manual_candidate(
        store,
        candidate_set_id,
        "exp-001",
        corners=[{"x": 0, "y": 0}, {"x": 9, "y": 0}, {"x": 9, "y": 9}, {"x": 0, "y": 9}],
        orientation=2,
        preview_width=10,
        preview_height=10,
    )

    assert candidate["status"] == "ready_changed"
    assert candidate["replacement_extraction_result"]["method"] == "manual"
    assert maintenance_reextract.load_manifest(store, candidate_set_id).counts_by_status == {"ready_changed": 1}
    manual_row = maintenance_reextract.list_candidate_samples(store, candidate_set_id)[0]
    assert manual_row["review"]["decision"] == "pending"
    assert manual_row["review"]["accepted"] is False
    assert store.get_extraction_result("exp-001")["extraction_result_id"] == "ext-original"


def test_transmission_only_existing_coordinate_candidates_preserve_appearance_and_stale_all_models(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _accepted_store_with_model_fits(tmp_path)
    before = store.get_extraction_result("exp-001")
    _install_fake_existing_coordinate_replay(monkeypatch)

    scope = _scope_for("transmission_only")
    report = maintenance_reextract.generate_reextract_candidates(
        store,
        scope,
        preflight=maintenance_reextract.preflight_reextract_sample_images(store, scope),
    )
    candidate_set_id = report["summary"]["candidate_set_id"]
    candidate_set_dir = maintenance_reextract.candidate_set_path(store, candidate_set_id)
    candidate = maintenance_reextract.load_candidate_sample(store, candidate_set_id, "exp-001")

    assert candidate["status"] == "ready_changed"
    assert "appearance" not in candidate["artifacts"]
    assert (
        candidate["replacement_extraction_result"]["measurements"]["swatches"][0]["appearance"]
        == before["measurements"]["swatches"][0]["appearance"]
    )

    _mark_candidate_save(store, candidate_set_id)
    apply_report = maintenance_reextract.apply_reextract_candidates(store, candidate_set_id)

    assert apply_report["candidate_set_deleted"] is True
    assert not candidate_set_dir.exists()
    assert apply_report["summary"]["applied_changed"] == 1
    after = store.get_extraction_result("exp-001")
    assert after["measurements"]["swatches"][0]["appearance"] == before["measurements"]["swatches"][0]["appearance"]
    assert after["measurements"]["swatches"][0]["transmission"] != before["measurements"]["swatches"][0]["transmission"]
    assert store.get_model_fit("fit-camera_transform")["currentness_state"] == "stale"
    assert store.get_model_fit("fit-legacy_spline")["currentness_state"] == "stale"
    assert store.get_model_fit("fit-photo_stack_v2")["currentness_state"] == "stale"


def test_partial_redetect_modes_are_blocked(tmp_path: Path) -> None:
    store = _accepted_store_with_model_fits(tmp_path)

    for domain_mode in ("appearance_only", "transmission_only"):
        preflight = maintenance_reextract.preflight_reextract_sample_images(
            store,
            _scope_for(domain_mode, "redetect_from_scratch"),
        )

        assert preflight["enabled"] is False
        assert preflight["blocked"][0]["category"] == "mode_not_implemented"


def test_complete_redetect_automatic_candidates_are_staged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _accepted_store_with_model_fits(tmp_path)
    base = _result(result_id="ext-redetect").model_copy(update={"review_state": "accepted"})

    def fake_process_sample(sample, _source, _blank, _orientation, _store, *, commit, artifact_sink, build_extraction_payload):
        assert commit is False
        assert build_extraction_payload is True
        for kind in ("source", "blank", "strip", "appearance", "transmission_roi"):
            artifact_sink.write_image(sample.sample_id, kind, np.full((10, 20, 3), 90, dtype=np.uint8))
        payload = base.model_dump()
        payload["measurements"]["swatches"][0]["transmission"]["R_linear"] = 0.777
        return ProcessingResult(
            sample_id=sample.sample_id,
            status="success",
            confidence=ProcessingConfidence(spine_score=0.87, detection_strategy="cascade", contour_found=True),
            extraction_result_payload=payload,
        )

    monkeypatch.setattr(maintenance_reextract, "_process_sample", fake_process_sample)

    scope = _scope_for("complete", "redetect_from_scratch")
    report = maintenance_reextract.generate_reextract_candidates(
        store,
        scope,
        preflight=maintenance_reextract.preflight_reextract_sample_images(store, scope),
    )
    candidate = maintenance_reextract.load_candidate_sample(store, report["summary"]["candidate_set_id"], "exp-001")

    assert candidate["status"] == "ready_changed"
    assert set(candidate["artifacts"]) == {"source", "blank", "strip", "appearance", "transmission_roi"}
    assert candidate["diagnostics"]["confidence"]["spine_score"] == 0.87
    assert candidate["replacement_extraction_result"]["evidence_binding"]["cr2_source"] == "images"


def test_complete_redetect_candidate_missing_required_artifact_is_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _accepted_store_with_model_fits(tmp_path)
    base = _result(result_id="ext-missing-artifact").model_copy(update={"review_state": "accepted"})

    def fake_process_sample(sample, _source, _blank, _orientation, _store, *, commit, artifact_sink, build_extraction_payload):
        assert commit is False
        assert build_extraction_payload is True
        for kind in ("source", "blank", "strip", "transmission_roi"):
            artifact_sink.write_image(sample.sample_id, kind, np.full((10, 20, 3), 90, dtype=np.uint8))
        payload = base.model_dump()
        payload["measurements"]["swatches"][0]["transmission"]["R_linear"] = 0.777
        return ProcessingResult(
            sample_id=sample.sample_id,
            status="success",
            confidence=ProcessingConfidence(spine_score=0.87, detection_strategy="cascade", contour_found=True),
            extraction_result_payload=payload,
        )

    monkeypatch.setattr(maintenance_reextract, "_process_sample", fake_process_sample)

    scope = _scope_for("complete", "redetect_from_scratch")
    report = maintenance_reextract.generate_reextract_candidates(
        store,
        scope,
        preflight=maintenance_reextract.preflight_reextract_sample_images(store, scope),
    )
    candidate = maintenance_reextract.load_candidate_sample(store, report["summary"]["candidate_set_id"], "exp-001")

    assert report["summary"]["failed"] == 1
    assert report["summary"]["ready_changed"] == 0
    assert candidate["status"] == "failed"
    assert "appearance" in candidate["error"]
    assert candidate["diagnostics"]["missing_required_artifacts"] == ["appearance"]


def test_complete_redetect_failure_records_candidate_without_aborting_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _accepted_store_with_model_fits(tmp_path)

    def fake_process_sample(sample, *_args, **_kwargs):
        return ProcessingResult(sample_id=sample.sample_id, status="failed_detection", error_detail="no strip")

    monkeypatch.setattr(maintenance_reextract, "_process_sample", fake_process_sample)

    scope = _scope_for("complete", "redetect_from_scratch")
    report = maintenance_reextract.generate_reextract_candidates(
        store,
        scope,
        preflight=maintenance_reextract.preflight_reextract_sample_images(store, scope),
    )
    candidate = maintenance_reextract.load_candidate_sample(store, report["summary"]["candidate_set_id"], "exp-001")

    assert report["status"] == "completed"
    assert report["summary"]["failed"] == 1
    assert candidate["status"] == "failed"
    assert candidate["error"] == "no strip"


def test_manual_corners_generate_candidate_inside_redetect_set_without_live_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _accepted_store_with_model_fits(tmp_path)
    original = store.get_extraction_result("exp-001")
    manual_result = _result(result_id="ext-manual-candidate").model_copy(update={"review_state": "accepted"})

    def fake_manual(**kwargs):
        assert kwargs["commit"] is False
        assert kwargs["build_extraction_payload"] is True
        sink = kwargs["artifact_sink"]
        sample = kwargs["sample"]
        for kind in ("source", "blank", "strip", "appearance", "transmission_roi"):
            sink.write_image(sample.sample_id, kind, np.full((10, 20, 3), 120, dtype=np.uint8))
        payload = manual_result.model_dump()
        payload["method"] = "manual"
        payload["measurements"]["swatches"][0]["transmission"]["R_linear"] = 0.654
        return ProcessingResult(
            sample_id=sample.sample_id,
            status="success",
            confidence=ProcessingConfidence(detection_strategy="manual", contour_found=True),
            extraction_result_payload=payload,
        )

    monkeypatch.setattr(maintenance_reextract, "extract_strip_manual", fake_manual)
    monkeypatch.setattr(maintenance_reextract, "_preview_scale_for_candidate_source", lambda **_kwargs: 1.0)
    scope = _scope_for("complete", "redetect_from_scratch")
    report = maintenance_reextract.generate_reextract_candidates(
        store,
        scope,
        preflight=maintenance_reextract.preflight_reextract_sample_images(store, scope),
    )

    candidate = maintenance_reextract.generate_manual_candidate(
        store,
        report["summary"]["candidate_set_id"],
        "exp-001",
        corners=[{"x": 0, "y": 0}, {"x": 9, "y": 0}, {"x": 9, "y": 9}, {"x": 0, "y": 9}],
        orientation=2,
        preview_width=10,
        preview_height=10,
    )

    assert candidate["status"] == "ready_changed"
    assert candidate["replacement_extraction_result"]["method"] == "manual"
    assert candidate["diagnostics"]["manual_orientation"] == 2
    assert set(candidate["artifacts"]) == {"source", "blank", "strip", "appearance", "transmission_roi"}
    assert store.get_extraction_result("exp-001") == original


def test_manual_corners_missing_required_artifact_is_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _accepted_store_with_model_fits(tmp_path)
    original = store.get_extraction_result("exp-001")
    manual_result = _result(result_id="ext-manual-missing-artifact").model_copy(update={"review_state": "accepted"})

    def fake_manual(**kwargs):
        assert kwargs["commit"] is False
        assert kwargs["build_extraction_payload"] is True
        sink = kwargs["artifact_sink"]
        sample = kwargs["sample"]
        for kind in ("source", "blank", "strip", "transmission_roi"):
            sink.write_image(sample.sample_id, kind, np.full((10, 20, 3), 120, dtype=np.uint8))
        payload = manual_result.model_dump()
        payload["method"] = "manual"
        payload["measurements"]["swatches"][0]["transmission"]["R_linear"] = 0.654
        return ProcessingResult(
            sample_id=sample.sample_id,
            status="success",
            confidence=ProcessingConfidence(detection_strategy="manual", contour_found=True),
            extraction_result_payload=payload,
        )

    monkeypatch.setattr(maintenance_reextract, "extract_strip_manual", fake_manual)
    monkeypatch.setattr(maintenance_reextract, "_preview_scale_for_candidate_source", lambda **_kwargs: 1.0)
    scope = _scope_for("complete", "redetect_from_scratch")
    report = maintenance_reextract.generate_reextract_candidates(
        store,
        scope,
        preflight=maintenance_reextract.preflight_reextract_sample_images(store, scope),
    )

    candidate = maintenance_reextract.generate_manual_candidate(
        store,
        report["summary"]["candidate_set_id"],
        "exp-001",
        corners=[{"x": 0, "y": 0}, {"x": 9, "y": 0}, {"x": 9, "y": 9}, {"x": 0, "y": 9}],
        orientation=2,
        preview_width=10,
        preview_height=10,
    )

    assert candidate["status"] == "failed"
    assert "appearance" in candidate["error"]
    assert candidate["diagnostics"]["missing_required_artifacts"] == ["appearance"]
    assert maintenance_reextract.load_manifest(store, report["summary"]["candidate_set_id"]).counts_by_status == {"failed": 1}
    assert store.get_extraction_result("exp-001") == original


def test_manual_corners_endpoint_rejects_malformed_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _accepted_store_with_model_fits(tmp_path)
    monkeypatch.setattr(server, "_store", store)
    client = TestClient(server.app, raise_server_exceptions=False)
    scope = _scope_for("complete", "redetect_from_scratch")
    report = maintenance_reextract.generate_reextract_candidates(
        store,
        scope,
        preflight=maintenance_reextract.preflight_reextract_sample_images(store, scope),
    )

    response = client.post(
        f"/api/maintenance/reextract-sample-images/candidate-sets/{report['summary']['candidate_set_id']}/samples/exp-001/manual-corners",
        json={"corners": [{"x": 0, "y": 0}], "orientation": 0, "preview_width": 10, "preview_height": 10},
    )

    assert response.status_code == 400


def test_manual_corners_job_reports_failed_candidate_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _accepted_store_with_model_fits(tmp_path)
    manual_result = _result(result_id="ext-manual-job-missing-artifact").model_copy(update={"review_state": "accepted"})

    def fake_manual(**kwargs):
        sink = kwargs["artifact_sink"]
        sample = kwargs["sample"]
        for kind in ("source", "blank", "strip", "transmission_roi"):
            sink.write_image(sample.sample_id, kind, np.full((10, 20, 3), 120, dtype=np.uint8))
        payload = manual_result.model_dump()
        payload["method"] = "manual"
        payload["measurements"]["swatches"][0]["transmission"]["R_linear"] = 0.654
        return ProcessingResult(
            sample_id=sample.sample_id,
            status="success",
            confidence=ProcessingConfidence(detection_strategy="manual", contour_found=True),
            extraction_result_payload=payload,
        )

    monkeypatch.setattr(maintenance_reextract, "extract_strip_manual", fake_manual)
    monkeypatch.setattr(maintenance_reextract, "_preview_scale_for_candidate_source", lambda **_kwargs: 1.0)
    monkeypatch.setattr(server, "_store", store)
    client = TestClient(server.app, raise_server_exceptions=False)
    scope = _scope_for("complete", "redetect_from_scratch")
    report = maintenance_reextract.generate_reextract_candidates(
        store,
        scope,
        preflight=maintenance_reextract.preflight_reextract_sample_images(store, scope),
    )
    candidate_set_id = report["summary"]["candidate_set_id"]

    response = client.post(
        f"/api/maintenance/reextract-sample-images/candidate-sets/{candidate_set_id}/samples/exp-001/manual-corners/jobs",
        json={
            "corners": [{"x": 0, "y": 0}, {"x": 9, "y": 0}, {"x": 9, "y": 9}, {"x": 0, "y": 9}],
            "orientation": 0,
            "preview_width": 10,
            "preview_height": 10,
        },
    )
    assert response.status_code == 200, response.text

    job = _wait_reextract_job(client, response.json()["job_id"])

    assert job["status"] == "failed"
    assert "appearance" in job["error"]["message"]
    assert job["result"]["candidate"]["status"] == "failed"
    assert maintenance_reextract.load_candidate_sample(store, candidate_set_id, "exp-001")["status"] == "failed"


def test_candidate_review_rejection_excludes_sample_from_default_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _accepted_store_with_model_fits(tmp_path)
    _install_fake_existing_coordinate_replay(monkeypatch)
    original = store.get_extraction_result("exp-001")
    scope = _scope_for("complete")
    report = maintenance_reextract.generate_reextract_candidates(
        store,
        scope,
        preflight=maintenance_reextract.preflight_reextract_sample_images(store, scope),
    )
    candidate_set_id = report["summary"]["candidate_set_id"]

    reviewed = maintenance_reextract.update_candidate_review(
        store,
        candidate_set_id,
        "exp-001",
        decision="skip",
        note="skip",
    )

    with pytest.raises(ValueError, match="No candidates are marked Save"):
        maintenance_reextract.apply_reextract_candidates(store, candidate_set_id)
    assert reviewed["review"]["decision"] == "skip"
    assert reviewed["review"]["accepted"] is False
    assert store.get_extraction_result("exp-001") == original


def test_saved_candidate_that_goes_stale_reports_partial_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _accepted_store_with_model_fits(tmp_path)
    _install_fake_existing_coordinate_replay(monkeypatch)
    scope = _scope_for("complete")
    report = maintenance_reextract.generate_reextract_candidates(
        store,
        scope,
        preflight=maintenance_reextract.preflight_reextract_sample_images(store, scope),
    )
    candidate_set_id = report["summary"]["candidate_set_id"]
    candidate_set_dir = maintenance_reextract.candidate_set_path(store, candidate_set_id)
    _mark_candidate_save(store, candidate_set_id)

    changed_current = _result(result_id="ext-updated-before-apply").model_copy(update={"review_state": "accepted"})
    store.save_extraction_result("exp-001", changed_current.model_dump())

    apply_report = maintenance_reextract.apply_reextract_candidates(store, candidate_set_id)

    assert apply_report["status"] == "partial"
    assert apply_report["candidate_set_deleted"] is False
    assert candidate_set_dir.exists()
    assert apply_report["summary"]["applied_changed"] == 0
    assert apply_report["summary"]["saved_skipped"] == 1
    assert apply_report["summary"]["skipped"] == 1
    assert apply_report["findings"][0]["category"] == "stale"
    assert store.get_extraction_result("exp-001")["extraction_result_id"] == "ext-updated-before-apply"
    assert maintenance_reextract.load_candidate_sample(store, candidate_set_id, "exp-001")["status"] == "stale"


def test_saved_complete_candidate_missing_staged_artifact_fails_before_replacing_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _accepted_store_with_model_fits(tmp_path)
    _install_fake_existing_coordinate_replay(monkeypatch)
    original = store.get_extraction_result("exp-001")
    scope = _scope_for("complete")
    report = maintenance_reextract.generate_reextract_candidates(
        store,
        scope,
        preflight=maintenance_reextract.preflight_reextract_sample_images(store, scope),
    )
    candidate_set_id = report["summary"]["candidate_set_id"]
    candidate_set_dir = maintenance_reextract.candidate_set_path(store, candidate_set_id)
    _mark_candidate_save(store, candidate_set_id)
    maintenance_reextract.candidate_artifact_path(store, candidate_set_id, "exp-001", "appearance").unlink()

    apply_report = maintenance_reextract.apply_reextract_candidates(store, candidate_set_id)

    assert apply_report["status"] == "failed"
    assert apply_report["candidate_set_deleted"] is False
    assert candidate_set_dir.exists()
    assert apply_report["summary"]["applied_changed"] == 0
    assert apply_report["summary"]["failed"] == 1
    assert apply_report["findings"][0]["category"] == "missing_required_artifacts"
    assert apply_report["findings"][0]["missing_required_artifacts"] == ["appearance"]
    candidate = maintenance_reextract.load_candidate_sample(store, candidate_set_id, "exp-001")
    assert candidate["status"] == "failed"
    assert "appearance" in candidate["error"]
    assert candidate["review"]["decision"] == "skip"
    assert store.get_extraction_result("exp-001") == original


def test_retry_candidate_replaces_failed_candidate_in_same_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _accepted_store_with_model_fits(tmp_path)
    calls = {"count": 0}

    def fake_process_sample(sample, *_args, **_kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            return ProcessingResult(sample_id=sample.sample_id, status="failed_detection", error_detail="first fail")
        artifact_sink = _kwargs["artifact_sink"]
        for kind in ("source", "blank", "strip", "appearance", "transmission_roi"):
            artifact_sink.write_image(sample.sample_id, kind, np.full((10, 20, 3), 90, dtype=np.uint8))
        payload = _result(result_id="ext-retry").model_dump()
        payload["measurements"]["swatches"][0]["transmission"]["R_linear"] = 0.888
        return ProcessingResult(
            sample_id=sample.sample_id,
            status="success",
            confidence=ProcessingConfidence(spine_score=0.75, detection_strategy="cascade", contour_found=True),
            extraction_result_payload=payload,
        )

    monkeypatch.setattr(maintenance_reextract, "_process_sample", fake_process_sample)
    scope = _scope_for("complete", "redetect_from_scratch")
    report = maintenance_reextract.generate_reextract_candidates(
        store,
        scope,
        preflight=maintenance_reextract.preflight_reextract_sample_images(store, scope),
    )
    candidate_set_id = report["summary"]["candidate_set_id"]
    assert maintenance_reextract.load_candidate_sample(store, candidate_set_id, "exp-001")["status"] == "failed"

    retried = maintenance_reextract.retry_candidate(store, candidate_set_id, "exp-001")

    assert retried["status"] == "ready_changed"
    assert maintenance_reextract.load_manifest(store, candidate_set_id).counts_by_status == {"ready_changed": 1}


def test_delete_candidate_set_removes_staged_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _accepted_store_with_model_fits(tmp_path)
    _install_fake_appearance(monkeypatch, {0: (10.0, 20.0, 30.0), 1: (40.0, 50.0, 60.0)})
    report = maintenance_reextract.generate_appearance_existing_coordinate_candidates(
        store,
        _scope(),
        preflight=maintenance_reextract.preflight_reextract_sample_images(store, _scope()),
    )
    candidate_set_id = report["summary"]["candidate_set_id"]
    path = maintenance_reextract.candidate_set_path(store, candidate_set_id)

    deleted = maintenance_reextract.delete_candidate_set(store, candidate_set_id)

    assert deleted["deleted"] is True
    assert not path.exists()


def test_cleanup_retired_reextract_artifacts_dry_run_and_execute(tmp_path: Path) -> None:
    store = _accepted_store_with_model_fits(tmp_path)
    live_mock = store.root / "thumbnails" / "exp-001" / "mock.jpg"
    live_mock.parent.mkdir(parents=True, exist_ok=True)
    live_mock.write_bytes(b"retired live mock")

    applied_set_id = "rext_" + ("a" * 32)
    applied_dir = maintenance_reextract.candidate_set_path(store, applied_set_id)
    applied_mock = applied_dir / "candidates" / "exp-001" / "mock.jpg"
    applied_mock.parent.mkdir(parents=True, exist_ok=True)
    applied_mock.write_bytes(b"retired staged mock")
    maintenance_reextract._atomic_write_json(
        applied_dir / "manifest.json",
        maintenance_reextract.ReextractManifest(
            candidate_set_id=applied_set_id,
            created_at=maintenance_reextract._now_iso(),
            updated_at=maintenance_reextract._now_iso(),
            status="applied",
            workflow_options={},
            plan_digest="test",
            sample_ids=["exp-001"],
            incomplete=False,
        ).model_dump(),
    )

    pending_set_id = "rext_" + ("b" * 32)
    pending_dir = maintenance_reextract.candidate_set_path(store, pending_set_id)
    pending_mock = pending_dir / "candidates" / "exp-001" / "mock.jpg"
    pending_mock.parent.mkdir(parents=True, exist_ok=True)
    pending_mock.write_bytes(b"pending staged mock")
    maintenance_reextract._atomic_write_json(
        pending_dir / "manifest.json",
        maintenance_reextract.ReextractManifest(
            candidate_set_id=pending_set_id,
            created_at=maintenance_reextract._now_iso(),
            updated_at=maintenance_reextract._now_iso(),
            status="completed",
            workflow_options={},
            plan_digest="test",
            sample_ids=["exp-001"],
            incomplete=False,
        ).model_dump(),
    )

    dry_run = maintenance_reextract.cleanup_retired_reextract_artifacts(store, dry_run=True)

    assert dry_run["dry_run"] is True
    assert dry_run["live_mock_files"] == 1
    assert dry_run["staged_mock_files"] == 2
    assert dry_run["applied_candidate_sets"] == 1
    assert live_mock.exists()
    assert applied_dir.exists()
    assert pending_dir.exists()

    result = maintenance_reextract.cleanup_retired_reextract_artifacts(store, dry_run=False)

    assert result["errors"] == []
    assert result["deleted_live_mock_files"] == 1
    assert result["deleted_staged_mock_files"] == 2
    assert result["deleted_applied_candidate_sets"] == 1
    assert not live_mock.exists()
    assert not applied_dir.exists()
    assert pending_dir.exists()
    assert not pending_mock.exists()


def test_reextract_appearance_candidate_generation_is_staged_and_apply_updates_ct_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _accepted_store_with_model_fits(tmp_path)
    _install_fake_appearance(monkeypatch, {0: (10.0, 20.0, 30.0), 1: (40.0, 50.0, 60.0)})
    before_result = store.get_extraction_result("exp-001")
    assert before_result is not None
    before_image_status = store.get_image_source_status("img-sample")
    live_appearance = store.root / "thumbnails" / "exp-001" / "appearance.jpg"

    preflight = maintenance_reextract.preflight_reextract_sample_images(store, _scope())
    report = maintenance_reextract.generate_appearance_existing_coordinate_candidates(
        store,
        _scope(),
        preflight=preflight,
    )

    candidate_set_id = report["summary"]["candidate_set_id"]
    candidate_set_dir = maintenance_reextract.candidate_set_path(store, candidate_set_id)
    sample = maintenance_reextract.load_candidate_sample(store, candidate_set_id, "exp-001")
    assert sample["status"] == "ready_changed"
    assert Path(sample["artifacts"]["appearance"]).as_posix() == "candidates/exp-001/appearance_review.jpg"
    assert Path(sample["artifacts"]["transmission_roi"]).as_posix() == "candidates/exp-001/transmission_review.jpg"
    assert maintenance_reextract.candidate_artifact_path(store, candidate_set_id, "exp-001", "appearance").exists()
    assert maintenance_reextract.candidate_artifact_path(store, candidate_set_id, "exp-001", "transmission_roi").exists()
    assert store.get_extraction_result("exp-001") == before_result
    assert store.get_image_source_status("img-sample") == before_image_status
    assert not live_appearance.exists()

    _mark_candidate_save(store, candidate_set_id)
    apply_report = maintenance_reextract.apply_appearance_candidates(store, candidate_set_id)

    assert apply_report["candidate_set_deleted"] is True
    assert apply_report["candidate_set_cleanup_warning"] == ""
    assert not candidate_set_dir.exists()
    assert apply_report["summary"]["applied_changed"] == 1
    assert apply_report["summary"]["applied_unchanged"] == 0
    assert apply_report["summary"]["visual_artifacts_changed"] == 0
    assert not live_appearance.exists()
    after = store.get_extraction_result("exp-001")
    assert after is not None
    assert after["extraction_result_id"] == "ext-original"
    assert after["measurements"]["swatches"][0]["appearance"] == {
        "source": maintenance_reextract.APPEARANCE_SOURCE_PROVENANCE_QUAD,
        "jpeg_r": 10.0,
        "jpeg_g": 20.0,
        "jpeg_b": 30.0,
        "swatch_box": None,
    }
    assert store.get_model_fit("fit-camera_transform")["currentness_state"] == "stale"
    assert store.get_model_fit("fit-legacy_spline")["currentness_state"] == "current"
    assert store.get_model_fit("fit-photo_stack_v2")["currentness_state"] == "current"


def test_reextract_appearance_noop_does_not_persist_review_visual_or_stale_models(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = {"rawpy": "test", "pillow": "test"}
    store = _accepted_store_with_model_fits(
        tmp_path,
        appearance_source=maintenance_reextract.APPEARANCE_SOURCE_PROVENANCE_QUAD,
        appearance_flipped=False,
        appearance_order_correlation=0.95,
        decode_environment=env,
    )
    _install_fake_appearance(
        monkeypatch,
        {0: (100.0, 110.0, 120.0), 1: (101.0, 111.0, 121.0)},
        flipped=False,
        order_correlation=0.95,
        decode_environment=env,
    )

    preflight = maintenance_reextract.preflight_reextract_sample_images(store, _scope())
    report = maintenance_reextract.generate_appearance_existing_coordinate_candidates(
        store,
        _scope(),
        preflight=preflight,
    )
    candidate_set_id = report["summary"]["candidate_set_id"]
    candidate_set_dir = maintenance_reextract.candidate_set_path(store, candidate_set_id)
    sample = maintenance_reextract.load_candidate_sample(store, candidate_set_id, "exp-001")
    assert sample["status"] == "ready_unchanged"
    assert not (store.root / "thumbnails" / "exp-001" / "appearance.jpg").exists()

    _mark_candidate_save(store, candidate_set_id)
    apply_report = maintenance_reextract.apply_appearance_candidates(store, candidate_set_id)

    assert apply_report["candidate_set_deleted"] is True
    assert not candidate_set_dir.exists()
    assert apply_report["summary"]["applied_changed"] == 0
    assert apply_report["summary"]["applied_unchanged"] == 1
    assert apply_report["summary"]["visual_artifacts_changed"] == 0
    assert apply_report["summary"]["stale_model_fit_count"] == 0
    assert not (store.root / "thumbnails" / "exp-001" / "appearance.jpg").exists()
    assert store.get_model_fit("fit-camera_transform")["currentness_state"] == "current"
    assert store.get_model_fit("fit-legacy_spline")["currentness_state"] == "current"
    assert store.get_model_fit("fit-photo_stack_v2")["currentness_state"] == "current"


def test_reextract_appearance_decode_metadata_only_update_does_not_stale_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _accepted_store_with_model_fits(
        tmp_path,
        appearance_source=maintenance_reextract.APPEARANCE_SOURCE_PROVENANCE_QUAD,
        appearance_flipped=False,
        appearance_order_correlation=0.95,
        decode_environment={"rawpy": "old", "pillow": "old"},
    )
    _install_fake_appearance(
        monkeypatch,
        {0: (100.0, 110.0, 120.0), 1: (101.0, 111.0, 121.0)},
        flipped=False,
        order_correlation=0.95,
        decode_environment={"rawpy": "new", "pillow": "new"},
    )

    preflight = maintenance_reextract.preflight_reextract_sample_images(store, _scope())
    report = maintenance_reextract.generate_appearance_existing_coordinate_candidates(
        store,
        _scope(),
        preflight=preflight,
    )
    candidate_set_id = report["summary"]["candidate_set_id"]
    assert maintenance_reextract.load_candidate_sample(store, candidate_set_id, "exp-001")["status"] == "ready_unchanged"

    _mark_candidate_save(store, candidate_set_id)
    apply_report = maintenance_reextract.apply_appearance_candidates(store, candidate_set_id)

    assert apply_report["summary"]["applied_changed"] == 0
    assert apply_report["summary"]["applied_unchanged"] == 1
    assert apply_report["summary"]["stale_model_fit_count"] == 0
    assert store.get_extraction_result("exp-001")["diagnostics"]["decode_environment"] == {
        "pillow": "new",
        "rawpy": "new",
    }
    assert store.get_model_fit("fit-camera_transform")["currentness_state"] == "current"


def test_reextract_appearance_candidates_use_strip_sampling_boxes_for_colors_and_visual(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _accepted_store_with_model_fits(tmp_path)
    strip = np.full((20, 40, 3), 90, dtype=np.uint8)
    boxes = {0: (4, 5, 12, 15), 1: (20, 5, 28, 15)}
    color_calls: list[dict] = []
    visual_calls: list[dict] = []
    monkeypatch.setattr(
        maintenance_reextract,
        "_source_strip_and_sampling_boxes_from_target",
        lambda *_args, **_kwargs: (strip, boxes, {"coordinate_space": "test"}),
    )

    def fake_extraction(**kwargs):
        color_calls.append(kwargs)
        return SimpleNamespace(
            colors_by_swatch_index={
                0: np.array([10.0, 20.0, 30.0]),
                1: np.array([40.0, 50.0, 60.0]),
            },
            appearance_source=maintenance_reextract.APPEARANCE_SOURCE_PROVENANCE_QUAD,
            flipped=False,
            order_correlation=1.0,
            strip_rgb=np.full((12, 30, 3), 140, dtype=np.uint8),
            boxes_by_swatch_index=boxes,
        )

    def fake_visual(extraction):
        visual_calls.append(extraction)
        return np.full((10, 24, 3), 180, dtype=np.uint8)

    monkeypatch.setattr(maintenance_reextract, "_embedded_jpeg_extraction", fake_extraction)
    monkeypatch.setattr(maintenance_reextract, "appearance_strip_visual_from_extraction", fake_visual)
    monkeypatch.setattr(maintenance_reextract, "_decode_environment", lambda: {"rawpy": "test-reextract"})

    report = maintenance_reextract.generate_appearance_existing_coordinate_candidates(
        store,
        _scope(),
        preflight=maintenance_reextract.preflight_reextract_sample_images(store, _scope()),
    )

    candidate = maintenance_reextract.load_candidate_sample(store, report["summary"]["candidate_set_id"], "exp-001")
    assert set(candidate["artifacts"]) == {"appearance", "transmission_roi"}
    assert color_calls[0]["strip_sample_boxes"] == boxes
    assert color_calls[0]["strip_sample_shape_hw"] == strip.shape[:2]
    assert visual_calls[0].boxes_by_swatch_index == boxes


def test_reextract_appearance_generation_and_apply_endpoints_use_writer_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _accepted_store_with_model_fits(tmp_path)
    monkeypatch.setattr(server, "_store", store)
    _install_fake_appearance(monkeypatch, {0: (10.0, 20.0, 30.0), 1: (40.0, 50.0, 60.0)})
    client = TestClient(server.app, raise_server_exceptions=False)

    acquired, blocker = server._try_begin_model_fit_run(  # type: ignore[attr-defined]
        "photo_stack_v2",
        job_id="fit-lock",
        operation_id="photo_stack_v2",
    )
    assert acquired, blocker
    try:
        blocked = client.post(
            "/api/maintenance/reextract-sample-images/candidate-sets",
            json={"scope": _scope()},
        )
        assert blocked.status_code == 409, blocked.text
        assert "photo stack" in blocked.text.lower()
    finally:
        server._end_model_fit_run(kind="photo_stack_v2", job_id="fit-lock")  # type: ignore[attr-defined]

    created = client.post(
        "/api/maintenance/reextract-sample-images/candidate-sets",
        json={"scope": _scope()},
    )
    assert created.status_code == 200, created.text
    candidate_set_id = created.json()["candidate_set_id"]

    reviewed = client.post(
        f"/api/maintenance/reextract-sample-images/candidate-sets/{candidate_set_id}/samples/exp-001/review",
        json={"decision": "save"},
    )
    assert reviewed.status_code == 200, reviewed.text
    applied = client.post(f"/api/maintenance/reextract-sample-images/candidate-sets/{candidate_set_id}/apply", json={})
    assert applied.status_code == 200, applied.text
    assert applied.json()["report"]["summary"]["applied_changed"] == 1


def test_reextract_review_endpoints_block_while_reextract_job_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _accepted_store_with_model_fits(tmp_path)
    monkeypatch.setattr(server, "_store", store)
    _install_fake_appearance(monkeypatch, {0: (10.0, 20.0, 30.0), 1: (40.0, 50.0, 60.0)})
    client = TestClient(server.app, raise_server_exceptions=False)

    created = client.post(
        "/api/maintenance/reextract-sample-images/candidate-sets",
        json={"scope": _scope()},
    )
    assert created.status_code == 200, created.text
    candidate_set_id = created.json()["candidate_set_id"]
    with server._reextract_jobs_lock:  # type: ignore[attr-defined]
        server._reextract_jobs["review-lock"] = {  # type: ignore[attr-defined]
            "job_id": "review-lock",
            "status": "running",
            "candidate_set_id": candidate_set_id,
        }
    try:
        single = client.post(
            f"/api/maintenance/reextract-sample-images/candidate-sets/{candidate_set_id}/samples/exp-001/review",
            json={"decision": "save"},
        )
        bulk = client.post(
            f"/api/maintenance/reextract-sample-images/candidate-sets/{candidate_set_id}/review",
            json={"decision": "save"},
        )
    finally:
        with server._reextract_jobs_lock:  # type: ignore[attr-defined]
            server._reextract_jobs.clear()  # type: ignore[attr-defined]

    assert single.status_code == 409, single.text
    assert bulk.status_code == 409, bulk.text
    assert "re-extraction job" in single.text.lower()
    assert "re-extraction job" in bulk.text.lower()
    assert maintenance_reextract.load_candidate_sample(store, candidate_set_id, "exp-001")["review"]["decision"] == "pending"


def test_reextract_preflight_job_reports_progress_and_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path, materialize_assets=True)
    monkeypatch.setattr(server, "_store", store)
    with server._reextract_jobs_lock:  # type: ignore[attr-defined]
        server._reextract_jobs.clear()  # type: ignore[attr-defined]

    def fake_preflight(_store, scope, *, progress_cb=None, should_cancel=None, **_kwargs):
        assert scope["domain_mode"] == "appearance_only"
        assert callable(should_cancel)
        if progress_cb:
            progress_cb(
                schema="prisma-reextract-progress-v1",
                phase="preflight",
                message="Checking test targets",
                current=1,
                total=2,
                percent=50.0,
                sample_id="exp-001",
                action_label="Checking source and blank files",
                counts={"targets": 1},
                performance={"hash_misses": 2},
                elapsed_seconds=0.1,
            )
        return {"enabled": True, "summary": {"targets": 1}, "blocked": [], "warnings": []}

    monkeypatch.setattr(server, "_preflight_reextract_sample_images", fake_preflight)
    client = TestClient(server.app, raise_server_exceptions=False)

    start = client.post("/api/maintenance/reextract-sample-images/preflight/jobs", json={"scope": _scope()})
    assert start.status_code == 200, start.text
    job = _wait_reextract_job(client, start.json()["job_id"])

    assert job["status"] == "succeeded"
    assert job["result"]["preflight"]["summary"]["targets"] == 1
    assert job["progress"]["schema"] == "prisma-reextract-progress-v1"
    assert job["progress"]["percent"] == 100.0


def test_reextract_generation_job_can_be_cancelled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path, materialize_assets=True)
    monkeypatch.setattr(server, "_store", store)
    with server._reextract_jobs_lock:  # type: ignore[attr-defined]
        server._reextract_jobs.clear()  # type: ignore[attr-defined]
    candidate_set_id = "rext_" + "1" * 32

    def fake_generate(_store, scope, *, preflight=None, progress_cb=None, should_cancel=None, job_id=None, **_kwargs):
        del scope, preflight, job_id
        if progress_cb:
            progress_cb(
                schema="prisma-reextract-progress-v1",
                phase="generate_candidates",
                message="Generating test candidates",
                current=0.25,
                total=10,
                percent=2.5,
                sample_id="exp-001",
                action_label="Loading source RAW",
                candidate_set_id=candidate_set_id,
                counts={"ready_changed": 1},
                performance={"blank_raw_hits": 1},
                elapsed_seconds=0.1,
            )
        for _idx in range(200):
            if should_cancel and should_cancel():
                raise maintenance_reextract.ReextractCancelled("cancelled for test")
            time.sleep(0.005)
        return {
            "status": "completed",
            "summary": {"candidate_set_id": candidate_set_id},
            "candidate_set_id": candidate_set_id,
        }

    monkeypatch.setattr(server, "_generate_reextract_candidates", fake_generate)
    client = TestClient(server.app, raise_server_exceptions=False)

    start = client.post(
        "/api/maintenance/reextract-sample-images/candidate-sets/jobs",
        json={"scope": _scope(), "preflight": {"enabled": True, "plan_digest": "test"}},
    )
    assert start.status_code == 200, start.text
    job_id = start.json()["job_id"]
    cancel = client.post(f"/api/maintenance/reextract-sample-images/jobs/{job_id}/cancel", json={})
    assert cancel.status_code == 200, cancel.text
    job = _wait_reextract_job(client, job_id)

    assert job["status"] == "cancelled"
    assert job["candidate_set_id"] == candidate_set_id
    assert job["progress"]["candidate_set_id"] == candidate_set_id


def test_reextract_job_progress_cannot_overwrite_cancelling_and_late_completion_is_explicit() -> None:
    with server._reextract_jobs_lock:  # type: ignore[attr-defined]
        server._reextract_jobs.clear()  # type: ignore[attr-defined]
    job = server._create_reextract_job(kind="apply", candidate_set_id="rext_" + "1" * 32)  # type: ignore[attr-defined]
    job_id = job["job_id"]

    cancelled = server._cancel_reextract_job(job_id)  # type: ignore[attr-defined]
    assert cancelled["status"] == "cancelling"
    server._update_reextract_job(  # type: ignore[attr-defined]
        job_id,
        status="running",
        phase="apply",
        message="Replacing data",
        progress={"phase": "apply", "message": "Replacing data", "percent": 50},
    )
    still_cancelling = server._reextract_job_snapshot(job_id)  # type: ignore[attr-defined]
    assert still_cancelling["status"] == "cancelling"
    assert still_cancelling["progress"]["phase"] == "cancelling"
    assert still_cancelling["message"] == "Cancelling after current safe point"

    server._update_reextract_job(  # type: ignore[attr-defined]
        job_id,
        status="succeeded",
        phase="complete",
        message="Apply complete",
        progress={"phase": "complete", "message": "Apply complete", "percent": 100},
    )
    completed = server._reextract_job_snapshot(job_id)  # type: ignore[attr-defined]
    assert completed["status"] == "succeeded"
    assert completed["message"] == "Completed before cancellation took effect"
    assert completed["progress"]["phase"] == "complete"
    with server._reextract_jobs_lock:  # type: ignore[attr-defined]
        server._reextract_jobs.clear()  # type: ignore[attr-defined]


def test_reextract_candidate_review_apis_load_from_disk_after_store_reopen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _accepted_store_with_model_fits(tmp_path)
    _install_fake_appearance(monkeypatch, {0: (10.0, 20.0, 30.0), 1: (40.0, 50.0, 60.0)})
    report = maintenance_reextract.generate_appearance_existing_coordinate_candidates(
        store,
        _scope(),
        preflight=maintenance_reextract.preflight_reextract_sample_images(store, _scope()),
    )
    candidate_set_id = report["summary"]["candidate_set_id"]

    reopened_store = SQLiteDataStore(store.sqlite_path, asset_root=store.root)
    monkeypatch.setattr(server, "_store", reopened_store)
    client = TestClient(server.app, raise_server_exceptions=False)

    sets_response = client.get("/api/maintenance/reextract-sample-images/candidate-sets")
    assert sets_response.status_code == 200, sets_response.text
    assert sets_response.json()["candidate_sets"][0]["candidate_set_id"] == candidate_set_id

    set_response = client.get(f"/api/maintenance/reextract-sample-images/candidate-sets/{candidate_set_id}")
    assert set_response.status_code == 200, set_response.text
    assert set_response.json()["status"] == "completed"

    samples_response = client.get(f"/api/maintenance/reextract-sample-images/candidate-sets/{candidate_set_id}/samples")
    assert samples_response.status_code == 200, samples_response.text
    assert samples_response.json()["samples"][0]["sample_id"] == "exp-001"

    detail_response = client.get(f"/api/maintenance/reextract-sample-images/candidate-sets/{candidate_set_id}/samples/exp-001")
    assert detail_response.status_code == 200, detail_response.text
    assert detail_response.json()["review"]["decision"] == "pending"
    assert detail_response.json()["review"]["accepted"] is False

    artifact_response = client.get(
        f"/api/maintenance/reextract-sample-images/candidate-sets/{candidate_set_id}/artifacts/exp-001/appearance"
    )
    assert artifact_response.status_code == 200, artifact_response.text
    assert artifact_response.headers["content-type"].startswith("image/jpeg")
    assert artifact_response.content


def test_reextract_candidate_review_apis_reject_unsafe_or_missing_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _accepted_store_with_model_fits(tmp_path)
    _install_fake_appearance(monkeypatch, {0: (10.0, 20.0, 30.0), 1: (40.0, 50.0, 60.0)})
    report = maintenance_reextract.generate_appearance_existing_coordinate_candidates(
        store,
        _scope(),
        preflight=maintenance_reextract.preflight_reextract_sample_images(store, _scope()),
    )
    candidate_set_id = report["summary"]["candidate_set_id"]
    monkeypatch.setattr(server, "_store", store)
    client = TestClient(server.app, raise_server_exceptions=False)

    bad_id = client.get("/api/maintenance/reextract-sample-images/candidate-sets/not-a-candidate")
    assert bad_id.status_code == 400, bad_id.text

    missing_sample = client.get(
        f"/api/maintenance/reextract-sample-images/candidate-sets/{candidate_set_id}/artifacts/exp-missing/appearance"
    )
    assert missing_sample.status_code == 404, missing_sample.text

    bad_kind = client.get(
        f"/api/maintenance/reextract-sample-images/candidate-sets/{candidate_set_id}/artifacts/exp-001/candidate"
    )
    assert bad_kind.status_code == 400, bad_kind.text


def test_reextract_incomplete_candidate_set_is_recoverable_from_disk(tmp_path: Path) -> None:
    store = _store(tmp_path, materialize_assets=True)
    manifest, _set_path = maintenance_reextract.create_candidate_set(
        store,
        _scope(),
        plan_digest="digest-incomplete",
    )

    reopened_store = SQLiteDataStore(store.sqlite_path, asset_root=store.root)
    rows = maintenance_reextract.list_candidate_sets(reopened_store)

    assert rows[0]["candidate_set_id"] == manifest.candidate_set_id
    assert rows[0]["status"] == "incomplete"
    assert rows[0]["incomplete"] is True
