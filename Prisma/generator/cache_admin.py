"""Junction/symlink-safe deletion bounded to a root directory (Windows-aware)."""
from __future__ import annotations

import os
import stat
from pathlib import Path


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


def safe_clear_dir(target: Path, *, root: Path) -> int:
    """Delete the CONTENTS of `target` (keeping `target` itself), refusing any
    target not inside `root`, and never following a symlink/junction out of root.
    Returns the number of top-level entries removed.

    Containment is checked on LEXICAL absolute paths (normalized, but WITHOUT
    following symlinks/junctions), and the root plus every path component down to
    target must be a real directory — never a reparse point. This prevents a
    junctioned root (or ancestor) from redirecting deletion outside the cache.
    """
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
    if not target.exists():
        return 0
    removed = 0
    for entry in os.scandir(str(target)):
        child = Path(entry.path)
        if _is_link_or_junction(child):
            _unlink_link(child)
        elif entry.is_dir(follow_symlinks=False):
            _rm_tree(child)
            os.rmdir(str(child))
        else:
            os.unlink(str(child))
        removed += 1
    return removed
