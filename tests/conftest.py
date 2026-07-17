"""Keep the two legacy bare-import web apps isolated during one pytest run.

Calibration and Generator both expose a top-level ``server`` module because
their application directories are intentionally placed on ``sys.path``.  A
combined pytest invocation must activate the module set that belongs to the
directory currently being collected or executed; otherwise whichever app was
imported first silently services the other app's tests.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from types import ModuleType


_ROOT = Path(__file__).resolve().parents[1]
_PRISMA = _ROOT / "Prisma"
_APP_DIRS = {
    "calibration": _PRISMA / "calibration",
    "generator": _PRISMA / "generator",
}
_active_app: str | None = None
_MODULE_SNAPSHOTS: dict[str, dict[str, ModuleType]] = {
    app: {} for app in _APP_DIRS
}


def _prisma_environment() -> dict[str, str]:
    return {name: value for name, value in os.environ.items() if name.startswith("PRISMA_")}


_BASE_PRISMA_ENV = _prisma_environment()
_ENV_SNAPSHOTS = {app: dict(_BASE_PRISMA_ENV) for app in _APP_DIRS}


def _restore_prisma_environment(environment: dict[str, str]) -> None:
    for name in tuple(os.environ):
        if name.startswith("PRISMA_"):
            os.environ.pop(name, None)
    os.environ.update(environment)


def _module_path(module: ModuleType) -> Path | None:
    filename = getattr(module, "__file__", None)
    if not filename:
        return None
    try:
        return Path(filename).resolve()
    except OSError:
        return None


def _module_app(module: ModuleType) -> str | None:
    path = _module_path(module)
    if path is None:
        return None
    for app, app_dir in _APP_DIRS.items():
        try:
            path.relative_to(app_dir)
        except ValueError:
            continue
        return app
    return None


def _stash_and_remove_app_modules(active_context: str | None) -> None:
    """Preserve each product's imported graph before changing bare-import roots.

    Pytest imports every test module during collection. Permanently purging the
    first product at the next collection boundary leaves those test modules
    holding orphaned globals; later function-local imports then create a second
    copy of the same product module. Saving and restoring the exact module
    objects keeps those identities coherent while still isolating colliding
    names such as ``server`` and ``models``.
    """

    current_context: dict[str, ModuleType] = {}
    for name, module in list(sys.modules.items()):
        if module is None:
            continue
        if _module_app(module) is None:
            continue
        if active_context is not None:
            current_context[name] = module
        sys.modules.pop(name, None)
    if active_context is not None:
        # A context can intentionally import modules from the other product by
        # qualified name. Keep those exact objects with the importing context;
        # grouping the snapshot by module owner would overwrite the first
        # product's collection graph with second-product imports.
        _MODULE_SNAPSHOTS[active_context] = current_context


def _activate_app(app: str) -> None:
    global _active_app
    if _active_app == app:
        return

    app_dir = _APP_DIRS[app]
    current_environment = _prisma_environment()
    configured_app = current_environment.get("PRISMA_TEST_ACTIVE_PRODUCT")
    if configured_app in _APP_DIRS and configured_app != _active_app:
        # A product conftest can be imported immediately before pytest invokes
        # the collection hook for its first file. Attribute that environment to
        # the conftest's product, not to the previously active product.
        _ENV_SNAPSHOTS[configured_app] = current_environment
    elif _active_app is not None:
        _ENV_SNAPSHOTS[_active_app] = current_environment
    _stash_and_remove_app_modules(_active_app)
    sys.modules.update(_MODULE_SNAPSHOTS[app])
    _restore_prisma_environment(_ENV_SNAPSHOTS[app])
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
    _active_app = app


def _deactivate_app() -> None:
    """Restore the ambient environment before collecting or running shared tests."""

    global _active_app
    if _active_app is None:
        return
    _ENV_SNAPSHOTS[_active_app] = _prisma_environment()
    _stash_and_remove_app_modules(_active_app)
    _restore_prisma_environment(_BASE_PRISMA_ENV)
    for path in _APP_DIRS.values():
        text = str(path)
        while text in sys.path:
            sys.path.remove(text)
    importlib.invalidate_caches()
    _active_app = None


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
    else:
        _deactivate_app()
    return None


def pytest_runtest_setup(item):  # type: ignore[no-untyped-def]
    app = _app_for_path(Path(str(item.path)))
    if app is not None:
        _activate_app(app)
    else:
        _deactivate_app()
