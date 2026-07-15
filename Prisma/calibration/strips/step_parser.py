"""
step_parser.py — STEP filename parsing.

Parses structured metadata from STEP filenames in both the current
double-underscore format and the legacy single-underscore format.
"""
from __future__ import annotations

import re


def parse_step_filename(filename: str) -> dict | None:
    """Parse a STEP filename into structured metadata.

    Returns dict with variable_thicknesses_mm, fixed_layers, layer_height_mm, layer_count.
    Returns None if unparseable.
    """
    name = filename
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    if "\\" in name:
        name = name.rsplit("\\", 1)[-1]
    name = re.sub(r"\.step$", "", name, flags=re.IGNORECASE)

    if not name:
        return None

    result = {
        "variable_thicknesses_mm": [],
        "fixed_layers": [],
        "layer_height_mm": 0.1,
        "layer_count": 1,
    }

    # Double-underscore format: 1L__v-...__f1-...__lh0.08
    if "__" in name:
        segments = name.split("__")
        for seg in segments:
            seg = seg.strip()
            m = re.match(r"^(\d+)L$", seg)
            if m:
                result["layer_count"] = int(m.group(1))
                continue
            if seg.startswith("v-"):
                vals = seg[2:].split("-")
                result["variable_thicknesses_mm"] = [float(v) for v in vals if v]
                continue
            m = re.match(r"^f(\d+)-(.+)$", seg)
            if m:
                result["fixed_layers"].append({"thickness_mm": float(m.group(2))})
                continue
            m = re.match(r"^lh(.+)$", seg)
            if m:
                result["layer_height_mm"] = float(m.group(1))
                continue
        return result

    # Legacy single-underscore format
    m = re.match(r"^(\d+)L[_]", name)
    if m:
        result["layer_count"] = int(m.group(1))

    m_lh = re.search(r"_lh([\d.]+)$", name)
    if m_lh:
        result["layer_height_mm"] = float(m_lh.group(1))

    m_var = re.search(r"(?:v|manual|linear)-([\d.|,-]+?)(?=_[fl]|$)", name)
    if m_var:
        raw = m_var.group(1)
        sep = "|" if "|" in raw else "-"
        vals = raw.split(sep)
        result["variable_thicknesses_mm"] = [float(v) for v in vals if v]

    m_fixed = re.search(r"_f-([\d.]+(?:-[\d.]+)*)", name)
    if m_fixed:
        vals = m_fixed.group(1).split("-")
        result["fixed_layers"] = [{"thickness_mm": float(v)} for v in vals if v]

    if not result["variable_thicknesses_mm"]:
        return None

    return result
