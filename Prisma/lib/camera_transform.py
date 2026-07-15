"""Camera Transform artifact loading and evaluation.

The Camera Transform maps model-domain transmission RGB to camera-rendered
display sRGB (forward F) and provides the baked inverse 33^3 LUT used for
image ingress (G).  The v1 artifact schema stores the v2 fit model: a
Finlayson degree-2 root-polynomial matrix followed by monotone PCHIP curves.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import numpy as np
from scipy.interpolate import PchipInterpolator

CAMERA_TRANSFORM_SCHEMA = "camera_transform_v1"
CAMERA_TRANSFORM_JSON = "camera_transform.json"
CAMERA_TRANSFORM_LUT = "inverse_lut_33.npz"
CAMERA_TRANSFORM_MANIFEST = "manifest.json"
CAMERA_TRANSFORM_CURRENT = "CURRENT"

N_KNOTS = 10
OUT_MAX = 1.05
MIN_DELTA = 1e-4
X_KNOTS = np.linspace(0.0, 1.0, N_KNOTS)
N_W = 18
N_PARAMS = N_W + 3 * N_KNOTS
LUT_SHAPE = (33, 33, 33, 3)

ACTIONABLE_REBUILD = "Rebuild via Calibration -> Camera Transform."
_DEFAULT_DIR = Path(__file__).resolve().parent.parent / "data" / "camera_transform"
_REPO_ROOT = Path(__file__).resolve().parents[2]
_PRISMA_DIR = _REPO_ROOT / "Prisma"

_TRANSFORM_CACHE: dict[tuple[str, int], "CameraTransform"] = {}
_LUT_CACHE: dict[tuple[str, int], np.ndarray] = {}


class CameraTransformError(RuntimeError):
    """Actionable error raised for missing or invalid Camera Transform artifacts."""


@dataclass(frozen=True)
class CameraTransform:
    path: Path
    payload: dict[str, Any]
    params: np.ndarray
    sha256: str


@dataclass(frozen=True)
class _ArtifactFile:
    path: Path
    generation_dir: Path | None


def default_camera_transform_dir() -> Path:
    return _DEFAULT_DIR


def default_inverse_lut_path() -> Path:
    return _DEFAULT_DIR


def _read_current_generation(root: Path, *, published: bool = False) -> Path:
    if not (published or _published_library_mode()):
        try:
            from .model_registry import current_model_artifact_root
        except ImportError:  # pragma: no cover - app-style import
            from lib.model_registry import current_model_artifact_root
        authoritative, registered = current_model_artifact_root(root.parent, "camera_transform")
        if authoritative:
            if registered is None:
                raise _artifact_error(root, "no current Camera Transform is published in SQLite")
            # Pre-one-fit publications recorded the family root rather than the
            # selected generation. Resolve its CURRENT pointer explicitly until the
            # next fit republishes a generation-specific SQLite root.
            if registered != root:
                if not registered.is_dir():
                    raise _artifact_error(registered, "SQLite current Camera Transform generation is missing")
                return registered
    current = root / CAMERA_TRANSFORM_CURRENT
    if not current.exists():
        raise _artifact_error(current, "CURRENT pointer is missing")
    try:
        generation_name = current.read_text(encoding="utf-8").strip()
    except Exception as exc:
        raise _artifact_error(current, f"could not read CURRENT pointer ({exc})") from exc
    if not generation_name:
        raise _artifact_error(current, "CURRENT pointer is empty")
    generation = Path(generation_name)
    if generation.is_absolute() or len(generation.parts) != 1:
        raise _artifact_error(current, f"CURRENT pointer must name a local generation, got {generation_name!r}")
    generation_dir = root / generation_name
    if not generation_dir.is_dir():
        raise _artifact_error(current, f"generation {generation_name!r} is missing")
    return generation_dir


def _resolve_artifact_file(
    dir_or_path: str | Path,
    filename: str,
    *,
    published: bool = False,
) -> _ArtifactFile:
    path = Path(dir_or_path).expanduser()
    if not path.is_absolute() and not path.exists():
        path = _REPO_ROOT / path
    path = _resolve_data_root_default(path, published=published)
    if path.is_dir():
        if (path / CAMERA_TRANSFORM_CURRENT).exists():
            generation_dir = _read_current_generation(path, published=published)
            return _ArtifactFile(generation_dir / filename, generation_dir)
        return _ArtifactFile(path / filename, None)
    return _ArtifactFile(path, None)


def _resolve_transform_json(dir_or_path: str | Path, *, published: bool = False) -> _ArtifactFile:
    return _resolve_artifact_file(dir_or_path, CAMERA_TRANSFORM_JSON, published=published)


def _configured_data_root() -> Path | None:
    pointer = _PRISMA_DIR / "calibration" / ".data-root"
    if not pointer.exists():
        return None
    text = pointer.read_text(encoding="utf-8").strip()
    if not text:
        return None
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = pointer.parent / path
    return path.resolve()


def _published_library_mode() -> bool:
    return str(os.environ.get("PRISMA_PUBLISHED_LIBRARY_MODE") or "").strip() == "1"


def _resolve_data_root_default(path: Path, *, published: bool = False) -> Path:
    if published or _published_library_mode():
        return path
    data_root = _configured_data_root()
    if data_root is not None:
        try:
            path.resolve().relative_to(data_root.resolve())
            return path
        except ValueError:
            pass
    try:
        rel = path.resolve().relative_to((_PRISMA_DIR / "data").resolve())
    except ValueError:
        return path
    if data_root is None:
        return path
    return data_root / rel


def _artifact_error(path: Path, message: str) -> CameraTransformError:
    return CameraTransformError(f"Camera Transform artifact {path}: {message}. {ACTIONABLE_REBUILD}")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_manifest(generation_dir: Path) -> dict[str, Any]:
    manifest_path = generation_dir / CAMERA_TRANSFORM_MANIFEST
    if not manifest_path.exists():
        raise _artifact_error(manifest_path, "manifest file is missing")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise _artifact_error(manifest_path, f"could not parse manifest ({exc})") from exc
    if not isinstance(manifest, dict):
        raise _artifact_error(manifest_path, "manifest must be a JSON object")
    return manifest


def _verify_generation_hashes(generation_dir: Path) -> None:
    manifest = _load_manifest(generation_dir)
    hashes = manifest.get("artifact_hashes")
    if not isinstance(hashes, dict):
        raise _artifact_error(generation_dir / CAMERA_TRANSFORM_MANIFEST, "manifest missing artifact_hashes")
    expected_json = hashes.get(CAMERA_TRANSFORM_JSON)
    expected_lut = hashes.get(CAMERA_TRANSFORM_LUT)
    if not isinstance(expected_json, str) or not isinstance(expected_lut, str):
        raise _artifact_error(generation_dir / CAMERA_TRANSFORM_MANIFEST, "manifest artifact_hashes must include camera_transform.json and inverse_lut_33.npz")
    for filename, expected in ((CAMERA_TRANSFORM_JSON, expected_json), (CAMERA_TRANSFORM_LUT, expected_lut)):
        path = generation_dir / filename
        if not path.exists():
            raise _artifact_error(path, "file is missing from committed generation")
        actual = _sha256_file(path)
        if actual != expected:
            raise _artifact_error(path, f"sha256 mismatch for committed generation (expected {expected}, got {actual})")


def validate_camera_transform_payload(payload: dict[str, Any], *, path: str | Path = "<payload>") -> None:
    p = Path(path)
    if not isinstance(payload, dict):
        raise _artifact_error(p, "payload must be a JSON object")
    schema = payload.get("schema")
    if schema != CAMERA_TRANSFORM_SCHEMA:
        raise _artifact_error(p, f"schema must be {CAMERA_TRANSFORM_SCHEMA!r}, got {schema!r}")
    version = payload.get("model_version")
    if version != "v2":
        raise _artifact_error(p, f"model_version must be 'v2', got {version!r}")
    if payload.get("used_lattice"):
        raise _artifact_error(p, "used_lattice must be false; lattice transforms are not supported")
    params = np.asarray(payload.get("params"), dtype=np.float64)
    if params.shape != (N_PARAMS,):
        raise _artifact_error(p, f"params must contain {N_PARAMS} finite values, got shape {params.shape}")
    if not np.isfinite(params).all():
        raise _artifact_error(p, "params contain non-finite values")
    if int(payload.get("n_params", N_PARAMS)) != N_PARAMS:
        raise _artifact_error(p, f"n_params must be {N_PARAMS}")
    if int(payload.get("n_knots", N_KNOTS)) != N_KNOTS:
        raise _artifact_error(p, f"n_knots must be {N_KNOTS}")


def load_camera_transform(
    dir_or_path: str | Path,
    *,
    published: bool = False,
    use_cache: bool = True,
) -> CameraTransform:
    resolved = _resolve_transform_json(dir_or_path, published=published)
    path = resolved.path
    if resolved.generation_dir is not None:
        _verify_generation_hashes(resolved.generation_dir)
    if not path.exists():
        raise _artifact_error(path, "file is missing")
    stat = path.stat()
    key = (str(path.resolve()), int(stat.st_mtime_ns))
    cached = _TRANSFORM_CACHE.get(key) if use_cache else None
    if cached is not None:
        return cached
    raw = path.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise _artifact_error(path, f"could not parse JSON ({exc})") from exc
    validate_camera_transform_payload(payload, path=path)
    transform = CameraTransform(
        path=path,
        payload=payload,
        params=np.asarray(payload["params"], dtype=np.float64),
        sha256=hashlib.sha256(raw).hexdigest(),
    )
    if use_cache:
        _TRANSFORM_CACHE.clear()
        _TRANSFORM_CACHE[key] = transform
    return transform


def _decode_knots(raw: np.ndarray) -> np.ndarray:
    with np.errstate(over="ignore", invalid="ignore"):
        y0 = OUT_MAX * 0.01 * (1.0 / (1.0 + np.exp(-raw[0])))
        step_scale = OUT_MAX / (N_KNOTS - 1)
        deltas = np.log1p(np.exp(raw[1:])) * step_scale + MIN_DELTA
        ys = np.empty(N_KNOTS)
        ys[0] = y0
        for i in range(1, N_KNOTS):
            ys[i] = ys[i - 1] + deltas[i - 1]
        if ys[-1] > OUT_MAX:
            ys = ys * (OUT_MAX / ys[-1])
    return ys


def rp_features(linear_rgb: np.ndarray) -> np.ndarray:
    arr = np.clip(np.asarray(linear_rgb, dtype=np.float64), 0.0, None)
    r, g, b = arr[:, 0], arr[:, 1], arr[:, 2]
    return np.stack([r, g, b, np.sqrt(r * g), np.sqrt(g * b), np.sqrt(r * b)], axis=1)


def apply_rp(linear_rgb: np.ndarray, w: np.ndarray) -> np.ndarray:
    return rp_features(linear_rgb) @ w.T


def unpack_params(params: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    arr = np.asarray(params, dtype=np.float64)
    return arr[:N_W].reshape(3, 6), arr[N_W:N_W + 3 * N_KNOTS]


def apply_forward(linear_rgb: np.ndarray, params: np.ndarray) -> np.ndarray:
    arr = np.asarray(linear_rgb, dtype=np.float64)
    shape = arr.shape
    flat = arr.reshape(-1, 3)
    w, curve_params = unpack_params(params)
    z = np.clip(apply_rp(flat, w), 0.0, 1.0)
    out = np.empty_like(z)
    for c in range(3):
        raw = curve_params[c * N_KNOTS:(c + 1) * N_KNOTS]
        interp = PchipInterpolator(X_KNOTS, _decode_knots(raw), extrapolate=True)
        out[:, c] = interp(z[:, c])
    return np.clip(out, 0.0, 1.0).reshape(shape)


def invert_curve_channel(y_col: np.ndarray, raw: np.ndarray) -> np.ndarray:
    ys = _decode_knots(raw)
    inv = PchipInterpolator(ys, X_KNOTS, extrapolate=True)
    return inv(np.clip(y_col, ys[0], ys[-1]))


def invert_forward(display_srgb: np.ndarray, params: np.ndarray, *, n_newton: int = 4) -> np.ndarray:
    w, curve_params = unpack_params(params)
    y_cl = np.clip(np.asarray(display_srgb, dtype=np.float64).reshape(-1, 3), 0.0, 1.0)
    z_hat = np.empty_like(y_cl)
    for c in range(3):
        raw = curve_params[c * N_KNOTS:(c + 1) * N_KNOTS]
        z_hat[:, c] = invert_curve_channel(y_cl[:, c], raw)
    z_hat = np.clip(z_hat, 0.0, 1.0)

    try:
        t = z_hat @ np.linalg.inv(w[:, :3]).T
    except np.linalg.LinAlgError:
        t = z_hat.copy()
    t = np.clip(t, 0.0, 1.0)

    for _ in range(n_newton):
        residual = z_hat - rp_features(t) @ w.T
        r = np.clip(t[:, 0:1], 1e-9, None)
        g = np.clip(t[:, 1:2], 1e-9, None)
        b = np.clip(t[:, 2:3], 1e-9, None)
        sqrt_rg = np.sqrt(r * g)
        sqrt_gb = np.sqrt(g * b)
        sqrt_rb = np.sqrt(r * b)
        dphi_dr = np.concatenate(
            [np.ones_like(r), np.zeros_like(g), np.zeros_like(b), g / (2 * sqrt_rg + 1e-12), np.zeros_like(b), b / (2 * sqrt_rb + 1e-12)],
            axis=1,
        )
        dphi_dg = np.concatenate(
            [np.zeros_like(r), np.ones_like(g), np.zeros_like(b), r / (2 * sqrt_rg + 1e-12), b / (2 * sqrt_gb + 1e-12), np.zeros_like(b)],
            axis=1,
        )
        dphi_db = np.concatenate(
            [np.zeros_like(r), np.zeros_like(g), np.ones_like(b), np.zeros_like(r), g / (2 * sqrt_gb + 1e-12), r / (2 * sqrt_rb + 1e-12)],
            axis=1,
        )
        jac = np.stack([dphi_dr @ w.T, dphi_dg @ w.T, dphi_db @ w.T], axis=2)
        delta = np.zeros_like(t)
        for idx in range(len(t)):
            try:
                delta[idx] = np.linalg.solve(jac[idx], residual[idx])
            except np.linalg.LinAlgError:
                pass
        t = np.clip(t + delta, 0.0, 1.0)
    return t.reshape(np.asarray(display_srgb).shape)


def validate_inverse_lut(lut: np.ndarray, *, path: str | Path = "<lut>") -> None:
    p = Path(path)
    if lut.shape != LUT_SHAPE:
        raise _artifact_error(p, f"inverse LUT must have shape {LUT_SHAPE}, got {lut.shape}")
    if not np.isfinite(lut).all():
        raise _artifact_error(p, "inverse LUT contains non-finite values")


def load_inverse_lut(
    path: str | Path,
    *,
    published: bool = False,
    use_cache: bool = True,
) -> np.ndarray:
    resolved = _resolve_artifact_file(path, CAMERA_TRANSFORM_LUT, published=published)
    p = resolved.path
    if resolved.generation_dir is not None:
        _verify_generation_hashes(resolved.generation_dir)
    if not p.exists():
        raise _artifact_error(p, "inverse LUT file is missing")
    stat = p.stat()
    key = (str(p.resolve()), int(stat.st_mtime_ns))
    cached = _LUT_CACHE.get(key) if use_cache else None
    if cached is not None:
        return cached
    try:
        with np.load(p) as payload:
            lut = np.array(payload["lut"], dtype=np.float32, copy=True)
    except Exception as exc:
        raise _artifact_error(p, f"could not load inverse LUT ({exc})") from exc
    validate_inverse_lut(lut, path=p)
    if use_cache:
        _LUT_CACHE.clear()
        _LUT_CACHE[key] = lut
    return lut


def apply_inverse_lut(img_srgb: np.ndarray, lut: np.ndarray) -> np.ndarray:
    validate_inverse_lut(np.asarray(lut), path="<provided LUT>")
    img = np.asarray(img_srgb, dtype=np.float32)
    if np.issubdtype(np.asarray(img_srgb).dtype, np.integer):
        img = img / 255.0
    img = np.clip(img, 0.0, 1.0)
    coords = img * np.float32(lut.shape[0] - 1)
    lo = np.floor(coords).astype(np.int32)
    hi = np.minimum(lo + 1, lut.shape[0] - 1)
    frac = coords - lo.astype(np.float32)
    r0, g0, b0 = lo[..., 0], lo[..., 1], lo[..., 2]
    r1, g1, b1 = hi[..., 0], hi[..., 1], hi[..., 2]
    fr, fg, fb = frac[..., 0:1], frac[..., 1:2], frac[..., 2:3]
    c000 = lut[r0, g0, b0]
    c100 = lut[r1, g0, b0]
    c010 = lut[r0, g1, b0]
    c110 = lut[r1, g1, b0]
    c001 = lut[r0, g0, b1]
    c101 = lut[r1, g0, b1]
    c011 = lut[r0, g1, b1]
    c111 = lut[r1, g1, b1]
    c00 = c000 * (1.0 - fr) + c100 * fr
    c10 = c010 * (1.0 - fr) + c110 * fr
    c01 = c001 * (1.0 - fr) + c101 * fr
    c11 = c011 * (1.0 - fr) + c111 * fr
    c0 = c00 * (1.0 - fg) + c10 * fg
    c1 = c01 * (1.0 - fg) + c11 * fg
    return np.clip(c0 * (1.0 - fb) + c1 * fb, 0.0, 1.0).astype(np.float32)
