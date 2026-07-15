"""Publish Calibration's current models without changing their lifecycle.

This module is the one-way ownership boundary between Calibration's mutable
working state and Generator's immutable installed model libraries.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import uuid
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

try:
    from Prisma.lib.model_library_store import (
        DISK_SAFETY_MARGIN_BYTES,
        ModelLibraryStore,
        ModelLibraryStoreError,
    )
    from Prisma.lib.runtime_layout import resolve_runtime_layout
    from Prisma.lib.standard_model_library import (
        StandardModelLibraryError,
        export_standard_model_library,
        standard_model_library_readiness,
        validate_standard_model_library,
    )
except ModuleNotFoundError:  # Direct ``python Prisma/calibration/server.py`` launch.
    from lib.model_library_store import (
        DISK_SAFETY_MARGIN_BYTES,
        ModelLibraryStore,
        ModelLibraryStoreError,
    )
    from lib.runtime_layout import resolve_runtime_layout
    from lib.standard_model_library import (
        StandardModelLibraryError,
        export_standard_model_library,
        standard_model_library_readiness,
        validate_standard_model_library,
    )


PUBLICATION_STAGE_PREFIX = ".publishing-"


class ModelLibraryPublicationError(RuntimeError):
    """Raised when a current Calibration model set cannot be published."""


@dataclass(frozen=True)
class PublicationMetadata:
    library_name: str
    library_version: str
    publisher: str
    minimum_prisma_version: str
    maximum_prisma_version: str | None = None
    description: str = ""
    release_notes: str = ""


@dataclass(frozen=True)
class PublicationPaths:
    staging_root: Path
    published_models_root: Path
    generator_libraries_root: Path
    generator_workspace_root: Path

    def __post_init__(self) -> None:
        for name in (
            "staging_root",
            "published_models_root",
            "generator_libraries_root",
            "generator_workspace_root",
        ):
            object.__setattr__(self, name, Path(getattr(self, name)).expanduser().resolve())


def publication_paths_for_app_root(app_root: str | Path) -> PublicationPaths:
    layout = resolve_runtime_layout(app_root=app_root, environ={}, allow_environment_overrides=False)
    return PublicationPaths(
        staging_root=layout.calibration_workspace_root / ".Model Library Publication",
        published_models_root=layout.calibration_published_models_root,
        generator_libraries_root=layout.generator_model_libraries_root,
        generator_workspace_root=layout.generator_workspace_root,
    )


def readiness(*, data_root: str | Path, sqlite_path: str | Path) -> dict[str, Any]:
    return standard_model_library_readiness(data_root=data_root, sqlite_path=sqlite_path)


def reconcile_publication_staging(staging_root: str | Path) -> int:
    """Remove only abandoned private directories created by this publisher."""

    root = Path(staging_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    pattern = re.compile(rf"^{re.escape(PUBLICATION_STAGE_PREFIX)}[0-9a-f]{{32}}$")
    removed = 0
    for path in root.iterdir():
        if not path.is_dir() or not pattern.fullmatch(path.name):
            continue
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise ModelLibraryPublicationError("abandoned publication staging may not be a filesystem link")
        shutil.rmtree(path)
        removed += 1
    return removed


def _filename_component(value: str, *, fallback: str, maximum: int) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip())
    text = re.sub(r"-+", "-", text).strip(" .-_")
    return (text or fallback)[:maximum]


def public_publication_error_message(exc: Exception) -> str:
    """Return an actionable error without exposing managed/internal paths."""

    message = str(exc).strip() or "Model-library publication failed."
    if (
        re.search(r"[A-Za-z]:[\\/]", message)
        or "\\\\" in message
        or re.search(r"/(?:Users|home)/", message, flags=re.IGNORECASE)
        or ".publishing-" in message
        or ".staging-" in message
    ):
        return "Model-library publication failed because current Calibration artifacts changed or are invalid."
    return message


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _zip_member_sha256(archive: zipfile.ZipFile, name: str) -> str:
    digest = hashlib.sha256()
    with archive.open(name) as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_validated_zip(
    library_root: Path,
    destination: Path,
    *,
    wrapper_name: str,
) -> dict[str, Any]:
    report = validate_standard_model_library(library_root)
    if destination.exists():
        raise ModelLibraryPublicationError("a published package with this identity already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    required = int(report["total_bytes"]) + DISK_SAFETY_MARGIN_BYTES
    free = shutil.disk_usage(destination.parent).free
    if free < required:
        raise ModelLibraryPublicationError(
            "not enough free space to create the model-library package "
            f"(need at least {required:,} bytes, have {free:,})"
        )

    temporary = destination.parent / f".publishing-{uuid.uuid4().hex}.tmp"
    source_files = sorted(path for path in library_root.rglob("*") if path.is_file())
    expected: dict[str, tuple[int, str]] = {}
    finalized = False
    package_bytes = 0
    try:
        with zipfile.ZipFile(
            temporary,
            "x",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            for source in source_files:
                relative = source.relative_to(library_root).as_posix()
                member = f"{wrapper_name}/{relative}"
                archive.write(source, member)
                expected[member] = (source.stat().st_size, _file_sha256(source))
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())

        with zipfile.ZipFile(temporary, "r") as archive:
            if archive.testzip() is not None:
                raise ModelLibraryPublicationError("the completed model-library ZIP failed its integrity check")
            infos = [info for info in archive.infolist() if not info.is_dir()]
            names = [info.filename for info in infos]
            if len(names) != len(set(names)) or set(names) != set(expected):
                raise ModelLibraryPublicationError("the completed model-library ZIP has an unexpected file set")
            for info in infos:
                expected_size, expected_hash = expected[info.filename]
                if info.file_size != expected_size or _zip_member_sha256(archive, info.filename) != expected_hash:
                    raise ModelLibraryPublicationError(
                        "the completed model-library ZIP does not match its validated payload"
                    )
        package_bytes = temporary.stat().st_size

        # The identity-bearing UUID makes the final name unique; replace gives
        # atomic finalization even on portable Windows filesystems that do not
        # support hard links (for example exFAT removable media).
        if destination.exists():
            raise ModelLibraryPublicationError("a published package with this identity already exists")
        os.replace(temporary, destination)
        finalized = True
    except Exception:
        if temporary.exists():
            temporary.unlink()
        if finalized and destination.exists():
            destination.unlink()
        raise

    public_report = {key: value for key, value in report.items() if key != "library_root"}
    return {
        **public_report,
        "package_path": str(destination),
        "package_filename": destination.name,
        "package_bytes": package_bytes,
    }


@contextmanager
def _staged_library(
    *,
    data_root: str | Path,
    sqlite_path: str | Path,
    paths: PublicationPaths,
    metadata: PublicationMetadata,
) -> Iterator[tuple[Path, dict[str, Any]]]:
    paths.staging_root.mkdir(parents=True, exist_ok=True)
    reconcile_publication_staging(paths.staging_root)
    container = paths.staging_root / f"{PUBLICATION_STAGE_PREFIX}{uuid.uuid4().hex}"
    library_root = container / "library"
    container.mkdir()
    try:
        report = export_standard_model_library(
            data_root=data_root,
            sqlite_path=sqlite_path,
            destination=library_root,
            library_name=metadata.library_name,
            library_version=metadata.library_version,
            publisher=metadata.publisher,
            minimum_prisma_version=metadata.minimum_prisma_version,
            maximum_prisma_version=metadata.maximum_prisma_version,
            description=metadata.description,
            release_notes=metadata.release_notes,
        )
        yield library_root, report
    except (StandardModelLibraryError, ModelLibraryStoreError, OSError, zipfile.BadZipFile) as exc:
        raise ModelLibraryPublicationError(str(exc)) from exc
    finally:
        if container.exists():
            shutil.rmtree(container, ignore_errors=True)


def export_library_package(
    *,
    data_root: str | Path,
    sqlite_path: str | Path,
    paths: PublicationPaths,
    metadata: PublicationMetadata,
) -> dict[str, Any]:
    """Create one validated portable ZIP without retaining a mutable copy."""

    with _staged_library(
        data_root=data_root,
        sqlite_path=sqlite_path,
        paths=paths,
        metadata=metadata,
    ) as (library_root, report):
        library_id = str(report["library_id"])
        friendly = "-".join(
            (
                "Prisma-Model-Library",
                _filename_component(metadata.library_name, fallback="Models", maximum=40),
                _filename_component(metadata.library_version, fallback="Unversioned", maximum=24),
                library_id,
            )
        )
        destination = paths.published_models_root / f"{friendly}.zip"
        return _write_validated_zip(library_root, destination, wrapper_name=friendly)


def publish_to_generator(
    *,
    data_root: str | Path,
    sqlite_path: str | Path,
    paths: PublicationPaths,
    metadata: PublicationMetadata,
    prisma_version: str,
) -> dict[str, Any]:
    """Install a new immutable copy; never activate or later mutate it."""

    with _staged_library(
        data_root=data_root,
        sqlite_path=sqlite_path,
        paths=paths,
        metadata=metadata,
    ) as (library_root, _report):
        store = ModelLibraryStore(
            paths.generator_libraries_root,
            paths.generator_workspace_root,
            prisma_version=prisma_version,
        )
        installed = store.install(library_root)
    return {
        key: value
        for key, value in installed.items()
        if key != "library_root"
    } | {"installed": True, "active": False}
