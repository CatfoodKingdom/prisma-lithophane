from __future__ import annotations

from pathlib import Path

from Prisma.lib import export_stage_recovery


def test_recovery_removes_only_exact_ordinary_export_stages_and_is_idempotent(tmp_path: Path) -> None:
    export_root = tmp_path / "Exports"
    export_root.mkdir()
    stages = [
        export_root / ".export-stage-20260713-123456-cat-deadbeef",
        export_root / ".export-stage-export-0123abcd",
    ]
    for stage in stages:
        (stage / "stls").mkdir(parents=True)
        (stage / "stls" / "partial.stl").write_bytes(b"partial")
    final_export = export_root / "20260713-123456-cat"
    final_export.mkdir()
    user_file = export_root / "notes.txt"
    user_file.write_text("keep", encoding="utf-8")
    unrecognized = export_root / ".export-stage-export-not-hex"
    unrecognized.mkdir()
    exact_named_file = export_root / ".export-stage-export-feedface"
    exact_named_file.write_text("not a directory", encoding="utf-8")

    first = export_stage_recovery.reconcile_interrupted_export_stages(export_root)
    second = export_stage_recovery.reconcile_interrupted_export_stages(export_root)

    assert sorted(first["removed"]) == sorted(str(path) for path in stages)
    assert second["removed"] == []
    assert final_export.is_dir()
    assert user_file.read_text(encoding="utf-8") == "keep"
    assert unrecognized.is_dir()
    assert exact_named_file.read_text(encoding="utf-8") == "not a directory"
    statuses = {finding["status"] for finding in first["findings"]}
    assert statuses == {"preserved_unrecognized_stage", "preserved_unsafe_stage"}


def test_recovery_preserves_linklike_or_link_containing_stage(
    tmp_path: Path,
    monkeypatch,
) -> None:
    export_root = tmp_path / "Exports"
    export_root.mkdir()
    linklike = export_root / ".export-stage-first-deadbeef"
    nested_link = export_root / ".export-stage-second-feedface"
    linklike.mkdir()
    nested_link.mkdir()
    real_is_linklike = export_stage_recovery._is_linklike
    real_tree_contains_link = export_stage_recovery._tree_contains_link
    monkeypatch.setattr(
        export_stage_recovery,
        "_is_linklike",
        lambda path: Path(path) == linklike or real_is_linklike(path),
    )
    monkeypatch.setattr(
        export_stage_recovery,
        "_tree_contains_link",
        lambda path: Path(path) == nested_link or real_tree_contains_link(path),
    )

    report = export_stage_recovery.reconcile_interrupted_export_stages(export_root)

    assert report["removed"] == []
    assert linklike.exists()
    assert nested_link.exists()
    assert [finding["status"] for finding in report["findings"]] == [
        "preserved_unsafe_stage",
        "preserved_unsafe_stage",
    ]
