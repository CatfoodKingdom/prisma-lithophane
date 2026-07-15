"""Filesystem boundaries for destructive operations on Prisma-managed trees."""
from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path
from typing import Any, Callable


class UnsafeManagedPathError(OSError):
    """Raised when a destructive path crosses or contains a filesystem link."""


def lexical_absolute(path: Path) -> Path:
    """Return an absolute normalized path without resolving links."""
    return Path(os.path.abspath(os.fspath(path)))


def is_linklike(path: Path) -> bool:
    """Recognize symlinks, Windows junctions, and other reparse points."""
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
        # Unreadable path metadata is not safe enough for automatic deletion.
        return True


def require_unlinked_path(path: Path, boundary: Path, *, allow_boundary: bool = False) -> Path:
    """Require lexical containment and an ordinary path component chain."""
    candidate = lexical_absolute(path)
    root = lexical_absolute(boundary)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise UnsafeManagedPathError(f"Managed path escapes its boundary: {candidate}") from exc
    if candidate == root and not allow_boundary:
        raise UnsafeManagedPathError(f"Refusing destructive access to managed root itself: {root}")

    current = candidate
    while True:
        if is_linklike(current):
            raise UnsafeManagedPathError(f"Managed path contains a filesystem link: {current}")
        if current == root:
            break
        parent = current.parent
        if parent == current:
            raise UnsafeManagedPathError(f"Could not reach managed boundary from: {candidate}")
        current = parent
    return candidate


def tree_contains_link(path: Path) -> bool:
    """Inspect an ordinary tree without following links."""
    pending = [Path(path)]
    while pending:
        current = pending.pop()
        try:
            children = list(current.iterdir())
        except OSError:
            return True
        for child in children:
            if is_linklike(child):
                return True
            try:
                if child.is_dir():
                    pending.append(child)
            except OSError:
                return True
    return False


def _retry_readonly_removal(function: Callable[..., Any], path: str, error: BaseException) -> None:
    """Retry one rmtree operation after clearing a Windows read-only flag."""
    if not isinstance(error, PermissionError):
        raise error
    target = Path(path)
    if is_linklike(target):
        raise UnsafeManagedPathError(f"Refusing read-only cleanup through a filesystem link: {target}") from error
    os.chmod(target, stat.S_IREAD | stat.S_IWRITE)
    function(path)


def safe_rmtree(path: Path, boundary: Path) -> None:
    """Remove one contained ordinary tree; never traverse a link."""
    target = require_unlinked_path(path, boundary)
    if not target.exists():
        return
    if not target.is_dir():
        raise UnsafeManagedPathError(f"Managed tree target is not a directory: {target}")
    if tree_contains_link(target):
        raise UnsafeManagedPathError(f"Managed tree contains a filesystem link: {target}")
    shutil.rmtree(target, onexc=_retry_readonly_removal)


def safe_unlink(path: Path, boundary: Path) -> None:
    """Unlink one contained regular file; refuse links and directories."""
    target = require_unlinked_path(path, boundary)
    if not target.exists():
        return
    if not target.is_file() or is_linklike(target):
        raise UnsafeManagedPathError(f"Managed file target is not an ordinary file: {target}")
    try:
        target.unlink()
    except PermissionError:
        # Imported camera files can retain Windows' read-only attribute. Recheck
        # the complete boundary before changing attributes so a path swap cannot
        # turn this retry into destructive access through a link.
        target = require_unlinked_path(target, boundary)
        if not target.is_file() or is_linklike(target):
            raise UnsafeManagedPathError(f"Managed file target is not an ordinary file: {target}")
        os.chmod(target, stat.S_IREAD | stat.S_IWRITE)
        target.unlink()


def require_single_link_file(path: Path) -> None:
    """Reject a hardlinked mutable file, which would silently share writes."""
    target = lexical_absolute(path)
    try:
        link_count = int(target.stat().st_nlink)
    except OSError as exc:
        raise UnsafeManagedPathError(f"Could not inspect managed file links: {target}") from exc
    if link_count != 1:
        raise UnsafeManagedPathError(
            f"Mutable managed file has {link_count} hardlinks and is unsafe to open: {target}"
        )
