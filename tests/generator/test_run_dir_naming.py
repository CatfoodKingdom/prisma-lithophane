"""Tests for palette-NN sequential naming of Solve All batch child folders.

The old scheme joined filament IDs with '+', which blew past Windows MAX_PATH
and could produce silent collisions on truncation. This suite pins the new
scheme: opaque `palette-NN` sequential indices, with identity living in
run.json.
"""
from pathlib import Path

import pytest

from server import _next_palette_index, _run_dir_palette_subfolder


class TestNextPaletteIndex:
    """Pure-function tests for the index picker."""

    def test_empty_parent_starts_at_one(self):
        assert _next_palette_index([]) == 1

    def test_sequential_existing_children(self):
        assert _next_palette_index(["palette-01", "palette-02"]) == 3

    def test_gap_in_sequence_picks_above_max(self):
        # Gap tolerance: we do not fill holes, we take max+1
        assert _next_palette_index(["palette-01", "palette-03"]) == 4

    def test_ignores_non_matching_names(self):
        assert _next_palette_index(
            ["palette-01", "foo", "palette-xx", "README.md"]
        ) == 2

    def test_rolls_over_two_digits(self):
        assert _next_palette_index(["palette-99"]) == 100

    def test_mixed_width_takes_numeric_max(self):
        assert _next_palette_index(["palette-100", "palette-01"]) == 101

    def test_single_garbage_entry(self):
        # No valid palette-NN entries means next index is 1
        assert _next_palette_index(["garbage", ".DS_Store"]) == 1


class TestRunDirPaletteSubfolder:
    """Integration tests against a real tmp_path."""

    def test_creates_palette_01_in_empty_parent(self, tmp_path: Path):
        sub = _run_dir_palette_subfolder(tmp_path)
        assert sub.name == "palette-01"
        assert sub.is_dir()
        assert sub.parent == tmp_path

    def test_three_calls_produce_01_02_03(self, tmp_path: Path):
        first = _run_dir_palette_subfolder(tmp_path)
        second = _run_dir_palette_subfolder(tmp_path)
        third = _run_dir_palette_subfolder(tmp_path)
        assert [p.name for p in (first, second, third)] == [
            "palette-01",
            "palette-02",
            "palette-03",
        ]
        for p in (first, second, third):
            assert p.is_dir()

    def test_skips_over_existing_gap(self, tmp_path: Path):
        (tmp_path / "palette-01").mkdir()
        (tmp_path / "palette-03").mkdir()
        sub = _run_dir_palette_subfolder(tmp_path)
        assert sub.name == "palette-04"
        assert sub.is_dir()

    def test_ignores_unrelated_siblings(self, tmp_path: Path):
        (tmp_path / "notes.txt").write_text("hi")
        (tmp_path / "scratch").mkdir()
        sub = _run_dir_palette_subfolder(tmp_path)
        assert sub.name == "palette-01"

    def test_path_length_bounded(self, tmp_path: Path):
        # Proxy check: folder name itself is short and predictable.
        sub = _run_dir_palette_subfolder(tmp_path)
        assert len(sub.name) <= 12  # palette-NN with up to 4 digits stays ≤12
