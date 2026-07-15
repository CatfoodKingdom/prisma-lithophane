"""
parity.py — Step 3 Camera Transform parity harness (doc-30 §9, §14 steps 2-3).

READ-ONLY. Captures the legacy (or stored) CT corpus plus the downstream hygiene
filter and seeded validation-fold assignment into a deterministic, JSON-serializable
snapshot. The parity oracle (later phase) diffs two snapshots — legacy fit-time
extraction vs stored-sidecar — to prove the CT switch preserves membership, row
order, skips, hygiene, validation folds, and (A1) the per-row T/appearance values.

`normalize_skip_reason` is the shared D4 helper: it maps the raw legacy skip
strings to a stable MEMBERSHIP class. Both the legacy and stored skip reasons go
through it so the oracle compares classes (robust to a changed embedded-JPEG
`{exc}` text), while the raw `corpus.summary.skip_reasons` strings remain a
separate artifact-parity surface compared exactly under the freeze protocol.
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_CAL_DIR = _Path(__file__).resolve().parent.parent.parent
if str(_CAL_DIR) not in _sys.path:
    _sys.path.insert(0, str(_CAL_DIR))

from fitting.camera_transform.fit import (
    CV_FOLDS,
    CV_POLICY,
    SEED,
    assign_validation_folds,
    load_and_filter,
)

# Membership classes (D4). Order: most specific prefix match first is unnecessary
# because the legacy raw strings are disjoint except the decode-failure prefix.
_EXACT_SKIP_CLASSES = {
    "missing sample_id": "missing_sample_id",
    "sample marked fit_exclude": "sample_fit_excluded",
    "missing processed measurements": "missing_measurements",
    "no CR2 for embedded JPEG": "missing_cr2",
    "no usable embedded JPEG swatches": "no_usable_swatches",
}
_DECODE_FAILURE_PREFIX = "embedded JPEG extraction failed:"

# The full set of valid classes (covers corpus.py:206/209/214/218/227/264).
SKIP_CLASSES = frozenset(_EXACT_SKIP_CLASSES.values()) | {"embedded_jpeg_decode_failed"}


def normalize_skip_reason(raw: str) -> str:
    """Map a raw legacy CT skip reason to its membership class (D4).

    The embedded-JPEG decode failure carries variable `{exc}` text, so it is
    matched by prefix; all others are exact. An unrecognized reason raises (fail
    loud) so a new skip reason can never be silently misclassified.
    """
    text = str(raw)
    if text in _EXACT_SKIP_CLASSES:
        return _EXACT_SKIP_CLASSES[text]
    if text.startswith(_DECODE_FAILURE_PREFIX):
        return "embedded_jpeg_decode_failed"
    raise ValueError(f"unrecognized CT skip reason (no D4 class): {raw!r}")


def _row_keys(df) -> list[list]:
    if df is None or len(df) == 0:
        return []
    return [[str(sid), int(idx)]
            for sid, idx in zip(df["sample_id"], df["swatch_index"])]


# Columns the hard (§9.2) + numeric (§9.3 / A1) tiers compare per row.
_VALUE_COLS = (
    "T_R", "T_G", "T_B",
    "jpeg_r", "jpeg_g", "jpeg_b",
    "nominal_thickness_mm", "appearance_source", "cr2_source",
)


def capture_corpus_baseline(corpus, *, seed: int = SEED) -> dict:
    """Snapshot a corpus, hygiene, and validation folds without fitting.

    Mirrors the fit pipeline order: ``load_and_filter`` followed by deterministic
    sample-grouped fold assignment on the cleaned frame.
    """
    df = corpus.rows
    empty = df is None or len(df) == 0

    skipped = [
        {"sample_id": str(s["sample_id"]), "reason": str(s["reason"]),
         "class": normalize_skip_reason(s["reason"])}
        for s in (corpus.skipped_samples or [])
    ]

    if empty:
        clean, drop_counts, n_censored = df, {}, 0
        clean_keys = censored_keys = []
        fold_by_sample = {}
    else:
        clean, drop_counts, n_censored = load_and_filter(df)
        clean_keys = _row_keys(clean)
        censored_keys = [
            [str(sid), int(idx)]
            for sid, idx, c in zip(clean["sample_id"], clean["swatch_index"], clean["_censored"])
            if bool(c)
        ]
        fold_by_sample = assign_validation_folds(clean, seed=seed)

    folds = []
    for fold_index in range(CV_FOLDS):
        sample_ids = sorted(
            sample_id for sample_id, fold in fold_by_sample.items()
            if fold == fold_index
        )
        if clean is None or len(clean) == 0:
            row_keys = []
        else:
            row_keys = _row_keys(
                clean[clean["sample_id"].astype(str).isin(sample_ids)]
            )
        folds.append(
            {
                "fold": fold_index,
                "sample_ids": sample_ids,
                "row_keys": row_keys,
            }
        )

    values: dict[str, dict] = {}
    if not empty:
        for row in df.itertuples(index=False):
            key = f"{getattr(row, 'sample_id')}|{int(getattr(row, 'swatch_index'))}"
            values[key] = {col: _coerce(getattr(row, col, None)) for col in _VALUE_COLS}

    return {
        "raw_row_keys": _row_keys(df),
        "skipped": skipped,
        "summary": dict(corpus.summary) if corpus.summary else {},
        "hygiene": {
            "drop_counts": dict(drop_counts),
            "n_censored": int(n_censored),
            "clean_row_keys": clean_keys,
            "censored_row_keys": censored_keys,
        },
        "validation": {
            "seed": int(seed),
            "method": CV_POLICY,
            "fold_count": CV_FOLDS,
            "folds": folds,
        },
        "values": values,
    }


def _coerce(v):
    """Make a cell JSON-serializable without changing numeric value."""
    if v is None:
        return None
    if isinstance(v, (str, bool, int)):
        return v
    try:
        return float(v)
    except (TypeError, ValueError):
        return str(v)


# Hard-tier per-row columns that must match EXACTLY (no tolerance) — doc-30 §9.2 + A1.
_HARD_VALUE_COLS = ("T_R", "T_G", "T_B", "nominal_thickness_mm", "appearance_source", "cr2_source")
_JPEG_COLS = ("jpeg_r", "jpeg_g", "jpeg_b")


def compare_corpus_baselines(
    legacy: dict,
    stored: dict,
    *,
    jpeg_per_channel_tol: float = 0.0,
    jpeg_mean_bias_tol: float = 0.0,
) -> dict:
    """Diff a legacy vs a stored corpus baseline (doc-30 §9 oracle).

    Defaults assert EXACT equality (Theo 2026-06-17: a same-machine /
    same-decode_environment backfill is bit-exact, so the same-machine gate is
    zero-tolerance). Pass the cross-`decode_environment` byte-scale bounds
    (per-channel 1.0, mean-bias 0.05) only for a future cross-version re-fit.

    Tiers:
    - HARD (§9.2): summary (incl. raw skip_reasons strings), row order, skip
      MEMBERSHIP by normalized class (D4), hygiene clean/censored sets (the
      §9.4 threshold-crossing guard — a row that changes a low-corr/suspicious/
      nan/clip flag drops out of these sets), validation folds, value-key set, and
      the per-row hard columns (T/nominal/appearance_source/cr2_source).
    - NUMERIC (§9.3): per-channel jpeg drift vs the tolerances.
    """
    checks: dict = {}

    checks["summary"] = {"pass": legacy["summary"] == stored["summary"]}
    checks["row_order"] = {"pass": legacy["raw_row_keys"] == stored["raw_row_keys"]}

    leg_skip = [(s["sample_id"], s["class"]) for s in legacy["skipped"]]
    sto_skip = [(s["sample_id"], s["class"]) for s in stored["skipped"]]
    checks["skip_membership"] = {"pass": leg_skip == sto_skip}

    lh, sh = legacy["hygiene"], stored["hygiene"]
    checks["hygiene"] = {
        "pass": (lh["clean_row_keys"] == sh["clean_row_keys"]
                 and lh["censored_row_keys"] == sh["censored_row_keys"]),
    }
    checks["validation"] = {"pass": legacy["validation"] == stored["validation"]}

    lv, sv = legacy["values"], stored["values"]
    keys_match = set(lv) == set(sv)
    checks["value_keys_match"] = {"pass": keys_match}

    hard_mismatches = []
    jpeg_diffs = []
    for key in sorted(set(lv) & set(sv)):
        a, b = lv[key], sv[key]
        for col in _HARD_VALUE_COLS:
            if a.get(col) != b.get(col):
                hard_mismatches.append({"key": key, "col": col, "legacy": a.get(col), "stored": b.get(col)})
        for col in _JPEG_COLS:
            jpeg_diffs.append(float(b.get(col, 0.0)) - float(a.get(col, 0.0)))
    checks["hard_values"] = {"pass": not hard_mismatches, "mismatches": hard_mismatches[:50]}

    max_abs = max((abs(d) for d in jpeg_diffs), default=0.0)
    mean_bias = (sum(jpeg_diffs) / len(jpeg_diffs)) if jpeg_diffs else 0.0
    n_over = sum(1 for d in jpeg_diffs if abs(d) > jpeg_per_channel_tol)
    checks["jpeg_numeric"] = {
        "pass": (max_abs <= jpeg_per_channel_tol and abs(mean_bias) <= jpeg_mean_bias_tol),
        "max_abs_diff": max_abs,
        "mean_bias": mean_bias,
        "n_over_tol": n_over,
        "n_compared": len(jpeg_diffs),
        "per_channel_tol": jpeg_per_channel_tol,
        "mean_bias_tol": jpeg_mean_bias_tol,
    }

    passed = all(c["pass"] for c in checks.values())
    return {"passed": passed, "checks": checks}
