"""Side-effect-controlled writers for extraction visual artifacts."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import tempfile
from typing import Callable, Protocol

import cv2
import numpy as np

from path_safety import require_unlinked_path


_SAMPLE_ID_RE = r"^[A-Za-z0-9_.-]+$"
_ARTIFACT_KINDS = {"source", "blank", "strip", "appearance", "transmission_roi"}
_STAGED_ARTIFACT_FILENAMES = {
    "blank": "blank_review.jpg",
    "appearance": "appearance_review.jpg",
    "transmission_roi": "transmission_review.jpg",
}


def staged_artifact_filename(kind: str) -> str:
    kind = _validate_artifact_kind(kind)
    return _STAGED_ARTIFACT_FILENAMES.get(kind, f"{kind}.jpg")


def _validate_sample_id(sample_id: str) -> str:
    import re

    sample_id = str(sample_id or "")
    if not re.fullmatch(_SAMPLE_ID_RE, sample_id):
        raise ValueError("invalid sample_id for extraction artifact path")
    return sample_id


def _validate_artifact_kind(kind: str) -> str:
    kind = str(kind or "")
    if kind not in _ARTIFACT_KINDS:
        raise ValueError(f"invalid extraction artifact kind: {kind!r}")
    return kind


def temporary_sibling_path(path: Path, *, label: str = "stage") -> Path:
    """Reserve a unique same-directory JPEG path for staged publication."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_path = tempfile.mkstemp(
        prefix=f".{path.stem}.{label}.",
        suffix=".jpg",
        dir=path.parent,
    )
    os.close(fd)
    return Path(raw_path)


def discard_staged_files(paths: list[Path] | tuple[Path, ...]) -> None:
    for path in paths:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass


def publish_staged_files(
    replacements: list[tuple[Path, Path]],
    *,
    boundary_hook: Callable[[str, Path], None] | None = None,
) -> list[Path]:
    """Promote a verified same-directory file set, restoring old files on failure."""
    normalized: list[tuple[Path, Path]] = []
    seen_targets: set[Path] = set()
    for staged, target in replacements:
        staged = Path(staged)
        target = Path(target)
        if staged == target or staged.parent.resolve() != target.parent.resolve():
            raise ValueError("staged artifacts must be distinct siblings of their live targets")
        if target in seen_targets:
            raise ValueError(f"duplicate publication target: {target}")
        if not staged.is_file() or staged.stat().st_size <= 0:
            raise RuntimeError(f"staged artifact is missing or empty: {staged}")
        seen_targets.add(target)
        normalized.append((staged, target))

    backups: dict[Path, Path] = {}
    promoted: list[Path] = []
    preserved_backups: set[Path] = set()
    try:
        for _staged, target in normalized:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if not target.is_file():
                    raise RuntimeError(f"artifact target is not a regular file: {target}")
                backup = temporary_sibling_path(target, label="rollback")
                try:
                    shutil.copy2(target, backup)
                except Exception:
                    backup.unlink(missing_ok=True)
                    raise
                backups[target] = backup
                if boundary_hook is not None:
                    boundary_hook("after_live_backup", target)
        for staged, target in normalized:
            os.replace(staged, target)
            promoted.append(target)
            if boundary_hook is not None:
                boundary_hook("after_live_replace", target)
    except Exception as exc:
        rollback_errors: list[str] = []
        for target in reversed(promoted):
            backup = backups.get(target)
            try:
                if backup is not None and backup.exists():
                    os.replace(backup, target)
                else:
                    target.unlink(missing_ok=True)
            except Exception as rollback_exc:
                if backup is not None and backup.exists():
                    preserved_backups.add(backup)
                    rollback_errors.append(
                        f"{target}: {rollback_exc} (recovery copy preserved at {backup})"
                    )
                else:
                    rollback_errors.append(f"{target}: {rollback_exc}")
        detail = f"; rollback failures: {'; '.join(rollback_errors)}" if rollback_errors else ""
        raise RuntimeError(f"artifact publication failed: {exc}{detail}") from exc
    finally:
        discard_staged_files([staged for staged, _target in normalized])
        discard_staged_files([
            backup for backup in backups.values()
            if backup not in preserved_backups
        ])
    return [target for _staged, target in normalized]


def stage_jpeg_image(
    img_bgr: np.ndarray,
    target: Path,
    *,
    max_dim: int | None = None,
    quality: int = 80,
) -> Path:
    """Encode and verify a JPEG beside its eventual live target."""
    if img_bgr.ndim < 2 or img_bgr.size == 0:
        raise ValueError("cannot encode an empty image")
    h, w = img_bgr.shape[:2]
    if max_dim is not None:
        scale = min(1.0, float(max_dim) / max(h, w))
        if scale < 1.0:
            img_bgr = cv2.resize(
                img_bgr,
                (max(1, int(w * scale)), max(1, int(h * scale))),
                interpolation=cv2.INTER_AREA,
            )
    staged = temporary_sibling_path(Path(target))
    try:
        encoded = cv2.imwrite(str(staged), img_bgr, [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
        if not encoded or not staged.is_file() or staged.stat().st_size <= 0:
            raise RuntimeError(f"JPEG encoder did not produce {target}")
        return staged
    except Exception:
        staged.unlink(missing_ok=True)
        raise


def write_thumbnail_image(img_bgr: np.ndarray, path: Path, *, max_dim: int = 800) -> Path:
    """Atomically write a checked, bounded JPEG artifact."""
    staged = stage_jpeg_image(img_bgr, path, max_dim=max_dim, quality=80)
    publish_staged_files([(staged, Path(path))])
    return path


class ExtractionArtifactSink(Protocol):
    """Destination for sample extraction visual artifacts."""

    def write_image(
        self,
        sample_id: str,
        kind: str,
        image_bgr: np.ndarray,
        *,
        max_dim: int = 800,
    ) -> Path:
        ...

    def wants_image(self, kind: str) -> bool:
        ...


@dataclass(frozen=True)
class LiveThumbnailSink:
    """Writes normal live thumbnails under ``{data_root}/thumbnails``."""

    thumbnail_root: Path

    def wants_image(self, kind: str) -> bool:
        return _validate_artifact_kind(kind) in {"source", "strip"}

    def write_image(
        self,
        sample_id: str,
        kind: str,
        image_bgr: np.ndarray,
        *,
        max_dim: int = 800,
    ) -> Path:
        sample_id = _validate_sample_id(sample_id)
        kind = _validate_artifact_kind(kind)
        if not self.wants_image(kind):
            raise ValueError(f"{kind!r} is a review-only extraction artifact")
        target = self.thumbnail_root / sample_id / f"{kind}.jpg"
        require_unlinked_path(target, self.thumbnail_root.parent)
        return write_thumbnail_image(
            image_bgr,
            target,
            max_dim=max_dim,
        )


@dataclass(frozen=True)
class StagedLiveThumbnailSink:
    """Stages the two live visuals away from their observable final paths."""

    sample_dir: Path
    sample_id: str

    def wants_image(self, kind: str) -> bool:
        return _validate_artifact_kind(kind) in {"source", "strip"}

    def write_image(
        self,
        sample_id: str,
        kind: str,
        image_bgr: np.ndarray,
        *,
        max_dim: int = 800,
    ) -> Path:
        sample_id = _validate_sample_id(sample_id)
        if sample_id != self.sample_id:
            raise ValueError("staged live visual sink belongs to another sample")
        kind = _validate_artifact_kind(kind)
        if not self.wants_image(kind):
            raise ValueError(f"{kind!r} is not a durable live extraction visual")
        target = self.sample_dir / f"{kind}.jpg"
        require_unlinked_path(target, self.sample_dir, allow_boundary=False)
        return write_thumbnail_image(image_bgr, target, max_dim=max_dim)

    def visual_paths(self) -> dict[str, Path]:
        paths = {kind: self.sample_dir / f"{kind}.jpg" for kind in ("source", "strip")}
        for kind, path in paths.items():
            require_unlinked_path(path, self.sample_dir, allow_boundary=False)
            if not path.is_file() or path.stat().st_size <= 0:
                raise RuntimeError(f"staged live extraction visual is missing: {kind}")
        return paths


@dataclass(frozen=True)
class SampleArtifactDirectorySink:
    """Writes one sample's staged candidate artifacts into a fixed directory."""

    sample_dir: Path

    def wants_image(self, kind: str) -> bool:
        _validate_artifact_kind(kind)
        return True

    def write_image(
        self,
        sample_id: str,
        kind: str,
        image_bgr: np.ndarray,
        *,
        max_dim: int = 800,
    ) -> Path:
        del sample_id
        kind = _validate_artifact_kind(kind)
        target = self.sample_dir / staged_artifact_filename(kind)
        require_unlinked_path(target, self.sample_dir, allow_boundary=False)
        return write_thumbnail_image(
            image_bgr,
            target,
            max_dim=max_dim,
        )


def sink_wants_image(sink: ExtractionArtifactSink, kind: str) -> bool:
    """Allow older test/extension sinks while making built-in policy explicit."""
    wants_image = getattr(sink, "wants_image", None)
    return bool(wants_image(kind)) if callable(wants_image) else True
