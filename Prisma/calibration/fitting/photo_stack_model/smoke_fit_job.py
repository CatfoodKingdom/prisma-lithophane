"""Run the photo stack fit job from a DataStore root.

This is a developer smoke harness, not a user-facing webapp command.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

CAL_DIR = Path(__file__).resolve().parents[2]
PRISMA_DIR = CAL_DIR.parent
for path in (CAL_DIR, PRISMA_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from data_access import DataStore
from fitting.photo_stack_model.fit_job import run_photo_stack_fit_job


def main() -> None:
    parser = argparse.ArgumentParser(description="Smoke-run the photo stack fit job")
    parser.add_argument("--data-root", required=True, help="Calibration DataStore root to read/write")
    parser.add_argument("--run-id", default=None, help="Optional candidate run id")
    parser.add_argument("--use-fit-exclusions", action="store_true", help="Honor sample/swatch fit exclusions")
    args = parser.parse_args()

    store = DataStore(Path(args.data_root))

    def progress(**event):
        print(json.dumps({"progress": event}, sort_keys=True))

    result = run_photo_stack_fit_job(
        store=store,
        run_id=args.run_id,
        use_fit_exclusions=args.use_fit_exclusions,
        created_by="smoke_fit_job",
        progress_cb=progress,
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
