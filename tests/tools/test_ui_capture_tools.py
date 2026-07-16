from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from tools.ui_diff import diff_images
from tools.ui_screenshot import APP_REGISTRY, main as screenshot_main


def _image(path: Path, color: tuple[int, int, int], size: tuple[int, int] = (4, 3)) -> None:
    Image.new("RGB", size, color).save(path)


def test_ui_diff_detects_non_red_channel_changes(tmp_path: Path) -> None:
    before = tmp_path / "before.png"
    after = tmp_path / "after.png"
    _image(before, (0, 0, 0))
    _image(after, (0, 0, 1))

    report = diff_images(before, after, tmp_path / "diff.png")
    assert report["changed_pixels"] == 12
    assert report["max_channel_delta"] == 1
    assert report["exact_match"] is False


def test_ui_diff_masks_only_the_declared_rectangle(tmp_path: Path) -> None:
    before = tmp_path / "before.png"
    after = tmp_path / "after.png"
    _image(before, (0, 0, 0))
    image = Image.open(before).convert("RGB")
    image.putpixel((1, 1), (0, 9, 0))
    image.save(after)

    report = diff_images(
        before,
        after,
        tmp_path / "diff.png",
        masks=[(1, 1, 1, 1)],
    )
    assert report["changed_pixels"] == 0
    assert report["exact_match"] is True
    assert report["max_channel_delta"] == 0
    assert report["raw_max_channel_delta"] == 9


def test_ui_diff_refuses_dimension_mismatch_by_default(tmp_path: Path) -> None:
    before = tmp_path / "before.png"
    after = tmp_path / "after.png"
    _image(before, (0, 0, 0), (4, 3))
    _image(after, (0, 0, 0), (5, 3))
    with pytest.raises(ValueError, match="refusing to resize"):
        diff_images(before, after, tmp_path / "diff.png")


def test_generator_screenshot_recipes_match_current_navigation(capsys) -> None:
    recipes = APP_REGISTRY["generator"]["recipes"]
    assert {
        "image",
        "palette",
        "preview",
        "export",
        "settings",
        "saved-runs",
        "loaded-run",
        "loaded-export",
    } <= set(recipes)
    assert "comparison" not in recipes
    assert any(
        step.get("selector") == "#settingsDrawerBtn" for step in recipes["settings"]
    )
    assert any(
        step.get("action") == "click_first" and step.get("selector") == ".saved-run-row"
        for step in recipes["loaded-run"]
    )
    assert screenshot_main(["--app", "generator", "--list-views"]) == 0
    assert "saved-runs" in capsys.readouterr().out
