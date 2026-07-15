"""
data_access.py — Read/write operations for all domain objects.

All file I/O is scoped to a configurable DATA_ROOT so the sandbox
is isolated from real project data.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import shutil
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import sys as _sys
from pathlib import Path as _Path
_CAL_DIR = _Path(__file__).resolve().parent
if str(_CAL_DIR) not in _sys.path:
    _sys.path.insert(0, str(_CAL_DIR))
_PRISMA_DIR = _CAL_DIR.parent
if str(_PRISMA_DIR) not in _sys.path:
    _sys.path.insert(0, str(_PRISMA_DIR))

from models import Sample, Filament, Blank, StepDefinitionMeta, StepRecord, StripGeometry
from sample_visuals import remove_sample_visuals
from strips.generator import step_filename
from strips.step_parser import parse_step_filename


_STEP_REGISTRY_IO_LOCK = threading.RLock()
_EXTRACTION_RESULT_IO_LOCK = threading.RLock()


def _replace_with_retry(src, dst, *, retries: int = 8, base_delay: float = 0.05) -> None:
    """``os.replace`` with bounded retry for transient Windows locks.

    On Windows an atomic replace onto a just-written file can fail with
    ``PermissionError`` (WinError 5) when antivirus / the search indexer briefly
    holds the destination open. The replace is safe to retry — the temp source is
    a complete file — so back off and try again rather than failing the write.
    """
    for attempt in range(retries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == retries - 1:
                raise
            time.sleep(base_delay * (attempt + 1))


class DataStore:
    """Scoped data access layer. All paths relative to data_root."""

    def __init__(self, data_root: Path):
        self.root = Path(data_root).resolve()
        self._filament_cache: Optional[tuple[tuple, list[Filament]]] = None
        self._ensure_dirs()
        self._migrate_step_truth_to_registry()

    def _ensure_dirs(self):
        """Create subdirectories if they don't exist."""
        for sub in ["filaments", "filaments/profiles", "samples", "strips",
                    "images", "segmentation", "extraction_results",
                    "blanks", "_system/validation", "_system/step_artifacts"]:
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        self.step_export_dir.mkdir(parents=True, exist_ok=True)

    def _steps_registry_path(self) -> Path:
        return self.system_dir / "steps_registry.json"

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _normalize_strip_geometry(self, strip_geometry: dict | StripGeometry | None = None) -> dict:
        if isinstance(strip_geometry, StripGeometry):
            base = strip_geometry.model_dump()
        else:
            base = dict(strip_geometry or {})
        return {
            "num_swatches": int(base.get("num_swatches", 8) or 8),
            "step_w_mm": float(base.get("step_w_mm", 12.0) or 12.0),
            "step_h_mm": float(base.get("step_h_mm", 20.0) or 20.0),
            "border_mm": float(base.get("border_mm", 3.0) or 3.0),
        }

    def _step_signature_payload(
        self,
        layer_count: int,
        layer_height_mm: float,
        variable_thicknesses_mm: list[float],
        fixed_thicknesses_mm: list[float],
        strip_geometry: dict | StripGeometry | None = None,
    ) -> dict:
        return {
            "layer_count": int(layer_count or 1),
            "layer_height_mm": round(float(layer_height_mm or 0.1), 6),
            "variable_thicknesses_mm": [round(float(v), 6) for v in (variable_thicknesses_mm or [])],
            "fixed_thicknesses_mm": [round(float(v), 6) for v in (fixed_thicknesses_mm or [])],
            "strip_geometry": self._normalize_strip_geometry(strip_geometry),
        }

    def _step_geometry_signature(
        self,
        layer_count: int,
        layer_height_mm: float,
        variable_thicknesses_mm: list[float],
        fixed_thicknesses_mm: list[float],
        strip_geometry: dict | StripGeometry | None = None,
    ) -> str:
        payload = self._step_signature_payload(
            layer_count,
            layer_height_mm,
            variable_thicknesses_mm,
            fixed_thicknesses_mm,
            strip_geometry,
        )
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    def _step_id_from_signature(self, geometry_signature: str) -> str:
        digest = hashlib.sha1(geometry_signature.encode("utf-8")).hexdigest()[:12]
        return f"step_{digest}"

    def _canonical_step_filename(
        self,
        layer_count: int,
        variable_thicknesses_mm: list[float],
        fixed_thicknesses_mm: list[float],
        layer_height_mm: float,
    ) -> str:
        return step_filename(
            int(layer_count or 1),
            [float(v) for v in (variable_thicknesses_mm or [])],
            [float(v) for v in (fixed_thicknesses_mm or [])],
            float(layer_height_mm or 0.1),
        )

    def _managed_step_artifact_path(self, file_name: str) -> Path:
        return self.managed_step_dir / file_name

    def _step_export_path(self, file_name: str) -> Path:
        return self.step_export_dir / file_name

    def _copy_step_file_if_missing(self, source_path: Path, target_path: Path) -> None:
        if not source_path.exists() or target_path.exists():
            return
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target_path)

    def _adopt_legacy_step_artifact(self, canonical_name: str, source_filenames: list[str] | None = None) -> None:
        target_path = self._managed_step_artifact_path(canonical_name)
        if target_path.exists():
            return
        candidate_names = [canonical_name, *(source_filenames or [])]
        seen = set()
        for name in candidate_names:
            if not name or name in seen:
                continue
            seen.add(name)
            source_path = self.legacy_step_dir / name
            if source_path.exists():
                self._copy_step_file_if_missing(source_path, target_path)
                return

    def _sync_step_export_artifacts(self, record: StepRecord) -> list[Path]:
        exported_paths: list[Path] = []
        managed_step_path = self._managed_step_artifact_path(record.file_name)
        export_step_path = self._step_export_path(record.file_name)
        self._copy_step_file_if_missing(managed_step_path, export_step_path)
        if export_step_path.exists():
            exported_paths.append(export_step_path)
        return exported_paths

    def _step_record_from_components(
        self,
        *,
        layer_count: int,
        layer_height_mm: float,
        variable_thicknesses_mm: list[float],
        fixed_thicknesses_mm: list[float],
        strip_geometry: dict | StripGeometry | None = None,
        alias: str = "",
        source_filenames: list[str] | None = None,
        created_at: str | None = None,
        updated_at: str | None = None,
    ) -> StepRecord:
        geometry_signature = self._step_geometry_signature(
            layer_count,
            layer_height_mm,
            variable_thicknesses_mm,
            fixed_thicknesses_mm,
            strip_geometry,
        )
        step_id = self._step_id_from_signature(geometry_signature)
        file_name = self._canonical_step_filename(
            layer_count,
            variable_thicknesses_mm,
            fixed_thicknesses_mm,
            layer_height_mm,
        )
        fixed_layers = [{"thickness_mm": float(v)} for v in (fixed_thicknesses_mm or [])]
        artifact_path = self._managed_step_artifact_path(file_name)
        now = self._now_iso()
        return StepRecord(
            step_id=step_id,
            geometry_signature=geometry_signature,
            file_name=file_name,
            alias=alias or "",
            layer_count=int(layer_count or 1),
            variable_thicknesses_mm=[float(v) for v in (variable_thicknesses_mm or [])],
            fixed_layers=fixed_layers,
            layer_height_mm=float(layer_height_mm or 0.1),
            strip_geometry=StripGeometry(**self._normalize_strip_geometry(strip_geometry)),
            artifact_exists=artifact_path.exists(),
            artifact_path=str(artifact_path.resolve()) if artifact_path.exists() else "",
            source_filenames=sorted({f for f in (source_filenames or []) if f}),
            created_at=created_at or now,
            updated_at=updated_at or now,
        )

    def _step_record_from_strip_definition(self, strip_definition: dict | None) -> StepRecord | None:
        if not strip_definition:
            return None
        variable_thicknesses = strip_definition.get("variable_thicknesses_mm") or []
        if not variable_thicknesses:
            return None
        fixed_thicknesses = strip_definition.get("fixed_thicknesses_mm") or []
        return self._step_record_from_components(
            layer_count=strip_definition.get("n_layers", 1),
            layer_height_mm=strip_definition.get("layer_height_mm", 0.1),
            variable_thicknesses_mm=variable_thicknesses,
            fixed_thicknesses_mm=fixed_thicknesses,
            strip_geometry=strip_definition.get("strip_geometry"),
        )

    def _step_record_from_legacy_filename(self, filename: str) -> StepRecord | None:
        meta = parse_step_filename(filename or "")
        if meta is None:
            return None
        fixed_thicknesses = [fl.get("thickness_mm", 0.0) for fl in (meta.get("fixed_layers") or [])]
        return self._step_record_from_components(
            layer_count=meta.get("layer_count", 1),
            layer_height_mm=meta.get("layer_height_mm", 0.1),
            variable_thicknesses_mm=meta.get("variable_thicknesses_mm") or [],
            fixed_thicknesses_mm=fixed_thicknesses,
            source_filenames=[filename],
        )

    def load_steps_registry(self) -> dict[str, dict]:
        path = self._steps_registry_path()
        if not path.exists():
            return {}
        with _STEP_REGISTRY_IO_LOCK:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
        if isinstance(raw, dict) and "steps" in raw:
            steps = raw.get("steps") or []
        elif isinstance(raw, list):
            steps = raw
        else:
            steps = []
        result = {}
        for item in steps:
            try:
                record = StepRecord(**item)
            except Exception:
                continue
            resolved = record.model_dump()
            artifact_path = self._managed_step_artifact_path(resolved["file_name"])
            resolved["artifact_exists"] = artifact_path.exists()
            resolved["artifact_path"] = (
                str(artifact_path.resolve()) if artifact_path.exists() else ""
            )
            result[record.step_id] = resolved
        return result

    # Fields that are always recomputed at read time and must never be persisted,
    # because their values are machine-specific (absolute filesystem paths) or
    # depend on transient disk state (whether the artifact currently exists).
    _VOLATILE_REGISTRY_FIELDS = ("artifact_path", "artifact_exists")

    def save_steps_registry(self, registry: dict[str, dict]) -> Path:
        path = self._steps_registry_path()
        steps = []
        for step_id in sorted(registry):
            item = {k: v for k, v in registry[step_id].items() if k not in self._VOLATILE_REGISTRY_FIELDS}
            steps.append(item)
        payload = {"steps": steps}
        path.parent.mkdir(parents=True, exist_ok=True)
        with _STEP_REGISTRY_IO_LOCK:
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{path.name}.",
                suffix=".tmp",
                dir=path.parent,
                text=True,
            )
            tmp_path = Path(tmp_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(payload, f, indent=2, ensure_ascii=False)
                    f.write("\n")
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, path)
            except Exception:
                tmp_path.unlink(missing_ok=True)
                raise
        return path

    def _copy_canonical_step_artifact(self, source_name: str, canonical_name: str) -> None:
        source_path = self.legacy_step_dir / source_name
        target_path = self._managed_step_artifact_path(canonical_name)
        if not source_path.exists() or target_path.exists():
            return
        shutil.copy2(source_path, target_path)

    def _migrate_step_truth_to_registry(self) -> None:
        """Reconcile legacy STEP sources into the canonical registry.

        Idempotent: when every legacy source already resolves to an existing
        registry entry (and no bundle/sample still references legacy shapes),
        this method performs no writes. That matters because it runs on every
        DataStore instantiation — unconditional writes would dirty the working
        tree on every server boot.
        """
        registry = self.load_steps_registry()
        records_by_signature = {
            item["geometry_signature"]: step_id
            for step_id, item in registry.items()
            if item.get("geometry_signature")
        }
        legacy_meta = self.load_step_metadata()
        step_dir = self.legacy_step_dir
        now = self._now_iso()
        registry_dirty = False

        def ensure_record(record: StepRecord, alias_hint: str = "") -> StepRecord:
            nonlocal registry_dirty
            existing_id = records_by_signature.get(record.geometry_signature)
            if existing_id:
                existing = dict(registry[existing_id])
                current_sources = set(existing.get("source_filenames") or [])
                merged_sources = current_sources | set(record.source_filenames or [])
                changed = False
                if merged_sources != current_sources:
                    existing["source_filenames"] = sorted(merged_sources)
                    changed = True
                if alias_hint and not existing.get("alias"):
                    existing["alias"] = alias_hint
                    changed = True
                if changed:
                    existing["updated_at"] = now
                    registry[existing_id] = existing
                    registry_dirty = True
                return StepRecord(**existing)
            item = record.model_dump()
            if alias_hint and not item.get("alias"):
                item["alias"] = alias_hint
            item["created_at"] = item.get("created_at") or now
            item["updated_at"] = now
            registry[item["step_id"]] = item
            records_by_signature[item["geometry_signature"]] = item["step_id"]
            registry_dirty = True
            return StepRecord(**item)

        for step_path in sorted(step_dir.glob("*.step")):
            record = self._step_record_from_legacy_filename(step_path.name)
            if record is None:
                continue
            alias_hint = str((legacy_meta.get(step_path.name) or {}).get("alias") or "")
            record = ensure_record(
                StepRecord(**{
                    **record.model_dump(),
                    "source_filenames": sorted(set((record.source_filenames or []) + [step_path.name])),
                }),
                alias_hint=alias_hint,
            )
            self._copy_canonical_step_artifact(step_path.name, record.file_name)

        for sample_path in sorted((self.root / "samples").glob("exp-*.json")):
            with open(sample_path, encoding="utf-8") as f:
                raw = json.load(f)
            record = self._step_record_from_strip_definition(raw.get("strip_definition"))
            if record is None:
                record = self._step_record_from_legacy_filename(raw.get("step_file", ""))
            if record is None:
                continue
            alias_hint = str((legacy_meta.get(raw.get("step_file", "")) or {}).get("alias") or "")
            source_names = list(record.source_filenames or [])
            if raw.get("step_file"):
                source_names.append(raw["step_file"])
            record = ensure_record(
                StepRecord(**{
                    **record.model_dump(),
                    "source_filenames": sorted(set(source_names)),
                }),
                alias_hint=alias_hint,
            )
            if raw.get("step_id") != record.step_id or raw.get("step_file") != record.file_name:
                raw["step_id"] = record.step_id
                raw["step_file"] = record.file_name
                with open(sample_path, "w", encoding="utf-8") as f:
                    json.dump(raw, f, indent=2, ensure_ascii=False)

        bundles_path = self._bundles_path()
        raw_bundles = self._load_bundles()
        changed_bundles = False
        for bundle in raw_bundles:
            step_ids = list(bundle.get("step_ids") or [])
            legacy_step_files = list(bundle.get("step_files") or [])
            for step_file in legacy_step_files:
                record = self._step_record_from_legacy_filename(step_file)
                if record is None:
                    continue
                alias_hint = str((legacy_meta.get(step_file) or {}).get("alias") or "")
                source_names = list(record.source_filenames or [])
                source_names.append(step_file)
                record = ensure_record(
                    StepRecord(**{
                        **record.model_dump(),
                        "source_filenames": sorted(set(source_names)),
                    }),
                    alias_hint=alias_hint,
                )
                if record.step_id not in step_ids:
                    step_ids.append(record.step_id)
            derived_files = []
            for step_id in step_ids:
                item = registry.get(step_id)
                if item:
                    derived_files.append(item["file_name"])
            if bundle.get("step_ids") != step_ids or bundle.get("step_files") != derived_files:
                bundle["step_ids"] = step_ids
                bundle["step_files"] = derived_files
                changed_bundles = True
        if changed_bundles:
            with open(bundles_path, "w", encoding="utf-8") as f:
                json.dump({"bundles": raw_bundles}, f, indent=2, ensure_ascii=False)

        # Finalize alias preference and normalize source_filenames.
        # Only bump updated_at when we actually change a record.
        for step_id, item in list(registry.items()):
            source_filenames = sorted(set(item.get("source_filenames") or []))
            preferred_alias = ""
            if item["file_name"] in legacy_meta:
                preferred_alias = str((legacy_meta.get(item["file_name"]) or {}).get("alias") or "")
            if not preferred_alias:
                for name in source_filenames:
                    alias = str((legacy_meta.get(name) or {}).get("alias") or "")
                    if alias:
                        preferred_alias = alias
                        break
            changed = False
            if preferred_alias and not item.get("alias"):
                item["alias"] = preferred_alias
                changed = True
            if item.get("source_filenames") != source_filenames:
                item["source_filenames"] = source_filenames
                changed = True
            if changed:
                item["updated_at"] = now
                registry[step_id] = item
                registry_dirty = True
            self._adopt_legacy_step_artifact(item["file_name"], source_filenames)

        if registry_dirty:
            self.save_steps_registry(registry)
        for step_id in sorted(registry):
            try:
                self._sync_step_export_artifacts(StepRecord(**registry[step_id]))
            except Exception:
                continue

    @property
    def managed_images_dir(self) -> Path:
        """Server-managed image storage under the data root."""
        return self.root / "images"

    @property
    def inbox_dir(self) -> Path:
        """User-facing raw image inbox adjacent to Prisma/data."""
        return self.root.parent / "inbox"

    @property
    def image_overrides_path(self) -> Path:
        """Per-image display/processing overrides keyed by filename."""
        return self.root / "image_overrides.json"

    @property
    def image_metadata_cache_path(self) -> Path:
        """Cached inbox image metadata keyed by filename."""
        return self.system_dir / "image_metadata.json"

    @property
    def system_dir(self) -> Path:
        """Server-managed internal state not meant for direct user interaction."""
        return self.root / "_system"

    @property
    def managed_step_dir(self) -> Path:
        """Internal cache for managed STEP artifacts."""
        return self.system_dir / "step_artifacts"

    @property
    def step_export_dir(self) -> Path:
        """User-facing export directory for generated STEP/STL files."""
        return self.root.parent / "output" / "steps"

    @property
    def legacy_step_dir(self) -> Path:
        """Legacy server-facing STEP folder kept only for migration compatibility."""
        return self.root / "step"

    def _load_image_overrides(self) -> dict[str, dict]:
        path = self.image_overrides_path
        if not path.exists():
            return {}
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            return {}
        if not isinstance(raw, dict):
            return {}
        return raw

    def _save_image_overrides(self, overrides: dict[str, dict]) -> None:
        with open(self.image_overrides_path, "w", encoding="utf-8") as f:
            json.dump(overrides, f, indent=2)

    def get_image_rotation(self, filename: str) -> int:
        info = self._load_image_overrides().get(filename, {})
        try:
            return int(info.get("rotation_cw", 0)) % 4
        except Exception:
            return 0

    def set_image_rotation(self, filename: str, rotation_cw: int) -> int:
        overrides = self._load_image_overrides()
        rot = int(rotation_cw) % 4
        if rot == 0:
            overrides.pop(filename, None)
        else:
            overrides[filename] = {"rotation_cw": rot}
        self._save_image_overrides(overrides)
        return rot

    def list_image_overrides(self) -> dict[str, dict]:
        """Return persisted per-image override metadata keyed by filename."""
        return self._load_image_overrides()

    def _load_image_metadata_cache(self) -> dict[str, dict]:
        path = self.image_metadata_cache_path
        if not path.exists():
            return {}
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            return {}
        images = raw.get("images", raw) if isinstance(raw, dict) else {}
        return images if isinstance(images, dict) else {}

    def _save_image_metadata_cache(self, cache: dict[str, dict]) -> None:
        path = self.image_metadata_cache_path
        payload = {
            "updated_at": self._now_iso(),
            "images": cache,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)

    # ── Filaments ──────────────────────────────────────────────────────────

    def _invalidate_filament_cache(self) -> None:
        """Drop the cached filament list so the next read hits disk."""
        self._filament_cache = None

    def _profile_state_token(self) -> tuple:
        profiles_dir = self.root / "filaments" / "profiles"
        if not profiles_dir.exists():
            return ()
        return tuple(
            (p.name, p.stat().st_mtime_ns, p.stat().st_size)
            for p in sorted(profiles_dir.glob("*.json"))
        )

    def _profile_path(self, filament_id: str) -> Path:
        return self.root / "filaments" / "profiles" / f"{filament_id}.json"

    @staticmethod
    def _profile_is_stale(profile: dict) -> bool:
        return bool(profile.get("stale") or profile.get("stale_reason") or profile.get("stale_at"))

    def _has_fresh_profile(self, filament_id: str) -> bool:
        return self.get_profile(filament_id) is not None

    def list_filaments(self) -> list[Filament]:
        """Load filament registry and annotate with profile existence.

        Results are cached by registry.json mtime; the cache is discarded
        automatically when the registry or profile files change, or when a
        write operation calls _invalidate_filament_cache().
        """
        reg_path = self.root / "filaments" / "registry.json"
        if not reg_path.exists():
            return []

        cache_key = (reg_path.stat().st_mtime_ns, self._profile_state_token())
        if self._filament_cache is not None:
            cached_key, cached_list = self._filament_cache
            if cached_key == cache_key:
                return cached_list

        with open(reg_path, encoding="utf-8") as f:
            registry = json.load(f)
        result = []
        for fid, info in registry.items():
            result.append(Filament(
                filament_id=fid,
                display_name=info.get("display_name", ""),
                manufacturer=info.get("manufacturer", ""),
                color_name=info.get("color_name", ""),
                material=info.get("material", ""),
                hex=info.get("hex", ""),
                has_profile=self._has_fresh_profile(fid),
                white_cap_eligible=bool(info.get("white_cap_eligible", False)),
                special_roles=list(info.get("special_roles", []) or []),
                exclude_from_model=bool(info.get("exclude_from_model", False)),
                notes=info.get("notes", ""),
            ))

        self._filament_cache = (cache_key, result)
        return result

    def get_filament(self, filament_id: str) -> Optional[Filament]:
        """Load a single filament by ID (O(1) dict lookup via cached list)."""
        lookup = {f.filament_id: f for f in self.list_filaments()}
        return lookup.get(filament_id)

    def add_filament(self, filament_id: str, display_name: str,
                     manufacturer: str, color_name: str, hex_color: str,
                     exclude_from_model: bool = False,
                     material: str = "unknown",
                     white_cap_eligible: bool = False,
                     special_roles: list[str] | None = None,
                     notes: str = "") -> Filament:
        """Add a new filament to the registry. Returns the created Filament.

        Raises ValueError if filament_id already exists.
        """
        reg_path = self.root / "filaments" / "registry.json"
        if reg_path.exists():
            with open(reg_path, encoding="utf-8") as f:
                registry = json.load(f)
        else:
            registry = {}

        if filament_id in registry:
            raise ValueError(f"Filament '{filament_id}' already exists")

        entry = {
            "display_name": display_name,
            "manufacturer": manufacturer,
            "color_name": color_name,
            "material": material,
            "hex": hex_color,
            "white_cap_eligible": bool(white_cap_eligible),
            "special_roles": list(special_roles or []),
            "notes": notes,
        }
        if exclude_from_model:
            entry["exclude_from_model"] = True
        registry[filament_id] = entry

        with open(reg_path, "w", encoding="utf-8") as f:
            json.dump(registry, f, indent=2, ensure_ascii=False)
        self._invalidate_filament_cache()

        return Filament(
            filament_id=filament_id,
            display_name=display_name,
            manufacturer=manufacturer,
            color_name=color_name,
            material=material,
            hex=hex_color,
            has_profile=self._has_fresh_profile(filament_id),
            white_cap_eligible=bool(white_cap_eligible),
            special_roles=list(special_roles or []),
            exclude_from_model=exclude_from_model,
            notes=notes,
        )

    def delete_filament(self, filament_id: str) -> bool:
        """Remove a filament from the registry.

        Returns True if deleted, False if not found.
        Does NOT check for references — caller must validate before calling.
        """
        reg_path = self.root / "filaments" / "registry.json"
        if not reg_path.exists():
            return False
        with open(reg_path, encoding="utf-8") as f:
            registry = json.load(f)
        if filament_id not in registry:
            return False
        del registry[filament_id]
        with open(reg_path, "w", encoding="utf-8") as f:
            json.dump(registry, f, indent=2, ensure_ascii=False)
        self._invalidate_filament_cache()
        return True

    def update_filament(self, filament_id: str, *,
                        manufacturer: str | None = None,
                        color_name: str | None = None,
                        hex_color: str | None = None,
                        exclude_from_model: bool | None = None,
                        material: str | None = None,
                        white_cap_eligible: bool | None = None,
                        special_roles: list[str] | None = None,
                        notes: str | None = None) -> Filament:
        """Update mutable fields of an existing filament. Returns updated Filament.

        Raises ValueError if filament_id does not exist. Fields left None are
        unchanged (so an unrelated edit never clears `exclude_from_model`).
        """
        reg_path = self.root / "filaments" / "registry.json"
        if not reg_path.exists():
            raise ValueError(f"Filament '{filament_id}' not found")

        with open(reg_path, encoding="utf-8") as f:
            registry = json.load(f)

        if filament_id not in registry:
            raise ValueError(f"Filament '{filament_id}' not found")

        entry = registry[filament_id]
        if manufacturer is not None:
            entry["manufacturer"] = manufacturer
        if color_name is not None:
            entry["color_name"] = color_name
        if material is not None:
            entry["material"] = material
        if hex_color is not None:
            entry["hex"] = hex_color
        if white_cap_eligible is not None:
            entry["white_cap_eligible"] = bool(white_cap_eligible)
        if special_roles is not None:
            entry["special_roles"] = list(special_roles)
        if exclude_from_model is not None:
            if exclude_from_model:
                entry["exclude_from_model"] = True
            else:
                entry.pop("exclude_from_model", None)
        if notes is not None:
            entry["notes"] = notes

        # Regenerate display_name from current values
        entry["display_name"] = f"{entry['manufacturer']} {entry['color_name']}"

        with open(reg_path, "w", encoding="utf-8") as f:
            json.dump(registry, f, indent=2, ensure_ascii=False)
        self._invalidate_filament_cache()

        return Filament(
            filament_id=filament_id,
            display_name=entry["display_name"],
            manufacturer=entry["manufacturer"],
            color_name=entry["color_name"],
            material=entry.get("material", ""),
            hex=entry["hex"],
            has_profile=self._has_fresh_profile(filament_id),
            white_cap_eligible=bool(entry.get("white_cap_eligible", False)),
            special_roles=list(entry.get("special_roles", []) or []),
            exclude_from_model=bool(entry.get("exclude_from_model", False)),
            notes=entry.get("notes", ""),
        )

    # ── Samples ────────────────────────────────────────────────────────────

    def list_samples(self) -> list[Sample]:
        """Load all sample JSONs."""
        samples_dir = self.root / "samples"
        samples = []
        for f in sorted(samples_dir.glob("exp-*.json")):
            samples.append(self._load_sample(f))
        return samples

    def list_sample_records_raw(self) -> list[dict]:
        """Raw exp-*.json dicts WITHOUT Pydantic validation, in the same order as
        ``list_samples``.

        Validating 820 samples into full ``Sample`` objects (with nested
        measurement payloads) costs ~16s; the raw JSON read is ~0.06s. The slim
        list endpoint needs only identity/status/summary, so it reads raw and
        skips the validation entirely. The on-disk JSON is exactly
        ``Sample.model_dump(exclude_none=True)`` (see ``save_sample``), so the
        keys match the serialized model.
        """
        samples_dir = self.root / "samples"
        records = []
        for f in sorted(samples_dir.glob("exp-*.json")):
            with open(f, encoding="utf-8") as fh:
                records.append(json.load(fh))
        return records

    def get_sample(self, sample_id: str) -> Optional[Sample]:
        """Load a single sample by ID."""
        path = self.root / "samples" / f"{sample_id}.json"
        if not path.exists():
            return None
        return self._load_sample(path)

    def save_sample(self, sample: Sample) -> Path:
        """Write a sample back to disk."""
        path = self.root / "samples" / f"{sample.sample_id}.json"
        data = sample.model_dump(exclude_none=True)
        # Use sample_id as the canonical on-disk key
        data["sample_id"] = data.pop("sample_id", sample.sample_id)
        if sample.step_id:
            record = self.get_step_record(sample.step_id)
            if record is not None:
                data["step_file"] = record.file_name
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return path

    def delete_sample(self, sample_id: str) -> bool:
        """Delete a sample JSON and its thumbnail directory.

        Returns True if the sample was deleted, False if not found.
        """
        path = self.root / "samples" / f"{sample_id}.json"
        if not path.exists():
            return False
        path.unlink()

        # Clean up thumbnails
        try:
            remove_sample_visuals(self.root, sample_id)
        except OSError:
            pass

        # Clean up the extraction-result sidecar (best-effort; tolerates absence)
        self.delete_extraction_result(sample_id)

        return True

    def next_sample_id(self) -> str:
        """Generate next sample ID from existing files."""
        samples_dir = self.root / "samples"
        max_num = 0
        for f in samples_dir.glob("exp-*.json"):
            stem = f.stem  # e.g. "exp-042"
            parts = stem.split("-", 1)
            if len(parts) == 2 and parts[1].isdigit():
                max_num = max(max_num, int(parts[1]))
        return f"exp-{max_num + 1:03d}"

    def create_sample(self, sample_id: str, step_record: StepRecord,
                      variable_filament: 'Filament',
                      fixed_filaments: list['Filament'],
                      notes: str = "",
                      fixed_thicknesses_mm: list[float] | None = None) -> Sample:
        """Create a new sample/experiment and write it to disk.

        Args:
            sample_id: e.g. "exp-182"
            step_record: canonical STEP record
            variable_filament: Filament object for the variable layer
            fixed_filaments: Filament objects for fixed layers (may be empty)
            notes: optional notes string

        Returns the created Sample.
        Raises ValueError if sample_id already exists.
        """
        path = self.root / "samples" / f"{sample_id}.json"
        if path.exists():
            raise ValueError(f"Sample '{sample_id}' already exists")

        # Build strip_definition from STEP metadata
        variable_thicknesses = step_record.variable_thicknesses_mm
        fixed_layers = step_record.fixed_layers
        layer_height = step_record.layer_height_mm
        layer_count = step_record.layer_count
        fixed_thicknesses = (
            [float(v) for v in fixed_thicknesses_mm]
            if fixed_thicknesses_mm is not None
            else [fl.get("thickness_mm", 0) for fl in fixed_layers]
        )

        from models import classify_mode
        mode = classify_mode(variable_thicknesses)

        # Build auto-name
        vt_str = "-".join(f"{t:.2f}" for t in variable_thicknesses)
        name = f"{variable_filament.filament_id}_{mode}-{vt_str}_lh{layer_height:.2f}"

        from datetime import date
        created_date = date.today().isoformat()

        strip_def = {
            "n_layers": layer_count,
            "layer_height_mm": layer_height,
            "mode": mode,
            "anchor_mm": variable_thicknesses[0] if variable_thicknesses else None,
            "variable_thicknesses_mm": variable_thicknesses,
            "fixed_thicknesses_mm": fixed_thicknesses,
            "strip_geometry": {
                "num_swatches": len(variable_thicknesses) or 8,
                "step_w_mm": 12.0,
                "step_h_mm": 20.0,
                "border_mm": 3.0,
            },
        }

        # Build the sample JSON
        exp_data = {
            "sample_id": sample_id,
            "name": name,
            "created": created_date,
            "filaments": {
                "variable": variable_filament.filament_id,
                "fixed": [f.filament_id for f in fixed_filaments],
            },
            "step_id": step_record.step_id,
            "step_file": step_record.file_name,
            "strip_definition": strip_def,
            "photos": [],
            "blank_image": None,
            "assigned_image": None,
            "assigned_blank_id": None,
            "processing_status": "unassigned",
            "orientation_rots": None,
            "flag_reason": None,
        }

        if notes:
            exp_data["notes"] = notes

        with open(path, "w", encoding="utf-8") as f:
            json.dump(exp_data, f, indent=2)

        return self._load_sample(path)

    def _load_sample(self, path: Path) -> Sample:
        """Parse an experiment JSON into a Sample model."""
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)

        # Map existing experiment schema to Sample model
        filaments = raw.get("filaments", {})
        strip_def = raw.get("strip_definition")

        sample_id = raw.get("sample_id") or raw.get("experiment_id", path.stem)
        step_id = raw.get("step_id", "")
        if not step_id:
            record = self._step_record_from_strip_definition(strip_def) or self._step_record_from_legacy_filename(raw.get("step_file", ""))
            step_id = record.step_id if record else ""
        step_file = raw.get("step_file", "")
        if step_id:
            record = self.get_step_record(step_id)
            if record is not None:
                step_file = record.file_name
        return Sample(
            sample_id=sample_id,
            name=raw.get("name", ""),
            created=raw.get("created", ""),
            filaments={
                "variable": filaments.get("variable", ""),
                "fixed": filaments.get("fixed", []),
            },
            step_id=step_id,
            step_file=step_file,
            roles=raw.get("roles", []),
            strip_definition=strip_def,
            photos=[
                p["filename"] if isinstance(p, dict) else p
                for p in raw.get("photos", [])
            ],
            blank_image=raw.get("blank_image"),
            # New fields default to unset:
            assigned_image=raw.get("assigned_image"),
            assigned_blank_id=raw.get("assigned_blank_id"),
            processing_status=raw.get("processing_status", "unassigned"),
            orientation_rots=raw.get("orientation_rots"),
            measurements=raw.get("measurements"),
            flag_reason=raw.get("flag_reason"),
            review_accepted=raw.get("review_accepted", False),
            fit_exclude=raw.get("fit_exclude", False),
            excluded_swatches=raw.get("excluded_swatches", []),
        )

    # ── Blanks ─────────────────────────────────────────────────────────────

    def list_blanks(self) -> list[Blank]:
        """Load all registered blanks."""
        blanks_dir = self.root / "blanks"
        registry_path = blanks_dir / "registry.json"
        if not registry_path.exists():
            return []
        with open(registry_path, encoding="utf-8") as f:
            raw = json.load(f)
        return [Blank(**b) for b in raw.get("blanks", [])]

    def register_blank(self, blank: Blank) -> Path:
        """Register a new blank and save to registry."""
        blanks_dir = self.root / "blanks"
        registry_path = blanks_dir / "registry.json"

        if registry_path.exists():
            with open(registry_path, encoding="utf-8") as f:
                raw = json.load(f)
        else:
            raw = {"blanks": []}

        raw["blanks"].append(blank.model_dump())
        with open(registry_path, "w", encoding="utf-8") as f:
            json.dump(raw, f, indent=2)
        return registry_path

    def get_blank(self, blank_id: str) -> Optional[Blank]:
        """Return a registered blank by ID."""
        for blank in self.list_blanks():
            if blank.blank_id == blank_id:
                return blank
        return None

    def get_blank_storage_path(self, blank_id: str) -> Optional[Path]:
        """Resolve the stored path of a registered blank image."""
        blank = self.get_blank(blank_id)
        if blank is None:
            return None
        path = self.root / blank.storage_path
        return path if path.exists() else None

    def next_blank_id(self) -> str:
        """Generate next blank ID from existing registrations."""
        blanks = self.list_blanks()
        if not blanks:
            return "blank-001"
        max_num = max(
            int(b.blank_id.split("-")[1])
            for b in blanks
            if b.blank_id.startswith("blank-") and b.blank_id.split("-")[1].isdigit()
        )
        return f"blank-{max_num + 1:03d}"

    # ── Profiles ───────────────────────────────────────────────────────────

    def list_profiles(self, *, include_stale: bool = False) -> list[str]:
        """Return filament IDs that have usable saved profiles."""
        profiles_dir = self.root / "filaments" / "profiles"
        result = []
        for path in sorted(profiles_dir.glob("*.json")):
            if include_stale:
                result.append(path.stem)
                continue
            try:
                profile = json.load(open(path, encoding="utf-8"))
            except Exception:
                continue
            if not self._profile_is_stale(profile):
                result.append(path.stem)
        return result

    def get_profile(self, filament_id: str, *, include_stale: bool = False) -> Optional[dict]:
        """Load a usable profile JSON; stale profiles behave as missing."""
        path = self._profile_path(filament_id)
        if not path.exists():
            return None
        try:
            with open(path, encoding="utf-8") as f:
                profile = json.load(f)
        except Exception:
            return None
        if not include_stale and self._profile_is_stale(profile):
            return None
        return profile

    def flag_profile_stale(self, filament_id: str, reason: str = "") -> bool:
        """Mark a profile as stale (source data changed/deleted). Returns True if profile existed."""
        path = self._profile_path(filament_id)
        if not path.exists():
            return False
        with open(path, encoding="utf-8") as f:
            profile = json.load(f)
        profile["stale"] = True
        profile["stale_at"] = self._now_iso()
        if reason:
            profile["stale_reason"] = reason
        with open(path, "w", encoding="utf-8") as f:
            json.dump(profile, f, indent=2)
        pair_corrections = self.root / "filaments" / "pair_corrections.json"
        pair_corrections.unlink(missing_ok=True)
        self._invalidate_filament_cache()
        return True

    # ── Images / inbox ─────────────────────────────────────────────────────

    def list_images(self) -> list[dict]:
        """List raw/derivative calibration images from the user inbox with basic metadata."""
        from processing.blank_registry import _extract_exif_timestamp

        images_dir = self.inbox_dir
        ignored = self._load_ignored_images()
        overrides = self._load_image_overrides()
        metadata_cache = self._load_image_metadata_cache()
        next_cache: dict[str, dict] = {}
        cache_dirty = False
        results = []
        allowed_exts = {".cr2", ".dng", ".tif", ".tiff"}
        if not images_dir.exists():
            return results
        files = sorted(
            f for f in images_dir.iterdir()
            if f.is_file() and f.suffix.lower() in allowed_exts
        )
        for f in files:
            stat = f.stat()
            cached = metadata_cache.get(f.name) or {}
            if (
                cached.get("mtime_ns") == stat.st_mtime_ns
                and cached.get("size_bytes") == stat.st_size
            ):
                exif_timestamp = cached.get("exif_timestamp")
            else:
                exif_timestamp = _extract_exif_timestamp(f)
                cache_dirty = True
            next_cache[f.name] = {
                "mtime_ns": stat.st_mtime_ns,
                "size_bytes": stat.st_size,
                "exif_timestamp": exif_timestamp,
            }
            results.append({
                "filename": f.name,
                "size_bytes": stat.st_size,
                "path": str(f),
                "exif_timestamp": exif_timestamp,
                "ignored": f.name in ignored,
                "rotation_cw": int((overrides.get(f.name) or {}).get("rotation_cw", 0) or 0) % 4,
            })
        if cache_dirty or set(next_cache) != set(metadata_cache):
            self._save_image_metadata_cache(next_cache)
        return results

    def _load_ignored_images(self) -> set:
        path = self.managed_images_dir / "ignored.json"
        if not path.exists():
            return set()
        with open(path, encoding="utf-8") as f:
            return set(json.load(f).get("filenames", []))

    def set_image_ignored(self, filename: str, ignored: bool):
        path = self.managed_images_dir / "ignored.json"
        current = self._load_ignored_images()
        if ignored:
            current.add(filename)
        else:
            current.discard(filename)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"filenames": sorted(current)}, f, indent=2)

    def get_image_path(self, filename: str) -> Optional[Path]:
        """Resolve an image filename from inbox first, then managed storage."""
        for base in (self.inbox_dir, self.managed_images_dir):
            path = base / filename
            if path.exists():
                return path
        return None

    def promote_image_to_managed(self, filename: str) -> Optional[Path]:
        """Copy a raw inbox image into managed storage after successful processing."""
        source = self.inbox_dir / filename
        if not source.exists():
            managed = self.managed_images_dir / filename
            return managed if managed.exists() else None

        dest = self.managed_images_dir / filename
        if not dest.exists():
            shutil.copy2(source, dest)
        return dest

    # ── Segmentation ───────────────────────────────────────────────────────

    def get_segmentation(self, key: str) -> Optional[dict]:
        """Load saved segmentation margins for an image/sample."""
        path = self.root / "segmentation" / f"{key}.json"
        if not path.exists():
            return None
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def save_segmentation(self, key: str, data: dict) -> Path:
        """Save segmentation margins."""
        path = self.root / "segmentation" / f"{key}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return path

    # ── Extraction results ─────────────────────────────────────────────────

    def _extraction_result_path(self, sample_id: str) -> Path:
        return self.root / "extraction_results" / f"{sample_id}.json"

    def get_extraction_result(self, sample_id: str) -> Optional[dict]:
        """Load the saved extraction-result sidecar for a sample, or None."""
        path = self._extraction_result_path(sample_id)
        if not path.exists():
            return None
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def save_extraction_result(self, sample_id: str, data: dict) -> Path:
        """Atomically write the extraction-result sidecar (temp + fsync + replace).

        Caller passes ``result.model_dump()`` (NOT exclude_none) so nullable buckets
        like ``appearance`` serialize as visible ``null`` — this sidecar is its own
        canonical record, not a ``Sample`` (doc-22).
        """
        path = self._extraction_result_path(sample_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with _EXTRACTION_RESULT_IO_LOCK:
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, text=True,
            )
            tmp_path = Path(tmp_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                    f.write("\n")
                    f.flush()
                    os.fsync(f.fileno())
                _replace_with_retry(tmp_path, path)
            except Exception:
                tmp_path.unlink(missing_ok=True)
                raise
        return path

    def delete_extraction_result(self, sample_id: str) -> bool:
        """Delete the extraction-result sidecar for a sample.

        Returns True if a sidecar existed and was removed, False if absent.
        Used by ``delete_sample`` and by every committed-failure path (doc-29
        §6.4) so a failed reprocess never leaves a sidecar more current than the
        sample.
        """
        path = self._extraction_result_path(sample_id)
        with _EXTRACTION_RESULT_IO_LOCK:
            if not path.exists():
                return False
            path.unlink(missing_ok=True)
        return True

    def set_extraction_review_state(
        self, sample_id: str, state: str, notes: str = ""
    ) -> Optional[dict]:
        """Set the sidecar review_state (lockstep with Sample.review_accepted).

        ``state`` must be "pending_review" or "accepted" (reject = discard, never
        a persisted state). Returns the updated record dict, or None if no sidecar
        exists (old samples may predate Step 2). Read-modify-write under the
        extraction-result lock so it cannot race a sidecar save (doc-29 §6.2).
        """
        if state not in ("pending_review", "accepted"):
            raise ValueError(f"invalid review_state: {state!r}")
        with _EXTRACTION_RESULT_IO_LOCK:
            result = self.get_extraction_result(sample_id)
            if result is None:
                return None
            result["review_state"] = state
            result["reviewed_at"] = self._now_iso() if state == "accepted" else None
            if notes:
                result["review_notes"] = notes
            self.save_extraction_result(sample_id, result)
            return result

    def snapshot_extraction_result(self, sample_id: str) -> Optional[bytes]:
        """Read the current sidecar bytes (or None if absent) for atomic rollback.

        Used by the producer commit transaction (doc-29 §6.5): the prior sidecar
        is snapshotted before being overwritten so an aborted commit can restore
        it byte-for-byte.
        """
        path = self._extraction_result_path(sample_id)
        with _EXTRACTION_RESULT_IO_LOCK:
            if not path.exists():
                return None
            return path.read_bytes()

    def restore_extraction_result(self, sample_id: str, snapshot: Optional[bytes]) -> None:
        """Restore a sidecar from snapshot bytes, or delete it if snapshot is None.

        Mirrors ``save_extraction_result``'s temp + fsync + ``os.replace`` pattern
        but writes the exact prior bytes, so the restored sidecar is byte-identical
        to the snapshot (doc-29 §6.5). ``snapshot=None`` means there was no prior
        sidecar, so the newly written one is removed.
        """
        path = self._extraction_result_path(sample_id)
        with _EXTRACTION_RESULT_IO_LOCK:
            if snapshot is None:
                path.unlink(missing_ok=True)
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".tmp", dir=path.parent,
            )
            tmp_path = Path(tmp_name)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(snapshot)
                    f.flush()
                    os.fsync(f.fileno())
                _replace_with_retry(tmp_path, path)
            except Exception:
                tmp_path.unlink(missing_ok=True)
                raise

    # ── Bundles ─────────────────────────────────────────────────────────────

    def _bundles_path(self) -> Path:
        return self.root / "bundles.json"

    def _load_bundles(self) -> list[dict]:
        path = self._bundles_path()
        if not path.exists():
            return []
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        return raw.get("bundles", [])

    def _save_bundles(self, bundles: list[dict]):
        path = self._bundles_path()
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"bundles": bundles}, f, indent=2, ensure_ascii=False)

    def list_bundles(self) -> list[dict]:
        """Load bundles and derive compatibility step filenames."""
        registry = self.load_steps_registry()
        bundles = self._load_bundles()
        for bundle in bundles:
            step_ids = list(bundle.get("step_ids") or [])
            if not step_ids and bundle.get("step_files"):
                derived_ids = []
                for step_file in bundle.get("step_files", []):
                    record = self.find_step_record(step_file=step_file)
                    if record is not None:
                        derived_ids.append(record.step_id)
                step_ids = derived_ids
            bundle["step_ids"] = step_ids
            bundle["step_files"] = [
                registry[step_id]["file_name"]
                for step_id in step_ids
                if step_id in registry
            ]
        return bundles

    def get_bundle(self, name: str) -> Optional[dict]:
        """Get a single bundle by name."""
        for b in self.list_bundles():
            if b["name"] == name:
                return b
        return None

    def create_bundle(self, name: str, step_ids: list[str] | None = None) -> dict:
        """Create a new bundle. Raises ValueError if name already exists."""
        name = name.strip()
        if not name:
            raise ValueError("Bundle name cannot be empty")
        bundles = self._load_bundles()
        for b in bundles:
            if b["name"] == name:
                raise ValueError(f"Bundle '{name}' already exists")
        step_ids = step_ids or []
        bundle = {
            "name": name,
            "step_ids": step_ids,
            "step_files": [self.get_step_record(step_id).file_name for step_id in step_ids if self.get_step_record(step_id)],
        }
        bundles.append(bundle)
        self._save_bundles(bundles)
        return bundle

    def update_bundle(self, name: str, *,
                      new_name: str | None = None,
                      step_ids: list[str] | None = None) -> dict:
        """Rename and/or update members of a bundle.

        Raises ValueError if bundle not found or new_name conflicts.
        """
        bundles = self._load_bundles()
        target = None
        for b in bundles:
            if b["name"] == name:
                target = b
                break
        if target is None:
            raise ValueError(f"Bundle '{name}' not found")

        if new_name is not None:
            new_name = new_name.strip()
            if not new_name:
                raise ValueError("Bundle name cannot be empty")
            if new_name != name:
                for b in bundles:
                    if b["name"] == new_name:
                        raise ValueError(f"Bundle '{new_name}' already exists")
                target["name"] = new_name

        if step_ids is not None:
            target["step_ids"] = step_ids
            target["step_files"] = [self.get_step_record(step_id).file_name for step_id in step_ids if self.get_step_record(step_id)]

        self._save_bundles(bundles)
        return target

    def delete_bundle(self, name: str) -> bool:
        """Remove a bundle by name. Returns True if deleted."""
        bundles = self._load_bundles()
        new_bundles = [b for b in bundles if b["name"] != name]
        if len(new_bundles) == len(bundles):
            return False
        self._save_bundles(new_bundles)
        return True

    def add_step_to_bundle(self, name: str, step_id: str) -> dict:
        """Add a STEP record to a bundle. Raises ValueError if bundle not found."""
        bundles = self._load_bundles()
        target = None
        for b in bundles:
            if b["name"] == name:
                target = b
                break
        if target is None:
            raise ValueError(f"Bundle '{name}' not found")
        step_ids = target.setdefault("step_ids", [])
        if step_id not in step_ids:
            step_ids.append(step_id)
        target["step_files"] = [self.get_step_record(sid).file_name for sid in step_ids if self.get_step_record(sid)]
        self._save_bundles(bundles)
        return target

    def remove_step_from_bundle(self, name: str, step_id: str) -> dict:
        """Remove a STEP record from a bundle. Raises ValueError if bundle not found."""
        bundles = self._load_bundles()
        target = None
        for b in bundles:
            if b["name"] == name:
                target = b
                break
        if target is None:
            raise ValueError(f"Bundle '{name}' not found")
        target["step_ids"] = [sid for sid in target.get("step_ids", []) if sid != step_id]
        target["step_files"] = [self.get_step_record(sid).file_name for sid in target["step_ids"] if self.get_step_record(sid)]
        self._save_bundles(bundles)
        return target

    # ── STEP Metadata ──────────────────────────────────────────────────────

    def _step_metadata_path(self) -> Path:
        return self.root / "step_metadata.json"

    def load_step_metadata(self) -> dict:
        """Read step_metadata.json. Returns {} if file is missing."""
        path = self._step_metadata_path()
        if not path.exists():
            return {}
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def save_step_metadata(self, metadata: dict):
        """Write the full step metadata dict to disk."""
        path = self._step_metadata_path()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2, ensure_ascii=False)

    def list_step_records(self) -> list[StepRecord]:
        registry = self.load_steps_registry()
        result = []
        for step_id in sorted(registry):
            item = dict(registry[step_id])
            artifact_path = self._managed_step_artifact_path(item["file_name"])
            item["artifact_exists"] = artifact_path.exists()
            item["artifact_path"] = str(artifact_path.resolve()) if artifact_path.exists() else ""
            result.append(StepRecord(**item))
        return result

    def get_step_record(self, step_id: str) -> Optional[StepRecord]:
        registry = self.load_steps_registry()
        item = registry.get(step_id)
        if item is None:
            return None
        artifact_path = self._managed_step_artifact_path(item["file_name"])
        item = dict(item)
        item["artifact_exists"] = artifact_path.exists()
        item["artifact_path"] = str(artifact_path.resolve()) if artifact_path.exists() else ""
        return StepRecord(**item)

    def find_step_record(self, *, step_id: str | None = None, step_file: str | None = None, strip_definition: dict | None = None) -> Optional[StepRecord]:
        if step_id:
            return self.get_step_record(step_id)
        registry = self.load_steps_registry()
        if strip_definition:
            record = self._step_record_from_strip_definition(strip_definition)
            if record and record.step_id in registry:
                return self.get_step_record(record.step_id)
        if step_file:
            filename = Path(step_file).name
            for item in registry.values():
                if item.get("file_name") == filename or filename in (item.get("source_filenames") or []):
                    return self.get_step_record(item["step_id"])
            record = self._step_record_from_legacy_filename(filename)
            if record and record.step_id in registry:
                return self.get_step_record(record.step_id)
        return None

    def update_step_meta(self, step_ref: str, alias: str, bundle: str = ""):
        """Update alias for one STEP record and optionally sync a primary bundle."""
        record = self.find_step_record(step_id=step_ref) or self.find_step_record(step_file=step_ref)
        if record is None:
            raise ValueError(f"STEP '{step_ref}' not found")
        registry = self.load_steps_registry()
        item = dict(registry[record.step_id])
        item["alias"] = alias
        item["updated_at"] = self._now_iso()
        registry[record.step_id] = item
        self.save_steps_registry(registry)
        if bundle:
            bundles = self._load_bundles()
            changed = False
            for entry in bundles:
                if entry.get("name") != bundle:
                    continue
                step_ids = entry.setdefault("step_ids", [])
                if record.step_id not in step_ids:
                    step_ids.append(record.step_id)
                    entry["step_files"] = [self.get_step_record(sid).file_name for sid in step_ids if self.get_step_record(sid)]
                    changed = True
                break
            if changed:
                self._save_bundles(bundles)
        return StepRecord(**registry[record.step_id])

    def ensure_step_artifact(self, step_id: str) -> StepRecord:
        record = self.get_step_record(step_id)
        if record is None:
            raise ValueError(f"STEP '{step_id}' not found")
        artifact_path = self._managed_step_artifact_path(record.file_name)
        from strips.generator import generate_step, generate_stls

        if not artifact_path.exists():
            generate_step(
                variable_thicknesses=record.variable_thicknesses_mm,
                fixed_thicknesses=[layer.get("thickness_mm", 0.0) for layer in (record.fixed_layers or [])],
                output_path=artifact_path,
                layer_height=record.layer_height_mm,
            )
        export_base_name = record.file_name.replace(".step", "")
        export_glob = f"{export_base_name}_*.stl"
        export_stls = list(self.step_export_dir.glob(export_glob))
        if not export_stls:
            generate_stls(
                variable_thicknesses=record.variable_thicknesses_mm,
                fixed_thicknesses=[layer.get("thickness_mm", 0.0) for layer in (record.fixed_layers or [])],
                output_dir=self.step_export_dir,
                base_name=export_base_name,
                layer_height=record.layer_height_mm,
            )
        self._sync_step_export_artifacts(record)
        item = record.model_dump()
        item["artifact_exists"] = artifact_path.exists()
        item["artifact_path"] = str(artifact_path.resolve()) if artifact_path.exists() else ""
        return StepRecord(**item)

    # ── Strips (legacy, read-only for migration) ──────────────────────────

    def list_strip_filaments(self) -> list[str]:
        """Return filament IDs that have strip data."""
        strips_dir = self.root / "strips"
        return sorted(p.stem for p in strips_dir.glob("*.json"))

    def get_strips(self, filament_id: str) -> Optional[dict]:
        """Load legacy strip JSON for a filament."""
        path = self.root / "strips" / f"{filament_id}.json"
        if not path.exists():
            return None
        with open(path, encoding="utf-8") as f:
            return json.load(f)
