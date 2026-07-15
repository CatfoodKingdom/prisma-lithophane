from __future__ import annotations

import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

import backup_restore
import path_safety
from sample_visuals import remove_sample_visuals
from sqlite_data_access import SQLiteDataStore


def test_sample_visual_cleanup_refuses_linked_thumbnail_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = tmp_path / "assets"
    sample_dir = data_root / "thumbnails" / "exp-001"
    visual = sample_dir / "source.jpg"
    visual.parent.mkdir(parents=True)
    visual.write_bytes(b"keep")
    linked_root = data_root / "thumbnails"
    real_is_linklike = path_safety.is_linklike
    monkeypatch.setattr(
        path_safety,
        "is_linklike",
        lambda path: Path(path) == linked_root or real_is_linklike(Path(path)),
    )

    with pytest.raises(path_safety.UnsafeManagedPathError):
        remove_sample_visuals(data_root, "exp-001")

    assert visual.read_bytes() == b"keep"


def test_safe_rmtree_refuses_link_nested_anywhere_in_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "managed"
    target = root / "candidate"
    nested = target / "nested"
    nested.mkdir(parents=True)
    payload = nested / "keep.txt"
    payload.write_text("keep", encoding="utf-8")
    real_is_linklike = path_safety.is_linklike
    monkeypatch.setattr(
        path_safety,
        "is_linklike",
        lambda path: Path(path) == nested or real_is_linklike(Path(path)),
    )

    with pytest.raises(path_safety.UnsafeManagedPathError):
        path_safety.safe_rmtree(target, root)

    assert payload.read_text(encoding="utf-8") == "keep"


def test_safe_rmtree_removes_readonly_files(tmp_path: Path) -> None:
    root = tmp_path / "managed"
    target = root / "candidate"
    payload = target / "source.CR2"
    payload.parent.mkdir(parents=True)
    payload.write_bytes(b"raw")
    payload.chmod(stat.S_IREAD)

    path_safety.safe_rmtree(target, root)

    assert not target.exists()


def test_safe_unlink_removes_readonly_file(tmp_path: Path) -> None:
    root = tmp_path / "managed"
    payload = root / "images" / "source.CR2"
    payload.parent.mkdir(parents=True)
    payload.write_bytes(b"raw")
    payload.chmod(stat.S_IREAD)

    path_safety.safe_unlink(payload, root)

    assert not payload.exists()


def test_raw_archive_release_path_check_refuses_linked_images_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "assets"
    image_path = root / "images" / "img-001" / "test.CR2"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"raw")
    linked_root = root / "images"
    real_is_linklike = path_safety.is_linklike
    monkeypatch.setattr(
        path_safety,
        "is_linklike",
        lambda path: Path(path) == linked_root or real_is_linklike(Path(path)),
    )
    store = SimpleNamespace(root=root)

    with pytest.raises(backup_restore.BackupRestoreError):
        backup_restore._image_manifest_rel_path(store, {"path": str(image_path)})

    assert image_path.read_bytes() == b"raw"


def test_sqlite_store_refuses_hardlinked_mutable_database(tmp_path: Path) -> None:
    original = tmp_path / "original.sqlite3"
    linked = tmp_path / "linked.sqlite3"
    original.write_bytes(b"not opened because link validation runs first")
    linked.hardlink_to(original)
    asset_root = tmp_path / "assets"
    asset_root.mkdir()

    with pytest.raises(path_safety.UnsafeManagedPathError, match="hardlinks"):
        SQLiteDataStore(linked, asset_root=asset_root)

    assert original.read_bytes() == b"not opened because link validation runs first"
