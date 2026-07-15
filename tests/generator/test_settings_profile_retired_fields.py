from __future__ import annotations


def test_settings_profile_normalization_drops_retired_subject_fields():
    import server

    retired_subject = "protect" + "_subject"
    retired_mask = "protect" + "_mask"
    retired_keys = {
        f"{retired_subject}_enabled": True,
        f"{retired_subject}_strength": 0.5,
        "protect" + "_confidence_floor": 0.2,
        f"{retired_mask}_provider": "old-provider",
        f"{retired_mask}_override": {"strokes": []},
    }
    normalized = server._normalize_settings_profile_settings(
        {
            **retired_keys,
            "gamut_mode": "hull",
            "preprocessing_params": {},
        }
    )

    for key in retired_keys:
        assert key not in normalized
    assert normalized["gamut_mode"] == "hull"
    assert normalized["preprocessing_params"] == {}
