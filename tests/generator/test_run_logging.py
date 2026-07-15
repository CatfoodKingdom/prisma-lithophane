"""Tests for the retired run-logging feature and the surviving run.json writer."""
import json
from pathlib import Path

import pytest

import server


def _make_run_json_data():
    """Return a minimal run.json-shaped dict for schema validation."""
    return {
        "timestamp": "2026-04-04T14:30:00",
        "image": "test.jpg",
        "palette": ["bambu-red", "bambu-blue"],
        "profile_ref": {"kind": "named", "id": "profile-123", "name": "Portrait Warm"},
        "profile_name_at_solve": "Portrait Warm",
        "is_profile_modified_at_solve": False,
        "recipe_snapshot": {
            "palette": ["bambu-red", "bambu-blue"],
            "profile_ref": {"kind": "named", "id": "profile-123", "name": "Portrait Warm"},
            "profile_snapshot": {
                "name": "Portrait Warm",
                "settings": {},
                "modules": {},
            },
        },
        "config": {"run_logging": True, "palette": ["bambu-red", "bambu-blue"]},
        "stats": {
            "mean_de": 0.03,
            "max_de": 0.18,
            "n_oog": 0,
            "total_pixels": 100,
            "coverage_pct": 100.0,
            "image_w": 10,
            "image_h": 10,
            "max_height": 2.5,
            "per_filament": [],
        },
    }


class TestRunJsonSchema:
    """Validate run.json structure."""

    def test_required_keys_present(self):
        data = _make_run_json_data()
        for key in (
            "timestamp",
            "image",
            "palette",
            "profile_ref",
            "profile_name_at_solve",
            "is_profile_modified_at_solve",
            "recipe_snapshot",
            "config",
            "stats",
        ):
            assert key in data

    def test_stats_required_keys(self):
        stats = _make_run_json_data()["stats"]
        for key in ("mean_de", "max_de", "n_oog", "total_pixels",
                     "coverage_pct", "image_w", "image_h", "max_height"):
            assert key in stats

    def test_palette_is_list_of_strings(self):
        data = _make_run_json_data()
        assert isinstance(data["palette"], list)
        assert all(isinstance(p, str) for p in data["palette"])

    def test_config_is_dict(self):
        data = _make_run_json_data()
        assert isinstance(data["config"], dict)

    def test_recipe_snapshot_tracks_palette_and_profile(self):
        data = _make_run_json_data()
        recipe = data["recipe_snapshot"]
        assert recipe["palette"] == data["palette"]
        assert recipe["profile_ref"]["id"] == data["profile_ref"]["id"]
        assert recipe["profile_snapshot"]["name"] == data["profile_name_at_solve"]

    def test_roundtrip_json(self, tmp_path):
        data = _make_run_json_data()
        path = tmp_path / "run.json"
        path.write_text(json.dumps(data, indent=2))
        loaded = json.loads(path.read_text())
        assert loaded == data


class TestRunJsonWriter:
    """The always-on per-solve run.json writer survives run_logging removal."""

    def test_writes_run_json(self, tmp_path):
        from server import _write_run_json
        data = _make_run_json_data()
        _write_run_json(tmp_path, data)
        path = tmp_path / "run.json"
        assert path.exists()
        loaded = json.loads(path.read_text())
        assert loaded["timestamp"] == data["timestamp"]
        assert loaded["palette"] == data["palette"]
        assert loaded["profile_ref"] == data["profile_ref"]
        assert loaded["recipe_snapshot"] == data["recipe_snapshot"]


def test_run_logging_field_is_quietly_ignored_by_config_payload():
    payload = server.ConfigPayload(run_logging=True)
    assert "run_logging" not in server._payload_model_fields(payload)


def test_run_logging_not_in_default_config():
    assert "run_logging" not in server._DEFAULT_CONFIG


def test_run_logging_write_only_snapshot_path_removed():
    source = Path(server.__file__).read_text(encoding="utf-8")
    assert "_create_run_dir" not in source
    assert "cfg.get(\"run_logging\")" not in source
    assert "RUN_LOGS_DIR" not in source
