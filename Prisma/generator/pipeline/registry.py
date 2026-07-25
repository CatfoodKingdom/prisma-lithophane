# lithophane_generator/pipeline/registry.py
"""Module registration and discovery."""
from __future__ import annotations

import importlib
import pkgutil
import sys
from typing import Dict, Type

from .base import PreprocessingModule

_PREPROCESSORS: Dict[str, Type[PreprocessingModule]] = {}
_PREPROCESSING_DISCOVERY_COMPLETE = False

# Auto-discovery boundary for the F1 preprocessing slot (R3-C / R4-A).
# All public modules under `preprocessing/operators/` are imported in
# lex-sorted order; that import position becomes their R2-C
# `registration_order` tiebreaker. Helper modules at the `preprocessing/`
# package root (types, runner, color_convert) are NOT auto-discovered.
_PREPROCESSING_OPERATORS_PACKAGE = "preprocessing.operators"
PREPROCESSING_MODULE_IDS: set[str] = set()


def _discover_preprocessing_operators() -> None:
    """Import every public submodule of `preprocessing.operators` in lex order.

    Modules are responsible for self-registering via `@register_preprocessing`
    at import time. After this function returns, `PREPROCESSING_MODULE_IDS`
    holds every operator name the slot can resolve.

    Per R4-B, an empty operators/ package is legal — the slot becomes a no-op.
    """
    PREPROCESSING_MODULE_IDS.clear()
    try:
        package = importlib.import_module(_PREPROCESSING_OPERATORS_PACKAGE)
    except ImportError:
        return
    paths = getattr(package, "__path__", None)
    if not paths:
        PREPROCESSING_MODULE_IDS.update(_PREPROCESSORS.keys())
        return

    submodules = sorted(
        info.name
        for info in pkgutil.iter_modules(paths)
        if not info.ispkg and not info.name.startswith("_")
    )
    for sub in submodules:
        full = f"{_PREPROCESSING_OPERATORS_PACKAGE}.{sub}"
        if full in sys.modules:
            importlib.reload(sys.modules[full])
        else:
            importlib.import_module(full)
    PREPROCESSING_MODULE_IDS.update(_PREPROCESSORS.keys())


def _ensure_store_populated(
    store: Dict[str, Type],
    expected_names: set[str],
    module_names: tuple[str, ...],
) -> None:
    """(Re)import known modules when the registry has been cleared or is partial."""
    if expected_names.issubset(store.keys()):
        return

    # If the registry was explicitly cleared, reloading already-imported
    # modules is necessary so their decorators run again.  If the store is only
    # partial because one module was imported by a caller before discovery, do
    # not reload that module: doing so creates a second class object for the
    # same module and breaks identity-sensitive registration tests.
    reload_loaded_modules = not store
    for module_name in module_names:
        if expected_names.issubset(store.keys()):
            return
        module = sys.modules.get(module_name)
        if module is None:
            importlib.import_module(module_name)
        elif reload_loaded_modules:
            importlib.reload(module)
        else:
            already_registered = any(
                getattr(cls, "__module__", None) == module_name
                for cls in store.values()
            )
            if not already_registered:
                importlib.reload(module)


def _ensure_preprocessing_populated() -> None:
    """Ensure direct operator imports cannot leave the registry partial."""
    global _PREPROCESSING_DISCOVERY_COMPLETE
    if _PREPROCESSING_DISCOVERY_COMPLETE:
        return
    if not _PREPROCESSORS:
        _discover_preprocessing_operators()
        _PREPROCESSING_DISCOVERY_COMPLETE = True
        return

    package = importlib.import_module(_PREPROCESSING_OPERATORS_PACKAGE)
    paths = getattr(package, "__path__", None)
    if not paths:
        PREPROCESSING_MODULE_IDS.update(_PREPROCESSORS.keys())
        _PREPROCESSING_DISCOVERY_COMPLETE = True
        return
    registered_modules = {
        getattr(cls, "__module__", "")
        for cls in _PREPROCESSORS.values()
    }
    # Tests and embedding callers may intentionally install an isolated,
    # synthetic registry. Only complete a partial registry when its existing
    # entries are real operators from the discoverable package.
    if any(
        not module.startswith(f"{_PREPROCESSING_OPERATORS_PACKAGE}.")
        for module in registered_modules
    ):
        PREPROCESSING_MODULE_IDS.update(_PREPROCESSORS.keys())
        return
    for info in sorted(pkgutil.iter_modules(paths), key=lambda entry: entry.name):
        if info.ispkg or info.name.startswith("_"):
            continue
        full = f"{_PREPROCESSING_OPERATORS_PACKAGE}.{info.name}"
        if full in registered_modules:
            continue
        module = sys.modules.get(full)
        if module is None:
            importlib.import_module(full)
        else:
            importlib.reload(module)
    PREPROCESSING_MODULE_IDS.update(_PREPROCESSORS.keys())
    _PREPROCESSING_DISCOVERY_COMPLETE = True


def _ensure_registry_populated() -> None:
    """Ensure all built-in modules have registered themselves."""
    _ensure_preprocessing_populated()


def register_preprocessing(
    cls: Type[PreprocessingModule],
) -> Type[PreprocessingModule]:
    """Class decorator: register a preprocessing operator (F1)."""
    _PREPROCESSORS[cls.name] = cls
    PREPROCESSING_MODULE_IDS.add(cls.name)
    return cls


def get_preprocessing(name: str) -> Type[PreprocessingModule]:
    _ensure_registry_populated()
    return _PREPROCESSORS[name]


def list_preprocessings() -> list[str]:
    _ensure_registry_populated()
    return list(_PREPROCESSORS.keys())


def _clear_registry() -> None:
    """Clear all registrations. For testing only."""
    global _PREPROCESSING_DISCOVERY_COMPLETE
    _PREPROCESSORS.clear()
    PREPROCESSING_MODULE_IDS.clear()
    _PREPROCESSING_DISCOVERY_COMPLETE = False


def list_all_modules() -> list[dict]:
    """Return descriptors for all registered modules."""
    _ensure_registry_populated()
    modules = []
    for _name, cls in _PREPROCESSORS.items():
        inst = cls.__new__(cls)
        modules.append(inst.describe())
    return modules
