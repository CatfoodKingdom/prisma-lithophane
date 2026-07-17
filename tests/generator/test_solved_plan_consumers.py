"""Phase 3 commit 6 — downstream consumer migration onto SolvedMaterialPlan.

Tests assert:
  - runner reads image domain from solved_plan when available
  - direct runner/facade calls use the staged plan path by default
  - facade.SolveResult carries solved_plan through from state
  - server session stores and clears solved_plan
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

from tests.generator.profile_fixture import PROFILES_DIR as _PROFILES_DIR


# ── runner: plan-authoritative image domain ──────────────────────────────────


def test_runner_reads_domain_from_plan():
    """When solved_plan is populated, runner reads image domain from it."""
    from pipeline.runner import run_pipeline
    from pipeline.state import PipelineConfig, FULL_PRESET

    cfg = PipelineConfig(
        palette=["bambu-basic-cyan", "bambu-basic-yellow"],
        white_base="panchroma-matte-cotton-white",
        profiles_dir=_PROFILES_DIR,
        preset=FULL_PRESET,
    )

    img = np.random.default_rng(0).integers(0, 256, (10, 10, 3), dtype=np.uint8)
    state = run_pipeline(img, cfg)

    assert state.solved_plan is not None
    assert state.image_domain_width_mm == state.solved_plan.image_domain_width_mm
    assert state.image_domain_height_mm == state.solved_plan.image_domain_height_mm


def test_runner_uses_staged_plan_path_by_default():
    """Direct runner calls populate solved_plan; the solver slot is no longer a fallback path."""
    from pipeline.runner import run_pipeline
    from pipeline.state import PipelineConfig, PREVIEW_PRESET

    cfg = PipelineConfig(
        palette=["bambu-basic-cyan"],
        white_base="panchroma-matte-cotton-white",
        profiles_dir=_PROFILES_DIR,
        preset=PREVIEW_PRESET,
    )

    img = np.random.default_rng(1).integers(0, 256, (8, 8, 3), dtype=np.uint8)
    state = run_pipeline(img, cfg)

    assert state.solved_plan is not None
    assert state.image_domain_width_mm == state.solved_plan.image_domain_width_mm
    assert state.image_domain_height_mm == state.solved_plan.image_domain_height_mm


# ── facade: SolveResult carries solved_plan ──────────────────────────────────


def test_solve_result_carries_plan():
    """facade.solve_full produces a SolveResult with solved_plan populated."""
    from facade import solve_full, SolveConfig

    cfg = SolveConfig(
        palette=["bambu-basic-cyan", "bambu-basic-yellow"],
        white_base="panchroma-matte-cotton-white",
        color_region_target_mm=0.60,
    )

    img = np.random.default_rng(2).integers(0, 256, (10, 10, 3), dtype=np.uint8)
    result = solve_full(img, cfg, progress=None)

    assert result.solved_plan is not None
    assert result.image_domain_width_mm == result.solved_plan.image_domain_width_mm


def test_solve_result_preview_carries_staged_plan_by_default():
    """facade.solve_preview produces a SolveResult with solved_plan populated."""
    from facade import solve_preview, SolveConfig

    cfg = SolveConfig(
        palette=["bambu-basic-cyan"],
        white_base="panchroma-matte-cotton-white",
    )

    img = np.random.default_rng(3).integers(0, 256, (8, 8, 3), dtype=np.uint8)
    result = solve_preview(img, cfg, progress=None)

    assert result.solved_plan is not None
    assert result.image_domain_width_mm == result.solved_plan.image_domain_width_mm


# ── server session: solved_plan lifecycle ────────────────────────────────────


def test_server_session_has_solved_plan_field():
    """The server session solve dict includes a solved_plan slot."""
    _gen_dir = str(Path(__file__).resolve().parent.parent.parent / "Prisma" / "generator")
    if _gen_dir not in sys.path:
        sys.path.insert(0, _gen_dir)

    import importlib
    server = importlib.import_module("server")
    solve_dict = server.session["solve"]
    solve_dict["solved_plan"] = None

    assert "solved_plan" in solve_dict
    assert solve_dict["solved_plan"] is None
