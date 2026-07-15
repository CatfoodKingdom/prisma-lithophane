"""Export and validate database-free Prisma Standard Model Libraries."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from packaging.version import InvalidVersion, Version

from .camera_transform import load_camera_transform, load_inverse_lut
from .photo_stack_model.bundle import (
    DEPLOYMENT_BUNDLE_SCHEMA,
    DEPLOYMENT_BUNDLE_SCHEMA_VERSION,
    build_photo_stack_deployment_bundle,
    load_photo_stack_bundle,
)
from .photo_stack_model.correction_layer import CORRECTION_SCHEMA


LIBRARY_FORMAT = "prisma-standard-model-library"
LIBRARY_SCHEMA_VERSION = 2
MANIFEST_NAME = "prisma-library.json"
REQUIRED_MODEL_KINDS = ("legacy_spline", "photo_stack_v2", "camera_transform")
MODEL_KIND_LABELS = {
    "legacy_spline": "Color Model v1",
    "photo_stack_v2": "Color Model v2",
    "camera_transform": "Camera Transform",
}
PHOTO_DEPLOYMENT_RUN_ID = "published-v2"
CAMERA_DEPLOYMENT_GENERATION = "published-v2"
CAMERA_DEPLOYMENT_MANIFEST_SCHEMA = "prisma_camera_transform_deployment_manifest"
CAMERA_DEPLOYMENT_MANIFEST_SCHEMA_VERSION = 1
PUBLIC_FILAMENT_FIELDS = {
    "display_name",
    "manufacturer",
    "color_name",
    "material",
    "hex",
    "white_cap_eligible",
    "special_roles",
    "exclude_from_model",
    "generation_available",
}
MAX_LIBRARY_NAME_LENGTH = 120
MAX_PUBLISHER_LENGTH = 120
MAX_VERSION_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 2000
MAX_RELEASE_NOTES_LENGTH = 8000
PUBLICATION_DISK_SAFETY_MARGIN_BYTES = 16 * 1024 * 1024
ROOT_MANIFEST_FIELDS = {
    "format",
    "schema_version",
    "library_id",
    "name",
    "library_version",
    "publisher",
    "description",
    "release_notes",
    "compatibility",
    "created_at",
    "filament_count",
    "models",
    "files",
}


class StandardModelLibraryError(RuntimeError):
    """Raised when a Standard Model Library cannot be safely built or used."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StandardModelLibraryError(f"JSON file is missing or invalid: {path}") from exc
    if not isinstance(payload, dict):
        raise StandardModelLibraryError(f"JSON file must contain an object: {path}")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _safe_relative_path(root: Path, value: str, *, label: str) -> tuple[str, Path]:
    text = str(value or "")
    if "\\" in text:
        raise StandardModelLibraryError(f"{label} must use forward slashes: {value!r}")
    relative = PurePosixPath(text)
    if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise StandardModelLibraryError(f"unsafe {label}: {value!r}")
    path = root.joinpath(*relative.parts)
    resolved_root = root.resolve()
    resolved_path = path.resolve()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise StandardModelLibraryError(f"{label} escapes the library root: {value!r}") from exc
    return relative.as_posix(), path


def _reject_linked_path(root: Path, path: Path, *, label: str) -> None:
    current = path
    while current != root:
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise StandardModelLibraryError(f"{label} may not be a filesystem link: {path}")
        current = current.parent


def _database_tables(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _generator_filament_catalog(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    required = {"filaments", "filament_special_roles"}
    missing = required - _database_tables(conn)
    if missing:
        raise StandardModelLibraryError(
            "calibration database is missing filament catalog tables: " + ", ".join(sorted(missing))
        )
    rows = conn.execute(
        """
        SELECT f.filament_id,
               f.name,
               f.manufacturer,
               f.material,
               f.hex_color,
               f.white_cap_eligible,
               f.exclude_from_model,
               COALESCE(
                 (
                   SELECT json_group_array(role_rows.special_role)
                   FROM (
                     SELECT special_role
                     FROM filament_special_roles
                     WHERE filament_id = f.filament_id
                     ORDER BY special_role
                   ) AS role_rows
                 ),
                 '[]'
               ) AS special_roles
        FROM filaments AS f
        ORDER BY f.manufacturer COLLATE NOCASE,
                 f.name COLLATE NOCASE,
                 f.filament_id
        """
    ).fetchall()
    if not rows:
        raise StandardModelLibraryError("calibration database contains no filaments")

    catalog: dict[str, dict[str, Any]] = {}
    for row in rows:
        filament_id = str(row[0] or "").strip()
        if not filament_id or filament_id in catalog:
            raise StandardModelLibraryError(f"invalid or duplicate filament id: {filament_id!r}")
        name = str(row[1] or "")
        manufacturer = str(row[2] or "")
        color_name = name
        if manufacturer and name.lower().startswith(manufacturer.lower()):
            color_name = name[len(manufacturer):].strip(" -") or name
        try:
            raw_roles = json.loads(str(row[7] or "[]"))
        except json.JSONDecodeError as exc:
            raise StandardModelLibraryError(f"invalid special roles for filament {filament_id}") from exc
        special_roles = sorted(
            {
                str(role).strip().lower()
                for role in raw_roles
                if str(role).strip().lower() in {"black", "transparent"}
            }
        )
        excluded = bool(row[6])
        catalog[filament_id] = {
            "display_name": name,
            "manufacturer": manufacturer,
            "color_name": color_name,
            "material": str(row[3] or ""),
            "hex": str(row[4] or ""),
            "white_cap_eligible": bool(row[5]),
            "special_roles": special_roles,
            "exclude_from_model": excluded,
            "generation_available": not excluded,
        }
    return catalog


def _fit_with_artifacts(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    fit = dict(row)
    artifacts = conn.execute(
        """
        SELECT artifact_kind, artifact_rel_path, content_sha256
        FROM model_artifacts
        WHERE model_fit_id = ?
        ORDER BY artifact_rel_path, artifact_kind
        """,
        (fit["model_fit_id"],),
    ).fetchall()
    fit["artifacts"] = [dict(artifact) for artifact in artifacts]
    return fit


def _current_fits(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    required = {"model_fits", "model_artifacts"}
    missing = required - _database_tables(conn)
    if missing:
        raise StandardModelLibraryError(
            "calibration database is missing model lifecycle tables: " + ", ".join(sorted(missing))
        )
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT model_fit_id, model_kind, model_label, generated_at,
               artifact_root_rel_path, code_version
        FROM model_fits
        WHERE currentness_state = 'current'
          AND model_kind IN (?, ?, ?)
        ORDER BY model_kind, generated_at DESC, model_fit_id DESC
        """,
        REQUIRED_MODEL_KINDS,
    ).fetchall()
    by_kind: dict[str, dict[str, Any]] = {}
    for row in rows:
        kind = str(row["model_kind"])
        if kind in by_kind:
            raise StandardModelLibraryError(f"multiple current model fits exist for {kind}")
        fit = _fit_with_artifacts(conn, row)
        by_kind[kind] = fit
    missing_kinds = [kind for kind in REQUIRED_MODEL_KINDS if kind not in by_kind]
    if missing_kinds:
        raise StandardModelLibraryError("no current fit exists for: " + ", ".join(missing_kinds))
    return by_kind


def _publication_snapshot(
    database: Path,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Read the current fits and public catalog from one SQLite snapshot."""

    uri = f"{database.as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        conn.execute("BEGIN")
        return _current_fits(conn), _generator_filament_catalog(conn)


def _snapshot_signature(
    fits: dict[str, dict[str, Any]],
    catalog: dict[str, dict[str, Any]],
) -> str:
    payload = {
        "fits": {
            kind: {
                "model_fit_id": fit.get("model_fit_id"),
                "generated_at": fit.get("generated_at"),
                "artifact_root_rel_path": fit.get("artifact_root_rel_path"),
                "artifacts": [
                    {
                        "artifact_kind": artifact.get("artifact_kind"),
                        "artifact_rel_path": artifact.get("artifact_rel_path"),
                        "content_sha256": artifact.get("content_sha256"),
                    }
                    for artifact in fit.get("artifacts") or []
                ],
            }
            for kind, fit in sorted(fits.items())
        },
        "catalog": catalog,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _require_publication_disk_space(
    destination_parent: Path,
    data_root: Path,
    fits: dict[str, dict[str, Any]],
) -> None:
    registered_paths = {
        str(artifact.get("artifact_rel_path") or "")
        for fit in fits.values()
        for artifact in fit.get("artifacts") or []
    }
    source_bytes = 0
    for relative in registered_paths:
        _rel, path = _safe_relative_path(data_root, relative, label="registered artifact path")
        if path.is_file():
            source_bytes += path.stat().st_size
    # The published payload is smaller than the registered source set, but use
    # the full source size plus a fixed margin as a deliberately conservative
    # preflight estimate.
    required = source_bytes + PUBLICATION_DISK_SAFETY_MARGIN_BYTES
    free = shutil.disk_usage(destination_parent).free
    if free < required:
        raise StandardModelLibraryError(
            "not enough free space to stage the model library "
            f"(need at least {required:,} bytes, have {free:,})"
        )


def standard_model_library_readiness(
    *,
    data_root: str | Path,
    sqlite_path: str | Path,
) -> dict[str, Any]:
    """Report publishability without changing Calibration or creating files."""

    source_root = Path(data_root).expanduser().resolve()
    database = Path(sqlite_path).expanduser().resolve()
    components: dict[str, dict[str, Any]] = {
        kind: {
            "key": kind,
            "label": MODEL_KIND_LABELS[kind],
            "ready": False,
            "status": "missing",
            "reason": "No current fit is available.",
        }
        for kind in REQUIRED_MODEL_KINDS
    }
    components["filament_catalog"] = {
        "key": "filament_catalog",
        "label": "Filament catalog",
        "ready": False,
        "status": "unavailable",
        "reason": "The filament catalog could not be read.",
        "filament_count": 0,
    }

    if not source_root.is_dir():
        return {
            "ready": False,
            "components": components,
            "blocking_reasons": ["Calibration's managed model-artifact storage is unavailable."],
        }
    if not database.is_file():
        return {
            "ready": False,
            "components": components,
            "blocking_reasons": ["Calibration's database is unavailable."],
        }

    blocking: list[str] = []
    fits: dict[str, dict[str, Any]] = {}
    try:
        with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            tables = _database_tables(conn)
            if "model_fits" not in tables or "model_artifacts" not in tables:
                raise StandardModelLibraryError("model lifecycle tables are unavailable")
            for kind in REQUIRED_MODEL_KINDS:
                rows = conn.execute(
                    """
                    SELECT model_fit_id, model_kind, model_label, currentness_state,
                           generated_at, artifact_root_rel_path, code_version
                    FROM model_fits
                    WHERE model_kind = ?
                    ORDER BY generated_at DESC, model_fit_id DESC
                    """,
                    (kind,),
                ).fetchall()
                current_rows = [row for row in rows if str(row["currentness_state"]) == "current"]
                if len(current_rows) > 1:
                    components[kind].update(
                        status="invalid",
                        reason="More than one current fit is registered.",
                    )
                    blocking.append(f"{MODEL_KIND_LABELS[kind]} has invalid lifecycle state.")
                elif current_rows:
                    components[kind].update(status="current", reason="")
                    fits[kind] = _fit_with_artifacts(conn, current_rows[0])
                elif rows:
                    components[kind].update(
                        status="stale",
                        reason="The fit is stale and must be rebuilt before publication.",
                    )
                    blocking.append(f"{MODEL_KIND_LABELS[kind]} is stale.")
                else:
                    blocking.append(f"{MODEL_KIND_LABELS[kind]} is missing.")
            try:
                catalog = _generator_filament_catalog(conn)
            except StandardModelLibraryError:
                blocking.append("The filament catalog is missing or invalid.")
            else:
                components["filament_catalog"].update(
                    ready=True,
                    status="ready",
                    reason="",
                    filament_count=len(catalog),
                )
    except StandardModelLibraryError:
        blocking.append("Calibration's model lifecycle data is unavailable.")
        return {
            "ready": False,
            "components": components,
            "blocking_reasons": list(dict.fromkeys(blocking)),
        }
    except sqlite3.Error:
        return {
            "ready": False,
            "components": components,
            "blocking_reasons": ["Calibration's database could not be read."],
        }

    for kind, fit in fits.items():
        try:
            _validate_source_fit_contract(source_root, kind, fit)
            _validate_source_payload_schema(source_root, kind, fit)
        except (StandardModelLibraryError, OSError, ValueError, KeyError):
            components[kind].update(
                ready=False,
                status="invalid",
                reason="Current registered artifacts are incomplete, changed, or invalid.",
            )
            blocking.append(f"{MODEL_KIND_LABELS[kind]} artifacts are incomplete, changed, or invalid.")
        else:
            components[kind].update(ready=True, status="current", reason="")

    if len(fits) == len(REQUIRED_MODEL_KINDS) and all(components[kind]["ready"] for kind in REQUIRED_MODEL_KINDS):
        try:
            _validate_source_contract(source_root, fits)
            _model_manifest_entries(source_root, fits)
        except (StandardModelLibraryError, OSError, ValueError, KeyError):
            blocking.append("The combined model payload is internally inconsistent.")

    return {
        "ready": not blocking and all(item["ready"] for item in components.values()),
        "components": components,
        "blocking_reasons": list(dict.fromkeys(blocking)),
    }


def _validate_source_fit_contract(data_root: Path, kind: str, fit: dict[str, Any]) -> set[str]:
    root_rel, artifact_root = _safe_relative_path(
        data_root,
        str(fit.get("artifact_root_rel_path") or ""),
        label=f"{kind} artifact root",
    )
    if not artifact_root.is_dir():
        raise StandardModelLibraryError(f"current {kind} artifact root is missing: {artifact_root}")
    artifacts = fit.get("artifacts") or []
    if not artifacts:
        raise StandardModelLibraryError(f"current {kind} fit has no registered artifacts")
    paths: set[str] = set()
    for artifact in artifacts:
        rel, source = _safe_relative_path(
            data_root,
            str(artifact.get("artifact_rel_path") or ""),
            label=f"{kind} artifact path",
        )
        if rel in paths:
            raise StandardModelLibraryError(f"artifact path is registered more than once: {rel}")
        paths.add(rel)
        try:
            source.resolve().relative_to(artifact_root.resolve())
        except ValueError as exc:
            raise StandardModelLibraryError(
                f"registered {kind} artifact is outside its artifact root {root_rel}: {rel}"
            ) from exc
        _reject_linked_path(data_root, source, label="source artifact")
        if not source.is_file():
            raise StandardModelLibraryError(f"registered artifact is missing: {source}")
        expected_hash = str(artifact.get("content_sha256") or "").lower()
        if len(expected_hash) != 64 or any(ch not in "0123456789abcdef" for ch in expected_hash):
            raise StandardModelLibraryError(f"registered artifact has no valid SHA-256: {rel}")
        if _sha256_file(source) != expected_hash:
            raise StandardModelLibraryError(f"registered artifact hash mismatch: {rel}")

    if kind == "legacy_spline":
        if not any(path.startswith("filaments/profiles/") and path.endswith(".json") for path in paths):
            raise StandardModelLibraryError("current legacy spline fit has no registered profile")
        if "filaments/pair_corrections.json" not in paths:
            raise StandardModelLibraryError("current legacy spline fit is missing pair_corrections.json")
    elif kind == "photo_stack_v2":
        photo_root = PurePosixPath(str(fit["artifact_root_rel_path"]))
        required = {
            "manifest.json",
            "model.json",
            "correction_layer.json",
            "metrics.json",
            "fit_log.json",
            "review_summary.json",
            "evidence_summary.json",
            "sample_predictions.json",
            "runtime_bundle.json",
        }
        missing = sorted(name for name in required if (photo_root / name).as_posix() not in paths)
        if missing:
            raise StandardModelLibraryError("current Photo Stack fit is incomplete: " + ", ".join(missing))
    elif kind == "camera_transform":
        if "camera_transform/CURRENT" not in paths:
            raise StandardModelLibraryError("current Camera Transform fit is missing its registered CURRENT pointer")
        pointer = (data_root / "camera_transform" / "CURRENT").read_text(encoding="utf-8").strip()
        if not pointer or PurePosixPath(pointer).name != pointer or pointer in {".", ".."}:
            raise StandardModelLibraryError("Camera Transform CURRENT pointer is unsafe")
        required = {
            f"camera_transform/{pointer}/camera_transform.json",
            f"camera_transform/{pointer}/inverse_lut_33.npz",
            f"camera_transform/{pointer}/manifest.json",
        }
        missing = sorted(required - paths)
        if missing:
            raise StandardModelLibraryError("current Camera Transform fit is incomplete: " + ", ".join(missing))
    else:
        raise StandardModelLibraryError(f"unsupported model kind: {kind}")
    return paths


def _validate_source_contract(data_root: Path, fits: dict[str, dict[str, Any]]) -> None:
    seen_paths: set[str] = set()
    for kind in REQUIRED_MODEL_KINDS:
        if kind not in fits:
            raise StandardModelLibraryError(f"no current fit exists for: {kind}")
        paths = _validate_source_fit_contract(data_root, kind, fits[kind])
        duplicates = seen_paths & paths
        if duplicates:
            raise StandardModelLibraryError(
                "artifact path is registered across model families: " + ", ".join(sorted(duplicates))
            )
        seen_paths.update(paths)


def _validate_source_payload_schema(data_root: Path, kind: str, fit: dict[str, Any]) -> None:
    if kind == "legacy_spline":
        for artifact in fit["artifacts"]:
            if not str(artifact["artifact_kind"]).startswith("spline_profile:"):
                continue
            relative = PurePosixPath(str(artifact["artifact_rel_path"]))
            payload = _read_json(data_root.joinpath(*relative.parts))
            if not str(payload.get("model") or "").strip():
                raise StandardModelLibraryError(
                    f"current Model v1 profile has no model schema: {relative.as_posix()}"
                )
            schema_version = payload.get("schema_version", 1)
            if not isinstance(schema_version, int) or schema_version < 1:
                raise StandardModelLibraryError(
                    f"current Model v1 profile has an invalid schema version: {relative.as_posix()}"
                )
        pair_payload = _read_json(data_root / "filaments" / "pair_corrections.json")
        if not isinstance(pair_payload.get("pairs", {}), dict):
            raise StandardModelLibraryError("current Model v1 pair corrections have an invalid schema")
        return
    if kind == "photo_stack_v2":
        photo_root = data_root.joinpath(*PurePosixPath(str(fit["artifact_root_rel_path"])).parts)
        try:
            live_bundle = load_photo_stack_bundle(photo_root / "runtime_bundle.json")
            build_photo_stack_deployment_bundle(live_bundle.payload)
        except Exception as exc:
            raise StandardModelLibraryError(f"current Photo Stack runtime bundle is invalid: {exc}") from exc
        correction = _read_json(photo_root / "correction_layer.json")
        if correction.get("schema") != CORRECTION_SCHEMA:
            raise StandardModelLibraryError("current Photo Stack correction layer has an unsupported schema")
        return
    if kind == "camera_transform":
        pointer = (data_root / "camera_transform" / "CURRENT").read_text(encoding="utf-8").strip()
        camera_generation = data_root / "camera_transform" / pointer
        try:
            load_camera_transform(camera_generation, published=True, use_cache=False)
            load_inverse_lut(camera_generation, published=True, use_cache=False)
        except Exception as exc:
            raise StandardModelLibraryError(f"current Camera Transform payload is invalid: {exc}") from exc
        return
    raise StandardModelLibraryError(f"unsupported model kind: {kind}")


def _validate_source_payload_schemas(data_root: Path, fits: dict[str, dict[str, Any]]) -> None:
    for kind in REQUIRED_MODEL_KINDS:
        _validate_source_payload_schema(data_root, kind, fits[kind])


def _fit_fingerprint(fit: dict[str, Any]) -> str:
    digest = hashlib.sha256()
    for artifact in sorted(fit["artifacts"], key=lambda item: str(item["artifact_rel_path"])):
        digest.update(str(artifact["artifact_kind"]).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(artifact["content_sha256"]).lower().encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _model_manifest_entries(data_root: Path, fits: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    legacy_profile = next(
        data_root / Path(*PurePosixPath(str(item["artifact_rel_path"])).parts)
        for item in fits["legacy_spline"]["artifacts"]
        if str(item["artifact_kind"]).startswith("spline_profile:")
    )
    legacy_payload = _read_json(legacy_profile)

    photo_root = data_root / Path(*PurePosixPath(str(fits["photo_stack_v2"]["artifact_root_rel_path"])).parts)
    photo_manifest = _read_json(photo_root / "manifest.json")
    photo_bundle = _read_json(photo_root / "runtime_bundle.json")

    camera_pointer = (data_root / "camera_transform" / "CURRENT").read_text(encoding="utf-8").strip()
    camera_payload = _read_json(data_root / "camera_transform" / camera_pointer / "camera_transform.json")

    schema_details = {
        "legacy_spline": {
            "model_version": str(fits["legacy_spline"].get("code_version") or legacy_payload.get("model") or "legacy_spline"),
            "artifact_schema": str(legacy_payload.get("model") or "spline"),
            "artifact_schema_version": int(legacy_payload.get("schema_version") or 1),
        },
        "photo_stack_v2": {
            "model_version": str(photo_manifest.get("model_version") or fits["photo_stack_v2"].get("code_version") or ""),
            "artifact_schema": DEPLOYMENT_BUNDLE_SCHEMA,
            "artifact_schema_version": DEPLOYMENT_BUNDLE_SCHEMA_VERSION,
            "runtime_fingerprint": str(photo_bundle.get("fingerprint") or ""),
        },
        "camera_transform": {
            "model_version": str(camera_payload.get("model_version") or fits["camera_transform"].get("code_version") or ""),
            "artifact_schema": str(camera_payload.get("schema") or ""),
            "artifact_schema_version": int(camera_payload.get("schema_version") or 1),
        },
    }
    entries: list[dict[str, Any]] = []
    for kind in REQUIRED_MODEL_KINDS:
        fit = fits[kind]
        entries.append(
            {
                "model_kind": kind,
                "source_model_fit_id": str(fit["model_fit_id"]),
                "source_generated_at": str(fit["generated_at"]),
                "source_fingerprint": _fit_fingerprint(fit),
                **schema_details[kind],
            }
        )
    return entries


def _record_file(root: Path, relative_path: str, *, source: dict[str, str]) -> dict[str, Any]:
    rel, path = _safe_relative_path(root, relative_path, label="manifest file path")
    if not path.is_file():
        raise StandardModelLibraryError(f"manifest file is missing: {rel}")
    return {
        "path": rel,
        "byte_size": path.stat().st_size,
        "sha256": _sha256_file(path),
        "source": source,
    }


def _validated_text(value: Any, *, label: str, maximum: int, allow_empty: bool = False) -> str:
    text = str(value or "").strip()
    if not text and not allow_empty:
        raise StandardModelLibraryError(f"library manifest is missing {label}")
    if len(text) > maximum:
        raise StandardModelLibraryError(f"library manifest {label} exceeds {maximum} characters")
    return text


def _reject_machine_paths(payload: Any, *, label: str) -> None:
    def visit(value: Any, location: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                visit(child, f"{location}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{location}[{index}]")
        elif isinstance(value, str):
            text = value.strip()
            if (
                re.match(r"^[A-Za-z]:[\\/]", text)
                or text.startswith(("\\\\", "//"))
                or re.match(r"^/(?:Users|home)/", text, flags=re.IGNORECASE)
            ):
                raise StandardModelLibraryError(
                    f"{label} contains an absolute machine path at {location}"
                )

    visit(payload, label)


def validate_standard_model_library(library_root: str | Path) -> dict[str, Any]:
    """Validate a complete extracted library without consulting SQLite."""
    root = Path(library_root).expanduser().resolve()
    if not root.is_dir():
        raise StandardModelLibraryError(f"library directory is missing: {root}")
    manifest = _read_json(root / MANIFEST_NAME)
    if set(manifest) != ROOT_MANIFEST_FIELDS:
        raise StandardModelLibraryError("library manifest fields do not match schema 2")
    _reject_machine_paths(manifest, label="library manifest")
    if manifest.get("format") != LIBRARY_FORMAT:
        raise StandardModelLibraryError("library manifest has an unsupported format")
    if manifest.get("schema_version") != LIBRARY_SCHEMA_VERSION:
        raise StandardModelLibraryError("library manifest has an unsupported schema version")
    library_id = _validated_text(manifest.get("library_id"), label="library_id", maximum=36)
    try:
        if str(uuid.UUID(library_id)) != library_id:
            raise ValueError
    except ValueError as exc:
        raise StandardModelLibraryError("library manifest has an invalid library_id") from exc
    library_name = _validated_text(manifest.get("name"), label="name", maximum=MAX_LIBRARY_NAME_LENGTH)
    publisher = _validated_text(manifest.get("publisher"), label="publisher", maximum=MAX_PUBLISHER_LENGTH)
    library_version = _validated_text(
        manifest.get("library_version"), label="library_version", maximum=MAX_VERSION_LENGTH
    )
    description = _validated_text(
        manifest.get("description"), label="description", maximum=MAX_DESCRIPTION_LENGTH, allow_empty=True
    )
    release_notes = _validated_text(
        manifest.get("release_notes"), label="release_notes", maximum=MAX_RELEASE_NOTES_LENGTH, allow_empty=True
    )
    created_at = _validated_text(manifest.get("created_at"), label="created_at", maximum=80)
    compatibility = manifest.get("compatibility")
    if not isinstance(compatibility, dict):
        raise StandardModelLibraryError("library manifest compatibility must be an object")
    if set(compatibility) != {"minimum_prisma_version", "maximum_prisma_version"}:
        raise StandardModelLibraryError("library manifest compatibility fields are invalid")
    minimum_prisma_version = _validated_text(
        compatibility.get("minimum_prisma_version"),
        label="compatibility.minimum_prisma_version",
        maximum=MAX_VERSION_LENGTH,
    )
    maximum_prisma_version = compatibility.get("maximum_prisma_version")
    if maximum_prisma_version is not None:
        _validated_text(
            maximum_prisma_version,
            label="compatibility.maximum_prisma_version",
            maximum=MAX_VERSION_LENGTH,
        )
    try:
        minimum_version = Version(minimum_prisma_version)
        maximum_version = Version(str(maximum_prisma_version)) if maximum_prisma_version is not None else None
    except InvalidVersion as exc:
        raise StandardModelLibraryError("library manifest contains an invalid Prisma compatibility version") from exc
    if maximum_version is not None and maximum_version < minimum_version:
        raise StandardModelLibraryError("library manifest maximum Prisma version is below its minimum version")
    if (root / "calibration.sqlite3").exists():
        raise StandardModelLibraryError("a Standard Model Library may not contain calibration.sqlite3")

    models = manifest.get("models")
    if not isinstance(models, list):
        raise StandardModelLibraryError("library manifest models must be a list")
    model_kinds = [str(item.get("model_kind") or "") for item in models if isinstance(item, dict)]
    if sorted(model_kinds) != sorted(REQUIRED_MODEL_KINDS):
        raise StandardModelLibraryError("library manifest must contain exactly the three required model families")
    for item in models:
        if not isinstance(item, dict):
            raise StandardModelLibraryError("library manifest contains an invalid model entry")
        kind = str(item.get("model_kind") or "")
        required_model_fields = {
            "model_kind",
            "source_model_fit_id",
            "source_generated_at",
            "source_fingerprint",
            "model_version",
            "artifact_schema",
            "artifact_schema_version",
        }
        if kind == "photo_stack_v2":
            required_model_fields.add("runtime_fingerprint")
        if set(item) != required_model_fields:
            raise StandardModelLibraryError(f"library manifest has invalid {kind} model fields")
        if not str(item.get("model_version") or "").strip():
            raise StandardModelLibraryError(f"library manifest is missing the {kind} model version")
        if not str(item.get("artifact_schema") or "").strip():
            raise StandardModelLibraryError(f"library manifest is missing the {kind} artifact schema")
        if not isinstance(item.get("artifact_schema_version"), int) or item["artifact_schema_version"] < 1:
            raise StandardModelLibraryError(f"library manifest has an invalid {kind} artifact schema version")
        if not str(item.get("source_model_fit_id") or "").strip():
            raise StandardModelLibraryError(f"library manifest is missing the {kind} source model-fit id")
        fingerprint = str(item.get("source_fingerprint") or "").lower()
        if len(fingerprint) != 64 or any(ch not in "0123456789abcdef" for ch in fingerprint):
            raise StandardModelLibraryError(f"library manifest has an invalid {kind} source fingerprint")

    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise StandardModelLibraryError("library manifest contains no files")
    expected_paths: set[str] = set()
    for item in files:
        if not isinstance(item, dict):
            raise StandardModelLibraryError("library manifest contains an invalid file entry")
        if set(item) != {"path", "byte_size", "sha256", "source"}:
            raise StandardModelLibraryError("library manifest contains invalid file-entry fields")
        source_metadata = item.get("source")
        if not isinstance(source_metadata, dict):
            raise StandardModelLibraryError("library manifest file source must be an object")
        if not set(source_metadata) <= {"type", "model_kind", "artifact_kind"}:
            raise StandardModelLibraryError("library manifest file source contains invalid fields")
        if not str(source_metadata.get("type") or "").strip():
            raise StandardModelLibraryError("library manifest file source is missing its type")
        rel, path = _safe_relative_path(root, str(item.get("path") or ""), label="manifest file path")
        if rel == MANIFEST_NAME or rel in expected_paths:
            raise StandardModelLibraryError(f"duplicate or reserved manifest file path: {rel}")
        if PurePosixPath(rel).name == "calibration.sqlite3":
            raise StandardModelLibraryError("a Standard Model Library may not contain calibration.sqlite3")
        expected_paths.add(rel)
        _reject_linked_path(root, path, label="library file")
        if not path.is_file():
            raise StandardModelLibraryError(f"library file is missing: {rel}")
        if path.stat().st_size != item.get("byte_size"):
            raise StandardModelLibraryError(f"library file size mismatch: {rel}")
        expected_hash = str(item.get("sha256") or "").lower()
        if _sha256_file(path) != expected_hash:
            raise StandardModelLibraryError(f"library file hash mismatch: {rel}")

    actual_paths: set[str] = set()
    for path in root.rglob("*"):
        _reject_linked_path(root, path, label="library entry")
        if path.is_file():
            actual_paths.add(path.relative_to(root).as_posix())
    expected_with_manifest = expected_paths | {MANIFEST_NAME}
    if actual_paths != expected_with_manifest:
        extras = sorted(actual_paths - expected_with_manifest)
        missing = sorted(expected_with_manifest - actual_paths)
        detail = []
        if extras:
            detail.append("unmanifested: " + ", ".join(extras[:8]))
        if missing:
            detail.append("missing: " + ", ".join(missing[:8]))
        raise StandardModelLibraryError("library file set mismatch (" + "; ".join(detail) + ")")

    for rel in sorted(expected_paths):
        if rel.endswith(".json"):
            _reject_machine_paths(
                _read_json(root.joinpath(*PurePosixPath(rel).parts)),
                label=rel,
            )

    registry = _read_json(root / "filaments" / "registry.json")
    if not registry:
        raise StandardModelLibraryError("library filament registry is empty")
    if manifest.get("filament_count") != len(registry):
        raise StandardModelLibraryError("library manifest filament_count does not match the registry")
    for filament_id, record in registry.items():
        if not str(filament_id).strip() or not isinstance(record, dict):
            raise StandardModelLibraryError("library filament registry contains an invalid record")
        if set(record) != PUBLIC_FILAMENT_FIELDS:
            raise StandardModelLibraryError(
                f"library filament registry contains non-public or missing fields for {filament_id}"
            )

    photo_pointer = _read_json(root / "filaments" / "photo_stack_models" / "latest.json")
    if set(photo_pointer) != {"run_id", "path", "model_family", "model_version"}:
        raise StandardModelLibraryError("Photo Stack latest pointer fields are invalid")
    run_id = str(photo_pointer.get("run_id") or photo_pointer.get("path") or "").strip()
    if not run_id or PurePosixPath(run_id).name != run_id:
        raise StandardModelLibraryError("Photo Stack latest pointer is unsafe")
    if photo_pointer.get("path") != run_id or photo_pointer.get("model_family") != "photo_stack":
        raise StandardModelLibraryError("Photo Stack latest pointer metadata is inconsistent")
    if not (root / "filaments" / "photo_stack_models" / run_id).is_dir():
        raise StandardModelLibraryError("Photo Stack latest pointer target is missing")
    runtime_path = root / "filaments" / "photo_stack_models" / run_id / "runtime_bundle.json"
    correction_path = root / "filaments" / "photo_stack_models" / run_id / "correction_layer.json"
    try:
        bundle = load_photo_stack_bundle(runtime_path)
    except Exception as exc:
        raise StandardModelLibraryError(f"Photo Stack deployment bundle is invalid: {exc}") from exc
    if bundle.payload.get("schema") != DEPLOYMENT_BUNDLE_SCHEMA:
        raise StandardModelLibraryError("published Photo Stack payload is not a deployment bundle")
    model_by_kind = {str(item["model_kind"]): item for item in models}
    photo_model = model_by_kind["photo_stack_v2"]
    if (
        photo_model.get("artifact_schema") != bundle.payload.get("schema")
        or photo_model.get("artifact_schema_version") != bundle.payload.get("schema_version")
        or photo_model.get("model_version") != bundle.payload.get("model_version")
        or photo_model.get("runtime_fingerprint") != bundle.fingerprint
        or photo_pointer.get("model_version") != bundle.payload.get("model_version")
    ):
        raise StandardModelLibraryError("Photo Stack manifest metadata does not match its deployment bundle")
    correction = _read_json(correction_path)
    if correction.get("schema") != CORRECTION_SCHEMA:
        raise StandardModelLibraryError("published Photo Stack correction layer has an unsupported schema")

    camera_pointer_path = root / "camera_transform" / "CURRENT"
    try:
        generation = camera_pointer_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise StandardModelLibraryError("Camera Transform CURRENT pointer is missing") from exc
    if not generation or PurePosixPath(generation).name != generation:
        raise StandardModelLibraryError("Camera Transform CURRENT pointer is unsafe")
    if not (camera_pointer_path.parent / generation).is_dir():
        raise StandardModelLibraryError("Camera Transform CURRENT pointer target is missing")
    camera_manifest = _read_json(camera_pointer_path.parent / generation / "manifest.json")
    if set(camera_manifest) != {"schema", "schema_version", "model_version", "artifact_hashes"}:
        raise StandardModelLibraryError("published Camera Transform manifest is not runtime-only")
    if camera_manifest.get("schema") != CAMERA_DEPLOYMENT_MANIFEST_SCHEMA:
        raise StandardModelLibraryError("published Camera Transform manifest has an unsupported schema")
    if camera_manifest.get("schema_version") != CAMERA_DEPLOYMENT_MANIFEST_SCHEMA_VERSION:
        raise StandardModelLibraryError("published Camera Transform manifest has an unsupported schema version")
    try:
        camera_transform = load_camera_transform(
            camera_pointer_path.parent,
            published=True,
            use_cache=False,
        )
        load_inverse_lut(
            camera_pointer_path.parent,
            published=True,
            use_cache=False,
        )
    except Exception as exc:
        raise StandardModelLibraryError(f"published Camera Transform is invalid: {exc}") from exc
    camera_model = model_by_kind["camera_transform"]
    if (
        camera_model.get("artifact_schema") != camera_transform.payload.get("schema")
        or camera_model.get("model_version") != camera_transform.payload.get("model_version")
        or camera_manifest.get("model_version") != camera_transform.payload.get("model_version")
    ):
        raise StandardModelLibraryError("Camera Transform manifest metadata does not match its payload")

    profiles = {
        path for path in expected_paths
        if path.startswith("filaments/profiles/") and path.endswith(".json")
    }
    required_paths = {
        "filaments/registry.json",
        "filaments/pair_corrections.json",
        "filaments/photo_stack_models/latest.json",
        f"filaments/photo_stack_models/{run_id}/runtime_bundle.json",
        f"filaments/photo_stack_models/{run_id}/correction_layer.json",
        "camera_transform/CURRENT",
        f"camera_transform/{generation}/camera_transform.json",
        f"camera_transform/{generation}/inverse_lut_33.npz",
        f"camera_transform/{generation}/manifest.json",
        *profiles,
    }
    if not profiles or expected_paths != required_paths:
        extras = sorted(expected_paths - required_paths)
        missing = sorted(required_paths - expected_paths)
        detail = []
        if not profiles:
            detail.append("no spline profiles")
        if extras:
            detail.append("unexpected payload: " + ", ".join(extras[:8]))
        if missing:
            detail.append("missing payload: " + ", ".join(missing[:8]))
        raise StandardModelLibraryError("library does not match the runtime payload allowlist (" + "; ".join(detail) + ")")
    legacy_payload = _read_json(root.joinpath(*PurePosixPath(sorted(profiles)[0]).parts))
    legacy_model = model_by_kind["legacy_spline"]
    if (
        legacy_model.get("artifact_schema") != legacy_payload.get("model")
        or legacy_model.get("artifact_schema_version") != int(legacy_payload.get("schema_version") or 1)
    ):
        raise StandardModelLibraryError("Model v1 manifest metadata does not match its profiles")

    return {
        "ok": True,
        "library_root": str(root),
        "library_id": library_id,
        "library_name": library_name,
        "publisher": publisher,
        "library_version": library_version,
        "description": description,
        "release_notes": release_notes,
        "created_at": created_at,
        "minimum_prisma_version": minimum_prisma_version,
        "maximum_prisma_version": str(maximum_prisma_version) if maximum_prisma_version is not None else None,
        "file_count": len(files),
        "total_bytes": sum(int(item["byte_size"]) for item in files),
        "filament_count": len(registry),
        "model_kinds": list(REQUIRED_MODEL_KINDS),
    }


def export_standard_model_library(
    *,
    data_root: str | Path,
    sqlite_path: str | Path,
    destination: str | Path,
    library_name: str,
    library_version: str,
    publisher: str,
    minimum_prisma_version: str,
    maximum_prisma_version: str | None = None,
    description: str = "",
    release_notes: str = "",
    library_id: str | None = None,
) -> dict[str, Any]:
    """Stage, validate, and atomically promote a Standard Model Library."""
    source_root = Path(data_root).expanduser().resolve()
    database = Path(sqlite_path).expanduser().resolve()
    target = Path(destination).expanduser().resolve()
    if not source_root.is_dir():
        raise StandardModelLibraryError(f"source data root is missing: {source_root}")
    if not database.is_file():
        raise StandardModelLibraryError(f"source calibration database is missing: {database}")
    if target.exists():
        raise StandardModelLibraryError(f"destination already exists: {target}")
    clean_name = _validated_text(library_name, label="name", maximum=MAX_LIBRARY_NAME_LENGTH)
    clean_version = _validated_text(
        library_version, label="library_version", maximum=MAX_VERSION_LENGTH
    )
    clean_publisher = _validated_text(publisher, label="publisher", maximum=MAX_PUBLISHER_LENGTH)
    clean_minimum = _validated_text(
        minimum_prisma_version,
        label="compatibility.minimum_prisma_version",
        maximum=MAX_VERSION_LENGTH,
    )
    clean_maximum = None
    if maximum_prisma_version is not None:
        clean_maximum = _validated_text(
            maximum_prisma_version,
            label="compatibility.maximum_prisma_version",
            maximum=MAX_VERSION_LENGTH,
        )
    clean_description = _validated_text(
        description, label="description", maximum=MAX_DESCRIPTION_LENGTH, allow_empty=True
    )
    clean_release_notes = _validated_text(
        release_notes, label="release_notes", maximum=MAX_RELEASE_NOTES_LENGTH, allow_empty=True
    )
    clean_library_id = str(uuid.uuid4()) if library_id is None else str(library_id).strip().lower()
    try:
        if str(uuid.UUID(clean_library_id)) != clean_library_id:
            raise ValueError
    except ValueError as exc:
        raise StandardModelLibraryError("library_id must be a canonical UUID") from exc

    target.parent.mkdir(parents=True, exist_ok=True)
    stage = target.parent / f".{target.name}.staging-{uuid.uuid4().hex}"
    if stage.exists():
        raise StandardModelLibraryError(f"staging path unexpectedly exists: {stage}")

    try:
        fits, catalog = _publication_snapshot(database)
        _validate_source_contract(source_root, fits)
        _validate_source_payload_schemas(source_root, fits)
        _require_publication_disk_space(target.parent, source_root, fits)
        stage.mkdir()

        file_records: list[dict[str, Any]] = []

        def copy_registered(
            artifact: dict[str, Any],
            *,
            model_kind: str,
            destination_rel: str | None = None,
        ) -> None:
            source_rel, source = _safe_relative_path(
                source_root,
                str(artifact["artifact_rel_path"]),
                label=f"{model_kind} artifact path",
            )
            output_rel = destination_rel or source_rel
            _output_rel, destination_path = _safe_relative_path(
                stage, output_rel, label="published artifact path"
            )
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination_path)
            record = _record_file(
                stage,
                output_rel,
                source={
                    "type": "registered_model_artifact",
                    "model_kind": model_kind,
                    "artifact_kind": str(artifact["artifact_kind"]),
                },
            )
            if record["sha256"] != str(artifact["content_sha256"]).lower():
                raise StandardModelLibraryError(f"staged artifact hash does not match SQLite: {source_rel}")
            file_records.append(record)

        legacy_artifacts = fits["legacy_spline"]["artifacts"]
        profiles = [
            artifact for artifact in legacy_artifacts
            if str(artifact["artifact_kind"]).startswith("spline_profile:")
        ]
        pair_corrections = next(
            artifact for artifact in legacy_artifacts
            if str(artifact["artifact_rel_path"]) == "filaments/pair_corrections.json"
        )
        for artifact in profiles:
            copy_registered(artifact, model_kind="legacy_spline")
        copy_registered(pair_corrections, model_kind="legacy_spline")

        photo_fit = fits["photo_stack_v2"]
        photo_root_rel = PurePosixPath(str(photo_fit["artifact_root_rel_path"]))
        photo_by_name = {
            PurePosixPath(str(artifact["artifact_rel_path"])).name: artifact
            for artifact in photo_fit["artifacts"]
        }
        source_runtime = source_root.joinpath(*photo_root_rel.parts) / "runtime_bundle.json"
        deployment_runtime = build_photo_stack_deployment_bundle(_read_json(source_runtime))
        deployment_root_rel = f"filaments/photo_stack_models/{PHOTO_DEPLOYMENT_RUN_ID}"
        runtime_rel = f"{deployment_root_rel}/runtime_bundle.json"
        _write_json(stage.joinpath(*PurePosixPath(runtime_rel).parts), deployment_runtime)
        file_records.append(
            _record_file(
                stage,
                runtime_rel,
                source={"type": "derived_runtime_artifact", "model_kind": "photo_stack_v2"},
            )
        )
        copy_registered(
            photo_by_name["correction_layer.json"],
            model_kind="photo_stack_v2",
            destination_rel=f"{deployment_root_rel}/correction_layer.json",
        )

        camera_fit = fits["camera_transform"]
        camera_by_name = {
            PurePosixPath(str(artifact["artifact_rel_path"])).name: artifact
            for artifact in camera_fit["artifacts"]
            if PurePosixPath(str(artifact["artifact_rel_path"])).name != "CURRENT"
        }
        camera_root_rel = f"camera_transform/{CAMERA_DEPLOYMENT_GENERATION}"
        for filename in ("camera_transform.json", "inverse_lut_33.npz"):
            copy_registered(
                camera_by_name[filename],
                model_kind="camera_transform",
                destination_rel=f"{camera_root_rel}/{filename}",
            )
        camera_manifest_rel = f"{camera_root_rel}/manifest.json"
        camera_payload = _read_json(stage / camera_root_rel / "camera_transform.json")
        _write_json(
            stage / camera_manifest_rel,
            {
                "schema": CAMERA_DEPLOYMENT_MANIFEST_SCHEMA,
                "schema_version": CAMERA_DEPLOYMENT_MANIFEST_SCHEMA_VERSION,
                "model_version": str(camera_payload.get("model_version") or camera_fit.get("code_version") or "v2"),
                "artifact_hashes": {
                    "camera_transform.json": _sha256_file(stage / camera_root_rel / "camera_transform.json"),
                    "inverse_lut_33.npz": _sha256_file(stage / camera_root_rel / "inverse_lut_33.npz"),
                },
            },
        )
        file_records.append(
            _record_file(
                stage,
                camera_manifest_rel,
                source={"type": "derived_integrity_manifest", "model_kind": "camera_transform"},
            )
        )
        camera_pointer_rel = "camera_transform/CURRENT"
        camera_pointer = stage / "camera_transform" / "CURRENT"
        camera_pointer.parent.mkdir(parents=True, exist_ok=True)
        camera_pointer.write_text(CAMERA_DEPLOYMENT_GENERATION + "\n", encoding="utf-8")
        file_records.append(
            _record_file(
                stage,
                camera_pointer_rel,
                source={"type": "generated_runtime_pointer", "model_kind": "camera_transform"},
            )
        )

        registry_rel = "filaments/registry.json"
        _write_json(stage / "filaments" / "registry.json", catalog)
        file_records.append(
            _record_file(stage, registry_rel, source={"type": "generated_filament_catalog", "model_kind": "catalog"})
        )

        latest_rel = "filaments/photo_stack_models/latest.json"
        _write_json(
            stage / "filaments" / "photo_stack_models" / "latest.json",
            {
                "run_id": PHOTO_DEPLOYMENT_RUN_ID,
                "path": PHOTO_DEPLOYMENT_RUN_ID,
                "model_family": str(deployment_runtime.get("model_family") or "photo_stack"),
                "model_version": str(deployment_runtime.get("model_version") or photo_fit.get("code_version") or "v2"),
            },
        )
        file_records.append(
            _record_file(stage, latest_rel, source={"type": "generated_runtime_pointer", "model_kind": "photo_stack_v2"})
        )

        manifest = {
            "format": LIBRARY_FORMAT,
            "schema_version": LIBRARY_SCHEMA_VERSION,
            "library_id": clean_library_id,
            "name": clean_name,
            "library_version": clean_version,
            "publisher": clean_publisher,
            "description": clean_description,
            "release_notes": clean_release_notes,
            "compatibility": {
                "minimum_prisma_version": clean_minimum,
                "maximum_prisma_version": clean_maximum,
            },
            "created_at": datetime.now(timezone.utc).isoformat(),
            "filament_count": len(catalog),
            "models": _model_manifest_entries(source_root, fits),
            "files": sorted(file_records, key=lambda item: item["path"]),
        }
        _write_json(stage / MANIFEST_NAME, manifest)
        # Reserve SQLite's writer slot during the final source-state recheck
        # and promotion. This makes it impossible for invalidation/refitting to
        # commit between the recheck and ownership transfer.
        with closing(sqlite3.connect(database, timeout=0.0)) as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                raise StandardModelLibraryError(
                    "Calibration data is busy; wait for the current operation and publish again"
                ) from exc
            confirmed_fits = _current_fits(conn)
            confirmed_catalog = _generator_filament_catalog(conn)
            if _snapshot_signature(confirmed_fits, confirmed_catalog) != _snapshot_signature(fits, catalog):
                raise StandardModelLibraryError(
                    "Calibration models or filament catalog changed during publication; publish again"
                )
            report = validate_standard_model_library(stage)
            for attempt, delay in enumerate((0.0, 0.05, 0.1, 0.2, 0.4, 0.8)):
                if delay:
                    time.sleep(delay)
                try:
                    os.replace(stage, target)
                    break
                except PermissionError:
                    if attempt == 5:
                        raise
            conn.rollback()
        report["library_root"] = str(target)
        return report
    except Exception:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
        raise
