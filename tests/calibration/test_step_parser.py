"""Tests for unified_calibration.pipeline.step_parser."""
import pytest
from strips.step_parser import parse_step_filename


# ---------------------------------------------------------------------------
# Double-underscore format
# ---------------------------------------------------------------------------

class TestDoubleUnderscoreFormat:
    def test_1L_no_fixed(self):
        result = parse_step_filename(
            "1L__v-0.20-0.28-0.36-0.44-0.52-0.60-0.68-0.76__lh0.08.step"
        )
        assert result is not None
        assert result["layer_count"] == 1
        assert result["variable_thicknesses_mm"] == pytest.approx(
            [0.20, 0.28, 0.36, 0.44, 0.52, 0.60, 0.68, 0.76]
        )
        assert result["layer_height_mm"] == pytest.approx(0.08)
        assert result["fixed_layers"] == []

    def test_2L_one_fixed(self):
        result = parse_step_filename(
            "2L__v-0.00-0.08-0.16-0.24-0.32-0.40-0.48-0.56__f1-0.20__lh0.08.step"
        )
        assert result is not None
        assert result["layer_count"] == 2
        assert result["variable_thicknesses_mm"] == pytest.approx(
            [0.00, 0.08, 0.16, 0.24, 0.32, 0.40, 0.48, 0.56]
        )
        assert result["layer_height_mm"] == pytest.approx(0.08)
        assert len(result["fixed_layers"]) == 1
        assert result["fixed_layers"][0]["thickness_mm"] == pytest.approx(0.20)

    def test_3L_two_fixed(self):
        result = parse_step_filename(
            "3L__v-0.00-0.08-0.16-0.24-0.32-0.40-0.48-0.56__f1-0.20__f2-0.16__lh0.08.step"
        )
        assert result is not None
        assert result["layer_count"] == 3
        assert result["variable_thicknesses_mm"] == pytest.approx(
            [0.00, 0.08, 0.16, 0.24, 0.32, 0.40, 0.48, 0.56]
        )
        assert result["layer_height_mm"] == pytest.approx(0.08)
        assert len(result["fixed_layers"]) == 2
        thicknesses = [f["thickness_mm"] for f in result["fixed_layers"]]
        assert thicknesses == pytest.approx([0.20, 0.16])


# ---------------------------------------------------------------------------
# Legacy single-underscore format
# ---------------------------------------------------------------------------

class TestLegacySingleUnderscoreFormat:
    def test_dash_separated(self):
        result = parse_step_filename(
            "1L_v-0.20-0.30-0.40-0.50-0.60-0.70-0.80-0.90_lh0.10.step"
        )
        assert result is not None
        assert result["layer_count"] == 1
        assert result["variable_thicknesses_mm"] == pytest.approx(
            [0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90]
        )
        assert result["layer_height_mm"] == pytest.approx(0.10)
        assert result["fixed_layers"] == []

    def test_pipe_separated(self):
        result = parse_step_filename(
            "1L_manual-0.00|0.16|0.32|0.48|0.64|0.80|0.96|1.12_lh0.16.step"
        )
        assert result is not None
        assert result["layer_count"] == 1
        assert result["variable_thicknesses_mm"] == pytest.approx(
            [0.00, 0.16, 0.32, 0.48, 0.64, 0.80, 0.96, 1.12]
        )
        assert result["layer_height_mm"] == pytest.approx(0.16)
        assert result["fixed_layers"] == []

    def test_pipe_separated_with_fixed(self):
        result = parse_step_filename(
            "2L_manual-0.00|0.16|0.32|0.48|0.64|0.80|0.96|1.12_f-0.20_lh0.16.step"
        )
        assert result is not None
        assert result["layer_count"] == 2
        assert result["variable_thicknesses_mm"] == pytest.approx(
            [0.00, 0.16, 0.32, 0.48, 0.64, 0.80, 0.96, 1.12]
        )
        assert result["layer_height_mm"] == pytest.approx(0.16)
        assert len(result["fixed_layers"]) == 1
        assert result["fixed_layers"][0]["thickness_mm"] == pytest.approx(0.20)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_empty_string_returns_none(self):
        assert parse_step_filename("") is None

    def test_unparseable_filename_returns_none(self):
        assert parse_step_filename("random_filename.step") is None

    def test_forward_slash_path_prefix_stripped(self):
        result = parse_step_filename(
            "/some/path/to/1L__v-0.20-0.28-0.36-0.44-0.52-0.60-0.68-0.76__lh0.08.step"
        )
        assert result is not None
        assert result["layer_count"] == 1
        assert result["variable_thicknesses_mm"] == pytest.approx(
            [0.20, 0.28, 0.36, 0.44, 0.52, 0.60, 0.68, 0.76]
        )

    def test_backslash_path_prefix_stripped(self):
        result = parse_step_filename(
            "C:\\Users\\brand\\steps\\1L__v-0.20-0.28-0.36-0.44-0.52-0.60-0.68-0.76__lh0.08.step"
        )
        assert result is not None
        assert result["layer_count"] == 1
        assert result["variable_thicknesses_mm"] == pytest.approx(
            [0.20, 0.28, 0.36, 0.44, 0.52, 0.60, 0.68, 0.76]
        )

    def test_uppercase_step_extension(self):
        result = parse_step_filename(
            "1L__v-0.20-0.28-0.36-0.44-0.52-0.60-0.68-0.76__lh0.08.STEP"
        )
        assert result is not None
        assert result["layer_count"] == 1
        assert result["variable_thicknesses_mm"] == pytest.approx(
            [0.20, 0.28, 0.36, 0.44, 0.52, 0.60, 0.68, 0.76]
        )

    def test_only_extension_returns_none(self):
        # Just ".step" with no basename — after stripping extension, name is empty
        result = parse_step_filename(".step")
        assert result is None

    def test_very_large_thickness_values(self):
        # 10.00mm thickness — parser should handle it without error
        result = parse_step_filename(
            "1L__v-2.00-4.00-6.00-8.00-10.00-10.00-10.00-10.00__lh0.20.step"
        )
        assert result is not None
        assert result["variable_thicknesses_mm"] == pytest.approx(
            [2.00, 4.00, 6.00, 8.00, 10.00, 10.00, 10.00, 10.00]
        )

    def test_layer_height_zero(self):
        # lh0.00 — should parse without error; value should be 0.0
        result = parse_step_filename(
            "1L__v-0.00-0.08-0.16-0.24-0.32-0.40-0.48-0.56__lh0.00.step"
        )
        assert result is not None
        assert result["layer_height_mm"] == pytest.approx(0.0)

    def test_negative_thickness_values_current_behavior(self):
        # Negative values in the v-segment: document what the parser currently does.
        # The regex splits on "-" so a leading "-" will produce an empty first token
        # or be absorbed into the split — this tests current behavior, not ideal behavior.
        result = parse_step_filename(
            "1L__v--0.10-0.00-0.10-0.20-0.30-0.40-0.50-0.60__lh0.10.step"
        )
        # We just assert it doesn't raise; the exact parse result is implementation-defined
        # (may return None or a dict with unexpected values)
        assert result is None or isinstance(result, dict)

    def test_double_underscore_missing_layer_count_segment(self):
        # Double-underscore format but no NL segment — layer_count should default to 1
        result = parse_step_filename(
            "v-0.20-0.28-0.36-0.44-0.52-0.60-0.68-0.76__lh0.08.step"
        )
        # No layer_count segment → defaults to 1 (set at dict initialization)
        assert result is not None
        assert result["layer_count"] == 1
        assert result["variable_thicknesses_mm"] == pytest.approx(
            [0.20, 0.28, 0.36, 0.44, 0.52, 0.60, 0.68, 0.76]
        )

    def test_double_underscore_extra_whitespace_in_segments(self):
        # strip() is called per segment in the double-underscore branch
        result = parse_step_filename(
            "1L__ v-0.20-0.28-0.36-0.44-0.52-0.60-0.68-0.76 __lh0.08.step"
        )
        # Whitespace is stripped, so this should parse correctly
        assert result is not None
        assert result["variable_thicknesses_mm"] == pytest.approx(
            [0.20, 0.28, 0.36, 0.44, 0.52, 0.60, 0.68, 0.76]
        )
