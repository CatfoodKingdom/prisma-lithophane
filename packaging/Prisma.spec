# PyInstaller one-folder build for the local Prisma generator.

from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules


ROOT = Path(SPECPATH).resolve().parent
PRISMA = ROOT / "Prisma"
GENERATOR = PRISMA / "generator"
USE_UPX = sys.platform == "win32"

for path in (ROOT, PRISMA, GENERATOR):
    text = str(path)
    if text not in sys.path:
        sys.path.insert(0, text)

pi_heif_binaries = collect_dynamic_libs("pi_heif")
pi_heif_hiddenimports = collect_submodules("pi_heif")

hiddenimports = [
    "Prisma.generator.server",
    *pi_heif_hiddenimports,
    *collect_submodules("lib"),
    *collect_submodules("preprocessing.operators"),
]

analysis = Analysis(
    [str(PRISMA / "launcher.py")],
    pathex=[str(ROOT), str(PRISMA), str(GENERATOR)],
    binaries=pi_heif_binaries,
    datas=[
        (str(GENERATOR / "app"), "Prisma/generator/app"),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "Prisma.calibration",
        "build123d",
        "cadquery",
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
    name="Prisma",
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
    strip=False,
    upx=USE_UPX,
    upx_exclude=[],
    name="Prisma",
)
