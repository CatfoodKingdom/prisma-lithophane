"""Robust Camera Transform fit-input fingerprinting.

The Camera Transform rebuild decision is based on every final-fit row and the
deterministic sample-grouped validation policy, not just extraction-result ids or
sample counts.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any

try:
    from Prisma.lib.camera_transform import (
        CAMERA_TRANSFORM_SCHEMA,
        MIN_DELTA,
        N_KNOTS,
        OUT_MAX,
    )
except ImportError:  # app context: uvicorn puts Prisma/ on sys.path
    from lib.camera_transform import (
        CAMERA_TRANSFORM_SCHEMA,
        MIN_DELTA,
        N_KNOTS,
        OUT_MAX,
    )

from .corpus import CameraTransformCorpus, build_camera_transform_corpus_from_extraction_results
from .fit import (
    CV_FOLDS,
    CV_POLICY,
    JPEG_CLIP_THRESHOLD,
    SEED,
    assign_validation_folds,
    load_and_filter,
)


CT_FINGERPRINT_SCHEMA_VERSION = 3
CT_FINGERPRINT_KIND = "camera_transform_fit_input"
CT_HYGIENE_RULES_VERSION = "load_and_filter_v1"


@dataclass(frozen=True)
class CameraTransformFingerprintResult:
    fingerprint: dict[str, Any]
    fit_input_hash: str
    raw_corpus_hash: str
    diagnostic_hash: str
    validation_hash: str
    counts: dict[str, int]
    corpus_summary: dict[str, Any]
    hygiene_drop_counts: dict[str, int]


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha256_payload(value: Any) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


def _canon_float(value: Any) -> str:
    return format(float(value), ".17g")


def _clean_fit_rows(clean: Any, fold_by_sample: dict[str, int]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if clean is None or len(clean) == 0:
        return rows
    for row in clean.to_dict("records"):
        key = (str(row.get("sample_id")), int(row.get("swatch_index")))
        rows.append(
            {
                "sample_id": key[0],
                "swatch_index": key[1],
                "validation_fold": fold_by_sample.get(key[0]),
                "T_R": _canon_float(row.get("T_R")),
                "T_G": _canon_float(row.get("T_G")),
                "T_B": _canon_float(row.get("T_B")),
                "jpeg_r": _canon_float(row.get("jpeg_r")),
                "jpeg_g": _canon_float(row.get("jpeg_g")),
                "jpeg_b": _canon_float(row.get("jpeg_b")),
                "_censored": bool(row.get("_censored", False)),
            }
        )
    return rows


def _raw_corpus_rows(rows_df: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if rows_df is None or len(rows_df) == 0:
        return rows
    for row in rows_df.itertuples(index=False):
        rows.append(
            {
                "sample_id": str(getattr(row, "sample_id")),
                "swatch_index": int(getattr(row, "swatch_index")),
                "variable_fid": str(getattr(row, "variable_fid", "") or ""),
                "nominal_thickness_mm": _canon_float(getattr(row, "nominal_thickness_mm")),
                "T_R": _canon_float(getattr(row, "T_R")),
                "T_G": _canon_float(getattr(row, "T_G")),
                "T_B": _canon_float(getattr(row, "T_B")),
                "jpeg_r": _canon_float(getattr(row, "jpeg_r")),
                "jpeg_g": _canon_float(getattr(row, "jpeg_g")),
                "jpeg_b": _canon_float(getattr(row, "jpeg_b")),
                "fit_state": str(getattr(row, "fit_state", "included") or "included"),
                "order_correlation": _canon_float(getattr(row, "order_correlation")),
                "orientation_flipped": bool(getattr(row, "orientation_flipped", False)),
                "appearance_source": str(getattr(row, "appearance_source", "") or ""),
                "cr2_source": str(getattr(row, "cr2_source", "") or ""),
            }
        )
    return rows


def _fit_policy(*, seed: int) -> dict[str, Any]:
    return {
        "schema": CAMERA_TRANSFORM_SCHEMA,
        "model_version": "v2",
        "architecture": "root_polynomial_6feat + monotone_pchip_curves",
        "seed": int(seed),
        "validation_method": CV_POLICY,
        "validation_fold_count": CV_FOLDS,
        "validation_score": "median_per_sample(mean(T_R,T_G,T_B))",
        "validation_assignment": "measured_order_blocks_then_sha256(camera_transform_cv_v1\\0seed\\0sample_id)",
        "final_fit_rows": "all clean eligible rows",
        "jpeg_clip_threshold": int(JPEG_CLIP_THRESHOLD),
        "n_knots": int(N_KNOTS),
        "out_max": _canon_float(OUT_MAX),
        "min_delta": _canon_float(MIN_DELTA),
        "hygiene_rules_version": CT_HYGIENE_RULES_VERSION,
    }


def _corpus_policy(corpus: CameraTransformCorpus, *, use_fit_exclusions: bool) -> dict[str, Any]:
    return {
        "builder": "stored_extraction_result_appearance",
        "source": (corpus.summary or {}).get("source"),
        "use_fit_exclusions": bool(use_fit_exclusions),
        "accepted_extraction_results_only": True,
        "filament_exclusion_policy": "exclude_from_model drops samples referencing the filament in any role",
    }


def build_camera_transform_fit_fingerprint(
    store: Any,
    *,
    use_fit_exclusions: bool = True,
    corpus: CameraTransformCorpus | None = None,
    seed: int = SEED,
) -> CameraTransformFingerprintResult:
    if corpus is None:
        corpus = build_camera_transform_corpus_from_extraction_results(
            store,
            use_fit_exclusions=use_fit_exclusions,
        )

    clean, hygiene, n_censored = load_and_filter(corpus.rows)
    fold_by_sample = assign_validation_folds(clean, seed=seed)

    corpus_policy = _corpus_policy(corpus, use_fit_exclusions=use_fit_exclusions)
    fit_policy = _fit_policy(seed=seed)
    clean_rows = _clean_fit_rows(clean, fold_by_sample)
    raw_rows = _raw_corpus_rows(corpus.rows)
    validation_assignments = [
        {"sample_id": sample_id, "fold": int(fold)}
        for sample_id, fold in sorted(fold_by_sample.items())
    ]

    counts = {
        "raw_row_count": int(len(corpus.rows)),
        "fit_row_count": int(len(clean)),
        "final_fit_row_count": int(len(clean)),
        "validation_sample_count": len(fold_by_sample),
        "validation_fold_count": len(set(fold_by_sample.values())),
        "censored_row_count": int(n_censored),
        "censored_final_fit_row_count": int(clean["_censored"].sum()) if len(clean) and "_censored" in clean.columns else 0,
    }

    fit_input_payload = {
        "schema_version": CT_FINGERPRINT_SCHEMA_VERSION,
        "fingerprint_kind": CT_FINGERPRINT_KIND,
        "corpus_policy": corpus_policy,
        "fit_policy": fit_policy,
        "rows": clean_rows,
    }
    raw_corpus_payload = {
        "schema_version": CT_FINGERPRINT_SCHEMA_VERSION,
        "fingerprint_kind": "camera_transform_raw_corpus",
        "corpus_policy": corpus_policy,
        "rows": raw_rows,
        "skipped_samples": list(corpus.skipped_samples or []),
    }
    validation_payload = {
        "schema_version": CT_FINGERPRINT_SCHEMA_VERSION,
        "fingerprint_kind": "camera_transform_validation_folds",
        "seed": int(seed),
        "validation_method": CV_POLICY,
        "fold_count": CV_FOLDS,
        "assignments": validation_assignments,
    }
    diagnostic_payload = {
        "schema_version": CT_FINGERPRINT_SCHEMA_VERSION,
        "fingerprint_kind": "camera_transform_diagnostics",
        "corpus_summary": dict(corpus.summary or {}),
        "source_fingerprint": dict(corpus.source_fingerprint or {}),
        "hygiene_drop_counts": dict(hygiene or {}),
        "counts": counts,
    }

    fit_input_hash = _sha256_payload(fit_input_payload)
    raw_corpus_hash = _sha256_payload(raw_corpus_payload)
    validation_hash = _sha256_payload(validation_payload)
    diagnostic_hash = _sha256_payload(diagnostic_payload)

    fingerprint = {
        "schema_version": CT_FINGERPRINT_SCHEMA_VERSION,
        "fingerprint_kind": CT_FINGERPRINT_KIND,
        "fit_input_hash": fit_input_hash,
        "raw_corpus_hash": raw_corpus_hash,
        "diagnostic_hash": diagnostic_hash,
        "validation_hash": validation_hash,
        "corpus_policy": corpus_policy,
        "fit_policy": fit_policy,
        "counts": counts,
        "source_data_fingerprint": dict(corpus.source_fingerprint or {}),
    }

    return CameraTransformFingerprintResult(
        fingerprint=fingerprint,
        fit_input_hash=fit_input_hash,
        raw_corpus_hash=raw_corpus_hash,
        diagnostic_hash=diagnostic_hash,
        validation_hash=validation_hash,
        counts=counts,
        corpus_summary=dict(corpus.summary or {}),
        hygiene_drop_counts=dict(hygiene or {}),
    )


def parse_model_fit_fingerprint(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def robust_fit_input_hash(fingerprint: Any) -> str | None:
    parsed = parse_model_fit_fingerprint(fingerprint)
    if not parsed:
        return None
    if parsed.get("schema_version") != CT_FINGERPRINT_SCHEMA_VERSION:
        return None
    if parsed.get("fingerprint_kind") != CT_FINGERPRINT_KIND:
        return None
    value = parsed.get("fit_input_hash")
    return value if isinstance(value, str) and value else None
