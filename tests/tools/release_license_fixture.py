from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts.collect_third_party_licenses import BUNDLE_FORMAT, BUNDLE_SCHEMA_VERSION


def _record(root: Path, relative: str) -> dict:
    path = root / relative
    return {
        "path": relative,
        "byte_size": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def make_license_bundle(root: Path) -> Path:
    if root.exists():
        return root
    (root / "CPython").mkdir(parents=True)
    (root / "packages" / "example-1.0").mkdir(parents=True)
    (root / "CPython" / "LICENSE.txt").write_text("Python test license\n", encoding="utf-8")
    (root / "packages" / "example-1.0" / "LICENSE.txt").write_text(
        "Example dependency test license\n", encoding="utf-8"
    )
    (root / "README.txt").write_text("Test-only collected license fixture.\n", encoding="utf-8")
    records = [
        _record(root, "CPython/LICENSE.txt"),
        _record(root, "packages/example-1.0/LICENSE.txt"),
        _record(root, "README.txt"),
    ]
    package_record = records[1]
    index = {
        "format": BUNDLE_FORMAT,
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "created_at": "2026-07-14T00:00:00+00:00",
        "python_implementation": "cpython",
        "python_version": "3.12.0",
        "platform": "test",
        "distribution_count": 1,
        "distributions": [
            {
                "name": "example",
                "version": "1.0",
                "license_expression": "MIT",
                "license_files": [package_record],
            }
        ],
        "files": records,
    }
    (root / "INDEX.json").write_text(
        json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return root
