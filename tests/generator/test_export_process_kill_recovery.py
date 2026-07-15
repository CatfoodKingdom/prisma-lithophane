from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest

from Prisma.lib.export_stage_recovery import reconcile_interrupted_export_stages


@pytest.mark.parametrize(
    ("boundary", "published"),
    [
        ("after_stage_creation", False),
        ("during_partial_staging", False),
        ("before_final_rename", False),
        ("after_final_rename", True),
    ],
)
def test_process_kill_recovery_preserves_only_published_final_export(
    tmp_path: Path,
    boundary: str,
    published: bool,
) -> None:
    export_root = tmp_path / "Exports"
    user_file = export_root / "user-note.txt"
    export_root.mkdir()
    user_file.write_text("keep", encoding="utf-8")
    worker = Path(__file__).with_name("_export_stage_kill_worker.py")

    completed = subprocess.run(
        [sys.executable, str(worker), str(export_root), boundary],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 92, completed.stderr
    first = reconcile_interrupted_export_stages(export_root)
    second = reconcile_interrupted_export_stages(export_root)
    final = export_root / "20260713-123456-process-kill"
    if published:
        assert first["removed"] == []
        assert (final / "export_manifest.json").is_file()
        assert (final / "20260713-123456-process-kill.3mf").is_file()
    else:
        assert len(first["removed"]) == 1
        assert not final.exists()
    assert second["removed"] == []
    assert not any(path.name.startswith(".export-stage-") for path in export_root.iterdir())
    assert user_file.read_text(encoding="utf-8") == "keep"
