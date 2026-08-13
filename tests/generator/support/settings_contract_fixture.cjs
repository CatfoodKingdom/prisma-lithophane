"use strict";

const defaults = {
  base_filament: "bambu-tough-white",
  cap_filament: "__same__",
  layer_height: 0.08,
  d_wb: 0.20,
  min_cap_layers: 2,
  t_max: 3.0,
  k_max: 3,
  de_threshold: 0.01,
  boundary_cap_smoothing_radius_mm: 1.0,
  appearance_model_provider: "photo_stack_bundle",
  photo_stack_bundle_path: null,
  gamut_mode: "hull",
  gamut_white_rescale: false,
  model_domain_ingress_lut_path: "camera_transform",
  chroma_weight: 1.0,
  luminance_mode: "standard",
  luminance_base_shading_limit_fraction: 0.75,
  luminance_detail_authoring_printability: "off",
  solve_pitch_extrusion_width_multiplier: 1,
  detail_cap_max_layers: 5,
  detail_cap_smoothing_enabled: true,
  detail_cap_smoothing_exact_speckle_max_px: 1,
  detail_cap_smoothing_cumulative_component_max_px: 2,
  detail_cap_smoothing_cumulative_hole_max_px: 2,
  color_region_target_mm: 0.60,
  cell_mode: "felzenszwalb",
  stage1_coarsening_factor: 1,
  neutral_field_protection_enabled: false,
  neutral_field_protection_cutoff: 0.020,
  stage2_fine_override_enabled: true,
  stage2_boundary_mutation_enabled: true,
  stage2_boundary_mutation_min_gain: 0.01,
  stage2_boundary_mutation_max_passes: 1,
  cap_mode: "appearance_bounded_smooth",
  boundary_cap_de_budget: 0.004,
  source_resample_kernel: "lanczos",
  preprocessing_params: {},
};

const profile_keys = Object.keys(defaults);
const metadata = {
  min_cap_layers: { kind: "int", minimum: 1 },
  solve_pitch_extrusion_width_multiplier: { kind: "int", minimum: 1 },
  k_max: { kind: "int", minimum: 1, maximum: 7 },
  chroma_weight: { kind: "float", minimum: 0.125, maximum: 8 },
  stage1_coarsening_factor: { kind: "int", minimum: 1, maximum: 4 },
  luminance_base_shading_limit_fraction: { kind: "float", minimum: 0, maximum: 1 },
  detail_cap_max_layers: { kind: "int", minimum: 0 },
  neutral_field_protection_cutoff: {
    kind: "float",
    minimum: 0,
    maximum: 1,
    presets: [
      { id: "narrow", value: 0.010 },
      { id: "standard", value: 0.020 },
      { id: "broad", value: 0.035 },
    ],
  },
  stage2_boundary_mutation_max_passes: { kind: "int", minimum: 1, maximum: 16 },
};
const TEST_SETTINGS_CONTRACT = {
  schema_version: 4,
  profile_keys,
  settings: profile_keys.map((key) => ({ key, default: defaults[key], ...(metadata[key] || {}) })),
};

module.exports = { TEST_SETTINGS_CONTRACT };
