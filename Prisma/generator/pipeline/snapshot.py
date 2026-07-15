"""SnapshotPublisher — renders preprocessed source-image preview frames to
PNG and writes their URLs into a shared progress dict.

The live hook is publish_image_preview, which emits `preprocess/<module>`
frames produced during the preprocessing pipeline (Wing C / R5-A). The
progressive-*solve* snapshot path (maybe_publish) was removed in the Stage 3
cleanup; only the preprocessing image-preview hook remains."""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

# Path setup mirrors other pipeline modules
_PIPE_DIR = Path(__file__).resolve().parent
_GEN_DIR = _PIPE_DIR.parent
_PRISMA_DIR = _GEN_DIR.parent
if str(_GEN_DIR) not in sys.path:
    sys.path.insert(0, str(_GEN_DIR))
if str(_PRISMA_DIR) not in sys.path:
    sys.path.insert(0, str(_PRISMA_DIR))


class SnapshotPublisher:
    """Publish preprocessed-input preview frames to PNG and write their
    URLs into a shared progress dict.

    The live hook is publish_image_preview, which emits `preprocess/<module>`
    frames produced during the preprocessing pipeline (Wing C / R5-A). The
    boundary is srgb_u8 exclusively.
    """

    def __init__(
        self,
        out_dir: Path,
        card_id: str,
        progress_dict: dict,
    ) -> None:
        self._out_dir = Path(out_dir)
        self._card_id = card_id
        self._progress_dict = progress_dict
        self._subdir = self._out_dir / "progressive"

    def publish_image_preview(
        self,
        image: np.ndarray,
        stage_key: str,
        *,
        context_fingerprint: str | None = None,
    ) -> None:
        """Emit a `preprocess/<module>` preview frame (R1 A3 + R3-F + R5-A).

        This hook does not depend on any solver output — preprocessing runs
        BEFORE the solver produces thickness maps. The runner is responsible for
        canonicalizing the operator's declared output_domain to `srgb_u8`
        via `preprocessing/color_convert.py` before calling here; the
        publisher boundary is `srgb_u8` exclusively per R5-A.

        `context_fingerprint` is a Wing C § B.6 / R6 C.4 hook: when the
        operator's output depends on shared context that lives outside
        its `params` (currently only `palette_metadata`), the runner
        passes the resolved request fingerprint here so the cache-busting
        URL incorporates it. The browser/proxy keys per (stage_key, fp).
        """
        if not isinstance(image, np.ndarray):
            raise TypeError(
                f"publish_image_preview requires np.ndarray, got {type(image).__name__}"
            )
        if image.dtype != np.uint8:
            raise ValueError(
                "publish_image_preview boundary contract is srgb_u8 (R5-A); "
                f"runner handed dtype={image.dtype}. Convert through "
                "preprocessing.color_convert before calling."
            )

        png_path = self._subdir / f"{stage_key}.png"
        png_path.parent.mkdir(parents=True, exist_ok=True)
        Image.fromarray(image).save(str(png_path))

        ts = int(time.time() * 1000)
        url = (
            f"/api/run-cache/files/progressive/{stage_key}.png"
            f"?run={self._card_id}&t={ts}"
        )
        if context_fingerprint:
            url = f"{url}&fp={context_fingerprint}"
        self._progress_dict["preview_url"] = url
        self._progress_dict["preview_stage"] = stage_key
