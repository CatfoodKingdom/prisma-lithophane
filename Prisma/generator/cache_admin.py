"""Junction/symlink-safe deletion bounded to a root directory (Windows-aware)."""
from __future__ import annotations

import os
import stat
import time
from pathlib import Path
from typing import Iterable


class OutsideRootError(ValueError):
    """Raised when a clear target is not inside the permitted root."""


def _is_link_or_junction(path: Path) -> bool:
    """True for symlinks AND Windows directory junctions (reparse points)."""
    try:
        st = os.lstat(str(path))
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    # Windows junctions: reparse-point attribute on st_file_attributes.
    FILE_ATTRIBUTE_REPARSE_POINT = 0x400
    return bool(getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)


def _unlink_link(path: Path) -> None:
    """Remove a symlink/junction ENTRY without recursing through it."""
    try:
        os.rmdir(str(path))   # junction-to-dir / dir symlink
    except OSError:
        os.unlink(str(path))  # file symlink


def _rm_tree(path: Path) -> None:
    """Recursively delete a real directory, never following links/junctions."""
    for entry in os.scandir(str(path)):
        child = Path(entry.path)
        if _is_link_or_junction(child):
            _unlink_link(child)
        elif entry.is_dir(follow_symlinks=False):
            _rm_tree(child)
            os.rmdir(str(child))
        else:
            os.unlink(str(child))


def _validated_paths(target: Path, root: Path) -> tuple[Path, Path]:
    """Return safe lexical target/root paths without following reparse points."""

    # Lexical absolutisation: resolves '..' but does NOT follow symlinks/junctions.
    target = Path(os.path.normpath(os.path.abspath(str(target))))
    root = Path(os.path.normpath(os.path.abspath(str(root))))
    if target != root and root not in target.parents:
        raise OutsideRootError(f"{target} is not under {root}")
    # Refuse if root OR ANY ancestor above it is a link/junction. Lexical paths
    # (above) do not resolve junctions, so a junction anywhere from the filesystem
    # anchor down to root would physically relocate the whole cache tree outside
    # the intended location and let deletion escape. Inspect every ancestor.
    for ancestor in (root, *root.parents):
        if _is_link_or_junction(ancestor):
            raise OutsideRootError(
                f"cache root ancestor {ancestor} is a symlink/junction"
            )
    # Refuse if any path component between root and target is a link/junction.
    walk = root
    for part in target.relative_to(root).parts:
        walk = walk / part
        if _is_link_or_junction(walk):
            raise OutsideRootError(f"path component {walk} is a symlink/junction")
    return target, root


def _remove_entry(path: Path) -> None:
    """Remove one entry without following a link or junction."""

    if _is_link_or_junction(path):
        _unlink_link(path)
    elif path.is_dir():
        _rm_tree(path)
        os.rmdir(str(path))
    else:
        os.unlink(str(path))


def safe_clear_dir(target: Path, *, root: Path) -> int:
    """Delete the contents of ``target`` while keeping ``target`` itself."""

    target, _root = _validated_paths(target, root)
    if not target.exists():
        return 0
    removed = 0
    for entry in os.scandir(str(target)):
        child = Path(entry.path)
        _remove_entry(child)
        removed += 1
    return removed


def safe_clear_dir_except(
    target: Path,
    *,
    root: Path,
    preserve_names: Iterable[str],
    retries: int = 1,
) -> dict:
    """Clear direct children of ``target`` except an explicit name allowlist.

    Preserved names apply only to direct, real children. A symlink or junction
    whose name matches the allowlist is rejected rather than preserved. Failed
    removals are retried after a short yield and returned to the caller so a
    desktop launcher can report them without making startup impossible.
    """

    target, _root = _validated_paths(target, root)
    anchor = Path(target.anchor)
    home = Path(os.path.normpath(os.path.abspath(str(Path.home()))))
    if target in {anchor, home} or target.parent == anchor:
        raise OutsideRootError(f"refusing unsafe clear root: {target}")
    preserved = {str(name) for name in preserve_names}
    if any(not name or Path(name).name != name for name in preserved):
        raise ValueError("preserve_names must contain direct child names")
    if not target.exists():
        return {"removed": 0, "preserved": [], "failures": []}

    preserved_paths: list[str] = []
    pending: list[tuple[Path, str]] = []
    removed = 0
    for entry in os.scandir(str(target)):
        child = Path(entry.path)
        if (
            child.name in preserved
            and not _is_link_or_junction(child)
            and child.is_dir()
        ):
            preserved_paths.append(child.name)
            continue
        try:
            _remove_entry(child)
            removed += 1
        except OSError as exc:
            pending.append((child, str(exc)))

    attempts = max(0, int(retries))
    for _attempt in range(attempts):
        if not pending:
            break
        time.sleep(0)
        retrying, pending = pending, []
        for child, _previous_error in retrying:
            try:
                _remove_entry(child)
                removed += 1
            except OSError as exc:
                pending.append((child, str(exc)))

    return {
        "removed": removed,
        "preserved": sorted(preserved_paths),
        "failures": [
            {"path": str(child), "error": error}
            for child, error in pending
        ],
    }
