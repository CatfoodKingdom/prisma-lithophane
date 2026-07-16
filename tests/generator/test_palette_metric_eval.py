from pathlib import Path
import sys

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
_GEN_DIR = _ROOT / "Prisma" / "generator"
for _p in (_GEN_DIR, _ROOT / "Prisma"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from tools.generator.evaluation.palette_metric_eval import (
    METRIC_KEYS,
    SMALL_SAMPLE_CAVEAT,
    evaluate_palette_metrics,
)


def test_palette_metric_eval_emits_json_markdown_correlations_and_caveat(tmp_path: Path):
    def fake_suggest(image_path, *, filament_ids, n_filaments, top_n):
        return [
            {
                "filament_ids": [f"f{idx}", "x", "y"],
                "scaled_mean_de": float(idx + 1),
                "coverage_pct": float(100 - idx),
                "p90_de": float(idx + 2),
                "rank_score": float(idx + 3),
            }
            for idx in range(top_n)
        ]

    def fake_solve_rms(image_path, palette, *, coarse_pitch_mm):
        return float(int(palette[0][1:]) + 10)

    result = evaluate_palette_metrics(
        ["image-a.png", "image-b.png"],
        out_dir=tmp_path,
        n_filaments=3,
        top_n=4,
        coarse_pitch_mm=0.7,
        suggest_fn=fake_suggest,
        solve_rms_fn=fake_solve_rms,
    )

    json_path = Path(result["artifacts"]["json"])
    markdown_path = Path(result["artifacts"]["markdown"])
    assert json_path.exists()
    assert markdown_path.exists()
    assert result["mode"] == "standard"
    assert result["coarse_pitch_mm"] == pytest.approx(0.7)
    assert result["small_sample_caveat"] == SMALL_SAMPLE_CAVEAT
    assert set(result["pooled"]["spearman"]) == set(METRIC_KEYS)
    assert all(set(payload["spearman"]) == set(METRIC_KEYS) for payload in result["per_image"].values())
    assert result["pooled"]["n"] == 8

    markdown = markdown_path.read_text(encoding="utf-8")
    assert SMALL_SAMPLE_CAVEAT in markdown
    for metric in METRIC_KEYS:
        assert metric in markdown
