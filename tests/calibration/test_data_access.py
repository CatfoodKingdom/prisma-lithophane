"""
test_data_access.py — Unit tests for DataStore filament cache.

Uses pytest's tmp_path fixture so every test gets its own isolated
directory; no shared state between tests.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from data_access import DataStore


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_store(tmp_path: Path) -> DataStore:
    """Create a DataStore rooted at tmp_path with an empty registry."""
    (tmp_path / "filaments").mkdir(parents=True, exist_ok=True)
    reg = tmp_path / "filaments" / "registry.json"
    reg.write_text("{}", encoding="utf-8")
    return DataStore(tmp_path)


def _seed_registry(tmp_path: Path, filaments: dict) -> None:
    """Write a filament registry dict to disk."""
    (tmp_path / "filaments").mkdir(parents=True, exist_ok=True)
    reg = tmp_path / "filaments" / "registry.json"
    reg.write_text(json.dumps(filaments, indent=2), encoding="utf-8")


# ── tests ─────────────────────────────────────────────────────────────────────

class TestGetFilament:
    def test_returns_correct_filament(self, tmp_path):
        _seed_registry(tmp_path, {
            "bambu-cyan": {
                "display_name": "Bambu Cyan",
                "manufacturer": "Bambu",
                "color_name": "Cyan",
                "hex": "#00FFFF",
            },
            "bambu-red": {
                "display_name": "Bambu Red",
                "manufacturer": "Bambu",
                "color_name": "Red",
                "hex": "#FF0000",
            },
        })
        store = DataStore(tmp_path)

        fil = store.get_filament("bambu-cyan")

        assert fil is not None
        assert fil.filament_id == "bambu-cyan"
        assert fil.display_name == "Bambu Cyan"
        assert fil.hex == "#00FFFF"

    def test_returns_none_for_missing_filament(self, tmp_path):
        _seed_registry(tmp_path, {
            "bambu-cyan": {
                "display_name": "Bambu Cyan",
                "manufacturer": "Bambu",
                "color_name": "Cyan",
                "hex": "#00FFFF",
            },
        })
        store = DataStore(tmp_path)

        result = store.get_filament("does-not-exist")

        assert result is None

    def test_returns_none_when_registry_missing(self, tmp_path):
        # DataStore._ensure_dirs creates subdirs but registry.json itself
        # is not created automatically — test the missing-file path.
        store = DataStore(tmp_path)
        # registry.json was never written
        (tmp_path / "filaments" / "registry.json").unlink(missing_ok=True)

        result = store.get_filament("anything")

        assert result is None


class TestFilamentCache:
    def test_cache_is_used_on_second_call(self, tmp_path):
        """list_filaments called twice returns identical objects from cache."""
        _seed_registry(tmp_path, {
            "bambu-cyan": {
                "display_name": "Bambu Cyan",
                "manufacturer": "Bambu",
                "color_name": "Cyan",
                "hex": "#00FFFF",
            },
        })
        store = DataStore(tmp_path)

        first = store.list_filaments()
        second = store.list_filaments()

        # Same list object returned from cache
        assert first is second

    def test_cache_invalidated_after_add_filament(self, tmp_path):
        """After add_filament, list_filaments re-reads disk and returns new entry."""
        _seed_registry(tmp_path, {})
        store = DataStore(tmp_path)

        # Populate cache with empty list
        before = store.list_filaments()
        assert before == []

        store.add_filament(
            filament_id="bambu-magenta",
            display_name="Bambu Magenta",
            manufacturer="Bambu",
            color_name="Magenta",
            hex_color="#FF00FF",
        )

        after = store.list_filaments()
        assert len(after) == 1
        assert after[0].filament_id == "bambu-magenta"

    def test_cache_invalidated_after_delete_filament(self, tmp_path):
        """After delete_filament, list_filaments no longer returns deleted entry."""
        _seed_registry(tmp_path, {
            "bambu-cyan": {
                "display_name": "Bambu Cyan",
                "manufacturer": "Bambu",
                "color_name": "Cyan",
                "hex": "#00FFFF",
            },
        })
        store = DataStore(tmp_path)

        before = store.list_filaments()
        assert len(before) == 1

        store.delete_filament("bambu-cyan")

        after = store.list_filaments()
        assert after == []

    def test_cache_invalidated_after_update_filament(self, tmp_path):
        """After update_filament, list_filaments reflects the updated values."""
        _seed_registry(tmp_path, {
            "bambu-cyan": {
                "display_name": "Bambu Cyan",
                "manufacturer": "Bambu",
                "color_name": "Cyan",
                "hex": "#00FFFF",
            },
        })
        store = DataStore(tmp_path)

        before = store.get_filament("bambu-cyan")
        assert before.hex == "#00FFFF"

        store.update_filament("bambu-cyan", hex_color="#0099FF")

        after = store.get_filament("bambu-cyan")
        assert after.hex == "#0099FF"

    def test_cache_refreshed_when_mtime_changes(self, tmp_path):
        """If registry.json is written externally, next list_filaments re-reads it."""
        _seed_registry(tmp_path, {
            "bambu-cyan": {
                "display_name": "Bambu Cyan",
                "manufacturer": "Bambu",
                "color_name": "Cyan",
                "hex": "#00FFFF",
            },
        })
        store = DataStore(tmp_path)

        # Populate cache
        first = store.list_filaments()
        assert len(first) == 1

        # Simulate an external write by overwriting registry.json directly,
        # forcing a different mtime by touching the file after a small delay.
        import time
        time.sleep(0.01)  # ensure mtime differs
        new_registry = {
            "bambu-cyan": {
                "display_name": "Bambu Cyan",
                "manufacturer": "Bambu",
                "color_name": "Cyan",
                "hex": "#00FFFF",
            },
            "bambu-red": {
                "display_name": "Bambu Red",
                "manufacturer": "Bambu",
                "color_name": "Red",
                "hex": "#FF0000",
            },
        }
        reg_path = tmp_path / "filaments" / "registry.json"
        reg_path.write_text(json.dumps(new_registry, indent=2), encoding="utf-8")

        second = store.list_filaments()
        assert len(second) == 2


class TestFreshProfiles:
    def test_stale_profiles_are_hidden_from_normal_reads(self, tmp_path):
        _seed_registry(tmp_path, {
            "bambu-cyan": {
                "display_name": "Bambu Cyan",
                "manufacturer": "Bambu",
                "color_name": "Cyan",
                "hex": "#00FFFF",
            },
        })
        profiles_dir = tmp_path / "filaments" / "profiles"
        profiles_dir.mkdir(parents=True, exist_ok=True)
        (profiles_dir / "bambu-cyan.json").write_text(json.dumps({
            "filament_id": "bambu-cyan",
            "stale": True,
            "stale_reason": "source data changed",
        }), encoding="utf-8")
        store = DataStore(tmp_path)

        assert store.get_profile("bambu-cyan") is None
        assert store.get_profile("bambu-cyan", include_stale=True)["stale"] is True
        assert store.list_profiles() == []
        assert store.list_profiles(include_stale=True) == ["bambu-cyan"]
        assert store.get_filament("bambu-cyan").has_profile is False

    def test_flag_profile_stale_invalidates_profile_status_and_pair_corrections(self, tmp_path):
        _seed_registry(tmp_path, {
            "bambu-cyan": {
                "display_name": "Bambu Cyan",
                "manufacturer": "Bambu",
                "color_name": "Cyan",
                "hex": "#00FFFF",
            },
        })
        profiles_dir = tmp_path / "filaments" / "profiles"
        profiles_dir.mkdir(parents=True, exist_ok=True)
        (profiles_dir / "bambu-cyan.json").write_text(json.dumps({
            "filament_id": "bambu-cyan",
            "knots_mm": [0, 1],
            "T_r": [1, 0.5],
            "T_g": [1, 0.5],
            "T_b": [1, 0.5],
        }), encoding="utf-8")
        pair_corrections = tmp_path / "filaments" / "pair_corrections.json"
        pair_corrections.write_text("{}", encoding="utf-8")
        store = DataStore(tmp_path)

        assert store.get_filament("bambu-cyan").has_profile is True
        assert store.flag_profile_stale("bambu-cyan", "sample reassigned") is True

        assert store.get_profile("bambu-cyan") is None
        assert store.get_filament("bambu-cyan").has_profile is False
        assert not pair_corrections.exists()


class TestImageInboxMetadataCache:
    def test_list_images_allows_missing_inbox(self, tmp_path):
        data_root = tmp_path / "data"
        _seed_registry(data_root, {})
        store = DataStore(data_root)

        assert store.list_images() == []

    def test_list_images_reuses_cached_exif_metadata(self, tmp_path, monkeypatch):
        data_root = tmp_path / "data"
        _seed_registry(data_root, {})
        store = DataStore(data_root)
        store.inbox_dir.mkdir(parents=True, exist_ok=True)
        image_path = store.inbox_dir / "IMG_0001.CR2"
        image_path.write_bytes(b"raw-data")
        calls = []

        def fake_extract(path):
            calls.append(path.name)
            return f"timestamp-{len(calls)}"

        monkeypatch.setattr("processing.blank_registry._extract_exif_timestamp", fake_extract)

        first = store.list_images()
        second = store.list_images()

        assert calls == ["IMG_0001.CR2"]
        assert first[0]["exif_timestamp"] == "timestamp-1"
        assert second[0]["exif_timestamp"] == "timestamp-1"
        assert store.image_metadata_cache_path.exists()

        image_path.write_bytes(b"raw-data-updated")

        third = store.list_images()

        assert calls == ["IMG_0001.CR2", "IMG_0001.CR2"]
        assert third[0]["exif_timestamp"] == "timestamp-2"
