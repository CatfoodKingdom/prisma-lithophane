from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

import Prisma.calibration.fitting.camera_transform.export as camera_export
from Prisma.calibration.fitting.camera_transform.corpus import build_camera_transform_corpus
from Prisma.calibration.fitting.camera_transform.export import build_payload, write_camera_transform_artifact
from Prisma.calibration.fitting.camera_transform.fit import assign_validation_folds, fit_camera_transform
from Prisma.calibration.fitting.camera_transform.job import run_camera_transform_build_job
from Prisma.calibration.fitting.camera_transform.lut import bake_inverse_lut
from Prisma.lib.camera_transform import CAMERA_TRANSFORM_CURRENT, apply_forward, load_camera_transform, load_inverse_lut
from tests.calibration.support.model_artifact_fixtures import (
    camera_validation_metrics as _validation_metrics,
    identity_camera_params as _identity_params,
)


pytestmark = pytest.mark.slow




def _synthetic_df(n: int = 240) -> pd.DataFrame:
    rng = np.random.default_rng(4)
    t = rng.uniform(0.02, 0.8, size=(n, 3))
    y = apply_forward(t, _identity_params())
    df = pd.DataFrame(
        {
            "sample_id": [f"s-{i // 8:03d}" for i in range(n)],
            "swatch_index": [i % 8 for i in range(n)],
            "variable_fid": ["opaque-test"] * n,
            "nominal_thickness_mm": np.linspace(0.1, 0.9, n),
            "T_R": t[:, 0],
            "T_G": t[:, 1],
            "T_B": t[:, 2],
            "jpeg_r": np.round(y[:, 0] * 255),
            "jpeg_g": np.round(y[:, 1] * 255),
            "jpeg_b": np.round(y[:, 2] * 255),
            "fit_state": ["included"] * n,
            "order_correlation": [1.0] * n,
        }
    )
    df.loc[0, "jpeg_r"] = 255
    return df


@pytest.mark.parametrize("row_count", [200, 400])
def test_sparse_synthetic_fit_recovers_identity_and_validation_folds_are_deterministic(
    row_count: int,
) -> None:
    df = _synthetic_df(row_count)
    folds1 = assign_validation_folds(df, seed=42)
    folds2 = assign_validation_folds(df, seed=42)
    assert folds1 == folds2
    assert set(folds1.values()) == set(range(5))
    result = fit_camera_transform(df)
    assert result.metrics["validation"]["dE76_CIELAB"]["mean"] < 1.0
    assert result.metrics["final_fit"]["row_count"] == len(result.clean)
    assert result.metrics["final_fit"]["sample_count"] == row_count // 8
    assert len(result.oof_predictions) == len(result.clean)
    assert result.metrics["n_censored_from_loss"] == 1




def _payload(created_by: str, params: np.ndarray | None = None) -> dict:
    return build_payload(
        params=_identity_params() if params is None else params,
        created_by=created_by,
        metrics=_validation_metrics(),
        corpus_summary={},
    )


def _publish(root: Path, created_by: str, lut_value: float, *, hook=None, params: np.ndarray | None = None) -> None:
    write_camera_transform_artifact(
        target_dir=root,
        payload=_payload(created_by, params=params),
        lut=np.full((33, 33, 33, 3), lut_value, dtype=np.float32),
        manifest={"created_at": created_by, "validation_dE76_CIELAB": {"mean": 1.0}, "corpus": {}, "params_sha256": created_by},
        publish_hook=hook,
    )


def _active_created_by_and_lut(root: Path) -> tuple[str, float]:
    return load_camera_transform(root).payload["created_by"], float(load_inverse_lut(root)[0, 0, 0, 0])


def test_export_interleaved_reader_only_sees_old_or_new_generation(tmp_path: Path) -> None:
    root = tmp_path / "camera_transform"
    old_params = _identity_params()
    new_params = _identity_params()
    new_params[1] = 0.25
    _publish(root, "old", 0.0, params=old_params)
    observed: list[tuple[str, str, float]] = []

    def hook(op: str, _path: Path) -> None:
        created_by, lut_value = _active_created_by_and_lut(root)
        observed.append((op, created_by, lut_value))
        assert (created_by, lut_value) in {("old", 0.0), ("new", 1.0)}

    _publish(root, "new", 1.0, hook=hook, params=new_params)
    assert any(op == "replace_current" and created_by == "new" for op, created_by, _ in observed)
    assert _active_created_by_and_lut(root) == ("new", 1.0)


def test_export_pre_commit_crashes_leave_old_generation_loadable(tmp_path: Path) -> None:
    root = tmp_path / "camera_transform"
    _publish(root, "old", 0.0)
    ops: list[str] = []
    _publish(tmp_path / "probe", "probe-old", 0.0)
    _publish(tmp_path / "probe", "probe-new", 1.0, hook=lambda op, _path: ops.append(op))

    pre_commit_ops = ops[:ops.index("replace_current")]
    for op_to_fail in pre_commit_ops:
        case_root = tmp_path / f"case-{op_to_fail}"
        _publish(case_root, "old", 0.0)

        def hook(op: str, _path: Path) -> None:
            if op == op_to_fail:
                raise RuntimeError(f"crash after {op}")

        with pytest.raises(RuntimeError, match="crash after"):
            _publish(case_root, "new", 1.0, hook=hook)
        assert _active_created_by_and_lut(case_root) == ("old", 0.0)


def test_generation_commit_retries_transient_windows_rename_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "camera_transform"
    real_rename = camera_export.os.rename
    attempts = 0

    def flaky_rename(source, destination):  # type: ignore[no-untyped-def]
        nonlocal attempts
        source_path = Path(source)
        destination_path = Path(destination)
        if source_path.name.startswith(".") and destination_path.name.startswith("gen-"):
            attempts += 1
            if attempts < 3:
                error = PermissionError("simulated transient scanner lock")
                error.winerror = 5  # type: ignore[attr-defined]
                raise error
        return real_rename(source, destination)

    monkeypatch.setattr(camera_export, "CAMERA_GENERATION_RENAME_RETRY_DELAYS_SECONDS", (0.0, 0.0))
    monkeypatch.setattr(camera_export.os, "rename", flaky_rename)
    monkeypatch.setattr(camera_export, "_transient_windows_generation_rename_error", lambda exc: exc.winerror == 5)

    _publish(root, "new", 1.0)

    assert attempts == 3
    assert _active_created_by_and_lut(root) == ("new", 1.0)
    assert not list(root.glob(".*.tmp-*"))


def test_generation_rename_retry_classification_is_windows_specific(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = PermissionError("simulated Windows sharing violation")
    error.winerror = 5  # type: ignore[attr-defined]
    monkeypatch.setattr(camera_export.os, "name", "nt")
    assert camera_export._transient_windows_generation_rename_error(error) is True

    error.winerror = 123  # type: ignore[attr-defined]
    assert camera_export._transient_windows_generation_rename_error(error) is False

    error.winerror = 32  # type: ignore[attr-defined]
    monkeypatch.setattr(camera_export.os, "name", "posix")
    assert camera_export._transient_windows_generation_rename_error(error) is False


def test_generation_commit_exhausts_retry_budget_without_changing_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "camera_transform"
    _publish(root, "old", 0.0)
    old_generation = (root / CAMERA_TRANSFORM_CURRENT).read_text(encoding="utf-8").strip()
    real_rename = camera_export.os.rename
    attempts = 0

    def locked_rename(source, destination):  # type: ignore[no-untyped-def]
        nonlocal attempts
        source_path = Path(source)
        destination_path = Path(destination)
        if source_path.name.startswith(".") and destination_path.name.startswith("gen-"):
            attempts += 1
            error = PermissionError("simulated persistent scanner lock")
            error.winerror = 32  # type: ignore[attr-defined]
            raise error
        return real_rename(source, destination)

    monkeypatch.setattr(camera_export, "CAMERA_GENERATION_RENAME_RETRY_DELAYS_SECONDS", (0.0, 0.0))
    monkeypatch.setattr(camera_export.os, "rename", locked_rename)
    monkeypatch.setattr(camera_export, "_transient_windows_generation_rename_error", lambda exc: exc.winerror == 32)

    with pytest.raises(PermissionError, match="persistent scanner lock"):
        _publish(root, "new", 1.0)

    assert attempts == 3
    assert (root / CAMERA_TRANSFORM_CURRENT).read_text(encoding="utf-8").strip() == old_generation
    assert _active_created_by_and_lut(root) == ("old", 0.0)
    assert not list(root.glob(".*.tmp-*"))


def test_generation_commit_does_not_retry_non_transient_rename_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "camera_transform"
    real_rename = camera_export.os.rename
    attempts = 0

    def invalid_rename(source, destination):  # type: ignore[no-untyped-def]
        nonlocal attempts
        source_path = Path(source)
        destination_path = Path(destination)
        if source_path.name.startswith(".") and destination_path.name.startswith("gen-"):
            attempts += 1
            raise FileExistsError("simulated non-transient conflict")
        return real_rename(source, destination)

    monkeypatch.setattr(camera_export, "CAMERA_GENERATION_RENAME_RETRY_DELAYS_SECONDS", (0.0, 0.0))
    monkeypatch.setattr(camera_export.os, "rename", invalid_rename)
    monkeypatch.setattr(camera_export, "_transient_windows_generation_rename_error", lambda _exc: False)

    with pytest.raises(FileExistsError, match="non-transient conflict"):
        _publish(root, "new", 1.0)

    assert attempts == 1
    assert not list(root.glob(".*.tmp-*"))


def test_export_hash_verification_rejects_corrupt_committed_generation(tmp_path: Path) -> None:
    root = tmp_path / "camera_transform"
    _publish(root, "old", 0.0)
    generation = (root / CAMERA_TRANSFORM_CURRENT).read_text(encoding="utf-8").strip()
    with (root / generation / "inverse_lut_33.npz").open("ab") as fh:
        fh.write(b"corrupt")
    with pytest.raises(RuntimeError, match="Calibration -> Camera Transform"):
        load_inverse_lut(root)


def test_export_gc_keeps_one_generation_and_skips_locked_old_generation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "camera_transform"
    _publish(root, "one", 0.0)
    _publish(root, "two", 1.0)
    assert len([p for p in root.iterdir() if p.is_dir() and p.name.startswith("gen-")]) == 1

    _publish(root, "three", 2.0)
    old_generation = next(p for p in root.iterdir() if p.is_dir() and p.name.startswith("gen-"))
    import Prisma.calibration.fitting.camera_transform.export as export_mod

    def locked(_path):
        raise OSError("locked")

    monkeypatch.setattr(export_mod.shutil, "rmtree", locked)
    _publish(root, "four", 3.0)
    assert _active_created_by_and_lut(root) == ("four", 3.0)
    assert old_generation.exists()


def test_camera_transform_pointer_resolution_accepts_root_and_direct_file_paths(tmp_path: Path) -> None:
    root = tmp_path / "camera_transform"
    _publish(root, "old", 0.0)
    generation = (root / CAMERA_TRANSFORM_CURRENT).read_text(encoding="utf-8").strip()
    generation_dir = root / generation
    assert load_camera_transform(root).payload["created_by"] == "old"
    assert load_camera_transform(generation_dir / "camera_transform.json").payload["created_by"] == "old"
    assert float(load_inverse_lut(root)[0, 0, 0, 0]) == 0.0
    assert float(load_inverse_lut(generation_dir / "inverse_lut_33.npz")[0, 0, 0, 0]) == 0.0


def test_corpus_uses_locator_boxes_and_never_display_artifact_or_visual_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import Prisma.calibration.fitting.camera_transform.corpus as corpus_mod

    img = np.zeros((240, 420, 3), dtype=np.uint8)
    img[20:220, 20:400] = [245, 250, 252]
    strip_y0, strip_y1 = 90, 150
    strip_x0, strip_x1 = 60, 360
    expected = [
        (140, 100, 80),
        (100, 80, 60),
        (60, 50, 40),
        (20, 30, 20),
    ]
    swatch_w = (strip_x1 - strip_x0) // len(expected)
    for i, rgb in enumerate(expected):
        x0 = strip_x0 + i * swatch_w
        x1 = strip_x0 + (i + 1) * swatch_w
        img[strip_y0:strip_y1, x0:x1] = rgb
    monkeypatch.setattr(corpus_mod, "_extract_embedded_jpeg", lambda _path: img)

    sample = SimpleNamespace(
        sample_id="s-001",
        assigned_image="s-001.CR2",
        fit_exclude=False,
        filaments=SimpleNamespace(variable="opaque-test"),
        excluded_swatches=[],
        measurements=SimpleNamespace(
            swatches=[
                SimpleNamespace(
                    swatch_index=i,
                    nominal_thickness_mm=0.2 + i * 0.1,
                    R=240,
                    G=240,
                    B=240,
                    R_linear=0.9 - i * 0.2,
                    G_linear=0.9 - i * 0.2,
                    B_linear=0.9 - i * 0.2,
                    fit_state="included",
                )
                for i in range(len(expected))
            ]
        ),
    )
    no_cr2 = SimpleNamespace(
        sample_id="s-002",
        assigned_image="missing.CR2",
        fit_exclude=False,
        filaments=SimpleNamespace(variable="opaque-test"),
        excluded_swatches=[],
        measurements=sample.measurements,
    )
    skipped = SimpleNamespace(sample_id="s-003", fit_exclude=False, measurements=None)
    images = tmp_path / "images"
    images.mkdir()
    (images / "s-001.CR2").write_bytes(b"dummy cr2")
    thumb = tmp_path / "thumbnails" / "s-001"
    thumb.mkdir(parents=True)
    (thumb / "display_artifacts.json").write_text(
        json.dumps(
            {
                "exact": True,
                "source": "automatic_full_raw_visual",
                "swatches": [
                    {
                        "swatch_index": i,
                        "observed_display_rgb": [250, 250, 250],
                        "full_res_transmission_sample_rect": {"x0": 20, "x1": 40, "y0": 20, "y1": 40},
                    }
                    for i in range(len(expected))
                ],
            }
        ),
        encoding="utf-8",
    )
    store = SimpleNamespace(
        root=tmp_path,
        list_samples=lambda: [sample, no_cr2, skipped],
        list_filaments=lambda: [
            SimpleNamespace(
                filament_id="opaque-test",
                exclude_from_model=False,
            )
        ],
    )
    corpus = build_camera_transform_corpus(store)
    assert len(corpus.rows) == len(expected)
    actual = [
        tuple(float(corpus.rows.iloc[i][ch]) for ch in ("jpeg_r", "jpeg_g", "jpeg_b"))
        for i in range(len(expected))
    ]
    assert actual == [(float(r), float(g), float(b)) for r, g, b in expected]
    assert set(corpus.rows["appearance_source"]) == {"embedded_jpeg/located_strip_boxes"}
    assert set(corpus.rows["order_correlation"]) == {1.0}
    assert corpus.summary["appearance_sources"] == {"embedded_jpeg/located_strip_boxes": len(expected)}
    assert corpus.summary["usable_swatch_count"] == len(expected)
    assert [item["reason"] for item in corpus.skipped_samples] == [
        "no CR2 for embedded JPEG",
        "missing processed measurements",
    ]


def test_job_warning_and_bad_fit_gates_do_not_overwrite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import Prisma.calibration.fitting.camera_transform.job as job_mod

    small = SimpleNamespace(summary={"usable_swatch_count": 10}, rows=_synthetic_df(40), skipped_samples=[], source_fingerprint={})
    monkeypatch.setattr(job_mod, "build_camera_transform_corpus_from_extraction_results", lambda *_args, **_kwargs: small)
    monkeypatch.setattr(
        job_mod,
        "fit_camera_transform",
        lambda _rows: SimpleNamespace(
            params=_identity_params(),
            metrics=_validation_metrics(1.0),
            hygiene={"start": 32, "kept": 32},
        ),
    )
    monkeypatch.setattr(job_mod, "bake_inverse_lut", lambda _params: np.zeros((33, 33, 33, 3), dtype=np.float32))
    result = run_camera_transform_build_job(store=SimpleNamespace(root=tmp_path), output_dir=tmp_path / "camera_transform")
    assert result["status"] == "warning"
    assert load_camera_transform(tmp_path / "camera_transform").payload["created_by"] == "calibration_webapp"

    old_current = (tmp_path / "camera_transform" / CAMERA_TRANSFORM_CURRENT).read_text(encoding="utf-8")
    monkeypatch.setattr(
        job_mod,
        "fit_camera_transform",
        lambda _rows: SimpleNamespace(
            params=_identity_params(),
            metrics=_validation_metrics(99.0),
            hygiene={"start": 32, "kept": 32},
        ),
    )
    with pytest.raises(RuntimeError, match="existing artifact was not modified"):
        run_camera_transform_build_job(store=SimpleNamespace(root=tmp_path), output_dir=tmp_path / "camera_transform")
    assert (tmp_path / "camera_transform" / CAMERA_TRANSFORM_CURRENT).read_text(encoding="utf-8") == old_current


def test_job_uses_stored_corpus_builder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Step 3 Phase 6: after the switch the CT build job reads stored sidecar
    # appearance and must NOT call the legacy fit-time extractor (doc-30 §7.4).
    import Prisma.calibration.fitting.camera_transform.job as job_mod

    small = SimpleNamespace(summary={"usable_swatch_count": 10}, rows=_synthetic_df(40),
                            skipped_samples=[], source_fingerprint={})

    def _legacy_must_not_run(*_a, **_k):
        raise AssertionError("CT job must use the stored-sidecar corpus builder, not legacy extraction")

    monkeypatch.setattr(job_mod, "build_camera_transform_corpus", _legacy_must_not_run)
    monkeypatch.setattr(job_mod, "build_camera_transform_corpus_from_extraction_results",
                        lambda *_a, **_k: small, raising=False)
    monkeypatch.setattr(job_mod, "fit_camera_transform", lambda _rows: SimpleNamespace(
        params=_identity_params(), metrics=_validation_metrics(1.0),
        hygiene={"start": 40, "kept": 40}))
    monkeypatch.setattr(job_mod, "bake_inverse_lut", lambda _p: np.zeros((33, 33, 33, 3), dtype=np.float32))

    # Completes via the stored builder (the legacy extractor would have raised);
    # "warning" because the 10-swatch stub is below the min-coverage gate.
    result = run_camera_transform_build_job(store=SimpleNamespace(root=tmp_path),
                                            output_dir=tmp_path / "camera_transform")
    assert result["status"] == "warning"
    assert load_camera_transform(tmp_path / "camera_transform").payload["created_by"] == "calibration_webapp"


def test_calibration_camera_transform_endpoints(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import Prisma.calibration.server as server

    store = SimpleNamespace(root=tmp_path)
    monkeypatch.setattr(server, "get_store", lambda: store)
    client = TestClient(server.app)
    current = client.get("/api/camera-transform/current")
    assert current.status_code == 200
    assert current.json()["status"] == "missing"

    def instant(job_id: str) -> None:
        server._update_camera_transform_job(job_id, status="completed", result={"ok": True})

    monkeypatch.setattr(server, "_run_camera_transform_job", instant)
    started = client.post("/api/camera-transform/build")
    assert started.status_code == 200
    job_id = started.json()["job_id"]
    status = client.get(f"/api/camera-transform/status/{job_id}")
    assert status.status_code == 200
