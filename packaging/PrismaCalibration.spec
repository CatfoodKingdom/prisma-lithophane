# PyInstaller one-folder build for the local Prisma Calibration app.

from pathlib import Path
import sys

from PyInstaller.building.datastruct import Tree
from PyInstaller.utils.hooks import collect_submodules


ROOT = Path(SPECPATH).resolve().parent
PRISMA = ROOT / "Prisma"
CALIBRATION = PRISMA / "calibration"
V63_FIT_ENGINE = CALIBRATION / "fitting" / "photo_stack_model" / "v63_fit_engine"
USE_UPX = sys.platform == "win32"

for path in (ROOT, PRISMA, CALIBRATION):
    text = str(path)
    if text not in sys.path:
        sys.path.insert(0, text)

hiddenimports = [
    "Prisma.calibration.server",
    *collect_submodules("fitting"),
    *collect_submodules("lib"),
    *collect_submodules("processing"),
    *collect_submodules("strips"),
]
v63_fit_engine_tree = Tree(
    str(V63_FIT_ENGINE),
    prefix="fitting/photo_stack_model/v63_fit_engine",
    excludes=["__pycache__", "*.pyc"],
)

analysis = Analysis(
    [str(PRISMA / "calibration_launcher.py")],
    pathex=[str(ROOT), str(PRISMA), str(CALIBRATION)],
    binaries=[],
    datas=[
        (str(CALIBRATION / "app"), "Prisma/calibration/app"),
        (str(CALIBRATION / "blank_calibration_schema.sql"), "Prisma/calibration"),
    ],
    hiddenimports=hiddenimports,
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
        "matplotlib",
        "playwright",
        "plotly",
        "pytest",
        "scipy_stubs",
        "setuptools",
        "shapely",
        "tkinter",
        "vtk",
    ],
    noarchive=False,
    optimize=0,
)

# The official headless wheel still bundles its optional FFmpeg video-I/O
# plugin. Prisma reads still images only. Keep this narrowly asserted so an
# upstream wheel-layout change fails the release build instead of silently
# restoring the unused payload or removing an unexpected binary.
opencv_videoio_plugins = [
    entry
    for entry in analysis.binaries
    if Path(entry[0]).name.lower().startswith("opencv_videoio_ffmpeg")
]
if sys.platform == "win32":
    if len(opencv_videoio_plugins) != 1:
        raise RuntimeError(
            "Expected exactly one optional OpenCV FFmpeg video-I/O plugin, found "
            f"{len(opencv_videoio_plugins)}"
        )
    analysis.binaries = [entry for entry in analysis.binaries if entry not in opencv_videoio_plugins]
elif opencv_videoio_plugins:
    raise RuntimeError("Unexpected Windows OpenCV FFmpeg plugin on a non-Windows build")

python_archive = PYZ(analysis.pure)

executable = EXE(
    python_archive,
    analysis.scripts,
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

distribution = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    v63_fit_engine_tree,
    strip=False,
    upx=USE_UPX,
    upx_exclude=[],
    name="Prisma Calibration",
)
