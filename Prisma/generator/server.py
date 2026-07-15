"""
server.py — FastAPI backend for the unified lithophane generator web UI.

Wraps the lithophane generator solve pipeline and post-solve print-file
exporter. All computation is server-side; the frontend polls for status.

Launch:
    cd unified_generator/pipeline
    uvicorn server:app --reload --port 8001
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import math
import os
import re
import shutil
import threading
import time
import uuid
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Mapping, Optional
from urllib.parse import quote

import numpy as np
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator
from PIL import Image, ImageOps


class SolveCancelled(Exception):
    """Raised by the progress callback when cancellation is requested."""
    pass


class ExportCancelled(Exception):
    """Raised by the export progress callback when cancellation is requested."""
    pass

# ---------------------------------------------------------------------------
# Path setup — Prisma/generator/server.py
# ---------------------------------------------------------------------------

_GEN_DIR = Path(__file__).resolve().parent          # Prisma/generator/
_PRISMA_DIR = _GEN_DIR.parent                       # Prisma/

import sys

if str(_GEN_DIR) not in sys.path:
    sys.path.insert(0, str(_GEN_DIR))
if str(_PRISMA_DIR) not in sys.path:
    sys.path.insert(0, str(_PRISMA_DIR))

import data_paths  # noqa: E402
from progress import ProgressCancelled, ProgressReporter  # noqa: E402

from pipeline_cli import (  # noqa: E402
    load_image,
    apply_adjustments,
)
from model import (  # noqa: E402
    load_profile as _load_profile,
    predict_transmission,
    srgb_to_linear,
    to_oklab,
)
from facade import (  # noqa: E402
    SolveConfig,
    SolveStats,
    solve_preview,
    solve_full,
    solve_compare,
)
from filament_order import canonical_palette_order, load_filament_order_registry  # noqa: E402
from grouping.banded_export import (  # noqa: E402
    band_slot_tables,
    banded_color_ceiling_map,
    banded_export_plan_from_metadata,
    banded_fill_maps,
)
from grouping.band_plan import band_fill_thicknesses  # noqa: E402
from thickness_maps import MapKey  # noqa: E402
from white_cap_contract import (  # noqa: E402
    PHYSICAL_GEOMETRY_METADATA_KEY,
    WHITE_CAP_FIELD_TARGET_METADATA_KEY,
    WHITE_CAP_FIELD_TARGET_UPPER_SURFACE_KEY,
)
from recipe_cookbook import build_recipe_cookbook  # noqa: E402
from mesh.export_serializers import (  # noqa: E402
    coalesce_color_quarantine_for_3mf_bundle,
    write_export_mesh_bundle_as_3mf,
)
from mesh.material_maps import prepare_export_material_maps  # noqa: E402
from mesh.post_solve_export import (  # noqa: E402
    ExportProgressEvent,
    ExportPreparationError,
    FieldWhiteReconstructionConfig,
    export_solve_bundle,
    normalize_geometry_source,
    write_export_manifest,
)
from pipeline.blueprint_triage.report import PrintabilityError  # noqa: E402
from pipeline.material_exposure import (  # noqa: E402
    MaterialExposureAudit,
    audit_colored_filament_exposure_from_thickness_maps,
)
from pipeline.derived_views import committed_stack_label_map  # noqa: E402
from lib.photo_stack_model.bundle import (  # noqa: E402
    DEPLOYMENT_BUNDLE_SCHEMA,
    load_photo_stack_bundle,
)
from lib.photo_stack_model import predictor as runtime_predictor  # noqa: E402
from lib.model_library_store import ModelLibraryStore, ModelLibraryStoreError  # noqa: E402
from lib.platform_open import open_folder_in_file_manager  # noqa: E402
from pipeline.modules import (  # noqa: E402
    _normalize_module_state,
    load_module_state,
    save_module_state,
    toggle_module,
)
from pipeline.registry import (  # noqa: E402
    PREPROCESSING_MODULE_IDS,
    _ensure_registry_populated,
    list_all_modules,
)
from config.resolution_schema import (  # noqa: E402
    LEGACY_RESOLUTION_REPLACEMENTS,
    normalize_resolution_schema,
    ResolutionSchemaConflictError,
    ResolutionSchemaLegacyFieldError,
)
from swap import (  # noqa: E402
    generate_swap_instructions,
    generate_orcaslicer_pause_gcode,
)
# ---------------------------------------------------------------------------
# Key directories
# ---------------------------------------------------------------------------

# Thin aliases so existing references in this file keep working.
_resolve_data_dir = data_paths.resolve_data_dir
_DATA_DIR = data_paths.DATA_DIR
_PROFILES_DIR = _DATA_DIR / "filaments" / "profiles"
_CORR_PATH = _DATA_DIR / "filaments" / "pair_corrections.json"
_GENERATOR_DATA_DIR = data_paths.GENERATOR_DATA_DIR
_SETTINGS_PROFILES_DIR = _GENERATOR_DATA_DIR / "settings_profiles"
_MODEL_LIBRARY_AVAILABLE = str(os.environ.get("PRISMA_MODEL_LIBRARY_AVAILABLE", "1")).strip() != "0"
_ACTIVE_MODEL_LIBRARY_ID = str(os.environ.get("PRISMA_ACTIVE_MODEL_LIBRARY_ID") or "").strip() or None
_MODEL_LIBRARY_ERROR = str(os.environ.get("PRISMA_MODEL_LIBRARY_ERROR") or "").strip() or None
_MODEL_LIBRARIES_ROOT = Path(
    str(os.environ.get("PRISMA_MODEL_LIBRARIES_ROOT") or (_GENERATOR_DATA_DIR.parent / "Model Libraries"))
).expanduser().resolve()
_MODEL_LIBRARY_STORE = ModelLibraryStore(_MODEL_LIBRARIES_ROOT, _GENERATOR_DATA_DIR, prisma_version="0.1.0")
_MODEL_LIBRARY_OPERATION_LOCK = threading.Lock()
_MODEL_RESOURCE_COORDINATION_LOCK = threading.RLock()
_RESTART_REQUESTED = threading.Event()
_RESTART_CALLBACK: Callable[[], None] | None = None
_IMAGES_DIR = data_paths.UPLOAD_DIR
_OUTPUT_DIR = data_paths.OUTPUT_DIR

data_paths.ensure_dirs()
_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
_PRINTERS_PATH = data_paths.CONFIG_DIR / "printers.json"
# Single source of truth for module toggle state in this worktree.
_MODULES_PATH = data_paths.CONFIG_DIR / "modules.json"

_SETTINGS_PROFILE_SCHEMA_VERSION = 1
_SYSTEM_SETTINGS_PROFILE_ID = "system-default"
_SYSTEM_SETTINGS_PROFILE_NAME = "Basic"
_SETTINGS_PROFILE_STATE_NAME = "state.json"
_SETTINGS_PROFILE_FORBIDDEN_NAME_CHARS = set('<>:"/\\|?*')

_DEFAULT_PRINTERS = {
    "printers": [
        {
            "id": "bambu-x1c",
            "name": "Bambu X1C",
            "max_print_area": {"x": 256, "y": 256},
            "ams_units": 1,
            "slots_per_ams": 4,
            "nozzle_profiles": [
                {
                    "size": 0.2,
                    "min_layer_height": 0.05,
                    "max_layer_height": 0.15,
                    "line_width": 0.22,
                    "min_line_width": 0.16,
                    "max_line_width": 0.25,
                    "min_line_length": 0.40,
                },
                {
                    "size": 0.4,
                    "min_layer_height": 0.08,
                    "max_layer_height": 0.32,
                    "line_width": 0.42,
                    "min_line_width": 0.32,
                    "max_line_width": 0.5,
                    "min_line_length": 0.50,
                },
            ],
        }
    ],
    "active_printer_id": "bambu-x1c",
    "active_nozzle_size": 0.2,
}


def _default_line_widths(nozzle_size: float) -> dict[str, float]:
    size = round(float(nozzle_size), 4)
    if math.isclose(size, 0.2, abs_tol=1e-6):
        return {"line_width": 0.22, "min_line_width": 0.16, "max_line_width": 0.25}
    if math.isclose(size, 0.4, abs_tol=1e-6):
        return {"line_width": 0.42, "min_line_width": 0.32, "max_line_width": 0.5}
    return {
        "line_width": round(size * 1.05, 2),
        "min_line_width": round(size * 0.8, 2),
        "max_line_width": round(size * 1.25, 2),
    }


def _default_printability_lengths(min_line_width: float) -> dict[str, float]:
    minimum = max(0.40, float(min_line_width) + 0.10)
    return {
        "min_line_length": round(minimum, 2),
    }


def _find_nozzle_profiles_with_retired_preferred_length(data: dict) -> list[str]:
    hits: list[str] = []
    for printer_index, printer in enumerate((data or {}).get("printers", []) or []):
        printer_id = printer.get("id") or f"#{printer_index}"
        for nozzle_index, profile in enumerate(printer.get("nozzle_profiles", []) or []):
            if "preferred_line_length" in (profile or {}):
                size = profile.get("size", f"#{nozzle_index}")
                hits.append(f"{printer_id}/nozzle-{size}")
    return hits


def _normalize_nozzle_profile(profile: dict) -> dict:
    normalized = dict(profile)
    normalized.pop("preferred_line_length", None)
    size = float(normalized.get("size") or 0.4)
    defaults = _default_line_widths(size)
    min_line_width = float(normalized.get("min_line_width") or defaults["min_line_width"])
    max_line_width = float(normalized.get("max_line_width") or defaults["max_line_width"])
    if min_line_width > max_line_width:
        min_line_width, max_line_width = max_line_width, min_line_width
    nominal = float(normalized.get("line_width") or defaults["line_width"])
    nominal = min(max(nominal, min_line_width), max_line_width)
    length_defaults = _default_printability_lengths(min_line_width)
    min_line_length = float(
        normalized.get("min_line_length") or length_defaults["min_line_length"]
    )
    normalized["size"] = size
    normalized["line_width"] = nominal
    normalized["min_line_width"] = min_line_width
    normalized["max_line_width"] = max_line_width
    normalized["min_line_length"] = min_line_length
    return normalized


def _normalize_printers_data(data: dict, *, retired_policy: str = "warn_drop") -> dict:
    retired_hits = _find_nozzle_profiles_with_retired_preferred_length(data)
    if retired_hits:
        if retired_policy == "reject":
            raise HTTPException(
                422,
                {
                    "error": "retired_printer_profile_field",
                    "field": "preferred_line_length",
                    "profiles": retired_hits,
                    "message": (
                        "preferred_line_length is retired; use minimum line "
                        "length hard printability settings instead"
                    ),
                },
            )
        if retired_policy == "warn_drop":
            logger.warning(
                "Dropping retired preferred_line_length from printer profile(s): %s",
                ", ".join(retired_hits),
            )
        else:
            raise ValueError(f"Unknown printer retired-field policy {retired_policy!r}")
    normalized = dict(data)
    printers = []
    for printer in data.get("printers", []):
        normalized_printer = dict(printer)
        normalized_printer["nozzle_profiles"] = [
            _normalize_nozzle_profile(profile)
            for profile in printer.get("nozzle_profiles", [])
        ]
        printers.append(normalized_printer)
    normalized["printers"] = printers
    return normalized


# Force-import live preprocessing modules so they register themselves.
_ensure_registry_populated()


def _load_printers() -> dict:
    """Load printers.json, creating with defaults if missing."""
    if _PRINTERS_PATH.exists():
        with open(_PRINTERS_PATH, encoding="utf-8") as f:
            return _normalize_printers_data(json.load(f))
    _save_printers(_DEFAULT_PRINTERS)
    return _normalize_printers_data(_DEFAULT_PRINTERS)


def _save_printers(data: dict) -> None:
    """Write printers.json atomically."""
    data = _normalize_printers_data(data)
    _PRINTERS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _PRINTERS_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    tmp.replace(_PRINTERS_PATH)


def _resolve_active_printer(data: dict) -> dict:
    """Return {printer: {...}, nozzle: {...}} for the active selection."""
    data = _normalize_printers_data(data)
    printers = data.get("printers", [])
    active_id = data.get("active_printer_id")
    printer = next((p for p in printers if p["id"] == active_id), printers[0] if printers else None)
    if not printer:
        return {"printer": None, "nozzle": None}
    active_nozzle_size = data.get("active_nozzle_size")
    nozzle = next(
        (n for n in printer.get("nozzle_profiles", []) if n["size"] == active_nozzle_size),
        printer["nozzle_profiles"][0] if printer.get("nozzle_profiles") else None,
    )
    return {"printer": printer, "nozzle": nozzle}


def _load_profile_sandbox(filament_id: str) -> dict:
    """Load a profile from the active immutable published library."""
    return _load_profile(filament_id)


def _runtime_profiles_dir() -> Path:
    return _PROFILES_DIR


def _runtime_profile_exists(filament_id: str) -> bool:
    return (_PROFILES_DIR / f"{filament_id}.json").is_file()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger("prisma.server")
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(name)s  %(levelname)s  %(message)s")

_log_file = data_paths.LOG_DIR / "generator.log"
_log_file.parent.mkdir(parents=True, exist_ok=True)
_file_handler = logging.FileHandler(str(_log_file), encoding="utf-8")
_file_handler.setFormatter(logging.Formatter("%(asctime)s  %(name)s  %(levelname)s  %(message)s"))
logging.getLogger("prisma").addHandler(_file_handler)

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Prisma — Lithophane Generator API",
    version="0.1.0",
    description="Backend for the unified lithophane generator web UI.",
)

# CORS — wide open during development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/system/health")
def system_health() -> dict:
    """Lightweight readiness identity for the local desktop launcher."""
    return {
        "ok": True,
        "app": "prisma-generator",
        "version": app.version,
        "mode": "normal" if _MODEL_LIBRARY_AVAILABLE else "library_recovery",
        "model_library_available": _MODEL_LIBRARY_AVAILABLE,
        "active_library_id": _ACTIVE_MODEL_LIBRARY_ID,
        "model_library_error": _MODEL_LIBRARY_ERROR,
    }


def _require_model_library() -> None:
    if _MODEL_LIBRARY_OPERATION_LOCK.locked():
        raise HTTPException(status_code=409, detail="A model-library operation is currently running")
    if not _MODEL_LIBRARY_AVAILABLE:
        raise HTTPException(
            status_code=409,
            detail=_MODEL_LIBRARY_ERROR or "No valid model library is active. Manage Model Libraries to continue.",
        )

# ---------------------------------------------------------------------------
# Session state (module-level, single-user)
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG: Dict[str, Any] = {
    "image_path": None,
    "palette": [],
    "base_filament": "bambu-tough-white",
    "cap_filament": "__same__",
    "white_base": "bambu-tough-white",
    "white_cap": None,
    "layer_height": 0.08,
    # Canonical solve-resolution fields only. Legacy aliases are rejected on
    # ingress and are not stored in session state.
    "image_sample_pitch_mm": 0.20,
    "solver_fine_pitch_mm": 0.20,
    "detail_cap_pitch_mm": 0.20,
    "detail_cap_enabled": True,
    "detail_cap_max_layers": 5,
    "detail_cap_smoothing_enabled": True,
    "detail_cap_smoothing_exact_speckle_max_px": 1,
    "detail_cap_smoothing_cumulative_component_max_px": 2,
    "detail_cap_smoothing_cumulative_hole_max_px": 2,
    "color_region_target_mm": 0.60,
    "stage1_coarsening_factor": 1,
    "emit_pressure_diagnostics": False,
    "emit_geometry_attribution": False,
    "emit_blueprint_printability": True,
    "printability_minimum_extrusion_width_mm": None,
    "printability_minimum_line_length_mm": None,
    "enforce_printability": True,
    "color_region_target_from_printability": True,
    "color_region_target_width_multiplier": 2.0,
    "stage2_fine_override_enabled": True,
    "stage2_final_printability_gate_fine_override": True,
    "stage2_printability_gate_fine_override": True,
    "stage2_printability_repair_fine_override": True,
    "stage2_boundary_mutation_enabled": True,
    "stage2_boundary_mutation_min_gain": None,
    "stage2_boundary_mutation_min_component_mm": None,
    "stage2_boundary_mutation_current_de_percentile": None,
    "stage2_boundary_mutation_max_passes": 1,
    "stage4_printability_gate_detail": True,
    "luminance_detail_authoring_printability": "off",
    "d_wb": 0.20,
    "d_wc_min": 0.08,
    "t_max": 3.0,
    "k_max": 3,
    "de_threshold": 0.01,
    "smooth_kernel": 5.0,
    "smooth_iters": 3,
    "allow_print_despite_hazards": False,
    "border": False,
    "border_width_mm": 3.0,
    "border_height_mm": 3.0,
    "use_corrections": True,
    "appearance_model_provider": "photo_stack_bundle",
    "photo_stack_bundle_path": None,
    "max_dim_mm": 130.0,
    "frame": None,
    "image_adjust": None,
    "ams_slots": 4,
    "n_ams_units": 1,
    "white_slots": 1,
    "swap_improvement_threshold": 2.0,
    "force_all_tiers": False,
    "gamut_mode": "hull",
    "gamut_white_rescale": False,
    "model_domain_ingress": True,
    "model_domain_ingress_lut_path": str(_DATA_DIR / "camera_transform"),
    "model_domain_display_transform_path": str(_DATA_DIR / "camera_transform"),
    "chroma_weight": 1.0,
    "luminance_mode": "standard",
    "luminance_handler_enabled": False,
    "luminance_handler_mode": "boundary_prior",
    "luminance_handler_strength": 1.0,
    "luminance_handler_optical_authority_fraction": 0.75,
    "luminance_base_shading_limit_fraction": 0.75,
    "luminance_handler_boundary_percentile": 95.0,
    "luminance_handler_boundary_sigma_px": None,
    "luminance_handler_response_curve": "linear",
    "luminance_handler_response_gamma": 1.0,
    "luminance_handler_detail_residual": True,
    "luminance_handler_include_solver_detail": True,
    # Print-aware source resample kernel (Wing B §E / B7). JSON-only in v1
    # per consensus §G.7. "lanczos" is bit-exact with pre-B7 behavior.
    "source_resample_kernel": "lanczos",
    # Per-operator preprocessing params keyed by preprocessing module id.
    "preprocessing_params": {},
    "cap_mode": "appearance_bounded_smooth",
    "boundary_cap_de_budget": 0.008,
    "cap_continuity_cleanup": True,
    "cell_mode": "felzenszwalb",
    "smooth_boundaries": False,
    "boundary_smooth_radius": 1,
    "v2_cleanup_de_budget": 0.10,
    "v2_enable_cliff_closure": True,
    "v2_enable_cap_topology_cleanup": False,
    "v2_max_cleanup_rounds": 1,
    "v2_full_cap_quality_report": False,
}

session: Dict[str, Any] = {
    "config": deepcopy(_DEFAULT_CONFIG),
    "solve": {
        "status": "idle",       # idle | running | complete | error | cancelled
        "progress": {},
        "elapsed_s": 0.0,
        "started_monotonic": None,
        "job_id": None,
        "card_id": None,
        "result": None,         # JSON-safe result dict (populated after solve)
        "thickness_maps": None, # numpy arrays, NOT serialized
        "predicted": None,      # (H,W,3) uint8 — predicted image
        "img": None,            # (H,W,3) uint8 — source at solve resolution
        "color_profiles": None,
        "wb_profile": None,
        "wc_profile": None,
        "luts": None,
        "grouping": None,
        "image_domain_width_mm": None,   # solve-owned physical image width
        "image_domain_height_mm": None,  # solve-owned physical image height
        "solved_plan": None,             # SolvedMaterialPlan (populated by the solve path)
        "blueprint_triage": None,
        "export_maps": None,             # product/export contract arrays
        "export_metadata": None,         # product/export contract metadata
        "solve_owned_fingerprint": None, # hash of solve-owned config at solve time
        "cancel_requested": False,
    },
    "solve_cache": {},        # card_id -> {"solve": ..., "config": ...}
    "compare": {
        "status": "idle",       # idle | running | complete | error | cancelled
        "progress": {},
        "elapsed_s": 0.0,
        "result": None,
        "cancel_requested": False,
        "job_id": None,
    },
    "suggest": {
        "status": "idle",       # idle | running | complete | error | cancelled
        "progress": {},
        "elapsed_s": 0.0,
        "result": None,
        "cancel_requested": False,
        "job_id": None,
    },
    "export": {
        "status": "idle",       # idle | running | cancelling | complete | error | cancelled
        "progress": {},
        "elapsed_s": 0.0,
        "result": None,
        "cancel_requested": False,
        "job_id": None,
    },
}

_solve_lock = threading.Lock()
_compare_lock = threading.Lock()
_suggest_lock = threading.Lock()
_ACTIVE_MODEL_JOB_STATUSES = {"running", "cancelling"}
_PALETTE_BACKEND_CACHE_MAX_SIZE = 2
_PALETTE_BACKEND_CACHE_LOCK = threading.RLock()
_PALETTE_BACKEND_CACHE: OrderedDict[tuple[Any, ...], object] = OrderedDict()


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class ModelLibraryIdPayload(BaseModel):
    library_id: str


class ConfigPayload(BaseModel):
    """All configuration knobs that the frontend can set.

    Resolution fields: the canonical pitch fields are image_sample_pitch_mm,
    solver_fine_pitch_mm, detail_cap_pitch_mm, detail_cap_enabled,
    detail_cap_max_layers, color_region_target_mm.
    Legacy aliases such as pixel_size_mm and color_pixel_mm are rejected on
    ingress without being carried as live model fields.
    """
    model_config = ConfigDict(extra="allow")

    image_path: Optional[str] = None
    palette: List[str] = Field(default_factory=list)
    base_filament: str = "bambu-tough-white"
    cap_filament: str = "__same__"
    white_base: str = "bambu-tough-white"
    white_cap: Optional[str] = None
    layer_height: float = 0.08
    d_wb: float = 0.20
    d_wc_min: float = 0.08
    t_max: float = 3.0
    k_max: int = 3
    de_threshold: float = 0.01
    smooth_kernel: float = 5.0
    smooth_iters: int = 3
    allow_print_despite_hazards: bool = False
    image_sample_pitch_mm: Optional[float] = None
    solver_fine_pitch_mm: Optional[float] = None
    color_region_target_mm: Optional[float] = None
    detail_cap_pitch_mm: Optional[float] = None
    detail_cap_enabled: bool = True
    detail_cap_max_layers: Optional[int] = 5
    detail_cap_smoothing_enabled: bool = True
    detail_cap_smoothing_exact_speckle_max_px: int = 1
    detail_cap_smoothing_cumulative_component_max_px: int = 2
    detail_cap_smoothing_cumulative_hole_max_px: int = 2
    stage1_coarsening_factor: int = 1
    emit_pressure_diagnostics: bool = False
    emit_geometry_attribution: bool = False
    emit_blueprint_printability: bool = True
    printability_minimum_extrusion_width_mm: Optional[float] = None
    printability_minimum_line_length_mm: Optional[float] = None
    enforce_printability: bool = True
    color_region_target_from_printability: bool = True
    color_region_target_width_multiplier: float = 2.0
    stage2_fine_override_enabled: bool = True
    stage2_final_printability_gate_fine_override: bool = True
    stage2_printability_gate_fine_override: bool = True
    stage2_printability_repair_fine_override: bool = True
    stage2_boundary_mutation_enabled: bool = True
    stage2_boundary_mutation_min_gain: Optional[float] = None
    stage2_boundary_mutation_min_component_mm: Optional[float] = None
    stage2_boundary_mutation_current_de_percentile: Optional[float] = None
    stage2_boundary_mutation_max_passes: Optional[int] = 1
    stage4_printability_gate_detail: bool = True
    luminance_detail_authoring_printability: str = "off"
    border: bool = False
    border_width_mm: float = 3.0
    border_height_mm: float = 3.0
    use_corrections: bool = True
    appearance_model_provider: str = "photo_stack_bundle"
    photo_stack_bundle_path: Optional[str] = None
    max_dim_mm: float = 130.0
    frame: Optional[Dict[str, Any]] = None
    image_adjust: Optional[Dict[str, Any]] = None
    ams_slots: int = 4
    n_ams_units: int = 1
    white_slots: int = 1
    swap_improvement_threshold: float = 2.0
    force_all_tiers: bool = False
    gamut_mode: str = "hull"
    gamut_white_rescale: bool = False
    model_domain_ingress: bool = True
    model_domain_ingress_lut_path: str = str(_DATA_DIR / "camera_transform")
    model_domain_display_transform_path: str = str(_DATA_DIR / "camera_transform")
    chroma_weight: float = 1.0
    luminance_mode: str = "standard"
    luminance_handler_enabled: bool = False
    luminance_handler_mode: str = "boundary_prior"
    luminance_handler_strength: float = 1.0
    luminance_handler_optical_authority_fraction: Optional[float] = 0.75
    luminance_base_shading_limit_fraction: Optional[float] = None
    luminance_handler_boundary_percentile: float = 95.0
    luminance_handler_boundary_sigma_px: Optional[float] = None
    luminance_handler_response_curve: str = "linear"
    luminance_handler_response_gamma: float = 1.0
    luminance_handler_detail_residual: bool = True
    luminance_handler_include_solver_detail: bool = True
    source_resample_kernel: str = "lanczos"
    preprocessing_params: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    cap_mode: str = "appearance_bounded_smooth"
    boundary_cap_de_budget: float = 0.008
    cap_continuity_cleanup: bool = True
    cell_mode: str = "felzenszwalb"
    smooth_boundaries: bool = False
    boundary_smooth_radius: int = 1
    v2_cleanup_de_budget: float = 0.10
    v2_enable_cliff_closure: bool = True
    v2_enable_cap_topology_cleanup: bool = False
    v2_max_cleanup_rounds: int = 1
    v2_full_cap_quality_report: bool = False
    @field_validator("source_resample_kernel", mode="before")
    @classmethod
    def _normalize_source_resample_kernel(cls, value: Any) -> str:
        """Canonicalise Wing B §E / B7 kernel string.

        Accepts any case / surrounding whitespace at ingress (per consensus
        §R6.C) and stores the lowercase canonical token. Rejects anything
        other than `{"lanczos", "area"}`.
        """
        if not isinstance(value, str):
            raise ValueError(
                f"source_resample_kernel must be a string, got {type(value).__name__}"
            )
        canonical = value.strip().lower()
        if canonical not in {"lanczos", "area"}:
            raise ValueError(
                f"Unsupported source_resample_kernel: {value!r} "
                f"(valid: 'lanczos', 'area')"
            )
        return canonical

    @field_validator("gamut_mode", mode="before")
    @classmethod
    def _normalize_gamut_mode(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError(f"gamut_mode must be a string, got {type(value).__name__}")
        canonical = value.strip().lower()
        if canonical == "chroma":
            return "hue_preserving"
        if canonical not in {"hull", "hue_preserving"}:
            raise ValueError(
                f"Unsupported gamut_mode: {value!r} (valid: 'hull', 'hue_preserving')"
            )
        return canonical

    @field_validator("luminance_mode", mode="before")
    @classmethod
    def _normalize_luminance_mode_field(cls, value: Any) -> str:
        return _normalize_luminance_mode(value)

    @field_validator("cap_mode", mode="before")
    @classmethod
    def _normalize_cap_mode_field(cls, value: Any) -> str:
        return _normalize_cap_mode(value)

    @field_validator("detail_cap_enabled")
    @classmethod
    def _validate_detail_cap_enabled(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("detail_cap_enabled is mandatory and can no longer be disabled")
        return True

    @field_validator("preprocessing_params", mode="before")
    @classmethod
    def _validate_preprocessing_params(cls, value: Any) -> Dict[str, Dict[str, Any]]:
        return _normalize_preprocessing_params(value)


_QUIET_DROPPED_CONFIG_EXTRAS = frozenset({
    "stage2_boundary_mutation_segment_mode",
    "stage2_boundary_mutation_edge_run_mode",
    "preview_resolution",
})


class PaletteValidatePayload(BaseModel):
    palette: List[str]


class ExportFilesPayload(BaseModel):
    geometry_source: str = "field_derived"
    field_scale: int = 4
    output_format: str = "3mf"
    validate_written_meshes: bool = False
    card_id: Optional[str] = None

    @field_validator("geometry_source")
    @classmethod
    def _normalize_geometry_source(cls, value: str) -> str:
        return normalize_geometry_source(value).value

    @field_validator("field_scale")
    @classmethod
    def _validate_field_scale(cls, value: int) -> int:
        if int(value) not in {2, 4, 8, 16}:
            raise ValueError("field_scale must be one of 2, 4, 8, or 16")
        return int(value)

    @field_validator("output_format")
    @classmethod
    def _validate_output_format(cls, value: str) -> str:
        normalized = str(value or "").strip().lower()
        if normalized not in {"stls", "3mf"}:
            raise ValueError("output_format must be 'stls' or '3mf'")
        return normalized


class ExportFolderPayload(BaseModel):
    export_id: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _white_base(cfg: dict = None) -> str:
    """Resolve white base filament ID from config."""
    if cfg is None:
        cfg = _cfg()
    return cfg.get("white_base") or cfg.get("white_filament", "bambu-tough-white")


def _white_cap(cfg: dict = None) -> str:
    """Resolve white cap filament ID from config (defaults to white base)."""
    if cfg is None:
        cfg = _cfg()
    cap = cfg.get("white_cap")
    return cap if cap else _white_base(cfg)


def _white_ids(cfg: dict = None) -> list:
    """Return deduplicated list of white filament IDs."""
    wb, wc = _white_base(cfg), _white_cap(cfg)
    return [wb] if wb == wc else [wb, wc]


def _load_source_image_for_export(cfg: dict) -> Optional[np.ndarray]:
    """Reload the configured source image for authored export paths."""
    image_rel = cfg.get("image_path")
    if not image_rel:
        return None

    image_path = _IMAGES_DIR / image_rel
    if not image_path.exists():
        logger.warning("Export source image not found: %s", image_path)
        return None

    try:
        return _load_run_source_image(image_path, cfg)
    except Exception:
        logger.exception("Failed to reload source image for export")
        return None


def _load_run_source_image(image_path: Path, cfg: dict, *, max_dim_mm: Optional[float] = None) -> np.ndarray:
    """Load the framed, adjusted source raster for solve-adjacent paths."""

    img = load_image(
        image_path,
        pixel_size_mm=cfg["image_sample_pitch_mm"],
        max_dim_mm=cfg["max_dim_mm"] if max_dim_mm is None else max_dim_mm,
        frame=cfg.get("frame"),
        source_resample_kernel=cfg.get("source_resample_kernel", "lanczos"),
    )
    return apply_adjustments(img, cfg.get("image_adjust"))


def _prepare_export_materialization(
    cfg: dict,
    thickness_maps: Dict[str, np.ndarray],
) -> tuple[Dict[str, np.ndarray], List[str]]:
    """Prepare export material maps for the product path."""
    cfg = _force_mandatory_product_settings(cfg)
    return prepare_export_material_maps(
        thickness_maps,
        list(cfg["palette"]),
    )


def _force_mandatory_product_settings(cfg: dict) -> dict:
    """Return cfg with non-optional product safety settings enabled."""
    resolved = dict(cfg)
    for key in _QUIET_DROPPED_CONFIG_EXTRAS:
        resolved.pop(key, None)
    layer_height = max(float(resolved.get("layer_height", 0.08) or 0.08), 1e-9)
    min_cap = max(float(resolved.get("d_wc_min", layer_height) or layer_height), layer_height)
    min_cap_layers = max(1, int(math.ceil(min_cap / layer_height - 1e-9)))
    resolved["d_wc_min"] = round(min_cap_layers * layer_height, 6)
    resolved["model_domain_ingress"] = True
    resolved["enforce_printability"] = True
    resolved["cap_continuity_cleanup"] = True
    resolved["color_region_target_from_printability"] = True
    resolved["detail_cap_enabled"] = True
    resolved["stage2_final_printability_gate_fine_override"] = True
    resolved["stage2_printability_gate_fine_override"] = True
    resolved["stage2_printability_repair_fine_override"] = True
    resolved["stage4_printability_gate_detail"] = True
    return resolved


def _load_registry() -> dict:
    """Load the catalog from the active immutable published library."""
    if not _MODEL_LIBRARY_AVAILABLE:
        return {}
    return load_filament_order_registry()


def _load_corrections() -> Optional[dict]:
    """Load filaments/pair_corrections.json if present."""
    if not _MODEL_LIBRARY_AVAILABLE:
        return None
    if _CORR_PATH.exists():
        with open(_CORR_PATH, encoding="utf-8") as f:
            return json.load(f)
    return None


def _normalize_luminance_mode(value: Any) -> str:
    canonical = str(value or "standard").strip().lower()
    aliases = {
        "standard": "standard",
        "off": "standard",
        "default": "standard",
        "color": "standard",
        "luminance": "luminance_detail",
        "luminance_detail": "luminance_detail",
        "luminance-detail": "luminance_detail",
        "detail": "luminance_detail",
    }
    if canonical not in aliases:
        raise ValueError(
            "Unsupported luminance_mode: "
            f"{value!r} (valid: 'standard', 'luminance_detail')"
        )
    return aliases[canonical]


_SUPPORTED_CONFIG_CAP_MODES = frozenset({
    "smooth_variable",
    "appearance_bounded_smooth",
})


def _normalize_cap_mode(value: Any) -> str:
    if value is None or value == "":
        return "appearance_bounded_smooth"
    if not isinstance(value, str):
        raise ValueError(f"cap_mode must be a string, got {type(value).__name__}")
    canonical = value.strip().lower()
    if canonical == "fixed":
        raise ValueError(
            "cap_mode='fixed' has been retired; use Smooth or "
            "Detail Aware"
        )
    if canonical not in _SUPPORTED_CONFIG_CAP_MODES:
        raise ValueError(
            f"Unsupported cap_mode: {value!r} "
            f"(valid: {sorted(_SUPPORTED_CONFIG_CAP_MODES)!r})"
        )
    return canonical


def _apply_luminance_mode_preset(
    cfg: dict,
    *,
    reset_standard: bool = False,
) -> dict:
    """Expand the high-level luminance mode into conservative backend flags."""

    resolved = dict(cfg)
    mode = _normalize_luminance_mode(resolved.get("luminance_mode", "standard"))
    resolved["luminance_mode"] = mode
    if mode == "luminance_detail":
        resolved.update(
            {
                "cap_mode": "smooth_variable",
                "luminance_handler_enabled": True,
                "luminance_handler_mode": "boundary_ceiling",
                "luminance_handler_strength": 1.0,
                "luminance_handler_boundary_percentile": 95.0,
                "luminance_handler_boundary_sigma_px": None,
                "luminance_handler_response_curve": "linear",
                "luminance_handler_response_gamma": 1.0,
                "luminance_handler_detail_residual": True,
                "luminance_handler_include_solver_detail": True,
                "detail_cap_enabled": True,
                "luminance_detail_authoring_printability": (
                    "absolute_finalgate"
                ),
                "enforce_printability": True,
                "emit_blueprint_printability": True,
            }
        )
        if resolved.get("luminance_handler_optical_authority_fraction") is None:
            resolved["luminance_handler_optical_authority_fraction"] = 0.75
        resolved["luminance_base_shading_limit_fraction"] = resolved[
            "luminance_handler_optical_authority_fraction"
        ]
    elif reset_standard:
        resolved["luminance_handler_enabled"] = False
        resolved["luminance_detail_authoring_printability"] = "off"
    return _force_mandatory_product_settings(resolved)


def _is_photo_stack_provider(provider: object) -> bool:
    return str(provider or "").strip().lower() == "photo_stack_bundle"


def _resolve_photo_stack_candidate_path_for_solve(cfg: dict) -> Path | None:
    _require_model_library()
    provider = str(
        cfg.get("appearance_model_provider", _DEFAULT_CONFIG["appearance_model_provider"]) or ""
    ).strip().lower()
    if not _is_photo_stack_provider(provider):
        return None
    try:
        pointer_path = _DATA_DIR / "filaments" / "photo_stack_models" / "latest.json"
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        run_id = str(pointer.get("run_id") or pointer.get("path") or "").strip()
        if not run_id or Path(run_id).name != run_id or any(char in run_id for char in ("/", "\\")):
            raise RuntimeError("active library has an unsafe Photo Stack pointer")
        bundle_path = pointer_path.parent / run_id / "runtime_bundle.json"
        bundle = load_photo_stack_bundle(bundle_path)
        if bundle.payload.get("schema") != DEPLOYMENT_BUNDLE_SCHEMA:
            raise RuntimeError("active library does not contain a published Photo Stack deployment bundle")
        configured_path = cfg.get("photo_stack_bundle_path")
        if configured_path and Path(str(configured_path)).resolve() != bundle_path.resolve():
            raise RuntimeError(
                "saved Generator settings reference a Photo Stack model outside the active published library"
            )
        return bundle_path.resolve()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _build_solve_config(cfg: dict, palette_override: List[str] | None = None) -> SolveConfig:
    """Translate session config dict to a SolveConfig dataclass.

    Resolution fields are passed via canonical names only. Live session
    config is canonical-only; legacy aliases are rejected at ingress.
    """
    _require_model_library()
    cfg = _apply_luminance_mode_preset(cfg, reset_standard=True)
    cfg = _force_mandatory_product_settings(cfg)
    # Derive nozzle diameter from active printer config
    active = get_active_printer()
    nozzle_size = 0.20  # fallback
    printer_min_line_width = None
    printer_min_line_length = None
    if active.get("nozzle") and active["nozzle"].get("size"):
        nozzle = active["nozzle"]
        nozzle_size = nozzle["size"]
        printer_min_line_width = nozzle.get("min_line_width")
        printer_min_line_length = nozzle.get("min_line_length")
    # Product printability is mandatory; older sessions/profiles cannot
    # silently disable or partially reconfigure enforcement.
    # Blueprint printability diagnostics stay on for normal solves.  Heavier
    # pressure/geometry attribution artifacts are CLI/API-only research output.
    _enforce = True
    photo_stack_candidate_path = _resolve_photo_stack_candidate_path_for_solve(cfg)
    photo_stack_provider = _is_photo_stack_provider(cfg.get("appearance_model_provider"))

    registry = load_filament_order_registry()
    palette = canonical_palette_order(
        palette_override if palette_override is not None else cfg["palette"],
        registry,
    )

    return SolveConfig(
        palette=palette,
        white_base=_white_base(cfg),
        white_cap=cfg.get("white_cap"),
        layer_height=cfg["layer_height"],
        # Canonical solve-resolution fields only.
        image_sample_pitch_mm=cfg.get("image_sample_pitch_mm"),
        solver_fine_pitch_mm=cfg.get("solver_fine_pitch_mm"),
        detail_cap_pitch_mm=cfg.get("detail_cap_pitch_mm"),
        detail_cap_enabled=True,
        detail_cap_max_layers=(
            2
            if cfg.get("detail_cap_max_layers") is None
            else max(0, int(cfg.get("detail_cap_max_layers")))
        ),
        color_region_target_mm=cfg.get("color_region_target_mm"),
        stage1_coarsening_factor=cfg.get(
            "stage1_coarsening_factor", 1
        ),
        emit_pressure_diagnostics=bool(
            cfg.get("emit_pressure_diagnostics", False)
        ),
        emit_geometry_attribution=bool(
            cfg.get("emit_geometry_attribution", False)
        ),
        emit_blueprint_printability=True,
        printability_minimum_extrusion_width_mm=printer_min_line_width,
        printability_minimum_line_length_mm=printer_min_line_length,
        enforce_printability=_enforce,
        color_region_target_from_printability=_enforce,
        color_region_target_width_multiplier=cfg.get(
            "color_region_target_width_multiplier", 2.0
        ),
        stage2_fine_override_enabled=cfg.get(
            "stage2_fine_override_enabled", True
        ),
        stage2_final_printability_gate_fine_override=_enforce,
        stage2_printability_gate_fine_override=_enforce,
        stage2_printability_repair_fine_override=_enforce,
        stage2_boundary_mutation_enabled=cfg.get(
            "stage2_boundary_mutation_enabled", True
        ),
        stage2_boundary_mutation_min_gain=cfg.get(
            "stage2_boundary_mutation_min_gain"
        ),
        stage2_boundary_mutation_min_component_mm=cfg.get(
            "stage2_boundary_mutation_min_component_mm"
        ),
        stage2_boundary_mutation_current_de_percentile=cfg.get(
            "stage2_boundary_mutation_current_de_percentile"
        ),
        stage2_boundary_mutation_max_passes=cfg.get(
            "stage2_boundary_mutation_max_passes", 1
        ),
        stage4_printability_gate_detail=_enforce,
        luminance_detail_authoring_printability=cfg.get(
            "luminance_detail_authoring_printability",
            "off",
        ),
        detail_cap_smoothing_enabled=bool(
            cfg.get("detail_cap_smoothing_enabled", True)
        ),
        detail_cap_smoothing_exact_speckle_max_px=int(
            cfg.get("detail_cap_smoothing_exact_speckle_max_px", 1) or 0
        ),
        detail_cap_smoothing_cumulative_component_max_px=int(
            cfg.get("detail_cap_smoothing_cumulative_component_max_px", 2) or 0
        ),
        detail_cap_smoothing_cumulative_hole_max_px=int(
            cfg.get("detail_cap_smoothing_cumulative_hole_max_px", 2) or 0
        ),
        d_wb=cfg["d_wb"],
        d_wc_min=cfg["d_wc_min"],
        t_max=cfg["t_max"],
        k_max=cfg["k_max"],
        de_threshold=cfg["de_threshold"],
        smooth_kernel=cfg["smooth_kernel"],
        smooth_iters=cfg["smooth_iters"],
        ams_slots=cfg.get("ams_slots", 4),
        white_slots=cfg.get("white_slots", 1),
        use_corrections=cfg["use_corrections"],
        corrections=(
            _load_corrections()
            if cfg["use_corrections"] and not photo_stack_provider
            else None
        ),
        profiles_dir=_runtime_profiles_dir(),
        appearance_model_provider=cfg.get(
            "appearance_model_provider",
            _DEFAULT_CONFIG["appearance_model_provider"],
        ),
        photo_stack_bundle_path=photo_stack_candidate_path,
        nozzle_diameter=nozzle_size,
        printer_min_line_width_mm=printer_min_line_width,
        gamut_mode=cfg.get("gamut_mode", "hull"),
        gamut_white_rescale=bool(cfg.get("gamut_white_rescale", False)),
        model_domain_ingress=True,
        # Model assets are owned by the active published library.  Never accept
        # a stale saved/client path into Calibration or an earlier installation.
        model_domain_ingress_lut_path=str(_DATA_DIR / "camera_transform"),
        chroma_weight=cfg.get("chroma_weight", 1.0),
        luminance_handler_enabled=cfg.get(
            "luminance_handler_enabled",
            False,
        ),
        luminance_handler_mode=cfg.get(
            "luminance_handler_mode",
            "boundary_prior",
        ),
        luminance_handler_strength=cfg.get(
            "luminance_handler_strength",
            1.0,
        ),
        luminance_handler_optical_authority_fraction=cfg.get(
            "luminance_handler_optical_authority_fraction",
            0.75,
        ),
        luminance_handler_boundary_percentile=cfg.get(
            "luminance_handler_boundary_percentile",
            95.0,
        ),
        luminance_handler_boundary_sigma_px=cfg.get(
            "luminance_handler_boundary_sigma_px"
        ),
        luminance_handler_response_curve=cfg.get(
            "luminance_handler_response_curve",
            "linear",
        ),
        luminance_handler_response_gamma=cfg.get(
            "luminance_handler_response_gamma",
            1.0,
        ),
        luminance_handler_detail_residual=cfg.get(
            "luminance_handler_detail_residual",
            True,
        ),
        luminance_handler_include_solver_detail=cfg.get(
            "luminance_handler_include_solver_detail",
            True,
        ),
        allow_print_despite_hazards=cfg.get("allow_print_despite_hazards", False),
        source_resample_kernel=cfg.get("source_resample_kernel", "lanczos"),
        preprocessing_params=deepcopy(cfg.get("preprocessing_params", {})),
        cap_mode=cfg.get("cap_mode", "appearance_bounded_smooth"),
        boundary_cap_de_budget=cfg.get("boundary_cap_de_budget", 0.008),
        cap_continuity_cleanup=True,
        cell_mode=cfg.get("cell_mode", "felzenszwalb"),
        smooth_boundaries=cfg.get("smooth_boundaries", False),
        boundary_smooth_radius=cfg.get("boundary_smooth_radius", 1),
        v2_cleanup_de_budget=cfg.get("v2_cleanup_de_budget", 0.10),
        v2_enable_cliff_closure=cfg.get("v2_enable_cliff_closure", True),
        v2_enable_cap_topology_cleanup=cfg.get("v2_enable_cap_topology_cleanup", False),
        v2_max_cleanup_rounds=cfg.get("v2_max_cleanup_rounds", 1),
        v2_full_cap_quality_report=cfg.get("v2_full_cap_quality_report", False),
    )


def _image_info(path: Path) -> dict:
    """Return metadata for a single image file."""
    try:
        with Image.open(path) as im:
            im_oriented = ImageOps.exif_transpose(im)
            w, h = im_oriented.size
    except Exception:
        w, h = 0, 0
    size_kb = path.stat().st_size / 1024
    return {
        "filename": path.name,
        "width": w,
        "height": h,
        "size_kb": round(size_kb, 1),
        "thumbnail_url": f"/api/images/preview/{path.name}",
    }


def _save_de_map(de_map: np.ndarray, path: Path) -> None:
    """Save a false-color dE map: green (0) -> yellow -> red (>=0.15)."""
    de_clamp = np.clip(de_map / 0.35, 0, 1)
    r = (np.clip(de_clamp * 2,     0, 1) * 255).astype(np.uint8)
    g = (np.clip(2 - de_clamp * 2, 0, 1) * 255).astype(np.uint8)
    b = np.zeros_like(r)
    rgb = np.stack([r, g, b], axis=-1)
    Image.fromarray(rgb).save(str(path))


def _save_cap_height_map(
    wc_map: np.ndarray,
    path: Path,
    max_mm: float = 3.0,
    zero_rgb: tuple[int, int, int] | None = None,
    zero_mask: np.ndarray | None = None,
) -> None:
    """Save a viridis-colored cap height map."""
    t = np.clip(wc_map / max_mm, 0.0, 1.0)
    r = np.clip(( 0.267 + 2.173*t - 1.802*t**2) * 255, 0, 255).astype(np.uint8)
    g = np.clip((-0.004 + 1.874*t - 0.870*t**2) * 255, 0, 255).astype(np.uint8)
    b = np.clip(( 0.329 - 1.120*t + 0.791*t**2) * 255, 0, 255).astype(np.uint8)
    rgb = np.stack([r, g, b], axis=-1)
    if zero_rgb is not None:
        mask = (
            np.asarray(zero_mask, dtype=bool)
            if zero_mask is not None
            else np.asarray(wc_map) <= 1e-9
        )
        rgb[mask] = np.asarray(zero_rgb, dtype=np.uint8)
    Image.fromarray(rgb).save(str(path))


def _save_overlay_map(overlay: np.ndarray, path: Path) -> None:
    """Save a risk overlay as black→yellow→red intensity."""
    arr = np.asarray(overlay, dtype=np.float32)
    if arr.size:
        finite = np.isfinite(arr)
        if finite.any():
            finite_max = float(arr[finite].max())
            arr = np.where(finite, arr, finite_max)
        else:
            arr = np.zeros_like(arr, dtype=np.float32)
    arr = np.maximum(arr, 0.0)
    peak = float(arr.max()) if arr.size else 0.0
    if peak <= 1e-9:
        rgb = np.zeros(arr.shape + (3,), dtype=np.uint8)
    else:
        t = np.clip(arr / peak, 0.0, 1.0)
        r = (255.0 * t).astype(np.uint8)
        g = (220.0 * np.sqrt(t)).astype(np.uint8)
        b = np.zeros_like(r)
        rgb = np.stack([r, g, b], axis=-1)
    Image.fromarray(rgb).save(str(path))


def _compute_color_ceiling(
    thickness_maps: dict, d_wb: float
) -> np.ndarray:
    """Compute color ceiling: d_wb + sum of all color filament thicknesses.

    Excludes dunder keys (__white_cap__, __de__, __gamut_mask__).
    Returns (H, W) float32.
    """
    color_fids = [k for k in thickness_maps if not k.startswith("__")]
    if not color_fids:
        h, w = next(iter(thickness_maps.values())).shape
        return np.full((h, w), d_wb, dtype=np.float32)
    stack = np.stack([thickness_maps[fid] for fid in color_fids], axis=0)
    return (d_wb + stack.sum(axis=0)).astype(np.float32)


def _thickness_map_volume_mm3(
    thickness_map: np.ndarray,
    *,
    image_domain_width_mm: float | None,
    image_domain_height_mm: float | None,
) -> float | None:
    """Integrate a solve-grid thickness field into physical material volume."""
    field = np.asarray(thickness_map, dtype=np.float64)
    if field.ndim != 2 or field.shape[0] == 0 or field.shape[1] == 0:
        return None
    if image_domain_width_mm is None or image_domain_height_mm is None:
        return None
    width_mm = float(image_domain_width_mm)
    height_mm = float(image_domain_height_mm)
    if width_mm <= 0.0 or height_mm <= 0.0:
        return None
    pixel_area_mm2 = (width_mm / field.shape[1]) * (height_mm / field.shape[0])
    return float(field.sum(dtype=np.float64) * pixel_area_mm2)


def _swap_grouping_from_solve(solve: Mapping[str, Any]) -> dict | None:
    """Return the solve-owned band plan without inventing one at export time."""

    direct = solve.get("swap_grouping")
    if isinstance(direct, dict):
        return direct
    result = solve.get("result")
    if not isinstance(result, dict):
        return None
    staged_metrics = result.get("staged_metrics")
    if not isinstance(staged_metrics, dict):
        return None
    grouping = staged_metrics.get("swap_grouping")
    return grouping if isinstance(grouping, dict) else None


def _swap_plan_availability_from_solve(solve: Mapping[str, Any]) -> dict | None:
    result = solve.get("result")
    if not isinstance(result, dict):
        return None
    staged_metrics = result.get("staged_metrics")
    if not isinstance(staged_metrics, dict):
        return None
    availability = staged_metrics.get("swap_plan_availability")
    return availability if isinstance(availability, dict) else None


def _compute_total_surface(
    color_ceiling: np.ndarray,
    cap_map: np.ndarray,
) -> np.ndarray:
    """Total outer surface = color ceiling + visible white cap."""
    surface = color_ceiling + cap_map
    return surface.astype(np.float32)


def _compute_cap_component_surface(
    color_ceiling: np.ndarray,
    cap_component_map: np.ndarray,
) -> np.ndarray:
    """Absolute top height for one cap component, excluding later cap layers."""
    surface = color_ceiling + cap_component_map
    return surface.astype(np.float32)


def _white_cap_preview_scale_max(cfg: dict) -> float:
    """Absolute mm scale for white-cap thickness preview maps."""
    return max(float(cfg.get("t_max", 3.0) or 3.0), 1e-6)


def _save_surface_blob(arr: np.ndarray, path: Path) -> None:
    """Save a 2D float32 array as a binary blob with a shape header.

    Format: [uint32 height][uint32 width][float32 data row-major]
    """
    h, w = arr.shape
    with open(str(path), "wb") as f:
        f.write(np.array([h, w], dtype=np.uint32).tobytes())
        f.write(arr.astype(np.float32).tobytes())


def _save_uint32_blob(arr: np.ndarray, path: Path) -> None:
    """Save a 2D uint32 array as a binary blob with a shape header.

    Format: [uint32 height][uint32 width][uint32 data row-major]
    """
    h, w = arr.shape
    with open(str(path), "wb") as f:
        f.write(np.array([h, w], dtype=np.uint32).tobytes())
        f.write(np.ascontiguousarray(arr, dtype=np.uint32).tobytes())


def _canonical_plan_color_order(plan, palette_order) -> tuple[str, ...]:
    """Return color filament order for plan interval display."""
    declared: list[str] = []
    for filament_id in palette_order or ():
        fid = str(filament_id)
        if fid not in declared:
            declared.append(fid)
    extras = sorted(
        filament_id
        for filament_id in plan.filament_ids()
        if filament_id not in declared
    )
    return tuple(declared + extras)


def _banded_display_plan(swap_grouping, palette_order):
    if swap_grouping is None:
        return None
    return banded_export_plan_from_metadata(
        swap_grouping,
        d_wb_mm=float(swap_grouping["d_wb_mm"]),
        layer_height_mm=float(swap_grouping["layer_height_mm"]),
        expected_palette=palette_order,
    )


def _solved_palette_order_from_grouping(swap_grouping, fallback_order) -> list[str]:
    """Prefer the persisted canonical order; group order encodes physical bands."""

    if isinstance(swap_grouping, dict):
        canonical = swap_grouping.get("canonical_palette")
        if isinstance(canonical, list) and canonical:
            return [str(fid) for fid in canonical]
        groups = swap_grouping.get("groups")
        if isinstance(groups, list):
            flattened = [
                str(fid)
                for group in groups
                if isinstance(group, list)
                for fid in group
            ]
            if flattened:
                return flattened
    return [str(fid) for fid in fallback_order]


def _banded_stack_materials(stack_map, band_plan, white_fill_filament) -> list[dict[str, object]]:
    materials: list[dict[str, object]] = []
    fills = band_fill_thicknesses(
        stack_map,
        band_plan.groups,
        band_plan.band_layers,
        layer_height=band_plan.layer_height_mm,
    )
    for band_index, (group, fill_mm) in enumerate(zip(band_plan.groups, fills)):
        for filament_id in group:
            thickness_mm = float(stack_map.get(filament_id, 0.0))
            if thickness_mm > 0.0:
                materials.append({
                    "filament_id": filament_id,
                    "thickness_mm": thickness_mm,
                    "band_index": band_index,
                    "material_role": "color",
                })
        if fill_mm > 0.0:
            materials.append({
                "filament_id": str(white_fill_filament),
                "thickness_mm": float(fill_mm),
                "band_index": band_index,
                "material_role": "white_fill",
            })
    return materials


def _recipe_key_item(item: Mapping[str, object]) -> str:
    prefix = ""
    band_index = item.get("band_index")
    if band_index is not None:
        prefix = f"b{int(band_index)}:"
    return f"{prefix}{item['filament_id']}:{float(item['thickness_mm']):.6g}"


def _build_explorer_stack_table(
    plan,
    palette_order,
    *,
    swap_grouping=None,
    white_fill_filament: str | None = None,
) -> list[list[dict[str, object]]]:
    """Serialize plan stack material intervals in deterministic print order."""
    color_order = _canonical_plan_color_order(plan, palette_order)
    band_plan = _banded_display_plan(swap_grouping, palette_order)
    stack_table: list[list[dict[str, object]]] = []
    for stack in plan.stack_table:
        stack_map = stack.as_dict()
        if band_plan is not None:
            materials = _banded_stack_materials(
                stack_map,
                band_plan,
                white_fill_filament or "__white_cap__",
            )
        else:
            materials = []
            for filament_id in color_order:
                thickness_mm = float(stack_map.get(filament_id, 0.0))
                if thickness_mm > 0.0:
                    materials.append({
                        "filament_id": filament_id,
                        "thickness_mm": thickness_mm,
                    })
        stack_table.append(materials)
    return stack_table


def _save_explorer_plan_artifact(
    out_dir: Path,
    plan,
    palette_order,
    *,
    prefix: str | None = None,
    swap_grouping=None,
    white_fill_filament: str | None = None,
) -> dict[str, object] | None:
    """Write the plan-derived stack-label map used by Explorer Rich mode."""
    if plan is None:
        return None
    label_map = committed_stack_label_map(plan)
    if label_map.ndim != 2:
        return None
    filename = f"{prefix}_explorer_stack_label.bin" if prefix else "explorer_stack_label.bin"
    _save_uint32_blob(label_map, out_dir / filename)
    return {
        "filename": filename,
        "stack_table": _build_explorer_stack_table(
            plan,
            palette_order,
            swap_grouping=swap_grouping,
            white_fill_filament=white_fill_filament,
        ),
    }


def _save_color_recipe_breakdown_artifacts(
    out_dir: Path,
    plan,
    palette_order,
    *,
    prefix: str | None = None,
    swap_grouping=None,
    white_fill_filament: str | None = None,
) -> dict[str, object] | None:
    """Write per-recipe color-stack footprint artifacts for a solved plan."""
    if plan is None:
        return None

    segment_id_map = np.asarray(plan.segment_id_map, dtype=np.int64)
    segment_stack_id = np.asarray(plan.segment_stack_id, dtype=np.int64)
    if segment_id_map.ndim != 2 or segment_stack_id.ndim != 1:
        return None

    n_segments = int(segment_stack_id.shape[0])
    n_stacks = int(len(plan.stack_table))
    if n_segments <= 0 or n_stacks <= 0:
        return None

    segment_pixel_counts = np.bincount(
        segment_id_map.reshape(-1),
        minlength=n_segments,
    ).astype(np.int64, copy=False)
    stack_pixel_counts = np.zeros(n_stacks, dtype=np.int64)
    np.add.at(stack_pixel_counts, segment_stack_id, segment_pixel_counts)

    h, w = segment_id_map.shape
    x_pitch_mm = float(plan.image_domain_width_mm) / max(int(w), 1)
    y_pitch_mm = float(plan.image_domain_height_mm) / max(int(h), 1)
    pixel_area_mm2 = x_pitch_mm * y_pitch_mm
    color_order = _canonical_plan_color_order(plan, palette_order)
    band_plan = _banded_display_plan(swap_grouping, palette_order)

    entries: list[dict[str, object]] = []
    excluded_empty_recipe_pixels = 0
    for stack_index, stack in enumerate(plan.stack_table):
        pixel_count = int(stack_pixel_counts[stack_index])
        if pixel_count <= 0:
            continue
        stack_map = stack.as_dict()
        if band_plan is not None:
            recipe = _banded_stack_materials(
                stack_map,
                band_plan,
                white_fill_filament or "__white_cap__",
            )
        else:
            recipe = [
                {
                    "filament_id": filament_id,
                    "thickness_mm": float(stack_map[filament_id]),
                }
                for filament_id in color_order
                if float(stack_map.get(filament_id, 0.0)) > 0.0
            ]
        has_colored_material = any(item.get("material_role") != "white_fill" for item in recipe)
        if not has_colored_material:
            excluded_empty_recipe_pixels += pixel_count
            continue
        total_color_thickness_mm = float(stack.total_color_thickness_mm)
        total_white_fill_thickness_mm = float(sum(
            float(item["thickness_mm"])
            for item in recipe
            if item.get("material_role") == "white_fill"
        ))
        entries.append({
            "recipe_index": len(entries),
            "stack_index": int(stack_index),
            "pixel_count": pixel_count,
            "area_mm2": round(pixel_count * pixel_area_mm2, 6),
            "area_fraction_of_color_domain": 0.0,
            "total_color_thickness_mm": round(total_color_thickness_mm, 6),
            "total_white_fill_thickness_mm": round(total_white_fill_thickness_mm, 6),
            "recipe_key": " | ".join(
                _recipe_key_item(item)
                for item in recipe
            ),
            "recipe": recipe,
        })

    total_recipe_pixels = int(sum(int(entry["pixel_count"]) for entry in entries))
    total_recipe_area_mm2 = total_recipe_pixels * pixel_area_mm2
    for entry in entries:
        entry["area_fraction_of_color_domain"] = (
            round(float(entry["pixel_count"]) / total_recipe_pixels, 8)
            if total_recipe_pixels > 0
            else 0.0
        )
    entries.sort(
        key=lambda item: (
            -int(item["pixel_count"]),
            str(item["recipe_key"]),
        )
    )
    for idx, entry in enumerate(entries):
        entry["rank"] = idx + 1

    base = f"{prefix}_color_recipe_breakdown" if prefix else "color_recipe_breakdown"
    json_name = f"{base}.json"
    csv_name = f"{base}.csv"
    payload = {
        "schema": "prisma-color-recipe-breakdown-v1",
        "description": (
            "Color-stack recipe footprint grouped by identical solved recipe. "
            "White base and final white cap are excluded; swap-band white fill is included when present."
        ),
        "image_domain": {
            "width_px": int(w),
            "height_px": int(h),
            "width_mm": float(plan.image_domain_width_mm),
            "height_mm": float(plan.image_domain_height_mm),
            "x_pitch_mm": x_pitch_mm,
            "y_pitch_mm": y_pitch_mm,
            "pixel_area_mm2": pixel_area_mm2,
        },
        "totals": {
            "color_recipe_count": int(len(entries)),
            "color_domain_pixels": total_recipe_pixels,
            "color_domain_area_mm2": round(total_recipe_area_mm2, 6),
            "excluded_empty_recipe_pixels": int(excluded_empty_recipe_pixels),
            "all_plan_pixels": int(segment_id_map.size),
        },
        "recipes": entries,
    }
    (out_dir / json_name).write_text(
        json.dumps(payload, indent=2),
        encoding="utf-8",
    )

    max_layers = max((len(entry["recipe"]) for entry in entries), default=0)
    fieldnames = [
        "rank",
        "recipe_index",
        "stack_index",
        "pixel_count",
        "area_mm2",
        "area_fraction_of_color_domain",
        "total_color_thickness_mm",
        "total_white_fill_thickness_mm",
        "recipe_key",
    ]
    for i in range(max_layers):
        fieldnames.extend([f"filament_{i + 1}", f"thickness_{i + 1}_mm"])
    with (out_dir / csv_name).open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for entry in entries:
            row = {key: entry.get(key, "") for key in fieldnames}
            for i, item in enumerate(entry["recipe"]):
                row[f"filament_{i + 1}"] = item["filament_id"]
                row[f"thickness_{i + 1}_mm"] = item["thickness_mm"]
            writer.writerow(row)

    # Recipe cookbook: roll the per-recipe leaves into the family -> combo -> recipe
    # tree + filament rollup that the interactive recipe viewer consumes. Fractions
    # are of TOTAL image area (all_plan_pixels); the 0-color family comes from the
    # base-only pixel count. See recipe_cookbook.build_recipe_cookbook.
    cookbook = build_recipe_cookbook(
        entries,
        all_plan_pixels=int(payload["totals"]["all_plan_pixels"]),
        base_only_pixels=int(payload["totals"]["excluded_empty_recipe_pixels"]),
    )
    cookbook_name = f"{base}_cookbook.json"
    (out_dir / cookbook_name).write_text(
        json.dumps(cookbook, indent=2),
        encoding="utf-8",
    )

    return {
        "json_filename": json_name,
        "csv_filename": csv_name,
        "cookbook_filename": cookbook_name,
        "summary": payload["totals"],
    }


def _save_masked_contour_blob(
    surface_map: np.ndarray | None,
    mask_map: np.ndarray | None,
    path: Path,
    *,
    pad: tuple[int, int, int, int, int] | None = None,
    margin_height_mm: float = 0.0,
    border_height_mm: float = 0.0,
    margin_mask_mm: float = 0.0,
    border_mask_mm: float = 0.0,
) -> bool:
    """Save float values for frontend contours, zero where the component is absent."""
    if surface_map is None or mask_map is None:
        return False
    surface = np.asarray(surface_map, dtype=np.float32)
    mask_values = np.asarray(mask_map, dtype=np.float32)
    if surface.ndim != 2 or mask_values.ndim != 2 or surface.shape != mask_values.shape:
        return False
    padded_surface = (
        _pad_preview_2d(surface, pad, margin_height_mm, border_height_mm)
        if pad is not None
        else surface
    ).astype(np.float32, copy=True)
    padded_mask = (
        _pad_preview_2d(mask_values, pad, margin_mask_mm, border_mask_mm)
        if pad is not None
        else mask_values
    )
    padded_surface[padded_mask <= np.float32(1e-9)] = np.float32(0.0)
    _save_surface_blob(padded_surface, path)
    return True


def _write_thickness_blobs(
    out_dir: Path,
    cap_map: np.ndarray,
    thickness_maps: dict,
    prefix: str | None = None,
) -> dict:
    """Write the cap-height blob and one blob per non-special filament.

    Filenames:
      cap blob:       <prefix>_cap_height.bin   (or cap_height.bin if prefix is None)
      filament blob:  <prefix>_filament_<fid>.bin

    Special keys in thickness_maps starting with "__" are skipped (cap is
    written from `cap_map`, not from thickness_maps["__white_cap__"], so the
    two sources can diverge during the compare path without affecting
    output).

    Returns:
      {
        "cap_path":       Path,
        "filament_paths": { filament_id: Path, ... }
      }
    """
    cap_name = f"{prefix}_cap_height.bin" if prefix else "cap_height.bin"
    cap_path = out_dir / cap_name
    _save_surface_blob(cap_map.astype(np.float32, copy=False), cap_path)

    filament_paths: dict[str, Path] = {}
    for fid, arr in thickness_maps.items():
        if fid.startswith("__"):
            continue
        fname = f"{prefix}_filament_{fid}.bin" if prefix else f"filament_{fid}.bin"
        path = out_dir / fname
        _save_surface_blob(arr.astype(np.float32, copy=False), path)
        filament_paths[fid] = path
    return {"cap_path": cap_path, "filament_paths": filament_paths}


def _save_white_cap_part_map(
    arr: np.ndarray | None,
    path: Path,
    *,
    pad: tuple[int, int, int, int, int] | None = None,
    max_mm: float | None = None,
) -> dict[str, float | int] | None:
    """Save a cap-part thickness map and return active/max stats."""
    if arr is None:
        return None
    cap_part = np.asarray(arr, dtype=np.float32)
    if cap_part.ndim != 2:
        return None
    active_px = int(np.count_nonzero(cap_part > np.float32(1e-9)))
    max_d = float(np.max(cap_part)) if cap_part.size else 0.0
    scale_max = float(max_mm) if max_mm is not None else max(max_d, 0.08)
    padded = (
        _pad_preview_2d(cap_part, pad, 0.0, 0.0)
        if pad is not None
        else cap_part
    )
    _save_cap_height_map(
        padded,
        path,
        max_mm=scale_max,
        zero_rgb=_ZERO_THICKNESS_RGB,
    )
    return {"active_px": active_px, "max_d": round(max_d, 4)}


def _save_masked_white_cap_height_map(
    surface_map: np.ndarray | None,
    mask_map: np.ndarray | None,
    path: Path,
    *,
    pad: tuple[int, int, int, int, int] | None = None,
    max_mm: float,
    margin_height_mm: float = 0.0,
    border_height_mm: float = 0.0,
    margin_mask_mm: float = 0.0,
    border_mask_mm: float = 0.0,
) -> dict[str, float | int] | None:
    """Save absolute surface height, black where the selected cap is absent."""
    if surface_map is None or mask_map is None:
        return None
    surface = np.asarray(surface_map, dtype=np.float32)
    mask_values = np.asarray(mask_map, dtype=np.float32)
    if surface.ndim != 2 or mask_values.ndim != 2 or surface.shape != mask_values.shape:
        return None

    active_mask = mask_values > np.float32(1e-9)
    active_px = int(np.count_nonzero(active_mask))
    max_d = float(np.max(surface[active_mask])) if active_px else 0.0
    padded_surface = (
        _pad_preview_2d(surface, pad, margin_height_mm, border_height_mm)
        if pad is not None
        else surface
    )
    padded_mask_values = (
        _pad_preview_2d(mask_values, pad, margin_mask_mm, border_mask_mm)
        if pad is not None
        else mask_values
    )
    _save_cap_height_map(
        padded_surface,
        path,
        max_mm=max_mm,
        zero_rgb=_ZERO_THICKNESS_RGB,
        zero_mask=padded_mask_values <= np.float32(1e-9),
    )
    return {"active_px": active_px, "max_d": round(max_d, 4)}


def _make_gamut_overlay(source_img: np.ndarray, gamut_mask: np.ndarray) -> np.ndarray:
    """Overlay red on out-of-gamut pixels."""
    overlay = source_img.copy()
    mask = np.asarray(gamut_mask) > 0
    if mask.shape != overlay.shape[:2]:
        raise ValueError(
            f"gamut mask shape {mask.shape} does not match source image shape {overlay.shape[:2]}"
        )
    red = np.array([255, 60, 60], dtype=np.uint8)
    overlay[mask] = (
        (overlay[mask].astype(np.float32) * 0.4 + red.astype(np.float32) * 0.6)
        .clip(0, 255).astype(np.uint8)
    )
    return overlay


def _save_de_map_scaled(de_map: np.ndarray, path: Path, scale_max: float) -> None:
    """Save a false-color dE map with configurable scale ceiling."""
    de_clamp = np.clip(de_map / scale_max, 0, 1)
    r = (np.clip(de_clamp * 2,     0, 1) * 255).astype(np.uint8)
    g = (np.clip(2 - de_clamp * 2, 0, 1) * 255).astype(np.uint8)
    b = np.zeros_like(r)
    rgb = np.stack([r, g, b], axis=-1)
    Image.fromarray(rgb).save(str(path))


def _save_de_raw(de_map: np.ndarray, path: Path, de_max: float) -> None:
    """Save raw dE as 8-bit grayscale PNG, linearly mapped to [0, 255].

    Client uses this to do threshold-based OOG overlay rendering on canvas.
    de_max is sent as metadata so the client can map pixel values back to dE.
    """
    de_norm = np.clip(de_map / max(de_max, 1e-6), 0, 1)
    gray = (de_norm * 255).astype(np.uint8)
    Image.fromarray(gray, mode="L").save(str(path))



# ---------------------------------------------------------------------------
# Content-domain diagnostic array helpers
# ---------------------------------------------------------------------------

_ZERO_THICKNESS_RGB = (0, 0, 0)     # black: no material present


def _as_uint8_rgb_image(content: np.ndarray) -> np.ndarray:
    arr = np.asarray(content)
    if arr.ndim == 2:
        arr = np.repeat(arr[..., np.newaxis], 3, axis=2)
    if arr.shape[-1] > 3:
        arr = arr[..., :3]
    if arr.dtype != np.uint8:
        max_value = float(np.nanmax(arr)) if arr.size else 0.0
        if np.issubdtype(arr.dtype, np.floating) and max_value <= 1.0:
            arr = arr * 255.0
        arr = np.clip(arr, 0, 255).astype(np.uint8)
    return arr


def _pad_preview_2d(content: np.ndarray, padding, margin_fill, border_fill):
    """Optionally pad a 2D array for legacy helper callers."""
    mt, mb, ml, mr, bp = padding
    if mt == 0 and mb == 0 and ml == 0 and mr == 0 and bp == 0:
        return content

    H, W = content.shape
    out_H = H + mt + mb + 2 * bp
    out_W = W + ml + mr + 2 * bp
    out = np.full((out_H, out_W), border_fill, dtype=content.dtype)

    if mt > 0 or mb > 0 or ml > 0 or mr > 0:
        out[bp:out_H - bp, bp:out_W - bp] = margin_fill

    y0 = bp + mt
    x0 = bp + ml
    out[y0:y0 + H, x0:x0 + W] = content
    return out


def _validate_card_id(card_id: str) -> str:
    """Validate card_id is safe for use as a directory name."""
    import re
    if not re.match(r'^[a-zA-Z0-9_-]+$', card_id):
        raise HTTPException(400, f"Invalid card_id: {card_id}")
    return card_id


def _current_out_dir(card_id: str | None = None) -> Path:
    """Return (and create) the per-solve working directory in the clearable run cache (data/generator/cache/runs/{run_id}).

    run_id is the request's card_id (default 'current').
    """
    if card_id:
        _validate_card_id(card_id)
        out = data_paths.RUN_CACHE_DIR / card_id
    else:
        out = data_paths.RUN_CACHE_DIR / "current"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _run_cache_dir(run_id: str | None = None) -> Path:
    """Per-solve working dir in the clearable run cache. run_id == card_id."""
    return _current_out_dir(run_id)  # already cache-rooted


def _export_out_dir(export_id: str) -> Path:
    """Resolve a user-facing export directory without creating it."""
    _validate_card_id(export_id)  # reuse the existing slug/traversal guard
    return _OUTPUT_DIR / export_id


def _with_canonical_pitch_egress(cfg: dict) -> dict:
    """Return a shallow copy of cfg with legacy resolution aliases removed."""
    out = _force_mandatory_product_settings(cfg)
    out.pop("pixel_size_mm", None)
    out.pop("color_pixel_mm", None)
    out.pop("mesh_xy_pitch_mm", None)
    out["luminance_base_shading_limit_fraction"] = out.get(
        "luminance_handler_optical_authority_fraction",
        out.get("luminance_base_shading_limit_fraction", 0.75),
    )
    return out


# Solve-owned config keys: changing any of these invalidates a cached solve.
# This list is cross-referenced against _SETTINGS_PROFILE_KEYS and must cover
# every setting that feeds any stage of the solve pipeline.
#
# Intentionally absent:
#   - Export/display-owned: border_*
#   - Legacy aliases: pixel_size_mm, color_pixel_mm, mesh_xy_pitch_mm
#     (rejected at ingress)
_SOLVE_OWNED_KEYS = (
    # Resolution (canonical pitch fields only)
    "image_sample_pitch_mm",
    "solver_fine_pitch_mm",
    "color_region_target_mm",
    "detail_cap_pitch_mm",
    "detail_cap_max_layers",
    "detail_cap_smoothing_enabled",
    "detail_cap_smoothing_exact_speckle_max_px",
    "detail_cap_smoothing_cumulative_component_max_px",
    "detail_cap_smoothing_cumulative_hole_max_px",
    # Staged backend proof-slice knobs.
    "stage1_coarsening_factor",
    "color_region_target_from_printability",
    "color_region_target_width_multiplier",
    "stage2_fine_override_enabled",
    "stage2_final_printability_gate_fine_override",
    "stage2_printability_gate_fine_override",
    "stage2_printability_repair_fine_override",
    "stage2_boundary_mutation_enabled",
    "stage2_boundary_mutation_min_gain",
    "stage2_boundary_mutation_min_component_mm",
    "stage2_boundary_mutation_current_de_percentile",
    "stage2_boundary_mutation_max_passes",
    "stage4_printability_gate_detail",
    "luminance_detail_authoring_printability",
    # Core physics / solver
    "palette",
    "d_wb", "d_wc_min", "d_wc_max", "t_max", "k_max",
    # Source-image ingress settings must invalidate solve-owned outputs.
    "de_threshold", "gamut_mode", "gamut_white_rescale", "model_domain_ingress_lut_path", "chroma_weight",
    "luminance_mode",
    "luminance_handler_enabled",
    "luminance_handler_mode",
    "luminance_handler_strength",
    "luminance_handler_optical_authority_fraction",
    "luminance_base_shading_limit_fraction",
    "luminance_handler_boundary_percentile",
    "luminance_handler_boundary_sigma_px",
    "luminance_handler_response_curve",
    "luminance_handler_response_gamma",
    "luminance_handler_detail_residual",
    "luminance_handler_include_solver_detail",
    "use_corrections",
    "appearance_model_provider",
    "photo_stack_bundle_path",
    # Stage 1 zone-label generator params.
    "cell_mode", "smooth_boundaries", "boundary_smooth_radius",
    # Filament selection (white base/cap affect cap solve)
    "base_filament", "cap_filament",
    "layer_height",
    "v2_cleanup_de_budget", "v2_enable_cliff_closure",
    "v2_enable_cap_topology_cleanup", "v2_max_cleanup_rounds",
    "v2_full_cap_quality_report",
    # Boundary/detail cap surface-shaping params.
    "cap_mode", "boundary_cap_de_budget",
    # (No cleanup_* raster params here: all four retired — 2.2a reassign_mode/
    # search_radius_mm, 2.2b min_width/min_area. Wing-B feature scale is now
    # nozzle-derived; the active nozzle's size/min_line_width already enter this
    # fingerprint via __active_nozzle_printability__ below.)
    # Print-aware source resample kernel (Wing B / B7).
    # Toggling this changes the ingress raster and must invalidate
    # cached solves — per consensus §R6.C.
    "source_resample_kernel",
    # Thickness smoothing kernel (applied after solver)
    "smooth_kernel", "smooth_iters",
    # Image selection
    "image_path", "image_adjust", "max_dim_mm", "frame",
)


def _solve_owned_fingerprint(cfg: dict) -> str:
    """Stable hash of the canonical resolved solve-owned config subset.

    Anything not in _SOLVE_OWNED_KEYS is considered export-owned or
    presentation-only and does not invalidate a cached solve.

    Module activation state is the authoritative control plane for
    preprocessing - included via __module_state__ so toggling a module
    on/off invalidates the cached solve.

    Session config is always pre-normalized at ingress (set_config /
    resolution_schema), so re-normalization is not needed here.

    F1 R3-B: `preprocessing_params_enabled` is derived from the live
    module state and the session's `preprocessing_params` dict —
    only ENABLED preprocessing operators' params are hashed. Disabled
    operators' enablement still flows through `__module_state__`, so
    enable/disable transitions invalidate the cache; only their saved
    params are excluded from this derived subset (preserving R2-G
    storage semantics without contaminating the hash).
    """
    canonical_cfg = _force_mandatory_product_settings(cfg)
    subset = {k: canonical_cfg.get(k) for k in _SOLVE_OWNED_KEYS}
    active = get_active_printer()
    nozzle = active.get("nozzle") or {}
    subset["__active_nozzle_printability__"] = {
        "size": nozzle.get("size"),
        "min_line_width": nozzle.get("min_line_width"),
        "min_line_length": nozzle.get("min_line_length"),
    }
    subset["__module_state__"] = load_module_state(_MODULES_PATH)
    _ensure_registry_populated()
    pre_params = cfg.get("preprocessing_params", {}) or {}
    subset["preprocessing_params_enabled"] = {
        mid: pre_params.get(mid, {})
        for mid, enabled in subset["__module_state__"].items()
        if enabled and mid in PREPROCESSING_MODULE_IDS
    }
    if _is_photo_stack_provider(canonical_cfg.get("appearance_model_provider")):
        # The provider instance is not available at config-fingerprint time,
        # but photo-stack predictor logic changes alter solved colors and
        # export freshness.  Mirror the provider fingerprint's photo_stack_logic
        # token here so stale solves/exports cannot survive code-only predictor
        # changes.
        subset["__photo_stack_predictor_logic_version__"] = int(
            runtime_predictor.PHOTO_STACK_PREDICTOR_LOGIC_VERSION
        )
        # Likewise mirror the provider's appearance-domain contract version so
        # solves made under a different domain contract (e.g. pre-retirement
        # identity projection) are rejected as stale on export/swap.
        import appearance_model as _appearance_model

        subset["__photo_stack_appearance_domain_version__"] = str(
            _appearance_model.APPEARANCE_DOMAIN_VERSION
        )
    blob = json.dumps(subset, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _pitch_family_coupled(cfg: dict) -> bool:
    """True iff image_sample_pitch_mm == solver_fine_pitch_mm within tolerance.

    Session config always has canonical pitch fields populated by ingress
    normalization, so any legacy pixel_size_mm fallback is not needed.
    Returns True when any canonical value is absent (can't prove divergence).
    """
    _ABS_TOL = 1e-6
    _REL_TOL = 1e-9

    s = cfg.get("image_sample_pitch_mm")
    f = cfg.get("solver_fine_pitch_mm")
    if s is None or f is None:
        return True
    return math.isclose(float(s), float(f), rel_tol=_REL_TOL, abs_tol=_ABS_TOL)


def _write_run_json(run_dir: Path, data: dict) -> None:
    """Write run.json with canonical-only resolution fields and summary data."""
    cfg = data.get("config") or {}
    egress_cfg = _with_canonical_pitch_egress(cfg)

    # Build resolved block directly from egress_cfg (bypasses normalize which
    # raises on divergent values — _with_canonical_pitch_egress handles that).
    resolved = {}
    for key in ("image_sample_pitch_mm", "solver_fine_pitch_mm", "color_region_target_mm"):
        if key in egress_cfg:
            resolved[key] = egress_cfg[key]

    if resolved:
        resolved["phase1_coupled"] = _pitch_family_coupled(cfg)
        data = {
            **data,
            "config": egress_cfg,
            "resolved_resolution": resolved,
        }

    path = run_dir / "run.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)


def _module_descriptors_by_name() -> dict[str, dict]:
    """Return module descriptors keyed by module name."""
    return {module["name"]: module for module in list_all_modules()}


def _resolve_active_runtime_modules(module_state: Optional[dict] = None) -> dict:
    """Summarize the active runtime module selection from modules.json."""
    state = module_state or load_module_state(_MODULES_PATH)
    descriptors = _module_descriptors_by_name()

    def _active_in_slot(slot: str) -> list[str]:
        return [
            name for name, enabled in state.items()
            if enabled and descriptors.get(name, {}).get("slot") == slot
        ]

    active_preprocessing = _active_in_slot("preprocessing")
    return {
        "preprocessing": active_preprocessing,
    }


def _collect_module_param_values(cfg: dict, module_name: Optional[str], descriptors: dict[str, dict]) -> dict:
    """Collect config-backed parameter values for a specific active module."""
    if not module_name:
        return {}
    params = descriptors.get(module_name, {}).get("params", {})
    collected = {}
    for key, meta in sorted(params.items(), key=lambda item: (item[1].get("order", 0), item[0])):
        if key in cfg:
            collected[key] = cfg[key]
    return collected


def _build_solve_start_diagnostics(cfg: dict) -> dict:
    """Build a compact, inspectable summary of solve-start runtime inputs."""
    module_state = load_module_state(_MODULES_PATH)
    descriptors = _module_descriptors_by_name()
    active_modules = _resolve_active_runtime_modules(module_state)

    resolved_settings = {
        "appearance_model_provider": cfg.get("appearance_model_provider"),
        "use_corrections": cfg.get("use_corrections"),
        "gamut_mode": cfg.get("gamut_mode"),
        "gamut_white_rescale": cfg.get("gamut_white_rescale"),
        # Snapshot the source-image target transform.
        "model_domain_ingress": cfg.get("model_domain_ingress"),
        "chroma_weight": cfg.get("chroma_weight"),
        "image_sample_pitch_mm": cfg.get("image_sample_pitch_mm"),
        "solver_fine_pitch_mm": cfg.get("solver_fine_pitch_mm"),
        "color_region_target_mm": cfg.get("color_region_target_mm"),
        "luminance_mode": cfg.get("luminance_mode"),
        "detail_cap_max_layers": cfg.get("detail_cap_max_layers"),
        "de_threshold": cfg.get("de_threshold"),
        "layer_height": cfg.get("layer_height"),
        "t_max": cfg.get("t_max"),
        "k_max": cfg.get("k_max"),
        "image_path": cfg.get("image_path"),
        "palette": list(cfg.get("palette", [])),
        "data_root": str(_DATA_DIR),
    }
    if _is_photo_stack_provider(cfg.get("appearance_model_provider")):
        try:
            candidate_path = _resolve_photo_stack_candidate_path_for_solve(cfg)
            if candidate_path is not None:
                resolved_settings["photo_stack_candidate_path"] = str(candidate_path)
        except Exception as exc:
            resolved_settings["photo_stack_candidate_error"] = str(exc)
    resolved_settings = {k: v for k, v in resolved_settings.items() if v is not None}

    module_settings = {}
    for module_name in active_modules.get("preprocessing", []):
        values = _collect_module_param_values(cfg, module_name, descriptors)
        if values:
            module_settings[module_name] = values

    return {
        "active_modules": active_modules,
        "resolved_settings": resolved_settings,
        "module_settings": module_settings,
        "module_state": module_state,
    }


def _log_solve_start_diagnostics(job_id: str, diagnostics: dict) -> None:
    """Emit compact solve-start diagnostics to the server log."""
    active = diagnostics.get("active_modules", {})
    logger.info(
        "Solve %s runtime modules: preprocessing=%s",
        job_id,
        ", ".join(active.get("preprocessing", [])) or "(none)",
    )
    logger.info(
        "Solve %s resolved settings: %s",
        job_id,
        json.dumps(diagnostics.get("resolved_settings", {}), sort_keys=True, default=str),
    )
    module_settings = diagnostics.get("module_settings", {})
    if module_settings:
        logger.info(
            "Solve %s active module params: %s",
            job_id,
            json.dumps(module_settings, sort_keys=True, default=str),
        )


def _build_run_metadata(
    *,
    cfg: dict,
    stats,
    profile_ref,
    profile_name_at_solve,
    is_profile_modified_at_solve,
    recipe_snapshot,
    solve_start_diagnostics,
    card_id: str | None,
) -> dict:
    """Build the durable solve metadata payload used for cache and run logs."""
    source_rms_de = getattr(stats, "source_rms_de", getattr(stats, "mean_de", None))
    return {
        "card_id": card_id,
        "image": cfg.get("image_path", ""),
        "palette": list(cfg["palette"]),
        "profile_ref": profile_ref,
        "profile_name_at_solve": profile_name_at_solve,
        "is_profile_modified_at_solve": is_profile_modified_at_solve,
        "recipe_snapshot": recipe_snapshot,
        "solve_start_diagnostics": solve_start_diagnostics,
        "config": {
            k: v for k, v in cfg.items()
            if not isinstance(v, (np.ndarray,))
        },
        "stats": {
            "mean_de": stats.mean_de,
            "source_rms_de": source_rms_de,
            "max_de": stats.max_de,
            "n_oog": stats.n_out_of_gamut,
            "total_pixels": stats.total_pixels,
            "coverage_pct": stats.coverage_pct,
            "image_w": stats.image_w,
            "image_h": stats.image_h,
            "max_height": stats.max_height,
            "per_filament": [
                {
                    "filament_id": fs.filament_id,
                    "active_pixels": fs.active_pixels,
                    "mean_thickness": round(fs.mean_thickness, 4),
                    "max_thickness": round(fs.max_thickness, 4),
                }
                for fs in stats.per_filament
            ],
        },
    }


_RUNTIME_DIAGNOSTIC_KEYS = (
    "__appearance_provider__",
    "__provider_lut_cache__",
    "__target_gamut_mapping__",
    "__swap_grouping__",
    "__swap_plan_availability__",
)


def _json_safe_runtime_diagnostic(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _json_safe_runtime_diagnostic(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe_runtime_diagnostic(v) for v in value]
    if isinstance(value, np.ndarray):
        arr = np.asarray(value)
        summary: dict[str, Any] = {
            "shape": list(arr.shape),
            "dtype": str(arr.dtype),
        }
        if arr.size and arr.dtype.kind in "biuf":
            finite = arr[np.isfinite(arr)] if arr.dtype.kind in "f" else arr.reshape(-1)
            if finite.size:
                summary.update(
                    {
                        "min": float(np.min(finite)),
                        "max": float(np.max(finite)),
                        "mean": float(np.mean(finite)),
                    }
                )
        return summary
    return str(value)


def _runtime_diagnostics_from_result(result) -> dict[str, Any]:
    diagnostics = getattr(result, "diagnostics", {}) or {}
    out: dict[str, Any] = {}
    for key in _RUNTIME_DIAGNOSTIC_KEYS:
        if key in diagnostics:
            out[key] = _json_safe_runtime_diagnostic(diagnostics[key])
    return out


def _staged_metrics_from_result(result) -> Dict[str, Any]:
    staged_metrics: Dict[str, Any] = {}
    staged_result = getattr(result, "staged_result", None)
    perf = getattr(staged_result, "performance_profile", None)
    if perf is not None:
        for key, value in getattr(perf, "counters", {}).items():
            if isinstance(value, (np.integer,)):
                value = int(value)
            elif isinstance(value, (np.floating,)):
                value = float(value)
            elif isinstance(value, (np.bool_,)):
                value = bool(value)
            staged_metrics[str(key)] = value
        for key, value in getattr(perf, "timings_s", {}).items():
            staged_metrics[f"{key}_s"] = float(value)
    cap_quality = getattr(result, "cap_quality", {}) or {}
    smoothing = cap_quality.get("detail_cap_smoothing")
    if isinstance(smoothing, dict) and smoothing.get("applied"):
        staged_metrics.update(
            {
                "detail_cap_smoothing_applied": True,
                "detail_cap_smoothing_changed_px": int(
                    smoothing.get("changed_px", 0) or 0
                ),
                "detail_cap_smoothing_raised_px": int(
                    smoothing.get("raised_px", 0) or 0
                ),
                "detail_cap_smoothing_lowered_px": int(
                    smoothing.get("lowered_px", 0) or 0
                ),
                "detail_cap_smoothing_mean_abs_layer_delta": float(
                    smoothing.get("mean_abs_layer_delta", 0.0) or 0.0
                ),
                "detail_cap_smoothing_p95_abs_layer_delta": float(
                    smoothing.get("p95_abs_layer_delta", 0.0) or 0.0
                ),
                "detail_cap_smoothing_max_abs_layer_delta": int(
                    smoothing.get("max_abs_layer_delta", 0) or 0
                ),
            }
        )
    return staged_metrics


def _attach_staged_metrics_to_run_metadata(
    run_metadata: dict,
    staged_metrics: Dict[str, Any],
) -> None:
    if not staged_metrics:
        return
    run_metadata["staged_metrics"] = dict(staged_metrics)
    stats = run_metadata.setdefault("stats", {})
    for key in (
        "blueprint_printability_minimum_extrusion_width_mm",
        "blueprint_printability_minimum_line_length_mm",
        "blueprint_printability_runtime_s",
        "blueprint_printability_hard_fail_component_count",
        "blueprint_printability_hard_fail_pixels",
        "blueprint_printability_color_hard_fail_pixels",
        "blueprint_printability_boundary_cap_hard_fail_pixels",
        "blueprint_printability_detail_hard_fail_pixels",
    ):
        if key in staged_metrics:
            stats[key] = staged_metrics[key]


def _run_material_exposure_audit(
    thickness_maps: dict,
    cfg: dict,
) -> MaterialExposureAudit:
    return audit_colored_filament_exposure_from_thickness_maps(
        thickness_maps,
        layer_height_mm=float(cfg["layer_height"]),
        excluded_material_ids=_white_ids(cfg),
    )


# NOTE (2026-06-19) — SUSPECTED INERT in the live path (not confirmed). During a
# solve only the NON-raising _run_material_exposure_audit runs (it records a
# diagnostic, gates nothing); this assert-form gate that actually raises
# PrintabilityError appears to be exercised only by
# tests/generator/test_material_exposure_product_gate.py. Suspected, not proven —
# an earlier audit missed references here, so re-verify the full reference graph
# (callers, tests, config plumbing) before wiring it in, disabling, or removing it.
def _assert_material_exposure_safe_for_product(
    thickness_maps: dict,
    cfg: dict,
) -> MaterialExposureAudit:
    audit = _run_material_exposure_audit(thickness_maps, cfg)
    if not audit.passes:
        summary = audit.to_summary()
        raise PrintabilityError(
            "White-cap solve exposes colored filament to air "
            f"(total={summary['total_exposed_face_count']}, "
            f"lateral={summary['lateral_internal_face_count']}, "
            f"top={summary['top_face_count']}, "
            f"exterior={summary['exterior_face_count']})."
        )
    return audit


_PALETTE_DIR_RE = re.compile(r"^palette-(\d+)$")


def _next_palette_index(existing_names) -> int:
    """Return max(palette-NN) + 1 across existing_names, or 1 if none match.

    Non-matching entries are ignored. Gaps are not filled; we always take
    one above the current numeric maximum so repeated solves in a batch
    produce stable, collision-free ordering.
    """
    indices = []
    for name in existing_names:
        m = _PALETTE_DIR_RE.match(name)
        if m:
            indices.append(int(m.group(1)))
    return max(indices) + 1 if indices else 1


def _run_dir_palette_subfolder(parent: Path) -> Path:
    """Create the next sequential `palette-NN` subfolder under parent.

    Index is scanned from existing children, so the function is self-healing
    across crashes and manual edits. Width auto-widens past two digits so
    a batch can exceed 99 palettes without breaking sort order within the
    normal single-width band.
    """
    existing = [p.name for p in parent.iterdir() if p.is_dir()]
    idx = _next_palette_index(existing)
    width = max(2, len(str(idx)))
    sub = parent / f"palette-{idx:0{width}d}"
    sub.mkdir(parents=False, exist_ok=False)
    return sub


def _cfg() -> dict:
    """Shorthand for current config."""
    return session["config"]


def _resolve_export_target(card_id: str | None = None) -> tuple[dict, dict, str | None]:
    """Return (solve_bundle, cfg_snapshot, card_id) for export operations."""
    if card_id:
        _validate_card_id(card_id)
        cached = session.get("solve_cache", {}).get(card_id)
        if cached:
            return cached["solve"], cached["config"], card_id

        solve = session["solve"]
        if solve.get("status") == "complete" and solve.get("card_id") == card_id:
            return solve, _cfg(), card_id

        raise HTTPException(404, f"No cached solve found for run {card_id}")

    solve = session["solve"]
    return solve, _cfg(), solve.get("card_id")


# ═══════════════════════════════════════════════════════════════════════════
#  ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════


# ── Filaments ─────────────────────────────────────────────────────────────

@app.get("/api/filaments")
def list_filaments() -> List[dict]:
    """Return all registered filaments with profile availability."""
    registry = _load_registry()
    result = []
    for fid, info in sorted(registry.items()):
        has_profile = _runtime_profile_exists(fid)
        excluded = bool(info.get("exclude_from_model", False))
        result.append({
            "filament_id": fid,
            "display_name": info.get("display_name", fid),
            "manufacturer": info.get("manufacturer", ""),
            "color_name": info.get("color_name", ""),
            "hex": info.get("hex", "#888888"),
            "has_profile": has_profile,
            "white_cap_eligible": bool(info.get("white_cap_eligible", False)),
            # Contract bridge: surface the calibration model-policy flag and a
            # derived generation-availability so UI/callers can warn/disable.
            "exclude_from_model": excluded,
            "generation_available": not excluded,
        })
    return result


# ── Images ────────────────────────────────────────────────────────────────

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}


@app.get("/api/images")
def list_images() -> List[dict]:
    """List source images in images/ directory."""
    if not _IMAGES_DIR.exists():
        return []
    files = sorted(
        p for p in _IMAGES_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in _IMAGE_EXTENSIONS
    )
    return [_image_info(f) for f in files]


def _open_images_folder() -> None:
    _IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    open_folder_in_file_manager(_IMAGES_DIR)


@app.post("/api/images/open-folder")
def open_images_folder() -> dict:
    try:
        _open_images_folder()
    except OSError as exc:
        raise HTTPException(500, f"Could not open the Images folder: {exc}") from exc
    return {"opened": True}


@app.post("/api/images/upload")
async def upload_image(file: UploadFile) -> dict:
    """Upload an image to images/ directory."""
    if not file.filename:
        raise HTTPException(400, "No filename provided")

    suffix = Path(file.filename).suffix.lower()
    if suffix not in _IMAGE_EXTENSIONS:
        raise HTTPException(400, f"Unsupported format: {suffix}")

    _IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    # Sanitize filename — use only the basename to prevent path traversal
    safe_name = Path(file.filename).name
    dest = _IMAGES_DIR / safe_name

    # Avoid overwriting — append a counter if needed
    counter = 1
    while dest.exists():
        stem = Path(file.filename).stem
        dest = _IMAGES_DIR / f"{stem}_{counter}{suffix}"
        counter += 1

    content = await file.read()
    dest.write_bytes(content)

    try:
        with Image.open(dest) as im:
            w, h = im.size
    except Exception as exc:
        dest.unlink(missing_ok=True)
        raise HTTPException(400, f"Not a valid image: {exc}")

    logger.info("Uploaded image: %s (%dx%d)", dest.name, w, h)
    return {"filename": dest.name, "width": w, "height": h}


def _safe_path(base: Path, filename: str) -> Path:
    """Resolve filename under base, rejecting path traversal."""
    resolved = (base / filename).resolve()
    if not resolved.is_relative_to(base.resolve()):
        raise HTTPException(400, "Invalid filename")
    return resolved


@app.get("/api/images/preview/{filename}")
def get_image_preview(filename: str):
    """Return a resized JPEG preview (max 800px wide)."""
    path = _safe_path(_IMAGES_DIR, filename)
    if not path.exists():
        raise HTTPException(404, f"Image not found: {filename}")

    try:
        with Image.open(path) as raw_img:
            img = ImageOps.exif_transpose(raw_img)
            img = img.convert("RGB")
    except Exception as exc:
        raise HTTPException(400, f"Cannot open image: {exc}")

    img.thumbnail((800, 800))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    img.close()
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/jpeg")


# ── Session / Config ──────────────────────────────────────────────────────

@app.get("/api/session")
def get_session() -> dict:
    """Return full session state (config + solve status)."""
    return {
        "config": _with_canonical_pitch_egress(session["config"]),
        "solve": _serialize_solve_status(session["solve"]),
    }


def _solve_elapsed_seconds(solve: dict) -> float:
    started = solve.get("started_monotonic")
    if solve.get("status") == "running" and started is not None:
        return max(0.0, time.monotonic() - float(started))
    return max(0.0, float(solve.get("elapsed_s", 0.0) or 0.0))


def _serialize_solve_status(solve: dict) -> dict:
    progress = solve.get("progress", {})
    detail = progress if isinstance(progress, dict) else {}
    label = detail.get("stage_label", "") if detail else str(progress or "")
    return {
        "job_id": solve.get("job_id"),
        "card_id": solve.get("card_id"),
        "status": solve.get("status", "idle"),
        "progress": label,
        "progress_detail": detail,
        "elapsed_s": round(_solve_elapsed_seconds(solve), 2),
        "result": solve.get("result"),
        "cancel_requested": bool(solve.get("cancel_requested", False)),
    }


def _translate_resolution_schema_error(exc: Exception) -> HTTPException:
    """Translate resolution schema errors into HTTP 422 with field metadata."""
    if isinstance(exc, ResolutionSchemaLegacyFieldError):
        return HTTPException(
            status_code=422,
            detail={
                "error": "legacy_resolution_field",
                "message": str(exc),
                "field": exc.field,
                "replacements": list(exc.replacements),
            },
        )
    if isinstance(exc, ResolutionSchemaConflictError):
        return HTTPException(
            status_code=422,
            detail={
                "error": "resolution_schema_conflict",
                "message": str(exc),
                "field_a": exc.field_a,
                "value_a": exc.value_a,
                "field_b": exc.field_b,
                "value_b": exc.value_b,
            },
        )
    raise TypeError(f"Unsupported resolution schema error: {exc!r}")


@app.get("/api/session/config")
def get_config() -> dict:
    """Return current session config in canonical-only form."""
    return _with_canonical_pitch_egress(session["config"])


_RETIRED_CONFIG_FIELDS = frozenset({
    "smooth_stl",
    "solver_backend",
    # Task 2.1a (2026-06-13): retired guided/hybrid cap-shaping + top-level
    # tv_denoise tv_weight. NOTE: this is the top-level cap field only — the
    # live B3 preprocessing param preprocessing_params.b3_tv_flatten.tv_weight
    # is unaffected (nested, not a top-level config key).
    "guided_surface_mode",
    "guided_surface_radius_mm",
    "guided_surface_eps",
    "guided_surface_gaussian_sigma_mm",
    "hybrid_relax_strength",
    "hybrid_relax_radius_mm",
    "hybrid_edge_guard",
    "hybrid_underfill_bias",
    "tv_weight",
    # Task 2.1b (2026-06-13): retired dead cap convergence/significance fields
    # (audit-confirmed zero live readers; never threaded into PipelineConfig).
    "cap_convergence_mm",
    "cap_significant_layers",
    # Task 2.2a (2026-06-13): retired orphaned raster-cleanup params. Their
    # consumer (MinFeatureCleanupRefinement) was deleted with refinements/
    # on 2026-06-12; audit confirmed zero live readers anywhere in source.
    "cleanup_reassign_mode",
    "cleanup_search_radius_mm",
    # Task 2.2b (2026-06-13): retired the last two cleanup_* fields after
    # re-anchoring Wing-B preprocessing to nozzle_diameter (2 x nozzle).
    # cleanup_min_width_mm was the preprocessing feature-scale anchor;
    # cleanup_min_area_mm2 fed (dormant) blueprint-triage thresholds.
    "cleanup_min_width_mm",
    "cleanup_min_area_mm2",
    # Task 2.3 (2026-06-13): removed the translucent-underfill feature entirely
    # (config family + the solve-time __translucent_underfill__ overlay). Reject
    # stale submissions of any of these keys loudly instead of dropping silently.
    "v2_translucent_underfill_enabled",
    "v2_translucent_underfill_filament",
    "v2_translucent_underfill_max_mm",
    "v2_translucent_underfill_de_budget",
    "v2_translucent_underfill_white_skin_mm",
    "v2_translucent_preferred_visible_skin_mode",
    "v2_translucent_support_target_mode",
    "v2_translucent_underfill_policy",
    "v2_translucent_underfill_safe_subset_erosion_px",
    "v2_translucent_underfill_safe_subset_min_debt_layers",
    "v2_translucent_underfill_chooser_de_weight",
    "v2_translucent_underfill_component_activation_cost",
    # 2026-07-01: retired detail-cap mode selector in favor of the single
    # detail layer limit control.
    "stage4_independent_detail_surface",
    "stage4_optical_detail_surface",
    # 2026-07-03: retired fixed-thickness white-cap mode.
    "cap_fixed_thickness_mm",
    # 2026-07-03: retired preferred line length and its soft-warning diagnostics.
    "printability_preferred_line_length_mm",
})


_STRICT_RETIRED_CONFIG_FIELDS = frozenset({
    "cap_fixed_thickness_mm",
    "printability_preferred_line_length_mm",
})


def _retired_config_field_error(field: str) -> HTTPException:
    return HTTPException(
        422,
        {
            "error": "retired_config_field",
            "field": field,
            "message": (
                f"Retired config field {field!r} is no longer accepted; "
                "use the current product settings instead"
            ),
        },
    )


def _payload_model_fields(payload: ConfigPayload) -> dict:
    """Return explicit live ConfigPayload fields while rejecting retired extras.

    ConfigPayload allows extras only so this function can inspect stale client
    keys. Unknown non-retired extras keep the old behavior and are ignored.
    """
    dumped = payload.model_dump(exclude_unset=True)
    field_names = set(ConfigPayload.model_fields)
    extras = getattr(payload, "model_extra", None) or {}
    submitted_extras = {
        key: dumped.get(key, value)
        for key, value in extras.items()
        if key not in field_names
    }

    for legacy, replacements in LEGACY_RESOLUTION_REPLACEMENTS.items():
        if legacy in submitted_extras and submitted_extras[legacy] is not None:
            raise _translate_resolution_schema_error(
                ResolutionSchemaLegacyFieldError(legacy, replacements)
            )

    for field in sorted(_RETIRED_CONFIG_FIELDS):
        if (
            field in submitted_extras
            and (
                field in _STRICT_RETIRED_CONFIG_FIELDS
                or submitted_extras[field] is not None
            )
        ):
            raise _retired_config_field_error(field)

    for field in sorted(_QUIET_DROPPED_CONFIG_EXTRAS):
        if field in submitted_extras:
            logger.info("Dropping retired config key from payload: %s", field)
            dumped.pop(field, None)

    return {key: value for key, value in dumped.items() if key in field_names}


@app.post("/api/session/config")
def set_config(payload: ConfigPayload) -> dict:
    """Set config fields (partial PATCH semantics).

    Canonical-only session config:
      - legacy aliases are rejected
      - canonical pitch fields are validated directly
      - partial updates merge into the current session first, then validate
        the merged result
    """
    old = session["config"]
    incoming = _payload_model_fields(payload)
    if "luminance_base_shading_limit_fraction" in incoming:
        incoming["luminance_handler_optical_authority_fraction"] = incoming.pop(
            "luminance_base_shading_limit_fraction"
        )

    try:
        norm_out = normalize_resolution_schema({**old, **incoming})
    except (ResolutionSchemaConflictError, ResolutionSchemaLegacyFieldError) as exc:
        raise _translate_resolution_schema_error(exc) from exc

    # Merge: start from session, overlay normalized resolution fields, then
    # overlay any remaining non-resolution incoming fields (already in norm_out
    # for resolution fields; added below for non-resolution).
    merged = {**old, **norm_out}
    # Also carry any non-resolution fields from incoming that normalize didn't touch.
    for k, v in incoming.items():
        if k not in norm_out:
            merged[k] = v
    merged = _apply_luminance_mode_preset(
        merged,
        reset_standard=("luminance_mode" in incoming),
    )
    merged = _force_mandatory_product_settings(merged)
    merged["luminance_base_shading_limit_fraction"] = merged.get(
        "luminance_handler_optical_authority_fraction",
        merged.get("luminance_base_shading_limit_fraction", 0.75),
    )
    changed = [k for k in merged if old.get(k) != merged[k]]
    session["config"] = merged
    if changed:
        logger.info("Config updated: %s", ", ".join(changed))
    return {"ok": True, "config": _with_canonical_pitch_egress(session["config"])}


# ── Printers ──────────────────────────────────────────────────────────────

@app.get("/api/printers")
def get_printers() -> dict:
    """Return all printer configs + active selection."""
    return _load_printers()


@app.put("/api/printers")
def save_printers(payload: dict) -> dict:
    """Save the full printers object (printers list + active selection)."""
    _normalize_printers_data(payload, retired_policy="reject")
    _save_printers(payload)
    logger.info("Printers saved: %d printer(s)", len(payload.get("printers", [])))
    return {"ok": True}


@app.get("/api/printers/active")
def get_active_printer() -> dict:
    """Return the resolved active printer and nozzle profile."""
    data = _load_printers()
    return _resolve_active_printer(data)


@app.put("/api/printers/active")
def set_active_printer(payload: dict) -> dict:
    """Set active_printer_id and/or active_nozzle_size."""
    data = _load_printers()
    if "active_printer_id" in payload:
        data["active_printer_id"] = payload["active_printer_id"]
    if "active_nozzle_size" in payload:
        data["active_nozzle_size"] = payload["active_nozzle_size"]
    _save_printers(data)
    return {"ok": True, **_resolve_active_printer(data)}


# ── Modules ──────────────────────────────────────────────────────────────

@app.get("/api/modules")
def get_modules() -> dict:
    """Return all modules with descriptors and enabled state."""
    state = load_module_state(_MODULES_PATH)
    modules = [
        m for m in list_all_modules()
        if m.get("slot") in {"grouping", "preprocessing"}
    ]
    for m in modules:
        m["enabled"] = state.get(m["name"], m.get("default_enabled", False))
    return {"modules": modules}


@app.post("/api/modules/toggle")
def toggle_module_endpoint(payload: dict) -> dict:
    """Toggle a module on/off. Expects {module_id: str, enabled: bool}."""
    module_id = payload.get("module_id")
    enabled = payload.get("enabled")
    if not module_id or enabled is None:
        raise HTTPException(400, "module_id and enabled required")
    state = toggle_module(_MODULES_PATH, module_id, bool(enabled))
    logger.info("Module toggled: %s = %s", module_id, enabled)
    return {"ok": True, "state": state}


@app.put("/api/modules/state")
def set_module_state_endpoint(payload: dict) -> dict:
    """Persist a complete module toggle snapshot."""
    state = payload.get("state")
    if not isinstance(state, dict):
        raise HTTPException(400, "state dict required")
    save_module_state(_MODULES_PATH, state)
    normalized = load_module_state(_MODULES_PATH)
    logger.info("Module state replaced: %d active module(s)", sum(bool(v) for v in normalized.values()))
    return {"ok": True, "state": normalized}


# ── Settings Profiles ──────────────────────────────────────────────────────

# Settings persisted in a Settings Profile record.
_SETTINGS_PROFILE_KEYS = (
    # --- session-owned canonical settings ---
    "base_filament",
    "cap_filament",
    "layer_height",
    "d_wb",
    "d_wc_min",
    "t_max",
    "k_max",
    "de_threshold",
    "smooth_kernel",
    "use_corrections",
    "appearance_model_provider",
    "photo_stack_bundle_path",
    "gamut_mode",
    "gamut_white_rescale",
    "model_domain_ingress_lut_path",
    "chroma_weight",
    "luminance_mode",
    "luminance_base_shading_limit_fraction",
    "luminance_detail_authoring_printability",
    # --- canonical resolution (Phase 2+) ---
    "image_sample_pitch_mm",
    "solver_fine_pitch_mm",
    "detail_cap_pitch_mm",
    "detail_cap_max_layers",
    "detail_cap_smoothing_enabled",
    "detail_cap_smoothing_exact_speckle_max_px",
    "detail_cap_smoothing_cumulative_component_max_px",
    "detail_cap_smoothing_cumulative_hole_max_px",
    "color_region_target_mm",
    "cell_mode",
    # --- canonical staged backend params ---
    "stage1_coarsening_factor",
    "stage2_fine_override_enabled",
    "stage2_boundary_mutation_enabled",
    "stage2_boundary_mutation_min_gain",
    "stage2_boundary_mutation_min_component_mm",
    "stage2_boundary_mutation_current_de_percentile",
    "stage2_boundary_mutation_max_passes",
    # --- boundary/detail cap params ---
    "cap_mode",
    "boundary_cap_de_budget",
    # Wing B / B7 print-aware resample kernel
    "source_resample_kernel",
    # F1 preprocessing operator param blocks keyed by operator id.
    "preprocessing_params",
)


@dataclass
class SettingsProfileRecord:
    id: str
    kind: str
    name: str
    settings: Dict[str, Any]
    modules: Dict[str, bool]
    created_at: str
    updated_at: str
    schema_version: int = _SETTINGS_PROFILE_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "settings": deepcopy(self.settings),
            "modules": dict(self.modules),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "schema_version": self.schema_version,
        }


class SettingsProfilePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    settings: Dict[str, Any] = Field(default_factory=dict)
    modules: Dict[str, bool] = Field(default_factory=dict)


def _settings_profile_state_path() -> Path:
    return _SETTINGS_PROFILES_DIR / _SETTINGS_PROFILE_STATE_NAME


def _settings_profile_path(profile_id: str) -> Path:
    return _SETTINGS_PROFILES_DIR / f"{profile_id}.json"


def _settings_profile_relative_path(path: Path) -> Path:
    return path.relative_to(_SETTINGS_PROFILES_DIR)


def _settings_profile_sort_key(path: Path) -> tuple[int, str]:
    relative = _settings_profile_relative_path(path)
    return (len(relative.parts), relative.as_posix().casefold())


def _settings_profile_fallback_id(path: Path) -> str:
    relative = _settings_profile_relative_path(path).with_suffix("")
    if len(relative.parts) == 1:
        return relative.name
    digest = hashlib.sha256(relative.as_posix().encode("utf-8")).hexdigest()[:12]
    return f"{relative.name}-{digest}"


def _settings_profile_named_paths() -> List[Path]:
    if not _SETTINGS_PROFILES_DIR.exists():
        return []
    return sorted(
        (
            path
            for path in _SETTINGS_PROFILES_DIR.rglob("*.json")
            if path.name
            not in {_SETTINGS_PROFILE_STATE_NAME, f"{_SYSTEM_SETTINGS_PROFILE_ID}.json"}
        ),
        key=_settings_profile_sort_key,
    )


def _settings_profile_paths_for_id(profile_id: str) -> List[Path]:
    matches: List[Path] = []
    for path in _settings_profile_named_paths():
        try:
            record = _load_settings_profile_record(path, kind_hint="named")
        except Exception as exc:
            logger.warning("Skipping invalid settings profile %s: %s", path.name, exc)
            continue
        if record.id == profile_id:
            matches.append(path)
    return matches


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    tmp.replace(path)


def _generate_settings_profile_id() -> str:
    return f"profile-{uuid.uuid4().hex[:12]}"


def _normalize_preprocessing_params(raw: Any) -> Dict[str, Dict[str, Any]]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("preprocessing_params must be an object keyed by module id")

    normalized: Dict[str, Dict[str, Any]] = {}
    for module_id, params in raw.items():
        if not isinstance(module_id, str):
            raise ValueError("preprocessing_params keys must be strings")
        if params is None:
            normalized[module_id] = {}
            continue
        if not isinstance(params, dict):
            raise ValueError(
                f"preprocessing_params[{module_id!r}] must be an object"
            )
        normalized[module_id] = deepcopy(dict(params))
    return normalized


_RETIRED_SETTINGS_PROFILE_MODULE_KEYS = frozenset(
    {
        "greedy_" + "optimize",
        "greedy_" + "grouping",
        "group_" + "budget",
    }
)
_REPORTED_RETIRED_SETTINGS_PROFILE_MODULE_KEYS: set[str] = set()


def _normalize_settings_profile_modules(modules: Optional[dict]) -> Dict[str, bool]:
    incoming = dict(modules or {})
    retired = sorted(
        key for key in incoming
        if key in _RETIRED_SETTINGS_PROFILE_MODULE_KEYS
    )
    for key in retired:
        incoming.pop(key, None)
    unseen = [
        key for key in retired
        if key not in _REPORTED_RETIRED_SETTINGS_PROFILE_MODULE_KEYS
    ]
    if unseen:
        logger.info(
            "Dropping retired module key(s) from settings profile: %s",
            ", ".join(unseen),
        )
        _REPORTED_RETIRED_SETTINGS_PROFILE_MODULE_KEYS.update(unseen)
    return _normalize_module_state(incoming)


def _normalize_settings_profile_settings(settings: Optional[dict]) -> Dict[str, Any]:
    incoming = dict(settings or {})
    incoming.pop("run_logging", None)  # retired feature: quiet-drop stale profile/UI residue
    for key in sorted(_QUIET_DROPPED_CONFIG_EXTRAS):
        if key in incoming:
            logger.info("Dropping retired config key from settings profile: %s", key)
            incoming.pop(key, None)
    retired_subject = "protect" + "_subject"
    retired_mask = "protect" + "_mask"
    for key in (
        f"{retired_subject}_enabled",
        f"{retired_subject}_strength",
        "protect" + "_confidence_floor",
        f"{retired_mask}_provider",
        f"{retired_mask}_override",
    ):
        incoming.pop(key, None)
    if "detail_cap_enabled" in incoming:
        if incoming["detail_cap_enabled"] is not True:
            raise ValueError(
                "detail_cap_enabled is mandatory and can no longer be disabled"
            )
        incoming.pop("detail_cap_enabled", None)
    if "cap_fixed_thickness_mm" in incoming:
        raise ValueError(
            "Retired config field 'cap_fixed_thickness_mm' is no longer accepted; "
            "use the current product settings instead"
        )
    if "cap_mode" in incoming:
        incoming["cap_mode"] = _normalize_cap_mode(incoming["cap_mode"])
    if "gamut_mode" in incoming:
        incoming["gamut_mode"] = ConfigPayload._normalize_gamut_mode(incoming["gamut_mode"])  # type: ignore[misc]
    if "base_filament" not in incoming and incoming.get("white_base"):
        incoming["base_filament"] = incoming["white_base"]
    if "cap_filament" not in incoming and "white_cap" in incoming:
        white_cap = incoming.get("white_cap")
        white_base = incoming.get("white_base") or incoming.get("base_filament")
        incoming["cap_filament"] = "__same__" if not white_cap or white_cap == white_base else white_cap
    if "luminance_mode" in incoming:
        incoming["luminance_mode"] = _normalize_luminance_mode(incoming["luminance_mode"])
    if "luminance_base_shading_limit_fraction" in incoming:
        incoming["luminance_handler_optical_authority_fraction"] = incoming[
            "luminance_base_shading_limit_fraction"
        ]
    elif "luminance_handler_optical_authority_fraction" in incoming:
        incoming["luminance_base_shading_limit_fraction"] = incoming[
            "luminance_handler_optical_authority_fraction"
        ]
    if "preprocessing_params" in incoming:
        incoming["preprocessing_params"] = _normalize_preprocessing_params(
            incoming["preprocessing_params"]
        )
    normalized = {key: deepcopy(_DEFAULT_CONFIG[key]) for key in _SETTINGS_PROFILE_KEYS}
    for key in _SETTINGS_PROFILE_KEYS:
        if key in incoming:
            normalized[key] = deepcopy(incoming[key])
    return _force_mandatory_product_settings(normalized)


def _load_settings_profile_record(path: Path, kind_hint: Optional[str] = None) -> SettingsProfileRecord:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    profile_id = str(data.get("id") or _settings_profile_fallback_id(path))
    kind = str(data.get("kind") or kind_hint or ("system" if profile_id == _SYSTEM_SETTINGS_PROFILE_ID else "named"))
    raw_settings = normalize_resolution_schema(dict(data.get("settings") or {}))
    settings = _normalize_settings_profile_settings(raw_settings)
    modules = _normalize_settings_profile_modules(dict(data.get("modules") or {}))
    created_at = str(data.get("created_at") or _utc_now_iso())
    updated_at = str(data.get("updated_at") or created_at)
    schema_version = int(data.get("schema_version") or _SETTINGS_PROFILE_SCHEMA_VERSION)
    name = str(
        data.get("name")
        or (_SYSTEM_SETTINGS_PROFILE_NAME if kind == "system" else profile_id)
    )
    return SettingsProfileRecord(
        id=profile_id,
        kind=kind,
        name=name,
        settings=settings,
        modules=modules,
        created_at=created_at,
        updated_at=updated_at,
        schema_version=schema_version,
    )


def _canonical_system_settings_profile(existing: Optional[SettingsProfileRecord] = None) -> SettingsProfileRecord:
    timestamp = _utc_now_iso()
    return SettingsProfileRecord(
        id=_SYSTEM_SETTINGS_PROFILE_ID,
        kind="system",
        name=_SYSTEM_SETTINGS_PROFILE_NAME,
        settings=_normalize_settings_profile_settings({}),
        modules=_normalize_settings_profile_modules({}),
        created_at=existing.created_at if existing else timestamp,
        updated_at=timestamp,
    )


def _save_settings_profile_record(record: SettingsProfileRecord) -> SettingsProfileRecord:
    _write_json_atomic(_settings_profile_path(record.id), record.to_dict())
    return record


def _ensure_system_settings_profile() -> SettingsProfileRecord:
    _SETTINGS_PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    path = _settings_profile_path(_SYSTEM_SETTINGS_PROFILE_ID)
    existing = None
    if path.exists():
        try:
            existing = _load_settings_profile_record(path, kind_hint="system")
        except Exception as exc:
            logger.warning("System settings profile invalid; regenerating: %s", exc)
            existing = None
    canonical = _canonical_system_settings_profile(existing)
    if existing and (
        existing.id == canonical.id
        and existing.kind == canonical.kind
        and existing.name == canonical.name
        and existing.settings == canonical.settings
        and existing.modules == canonical.modules
        and existing.schema_version == canonical.schema_version
    ):
        return existing
    return _save_settings_profile_record(canonical)


def _restore_system_settings_profile() -> SettingsProfileRecord:
    existing = None
    path = _settings_profile_path(_SYSTEM_SETTINGS_PROFILE_ID)
    if path.exists():
        try:
            existing = _load_settings_profile_record(path, kind_hint="system")
        except Exception:
            existing = None
    return _save_settings_profile_record(_canonical_system_settings_profile(existing))


def _load_settings_profile_state() -> dict:
    path = _settings_profile_state_path()
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            logger.warning("Settings profile state invalid; resetting: %s", exc)
            data = {}
    else:
        data = {}
    return {
        "schema_version": _SETTINGS_PROFILE_SCHEMA_VERSION,
        "user_default_profile_id": data.get("user_default_profile_id") or _SYSTEM_SETTINGS_PROFILE_ID,
    }


def _save_settings_profile_state(state: dict) -> dict:
    payload = {
        "schema_version": _SETTINGS_PROFILE_SCHEMA_VERSION,
        "user_default_profile_id": state.get("user_default_profile_id") or _SYSTEM_SETTINGS_PROFILE_ID,
    }
    _write_json_atomic(_settings_profile_state_path(), payload)
    return payload


def _load_all_settings_profiles() -> List[SettingsProfileRecord]:
    system_profile = _ensure_system_settings_profile()
    named_profiles_by_id: Dict[str, SettingsProfileRecord] = {}
    named_profile_sources: Dict[str, Path] = {}
    for path in _settings_profile_named_paths():
        try:
            record = _load_settings_profile_record(path, kind_hint="named")
        except Exception as exc:
            logger.warning("Skipping invalid settings profile %s: %s", path.name, exc)
            continue
        existing_source = named_profile_sources.get(record.id)
        if existing_source is not None:
            logger.warning(
                "Skipping duplicate settings profile id %s from %s; keeping %s",
                record.id,
                _settings_profile_relative_path(path),
                _settings_profile_relative_path(existing_source),
            )
            continue
        named_profile_sources[record.id] = path
        named_profiles_by_id[record.id] = record
    named_profiles = list(named_profiles_by_id.values())
    named_profiles.sort(key=lambda record: record.name.casefold())
    return [system_profile, *named_profiles]


def _ensure_settings_profile_store() -> dict:
    _SETTINGS_PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    _ensure_system_settings_profile()
    if not _settings_profile_state_path().exists() and not _settings_profile_named_paths():
        _save_settings_profile_state({})
    state = _load_settings_profile_state()
    profiles = _load_all_settings_profiles()
    valid_ids = {profile.id for profile in profiles}
    if state["user_default_profile_id"] not in valid_ids:
        state["user_default_profile_id"] = _SYSTEM_SETTINGS_PROFILE_ID
        state = _save_settings_profile_state(state)
    return {"profiles": profiles, "state": state}


def _settings_profile_name_error(
    name: str,
    profiles: List[SettingsProfileRecord],
    current_profile_id: Optional[str] = None,
) -> Optional[str]:
    if not isinstance(name, str):
        return "Settings profile name must be text"
    if not name.strip():
        return "Settings profile name is required"
    if name != name.strip():
        return "Settings profile name cannot start or end with whitespace"
    if name.endswith(".") or name.endswith(" "):
        return "Settings profile name cannot end with a period or space"
    if any(ch in _SETTINGS_PROFILE_FORBIDDEN_NAME_CHARS or ord(ch) < 32 for ch in name):
        return "Settings profile name contains unsupported characters"
    if name.casefold() == _SYSTEM_SETTINGS_PROFILE_NAME.casefold():
        return f'"{_SYSTEM_SETTINGS_PROFILE_NAME}" is reserved for the system default profile'
    for profile in profiles:
        if profile.id == current_profile_id:
            continue
        if profile.name.casefold() == name.casefold():
            return f'A settings profile named "{name}" already exists'
    return None


def _serialize_settings_profile_record(record: SettingsProfileRecord, user_default_profile_id: str) -> dict:
    data = record.to_dict()
    data["settings"] = _with_canonical_pitch_egress(data["settings"])
    data["is_system_default"] = record.id == _SYSTEM_SETTINGS_PROFILE_ID
    data["is_user_default"] = record.id == user_default_profile_id
    return data


def _settings_profiles_response() -> dict:
    store = _ensure_settings_profile_store()
    state = store["state"]
    return {
        "profiles": [
            _serialize_settings_profile_record(profile, state["user_default_profile_id"])
            for profile in store["profiles"]
        ],
        "user_default_profile_id": state["user_default_profile_id"],
        "system_profile_id": _SYSTEM_SETTINGS_PROFILE_ID,
    }


def _find_settings_profile(profile_id: str) -> SettingsProfileRecord:
    store = _ensure_settings_profile_store()
    for profile in store["profiles"]:
        if profile.id == profile_id:
            return profile
    raise HTTPException(404, f"Unknown settings profile: {profile_id}")


@app.get("/api/settings-profiles")
def get_settings_profiles() -> dict:
    return _settings_profiles_response()


@app.post("/api/settings-profiles")
def create_settings_profile(payload: SettingsProfilePayload) -> dict:
    store = _ensure_settings_profile_store()
    profiles = store["profiles"]
    error = _settings_profile_name_error(payload.name, profiles)
    if error:
        raise HTTPException(400, error)

    timestamp = _utc_now_iso()
    try:
        raw_settings = normalize_resolution_schema(dict(payload.settings or {}))
    except (ResolutionSchemaConflictError, ResolutionSchemaLegacyFieldError) as exc:
        raise _translate_resolution_schema_error(exc) from exc
    record = SettingsProfileRecord(
        id=_generate_settings_profile_id(),
        kind="named",
        name=payload.name,
        settings=_normalize_settings_profile_settings(raw_settings),
        modules=_normalize_settings_profile_modules(dict(payload.modules or {})),
        created_at=timestamp,
        updated_at=timestamp,
    )
    _save_settings_profile_record(record)
    logger.info("Settings profile created: %s", record.name)
    return {"ok": True, **_settings_profiles_response()}


@app.put("/api/settings-profiles/user-default")
def set_user_default_settings_profile(payload: dict) -> dict:
    profile_id = payload.get("profile_id")
    if not profile_id:
        raise HTTPException(400, "profile_id required")
    _ = _find_settings_profile(profile_id)
    _save_settings_profile_state({"user_default_profile_id": profile_id})
    logger.info("Settings profile user default set: %s", profile_id)
    return {"ok": True, **_settings_profiles_response()}


@app.post("/api/settings-profiles/restore-system")
def restore_system_settings_profile() -> dict:
    _restore_system_settings_profile()
    logger.info("System settings profile restored")
    return {"ok": True, **_settings_profiles_response()}


@app.put("/api/settings-profiles/{profile_id}")
def update_settings_profile(profile_id: str, payload: SettingsProfilePayload) -> dict:
    existing = _find_settings_profile(profile_id)
    if existing.kind != "named":
        raise HTTPException(400, "The system default profile cannot be overwritten")

    profiles = _ensure_settings_profile_store()["profiles"]
    error = _settings_profile_name_error(payload.name, profiles, current_profile_id=profile_id)
    if error:
        raise HTTPException(400, error)

    try:
        raw_settings = normalize_resolution_schema(dict(payload.settings or {}))
    except (ResolutionSchemaConflictError, ResolutionSchemaLegacyFieldError) as exc:
        raise _translate_resolution_schema_error(exc) from exc
    record = SettingsProfileRecord(
        id=existing.id,
        kind="named",
        name=payload.name,
        settings=_normalize_settings_profile_settings(raw_settings),
        modules=_normalize_settings_profile_modules(dict(payload.modules or {})),
        created_at=existing.created_at,
        updated_at=_utc_now_iso(),
    )
    _save_settings_profile_record(record)
    logger.info("Settings profile updated: %s", record.name)
    return {"ok": True, **_settings_profiles_response()}


@app.delete("/api/settings-profiles/{profile_id}")
def delete_settings_profile(profile_id: str) -> dict:
    existing = _find_settings_profile(profile_id)
    if existing.kind != "named":
        raise HTTPException(400, "The system default profile cannot be deleted")

    paths = _settings_profile_paths_for_id(profile_id)
    fallback_path = _settings_profile_path(profile_id)
    if fallback_path.exists() and fallback_path not in paths:
        paths.append(fallback_path)
    for path in paths:
        path.unlink()

    state = _load_settings_profile_state()
    if state["user_default_profile_id"] == profile_id:
        _save_settings_profile_state({"user_default_profile_id": _SYSTEM_SETTINGS_PROFILE_ID})
    logger.info("Settings profile deleted: %s", existing.name)
    return {"ok": True, **_settings_profiles_response()}


# ── Saved Palettes ────────────────────────────────────────────────────────

_PALETTES_PATH = _GENERATOR_DATA_DIR / "palettes.json"

_DEFAULT_PALETTES: Dict[str, Any] = {"palettes": []}


def _load_palettes() -> dict:
    if _PALETTES_PATH.exists():
        with open(_PALETTES_PATH, encoding="utf-8") as f:
            return json.load(f)
    return dict(_DEFAULT_PALETTES)


def _save_palettes(data: dict) -> None:
    _PALETTES_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _PALETTES_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    tmp.replace(_PALETTES_PATH)


@app.get("/api/palettes")
def get_palettes() -> dict:
    return _load_palettes()


@app.put("/api/palettes")
def save_palettes_endpoint(payload: dict) -> dict:
    _save_palettes(payload)
    logger.info("Palettes saved: %d palette(s)", len(payload.get("palettes", [])))
    return {"ok": True}


# ── Palette ───────────────────────────────────────────────────────────────

@app.post("/api/palette/validate")
def validate_palette_endpoint(payload: PaletteValidatePayload) -> dict:
    """Pre-check a palette: profiles present AND no excluded filaments.

    `missing` = no profile on disk; `unavailable` = flagged exclude_from_model
    (a stale profile may still exist on disk, but it must not be used for a new
    solve). The solve path itself re-validates via pipeline_cli.validate_palette.
    """
    _require_model_library()
    from filament_policy import unavailable_for_generation
    registry = _load_registry()
    missing = [
        fid for fid in payload.palette
        if not _runtime_profile_exists(fid)
    ]
    unavailable = unavailable_for_generation(payload.palette, registry)
    return {
        "valid": (len(missing) == 0 and len(unavailable) == 0),
        "missing": missing,
        "unavailable": unavailable,
    }


# ── Palette Suggestion ────────────────────────────────────────────────────

class PaletteSuggestPayload(BaseModel):
    # Unknown fields are rejected loudly (422). In particular the removed
    # quality-mode fields (search_mode, quality_weights) must not be silently
    # accepted from stale clients — suggestion is always thorough now.
    model_config = ConfigDict(extra="forbid")

    image_path: str
    n_filaments: int = 7
    top_k: int = 5
    filament_ids: Optional[List[str]] = None
    max_swaps: Optional[int] = None  # if set, use swap-tier sweep
    palette_mode: str = "standard"
    improvement_threshold: Optional[float] = None
    force_all_tiers: Optional[bool] = None

    @field_validator("palette_mode", mode="before")
    @classmethod
    def _normalize_palette_mode_field(cls, value: Any) -> str:
        raw = str(value or "standard").strip().lower()
        if raw in {"standard", "source", "source_color", "source-color", "source_color"}:
            return "standard"
        if raw in {"luminance", "luminance_detail", "luminance-detail", "detail"}:
            return "luminance_detail"
        raise ValueError(
            "Unsupported palette_mode: "
            f"{value!r} (valid: 'standard', 'luminance_detail')"
        )


class LuminanceBaseShadingLimitRecommendPayload(BaseModel):
    image_path: Optional[str] = None


def _recommend_luminance_base_shading_limit_fraction(img: np.ndarray) -> dict:
    """Return a loose image-derived starting point for base shading limit."""
    h, w = img.shape[:2]
    ok = to_oklab(srgb_to_linear(img).reshape(h * w, 3)).reshape(h, w, 3)
    luma = np.asarray(ok[:, :, 0], dtype=np.float32)
    chroma = np.linalg.norm(ok[:, :, 1:3], axis=2).astype(np.float32)

    dy = np.diff(luma, axis=0)
    dx = np.diff(luma, axis=1)
    grad_mean = (
        float(np.mean(np.abs(dy))) + float(np.mean(np.abs(dx)))
    ) * 0.5
    l_std = float(np.std(luma))
    l_range = float(np.percentile(luma, 95.0) - np.percentile(luma, 5.0))
    chroma_p90 = float(np.percentile(chroma, 90.0))
    chroma_active = float(np.mean(chroma > np.float32(max(chroma_p90 * 0.35, 1e-6))))
    edge_to_tone = grad_mean / max(l_std, 1e-6)

    fraction = 0.75
    if l_range >= 0.55:
        fraction += 0.10
    elif l_range <= 0.30:
        fraction -= 0.05
    if l_std >= 0.18:
        fraction += 0.05
    if edge_to_tone >= 0.32:
        fraction -= 0.10
    elif edge_to_tone <= 0.18:
        fraction += 0.05
    if chroma_active >= 0.70 and l_range < 0.45:
        fraction -= 0.05

    recommended = round(float(np.clip(fraction, 0.60, 0.90)), 2)
    return {
        "recommended_base_shading_limit_fraction": recommended,
        "recommended_authority_fraction": recommended,
        "metrics": {
            "luminance_std": round(l_std, 4),
            "luminance_range_p95_p05": round(l_range, 4),
            "luminance_gradient_mean": round(grad_mean, 4),
            "edge_to_tone_ratio": round(edge_to_tone, 4),
            "chroma_active_fraction": round(chroma_active, 4),
        },
    }


def _format_candidate_response(
    candidates,
    *,
    palette_mode: str = "standard",
    signature_stats: Optional[dict] = None,
    model_metadata: Optional[dict] = None,
) -> dict:
    result = [_format_palette_candidate(cand) for cand in candidates]
    response = {"candidates": result, "palette_mode": palette_mode}
    if signature_stats:
        response["signature_stats"] = signature_stats
    if model_metadata:
        response["model_metadata"] = model_metadata
    return response


def _format_palette_candidate(cand) -> dict:
    d = {
        "filament_ids": cand.filament_ids,
        "mean_de": round(cand.mean_de, 4),
        "suggestion_mean_de": round(cand.mean_de, 4),
        "max_de": round(cand.max_de, 4),
        "coverage_pct": round(100.0 - cand.pct_above_threshold, 1),
    }
    if getattr(cand, "p90_de", None) is not None:
        d["p90_de"] = round(cand.p90_de, 4)
    if getattr(cand, "rank_score", None) is not None:
        d["rank_score"] = round(cand.rank_score, 4)
        d["rank_mode"] = cand.rank_mode or "mean"
    return d


def _format_tier_response(
    sweep,
    *,
    palette_mode: str = "standard",
    signature_stats: Optional[dict] = None,
    model_metadata: Optional[dict] = None,
) -> dict:
    tiers = getattr(sweep, "tiers", sweep)
    tier_results = []
    for tier in tiers:
        tier_cands = [_format_palette_candidate(cand) for cand in tier.candidates]
        tier_results.append({
            "swap_count": tier.swap_count,
            "n_filaments": tier.n_filaments,
            "best_mean_de": round(tier.best_mean_de, 4),
            "best_coverage_pct": round(tier.best_coverage_pct, 1),
            "improvement": round(tier.improvement_over_prev, 1)
                if tier.improvement_over_prev is not None else None,
            "candidates": tier_cands,
        })
    response = {"tiers": tier_results, "palette_mode": palette_mode}
    if hasattr(sweep, "alternatives"):
        response["alternatives"] = [
            _format_palette_candidate(cand)
            for cand in getattr(sweep, "alternatives", [])
        ]
    if hasattr(sweep, "recommended"):
        response["recommended"] = getattr(sweep, "recommended", None)
    per_load_capped = getattr(sweep, "per_load_capped", None)
    if per_load_capped:
        response["per_load_capped"] = per_load_capped
    if signature_stats:
        response["signature_stats"] = signature_stats
    if model_metadata:
        response["model_metadata"] = model_metadata
    return response


def _palette_suggestion_ams_capacity(snapshot: dict) -> tuple[int, int]:
    """Resolve palette-suggestion AMS capacity from active printer state."""
    printer = (get_active_printer() or {}).get("printer") or {}
    snapshot_units = max(1, int(snapshot.get("n_ams_units", 1) or 1))
    snapshot_total_slots = max(1, int(snapshot.get("ams_slots", 4) or 4))
    n_ams_units = max(1, int(printer.get("ams_units") or snapshot_units))
    if printer.get("slots_per_ams") is not None:
        slots_per_ams = max(1, int(printer["slots_per_ams"]))
    else:
        slots_per_ams = max(1, snapshot_total_slots // snapshot_units)
    return slots_per_ams, n_ams_units


_PALETTE_GAMUT_SAMPLING = {
    "single_cap_step": 4,
    "pair_cap_step": 8,
    "pair_samples": 10,
    "triple_cap_step": 8,
    "triple_samples": 4,
}


def _palette_artifact_identity(path: Path) -> tuple[str, int | None, int | None]:
    """Cheap process-cache identity for an immutable published artifact."""

    resolved = path.expanduser().resolve()
    try:
        stat = resolved.stat()
    except OSError:
        return str(resolved), None, None
    return str(resolved), int(stat.st_size), int(stat.st_mtime_ns)


def _palette_backend_cache_key(
    *,
    bundle_path: str | Path,
    use_corrections: bool,
    white_base: str,
    white_cap: str,
    d_wb: float,
    d_wc_min: float,
    d_wc_max: float,
    t_max: float,
    layer_height: float,
    max_layers: int,
) -> tuple[Any, ...]:
    source = Path(bundle_path).expanduser().resolve()
    runtime_path = source / "runtime_bundle.json" if source.is_dir() else source
    correction_path = runtime_path.parent / "correction_layer.json"
    return (
        "photo-stack-palette-backend-v1",
        _palette_artifact_identity(runtime_path),
        _palette_artifact_identity(correction_path) if use_corrections else None,
        bool(use_corrections),
        str(white_base),
        str(white_cap),
        float(d_wb).hex(),
        float(d_wc_min).hex(),
        float(d_wc_max).hex(),
        float(t_max).hex(),
        float(layer_height).hex(),
        int(max_layers),
        tuple(sorted(_PALETTE_GAMUT_SAMPLING.items())),
    )


def _get_cached_palette_backend(key: tuple[Any, ...]) -> object | None:
    with _PALETTE_BACKEND_CACHE_LOCK:
        backend = _PALETTE_BACKEND_CACHE.get(key)
        if backend is not None:
            _PALETTE_BACKEND_CACHE.move_to_end(key)
        return backend


def _publish_cached_palette_backend(key: tuple[Any, ...], candidate: object) -> object:
    """Publish one complete backend, preferring an existing concurrent winner."""

    with _PALETTE_BACKEND_CACHE_LOCK:
        existing = _PALETTE_BACKEND_CACHE.get(key)
        if existing is not None:
            _PALETTE_BACKEND_CACHE.move_to_end(key)
            return existing
        _PALETTE_BACKEND_CACHE[key] = candidate
        while len(_PALETTE_BACKEND_CACHE) > _PALETTE_BACKEND_CACHE_MAX_SIZE:
            _PALETTE_BACKEND_CACHE.popitem(last=False)
        return candidate


def _clear_palette_backend_cache() -> int:
    with _PALETTE_BACKEND_CACHE_LOCK:
        count = len(_PALETTE_BACKEND_CACHE)
        _PALETTE_BACKEND_CACHE.clear()
        return count


def _build_palette_suggestion_model(
    snapshot: dict,
) -> tuple[object | None, dict, dict]:
    """Resolve the appearance backend used to estimate palette gamuts."""
    provider_name = str(
        snapshot.get(
            "appearance_model_provider",
            _DEFAULT_CONFIG["appearance_model_provider"],
        )
        or _DEFAULT_CONFIG["appearance_model_provider"]
    )
    solve_cfg = _build_solve_config(snapshot)
    height_budget = max(float(solve_cfg.d_wc_min), float(solve_cfg.t_max) - float(solve_cfg.d_wb))
    max_layers = int(solve_cfg.effective_max_layers())
    if not _is_photo_stack_provider(provider_name):
        return None, {
            "appearance_model_provider": provider_name,
            "gamut_backend": "historical_spline_profile",
            "gamut_domain": "model_oklab",
            "corrections_enabled": bool(snapshot.get("use_corrections", False)),
            "height_budget_without_base_mm": round(height_budget, 6),
            "layer_height_mm": round(float(solve_cfg.layer_height), 6),
            "max_layers": max_layers,
            "palette_ranking_mode": "mean",
        }, {
            "d_wc_min": float(solve_cfg.d_wc_min),
            "layer_height": float(solve_cfg.layer_height),
            "max_layers": max_layers,
            "t_max": height_budget,
        }

    bundle_path = solve_cfg.photo_stack_bundle_path
    if bundle_path is None:
        raise HTTPException(
            status_code=400,
            detail="the active model library has no usable Photo Stack deployment bundle",
        )

    use_corrections = bool(snapshot.get("use_corrections", False))
    cap_budget = float(solve_cfg.effective_boundary_d_wc_max())
    cache_key = _palette_backend_cache_key(
        bundle_path=bundle_path,
        use_corrections=use_corrections,
        white_base=solve_cfg.white_base,
        white_cap=solve_cfg.effective_white_cap(),
        d_wb=float(solve_cfg.d_wb),
        d_wc_min=float(solve_cfg.d_wc_min),
        d_wc_max=cap_budget,
        t_max=height_budget,
        layer_height=float(solve_cfg.layer_height),
        max_layers=max_layers,
    )
    backend = _get_cached_palette_backend(cache_key)
    if backend is None:
        from appearance_model import PhotoStackBundleAppearanceProvider
        from palette.suggest import PhotoStackPaletteGamutBackend

        provider = PhotoStackBundleAppearanceProvider(
            bundle_path=bundle_path,
            use_corrections=use_corrections,
        )
        candidate = PhotoStackPaletteGamutBackend(
            provider,
            white_base=solve_cfg.white_base,
            white_cap=solve_cfg.effective_white_cap(),
            d_wb=float(solve_cfg.d_wb),
            d_wc_min=float(solve_cfg.d_wc_min),
            d_wc_max=cap_budget,
            t_max=height_budget,
            layer_height=float(solve_cfg.layer_height),
            max_layers=max_layers,
            **_PALETTE_GAMUT_SAMPLING,
        )
        backend = _publish_cached_palette_backend(cache_key, candidate)
    metadata = backend.metadata()
    metadata.update({
        "photo_stack_candidate_run_id": Path(bundle_path).parent.name,
        "height_budget_without_base_mm": round(height_budget, 6),
        "d_wc_max_mm": round(cap_budget, 6),
        "layer_height_mm": round(float(solve_cfg.layer_height), 6),
        "max_layers": max_layers,
        "palette_ranking_mode": "robust",
    })
    kwargs = {
        "gamut_backend": backend,
        "d_wc_min": float(solve_cfg.d_wc_min),
        "layer_height": float(solve_cfg.layer_height),
        "max_layers": max_layers,
        "t_max": height_budget,
        "ranking_mode": "robust",
    }
    return backend, metadata, kwargs


@app.post("/api/luminance/base-shading-limit/recommend")
def recommend_luminance_base_shading_limit(
    payload: LuminanceBaseShadingLimitRecommendPayload,
) -> dict:
    _require_model_library()
    cfg = _cfg()
    image_name = payload.image_path or cfg.get("image_path")
    if not image_name:
        raise HTTPException(400, "No image selected")
    image_path = _IMAGES_DIR / str(image_name)
    if not image_path.exists():
        raise HTTPException(404, f"Image not found: {image_name}")
    img = _load_run_source_image(image_path, cfg, max_dim_mm=80.0)
    result = _recommend_luminance_base_shading_limit_fraction(img)
    return {
        "image_path": str(image_name),
        **result,
    }


def _suggest_cancel_requested(job_id: str) -> bool:
    with _MODEL_RESOURCE_COORDINATION_LOCK:
        suggest = session["suggest"]
        return bool(
            str(suggest.get("job_id") or "") != job_id
            or suggest.get("cancel_requested")
        )


def _update_suggest_job(job_id: str, **updates: Any) -> bool:
    with _MODEL_RESOURCE_COORDINATION_LOCK:
        suggest = session["suggest"]
        if str(suggest.get("job_id") or "") != job_id:
            return False
        suggest.update(updates)
        return True


def _complete_suggest_job(job_id: str, *, result: dict[str, Any], elapsed_s: float) -> bool:
    with _MODEL_RESOURCE_COORDINATION_LOCK:
        suggest = session["suggest"]
        if str(suggest.get("job_id") or "") != job_id:
            return False
        if suggest.get("cancel_requested"):
            suggest["result"] = result
            return False
        suggest.update({
            "status": "complete",
            "result": result,
            "elapsed_s": elapsed_s,
            "progress": {"stage_label": "Complete", "stage_pct": 100},
        })
        return True


@app.post("/api/palette/suggest")
def suggest_palettes_endpoint(payload: PaletteSuggestPayload) -> dict:
    """Auto-suggest optimal palettes for the given image."""
    _require_model_library()
    image_path = _IMAGES_DIR / payload.image_path
    if not image_path.exists():
        raise HTTPException(404, f"Image not found: {payload.image_path}")

    cfg = _cfg()

    # Palette suggestion is always thorough now. The old fast/quality switches
    # were only user-facing complexity; thorough is fast enough for this app.
    suggest = session["suggest"]
    if suggest["status"] == "running":
        raise HTTPException(409, "Suggestion already running")

    snapshot = dict(cfg)

    # The configured white base/cap are fixed inputs the suggester cannot drop
    # (unlike color candidates) — refuse up front if either is excluded from
    # model-backed generation, before any profile is loaded.
    from filament_policy import unavailable_for_generation
    _unavailable_whites = unavailable_for_generation(_white_ids(snapshot), _load_registry())
    if _unavailable_whites:
        raise HTTPException(
            400,
            "White base/cap excluded from model-backed generation "
            f"(exclude_from_model): {_unavailable_whites}. Choose a different "
            "white before suggesting.",
        )

    job_id = uuid.uuid4().hex
    _reserve_model_job(
        "suggest",
        already_running="Suggestion already running",
        state={
            "status": "running",
            "progress": {"stage_label": "Starting..."},
            "elapsed_s": 0.0,
            "result": None,
            "cancel_requested": False,
            "job_id": job_id,
        },
    )

    def _run_suggest():
        with _suggest_lock:
            start = time.time()

            def _check_cancel() -> None:
                if _suggest_cancel_requested(job_id):
                    raise SolveCancelled()

            last_progress_fraction = 0.0

            def _report_progress(msg: str, fraction: float) -> None:
                nonlocal last_progress_fraction
                _check_cancel()
                normalized = float(np.clip(fraction, 0.0, 0.99))
                normalized = max(last_progress_fraction, normalized)
                last_progress_fraction = normalized
                _update_suggest_job(
                    job_id,
                    elapsed_s=round(time.time() - start, 2),
                    progress={
                        "stage_label": str(msg),
                        "stage_pct": round(normalized * 100),
                    },
                )

            def _progress(msg, frac):
                child_fraction = float(np.clip(frac, 0.0, 1.0))
                _report_progress(msg, 0.10 + 0.89 * child_fraction)

            def _cancel():
                return _suggest_cancel_requested(job_id)

            try:
                _check_cancel()
                _report_progress("Loading appearance model", 0.02)
                from palette.suggest import suggest_palettes as _suggest_palettes
                from palette.suggest import suggest_palettes_swap_aware
                from palette.suggest import SUGGESTION_COVERAGE_DE_THRESHOLD
                from palette.suggest import extract_color_signature_from_oklab
                from palette.suggest import extract_luminance_residual_signature
                from palette.suggest import solve_target_oklab_for_signature
                from filament_policy import excluded_filament_ids

                wb_profile = _load_profile_sandbox(_white_base(snapshot))
                wc_profile = _load_profile_sandbox(_white_cap(snapshot))
                gamut_backend, model_metadata, model_kwargs = _build_palette_suggestion_model(snapshot)
                _report_progress("Loading source image", 0.04)
                suggest_img = _load_run_source_image(image_path, snapshot, max_dim_mm=80.0)
                _check_cancel()
                palette_mode = str(payload.palette_mode or "standard")
                solve_cfg = _build_solve_config(snapshot)
                white_rescale_provider = getattr(gamut_backend, "provider", None)
                if white_rescale_provider is None:
                    from appearance_model import create_appearance_provider

                    white_rescale_provider = create_appearance_provider(
                        provider_kind=solve_cfg.appearance_model_provider,
                        color_profiles={},
                        wb_profile=wb_profile,
                        wc_profile=wc_profile,
                        photo_stack_bundle_path=solve_cfg.photo_stack_bundle_path,
                        use_corrections=bool(snapshot.get("use_corrections", False)),
                    )
                    if hasattr(white_rescale_provider, "fingerprint"):
                        model_metadata["provider_fingerprint"] = white_rescale_provider.fingerprint()
                _report_progress("Preparing image target", 0.06)
                target_oklab, target_stats = solve_target_oklab_for_signature(
                    suggest_img,
                    wb_profile=wb_profile,
                    config=solve_cfg,
                    white_rescale_provider=white_rescale_provider,
                )
                _check_cancel()
                chroma_weight = max(float(snapshot.get("chroma_weight", 1.0) or 1.0), 1e-9)
                gamut_luminance_weight = 1.0 / chroma_weight
                _report_progress("Extracting color signature", 0.08)
                if palette_mode == "luminance_detail":
                    detail_l_weight = 0.15
                    gamut_luminance_weight = detail_l_weight / chroma_weight
                    sig, luminance_stats = extract_luminance_residual_signature(
                        target_oklab=target_oklab,
                        domain=target_stats["signature_domain"],
                        n_clusters=120,
                        luminance_weight=detail_l_weight,
                        chroma_weight_power=1.0,
                    )
                    signature_stats = {
                        **target_stats,
                        **luminance_stats,
                        "signature_domain": sig.domain,
                        "model_domain_ingress": bool(solve_cfg.model_domain_ingress),
                        "gamut_white_rescale": bool(solve_cfg.gamut_white_rescale),
                        "metric_luminance_weight": float(gamut_luminance_weight),
                    }
                else:
                    sig = extract_color_signature_from_oklab(
                        target_oklab,
                        n_clusters=100,
                        domain=target_stats["signature_domain"],
                        n_pixels=target_oklab.shape[0],
                    )
                    signature_stats = {
                        **target_stats,
                        "signature_domain": sig.domain,
                        "metric_luminance_weight": float(gamut_luminance_weight),
                    }
                _check_cancel()
                # Domain + metric provenance. signature_domain and
                # metric_luminance_weight fully describe the scoring space;
                # the old nearest_color_space field carried false semantics
                # and is deliberately gone.
                model_metadata.update({
                    "signature_domain": sig.domain,
                    "model_domain_ingress": bool(solve_cfg.model_domain_ingress),
                    "model_domain_ingress_lut_path": str(solve_cfg.model_domain_ingress_lut_path),
                    "metric_luminance_weight": float(gamut_luminance_weight),
                })
                _report_progress("Computing palette gamuts", 0.10)

                common_kwargs = dict(
                    progress=_progress,
                    cancel=_cancel,
                    wb_profile=wb_profile,
                    wc_profile=wc_profile,
                    d_wb=snapshot["d_wb"],
                    de_threshold=SUGGESTION_COVERAGE_DE_THRESHOLD,
                    profiles_dir=_runtime_profiles_dir(),
                    filament_ids=payload.filament_ids,
                    # Contract bridge: never suggest filaments excluded from
                    # model-backed generation (default pool or user-supplied).
                    exclude_filament_ids=excluded_filament_ids(_load_registry()),
                    gamut_luminance_weight=gamut_luminance_weight,
                )
                common_kwargs.update(model_kwargs)

                if payload.max_swaps is not None:
                    slots_per_ams, n_ams_units = _palette_suggestion_ams_capacity(snapshot)
                    sweep = suggest_palettes_swap_aware(
                        sig,
                        max_colors_per_load=payload.n_filaments,
                        slots_per_ams=slots_per_ams,
                        n_ams_units=n_ams_units,
                        reserved_white=snapshot.get("white_slots", 1),
                        max_swaps=payload.max_swaps,
                        top_k=payload.top_k,
                        improvement_threshold=payload.improvement_threshold or snapshot.get("swap_improvement_threshold", 2.0),
                        force_all_tiers=payload.force_all_tiers if payload.force_all_tiers is not None else snapshot.get("force_all_tiers", False),
                        **common_kwargs,
                    )
                    model_metadata.update(getattr(sweep, "model_metadata", {}) or {})
                    result = _format_tier_response(
                        sweep,
                        palette_mode=palette_mode,
                        signature_stats=signature_stats,
                        model_metadata=model_metadata,
                    )
                else:
                    candidates = _suggest_palettes(
                        sig,
                        n_filaments=payload.n_filaments,
                        top_k=payload.top_k,
                        **common_kwargs,
                    )
                    result = _format_candidate_response(
                        candidates,
                        palette_mode=palette_mode,
                        signature_stats=signature_stats,
                        model_metadata=model_metadata,
                    )

                if not _complete_suggest_job(
                    job_id,
                    result=result,
                    elapsed_s=round(time.time() - start, 2),
                ):
                    raise SolveCancelled()

            except SolveCancelled:
                _update_suggest_job(
                    job_id,
                    status="cancelled",
                    progress={"stage_label": "Cancelled"},
                )
                logger.info("Suggest cancelled by user after %.1fs", time.time() - start)
            except Exception as exc:
                logger.exception("Suggest failed")
                _update_suggest_job(
                    job_id,
                    status="error",
                    progress={"stage_label": str(exc)},
                )
            finally:
                _update_suggest_job(job_id, elapsed_s=round(time.time() - start, 2))

    thread = threading.Thread(target=_run_suggest, daemon=True)
    try:
        thread.start()
    except Exception as exc:
        _update_suggest_job(
            job_id,
            status="error",
            progress={"stage_label": f"Could not start suggestion worker: {exc}"},
        )
        raise HTTPException(500, f"Could not start suggestion worker: {exc}") from exc
    return {"status": "started", "job_id": job_id}


@app.get("/api/palette/suggest/status")
def suggest_status() -> dict:
    """Poll suggest progress."""
    with _MODEL_RESOURCE_COORDINATION_LOCK:
        suggest = dict(session["suggest"])
    prog = suggest["progress"]
    return {
        "status": suggest["status"],
        "progress": prog.get("stage_label", "") if isinstance(prog, dict) else str(prog),
        "progress_detail": prog if isinstance(prog, dict) else {},
        "elapsed_s": round(suggest["elapsed_s"], 2),
        "result": suggest["result"],
        "cancel_requested": bool(suggest.get("cancel_requested")),
        "job_id": suggest.get("job_id"),
    }


@app.post("/api/palette/suggest/cancel")
def cancel_suggest(job_id: str | None = None) -> dict:
    """Request cancellation of a running suggest."""
    with _MODEL_RESOURCE_COORDINATION_LOCK:
        suggest = session["suggest"]
        active_job_id = str(suggest.get("job_id") or "")
        if job_id and job_id != active_job_id:
            raise HTTPException(409, "Suggestion job no longer matches the active job")
        if suggest["status"] != "running":
            return {
                "cancelled": False,
                "reason": "not running",
                "job_id": active_job_id or None,
            }
        suggest["cancel_requested"] = True
        return {
            "cancelled": True,
            "cancel_requested": True,
            "job_id": active_job_id or None,
        }


# ── Gamut Preview ─────────────────────────────────────────────────────────

class GamutPreviewPayload(BaseModel):
    palette: Optional[List[str]] = None  # override session palette if provided


@app.post("/api/gamut-preview")
def gamut_preview(payload: GamutPreviewPayload = GamutPreviewPayload()) -> dict:
    """
    Quick gamut check.  Accepts an optional palette override so the frontend
    can check non-active deck palettes without mutating the session.
    """
    cfg = _cfg()
    palette = payload.palette if payload.palette else cfg["palette"]

    if not cfg["image_path"]:
        raise HTTPException(400, "No image selected")
    if not palette:
        raise HTTPException(400, "No palette selected")

    image_path = _IMAGES_DIR / cfg["image_path"]
    if not image_path.exists():
        raise HTTPException(404, f"Image not found: {cfg['image_path']}")

    out = _current_out_dir()

    try:
        # Load image at solve resolution
        img = _load_run_source_image(image_path, cfg)

        # Facade solve
        sc = _build_solve_config(cfg, palette_override=palette)
        result = solve_preview(img, sc, modules_path=_MODULES_PATH)
        st = result.stats

        # Save diagnostic images
        _save_de_map(result.de_map, out / "de_map_preview.png")
        gamut_overlay = _make_gamut_overlay(img, result.gamut_mask)
        Image.fromarray(gamut_overlay).save(str(out / "gamut_overlay.png"))
        Image.fromarray(img).save(str(out / "source_preview.png"))

        pred = result.predict_image()
        Image.fromarray(pred).save(str(out / "predicted_preview.png"))

        # Cache-bust so the browser fetches the fresh preview rather than a
        # same-named image cached from a prior gamut-preview run (files land in
        # the shared 'current' run-cache dir).
        _gcb = f"?t={int(time.time())}"
        return {
            "coverage_pct": st.coverage_pct,
            "n_out_of_gamut": st.n_out_of_gamut,
            "total_pixels": st.total_pixels,
            "mean_de": st.mean_de,
            "source_rms_de": getattr(st, "source_rms_de", st.mean_de),
            "max_de": st.max_de,
            "image_w": st.image_w,
            "image_h": st.image_h,
            "de_map_url": f"/api/run-cache/files/de_map_preview.png{_gcb}",
            "gamut_overlay_url": f"/api/run-cache/files/gamut_overlay.png{_gcb}",
            "source_url": f"/api/run-cache/files/source_preview.png{_gcb}",
            "predicted_url": f"/api/run-cache/files/predicted_preview.png{_gcb}",
        }

    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))
    except Exception as exc:
        logger.exception("Gamut preview failed")
        raise HTTPException(500, f"Gamut preview failed: {exc}")


# ── Palette Comparison ────────────────────────────────────────────────────

class ComparisonPayload(BaseModel):
    palettes: List[List[str]]  # list of palette filament ID lists


@app.post("/api/palette/compare")
def compare_palettes(payload: ComparisonPayload) -> dict:
    """Start a background compare job."""
    _require_model_library()
    cfg = _cfg()
    if not cfg["image_path"]:
        raise HTTPException(400, "No image selected")
    if not payload.palettes:
        raise HTTPException(400, "No palettes to compare")

    image_path = _IMAGES_DIR / cfg["image_path"]
    if not image_path.exists():
        raise HTTPException(404, f"Image not found: {cfg['image_path']}")

    compare = session["compare"]
    if compare["status"] == "running":
        raise HTTPException(409, "Compare already running")

    registry = load_filament_order_registry()
    palettes = [canonical_palette_order(p, registry) for p in payload.palettes]
    snapshot = dict(cfg)

    job_id = uuid.uuid4().hex

    # Set status synchronously so _assert_no_active_job() sees "running"
    # immediately after this handler returns (mirrors export_files_start).
    _reserve_model_job(
        "compare",
        already_running="Compare already running",
        state={
            "status": "running",
            "job_id": job_id,
            "progress": {},
            "elapsed_s": 0.0,
            "result": None,
            "cancel_requested": False,
        },
    )

    def _run_compare():
        logger.info("Compare thread started for %d palettes", len(palettes))
        with _compare_lock:
            compare["status"] = "running"
            compare["progress"] = {"stage_label": "Loading image..."}
            compare["result"] = None
            compare["cancel_requested"] = False
            start = time.time()
            out = _current_out_dir()

            try:
                img = _load_run_source_image(image_path, snapshot)
                H, W = img.shape[:2]
                Image.fromarray(img).save(str(out / "compare_source.png"))

                sc = _build_solve_config(snapshot)

                def _progress(msg):
                    if compare["cancel_requested"]:
                        raise SolveCancelled()
                    compare["elapsed_s"] = round(time.time() - start, 2)
                    if isinstance(msg, dict):
                        compare["progress"] = msg
                    else:
                        compare["progress"] = {"stage_label": msg}

                solve_results = solve_compare(
                    img,
                    palettes,
                    sc,
                    progress=_progress,
                    modules_path=_MODULES_PATH,
                )

                # Post-process (same as the old synchronous code)
                results = []
                palette_data = []

                for idx, sr in enumerate(solve_results):
                    _progress({
                        "stage_label": f"Preparing compare outputs {idx + 1}/{len(solve_results)}",
                        "stage_pct": round(100 * idx / max(len(solve_results), 1)),
                    })
                    if isinstance(sr, dict):
                        results.append(sr)
                        continue

                    prefix = f"compare_{idx}"
                    st = sr.stats
                    pred = sr.predict_image()
                    white_cap_scale_max = _white_cap_preview_scale_max(snapshot)
                    Image.fromarray(pred).save(str(out / f"{prefix}_predicted.png"))
                    _save_cap_height_map(
                        sr.cap_map,
                        out / f"{prefix}_cap.png",
                        max_mm=white_cap_scale_max,
                        zero_rgb=_ZERO_THICKNESS_RGB,
                    )
                    boundary_cap_stats = _save_white_cap_part_map(
                        sr.boundary_cap,
                        out / f"{prefix}_boundary_cap.png",
                        max_mm=white_cap_scale_max,
                    )
                    detail_cap_stats = _save_white_cap_part_map(
                        sr.detail_cap,
                        out / f"{prefix}_detail_cap.png",
                        max_mm=white_cap_scale_max,
                    )
                    _ceiling = _compute_color_ceiling(sr.thickness_maps, snapshot["d_wb"])
                    _surface = _compute_total_surface(
                        _ceiling,
                        sr.cap_map,
                    )
                    _boundary_surface = None
                    _boundary_cap_map = sr.boundary_cap
                    if _boundary_cap_map is not None:
                        _boundary_surface = _compute_cap_component_surface(
                            _ceiling,
                            _boundary_cap_map,
                        )
                    cap_surface_height_stats = _save_masked_white_cap_height_map(
                        _surface,
                        sr.cap_map,
                        out / f"{prefix}_cap_surface_height.png",
                        max_mm=snapshot["t_max"],
                    )
                    boundary_cap_surface_height_stats = _save_masked_white_cap_height_map(
                        _boundary_surface,
                        _boundary_cap_map,
                        out / f"{prefix}_boundary_cap_surface_height.png",
                        max_mm=snapshot["t_max"],
                    )
                    detail_cap_surface_height_stats = _save_masked_white_cap_height_map(
                        _surface,
                        sr.detail_cap,
                        out / f"{prefix}_detail_cap_surface_height.png",
                        max_mm=snapshot["t_max"],
                    )
                    _save_cap_height_map(_ceiling, out / f"{prefix}_color_ceiling.png",
                                         max_mm=snapshot["t_max"])
                    _save_cap_height_map(_surface, out / f"{prefix}_total_surface.png",
                                         max_mm=snapshot["t_max"])
                    _save_surface_blob(_surface, out / f"{prefix}_total_surface.bin")
                    _save_surface_blob(_ceiling, out / f"{prefix}_color_ceiling.bin")
                    _save_surface_blob(_ceiling, out / f"{prefix}_color_ceiling_contour.bin")
                    _save_surface_blob(_surface, out / f"{prefix}_total_surface_contour.bin")
                    _written_thickness = _write_thickness_blobs(
                        out, sr.cap_map, sr.thickness_maps, prefix=prefix,
                    )
                    if _boundary_cap_map is not None:
                        _save_surface_blob(
                            _boundary_cap_map,
                            out / f"{prefix}_boundary_cap_height.bin",
                        )
                    _detail_cap_map = sr.detail_cap
                    if _detail_cap_map is not None:
                        _save_surface_blob(
                            _detail_cap_map,
                            out / f"{prefix}_detail_cap_height.bin",
                        )
                    _explorer_plan = _save_explorer_plan_artifact(
                        out,
                        getattr(sr, "solved_plan", None),
                        getattr(sr.config, "palette", ()),
                        prefix=prefix,
                    )

                    palette_data.append({
                        "palette": sr.config.palette, "pred": pred,
                        "de_map": sr.de_map, "gamut_mask": sr.gamut_mask,
                        "stats": st, "prefix": prefix,
                        "cap_quality": dict(getattr(sr, "cap_quality", {})),
                        "filament_ids": list(_written_thickness["filament_paths"].keys()),
                        "white_cap_scale_max": white_cap_scale_max,
                        "boundary_cap_stats": boundary_cap_stats,
                        "detail_cap_stats": detail_cap_stats,
                        "cap_surface_height_stats": cap_surface_height_stats,
                        "boundary_cap_surface_height_stats": boundary_cap_surface_height_stats,
                        "detail_cap_surface_height_stats": detail_cap_surface_height_stats,
                        "explorer_plan": _explorer_plan,
                        "explorer_base_filament_id": sr.config.white_base,
                        "explorer_cap_filament_id": sr.config.effective_white_cap(),
                        "explorer_base_thickness_mm": float(sr.config.d_wb),
                    })

                de_max_all = max((d["stats"].max_de for d in palette_data), default=0.35)
                de_threshold = snapshot["de_threshold"]

                for idx, d in enumerate(palette_data, start=1):
                    _progress({
                        "stage_label": f"Writing compare previews {idx}/{len(palette_data)}",
                        "stage_pct": round(100 * idx / max(len(palette_data), 1)),
                    })
                    prefix = d["prefix"]
                    st = d["stats"]
                    boundary_cap_stats = d.get("boundary_cap_stats")
                    detail_cap_stats = d.get("detail_cap_stats")
                    white_cap_scale_max = d.get(
                        "white_cap_scale_max",
                        _white_cap_preview_scale_max(snapshot),
                    )
                    cap_surface_height_stats = d.get("cap_surface_height_stats")
                    boundary_cap_surface_height_stats = d.get("boundary_cap_surface_height_stats")
                    detail_cap_surface_height_stats = d.get("detail_cap_surface_height_stats")
                    explorer_plan = d.get("explorer_plan") or {}
                    _save_de_map_scaled(d["de_map"], out / f"{prefix}_de_perceptual.png",
                                        scale_max=de_threshold * 3)
                    _save_de_map_scaled(d["de_map"], out / f"{prefix}_de_maxset.png",
                                        scale_max=de_max_all * 1.05)
                    _save_de_raw(d["de_map"], out / f"{prefix}_de_raw.png",
                                 de_max=de_max_all * 1.05)

                    _ccb = f"?t={int(time.time())}"
                    results.append({
                        "palette": d["palette"],
                        "mean_de": st.mean_de, "max_de": st.max_de,
                        "source_rms_de": getattr(st, "source_rms_de", st.mean_de),
                        "de_scale_max": round(de_max_all * 1.05, 4),
                        "n_oog": st.n_out_of_gamut,
                        "total_pixels": st.total_pixels,
                        "coverage_pct": st.coverage_pct,
                        "image_w": W, "image_h": H,
                        "predicted_url": f"/api/run-cache/files/{prefix}_predicted.png{_ccb}",
                        "de_map_perceptual_url": f"/api/run-cache/files/{prefix}_de_perceptual.png{_ccb}",
                        "de_map_maxset_url": f"/api/run-cache/files/{prefix}_de_maxset.png{_ccb}",
                        "de_raw_url": f"/api/run-cache/files/{prefix}_de_raw.png{_ccb}",
                        "cap_map_url": f"/api/run-cache/files/{prefix}_cap.png{_ccb}",
                        "boundary_cap_map_url": (
                            f"/api/run-cache/files/{prefix}_boundary_cap.png{_ccb}"
                            if boundary_cap_stats is not None
                            else None
                        ),
                        "boundary_cap_map_active_px": (
                            int(boundary_cap_stats["active_px"])
                            if boundary_cap_stats is not None
                            else 0
                        ),
                        "boundary_cap_map_max_d": (
                            float(boundary_cap_stats["max_d"])
                            if boundary_cap_stats is not None
                            else 0.0
                        ),
                        "detail_cap_map_url": (
                            f"/api/run-cache/files/{prefix}_detail_cap.png{_ccb}"
                            if detail_cap_stats is not None
                            else None
                        ),
                        "detail_cap_map_active_px": (
                            int(detail_cap_stats["active_px"])
                            if detail_cap_stats is not None
                            else 0
                        ),
                        "detail_cap_map_max_d": (
                            float(detail_cap_stats["max_d"])
                            if detail_cap_stats is not None
                            else 0.0
                        ),
                        "white_cap_scale_max_d": round(float(white_cap_scale_max), 4),
                        "cap_surface_height_url": (
                            f"/api/run-cache/files/{prefix}_cap_surface_height.png{_ccb}"
                            if cap_surface_height_stats is not None
                            else None
                        ),
                        "cap_surface_height_max_d": (
                            float(cap_surface_height_stats["max_d"])
                            if cap_surface_height_stats is not None
                            else 0.0
                        ),
                        "boundary_cap_surface_height_url": (
                            f"/api/run-cache/files/{prefix}_boundary_cap_surface_height.png{_ccb}"
                            if boundary_cap_surface_height_stats is not None
                            else None
                        ),
                        "boundary_cap_surface_height_max_d": (
                            float(boundary_cap_surface_height_stats["max_d"])
                            if boundary_cap_surface_height_stats is not None
                            else 0.0
                        ),
                        "detail_cap_surface_height_url": (
                            f"/api/run-cache/files/{prefix}_detail_cap_surface_height.png{_ccb}"
                            if detail_cap_surface_height_stats is not None
                            else None
                        ),
                        "detail_cap_surface_height_max_d": (
                            float(detail_cap_surface_height_stats["max_d"])
                            if detail_cap_surface_height_stats is not None
                            else 0.0
                        ),
                        "color_ceiling_url": f"/api/run-cache/files/{prefix}_color_ceiling.png{_ccb}",
                        "total_surface_url": f"/api/run-cache/files/{prefix}_total_surface.png{_ccb}",
                        "color_ceiling_bin_url": f"/api/run-cache/files/{prefix}_color_ceiling.bin{_ccb}",
                        "color_ceiling_contour_bin_url": f"/api/run-cache/files/{prefix}_color_ceiling_contour.bin{_ccb}",
                        "total_surface_bin_url": f"/api/run-cache/files/{prefix}_total_surface.bin{_ccb}",
                        "total_surface_contour_bin_url": f"/api/run-cache/files/{prefix}_total_surface_contour.bin{_ccb}",
                        "cap_height_bin_url": f"/api/run-cache/files/{prefix}_cap_height.bin{_ccb}",
                        "boundary_cap_height_bin_url": (
                            f"/api/run-cache/files/{prefix}_boundary_cap_height.bin{_ccb}"
                            if boundary_cap_stats is not None
                            else None
                        ),
                        "detail_cap_height_bin_url": (
                            f"/api/run-cache/files/{prefix}_detail_cap_height.bin{_ccb}"
                            if detail_cap_stats is not None
                            else None
                        ),
                        "filament_bin_urls": {
                            fid: f"/api/run-cache/files/{prefix}_filament_{fid}.bin{_ccb}"
                            for fid in d["filament_ids"]
                        },
                        "explorer_stack_label_bin_url": (
                            f"/api/run-cache/files/{explorer_plan['filename']}{_ccb}"
                            if explorer_plan
                            else None
                        ),
                        "explorer_stack_table": explorer_plan.get("stack_table"),
                        "explorer_base_filament_id": d.get("explorer_base_filament_id"),
                        "explorer_cap_filament_id": d.get("explorer_cap_filament_id"),
                        "explorer_base_thickness_mm": d.get("explorer_base_thickness_mm"),
                        "cap_quality": d["cap_quality"],
                        "source_url": f"/api/run-cache/files/compare_source.png{_ccb}",
                    })

                compare["result"] = {"results": results}
                compare["status"] = "complete"
                compare["progress"] = {"stage_label": "Done", "stage_pct": 100}
                logger.info("Compare complete: %d palettes, %.1fs",
                            len(palettes), time.time() - start)

            except SolveCancelled:
                compare["status"] = "cancelled"
                compare["progress"] = {"stage_label": "Cancelled"}
                logger.info("Compare cancelled by user after %.1fs", time.time() - start)
            except Exception as exc:
                logger.exception("Compare failed")
                compare["status"] = "error"
                compare["progress"] = {"stage_label": str(exc)}
            finally:
                compare["elapsed_s"] = round(time.time() - start, 2)
                compare["cancel_requested"] = False

    thread = threading.Thread(target=_run_compare, daemon=True)
    thread.start()
    return {"status": "running", "job_id": job_id}


@app.get("/api/palette/compare/status")
def compare_status() -> dict:
    """Poll compare status."""
    compare = session["compare"]
    prog = compare["progress"]
    return {
        "job_id": compare.get("job_id"),
        "status": compare["status"],
        "progress": prog.get("stage_label", "") if isinstance(prog, dict) else str(prog),
        "progress_detail": prog if isinstance(prog, dict) else {},
        "elapsed_s": round(compare["elapsed_s"], 2),
        "result": compare["result"],
    }


# ── Solve ─────────────────────────────────────────────────────────────────

class SolveStartPayload(BaseModel):
    palette: Optional[List[str]] = None  # override session palette
    card_id: Optional[str] = None        # run ID for per-run caching
    profile_ref: Optional[Dict[str, Any]] = None
    profile_name_at_solve: Optional[str] = None
    is_profile_modified_at_solve: Optional[bool] = None
    recipe_snapshot: Optional[Dict[str, Any]] = None


_WHITE_CAP_STAGE4_FIELD_ARRAY_KEYS = (
    "stage4_boundary_raw_requested_cap_mm",
    "stage4_boundary_raw_top_reference_mm",
    "stage4_boundary_smoothed_top_pre_restore_mm",
    "stage4_boundary_smoothed_top_post_restore_mm",
    "stage4_boundary_unquantized_requested_cap_mm",
    "stage4_boundary_quantized_requested_cap_mm",
    "stage4_boundary_smooth_candidate_cap_mm",
    "stage4_boundary_appearance_raw_de",
    "stage4_boundary_appearance_candidate_de",
    "stage4_boundary_appearance_accepted_de",
    "stage4_boundary_appearance_extra_de",
    "stage4_boundary_appearance_bounded_cap_mm",
    "stage4_boundary_appearance_rejected_mm",
    "stage4_boundary_appearance_accept_mask",
    "stage4_boundary_candidate_minus_raw_mm",
    "stage4_boundary_accepted_minus_raw_mm",
    "stage4_boundary_minimal_floor_mm",
    "stage4_appearance_desired_final_cap_mm",
    "stage4_boundary_structural_cap_mm",
    "stage4_final_target_equivalence_delta_mm",
    "stage4_color_ceiling_mm",
    "stage4_boundary_edge_guard_weight",
    "stage4_detail_optical_gain_map",
    "stage4_detail_best_layers_pre_authoring_mm",
    "stage4_detail_signal_map",
    "stage4_detail_candidate_mask_pre_zone",
    "stage4_detail_candidate_zone_labels",
    "stage4_detail_zone_labels",
    "stage4_detail_rejection_reasons",
    "stage4_detail_requested_layers_post_authoring_mm",
    "stage4_detail_residual_from_appearance_target_mm",
    "stage4_detail_final_height_mm",
)
_WHITE_CAP_LUMINANCE_HANDLER_ARRAY_KEYS = (
    "luminance_handler_source_l",
    "luminance_handler_boundary_l",
    "luminance_handler_full_cap_reference",
    "luminance_handler_boundary_cap_prior",
    "luminance_handler_boundary_authority_mm_map",
    "luminance_handler_stage4_boundary_request",
    "luminance_handler_stage4_detail_reference",
    "luminance_handler_stage4_boundary_after_hard_ceiling",
)
_POST_SOLVE_EXPORT_BUNDLE_DIRNAME = "post_solve_export_bundle"
_FRESH_WHITE_CAP_EXPORT_FIELDS_ERROR = (
    "Export requires fresh white-cap canonical fields; rerun the solve before exporting."
)


def _white_cap_array(
    thickness_maps: dict,
    key: "MapKey | str",
    *,
    expected_shape: tuple[int, int] | None = None,
) -> np.ndarray | None:
    # ``key`` may be a MapKey; use its exact string value in error messages so
    # the wording stays identical to the legacy raw-string contract.
    label = key.value if isinstance(key, MapKey) else key
    value = thickness_maps.get(key)
    if value is None:
        return None
    arr = np.asarray(value, dtype=np.float32)
    if arr.ndim != 2:
        raise HTTPException(409, f"White-cap thickness map {label} is not 2D")
    if expected_shape is not None and tuple(arr.shape) != tuple(expected_shape):
        raise HTTPException(
            409,
            f"White-cap thickness map {label} shape {tuple(arr.shape)} does not match expected {expected_shape}",
        )
    return np.array(arr, dtype=np.float32, copy=True)


def _export_contract_array(
    solve: dict,
    key: str,
    *,
    expected_shape: tuple[int, int],
) -> np.ndarray:
    export_maps = solve.get("export_maps")
    if not isinstance(export_maps, dict):
        raise HTTPException(409, _FRESH_WHITE_CAP_EXPORT_FIELDS_ERROR)
    value = export_maps.get(key)
    if value is None:
        raise HTTPException(409, _FRESH_WHITE_CAP_EXPORT_FIELDS_ERROR)
    arr = np.asarray(value, dtype=np.float32)
    if arr.ndim != 2 or tuple(arr.shape) != tuple(expected_shape):
        raise HTTPException(
            409,
            f"Canonical export map {key!r} shape {tuple(arr.shape)} does not match expected {expected_shape}",
        )
    return np.array(arr, dtype=np.float32, copy=True)


def _export_contract_metadata(solve: dict) -> dict:
    metadata = solve.get("export_metadata")
    if not isinstance(metadata, dict):
        raise HTTPException(409, _FRESH_WHITE_CAP_EXPORT_FIELDS_ERROR)
    target_meta = metadata.get(WHITE_CAP_FIELD_TARGET_METADATA_KEY)
    physical = metadata.get(PHYSICAL_GEOMETRY_METADATA_KEY)
    if not isinstance(target_meta, dict) or not isinstance(physical, dict):
        raise HTTPException(409, _FRESH_WHITE_CAP_EXPORT_FIELDS_ERROR)
    if target_meta.get("field_key") != WHITE_CAP_FIELD_TARGET_UPPER_SURFACE_KEY:
        raise HTTPException(409, _FRESH_WHITE_CAP_EXPORT_FIELDS_ERROR)
    if not target_meta.get("luminance_mode") or not target_meta.get("cap_mode"):
        raise HTTPException(409, _FRESH_WHITE_CAP_EXPORT_FIELDS_ERROR)
    if "luminance_mode" not in physical or "cap_mode" not in physical:
        raise HTTPException(409, _FRESH_WHITE_CAP_EXPORT_FIELDS_ERROR)
    return deepcopy(metadata)


def _materialize_post_solve_export_bundle_from_cached_solve(
    *,
    card_id: str | None,
    solve: dict,
    cfg: dict,
    thickness_maps: dict,
    ordering: list[str],
) -> Path:
    """Write the canonical bundle consumed by the post-solve exporter."""

    if not thickness_maps:
        raise HTTPException(400, "No thickness maps available")
    if MapKey.WHITE_CAP not in thickness_maps:
        raise HTTPException(409, "Export requires a __white_cap__ thickness map")
    if not ordering:
        raise HTTPException(409, "Export requires at least one color thickness map")

    ref = np.asarray(thickness_maps[MapKey.WHITE_CAP], dtype=np.float32)
    if ref.ndim != 2:
        raise HTTPException(409, "__white_cap__ thickness map must be 2D")
    shape = tuple(int(v) for v in ref.shape)

    color_arrays: list[np.ndarray] = []
    for fid in ordering:
        arr = np.asarray(thickness_maps[fid], dtype=np.float32)
        if arr.ndim != 2 or tuple(arr.shape) != shape:
            raise HTTPException(
                409,
                f"Color thickness map {fid!r} has shape {arr.shape}, expected {shape}",
            )
        color_arrays.append(np.array(arr, dtype=np.float32, copy=True))

    white_total = np.array(ref, dtype=np.float32, copy=True)
    boundary = _white_cap_array(thickness_maps, MapKey.WHITE_BOUNDARY_CAP, expected_shape=shape)
    detail = _white_cap_array(thickness_maps, MapKey.WHITE_DETAIL_CAP, expected_shape=shape)
    if boundary is None:
        raise HTTPException(409, _FRESH_WHITE_CAP_EXPORT_FIELDS_ERROR)
    if detail is None:
        raise HTTPException(409, _FRESH_WHITE_CAP_EXPORT_FIELDS_ERROR)
    canonical_target = _export_contract_array(
        solve,
        WHITE_CAP_FIELD_TARGET_UPPER_SURFACE_KEY,
        expected_shape=shape,
    )
    export_metadata = _export_contract_metadata(solve)
    swap_grouping = _swap_grouping_from_solve(solve)
    try:
        band_plan = banded_export_plan_from_metadata(
            swap_grouping,
            d_wb_mm=float(cfg.get("d_wb", 0.0) or 0.0),
            layer_height_mm=float(cfg.get("layer_height", 0.08) or 0.08),
            expected_palette=ordering,
        )
    except ValueError as exc:
        raise HTTPException(409, f"Invalid swap grouping on solved run: {exc}") from exc
    if band_plan is None:
        color_ceiling = _compute_color_ceiling(
            thickness_maps,
            float(cfg.get("d_wb", 0.0) or 0.0),
        )
        band_fills: tuple[np.ndarray, ...] = ()
    else:
        try:
            band_fills = banded_fill_maps(thickness_maps, band_plan)
        except ValueError as exc:
            raise HTTPException(409, f"Banded solve cannot be exported: {exc}") from exc
        color_ceiling = banded_color_ceiling_map(shape, band_plan)
        color_stack_height = np.sum(
            np.stack(color_arrays, axis=0),
            axis=0,
            dtype=np.float32,
        )
        d_wb = np.float32(float(cfg.get("d_wb", 0.0) or 0.0))
        canonical_target = (
            canonical_target
            + (color_ceiling - (d_wb + color_stack_height))
        ).astype(np.float32, copy=False)
        d_wc_min = np.float32(float(cfg.get("d_wc_min", 0.0) or 0.0))
        if np.any(canonical_target < color_ceiling + d_wc_min - np.float32(1e-6)):
            raise HTTPException(
                409,
                "Banded export canonical white-cap field target is below the "
                "minimum printable cap after ceiling rebase",
            )
    boundary_upper = np.where(
        boundary > np.float32(1e-9),
        color_ceiling + boundary,
        np.float32(0.0),
    ).astype(np.float32, copy=False)

    arrays: dict[str, np.ndarray] = {
        "color_thickness_maps": np.stack(color_arrays, axis=0).astype(np.float32, copy=False),
        "white_cap_total_thickness_map": white_total,
        "white_cap_boundary_thickness_map": np.array(boundary, dtype=np.float32, copy=True),
        "white_cap_detail_thickness_map": np.array(detail, dtype=np.float32, copy=True),
        "color_stack_ceiling_height_map": np.array(color_ceiling, dtype=np.float32, copy=True),
        "boundary_cap_upper_surface_height_map": np.array(boundary_upper, dtype=np.float32, copy=True),
        WHITE_CAP_FIELD_TARGET_UPPER_SURFACE_KEY: canonical_target,
    }
    if band_plan is not None:
        arrays["band_white_fill_thickness_maps"] = np.stack(band_fills, axis=0).astype(
            np.float32,
            copy=False,
        )

    debug_maps = dict(solve.get("debug_maps") or {})
    for key in _WHITE_CAP_STAGE4_FIELD_ARRAY_KEYS:
        value = debug_maps.get(key)
        if value is None:
            continue
        arr = np.asarray(value, dtype=np.float32)
        if arr.ndim == 2 and tuple(arr.shape) == shape:
            arrays[key] = np.array(arr, dtype=np.float32, copy=True)
    for key in _WHITE_CAP_LUMINANCE_HANDLER_ARRAY_KEYS:
        value = debug_maps.get(key)
        if value is None:
            continue
        arr = np.asarray(value, dtype=np.float32)
        if arr.ndim == 2 and tuple(arr.shape) == shape:
            arrays[key] = np.array(arr, dtype=np.float32, copy=True)

    out_dir = _current_out_dir(card_id) / _POST_SOLVE_EXPORT_BUNDLE_DIRNAME
    out_dir.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out_dir / "arrays.npz", **arrays)

    run_metadata = {
        "schema": "post-solve-export-run-metadata-v1",
        "card_id": card_id,
        "created_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "config": {
            **dict(cfg),
            "luminance_mode": cfg.get("luminance_mode", "standard"),
            "solver_fine_pitch_mm": cfg.get("solver_fine_pitch_mm") or cfg.get("image_sample_pitch_mm"),
            "image_sample_pitch_mm": cfg.get("image_sample_pitch_mm") or cfg.get("solver_fine_pitch_mm"),
            "layer_height": cfg.get("layer_height"),
            "d_wb": cfg.get("d_wb"),
        },
        "resolved_settings": {
            "luminance_mode": cfg.get("luminance_mode", "standard"),
            "solver_fine_pitch_mm": cfg.get("solver_fine_pitch_mm") or cfg.get("image_sample_pitch_mm"),
            "layer_height": cfg.get("layer_height"),
            "d_wb": cfg.get("d_wb"),
        },
        "export_metadata": export_metadata,
        PHYSICAL_GEOMETRY_METADATA_KEY: export_metadata[PHYSICAL_GEOMETRY_METADATA_KEY],
        WHITE_CAP_FIELD_TARGET_METADATA_KEY: export_metadata[
            WHITE_CAP_FIELD_TARGET_METADATA_KEY
        ],
    }
    if swap_grouping is not None:
        run_metadata["swap_grouping"] = deepcopy(swap_grouping)
    (out_dir / "run_template.json").write_text(
        json.dumps(run_metadata, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )

    replay = {
        "schema": "prediction-replay-v1",
        "available": True,
        "color_thickness_maps_key": "color_thickness_maps",
        "color_thickness_filament_ids": list(ordering),
        "white_base_filament_id": _white_base(cfg),
        "white_cap_filament_id": _white_cap(cfg),
        "d_wb_mm": float(cfg.get("d_wb", 0.0) or 0.0),
        "layer_height_mm": float(cfg.get("layer_height", 0.08) or 0.08),
    }
    if swap_grouping is not None:
        replay["swap_grouping"] = deepcopy(swap_grouping)
    (out_dir / "prediction_replay_metadata.json").write_text(
        json.dumps(replay, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    summary = {
        "schema": "post-solve-export-bundle-summary-v1",
        "card_id": card_id,
        "shape_hw": [int(shape[0]), int(shape[1])],
        "array_names": sorted(arrays),
        "config": run_metadata["config"],
    }
    (out_dir / "stageA_field_bundle_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    return out_dir


def _material_display_names_for_export(material_keys: list[str], cfg: dict) -> dict[str, str]:
    registry = _load_registry()
    white_base_id = _white_base(cfg)
    white_cap_id = _white_cap(cfg)
    names: dict[str, str] = {}
    for key in material_keys:
        if key == "__white_base__":
            fid = white_base_id
        elif key == "__white_cap__":
            fid = white_cap_id
        else:
            fid = key
        entry = registry.get(fid, {}) if isinstance(registry, dict) else {}
        names[key] = str(entry.get("display_name") or fid)
    return names


def _filament_assignments_for_export(material_keys: list[str], cfg: dict) -> dict[str, int]:
    white_base_id = _white_base(cfg)
    white_cap_id = _white_cap(cfg)
    slot_by_filament: dict[str, int] = {}
    assignments: dict[str, int] = {}
    for key in material_keys:
        if key == "__white_base__":
            fid = white_base_id
        elif key == "__white_cap__":
            fid = white_cap_id
        else:
            fid = key
        if fid not in slot_by_filament:
            slot_by_filament[fid] = len(slot_by_filament) + 1
        assignments[key] = slot_by_filament[fid]
    return assignments


def _copy_export_maps_for_session(export_maps: dict | None) -> dict[str, np.ndarray]:
    if not export_maps:
        return {}
    return {
        str(getattr(key, "value", key)): np.asarray(value, dtype=np.float32).copy()
        for key, value in export_maps.items()
    }


def _write_completed_solve_cache_entry(card_id: str, cfg: dict, solve: dict, result) -> dict:
    session["solve_cache"][card_id] = {
        "config": json.loads(json.dumps(cfg, default=str)),
        "solve": {
            "status": "complete",
            "card_id": card_id,
            "thickness_maps": result.thickness_maps,
            "color_profiles": solve.get("color_profiles"),
            "wb_profile": solve.get("wb_profile"),
            "wc_profile": solve.get("wc_profile"),
            "image_domain_width_mm": solve.get("image_domain_width_mm"),
            "image_domain_height_mm": solve.get("image_domain_height_mm"),
            "solved_plan": solve.get("solved_plan"),
            "blueprint_triage": solve.get("blueprint_triage"),
            "debug_maps": getattr(result, "debug_maps", {}) or {},
            "export_maps": _copy_export_maps_for_session(
                getattr(result, "export_maps", {}) or {}
            ),
            "export_metadata": deepcopy(getattr(result, "export_metadata", {}) or {}),
            "material_exposure_audit": solve.get("material_exposure_audit"),
            "solve_owned_fingerprint": solve.get("solve_owned_fingerprint"),
            "result": solve["result"],
        },
    }
    return session["solve_cache"][card_id]


_SOLVE_PITCH_NOZZLE_TOLERANCE_MM = 1e-6


def _validate_solve_pitch_for_nozzle(cfg: dict, active: dict | None = None) -> None:
    """Reject a canonical solve grid that is finer than the active nozzle."""
    resolved_active = active if active is not None else _resolve_active_printer(_load_printers())
    nozzle = (resolved_active or {}).get("nozzle") or {}
    nozzle_size = nozzle.get("size")
    pitch = cfg.get("solver_fine_pitch_mm")
    if pitch is None:
        pitch = cfg.get("image_sample_pitch_mm")
    try:
        pitch_value = float(pitch)
        nozzle_value = float(nozzle_size)
    except (TypeError, ValueError):
        return
    if pitch_value < nozzle_value - _SOLVE_PITCH_NOZZLE_TOLERANCE_MM:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Solve Pitch ({pitch_value:g} mm) cannot be smaller than the active "
                f"nozzle diameter ({nozzle_value:g} mm). Increase Solve Pitch or choose "
                "a smaller nozzle."
            ),
        )


@app.post("/api/solve/start")
def start_solve(payload: SolveStartPayload = SolveStartPayload()) -> dict:
    """Start full solve in a background thread."""
    _require_model_library()
    if session["solve"]["status"] == "running":
        raise HTTPException(409, "Solve already running")

    raw_cfg = _cfg()
    if not raw_cfg["image_path"]:
        raise HTTPException(400, "No image selected")
    palette = payload.palette if payload.palette else raw_cfg["palette"]
    if not palette:
        raise HTTPException(400, "No palette selected")

    image_path = _IMAGES_DIR / raw_cfg["image_path"]
    if not image_path.exists():
        raise HTTPException(404, f"Image not found: {raw_cfg['image_path']}")

    # Snapshot config so the solve thread isn't affected by mid-solve changes
    cfg = dict(raw_cfg)
    _validate_solve_pitch_for_nozzle(cfg)
    cfg["palette"] = canonical_palette_order(
        palette,
        load_filament_order_registry(),
    )  # apply override if provided
    card_id = payload.card_id
    profile_ref = dict(payload.profile_ref or {})
    profile_name_at_solve = payload.profile_name_at_solve
    is_profile_modified_at_solve = bool(payload.is_profile_modified_at_solve)
    _raw_recipe = dict(payload.recipe_snapshot or {})
    if "config" in _raw_recipe and isinstance(_raw_recipe["config"], dict):
        _raw_recipe = {**_raw_recipe, "config": _with_canonical_pitch_egress(_raw_recipe["config"])}
    recipe_snapshot = _raw_recipe
    job_id = str(uuid.uuid4())[:8]
    solve_start_diagnostics = _build_solve_start_diagnostics(cfg)
    sc = _build_solve_config(cfg)
    swap_banding_requested = len(sc.palette) > sc.color_slots()
    solve_stage_count = 10 if swap_banding_requested else 7
    started_monotonic = time.monotonic()

    # Set status synchronously so _assert_no_active_job() sees "running"
    # immediately after this handler returns (mirrors export_files_start).
    _reserve_model_job(
        "solve",
        already_running="Solve already running",
        state={
            "status": "running",
            "progress": {},
            "elapsed_s": 0.0,
            "started_monotonic": started_monotonic,
            "job_id": job_id,
            "card_id": card_id,
            "cancel_requested": False,
        },
    )

    def _store_progress(event: dict) -> None:
        solve = session["solve"]
        if solve.get("job_id") != job_id:
            return
        update = {"progress": dict(event)}
        if event.get("stage") == "complete" and event.get("overall_pct") == 100.0:
            update["status"] = "complete"
        solve.update(update)

    root_progress = ProgressReporter.root(
        _store_progress,
        started_at=started_monotonic,
        cancel_check=lambda: bool(
            session["solve"].get("job_id") == job_id
            and session["solve"].get("cancel_requested")
        ),
        stage_count=solve_stage_count,
        job_id=job_id,
    )
    source_progress = root_progress.child(
        0,
        5,
        stage="source",
        stage_index=1,
    )
    pipeline_progress = root_progress.child(
        5,
        85,
        stage_offset=1,
    )
    artifact_progress = root_progress.child(
        85,
        99,
        stage="artifacts",
        stage_index=solve_stage_count,
    )

    def _run_solve():
        with _solve_lock:
            solve = session["solve"]
            solve["status"] = "running"
            solve["result"] = None
            solve["thickness_maps"] = None
            solve["predicted"] = None
            solve["img"] = None
            solve["grouping"] = None
            solve["image_domain_width_mm"] = None
            solve["image_domain_height_mm"] = None
            solve["solved_plan"] = None
            solve["blueprint_triage"] = None
            solve["export_maps"] = None
            solve["export_metadata"] = None
            solve["solve_owned_fingerprint"] = None
            start = time.time()

            # Timing-only instrumentation: coarse phase marks for the solve
            # thread, logged as one SOLVE_PHASE_TIMINGS line on completion.
            _phase_marks: list = []

            def _mark_phase(name: str) -> None:
                _phase_marks.append((name, time.time()))

            out = _current_out_dir(card_id)

            try:
                # Step 1: Load image
                source_progress.emit(
                    stage="source",
                    stage_label="Loading source image...",
                    stage_index=1,
                    local_pct=0,
                )
                img = _load_run_source_image(image_path, cfg)
                H, W = img.shape[:2]
                solve["img"] = img
                _mark_phase("load_image")
                logger.info("Loaded image: %dx%d px", W, H)
                logger.info("Solve started: %dx%d (%d px), palette=[%s]",
                            W, H, W * H, ", ".join(cfg["palette"]))
                _log_solve_start_diagnostics(job_id, solve_start_diagnostics)
                source_progress.emit(
                    stage="source",
                    stage_label="Source image loaded",
                    stage_index=1,
                    stage_pct=100,
                    local_pct=100,
                )

                # Step 2-4: Facade solve (profiles → LUTs → solve → grouping)
                result = solve_full(img, sc, progress=pipeline_progress,
                                    modules_path=_MODULES_PATH)
                _mark_phase("solve_full")
                detail_cap_smoothing_summary = (
                    getattr(result, "cap_quality", {}) or {}
                ).get(
                    "detail_cap_smoothing"
                )
                if detail_cap_smoothing_summary:
                    logger.info(
                        "Detail cap smoothing applied: changed_px=%d, "
                        "exact<=3 %d->%d, cumulative<=3 %d->%d, holes<=3 %d->%d",
                        int(detail_cap_smoothing_summary.get("changed_px", 0) or 0),
                        int(
                            detail_cap_smoothing_summary["before"]["topology"][
                                "exact_components_le3"
                            ]
                        ),
                        int(
                            detail_cap_smoothing_summary["after"]["topology"][
                                "exact_components_le3"
                            ]
                        ),
                        int(
                            detail_cap_smoothing_summary["before"]["topology"][
                                "cumulative_components_le3"
                            ]
                        ),
                        int(
                            detail_cap_smoothing_summary["after"]["topology"][
                                "cumulative_components_le3"
                            ]
                        ),
                        int(
                            detail_cap_smoothing_summary["before"]["topology"][
                                "cumulative_holes_le3"
                            ]
                        ),
                        int(
                            detail_cap_smoothing_summary["after"]["topology"][
                                "cumulative_holes_le3"
                            ]
                        ),
                    )

                solve["thickness_maps"] = result.thickness_maps
                solve["color_profiles"] = result.color_profiles
                solve["wb_profile"] = result.wb_profile
                solve["wc_profile"] = result.wc_profile
                solve["image_domain_width_mm"] = result.image_domain_width_mm
                solve["image_domain_height_mm"] = result.image_domain_height_mm
                solve["solved_plan"] = result.solved_plan
                solve["blueprint_triage"] = getattr(result, "blueprint_triage", None)
                solve["debug_maps"] = getattr(result, "debug_maps", {}) or {}
                solve["export_maps"] = _copy_export_maps_for_session(
                    getattr(result, "export_maps", {}) or {}
                )
                solve["export_metadata"] = deepcopy(
                    getattr(result, "export_metadata", {}) or {}
                )
                # Step 5: Predict and save diagnostics
                artifact_progress.emit(
                    stage="artifacts",
                    stage_label="Generating predicted images...",
                    stage_index=1,
                    local_pct=0,
                )
                pred = result.predict_image()
                solve["predicted"] = pred
                _mark_phase("predict_image")

                # Canonical solve diagnostics are content-domain artifacts.
                # Export borders are emitted later as geometry rather than
                # painted into predicted/source/thickness images.
                Image.fromarray(pred).save(str(out / "predicted.png"))
                Image.fromarray(img).save(str(out / "source.png"))
                reference_img = getattr(result, "reference_image", None)
                if reference_img is None:
                    reference_img = img
                reference_img = _as_uint8_rgb_image(reference_img)
                Image.fromarray(reference_img).save(str(out / "reference.png"))
                palette_fit_img = getattr(result, "palette_fit_image", None)
                if palette_fit_img is None:
                    palette_fit_img = reference_img
                palette_fit_img = _as_uint8_rgb_image(palette_fit_img)
                Image.fromarray(palette_fit_img).save(str(out / "palette_fit.png"))

                # Bake view-domain images for the Appearance/Transmission display
                # toggle. Loud failure is deliberate: a run without these assets
                # must not render.
                from model_domain_display import bake_view_domain_images

                _display_transform_path = str(_DATA_DIR / "camera_transform")

                _view_domain_provenance = bake_view_domain_images(
                    out,
                    pred_srgb8=pred,
                    source_srgb8=img,
                    model_domain_ingress=True,
                    model_domain_ingress_lut_path=str(_DATA_DIR / "camera_transform"),
                    display_transform_path=_display_transform_path,
                )

                # Color-only predicted render for the recipe viewer (Color
                # Regions tab): base + color layers with the white cap omitted,
                # baked through the SAME forward Camera Transform F as the full
                # appearance so the two cards are comparable. Drops only the cap
                # term — a strict subset of the validated composition.
                from model_domain_display import bake_appearance_png

                pred_color_only = result.predict_image_color_only()
                Image.fromarray(pred_color_only).save(
                    str(out / "predicted_color_only.png")
                )
                bake_appearance_png(
                    pred_color_only,
                    out / "predicted_color_only_appearance.png",
                    display_transform_path=_display_transform_path,
                )

                artifact_progress.emit(
                    stage="artifacts",
                    stage_label="Writing diagnostic maps...",
                    stage_index=1,
                    stage_pct=25,
                    local_pct=25,
                )
                # Diagnostic images.
                de_threshold = cfg.get("de_threshold", 0.01)
                de_max = result.stats.max_de

                # dE map (fixed scale)
                _save_de_map(result.de_map, out / "de_map.png")
                # dE perceptual + maxset (variable scale)
                _save_de_map_scaled(result.de_map, out / "de_perceptual.png",
                                    scale_max=de_threshold * 3)
                _save_de_map_scaled(result.de_map, out / "de_maxset.png",
                                    scale_max=de_max * 1.05 if de_max > 0 else 0.35)
                solver_loss_map = getattr(result, "solver_loss_map", None)
                if solver_loss_map is None:
                    solver_loss_map = np.zeros_like(result.de_map, dtype=np.float32)
                solver_loss_scale_max = max(de_threshold * 2, 1e-6)
                _save_de_map_scaled(solver_loss_map, out / "solver_loss.png",
                                    scale_max=solver_loss_scale_max)
                _save_de_raw(
                    solver_loss_map,
                    out / "solver_loss_raw.png",
                    de_max=solver_loss_scale_max,
                )

                white_cap_scale_max = _white_cap_preview_scale_max(cfg)

                _save_cap_height_map(
                    result.cap_map,
                    out / "cap_height.png",
                    max_mm=white_cap_scale_max,
                    zero_rgb=_ZERO_THICKNESS_RGB)
                boundary_cap_map = result.boundary_cap
                detail_cap_map = result.detail_cap
                boundary_cap_stats = _save_white_cap_part_map(
                    boundary_cap_map,
                    out / "boundary_cap_height.png",
                    max_mm=white_cap_scale_max,
                )
                detail_cap_stats = _save_white_cap_part_map(
                    detail_cap_map,
                    out / "detail_cap_height.png",
                    max_mm=white_cap_scale_max,
                )
                cap_map_contour_saved = _save_masked_contour_blob(
                    result.cap_map,
                    result.cap_map,
                    out / "cap_map_contour.bin",
                )
                boundary_cap_contour_saved = _save_masked_contour_blob(
                    boundary_cap_map,
                    boundary_cap_map,
                    out / "boundary_cap_contour.bin",
                )
                detail_cap_contour_saved = _save_masked_contour_blob(
                    detail_cap_map,
                    detail_cap_map,
                    out / "detail_cap_contour.bin",
                )

                # Color ceiling and total surface diagnostic maps
                t_max = cfg["t_max"]
                _result_swap_grouping = (getattr(result, "diagnostics", {}) or {}).get(
                    "__swap_grouping__"
                )
                _result_filament_ids = getattr(result, "filament_ids", None)
                _solved_palette_order = _solved_palette_order_from_grouping(
                    _result_swap_grouping,
                    (
                        list(_result_filament_ids)
                        if _result_filament_ids is not None
                        else list(cfg["palette"])
                    ),
                )
                try:
                    _display_band_plan = _banded_display_plan(
                        _result_swap_grouping,
                        _solved_palette_order,
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise RuntimeError(f"Solved swap grouping is invalid: {exc}") from exc
                color_ceiling = (
                    banded_color_ceiling_map(result.cap_map.shape, _display_band_plan)
                    if _display_band_plan is not None
                    else _compute_color_ceiling(result.thickness_maps, cfg["d_wb"])
                )
                total_surface = _compute_total_surface(
                    color_ceiling,
                    result.cap_map,
                )
                boundary_cap_surface = None
                if boundary_cap_map is not None:
                    boundary_cap_surface = _compute_cap_component_surface(
                        color_ceiling,
                        boundary_cap_map,
                    )
                cap_surface_height_stats = _save_masked_white_cap_height_map(
                    total_surface,
                    result.cap_map,
                    out / "cap_surface_height.png",
                    max_mm=t_max,
                )
                boundary_cap_surface_height_stats = _save_masked_white_cap_height_map(
                    boundary_cap_surface,
                    boundary_cap_map,
                    out / "boundary_cap_surface_height.png",
                    max_mm=t_max,
                )
                detail_cap_surface_height_stats = _save_masked_white_cap_height_map(
                    total_surface,
                    detail_cap_map,
                    out / "detail_cap_surface_height.png",
                    max_mm=t_max,
                )
                cap_surface_height_contour_saved = _save_masked_contour_blob(
                    total_surface,
                    result.cap_map,
                    out / "cap_surface_height_contour.bin",
                )
                boundary_cap_surface_height_contour_saved = _save_masked_contour_blob(
                    boundary_cap_surface,
                    boundary_cap_map,
                    out / "boundary_cap_surface_height_contour.bin",
                )
                detail_cap_surface_height_contour_saved = _save_masked_contour_blob(
                    total_surface,
                    detail_cap_map,
                    out / "detail_cap_surface_height_contour.bin",
                )

                # Static viridis PNGs (same scale as cap map but pegged to t_max)
                _save_cap_height_map(
                    color_ceiling,
                    out / "color_ceiling.png", max_mm=t_max)
                _save_cap_height_map(
                    total_surface,
                    out / "total_surface.png", max_mm=t_max)

                # Binary float32 blobs for interactive views
                _save_surface_blob(color_ceiling, out / "color_ceiling.bin")
                _save_surface_blob(total_surface, out / "total_surface.bin")
                _save_surface_blob(color_ceiling, out / "color_ceiling_contour.bin")
                _save_surface_blob(total_surface, out / "total_surface_contour.bin")
                _written_thickness = _write_thickness_blobs(
                    out, result.cap_map, result.thickness_maps, prefix=None,
                )
                if boundary_cap_map is not None:
                    _save_surface_blob(boundary_cap_map, out / "boundary_cap_height.bin")
                if detail_cap_map is not None:
                    _save_surface_blob(detail_cap_map, out / "detail_cap_height.bin")
                _explorer_plan = _save_explorer_plan_artifact(
                    out,
                    result.solved_plan,
                    _solved_palette_order,
                    swap_grouping=_result_swap_grouping,
                    white_fill_filament=_white_cap(cfg),
                )
                _color_recipe_breakdown = _save_color_recipe_breakdown_artifacts(
                    out,
                    result.solved_plan,
                    _solved_palette_order,
                    swap_grouping=_result_swap_grouping,
                    white_fill_filament=_white_cap(cfg),
                )
                color_ceiling_max_d = round(float(color_ceiling.max()), 4)
                total_surface_max_d = round(float(total_surface.max()), 4)

                # Cache-bust so browser fetches fresh images.  Compute this
                # before debug-map URLs are assembled below.
                _dir_q = f"run={card_id}&" if card_id else ""
                _cb = f"?{_dir_q}t={int(time.time())}"

                overlay_files = {
                    "cap_cliff_risk": "cap_cliff_risk.png",
                    "cap_island_risk": "cap_island_risk.png",
                    "cap_pinhole_risk": "cap_pinhole_risk.png",
                    "cap_seam_risk": "cap_seam_risk.png",
                }
                overlay_urls = {}
                for key, filename in overlay_files.items():
                    overlay = getattr(result, "debug_maps", {}).get(key)
                    if overlay is None:
                        continue
                    _save_overlay_map(
                        overlay.astype(np.float32),
                        out / filename,
                    )
                    overlay_urls[f"{key}_url"] = f"/api/run-cache/files/{filename}{_cb}"

                debug_map_urls = {}
                staged_debug_keys = (
                    "stage2_boundary_mutation",
                    "stage2_final_substrate_repair_absorptions",
                    "stage2_printability_gate_rejections",
                    "stage2_printability_repair",
                    "stage4_boundary_cap_printability_repairs",
                    "stage4_detail_printability_suppression",
                    "blueprint_printability_hard_fail",
                    "blueprint_printability_narrow_width",
                    "blueprint_printability_short_component",
                    "blueprint_printability_color_hard_fail",
                    "blueprint_printability_cap_hard_fail",
                    "blueprint_printability_boundary_cap_hard_fail",
                    "blueprint_printability_detail_hard_fail",
                    "blueprint_printability_center_clearance",
                    "blueprint_printability_width_loss",
                    "blueprint_printability_low_support",
                    "boundary_cap_height",
                    "detail_height",
                    "detail_zone_labels",
                    "detail_rejection_reasons",
                    "final_visible_top",
                    "zone_labels",
                    "recipe_labels",
                    *_WHITE_CAP_STAGE4_FIELD_ARRAY_KEYS,
                )
                debug_maps = getattr(result, "debug_maps", {}) or {}
                for key in staged_debug_keys:
                    debug_map = debug_maps.get(key)
                    if debug_map is None:
                        continue
                    filename = f"{key}.png"
                    _save_overlay_map(
                        np.asarray(debug_map, dtype=np.float32),
                        out / filename,
                    )
                    debug_map_urls[key] = f"/api/run-cache/files/{filename}{_cb}"

                Image.fromarray((result.gamut_mask * 255).astype(np.uint8)).save(
                    str(out / "gamut_mask.png")
                )

                _save_de_raw(
                    result.de_map,
                    out / "de_raw.png",
                    de_max=de_max * 1.05 if de_max > 0 else 0.35)

                artifact_progress.emit(
                    stage="artifacts",
                    stage_label="Writing material maps...",
                    stage_index=1,
                    stage_pct=60,
                    local_pct=60,
                )
                # Per-filament thickness maps as colored PNGs
                filament_maps_info = []
                for fs in result.stats.per_filament:
                    d_map = result.thickness_maps[fs.filament_id]
                    volume_mm3 = _thickness_map_volume_mm3(
                        d_map,
                        image_domain_width_mm=result.image_domain_width_mm,
                        image_domain_height_mm=result.image_domain_height_mm,
                    )

                    d_norm = np.clip(d_map / max(d_map.max(), 1e-9), 0, 1)
                    # Viridis colormap (same polynomial as cap height map)
                    r_ch = np.clip(( 0.267 + 2.173*d_norm - 1.802*d_norm**2) * 255, 0, 255).astype(np.uint8)
                    g_ch = np.clip((-0.004 + 1.874*d_norm - 0.870*d_norm**2) * 255, 0, 255).astype(np.uint8)
                    b_ch = np.clip(( 0.329 - 1.120*d_norm + 0.791*d_norm**2) * 255, 0, 255).astype(np.uint8)
                    inactive = d_map <= 1e-9
                    r_ch[inactive] = 0
                    g_ch[inactive] = 0
                    b_ch[inactive] = 0
                    rgb = np.stack([r_ch, g_ch, b_ch], axis=-1)
                    map_name = f"{fs.filament_id}_thickness.png"
                    Image.fromarray(rgb).save(str(out / map_name))

                    filament_maps_info.append({
                        "filament_id": fs.filament_id,
                        "active_px": fs.active_pixels,
                        "mean_d": round(fs.mean_thickness, 4),
                        "max_d": round(fs.max_thickness, 4),
                        "volume_mm3": (
                            round(volume_mm3, 4) if volume_mm3 is not None else None
                        ),
                        "map_url": f"/api/run-cache/files/{map_name}{_cb}",
                    })

                cap_map_active_px = int((result.cap_map > 1e-9).sum())
                cap_map_max_d = round(float(result.cap_map.max()), 4)
                cap_map_volume_mm3 = _thickness_map_volume_mm3(
                    result.cap_map,
                    image_domain_width_mm=result.image_domain_width_mm,
                    image_domain_height_mm=result.image_domain_height_mm,
                )
                boundary_cap_map_volume_mm3 = (
                    _thickness_map_volume_mm3(
                        boundary_cap_map,
                        image_domain_width_mm=result.image_domain_width_mm,
                        image_domain_height_mm=result.image_domain_height_mm,
                    )
                    if boundary_cap_map is not None
                    else None
                )
                detail_cap_map_volume_mm3 = (
                    _thickness_map_volume_mm3(
                        detail_cap_map,
                        image_domain_width_mm=result.image_domain_width_mm,
                        image_domain_height_mm=result.image_domain_height_mm,
                    )
                    if detail_cap_map is not None
                    else None
                )

                st = result.stats
                run_metadata = _build_run_metadata(
                    cfg=cfg,
                    stats=st,
                    profile_ref=profile_ref,
                    profile_name_at_solve=profile_name_at_solve,
                    is_profile_modified_at_solve=is_profile_modified_at_solve,
                    recipe_snapshot=recipe_snapshot,
                    solve_start_diagnostics=solve_start_diagnostics,
                    card_id=card_id,
                )
                runtime_diagnostics = _runtime_diagnostics_from_result(result)
                staged_metrics = _staged_metrics_from_result(result)
                if "__swap_grouping__" in runtime_diagnostics:
                    staged_metrics["swap_grouping"] = runtime_diagnostics["__swap_grouping__"]
                if "__swap_plan_availability__" in runtime_diagnostics:
                    staged_metrics["swap_plan_availability"] = runtime_diagnostics[
                        "__swap_plan_availability__"
                    ]
                _attach_staged_metrics_to_run_metadata(
                    run_metadata,
                    staged_metrics,
                )
                if runtime_diagnostics:
                    run_metadata["runtime_diagnostics"] = runtime_diagnostics
                if "__swap_grouping__" in runtime_diagnostics:
                    run_metadata["swap_grouping"] = runtime_diagnostics["__swap_grouping__"]
                if "__swap_plan_availability__" in runtime_diagnostics:
                    run_metadata["swap_plan_availability"] = runtime_diagnostics[
                        "__swap_plan_availability__"
                    ]
                material_exposure_audit = _run_material_exposure_audit(
                    result.thickness_maps,
                    cfg,
                ).to_summary()
                run_metadata["material_exposure_audit"] = material_exposure_audit
                run_metadata["white_cap_exposure_safe"] = bool(
                    material_exposure_audit.get("passes", False)
                )
                if _color_recipe_breakdown is not None:
                    run_metadata["color_recipe_breakdown"] = {
                        "json_filename": _color_recipe_breakdown["json_filename"],
                        "csv_filename": _color_recipe_breakdown["csv_filename"],
                        "cookbook_filename": _color_recipe_breakdown["cookbook_filename"],
                        "summary": _color_recipe_breakdown["summary"],
                    }

                artifact_progress.emit(
                    stage="artifacts",
                    stage_label="Writing run metadata...",
                    stage_index=1,
                    stage_pct=75,
                    local_pct=75,
                )
                # Every solve output directory should be self-describing, even when
                # full run logging is off.
                run_metadata["model_domain_display"] = _view_domain_provenance
                _write_run_json(out, run_metadata)

                # Also bust filament map URLs
                for fm in filament_maps_info:
                    fm["map_url"] += _cb

                artifact_progress.emit(
                    stage="artifacts",
                    stage_label="Assembling solve result...",
                    stage_index=1,
                    stage_pct=82,
                    local_pct=82,
                )
                solve["result"] = {
                    "card_id": card_id,
                    "mean_de": st.mean_de,
                    "source_rms_de": getattr(st, "source_rms_de", st.mean_de),
                    "max_de": st.max_de,
                    "de_scale_max": round((de_max * 1.05) if de_max > 0 else 0.35, 4),
                    "n_oog": st.n_out_of_gamut,
                    "total_pixels": st.total_pixels,
                    "coverage_pct": st.coverage_pct,
                    "image_w": st.image_w,
                    "image_h": st.image_h,
                    "image_domain_width_mm": result.image_domain_width_mm,
                    "image_domain_height_mm": result.image_domain_height_mm,
                    "max_height": st.max_height,
                    "predicted_url": f"/api/run-cache/files/predicted.png{_cb}",
                    "predicted_appearance_url": f"/api/run-cache/files/predicted_appearance.png{_cb}",
                    "predicted_color_only_appearance_url": (
                        f"/api/run-cache/files/predicted_color_only_appearance.png{_cb}"
                    ),
                    "de_map_url": f"/api/run-cache/files/de_map.png{_cb}",
                    "de_map_perceptual_url": f"/api/run-cache/files/de_perceptual.png{_cb}",
                    "de_map_maxset_url": f"/api/run-cache/files/de_maxset.png{_cb}",
                    "palette_fit_url": f"/api/run-cache/files/palette_fit.png{_cb}",
                    "solver_loss_map_url": f"/api/run-cache/files/solver_loss.png{_cb}",
                    "solver_loss_raw_url": f"/api/run-cache/files/solver_loss_raw.png{_cb}",
                    "solver_loss_scale_max": round(solver_loss_scale_max, 4),
                    "palette_fit_rms_de": getattr(result, "palette_fit_rms_de", None),
                    "solver_loss_rms_de": getattr(result, "solver_loss_rms_de", None),
                    "de_raw_url": f"/api/run-cache/files/de_raw.png{_cb}",
                    "source_url": f"/api/run-cache/files/source.png{_cb}",
                    "target_transmission_url": f"/api/run-cache/files/target_transmission.png{_cb}",
                    "reference_url": f"/api/run-cache/files/reference.png{_cb}",
                    "cap_map_url": f"/api/run-cache/files/cap_height.png{_cb}",
                    "cap_map_active_px": cap_map_active_px,
                    "cap_map_max_d": cap_map_max_d,
                    "cap_map_volume_mm3": (
                        round(cap_map_volume_mm3, 4)
                        if cap_map_volume_mm3 is not None
                        else None
                    ),
                    "cap_map_contour_bin_url": (
                        f"/api/run-cache/files/cap_map_contour.bin{_cb}"
                        if cap_map_contour_saved
                        else None
                    ),
                    "boundary_cap_map_url": (
                        f"/api/run-cache/files/boundary_cap_height.png{_cb}"
                        if boundary_cap_stats is not None
                        else None
                    ),
                    "boundary_cap_map_active_px": (
                        int(boundary_cap_stats["active_px"])
                        if boundary_cap_stats is not None
                        else 0
                    ),
                    "boundary_cap_map_max_d": (
                        float(boundary_cap_stats["max_d"])
                        if boundary_cap_stats is not None
                        else 0.0
                    ),
                    "boundary_cap_map_volume_mm3": (
                        round(boundary_cap_map_volume_mm3, 4)
                        if boundary_cap_map_volume_mm3 is not None
                        else None
                    ),
                    "boundary_cap_contour_bin_url": (
                        f"/api/run-cache/files/boundary_cap_contour.bin{_cb}"
                        if boundary_cap_contour_saved
                        else None
                    ),
                    "detail_cap_map_url": (
                        f"/api/run-cache/files/detail_cap_height.png{_cb}"
                        if detail_cap_stats is not None
                        else None
                    ),
                    "detail_cap_map_active_px": (
                        int(detail_cap_stats["active_px"])
                        if detail_cap_stats is not None
                        else 0
                    ),
                    "detail_cap_map_max_d": (
                        float(detail_cap_stats["max_d"])
                        if detail_cap_stats is not None
                        else 0.0
                    ),
                    "detail_cap_map_volume_mm3": (
                        round(detail_cap_map_volume_mm3, 4)
                        if detail_cap_map_volume_mm3 is not None
                        else None
                    ),
                    "detail_cap_contour_bin_url": (
                        f"/api/run-cache/files/detail_cap_contour.bin{_cb}"
                        if detail_cap_contour_saved
                        else None
                    ),
                    "white_cap_scale_max_d": round(float(white_cap_scale_max), 4),
                    "cap_surface_height_url": (
                        f"/api/run-cache/files/cap_surface_height.png{_cb}"
                        if cap_surface_height_stats is not None
                        else None
                    ),
                    "cap_surface_height_max_d": (
                        float(cap_surface_height_stats["max_d"])
                        if cap_surface_height_stats is not None
                        else 0.0
                    ),
                    "cap_surface_height_contour_bin_url": (
                        f"/api/run-cache/files/cap_surface_height_contour.bin{_cb}"
                        if cap_surface_height_contour_saved
                        else None
                    ),
                    "boundary_cap_surface_height_url": (
                        f"/api/run-cache/files/boundary_cap_surface_height.png{_cb}"
                        if boundary_cap_surface_height_stats is not None
                        else None
                    ),
                    "boundary_cap_surface_height_max_d": (
                        float(boundary_cap_surface_height_stats["max_d"])
                        if boundary_cap_surface_height_stats is not None
                        else 0.0
                    ),
                    "boundary_cap_surface_height_contour_bin_url": (
                        f"/api/run-cache/files/boundary_cap_surface_height_contour.bin{_cb}"
                        if boundary_cap_surface_height_contour_saved
                        else None
                    ),
                    "detail_cap_surface_height_url": (
                        f"/api/run-cache/files/detail_cap_surface_height.png{_cb}"
                        if detail_cap_surface_height_stats is not None
                        else None
                    ),
                    "detail_cap_surface_height_max_d": (
                        float(detail_cap_surface_height_stats["max_d"])
                        if detail_cap_surface_height_stats is not None
                        else 0.0
                    ),
                    "detail_cap_surface_height_contour_bin_url": (
                        f"/api/run-cache/files/detail_cap_surface_height_contour.bin{_cb}"
                        if detail_cap_surface_height_contour_saved
                        else None
                    ),
                    "color_ceiling_url": f"/api/run-cache/files/color_ceiling.png{_cb}",
                    "total_surface_url": f"/api/run-cache/files/total_surface.png{_cb}",
                    "color_ceiling_max_d": color_ceiling_max_d,
                    "total_surface_max_d": total_surface_max_d,
                    "color_ceiling_bin_url": f"/api/run-cache/files/color_ceiling.bin{_cb}",
                    "color_ceiling_contour_bin_url": f"/api/run-cache/files/color_ceiling_contour.bin{_cb}",
                    "total_surface_bin_url": f"/api/run-cache/files/total_surface.bin{_cb}",
                    "total_surface_contour_bin_url": f"/api/run-cache/files/total_surface_contour.bin{_cb}",
                    "cap_height_bin_url": f"/api/run-cache/files/cap_height.bin{_cb}",
                    "boundary_cap_height_bin_url": (
                        f"/api/run-cache/files/boundary_cap_height.bin{_cb}"
                        if boundary_cap_stats is not None
                        else None
                    ),
                    "detail_cap_height_bin_url": (
                        f"/api/run-cache/files/detail_cap_height.bin{_cb}"
                        if detail_cap_stats is not None
                        else None
                    ),
                    "filament_bin_urls": {
                        fid: f"/api/run-cache/files/filament_{fid}.bin{_cb}"
                        for fid in _written_thickness["filament_paths"]
                    },
                    "explorer_stack_label_bin_url": (
                        f"/api/run-cache/files/{_explorer_plan['filename']}{_cb}"
                        if _explorer_plan
                        else None
                    ),
                    "explorer_stack_table": (
                        _explorer_plan["stack_table"] if _explorer_plan else None
                    ),
                    "color_recipe_breakdown_json_url": (
                        f"/api/run-cache/files/{_color_recipe_breakdown['json_filename']}{_cb}"
                        if _color_recipe_breakdown
                        else None
                    ),
                    "color_recipe_breakdown_csv_url": (
                        f"/api/run-cache/files/{_color_recipe_breakdown['csv_filename']}{_cb}"
                        if _color_recipe_breakdown
                        else None
                    ),
                    "color_recipe_breakdown_cookbook_url": (
                        f"/api/run-cache/files/{_color_recipe_breakdown['cookbook_filename']}{_cb}"
                        if _color_recipe_breakdown
                        else None
                    ),
                    "color_recipe_breakdown_summary": (
                        _color_recipe_breakdown["summary"]
                        if _color_recipe_breakdown
                        else None
                    ),
                    "explorer_base_filament_id": _white_base(cfg),
                    "explorer_cap_filament_id": _white_cap(cfg),
                    "explorer_base_thickness_mm": float(cfg["d_wb"]),
                    "cap_quality": dict(getattr(result, "cap_quality", {})),
                    "staged_metrics": staged_metrics,
                    "debug_map_urls": debug_map_urls,
                    "filament_maps": filament_maps_info,
                    "solve_start_diagnostics": solve_start_diagnostics,
                    **overlay_urls,
                }
                solve["material_exposure_audit"] = material_exposure_audit
                solve["result"]["material_exposure_audit"] = material_exposure_audit
                solve["result"]["white_cap_exposure_safe"] = bool(
                    material_exposure_audit.get("passes", False)
                )

                solve["solve_owned_fingerprint"] = _solve_owned_fingerprint(cfg)
                artifact_progress.emit(
                    stage="artifacts",
                    stage_label="Persisting completed run...",
                    stage_index=1,
                    stage_pct=95,
                    local_pct=95,
                )
                _mark_phase("persist_artifacts")
                _phase_durations = {}
                _prev_t = start
                for _phase_name, _phase_t in _phase_marks:
                    _phase_durations[_phase_name] = round(_phase_t - _prev_t, 2)
                    _prev_t = _phase_t
                _phase_durations["total"] = round(time.time() - start, 2)
                logger.info("SOLVE_PHASE_TIMINGS %s", json.dumps(_phase_durations))
                if card_id:
                    entry = _write_completed_solve_cache_entry(card_id, cfg, solve, result)
                    _maybe_write_auto_run(card_id, entry["solve"], entry["config"])
                root_progress.emit(
                    stage="complete",
                    stage_label="Solve complete",
                    stage_index=solve_stage_count,
                    stage_pct=100,
                    local_pct=100,
                    check_cancel=False,
                )
                elapsed = time.time() - start
                logger.info(
                    "Solve complete: %dx%d (%d px), mean dE=%.4f, max dE=%.4f, %.1fs",
                    W, H, W * H, st.mean_de, st.max_de, elapsed,
                )

            except (SolveCancelled, ProgressCancelled):
                solve["status"] = "cancelled"
                solve["progress"] = {
                    **(solve.get("progress") or {}),
                    "stage_label": "Cancelled",
                    "elapsed_s": max(0.0, time.monotonic() - started_monotonic),
                    "job_id": job_id,
                }
                logger.info("Solve cancelled by user after %.1fs", time.time() - start)

            except Exception as exc:
                solve["status"] = "error"
                solve["progress"] = {
                    **(solve.get("progress") or {}),
                    "stage_label": str(exc),
                    "elapsed_s": max(0.0, time.monotonic() - started_monotonic),
                    "job_id": job_id,
                }
                logger.exception("Solve failed")

            finally:
                solve["cancel_requested"] = False
                solve["elapsed_s"] = time.time() - start
                solve["started_monotonic"] = None

    thread = threading.Thread(target=_run_solve, daemon=True)
    thread.start()
    return {"job_id": job_id, "status": "running"}


@app.get("/api/solve/status")
def solve_status() -> dict:
    """Poll solve status."""
    return _serialize_solve_status(session["solve"])


@app.post("/api/solve/cancel")
def cancel_solve(job_id: str | None = None) -> dict:
    """Request cancellation of a running solve."""
    solve = session["solve"]
    active_job_id = str(solve.get("job_id") or "")
    if job_id and job_id != active_job_id:
        raise HTTPException(409, "Solve job no longer matches the active job")
    if solve["status"] != "running":
        return {
            "cancelled": False,
            "reason": "not running",
            "job_id": active_job_id or None,
        }
    solve["cancel_requested"] = True
    return {"requested": True, "job_id": active_job_id}


# ── Export ────────────────────────────────────────────────────────────────


def _progress_dict_from_export_event(event: ExportProgressEvent) -> dict[str, Any]:
    fraction = event.fraction_complete
    indeterminate = fraction is None
    resolved_fraction = 0.0 if indeterminate else max(0.0, min(1.0, float(fraction)))
    stage_count = max(int(event.stage_count), 1)
    stage_index = max(1, min(int(event.stage_index), stage_count))
    overall = ((stage_index - 1) + resolved_fraction) / float(stage_count)
    label = event.message or event.stage_label
    return {
        "stage_id": event.stage_id,
        "stage_label": str(label),
        "stage_title": event.stage_label,
        "stage_index": stage_index,
        "stage_count": stage_count,
        "stage_pct": None if indeterminate else round(max(0.0, min(100.0, overall * 100.0)), 1),
        "stage_fraction_pct": None if indeterminate else round(resolved_fraction * 100.0, 1),
        "indeterminate": indeterminate,
        "elapsed_s": float(event.elapsed_seconds),
        "message": str(event.message or ""),
    }


def _mesh_object_manifest_summary(obj: Any) -> dict[str, Any]:
    return {
        "object_key": str(getattr(obj, "object_key", "")),
        "material_key": str(getattr(obj, "material_key", "")),
        "role": str(getattr(obj, "role", "")),
        "mesh_style": str(getattr(obj, "mesh_style", "")),
        "vertices": int(getattr(obj, "vertices").shape[0]),
        "faces": int(getattr(obj, "faces").shape[0]),
    }


def _write_3mf_packaging_manifest_update(
    *,
    manifest_path: Path,
    manifest: dict[str, Any],
    package_bundle: Any,
    package_report: dict[str, Any],
    package_path: Path,
) -> dict[str, Any]:
    updated = deepcopy(dict(manifest))
    format_packaging = dict(updated.get("format_packaging") or {})
    format_packaging["3mf"] = {
        "package_path": str(package_path),
        "package_filename": str(package_path.name),
        "quality_policy": "core_quality_preserved_package_mesh_not_revalidated",
        "color_quarantine_coalescence": dict(package_report),
        "packaged_objects": [
            _mesh_object_manifest_summary(obj)
            for obj in getattr(package_bundle, "objects", ())
            if not getattr(obj, "is_empty", False)
        ],
    }
    updated["format_packaging"] = format_packaging
    write_export_manifest(manifest_path, updated)
    return updated


def _discard_export_stage(stage_dir: Path) -> None:
    try:
        if stage_dir.exists():
            shutil.rmtree(stage_dir)
    except OSError:
        logger.warning("Could not remove failed export staging directory %s", stage_dir, exc_info=True)


def _remap_staged_export_paths(value: Any, *, stage_dir: Path, final_dir: Path) -> Any:
    if isinstance(value, dict):
        return {
            key: _remap_staged_export_paths(item, stage_dir=stage_dir, final_dir=final_dir)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _remap_staged_export_paths(item, stage_dir=stage_dir, final_dir=final_dir)
            for item in value
        ]
    if isinstance(value, tuple):
        return tuple(
            _remap_staged_export_paths(item, stage_dir=stage_dir, final_dir=final_dir)
            for item in value
        )
    if not isinstance(value, str):
        return value
    try:
        path = Path(value)
        if not path.is_absolute():
            return value
        relative = path.relative_to(stage_dir)
    except (OSError, ValueError):
        return value
    return str(final_dir / relative)


def _finalize_staged_export(
    *,
    solve: dict[str, Any],
    cfg: dict[str, Any],
    export_thickness_maps: dict[str, np.ndarray],
    ordering: list[str],
    result: Any,
    response_manifest: dict[str, Any],
    staged_threemf_path: Path | None,
    stage_dir: Path,
    final_dir: Path,
    export_id: str,
    target_card_id: str,
    geometry_source: str,
    field_scale: int,
    output_format: str,
    export_start: float,
    progress_callback: Callable[[ExportProgressEvent], None] | None,
    cancel_check: Callable[[], None] | None,
) -> dict[str, Any]:
    if cancel_check is not None:
        cancel_check()
    swap_payload = _build_swap_instruction_payload(
        solve=solve,
        cfg=cfg,
        export_thickness_maps=export_thickness_maps,
        ordering=ordering,
    )
    staged_swap_path: Path | None = None
    if bool(swap_payload.get("available", True)):
        staged_swap_path = stage_dir / "swap_instructions.txt"
        staged_swap_path.write_text(swap_payload["instructions"], encoding="utf-8")

    response_manifest = _remap_staged_export_paths(
        response_manifest,
        stage_dir=stage_dir,
        final_dir=final_dir,
    )
    write_export_manifest(result.manifest_path, response_manifest)

    def published_path(staged_path: Path) -> Path:
        return final_dir / staged_path.relative_to(stage_dir)

    def file_url(rel_name: str) -> str:
        base = f"/api/export/files/{quote(rel_name, safe='/')}"
        return f"{base}?dir={export_id}"

    files = []
    for obj in result.bundle.objects:
        if cancel_check is not None:
            cancel_check()
        staged_path = result.output_paths.get(obj.object_key)
        if staged_path is None:
            continue
        size_mb = staged_path.stat().st_size / (1024 * 1024)
        quality = dict(result.bundle.quality.get(obj.object_key, {}))
        rel_name = staged_path.relative_to(stage_dir).as_posix()
        files.append({
            "name": rel_name,
            "abs_path": str(published_path(staged_path)),
            "url": file_url(rel_name),
            "size_kb": round(size_mb * 1024, 1),
            "size_mb": round(size_mb, 2),
            "n_faces": quality.get("n_faces", 0),
            "watertight": quality.get("is_watertight", False),
            "has_holes": quality.get("has_holes", True),
            "n_pinch_edges": quality.get("n_pinch_edges", 0),
            "material_key": obj.material_key,
            "role": obj.role,
            "mesh_style": obj.mesh_style,
        })

    staged_manifest_path = result.manifest_path
    manifest_rel = staged_manifest_path.relative_to(stage_dir).as_posix()
    manifest_size_mb = staged_manifest_path.stat().st_size / (1024 * 1024)
    files.append({
        "name": manifest_rel,
        "abs_path": str(published_path(staged_manifest_path)),
        "url": file_url(manifest_rel),
        "size_kb": round(manifest_size_mb * 1024, 1),
        "size_mb": round(manifest_size_mb, 2),
        "n_faces": 0,
        "watertight": True,
        "has_holes": False,
        "n_pinch_edges": 0,
        "material_key": "",
        "role": "manifest",
        "mesh_style": "json",
    })
    if staged_swap_path is not None:
        swap_rel = staged_swap_path.relative_to(stage_dir).as_posix()
        swap_size_mb = staged_swap_path.stat().st_size / (1024 * 1024)
        files.append({
            "name": swap_rel,
            "abs_path": str(published_path(staged_swap_path)),
            "url": file_url(swap_rel),
            "size_kb": round(swap_size_mb * 1024, 1),
            "size_mb": round(swap_size_mb, 2),
            "n_faces": 0,
            "watertight": True,
            "has_holes": False,
            "n_pinch_edges": 0,
            "material_key": "",
            "role": "swap_instructions",
            "mesh_style": "txt",
        })

    threemf_url = None
    if staged_threemf_path is not None:
        threemf_rel = staged_threemf_path.relative_to(stage_dir).as_posix()
        threemf_url = file_url(threemf_rel)
        threemf_size_mb = staged_threemf_path.stat().st_size / (1024 * 1024)
        files.append({
            "name": threemf_rel,
            "abs_path": str(published_path(staged_threemf_path)),
            "url": threemf_url,
            "size_kb": round(threemf_size_mb * 1024, 1),
            "size_mb": round(threemf_size_mb, 2),
            "n_faces": 0,
            "watertight": True,
            "has_holes": False,
            "n_pinch_edges": 0,
            "material_key": "",
            "role": "3mf_package",
            "mesh_style": "3mf",
        })

    if progress_callback is not None:
        progress_callback(
            ExportProgressEvent(
                stage_id="publish_outputs",
                stage_label="Publish outputs",
                stage_index=10,
                stage_count=10,
                elapsed_seconds=time.time() - export_start,
                fraction_complete=0.0,
                message="publishing completed export",
            )
        )

    if final_dir.exists():
        raise FileExistsError(f"Export destination appeared during publication: {final_dir}")
    response = {
        "files": files,
        "out_dir": str(final_dir),
        "card_id": target_card_id,
        "export_id": export_id,
        "zip_url": f"/api/export/files-zip?dir={export_id}",
        "manifest_url": file_url(manifest_rel),
        "threemf_url": threemf_url,
        "geometry_source": geometry_source,
        "field_scale": field_scale,
        "output_format": output_format,
        "swap_plan": swap_payload,
        "manifest": response_manifest,
        "progress_events": [event.as_dict() for event in result.progress_events],
    }
    logger.info(
        "Post-solve export ready for publication: %d files, geometry=%s, format=%s, %.1fs",
        len(files),
        geometry_source,
        output_format,
        time.time() - export_start,
    )
    if cancel_check is not None:
        cancel_check()
    # This rename is the publication commit point. Keep the successful path
    # after it deliberately free of callbacks, cancellation checks, and logging.
    stage_dir.rename(final_dir)
    return response


def _perform_export_files(
    payload: ExportFilesPayload,
    *,
    progress_callback: Callable[[ExportProgressEvent], None] | None = None,
    cancel_check: Callable[[], None] | None = None,
) -> dict:
    """Export print files from the selected/current solve result."""
    if cancel_check is not None:
        cancel_check()
    solve, cfg, target_card_id = _resolve_export_target(payload.card_id)

    if solve["status"] != "complete":
        raise HTTPException(400, "No completed solve to export")

    if payload.card_id is None:
        current_fp = _solve_owned_fingerprint(cfg)
        if solve.get("solve_owned_fingerprint") != current_fp:
            raise HTTPException(
                409,
                "Solve is stale — a solve-owned setting changed since the last "
                "solve. Re-solve before exporting.",
            )

    thickness_maps = solve["thickness_maps"]
    if thickness_maps is None:
        raise HTTPException(400, "No thickness maps available")
    export_thickness_maps, ordering = _prepare_export_materialization(cfg, thickness_maps)
    if cancel_check is not None:
        cancel_check()

    # Validate every option that can fail before creating a private publication
    # directory. Once the stage exists, all subsequent failures must pass through
    # the cleanup block below.
    geometry_source = normalize_geometry_source(payload.geometry_source).value
    field_scale = int(payload.field_scale)
    output_format = payload.output_format

    from datetime import datetime
    from run_naming import make_export_id
    export_timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    export_id = make_export_id(cfg.get("image_path", ""), _OUTPUT_DIR, timestamp=export_timestamp)
    _validate_card_id(export_id)
    final_out = _OUTPUT_DIR / export_id
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out = _OUTPUT_DIR / f".export-stage-{export_id}-{uuid.uuid4().hex[:8]}"
    out.mkdir()

    try:
        logger.info(
            "Post-solve export started: geometry=%s, field_scale=%dx, format=%s, filaments=%d",
            geometry_source,
            field_scale,
            output_format,
            len(ordering),
        )
        export_start = time.time()
        if cancel_check is not None:
            cancel_check()
        if progress_callback is not None:
            progress_callback(
                ExportProgressEvent(
                    stage_id="load_bundle",
                    stage_label="Load bundle",
                    stage_index=1,
                    stage_count=10,
                    elapsed_seconds=time.time() - export_start,
                    fraction_complete=0.0,
                    message="materializing solved export bundle",
                )
            )
        bundle_dir = _materialize_post_solve_export_bundle_from_cached_solve(
            card_id=target_card_id,
            solve=solve,
            cfg=cfg,
            thickness_maps=export_thickness_maps,
            ordering=ordering,
        )
        if cancel_check is not None:
            cancel_check()
        result = export_solve_bundle(
            bundle_path=bundle_dir,
            out_dir=out,
            geometry_source=geometry_source,
            field_reconstruction_config=FieldWhiteReconstructionConfig(field_scale=field_scale),
            write_stls=(output_format == "stls"),
            validate_written_meshes=bool(payload.validate_written_meshes),
            progress_callback=progress_callback,
            cancel_check=cancel_check,
        )
        response_manifest = dict(result.manifest)
        threemf_path: Path | None = None
        if output_format == "3mf":
            if progress_callback is not None:
                progress_callback(
                    ExportProgressEvent(
                        stage_id="serialize_outputs",
                        stage_label="Serialize outputs",
                        stage_index=7,
                        stage_count=10,
                        elapsed_seconds=time.time() - export_start,
                        fraction_complete=0.5,
                        message="packaging 3MF",
                    )
                )
            material_keys: list[str] = []
            for obj in result.bundle.objects:
                if obj.material_key not in material_keys:
                    material_keys.append(obj.material_key)
            package_bundle, package_report = coalesce_color_quarantine_for_3mf_bundle(
                result.bundle,
                cancel_check=cancel_check,
            )

            def _threemf_progress(idx: int, total: int, obj: Any) -> None:
                if progress_callback is not None:
                    progress_callback(
                        ExportProgressEvent(
                            stage_id="serialize_outputs",
                            stage_label="Serialize outputs",
                            stage_index=7,
                            stage_count=10,
                            elapsed_seconds=time.time() - export_start,
                            fraction_complete=0.5 + 0.5 * (idx / max(total, 1)),
                            message=f"packaging 3MF {idx}/{total}: {getattr(obj, 'object_key', 'object')}",
                        )
                    )

            threemf_path = write_export_mesh_bundle_as_3mf(
                package_bundle,
                # Name the single-file 3MF after its export folder ({export_id}.3mf)
                # so multiple lithophanes don't collide in one slicer project.
                out / f"{export_id}.3mf",
                filament_assignments=_filament_assignments_for_export(material_keys, cfg),
                material_display_names=_material_display_names_for_export(material_keys, cfg),
                progress_callback=_threemf_progress,
                cancel_check=cancel_check,
                verbose=False,
            )
            response_manifest = _write_3mf_packaging_manifest_update(
                manifest_path=result.manifest_path,
                manifest=response_manifest,
                package_bundle=package_bundle,
                package_report=package_report,
                package_path=threemf_path,
            )
        return _finalize_staged_export(
            solve=solve,
            cfg=cfg,
            export_thickness_maps=export_thickness_maps,
            ordering=ordering,
            result=result,
            response_manifest=response_manifest,
            staged_threemf_path=threemf_path,
            stage_dir=out,
            final_dir=final_out,
            export_id=export_id,
            target_card_id=target_card_id,
            geometry_source=geometry_source,
            field_scale=field_scale,
            output_format=output_format,
            export_start=export_start,
            progress_callback=progress_callback,
            cancel_check=cancel_check,
        )
    except HTTPException:
        _discard_export_stage(out)
        raise
    except ExportCancelled:
        _discard_export_stage(out)
        raise
    except ExportPreparationError as exc:
        _discard_export_stage(out)
        raise HTTPException(409, str(exc))
    except Exception as exc:
        _discard_export_stage(out)
        logger.exception("Post-solve export failed")
        raise HTTPException(500, f"Export failed: {exc}")
    except BaseException:
        _discard_export_stage(out)
        raise


@app.post("/api/export/files")
def export_files(payload: ExportFilesPayload) -> dict:
    """Synchronous export endpoint retained for direct API callers/tests."""
    _require_model_library()
    export = session["export"]
    _reserve_model_job(
        "export",
        already_running="Export already running",
        state={
            "status": "running",
            "cancel_requested": False,
            "job_id": None,
            "result": None,
        },
    )
    try:
        return _perform_export_files(payload)
    finally:
        with _MODEL_RESOURCE_COORDINATION_LOCK:
            if export.get("job_id") is None:
                export["status"] = "idle"


@app.post("/api/export/files/start")
def export_files_start(payload: ExportFilesPayload) -> dict:
    """Start a print-file export in the background so the UI can poll progress."""
    _require_model_library()
    export = session["export"]

    job_id = str(uuid.uuid4())
    _reserve_model_job(
        "export",
        already_running="Export already running",
        state={
            "status": "running",
            "progress": {"stage_label": "Starting export...", "stage_pct": 0.0},
            "elapsed_s": 0.0,
            "result": None,
            "cancel_requested": False,
            "job_id": job_id,
        },
    )
    start_time = time.time()

    def _run_export() -> None:
        def _check_cancel() -> None:
            with _MODEL_RESOURCE_COORDINATION_LOCK:
                current = session["export"]
                if current.get("job_id") != job_id or current.get("cancel_requested"):
                    raise ExportCancelled()

        def _finish(status: str, label: str, *, result: Any = None) -> None:
            with _MODEL_RESOURCE_COORDINATION_LOCK:
                current = session["export"]
                if current.get("job_id") != job_id:
                    return
                elapsed = time.time() - start_time
                if status == "complete" and current.get("cancel_requested"):
                    label = "Completed before cancellation took effect"
                current["status"] = status
                current["elapsed_s"] = elapsed
                current["progress"] = {
                    "stage_label": label,
                    "stage_pct": 100.0 if status == "complete" else current.get("progress", {}).get("stage_pct"),
                    "elapsed_s": elapsed,
                }
                current["result"] = result

        try:
            def _progress(event: ExportProgressEvent) -> None:
                with _MODEL_RESOURCE_COORDINATION_LOCK:
                    current = session["export"]
                    if current.get("job_id") != job_id or current.get("cancel_requested"):
                        raise ExportCancelled()
                    current["elapsed_s"] = time.time() - start_time
                    current["progress"] = _progress_dict_from_export_event(event)

            result = _perform_export_files(
                payload,
                progress_callback=_progress,
                cancel_check=_check_cancel,
            )
            _finish("complete", "Export complete", result=result)
        except ExportCancelled:
            _finish("cancelled", "Cancelled")
            logger.info("Post-solve export cancelled after %.1fs", time.time() - start_time)
        except HTTPException as exc:
            _finish("error", str(exc.detail))
            logger.warning("Post-solve export failed: %s", exc.detail)
        except ExportPreparationError as exc:
            _finish("error", str(exc))
            logger.warning("Post-solve export preparation failed: %s", exc)
        except Exception as exc:
            _finish("error", f"Export failed: {exc}")
            logger.exception("Post-solve export failed")

    thread = threading.Thread(target=_run_export, daemon=True)
    thread.start()
    return {"job_id": job_id, "status": "running"}


@app.get("/api/export/files/status")
def export_files_status() -> dict:
    """Poll print-file export progress."""
    with _MODEL_RESOURCE_COORDINATION_LOCK:
        export = dict(session["export"])
        progress = dict(export.get("progress") or {})
    if isinstance(progress, dict):
        progress_label = progress.get("stage_label", "")
    else:
        progress_label = str(progress)
    return {
        "job_id": export.get("job_id"),
        "status": export.get("status", "idle"),
        "progress": progress_label,
        "progress_detail": progress if isinstance(progress, dict) else {},
        "elapsed_s": export.get("elapsed_s", 0.0),
        "result": export.get("result"),
        "cancel_requested": bool(export.get("cancel_requested")),
    }


@app.post("/api/export/files/cancel")
def export_files_cancel(job_id: str | None = None) -> dict:
    """Request cancellation of a running print-file export."""
    with _MODEL_RESOURCE_COORDINATION_LOCK:
        export = session["export"]
        current_job_id = str(export.get("job_id") or "")
        if job_id and job_id != current_job_id:
            raise HTTPException(409, "Export job id does not match the active job")
        if export.get("status") == "cancelling":
            return {
                "cancelled": True,
                "job_id": current_job_id,
                "status": "cancelling",
                "cancel_requested": True,
            }
        if export.get("status") == "cancelled" and current_job_id:
            return {
                "cancelled": True,
                "job_id": current_job_id,
                "status": "cancelled",
                "cancel_requested": True,
            }
        if export.get("status") != "running" or not current_job_id:
            return {"cancelled": False, "reason": "not running"}
        export["cancel_requested"] = True
        export["status"] = "cancelling"
        progress = dict(export.get("progress") or {})
        progress["stage_label"] = "Cancelling after current safe point"
        export["progress"] = progress
        return {
            "cancelled": True,
            "job_id": current_job_id,
            "status": "cancelling",
            "cancel_requested": True,
        }


def _build_static_swap_instruction_payload(
    *,
    solve: dict,
    cfg: dict,
    export_thickness_maps: dict,
    ordering: list[str],
    ams_slots: int,
    white_slots: int,
) -> dict:
    """Return the no-swap slot table for palettes that fit in one load."""

    color_slots = int(ams_slots) - int(white_slots)
    if color_slots < 1:
        raise HTTPException(409, "Active printer has no color slots available")

    filaments = [
        fid for fid in ordering
        if fid in export_thickness_maps and not str(fid).startswith("__")
    ]
    if len(filaments) > color_slots:
        raise HTTPException(
            409,
            "Solved run has no swap-banding plan but exceeds active printer color-slot capacity",
        )

    slot_assignments = {fid: index + 1 for index, fid in enumerate(filaments)}
    slot_table = {
        str(slot): filaments[slot - 1] if slot <= len(filaments) else "unused - leave loaded"
        for slot in range(1, color_slots + 1)
    }
    white_slot_numbers = list(range(color_slots + 1, int(ams_slots) + 1))
    if not white_slot_numbers:
        raise HTTPException(409, "Active printer has no dedicated white slot available")

    white_base = _white_base(cfg)
    white_cap = _white_cap(cfg)
    color_ceiling = _compute_color_ceiling(export_thickness_maps, cfg["d_wb"])
    color_top = float(np.max(color_ceiling)) if color_ceiling.size else float(cfg["d_wb"])
    wc_map = np.asarray(export_thickness_maps[MapKey.WHITE_CAP], dtype=np.float32)
    lith_height = float(np.max(color_ceiling + wc_map)) if wc_map.size else color_top
    has_border = bool(cfg["border"] and cfg["border_width_mm"] > 0)
    width_mm = float(solve["image_domain_width_mm"]) + (
        2.0 * float(cfg["border_width_mm"]) if has_border else 0.0
    )
    height_mm = float(solve["image_domain_height_mm"]) + (
        2.0 * float(cfg["border_width_mm"]) if has_border else 0.0
    )
    total_height = max(lith_height, float(cfg["border_height_mm"])) if has_border else lith_height

    lines = [
        "=== Prisma Filament Swap Instructions ===",
        "",
        f"Print dimensions: {width_mm:.1f} x {height_mm:.1f} mm",
        f"Total height: {total_height:.2f} mm",
        f"Color filaments: {len(filaments)} (1 load, 0 mid-print swaps needed)",
        "",
        "--- LOAD 1 (entire print) ---",
    ]
    for slot in range(1, color_slots + 1):
        lines.append(f"AMS Slot {slot}: {slot_table[str(slot)]}")

    if white_cap == white_base or len(white_slot_numbers) == 1:
        lines.append(
            f"AMS Slot {white_slot_numbers[-1]}: {white_base}  "
            "[FIXED white base + cap - do not change]"
        )
    else:
        lines.append(
            f"AMS Slot {white_slot_numbers[-1]}: {white_base}  "
            "[FIXED white base - do not change]"
        )
        lines.append(
            f"AMS Slot {white_slot_numbers[-2]}: {white_cap}  "
            "[FIXED white cap - do not change]"
        )
    lines.extend(["", "No mid-print swaps needed - all filaments fit in one AMS load.", ""])

    groups_json = [
        {
            "group_index": 0,
            "filaments": filaments,
            "z_start": round(float(cfg["d_wb"]), 3),
            "z_end": round(color_top, 3),
            "slot_assignments": slot_assignments,
            "slot_table": slot_table,
        }
    ] if filaments else []

    return {
        "available": True,
        "banded": False,
        "groups": groups_json,
        "pause_z_mm": [],
        "instructions": "\n".join(lines),
        "gcode": "; No filament swaps needed\n",
    }


def _build_swap_instruction_payload(
    *,
    solve: dict,
    cfg: dict,
    export_thickness_maps: dict,
    ordering: list[str],
) -> dict:
    has_border = cfg["border"] and cfg["border_width_mm"] > 0

    # AMS capacity is export-time printer state, not solve-owned geometry.
    # A cached solve may have been created under a different printer profile,
    # but swap instructions should reflect the user's currently selected AMS.
    current_cfg = _cfg()
    ams_slots = current_cfg.get("ams_slots", cfg.get("ams_slots", 4))
    white_slots = current_cfg.get("white_slots", cfg.get("white_slots", 1))

    availability = _swap_plan_availability_from_solve(solve)
    if availability is not None and not bool(availability.get("available", True)):
        reason = str(availability.get("reason") or "swap plan unavailable")
        return {
            "available": False,
            "reason": reason,
            "groups": [],
            "instructions": f"No swap plan available: {reason}\n",
            "gcode": "; Swap plan unavailable\n",
        }

    swap_grouping = _swap_grouping_from_solve(solve)
    try:
        band_plan = banded_export_plan_from_metadata(
            swap_grouping,
            d_wb_mm=float(cfg["d_wb"]),
            layer_height_mm=float(cfg["layer_height"]),
            expected_palette=ordering,
        )
    except ValueError as exc:
        raise HTTPException(409, f"Invalid swap grouping on solved run: {exc}") from exc

    if solve.get("image_domain_width_mm") is None or solve.get("image_domain_height_mm") is None:
        raise HTTPException(status_code=500, detail="Solve completed without physical domain dimensions")

    wc_map = export_thickness_maps[MapKey.WHITE_CAP]
    if band_plan is not None:
        color_slots = int(ams_slots) - int(white_slots)
        try:
            slot_tables = band_slot_tables(band_plan, color_slots=color_slots)
        except ValueError as exc:
            raise HTTPException(
                409,
                "Active printer cannot execute this solved swap-banded run: " + str(exc),
            ) from exc
        try:
            instructions = generate_swap_instructions(
                band_plan,
                cfg["d_wb"],
                wc_map,
                cfg["layer_height"],
                solve["image_domain_width_mm"],
                solve["image_domain_height_mm"],
                white_base=_white_base(cfg),
                white_cap=_white_cap(cfg),
                ams_slots=ams_slots,
                white_slots=white_slots,
                border_width_mm=cfg["border_width_mm"] if has_border else 0.0,
                border_height_mm=cfg["border_height_mm"] if has_border else 0.0,
            )
        except ValueError as exc:
            raise HTTPException(409, "Active printer cannot execute this swap-banded run: " + str(exc)) from exc
        groups_json = [
            {
                "group_index": index,
                "filaments": list(group),
                "z_start": round(band_plan.band_floor_mm(index), 3),
                "z_end": round(band_plan.band_ceiling_mm(index), 3),
                "slot_assignments": {
                    fid: slot
                    for slot, fid in slot_tables[index].items()
                    if fid is not None
                },
                "slot_table": {
                    str(slot): fid if fid is not None else "unused - leave loaded"
                    for slot, fid in slot_tables[index].items()
                },
            }
            for index, group in enumerate(band_plan.groups)
        ]
        return {
            "available": True,
            "banded": True,
            "groups": groups_json,
            "pause_z_mm": list(band_plan.pause_z_mm),
            "instructions": instructions,
            "gcode": generate_orcaslicer_pause_gcode(band_plan),
        }

    return _build_static_swap_instruction_payload(
        solve=solve,
        cfg=cfg,
        export_thickness_maps=export_thickness_maps,
        ordering=ordering,
        ams_slots=ams_slots,
        white_slots=white_slots,
    )


@app.get("/api/export/swap-instructions")
def get_swap_instructions(card_id: str | None = None) -> dict:
    """Get AMS swap plan for the selected/current export target."""
    solve, cfg, _target_card_id = _resolve_export_target(card_id)

    if solve["status"] != "complete":
        raise HTTPException(400, "No completed solve available for swap instructions")
    if card_id is None:
        current_fp = _solve_owned_fingerprint(cfg)
        if solve.get("solve_owned_fingerprint") != current_fp:
            raise HTTPException(
                409,
                "Solve is stale — a solve-owned setting changed since the last "
                "solve. Re-solve before generating swap instructions.",
            )

    thickness_maps = solve["thickness_maps"]
    if thickness_maps is None:
        raise HTTPException(400, "No thickness maps available")
    export_thickness_maps, ordering = _prepare_export_materialization(cfg, thickness_maps)

    return _build_swap_instruction_payload(
        solve=solve,
        cfg=cfg,
        export_thickness_maps=export_thickness_maps,
        ordering=ordering,
    )


@app.get("/api/export/files-zip")
def download_export_files_zip(dir: str | None = None):
    """Return a ZIP of the generated export files and manifest."""
    import io
    import zipfile

    out_dir = _export_out_dir(dir) if dir else _OUTPUT_DIR
    files = sorted((out_dir / "stls").glob("*.stl")) if (out_dir / "stls").exists() else []
    # The single-file 3MF is named after its export folder ({export_id}.3mf).
    for extra in (
        out_dir / "export_manifest.json",
        out_dir / "swap_instructions.txt",
        out_dir / f"{out_dir.name}.3mf",
    ):
        if extra.exists():
            files.append(extra)
    if not files:
        raise HTTPException(404, "No export files in output directory")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in files:
            zf.write(str(p), arcname=p.relative_to(out_dir).as_posix())
    buf.seek(0)

    headers = {"Content-Disposition": 'attachment; filename="lithophane_export.zip"'}
    return StreamingResponse(buf, media_type="application/zip", headers=headers)


def _serve_file_response(path: Path):
    """Serve a file on disk with an appropriate media type (shared by export + run-cache routes)."""
    suffix = path.suffix.lower()
    media_types = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".stl": "application/octet-stream",
        ".3mf": "application/octet-stream",
        ".txt": "text/plain",
        ".json": "application/json",
        ".bin": "application/octet-stream",
    }
    media_type = media_types.get(suffix, "application/octet-stream")
    return FileResponse(path, media_type=media_type)


@app.get("/api/export/files/{filename:path}")
def serve_export_file(filename: str, dir: str | None = None):
    """Serve an exported file (STL, PNG, etc.) from the output directory.

    Uses `:path` converter so subdirectory paths like
    `progressive/preprocess/<op>.png` reach this handler; `_safe_path` still rejects
    traversal attempts via `is_relative_to()`.
    """
    out_dir = _export_out_dir(dir) if dir else _OUTPUT_DIR
    path = _safe_path(out_dir, filename)
    if not path.exists():
        raise HTTPException(404, f"File not found: {filename}")
    return _serve_file_response(path)


@app.post("/api/export/files/open-folder")
def open_export_folder(payload: ExportFolderPayload) -> dict:
    out_dir = _export_out_dir(payload.export_id)
    if not out_dir.is_dir():
        raise HTTPException(404, "That export folder is no longer available")
    try:
        open_folder_in_file_manager(out_dir)
    except OSError as exc:
        raise HTTPException(500, f"Could not open the export folder: {exc}") from exc
    return {"opened": True, "export_id": payload.export_id}


@app.get("/api/run-cache/files/{filename:path}")
def serve_run_cache_file(filename: str, run: str | None = None):
    """Serve a solve diagnostic / progressive / preview file from the run cache."""
    out_dir = _run_cache_dir(run)
    path = _safe_path(out_dir, filename)
    if not path.exists():
        raise HTTPException(404, f"File not found: {filename}")
    return _serve_file_response(path)


# ---------------------------------------------------------------------------
# Cache management endpoints (Stage 9a Slice 3)
# ---------------------------------------------------------------------------

def _assert_no_active_job(*, action: str = "clear cache") -> None:
    """Raise HTTP 409 if a long-running Generator job owns runtime resources."""
    with _MODEL_RESOURCE_COORDINATION_LOCK:
        if action != "manage model libraries" and _MODEL_LIBRARY_OPERATION_LOCK.locked():
            raise HTTPException(409, "Cannot clear cache while a model-library operation is running")
        for key in ("solve", "export", "compare", "suggest"):
            if session.get(key, {}).get("status") in _ACTIVE_MODEL_JOB_STATUSES:
                raise HTTPException(409, f"Cannot {action} while a {key} job is running")


def _reserve_model_job(key: str, *, already_running: str, state: dict[str, Any]) -> None:
    with _MODEL_RESOURCE_COORDINATION_LOCK:
        if _RESTART_REQUESTED.is_set():
            raise HTTPException(409, "Prisma is restarting")
        if _MODEL_LIBRARY_OPERATION_LOCK.locked():
            raise HTTPException(409, "A model-library operation is currently running")
        if session.get(key, {}).get("status") in _ACTIVE_MODEL_JOB_STATUSES:
            raise HTTPException(409, already_running)
        session[key].update(state)


def _library_status() -> dict:
    status = _MODEL_LIBRARY_STORE.list()
    selected = status.get("active_library_id")
    status["runtime_active_library_id"] = _ACTIVE_MODEL_LIBRARY_ID
    status["restart_required"] = selected != _ACTIVE_MODEL_LIBRARY_ID
    for item in status["libraries"]:
        item["runtime_active"] = bool(
            item.get("valid") and item.get("library_id") == _ACTIVE_MODEL_LIBRARY_ID
        )
        item["selected_for_next_launch"] = bool(item.get("active"))
    return status


def _begin_library_mutation() -> None:
    with _MODEL_RESOURCE_COORDINATION_LOCK:
        if _RESTART_REQUESTED.is_set():
            raise HTTPException(409, "Prisma is restarting")
        _assert_no_active_job(action="manage model libraries")
        if not _MODEL_LIBRARY_OPERATION_LOCK.acquire(blocking=False):
            raise HTTPException(409, "Another model-library operation is already running")


@app.get("/api/model-libraries")
def list_model_libraries() -> dict:
    return _library_status()


@app.post("/api/model-libraries/install")
def install_model_library(package: UploadFile = File(...)) -> dict:
    _begin_library_mutation()
    upload_root = data_paths.CACHE_DIR / "library-imports"
    temporary = upload_root / f".upload-{uuid.uuid4().hex}.zip"
    try:
        upload_root.mkdir(parents=True, exist_ok=True)
        with temporary.open("xb") as output:
            while True:
                block = package.file.read(1024 * 1024)
                if not block:
                    break
                output.write(block)
        report = _MODEL_LIBRARY_STORE.install(temporary)
        return {"installed": report, "status": _library_status()}
    except ModelLibraryStoreError as exc:
        raise HTTPException(400, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(500, f"Could not stage the model-library upload: {exc}") from exc
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                logger.warning("Could not remove staged model-library upload: %s", temporary)
        _MODEL_LIBRARY_OPERATION_LOCK.release()


@app.post("/api/model-libraries/activate")
def activate_model_library(payload: ModelLibraryIdPayload) -> dict:
    _begin_library_mutation()
    try:
        report = _MODEL_LIBRARY_STORE.activate(payload.library_id)
        return {
            "activated_for_next_launch": report,
            "restart_required": payload.library_id != _ACTIVE_MODEL_LIBRARY_ID,
            "status": _library_status(),
        }
    except ModelLibraryStoreError as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        _MODEL_LIBRARY_OPERATION_LOCK.release()


@app.post("/api/model-libraries/remove")
def remove_model_library(payload: ModelLibraryIdPayload) -> dict:
    _begin_library_mutation()
    try:
        if payload.library_id == _ACTIVE_MODEL_LIBRARY_ID:
            raise ModelLibraryStoreError(
                "the library currently loaded by this running Generator cannot be removed; restart first"
            )
        _MODEL_LIBRARY_STORE.remove(payload.library_id)
        return {"removed_library_id": payload.library_id, "status": _library_status()}
    except ModelLibraryStoreError as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        _MODEL_LIBRARY_OPERATION_LOCK.release()


def configure_restart_callback(callback: Callable[[], None] | None) -> None:
    """Connect the packaged launcher to the UI restart endpoint."""

    global _RESTART_CALLBACK
    _RESTART_CALLBACK = callback


def _open_model_libraries_folder() -> None:
    _MODEL_LIBRARIES_ROOT.mkdir(parents=True, exist_ok=True)
    open_folder_in_file_manager(_MODEL_LIBRARIES_ROOT)


@app.post("/api/model-libraries/open-folder")
def open_model_libraries_folder() -> dict:
    try:
        _open_model_libraries_folder()
    except OSError as exc:
        raise HTTPException(500, f"Could not open the Model Libraries folder: {exc}") from exc
    return {"opened": True}


@app.post("/api/system/restart", status_code=202)
def restart_prisma() -> dict:
    callback = _RESTART_CALLBACK
    if callback is None:
        raise HTTPException(503, "Automatic restart is unavailable; close and reopen Prisma")
    with _MODEL_RESOURCE_COORDINATION_LOCK:
        _assert_no_active_job(action="restart Prisma")
        status = _library_status()
        if not status.get("restart_required"):
            raise HTTPException(409, "Prisma is already using the selected model library")
        selected_id = status.get("active_library_id")
        selected_entry = next(
            (
                item
                for item in status.get("libraries", [])
                if item.get("valid") and item.get("library_id") == selected_id
            ),
            None,
        )
        if selected_entry is None:
            raise HTTPException(409, "The selected model library is invalid; select a valid library first")
        if _RESTART_REQUESTED.is_set():
            return {"restarting": True}
        _RESTART_REQUESTED.set()
    # Allow FastAPI to flush the 202 response before the launcher stops Uvicorn.
    timer = threading.Timer(0.25, callback)
    timer.daemon = True
    timer.start()
    return {"restarting": True}


@app.post("/api/cache/clear-runs")
def clear_cache_runs() -> dict:
    """Clear cached solve runs + auto-run archives + in-RAM solve cache.

    Auto-run archives (cache/auto_runs/) are cache artifacts and are swept here.
    Keeps LUTs, output, and the user-curated saved_runs/.
    """
    from cache_admin import safe_clear_dir
    _assert_no_active_job()
    removed = sum(
        safe_clear_dir(d, root=data_paths.CACHE_DIR)
        for d in (data_paths.RUN_CACHE_DIR, data_paths.AUTO_RUNS_DIR)
    )
    session.get("solve_cache", {}).clear()
    return {"cleared": "runs", "removed": removed}


@app.post("/api/cache/clear-all")
def clear_cache_all() -> dict:
    """Clear ALL clearable cache (files + solve and palette in-RAM caches).

    Keeps output and the user-curated saved_runs/.
    """
    from cache_admin import safe_clear_dir
    _assert_no_active_job()
    removed = sum(
        safe_clear_dir(d, root=data_paths.CACHE_DIR)
        for d in (data_paths.RUN_CACHE_DIR, data_paths.LUT_CACHE_DIR, data_paths.AUTO_RUNS_DIR)
    )
    session.get("solve_cache", {}).clear()
    _clear_palette_backend_cache()
    return {"cleared": "all", "removed": removed}


# ---------------------------------------------------------------------------
# Saved-run archive endpoints (Stage 9b)
# ---------------------------------------------------------------------------

import run_archive
import run_store
import auto_run_store
from run_naming import make_save_id


class SaveRunPayload(BaseModel):
    card_id: str
    label: Optional[str] = None


class RenameRunPayload(BaseModel):
    label: str


def _cached_solve_or_409(card_id: str) -> tuple[dict, dict]:
    _validate_card_id(card_id)
    cached = session.get("solve_cache", {}).get(card_id)
    if not cached or cached.get("solve", {}).get("status") != "complete":
        raise HTTPException(409, f"No completed solve cached for card_id {card_id!r}")
    return cached["solve"], cached["config"]


def _build_archive_inputs(card_id: str, solve: dict, cfg: dict, *, label: str, saved_at: str):
    image_name = Path(str(cfg.get("image_path", ""))).name or "image"
    image_path = _IMAGES_DIR / image_name
    if not image_path.exists():
        # The source image is a REQUIRED archive member — never save a hollow archive.
        raise HTTPException(409, f"Source image {image_name!r} is missing; cannot save a self-contained run")
    image_bytes = image_path.read_bytes()
    thickness_arrays = {}
    for key, arr in (solve.get("thickness_maps") or {}).items():
        thickness_arrays[f"tm__{getattr(key, 'value', key)}"] = np.asarray(arr)
    for key, arr in (solve.get("debug_maps") or {}).items():
        thickness_arrays[f"dbg__{getattr(key, 'value', key)}"] = np.asarray(arr)
    for key, arr in (solve.get("export_maps") or {}).items():
        thickness_arrays[f"ex__{getattr(key, 'value', key)}"] = np.asarray(arr)
    # Copy the ENTIRE per-card run-cache subtree (png/bin/json/csv + bundle subdir) so
    # instant review (contours, surface explorer, recipe) works on the loaded run.
    run_cache_files = {}
    run_dir = data_paths.RUN_CACHE_DIR / card_id
    cached_run_metadata = None
    if run_dir.is_dir():
        for p in run_dir.rglob("*"):
            if p.is_file():
                run_cache_files[p.relative_to(run_dir).as_posix()] = p.read_bytes()
        metadata_path = run_dir / "run.json"
        if metadata_path.is_file():
            try:
                parsed_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                if isinstance(parsed_metadata, dict):
                    cached_run_metadata = parsed_metadata
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                logger.warning("Could not read run metadata for archive card %s", card_id)
    run_json = {
        "schema_version": run_archive.SCHEMA_VERSION,
        "save_id": None, "label": label, "saved_at": saved_at,
        "source_image_name": image_name,
        "config": cfg,
        "palette": cfg.get("palette") or [],
        "image_domain_width_mm": solve.get("image_domain_width_mm"),
        "image_domain_height_mm": solve.get("image_domain_height_mm"),
        "stats": solve.get("result") or {},
        "result": solve.get("result") or {},
        "export_metadata": _json_safe_runtime_diagnostic(
            solve.get("export_metadata") or {}
        ),
    }
    # Preserve the durable solve metadata alongside the compact archive summary.
    # This is optional so schema-version-1 archives made before this field was
    # added remain valid and continue through the diagnostics/config fallback.
    if cached_run_metadata is not None:
        run_json["run_metadata"] = _json_safe_runtime_diagnostic(cached_run_metadata)
    solve_state = {"solve_owned_fingerprint": solve.get("solve_owned_fingerprint")}
    return run_json, thickness_arrays, image_bytes, image_name, solve_state, run_cache_files


def _pack_completed_run_archive(card_id: str, solve: dict, cfg: dict, *, label: str,
                                saved_at: str, root: Path, tier: str) -> tuple[str, bytes, dict]:
    save_id = make_save_id(cfg.get("image_path", ""), root, timestamp=saved_at)
    label = label or save_id  # empty label defaults to the id (single mint, consistent across run_json+sidecar)
    run_json, tmaps, img_bytes, img_name, solve_state, run_cache_files = _build_archive_inputs(
        card_id, solve, cfg, label=label, saved_at=saved_at)
    run_json["save_id"] = save_id
    zip_bytes = run_archive.pack_run_archive(
        run_json=run_json,
        thickness_arrays=tmaps,
        image_bytes=img_bytes,
        image_name=img_name,
        solve_state=solve_state,
        run_cache_files=run_cache_files,
    )
    sidecar = {
        "save_id": save_id,
        "label": label,
        "saved_at": saved_at,
        "source_image_name": img_name,
        "palette": run_json["palette"],
        "stats": {
            "mean_de": run_json["stats"].get("mean_de"),
            "max_de": run_json["stats"].get("max_de"),
        },
        "schema_version": run_archive.SCHEMA_VERSION,
        "tier": tier,
    }
    return save_id, zip_bytes, sidecar


def _maybe_write_auto_run(card_id: str | None, solve: dict, cfg: dict, *, saved_at: str | None = None) -> None:
    if not card_id:
        return
    try:
        ts = saved_at or datetime.now().strftime("%Y%m%d-%H%M%S")
        label = f"Auto {ts}"
        save_id, zip_bytes, sidecar = _pack_completed_run_archive(
            card_id, solve, cfg, label=label, saved_at=ts,
            root=data_paths.AUTO_RUNS_DIR, tier="auto")
        auto_run_store.write_auto_run(save_id, zip_bytes, sidecar)
    except Exception:
        logger.exception("Auto-run archive failed for card_id=%s", card_id)


@app.post("/api/runs/save")
def save_run(payload: SaveRunPayload) -> dict:
    solve, cfg = _cached_solve_or_409(payload.card_id)
    saved_at = datetime.now().strftime("%Y%m%d-%H%M%S")
    # save_id identity = timestamp + image only (stable across renames, spec §3).
    # _pack mints the id once and defaults an empty label to it.
    save_id, zip_bytes, sidecar = _pack_completed_run_archive(
        payload.card_id, solve, cfg, label=(payload.label or "").strip(), saved_at=saved_at,
        root=data_paths.SAVED_RUNS_DIR, tier="saved")
    run_store.write_save(save_id, zip_bytes, sidecar)
    return {"save_id": save_id, "label": sidecar["label"]}


@app.get("/api/runs/saved")
def list_saved_runs() -> list:
    saved = [dict(s, tier="saved") for s in run_store.list_saves()]
    auto = [dict(s, tier="auto") for s in auto_run_store.list_auto_runs()]
    rows = saved + auto
    rows.sort(key=lambda s: (str(s.get("saved_at", "")), str(s.get("save_id", ""))), reverse=True)
    return rows


@app.get("/api/runs/saved/{save_id}/download")
def download_saved_run(save_id: str):
    import io
    try:
        data = run_store.read_zip_bytes(save_id)
    except run_store.SaveNotFoundError:
        raise HTTPException(404, f"No such saved run: {save_id}")
    headers = {"Content-Disposition": f'attachment; filename="{save_id}.zip"'}
    return StreamingResponse(io.BytesIO(data), media_type="application/zip", headers=headers)


@app.get("/api/runs/auto/{save_id}/download")
def download_auto_run(save_id: str):
    import io
    try:
        data = auto_run_store.read_auto_zip_bytes(save_id)
    except run_store.SaveNotFoundError:
        raise HTTPException(404, f"No such auto run: {save_id}")
    headers = {"Content-Disposition": f'attachment; filename="{save_id}.zip"'}
    return StreamingResponse(io.BytesIO(data), media_type="application/zip", headers=headers)


def _saved_run_preview_response(save_id: str, tier: str):
    try:
        raw = _read_run_zip_for_tier(save_id, tier)
    except run_store.SaveNotFoundError:
        raise HTTPException(404, f"No such {tier} run: {save_id}")
    try:
        parsed = run_archive.read_run_archive(raw)
        with Image.open(io.BytesIO(parsed.image_bytes)) as raw_img:
            img = ImageOps.exif_transpose(raw_img).convert("RGB")
        img.thumbnail((320, 320))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=82, optimize=True)
        img.close()
    except (run_archive.ArchiveError, OSError, ValueError) as exc:
        raise HTTPException(400, f"Saved run preview is unavailable: {exc}") from exc
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/api/runs/saved/{save_id}/preview")
def saved_run_preview(save_id: str):
    return _saved_run_preview_response(save_id, "saved")


@app.get("/api/runs/auto/{save_id}/preview")
def auto_run_preview(save_id: str):
    return _saved_run_preview_response(save_id, "auto")


@app.post("/api/runs/auto/{save_id}/promote")
def promote_auto_run(save_id: str) -> dict:
    _assert_no_active_job()
    try:
        sidecar = auto_run_store.promote_auto_run(save_id)
    except run_store.SaveNotFoundError:
        raise HTTPException(404, f"No such auto run: {save_id}")
    return sidecar


@app.post("/api/runs/saved/{save_id}/rename")
def rename_saved_run(save_id: str, payload: RenameRunPayload) -> dict:
    try:
        return run_store.rename_save(save_id, payload.label.strip() or save_id)
    except run_store.SaveNotFoundError:
        raise HTTPException(404, f"No such saved run: {save_id}")


@app.delete("/api/runs/saved/{save_id}")
def delete_saved_run(save_id: str) -> dict:
    try:
        run_store.delete_save(save_id)
    except run_store.SaveDeletionError:
        raise HTTPException(
            409,
            f"Couldn't delete {save_id}: the file is in use — close any program "
            "holding it and retry",
        )
    return {"deleted": save_id}


@app.delete("/api/runs/auto/{save_id}")
def delete_auto_run(save_id: str) -> dict:
    try:
        auto_run_store.delete_auto_run(save_id)
    except run_store.SaveDeletionError:
        raise HTTPException(
            409,
            f"Couldn't delete {save_id}: the file is in use — close any program "
            "holding it and retry",
        )
    return {"deleted": save_id, "tier": "auto"}


import re as _re


def _fresh_loaded_card_id() -> str:
    """A collision-free card_id for a loaded run, valid per _validate_card_id."""
    base = datetime.now().strftime("loaded-%Y%m%d-%H%M%S")
    cand, n = base, 1
    while cand in session.get("solve_cache", {}) or (data_paths.RUN_CACHE_DIR / cand).exists():
        n += 1
        cand = f"{base}-{n}"
    _validate_card_id(cand)
    return cand


def _rebase_run_urls(obj, new_card_id: str):
    """Recursively rewrite ?run=<old> -> ?run=<new> in EVERY string anywhere in the
    payload (top-level *_url strings AND nested maps/lists like filament_bin_urls,
    debug_map_urls, overlay_urls, filament_maps[].map_url). Only URLs carry run=,
    so blanket string rewriting is safe."""
    if isinstance(obj, str):
        return _re.sub(r"run=[^&\"'\s]*", f"run={new_card_id}", obj) if "run=" in obj else obj
    if isinstance(obj, dict):
        return {k: _rebase_run_urls(v, new_card_id) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_rebase_run_urls(v, new_card_id) for v in obj]
    return obj


_RETIRED_ARCHIVE_ARTIFACT_TERMS = tuple(
    term.lower()
    for term in (
        "preferred_line_length",
        "printability_preferred_line_length",
        "short_length_preferred",
        "blueprint_printability_preferred",
        "soft_warn",
        "soft_short",
        "stage2_printability_gate_soft_warn",
        "pref-ll",
        "prefLen",
        "preferredLineLength",
    )
)


def _retired_archive_artifact_error(path: str) -> HTTPException:
    return HTTPException(
        422,
        {
            "error": "retired_archive_artifact",
            "path": path,
            "message": (
                "Loaded run archive contains retired preferred-length or "
                "soft-warning artifacts and cannot be rehydrated"
            ),
        },
    )


def _contains_retired_archive_term(text: object) -> bool:
    haystack = str(text).lower()
    return any(term in haystack for term in _RETIRED_ARCHIVE_ARTIFACT_TERMS)


def _first_retired_archive_artifact_path(obj: object, path: str) -> str | None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            child_path = f"{path}.{key}"
            if _contains_retired_archive_term(key):
                return child_path
            found = _first_retired_archive_artifact_path(value, child_path)
            if found:
                return found
        return None
    if isinstance(obj, (list, tuple)):
        for index, value in enumerate(obj):
            found = _first_retired_archive_artifact_path(value, f"{path}[{index}]")
            if found:
                return found
        return None
    if isinstance(obj, str) and _contains_retired_archive_term(obj):
        return path
    return None


def _reject_retired_loaded_archive_artifacts(parsed) -> None:
    for key in getattr(parsed, "thickness_arrays", {}) or {}:
        if _contains_retired_archive_term(key):
            raise _retired_archive_artifact_error(f"thickness_arrays.{key}")
    run_json = getattr(parsed, "run_json", {}) or {}
    for key in ("result", "export_metadata"):
        found = _first_retired_archive_artifact_path(run_json.get(key), f"run_json.{key}")
        if found:
            raise _retired_archive_artifact_error(found)
    raw_run_metadata = run_json.get("run_metadata")
    run_metadata = raw_run_metadata if isinstance(raw_run_metadata, dict) else {}
    found = _first_retired_archive_artifact_path(
        run_metadata.get("staged_metrics"),
        "run_json.run_metadata.staged_metrics",
    )
    if found:
        raise _retired_archive_artifact_error(found)
    for rel in getattr(parsed, "run_cache_files", {}) or {}:
        if _contains_retired_archive_term(rel):
            raise _retired_archive_artifact_error(f"run_cache_files.{rel}")


def _validate_loaded_archive_config(cfg: dict) -> dict:
    incoming = dict(cfg or {})
    if incoming.get("detail_cap_enabled") is False:
        raise HTTPException(
            422,
            "detail_cap_enabled is mandatory and can no longer be disabled",
        )
    incoming.pop("detail_cap_enabled", None)
    if "cap_fixed_thickness_mm" in incoming:
        raise _retired_config_field_error("cap_fixed_thickness_mm")
    if "printability_preferred_line_length_mm" in incoming:
        raise _retired_config_field_error("printability_preferred_line_length_mm")
    if "cap_mode" in incoming:
        try:
            incoming["cap_mode"] = _normalize_cap_mode(incoming["cap_mode"])
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    return incoming


def _optional_loaded_run_metadata(parsed) -> dict:
    """Return optional durable run metadata without making it load-critical.

    New archives carry this in the top-level run.json. Older archives still
    contain the original per-card run.json under run_cache/, so use that as a
    compatibility fallback when it is valid JSON. Malformed optional metadata
    is ignored; the compact config/result payload remains authoritative.
    """
    raw = (getattr(parsed, "run_json", {}) or {}).get("run_metadata")
    if isinstance(raw, dict):
        return deepcopy(raw)
    cached = (getattr(parsed, "run_cache_files", {}) or {}).get("run.json")
    if not cached:
        return {}
    try:
        decoded = json.loads(cached.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return deepcopy(decoded) if isinstance(decoded, dict) else {}


def _rehydrate_loaded_archive(parsed) -> dict:
    """Build a completed-solve payload + populate solve_cache from a ParsedArchive."""
    card_id = _fresh_loaded_card_id()
    rj = parsed.run_json
    cfg = _validate_loaded_archive_config(rj.get("config") or {})
    _reject_retired_loaded_archive_artifacts(parsed)
    run_metadata = _optional_loaded_run_metadata(parsed)
    # 1. Image -> save-scoped unique path; rewrite config.image_path.
    _IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    safe_img = f"{card_id}-{Path(parsed.image_name).name}"
    (_IMAGES_DIR / safe_img).write_bytes(parsed.image_bytes)
    cfg["image_path"] = safe_img
    # 2. Split npz back into thickness_maps + debug_maps (string keys; consumers tolerate str).
    thickness_maps, debug_maps, export_maps = {}, {}, {}
    for key, arr in parsed.thickness_arrays.items():
        if key.startswith("tm__"):
            thickness_maps[key[4:]] = arr
        elif key.startswith("dbg__"):
            debug_maps[key[5:]] = arr
        elif key.startswith("ex__"):
            export_maps[key[4:]] = arr
    # 3. Restore the WHOLE run-cache subtree (png/bin/json/csv + bundle) under the fresh
    #    card's dir so /api/run-cache/files serves contours/explorer/recipe, not just PNGs.
    run_dir = data_paths.RUN_CACHE_DIR / card_id
    run_dir.mkdir(parents=True, exist_ok=True)
    for rel, payload in parsed.run_cache_files.items():
        dest = run_dir / rel        # rel already validated safe by read_run_archive
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(payload)
    # 4. Rebased result payload (recursive: top-level + nested url maps/lists).
    result = _rebase_run_urls(rj.get("result") or {}, card_id)
    result["card_id"] = card_id
    if result.get("image_domain_width_mm") is None:
        result["image_domain_width_mm"] = rj.get("image_domain_width_mm")
    if result.get("image_domain_height_mm") is None:
        result["image_domain_height_mm"] = rj.get("image_domain_height_mm")
    # 5. Cache entry (grouping/solved_plan/profiles omitted -> None; re-export+swap tolerate it).
    session["solve_cache"][card_id] = {
        "config": cfg,
        "solve": {
            "status": "complete", "card_id": card_id,
            "thickness_maps": thickness_maps, "debug_maps": debug_maps,
            "export_maps": export_maps,
            "export_metadata": deepcopy(rj.get("export_metadata") or {}),
            "color_profiles": None, "wb_profile": None, "wc_profile": None,
            "grouping": None, "solved_plan": None, "blueprint_triage": None,
            "image_domain_width_mm": rj.get("image_domain_width_mm"),
            "image_domain_height_mm": rj.get("image_domain_height_mm"),
            "material_exposure_audit": None,
            "solve_owned_fingerprint": (parsed.solve_state or {}).get("solve_owned_fingerprint"),
            "result": result,
        },
    }
    return {
        "card_id": card_id,
        "config": cfg,
        "palette": rj.get("palette") or [],
        "result": result,
        "label": rj.get("label"),
        "run_metadata": run_metadata,
    }


class LoadRunPayload(BaseModel):
    save_id: str
    tier: Literal["saved", "auto"] = "saved"


class LoadRunSettingsPayload(BaseModel):
    save_id: str
    tier: Literal["saved", "auto"] = "saved"


def _load_from_bytes(raw: bytes) -> dict:
    try:
        parsed = run_archive.read_run_archive(raw)
    except run_archive.ArchiveError as exc:
        raise HTTPException(400, f"Invalid run archive: {exc}")
    return _rehydrate_loaded_archive(parsed)


def _read_run_zip_for_tier(save_id: str, tier: str) -> bytes:
    if tier == "auto":
        return auto_run_store.read_auto_zip_bytes(save_id)
    return run_store.read_zip_bytes(save_id)


@app.post("/api/runs/settings")
def load_run_settings(payload: LoadRunSettingsPayload) -> dict:
    """Read captured solve settings without rehydrating a run into the session."""
    try:
        raw = _read_run_zip_for_tier(payload.save_id, payload.tier)
        parsed = run_archive.read_run_archive(raw)
    except run_store.SaveNotFoundError:
        raise HTTPException(404, f"No such {payload.tier} run: {payload.save_id}")
    except run_archive.ArchiveError as exc:
        raise HTTPException(400, f"Invalid run archive: {exc}") from exc

    config = _validate_loaded_archive_config(parsed.run_json.get("config") or {})
    _reject_retired_loaded_archive_artifacts(parsed)
    result = parsed.run_json.get("result") or {}
    diagnostics = result.get("solve_start_diagnostics") if isinstance(result, dict) else None
    metadata = _optional_loaded_run_metadata(parsed)
    return {
        "config": config,
        "palette": parsed.run_json.get("palette") or [],
        "label": parsed.run_json.get("label"),
        "run_metadata": metadata,
        "result": {"solve_start_diagnostics": diagnostics} if isinstance(diagnostics, dict) else {},
    }


@app.post("/api/runs/load")
def load_run(payload: LoadRunPayload) -> dict:
    """Load a saved run BY save_id (JSON). The locked API shape is a single load
    route with a ``tier`` field ("saved" | "auto"); auto loads pass through the
    same Stage 9b rehydration path. Separate from the multipart upload route —
    a single FastAPI route cannot reliably bind both a JSON body and an UploadFile."""
    _assert_no_active_job()
    try:
        raw = _read_run_zip_for_tier(payload.save_id, payload.tier)
    except run_store.SaveNotFoundError:
        raise HTTPException(404, f"No such {payload.tier} run: {payload.save_id}")
    return _load_from_bytes(raw)


@app.post("/api/runs/load-upload")
async def load_run_upload(file: UploadFile = File(...)) -> dict:
    """Load a run from an UPLOADED zip (multipart), capped before the full read."""
    _assert_no_active_job()
    # Bound memory: read in 1 MB chunks and reject once we pass the compressed-upload cap,
    # BEFORE the (later) uncompressed zip-bomb check. Don't trust a client Content-Length.
    raw = bytearray()
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        raw.extend(chunk)
        if len(raw) > run_archive.MAX_UPLOAD_BYTES:
            raise HTTPException(413, "Uploaded run archive too large")
    return _load_from_bytes(bytes(raw))


# ── Static files (must be last) ──────────────────────────────────────────

@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    """Satisfy browsers' automatic favicon request without a noisy static 404."""
    return Response(status_code=204)


# Serve the unified_generator/ directory for index.html, style.css, app.js
app.mount("/", StaticFiles(directory=str(_GEN_DIR / "app"), html=True), name="static")
