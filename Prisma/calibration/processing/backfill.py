"""
backfill.py — Step 3 legacy extraction_result backfill (doc-30 §6).

One-shot migration: populate per-sample extraction_result sidecars (incl. the CT
appearance bucket) for legacy samples that already have ``Sample.measurements``,
so the historical library has sidecars before the Camera Transform switches to
reading stored appearance. Backfill writes SIDECARS ONLY — it never re-extracts
transmission and never mutates ``Sample.measurements`` (doc-30 §6.1/§6.2).

Phase 2 here is the read-only dry-run INVENTORY (classification, no writes). The
apply path lands in a later phase. Eligibility is MEASUREMENT-based, not
status-based: a measurement-bearing sample is in scope regardless of
``processing_status`` because the legacy CT corpus gates on measurements
(corpus.py:211-215), not status.
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_CAL_DIR = _Path(__file__).resolve().parent.parent
if str(_CAL_DIR) not in _sys.path:
    _sys.path.insert(0, str(_CAL_DIR))

import json
from datetime import datetime, timezone

from models import ExtractionResult, EvidenceBinding, ExtractionDiagnostics
from processing.extraction_result import build_extraction_result, _decode_environment
from processing.processor import _resolve_cr2

# Classification labels (doc-30 Phase 2).
NO_MEASUREMENTS = "no_measurements"
HAS_MEASUREMENTS_NO_SIDECAR = "has_measurements_no_sidecar"
SIDECAR_COMPLETE_APPEARANCE = "sidecar_complete_appearance"
SIDECAR_APPEARANCE_FAILURE = "sidecar_appearance_failure"
SIDECAR_INCOMPLETE_NO_REASON = "sidecar_incomplete_no_reason"
MALFORMED_SIDECAR = "malformed_sidecar"

# Classes the apply step will act on (create a fresh sidecar). The others are
# reported, never silently repaired (doc-30 §6.6).
BACKFILL_ELIGIBLE = frozenset({HAS_MEASUREMENTS_NO_SIDECAR})


def classify_backfill_sample(store, sample) -> dict:
    """Classify one sample for backfill (read-only).

    Measurement presence is checked FIRST; status/review are metadata, never a
    gate. A malformed or incomplete sidecar is reported, not repaired.
    """
    sid = sample.sample_id
    has_meas = sample.measurements is not None and bool(sample.measurements.swatches)
    if not has_meas:
        return {"sample_id": sid, "classification": NO_MEASUREMENTS}

    raw = store.get_extraction_result(sid)
    if raw is None:
        return {"sample_id": sid, "classification": HAS_MEASUREMENTS_NO_SIDECAR}

    try:
        result = ExtractionResult(**raw)
    except Exception as exc:  # validation/parse failure — report, do not repair
        return {"sample_id": sid, "classification": MALFORMED_SIDECAR,
                "detail": f"{type(exc).__name__}: {exc}"}

    swatches = result.measurements.swatches
    all_appearance = bool(swatches) and all(sw.appearance is not None for sw in swatches)
    any_appearance = any(sw.appearance is not None for sw in swatches)
    appearance_error = result.diagnostics.appearance_error if result.diagnostics else None

    if all_appearance:
        return {"sample_id": sid, "classification": SIDECAR_COMPLETE_APPEARANCE}
    if not any_appearance and appearance_error:
        return {"sample_id": sid, "classification": SIDECAR_APPEARANCE_FAILURE,
                "appearance_error": appearance_error}
    # appearance partial, or missing with no recorded reason — an invariant
    # violation vs Step 2's all-or-nothing appearance policy. Report it.
    return {"sample_id": sid, "classification": SIDECAR_INCOMPLETE_NO_REASON}


def backfill_inventory(store) -> dict:
    """Dry-run inventory of every sample in store order (NO writes, doc-30 Phase 2)."""
    samples = list(store.list_samples())
    classifications = [classify_backfill_sample(store, s) for s in samples]
    counts: dict[str, int] = {}
    for c in classifications:
        counts[c["classification"]] = counts.get(c["classification"], 0) + 1
    return {
        "total": len(classifications),
        "counts": counts,
        "samples": classifications,
    }


def apply_backfill_sample(store, sample, *, force: bool = False) -> dict:
    """Write a ``legacy_backfill`` sidecar from existing measurements (doc-30 §6).

    Idempotent: a sample that is not ``has_measurements_no_sidecar`` is skipped
    unless ``force`` (a forced rewrite is a debug/refresh op, never automatic —
    doc-30 §6.6). Appearance is resolved from ``sample.assigned_image`` images-first
    so ``cr2_source`` and the appearance medians match what the legacy CT corpus
    computes (corpus.py:216/259). NEVER mutates ``Sample.measurements`` (§6.2).
    """
    sid = sample.sample_id
    classification = classify_backfill_sample(store, sample)["classification"]
    if classification == NO_MEASUREMENTS:
        return {"sample_id": sid, "action": "skipped", "reason": NO_MEASUREMENTS}
    if classification not in BACKFILL_ELIGIBLE and not force:
        # existing sidecar (complete/failure/incomplete/malformed) — do not repair
        return {"sample_id": sid, "action": "skipped", "reason": classification}

    measurements = sample.measurements
    # Resolve CR2 the same way the legacy CT corpus does (from assigned_image),
    # so cr2_source and appearance medians are parity-identical (doc-30 §5.4).
    cr2_path, cr2_source = _resolve_cr2(store, sample.assigned_image)
    result = build_extraction_result(
        sample=sample,
        method="legacy_backfill",          # D1 — unrecoverable legacy route
        measurements=measurements,
        method_provenance=None,            # D1 — no truthful provenance to claim
        evidence_binding=EvidenceBinding(
            sample_image_asset_id=sample.assigned_image,
            blank_id=sample.assigned_blank_id,
            orientation_rots=sample.orientation_rots,
            source_image=measurements.source_image,
            cr2_source=cr2_source,
        ),
        diagnostics=ExtractionDiagnostics(),
        cr2_path=cr2_path,
        store=store,
    )
    store.save_extraction_result(sid, result.model_dump())
    # Review-state two-step (§6.3): the builder hardcodes pending_review.
    if sample.review_accepted:
        store.set_extraction_review_state(sid, "accepted")
    return {
        "sample_id": sid, "action": "written",
        "cr2_source": cr2_source,
        "appearance_error": result.diagnostics.appearance_error,
        "forced": bool(force and classification not in BACKFILL_ELIGIBLE),
    }


def run_backfill(store, *, apply: bool = False, force: bool = False) -> dict:
    """Run the backfill in dry-run (default) or apply mode (doc-30 §6.7).

    Dry-run is the default and performs NO writes. Apply writes sidecars for
    eligible samples and returns an audit report (action + cr2_source +
    appearance_error counts).
    """
    samples = list(store.list_samples())
    measurement_bearing = sum(
        1 for s in samples
        if s.measurements is not None and bool(s.measurements.swatches)
    )

    if not apply:
        inv = backfill_inventory(store)
        return {"mode": "dry_run", "measurement_bearing_count": measurement_bearing, **inv}

    results = [apply_backfill_sample(store, s, force=force) for s in samples]
    actions: dict[str, int] = {}
    cr2_sources: dict[str, int] = {}
    appearance_errors: dict[str, int] = {}
    for r in results:
        actions[r["action"]] = actions.get(r["action"], 0) + 1
        if r["action"] == "written":
            cr2_sources[str(r.get("cr2_source"))] = cr2_sources.get(str(r.get("cr2_source")), 0) + 1
            err = r.get("appearance_error")
            if err:
                appearance_errors[err] = appearance_errors.get(err, 0) + 1
    return {
        "mode": "apply",
        "total": len(results),
        "measurement_bearing_count": measurement_bearing,
        "actions": actions,
        "cr2_source_counts": cr2_sources,
        "appearance_error_counts": appearance_errors,
        "results": results,
    }


# ── Durable report + CLI (doc-30 §6.9, D3) ───────────────────────────────────

def write_backfill_report(report: dict, *, report_dir) -> "Path":
    """Write a timestamped, durable backfill report (doc-30 §6.9).

    Stamps the run time + decode_environment (un-backfillable provenance) onto
    the run report and writes it under ``report_dir`` (default at the CLI:
    ``<data-root>/calibration_backfill_reports/``, NOT a model-artifact path — D3).
    """
    report_dir = _Path(report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    enriched = {
        "generated_at": now.isoformat(),
        "decode_environment": _decode_environment(),
        **report,
    }
    path = report_dir / f"backfill-{now.strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(json.dumps(enriched, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def main(argv=None) -> int:
    """CLI entry point for the one-shot CT appearance backfill (D3: CLI-first).

    Dry-run is the default; ``--apply`` writes sidecars. Always emits a durable
    report. Example:
        python -m processing.backfill --data-root Prisma/data --apply
    """
    import argparse
    from data_access import DataStore

    parser = argparse.ArgumentParser(description="Step 3 Camera Transform appearance backfill (doc-30 §6).")
    parser.add_argument("--data-root", required=True, help="path to the Prisma/data root")
    parser.add_argument("--apply", action="store_true", help="write sidecars (default: dry-run inventory, no writes)")
    parser.add_argument("--force", action="store_true", help="rewrite existing/failed sidecars (debug/refresh only)")
    parser.add_argument("--report-dir", default=None,
                        help="durable report directory (default: <data-root>/calibration_backfill_reports)")
    args = parser.parse_args(argv)

    store = DataStore(_Path(args.data_root))
    report = run_backfill(store, apply=args.apply, force=args.force)
    report_dir = _Path(args.report_dir) if args.report_dir else _Path(args.data_root) / "calibration_backfill_reports"
    path = write_backfill_report(report, report_dir=report_dir)

    print(f"mode={report['mode']} total={report['total']} "
          f"measurement_bearing={report.get('measurement_bearing_count')}")
    if report["mode"] == "apply":
        print(f"actions={report['actions']} cr2_source={report['cr2_source_counts']} "
              f"appearance_errors={report['appearance_error_counts']}")
    else:
        print(f"counts={report['counts']}")
    print(f"report written: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
