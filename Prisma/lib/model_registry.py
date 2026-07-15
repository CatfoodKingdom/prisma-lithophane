"""Read-only runtime resolution of current calibration products."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path, PurePosixPath


def _database_path(data_root: str | Path) -> Path:
    configured = str(os.environ.get("PRISMA_CALIBRATION_SQLITE_PATH") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    root = Path(data_root).resolve()
    local = root / "calibration.sqlite3"
    if local.is_file():
        return local
    calibration_dir = Path(__file__).resolve().parents[1] / "calibration"
    pointer = calibration_dir / ".sqlite-path"
    asset_pointer = calibration_dir / ".asset-root"
    try:
        asset_text = asset_pointer.read_text(encoding="utf-8").strip()
    except OSError:
        asset_text = ""
    if not asset_text:
        return local
    configured_asset = Path(asset_text).expanduser()
    configured_asset = (configured_asset if configured_asset.is_absolute() else asset_pointer.parent / configured_asset).resolve()
    if configured_asset != root:
        return local
    try:
        text = pointer.read_text(encoding="utf-8").strip()
    except OSError:
        text = ""
    if text:
        path = Path(text).expanduser()
        return (path if path.is_absolute() else pointer.parent / path).resolve()
    return local


def _safe_artifact_path(data_root: Path, rel_path: str) -> Path:
    rel = PurePosixPath(str(rel_path or ""))
    if rel.is_absolute() or not rel.parts or any(part in {"", ".", ".."} for part in rel.parts):
        raise RuntimeError(f"invalid current model artifact path: {rel_path!r}")
    path = data_root.joinpath(*rel.parts).resolve()
    try:
        path.relative_to(data_root)
    except ValueError as exc:
        raise RuntimeError(f"current model artifact escapes the data root: {rel_path!r}") from exc
    return path


def current_model_artifact_root(data_root: str | Path, model_kind: str) -> tuple[bool, Path | None]:
    """Return ``(SQLite authoritative, current artifact root)``."""
    root = Path(data_root).resolve()
    database = _database_path(root)
    if not database.is_file():
        return False, None
    try:
        with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as conn:
            row = conn.execute(
                """
                SELECT artifact_root_rel_path
                FROM model_fits
                WHERE model_kind = ? AND currentness_state = 'current'
                ORDER BY generated_at DESC, model_fit_id DESC
                LIMIT 1
                """,
                (model_kind,),
            ).fetchone()
    except sqlite3.OperationalError:
        return False, None
    if row is None or not row[0]:
        return True, None
    return True, _safe_artifact_path(root, str(row[0]))


def current_model_artifact_path(
    data_root: str | Path,
    model_kind: str,
    artifact_kind: str,
) -> tuple[bool, Path | None]:
    root = Path(data_root).resolve()
    database = _database_path(root)
    if not database.is_file():
        return False, None
    try:
        with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as conn:
            row = conn.execute(
                """
                SELECT ma.artifact_rel_path
                FROM model_fits mf
                JOIN model_artifacts ma ON ma.model_fit_id = mf.model_fit_id
                WHERE mf.model_kind = ?
                  AND mf.currentness_state = 'current'
                  AND ma.artifact_kind = ?
                ORDER BY mf.generated_at DESC, mf.model_fit_id DESC
                LIMIT 1
                """,
                (model_kind, artifact_kind),
            ).fetchone()
    except sqlite3.OperationalError:
        return False, None
    if row is None:
        return True, None
    return True, _safe_artifact_path(root, str(row[0]))


def current_legacy_spline_profiles_dir(data_root: str | Path) -> tuple[bool, Path | None]:
    root = Path(data_root).resolve()
    database = _database_path(root)
    if not database.is_file():
        return False, None
    try:
        with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as conn:
            rows = conn.execute(
                """
                SELECT ma.artifact_rel_path
                FROM model_fits mf
                JOIN model_artifacts ma ON ma.model_fit_id = mf.model_fit_id
                WHERE mf.model_kind = 'legacy_spline'
                  AND mf.currentness_state = 'current'
                  AND ma.artifact_kind LIKE 'spline_profile:%'
                """
            ).fetchall()
    except sqlite3.OperationalError:
        return False, None
    if not rows:
        return True, None
    parents = {_safe_artifact_path(root, str(row[0])).parent for row in rows}
    if len(parents) != 1:
        raise RuntimeError("current spline profile artifacts do not share one directory")
    return True, parents.pop()


def _compat_color_name(*, name: str, manufacturer: str) -> str:
    if manufacturer and name.lower().startswith(manufacturer.lower()):
        stripped = name[len(manufacturer):].strip(" -")
        if stripped:
            return stripped
    return name


def current_filament_catalog(data_root: str | Path) -> tuple[bool, dict[str, dict]]:
    """Return ``(SQLite authoritative, generator-compatible catalog)``.

    A missing database or an older/minimal schema returns ``False`` so a
    generator-only model library can fall back to ``filaments/registry.json``.
    """
    root = Path(data_root).resolve()
    database = _database_path(root)
    if not database.is_file():
        return False, {}
    try:
        with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as conn:
            rows = conn.execute(
                """
                SELECT f.filament_id,
                       f.name,
                       f.manufacturer,
                       f.material,
                       f.hex_color,
                       f.white_cap_eligible,
                       f.exclude_from_model,
                       f.notes,
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
    except sqlite3.OperationalError:
        return False, {}

    catalog: dict[str, dict] = {}
    for row in rows:
        filament_id = str(row[0])
        name = str(row[1] or "")
        manufacturer = str(row[2] or "")
        try:
            special_roles = json.loads(str(row[8] or "[]"))
        except json.JSONDecodeError:
            special_roles = []
        special_roles = sorted(
            {
                str(role).strip().lower()
                for role in special_roles
                if str(role).strip().lower() in {"black", "transparent"}
            }
        )
        excluded = bool(row[6])
        catalog[filament_id] = {
            "display_name": name,
            "manufacturer": manufacturer,
            "color_name": _compat_color_name(name=name, manufacturer=manufacturer),
            "material": str(row[3] or ""),
            "hex": str(row[4] or ""),
            "white_cap_eligible": bool(row[5]),
            "special_roles": special_roles,
            "exclude_from_model": excluded,
            "generation_available": not excluded,
            "notes": str(row[7] or ""),
        }
    return True, catalog
