"""Tests for the data_paths module (Stage 9a managed filesystem locations)."""
import importlib
import json

import pytest


def test_managed_dirs_are_under_resolved_data_root_not_output():
    dp = importlib.import_module("data_paths")  # generator dir is on sys.path via conftest
    assert dp.CACHE_DIR.name == "cache"
    assert dp.CACHE_DIR.parent.name == "Workspace"
    for child in (dp.RUN_CACHE_DIR, dp.LUT_CACHE_DIR, dp.AUTO_RUNS_DIR):
        assert dp.CACHE_DIR in child.parents, f"{child} must live under CACHE_DIR"
    assert dp.CACHE_DIR not in dp.OUTPUT_DIR.parents
    assert dp.OUTPUT_DIR not in dp.CACHE_DIR.parents
    assert dp.CACHE_DIR not in dp.SAVED_RUNS_DIR.parents


def test_env_var_overrides_data_root(tmp_path, monkeypatch):
    monkeypatch.setenv("PRISMA_MODEL_LIBRARY_ROOT", str(tmp_path))
    import data_paths
    dp = importlib.reload(data_paths)
    try:
        assert dp.DATA_DIR == tmp_path.resolve()
    finally:
        monkeypatch.undo()
        importlib.reload(data_paths)


def test_ensure_dirs_creates_saved_runs(tmp_path, monkeypatch):
    import importlib, data_paths
    monkeypatch.setenv("PRISMA_USER_DATA_ROOT", str(tmp_path / "Workspace"))
    monkeypatch.setenv("PRISMA_IMAGE_ROOT", str(tmp_path / "Images"))
    monkeypatch.setenv("PRISMA_EXPORT_ROOT", str(tmp_path / "Exports"))
    dp = importlib.reload(data_paths)
    dp.ensure_dirs()
    assert dp.SAVED_RUNS_DIR == (tmp_path / "Workspace" / "saved_runs").resolve()
    assert dp.SAVED_RUNS_DIR.is_dir()
    assert dp.UPLOAD_DIR == (tmp_path / "Images").resolve()
    assert dp.OUTPUT_DIR == (tmp_path / "Exports").resolve()
    monkeypatch.undo()
    importlib.reload(dp)  # restore default for other tests


def test_only_explicit_published_library_env_is_honored(tmp_path):
    import data_paths

    model_library = tmp_path / "model-library"
    legacy_generator = tmp_path / "legacy-generator"
    calibration = tmp_path / "calibration"

    assert data_paths.resolve_data_dir(
        environ={
            "PRISMA_MODEL_LIBRARY_ROOT": str(model_library),
            "PRISMA_GENERATOR_DATA_ROOT": str(legacy_generator),
            "PRISMA_CALIBRATION_DATA_ROOT": str(calibration),
        }
    ) == model_library.resolve()
    with pytest.raises(RuntimeError, match="no selected published model library"):
        data_paths.resolve_data_dir(
            environ={
                "PRISMA_GENERATOR_DATA_ROOT": str(legacy_generator),
                "PRISMA_CALIBRATION_DATA_ROOT": str(calibration),
            }
        )


def test_distribution_roots_separate_library_user_state_and_exports(tmp_path):
    import data_paths

    library = tmp_path / "library"
    user = tmp_path / "user"
    exports = tmp_path / "exports"
    env = {
        "PRISMA_MODEL_LIBRARY_ROOT": str(library),
        "PRISMA_USER_DATA_ROOT": str(user),
        "PRISMA_IMAGE_ROOT": str(tmp_path / "images"),
        "PRISMA_EXPORT_ROOT": str(exports),
    }
    resolved_library = data_paths.resolve_data_dir(environ=env)
    resolved_user = data_paths.resolve_user_data_dir(
        model_library_dir=resolved_library,
        environ=env,
    )

    assert resolved_library == library.resolve()
    assert resolved_user == user.resolve()
    assert data_paths.resolve_upload_dir(
        prisma_dir=tmp_path / "Prisma",
        user_data_dir=resolved_user,
        environ=env,
    ) == (tmp_path / "images").resolve()
    assert data_paths.resolve_config_dir(
        generator_dir=tmp_path / "Prisma" / "generator",
        user_data_dir=resolved_user,
        environ=env,
    ) == user.resolve() / "config"
    assert data_paths.resolve_output_dir(
        prisma_dir=tmp_path / "Prisma",
        environ=env,
    ) == exports.resolve()


def test_distribution_resolvers_refuse_implicit_legacy_defaults(tmp_path):
    import data_paths

    prisma_dir = tmp_path / "Prisma"
    generator_dir = prisma_dir / "generator"
    library = tmp_path / "library"
    with pytest.raises(RuntimeError, match="no Workspace path"):
        data_paths.resolve_user_data_dir(model_library_dir=library, environ={})
    with pytest.raises(RuntimeError, match="no Images path"):
        data_paths.resolve_upload_dir(prisma_dir=prisma_dir, user_data_dir=tmp_path, environ={})
    with pytest.raises(RuntimeError, match="no Workspace path"):
        data_paths.resolve_config_dir(generator_dir=generator_dir, user_data_dir=tmp_path, environ={})
    with pytest.raises(RuntimeError, match="no Exports path"):
        data_paths.resolve_output_dir(prisma_dir=prisma_dir, environ={})


def test_server_mutable_paths_use_shared_distribution_contract():
    import data_paths
    from Prisma.generator import server

    assert server._GENERATOR_DATA_DIR == data_paths.GENERATOR_DATA_DIR
    assert server._IMAGES_DIR == data_paths.UPLOAD_DIR
    assert server._OUTPUT_DIR == data_paths.OUTPUT_DIR
    assert server._PRINTERS_PATH == data_paths.CONFIG_DIR / "printers.json"
    assert server._MODULES_PATH == data_paths.CONFIG_DIR / "modules.json"


def test_generator_health_endpoint_identifies_ready_app():
    from Prisma.generator import server

    assert server.system_health() == {
        "ok": True,
        "app": "prisma-generator",
        "version": "0.1.0",
        "mode": "normal",
        "model_library_available": True,
        "active_library_id": None,
        "model_library_error": None,
    }


def test_recovery_mode_serves_health_but_blocks_model_work(monkeypatch):
    from Prisma.generator import server

    monkeypatch.setattr(server, "_MODEL_LIBRARY_AVAILABLE", False)
    monkeypatch.setattr(server, "_ACTIVE_MODEL_LIBRARY_ID", None)
    monkeypatch.setattr(server, "_MODEL_LIBRARY_ERROR", "active library is corrupt")

    health = server.system_health()
    assert health["mode"] == "library_recovery"
    assert health["model_library_available"] is False
    assert health["model_library_error"] == "active library is corrupt"
    assert server._load_registry() == {}
    assert server._load_corrections() is None
    with pytest.raises(server.HTTPException) as excinfo:
        server._build_solve_config(dict(server._DEFAULT_CONFIG))
    assert excinfo.value.status_code == 409


def test_printer_defaults_bootstrap_into_missing_distribution_config_dir(tmp_path, monkeypatch):
    from Prisma.generator import server

    printers_path = tmp_path / "new-user-data" / "config" / "printers.json"
    monkeypatch.setattr(server, "_PRINTERS_PATH", printers_path)

    printers = server._load_printers()

    assert printers["printers"]
    assert printers_path.is_file()
    persisted = json.loads(printers_path.read_text(encoding="utf-8"))
    for printer in persisted["printers"]:
        for nozzle in printer["nozzle_profiles"]:
            assert nozzle["min_line_length_multiplier"] == 2
            assert "min_line_width" not in nozzle
            assert "min_line_length" not in nozzle

    before = printers_path.read_bytes()
    assert server._load_printers() == printers
    assert printers_path.read_bytes() == before
