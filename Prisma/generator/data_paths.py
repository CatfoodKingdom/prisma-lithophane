"""Single source of truth for the portable Generator filesystem contract.

The launcher explicitly supplies the active published library, visible Images
and Exports folders, and the app-owned Workspace before importing this module.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

_GEN_DIR = Path(__file__).resolve().parent          # Prisma/generator
_PRISMA_DIR = _GEN_DIR.parent                        # Prisma

_MODEL_LIBRARY_ROOT_ENV = "PRISMA_MODEL_LIBRARY_ROOT"
_USER_DATA_ROOT_ENV = "PRISMA_USER_DATA_ROOT"
_IMAGE_ROOT_ENV = "PRISMA_IMAGE_ROOT"
_EXPORT_ROOT_ENV = "PRISMA_EXPORT_ROOT"


def _environment(environ: Optional[dict]) -> dict:
    return os.environ if environ is None else environ


def _environment_path(environ: dict, name: str) -> Path | None:
    value = str(environ.get(name) or "").strip()
    return Path(value).expanduser().resolve() if value else None


def resolve_data_dir(
    *,
    generator_dir: Path = _GEN_DIR,
    prisma_dir: Path = _PRISMA_DIR,
    environ: Optional[dict] = None,
) -> Path:
    """Resolve the active published library selected by the launcher."""
    env = _environment(environ)
    configured = _environment_path(env, _MODEL_LIBRARY_ROOT_ENV)
    if configured is None:
        raise RuntimeError(
            "Generator has no selected published model library. "
            "Start it through the Prisma launcher."
        )
    return configured


def resolve_user_data_dir(
    *,
    model_library_dir: Path,
    environ: Optional[dict] = None,
) -> Path:
    """Resolve the Generator Workspace selected by the launcher."""
    configured = _environment_path(_environment(environ), _USER_DATA_ROOT_ENV)
    if configured is None:
        raise RuntimeError("Generator has no Workspace path. Start it through the Prisma launcher.")
    return configured


def resolve_output_dir(
    *,
    prisma_dir: Path = _PRISMA_DIR,
    environ: Optional[dict] = None,
) -> Path:
    """Resolve the user-facing lithophane export directory."""
    configured = _environment_path(_environment(environ), _EXPORT_ROOT_ENV)
    if configured is None:
        raise RuntimeError("Generator has no Exports path. Start it through the Prisma launcher.")
    return configured


def resolve_upload_dir(
    *,
    prisma_dir: Path = _PRISMA_DIR,
    user_data_dir: Path,
    environ: Optional[dict] = None,
) -> Path:
    """Resolve the user-facing Generator image folder."""
    env = _environment(environ)
    configured = _environment_path(env, _IMAGE_ROOT_ENV)
    if configured is not None:
        return configured
    raise RuntimeError("Generator has no Images path. Start it through the Prisma launcher.")


def resolve_config_dir(
    *,
    generator_dir: Path = _GEN_DIR,
    user_data_dir: Path,
    environ: Optional[dict] = None,
) -> Path:
    """Resolve mutable Generator configuration inside its Workspace."""
    env = _environment(environ)
    if _environment_path(env, _USER_DATA_ROOT_ENV) is None:
        raise RuntimeError("Generator has no Workspace path. Start it through the Prisma launcher.")
    return Path(user_data_dir) / "config"


DATA_DIR = resolve_data_dir()
MODEL_LIBRARY_DIR = DATA_DIR
GENERATOR_DATA_DIR = resolve_user_data_dir(model_library_dir=MODEL_LIBRARY_DIR)

OUTPUT_DIR = resolve_output_dir()
UPLOAD_DIR = resolve_upload_dir(user_data_dir=GENERATOR_DATA_DIR)
CONFIG_DIR = resolve_config_dir(user_data_dir=GENERATOR_DATA_DIR)

CACHE_DIR = GENERATOR_DATA_DIR / "cache"
RUN_CACHE_DIR = CACHE_DIR / "runs"
LUT_CACHE_DIR = CACHE_DIR / "luts"
AUTO_RUNS_DIR = CACHE_DIR / "auto_runs"
SOURCE_IMAGE_CACHE_DIR = CACHE_DIR / "source-images"
SOURCE_IMAGE_IMPORT_DIR = CACHE_DIR / "image-imports"

SAVED_RUNS_DIR = GENERATOR_DATA_DIR / "saved_runs"
LOG_DIR = GENERATOR_DATA_DIR / "logs"


def ensure_dirs() -> None:
    """Create the managed directories that must exist on boot."""
    for d in (
        UPLOAD_DIR,
        OUTPUT_DIR,
        CACHE_DIR,
        RUN_CACHE_DIR,
        LUT_CACHE_DIR,
        AUTO_RUNS_DIR,
        SOURCE_IMAGE_CACHE_DIR,
        SOURCE_IMAGE_IMPORT_DIR,
        SAVED_RUNS_DIR,
        LOG_DIR,
    ):
        d.mkdir(parents=True, exist_ok=True)
