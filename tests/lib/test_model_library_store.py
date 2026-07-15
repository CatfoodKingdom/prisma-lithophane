from __future__ import annotations

import json
import shutil
import stat
import uuid
import zipfile
from pathlib import Path

import pytest

import Prisma.lib.model_library_store as store_module
from Prisma.lib.model_library_store import ModelLibraryStore, ModelLibraryStoreError


def _library_id() -> str:
    return str(uuid.uuid4())


def _make_library(
    root: Path,
    *,
    library_id: str | None = None,
    name: str = "Test Colors",
    minimum: str = "0.1.0",
    maximum: str | None = None,
) -> tuple[Path, str]:
    identifier = library_id or _library_id()
    root.mkdir(parents=True)
    (root / "payload.bin").write_bytes(b"runtime model")
    (root / "prisma-library.json").write_text(
        json.dumps(
            {
                "library_id": identifier,
                "name": name,
                "library_version": "2026.07",
                "publisher": "Test Publisher",
                "created_at": "2026-07-12T00:00:00+00:00",
                "description": "",
                "release_notes": "",
                "minimum_prisma_version": minimum,
                "maximum_prisma_version": maximum,
                "filament_count": 3,
            }
        ),
        encoding="utf-8",
    )
    return root, identifier


@pytest.fixture(autouse=True)
def _fake_library_validator(monkeypatch: pytest.MonkeyPatch):
    def validate(path: str | Path) -> dict:
        root = Path(path)
        try:
            payload = json.loads((root / "prisma-library.json").read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError("manifest is corrupt") from exc
        if not (root / "payload.bin").is_file():
            raise RuntimeError("payload is missing")
        return {
            "ok": True,
            "library_root": str(root.resolve()),
            "library_id": payload["library_id"],
            "library_name": payload["name"],
            "library_version": payload["library_version"],
            "publisher": payload["publisher"],
            "created_at": payload["created_at"],
            "description": payload["description"],
            "release_notes": payload["release_notes"],
            "minimum_prisma_version": payload["minimum_prisma_version"],
            "maximum_prisma_version": payload["maximum_prisma_version"],
            "filament_count": payload["filament_count"],
        }

    monkeypatch.setattr(store_module, "validate_standard_model_library", validate)


def _store(root: Path, *, version: str = "0.1.0") -> ModelLibraryStore:
    return ModelLibraryStore(root / "Generator" / "Model Libraries", root / "Generator" / "Workspace", version)


def test_directory_install_is_create_new_only_and_leaves_no_stage(tmp_path: Path) -> None:
    source, library_id = _make_library(tmp_path / "download")
    store = _store(tmp_path / "Prisma")

    report = store.install(source)

    installed = store.libraries_root / library_id
    assert report["installed"] is True
    assert Path(report["library_root"]) == installed
    assert (installed / "payload.bin").read_bytes() == b"runtime model"
    assert (source / "payload.bin").read_bytes() == b"runtime model"
    assert not list(store.libraries_root.glob(".installing-*"))

    with pytest.raises(ModelLibraryStoreError, match="already installed"):
        store.install(source)
    assert (installed / "payload.bin").read_bytes() == b"runtime model"


def test_non_zip_package_error_does_not_expose_internal_staging_path(tmp_path: Path) -> None:
    package = tmp_path / "not-a-library.zip"
    package.write_text("plain text", encoding="utf-8")
    store = _store(tmp_path / "Prisma")

    with pytest.raises(ModelLibraryStoreError) as excinfo:
        store.install(package)

    assert str(excinfo.value) == "the selected file is not a readable ZIP package"
    assert str(package) not in str(excinfo.value)


def test_activation_persists_only_identity_and_survives_moving_root(tmp_path: Path) -> None:
    source, library_id = _make_library(tmp_path / "download")
    old_root = tmp_path / "Prisma Before Move"
    store = _store(old_root)
    store.install(source)
    store.activate(library_id)

    state_text = store.active_state_path.read_text(encoding="utf-8")
    assert library_id in state_text
    assert str(old_root) not in state_text
    assert store.resolve_active().root == store.libraries_root / library_id

    new_root = tmp_path / "A Different Folder" / "Prisma After Move"
    new_root.parent.mkdir()
    shutil.move(str(old_root), str(new_root))
    moved = _store(new_root)
    active = moved.resolve_active()

    assert active.library_id == library_id
    assert active.root == new_root.resolve() / "Generator" / "Model Libraries" / library_id


def test_listing_reports_corruption_without_hiding_other_libraries(tmp_path: Path) -> None:
    store = _store(tmp_path / "Prisma")
    good, good_id = _make_library(tmp_path / "good")
    store.install(good)
    corrupt_id = _library_id()
    corrupt = store.libraries_root / corrupt_id
    corrupt.mkdir()
    (corrupt / "prisma-library.json").write_text("not json", encoding="utf-8")

    status = store.list()

    by_root = {Path(item["library_root"]).name: item for item in status["libraries"]}
    assert by_root[good_id]["valid"] is True
    assert by_root[corrupt_id]["valid"] is False
    assert by_root[corrupt_id]["library_id"] == corrupt_id
    assert "corrupt" in by_root[corrupt_id]["error"]


def test_active_library_cannot_be_removed_but_inactive_library_can(tmp_path: Path) -> None:
    store = _store(tmp_path / "Prisma")
    first, first_id = _make_library(tmp_path / "first")
    second, second_id = _make_library(tmp_path / "second")
    store.install(first)
    store.install(second)
    store.activate(first_id)

    with pytest.raises(ModelLibraryStoreError, match="active model library cannot be removed"):
        store.remove(first_id)
    store.remove(second_id)

    assert (store.libraries_root / first_id).is_dir()
    assert not (store.libraries_root / second_id).exists()


def test_seed_uses_normal_installer_and_never_overrides_later_selection(tmp_path: Path) -> None:
    store = _store(tmp_path / "Prisma")
    seed, seed_id = _make_library(tmp_path / "seed", name="Seed Colors")
    personal, personal_id = _make_library(tmp_path / "personal", name="Personal Colors")

    first = store.ensure_seed_installed(seed)
    assert first["activated"] is True
    assert store.resolve_active().library_id == seed_id

    store.install(personal)
    store.activate(personal_id)
    again = store.ensure_seed_installed(seed)

    assert again["installed"] is False
    assert again["activated"] is False
    assert store.resolve_active().library_id == personal_id


def test_zip_with_one_wrapper_root_installs_without_extractall(tmp_path: Path) -> None:
    source, library_id = _make_library(tmp_path / "library")
    package = tmp_path / "download.prisma-library.zip"
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in source.rglob("*"):
            if path.is_file():
                archive.write(path, f"Friendly Library/{path.relative_to(source).as_posix()}")

    report = _store(tmp_path / "Prisma").install(package)

    assert report["library_id"] == library_id
    assert Path(report["library_root"]).name == library_id


@pytest.mark.parametrize("unsafe_name", ["../escape.txt", "C:/escape.txt", "wrapper/../../escape.txt"])
def test_zip_path_escape_is_rejected_and_stage_is_removed(tmp_path: Path, unsafe_name: str) -> None:
    source, _library_id_value = _make_library(tmp_path / "library")
    package = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(package, "w") as archive:
        for path in source.rglob("*"):
            if path.is_file():
                archive.write(path, f"wrapper/{path.relative_to(source).as_posix()}")
        archive.writestr(unsafe_name, b"escape")
    store = _store(tmp_path / "Prisma")

    with pytest.raises(ModelLibraryStoreError, match="unsafe|outside"):
        store.install(package)

    assert not list(store.libraries_root.glob(".installing-*"))
    assert not (tmp_path / "escape.txt").exists()


def test_zip_link_is_rejected(tmp_path: Path) -> None:
    source, _library_id_value = _make_library(tmp_path / "library")
    package = tmp_path / "linked.zip"
    with zipfile.ZipFile(package, "w") as archive:
        for path in source.rglob("*"):
            if path.is_file():
                archive.write(path, f"wrapper/{path.relative_to(source).as_posix()}")
        link = zipfile.ZipInfo("wrapper/link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "payload.bin")

    with pytest.raises(ModelLibraryStoreError, match="filesystem link"):
        _store(tmp_path / "Prisma").install(package)


def test_incompatible_library_is_rejected_before_install(tmp_path: Path) -> None:
    source, _library_id_value = _make_library(tmp_path / "future", minimum="9.0.0")
    store = _store(tmp_path / "Prisma", version="0.1.0")

    with pytest.raises(ModelLibraryStoreError, match="requires Prisma 9.0.0"):
        store.install(source)

    assert not list(store.libraries_root.glob(".installing-*"))


def test_reconciliation_removes_only_exact_store_staging_names(tmp_path: Path) -> None:
    store = _store(tmp_path / "Prisma")
    store.libraries_root.mkdir(parents=True)
    abandoned = store.libraries_root / f".installing-{uuid.uuid4().hex}"
    similar = store.libraries_root / ".installing-user-folder"
    abandoned.mkdir()
    similar.mkdir()
    store.workspace_root.mkdir(parents=True)
    state_stage = store.workspace_root / f".active-model-library.json.tmp-{uuid.uuid4().hex}"
    state_similar = store.workspace_root / ".active-model-library.json.tmp-user"
    state_stage.write_text("partial", encoding="utf-8")
    state_similar.write_text("keep", encoding="utf-8")

    assert store.reconcile_staging() == 2
    assert not abandoned.exists()
    assert similar.is_dir()
    assert not state_stage.exists()
    assert state_similar.is_file()
