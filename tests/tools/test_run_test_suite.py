from __future__ import annotations

from types import SimpleNamespace

from tools import run_test_suite as runner


def test_generator_scope_runs_python_and_every_frontend_file(monkeypatch):
    monkeypatch.setattr(runner.shutil, "which", lambda executable: f"C:/{executable}.exe")

    suites = runner.build_suites("generator", "full")

    assert [suite.name for suite in suites] == ["Generator Python", "Generator frontend"]
    assert suites[0].command[-1] == "tests\\generator"
    frontend_paths = suites[1].command[2:]
    assert frontend_paths == tuple(sorted(frontend_paths))
    assert "tests\\generator\\test_feature_controller_contracts.js" in frontend_paths


def test_fast_tier_excludes_only_execution_markers():
    suite = runner.build_suites("shared", "fast")[0]

    marker_index = max(index for index, argument in enumerate(suite.command) if argument == "-m")
    assert suite.command[marker_index + 1] == "not slow and not process and not browser"
    assert "tests\\lib" in suite.command
    assert "tests\\tools" in suite.command
    assert "tests\\test_prisma_test_patterns.py" in suite.command


def test_failed_component_makes_aggregate_command_fail(monkeypatch):
    returncodes = iter([0, 3])
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=next(returncodes)),
    )

    result = runner.run_suites(
        [
            runner.Suite("first", ("first",)),
            runner.Suite("second", ("second",)),
        ]
    )

    assert result == 1
