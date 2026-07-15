"""Tests for deterministic Prisma test-pattern generation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image

from tools import generate_prisma_test_patterns as patterns


EXPECTED_FILENAMES = [
    "01_grayscale_ramp_horizontal.png",
    "02_grayscale_ramp_vertical.png",
    "03_grayscale_steps_21.png",
    "04_primary_secondary_bars.png",
    "05_saturation_lightness_grid.png",
    "06_hard_edge_quadrants.png",
    "07_thin_lines_hv_diag.png",
    "08_checkerboard_multiscale.png",
    "09_frequency_sweep.png",
    "10_dots_islands_branches.png",
    "11_concentric_rings_and_radials.png",
    "12a_registration_ultrawide.png",
    "12b_registration_ultratall.png",
]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _generate(tmp_path: Path):
    out_dir = tmp_path / "generated"
    manifest = tmp_path / "pattern_manifest.json"
    entries = patterns.generate_suite(out_dir=out_dir, manifest_path=manifest)
    return out_dir, manifest, entries


def test_generator_produces_expected_file_list(tmp_path):
    out_dir, manifest, entries = _generate(tmp_path)

    assert [entry["filename"] for entry in entries] == EXPECTED_FILENAMES
    assert sorted(path.name for path in out_dir.glob("*.png")) == sorted(EXPECTED_FILENAMES)
    assert manifest.exists()


def test_generated_png_modes_and_dimensions(tmp_path):
    out_dir, _, _ = _generate(tmp_path)

    expected_sizes = {spec.filename: spec.size for spec in patterns.PATTERN_SPECS}
    for filename in EXPECTED_FILENAMES:
        with Image.open(out_dir / filename) as image:
            assert image.mode == "RGB"
            assert image.size == expected_sizes[filename]


def test_rerunning_generator_is_byte_stable(tmp_path):
    out_a = tmp_path / "run_a"
    manifest_a = tmp_path / "manifest_a.json"
    out_b = tmp_path / "run_b"
    manifest_b = tmp_path / "manifest_b.json"

    patterns.generate_suite(out_dir=out_a, manifest_path=manifest_a)
    patterns.generate_suite(out_dir=out_b, manifest_path=manifest_b)

    hashes_a = {path.name: _sha256(path) for path in out_a.glob("*.png")}
    hashes_b = {path.name: _sha256(path) for path in out_b.glob("*.png")}
    assert hashes_a == hashes_b
    assert _sha256(manifest_a) == _sha256(manifest_b)


def test_horizontal_ramp_is_monotonic():
    arr = patterns.build_01_grayscale_ramp_horizontal()
    row = arr[arr.shape[0] // 2, :, 0].astype(np.int16)
    assert np.all(np.diff(row) >= 0)


def test_vertical_ramp_is_monotonic():
    arr = patterns.build_02_grayscale_ramp_vertical()
    col = arr[:, arr.shape[1] // 2, 0].astype(np.int16)
    assert np.all(np.diff(col) >= 0)


def test_grayscale_step_chart_has_21_plateaus():
    arr = patterns.build_03_grayscale_steps_21()
    row = arr[arr.shape[0] // 2, :, 0]
    transitions = np.flatnonzero(np.diff(row) != 0)
    unique_values = np.unique(row)

    assert len(unique_values) == 21
    assert len(transitions) == 20


def test_primary_secondary_bars_contain_expected_colors():
    arr = patterns.build_04_primary_secondary_bars()
    width = arr.shape[1]
    sampled = []
    for cols in np.array_split(np.arange(width), len(patterns.PRIMARY_SECONDARY_BAR_COLORS)):
        sampled.append(tuple(int(v) for v in arr[arr.shape[0] // 2, cols[len(cols) // 2]]))
    assert sampled == patterns.PRIMARY_SECONDARY_BAR_COLORS


def test_thin_line_chart_contains_configured_widths():
    arr = patterns.build_07_thin_lines_hv_diag()
    upper_half = arr[: arr.shape[0] // 2]
    dark_mask = np.all(upper_half < 20, axis=2)
    dark_row_counts = dark_mask.sum(axis=1)
    line_rows = dark_row_counts >= (arr.shape[1] - 2 * patterns.SAFE_MARGIN - 34)

    run_lengths = []
    run = 0
    for is_line in line_rows:
        if is_line:
            run += 1
        elif run:
            run_lengths.append(run)
            run = 0
    if run:
        run_lengths.append(run)

    assert run_lengths[: len(patterns.THIN_LINE_WIDTHS)] == patterns.THIN_LINE_WIDTHS


def test_checkerboard_contains_each_configured_block_scale():
    arr = patterns.build_08_checkerboard_multiscale()
    tile_w = arr.shape[1] // 4
    tile_h = arr.shape[0] // 2
    observed = []
    idx = 0
    for row in range(2):
        for col in range(4):
            y0 = row * tile_h
            y1 = arr.shape[0] if row == 1 else (row + 1) * tile_h
            x0 = col * tile_w
            x1 = arr.shape[1] if col == 3 else (col + 1) * tile_w
            region = arr[y0:y1, x0:x1, 0]
            sample_row = region[min(10, region.shape[0] - 1)]
            transitions = np.flatnonzero(np.diff(sample_row) != 0)
            first_run = transitions[0] + 1 if len(transitions) else region.shape[1]
            observed.append(first_run)
            idx += 1

    assert observed == patterns.CHECKERBOARD_SCALES


def test_registration_charts_have_distinct_corners_and_asymmetric_markers():
    for builder, size in [
        (patterns.build_12a_registration_ultrawide, patterns.ULTRAWIDE_SIZE),
        (patterns.build_12b_registration_ultratall, patterns.ULTRATALL_SIZE),
    ]:
        arr = builder()
        width, height = size
        m = patterns.SAFE_MARGIN + 20
        corner_samples = {
            "tl": tuple(int(v) for v in arr[m, m]),
            "tr": tuple(int(v) for v in arr[m, width - m]),
            "bl": tuple(int(v) for v in arr[height - m, m]),
            "br": tuple(int(v) for v in arr[height - m, width - m]),
        }
        assert corner_samples == patterns.REGISTRATION_CORNER_COLORS

        magenta_sample = tuple(int(v) for v in arr[patterns.SAFE_MARGIN + 135, patterns.SAFE_MARGIN + 185])
        cyan_x = width - patterns.SAFE_MARGIN - 240 + 2 * 24 + 6
        cyan_y = height - patterns.SAFE_MARGIN - 180 + 70
        cyan_sample = tuple(
            int(v)
            for v in arr[cyan_y, cyan_x]
        )
        assert magenta_sample[0] > 200 and magenta_sample[2] > 200 and magenta_sample[1] < 80
        assert cyan_sample[1] > 150 and cyan_sample[2] > 200 and cyan_sample[0] < 80


def test_manifest_matches_generated_files(tmp_path):
    out_dir, manifest_path, _ = _generate(tmp_path)

    manifest_entries = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_names = [entry["filename"] for entry in manifest_entries]
    disk_names = sorted(path.name for path in out_dir.glob("*.png"))

    assert manifest_names == EXPECTED_FILENAMES
    assert sorted(manifest_names) == disk_names
    expected_keys = [
        "category",
        "description",
        "dimensions",
        "filename",
        "id",
        "notes",
        "pipeline_targets",
        "suggested_checks",
    ]
    for entry in manifest_entries:
        assert sorted(entry.keys()) == expected_keys


def test_only_selector_supports_pattern_family(tmp_path):
    out_dir = tmp_path / "subset"
    manifest = tmp_path / "subset_manifest.json"

    entries = patterns.generate_suite(out_dir=out_dir, manifest_path=manifest, only=["12"])

    assert [entry["filename"] for entry in entries] == [
        "12a_registration_ultrawide.png",
        "12b_registration_ultratall.png",
    ]
