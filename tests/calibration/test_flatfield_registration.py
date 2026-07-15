import numpy as np

from processing.extraction import (
    apply_flatfield,
    register_flatfield,
)


def _synthetic_lightbox_scene(edge_value: float = 0.30, center_value: float = 0.95):
    height, width = 120, 180
    lb_x, lb_y, lb_w, lb_h = 20, 18, 140, 84
    center_x0, center_x1 = lb_x + 38, lb_x + lb_w - 38
    center_y0, center_y1 = lb_y + 24, lb_y + lb_h - 24

    flatfield_linear = np.zeros((height, width, 3), dtype=np.float32)
    flatfield_linear[lb_y:lb_y + lb_h, lb_x:lb_x + lb_w] = edge_value
    flatfield_linear[center_y0:center_y1, center_x0:center_x1] = center_value

    flatfield_visual = np.zeros((height, width, 3), dtype=np.uint8)
    flatfield_visual[lb_y:lb_y + lb_h, lb_x:lb_x + lb_w] = 230

    strip_visual = flatfield_visual.copy()
    sample_linear = flatfield_linear.copy()
    sample_linear[lb_y:lb_y + lb_h, lb_x:lb_x + lb_w] *= 0.90

    edge_patch = np.s_[lb_y + 6:lb_y + 18, lb_x + 6:lb_x + 22]
    center_pixel = (lb_y + lb_h // 2, lb_x + lb_w // 2)
    return (
        flatfield_linear,
        flatfield_visual,
        strip_visual,
        sample_linear,
        edge_patch,
        center_pixel,
    )


def test_flatfield_registration_uses_visual_blank_boundary_for_edge_transmission():
    (
        flatfield_linear,
        flatfield_visual,
        strip_visual,
        sample_linear,
        edge_patch,
        center_pixel,
    ) = _synthetic_lightbox_scene()

    registered = register_flatfield(
        flatfield_linear,
        strip_visual,
        flatfield_visual_bgr=flatfield_visual,
    )
    transmission = apply_flatfield(sample_linear, registered)

    edge_median = np.median(transmission[edge_patch].reshape(-1, 3), axis=0)
    np.testing.assert_allclose(edge_median, [0.90, 0.90, 0.90], atol=0.02)
    np.testing.assert_allclose(
        registered[edge_patch].reshape(-1, 3).mean(axis=0),
        [0.30, 0.30, 0.30],
        atol=0.02,
    )
    np.testing.assert_allclose(registered[center_pixel], [0.95, 0.95, 0.95], atol=0.02)
