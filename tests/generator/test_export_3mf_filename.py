"""ITEM A2: the single-file 3MF deliverable is named after the export folder.

A 3MF export must write ``{export_id}.3mf`` (same stem as its ``{export_id}/``
folder), not the legacy fixed ``lithophane.3mf``. Unique filenames keep multiple
lithophanes from colliding inside one slicer project.

The export's heavy mesh chain (materialize -> export_solve_bundle -> 3mf writer)
is monkeypatched so this test exercises ONLY the filename/URL/deliverable-entry
contract in ``server._perform_export_files`` plus the zip-download glob, fast and
deterministically. The written 3MF path, the served file URL, the per-file
deliverable entry (the API response ``files`` list — NOT ``export_manifest.json``,
which records per-object mesh paths), and the download-zip member must all use
``{export_id}.3mf``.
"""
from __future__ import annotations

import copy
import json
import threading
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

import data_paths
import server
from mesh.export_mesh_bundle import ExportMeshBundle, MeshObject
from mesh.post_solve_export import PostSolveBundleExportResult

_SESSION_TEMPLATE = copy.deepcopy(server.session)


def _object(
    object_key: str,
    *,
    material_key: str,
    role: str,
    x_offset: float,
) -> MeshObject:
    vertices = np.array(
        [
            [x_offset, 0.0, 0.0],
            [x_offset + 1.0, 0.0, 0.0],
            [x_offset, 1.0, 0.0],
        ],
        dtype=np.float64,
    )
    faces = np.array([[0, 1, 2]], dtype=np.int64)
    return MeshObject(
        object_key=object_key,
        material_key=material_key,
        role=role,
        vertices=vertices,
        faces=faces,
        mesh_style="rectilinear_interval",
    )


def _export_bundle_with_color_quarantine() -> ExportMeshBundle:
    return ExportMeshBundle(
        objects=(
            _object(
                "__white_base__",
                material_key="__white_base__",
                role="white_base",
                x_offset=0.0,
            ),
            _object("cyan", material_key="cyan", role="color", x_offset=10.0),
            _object(
                "cyan__topology_quarantine__",
                material_key="cyan",
                role="color_quarantine",
                x_offset=20.0,
            ),
            _object(
                "__white_cap__",
                material_key="__white_cap__",
                role="combined_white_cap",
                x_offset=30.0,
            ),
        ),
        image_domain_width_mm=1.0,
        image_domain_height_mm=1.0,
        layer_height_mm=0.08,
        xy_quantum_mm=0.4,
        object_coordinate_frame="test",
        mesh_build_report={},
        quality={},
        color_export_details={},
    )


def _manifest_objects(bundle: ExportMeshBundle) -> list[dict]:
    return [
        {
            "object_key": obj.object_key,
            "material_key": obj.material_key,
            "role": obj.role,
            "mesh_style": obj.mesh_style,
            "vertices": int(obj.vertices.shape[0]),
            "faces": int(obj.faces.shape[0]),
            "path": "",
        }
        for obj in bundle.objects
    ]


@pytest.fixture
def export_env(tmp_path, monkeypatch):
    server.session = copy.deepcopy(_SESSION_TEMPLATE)
    monkeypatch.setattr(data_paths, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(server, "_OUTPUT_DIR", tmp_path / "output")
    (tmp_path / "output").mkdir()
    captured = {}

    # Short-circuit the heavy mesh pipeline. We only assert filename plumbing.
    solve = {"status": "complete", "card_id": "card", "thickness_maps": {"f": object()}}
    cfg = {"image_path": "steve.jpg"}
    monkeypatch.setattr(server, "_resolve_export_target", lambda card_id=None: (solve, cfg, "card"))
    monkeypatch.setattr(
        server, "_prepare_export_materialization", lambda cfg, tm: ({}, ["f"])
    )
    monkeypatch.setattr(
        server,
        "_materialize_post_solve_export_bundle_from_cached_solve",
        lambda **kw: tmp_path / "bundle",
    )

    def _fake_export_solve_bundle(*, out_dir, **kw):
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        bundle = _export_bundle_with_color_quarantine()
        manifest = {
            "schema": "post-solve-export-manifest-v1",
            "objects": _manifest_objects(bundle),
        }
        manifest_path = out / "export_manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        return PostSolveBundleExportResult(
            bundle=bundle,
            manifest=manifest,
            manifest_path=manifest_path,
            output_paths={},
            progress_events=(),
            reload_quality={},
        )

    monkeypatch.setattr(server, "export_solve_bundle", _fake_export_solve_bundle)

    def _fake_write_3mf(bundle, out_path, **kw):
        captured["threemf_bundle"] = bundle
        captured["threemf_kwargs"] = dict(kw)
        out_path = Path(out_path)
        out_path.write_bytes(b"PK\x03\x04fake-3mf")
        return out_path

    monkeypatch.setattr(server, "write_export_mesh_bundle_as_3mf", _fake_write_3mf)
    monkeypatch.setattr(
        server,
        "_build_swap_instruction_payload",
        lambda **kw: {"groups": [], "instructions": "swap instructions\n", "gcode": ""},
    )

    client = TestClient(server.app)
    try:
        yield SimpleNamespace(client=client, tmp_path=tmp_path, captured=captured)
    finally:
        server.session = copy.deepcopy(_SESSION_TEMPLATE)


def test_3mf_export_names_file_after_export_folder(export_env):
    env = export_env
    resp = env.client.post(
        "/api/export/files", json={"output_format": "3mf", "card_id": "card"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    export_id = body["export_id"]
    out_dir = Path(body["out_dir"])
    assert out_dir.name == export_id

    # The written file has the folder's stem, NOT the legacy fixed name.
    expected = out_dir / f"{export_id}.3mf"
    assert expected.exists(), f"expected {expected.name} on disk"
    assert not (out_dir / "lithophane.3mf").exists()

    # The served URL + the per-file deliverable entry both use the new name.
    assert f"{export_id}.3mf" in body["threemf_url"]
    threemf_entries = [f for f in body["files"] if f["role"] == "3mf_package"]
    assert len(threemf_entries) == 1
    assert threemf_entries[0]["name"] == f"{export_id}.3mf"

    swap_path = out_dir / "swap_instructions.txt"
    assert swap_path.read_text(encoding="utf-8") == "swap instructions\n"
    swap_entries = [f for f in body["files"] if f["role"] == "swap_instructions"]
    assert len(swap_entries) == 1
    assert swap_entries[0]["name"] == "swap_instructions.txt"

    # The download-zip glob picks up the renamed file as a zip member.
    zr = env.client.get(f"/api/export/files-zip?dir={export_id}")
    assert zr.status_code == 200
    import io

    with zipfile.ZipFile(io.BytesIO(zr.content)) as zf:
        names = zf.namelist()
    assert f"{export_id}.3mf" in names
    assert "swap_instructions.txt" in names
    assert "lithophane.3mf" not in names


def test_3mf_export_packages_color_quarantine_and_updates_manifest(export_env):
    env = export_env
    resp = env.client.post(
        "/api/export/files", json={"output_format": "3mf", "card_id": "card"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    package_bundle = env.captured["threemf_bundle"]
    package_object_keys = [obj.object_key for obj in package_bundle.objects]
    assert "cyan" in package_object_keys
    assert "cyan__topology_quarantine__" not in package_object_keys

    manifest_path = Path(body["out_dir"]) / "export_manifest.json"
    on_disk_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    response_manifest = body["manifest"]
    assert response_manifest == on_disk_manifest
    assert response_manifest != {"schema": "post-solve-export-manifest-v1"}

    core_keys = [obj["object_key"] for obj in response_manifest["objects"]]
    assert "cyan" in core_keys
    assert "cyan__topology_quarantine__" in core_keys

    packaging = response_manifest["format_packaging"]["3mf"]
    assert packaging["package_filename"] == f"{body['export_id']}.3mf"
    assert Path(packaging["package_path"]) == manifest_path.parent / f"{body['export_id']}.3mf"
    assert ".export-stage-" not in json.dumps(response_manifest)
    assert packaging["quality_policy"] == "core_quality_preserved_package_mesh_not_revalidated"
    packaged_keys = [obj["object_key"] for obj in packaging["packaged_objects"]]
    assert packaged_keys == ["__white_base__", "cyan", "__white_cap__"]

    coalescence = packaging["color_quarantine_coalescence"]
    assert coalescence["coalesced_object_count"] == 1
    assert coalescence["absorbed_quarantine_object_keys"] == ["cyan__topology_quarantine__"]
    assert coalescence["package_build_ms"] >= 0.0


def test_export_failure_removes_partial_stage_and_preserves_unrelated_output(
    export_env,
    monkeypatch,
):
    output_root = export_env.tmp_path / "output"
    unrelated = output_root / "user-note.txt"
    unrelated.write_text("keep", encoding="utf-8")

    def fail_after_partial_write(*, out_dir, **kwargs):
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "partial.stl").write_bytes(b"partial")
        raise RuntimeError("injected mesh failure")

    monkeypatch.setattr(server, "export_solve_bundle", fail_after_partial_write)
    response = export_env.client.post(
        "/api/export/files",
        json={"output_format": "stls", "card_id": "card"},
    )

    assert response.status_code == 500
    assert "injected mesh failure" in response.text
    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert sorted(path.name for path in output_root.iterdir()) == ["user-note.txt"]


def test_export_cancellation_before_publication_discards_complete_stage(export_env):
    def cancel_at_publication(event):
        if event.stage_id == "publish_outputs":
            raise server.ExportCancelled()

    with pytest.raises(server.ExportCancelled):
        server._perform_export_files(
            server.ExportFilesPayload(output_format="3mf", card_id="card"),
            progress_callback=cancel_at_publication,
        )

    output_root = export_env.tmp_path / "output"
    assert list(output_root.iterdir()) == []


def test_export_cancel_check_escapes_from_mesh_work_and_discards_partial_stage(
    export_env,
    monkeypatch,
):
    armed = False

    def cancel_when_armed():
        if armed:
            raise server.ExportCancelled()

    def cancel_inside_mesh(*, out_dir, cancel_check, **kwargs):
        nonlocal armed
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "partial.mesh").write_bytes(b"partial")
        armed = True
        cancel_check()

    monkeypatch.setattr(server, "export_solve_bundle", cancel_inside_mesh)

    with pytest.raises(server.ExportCancelled):
        server._perform_export_files(
            server.ExportFilesPayload(output_format="3mf", card_id="card"),
            cancel_check=cancel_when_armed,
        )

    assert list((export_env.tmp_path / "output").iterdir()) == []


def test_export_option_validation_does_not_create_a_private_stage(
    export_env,
    monkeypatch,
):
    def reject_geometry(_value):
        raise server.ExportPreparationError("invalid geometry for test")

    monkeypatch.setattr(server, "normalize_geometry_source", reject_geometry)

    with pytest.raises(server.ExportPreparationError, match="invalid geometry for test"):
        server._perform_export_files(
            server.ExportFilesPayload(output_format="3mf", card_id="card")
        )

    assert list((export_env.tmp_path / "output").iterdir()) == []


def _wait_for_export_status(client, expected: set[str], timeout: float = 3.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = client.get("/api/export/files/status")
        assert response.status_code == 200
        payload = response.json()
        if payload["status"] in expected:
            return payload
        time.sleep(0.01)
    raise AssertionError(f"export did not reach {sorted(expected)}")


def test_background_export_cancel_is_job_scoped_idempotent_and_terminal(
    export_env,
    monkeypatch,
):
    entered = threading.Event()
    allow_check = threading.Event()

    def blocked_export(_payload, *, progress_callback=None, cancel_check=None):
        entered.set()
        assert allow_check.wait(3.0)
        cancel_check()
        pytest.fail("cancel check should have raised")

    monkeypatch.setattr(server, "_perform_export_files", blocked_export)
    started = export_env.client.post(
        "/api/export/files/start",
        json={"output_format": "3mf", "card_id": "card"},
    )
    assert started.status_code == 200
    job_id = started.json()["job_id"]
    assert entered.wait(3.0)

    mismatch = export_env.client.post("/api/export/files/cancel?job_id=not-this-job")
    assert mismatch.status_code == 409
    first = export_env.client.post(f"/api/export/files/cancel?job_id={job_id}")
    second = export_env.client.post(f"/api/export/files/cancel?job_id={job_id}")
    assert first.json()["status"] == "cancelling"
    assert second.json()["status"] == "cancelling"
    status = export_env.client.get("/api/export/files/status").json()
    assert status["status"] == "cancelling"
    assert status["cancel_requested"] is True
    conflict = export_env.client.post(
        "/api/export/files/start",
        json={"output_format": "3mf", "card_id": "card"},
    )
    assert conflict.status_code == 409

    allow_check.set()
    terminal = _wait_for_export_status(export_env.client, {"cancelled"})
    assert terminal["job_id"] == job_id
    assert terminal["result"] is None
    after_terminal = export_env.client.post(f"/api/export/files/cancel?job_id={job_id}")
    assert after_terminal.json()["status"] == "cancelled"
    assert after_terminal.json()["cancelled"] is True


def test_export_completed_after_publication_reports_late_cancel_truthfully(
    export_env,
    monkeypatch,
):
    published = threading.Event()
    allow_return = threading.Event()
    completed_result = {"out_dir": "published"}

    def published_export(_payload, *, progress_callback=None, cancel_check=None):
        # Represents the interval after the final directory rename: cancellation
        # is no longer allowed to revoke a complete user export.
        published.set()
        assert allow_return.wait(3.0)
        return completed_result

    monkeypatch.setattr(server, "_perform_export_files", published_export)
    started = export_env.client.post(
        "/api/export/files/start",
        json={"output_format": "3mf", "card_id": "card"},
    )
    job_id = started.json()["job_id"]
    assert published.wait(3.0)
    requested = export_env.client.post(f"/api/export/files/cancel?job_id={job_id}")
    assert requested.json()["status"] == "cancelling"
    allow_return.set()

    terminal = _wait_for_export_status(export_env.client, {"complete"})
    assert terminal["result"] == completed_result
    assert terminal["progress"] == "Completed before cancellation took effect"


def test_export_publication_collision_preserves_existing_destination(
    export_env,
    monkeypatch,
):
    import run_naming

    output_root = export_env.tmp_path / "output"
    monkeypatch.setattr(run_naming, "make_export_id", lambda *args, **kwargs: "fixed-export")

    def create_collision_at_publication(event):
        if event.stage_id != "publish_outputs":
            return
        destination = output_root / "fixed-export"
        destination.mkdir()
        (destination / "owned.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(server.HTTPException, match="Export failed"):
        server._perform_export_files(
            server.ExportFilesPayload(output_format="3mf", card_id="card"),
            progress_callback=create_collision_at_publication,
        )

    destination = output_root / "fixed-export"
    assert (destination / "owned.txt").read_text(encoding="utf-8") == "keep"
    assert sorted(path.name for path in output_root.iterdir()) == ["fixed-export"]


def test_stl_export_does_not_add_3mf_packaging_manifest(export_env):
    env = export_env
    resp = env.client.post(
        "/api/export/files", json={"output_format": "stls", "card_id": "card"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert "threemf_bundle" not in env.captured
    assert "format_packaging" not in body["manifest"]
    on_disk_manifest = json.loads(
        (Path(body["out_dir"]) / "export_manifest.json").read_text(encoding="utf-8")
    )
    assert "format_packaging" not in on_disk_manifest
