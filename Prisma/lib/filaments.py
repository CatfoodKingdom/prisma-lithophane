"""filaments.py — Load and query the filament registry."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional


def load_registry(registry_path: Path) -> dict:
    """Load filament registry JSON and return as dict keyed by filament_id."""
    with open(registry_path, encoding="utf-8") as f:
        return json.load(f)


def list_filament_ids(registry_path: Path) -> list[str]:
    """Return sorted list of filament IDs from the registry."""
    return sorted(load_registry(registry_path).keys())


def get_filament(registry_path: Path, filament_id: str) -> Optional[dict]:
    """Look up a single filament by ID. Returns None if not found."""
    registry = load_registry(registry_path)
    info = registry.get(filament_id)
    if info is None:
        return None
    return {"filament_id": filament_id, **info}
