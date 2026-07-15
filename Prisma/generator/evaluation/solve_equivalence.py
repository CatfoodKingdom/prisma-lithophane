"""Deterministic fingerprints for benchmark-gated Generator solve changes.

This module deliberately records product outputs and stable diagnostic counters,
but not wall-clock measurements.  It is used by the performance benchmark and
its regression tests; the normal Generator runtime does not import it.
"""
from __future__ import annotations

from dataclasses import fields, is_dataclass
from enum import Enum
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping

import numpy as np


SNAPSHOT_SCHEMA = "prisma-generator-solve-equivalence-v1"
_DIAGNOSTIC_ONLY_COUNTER_PREFIXES = (
    "stage2_printability_ledger_",
)


def _is_volatile_key(key: str) -> bool:
    """Return True for measurements that legitimately vary between runs."""

    normalized = key.lower()
    return (
        normalized == "timings_s"
        or normalized in {
            "runtime_s",
            "elapsed_s",
            "wall_s",
            "cpu_s",
            "cache_hit",
        }
        or normalized.endswith("_runtime_s")
        or normalized.endswith("_elapsed_s")
        or normalized.endswith("_wall_s")
        or normalized.endswith("_cpu_s")
        or normalized.endswith("_seconds")
    )


def _array_record(value: np.ndarray) -> dict[str, Any]:
    array = np.asarray(value)
    if array.dtype.hasobject:
        raise TypeError("object arrays are not valid solve-equivalence artifacts")
    contiguous = np.ascontiguousarray(array)
    return {
        "kind": "ndarray",
        "dtype": contiguous.dtype.str,
        "shape": list(contiguous.shape),
        "sha256": hashlib.sha256(contiguous.tobytes(order="C")).hexdigest(),
    }


def _canonicalize(value: Any, *, field_name: str = "") -> Any:
    if _is_volatile_key(field_name):
        return None
    if isinstance(value, np.ndarray):
        return _array_record(value)
    if isinstance(value, np.generic):
        return _canonicalize(value.item(), field_name=field_name)
    if is_dataclass(value) and not isinstance(value, type):
        return {
            entry.name: _canonicalize(
                getattr(value, entry.name),
                field_name=entry.name,
            )
            for entry in fields(value)
            if not _is_volatile_key(entry.name)
        }
    if isinstance(value, Mapping):
        return {
            str(key): _canonicalize(item, field_name=str(key))
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if not _is_volatile_key(str(key))
        }
    if isinstance(value, (list, tuple)):
        return [_canonicalize(item) for item in value]
    if isinstance(value, (set, frozenset)):
        canonical_items = [_canonicalize(item) for item in value]
        return sorted(
            canonical_items,
            key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
        )
    if isinstance(value, Enum):
        return _canonicalize(value.value, field_name=field_name)
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, bytes):
        return {
            "kind": "bytes",
            "length": len(value),
            "sha256": hashlib.sha256(value).hexdigest(),
        }
    if isinstance(value, float):
        if math.isnan(value):
            return {"kind": "float", "value": "nan"}
        if math.isinf(value):
            return {"kind": "float", "value": "inf" if value > 0 else "-inf"}
        return value
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(
        f"unsupported solve-equivalence value {type(value).__name__}"
        + (f" in field {field_name!r}" if field_name else "")
    )


def _digest(value: Any) -> str:
    encoded = json.dumps(
        _canonicalize(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _add_detail_sections(
    sections: dict[str, Any],
    prefix: str,
    value: Any,
    *,
    depth: int = 2,
) -> None:
    """Add bounded field-level hashes so mismatches are actionable."""

    if depth <= 0:
        return
    if is_dataclass(value) and not isinstance(value, type):
        for entry in fields(value):
            if _is_volatile_key(entry.name):
                continue
            child_prefix = f"{prefix}.{entry.name}"
            child = getattr(value, entry.name)
            sections[child_prefix] = child
            _add_detail_sections(
                sections,
                child_prefix,
                child,
                depth=depth - 1,
            )
    elif isinstance(value, Mapping):
        for key, child in sorted(value.items(), key=lambda pair: str(pair[0])):
            key_text = str(key)
            if _is_volatile_key(key_text):
                continue
            child_prefix = f"{prefix}.{key_text}"
            sections[child_prefix] = child
            _add_detail_sections(
                sections,
                child_prefix,
                child,
                depth=depth - 1,
            )


def fingerprint_sections(sections: Mapping[str, Any]) -> dict[str, Any]:
    """Fingerprint named output sections without retaining bulky payloads."""

    section_hashes = {
        str(name): _digest(value)
        for name, value in sorted(sections.items(), key=lambda pair: str(pair[0]))
    }
    root = hashlib.sha256(
        json.dumps(
            section_hashes,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "schema": SNAPSHOT_SCHEMA,
        "root_sha256": root,
        "sections": section_hashes,
    }


def solve_equivalence_snapshot(result: Any) -> dict[str, Any]:
    """Create an exact, deterministic output snapshot for one ``SolveResult``.

    Appearance providers and timing measurements are intentionally absent.  The
    provider is an input dependency, while timing data belongs in the adjacent
    benchmark record.  Stable performance counters remain part of the contract.
    """

    staged = getattr(result, "staged_result", None)
    sections: dict[str, Any] = {
        "thickness_maps": result.thickness_maps,
        "diagnostics": getattr(result, "diagnostics", {}),
        "debug_maps": getattr(result, "debug_maps", {}),
        "export_maps": getattr(result, "export_maps", {}),
        "export_metadata": getattr(result, "export_metadata", {}),
        "preprocessing_metrics": getattr(result, "preprocessing_metrics", {}),
        "cap_quality": getattr(result, "cap_quality", {}),
        "blueprint_triage": getattr(result, "blueprint_triage", None),
        "swap_grouping": getattr(result, "swap_grouping", None),
        "stats": result.stats,
        "solved_plan": getattr(result, "solved_plan", None),
        "rendered_outputs": {
            "reference_image": getattr(result, "reference_image", None),
            "palette_fit_image": getattr(result, "palette_fit_image", None),
            "palette_fit_de": getattr(result, "palette_fit_de", None),
            "solver_loss_map": getattr(result, "solver_loss_map", None),
            "palette_fit_rms_de": getattr(result, "palette_fit_rms_de", None),
            "solver_loss_rms_de": getattr(result, "solver_loss_rms_de", None),
            "image_domain_width_mm": getattr(result, "image_domain_width_mm", None),
            "image_domain_height_mm": getattr(result, "image_domain_height_mm", None),
        },
    }
    for mapping_name in (
        "thickness_maps",
        "diagnostics",
        "debug_maps",
        "export_maps",
    ):
        mapping = getattr(result, mapping_name, {})
        for key, value in sorted(mapping.items(), key=lambda pair: str(pair[0])):
            prefix = f"{mapping_name}.{key}"
            sections[prefix] = value
            _add_detail_sections(sections, prefix, value)
    if staged is not None:
        visible_plan = staged.visible_plan
        stable_counters = {
            str(key): value
            for key, value in staged.performance_profile.counters.items()
            if not any(
                str(key).startswith(prefix)
                for prefix in _DIAGNOSTIC_ONLY_COUNTER_PREFIXES
            )
        }
        sections.update(
            {
                "staged_compiled_directives": staged.compiled_directives,
                "staged_receipts": staged.receipts,
                "staged_planning_diagnostics": staged.planning_diagnostics,
                "staged_lateral_zone_plan": staged.lateral_zone_plan,
                "staged_visible_plan": visible_plan,
                "staged_visible_recipe_label_map": visible_plan.recipe_label_map,
                "staged_filler_plan": staged.filler_plan,
                "staged_cap_plan": staged.cap_plan,
                "staged_compatibility_bundle": staged.compatibility_bundle,
                "staged_stable_counters": stable_counters,
            }
        )
        for artifact_name in (
            "lateral_zone_plan",
            "visible_plan",
            "filler_plan",
            "cap_plan",
            "compatibility_bundle",
        ):
            artifact = getattr(staged, artifact_name)
            _add_detail_sections(
                sections,
                f"staged_{artifact_name}",
                artifact,
                depth=3,
            )
    return fingerprint_sections(sections)


def snapshot_differences(
    expected: Mapping[str, Any],
    actual: Mapping[str, Any],
) -> list[str]:
    """Describe changed/missing sections in two compact snapshots."""

    if expected.get("schema") != actual.get("schema"):
        return [
            "snapshot schema differs: "
            f"{expected.get('schema')!r} != {actual.get('schema')!r}"
        ]
    expected_sections = dict(expected.get("sections") or {})
    actual_sections = dict(actual.get("sections") or {})
    names = sorted(set(expected_sections) | set(actual_sections))
    differences: list[str] = []
    for name in names:
        if name not in expected_sections:
            differences.append(f"unexpected section: {name}")
        elif name not in actual_sections:
            differences.append(f"missing section: {name}")
        elif expected_sections[name] != actual_sections[name]:
            differences.append(f"changed section: {name}")
    if not differences and expected.get("root_sha256") != actual.get("root_sha256"):
        differences.append("root digest differs despite matching section digests")
    return differences


def assert_snapshots_equal(
    expected: Mapping[str, Any],
    actual: Mapping[str, Any],
) -> None:
    differences = snapshot_differences(expected, actual)
    if differences:
        raise AssertionError("solve output changed: " + "; ".join(differences))
