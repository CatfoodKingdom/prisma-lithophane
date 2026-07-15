# PyInstaller one-folder build for the combined Prisma Generator + Calibration Suite.

from pathlib import Path
import sys

from PyInstaller.building.datastruct import Tree
from PyInstaller.utils.hooks import collect_submodules


ROOT = Path(SPECPATH).resolve().parent
PRISMA = ROOT / "Prisma"
GENERATOR = PRISMA / "generator"
CALIBRATION = PRISMA / "calibration"
V63_FIT_ENGINE = CALIBRATION / "fitting" / "photo_stack_model" / "v63_fit_engine"
USE_UPX = sys.platform == "win32"

for path in (ROOT, PRISMA, GENERATOR, CALIBRATION):
    text = str(path)
    if text not in sys.path:
        sys.path.insert(0, text)


def remove_asserted_opencv_video_plugin(analysis, *, application_name):
    """Remove the one optional FFmpeg plugin from a still-image-only app."""

    plugins = [
        entry
        for entry in analysis.binaries
        if Path(entry[0]).name.lower().startswith("opencv_videoio_ffmpeg")
    ]
    if sys.platform == "win32":
        if len(plugins) != 1:
            raise RuntimeError(
                f"{application_name}: expected exactly one optional OpenCV FFmpeg "
                f"video-I/O plugin, found {len(plugins)}"
            )
        analysis.binaries = [entry for entry in analysis.binaries if entry not in plugins]
    elif plugins:
        raise RuntimeError(
            f"{application_name}: unexpected Windows OpenCV FFmpeg plugin on a non-Windows build"
        )


generator_analysis = Analysis(
    [str(PRISMA / "launcher.py")],
    pathex=[str(ROOT), str(PRISMA), str(GENERATOR)],
    binaries=[],
    datas=[
        (str(GENERATOR / "app"), "Prisma/generator/app"),
    ],
    hiddenimports=[
        "Prisma.generator.server",
        *collect_submodules("lib"),
        *collect_submodules("preprocessing.operators"),
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "Prisma.calibration",
        "build123d",
        "cadquery",
        "exifread",
        "IPython",
        "ipykernel",
        "ipympl",
        "ipywidgets",
        "jupyterlab",
        "matplotlib",
        "playwright",
        "plotly",
        "pytest",
        "rawpy",
        "scipy_stubs",
        "setuptools",
        "tkinter",
        "vtk",
    ],
    noarchive=False,
    optimize=0,
)
remove_asserted_opencv_video_plugin(generator_analysis, application_name="Generator")

calibration_analysis = Analysis(
    [str(PRISMA / "calibration_launcher.py")],
    pathex=[str(ROOT), str(PRISMA), str(CALIBRATION)],
    binaries=[],
    datas=[
        (str(CALIBRATION / "app"), "Prisma/calibration/app"),
        (str(CALIBRATION / "blank_calibration_schema.sql"), "Prisma/calibration"),
    ],
    hiddenimports=[
        "Prisma.calibration.server",
        *collect_submodules("fitting"),
        *collect_submodules("lib"),
        *collect_submodules("processing"),
        *collect_submodules("strips"),
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "IPython",
        "OCP",
        "Prisma.generator",
        "build123d",
        "cadquery",
        "ipykernel",
        "ipympl",
        "ipywidgets",
        "jupyterlab",
        "lib3mf",
        "mapbox_earcut",
        "matplotlib",
        "playwright",
        "plotly",
        "pytest",
        "scipy_stubs",
        "setuptools",
        "shapely",
        "tkinter",
        "trimesh",
        "vtk",
        "xxhash",
    ],
    noarchive=False,
    optimize=0,
)
remove_asserted_opencv_video_plugin(calibration_analysis, application_name="Calibration")

generator_archive = PYZ(generator_analysis.pure)
calibration_archive = PYZ(calibration_analysis.pure)

generator_executable = EXE(
    generator_archive,
    generator_analysis.scripts,
    [],
    exclude_binaries=True,
    name="Prisma Generator",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=USE_UPX,
    console=True,
    disable_windowed_traceback=False,
)
calibration_executable = EXE(
    calibration_archive,
    calibration_analysis.scripts,
    [],
    exclude_binaries=True,
    name="Prisma Calibration",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=USE_UPX,
    console=True,
    disable_windowed_traceback=False,
)

v63_fit_engine_tree = Tree(
    str(V63_FIT_ENGINE),
    prefix="fitting/photo_stack_model/v63_fit_engine",
    excludes=["__pycache__", "*.pyc"],
)

# One COLLECT gives both independent launchers one shared _internal directory.
# Do not use PyInstaller MERGE: it gives even onedir applications onefile
# extraction semantics and makes one executable depend on the other's archive.
distribution = COLLECT(
    generator_executable,
    calibration_executable,
    generator_analysis.binaries,
    generator_analysis.datas,
    calibration_analysis.binaries,
    calibration_analysis.datas,
    v63_fit_engine_tree,
    strip=False,
    upx=USE_UPX,
    upx_exclude=[],
    name="Prisma Suite",
)
