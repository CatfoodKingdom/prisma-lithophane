from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CALIBRATION_DIR = REPO_ROOT / "Prisma" / "calibration"
if str(CALIBRATION_DIR) not in sys.path:
    sys.path.insert(0, str(CALIBRATION_DIR))

from models import ExtractionDiagnostics, ProcessingResult
from processing.extraction_result import commit_extraction_result
from tests.tools.test_image_materialization import build_final_sqlite
from tests.tools.test_sqlite_runtime_store import (
    evidence_binding_for_sample,
    measurements_for_sample,
    provenance,
)
from tools.migration_preflight.extraction_regeneration_rehearsal import (
    REPORT_NAME,
    _cache_blank_image_loads,
    main as rehearsal_main,
    processor_module,
    run_extraction_regeneration_rehearsal,
)
from tools.migration_preflight.materialize_image_assets import materialize_image_assets


def build_rehearsal_inputs(tmp_path: Path) -> tuple[Path, Path]:
    sqlite_path, report_dir, _report = build_final_sqlite(tmp_path)
    materialized_root = tmp_path / ".codex-work" / "materialized"
    result = materialize_image_assets(
        sqlite_path=sqlite_path,
        target_root=materialized_root,
        image_custody_map_path=report_dir / "image_custody_map.json",
    )
    assert result["status"] == "pass"
    return sqlite_path, materialized_root


def fake_success_processor(sample, image_path, blank_path, orientation_rots, store) -> ProcessingResult:
    measurements = measurements_for_sample(sample)
    commit_extraction_result(
        store=store,
        sample=sample,
        measurements=measurements,
        method="automatic",
        method_provenance=provenance(),
        evidence_binding=evidence_binding_for_sample(sample),
        diagnostics=ExtractionDiagnostics(
            confidence=0.9,
            detection_strategy="fake_rehearsal",
            contour_found=True,
        ),
        next_processing_status="processed",
        next_flag_reason=None,
        cr2_path=None,
    )
    return ProcessingResult(
        sample_id=sample.sample_id,
        status="success",
        measurements=measurements,
    )


def fake_success_with_warning(sample, image_path, blank_path, orientation_rots, store) -> ProcessingResult:
    print("  WARNING: synthetic processor warning")
    return fake_success_processor(sample, image_path, blank_path, orientation_rots, store)


def fake_success_without_write(sample, image_path, blank_path, orientation_rots, store) -> ProcessingResult:
    return ProcessingResult(
        sample_id=sample.sample_id,
        status="success",
        measurements=measurements_for_sample(sample),
    )


def fake_failed_processor(sample, image_path, blank_path, orientation_rots, store) -> ProcessingResult:
    return ProcessingResult(
        sample_id=sample.sample_id,
        status="failed_detection",
        error_detail="synthetic failure",
    )


def extraction_result_count(sqlite_path: Path) -> int:
    conn = sqlite3.connect(sqlite_path)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM extraction_results").fetchone()[0])
    finally:
        conn.close()


def extraction_review_state(sqlite_path: Path, sample_id: str) -> str | None:
    conn = sqlite3.connect(sqlite_path)
    try:
        row = conn.execute(
            "SELECT review_state FROM extraction_results WHERE sample_id = ?",
            (sample_id,),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def test_extraction_regeneration_rehearsal_writes_only_copied_sqlite(tmp_path: Path) -> None:
    sqlite_path, materialized_root = build_rehearsal_inputs(tmp_path)
    output_dir = tmp_path / ".codex-work" / "regen"

    report = run_extraction_regeneration_rehearsal(
        sqlite_path=sqlite_path,
        materialized_root=materialized_root,
        output_dir=output_dir,
        sample_ids=["exp-001"],
        processor=fake_success_processor,
    )

    output_sqlite = output_dir / "extraction_regeneration.sqlite"
    assert report["status"] == "pass"
    assert report["summary"]["success_count"] == 1
    assert report["summary"]["extraction_result_count"] == 1
    assert extraction_result_count(sqlite_path) == 0
    assert extraction_result_count(output_sqlite) == 1
    assert extraction_review_state(output_sqlite, "exp-001") == "pending_review"
    assert report["image_load_cache"]["enabled"] is True
    assert report["image_load_cache"]["blank_path_count"] == 1
    assert report["image_load_cache"]["raw_cache_max_entries"] == 16
    saved_report = json.loads((output_dir / REPORT_NAME).read_text(encoding="utf-8"))
    assert saved_report["summary"]["success_count"] == 1
    assert saved_report["image_load_cache"]["blank_path_count"] == 1


def test_blank_raw_cache_returns_fresh_arrays(monkeypatch, tmp_path: Path) -> None:
    blank_path = tmp_path / "blank.CR2"
    blank_path.write_bytes(b"placeholder")
    calls = {"raw": 0}

    def fake_raw_both(_path):
        calls["raw"] += 1
        return np.array([[1.0]], dtype=np.float32), np.array([[2.0]], dtype=np.float32)

    monkeypatch.setattr(processor_module, "load_raw_both", fake_raw_both)

    with _cache_blank_image_loads({blank_path}, raw_cache_size=16) as stats:
        first_visual, first_linear = processor_module.load_raw_both(blank_path)
        first_visual[0, 0] = 99.0
        first_linear[0, 0] = 88.0
        second_visual, second_linear = processor_module.load_raw_both(blank_path)

    assert calls["raw"] == 1
    assert stats["raw_misses"] == 1
    assert stats["raw_hits"] == 1
    assert second_visual[0, 0] == 1.0
    assert second_linear[0, 0] == 2.0


def test_extraction_regeneration_rehearsal_attributes_processor_warnings(tmp_path: Path) -> None:
    sqlite_path, materialized_root = build_rehearsal_inputs(tmp_path)
    output_dir = tmp_path / ".codex-work" / "regen"

    report = run_extraction_regeneration_rehearsal(
        sqlite_path=sqlite_path,
        materialized_root=materialized_root,
        output_dir=output_dir,
        sample_ids=["exp-001"],
        processor=fake_success_with_warning,
    )

    assert report["status"] == "pass"
    assert report["summary"]["processor_warning_count"] == 1
    assert report["summary"]["processor_warning_sample_ids"] == ["exp-001"]
    assert report["samples"][0]["processor_output_warnings"] == [
        "WARNING: synthetic processor warning"
    ]


def test_extraction_regeneration_rehearsal_can_accept_successes(tmp_path: Path) -> None:
    sqlite_path, materialized_root = build_rehearsal_inputs(tmp_path)
    output_dir = tmp_path / ".codex-work" / "regen"

    report = run_extraction_regeneration_rehearsal(
        sqlite_path=sqlite_path,
        materialized_root=materialized_root,
        output_dir=output_dir,
        sample_ids=["exp-001"],
        accept_success=True,
        processor=fake_success_processor,
    )

    assert report["samples"][0]["accepted_by_runner"] is True
    assert report["samples"][0]["review_state"] == "accepted"
    assert extraction_review_state(output_dir / "extraction_regeneration.sqlite", "exp-001") == "accepted"


def test_extraction_regeneration_rehearsal_reports_missing_sample(tmp_path: Path) -> None:
    sqlite_path, materialized_root = build_rehearsal_inputs(tmp_path)
    output_dir = tmp_path / ".codex-work" / "regen"

    report = run_extraction_regeneration_rehearsal(
        sqlite_path=sqlite_path,
        materialized_root=materialized_root,
        output_dir=output_dir,
        sample_ids=["exp-missing"],
        processor=fake_success_processor,
    )

    assert report["status"] == "partial"
    assert report["summary"]["selection_error_count"] == 1
    assert report["selection_errors"][0]["reason"] == "sample_not_found"


def test_extraction_regeneration_rehearsal_fails_success_without_extraction_row(tmp_path: Path) -> None:
    sqlite_path, materialized_root = build_rehearsal_inputs(tmp_path)
    output_dir = tmp_path / ".codex-work" / "regen"

    report = run_extraction_regeneration_rehearsal(
        sqlite_path=sqlite_path,
        materialized_root=materialized_root,
        output_dir=output_dir,
        sample_ids=["exp-001"],
        processor=fake_success_without_write,
    )

    assert report["status"] == "partial"
    assert report["summary"]["failure_count"] == 1
    assert report["samples"][0]["failure_stage"] == "missing_extraction_result_after_success"


def test_extraction_regeneration_rehearsal_discards_prior_rows_before_regeneration(tmp_path: Path) -> None:
    sqlite_path, materialized_root = build_rehearsal_inputs(tmp_path)
    seed_output = tmp_path / ".codex-work" / "seed"
    seeded = run_extraction_regeneration_rehearsal(
        sqlite_path=sqlite_path,
        materialized_root=materialized_root,
        output_dir=seed_output,
        sample_ids=["exp-001"],
        processor=fake_success_processor,
    )
    assert seeded["summary"]["extraction_result_count"] == 1

    output_dir = tmp_path / ".codex-work" / "regen"
    report = run_extraction_regeneration_rehearsal(
        sqlite_path=seed_output / "extraction_regeneration.sqlite",
        materialized_root=materialized_root,
        output_dir=output_dir,
        sample_ids=["exp-001"],
        processor=fake_failed_processor,
    )

    assert report["status"] == "partial"
    assert report["samples"][0]["prior_extraction_result_id"] is not None
    assert report["samples"][0]["extraction_result_id"] is None
    assert report["summary"]["extraction_result_count"] == 0
    assert extraction_result_count(output_dir / "extraction_regeneration.sqlite") == 0


def test_extraction_regeneration_rehearsal_refuses_existing_output_without_overwrite(tmp_path: Path) -> None:
    sqlite_path, materialized_root = build_rehearsal_inputs(tmp_path)
    output_dir = tmp_path / ".codex-work" / "regen"
    run_extraction_regeneration_rehearsal(
        sqlite_path=sqlite_path,
        materialized_root=materialized_root,
        output_dir=output_dir,
        sample_ids=["exp-001"],
        processor=fake_success_processor,
    )

    with pytest.raises(FileExistsError):
        run_extraction_regeneration_rehearsal(
            sqlite_path=sqlite_path,
            materialized_root=materialized_root,
            output_dir=output_dir,
            sample_ids=["exp-001"],
            processor=fake_success_processor,
        )


def test_extraction_regeneration_rehearsal_overwrite_clears_runtime_root(tmp_path: Path) -> None:
    sqlite_path, materialized_root = build_rehearsal_inputs(tmp_path)
    output_dir = tmp_path / ".codex-work" / "regen"
    run_extraction_regeneration_rehearsal(
        sqlite_path=sqlite_path,
        materialized_root=materialized_root,
        output_dir=output_dir,
        sample_ids=["exp-001"],
        processor=fake_success_processor,
    )
    stale_marker = output_dir / "runtime_store" / "images" / "stale.CR2"
    stale_marker.write_bytes(b"stale")

    run_extraction_regeneration_rehearsal(
        sqlite_path=sqlite_path,
        materialized_root=materialized_root,
        output_dir=output_dir,
        sample_ids=["exp-001"],
        overwrite=True,
        processor=fake_success_processor,
    )

    assert not stale_marker.exists()


def test_extraction_regeneration_rehearsal_refuses_source_as_output(tmp_path: Path) -> None:
    sqlite_path, materialized_root = build_rehearsal_inputs(tmp_path)
    same_name_source = tmp_path / ".codex-work" / "same-file" / "extraction_regeneration.sqlite"
    same_name_source.parent.mkdir(parents=True, exist_ok=True)
    same_name_source.write_bytes(sqlite_path.read_bytes())

    with pytest.raises(ValueError, match="must differ"):
        run_extraction_regeneration_rehearsal(
            sqlite_path=same_name_source,
            materialized_root=materialized_root,
            output_dir=same_name_source.parent,
            sample_ids=["exp-001"],
            processor=fake_success_processor,
        )


def test_extraction_regeneration_rehearsal_refuses_unscoped_full_run(tmp_path: Path) -> None:
    sqlite_path, materialized_root = build_rehearsal_inputs(tmp_path)

    with pytest.raises(ValueError, match="explicit scope"):
        run_extraction_regeneration_rehearsal(
            sqlite_path=sqlite_path,
            materialized_root=materialized_root,
            output_dir=tmp_path / ".codex-work" / "regen",
            processor=fake_success_processor,
        )


def test_extraction_regeneration_rehearsal_all_complete_evidence_is_explicit(tmp_path: Path) -> None:
    sqlite_path, materialized_root = build_rehearsal_inputs(tmp_path)
    output_dir = tmp_path / ".codex-work" / "regen"

    report = run_extraction_regeneration_rehearsal(
        sqlite_path=sqlite_path,
        materialized_root=materialized_root,
        output_dir=output_dir,
        all_complete_evidence=True,
        processor=fake_success_processor,
    )

    assert report["status"] == "pass"
    assert report["all_complete_evidence"] is True
    assert report["summary"]["selected_count"] == 1


def test_extraction_regeneration_rehearsal_cli_smoke_with_no_selected_samples(tmp_path: Path) -> None:
    sqlite_path, materialized_root = build_rehearsal_inputs(tmp_path)
    output_dir = tmp_path / ".codex-work" / "regen"

    exit_code = rehearsal_main(
        [
            "--sqlite-path",
            str(sqlite_path),
            "--materialized-root",
            str(materialized_root),
            "--output-dir",
            str(output_dir),
            "--limit",
            "0",
        ]
    )

    assert exit_code == 0
    report = json.loads((output_dir / REPORT_NAME).read_text(encoding="utf-8"))
    assert report["summary"]["selected_count"] == 0
