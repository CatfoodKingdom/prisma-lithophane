from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = (ROOT / "packaging" / "PrismaSuite.spec").read_text(encoding="utf-8")


def test_suite_has_two_independent_entry_points_and_one_collect() -> None:
    assert '[str(PRISMA / "launcher.py")]' in SPEC
    assert '[str(PRISMA / "calibration_launcher.py")]' in SPEC
    assert 'name="Prisma Generator"' in SPEC
    assert 'name="Prisma Calibration"' in SPEC
    assert SPEC.count("COLLECT(") == 1
    assert "generator_executable," in SPEC
    assert "calibration_executable," in SPEC
    assert 'name="Prisma Suite"' in SPEC
    assert "MERGE(" not in SPEC


def test_suite_collects_both_frontends_schema_and_path_loaded_fitter() -> None:
    assert '(str(GENERATOR / "app"), "Prisma/generator/app")' in SPEC
    assert '(str(CALIBRATION / "app"), "Prisma/calibration/app")' in SPEC
    assert '"blank_calibration_schema.sql"' in SPEC
    assert 'prefix="fitting/photo_stack_model/v63_fit_engine"' in SPEC
    assert 'excludes=["__pycache__", "*.pyc"]' in SPEC


def test_suite_generator_collects_heif_decoder_without_adding_it_to_calibration() -> None:
    generator = SPEC.split("generator_analysis = Analysis(", 1)[1].split(")\nremove_asserted", 1)[0]
    calibration = SPEC.split("calibration_analysis = Analysis(", 1)[1].split(")\nremove_asserted", 1)[0]
    assert 'collect_dynamic_libs("pi_heif")' in SPEC
    assert 'collect_submodules("pi_heif")' in SPEC
    assert "binaries=pi_heif_binaries" in generator
    assert "*pi_heif_hiddenimports" in generator
    assert "pi_heif_datas" not in generator
    assert "pi_heif" not in calibration


def test_generator_frontend_directory_contains_nested_module_and_style_assets() -> None:
    app = ROOT / "Prisma" / "generator" / "app"
    required = [
        app / "bootstrap.js",
        app / "api" / "index.js",
        app / "core" / "application-context.js",
        app / "features" / "solve" / "lightbox.js",
        app / "features" / "settings" / "layout.js",
        app / "features" / "palette" / "deck.js",
        app / "styles" / "tokens.css",
        app / "styles" / "diagnostics.css",
    ]
    assert all(path.is_file() for path in required)
    assert '(str(GENERATOR / "app"), "Prisma/generator/app")' in SPEC


def test_suite_keeps_application_analyses_separate() -> None:
    generator = SPEC.split("generator_analysis = Analysis(", 1)[1].split(")\nremove_asserted", 1)[0]
    calibration = SPEC.split("calibration_analysis = Analysis(", 1)[1].split(")\nremove_asserted", 1)[0]
    assert '"Prisma.calibration"' in generator
    assert '"rawpy"' in generator
    assert '"Prisma.generator"' in calibration
    for generator_only in ('"mapbox_earcut"', '"shapely"', '"trimesh"', '"xxhash"'):
        assert generator_only in calibration


def test_suite_removes_only_asserted_opencv_video_plugins() -> None:
    assert SPEC.count("remove_asserted_opencv_video_plugin(") == 3  # definition + two calls
    assert 'startswith("opencv_videoio_ffmpeg")' in SPEC
    assert 'if sys.platform == "win32":' in SPEC
    assert "if len(plugins) != 1:" in SPEC
    assert 'application_name="Generator"' in SPEC
    assert 'application_name="Calibration"' in SPEC
    assert 'elif plugins:' in SPEC
    assert 'USE_UPX = sys.platform == "win32"' in SPEC
    assert "upx=USE_UPX" in SPEC
