"""Module toggle state persistence."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict

from .registry import (
    _PREPROCESSORS,
    _ensure_registry_populated,
)


def _normalize_module_state(state: Dict[str, bool]) -> Dict[str, bool]:
    """Fill defaults for live preprocessing modules and drop retired keys."""
    defaults = _get_all_defaults()
    normalized = {name: defaults[name] for name in defaults}
    for name in defaults:
        if name in state:
            normalized[name] = bool(state[name])
    return normalized


def _get_all_defaults() -> Dict[str, bool]:
    """Return {module_name: default_enabled} for all registered modules.

    Preprocessing operators are non-exclusive: each contributes its own
    default and several may be on at once. R4-B legitimizes the
    zero-enabled case as a no-op.
    """
    _ensure_registry_populated()
    defaults = {}
    for name, cls in _PREPROCESSORS.items():
        defaults[name] = getattr(cls, "default_enabled", False)
    return defaults


def load_module_state(path: Path) -> Dict[str, bool]:
    """Load module toggle state from JSON file, filling missing keys with defaults."""
    if path.exists():
        with open(path, encoding="utf-8") as f:
            saved = json.load(f)
        return _normalize_module_state(saved)
    return _normalize_module_state({})


def save_module_state(path: Path, state: Dict[str, bool]) -> None:
    """Write module toggle state to JSON file."""
    normalized = _normalize_module_state(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(normalized, f, indent=2)


def toggle_module(path: Path, module_id: str, enabled: bool) -> Dict[str, bool]:
    """Toggle a single module and persist. Returns updated full state."""
    known = set(_PREPROCESSORS)
    if module_id not in known:
        raise ValueError(f"Unknown module: {module_id!r}")
    state = load_module_state(path)
    state[module_id] = enabled
    state = _normalize_module_state(state)
    save_module_state(path, state)
    return state
