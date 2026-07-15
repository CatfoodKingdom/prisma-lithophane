from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from scripts.create_calibration_data_sandbox import (
    MARKER_NAME,
    SandboxError,
    configure_worktree,
    create_sandbox,
    verify_sandbox,
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_data_root(tmp_path: Path) -> Path:
    root = tmp_path / "source" / "data"
    image = root / "images" / "sample.cr2"
    profile = root / "filaments" / "profiles" / "red.json"
    image.parent.mkdir(parents=True)
    profile.parent.mkdir(parents=True)
    image.write_bytes(b"independent raw bytes")
    profile.write_text('{"filament_id":"red"}\n', encoding="utf-8")
    cache = root / "generator" / "cache" / "luts" / "derived.bin"
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"discard me")

    db = root / "calibration.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE image_assets (
              image_asset_id TEXT PRIMARY KEY,
              managed_rel_path TEXT,
              content_sha256 TEXT
            );
            CREATE TABLE model_fits (
              model_fit_id TEXT PRIMARY KEY,
              model_kind TEXT NOT NULL,
              currentness_state TEXT NOT NULL,
              generated_at TEXT NOT NULL,
              artifact_root_rel_path TEXT
            );
            CREATE TABLE model_fit_contributors (
              model_fit_id TEXT NOT NULL,
              sample_id TEXT NOT NULL,
              PRIMARY KEY(model_fit_id, sample_id),
              FOREIGN KEY(model_fit_id) REFERENCES model_fits(model_fit_id) ON DELETE CASCADE
            );
            CREATE TABLE model_artifacts (
              model_artifact_id TEXT PRIMARY KEY,
              model_fit_id TEXT NOT NULL,
              artifact_rel_path TEXT NOT NULL,
              content_sha256 TEXT,
              FOREIGN KEY(model_fit_id) REFERENCES model_fits(model_fit_id) ON DELETE CASCADE
            );
            """
        )
        conn.execute(
            "INSERT INTO image_assets VALUES ('image-1', 'images/sample.cr2', ?)",
            (_sha(image),),
        )
        conn.execute(
            "INSERT INTO model_fits VALUES ('fit-1', 'legacy_spline', 'current', '2026-07-11T00:00:00Z', 'filaments')"
        )
        conn.execute(
            "INSERT INTO model_artifacts VALUES ('artifact-1', 'fit-1', 'filaments/profiles/red.json', ?)",
            (_sha(profile),),
        )
    return root


def test_create_uses_independent_files_and_excludes_cache(tmp_path: Path) -> None:
    source = _source_data_root(tmp_path)
    sandbox_parent = tmp_path / "sandboxes"
    target = sandbox_parent / "case" / "working" / "data"

    result = create_sandbox(
        source_root=source,
        target_root=target,
        sandbox_parent=sandbox_parent,
        role="working",
        progress=False,
    )

    assert result["ok"] is True
    assert (target / MARKER_NAME).is_file()
    assert (target / "images" / "sample.cr2").read_bytes() == b"independent raw bytes"
    assert not (target / "generator" / "cache").exists()
    (target / "images" / "sample.cr2").write_bytes(b"sandbox mutation")
    assert (source / "images" / "sample.cr2").read_bytes() == b"independent raw bytes"
    with pytest.raises(SandboxError, match="image hash mismatch"):
        verify_sandbox(target)


def test_replace_requires_a_valid_marker(tmp_path: Path) -> None:
    source = _source_data_root(tmp_path)
    sandbox_parent = tmp_path / "sandboxes"
    target = sandbox_parent / "case" / "working" / "data"
    target.mkdir(parents=True)
    (target / "unrelated.txt").write_text("do not delete", encoding="utf-8")

    with pytest.raises(SandboxError, match="unmarked"):
        create_sandbox(
            source_root=source,
            target_root=target,
            sandbox_parent=sandbox_parent,
            role="working",
            replace=True,
            progress=False,
        )
    assert (target / "unrelated.txt").is_file()


def test_replace_rebuilds_a_marked_sandbox(tmp_path: Path) -> None:
    source = _source_data_root(tmp_path)
    sandbox_parent = tmp_path / "sandboxes"
    target = sandbox_parent / "case" / "working" / "data"
    first = create_sandbox(
        source_root=source,
        target_root=target,
        sandbox_parent=sandbox_parent,
        role="working",
        progress=False,
    )
    first_id = first["marker"]["sandbox_id"]
    source_image = source / "images" / "sample.cr2"
    source_image.write_bytes(b"replacement source bytes")
    with sqlite3.connect(source / "calibration.sqlite3") as conn:
        conn.execute(
            "UPDATE image_assets SET content_sha256 = ? WHERE image_asset_id = 'image-1'",
            (_sha(source_image),),
        )

    second = create_sandbox(
        source_root=source,
        target_root=target,
        sandbox_parent=sandbox_parent,
        role="working",
        replace=True,
        progress=False,
    )

    assert second["marker"]["sandbox_id"] != first_id
    assert (target / "images" / "sample.cr2").read_bytes() == b"replacement source bytes"


def test_rejects_overlapping_source_and_target(tmp_path: Path) -> None:
    source = _source_data_root(tmp_path)
    with pytest.raises(SandboxError, match="inside the source"):
        create_sandbox(
            source_root=source,
            target_root=source / "nested",
            sandbox_parent=source,
            role="working",
            progress=False,
        )


def test_verify_rejects_missing_current_artifact(tmp_path: Path) -> None:
    source = _source_data_root(tmp_path)
    sandbox_parent = tmp_path / "sandboxes"
    target = sandbox_parent / "case" / "working" / "data"
    create_sandbox(
        source_root=source,
        target_root=target,
        sandbox_parent=sandbox_parent,
        role="working",
        progress=False,
    )
    (target / "filaments" / "profiles" / "red.json").unlink()
    with pytest.raises(SandboxError, match="current model artifact"):
        verify_sandbox(target)


def test_configure_requires_working_role(tmp_path: Path) -> None:
    source = _source_data_root(tmp_path)
    sandbox_parent = tmp_path / "sandboxes"
    baseline = sandbox_parent / "case" / "baseline" / "data"
    create_sandbox(
        source_root=source,
        target_root=baseline,
        sandbox_parent=sandbox_parent,
        role="baseline",
        progress=False,
    )
    worktree = tmp_path / "worktree"
    (worktree / "Prisma" / "calibration").mkdir(parents=True)
    (worktree / "Prisma" / "generator").mkdir(parents=True)
    (worktree / "Prisma" / "calibration" / "server.py").write_text("", encoding="utf-8")
    with pytest.raises(SandboxError, match="role='working'"):
        configure_worktree(worktree_root=worktree, data_root=baseline)


def test_configure_writes_worktree_local_pointers(tmp_path: Path) -> None:
    source = _source_data_root(tmp_path)
    sandbox_parent = tmp_path / "sandboxes"
    working = sandbox_parent / "case" / "working" / "data"
    create_sandbox(
        source_root=source,
        target_root=working,
        sandbox_parent=sandbox_parent,
        role="working",
        progress=False,
    )
    worktree = tmp_path / "worktree"
    calibration = worktree / "Prisma" / "calibration"
    generator = worktree / "Prisma" / "generator"
    calibration.mkdir(parents=True)
    generator.mkdir(parents=True)
    (calibration / "server.py").write_text("", encoding="utf-8")

    result = configure_worktree(worktree_root=worktree, data_root=working)

    assert result["ok"] is True
    assert (calibration / ".backend").read_text(encoding="utf-8").strip() == "sqlite"
    assert Path((calibration / ".sqlite-path").read_text(encoding="utf-8").strip()) == working / "calibration.sqlite3"
    assert Path((calibration / ".asset-root").read_text(encoding="utf-8").strip()) == working
    assert Path((generator / ".data-root").read_text(encoding="utf-8").strip()) == working
