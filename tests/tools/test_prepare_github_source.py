from pathlib import Path

from scripts.prepare_github_source import (
    PACKAGING_FILES,
    ROOT_FILES,
    SCRIPT_FILES,
    _collect_allowlist,
)


def _write(root: Path, relative: str, content: str = "fixture") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_source_allowlist_excludes_generator_runtime_scratch(tmp_path: Path) -> None:
    for relative in ROOT_FILES + PACKAGING_FILES + SCRIPT_FILES:
        _write(tmp_path, relative)
    public_module = _write(tmp_path, "Prisma/generator/app/bootstrap.js")
    public_test = _write(tmp_path, "tests/generator/test_public.py")
    _write(tmp_path, "Prisma/generator/.tmp/cache/run.json")

    selected = set(_collect_allowlist(tmp_path))

    assert public_module in selected
    assert public_test in selected
    assert not any(".tmp" in path.parts for path in selected)
