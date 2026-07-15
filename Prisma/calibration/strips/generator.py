"""Swatch-strip math and lightweight rectilinear STEP/STL generation."""

from __future__ import annotations
from dataclasses import dataclass
import math
from pathlib import Path

try:
    from ..geometry_builder import RectPrism
    from ..lightweight_step import (
        RectilinearComponent,
        build_rectilinear_components,
        write_lightweight_step,
        write_lightweight_stl,
    )
except ImportError:
    from geometry_builder import RectPrism
    from lightweight_step import (
        RectilinearComponent,
        build_rectilinear_components,
        write_lightweight_step,
        write_lightweight_stl,
    )


# ── Physical constants ───────────────────────────────────────────────────────

SWATCH_W = 12.0    # mm, width of each swatch cell (along the 102mm long axis)
SWATCH_D = 20.0    # mm, depth of each swatch cell (along the 23mm short axis)
BORDER   = 3.0     # mm, border on left, right, and back sides

N_SWATCHES = 8


@dataclass(frozen=True)
class _BoundsPoint:
    X: float
    Y: float
    Z: float


@dataclass(frozen=True)
class _Bounds:
    min: _BoundsPoint
    max: _BoundsPoint


@dataclass
class LayerGeometry:
    """One legacy filament layer represented only by rectangular prisms."""

    prisms: tuple[RectPrism, ...]
    label: str = ""

    def bounding_box(self) -> _Bounds:
        if not self.prisms:
            raise ValueError("Layer geometry is empty")
        return _Bounds(
            min=_BoundsPoint(
                min(prism.x_min for prism in self.prisms),
                min(prism.y_min for prism in self.prisms),
                min(prism.z_min for prism in self.prisms),
            ),
            max=_BoundsPoint(
                max(prism.x_max for prism in self.prisms),
                max(prism.y_max for prism in self.prisms),
                max(prism.z_max for prism in self.prisms),
            ),
        )


# ── Rounding ────────────────────────────────────────────────────────────────

def snap_offset(value: float, anchor: float, layer_height: float) -> float:
    """
    Round `value` to the nearest layer_height multiple offset from `anchor`.
    e.g. anchor=0.2, layer_height=0.16, value=0.7429 → 0.68
    """
    if layer_height <= 0:
        return round(value, 6)
    offset = value - anchor
    n = round(offset / layer_height)
    return round(anchor + n * layer_height, 6)


# ── Thickness computation ────────────────────────────────────────────────────

def compute_linear_stop(
    anchor: float, stop: float, layer_height: float, n: int = N_SWATCHES
) -> list[float]:
    """Linear spacing anchor→stop, subsequent swatches snapped via snap_offset."""
    if n <= 1:
        return [anchor]
    result = [anchor]
    for i in range(1, n):
        ideal = anchor + (stop - anchor) * i / (n - 1)
        result.append(snap_offset(ideal, anchor, layer_height))
    return result


def compute_linear_increment(
    anchor: float, increment: float, layer_height: float, n: int = N_SWATCHES
) -> list[float]:
    """Linear spacing using a fixed increment (already a layer_height multiple)."""
    snapped_inc = snap_offset(anchor + increment, anchor, layer_height) - anchor
    return [round(anchor + i * snapped_inc, 6) for i in range(n)]


def compute_log_stop(
    anchor: float, stop: float, layer_height: float, n: int = N_SWATCHES
) -> list[float]:
    """Log spacing anchor→stop, subsequent swatches snapped via snap_offset."""
    if n <= 1 or anchor <= 0 or stop <= 0:
        return [anchor] * n
    log_a = math.log(anchor)
    log_s = math.log(stop)
    result = [anchor]
    for i in range(1, n):
        ideal = math.exp(log_a + (log_s - log_a) * i / (n - 1))
        result.append(snap_offset(ideal, anchor, layer_height))
    return result


def derive_increment(
    anchor: float, stop: float, layer_height: float, n: int = N_SWATCHES
) -> float:
    """Given start+stop, return the snapped increment that would be used."""
    if n <= 1:
        return 0.0
    ideal_inc = (stop - anchor) / (n - 1)
    return snap_offset(anchor + ideal_inc, anchor, layer_height) - anchor


def derive_stop(anchor: float, increment: float, n: int = N_SWATCHES) -> float:
    """Given start+increment, return the resulting stop value."""
    return round(anchor + (n - 1) * increment, 6)


# ── STEP file naming ─────────────────────────────────────────────────────────

def step_filename(
    n_layers: int,
    variable_thicknesses: list[float],
    fixed_thicknesses: list[float],
    layer_height: float,
) -> str:
    """
    Canonical STEP filename from geometry only (filament-agnostic).
    fixed_thicknesses: ordered bottom→top (fixed layer N first, fixed layer 1 last).
    """
    v = "-".join(f"{t:.2f}" for t in variable_thicknesses)
    lh = f"{layer_height:.2f}"
    if fixed_thicknesses:
        f = "-".join(f"{t:.2f}" for t in fixed_thicknesses)
        return f"{n_layers}L_v-{v}_f-{f}_lh{lh}.step"
    return f"{n_layers}L_v-{v}_lh{lh}.step"


def manual_step_filename(
    n_layers: int,
    variable_thicknesses: list[float],
    fixed_thicknesses: list[float],
    layer_height: float,
) -> str:
    """STEP filename for manually-entered thicknesses."""
    v = "|".join(f"{t:.2f}" for t in variable_thicknesses)
    lh = f"{layer_height:.2f}"
    if fixed_thicknesses:
        f = "-".join(f"{t:.2f}" for t in fixed_thicknesses)
        return f"{n_layers}L_manual-{v}_f-{f}_lh{lh}.step"
    return f"{n_layers}L_manual-{v}_lh{lh}.step"


# ── Experiment naming ────────────────────────────────────────────────────────

def experiment_name(
    filament_id: str,
    mode: str,
    anchor: float,
    stop: float,
    layer_height: float,
    thicknesses: list[float] | None = None,
) -> str:
    lh = f"lh{layer_height:.2f}"
    if mode == "manual" and thicknesses:
        t_str = "|".join(f"{t:.2f}" for t in thicknesses)
        return f"{filament_id}_manual-{t_str}_{lh}"
    elif mode == "log":
        return f"{filament_id}_log-{anchor:.2f}-{stop:.2f}_{lh}"
    else:
        return f"{filament_id}_linear-{anchor:.2f}-{stop:.2f}_{lh}"


# ── Rectilinear STEP/STL geometry ─────────────────────────────────────────────

def build_fixed_layer(
    thickness: float,
    z_offset: float = 0.0,
) -> LayerGeometry:
    """
    Build the fixed base layer as a single flat box covering the full strip footprint.
    The border is included in this solid (it is the full plate).

    Returns one lightweight rectangular layer plan.
    """
    total_w = BORDER + N_SWATCHES * SWATCH_W + BORDER
    total_d = SWATCH_D + BORDER
    if not math.isfinite(thickness) or thickness <= 0:
        raise ValueError("Fixed-layer thickness must be finite and greater than zero")
    return LayerGeometry(
        prisms=(RectPrism(0.0, total_w, 0.0, total_d, z_offset, z_offset + thickness),)
    )


def build_variable_layer(
    variable_thicknesses: list[float],
    z_offset: float = 0.0,
    layer_height: float = 0.1,
) -> LayerGeometry | None:
    """
    Build the variable (top) layer as a single fused solid.

    Swatch cells are stair-stepped (thickest left, thinnest right).
    The spine border (left, right, back) is part of this solid and uses
    border_h = max_t + layer_height to cap the top of the strip.

    Returns a lightweight prism-union layer plan.
    """
    n = len(variable_thicknesses)
    max_t = max(variable_thicknesses) if variable_thicknesses else 0.0
    border_h = max_t + layer_height
    total_w = BORDER + n * SWATCH_W + BORDER
    total_d = SWATCH_D + BORDER

    if border_h <= 0:
        return None

    sorted_thicknesses = sorted(variable_thicknesses, reverse=True)

    prisms = [
        RectPrism(0.0, BORDER, 0.0, total_d, z_offset, z_offset + border_h),
        RectPrism(total_w - BORDER, total_w, 0.0, total_d, z_offset, z_offset + border_h),
        RectPrism(BORDER, total_w - BORDER, 0.0, BORDER, z_offset, z_offset + border_h),
    ]
    for index, thickness in enumerate(sorted_thicknesses):
        if thickness <= 0:
            continue
        x_min = BORDER + index * SWATCH_W
        prisms.append(
            RectPrism(
                x_min,
                x_min + SWATCH_W,
                BORDER,
                BORDER + SWATCH_D,
                z_offset,
                z_offset + thickness,
            )
        )
    return LayerGeometry(prisms=tuple(prisms))


def _format_mm_label(value: float) -> str:
    return f"{float(value):.2f}"


def fixed_layer_label(index: int, thickness: float) -> str:
    """Label for fixed-layer STEP solids; index is bottom-to-top."""
    return f"fixed layer {index} {_format_mm_label(thickness)} mm"


def variable_layer_label(variable_thicknesses: list[float]) -> str:
    if not variable_thicknesses:
        return "variable"
    min_t = min(float(t) for t in variable_thicknesses)
    max_t = max(float(t) for t in variable_thicknesses)
    return f"variable {_format_mm_label(min_t)} - {_format_mm_label(max_t)} mm"


def build_fixed_layer_stack(
    fixed_thicknesses: list[float],
) -> tuple[list[LayerGeometry], float]:
    """
    Build fixed-layer solids from canonical bottom-to-top layer order.

    The first fixed thickness is physically on the bottom. Returned solids
    preserve the canonical order for STEP part naming/selection.
    """
    layers: list[LayerGeometry] = []
    z = 0.0

    for idx, t in enumerate(fixed_thicknesses, 1):
        layer = build_fixed_layer(t, z_offset=z)
        layer.label = fixed_layer_label(idx, t)
        layers.append(layer)
        z += t

    return layers, z


def generate_stls(
    variable_thicknesses: list[float],
    fixed_thicknesses: list[float],
    output_dir: str | Path,
    base_name: str,
    layer_height: float = 0.1,
) -> list[Path]:
    """
    Export one STL per filament layer into output_dir.

    Files are named:
      {base_name}_layer1.stl  (bottom-most fixed layer)
      {base_name}_layer2.stl  (next fixed layer, if any)
      {base_name}_variable.stl  (top variable layer)

    Returns list of exported Paths in bottom→top order.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    paths = []
    fixed_layers, z = build_fixed_layer_stack(fixed_thicknesses)

    for idx, layer in enumerate(fixed_layers, 1):
        p = output_dir / f"{base_name}_layer{idx}.stl"
        components = build_rectilinear_components(
            name=layer.label,
            role_index=idx,
            prisms=layer.prisms,
        )
        write_lightweight_stl(components, p, solid_name=layer.label)
        paths.append(p)

    var_layer = build_variable_layer(variable_thicknesses, z_offset=z,
                                     layer_height=layer_height)
    if var_layer is not None:
        var_layer.label = variable_layer_label(variable_thicknesses)
        p = output_dir / f"{base_name}_variable.stl"
        components = build_rectilinear_components(
            name=var_layer.label,
            role_index=len(fixed_layers) + 1,
            prisms=var_layer.prisms,
        )
        write_lightweight_stl(components, p, solid_name=var_layer.label)
        paths.append(p)

    if not paths:
        raise ValueError("No geometry to export — all thicknesses are zero.")

    return paths


def generate_step(
    variable_thicknesses: list[float],
    fixed_thicknesses: list[float],
    output_path: str | Path,
    layer_height: float = 0.1,
) -> Path:
    """
    Generate a STEP file for a swatch strip.

    For single-filament strips: one compound solid exported as one STEP file.
    For multi-filament strips: each layer is a separate solid in the same STEP
    file (importable as separate parts in OrcaSlicer).

    fixed_thicknesses: fixed layer thicknesses, bottom-to-top below variable.
    variable_thicknesses: thicknesses of the topmost (variable) layer per swatch.

    Returns the output Path.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    layers: list[LayerGeometry] = []

    # Fixed layers — each is a single flat-plate solid (includes its border region).
    fixed_layers, z = build_fixed_layer_stack(fixed_thicknesses)
    layers.extend(fixed_layers)

    # Variable (top) layer — staircase + spine border, one fused solid
    var_layer = build_variable_layer(variable_thicknesses, z_offset=z,
                                     layer_height=layer_height)
    if var_layer is not None:
        var_layer.label = variable_layer_label(variable_thicknesses)
        layers.append(var_layer)

    if not layers:
        raise ValueError("No geometry to export — all thicknesses are zero.")

    components: list[RectilinearComponent] = []
    for role_index, layer in enumerate(layers, start=1):
        components.extend(
            build_rectilinear_components(
                name=layer.label,
                role_index=role_index,
                prisms=layer.prisms,
            )
        )
    return write_lightweight_step(
        components,
        output_path,
        document_name="swatch strip",
    )
