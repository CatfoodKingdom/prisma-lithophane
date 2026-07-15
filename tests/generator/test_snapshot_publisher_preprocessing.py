"""SnapshotPublisher.publish_image_preview tests for the preprocessing slot.

R1 A3 keys progressive-preview frames as `preprocess/<module_name>`.
R3-F + R5-A pin the publisher boundary as `srgb_u8` exclusively, so
publish_image_preview takes a raster the runner has already canonicalized
through `color_convert.py` helpers and just routes it to disk.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from pipeline.snapshot import SnapshotPublisher


def _make_publisher(tmp_path: Path) -> tuple[SnapshotPublisher, dict]:
    progress: dict = {}
    pub = SnapshotPublisher(out_dir=tmp_path, card_id="test_card", progress_dict=progress)
    return pub, progress


def test_publish_image_preview_writes_png(tmp_path: Path):
    pub, progress = _make_publisher(tmp_path)
    img = np.full((8, 8, 3), 128, dtype=np.uint8)
    pub.publish_image_preview(img, stage_key="preprocess/legacy_adjustments")
    out = tmp_path / "progressive" / "preprocess" / "legacy_adjustments.png"
    assert out.exists()
    decoded = np.array(Image.open(out))
    assert decoded.shape == (8, 8, 3)
    assert decoded.dtype == np.uint8


def test_publish_image_preview_updates_progress_dict(tmp_path: Path):
    pub, progress = _make_publisher(tmp_path)
    img = np.full((4, 4, 3), 200, dtype=np.uint8)
    pub.publish_image_preview(img, stage_key="preprocess/test_op")
    assert "preview_url" in progress
    assert "preprocess/test_op" in progress["preview_url"]
    assert progress["preview_stage"] == "preprocess/test_op"
    assert "run=test_card" in progress["preview_url"]


def test_publish_image_preview_rejects_non_uint8(tmp_path: Path):
    """R5-A: the publisher boundary is srgb_u8 exclusively. Non-uint8 input
    is a runner bug — fail loudly so we catch it in tests, not in production
    where it would silently corrupt the preview stream.
    """
    pub, _ = _make_publisher(tmp_path)
    img = np.zeros((4, 4, 3), dtype=np.float32)
    with pytest.raises((TypeError, ValueError)):
        pub.publish_image_preview(img, stage_key="preprocess/bad_op")


def test_publish_image_preview_does_not_require_thickness_maps(tmp_path: Path):
    """The preprocessing preview channel must work standalone —
    preprocessing runs BEFORE any solve has produced thickness maps.
    """
    pub, progress = _make_publisher(tmp_path)
    img = np.zeros((4, 4, 3), dtype=np.uint8)
    pub.publish_image_preview(img, stage_key="preprocess/op_a")
    assert (tmp_path / "progressive" / "preprocess" / "op_a.png").exists()
