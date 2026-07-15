"""Filesystem-safe, collision-free export folder names for output/lithophanes/."""
from __future__ import annotations

import re
from pathlib import Path

_MAX_LEN = 64
_FALLBACK = "export"
_WINDOWS_RESERVED = (
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)


def _slugify(*parts: str) -> str:
    # Replace path separators with spaces so they become hyphens after sanitising,
    # rather than letting Path().stem strip palette labels that contain dots.
    cleaned = []
    for p in parts:
        if p:
            # Normalize path separators to spaces; keep everything else for the regex
            cleaned.append(re.sub(r"[/\\]", " ", p))
    raw = "-".join(cleaned)
    slug = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
    slug = slug[:_MAX_LEN].strip("-")
    if not slug:
        slug = _FALLBACK
    if slug in _WINDOWS_RESERVED:
        slug = f"{slug}-x"
    return slug


def make_export_id(image_path: str, output_root: Path, *, timestamp: str) -> str:
    """Return a unique, Windows-safe folder name ``{timestamp}-{image}`` under output_root.

    The timestamp leads so exports sort chronologically; the image stem follows for
    readability. Appends -2, -3, ... only as a same-second collision backstop (the
    timestamp already makes distinct exports unique). The palette is recorded in the
    manifest, not the folder name.
    """
    image_stem = Path(str(image_path)).stem if image_path else ""
    base = _slugify(str(timestamp or ""), image_stem)
    candidate = base
    n = 1
    while (output_root / candidate).exists():
        n += 1
        suffix = f"-{n}"
        # Keep the suffixed name within _MAX_LEN by trimming the base first.
        candidate = f"{base[:_MAX_LEN - len(suffix)].rstrip('-')}{suffix}"
    return candidate


def make_save_id(image_path: str, saved_runs_root: Path, *, timestamp: str) -> str:
    """Return a unique, Windows-safe save id ``{timestamp}-{image}`` (stable across renames).

    Mirrors make_export_id: timestamp leads for chronological sort; the image stem
    follows for readability. The display label is intentionally NOT part of the id
    (spec §3 — renaming must not change identity). Same-second collisions append
    -2, -3, ...; a save_id collides if either {save_id}.zip or {save_id}.json exists.
    """
    image_stem = Path(str(image_path)).stem if image_path else ""
    base = _slugify(str(timestamp or ""), image_stem)
    candidate = base
    n = 1
    while (saved_runs_root / f"{candidate}.zip").exists() or (saved_runs_root / f"{candidate}.json").exists():
        n += 1
        suffix = f"-{n}"
        candidate = f"{base[:_MAX_LEN - len(suffix)].rstrip('-')}{suffix}"
    return candidate
