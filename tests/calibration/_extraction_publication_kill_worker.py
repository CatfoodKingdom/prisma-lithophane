"""Subprocess worker used to terminate extraction publication at exact boundaries."""
from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import sys


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CALIBRATION_ROOT = _PROJECT_ROOT / "Prisma" / "calibration"
sys.path.insert(0, str(_CALIBRATION_ROOT))

from processing.extraction_publication import publish_extraction_update  # noqa: E402


KILL_EXIT_CODE = 91


class PersistentSQLiteStore:
    backend = "sqlite"

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.database = self.root / "state.sqlite"

    def get_extraction_result(self, sample_id: str):
        with sqlite3.connect(self.database) as connection:
            row = connection.execute(
                "SELECT current_id FROM extraction_state WHERE sample_id = ?",
                (sample_id,),
            ).fetchone()
        if row is None or not row[0]:
            return None
        return {"sample_id": sample_id, "extraction_result_id": str(row[0])}

    def replace_current_id(self, sample_id: str, replacement_id: str) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "UPDATE extraction_state SET current_id = ? WHERE sample_id = ?",
                (replacement_id, sample_id),
            )


def _matches_boundary(specification: str, event: str, count: int) -> bool:
    requested, separator, requested_count = specification.partition(":")
    if requested != event:
        return False
    return not separator or count == int(requested_count)


def main() -> None:
    root = Path(sys.argv[1])
    kill_boundary = sys.argv[2]
    store = PersistentSQLiteStore(root)
    event_counts: dict[str, int] = {}

    def kill_now() -> None:
        os._exit(KILL_EXIT_CODE)

    def semantic_commit() -> None:
        store.replace_current_id("exp-001", "ext_new")
        if kill_boundary == "after_sqlite_commit_before_phase":
            kill_now()

    def fault_hook(event: str, _record) -> None:
        event_counts[event] = event_counts.get(event, 0) + 1
        if _matches_boundary(kill_boundary, event, event_counts[event]):
            kill_now()

    publish_extraction_update(
        store,
        sample_id="exp-001",
        prior_extraction_result_id="ext_old",
        replacement_extraction_result_id="ext_new",
        semantic_change=True,
        origin="automatic",
        visual_paths={
            "source": root / "candidate" / "source.jpg",
            "strip": root / "candidate" / "strip.jpg",
        },
        semantic_commit=semantic_commit,
        fault_hook=fault_hook,
    )


if __name__ == "__main__":
    main()
