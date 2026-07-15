import json
from pathlib import Path

import pytest

import run_store


@pytest.fixture()
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(run_store.data_paths, "SAVED_RUNS_DIR", tmp_path)
    return tmp_path


def _sidecar(save_id="20260616-101500-run"):
    return {"save_id": save_id, "label": "My Run", "saved_at": "20260616-101500",
            "source_image_name": "steve.jpg", "palette": ["a", "b"],
            "stats": {"mean_de": 1.2, "max_de": 4.0}, "schema_version": 1}


def test_write_then_list_newest_first(root):
    run_store.write_save("20260616-101500-a", b"PK-zip-a", {**_sidecar("20260616-101500-a"), "saved_at": "20260616-101500"})
    run_store.write_save("20260616-101600-b", b"PK-zip-b", {**_sidecar("20260616-101600-b"), "saved_at": "20260616-101600"})
    listed = run_store.list_saves()
    assert [s["save_id"] for s in listed] == ["20260616-101600-b", "20260616-101500-a"]
    assert (root / "20260616-101500-a.zip").read_bytes() == b"PK-zip-a"


def test_read_zip_bytes(root):
    run_store.write_save("s1", b"ZIPDATA", _sidecar("s1"))
    assert run_store.read_zip_bytes("s1") == b"ZIPDATA"


def test_delete_is_idempotent(root):
    run_store.write_save("s1", b"x", _sidecar("s1"))
    run_store.delete_save("s1")
    assert not (root / "s1.zip").exists() and not (root / "s1.json").exists()
    run_store.delete_save("s1")  # second delete: no error


def test_delete_raises_typed_error_when_file_locked(root, monkeypatch):
    """A locked/open file makes unlink raise PermissionError (an OSError that
    missing_ok does NOT swallow). delete_save must surface this as a typed
    SaveDeletionError, not let a raw OSError escape."""
    run_store.write_save("s1", b"x", _sidecar("s1"))

    def _boom(self, *a, **k):
        raise PermissionError("file is in use")

    monkeypatch.setattr(Path, "unlink", _boom)
    with pytest.raises(run_store.SaveDeletionError) as exc:
        run_store.delete_save("s1")
    assert "s1" in str(exc.value)


def test_rename_updates_sidecar_only(root):
    run_store.write_save("s1", b"x", _sidecar("s1"))
    run_store.rename_save("s1", "Renamed")
    assert json.loads((root / "s1.json").read_text())["label"] == "Renamed"
    assert (root / "s1.zip").read_bytes() == b"x"  # zip untouched


def test_resolve_rejects_bad_save_id(root):
    for bad in ("../escape", "a/b", "C:/x", "a.zip"):
        with pytest.raises(run_store.SaveNotFoundError):
            run_store.read_zip_bytes(bad)


def test_store_can_target_distinct_roots(tmp_path):
    saved = tmp_path / "saved"
    auto = tmp_path / "auto"
    run_store.write_save("same-id", b"SAVED", _sidecar("same-id"), root=saved)
    run_store.write_save("same-id", b"AUTO", {**_sidecar("same-id"), "tier": "auto"}, root=auto)

    assert run_store.read_zip_bytes("same-id", root=saved) == b"SAVED"
    assert run_store.read_zip_bytes("same-id", root=auto) == b"AUTO"
    assert run_store.list_saves(root=saved)[0].get("tier") is None
    assert run_store.list_saves(root=auto)[0]["tier"] == "auto"


def test_resolve_save_paths_rejects_traversal_for_any_root(tmp_path):
    for bad in ("../escape", "a/b", "C:/x", "a.zip"):
        with pytest.raises(run_store.SaveNotFoundError):
            run_store.resolve_save_paths(bad, root=tmp_path)
