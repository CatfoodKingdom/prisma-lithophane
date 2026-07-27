"""Shared coarse-grid projection and sampling kernels."""
from __future__ import annotations


import numpy as np




def _effective_stage1_coarsening_factor(cfg) -> int:
    """Return the experimental Stage 1 coarse-to-fine scale factor."""
    raw = int(cfg.stage1_coarsening_factor or 1)
    return max(1, raw)

def _stage1_lattice_offset_px(cfg) -> tuple[int, int]:
    """Return the experimental projected Stage 1 lattice offset in fine pixels."""
    y_px = int(cfg.stage1_lattice_offset_y_px or 0)
    x_px = int(cfg.stage1_lattice_offset_x_px or 0)
    return y_px, x_px

def _coarsened_shape(shape: tuple[int, int], factor: int) -> tuple[int, int]:
    """Return the coarse grid shape for an integer downsampling factor."""
    h, w = int(shape[0]), int(shape[1])
    factor = max(1, int(factor))
    return ((h + factor - 1) // factor, (w + factor - 1) // factor)

def _coarse_lattice_indices(
    length: int,
    factor: int,
    offset_px: int,
    coarse_length: int,
) -> np.ndarray:
    """Map fine-grid coordinates to a shifted coarse lattice."""
    factor = max(1, int(factor))
    coarse_length = max(1, int(coarse_length))
    coords = np.arange(int(length), dtype=np.int32)
    indices = np.floor_divide(coords - int(offset_px), factor)
    return np.clip(indices, 0, coarse_length - 1).astype(np.int32, copy=False)

def _downsample_rgb_image(
    image: np.ndarray,
    factor: int,
    *,
    offset_y_px: int = 0,
    offset_x_px: int = 0,
) -> np.ndarray:
    """Downsample an RGB image by block averaging without changing extents."""
    factor = max(1, int(factor))
    source = np.asarray(image)
    source_is_float = np.issubdtype(source.dtype, np.floating)

    def _finish_downsampled_rgb(arr: np.ndarray) -> np.ndarray:
        if source_is_float:
            return np.clip(arr, 0.0, 1.0).astype(np.float32, copy=False)
        return np.clip(np.rint(arr), 0.0, 255.0).astype(np.uint8)

    if factor == 1:
        return _finish_downsampled_rgb(source).copy()
    h, w = image.shape[:2]
    coarse_h, coarse_w = _coarsened_shape((h, w), factor)
    if int(offset_y_px) != 0 or int(offset_x_px) != 0:
        image_f = source.astype(np.float64, copy=False)
        y_idx = _coarse_lattice_indices(h, factor, int(offset_y_px), coarse_h)
        x_idx = _coarse_lattice_indices(w, factor, int(offset_x_px), coarse_w)
        flat_idx = (y_idx[:, None] * coarse_w + x_idx[None, :]).reshape(-1)
        channel_count = int(image.shape[2])
        accum = np.zeros((coarse_h * coarse_w, channel_count), dtype=np.float64)
        np.add.at(accum, flat_idx, image_f.reshape(-1, channel_count))
        counts = np.bincount(flat_idx, minlength=coarse_h * coarse_w).astype(np.float64)
        coarse = np.divide(
            accum,
            np.maximum(counts[:, None], 1.0),
            out=np.zeros_like(accum),
            where=counts[:, None] > 0.0,
        )
        return _finish_downsampled_rgb(coarse.reshape(coarse_h, coarse_w, channel_count))
    accum = np.zeros((coarse_h, coarse_w, image.shape[2]), dtype=np.float64)
    counts = np.zeros((coarse_h, coarse_w, 1), dtype=np.float64)
    image_f = source.astype(np.float64, copy=False)
    for oy in range(factor):
        rows = image_f[oy::factor, :, :]
        if rows.size == 0:
            continue
        for ox in range(factor):
            block = rows[:, ox::factor, :]
            if block.size == 0:
                continue
            bh, bw = block.shape[:2]
            accum[:bh, :bw, :] += block
            counts[:bh, :bw, 0] += 1.0
    coarse = np.divide(accum, np.maximum(counts, 1.0), out=np.zeros_like(accum), where=counts > 0.0)
    return _finish_downsampled_rgb(coarse)

def _downsample_flat_oklab_targets(
    targets: np.ndarray,
    fine_shape: tuple[int, int],
    factor: int,
    *,
    offset_y_px: int = 0,
    offset_x_px: int = 0,
) -> np.ndarray:
    """Downsample flattened solve-grid OKLab targets onto a coarse lattice."""
    factor = max(1, int(factor))
    target_arr = np.asarray(targets, dtype=np.float32)
    if factor == 1:
        return target_arr.astype(np.float32, copy=True)
    h, w = int(fine_shape[0]), int(fine_shape[1])
    coarse_h, coarse_w = _coarsened_shape((h, w), factor)
    target_grid = target_arr.reshape(h, w, 3).astype(np.float64, copy=False)
    if int(offset_y_px) != 0 or int(offset_x_px) != 0:
        y_idx = _coarse_lattice_indices(h, factor, int(offset_y_px), coarse_h)
        x_idx = _coarse_lattice_indices(w, factor, int(offset_x_px), coarse_w)
        flat_idx = (y_idx[:, None] * coarse_w + x_idx[None, :]).reshape(-1)
        accum = np.zeros((coarse_h * coarse_w, 3), dtype=np.float64)
        np.add.at(accum, flat_idx, target_grid.reshape(-1, 3))
        counts = np.bincount(flat_idx, minlength=coarse_h * coarse_w).astype(np.float64)
        coarse = np.divide(
            accum,
            np.maximum(counts[:, None], 1.0),
            out=np.zeros_like(accum),
            where=counts[:, None] > 0.0,
        )
        return coarse.astype(np.float32).reshape(-1, 3)
    accum = np.zeros((coarse_h, coarse_w, 3), dtype=np.float64)
    counts = np.zeros((coarse_h, coarse_w, 1), dtype=np.float64)
    for oy in range(factor):
        rows = target_grid[oy::factor, :, :]
        if rows.size == 0:
            continue
        for ox in range(factor):
            block = rows[:, ox::factor, :]
            if block.size == 0:
                continue
            bh, bw = block.shape[:2]
            accum[:bh, :bw, :] += block
            counts[:bh, :bw, 0] += 1.0
    coarse = np.divide(accum, np.maximum(counts, 1.0), out=np.zeros_like(accum), where=counts > 0.0)
    return coarse.reshape(-1, 3).astype(np.float32)

def _project_zone_labels_to_fine(
    coarse_zone_labels: np.ndarray,
    factor: int,
    fine_shape: tuple[int, int],
    *,
    offset_y_px: int = 0,
    offset_x_px: int = 0,
) -> np.ndarray:
    """Project a coarse zone raster onto the fine evaluation lattice."""
    factor = max(1, int(factor))
    coarse_arr = np.asarray(coarse_zone_labels, dtype=np.int32)
    fine_h, fine_w = int(fine_shape[0]), int(fine_shape[1])
    if (
        factor == 1
        and coarse_arr.shape == (fine_h, fine_w)
        and int(offset_y_px) == 0
        and int(offset_x_px) == 0
    ):
        return coarse_arr.astype(np.int32, copy=True)
    y_idx = _coarse_lattice_indices(fine_h, factor, int(offset_y_px), coarse_arr.shape[0])
    x_idx = _coarse_lattice_indices(fine_w, factor, int(offset_x_px), coarse_arr.shape[1])
    return coarse_arr[y_idx[:, None], x_idx[None, :]].astype(np.int32, copy=False)

def _stage2_coarse_lattice_edge_masks(
    shape: tuple[int, int],
    scale: int,
    *,
    offset_y_px: int = 0,
    offset_x_px: int = 0,
) -> tuple[np.ndarray, np.ndarray]:
    """Return edge masks where adjacent pixels cross the projected coarse lattice."""
    h, w = int(shape[0]), int(shape[1])
    scale = int(scale)
    if scale <= 1:
        return np.zeros((max(0, h - 1), w), dtype=bool), np.zeros((h, max(0, w - 1)), dtype=bool)
    coarse_h, coarse_w = _coarsened_shape((h, w), scale)
    y_idx = _coarse_lattice_indices(h, scale, int(offset_y_px), coarse_h)
    x_idx = _coarse_lattice_indices(w, scale, int(offset_x_px), coarse_w)
    y_cross = y_idx[1:] != y_idx[:-1]
    x_cross = x_idx[1:] != x_idx[:-1]
    y_lattice = np.broadcast_to(y_cross[:, None], (max(0, h - 1), w)).astype(bool, copy=True)
    x_lattice = np.broadcast_to(x_cross[None, :], (h, max(0, w - 1))).astype(bool, copy=True)
    return y_lattice, x_lattice

def _stage2_coarse_lattice_pixel_mask(
    shape: tuple[int, int],
    scale: int,
    *,
    offset_y_px: int = 0,
    offset_x_px: int = 0,
) -> np.ndarray:
    """Return pixels touching projected coarse-cell lattice boundaries."""
    h, w = int(shape[0]), int(shape[1])
    if int(scale) <= 1:
        return np.zeros((h, w), dtype=bool)
    y_lattice, x_lattice = _stage2_coarse_lattice_edge_masks(
        (h, w),
        int(scale),
        offset_y_px=int(offset_y_px),
        offset_x_px=int(offset_x_px),
    )
    mask = np.zeros((h, w), dtype=bool)
    if y_lattice.size:
        mask[:-1, :] |= y_lattice
        mask[1:, :] |= y_lattice
    if x_lattice.size:
        mask[:, :-1] |= x_lattice
        mask[:, 1:] |= x_lattice
    return mask

__all__ = (
    '_effective_stage1_coarsening_factor',
    '_stage1_lattice_offset_px',
    '_coarsened_shape',
    '_coarse_lattice_indices',
    '_downsample_rgb_image',
    '_downsample_flat_oklab_targets',
    '_project_zone_labels_to_fine',
    '_stage2_coarse_lattice_edge_masks',
    '_stage2_coarse_lattice_pixel_mask',
)
