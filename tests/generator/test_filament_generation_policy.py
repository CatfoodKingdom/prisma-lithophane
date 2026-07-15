"""
Generator-side generation-availability policy (contract bridge for
exclude_from_model). Per .tmp/exclude_generator_review/RECOMMENDATION.md:
an excluded filament must not silently flow into a NEW solve / palette validation
just because an old profile file remains on disk; existing data is preserved.

Run: python -m pytest tests/generator/test_filament_generation_policy.py -q
"""
from __future__ import annotations

import json

import pytest

from filament_policy import (
    FilamentUnavailableError,
    excluded_filament_ids,
    is_generation_available,
    unavailable_for_generation,
)

_REG = {
    "bambu-cyan": {"manufacturer": "Bambu", "white_cap_eligible": False},
    "bambu-tough-white": {"manufacturer": "Bambu", "white_cap_eligible": True},
    "bambu-translucent-orange": {"exclude_from_model": True},
    "panchroma-translucent-natural": {"exclude_from_model": True},
}


class TestPolicyHelper:
    def test_is_generation_available(self):
        assert is_generation_available("bambu-cyan", _REG) is True
        assert is_generation_available("bambu-translucent-orange", _REG) is False
        # unknown filament defaults available (no policy known)
        assert is_generation_available("who-knows", _REG) is True

    def test_excluded_filament_ids(self):
        assert excluded_filament_ids(_REG) == {
            "bambu-translucent-orange", "panchroma-translucent-natural"}

    def test_unavailable_for_generation_orders_and_dedups(self):
        ids = ["bambu-cyan", "bambu-translucent-orange", "bambu-cyan",
               "panchroma-translucent-natural"]
        assert unavailable_for_generation(ids, _REG) == [
            "bambu-translucent-orange", "panchroma-translucent-natural"]

    def test_clean_palette_has_no_unavailable(self):
        assert unavailable_for_generation(["bambu-cyan"], _REG) == []


class TestRegistryResolution:
    def test_default_registry_uses_authoritative_sqlite_catalog(self, monkeypatch):
        import filament_order

        monkeypatch.setattr(
            filament_order,
            "current_filament_catalog",
            lambda _root: (True, _REG),
        )

        assert filament_order.load_filament_order_registry() == _REG

    def test_explicit_registry_path_uses_json_export(self, monkeypatch, tmp_path):
        import filament_order

        path = tmp_path / "registry.json"
        path.write_text(json.dumps(_REG), encoding="utf-8")
        monkeypatch.setattr(
            filament_order,
            "current_filament_catalog",
            lambda _root: (_ for _ in ()).throw(AssertionError("SQLite should not be queried")),
        )

        assert filament_order.load_filament_order_registry(path) == _REG


class TestValidatePalettePolicy:
    @staticmethod
    def _profiles(tmp_path, *fids):
        for fid in fids:
            (tmp_path / f"{fid}.json").write_text("{}", encoding="utf-8")
        return tmp_path

    def test_clean_palette_passes(self, tmp_path):
        from pipeline_cli import validate_palette
        pdir = self._profiles(tmp_path, "bambu-cyan")
        validate_palette(["bambu-cyan"], profiles_dir=pdir, registry=_REG)  # no raise

    def test_excluded_filament_refused(self, tmp_path):
        from pipeline_cli import validate_palette
        # orange HAS a profile file (the stale one) — so it's not "missing",
        # it must be refused on POLICY.
        pdir = self._profiles(tmp_path, "bambu-cyan", "bambu-translucent-orange")
        with pytest.raises(FilamentUnavailableError) as exc:
            validate_palette(["bambu-cyan", "bambu-translucent-orange"],
                             profiles_dir=pdir, registry=_REG)
        assert "bambu-translucent-orange" in exc.value.unavailable

    def test_missing_profile_is_distinct_from_policy(self, tmp_path):
        from pipeline_cli import validate_palette
        pdir = self._profiles(tmp_path, "bambu-cyan")
        with pytest.raises(ValueError) as exc:
            validate_palette(["does-not-exist"], profiles_dir=pdir, registry=_REG)
        # a plain missing-profile error, NOT the policy subclass
        assert not isinstance(exc.value, FilamentUnavailableError)

    def test_white_base_or_cap_excluded_is_refused(self, tmp_path):
        # runner.py validates palette + white base + cap through this one call.
        from pipeline_cli import validate_palette
        pdir = self._profiles(tmp_path, "bambu-cyan", "panchroma-translucent-natural")
        with pytest.raises(FilamentUnavailableError):
            validate_palette(["bambu-cyan", "panchroma-translucent-natural"],
                             profiles_dir=pdir, registry=_REG)


class TestEndpoints:
    def _client(self, monkeypatch, tmp_path, registry):
        import server
        from fastapi.testclient import TestClient
        monkeypatch.setattr(server, "_load_registry", lambda: registry)
        monkeypatch.setattr(server, "_PROFILES_DIR", tmp_path)
        return TestClient(server.app)

    def test_list_filaments_emits_policy(self, monkeypatch, tmp_path):
        (tmp_path / "bambu-tough-white.json").write_text("{}", encoding="utf-8")
        client = self._client(monkeypatch, tmp_path, _REG)
        rows = {r["filament_id"]: r for r in client.get("/api/filaments").json()}
        assert rows["bambu-translucent-orange"]["exclude_from_model"] is True
        assert rows["bambu-translucent-orange"]["generation_available"] is False
        assert rows["bambu-cyan"]["exclude_from_model"] is False
        assert rows["bambu-cyan"]["generation_available"] is True
        assert rows["bambu-cyan"]["white_cap_eligible"] is False
        assert rows["bambu-tough-white"]["white_cap_eligible"] is True
        assert rows["bambu-tough-white"]["has_profile"] is True

    def test_validate_endpoint_reports_unavailable(self, monkeypatch, tmp_path):
        # both have profiles on disk → not "missing" → isolates the policy check.
        (tmp_path / "bambu-cyan.json").write_text("{}", encoding="utf-8")
        (tmp_path / "bambu-translucent-orange.json").write_text("{}", encoding="utf-8")
        client = self._client(monkeypatch, tmp_path, _REG)
        r = client.post("/api/palette/validate",
                        json={"palette": ["bambu-cyan", "bambu-translucent-orange"]}).json()
        assert r["valid"] is False
        assert r["unavailable"] == ["bambu-translucent-orange"]
        assert r["missing"] == []
        r2 = client.post("/api/palette/validate", json={"palette": ["bambu-cyan"]}).json()
        assert r2["valid"] is True

    def test_saved_palettes_live_under_generator_data_root(self):
        import server
        assert server._PALETTES_PATH == server._GENERATOR_DATA_DIR / "palettes.json"
