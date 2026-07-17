from __future__ import annotations

import re
from pathlib import Path

import numpy as np
from PIL import Image

import run_archive
import server
from scalar_palette import INFERNO_RGB8, INFERNO_V1, sample_inferno, scalar_diagnostic_rgb


def test_canonical_inferno_samples_and_frontend_lut_match() -> None:
    indices = np.array([0, 1, 64, 128, 192, 255])
    expected = np.array(
        [[0, 0, 4], [1, 0, 5], [87, 16, 110], [188, 55, 84], [249, 142, 9], [252, 255, 164]],
        dtype=np.uint8,
    )
    np.testing.assert_array_equal(INFERNO_RGB8[indices], expected)
    np.testing.assert_array_equal(sample_inferno(indices / 255.0), expected)
    np.testing.assert_array_equal(sample_inferno(np.array([0.5]))[0], [187, 55, 85])

    state_module = (
        Path(__file__).parents[2] / "Prisma/generator/app/core/application-context.js"
    ).read_text(encoding="utf-8")
    match = re.search(r'INFERNO_RGB8_HEX\s*=\s*"([0-9a-f]+)";', state_module)
    assert match is not None
    assert match.group(1) == INFERNO_RGB8.tobytes().hex()


def test_zero_mask_one_layer_and_png_dimensions(tmp_path: Path) -> None:
    layer_height = 0.08
    values = np.array([[0.0, layer_height, 1.5, 3.0]], dtype=np.float32)
    rgb = scalar_diagnostic_rgb(values, max_value=3.0, zero_rgb=(0, 0, 0))
    assert rgb.shape == (1, 4, 3)
    np.testing.assert_array_equal(rgb[0, 0], [0, 0, 0])
    assert tuple(rgb[0, 1]) != (0, 0, 0), "one printable layer must remain visible beside the no-material mask"
    assert len({tuple(pixel) for pixel in rgb[0]}) == 4

    output = tmp_path / "scalar.png"
    server._save_cap_height_map(values, output, max_mm=3.0, zero_rgb=(0, 0, 0))
    with Image.open(output) as image:
        assert image.size == (4, 1)
        np.testing.assert_array_equal(np.asarray(image), rgb)


def test_palette_provenance_roundtrips_without_invalidating_legacy_archives() -> None:
    common = dict(
        run_json={"schema_version": run_archive.SCHEMA_VERSION, "config": {}, "palette": []},
        thickness_arrays={"tm__white_cap": np.zeros((1, 1), np.float32)},
        image_bytes=b"image",
        image_name="source.png",
        run_cache_files={"surface.png": b"png"},
    )
    parsed = run_archive.read_run_archive(run_archive.pack_run_archive(
        **common,
        solve_state={"result": {"diagnostic_palette_version": INFERNO_V1}},
    ))
    assert parsed.solve_state["result"]["diagnostic_palette_version"] == INFERNO_V1

    legacy = run_archive.read_run_archive(run_archive.pack_run_archive(
        **common,
        solve_state={"result": {}},
    ))
    assert "diagnostic_palette_version" not in legacy.solve_state["result"]
