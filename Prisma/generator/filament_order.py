"""Canonical color-filament ordering for generator solves."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Mapping, Any

import data_paths


DEFAULT_LUMA = 128.0
_REGISTRY_PATH = data_paths.DATA_DIR / "filaments" / "registry.json"


def load_filament_order_registry(path: Path | None = None) -> dict:
    """Load the filament registry used for solve-order canonicalization."""
    registry_path = _REGISTRY_PATH if path is None else Path(path)
    if registry_path.exists():
        with open(registry_path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def filament_luma(
    filament_id: str,
    registry: Mapping[str, Mapping[str, Any]] | None = None,
) -> float:
    """Return Rec.709 luma from registry hex, or mid-gray for unknown values."""
    entry = (registry or {}).get(str(filament_id)) or {}
    hex_value = str(entry.get("hex", "") or "").strip().lstrip("#")
    if len(hex_value) >= 6:
        try:
            r = int(hex_value[0:2], 16)
            g = int(hex_value[2:4], 16)
            b = int(hex_value[4:6], 16)
            return 0.2126 * r + 0.7152 * g + 0.0722 * b
        except ValueError:
            pass
    return DEFAULT_LUMA


def canonical_palette_order(
    palette: Iterable[str],
    registry: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[str]:
    """Sort color filaments dark-first, with filament id as the total tie-break."""
    return sorted(
        (str(fid) for fid in palette),
        key=lambda fid: (filament_luma(fid, registry), fid),
    )
