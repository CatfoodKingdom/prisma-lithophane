from run_naming import make_export_id

WINDOWS_RESERVED = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}

TS = "20260615-143022"  # a representative YYYYMMDD-HHMMSS export timestamp


def test_sanitizes_unsafe_characters(tmp_path):
    eid = make_export_id("C:/photos/My Photo!.JPG", tmp_path, timestamp=TS)
    assert eid == eid.lower()
    assert all(c.isalnum() or c in "-_" for c in eid)
    assert "/" not in eid and "\\" not in eid and " " not in eid
    assert eid  # never empty


def test_timestamp_leads_then_image(tmp_path):
    eid = make_export_id("steve.jpg", tmp_path, timestamp=TS)
    assert eid == "20260615-143022-steve"


def test_reserved_windows_name_escaped_when_timestamp_empty(tmp_path):
    # Defensive: _slugify still escapes a bare reserved name if the timestamp is empty.
    eid = make_export_id("nul", tmp_path, timestamp="")
    assert eid.lower() not in WINDOWS_RESERVED


def test_length_is_capped(tmp_path):
    eid = make_export_id("x" * 200, tmp_path, timestamp=TS)
    assert len(eid) <= 64


def test_collision_suffixing(tmp_path):
    # Two exports in the SAME second collide on the timestamp; suffix disambiguates.
    a = make_export_id("steve.jpg", tmp_path, timestamp=TS)
    (tmp_path / a).mkdir()
    b = make_export_id("steve.jpg", tmp_path, timestamp=TS)
    assert b != a and b.endswith("-2")
    (tmp_path / b).mkdir()
    c = make_export_id("steve.jpg", tmp_path, timestamp=TS)
    assert c.endswith("-3")


def test_empty_image_uses_timestamp_only(tmp_path):
    eid = make_export_id("", tmp_path, timestamp=TS)
    assert eid == "20260615-143022"


def test_both_empty_falls_back_to_export(tmp_path):
    eid = make_export_id("", tmp_path, timestamp="")
    assert eid.startswith("export")


def test_collision_suffix_stays_within_length_cap(tmp_path):
    # A maximal-length base must not exceed 64 chars once a collision suffix is added.
    a = make_export_id("x" * 200, tmp_path, timestamp=TS)
    (tmp_path / a).mkdir()
    b = make_export_id("x" * 200, tmp_path, timestamp=TS)
    assert b != a and len(b) <= 64 and b.endswith("-2")


from run_naming import make_save_id


def test_make_save_id_timestamp_plus_image_only(tmp_path):
    # Identity = timestamp + image stem ONLY (spec §3). The display label is NOT
    # part of the id, so renaming a save never changes its id.
    sid = make_save_id("C:/photos/Steve Photo.JPG", tmp_path, timestamp="20260616-101500")
    assert sid == sid.lower()
    assert all(c.isalnum() or c in "-_" for c in sid)
    assert sid.startswith("20260616-101500-") and "steve" in sid
    assert len(sid) <= 64


def test_make_save_id_collision_suffixes(tmp_path):
    a = make_save_id("steve.jpg", tmp_path, timestamp="20260616-101500")
    (tmp_path / f"{a}.zip").write_bytes(b"x")  # collide on the REAL artifact name, not a dir
    b = make_save_id("steve.jpg", tmp_path, timestamp="20260616-101500")
    assert b != a and b.endswith("-2")


def test_make_save_id_empty_inputs_fall_back(tmp_path):
    sid = make_save_id("", tmp_path, timestamp="")
    assert sid  # never empty
