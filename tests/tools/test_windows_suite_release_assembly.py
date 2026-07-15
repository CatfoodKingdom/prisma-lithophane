from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

import scripts.assemble_windows_suite as suite
from tests.tools.release_license_fixture import make_license_bundle


def _sources(tmp_path: Path) -> tuple[Path, Path]:
    app = tmp_path / "pyinstaller"
    generator_app = app / "_internal" / "Prisma" / "generator" / "app"
    calibration = app / "_internal" / "Prisma" / "calibration"
    generator_app.mkdir(parents=True)
    (calibration / "app").mkdir(parents=True)
    (app / suite.GENERATOR_EXE).write_bytes(b"generator-exe")
    (app / suite.CALIBRATION_EXE).write_bytes(b"calibration-exe")
    (app / "_internal" / "runtime.dll").write_bytes(b"runtime")
    (generator_app / "index.html").write_text("Generator")
    (calibration / "app" / "index.html").write_text("Calibration")
    (calibration / "blank_calibration_schema.sql").write_text("CREATE TABLE example(id);")
    library = tmp_path / "library"
    library.mkdir()
    (library / "prisma-library.json").write_text("{}")
    (library / "model.bin").write_bytes(b"model")
    return app, library


def _library_report(path: Path) -> dict:
    return {"ok": True, "library_root": str(path), "library_version": "2026.07", "file_count": 2}


def _assemble(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    app: Path | None = None,
    library: Path | None = None,
    destination_name: str = "Prisma-Suite-0.1.0",
    zip_it: bool = False,
) -> tuple[Path, Path | None, dict]:
    if app is None or library is None:
        app, library = _sources(tmp_path)
    monkeypatch.setattr(suite, "validate_standard_model_library", _library_report)
    destination = tmp_path / destination_name
    zip_path = tmp_path / f"{destination_name}-windows-x64.zip" if zip_it else None
    report = suite.assemble_windows_suite_release(
        pyinstaller_root=app,
        model_library_root=library,
        third_party_licenses_root=make_license_bundle(tmp_path / "licenses"),
        destination=destination,
        release_version="0.1.0-test1",
        app_version="0.1.0",
        zip_path=zip_path,
    )
    return destination, zip_path, report


def test_assembly_stages_both_apps_visible_skeleton_and_exact_zip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination, zip_path, report = _assemble(tmp_path, monkeypatch, zip_it=True)

    assert report["ok"] is True
    assert report["model_library_version"] == "2026.07"
    assert (destination / suite.GENERATOR_EXE).read_bytes() == b"generator-exe"
    assert (destination / suite.CALIBRATION_EXE).read_bytes() == b"calibration-exe"
    assert (destination / "_internal" / "seed-model-library" / "model.bin").read_bytes() == b"model"
    for name in suite.LEGAL_FILES:
        assert (destination / name).read_bytes() == (suite.PROJECT_ROOT / name).read_bytes()
    for relative in suite.VISIBLE_DIRECTORIES:
        assert destination.joinpath(*relative.split("/")).is_dir()
    readme = (destination / "README.txt").read_text(encoding="utf-8")
    assert "Double-click Prisma Generator.exe" in readme
    assert "Double-click Prisma Calibration.exe" in readme
    assert suite.validate_windows_suite_release(destination)["ok"] is True

    assert zip_path is not None
    with zipfile.ZipFile(zip_path) as archive:
        names = set(archive.namelist())
    root = destination.name
    assert f"{root}/{suite.GENERATOR_EXE}" in names
    assert f"{root}/{suite.CALIBRATION_EXE}" in names
    assert f"{root}/_internal/seed-model-library/model.bin" in names
    assert f"{root}/prisma-release.json" in names
    for name in suite.LEGAL_FILES:
        assert f"{root}/{name}" in names
    for relative in suite.VISIBLE_DIRECTORIES:
        assert f"{root}/{relative}/" in names


def test_manifest_identifies_suite_product(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination, _, _ = _assemble(tmp_path, monkeypatch)
    manifest = json.loads((destination / suite.RELEASE_MANIFEST).read_text(encoding="utf-8"))

    assert manifest["format"] == "prisma-windows-suite-release"
    assert manifest["schema_version"] == 1
    assert manifest["applications"] == ["generator", "calibration"]

    manifest["applications"] = ["generator"]
    (destination / suite.RELEASE_MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(suite.WindowsSuiteReleaseError, match="does not identify"):
        suite.validate_windows_suite_release(destination)


@pytest.mark.parametrize("missing", [suite.GENERATOR_EXE, suite.CALIBRATION_EXE])
def test_assembly_requires_both_entry_points(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
) -> None:
    app, library = _sources(tmp_path)
    (app / missing).unlink()
    monkeypatch.setattr(suite, "validate_standard_model_library", _library_report)

    with pytest.raises(suite.WindowsSuiteReleaseError, match="source is missing"):
        suite.assemble_windows_suite_release(
            pyinstaller_root=app,
            model_library_root=library,
            third_party_licenses_root=make_license_bundle(tmp_path / "licenses"),
            destination=tmp_path / "release",
            release_version="test",
            app_version="0.1.0",
        )


@pytest.mark.parametrize("private_name", ["calibration.sqlite3", "sample.CR2", "blank.dng"])
def test_forbidden_private_file_aborts_before_destination_promotion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    private_name: str,
) -> None:
    app, library = _sources(tmp_path)
    (app / private_name).write_bytes(b"private")
    monkeypatch.setattr(suite, "validate_standard_model_library", _library_report)
    destination = tmp_path / "release"

    with pytest.raises(suite.WindowsSuiteReleaseError, match="forbidden file type"):
        suite.assemble_windows_suite_release(
            pyinstaller_root=app,
            model_library_root=library,
            third_party_licenses_root=make_license_bundle(tmp_path / "licenses"),
            destination=destination,
            release_version="test",
            app_version="0.1.0",
        )

    assert not destination.exists()
    assert list(tmp_path.glob(".release.staging-*")) == []


def test_only_asserted_packaged_python_source_locations_are_allowed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, library = _sources(tmp_path)
    cv2_config = app / "_internal" / "cv2" / "config.py"
    v63_module = (
        app
        / "_internal"
        / "fitting"
        / "photo_stack_model"
        / "v63_fit_engine"
        / "runner.py"
    )
    cv2_config.parent.mkdir()
    v63_module.parent.mkdir(parents=True)
    cv2_config.write_text("BINARIES_PATHS = []")
    v63_module.write_text("VERSION = 63")
    destination, _, _ = _assemble(
        tmp_path,
        monkeypatch,
        app=app,
        library=library,
        destination_name="allowed",
    )
    assert (destination / cv2_config.relative_to(app)).is_file()
    assert (destination / v63_module.relative_to(app)).is_file()

    app2, library2 = _sources(tmp_path / "second")
    (app2 / "_internal" / "fitting" / "leaked.py").parent.mkdir(parents=True)
    (app2 / "_internal" / "fitting" / "leaked.py").write_text("print('no')")
    with pytest.raises(suite.WindowsSuiteReleaseError, match="forbidden Python source"):
        suite.assemble_windows_suite_release(
            pyinstaller_root=app2,
            model_library_root=library2,
            third_party_licenses_root=make_license_bundle(tmp_path / "licenses"),
            destination=tmp_path / "rejected",
            release_version="test",
            app_version="0.1.0",
        )


def test_assembly_refuses_live_data_in_pyinstaller_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, library = _sources(tmp_path)
    (app / "Calibration" / "Workspace").mkdir(parents=True)
    monkeypatch.setattr(suite, "validate_standard_model_library", _library_report)

    with pytest.raises(suite.WindowsSuiteReleaseError, match="live user-data"):
        suite.assemble_windows_suite_release(
            pyinstaller_root=app,
            model_library_root=library,
            third_party_licenses_root=make_license_bundle(tmp_path / "licenses"),
            destination=tmp_path / "release",
            release_version="test",
            app_version="0.1.0",
        )


def test_assembly_refuses_to_replace_existing_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, library = _sources(tmp_path)
    monkeypatch.setattr(suite, "validate_standard_model_library", _library_report)
    destination = tmp_path / "existing"
    destination.mkdir()
    marker = destination / "keep.txt"
    marker.write_text("keep")

    with pytest.raises(suite.WindowsSuiteReleaseError, match="destination already exists"):
        suite.assemble_windows_suite_release(
            pyinstaller_root=app,
            model_library_root=library,
            third_party_licenses_root=make_license_bundle(tmp_path / "licenses"),
            destination=destination,
            release_version="test",
            app_version="0.1.0",
        )

    assert marker.read_text() == "keep"


def test_zip_failure_does_not_promote_release_folder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, library = _sources(tmp_path)
    monkeypatch.setattr(suite, "validate_standard_model_library", _library_report)
    monkeypatch.setattr(
        suite,
        "_write_zip",
        lambda *args, **kwargs: (_ for _ in ()).throw(suite.WindowsSuiteReleaseError("zip failed")),
    )
    destination = tmp_path / "release"
    zip_path = tmp_path / "release.zip"

    with pytest.raises(suite.WindowsSuiteReleaseError, match="zip failed"):
        suite.assemble_windows_suite_release(
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


def test_assembly_rejects_zip_destination_inside_release_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, library = _sources(tmp_path)
    monkeypatch.setattr(suite, "validate_standard_model_library", _library_report)
    destination = tmp_path / "release"

    with pytest.raises(suite.WindowsSuiteReleaseError, match="ZIP destination may not be inside"):
        suite.assemble_windows_suite_release(
            pyinstaller_root=app,
            model_library_root=library,
            third_party_licenses_root=make_license_bundle(tmp_path / "licenses"),
            destination=destination,
            release_version="test",
            app_version="0.1.0",
            zip_path=destination / "release.zip",
        )


def test_release_validator_detects_changed_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination, _, _ = _assemble(tmp_path, monkeypatch)
    (destination / suite.GENERATOR_EXE).write_bytes(b"changed")

    with pytest.raises(suite.WindowsSuiteReleaseError, match="(?:size|hash) mismatch"):
        suite.validate_windows_suite_release(destination)
