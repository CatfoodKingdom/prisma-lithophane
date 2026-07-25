from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = (ROOT / "packaging" / "Prisma.spec").read_text(encoding="utf-8")
PROJECT = (ROOT / "pyproject.toml").read_text(encoding="utf-8")


def test_generator_runtime_group_excludes_development_families() -> None:
    group = PROJECT.split("generator-runtime = [", 1)[1].split("]", 1)[0]
    for required in ("fastapi", "opencv-python-headless", "pi-heif", "shapely", "trimesh", "uvicorn"):
        assert f'"{required}' in group
    for development_only in ("build123d", "matplotlib", "playwright", "pytest", "rawpy"):
        assert f'"{development_only}' not in group


def test_generator_spec_excludes_other_app_and_development_families() -> None:
    for excluded in (
        '"Prisma.calibration"',
        '"build123d"',
        '"matplotlib"',
        '"playwright"',
        '"pytest"',
        '"rawpy"',
        '"setuptools"',
        '"vtk"',
    ):
        assert excluded in SPEC


def test_generator_spec_removes_only_asserted_opencv_video_plugin() -> None:
    assert 'startswith("opencv_videoio_ffmpeg")' in SPEC
    assert 'if sys.platform == "win32":' in SPEC
    assert "if len(opencv_videoio_plugins) != 1:" in SPEC
    assert "analysis.binaries = [entry for entry in analysis.binaries" in SPEC
    assert 'elif opencv_videoio_plugins:' in SPEC
    assert 'USE_UPX = sys.platform == "win32"' in SPEC
    assert "upx=USE_UPX" in SPEC


def test_generator_spec_collects_heif_decoder_and_native_libraries() -> None:
    assert 'collect_dynamic_libs("pi_heif")' in SPEC
    assert 'collect_submodules("pi_heif")' in SPEC
    assert "binaries=pi_heif_binaries" in SPEC
    assert "*pi_heif_hiddenimports" in SPEC
    assert "pi_heif_datas" not in SPEC


def test_generator_frontend_has_no_obsolete_bundled_printer_profile() -> None:
    assert not (ROOT / "Prisma" / "generator" / "app" / "printers.json").exists()
