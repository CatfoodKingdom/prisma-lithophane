"""
sample_mutations.py — Domain logic for sample creation, update, and fitting.

Extracted from server.py to keep HTTP handlers thin.
All functions raise ValueError for domain-level validation errors;
the server layer converts these to HTTPException.
"""
from __future__ import annotations

import numpy as np

import sys as _sys
from pathlib import Path as _Path
_CAL_DIR = _Path(__file__).resolve().parent
if str(_CAL_DIR) not in _sys.path:
    _sys.path.insert(0, str(_CAL_DIR))

from data_access import DataStore
from models import StripDefinition, StripGeometry, UpdateSampleRequest, classify_mode
from fitting import fitting as _fitting
from sample_visuals import remove_sample_visuals


# ── Processing invalidation ──────────────────────────────────────────────────


def sample_profile_filament_ids(sample) -> set[str]:
    """Return filament ids whose profiles may depend on this sample."""
    if sample is None or sample.filaments is None:
        return set()
    ids = set()
    if sample.filaments.variable:
        ids.add(sample.filaments.variable)
    ids.update(fid for fid in (sample.filaments.fixed or []) if fid)
    return ids


def mark_profiles_stale_for_sample(store: DataStore, sample, reason: str = "") -> list[str]:
    """Mark every profile that may have consumed this sample's data as stale."""
    stale_ids = []
    for filament_id in sorted(sample_profile_filament_ids(sample)):
        if store.flag_profile_stale(filament_id, reason):
            stale_ids.append(filament_id)
    return stale_ids


def recompute_sample_status(sample) -> str:
    """Set sample status from current assignment completeness when unprocessed."""
    if sample.measurements is not None:
        return sample.processing_status
    if sample.assigned_image and sample.assigned_blank_id and sample.orientation_rots is not None:
        sample.processing_status = "assigned"
    else:
        sample.processing_status = "unassigned"
    return sample.processing_status


def _has_processing_outputs(store: DataStore, sample) -> bool:
    if sample is None:
        return False
    if sample.measurements is not None:
        return True
    if sample.processing_status in {"processed", "failed", "flagged"}:
        return True
    if getattr(store, "backend", "") == "sqlite":
        try:
            return store.get_extraction_result(sample.sample_id) is not None
        except Exception:
            return False
    if (store.root / "thumbnails" / sample.sample_id).exists():
        return True
    # A pending extraction-result sidecar is also a processing output (doc-29 §12.1),
    # so a sample carrying only a sidecar is still invalidated on a meaning change.
    return store._extraction_result_path(sample.sample_id).exists()


def _could_have_contributed_to_fit(sample) -> bool:
    if sample is None:
        return False
    return sample.measurements is not None or sample.processing_status == "processed"


def invalidate_sample_processing(
    store: DataStore,
    sample,
    previous_sample=None,
    reason: str = "",
    clear_fit_exclude: bool = True,
) -> dict:
    """Clear processing-derived state and mark dependent profiles stale.

    ``previous_sample`` should be the sample state before a provenance mutation.
    It is used for stale-profile ownership, because the old filament identities
    are the ones whose fitted models may have consumed the old measurements.
    """
    before = previous_sample or sample
    had_outputs = _has_processing_outputs(store, before) or _has_processing_outputs(store, sample)
    stale_profiles: list[str] = []
    sqlite_backend = getattr(store, "backend", "") == "sqlite"

    if had_outputs:
        if _could_have_contributed_to_fit(before) and not sqlite_backend:
            stale_profiles = mark_profiles_stale_for_sample(store, before, reason)

        sample.measurements = None
        sample.flag_reason = None
        sample.review_accepted = False
        sample.excluded_swatches = []
        if clear_fit_exclude:
            sample.fit_exclude = False

        if not sqlite_backend:
            remove_sample_visuals(store.root, sample.sample_id)

            # Discard the extraction-result sidecar — a meaning-changing mutation
            # makes the stored extraction (incl. any appearance) stale (doc-29 §12.1).
            store.delete_extraction_result(sample.sample_id)

    status = recompute_sample_status(sample)
    return {
        "invalidated": had_outputs,
        "status": status,
        "stale_profiles": stale_profiles,
    }


# ── Sample update ─────────────────────────────────────────────────────────────


def apply_sample_update(store: DataStore, sample, req: UpdateSampleRequest) -> bool:
    """Apply mutation fields from *req* to *sample*.

    Returns True if anything changed.  Raises ``ValueError`` for
    validation problems (unknown filament, unparseable STEP file, etc.).
    """
    changed = False
    provenance_changed = False
    previous_sample = sample.model_copy(deep=True)

    # Update variable filament
    if req.variable_filament_id is not None:
        old_var = sample.filaments.variable
        var_fil = store.get_filament(req.variable_filament_id)
        if var_fil is None:
            raise ValueError(f"Filament '{req.variable_filament_id}' not found")
        sample.filaments.variable = req.variable_filament_id

        # Rebuild auto-name
        strip_def = sample.strip_definition
        vt = strip_def.variable_thicknesses_mm if strip_def else []
        lh = strip_def.layer_height_mm if strip_def else 0.1
        mode = strip_def.mode if strip_def else "manual"
        vt_str = "-".join(f"{t:.2f}" for t in vt)
        sample.name = f"{req.variable_filament_id}_{mode}-{vt_str}_lh{lh:.2f}"

        if old_var != req.variable_filament_id:
            provenance_changed = True
        changed = True

    # Update fixed filaments
    if req.fixed_filament_ids is not None:
        for fid in req.fixed_filament_ids:
            fil = store.get_filament(fid)
            if fil is None:
                raise ValueError(f"Fixed filament '{fid}' not found")
        if list(sample.filaments.fixed or []) != list(req.fixed_filament_ids or []):
            provenance_changed = True
        sample.filaments.fixed = req.fixed_filament_ids
        changed = True

    # Update STEP file (this changes geometry, so rebuild strip_definition)
    requested_step_ref = req.step_id if req.step_id is not None else req.step_file
    current_step_ref = sample.step_id or sample.step_file
    if requested_step_ref is not None and requested_step_ref != current_step_ref:
        step_record = store.find_step_record(step_id=req.step_id, step_file=req.step_file)
        if step_record is None:
            raise ValueError(f"Unknown STEP reference: '{requested_step_ref}'")

        variable_thicknesses = step_record.variable_thicknesses_mm
        fixed_layers = step_record.fixed_layers
        layer_height = step_record.layer_height_mm
        layer_count = step_record.layer_count
        fixed_thicknesses = [fl.get("thickness_mm", 0) for fl in fixed_layers]

        mode = classify_mode(variable_thicknesses)

        sample.step_id = step_record.step_id
        sample.step_file = step_record.file_name
        sample.strip_definition = StripDefinition(
            n_layers=layer_count,
            layer_height_mm=layer_height,
            mode=mode,
            anchor_mm=variable_thicknesses[0] if variable_thicknesses else None,
            variable_thicknesses_mm=variable_thicknesses,
            fixed_thicknesses_mm=fixed_thicknesses,
            strip_geometry=StripGeometry(
                num_swatches=len(variable_thicknesses) or 8,
                step_w_mm=12.0,
                step_h_mm=20.0,
                border_mm=3.0,
            ),
        )

        # Rebuild name with new geometry
        var_id = sample.filaments.variable
        vt_str = "-".join(f"{t:.2f}" for t in variable_thicknesses)
        sample.name = f"{var_id}_{mode}-{vt_str}_lh{layer_height:.2f}"

        provenance_changed = True
        changed = True

    # Update notes
    if req.notes is not None:
        # notes is stored in the raw JSON but not in the Sample model yet
        # For now just save it through — it's preserved in the JSON
        changed = True

    # Update review_accepted flag
    if req.review_accepted is not None:
        sample.review_accepted = req.review_accepted
        changed = True

    if provenance_changed:
        invalidate_sample_processing(
            store,
            sample,
            previous_sample,
            f"Sample {sample.sample_id} provenance changed",
        )

    return changed


# ── Batch sample creation from STEP files ─────────────────────────────────────


def create_samples_from_step_files(
    store: DataStore,
    variable_filament,
    fixed_filaments: list,
    fixed_filament_ids: list[str],
    step_ids: list[str],
    notes: str = "",
) -> dict:
    """Create one sample per STEP file.

    *variable_filament* and *fixed_filaments* are already-validated filament
    objects (looked up by the caller).  *fixed_filament_ids* is the raw id
    list used for count validation against the STEP metadata.

    Returns ``{"created": [...], "errors": [...]}``.
    """
    created = []
    errors = []

    for step_id in step_ids:
        step_record = store.find_step_record(step_id=step_id)
        if step_record is None:
            errors.append({"step_id": step_id, "error": "Unknown STEP record"})
            continue

        # Validate fixed count matches
        expected_fixed = len(step_record.fixed_layers or [])
        if len(fixed_filament_ids) != expected_fixed:
            errors.append({
                "step_id": step_id,
                "error": f"STEP requires {expected_fixed} fixed filament(s), got {len(fixed_filament_ids)}",
            })
            continue

        sample_id = store.next_sample_id()
        try:
            sample = store.create_sample(
                sample_id=sample_id,
                step_record=step_record,
                variable_filament=variable_filament,
                fixed_filaments=fixed_filaments,
                notes=notes,
            )
            created.append({"sample_id": sample.sample_id, "name": sample.name, "step_id": step_record.step_id, "step_file": step_record.file_name})
        except ValueError as exc:
            errors.append({"step_id": step_id, "error": str(exc)})

    return {"created": created, "errors": errors}


# ── Fitting ───────────────────────────────────────────────────────────────────


def compute_fit_result(store: DataStore, filament_id: str, params: dict) -> dict:
    """Run a spline fit and return the full response dict.

    Raises ``ValueError`` if no processed samples exist for *filament_id*.
    """
    profiles_dir = store.root / "filaments" / "profiles"

    has_data = any(
        s.filaments.variable == filament_id and s.processing_status == 'processed'
        for s in store.list_samples()
    )
    if not has_data:
        raise ValueError(f"No processed samples for '{filament_id}'")

    # Build exclusion sets from sample data if requested
    excluded_samples = None
    excluded_swatches_map = None
    if params.get("use_exclusions", False):
        excluded_samples = set()
        excluded_swatches_map = {}
        for s in store.list_samples():
            if s.filaments.variable != filament_id:
                continue
            if s.fit_exclude:
                excluded_samples.add(s.sample_id)
            if s.excluded_swatches:
                excluded_swatches_map[s.sample_id] = set(s.excluded_swatches)

    profile, diagnostics = _fitting.fit_spline_profile(
        fid=filament_id,
        store=store,
        profiles_dir=profiles_dir,
        noise_floor_T=params.get("noise_floor_T", 0.008),
        excluded_samples=excluded_samples,
        excluded_swatches=excluded_swatches_map,
        lower_cutoff=params.get("lower_cutoff"),
        upper_cutoff=params.get("upper_cutoff"),
        outlier_threshold=params.get("outlier_threshold", 0.35),
        thin_wins_below=params.get("thin_wins_below", 0.3),
        monotone=params.get("monotone", True),
        include_crosscal=params.get("include_crosscal", False),
    )

    if profile is None:
        return {"ok": False, "error": diagnostics.get("error", "unknown"), "diagnostics": diagnostics}

    # Compute delta E for evaluation
    delta_e = _fitting.compute_delta_e(profile, store, profiles_dir, filament_id)
    avg_de = sum(r["delta_e"] for r in delta_e) / max(len(delta_e), 1)
    max_de = max((r["delta_e"] for r in delta_e), default=0)

    # Build spline curve points for charting (200 points)
    d_max = profile["knots_mm"][-1] * 1.1
    curve_ds = np.linspace(0, d_max, 200).tolist()
    curve = {"d": curve_ds, "T_r": [], "T_g": [], "T_b": []}
    for d in curve_ds:
        T = _fitting.predict(profile, d)
        curve["T_r"].append(float(T[0]))
        curve["T_g"].append(float(T[1]))
        curve["T_b"].append(float(T[2]))

    return {
        "ok": True,
        "profile": _fitting.profile_to_save_dict(profile),
        "diagnostics": diagnostics,
        "delta_e": delta_e,
        "avg_delta_e": avg_de,
        "max_delta_e": max_de,
        "curve": curve,
    }
