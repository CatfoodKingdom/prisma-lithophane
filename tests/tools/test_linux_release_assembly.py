from __future__ import annotations

import json
import os
import tarfile
from pathlib import Path

import pytest

import scripts.assemble_linux_release as release
from tests.tools.release_license_fixture import make_license_bundle


pytestmark = [
    pytest.mark.platform,
    pytest.mark.skipif(os.name != "posix", reason="Linux assembler requires POSIX semantics"),
]


def _sources(tmp_path: Path, *, product: release.Product) -> tuple[Path, Path]:
    app = tmp_path / "pyinstaller"
    generator_app = app / "_internal" / "Prisma" / "generator" / "app"
    generator_app.mkdir(parents=True)
    (generator_app / "index.html").write_text("Generator")
    (app / "_internal" / "runtime.so").write_bytes(b"runtime")
    executable_names = (
        (release.GENERATOR_ONLY_EXE,)
        if product == "generator"
        else (release.GENERATOR_EXE, release.CALIBRATION_EXE)
    )
    for name in executable_names:
        path = app / name
        path.write_bytes(b"executable")
        path.chmod(0o755)
    if product == "suite":
        calibration = app / "_internal" / "Prisma" / "calibration"
        (calibration / "app").mkdir(parents=True)
        (calibration / "app" / "index.html").write_text("Calibration")
        (calibration / "blank_calibration_schema.sql").write_text("CREATE TABLE example(id);")
    library = tmp_path / "library"
    library.mkdir()
    (library / "prisma-library.json").write_text("{}")
    (library / "model.bin").write_bytes(b"model")
    return app, library


def _library_report(path: Path) -> dict:
    return {"ok": True, "library_root": str(path), "library_version": "2026.07", "file_count": 2}


@pytest.mark.parametrize("product", ["generator", "suite"])
def test_assembly_stages_validates_and_archives_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    product: release.Product,
) -> None:
    app, library = _sources(tmp_path, product=product)
    target_name = "Prisma-0.1.0" if product == "generator" else "Prisma-Suite-0.1.0"
    destination = tmp_path / target_name
    tar_path = tmp_path / f"{target_name}-linux-x86_64.tar.gz"
    monkeypatch.setattr(release, "validate_standard_model_library", _library_report)

    report = release.assemble_linux_release(
        product=product,
        pyinstaller_root=app,
        model_library_root=library,
        third_party_licenses_root=make_license_bundle(tmp_path / "licenses"),
        destination=destination,
        release_version="0.1.0-test1",
        app_version="0.1.0",
        tar_path=tar_path,
    )

    assert report["ok"] is True
    assert report["product"] == product
    assert (destination / "_internal" / "seed-model-library" / "model.bin").read_bytes() == b"model"
    for name in release.LEGAL_FILES:
        assert (destination / name).read_bytes() == (release.PROJECT_ROOT / name).read_bytes()
    for relative in release._visible_directories(product):
        assert destination.joinpath(*relative.split("/")).is_dir()
    assert release.validate_linux_release(destination)["ok"] is True

    with tarfile.open(tar_path, "r:gz") as archive:
        members = archive.getmembers()
    assert members
    assert all((member.uid, member.gid, member.uname, member.gname) == (0, 0, "root", "root") for member in members)
    assert f"{target_name}/{release.RELEASE_MANIFEST}" in {member.name for member in members}


def test_safe_pyinstaller_symlink_is_preserved_and_manifested(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, library = _sources(tmp_path, product="generator")
    target = app / "_internal" / "runtime.so"
    link = app / "_internal" / "runtime-current.so"
    link.symlink_to(target.name)
    monkeypatch.setattr(release, "validate_standard_model_library", _library_report)
    destination = tmp_path / "release"

    release.assemble_linux_release(
        product="generator",
        pyinstaller_root=app,
        model_library_root=library,
        third_party_licenses_root=make_license_bundle(tmp_path / "licenses"),
        destination=destination,
        release_version="test",
        app_version="0.1.0",
    )

    staged_link = destination / "_internal" / "runtime-current.so"
    assert staged_link.is_symlink()
    manifest = json.loads((destination / release.RELEASE_MANIFEST).read_text())
    record = next(item for item in manifest["files"] if item["path"] == "_internal/runtime-current.so")
    assert record["type"] == "symlink"
    assert record["link_target"] == "runtime.so"


def test_escaping_symlink_is_rejected_before_promotion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, library = _sources(tmp_path, product="generator")
    (app / "_internal" / "escape").symlink_to("../../../outside")
    monkeypatch.setattr(release, "validate_standard_model_library", _library_report)
    destination = tmp_path / "release"

    with pytest.raises(release.LinuxReleaseError, match="escapes the root"):
        release.assemble_linux_release(
            product="generator",
            pyinstaller_root=app,
            model_library_root=library,
            third_party_licenses_root=make_license_bundle(tmp_path / "licenses"),
            destination=destination,
            release_version="test",
            app_version="0.1.0",
        )
    assert not destination.exists()


def test_private_runtime_data_is_rejected_before_promotion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, library = _sources(tmp_path, product="suite")
    (app / "calibration.sqlite3").write_bytes(b"private")
    monkeypatch.setattr(release, "validate_standard_model_library", _library_report)

    with pytest.raises(release.LinuxReleaseError, match="forbidden file type"):
        release.assemble_linux_release(
            product="suite",
            pyinstaller_root=app,
            model_library_root=library,
            third_party_licenses_root=make_license_bundle(tmp_path / "licenses"),
            destination=tmp_path / "release",
            release_version="test",
            app_version="0.1.0",
        )


def test_validator_detects_changed_executable_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, library = _sources(tmp_path, product="generator")
    monkeypatch.setattr(release, "validate_standard_model_library", _library_report)
    destination = tmp_path / "release"
    release.assemble_linux_release(
        product="generator",
        pyinstaller_root=app,
        model_library_root=library,
        third_party_licenses_root=make_license_bundle(tmp_path / "licenses"),
        destination=destination,
        release_version="test",
        app_version="0.1.0",
    )
    (destination / release.GENERATOR_ONLY_EXE).chmod(0o644)

    with pytest.raises(release.LinuxReleaseError, match="not executable"):
        release.validate_linux_release(destination)


def test_assembly_rejects_tar_destination_inside_release_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, library = _sources(tmp_path, product="generator")
    monkeypatch.setattr(release, "validate_standard_model_library", _library_report)
    destination = tmp_path / "release"

    with pytest.raises(release.LinuxReleaseError, match="may not be inside"):
        release.assemble_linux_release(
            product="generator",
            pyinstaller_root=app,
            model_library_root=library,
            third_party_licenses_root=make_license_bundle(tmp_path / "licenses"),
            destination=destination,
            release_version="test",
            app_version="0.1.0",
            tar_path=destination / "release.tar.gz",
        )
