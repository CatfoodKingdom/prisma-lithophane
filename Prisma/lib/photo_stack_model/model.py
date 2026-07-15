"""Lightweight loaded-artifact representation for the photo stack model."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PhotoStackModelArtifact:
    """A fitted candidate artifact loaded from a run directory."""

    run_dir: Path
    manifest: dict[str, Any]
    model: dict[str, Any]
    corrections: dict[str, Any]


def load_candidate(run_dir: str | Path) -> PhotoStackModelArtifact:
    """Load core candidate files from a candidate run directory."""

    path = Path(run_dir).resolve()
    with open(path / "manifest.json", encoding="utf-8") as f:
        manifest = json.load(f)
    with open(path / "model.json", encoding="utf-8") as f:
        model = json.load(f)
    corrections_path = path / "correction_layer.json"
    if not corrections_path.exists():
        corrections_path = path / "corrections.json"
    with open(corrections_path, encoding="utf-8") as f:
        corrections = json.load(f)
    return PhotoStackModelArtifact(path, manifest, model, corrections)
