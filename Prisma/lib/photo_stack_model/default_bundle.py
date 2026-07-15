"""Point-in-time reference runtime bundle location.

This bundled artifact is for replay/comparison and bootstrapping. Live fitted
models should come from calibration candidate artifacts generated from current
data.
"""

from __future__ import annotations

from pathlib import Path

from .bundle import PhotoStackBundle, load_photo_stack_bundle


DEFAULT_PHOTO_STACK_BUNDLE_PATH = Path(__file__).resolve().parent / "bundles" / "runtime_bundle.json"


def load_default_photo_stack_bundle() -> PhotoStackBundle:
    """Load the Prisma-managed point-in-time reference bundle."""

    return load_photo_stack_bundle(DEFAULT_PHOTO_STACK_BUNDLE_PATH)
