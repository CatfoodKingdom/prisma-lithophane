from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
GENERATOR = ROOT / "Prisma" / "generator"


def _read_product_sources() -> str:
    rel_paths = [
        "facade.py",
        "server.py",
        "pipeline/modules.py",
        "pipeline/registry.py",
        "pipeline/runner.py",
        "pipeline/state.py",
    ]
    return "\n".join((GENERATOR / rel_path).read_text(encoding="utf-8") for rel_path in rel_paths)


def test_retired_solver_and_refinement_files_are_removed() -> None:
    assert not (GENERATOR / "refinements").exists()

    retired_solver_files = [
        "dual_resolution.py",
        "dual_resolution_v2.py",
        "regional_ownership.py",
        "region_consolidation.py",
        "superpixel.py",
        "joint_lut.py",
    ]
    for filename in retired_solver_files:
        assert not (GENERATOR / "solver" / filename).exists()


def test_product_sources_do_not_import_retired_solver_or_refinement_modules() -> None:
    source = _read_product_sources()
    forbidden_snippets = [
        "from refinements",
        "import refinements",
        "refinements.cap_smoothing",
        "refinements.guided_surface",
        "refinements.geometry_native_cleanup",
        "from solver.dual_resolution",
        "from solver.dual_resolution_v2",
        "from solver.superpixel",
        "from solver.regional_ownership",
        "from solver.joint_lut",
        "import solver.dual_resolution",
        "import solver.dual_resolution_v2",
        "import solver.superpixel",
        "import solver.regional_ownership",
        "import solver.joint_lut",
        "_resolve_refinements",
        "CapSmoothingRefinement",
        "GuidedSurfaceRefinement",
        "GeometryNativeCleanupRefinement",
    ]
    for snippet in forbidden_snippets:
        assert snippet not in source
