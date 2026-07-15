"""Reusable legacy spline fit-all service."""

from __future__ import annotations

import json
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Callable

try:
    from fitting import fitting as _fitting
    from fitting.model_publication import publish_legacy_spline_fit, publish_staged_legacy_spline_fit
except ImportError:  # pragma: no cover - package-style imports
    from Prisma.calibration.fitting import fitting as _fitting
    from Prisma.calibration.fitting.model_publication import publish_legacy_spline_fit, publish_staged_legacy_spline_fit

ProgressCallback = Callable[..., None]
ResultsCallback = Callable[[list[dict[str, Any]]], None]
CancelCheck = Callable[[], bool]


def profile_fit_ids_with_data(samples: list[Any]) -> list[str]:
    fids: set[str] = set()
    for sample in samples:
        if getattr(sample, "processing_status", None) != "processed":
            continue
        fid = getattr(getattr(sample, "filaments", None), "variable", None)
        if fid:
            fids.add(fid)
    return sorted(fids)


def profile_fit_summary(fitted: int, failed: int, skipped: int) -> dict[str, int]:
    return {"fitted": int(fitted), "failed": int(failed), "skipped": int(skipped)}


def collect_spline_exclusions(samples: list[Any]) -> tuple[set[str] | None, dict[str, set[int]] | None]:
    excluded_samples: set[str] = set()
    excluded_swatches: dict[str, set[int]] = {}
    for sample in samples:
        sample_id = str(getattr(sample, "sample_id", "") or "")
        if not sample_id:
            continue
        if getattr(sample, "fit_exclude", False):
            excluded_samples.add(sample_id)
        sw_excl = {int(i) for i in (getattr(sample, "excluded_swatches", None) or [])}
        measurements = getattr(sample, "measurements", None)
        if measurements is not None:
            for swatch in measurements.swatches:
                if getattr(swatch, "fit_state", "") == "excluded":
                    sw_excl.add(int(swatch.swatch_index))
        if sw_excl:
            excluded_swatches[sample_id] = sw_excl
    return (excluded_samples or None), (excluded_swatches or None)


def build_legacy_spline_preflight(store: Any) -> dict[str, Any]:
    samples = store.list_samples()
    target_filaments = profile_fit_ids_with_data(samples)
    excluded_samples, excluded_swatches = collect_spline_exclusions(samples)
    return {
        "target_filaments": target_filaments,
        "target_filament_count": len(target_filaments),
        "sample_count": len(samples),
        "excluded_sample_count": len(excluded_samples or []),
        "excluded_swatch_count": sum(len(items) for items in (excluded_swatches or {}).values()),
    }


def _cancelled(should_cancel: CancelCheck | None) -> bool:
    try:
        return bool(should_cancel and should_cancel())
    except Exception:
        return False


def _run_legacy_spline_fit_all_impl(
    *,
    store: Any,
    profiles_dir: Path | None = None,
    progress_cb: ProgressCallback | None = None,
    results_cb: ResultsCallback | None = None,
    should_cancel: CancelCheck | None = None,
    job_id: str | None = None,
    publish: bool = True,
    _staging_root: Path | None = None,
) -> dict[str, Any]:
    run_started = time.perf_counter()
    profiles_dir = profiles_dir or Path(store.root) / "filaments" / "profiles"
    live_profiles_dir = profiles_dir
    staging_root: Path | None = None
    if publish and callable(getattr(store, "publish_model_fit", None)):
        staging_root = _staging_root or live_profiles_dir.parent / f".legacy-spline-fit-{uuid.uuid4().hex}"
        profiles_dir = staging_root / "profiles"
        if live_profiles_dir.exists():
            shutil.copytree(live_profiles_dir, profiles_dir)
        else:
            profiles_dir.mkdir(parents=True)
    else:
        profiles_dir.mkdir(parents=True, exist_ok=True)

    def discard_staging() -> None:
        if staging_root is not None and staging_root.exists():
            shutil.rmtree(staging_root, ignore_errors=True)

    samples = store.list_samples()
    fids_with_data = profile_fit_ids_with_data(samples)
    total = len(fids_with_data)
    if progress_cb:
        progress_cb(
            phase="collecting",
            message="Collected filaments with processed data",
            current=0,
            total=max(1, total),
            target=None,
            summary=profile_fit_summary(0, 0, 0),
        )

    if not fids_with_data:
        discard_staging()
        return {
            "status": "completed",
            "results": [],
            "fitted": 0,
            "failed": 0,
            "skipped": 0,
            "pair_corrections": None,
            "pair_corrections_error": None,
            "elapsed_seconds": round(time.perf_counter() - run_started, 3),
        }

    if _cancelled(should_cancel):
        discard_staging()
        return {
            "status": "cancelled",
            "results": [],
            "fitted": 0,
            "failed": 0,
            "skipped": 0,
            "pair_corrections": None,
            "pair_corrections_error": None,
            "elapsed_seconds": round(time.perf_counter() - run_started, 3),
        }

    strips_by_filament = _fitting._load_all_strips_from_samples(store, samples=samples)
    fit_excluded_samples, fit_excluded_swatches = collect_spline_exclusions(samples)

    results: list[dict[str, Any]] = []
    fitted = 0
    failed = 0
    skipped = 0

    for index, fid in enumerate(fids_with_data, start=1):
        if _cancelled(should_cancel):
            discard_staging()
            return {
                "status": "cancelled",
                "results": results,
                "fitted": fitted,
                "failed": failed,
                "skipped": skipped,
                "pair_corrections": None,
                "pair_corrections_error": None,
                "elapsed_seconds": round(time.perf_counter() - run_started, 3),
            }
        if progress_cb:
            progress_cb(
                phase="fitting",
                message=f"Fitting {fid}",
                current=index - 1,
                total=total,
                target=fid,
                summary=profile_fit_summary(fitted, failed, skipped),
            )
        fit_started = time.perf_counter()
        try:
            profile, diagnostics = _fitting.fit_spline_profile(
                fid=fid,
                store=store,
                profiles_dir=profiles_dir,
                excluded_samples=fit_excluded_samples,
                excluded_swatches=fit_excluded_swatches,
                strips_by_filament=strips_by_filament,
            )
            if profile is None:
                status = diagnostics.get("error", "unknown") if diagnostics else "unknown"
                result = {
                    "filament_id": fid,
                    "status": status,
                    "fitted": False,
                    "elapsed_seconds": round(time.perf_counter() - fit_started, 3),
                }
                if "no strip" in status.lower():
                    skipped += 1
                else:
                    failed += 1
            else:
                out_path = profiles_dir / f"{fid}.json"
                out_path.write_text(json.dumps(_fitting.profile_to_save_dict(profile), indent=2), encoding="utf-8")
                result = {
                    "filament_id": fid,
                    "status": "ok",
                    "fitted": True,
                    "n_knots": profile.get("n_knots", len(profile.get("knots_mm", []))),
                    "elapsed_seconds": round(time.perf_counter() - fit_started, 3),
                }
                fitted += 1
        except Exception as exc:
            result = {
                "filament_id": fid,
                "status": str(exc),
                "fitted": False,
                "elapsed_seconds": round(time.perf_counter() - fit_started, 3),
            }
            failed += 1

        results.append(result)
        if results_cb:
            results_cb(results)
        if progress_cb:
            progress_cb(
                phase="fitting",
                message=f"Completed {fid}",
                current=index,
                total=total,
                target=fid,
                summary=profile_fit_summary(fitted, failed, skipped),
            )

    if staging_root is not None:
        fitted_ids = {
            str(item.get("filament_id") or "")
            for item in results
            if item.get("fitted")
        }
        for path in profiles_dir.glob("*.json"):
            if path.stem not in fitted_ids:
                path.unlink()

    pair_corrections: dict[str, Any] | None = None
    pair_corrections_error: str | None = None
    if fitted > 0:
        if _cancelled(should_cancel):
            discard_staging()
            return {
                "status": "cancelled",
                "results": results,
                "fitted": fitted,
                "failed": failed,
                "skipped": skipped,
                "pair_corrections": None,
                "pair_corrections_error": None,
                "elapsed_seconds": round(time.perf_counter() - run_started, 3),
            }
        if progress_cb:
            progress_cb(
                phase="pair_corrections",
                message="Computing pair corrections",
                current=total,
                total=total,
                target=None,
                summary=profile_fit_summary(fitted, failed, skipped),
            )
        try:
            pair_corrections = _fitting.compute_and_save_pair_corrections(
                store,
                profiles_dir,
                samples=samples,
                excluded_samples=fit_excluded_samples,
                excluded_swatches=fit_excluded_swatches,
            )
        except Exception as exc:
            pair_corrections_error = str(exc)

    result = {
        "status": "completed" if pair_corrections_error is None else "failed",
        "results": results,
        "fitted": fitted,
        "failed": failed,
        "skipped": skipped,
        "pair_corrections": pair_corrections,
        "pair_corrections_error": pair_corrections_error,
        "elapsed_seconds": round(time.perf_counter() - run_started, 3),
    }
    if publish and fitted > 0 and pair_corrections_error is None:
        if staging_root is not None:
            try:
                publish_staged_legacy_spline_fit(
                    store,
                    staged_profiles_dir=profiles_dir,
                    live_profiles_dir=live_profiles_dir,
                    result=result,
                )
            finally:
                discard_staging()
        else:
            publish_legacy_spline_fit(store, profiles_dir=profiles_dir, result=result)
    else:
        discard_staging()
    return result


def run_legacy_spline_fit_all(
    *,
    store: Any,
    profiles_dir: Path | None = None,
    progress_cb: ProgressCallback | None = None,
    results_cb: ResultsCallback | None = None,
    should_cancel: CancelCheck | None = None,
    job_id: str | None = None,
    publish: bool = True,
) -> dict[str, Any]:
    """Run Fit All and always retire its private staging workspace."""
    live_profiles_dir = profiles_dir or Path(store.root) / "filaments" / "profiles"
    staging_root = None
    if publish and callable(getattr(store, "publish_model_fit", None)):
        staging_root = live_profiles_dir.parent / f".legacy-spline-fit-{uuid.uuid4().hex}"
    try:
        return _run_legacy_spline_fit_all_impl(
            store=store,
            profiles_dir=live_profiles_dir,
            progress_cb=progress_cb,
            results_cb=results_cb,
            should_cancel=should_cancel,
            job_id=job_id,
            publish=publish,
            _staging_root=staging_root,
        )
    finally:
        if staging_root is not None and staging_root.exists():
            shutil.rmtree(staging_root, ignore_errors=True)
