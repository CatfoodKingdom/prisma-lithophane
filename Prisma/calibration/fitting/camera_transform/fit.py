"""Faithful v2 Camera Transform fit ported from the sandbox script."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Any

import numpy as np
import pandas as pd
from scipy.optimize import least_squares, minimize
from scipy.interpolate import PchipInterpolator

try:
    from Prisma.lib.camera_transform import (
        N_KNOTS,
        N_PARAMS,
        N_W,
        OUT_MAX,
        X_KNOTS,
        apply_forward,
        apply_rp,
        invert_curve_channel,
        rp_features,
        unpack_params,
    )
except ImportError:  # app context: uvicorn puts Prisma/ on sys.path, not the repo root
    from lib.camera_transform import (
        N_KNOTS,
        N_PARAMS,
        N_W,
        OUT_MAX,
        X_KNOTS,
        apply_forward,
        apply_rp,
        invert_curve_channel,
        rp_features,
        unpack_params,
    )

try:
    from Prisma.lib.transmission import linear_to_oklab
except ImportError:  # app context: uvicorn puts Prisma/ on sys.path, not the repo root
    from lib.transmission import linear_to_oklab

SEED = 42
JPEG_CLIP_THRESHOLD = 250
N_CURVE_PARAMS = N_KNOTS * 3
CV_FOLDS = 5
CV_POLICY = "sample_grouped_5_fold_oof_v1"
_CV_HASH_DOMAIN = "camera_transform_cv_v1"

_M_RGB2XYZ = np.array(
    [
        [0.4124564, 0.3575761, 0.1804375],
        [0.2126729, 0.7151522, 0.0721750],
        [0.0193339, 0.1191920, 0.9503041],
    ],
    dtype=np.float64,
)
_D65 = np.array([0.95047, 1.00000, 1.08883], dtype=np.float64)


@dataclass(frozen=True)
class CameraTransformFitResult:
    params: np.ndarray
    clean: pd.DataFrame
    oof_predictions: pd.DataFrame
    fold_by_sample: dict[str, int]
    metrics: dict[str, Any]
    hygiene: dict[str, int]


def srgb_to_linear(x: np.ndarray) -> np.ndarray:
    arr = np.clip(x, 0.0, 1.0)
    return np.where(arr <= 0.04045, arr / 12.92, ((arr + 0.055) / 1.055) ** 2.4)


def linear_to_srgb(x: np.ndarray) -> np.ndarray:
    arr = np.clip(x, 0.0, 1.0)
    return np.where(arr <= 0.0031308, 12.92 * arr, 1.055 * arr ** (1.0 / 2.4) - 0.055)


def srgb_to_lab(rgb_01: np.ndarray) -> np.ndarray:
    lin = srgb_to_linear(rgb_01)
    xyz = lin @ _M_RGB2XYZ.T
    t = xyz / _D65
    d = 6.0 / 29.0
    ft = np.where(t > d**3, np.cbrt(t), t / (3 * d**2) + 4.0 / 29.0)
    return np.stack([116.0 * ft[:, 1] - 16.0, 500.0 * (ft[:, 0] - ft[:, 1]), 200.0 * (ft[:, 1] - ft[:, 2])], axis=1)


def de76(pred_01: np.ndarray, truth_01: np.ndarray) -> np.ndarray:
    return np.sqrt(np.sum((srgb_to_lab(pred_01) - srgb_to_lab(truth_01)) ** 2, axis=1))


def _decode_knots(raw: np.ndarray) -> np.ndarray:
    with np.errstate(over="ignore", invalid="ignore"):
        y0 = OUT_MAX * 0.01 * (1.0 / (1.0 + np.exp(-raw[0])))
        step_scale = OUT_MAX / (N_KNOTS - 1)
        deltas = np.log1p(np.exp(raw[1:])) * step_scale + 1e-4
        ys = np.empty(N_KNOTS)
        ys[0] = y0
        for i in range(1, N_KNOTS):
            ys[i] = ys[i - 1] + deltas[i - 1]
        if ys[-1] > OUT_MAX:
            ys = ys * (OUT_MAX / ys[-1])
    return ys


def apply_curves_unclipped(z: np.ndarray, curve_params: np.ndarray) -> np.ndarray:
    z_clamped = np.clip(z, 0.0, 1.0)
    out = np.empty_like(z_clamped)
    for c in range(3):
        raw = curve_params[c * N_KNOTS:(c + 1) * N_KNOTS]
        out[:, c] = PchipInterpolator(X_KNOTS, _decode_knots(raw), extrapolate=True)(z_clamped[:, c])
    return out


def load_and_filter(df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, int], int]:
    work = df.copy()
    n_start = len(work)
    drop_counts: dict[str, int] = {}
    if work.empty:
        work["_censored"] = pd.Series(dtype=bool)
        drop_counts.update(
            {
                "rule1_excluded": 0,
                "rule2_low_corr_samples": 0,
                "rule3_suspicious": 0,
                "rule4_nan": 0,
                "rule5_censored_from_loss": 0,
                "start": int(n_start),
                "kept": 0,
            }
        )
        return work, drop_counts, 0
    if "fit_state" in work.columns:
        mask = work["fit_state"] == "excluded"
        drop_counts["rule1_excluded"] = int(mask.sum())
        work = work[~mask].copy()
    low_corr_samples = set(work.loc[work["order_correlation"].abs() < 0.8, "sample_id"].unique()) if "order_correlation" in work.columns else set()
    mask = work["sample_id"].isin(low_corr_samples) if low_corr_samples else np.zeros(len(work), dtype=bool)
    drop_counts["rule2_low_corr_samples"] = int(np.count_nonzero(mask))
    work = work[~mask].copy()
    jpeg_lum = work[["jpeg_r", "jpeg_g", "jpeg_b"]].mean(axis=1)
    mask = (jpeg_lum > 235) & (work["T_G"] < 0.5)
    drop_counts["rule3_suspicious"] = int(mask.sum())
    work = work[~mask].copy()
    mask = work[["T_R", "T_G", "T_B"]].isna().any(axis=1) | work[["jpeg_r", "jpeg_g", "jpeg_b"]].isna().any(axis=1)
    drop_counts["rule4_nan"] = int(mask.sum())
    work = work[~mask].copy()
    clipped = (work["jpeg_r"] >= JPEG_CLIP_THRESHOLD) | (work["jpeg_g"] >= JPEG_CLIP_THRESHOLD) | (work["jpeg_b"] >= JPEG_CLIP_THRESHOLD)
    work["_censored"] = clipped
    drop_counts["rule5_censored_from_loss"] = int(clipped.sum())
    drop_counts["start"] = int(n_start)
    drop_counts["kept"] = int(len(work))
    return work, drop_counts, int(clipped.sum())


def sample_validation_scores(df: pd.DataFrame) -> pd.DataFrame:
    """Return deterministic measured-transmission scores, one row per sample."""

    if df is None or len(df) == 0:
        return pd.DataFrame(columns=["sample_id", "sample_score"])
    required = {"sample_id", "T_R", "T_G", "T_B"}
    missing = sorted(required - set(df.columns))
    if missing:
        raise ValueError(f"Camera Transform validation rows are missing columns: {', '.join(missing)}")
    rows = pd.DataFrame(
        {
            "sample_id": df["sample_id"].astype(str).values,
            "row_score": df[["T_R", "T_G", "T_B"]].astype(np.float64).mean(axis=1).values,
        }
    )
    scores = (
        rows.groupby("sample_id", as_index=False, sort=False)["row_score"]
        .median()
        .rename(columns={"row_score": "sample_score"})
        .sort_values(["sample_score", "sample_id"], kind="mergesort")
        .reset_index(drop=True)
    )
    if not np.isfinite(scores["sample_score"].to_numpy(dtype=np.float64)).all():
        raise ValueError("Camera Transform validation sample scores must be finite")
    return scores


def _validation_hash(seed: int, sample_id: str) -> bytes:
    payload = f"{_CV_HASH_DOMAIN}\0{int(seed)}\0{sample_id}".encode("utf-8")
    return hashlib.sha256(payload).digest()


def assign_validation_folds(
    df: pd.DataFrame,
    *,
    seed: int = SEED,
    n_folds: int = CV_FOLDS,
) -> dict[str, int]:
    """Assign samples to measured-range-balanced deterministic folds."""

    if n_folds < 2:
        raise ValueError("Camera Transform validation requires at least two folds")
    scores = sample_validation_scores(df)
    fold_by_sample: dict[str, int] = {}
    for start in range(0, len(scores), n_folds):
        block = [str(value) for value in scores.iloc[start:start + n_folds]["sample_id"].tolist()]
        ordered = sorted(block, key=lambda sample_id: (_validation_hash(seed, sample_id), sample_id))
        for fold_index, sample_id in enumerate(ordered):
            fold_by_sample[sample_id] = fold_index
    return fold_by_sample


def _init_raw_identity() -> np.ndarray:
    raw = np.empty(N_KNOTS)
    raw[0] = -10.0
    raw[1:] = float(np.log(np.exp(1.0) - 1.0))
    return raw


def _w_identity_init() -> np.ndarray:
    w = np.zeros((3, 6))
    w[0, 0] = 1.0
    w[1, 1] = 1.0
    w[2, 2] = 1.0
    return w


def init_v2() -> np.ndarray:
    return np.concatenate([_w_identity_init().ravel(), np.tile(_init_raw_identity(), 3)])


def als_step_w(t: np.ndarray, y_truth: np.ndarray, curve_params: np.ndarray, censor_mask: np.ndarray) -> np.ndarray:
    y_cl = np.clip(y_truth, 0.0, 1.0)
    z_target = np.empty_like(y_cl)
    for c in range(3):
        raw = curve_params[c * N_KNOTS:(c + 1) * N_KNOTS]
        z_target[:, c] = invert_curve_channel(y_cl[:, c], raw)
    keep = ~censor_mask
    phi = rp_features(t[keep])
    z_t = z_target[keep]
    w_new = np.zeros((3, 6))
    for c in range(3):
        sol, _, _, _ = np.linalg.lstsq(phi, z_t[:, c], rcond=None)
        w_new[c] = sol
    return w_new


def als_step_curves(t: np.ndarray, y_truth: np.ndarray, w: np.ndarray, censor_mask: np.ndarray, *, n_iter_inner: int = 1000) -> np.ndarray:
    z = np.clip(apply_rp(t, w), 0.0, 1.0)
    keep = ~censor_mask
    z_keep = z[keep]
    y_keep = np.clip(y_truth[keep], 0.0, 1.0)
    curve_params_new = np.empty(N_CURVE_PARAMS)
    for c in range(3):
        z_c = z_keep[:, c]
        y_c = y_keep[:, c]

        def channel_loss(raw8: np.ndarray) -> float:
            pred = np.clip(PchipInterpolator(X_KNOTS, _decode_knots(raw8), extrapolate=True)(z_c), 0.0, 1.0)
            return float(np.mean((pred - y_c) ** 2))

        res = minimize(channel_loss, _init_raw_identity().copy(), method="L-BFGS-B", options={"maxiter": n_iter_inner, "ftol": 1e-12, "gtol": 1e-8})
        curve_params_new[c * N_KNOTS:(c + 1) * N_KNOTS] = res.x
    return curve_params_new


def als_fit(t_train: np.ndarray, y_train: np.ndarray, censor_mask_train: np.ndarray, *, n_als: int = 4) -> np.ndarray:
    params = init_v2()
    w, curve_params = unpack_params(params)
    for _ in range(n_als):
        w = als_step_w(t_train, y_train, curve_params, censor_mask_train)
        curve_params = als_step_curves(t_train, y_train, w, censor_mask_train)
    return np.concatenate([w.ravel(), curve_params])


def residuals_v2(params_flat: np.ndarray, t: np.ndarray, y_truth: np.ndarray, censor_mask: np.ndarray) -> np.ndarray:
    w, curve_params = unpack_params(params_flat)
    y_pred = np.clip(apply_curves_unclipped(np.clip(apply_rp(t, w), 0.0, 1.0), curve_params), 0.0, 1.0)
    keep = ~censor_mask
    return (srgb_to_lab(y_pred[keep]) - srgb_to_lab(y_truth[keep])).ravel()


def joint_polish(params_als: np.ndarray, t_train: np.ndarray, y_train: np.ndarray, censor_mask_train: np.ndarray) -> np.ndarray:
    def jac_call(p: np.ndarray) -> np.ndarray:
        return residuals_v2(p, t_train, y_train, censor_mask_train)

    try:
        lm = least_squares(jac_call, params_als, method="lm", loss="linear", ftol=1e-8, xtol=1e-8, gtol=1e-8, max_nfev=5000, verbose=0).x
    except Exception:
        lm = params_als
    try:
        return least_squares(jac_call, lm, method="trf", loss="soft_l1", f_scale=5.0, ftol=1e-8, xtol=1e-8, gtol=1e-8, max_nfev=10000, verbose=0).x
    except Exception:
        return lm


def _stats(values: np.ndarray) -> dict[str, float | int]:
    arr = np.asarray(values, dtype=np.float64)
    if arr.size == 0:
        raise RuntimeError("Camera Transform metrics require at least one value")
    if not np.isfinite(arr).all():
        raise RuntimeError("Camera Transform metrics contain nonfinite values")
    return {
        "mean": float(np.mean(arr)),
        "median": float(np.median(arr)),
        "p90": float(np.percentile(arr, 90)),
        "n": int(arr.size),
    }


def evaluate(params: np.ndarray, df: pd.DataFrame) -> np.ndarray:
    t = df[["T_R", "T_G", "T_B"]].values.astype(np.float64)
    y = df[["jpeg_r", "jpeg_g", "jpeg_b"]].values.astype(np.float64) / 255.0
    return de76(apply_forward(t, params), y)


def _fit_params(df: pd.DataFrame, *, context: str) -> np.ndarray:
    if df is None or len(df) == 0:
        raise RuntimeError(f"Camera Transform {context} has no training rows")
    t_train = df[["T_R", "T_G", "T_B"]].values.astype(np.float64)
    y_train = df[["jpeg_r", "jpeg_g", "jpeg_b"]].values.astype(np.float64) / 255.0
    censor = df["_censored"].values.astype(bool)
    usable_for_loss = int(np.count_nonzero(~censor))
    if usable_for_loss < N_PARAMS:
        raise RuntimeError(
            f"Camera Transform {context} has {usable_for_loss} uncensored training rows; "
            f"at least {N_PARAMS} are required"
        )
    return joint_polish(als_fit(t_train, y_train, censor), t_train, y_train, censor)


def _oklab_distances(pred_srgb: np.ndarray, truth_srgb: np.ndarray) -> np.ndarray:
    pred_ok = linear_to_oklab(srgb_to_linear(pred_srgb))
    truth_ok = linear_to_oklab(srgb_to_linear(truth_srgb))
    return np.sqrt(np.sum((pred_ok - truth_ok) ** 2, axis=1))


def fit_camera_transform(df: pd.DataFrame, *, seed: int = SEED) -> CameraTransformFitResult:
    clean, hygiene, n_censored = load_and_filter(df)
    clean = clean.reset_index(drop=True)
    if clean.empty:
        raise RuntimeError("Camera Transform corpus has no usable rows after hygiene filtering")
    fold_by_sample = assign_validation_folds(clean, seed=seed)
    sample_count = len(fold_by_sample)
    if sample_count < CV_FOLDS:
        raise RuntimeError(
            f"Camera Transform five-fold validation requires at least {CV_FOLDS} eligible samples; "
            f"found {sample_count}"
        )

    fold_series = clean["sample_id"].astype(str).map(fold_by_sample)
    sample_scores = sample_validation_scores(clean).set_index("sample_id")["sample_score"]
    oof_parts: list[pd.DataFrame] = []
    fold_metrics: list[dict[str, Any]] = []
    for fold_index in range(CV_FOLDS):
        validation_mask = fold_series == fold_index
        validation = clean[validation_mask].copy()
        train = clean[~validation_mask].copy()
        if validation.empty:
            raise RuntimeError(f"Camera Transform validation fold {fold_index} has no rows")
        fold_params = _fit_params(train, context=f"validation fold {fold_index}")
        t_validation = validation[["T_R", "T_G", "T_B"]].values.astype(np.float64)
        truth_srgb = validation[["jpeg_r", "jpeg_g", "jpeg_b"]].values.astype(np.float64) / 255.0
        pred_srgb = apply_forward(t_validation, fold_params)
        fold_de = de76(pred_srgb, truth_srgb)
        fold_oklab = _oklab_distances(pred_srgb, truth_srgb)
        validation["validation_fold"] = int(fold_index)
        validation[["pred_srgb_r", "pred_srgb_g", "pred_srgb_b"]] = pred_srgb
        validation["de76_CIELAB"] = fold_de
        validation["oklab_distance"] = fold_oklab
        oof_parts.append(validation)

        validation_sample_ids = sorted(set(validation["sample_id"].astype(str)))
        fold_scores = sample_scores.loc[validation_sample_ids].to_numpy(dtype=np.float64)
        fold_metrics.append(
            {
                "fold": int(fold_index),
                "train_sample_count": int(train["sample_id"].astype(str).nunique()),
                "validation_sample_count": len(validation_sample_ids),
                "train_row_count": int(len(train)),
                "validation_row_count": int(len(validation)),
                "dE76_CIELAB": _stats(fold_de),
                "OKLab": _stats(fold_oklab),
                "sample_score": {
                    "min": float(np.min(fold_scores)),
                    "median": float(np.median(fold_scores)),
                    "max": float(np.max(fold_scores)),
                },
            }
        )

    oof_predictions = pd.concat(oof_parts).sort_index()
    if len(oof_predictions) != len(clean) or not oof_predictions.index.equals(clean.index):
        raise RuntimeError("Camera Transform validation did not produce exactly one prediction per clean row")
    oof_de = oof_predictions["de76_CIELAB"].to_numpy(dtype=np.float64)
    oof_oklab = oof_predictions["oklab_distance"].to_numpy(dtype=np.float64)

    params = _fit_params(clean, context="final all-data fit")
    final_train_de = evaluate(params, clean)
    metrics = {
        "seed": int(seed),
        "jpeg_clip_threshold": JPEG_CLIP_THRESHOLD,
        "n_censored_from_loss": int(n_censored),
        "validation": {
            "method": CV_POLICY,
            "fold_count": CV_FOLDS,
            "dE76_CIELAB": _stats(oof_de),
            "OKLab": _stats(oof_oklab),
            "folds": fold_metrics,
        },
        "final_fit": {
            "sample_count": sample_count,
            "row_count": int(len(clean)),
            "uncensored_row_count": int(np.count_nonzero(~clean["_censored"].to_numpy(dtype=bool))),
            "training_dE76_CIELAB": _stats(final_train_de),
        },
        "n_params": N_PARAMS,
        "used_lattice": False,
    }
    return CameraTransformFitResult(
        params=params,
        clean=clean,
        oof_predictions=oof_predictions,
        fold_by_sample=fold_by_sample,
        metrics=metrics,
        hygiene=hygiene,
    )
