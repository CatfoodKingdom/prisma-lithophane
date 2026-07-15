"""Guard: the stale ``experimental_`` / ``Experimental`` fossil prefix must not
reappear in the runtime, test, or tracked-settings surfaces.

Task 4B.3 (2026-06-13) renamed the now-permanent staged backend's
``experimental_`` prefix away — to ``staged_`` (backend internals),
``printability_`` (the printability knobs), or the bare name (everything else).
This test fails loudly if any tracked source/test/script/profile file
reintroduces the fossil.

There is intentionally NO allow-list: the rename was total. If a future task
legitimately needs the token, add a narrow, documented exception here.
"""
import subprocess
from pathlib import Path

# tests/generator/<thisfile> -> worktree root is two parents up.
_ROOT = Path(__file__).resolve().parents[2]
_SELF_REL = Path(__file__).resolve().relative_to(_ROOT).as_posix()

# Assembled so this guard file does not match its own scan.
_SNAKE = "experimental" + "_"
_CAMEL = "Experiment" + "al"

_SCOPE_PREFIXES = (
    "Prisma/generator/",
    "tests/generator/",
    "scripts/",
    "Prisma/data/generator/settings_profiles/",
)
_EXTS = ("py", "js", "html", "json")


def _scope_files():
    out = subprocess.check_output(["git", "ls-files"], cwd=_ROOT, text=True).splitlines()
    files = []
    for rel in out:
        if rel == _SELF_REL:
            continue
        if rel.startswith("Prisma/generator/docs/"):
            continue
        if not any(rel.startswith(p) for p in _SCOPE_PREFIXES):
            continue
        if rel.rsplit(".", 1)[-1] not in _EXTS:
            continue
        if not (_ROOT / rel).exists():
            continue
        files.append(rel)
    return files


def test_no_experimental_prefix_survives():
    offenders = {}
    for rel in _scope_files():
        text = (_ROOT / rel).read_text(encoding="utf-8", errors="ignore")
        hits = []
        if _SNAKE in text:
            hits.append(_SNAKE)
        if _CAMEL in text:
            hits.append(_CAMEL)
        if hits:
            offenders[rel] = hits
    assert not offenders, (
        "Stale experimental_/Experimental prefix reintroduced (Task 4B.3 retired "
        f"it; use staged_/printability_/bare names): {offenders}"
    )
