"""Conservative startup cleanup for interrupted Generator export stages."""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil


EXPORT_STAGE_NAME_RE = re.compile(
    r"^\.export-stage-[a-z0-9](?:[a-z0-9-]{0,63})-[0-9a-f]{8}$"
)


def _is_linklike(path: Path) -> bool:
    path = Path(path)
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if callable(is_junction) and is_junction():
            return True
        attributes = int(getattr(path.lstat(), "st_file_attributes", 0) or 0)
        return bool(attributes & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT
    except FileNotFoundError:
        return False
    except OSError:
        return True


def _tree_contains_link(path: Path) -> bool:
    pending = [Path(path)]
    while pending:
        current = pending.pop()
        try:
            children = list(current.iterdir())
        except OSError:
            return True
        for child in children:
            if _is_linklike(child):
                return True
            try:
                if child.is_dir():
                    pending.append(child)
            except OSError:
                return True
    return False


def _retry_readonly_removal(function, path: str, error: BaseException) -> None:
    if not isinstance(error, PermissionError):
        raise error
    target = Path(path)
    if _is_linklike(target):
        raise OSError(f"refusing read-only cleanup through a filesystem link: {target}") from error
    os.chmod(target, 0o600)
    function(path)


def reconcile_interrupted_export_stages(export_root: str | Path) -> dict[str, object]:
    """Remove only provably private stages after the caller owns the app lock."""
    root = Path(os.path.abspath(os.fspath(export_root)))
    removed: list[str] = []
    findings: list[dict[str, str]] = []
    report: dict[str, object] = {"removed": removed, "findings": findings}
    if not root.exists():
        return report
    if _is_linklike(root) or not root.is_dir():
        findings.append({"status": "unsafe_export_root", "path": str(root)})
        return report
    try:
        children = sorted(root.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        findings.append({"status": "export_root_scan_failed", "path": str(root), "error": str(exc)})
        return report

    for child in children:
        if not child.name.startswith(".export-stage-"):
            continue
        if not EXPORT_STAGE_NAME_RE.fullmatch(child.name):
            findings.append({"status": "preserved_unrecognized_stage", "path": str(child)})
            continue
        if _is_linklike(child) or not child.is_dir() or _tree_contains_link(child):
            findings.append({"status": "preserved_unsafe_stage", "path": str(child)})
            continue
        try:
            # Recheck immediately before deletion; the direct-child name and
            # ordinary-tree proof are the complete disposable-stage schema.
            if child.parent != root or _is_linklike(child) or _tree_contains_link(child):
                findings.append({"status": "preserved_unsafe_stage", "path": str(child)})
                continue
            shutil.rmtree(child, onexc=_retry_readonly_removal)
            removed.append(str(child))
        except OSError as exc:
            findings.append({"status": "stage_cleanup_failed", "path": str(child), "error": str(exc)})
    return report
