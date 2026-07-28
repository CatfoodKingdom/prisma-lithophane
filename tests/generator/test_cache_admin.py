import os
import subprocess
import sys
from pathlib import Path
import pytest
from cache_admin import safe_clear_dir, safe_clear_dir_except, OutsideRootError


def _make_junction(link: Path, target: Path) -> bool:
    """Create a Windows directory junction (no elevation needed). Returns False if unavailable."""
    if sys.platform != "win32":
        return False
    try:
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True, text=True,
        )
        return result.returncode == 0 and link.exists()
    except OSError:
        return False


def test_clears_contents_keeps_root(tmp_path):
    root = tmp_path / "cache"
    (root / "runs" / "a").mkdir(parents=True)
    (root / "runs" / "a" / "f.txt").write_text("x")
    safe_clear_dir(root / "runs", root=root)
    assert (root / "runs").exists()           # root kept
    assert not any((root / "runs").iterdir())  # emptied


def test_refuses_target_outside_root(tmp_path):
    root = tmp_path / "cache"
    root.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with pytest.raises(OutsideRootError):
        safe_clear_dir(outside, root=root)


def test_does_not_follow_symlink_out_of_root(tmp_path):
    root = tmp_path / "cache"
    (root / "runs").mkdir(parents=True)
    precious = tmp_path / "precious"
    precious.mkdir()
    (precious / "keep.txt").write_text("do not delete")
    link = root / "runs" / "link"
    try:
        os.symlink(str(precious), str(link), target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink/junction creation not permitted in this environment")
    safe_clear_dir(root / "runs", root=root)
    assert precious.exists() and (precious / "keep.txt").exists()  # target survived


def test_does_not_follow_junction_out_of_root(tmp_path):
    # Windows junctions need no elevation, so this actually exercises the
    # reparse-point detection on Windows (the symlink test above usually skips).
    root = tmp_path / "cache"
    (root / "runs").mkdir(parents=True)
    precious = tmp_path / "precious"
    precious.mkdir()
    (precious / "keep.txt").write_text("do not delete")
    link = root / "runs" / "jct"
    if not _make_junction(link, precious):
        pytest.skip("directory junctions not available in this environment")
    removed = safe_clear_dir(root / "runs", root=root)
    # The junction entry was removed, but the target dir + its file survived.
    assert not link.exists()
    assert precious.exists() and (precious / "keep.txt").exists()
    assert removed == 1


def test_refuses_when_root_itself_is_a_junction(tmp_path):
    # If CACHE_DIR itself is a junction to an outside dir, deletion must be refused
    # (not redirected through the junction to delete the outside target's contents).
    real = tmp_path / "real_outside"
    (real / "runs").mkdir(parents=True)
    (real / "runs" / "keep.txt").write_text("do not delete")
    cache_link = tmp_path / "cache"  # becomes a junction -> real
    if not _make_junction(cache_link, real):
        pytest.skip("directory junctions not available in this environment")
    with pytest.raises(OutsideRootError):
        safe_clear_dir(cache_link / "runs", root=cache_link)
    assert (real / "runs" / "keep.txt").exists()  # outside target untouched


def test_refuses_when_parent_above_root_is_a_junction(tmp_path):
    # A junction ABOVE root (an ancestor of root itself, not between root and
    # target) must also be refused: it physically relocates the whole cache tree
    # outside the intended location. Reproduces the Stage-9a full-diff review escape.
    real = tmp_path / "real_outside"
    (real / "generator" / "cache" / "runs").mkdir(parents=True)
    (real / "generator" / "cache" / "runs" / "keep.txt").write_text("do not delete")
    data_link = tmp_path / "data"  # junction ABOVE root -> real
    if not _make_junction(data_link, real):
        pytest.skip("directory junctions not available in this environment")
    root = data_link / "generator" / "cache"
    target = root / "runs"
    with pytest.raises(OutsideRootError):
        safe_clear_dir(target, root=root)
    assert (real / "generator" / "cache" / "runs" / "keep.txt").exists()  # outside target untouched


def test_refuses_when_ancestor_component_is_a_junction(tmp_path):
    # A junction BETWEEN root and target must also be refused.
    root = tmp_path / "cache"
    root.mkdir()
    real = tmp_path / "real_outside"
    (real / "inner").mkdir(parents=True)
    (real / "inner" / "keep.txt").write_text("do not delete")
    mid = root / "mid"  # junction (under root) -> real
    if not _make_junction(mid, real):
        pytest.skip("directory junctions not available in this environment")
    with pytest.raises(OutsideRootError):
        safe_clear_dir(mid / "inner", root=root)
    assert (real / "inner" / "keep.txt").exists()  # outside target untouched


def test_does_not_follow_nested_junction(tmp_path):
    # A junction NESTED inside a real subdirectory must also be unlinked (not
    # recursed through) when _rm_tree descends into the real dir.
    root = tmp_path / "cache"
    real_sub = root / "runs" / "real_sub"
    real_sub.mkdir(parents=True)
    (real_sub / "inner.txt").write_text("delete me")
    precious = tmp_path / "precious"
    precious.mkdir()
    (precious / "keep.txt").write_text("do not delete")
    link = real_sub / "jct"
    if not _make_junction(link, precious):
        pytest.skip("directory junctions not available in this environment")
    safe_clear_dir(root / "runs", root=root)
    assert not (root / "runs" / "real_sub").exists()       # real subtree gone
    assert precious.exists() and (precious / "keep.txt").exists()  # junction target survived


def test_clear_except_preserves_only_named_direct_child(tmp_path):
    root = tmp_path / "cache"
    (root / "source-images").mkdir(parents=True)
    (root / "source-images" / "manifest.json").write_text("keep")
    (root / "runs").mkdir()
    (root / "runs" / "result.bin").write_text("remove")
    (root / "luts").mkdir()
    (root / "loose.tmp").write_text("remove")

    report = safe_clear_dir_except(
        root,
        root=root,
        preserve_names={"source-images"},
    )

    assert report == {
        "removed": 3,
        "preserved": ["source-images"],
        "failures": [],
    }
    assert (root / "source-images" / "manifest.json").read_text() == "keep"
    assert sorted(path.name for path in root.iterdir()) == ["source-images"]


def test_clear_except_removes_same_named_junction_instead_of_preserving(tmp_path):
    root = tmp_path / "cache"
    root.mkdir()
    precious = tmp_path / "precious"
    precious.mkdir()
    (precious / "keep.txt").write_text("keep")
    link = root / "source-images"
    if not _make_junction(link, precious):
        pytest.skip("directory junctions not available in this environment")

    report = safe_clear_dir_except(
        root,
        root=root,
        preserve_names={"source-images"},
    )

    assert report["removed"] == 1
    assert report["preserved"] == []
    assert not link.exists()
    assert (precious / "keep.txt").read_text() == "keep"


def test_clear_except_refuses_filesystem_root():
    anchor = Path(Path.cwd().anchor)
    with pytest.raises(OutsideRootError, match="unsafe clear root"):
        safe_clear_dir_except(anchor, root=anchor, preserve_names={"source-images"})


def test_clear_except_does_not_preserve_same_named_file(tmp_path):
    root = tmp_path / "cache"
    root.mkdir()
    (root / "source-images").write_text("not a subtree")

    report = safe_clear_dir_except(
        root,
        root=root,
        preserve_names={"source-images"},
    )

    assert report["removed"] == 1
    assert not (root / "source-images").exists()
