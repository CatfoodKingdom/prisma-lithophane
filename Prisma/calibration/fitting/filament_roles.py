"""Filament role classifiers for calibration model fitting."""

from __future__ import annotations

import hashlib
from typing import Any, Collection, Iterable

try:
    from lib.photo_stack_model.bundle import (
        MODEL_WHITE_CLASSIFIER_SCHEMA,
        MODEL_WHITE_CLASSIFIER_VERSION,
    )
    from lib.photo_stack_model.schema import stable_json_hash_payload
except ImportError:  # pragma: no cover - package import from repo root
    from Prisma.lib.photo_stack_model.bundle import (
        MODEL_WHITE_CLASSIFIER_SCHEMA,
        MODEL_WHITE_CLASSIFIER_VERSION,
    )
    from Prisma.lib.photo_stack_model.schema import stable_json_hash_payload


def _list_filaments(store: Any) -> list[Any]:
    lister = getattr(store, "list_filaments", None)
    if not callable(lister):
        raise RuntimeError("model fitting requires store.list_filaments() metadata")
    return list(lister())


def _filament_lookup(store: Any) -> dict[str, Any]:
    filaments = _list_filaments(store)
    return {str(getattr(fil, "filament_id", "") or ""): fil for fil in filaments}


def _sha256_payload(value: Any) -> str:
    return hashlib.sha256(stable_json_hash_payload(value).encode("utf-8")).hexdigest()


def model_white_filament_ids(store: Any) -> frozenset[str]:
    """Return filament ids with ``white_cap_eligible=True``."""

    return frozenset(
        str(getattr(filament, "filament_id", "") or "")
        for filament in _list_filaments(store)
        if getattr(filament, "white_cap_eligible", False)
    )


def filament_role_snapshot(store: Any) -> dict[str, dict[str, Any]]:
    """Return stable color-model role metadata for fingerprinting."""

    snapshot: dict[str, dict[str, Any]] = {}
    for filament in _list_filaments(store):
        fid = str(getattr(filament, "filament_id", "") or "")
        if not fid:
            continue
        snapshot[fid] = {
            "filament_id": fid,
            "white_cap_eligible": bool(getattr(filament, "white_cap_eligible", False)),
            "exclude_from_model": bool(getattr(filament, "exclude_from_model", False)),
        }
    return dict(sorted(snapshot.items()))


def filament_role_snapshot_hash(store: Any) -> str:
    return _sha256_payload(filament_role_snapshot(store))


def photo_stack_bundle_classifier_metadata(
    white_ids: Collection[str],
    snapshot_hash: str,
) -> dict[str, Any]:
    """Return top-level runtime-bundle classifier metadata."""

    return {
        "schema": MODEL_WHITE_CLASSIFIER_SCHEMA,
        "mode": "white_cap_eligible",
        "source": "filament.white_cap_eligible",
        "classifier_version": MODEL_WHITE_CLASSIFIER_VERSION,
        "model_white_filament_ids": sorted(str(fid) for fid in white_ids),
        "model_white_snapshot_hash": str(snapshot_hash),
    }


def require_known_filaments(store: Any, filament_ids: Iterable[str]) -> None:
    """Fail loudly if evidence references a filament absent from the library."""

    known = set(_filament_lookup(store))
    missing = sorted({str(fid) for fid in filament_ids if str(fid) and str(fid) not in known})
    if missing:
        raise RuntimeError(
            "model evidence references filaments absent from the library: "
            + ", ".join(missing)
        )


def is_model_white(fid: str, white_ids: Collection[str]) -> bool:
    return str(fid) in set(str(item) for item in white_ids)
