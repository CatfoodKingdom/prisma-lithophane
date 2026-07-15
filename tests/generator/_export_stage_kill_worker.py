"""Create an export publication state and terminate without Python cleanup."""
from __future__ import annotations

import os
from pathlib import Path
import sys


KILL_EXIT_CODE = 92
EXPORT_ID = "20260713-123456-process-kill"
STAGE_NAME = f".export-stage-{EXPORT_ID}-deadbeef"


def main() -> None:
    export_root = Path(sys.argv[1])
    boundary = sys.argv[2]
    export_root.mkdir(parents=True, exist_ok=True)
    stage = export_root / STAGE_NAME
    final = export_root / EXPORT_ID
    stage.mkdir()
    if boundary == "after_stage_creation":
        os._exit(KILL_EXIT_CODE)
    (stage / "partial.mesh").write_bytes(b"partial")
    if boundary == "during_partial_staging":
        os._exit(KILL_EXIT_CODE)
    (stage / "partial.mesh").unlink()
    (stage / "export_manifest.json").write_text('{"schema":"test"}\n', encoding="utf-8")
    (stage / f"{EXPORT_ID}.3mf").write_bytes(b"PK\x03\x04complete")
    if boundary == "before_final_rename":
        os._exit(KILL_EXIT_CODE)
    stage.rename(final)
    if boundary == "after_final_rename":
        os._exit(KILL_EXIT_CODE)
    raise ValueError(f"unknown boundary: {boundary}")


if __name__ == "__main__":
    main()
