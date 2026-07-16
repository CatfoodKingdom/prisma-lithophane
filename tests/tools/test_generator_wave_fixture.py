from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.generator_wave_fixture import FixtureError, prepare_pair, verify_pair


def _seed_runtime(root: Path) -> Path:
    seed = root / "seed"
    files = {
        "Generator/Images/source.png": b"source-image",
        "Generator/Exports/old-export.txt": b"volatile export",
        "Generator/Workspace/cache/luts/cache.bin": b"volatile cache",
        "Generator/Workspace/logs/generator.log": b"volatile log",
        "Generator/Workspace/saved_runs/kept.zip": b"saved-run",
        "Generator/Workspace/.prisma-generator.lock": b"stale lock",
        "Generator/Model Libraries/library-a/manifest.json": b"{}",
        "Calibration/Workspace/private.sqlite3": b"must-not-copy",
    }
    for relative, content in files.items():
        path = seed / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return seed


def test_prepare_pair_sanitizes_only_copies_and_records_exact_manifest(tmp_path: Path) -> None:
    seed = _seed_runtime(tmp_path)
    manifest_path = prepare_pair(
        seed_root=seed,
        output_root=tmp_path / "pairs",
        name="wave3",
    )

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    before = Path(payload["before_root"])
    after = Path(payload["after_root"])
    assert (seed / "Generator/Exports/old-export.txt").is_file()
    assert (seed / "Generator/Workspace/cache/luts/cache.bin").is_file()
    for runtime in (before, after):
        assert not (runtime / "Calibration").exists()
        assert list((runtime / "Generator/Exports").iterdir()) == []
        assert list((runtime / "Generator/Workspace/cache").iterdir()) == []
        assert list((runtime / "Generator/Workspace/logs").iterdir()) == []
        assert not (runtime / "Generator/Workspace/.prisma-generator.lock").exists()
        assert (runtime / "Generator/Workspace/saved_runs/kept.zip").read_bytes() == b"saved-run"
    assert payload["file_count"] == 3
    assert verify_pair(manifest_path)["ok"] is True


def test_verify_pair_detects_mutation(tmp_path: Path) -> None:
    seed = _seed_runtime(tmp_path)
    manifest = prepare_pair(
        seed_root=seed,
        output_root=tmp_path / "pairs",
        name="wave4",
    )
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    changed = Path(payload["before_root"]) / "Generator/Images/source.png"
    changed.write_bytes(b"changed")

    report = verify_pair(manifest)
    assert report["ok"] is False
    assert report["before_matches_manifest"] is False
    assert report["after_matches_manifest"] is True
    assert report["pair_matches"] is False


def test_prepare_pair_refuses_overwrite_and_nested_output(tmp_path: Path) -> None:
    seed = _seed_runtime(tmp_path)
    output = tmp_path / "pairs"
    prepare_pair(seed_root=seed, output_root=output, name="wave5")
    with pytest.raises(FixtureError, match="refusing to overwrite"):
        prepare_pair(seed_root=seed, output_root=output, name="wave5")
    with pytest.raises(FixtureError, match="must not contain"):
        prepare_pair(seed_root=seed, output_root=seed / "nested", name="bad")
