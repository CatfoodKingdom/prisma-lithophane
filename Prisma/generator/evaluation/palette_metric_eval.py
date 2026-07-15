"""Manual palette metric evaluation harness.

Runs standard-mode palette suggestions, solves the top candidates at coarse
preview resolution, and compares suggestion metrics with solve ``source_rms_de``.
This is deliberately not wired into CI; it is evidence-gathering for the P3b
headline-metric decision.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

import numpy as np


_EVAL_DIR = Path(__file__).resolve().parent
_GEN_DIR = _EVAL_DIR.parent
_PRISMA_DIR = _GEN_DIR.parent
if str(_GEN_DIR) not in sys.path:
    sys.path.insert(0, str(_GEN_DIR))
if str(_PRISMA_DIR) not in sys.path:
    sys.path.insert(0, str(_PRISMA_DIR))


SMALL_SAMPLE_CAVEAT = (
    "Caveat: this harness intentionally uses about n=10 palettes per image; "
    "treat individual correlations as directional small-sample evidence, not "
    "a final statistical verdict."
)
METRIC_KEYS = ("scaled_mean_de", "coverage_pct", "p90_de", "rank_score")


@dataclass(frozen=True)
class PaletteMetricRecord:
    image_path: str
    palette: list[str]
    scaled_mean_de: float
    coverage_pct: float
    p90_de: float
    rank_score: float
    source_rms_de: float

    def as_dict(self) -> dict:
        return {
            "image_path": self.image_path,
            "palette": list(self.palette),
            "scaled_mean_de": float(self.scaled_mean_de),
            "coverage_pct": float(self.coverage_pct),
            "p90_de": float(self.p90_de),
            "rank_score": float(self.rank_score),
            "source_rms_de": float(self.source_rms_de),
        }


def _average_ranks(values: Sequence[float]) -> np.ndarray:
    arr = np.asarray(values, dtype=np.float64)
    order = np.argsort(arr, kind="mergesort")
    ranks = np.empty(len(arr), dtype=np.float64)
    i = 0
    while i < len(arr):
        j = i + 1
        while j < len(arr) and arr[order[j]] == arr[order[i]]:
            j += 1
        rank = (i + 1 + j) / 2.0
        ranks[order[i:j]] = rank
        i = j
    return ranks


def spearman(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    xr = _average_ranks(xs)
    yr = _average_ranks(ys)
    if float(np.std(xr)) == 0.0 or float(np.std(yr)) == 0.0:
        return None
    return float(np.corrcoef(xr, yr)[0, 1])


def _correlations(records: Sequence[PaletteMetricRecord]) -> dict[str, Optional[float]]:
    return {
        key: spearman([getattr(record, key) for record in records], [record.source_rms_de for record in records])
        for key in METRIC_KEYS
    }


def _default_suggest_candidates(
    image_path: Path,
    *,
    filament_ids: Optional[list[str]],
    n_filaments: int,
    top_n: int,
) -> list[dict]:
    from palette.suggest import extract_color_signature, suggest_palettes
    from server import _DEFAULT_CONFIG, _load_profile_sandbox, _white_base, _white_cap

    cfg = dict(_DEFAULT_CONFIG)
    wb_profile = _load_profile_sandbox(_white_base(cfg))
    wc_profile = _load_profile_sandbox(_white_cap(cfg))
    sig = extract_color_signature(
        image_path,
        n_clusters=100,
        max_dim_mm=80.0,
        image_sample_pitch_mm=float(cfg["image_sample_pitch_mm"]),
        wb_profile=wb_profile,
        d_wb=float(cfg["d_wb"]),
    )
    candidates = suggest_palettes(
        sig,
        n_filaments=n_filaments,
        top_k=top_n,
        filament_ids=filament_ids,
        wb_profile=wb_profile,
        wc_profile=wc_profile,
        d_wb=float(cfg["d_wb"]),
        verbose=True,
    )
    return [
        {
            "filament_ids": list(candidate.filament_ids),
            "scaled_mean_de": float(candidate.mean_de),
            "coverage_pct": 100.0 - float(candidate.pct_above_threshold),
            "p90_de": float(candidate.p90_de if candidate.p90_de is not None else candidate.mean_de),
            "rank_score": float(candidate.rank_score if candidate.rank_score is not None else candidate.mean_de),
        }
        for candidate in candidates[:top_n]
    ]


def _default_solve_rms(
    image_path: Path,
    palette: list[str],
    *,
    coarse_pitch_mm: float,
) -> float:
    from server import _DEFAULT_CONFIG, _build_solve_config, _load_run_source_image
    from facade import solve_preview

    cfg = dict(_DEFAULT_CONFIG)
    cfg["image_sample_pitch_mm"] = float(coarse_pitch_mm)
    cfg["solver_fine_pitch_mm"] = float(coarse_pitch_mm)
    cfg["palette"] = list(palette)
    image = _load_run_source_image(image_path, cfg, max_dim_mm=80.0)
    config = _build_solve_config(cfg, palette_override=palette)
    result = solve_preview(image, config)
    return float(getattr(result.stats, "source_rms_de", result.stats.mean_de))


def evaluate_palette_metrics(
    image_paths: Sequence[str | Path],
    *,
    out_dir: str | Path,
    filament_ids: Optional[list[str]] = None,
    n_filaments: int = 4,
    top_n: int = 10,
    coarse_pitch_mm: float = 0.60,
    suggest_fn: Optional[Callable[..., list[dict]]] = None,
    solve_rms_fn: Optional[Callable[..., float]] = None,
) -> dict:
    if top_n <= 0:
        raise ValueError("top_n must be positive")
    suggest = suggest_fn or _default_suggest_candidates
    solve_rms = solve_rms_fn or _default_solve_rms
    output_dir = Path(out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    records: list[PaletteMetricRecord] = []
    by_image: dict[str, list[PaletteMetricRecord]] = {}
    for raw_path in image_paths:
        image_path = Path(raw_path)
        suggested = suggest(
            image_path,
            filament_ids=filament_ids,
            n_filaments=n_filaments,
            top_n=top_n,
        )[:top_n]
        image_records: list[PaletteMetricRecord] = []
        for candidate in suggested:
            palette = list(candidate["filament_ids"])
            rms = solve_rms(
                image_path,
                palette,
                coarse_pitch_mm=coarse_pitch_mm,
            )
            record = PaletteMetricRecord(
                image_path=str(image_path),
                palette=palette,
                scaled_mean_de=float(candidate.get("scaled_mean_de", candidate.get("mean_de", math.nan))),
                coverage_pct=float(candidate["coverage_pct"]),
                p90_de=float(candidate.get("p90_de", candidate.get("scaled_mean_de", math.nan))),
                rank_score=float(candidate.get("rank_score", candidate.get("scaled_mean_de", math.nan))),
                source_rms_de=float(rms),
            )
            records.append(record)
            image_records.append(record)
        by_image[str(image_path)] = image_records

    per_image = {
        image: {
            "n": len(image_records),
            "spearman": _correlations(image_records),
            "records": [record.as_dict() for record in image_records],
        }
        for image, image_records in by_image.items()
    }
    result = {
        "mode": "standard",
        "coarse_pitch_mm": float(coarse_pitch_mm),
        "small_sample_caveat": SMALL_SAMPLE_CAVEAT,
        "metrics_compared": list(METRIC_KEYS),
        "pooled": {
            "n": len(records),
            "spearman": _correlations(records),
        },
        "per_image": per_image,
    }
    json_path = output_dir / "palette_metric_eval.json"
    markdown_path = output_dir / "palette_metric_eval.md"
    json_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    markdown_path.write_text(_render_markdown(result), encoding="utf-8")
    result["artifacts"] = {
        "json": str(json_path),
        "markdown": str(markdown_path),
    }
    return result


def _fmt_corr(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def _render_markdown(result: dict) -> str:
    lines = [
        "# Palette Metric Evaluation",
        "",
        f"Mode: `{result['mode']}`",
        f"Coarse preview pitch: `{result['coarse_pitch_mm']:.3f} mm`",
        "",
        SMALL_SAMPLE_CAVEAT,
        "",
        "## Pooled Spearman",
        "",
        "| Metric | Spearman vs source RMS dE |",
        "| --- | ---: |",
    ]
    for key in METRIC_KEYS:
        lines.append(f"| {key} | {_fmt_corr(result['pooled']['spearman'][key])} |")
    lines.extend(["", "## Per Image", ""])
    for image, payload in result["per_image"].items():
        lines.extend([
            f"### {image}",
            "",
            "| Metric | Spearman vs source RMS dE |",
            "| --- | ---: |",
        ])
        for key in METRIC_KEYS:
            lines.append(f"| {key} | {_fmt_corr(payload['spearman'][key])} |")
        lines.extend([
            "",
            "| Palette | Scaled mean dE | Coverage % | p90 | Rank score | Source RMS dE |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ])
        for record in payload["records"]:
            lines.append(
                "| "
                + ", ".join(record["palette"])
                + f" | {record['scaled_mean_de']:.5f}"
                + f" | {record['coverage_pct']:.2f}"
                + f" | {record['p90_de']:.5f}"
                + f" | {record['rank_score']:.5f}"
                + f" | {record['source_rms_de']:.5f} |"
            )
        lines.append("")
    return "\n".join(lines)


def _parse_filament_ids(raw: Optional[str]) -> Optional[list[str]]:
    if not raw:
        return None
    return [item.strip() for item in raw.split(",") if item.strip()]


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("images", nargs="+", help="Source images to evaluate")
    parser.add_argument("--out-dir", required=True, help="Directory for JSON and Markdown artifacts")
    parser.add_argument("--filament-ids", help="Comma-separated candidate filament pool")
    parser.add_argument("--n-filaments", type=int, default=4, help="Palette size to suggest [4]")
    parser.add_argument("--top-n", type=int, default=10, help="Suggestions per image [10]")
    parser.add_argument("--coarse-pitch-mm", type=float, default=0.60, help="Preview solve pitch [0.60]")
    args = parser.parse_args(argv)
    result = evaluate_palette_metrics(
        args.images,
        out_dir=args.out_dir,
        filament_ids=_parse_filament_ids(args.filament_ids),
        n_filaments=args.n_filaments,
        top_n=args.top_n,
        coarse_pitch_mm=args.coarse_pitch_mm,
    )
    print(f"Wrote {result['artifacts']['json']}")
    print(f"Wrote {result['artifacts']['markdown']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
