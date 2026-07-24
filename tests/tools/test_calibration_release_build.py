from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = (ROOT / "packaging" / "PrismaCalibration.spec").read_text(encoding="utf-8")
PROJECT = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
EXTRACTION = (ROOT / "Prisma" / "calibration" / "processing" / "extraction.py").read_text(encoding="utf-8")
APP = ROOT / "Prisma" / "calibration" / "app"


def test_calibration_runtime_group_contains_its_non_generator_dependencies() -> None:
    group = PROJECT.split("calibration-runtime = [", 1)[1].split("]", 1)[0]
    for package in ("exifread", "packaging", "rawpy"):
        assert f'"{package}' in group
    for development_only in ("build123d", "matplotlib", "shapely"):
        assert f'"{development_only}' not in group


def test_calibration_spec_bundles_first_run_and_frontend_assets() -> None:
    assert 'name="Prisma Calibration"' in SPEC
    assert 'CALIBRATION / "app"' in SPEC
    assert 'CALIBRATION / "blank_calibration_schema.sql"' in SPEC
    assert '"Prisma.generator"' in SPEC


def test_calibration_frontend_asset_tree_contains_modules_and_no_retired_monoliths() -> None:
    assert (APP / "bootstrap.js").is_file()
    assert (APP / "api" / "index.js").is_file()
    assert (APP / "core" / "application-context.js").is_file()
    assert (APP / "features" / "application.js").is_file()
    assert (APP / "styles" / "tokens.css").is_file()
    for retired in ("app.js", "api.js", "data.js"):
        assert not (APP / retired).exists()


def test_calibration_spec_preserves_path_loaded_fitter_without_cad_kernel() -> None:
    assert 'Tree(' in SPEC
    assert 'prefix="fitting/photo_stack_model/v63_fit_engine"' in SPEC
    assert 'excludes=["__pycache__", "*.pyc"]' in SPEC
    assert "collect_dynamic_libs" not in SPEC
    for excluded in (
        '"build123d"',
        '"cadquery"',
        '"OCP"',
        '"vtk"',
        '"lib3mf"',
        '"IPython"',
        '"matplotlib"',
        '"setuptools"',
        '"shapely"',
    ):
        assert excluded in SPEC


def test_calibration_spec_removes_only_asserted_opencv_video_plugin() -> None:
    assert 'startswith("opencv_videoio_ffmpeg")' in SPEC
    assert 'if sys.platform == "win32":' in SPEC
    assert "if len(opencv_videoio_plugins) != 1:" in SPEC
    assert "analysis.binaries = [entry for entry in analysis.binaries" in SPEC
    assert 'elif opencv_videoio_plugins:' in SPEC
    assert 'USE_UPX = sys.platform == "win32"' in SPEC
    assert "upx=USE_UPX" in SPEC


def test_matplotlib_is_lazy_and_development_only() -> None:
    tree = ast.parse(EXTRACTION)
    top_level_imports = [
        node
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    assert not any(
        (
            isinstance(node, ast.Import)
            and any(alias.name.startswith("matplotlib") for alias in node.names)
        )
        or (
            isinstance(node, ast.ImportFrom)
            and str(node.module or "").startswith("matplotlib")
        )
        for node in top_level_imports
    )
    assert "import matplotlib.pyplot as plt" in EXTRACTION
