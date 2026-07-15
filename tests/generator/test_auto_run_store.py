import json
import os

import pytest

import auto_run_store
import data_paths
import run_store


def _sidecar(save_id, saved_at):
    return {
        "save_id": save_id,
        "label": save_id,
        "saved_at": saved_at,
        "source_image_name": "steve.jpg",
        "palette": ["a"],
        "stats": {"mean_de": 1.0, "max_de": 2.0},
        "schema_version": 1,
        "tier": "auto",
    }


def test_auto_write_evicts_oldest(tmp_path, monkeypatch):
    monkeypatch.setattr(data_paths, "AUTO_RUNS_DIR", tmp_path / "auto")
    for i in range(4):
        auto_run_store.write_auto_run(
            f"run-{i}",
            f"ZIP-{i}".encode(),
            _sidecar(f"run-{i}", f"20260616-10150{i}"),
            limit=3,
        )

    listed = auto_run_store.list_auto_runs()
    assert [s["save_id"] for s in listed] == ["run-3", "run-2", "run-1"]
    assert not (data_paths.AUTO_RUNS_DIR / "run-0.zip").exists()
    assert not (data_paths.AUTO_RUNS_DIR / "run-0.json").exists()


def test_promote_moves_zip_to_saved_without_repack(tmp_path, monkeypatch):
    auto = tmp_path / "auto"
    saved = tmp_path / "saved"
    monkeypatch.setattr(data_paths, "AUTO_RUNS_DIR", auto)
    monkeypatch.setattr(data_paths, "SAVED_RUNS_DIR", saved)
    auto_run_store.write_auto_run("20260616-101500-steve", b"ZIP-BYTES", _sidecar("20260616-101500-steve", "20260616-101500"))
    # Force a collision in saved_runs; promote must suffix.
    run_store.write_save("20260616-101500-steve", b"EXISTING", {**_sidecar("20260616-101500-steve", "20260616-101400"), "tier": "saved"})

    promoted = auto_run_store.promote_auto_run("20260616-101500-steve", timestamp="20260616-101500")

    assert promoted["tier"] == "saved"
    assert promoted["save_id"] == "20260616-101500-steve-2"
    assert run_store.read_zip_bytes("20260616-101500-steve-2") == b"ZIP-BYTES"
    assert not (auto / "20260616-101500-steve.zip").exists()
    assert not (auto / "20260616-101500-steve.json").exists()


def test_promote_rolls_back_when_sidecar_publish_fails(tmp_path, monkeypatch):
    auto = tmp_path / "auto"
    saved = tmp_path / "saved"
    monkeypatch.setattr(data_paths, "AUTO_RUNS_DIR", auto)
    monkeypatch.setattr(data_paths, "SAVED_RUNS_DIR", saved)
    auto_run_store.write_auto_run(
        "20260616-101500-steve", b"ZIP-BYTES",
        _sidecar("20260616-101500-steve", "20260616-101500"))

    real_replace = os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        # 1st replace = zip move (ok); 2nd = sidecar publish (fail);
        # 3rd = the rollback zip move (must succeed).
        if calls["n"] == 2:
            raise OSError("sidecar publish failed")
        return real_replace(src, dst)

    monkeypatch.setattr(auto_run_store.os, "replace", flaky_replace)

    with pytest.raises(OSError):
        auto_run_store.promote_auto_run("20260616-101500-steve", timestamp="20260616-101500")

    # Auto record intact (zip rolled back, sidecar never removed); no saved orphan.
    assert auto_run_store.read_auto_zip_bytes("20260616-101500-steve") == b"ZIP-BYTES"
    assert [s["save_id"] for s in auto_run_store.list_auto_runs()] == ["20260616-101500-steve"]
    assert list(saved.glob("*.zip")) == []
    assert list(saved.glob("*.json")) == []


def test_promote_cleans_staged_sidecar_when_zip_move_fails(tmp_path, monkeypatch):
    auto = tmp_path / "auto"
    saved = tmp_path / "saved"
    monkeypatch.setattr(data_paths, "AUTO_RUNS_DIR", auto)
    monkeypatch.setattr(data_paths, "SAVED_RUNS_DIR", saved)
    auto_run_store.write_auto_run(
        "20260616-101500-steve",
        b"ZIP-BYTES",
        _sidecar("20260616-101500-steve", "20260616-101500"),
    )

    def fail_zip_move(src, dst):  # type: ignore[no-untyped-def]
        raise OSError("zip move failed")

    monkeypatch.setattr(auto_run_store.os, "replace", fail_zip_move)

    with pytest.raises(OSError, match="zip move failed"):
        auto_run_store.promote_auto_run(
            "20260616-101500-steve",
            timestamp="20260616-101500",
        )

    assert auto_run_store.read_auto_zip_bytes("20260616-101500-steve") == b"ZIP-BYTES"
    assert [save["save_id"] for save in auto_run_store.list_auto_runs()] == [
        "20260616-101500-steve"
    ]
    assert list(saved.iterdir()) == []
