from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tools.migration_preflight.generator_contract_smoke import (
    build_filament_registry_from_sqlite,
    export_runtime_filament_registry,
)


_REPO_ROOT = Path(__file__).resolve().parents[2]
_GENERATOR_DIR = _REPO_ROOT / "Prisma" / "generator"


def _seed_filaments(sqlite_path: Path) -> None:
    conn = sqlite3.connect(sqlite_path)
    try:
        conn.execute(
            """
            CREATE TABLE filaments (
                filament_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                manufacturer TEXT,
                material TEXT,
                hex_color TEXT,
                white_cap_eligible INTEGER NOT NULL,
                exclude_from_model INTEGER NOT NULL,
                notes TEXT,
                created_at TEXT,
                updated_at TEXT
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO filaments (
                filament_id,
                name,
                manufacturer,
                material,
                hex_color,
                white_cap_eligible,
                exclude_from_model,
                notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "bambu-basic-cyan",
                    "Bambu Basic Cyan",
                    "Bambu",
                    "PLA",
                    "#0086D6",
                    0,
                    0,
                    None,
                ),
                (
                    "panchroma-matte-cotton-white",
                    "Panchroma Matte Cotton White",
                    "Panchroma",
                    "PLA",
                    "#F4EFEB",
                    1,
                    0,
                    "white cap candidate",
                ),
                (
                    "panchroma-translucent-natural",
                    "Panchroma Translucent Natural",
                    "Panchroma",
                    "PLA",
                    "#E8E6D0",
                    0,
                    1,
                    "excluded from fits",
                ),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def test_build_filament_registry_from_sqlite_preserves_generator_policy_fields(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "migration.sqlite"
    _seed_filaments(sqlite_path)

    registry = build_filament_registry_from_sqlite(sqlite_path)

    assert sorted(registry) == [
        "bambu-basic-cyan",
        "panchroma-matte-cotton-white",
        "panchroma-translucent-natural",
    ]
    assert registry["bambu-basic-cyan"]["display_name"] == "Bambu Basic Cyan"
    assert registry["bambu-basic-cyan"]["hex"] == "#0086D6"
    assert registry["panchroma-matte-cotton-white"]["white_cap_eligible"] is True
    assert registry["panchroma-translucent-natural"]["exclude_from_model"] is True


def test_export_runtime_filament_registry_refuses_non_disposable_runtime(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "migration.sqlite"
    _seed_filaments(sqlite_path)

    with pytest.raises(ValueError, match="outside .codex-work"):
        export_runtime_filament_registry(sqlite_path, tmp_path / "runtime_store")


def test_export_runtime_filament_registry_allows_explicit_production_runtime(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "migration.sqlite"
    _seed_filaments(sqlite_path)
    runtime_root = tmp_path / "runtime_store"

    path = export_runtime_filament_registry(
        sqlite_path,
        runtime_root,
        allow_production_runtime_root=True,
    )

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert path == runtime_root / "filaments" / "registry.json"
    assert sorted(saved) == [
        "bambu-basic-cyan",
        "panchroma-matte-cotton-white",
        "panchroma-translucent-natural",
    ]


def test_export_runtime_filament_registry_writes_legacy_shape(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "migration.sqlite"
    _seed_filaments(sqlite_path)
    runtime_root = tmp_path / ".codex-work" / "runtime_store"

    path = export_runtime_filament_registry(sqlite_path, runtime_root)

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert path == runtime_root / "filaments" / "registry.json"
    assert saved["panchroma-translucent-natural"]["exclude_from_model"] is True


def test_generator_rooted_modules_honor_configured_data_root(tmp_path: Path) -> None:
    env = os.environ.copy()
    library_root = tmp_path / "library"
    workspace_root = tmp_path / "workspace"
    image_root = tmp_path / "Images"
    export_root = tmp_path / "Exports"
    env["PRISMA_MODEL_LIBRARY_ROOT"] = str(library_root)
    env["PRISMA_USER_DATA_ROOT"] = str(workspace_root)
    env["PRISMA_IMAGE_ROOT"] = str(image_root)
    env["PRISMA_EXPORT_ROOT"] = str(export_root)
    code = """
import json
from pathlib import Path
import data_paths
import filament_order
import model
print(json.dumps({
    "data_dir": str(data_paths.DATA_DIR),
    "workspace_dir": str(data_paths.GENERATOR_DATA_DIR),
    "image_dir": str(data_paths.UPLOAD_DIR),
    "export_dir": str(data_paths.OUTPUT_DIR),
    "profiles_dir": str(model.PROFILES_DIR),
    "lut_path": str(model.DEFAULT_MODEL_DOMAIN_INGRESS_LUT_PATH),
    "registry_path": str(filament_order._REGISTRY_PATH),
}, sort_keys=True))
"""

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(_GENERATOR_DIR),
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    payload = json.loads(result.stdout)

    assert Path(payload["data_dir"]).resolve() == library_root.resolve()
    assert Path(payload["workspace_dir"]).resolve() == workspace_root.resolve()
    assert Path(payload["image_dir"]).resolve() == image_root.resolve()
    assert Path(payload["export_dir"]).resolve() == export_root.resolve()
    assert Path(payload["profiles_dir"]).resolve() == (library_root / "filaments" / "profiles").resolve()
    assert Path(payload["lut_path"]).resolve() == (library_root / "camera_transform").resolve()
    assert Path(payload["registry_path"]).resolve() == (library_root / "filaments" / "registry.json").resolve()
