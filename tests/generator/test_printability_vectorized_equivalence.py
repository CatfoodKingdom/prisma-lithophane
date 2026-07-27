"""Equivalence guards for the vectorized printability hot paths.

Each test compares the production implementation against an embedded
straight-line reference copy of the original per-component algorithm on
randomized inputs. Written green against the pre-vectorization code; the
vectorized replacements must keep them green bit-for-bit.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from scipy import ndimage as ndi

_GEN_DIR = Path(__file__).resolve().parent.parent.parent / "Prisma" / "generator"
if str(_GEN_DIR) not in sys.path:
    sys.path.insert(0, str(_GEN_DIR))
_PRISMA_DIR = _GEN_DIR.parent
if str(_PRISMA_DIR) not in sys.path:
    sys.path.insert(0, str(_PRISMA_DIR))

from pipeline.staged import printability_enforcement  # noqa: E402
from pipeline.staged.stage2 import objective as stage2_objective  # noqa: E402
from pipeline.staged.stage2 import printability as stage2_printability  # noqa: E402
from pipeline.staged.stage2 import refinement as stage2_refinement  # noqa: E402
from pipeline.staged.stage4 import printability as stage4_printability  # noqa: E402
from pipeline.staged_printability import (  # noqa: E402
    grade_blueprint_component,
    opening_width_loss,
    opening_width_loss_is_structural,
    opening_width_structure,
)

FOUR = ndi.generate_binary_structure(2, 1)

SETTINGS = SimpleNamespace(
    pitch_mm=0.2,
    minimum_extrusion_width_mm=0.4,
    minimum_line_length_mm=0.8,
    layer_height_mm=0.08,
)


# ── reference: original per-component algorithms (verbatim logic) ─────────────

def _ref_component_grade(component_indices, width_px, settings):
    ys = component_indices // int(width_px)
    xs = component_indices - ys * int(width_px)
    height_px = int(np.max(ys) - np.min(ys) + 1)
    component_width_px = int(np.max(xs) - np.min(xs) + 1)
    grade, reasons, _, _, _ = grade_blueprint_component(
        pixel_count=int(component_indices.size),
        height_px=height_px,
        width_px=component_width_px,
        settings=settings,
    )
    return str(grade), tuple(reasons)


def _boundary_contact_mask_for_borrowed_stack(
    *,
    component: np.ndarray,
    original_stack_map: np.ndarray,
    borrowed_stack_id: int,
) -> np.ndarray:
    """Scalar-oracle helper for borrowed-recipe boundary contact."""

    component_bool = np.asarray(component, dtype=bool)
    original = np.asarray(original_stack_map, dtype=np.int32)
    contact = np.zeros(component_bool.shape, dtype=bool)
    if component_bool.size == 0 or not np.any(component_bool):
        return contact
    borrowed = int(borrowed_stack_id)
    if component_bool.shape[0] > 1:
        contact[1:, :] |= component_bool[1:, :] & (original[:-1, :] == borrowed)
        contact[:-1, :] |= component_bool[:-1, :] & (original[1:, :] == borrowed)
    if component_bool.shape[1] > 1:
        contact[:, 1:] |= component_bool[:, 1:] & (original[:, :-1] == borrowed)
        contact[:, :-1] |= component_bool[:, :-1] & (original[:, 1:] == borrowed)
    return contact


def _ref_layer_component_failures(
    component_indices,
    shape,
    settings,
    width_structure,
    *,
    localize_opening_width_loss=True,
    structural_opening_width_loss=True,
):
    base_grade, base_reasons = _ref_component_grade(component_indices, shape[1], settings)
    if base_grade == "hard_fail":
        return ((component_indices.astype(np.int32), tuple(base_reasons)),)
    if not localize_opening_width_loss:
        return ()
    ys = component_indices // int(shape[1])
    xs = component_indices - ys * int(shape[1])
    y_min, x_min = int(np.min(ys)), int(np.min(xs))
    mask = np.zeros((int(np.max(ys)) - y_min + 1, int(np.max(xs)) - x_min + 1), dtype=bool)
    mask[ys - y_min, xs - x_min] = True
    loss = opening_width_loss(mask, structure=width_structure)
    if not np.any(loss):
        return ()
    if structural_opening_width_loss and not opening_width_loss_is_structural(mask, loss):
        return ()
    labels, count = ndi.label(loss, structure=FOUR)
    reasons = list(base_reasons)
    if "narrow_width" not in reasons:
        reasons.append("narrow_width")
    out = []
    for cid in range(1, count + 1):
        ly, lx = np.nonzero(labels == cid)
        if ly.size:
            out.append((((ly + y_min) * shape[1] + (lx + x_min)).astype(np.int32), tuple(reasons)))
    return tuple(out)


def _ref_layer_failures(
    layer_mask,
    shape,
    settings,
    width_structure,
    *,
    localize_opening_width_loss=True,
    structural_opening_width_loss=True,
):
    """Current component-at-a-time classification for one physical layer."""

    labels, count = ndi.label(layer_mask)
    flat_labels = labels.reshape(-1)
    failures = []
    accepted = 0
    for component_id in range(1, int(count) + 1):
        component_indices = np.flatnonzero(flat_labels == component_id).astype(
            np.int32,
            copy=False,
        )
        if component_indices.size == 0:
            continue
        component_failures = _ref_layer_component_failures(
            component_indices,
            shape,
            settings,
            width_structure,
            localize_opening_width_loss=localize_opening_width_loss,
            structural_opening_width_loss=structural_opening_width_loss,
        )
        if component_failures:
            failures.extend(component_failures)
        else:
            accepted += 1
    return failures, accepted


def _ref_gate(detail_height_mm, settings, base_top_mm=None):
    detail_height = np.asarray(detail_height_mm, dtype=np.float32)
    shape = detail_height.shape
    lh = max(float(settings.layer_height_mm), 1e-9)
    layers = np.maximum(np.rint(detail_height / np.float32(lh)).astype(np.int32), 0)
    layers[(detail_height > np.float32(1e-9)) & (layers < 1)] = 1
    base_layers = None
    if base_top_mm is not None:
        base_top = np.asarray(base_top_mm, dtype=np.float32)
        z0 = float(np.min(base_top))
        base_layers = np.maximum(np.rint((base_top - np.float32(z0)) / np.float32(lh)).astype(np.int32), 0)
    rejection = np.zeros(shape, dtype=np.uint8)
    counters = dict(
        suppressed_layer_pixels=0, suppressed_components=0, accepted_components=0,
        rejected_tiny_components=0, rejected_narrow_components=0, rejected_short_components=0,
    )
    max_layer = int(np.max(layers, initial=0)) if base_layers is None else int(np.max(base_layers + layers, initial=0))
    flat_rej = rejection.reshape(-1)
    flat_layers = layers.reshape(-1)
    ws = opening_width_structure(settings)
    if max_layer > 0:
        for _ in range(5):
            removed = 0
            for L in range(max_layer, 0, -1):
                mask = stage4_printability._stage4_absolute_layer_mask(
                    boundary_layers=layers, layer_index=L, ceiling_layers=base_layers
                )
                if not np.any(mask):
                    continue
                lab, n = ndi.label(mask)
                flat_lab = lab.reshape(-1)
                for cid in range(1, n + 1):
                    idx = np.flatnonzero(flat_lab == cid).astype(np.int32)
                    if idx.size == 0:
                        continue
                    fails = _ref_layer_component_failures(idx, shape, settings, ws)
                    if not fails:
                        counters["accepted_components"] += 1
                        continue
                    fb = None if base_layers is None else base_layers.reshape(-1)
                    for fi, reasons in fails:
                        if fi.size == 0:
                            continue
                        i64 = fi.astype(np.int64)
                        if base_layers is None:
                            limit = np.full(i64.shape, L - 1, dtype=np.int32)
                        else:
                            limit = np.maximum((L - fb[i64] - 1).astype(np.int32), 0)
                        flat_layers[i64] = np.minimum(flat_layers[i64], limit)
                        flat_rej[i64] |= np.uint8(printability_enforcement._stage2_printability_reason_bits(tuple(reasons)))
                        counters["suppressed_layer_pixels"] += int(fi.size)
                        counters["suppressed_components"] += 1
                        removed += int(fi.size)
                        for tag, key in (
                            ("tiny_component", "rejected_tiny_components"),
                            ("narrow_width", "rejected_narrow_components"),
                            ("short_length", "rejected_short_components"),
                        ):
                            if tag in reasons:
                                counters[key] += 1
            if removed <= 0:
                break
    return (layers.astype(np.float32) * np.float32(lh)).astype(np.float32), rejection, counters


def _ref_mutation(**kwargs):
    """Reference = original algorithm; compares the dense prelude + pair loops."""
    original = np.asarray(kwargs["fine_stack_id_map"], dtype=np.int32)
    targets = np.asarray(kwargs["targets"], dtype=np.float32)
    all_oklabs = kwargs["all_oklabs"]
    shape = original.shape
    current = stage2_objective._score_pixels_against_stack_ids(targets, original.reshape(-1), all_oklabs).reshape(shape)
    best_gain = np.zeros(shape, dtype=np.float32)
    best_stack = original.copy()
    candidate_mask = np.zeros(shape, dtype=bool)

    def consider(neighbor, valid):
        nonlocal candidate_mask
        cand = valid & (neighbor >= 0) & (original >= 0) & (neighbor != original)
        if not np.any(cand):
            return
        candidate_mask |= cand
        scores = stage2_objective._score_pixels_against_stack_ids(targets, neighbor.reshape(-1), all_oklabs).reshape(shape)
        gain = (current - scores).astype(np.float32)
        better = cand & (gain > best_gain + np.float32(1e-9))
        best_gain[better] = gain[better]
        best_stack[better] = neighbor[better]

    for axis, sign in ((0, 1), (0, -1), (1, 1), (1, -1)):
        n = original.copy()
        v = np.zeros(shape, dtype=bool)
        if axis == 0 and sign == 1 and shape[0] > 1:
            n[1:, :] = original[:-1, :]; v[1:, :] = True
        elif axis == 0 and sign == -1 and shape[0] > 1:
            n[:-1, :] = original[1:, :]; v[:-1, :] = True
        elif axis == 1 and sign == 1 and shape[1] > 1:
            n[:, 1:] = original[:, :-1]; v[:, 1:] = True
        elif axis == 1 and sign == -1 and shape[1] > 1:
            n[:, :-1] = original[:, 1:]; v[:, :-1] = True
        else:
            continue
        consider(n, v)

    current_de_mask = np.ones(shape, dtype=bool)
    pct = kwargs.get("current_de_percentile")
    if pct is not None and np.any(candidate_mask):
        thr = float(np.percentile(current[candidate_mask], min(100.0, max(0.0, float(pct)))))
        current_de_mask = candidate_mask & (current >= np.float32(thr))
    gain_threshold = np.float32(max(0.0, float(kwargs["min_gain"])))
    mcp = int(kwargs.get("min_component_pixels", 0))
    counters = dict.fromkeys(
        ("rejected_small_pixels", "rejected_small_components", "rejected_weak_pixels",
         "rejected_weak_components", "accepted_boundary_contact_pixels",
         "rejected_short_run_pixels", "rejected_short_run_components"), 0)
    positive = (best_gain > np.float32(0.0)) & current_de_mask
    accepted = np.zeros(shape, dtype=bool)
    component_count = 0
    mcp = max(1, mcp)
    if np.any(positive):
        pairs = np.unique(np.column_stack((original[positive], best_stack[positive])), axis=0)
        for o, b in pairs:
            pm = positive & (original == int(o)) & (best_stack == int(b))
            pl, pc = ndi.label(pm, structure=FOUR)
            for lid in range(1, pc + 1):
                comp = pl == lid
                size = int(np.count_nonzero(comp))
                if size <= 0:
                    continue
                contact = _boundary_contact_mask_for_borrowed_stack(
                    component=comp, original_stack_map=original, borrowed_stack_id=int(b))
                cpx = int(np.count_nonzero(contact))
                if cpx < mcp:
                    counters["rejected_short_run_pixels"] += size
                    counters["rejected_short_run_components"] += 1
                    continue
                if float(np.mean(best_gain[comp])) <= float(gain_threshold):
                    counters["rejected_weak_pixels"] += size
                    counters["rejected_weak_components"] += 1
                    continue
                accepted |= comp
                counters["accepted_boundary_contact_pixels"] += cpx
    if np.any(accepted):
        _l, component_count = ndi.label(accepted, structure=FOUR)
    mutated = original.copy()
    mutated[accepted] = best_stack[accepted]
    return mutated, accepted.astype(np.uint8), int(np.count_nonzero(candidate_mask)), component_count, counters


# ── input generators ─────────────────────────────────────────────────────────

def _random_heights(rng, shape, with_base):
    blob = ndi.gaussian_filter(rng.random(shape), 1.2) > 0.55
    levels = rng.integers(1, 5, size=shape).astype(np.float32)
    height = np.where(blob, levels * 0.08, 0.0).astype(np.float32)
    base = None
    if with_base:
        base = (np.cumsum(rng.integers(0, 2, size=shape), axis=1) * 0.08).astype(np.float32)
    return height, base


def _random_mutation_inputs(rng, shape=(30, 24), n_stacks=6, n_caps=5):
    coarse = rng.integers(0, n_stacks, size=(shape[0] // 3, shape[1] // 3))
    ids = np.kron(coarse, np.ones((3, 3), dtype=np.int64)).astype(np.int32)
    ids[rng.random(shape) < 0.04] = -1
    oklab = rng.random((n_stacks, n_caps, 3)).astype(np.float32)
    oklab[rng.random((n_stacks, n_caps)) < 0.15] = np.inf
    targets = rng.random((shape[0] * shape[1], 3)).astype(np.float32)
    return ids, targets, oklab


# ── tests ────────────────────────────────────────────────────────────────────

def _ref_color_layer_hard_fail(**kwargs):
    """Reference copy of the original per-component color hard-fail scan."""
    stack_ids, layer_table = stage2_printability._stack_color_layer_labels(
        unique_stack_dicts=kwargs["unique_stack_dicts"],
        palette_order=kwargs["palette_order"],
        layer_height_mm=float(kwargs["layer_height_mm"]),
    )
    settings = kwargs["settings"]
    shape = tuple(np.asarray(kwargs["fine_stack_id_map"]).shape)
    flat = np.asarray(kwargs["fine_stack_id_map"], dtype=np.int32).reshape(-1)
    hard = np.zeros(flat.shape[0], dtype=bool)
    comps: list = []
    if stack_ids.size == 0 or layer_table.shape[1] == 0:
        return hard.reshape(shape), comps
    row_by_pixel = np.full(flat.shape[0], -1, dtype=np.int32)
    for row, sid in enumerate(stack_ids.tolist()):
        row_by_pixel[flat == int(sid)] = int(row)
    valid = row_by_pixel >= 0
    if not np.any(valid):
        return hard.reshape(shape), comps
    ws = opening_width_structure(settings)
    for li in range(int(layer_table.shape[1])):
        layer_values = np.full(flat.shape[0], -1, dtype=np.int16)
        layer_values[valid] = layer_table[row_by_pixel[valid].astype(np.int64), li]
        for mid in np.unique(layer_values[layer_values >= 0]).tolist():
            mask = (layer_values == int(mid)).reshape(shape)
            lab, n = ndi.label(mask, structure=FOUR)
            fl = lab.reshape(-1)
            for cid in range(1, n + 1):
                idx = np.flatnonzero(fl == cid).astype(np.int32)
                if idx.size == 0:
                    continue
                grade, reasons, _h, _w = printability_enforcement._stage2_component_physical_grade(
                    component_indices=idx, width_px=int(shape[1]), settings=settings)
                if str(grade) == "hard_fail":
                    hard[idx.astype(np.int64)] = True
                    comps.append((idx, tuple(reasons)))
                elif kwargs.get("localize_opening_width_loss"):
                    loss = printability_enforcement._opening_width_loss_components_for_indices(
                        component_indices=idx, shape=shape, width_structure=ws,
                        structural_only=bool(kwargs.get("structural_opening_width_loss")))
                    if not loss:
                        continue
                    rs = list(reasons)
                    if "narrow_width" not in rs:
                        rs.append("narrow_width")
                    for l_idx in loss:
                        hard[l_idx.astype(np.int64)] = True
                        comps.append((l_idx.astype(np.int32), tuple(rs)))
    return hard.reshape(shape), comps


def _ref_mandatory_cap_hard_fail(**kwargs):
    """Reference copy of the original mandatory-cap hard-fail scan."""
    shape = tuple(np.asarray(kwargs["fine_stack_id_map"]).shape)
    flat = np.asarray(kwargs["fine_stack_id_map"], dtype=np.int32).reshape(-1)
    settings = kwargs["settings"]
    uds = kwargs["unique_stack_dicts"]
    lh = max(float(kwargs["layer_height_mm"]), 1e-9)
    cap_floor = int(np.ceil(max(float(kwargs["minimum_cap_height_mm"]), 0.0) / lh - 1e-9))
    if cap_floor <= 0 or flat.size == 0:
        return []
    stack_ids = np.array(sorted(int(s) for s in uds), dtype=np.int32)
    if stack_ids.size == 0:
        return []
    totals = np.asarray(
        [max(0, int(np.rint(np.float32(stage2_printability._stack_total_thickness_mm(uds[int(s)])) / np.float32(lh))))
         for s in stack_ids.tolist()], dtype=np.int32)
    row_by_pixel = np.full(flat.shape[0], -1, dtype=np.int32)
    for row, sid in enumerate(stack_ids.tolist()):
        row_by_pixel[flat == int(sid)] = int(row)
    valid = row_by_pixel >= 0
    if not np.any(valid):
        return []
    color_layers = np.zeros(flat.shape[0], dtype=np.int32)
    color_layers[valid] = totals[row_by_pixel[valid].astype(np.int64)]
    z0 = int(np.min(color_layers[valid]))
    base = color_layers - np.int32(z0)
    max_layer = int(np.max(base[valid] + np.int32(cap_floor), initial=0))
    ws = opening_width_structure(settings)
    comps: list = []
    seen: set = set()
    for ln in range(1, max_layer + 1):
        mask = (valid & (base < ln) & ((base + np.int32(cap_floor)) >= ln)).reshape(shape)
        if not np.any(mask):
            continue
        lab, n = ndi.label(mask, structure=FOUR)
        fl = lab.reshape(-1)
        for cid in range(1, n + 1):
            idx = np.flatnonzero(fl == cid).astype(np.int32)
            if idx.size == 0:
                continue
            grade, reasons, _h, _w = printability_enforcement._stage2_component_physical_grade(
                component_indices=idx, width_px=int(shape[1]), settings=settings)
            if str(grade) == "hard_fail":
                key = np.sort(idx).tobytes()
                if key in seen:
                    continue
                seen.add(key)
                comps.append((idx, tuple(reasons)))
                continue
            if not kwargs.get("localize_opening_width_loss"):
                continue
            loss = printability_enforcement._opening_width_loss_components_for_indices(
                component_indices=idx, shape=shape, width_structure=ws,
                structural_only=bool(kwargs.get("structural_opening_width_loss")))
            if not loss:
                continue
            rs = list(reasons)
            if "narrow_width" not in rs:
                rs.append("narrow_width")
            for l_idx in loss:
                key = np.sort(l_idx.astype(np.int32)).tobytes()
                if key in seen:
                    continue
                seen.add(key)
                comps.append((l_idx.astype(np.int32), tuple(rs)))
    return comps


def _random_stack_world(rng, shape=(34, 26), n_stacks=7):
    palette = ("fil-a", "fil-b", "fil-c")
    uds = {}
    for sid in range(n_stacks):
        stack = {}
        for fid in palette:
            if rng.random() < 0.6:
                stack[fid] = round(int(rng.integers(1, 5)) * 0.08, 6)
        uds[sid] = stack
    coarse = rng.integers(0, n_stacks, size=(shape[0] // 2, shape[1] // 2))
    ids = np.kron(coarse, np.ones((2, 2), dtype=np.int64)).astype(np.int32)
    ids[rng.random(shape) < 0.05] = -1
    return ids, uds, palette


def _assert_components_equal(got, ref, label):
    assert len(got) == len(ref), f"{label}: {len(got)} vs {len(ref)} components"
    for i, ((gi, gr), (ri, rr)) in enumerate(zip(got, ref)):
        np.testing.assert_array_equal(gi, ri, err_msg=f"{label} comp {i} indices")
        assert tuple(gr) == tuple(rr), f"{label} comp {i} reasons {gr} vs {rr}"


def test_color_layer_hard_fail_matches_reference() -> None:
    rng = np.random.default_rng(31)
    for case in range(5):
        ids, uds, palette = _random_stack_world(rng)
        for localize, structural in ((True, True), (True, False), (False, False)):
            kwargs = dict(
                fine_stack_id_map=ids, unique_stack_dicts=uds, palette_order=palette,
                layer_height_mm=0.08, settings=SETTINGS,
                localize_opening_width_loss=localize,
                structural_opening_width_loss=structural,
            )
            ref_map, ref_comps = _ref_color_layer_hard_fail(**kwargs)
            got_map, got_comps = stage2_printability._color_layer_hard_fail_components_from_stack_ids(**kwargs)
            np.testing.assert_array_equal(got_map, ref_map, err_msg=f"case {case} map")
            _assert_components_equal(got_comps, ref_comps, f"case {case} loc={localize}")


def test_mandatory_cap_hard_fail_matches_reference() -> None:
    rng = np.random.default_rng(47)
    for case in range(5):
        ids, uds, palette = _random_stack_world(rng)
        for localize, structural in ((True, True), (False, False)):
            kwargs = dict(
                fine_stack_id_map=ids, unique_stack_dicts=uds,
                layer_height_mm=0.08, minimum_cap_height_mm=0.16, settings=SETTINGS,
                localize_opening_width_loss=localize,
                structural_opening_width_loss=structural,
            )
            ref_comps = _ref_mandatory_cap_hard_fail(**kwargs)
            got_comps = stage2_printability._mandatory_cap_hard_fail_components_from_stack_ids(**kwargs)
            _assert_components_equal(got_comps, ref_comps, f"case {case} loc={localize}")


def test_stage4_layer_failure_batch_matches_component_reference() -> None:
    rng = np.random.default_rng(83)
    shape = (41, 37)
    masks = []
    for threshold in (0.35, 0.50, 0.65):
        for _case in range(5):
            field = ndi.gaussian_filter(rng.random(shape), sigma=1.1)
            masks.append(field > threshold)

    solid = np.zeros(shape, dtype=bool)
    solid[4:35, 5:31] = True
    masks.append(solid)

    islands = np.zeros(shape, dtype=bool)
    islands[2, 2] = True
    islands[8:12, 7:18] = True
    islands[25:39, 28:31] = True
    masks.append(islands)

    dumbbell = np.zeros(shape, dtype=bool)
    dumbbell[8:17, 4:13] = True
    dumbbell[8:17, 23:32] = True
    dumbbell[12, 13:23] = True
    masks.append(dumbbell)

    width_structure = opening_width_structure(SETTINGS)
    modes = ((True, True), (True, False), (False, False))
    for case, layer_mask in enumerate(masks):
        for localize, structural in modes:
            ref_failures, ref_accepted = _ref_layer_failures(
                layer_mask,
                shape,
                SETTINGS,
                width_structure,
                localize_opening_width_loss=localize,
                structural_opening_width_loss=structural,
            )
            got_failures, got_accepted = printability_enforcement._stage4_layer_failures_vectorized(
                layer_mask=layer_mask,
                shape=shape,
                settings=SETTINGS,
                width_structure=width_structure,
                localize_opening_width_loss=localize,
                structural_opening_width_loss=structural,
            )
            assert got_accepted == ref_accepted, (
                f"case {case} accepted components, "
                f"localize={localize} structural={structural}"
            )
            _assert_components_equal(
                got_failures,
                ref_failures,
                f"case {case} localize={localize} structural={structural}",
            )


def test_stage4_boundary_gate_clean_layers_skip_component_fallback(monkeypatch) -> None:
    def unexpected_component_scan(**_kwargs):
        raise AssertionError("clean boundary layers should use the batch classifier")

    monkeypatch.setattr(stage4_printability, "_stage4_layer_component_failures", unexpected_component_scan)
    height = np.full((12, 10), 0.16, dtype=np.float32)

    result = stage4_printability._apply_stage4_boundary_cap_printability_gate(
        boundary_cap_height_mm=height,
        settings=SETTINGS,
        apply_changes=True,
    )

    np.testing.assert_array_equal(result.boundary_cap_height_mm, height)
    assert result.summary.flagged_components == 0
    assert result.summary.accepted_components == 2


def test_stage4_boundary_gate_failure_uses_component_repair_fallback(monkeypatch) -> None:
    original = stage4_printability._stage4_layer_component_failures
    calls = 0

    def recording_component_scan(**kwargs):
        nonlocal calls
        calls += 1
        return original(**kwargs)

    monkeypatch.setattr(stage4_printability, "_stage4_layer_component_failures", recording_component_scan)
    height = np.zeros((12, 10), dtype=np.float32)
    height[5, 5] = np.float32(0.08)

    result = stage4_printability._apply_stage4_boundary_cap_printability_gate(
        boundary_cap_height_mm=height,
        settings=SETTINGS,
        apply_changes=False,
    )

    np.testing.assert_array_equal(result.boundary_cap_height_mm, height)
    assert calls > 0
    assert result.summary.flagged_components > 0


def test_stage4_boundary_gate_skips_recursive_cleanup_when_unchanged(monkeypatch) -> None:
    original = stage4_printability._apply_stage4_boundary_cap_printability_gate
    recursive_calls = 0

    def recording_recursive_call(**kwargs):
        nonlocal recursive_calls
        recursive_calls += 1
        return original(**kwargs)

    monkeypatch.setattr(
        stage4_printability,
        "_apply_stage4_boundary_cap_printability_gate",
        recording_recursive_call,
    )
    height = np.full((12, 10), 0.16, dtype=np.float32)

    result = original(
        boundary_cap_height_mm=height,
        settings=SETTINGS,
        apply_changes=True,
        repair_with_growth=True,
    )

    np.testing.assert_array_equal(result.boundary_cap_height_mm, height)
    assert recursive_calls == 0


def test_stage4_boundary_gate_keeps_recursive_cleanup_after_mutation(monkeypatch) -> None:
    original = stage4_printability._apply_stage4_boundary_cap_printability_gate
    recursive_calls = 0

    def recording_recursive_call(**kwargs):
        nonlocal recursive_calls
        recursive_calls += 1
        return original(**kwargs)

    monkeypatch.setattr(
        stage4_printability,
        "_apply_stage4_boundary_cap_printability_gate",
        recording_recursive_call,
    )
    height = np.zeros((12, 10), dtype=np.float32)
    height[5, 5] = np.float32(0.08)
    max_height = np.full_like(height, np.float32(0.16))

    result = original(
        boundary_cap_height_mm=height,
        settings=SETTINGS,
        max_boundary_cap_height_mm=max_height,
        apply_changes=True,
        repair_with_growth=True,
    )

    assert result.summary.grown_layer_pixels > 0
    assert recursive_calls > 0


def test_stage4_boundary_gate_keeps_recursive_cleanup_after_unchanged_failure(
    monkeypatch,
) -> None:
    original = stage4_printability._apply_stage4_boundary_cap_printability_gate
    recursive_calls = 0

    def recording_recursive_call(**kwargs):
        nonlocal recursive_calls
        recursive_calls += 1
        return original(**kwargs)

    monkeypatch.setattr(
        stage4_printability,
        "_apply_stage4_boundary_cap_printability_gate",
        recording_recursive_call,
    )
    height = np.zeros((12, 10), dtype=np.float32)
    height[5, 5] = np.float32(0.08)

    result = original(
        boundary_cap_height_mm=height,
        settings=SETTINGS,
        max_boundary_cap_height_mm=height,
        minimum_boundary_cap_height_map_mm=height,
        apply_changes=True,
        repair_with_growth=True,
    )

    assert result.summary.flagged_components > 0
    assert result.summary.grown_layer_pixels == 0
    assert result.summary.suppressed_optional_layer_pixels == 0
    assert recursive_calls > 0


def test_stage4_detail_gate_matches_reference() -> None:
    rng = np.random.default_rng(11)
    for case in range(6):
        height, base = _random_heights(rng, (36, 28), with_base=case % 2 == 1)
        ref_h, ref_r, ref_c = _ref_gate(height.copy(), SETTINGS, None if base is None else base.copy())
        got = stage4_printability._apply_stage4_detail_printability_gate(
            detail_height_mm=height.copy(),
            settings=SETTINGS,
            base_top_mm=None if base is None else base.copy(),
        )
        np.testing.assert_array_equal(got.detail_height_mm, ref_h, err_msg=f"case {case} heights")
        np.testing.assert_array_equal(got.rejection_map, ref_r, err_msg=f"case {case} rejection")
        for key, value in ref_c.items():
            assert int(getattr(got.summary, key)) == int(value), f"case {case} {key}"


def test_boundary_mutation_matches_reference() -> None:
    rng = np.random.default_rng(23)
    modes = [
        dict(min_component_pixels=0, current_de_percentile=None),
        dict(min_component_pixels=3, current_de_percentile=60.0),
        dict(min_component_pixels=4, current_de_percentile=40.0),
    ]
    for case, mode in enumerate(modes):
        ids, targets, oklab = _random_mutation_inputs(rng)
        kwargs = dict(fine_stack_id_map=ids, targets=targets, all_oklabs=oklab, min_gain=0.01, **mode)
        ref_map, ref_mut, ref_cand, ref_components, ref_counters = _ref_mutation(**kwargs)
        got = stage2_refinement._apply_stage2_boundary_recipe_mutation(
            **{k: (v.copy() if hasattr(v, "copy") else v) for k, v in kwargs.items()}
        )
        np.testing.assert_array_equal(got.fine_stack_id_map, ref_map, err_msg=f"case {case} map")
        np.testing.assert_array_equal(got.mutation_map, ref_mut, err_msg=f"case {case} mutation_map")
        assert got.candidate_pixels == ref_cand, f"case {case} candidates"
        assert got.accepted_components == ref_components, f"case {case} components"
        for key, value in ref_counters.items():
            assert int(getattr(got, key)) == int(value), f"case {case} {key}"
