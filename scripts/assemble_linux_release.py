"""Stage, validate, and archive Prisma Linux Generator or Suite releases."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import posixpath
import shutil
import stat
import sys
import tarfile
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Literal


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


Product = Literal["generator", "suite"]

RELEASE_FORMAT = "prisma-linux-release"
RELEASE_SCHEMA_VERSION = 1
RELEASE_MANIFEST = "prisma-release.json"
README_NAME = "README.txt"
LEGAL_FILES = ("LICENSE", "THIRD_PARTY_NOTICES.md", "ASSET_LICENSES.md")
THIRD_PARTY_LICENSES_DIR = "THIRD_PARTY_LICENSES"
GENERATOR_EXE = "Prisma Generator"
CALIBRATION_EXE = "Prisma Calibration"
GENERATOR_ONLY_EXE = "Prisma"

GENERATOR_DIRECTORIES = (
    "Generator",
    "Generator/Images",
    "Generator/Exports",
    "Generator/Model Libraries",
    "Generator/Workspace",
)
CALIBRATION_DIRECTORIES = (
    "Calibration",
    "Calibration/Inbox",
    "Calibration/Inbox/Removed Images",
    "Calibration/Output",
    "Calibration/Output/Steps",
    "Calibration/Output/Backups",
    "Calibration/Output/Published Models",
    "Calibration/Workspace",
)
REQUIRED_GENERATOR_FILES = ("_internal/Prisma/generator/app/index.html",)
REQUIRED_SUITE_FILES = REQUIRED_GENERATOR_FILES + (
    "_internal/Prisma/calibration/app/index.html",
    "_internal/Prisma/calibration/blank_calibration_schema.sql",
)

FORBIDDEN_SUFFIXES = {
    ".arw",
    ".cr2",
    ".cr3",
    ".db",
    ".dng",
    ".ipynb",
    ".nef",
    ".orf",
    ".pyc",
    ".pyo",
    ".raf",
    ".rw2",
    ".sqlite",
    ".sqlite3",
}
FORBIDDEN_PARTS = {
    ".git",
    ".idea",
    "__pycache__",
    "build123d",
    "cadquery",
    "ipython",
    "ipykernel",
    "ipympl",
    "ipywidgets",
    "jupyterlab",
    "lib3mf",
    "matplotlib",
    "ocp",
    "playwright",
    "plotly",
    "pytest",
    "scipy_stubs",
    "setuptools",
    "tests",
    "vtk",
}
ALLOWED_PYTHON_PREFIXES = (
    ("_internal", "cv2"),
    ("_internal", "fitting", "photo_stack_model", "v63_fit_engine"),
)


class LinuxReleaseError(RuntimeError):
    """Raised when a Linux release cannot be assembled safely."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _has_prefix(parts: tuple[str, ...], prefix: tuple[str, ...]) -> bool:
    return len(parts) >= len(prefix) and parts[: len(prefix)] == prefix


def _forbidden_reason(relative_path: str) -> str | None:
    rel = PurePosixPath(relative_path)
    lowered = tuple(part.casefold() for part in rel.parts)
    blocked_parts = sorted(set(lowered) & FORBIDDEN_PARTS)
    if blocked_parts:
        return f"forbidden path component {blocked_parts[0]!r}"
    if rel.suffix.casefold() == ".py" and not any(
        _has_prefix(lowered, prefix) for prefix in ALLOWED_PYTHON_PREFIXES
    ):
        return "forbidden Python source outside an asserted packaged-runtime location"
    if rel.suffix.casefold() in FORBIDDEN_SUFFIXES:
        return f"forbidden file type {rel.suffix.casefold()!r}"
    return None


def _safe_link_target(root: Path, path: Path) -> str:
    target = os.readlink(path)
    target_posix = target.replace("\\", "/")
    if PurePosixPath(target_posix).is_absolute():
        raise LinuxReleaseError(f"release link has an absolute target: {path} -> {target}")
    rel_parent = path.parent.relative_to(root).as_posix()
    normalized = posixpath.normpath(posixpath.join(rel_parent, target_posix))
    if normalized == ".." or normalized.startswith("../"):
        raise LinuxReleaseError(f"release link escapes the root: {path} -> {target}")
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError) as exc:
        raise LinuxReleaseError(f"release link is broken, cyclic, or escapes the root: {path}") from exc
    return target_posix


def _tree_entries(root: Path, *, enforce_release_policy: bool) -> list[Path]:
    if not root.is_dir() or root.is_symlink():
        raise LinuxReleaseError(f"directory is missing or linked: {root}")
    entries: list[Path] = []
    for current, directory_names, file_names in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in sorted(directory_names + file_names):
            path = current_path / name
            mode = path.lstat().st_mode
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode) or stat.S_ISLNK(mode)):
                raise LinuxReleaseError(f"release contains a special filesystem entry: {path}")
            rel = path.relative_to(root).as_posix()
            if enforce_release_policy:
                reason = _forbidden_reason(rel)
                if reason:
                    raise LinuxReleaseError(f"release contains {reason}: {rel}")
            if stat.S_ISLNK(mode):
                _safe_link_target(root, path)
            entries.append(path)
    return sorted(entries, key=lambda item: item.relative_to(root).as_posix())


def _copy_tree(source: Path, destination: Path) -> None:
    _tree_entries(source, enforce_release_policy=False)
    shutil.copytree(source, destination, symlinks=True, copy_function=shutil.copy2)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _copy_legal_files(destination: Path) -> None:
    for name in LEGAL_FILES:
        source = PROJECT_ROOT / name
        if not source.is_file():
            raise LinuxReleaseError(f"required legal file is missing: {source}")
        shutil.copy2(source, destination / name)


def _visible_directories(product: Product) -> tuple[str, ...]:
    return GENERATOR_DIRECTORIES + (CALIBRATION_DIRECTORIES if product == "suite" else ())


def _release_readme(*, product: Product, release_version: str, library_version: str) -> str:
    if product == "suite":
        launch = """2. Run ./Prisma\\ Generator to make lithophanes.
3. Run ./Prisma\\ Calibration to capture calibration data, fit models, and
   publish model libraries.
4. Keep an app's terminal open while using its browser interface. Closing it
   stops only that app. Both apps may run at the same time."""
        title = "Prisma Suite"
        ownership = "Generator files live under Generator. Calibration files live under Calibration."
    else:
        launch = """2. Run ./Prisma to make lithophanes.
3. Keep its terminal open while using the browser interface. Closing it stops
   the local server."""
        title = "Prisma"
        ownership = "Images, exports, installed model libraries, and workspace files live under Generator."
    return f"""{title} {release_version} for Linux x86_64

Standard Model Library: {library_version}

1. Extract the entire folder from the TAR.GZ archive before running Prisma.
{launch}

Prisma runs only on your own computer and binds to 127.0.0.1. It does not
upload your images or host a public website.

{ownership} Keep independent backups of important work outside the live Prisma
folder. Do not run the live folder from a cloud-synchronized or network share.

This build targets glibc 2.38 or newer. It was built on Ubuntu 24.04 x86_64;
other Linux distributions have not yet been verified. macOS requires a
separate native build and is not supported by this archive.
"""


def _file_records(root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in _tree_entries(root, enforce_release_policy=True):
        rel = path.relative_to(root).as_posix()
        if rel == RELEASE_MANIFEST or path.is_dir() and not path.is_symlink():
            continue
        mode = stat.S_IMODE(path.lstat().st_mode)
        if path.is_symlink():
            records.append(
                {
                    "path": rel,
                    "type": "symlink",
                    "link_target": _safe_link_target(root, path),
                    "mode": f"{mode:04o}",
                }
            )
        else:
            records.append(
                {
                    "path": rel,
                    "type": "file",
                    "byte_size": path.stat().st_size,
                    "sha256": _sha256_file(path),
                    "mode": f"{mode:04o}",
                }
            )
    return records


def _safe_manifest_path(root: Path, value: str) -> tuple[str, Path]:
    text = str(value or "")
    if "\\" in text:
        raise LinuxReleaseError(f"release manifest paths must use forward slashes: {text!r}")
    rel = PurePosixPath(text)
    if rel.is_absolute() or not rel.parts or any(part in {"", ".", ".."} for part in rel.parts):
        raise LinuxReleaseError(f"unsafe release manifest path: {text!r}")
    return rel.as_posix(), root.joinpath(*rel.parts)


def _require_shape(root: Path, product: Product) -> None:
    executables = (GENERATOR_ONLY_EXE,) if product == "generator" else (GENERATOR_EXE, CALIBRATION_EXE)
    disallowed = (GENERATOR_EXE, CALIBRATION_EXE) if product == "generator" else (GENERATOR_ONLY_EXE,)
    for name in executables:
        path = root / name
        if not path.is_file() or path.is_symlink() or not os.access(path, os.X_OK):
            raise LinuxReleaseError(f"required executable is missing, linked, or not executable: {name}")
    for name in disallowed:
        if (root / name).exists() or (root / name).is_symlink():
            raise LinuxReleaseError(f"unexpected executable appears in {product} release: {name}")
    if not (root / "_internal").is_dir() or (root / "_internal").is_symlink():
        raise LinuxReleaseError("PyInstaller _internal directory is missing or linked")
    required_files = REQUIRED_GENERATOR_FILES if product == "generator" else REQUIRED_SUITE_FILES
    for relative in required_files:
        if not root.joinpath(*PurePosixPath(relative).parts).is_file():
            raise LinuxReleaseError(f"required bundled file is missing: {relative}")
    for relative in _visible_directories(product):
        path = root.joinpath(*PurePosixPath(relative).parts)
        if not path.is_dir() or path.is_symlink():
            raise LinuxReleaseError(f"visible data directory is missing or linked: {relative}")
    for name in LEGAL_FILES:
        if not (root / name).is_file() or (root / name).is_symlink():
            raise LinuxReleaseError(f"required legal file is missing or linked: {name}")
    try:
        validate_license_bundle(root / THIRD_PARTY_LICENSES_DIR)
    except LicenseBundleError as exc:
        raise LinuxReleaseError(f"third-party license bundle is invalid: {exc}") from exc


def validate_linux_release(release_root: str | Path) -> dict[str, Any]:
    root = Path(release_root).expanduser().resolve()
    try:
        manifest = json.loads((root / RELEASE_MANIFEST).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LinuxReleaseError(f"release manifest is missing or invalid: {root / RELEASE_MANIFEST}") from exc
    if manifest.get("format") != RELEASE_FORMAT or manifest.get("schema_version") != RELEASE_SCHEMA_VERSION:
        raise LinuxReleaseError("release manifest format or schema version is unsupported")
    product = manifest.get("product")
    if product not in {"generator", "suite"}:
        raise LinuxReleaseError("release manifest product is unsupported")
    _require_shape(root, product)
    try:
        library_report = validate_standard_model_library(root / "_internal" / "seed-model-library")
    except StandardModelLibraryError as exc:
        raise LinuxReleaseError(f"bundled Standard Model Library is invalid: {exc}") from exc
    if manifest.get("platform") != "linux-x86_64":
        raise LinuxReleaseError("release manifest platform is unsupported")
    if manifest.get("minimum_glibc") != "2.38":
        raise LinuxReleaseError("release manifest minimum_glibc is unsupported")
    if not str(manifest.get("release_version") or "").strip() or not str(manifest.get("app_version") or "").strip():
        raise LinuxReleaseError("release manifest is missing release_version or app_version")
    if str(manifest.get("model_library_version") or "") != str(library_report["library_version"]):
        raise LinuxReleaseError("release manifest model-library version does not match bundled library")

    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        raise LinuxReleaseError("release manifest contains no files")
    expected: set[str] = set()
    total_bytes = 0
    for entry in entries:
        if not isinstance(entry, dict):
            raise LinuxReleaseError("release manifest contains an invalid file entry")
        rel, path = _safe_manifest_path(root, str(entry.get("path") or ""))
        if rel == RELEASE_MANIFEST or rel in expected:
            raise LinuxReleaseError(f"duplicate or reserved release file path: {rel}")
        reason = _forbidden_reason(rel)
        if reason:
            raise LinuxReleaseError(f"release contains {reason}: {rel}")
        expected.add(rel)
        if entry.get("type") == "symlink":
            if not path.is_symlink() or _safe_link_target(root, path) != entry.get("link_target"):
                raise LinuxReleaseError(f"release symlink is missing or changed: {rel}")
        elif entry.get("type") == "file":
            if path.is_symlink() or not path.is_file():
                raise LinuxReleaseError(f"release file is missing or linked: {rel}")
            if path.stat().st_size != entry.get("byte_size"):
                raise LinuxReleaseError(f"release file size mismatch: {rel}")
            if _sha256_file(path) != str(entry.get("sha256") or "").lower():
                raise LinuxReleaseError(f"release file hash mismatch: {rel}")
            total_bytes += int(entry["byte_size"])
        else:
            raise LinuxReleaseError(f"release manifest has unsupported entry type: {rel}")
        actual_mode = f"{stat.S_IMODE(path.lstat().st_mode):04o}"
        if actual_mode != entry.get("mode"):
            raise LinuxReleaseError(f"release file mode mismatch: {rel}")

    actual = {
        path.relative_to(root).as_posix()
        for path in _tree_entries(root, enforce_release_policy=True)
        if path.is_symlink() or path.is_file()
    }
    if actual != expected | {RELEASE_MANIFEST}:
        raise LinuxReleaseError(
            f"release file set mismatch; extras={sorted(actual - expected - {RELEASE_MANIFEST})[:8]}, "
            f"missing={sorted(expected - actual)[:8]}"
        )
    return {
        "ok": True,
        "release_root": str(root),
        "release_version": str(manifest["release_version"]),
        "product": product,
        "model_library_version": str(manifest["model_library_version"]),
        "file_count": len(entries),
        "total_bytes": total_bytes,
    }


def _normalized_tar_info(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    return info


def _validate_tar(tar_path: Path, *, archive_root_name: str, expected_directories: set[str]) -> None:
    prefix = f"{archive_root_name}/"
    seen: set[str] = set()
    with tarfile.open(tar_path, "r:gz") as archive:
        for member in archive.getmembers():
            name = member.name
            if name == archive_root_name:
                continue
            if not name.startswith(prefix):
                raise LinuxReleaseError(f"archive member is outside the release root: {name}")
            relative = name[len(prefix) :]
            rel = PurePosixPath(relative)
            if rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts):
                raise LinuxReleaseError(f"archive member has an unsafe path: {name}")
            if member.isdev() or member.isfifo() or member.issym() and PurePosixPath(member.linkname).is_absolute():
                raise LinuxReleaseError(f"archive contains an unsafe member: {name}")
            if member.issym():
                normalized = posixpath.normpath(posixpath.join(posixpath.dirname(relative), member.linkname))
                if normalized == ".." or normalized.startswith("../"):
                    raise LinuxReleaseError(f"archive symlink escapes the release root: {name}")
            if member.islnk():
                raise LinuxReleaseError(f"archive may not contain hard links: {name}")
            if member.uname != "root" or member.gname != "root" or member.uid != 0 or member.gid != 0:
                raise LinuxReleaseError(f"archive ownership metadata was not normalized: {name}")
            seen.add(name.rstrip("/"))
            if member.isfile():
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise LinuxReleaseError(f"archive file cannot be read: {name}")
                while extracted.read(1024 * 1024):
                    pass
    missing = sorted({f"{archive_root_name}/{item}" for item in expected_directories} - seen)
    if missing:
        raise LinuxReleaseError(f"archive omitted visible data directories: {missing[:8]}")


def _write_tar(source_root: Path, tar_path: Path, *, archive_root_name: str, product: Product) -> dict[str, Any]:
    if tar_path.exists():
        raise LinuxReleaseError(f"TAR.GZ destination already exists: {tar_path}")
    tar_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = tar_path.parent / f".{tar_path.name}.staging-{uuid.uuid4().hex}"
    try:
        with tarfile.open(temporary, "w:gz", dereference=False) as archive:
            archive.add(source_root, arcname=archive_root_name, recursive=True, filter=_normalized_tar_info)
        _validate_tar(
            temporary,
            archive_root_name=archive_root_name,
            expected_directories=set(_visible_directories(product)),
        )
        byte_size = temporary.stat().st_size
        sha256 = _sha256_file(temporary)
        os.replace(temporary, tar_path)
    finally:
        temporary.unlink(missing_ok=True)
    return {"tar_path": str(tar_path), "tar_byte_size": byte_size, "tar_sha256": sha256}


def assemble_linux_release(
    *,
    product: Product,
    pyinstaller_root: str | Path,
    model_library_root: str | Path,
    third_party_licenses_root: str | Path,
    destination: str | Path,
    release_version: str,
    app_version: str,
    tar_path: str | Path | None = None,
) -> dict[str, Any]:
    if product not in {"generator", "suite"}:
        raise LinuxReleaseError(f"unsupported product: {product}")
    source_app = Path(pyinstaller_root).expanduser().resolve()
    source_library = Path(model_library_root).expanduser().resolve()
    source_licenses = Path(third_party_licenses_root).expanduser().resolve()
    target = Path(destination).expanduser().resolve()
    target_tar = Path(tar_path).expanduser().resolve() if tar_path is not None else None
    if os.name != "posix":
        raise LinuxReleaseError("Linux releases must be assembled in a native POSIX environment")
    if target.exists() or target_tar is not None and target_tar.exists():
        raise LinuxReleaseError("release destination or TAR.GZ destination already exists")
    if not str(release_version or "").strip() or not str(app_version or "").strip():
        raise LinuxReleaseError("release_version and app_version are required")
    if not source_app.is_dir() or not (source_app / "_internal").is_dir():
        raise LinuxReleaseError(f"PyInstaller one-folder source is incomplete: {source_app}")
    if (source_app / "_internal" / "seed-model-library").exists():
        raise LinuxReleaseError("PyInstaller source must not already contain a seed model library")
    if (source_app / "Generator").exists() or (source_app / "Calibration").exists():
        raise LinuxReleaseError("PyInstaller source must not contain live user-data directories")
    expected_executables = (GENERATOR_ONLY_EXE,) if product == "generator" else (GENERATOR_EXE, CALIBRATION_EXE)
    for name in expected_executables:
        path = source_app / name
        if not path.is_file() or path.is_symlink() or not os.access(path, os.X_OK):
            raise LinuxReleaseError(f"PyInstaller source is missing executable: {name}")
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
            raise LinuxReleaseError("release input trees may not overlap")
    if target_tar is not None:
        try:
            target_tar.relative_to(target)
        except ValueError:
            pass
        else:
            raise LinuxReleaseError(f"TAR.GZ destination may not be inside release destination: {target_tar}")
    for source, label in (
        (source_app, "PyInstaller source"),
        (source_library, "model library source"),
        (source_licenses, "third-party license source"),
    ):
        for output in (target, target_tar):
            if output is None:
                continue
            try:
                output.relative_to(source)
            except ValueError:
                pass
            else:
                raise LinuxReleaseError(f"release output may not be inside {label}: {output}")
    try:
        library_report = validate_standard_model_library(source_library)
    except StandardModelLibraryError as exc:
        raise LinuxReleaseError(f"source Standard Model Library is invalid: {exc}") from exc
    try:
        validate_license_bundle(source_licenses)
    except LicenseBundleError as exc:
        raise LinuxReleaseError(f"source third-party license bundle is invalid: {exc}") from exc
    _tree_entries(source_app, enforce_release_policy=True)

    target.parent.mkdir(parents=True, exist_ok=True)
    stage = target.parent / f".{target.name}.staging-{uuid.uuid4().hex}"
    tar_created = False
    try:
        _copy_tree(source_app, stage)
        _copy_tree(source_library, stage / "_internal" / "seed-model-library")
        _copy_tree(source_licenses, stage / THIRD_PARTY_LICENSES_DIR)
        _copy_legal_files(stage)
        for relative in _visible_directories(product):
            stage.joinpath(*PurePosixPath(relative).parts).mkdir(parents=True, exist_ok=True)
        (stage / README_NAME).write_text(
            _release_readme(
                product=product,
                release_version=str(release_version).strip(),
                library_version=str(library_report["library_version"]),
            ),
            encoding="utf-8",
        )
        manifest = {
            "format": RELEASE_FORMAT,
            "schema_version": RELEASE_SCHEMA_VERSION,
            "platform": "linux-x86_64",
            "product": product,
            "release_version": str(release_version).strip(),
            "app_version": str(app_version).strip(),
            "model_library_version": str(library_report["library_version"]),
            "minimum_glibc": "2.38",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "files": _file_records(stage),
        }
        _write_json(stage / RELEASE_MANIFEST, manifest)
        report = validate_linux_release(stage)
        if target_tar is not None:
            report.update(_write_tar(stage, target_tar, archive_root_name=target.name, product=product))
            tar_created = True
        os.replace(stage, target)
        report["release_root"] = str(target)
        return report
    except Exception:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
        if target_tar is not None and tar_created:
            target_tar.unlink(missing_ok=True)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    assemble = commands.add_parser("assemble")
    assemble.add_argument("--product", required=True, choices=("generator", "suite"))
    assemble.add_argument("--pyinstaller-root", required=True, type=Path)
    assemble.add_argument("--model-library-root", required=True, type=Path)
    assemble.add_argument("--third-party-licenses-root", required=True, type=Path)
    assemble.add_argument("--destination", required=True, type=Path)
    assemble.add_argument("--release-version", required=True)
    assemble.add_argument("--app-version", required=True)
    assemble.add_argument("--tar-path", type=Path)
    validate = commands.add_parser("validate")
    validate.add_argument("--release-root", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "assemble":
            report = assemble_linux_release(
                product=args.product,
                pyinstaller_root=args.pyinstaller_root,
                model_library_root=args.model_library_root,
                third_party_licenses_root=args.third_party_licenses_root,
                destination=args.destination,
                release_version=args.release_version,
                app_version=args.app_version,
                tar_path=args.tar_path,
            )
        else:
            report = validate_linux_release(args.release_root)
    except LinuxReleaseError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
