"""Shared circular interpolation over hue angles in degrees.

Wing C operators (notably C2 `c2_soft_gamut_compress`) need to query the
hue-binned chroma envelope at arbitrary OKLab hue angles. The bin centers
in `PaletteMetadata.hue_degrees` are sparse (>= 12 bins per § B.6 sanity)
and live on a circular domain — interpolation MUST wrap continuously
across the 360° → 0° seam.

This helper sits next to `feature_scale.py` as shared infrastructure: it
encodes only the geometric interpolation contract (continuous wrap, exact
sample recovery, dtype/shape preservation) and is independent of any
specific operator's mapping algorithm.
"""
from __future__ import annotations

import numpy as np


def interpolate_circular_degrees(
    query_degrees: np.ndarray,
    sample_degrees: np.ndarray,
    sample_values: np.ndarray,
) -> np.ndarray:
    """Linearly interpolate `sample_values` indexed by hue at `query_degrees`.

    Parameters
    ----------
    query_degrees
        Arbitrary-shape float array of hue angles in degrees. May be < 0 or
        >= 360 — values are normalized modulo 360 before lookup.
    sample_degrees
        1-D float array of bin center hue angles in degrees. Need not be
        sorted; the helper sorts internally. Each value is normalized
        modulo 360.
    sample_values
        1-D array of values aligned 1:1 with `sample_degrees`.

    Returns
    -------
    np.ndarray
        Same shape as `query_degrees`, dtype float32. Value at each query
        is the linear interpolation between the two nearest bin centers
        ON THE CIRCLE — so a query at 0° with bins at 350° and 10° lands
        on the midpoint of those two bins (NOT on the long arc through 180°).

    Degenerate case: if `sample_degrees` has length 1, every query returns
    that bin's value.
    """
    sd = np.asarray(sample_degrees, dtype=np.float32).ravel()
    sv = np.asarray(sample_values, dtype=np.float32).ravel()
    if sd.shape != sv.shape:
        raise ValueError(
            f"sample_degrees and sample_values must align: "
            f"got {sd.shape} vs {sv.shape}"
        )
    n = sd.shape[0]
    if n == 0:
        raise ValueError("sample arrays must be non-empty")

    q = np.asarray(query_degrees, dtype=np.float32)
    out_shape = q.shape
    q_flat = np.mod(q.ravel(), 360.0).astype(np.float32)

    if n == 1:
        return np.full(out_shape, sv[0], dtype=np.float32)

    # Sort sample bins by angle, then build a wrap-extended copy so a
    # standard 1-D piecewise-linear interp handles the seam without
    # branching on each query.
    sd_norm = np.mod(sd, 360.0)
    order = np.argsort(sd_norm)
    sd_sorted = sd_norm[order]
    sv_sorted = sv[order]

    # Extend by one bin on each side: the last bin shifted -360° and the
    # first bin shifted +360°. After this, sd_ext is strictly increasing
    # over (sd_sorted[-1] - 360, sd_sorted[0] + 360], covering [0, 360]
    # with continuous values across the seam.
    sd_ext = np.concatenate(([sd_sorted[-1] - 360.0], sd_sorted, [sd_sorted[0] + 360.0]))
    sv_ext = np.concatenate(([sv_sorted[-1]], sv_sorted, [sv_sorted[0]]))

    interp = np.interp(q_flat, sd_ext, sv_ext).astype(np.float32)
    return interp.reshape(out_shape)
