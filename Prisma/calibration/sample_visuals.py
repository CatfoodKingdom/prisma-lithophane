"""Lifecycle helpers for accepted-sample display artifacts.

Only ``source.jpg`` and ``strip.jpg`` are durable accepted-sample visuals.
Other extraction images belong to an active review workspace and must not be
promoted into the live thumbnail tree.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Iterable

from path_safety import safe_rmtree, safe_unlink


DURABLE_SAMPLE_VISUAL_KINDS = ("source", "strip")
TRANSIENT_SAMPLE_VISUAL_KINDS = ("blank", "appearance", "transmission_roi")
_SAMPLE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
logger = logging.getLogger(__name__)


def _sample_visual_dir(data_root: Path, sample_id: str) -> Path:
    sample_id = str(sample_id or "")
    if not _SAMPLE_ID_RE.fullmatch(sample_id):
        raise ValueError("invalid sample_id for sample visual path")
    return Path(data_root) / "thumbnails" / sample_id


def manual_review_visual_dir(data_root: Path, sample_id: str) -> Path:
    sample_id = str(sample_id or "")
    if not _SAMPLE_ID_RE.fullmatch(sample_id):
        raise ValueError("invalid sample_id for manual review path")
    return Path(data_root) / "maintenance" / "manual_extraction_reviews" / sample_id


def remove_manual_review_visuals(data_root: Path, sample_id: str) -> bool:
    review_dir = manual_review_visual_dir(data_root, sample_id)
    if not review_dir.exists() and not review_dir.is_symlink():
        return False
    safe_rmtree(review_dir, Path(data_root))
    return True


def remove_all_manual_review_visuals(data_root: Path) -> bool:
    review_root = Path(data_root) / "maintenance" / "manual_extraction_reviews"
    if not review_root.exists() and not review_root.is_symlink():
        return False
    safe_rmtree(review_root, Path(data_root))
    return True


def remove_sample_visuals(
    data_root: Path,
    sample_id: str,
    *,
    kinds: Iterable[str] | None = None,
) -> list[Path]:
    """Remove live visuals without ever traversing a linked sample directory."""
    sample_dir = _sample_visual_dir(data_root, sample_id)
    if not sample_dir.exists() and not sample_dir.is_symlink():
        return []

    if kinds is None:
        safe_rmtree(sample_dir, Path(data_root))
        return [sample_dir]

    removed: list[Path] = []
    for kind in kinds:
        if kind not in DURABLE_SAMPLE_VISUAL_KINDS + TRANSIENT_SAMPLE_VISUAL_KINDS:
            raise ValueError(f"invalid sample visual kind: {kind!r}")
        path = sample_dir / f"{kind}.jpg"
        if path.exists() or path.is_symlink():
            safe_unlink(path, Path(data_root))
            removed.append(path)
    try:
        sample_dir.rmdir()
    except OSError:
        pass
    return removed


def discard_transient_sample_visuals(data_root: Path, sample_id: str) -> None:
    """Best-effort cleanup that cannot turn a committed extraction into failure."""
    try:
        remove_sample_visuals(
            data_root,
            sample_id,
            kinds=TRANSIENT_SAMPLE_VISUAL_KINDS,
        )
    except OSError:
        logger.warning("Could not remove retired live review visuals for %s", sample_id, exc_info=True)
