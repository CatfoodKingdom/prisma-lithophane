"""Forward Camera Transform F for display-domain conversion.

The photo stack generator pipeline predicts model-domain (flatfield-normalized
transmission) RGB.  This module maps that to camera/display sRGB-encoded
appearance — "what the lit lithophane will look like" — using the
sandbox-fitted v2 transform: a Finlayson degree-2 root-polynomial (3x6 on
features [r, g, b, sqrt(rg), sqrt(gb), sqrt(rb)]) followed by per-channel
monotone PCHIP tone curves.  It is the forward inverse of the
model_domain_ingress LUT (G = F^-1) used at image ingestion.

Missing or invalid transform params are a hard error — never fall back to
raw transmission silently (that recreates the exact display confusion this
module exists to fix).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

_PRISMA_DIR = Path(__file__).resolve().parent.parent

DEFAULT_MODEL_DOMAIN_DISPLAY_TRANSFORM_PATH = (
    str(_PRISMA_DIR / "data" / "camera_transform")
)

from lib.camera_transform import apply_forward, load_camera_transform  # noqa: E402


def load_display_transform_params(path: str | Path) -> tuple[np.ndarray, str]:
    """Load + validate Camera Transform params. Returns (params, sha256)."""
    transform = load_camera_transform(path)
    return transform.params, transform.sha256


def apply_appearance_transform(linear_rgb: np.ndarray, params: np.ndarray) -> np.ndarray:
    """Model-domain linear RGB (..., 3) -> display sRGB-encoded appearance in [0,1].

    Exact port of the sandbox apply_v2: Z = clip(phi(T) @ W.T, 0, 1), then
    per-channel monotone PCHIP curves, then clip to [0,1].  Output is
    DISPLAY-ENCODED — write it to PNG via round(out * 255), no extra gamma.
    """
    return apply_forward(linear_rgb, params)


def bake_appearance_png(
    pred_srgb8: np.ndarray,
    out_path: Path,
    *,
    display_transform_path: str | Path,
) -> str:
    """Bake one model-domain gamma-2.2 sRGB image through F to an appearance PNG.

    Shares the exact encoding contract of :func:`bake_view_domain_images`'s
    predicted_appearance step: recover the model-domain transmission as
    ``(png/255) ** 2.2``, apply the forward Camera Transform F (which outputs
    display-encoded sRGB directly), then write ``round(F * 255)``. Used for the
    color-only predicted render so the recipe viewer's card reads as lit
    appearance, consistent with the full predicted_appearance.png.

    Returns the transform sha256 for provenance.
    """
    from PIL import Image

    params, sha = load_display_transform_params(display_transform_path)
    pred_t = (np.asarray(pred_srgb8, dtype=np.float64) / 255.0) ** 2.2
    appearance = apply_appearance_transform(pred_t, params)
    Image.fromarray((appearance * 255.0 + 0.5).astype(np.uint8)).save(str(out_path))
    return sha


def bake_view_domain_images(
    out_dir: Path,
    *,
    pred_srgb8: np.ndarray,
    source_srgb8: np.ndarray,
    model_domain_ingress: bool,
    model_domain_ingress_lut_path: str | Path,
    display_transform_path: str | Path,
) -> dict:
    """Write predicted_appearance.png and target_transmission.png next to
    predicted.png/source.png.  Per-run honest: the target image is what THIS
    run actually solved for.  Returns a provenance dict for run.json.

    Encoding contract (pinned by the module tests):
    - predicted.png was encoded with gamma 2.2 (_srgb8_from_linear), so the
      model-domain T is recovered as (png/255) ** 2.2.
    - F outputs display-encoded sRGB directly -> round(F * 255).
    - target_transmission.png uses _srgb8_from_linear so it is comparable to
      predicted.png in Transmission mode.
    """
    import shutil

    from PIL import Image

    # Bare sibling imports (generator app dir is on sys.path); deferred to
    # call time to avoid import cycles at module load.
    from appearance_model import _srgb8_from_linear
    from model import _apply_model_domain_ingress_lut, _load_model_domain_ingress_lut

    out_dir = Path(out_dir)
    params, sha = load_display_transform_params(display_transform_path)

    pred_t = (np.asarray(pred_srgb8, dtype=np.float64) / 255.0) ** 2.2
    appearance = apply_appearance_transform(pred_t, params)
    Image.fromarray((appearance * 255.0 + 0.5).astype(np.uint8)).save(
        str(out_dir / "predicted_appearance.png")
    )

    if model_domain_ingress:
        lut = _load_model_domain_ingress_lut(model_domain_ingress_lut_path)
        target_t = _apply_model_domain_ingress_lut(source_srgb8, lut)
        Image.fromarray(_srgb8_from_linear(target_t)).save(
            str(out_dir / "target_transmission.png")
        )
    else:
        # OFF runs: the decoded source WAS the solve target; byte-copy keeps
        # the frontend rule unconditional.
        shutil.copy2(str(out_dir / "source.png"), str(out_dir / "target_transmission.png"))

    return {
        "display_transform_path": str(display_transform_path),
        "display_transform_sha256": sha,
    }
