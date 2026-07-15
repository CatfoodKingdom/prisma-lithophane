"""Collect exact dependency license texts from a native release environment.

Run this script with the Python interpreter from the same environment used to
build a PyInstaller release.  The resulting directory is an immutable input to
the platform release assemblers.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import sys
import sysconfig
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


BUNDLE_FORMAT = "prisma-third-party-license-bundle"
BUNDLE_SCHEMA_VERSION = 1
INDEX_NAME = "INDEX.json"
README_NAME = "README.txt"
_LICENSE_PREFIXES = ("license", "licence", "copying", "notice", "authors", "copyright")


class LicenseBundleError(RuntimeError):
    """Raised when a collected license bundle is incomplete or unsafe."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_name(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-.")
    if not normalized:
        raise LicenseBundleError(f"unsafe empty package name derived from {value!r}")
    return normalized


def _safe_relative(value: str) -> PurePosixPath:
    if "\\" in value:
        value = value.replace("\\", "/")
    rel = PurePosixPath(value)
    if rel.is_absolute() or not rel.parts or any(part in {"", ".", ".."} for part in rel.parts):
        raise LicenseBundleError(f"unsafe license path: {value!r}")
    return rel


def _is_license_file(path: Path) -> bool:
    return any(part.casefold().startswith(_LICENSE_PREFIXES) for part in path.parts)


def _python_license_path(explicit: str | Path | None) -> Path:
    if explicit is not None:
        candidate = Path(explicit).expanduser().resolve()
        if not candidate.is_file():
            raise LicenseBundleError(f"CPython license file is missing: {candidate}")
        return candidate
    candidates = [
        Path(sys.base_prefix) / "LICENSE.txt",
        Path(sys.base_prefix) / "LICENSE",
        Path(sysconfig.get_config_var("prefix") or sys.base_prefix) / "LICENSE.txt",
        Path(f"/usr/share/doc/python{sys.version_info.major}.{sys.version_info.minor}/copyright"),
        Path(f"/usr/share/doc/python{sys.version_info.major}/copyright"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise LicenseBundleError(
        "CPython license text was not found; pass --python-license-file from the native build environment"
    )


def _copy_record(source: Path, destination: Path, *, relative_path: str) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return {
        "path": relative_path,
        "byte_size": destination.stat().st_size,
        "sha256": _sha256_file(destination),
    }


def _distribution_license_files(distribution: importlib.metadata.Distribution) -> list[tuple[Path, PurePosixPath]]:
    metadata_root = getattr(distribution, "_path", None)
    if metadata_root is None:
        raise LicenseBundleError(
            f"cannot locate metadata directory for {distribution.metadata.get('Name', '<unknown>')}"
        )
    root = Path(metadata_root).resolve()
    if not root.is_dir():
        raise LicenseBundleError(f"distribution metadata directory is missing: {root}")
    found: dict[str, tuple[Path, PurePosixPath]] = {}
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        rel = PurePosixPath(path.relative_to(root).as_posix())
        if _is_license_file(Path(*rel.parts)):
            found[rel.as_posix().casefold()] = (path, rel)
    return [found[key] for key in sorted(found)]


def collect_license_bundle(
    destination: str | Path,
    *,
    python_license_file: str | Path | None = None,
) -> dict[str, Any]:
    target = Path(destination).expanduser().resolve()
    if target.exists():
        raise LicenseBundleError(f"license bundle destination already exists: {target}")
    python_license = _python_license_path(python_license_file)
    stage = target.parent / f".{target.name}.staging-{uuid.uuid4().hex}"
    try:
        stage.mkdir(parents=True)
        records: list[dict[str, Any]] = []
        cpython_rel = "CPython/LICENSE.txt"
        records.append(
            _copy_record(python_license, stage / Path(*PurePosixPath(cpython_rel).parts), relative_path=cpython_rel)
        )

        packages: list[dict[str, Any]] = []
        seen_directories: set[str] = set()
        distributions = sorted(
            importlib.metadata.distributions(),
            key=lambda item: (
                str(item.metadata.get("Name") or "").casefold(),
                str(item.version),
            ),
        )
        for distribution in distributions:
            name = str(distribution.metadata.get("Name") or "").strip()
            version = str(distribution.version or "").strip()
            if not name or not version:
                raise LicenseBundleError("build environment contains a distribution without name/version metadata")
            directory = f"{_safe_name(name)}-{_safe_name(version)}"
            directory_key = directory.casefold()
            if directory_key in seen_directories:
                raise LicenseBundleError(f"duplicate normalized distribution directory: {directory}")
            seen_directories.add(directory_key)

            discovered = _distribution_license_files(distribution)
            if not discovered:
                raise LicenseBundleError(f"no exact license/notice file found for {name}=={version}")
            package_records: list[dict[str, Any]] = []
            for source, metadata_rel in discovered:
                output_rel = _safe_relative(f"packages/{directory}/{metadata_rel.as_posix()}")
                record = _copy_record(
                    source,
                    stage.joinpath(*output_rel.parts),
                    relative_path=output_rel.as_posix(),
                )
                records.append(record)
                package_records.append(record)
            packages.append(
                {
                    "name": name,
                    "version": version,
                    "license_expression": str(distribution.metadata.get("License-Expression") or "").strip(),
                    "license_files": package_records,
                }
            )

        readme = (
            "Exact third-party license and notice texts collected from the native Python\n"
            "environment used to build this Prisma release. INDEX.json records the\n"
            "resolved distribution versions and SHA-256 hashes. Additional native-library\n"
            "texts shipped by binary wheels are retained in their original subdirectories.\n"
        )
        (stage / README_NAME).write_text(readme, encoding="utf-8")
        records.append(
            {
                "path": README_NAME,
                "byte_size": (stage / README_NAME).stat().st_size,
                "sha256": _sha256_file(stage / README_NAME),
            }
        )
        index = {
            "format": BUNDLE_FORMAT,
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "python_implementation": sys.implementation.name,
            "python_version": sys.version.split()[0],
            "platform": sys.platform,
            "distribution_count": len(packages),
            "distributions": packages,
            "files": sorted(records, key=lambda item: item["path"]),
        }
        (stage / INDEX_NAME).write_text(json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        validate_license_bundle(stage)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(stage, target)
        return {
            "ok": True,
            "destination": str(target),
            "python_version": index["python_version"],
            "platform": index["platform"],
            "distribution_count": len(packages),
            "file_count": len(records),
        }
    except Exception:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
        raise


def validate_license_bundle(root: str | Path) -> dict[str, Any]:
    bundle = Path(root).expanduser().resolve()
    try:
        index = json.loads((bundle / INDEX_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LicenseBundleError(f"license bundle index is missing or invalid: {bundle / INDEX_NAME}") from exc
    if index.get("format") != BUNDLE_FORMAT or index.get("schema_version") != BUNDLE_SCHEMA_VERSION:
        raise LicenseBundleError("license bundle format or schema version is unsupported")
    if not (bundle / "CPython" / "LICENSE.txt").is_file():
        raise LicenseBundleError("license bundle is missing the CPython license")
    distributions = index.get("distributions")
    records = index.get("files")
    if not isinstance(distributions, list) or not distributions:
        raise LicenseBundleError("license bundle contains no distributions")
    if index.get("distribution_count") != len(distributions):
        raise LicenseBundleError("license bundle distribution count is inconsistent")
    if not isinstance(records, list) or not records:
        raise LicenseBundleError("license bundle contains no file records")
    expected: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            raise LicenseBundleError("license bundle contains an invalid file record")
        rel = _safe_relative(str(record.get("path") or ""))
        rel_text = rel.as_posix()
        if rel_text in expected or rel_text == INDEX_NAME:
            raise LicenseBundleError(f"duplicate or reserved license bundle path: {rel_text}")
        expected.add(rel_text)
        path = bundle.joinpath(*rel.parts)
        if path.is_symlink() or not path.is_file():
            raise LicenseBundleError(f"license bundle file is missing or linked: {rel_text}")
        if path.stat().st_size != record.get("byte_size"):
            raise LicenseBundleError(f"license bundle file size mismatch: {rel_text}")
        if _sha256_file(path) != str(record.get("sha256") or "").casefold():
            raise LicenseBundleError(f"license bundle file hash mismatch: {rel_text}")
    actual = {
        path.relative_to(bundle).as_posix()
        for path in bundle.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    if actual != expected | {INDEX_NAME}:
        raise LicenseBundleError(
            f"license bundle file set mismatch; extras={sorted(actual - expected - {INDEX_NAME})[:8]}, "
            f"missing={sorted(expected - actual)[:8]}"
        )
    return {
        "ok": True,
        "root": str(bundle),
        "python_version": str(index.get("python_version") or ""),
        "platform": str(index.get("platform") or ""),
        "distribution_count": len(distributions),
        "file_count": len(records),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    collect = commands.add_parser("collect")
    collect.add_argument("--destination", required=True, type=Path)
    collect.add_argument("--python-license-file", type=Path)
    validate = commands.add_parser("validate")
    validate.add_argument("--root", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "collect":
            report = collect_license_bundle(
                args.destination,
                python_license_file=args.python_license_file,
            )
        else:
            report = validate_license_bundle(args.root)
    except LicenseBundleError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
