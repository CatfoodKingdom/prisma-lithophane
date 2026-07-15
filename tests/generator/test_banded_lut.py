"""Library-level contract tests for exact-height, white-filled spline LUT bands."""

from __future__ import annotations

import hashlib
import numpy as np
import pytest

import lut as lut_module
from lut import build_banded_luts, build_luts, query_luts_batch
from model import predict_transmission, to_oklab


LAYER_HEIGHT = 0.1


def _profile(r: float, g: float, b: float) -> dict:
    """A small monotone spline profile with known values at one layer."""
    return {
        "knots_mm": [0.0, LAYER_HEIGHT, 2 * LAYER_HEIGHT, 3 * LAYER_HEIGHT],
        "T_r": [1.0, r, r * r, r * r * r],
        "T_g": [1.0, g, g * g, g * g * g],
        "T_b": [1.0, b, b * b, b * b * b],
    }


def _inputs() -> tuple[dict, dict, dict]:
    white = _profile(0.91, 0.90, 0.89)
    colors = {
        "dark": _profile(0.53, 0.72, 0.83),
        "light": _profile(0.79, 0.57, 0.46),
    }
    return colors, white, white


def _build(*, groups=None, band_layers=None, corrections=None, use_cache=False):
    colors, wb, wc = _inputs()
    return build_banded_luts(
        colors,
        wb_profile=wb,
        wc_profile=wc,
        groups=groups or [["dark"], ["light"]],
        band_layers=band_layers or [1, 1],
        layer_height=LAYER_HEIGHT,
        max_layers=2,
        d_wb=LAYER_HEIGHT,
        d_wc_min=LAYER_HEIGHT,
        d_wc_max=LAYER_HEIGHT,
        t_max=3 * LAYER_HEIGHT,
        corrections=corrections,
        use_cache=use_cache,
        verbose=False,
    )[0]


def _row(entry, color_layers: tuple[int, ...], fill_layers: tuple[int, ...]) -> int:
    matches = np.flatnonzero(
        np.all(entry.band_color_layers == np.asarray(color_layers), axis=1)
        & np.all(entry.band_fill_layers == np.asarray(fill_layers), axis=1)
    )
    assert len(matches) == 1
    return int(matches[0])


def test_banded_lut_records_exact_band_geometry_and_pure_fill_choice() -> None:
    entry = _build()

    assert entry.filaments == ("dark", "light")
    assert entry.band_groups == (("dark",), ("light",))
    assert entry.band_layer_budgets == (1, 1)
    np.testing.assert_array_equal(
        entry.band_color_layers[:, 0] + entry.band_fill_layers[:, 0], 1,
    )
    np.testing.assert_array_equal(
        entry.band_color_layers[:, 1] + entry.band_fill_layers[:, 1], 1,
    )
    _row(entry, (0, 0), (1, 1))  # Empty subsets are pure white fill.


def test_banded_lut_prices_white_fill_in_linear_transmission() -> None:
    colors, wb, wc = _inputs()
    entry = _build()
    index = _row(entry, (1, 0), (0, 1))

    expected_T = (
        predict_transmission(wb, LAYER_HEIGHT)
        * predict_transmission(colors["dark"], LAYER_HEIGHT)
        * predict_transmission(wc, LAYER_HEIGHT)  # band 2 fill
        * predict_transmission(wc, LAYER_HEIGHT)  # cap
    )
    np.testing.assert_allclose(entry.oklab[index], to_oklab(expected_T), atol=1e-6)


def test_banded_lut_tiny_cartesian_product_is_hand_enumerable() -> None:
    entry = _build()
    actual = {
        (tuple(color), tuple(fill))
        for color, fill in zip(entry.band_color_layers, entry.band_fill_layers)
    }
    assert actual == {
        ((0, 0), (1, 1)),
        ((1, 0), (0, 1)),
        ((0, 1), (1, 0)),
        ((1, 1), (0, 0)),
    }


def test_banded_lut_uses_the_existing_query_entry_layout() -> None:
    entry = _build()
    result, de = query_luts_batch([entry], entry.oklab, parallel=False)
    np.testing.assert_allclose(de, 0.0, atol=1e-7)
    np.testing.assert_allclose(result["dark"], entry.thicknesses[:, 0])
    np.testing.assert_allclose(result["light"], entry.thicknesses[:, 1])
    np.testing.assert_allclose(result["__white_cap__"], entry.cap_thicknesses)


def test_banded_lut_cap_range_respects_fixed_band_height_budget() -> None:
    colors, wb, wc = _inputs()
    entry = build_banded_luts(
        colors,
        wb_profile=wb,
        wc_profile=wc,
        groups=[["dark"], ["light"]],
        band_layers=[1, 1],
        layer_height=LAYER_HEIGHT,
        d_wb=LAYER_HEIGHT,
        d_wc_min=LAYER_HEIGHT,
        d_wc_max=2 * LAYER_HEIGHT,
        t_max=3 * LAYER_HEIGHT,
        use_cache=False,
        verbose=False,
    )[0]
    # Two fixed color bands consume 0.20 mm, leaving exactly one cap layer.
    np.testing.assert_allclose(entry.cap_thicknesses, LAYER_HEIGHT, atol=1e-7)


def _pair_corrections() -> dict:
    return {
        "dark-on-light": {
            "overlay": "dark",
            "bases": [["light", LAYER_HEIGHT]],
            "knots_mm": [0.0, LAYER_HEIGHT, 2 * LAYER_HEIGHT],
            "C_r": [1.0, 0.81, 0.81],
            "C_g": [1.0, 0.82, 0.82],
            "C_b": [1.0, 0.83, 0.83],
        }
    }


def _unbanded_pair_row(corrections: dict) -> np.ndarray:
    colors, wb, wc = _inputs()
    luts = build_luts(
        colors,
        wb_profile=wb,
        wc_profile=wc,
        layer_height=LAYER_HEIGHT,
        max_layers=2,
        d_wb=LAYER_HEIGHT,
        d_wc_min=LAYER_HEIGHT,
        d_wc_max=LAYER_HEIGHT,
        k_max=2,
        t_max=3 * LAYER_HEIGHT,
        corrections=corrections,
        use_cache=False,
        verbose=False,
    )
    entry = next(lut for lut in luts if lut.filaments == ("dark", "light"))
    index = np.flatnonzero(
        np.all(np.isclose(entry.thicknesses, LAYER_HEIGHT), axis=1)
        & np.isclose(entry.cap_thicknesses, LAYER_HEIGHT)
    )
    assert len(index) == 1
    return entry.oklab[index[0]]


def test_banded_two_color_corrections_match_unbanded_for_cross_and_same_band() -> None:
    corrections = _pair_corrections()
    expected = _unbanded_pair_row(corrections)

    cross_band = _build(corrections=corrections)
    same_band = _build(
        groups=[["dark", "light"]], band_layers=[2], corrections=corrections,
    )
    np.testing.assert_allclose(
        cross_band.oklab[_row(cross_band, (1, 1), (0, 0))], expected, atol=1e-6,
    )
    np.testing.assert_allclose(
        same_band.oklab[_row(same_band, (1, 1), (0,))], expected, atol=1e-6,
    )


def test_three_color_banded_choice_does_not_receive_pair_correction() -> None:
    colors, wb, wc = _inputs()
    colors["third"] = _profile(0.67, 0.61, 0.75)
    common = dict(
        wb_profile=wb,
        wc_profile=wc,
        groups=[["dark"], ["light"], ["third"]],
        band_layers=[1, 1, 1],
        layer_height=LAYER_HEIGHT,
        d_wb=LAYER_HEIGHT,
        d_wc_min=LAYER_HEIGHT,
        d_wc_max=LAYER_HEIGHT,
        use_cache=False,
        verbose=False,
    )
    corrected = build_banded_luts(colors, corrections=_pair_corrections(), **common)[0]
    plain = build_banded_luts(colors, corrections=None, **common)[0]
    corrected_index = _row(corrected, (1, 1, 1), (0, 0, 0))
    plain_index = _row(plain, (1, 1, 1), (0, 0, 0))
    np.testing.assert_allclose(
        corrected.oklab[corrected_index], plain.oklab[plain_index], atol=1e-6,
    )


def test_full_band_unbanded_combinations_embed_without_fill() -> None:
    """Partial unbanded combinations need spacer; exact full bands must not change."""
    colors, wb, wc = _inputs()
    unbanded = build_luts(
        colors,
        wb_profile=wb,
        wc_profile=wc,
        layer_height=LAYER_HEIGHT,
        max_layers=2,
        d_wb=LAYER_HEIGHT,
        d_wc_min=LAYER_HEIGHT,
        d_wc_max=LAYER_HEIGHT,
        k_max=2,
        t_max=3 * LAYER_HEIGHT,
        use_cache=False,
        verbose=False,
    )
    banded = _build(groups=[["dark", "light"]], band_layers=[2])
    for lut in unbanded:
        full = np.isclose(lut.thicknesses.sum(axis=1), 2 * LAYER_HEIGHT)
        for thicknesses, oklab in zip(lut.thicknesses[full], lut.oklab[full]):
            padded = np.zeros(2, dtype=np.int32)
            for column, fid in enumerate(lut.filaments):
                padded[banded.filaments.index(fid)] = round(thicknesses[column] / LAYER_HEIGHT)
            index = _row(banded, tuple(padded), (0,))
            np.testing.assert_allclose(banded.oklab[index], oklab, atol=1e-6)


def test_banded_lut_is_deterministic_and_cache_round_trips(tmp_path, monkeypatch) -> None:
    first = _build()
    second = _build()
    np.testing.assert_array_equal(first.thicknesses, second.thicknesses)
    np.testing.assert_array_equal(first.oklab, second.oklab)
    np.testing.assert_array_equal(first.band_color_layers, second.band_color_layers)
    np.testing.assert_array_equal(first.band_fill_layers, second.band_fill_layers)

    monkeypatch.setattr(lut_module, "CACHE_DIR", tmp_path)
    cached_first = _build(use_cache=True)
    cached_second = _build(use_cache=True)
    np.testing.assert_array_equal(cached_first.oklab, cached_second.oklab)
    np.testing.assert_array_equal(cached_first.band_color_layers, cached_second.band_color_layers)
    np.testing.assert_array_equal(cached_first.band_fill_layers, cached_second.band_fill_layers)

    _build(groups=[["dark", "light"]], band_layers=[2], use_cache=True)
    assert len(list(tmp_path.glob("lut_banded_*.npz"))) == 2


def test_banded_spline_output_hash_is_frozen_across_provider_pricing_seam() -> None:
    entry = _build()
    digest = hashlib.sha256()
    for value in (
        entry.filaments,
        entry.thicknesses,
        entry.cap_thicknesses,
        entry.oklab,
        entry.band_color_layers,
        entry.band_fill_layers,
        entry.band_groups,
        entry.band_layer_budgets,
    ):
        if isinstance(value, np.ndarray):
            digest.update(str(value.dtype).encode())
            digest.update(str(value.shape).encode())
            digest.update(value.tobytes())
        else:
            digest.update(repr(value).encode())
    assert digest.hexdigest() == (
        "899ca890db43ef323414039a745b9f08ef0e830fd236b0d650dd40cf7b680914"
    )


def test_banded_lut_refuses_excessive_preprune_expansion(monkeypatch) -> None:
    monkeypatch.setattr(lut_module, "_BANDED_LUT_MAX_PREPRUNE_ENTRIES", 3)
    with pytest.raises(RuntimeError, match="projected pre-prune entry count"):
        _build()
