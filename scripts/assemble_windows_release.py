"""Stage, validate, and zip a self-contained Prisma Windows release."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Prisma.lib.standard_model_library import (  # noqa: E402
    StandardModelLibraryError,
    validate_standard_model_library,
)
from scripts.collect_third_party_licenses import (  # noqa: E402
    LicenseBundleError,
    validate_license_bundle,
)


RELEASE_FORMAT = "prisma-windows-release"
RELEASE_SCHEMA_VERSION = 1
RELEASE_MANIFEST = "prisma-release.json"
README_NAME = "README.txt"
LEGAL_FILES = ("LICENSE", "THIRD_PARTY_NOTICES.md", "ASSET_LICENSES.md")
THIRD_PARTY_LICENSES_DIR = "THIRD_PARTY_LICENSES"

FORBIDDEN_SUFFIXES = {
    ".cr2",
    ".db",
    ".ipynb",
    ".pyc",
    ".pyo",
    ".sqlite",
    ".sqlite3",
}
FORBIDDEN_PARTS = {
    ".git",
    ".idea",
    "__pycache__",
    "build123d",
    "cadquery",
    "calibration",
    "jupyterlab",
    "matplotlib",
    "playwright",
    "plotly",
    "pytest",
    "rawpy",
    "tests",
    "vtk",
}


class WindowsReleaseError(RuntimeError):
    """Raised when a Windows release cannot be assembled safely."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_link(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _safe_manifest_path(root: Path, value: str) -> tuple[str, Path]:
    text = str(value or "")
    if "\\" in text:
        raise WindowsReleaseError(f"release manifest paths must use forward slashes: {text!r}")
    rel = PurePosixPath(text)
    if rel.is_absolute() or not rel.parts or any(part in {"", ".", ".."} for part in rel.parts):
        raise WindowsReleaseError(f"unsafe release manifest path: {text!r}")
    path = root.joinpath(*rel.parts)
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise WindowsReleaseError(f"release manifest path escapes the root: {text!r}") from exc
    return rel.as_posix(), path


def _forbidden_reason(relative_path: str) -> str | None:
    rel = PurePosixPath(relative_path)
    if rel.as_posix().casefold() == "_internal/prisma/generator/app/printers.json":
        return "obsolete bundled printer profile"
    lowered_parts = {part.casefold() for part in rel.parts}
    blocked_parts = sorted(lowered_parts & FORBIDDEN_PARTS)
    if blocked_parts:
        return f"forbidden path component {blocked_parts[0]!r}"
    if rel.suffix.casefold() == ".py" and tuple(part.casefold() for part in rel.parts[:2]) != (
        "_internal",
        "cv2",
    ):
        return "forbidden Python source outside the packaged OpenCV runtime"
    if rel.suffix.casefold() in FORBIDDEN_SUFFIXES:
        return f"forbidden file type {rel.suffix.casefold()!r}"
    return None


def _tree_files(root: Path, *, enforce_release_policy: bool) -> list[Path]:
    if not root.is_dir():
        raise WindowsReleaseError(f"directory is missing: {root}")
    files: list[Path] = []
    for path in root.rglob("*"):
        if _is_link(path):
            raise WindowsReleaseError(f"release trees may not contain filesystem links: {path}")
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if enforce_release_policy:
            reason = _forbidden_reason(rel)
            if reason:
                raise WindowsReleaseError(f"release contains {reason}: {rel}")
        files.append(path)
    return sorted(files, key=lambda item: item.relative_to(root).as_posix())


def _copy_tree_files(source: Path, destination: Path) -> None:
    for source_path in _tree_files(source, enforce_release_policy=False):
        rel = source_path.relative_to(source)
        destination_path = destination / rel
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, destination_path)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _copy_legal_files(destination: Path) -> None:
    for name in LEGAL_FILES:
        source = PROJECT_ROOT / name
        if not source.is_file():
            raise WindowsReleaseError(f"required legal file is missing: {source}")
        shutil.copy2(source, destination / name)


def _release_readme(*, release_version: str, library_version: str) -> str:
    return f"""Prisma {release_version} for Windows

Standard Model Library: {library_version}

1. Extract the entire Prisma folder from the ZIP. Do not run Prisma from
   inside the ZIP preview.
2. Double-click Prisma.exe.
3. Keep the Prisma window open while using the browser interface. Closing the
   window stops the local server.

Prisma runs only on your own computer and binds to 127.0.0.1. It does not
upload your images or host a public website.

Your images, finished lithophanes, installed model libraries, and Generator
Workspace all stay inside this extracted Prisma folder. Do not place the live
folder in Program Files, a cloud-synchronized folder, or a network share.

This build is not code-signed yet, so Windows SmartScreen may show an
unrecognized-app warning during testing. Only use release files obtained from
the official Prisma GitHub Releases page.
"""


def _file_records(root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in _tree_files(root, enforce_release_policy=True):
        rel = path.relative_to(root).as_posix()
        if rel == RELEASE_MANIFEST:
            continue
        records.append(
            {
                "path": rel,
                "byte_size": path.stat().st_size,
                "sha256": _sha256_file(path),
            }
        )
    return records


def validate_windows_release(release_root: str | Path) -> dict[str, Any]:
    root = Path(release_root).expanduser().resolve()
    if not (root / "Prisma.exe").is_file():
        raise WindowsReleaseError(f"Prisma.exe is missing from release root: {root}")
    if not (root / "_internal").is_dir():
        raise WindowsReleaseError(f"PyInstaller _internal directory is missing: {root}")
    for name in LEGAL_FILES:
        if not (root / name).is_file() or _is_link(root / name):
            raise WindowsReleaseError(f"required legal file is missing or linked: {name}")
    try:
        license_report = validate_license_bundle(root / THIRD_PARTY_LICENSES_DIR)
    except LicenseBundleError as exc:
        raise WindowsReleaseError(f"third-party license bundle is invalid: {exc}") from exc
    try:
        library_report = validate_standard_model_library(root / "_internal" / "seed-model-library")
    except StandardModelLibraryError as exc:
        raise WindowsReleaseError(f"bundled Standard Model Library is invalid: {exc}") from exc

    try:
        manifest = json.loads((root / RELEASE_MANIFEST).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WindowsReleaseError(f"release manifest is missing or invalid: {root / RELEASE_MANIFEST}") from exc
    if manifest.get("format") != RELEASE_FORMAT or manifest.get("schema_version") != RELEASE_SCHEMA_VERSION:
        raise WindowsReleaseError("release manifest format or schema version is unsupported")
    if not str(manifest.get("release_version") or "").strip():
        raise WindowsReleaseError("release manifest is missing release_version")
    if str(manifest.get("model_library_version") or "") != str(library_report["library_version"]):
        raise WindowsReleaseError("release manifest model-library version does not match the bundled library")

    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        raise WindowsReleaseError("release manifest contains no files")
    expected: set[str] = set()
    total_bytes = 0
    for entry in entries:
        if not isinstance(entry, dict):
            raise WindowsReleaseError("release manifest contains an invalid file entry")
        rel, path = _safe_manifest_path(root, str(entry.get("path") or ""))
        if rel == RELEASE_MANIFEST or rel in expected:
            raise WindowsReleaseError(f"duplicate or reserved release file path: {rel}")
        reason = _forbidden_reason(rel)
        if reason:
            raise WindowsReleaseError(f"release contains {reason}: {rel}")
        expected.add(rel)
        if _is_link(path) or not path.is_file():
            raise WindowsReleaseError(f"release file is missing or linked: {rel}")
        if path.stat().st_size != entry.get("byte_size"):
            raise WindowsReleaseError(f"release file size mismatch: {rel}")
        if _sha256_file(path) != str(entry.get("sha256") or "").lower():
            raise WindowsReleaseError(f"release file hash mismatch: {rel}")
        total_bytes += int(entry["byte_size"])

    actual = {path.relative_to(root).as_posix() for path in _tree_files(root, enforce_release_policy=True)}
    expected_with_manifest = expected | {RELEASE_MANIFEST}
    if actual != expected_with_manifest:
        extras = sorted(actual - expected_with_manifest)
        missing = sorted(expected_with_manifest - actual)
        raise WindowsReleaseError(f"release file set mismatch; extras={extras[:8]}, missing={missing[:8]}")
    return {
        "ok": True,
        "release_root": str(root),
        "release_version": str(manifest["release_version"]),
        "model_library_version": str(manifest["model_library_version"]),
        "third_party_distribution_count": int(license_report["distribution_count"]),
        "file_count": len(entries),
        "total_bytes": total_bytes,
    }


def _write_zip(source_root: Path, zip_path: Path, *, archive_root_name: str) -> dict[str, Any]:
    if zip_path.exists():
        raise WindowsReleaseError(f"ZIP destination already exists: {zip_path}")
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = zip_path.parent / f".{zip_path.name}.staging-{uuid.uuid4().hex}"
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for path in _tree_files(source_root, enforce_release_policy=True):
                rel = path.relative_to(source_root).as_posix()
                archive.write(path, f"{archive_root_name}/{rel}")
        with zipfile.ZipFile(temporary, "r") as archive:
            bad_file = archive.testzip()
            if bad_file is not None:
                raise WindowsReleaseError(f"ZIP CRC validation failed: {bad_file}")
        os.replace(temporary, zip_path)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "zip_path": str(zip_path),
        "zip_byte_size": zip_path.stat().st_size,
        "zip_sha256": _sha256_file(zip_path),
    }


def assemble_windows_release(
    *,
    pyinstaller_root: str | Path,
    model_library_root: str | Path,
    third_party_licenses_root: str | Path,
    destination: str | Path,
    release_version: str,
    app_version: str,
    zip_path: str | Path | None = None,
) -> dict[str, Any]:
    source_app = Path(pyinstaller_root).expanduser().resolve()
    source_library = Path(model_library_root).expanduser().resolve()
    source_licenses = Path(third_party_licenses_root).expanduser().resolve()
    target = Path(destination).expanduser().resolve()
    target_zip = Path(zip_path).expanduser().resolve() if zip_path is not None else None
    if target.exists():
        raise WindowsReleaseError(f"release destination already exists: {target}")
    if target_zip is not None and target_zip.exists():
        raise WindowsReleaseError(f"ZIP destination already exists: {target_zip}")
    if not str(release_version or "").strip() or not str(app_version or "").strip():
        raise WindowsReleaseError("release_version and app_version are required")
    if not (source_app / "Prisma.exe").is_file() or not (source_app / "_internal").is_dir():
        raise WindowsReleaseError(f"PyInstaller one-folder source is incomplete: {source_app}")
    if (source_app / "_internal" / "seed-model-library").exists():
        raise WindowsReleaseError("PyInstaller source must not already contain a seed model library")
    for candidate, parent in (
        (source_app, source_library),
        (source_library, source_app),
        (source_app, source_licenses),
        (source_licenses, source_app),
        (source_library, source_licenses),
        (source_licenses, source_library),
    ):
        try:
            candidate.relative_to(parent)
        except ValueError:
            pass
        else:
            raise WindowsReleaseError("release input trees may not overlap")
    for source, label in (
        (source_app, "PyInstaller source"),
        (source_library, "model library source"),
        (source_licenses, "third-party license source"),
    ):
        try:
            target.relative_to(source)
        except ValueError:
            pass
        else:
            raise WindowsReleaseError(f"release destination may not be inside {label}: {target}")
        if target_zip is not None:
            try:
                target_zip.relative_to(source)
            except ValueError:
                pass
            else:
                raise WindowsReleaseError(f"ZIP destination may not be inside {label}: {target_zip}")
    if target_zip is not None:
        try:
            target_zip.relative_to(target)
        except ValueError:
            pass
        else:
            raise WindowsReleaseError(f"ZIP destination may not be inside release destination: {target_zip}")

    try:
        library_report = validate_standard_model_library(source_library)
    except StandardModelLibraryError as exc:
        raise WindowsReleaseError(f"source Standard Model Library is invalid: {exc}") from exc
    try:
        validate_license_bundle(source_licenses)
    except LicenseBundleError as exc:
        raise WindowsReleaseError(f"source third-party license bundle is invalid: {exc}") from exc
    _tree_files(source_app, enforce_release_policy=True)

    target.parent.mkdir(parents=True, exist_ok=True)
    stage = target.parent / f".{target.name}.staging-{uuid.uuid4().hex}"
    zip_created = False
    try:
        stage.mkdir()
        _copy_tree_files(source_app, stage)
        _copy_tree_files(source_library, stage / "_internal" / "seed-model-library")
        _copy_tree_files(source_licenses, stage / THIRD_PARTY_LICENSES_DIR)
        _copy_legal_files(stage)
        (stage / README_NAME).write_text(
            _release_readme(
                release_version=str(release_version).strip(),
                library_version=str(library_report["library_version"]),
            ),
            encoding="utf-8",
        )
        manifest = {
            "format": RELEASE_FORMAT,
            "schema_version": RELEASE_SCHEMA_VERSION,
            "release_version": str(release_version).strip(),
            "app_version": str(app_version).strip(),
            "model_library_version": str(library_report["library_version"]),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "files": _file_records(stage),
        }
        _write_json(stage / RELEASE_MANIFEST, manifest)
        report = validate_windows_release(stage)
        if target_zip is not None:
            report.update(_write_zip(stage, target_zip, archive_root_name=target.name))
            zip_created = True
        os.replace(stage, target)
        report["release_root"] = str(target)
        return report
    except Exception:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
        if target_zip is not None and zip_created:
            target_zip.unlink(missing_ok=True)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    assemble = commands.add_parser("assemble")
    assemble.add_argument("--pyinstaller-root", required=True, type=Path)
    assemble.add_argument("--model-library-root", required=True, type=Path)
    assemble.add_argument("--third-party-licenses-root", required=True, type=Path)
    assemble.add_argument("--destination", required=True, type=Path)
    assemble.add_argument("--release-version", required=True)
    assemble.add_argument("--app-version", required=True)
    assemble.add_argument("--zip-path", type=Path)
    validate = commands.add_parser("validate")
    validate.add_argument("--release-root", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "assemble":
            report = assemble_windows_release(
                pyinstaller_root=args.pyinstaller_root,
                model_library_root=args.model_library_root,
                third_party_licenses_root=args.third_party_licenses_root,
                destination=args.destination,
                release_version=args.release_version,
                app_version=args.app_version,
                zip_path=args.zip_path,
            )
        else:
            report = validate_windows_release(args.release_root)
    except WindowsReleaseError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
