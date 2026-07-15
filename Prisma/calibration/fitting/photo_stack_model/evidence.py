"""Adapter from live calibration samples into photo stack model evidence."""

from __future__ import annotations

import contextlib
import hashlib
import math
from collections import Counter
from pathlib import Path
from typing import Any

try:
    from fitting.filament_roles import (
        filament_role_snapshot_hash,
        model_white_filament_ids,
        photo_stack_bundle_classifier_metadata,
        require_known_filaments,
    )
    from fitting.filament_exclusions import (
        excluded_filament_ids, sample_references_excluded_filament,
    )
    from fitting.physical_stack import PhysicalStackError, physical_stack_for_swatch
except ImportError:  # pragma: no cover - supports package import from repo root
    from Prisma.calibration.fitting.filament_roles import (
        filament_role_snapshot_hash,
        model_white_filament_ids,
        photo_stack_bundle_classifier_metadata,
        require_known_filaments,
    )
    from Prisma.calibration.fitting.filament_exclusions import (
        excluded_filament_ids, sample_references_excluded_filament,
    )
    from Prisma.calibration.fitting.physical_stack import PhysicalStackError, physical_stack_for_swatch

try:
    from lib.photo_stack_model.schema import canonical_stack_signature, stable_json_hash_payload
except ImportError:  # pragma: no cover - supports package import from repo root
    from Prisma.lib.photo_stack_model.schema import canonical_stack_signature, stable_json_hash_payload


class EvidenceBuildError(RuntimeError):
    """Raised when calibration records cannot be mapped to physical evidence."""


EPS = 1e-9


def _linear_to_oklab(rgb_linear: list[float]) -> tuple[float, float, float]:
    """Convert linear sRGB to OKLab without importing the spline runtime."""

    r, g, b = [max(0.0, float(v)) for v in rgb_linear]
    l_val = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    m_val = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s_val = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l_root = l_val ** (1.0 / 3.0)
    m_root = m_val ** (1.0 / 3.0)
    s_root = s_val ** (1.0 / 3.0)
    return (
        0.2104542553 * l_root + 0.7936177850 * m_root - 0.0040720468 * s_root,
        1.9779984951 * l_root - 2.4285922050 * m_root + 0.4505937099 * s_root,
        0.0259040371 * l_root + 0.7827717662 * m_root - 0.8086757660 * s_root,
    )


def _is_white_filament(filament_id: str, white_filament_ids: set[str] | frozenset[str]) -> bool:
    return str(filament_id) in white_filament_ids


def _round_mm(value: Any) -> float:
    return round(float(value), 5)


def _file_sha256(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _directory_hash(root: Path, pattern: str) -> str:
    h = hashlib.sha256()
    if not root.exists():
        return h.hexdigest()
    for path in sorted(root.glob(pattern)):
        if not path.is_file():
            continue
        h.update(path.name.encode("utf-8"))
        with open(path, "rb") as f:
            h.update(f.read())
    return h.hexdigest()


def _oklch_from_linear(rgb_linear: list[float]) -> dict[str, float | None]:
    L, a, b = _linear_to_oklab(rgb_linear)
    C = math.sqrt(a * a + b * b)
    H = (math.degrees(math.atan2(b, a)) + 360.0) % 360.0 if C > 1e-12 else None
    return {
        "okL": L,
        "okA": a,
        "okB": b,
        "okC": C,
        "okH_deg": H,
    }


def _color_diagnostics(rgb_linear: list[float], oklch: dict[str, float | None]) -> dict[str, Any]:
    clipped = [max(float(v), 1e-6) for v in rgb_linear]
    od_rgb = [-math.log(v) for v in clipped]
    okL = max(float(oklch["okL"] or 0.0), 1e-6)
    return {
        "od_rgb": od_rgb,
        "mean_od_rgb": sum(od_rgb) / 3.0,
        "okL_log_od": -math.log(okL),
    }


def _classify_realized_stack(
    layers: tuple[tuple[str, float], ...],
    *,
    white_filament_ids: set[str] | frozenset[str],
) -> str:
    if not layers:
        return "empty_or_zero_thickness"

    white_flags = [_is_white_filament(fid, white_filament_ids) for fid, _thickness in layers]
    nonwhite_layers = [
        (fid, thickness)
        for fid, thickness in layers
        if not _is_white_filament(fid, white_filament_ids)
    ]
    nonwhite_unique = {fid for fid, _thickness in nonwhite_layers}

    if not nonwhite_layers:
        return "white_only"
    if len(layers) == 1:
        return "naked_single_filament"

    has_white_base = white_flags[0]
    has_white_top = white_flags[-1]
    if has_white_base and has_white_top:
        if len(nonwhite_unique) == 1:
            return "single_color_sandwich"
        return "cross_color_multilayer_sandwich"
    if has_white_base and not has_white_top:
        if len(nonwhite_unique) == 1:
            return "color_over_white"
        return "multicolor_over_white"
    return "unsupported_or_diagnostic"


def _sample_source_record(sample: Any) -> dict[str, Any]:
    sd = sample.strip_definition
    roles = [dict(role) if isinstance(role, dict) else dict(getattr(role, "__dict__", {})) for role in (getattr(sample, "roles", []) or [])]
    return {
        "sample_id": sample.sample_id,
        "step_id": sample.step_id,
        "step_file": sample.step_file,
        "processing_status": sample.processing_status,
        "review_accepted": bool(getattr(sample, "review_accepted", False)),
        "fit_exclude": bool(getattr(sample, "fit_exclude", False)),
        "excluded_swatches": list(getattr(sample, "excluded_swatches", []) or []),
        "roles": roles,
        "variable_thicknesses_mm": list(sd.variable_thicknesses_mm or []) if sd else [],
    }


# ── Measured-color source switch (doc 32 Stage 4.5/4.6) ─────────────────────
# Production reads measured color from the per-sample extraction_result sidecar;
# fit-control (fit_state / exclusion_reason) stays a LIVE read. The legacy
# Sample.measurements color path stays callable via use_measured_source().
_MEASURED_SOURCE = "sidecar"   # "sidecar" | "legacy"


@contextlib.contextmanager
def use_measured_source(mode: str):
    """Temporarily select the measured-color source ('sidecar'|'legacy')."""
    global _MEASURED_SOURCE
    if mode not in ("sidecar", "legacy"):
        raise ValueError(f"invalid measured source: {mode!r}")
    prev = _MEASURED_SOURCE
    _MEASURED_SOURCE = mode
    try:
        yield
    finally:
        _MEASURED_SOURCE = prev


def _sidecar_swatch_map(store: Any, sample: Any) -> dict[int, Any]:
    """Return {swatch_index: SwatchExtraction} from the sample's sidecar. Fails
    loud (no silent fallback) if the sidecar is absent or malformed (doc 32 §3.5)."""
    try:
        from models import ExtractionResult
    except ModuleNotFoundError:  # pragma: no cover - package-style imports
        from Prisma.calibration.models import ExtractionResult
    from pydantic import ValidationError

    raw = store.get_extraction_result(sample.sample_id)
    if raw is None:
        raise EvidenceBuildError(
            f"{sample.sample_id}: measurement-bearing sample has no "
            f"extraction_result sidecar; run backfill")
    try:
        result = ExtractionResult(**raw)
    except ValidationError as exc:
        raise EvidenceBuildError(
            f"{sample.sample_id}: malformed extraction_result sidecar: {exc}")
    return {int(sw.swatch_index): sw for sw in result.measurements.swatches}


def build_photo_stack_evidence(
    store: Any,
    *,
    use_fit_exclusions: bool = False,
    require_processed: bool = True,
) -> dict[str, Any]:
    """Build canonical stack evidence from live calibration samples.

    Geometry is strict: each measured swatch must have an annotated variable
    thickness, and fixed filament/thickness counts must agree. No swatch-index
    fallback is used.
    """

    samples = store.list_samples()
    excluded_fids = excluded_filament_ids(store)
    white_ids = model_white_filament_ids(store)
    snapshot_hash = filament_role_snapshot_hash(store)
    classifier_metadata = photo_stack_bundle_classifier_metadata(white_ids, snapshot_hash)
    referenced_filaments: set[str] = set()
    for sample in samples:
        for role in (getattr(sample, "roles", []) or []):
            fid = str(getattr(role, "filament_id", "") if not isinstance(role, dict) else role.get("filament_id", "") or "")
            if fid:
                referenced_filaments.add(fid)
    require_known_filaments(store, referenced_filaments)
    sample_records: list[dict[str, Any]] = []
    swatch_records: list[dict[str, Any]] = []
    skipped_samples: list[dict[str, str]] = []
    class_counts: Counter[str] = Counter()
    excluded_sample_count = 0
    excluded_swatch_count = 0

    for sample in samples:
        source = _sample_source_record(sample)
        # Per-filament model exclusion (doc 33 B1f) — always-on, drops samples
        # referencing an exclude_from_model filament (variable or fixed role).
        if sample_references_excluded_filament(sample, excluded_fids):
            skipped_samples.append({"sample_id": sample.sample_id,
                                    "reason": "filament_exclude_from_model"})
            continue
        if require_processed and sample.processing_status != "processed":
            skipped_samples.append({"sample_id": sample.sample_id, "reason": "not_processed"})
            continue
        if sample.measurements is None:
            skipped_samples.append({"sample_id": sample.sample_id, "reason": "missing_measurements"})
            continue
        if sample.strip_definition is None:
            skipped_samples.append({"sample_id": sample.sample_id, "reason": "missing_strip_definition"})
            continue

        sample_fit_excluded = bool(getattr(sample, "fit_exclude", False))
        if sample_fit_excluded:
            excluded_sample_count += 1
        excluded_swatches = set(int(i) for i in (getattr(sample, "excluded_swatches", []) or []))

        # Two-source join: measured color from the sidecar (keyed by swatch_index),
        # fit-control from the live sample. Guard that the swatch sets match exactly.
        side_map = None
        if _MEASURED_SOURCE == "sidecar":
            side_map = _sidecar_swatch_map(store, sample)
            live_indices = {int(sw.swatch_index) for sw in sample.measurements.swatches}
            if set(side_map) != live_indices:
                raise EvidenceBuildError(
                    f"{sample.sample_id}: sidecar swatch indices {sorted(side_map)} "
                    f"!= live measurement indices {sorted(live_indices)}")

        swatch_indices: list[int] = []
        swatch_classes: Counter[str] = Counter()

        for sw in sample.measurements.swatches:
            swatch_index = int(sw.swatch_index)
            try:
                physical_stack = physical_stack_for_swatch(
                    sample,
                    swatch_index,
                    white_filament_ids=white_ids,
                )
            except PhysicalStackError as exc:
                raise EvidenceBuildError(str(exc)) from exc
            layers = physical_stack.collapsed_layers_bottom_to_top
            evidence_class = _classify_realized_stack(layers, white_filament_ids=white_ids)
            class_counts[evidence_class] += 1
            swatch_classes[evidence_class] += 1
            swatch_indices.append(swatch_index)

            swatch_fit_excluded = swatch_index in excluded_swatches or getattr(sw, "fit_state", "") == "excluded"
            if swatch_fit_excluded:
                excluded_swatch_count += 1
            included = not (use_fit_exclusions and (sample_fit_excluded or swatch_fit_excluded))

            if side_map is not None:
                side = side_map[swatch_index]
                rgb_linear = [
                    float(side.transmission.R_linear),
                    float(side.transmission.G_linear),
                    float(side.transmission.B_linear),
                ]
                measured_hex = side.display.hex
                measured_srgb = [int(side.display.R), int(side.display.G), int(side.display.B)]
                nominal_mm = float(side.nominal_thickness_mm)
            else:
                rgb_linear = [
                    float(sw.R_linear),
                    float(sw.G_linear),
                    float(sw.B_linear),
                ]
                measured_hex = sw.hex
                measured_srgb = [int(sw.R), int(sw.G), int(sw.B)]
                nominal_mm = float(sw.nominal_thickness_mm)
            oklch = _oklch_from_linear(rgb_linear)
            swatch_records.append(
                {
                    "sample_id": sample.sample_id,
                    "swatch_index": swatch_index,
                    "included": included,
                    "excluded_by_record": bool(sample_fit_excluded or swatch_fit_excluded),
                    "exclusion_reason": getattr(sw, "exclusion_reason", "") or "",
                    "evidence_class": evidence_class,
                    "stack": [
                        {"filament_id": fid, "thickness_mm": thickness}
                        for fid, thickness in layers
                    ],
                    "authored_layers": [
                        layer.as_dict()
                        for layer in physical_stack.authored_layers_bottom_to_top
                    ],
                    "model_layers": [
                        {"filament_id": fid, "thickness_mm": thickness, "role": role}
                        for fid, thickness, role in physical_stack.model_layer_triples_bottom_to_top
                    ],
                    "model_layer_triples": [
                        [fid, thickness, role]
                        for fid, thickness, role in physical_stack.model_layer_triples_bottom_to_top
                    ],
                    "model_white_filament_ids_in_stack": sorted(
                        {
                            fid for fid, _thickness in layers
                            if _is_white_filament(fid, white_ids)
                        }
                    ),
                    "model_color_filament_ids_in_stack": sorted(
                        {
                            fid for fid, _thickness in layers
                            if not _is_white_filament(fid, white_ids)
                        }
                    ),
                    "stack_signature": canonical_stack_signature(layers),
                    "variable_role_index": physical_stack.variable_role_index,
                    "variable_filament_id": physical_stack.variable_filament_id,
                    "variable_thickness_mm": physical_stack.variable_thickness_mm,
                    "nominal_thickness_mm": nominal_mm,
                    "measured": {
                        "hex": measured_hex,
                        "srgb": measured_srgb,
                        "linear_rgb": rgb_linear,
                        **oklch,
                        **_color_diagnostics(rgb_linear, oklch),
                    },
                }
            )

        dominant_class = swatch_classes.most_common(1)[0][0] if swatch_classes else "unknown"
        sample_records.append(
            {
                **source,
                "included": not (use_fit_exclusions and sample_fit_excluded),
                "dominant_evidence_class": dominant_class,
                "evidence_classes": dict(sorted(swatch_classes.items())),
                "swatch_indices": swatch_indices,
                "swatch_count": len(swatch_indices),
            }
        )

    sample_payload_for_hash = [_sample_source_record(s) for s in samples]
    evidence_hash = hashlib.sha256(stable_json_hash_payload(swatch_records).encode("utf-8")).hexdigest()
    data_root = Path(store.root).resolve()
    fingerprint = {
        "data_root": str(data_root),
        "samples_hash": _directory_hash(data_root / "samples", "exp-*.json"),
        "filaments_hash": _file_sha256(data_root / "filaments" / "registry.json"),
        "filament_role_snapshot_hash": snapshot_hash,
        "evidence_hash": evidence_hash,
        "source_sample_hash": hashlib.sha256(
            stable_json_hash_payload(sample_payload_for_hash).encode("utf-8")
        ).hexdigest(),
        "source_snapshot_label": None,
    }

    summary = {
        "sample_count": len(sample_records),
        "swatch_count": len(swatch_records),
        "skipped_sample_count": len(skipped_samples),
        "excluded_sample_count": excluded_sample_count,
        "excluded_swatch_count": excluded_swatch_count,
        "use_fit_exclusions": bool(use_fit_exclusions),
        "evidence_classes": dict(sorted(class_counts.items())),
    }

    return {
        "schema_version": 1,
        "source": "calibration_datastore",
        "filament_classification": classifier_metadata,
        "summary": summary,
        "input_fingerprint": fingerprint,
        "samples": sample_records,
        "swatches": swatch_records,
        "skipped_samples": skipped_samples,
    }
