"""Create and validate a clean, allowlisted Prisma GitHub source staging tree."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_MANIFEST = "SOURCE-MANIFEST.json"
SOURCE_FORMAT = "prisma-github-source"
SOURCE_SCHEMA_VERSION = 1

ROOT_FILES = (
    ".gitignore",
    ".python-version",
    "ASSET_LICENSES.md",
    "LICENSE",
    "README.md",
    "THIRD_PARTY_NOTICES.md",
    "pyproject.toml",
    "uv.lock",
)
PACKAGING_FILES = (
    "packaging/Prisma.spec",
    "packaging/PrismaCalibration.spec",
    "packaging/PrismaSuite.spec",
)
SCRIPT_FILES = (
    "scripts/assemble_linux_release.py",
    "scripts/assemble_windows_release.py",
    "scripts/assemble_windows_suite.py",
    "scripts/collect_third_party_licenses.py",
    "scripts/prepare_github_source.py",
    "scripts/standard_model_library.py",
)
PRISMA_SUFFIXES = {".py", ".js", ".css", ".html", ".sql", ".json"}
TEST_SUFFIXES = {".py", ".js", ".cjs", ".json", ".txt"}

EXCLUDED_PREFIXES = (
    ".claude/",
    ".idea/",
    ".release-build/",
    ".superpowers/",
    ".venv/",
    "Calibration/",
    "DevelopmentSandbox/",
    "Documentation/",
    "Generator/",
    "PixeSTL_Java_Codebase/",
    "docs/",
    "notebooks/",
    "tools/",
)
EXCLUDED_ASSETS = (
    "Prisma/data/",
    "Prisma/calibration/docs/",
    "Prisma/generator/docs/",
    "Prisma/generator/.tmp/",
    "Prisma/lib/photo_stack_model/bundles/runtime_bundle.json",
)
PUBLIC_DATA_PREFIXES = (
    "Prisma/data/generator/settings_profiles/",
)

PERSONAL_PATTERNS = {
    "personal_name": re.compile(b"bran" + b"don", re.IGNORECASE),
    "windows_user_path": re.compile(rb"c:[\\/]users[\\/]brand(?:on)?(?:[\\/]|\b)", re.IGNORECASE),
    "posix_user_path": re.compile(rb"/(?:home|users)/bran" + rb"don(?:/|\b)", re.IGNORECASE),
    "fossil_project": re.compile(b"jupyter" + b"project", re.IGNORECASE),
}
SECRET_PATTERNS = {
    "private_key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "github_token": re.compile(rb"\b(?:ghp|github_pat)_[A-Za-z0-9_]{20,}\b"),
    "openai_key": re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "aws_access_key": re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
}


class SourcePreparationError(RuntimeError):
    """Raised when a source staging tree is incomplete or unsafe."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_link(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _project_files(root: Path) -> list[Path]:
    return sorted(
        (path for path in root.rglob("*") if path.is_file() or _is_link(path)),
        key=lambda path: path.relative_to(root).as_posix(),
    )


def _require_regular_source(path: Path) -> None:
    if _is_link(path) or not path.is_file():
        raise SourcePreparationError(f"allowlisted source is missing or linked: {path}")


def _collect_allowlist(project_root: Path) -> list[Path]:
    selected: dict[str, Path] = {}

    def add(relative: str) -> None:
        rel = PurePosixPath(relative)
        if rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts):
            raise SourcePreparationError(f"invalid source allowlist path: {relative!r}")
        path = project_root.joinpath(*rel.parts)
        _require_regular_source(path)
        selected[rel.as_posix()] = path

    for relative in ROOT_FILES + PACKAGING_FILES + SCRIPT_FILES:
        add(relative)

    prisma_root = project_root / "Prisma"
    for path in _project_files(prisma_root):
        rel = path.relative_to(project_root).as_posix()
        if _is_link(path):
            raise SourcePreparationError(f"Prisma source may not contain filesystem links: {rel}")
        is_public_data = any(rel.startswith(prefix) for prefix in PUBLIC_DATA_PREFIXES)
        if any(rel.startswith(prefix) for prefix in EXCLUDED_ASSETS) and not is_public_data:
            continue
        if path.suffix.casefold() in PRISMA_SUFFIXES:
            selected[rel] = path

    tests_root = project_root / "tests"
    for path in _project_files(tests_root):
        rel = path.relative_to(project_root).as_posix()
        if _is_link(path):
            raise SourcePreparationError(f"test source may not contain filesystem links: {rel}")
        if any(rel.startswith(prefix) for prefix in EXCLUDED_ASSETS):
            continue
        if path.suffix.casefold() in TEST_SUFFIXES:
            selected[rel] = path

    if not selected:
        raise SourcePreparationError("source allowlist selected no files")
    return [selected[key] for key in sorted(selected)]


def _scan_files(root: Path, paths: Iterable[Path]) -> dict[str, list[str]]:
    findings: dict[str, list[str]] = {}
    for path in paths:
        rel = path.relative_to(root).as_posix()
        payload = path.read_bytes()
        for label, pattern in {**PERSONAL_PATTERNS, **SECRET_PATTERNS}.items():
            if pattern.search(payload):
                findings.setdefault(label, []).append(rel)
    return {key: sorted(set(value)) for key, value in sorted(findings.items())}


def _file_records(root: Path) -> list[dict[str, Any]]:
    records = []
    for path in _project_files(root):
        rel = path.relative_to(root).as_posix()
        if rel == SOURCE_MANIFEST:
            continue
        if _is_link(path):
            raise SourcePreparationError(f"staged source contains a filesystem link: {rel}")
        records.append(
            {"path": rel, "byte_size": path.stat().st_size, "sha256": _sha256_file(path)}
        )
    return records


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def validate_source_tree(source_root: str | Path) -> dict[str, Any]:
    root = Path(source_root).expanduser().resolve()
    try:
        manifest = json.loads((root / SOURCE_MANIFEST).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SourcePreparationError(f"source manifest is missing or invalid: {root / SOURCE_MANIFEST}") from exc
    if manifest.get("format") != SOURCE_FORMAT or manifest.get("schema_version") != SOURCE_SCHEMA_VERSION:
        raise SourcePreparationError("source manifest format or schema version is unsupported")
    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        raise SourcePreparationError("source manifest contains no files")
    expected: set[str] = set()
    total_bytes = 0
    for entry in entries:
        if not isinstance(entry, dict):
            raise SourcePreparationError("source manifest contains an invalid file entry")
        text = str(entry.get("path") or "")
        if "\\" in text:
            raise SourcePreparationError(f"source manifest path must use forward slashes: {text!r}")
        rel = PurePosixPath(text)
        if rel.is_absolute() or not rel.parts or any(part in {"", ".", ".."} for part in rel.parts):
            raise SourcePreparationError(f"source manifest contains an unsafe path: {text!r}")
        normalized = rel.as_posix()
        if normalized == SOURCE_MANIFEST or normalized in expected:
            raise SourcePreparationError(f"source manifest contains duplicate or reserved path: {normalized}")
        is_public_data = any(
            normalized.startswith(prefix) for prefix in PUBLIC_DATA_PREFIXES
        )
        if normalized.startswith(EXCLUDED_PREFIXES) or (
            normalized.startswith(EXCLUDED_ASSETS) and not is_public_data
        ):
            raise SourcePreparationError(f"source manifest contains an excluded path: {normalized}")
        path = root.joinpath(*rel.parts)
        if _is_link(path) or not path.is_file():
            raise SourcePreparationError(f"staged source is missing or linked: {normalized}")
        if path.stat().st_size != entry.get("byte_size") or _sha256_file(path) != entry.get("sha256"):
            raise SourcePreparationError(f"staged source does not match its manifest: {normalized}")
        expected.add(normalized)
        total_bytes += int(entry["byte_size"])

    actual_paths = _project_files(root)
    actual = {path.relative_to(root).as_posix() for path in actual_paths}
    if actual != expected | {SOURCE_MANIFEST}:
        raise SourcePreparationError(
            f"source file set mismatch; extras={sorted(actual - expected - {SOURCE_MANIFEST})[:8]}, "
            f"missing={sorted(expected - actual)[:8]}"
        )
    findings = _scan_files(root, actual_paths)
    if findings:
        raise SourcePreparationError(f"source privacy/secret scan failed: {findings}")
    for required in ROOT_FILES + PACKAGING_FILES + SCRIPT_FILES:
        if required not in expected:
            raise SourcePreparationError(f"source tree is missing required file: {required}")
    return {
        "ok": True,
        "source_root": str(root),
        "file_count": len(expected) + 1,
        "manifested_file_count": len(expected),
        "total_bytes": total_bytes,
        "privacy_findings": {},
    }


def _write_zip(source_root: Path, zip_path: Path, *, archive_root_name: str) -> dict[str, Any]:
    if zip_path.exists():
        raise SourcePreparationError(f"source ZIP destination already exists: {zip_path}")
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = zip_path.parent / f".{zip_path.name}.staging-{uuid.uuid4().hex}"
    expected = set()
    try:
        with zipfile.ZipFile(temporary, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for path in _project_files(source_root):
                rel = path.relative_to(source_root).as_posix()
                member = f"{archive_root_name}/{rel}"
                archive.write(path, member)
                expected.add(member)
        with zipfile.ZipFile(temporary, "r") as archive:
            bad = archive.testzip()
            if bad is not None:
                raise SourcePreparationError(f"source ZIP CRC validation failed: {bad}")
            names = {info.filename for info in archive.infolist() if not info.is_dir()}
            if names != expected:
                raise SourcePreparationError("source ZIP file set does not match staging")
        byte_size = temporary.stat().st_size
        sha256 = _sha256_file(temporary)
        os.replace(temporary, zip_path)
    finally:
        temporary.unlink(missing_ok=True)
    return {"zip_path": str(zip_path), "zip_byte_size": byte_size, "zip_sha256": sha256}


def prepare_source_tree(
    *,
    destination: str | Path,
    release_version: str,
    zip_path: str | Path | None = None,
) -> dict[str, Any]:
    target = Path(destination).expanduser().resolve()
    target_zip = Path(zip_path).expanduser().resolve() if zip_path is not None else None
    if target.exists() or target_zip is not None and target_zip.exists():
        raise SourcePreparationError("source destination or ZIP destination already exists")
    if not str(release_version or "").strip():
        raise SourcePreparationError("release_version is required")
    if target_zip is not None:
        try:
            target_zip.relative_to(target)
        except ValueError:
            pass
        else:
            raise SourcePreparationError("source ZIP destination may not be inside source destination")
    sources = _collect_allowlist(PROJECT_ROOT)
    findings = _scan_files(PROJECT_ROOT, sources)
    if findings:
        raise SourcePreparationError(f"allowlisted source privacy/secret scan failed: {findings}")

    target.parent.mkdir(parents=True, exist_ok=True)
    stage = target.parent / f".{target.name}.staging-{uuid.uuid4().hex}"
    zip_created = False
    try:
        stage.mkdir()
        for source in sources:
            relative = source.relative_to(PROJECT_ROOT)
            output = stage / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, output)
        manifest = {
            "format": SOURCE_FORMAT,
            "schema_version": SOURCE_SCHEMA_VERSION,
            "release_version": str(release_version).strip(),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "selection": "explicit allowlist",
            "documentation_repository": "https://github.com/CatfoodKingdom/prisma-docs",
            "excluded_roots": list(EXCLUDED_PREFIXES),
            "excluded_assets": list(EXCLUDED_ASSETS),
            "files": _file_records(stage),
        }
        _write_json(stage / SOURCE_MANIFEST, manifest)
        report = validate_source_tree(stage)
        if target_zip is not None:
            report.update(_write_zip(stage, target_zip, archive_root_name=target.name))
            zip_created = True
        os.replace(stage, target)
        report["source_root"] = str(target)
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
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--destination", required=True, type=Path)
    prepare.add_argument("--release-version", required=True)
    prepare.add_argument("--zip-path", type=Path)
    validate = commands.add_parser("validate")
    validate.add_argument("--source-root", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "prepare":
            report = prepare_source_tree(
                destination=args.destination,
                release_version=args.release_version,
                zip_path=args.zip_path,
            )
        else:
            report = validate_source_tree(args.source_root)
    except SourcePreparationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
