"""Paths for the deterministic published-library Generator test fixture."""

from __future__ import annotations

import os
from pathlib import Path


PROFILES_DIR = Path(os.environ["PRISMA_MODEL_LIBRARY_ROOT"]) / "filaments" / "profiles"
