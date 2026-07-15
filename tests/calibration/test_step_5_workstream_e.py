"""
test_step_5_workstream_e.py — Step 5 Workstream E (doc 33 §6): the photo-stack
runtime-bundle fingerprint must be reproducible run-to-run on identical inputs.

The bundle embeds wall-clock fit timings (model.fit_info.fit_runtime_stages /
full_fit_runtime_stages, each a list of {stage, seconds}). Those must be excluded
from the fingerprint hash input — but kept in the bundle for diagnostics — so two
no-change fits on identical evidence produce an identical fingerprint
(task_8b571864). The export timestamp (exported_at_unix) is likewise excluded.

Run: python -m pytest tests/calibration/test_step_5_workstream_e.py -q
"""
from __future__ import annotations

import copy

from fitting.photo_stack_model.live_fit import _fingerprint_payload, _sha256_payload


def _bundle(*, floor=0.5, fit_seconds=1.0, full_seconds=2.0, ts=1000.0) -> dict:
    return {
        "schema": "photo_stack_bundle",
        "exported_at_unix": ts,
        "model": {
            "floor": floor,
            "curves": {"bambu-cyan": [{"thickness": 0.16, "od": 0.42}]},
            "fit_info": {
                "candidate_count": 3,
                "high_extrapolation_taper_mm": 1.0,
                "fit_runtime_stages": [
                    {"stage": "curves", "seconds": fit_seconds},
                    {"stage": "interaction", "seconds": fit_seconds * 2},
                ],
                "full_fit_runtime_stages": [
                    {"stage": "full", "seconds": full_seconds},
                ],
            },
        },
        "source": {"evidence_rows": 100},
    }


class TestFingerprintPayload:
    def test_payload_drops_timestamp_and_timing_stages(self):
        payload = _fingerprint_payload(_bundle())
        assert "exported_at_unix" not in payload
        fi = payload["model"]["fit_info"]
        assert "fit_runtime_stages" not in fi
        assert "full_fit_runtime_stages" not in fi
        # Non-timing fit_info survives, as does the rest of the bundle.
        assert fi["candidate_count"] == 3
        assert fi["high_extrapolation_taper_mm"] == 1.0
        assert payload["model"]["floor"] == 0.5
        assert payload["source"]["evidence_rows"] == 100

    def test_does_not_mutate_the_bundle(self):
        bundle = _bundle()
        original = copy.deepcopy(bundle)
        _fingerprint_payload(bundle)
        # Timings stay in the bundle for diagnostics — the strip is non-destructive.
        assert bundle == original


class TestFingerprintReproducibility:
    def test_fingerprint_ignores_wall_clock_timings(self):
        # Same model + evidence, different wall-clock timings and export time.
        a = _bundle(fit_seconds=1.0, full_seconds=2.0, ts=1000.0)
        b = _bundle(fit_seconds=37.5, full_seconds=99.9, ts=2000.0)
        fa = _sha256_payload(_fingerprint_payload(a))
        fb = _sha256_payload(_fingerprint_payload(b))
        assert fa == fb

    def test_fingerprint_still_reflects_real_model_changes(self):
        # A genuine model difference must change the fingerprint (no over-stripping).
        a = _bundle(floor=0.5)
        b = _bundle(floor=0.6)
        fa = _sha256_payload(_fingerprint_payload(a))
        fb = _sha256_payload(_fingerprint_payload(b))
        assert fa != fb
