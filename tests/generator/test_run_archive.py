import io
import zipfile

import numpy as np
import pytest

from run_archive import (
    SCHEMA_VERSION, ArchiveError, pack_run_archive, read_run_archive,
)


def _inputs():
    return dict(
        run_json={"schema_version": SCHEMA_VERSION, "save_id": "s1", "label": "L",
                  "saved_at": "20260616-101500", "source_image_name": "steve.jpg",
                  "config": {"image_path": "steve.jpg", "d_wb": 0.2, "layer_height": 0.08},
                  "palette": ["a", "b"], "image_domain_width_mm": 100.0,
                  "image_domain_height_mm": 80.0, "stats": {"mean_de": 1.2}},
        thickness_arrays={"tm__white_cap": np.zeros((4, 4), np.float32),
                          "tm__a": np.ones((4, 4), np.float32),
                          "dbg__de_map": np.full((4, 4), 0.5, np.float32)},
        image_bytes=b"\x89PNG\r\n\x1a\n-fake-but-bytes",
        image_name="steve.jpg",
        solve_state={"solve_owned_fingerprint": "abc123", "profiles": None},
        run_cache_files={"predicted.png": b"png-a", "cap_map_contour.bin": b"\x00\x01",
                         "post_solve_export_bundle/arrays.npz": b"npz-bytes"},
    )


def test_pack_then_read_roundtrips(tmp_path):
    data = pack_run_archive(**_inputs())
    parsed = read_run_archive(data)
    assert parsed.run_json["save_id"] == "s1"
    assert parsed.image_name == "steve.jpg" and parsed.image_bytes.startswith(b"\x89PNG")
    assert np.array_equal(parsed.thickness_arrays["tm__a"], np.ones((4, 4), np.float32))
    assert np.array_equal(parsed.thickness_arrays["dbg__de_map"], np.full((4, 4), 0.5, np.float32))
    assert parsed.solve_state["solve_owned_fingerprint"] == "abc123"
    # The whole run-cache subtree round-trips, including nested bundle files.
    assert set(parsed.run_cache_files) == {"predicted.png", "cap_map_contour.bin",
                                           "post_solve_export_bundle/arrays.npz"}
    assert parsed.run_cache_files["post_solve_export_bundle/arrays.npz"] == b"npz-bytes"


def test_read_rejects_duplicate_member_names():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("run.json", b'{"schema_version": 1}')
        zf.writestr("thickness_maps.npz", b"a")
        zf.writestr("run_cache/x.png", b"1")
        zf.writestr("run_cache/x.png", b"2")  # duplicate
    with pytest.raises(ArchiveError):
        read_run_archive(buf.getvalue())


def test_read_rejects_nested_traversal_under_run_cache():
    data = _zip_with({"run.json": b'{"schema_version": 1}',
                      "thickness_maps.npz": b"a",
                      "run_cache/../../escape.png": b"x"})
    with pytest.raises(ArchiveError):
        read_run_archive(data)


def test_read_enforces_uncompressed_ceiling(monkeypatch):
    import run_archive
    monkeypatch.setattr(run_archive, "MAX_TOTAL_UNCOMPRESSED_BYTES", 1024)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("run.json", b'{"schema_version": 1}')
        zf.writestr("thickness_maps.npz", b"\0" * 4096)  # 4 KB uncompressed > 1 KB ceiling
    with pytest.raises(ArchiveError):
        run_archive.read_run_archive(buf.getvalue())


def test_pack_enforces_uncompressed_ceiling(monkeypatch):
    # Save must not be able to produce an archive the loader would later reject.
    import run_archive
    monkeypatch.setattr(run_archive, "MAX_TOTAL_UNCOMPRESSED_BYTES", 1024)
    inp = _inputs()
    inp["run_cache_files"] = {"big.bin": b"\0" * 4096}  # blows the (lowered) ceiling
    with pytest.raises(ArchiveError):
        run_archive.pack_run_archive(**inp)


@pytest.mark.parametrize("bad", ["run_cache/.", "run_cache/..", "run_cache/../x.png"])
def test_safe_relpath_rejects_dot_members(bad):
    data = _zip_with({"run.json": b'{"schema_version": 1}', "thickness_maps.npz": b"a", bad: b""})
    with pytest.raises(ArchiveError):
        read_run_archive(data)


def test_read_rejects_unknown_schema_version(tmp_path):
    inp = _inputs(); inp["run_json"]["schema_version"] = 999
    with pytest.raises(ArchiveError):
        read_run_archive(pack_run_archive(**inp))


def test_read_rejects_non_zip():
    with pytest.raises(ArchiveError):
        read_run_archive(b"not a zip at all")


def _zip_with(members):  # members: dict name->bytes
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, b in members.items():
            zf.writestr(name, b)
    return buf.getvalue()


def test_read_rejects_member_outside_allowlist():
    data = _zip_with({"run.json": b'{"schema_version": 1}', "evil.sh": b"rm -rf"})
    with pytest.raises(ArchiveError):
        read_run_archive(data)


def test_read_rejects_empty_image_member():
    # pack rejects empty image bytes; read must too, or Load writes a 0-byte source image.
    npz = io.BytesIO()
    np.savez_compressed(npz, tm__a=np.zeros((2, 2), np.float32))
    data = _zip_with({"run.json": b'{"schema_version": 1}',
                      "thickness_maps.npz": npz.getvalue(),
                      "image/steve.jpg": b""})
    with pytest.raises(ArchiveError):
        read_run_archive(data)


@pytest.mark.parametrize("bad", ["../escape.json", "/abs/run.json", "C:/abs/run.json", "diag/../../x.png"])
def test_read_rejects_traversal_member_names(bad):
    data = _zip_with({"run.json": b'{"schema_version": 1}', bad: b"x"})
    with pytest.raises(ArchiveError):
        read_run_archive(data)


def test_read_rejects_too_many_members():
    members = {f"run_cache/d{i}.png": b"x" for i in range(5000)}
    members["run.json"] = b'{"schema_version": 1}'
    members["thickness_maps.npz"] = b"a"
    with pytest.raises(ArchiveError):
        read_run_archive(_zip_with(members))
