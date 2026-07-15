from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

import scripts.assemble_windows_release as release
from tests.tools.release_license_fixture import make_license_bundle


def _sources(tmp_path: Path) -> tuple[Path, Path]:
    app = tmp_path / "pyinstaller"
    (app / "_internal" / "Prisma" / "generator" / "app").mkdir(parents=True)
    (app / "Prisma.exe").write_bytes(b"exe")
    (app / "_internal" / "runtime.dll").write_bytes(b"runtime")
    (app / "_internal" / "Prisma" / "generator" / "app" / "index.html").write_text("Prisma")
    library = tmp_path / "library"
    library.mkdir()
    (library / "prisma-library.json").write_text("{}")
    (library / "model.bin").write_bytes(b"model")
    return app, library


def _library_report(path: Path) -> dict:
    return {"ok": True, "library_root": str(path), "library_version": "2026.07", "file_count": 2}


def test_assembly_stages_validates_and_zips_exact_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, library = _sources(tmp_path)
    monkeypatch.setattr(release, "validate_standard_model_library", _library_report)
    destination = tmp_path / "Prisma-0.1.0"
    zip_path = tmp_path / "Prisma-0.1.0-windows-x64.zip"

    report = release.assemble_windows_release(
        pyinstaller_root=app,
        model_library_root=library,
        third_party_licenses_root=make_license_bundle(tmp_path / "licenses"),
        destination=destination,
        release_version="0.1.0-test1",
        app_version="0.1.0",
        zip_path=zip_path,
    )

    assert report["ok"] is True
    assert report["model_library_version"] == "2026.07"
    assert (destination / "Prisma.exe").read_bytes() == b"exe"
    assert (destination / "_internal" / "seed-model-library" / "model.bin").read_bytes() == b"model"
    for name in release.LEGAL_FILES:
        assert (destination / name).read_bytes() == (release.PROJECT_ROOT / name).read_bytes()
    assert "Double-click Prisma.exe" in (destination / "README.txt").read_text(encoding="utf-8")
    assert release.validate_windows_release(destination)["ok"] is True
    with zipfile.ZipFile(zip_path) as archive:
        names = set(archive.namelist())
    assert "Prisma-0.1.0/Prisma.exe" in names
    assert "Prisma-0.1.0/_internal/seed-model-library/model.bin" in names
    assert "Prisma-0.1.0/prisma-release.json" in names
    for name in release.LEGAL_FILES:
        assert f"Prisma-0.1.0/{name}" in names


def test_forbidden_private_file_aborts_before_destination_promotion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, library = _sources(tmp_path)
    (app / "calibration.sqlite3").write_bytes(b"private")
    monkeypatch.setattr(release, "validate_standard_model_library", _library_report)
    destination = tmp_path / "release"

    with pytest.raises(release.WindowsReleaseError, match="forbidden file type"):
        release.assemble_windows_release(
            pyinstaller_root=app,
            model_library_root=library,
            third_party_licenses_root=make_license_bundle(tmp_path / "licenses"),
            destination=destination,
            release_version="test",
            app_version="0.1.0",
        )

    assert not destination.exists()
    assert list(tmp_path.glob(".release.staging-*")) == []


def test_only_packaged_opencv_python_runtime_files_are_allowed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, library = _sources(tmp_path)
    cv2_config = app / "_internal" / "cv2" / "config.py"
    cv2_config.parent.mkdir()
    cv2_config.write_text("BINARIES_PATHS = []")
    monkeypatch.setattr(release, "validate_standard_model_library", _library_report)

    allowed = tmp_path / "allowed"
    release.assemble_windows_release(
        pyinstaller_root=app,
        model_library_root=library,
        third_party_licenses_root=make_license_bundle(tmp_path / "licenses"),
        destination=allowed,
        release_version="test",
        app_version="0.1.0",
    )
    assert (allowed / "_internal" / "cv2" / "config.py").is_file()

    app2, library2 = _sources(tmp_path / "second")
    (app2 / "leaked_source.py").write_text("print('no')")
    with pytest.raises(release.WindowsReleaseError, match="forbidden Python source"):
        release.assemble_windows_release(
            pyinstaller_root=app2,
            model_library_root=library2,
            third_party_licenses_root=make_license_bundle(tmp_path / "licenses"),
            destination=tmp_path / "rejected",
            release_version="test",
            app_version="0.1.0",
        )


def test_assembly_refuses_to_replace_existing_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, library = _sources(tmp_path)
    monkeypatch.setattr(release, "validate_standard_model_library", _library_report)
    destination = tmp_path / "existing"
    destination.mkdir()
    marker = destination / "keep.txt"
    marker.write_text("keep")

    with pytest.raises(release.WindowsReleaseError, match="destination already exists"):
        release.assemble_windows_release(
            pyinstaller_root=app,
            model_library_root=library,
            third_party_licenses_root=make_license_bundle(tmp_path / "licenses"),
            destination=destination,
            release_version="test",
            app_version="0.1.0",
        )

    assert marker.read_text() == "keep"


def test_release_validator_detects_changed_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, library = _sources(tmp_path)
    monkeypatch.setattr(release, "validate_standard_model_library", _library_report)
    destination = tmp_path / "release"
    release.assemble_windows_release(
        pyinstaller_root=app,
        model_library_root=library,
        third_party_licenses_root=make_license_bundle(tmp_path / "licenses"),
        destination=destination,
        release_version="test",
        app_version="0.1.0",
    )
    (destination / "Prisma.exe").write_bytes(b"changed")

    with pytest.raises(release.WindowsReleaseError, match="(?:size|hash) mismatch"):
        release.validate_windows_release(destination)


def test_zip_failure_does_not_promote_release_folder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, library = _sources(tmp_path)
    monkeypatch.setattr(release, "validate_standard_model_library", _library_report)
    monkeypatch.setattr(
        release,
        "_write_zip",
        lambda *args, **kwargs: (_ for _ in ()).throw(release.WindowsReleaseError("zip failed")),
    )
    destination = tmp_path / "release"
    zip_path = tmp_path / "release.zip"

    with pytest.raises(release.WindowsReleaseError, match="zip failed"):
        release.assemble_windows_release(
            pyinstaller_root=app,
            model_library_root=library,
            third_party_licenses_root=make_license_bundle(tmp_path / "licenses"),
            destination=destination,
            release_version="test",
            app_version="0.1.0",
            zip_path=zip_path,
        )

    assert not destination.exists()
    assert not zip_path.exists()
    assert list(tmp_path.glob(".release.staging-*")) == []
