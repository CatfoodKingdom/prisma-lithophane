"""CLI wrapper for ``evaluation.preprocessing_harness.evaluate_pair``.

Thin construction layer around ``EvaluationInput`` + ``SolveConfig`` per
``F4_PHASE.md § 2.5``. The CLI loads two images from disk, builds a shared
``SolveConfig`` from the palette/white_base/profiles flags, calls
``evaluate_pair``, and prints the metric bundle to stdout. Artifact writes
are opt-in via ``--out-dir``.

Example:

    python preprocessing_eval_cli.py \\
        --case-id my_case \\
        --baseline baseline.png \\
        --candidate candidate.png \\
        --profiles "<published-library>/filaments/profiles" \\
        --palette bambu-basic-cyan bambu-basic-yellow \\
        --white-base panchroma-matte-cotton-white \\
        --out-dir /tmp/eval_out
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps


# Path setup — Prisma/generator/preprocessing_eval_cli.py
_GEN_DIR = Path(__file__).resolve().parent
_PRISMA_DIR = _GEN_DIR.parent
if str(_GEN_DIR) not in sys.path:
    sys.path.insert(0, str(_GEN_DIR))
if str(_PRISMA_DIR) not in sys.path:
    sys.path.insert(0, str(_PRISMA_DIR))

from facade import SolveConfig

from evaluation.preprocessing_harness import (
    EvaluationInput,
    EvaluationResult,
    evaluate_pair,
)


_SUPPORTED_SOLVER_PRESETS = ("full",)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="preprocessing_eval_cli",
        description=(
            "Pairwise preprocessing evaluation harness: solve two images "
            "through the full pipeline under a shared SolveConfig and report "
            "A4 metric primitives (dE, OOG, small-component, cap TV)."
        ),
    )
    p.add_argument("--case-id", required=True, help="Label emitted into report.json.")
    p.add_argument("--baseline", required=True, metavar="IMAGE_PATH",
                   help="Baseline image path.")
    p.add_argument("--candidate", required=True, metavar="IMAGE_PATH",
                   help="Candidate image path.")
    p.add_argument("--profiles", required=True, metavar="DIR",
                   help="Filament spline-profile directory.")
    p.add_argument("--palette", required=True, nargs="+", metavar="FILAMENT_ID",
                   help="Color filament IDs for the solve.")
    p.add_argument("--white-base", required=True, metavar="FILAMENT_ID",
                   help="White-base filament ID.")
    p.add_argument("--out-dir", default=None, metavar="PATH",
                   help="If set, write baseline/candidate predicted PNGs, the "
                        "delta-dE heatmap, and report.json under this dir.")
    p.add_argument(
        "--solver-preset",
        default="full",
        choices=_SUPPORTED_SOLVER_PRESETS,
        help="Solver preset (MVT supports 'full' only).",
    )
    return p


def _load_image(path: Path) -> np.ndarray:
    img = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    return np.asarray(img, dtype=np.uint8)


def _build_config(args: argparse.Namespace) -> SolveConfig:
    return SolveConfig(
        palette=list(args.palette),
        white_base=args.white_base,
        profiles_dir=Path(args.profiles),
    )


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    baseline_image = _load_image(Path(args.baseline))
    candidate_image = _load_image(Path(args.candidate))
    config = _build_config(args)
    out_dir = Path(args.out_dir) if args.out_dir else None

    request = EvaluationInput(
        case_id=args.case_id,
        baseline_image=baseline_image,
        candidate_image=candidate_image,
        solve_config=config,
        out_dir=out_dir,
    )

    result: EvaluationResult = evaluate_pair(request)

    report = {
        "case_id": args.case_id,
        **asdict(result.metrics),
        "artifact_paths": {k: str(v) for k, v in result.artifact_paths.items()},
    }
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
