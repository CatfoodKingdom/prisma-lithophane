from __future__ import annotations

import json
from pathlib import Path

from tests.tools.test_sqlite_runtime_store import build_store, extraction_payload
from tools.migration_preflight.model_payload_rehearsal import (
    REPORT_NAME,
    _ct_diagnostic_gaps,
    run_model_payload_rehearsal,
)


def seed_processed_accepted_result(tmp_path: Path):
    store = build_store(tmp_path)
    sample = store.get_sample("exp-001")
    payload = extraction_payload(store)
    for idx, swatch in enumerate(payload["measurements"]["swatches"]):
        swatch["appearance"] = {
            "source": "embedded_jpeg",
            "jpeg_r": 100.0 + idx,
            "jpeg_g": 110.0 + idx,
            "jpeg_b": 120.0 + idx,
            "swatch_box": {"x0": idx, "y0": idx + 1, "x1": idx + 2, "y1": idx + 3},
        }
    store.save_extraction_result("exp-001", payload)
    store.set_extraction_review_state("exp-001", "accepted")
    sample = store.get_sample("exp-001")
    sample.processing_status = "processed"
    store.save_sample(sample)
    return store


def test_model_payload_rehearsal_builds_payloads_from_accepted_results(tmp_path: Path) -> None:
    store = seed_processed_accepted_result(tmp_path)

    output_dir = tmp_path / ".codex-work" / "payloads"
    report = run_model_payload_rehearsal(
        sqlite_path=store.sqlite_path,
        materialized_root=store.materialized_root,
        output_dir=output_dir,
        probe_pending_review=False,
    )

    assert report["status"] == "pass"
    assert report["extraction_review_state_counts"] == {"accepted": 1}
    assert report["payloads"]["camera_transform"]["status"] == "pass"
    assert report["payloads"]["camera_transform"]["row_count"] == 2
    assert report["payloads"]["photo_stack_v2"]["status"] == "pass"
    assert report["payloads"]["photo_stack_v2"]["swatch_count"] == 2
    assert report["payloads"]["legacy_spline"]["status"] == "pass"
    assert report["payloads"]["legacy_spline"]["strip_count"] == 1

    saved = json.loads((output_dir / REPORT_NAME).read_text(encoding="utf-8"))
    assert saved["status"] == "pass"


def test_ct_diagnostic_gap_allows_explicit_undefined_order_correlation(tmp_path: Path) -> None:
    store = seed_processed_accepted_result(tmp_path)
    payload = store.get_extraction_result("exp-001")
    assert payload is not None
    payload["diagnostics"]["appearance_order_correlation"] = None
    payload["diagnostics"]["appearance_order_correlation_state"] = "nan"
    payload["diagnostics"]["appearance_orientation_flipped"] = False
    store.save_extraction_result("exp-001", payload)

    gaps = _ct_diagnostic_gaps(store.sqlite_path)

    assert gaps == {"count": 0, "examples": []}


def test_model_payload_rehearsal_blocks_pending_review_payload_leak(tmp_path: Path) -> None:
    store = seed_processed_accepted_result(tmp_path)

    report = run_model_payload_rehearsal(
        sqlite_path=store.sqlite_path,
        materialized_root=store.materialized_root,
        output_dir=tmp_path / ".codex-work" / "payloads",
        probe_pending_review=True,
    )

    assert report["status"] == "pass"
    assert report["failures"] == []
    probe = report["pending_review_probe"]
    assert probe["status"] == "pass"
    assert probe["probe_scope"] == "all_results_pending_review"
    assert probe["sample_review_accepted"] is False
    assert probe["sample_measurements_projected"] is False
    assert probe["adapters"]["camera_transform"]["contains_sample"] is False
    assert probe["adapters"]["photo_stack_v2"]["contains_sample"] is False
    assert probe["adapters"]["legacy_spline"]["contains_sample"] is False
