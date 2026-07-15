"""
lith_model.py — Forward model wrapper for the lithophane generator.

Loads calibrated spline profiles and exposes a unified interface for
predicting pixel colors from filament thickness assignments.
"""
from __future__ import annotations
import sys
import numpy as np
from pathlib import Path
from typing import Dict, List, Tuple

# Path setup — Prisma/generator/model.py → Prisma/ for lib imports
_GEN_DIR = Path(__file__).resolve().parent
_PRISMA_DIR = _GEN_DIR.parent
if str(_PRISMA_DIR) not in sys.path:
    sys.path.insert(0, str(_PRISMA_DIR))
if str(_GEN_DIR) not in sys.path:
    sys.path.insert(0, str(_GEN_DIR))

from lib.camera_transform import apply_inverse_lut, load_inverse_lut  # noqa: E402
from lib.transmission import predict, compose, linear_to_oklab, profile_from_dict  # noqa: E402
import data_paths  # noqa: E402

# Canonical profiles inside the launcher's active published model library.
PROFILES_DIR = data_paths.DATA_DIR / "filaments" / "profiles"
GAMMA = 2.2  # sRGB gamma
DEFAULT_MODEL_DOMAIN_INGRESS_LUT_PATH = (
    data_paths.DATA_DIR / "camera_transform"
)


# ── Profile loading ───────────────────────────────────────────────────────────

def load_profile(filament_id: str, profiles_dir: Path | None = None) -> dict:
    """Load a spline profile JSON and build its interpolators.

    Parameters
    ----------
    filament_id : str
        Filament slug (hyphens, e.g. bambu-basic-cyan).
    profiles_dir : Path, optional
        Override for the module-level PROFILES_DIR.
    """
    import json
    if profiles_dir is None:
        path = PROFILES_DIR / f"{filament_id}.json"
    else:
        path = Path(profiles_dir) / f"{filament_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"No spline profile for '{filament_id}' at {path}")
    with open(path) as f:
        raw = json.load(f)
    # profile_from_dict builds the spline interpolators and returns a ready-to-use profile
    return profile_from_dict(raw)


def load_profiles(filament_ids: List[str], profiles_dir: Path | None = None) -> Dict[str, dict]:
    """Load spline profiles for a list of filament IDs. Returns dict id→profile."""
    return {fid: load_profile(fid, profiles_dir=profiles_dir) for fid in filament_ids}


# ── Transmission prediction ───────────────────────────────────────────────────

def predict_transmission(profile: dict, d_mm: float) -> np.ndarray:
    """
    Return linear-RGB transmission [T_r, T_g, T_b] for one filament at thickness d_mm.
    profile must be a loaded profile dict (output of load_profile / profile_from_dict).
    """
    return np.array(predict(profile, d_mm))


def compose_stack(layers: List[Tuple[dict, float]]) -> np.ndarray:
    """
    Compose an ordered list of (profile, thickness_mm) pairs into a combined
    linear-RGB transmission vector. Order is arbitrary (multiplicative model).
    """
    return np.array(compose(layers))


def predict_pixel(
    color_profiles: Dict[str, dict],
    thicknesses: Dict[str, float],
    wb_profile: dict,
    d_wb: float,
    wc_profile: dict,
    d_wc: float,
) -> np.ndarray:
    """
    Predict linear-RGB transmission for one pixel given:
      color_profiles : filament_id → loaded profile
      thicknesses     : filament_id → thickness in mm (0.0 = absent)
      wb_profile      : white base profile
      d_wb            : white base thickness in mm
      wc_profile      : white cap profile
      d_wc            : white cap thickness in mm

    Returns np.array([T_r, T_g, T_b]) in [0, 1] linear.
    """
    layers = [(wb_profile, d_wb)]
    for fid, d in thicknesses.items():
        if d > 0:
            layers.append((color_profiles[fid], d))
    layers.append((wc_profile, d_wc))
    return compose_stack(layers)


# ── Image → target map ────────────────────────────────────────────────────────

def srgb_to_linear(img_srgb: np.ndarray) -> np.ndarray:
    """Convert uint8 [0,255] or float [0,1] sRGB image to linear RGB [0,1]."""
    img = img_srgb.astype(np.float32)
    if np.issubdtype(np.asarray(img_srgb).dtype, np.integer) or img.max() > 1.5:
        img = img / 255.0
    else:
        img = np.clip(img, 0.0, 1.0)
    return (img ** GAMMA).clip(0.0, 1.0)


def _load_model_domain_ingress_lut(lut_path: str | Path) -> np.ndarray:
    """Load the Camera Transform inverse source-sRGB -> model-domain LUT."""
    return load_inverse_lut(lut_path)


def _apply_model_domain_ingress_lut(img_srgb: np.ndarray, lut: np.ndarray) -> np.ndarray:
    """Trilinear source-sRGB -> model-domain target lookup."""
    return apply_inverse_lut(img_srgb, lut)


def image_to_target(
    img_srgb: np.ndarray,
    wb_profile: dict,
    d_wb: float,
    *,
    model_domain_ingress: bool = False,
    model_domain_ingress_lut_path: str | Path = DEFAULT_MODEL_DOMAIN_INGRESS_LUT_PATH,
) -> np.ndarray:
    """
    Convert an sRGB source image to per-pixel target linear-RGB values.

    The LUT stores oklab(T_wb × T_wc × ∏T_color) — the full-stack
    transmission including white base.  The target must be in the same
    space, so we return srgb_to_linear(img) directly.  T_wb is NOT
    divided out (it is already included in every LUT entry).

    wb_profile and d_wb are accepted for API compatibility but unused.

    Returns float32 array of shape (H, W, 3), values in [0, 1] linear.
    """
    # Ingress-off mode remains the legacy bit-identical srgb-to-linear path.
    if model_domain_ingress:
        lut = _load_model_domain_ingress_lut(model_domain_ingress_lut_path)
        return _apply_model_domain_ingress_lut(img_srgb, lut)
    return srgb_to_linear(img_srgb).astype(np.float32)


def to_oklab(linear_rgb: np.ndarray) -> np.ndarray:
    """
    Convert a (..., 3) linear-RGB array to OKLab.
    model_spline.linear_to_oklab already handles arbitrary leading dimensions.
    """
    return linear_to_oklab(linear_rgb).astype(np.float32)
