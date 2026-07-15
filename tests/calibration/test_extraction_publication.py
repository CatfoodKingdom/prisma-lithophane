from __future__ import annotations

from pathlib import Path
import sqlite3
import subprocess
import sys

import numpy as np
import pytest

from processing.extraction_publication import (
    ExtractionPublicationError,
    LIVE_VISUAL_KINDS,
    create_visual_stage,
    mark_origin_complete,
    prepare_publication,
    publication_root,
    publish_extraction_update,
    reconcile_publications,
    resolve_visual_path,
    visual_stage_root,
)


class _FakeSQLiteStore:
    backend = "sqlite"

    def __init__(self, root: Path, current_id: str = "ext_old") -> None:
        self.root = root
        self.current_id = current_id

    def get_extraction_result(self, sample_id: str):
        if not self.current_id:
            return None
        return {
            "sample_id": sample_id,
            "extraction_result_id": self.current_id,
        }


class _PersistentSQLiteStore:
    backend = "sqlite"

    def __init__(self, root: Path) -> None:
        self.root = root
        self.database = root / "state.sqlite"

    def get_extraction_result(self, sample_id: str):
        with sqlite3.connect(self.database) as connection:
            row = connection.execute(
                "SELECT current_id FROM extraction_state WHERE sample_id = ?",
                (sample_id,),
            ).fetchone()
        if row is None or not row[0]:
            return None
        return {"sample_id": sample_id, "extraction_result_id": str(row[0])}


def _seed_persistent_store(root: Path) -> _PersistentSQLiteStore:
    root.mkdir(parents=True, exist_ok=True)
    store = _PersistentSQLiteStore(root)
    with sqlite3.connect(store.database) as connection:
        connection.execute(
            "CREATE TABLE extraction_state (sample_id TEXT PRIMARY KEY, current_id TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO extraction_state (sample_id, current_id) VALUES (?, ?)",
            ("exp-001", "ext_old"),
        )
    return store


def _write_visuals(root: Path, prefix: str) -> dict[str, Path]:
    root.mkdir(parents=True, exist_ok=True)
    result: dict[str, Path] = {}
    for kind in LIVE_VISUAL_KINDS:
        path = root / f"{kind}.jpg"
        path.write_bytes(f"{prefix}-{kind}".encode("ascii"))
        result[kind] = path
    return result


def _live_visuals(store: _FakeSQLiteStore, prefix: str = "old") -> dict[str, Path]:
    return _write_visuals(store.root / "thumbnails" / "exp-001", prefix)


def test_publication_commits_both_visuals_and_removes_journal(tmp_path: Path) -> None:
    store = _FakeSQLiteStore(tmp_path)
    live = _live_visuals(store)
    staged = _write_visuals(tmp_path / "candidate", "new")

    def commit() -> str:
        store.current_id = "ext_new"
        return "committed"

    outcome = publish_extraction_update(
        store,
        sample_id="exp-001",
        prior_extraction_result_id="ext_old",
        replacement_extraction_result_id="ext_new",
        semantic_change=True,
        origin="automatic",
        visual_paths=staged,
        semantic_commit=commit,
    )

    assert outcome.semantic_result == "committed"
    assert outcome.visuals_published is True
    assert outcome.pending_recovery is False
    assert {kind: path.read_bytes() for kind, path in live.items()} == {
        "source": b"new-source",
        "strip": b"new-strip",
    }
    assert not publication_root(store).exists() or not any(publication_root(store).iterdir())


def test_failed_semantic_commit_discards_journal_and_keeps_old_visuals(tmp_path: Path) -> None:
    store = _FakeSQLiteStore(tmp_path)
    live = _live_visuals(store)
    staged = _write_visuals(tmp_path / "candidate", "new")

    with pytest.raises(RuntimeError, match="semantic failure"):
        publish_extraction_update(
            store,
            sample_id="exp-001",
            prior_extraction_result_id="ext_old",
            replacement_extraction_result_id="ext_new",
            semantic_change=True,
            origin="manual",
            visual_paths=staged,
            semantic_commit=lambda: (_ for _ in ()).throw(RuntimeError("semantic failure")),
        )

    assert {kind: path.read_bytes() for kind, path in live.items()} == {
        "source": b"old-source",
        "strip": b"old-strip",
    }
    assert not publication_root(store).exists() or not any(publication_root(store).iterdir())


def test_replace_failure_rolls_live_pair_back_and_journal_recovers_forward(tmp_path: Path) -> None:
    store = _FakeSQLiteStore(tmp_path)
    live = _live_visuals(store)
    staged = _write_visuals(tmp_path / "candidate", "new")
    replaced = 0

    def fault_hook(boundary: str, _record) -> None:
        nonlocal replaced
        if boundary == "after_live_replace":
            replaced += 1
            if replaced == 1:
                raise RuntimeError("injected replace failure")

    def commit() -> None:
        store.current_id = "ext_new"

    outcome = publish_extraction_update(
        store,
        sample_id="exp-001",
        prior_extraction_result_id="ext_old",
        replacement_extraction_result_id="ext_new",
        semantic_change=True,
        origin="automatic",
        visual_paths=staged,
        semantic_commit=commit,
        fault_hook=fault_hook,
    )

    assert outcome.pending_recovery is True
    assert "injected replace failure" in outcome.publication_error
    assert {kind: path.read_bytes() for kind, path in live.items()} == {
        "source": b"old-source",
        "strip": b"old-strip",
    }
    assert resolve_visual_path(store, "exp-001", "source").read_bytes() == b"new-source"
    assert resolve_visual_path(store, "exp-001", "strip").read_bytes() == b"new-strip"

    first = reconcile_publications(store)
    second = reconcile_publications(store)

    assert first["pending_finalization"] == []
    assert second["pending_finalization"] == []
    assert {kind: path.read_bytes() for kind, path in live.items()} == {
        "source": b"new-source",
        "strip": b"new-strip",
    }
    assert not publication_root(store).exists() or not any(publication_root(store).iterdir())


def test_prepared_semantic_change_is_discarded_when_database_is_old(tmp_path: Path) -> None:
    store = _FakeSQLiteStore(tmp_path)
    live = _live_visuals(store)
    staged = _write_visuals(tmp_path / "candidate", "new")
    prepare_publication(
        store,
        sample_id="exp-001",
        prior_extraction_result_id="ext_old",
        replacement_extraction_result_id="ext_new",
        semantic_change=True,
        origin="automatic",
        visual_paths=staged,
    )

    result = reconcile_publications(store)

    assert any(item["status"] == "discarded_uncommitted_or_superseded" for item in result["findings"])
    assert {kind: path.read_bytes() for kind, path in live.items()} == {
        "source": b"old-source",
        "strip": b"old-strip",
    }


def test_unchanged_publication_treats_durable_prepare_as_commit_point(tmp_path: Path) -> None:
    store = _FakeSQLiteStore(tmp_path)
    live = _live_visuals(store)
    staged = _write_visuals(tmp_path / "candidate", "new")
    prepare_publication(
        store,
        sample_id="exp-001",
        prior_extraction_result_id="ext_old",
        replacement_extraction_result_id="ext_old",
        semantic_change=False,
        origin="reextract",
        visual_paths=staged,
        origin_metadata={"candidate_set_id": "rext_123", "sample_id": "exp-001"},
    )

    result = reconcile_publications(store)

    assert len(result["pending_finalization"]) == 1
    record = result["pending_finalization"][0]
    assert {kind: path.read_bytes() for kind, path in live.items()} == {
        "source": b"new-source",
        "strip": b"new-strip",
    }
    mark_origin_complete(store, record)
    assert not publication_root(store).exists() or not any(publication_root(store).iterdir())


def test_publication_rejects_inconsistent_semantic_change_ids(tmp_path: Path) -> None:
    store = _FakeSQLiteStore(tmp_path)
    staged = _write_visuals(tmp_path / "candidate", "new")

    with pytest.raises(ValueError, match="must replace"):
        prepare_publication(
            store,
            sample_id="exp-001",
            prior_extraction_result_id="ext_old",
            replacement_extraction_result_id="ext_old",
            semantic_change=True,
            origin="automatic",
            visual_paths=staged,
        )
    with pytest.raises(ValueError, match="must retain"):
        prepare_publication(
            store,
            sample_id="exp-001",
            prior_extraction_result_id="ext_old",
            replacement_extraction_result_id="ext_new",
            semantic_change=False,
            origin="reextract",
            visual_paths=staged,
        )


def test_origin_completion_refuses_a_superseded_publication(tmp_path: Path) -> None:
    store = _FakeSQLiteStore(tmp_path)
    staged = _write_visuals(tmp_path / "candidate", "new")
    record = prepare_publication(
        store,
        sample_id="exp-001",
        prior_extraction_result_id="ext_old",
        replacement_extraction_result_id="ext_old",
        semantic_change=False,
        origin="reextract",
        visual_paths=staged,
    )
    store.current_id = "ext_later"

    with pytest.raises(ExtractionPublicationError, match="superseded"):
        mark_origin_complete(store, record)
    assert record.directory.exists()


def test_visual_resolution_does_not_clean_abandoned_stage(tmp_path: Path) -> None:
    store = _FakeSQLiteStore(tmp_path)
    live = _live_visuals(store)
    root = publication_root(store)
    abandoned = root / ("pub_" + "2" * 32)
    abandoned.mkdir(parents=True)
    (abandoned / "source.jpg").write_bytes(b"partial")

    assert resolve_visual_path(store, "exp-001", "source") == live["source"]
    assert abandoned.exists()


def test_abandoned_prejournal_directory_is_removed_but_unknown_entry_is_preserved(tmp_path: Path) -> None:
    store = _FakeSQLiteStore(tmp_path)
    root = publication_root(store)
    abandoned = root / ("pub_" + "1" * 32)
    abandoned.mkdir(parents=True)
    (abandoned / "source.jpg").write_bytes(b"partial")
    unknown = root / "notes"
    unknown.mkdir()

    result = reconcile_publications(store)

    assert not abandoned.exists()
    assert unknown.exists()
    assert any(item["status"] == "removed_abandoned_stage" for item in result["findings"])
    assert any(item["status"] == "preserved_unknown" for item in result["findings"])


def test_internal_visual_stage_is_unobservable_and_removed_on_recovery(tmp_path: Path) -> None:
    store = _FakeSQLiteStore(tmp_path)
    sink = create_visual_stage(store, "exp-001")
    image = np.full((8, 12, 3), 127, dtype=np.uint8)
    sink.write_image("exp-001", "source", image)
    sink.write_image("exp-001", "strip", image)

    assert set(sink.visual_paths()) == {"source", "strip"}
    assert not (store.root / "thumbnails" / "exp-001").exists()

    result = reconcile_publications(store)

    assert not sink.sample_dir.exists()
    assert not visual_stage_root(store).exists() or not any(visual_stage_root(store).iterdir())
    assert any(item["status"] == "removed_abandoned_visual_stage" for item in result["findings"])


@pytest.mark.parametrize(
    ("kill_boundary", "expected_prefix"),
    [
        ("after_source_payload", "old"),
        ("after_strip_payload", "old"),
        ("after_prepared", "old"),
        ("after_sqlite_commit_before_phase", "new"),
        ("after_semantic_committed", "new"),
        ("after_source_live_stage", "new"),
        ("after_strip_live_stage", "new"),
        ("after_live_backup:1", "new"),
        ("after_live_backup:2", "new"),
        ("after_live_replace:1", "new"),
        ("after_live_replace:2", "new"),
        ("after_visuals_published", "new"),
    ],
)
def test_process_kill_converges_to_wholly_old_or_new_publication(
    tmp_path: Path,
    kill_boundary: str,
    expected_prefix: str,
) -> None:
    store = _seed_persistent_store(tmp_path)
    live = _live_visuals(store)
    unrelated = live["source"].parent / ".source.publication.user-note.txt"
    unrelated.write_bytes(b"not a Prisma publication temporary")
    _write_visuals(tmp_path / "candidate", "new")
    worker = Path(__file__).with_name("_extraction_publication_kill_worker.py")

    completed = subprocess.run(
        [sys.executable, str(worker), str(tmp_path), kill_boundary],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 91, completed.stderr
    expected_id = f"ext_{expected_prefix}"
    assert store.get_extraction_result("exp-001")["extraction_result_id"] == expected_id
    # Even if the fixed live filenames were interrupted between replacements,
    # the application resolver must expose one coherent journal-backed pair.
    assert resolve_visual_path(store, "exp-001", "source").read_bytes() == (
        f"{expected_prefix}-source".encode("ascii")
    )
    assert resolve_visual_path(store, "exp-001", "strip").read_bytes() == (
        f"{expected_prefix}-strip".encode("ascii")
    )

    first = reconcile_publications(store)
    second = reconcile_publications(store)

    assert first["pending_finalization"] == []
    assert second["pending_finalization"] == []
    assert {kind: path.read_bytes() for kind, path in live.items()} == {
        "source": f"{expected_prefix}-source".encode("ascii"),
        "strip": f"{expected_prefix}-strip".encode("ascii"),
    }
    assert not publication_root(store).exists() or not any(publication_root(store).iterdir())
    assert not [
        path
        for path in live["source"].parent.iterdir()
        if ".publication." in path.name or ".rollback." in path.name
        if path != unrelated
    ]
    assert unrelated.read_bytes() == b"not a Prisma publication temporary"
