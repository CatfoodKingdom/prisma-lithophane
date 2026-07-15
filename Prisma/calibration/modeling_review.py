from __future__ import annotations

import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

try:
    from lib.camera_transform import apply_forward as _apply_camera_forward
    from lib.camera_transform import load_camera_transform as _load_camera_transform
    from lib.color import linear_to_hex as _linear_to_hex
except ImportError:  # pragma: no cover - package-style test imports
    from Prisma.lib.camera_transform import apply_forward as _apply_camera_forward
    from Prisma.lib.camera_transform import load_camera_transform as _load_camera_transform
    from Prisma.lib.color import linear_to_hex as _linear_to_hex


MODEL_LABELS = {
    "legacy_spline": "Color Model v1",
    "photo_stack_v2": "Color Model v2",
    "camera_transform": "Camera Transform",
}
_UNSET = object()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _model_currentness_payload(store: Any, model_kind: str) -> dict[str, Any] | None:
    current_getter = getattr(store, "current_model_fit", None)
    list_getter = getattr(store, "list_model_fits", None)
    if not callable(current_getter) or not callable(list_getter):
        return None
    try:
        fit = current_getter(model_kind)
        if fit is None:
            fits = list_getter(model_kind=model_kind, include_stale=True)
            fit = fits[-1] if fits else None
    except Exception:
        return None
    if fit is None:
        return None
    return {
        "model_fit_id": fit.get("model_fit_id"),
        "model_kind": fit.get("model_kind"),
        "currentness_state": fit.get("currentness_state"),
        "stale_reason": fit.get("stale_reason"),
        "generated_at": fit.get("generated_at"),
        "artifact_root_rel_path": fit.get("artifact_root_rel_path"),
        "output_exists_at_last_check": fit.get("output_exists_at_last_check"),
    }


def build_model_status_payload(store: Any) -> dict[str, Any]:
    models: dict[str, dict[str, Any]] = {}
    for model_kind, label in MODEL_LABELS.items():
        currentness = _model_currentness_payload(store, model_kind)
        models[model_kind] = {
            "model_kind": model_kind,
            "label": label,
            "status": str(currentness.get("currentness_state") or "unknown") if currentness else "missing",
            "generated_at": currentness.get("generated_at") if currentness else None,
            "stale_reason": currentness.get("stale_reason") if currentness else None,
            "model_currentness": currentness,
        }
    return {"generated_at": _now_iso(), "models": models}


def _filament_map(store: Any) -> dict[str, Any]:
    try:
        return {str(_get(f, "filament_id")): f for f in (store.list_filaments() or [])}
    except Exception:
        return {}


def _filament_payload(filament: Any | None, filament_id: str) -> dict[str, Any]:
    name = _get(filament, "color_name") or _get(filament, "display_name") or filament_id
    return {
        "filament_id": filament_id,
        "brand": _get(filament, "manufacturer", "") if filament else "",
        "name": name,
        "display_name": _get(filament, "display_name", name) if filament else name,
        "hex": _get(filament, "hex", "#999999") if filament else "#999999",
        "exclude_from_model": bool(_get(filament, "exclude_from_model", False)) if filament else False,
        "white_cap_eligible": bool(_get(filament, "white_cap_eligible", False)) if filament else False,
    }


def _sample_roles(sample: Any, filaments: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for role in sorted((_get(sample, "roles", []) or []), key=lambda item: int(item.get("role_index") or 0)):
        fid = str(role.get("filament_id") or "")
        filament = _filament_payload(filaments.get(fid), fid)
        rows.append(
            {
                "role_index": int(role.get("role_index") or 0),
                "role_label": str(role.get("role_label") or ""),
                "role_kind": str(role.get("role_kind") or ""),
                "fixed_thickness_mm": role.get("fixed_thickness_mm"),
                **filament,
            }
        )
    return rows


def _accepted_extraction(store: Any, sample_id: str) -> dict[str, Any] | None:
    getter = getattr(store, "get_extraction_result", None)
    if not callable(getter):
        return None
    result = getter(sample_id)
    if not result or str(result.get("review_state") or "") != "accepted":
        return None
    return result


def _accepted_extractions_by_sample(store: Any) -> tuple[dict[str, dict[str, Any]], bool]:
    getter = getattr(store, "accepted_extraction_results_by_sample", None)
    if not callable(getter):
        return {}, False
    try:
        return dict(getter() or {}), True
    except Exception:
        return {}, False


def _hex_from_rgb(r: Any, g: Any, b: Any) -> str | None:
    try:
        vals = [max(0, min(255, int(round(float(v))))) for v in (r, g, b)]
    except (TypeError, ValueError):
        return None
    return "#{:02X}{:02X}{:02X}".format(*vals)


def _swatches_from_extraction(extraction: dict[str, Any] | None) -> list[dict[str, Any]]:
    swatches = (((extraction or {}).get("measurements") or {}).get("swatches") or [])
    return sorted(swatches, key=lambda sw: int(sw.get("swatch_index") or 0))


def _observed_payload(swatches: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    appearance_hex: list[str | None] = []
    appearance_rgb: list[list[float | None]] = []
    transmission_rgb: list[list[float | None]] = []
    transmission_hex: list[str | None] = []
    for swatch in swatches:
        appearance = swatch.get("appearance") or {}
        app_hex = _hex_from_rgb(appearance.get("jpeg_r"), appearance.get("jpeg_g"), appearance.get("jpeg_b"))
        appearance_hex.append(app_hex)
        appearance_rgb.append([appearance.get("jpeg_r"), appearance.get("jpeg_g"), appearance.get("jpeg_b")])
        trans = swatch.get("transmission") or {}
        transmission_rgb.append([trans.get("R_linear"), trans.get("G_linear"), trans.get("B_linear")])
        display = swatch.get("display") or {}
        transmission_hex.append(display.get("hex"))
    return (
        {
            "available": any(bool(item) for item in appearance_hex),
            "hex": appearance_hex,
            "rgb": appearance_rgb,
        },
        {
            "available": bool(swatches),
            "hex": transmission_hex,
            "rgb": transmission_rgb,
        },
    )


def _linear_triplet(value: Any) -> list[float] | None:
    if value is None:
        return None
    try:
        vals = [float(value[0]), float(value[1]), float(value[2])]
    except (TypeError, ValueError, IndexError, KeyError):
        return None
    if not all(math.isfinite(v) for v in vals):
        return None
    return [max(0.0, min(1.0, v)) for v in vals]


def _json_triplet_series(values: list[Any]) -> list[list[float] | None]:
    return [_linear_triplet(value) for value in values]


def _srgb01_to_hex(value: Any) -> str | None:
    try:
        vals = [max(0.0, min(1.0, float(value[i]))) for i in range(3)]
    except (TypeError, ValueError, IndexError, KeyError):
        return None
    return "#{:02X}{:02X}{:02X}".format(*(int(round(v * 255.0)) for v in vals))


def _series_payload(
    hex_values: list[str | None],
    *,
    rgb: list[Any] | None = None,
    status: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    hexes = list(hex_values or [])
    available = any(bool(item) for item in hexes)
    payload: dict[str, Any] = {
        "available": available,
        "status": status or ("available" if available else "unavailable"),
        "hex": hexes,
    }
    if rgb is not None:
        payload["rgb"] = list(rgb)
    if reason:
        payload["reason"] = reason
    return payload


def _transmission_hexes(linear_values: list[Any]) -> list[str | None]:
    out: list[str | None] = []
    for value in linear_values:
        rgb = _linear_triplet(value)
        out.append(_linear_to_hex(*rgb) if rgb is not None else None)
    return out


def _appearance_hexes(
    linear_values: list[Any],
    transform: Any | None,
) -> list[str | None]:
    out: list[str | None] = [None] * len(linear_values)
    if transform is None:
        return out
    indexed = [(idx, _linear_triplet(value)) for idx, value in enumerate(linear_values)]
    valid = [(idx, rgb) for idx, rgb in indexed if rgb is not None]
    if not valid:
        return out
    predicted = _apply_camera_forward(
        np.asarray([rgb for _idx, rgb in valid], dtype=np.float64),
        transform.params,
    )
    for (idx, _rgb), srgb in zip(valid, predicted):
        out[idx] = _srgb01_to_hex(srgb)
    return out


def _load_display_transform(store: Any) -> tuple[Any | None, str | None]:
    try:
        return _load_camera_transform(Path(store.root) / "camera_transform"), None
    except Exception as exc:
        return None, str(exc)


def _empty_model_domains(swatch_count: int, reason: str) -> dict[str, dict[str, dict[str, Any]]]:
    blank = [None] * max(0, int(swatch_count or 0))
    return {
        "appearance": {
            "corrected": _series_payload(blank, status="unavailable", reason=reason),
            "uncorrected": _series_payload(blank, status="unavailable", reason=reason),
        },
        "transmission": {
            "corrected": _series_payload(blank, status="unavailable", reason=reason),
            "uncorrected": _series_payload(blank, status="unavailable", reason=reason),
        },
    }


def _model_domains_from_linear(
    variants: dict[str, list[Any]],
    *,
    transform: Any | None,
    transform_error: str | None,
) -> dict[str, dict[str, dict[str, Any]]]:
    domains = {"appearance": {}, "transmission": {}}
    for key, values in variants.items():
        linears = _json_triplet_series(values)
        domains["transmission"][key] = _series_payload(
            _transmission_hexes(linears),
            rgb=linears,
        )
        if transform is None:
            domains["appearance"][key] = _series_payload(
                [None] * len(linears),
                rgb=linears,
                status="unavailable",
                reason=f"Camera Transform unavailable: {transform_error or 'missing transform'}",
            )
        else:
            domains["appearance"][key] = _series_payload(
                _appearance_hexes(linears, transform),
                rgb=linears,
            )
    return domains


def _photo_stack_sample_prediction(store: Any, sample_id: str) -> tuple[dict[str, Any] | None, str | None, str | None]:
    try:
        from lib.photo_stack_model.artifacts import load_candidate_file, load_latest_pointer
    except ImportError:  # pragma: no cover - package-style test imports
        from Prisma.lib.photo_stack_model.artifacts import load_candidate_file, load_latest_pointer

    try:
        latest = load_latest_pointer(store.root)
    except Exception as exc:
        return None, None, f"Latest Color Model v2 pointer could not be read: {exc}"
    if latest is None:
        return None, None, "No current Color Model v2 artifact"
    run_id = str(latest.get("run_id") or latest.get("path") or "")
    if not run_id:
        return None, None, "Latest Color Model v2 pointer is missing a run id"
    try:
        payload = load_candidate_file(store.root, run_id, "sample_predictions.json")
    except Exception as exc:
        return None, run_id, f"Color Model v2 sample predictions could not be read: {exc}"
    for sample in payload.get("samples") or []:
        if str(sample.get("sample_id") or "") == sample_id:
            return sample, run_id, None
    return None, run_id, "Sample was not present in the latest Color Model v2 prediction artifact"


def _photo_stack_model_domains(
    store: Any,
    sample_id: str,
    *,
    swatch_count: int,
    transform: Any | None,
    transform_error: str | None,
) -> dict[str, dict[str, dict[str, Any]]]:
    sample_prediction, run_id, reason = _photo_stack_sample_prediction(store, sample_id)
    if sample_prediction is None:
        return _empty_model_domains(swatch_count, reason or "Color Model v2 prediction unavailable")

    def swatch_sort_index(swatch: dict[str, Any]) -> int:
        try:
            return int(swatch.get("swatch_index"))
        except (TypeError, ValueError):
            return 0

    corrected: list[Any] = [None] * max(0, int(swatch_count or 0))
    uncorrected: list[Any] = [None] * max(0, int(swatch_count or 0))
    swatches = sorted(sample_prediction.get("swatches") or [], key=swatch_sort_index)
    for swatch in swatches:
        try:
            swatch_index = int(swatch.get("swatch_index"))
        except (TypeError, ValueError):
            continue
        if swatch_index < 0 or swatch_index >= len(uncorrected):
            continue
        predictions = swatch.get("predictions") or {}
        uncorrected[swatch_index] = (predictions.get("photo_stack") or {}).get("linear_rgb")
        corrected_payload = predictions.get("photo_stack_corrected") or swatch.get("predicted") or {}
        corrected[swatch_index] = corrected_payload.get("linear_rgb")
    domains = _model_domains_from_linear(
        {"corrected": corrected, "uncorrected": uncorrected},
        transform=transform,
        transform_error=transform_error,
    )
    for domain in domains.values():
        for payload in domain.values():
            payload["model_run_id"] = run_id
    return domains


def _legacy_spline_model_domains(
    store: Any,
    sample: Any,
    *,
    swatch_count: int,
    transform: Any | None,
    transform_error: str | None,
) -> dict[str, dict[str, dict[str, Any]]]:
    try:
        from fitting import composition as _comp
        from fitting.filament_roles import model_white_filament_ids
        from fitting.fitting import _legacy_spline_geometry
    except ImportError:  # pragma: no cover - package-style test imports
        from Prisma.calibration.fitting import composition as _comp
        from Prisma.calibration.fitting.filament_roles import model_white_filament_ids
        from Prisma.calibration.fitting.fitting import _legacy_spline_geometry

    if str(_get(sample, "processing_status", "") or "") != "processed":
        return _empty_model_domains(swatch_count, "Sample is not processed")
    strip_definition = _get(sample, "strip_definition", None)
    if strip_definition is None:
        return _empty_model_domains(swatch_count, "Sample has no strip definition")
    try:
        geometry = _legacy_spline_geometry(sample, white_filament_ids=model_white_filament_ids(store))
    except Exception as exc:
        return _empty_model_domains(swatch_count, f"Color Model v1 geometry could not be interpreted: {exc}")
    if geometry is None:
        return _empty_model_domains(swatch_count, "Sample geometry is not supported by Color Model v1")

    profiles_dir = Path(store.root) / "filaments" / "profiles"
    variable_fid = str(geometry.get("variable_filament_id") or "")
    var_profile = _comp.load_profile(variable_fid, profiles_dir)
    fixed_layers = list(geometry.get("fixed_layers") or [])
    fixed_profiles: list[tuple[dict[str, Any], float]] = []
    missing_profiles: list[str] = []
    if var_profile is None:
        missing_profiles.append(variable_fid)
    for layer in fixed_layers:
        fid = str(layer.get("filament_id") or "")
        profile = _comp.load_profile(fid, profiles_dir)
        if profile is None:
            missing_profiles.append(fid)
            continue
        fixed_profiles.append((profile, float(layer.get("nominal_thickness_mm") or 0.0)))
    if missing_profiles or var_profile is None:
        missing = ", ".join(sorted(set(missing_profiles)))
        return _empty_model_domains(swatch_count, f"Missing Color Model v1 profile: {missing}")

    try:
        corrections = _comp.load_pair_corrections(Path(store.root) / "filaments")
    except Exception:
        corrections = None

    corrected: list[Any] = []
    uncorrected: list[Any] = []
    thicknesses = list(_get(strip_definition, "variable_thicknesses_mm", []) or [])
    for thickness in thicknesses:
        layers = [(var_profile, float(thickness)), *fixed_profiles]
        raw = _comp.compose(layers, None)
        uncorrected.append(raw)
        corrected.append(_comp.compose(layers, corrections) if corrections else raw)
    return _model_domains_from_linear(
        {"corrected": corrected, "uncorrected": uncorrected},
        transform=transform,
        transform_error=transform_error,
    )


def _sample_model_detail(store: Any, sample: Any, row: dict[str, Any]) -> dict[str, Any]:
    appearance_hex = row.get("observed_appearance", {}).get("hex") or []
    transmission_hex = row.get("observed_transmission", {}).get("hex") or []
    swatch_count = max(len(appearance_hex), len(transmission_hex), int(row.get("swatch_count") or 0))
    transform, transform_error = _load_display_transform(store)
    excluded_filaments = row.get("excluded_model_filaments") or []
    if excluded_filaments:
        names = ", ".join(str(item.get("name") or item.get("filament_id") or "") for item in excluded_filaments)
        reason = f"Sample contains filament excluded from model fitting: {names}"
        photo_stack = _empty_model_domains(swatch_count, reason)
        legacy_spline = _empty_model_domains(swatch_count, reason)
    else:
        photo_stack = _photo_stack_model_domains(
            store,
            str(row.get("sample_id") or ""),
            swatch_count=swatch_count,
            transform=transform,
            transform_error=transform_error,
        )
        legacy_spline = _legacy_spline_model_domains(
            store,
            sample,
            swatch_count=swatch_count,
            transform=transform,
            transform_error=transform_error,
        )
    return {
        "schema_version": 1,
        "defaults": {
            "include_corrections": True,
            "domain": "appearance",
        },
        "camera_transform": {
            "available": transform is not None,
            "reason": transform_error,
        },
        "domains": {
            "appearance": {
                "measured": _series_payload(
                    appearance_hex,
                    rgb=row.get("observed_appearance", {}).get("rgb"),
                    status="available" if row.get("observed_appearance", {}).get("available") else "unavailable",
                ),
                "photo_stack_v2": photo_stack["appearance"],
                "legacy_spline": legacy_spline["appearance"],
            },
            "transmission": {
                "measured": _series_payload(
                    transmission_hex,
                    rgb=row.get("observed_transmission", {}).get("rgb"),
                    status="available" if row.get("observed_transmission", {}).get("available") else "unavailable",
                ),
                "photo_stack_v2": photo_stack["transmission"],
                "legacy_spline": legacy_spline["transmission"],
            },
        },
    }


def _sample_swatch_counts(sample: Any, swatches: list[dict[str, Any]]) -> dict[str, int]:
    excluded = {int(idx) for idx in (_get(sample, "excluded_swatches", []) or [])}
    total = len(swatches) or int(_get(sample, "n_swatches", 0) or 0)
    excluded_count = len([idx for idx in excluded if total <= 0 or idx < total])
    included = 0 if bool(_get(sample, "fit_exclude", False)) else max(0, total - excluded_count)
    return {
        "swatch_count": total,
        "included_swatch_count": included,
        "excluded_swatch_count": excluded_count,
    }


def _excluded_model_filaments(roles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    excluded: list[dict[str, Any]] = []
    seen: set[str] = set()
    for role in roles:
        filament_id = str(role.get("filament_id") or "")
        if not filament_id or filament_id in seen or not bool(role.get("exclude_from_model")):
            continue
        seen.add(filament_id)
        excluded.append(
            {
                "filament_id": filament_id,
                "brand": role.get("brand") or "",
                "name": role.get("name") or role.get("display_name") or filament_id,
                "display_name": role.get("display_name") or role.get("name") or filament_id,
                "hex": role.get("hex") or "#999999",
            }
        )
    return excluded


def _sample_row(store: Any, sample: Any, filaments: dict[str, Any], extraction: Any = _UNSET) -> dict[str, Any]:
    sample_id = str(_get(sample, "sample_id", ""))
    if extraction is _UNSET:
        extraction = _accepted_extraction(store, sample_id)
    swatches = _swatches_from_extraction(extraction)
    appearance, transmission = _observed_payload(swatches)
    counts = _sample_swatch_counts(sample, swatches)
    excluded_swatches = sorted(int(idx) for idx in (_get(sample, "excluded_swatches", []) or []))
    roles = _sample_roles(sample, filaments)
    excluded_filaments = _excluded_model_filaments(roles)
    fit_exclude = bool(_get(sample, "fit_exclude", False))
    has_extraction = extraction is not None
    model_eligible = (
        has_extraction
        and not fit_exclude
        and not excluded_filaments
        and counts["included_swatch_count"] > 0
    )
    if not has_extraction:
        ineligible_reason = "no_accepted_extraction"
    elif excluded_filaments:
        ineligible_reason = "filament_excluded"
    elif fit_exclude:
        ineligible_reason = "sample_excluded"
    elif counts["included_swatch_count"] <= 0:
        ineligible_reason = "no_included_swatches"
    else:
        ineligible_reason = ""
    return {
        "sample_id": sample_id,
        "sample_number": int(_get(sample, "sample_number", 0) or 0),
        "name": _get(sample, "name", "") or "",
        "processing_status": _get(sample, "processing_status", "") or "",
        "fit_exclude": fit_exclude,
        "excluded_swatches": excluded_swatches,
        **counts,
        "eligible_swatch_count": counts["included_swatch_count"] if model_eligible else 0,
        "model_eligible": model_eligible,
        "model_ineligible_reason": ineligible_reason,
        "has_excluded_model_filaments": bool(excluded_filaments),
        "excluded_model_filaments": excluded_filaments,
        "has_accepted_extraction": has_extraction,
        "geometry": {
            "geometry_id": _get(sample, "step_id", "") or "",
            "label": _get(sample, "step_file", "") or _get(sample, "step_id", "") or "",
            "variable_thicknesses_mm": list(_get(_get(sample, "strip_definition", None), "variable_thicknesses_mm", []) or []),
        },
        "filaments": roles,
        "observed_appearance": appearance,
        "observed_transmission": transmission,
        "model_results": {
            "legacy_spline": {"status": "not_evaluated", "reason": "prediction_integration_not_enabled"},
            "photo_stack_v2": {"status": "not_evaluated", "reason": "prediction_integration_not_enabled"},
        },
    }


def _all_sample_rows(store: Any) -> list[dict[str, Any]]:
    filaments = _filament_map(store)
    accepted_extractions, bulk_available = _accepted_extractions_by_sample(store)
    if bulk_available:
        return [
            _sample_row(store, sample, filaments, accepted_extractions.get(str(_get(sample, "sample_id", ""))))
            for sample in (store.list_samples() or [])
        ]
    return [_sample_row(store, sample, filaments) for sample in (store.list_samples() or [])]


def _model_eligible_sample_rows(store: Any) -> list[dict[str, Any]]:
    return [row for row in _all_sample_rows(store) if row["has_accepted_extraction"]]


def _status_attention(model_status: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    stale = []
    missing = []
    for key, entry in (model_status.get("models") or {}).items():
        item = {"model_kind": key, "label": entry.get("label"), "reason": entry.get("stale_reason") or ""}
        if entry.get("status") == "stale":
            stale.append(item)
        if entry.get("status") == "missing":
            missing.append(item)
    return {"stale_models": stale, "missing_models": missing}


def build_modeling_overview(store: Any) -> dict[str, Any]:
    all_rows = _all_sample_rows(store)
    rows = [row for row in all_rows if row["has_accepted_extraction"]]
    model_status = build_model_status_payload(store)
    sample_total = len(rows)
    sample_excluded = sum(1 for row in rows if row["fit_exclude"])
    sample_blocked_by_filament = sum(1 for row in rows if row["has_excluded_model_filaments"])
    sample_included = sum(1 for row in rows if row["model_eligible"])
    swatches_total = sum(row["swatch_count"] for row in rows)
    swatches_included = sum(row["eligible_swatch_count"] for row in rows)
    swatches_excluded = sum(row["excluded_swatch_count"] for row in rows)
    attention = _status_attention(model_status)
    attention.update(
        {
            "samples_without_accepted_extraction": sum(1 for row in all_rows if not row["has_accepted_extraction"]),
            "samples_with_excluded_filaments": sample_blocked_by_filament,
            "filaments_excluded_only": 0,
        }
    )
    return {
        "generated_at": _now_iso(),
        "review_schema_version": 1,
        "model_status": model_status,
        "inclusion_summary": {
            "samples_total": sample_total,
            "samples_included": sample_included,
            "samples_excluded": sample_excluded,
            "samples_blocked_by_filament": sample_blocked_by_filament,
            "swatches_total": swatches_total,
            "swatches_included": swatches_included,
            "swatches_excluded": swatches_excluded,
        },
        "attention": attention,
    }


def _normalize_filament_ids(filament_id: str | None = None, filament_ids: list[str] | None = None) -> list[str]:
    values: list[str] = []
    for value in filament_ids or []:
        text = str(value or "").strip()
        if text:
            values.append(text)
    legacy = str(filament_id or "").strip()
    if legacy:
        values.append(legacy)
    return list(dict.fromkeys(values))


def _row_contains_any_filament(row: dict[str, Any], filament_ids: list[str] | None) -> bool:
    normalized = _normalize_filament_ids(filament_ids=filament_ids)
    if not normalized:
        return True
    wanted = set(normalized)
    return any(item.get("filament_id") in wanted for item in row.get("filaments") or [])


def _row_contains_filament(row: dict[str, Any], filament_id: str | None) -> bool:
    return _row_contains_any_filament(row, [filament_id] if filament_id else None)


def _filter_sample_rows(
    rows: list[dict[str, Any]],
    filter_name: str,
    filament_ids: list[str] | None,
    model_status: dict[str, Any],
) -> list[dict[str, Any]]:
    stale_models = any(entry.get("status") == "stale" for entry in (model_status.get("models") or {}).values())
    missing_models = any(entry.get("status") == "missing" for entry in (model_status.get("models") or {}).values())
    filtered = [row for row in rows if _row_contains_any_filament(row, filament_ids)]
    if filter_name == "included":
        return [row for row in filtered if row["model_eligible"]]
    if filter_name == "excluded":
        return [row for row in filtered if row["fit_exclude"]]
    if filter_name == "filament_excluded":
        return [row for row in filtered if row["has_excluded_model_filaments"]]
    if filter_name == "has_excluded_swatches":
        return [row for row in filtered if row["excluded_swatch_count"] > 0]
    if filter_name == "needs_extraction":
        return [row for row in filtered if not row["has_accepted_extraction"]]
    if filter_name == "missing_model" and missing_models:
        return filtered
    if filter_name == "stale" and stale_models:
        return filtered
    if filter_name in {"missing_model", "stale"}:
        return []
    return filtered


def _normalize_sort_dir(sort_dir: str | None) -> str:
    return "desc" if str(sort_dir or "").casefold() == "desc" else "asc"


def _sample_fit_state_rank(row: dict[str, Any]) -> tuple[int, int]:
    if bool(row.get("has_excluded_model_filaments")):
        return (3, len(row.get("excluded_model_filaments") or []))
    if bool(row.get("fit_exclude")):
        return (2, int(row.get("excluded_swatch_count") or 0))
    excluded = int(row.get("excluded_swatch_count") or 0)
    if excluded > 0:
        return (1, excluded)
    return (0, 0)


def _sort_sample_rows(rows: list[dict[str, Any]], sort: str, sort_dir: str = "asc") -> list[dict[str, Any]]:
    def first_filament(row: dict[str, Any]) -> str:
        filaments = row.get("filaments") or []
        if not filaments:
            return ""
        return str(filaments[0].get("name") or filaments[0].get("filament_id") or "")

    direction = _normalize_sort_dir(sort_dir)
    if sort == "filament":
        key = lambda row: (first_filament(row).casefold(), row.get("sample_number") or 0, row["sample_id"])
    elif sort == "excluded_swatches":
        key = lambda row: (int(row.get("excluded_swatch_count") or 0), row.get("sample_number") or 0, row["sample_id"])
    elif sort == "status":
        key = lambda row: (_sample_fit_state_rank(row), str(row.get("processing_status") or ""), row.get("sample_number") or 0, row["sample_id"])
    else:
        key = lambda row: (row.get("sample_number") or 0, row["sample_id"])
    return sorted(rows, key=key, reverse=direction == "desc")


def list_modeling_samples(
    store: Any,
    *,
    filter: str = "all",
    filament_id: str | None = None,
    filament_ids: list[str] | None = None,
    sort: str = "sample_id",
    sort_dir: str = "asc",
    offset: int = 0,
    limit: int = 100,
) -> dict[str, Any]:
    model_status = build_model_status_payload(store)
    normalized_filament_ids = _normalize_filament_ids(filament_id=filament_id, filament_ids=filament_ids)
    direction = _normalize_sort_dir(sort_dir)
    rows = _sort_sample_rows(
        _filter_sample_rows(_model_eligible_sample_rows(store), filter, normalized_filament_ids, model_status),
        sort,
        direction,
    )
    offset = max(0, int(offset or 0))
    limit = max(1, min(int(limit or 100), 1000))
    return {
        "generated_at": _now_iso(),
        "review_schema_version": 1,
        "model_status": model_status,
        "filter": filter,
        "filament_id": normalized_filament_ids[0] if len(normalized_filament_ids) == 1 else None,
        "filament_ids": normalized_filament_ids,
        "sort": sort,
        "sort_dir": direction,
        "total": len(rows),
        "offset": offset,
        "limit": limit,
        "rows": rows[offset:offset + limit],
    }


def get_modeling_sample(store: Any, sample_id: str) -> dict[str, Any]:
    sample = store.get_sample(sample_id)
    if sample is None:
        raise KeyError(sample_id)
    row = _sample_row(store, sample, _filament_map(store))
    row["model_detail"] = _sample_model_detail(store, sample, row)
    return {
        "generated_at": _now_iso(),
        "review_schema_version": 1,
        "model_status": build_model_status_payload(store),
        "sample": row,
    }


def _filament_review_row(
    filament_id: str,
    filament: Any | None,
    sample_rows: list[dict[str, Any]],
    *,
    any_stale: bool,
) -> dict[str, Any]:
    containing = [row for row in sample_rows if _row_contains_filament(row, filament_id)]
    included_samples = [row for row in containing if row["model_eligible"]]
    included_swatches = sum(row["eligible_swatch_count"] for row in containing)
    excluded_swatches = sum(row["excluded_swatch_count"] for row in containing)
    role_counts: dict[str, int] = {}
    thicknesses: list[float] = []
    for row in containing:
        thicknesses.extend(float(v) for v in (row.get("geometry", {}).get("variable_thicknesses_mm") or []))
        role_kinds_for_sample: set[str] = set()
        for role in row.get("filaments") or []:
            if role.get("filament_id") == filament_id:
                role_kinds_for_sample.add(str(role.get("role_kind") or "unknown"))
        for role_kind in role_kinds_for_sample:
            role_counts[role_kind] = role_counts.get(role_kind, 0) + 1
    if bool(_get(filament, "exclude_from_model", False)):
        health = {"state": "excluded", "label": "Excluded", "reason": "Filament is excluded from model fitting"}
    elif any_stale:
        health = {"state": "stale", "label": "Stale", "reason": "One or more model artifacts need refitting"}
    elif containing and not included_samples:
        health = {"state": "excluded_only", "label": "Excluded only", "reason": "All samples containing this filament are excluded"}
    elif included_swatches < 8:
        health = {"state": "sparse", "label": "Sparse", "reason": "Limited included swatch evidence"}
    else:
        health = {"state": "good", "label": "Good", "reason": ""}
    return {
        **_filament_payload(filament, filament_id),
        "sample_count": len(containing),
        "included_sample_count": len(included_samples),
        "excluded_sample_count": max(0, len(containing) - len(included_samples)),
        "swatch_count": sum(row["swatch_count"] for row in containing),
        "included_swatch_count": included_swatches,
        "excluded_swatch_count": excluded_swatches,
        "role_counts": role_counts,
        "thickness": {
            "min_mm": min(thicknesses) if thicknesses else None,
            "max_mm": max(thicknesses) if thicknesses else None,
            "distinct_count": len({round(value, 6) for value in thicknesses}),
        },
        "health": health,
        "worst_sample": None,
        "model_results": {},
    }


def _roles_for_filament(row: dict[str, Any], filament_id: str) -> list[dict[str, Any]]:
    return [role for role in row.get("filaments") or [] if role.get("filament_id") == filament_id]


def list_modeling_filaments(
    store: Any,
    *,
    sort: str = "name",
    sort_dir: str = "asc",
    offset: int = 0,
    limit: int = 200,
) -> dict[str, Any]:
    filaments = _filament_map(store)
    sample_rows = _model_eligible_sample_rows(store)
    model_status = build_model_status_payload(store)
    any_stale = any(entry.get("status") == "stale" for entry in (model_status.get("models") or {}).values())
    rows: list[dict[str, Any]] = []
    for filament_id, filament in filaments.items():
        rows.append(_filament_review_row(filament_id, filament, sample_rows, any_stale=any_stale))
    direction = _normalize_sort_dir(sort_dir)
    if sort == "brand":
        key = lambda row: (str(row["brand"]).casefold(), str(row["name"]).casefold(), row["filament_id"])
    elif sort == "sample_count":
        key = lambda row: (int(row["sample_count"] or 0), str(row["name"]).casefold(), row["filament_id"])
    elif sort == "included_sample_count":
        key = lambda row: (int(row["included_sample_count"] or 0), str(row["name"]).casefold(), row["filament_id"])
    elif sort == "included_swatch_count":
        key = lambda row: (int(row["included_swatch_count"] or 0), str(row["name"]).casefold(), row["filament_id"])
    elif sort == "excluded_swatch_count":
        key = lambda row: (int(row["excluded_swatch_count"] or 0), str(row["name"]).casefold(), row["filament_id"])
    elif sort == "health":
        key = lambda row: (str((row.get("health") or {}).get("label") or "").casefold(), str(row["name"]).casefold(), row["filament_id"])
    else:
        key = lambda row: (str(row["name"]).casefold(), str(row["brand"]).casefold(), row["filament_id"])
    rows.sort(key=key, reverse=direction == "desc")
    offset = max(0, int(offset or 0))
    limit = max(1, min(int(limit or 200), 500))
    return {
        "generated_at": _now_iso(),
        "review_schema_version": 1,
        "model_status": model_status,
        "sort": sort,
        "sort_dir": direction,
        "total": len(rows),
        "offset": offset,
        "limit": limit,
        "rows": rows[offset:offset + limit],
    }


def get_modeling_filament(store: Any, filament_id: str) -> dict[str, Any]:
    filaments = _filament_map(store)
    filament = filaments.get(filament_id)
    if filament is None:
        raise KeyError(filament_id)
    sample_rows = _model_eligible_sample_rows(store)
    model_status = build_model_status_payload(store)
    any_stale = any(entry.get("status") == "stale" for entry in (model_status.get("models") or {}).values())
    row = _filament_review_row(filament_id, filament, sample_rows, any_stale=any_stale)
    samples = []
    for sample_row in sample_rows:
        if not _row_contains_filament(sample_row, filament_id):
            continue
        detail_row = dict(sample_row)
        detail_row.pop("model_detail", None)
        detail_row["roles_for_filament"] = _roles_for_filament(sample_row, filament_id)
        samples.append(detail_row)
    samples.sort(key=lambda item: (item.get("sample_number") or 0, item.get("sample_id") or ""))
    return {
        "generated_at": _now_iso(),
        "review_schema_version": 1,
        "model_status": model_status,
        "filament": row,
        "samples": samples,
    }
