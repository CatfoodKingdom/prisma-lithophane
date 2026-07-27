"""Structural contracts for the stage-owned solver implementation."""

from __future__ import annotations

import ast
import subprocess
import sys
from collections import defaultdict
from pathlib import Path


_GENERATOR_ROOT = Path(__file__).resolve().parents[2] / "Prisma" / "generator"
_PIPELINE_ROOT = _GENERATOR_ROOT / "pipeline"
_STAGED_ROOT = _PIPELINE_ROOT / "staged"


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imported_modules(path: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    return modules


def test_staged_runner_is_a_thin_product_facade():
    path = _PIPELINE_ROOT / "staged_runner.py"
    tree = _tree(path)

    implementations = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]

    assert implementations == []
    assert len(path.read_text(encoding="utf-8").splitlines()) <= 10


def test_staged_runner_preserves_the_product_entry_point():
    from pipeline.staged.orchestration import (
        run_staged_backend_path as implementation,
    )
    from pipeline.staged_runner import run_staged_backend_path as facade

    assert facade is implementation


def test_stage_services_expose_named_entry_points():
    from pipeline.staged.stage0_directives import compile_directives
    from pipeline.staged.stage1_zones import build_zone_plan
    from pipeline.staged.stage2 import build_visible_plan
    from pipeline.staged.stage3_filler import build_filler_plan
    from pipeline.staged.stage4 import build_cap_plan

    services = (
        compile_directives,
        build_zone_plan,
        build_visible_plan,
        build_filler_plan,
        build_cap_plan,
    )

    assert all(callable(service) for service in services)
    assert [service.__name__ for service in services] == [
        "compile_directives",
        "build_zone_plan",
        "build_visible_plan",
        "build_filler_plan",
        "build_cap_plan",
    ]


def test_stage_entry_modules_import_in_fresh_processes():
    modules = (
        "pipeline.staged_runner",
        "pipeline.staged.orchestration",
        "pipeline.staged.stage0_directives",
        "pipeline.staged.stage1_zones",
        "pipeline.staged.stage2.service",
        "pipeline.staged.stage3_filler",
        "pipeline.staged.stage4.service",
    )

    for module in modules:
        completed = subprocess.run(
            [sys.executable, "-c", f"import {module}"],
            cwd=_GENERATOR_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr


def test_stage_modules_do_not_import_the_compatibility_facade():
    offenders = []
    for path in _STAGED_ROOT.rglob("*.py"):
        imported = _imported_modules(path)
        if any(module.endswith("staged_runner") for module in imported):
            offenders.append(path.relative_to(_GENERATOR_ROOT).as_posix())

    assert offenders == []


def test_stage2_and_stage4_implementation_packages_are_independent():
    offenders: list[str] = []
    for stage_name, forbidden_name in (("stage2", "stage4"), ("stage4", "stage2")):
        for path in (_STAGED_ROOT / stage_name).glob("*.py"):
            for module in _imported_modules(path):
                if forbidden_name in module.split("."):
                    offenders.append(
                        f"{path.relative_to(_GENERATOR_ROOT).as_posix()} -> {module}"
                    )

    assert offenders == []


def test_only_orchestration_imports_both_stage_services():
    importers: list[str] = []
    for path in _STAGED_ROOT.rglob("*.py"):
        imported = _imported_modules(path)
        has_stage2 = any("stage2" in module.split(".") for module in imported)
        has_stage4 = any("stage4" in module.split(".") for module in imported)
        if has_stage2 and has_stage4:
            importers.append(path.relative_to(_STAGED_ROOT).as_posix())

    assert importers == ["orchestration.py"]


def test_staged_package_has_no_generic_catchall_modules():
    forbidden = {"common.py", "engine.py", "helpers.py", "manager.py", "utils.py"}

    assert forbidden.isdisjoint(path.name for path in _STAGED_ROOT.rglob("*.py"))


def test_each_staged_definition_has_one_implementation_owner():
    owners: dict[str, list[str]] = defaultdict(list)
    for path in _STAGED_ROOT.rglob("*.py"):
        for node in _tree(path).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                owners[node.name].append(path.relative_to(_STAGED_ROOT).as_posix())

    assert {name: paths for name, paths in owners.items() if len(paths) > 1} == {}


def test_orchestrator_stays_focused_on_stage_sequence():
    path = _STAGED_ROOT / "orchestration.py"
    tree = _tree(path)
    functions = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]

    assert functions == ["run_staged_backend_path"]
    assert len(path.read_text(encoding="utf-8").splitlines()) <= 250
