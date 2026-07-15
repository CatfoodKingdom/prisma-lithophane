"""Installed published-model libraries and atomic active-library selection."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from packaging.version import InvalidVersion, Version

from .standard_model_library import MANIFEST_NAME, validate_standard_model_library


ACTIVE_STATE_NAME = "active-model-library.json"
ACTIVE_STATE_SCHEMA = "prisma-active-model-library"
ACTIVE_STATE_SCHEMA_VERSION = 1
INSTALL_STAGE_PREFIX = ".installing-"
DISK_SAFETY_MARGIN_BYTES = 16 * 1024 * 1024


class ModelLibraryStoreError(RuntimeError):
    """Raised when an installed-library operation cannot complete safely."""


@dataclass(frozen=True)
class ActiveModelLibrary:
    library_id: str
    root: Path
    report: dict[str, Any]


def _canonical_library_id(value: object) -> str:
    text = str(value or "").strip()
    try:
        if str(uuid.UUID(text)) != text:
            raise ValueError
    except ValueError as exc:
        raise ModelLibraryStoreError(f"invalid model-library id: {text!r}") from exc
    return text


def _checked_version(value: object, *, label: str) -> Version:
    try:
        return Version(str(value or "").strip())
    except InvalidVersion as exc:
        raise ModelLibraryStoreError(f"{label} is not a valid version: {value!r}") from exc


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.tmp-{uuid.uuid4().hex}"
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _source_size(path: Path) -> int:
    if path.is_file():
        try:
            with zipfile.ZipFile(path) as archive:
                return sum(int(info.file_size) for info in archive.infolist() if not info.is_dir())
        except (OSError, zipfile.BadZipFile) as exc:
            raise ModelLibraryStoreError("the selected file is not a readable ZIP package") from exc
    if path.is_dir():
        return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
    raise ModelLibraryStoreError(f"model-library source is missing: {path}")


def _require_disk_space(destination_root: Path, required_bytes: int) -> None:
    free = shutil.disk_usage(destination_root).free
    required = int(required_bytes) + DISK_SAFETY_MARGIN_BYTES
    if free < required:
        raise ModelLibraryStoreError(
            f"not enough free space to install the model library "
            f"(need at least {required:,} bytes, have {free:,})"
        )


def _is_zip_link(info: zipfile.ZipInfo) -> bool:
    return stat.S_IFMT((int(info.external_attr) >> 16) & 0xFFFF) == stat.S_IFLNK


def _safe_zip_parts(name: str) -> tuple[str, ...]:
    if "\\" in name:
        raise ModelLibraryStoreError(f"ZIP member must use forward slashes: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ModelLibraryStoreError(f"ZIP contains an unsafe member path: {name!r}")
    if ":" in path.parts[0]:
        raise ModelLibraryStoreError(f"ZIP contains an unsafe member path: {name!r}")
    return path.parts


def _extract_library_zip(package: Path, stage: Path) -> None:
    try:
        archive = zipfile.ZipFile(package)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ModelLibraryStoreError("the selected file is not a readable ZIP package") from exc
    with archive:
        members: list[tuple[zipfile.ZipInfo, tuple[str, ...]]] = []
        manifests: list[tuple[str, ...]] = []
        for info in archive.infolist():
            parts = _safe_zip_parts(info.filename.rstrip("/"))
            if _is_zip_link(info):
                raise ModelLibraryStoreError(f"ZIP member may not be a filesystem link: {info.filename}")
            members.append((info, parts))
            if not info.is_dir() and parts[-1] == MANIFEST_NAME:
                manifests.append(parts)
        if len(manifests) != 1:
            raise ModelLibraryStoreError("model-library ZIP must contain exactly one library manifest")
        prefix = manifests[0][:-1]
        seen: set[tuple[str, ...]] = set()
        for info, parts in members:
            if parts[: len(prefix)] != prefix:
                raise ModelLibraryStoreError("model-library ZIP contains files outside its single library root")
            relative = parts[len(prefix) :]
            if not relative:
                if info.is_dir():
                    continue
                raise ModelLibraryStoreError("model-library ZIP contains an invalid root member")
            if info.is_dir():
                continue
            if relative in seen:
                raise ModelLibraryStoreError(
                    "model-library ZIP contains duplicate output paths: " + "/".join(relative)
                )
            seen.add(relative)
            destination = stage.joinpath(*relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                with archive.open(info) as source, destination.open("xb") as output:
                    shutil.copyfileobj(source, output)
            except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                raise ModelLibraryStoreError(f"could not extract ZIP member: {info.filename}") from exc


@dataclass(frozen=True)
class ModelLibraryStore:
    libraries_root: Path
    workspace_root: Path
    prisma_version: str = "0.1.0"

    def __post_init__(self) -> None:
        object.__setattr__(self, "libraries_root", Path(self.libraries_root).expanduser().resolve())
        object.__setattr__(self, "workspace_root", Path(self.workspace_root).expanduser().resolve())
        _checked_version(self.prisma_version, label="Prisma application version")

    @property
    def active_state_path(self) -> Path:
        return self.workspace_root / ACTIVE_STATE_NAME

    def _library_path(self, library_id: object) -> Path:
        canonical = _canonical_library_id(library_id)
        path = (self.libraries_root / canonical).resolve()
        try:
            path.relative_to(self.libraries_root)
        except ValueError as exc:  # pragma: no cover - UUID validation is the primary guard
            raise ModelLibraryStoreError("model-library path escapes its store") from exc
        return path

    def _validate_compatible(self, path: Path) -> dict[str, Any]:
        try:
            report = validate_standard_model_library(path)
        except Exception as exc:
            raise ModelLibraryStoreError(f"model library is invalid: {exc}") from exc
        app = _checked_version(self.prisma_version, label="Prisma application version")
        minimum = _checked_version(report.get("minimum_prisma_version"), label="minimum compatible Prisma version")
        maximum_raw = report.get("maximum_prisma_version")
        maximum = (
            _checked_version(maximum_raw, label="maximum compatible Prisma version")
            if maximum_raw is not None
            else None
        )
        if app < minimum or (maximum is not None and app > maximum):
            maximum_text = f" through {maximum}" if maximum is not None else " or newer"
            raise ModelLibraryStoreError(
                f"model library requires Prisma {minimum}{maximum_text}; this application is {app}"
            )
        return report

    def validate(self, path: str | Path) -> dict[str, Any]:
        """Validate a library and its compatibility without installing it."""

        return self._validate_compatible(Path(path).expanduser().resolve())

    def _read_active_id(self) -> str | None:
        path = self.active_state_path
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ModelLibraryStoreError(f"active model-library state is unreadable: {path}") from exc
        if not isinstance(payload, dict) or set(payload) != {
            "schema",
            "schema_version",
            "library_id",
            "selected_at",
        }:
            raise ModelLibraryStoreError("active model-library state has an unsupported shape")
        if payload.get("schema") != ACTIVE_STATE_SCHEMA or payload.get("schema_version") != ACTIVE_STATE_SCHEMA_VERSION:
            raise ModelLibraryStoreError("active model-library state has an unsupported schema")
        return _canonical_library_id(payload.get("library_id"))

    def reconcile_staging(self) -> int:
        """Remove only abandoned staging directories created by this store."""

        self.libraries_root.mkdir(parents=True, exist_ok=True)
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        removed = 0
        pattern = re.compile(rf"^{re.escape(INSTALL_STAGE_PREFIX)}[0-9a-f]{{32}}$")
        for path in self.libraries_root.iterdir():
            if path.is_dir() and pattern.fullmatch(path.name):
                if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                    raise ModelLibraryStoreError(f"abandoned staging path may not be a filesystem link: {path}")
                shutil.rmtree(path)
                removed += 1
        state_pattern = re.compile(
            rf"^\.{re.escape(ACTIVE_STATE_NAME)}\.tmp-[0-9a-f]{{32}}$"
        )
        for path in self.workspace_root.iterdir():
            if path.is_file() and state_pattern.fullmatch(path.name):
                if path.is_symlink():
                    raise ModelLibraryStoreError(f"abandoned state staging path may not be a link: {path}")
                path.unlink()
                removed += 1
        return removed

    def install(self, source: str | Path) -> dict[str, Any]:
        """Install one directory or ZIP through staging without replacing an id."""

        source_path = Path(source).expanduser().resolve()
        self.libraries_root.mkdir(parents=True, exist_ok=True)
        required_bytes = _source_size(source_path)
        _require_disk_space(self.libraries_root, required_bytes)
        stage = self.libraries_root / f"{INSTALL_STAGE_PREFIX}{uuid.uuid4().hex}"
        if stage.exists():  # pragma: no cover - UUID collision guard
            raise ModelLibraryStoreError(f"installation staging path already exists: {stage}")
        try:
            if source_path.is_dir():
                source_report = self._validate_compatible(source_path)
                shutil.copytree(source_path, stage)
                expected_id = _canonical_library_id(source_report.get("library_id"))
            else:
                stage.mkdir()
                _extract_library_zip(source_path, stage)
                expected_id = None
            staged_report = self._validate_compatible(stage)
            library_id = _canonical_library_id(staged_report.get("library_id"))
            if expected_id is not None and library_id != expected_id:
                raise ModelLibraryStoreError("staged model-library identity changed during installation")
            target = self._library_path(library_id)
            if target.exists():
                raise ModelLibraryStoreError(f"model library {library_id} is already installed")
            os.replace(stage, target)
            return {**staged_report, "library_root": str(target), "installed": True}
        except Exception:
            if stage.exists():
                shutil.rmtree(stage, ignore_errors=True)
            raise

    def activate(self, library_id: object) -> dict[str, Any]:
        canonical = _canonical_library_id(library_id)
        root = self._library_path(canonical)
        report = self._validate_compatible(root)
        if report.get("library_id") != canonical:
            raise ModelLibraryStoreError("installed directory name does not match its library identity")
        _atomic_write_json(
            self.active_state_path,
            {
                "schema": ACTIVE_STATE_SCHEMA,
                "schema_version": ACTIVE_STATE_SCHEMA_VERSION,
                "library_id": canonical,
                "selected_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        return {**report, "library_root": str(root), "active": True}

    def resolve_active(self) -> ActiveModelLibrary:
        library_id = self._read_active_id()
        if library_id is None:
            raise ModelLibraryStoreError("no active model library is selected")
        root = self._library_path(library_id)
        report = self._validate_compatible(root)
        if report.get("library_id") != library_id:
            raise ModelLibraryStoreError("active directory name does not match its library identity")
        return ActiveModelLibrary(library_id=library_id, root=root, report=report)

    def list(self) -> dict[str, Any]:
        self.libraries_root.mkdir(parents=True, exist_ok=True)
        active_error = None
        try:
            active_id = self._read_active_id()
        except ModelLibraryStoreError as exc:
            active_id = None
            active_error = str(exc)
        entries: list[dict[str, Any]] = []
        for path in sorted(self.libraries_root.iterdir(), key=lambda item: item.name.lower()):
            if not path.is_dir() or path.name.startswith(INSTALL_STAGE_PREFIX):
                continue
            canonical: str | None = None
            try:
                canonical = _canonical_library_id(path.name)
                report = self._validate_compatible(path)
                if report.get("library_id") != canonical:
                    raise ModelLibraryStoreError("directory name does not match manifest identity")
                entries.append({**report, "library_root": str(path), "active": canonical == active_id, "valid": True})
            except ModelLibraryStoreError as exc:
                entries.append({
                    "library_id": canonical,
                    "directory_name": path.name,
                    "library_root": str(path),
                    "active": path.name == active_id,
                    "valid": False,
                    "error": str(exc),
                })
        return {
            "active_library_id": active_id,
            "active_state_error": active_error,
            "libraries": entries,
        }

    def remove(self, library_id: object) -> None:
        canonical = _canonical_library_id(library_id)
        try:
            active_id = self._read_active_id()
        except ModelLibraryStoreError as exc:
            raise ModelLibraryStoreError(
                "cannot remove a library while active selection state is invalid"
            ) from exc
        if active_id == canonical:
            raise ModelLibraryStoreError("the active model library cannot be removed")
        target = self._library_path(canonical)
        if not target.is_dir():
            raise ModelLibraryStoreError(f"model library is not installed: {canonical}")
        shutil.rmtree(target)

    def ensure_seed_installed(self, seed_root: str | Path) -> dict[str, Any]:
        """Install the normal library-format seed and select it only on first use."""

        source = Path(seed_root).expanduser().resolve()
        seed_report = self._validate_compatible(source)
        library_id = _canonical_library_id(seed_report.get("library_id"))
        target = self._library_path(library_id)
        if target.exists():
            installed = self._validate_compatible(target)
            if installed.get("library_id") != library_id:
                raise ModelLibraryStoreError("installed seed directory has the wrong identity")
            report = {**installed, "library_root": str(target), "installed": False}
        else:
            report = self.install(source)
        if not self.active_state_path.exists():
            self.activate(library_id)
            report["activated"] = True
        else:
            report["activated"] = False
        return report
