"""
test_manual_flatfield.py — Step 2 §10.3/§10.4 manual extraction-math units.

Two cleanly-testable pieces of the Theo-cleared manual harmonization:
- register_flatfield_strict (§10.4): strict lightbox-homography registration that
  RAISES on detection/homography failure — no silent resize fallback.
- _preview_scale_for_rotation (§10.3): preview→raw scale must use the raw HEIGHT as
  the effective width for 90/270° source rotations (preview is rotation-baked).

The integrated extract_strip_manual rewrite changes manual OUTPUT and is validated
on real samples (no golden, doc-29 §14.3) — not unit-tested here.

Run: python -m pytest tests/calibration/test_manual_flatfield.py -q
"""
from __future__ import annotations

import cv2
import numpy as np
import pytest

from processing.extraction import register_flatfield_strict
from processing.manual import _preview_scale_for_rotation


def _bright_rect(w=200, h=200, rx0=60, ry0=60, rx1=140, ry1=140) -> np.ndarray:
    """A dark frame with a bright rectangle (a stand-in lightbox).

    Kept under ~30% of the frame so find_lightbox_region's max(p70, 180)
    threshold detects it (a real lightbox photo isn't saturated-pure).
    """
    img = np.zeros((h, w, 3), dtype=np.uint8)
    img[ry0:ry1, rx0:rx1] = 255
    return img


# ── §10.4 strict flatfield ────────────────────────────────────────────────────

class TestRegisterFlatfieldStrict:
    def test_raises_when_strip_lightbox_undetected(self):
        ff_linear = np.ones((200, 200, 3), dtype=np.float32)
        ff_vis = _bright_rect()
        dark_strip = np.zeros((200, 200, 3), dtype=np.uint8)  # no bright region
        with pytest.raises(Exception):
            register_flatfield_strict(ff_linear, dark_strip, flatfield_visual_bgr=ff_vis)

    def test_raises_when_flatfield_lightbox_undetected(self):
        ff_linear = np.ones((200, 200, 3), dtype=np.float32)
        dark_ff_vis = np.zeros((200, 200, 3), dtype=np.uint8)  # no bright region
        strip_vis = _bright_rect()
        with pytest.raises(Exception):
            register_flatfield_strict(ff_linear, strip_vis, flatfield_visual_bgr=dark_ff_vis)

    def test_raises_on_homography_failure(self, monkeypatch):
        ff_linear = np.ones((200, 200, 3), dtype=np.float32)
        ff_vis = _bright_rect()
        strip_vis = _bright_rect()
        monkeypatch.setattr(cv2, "findHomography", lambda *a, **k: (None, None))
        with pytest.raises(Exception):
            register_flatfield_strict(ff_linear, strip_vis, flatfield_visual_bgr=ff_vis)

    def test_success_returns_source_sized_array(self):
        ff_linear = np.full((180, 220, 3), 0.5, dtype=np.float32)
        ff_vis = _bright_rect(w=220, h=180, rx0=70, ry0=55, rx1=150, ry1=125)
        strip_vis = _bright_rect(w=200, h=200)
        out = register_flatfield_strict(ff_linear, strip_vis, flatfield_visual_bgr=ff_vis)
        assert out.shape[:2] == strip_vis.shape[:2]   # warped into the source frame

    def test_never_silently_resizes(self, monkeypatch):
        # If lightbox detection fails, the function must NOT fall back to cv2.resize.
        called = {"resize": False}
        real_resize = cv2.resize

        def spy(*a, **k):
            called["resize"] = True
            return real_resize(*a, **k)
        monkeypatch.setattr(cv2, "resize", spy)
        ff_linear = np.ones((200, 200, 3), dtype=np.float32)
        dark = np.zeros((200, 200, 3), dtype=np.uint8)
        with pytest.raises(Exception):
            register_flatfield_strict(ff_linear, dark, flatfield_visual_bgr=_bright_rect())
        assert called["resize"] is False


# ── §10.3 preview scale for rotation ─────────────────────────────────────────

class TestPreviewScaleForRotation:
    def test_even_rotation_uses_raw_width(self):
        # raw 6000x4000, preview 2000 wide, no rotation -> scale 2000/6000
        assert _preview_scale_for_rotation(2000, 6000, 4000, 0) == pytest.approx(2000 / 6000)
        assert _preview_scale_for_rotation(2000, 6000, 4000, 2) == pytest.approx(2000 / 6000)

    def test_odd_rotation_uses_raw_height(self):
        # 90/270 rotation: preview width corresponds to raw HEIGHT (4000)
        assert _preview_scale_for_rotation(2000, 6000, 4000, 1) == pytest.approx(2000 / 4000)
        assert _preview_scale_for_rotation(2000, 6000, 4000, 3) == pytest.approx(2000 / 4000)

    def test_zero_guard(self):
        assert _preview_scale_for_rotation(2000, 0, 0, 0) == 1.0
