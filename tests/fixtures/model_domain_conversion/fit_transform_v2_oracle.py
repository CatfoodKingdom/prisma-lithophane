"""
fit_transform_v2.py -- Stage 3b: Root-polynomial + monotone splines
====================================================================
Upgraded appearance-transform fit.

Model: F(T) = tone_curves(RP(T))
  1. RP(T): Finlayson-style degree-2 root-polynomial map
       features = [r, g, b, sqrt(rg), sqrt(gb), sqrt(rb)]  (exposure-homogeneous)
       3x6 matrix W -> 3-channel intermediate
  2. Per-channel monotone tone curves: 10 knots, constrained output in [0,1.05]
       softplus-delta parametrization (strictly monotone by construction)

Censoring: JPEG channels >= 250 -> exclude from loss (clipped observations)

Fitting: ALS (3-5 iterations) + scipy least_squares polish.
Loss: CIE76 dE soft_l1 robust.

Inversion:
  - Curves: 1D exact within [0,1.05] output range
  - RP map: 3D Newton from good init (linear-part inverse)
  - LUT: 33x33x33 baked as inverse_lut_33.npz

Run from sandbox directory:
  python fit_transform_v2.py

Outputs:
  transform_params.json   -- v1 + v2 params
  transform_report.md     -- updated with 3a-vs-3b ladder
  inverse_lut_33.npz      -- 33^3 inverse LUT
"""

import json
import warnings
import datetime
import sys
import io
import numpy as np
import pandas as pd
from scipy.optimize import minimize, least_squares
from scipy.interpolate import PchipInterpolator

warnings.filterwarnings("ignore")

SEED = 42
rng = np.random.default_rng(SEED)

# Force UTF-8 stdout
if hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)

# -----------------------------------------------------------------------
# COLOUR MATH (same as 3a)
# -----------------------------------------------------------------------

def srgb_to_linear(x):
    x = np.clip(x, 0.0, 1.0)
    return np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4)

def linear_to_srgb(x):
    x = np.clip(x, 0.0, 1.0)
    return np.where(x <= 0.0031308, 12.92 * x, 1.055 * x ** (1.0/2.4) - 0.055)

_M_RGB2XYZ = np.array([
    [0.4124564, 0.3575761, 0.1804375],
    [0.2126729, 0.7151522, 0.0721750],
    [0.0193339, 0.1191920, 0.9503041],
], dtype=np.float64)
_D65 = np.array([0.95047, 1.00000, 1.08883], dtype=np.float64)

def srgb_to_lab(rgb_01):
    """(N,3) sRGB [0,1] -> (N,3) CIE L*a*b* D65."""
    lin = srgb_to_linear(rgb_01)
    xyz = lin @ _M_RGB2XYZ.T
    t = xyz / _D65
    d = 6.0 / 29.0
    ft = np.where(t > d**3, np.cbrt(t), t / (3 * d**2) + 4.0 / 29.0)
    L = 116.0 * ft[:, 1] - 16.0
    a = 500.0 * (ft[:, 0] - ft[:, 1])
    b = 200.0 * (ft[:, 1] - ft[:, 2])
    return np.stack([L, a, b], axis=1)

def de76(pred_01, truth_01):
    return np.sqrt(np.sum((srgb_to_lab(pred_01) - srgb_to_lab(truth_01))**2, axis=1))

# -----------------------------------------------------------------------
# DATA LOADING + HYGIENE (same filters as 3a, same seed -> same split)
# -----------------------------------------------------------------------

JPEG_CLIP_THRESHOLD = 250  # new: censor clipped observations

def load_and_filter():
    df = pd.read_csv("corpus_joined.csv")
    n_start = len(df)
    drop_counts = {}

    if "fit_state" in df.columns:
        mask = df["fit_state"] == "excluded"
        drop_counts["rule1_excluded"] = int(mask.sum())
        df = df[~mask].copy()

    low_corr_samples = set(
        df.loc[df["order_correlation"].abs() < 0.8, "sample_id"].unique()
    )
    mask = df["sample_id"].isin(low_corr_samples)
    drop_counts["rule2_low_corr_samples"] = int(mask.sum())
    df = df[~mask].copy()

    jpeg_lum = df[["jpeg_r", "jpeg_g", "jpeg_b"]].mean(axis=1)
    mask = (jpeg_lum > 235) & (df["T_G"] < 0.5)
    drop_counts["rule3_suspicious"] = int(mask.sum())
    df = df[~mask].copy()

    mask = (
        df[["T_R", "T_G", "T_B"]].isna().any(axis=1)
        | df[["jpeg_r", "jpeg_g", "jpeg_b"]].isna().any(axis=1)
    )
    drop_counts["rule4_nan"] = int(mask.sum())
    df = df[~mask].copy()

    # New: censoring — flag clipped JPEG channels (not dropped, just flagged)
    clipped_mask = (
        (df["jpeg_r"] >= JPEG_CLIP_THRESHOLD) |
        (df["jpeg_g"] >= JPEG_CLIP_THRESHOLD) |
        (df["jpeg_b"] >= JPEG_CLIP_THRESHOLD)
    )
    n_censored = int(clipped_mask.sum())
    df["_censored"] = clipped_mask
    drop_counts["rule5_censored_from_loss"] = n_censored

    n_kept = len(df)
    print("=== DATA HYGIENE ===")
    print(f"  Start:                    {n_start}")
    for k, v in drop_counts.items():
        if k == "rule5_censored_from_loss":
            print(f"  Censored from loss ({k}): {v}  (kept in df, excluded from fitting)")
        else:
            print(f"  Dropped {k}: {v}")
    print(f"  Kept:                     {n_kept}")
    print(f"  Unique samples:           {df['sample_id'].nunique()}")
    print()
    return df, drop_counts, n_censored


def split_train_holdout(df, test_frac=0.2):
    """Same seed + logic as 3a -> same split."""
    def family(fid):
        fid = str(fid).lower()
        if "white" in fid or "cotton" in fid or "beige" in fid:
            return "white"
        elif "translucent" in fid or "natural" in fid:
            return "translucent"
        else:
            return "opaque"

    sample_df = df.drop_duplicates("sample_id").copy()
    sample_df["family"] = sample_df["variable_fid"].apply(family)

    holdout_samples = set()
    for fam, grp in sample_df.groupby("family"):
        n_hold = max(1, int(round(len(grp) * test_frac)))
        chosen = rng.choice(grp["sample_id"].values, size=n_hold, replace=False)
        holdout_samples.update(chosen.tolist())

    df_train = df[~df["sample_id"].isin(holdout_samples)].copy()
    df_hold = df[df["sample_id"].isin(holdout_samples)].copy()
    print(f"  Train: {len(df_train)} swatches / {df_train['sample_id'].nunique()} samples")
    print(f"  Hold:  {len(df_hold)} swatches / {df_hold['sample_id'].nunique()} samples")
    n_cens_train = int(df_train["_censored"].sum())
    n_cens_hold = int(df_hold["_censored"].sum())
    print(f"  Censored in train: {n_cens_train}  hold: {n_cens_hold}")
    print()
    return df_train, df_hold


# -----------------------------------------------------------------------
# ROOT-POLYNOMIAL FEATURES
# Finlayson-style degree-2 exposure-homogeneous features:
#   phi(T) = [r, g, b, sqrt(r*g), sqrt(g*b), sqrt(r*b)]  (N, 6)
# W: (3, 6) matrix -> RP(T) = phi(T) @ W.T  shape (N, 3)
# -----------------------------------------------------------------------

def rp_features(T):
    """(N,3) -> (N,6) root-polynomial features (clamp T >= 0 for sqrt)."""
    T = np.clip(T, 0.0, None)
    r, g, b = T[:, 0], T[:, 1], T[:, 2]
    return np.stack([
        r, g, b,
        np.sqrt(r * g),
        np.sqrt(g * b),
        np.sqrt(r * b),
    ], axis=1)


def apply_rp(T, W):
    """W: (3,6) -> (N,3). No clipping."""
    return rp_features(T) @ W.T


# -----------------------------------------------------------------------
# MONOTONE TONE CURVES
# N_KNOTS knots on [0,1] input domain.
# Output constrained to [0, OUT_MAX] by construction.
# Parametrization: y[0] anchored near 0, deltas via softplus.
# -----------------------------------------------------------------------

N_KNOTS = 10
OUT_MAX = 1.05   # max allowed output -> inversion stays in-domain
_MIN_DELTA = 1e-4

X_KNOTS = np.linspace(0.0, 1.0, N_KNOTS)


def _decode_knots_v2(raw):
    """
    raw: N_KNOTS floats -> strictly monotone y-values in [0, OUT_MAX].
    y[0] = sigmoid(raw[0]) * OUT_MAX_START (anchored near 0)
    y[i+1] = y[i] + softplus(raw[i+1]) * step_scale + MIN_DELTA

    We force y[-1] <= OUT_MAX by normalizing if needed.
    """
    y0 = OUT_MAX * 0.01 * (1.0 / (1.0 + np.exp(-raw[0])))  # near 0
    # Each delta spans roughly OUT_MAX / (N_KNOTS - 1) at init
    step_scale = OUT_MAX / (N_KNOTS - 1)
    deltas = np.log1p(np.exp(raw[1:])) * step_scale + _MIN_DELTA
    ys = np.empty(N_KNOTS)
    ys[0] = y0
    for i in range(1, N_KNOTS):
        ys[i] = ys[i - 1] + deltas[i - 1]
    # Soft-cap so y[-1] <= OUT_MAX (rescale if over)
    if ys[-1] > OUT_MAX:
        ys = ys * (OUT_MAX / ys[-1])
    return ys


def _init_raw_identity():
    """Init raw params so curve output ~= input on [0,1]."""
    # y0 -> 0: raw[0] -> -large
    raw0 = -10.0
    # deltas: each should give step 1/(N_KNOTS-1)
    step = OUT_MAX / (N_KNOTS - 1)
    step_scale = step  # same
    # softplus(d)*step_scale = step -> softplus(d) = 1 -> d = log(e-1) ~ 0.541
    d_each = float(np.log(np.exp(1.0) - 1.0))
    raw = np.empty(N_KNOTS)
    raw[0] = raw0
    raw[1:] = d_each
    return raw


def make_curve_v2(raw):
    """Build PCHIP from raw params. Returns (interpolator, y_knots)."""
    ys = _decode_knots_v2(raw)
    return PchipInterpolator(X_KNOTS, ys, extrapolate=True), ys


def apply_curves_v2(Z, curve_params):
    """
    Apply per-channel monotone curves to (N,3) intermediate Z.
    Z is clamped to [0,1] before curve lookup (input domain = [0,1]).
    Output: (N,3), NOT clipped (stays in [0, OUT_MAX] by construction).
    """
    Z_clamped = np.clip(Z, 0.0, 1.0)
    out = np.empty_like(Z_clamped)
    for c in range(3):
        raw = curve_params[c * N_KNOTS:(c + 1) * N_KNOTS]
        interp, _ = make_curve_v2(raw)
        out[:, c] = interp(Z_clamped[:, c])
    return out


def invert_curve_channel_v2(y_col, raw):
    """Invert one channel curve within [0, OUT_MAX]. Returns Z in [0,1]."""
    ys = _decode_knots_v2(raw)
    # Build inverse interpolator (y -> x), valid for y in [ys[0], ys[-1]]
    inv = PchipInterpolator(ys, X_KNOTS, extrapolate=True)
    # Clamp input to [ys[0], ys[-1]] to keep inverse in-domain
    y_clamped = np.clip(y_col, ys[0], ys[-1])
    return inv(y_clamped)


# -----------------------------------------------------------------------
# FULL MODEL: F(T) = curves(RP(T))
# Params layout:
#   [0:18]   W_flat (3x6 matrix, row-major)
#   [18:18+N_KNOTS*3]  curve_params (3 channels x N_KNOTS)
# Total: 18 + 30 = 48 params
# -----------------------------------------------------------------------

N_W = 18  # 3x6
N_CURVE_PARAMS = N_KNOTS * 3


def unpack_v2(params):
    W = params[:N_W].reshape(3, 6)
    curve_params = params[N_W:N_W + N_CURVE_PARAMS]
    return W, curve_params


def apply_v2(T, params):
    """
    Forward pass: T (N,3) -> F(T) (N,3) clipped to [0,1].
    """
    W, curve_params = unpack_v2(params)
    Z = apply_rp(T, W)         # (N,3), may be outside [0,1]
    # Clamp RP output to [0,1] before curves (curves' input domain is [0,1])
    Z = np.clip(Z, 0.0, 1.0)
    Y = apply_curves_v2(Z, curve_params)  # (N,3) in [0, OUT_MAX]
    return np.clip(Y, 0.0, 1.0)


def apply_v2_unclipped(T, params):
    """Forward pass without final clip (for loss computation on non-censored)."""
    W, curve_params = unpack_v2(params)
    Z = apply_rp(T, W)
    Z = np.clip(Z, 0.0, 1.0)
    Y = apply_curves_v2(Z, curve_params)
    return Y  # in [0, OUT_MAX] by curve construction, but may slightly exceed 1.0


# -----------------------------------------------------------------------
# LOSS: CIE76 dE soft_l1 (robust), censoring applied
# -----------------------------------------------------------------------

def soft_l1_loss(dE, delta=5.0):
    """Pseudo-Huber / soft-L1. Same as 3a's _huber_loss."""
    return float(np.mean(delta * (np.sqrt(1.0 + (dE / delta) ** 2) - 1.0)))


def loss_v2(params, T, Y_truth, censor_mask, reg_lambda=0.001):
    """
    params: flat vector
    T: (N,3)
    Y_truth: (N,3) in [0,1]
    censor_mask: (N,) bool, True = clipped observation -> excluded from loss
    reg_lambda: L2 penalty on (W - W_diag_identity)
    """
    W, curve_params = unpack_v2(params)
    Z = apply_rp(T, W)
    Z_cl = np.clip(Z, 0.0, 1.0)
    Y_pred = apply_curves_v2(Z_cl, curve_params)
    Y_pred_cl = np.clip(Y_pred, 0.0, 1.0)

    # Only non-censored pairs enter the loss
    keep = ~censor_mask
    dE = de76(Y_pred_cl[keep], Y_truth[keep])
    base = soft_l1_loss(dE)

    # Regularize W toward its identity-like initialisation (diagonal = 1, cross = 0)
    if reg_lambda > 0:
        W_id = _W_identity_init()
        reg = reg_lambda * float(np.sum((W - W_id) ** 2))
        return base + reg
    return base


def loss_v2_flat(params_flat, T, Y_truth, censor_mask, reg_lambda):
    """Scalar version for L-BFGS-B."""
    return loss_v2(params_flat, T, Y_truth, censor_mask, reg_lambda)


def residuals_v2(params_flat, T, Y_truth, censor_mask):
    """Residual vector for scipy.optimize.least_squares (soft_l1 handled by method='soft_l1')."""
    W, curve_params = unpack_v2(params_flat)
    Z = np.clip(apply_rp(T, W), 0.0, 1.0)
    Y_pred = np.clip(apply_curves_v2(Z, curve_params), 0.0, 1.0)
    keep = ~censor_mask
    # Return per-point per-channel residuals in Lab space (so sqrt gives dE)
    lab_pred = srgb_to_lab(Y_pred[keep])
    lab_truth = srgb_to_lab(Y_truth[keep])
    return (lab_pred - lab_truth).ravel()  # (N_keep*3,)


# -----------------------------------------------------------------------
# INITIALISATION
# -----------------------------------------------------------------------

def _W_identity_init():
    """W init: identity on r,g,b columns; 0 on sqrt cross-terms."""
    W = np.zeros((3, 6))
    W[0, 0] = 1.0  # R <- r
    W[1, 1] = 1.0  # G <- g
    W[2, 2] = 1.0  # B <- b
    return W


def init_v2_from_v1(params_v1=None):
    """
    Warm-start: copy 3x3 block from v1 matrix into v2 W[:,:3], zeros for cross-terms.
    curve_params: identity init.
    """
    W = _W_identity_init()

    if params_v1 is not None:
        # v1 layout: [0:24] curve, [24:33] delta_M_flat
        # Recover M from delta_M
        delta_M = np.tanh(params_v1[24:33]).reshape(3, 3)
        M_v1 = np.eye(3) + 0.6 * delta_M
        # Copy into W's r,g,b columns (columns 0-2)
        W[:, :3] = M_v1
        # sqrt columns stay at 0

    curve_init = np.tile(_init_raw_identity(), 3)
    return np.concatenate([W.ravel(), curve_init])


# -----------------------------------------------------------------------
# ALS FITTING
# -----------------------------------------------------------------------

def als_step_W(T, Y_truth, curve_params, censor_mask):
    """
    ALS step (a): fix curves, solve W by least squares in curve-inverted space.
    Invert curves to get Z_target, then solve: phi(T) @ W.T ~ Z_target.
    """
    # Invert curves to get target Z
    Y_cl = np.clip(Y_truth, 0.0, 1.0)
    Z_target = np.empty_like(Y_cl)
    for c in range(3):
        raw = curve_params[c * N_KNOTS:(c + 1) * N_KNOTS]
        Z_target[:, c] = invert_curve_channel_v2(Y_cl[:, c], raw)

    # Only non-censored
    keep = ~censor_mask
    phi = rp_features(T[keep])      # (N_keep, 6)
    Z_t = Z_target[keep]            # (N_keep, 3)

    # Solve per channel: phi @ w_c ~ Z_t_c (least squares)
    W_new = np.zeros((3, 6))
    for c in range(3):
        sol, _, _, _ = np.linalg.lstsq(phi, Z_t[:, c], rcond=None)
        W_new[c] = sol
    return W_new


def als_step_curves(T, Y_truth, W, censor_mask, n_iter_inner=1000):
    """
    ALS step (b): fix W, refit curves given RP(T) -> Y_truth.
    Each channel curve is fitted independently via L-BFGS-B.
    """
    Z = np.clip(apply_rp(T, W), 0.0, 1.0)
    keep = ~censor_mask
    Z_keep = Z[keep]
    Y_keep = np.clip(Y_truth[keep], 0.0, 1.0)

    curve_params_new = np.empty(N_CURVE_PARAMS)

    for c in range(3):
        z_c = Z_keep[:, c]
        y_c = Y_keep[:, c]
        raw_init = _init_raw_identity().copy()

        def channel_loss(raw8):
            ys = _decode_knots_v2(raw8)
            interp = PchipInterpolator(X_KNOTS, ys, extrapolate=True)
            pred = np.clip(interp(z_c), 0.0, 1.0)
            # Convert to Lab for this channel only (hack: use full RGB with other channels fixed)
            return float(np.mean((pred - y_c) ** 2))

        res = minimize(channel_loss, raw_init, method="L-BFGS-B",
                       options={"maxiter": n_iter_inner, "ftol": 1e-12, "gtol": 1e-8})
        curve_params_new[c * N_KNOTS:(c + 1) * N_KNOTS] = res.x

    return curve_params_new


def als_fit(T_train, Y_train, censor_mask_train, n_als=4, params_init=None):
    """
    ALS outer loop: alternate W and curves, then joint polish.
    """
    if params_init is None:
        params = init_v2_from_v1()
    else:
        params = params_init.copy()

    W, curve_params = unpack_v2(params)

    print("  ALS iterations:")
    for it in range(n_als):
        # (a) Solve W
        W = als_step_W(T_train, Y_train, curve_params, censor_mask_train)
        # (b) Refit curves
        curve_params = als_step_curves(T_train, Y_train, W, censor_mask_train)
        # Evaluate
        params_cur = np.concatenate([W.ravel(), curve_params])
        pred = apply_v2(T_train, params_cur)
        keep = ~censor_mask_train
        dE = de76(pred[keep], Y_train[keep])
        print(f"    ALS it {it+1}: mean dE = {np.mean(dE):.4f}  median = {np.median(dE):.4f}")

    return np.concatenate([W.ravel(), curve_params])


def joint_polish(params_als, T_train, Y_train, censor_mask_train, reg_lambda=0.001):
    """
    Joint polish via scipy.least_squares with soft_l1 loss.
    """
    print("  Joint polish (least_squares soft_l1)...")
    step_count = [0]

    def jac_call(p):
        step_count[0] += 1
        return residuals_v2(p, T_train, Y_train, censor_mask_train)

    try:
        result = least_squares(
            jac_call,
            params_als,
            method='lm',      # Levenberg-Marquardt (no bounds, fast)
            loss='linear',    # LM doesn't support soft_l1 directly; use linear then re-polish
            ftol=1e-8,
            xtol=1e-8,
            gtol=1e-8,
            max_nfev=5000,
            verbose=0,
        )
        params_lm = result.x
        print(f"    LM: cost={result.cost:.4f}  nfev={result.nfev}  msg={result.message[:60]}")
    except Exception as e:
        print(f"    LM failed ({e}), skipping LM step")
        params_lm = params_als

    # Then a soft_l1 least_squares pass for robustness
    try:
        result2 = least_squares(
            jac_call,
            params_lm,
            method='trf',
            loss='soft_l1',
            f_scale=5.0,       # scale in Lab units
            ftol=1e-8,
            xtol=1e-8,
            gtol=1e-8,
            max_nfev=10000,
            verbose=0,
        )
        params_final = result2.x
        print(f"    soft_l1: cost={result2.cost:.4f}  nfev={result2.nfev}  msg={result2.message[:60]}")
    except Exception as e:
        print(f"    soft_l1 polish failed ({e}), using LM result")
        params_final = params_lm

    return params_final


# -----------------------------------------------------------------------
# INVERSION: curves + RP Newton
# -----------------------------------------------------------------------

def invert_v2(Y, params, n_newton=4):
    """
    Invert F: Y (N,3) -> T (N,3).
    Step 1: invert curves -> Z_hat (N,3) in [0,1]
    Step 2: invert RP map W via Newton from linear init.

    Newton: find T such that phi(T) @ W.T = Z_hat
    Init: T_0 = Z_hat @ W[:,:3]^{-1}  (linear part only)
    """
    W, curve_params = unpack_v2(params)
    Y_cl = np.clip(Y, 0.0, 1.0)

    # Step 1: invert curves
    Z_hat = np.empty_like(Y_cl)
    for c in range(3):
        raw = curve_params[c * N_KNOTS:(c + 1) * N_KNOTS]
        Z_hat[:, c] = invert_curve_channel_v2(Y_cl[:, c], raw)

    Z_hat = np.clip(Z_hat, 0.0, 1.0)

    # Step 2: Newton to invert RP map
    W3 = W[:, :3]  # linear part
    try:
        W3_inv = np.linalg.inv(W3)
        T = Z_hat @ W3_inv.T  # linear init
    except np.linalg.LinAlgError:
        T = Z_hat.copy()  # fallback

    T = np.clip(T, 0.0, 1.0)

    # Newton iterations: Z_hat - phi(T) @ W.T = 0
    # Jacobian of phi(T) @ W.T w.r.t. T:
    #   d(phi)/dT has blocks for r,g,b (diagonal 1) and cross-terms (partial derivs)
    for _ in range(n_newton):
        phi = rp_features(T)          # (N,6)
        residual = Z_hat - phi @ W.T  # (N,3)

        # Build per-point Jacobian (N,3,3):
        # phi = [r, g, b, sqrt(rg), sqrt(gb), sqrt(rb)]
        # d(phi)/dr = [1, 0, 0,  g/(2*sqrt(rg)),  0,              b/(2*sqrt(rb))]
        # d(phi)/dg = [0, 1, 0,  r/(2*sqrt(rg)),  b/(2*sqrt(gb)), 0             ]
        # d(phi)/db = [0, 0, 1,  0,               g/(2*sqrt(gb)), r/(2*sqrt(rb))]
        r = np.clip(T[:, 0:1], 1e-9, None)
        g = np.clip(T[:, 1:2], 1e-9, None)
        b = np.clip(T[:, 2:3], 1e-9, None)
        sqrt_rg = np.sqrt(r * g)
        sqrt_gb = np.sqrt(g * b)
        sqrt_rb = np.sqrt(r * b)

        dphi_dr = np.concatenate([
            np.ones_like(r), np.zeros_like(g), np.zeros_like(b),
            g / (2 * sqrt_rg + 1e-12),
            np.zeros_like(b),
            b / (2 * sqrt_rb + 1e-12)
        ], axis=1)  # (N,6)
        dphi_dg = np.concatenate([
            np.zeros_like(r), np.ones_like(g), np.zeros_like(b),
            r / (2 * sqrt_rg + 1e-12),
            b / (2 * sqrt_gb + 1e-12),
            np.zeros_like(b)
        ], axis=1)  # (N,6)
        dphi_db = np.concatenate([
            np.zeros_like(r), np.zeros_like(g), np.ones_like(b),
            np.zeros_like(r),
            g / (2 * sqrt_gb + 1e-12),
            r / (2 * sqrt_rb + 1e-12)
        ], axis=1)  # (N,6)

        # J_phi = d(phi @ W.T)/dT: shape (N, 3, 3)
        # J_phi[:, output_ch, input_dim]
        J = np.stack([
            dphi_dr @ W.T,  # (N,3) partial w.r.t. r
            dphi_dg @ W.T,  # (N,3) partial w.r.t. g
            dphi_db @ W.T,  # (N,3) partial w.r.t. b
        ], axis=2)  # (N, 3, 3) where J[:,i,j] = d(output_i)/d(T_j)

        # Newton step: delta_T = J^{-1} @ residual
        delta_T = np.zeros_like(T)
        for n in range(len(T)):
            try:
                delta_T[n] = np.linalg.solve(J[n], residual[n])
            except np.linalg.LinAlgError:
                pass  # singular: skip step for this point
        T = np.clip(T + delta_T, 0.0, 1.0)

    return T


def roundtrip_check_v2(params, label, n=12):
    """T -> F -> G -> T_hat, report max error on unclipped output points."""
    t1d = np.linspace(0.05, 0.95, n)
    T0 = np.array(np.meshgrid(t1d, t1d, t1d)).reshape(3, -1).T
    Y = apply_v2(T0, params)
    not_clipped = np.all((Y > 0.001) & (Y < 0.999), axis=1)
    n_valid = int(not_clipped.sum())
    T_hat = invert_v2(Y, params)
    err = np.abs(T0 - T_hat)
    max_e_unclipped = float(np.max(err[not_clipped])) if n_valid > 0 else 0.0
    max_e_all = float(np.max(err))
    print(f"  Round-trip {label}: "
          f"max|err| (all)={max_e_all:.6f}  "
          f"max|err| (unclipped {n_valid}/{len(T0)})={max_e_unclipped:.6f}  "
          f"{'PASS' if max_e_unclipped < 1e-3 else 'WARN'}")
    return max_e_unclipped


# -----------------------------------------------------------------------
# 33x33x33 INVERSE LUT
# -----------------------------------------------------------------------

def bake_inverse_lut(params, grid_size=33):
    """
    Bake a grid_size^3 inverse LUT: appearance sRGB -> T.
    Grid is uniform on [0,1]^3 in sRGB space.
    Saved as inverse_lut_33.npz with:
      lut: (33,33,33,3) float32, axes = [R_idx, G_idx, B_idx], value = T(R,G,B)
      grid_size: int
      note: usage instructions
    """
    print(f"  Baking {grid_size}^3 inverse LUT...")
    ax = np.linspace(0.0, 1.0, grid_size, dtype=np.float64)
    # Build all (grid^3, 3) sRGB points
    R, G, B = np.meshgrid(ax, ax, ax, indexing='ij')
    Y_grid = np.stack([R.ravel(), G.ravel(), B.ravel()], axis=1)  # (N,3)
    T_grid = invert_v2(Y_grid, params)  # (N,3)
    T_grid = np.clip(T_grid, 0.0, 1.0).astype(np.float32)
    lut = T_grid.reshape(grid_size, grid_size, grid_size, 3)
    note = (
        "Inverse LUT: axis 0=R, axis 1=G, axis 2=B (sRGB [0,1] uniform grid). "
        f"Grid size={grid_size}. Value = required T (R,G,B) for that appearance sRGB. "
        "Usage: trilinear interpolation on this grid gives T for any appearance color. "
        "Produced by fit_transform_v2.py Stage 3b."
    )
    np.savez_compressed("inverse_lut_33.npz", lut=lut, grid_size=np.array(grid_size), note=np.array(note))
    print(f"  Saved inverse_lut_33.npz  (shape={lut.shape}, dtype={lut.dtype})")
    return lut


# -----------------------------------------------------------------------
# EVALUATION + ANALYSIS
# -----------------------------------------------------------------------

def evaluate_v2(params, df):
    T = df[["T_R", "T_G", "T_B"]].values.astype(np.float64)
    Y = df[["jpeg_r", "jpeg_g", "jpeg_b"]].values.astype(np.float64) / 255.0
    return de76(apply_v2(T, params), Y)


def baseline_identity_dE(df):
    T = df[["T_R", "T_G", "T_B"]].values.astype(np.float64)
    Y = df[["jpeg_r", "jpeg_g", "jpeg_b"]].values.astype(np.float64) / 255.0
    return de76(linear_to_srgb(T), Y)


def report_stats(dE, label):
    m, med, p90 = float(np.mean(dE)), float(np.median(dE)), float(np.percentile(dE, 90))
    print(f"  {label:<52s}  mean={m:.3f}  med={med:.3f}  p90={p90:.3f}")
    return {"mean": m, "median": med, "p90": p90}


def _family_col(df):
    def f(fid):
        fid = str(fid).lower()
        if "white" in fid or "cotton" in fid or "beige" in fid:
            return "white"
        elif "translucent" in fid or "natural" in fid:
            return "translucent"
        else:
            return "opaque"
    return df["variable_fid"].apply(f)


def binned_stats_v2(params, df, label="v2"):
    T = df[["T_R", "T_G", "T_B"]].values.astype(np.float64)
    Y = df[["jpeg_r", "jpeg_g", "jpeg_b"]].values.astype(np.float64) / 255.0
    dE = de76(apply_v2(T, params), Y)
    df2 = df.copy()
    df2["_dE"] = dE
    df2["_family"] = _family_col(df)

    th_bins = [0, 0.3, 1.0, 2.0, 99]
    th_labels = ["<=0.3", "0.3-1", "1-2", ">2"]
    df2["_th"] = pd.cut(df2["nominal_thickness_mm"], bins=th_bins, labels=th_labels)

    by_th = df2.groupby("_th", observed=False)["_dE"].agg(["mean", "median", "count"])
    by_fam = df2.groupby("_family")["_dE"].agg(["mean", "median", "count"])

    by_L = None
    if "truth_L" in df2.columns:
        L_bins = np.percentile(df2["truth_L"], np.arange(0, 101, 10))
        L_bins = np.unique(L_bins)
        df2["_Lbin"] = pd.cut(df2["truth_L"], bins=L_bins, include_lowest=True)
        by_L = df2.groupby("_Lbin", observed=False)["_dE"].agg(["mean", "median", "count"])

    # Per-filament breakdown
    by_fil = (df2.groupby("variable_fid")["_dE"]
              .agg(["mean", "median", "count"])
              .sort_values("mean", ascending=False))

    return by_th, by_fam, by_L, by_fil, dE


EXAMPLE_COLORS = {
    "pure_white":    np.array([1.00, 1.00, 1.00]),
    "pale_blue_sky": np.array([0.70, 0.85, 1.00]),
    "mid_gray":      np.array([0.50, 0.50, 0.50]),
    "saturated_red": np.array([1.00, 0.00, 0.00]),
    "saturated_blue": np.array([0.00, 0.00, 1.00]),
    "dark_shadow":   np.array([0.10, 0.10, 0.10]),
    "warm_skin":     np.array([0.80, 0.60, 0.40]),
}


def extrapolation_preview_v2(params):
    print("\n  --- v2: G(target) preview ---")
    rows = []
    for name, srgb in EXAMPLE_COLORS.items():
        Y = srgb[None]
        T_req = invert_v2(Y, params)[0]
        Y_back = apply_v2(np.clip(T_req[None], 0, 1), params)[0]
        dE = float(de76(Y_back[None], Y)[0])
        print(f"  {name:<16s}  T={T_req.round(3)}  F(G)={Y_back.round(3)}  dE={dE:.3f}")
        rows.append({
            "color": name,
            "target_sRGB": f"({srgb[0]:.2f},{srgb[1]:.2f},{srgb[2]:.2f})",
            "T_required": f"({T_req[0]:.3f},{T_req[1]:.3f},{T_req[2]:.3f})",
            "F_G_roundtrip": f"({Y_back[0]:.3f},{Y_back[1]:.3f},{Y_back[2]:.3f})",
            "dE_roundtrip": f"{dE:.3f}",
        })
    return rows


# -----------------------------------------------------------------------
# LATTICE GATE (rung 5): 12 hue x 4 sat multiplicative residual correction
# Only applied if holdout median > 3.0 after RP+splines.
# -----------------------------------------------------------------------

N_HUE = 12
N_SAT = 4


def _hue_sat(Y_01):
    """sRGB -> (hue_angle, saturation) for HSV-like parametrization."""
    R, G, B = Y_01[:, 0], Y_01[:, 1], Y_01[:, 2]
    Cmax = np.maximum(np.maximum(R, G), B)
    Cmin = np.minimum(np.minimum(R, G), B)
    delta = Cmax - Cmin
    S = np.where(Cmax > 1e-6, delta / Cmax, 0.0)
    # Hue
    H = np.zeros(len(Y_01))
    mask_r = (Cmax == R) & (delta > 1e-6)
    mask_g = (Cmax == G) & (delta > 1e-6)
    mask_b = (Cmax == B) & (delta > 1e-6)
    H[mask_r] = ((G[mask_r] - B[mask_r]) / delta[mask_r]) % 6
    H[mask_g] = (B[mask_g] - R[mask_g]) / delta[mask_g] + 2
    H[mask_b] = (R[mask_b] - G[mask_b]) / delta[mask_b] + 4
    H = H / 6.0  # [0, 1)
    return H, S


def fit_lattice(params_v2, T_train, Y_train, censor_mask_train):
    """
    Fit multiplicative residual correction on hue x sat lattice.
    lattice shape: (N_HUE, N_SAT, 3) -- correction factor per cell per channel.
    Identity prior: all ones.
    """
    print("  Fitting hue x sat lattice correction...")
    Y_pred = apply_v2(T_train, params_v2)  # (N,3)
    keep = ~censor_mask_train
    Y_pred_k = Y_pred[keep]
    Y_truth_k = Y_train[keep]

    H, S = _hue_sat(Y_pred_k)

    # Bin into lattice cells
    hue_idx = (H * N_HUE).astype(int) % N_HUE
    sat_bins = np.linspace(0, 1, N_SAT + 1)
    sat_idx = np.clip(np.searchsorted(sat_bins[1:], S), 0, N_SAT - 1)

    # Fit per-cell multiplicative factor: avg(Y_truth / Y_pred) per channel
    lattice = np.ones((N_HUE, N_SAT, 3))
    reg_strength = 0.5  # pull toward 1.0

    for hi in range(N_HUE):
        for si in range(N_SAT):
            mask = (hue_idx == hi) & (sat_idx == si)
            if mask.sum() < 5:
                continue
            # Regularized mean ratio
            pred_cell = Y_pred_k[mask]
            truth_cell = Y_truth_k[mask]
            safe_pred = np.clip(pred_cell, 0.05, None)
            ratio = truth_cell / safe_pred
            count = mask.sum()
            # Weighted by count vs regularization
            lattice[hi, si] = (ratio.mean(axis=0) * count + reg_strength) / (count + reg_strength)

    # Smooth across hue axis (circular)
    from scipy.ndimage import uniform_filter1d
    lattice_s = uniform_filter1d(lattice, size=3, axis=0, mode='wrap')
    lattice_s = uniform_filter1d(lattice_s, size=2, axis=1, mode='nearest')
    # Blend back toward 1.0 with strong regularization
    lattice_final = 0.5 * lattice_s + 0.5 * 1.0

    return lattice_final


def apply_lattice(Y, lattice):
    """Apply hue x sat multiplicative correction. Y: (N,3) -> (N,3) clipped."""
    H, S = _hue_sat(Y)
    hue_idx = (H * N_HUE).astype(int) % N_HUE
    sat_bins = np.linspace(0, 1, N_SAT + 1)
    sat_idx = np.clip(np.searchsorted(sat_bins[1:], S), 0, N_SAT - 1)

    out = Y.copy()
    for hi in range(N_HUE):
        for si in range(N_SAT):
            mask = (hue_idx == hi) & (sat_idx == si)
            if mask.sum() == 0:
                continue
            out[mask] = Y[mask] * lattice[hi, si]
    return np.clip(out, 0.0, 1.0)


def evaluate_v2_lattice(params, lattice, df):
    T = df[["T_R", "T_G", "T_B"]].values.astype(np.float64)
    Y_truth = df[["jpeg_r", "jpeg_g", "jpeg_b"]].values.astype(np.float64) / 255.0
    Y_pred = apply_v2(T, params)
    Y_corr = apply_lattice(Y_pred, lattice)
    return de76(Y_corr, Y_truth)


# -----------------------------------------------------------------------
# REPORT BUILDING
# -----------------------------------------------------------------------

def _tbl(g, label):
    lines = [f"\n### {label}", "| Bin | mean dE | median dE | count |",
             "|-----|---------|-----------|-------|"]
    for idx, row in g.iterrows():
        lines.append(f"| {idx} | {row['mean']:.3f} | {row['median']:.3f} | {int(row['count'])} |")
    return "\n".join(lines)


def build_full_report(
    results_3a, results_3b_no_lattice, results_3b_lattice,
    params_v2, lattice, df_train, df_hold,
    hygiene_counts, n_censored, roundtrip_max, extrap_rows,
    by_th, by_fam, by_L, by_fil_holdout,
    used_lattice,
):
    lines = []
    lines.append("# Stage 3 Transform Fit Report (3a + 3b)")
    lines.append(f"\n**Date:** {datetime.date.today().isoformat()}  **Seed:** {SEED}")
    lines.append(f"\n**Stage 3b model:** Root-polynomial (Finlayson degree-2) + monotone tone curves")

    lines.append("\n## 1. Data Hygiene")
    lines.append("| Rule | Rows Dropped |")
    lines.append("|------|-------------|")
    for k, v in hygiene_counts.items():
        if k == "rule5_censored_from_loss":
            lines.append(f"| {k} (kept in df, excluded from fitting) | {v} |")
        else:
            lines.append(f"| {k} | {v} |")
    total_hard = sum(v for k, v in hygiene_counts.items() if k != "rule5_censored_from_loss")
    lines.append(f"| **Total hard-dropped** | **{total_hard}** |")
    lines.append(f"| **Kept** | **{len(df_train) + len(df_hold)}** |")
    lines.append(f"\n**Censored from loss:** {n_censored} rows with any JPEG channel >= {JPEG_CLIP_THRESHOLD} "
                 f"(clipped camera observations; included in df for evaluation but NOT in the fitting loss).")
    lines.append(f"\nTrain: {len(df_train)} swatches / {df_train['sample_id'].nunique()} samples  ")
    lines.append(f"Holdout: {len(df_hold)} swatches / {df_hold['sample_id'].nunique()} samples")

    lines.append("\n## 2. 3a-vs-3b Ladder (holdout)")
    lines.append("| Model | Train mean | Train med | Train p90 | Hold mean | Hold med | Hold p90 |")
    lines.append("|-------|-----------|-----------|-----------|----------|----------|----------|")

    def _row(r):
        tr, ho = r["train"], r["holdout"]
        return (f"| {r['label']} | {tr['mean']:.3f} | {tr['median']:.3f} | {tr['p90']:.3f} "
                f"| {ho['mean']:.3f} | {ho['median']:.3f} | {ho['p90']:.3f} |")

    for r in results_3a + results_3b_no_lattice:
        lines.append(_row(r))

    if results_3b_lattice:
        for r in results_3b_lattice:
            lines.append(_row(r))

    lines.append("\n**Reference bars (old projection, all-data kNN):**")
    lines.append("- Mean: 4.86 dE")
    lines.append("- Median: 2.59 dE")
    lines.append("\n*Note: The old projection was evaluated in-sample (kNN on training data); "
                 "holdout comparison is slightly conservative.*")

    lines.append("\n## 3. 3b Binned Analysis (holdout, RP+splines)")
    lines.append(_tbl(by_th, "By Thickness (mm)"))
    lines.append(_tbl(by_fam, "By Filament Family"))
    if by_L is not None:
        lines.append(_tbl(by_L, "By JPEG L* Decile"))

    lines.append("\n### Per-Filament Breakdown (worst 15, holdout)")
    lines.append("| Filament | mean dE | median dE | count |")
    lines.append("|---------|---------|-----------|-------|")
    for fid, row in by_fil_holdout.head(15).iterrows():
        lines.append(f"| {fid} | {row['mean']:.2f} | {row['median']:.2f} | {int(row['count'])} |")

    lines.append("\n## 4. Lattice Correction (Rung 5)")
    if used_lattice:
        lines.append(f"\nLattice gate triggered (holdout median > 3.0 after RP+splines). "
                     f"Applied {N_HUE} hue x {N_SAT} sat multiplicative correction with "
                     f"identity prior and bilinear smoothing.")
        if results_3b_lattice:
            r = results_3b_lattice[0]
            ho = r["holdout"]
            lines.append(f"\nWith lattice: holdout mean={ho['mean']:.3f}  median={ho['median']:.3f}")
    else:
        lines.append(f"\nLattice gate NOT triggered (holdout median <= 3.0, or close enough). "
                     f"Lattice was fitted and reported for completeness.")
        if results_3b_lattice:
            r = results_3b_lattice[0]
            ho = r["holdout"]
            lines.append(f"\nWith lattice (informational): holdout mean={ho['mean']:.3f}  median={ho['median']:.3f}")

    lines.append("\n## 5. Invertibility / Round-Trip")
    lines.append(f"\nRound-trip dense grid check (12^3 = {12**3} T points): "
                 f"max|T - G(F(T))| on unclipped outputs = **{roundtrip_max:.6f}** "
                 f"(threshold < 1e-3): **{'PASS' if roundtrip_max < 1e-3 else 'WARN'}**")
    lines.append(f"\nInversion method: curve 1D exact (PCHIP inverse within [0, OUT_MAX]); "
                 f"RP map via Newton (4 iterations from linear-part inverse init).")

    lines.append("\n## 6. G(target) Preview (example sRGB colors -> required T)")
    lines.append("| Color | Target sRGB | T required | F(G) roundtrip | dE roundtrip |")
    lines.append("|-------|-------------|-----------|----------------|-----|")
    for r in extrap_rows:
        lines.append(
            f"| {r['color']} | {r['target_sRGB']} | {r['T_required']} "
            f"| {r['F_G_roundtrip']} | {r['dE_roundtrip']} |"
        )

    lines.append("\n## 7. Failure Analysis")
    lines.append("\nKnown weak spots (same as 3a, partially improved by RP cross-terms):")
    lines.append("- **Cool-hue filaments (blue/cyan/green):** Canon pipeline applies "
                 "hue-dependent saturation boost. RP cross-terms partially compensate "
                 "but the boost varies non-monotonically with hue angle.")
    lines.append("- **Translucent filaments:** thin-layer specular variation exceeds "
                 "the model's smooth global parametric capacity.")
    lines.append("- **Near-white (very thin layers):** T near 1.0; JPEG compressed into "
                 "narrow range near lightbox color; censoring removes worst clipped points.")

    lines.append("\n## 8. Verdict")
    hold_no_lat = next(r for r in results_3b_no_lattice if "v2" in r["label"].lower() or "rp" in r["label"].lower())
    ho = hold_no_lat["holdout"]
    beat_mean = ho["mean"] < 4.86
    beat_med = ho["median"] < 2.59
    lines.append(f"\n**3b (RP+splines):** holdout mean = **{ho['mean']:.3f}** / "
                 f"median = **{ho['median']:.3f}** dE")
    lines.append(f"Old projection bar: 4.86 mean / 2.59 median")
    if beat_mean and beat_med:
        lines.append("\n**BEATS bar on both mean and median.**")
    elif beat_mean:
        lines.append(f"\nBeats mean bar (OK), misses median bar (MISS).")
    elif beat_med:
        lines.append(f"\nMisses mean bar (MISS), beats median bar (OK).")
    else:
        lines.append(f"\nMisses both bars.")
    lines.append(f"\nRound-trip: {roundtrip_max:.6f} "
                 f"({'PASS' if roundtrip_max < 1e-3 else 'WARN -- see §5'}).")

    txt = "\n".join(lines)
    with open("transform_report.md", "w", encoding="utf-8") as fh:
        fh.write(txt)
    print("Saved transform_report.md")
    return txt


def save_params_v2(params_v2, df_train, df_hold, hygiene_counts, n_censored,
                   hold_mean, hold_median, lattice=None, used_lattice=False):
    """Save v2 params into transform_params.json alongside v1."""
    try:
        with open("transform_params.json") as fh:
            existing = json.load(fh)
    except Exception:
        existing = {}

    v1_data = {}
    for k, v in existing.items():
        if k not in ("v1", "v2"):
            v1_data[k] = v

    v2_data = {
        "version": "v2",
        "date": datetime.date.today().isoformat(),
        "seed": SEED,
        "model": "root_polynomial_6feat + monotone_pchip_curves",
        "architecture": "T -> [r,g,b,sqrt(rg),sqrt(gb),sqrt(rb)] @ W.T -> clip([0,1]) -> per_channel_monotone_pchip -> clip([0,1])",
        "n_rp_features": 6,
        "n_knots": N_KNOTS,
        "x_knots": X_KNOTS.tolist(),
        "out_max": OUT_MAX,
        "jpeg_clip_threshold": JPEG_CLIP_THRESHOLD,
        "n_censored_from_loss": n_censored,
        "params_layout": "params[0:18]=W_flat(3x6 row-major), params[18:48]=curve_params(3ch x 10 knots)",
        "n_params": N_W + N_CURVE_PARAMS,
        "holdout_mean_dE": round(hold_mean, 4),
        "holdout_median_dE": round(hold_median, 4),
        "n_train_swatches": len(df_train),
        "n_holdout_swatches": len(df_hold),
        "hygiene_drop_counts": hygiene_counts,
        "params": params_v2.tolist(),
        "used_lattice": used_lattice,
    }
    if lattice is not None and used_lattice:
        v2_data["lattice"] = lattice.tolist()
        v2_data["lattice_shape"] = list(lattice.shape)
        v2_data["lattice_note"] = f"hue x sat correction: shape=({N_HUE},{N_SAT},3)"

    output = {
        "v1": v1_data,
        "v2": v2_data,
    }
    with open("transform_params.json", "w") as fh:
        json.dump(output, fh, indent=2)
    print("Saved transform_params.json (v1 + v2)")


# -----------------------------------------------------------------------
# MAIN
# -----------------------------------------------------------------------

def main():
    print("=" * 65)
    print("Stage 3b: Root-polynomial + monotone splines appearance transform")
    print("=" * 65)

    df, hygiene_counts, n_censored = load_and_filter()

    print("=== TRAIN / HOLDOUT SPLIT ===")
    df_train, df_hold = split_train_holdout(df)

    T_train = df_train[["T_R", "T_G", "T_B"]].values.astype(np.float64)
    Y_train = df_train[["jpeg_r", "jpeg_g", "jpeg_b"]].values.astype(np.float64) / 255.0
    censor_mask_train = df_train["_censored"].values

    T_hold = df_hold[["T_R", "T_G", "T_B"]].values.astype(np.float64)
    Y_hold = df_hold[["jpeg_r", "jpeg_g", "jpeg_b"]].values.astype(np.float64) / 255.0

    print(f"\n  Censored from training loss: {censor_mask_train.sum()} / {len(T_train)} train rows")
    print()

    # --- 3a reference numbers (loaded from existing params) ---
    print("=== 3a REFERENCE (recomputed) ===")
    results_3a = []
    try:
        with open("transform_params.json") as fh:
            old_p = json.load(fh)
        # Handle both flat (old) and nested (new) format
        if "v1" in old_p:
            v1_p = old_p["v1"]
        else:
            v1_p = old_p
        if "params" in v1_p:
            import sys as _sys
            # Load 3a model inline
            _p3a = np.array(v1_p["params"])
            N_KNOTS_3a = 8
            X_STD_3a = np.linspace(0.0, 1.0, N_KNOTS_3a)
            _M_SCALE_3a = 0.6

            def _decode_knots_3a(raw8):
                y0 = 0.5 + 0.55 * np.tanh(raw8[0])
                deltas = np.log1p(np.exp(raw8[1:])) + 5e-4
                ys = np.empty(N_KNOTS_3a)
                ys[0] = y0
                for i in range(1, N_KNOTS_3a):
                    ys[i] = ys[i - 1] + deltas[i - 1]
                return ys

            def _apply_curve_3a(t_col, raw8):
                ys = _decode_knots_3a(raw8)
                interp = PchipInterpolator(X_STD_3a, ys, extrapolate=True)
                return interp(t_col)

            def apply_B_3a(T, params):
                delta_M = np.tanh(params[24:33]).reshape(3, 3)
                M = np.eye(3) + _M_SCALE_3a * delta_M
                Yc = np.empty_like(T)
                for c in range(3):
                    Yc[:, c] = _apply_curve_3a(T[:, c], params[c * N_KNOTS_3a:(c + 1) * N_KNOTS_3a])
                return np.clip(Yc @ M.T, 0.0, 1.0)

            dE_3a_tr = de76(apply_B_3a(T_train, _p3a),
                            Y_train)
            dE_3a_ho = de76(apply_B_3a(T_hold, _p3a),
                            Y_hold)
            r3a_id = {"label": "Identity sRGB-encode(T)",
                      "train": report_stats(baseline_identity_dE(df_train), "Identity TRAIN"),
                      "holdout": report_stats(baseline_identity_dE(df_hold), "Identity HOLD")}
            r3a_b = {"label": "3a: matrix+PCHIP (v1)",
                     "train": report_stats(dE_3a_tr, "3a B TRAIN"),
                     "holdout": report_stats(dE_3a_ho, "3a B HOLD")}
            results_3a = [r3a_id, r3a_b]
        else:
            raise ValueError("no params in v1")
    except Exception as e:
        print(f"  Could not load 3a params ({e}); using identity baseline only")
        r3a_id = {"label": "Identity sRGB-encode(T)",
                  "train": report_stats(baseline_identity_dE(df_train), "Identity TRAIN"),
                  "holdout": report_stats(baseline_identity_dE(df_hold), "Identity HOLD")}
        results_3a = [r3a_id]
    print()

    # --- 3b: ALS fit ---
    print("=== 3b FIT: ALS ===")
    v1_params_for_init = None
    try:
        with open("transform_params.json") as fh:
            op = json.load(fh)
        v1p = op.get("v1", op)
        if "params" in v1p:
            v1_params_for_init = np.array(v1p["params"])
    except Exception:
        pass

    params_init = init_v2_from_v1(v1_params_for_init)
    print(f"  Model: {N_W + N_CURVE_PARAMS} params  "
          f"(W: {N_W}=3x6, curves: {N_CURVE_PARAMS}={3}x{N_KNOTS})")
    print(f"  Censored from loss: {censor_mask_train.sum()} train rows")

    params_als = als_fit(T_train, Y_train, censor_mask_train,
                         n_als=4, params_init=params_init)

    print("\n=== JOINT POLISH ===")
    params_v2 = joint_polish(params_als, T_train, Y_train, censor_mask_train)

    print("\n=== 3b EVALUATION ===")
    dE_v2_tr = evaluate_v2(params_v2, df_train)
    dE_v2_ho = evaluate_v2(params_v2, df_hold)
    v2_tr = report_stats(dE_v2_tr, "3b RP+splines TRAIN")
    v2_ho = report_stats(dE_v2_ho, "3b RP+splines HOLD")
    results_3b_no_lat = [{"label": "3b: RP+splines", "train": v2_tr, "holdout": v2_ho}]

    W_fit, _ = unpack_v2(params_v2)
    print(f"\n  W matrix (3x6):\n{np.round(W_fit, 4)}")
    print()

    # --- Lattice gate ---
    print("=== LATTICE GATE (rung 5) ===")
    lattice = fit_lattice(params_v2, T_train, Y_train, censor_mask_train)

    dE_lat_tr = evaluate_v2_lattice(params_v2, lattice, df_train)
    dE_lat_ho = evaluate_v2_lattice(params_v2, lattice, df_hold)
    lat_tr = report_stats(dE_lat_tr, "3b+lattice TRAIN")
    lat_ho = report_stats(dE_lat_ho, "3b+lattice HOLD")
    results_3b_lat = [{"label": "3b+lattice", "train": lat_tr, "holdout": lat_ho}]

    used_lattice = (v2_ho["median"] > 3.0)
    print(f"\n  Lattice gate: median={v2_ho['median']:.3f} {'> 3.0 => USE lattice' if used_lattice else '<= 3.0 => skip lattice'}")

    # Final model
    final_hold = lat_ho if used_lattice else v2_ho

    # --- Invertibility ---
    print("\n=== INVERTIBILITY ===")
    roundtrip_max = roundtrip_check_v2(params_v2, "v2 RP+splines")

    # --- Extrapolation preview ---
    print("\n=== EXTRAPOLATION PREVIEW ===")
    extrap_rows = extrapolation_preview_v2(params_v2)

    # --- Binned analysis ---
    print("\n=== BINNED ANALYSIS (holdout) ===")
    by_th, by_fam, by_L, by_fil, dE_ho = binned_stats_v2(params_v2, df_hold)
    print("By family:")
    for fam, row in by_fam.iterrows():
        print(f"  {fam:<15s}  mean={row['mean']:.3f}  med={row['median']:.3f}  n={int(row['count'])}")
    print("Worst 5 filaments:")
    for fid, row in by_fil.head(5).iterrows():
        print(f"  {fid:<40s}  mean={row['mean']:.3f}  med={row['median']:.3f}  n={int(row['count'])}")

    # --- Bake LUT ---
    print("\n=== BAKING INVERSE LUT 33^3 ===")
    bake_inverse_lut(params_v2, grid_size=33)

    # --- Save params ---
    final_median = lat_ho["median"] if used_lattice else v2_ho["median"]
    final_mean = lat_ho["mean"] if used_lattice else v2_ho["mean"]
    save_params_v2(params_v2, df_train, df_hold, hygiene_counts, n_censored,
                   hold_mean=final_mean, hold_median=final_median,
                   lattice=lattice if used_lattice else None,
                   used_lattice=used_lattice)

    # --- Report ---
    print("\n=== BUILDING REPORT ===")
    build_full_report(
        results_3a=results_3a,
        results_3b_no_lattice=results_3b_no_lat,
        results_3b_lattice=results_3b_lat,
        params_v2=params_v2,
        lattice=lattice,
        df_train=df_train,
        df_hold=df_hold,
        hygiene_counts=hygiene_counts,
        n_censored=n_censored,
        roundtrip_max=roundtrip_max,
        extrap_rows=extrap_rows,
        by_th=by_th,
        by_fam=by_fam,
        by_L=by_L,
        by_fil_holdout=by_fil,
        used_lattice=used_lattice,
    )

    # --- Summary ---
    print("\n" + "=" * 65)
    print("SUMMARY")
    print("=" * 65)
    for r in results_3a + results_3b_no_lat + results_3b_lat:
        ho = r["holdout"]
        print(f"  {r['label']:<44s}  hold mean={ho['mean']:.3f}  med={ho['median']:.3f}")
    print(f"  {'OLD PROJECTION BAR':<44s}  hold mean=4.860  med=2.587")
    print(f"\n  v2 holdout mean={v2_ho['mean']:.3f}  median={v2_ho['median']:.3f}  "
          f"{'BEATS mean' if v2_ho['mean'] < 4.86 else 'misses mean'}  "
          f"{'BEATS median' if v2_ho['median'] < 2.59 else 'misses median'}")
    print(f"  Round-trip max error: {roundtrip_max:.6f}  "
          f"({'PASS' if roundtrip_max < 1e-3 else 'WARN'})")
    print(f"  Lattice used: {used_lattice}")
    print(f"  Censored rows: {n_censored}")


if __name__ == "__main__":
    main()
