"""Generator-facing appearance model providers.

The generator historically predicts appearance from independent spline
profiles.  The photo stack model is context-aware, so it enters through this
small stack-prediction boundary instead of being forced into per-filament
profile JSONs.
"""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import numpy as np

from thickness_maps import MapKey

_GEN_DIR = Path(__file__).resolve().parent
_PRISMA_DIR = _GEN_DIR.parent
if str(_PRISMA_DIR) not in sys.path:
    sys.path.insert(0, str(_PRISMA_DIR))
if str(_GEN_DIR) not in sys.path:
    sys.path.insert(0, str(_GEN_DIR))

from lib.stack_geometry import collapse_adjacent_layers
from model import compose_stack, predict_transmission, to_oklab


Layer = tuple[str, float]


@dataclass(frozen=True)
class StackRequest:
    """One physical stack request in generator terms."""

    white_base: Layer
    color_layers: tuple[Layer, ...]
    white_cap: Layer


def _profile_checksum(profile: dict) -> str:
    h = hashlib.sha256()
    for key in ("filament_id", "knots_mm", "T_r", "T_g", "T_b"):
        value = profile.get(key, "")
        if isinstance(value, list):
            h.update(np.asarray(value, dtype=np.float64).tobytes())
        else:
            h.update(str(value).encode())
    return h.hexdigest()[:16]


def _fingerprint_profiles(
    *,
    color_profiles: Mapping[str, dict],
    wb_profile: dict,
    wc_profile: dict,
) -> str:
    h = hashlib.sha256()
    for fid in sorted(color_profiles):
        h.update(f"color:{fid}:{_profile_checksum(color_profiles[fid])}".encode())
    h.update(f"wb:{_profile_checksum(wb_profile)}".encode())
    h.update(f"wc:{_profile_checksum(wc_profile)}".encode())
    return h.hexdigest()[:20]


def _srgb8_from_linear(rgb: np.ndarray) -> np.ndarray:
    return (np.clip(rgb, 0.0, 1.0) ** (1.0 / 2.2) * 255.0).astype(np.uint8)


# Provider domain-contract version. Bump when the meaning of the provider's
# appearance-domain output changes (e.g. the 2026-06-12 projection-slot
# retirement made appearance == model-domain exactly). Folded into BOTH the
# provider fingerprint and _solve_owned_fingerprint so stale solves cannot
# survive a domain-contract change.
APPEARANCE_DOMAIN_VERSION = "domain-pass-through-v1"


class HistoricalSplineAppearanceProvider:
    """Adapter around the existing spline multiplication model."""

    model_kind = "historical_spline"

    def __init__(
        self,
        *,
        color_profiles: Mapping[str, dict],
        wb_profile: dict,
        wc_profile: dict,
    ) -> None:
        self.color_profiles = dict(color_profiles)
        self.wb_profile = wb_profile
        self.wc_profile = wc_profile
        self._fingerprint = "historical_spline:" + _fingerprint_profiles(
            color_profiles=self.color_profiles,
            wb_profile=self.wb_profile,
            wc_profile=self.wc_profile,
        )

    def fingerprint(self) -> str:
        return self._fingerprint

    def predict_stack_linear_rgb(
        self,
        *,
        white_base: Layer,
        color_layers: Sequence[Layer],
        white_cap: Layer,
    ) -> np.ndarray:
        layers: list[tuple[dict, float]] = [(self.wb_profile, float(white_base[1]))]
        for fid, thickness in color_layers:
            if float(thickness) > 0.0 and fid in self.color_profiles:
                layers.append((self.color_profiles[str(fid)], float(thickness)))
        layers.append((self.wc_profile, float(white_cap[1])))
        return compose_stack(layers).astype(np.float32)

    def predict_stack_linear_rgb_batch(self, requests: Sequence[StackRequest]) -> np.ndarray:
        return np.asarray(
            [
                self.predict_stack_linear_rgb(
                    white_base=req.white_base,
                    color_layers=req.color_layers,
                    white_cap=req.white_cap,
                )
                for req in requests
            ],
            dtype=np.float32,
        )

    def sample_white_cap_oklab(
        self,
        *,
        white_base: Layer,
        white_cap_id: str,
        cap_thicknesses_mm: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray]:
        wb_trans = np.asarray(predict_transmission(self.wb_profile, float(white_base[1])), dtype=np.float32)
        wc_trans = np.asarray(
            [predict_transmission(self.wc_profile, float(d)) for d in cap_thicknesses_mm],
            dtype=np.float32,
        )
        return (
            to_oklab(wb_trans[np.newaxis, :] * wc_trans).astype(np.float32),
            to_oklab(wc_trans).astype(np.float32),
        )

    def predict_thickness_maps_srgb(
        self,
        *,
        thickness_maps: Mapping[str, np.ndarray],
        white_base: Layer,
        white_cap_id: str,
        layer_height: float,
        max_layers: int,
        color_order: Sequence[str] | None = None,
    ) -> np.ndarray:
        return _srgb8_from_linear(
            self.predict_thickness_maps_linear_rgb(
                thickness_maps=thickness_maps,
                white_base=white_base,
                white_cap_id=white_cap_id,
                layer_height=layer_height,
                max_layers=max_layers,
                color_order=color_order,
            )
        )

    def predict_thickness_maps_linear_rgb(
        self,
        *,
        thickness_maps: Mapping[str, np.ndarray],
        white_base: Layer,
        white_cap_id: str,
        layer_height: float,
        max_layers: int,
        color_order: Sequence[str] | None = None,
    ) -> np.ndarray:
        fids = [k for k in thickness_maps if not k.startswith("__")]
        wc_map = np.asarray(thickness_maps[MapKey.WHITE_CAP], dtype=np.float32)
        h, w = np.asarray(thickness_maps[fids[0]]).shape if fids else wc_map.shape
        steps = [round(i * float(layer_height), 6) for i in range(int(max_layers) + 1)]
        T_wb = np.asarray(predict_transmission(self.wb_profile, float(white_base[1])), dtype=np.float32)
        color_luts = {
            fid: np.asarray(
                [predict_transmission(self.color_profiles[fid], d) for d in steps],
                dtype=np.float32,
            )
            for fid in fids
        }
        cap_lut = {
            float(d): np.asarray(predict_transmission(self.wc_profile, float(d)), dtype=np.float32)
            for d in np.unique(wc_map)
        }
        rgb = np.ones((h, w, 3), dtype=np.float32) * T_wb[np.newaxis, np.newaxis, :]
        for fid in fids:
            idx_map = (
                np.round(np.asarray(thickness_maps[fid], dtype=np.float32) / float(layer_height))
                .astype(int)
                .clip(0, int(max_layers))
            )
            rgb *= color_luts[fid][idx_map]
        for d_cap, T_cap in cap_lut.items():
            rgb[wc_map == d_cap] *= T_cap[np.newaxis, :]
        return np.clip(rgb, 0.0, 1.0).astype(np.float32)


class PhotoStackBundleAppearanceProvider:
    """Photo Stack provider backed by an explicit published artifact.

    The Generator passes either a published deployment directory or its
    ``runtime_bundle.json`` path.  Corrections remain a separate sibling
    artifact so the user can compare the direct model against the usable
    corrected model without changing the baseline bundle.

    Domain contract: model-domain RGB is the flatfield-normalized
    model/correction output.  The appearance-named methods remain as generator
    API names, but they now pass model-domain values through exactly.  Display
    conversion lives at the generator ingress/display boundaries.
    """

    model_kind = "photo_stack_bundle"

    def __init__(
        self,
        *,
        bundle_path: str | Path | None = None,
        use_corrections: bool = False,
    ) -> None:
        if bundle_path is None:
            raise ValueError(
                "Photo Stack appearance provider requires an explicit published deployment "
                "directory or runtime bundle path"
            )
        from lib.photo_stack_model.correction_layer import CORRECTION_SCHEMA
        from lib.photo_stack_model.model import load_candidate
        from lib.photo_stack_model.bundle import load_photo_stack_bundle

        self.use_corrections = bool(use_corrections)
        self.corrections: dict | None = None
        source_path = Path(bundle_path).resolve()
        if source_path.is_dir():
            candidate = load_candidate(source_path)
            self.corrections = candidate.corrections
            runtime_path = source_path / "runtime_bundle.json"
            self._candidate_dir = source_path
        else:
            runtime_path = source_path
            self._candidate_dir = source_path.parent if (source_path.parent / "manifest.json").exists() else None
            corrections_path = source_path.parent / "correction_layer.json"
            if corrections_path.exists():
                try:
                    with open(corrections_path, encoding="utf-8") as f:
                        self.corrections = json.load(f)
                except Exception as exc:
                    raise ValueError(f"failed to load photo stack correction artifact: {corrections_path}") from exc

        if self.use_corrections:
            if not isinstance(self.corrections, dict) or self.corrections.get("schema") != CORRECTION_SCHEMA:
                raise ValueError(
                    "photo stack corrections were requested but no valid "
                    "correction_layer.json artifact was found beside the runtime bundle"
                )

        self.bundle = load_photo_stack_bundle(runtime_path)
        from lib.photo_stack_model import predictor as runtime_predictor

        self.predictor = runtime_predictor.predictor_for_bundle(self.bundle)
        logic_key = f"photo_stack_logic:{runtime_predictor.PHOTO_STACK_PREDICTOR_LOGIC_VERSION}"
        correction_key = "direct"
        if self.use_corrections:
            payload = json.dumps(self.corrections, sort_keys=True, separators=(",", ":")).encode()
            correction_key = f"corrected:{hashlib.sha256(payload).hexdigest()[:16]}"
        self._fingerprint = f"photo_stack_bundle:{self.bundle.fingerprint}:{logic_key}:{correction_key}:{APPEARANCE_DOMAIN_VERSION}"

    def fingerprint(self) -> str:
        return self._fingerprint

    @staticmethod
    def _physical_layers(
        *,
        white_base: Layer,
        color_layers: Sequence[Layer],
        white_cap: Layer,
    ) -> list[Layer]:
        layers: list[Layer] = [
            (str(white_base[0]), float(white_base[1])),
            *[(str(fid), float(thickness)) for fid, thickness in color_layers],
            (str(white_cap[0]), float(white_cap[1])),
        ]
        return list(collapse_adjacent_layers(layers, precision=5, drop_zero=True))

    @staticmethod
    def _prediction_row_from_physical_layers(layers: Sequence[Layer]) -> dict:
        """Build the model runtime row shape without per-stack DataFrame churn."""
        from lib.photo_stack_model.predictor import _runtime_evidence_class, is_white

        layer_list = list(layers)
        if not layer_list:
            raise ValueError("photo stack prediction requires at least one nonzero layer")
        fixed = layer_list[:-1]
        variable_fid, variable_t = layer_list[-1]
        layers_from_row = [
            (
                str(fid),
                float(thickness),
                "fixed_white" if is_white(fid) else "fixed_color",
            )
            for fid, thickness in fixed
        ]
        if float(variable_t) > 0.0:
            layers_from_row.append(
                (
                    str(variable_fid),
                    float(variable_t),
                    "variable_white_cap" if is_white(variable_fid) else "variable_color",
                )
            )
        canonical_groups: list[tuple[str, float, str]] = []
        for fid, thickness, role in layers_from_row:
            role_key = "cap_white" if is_white(fid) and "cap" in role else "base_white" if is_white(fid) else "color"
            if canonical_groups and canonical_groups[-1][0] == fid and canonical_groups[-1][2] == role_key:
                prev_fid, prev_t, prev_role = canonical_groups[-1]
                canonical_groups[-1] = (prev_fid, prev_t + float(thickness), prev_role)
            else:
                canonical_groups.append((fid, float(thickness), role_key))
        color_layers = [(fid, thickness) for fid, thickness, role in canonical_groups if role == "color"]
        color_totals: dict[str, float] = {}
        base_thickness = 0.0
        cap_thickness = 0.0
        for fid, thickness, role in canonical_groups:
            if role == "color":
                color_totals[fid] = color_totals.get(fid, 0.0) + float(thickness)
            elif role == "base_white":
                base_thickness += float(thickness)
            elif role == "cap_white":
                cap_thickness += float(thickness)
        return {
            "sample_id": "__runtime_stack__",
            "swatch_index0": 0,
            "fixed_ids_list": [fid for fid, _thickness in fixed],
            "fixed_thickness_list": [float(thickness) for _fid, thickness in fixed],
            "variable_filament_id": str(variable_fid),
            "nominal_variable_thickness_mm": float(variable_t),
            "all_color_ids_list": [fid for fid, _thickness in layer_list if not is_white(fid)],
            "evidence_class": _runtime_evidence_class(layer_list),
            "layers_json": json.dumps(
                [
                    {"filament_id": fid, "thickness_mm": float(thickness)}
                    for fid, thickness in layer_list
                ],
                separators=(",", ":"),
            ),
            "_layers_from_row": layers_from_row,
            "_canonical_layer_groups": canonical_groups,
            "_stack_thickness_descriptor": {
                "color_layers": color_layers,
                "color_totals": color_totals,
                "unique_color_ids": list(color_totals.keys()),
                "total_color_thickness": float(sum(color_totals.values())),
                "cap_thickness": float(cap_thickness),
                "base_thickness": float(base_thickness),
            },
        }

    def predict_stack_linear_rgb(
        self,
        *,
        white_base: Layer,
        color_layers: Sequence[Layer],
        white_cap: Layer,
    ) -> np.ndarray:
        rows = self.predict_stack_linear_rgb_batch(
            [StackRequest(white_base=white_base, color_layers=tuple(color_layers), white_cap=white_cap)]
        )
        return rows[0]

    def predict_stack_model_linear_rgb_batch(self, requests: Sequence[StackRequest]) -> np.ndarray:
        import pandas as pd
        from lib.photo_stack_model.correction_layer import apply_photo_stack_correction
        from lib.photo_stack_model.predict import CORRECTED_MODEL_NAME
        from lib.photo_stack_model.predictor import (
            MODEL_NAME as PHOTO_STACK_MODEL_NAME,
        )

        from lib.photo_stack_model.predictor import model_white_classifier_context

        classifier = self.bundle.model_white_classifier
        for req in requests:
            if not classifier.is_white(req.white_base[0]) or not classifier.is_white(req.white_cap[0]):
                raise ValueError(
                    "photo stack provider requires configured white_base and white_cap "
                    "to be model-white in the runtime bundle classifier"
                )
        with model_white_classifier_context(self.bundle.model_white_classifier):
            rows = [
                self._prediction_row_from_physical_layers(
                    self._physical_layers(
                        white_base=req.white_base,
                        color_layers=req.color_layers,
                        white_cap=req.white_cap,
                    )
                )
                for req in requests
            ]
        if not rows:
            return np.zeros((0, 3), dtype=np.float32)
        pred = self.predictor.predict_rows(pd.DataFrame(rows), model_name=PHOTO_STACK_MODEL_NAME)
        model_name = PHOTO_STACK_MODEL_NAME
        if self.use_corrections:
            pred = apply_photo_stack_correction(
                pred,
                self.corrections or {},
                model_name=CORRECTED_MODEL_NAME,
                classifier=self.bundle.model_white_classifier,
            )
            model_name = CORRECTED_MODEL_NAME
        cols = [f"{model_name}_r_linear", f"{model_name}_g_linear", f"{model_name}_b_linear"]
        return pred[cols].to_numpy(dtype=np.float32)

    def predict_stack_linear_rgb_batch(self, requests: Sequence[StackRequest]) -> np.ndarray:
        """Compatibility alias for model-domain stack predictions."""

        return self.predict_stack_model_linear_rgb_batch(requests)

    def project_model_linear_rgb_to_appearance(self, rgb: np.ndarray) -> np.ndarray:
        """Compatibility pass-through for the retired solve-domain conversion."""

        return np.asarray(rgb)

    def predict_stack_appearance_linear_rgb_batch(self, requests: Sequence[StackRequest]) -> np.ndarray:
        return self.predict_stack_model_linear_rgb_batch(requests)

    def sample_white_cap_oklab(
        self,
        *,
        white_base: Layer,
        white_cap_id: str,
        cap_thicknesses_mm: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray]:
        requests = [
            StackRequest(
                white_base=white_base,
                color_layers=(),
                white_cap=(white_cap_id, float(d)),
            )
            for d in np.asarray(cap_thicknesses_mm, dtype=np.float32)
        ]
        full_rgb = self.predict_stack_appearance_linear_rgb_batch(requests)
        cap_only = [
            StackRequest(
                white_base=(white_cap_id, 0.0),
                color_layers=(),
                white_cap=(white_cap_id, float(d)),
            )
            for d in np.asarray(cap_thicknesses_mm, dtype=np.float32)
        ]
        cap_rgb = self.predict_stack_appearance_linear_rgb_batch(cap_only)
        return to_oklab(full_rgb).astype(np.float32), to_oklab(cap_rgb).astype(np.float32)

    def predict_thickness_maps_srgb(
        self,
        *,
        thickness_maps: Mapping[str, np.ndarray],
        white_base: Layer,
        white_cap_id: str,
        layer_height: float,
        max_layers: int,
        color_order: Sequence[str] | None = None,
    ) -> np.ndarray:
        return _srgb8_from_linear(
            self.predict_thickness_maps_appearance_linear_rgb(
                thickness_maps=thickness_maps,
                white_base=white_base,
                white_cap_id=white_cap_id,
                layer_height=layer_height,
                max_layers=max_layers,
                color_order=color_order,
            )
        )

    def predict_thickness_maps_model_linear_rgb(
        self,
        *,
        thickness_maps: Mapping[str, np.ndarray],
        white_base: Layer,
        white_cap_id: str,
        layer_height: float,
        max_layers: int,
        color_order: Sequence[str] | None = None,
    ) -> np.ndarray:
        wc_map = np.asarray(thickness_maps[MapKey.WHITE_CAP], dtype=np.float32)
        h, w = wc_map.shape
        fids = [fid for fid in (color_order or []) if fid in thickness_maps]
        fids.extend(sorted(fid for fid in thickness_maps if not fid.startswith("__") and fid not in fids))
        if not fids:
            combo = wc_map.reshape(-1, 1)
        else:
            combo = np.column_stack([np.asarray(thickness_maps[fid], dtype=np.float32).reshape(-1) for fid in fids] + [wc_map.reshape(-1)])
        rounded = np.round(combo.astype(np.float64), 6)
        unique, inverse = np.unique(rounded, axis=0, return_inverse=True)
        unique_rgb = self._predict_unique_combo_model_linear_rgb(
            unique,
            fids,
            white_base=white_base,
            white_cap_id=white_cap_id,
            layer_height=float(layer_height),
            max_layers=int(max_layers),
        )
        rgb = unique_rgb[inverse].reshape(h, w, 3)
        return np.clip(rgb, 0.0, 1.0).astype(np.float32)

    def _predict_unique_combo_model_linear_rgb(
        self,
        unique: np.ndarray,
        fids: Sequence[str],
        *,
        white_base: Layer,
        white_cap_id: str,
        layer_height: float,
        max_layers: int,
    ) -> np.ndarray:
        """Predict deduplicated (colors..., cap) rows in model-domain RGB.

        Grid-aligned rows go through the vectorized LUT evaluator; the
        row-shaped predictor remains the fallback oracle for off-grid
        thicknesses so callers never lose exactness, only speed.
        """

        k = len(fids)
        counts = np.zeros((len(unique), k), dtype=np.int32)
        on_grid = True
        if k:
            color_mm = unique[:, :k]
            counts = np.rint(color_mm / layer_height).astype(np.int32)
            on_grid = (
                float(np.max(np.abs(counts * layer_height - color_mm), initial=0.0)) <= 1e-6
                and int(counts.min(initial=0)) >= 0
                and int(counts.max(initial=0)) <= int(max_layers)
            )
        if on_grid:
            from photo_stack_lut import predict_combo_model_linear_rgb

            cap_steps, cap_indices = np.unique(unique[:, -1], return_inverse=True)
            return predict_combo_model_linear_rgb(
                self,
                fids=list(fids),
                counts=counts,
                cap_steps_d=cap_steps,
                cap_indices=cap_indices,
                white_base=str(white_base[0]),
                d_wb=float(white_base[1]),
                white_cap=str(white_cap_id),
                layer_height=float(layer_height),
                max_layers=int(max_layers),
            )
        requests: list[StackRequest] = []
        for row in unique:
            colors = tuple(
                (fid, float(row[index]))
                for index, fid in enumerate(fids)
                if float(row[index]) > 1e-9
            )
            requests.append(
                StackRequest(
                    white_base=(white_base[0], float(white_base[1])),
                    color_layers=colors,
                    white_cap=(white_cap_id, float(row[-1])),
                )
            )
        return self.predict_stack_model_linear_rgb_batch(requests)

    def predict_thickness_maps_appearance_linear_rgb(
        self,
        *,
        thickness_maps: Mapping[str, np.ndarray],
        white_base: Layer,
        white_cap_id: str,
        layer_height: float,
        max_layers: int,
        color_order: Sequence[str] | None = None,
    ) -> np.ndarray:
        return self.predict_thickness_maps_model_linear_rgb(
            thickness_maps=thickness_maps,
            white_base=white_base,
            white_cap_id=white_cap_id,
            layer_height=layer_height,
            max_layers=max_layers,
            color_order=color_order,
        )

    def predict_thickness_maps_linear_rgb(
        self,
        *,
        thickness_maps: Mapping[str, np.ndarray],
        white_base: Layer,
        white_cap_id: str,
        layer_height: float,
        max_layers: int,
        color_order: Sequence[str] | None = None,
    ) -> np.ndarray:
        """Compatibility alias for model-domain thickness-map predictions."""

        return self.predict_thickness_maps_model_linear_rgb(
            thickness_maps=thickness_maps,
            white_base=white_base,
            white_cap_id=white_cap_id,
            layer_height=layer_height,
            max_layers=max_layers,
            color_order=color_order,
        )


def create_appearance_provider(
    *,
    provider_kind: str,
    color_profiles: Mapping[str, dict],
    wb_profile: dict,
    wc_profile: dict,
    photo_stack_bundle_path: str | Path | None = None,
    use_corrections: bool = False,
):
    """Create the selected provider while preserving historical default behavior."""

    kind = str(provider_kind or "historical_spline").strip().lower()
    if kind in {"historical", "historical_spline", "spline"}:
        return HistoricalSplineAppearanceProvider(
            color_profiles=color_profiles,
            wb_profile=wb_profile,
            wc_profile=wc_profile,
        )
    if kind == "photo_stack_bundle":
        return PhotoStackBundleAppearanceProvider(
            bundle_path=photo_stack_bundle_path,
            use_corrections=use_corrections,
        )
    raise ValueError(f"unknown appearance_model_provider: {provider_kind!r}")
