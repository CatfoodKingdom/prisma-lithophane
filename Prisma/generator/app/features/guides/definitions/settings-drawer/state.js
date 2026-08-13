export const SETTINGS_DRAWER_MODULE_IDS = Object.freeze([
  "a1_bilateral_denoise",
  "b1_printscale_bilateral",
  "b3_tv_flatten",
  "c1_achievable_tonemap",
  "c2_soft_gamut_compress",
]);

export const SETTINGS_DRAWER_ALL_MODULES_OFF = Object.freeze(Object.fromEntries(
  SETTINGS_DRAWER_MODULE_IDS.map(id => [id, false]),
));

export const SETTINGS_DRAWER_BASELINE = Object.freeze({
  luminance_mode: "standard",
  solve_pitch_extrusion_width_multiplier: 1,
  layer_height: 0.08,
  d_wb: 0.15,
  t_max: 2.95,
  min_cap_layers: 2,
  neutral_field_protection_enabled: false,
  neutral_field_protection_cutoff: 0.020,
  stage2_boundary_mutation_enabled: true,
  cap_mode: "appearance_bounded_smooth",
  preprocessing_params: Object.freeze({}),
});

export const SETTINGS_DRAWER_MODULE_PRESETS = Object.freeze({
  a1_bilateral_denoise: Object.freeze({
    radius_px: 3,
    sigma_range: 0.04,
    sigma_spatial: 0.5,
  }),
  b1_printscale_bilateral: Object.freeze({
    feature_scale_multiplier: 1.0,
    sigma_range: 0.05,
    passes: 1,
  }),
  b3_tv_flatten: Object.freeze({
    tv_weight: 0.04,
    weight_autoscale: true,
    channel_axis: "oklab_L_only",
    n_iter_max: 20,
  }),
  c1_achievable_tonemap: Object.freeze({
    strength: 0.25,
    shadow_percentile: 0.25,
    highlight_percentile: 99.5,
    midtone_contrast: 0.75,
  }),
  c2_soft_gamut_compress: Object.freeze({
    knee_start_ratio: 0.85,
    knee_softness: 0.50,
  }),
});

export function settingsDrawerModuleMap(enabledId = null) {
  return Object.freeze(Object.fromEntries(
    SETTINGS_DRAWER_MODULE_IDS.map(id => [id, id === enabledId]),
  ));
}
