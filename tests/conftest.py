"""Keep the two legacy bare-import web apps isolated during one pytest run.

Calibration and Generator both expose a top-level ``server`` module because
their application directories are intentionally placed on ``sys.path``.  A
combined pytest invocation must activate the module set that belongs to the
directory currently being collected or executed; otherwise whichever app was
imported first silently services the other app's tests.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType


_ROOT = Path(__file__).resolve().parents[1]
_PRISMA = _ROOT / "Prisma"
_APP_DIRS = {
    "calibration": _PRISMA / "calibration",
    "generator": _PRISMA / "generator",
}


def _module_path(module: ModuleType) -> Path | None:
    filename = getattr(module, "__file__", None)
    if not filename:
        return None
    try:
        return Path(filename).resolve()
    except OSError:
        return None


def _purge_other_app_modules(active: str) -> None:
    other = _APP_DIRS["generator" if active == "calibration" else "calibration"]
    for name, module in list(sys.modules.items()):
        if module is None:
            continue
        path = _module_path(module)
        if path is None:
            continue
        try:
            path.relative_to(other)
        except ValueError:
            continue
        sys.modules.pop(name, None)


def _activate_app(app: str) -> None:
    app_dir = _APP_DIRS[app]
    _purge_other_app_modules(app)
    # Remove both application directories before putting the active one first.
    # The per-app conftests historically used ``insert(0)`` without removing
    # the other path, so a combined run could resolve the other app's bare
    # ``server`` module during collection.
    for path in _APP_DIRS.values():
        text = str(path)
        while text in sys.path:
            sys.path.remove(text)
    for path in (app_dir, _PRISMA, _ROOT):
        text = str(path)
        if text in sys.path:
            sys.path.remove(text)
        sys.path.insert(0, text)
    importlib.invalidate_caches()


def _app_for_path(path: Path) -> str | None:
    try:
        relative = path.resolve().relative_to(_ROOT / "tests")
    except ValueError:
        return None
    first = relative.parts[0] if relative.parts else ""
    return first if first in _APP_DIRS else None


def pytest_collect_directory(path: Path, parent):  # type: ignore[no-untyped-def]
    app = _app_for_path(path)
    if app is not None and path.resolve() == (_ROOT / "tests" / app).resolve():
        _activate_app(app)
    return None


def pytest_collect_file(file_path: Path, parent):  # type: ignore[no-untyped-def]
    """Activate the owning app immediately before pytest imports a test file."""
    app = _app_for_path(file_path)
    if app is not None:
        _activate_app(app)
    return None


def pytest_runtest_setup(item):  # type: ignore[no-untyped-def]
    app = _app_for_path(Path(str(item.path)))
    if app is not None:
        _activate_app(app)
