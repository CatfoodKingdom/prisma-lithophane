"""
palette.suggest — Automatic palette selection for Prisma.

Given a source image and a library of calibrated filaments, find diverse
candidate subsets of N color filaments that minimize the image signature's
nearest-gamut error.

Algorithm
---------
1. Extract a weighted OKLab color signature as K centroids in the same target
   domain used by solving.
2. Build each filament's single-color gamut and, when enabled, each filament
   pair's sampled two-color gamut. Pair gamuts are the only mixed-color clouds
   considered until the sparse rescore phase.
3. Precompute each centroid's nearest distance to every single and pair gamut
   in the scaled-L scoring space (`gamut_luminance_weight` applies to OKLab L).
4. Enumerate all palettes when the combination count is below the exhaustive
   limit, scoring batches by elementwise minima over the precomputed distance
   vectors; otherwise use multi-start local search to generate candidates.
5. Walk the scored candidate buffer best-first with a diversity floor, then
   fill from best rejected palettes if necessary so the requested count wins.

Usage
-----
    from palette.suggest import suggest_palettes, extract_color_signature

    sig = extract_color_signature("photo.jpg", n_clusters=100)
    candidates = suggest_palettes(sig, n_filaments=7, top_k=5)

    for c in candidates:
        print(f"{c.mean_de:.2f}  {c.filament_ids}")
"""
from __future__ import annotations

import itertools
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Protocol, Tuple

import numpy as np
from scipy.spatial import KDTree

# Path setup — Prisma/generator/palette/suggest.py
_GEN_DIR = Path(__file__).resolve().parent.parent
_PRISMA_DIR = _GEN_DIR.parent
if str(_GEN_DIR) not in sys.path:
    sys.path.insert(0, str(_GEN_DIR))
if str(_PRISMA_DIR) not in sys.path:
    sys.path.insert(0, str(_PRISMA_DIR))

from model import (
    load_profile, load_profiles, predict_transmission,
    srgb_to_linear, to_oklab, PROFILES_DIR, DEFAULT_MODEL_DOMAIN_INGRESS_LUT_PATH,
)
from lib.transmission import _get_splines, _extrapolate
from lut import derive_d_wc_max
from pipeline.target_cloud import (
    MODEL_OKLAB_DOMAIN,
    apply_gamut_white_rescale_to_targets,
    compute_solve_target_cloud,
)


SUGGESTION_COVERAGE_DE_THRESHOLD = 0.02
_EXHAUSTIVE_COMBO_LIMIT = 500_000
_EXHAUSTIVE_SCORE_CHUNK_SIZE = 2048


def _scoped_progress(
    progress: Optional[Callable[[str, float], None]],
    start: float,
    end: float,
) -> Optional[Callable[[str, float], None]]:
    """Map a child operation's monotonic 0..1 progress into a parent range."""

    if progress is None:
        return None
    lo = float(np.clip(start, 0.0, 1.0))
    hi = float(np.clip(end, lo, 1.0))
    last_local = 0.0

    def emit(label: str, fraction: float) -> None:
        nonlocal last_local
        local = float(np.clip(fraction, 0.0, 1.0))
        local = max(last_local, local)
        last_local = local
        progress(label, lo + (hi - lo) * local)

    return emit


# ── Batch transmission evaluation ────────────────────────────────────────────

def _batch_predict_transmission(profile: dict, d_array: np.ndarray) -> np.ndarray:
    """
    Evaluate transmission at multiple thicknesses in one vectorized call.

    Returns (N, 3) float64 array of linear-RGB transmissions.
    """
    spl = _get_splines(profile)
    d_max = profile['knots_mm'][-1]
    d = np.asarray(d_array, dtype=np.float64)
    n = len(d)
    T = np.ones((n, 3), dtype=np.float64)

    interp_mask = (d > 0) & (d <= d_max)
    if interp_mask.any():
        d_interp = d[interp_mask]
        T[interp_mask, 0] = np.clip(spl['r'](d_interp), 0.0, 1.0)
        T[interp_mask, 1] = np.clip(spl['g'](d_interp), 0.0, 1.0)
        T[interp_mask, 2] = np.clip(spl['b'](d_interp), 0.0, 1.0)

    extrap_mask = d > d_max
    if extrap_mask.any():
        for idx in np.where(extrap_mask)[0]:
            T[idx] = _extrapolate(profile, spl, d[idx], d_max)

    return T


# ── Data structures ──────────────────────────────────────────────────────────

@dataclass
class ColorSignature:
    """Weighted color signature of an image in OKLab space."""
    centroids: np.ndarray    # (K, 3) OKLab centroids
    weights: np.ndarray      # (K,) pixel count per cluster (sums to 1.0)
    n_pixels: int            # total pixels in original image
    domain: str = MODEL_OKLAB_DOMAIN


@dataclass
class PaletteCandidate:
    """A candidate filament palette with estimated quality metrics."""
    filament_ids: List[str]
    mean_de: float           # weighted mean dE across image centroids
    max_de: float            # worst centroid dE
    pct_above_threshold: float  # % of image weight with dE > 0.02 (~1 JND)
    gamut_points: int        # total OKLab gamut points used for evaluation
    p90_de: Optional[float] = None
    rank_score: Optional[float] = None
    rank_mode: Optional[str] = None


@dataclass
class _PaletteSearchContext:
    """Prepared inputs shared by exact-size and ladder palette searches."""
    filament_ids: List[str]
    single_gamuts: Dict[str, np.ndarray]
    profiles: Dict[str, dict]
    wb_profile: dict
    wc_profile: dict
    d_wb: float
    d_wc_min: float
    layer_height: float
    max_layers: int
    d_wc_max: float
    t_max: float
    dist_single: Dict[str, np.ndarray]
    dist_pair: Dict[Tuple[str, str], np.ndarray]
    ranking_mode: str
    gamut_luminance_weight: float
    gamut_backend: Optional[PaletteGamutBackend] = None


class PaletteGamutBackend(Protocol):
    """Model-specific gamut source for palette ranking."""

    model_kind: str
    domain: str

    def metadata(self) -> dict:
        ...

    def supports(self, fid: str) -> bool:
        ...

    def single_gamut(self, fid: str) -> np.ndarray:
        ...

    def pair_gamut(self, fid_a: str, fid_b: str) -> np.ndarray:
        ...

    def triple_gamut(self, fid_a: str, fid_b: str, fid_c: str) -> np.ndarray:
        ...


class PhotoStackPaletteGamutBackend:
    """Palette gamut backend backed by the selected photo-stack provider."""

    model_kind = "photo_stack_vectorized"
    domain = MODEL_OKLAB_DOMAIN

    def __init__(
        self,
        provider,
        *,
        white_base: str,
        white_cap: str,
        d_wb: float = 0.20,
        d_wc_min: float = 0.08,
        d_wc_max: float,
        t_max: float,
        layer_height: float = 0.08,
        max_layers: int = 25,
        single_cap_step: int = 4,
        pair_cap_step: int = 8,
        pair_samples: int = 10,
        triple_cap_step: int = 8,
        triple_samples: int = 4,
    ) -> None:
        from photo_stack_lut import _PhotoStackGridEvaluator

        self.provider = provider
        self.white_base = str(white_base)
        self.white_cap = str(white_cap)
        self.d_wb = float(d_wb)
        self.d_wc_min = float(d_wc_min)
        self.d_wc_max = float(d_wc_max)
        self.t_max = float(t_max)
        self.layer_height = float(layer_height)
        self.max_layers = int(max_layers)
        self.single_cap_step = max(1, int(single_cap_step))
        self.pair_cap_step = max(1, int(pair_cap_step))
        self.pair_samples = max(2, int(pair_samples))
        self.triple_cap_step = max(1, int(triple_cap_step))
        self.triple_samples = max(2, int(triple_samples))
        self._evaluator_cls = _PhotoStackGridEvaluator
        self._single_cache: Dict[str, np.ndarray] = {}
        self._pair_cache: Dict[Tuple[str, str], np.ndarray] = {}
        self._triple_cache: Dict[Tuple[str, str, str], np.ndarray] = {}
        predictor = getattr(self.provider, "predictor", None)
        curves = getattr(predictor, "curves", {})
        self._evaluator_filament_ids = tuple(
            sorted(str(fid) for fid in curves if self.supports(str(fid)))
        )
        self._evaluator_filament_id_set = frozenset(self._evaluator_filament_ids)
        self._evaluator_cache: Dict[Tuple[float, ...], object] = {}

    def metadata(self) -> dict:
        return {
            "gamut_backend": self.model_kind,
            "gamut_domain": self.domain,
            "appearance_model_provider": getattr(self.provider, "model_kind", "photo_stack_bundle"),
            "provider_fingerprint": self.provider.fingerprint(),
            "corrections_enabled": bool(getattr(self.provider, "use_corrections", False)),
            "estimated_with_pairs": True,
            "estimated_with_three_color_rescore": False,
        }

    def supports(self, fid: str) -> bool:
        from lib.photo_stack_model.predictor import is_white

        predictor = getattr(self.provider, "predictor", None)
        curves = getattr(predictor, "curves", {})
        text = str(fid)
        bundle = getattr(self.provider, "bundle", None)
        classifier = getattr(bundle, "model_white_classifier", None)
        is_model_white = classifier.is_white(text) if classifier is not None else is_white(text)
        return bool(text and not is_model_white and text in curves)

    def _cap_steps(self, step: int) -> tuple[np.ndarray, np.ndarray]:
        n_wc_min = max(1, round(self.d_wc_min / self.layer_height))
        n_wc_max = max(n_wc_min, round(self.d_wc_max / self.layer_height))
        cap_grid_indices = np.arange(n_wc_min, n_wc_max + 1, max(1, int(step)), dtype=np.int16)
        cap_steps_d = np.round(cap_grid_indices.astype(float) * self.layer_height, 6)
        return cap_grid_indices, cap_steps_d

    def _evaluator(self, filament_ids: List[str], cap_steps_d: np.ndarray):
        requested = tuple(str(fid) for fid in filament_ids)
        missing = sorted(set(requested) - self._evaluator_filament_id_set)
        if missing:
            raise ValueError(f"palette evaluator does not support filaments: {missing}")
        cap_grid = np.asarray(cap_steps_d, dtype=float)
        key = tuple(float(value) for value in cap_grid)
        evaluator = self._evaluator_cache.get(key)
        if evaluator is None:
            # The grid evaluator uses its configured filament list only to
            # prebuild immutable OD/profile tables. predict_subset() still
            # receives the exact requested single, pair, or triple. Sharing
            # one evaluator per cap grid therefore avoids recompiling the same
            # correction artifact and profile grids without changing samples.
            evaluator = self._evaluator_cls(
                self.provider,
                filament_ids=list(self._evaluator_filament_ids),
                white_base=self.white_base,
                white_cap=self.white_cap,
                d_wb=self.d_wb,
                layer_height=self.layer_height,
                max_layers=self.max_layers,
                cap_steps_d=cap_grid,
            )
            self._evaluator_cache[key] = evaluator
        return evaluator

    def _gamut_oklab_from_model_rgb(self, model_rgb: np.ndarray) -> np.ndarray:
        """Convert photo-stack model RGB into model-domain OKLab."""

        rgb = np.clip(np.asarray(model_rgb, dtype=np.float32), 1e-9, 1.0)
        project = getattr(self.provider, "project_model_linear_rgb_to_appearance", None)
        if callable(project):
            # The projection slot is a compatibility no-op after the photo stack
            # domain retirement; keep calling it so backend and solve LUTs share the
            # same provider hook.
            rgb = project(rgb)
            rgb = np.clip(rgb, 1e-9, 1.0)
        return to_oklab(rgb).astype(np.float32)

    def single_gamut(self, fid: str) -> np.ndarray:
        fid = str(fid)
        cached = self._single_cache.get(fid)
        if cached is not None:
            return cached
        if not self.supports(fid):
            empty = np.zeros((0, 3), dtype=np.float32)
            self._single_cache[fid] = empty
            return empty
        _cap_grid_indices, cap_steps_d = self._cap_steps(self.single_cap_step)
        evaluator = self._evaluator([fid], cap_steps_d)
        color_counts = np.arange(self.max_layers + 1, dtype=np.int16)
        color_t = color_counts.astype(float) * self.layer_height
        parts: list[np.ndarray] = []
        for cap_idx, cap_t in enumerate(cap_steps_d):
            mask = color_t + float(cap_t) <= self.t_max + 1e-9
            if not np.any(mask):
                continue
            counts = color_counts[mask].reshape(-1, 1)
            cap_indices = np.full(len(counts), cap_idx, dtype=np.int16)
            rgb = evaluator.predict_subset((fid,), counts, cap_indices)
            parts.append(self._gamut_oklab_from_model_rgb(rgb))
        gamut = np.concatenate(parts, axis=0) if parts else np.zeros((0, 3), dtype=np.float32)
        self._single_cache[fid] = gamut
        return gamut

    def pair_gamut(self, fid_a: str, fid_b: str) -> np.ndarray:
        key = (min(str(fid_a), str(fid_b)), max(str(fid_a), str(fid_b)))
        cached = self._pair_cache.get(key)
        if cached is not None:
            return cached
        if not self.supports(key[0]) or not self.supports(key[1]):
            empty = np.zeros((0, 3), dtype=np.float32)
            self._pair_cache[key] = empty
            return empty
        _cap_grid_indices, cap_steps_d = self._cap_steps(self.pair_cap_step)
        evaluator = self._evaluator(list(key), cap_steps_d)
        sample_counts = np.unique(
            np.rint(np.linspace(0, self.max_layers, self.pair_samples)).astype(np.int16)
        )
        aa, bb = np.meshgrid(sample_counts, sample_counts, indexing="ij")
        base_counts = np.column_stack([aa.ravel(), bb.ravel()]).astype(np.int16)
        color_t = base_counts.astype(float).sum(axis=1) * self.layer_height
        parts: list[np.ndarray] = []
        for cap_idx, cap_t in enumerate(cap_steps_d):
            mask = color_t + float(cap_t) <= self.t_max + 1e-9
            if not np.any(mask):
                continue
            counts = base_counts[mask]
            cap_indices = np.full(len(counts), cap_idx, dtype=np.int16)
            rgb = evaluator.predict_subset(key, counts, cap_indices)
            parts.append(self._gamut_oklab_from_model_rgb(rgb))
        gamut = np.concatenate(parts, axis=0) if parts else np.zeros((0, 3), dtype=np.float32)
        self._pair_cache[key] = gamut
        return gamut

    def triple_gamut(self, fid_a: str, fid_b: str, fid_c: str) -> np.ndarray:
        key = tuple(sorted((str(fid_a), str(fid_b), str(fid_c))))
        cached = self._triple_cache.get(key)
        if cached is not None:
            return cached
        if any(not self.supports(fid) for fid in key):
            empty = np.zeros((0, 3), dtype=np.float32)
            self._triple_cache[key] = empty
            return empty
        _cap_grid_indices, cap_steps_d = self._cap_steps(self.triple_cap_step)
        evaluator = self._evaluator(list(key), cap_steps_d)
        sample_counts = np.unique(
            np.rint(np.linspace(0, self.max_layers, self.triple_samples)).astype(np.int16)
        )
        aa, bb, cc = np.meshgrid(sample_counts, sample_counts, sample_counts, indexing="ij")
        base_counts = np.column_stack([aa.ravel(), bb.ravel(), cc.ravel()]).astype(np.int16)
        color_t = base_counts.astype(float).sum(axis=1) * self.layer_height
        parts: list[np.ndarray] = []
        for cap_idx, cap_t in enumerate(cap_steps_d):
            mask = color_t + float(cap_t) <= self.t_max + 1e-9
            if not np.any(mask):
                continue
            counts = base_counts[mask]
            cap_indices = np.full(len(counts), cap_idx, dtype=np.int16)
            rgb = evaluator.predict_subset(key, counts, cap_indices)
            parts.append(self._gamut_oklab_from_model_rgb(rgb))
        gamut = np.concatenate(parts, axis=0) if parts else np.zeros((0, 3), dtype=np.float32)
        self._triple_cache[key] = gamut
        return gamut


# ── Image color extraction ──────────────────────────────────────────────────

def extract_color_signature_from_oklab(
    target_oklab: np.ndarray,
    *,
    n_clusters: int = 100,
    domain: str = MODEL_OKLAB_DOMAIN,
    n_pixels: Optional[int] = None,
) -> ColorSignature:
    """Cluster an already-solved OKLab target cloud into a palette signature."""

    oklab = np.asarray(target_oklab, dtype=np.float32).reshape(-1, 3)
    total = int(len(oklab) if n_pixels is None else n_pixels)
    if len(oklab) == 0:
        return ColorSignature(
            centroids=np.zeros((0, 3), dtype=np.float32),
            weights=np.zeros(0, dtype=np.float64),
            n_pixels=total,
            domain=str(domain),
        )

    k = min(int(n_clusters), len(oklab))
    centroids, labels = _kmeans_oklab(oklab, k)
    counts = np.bincount(labels, minlength=k).astype(np.float64)
    weights = counts / counts.sum()

    active = weights > 0
    centroids = centroids[active]
    weights = weights[active]
    weights /= weights.sum()

    return ColorSignature(
        centroids=centroids.astype(np.float32),
        weights=weights,
        n_pixels=total,
        domain=str(domain),
    )


def solve_target_oklab_for_signature(
    img: np.ndarray,
    *,
    wb_profile: dict,
    config,
    white_rescale_provider=None,
) -> tuple[np.ndarray, dict]:
    """Return the solve-equivalent target cloud used by palette suggestion.

    Saturated-corner inverse-LUT entries are extrapolated outside measured
    swatch evidence and may move between Camera Transform generations. This
    helper intentionally mirrors solve ingress rather than promising stable
    precision for those corner distances.
    """

    cloud = compute_solve_target_cloud(img, wb_profile, config)
    target_oklab = cloud.solve_oklab
    white_rgb = None
    if bool(getattr(config, "gamut_white_rescale", False)):
        target_oklab, white_rgb = apply_gamut_white_rescale_to_targets(
            target_oklab,
            provider=white_rescale_provider,
            config=config,
        )
    stats = {
        "signature_domain": cloud.domain,
        "model_domain_ingress": bool(cloud.model_domain_ingress),
        "gamut_white_rescale": bool(getattr(config, "gamut_white_rescale", False)),
        "gamut_white_rescale_applied": white_rgb is not None,
        "white_rgb": [float(v) for v in white_rgb] if white_rgb is not None else None,
        "observation_shape": list(cloud.observation_shape),
        "solve_shape": list(cloud.solve_shape),
    }
    return np.asarray(target_oklab, dtype=np.float32), stats


def extract_color_signature(
    image_path: str | Path = None,
    n_clusters: int = 100,
    max_dim_mm: float = 80.0,
    image_sample_pitch_mm: float = 0.20,
    wb_profile: Optional[dict] = None,
    d_wb: float = 0.20,
    img: Optional[np.ndarray] = None,
    *,
    solve_config=None,
    white_rescale_provider=None,
    model_domain_ingress: bool = False,
    model_domain_ingress_lut_path: str | Path = DEFAULT_MODEL_DOMAIN_INGRESS_LUT_PATH,
) -> ColorSignature:
    """
    Extract the color signature of an image as weighted OKLab centroids.

    The image is loaded, optionally downsampled, converted through the same
    target-cloud path used by the solve runner, then clustered via k-means in
    OKLab. ``model_domain_ingress`` controls the source-pixel conversion used by
    ``image_to_target()``; the resulting comparison contract is model OKLab so
    it can be scored against model-domain palette gamuts.

    Saturated-corner inverse-LUT entries are extrapolated outside measured
    swatch evidence and may move between Camera Transform generations. Palette
    suggestion follows solve ingress for those pixels and leaves downstream
    gamut handling to the normal solve/gamut machinery.

    Parameters
    ----------
    image_path            : source image (unused when *img* is provided)
    n_clusters            : number of k-means clusters (more = finer, slower)
    max_dim_mm            : resize so longest edge <= this (mm) for speed
    image_sample_pitch_mm : image sampling pitch for resize calculation (mm/px)
    wb_profile            : white base profile (loaded if None)
    d_wb                  : white base thickness (mm)
    img                   : pre-loaded (and optionally adjusted) image array; skips load_image
    """
    if wb_profile is None:
        wb_profile = load_profile("panchroma-matte-cotton-white")

    if img is None:
        from image_ingress import load_image

        img = load_image(
            image_path,
            image_sample_pitch_mm=image_sample_pitch_mm,
            max_dim_mm=max_dim_mm,
        )
    if solve_config is None:
        from types import SimpleNamespace

        solve_config = SimpleNamespace(
            d_wb=d_wb,
            image_sample_pitch_mm=image_sample_pitch_mm,
            solver_fine_pitch_mm=image_sample_pitch_mm,
            model_domain_ingress=bool(model_domain_ingress),
            model_domain_ingress_lut_path=model_domain_ingress_lut_path,
            gamut_white_rescale=False,
        )

    target_oklab, stats = solve_target_oklab_for_signature(
        img,
        wb_profile=wb_profile,
        config=solve_config,
        white_rescale_provider=white_rescale_provider,
    )
    return extract_color_signature_from_oklab(
        target_oklab,
        n_clusters=n_clusters,
        domain=stats["signature_domain"],
        n_pixels=target_oklab.shape[0],
    )


def _scale_oklab_l(oklab: np.ndarray, luminance_weight: float) -> np.ndarray:
    """Scale OKLab L for palette scoring while preserving chroma axes."""
    scaled = np.asarray(oklab, dtype=np.float32).copy()
    scaled[..., 0] *= np.float32(max(0.0, float(luminance_weight)))
    return scaled


def _gamut_domain(gamut_backend: Optional[PaletteGamutBackend]) -> str:
    if gamut_backend is None:
        return MODEL_OKLAB_DOMAIN
    return str(getattr(gamut_backend, "domain", MODEL_OKLAB_DOMAIN))


def _assert_signature_gamut_domain_match(
    sig: ColorSignature,
    gamut_backend: Optional[PaletteGamutBackend],
) -> str:
    gamut_domain = _gamut_domain(gamut_backend)
    sig_domain = str(getattr(sig, "domain", "") or "")
    if sig_domain != gamut_domain:
        raise ValueError(
            "Palette suggestion domain mismatch: "
            f"signature domain {sig_domain!r} cannot be scored against "
            f"gamut domain {gamut_domain!r}"
        )
    return gamut_domain


def extract_luminance_residual_signature(
    img: Optional[np.ndarray] = None,
    *,
    target_oklab: Optional[np.ndarray] = None,
    domain: str = MODEL_OKLAB_DOMAIN,
    n_clusters: int = 100,
    luminance_weight: float = 0.15,
    chroma_weight_power: float = 1.0,
) -> Tuple[ColorSignature, Dict[str, float]]:
    """Extract a chroma-emphasized signature for luminance-detail palette search.

    Luminance-detail solves let the white cap carry much of the source L
    structure, so palette suggestion should emphasize chroma reach instead of
    fitting the full source luminance as if color stacks carried all detail.
    """
    if target_oklab is None:
        if img is None:
            raise ValueError("img or target_oklab is required")
        h, w = img.shape[:2]
        oklab = to_oklab(srgb_to_linear(img).reshape(h * w, 3)).astype(np.float32)
        n_pixels = int(h * w)
    else:
        oklab = np.asarray(target_oklab, dtype=np.float32).reshape(-1, 3)
        n_pixels = int(len(oklab))
    if len(oklab) == 0:
        empty = ColorSignature(
            centroids=np.zeros((0, 3), dtype=np.float32),
            weights=np.zeros(0, dtype=np.float64),
            n_pixels=n_pixels,
            domain=str(domain),
        )
        return empty, {
            "luminance_weight": float(luminance_weight),
            "chroma_weight_power": float(chroma_weight_power),
            "clusters": 0,
            "signature_domain": str(domain),
        }
    chroma = np.linalg.norm(oklab[:, 1:3], axis=1).astype(np.float32)
    chroma_ref = max(float(np.percentile(chroma, 90.0)), 1e-6)
    chroma_importance = np.clip(chroma / np.float32(chroma_ref), 0.0, 1.0)
    if chroma_weight_power != 1.0:
        chroma_importance = np.power(
            chroma_importance,
            np.float32(max(float(chroma_weight_power), 1e-6)),
            dtype=np.float32,
        )

    points = _scale_oklab_l(oklab, luminance_weight)
    k = min(int(n_clusters), len(points))
    if k <= 0:
        empty = ColorSignature(
            centroids=np.zeros((0, 3), dtype=np.float32),
            weights=np.zeros(0, dtype=np.float64),
            n_pixels=n_pixels,
            domain=str(domain),
        )
        return empty, {
            "luminance_weight": float(luminance_weight),
            "chroma_weight_power": float(chroma_weight_power),
            "clusters": 0,
        }

    _, labels = _kmeans_oklab(points, k)
    centroids = np.zeros((k, 3), dtype=np.float32)
    weights = np.zeros(k, dtype=np.float64)
    non_empty = np.zeros(k, dtype=bool)
    for cluster in range(k):
        mask = labels == cluster
        if not np.any(mask):
            continue
        non_empty[cluster] = True
        cluster_weights = chroma_importance[mask].astype(np.float64)
        total = float(np.sum(cluster_weights))
        if total > 1e-12:
            centroids[cluster] = np.average(
                oklab[mask],
                axis=0,
                weights=cluster_weights,
            ).astype(np.float32)
        else:
            centroids[cluster] = np.mean(oklab[mask], axis=0).astype(np.float32)
        weights[cluster] = total

    if float(np.sum(weights)) < 1e-9:
        active = non_empty
        weights = np.where(non_empty, 1.0, 0.0).astype(np.float64)
    else:
        active = weights > 0
    centroids = centroids[active]
    weights = weights[active]
    weights /= max(float(np.sum(weights)), 1e-12)

    stats = {
        "source_chroma_mean": float(np.mean(chroma)),
        "source_chroma_p90": chroma_ref,
        "source_chroma_p95": float(np.percentile(chroma, 95.0)),
        "chroma_importance_mean": float(np.mean(chroma_importance)),
        "chroma_importance_active_fraction": float(
            np.mean(chroma_importance > np.float32(0.05))
        ),
        "luminance_weight": float(luminance_weight),
        "chroma_weight_power": float(chroma_weight_power),
        "clusters": int(len(centroids)),
        "signature_domain": str(domain),
    }
    return ColorSignature(
        centroids=centroids,
        weights=weights,
        n_pixels=n_pixels,
        domain=str(domain),
    ), stats


def _kmeans_oklab(
    points: np.ndarray,
    k: int,
    max_iters: int = 30,
    seed: int = 42,
) -> Tuple[np.ndarray, np.ndarray]:
    """Simple k-means in OKLab space. Returns (centroids, labels)."""
    rng = np.random.RandomState(seed)
    n = len(points)

    # k-means++ initialization — O(n·k) via running minimum
    centroids = np.empty((k, 3), dtype=np.float32)
    idx = rng.randint(n)
    centroids[0] = points[idx]
    min_dists = np.full(n, np.inf, dtype=np.float32)

    for i in range(1, k):
        new_dists = np.sum((points - centroids[i - 1]) ** 2, axis=1)
        np.minimum(min_dists, new_dists, out=min_dists)
        total = min_dists.sum()
        if total < 1e-12:
            centroids[i] = points[rng.randint(n)]
        else:
            probs = min_dists / total
            idx = rng.choice(n, p=probs)
            centroids[i] = points[idx]

    # Iterate
    for _ in range(max_iters):
        tree = KDTree(centroids)
        _, labels = tree.query(points, k=1)
        labels = labels.ravel()

        # Vectorized centroid update via bincount
        new_centroids = centroids.copy()
        counts = np.bincount(labels, minlength=k)
        active = counts > 0
        for c in range(3):
            sums = np.bincount(labels, weights=points[:, c], minlength=k)
            new_centroids[active, c] = (sums[active] / counts[active]).astype(np.float32)

        if np.allclose(centroids, new_centroids, atol=1e-6):
            break
        centroids = new_centroids

    return centroids, labels


# ── Filament gamut computation ───────────────────────────────────────────────

def _compute_single_filament_gamut(
    profile: dict,
    wb_profile: dict,
    wc_profile: dict,
    d_wb: float = 0.20,
    d_wc_min: float = 0.08,
    layer_height: float = 0.08,
    max_layers: int = 25,
    d_wc_max: Optional[float] = None,
    cap_step: int = 4,
    t_max: Optional[float] = None,
) -> np.ndarray:
    """
    Compute OKLab gamut for a single filament across all thickness × cap combos.

    Parameters
    ----------
    cap_step : sample every Nth cap step for speed (4 = every 4th step)
    t_max    : total budget for cap + color (mm). Combos exceeding this are
               excluded. Matches the budget constraint in build_luts().

    Returns (M, 3) OKLab array.
    """
    if d_wc_max is None:
        d_wc_max = derive_d_wc_max(wc_profile, layer_height=layer_height)
    if t_max is None:
        t_max = d_wc_max + max_layers * layer_height

    T_wb = predict_transmission(wb_profile, d_wb)  # (3,)

    n_wc_min = max(1, round(float(d_wc_min) / layer_height))
    n_wc_max = max(n_wc_min, round(d_wc_max / layer_height))
    cap_indices = np.arange(n_wc_min, n_wc_max + 1, cap_step)
    d_wc_arr = np.round(cap_indices * layer_height, 6)

    color_layers = np.arange(max_layers + 1)
    d_c_arr = np.round(color_layers * layer_height, 6)

    # Batch evaluate all unique thicknesses at once
    T_wc_all = _batch_predict_transmission(wc_profile, d_wc_arr)  # (n_cap, 3)
    T_c_all = _batch_predict_transmission(profile, d_c_arr)       # (n_layer, 3)

    # Meshgrid: every (cap, color) combination
    # T_total[i,j] = T_wb * T_wc[i] * T_c[j]
    # Shape: (n_cap, n_layer, 3)
    T_total = T_wb[np.newaxis, np.newaxis, :] * \
              T_wc_all[:, np.newaxis, :] * \
              T_c_all[np.newaxis, :, :]

    # Budget mask: d_wc[i] + d_c[j] <= t_max
    budget_ok = d_wc_arr[:, np.newaxis] + d_c_arr[np.newaxis, :] <= t_max

    # Flatten and filter
    T_flat = T_total[budget_ok]
    if len(T_flat) == 0:
        return np.zeros((0, 3), dtype=np.float32)

    T_flat = np.clip(T_flat, 1e-9, 1.0).astype(np.float32)
    return to_oklab(T_flat).astype(np.float32)


def _compute_pair_gamut(
    prof_a: dict,
    prof_b: dict,
    wb_profile: dict,
    wc_profile: dict,
    d_wb: float = 0.20,
    d_wc_min: float = 0.08,
    layer_height: float = 0.08,
    max_layers: int = 25,
    d_wc_max: Optional[float] = None,
    n_samples: int = 10,
    cap_step: int = 8,
    t_max: Optional[float] = None,
) -> np.ndarray:
    """
    Sample OKLab gamut for a 2-filament combination.

    Uses n_samples thickness levels per filament (evenly spaced) and sampled
    cap steps. Returns (M, 3) OKLab array.
    """
    if d_wc_max is None:
        d_wc_max = derive_d_wc_max(wc_profile, layer_height=layer_height)

    if t_max is None:
        t_max = d_wc_max + max_layers * layer_height

    T_wb = predict_transmission(wb_profile, d_wb)  # (3,)

    n_wc_min = max(1, round(float(d_wc_min) / layer_height))
    n_wc_max = max(n_wc_min, round(d_wc_max / layer_height))
    cap_indices = np.arange(n_wc_min, n_wc_max + 1, cap_step)
    d_wc_arr = np.round(cap_indices * layer_height, 6)
    d_steps = np.linspace(0, max_layers * layer_height, n_samples)

    # Batch evaluate all unique thicknesses at once
    T_wc_all = _batch_predict_transmission(wc_profile, d_wc_arr)  # (n_cap, 3)
    T_a_all = _batch_predict_transmission(prof_a, d_steps)         # (n_samples, 3)
    T_b_all = _batch_predict_transmission(prof_b, d_steps)         # (n_samples, 3)

    # 3D meshgrid: T_total[i,j,k] = T_wb * T_wc[i] * T_a[j] * T_b[k]
    # Shape: (n_cap, n_samples, n_samples, 3)
    T_total = (T_wb[np.newaxis, np.newaxis, np.newaxis, :] *
               T_wc_all[:, np.newaxis, np.newaxis, :] *
               T_a_all[np.newaxis, :, np.newaxis, :] *
               T_b_all[np.newaxis, np.newaxis, :, :])

    # Budget mask: d_wc[i] + d_a[j] + d_b[k] <= t_max
    budget_ok = (d_wc_arr[:, np.newaxis, np.newaxis] +
                 d_steps[np.newaxis, :, np.newaxis] +
                 d_steps[np.newaxis, np.newaxis, :]) <= t_max

    T_flat = T_total[budget_ok]
    if len(T_flat) == 0:
        return np.zeros((0, 3), dtype=np.float32)

    T_flat = np.clip(T_flat, 1e-9, 1.0).astype(np.float32)
    return to_oklab(T_flat).astype(np.float32)


def _compute_triple_gamut(
    prof_a: dict,
    prof_b: dict,
    prof_c: dict,
    wb_profile: dict,
    wc_profile: dict,
    d_wb: float = 0.20,
    d_wc_min: float = 0.08,
    layer_height: float = 0.08,
    max_layers: int = 25,
    d_wc_max: Optional[float] = None,
    n_samples: int = 4,
    cap_step: int = 8,
    t_max: Optional[float] = None,
) -> np.ndarray:
    """Sample OKLab gamut for a sparse 3-filament combination."""
    if d_wc_max is None:
        d_wc_max = derive_d_wc_max(wc_profile, layer_height=layer_height)

    if t_max is None:
        t_max = d_wc_max + max_layers * layer_height

    T_wb = predict_transmission(wb_profile, d_wb)

    n_wc_min = max(1, round(float(d_wc_min) / layer_height))
    n_wc_max = max(n_wc_min, round(d_wc_max / layer_height))
    cap_indices = np.arange(n_wc_min, n_wc_max + 1, cap_step)
    d_wc_arr = np.round(cap_indices * layer_height, 6)
    d_steps = np.linspace(0, max_layers * layer_height, n_samples)

    T_wc_all = _batch_predict_transmission(wc_profile, d_wc_arr)
    T_a_all = _batch_predict_transmission(prof_a, d_steps)
    T_b_all = _batch_predict_transmission(prof_b, d_steps)
    T_c_all = _batch_predict_transmission(prof_c, d_steps)

    T_total = (
        T_wb[np.newaxis, np.newaxis, np.newaxis, np.newaxis, :]
        * T_wc_all[:, np.newaxis, np.newaxis, np.newaxis, :]
        * T_a_all[np.newaxis, :, np.newaxis, np.newaxis, :]
        * T_b_all[np.newaxis, np.newaxis, :, np.newaxis, :]
        * T_c_all[np.newaxis, np.newaxis, np.newaxis, :, :]
    )

    budget_ok = (
        d_wc_arr[:, np.newaxis, np.newaxis, np.newaxis]
        + d_steps[np.newaxis, :, np.newaxis, np.newaxis]
        + d_steps[np.newaxis, np.newaxis, :, np.newaxis]
        + d_steps[np.newaxis, np.newaxis, np.newaxis, :]
    ) <= t_max

    T_flat = T_total[budget_ok]
    if len(T_flat) == 0:
        return np.zeros((0, 3), dtype=np.float32)

    T_flat = np.clip(T_flat, 1e-9, 1.0).astype(np.float32)
    return to_oklab(T_flat).astype(np.float32)


# ── Core palette selection ───────────────────────────────────────────────────

def _weighted_mean_de(
    sig: ColorSignature,
    gamut_tree: KDTree,
    de_threshold: float = SUGGESTION_COVERAGE_DE_THRESHOLD,
) -> Tuple[float, float, float]:
    """
    Compute weighted mean dE, max dE, and % weight above de_threshold
    for a color signature against a gamut KD-tree.
    """
    dists, _ = gamut_tree.query(sig.centroids, k=1)
    mean_de = float(np.average(dists, weights=sig.weights))
    max_de = float(dists.max())
    pct_above_threshold = float(sig.weights[dists > de_threshold].sum() * 100)
    return mean_de, max_de, pct_above_threshold


def _weighted_percentile(values: np.ndarray, weights: np.ndarray, percentile: float) -> float:
    """Return a weighted percentile for a small color-signature distance vector."""
    v = np.asarray(values, dtype=np.float64)
    w = np.asarray(weights, dtype=np.float64)
    if v.size == 0:
        return float("nan")
    order = np.argsort(v)
    v = v[order]
    w = np.clip(w[order], 0.0, None)
    total = float(np.sum(w))
    if total <= 0.0:
        return float(np.percentile(v, percentile))
    cdf = np.cumsum(w) / total
    idx = int(np.searchsorted(cdf, float(percentile) / 100.0, side="left"))
    idx = min(max(idx, 0), len(v) - 1)
    return float(v[idx])


def _precompute_centroid_distances(
    sig: ColorSignature,
    filament_ids: List[str],
    single_gamuts: Dict[str, np.ndarray],
    profiles: Dict[str, dict],
    wb_profile: dict,
    wc_profile: dict,
    d_wb: float,
    d_wc_min: float,
    layer_height: float,
    max_layers: int,
    d_wc_max: float,
    include_pairs: bool,
    t_max: Optional[float],
    gamut_luminance_weight: float = 1.0,
    gamut_backend: Optional[PaletteGamutBackend] = None,
    progress: Optional[Callable[[str, float], None]] = None,
    cancel: Optional[Callable[[], bool]] = None,
) -> Tuple[Dict[str, np.ndarray], Dict[Tuple[str, str], np.ndarray]]:
    """
    Precompute per-centroid nearest-neighbor distances to each single and pair
    gamut. Returns (dist_single, dist_pair) where each value is a (K,) float32
    array of distances from each color signature centroid.
    """
    K = len(sig.centroids)
    query_centroids = _scale_oklab_l(sig.centroids, gamut_luminance_weight)
    dist_single: Dict[str, np.ndarray] = {}
    dist_pair: Dict[Tuple[str, str], np.ndarray] = {}

    for fid in filament_ids:
        gamut = single_gamuts.get(fid)
        if gamut is not None and len(gamut) > 0:
            tree = KDTree(_scale_oklab_l(gamut, gamut_luminance_weight))
            dists, _ = tree.query(query_centroids, k=1)
            dist_single[fid] = dists.astype(np.float32)
        else:
            dist_single[fid] = np.full(K, np.inf, dtype=np.float32)

    if include_pairs:
        pairs = list(itertools.combinations(filament_ids, 2))
        cancelled = False
        for idx, (fid_a, fid_b) in enumerate(pairs):
            if cancel and cancel():
                cancelled = True
                break
            if progress and idx % 10 == 0:
                progress(
                    f"Computing pair gamuts {idx + 1}/{len(pairs)}",
                    0.20 + 0.80 * idx / max(1, len(pairs)),
                )
            key = (min(fid_a, fid_b), max(fid_a, fid_b))
            if gamut_backend is not None:
                pg = gamut_backend.pair_gamut(fid_a, fid_b)
            else:
                pg = _compute_pair_gamut(
                    profiles[fid_a], profiles[fid_b], wb_profile, wc_profile,
                    d_wb=d_wb, d_wc_min=d_wc_min, layer_height=layer_height, max_layers=max_layers,
                    d_wc_max=d_wc_max, n_samples=10, cap_step=8, t_max=t_max,
                )
            if len(pg) > 0:
                tree = KDTree(_scale_oklab_l(pg, gamut_luminance_weight))
                dists, _ = tree.query(query_centroids, k=1)
                dist_pair[key] = dists.astype(np.float32)
            else:
                dist_pair[key] = np.full(K, np.inf, dtype=np.float32)
        if progress and not cancelled:
            progress("Pair gamuts complete", 1.0)
    elif progress:
        progress("Single gamuts complete", 1.0)

    return dist_single, dist_pair


def _palette_distance_vector(
    palette: List[str],
    dist_single: Dict[str, np.ndarray],
    dist_pair: Dict[Tuple[str, str], np.ndarray],
    n_centroids: int,
) -> np.ndarray:
    """Nearest-gamut distance for each signature centroid in OKLab space."""
    K = int(n_centroids)
    d_min = np.full(K, np.inf, dtype=np.float32)
    for fid in palette:
        if fid in dist_single:
            np.minimum(d_min, dist_single[fid], out=d_min)
    for fid_a, fid_b in itertools.combinations(palette, 2):
        key = (min(fid_a, fid_b), max(fid_a, fid_b))
        if key in dist_pair:
            np.minimum(d_min, dist_pair[key], out=d_min)
    return d_min


def _score_palette_metrics(
    palette: List[str],
    sig: ColorSignature,
    dist_single: Dict[str, np.ndarray],
    dist_pair: Dict[Tuple[str, str], np.ndarray],
    de_threshold: float = SUGGESTION_COVERAGE_DE_THRESHOLD,
) -> Tuple[float, float, float, float]:
    """Score a palette from precomputed OKLab nearest distances."""
    d_min = _palette_distance_vector(
        palette,
        dist_single,
        dist_pair,
        len(sig.centroids),
    )
    mean_de = float(np.average(d_min, weights=sig.weights))
    max_de = float(d_min.max())
    pct = float(sig.weights[d_min > de_threshold].sum() * 100)
    p90 = _weighted_percentile(d_min, sig.weights, 90.0)
    return mean_de, max_de, pct, p90


def _palette_rank_score(
    *,
    mean_de: float,
    max_de: float,
    pct_above_threshold: float,
    p90_de: float,
    mode: str,
) -> float:
    """Ranking objective; robust mode penalizes visible holes, not just averages."""
    if mode != "robust":
        return float(mean_de)
    return float(
        mean_de
        + 0.35 * p90_de
        + 0.15 * max_de
        + 0.03 * (pct_above_threshold / 100.0)
    )


def _make_palette_candidate(
    palette: List[str],
    sig: ColorSignature,
    dist_single: Dict[str, np.ndarray],
    dist_pair: Dict[Tuple[str, str], np.ndarray],
    de_threshold: float,
    gamut_points: int,
    ranking_mode: str,
) -> PaletteCandidate:
    mean_de, max_de, pct, p90 = _score_palette_metrics(
        palette, sig, dist_single, dist_pair, de_threshold
    )
    rank_score = _palette_rank_score(
        mean_de=mean_de,
        max_de=max_de,
        pct_above_threshold=pct,
        p90_de=p90,
        mode=ranking_mode,
    )
    return PaletteCandidate(
        filament_ids=list(palette),
        mean_de=mean_de,
        max_de=max_de,
        pct_above_threshold=pct,
        gamut_points=gamut_points,
        p90_de=p90,
        rank_score=rank_score,
        rank_mode=ranking_mode,
    )


def _candidate_sort_key(candidate: PaletteCandidate) -> tuple[float, float, float, Tuple[str, ...]]:
    score = candidate.rank_score if candidate.rank_score is not None else candidate.mean_de
    return (
        float(score),
        float(candidate.mean_de),
        float(candidate.max_de),
        tuple(sorted(str(fid) for fid in candidate.filament_ids)),
    )


def _candidate_buffer_size(top_k: int) -> int:
    return max(int(top_k) * 50, 200)


def _palette_differs_by_at_least_two_members(
    candidate: PaletteCandidate,
    selected: List[PaletteCandidate],
) -> bool:
    candidate_set = frozenset(candidate.filament_ids)
    return all(
        len(candidate_set.symmetric_difference(frozenset(existing.filament_ids))) >= 4
        for existing in selected
    )


def _select_diverse_candidates(
    candidates: List[PaletteCandidate],
    top_k: int,
) -> List[PaletteCandidate]:
    """Select best-first candidates with a two-member diversity floor.

    If the candidate pool cannot satisfy the requested count while preserving
    diversity, fill best-first from rejected distinct palettes. User-requested
    count outranks diversity, but duplicates are never returned.
    """
    requested = max(0, int(top_k))
    if requested == 0:
        return []

    selected: List[PaletteCandidate] = []
    rejected: List[PaletteCandidate] = []
    seen: set[frozenset[str]] = set()

    for cand in sorted(candidates, key=_candidate_sort_key):
        palette_set = frozenset(cand.filament_ids)
        if palette_set in seen:
            continue
        seen.add(palette_set)
        if _palette_differs_by_at_least_two_members(cand, selected):
            selected.append(cand)
            if len(selected) >= requested:
                return selected
        else:
            rejected.append(cand)

    for cand in rejected:
        if len(selected) >= requested:
            break
        selected.append(cand)

    return selected


def _thorough_search(
    sig: ColorSignature,
    filament_ids: List[str],
    single_gamuts: Dict[str, np.ndarray],
    profiles: Dict[str, dict],
    wb_profile: dict,
    wc_profile: dict,
    d_wb: float,
    d_wc_min: float,
    layer_height: float,
    max_layers: int,
    d_wc_max: float,
    n_filaments: int,
    top_k: int,
    include_pairs: bool,
    t_max: Optional[float],
    de_threshold: float,
    progress: Optional[Callable[[str, float], None]],
    cancel: Optional[Callable[[], bool]],
    verbose: bool,
    dist_single: Optional[Dict[str, np.ndarray]] = None,
    dist_pair: Optional[Dict[Tuple[str, str], np.ndarray]] = None,
    ranking_mode: str = "mean",
) -> List[PaletteCandidate]:
    """
    Thorough search: exhaustive enumeration when feasible, otherwise
    multi-start greedy from the best individual filaments.
    """
    from math import comb

    pair_cache: Dict[Tuple[str, str], np.ndarray] = {}
    n_available = len(filament_ids)
    n_combos = comb(n_available, min(n_filaments, n_available))

    if n_combos <= _EXHAUSTIVE_COMBO_LIMIT:
        return _exhaustive_search(
            sig, filament_ids, single_gamuts, profiles,
            wb_profile, wc_profile, d_wb, d_wc_min, layer_height, max_layers,
            d_wc_max, n_filaments, top_k, include_pairs, t_max,
            de_threshold, pair_cache, progress, cancel, verbose,
            dist_single=dist_single, dist_pair=dist_pair,
            ranking_mode=ranking_mode,
        )
    else:
        return _multi_start_search(
            sig, filament_ids, single_gamuts, profiles,
            wb_profile, wc_profile, d_wb, d_wc_min, layer_height, max_layers,
            d_wc_max, n_filaments, top_k, include_pairs, t_max,
            de_threshold, pair_cache, progress, cancel, verbose,
            dist_single=dist_single, dist_pair=dist_pair,
            ranking_mode=ranking_mode,
        )


def _exhaustive_search(
    sig, filament_ids, single_gamuts, profiles,
    wb_profile, wc_profile, d_wb, d_wc_min, layer_height, max_layers,
    d_wc_max, n_filaments, top_k, include_pairs, t_max,
    de_threshold, pair_cache, progress, cancel, verbose,
    dist_single=None, dist_pair=None,
    ranking_mode: str = "mean",
) -> List[PaletteCandidate]:
    """Enumerate all C(N, k) combinations and return diverse best candidates."""
    from math import comb

    k = min(n_filaments, len(filament_ids))
    total = comb(len(filament_ids), k)
    best: List[PaletteCandidate] = []
    use_precomputed = dist_single is not None
    buffer_size = _candidate_buffer_size(top_k)

    if verbose:
        print(f"Exhaustive search: {total:,} combinations of {k} from {len(filament_ids)}")

    if use_precomputed:
        ids = list(filament_ids)
        id_to_idx = {fid: idx for idx, fid in enumerate(ids)}
        K = len(sig.centroids)
        weights = np.asarray(sig.weights, dtype=np.float64)
        single_matrix = np.stack(
            [np.asarray(dist_single[fid], dtype=np.float32) for fid in ids],
            axis=0,
        )
        pair_matrix = None
        pair_index = np.full((len(ids), len(ids)), -1, dtype=np.int32)
        _dist_pair = dist_pair or {}
        if include_pairs and _dist_pair:
            pair_vectors = []
            for fid_a, fid_b in itertools.combinations(ids, 2):
                key = (min(fid_a, fid_b), max(fid_a, fid_b))
                pair_index[id_to_idx[fid_a], id_to_idx[fid_b]] = len(pair_vectors)
                pair_index[id_to_idx[fid_b], id_to_idx[fid_a]] = len(pair_vectors)
                pair_vectors.append(
                    np.asarray(
                        _dist_pair.get(key, np.full(K, np.inf, dtype=np.float32)),
                        dtype=np.float32,
                    )
                )
            if pair_vectors:
                pair_matrix = np.stack(pair_vectors, axis=0)

        combo_iter = itertools.combinations(range(len(ids)), k)
        processed = 0
        while True:
            batch_indices = list(itertools.islice(combo_iter, _EXHAUSTIVE_SCORE_CHUNK_SIZE))
            if not batch_indices:
                break
            if cancel and cancel():
                if verbose:
                    print(f"  Cancelled at {processed:,} / {total:,}")
                break

            combo_idx = np.asarray(batch_indices, dtype=np.int32)
            d_min = np.min(single_matrix[combo_idx], axis=1)
            if pair_matrix is not None and k >= 2:
                pair_rows = np.stack(
                    [
                        pair_index[combo_idx[:, a], combo_idx[:, b]]
                        for a, b in itertools.combinations(range(k), 2)
                    ],
                    axis=1,
                )
                if np.any(pair_rows >= 0):
                    pair_min = np.min(pair_matrix[pair_rows], axis=1)
                    d_min = np.minimum(d_min, pair_min)

            mean_de = d_min @ weights
            max_de = np.max(d_min, axis=1)
            pct = (d_min > float(de_threshold)) @ weights * 100.0
            order = np.argsort(d_min, axis=1)
            sorted_d = np.take_along_axis(d_min, order, axis=1)
            sorted_w = weights[order]
            cdf = np.cumsum(sorted_w, axis=1)
            p90_idx = np.argmax(cdf >= 0.9, axis=1)
            p90 = sorted_d[np.arange(len(batch_indices)), p90_idx]
            if ranking_mode == "robust":
                rank_score = (
                    mean_de
                    + 0.35 * p90
                    + 0.15 * max_de
                    + 0.03 * (pct / 100.0)
                )
            else:
                rank_score = mean_de

            batch: List[PaletteCandidate] = []
            for row, combo in enumerate(batch_indices):
                trial = [ids[i] for i in combo]
                gamut_points = sum(len(single_gamuts.get(f, [])) for f in trial)
                batch.append(
                    PaletteCandidate(
                        filament_ids=trial,
                        mean_de=float(mean_de[row]),
                        max_de=float(max_de[row]),
                        pct_above_threshold=float(pct[row]),
                        gamut_points=gamut_points,
                        p90_de=float(p90[row]),
                        rank_score=float(rank_score[row]),
                        rank_mode=ranking_mode,
                    )
                )

            best.extend(batch)
            if len(best) > buffer_size * 4:
                best.sort(key=_candidate_sort_key)
                best = best[:buffer_size]

            processed += len(batch_indices)
            if progress:
                progress(f"Evaluating {processed:,} / {total:,}", processed / max(1, total))

        best.sort(key=_candidate_sort_key)
        return _select_diverse_candidates(best[:buffer_size], top_k)

    for idx, combo in enumerate(itertools.combinations(filament_ids, k)):
        if cancel and cancel():
            if verbose:
                print(f"  Cancelled at {idx:,} / {total:,}")
            break

        if progress and idx % 100 == 0:
            progress(f"Evaluating {idx:,} / {total:,}", idx / total)

        trial = list(combo)

        if use_precomputed:
            gamut_points = sum(len(single_gamuts.get(f, [])) for f in trial)
            cand = _make_palette_candidate(
                trial,
                sig,
                dist_single,
                dist_pair or {},
                de_threshold,
                gamut_points,
                ranking_mode,
            )
        else:
            gamut = _build_palette_gamut(
                trial, single_gamuts, profiles,
                wb_profile, wc_profile, d_wb, layer_height, max_layers,
                d_wc_max, include_pairs=include_pairs, pair_cache=pair_cache,
                t_max=t_max, d_wc_min=d_wc_min,
            )
            if len(gamut) == 0:
                continue
            tree = KDTree(gamut)
            mean_de, max_de, pct = _weighted_mean_de(sig, tree, de_threshold=de_threshold)
            gamut_points = len(gamut)
            cand = PaletteCandidate(
                filament_ids=trial,
                mean_de=mean_de,
                max_de=max_de,
                pct_above_threshold=pct,
                gamut_points=gamut_points,
                rank_score=mean_de,
                rank_mode="mean",
            )

        # Maintain a sorted candidate buffer for the diversity walk.
        best.append(cand)
        if len(best) > buffer_size * 4:
            best.sort(key=_candidate_sort_key)
            best = best[:buffer_size]

    best.sort(key=_candidate_sort_key)
    return _select_diverse_candidates(best[:buffer_size], top_k)


def _multi_start_search(
    sig, filament_ids, single_gamuts, profiles,
    wb_profile, wc_profile, d_wb, d_wc_min, layer_height, max_layers,
    d_wc_max, n_filaments, top_k, include_pairs, t_max,
    de_threshold, pair_cache, progress, cancel, verbose,
    dist_single=None, dist_pair=None,
    ranking_mode: str = "mean",
) -> List[PaletteCandidate]:
    """Multi-start greedy: seed from top individual filaments."""
    use_precomputed = dist_single is not None
    _dp = dist_pair or {}

    # Rank filaments by individual coverage
    ranked = []
    for fid in filament_ids:
        if fid not in single_gamuts or len(single_gamuts[fid]) == 0:
            continue
        if use_precomputed:
            mean_de, max_de, pct, p90 = _score_palette_metrics(
                [fid], sig, dist_single, _dp, de_threshold
            )
            score = _palette_rank_score(
                mean_de=mean_de,
                max_de=max_de,
                pct_above_threshold=pct,
                p90_de=p90,
                mode=ranking_mode,
            )
        else:
            tree = KDTree(single_gamuts[fid])
            mean_de, _, _ = _weighted_mean_de(sig, tree)
            score = mean_de
        ranked.append((score, fid))
    ranked.sort()

    n_starts = min(8, len(ranked))
    all_candidates: List[PaletteCandidate] = []
    existing_sets: set = set()
    target_buffer = _candidate_buffer_size(top_k)
    try:
        from math import comb

        target_buffer = min(target_buffer, comb(len(filament_ids), min(n_filaments, len(filament_ids))))
    except ValueError:
        target_buffer = 0

    for start_idx in range(n_starts):
        if cancel and cancel():
            if verbose:
                print(f"  Cancelled at start {start_idx + 1} / {n_starts}")
            break

        if progress:
            best_so_far = min(
                (
                    c.rank_score if c.rank_score is not None else c.mean_de
                    for c in all_candidates
                ),
                default=float("inf"),
            )
            progress(
                f"Start {start_idx + 1}/{n_starts}, best score: {best_so_far:.4f}",
                (start_idx + 1) / n_starts,
            )

        seed_fid = ranked[start_idx][1]

        # Greedy selection seeded with this filament
        selected = [seed_fid]
        remaining = set(filament_ids) - {seed_fid}

        for step in range(min(n_filaments - 1, len(remaining))):
            best_fid = None
            best_score = np.inf
            for candidate_fid in remaining:
                trial = selected + [candidate_fid]
                if use_precomputed:
                    mean_de, max_de, pct, p90 = _score_palette_metrics(
                        trial, sig, dist_single, _dp, de_threshold
                    )
                    score = _palette_rank_score(
                        mean_de=mean_de,
                        max_de=max_de,
                        pct_above_threshold=pct,
                        p90_de=p90,
                        mode=ranking_mode,
                    )
                else:
                    gamut = _build_palette_gamut(
                        trial, single_gamuts, profiles,
                        wb_profile, wc_profile, d_wb, layer_height, max_layers,
                        d_wc_max, include_pairs=include_pairs, pair_cache=pair_cache,
                        t_max=t_max, d_wc_min=d_wc_min,
                    )
                    tree = KDTree(gamut)
                    de, _, _ = _weighted_mean_de(sig, tree)
                    score = de
                if score < best_score:
                    best_score = score
                    best_fid = candidate_fid
            if best_fid is None:
                break
            selected.append(best_fid)
            remaining.remove(best_fid)

        # Local swap refinement
        improved = True
        iters = 0
        while improved and iters < 20:
            improved = False
            iters += 1
            for i in range(len(selected)):
                if use_precomputed:
                    mean_now, max_now, pct_now, p90_now = _score_palette_metrics(
                        selected, sig, dist_single, _dp, de_threshold
                    )
                    score_now = _palette_rank_score(
                        mean_de=mean_now,
                        max_de=max_now,
                        pct_above_threshold=pct_now,
                        p90_de=p90_now,
                        mode=ranking_mode,
                    )
                else:
                    gamut_now = _build_palette_gamut(
                        selected, single_gamuts, profiles,
                        wb_profile, wc_profile, d_wb, layer_height, max_layers,
                        d_wc_max, include_pairs=include_pairs, pair_cache=pair_cache,
                        t_max=t_max, d_wc_min=d_wc_min,
                    )
                    tree_now = KDTree(gamut_now)
                    de_now, _, _ = _weighted_mean_de(sig, tree_now)
                    score_now = de_now

                for swap_fid in list(remaining):
                    trial = selected.copy()
                    trial[i] = swap_fid
                    if use_precomputed:
                        mean_trial, max_trial, pct_trial, p90_trial = _score_palette_metrics(
                            trial, sig, dist_single, _dp, de_threshold
                        )
                        score_trial = _palette_rank_score(
                            mean_de=mean_trial,
                            max_de=max_trial,
                            pct_above_threshold=pct_trial,
                            p90_de=p90_trial,
                            mode=ranking_mode,
                        )
                    else:
                        gamut_trial = _build_palette_gamut(
                            trial, single_gamuts, profiles,
                            wb_profile, wc_profile, d_wb, layer_height, max_layers,
                            d_wc_max, include_pairs=include_pairs, pair_cache=pair_cache,
                            t_max=t_max, d_wc_min=d_wc_min,
                        )
                        tree_trial = KDTree(gamut_trial)
                        de_trial, _, _ = _weighted_mean_de(sig, tree_trial)
                        score_trial = de_trial
                    if score_trial < score_now - 0.001:
                        remaining.remove(swap_fid)
                        remaining.add(selected[i])
                        selected[i] = swap_fid
                        score_now = score_trial
                        improved = True

        # Add result if unique
        palette_set = frozenset(selected)
        if palette_set not in existing_sets:
            existing_sets.add(palette_set)
            if use_precomputed:
                gamut_points = sum(len(single_gamuts.get(f, [])) for f in selected)
                cand = _make_palette_candidate(
                    selected,
                    sig,
                    dist_single,
                    _dp,
                    de_threshold,
                    gamut_points,
                    ranking_mode,
                )
            else:
                gamut_final = _build_palette_gamut(
                    selected, single_gamuts, profiles,
                    wb_profile, wc_profile, d_wb, layer_height, max_layers,
                    d_wc_max, include_pairs=include_pairs, pair_cache=pair_cache,
                    t_max=t_max, d_wc_min=d_wc_min,
                )
                tree_final = KDTree(gamut_final)
                mean_de, max_de, pct = _weighted_mean_de(sig, tree_final, de_threshold=de_threshold)
                gamut_points = len(gamut_final)
                cand = PaletteCandidate(
                    filament_ids=list(selected),
                    mean_de=mean_de,
                    max_de=max_de,
                    pct_above_threshold=pct,
                    gamut_points=gamut_points,
                    rank_score=mean_de,
                    rank_mode="mean",
                )
            all_candidates.append(cand)

    # Generate a candidate buffer for final diversity selection.  Honor the
    # requested-count contract (user directive, 2026-06-12): N requested means
    # N DISTINCT palettes whenever the pool allows.  Multi-start greedy can
    # converge to the same optimum from many seeds — increasingly so now that
    # the scoring domain is clean — and near-equal color error is NOT
    # user-equivalence (color distance metrics are not the final word; the
    # correct palette is the one the user chooses).  Fill the buffer with the
    # best distinct single-swap neighbors of the palettes already found.
    if use_precomputed and len(all_candidates) < target_buffer:
        pool_ids = [
            fid for fid in filament_ids
            if fid in single_gamuts and len(single_gamuts[fid]) > 0
        ]
        frontier = list(all_candidates)
        while len(all_candidates) < target_buffer and frontier:
            batch: List[PaletteCandidate] = []
            batch_seen: set = set()
            for base in frontier:
                base_ids = list(base.filament_ids)
                for i in range(len(base_ids)):
                    for swap_fid in pool_ids:
                        if swap_fid in base_ids:
                            continue
                        trial = base_ids.copy()
                        trial[i] = swap_fid
                        trial_set = frozenset(trial)
                        if trial_set in existing_sets or trial_set in batch_seen:
                            continue
                        batch_seen.add(trial_set)
                        gamut_points = sum(len(single_gamuts.get(f, [])) for f in trial)
                        batch.append(
                            _make_palette_candidate(
                                trial, sig, dist_single, _dp, de_threshold,
                                gamut_points, ranking_mode,
                            )
                        )
            if not batch:
                break
            batch.sort(key=_candidate_sort_key)
            take = batch[: target_buffer - len(all_candidates)]
            for cand in take:
                existing_sets.add(frozenset(cand.filament_ids))
                all_candidates.append(cand)
            frontier = take

    all_candidates.sort(key=_candidate_sort_key)
    return _select_diverse_candidates(all_candidates, top_k)


def _prepare_palette_search_context(
    sig: ColorSignature,
    *,
    progress: Optional[Callable[[str, float], None]] = None,
    cancel: Optional[Callable[[], bool]] = None,
    filament_ids: Optional[List[str]] = None,
    exclude_filament_ids: Optional[set] = None,
    wb_profile: Optional[dict] = None,
    wc_profile: Optional[dict] = None,
    d_wb: float = 0.20,
    d_wc_min: float = 0.08,
    layer_height: float = 0.08,
    max_layers: int = 25,
    include_pairs: bool = True,
    t_max: Optional[float] = None,
    gamut_luminance_weight: float = 1.0,
    profiles_dir: Optional[Path] = None,
    gamut_backend: Optional[PaletteGamutBackend] = None,
    ranking_mode: Optional[str] = None,
    verbose: bool = True,
) -> _PaletteSearchContext:
    if wb_profile is None:
        wb_profile = load_profile("panchroma-matte-cotton-white")
    if wc_profile is None:
        wc_profile = wb_profile

    _pdir = profiles_dir or PROFILES_DIR
    if filament_ids is None:
        filament_ids = sorted(
            p.stem for p in _pdir.glob("*.json")
            if p.stem != "panchroma-matte-cotton-white"
        )
    if gamut_backend is not None:
        filament_ids = [fid for fid in filament_ids if gamut_backend.supports(fid)]
    if exclude_filament_ids:
        filament_ids = [fid for fid in filament_ids if fid not in exclude_filament_ids]
    if not filament_ids:
        raise ValueError("No supported color filaments are available for palette suggestion")

    gamut_domain = _assert_signature_gamut_domain_match(sig, gamut_backend)
    if ranking_mode is None:
        ranking_mode = "robust" if gamut_backend is not None else "mean"
    ranking_mode = str(ranking_mode or "mean").strip().lower()
    if ranking_mode not in {"mean", "robust"}:
        raise ValueError("ranking_mode must be 'mean' or 'robust'")

    if verbose:
        print(f"Palette selection: {len(filament_ids)} filaments available, "
              f"domain = {gamut_domain}")

    d_wc_max = derive_d_wc_max(wc_profile, layer_height=layer_height)
    if t_max is None:
        t_max = d_wc_max + max_layers * layer_height

    profiles = {} if gamut_backend is not None else load_profiles(filament_ids, profiles_dir=_pdir)
    single_gamuts: Dict[str, np.ndarray] = {}
    for idx, fid in enumerate(filament_ids):
        if cancel and cancel():
            break
        if progress and idx % 5 == 0:
            progress(
                f"Computing single gamuts {idx + 1}/{len(filament_ids)}",
                0.20 * idx / max(1, len(filament_ids)),
            )
        if gamut_backend is not None:
            single_gamuts[fid] = gamut_backend.single_gamut(fid)
        else:
            single_gamuts[fid] = _compute_single_filament_gamut(
                profiles[fid], wb_profile, wc_profile,
                d_wb=d_wb, d_wc_min=d_wc_min, layer_height=layer_height,
                max_layers=max_layers, d_wc_max=d_wc_max,
                t_max=t_max,
            )

    if verbose:
        print(f"  Single-filament gamuts: {len(filament_ids)} filaments, "
              f"~{np.mean([len(g) for g in single_gamuts.values()]):.0f} points each")

    dist_single, dist_pair = _precompute_centroid_distances(
        sig, filament_ids, single_gamuts, profiles,
        wb_profile, wc_profile, d_wb, d_wc_min, layer_height, max_layers, d_wc_max,
        include_pairs=include_pairs, t_max=t_max,
        gamut_luminance_weight=gamut_luminance_weight,
        gamut_backend=gamut_backend,
        progress=progress,
        cancel=cancel,
    )

    return _PaletteSearchContext(
        filament_ids=list(filament_ids),
        single_gamuts=single_gamuts,
        profiles=profiles,
        wb_profile=wb_profile,
        wc_profile=wc_profile,
        d_wb=float(d_wb),
        d_wc_min=float(d_wc_min),
        layer_height=float(layer_height),
        max_layers=int(max_layers),
        d_wc_max=d_wc_max,
        t_max=float(t_max),
        dist_single=dist_single,
        dist_pair=dist_pair,
        ranking_mode=ranking_mode,
        gamut_luminance_weight=float(gamut_luminance_weight),
        gamut_backend=gamut_backend,
    )


def suggest_palettes(
    sig: ColorSignature,
    n_filaments: int = 7,
    top_k: int = 5,
    *,
    progress: Optional[Callable[[str, float], None]] = None,
    cancel: Optional[Callable[[], bool]] = None,
    filament_ids: Optional[List[str]] = None,
    exclude_filament_ids: Optional[set] = None,
    wb_profile: Optional[dict] = None,
    wc_profile: Optional[dict] = None,
    d_wb: float = 0.20,
    d_wc_min: float = 0.08,
    layer_height: float = 0.08,
    max_layers: int = 25,
    include_pairs: bool = True,
    t_max: Optional[float] = None,
    de_threshold: float = SUGGESTION_COVERAGE_DE_THRESHOLD,
    gamut_luminance_weight: float = 1.0,
    profiles_dir: Optional[Path] = None,
    gamut_backend: Optional[PaletteGamutBackend] = None,
    ranking_mode: Optional[str] = None,
    verbose: bool = True,
) -> List[PaletteCandidate]:
    """
    Suggest the best filament palettes for an image.

    Parameters
    ----------
    sig           : color signature from extract_color_signature()
    n_filaments   : target palette size (color filaments, not counting white)
    top_k         : number of candidate palettes to return
    filament_ids  : available filaments (default: all profiled filaments)
    include_pairs : include 2-filament gamut sampling in evaluation (slower but
                    captures mixed colors like green from C+Y)
    t_max         : total print height budget (mm). When None, auto-derived as
                    d_wc_max + max_layers * layer_height (matching LUT builder).
    de_threshold  : OKLab distance below which a pixel is considered "in gamut"
    verbose       : print progress

    Returns
    -------
    List of PaletteCandidate, sorted by mean_de (best first).
    """
    operation_progress = _scoped_progress(progress, 0.0, 1.0)
    prepare_progress = _scoped_progress(operation_progress, 0.0, 0.65)
    search_progress = _scoped_progress(operation_progress, 0.65, 0.99)
    ctx = _prepare_palette_search_context(
        sig,
        progress=prepare_progress,
        cancel=cancel,
        filament_ids=filament_ids,
        exclude_filament_ids=exclude_filament_ids,
        wb_profile=wb_profile,
        wc_profile=wc_profile,
        d_wb=d_wb,
        d_wc_min=d_wc_min,
        layer_height=layer_height,
        max_layers=max_layers,
        include_pairs=include_pairs,
        t_max=t_max,
        gamut_luminance_weight=gamut_luminance_weight,
        profiles_dir=profiles_dir,
        gamut_backend=gamut_backend,
        ranking_mode=ranking_mode,
        verbose=verbose,
    )

    candidates = _thorough_search(
        sig, ctx.filament_ids, ctx.single_gamuts, ctx.profiles,
        ctx.wb_profile, ctx.wc_profile, d_wb, d_wc_min, layer_height, max_layers,
        ctx.d_wc_max, n_filaments,
        top_k,
        include_pairs, ctx.t_max, de_threshold,
        search_progress, cancel, verbose,
        dist_single=ctx.dist_single, dist_pair=ctx.dist_pair,
        ranking_mode=ctx.ranking_mode,
    )
    if operation_progress:
        operation_progress("Palette suggestions complete", 1.0)

    if verbose:
        print(f"\nPalette candidates ({len(candidates)}):")
        print(f"  {'#':<3} {'Size':>4} {'Mean dE':>8} {'Max dE':>8} "
              f"{'dE>JND':>6} {'Filaments'}")
        print("  " + "-" * 80)
        for i, c in enumerate(candidates):
            print(f"  {i+1:<3} {len(c.filament_ids):>4} {c.mean_de:>8.4f} "
                  f"{c.max_de:>8.4f} {c.pct_above_threshold:>5.1f}% "
                  f"{', '.join(c.filament_ids)}")

    return candidates


def _build_palette_gamut(
    selected: List[str],
    single_gamuts: Dict[str, np.ndarray],
    profiles: Dict[str, dict],
    wb_profile: dict,
    wc_profile: dict,
    d_wb: float,
    layer_height: float,
    max_layers: int,
    d_wc_max: float,
    include_pairs: bool = True,
    pair_cache: Optional[Dict[Tuple[str, str], np.ndarray]] = None,
    t_max: Optional[float] = None,
    d_wc_min: float = 0.08,
    gamut_backend: Optional[PaletteGamutBackend] = None,
) -> np.ndarray:
    """Build combined OKLab gamut for a palette (singles + optional pairs)."""
    parts = [single_gamuts[fid] for fid in selected if fid in single_gamuts]

    if include_pairs and len(selected) >= 2:
        for fid_a, fid_b in itertools.combinations(selected, 2):
            key = (min(fid_a, fid_b), max(fid_a, fid_b))
            if pair_cache is not None and key in pair_cache:
                parts.append(pair_cache[key])
            else:
                if gamut_backend is not None:
                    pair_gamut = gamut_backend.pair_gamut(fid_a, fid_b)
                else:
                    pair_gamut = _compute_pair_gamut(
                        profiles[fid_a], profiles[fid_b],
                        wb_profile, wc_profile,
                        d_wb=d_wb, d_wc_min=d_wc_min, layer_height=layer_height,
                        max_layers=max_layers, d_wc_max=d_wc_max,
                        t_max=t_max,
                    )
                if pair_cache is not None:
                    pair_cache[key] = pair_gamut
                parts.append(pair_gamut)

    if not parts:
        return np.zeros((0, 3), dtype=np.float32)

    return np.concatenate(parts, axis=0)


# ── Swap-tier aware palette suggestion ───────────────────────────────────────

@dataclass
class SwapTierResult:
    """Result for one swap tier in the tier sweep."""
    swap_count: int                     # 0 = single AMS load, 1 = one swap, etc.
    n_filaments: int                    # max filaments available at this tier
    candidates: List[PaletteCandidate]  # best palettes for this tier
    best_mean_de: float                 # best candidate's mean dE
    best_coverage_pct: float            # best candidate's coverage (100 - pct_above_threshold)
    improvement_over_prev: Optional[float]  # coverage gain vs previous tier (%)


@dataclass
class PaletteSuggestionSweep:
    """Tier-ladder result plus recommended-size alternatives."""
    tiers: List[SwapTierResult]
    alternatives: List[PaletteCandidate]
    recommended: Optional[dict]
    candidates_by_size: Dict[int, List[PaletteCandidate]] = field(default_factory=dict)
    model_metadata: Dict[str, object] = field(default_factory=dict)
    per_load_capped: Optional[dict] = None


def _palette_coverage_pct(candidate: PaletteCandidate) -> float:
    return 100.0 - float(candidate.pct_above_threshold)


def _tier_palette_sizes(tier: int, per_load: int, available_count: int) -> List[int]:
    if tier < 0 or per_load <= 0 or available_count <= 0:
        return []
    start = per_load if tier == 0 else per_load * tier + 1
    end = per_load * (tier + 1)
    if start > available_count:
        return []
    return list(range(start, min(end, available_count) + 1))


def _recommended_ladder_size(
    ladder: List[tuple[int, int, PaletteCandidate]],
    improvement_threshold: float,
) -> tuple[int, int, PaletteCandidate]:
    if not ladder:
        raise ValueError("Cannot recommend from an empty palette ladder")
    ordered = sorted(ladder, key=lambda item: item[0])
    threshold = max(0.0, float(improvement_threshold))
    for idx, (size, tier, candidate) in enumerate(ordered):
        coverage = _palette_coverage_pct(candidate)
        if not any(
            _palette_coverage_pct(later_candidate) - coverage
            >= threshold * float(later_size - size)
            for later_size, _later_tier, later_candidate in ordered[idx + 1:]
        ):
            return size, tier, candidate
    return ordered[-1]


def _palette_set_key(candidate_or_ids) -> Tuple[str, ...]:
    ids = getattr(candidate_or_ids, "filament_ids", candidate_or_ids)
    return tuple(sorted(str(fid) for fid in ids))


def _find_candidate_by_set(
    candidates: List[PaletteCandidate],
    filament_ids,
) -> Optional[PaletteCandidate]:
    key = _palette_set_key(filament_ids)
    for candidate in candidates:
        if _palette_set_key(candidate) == key:
            return candidate
    return None


def _priority_finalists_for_three_color_rescore(
    sweep: PaletteSuggestionSweep,
    *,
    max_finalists: int = 30,
) -> List[PaletteCandidate]:
    """Return unique finalists in the P3 mandated rescore priority order."""
    ordered: List[PaletteCandidate] = []
    ladder: List[PaletteCandidate] = [
        candidate
        for tier in sorted(sweep.tiers, key=lambda item: item.swap_count)
        for candidate in tier.candidates
    ]
    if sweep.recommended:
        recommended = _find_candidate_by_set(ladder + sweep.alternatives, sweep.recommended.get("filament_ids", []))
        if recommended is not None:
            ordered.append(recommended)
    ordered.extend(ladder)
    ordered.extend(sweep.alternatives)

    selected: List[PaletteCandidate] = []
    seen: set[Tuple[str, ...]] = set()
    for candidate in ordered:
        key = _palette_set_key(candidate)
        if key in seen:
            continue
        selected.append(candidate)
        seen.add(key)
        if len(selected) >= max_finalists:
            break
    return selected


def _triple_gamut_for_key(
    key: Tuple[str, str, str],
    context: _PaletteSearchContext,
) -> np.ndarray:
    backend = context.gamut_backend
    if backend is not None and hasattr(backend, "triple_gamut"):
        return backend.triple_gamut(*key)
    return _compute_triple_gamut(
        context.profiles[key[0]],
        context.profiles[key[1]],
        context.profiles[key[2]],
        context.wb_profile,
        context.wc_profile,
        d_wb=context.d_wb,
        d_wc_min=context.d_wc_min,
        layer_height=context.layer_height,
        max_layers=context.max_layers,
        d_wc_max=context.d_wc_max,
        n_samples=4,
        cap_step=8,
        t_max=context.t_max,
    )


def _precompute_triple_centroid_distances(
    sig: ColorSignature,
    finalists: List[PaletteCandidate],
    context: _PaletteSearchContext,
    *,
    progress: Optional[Callable[[str, float], None]] = None,
    cancel: Optional[Callable[[], bool]] = None,
) -> tuple[Dict[Tuple[str, str, str], np.ndarray], Dict[Tuple[str, str, str], int]]:
    triples = sorted({
        tuple(sorted((str(a), str(b), str(c))))
        for candidate in finalists
        for a, b, c in itertools.combinations(candidate.filament_ids, 3)
    })
    query_centroids = _scale_oklab_l(sig.centroids, context.gamut_luminance_weight)
    distances: Dict[Tuple[str, str, str], np.ndarray] = {}
    point_counts: Dict[Tuple[str, str, str], int] = {}
    for idx, key in enumerate(triples):
        if cancel and cancel():
            break
        if progress and idx % 10 == 0:
            progress(
                f"Rescoring triple gamuts {idx + 1}/{len(triples)}",
                idx / max(1, len(triples)),
            )
        gamut = _triple_gamut_for_key(key, context)
        point_counts[key] = int(len(gamut))
        if len(gamut) == 0:
            distances[key] = np.full(len(sig.centroids), np.inf, dtype=np.float32)
            continue
        tree = KDTree(_scale_oklab_l(gamut, context.gamut_luminance_weight))
        dists, _ = tree.query(query_centroids, k=1)
        distances[key] = dists.astype(np.float32)
    return distances, point_counts


def _rescore_candidate_with_triples(
    candidate: PaletteCandidate,
    sig: ColorSignature,
    context: _PaletteSearchContext,
    triple_distances: Dict[Tuple[str, str, str], np.ndarray],
    triple_point_counts: Dict[Tuple[str, str, str], int],
    *,
    de_threshold: float,
) -> PaletteCandidate:
    d_min = _palette_distance_vector(
        candidate.filament_ids,
        context.dist_single,
        context.dist_pair,
        len(sig.centroids),
    )
    triple_points = 0
    for key in itertools.combinations(candidate.filament_ids, 3):
        triple_key = tuple(sorted(str(fid) for fid in key))
        dist = triple_distances.get(triple_key)
        if dist is None:
            continue
        np.minimum(d_min, dist, out=d_min)
        triple_points += int(triple_point_counts.get(triple_key, 0))
    mean_de = float(np.average(d_min, weights=sig.weights))
    max_de = float(d_min.max())
    pct = float(sig.weights[d_min > de_threshold].sum() * 100)
    p90 = _weighted_percentile(d_min, sig.weights, 90.0)
    rank_score = _palette_rank_score(
        mean_de=mean_de,
        max_de=max_de,
        pct_above_threshold=pct,
        p90_de=p90,
        mode=context.ranking_mode,
    )
    return PaletteCandidate(
        filament_ids=list(candidate.filament_ids),
        mean_de=mean_de,
        max_de=max_de,
        pct_above_threshold=pct,
        gamut_points=int(candidate.gamut_points) + triple_points,
        p90_de=p90,
        rank_score=rank_score,
        rank_mode=context.ranking_mode,
    )


def _apply_three_color_rescore_to_sweep(
    sweep: PaletteSuggestionSweep,
    sig: ColorSignature,
    context: _PaletteSearchContext,
    *,
    improvement_threshold: float,
    top_k: int,
    de_threshold: float,
    progress: Optional[Callable[[str, float], None]] = None,
    cancel: Optional[Callable[[], bool]] = None,
) -> PaletteSuggestionSweep:
    finalists = _priority_finalists_for_three_color_rescore(sweep)
    if not finalists:
        return sweep

    started = time.perf_counter()
    triple_distances, triple_point_counts = _precompute_triple_centroid_distances(
        sig,
        finalists,
        context,
        progress=progress,
        cancel=cancel,
    )
    rescored_by_key = {
        _palette_set_key(candidate): _rescore_candidate_with_triples(
            candidate,
            sig,
            context,
            triple_distances,
            triple_point_counts,
            de_threshold=de_threshold,
        )
        for candidate in finalists
    }
    elapsed = time.perf_counter() - started

    def replace(candidate: PaletteCandidate) -> PaletteCandidate:
        return rescored_by_key.get(_palette_set_key(candidate), candidate)

    rescored_tiers: List[SwapTierResult] = []
    ladder: List[tuple[int, int, PaletteCandidate]] = []
    prev_coverage: Optional[float] = None
    for tier in sweep.tiers:
        replaced = [replace(candidate) for candidate in tier.candidates]
        replaced_sorted = sorted(replaced, key=_candidate_sort_key)
        tier_best = replaced_sorted[0] if replaced_sorted else None
        if tier_best is None:
            rescored_tiers.append(tier)
            continue
        coverage = _palette_coverage_pct(tier_best)
        improvement = None if prev_coverage is None else coverage - prev_coverage
        rescored_tiers.append(SwapTierResult(
            swap_count=tier.swap_count,
            n_filaments=tier.n_filaments,
            candidates=replaced_sorted,
            best_mean_de=tier_best.mean_de,
            best_coverage_pct=coverage,
            improvement_over_prev=improvement,
        ))
        prev_coverage = coverage
        for candidate in replaced:
            ladder.append((len(candidate.filament_ids), tier.swap_count, candidate))

    candidates_by_size: Dict[int, List[PaletteCandidate]] = {}
    for size, candidates in sweep.candidates_by_size.items():
        rescored = [replace(candidate) for candidate in candidates]
        candidates_by_size[int(size)] = _select_diverse_candidates(
            sorted(rescored, key=_candidate_sort_key),
            max(top_k, len(candidates)),
        )[:len(candidates)]

    recommended = None
    alternatives: List[PaletteCandidate] = []
    if ladder:
        recommended_size, recommended_tier, recommended_candidate = _recommended_ladder_size(
            ladder,
            improvement_threshold,
        )
        recommended = {
            "swap_count": int(recommended_tier),
            "n_filaments": int(recommended_size),
            "filament_ids": list(recommended_candidate.filament_ids),
        }
        alternatives = candidates_by_size.get(recommended_size, [recommended_candidate])
        if alternatives and _palette_set_key(alternatives[0]) != _palette_set_key(recommended_candidate):
            alternatives = [recommended_candidate] + [
                candidate for candidate in alternatives
                if _palette_set_key(candidate) != _palette_set_key(recommended_candidate)
            ]
        alternatives = _select_diverse_candidates(
            sorted(alternatives, key=_candidate_sort_key),
            top_k,
        )

    result = PaletteSuggestionSweep(
        tiers=rescored_tiers,
        alternatives=alternatives[:top_k],
        recommended=recommended,
        candidates_by_size=candidates_by_size,
        model_metadata={
            **sweep.model_metadata,
            "estimated_with_three_color_rescore": True,
            "three_color_rescore_finalists": len(finalists),
            "three_color_rescore_elapsed_s": round(elapsed, 4),
        },
        per_load_capped=sweep.per_load_capped,
    )
    if progress:
        progress("Triple gamut rescore complete", 1.0)
    return result


def suggest_palettes_swap_aware(
    sig: ColorSignature,
    *,
    max_colors_per_load: Optional[int] = None,
    slots_per_ams: int = 4,
    n_ams_units: int = 1,
    reserved_white: int = 1,
    reserved_filler: int = 0,
    max_swaps: int = 2,
    improvement_threshold: float = 2.0,
    force_all_tiers: bool = False,
    top_k: int = 3,
    verbose: bool = True,
    three_color_rescore: bool = True,
    **kwargs,
) -> PaletteSuggestionSweep:
    """
    Sweep across swap tiers (0, 1, 2, ...) and suggest palettes for each.

    Each tier allows one additional AMS load, increasing the number of
    available color filaments. The sweep stops early when adding another
    swap doesn't improve coverage by at least `improvement_threshold` %.

    Parameters
    ----------
    sig                    : color signature from extract_color_signature()
    max_colors_per_load    : requested max colors per AMS load
    slots_per_ams          : slots per AMS unit (typically 4)
    n_ams_units            : number of AMS units connected
    reserved_white         : slots reserved for white filament(s)
    reserved_filler        : slots reserved for translucent filler
    max_swaps              : maximum number of mid-print swaps to consider
    improvement_threshold  : minimum coverage gain (%) to justify another swap
    force_all_tiers        : if True, always evaluate all tiers
    top_k                  : candidates per tier
    verbose                : print progress
    **kwargs               : forwarded to suggest_palettes()

    Returns
    -------
    PaletteSuggestionSweep with tier ladder entries plus top-level alternatives.
    """
    total_slots = slots_per_ams * n_ams_units
    color_slots_per_load = total_slots - reserved_white - reserved_filler

    if color_slots_per_load <= 0:
        raise ValueError(f"No color slots: {total_slots} total - "
                         f"{reserved_white} white - {reserved_filler} filler = "
                         f"{color_slots_per_load}")

    requested_per_load = int(max_colors_per_load or color_slots_per_load)
    per_load = max(1, min(requested_per_load, color_slots_per_load))
    per_load_capped = (
        {"requested": requested_per_load, "capacity": color_slots_per_load}
        if requested_per_load > color_slots_per_load
        else None
    )
    max_swaps = max(0, int(max_swaps))
    threshold = max(0.0, float(improvement_threshold))

    context_kwargs = dict(kwargs)
    progress = context_kwargs.pop("progress", None)
    operation_progress = _scoped_progress(progress, 0.0, 1.0)
    cancel = context_kwargs.pop("cancel", None)
    include_pairs = context_kwargs.pop("include_pairs", True)
    de_threshold = context_kwargs.pop("de_threshold", SUGGESTION_COVERAGE_DE_THRESHOLD)
    prepare_progress = _scoped_progress(operation_progress, 0.0, 0.55)
    context = _prepare_palette_search_context(
        sig,
        progress=prepare_progress,
        cancel=cancel,
        include_pairs=include_pairs,
        verbose=verbose,
        **context_kwargs,
    )

    results: List[SwapTierResult] = []
    ladder: List[tuple[int, int, PaletteCandidate]] = []
    candidates_by_size: Dict[int, List[PaletteCandidate]] = {}
    prev_tier_max_coverage: Optional[float] = None
    planned_search_count = sum(
        len(_tier_palette_sizes(tier, per_load, len(context.filament_ids)))
        for tier in range(max_swaps + 1)
    )
    search_index = 0

    for tier in range(max_swaps + 1):
        sizes = _tier_palette_sizes(tier, per_load, len(context.filament_ids))
        if not sizes:
            break

        if verbose:
            print(f"\n{'='*60}")
            print(f"Swap tier {tier}: evaluating palette sizes "
                  f"{sizes[0]}-{sizes[-1]} (per-load cap {per_load})")
            print(f"{'='*60}")

        tier_candidates: List[PaletteCandidate] = []
        for size in sizes:
            search_start = 0.55 + 0.27 * search_index / max(1, planned_search_count)
            search_end = 0.55 + 0.27 * (search_index + 1) / max(1, planned_search_count)
            search_progress = _scoped_progress(operation_progress, search_start, search_end)
            candidates = _thorough_search(
                sig,
                context.filament_ids,
                context.single_gamuts,
                context.profiles,
                context.wb_profile,
                context.wc_profile,
                kwargs.get("d_wb", 0.20),
                kwargs.get("d_wc_min", 0.08),
                kwargs.get("layer_height", 0.08),
                kwargs.get("max_layers", 25),
                context.d_wc_max,
                size,
                top_k,
                include_pairs,
                context.t_max,
                de_threshold,
                search_progress,
                cancel,
                verbose,
                dist_single=context.dist_single,
                dist_pair=context.dist_pair,
                ranking_mode=context.ranking_mode,
            )
            search_index += 1
            candidates_by_size[size] = candidates
            if candidates:
                best_for_size = candidates[0]
                tier_candidates.append(best_for_size)
                ladder.append((size, tier, best_for_size))

        if not tier_candidates:
            break

        tier_best = tier_candidates[-1]
        tier_max_coverage = _palette_coverage_pct(tier_best)
        improvement = (
            tier_max_coverage - prev_tier_max_coverage
            if prev_tier_max_coverage is not None
            else None
        )

        results.append(SwapTierResult(
            swap_count=tier,
            n_filaments=sizes[-1],
            candidates=tier_candidates,
            best_mean_de=tier_best.mean_de,
            best_coverage_pct=tier_max_coverage,
            improvement_over_prev=improvement,
        ))

        if verbose:
            print(f"\n  Tier {tier} max-size best: mean dE = {tier_best.mean_de:.4f}, "
                  f"coverage = {tier_max_coverage:.1f}%"
                  + (f", improvement = +{improvement:.1f}%" if improvement is not None else ""))

        if tier > 0 and not force_all_tiers:
            if improvement is not None and improvement < threshold * per_load:
                if verbose:
                    print(f"  Stopping: improvement {improvement:.1f}% < "
                          f"threshold {threshold * per_load:.1f}%")
                break

        prev_tier_max_coverage = tier_max_coverage

    if not ladder:
        if operation_progress:
            operation_progress("Palette suggestions complete", 1.0)
        return PaletteSuggestionSweep(
            tiers=results,
            alternatives=[],
            recommended=None,
            per_load_capped=per_load_capped,
        )

    recommended_size, recommended_tier, recommended_candidate = _recommended_ladder_size(
        ladder,
        threshold,
    )
    alternatives = candidates_by_size.get(recommended_size, [recommended_candidate])
    if alternatives and frozenset(alternatives[0].filament_ids) != frozenset(
        recommended_candidate.filament_ids
    ):
        alternatives = [recommended_candidate] + [
            cand for cand in alternatives
            if frozenset(cand.filament_ids) != frozenset(recommended_candidate.filament_ids)
        ]
    sweep = PaletteSuggestionSweep(
        tiers=results,
        alternatives=alternatives[:top_k],
        recommended={
            "swap_count": int(recommended_tier),
            "n_filaments": int(recommended_size),
            "filament_ids": list(recommended_candidate.filament_ids),
        },
        candidates_by_size=candidates_by_size,
        per_load_capped=per_load_capped,
    )
    if not three_color_rescore:
        if operation_progress:
            operation_progress("Palette suggestions complete", 1.0)
        return sweep
    result = _apply_three_color_rescore_to_sweep(
        sweep,
        sig,
        context,
        improvement_threshold=threshold,
        top_k=top_k,
        de_threshold=de_threshold,
        progress=_scoped_progress(operation_progress, 0.82, 0.99),
        cancel=cancel,
    )
    if operation_progress:
        operation_progress("Palette suggestions complete", 1.0)
    return result


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    import argparse
    import json
    from filament_policy import excluded_filament_ids
    p = argparse.ArgumentParser(
        prog="lith_palette",
        description="Suggest optimal filament palettes for a source image",
    )
    p.add_argument("image", help="Source image path")
    p.add_argument("--n-filaments", type=int, default=7,
                   help="Target palette size [7]")
    p.add_argument("--top-k", type=int, default=5,
                   help="Number of candidates to show [5]")
    p.add_argument("--n-clusters", type=int, default=100,
                   help="k-means clusters for color signature [100]")
    p.add_argument("--no-pairs", action="store_true",
                   help="Skip 2-filament gamut sampling (faster, less accurate)")
    args = p.parse_args()

    sig = extract_color_signature(args.image, n_clusters=args.n_clusters)
    print(f"Image: {args.image}  ({sig.n_pixels:,} pixels, "
          f"{len(sig.centroids)} color clusters)")

    # Apply the same generation-availability policy as the web app — the dev CLI
    # must not suggest filaments excluded from model-backed generation.
    _reg_path = PROFILES_DIR.parent / "registry.json"
    _registry = json.loads(_reg_path.read_text(encoding="utf-8")) if _reg_path.exists() else {}
    candidates = suggest_palettes(
        sig,
        n_filaments=args.n_filaments,
        top_k=args.top_k,
        include_pairs=not args.no_pairs,
        exclude_filament_ids=excluded_filament_ids(_registry),
    )


if __name__ == "__main__":
    main()
