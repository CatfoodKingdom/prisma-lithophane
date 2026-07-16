// app.js — Prisma Generator Web Application
//
// Vanilla JS frontend matching unified_calibration design language.
// Five-tab workflow: Image → Palette → Settings → Solve → Export
//
// Data flow: static STATIC_FILAMENTS (data.js) → API enrichment (api.js)
// All heavy computation on server; frontend is display + controls only.

function assertPolledJobIdentity(status, expectedJobId) {
  const expected = String(expectedJobId || "");
  const actual = String(status?.job_id || "");
  if (!expected || actual !== expected) {
    const error = new Error(
      actual
        ? `Polled job identity mismatch: expected ${expected || "(missing)"}, received ${actual}.`
        : `Polled job response omitted the expected job id ${expected || "(missing)"}.`,
    );
    error.name = "JobPollingIdentityError";
    error.expectedJobId = expected;
    error.actualJobId = actual;
    throw error;
  }
}

async function pollJobUntilTerminal({
  jobId,
  fetchStatus,
  isTerminal,
  onStatus = () => {},
  shouldContinue = () => true,
  onTransientError = () => {},
  onRecovered = () => {},
  intervalMs = 500,
  maxRetryDelayMs = 4000,
  wait = (delayMs) => new Promise((resolve) => setTimeout(resolve, delayMs)),
}) {
  const expectedJobId = String(jobId || "");
  if (!expectedJobId) throw new Error("Cannot poll a job without a job id.");
  let consecutiveFailures = 0;
  let sequence = 0;

  while (shouldContinue()) {
    let status;
    try {
      status = await fetchStatus(expectedJobId);
    } catch (error) {
      if (!shouldContinue()) return null;
      consecutiveFailures += 1;
      const retryDelayMs = Math.min(
        maxRetryDelayMs,
        intervalMs * (2 ** Math.min(consecutiveFailures - 1, 3)),
      );
      onTransientError(error, { consecutiveFailures, retryDelayMs });
      await wait(retryDelayMs);
      continue;
    }

    if (!shouldContinue()) return null;
    assertPolledJobIdentity(status, expectedJobId);
    sequence += 1;
    if (consecutiveFailures > 0) {
      onRecovered({ consecutiveFailures, sequence });
      consecutiveFailures = 0;
    }
    await onStatus(status, { sequence });
    if (isTerminal(status)) return status;
    await wait(intervalMs);
  }
  return null;
}


// ── State ────────────────────────────────────────────────────────────────────

let currentTab = "image";
let settingsDrawerOpen = false;
const SETTINGS_ADVANCED_VISIBLE_STORAGE_KEY = "prisma_settings_advanced_visible";
const COLOR_CAP_MODE_STORAGE_KEY = "prisma_color_cap_mode";
const LEGACY_ENABLED_FILAMENTS_STORAGE_KEY = "prisma_enabled_filaments";
const ENABLED_FILAMENTS_STORAGE_KEY = "prisma_enabled_filaments_by_library";
const ENABLED_FILAMENTS_STORAGE_VERSION = 1;
let settingsAdvancedVisible = loadSettingsAdvancedVisible();
let solveRunHoverTimer = null;
let solveRunHoverCloseTimer = null;
let solveRunHoverPendingRunId = null;
let solveRunHoverPreviewEl = null;
let solveRunHoverRunId = null;
let solveRunSettingsPanelEl = null;
let solveRunSettingsPanelRunId = null;
let solveRunSettingsPanelContext = null;
let solveRunSettingsAdvancedVisible = false;
let apiConnected = false;
let modelLibraryAutoOpened = false;
let modelLibraryManager = {
  status: null,
  selectedKey: null,
  loading: false,
  busy: false,
  restarting: false,
  message: "",
  messageKind: "",
  error: "",
};

// Filament data
let allFilaments = [...STATIC_FILAMENTS];
const DEFAULT_BASE_FILAMENT = "bambu-tough-white";

function getBaseFilament() {
  const sel = $("#cfgBaseFilament");
  return sel ? sel.value : DEFAULT_BASE_FILAMENT;
}

/** Set of filament IDs used as the shared white base/cap support. */
function getBaseCapIds() {
  const s = new Set();
  s.add(getBaseFilament());
  return s;
}

/** Number of AMS slots consumed by the shared white base/cap filament. */
function getBaseCapSlots() {
  return 1;
}

function xIconSvg(className = "icon-x") {
  return `<svg class="${className}" viewBox="0 0 12 12" aria-hidden="true" focusable="false"><path d="M2 2l8 8M10 2L2 10"></path></svg>`;
}

function panelResizeIconSvg(expanded) {
  return expanded
    ? `
      <svg class="panel-resize-icon" viewBox="0 0 14 14" aria-hidden="true" focusable="false">
        <path d="M3 5.25H8.75V11H3Z"></path>
        <path d="M5.25 3H11V8.75H9.25"></path>
      </svg>
    `
    : `
      <svg class="panel-resize-icon" viewBox="0 0 14 14" aria-hidden="true" focusable="false">
        <path d="M3 3H11V11H3Z"></path>
      </svg>
    `;
}

// Library management — which filaments are enabled for palette use
let enabledFilaments = new Set(
  STATIC_FILAMENTS.filter(isGenerationEligibleFilament).map(filament => filament.filament_id),
);
let enabledFilamentRuntimeLibraryId = null;
let enabledFilamentPersistenceReady = false;

// Image state
let availableImages = [];
let selectedImage = null; // { filename, width, height, size_kb }

// Frame-and-pan state
let frameState = {
  arMode: "specified",       // "specified" | "ratio" | "image" | "3:2" | "4:3" | "5:4" | "1:1"
  customRatio: { x: 1, y: 1 },
  widthMm: 100,
  heightMm: 100,
  scale: 100.0,              // crop zoom percentage (100-1000)
  rotation: 0,               // degrees (±180)
  panX: 0,                   // normalized offset (-1..1), 0 = centered
  panY: 0,
  flipH: false,              // horizontal mirror
  flipV: false,              // vertical mirror
};
let lastTouchedDim = "width";
let widthLocked = false;
let heightLocked = false;
let panDragState = null;
let frameDragState = null;
let frameEditorTab = "size";   // "size" | "image"
let libraryPaneState = "contracted"; // "contracted" | "expanded"
let imageDirection = "landscape"; // "landscape" | "portrait"

// Image adjustment state
let imageAdjust = {
  mode: "color",        // "bw" | "color"
  exposure: 0,
  contrast: 0,
  highlight: 0,
  shadow: 0,
  tint_hue: 0,
  tint_strength: 0,
  saturation: 0,
  temperature: 0,
};

// Palette state — Auto-Suggest / Manual Builder / Deck
let creationMode = "auto";        // "auto" | "manual"
let candidateSelection = new Set(); // filament IDs toggled as candidates in auto mode
let candidateInitialized = false;   // true after first render auto-selects all
let manualSlots = [];             // ordered filament IDs placed in manual AMS diagram
// Legacy compat (kept for deck/suggest flows)
let composerPalette = [];         // used by mintPaletteToDeck
let deck = [];                    // persistent palette deck (left rail); the ONLY deck solved from
let stagingDeck = [];             // transient staging pad (creation tab); suggestions/manual builds land here
let suggestCapacityNote = "";
let activeDeckId = null;          // id of the active deck card in `deck` (used for solve)
// Solve run history (session-only)
let solveRuns = [];              // solve run objects
let solveRunCounter = 0;         // auto-incrementing label counter
let selectedRunIds = new Set();  // which runs are visible in the comparison grid
let solveShowSourceImage = true; // show source column in Solve result grids
let solveColorRegionsView = "color_ceiling"; // "color_ceiling" | "recipe_regions" — sub-tab within the Color Regions tab
let solveColorRegionsViewWasExplicitlySelected = false;
let solveWhiteCapView = "cap_map"; // selected sub-view under the White Cap solve tab (height-only)
let solveContoursEnabled = true; // draw the boundary overlay appropriate to the current contour-capable view
let solveAdvancedViewsOpen = false; // view bar: inline-reveal of the demoted (advanced) views
let solveCapDiffMode = "changed";        // shared diff render mode (cap + filament): changed | added | removed | signed
let solveFilamentDiffId = "";            // currently-selected filament id for Color Diff
let savedRunRowsCache = [];       // rows currently shown in the Saved Runs modal
let selectedSavedRunKey = null;   // `${tier}:${save_id}` for the selected modal row
let savedRunsModalMode = "run";  // "run" for whole-run load, "settings" for settings-only selection
let savedRunDeleteConfirmPending = false;
let savedRunDeleteConfirmTimer = null;
let solveHistoryClearConfirmPending = false;
let solveHistoryClearConfirmTimer = null;
let solveRunDeleteArmedId = null;
let solveRunDeleteConfirmTimer = null;
let nextDeckNum = 1;              // auto-numbering for unnamed palettes
let savedPalettesData = null;     // loaded from server
let railDeckHoverTimer = null;
let railDeckHoverCloseTimer = null;
let railDeckHoverPreviewEl = null;
let railDeckHoverPreviewCardId = null;
let railDeckHoverPendingCardId = null;

// Convenience: activePalette getter for backward compat (solve, config sync, rail)
function getActivePalette() {
  const card = deck.find(d => d.id === activeDeckId);
  return card ? card.filament_ids : [];
}

// Printer config
let printerConfigOriginTab = null;    // tab that was active when config opened
let printerConfigEditingId = null;    // printer currently selected inside the config modal
let printerDeleteConfirmPending = false;
let printerDeleteConfirmTimer = null;
let printerConfig = {
  name: "Bambu X1C",
  max_x_mm: 256,
  max_y_mm: 256,
  ams_units: 1,
  slots_per_unit: 4,
  ams_slots: 4,
  white_slots: 1,
};
let printersData = null;   // full printers.json content (loaded from server)
let activeNozzle = null;   // resolved nozzle profile for active printer
let settingsProfiles = [];        // full settings profile records loaded from server
let temporarySettingsProfile = null; // one session-only profile reconstructed from a solved run
let loadedProfileRef = null;      // { id, kind, name } for the stored profile backing the current draft
let loadedProfileSnapshot = null; // { settings, modules } snapshot used for modified detection
let userDefaultProfileId = null;  // profile id loaded automatically on startup
const MODULE_POSTURE = {};
const MODULE_DISPLAY = {
  a1_bilateral_denoise: {
    label: "Noise Reduction",
    tooltip: "Softens small image noise before solving so color regions are less fragmented while preserving the overall look of the source image.",
  },
  b1_printscale_bilateral: {
    label: "Print-Scale Smoothing",
    tooltip: "Smooths image features at the scale the printer can reproduce, producing simpler and larger color regions.",
  },
  b3_tv_flatten: {
    label: "Flat-Area Smoothing",
    tooltip: "Flattens gradual texture into broader smooth areas before solving, creating a more painted or graphic look at stronger settings.",
  },
  c1_achievable_tonemap: {
    label: "Palette Tone Fit",
    tooltip: "Adjusts the source tone range toward what the selected palette can reproduce.",
  },
  c2_soft_gamut_compress: {
    label: "Palette Saturation Fit",
    tooltip: "Softly reduces colors the selected palette cannot reproduce while preserving hue.",
  },
};

const PREPROCESSING_PRESET_UI = {
  a1_bilateral_denoise: {
    controlLabel: "Noise reduction",
    defaultPreset: "medium",
    paramLabels: {
      radius_px: "Filter radius",
      sigma_range: "Color similarity",
      sigma_spatial: "Spatial reach",
    },
    paramTooltips: {
      radius_px: "Pixel radius of the local bilateral filter window.",
      sigma_range: "Higher values allow smoothing across larger color differences.",
      sigma_spatial: "Higher values allow smoothing influence to extend farther inside the filter radius.",
    },
    presets: [
      { key: "off", label: "Off", enabled: false },
      { key: "light", label: "Light", values: { radius_px: 3, sigma_range: 0.01, sigma_spatial: 0.5 } },
      { key: "medium", label: "Medium", values: { radius_px: 3, sigma_range: 0.04, sigma_spatial: 0.5 } },
      { key: "strong", label: "Strong", values: { radius_px: 8, sigma_range: 0.04, sigma_spatial: 2.0 } },
      { key: "very_strong", label: "Very Strong", values: { radius_px: 8, sigma_range: 0.15, sigma_spatial: 2.0 } },
      { key: "custom", label: "Custom", custom: true },
    ],
  },
  b1_printscale_bilateral: {
    controlLabel: "Print-scale smoothing",
    defaultPreset: "medium",
    paramLabels: {
      feature_scale_multiplier: "Feature scale",
      sigma_range: "Color similarity",
      passes: "Passes",
    },
    paramTooltips: {
      feature_scale_multiplier: "Multiplier against the configured printable feature width.",
      sigma_range: "Higher values smooth across larger color differences.",
      passes: "Repeating the filter increases flattening and region simplification.",
    },
    presets: [
      { key: "off", label: "Off", enabled: false },
      { key: "light", label: "Light", values: { feature_scale_multiplier: 0.5, sigma_range: 0.01, passes: 1 } },
      { key: "medium", label: "Medium", values: { feature_scale_multiplier: 1.0, sigma_range: 0.05, passes: 1 } },
      { key: "strong", label: "Strong", values: { feature_scale_multiplier: 1.0, sigma_range: 0.05, passes: 2 } },
      { key: "very_strong", label: "Very Strong", values: { feature_scale_multiplier: 2.0, sigma_range: 0.05, passes: 2 } },
      { key: "custom", label: "Custom", custom: true },
    ],
  },
  b3_tv_flatten: {
    controlLabel: "Flat-area smoothing",
    defaultPreset: "balanced",
    paramLabels: {
      tv_weight: "Flattening strength",
      weight_autoscale: "Scale strength to print width",
      channel_axis: "Flattening mode",
      n_iter_max: "Iteration limit",
    },
    paramTooltips: {
      tv_weight: "Higher values push the image toward broader smooth regions.",
      weight_autoscale: "Keeps flattening strength tied to the configured printable feature width.",
      channel_axis: "Controls whether flattening operates in RGB, perceptual lightness/chroma, or lightness only.",
      n_iter_max: "Advanced convergence cap for the flattening operation.",
    },
    choiceLabels: {
      channel_axis: {
        srgb_rgb: "RGB channels",
        oklab_L_ab: "Lightness and chroma",
        oklab_L_only: "Lightness only",
      },
    },
    presets: [
      { key: "off", label: "Off", enabled: false },
      { key: "subtle", label: "Subtle", values: { tv_weight: 0.01, weight_autoscale: true, channel_axis: "oklab_L_only", n_iter_max: 20 } },
      { key: "balanced", label: "Balanced", values: { tv_weight: 0.04, weight_autoscale: true, channel_axis: "oklab_L_only", n_iter_max: 20 } },
      { key: "bold", label: "Bold", values: { tv_weight: 0.16, weight_autoscale: true, channel_axis: "oklab_L_only", n_iter_max: 20 } },
      { key: "graphic", label: "Graphic", values: { tv_weight: 0.16, weight_autoscale: true, channel_axis: "oklab_L_ab", n_iter_max: 20 } },
      { key: "custom", label: "Custom", custom: true },
    ],
  },
  c1_achievable_tonemap: {
    controlLabel: "Palette tone fit",
    defaultPreset: "balanced",
    paramLabels: {
      strength: "Tone fit strength",
      shadow_percentile: "Shadow anchor",
      highlight_percentile: "Highlight anchor",
      midtone_contrast: "Midtone curve",
    },
    paramTooltips: {
      strength: "Blend amount between source tone and palette-achievable remap.",
      shadow_percentile: "Source luminance percentile used as the dark anchor.",
      highlight_percentile: "Source luminance percentile used as the bright anchor.",
      midtone_contrast: "Controls midpoint placement in the tone curve.",
    },
    presets: [
      { key: "off", label: "Off", enabled: false },
      { key: "subtle", label: "Subtle", values: { strength: 0.15, shadow_percentile: 0.0, highlight_percentile: 97.5, midtone_contrast: 1.0 } },
      { key: "balanced", label: "Balanced", values: { strength: 0.25, shadow_percentile: 0.25, highlight_percentile: 99.5, midtone_contrast: 0.75 } },
      { key: "strong", label: "Strong", values: { strength: 0.40, shadow_percentile: 0.25, highlight_percentile: 99.5, midtone_contrast: 0.75 } },
      { key: "custom", label: "Custom", custom: true },
    ],
  },
  c2_soft_gamut_compress: {
    controlLabel: "Palette saturation fit",
    defaultPreset: "medium",
    paramLabels: {
      knee_start_ratio: "Compression start",
      knee_softness: "Compression softness",
    },
    paramTooltips: {
      knee_start_ratio: "Lower values begin compressing saturation earlier and affect more pixels.",
      knee_softness: "Controls the shape of the soft-knee transition.",
    },
    presets: [
      { key: "off", label: "Off", enabled: false },
      { key: "light", label: "Light", values: { knee_start_ratio: 0.98, knee_softness: 0.50 } },
      { key: "medium", label: "Medium", values: { knee_start_ratio: 0.85, knee_softness: 0.50 } },
      { key: "strong", label: "Strong", values: { knee_start_ratio: 0.75, knee_softness: 0.30 } },
      { key: "custom", label: "Custom", custom: true },
    ],
  },
};

// Config state (mirrors server session config)
let config = {
  base_filament: "bambu-tough-white",
  cap_filament: "__same__",
  max_dim_mm: 130,
  // Canonical solve-resolution fields
  image_sample_pitch_mm: 0.20,
  solver_fine_pitch_mm: 0.20,
  detail_cap_enabled: true,
  detail_cap_max_layers: 5,
  detail_cap_smoothing_enabled: true,
  detail_cap_smoothing_exact_speckle_max_px: 1,
  detail_cap_smoothing_cumulative_component_max_px: 2,
  detail_cap_smoothing_cumulative_hole_max_px: 2,
  color_region_target_mm: 0.60,
  stage1_coarsening_factor: 1,
  emit_pressure_diagnostics: false,
  emit_geometry_attribution: false,
  emit_blueprint_printability: true,
  printability_minimum_extrusion_width_mm: null,
  printability_minimum_line_length_mm: null,
  enforce_printability: true,
  color_region_target_from_printability: true,
  color_region_target_width_multiplier: 2.0,
  stage2_fine_override_enabled: true,
  stage2_final_printability_gate_fine_override: true,
  stage2_printability_gate_fine_override: true,
  stage2_printability_repair_fine_override: true,
  stage2_boundary_mutation_enabled: true,
  stage2_boundary_mutation_min_gain: null,
  stage2_boundary_mutation_min_component_mm: null,
  stage2_boundary_mutation_current_de_percentile: null,
  stage2_boundary_mutation_max_passes: 1,
  stage4_printability_gate_detail: true,
  layer_height: 0.08,
  d_wb: 0.20,
  d_wc_min: 0.08,
  t_max: 3.0,
  k_max: 3,
  cell_mode: "felzenszwalb",
  de_threshold: 0.01,
  smooth_kernel: 5.0,
  border: false,
  border_width_mm: 3.0,
  border_height_mm: 3.0,
  use_corrections: true,
  appearance_model_provider: "photo_stack_bundle",
  photo_stack_bundle_path: null,
  cap_mode: "appearance_bounded_smooth",
  boundary_cap_de_budget: 0.008,
  cap_continuity_cleanup: true,
  gamut_mode: "hull",
  gamut_white_rescale: false,
  // Camera Transform ingress is mandatory for current solves.
  model_domain_ingress: true,
  chroma_weight: 1.0,
  luminance_mode: "standard",
  luminance_handler_enabled: false,
  luminance_handler_mode: "boundary_prior",
  luminance_handler_strength: 1.0,
  luminance_handler_optical_authority_fraction: 0.75,
  luminance_base_shading_limit_fraction: 0.75,
  luminance_handler_boundary_percentile: 95.0,
  luminance_handler_boundary_sigma_px: null,
  luminance_handler_response_curve: "linear",
  luminance_handler_response_gamma: 1.0,
  luminance_handler_detail_residual: true,
  luminance_handler_include_solver_detail: true,
  luminance_detail_authoring_printability: "off",
  source_resample_kernel: "lanczos",
  preprocessing_params: {},
  swap_improvement_threshold: 2.0,
  force_all_tiers: false,
};

let lastColorCapMode = loadLastColorCapMode(config.cap_mode || "appearance_bounded_smooth");
let capModeForcedByLuminance = false;

function normalizeColorCapModeForStorage(mode) {
  return mode === "smooth_variable" ? "smooth_variable" : "appearance_bounded_smooth";
}

function loadLastColorCapMode(fallback = "appearance_bounded_smooth") {
  try {
    const stored = localStorage.getItem(COLOR_CAP_MODE_STORAGE_KEY);
    if (stored === "appearance_bounded_smooth" || stored === "smooth_variable") {
      return stored;
    }
  } catch { /* ignore */ }
  return normalizeColorCapModeForStorage(fallback);
}

function saveLastColorCapMode(mode) {
  lastColorCapMode = normalizeColorCapModeForStorage(mode);
  try {
    localStorage.setItem(COLOR_CAP_MODE_STORAGE_KEY, lastColorCapMode);
  } catch { /* ignore */ }
  return lastColorCapMode;
}

function applyMandatoryProductSettings() {
  config.model_domain_ingress = true;
  config.detail_cap_enabled = true;
  config.enforce_printability = true;
  config.cap_continuity_cleanup = true;
  config.color_region_target_from_printability = true;
  config.stage2_final_printability_gate_fine_override = true;
  config.stage2_printability_gate_fine_override = true;
  config.stage2_printability_repair_fine_override = true;
  config.stage4_printability_gate_detail = true;
}

function formatRegionPlanningScale(value = config.stage1_coarsening_factor || 1) {
  const factor = Math.max(1, parseInt(value, 10) || 1);
  return factor === 1 ? "1x (full detail)" : `${factor}x (coarser regions)`;
}

function formatRegionMethod(mode = config.cell_mode || "felzenszwalb") {
  switch (String(mode || "felzenszwalb").toLowerCase()) {
    case "grid": return "fixed grid";
    case "slic": return "superpixels";
    case "felzenszwalb":
    default:
      return "image regions";
  }
}

function getCurrentLayerHeight() {
  const domValue = parseFloat($("#cfgLayerHeight")?.value);
  if (Number.isFinite(domValue) && domValue > 0) return domValue;
  const configValue = parseFloat(config.layer_height);
  return Number.isFinite(configValue) && configValue > 0 ? configValue : 0.08;
}

function minCapLayersFromThickness(thicknessMm = config.d_wc_min, layerHeight = getCurrentLayerHeight()) {
  const lh = Math.max(Number(layerHeight) || 0.08, 1e-9);
  const thickness = Math.max(Number(thicknessMm) || lh, lh);
  return Math.max(1, Math.ceil(thickness / lh - 1e-9));
}

function minCapThicknessFromLayers(layerCount, layerHeight = getCurrentLayerHeight()) {
  const layers = Math.max(1, Math.trunc(Number(layerCount) || 1));
  const lh = Math.max(Number(layerHeight) || 0.08, 1e-9);
  return Math.round(layers * lh * 1e6) / 1e6;
}

function smoothingRadiusMmFromCells(cells = config.smooth_kernel, solvePitch = getCurrentSolvePitch()) {
  const cellCount = Math.max(0, Number(cells) || 0);
  const pitch = Math.max(Number(solvePitch) || 0.20, 1e-9);
  return Math.round(cellCount * pitch * 1e6) / 1e6;
}

function smoothingCellsFromRadiusMm(radiusMm, solvePitch = getCurrentSolvePitch()) {
  const radius = Math.max(0, Number(radiusMm) || 0);
  const pitch = Math.max(Number(solvePitch) || 0.20, 1e-9);
  return radius / pitch;
}

function normalizeLuminanceMode(mode) {
  const raw = String(mode || "standard").trim().toLowerCase();
  if (["luminance", "luminance-detail", "luminance_detail", "detail"].includes(raw)) {
    return "luminance_detail";
  }
  return "standard";
}

function clampLuminanceBaseShadingLimitFraction(value) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return 0.75;
  return Math.max(0.0, Math.min(1.0, parsed));
}

function getLuminanceBaseShadingLimitFraction() {
  const current = config.luminance_base_shading_limit_fraction;
  const legacy = config.luminance_handler_optical_authority_fraction;
  const currentParsed = Number(current);
  const legacyParsed = Number(legacy);
  if (
    Number.isFinite(legacyParsed)
    && (!Number.isFinite(currentParsed) || (currentParsed === 0.75 && legacyParsed !== 0.75))
  ) {
    return clampLuminanceBaseShadingLimitFraction(legacyParsed);
  }
  return clampLuminanceBaseShadingLimitFraction(
    Number.isFinite(currentParsed) ? currentParsed : 0.75,
  );
}

function setLuminanceBaseShadingLimitFraction(value) {
  const clamped = clampLuminanceBaseShadingLimitFraction(value);
  config.luminance_base_shading_limit_fraction = clamped;
  config.luminance_handler_optical_authority_fraction = clamped;
  return clamped;
}

function formatLuminanceBaseShadingLimitPercent(value) {
  return String(Math.round(clampLuminanceBaseShadingLimitFraction(value) * 100));
}

function parseLuminanceBaseShadingLimitPercent(value) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return 0.75;
  return clampLuminanceBaseShadingLimitFraction(parsed / 100);
}

function getBaseShadingLimitInput() {
  return $("#cfgBaseShadingLimit");
}

function getBaseShadingLimitSlider() {
  return $("#cfgBaseShadingLimitSlider");
}

function syncBaseShadingLimitControls(percentValue = null) {
  const percent = percentValue == null
    ? formatLuminanceBaseShadingLimitPercent(getLuminanceBaseShadingLimitFraction())
    : String(Math.max(0, Math.min(100, Math.round(Number(percentValue) || 0))));
  const input = getBaseShadingLimitInput();
  if (input) input.value = percent;
  const slider = getBaseShadingLimitSlider();
  if (slider) slider.value = percent;
}

function applyLuminanceMode(mode, options = {}) {
  const { resetStandard = false } = options;
  const normalized = normalizeLuminanceMode(mode);
  config.luminance_mode = normalized;
  if (normalized === "luminance_detail") {
    config.luminance_handler_enabled = true;
    config.luminance_handler_mode = "boundary_ceiling";
    config.luminance_handler_strength = 1.0;
    setLuminanceBaseShadingLimitFraction(getLuminanceBaseShadingLimitFraction());
    config.luminance_handler_boundary_percentile = 95.0;
    config.luminance_handler_boundary_sigma_px = null;
    config.luminance_handler_response_curve = "linear";
    config.luminance_handler_response_gamma = 1.0;
    config.luminance_handler_detail_residual = true;
    config.luminance_handler_include_solver_detail = true;
    config.detail_cap_enabled = true;
    config.luminance_detail_authoring_printability = "absolute_finalgate";
    config.enforce_printability = true;
    config.emit_blueprint_printability = true;
  } else if (resetStandard) {
    config.luminance_handler_enabled = false;
    config.luminance_detail_authoring_printability = "off";
  }
  applyMandatoryProductSettings();
  return normalized;
}

function normalizeActiveGamutMode(mode = "hull") {
  const normalized = String(mode || "hull").trim().toLowerCase();
  return normalized === "chroma" ? "hue_preserving" : normalized;
}

const CHROMA_WEIGHT_SLIDER_MIN = -3;
const CHROMA_WEIGHT_SLIDER_MAX = 3;

function normalizeChromaWeight(weight) {
  const numeric = Number(weight);
  return Number.isFinite(numeric) && numeric > 0 ? numeric : 1.0;
}

function chromaWeightToSliderPosition(weight) {
  return Math.log2(normalizeChromaWeight(weight));
}

function chromaWeightFromSliderPosition(position) {
  const numeric = Number(position);
  return Math.pow(2, Number.isFinite(numeric) ? numeric : 0);
}

function formatChromaWeightReadout(weight) {
  return normalizeChromaWeight(weight).toFixed(2);
}

function syncChromaWeightControlFromConfig() {
  const rawWeight = normalizeChromaWeight(config.chroma_weight ?? 1.0);
  const slider = $("#cfgChromaWeight");
  if (slider) {
    const position = chromaWeightToSliderPosition(rawWeight);
    slider.value = String(Math.max(
      CHROMA_WEIGHT_SLIDER_MIN,
      Math.min(CHROMA_WEIGHT_SLIDER_MAX, position)
    ));
  }
  const readout = $("#cfgChromaWeightReadout");
  if (readout) readout.textContent = formatChromaWeightReadout(rawWeight);
}

function applyChromaWeightSliderInput(position) {
  config.chroma_weight = chromaWeightFromSliderPosition(position);
  syncChromaWeightControlFromConfig();
  return config.chroma_weight;
}

function getSolveModeControlValue() {
  const selected = document.querySelector("#cfgLuminanceMode .segmented-btn.is-active");
  return selected?.dataset.value || config.luminance_mode || "standard";
}

function setSolveModeControlValue(mode) {
  const normalized = normalizeLuminanceMode(mode);
  document.querySelectorAll("#cfgLuminanceMode .segmented-btn").forEach(btn => {
    const active = normalizeLuminanceMode(btn.dataset.value) === normalized;
    btn.classList.toggle("is-active", active);
    btn.setAttribute("aria-checked", active ? "true" : "false");
  });
}

// Gamut state (per-card, stored in deck[].gamut)

// Solve state
let solveStatus = { status: "idle", progress: "", elapsed_s: 0, result: null };
let solvePollingOwner = null;
let activeSolveRunId = null;
let activeSolveJobId = null;
let solveStartPending = false;
let solveCancelPending = false;
let solveProgressHideTimer = null;
let exportRunning = false;
let exportPollingOwner = null;

// Export state
let exportSelectedRunId = null;
let activeExportRunId = null;

// Solve view state
let solveView = "predicted";    // current solve tab view mode
// solveResultsPerCard removed — replaced by solveRuns[]
let _suggestPolling = null;
let activeSuggestJobId = null;
let suggestCancelPending = false;

// ── DOM refs ─────────────────────────────────────────────────────────────────

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

// ── Solve Run Factory ────────────────────────────────────────────────────────

function buildSolveRecipeContext(palette, settingsSnapshot = null) {
  const baseProfile = loadedProfileRef || {
    id: SYSTEM_SETTINGS_PROFILE_ID,
    kind: "system",
    name: SYSTEM_SETTINGS_PROFILE_NAME,
  };
  const modified = isSettingsProfileModified();
  const settings = _cloneValue(settingsSnapshot || _currentSettingsSnapshot());
  const modules = _currentSettingsProfileModulesSnapshot();
  const profileRef = modified
    ? {
        kind: "transient",
        source_kind: baseProfile.kind || "system",
        source_id: baseProfile.id || SYSTEM_SETTINGS_PROFILE_ID,
        source_name: baseProfile.name || SYSTEM_SETTINGS_PROFILE_NAME,
        name: baseProfile.name || SYSTEM_SETTINGS_PROFILE_NAME,
      }
    : {
        kind: baseProfile.kind || "system",
        id: baseProfile.id || SYSTEM_SETTINGS_PROFILE_ID,
        name: baseProfile.name || SYSTEM_SETTINGS_PROFILE_NAME,
      };
  return {
    profile_ref: profileRef,
    profile_name_at_solve: baseProfile.name || SYSTEM_SETTINGS_PROFILE_NAME,
    is_profile_modified_at_solve: modified,
    recipe_snapshot: {
      palette: [...palette],
      profile_ref: _cloneValue(profileRef),
      profile_snapshot: {
        name: baseProfile.name || SYSTEM_SETTINGS_PROFILE_NAME,
        settings,
        modules,
      },
    },
  };
}

function describeSolveRunProfile(run) {
  const profileRef = run?.profile_ref || {};
  const name = run?.recipe_snapshot?.profile_snapshot?.name
    || run?.profile_name_at_solve
    || profileRef.name
    || SYSTEM_SETTINGS_PROFILE_NAME;
  if (run?.is_profile_modified_at_solve) {
    const sourceName = profileRef.source_name || name;
    return {
      name,
      badgeClass: "settings-profile-mini-badge is-warn",
      badgeLabel: "modified draft",
      meta: `Draft based on ${sourceName}`,
    };
  }
  if ((profileRef.kind || "").toLowerCase() === "system" || profileRef.id === SYSTEM_SETTINGS_PROFILE_ID) {
    return {
      name,
      badgeClass: "settings-profile-mini-badge",
      badgeLabel: "system",
      meta: "System default profile",
    };
  }
  return {
    name,
    badgeClass: "settings-profile-mini-badge is-accent",
    badgeLabel: "saved",
    meta: "Named Settings Profile",
  };
}

function createSolveRun(palette, configSnapshot, recipeContext = null) {
  solveRunCounter++;
  const currentRecipeContext = recipeContext || buildSolveRecipeContext(palette);
  return {
    id: `run-${Date.now()}`,
    label: `Run ${solveRunCounter}`,
    image: selectedImage ? {
      filename: selectedImage.filename,
      width: selectedImage.width,
      height: selectedImage.height,
      scale_pct: configSnapshot.scale_pct || 100,
    } : null,
    palette: [...palette],
    config: _cloneValue(configSnapshot),
    ar: getEffectiveAR(),
    profile_ref: _cloneValue(currentRecipeContext.profile_ref),
    profile_name_at_solve: currentRecipeContext.profile_name_at_solve,
    is_profile_modified_at_solve: !!currentRecipeContext.is_profile_modified_at_solve,
    recipe_snapshot: _cloneValue(currentRecipeContext.recipe_snapshot),
    results: null,
    exportRecords: [],
    selectedExportId: null,
    timestamp: Date.now(),
  };
}

function _runAspect(run) {
  const ar = (run && typeof run.ar === "number" && isFinite(run.ar) && run.ar > 0)
    ? run.ar
    : getEffectiveAR();
  return `${Math.round(ar * 1000)} / 1000`;
}

function getSelectedRuns() {
  return solveRuns.filter(r => selectedRunIds.has(r.id));
}

// ── Utility ──────────────────────────────────────────────────────────────────

function showToast(message, type) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.className = "toast is-visible" + (type ? ` toast-${type}` : "");
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => toast.classList.remove("is-visible"), 3000);
}

// ── Operation Progress Overlay ───────────────────────────────────────────────

let _opAbort = null;
let _opTimer = null;
let _opStartTime = 0;
let _opLastElapsedSeconds = 0;
let activeExportJobId = "";
let exportCancelPending = false;

function _slowButtons() {
  return [
    $("#suggestPalettesBtn"), $("#exportFilesBtn"),
  ].filter(Boolean);
}

function startProgress(label, owner = "", { cancellable = true } = {}) {
  _opAbort = new AbortController();
  _opStartTime = Date.now();
  _opLastElapsedSeconds = 0;
  const el = $("#opProgress");
  const lbl = $("#opProgressLabel");
  const elapsed = $("#opProgressElapsed");
  const cancelBtn = $("#opProgressCancel");
  const fill = el?.querySelector(".op-progress-fill");
  if (lbl) lbl.textContent = label;
  if (elapsed) elapsed.textContent = "0s";
  if (cancelBtn) {
    cancelBtn.hidden = !cancellable;
    cancelBtn.disabled = !cancellable;
  }
  if (fill) {
    fill.className = "op-progress-fill indeterminate";
    fill.style.width = "";
  }
  if (el) {
    el.dataset.owner = owner;
    el.dataset.cancellable = cancellable ? "true" : "false";
    el.classList.remove("is-hidden");
  }
  clearInterval(_opTimer);
  _opTimer = setInterval(() => {
    const s = Math.round((Date.now() - _opStartTime) / 1000);
    setOperationElapsedSeconds(s);
  }, 500);
  _slowButtons().forEach(b => b.disabled = true);
  return _opAbort.signal;
}

function setOperationElapsedSeconds(seconds) {
  const elapsed = $("#opProgressElapsed");
  const next = Math.max(0, Math.round(Number(seconds) || 0));
  _opLastElapsedSeconds = Math.max(_opLastElapsedSeconds || 0, next);
  if (elapsed) elapsed.textContent = `${_opLastElapsedSeconds}s`;
}

function formatDurationSeconds(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value) || value < 0) return "";
  const rounded = Math.round(value);
  if (rounded < 60) return `${rounded}s`;
  const minutes = Math.floor(rounded / 60);
  const remainder = rounded % 60;
  return remainder ? `${minutes}m ${remainder}s` : `${minutes}m`;
}

function resetOperationElapsedSeconds() {
  _opLastElapsedSeconds = 0;
  const elapsed = $("#opProgressElapsed");
  if (elapsed) elapsed.textContent = "0s";
}

function stopProgress() {
  const el = $("#opProgress");
  const cancelBtn = $("#opProgressCancel");
  if (el) {
    el.classList.add("is-hidden");
    el.dataset.owner = "";
    el.dataset.cancellable = "";
  }
  if (cancelBtn) {
    cancelBtn.hidden = false;
    cancelBtn.disabled = false;
  }
  clearInterval(_opTimer);
  _opTimer = null;
  _opAbort = null;
  _opLastElapsedSeconds = 0;
  _slowButtons().forEach(b => b.disabled = false);
}

function renderSuggestCancellationState() {
  const el = $("#opProgress");
  if (!el || el.dataset.owner !== "suggest") return;
  const label = $("#opProgressLabel");
  const cancelBtn = $("#opProgressCancel");
  const prefix = "Cancellation requested: ";
  if (label) {
    const current = String(label.textContent || "");
    if (suggestCancelPending && !current.startsWith(prefix)) {
      label.textContent = `${prefix}${current || "Suggesting palettes..."}`;
    } else if (!suggestCancelPending && current.startsWith(prefix)) {
      label.textContent = current.slice(prefix.length);
    }
  }
  if (cancelBtn) cancelBtn.disabled = suggestCancelPending;
}

async function requestSuggestCancellation() {
  if (!_suggestPolling || !activeSuggestJobId || suggestCancelPending) return;
  const cancellationJobId = activeSuggestJobId;
  suggestCancelPending = true;
  renderSuggestCancellationState();
  try {
    const response = await apiPost(
      `/palette/suggest/cancel?job_id=${encodeURIComponent(cancellationJobId)}`,
      {},
    );
    if (activeSuggestJobId !== cancellationJobId) return;
    if (response?.cancelled) assertPolledJobIdentity(response, cancellationJobId);
    if (!response?.cancelled) {
      suggestCancelPending = false;
      renderSuggestCancellationState();
      return;
    }
    showToast("Suggestion cancellation requested", "warn");
  } catch (err) {
    if (activeSuggestJobId !== cancellationJobId) return;
    suggestCancelPending = false;
    renderSuggestCancellationState();
    showToast(`Could not request suggestion cancellation: ${err.message}`, "error");
  }
}

function renderExportCancellationState() {
  const el = $("#opProgress");
  if (!el || el.dataset.owner !== "export") return;
  const label = $("#opProgressLabel");
  const cancelBtn = $("#opProgressCancel");
  const prefix = "Cancellation requested: ";
  if (label) {
    const current = String(label.textContent || "");
    if (exportCancelPending && !current.startsWith(prefix)) {
      label.textContent = `${prefix}${current || "Exporting files..."}`;
    } else if (!exportCancelPending && current.startsWith(prefix)) {
      label.textContent = current.slice(prefix.length);
    }
  }
  if (cancelBtn) cancelBtn.disabled = !activeExportJobId || exportCancelPending;
}

async function requestExportCancellation() {
  if (!exportRunning || !activeExportJobId || exportCancelPending) return;
  const cancellationJobId = activeExportJobId;
  exportCancelPending = true;
  renderExportCancellationState();
  try {
    const response = await cancelExport(cancellationJobId);
    if (activeExportJobId !== cancellationJobId) return;
    if (response?.cancelled) assertPolledJobIdentity(response, cancellationJobId);
    if (!response?.cancelled) {
      exportCancelPending = false;
      renderExportCancellationState();
      return;
    }
    showToast("Export cancellation requested", "warn");
  } catch (err) {
    if (activeExportJobId !== cancellationJobId) return;
    exportCancelPending = false;
    renderExportCancellationState();
    showToast(`Could not request export cancellation: ${err.message}`, "error");
  }
}

function cancelProgress() {
  const el = $("#opProgress");
  if (el && el.dataset.owner === "solve") {
    // Delegate to solve cancel handler
    handleCancelSolve();
    return;
  }
  if (el && el.dataset.owner === "export") {
    void requestExportCancellation();
    return;
  }
  if (el && el.dataset.owner === "suggest") {
    requestSuggestCancellation();
    return;
  }
  if (el?.dataset.cancellable === "false") return;
  if (_opAbort) _opAbort.abort();
  showToast("Cancelled", "warn");
  stopProgress();
}

function updateOperationProgressFromStatus(status, fallbackLabel = "Working...") {
  const el = $("#opProgress");
  const lbl = $("#opProgressLabel");
  const elapsed = $("#opProgressElapsed");
  const fill = el?.querySelector(".op-progress-fill");
  const d = status?.progress_detail || {};

  let label = d.stage_label || status?.progress || fallbackLabel;
  if (el?.dataset.owner === "suggest") {
    suggestCancelPending = suggestCancelPending || Boolean(status?.cancel_requested);
  }
  if (el?.dataset.owner === "export") {
    exportCancelPending = exportCancelPending
      || status?.status === "cancelling"
      || Boolean(status?.cancel_requested);
  }
  if (d.stage_index && d.stage_count) {
    label = `Step ${d.stage_index}/${d.stage_count}: ${label}`;
  }
  if (lbl) lbl.textContent = label;
  renderSuggestCancellationState();
  renderExportCancellationState();

  const pct = d.overall_pct ?? d.stage_pct;
  if (fill) {
    if (pct != null && Number.isFinite(Number(pct))) {
      fill.className = "op-progress-fill";
      fill.style.width = `${Math.max(0, Math.min(100, Number(pct)))}%`;
    } else {
      fill.className = "op-progress-fill indeterminate";
      fill.style.width = "";
    }
  }

  const elapsedVal = status?.elapsed_s ?? d.elapsed_s;
  if (elapsed && elapsedVal != null) setOperationElapsedSeconds(elapsedVal);
}

/** In-browser confirm dialog. Returns a promise that resolves to true/false. */
function appConfirm(message, { ok = "OK", cancel = "Cancel", title = "Confirm" } = {}) {
  return new Promise(resolve => {
    const overlay = $("#appDialog");
    const titleEl = $("#appDialogTitle");
    const msg = $("#appDialogMsg");
    const input = $("#appDialogInput");
    const buttons = $("#appDialogButtons");
    const closeBtn = $("#appDialogClose");
    const hint = $("#appDialogHint");
    if (titleEl) titleEl.textContent = title;
    msg.textContent = message;
    input.style.display = "none";
    if (hint) {
      hint.classList.add("is-hidden");
      hint.innerHTML = "";
    }
    buttons.innerHTML = `
      <button class="ghost-button small" id="appDialogNo">${esc2(cancel)}</button>
      <button class="primary-button small" id="appDialogYes">${esc2(ok)}</button>
    `;
    overlay.setAttribute("aria-hidden", "false");
    const close = (val) => { overlay.setAttribute("aria-hidden", "true"); resolve(val); };
    $("#appDialogNo").onclick = () => close(false);
    $("#appDialogYes").onclick = () => close(true);
    if (closeBtn) closeBtn.onclick = () => close(false);
    overlay.onclick = (e) => { if (e.target === overlay) close(false); };
  });
}

/** In-browser prompt dialog. Returns a promise that resolves to string or null. */
function appPrompt(message, defaultValue = "", { title = "Input", validate = null } = {}) {
  return new Promise(resolve => {
    const overlay = $("#appDialog");
    const titleEl = $("#appDialogTitle");
    const msg = $("#appDialogMsg");
    const input = $("#appDialogInput");
    const buttons = $("#appDialogButtons");
    const closeBtn = $("#appDialogClose");
    const validationClass = "app-dialog-validation";
    const previousFocus = document.activeElement;
    if (titleEl) titleEl.textContent = title;
    msg.textContent = message;
    input.style.display = "";
    input.value = defaultValue;
    buttons.innerHTML = `
      <button class="ghost-button small" id="appDialogNo">Cancel</button>
      <button class="primary-button small" id="appDialogYes">OK</button>
    `;
    overlay.setAttribute("aria-hidden", "false");
    let settled = false;
    const cancelBtn = $("#appDialogNo");
    const okBtn = $("#appDialogYes");
    const onDocumentKeyDown = (e) => {
      if (overlay.getAttribute("aria-hidden") !== "false") return;
      if (e.key === "Escape") {
        e.preventDefault();
        close(null);
      }
    };
    const cleanup = () => {
      document.removeEventListener("keydown", onDocumentKeyDown);
      overlay.onclick = null;
      input.onkeydown = null;
      input.oninput = null;
      if (cancelBtn) cancelBtn.onclick = null;
      if (okBtn) okBtn.onclick = null;
      if (closeBtn) closeBtn.onclick = null;
      input.removeAttribute("aria-invalid");
      overlay.querySelector(`.${validationClass}`)?.remove();
    };
    const close = (val) => {
      if (settled) return;
      settled = true;
      cleanup();
      overlay.setAttribute("aria-hidden", "true");
      if (previousFocus && document.body.contains(previousFocus) && typeof previousFocus.focus === "function") {
        previousFocus.focus();
      } else {
        input.blur();
      }
      resolve(val);
    };
    const clearValidation = () => {
      input.removeAttribute("aria-invalid");
      overlay.querySelector(`.${validationClass}`)?.remove();
    };
    const showValidation = (messageText) => {
      input.setAttribute("aria-invalid", "true");
      let validation = overlay.querySelector(`.${validationClass}`);
      if (!validation) {
        validation = document.createElement("div");
        validation.className = validationClass;
        input.parentElement?.appendChild(validation);
      }
      validation.textContent = String(messageText);
    };
    const submit = () => {
      const validationMessage = typeof validate === "function" ? validate(input.value) : "";
      if (validationMessage) {
        showValidation(validationMessage);
        input.focus();
        return;
      }
      close(input.value);
    };
    if (cancelBtn) cancelBtn.onclick = () => close(null);
    if (okBtn) okBtn.onclick = submit;
    if (closeBtn) closeBtn.onclick = () => close(null);
    input.oninput = clearValidation;
    overlay.onclick = (e) => { if (e.target === overlay) close(null); };
    input.onkeydown = (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        submit();
      } else if (e.key === "Escape") {
        e.preventDefault();
        close(null);
      }
    };
    document.addEventListener("keydown", onDocumentKeyDown);
    setTimeout(() => {
      if (settled || overlay.getAttribute("aria-hidden") !== "false") return;
      input.focus();
      input.select();
    }, 50);
  });
}

/** In-browser multi-choice dialog. Resolves to the selected option value or null. */
function appChoice(message, options = [], { title = "Choose" } = {}) {
  return new Promise(resolve => {
    const overlay = $("#appDialog");
    const titleEl = $("#appDialogTitle");
    const msg = $("#appDialogMsg");
    const input = $("#appDialogInput");
    const buttons = $("#appDialogButtons");
    const closeBtn = $("#appDialogClose");
    const hint = $("#appDialogHint");
    if (titleEl) titleEl.textContent = title;
    msg.textContent = message;
    input.style.display = "none";
    if (hint) {
      hint.classList.add("is-hidden");
      hint.innerHTML = "";
    }
    buttons.innerHTML = options.map((opt, index) => {
      const btnClass = opt.kind === "primary"
        ? "primary-button small"
        : opt.kind === "danger"
          ? "ghost-button small danger"
          : "ghost-button small";
      const id = `appDialogChoice${index}`;
      return `<button class="${btnClass}" id="${id}">${esc2(opt.label)}</button>`;
    }).join("");
    overlay.setAttribute("aria-hidden", "false");
    const close = (val) => { overlay.setAttribute("aria-hidden", "true"); resolve(val); };
    options.forEach((opt, index) => {
      $(`#appDialogChoice${index}`).onclick = () => close(opt.value);
    });
    if (closeBtn) closeBtn.onclick = () => close(null);
    overlay.onclick = (e) => { if (e.target === overlay) close(null); };
  });
}

function showPaletteSaveModal(defaultValue = "") {
  return new Promise(resolve => {
    const overlay = $("#paletteSaveModal");
    const input = $("#paletteSaveNameInput");
    const cancel = $("#paletteSaveCancel");
    const submit = $("#paletteSaveSubmit");
    const closeBtn = $("#paletteSaveModalClose");
    if (!overlay || !input || !cancel || !submit || !closeBtn) {
      resolve(null);
      return;
    }

    let settled = false;
    const close = (value) => {
      if (settled) return;
      settled = true;
      overlay.classList.add("is-hidden");
      overlay.setAttribute("aria-hidden", "true");
      overlay.onclick = null;
      cancel.onclick = null;
      submit.onclick = null;
      closeBtn.onclick = null;
      input.onkeydown = null;
      document.removeEventListener("keydown", onKeyDown);
      resolve(value);
    };
    const onKeyDown = (e) => {
      if (overlay.getAttribute("aria-hidden") !== "false") return;
      if (e.key === "Escape") close(null);
    };

    input.value = defaultValue || "";
    overlay.classList.remove("is-hidden");
    overlay.setAttribute("aria-hidden", "false");
    overlay.onclick = (e) => { if (e.target === overlay) close(null); };
    cancel.onclick = () => close(null);
    closeBtn.onclick = () => close(null);
    submit.onclick = () => close(input.value);
    input.onkeydown = (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        close(input.value);
      } else if (e.key === "Escape") {
        e.preventDefault();
        close(null);
      }
    };
    document.addEventListener("keydown", onKeyDown);
    setTimeout(() => {
      input.focus();
      input.select();
    }, 0);
  });
}

// Escape helper that doesn't depend on esc() (avoids circular ref during init)
function esc2(str) { const el = document.createElement("span"); el.textContent = str; return el.innerHTML; }

function esc(str) {
  const el = document.createElement("span");
  el.textContent = str;
  return el.innerHTML;
}

function escAttr(str) {
  return String(str ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[char]);
}

function colorRmseValue(result) {
  if (!result) return null;
  const value = result.source_rms_de ?? result.color_rmse ?? result.suggestion_mean_de ?? result.mean_de;
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : null;
}

function formatColorRmse(result, digits = 3) {
  const rmse = colorRmseValue(result);
  if (rmse == null) return "RMSE % --";
  if (result?.suggestion_mean_de != null && result?.source_rms_de == null && result?.color_rmse == null) {
    return `Suggest dE ${rmse.toFixed(digits)}`;
  }
  return `RMSE % ${(rmse * 100).toFixed(digits)}`;
}

function formatSolveRunCardRmse(result, digits = 3) {
  const rmse = colorRmseValue(result);
  if (rmse == null) return "RMSE --";
  return `RMSE ${(rmse * 100).toFixed(digits)}%`;
}

function textColorForHex(hex) {
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  return (0.299 * r + 0.587 * g + 0.114 * b) > 140 ? "#1f1b18" : "#fffdf8";
}

function isLightHex(hex) {
  if (typeof hex !== "string" || !/^#[0-9a-f]{6}$/i.test(hex)) return false;
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  return (0.299 * r + 0.587 * g + 0.114 * b) > 180;
}

function filamentById(id) {
  return allFilaments.find((f) => f.filament_id === id);
}

// ── Tab Navigation ───────────────────────────────────────────────────────────

function switchTab(tab) {
  // If printer config is open and this is an external tab click, close config first
  const pcPage = $("#printerConfigPage");
  if (pcPage && !pcPage.classList.contains("is-hidden") && printerConfigOriginTab !== null) {
    // hidePrinterConfigPage will call switchTab again with the target
    hidePrinterConfigPage(tab);
    return;
  }
  hideSolveRunHoverPreview();
  hideSolveRunSettingsPanel();
  currentTab = tab;
  $$(".mode-button").forEach((btn) => {
    btn.classList.toggle("is-active", btn.dataset.tab === tab);
  });
  $$(".tab-content").forEach((section) => {
    section.classList.toggle("is-hidden", section.id !== `tab${capitalize(tab)}`);
  });
  if (tab === "image") renderImageTab();
  if (tab === "creation") renderCreationTab();
  if (tab === "solve") renderSolveTab();
  if (tab === "export") renderExportTab();
  updateRail();
}

function capitalize(s) { return s.charAt(0).toUpperCase() + s.slice(1); }

function updateTabStates() {
  $$(".mode-button").forEach((btn) => {
    const tab = btn.dataset.tab;
    let complete = false;
    if (tab === "image") complete = !!selectedImage;
    if (tab === "creation") complete = getActivePalette().length > 0;
    if (tab === "settings") complete = !!selectedImage && getActivePalette().length > 0;
    if (tab === "solve") complete = solveRuns.some(run => !!run.results);
    if (tab === "export") complete = solveRuns.some((run) => getRunExportRecords(run).length > 0);
    btn.classList.toggle("step-complete", complete);
  });
}

// ── Left Rail ────────────────────────────────────────────────────────────────

function updateRailFramedPreview(container) {
  // Render the framed preview (with crop, rotation, adjustments, border) to a
  // small canvas and display it in the sidebar thumbnail.
  const srcImg = $("#previewImg");
  if (!srcImg || !srcImg.naturalWidth || !selectedImage) return;

  const ar = getEffectiveAR();
  const bwMm = (config.border && config.border_width_mm > 0) ? config.border_width_mm : 0;
  const footW = frameState.widthMm + 2 * bwMm;
  const footH = frameState.heightMm + 2 * bwMm;
  const footAR = footW / footH;

  // Thumbnail size (fit within ~200px)
  const maxDim = 200;
  let thumbW, thumbH;
  if (footAR > 1) { thumbW = maxDim; thumbH = maxDim / footAR; }
  else { thumbH = maxDim; thumbW = maxDim * footAR; }

  const borderFrac = bwMm / footW;
  const bPx = borderFrac * thumbW;
  const frameW = thumbW - 2 * bPx;
  const frameH = thumbH - 2 * bPx;

  const canvas = document.createElement("canvas");
  canvas.width = thumbW;
  canvas.height = thumbH;
  const ctx = canvas.getContext("2d");

  // White border band
  if (bPx > 0) {
    ctx.fillStyle = "#fff";
    ctx.fillRect(0, 0, thumbW, thumbH);
  }

  // Black fallback behind the cropped image frame.
  ctx.fillStyle = "#000";
  ctx.fillRect(bPx, bPx, frameW, frameH);

  // Draw image within the frame area, applying crop/scale/rotation/flip.
  // The image always covers the frame; aspect differences crop source content.
  ctx.save();
  ctx.beginPath();
  ctx.rect(bPx, bPx, frameW, frameH);
  ctx.clip();

  const imgNatW = srcImg.naturalWidth;
  const imgNatH = srcImg.naturalHeight;
  const geom = cropCoverImageGeometry(frameW, frameH, imgNatW, imgNatH, frameState.scale, frameState.rotation);
  const displayW = geom.displayW;
  const displayH = geom.displayH;

  const slackX = Math.max(0, geom.visualW - frameW);
  const slackY = Math.max(0, geom.visualH - frameH);
  const offsetX = frameState.panX * slackX / 2;
  const offsetY = frameState.panY * slackY / 2;

  const imgL = bPx + frameW / 2 - offsetX;
  const imgT = bPx + frameH / 2 - offsetY;

  ctx.translate(imgL, imgT);
  ctx.rotate(frameState.rotation * Math.PI / 180);
  ctx.scale(frameState.flipH ? -1 : 1, frameState.flipV ? -1 : 1);
  ctx.drawImage(srcImg, -displayW / 2, -displayH / 2, displayW, displayH);
  ctx.restore();

  // Pixelation pass — always on in sidebar (shows actual print resolution)
  {
    const pxSizeMm = config.image_sample_pitch_mm || 0.20;
    const gridW = Math.max(1, Math.round(frameState.widthMm / pxSizeMm));
    const gridH = Math.max(1, Math.round(frameState.heightMm / pxSizeMm));

    // Draw image only at solve-grid resolution, using the same crop-cover model.
    const tmp = document.createElement("canvas");
    tmp.width = gridW;
    tmp.height = gridH;
    const tmpCtx = tmp.getContext("2d");
    const gGeom = cropCoverImageGeometry(gridW, gridH, imgNatW, imgNatH, frameState.scale, frameState.rotation);
    const gDispW = gGeom.displayW;
    const gDispH = gGeom.displayH;
    const gSlackX = Math.max(0, gGeom.visualW - gridW);
    const gSlackY = Math.max(0, gGeom.visualH - gridH);
    const gOffX = frameState.panX * gSlackX / 2;
    const gOffY = frameState.panY * gSlackY / 2;
    tmpCtx.save();
    tmpCtx.translate(gridW / 2 - gOffX, gridH / 2 - gOffY);
    tmpCtx.rotate(frameState.rotation * Math.PI / 180);
    tmpCtx.scale(frameState.flipH ? -1 : 1, frameState.flipV ? -1 : 1);
    tmpCtx.drawImage(srcImg, -gDispW / 2, -gDispH / 2, gDispW, gDispH);
    tmpCtx.restore();

    // Black background then pixelated image on top
    ctx.save();
    ctx.beginPath();
    ctx.rect(bPx, bPx, frameW, frameH);
    ctx.clip();
    ctx.fillStyle = "#000";
    ctx.fillRect(bPx, bPx, frameW, frameH);
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(tmp, 0, 0, gridW, gridH, bPx, bPx, frameW, frameH);
    ctx.restore();
  }

  // Thin outline around the whole thing
  ctx.strokeStyle = "rgba(0,0,0,0.15)";
  ctx.lineWidth = 1;
  ctx.strokeRect(0.5, 0.5, thumbW - 1, thumbH - 1);

  container.innerHTML = "";
  const img = document.createElement("img");
  img.src = canvas.toDataURL();
  img.alt = selectedImage.filename;
  container.appendChild(img);
}

function updateRail() {
  const preview = $("#railImagePreview");
  if (selectedImage) {
    if (currentTab === "image") {
      // Tab 1: show source image
      preview.innerHTML = `<img src="${imagePreviewUrl(selectedImage.filename)}" alt="${selectedImage.filename}">`;
    } else {
      // Other tabs: show framed preview snapshot
      updateRailFramedPreview(preview);
    }
  } else {
    preview.innerHTML = `<span class="muted-line">No image selected</span>`;
  }

  updateTabStates();

  const badge = $("#dataSourceBadge");
  badge.textContent = apiConnected ? "connected" : "offline";
  badge.classList.toggle("connected", apiConnected);
  renderModelLibraryRail();
  updateSolveReadiness();
}

// ── Printer Rail + Config Page ──────────────────────────────────────────────

function renderPrinterRail() {
  if (!printersData) return;
  const printers = printersData.printers || [];
  const activeId = printersData.active_printer_id;
  const container = $("#railPrinterSelector");
  if (!container) return;

  if (printers.length <= 1) {
    const printerName = printers.length
      ? (printers[0].name || printerConfig.name || "Unnamed printer")
      : "No printer configured";
    container.innerHTML = `<div class="rail-printer-name" title="${escAttr(printerName)}">${esc(printerName)}</div>`;
  } else {
    const activePrinter = printers.find(p => p.id === activeId) || printers[0];
    container.innerHTML = `<select id="railPrinterSelect" class="rail-select" title="${escAttr(activePrinter?.name || "Active printer")}" aria-label="Active printer">
      ${printers.map(p => `<option value="${p.id}"${p.id === activeId ? " selected" : ""}>${esc(p.name)}</option>`).join("")}
    </select>`;
    const sel = $("#railPrinterSelect");
    if (sel) sel.addEventListener("change", async () => {
      await setActivePrinter({ active_printer_id: sel.value });
      await loadPrinters();
    });
  }

  // Nozzle dropdown
  const printer = printers.find(p => p.id === activeId) || printers[0];
  const nozzleSel = $("#railNozzleSelect");
  if (nozzleSel && printer) {
    const profiles = printer.nozzle_profiles || [];
    nozzleSel.innerHTML = profiles.map(n =>
      `<option value="${n.size}"${n.size === printersData.active_nozzle_size ? " selected" : ""}>${n.size}mm</option>`
    ).join("");
    nozzleSel.onchange = async () => {
      await setActivePrinter({ active_nozzle_size: parseFloat(nozzleSel.value) });
      await loadPrinters();
    };
    nozzleSel.disabled = profiles.length === 0;
    nozzleSel.title = profiles.length ? "Active nozzle" : "No nozzle profiles configured";
  } else if (nozzleSel) {
    nozzleSel.innerHTML = "";
    nozzleSel.onchange = null;
    nozzleSel.disabled = true;
    nozzleSel.title = "No printer configured";
  }
}

async function loadPrinters() {
  try {
    printersData = await fetchPrinters();
    const active = await fetchActivePrinter();
    const printer = active.printer;
    activeNozzle = active.nozzle;
    if (printer) {
      printerConfig.name = printer.name;
      printerConfig.max_x_mm = printer.max_print_area?.x || 256;
      printerConfig.max_y_mm = printer.max_print_area?.y || 256;
      printerConfig.ams_units = printer.ams_units || 1;
      printerConfig.slots_per_unit = printer.slots_per_ams || 4;
      printerConfig.ams_slots = printerConfig.ams_units * printerConfig.slots_per_unit;
    }
    renderPrinterRail();
    updateDerivedParams();
  } catch (e) {
    console.warn("[printers] load failed:", e.message);
  }
}

function showPrinterConfigPage() {
  // The settings drawer is a content-area overlay; close it so printer config isn't behind it.
  if (settingsDrawerOpen) closeSettingsDrawer();
  // Record which tab the user came from
  printerConfigOriginTab = currentTab;
  // Dim the origin tab (still visually marked, but muted)
  $$(".mode-button").forEach(btn => {
    btn.classList.remove("is-active");
    btn.classList.toggle("is-dimmed", btn.dataset.tab === printerConfigOriginTab);
  });
  // Show config overlay on top of current tab content (don't hide it)
  const page = $("#printerConfigPage");
  if (page) page.classList.remove("is-hidden");
  printerConfigEditingId = printersData?.active_printer_id || printersData?.printers?.[0]?.id || null;
  renderPrinterConfigPage();
}

async function hidePrinterConfigPage(navigateTo) {
  // Auto-save on exit
  _readPrinterFromConfigPage();
  if (printerConfigEditingId && printersData?.printers?.some(p => p.id === printerConfigEditingId)) {
    printersData.active_printer_id = printerConfigEditingId;
    syncPrinterConfigActiveNozzle();
  }
  try {
    await savePrinters(printersData);
    await loadPrinters();
  } catch (e) {
    showToast("Failed to save printer config: " + e.message, "error");
  }
  const page = $("#printerConfigPage");
  if (page) page.classList.add("is-hidden");
  // Clear dimmed state
  $$(".mode-button").forEach(btn => btn.classList.remove("is-dimmed"));
  // Navigate to the requested tab (or back to origin)
  const target = navigateTo || printerConfigOriginTab || currentTab;
  printerConfigOriginTab = null;
  printerConfigEditingId = null;
  switchTab(target);
}

function defaultNozzleLineWidths(size) {
  const nozzleSize = parseFloat(size) || 0.4;
  if (Math.abs(nozzleSize - 0.2) < 1e-6) {
    return {
      line_width: 0.22,
      min_line_width: 0.16,
      max_line_width: 0.25,
      min_line_length: 0.40,
    };
  }
  if (Math.abs(nozzleSize - 0.4) < 1e-6) {
    return {
      line_width: 0.42,
      min_line_width: 0.32,
      max_line_width: 0.5,
      min_line_length: 0.50,
    };
  }
  const minLineWidth = Math.round(nozzleSize * 0.8 * 100) / 100;
  const minLineLength = Math.round(Math.max(0.40, minLineWidth + 0.10) * 100) / 100;
  return {
    line_width: Math.round(nozzleSize * 1.05 * 100) / 100,
    min_line_width: minLineWidth,
    max_line_width: Math.round(nozzleSize * 1.25 * 100) / 100,
    min_line_length: minLineLength,
  };
}

function normalizeNozzleProfile(profile) {
  const nozzle = { ...(profile || {}) };
  const size = parseFloat(nozzle.size) || 0.4;
  const defaults = defaultNozzleLineWidths(size);
  let minLayerHeight = parseFloat(nozzle.min_layer_height);
  let maxLayerHeight = parseFloat(nozzle.max_layer_height);
  if (!Number.isFinite(minLayerHeight) || minLayerHeight <= 0) minLayerHeight = 0.08;
  if (!Number.isFinite(maxLayerHeight) || maxLayerHeight <= 0) maxLayerHeight = 0.32;
  if (minLayerHeight > maxLayerHeight) [minLayerHeight, maxLayerHeight] = [maxLayerHeight, minLayerHeight];
  let minLineWidth = parseFloat(nozzle.min_line_width);
  let maxLineWidth = parseFloat(nozzle.max_line_width);
  if (!Number.isFinite(minLineWidth)) minLineWidth = defaults.min_line_width;
  if (!Number.isFinite(maxLineWidth)) maxLineWidth = defaults.max_line_width;
  if (minLineWidth > maxLineWidth) [minLineWidth, maxLineWidth] = [maxLineWidth, minLineWidth];
  let lineWidth = parseFloat(nozzle.line_width);
  if (!Number.isFinite(lineWidth)) lineWidth = defaults.line_width;
  lineWidth = Math.min(Math.max(lineWidth, minLineWidth), maxLineWidth);
  let minLineLength = parseFloat(nozzle.min_line_length);
  if (!Number.isFinite(minLineLength) || minLineLength <= 0) {
    minLineLength = defaults.min_line_length;
  }
  return {
    size,
    min_layer_height: minLayerHeight,
    max_layer_height: maxLayerHeight,
    line_width: lineWidth,
    min_line_width: minLineWidth,
    max_line_width: maxLineWidth,
    min_line_length: minLineLength,
  };
}

function currentPrinterConfigId() {
  const printers = printersData?.printers || [];
  if (!printers.length) {
    printerConfigEditingId = null;
    return null;
  }
  if (!printerConfigEditingId || !printers.some(p => p.id === printerConfigEditingId)) {
    printerConfigEditingId = printersData.active_printer_id || printers[0].id;
  }
  if (!printers.some(p => p.id === printerConfigEditingId)) {
    printerConfigEditingId = printers[0].id;
  }
  return printerConfigEditingId;
}

function syncPrinterConfigActiveNozzle() {
  const printer = (printersData?.printers || []).find(p => p.id === printerConfigEditingId);
  const profiles = printer?.nozzle_profiles || [];
  if (!profiles.some(n => Number(n.size) === Number(printersData.active_nozzle_size))) {
    printersData.active_nozzle_size = profiles.length ? Number(profiles[0].size) : null;
  }
}

function selectPrinterConfigId(nextId) {
  if (!nextId || nextId === printerConfigEditingId) return;
  _readPrinterFromConfigPage();
  resetPrinterDeleteConfirm();
  printerConfigEditingId = nextId;
  printersData.active_printer_id = nextId;
  syncPrinterConfigActiveNozzle();
}

function resetPrinterDeleteConfirm() {
  if (printerDeleteConfirmTimer) {
    clearTimeout(printerDeleteConfirmTimer);
    printerDeleteConfirmTimer = null;
  }
  printerDeleteConfirmPending = false;
  const btn = $("#pcDeletePrinterBtn");
  if (btn) {
    btn.textContent = "Delete";
    btn.classList.remove("confirm-pending");
    btn.title = "Delete selected printer";
  }
}

function updatePrinterConfigDropdownLabel(printerId, label) {
  const sel = $("#pcPrinterSelect");
  const option = Array.from(sel?.options || []).find(opt => opt.value === String(printerId));
  if (option) option.textContent = label;
}

function renderPrinterConfigPage() {
  if (!printersData) return;
  const printers = printersData.printers || [];
  const selectedId = currentPrinterConfigId();

  // Printer selector
  const sel = $("#pcPrinterSelect");
  if (sel) {
    sel.innerHTML = printers.map(p =>
      `<option value="${p.id}"${p.id === selectedId ? " selected" : ""}>${esc(p.name)}</option>`
    ).join("");
    sel.value = selectedId || "";
    sel.onchange = () => {
      selectPrinterConfigId(sel.value);
      renderPrinterConfigPage();
    };
  }

  const printer = printers.find(p => p.id === selectedId);
  if (!printer) return;

  // Fill fields
  const pcName = $("#pcName");
  if (pcName) {
    pcName.value = printer.name;
    pcName.oninput = () => {
      const nextName = (pcName.value || "").trim() || "New Printer";
      printer.name = nextName;
      updatePrinterConfigDropdownLabel(printer.id, nextName);
    };
  }
  const pcAreaX = $("#pcAreaX");
  if (pcAreaX) pcAreaX.value = printer.max_print_area?.x || 256;
  const pcAreaY = $("#pcAreaY");
  if (pcAreaY) pcAreaY.value = printer.max_print_area?.y || 256;
  const pcAmsUnits = $("#pcAmsUnits");
  if (pcAmsUnits) pcAmsUnits.value = printer.ams_units || 1;
  const pcSlotsPerAms = $("#pcSlotsPerAms");
  if (pcSlotsPerAms) pcSlotsPerAms.value = printer.slots_per_ams || 4;

  // Nozzle table
  const tbody = $("#pcNozzleBody");
  if (tbody) {
    tbody.innerHTML = (printer.nozzle_profiles || []).map((profile, i) => {
      const n = normalizeNozzleProfile(profile);
      return `
      <tr data-idx="${i}">
        <td><input type="number" class="nz-size" value="${n.size}" step="0.1" min="0.1" max="1.0"></td>
        <td><input type="number" class="nz-min-lh" value="${n.min_layer_height}" step="0.01" min="0.01" max="1.0"></td>
        <td><input type="number" class="nz-max-lh" value="${n.max_layer_height}" step="0.01" min="0.01" max="1.0"></td>
        <td><input type="number" class="nz-min-lw" value="${n.min_line_width}" step="0.01" min="0.05" max="2.0"></td>
        <td><input type="number" class="nz-min-ll" value="${n.min_line_length}" step="0.01" min="0.05" max="10.0"></td>
        <td><button class="ghost-button xs danger nz-delete" data-idx="${i}" aria-label="Delete nozzle profile" title="Delete nozzle profile">${xIconSvg()}</button></td>
      </tr>
    `;
    }).join("");

    tbody.querySelectorAll(".nz-delete").forEach(btn => {
      btn.addEventListener("click", () => {
        const idx = parseInt(btn.dataset.idx);
        printer.nozzle_profiles.splice(idx, 1);
        renderPrinterConfigPage();
      });
    });
  }

  // Delete printer button visibility
  const delBtn = $("#pcDeletePrinterBtn");
  if (delBtn) {
    delBtn.style.display = printers.length > 1 ? "" : "none";
    if (!printerDeleteConfirmPending) {
      delBtn.textContent = "Delete";
      delBtn.title = "Delete selected printer";
      delBtn.classList.remove("confirm-pending");
    }
  }
}

function _readPrinterFromConfigPage() {
  const printers = printersData.printers || [];
  const sel = $("#pcPrinterSelect");
  const selectedId = printerConfigEditingId || (sel ? sel.value : printersData.active_printer_id);
  const printer = printers.find(p => p.id === selectedId);
  if (!printer) return null;

  printer.name = ($("#pcName")?.value || printer.name).trim();
  printer.max_print_area = {
    x: parseFloat($("#pcAreaX")?.value) || 256,
    y: parseFloat($("#pcAreaY")?.value) || 256,
  };
  printer.ams_units = parseInt($("#pcAmsUnits")?.value) || 1;
  printer.slots_per_ams = parseInt($("#pcSlotsPerAms")?.value) || 4;

  // Read nozzle rows from table
  const rows = $$("#pcNozzleBody tr");
  printer.nozzle_profiles = Array.from(rows).map(row => {
    const size = parseFloat(row.querySelector(".nz-size")?.value) || 0.4;
    const defaults = defaultNozzleLineWidths(size);
    let minLineWidth = parseFloat(row.querySelector(".nz-min-lw")?.value);
    if (!Number.isFinite(minLineWidth)) minLineWidth = defaults.min_line_width;
    const maxLineWidth = Math.max(defaults.max_line_width, minLineWidth);
    let lineWidth = defaults.line_width;
    lineWidth = Math.min(Math.max(lineWidth, minLineWidth), maxLineWidth);
    let minLineLength = parseFloat(row.querySelector(".nz-min-ll")?.value);
    if (!Number.isFinite(minLineLength) || minLineLength <= 0) {
      minLineLength = defaults.min_line_length;
    }
    return {
      size,
      min_layer_height: parseFloat(row.querySelector(".nz-min-lh")?.value) || 0.08,
      max_layer_height: parseFloat(row.querySelector(".nz-max-lh")?.value) || 0.32,
      line_width: lineWidth,
      min_line_width: minLineWidth,
      max_line_width: maxLineWidth,
      min_line_length: minLineLength,
    };
  });

  return printer;
}

// ── Frame Editor Sub-tabs ───────────────────────────────────────────────────

function switchFrameEditorTab(tab) {
  frameEditorTab = tab;
  $$(".frame-tab").forEach(btn => btn.classList.toggle("is-active", btn.dataset.ftab === tab));
  const sizeCtrl = $("#frameControlsSize");
  const imgCtrl = $("#frameControlsImage");
  if (sizeCtrl) sizeCtrl.classList.toggle("is-hidden", tab !== "size");
  if (imgCtrl) imgCtrl.classList.toggle("is-hidden", tab !== "image");
  // Lock canvas interaction in Image mode
  const canvas = $("#frameCanvas");
  if (canvas) canvas.classList.toggle("interaction-locked", tab === "image");
}

// ── Image Library Pane State ─────────────────────────────────────────────────

function setLibraryPaneState(state) {
  libraryPaneState = state;
  const panel = $("#imageLibraryPanel");
  if (panel) panel.dataset.state = state;
  const resizeBtn = $("#libraryResizeBtn");
  if (resizeBtn) {
    const expanded = state === "expanded";
    const label = expanded ? "Compact image library" : "Expand image library";
    resizeBtn.innerHTML = panelResizeIconSvg(expanded);
    resizeBtn.setAttribute("aria-pressed", expanded ? "true" : "false");
    resizeBtn.title = label;
    resizeBtn.setAttribute("aria-label", resizeBtn.title);
  }
}

function toggleLibraryPaneState() {
  setLibraryPaneState(libraryPaneState === "expanded" ? "contracted" : "expanded");
}

// ── Image Tab ────────────────────────────────────────────────────────────────

function renderImageTab() {
  setLibraryPaneState(libraryPaneState);
  renderImageGrid();
  renderFrameCanvas();
  renderPreview();
  updateInfoGrid();
  updateBorderVisibility();
  syncDimFields();
  syncScaleSlider();
  syncRotationSlider();
  updateARButtons();
  switchFrameEditorTab(frameEditorTab);
  syncWidthSlider();
  syncHeightSlider();
}

async function refreshImageLibrary({ announce = false } = {}) {
  if (!apiConnected) {
    showToast("Start the server to refresh the image library", "warn");
    return;
  }

  const previousFilename = selectedImage?.filename || null;
  availableImages = await fetchImages();
  const refreshedSelection = previousFilename
    ? availableImages.find((img) => img.filename === previousFilename) || null
    : null;
  const selectionWasRemoved = !!previousFilename && !refreshedSelection;
  selectedImage = refreshedSelection;

  renderImageTab();
  updateRail();

  if (selectionWasRemoved) {
    await syncConfigToServer({ showErrorToast: true });
    showToast(`Removed missing image "${previousFilename}" from the current setup`, "warn");
  } else if (announce) {
    const count = availableImages.length;
    showToast(`Image library refreshed (${count} ${count === 1 ? "image" : "images"})`, "success");
  }
}

function renderImageGrid() {
  const grid = $("#imageGrid");

  if (availableImages.length === 0) {
    grid.innerHTML = `<p class="muted-line" style="text-align:center;padding:20px 0">
      ${apiConnected ? "No images found" : "Connect to server"}
    </p>`;
    return;
  }
  grid.innerHTML = availableImages.map((img) => {
    const sizeKb = img.size_kb || 0;
    const sizeStr = sizeKb >= 1024 ? (sizeKb / 1024).toFixed(1) + " MB" : sizeKb.toFixed(0) + " KB";
    return `<div class="image-card${selectedImage?.filename === img.filename ? " is-selected" : ""}"
         data-filename="${img.filename}" draggable="true">
      <img class="image-card-thumb" src="${imagePreviewUrl(img.filename)}" alt="${img.filename}"
           loading="lazy" onerror="this.style.display='none'">
      <div class="image-card-info">
        <strong>${esc(img.filename)}</strong>
        ${img.width}&times;${img.height} &middot; ${sizeStr}
      </div>
    </div>`;
  }).join("");

  grid.querySelectorAll(".image-card").forEach((card) => {
    card.addEventListener("click", () => {
      const filename = card.dataset.filename;
      const newImage = availableImages.find((i) => i.filename === filename) || null;
      const isNewImage = newImage?.filename !== selectedImage?.filename;
      if (isNewImage) {
        frameState.scale = 100.0;
        frameState.rotation = 0;
        frameState.panX = 0;
        frameState.panY = 0;
        frameState.flipH = false;
        frameState.flipV = false;
      }
      selectedImage = newImage;
      if (newImage && isNewImage) {
        applyImageAspectDefault();              // Stage 11: default to image aspect, short side 120mm
      } else if (frameState.arMode === "image" && newImage) {
        applyARFromLastTouched();
      }
      renderImageTab();
      updateRail();
    });
    card.addEventListener("dragstart", (e) => {
      e.dataTransfer.setData("application/json", JSON.stringify({
        type: "image", filename: card.dataset.filename,
      }));
      e.dataTransfer.effectAllowed = "copy";
    });
  });
}

function bindImageLibraryWheelScroll() {
  const grid = $("#imageGrid");
  if (!grid || grid._horizontalWheelScrollAttached) return;
  grid.addEventListener("wheel", (e) => {
    if (currentTab !== "image" || e.ctrlKey) return;
    if (grid.scrollWidth <= grid.clientWidth + 1) return;
    const delta = Math.abs(e.deltaY) >= Math.abs(e.deltaX) ? e.deltaY : e.deltaX;
    if (!delta) return;
    const before = grid.scrollLeft;
    grid.scrollLeft += delta;
    if (grid.scrollLeft !== before) e.preventDefault();
  }, { passive: false });
  grid._horizontalWheelScrollAttached = true;
}

// ── Frame Canvas ────────────────────────────────────────────────────────────

function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

function getEffectiveAR() {
  const m = frameState.arMode;
  if (m === "specified") return frameState.widthMm / frameState.heightMm;
  if (m === "image") {
    if (selectedImage) {
      const bounds = getTransformedSourceBounds();
      return bounds.width / bounds.height;
    }
    return frameState.widthMm / frameState.heightMm;  // fallback if no image
  }
  if (m === "ratio") {
    const raw = frameState.customRatio.x / frameState.customRatio.y;
    return imageDirection === "portrait" ? 1 / raw : raw;
  }
  if (m === "1:1") return 1;
  const [a, b] = m.split(":").map(Number);
  const raw = a / b;
  return imageDirection === "portrait" ? 1 / raw : raw;
}

function openRatioDialog() {
  const dialog = $("#ratioDialog");
  if (!dialog) return;
  dialog.setAttribute("aria-hidden", "false");
  const rx = $("#ratioDialogX");
  const ry = $("#ratioDialogY");
  if (rx) rx.value = frameState.customRatio.x;
  if (ry) ry.value = frameState.customRatio.y;
  if (rx) rx.focus();
}

function closeRatioDialog() {
  $("#ratioDialog")?.setAttribute("aria-hidden", "true");
}

function rotatedBounds(width, height, rotationDeg) {
  const rad = Math.abs(rotationDeg || 0) * Math.PI / 180;
  const cosR = Math.abs(Math.cos(rad));
  const sinR = Math.abs(Math.sin(rad));
  return {
    width: width * cosR + height * sinR,
    height: width * sinR + height * cosR,
  };
}

function cropCoverImageGeometry(frameW, frameH, imgW, imgH, scalePct, rotationDeg) {
  const rotated = rotatedBounds(imgW, imgH, rotationDeg);
  const baseScale = Math.max(
    frameW / Math.max(rotated.width, 1),
    frameH / Math.max(rotated.height, 1),
  );
  const zoom = Math.max(1, (Number(scalePct) || 100) / 100);
  const imgScale = baseScale * zoom;
  const displayW = imgW * imgScale;
  const displayH = imgH * imgScale;
  const visualW = rotated.width * imgScale;
  const visualH = rotated.height * imgScale;
  return { imgScale, displayW, displayH, visualW, visualH };
}

function getTransformedSourceBounds() {
  if (!selectedImage) return { width: 1, height: 1 };
  return rotatedBounds(selectedImage.width, selectedImage.height, frameState.rotation);
}

function largestContainedCrop(bounds, aspect) {
  const safeAspect = Math.max(Number(aspect) || 1, 1e-9);
  const boundsAR = bounds.width / Math.max(bounds.height, 1e-9);
  if (boundsAR >= safeAspect) {
    return { width: bounds.height * safeAspect, height: bounds.height };
  }
  return { width: bounds.width, height: bounds.width / safeAspect };
}

function cropModelFromFrameState() {
  const bounds = getTransformedSourceBounds();
  const aspect = getEffectiveAR();
  const base = largestContainedCrop(bounds, aspect);
  const zoom = Math.max(1, (Number(frameState.scale) || 100) / 100);
  const width = Math.max(1, base.width / zoom);
  const height = Math.max(1, base.height / zoom);
  const slackX = Math.max(0, bounds.width - width);
  const slackY = Math.max(0, bounds.height - height);
  const panX = slackX <= 1e-6 ? 0 : clamp(Number(frameState.panX) || 0, -1, 1);
  const panY = slackY <= 1e-6 ? 0 : clamp(Number(frameState.panY) || 0, -1, 1);
  frameState.panX = panX;
  frameState.panY = panY;
  return {
    bounds,
    aspect,
    width,
    height,
    left: (bounds.width - width) / 2 + panX * slackX / 2,
    top: (bounds.height - height) / 2 + panY * slackY / 2,
    slackX,
    slackY,
    scalePxPerSource: 1,
  };
}

function projectCropToFrame(frameL, frameT, frameW, frameH) {
  const crop = cropModelFromFrameState();
  const sourceScale = frameW / Math.max(crop.width, 1e-9);
  const visualW = crop.bounds.width * sourceScale;
  const visualH = crop.bounds.height * sourceScale;
  const displayW = selectedImage.width * sourceScale;
  const displayH = selectedImage.height * sourceScale;
  const visualLeft = frameL - crop.left * sourceScale;
  const visualTop = frameT - crop.top * sourceScale;
  const visualCenterX = visualLeft + visualW / 2;
  const visualCenterY = visualTop + visualH / 2;
  return {
    crop,
    imgScale: sourceScale,
    displayW,
    displayH,
    visualW,
    visualH,
    imgL: visualCenterX - displayW / 2,
    imgT: visualCenterY - displayH / 2,
    visualLeft,
    visualTop,
    visualRight: visualLeft + visualW,
    visualBottom: visualTop + visualH,
  };
}

function projectDragCropToFrame() {
  const snap = frameDragState?.projection;
  if (!snap) return null;
  return {
    crop: cropModelFromFrameState(),
    imgScale: snap.imgScale,
    displayW: snap.displayW,
    displayH: snap.displayH,
    visualW: snap.visualW,
    visualH: snap.visualH,
    imgL: snap.imgL,
    imgT: snap.imgT,
    visualLeft: snap.visualLeft,
    visualTop: snap.visualTop,
    visualRight: snap.visualRight,
    visualBottom: snap.visualBottom,
  };
}

function resetCropToFitSource() {
  frameState.scale = 100.0;
  frameState.panX = 0;
  frameState.panY = 0;
}

function roundFrameMm(value) {
  return Math.round(Number(value) * 100) / 100;
}

function applyCropModel(targetCrop, { anchor = "height" } = {}) {
  if (!selectedImage) return false;
  const bounds = getTransformedSourceBounds();
  let targetW = clamp(Number(targetCrop.width) || bounds.width, 1, bounds.width);
  let targetH = clamp(Number(targetCrop.height) || bounds.height, 1, bounds.height);
  let targetLeft = clamp(Number(targetCrop.left) || 0, 0, Math.max(0, bounds.width - targetW));
  let targetTop = clamp(Number(targetCrop.top) || 0, 0, Math.max(0, bounds.height - targetH));
  let aspect = targetW / Math.max(targetH, 1e-9);

  if (anchor === "height") {
    const heightMm = clampFrameHeight(frameState.heightMm);
    const widthMm = clampFrameWidth(heightMm * aspect);
    const actualAspect = widthMm / Math.max(heightMm, 1e-9);
    if (Math.abs(actualAspect - aspect) > 1e-6) {
      const centerX = targetLeft + targetW / 2;
      targetW = clamp(targetH * actualAspect, 1, bounds.width);
      targetLeft = clamp(centerX - targetW / 2, 0, Math.max(0, bounds.width - targetW));
      aspect = actualAspect;
    }
    frameState.widthMm = roundFrameMm(widthMm);
    frameState.heightMm = roundFrameMm(heightMm);
    lastTouchedDim = "width";
  } else {
    const widthMm = clampFrameWidth(frameState.widthMm);
    const heightMm = clampFrameHeight(widthMm / Math.max(aspect, 1e-9));
    const actualAspect = widthMm / Math.max(heightMm, 1e-9);
    if (Math.abs(actualAspect - aspect) > 1e-6) {
      const centerY = targetTop + targetH / 2;
      targetH = clamp(targetW / Math.max(actualAspect, 1e-9), 1, bounds.height);
      targetTop = clamp(centerY - targetH / 2, 0, Math.max(0, bounds.height - targetH));
      aspect = actualAspect;
    }
    frameState.widthMm = roundFrameMm(widthMm);
    frameState.heightMm = roundFrameMm(heightMm);
    lastTouchedDim = "height";
  }

  frameState.arMode = "specified";
  const base = largestContainedCrop(bounds, aspect);
  const zoom = Math.max(
    1,
    Math.min(base.width / Math.max(targetW, 1e-9), base.height / Math.max(targetH, 1e-9)),
  );
  frameState.scale = clamp(zoom * 100, 100, 1000);

  const actualW = base.width / Math.max(frameState.scale / 100, 1e-9);
  const actualH = base.height / Math.max(frameState.scale / 100, 1e-9);
  const slackX = Math.max(0, bounds.width - actualW);
  const slackY = Math.max(0, bounds.height - actualH);
  frameState.panX = slackX <= 1e-6
    ? 0
    : clamp((targetLeft - (bounds.width - actualW) / 2) * 2 / slackX, -1, 1);
  frameState.panY = slackY <= 1e-6
    ? 0
    : clamp((targetTop - (bounds.height - actualH) / 2) * 2 / slackY, -1, 1);
  return true;
}

function fitFrameToSourceWidth() {
  if (widthLocked || !selectedImage) return;
  const current = cropModelFromFrameState();
  applyCropModel({
    left: 0,
    top: current.top,
    width: current.bounds.width,
    height: current.height,
  }, { anchor: "height" });
}

function fitFrameToSourceHeight() {
  if (heightLocked || !selectedImage) return;
  const current = cropModelFromFrameState();
  applyCropModel({
    left: current.left,
    top: 0,
    width: current.width,
    height: current.bounds.height,
  }, { anchor: "width" });
}

function finishFrameModelUpdate({ syncServer = true } = {}) {
  syncDimFields();
  syncWidthSlider();
  syncHeightSlider();
  syncScaleSlider();
  updateARButtons();
  renderFrameCanvas();
  updateInfoGrid();
  if (syncServer) syncConfigToServer();
}

// ── SVG filter for highlights, shadows, temperature ──────────────────────
// CSS filters handle grayscale, brightness, contrast, hue-rotate, saturate.
// feComponentTransfer gives us real tone curves (highlights/shadows) and
// per-channel linear ramps (temperature) that CSS can't do.

function _ensureSvgFilter() {
  if (document.getElementById("imgAdjustSVG")) return;
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("width", "0");
  svg.setAttribute("height", "0");
  svg.style.position = "absolute";
  const defs = document.createElementNS(ns, "defs");
  const filter = document.createElementNS(ns, "filter");
  filter.setAttribute("id", "imgAdjustSVG");
  filter.setAttribute("color-interpolation-filters", "sRGB");
  const ct = document.createElementNS(ns, "feComponentTransfer");
  ct.setAttribute("id", "imgAdjustCT");
  for (const ch of ["R", "G", "B"]) {
    const func = document.createElementNS(ns, `feFunc${ch}`);
    func.setAttribute("id", `imgAdjustFunc${ch}`);
    func.setAttribute("type", "table");
    func.setAttribute("tableValues", "0 0.25 0.5 0.75 1");
    ct.appendChild(func);
  }
  filter.appendChild(ct);
  defs.appendChild(filter);
  svg.appendChild(defs);
  document.body.appendChild(svg);
}

function _updateSvgFilter() {
  _ensureSvgFilter();
  const hl = imageAdjust.highlight;   // -1..1
  const sh = imageAdjust.shadow;      // -1..1
  const temp = imageAdjust.temperature / 100;  // -1..1

  // Build a 5-point table transfer curve per channel.
  // Points at t = 0, 0.25, 0.5, 0.75, 1.0
  // Shadow adjustment shifts the dark end (t=0, 0.25)
  // Highlight adjustment shifts the bright end (t=0.75, 1.0)
  // Temperature shifts R up/B down (warm) or R down/B up (cool)
  for (const ch of ["R", "G", "B"]) {
    // Per-channel temperature bias (warm = boost R, cut B)
    let bias = 0;
    if (ch === "R") bias = temp * 0.25;
    if (ch === "B") bias = -temp * 0.25;

    // Identity curve + adjustments
    // Shadow slider shifts dark end (p0, p1); highlight shifts bright end (p3, p4)
    const p0 = Math.max(0, Math.min(1, 0.0  + sh * 0.30 + bias));
    const p1 = Math.max(0, Math.min(1, 0.25 + sh * 0.18 + bias));
    const p2 = Math.max(0, Math.min(1, 0.50 + bias));
    const p3 = Math.max(0, Math.min(1, 0.75 + hl * 0.18 + bias));
    const p4 = Math.max(0, Math.min(1, 1.0  + hl * 0.30 + bias));

    const func = document.getElementById(`imgAdjustFunc${ch}`);
    if (func) func.setAttribute("tableValues",
      `${p0.toFixed(4)} ${p1.toFixed(4)} ${p2.toFixed(4)} ${p3.toFixed(4)} ${p4.toFixed(4)}`);
  }
}

function buildAdjustFilterCSS() {
  const parts = [];
  if (imageAdjust.mode === "bw") parts.push("grayscale(100%)");
  if (imageAdjust.exposure !== 0) parts.push(`brightness(${1 + imageAdjust.exposure})`);
  if (imageAdjust.contrast !== 0) parts.push(`contrast(${1 + imageAdjust.contrast})`);
  // Tint: approximate with sepia base + hue rotation + strength via saturation blend
  if (imageAdjust.tint_strength > 0) {
    parts.push(`sepia(${imageAdjust.tint_strength})`);
    parts.push(`hue-rotate(${imageAdjust.tint_hue}deg)`);
  }
  if (imageAdjust.saturation !== 0) parts.push(`saturate(${1 + imageAdjust.saturation})`);

  // SVG filter for highlights, shadows, temperature (tone curve + channel bias)
  const needsSvg = imageAdjust.highlight !== 0
                || imageAdjust.shadow !== 0
                || imageAdjust.temperature !== 0;
  if (needsSvg) {
    _updateSvgFilter();
    parts.push("url(#imgAdjustSVG)");
  }

  return parts.join(" ") || "none";
}

function renderFrameCanvas() {
  const canvas = $("#frameCanvas");
  const img = $("#frameImage");
  const placeholder = $("#framePlaceholder");
  const mask = $("#frameMask");
  const win = $("#frameWindow");
  if (!canvas) return;

  // Toggle specified-mode class for edge handles
  canvas.classList.toggle("specified-mode", frameState.arMode === "specified");

  if (!selectedImage) {
    img.style.display = "none";
    placeholder.style.display = "";
    mask.style.display = "none";
    win.style.display = "none";
    return;
  }

  placeholder.style.display = "none";
  img.style.display = "";
  mask.style.display = "";
  win.style.display = "";

  // Load image if src changed
  const url = imagePreviewUrl(selectedImage.filename);
  if (img.src !== url && !img.src.endsWith(url)) {
    img.src = url;
  }

  const doLayout = () => {
    const canvasRect = canvas.getBoundingClientRect();
    const cW = canvasRect.width;
    const cH = canvasRect.height;
    if (cW === 0 || cH === 0) return;

    const ar = getEffectiveAR();
    const CANVAS_PAD_FRAC = 0.05; // visual breathing room inside the editor

    // Compute content frame size to fit within the editor.
    const availW = cW * (1 - 2 * CANVAS_PAD_FRAC);
    const availH = cH * (1 - 2 * CANVAS_PAD_FRAC);
    const dragProjection = frameDragState?.projection;
    let frameW, frameH;
    let frameL, frameT;
    if (dragProjection) {
      frameW = frameState.widthMm * dragProjection.pxPerMm;
      frameH = frameState.heightMm * dragProjection.pxPerMm;
      frameL = dragProjection.centerX - frameW / 2;
      frameT = dragProjection.centerY - frameH / 2;
    } else if (availW / availH > ar) {
      frameH = availH;
      frameW = frameH * ar;
      frameL = (cW - frameW) / 2;
      frameT = (cH - frameH) / 2;
    } else {
      frameW = availW;
      frameH = frameW / ar;
      frameL = (cW - frameW) / 2;
      frameT = (cH - frameH) / 2;
    }

    // Position frame window
    win.style.left = frameL + "px";
    win.style.top = frameT + "px";
    win.style.width = frameW + "px";
    win.style.height = frameH + "px";

    // Mask: use clip-path to cut out only the selected content frame.
    // There is no generated fill region in crop-only framing.
    mask.style.clipPath = `polygon(
      0% 0%, 100% 0%, 100% 100%, 0% 100%, 0% 0%,
      ${frameL}px ${frameT}px,
      ${frameL}px ${frameT + frameH}px,
      ${frameL + frameW}px ${frameT + frameH}px,
      ${frameL + frameW}px ${frameT}px,
      ${frameL}px ${frameT}px
    )`;

    // Position image: the crop model owns source-space crop semantics; the
    // editor only projects that model into CSS pixels.
    const rotation = frameState.rotation;
    const projection = frameDragState?.projection
      ? projectDragCropToFrame()
      : projectCropToFrame(frameL, frameT, frameW, frameH);
    const displayW = projection.displayW;
    const displayH = projection.displayH;
    const imgL = projection.imgL;
    const imgT = projection.imgT;

    img.style.left = imgL + "px";
    img.style.top = imgT + "px";
    img.style.width = displayW + "px";
    img.style.height = displayH + "px";
    const sx = frameState.flipH ? -1 : 1;
    const sy = frameState.flipV ? -1 : 1;
    img.style.transform = `rotate(${rotation}deg) scale(${sx}, ${sy})`;
    img.style.filter = buildAdjustFilterCSS();

    // Position edge handles — square handles at corners and edge midpoints
    const hs = 5; // half handle size
    canvas.querySelectorAll(".frame-edge").forEach((el) => {
      const edge = el.dataset.edge;
      // Edge midpoints
      if (edge === "n")  { el.style.left = (frameL + frameW / 2 - hs) + "px"; el.style.top = (frameT - hs) + "px"; }
      if (edge === "s")  { el.style.left = (frameL + frameW / 2 - hs) + "px"; el.style.top = (frameT + frameH - hs) + "px"; }
      if (edge === "e")  { el.style.left = (frameL + frameW - hs) + "px"; el.style.top = (frameT + frameH / 2 - hs) + "px"; }
      if (edge === "w")  { el.style.left = (frameL - hs) + "px"; el.style.top = (frameT + frameH / 2 - hs) + "px"; }
      // Corners
      if (edge === "nw") { el.style.left = (frameL - hs) + "px"; el.style.top = (frameT - hs) + "px"; }
      if (edge === "ne") { el.style.left = (frameL + frameW - hs) + "px"; el.style.top = (frameT - hs) + "px"; }
      if (edge === "sw") { el.style.left = (frameL - hs) + "px"; el.style.top = (frameT + frameH - hs) + "px"; }
      if (edge === "se") { el.style.left = (frameL + frameW - hs) + "px"; el.style.top = (frameT + frameH - hs) + "px"; }
    });

    // Store frame geometry for interaction handlers
    canvas._frameGeom = {
      frameL,
      frameT,
      frameW,
      frameH,
      cW,
      cH,
      imgScale: projection.imgScale,
      displayW,
      displayH,
      visualW: projection.visualW,
      visualH: projection.visualH,
      visualLeft: projection.visualLeft,
      visualTop: projection.visualTop,
      visualRight: projection.visualRight,
      visualBottom: projection.visualBottom,
      imgL,
      imgT,
      crop: projection.crop,
    };

    // Dimension annotations
    renderDimensionAnnotations(frameL, frameT, frameW, frameH);
  };

  if (img.complete && img.naturalWidth > 0) doLayout();
  else img.addEventListener("load", doLayout, { once: true });

  // Keep preview in sync
  renderPreview();
}

// ── Preview Pane (final framed result, updates live) ────────────────────────

function renderPreview() {
  const viewport = $("#previewViewport");
  const placeholder = $("#previewPlaceholder");
  const mat = $("#previewMat");
  const pImg = $("#previewImg");
  if (!viewport || !pImg) return;

  if (!selectedImage) {
    pImg.style.display = "none";
    if (placeholder) placeholder.style.display = "";
    return;
  }

  if (placeholder) placeholder.style.display = "none";
  pImg.style.display = "";

  const url = imagePreviewUrl(selectedImage.filename);
  if (pImg.src !== url && !pImg.src.endsWith(url)) {
    pImg.src = url;
  }

  const doPreview = () => {
    const vw = viewport.clientWidth;
    const vh = viewport.clientHeight;
    if (!vw || !vh) return;

    const ar = getEffectiveAR();
    const imgNatW = pImg.naturalWidth || selectedImage.width;
    const imgNatH = pImg.naturalHeight || selectedImage.height;

    // Fit total print footprint (image frame + border) within viewport
    const margin = 0.12;
    const availW = vw * (1 - margin * 2);
    const availH = vh * (1 - margin * 2);

    // Compute border ratio: border_mm / frame_width_mm gives relative border size
    const bwMm = (config.border && config.border_width_mm > 0) ? config.border_width_mm : 0;
    // Total footprint AR includes border: (W + 2*bw) / (H + 2*bw)
    const footW = frameState.widthMm + 2 * bwMm;
    const footH = frameState.heightMm + 2 * bwMm;
    const footAR = footW / footH;

    // Fit the full footprint within available space
    let totalW, totalH;
    if (availW / availH > footAR) {
      totalH = availH;
      totalW = totalH * footAR;
    } else {
      totalW = availW;
      totalH = totalW / footAR;
    }

    // Derive image frame and border sizes in pixels from the fitted total
    // Round to whole CSS pixels to avoid sub-pixel alignment shifts
    const borderPx = bwMm > 0 ? Math.max(2, Math.round(totalW * (bwMm / footW))) : 0;
    const frameW = Math.round(totalW) - 2 * borderPx;
    const frameH = Math.round(totalH) - 2 * borderPx;
    const borderEl = $("#previewBorder");

    // Mat = full print footprint (image frame + border on all sides)
    const matW = frameW + 2 * borderPx;
    const matH = frameH + 2 * borderPx;
    mat.style.width = matW + "px";
    mat.style.height = matH + "px";
    mat.style.overflow = "hidden";
    mat.style.position = "relative";
    mat.style.background = "#000";
    mat.style.outline = "1px solid rgba(255,255,255,0.2)";

    // Border overlay — white band around image frame, drawn inward from mat edge
    if (borderEl) {
      if (borderPx > 0) {
        borderEl.style.inset = "0";
        borderEl.style.borderWidth = borderPx + "px";
        borderEl.style.borderColor = "#fff";
        borderEl.style.borderStyle = "solid";
        borderEl.style.outline = "1px solid rgba(0,0,0,0.15)";
        borderEl.style.outlineOffset = "0px";
      } else {
        borderEl.style.inset = "0";
        borderEl.style.borderWidth = "0";
        borderEl.style.borderStyle = "none";
        borderEl.style.outline = "none";
      }
    }

    // Image transform within the frame (matching canvas logic)
    const rotation = frameState.rotation;

    const imageGeom = cropCoverImageGeometry(
      frameW,
      frameH,
      imgNatW,
      imgNatH,
      frameState.scale,
      rotation,
    );
    const displayW = imageGeom.displayW;
    const displayH = imageGeom.displayH;

    const slackX = Math.max(0, imageGeom.visualW - frameW);
    const slackY = Math.max(0, imageGeom.visualH - frameH);
    const offsetX = frameState.panX * slackX / 2;
    const offsetY = frameState.panY * slackY / 2;

    // Image positioned within the image frame area (inset by borderPx)
    const imgL = borderPx + frameW / 2 - displayW / 2 - offsetX;
    const imgT = borderPx + frameH / 2 - displayH / 2 - offsetY;

    pImg.style.position = "absolute";
    pImg.style.left = imgL + "px";
    pImg.style.top = imgT + "px";
    pImg.style.width = displayW + "px";
    pImg.style.height = displayH + "px";
    pImg.style.maxWidth = "none";
    pImg.style.maxHeight = "none";
    const sx = frameState.flipH ? -1 : 1;
    const sy = frameState.flipV ? -1 : 1;
    pImg.style.transform = `rotate(${rotation}deg) scale(${sx}, ${sy})`;
    pImg.style.transformOrigin = "center center";
    pImg.style.filter = buildAdjustFilterCSS();

    // Dimension annotations in preview
    const dimSvg = $("#previewDimensions");
    if (dimSvg) {
      const matRect = mat.getBoundingClientRect();
      const vpRect = viewport.getBoundingClientRect();
      // Mat is the full print footprint (image frame + border)
      const mL = matRect.left - vpRect.left;
      const mT = matRect.top - vpRect.top;
      const mW = matRect.width;
      const mH = matRect.height;

      const wMm = (frameState.widthMm + 2 * bwMm).toFixed(1);
      const hMm = (frameState.heightMm + 2 * bwMm).toFixed(1);
      const off = 20;  // offset from mat edge
      const cap = 6;   // end cap half-height
      const ah = 6;    // arrowhead size

      // Width: below mat — line with end caps and arrowheads
      const wy = mT + mH + off;
      const wxL = mL;
      const wxR = mL + mW;
      const wxMid = mL + mW / 2;

      // Height: right of mat
      const hx = mL + mW + off;
      const hyT = mT;
      const hyB = mT + mH;
      const hyMid = mT + mH / 2;

      const c = "rgba(255,255,255,0.7)";
      const tc = "rgba(255,255,255,0.9)";

      dimSvg.innerHTML = `
        <!-- Width dimension -->
        <line x1="${wxL}" y1="${wy}" x2="${wxR}" y2="${wy}" stroke="${c}" stroke-width="1"/>
        <line x1="${wxL}" y1="${wy-cap}" x2="${wxL}" y2="${wy+cap}" stroke="${c}" stroke-width="1"/>
        <line x1="${wxR}" y1="${wy-cap}" x2="${wxR}" y2="${wy+cap}" stroke="${c}" stroke-width="1"/>
        <polygon points="${wxL},${wy} ${wxL+ah},${wy-ah/2} ${wxL+ah},${wy+ah/2}" fill="${c}"/>
        <polygon points="${wxR},${wy} ${wxR-ah},${wy-ah/2} ${wxR-ah},${wy+ah/2}" fill="${c}"/>
        <text x="${wxMid}" y="${wy-6}" text-anchor="middle" fill="${tc}" font-size="12" font-family="Segoe UI,sans-serif" font-weight="600">${wMm} mm</text>
        <!-- Height dimension -->
        <line x1="${hx}" y1="${hyT}" x2="${hx}" y2="${hyB}" stroke="${c}" stroke-width="1"/>
        <line x1="${hx-cap}" y1="${hyT}" x2="${hx+cap}" y2="${hyT}" stroke="${c}" stroke-width="1"/>
        <line x1="${hx-cap}" y1="${hyB}" x2="${hx+cap}" y2="${hyB}" stroke="${c}" stroke-width="1"/>
        <polygon points="${hx},${hyT} ${hx-ah/2},${hyT+ah} ${hx+ah/2},${hyT+ah}" fill="${c}"/>
        <polygon points="${hx},${hyB} ${hx-ah/2},${hyB-ah} ${hx+ah/2},${hyB-ah}" fill="${c}"/>
        <text x="${hx}" y="${hyMid}" text-anchor="middle" fill="${tc}" font-size="12" font-family="Segoe UI,sans-serif" font-weight="600" transform="rotate(-90,${hx},${hyMid})" dy="-8">${hMm} mm</text>
      `;
    }

  };

  if (pImg.complete && pImg.naturalWidth > 0) {
    doPreview();
  } else {
    pImg.addEventListener("load", doPreview, { once: true });
  }

  // Also update border overlay even if image hasn't changed layout
  const borderEl2 = $("#previewBorder");
  if (borderEl2 && mat.style.width) {
    const fw = parseFloat(mat.style.width);
    if (config.border && config.border_width_mm > 0 && fw > 0) {
      const bPx = Math.max(2, (config.border_width_mm / frameState.widthMm) * fw);
      borderEl2.style.borderWidth = bPx + "px";
      borderEl2.style.borderColor = "#fff";
      borderEl2.style.borderStyle = "solid";
      borderEl2.style.outline = "1px solid rgba(0,0,0,0.15)";
      borderEl2.style.outlineOffset = "0px";
    } else {
      borderEl2.style.borderWidth = "0";
      borderEl2.style.borderStyle = "none";
      borderEl2.style.outline = "none";
    }
  }
}

// Sync width/height sliders to match frameState
function syncWidthSlider() {
  const sl = $("#widthSlider");
  if (sl) sl.value = frameState.widthMm;
}
function syncHeightSlider() {
  const sl = $("#heightSlider");
  if (sl) sl.value = frameState.heightMm;
}

// ── Dimension Annotations (engineering-drawing arrows on canvas) ────────────

function renderDimensionAnnotations(frameL, frameT, frameW, frameH) {
  const svg = $("#frameDimensions");
  if (!svg) return;
  if (frameW <= 0 || frameH <= 0) { svg.innerHTML = ""; return; }

  const wMm = frameState.widthMm.toFixed(1);
  const hMm = frameState.heightMm.toFixed(1);
  const off = 18; // offset from frame edge
  const ah = 4;   // arrowhead size

  // Width annotation: below frame
  const wy = frameT + frameH + off;
  const wxL = frameL;
  const wxR = frameL + frameW;
  const wxMid = frameL + frameW / 2;

  // Height annotation: right of frame
  const hx = frameL + frameW + off;
  const hyT = frameT;
  const hyB = frameT + frameH;
  const hyMid = frameT + frameH / 2;

  svg.innerHTML = `
    <!-- Width -->
    <line x1="${wxL}" y1="${wy}" x2="${wxR}" y2="${wy}" stroke="rgba(255,255,255,0.6)" stroke-width="1"/>
    <line x1="${wxL}" y1="${wy-ah}" x2="${wxL}" y2="${wy+ah}" stroke="rgba(255,255,255,0.6)" stroke-width="1"/>
    <line x1="${wxR}" y1="${wy-ah}" x2="${wxR}" y2="${wy+ah}" stroke="rgba(255,255,255,0.6)" stroke-width="1"/>
    <text x="${wxMid}" y="${wy+13}" text-anchor="middle" fill="rgba(255,255,255,0.85)" font-size="10" font-family="Segoe UI, sans-serif" font-weight="600">${wMm} mm</text>
    <!-- Height -->
    <line x1="${hx}" y1="${hyT}" x2="${hx}" y2="${hyB}" stroke="rgba(255,255,255,0.6)" stroke-width="1"/>
    <line x1="${hx-ah}" y1="${hyT}" x2="${hx+ah}" y2="${hyT}" stroke="rgba(255,255,255,0.6)" stroke-width="1"/>
    <line x1="${hx-ah}" y1="${hyB}" x2="${hx+ah}" y2="${hyB}" stroke="rgba(255,255,255,0.6)" stroke-width="1"/>
    <text x="${hx+13}" y="${hyMid+3}" text-anchor="middle" fill="rgba(255,255,255,0.85)" font-size="10" font-family="Segoe UI, sans-serif" font-weight="600" transform="rotate(90, ${hx+13}, ${hyMid+3})">${hMm} mm</text>
  `;
}

// ── Enhanced Slider Behavior ───────────────────────────────────────────────

function initEnhancedSlider(sliderEl, opts = {}) {
  const { center, onUpdate, snapThreshold = 0.03 } = opts;

  // Mark center position via CSS custom property (rendered by CSS)
  if (center !== undefined) {
    const min = parseFloat(sliderEl.min);
    const max = parseFloat(sliderEl.max);
    const pct = ((center - min) / (max - min)) * 100;
    sliderEl.style.setProperty("--center-pct", `${pct}%`);
    sliderEl.classList.add("has-center-tick");
  }

  // Magnetic snap to center on slider input (not scroll wheel)
  let fromWheel = false;

  sliderEl.addEventListener("input", () => {
    let val = parseFloat(sliderEl.value);
    if (!fromWheel && center !== undefined) {
      const range = parseFloat(sliderEl.max) - parseFloat(sliderEl.min);
      const snapRange = range * snapThreshold;
      if (Math.abs(val - center) < snapRange) {
        val = center;
        sliderEl.value = center;
      }
    }
    fromWheel = false;
    if (onUpdate) onUpdate(val);
  });

  // Scroll wheel: progressive increment, no snap
  sliderEl.addEventListener("wheel", (e) => {
    e.preventDefault();
    fromWheel = true;
    const step = parseFloat(sliderEl.step) || 1;
    const speed = Math.min(10, Math.max(1, Math.abs(e.deltaY) / 30));
    const delta = (e.deltaY > 0 ? -1 : 1) * step * speed;
    const min = parseFloat(sliderEl.min);
    const max = parseFloat(sliderEl.max);
    const newVal = Math.min(max, Math.max(min, parseFloat(sliderEl.value) + delta));
    sliderEl.value = newVal;
    if (onUpdate) onUpdate(newVal);
    // Fire input event so other handlers (from bindEvents) also react
    sliderEl.dispatchEvent(new Event("input", { bubbles: true }));
  }, { passive: false });
}

function initAllEnhancedSliders() {
  // Scale slider — center at 100%
  const scaleSlider = $("#scaleSlider");
  if (scaleSlider) initEnhancedSlider(scaleSlider, { center: 100 });

  // Rotation slider — center at 0°
  const rotSlider = $("#rotationSlider");
  if (rotSlider) initEnhancedSlider(rotSlider, { center: 0 });

  // Image adjustment sliders — center at 0
  const adjustSliders = [
    "adjustExposureSlider", "adjustContrastSlider",
    "adjustHighlightSlider", "adjustShadowSlider",
    "adjustSaturationSlider", "adjustTempSlider",
  ];
  adjustSliders.forEach(id => {
    const el = $(`#${id}`);
    if (el) initEnhancedSlider(el, { center: 0 });
  });

  // Tint sliders — hue has no center (0-360), strength has no center (0-1)
  const tintHueSlider = $("#adjustTintHueSlider");
  if (tintHueSlider) initEnhancedSlider(tintHueSlider, {});
  const tintStrSlider = $("#adjustTintStrengthSlider");
  if (tintStrSlider) initEnhancedSlider(tintStrSlider, {});

  // Width/Height sliders — scroll wheel support
  const widthSlider = $("#widthSlider");
  if (widthSlider) initEnhancedSlider(widthSlider, {
    onUpdate: (v) => {
      const oldW = frameState.widthMm, oldH = frameState.heightMm;
      frameState.widthMm = clampFrameWidth(v);
      lastTouchedDim = "width";
      if (frameState.arMode !== "specified") applyARToHeight();
      adjustScaleForFrameChange(oldW, oldH, frameState.widthMm, frameState.heightMm);
      syncDimFields();
      syncHeightSlider();
      renderFrameCanvas();
      updateInfoGrid();
    }
  });
  const heightSlider = $("#heightSlider");
  if (heightSlider) initEnhancedSlider(heightSlider, {
    onUpdate: (v) => {
      const oldW = frameState.widthMm, oldH = frameState.heightMm;
      frameState.heightMm = clampFrameHeight(v);
      lastTouchedDim = "height";
      if (frameState.arMode !== "specified") applyARToWidth();
      adjustScaleForFrameChange(oldW, oldH, frameState.widthMm, frameState.heightMm);
      syncDimFields();
      syncWidthSlider();
      renderFrameCanvas();
      updateInfoGrid();
    }
  });
}

function updateInfoGrid() {
  if (!selectedImage) {
    $("#infoOrigDims").textContent = "\u2014";
    $("#infoPrintSize").textContent = "\u2014";
    $("#infoSolvePx").textContent = "\u2014";
    return;
  }

  const w = selectedImage.width;
  const h = selectedImage.height;
  const pxSize = getCurrentSolvePitch();
  const printW = frameState.widthMm;
  const printH = frameState.heightMm;
  const solveW = Math.round(printW / pxSize);
  const solveH = Math.round(printH / pxSize);
  const totalPx = solveW * solveH;

  config.max_dim_mm = Math.max(printW, printH);

  // Print size = image frame + border if enabled
  const bw = (config.border && config.border_width_mm > 0) ? config.border_width_mm : 0;
  const lithW = printW + 2 * bw;
  const lithH = printH + 2 * bw;

  $("#infoOrigDims").textContent = `${w} \u00d7 ${h} px`;
  $("#infoPrintSize").textContent = `${lithW.toFixed(1)} \u00d7 ${lithH.toFixed(1)} mm`;
  $("#infoSolvePx").textContent = `${solveW} \u00d7 ${solveH} = ${totalPx.toLocaleString()} px`;
}

// ── Frame UI Sync Helpers ───────────────────────────────────────────────────

function adjustScaleForFrameChange(oldW, oldH, newW, newH) {
  // Kept as a compatibility hook for the existing event wiring. In crop-only
  // framing, physical dimension edits change output size/aspect; they should
  // not implicitly alter source crop zoom.
  frameState.scale = clamp(Number(frameState.scale) || 100, 100, 1000);
  syncScaleSlider();
}

function syncDimLockState() {
  const wSlider = $("#widthSlider");
  const hSlider = $("#heightSlider");
  const wInput = $("#outputWidthMm");
  const hInput = $("#outputHeightMm");
  if (wSlider) wSlider.disabled = widthLocked;
  if (hSlider) hSlider.disabled = heightLocked;
  if (wInput) wInput.disabled = widthLocked;
  if (hInput) hInput.disabled = heightLocked;
}

function syncDimFields() {
  const owInput = $("#outputWidthMm");
  const ohInput = $("#outputHeightMm");
  if (owInput) owInput.value = frameState.widthMm.toFixed(1);
  if (ohInput) ohInput.value = frameState.heightMm.toFixed(1);
}

function syncScaleSlider() {
  const slider = $("#scaleSlider");
  const input = $("#scaleInput");
  if (slider) slider.value = frameState.scale;
  if (input) input.value = Math.round(frameState.scale);
}

function syncRotationSlider() {
  const slider = $("#rotationSlider");
  const input = $("#rotationInput");
  if (slider) slider.value = frameState.rotation;
  if (input) input.value = frameState.rotation.toFixed(1);
}

function frameDimensionMin(axis) {
  const slider = axis === "height" ? $("#heightSlider") : $("#widthSlider");
  const parsed = slider ? parseFloat(slider.min) : NaN;
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 10;
}

function frameDimensionMax(axis) {
  const slider = axis === "height" ? $("#heightSlider") : $("#widthSlider");
  const parsed = slider ? parseFloat(slider.max) : NaN;
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 300;
}

function clampFrameWidth(value) {
  return clamp(Number(value) || frameDimensionMin("width"), frameDimensionMin("width"), frameDimensionMax("width"));
}

function clampFrameHeight(value) {
  return clamp(Number(value) || frameDimensionMin("height"), frameDimensionMin("height"), frameDimensionMax("height"));
}

function updateARButtons() {
  $$("#arButtonGroup .ar-button").forEach((btn) => {
    btn.classList.toggle("is-active", btn.dataset.ar === frameState.arMode);
    const ar = btn.dataset.ar;
    // Disable "Image" when no image is loaded
    if (ar === "image") btn.disabled = !selectedImage;
    // Update label to reflect direction
    if (ar && ar.includes(":") && ar !== "1:1") {
      const [a, b] = ar.split(":");
      btn.textContent = imageDirection === "portrait" ? `${b}:${a}` : `${a}:${b}`;
    }
  });
}

function applyARToHeight() {
  // Adjust height to match the current AR, keeping width as anchor
  const ar = getEffectiveAR();
  frameState.widthMm = clampFrameWidth(frameState.widthMm);
  let heightMm = frameState.widthMm / ar;
  if (heightMm > frameDimensionMax("height")) {
    heightMm = frameDimensionMax("height");
    frameState.widthMm = clampFrameWidth(heightMm * ar);
  }
  frameState.heightMm = Math.round(clampFrameHeight(heightMm) * 100) / 100;
}

function applyARToWidth() {
  // Adjust width to match the current AR, keeping height as anchor
  const ar = getEffectiveAR();
  frameState.heightMm = clampFrameHeight(frameState.heightMm);
  let widthMm = frameState.heightMm * ar;
  if (widthMm > frameDimensionMax("width")) {
    widthMm = frameDimensionMax("width");
    frameState.heightMm = clampFrameHeight(widthMm / ar);
  }
  frameState.widthMm = Math.round(clampFrameWidth(widthMm) * 100) / 100;
}

function applyARFromLastTouched() {
  if (lastTouchedDim === "height") applyARToWidth();
  else applyARToHeight();
}

// Stage 11: default the print frame to the source image's aspect ratio with the SHORT side
// pinned to 120 mm (clean derived sides for common ratios: 3:2→180, 4:3→160, 5:4→150, 1:1→120),
// long side derived. Used on a new-image selection so the frame stops defaulting to a square.
const IMAGE_ASPECT_SHORT_SIDE_MM = 120;
function applyImageAspectDefault() {
  if (!selectedImage) return;
  frameState.arMode = "image";
  const ar = getEffectiveAR(); // width / height for the selected image in "image" mode
  if (!(ar > 0) || !isFinite(ar)) return;
  if (ar >= 1) {
    // landscape or square: the height is the short side
    frameState.heightMm = clampFrameHeight(IMAGE_ASPECT_SHORT_SIDE_MM);
    frameState.widthMm = clampFrameWidth(roundFrameMm(IMAGE_ASPECT_SHORT_SIDE_MM * ar));
  } else {
    // portrait: the width is the short side
    frameState.widthMm = clampFrameWidth(IMAGE_ASPECT_SHORT_SIDE_MM);
    frameState.heightMm = clampFrameHeight(roundFrameMm(IMAGE_ASPECT_SHORT_SIDE_MM / ar));
  }
  lastTouchedDim = "width";
}

function setARMode(mode) {
  frameState.arMode = mode;
  if (mode !== "specified") {
    // Apply ratio to dimensions using last-touched anchor
    if (mode === "ratio") {
      // Will be applied after dialog confirm
    } else {
      applyARFromLastTouched();
    }
  }
  renderFrameCanvas();
  renderPreview();
  updateInfoGrid();
  syncDimFields();
  updateARButtons();
  syncConfigToServer();
}

// ── Frame Interaction ───────────────────────────────────────────────────────

function initFrameInteraction() {
  const canvas = $("#frameCanvas");
  if (!canvas) return;

  // Pan: drag on canvas
  canvas.addEventListener("mousedown", (e) => {
    if (!selectedImage) return;
    if (frameEditorTab === "image") return; // locked in Image mode
    // Don't intercept edge handle drags
    if (e.target.dataset?.edge) return;
    e.preventDefault();
    panDragState = {
      startX: e.clientX,
      startY: e.clientY,
      startPanX: frameState.panX,
      startPanY: frameState.panY,
    };
    canvas.style.cursor = "grabbing";
  });

  document.addEventListener("mousemove", (e) => {
    if (panDragState) {
      e.preventDefault();
      const geom = canvas._frameGeom;
      if (!geom) return;
      // Convert editor pixels through the source-space crop model.
      const crop = geom.crop;
      const sourceDx = (e.clientX - panDragState.startX) / Math.max(geom.imgScale, 1e-9);
      const sourceDy = (e.clientY - panDragState.startY) / Math.max(geom.imgScale, 1e-9);
      const dx = crop.slackX <= 1e-6 ? 0 : sourceDx / (crop.slackX / 2);
      const dy = crop.slackY <= 1e-6 ? 0 : sourceDy / (crop.slackY / 2);
      frameState.panX = crop.slackX <= 1e-6 ? 0 : clamp(panDragState.startPanX - dx, -1, 1);
      frameState.panY = crop.slackY <= 1e-6 ? 0 : clamp(panDragState.startPanY - dy, -1, 1);
      renderFrameCanvas();
  
    }
    if (frameDragState) {
      e.preventDefault();
      const geom = canvas._frameGeom;
      if (!geom) return;
      // Symmetric resize from center in Specified mode
      const dx = (e.clientX - frameDragState.startX);
      const dy = (e.clientY - frameDragState.startY);
      const edge = frameDragState.edge;
      const pxPerMm = frameDragState.projection.pxPerMm;

      let newW = frameDragState.startWMm;
      let newH = frameDragState.startHMm;

      // Symmetric: both sides move equally, so delta is doubled
      if (!widthLocked && (edge.includes("e") || edge.includes("w"))) newW = Math.max(10, frameDragState.startWMm + Math.abs(dx) * 2 / pxPerMm * Math.sign(edge.includes("e") ? dx : -dx));
      if (!heightLocked && (edge.includes("s") || edge.includes("n"))) newH = Math.max(10, frameDragState.startHMm + Math.abs(dy) * 2 / pxPerMm * Math.sign(edge.includes("s") ? dy : -dy));
      if (edge.length === 1 && (edge === "e" || edge === "w")) {
        newW = Math.min(newW, newH * frameDragState.projection.sourceAR);
      }
      if (edge.length === 1 && (edge === "n" || edge === "s")) {
        newH = Math.min(newH, newW / frameDragState.projection.sourceAR);
      }

      // Corner: resize both (unlocked axes only)
      if (edge.length === 2) {
        // Use both
      }

      const oldW = frameState.widthMm, oldH = frameState.heightMm;
      if (!widthLocked) frameState.widthMm = Math.round(newW * 100) / 100;
      if (!heightLocked) frameState.heightMm = Math.round(newH * 100) / 100;
      adjustScaleForFrameChange(oldW, oldH, frameState.widthMm, frameState.heightMm);
      finishFrameModelUpdate({ syncServer: false });
    }
  });

  document.addEventListener("mouseup", () => {
    if (panDragState) {
      panDragState = null;
      canvas.style.cursor = "";
      syncConfigToServer();
    }
    if (frameDragState) {
      frameDragState = null;
      canvas.style.cursor = "";
      syncConfigToServer();
    }
  });

  // Edge handles for Specified mode
  canvas.querySelectorAll(".frame-edge").forEach((el) => {
    el.addEventListener("mousedown", (e) => {
      if (frameState.arMode !== "specified" || !selectedImage || frameEditorTab === "image") return;
      e.preventDefault();
      e.stopPropagation();
      const geom = canvas._frameGeom;
      if (!geom) return;
      const bounds = getTransformedSourceBounds();
      const pxPerMm = geom.frameW / Math.max(frameState.widthMm, 1e-9);
      frameDragState = {
        edge: el.dataset.edge,
        startX: e.clientX,
        startY: e.clientY,
        startWMm: frameState.widthMm,
        startHMm: frameState.heightMm,
        projection: {
          pxPerMm,
          centerX: geom.frameL + geom.frameW / 2,
          centerY: geom.frameT + geom.frameH / 2,
          sourceAR: bounds.width / Math.max(bounds.height, 1e-9),
          imgScale: geom.imgScale,
          displayW: geom.displayW,
          displayH: geom.displayH,
          visualW: geom.visualW,
          visualH: geom.visualH,
          imgL: geom.imgL,
          imgT: geom.imgT,
          visualLeft: geom.visualLeft,
          visualTop: geom.visualTop,
          visualRight: geom.visualRight,
          visualBottom: geom.visualBottom,
        },
      };
      canvas.style.cursor = getComputedStyle(el).cursor;
    });
  });

  // Zoom: scroll wheel on canvas
  canvas.addEventListener("wheel", (e) => {
    if (!selectedImage) return;
    if (frameEditorTab === "image") return; // locked in Image mode
    e.preventDefault();
    const delta = e.deltaY > 0 ? -5 : 5; // scroll down = zoom out
    frameState.scale = clamp(frameState.scale + delta, 100, 1000);
    syncScaleSlider();
    renderFrameCanvas();

  }, { passive: false });

  // Resize observer
  const ro = new ResizeObserver(() => {
    if (selectedImage) renderFrameCanvas();
  });
  ro.observe(canvas);

  // Drag-drop images onto canvas
  canvas.addEventListener("dragover", (e) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
    canvas.classList.add("drag-target");
  });
  canvas.addEventListener("dragleave", () => {
    canvas.classList.remove("drag-target");
  });
  canvas.addEventListener("drop", (e) => {
    e.preventDefault();
    canvas.classList.remove("drag-target");
    try {
      const data = JSON.parse(e.dataTransfer.getData("application/json"));
      if (data.type === "image" && data.filename) {
        const isNewImage = data.filename !== selectedImage?.filename;
        if (isNewImage) {
          frameState.scale = 100.0;
          frameState.rotation = 0;
          frameState.panX = 0;
          frameState.panY = 0;
          frameState.flipH = false;
          frameState.flipV = false;
        }
        selectedImage = availableImages.find((i) => i.filename === data.filename) || null;
        if (selectedImage && isNewImage) applyImageAspectDefault();  // Stage 11: default to image aspect
        renderImageTab();
        updateRail();
      }
    } catch { /* ignore non-image drags */ }
  });
}

// ── Palette Tab — Auto-Suggest / Manual Builder / Deck ──────────────────────

function toggleCreationMode(mode) {
  creationMode = mode;
  const autoPanel = $("#panelAutoSuggest");
  const manualPanel = $("#panelManualBuilder");
  const layout = document.querySelector(".creation-layout");
  const deckPanel = $("#creationDeckPanel");
  const manualPalettePanel = $("#manualPalettePanel");
  const isAuto = mode === "auto";
  if (autoPanel) {
    autoPanel.classList.toggle("is-expanded", isAuto);
    autoPanel.hidden = !isAuto;
  }
  if (manualPanel) {
    manualPanel.classList.toggle("is-expanded", !isAuto);
    manualPanel.hidden = isAuto;
  }
  if (layout) layout.classList.toggle("is-manual-mode", !isAuto);
  if (deckPanel) deckPanel.hidden = !isAuto;
  if (manualPalettePanel) manualPalettePanel.hidden = isAuto;
  $$(".creation-mode-tabs .segmented-btn").forEach((btn) => {
    const active = btn.dataset.panel === mode;
    btn.classList.toggle("is-active", active);
    btn.setAttribute("aria-selected", active ? "true" : "false");
  });
  renderCreationTab();
}

function syncCreationSidePanelSizing() {
  const sourcePanel = creationMode === "manual" ? $("#panelManualBuilder") : $("#panelAutoSuggest");
  const sidePanel = creationMode === "manual" ? $("#manualPalettePanel") : $("#creationDeckPanel");
  if (!sourcePanel || !sidePanel || sidePanel.hidden) return;
  sidePanel.style.minHeight = "";
  const sourceHeight = Math.ceil(sourcePanel.getBoundingClientRect().height);
  if (sourceHeight > 0) sidePanel.style.minHeight = `${sourceHeight}px`;
}

const DECK_GENERATION_FIELD_MAP = [
  { configKey: "swap_improvement_threshold", paletteId: "paletteSwapThreshold", prop: "value" },
  { configKey: "force_all_tiers", paletteId: "paletteForceAllTiers", prop: "checked" },
];

function syncDeckGenerationSettingsUI(source = "settings") {
  for (const field of DECK_GENERATION_FIELD_MAP) {
    const el = $(`#${field.paletteId}`);
    if (!el) continue;
    if (source === "palette") {
      if (field.prop === "checked") {
        config[field.configKey] = !!el.checked;
      } else {
        const parsed = parseFloat(el.value);
        if (Number.isFinite(parsed)) config[field.configKey] = parsed;
      }
    } else {
      el[field.prop] = field.prop === "checked" ? !!config[field.configKey] : config[field.configKey];
    }
  }
}

function renderCreationTab() {
  const modeNote = $("#creationModeNote");
  if (modeNote) {
    modeNote.textContent = creationMode === "manual"
      ? "Select filaments to be included in the manual palette. The number chosen can exceed the available AMS slots, but will require swapping filaments mid-print."
      : "Set max colored filaments per load, extra color-load tiers, and suggestions to use. Choose the target that matches the Solve Mode you will use. Suggested Palettes need to be added to the Palette Deck before use in a Solve.";
  }

  syncDeckGenerationSettingsUI("settings");

  if (creationMode === "auto") {
    renderCandidateLibrary();   // may selectAllCandidates() on first render
    renderAmsPreview();
    updateSuggestSlotHint();
  } else {
    renderManualLibrary();
    renderManualAmsSlots();
  }

  // Update header chips (after renderCandidateLibrary so candidateSelection is populated)
  const profiled = allFilaments.filter(f => isGenerationEligibleFilament(f) && !getBaseCapIds().has(f.filament_id) && enabledFilaments.has(f.filament_id));
  const candidateChip = $("#candidateCountChip");
  if (candidateChip) candidateChip.textContent = `${candidateSelection.size}/${profiled.length}`;
  const manualChip = $("#manualCountChip");
  if (manualChip) manualChip.textContent = `${manualSlots.length}`;
  const capacityNote = $("#suggestCapacityNote");
  if (capacityNote) {
    capacityNote.textContent = suggestCapacityNote;
    capacityNote.hidden = !suggestCapacityNote;
  }

  renderDeckCards();
  syncCreationSidePanelSizing();
  updateLibraryFilterStatus();
}

// ── Auto-Suggest Mode ─────────────────────────────────────────────────────

function selectAllCandidates() {
  const profiled = allFilaments.filter(f => isGenerationEligibleFilament(f) && !getBaseCapIds().has(f.filament_id) && enabledFilaments.has(f.filament_id));
  candidateSelection = new Set(profiled.map(f => f.filament_id));
}

function renderCandidateLibrary() {
  const grid = $("#candidateGrid");
  if (!grid) return;

  const libraryFils = allFilaments.filter(
    f => isGenerationEligibleFilament(f) && !getBaseCapIds().has(f.filament_id) && enabledFilaments.has(f.filament_id)
  );

  // Auto-select all on first render only
  if (!candidateInitialized && libraryFils.length > 0) {
    candidateInitialized = true;
    selectAllCandidates();
  }

  // Group by manufacturer
  const groups = new Map();
  for (const fil of libraryFils) {
    const mfg = fil.manufacturer || "Other";
    if (!groups.has(mfg)) groups.set(mfg, []);
    groups.get(mfg).push(fil);
  }

  let html = "";
  for (const [mfg, fils] of groups) {
    html += `<div class="library-group-header">${esc(mfg)}</div>`;
    for (const fil of fils) {
      const selected = candidateSelection.has(fil.filament_id);
      const stateClass = selected ? "is-candidate" : "is-deselected-candidate";
      const textCol = textColorForHex(fil.hex);
      html += `<div class="filament-card ${stateClass}" data-filament-id="${fil.filament_id}">
        <div class="filament-swatch" style="background:${fil.hex};color:${textCol}"></div>
        <div class="filament-copy"><div class="filament-detail">${esc(fil.color_name)}</div></div>
      </div>`;
    }
  }
  grid.innerHTML = html;

  // Bind toggle clicks
  grid.querySelectorAll(".filament-card").forEach(card => {
    card.addEventListener("click", () => {
      const fid = card.dataset.filamentId;
      if (candidateSelection.has(fid)) {
        candidateSelection.delete(fid);
      } else {
        candidateSelection.add(fid);
      }
      renderCreationTab();
    });
  });
}

function renderAmsPreview() {
  const container = $("#amsPreview");
  if (!container) return;

  const maxColors = parseInt($("#targetFilamentCount")?.value) || 7;
  const totalSlots = printerConfig.ams_slots || 4;
  const whiteSlots = getBaseCapSlots();
  const colorSlots = totalSlots - whiteSlots;
  const filledCount = Math.min(maxColors, colorSlots);
  const units = printerConfig.ams_units || 1;
  const slotsPerUnit = printerConfig.slots_per_unit || 4;

  let html = "";
  let slotIdx = 0;

  for (let u = 0; u < units; u++) {
    html += `<div class="ams-preview-unit-label">AMS ${u + 1}</div>`;
    html += `<div class="ams-preview-slots">`;
    for (let s = 0; s < slotsPerUnit; s++) {
      if (slotIdx < whiteSlots) {
        html += `<div class="ams-preview-slot is-white"><span class="ams-preview-base-label"><span>BASE/</span><span>CAP</span></span></div>`;
      } else if (slotIdx < whiteSlots + filledCount) {
        html += `<div class="ams-preview-slot is-filled"><span class="ams-lozenge"></span></div>`;
      } else {
        html += `<div class="ams-preview-slot"><span class="ams-lozenge"></span></div>`;
      }
      slotIdx++;
    }
    html += `</div>`;
  }
  html += `<div class="ams-preview-status">${filledCount} / ${colorSlots} color slots</div>`;
  container.innerHTML = html;
}

// ── Manual Builder Mode ───────────────────────────────────────────────────

function renderManualLibrary() {
  const grid = $("#manualLibraryGrid");
  const chip = $("#manualLibraryCountChip");
  if (!grid) return;

  const libraryFils = allFilaments.filter(
    f => isGenerationEligibleFilament(f) && !getBaseCapIds().has(f.filament_id) && enabledFilaments.has(f.filament_id)
  );
  const availableCount = libraryFils.filter(f => !manualSlots.includes(f.filament_id)).length;
  if (chip) chip.textContent = availableCount;

  const groups = new Map();
  for (const fil of libraryFils) {
    const mfg = fil.manufacturer || "Other";
    if (!groups.has(mfg)) groups.set(mfg, []);
    groups.get(mfg).push(fil);
  }

  let html = "";
  for (const [mfg, fils] of groups) {
    html += `<div class="library-group-header">${esc(mfg)}</div>`;
    for (const fil of fils) {
      const placed = manualSlots.includes(fil.filament_id);
      const stateClass = placed ? "is-placed" : "";
      const textCol = textColorForHex(fil.hex);
      html += `<div class="filament-card ${stateClass}" data-filament-id="${fil.filament_id}">
        <div class="filament-swatch" style="background:${fil.hex};color:${textCol}"></div>
        <div class="filament-copy"><div class="filament-detail">${esc(fil.color_name)}</div></div>
      </div>`;
    }
  }
  grid.innerHTML = html;

  grid.querySelectorAll(".filament-card:not(.is-placed)").forEach(card => {
    card.addEventListener("click", () => {
      const fid = card.dataset.filamentId;
      if (!manualSlots.includes(fid)) {
        manualSlots.push(fid);
        renderCreationTab();
      }
    });
  });
}

function renderManualAmsSlots() {
  const container = $("#manualAmsSlots");
  const statusEl = $("#manualAmsStatus");
  const mintBtn = $("#mintPaletteBtn");
  if (!container) return;

  const units = printerConfig.ams_units || 1;
  const slotsPerUnit = printerConfig.slots_per_unit || 4;
  const totalSlots = printerConfig.ams_slots || 4;
  const whiteSlots = getBaseCapSlots();
  const colorSlots = totalSlots - whiteSlots;

  // Build white slot(s)
  const whiteHtml = [];
  const baseFil = filamentById(getBaseFilament());
  const baseHex = baseFil?.hex || "#f5f0e0";
  const baseName = baseFil?.color_name || getBaseFilament();
  whiteHtml.push(`<div class="ams-slot is-white">
    <span class="ams-slot-swatch" style="background:${baseHex};border:1px solid #ddd;"></span>
    <span class="ams-slot-name">${esc(baseName)}</span>
    <span class="ams-slot-label">BASE/CAP</span>
  </div>`);

  // Partition manual slots into AMS-fits and swaps
  const amsFilaments = manualSlots.slice(0, colorSlots);
  const swapFilaments = manualSlots.slice(colorSlots);

  let html = "";
  let colorIdx = 0;

  for (let u = 0; u < units; u++) {
    html += `<div class="ams-unit-label">AMS ${u + 1}</div>`;
    for (let s = 0; s < slotsPerUnit; s++) {
      const globalSlot = u * slotsPerUnit + s;
      if (globalSlot < whiteSlots) {
        html += whiteHtml[globalSlot];
      } else if (colorIdx < amsFilaments.length) {
        const fid = amsFilaments[colorIdx];
        const fil = filamentById(fid);
        html += `<div class="ams-slot" data-filament-id="${fid}">
          <span class="ams-slot-swatch" style="background:${fil?.hex || '#ccc'}"></span>
          <span class="ams-slot-name">${esc(fil?.color_name || fid)}</span>
          <span class="ams-slot-remove" data-remove="${fid}" aria-label="Remove filament" title="Remove filament">${xIconSvg()}</span>
        </div>`;
        colorIdx++;
      } else {
        html += `<div class="ams-slot is-empty"></div>`;
      }
    }
  }

  // Swap overflow
  if (swapFilaments.length > 0) {
    html += `<div class="ams-swap-label">&#9888; SWAP (${swapFilaments.length})</div>`;
    for (const fid of swapFilaments) {
      const fil = filamentById(fid);
      html += `<div class="ams-slot is-swap" data-filament-id="${fid}">
        <span class="ams-slot-swatch" style="background:${fil?.hex || '#ccc'}"></span>
        <span class="ams-slot-name">${esc(fil?.color_name || fid)}</span>
        <span class="ams-slot-remove" data-remove="${fid}" aria-label="Remove filament" title="Remove filament">${xIconSvg()}</span>
      </div>`;
    }
  }

  container.innerHTML = html;

  // Bind remove buttons
  container.querySelectorAll(".ams-slot-remove").forEach(btn => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const fid = btn.dataset.remove;
      manualSlots = manualSlots.filter(id => id !== fid);
      renderCreationTab();
    });
  });

  // Status
  const swapText = swapFilaments.length > 0 ? ` + ${swapFilaments.length} swap` : "";
  if (statusEl) statusEl.textContent = `${amsFilaments.length} / ${colorSlots} slots${swapText}`;
  if (mintBtn) mintBtn.disabled = manualSlots.length === 0;
}

function clearManualSlots() {
  manualSlots = [];
  renderCreationTab();
}

// ── Shared lightbox ────────────────────────────────────────────────────────
let _lightboxIdx = -1; // which comparison result is enlarged, -1 = closed

function closeCompLightbox() {
  _lightboxIdx = -1;
  _solveLightboxState = null;
  if (_lightboxCleanup) _lightboxCleanup();
  const lb = $("#compLightbox");
  if (lb) lb.classList.add("is-hidden");
}

function navigateSolveLightbox(key) {
  if (!_solveLightboxState) return false;
  const selectedRuns = getSelectedSolveRunsWithResults();
  if (!selectedRuns.length) return false;

  if (_solveLightboxState.kind === "source") {
    if (key !== "ArrowRight") return false;
    openSolvePreviewLightboxForRun(
      selectedRuns[0],
      _solveLightboxState.view || solveView,
      _solveLightboxState.targetKind || "run",
    );
    return true;
  }

  if (_solveLightboxState.kind === "thickness") {
    const runIndex = selectedRuns.findIndex(r => r.id === _solveLightboxState.runId);
    if (runIndex < 0) return false;
    const run = selectedRuns[runIndex];
    const items = getSolveThicknessItems(run);
    if (!items.length) return false;

    if (key === "ArrowRight") {
      if (_solveLightboxState.mapIndex < items.length - 1) {
        openThicknessLightboxForKey(run.id, items[_solveLightboxState.mapIndex + 1].key);
        return true;
      }
      return false;
    }
    if (key === "ArrowLeft") {
      if (_solveLightboxState.mapIndex > 0) {
        openThicknessLightboxForKey(run.id, items[_solveLightboxState.mapIndex - 1].key);
        return true;
      }
      return false;
    }
    if (key === "ArrowDown") {
      if (runIndex < selectedRuns.length - 1) {
        const nextRun = selectedRuns[runIndex + 1];
        if (getSolveThicknessItems(nextRun).some(item => item.key === _solveLightboxState.mapKey)) {
          openThicknessLightboxForKey(nextRun.id, _solveLightboxState.mapKey);
          return true;
        }
      }
      return false;
    }
    if (key === "ArrowUp") {
      if (runIndex > 0) {
        const nextRun = selectedRuns[runIndex - 1];
        if (getSolveThicknessItems(nextRun).some(item => item.key === _solveLightboxState.mapKey)) {
          openThicknessLightboxForKey(nextRun.id, _solveLightboxState.mapKey);
          return true;
        }
      }
      return false;
    }
    return false;
  }

  if (_solveLightboxState.kind === "solve" || _solveLightboxState.kind === "surface" || _solveLightboxState.kind === "recipe") {
    if (key !== "ArrowRight" && key !== "ArrowLeft") return false;
    const runIndex = selectedRuns.findIndex(r => r.id === _solveLightboxState.runId);
    if (runIndex < 0) return false;
    const delta = key === "ArrowRight" ? 1 : -1;
    const nextIndex = runIndex + delta;
    if (nextIndex < 0) {
      if (key === "ArrowLeft") {
        const sourceView = _solveLightboxState.kind === "surface"
          ? _solveLightboxState.viewType
          : (_solveLightboxState.kind === "recipe" ? "color_ceiling" : _solveLightboxState.view);
        const sourceTargetKind = _solveLightboxState.kind === "surface"
          ? "surface"
          : (_solveLightboxState.kind === "recipe" ? "recipe" : "run");
        if (shouldShowSolveSourceColumn(sourceView)) {
          openSolveSourceLightbox(selectedRuns[0], sourceView, sourceTargetKind);
          return true;
        }
      }
      return false;
    }
    if (nextIndex >= selectedRuns.length) return false;
    const nextRun = selectedRuns[nextIndex];
    if (_solveLightboxState.kind === "surface") {
      openSurfaceLightbox(_solveLightboxState.viewType, nextRun.id);
    } else if (_solveLightboxState.kind === "recipe") {
      openRecipeLightbox(nextRun.id);
    } else {
      openSolveRunLightbox(nextRun.id, _solveLightboxState.view);
    }
    return true;
  }

  return false;
}

function updateLibraryFilterStatus() {
  const eligibleIds = new Set(getGenerationEligibleFilamentIds());
  const enabledCount = [...enabledFilaments].filter(fid => eligibleIds.has(fid)).length;
  const totalEligible = eligibleIds.size;
  const label = `${enabledCount} / ${totalEligible} enabled`;
  const statusEl = $("#libraryFilterStatus");
  if (statusEl) statusEl.textContent = label;
  const railCount = $("#railLibraryCount");
  if (railCount) railCount.textContent = `(${enabledCount}/${totalEligible})`;
}

async function handleOpenImageLibraryFolder() {
  try {
    await openImagesFolder();
    showToast("Opened Images folder", "success");
  } catch (err) {
    showToast(`The Images folder could not be opened: ${err.message}`, "error");
  }
}

function normalizeSupportFromLoadedConfig(cfg = {}) {
  const base = cfg.base_filament || cfg.white_base || DEFAULT_BASE_FILAMENT;
  return {
    base,
    capEffective: base,
    capSelector: "__same__",
    base_filament: base,
    cap_filament: "__same__",
    white_base: base,
    white_cap: null,
  };
}

function normalizeSupportFromPaletteRecord(record = {}, { requireExplicit = false } = {}) {
  const hasBase = Boolean(record.base_filament || record.white_base);
  const hasCap = Object.prototype.hasOwnProperty.call(record, "cap_filament") ||
    Object.prototype.hasOwnProperty.call(record, "white_cap");
  if (requireExplicit && (!hasBase || !hasCap)) return null;
  if (!hasBase && !hasCap) return null;
  return normalizeSupportFromLoadedConfig(record);
}

function selectLoadedPalette(body = {}, cfg = body.config || {}) {
  const filterPalette = (items) => Array.isArray(items)
    ? items.filter(id => typeof id === "string" && id.trim())
    : [];
  const bodyPalette = filterPalette(body.palette);
  if (bodyPalette.length) return bodyPalette;
  return filterPalette(cfg.palette);
}

function getPaletteGatingIssues(filamentIds) {
  const issues = { missing: [], unavailable: [], disabled: [] };
  const seen = new Set();
  for (const filamentId of Array.isArray(filamentIds) ? filamentIds : []) {
    if (typeof filamentId !== "string" || !filamentId.trim() || seen.has(filamentId)) continue;
    seen.add(filamentId);
    const filament = filamentById(filamentId);
    if (!filament) issues.missing.push(filamentId);
    else if (!isGenerationEligibleFilament(filament)) issues.unavailable.push(filamentId);
    else if (!enabledFilaments.has(filamentId)) issues.disabled.push(filamentId);
  }
  return issues;
}

function paletteGatingIssueCount(issues) {
  return (issues?.missing?.length || 0)
    + (issues?.unavailable?.length || 0)
    + (issues?.disabled?.length || 0);
}

function buildPaletteGatingMessage(issues, prefix = "Can't use this palette.") {
  const parts = [];
  if (issues.missing.length) {
    parts.push(`${issues.missing.map(solveFilamentLabel).join(", ")} ${issues.missing.length === 1 ? "is" : "are"} not present in the active model library.`);
  }
  if (issues.unavailable.length) {
    parts.push(`${issues.unavailable.map(solveFilamentLabel).join(", ")} ${issues.unavailable.length === 1 ? "is" : "are"} unavailable for generation in the active model library.`);
  }
  if (issues.disabled.length) {
    parts.push(`${issues.disabled.map(solveFilamentLabel).join(", ")} ${issues.disabled.length === 1 ? "is" : "are"} disabled in Manage Filaments. Enable ${issues.disabled.length === 1 ? "it" : "them"} before continuing.`);
  }
  return `${prefix} ${parts.join(" ")}`.trim();
}

function makePaletteSignature(filamentIds, support) {
  const ids = Array.isArray(filamentIds)
    ? filamentIds.filter(id => typeof id === "string" && id.trim())
    : [];
  const normalizedSupport = support || normalizeSupportFromLoadedConfig({});
  return {
    filamentIds: [...ids],
    base: normalizedSupport.base,
    capEffective: normalizedSupport.capEffective,
  };
}

function paletteSignaturesEqual(a, b) {
  if (!a || !b) return false;
  if (a.base !== b.base || a.capEffective !== b.capEffective) return false;
  if (a.filamentIds.length !== b.filamentIds.length) return false;
  return a.filamentIds.every((id, idx) => id === b.filamentIds[idx]);
}

function signatureForPaletteRecord(record, inheritedSupport) {
  const explicitSupport = normalizeSupportFromPaletteRecord(record);
  return makePaletteSignature(record?.filament_ids || [], explicitSupport || inheritedSupport);
}

function findMatchingDeckCard(signature, deckCards = deck) {
  return (deckCards || []).find(card => paletteSignaturesEqual(
    signature,
    signatureForPaletteRecord(card, {
      base: signature.base,
      capEffective: signature.capEffective,
    }),
  )) || null;
}

function findMatchingSavedPaletteIndex(signature, savedPalettes = savedPalettesData?.palettes || []) {
  return (savedPalettes || []).findIndex(record => paletteSignaturesEqual(
    signature,
    signatureForPaletteRecord(record, {
      base: signature.base,
      capEffective: signature.capEffective,
    }),
  ));
}

function chooseLoadedPaletteRestoreAction({ filamentIds, support, deckCards = deck, savedPalettes = savedPalettesData?.palettes || [] }) {
  const signature = makePaletteSignature(filamentIds, support);
  if (!signature.filamentIds.length) return { kind: "none" };
  const existing = findMatchingDeckCard(signature, deckCards);
  if (existing) return { kind: "reuse-deck", cardId: existing.id };
  const savedIndex = findMatchingSavedPaletteIndex(signature, savedPalettes);
  if (savedIndex >= 0) return { kind: "load-saved", savedIndex };
  return { kind: "add-ad-hoc", filamentIds: [...signature.filamentIds] };
}

function createPaletteDeckCard({ idPrefix = "deck", name, filamentIds, saved = false }) {
  let suffix = 0;
  let id = `${idPrefix}-${Date.now()}`;
  while (deck.some(card => card.id === id) || stagingDeck.some(card => card.id === id)) {
    suffix += 1;
    id = `${idPrefix}-${Date.now()}-${suffix}`;
  }
  return {
    id,
    name,
    filament_ids: [...filamentIds],
    gamut: null,
    saved,
  };
}

function addLoadedAdHocPaletteToDeck(filamentIds, label = "Loaded run palette") {
  const card = createPaletteDeckCard({
    idPrefix: "loaded-run",
    name: label,
    filamentIds,
    saved: false,
  });
  deck.push(card);
  activateDeckCard(card.id, { sync: false });
  return card;
}

function mintPaletteToDeck() {
  const filaments = creationMode === "manual" ? manualSlots : composerPalette;
  if (filaments.length === 0) return;
  const card = createPaletteDeckCard({
    idPrefix: creationMode === "manual" ? "manual" : "deck",
    name: "Palette " + nextDeckNum++,
    filamentIds: filaments,
    saved: false,
  });
  if (creationMode === "manual") {
    deck.push(card);
    if (!activeDeckId) activeDeckId = card.id;
    manualSlots = [];
    syncConfigToServer();
    showToast(`Added "${card.name}" to the deck`, "success");
  } else {
    stagingDeck.push(card);
    composerPalette = [];
    showToast(`Staged "${card.name}" with ${card.filament_ids.length} filaments`, "success");
  }
  renderCreationTab();
  updateRail();
}

function activateDeckCard(cardId, { sync = true } = {}) {
  activeDeckId = cardId;
  renderDeckCards();
  updateRail();
  if (sync) syncConfigToServer();
}

function setActiveDeckCard(cardId) {
  activateDeckCard(cardId);
}

async function removeDeckCard(cardId) {
  deck = deck.filter(d => d.id !== cardId);
  if (activeDeckId === cardId) {
    activeDeckId = deck.length > 0 ? deck[0].id : null;
  }
  renderDeckCards();
  updateRail();
  syncConfigToServer();
}

// Move a staged card from the staging pad into the persistent deck (leaves the pad).
function promoteStagedCard(cardId) {
  const idx = stagingDeck.findIndex(c => c.id === cardId);
  if (idx < 0) return;
  const [card] = stagingDeck.splice(idx, 1);
  deck.push(card);
  // Activate only if it becomes the sole persistent card (matches mint/load); never steals
  // an existing active selection.
  if (deck.length === 1) activeDeckId = card.id;
  renderDeckCards();
  updateRail();
  syncConfigToServer();
  showToast(`Promoted "${card.name}" to the deck`, "success");
}

function removeStagingCard(cardId) {
  stagingDeck = stagingDeck.filter(c => c.id !== cardId);
  renderDeckCards();
}

async function saveDeckCard(cardId) {
  const card = deck.find(d => d.id === cardId);
  if (!card) return;
  const alias = await showPaletteSaveModal(card.name);
  if (alias === null) return; // cancelled
  card.name = alias || card.name;
  card.saved = true;

  // Persist to server
  if (!savedPalettesData) savedPalettesData = { palettes: [] };
  // Update or add
  const existing = savedPalettesData.palettes.findIndex(p => p.id === card.id);
  const entry = { id: card.id, name: card.name, filament_ids: card.filament_ids };
  if (existing >= 0) {
    savedPalettesData.palettes[existing] = entry;
  } else {
    savedPalettesData.palettes.push(entry);
  }
  try {
    await savePalettesToServer(savedPalettesData);
    showToast(`Saved "${card.name}"`, "success");
  } catch (err) {
    showToast(`Save failed: ${err.message}`, "error");
  }
  renderDeckCards();
}

async function loadSavedPalettes() {
  if (!apiConnected) return;
  try {
    savedPalettesData = await fetchSavedPalettes();
  } catch { savedPalettesData = { palettes: [] }; }
}

function showLoadPaletteMenu(anchorBtn = null) {
  if (!savedPalettesData || savedPalettesData.palettes.length === 0) {
    showToast("No saved palettes to load", "error");
    return;
  }
  const btn = anchorBtn || $("#railLoadPaletteBtn");
  if (!btn) return;

  // Remove any existing popover
  const old = document.querySelector(".load-palette-popover");
  if (old) {
    const sameAnchor = old.dataset.anchorId && btn.id && old.dataset.anchorId === btn.id;
    old.remove();
    if (sameAnchor) return;
  }

  const pop = document.createElement("div");
  pop.className = "load-palette-popover surface-menu";
  if (btn.id) pop.dataset.anchorId = btn.id;
  const isRailAnchor = btn.id === "railLoadPaletteBtn";
  if (isRailAnchor) {
    pop.classList.add("is-rail-popout");
    const rect = btn.getBoundingClientRect();
    pop.style.position = "fixed";
    pop.style.top = `${Math.round(rect.top)}px`;
    pop.style.left = `${Math.round(rect.right + 10)}px`;
  }
  function renderPopoverItems() {
    if (!savedPalettesData || savedPalettesData.palettes.length === 0) {
      pop.remove();
      return;
    }
    pop.innerHTML = savedPalettesData.palettes.map((p, i) => {
      const chips = p.filament_ids.map(fid => {
        const fil = filamentById(fid);
        return `<span class="color-chip" style="background:${fil?.hex || '#ccc'};width:8px;height:12px;border-radius:2px;display:inline-block"></span>`;
      }).join("");
      return `<div class="load-palette-item surface-menu-item" data-index="${i}">
        <span class="load-palette-name">${esc(p.name)}</span>
        <span class="load-palette-chips">${chips}</span>
        <span class="load-palette-delete" data-index="${i}" title="Delete saved palette" aria-label="Delete saved palette">${xIconSvg()}</span>
      </div>`;
    }).join("");
    bindPopoverItems();
  }

  function bindPopoverItems() {
    pop.querySelectorAll(".load-palette-item").forEach(item => {
      item.addEventListener("click", (e) => {
        if (e.target.closest(".load-palette-delete")) return;
        pop.remove();
        loadPaletteByIndex(parseInt(item.dataset.index));
      });
    });
    pop.querySelectorAll(".load-palette-delete").forEach(delBtn => {
      let confirmPending = false;
      delBtn.addEventListener("click", async (e) => {
        e.stopPropagation();
        if (confirmPending) {
          const idx = parseInt(delBtn.dataset.index);
          const name = savedPalettesData.palettes[idx]?.name;
          savedPalettesData.palettes.splice(idx, 1);
          try { await savePalettesToServer(savedPalettesData); } catch { /* ignore */ }
          showToast(`Deleted "${name}"`, "success");
          renderPopoverItems();
          return;
        }
        confirmPending = true;
        delBtn.textContent = "Del?";
        delBtn.classList.add("confirm-pending");
        setTimeout(() => {
          confirmPending = false;
          delBtn.innerHTML = xIconSvg();
          delBtn.classList.remove("confirm-pending");
        }, 2000);
      });
    });
  }

  renderPopoverItems();
  const mount = isRailAnchor ? document.body : btn.parentElement;
  if (!mount) return;
  if (!isRailAnchor) mount.style.position = "relative";
  mount.appendChild(pop);
  // Close on outside click
  setTimeout(() => {
    document.addEventListener("click", function closer(e) {
      if (!pop.contains(e.target) && e.target !== btn) {
        pop.remove();
        document.removeEventListener("click", closer);
      }
    });
  }, 0);
}

function loadPaletteByIndex(idx, { forceActive = true, sync = true, silent = false, allowUnavailable = false } = {}) {
  const saved = savedPalettesData.palettes[idx];
  if (!saved) return null;
  const gatingIssues = getPaletteGatingIssues(saved.filament_ids);
  if (!allowUnavailable && paletteGatingIssueCount(gatingIssues)) {
    showToast(buildPaletteGatingMessage(gatingIssues, `Can't load “${saved.name}”.`), "error");
    return null;
  }
  const card = createPaletteDeckCard({
    idPrefix: "loaded",
    name: saved.name,
    filamentIds: saved.filament_ids,
    saved: true,
  });
  deck.push(card);
  if (forceActive || deck.length === 1) activeDeckId = card.id;
  renderCreationTab();
  updateRail();
  if (sync) syncConfigToServer();
  if (!silent) showToast(`Loaded "${card.name}"`, "success");
  return card;
}

function renderDeckCards() {
  const container = $("#deckCards");
  if (stagingDeck.length === 0) {
    container.innerHTML = `<p class="muted-line palette-empty-msg">Auto-suggest palettes to stage them here</p>`;
    renderRailDeck();
    return;
  }

  container.innerHTML = stagingDeck.map((card) => {
    const chips = card.filament_ids.map((fid) => {
      const fil = filamentById(fid);
      return `<span class="color-chip" style="background:${fil?.hex || '#ccc'}" title="${escAttr(fil?.color_name || fid)}"></span>`;
    }).join("");
    const supportChips = buildDeckSupportChipsHtml();
    let gamutHtml = "";
    if (card.gamut?.status === "checking") {
      const pct = card.gamut.pct != null ? ` · ${Math.round(card.gamut.pct)}%` : "";
      const elapsed = card.gamut.elapsed_s != null ? ` · ${Math.round(card.gamut.elapsed_s)}s` : "";
      gamutHtml = `<div class="deck-card-gamut"><span class="muted-line">${esc(card.gamut.progress || "Checking gamut...")}${pct}${elapsed}</span></div>`;
    } else if (card.gamut?.status === "done") {
      const g = card.gamut;
      const oogPart = g.total_pixels > 0 ? ` &middot; ${g.n_out_of_gamut.toLocaleString()} OOG` : "";
      gamutHtml = `
        <div class="deck-card-gamut">
          <span><strong>${formatColorRmse(g)}</strong>${oogPart}</span>
        </div>
      `;
    } else if (card.gamut?.status === "error") {
      gamutHtml = `<div class="deck-card-gamut" style="color:var(--error-ink)"><span>Gamut check failed</span></div>`;
    }

    return `
      <div class="deck-card" data-card-id="${card.id}">
        <div class="deck-card-header">
          <span class="deck-card-title" title="${escAttr(card.name)}">${esc(card.name)} (${card.filament_ids.length})</span>
          <div class="deck-card-actions">
            <button class="ghost-button xxs deck-promote-btn" data-card-id="${card.id}" title="Add this palette to the palette deck">Add to Deck</button>
            <button class="ghost-button xxs danger deck-delete-btn" data-card-id="${card.id}" aria-label="Remove staged palette" title="Remove staged palette">${xIconSvg()}</button>
          </div>
        </div>
        <div class="deck-card-chips">
          <div class="deck-card-palette-chips">${chips}</div>
          ${supportChips}
        </div>
        ${gamutHtml}
      </div>
    `;
  }).join("");

  // Staging-pad card actions: Promote (move to persistent deck) and remove. Staging cards are
  // never active and have no Save — Save lives on the persistent rail deck.
  container.querySelectorAll(".deck-promote-btn").forEach((btn) => {
    btn.addEventListener("click", (e) => { e.stopPropagation(); promoteStagedCard(btn.dataset.cardId); });
  });
  container.querySelectorAll(".deck-delete-btn").forEach((btn) => {
    let confirmPending = false;
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      if (confirmPending) {
        removeStagingCard(btn.dataset.cardId);
        return;
      }
      confirmPending = true;
      btn.textContent = "Remove?";
      btn.classList.add("confirm-pending");
      setTimeout(() => {
        confirmPending = false;
        btn.innerHTML = xIconSvg();
        btn.classList.remove("confirm-pending");
      }, 2000);
    });
  });
  renderRailDeck();
}

function renderRailDeck() {
  hideRailDeckHoverPreview();
  const list = $("#railDeckList");
  if (!list) return;
  if (deck.length === 0) {
    list.innerHTML = `<span class="rail-deck-empty">No palettes yet</span>`;
    return;
  }
  list.innerHTML = deck.map((card) => {
    const isActive = card.id === activeDeckId;
    const chips = card.filament_ids.map((fid) => {
      const fil = filamentById(fid);
      return `<span class="color-chip" style="background:${fil?.hex || '#ccc'}"></span>`;
    }).join("");
    const supportChips = buildDeckSupportChipsHtml();
    const statusBits = [];
    if (card.gamut?.status === "checking") {
      statusBits.push("Checking gamut");
    } else if (card.gamut?.status === "done") {
      statusBits.push(formatColorRmse(card.gamut));
    } else if (card.gamut?.status === "error") {
      statusBits.push("Gamut failed");
    }
    const tags = [
      card.saved ? `<span class="rail-deck-tag is-saved">Saved</span>` : "",
    ].filter(Boolean).join("");
    return `<div class="rail-deck-card${isActive ? " is-active" : ""}" data-card-id="${card.id}">
      <div class="rail-deck-card-header">
        <div class="rail-deck-card-titlebar">
          <span class="rail-deck-card-title" title="${escAttr(card.name)}">${esc(card.name)}</span>
        </div>
        <div class="rail-deck-card-actions">
          ${tags}
          ${!card.saved ? `<button class="ghost-button xxs rail-deck-save" data-card-id="${card.id}">Save</button>` : ""}
          <button class="ghost-button xxs rail-deck-remove" data-card-id="${card.id}" title="Remove from deck" aria-label="Remove ${escAttr(card.name)}">${xIconSvg()}</button>
        </div>
      </div>
      <div class="rail-deck-card-chips">
        <div class="rail-deck-palette-chips">${chips}</div>
        ${supportChips}
      </div>
      <div class="rail-deck-card-meta">
        ${statusBits.map((bit) => `<span>${esc(bit)}</span>`).join("")}
      </div>
    </div>`;
  }).join("");
  list.querySelectorAll(".rail-deck-card").forEach((el) => {
    el.addEventListener("click", () => setActiveDeckCard(el.dataset.cardId));
    el.addEventListener("mousemove", (e) => handleRailDeckCardHoverMove(el, e));
    el.addEventListener("mouseleave", () => scheduleHideRailDeckHoverPreview());
  });
  list.querySelectorAll(".rail-deck-save").forEach((btn) => {
    btn.addEventListener("click", (e) => { e.stopPropagation(); saveDeckCard(btn.dataset.cardId); });
  });
  list.querySelectorAll(".rail-deck-remove").forEach((btn) => {
    let confirmPending = false;
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      if (confirmPending) {
        removeDeckCard(btn.dataset.cardId);
        return;
      }
      confirmPending = true;
      btn.textContent = "!";
      btn.classList.add("confirm-pending");
      btn.title = "Click again to remove";
      setTimeout(() => {
        confirmPending = false;
        btn.innerHTML = xIconSvg();
        btn.classList.remove("confirm-pending");
        btn.title = "Remove from deck";
      }, 1800);
    });
  });
}

function clearRailDeckHoverTimer() {
  if (railDeckHoverTimer) {
    clearTimeout(railDeckHoverTimer);
    railDeckHoverTimer = null;
  }
  railDeckHoverPendingCardId = null;
}

function clearRailDeckHoverCloseTimer() {
  if (railDeckHoverCloseTimer) {
    clearTimeout(railDeckHoverCloseTimer);
    railDeckHoverCloseTimer = null;
  }
}

function scheduleRailDeckHoverPreview(cardId, anchorEl) {
  if (railDeckHoverPreviewCardId === cardId && railDeckHoverPreviewEl?.classList.contains("is-visible")) return;
  if (railDeckHoverPendingCardId === cardId) return;
  clearRailDeckHoverTimer();
  clearRailDeckHoverCloseTimer();
  railDeckHoverPendingCardId = cardId;
  railDeckHoverTimer = setTimeout(() => {
    railDeckHoverPendingCardId = null;
    showRailDeckHoverPreview(cardId, anchorEl);
  }, 420);
}

function isRailDeckHoverBlockedTarget(target) {
  return Boolean(target?.closest?.(".rail-deck-card-actions button, .rail-deck-card-actions [role='button'], .rail-deck-card-actions a"));
}

function handleRailDeckCardHoverMove(cardEl, event) {
  if (isRailDeckHoverBlockedTarget(event.target)) {
    hideRailDeckHoverPreview();
    return;
  }
  scheduleRailDeckHoverPreview(cardEl.dataset.cardId, cardEl);
}

function scheduleHideRailDeckHoverPreview(delayMs = 180) {
  clearRailDeckHoverTimer();
  clearRailDeckHoverCloseTimer();
  railDeckHoverCloseTimer = setTimeout(() => {
    hideRailDeckHoverPreview();
  }, delayMs);
}

function buildDeckSupportChipsHtml() {
  const baseId = config.base_filament || DEFAULT_BASE_FILAMENT;
  const supportEntries = baseId ? [{ id: baseId, role: "White Base/Cap" }] : [];
  if (!supportEntries.length) return "";

  const slotHtml = Array.from({ length: 1 }, (_, index) => {
    const entry = supportEntries[index];
    if (!entry) {
      return `<span class="deck-support-slot is-empty" aria-hidden="true"></span>`;
    }
    const fil = filamentById(entry.id);
    const label = fil ? `${fil.manufacturer} ${fil.color_name}` : (entry.id || "Unset");
    const hex = fil?.hex || "#ccc";
    return `<span class="deck-support-slot is-filled${isLightHex(hex) ? " is-light" : ""}" title="${esc(`${entry.role}: ${label}`)}" aria-label="${esc(`${entry.role}: ${label}`)}">
      <span class="color-chip deck-support-chip" style="background:${hex}"></span>
    </span>`;
  }).join("");

  return `<div class="deck-support-tray" aria-label="Reserved white base and cap filament">${slotHtml}</div>`;
}

function railHoverFilamentLabel(fil, fallback = "Unset") {
  const colorName = fil?.color_name || fallback;
  const manufacturer = fil?.manufacturer || "";
  if (!manufacturer) return colorName;
  return colorName.toLocaleLowerCase().startsWith(manufacturer.toLocaleLowerCase())
    ? colorName
    : `${manufacturer} ${colorName}`;
}

function buildRailDeckHoverPreview(card) {
  const baseId = config.base_filament || DEFAULT_BASE_FILAMENT;
  let metricHtml = "";
  if (card.gamut?.status === "checking") {
    metricHtml += `<span>Checking gamut</span>`;
  } else if (card.gamut?.status === "done") {
    metricHtml += `<span>${formatColorRmse(card.gamut)}</span>`;
    if (Number.isFinite(card.gamut.n_out_of_gamut)) {
      metricHtml += `<span>${card.gamut.n_out_of_gamut.toLocaleString()} OOG</span>`;
    }
  } else if (card.gamut?.status === "error") {
    metricHtml += `<span>Gamut check failed</span>`;
  }

  const filamentRows = card.filament_ids.map((fid) => {
    const fil = filamentById(fid);
    const colorName = fil?.color_name || fid;
    const title = railHoverFilamentLabel(fil, fid);
    return `<div class="rail-hover-filament-row" title="${esc(title)}">
      <span class="color-chip" style="background:${fil?.hex || '#ccc'}"></span>
      <span class="rail-hover-filament-copy">
        <span class="rail-hover-filament-name">${esc(colorName)}</span>
      </span>
    </div>`;
  }).join("");

  const supportEntries = baseId ? [{ id: baseId, badges: ["Base/Cap"] }] : [];
  const supportRows = supportEntries.map((entry) => {
    const fil = filamentById(entry.id);
    const colorName = fil?.color_name || entry.id || "Unset";
    const title = railHoverFilamentLabel(fil, entry.id || "Unset");
    const badges = entry.badges.map((label) => `<span class="rail-hover-filament-badge">${esc(label)}</span>`).join("");
    return `<div class="rail-hover-filament-row rail-hover-filament-row-support" title="${esc(title)}">
      <span class="color-chip" style="background:${fil?.hex || '#ccc'}"></span>
      <span class="rail-hover-filament-copy">
        <span class="rail-hover-filament-name">${esc(colorName)}</span>
      </span>
      <span class="rail-hover-filament-badges">${badges}</span>
    </div>`;
  }).join("");
  const filamentStack = filamentRows + supportRows;

  return `
    <div class="rail-hover-head">
      <div class="rail-hover-title-row">
        <div class="rail-hover-title">${esc(card.name)}</div>
      </div>
    </div>
    ${metricHtml ? `<div class="rail-hover-metrics">${metricHtml}</div>` : ""}
    <div class="rail-hover-filament-list">${filamentStack}</div>
  `;
}

function positionRailDeckHoverPreview(panel, anchorEl) {
  const rect = anchorEl.getBoundingClientRect();
  const gap = 12;
  const viewportPad = 12;
  const panelRect = panel.getBoundingClientRect();
  let left = rect.right + gap;
  if (left + panelRect.width > window.innerWidth - viewportPad) {
    left = Math.max(viewportPad, rect.left - panelRect.width - gap);
  }
  let top = rect.top - 4;
  const maxTop = window.innerHeight - panelRect.height - viewportPad;
  top = Math.max(viewportPad, Math.min(top, maxTop));
  panel.style.left = `${Math.round(left)}px`;
  panel.style.top = `${Math.round(top)}px`;
}

function showRailDeckHoverPreview(cardId, anchorEl) {
  clearRailDeckHoverTimer();
  clearRailDeckHoverCloseTimer();
  const card = deck.find((entry) => entry.id === cardId);
  if (!card || !anchorEl || !document.body.contains(anchorEl)) return;

  if (!railDeckHoverPreviewEl) {
    railDeckHoverPreviewEl = document.createElement("div");
    railDeckHoverPreviewEl.className = "rail-deck-hover-preview";
    railDeckHoverPreviewEl.addEventListener("mouseenter", () => clearRailDeckHoverCloseTimer());
    railDeckHoverPreviewEl.addEventListener("mouseleave", () => scheduleHideRailDeckHoverPreview(120));
  }

  railDeckHoverPreviewCardId = cardId;
  railDeckHoverPreviewEl.innerHTML = buildRailDeckHoverPreview(card);
  if (!railDeckHoverPreviewEl.parentElement) {
    document.body.appendChild(railDeckHoverPreviewEl);
  }
  railDeckHoverPreviewEl.classList.remove("is-visible");
  positionRailDeckHoverPreview(railDeckHoverPreviewEl, anchorEl);
  requestAnimationFrame(() => {
    if (railDeckHoverPreviewEl) railDeckHoverPreviewEl.classList.add("is-visible");
  });
}

function hideRailDeckHoverPreview() {
  clearRailDeckHoverTimer();
  clearRailDeckHoverCloseTimer();
  if (railDeckHoverPreviewEl) {
    railDeckHoverPreviewEl.remove();
    railDeckHoverPreviewEl = null;
  }
  railDeckHoverPreviewCardId = null;
}

// ── Palette Suggestion (adds to the staging pad) ───────────────────────────

async function handleSuggestPalettes() {
  const btn = $("#suggestPalettesBtn");
  if (!btn) return;

  if (!selectedImage) {
    showToast("Load an image first before generating palette suggestions", "warn");
    return;
  }

  btn.disabled = true;
  btn.textContent = "...";

  const targetCount = parseInt($("#targetFilamentCount")?.value) || 7;
  const swapCount = parseInt($("#targetSwapCount")?.value) || 0;
  const availableIds = [...candidateSelection].filter(fid => !getBaseCapIds().has(fid));
  const paletteMode = normalizeLuminanceMode($("#paletteSuggestMode")?.value || "standard");

  const payload = {
    image_path: selectedImage.filename,
    n_filaments: targetCount,
    top_k: parseInt($("#targetSuggestCount")?.value) || 6,
    filament_ids: availableIds,
    palette_mode: paletteMode,
    max_swaps: swapCount,
  };

  let pollingOwner = null;
  try {
    const started = await apiPost("/palette/suggest", payload);
    activeSuggestJobId = started?.job_id || null;
    if (!activeSuggestJobId) throw new Error("Suggestion start did not return a job ID");
    suggestCancelPending = false;
    startProgress("Suggesting palettes...", "suggest");

    const pollingJobId = activeSuggestJobId;
    if (_suggestPolling) _suggestPolling.cancelled = true;
    pollingOwner = { jobId: pollingJobId, cancelled: false };
    _suggestPolling = pollingOwner;
    const st = await pollJobUntilTerminal({
      jobId: pollingJobId,
      fetchStatus: () => apiFetch("/palette/suggest/status"),
      isTerminal: (status) => !["running", "cancelling"].includes(status.status),
      shouldContinue: () => (
        !pollingOwner.cancelled
        && _suggestPolling === pollingOwner
        && activeSuggestJobId === pollingJobId
      ),
      intervalMs: 1000,
      onStatus: (status) => updateOperationProgressFromStatus(status, "Suggesting palettes..."),
      onTransientError: () => updateOperationProgressFromStatus(
        { status: "running", progress: "Connection interrupted; retrying suggestion status..." },
        "Suggesting palettes...",
      ),
    });
    if (!st || _suggestPolling !== pollingOwner) return;
    if (st.status === "complete" && st.result) {
      _processSuggestResults(st.result);
    } else if (st.status === "cancelled") {
      if (st.result) _processSuggestResults(st.result);
      showToast("Suggestion cancelled", "warn");
    } else if (st.status === "error") {
      showToast(`Suggestion failed: ${st.progress}`, "error");
    }
  } catch (err) {
    if (err.name === "AbortError") return;
    if (pollingOwner && _suggestPolling !== pollingOwner) return;
    const prefix = pollingOwner ? "Palette suggestion status failed" : "Palette suggestion failed to start";
    showToast(`${prefix}: ${err.message}`, "error");
  } finally {
    if (pollingOwner && _suggestPolling !== pollingOwner) return;
    if (_suggestPolling === pollingOwner) _suggestPolling = null;
    activeSuggestJobId = null;
    suggestCancelPending = false;
    stopProgress();
    btn.textContent = "Suggest Palettes";
    btn.disabled = false;
  }
}

async function handleSuggestBaseShadingLimit() {
  const btn = $("#cfgBaseShadingLimitSuggest");
  const input = getBaseShadingLimitInput();
  if (!btn || !input) return;
  if (!selectedImage) {
    showToast("Select an image before suggesting a shading balance", "warn");
    return;
  }
  btn.disabled = true;
  const oldText = btn.textContent;
  btn.textContent = "...";
  try {
    const result = await apiPost("/luminance/base-shading-limit/recommend", {
      image_path: selectedImage.filename,
    });
    const value = setLuminanceBaseShadingLimitFraction(
      result.recommended_base_shading_limit_fraction
        ?? result.recommended_authority_fraction
        ?? 0.75,
    );
    syncBaseShadingLimitControls(formatLuminanceBaseShadingLimitPercent(value));
    updateSettingsSummaries();
    checkPresetModified();
    await syncConfigToServer();
    showToast(`Shading balance set to ${formatLuminanceBaseShadingLimitPercent(value)}%`, "success");
  } catch (err) {
    showToast(`Shading balance suggestion failed: ${err.message}`, "error");
  } finally {
    btn.textContent = oldText || "Suggest";
    btn.disabled = false;
  }
}

function _processSuggestResults(suggestions) {
  let addedCount = 0;
  const seenSets = new Set();
  const perLoadCapped = suggestions?.per_load_capped;
  suggestCapacityNote = perLoadCapped?.capacity
    ? `Max colors capped to ${perLoadCapped.capacity} by AMS capacity`
    : "";
  const paletteMode = normalizeLuminanceMode(
    suggestions?.palette_mode || $("#paletteSuggestMode")?.value || "standard",
  );
  const modePrefix = paletteMode === "luminance_detail" ? "Luminance" : "Source";
  const makeKey = (ids = []) => [...ids].map(String).sort().join("\u0001");
  const pushSuggestionCard = (cand, name, extra = {}) => {
    const key = makeKey(cand.filament_ids || []);
    if (!key || seenSets.has(key)) return false;
    seenSets.add(key);
    const card = {
      id: "suggest-" + Date.now() + "-" + Math.random().toString(36).slice(2, 6),
      name,
      filament_ids: cand.filament_ids,
      gamut: {
        status: "done",
        coverage_pct: cand.coverage_pct || 0,
        mean_de: cand.mean_de || 0,
        suggestion_mean_de: cand.suggestion_mean_de ?? cand.mean_de ?? 0,
        n_out_of_gamut: 0,
        total_pixels: 0,
      },
      quality_metrics: null,
      saved: false,
      ...extra,
    };
    stagingDeck.push(card);
    addedCount++;
    return true;
  };

  if (suggestions.tiers) {
    const recommendedKey = makeKey(suggestions?.recommended?.filament_ids || []);
    const alternatives = suggestions.alternatives || [];
    for (const cand of alternatives) {
      const isRecommended = makeKey(cand.filament_ids || []) === recommendedKey;
      pushSuggestionCard(
        cand,
        `${modePrefix} suggested ${nextDeckNum++}`,
        { recommended: isRecommended },
      );
    }
    for (const tier of suggestions.tiers) {
      const tierLabel = tier.swap_count === 0 ? "base load" : `${tier.swap_count} extra load${tier.swap_count > 1 ? "s" : ""}`;
      for (const cand of tier.candidates) {
        pushSuggestionCard(cand, `${modePrefix} tier ${tier.swap_count} (${tierLabel}) size ${cand.filament_ids?.length || tier.n_filaments}`, {
          swap_count: tier.swap_count,
        });
      }
    }
  } else {
    const candidates = suggestions.candidates || [];
    for (const cand of candidates) {
      pushSuggestionCard(cand, `${modePrefix} suggested ${nextDeckNum++}`);
    }
  }

  if (addedCount === 0) {
    showToast("No palette suggestions found", "error");
    return;
  }
  renderCreationTab();
  updateRail();
  if (suggestCapacityNote) {
    showToast(suggestCapacityNote, "warn");
  }
  showToast(`Staged ${addedCount} suggested palettes`, "success");
}

// ── Library Filter (modal overlay) ───────────────────────────────────────────

function isGenerationEligibleFilament(filament) {
  return !!(
    filament
    && filament.has_profile
    && filament.exclude_from_model !== true
    && filament.generation_available !== false
  );
}

function getGenerationEligibleFilamentIds() {
  return allFilaments
    .filter(isGenerationEligibleFilament)
    .map(filament => filament.filament_id);
}

function normalizeEnabledFilamentEntry(entry) {
  if (!entry || !Array.isArray(entry.eligible_ids) || !Array.isArray(entry.enabled_ids)) return null;
  const stringsOnly = values => values.filter(value => typeof value === "string" && value.trim());
  return {
    eligible_ids: stringsOnly(entry.eligible_ids),
    enabled_ids: stringsOnly(entry.enabled_ids),
  };
}

function reconcileEnabledFilamentIds(eligibleIds, savedEntry) {
  const eligible = [...new Set(eligibleIds)];
  const normalized = normalizeEnabledFilamentEntry(savedEntry);
  if (!normalized) return eligible;

  const previouslyEligible = new Set(normalized.eligible_ids);
  const previouslyEnabled = new Set(normalized.enabled_ids);
  return eligible.filter(id => previouslyEnabled.has(id) || !previouslyEligible.has(id));
}

function readEnabledFilamentStore() {
  try {
    const parsed = JSON.parse(localStorage.getItem(ENABLED_FILAMENTS_STORAGE_KEY) || "null");
    if (
      parsed
      && parsed.schema_version === ENABLED_FILAMENTS_STORAGE_VERSION
      && parsed.libraries
      && typeof parsed.libraries === "object"
      && !Array.isArray(parsed.libraries)
    ) {
      return parsed;
    }
  } catch { /* discard malformed state after the runtime library is known */ }
  return { schema_version: ENABLED_FILAMENTS_STORAGE_VERSION, libraries: {} };
}

function authoritativeRuntimeLibraryId() {
  const status = modelLibraryManager.status;
  if (!status || status.active_state_error || !status.runtime_active_library_id) return null;
  return String(status.runtime_active_library_id);
}

function saveEnabledFilaments() {
  const runtimeLibraryId = authoritativeRuntimeLibraryId();
  if (
    !enabledFilamentPersistenceReady
    || !runtimeLibraryId
    || runtimeLibraryId !== enabledFilamentRuntimeLibraryId
  ) return false;

  const eligibleIds = getGenerationEligibleFilamentIds();
  const eligibleSet = new Set(eligibleIds);
  const store = readEnabledFilamentStore();
  store.libraries[runtimeLibraryId] = {
    eligible_ids: eligibleIds,
    enabled_ids: [...enabledFilaments].filter(id => eligibleSet.has(id)),
  };
  try {
    localStorage.setItem(ENABLED_FILAMENTS_STORAGE_KEY, JSON.stringify(store));
    return true;
  } catch {
    return false;
  }
}

function refreshEnabledFilamentConsumers({ reopenDetailId = null } = {}) {
  renderLibraryFilterGrid();
  renderCreationTab();
  updateRail();
  if (reopenDetailId) openFilamentDetail(reopenDetailId);
}

function applyEnabledFilamentSelection(nextIds, { persist = true, render = true, reopenDetailId = null } = {}) {
  const eligibleIds = new Set(getGenerationEligibleFilamentIds());
  enabledFilaments = new Set([...nextIds].filter(id => eligibleIds.has(id)));
  candidateSelection = new Set([...candidateSelection].filter(id => enabledFilaments.has(id)));
  manualSlots = manualSlots.filter(id => enabledFilaments.has(id));
  if (persist) saveEnabledFilaments();
  if (render) refreshEnabledFilamentConsumers({ reopenDetailId });
}

function setFilamentEnabled(filamentId, enabled, { reopenDetail = false } = {}) {
  const next = new Set(enabledFilaments);
  if (enabled) next.add(filamentId);
  else next.delete(filamentId);
  applyEnabledFilamentSelection(next, { reopenDetailId: reopenDetail ? filamentId : null });
}

function reconcileEnabledFilamentsForRuntimeLibrary() {
  const runtimeLibraryId = authoritativeRuntimeLibraryId();
  if (!runtimeLibraryId) {
    enabledFilamentRuntimeLibraryId = null;
    enabledFilamentPersistenceReady = false;
    applyEnabledFilamentSelection(getGenerationEligibleFilamentIds(), { persist: false, render: false });
    return false;
  }

  const store = readEnabledFilamentStore();
  const eligibleIds = getGenerationEligibleFilamentIds();
  const savedEntry = normalizeEnabledFilamentEntry(store.libraries[runtimeLibraryId]);
  const reconciledIds = reconcileEnabledFilamentIds(eligibleIds, savedEntry);
  enabledFilamentRuntimeLibraryId = runtimeLibraryId;
  enabledFilamentPersistenceReady = true;
  applyEnabledFilamentSelection(reconciledIds, { persist: false, render: false });

  // The legacy value has no library provenance. Retire it only after the new,
  // authoritative runtime-scoped record has been written successfully.
  if (saveEnabledFilaments()) {
    try { localStorage.removeItem(LEGACY_ENABLED_FILAMENTS_STORAGE_KEY); } catch { /* ignore */ }
  }
  return true;
}

function openLibraryModal() {
  const backdrop = $("#libraryModalBackdrop");
  if (backdrop) {
    backdrop.classList.remove("is-hidden");
    backdrop.setAttribute("aria-hidden", "false");
  }
  renderLibraryFilterGrid();
}

function closeLibraryModal() {
  const backdrop = $("#libraryModalBackdrop");
  if (backdrop) {
    backdrop.classList.add("is-hidden");
    backdrop.setAttribute("aria-hidden", "true");
  }
}

function renderLibraryFilterGrid() {
  const grid = $("#libraryFilterGrid");
  if (!grid) return;

  // Group by manufacturer (same pattern as palette library pane)
  const groups = new Map();
  for (const fil of allFilaments) {
    const mfg = fil.manufacturer || "Other";
    if (!groups.has(mfg)) groups.set(mfg, []);
    groups.get(mfg).push(fil);
  }

  let html = "";
  for (const [mfg, fils] of groups) {
    html += `<div class="library-group-header">${esc(mfg)}</div>`;
    html += `<div class="library-group-cards">`;
    for (const fil of fils) {
      const isEnabled = enabledFilaments.has(fil.filament_id);
      const hasProfile = fil.has_profile;
      const isEligible = isGenerationEligibleFilament(fil);
      const textCol = textColorForHex(fil.hex);
      const stateClass = !isEligible ? "no-profile" : isEnabled ? "is-selected" : "is-disabled-filter";
      const unavailableTitle = !hasProfile
        ? "No calibration profile available"
        : !isEligible
          ? "Unavailable in this model library"
          : "";
      html += `
        <div class="filament-card ${stateClass}"
             data-filament-id="${fil.filament_id}"
             ${unavailableTitle ? `title="${unavailableTitle}"` : ""}>
          <div class="filter-check">${isEnabled && isEligible ? "\u2713" : ""}</div>
          <div class="filament-swatch" style="background:${fil.hex};color:${textCol}">
            ${!isEligible ? "?" : ""}
          </div>
          <div class="filament-copy">
            <div class="filament-detail">${esc(fil.color_name)}</div>
          </div>
        </div>
      `;
    }
    html += `</div>`;
  }
  grid.innerHTML = html;

  grid.querySelectorAll(".filament-card").forEach((card) => {
    const fid = card.dataset.filamentId;
    const fil = filamentById(fid);
    if (!isGenerationEligibleFilament(fil)) return;

    card.addEventListener("click", () => {
      setFilamentEnabled(fid, !enabledFilaments.has(fid));
    });
  });

  updateLibraryFilterStatus();
}

function handleLibraryFilterSelectAll() {
  applyEnabledFilamentSelection(getGenerationEligibleFilamentIds());
}

function handleLibraryFilterDeselectAll() {
  applyEnabledFilamentSelection([]);
}

// ── Published Model Libraries ───────────────────────────────────────────────

function modelLibraryKey(item) {
  return item?.library_id || `directory:${item?.directory_name || "unknown"}`;
}

function selectedModelLibrary() {
  const libraries = modelLibraryManager.status?.libraries || [];
  return libraries.find(item => modelLibraryKey(item) === modelLibraryManager.selectedKey) || null;
}

function formatModelLibraryDate(value) {
  if (!value) return "Not available";
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return String(value);
  return date.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

function formatModelLibrarySize(value) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes < 0) return "Not available";
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function modelLibraryCompatibility(item) {
  if (!item?.valid) return "Could not be verified";
  const minimum = item.minimum_prisma_version || "unknown";
  const maximum = item.maximum_prisma_version;
  return maximum ? `Prisma ${minimum} through ${maximum}` : `Prisma ${minimum} or newer`;
}

function setModelLibraryMessage(message = "", kind = "") {
  modelLibraryManager.message = message;
  modelLibraryManager.messageKind = kind;
  modelLibraryManager.error = kind === "error" ? message : "";
}

function modelLibraryErrorMessage(error, fallback) {
  const message = String(error?.message || fallback || "Model-library operation failed");
  return message.replace(/^API\s+\d+:\s*/i, "").trim();
}

function modelLibraryDisplayName(item) {
  return item?.library_name || item?.directory_name || item?.library_id || "Unknown library";
}

function modelLibraryRailSummary(status, { loading = false } = {}) {
  if (!status) {
    return loading
      ? { name: "Checking model library…", state: "Checking", kind: "idle", detail: "Validating installed model libraries" }
      : { name: "Library status unavailable", state: "Unavailable", kind: "warning", detail: "Open Model Library to retry" };
  }

  const libraries = Array.isArray(status.libraries) ? status.libraries : [];
  const runtime = libraries.find(item => item.runtime_active)
    || libraries.find(item => item.valid && item.library_id === status.runtime_active_library_id)
    || null;
  const selected = libraries.find(item => item.selected_for_next_launch)
    || (status.active_library_id
      ? libraries.find(item => item.library_id === status.active_library_id)
      : null)
    || null;

  if (status.active_state_error) {
    return {
      name: selected ? modelLibraryDisplayName(selected) : "Library selection is unreadable",
      state: "Invalid",
      kind: "error",
      detail: String(status.active_state_error),
    };
  }
  if (selected && !selected.valid) {
    return {
      name: modelLibraryDisplayName(selected),
      state: "Invalid",
      kind: "error",
      detail: selected.error || "The selected model library failed validation",
    };
  }
  if (status.restart_required) {
    const nextName = selected ? modelLibraryDisplayName(selected) : "the selected library";
    return {
      name: runtime ? modelLibraryDisplayName(runtime) : nextName,
      state: "Restart Required",
      kind: "warning",
      detail: runtime
        ? `${nextName} is selected for the next launch`
        : `${nextName} will be used after Prisma restarts`,
    };
  }
  if (runtime) {
    return {
      name: modelLibraryDisplayName(runtime),
      state: "In Use",
      kind: "ok",
      detail: "This running Generator is using this model library",
    };
  }
  if (status.runtime_active_library_id || status.active_library_id) {
    return {
      name: selected ? modelLibraryDisplayName(selected) : "Selected library is unavailable",
      state: "Invalid",
      kind: "error",
      detail: "The selected model library is missing or could not be loaded",
    };
  }
  return {
    name: "No library selected",
    state: "No library selected",
    kind: "idle",
    detail: libraries.length
      ? "Choose a valid model library"
      : "Install a published model library to enable generation",
  };
}

function renderModelLibraryRail() {
  const summaryEl = $("#railModelLibrarySummary");
  const nameEl = $("#railModelLibraryName");
  const stateEl = $("#railModelLibraryState");
  if (!summaryEl || !nameEl || !stateEl) return;
  const summary = modelLibraryRailSummary(
    modelLibraryManager.status,
    { loading: modelLibraryManager.loading },
  );
  nameEl.textContent = summary.name;
  stateEl.textContent = summary.state;
  stateEl.className = `rail-model-library-state is-${summary.kind}`;
  summaryEl.title = summary.detail;
}

function setModelLibraryBusy(busy, footerText = "") {
  modelLibraryManager.busy = !!busy;
  const modal = $("#modelLibrariesModal .model-libraries-modal");
  if (modal) modal.classList.toggle("is-busy", !!busy);
  const footer = $("#modelLibrariesFooterStatus");
  if (footer && footerText) footer.textContent = footerText;
}

function updateModelLibrariesAttention() {
  const status = modelLibraryManager.status;
  const attention = $("#modelLibrariesAttention");
  const button = $("#modelLibrariesBtn");
  const noLibraries = !!status && Array.isArray(status.libraries) && status.libraries.length === 0;
  const needsAttention = !status || !!status.active_state_error || noLibraries || !!status.restart_required || !status.runtime_active_library_id;
  if (attention) attention.classList.toggle("is-hidden", !needsAttention);
  if (button) {
    button.classList.toggle("has-attention", needsAttention);
    button.title = noLibraries
      ? "Prisma needs a published model library"
      : status?.restart_required
        ? "A model library change is waiting for restart"
      : !status?.runtime_active_library_id
        ? "Prisma needs a valid model library"
        : "Install or switch published model libraries";
  }
}

function renderModelLibrariesManager() {
  renderModelLibraryRail();
  const status = modelLibraryManager.status;
  const libraries = status?.libraries || [];
  const list = $("#modelLibrariesList");
  const details = $("#modelLibraryDetails");
  const notice = $("#modelLibrariesNotice");
  const footer = $("#modelLibrariesFooterStatus");
  if (!list || !details || !notice) return;

  const existingKeys = new Set(libraries.map(modelLibraryKey));
  if (!existingKeys.has(modelLibraryManager.selectedKey)) {
    const preferred = libraries.find(item => item.runtime_active)
      || libraries.find(item => item.selected_for_next_launch)
      || libraries[0];
    modelLibraryManager.selectedKey = preferred ? modelLibraryKey(preferred) : null;
  }

  let noticeMessage = modelLibraryManager.message;
  let noticeKind = modelLibraryManager.messageKind;
  const selectedNextLibrary = libraries.find(item => item.selected_for_next_launch);
  if (!noticeMessage && status?.active_state_error) {
    noticeMessage = `Prisma could not read the selected-library record: ${status.active_state_error}`;
    noticeKind = "error";
  } else if (!noticeMessage && status && !libraries.length) {
    noticeMessage = status.active_library_id
      ? "The selected model library is no longer installed. Install a published model library, then select it for the next launch."
      : "No model libraries are installed. Install a published model library to enable lithophane generation.";
    noticeKind = "warning";
  } else if (!noticeMessage && selectedNextLibrary && !selectedNextLibrary.valid) {
    noticeMessage = "The selected model library is invalid. Choose a valid library before restarting Prisma.";
    noticeKind = "error";
  } else if (!noticeMessage && status?.restart_required) {
    noticeMessage = "A different model library is selected. Restart Prisma to begin using it.";
    noticeKind = "warning";
  } else if (!noticeMessage && status && !status.runtime_active_library_id) {
    noticeMessage = "Prisma is in Library Recovery mode. Install or select a valid library to enable lithophane generation.";
    noticeKind = "warning";
  }
  notice.textContent = noticeMessage || "";
  notice.className = `model-libraries-notice${noticeMessage ? "" : " is-hidden"}${noticeKind ? ` is-${noticeKind}` : ""}`;

  if (modelLibraryManager.loading) {
    list.innerHTML = `<div class="model-libraries-empty"><strong>Checking libraries…</strong><span>Validating installed model files.</span></div>`;
  } else if (!libraries.length) {
    list.innerHTML = `<div class="model-libraries-empty"><strong>No libraries installed</strong><span>Choose Install Library to add a downloaded Prisma model package.</span></div>`;
  } else {
    list.innerHTML = libraries.map((item, index) => {
      const key = modelLibraryKey(item);
      const name = item.library_name || item.directory_name || "Unreadable library";
      const meta = item.valid
        ? `${item.library_version || "Unknown version"} · ${item.publisher || "Unknown publisher"}`
        : (item.error || "Validation failed");
      const badges = [
        item.runtime_active ? `<span class="model-library-badge is-runtime">In Use</span>` : "",
        item.selected_for_next_launch && !item.runtime_active ? `<span class="model-library-badge is-next">Selected next launch</span>` : "",
        !item.valid ? `<span class="model-library-badge is-error">Invalid</span>` : "",
      ].join("");
      return `
        <button class="model-library-list-item${key === modelLibraryManager.selectedKey ? " is-selected" : ""}${item.runtime_active ? " is-runtime" : ""}${!item.valid ? " is-invalid" : ""}"
                type="button" data-model-library-index="${index}">
          <span class="model-library-list-marker" aria-hidden="true"></span>
          <span class="model-library-list-copy">
            <span class="model-library-list-name">${esc(name)}</span>
            <span class="model-library-list-meta">${esc(meta)}</span>
          </span>
          <span class="model-library-list-badges">${badges}</span>
        </button>`;
    }).join("");
  }

  list.querySelectorAll("[data-model-library-index]").forEach(button => {
    button.addEventListener("click", () => {
      const item = libraries[Number(button.dataset.modelLibraryIndex)];
      if (!item) return;
      modelLibraryManager.selectedKey = modelLibraryKey(item);
      setModelLibraryMessage();
      renderModelLibrariesManager();
    });
  });

  const item = selectedModelLibrary();
  if (!item) {
    details.innerHTML = `<div class="model-libraries-empty"><strong>Select a model library</strong><span>Library information and available actions will appear here.</span></div>`;
  } else if (!item.valid) {
    const removable = !!item.library_id && !item.runtime_active && !item.selected_for_next_launch;
    const invalidBadges = [
      item.selected_for_next_launch && !item.runtime_active ? `<span class="model-library-badge is-next">Selected next launch</span>` : "",
      `<span class="model-library-badge is-error">Invalid</span>`,
    ].join("");
    details.innerHTML = `
      <div class="model-library-detail-title-row">
        <div><h4 class="model-library-detail-title">${esc(item.directory_name || "Unreadable library")}</h4><div class="model-library-detail-version">This installed library cannot be used.</div></div>
        <div class="model-library-detail-badges">${invalidBadges}</div>
      </div>
      <div class="model-library-validation-error"><strong>Validation failed</strong><br>${esc(item.error || "Prisma could not validate this model library.")}</div>
      <div class="model-library-detail-actions">
        <button class="ghost-button small danger" id="modelLibraryRemoveBtn" type="button" ${removable ? "" : "disabled"}>Remove Library</button>
      </div>`;
  } else {
    const description = item.description || "No description was provided for this library.";
    const releaseNotes = item.release_notes
      ? `<div class="model-library-release-notes"><strong>Release notes</strong><br>${esc(item.release_notes)}</div>`
      : "";
    const activateLabel = item.runtime_active
      ? "Currently in Use"
      : item.selected_for_next_launch
        ? "Restart Prisma"
        : "Activate and Restart Prisma";
    const activateDisabled = item.runtime_active ? "disabled" : "";
    const removable = !item.runtime_active && !item.selected_for_next_launch;
    const badges = [
      item.runtime_active ? `<span class="model-library-badge is-runtime">In Use</span>` : "",
      item.selected_for_next_launch && !item.runtime_active ? `<span class="model-library-badge is-next">Selected next launch</span>` : "",
      `<span class="model-library-badge">Valid</span>`,
    ].join("");
    details.innerHTML = `
      <div class="model-library-detail-title-row">
        <div><h4 class="model-library-detail-title">${esc(item.library_name)}</h4><div class="model-library-detail-version">Version ${esc(item.library_version || "unknown")}</div></div>
        <div class="model-library-detail-badges">${badges}</div>
      </div>
      <p class="model-library-detail-description">${esc(description)}</p>
      <div class="model-library-detail-grid">
        <div class="model-library-detail-label">Publisher</div><div class="model-library-detail-value">${esc(item.publisher || "Not available")}</div>
        <div class="model-library-detail-label">Published</div><div class="model-library-detail-value">${esc(formatModelLibraryDate(item.created_at))}</div>
        <div class="model-library-detail-label">Filaments</div><div class="model-library-detail-value">${esc(String(item.filament_count ?? "Not available"))}</div>
        <div class="model-library-detail-label">Compatibility</div><div class="model-library-detail-value">${esc(modelLibraryCompatibility(item))}</div>
        <div class="model-library-detail-label">Package size</div><div class="model-library-detail-value">${esc(formatModelLibrarySize(item.total_bytes))}</div>
        <div class="model-library-detail-label">Validation</div><div class="model-library-detail-value">All model files verified</div>
      </div>
      ${releaseNotes}
      <div class="model-library-detail-actions">
        <button class="ghost-button small danger" id="modelLibraryRemoveBtn" type="button" ${removable ? "" : "disabled"}>Remove Library</button>
        <button class="primary-button small" id="modelLibraryActivateBtn" type="button" ${activateDisabled}>${activateLabel}</button>
      </div>`;
  }

  $("#modelLibraryActivateBtn")?.addEventListener("click", () => handleActivateModelLibrary(item));
  $("#modelLibraryRemoveBtn")?.addEventListener("click", () => handleRemoveModelLibrary(item));
  if (footer) {
    const validCount = libraries.filter(entry => entry.valid).length;
    footer.textContent = modelLibraryManager.busy
      ? footer.textContent
      : `${validCount} valid · ${libraries.length} installed`;
  }
  setModelLibraryBusy(modelLibraryManager.busy);
  updateModelLibrariesAttention();
}

function openModelLibrariesModal() {
  const modal = $("#modelLibrariesModal");
  if (!modal) return;
  modal.classList.remove("is-hidden");
  modal.setAttribute("aria-hidden", "false");
  if (modelLibraryManager.status) {
    renderModelLibrariesManager();
  } else {
    loadModelLibraries({ silent: true });
  }
}

function closeModelLibrariesModal() {
  if (modelLibraryManager.busy || modelLibraryManager.restarting) return;
  const modal = $("#modelLibrariesModal");
  if (!modal) return;
  modal.classList.add("is-hidden");
  modal.setAttribute("aria-hidden", "true");
}

async function loadModelLibraries({ openOnRecovery = false, silent = false } = {}) {
  modelLibraryManager.loading = true;
  if (!silent) setModelLibraryMessage();
  renderModelLibrariesManager();
  try {
    const status = await fetchModelLibraries();
    modelLibraryManager.status = status;
    reconcileEnabledFilamentsForRuntimeLibrary();
    const libraries = status.libraries || [];
    const existingKeys = new Set(libraries.map(modelLibraryKey));
    if (!existingKeys.has(modelLibraryManager.selectedKey)) {
      const preferred = libraries.find(item => item.runtime_active)
        || libraries.find(item => item.selected_for_next_launch)
        || libraries[0];
      modelLibraryManager.selectedKey = preferred ? modelLibraryKey(preferred) : null;
    }
    if (openOnRecovery && !modelLibraryAutoOpened && (!status.runtime_active_library_id || status.active_state_error)) {
      modelLibraryAutoOpened = true;
      const modal = $("#modelLibrariesModal");
      modal?.classList.remove("is-hidden");
      modal?.setAttribute("aria-hidden", "false");
    }
  } catch (error) {
    setModelLibraryMessage(modelLibraryErrorMessage(error, "Could not load model libraries"), "error");
  } finally {
    modelLibraryManager.loading = false;
    renderModelLibrariesManager();
  }
}

async function handleInstallModelLibrary(file) {
  if (!file || modelLibraryManager.busy) return;
  setModelLibraryMessage();
  setModelLibraryBusy(true, `Installing ${file.name}…`);
  try {
    const response = await installModelLibrary(file);
    modelLibraryManager.status = response.status;
    modelLibraryManager.selectedKey = response.installed?.library_id || modelLibraryManager.selectedKey;
    setModelLibraryMessage(
      `${response.installed?.library_name || "Model library"} was installed. Select Activate and Restart Prisma when you are ready to use it.`,
      "success",
    );
    showToast("Model library installed", "success");
  } catch (error) {
    setModelLibraryMessage(modelLibraryErrorMessage(error, "The model library could not be installed"), "error");
    showToast("Library installation failed", "error");
  } finally {
    const input = $("#modelLibraryPackageInput");
    if (input) input.value = "";
    setModelLibraryBusy(false);
    renderModelLibrariesManager();
  }
}

async function waitForModelLibraryRestart(targetId) {
  const deadline = Date.now() + 60000;
  while (Date.now() < deadline) {
    await new Promise(resolve => setTimeout(resolve, 500));
    try {
      const status = await fetchModelLibraries();
      if (status.runtime_active_library_id === targetId && !status.restart_required) {
        window.location.reload();
        return;
      }
    } catch { /* the server is expected to disappear briefly */ }
  }
  modelLibraryManager.restarting = false;
  setModelLibraryBusy(false);
  setModelLibraryMessage(
    "The selected library was saved, but Prisma did not reconnect. Close Prisma and open it again to finish switching libraries.",
    "warning",
  );
  renderModelLibrariesManager();
}

async function requestModelLibraryRestart(targetId) {
  modelLibraryManager.restarting = true;
  setModelLibraryMessage("Prisma is restarting with the selected model library…", "success");
  setModelLibraryBusy(true, "Restarting Prisma…");
  renderModelLibrariesManager();
  try {
    await restartPrisma();
    await waitForModelLibraryRestart(targetId);
  } catch (error) {
    modelLibraryManager.restarting = false;
    setModelLibraryBusy(false);
    setModelLibraryMessage(
      `The library selection was saved, but automatic restart failed. Close Prisma and open it again. ${modelLibraryErrorMessage(error, "")}`.trim(),
      "warning",
    );
    renderModelLibrariesManager();
  }
}

async function handleActivateModelLibrary(item) {
  if (!item?.valid || !item.library_id || modelLibraryManager.busy) return;
  if (item.selected_for_next_launch && !item.runtime_active) {
    await requestModelLibraryRestart(item.library_id);
    return;
  }
  const confirmed = await appConfirm(
    `Switch to “${item.library_name}”? Prisma will restart, and any unsaved session history will be cleared.`,
    { title: "Activate Model Library", ok: "Activate and Restart" },
  );
  if (!confirmed) return;
  setModelLibraryMessage();
  setModelLibraryBusy(true, `Selecting ${item.library_name}…`);
  try {
    const response = await activateModelLibrary(item.library_id);
    modelLibraryManager.status = response.status;
    modelLibraryManager.selectedKey = item.library_id;
    renderModelLibrariesManager();
    if (response.restart_required) {
      await requestModelLibraryRestart(item.library_id);
    } else {
      setModelLibraryMessage(`${item.library_name} is already in use.`, "success");
    }
  } catch (error) {
    setModelLibraryMessage(modelLibraryErrorMessage(error, "The model library could not be selected"), "error");
  } finally {
    if (!modelLibraryManager.restarting) {
      setModelLibraryBusy(false);
      renderModelLibrariesManager();
    }
  }
}

async function handleRemoveModelLibrary(item) {
  if (!item?.library_id || item.runtime_active || item.selected_for_next_launch || modelLibraryManager.busy) return;
  const name = item.library_name || item.directory_name || "this library";
  const confirmed = await appConfirm(
    `Remove “${name}” from this copy of Prisma? This cannot be undone, but the original downloaded package is not affected.`,
    { title: "Remove Model Library", ok: "Remove Library" },
  );
  if (!confirmed) return;
  setModelLibraryBusy(true, `Removing ${name}…`);
  try {
    const response = await removeModelLibrary(item.library_id);
    modelLibraryManager.status = response.status;
    modelLibraryManager.selectedKey = null;
    setModelLibraryMessage(`${name} was removed.`, "success");
    showToast("Model library removed", "success");
  } catch (error) {
    setModelLibraryMessage(modelLibraryErrorMessage(error, "The model library could not be removed"), "error");
  } finally {
    setModelLibraryBusy(false);
    renderModelLibrariesManager();
  }
}

async function handleOpenModelLibrariesFolder() {
  try {
    await openModelLibrariesFolder();
    showToast("Opened Model Libraries folder", "success");
  } catch (error) {
    setModelLibraryMessage(modelLibraryErrorMessage(error, "The Model Libraries folder could not be opened"), "error");
    renderModelLibrariesManager();
  }
}

// ── Detail Drawer ────────────────────────────────────────────────────────────

function openDetailDrawer(title, bodyHtml) {
  if (settingsDrawerOpen) closeSettingsDrawer();
  const drawer = $("#detailDrawer");
  const overlay = $("#drawerOverlay");
  $("#drawerTitle").textContent = title;
  $("#drawerBody").innerHTML = bodyHtml;
  drawer.setAttribute("aria-hidden", "false");
  overlay.setAttribute("aria-hidden", "false");
}

function closeDetailDrawer() {
  const drawer = $("#detailDrawer");
  const overlay = $("#drawerOverlay");
  drawer.setAttribute("aria-hidden", "true");
  overlay.setAttribute("aria-hidden", "true");
}

// ── Settings Drawer ─────────────────────────────────────────────────────────

function toggleSettingsDrawer() {
  // Settings opener(s) toggle: a second click on an already-open drawer closes it.
  if (settingsDrawerOpen) closeSettingsDrawer();
  else openSettingsDrawer();
}

function openSettingsDrawer() {
  // Close detail drawer if open
  const detailDrawer = $("#detailDrawer");
  if (detailDrawer && detailDrawer.getAttribute("aria-hidden") === "false") {
    closeDetailDrawer();
  }

  const grid = $(".settings-grid");
  const drawerBody = $("#settingsDrawerBody");
  const drawer = $("#settingsDrawer");

  // Reparent settings grid into drawer
  drawerBody.appendChild(grid);
  grid.classList.add("in-drawer");

  // Show drawer — persistent overlay, NO scrim: settings must not dim or close the page.
  drawer.setAttribute("aria-hidden", "false");
  settingsDrawerOpen = true;
  scheduleSettingsDrawerDistribution();
}

function closeSettingsDrawer() {
  const grid = $(".settings-grid");
  const tabSettings = $("#tabSettings");
  const drawer = $("#settingsDrawer");

  // Return dynamic preprocessing cards to their canonical container before
  // removing responsive wrappers. This keeps rerenders from leaving detached
  // duplicate controls behind.
  restoreSettingsFlowUnits(grid);

  // Remove responsive multi-column wrappers before returning the grid to its hidden host.
  grid.querySelectorAll(".settings-column").forEach(col => {
    while (col.firstChild) grid.appendChild(col.firstChild);
    col.remove();
  });
  grid.classList.remove("in-drawer");

  // Reparent settings grid back to its hidden host
  tabSettings.appendChild(grid);

  // Hide drawer — the settings drawer no longer owns #drawerOverlay (detail drawer does).
  drawer.setAttribute("aria-hidden", "true");
  settingsDrawerOpen = false;
}

function scheduleSettingsDrawerDistribution() {
  const distributeIfStillOpen = () => {
    const grid = $(".settings-grid");
    if (
      !settingsDrawerOpen
      || !grid?.classList.contains("in-drawer")
    ) {
      return;
    }
    distributeSettingsColumns();
  };
  requestAnimationFrame(() => requestAnimationFrame(distributeIfStillOpen));
  // The drawer slides for 200ms, so repeat after its final width is measurable.
  window.setTimeout(distributeIfStillOpen, 240);
}

// (openRunSettingsDrawer removed in Task 6 S5 — a past run's frozen settings now show as a
// read-only essentials flyout on its run card, not by piping the run through the live drawer.)

function loadSettingsAdvancedVisible() {
  try {
    const stored = localStorage.getItem(SETTINGS_ADVANCED_VISIBLE_STORAGE_KEY);
    if (stored === "true") return true;
    if (stored === "false") return false;
  } catch { /* ignore */ }
  return false;
}

function saveSettingsAdvancedVisible(visible) {
  try {
    localStorage.setItem(
      SETTINGS_ADVANCED_VISIBLE_STORAGE_KEY,
      visible ? "true" : "false",
    );
  } catch { /* ignore */ }
}

function updateAdvancedSettingsVisibility() {
  document.querySelectorAll(".settings-grid").forEach(grid => {
    grid.classList.toggle("show-advanced-settings", settingsAdvancedVisible);
  });
  document.querySelectorAll(".settings-advanced-only").forEach(el => {
    el.classList.toggle("is-hidden", !settingsAdvancedVisible);
    el.toggleAttribute("hidden", !settingsAdvancedVisible);
  });
  const toggle = $("#settingsAdvancedToggle");
  if (toggle) {
    toggle.classList.toggle("is-active", settingsAdvancedVisible);
    toggle.setAttribute("aria-pressed", settingsAdvancedVisible ? "true" : "false");
    toggle.textContent = `Advanced: ${settingsAdvancedVisible ? "On" : "Off"}`;
  }
}

function openFilamentDetail(filamentId) {
  const fil = filamentById(filamentId);
  if (!fil) return;

  const inManual = manualSlots.includes(filamentId);
  const isCandidate = candidateSelection.has(filamentId);
  const isEnabled = enabledFilaments.has(filamentId);
  const isEligible = isGenerationEligibleFilament(fil);

  const bodyHtml = `
    <div class="drawer-filament-header">
      <div class="drawer-filament-swatch" style="background:${fil.hex}"></div>
      <div>
        <div class="drawer-filament-name">${esc(fil.display_name || fil.color_name)}</div>
        <div class="drawer-filament-meta">${esc(fil.manufacturer)} &middot; ${esc(fil.filament_id)}</div>
      </div>
    </div>

    <div class="drawer-section">
      <div class="drawer-section-title">Properties</div>
      <div class="drawer-info-row">
        <span class="drawer-info-label">Hex Color</span>
        <span class="drawer-info-value" style="display:flex;align-items:center;gap:6px">
          <span class="color-chip" style="background:${fil.hex}"></span> ${esc(fil.hex)}
        </span>
      </div>
      <div class="drawer-info-row">
        <span class="drawer-info-label">Manufacturer</span>
        <span class="drawer-info-value">${esc(fil.manufacturer)}</span>
      </div>
      <div class="drawer-info-row">
        <span class="drawer-info-label">Color Name</span>
        <span class="drawer-info-value">${esc(fil.color_name)}</span>
      </div>
      <div class="drawer-info-row">
        <span class="drawer-info-label">Has Profile</span>
        <span class="drawer-info-value">${fil.has_profile ? '<span class="status-pill ok">Yes</span>' : '<span class="status-pill error">No</span>'}</span>
      </div>
      <div class="drawer-info-row">
        <span class="drawer-info-label">Available in Model</span>
        <span class="drawer-info-value">${isEligible ? '<span class="status-pill ok">Yes</span>' : '<span class="status-pill error">No</span>'}</span>
      </div>
      <div class="drawer-info-row">
        <span class="drawer-info-label">Enabled</span>
        <span class="drawer-info-value">${isEnabled ? '<span class="status-pill ok">Yes</span>' : '<span class="status-pill idle">No</span>'}</span>
      </div>
    </div>

    <div class="drawer-section">
      <div class="drawer-section-title">Actions</div>
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        ${creationMode === "manual" && !inManual && isEnabled && fil.has_profile ?
          `<button class="primary-button small" onclick="manualSlots.push('${filamentId}');renderCreationTab();openFilamentDetail('${filamentId}')">Add to Manual Palette</button>` :
          creationMode === "manual" && inManual ?
          `<button class="ghost-button small" onclick="manualSlots=manualSlots.filter(id=>id!=='${filamentId}');renderCreationTab();openFilamentDetail('${filamentId}')">Remove from Manual Palette</button>` : ""}
        ${isEligible ?
          `<button class="ghost-button small" id="filamentAvailabilityActionBtn" type="button">${isEnabled ? "Disable" : "Enable"}</button>` : ""}
      </div>
    </div>
  `;

  openDetailDrawer(esc(fil.color_name), bodyHtml);
  $("#filamentAvailabilityActionBtn")?.addEventListener("click", () => {
    setFilamentEnabled(filamentId, !isEnabled, { reopenDetail: true });
  });
}

// ── Accordion (Settings) ─────────────────────────────────────────────────────

function bindAccordions() {
  $$(".accordion-header").forEach((header) => {
    header.addEventListener("click", () => {
      const group = header.closest(".accordion-group");
      group.classList.toggle("is-open");
    });
  });
}

function updateAccordionSummaries() {
  const geom = $("#accordionSummaryGeometry");
  if (geom) {
    const lh = parseFloat($("#cfgLayerHeight")?.value) || 0.08;
    const tmax = parseFloat($("#cfgTMax")?.value) || 2.5;
    geom.textContent = `${lh} mm layers, ${tmax} mm max`;
  }

  const solver = $("#accordionSummarySolver");
  if (solver) {
    const kmax = parseInt($("#cfgKMax")?.value) || 3;
    solver.textContent = `k=${kmax}`;
  }

  const mesh = $("#accordionSummaryMesh");
  if (mesh) {
    mesh.textContent = "post-solve export";
  }

  // Printer accordion removed — printer config is its own page now
}

// ── White Base/Cap Filament Dropdown ─────────────────────────────────────────

function isWhiteCapEligibleFilament(filament) {
  return !!(
    filament
    && filament.has_profile
    && filament.white_cap_eligible === true
    && filament.exclude_from_model !== true
    && filament.generation_available !== false
  );
}

function filamentSelectLabel(filament) {
  return filament.display_name
    || [filament.manufacturer, filament.color_name].filter(Boolean).join(" ")
    || filament.filament_id;
}

function populateBaseCapDropdowns() {
  const whiteFils = allFilaments.filter(isWhiteCapEligibleFilament);
  const baseEl = $("#cfgBaseFilament");
  if (!baseEl) return;

  const currentBase = config.base_filament || baseEl.value || DEFAULT_BASE_FILAMENT;

  const sorted = [...whiteFils].sort((a, b) =>
    (a.color_name || "").localeCompare(b.color_name || "") ||
    (a.manufacturer || "").localeCompare(b.manufacturer || "")
  );

  if (sorted.length === 0) {
    baseEl.innerHTML = `<option value="">No profiled white filaments</option>`;
    config.base_filament = "";
    config.cap_filament = "__same__";
    baseEl.value = "";
    return;
  }

  const eligibleIds = new Set(sorted.map(f => f.filament_id));
  const fallbackBase = eligibleIds.has(DEFAULT_BASE_FILAMENT)
    ? DEFAULT_BASE_FILAMENT
    : sorted[0].filament_id;
  const resolvedBase = eligibleIds.has(currentBase) ? currentBase : fallbackBase;
  const opts = sorted.map(f =>
    `<option value="${f.filament_id}">${esc(filamentSelectLabel(f))}</option>`
  ).join("");

  baseEl.innerHTML = opts;

  config.base_filament = resolvedBase;
  config.cap_filament = "__same__";
  baseEl.value = resolvedBase;
}

function updateSuggestSlotHint() {
  const totalSlots = printerConfig.ams_slots || 4;
  const bcSlots = getBaseCapSlots();
  const ceiling = Math.max(1, totalSlots - bcSlots);
  // Sync max constraint but don't overwrite user's chosen value
  const input = $("#targetFilamentCount");
  if (input) {
    input.max = ceiling;
    const current = parseInt(input.value);
    if (!current || current > ceiling) {
      input.value = ceiling;
    }
  }

  // Update the hint text
  const hint = $("#suggestSlotHint");
  if (hint) {
    const nCandidates = candidateSelection.size;
    hint.textContent = `${nCandidates} candidates → best ${parseInt(input?.value) || ceiling} of ${ceiling} color slots.`;
  }
}

// ── Settings Tab ─────────────────────────────────────────────────────────────

function renderSettingsTab(options = {}) {
  const { preservePendingUi = false } = options;
  // Some callers are just refreshing the live settings view; let those absorb
  // any in-progress hardcoded-field edits before config -> DOM rendering.
  if (preservePendingUi) readConfigFromUI();
  syncConfigFromModuleState();
  applyLuminanceMode(config.luminance_mode || "standard");
  applyMandatoryProductSettings();
  populateBaseCapDropdowns();
  const baseEl = $("#cfgBaseFilament");
  if (baseEl) baseEl.value = config.base_filament || DEFAULT_BASE_FILAMENT;
  config.cap_filament = "__same__";
  $("#cfgLayerHeight").value = config.layer_height;
  $("#cfgDWb").value = config.d_wb;
  $("#cfgDWcMin").value = minCapLayersFromThickness();
  $("#cfgTMax").value = config.t_max;
  const solvePitchVal = config.image_sample_pitch_mm || 0.20;
  const solvePitchEl = $("#cfgSolvePitch");
  if (solvePitchEl) solvePitchEl.value = solvePitchVal;
  // These elements may be dynamically rendered by modules — guard against null
  const _set = (sel, val) => { const el = $(sel); if (el) el.value = val; };
  const _chk = (sel, val) => { const el = $(sel); if (el) el.checked = val; };
  _set("#cfgSourceResampleKernel", config.source_resample_kernel || "lanczos");
  _set("#cfgDetailCapMaxLayers", config.detail_cap_max_layers ?? 5);
  _set("#cfgCellMode", config.cell_mode || "felzenszwalb");
  _set("#cfgAppearanceModelProvider", config.appearance_model_provider || "photo_stack_bundle");
  _set("#cfgStage1Coarsening", config.stage1_coarsening_factor ?? 1);
  _set("#cfgColorRegionTarget", config.color_region_target_mm ?? 0.60);
  _chk("#cfgStage2FineOverride", config.stage2_fine_override_enabled !== false);
  _chk("#cfgStage2BoundaryMutation", config.stage2_boundary_mutation_enabled);
  setOptionalNumberInput("cfgStage2BoundaryMutationPercentile", config.stage2_boundary_mutation_current_de_percentile);
  setOptionalNumberInput("cfgStage2BoundaryMutationMaxPasses", config.stage2_boundary_mutation_max_passes ?? 1);
  setOptionalNumberInput("cfgStage2BoundaryMutationMinGain", config.stage2_boundary_mutation_min_gain);
  setOptionalNumberInput("cfgStage2BoundaryMutationMinComponent", config.stage2_boundary_mutation_min_component_mm);
  _set("#cfgKMax", config.k_max);
  _set("#cfgDeThreshold", config.de_threshold);
  _set("#cfgSmoothKernel", smoothingRadiusMmFromCells(config.smooth_kernel));
  _chk("#cfgBorder", config.border);
  _set("#cfgBorderWidth", config.border_width_mm);
  _set("#cfgBorderHeight", config.border_height_mm);
  _chk("#cfgUseCorrections", config.use_corrections);
  const capModeEl = $("#cfgCapMode");
  if (capModeEl) capModeEl.value = config.cap_mode || "appearance_bounded_smooth";
  const capDeBudgetEl = $("#cfgBoundaryCapDeBudget");
  if (capDeBudgetEl) capDeBudgetEl.value = config.boundary_cap_de_budget ?? 0.008;
  syncChromaWeightControlFromConfig();
  _set("#cfgGamutMode", normalizeActiveGamutMode(config.gamut_mode || "hull"));
  _chk("#cfgGamutWhiteRescale", config.gamut_white_rescale);
  setSolveModeControlValue(config.luminance_mode || "standard");
  syncBaseShadingLimitControls();
  updateLuminanceModeFields();
  updateCapModeFields();
  updateStage4DetailFields();
  updateBoundaryMutationFields();
  syncDeckGenerationSettingsUI("settings");

  // Printer summary removed — info is in the left rail printer card

  renderPresetBar();
  updateSettingsSummaries();
  updateBorderVisibility();
  updateDerivedParams();
  updateAccordionSummaries();
  updateSuggestSlotHint();
  updateAdvancedSettingsVisibility();
  bindSettingsAutoSyncControls();
}

function updateSolveModeFields() {
  // Solver-specific settings are fully module-driven now.
}

function bindSettingsAutoSyncControls() {
  $$(".settings-grid input, .settings-grid select").forEach((input) => {
    if (input.dataset.settingsAutosyncBound === "1") return;
    input.dataset.settingsAutosyncBound = "1";
    if (input.id === "cfgBaseShadingLimitSlider") {
      input.addEventListener("input", () => {
        syncBaseShadingLimitControls(input.value);
      });
    }
    if (input.id === "cfgChromaWeight") {
      input.addEventListener("input", () => {
        applyChromaWeightSliderInput(input.value);
        updateSettingsSummaries();
        checkPresetModified();
      });
    }
    input.addEventListener("change", () => {
      updateSolveModeFields();
      updateLuminanceModeFields();
      updateCapModeFields();
      updateBoundaryMutationFields();
      updateStage4DetailFields();
      readConfigFromUI();
      updateBorderVisibility();
      updateSettingsSummaries();
      updateDerivedParams();
      updateAccordionSummaries();
      checkPresetModified();
      syncDeckGenerationSettingsUI("settings");
      syncConfigToServer();
    });
  });
}

function updateLuminanceModeFields() {
  const enabled = normalizeLuminanceMode(getSolveModeControlValue()) === "luminance_detail";
  const capMode = $("#cfgCapMode");
  const configAlreadyLuminance = normalizeLuminanceMode(config.luminance_mode) === "luminance_detail";
  if (enabled) {
    if (capMode) {
      if (!capModeForcedByLuminance && !configAlreadyLuminance) {
        saveLastColorCapMode(capMode.value || config.cap_mode || lastColorCapMode);
      }
      capMode.value = "smooth_variable";
      capModeForcedByLuminance = true;
    }
  } else {
    const restored = lastColorCapMode || config.cap_mode || "appearance_bounded_smooth";
    if (capMode && capModeForcedByLuminance) {
      capMode.value = restored;
      capModeForcedByLuminance = false;
    }
  }
  document.querySelectorAll(".luminance-mode-field").forEach(row => {
    row.classList.toggle("is-hidden", !enabled);
    row.classList.toggle("is-disabled", !enabled);
    row.querySelectorAll("input, select, button").forEach(inp => inp.disabled = !enabled);
  });
}

function updateCapModeFields() {
  const capModeEl = $("#cfgCapMode");
  const luminanceMode = normalizeLuminanceMode(getSolveModeControlValue()) === "luminance_detail";
  const appearanceBoundOption = capModeEl?.querySelector('option[value="appearance_bounded_smooth"]');
  if (appearanceBoundOption) appearanceBoundOption.disabled = luminanceMode;
  if (
    luminanceMode
    && capModeEl?.value === "appearance_bounded_smooth"
  ) {
    saveLastColorCapMode("appearance_bounded_smooth");
    capModeEl.value = "smooth_variable";
    capModeForcedByLuminance = true;
  }
  const mode = capModeEl?.value || "appearance_bounded_smooth";
  const isAppearanceBounded = mode === "appearance_bounded_smooth";
  document.querySelectorAll(".cap-mode-field").forEach(row => {
    row.classList.toggle("is-hidden", luminanceMode);
    row.classList.toggle("is-disabled", luminanceMode);
    row.querySelectorAll("input, select").forEach(inp => inp.disabled = luminanceMode);
  });
  // Smooth-mode fields
  document.querySelectorAll(".cap-smooth-field").forEach(row => {
    row.classList.toggle("is-hidden", false);
    row.classList.toggle("is-disabled", false);
    row.querySelectorAll("input, select").forEach(inp => inp.disabled = false);
  });
  document.querySelectorAll(".cap-appearance-bound-field").forEach(row => {
    const visible = isAppearanceBounded && !luminanceMode;
    row.classList.toggle("is-hidden", !visible);
    row.classList.toggle("is-disabled", !visible);
    row.querySelectorAll("input, select").forEach(inp => inp.disabled = !visible);
  });
  document.querySelectorAll(".detail-section-head").forEach(row => {
    row.classList.toggle("is-hidden", false);
    row.classList.toggle("is-disabled", false);
  });
}

function updateBoundaryMutationFields() {
  const enabled = $("#cfgStage2BoundaryMutation")?.checked || false;
  document.querySelectorAll(".boundary-mutation-field").forEach(row => {
    row.classList.toggle("is-hidden", !enabled);
    row.classList.toggle("is-disabled", !enabled);
    row.querySelectorAll("input, select").forEach(inp => inp.disabled = !enabled);
  });
}

function updateStage4DetailFields() {
  document.querySelectorAll(".detail-surface-field").forEach(row => {
    row.classList.toggle("is-hidden", false);
    row.classList.toggle("is-disabled", false);
    row.querySelectorAll("input, select").forEach(inp => inp.disabled = false);
  });
}

function setSettingsSummary(id, title, body) {
  const el = document.getElementById(id);
  if (!el) return;
  if (!body) {
    el.innerHTML = "";
    el.classList.add("is-hidden");
    return;
  }
  el.classList.remove("is-hidden");
  el.innerHTML = `<strong>${esc(title)}:</strong> ${esc(body)}`;
}

function updateSettingsSummaries() {
  if (normalizeLuminanceMode(getSolveModeControlValue()) === "luminance_detail") {
    setSettingsSummary("capSummary", "", "");
  } else {
    const capMode = config.cap_mode || "appearance_bounded_smooth";
    if (capMode === "appearance_bounded_smooth") {
      setSettingsSummary(
        "capSummary",
        "Detail Aware",
        `Keeps the boundary cap structural, preserves appearance against the active model, and places the remaining tonal relief in detail where it stays within ${(config.boundary_cap_de_budget ?? 0.008).toFixed(3)} dE.`
      );
    } else {
      setSettingsSummary(
        "capSummary",
        "Smooth",
        `Smooths the viewing-side cap-height map with a ${smoothingRadiusMmFromCells(config.smooth_kernel).toFixed(2)} mm radius, prioritizing a continuous white cap surface.`
      );
    }
  }

}

function readPrinterConfig() {
  // Printer config is now loaded from the server via loadPrinters().
  printerConfig.ams_slots = printerConfig.ams_units * printerConfig.slots_per_unit;
  printerConfig.white_slots = getBaseCapSlots();
}

// ── Settings Profiles ────────────────────────────────────────────────────────

const SYSTEM_SETTINGS_PROFILE_ID = "system-default";
const SYSTEM_SETTINGS_PROFILE_NAME = "Basic";
const SETTINGS_PROFILE_FORBIDDEN_NAME_CHARS = new Set('<>:"/\\|?*'.split(""));

// Settings persisted in a Settings Profile. Matches the server-side
// _SETTINGS_PROFILE_KEYS. Canonical solve-pitch fields are authoritative.
// Module enablement is stored in the profile modules snapshot.
const SETTINGS_PROFILE_KEYS = [
  // session-owned canonical settings
  "base_filament", "cap_filament",
  "layer_height",
  "image_sample_pitch_mm", "solver_fine_pitch_mm",
  "detail_cap_max_layers",
  "detail_cap_smoothing_enabled",
  "detail_cap_smoothing_exact_speckle_max_px",
  "detail_cap_smoothing_cumulative_component_max_px",
  "detail_cap_smoothing_cumulative_hole_max_px",
  "color_region_target_mm",
  "cell_mode",
  "stage1_coarsening_factor",
  "stage2_fine_override_enabled",
  "stage2_boundary_mutation_enabled",
  "stage2_boundary_mutation_min_gain",
  "stage2_boundary_mutation_min_component_mm",
  "stage2_boundary_mutation_current_de_percentile",
  "stage2_boundary_mutation_max_passes",
  "d_wb", "d_wc_min", "t_max",
  "k_max", "de_threshold", "smooth_kernel",
  "use_corrections",
  "appearance_model_provider", "photo_stack_bundle_path",
  "gamut_mode", "gamut_white_rescale", "model_domain_ingress_lut_path", "chroma_weight",
  "luminance_mode",
  "luminance_base_shading_limit_fraction",
  "luminance_detail_authoring_printability",
  "source_resample_kernel",
  "preprocessing_params",
  "cap_mode", "boundary_cap_de_budget",
];

function _currentSettingsSnapshot() {
  applyMandatoryProductSettings();
  readConfigFromUI();
  const snap = {};
  for (const k of SETTINGS_PROFILE_KEYS) snap[k] = _cloneValue(config[k]);
  return snap;
}

function _configSettingsProfileSnapshot() {
  applyMandatoryProductSettings();
  const snap = {};
  for (const k of SETTINGS_PROFILE_KEYS) snap[k] = _cloneValue(config[k]);
  return snap;
}

function _cloneValue(value) {
  return value == null ? value : JSON.parse(JSON.stringify(value));
}

function _dropRetiredSettingsProfileKeys(settings) {
  const out = { ...(settings || {}) };
  const retiredSubject = "protect" + "_subject";
  const retiredMask = "protect" + "_mask";
  [
    `${retiredSubject}_enabled`,
    `${retiredSubject}_strength`,
    "protect" + "_confidence_floor",
    `${retiredMask}_provider`,
    `${retiredMask}_override`,
  ].forEach((key) => delete out[key]);
  return out;
}

const SETTINGS_PROFILE_DEFAULTS = {};
for (const k of SETTINGS_PROFILE_KEYS) {
  SETTINGS_PROFILE_DEFAULTS[k] = _cloneValue(config[k]);
}

/**
 * Normalize a module toggle snapshot against current module descriptors.
 * Missing module snapshots fall back to each module's default_enabled state.
 */
function _normalizeSettingsProfileModules(modules = null, settings = {}) {
  const next = {};
  const source = modules || {};

  (moduleData || []).forEach((m) => {
    if (Object.prototype.hasOwnProperty.call(source, m.name)) {
      next[m.name] = !!source[m.name];
    } else {
      next[m.name] = !!m.default_enabled;
    }
  });
  return next;
}

function _currentSettingsProfileModulesSnapshot() {
  return _normalizeSettingsProfileModules(moduleState, config);
}

function _settingsProfileModulesEqual(a = {}, b = {}) {
  const names = new Set([
    ...Object.keys(a || {}),
    ...Object.keys(b || {}),
    ...(moduleData || []).map((m) => m.name),
  ]);
  for (const name of names) {
    if (!!a[name] !== !!b[name]) return false;
  }
  return true;
}

function _settingsProfileValuesEqual(a, b) {
  if (a === b) return true;
  if (a == null || b == null) return a === b;

  const aIsArray = Array.isArray(a);
  const bIsArray = Array.isArray(b);
  if (aIsArray || bIsArray) {
    if (!aIsArray || !bIsArray || a.length !== b.length) return false;
    for (let i = 0; i < a.length; i += 1) {
      if (!_settingsProfileValuesEqual(a[i], b[i])) return false;
    }
    return true;
  }

  if (typeof a === "object" || typeof b === "object") {
    if (typeof a !== "object" || typeof b !== "object") return false;
    const keys = new Set([
      ...Object.keys(a || {}),
      ...Object.keys(b || {}),
    ]);
    for (const key of keys) {
      if (!_settingsProfileValuesEqual(a?.[key], b?.[key])) return false;
    }
    return true;
  }

  return String(a) === String(b);
}

async function _applyModuleSnapshot(
  modules,
  settings = {},
  persist = true,
  { refreshViews = true } = {},
) {
  const normalized = _normalizeSettingsProfileModules(modules, settings);
  if (moduleData.length === 0) {
    moduleState = { ...normalized };
    return moduleState;
  }

  if (persist && apiConnected) {
    const response = await setModuleState(normalized);
    moduleState = response.state || normalized;
  } else {
    moduleState = { ...normalized };
  }

  syncConfigFromModuleState();
  renderModulePanel();
  renderDynamicSettings();
  if (refreshViews) refreshModuleDrivenViews();
  return moduleState;
}

function _applySettingsProfileToConfig(settings) {
  const source = _dropRetiredSettingsProfileKeys(settings);
  for (const k of SETTINGS_PROFILE_KEYS) {
    let value = Object.prototype.hasOwnProperty.call(source, k)
      ? _cloneValue(source[k])
      : _cloneValue(SETTINGS_PROFILE_DEFAULTS[k]);
    if (k === "gamut_mode") value = normalizeActiveGamutMode(value);
    config[k] = value;
  }
}

function _setLoadedSettingsProfile(record, snapshot = null) {
  loadedProfileRef = record
    ? { id: record.id, kind: record.kind, name: record.name }
    : null;
  if (!record) {
    loadedProfileSnapshot = null;
    return;
  }
  loadedProfileSnapshot = _cloneValue(snapshot || {
    settings: _configSettingsProfileSnapshot(),
    modules: _currentSettingsProfileModulesSnapshot(),
  });
  loadedProfileSnapshot.settings = _dropRetiredSettingsProfileKeys(
    loadedProfileSnapshot.settings
  );
}

function allSettingsProfiles() {
  return temporarySettingsProfile
    ? [temporarySettingsProfile, ...settingsProfiles]
    : [...settingsProfiles];
}

function findSettingsProfile(profileId) {
  return allSettingsProfiles().find((profile) => profile.id === profileId) || null;
}

function isSettingsProfileModified() {
  if (!loadedProfileSnapshot) return false;
  const current = _currentSettingsSnapshot();
  for (const k of SETTINGS_PROFILE_KEYS) {
    if (!_settingsProfileValuesEqual(current[k], loadedProfileSnapshot.settings?.[k])) return true;
  }
  if (
    !_settingsProfileModulesEqual(
      _currentSettingsProfileModulesSnapshot(),
      loadedProfileSnapshot.modules || {}
    )
  ) return true;
  return false;
}

function findSettingsProfileByName(name) {
  const trimmed = String(name || "").trim().toLocaleLowerCase();
  if (!trimmed) return null;
  return allSettingsProfiles().find((profile) => (profile.name || "").trim().toLocaleLowerCase() === trimmed) || null;
}

function _settingsProfileBadges(profile, { modifiedLoaded = false } = {}) {
  if (!profile) return [];
  const badges = [];
  if (profile.kind === "temporary") {
    badges.push({ label: "TEMP", accent: false, warn: true });
  }
  if (profile.id === SYSTEM_SETTINGS_PROFILE_ID) {
    badges.push({ label: "system", accent: false, warn: false });
  }
  if (profile.id === userDefaultProfileId) {
    badges.push({ label: "startup", accent: true, warn: false });
  }
  return badges;
}

function _settingsProfileBadgesHtml(profile, { modifiedLoaded = false, mini = true } = {}) {
  return _settingsProfileBadges(profile, { modifiedLoaded }).map((badge) => {
    const classes = [
      mini ? "settings-profile-mini-badge" : "settings-profile-badge",
      badge.accent ? "is-accent" : "",
      badge.warn ? "is-warn" : "",
    ].filter(Boolean).join(" ");
    return `<span class="${classes}">${esc(badge.label)}</span>`;
  }).join("");
}

function describeSettingsProfileNameInput(name, {
  currentProfileId = null,
  allowReplace = false,
} = {}) {
  if (typeof name !== "string") {
    return { valid: false, error: "Settings Profile name must be text", trimmed: "", duplicate: null };
  }
  if (name.trim().length === 0) {
    return { valid: false, error: "Settings Profile name cannot be empty", trimmed: "", duplicate: null };
  }
  if (name !== name.trim()) {
    return {
      valid: false,
      error: "Settings Profile name cannot start or end with whitespace",
      trimmed: name.trim(),
      duplicate: null,
    };
  }

  const trimmed = name.trim();
  if (trimmed.endsWith(".")) {
    return { valid: false, error: "Settings Profile name cannot end with a period", trimmed, duplicate: null };
  }
  for (const ch of trimmed) {
    if (SETTINGS_PROFILE_FORBIDDEN_NAME_CHARS.has(ch) || ch.charCodeAt(0) < 32) {
      return {
        valid: false,
        error: 'Settings Profile name cannot contain < > : " / \\ | ? *',
        trimmed,
        duplicate: null,
      };
    }
  }

  const duplicate = settingsProfiles.find((profile) => {
    if (profile.id === currentProfileId) return false;
    return (profile.name || "").trim().toLocaleLowerCase() === trimmed.toLocaleLowerCase();
  }) || null;

  if (!duplicate) {
    return { valid: true, error: null, trimmed, duplicate: null, replaceExisting: false };
  }
  if (!allowReplace) {
    return {
      valid: false,
      error: `A Settings Profile named "${trimmed}" already exists`,
      trimmed,
      duplicate,
      replaceExisting: false,
    };
  }
  if (duplicate.kind !== "named") {
    return {
      valid: false,
      error: `The system Settings Profile "${trimmed}" cannot be replaced`,
      trimmed,
      duplicate,
      replaceExisting: false,
    };
  }
  return {
    valid: true,
    error: null,
    trimmed,
    duplicate,
    replaceExisting: true,
  };
}

function _refreshSettingsProfilesFromResponse(data) {
  settingsProfiles = Array.isArray(data?.profiles) ? data.profiles : [];
  userDefaultProfileId = data?.user_default_profile_id || SYSTEM_SETTINGS_PROFILE_ID;
}

function validateSettingsProfileNameLocal(name, currentProfileId = null) {
  return describeSettingsProfileNameInput(name, { currentProfileId }).error;
}

function _captureLiveSettingsProfileState() {
  return {
    config: _configSettingsProfileSnapshot(),
    modules: _currentSettingsProfileModulesSnapshot(),
    loadedProfileRef: _cloneValue(loadedProfileRef),
    loadedProfileSnapshot: _cloneValue(loadedProfileSnapshot),
    temporarySettingsProfile: _cloneValue(temporarySettingsProfile),
  };
}

async function _restoreLiveSettingsProfileState(snapshot, { syncServer = true } = {}) {
  _applySettingsProfileToConfig(snapshot.config || {});
  await _applyModuleSnapshot(
    snapshot.modules || {},
    snapshot.config || {},
    false,
    { refreshViews: false },
  );
  temporarySettingsProfile = _cloneValue(snapshot.temporarySettingsProfile);
  loadedProfileRef = _cloneValue(snapshot.loadedProfileRef);
  loadedProfileSnapshot = _cloneValue(snapshot.loadedProfileSnapshot);
  renderSettingsTab();
  if (syncServer && apiConnected) {
    await syncConfigToServer({ throwOnError: true });
    await _applyModuleSnapshot(
      snapshot.modules || {},
      snapshot.config || {},
      true,
      { refreshViews: false },
    );
  }
  renderSettingsProfileBar();
}

function _runSettingsMetadata(body) {
  const metadata = body?.run_metadata && typeof body.run_metadata === "object"
    ? body.run_metadata
    : {};
  // Live solve cards keep the same recipe snapshot on the card itself, while
  // archived runs expose it through the optional run_metadata envelope.
  const directRecipeSnapshot = body?.recipe_snapshot
    && typeof body.recipe_snapshot === "object"
    ? body.recipe_snapshot
    : {};
  const durableSnapshot = metadata.recipe_snapshot?.profile_snapshot
    && typeof metadata.recipe_snapshot.profile_snapshot === "object"
    ? metadata.recipe_snapshot.profile_snapshot
    : directRecipeSnapshot.profile_snapshot
      && typeof directRecipeSnapshot.profile_snapshot === "object"
      ? directRecipeSnapshot.profile_snapshot
      : {};
  const diagnostics = metadata.solve_start_diagnostics
    && typeof metadata.solve_start_diagnostics === "object"
    ? metadata.solve_start_diagnostics
    : body?.result?.solve_start_diagnostics
      && typeof body.result.solve_start_diagnostics === "object"
      ? body.result.solve_start_diagnostics
      : body?.results?.solve_start_diagnostics
        && typeof body.results.solve_start_diagnostics === "object"
        ? body.results.solve_start_diagnostics
        : {};
  return { metadata, durableSnapshot, diagnostics };
}

function _settingsSnapshotFromRunPayload(body) {
  const { metadata, durableSnapshot, diagnostics } = _runSettingsMetadata(body);
  const source = {
    ...(body?.config && typeof body.config === "object" ? body.config : {}),
    ...(metadata.config && typeof metadata.config === "object" ? metadata.config : {}),
    ...(diagnostics.resolved_settings && typeof diagnostics.resolved_settings === "object"
      ? diagnostics.resolved_settings : {}),
    ...(durableSnapshot.settings && typeof durableSnapshot.settings === "object"
      ? durableSnapshot.settings : {}),
  };
  if (source.luminance_base_shading_limit_fraction == null
      && source.luminance_handler_optical_authority_fraction != null) {
    source.luminance_base_shading_limit_fraction = source.luminance_handler_optical_authority_fraction;
  }
  const settings = {};
  for (const key of SETTINGS_PROFILE_KEYS) {
    settings[key] = Object.prototype.hasOwnProperty.call(source, key)
      ? _cloneValue(source[key])
      : _cloneValue(SETTINGS_PROFILE_DEFAULTS[key]);
  }
  const preprocessingParams = {
    ...(source.preprocessing_params && typeof source.preprocessing_params === "object"
      ? _cloneValue(source.preprocessing_params) : {}),
  };
  const diagnosticModuleSettings = diagnostics.module_settings
    && typeof diagnostics.module_settings === "object"
    ? diagnostics.module_settings
    : {};
  for (const [moduleId, values] of Object.entries(diagnosticModuleSettings)) {
    if (!values || typeof values !== "object") continue;
    preprocessingParams[moduleId] = {
      ...(preprocessingParams[moduleId] || {}),
      ..._cloneValue(values),
    };
  }
  settings.preprocessing_params = preprocessingParams;
  return {
    settings: _dropRetiredSettingsProfileKeys(settings),
    metadata,
    durableSnapshot,
    diagnostics,
  };
}

function _modulesSnapshotFromRunPayload(settings, durableSnapshot, diagnostics) {
  const durableModules = durableSnapshot.modules && typeof durableSnapshot.modules === "object"
    ? durableSnapshot.modules
    : null;
  const diagnosticModules = diagnostics.module_state && typeof diagnostics.module_state === "object"
    ? diagnostics.module_state
    : null;
  const normalized = _normalizeSettingsProfileModules(
    durableModules || diagnosticModules || null,
    settings,
  );
  // A durable recipe snapshot is the captured frontend truth. Diagnostics are
  // only a compatibility fallback for older runs that predate that snapshot.
  const active = !durableModules
    && diagnostics.active_modules && typeof diagnostics.active_modules === "object"
    ? diagnostics.active_modules
    : {};
  for (const [slot, names] of Object.entries(active)) {
    if (!Array.isArray(names)) continue;
    const slotModules = (moduleData || []).filter((module) => module.slot === slot);
    if (!slotModules.length) continue;
    slotModules.forEach((module) => { normalized[module.name] = false; });
    names.forEach((name) => {
      if (Object.prototype.hasOwnProperty.call(normalized, name)) normalized[name] = true;
    });
  }
  return normalized;
}

function buildTemporarySettingsProfileFromRun(body, source = {}) {
  const extracted = _settingsSnapshotFromRunPayload(body);
  const label = String(
    source.label || body?.label || extracted.durableSnapshot.name || "Solved run settings",
  ).trim() || "Solved run settings";
  const metadata = extracted.metadata || {};
  const profile = {
    id: "temporary-run-settings",
    kind: "temporary",
    name: label,
    settings: extracted.settings,
    modules: _modulesSnapshotFromRunPayload(
      extracted.settings,
      extracted.durableSnapshot,
      extracted.diagnostics,
    ),
    source: {
      kind: source.kind || (source.save_id ? "saved-run" : "solve-card"),
      save_id: source.save_id || null,
      tier: source.tier || null,
      run_id: source.run_id || body?.card_id || null,
      label,
    },
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  };
  profile.profile_ref = _cloneValue(
    metadata.profile_ref
      || body?.profile_ref
      || extracted.durableSnapshot.profile_ref
      || null,
  );
  return profile;
}

async function _doLoadSettingsProfile(profile, { syncServer = true } = {}) {
  if (!profile) return false;
  if (syncServer && apiConnected) {
    await _configSyncChain.catch(() => {});
  }
  const previous = _captureLiveSettingsProfileState();
  let configSynced = false;
  try {
    _applySettingsProfileToConfig(profile.settings || {});
    await _applyModuleSnapshot(
      profile.modules,
      profile.settings || {},
      false,
      { refreshViews: false },
    );
    renderSettingsTab();
    if (syncServer && apiConnected) {
      await syncConfigToServer({ throwOnError: true });
      configSynced = true;
      await _applyModuleSnapshot(
        profile.modules,
        profile.settings || {},
        true,
        { refreshViews: false },
      );
    }
  } catch (error) {
    try {
      await _restoreLiveSettingsProfileState(previous, { syncServer: configSynced });
    } catch (restoreError) {
      console.error("[settings profiles] rollback failed:", restoreError);
    }
    throw error;
  }
  temporarySettingsProfile = profile.kind === "temporary" ? _cloneValue(profile) : null;
  _setLoadedSettingsProfile(profile, {
    settings: _configSettingsProfileSnapshot(),
    modules: _currentSettingsProfileModulesSnapshot(),
  });
  renderSettingsProfileBar();
  return true;
}

async function _loadTemporarySettingsFromRun(body, source = {}) {
  const profile = buildTemporarySettingsProfileFromRun(body, source);
  const actionLabel = `loading settings from “${profile.source.label}”`;
  const proceed = await _guardSettingsProfileTransition(actionLabel);
  if (!proceed) return false;
  await _doLoadSettingsProfile(profile);
  showToast(`Loaded settings from “${profile.source.label}” as TEMP`, "success");
  return true;
}

async function loadSettingsProfiles() {
  try {
    const data = await fetchSettingsProfiles();
    _refreshSettingsProfilesFromResponse(data);
    const preferredProfile = findSettingsProfile(userDefaultProfileId)
      || findSettingsProfile(data?.system_profile_id || SYSTEM_SETTINGS_PROFILE_ID)
      || settingsProfiles[0]
      || null;
    if (preferredProfile) {
      await _doLoadSettingsProfile(preferredProfile, { syncServer: true });
    }
  } catch (e) {
    console.warn("[settings profiles] load failed:", e.message);
    if (!loadedProfileSnapshot) {
      _setLoadedSettingsProfile({
        id: SYSTEM_SETTINGS_PROFILE_ID,
        kind: "system",
        name: SYSTEM_SETTINGS_PROFILE_NAME,
      }, {
        settings: _configSettingsProfileSnapshot(),
        modules: _currentSettingsProfileModulesSnapshot(),
      });
    }
  }
  renderSettingsTab();
}

async function loadPresets() {
  return loadSettingsProfiles();
}

function renderSettingsProfileBar() {
  const profile = loadedProfileRef ? findSettingsProfile(loadedProfileRef.id) || loadedProfileRef : null;
  const modified = isSettingsProfileModified();
  const nameEl = $("#settingsProfileName");
  const statusEl = $("#settingsProfileStatus");
  const sourceEl = $("#settingsProfileSource");

  if (nameEl) nameEl.textContent = profile?.name || SYSTEM_SETTINGS_PROFILE_NAME;
  if (sourceEl) {
    sourceEl.textContent = profile?.kind === "temporary"
      ? `From solved run: ${profile.source?.label || profile.name || "unknown"}`
      : "";
    sourceEl.classList.toggle("is-hidden", profile?.kind !== "temporary");
  }
  if (statusEl) {
    const badges = [];
    badges.push(_settingsProfileBadgesHtml(profile, { mini: false }));
    if (modified) {
      badges.push('<span class="settings-profile-badge is-warn">modified</span>');
    }
    statusEl.innerHTML = badges.filter(Boolean).join("");
  }

  const saveBtn = $("#settingsProfileSaveBtn");
  if (saveBtn) {
    saveBtn.disabled = !modified;
    saveBtn.title = modified ? "" : "No changes to save";
  }
}

function checkPresetModified() {
  renderSettingsProfileBar();
}

function _renderSettingsProfileList(listEl, selectedId, {
  allowSelectLoaded = true,
  showActions = true,
  editingProfileId = null,
  editingName = "",
} = {}) {
  listEl.innerHTML = allSettingsProfiles().map((profile) => {
    const isLoaded = loadedProfileRef?.id === profile.id;
    const isEditing = editingProfileId === profile.id;
    const canRename = profile.kind === "named";
    const canDelete = profile.kind === "named" && !isLoaded;
    const isStartup = profile.id === userDefaultProfileId;
    const canSetStartup = profile.kind !== "temporary" && !isStartup;
    const sourceLabel = profile.kind === "temporary" && profile.source?.label
      ? `<span class="settings-profile-modal-item-source">From ${esc(profile.source.label)}</span>`
      : "";
    const itemClasses = [
      "settings-profile-modal-item",
      selectedId === profile.id ? "is-selected" : "",
      isLoaded ? "is-current" : "",
      isEditing ? "is-editing" : "",
      !allowSelectLoaded && isLoaded ? "is-disabled" : "",
    ].filter(Boolean).join(" ");
    return `
      <div
        class="${itemClasses}"
        data-profile-id="${esc(profile.id)}"
        tabindex="0"
        role="button"
      >
        <span class="settings-profile-modal-item-main">
          <span class="settings-profile-modal-item-head">
            ${isEditing ? `
              <input
                type="text"
                class="control-input settings-profile-inline-name-input"
                data-profile-rename-input="${esc(profile.id)}"
                value="${esc(editingName)}"
                aria-label="Rename profile"
                autocomplete="off"
                spellcheck="false"
              >
            ` : `
              <span class="settings-profile-modal-item-name">${esc(profile.name)}</span>
            `}
          </span>
          ${sourceLabel}
          <span class="settings-profile-modal-item-badges">${_settingsProfileBadgesHtml(profile)}</span>
        </span>
        ${showActions ? `
          <span class="settings-profile-modal-item-actions">
            ${isEditing ? `
              <button type="button" class="ghost-button xxs settings-profile-icon-btn" data-profile-action="rename_save" data-profile-id="${esc(profile.id)}" title="Save name" aria-label="Save name">
                <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3.2 8.3 6.4 11.4 12.8 4.8"></path></svg>
              </button>
              <button type="button" class="ghost-button xxs settings-profile-icon-btn" data-profile-action="rename_cancel" data-profile-id="${esc(profile.id)}" title="Cancel rename" aria-label="Cancel rename">
                <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 4l8 8M12 4 4 12"></path></svg>
              </button>
            ` : `
              ${canRename ? `
                <button type="button" class="ghost-button xxs settings-profile-icon-btn" data-profile-action="rename" data-profile-id="${esc(profile.id)}" title="Rename profile" aria-label="Rename profile">
                  <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3 11.5V13h1.5L11.8 5.7 10.3 4.2 3 11.5Z"></path><path d="M9.9 4.6 11.4 6.1"></path></svg>
                </button>
              ` : ""}
              ${profile.kind !== "temporary" ? `
                <button type="button" class="ghost-button xxs settings-profile-icon-btn${canSetStartup ? "" : " is-active"}" data-profile-action="set_default" data-profile-id="${esc(profile.id)}" title="${canSetStartup ? "Set as startup default" : "Startup default"}" aria-label="${canSetStartup ? "Set as startup default" : "Startup default"}">
                  <svg viewBox="0 0 16 16" aria-hidden="true"><path d="m8 2 1.8 3.7 4.2.6-3 2.9.7 4.2L8 11.5l-3.7 1.9.7-4.2-3-2.9 4.2-.6L8 2Z"></path></svg>
                </button>
              ` : ""}
              ${canDelete ? `
                <button type="button" class="ghost-button xxs danger settings-profile-icon-btn" data-profile-action="delete" data-profile-id="${esc(profile.id)}" title="Delete profile" aria-label="Delete profile">
                  <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 4l8 8M12 4 4 12"></path></svg>
                </button>
              ` : ""}
            `}
          </span>
        ` : ""}
      </div>
    `;
  }).join("");
}

function _settingsProfileSelectionHtml(profile) {
  const isLoaded = loadedProfileRef?.id === profile?.id;
  const modified = isSettingsProfileModified();
  const loadLabel = isLoaded && modified ? "Reload Saved Version" : "Load";
  const loadDisabled = !profile || (isLoaded && !modified);
  const source = profile?.kind === "temporary"
    ? `From solved run: ${profile.source?.label || profile.name || "unknown"}`
    : "";

  return `
    <div class="settings-profile-modal-selection-field">
      <div class="settings-profile-modal-selection-label">Selected Profile</div>
      <div class="settings-profile-modal-selection-value${profile ? "" : " is-empty"}">${profile ? esc(profile.name) : ""}</div>
      ${source ? `<div class="settings-profile-modal-selection-source">${esc(source)}</div>` : ""}
    </div>
    <div class="settings-profile-modal-selection-actions">
      <button class="primary-button" data-browser-action="load"${loadDisabled ? " disabled" : ""}>${esc2(loadLabel)}</button>
      <button class="ghost-button" data-browser-action="cancel">Cancel</button>
    </div>
  `;
}

async function showSettingsProfileBrowserModal({
  title = "Settings Profiles",
  selectedProfileId = null,
  onAction = null,
} = {}) {
  return new Promise((resolve) => {
    const overlay = $("#settingsProfileModal");
    const titleEl = $("#settingsProfileModalTitle");
    const listEl = $("#settingsProfileModalList");
    const selectionEl = $("#settingsProfileModalSelection");
    const closeBtn = $("#settingsProfileModalClose");
    const loadRunBtn = $("#settingsProfileModalLoadRunBtn");
    const restoreBtn = $("#settingsProfileModalRestoreBtn");
    if (!overlay || !titleEl || !listEl || !selectionEl || !closeBtn || !loadRunBtn || !restoreBtn) {
      resolve(null);
      return;
    }

    let selectedId = selectedProfileId;
    let pendingDeleteId = null;
    let editingProfileId = null;
    let editingName = "";
    let focusRenameInput = false;
    let busy = false;

    const cancelInlineRename = () => {
      editingProfileId = null;
      editingName = "";
      focusRenameInput = false;
    };

    const currentRenameState = () => {
      if (!editingProfileId) return null;
      const profile = findSettingsProfile(editingProfileId);
      if (!profile) return null;
      const described = describeSettingsProfileNameInput(editingName, { currentProfileId: editingProfileId });
      return {
        profile,
        ...described,
        unchanged: described.trimmed === profile.name,
      };
    };

    const syncInlineRenameUi = () => {
      const input = listEl.querySelector("[data-profile-rename-input]");
      const saveBtn = listEl.querySelector('[data-profile-action="rename_save"]');
      if (!input) return;
      const state = currentRenameState();
      const error = state?.error || "";
      const disableSave = !state || !state.valid || state.unchanged;
      input.classList.toggle("is-invalid", !!error);
      input.setAttribute("aria-invalid", error ? "true" : "false");
      input.title = error || "";
      if (saveBtn) {
        saveBtn.disabled = disableSave;
        saveBtn.title = error || (state?.unchanged ? "Name unchanged" : "Save name");
        saveBtn.setAttribute("aria-label", error || (state?.unchanged ? "Name unchanged" : "Save name"));
      }
    };

    const commitInlineRename = async () => {
      const state = currentRenameState();
      if (!state || !state.profile) return false;
      if (state.unchanged) {
        cancelInlineRename();
        render();
        return false;
      }
      if (!state.valid) {
        syncInlineRenameUi();
        const input = listEl.querySelector("[data-profile-rename-input]");
        input?.focus();
        input?.select();
        return false;
      }
      const response = await updateSettingsProfile(state.profile.id, {
        name: state.trimmed,
        settings: state.profile.settings,
        modules: state.profile.modules,
      });
      _refreshSettingsProfilesFromResponse(response);
      const updated = findSettingsProfile(state.profile.id);
      if (updated && loadedProfileRef?.id === updated.id) {
        loadedProfileRef = { ...loadedProfileRef, name: updated.name };
      }
      renderSettingsTab({ preservePendingUi: true });
      cancelInlineRename();
      showToast(`Renamed Settings Profile to "${state.trimmed}"`, "success");
      render();
      return true;
    };

    const renderList = () => {
      _renderSettingsProfileList(listEl, selectedId, {
        editingProfileId,
        editingName,
      });

      listEl.querySelectorAll(".settings-profile-modal-item").forEach((button) => {
        button.onclick = (event) => {
          if (event.target.closest("[data-profile-action]")) return;
          if (event.target.closest("[data-profile-rename-input]")) return;
          if (editingProfileId) cancelInlineRename();
          selectedId = button.dataset.profileId || null;
          pendingDeleteId = null;
          render();
        };
        button.onkeydown = (event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            if (editingProfileId) cancelInlineRename();
            selectedId = button.dataset.profileId || null;
            pendingDeleteId = null;
            render();
          }
        };
      });
      const renameInput = listEl.querySelector("[data-profile-rename-input]");
      if (renameInput) {
        renameInput.oninput = () => {
          editingName = renameInput.value;
          syncInlineRenameUi();
        };
        renameInput.onkeydown = async (event) => {
          if (event.key === "Enter") {
            event.preventDefault();
            if (busy) return;
            busy = true;
            try {
              await commitInlineRename();
            } finally {
              busy = false;
            }
            return;
          }
          if (event.key === "Escape") {
            event.preventDefault();
            cancelInlineRename();
            render();
          }
        };
        syncInlineRenameUi();
        if (focusRenameInput) {
          focusRenameInput = false;
          setTimeout(() => {
            renameInput.focus();
            renameInput.select();
          }, 0);
        }
      }
      listEl.querySelectorAll("[data-profile-action]").forEach((button) => {
        button.onclick = async (event) => {
          event.stopPropagation();
          if (busy) return;
          const action = button.dataset.profileAction;
          const profileId = button.dataset.profileId || null;
          if (action === "rename") {
            const profile = findSettingsProfile(profileId);
            if (!profile) return;
            editingProfileId = profileId;
            editingName = profile.name;
            selectedId = profileId;
            pendingDeleteId = null;
            focusRenameInput = true;
            render();
            return;
          }
          if (action === "rename_cancel") {
            cancelInlineRename();
            render();
            return;
          }
          if (action === "rename_save") {
            busy = true;
            try {
              await commitInlineRename();
            } finally {
              busy = false;
            }
            return;
          }
          if (action === "delete") {
            if (pendingDeleteId === profileId) {
              if (typeof onAction === "function") {
                busy = true;
                try {
                  const outcome = await onAction({ action, profileId, selectedProfileId: selectedId });
                  selectedId = outcome?.selectedProfileId || selectedId;
                  pendingDeleteId = null;
                  if (outcome?.close) {
                    close(outcome.result || null);
                    return;
                  }
                  render();
                } finally {
                  busy = false;
                }
                return;
              }
              close({ action, profileId });
              return;
            }
            pendingDeleteId = profileId;
            render();
            return;
          }
          if (editingProfileId) cancelInlineRename();
          if (typeof onAction === "function") {
            busy = true;
            try {
              const outcome = await onAction({ action, profileId, selectedProfileId: selectedId });
              selectedId = outcome?.selectedProfileId || selectedId;
              pendingDeleteId = null;
              if (outcome?.close) {
                close(outcome.result || null);
                return;
              }
              render();
            } finally {
              busy = false;
            }
            return;
          }
          pendingDeleteId = null;
          close({ action, profileId });
        };
        if (button.dataset.profileAction === "delete" && pendingDeleteId === button.dataset.profileId) {
          button.classList.add("is-pending-delete");
          button.title = "Click again to delete";
          button.setAttribute("aria-label", "Click again to delete");
        }
      });
    };

    const renderSelection = () => {
      const profile = findSettingsProfile(selectedId);
      selectionEl.innerHTML = _settingsProfileSelectionHtml(profile);
      selectionEl.querySelectorAll("[data-browser-action]").forEach((button) => {
        button.onclick = () => {
          const action = button.dataset.browserAction;
          if (action === "cancel") {
            close(null);
            return;
          }
          close({ action, profileId: selectedId });
        };
      });
    };

    const render = () => {
      renderList();
      renderSelection();
    };

    const close = (result) => {
      overlay.classList.add("is-hidden");
      overlay.setAttribute("aria-hidden", "true");
      resolve(result);
    };

    titleEl.textContent = title || "Settings Profiles";
    render();

    overlay.classList.remove("is-hidden");
    overlay.setAttribute("aria-hidden", "false");

    restoreBtn.onclick = () => close({ action: "restore", profileId: selectedId });
    loadRunBtn.onclick = () => close({ action: "load_saved_run", profileId: selectedId });
    closeBtn.onclick = () => close(null);
    overlay.onclick = (event) => {
      if (event.target === overlay) close(null);
    };
  });
}

async function showSettingsProfileSaveAsModal({
  title = "Save Settings Profile As",
  defaultValue = "",
  currentProfileId = null,
} = {}) {
  return new Promise((resolve) => {
    const overlay = $("#settingsProfileSaveModal");
    const titleEl = $("#settingsProfileSaveModalTitle");
    const listEl = $("#settingsProfileSaveList");
    const inputEl = $("#settingsProfileSaveName");
    const statusEl = $("#settingsProfileSaveStatus");
    const submitBtn = $("#settingsProfileSaveModalSubmit");
    const closeBtn = $("#settingsProfileSaveModalClose");
    const cancelBtn = $("#settingsProfileSaveModalCancel");
    if (!overlay || !titleEl || !listEl || !inputEl || !statusEl || !submitBtn || !closeBtn || !cancelBtn) {
      resolve(null);
      return;
    }

    let submission = null;
    let selectedId = null;

    const renderList = () => {
      _renderSettingsProfileList(listEl, selectedId, { showActions: false });
      listEl.querySelectorAll(".settings-profile-modal-item").forEach((button) => {
        button.onclick = () => {
          const profile = findSettingsProfile(button.dataset.profileId || "");
          if (!profile) return;
          selectedId = profile.id;
          inputEl.value = profile.name || "";
          renderList();
          renderStatus();
          inputEl.focus();
          inputEl.select();
        };
      });
    };

    const renderStatus = () => {
      const state = describeSettingsProfileNameInput(inputEl.value, {
        currentProfileId,
        allowReplace: true,
      });
      submission = state.valid
        ? {
            name: state.trimmed,
            replaceProfileId: state.replaceExisting ? state.duplicate?.id || null : null,
          }
        : null;

      if (!inputEl.value.trim()) {
        statusEl.className = "settings-profile-save-status";
        statusEl.innerHTML = "";
        submitBtn.disabled = true;
        submitBtn.textContent = "Save";
        return;
      }

      if (!state.valid) {
        statusEl.className = "settings-profile-save-status is-error";
        statusEl.textContent = state.error;
        submitBtn.disabled = true;
        submitBtn.textContent = "Save";
        return;
      }

      if (state.replaceExisting && state.duplicate) {
        statusEl.className = "settings-profile-save-status";
        statusEl.textContent = "";
        submitBtn.disabled = false;
        submitBtn.textContent = "Overwrite";
        return;
      }

      statusEl.className = "settings-profile-save-status";
      statusEl.innerHTML = "";
      submitBtn.disabled = false;
      submitBtn.textContent = "Save";
    };

    const close = (result) => {
      overlay.classList.add("is-hidden");
      overlay.setAttribute("aria-hidden", "true");
      resolve(result);
    };

    titleEl.textContent = title;
    inputEl.value = defaultValue || "";
    selectedId = findSettingsProfileByName(defaultValue || "")?.id || null;
    renderList();
    renderStatus();

    overlay.classList.remove("is-hidden");
    overlay.setAttribute("aria-hidden", "false");

    inputEl.oninput = () => {
      selectedId = findSettingsProfileByName(inputEl.value || "")?.id || null;
      renderList();
      renderStatus();
    };
    inputEl.onkeydown = (event) => {
      if (event.key === "Enter" && submission) {
        event.preventDefault();
        close(submission);
      }
    };
    submitBtn.onclick = () => {
      if (submission) close(submission);
    };
    cancelBtn.onclick = () => close(null);
    closeBtn.onclick = () => close(null);
    overlay.onclick = (event) => {
      if (event.target === overlay) close(null);
    };

    setTimeout(() => {
      inputEl.focus();
      inputEl.select();
    }, 0);
  });
}

async function _guardSettingsProfileTransition(actionLabel = "loading another Settings Profile") {
  if (!isSettingsProfileModified()) return true;
  const choice = await appChoice(
    `This Settings Profile has unsaved changes. What would you like to do before ${actionLabel}?`,
    [
      { label: "Save As", value: "save_as", kind: "primary" },
      { label: "Discard", value: "discard" },
      { label: "Cancel", value: "cancel" },
    ],
    { title: "Unsaved Settings Profile" },
  );
  if (choice === "discard") return true;
  if (choice === "save_as") {
    return !!(await _ensureDraftSavedAsSettingsProfile(
      loadedProfileRef?.kind === "named" ? loadedProfileRef.name : ""
    ));
  }
  return false;
}

async function _saveDraftAsSettingsProfileWithName(name) {
  const beforeIds = new Set(settingsProfiles.map((profile) => profile.id));
  const response = await createSettingsProfile({
    name,
    settings: _currentSettingsSnapshot(),
    modules: _currentSettingsProfileModulesSnapshot(),
  });
  _refreshSettingsProfilesFromResponse(response);
  const created = settingsProfiles.find((profile) => !beforeIds.has(profile.id))
    || settingsProfiles.find((profile) => (profile.name || "").toLocaleLowerCase() === name.toLocaleLowerCase())
    || null;
  if (created) {
    temporarySettingsProfile = null;
    _setLoadedSettingsProfile(created, {
      settings: _currentSettingsSnapshot(),
      modules: _currentSettingsProfileModulesSnapshot(),
    });
  }
  renderSettingsTab({ preservePendingUi: true });
  return created;
}

async function _overwriteSettingsProfile(profile, { nameOverride = null } = {}) {
  if (!profile || profile.kind !== "named") return null;
  const response = await updateSettingsProfile(profile.id, {
    name: nameOverride || profile.name,
    settings: _currentSettingsSnapshot(),
    modules: _currentSettingsProfileModulesSnapshot(),
  });
  _refreshSettingsProfilesFromResponse(response);
  const updated = findSettingsProfile(profile.id);
  if (updated) {
    temporarySettingsProfile = null;
    _setLoadedSettingsProfile(updated, {
      settings: _currentSettingsSnapshot(),
      modules: _currentSettingsProfileModulesSnapshot(),
    });
  }
  renderSettingsTab({ preservePendingUi: true });
  return updated;
}

async function _overwriteLoadedNamedSettingsProfile(nameOverride = null) {
  const current = loadedProfileRef ? findSettingsProfile(loadedProfileRef.id) : null;
  return _overwriteSettingsProfile(current, { nameOverride });
}

async function _ensureDraftSavedAsSettingsProfile(defaultName = "") {
  const result = await showSettingsProfileSaveAsModal({
    title: "Save Settings Profile As",
    defaultValue: defaultName,
    currentProfileId: null,
  });
  if (!result) return null;

  if (result.replaceProfileId) {
    const existing = findSettingsProfile(result.replaceProfileId);
    if (!existing) {
      showToast("That Settings Profile is no longer available", "error");
      return null;
    }
    const confirmed = await appConfirm(
      `Replace "${existing.name}" with the current draft?`,
      { ok: "Replace", cancel: "Cancel" }
    );
    if (!confirmed) return null;
    const updated = await _overwriteSettingsProfile(existing);
    if (updated) showToast(`Settings Profile "${updated.name}" saved`, "success");
    return updated;
  }

  const created = await _saveDraftAsSettingsProfileWithName(result.name);
  if (created) showToast(`Settings Profile "${created.name}" created`, "success");
  return created;
}

async function handleSettingsProfileDelete(profileId) {
  const profile = findSettingsProfile(profileId);
  if (!profile) return false;
  if (profile.kind !== "named") {
    showToast("The system default profile cannot be deleted", "error");
    return false;
  }
  if (loadedProfileRef?.id === profile.id) {
    showToast("Load another profile before deleting this one", "error");
    return false;
  }
  const confirmed = await appConfirm(
    `Delete the Settings Profile "${profile.name}"?`,
    { ok: "Delete", cancel: "Cancel" }
  );
  if (!confirmed) return false;
  const response = await deleteSettingsProfile(profile.id);
  _refreshSettingsProfilesFromResponse(response);
  renderSettingsTab({ preservePendingUi: true });
  showToast(`Deleted Settings Profile "${profile.name}"`, "success");
  return true;
}

async function handleSettingsProfileSetStartup(profileId) {
  const profile = findSettingsProfile(profileId);
  if (!profile) return false;
  if (profile.id === userDefaultProfileId) {
    return false;
  }
  const response = await setUserDefaultSettingsProfile(profile.id);
  _refreshSettingsProfilesFromResponse(response);
  renderSettingsTab({ preservePendingUi: true });
  return true;
}

async function handleSettingsProfilesBrowse() {
  let selectedProfileId = loadedProfileRef?.id || userDefaultProfileId || SYSTEM_SETTINGS_PROFILE_ID;
  while (true) {
    const result = await showSettingsProfileBrowserModal({
      title: "Settings Profiles",
      selectedProfileId,
      onAction: async ({ action, profileId, selectedProfileId: currentSelectedId }) => {
        if (action === "set_default") {
          await handleSettingsProfileSetStartup(profileId);
          return { close: false, selectedProfileId: profileId || currentSelectedId };
        }
        if (action === "delete") {
          await handleSettingsProfileDelete(profileId);
          return {
            close: false,
            selectedProfileId: loadedProfileRef?.id || userDefaultProfileId || SYSTEM_SETTINGS_PROFILE_ID,
          };
        }
        return null;
      },
    });
    if (!result) return;
    if (result.action === "load_saved_run") {
      await openSavedRunsModal("settings");
      return;
    }
    selectedProfileId = result.profileId || selectedProfileId;
    const selected = findSettingsProfile(selectedProfileId);
    if (!selected) {
      selectedProfileId = userDefaultProfileId || SYSTEM_SETTINGS_PROFILE_ID;
      continue;
    }

    try {
      if (result.action === "load") {
        const actionLabel = loadedProfileRef?.id === selected.id
          ? "reloading the saved Settings Profile"
          : "loading another Settings Profile";
        const proceed = await _guardSettingsProfileTransition(actionLabel);
        if (!proceed) continue;
        await _doLoadSettingsProfile(selected);
        showToast(`Loaded Settings Profile "${selected.name}"`, "success");
        return;
      } else if (result.action === "set_default") {
        await handleSettingsProfileSetStartup(selected.id);
      } else if (result.action === "delete") {
        await handleSettingsProfileDelete(selected.id);
        selectedProfileId = loadedProfileRef?.id || userDefaultProfileId || SYSTEM_SETTINGS_PROFILE_ID;
      } else if (result.action === "restore") {
        await handleRestoreSystemSettingsProfile();
        selectedProfileId = SYSTEM_SETTINGS_PROFILE_ID;
      } else {
        return;
      }
    } catch (e) {
      showToast("Failed: " + e.message, "error");
    }
  }
}

async function handleSettingsProfileSave() {
  if (!isSettingsProfileModified()) {
    showToast("No changes to save", "");
    return;
  }

  const current = loadedProfileRef ? findSettingsProfile(loadedProfileRef.id) || loadedProfileRef : null;
  const isNamed = current?.kind === "named";
  const isTemporary = current?.kind === "temporary";
  const choice = await appChoice(
    isNamed
      ? `Save changes to "${current.name}"?`
      : isTemporary
        ? "This TEMP profile is session-only. Save it as a new named Settings Profile?"
        : "The system default profile cannot be overwritten. Save this draft as a new Settings Profile?",
    isNamed
      ? [
          { label: "Overwrite", value: "overwrite", kind: "primary" },
          { label: "Save As", value: "save_as" },
          { label: "Cancel", value: "cancel" },
        ]
      : [
          { label: "Save As", value: "save_as", kind: "primary" },
          { label: "Cancel", value: "cancel" },
        ],
    { title: "Save Settings Profile" },
  );
  if (!choice || choice === "cancel") return;

  try {
    if (choice === "overwrite") {
      const updated = await _overwriteLoadedNamedSettingsProfile();
      if (updated) showToast(`Settings Profile "${updated.name}" saved`, "success");
      return;
    }
    await _ensureDraftSavedAsSettingsProfile(isNamed ? current.name : "");
  } catch (e) {
    showToast("Save failed: " + e.message, "error");
  }
}

async function handleSettingsProfileSaveAs() {
  try {
    await _ensureDraftSavedAsSettingsProfile(
      loadedProfileRef?.kind === "named" ? loadedProfileRef.name : ""
    );
  } catch (e) {
    showToast("Save failed: " + e.message, "error");
  }
}

async function handleSettingsProfileSetDefault() {
  const current = loadedProfileRef ? findSettingsProfile(loadedProfileRef.id) || loadedProfileRef : null;
  if (!current) return;
  if (current.kind === "temporary") {
    showToast("Save this TEMP profile as a named Settings Profile before making it the startup default", "warn");
    return;
  }

  try {
    if (!isSettingsProfileModified()) {
      if (current.id === userDefaultProfileId) {
        return;
      }
      const response = await setUserDefaultSettingsProfile(current.id);
      _refreshSettingsProfilesFromResponse(response);
      renderSettingsTab({ preservePendingUi: true });
      return;
    }

    const created = await _ensureDraftSavedAsSettingsProfile(
      current.kind === "named" ? current.name : ""
    );
    if (!created) return;
    const makeDefault = await appConfirm(
      `Make "${created.name}" the user default Settings Profile?`,
      { ok: "Make Default", cancel: "Not Now" }
    );
    if (!makeDefault) return;
    const response = await setUserDefaultSettingsProfile(created.id);
    _refreshSettingsProfilesFromResponse(response);
    renderSettingsTab({ preservePendingUi: true });
  } catch (e) {
    showToast("Failed: " + e.message, "error");
  }
}

async function handleRestoreSystemSettingsProfile() {
  const proceed = await _guardSettingsProfileTransition("restoring the system default");
  if (!proceed) return;
  try {
    const response = await restoreSystemSettingsProfile();
    _refreshSettingsProfilesFromResponse(response);
    const systemProfile = findSettingsProfile(SYSTEM_SETTINGS_PROFILE_ID);
    if (systemProfile) {
      await _doLoadSettingsProfile(systemProfile);
    }
    showToast("System default Settings Profile restored", "success");
  } catch (e) {
    showToast("Restore failed: " + e.message, "error");
  }
}

function renderPresetBar() {
  renderSettingsProfileBar();
}

function updateBorderVisibility() {
  // Border is now a toggle switch; CSS :has(.is-on) handles field opacity
  const toggle = $("#borderToggle");
  const checkbox = $("#cfgBorder");
  if (toggle && checkbox) {
    toggle.classList.toggle("is-on", checkbox.checked);
  }
}

function readSolvePreflightNumber(fieldId, configKey, fallback) {
  const el = typeof $ === "function" ? $(`#${fieldId}`) : null;
  const domValue = parseFloat(el?.value);
  if (Number.isFinite(domValue)) return domValue;
  const configValue = parseFloat(config?.[configKey]);
  return Number.isFinite(configValue) ? configValue : fallback;
}

function readSolvePreflightMinCapLayers(layerHeight) {
  const el = typeof $ === "function" ? $("#cfgDWcMin") : null;
  const domValue = parseInt(el?.value, 10);
  if (Number.isFinite(domValue) && domValue >= 1) return domValue;
  return minCapLayersFromThickness(config?.d_wc_min, layerHeight);
}

function calculateStackLayerAlignment(layerHeight, baseThickness, minCapThickness, maxTotalThickness) {
  const colorBudget = Math.round((maxTotalThickness - baseThickness - minCapThickness) * 1e6) / 1e6;
  const maxLayers = colorBudget > 0 ? Math.floor(colorBudget / layerHeight + 1e-9) : 0;
  const usedBudget = Math.round(maxLayers * layerHeight * 1e6) / 1e6;
  const remainderMm = Math.round((colorBudget - usedBudget) * 1e6) / 1e6;
  return {
    colorBudget,
    maxLayers,
    usedBudget,
    remainderMm,
    lowerTotalMm: baseThickness + minCapThickness + maxLayers * layerHeight,
    upperTotalMm: baseThickness + minCapThickness + (maxLayers + 1) * layerHeight,
  };
}

function buildStackLayerAlignmentIssue(alignment) {
  return `${alignment.remainderMm.toFixed(2)} mm of Max Total Thickness cannot be allocated in whole Layer Height steps. `
    + `Set Max Total Thickness to ${alignment.lowerTotalMm.toFixed(2)} mm (${alignment.maxLayers} color layers) `
    + `or ${alignment.upperTotalMm.toFixed(2)} mm (${alignment.maxLayers + 1} color layers)`;
}

function buildSolvePitchNozzleIssue(pitch, nozzleSize) {
  const pitchText = Number(pitch).toFixed(3).replace(/\.?0+$/, "");
  const nozzleText = Number(nozzleSize).toFixed(3).replace(/\.?0+$/, "");
  return `Solve Pitch (${pitchText} mm) cannot be smaller than the active nozzle diameter (${nozzleText} mm). `
    + "Increase Solve Pitch or choose a smaller nozzle.";
}

function getSolveSettingsPreflightIssues() {
  const lh = readSolvePreflightNumber("cfgLayerHeight", "layer_height", 0.08);
  const dwb = readSolvePreflightNumber("cfgDWb", "d_wb", 0.20);
  const dwcMinLayers = readSolvePreflightMinCapLayers(lh);
  const dwcMin = minCapThicknessFromLayers(dwcMinLayers, lh);
  const tMax = readSolvePreflightNumber("cfgTMax", "t_max", 2.5);
  const solvePitch = readSolvePreflightNumber(
    "cfgSolvePitch",
    "solver_fine_pitch_mm",
    parseFloat(config?.image_sample_pitch_mm) || 0.20,
  );
  const nozzle = typeof activeNozzle !== "undefined" ? activeNozzle : null;
  const eps = 0.001;
  const pitchEps = 1e-6;
  const issues = [];

  if (nozzle) {
    if (lh < nozzle.min_layer_height - eps) {
      issues.push(`Layer Height (${lh} mm) is below the ${nozzle.size} mm nozzle minimum (${nozzle.min_layer_height} mm)`);
    } else if (lh > nozzle.max_layer_height + eps) {
      issues.push(`Layer Height (${lh} mm) exceeds the ${nozzle.size} mm nozzle maximum (${nozzle.max_layer_height} mm)`);
    }
    const nozzleSize = Number(nozzle.size);
    if (Number.isFinite(nozzleSize) && solvePitch < nozzleSize - pitchEps) {
      issues.push(buildSolvePitchNozzleIssue(solvePitch, nozzleSize));
    }
  }

  const alignment = calculateStackLayerAlignment(lh, dwb, dwcMin, tMax);
  if (alignment.colorBudget <= 0) {
    issues.push("No color space — base + cap exceed max total thickness");
    return issues;
  }

  if (alignment.remainderMm > eps) {
    issues.push(buildStackLayerAlignmentIssue(alignment));
  }
  return issues;
}

function buildSolveSettingsPreflightMessage(issues) {
  return `Can't solve. Fix settings: ${(issues || []).join(" ")}`.trim();
}

function updateDerivedParams() {
  const lh = parseFloat($("#cfgLayerHeight").value) || 0.08;
  const dwb = parseFloat($("#cfgDWb").value) || 0.20;
  const dwcMinEl = $("#cfgDWcMin");
  let dwcMinLayers = parseInt(dwcMinEl?.value, 10);
  if (!Number.isFinite(dwcMinLayers) || dwcMinLayers < 1) {
    dwcMinLayers = 1;
    if (dwcMinEl) dwcMinEl.value = "1";
  }
  const dwcMin = minCapThicknessFromLayers(dwcMinLayers, lh);
  const tMax = parseFloat($("#cfgTMax").value) || 2.5;

  // Update layer height range label from active nozzle
  const lhLabel = $("#cfgLayerHeight")?.closest("tr")?.querySelector(".stg-range");
  if (lhLabel) {
    const lo = activeNozzle?.min_layer_height ?? 0.04;
    const hi = activeNozzle?.max_layer_height ?? 0.20;
    lhLabel.textContent = `${lo}\u2013${hi}`;
  }
  const solvePitchHint = $("#cfgSolvePitchHint");
  if (solvePitchHint) {
    const minimumPitch = Number(activeNozzle?.size ?? 0.20);
    const pitchText = Number.isFinite(minimumPitch)
      ? minimumPitch.toFixed(2).replace(/0+$/, "").replace(/\.$/, "")
      : "0.2";
    solvePitchHint.textContent = `minimum ${pitchText} mm`;
  }

  const alignment = calculateStackLayerAlignment(lh, dwb, dwcMin, tMax);
  const { colorBudget, maxLayers, usedBudget, remainderMm } = alignment;
  const eps = 0.001;
  const el = $("#derivedParams");
  const footnote = $("#stackDerived");

  if (colorBudget <= 0) {
    if (footnote) footnote.innerHTML = `<div class="stg-warn">No color space — base + cap exceed max total thickness</div>`;
    el.innerHTML = "";
    return;
  }

  // Stack geometry footnote — keep it clean, one line each
  if (footnote) {
    footnote.innerHTML = `
      <div>Color layers: <strong>${maxLayers}</strong> at ${lh} mm = <strong>${usedBudget.toFixed(2)} mm</strong></div>
    `;
  }

  // Validation warnings
  const warnings = [];

  // Nozzle compatibility
  if (activeNozzle) {
    if (lh < activeNozzle.min_layer_height - eps) {
      warnings.push(`Layer Height (${lh} mm) is below the ${activeNozzle.size} mm nozzle minimum (${activeNozzle.min_layer_height} mm)`);
    } else if (lh > activeNozzle.max_layer_height + eps) {
      warnings.push(`Layer Height (${lh} mm) exceeds the ${activeNozzle.size} mm nozzle maximum (${activeNozzle.max_layer_height} mm)`);
    }
    const solvePitch = getCurrentSolvePitch();
    const nozzleSize = Number(activeNozzle.size);
    if (Number.isFinite(nozzleSize) && solvePitch < nozzleSize - 1e-6) {
      warnings.push(buildSolvePitchNozzleIssue(solvePitch, nozzleSize));
    }
  }

  // Divisibility checks
  if (remainderMm > eps) {
    warnings.push(buildStackLayerAlignmentIssue(alignment));
  }
  // Soft range hints (not printability issues, just gentle nudges)
  const hints = [];
  const rangeChecks = [
    { id: "cfgLayerHeight", val: lh, min: activeNozzle?.min_layer_height ?? 0.04, max: activeNozzle?.max_layer_height ?? 0.20, name: "Layer Height" },
    { id: "cfgDWb", val: dwb, min: 0.08, max: 1.0, name: "Base Thickness" },
    { id: "cfgDWcMin", val: dwcMin, min: lh, max: 0.50, name: "Min Cap Thickness" },
    { id: "cfgTMax", val: tMax, min: 1.0, max: 5.0, name: "Max Total Thickness" },
    { id: "cfgKMax", val: parseInt($("#cfgKMax")?.value) || 3, min: 1, max: 7, name: "Max Colors per Region" },
    { id: "cfgDeThreshold", val: parseFloat($("#cfgDeThreshold")?.value) || 0.01, min: 0.01, max: 0.20, name: "Color Mismatch Tolerance" },
    { id: "cfgSmoothKernel", val: Number.isNaN(parseFloat($("#cfgSmoothKernel")?.value)) ? smoothingRadiusMmFromCells(config.smooth_kernel) : parseFloat($("#cfgSmoothKernel")?.value), min: 0, max: 20, name: "Smoothing Radius" },
  ];
  for (const { val, min, max, name } of rangeChecks) {
    if (val < min - eps) hints.push(`${name} (${val}) is below the suggested minimum of ${min}`);
    else if (val > max + eps) hints.push(`${name} (${val}) is above the suggested maximum of ${max}`);
  }

  // Clear all field-level warning indicators
  document.querySelectorAll(".stg-field-warn").forEach(el => el.remove());

  // Add ⚠ glyph next to fields that triggered warnings
  const warnFieldIds = new Set();
  if (activeNozzle && (lh < activeNozzle.min_layer_height - eps || lh > activeNozzle.max_layer_height + eps)) {
    warnFieldIds.add("cfgLayerHeight");
  }
  if (remainderMm > eps) {
    warnFieldIds.add("cfgLayerHeight");
    warnFieldIds.add("cfgTMax");
  }
  if (activeNozzle && getCurrentSolvePitch() < Number(activeNozzle.size) - 1e-6) {
    warnFieldIds.add("cfgSolvePitch");
  }

  // Border height must be >= base thickness (d_wb)
  if (config.border) {
    const borderHeightVal = parseFloat($("#cfgBorderHeight")?.value) || 0;
    if (borderHeightVal < dwb - eps) {
      warnings.push(`Border Height (${borderHeightVal} mm) must be at least the base thickness (${dwb} mm)`);
      warnFieldIds.add("cfgBorderHeight");
    }
  }

  for (const id of warnFieldIds) {
    const input = $(`#${id}`);
    if (input) {
      const wrapper = input.closest(".input-with-unit");
      if (wrapper && !wrapper.querySelector(".stg-field-warn")) {
        const mark = document.createElement("span");
        mark.className = "stg-field-warn";
        mark.textContent = "\u26a0";
        wrapper.parentElement.insertBefore(mark, wrapper);
      }
    }
  }

  let html = "";
  if (warnings.length) {
    html += warnings.map(w => `<div class="stg-warn">\u26a0 ${w}</div>`).join("");
  }
  if (hints.length) {
    html += hints.map(h => `<div class="stg-hint">${h}</div>`).join("");
  }
  el.innerHTML = html;
}

function applyDraftNumberField(key, rawValue, {
  parse = parseFloat,
  isValid = (value) => !Number.isNaN(value),
} = {}) {
  const value = parse(rawValue);
  if (!isValid(value)) return false;
  config[key] = value;
  return true;
}

function bindDraftNumberInput(id, applyDraft) {
  const el = $(`#${id}`);
  if (!el) return;
  el.addEventListener("input", () => {
    applyDraft(el.value, el);
  });
}

function readBoundedNumberInput(id, fallback, options = {}) {
  const el = $(`#${id}`);
  if (!el) return fallback;
  const coerced = coerceNumberValue(el.value, fallback, options);
  if (coerced.ok) el.value = coerced.value;
  return coerced.value;
}

function readOptionalNumberInput(id, options = {}) {
  const el = $(`#${id}`);
  if (!el) return null;
  const raw = String(el.value || "").trim();
  if (!raw) return null;
  const coerced = coerceNumberValue(raw, null, options);
  if (coerced.ok) {
    el.value = coerced.value;
    return coerced.value;
  }
  el.value = "";
  return null;
}

function setOptionalNumberInput(id, value) {
  const el = $(`#${id}`);
  if (!el) return;
  el.value = value === null || value === undefined ? "" : value;
}

function readConfigFromUI() {
  config.base_filament = $("#cfgBaseFilament")?.value || DEFAULT_BASE_FILAMENT;
  config.cap_filament = "__same__";
  config.layer_height = readBoundedNumberInput("cfgLayerHeight", config.layer_height, { min: 0.001 });
  config.d_wb = readBoundedNumberInput("cfgDWb", config.d_wb, { min: 0.001 });
  {
    const capLayers = readBoundedNumberInput(
      "cfgDWcMin",
      minCapLayersFromThickness(config.d_wc_min, config.layer_height),
      { parse: (value) => parseInt(value, 10), min: 1, integer: true },
    );
    config.d_wc_min = minCapThicknessFromLayers(capLayers, config.layer_height);
  }
  config.t_max = readBoundedNumberInput("cfgTMax", config.t_max, { min: 0.001 });
  config.k_max = readBoundedNumberInput("cfgKMax", config.k_max, { parse: (value) => parseInt(value, 10), min: 1, max: 7, integer: true });
  config.de_threshold = readBoundedNumberInput("cfgDeThreshold", config.de_threshold, { min: 0 });
  config.gamut_mode = normalizeActiveGamutMode($("#cfgGamutMode")?.value || config.gamut_mode || "hull");
  config.gamut_white_rescale = $("#cfgGamutWhiteRescale")?.checked ?? config.gamut_white_rescale;
  config.model_domain_ingress = true;
  {
    const radiusMm = readBoundedNumberInput(
      "cfgSmoothKernel",
      smoothingRadiusMmFromCells(config.smooth_kernel),
      { min: 0 },
    );
    config.smooth_kernel = smoothingCellsFromRadiusMm(radiusMm, getCurrentSolvePitch());
  }
  syncChromaWeightControlFromConfig();
  config.source_resample_kernel = $("#cfgSourceResampleKernel")?.value || config.source_resample_kernel || "lanczos";
  config.appearance_model_provider = $("#cfgAppearanceModelProvider")?.value || config.appearance_model_provider || "photo_stack_bundle";
  if (config.appearance_model_provider !== "photo_stack_bundle") {
    config.photo_stack_bundle_path = null;
  }
  // Module params are written directly to config by renderParamRow's input/change
  // handlers — no need to read from HTML here. Static settings that still have
  // hardcoded HTML elements:
  config.border = $("#cfgBorder")?.checked ?? config.border;
  config.border_width_mm = readBoundedNumberInput("cfgBorderWidth", config.border_width_mm, { min: 0 });
  config.border_height_mm = readBoundedNumberInput("cfgBorderHeight", config.border_height_mm, { min: 0 });
  config.use_corrections = $("#cfgUseCorrections")?.checked ?? config.use_corrections;
  const solvePitch = getCurrentSolvePitch();
  config.image_sample_pitch_mm = solvePitch;
  config.solver_fine_pitch_mm = solvePitch;
  const selectedLuminanceMode = normalizeLuminanceMode(getSolveModeControlValue());
  const selectedCapMode = $("#cfgCapMode")?.value || config.cap_mode || "appearance_bounded_smooth";
  if (selectedLuminanceMode !== "luminance_detail") {
    config.cap_mode = selectedCapMode;
    saveLastColorCapMode(config.cap_mode);
    capModeForcedByLuminance = false;
  }
  if (selectedLuminanceMode === "luminance_detail") {
    config.cap_mode = "smooth_variable";
    const capModeEl = $("#cfgCapMode");
    if (capModeEl) capModeEl.value = "smooth_variable";
  }
  config.boundary_cap_de_budget = readBoundedNumberInput("cfgBoundaryCapDeBudget", config.boundary_cap_de_budget ?? 0.008, { min: 0 });
  config.detail_cap_enabled = true;
  {
    const detailLayerRaw = String($("#cfgDetailCapMaxLayers")?.value || "").trim();
    const detailMaxLayers = /^[0-9]+$/.test(detailLayerRaw)
      ? parseInt(detailLayerRaw, 10)
      : NaN;
    config.detail_cap_max_layers = Number.isFinite(detailMaxLayers)
      ? Math.max(0, detailMaxLayers)
      : (config.detail_cap_max_layers ?? 5);
  }
  applyMandatoryProductSettings();
  config.cell_mode = $("#cfgCellMode")?.value || config.cell_mode || "felzenszwalb";
  config.stage1_coarsening_factor = readBoundedNumberInput("cfgStage1Coarsening", config.stage1_coarsening_factor || 1, { parse: (value) => parseInt(value, 10), min: 1, max: 4, integer: true });
  config.color_region_target_mm = readBoundedNumberInput("cfgColorRegionTarget", config.color_region_target_mm, { min: 0.001 });
  // Blueprint printability diagnostics stay on for normal app solves.
  // Heavier pressure/geometry attribution artifacts are research-only and
  // can still be enabled from scripts or API payloads.
  config.emit_pressure_diagnostics = false;
  config.emit_geometry_attribution = false;
  config.emit_blueprint_printability = true;
  config.printability_minimum_extrusion_width_mm = activeNozzle?.min_line_width ?? null;
  config.printability_minimum_line_length_mm = activeNozzle?.min_line_length ?? null;
  // Product printability enforcement is mandatory. Width multiplier remains
  // an internal/profile value and keeps whatever was loaded.
  config.color_region_target_from_printability = true;
  config.stage2_fine_override_enabled = $("#cfgStage2FineOverride")?.checked ?? config.stage2_fine_override_enabled;
  config.stage2_boundary_mutation_enabled = $("#cfgStage2BoundaryMutation")?.checked ?? config.stage2_boundary_mutation_enabled;
  config.stage2_boundary_mutation_current_de_percentile = readOptionalNumberInput("cfgStage2BoundaryMutationPercentile", { min: 0, max: 100 });
  config.stage2_boundary_mutation_max_passes = readOptionalNumberInput("cfgStage2BoundaryMutationMaxPasses", { min: 1, max: 16 }) ?? 1;
  config.stage2_boundary_mutation_min_gain = readOptionalNumberInput("cfgStage2BoundaryMutationMinGain", { min: 0 });
  config.stage2_boundary_mutation_min_component_mm = readOptionalNumberInput("cfgStage2BoundaryMutationMinComponent", { min: 0 });
  const baseShadingLimitEl = getBaseShadingLimitInput();
  if (baseShadingLimitEl) {
    const fraction = setLuminanceBaseShadingLimitFraction(
      parseLuminanceBaseShadingLimitPercent(baseShadingLimitEl.value),
    );
    syncBaseShadingLimitControls(formatLuminanceBaseShadingLimitPercent(fraction));
  }
  config.luminance_mode = applyLuminanceMode(selectedLuminanceMode, { resetStandard: true });
  config.swap_improvement_threshold = readBoundedNumberInput("paletteSwapThreshold", config.swap_improvement_threshold || 2.0, { min: 0 });
  config.force_all_tiers = $("#paletteForceAllTiers")?.checked || false;
}

function _formatConfigSyncError(err) {
  if (!err) return "unknown error";
  if (typeof err.message === "string" && err.message.trim()) return err.message.trim();
  return String(err);
}

let _configSyncChain = Promise.resolve();

async function syncConfigToServer({ throwOnError = false, showErrorToast = false } = {}) {
  if (!apiConnected) return;
  syncConfigFromModuleState();
  readConfigFromUI();
  // Serialize config writes so older requests cannot land after newer ones
  // and revert the session immediately before solve start.
  const frame = selectedImage ? {
    width_mm: frameState.widthMm,
    height_mm: frameState.heightMm,
    scale: frameState.scale,
    rotation: frameState.rotation,
    pan_x: frameState.panX,
    pan_y: frameState.panY,
    flip_h: frameState.flipH,
    flip_v: frameState.flipV,
  } : null;
  const payload = {
    ...config,
    image_path: selectedImage?.filename || null,
    palette: getActivePalette(),
    white_base: getBaseFilament(),
    white_cap: null,
    ams_slots: printerConfig.ams_slots,
    white_slots: printerConfig.white_slots,
    frame,
    image_adjust: imageAdjust,
  };
  const runSync = async () => {
    const response = await updateConfig(payload);
    if (response?.config) {
      Object.assign(config, response.config);
    }
    return response;
  };
  const pendingSync = _configSyncChain.catch(() => {}).then(runSync);
  _configSyncChain = pendingSync.catch(() => {});
  try {
    return await pendingSync;
  } catch (err) {
    console.warn("[config] sync failed:", err.message);
    if (showErrorToast) {
      showToast(`Couldn't sync settings to the server: ${_formatConfigSyncError(err)}`, "error");
    }
    if (throwOnError) throw err;
  }
}

// ── Solve Tab ────────────────────────────────────────────────────────────────

function renderSolveTab() {
  renderSolveRunSidebar();
  renderSolveProgress();
  renderSolveComparisonGrid();

  updateSolveReadiness();
}

function getCurrentSolvePitch() {
  return parseFloat($("#cfgSolvePitch")?.value)
    || config.image_sample_pitch_mm
    || 0.20;
}

function applySolvePitchDraft(rawValue, mirrorEl = null) {
  const previousPitch = getCurrentSolvePitch();
  const radiusMm = smoothingRadiusMmFromCells(config.smooth_kernel, previousPitch);
  const v = parseFloat(rawValue);
  if (!(v > 0)) return false;
  config.image_sample_pitch_mm = v;
  config.solver_fine_pitch_mm = v;
  config.smooth_kernel = smoothingCellsFromRadiusMm(radiusMm, v);
  const smoothEl = $("#cfgSmoothKernel");
  if (smoothEl) smoothEl.value = radiusMm;
  if (mirrorEl && mirrorEl.value !== rawValue) mirrorEl.value = rawValue;
  updateInfoGrid();
  renderPreview();
  return true;
}

function updateSolveReadiness() {
  const btn = $("#startSolveBtn");
  if (!btn) return;
  const canSolve = apiConnected && selectedImage && getActivePalette().length > 0;
  const isRunning = solveStatus.status === "running";
  btn.disabled = !(canSolve && !solveStartPending && !isRunning && !exportRunning);
  btn.title = exportRunning
    ? "Please wait for meshing to finish"
    : solveStartPending
      ? "Starting solve..."
      : isRunning
        ? "A solve is already running"
        : "Solve the active palette";
}

function isActivePendingSolveRun(run) {
  return !!run
    && !run.results
    && run.id === activeSolveRunId
    && solveStatus.status === "running";
}

function getSolveRunDeleteBlockReason(run) {
  if (isActivePendingSolveRun(run)) {
    return "Cancel this solve before removing its card";
  }
  if (run && exportRunning && run.id === activeExportRunId) {
    return "Cancel this export before removing its source run";
  }
  return "";
}

function buildSolveRunDeleteButton(run) {
  const blockReason = getSolveRunDeleteBlockReason(run);
  const armed = !blockReason && solveRunDeleteArmedId === run.id;
  const label = armed ? "Click again to delete this run" : (blockReason || "Delete this run");
  return `<button class="solve-run-delete-btn${armed ? " confirm-pending" : ""}" data-run-id="${escAttr(run.id)}" title="${escAttr(label)}" aria-label="${escAttr(label)}"${blockReason ? " disabled aria-disabled=\"true\"" : ""}>${armed ? "Confirm?" : xIconSvg()}</button>`;
}

function renderSolveRunDeleteState() {
  renderSolveRunSidebar();
  if (currentTab === "export") renderExportRunSidebar();
}

function resetSolveRunDeleteConfirm({ render = true } = {}) {
  if (solveRunDeleteConfirmTimer) {
    clearTimeout(solveRunDeleteConfirmTimer);
    solveRunDeleteConfirmTimer = null;
  }
  const changed = solveRunDeleteArmedId !== null;
  solveRunDeleteArmedId = null;
  if (render && changed) renderSolveRunDeleteState();
}

function armSolveRunDeleteConfirm(runId) {
  if (!runId) return;
  if (solveRunDeleteConfirmTimer) clearTimeout(solveRunDeleteConfirmTimer);
  solveRunDeleteArmedId = runId;
  solveRunDeleteConfirmTimer = setTimeout(() => {
    solveRunDeleteConfirmTimer = null;
    solveRunDeleteArmedId = null;
    renderSolveRunDeleteState();
  }, 3000);
  renderSolveRunDeleteState();
}

function handleSolveRunDeleteClick(runId) {
  const run = solveRuns.find((candidate) => candidate.id === runId);
  if (!run || getSolveRunDeleteBlockReason(run)) return false;
  if (solveRunDeleteArmedId !== runId) {
    armSolveRunDeleteConfirm(runId);
    return false;
  }
  resetSolveRunDeleteConfirm({ render: false });
  return deleteSolveRun(runId);
}

function deleteSolveRun(runId, { force = false } = {}) {
  const run = solveRuns.find(r => r.id === runId);
  if (!force && isActivePendingSolveRun(run)) {
    showToast("Cancel the solve before removing its pending card.", "warn");
    return false;
  }
  if (!force && exportRunning && runId === activeExportRunId) {
    showToast("Cancel the export before removing its source run.", "warn");
    return false;
  }
  const idx = solveRuns.findIndex(r => r.id === runId);
  if (idx === -1) return false;
  resetSolveRunDeleteConfirm({ render: false });
  solveRuns.splice(idx, 1);
  selectedRunIds.delete(runId);
  if (exportSelectedRunId === runId) {
    exportSelectedRunId = null;
  }
  if (solveRunHoverRunId === runId) hideSolveRunHoverPreview();
  if (solveRunSettingsPanelRunId === runId) hideSolveRunSettingsPanel();
  invalidateSolveRunCaches(run);
  renderSolveTab();
  if (currentTab === "export") renderExportTab();
  return true;
}

function removePendingSolveRun(runId) {
  if (!runId) return false;
  const run = solveRuns.find(r => r.id === runId);
  if (!run || run.results) return false;
  deleteSolveRun(runId, { force: true });
  return true;
}

function clearSolveHistory() {
  if (solveStatus.status === "running" || exportRunning) {
    showToast("Wait for the active solve or export to finish before clearing history.", "warn");
    return;
  }
  resetSolveRunDeleteConfirm({ render: false });
  solveRuns.forEach((run) => invalidateSolveRunCaches(run));
  solveRuns = [];
  solveRunCounter = 0;
  solveStatus = { status: "idle", progress: "", elapsed_s: 0, result: null };
  selectedRunIds.clear();
  exportSelectedRunId = null;
  renderSolveTab();
  if (currentTab === "export") renderExportTab();
}

function getSolveHistoryClearButtons() {
  return ["clearSolveHistoryBtn", "exportClearSolveHistoryBtn"]
    .map(id => $(`#${id}`))
    .filter(Boolean);
}

function resetSolveHistoryClearConfirm() {
  if (solveHistoryClearConfirmTimer) {
    clearTimeout(solveHistoryClearConfirmTimer);
    solveHistoryClearConfirmTimer = null;
  }
  solveHistoryClearConfirmPending = false;
  getSolveHistoryClearButtons().forEach((btn) => {
    btn.textContent = "Clear";
    btn.classList.remove("confirm-pending");
    btn.title = "Clear all solve runs";
  });
}

function armSolveHistoryClearConfirm() {
  solveHistoryClearConfirmPending = true;
  getSolveHistoryClearButtons().forEach((btn) => {
    btn.textContent = "Confirm?";
    btn.classList.add("confirm-pending");
    btn.title = "Click again to clear all solve runs";
  });
  solveHistoryClearConfirmTimer = setTimeout(resetSolveHistoryClearConfirm, 1800);
}

function handleSolveHistoryClearClick() {
  if (!solveRuns.length) return;
  if (!solveHistoryClearConfirmPending) {
    armSolveHistoryClearConfirm();
    return;
  }
  resetSolveHistoryClearConfirm();
  clearSolveHistory();
}

function renderSolveRunSidebar() {
  const container = $("#solveRunCards");
  if (!container) return;
  hideSolveRunHoverPreview();

  if (solveRuns.length === 0) {
    container.innerHTML = `<p class="muted-line" id="solveRunEmpty">No solves yet</p>`;
    return;
  }

  let html = "";
  for (let i = solveRuns.length - 1; i >= 0; i--) {
    const run = solveRuns[i];
    const isSelected = selectedRunIds.has(run.id);
    const chips = (run.palette || []).map(fid => {
      const fil = allFilaments.find(f => f.filament_id === fid);
      const hex = fil?.hex || "#888";
      return `<span class="comp-deck-chip" style="background:${hex}"></span>`;
    }).join("");

    const stats = run.results
      ? `<span class="solve-run-card-rmse">${formatSolveRunCardRmse(run.results)}</span>`
      : `<span class="solve-run-card-rmse is-pending">solving...</span>`;
    const loadedBadge = run.loaded_from_archive
      ? `<span class="solve-run-loaded-badge">Loaded</span>`
      : "";
    html += `<div class="solve-run-card ${isSelected ? "is-selected" : ""}" data-run-id="${esc(run.id)}" tabindex="0">
      <div class="solve-run-card-header">
        <span class="solve-run-label">${esc(run.label)}${loadedBadge}</span>
        <div class="solve-run-card-actions">
          <button class="solve-run-save-btn ghost-button xxs" data-run-id="${esc(run.id)}" title="Save this run to a portable archive">Save</button>
          ${buildSolveRunDeleteButton(run)}
        </div>
      </div>
      <div class="comp-deck-card-chips">${chips}</div>
      <div class="solve-run-card-meta">
        <button class="solve-run-settings-btn" data-run-id="${esc(run.id)}" title="View the settings captured for this run">Settings</button>
        ${stats}
      </div>
    </div>`;
  }
  container.innerHTML = html;

  container.querySelectorAll(".solve-run-card").forEach(el => {
    el.addEventListener("click", (e) => {
      if (e.target.closest(".solve-run-settings-btn")) return;
      if (e.target.closest(".solve-run-delete-btn")) return;
      if (e.target.closest(".solve-run-save-btn")) return;
      const runId = el.dataset.runId;
      if (selectedRunIds.has(runId)) selectedRunIds.delete(runId);
      else selectedRunIds.add(runId);
      renderSolveRunSidebar();
      renderSolveComparisonGrid();
    });
  });

  container.querySelectorAll(".solve-run-delete-btn").forEach(btn => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      if (btn.disabled) return;
      handleSolveRunDeleteClick(btn.dataset.runId);
    });
  });

  container.querySelectorAll(".solve-run-save-btn").forEach(btn => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const run = solveRuns.find(r => r.id === btn.dataset.runId);
      const label = await appPrompt(
        "Save this run as:",
        run?.label || "",
        { title: "Save Run", validate: value => String(value || "").trim() ? "" : "Run name cannot be empty." },
      );
      if (label == null) return;
      const trimmed = label.trim();
      try {
        const res = await saveRun(btn.dataset.runId, trimmed);
        showToast(`Saved as "${res.label}"`, "");
      } catch (err) { showToast(err.message, "error"); }
    });
  });

  bindSolveRunCardAuxiliaryInteractions(container, "preview");
}

// ── Saved Runs browser (Stage 9b) ─────────────────────────────────────────────

function _setSavedRunsModalOpen(open) {
  const modal = $("#savedRunsModal");
  if (!modal) return;
  modal.classList.toggle("is-hidden", !open);
  modal.setAttribute("aria-hidden", open ? "false" : "true");
  if (!open) {
    resetSavedRunDeleteConfirm();
    savedRunsModalMode = "run";
  }
}

async function openSavedRunsModal(mode = "run") {
  savedRunsModalMode = mode === "settings" ? "settings" : "run";
  const title = $("#savedRunsModalTitle");
  if (title) title.textContent = savedRunsModalMode === "settings"
    ? "Load Settings from Saved Run"
    : "Saved Runs";
  const uploadLabel = $("#savedRunUploadLabel");
  if (uploadLabel) {
    // The upload endpoint rehydrates a whole run (image/cache/history card),
    // so do not expose it in settings-only mode where that side effect would
    // contradict the action's contract.
    uploadLabel.hidden = savedRunsModalMode === "settings";
  }
  _setSavedRunsModalOpen(true);
  await refreshSavedRunRows();
}

function savedRunKey(save) {
  if (!save) return null;
  const tier = save.tier === "auto" ? "auto" : "saved";
  return `${tier}:${save.save_id}`;
}

function getSelectedSavedRun() {
  return savedRunRowsCache.find(save => savedRunKey(save) === selectedSavedRunKey) || null;
}

function savedRunTierLabel(save) {
  return save?.tier === "auto" ? "Autosave" : "Saved";
}

function formatSavedRunTimestamp(value) {
  const raw = String(value || "").trim();
  if (!raw) return "";
  const compact = raw.match(/^(\d{4})(\d{2})(\d{2})[-_ ]?(\d{2})(\d{2})(\d{2})$/);
  if (compact) {
    return `${compact[1]}-${compact[2]}-${compact[3]} ${compact[4]}:${compact[5]}`;
  }
  const iso = raw.match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/);
  if (iso) {
    return `${iso[1]}-${iso[2]}-${iso[3]} ${iso[4]}:${iso[5]}`;
  }
  return raw;
}

function savedRunDownloadUrl(save) {
  if (!save) return "";
  const tier = save.tier === "auto" ? "auto" : "saved";
  return tier === "auto"
    ? `/api/runs/auto/${encodeURIComponent(save.save_id)}/download`
    : `/api/runs/saved/${encodeURIComponent(save.save_id)}/download`;
}

async function loadSettingsFromSavedRun(save) {
  if (!save) return false;
  try {
    const body = await loadSavedRunSettings(save.save_id, save.tier === "auto" ? "auto" : "saved");
    const loaded = await _loadTemporarySettingsFromRun(body, {
      kind: "saved-run",
      save_id: save.save_id,
      tier: save.tier === "auto" ? "auto" : "saved",
      label: save.label || body.label || save.save_id,
    });
    if (loaded) _setSavedRunsModalOpen(false);
    return loaded;
  } catch (error) {
    showToast(`Settings could not be loaded: ${error.message}`, "error");
    return false;
  }
}

async function activateSelectedSavedRun() {
  const selected = getSelectedSavedRun();
  if (!selected) return;
  if (savedRunsModalMode === "settings") {
    await loadSettingsFromSavedRun(selected);
  } else {
    await onLoadSavedRun(selected.save_id, selected.tier);
  }
}

function resetSavedRunDeleteConfirm() {
  if (savedRunDeleteConfirmTimer) {
    clearTimeout(savedRunDeleteConfirmTimer);
    savedRunDeleteConfirmTimer = null;
  }
  savedRunDeleteConfirmPending = false;
  const delBtn = $("#savedRunDeleteBtn");
  if (delBtn) {
    delBtn.textContent = "Delete";
    delBtn.classList.remove("confirm-pending");
    delBtn.title = "Delete selected run";
  }
}

function updateSavedRunFooterActions() {
  const selected = getSelectedSavedRun();
  const tier = selected?.tier === "auto" ? "auto" : "saved";
  const hasSelection = !!selected;
  const downloadBtn = $("#savedRunDownloadBtn");
  const saveBtn = $("#savedRunSaveBtn");
  const renameBtn = $("#savedRunRenameBtn");
  const deleteBtn = $("#savedRunDeleteBtn");
  const loadBtn = $("#savedRunLoadBtn");
  const loadSettingsBtn = $("#savedRunLoadSettingsBtn");

  if (downloadBtn) downloadBtn.disabled = !hasSelection;
  if (loadBtn) {
    loadBtn.disabled = !hasSelection;
    loadBtn.hidden = savedRunsModalMode === "settings";
  }
  if (loadSettingsBtn) {
    loadSettingsBtn.disabled = !hasSelection;
    loadSettingsBtn.hidden = false;
  }
  if (saveBtn) {
    saveBtn.hidden = !hasSelection || tier !== "auto";
    saveBtn.disabled = !hasSelection || tier !== "auto";
  }
  if (renameBtn) {
    renameBtn.hidden = !hasSelection || tier !== "saved";
    renameBtn.disabled = !hasSelection || tier !== "saved";
  }
  if (deleteBtn) {
    deleteBtn.hidden = !hasSelection;
    deleteBtn.disabled = !hasSelection;
    deleteBtn.title = hasSelection
      ? `Delete selected ${tier === "auto" ? "autosave" : "saved run"}`
      : "Delete selected run";
    if (!hasSelection) resetSavedRunDeleteConfirm();
  }
}

async function refreshSavedRunRows() {
  const rows = $("#savedRunRows");
  if (!rows) return;
  rows.innerHTML = "";
  let saves = [];
  try { saves = await listSavedRuns(); }
  catch (e) { showToast(e.message, "error"); return; }
  savedRunRowsCache = saves.map(save => ({
    ...save,
    tier: save.tier === "auto" ? "auto" : "saved",
  }));
  if (!savedRunRowsCache.some(save => savedRunKey(save) === selectedSavedRunKey)) {
    selectedSavedRunKey = savedRunRowsCache.length ? savedRunKey(savedRunRowsCache[0]) : null;
  }
  resetSavedRunDeleteConfirm();
  if (!savedRunRowsCache.length) {
    rows.innerHTML = '<p class="muted-line saved-run-empty">No saved runs</p>';
    updateSavedRunFooterActions();
    return;
  }
  for (const s of savedRunRowsCache) {
    const tier = s.tier;
    const key = savedRunKey(s);
    const isSelected = key === selectedSavedRunKey;
    const row = document.createElement("div");
    row.className = `saved-run-row${isSelected ? " is-selected" : ""}`;
    row.dataset.savedRunKey = key;
    row.setAttribute("role", "option");
    row.setAttribute("aria-selected", isSelected ? "true" : "false");
    row.tabIndex = 0;
    const formattedSavedAt = formatSavedRunTimestamp(s.saved_at);
    const previewUrl = savedRunPreviewUrl(s);
    row.innerHTML = `
      <div class="saved-run-preview" aria-hidden="true">
        ${previewUrl ? `<img src="${escAttr(previewUrl)}" alt="" loading="lazy" onerror="this.closest('.saved-run-preview')?.classList.add('is-unavailable')">` : ""}
        <span class="saved-run-preview-placeholder">Preview</span>
      </div>
      <div class="saved-run-main">
        <span class="saved-run-label">${esc(s.label || s.save_id)}</span>
        <span class="saved-run-tier">${esc(savedRunTierLabel(s))}</span>
      </div>
      <div class="saved-run-meta">
        <span class="saved-run-source">${esc(s.source_image_name || "Unknown source")}</span>
        <span class="saved-run-date" title="${esc(s.saved_at || "")}">${esc(formattedSavedAt)}</span>
      </div>
    `;
    const select = () => {
      selectedSavedRunKey = key;
      resetSavedRunDeleteConfirm();
      refreshSavedRunSelection();
    };
    row.addEventListener("click", select);
    row.addEventListener("dblclick", activateSelectedSavedRun);
    row.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        activateSelectedSavedRun();
        return;
      }
      if (e.key === " " || e.key === "Spacebar") {
        e.preventDefault();
        select();
      }
    });
    rows.appendChild(row);
  }
  updateSavedRunFooterActions();
}

function refreshSavedRunSelection() {
  document.querySelectorAll(".saved-run-row[data-saved-run-key]").forEach(row => {
    const selected = row.dataset.savedRunKey === selectedSavedRunKey;
    row.classList.toggle("is-selected", selected);
    row.setAttribute("aria-selected", selected ? "true" : "false");
  });
  updateSavedRunFooterActions();
}

async function promoteSelectedSavedRun() {
  const selected = getSelectedSavedRun();
  if (!selected || selected.tier !== "auto") return;
  try {
    const promoted = await promoteAutoRun(selected.save_id);
    selectedSavedRunKey = savedRunKey(promoted);
    await refreshSavedRunRows();
    showToast("Saved run promoted", "");
  } catch (err) { showToast(err.message, "error"); }
}

async function deleteSelectedSavedRun() {
  const selected = getSelectedSavedRun();
  if (!selected) return;
  const tier = selected.tier === "auto" ? "auto" : "saved";
  const delBtn = $("#savedRunDeleteBtn");
  if (savedRunDeleteConfirmPending) {
    resetSavedRunDeleteConfirm();
    try {
      if (tier === "auto") {
        await deleteAutoRun(selected.save_id);
      } else {
        await deleteSavedRun(selected.save_id);
      }
      selectedSavedRunKey = null;
      await refreshSavedRunRows();
    } catch (err) { showToast(err.message, "error"); }
    return;
  }
  savedRunDeleteConfirmPending = true;
  if (delBtn) {
    delBtn.textContent = "Confirm?";
    delBtn.classList.add("confirm-pending");
    delBtn.title = `Click again to delete selected ${tier === "auto" ? "autosave" : "saved run"}`;
  }
  savedRunDeleteConfirmTimer = setTimeout(resetSavedRunDeleteConfirm, 1800);
}

function downloadSelectedSavedRun() {
  const selected = getSelectedSavedRun();
  if (!selected) return;
  window.location.href = savedRunDownloadUrl(selected);
}

// In-app rename dialog (Stage 9b): an editable Display name + a read-only On-disk
// name (the save_id, so the user sees the zip filename won't change). Mirrors the
// settingsProfileModal / appDialog modal-overlay doctrine.
function openRenameSavedRunDialog(save) {
  const overlay = $("#renameSavedRunModal");
  const display = $("#renameSavedRunDisplay");
  const diskName = $("#renameSavedRunDiskName");
  const submit = $("#renameSavedRunSubmit");
  const cancelBtn = $("#renameSavedRunCancelBtn");
  const closeBtn = $("#renameSavedRunCancel");
  if (!overlay || !display || !diskName || !submit) return;
  display.value = save.label || "";
  diskName.value = save.save_id || "";
  const setOpen = (open) => {
    overlay.classList.toggle("is-hidden", !open);
    overlay.setAttribute("aria-hidden", open ? "false" : "true");
  };
  const close = () => {
    setOpen(false);
    submit.onclick = null;
    if (cancelBtn) cancelBtn.onclick = null;
    if (closeBtn) closeBtn.onclick = null;
    overlay.onclick = null;
  };
  submit.onclick = async () => {
    const newLabel = display.value.trim();
    if (!newLabel) return;
    close();
    try { await renameSavedRun(save.save_id, newLabel); await refreshSavedRunRows(); }
    catch (e) { showToast(e.message, "error"); }
  };
  if (cancelBtn) cancelBtn.onclick = close;
  if (closeBtn) closeBtn.onclick = close;
  overlay.onclick = (e) => { if (e.target === overlay) close(); };
  setOpen(true);
  setTimeout(() => { display.focus(); display.select(); }, 50);
}

async function onLoadSavedRun(saveId, tier = "saved") {
  try {
    const body = await loadSavedRun(saveId, tier);
    await applyLoadedRun(body);
    _setSavedRunsModalOpen(false);
    showToast("Loaded saved run", "");
  } catch (e) { showToast(e.message, "error"); }
}

async function restoreLoadedRunPaletteToDeck(body, support) {
  const cfg = body.config || {};
  const filamentIds = selectLoadedPalette(body, cfg);
  if (!filamentIds.length) return null;

  if (!savedPalettesData) {
    try {
      await loadSavedPalettes();
    } catch {
      savedPalettesData = savedPalettesData || { palettes: [] };
    }
    if (!savedPalettesData) savedPalettesData = { palettes: [] };
  }

  const action = chooseLoadedPaletteRestoreAction({
    filamentIds,
    support,
    deckCards: deck,
    savedPalettes: savedPalettesData?.palettes || [],
  });

  if (action.kind === "reuse-deck") {
    const existing = deck.find(card => card.id === action.cardId);
    if (!existing) return null;
    activateDeckCard(existing.id, { sync: false });
    existing.gamut = null;
    return existing;
  }

  if (action.kind === "load-saved") {
    return loadPaletteByIndex(action.savedIndex, { forceActive: true, sync: false, silent: true, allowUnavailable: true });
  }

  if (action.kind === "add-ad-hoc") {
    return addLoadedAdHocPaletteToDeck(action.filamentIds, body.label || "Loaded run palette");
  }

  return null;
}

async function applyLoadedRun(body) {
  const cfg = body.config || {};
  const loadedPalette = selectLoadedPalette(body, cfg);
  const loadedSupport = normalizeSupportFromLoadedConfig(cfg);
  const normalizedConfig = {
    ..._cloneValue(cfg),
    palette: [...loadedPalette],
    base_filament: loadedSupport.base_filament,
    cap_filament: loadedSupport.cap_filament,
    white_base: loadedSupport.white_base,
    white_cap: loadedSupport.white_cap,
  };
  // 1. New history card (fresh server card_id; never clobbers an existing run).
  solveRunCounter++;
  const loadedLabel = String(body.label || "").trim();
  solveRuns.push({
    id: body.card_id,
    label: loadedLabel || `Loaded ${solveRunCounter}`,
    loaded_from_archive: true,
    image: normalizedConfig.image_path ? { filename: normalizedConfig.image_path } : null,
    palette: [...loadedPalette],
    config: _cloneValue(normalizedConfig),
    ar: getEffectiveAR(),
    profile_ref: null,
    profile_name_at_solve: null,
    is_profile_modified_at_solve: false,
    recipe_snapshot: null,
    results: body.result || null,
    exportRecords: [],
    selectedExportId: null,
    timestamp: Date.now(),
  });
  selectedRunIds.clear();
  selectedRunIds.add(body.card_id);
  // 2. Repopulate the active image. The server extracted the source image into
  //    Prisma/photos/ under a save-scoped filename and rewrote config.image_path to
  //    match, so refresh the image list (loadImages) BEFORE the find so the extracted
  //    image is selectable.
  if (normalizedConfig.image_path) {
    await loadImages();
    const match = (availableImages || []).find(i => i.filename === normalizedConfig.image_path);
    if (match) { selectedImage = match; applyImageAspectDefault(); }
  }
  // 3. Repopulate wizard settings so a re-solve reproduces the run. `config` is the
  //    source of truth (no inverse "read controls into config"), so copy loaded keys
  //    in, mirroring _applySettingsProfileToConfig.
  for (const [k, v] of Object.entries(normalizedConfig)) config[k] = _cloneValue(v);
  // 3b. Restore the live frame + image-adjust globals. The solve payload is built from
  //     these (see syncConfigToServer: cfg.frame ← frameState, cfg.image_adjust ←
  //     imageAdjust), NOT from `config`, so without this a re-solve would use stale
  //     crop/rotation/pan/flip/adjustment. Mirror the session-startup restore (init()),
  //     adding the flips it omits.
  if (normalizedConfig.frame) {
    const f = normalizedConfig.frame;
    frameState.widthMm = clampFrameWidth(f.width_mm ?? 100);
    frameState.heightMm = clampFrameHeight(f.height_mm ?? 100);
    frameState.scale = f.scale ?? 100;
    frameState.rotation = f.rotation ?? 0;
    frameState.panX = f.pan_x ?? 0;
    frameState.panY = f.pan_y ?? 0;
    frameState.flipH = f.flip_h ?? false;
    frameState.flipV = f.flip_v ?? false;
  }
  if (normalizedConfig.image_adjust) Object.assign(imageAdjust, normalizedConfig.image_adjust);
  // 4. Re-render controls + image tab + solve history. renderSettingsTab() is the real
  //    settings-controls re-render the settings-profile load path (_doLoadSettingsProfile)
  //    calls after _applySettingsProfileToConfig.
  renderSettingsTab();
  await restoreLoadedRunPaletteToDeck({ ...body, config: normalizedConfig, palette: loadedPalette }, loadedSupport);
  renderImageTab();
  // 4b. renderImageTab() re-syncs the frame controls from frameState (syncScaleSlider /
  //     syncRotationSlider / syncDimFields / width+height sliders), but it does NOT touch
  //     the image-adjust sliders/inputs or the B/W toggle. Push imageAdjust into those
  //     DOM controls explicitly, mirroring the Reset-button sync block (~frameResetBtn).
  const adjustSyncPairs = [
    ["adjustExposure", "exposure"], ["adjustContrast", "contrast"],
    ["adjustHighlight", "highlight"], ["adjustShadow", "shadow"],
    ["adjustTintHue", "tint_hue"], ["adjustTintStrength", "tint_strength"],
    ["adjustSaturation", "saturation"], ["adjustTemp", "temperature"],
  ];
  for (const [id, key] of adjustSyncPairs) {
    const val = imageAdjust[key] ?? 0;
    const inp = $(`#${id}`);
    const sld = $(`#${id}Slider`);
    if (inp) inp.value = val;
    if (sld) sld.value = val;
  }
  $$("#bwColorToggle .toggle-btn").forEach(b =>
    b.classList.toggle("is-active", b.dataset.val === imageAdjust.mode));
  const colorCtrl = $("#colorControls");
  if (colorCtrl) colorCtrl.style.display = imageAdjust.mode === "bw" ? "none" : "";
  renderFrameCanvas();
  renderSolveTab();
  if (currentTab === "export") renderExportTab();
}

function showWhenRuleMatches(rule, getActualValue) {
  if (!rule) return true;
  for (const [param, expected] of Object.entries(rule)) {
    const actual = getActualValue(param);
    const expectedValues = Array.isArray(expected) ? expected : [expected];
    if (!expectedValues.some(value => String(actual) === String(value))) {
      return false;
    }
  }
  return true;
}

function isModuleParamVisibleInSummary(param, configValues) {
  return showWhenRuleMatches(param.show_when, key => configValues[key]);
}

function formatSolveSummaryValue(param, rawValue) {
  if (typeof rawValue === "boolean") return rawValue ? "on" : "off";
  if (param?.type === "choice" && typeof rawValue === "string") {
    return rawValue.replace(/_/g, " ");
  }
  return param?.unit ? `${rawValue} ${param.unit}` : rawValue;
}

function formatSolveSummaryMm(rawValue) {
  const num = Number(rawValue);
  if (!Number.isFinite(num)) return "\u2014";
  return `${num.toFixed(2).replace(/\.?0+$/, "")} mm`;
}

function getSolveRunEssentialsItems(run) {
  // Stage 8 essentials, bound to a completed run's recipe snapshot (never live config).
  const settings = getSolveRunSettingsSnapshot(run);
  const isLuminance = normalizeLuminanceMode(settings.luminance_mode) === "luminance_detail";
  const items = [
    { label: "Mode", value: isLuminance ? "Luminance" : "Color" },
    { label: "Solve pitch", value: formatSolveSummaryMm(settings.solver_fine_pitch_mm || settings.image_sample_pitch_mm) },
    { label: "Layer height", value: formatSolveSummaryMm(settings.layer_height) },
    { label: "Max thickness", value: formatSolveSummaryMm(settings.t_max) },
    { label: "Color region target", value: `${settings.color_region_target_mm ?? 0.60} mm` },
    { label: "Detail limit", value: `${settings.detail_cap_max_layers ?? 5} layers` },
    { label: "Base thickness", value: formatSolveSummaryMm(settings.d_wb) },
  ];
  const preprocessing = getSolveRunActiveModulesForSlot(run, "preprocessing").map((name) => {
    const desc = moduleDescriptorById(name);
    return desc ? moduleDisplayName(desc) : humanizeModuleName(name);
  });
  if (preprocessing.length) {
    items.push({ label: "Pre-processing", value: preprocessing.join(", ") });
  }
  const swapGrouping = run?.results?.staged_metrics?.swap_grouping;
  const swapGroups = Array.isArray(swapGrouping?.groups) ? swapGrouping.groups : [];
  if (swapGroups.length) {
    const groupSizes = swapGroups.map((group) => Array.isArray(group) ? group.length : 0);
    items.push({
      label: "Swap groups",
      value: `${swapGroups.length} (${groupSizes.join(" + ")} colors)`,
    });
  }
  let bandHeights = Array.isArray(swapGrouping?.band_heights_mm)
    ? swapGrouping.band_heights_mm
    : [];
  if (!bandHeights.length && Array.isArray(swapGrouping?.band_layers)) {
    const bandLayerHeight = Number(swapGrouping?.layer_height_mm);
    if (Number.isFinite(bandLayerHeight)) {
      bandHeights = swapGrouping.band_layers.map((layers) => Number(layers) * bandLayerHeight);
    }
  }
  if (bandHeights.length && bandHeights.every((height) => Number.isFinite(Number(height)))) {
    items.push({
      label: "Band heights",
      value: bandHeights.map((height) => `${Number(height).toFixed(2)} mm`).join(" / "),
    });
  }
  const pauseHeights = Array.isArray(swapGrouping?.pause_z_mm) ? swapGrouping.pause_z_mm : [];
  if (swapGroups.length) {
    const pauseValue = pauseHeights.length
      ? `${pauseHeights.length} (${pauseHeights.map((height) => `z=${Number(height).toFixed(2)} mm`).join(", ")})`
      : "0";
    items.push({ label: "Pause count", value: pauseValue });
  }
  const medianBandingCost = swapGrouping?.banding_cost?.median_de_delta;
  if (Number.isFinite(medianBandingCost)) {
    const sign = medianBandingCost >= 0 ? "+" : "";
    items.push({
      label: "Swap banding cost",
      value: `median ${sign}${medianBandingCost.toFixed(2)} dE`,
    });
  }
  const swapAvailability = run?.results?.staged_metrics?.swap_plan_availability;
  if (swapAvailability?.available === false && swapAvailability?.reason) {
    items.push({ label: "Swap plan", value: swapAvailability.reason });
  }
  return items;
}

function renderSolveProgress() {
  // Reuse the shared op-progress floating bar (same as compare tab)
  const el = $("#opProgress");
  const lbl = $("#opProgressLabel");
  const elapsed = $("#opProgressElapsed");
  const fill = el?.querySelector(".op-progress-fill");
  const cancelBtn = $("#opProgressCancel");

  if (solveStatus.status === "idle") {
    // Only hide if we were the one showing it
    if (el && el.dataset.owner === "solve") {
      el.classList.add("is-hidden");
      el.dataset.owner = "";
    }
    return;
  }

  if (solveStatus.status === "running") {
    clearTimeout(solveProgressHideTimer);
    solveProgressHideTimer = null;
    const d = solveStatus.progress_detail || {};
    el.classList.remove("is-hidden");
    el.dataset.owner = "solve";
    el.dataset.cancellable = "true";
    if (cancelBtn) cancelBtn.hidden = false;

    // Label: stage info + stage label
    let label = d.stage_label || solveStatus.progress || "Solving...";
    if (solveCancelPending || solveStatus.cancel_requested) {
      label = `Cancellation requested: ${label}`;
    }
    if (d.stage_index && d.stage_count) {
      label = `Step ${d.stage_index}/${d.stage_count}: ${label}`;
    }
    if (lbl) lbl.textContent = label;
    if (cancelBtn) cancelBtn.disabled = !!(solveCancelPending || solveStatus.cancel_requested);

    // Progress bar
    if (fill) {
      const overallPct = Number(d.overall_pct);
      if (d.overall_pct != null && Number.isFinite(overallPct)) {
        const boundedPct = Math.max(0, Math.min(100, overallPct));
        fill.className = "op-progress-fill";
        fill.style.width = `${boundedPct}%`;
        el.setAttribute("role", "progressbar");
        el.setAttribute("aria-valuemin", "0");
        el.setAttribute("aria-valuemax", "100");
        el.setAttribute("aria-valuenow", String(Math.round(boundedPct)));
      } else {
        fill.className = "op-progress-fill indeterminate";
        fill.style.width = "";
        el.setAttribute("role", "progressbar");
        el.removeAttribute("aria-valuenow");
      }
    }

    // Elapsed
    const elapsedVal = solveStatus.elapsed_s ?? d.elapsed_s ?? 0;
    if (elapsed) setOperationElapsedSeconds(elapsedVal);

  } else if (solveStatus.status === "complete") {
    if (cancelBtn) cancelBtn.disabled = false;
    if (el && el.dataset.owner === "solve") {
      if (lbl) lbl.textContent = "Solve complete";
      if (fill) {
        fill.className = "op-progress-fill";
        fill.style.width = "100%";
      }
      el.setAttribute("role", "progressbar");
      el.setAttribute("aria-valuemin", "0");
      el.setAttribute("aria-valuemax", "100");
      el.setAttribute("aria-valuenow", "100");
      clearTimeout(solveProgressHideTimer);
      solveProgressHideTimer = setTimeout(() => {
        if (el.dataset.owner === "solve") {
          el.classList.add("is-hidden");
          el.dataset.owner = "";
        }
      }, 700);
    }
  } else if (solveStatus.status === "error" || solveStatus.status === "cancelled") {
    if (cancelBtn) cancelBtn.disabled = false;
    if (el && el.dataset.owner === "solve") {
      if (lbl) {
        lbl.textContent = solveStatus.status === "cancelled"
          ? "Solve cancelled"
          : `Error: ${solveStatus.progress}`;
      }
      // Auto-hide after a moment
      const terminalStatus = solveStatus.status;
      clearTimeout(solveProgressHideTimer);
      solveProgressHideTimer = setTimeout(() => {
        if (el.dataset.owner === "solve" && solveStatus.status === terminalStatus) {
          el.classList.add("is-hidden");
          el.dataset.owner = "";
        }
      }, 3000);
    }
  }
}

// _getActiveSolveResult removed — replaced by getSelectedRuns()

// ── Surface diagnostic utilities (shared by Task 3 Highpass + Task 4 Explorer) ──

/**
 * Load a binary surface blob (uint32 height, uint32 width, float32[] data).
 * Returns {width, height, data: Float32Array} or null on failure.
 */
async function loadSurfaceBlob(url) {
  try {
    const resp = await fetch(url);
    if (!resp.ok) return null;
    const buf = await resp.arrayBuffer();
    const header = new Uint32Array(buf, 0, 2);
    const height = header[0], width = header[1];
    const data = new Float32Array(buf, 8);
    if (data.length !== width * height) return null;
    return { width, height, data };
  } catch { return null; }
}

/**
 * Load a binary uint32 raster blob (uint32 height, uint32 width, uint32[] data).
 * Returns {width, height, data: Uint32Array} or null on failure.
 */
async function loadUint32Blob(url) {
  try {
    const resp = await fetch(url);
    if (!resp.ok) return null;
    const buf = await resp.arrayBuffer();
    if (buf.byteLength < 8 || ((buf.byteLength - 8) % 4) !== 0) return null;
    const header = new Uint32Array(buf, 0, 2);
    const height = header[0], width = header[1];
    const data = new Uint32Array(buf, 8);
    if (data.length !== width * height) return null;
    return { width, height, data };
  } catch { return null; }
}

/** Cached surface data per solve run. Keyed by run ID. */
const surfaceDataCache = {};
const solveContourDataCache = {};
const explorerMaterialDataCache = {};

// ── Thickness blob caches (Cap Diff / Color Diff) ──────────────────────
// Both keyed by URL string at the inner layer so re-solves invalidate naturally
// via cache-busting query strings. The outer per-run/per-filament dicts mirror
// the source-of-truth shapes the inspector code expects.
const capThicknessCache = {};        // run.id -> { width, height, data }
const filamentThicknessCache = {};   // run.id -> { filamentId -> { width, height, data } }

function invalidateSolveRunCaches(runOrId) {
  const run = typeof runOrId === "string" ? null : runOrId;
  const runId = typeof runOrId === "string" ? runOrId : run?.id;
  if (!runId) return;
  delete surfaceDataCache[runId];
  delete explorerMaterialDataCache[runId];
  delete capThicknessCache[runId];
  delete filamentThicknessCache[runId];
  delete recipeDataCache[runId];
  delete recipeDataPromiseCache[runId];
  delete recipeCookbookPromiseCache[runId];
  recipeDataGeneration[runId] = (recipeDataGeneration[runId] || 0) + 1;
  if (run?.results) {
    Object.values(run.results).forEach((value) => {
      if (typeof value === "string") delete solveContourDataCache[value];
    });
  } else {
    Object.keys(solveContourDataCache).forEach((key) => delete solveContourDataCache[key]);
  }
}

async function ensureCapThickness(run) {
  if (!run?.results?.cap_height_bin_url) return null;
  if (capThicknessCache[run.id]) return capThicknessCache[run.id];
  const blob = await loadSurfaceBlob(run.results.cap_height_bin_url);
  if (!blob) return null;
  capThicknessCache[run.id] = blob;
  return blob;
}

async function ensureFilamentThickness(run, filamentId) {
  const url = run?.results?.filament_bin_urls?.[filamentId];
  if (!url) return null;
  if (!filamentThicknessCache[run.id]) filamentThicknessCache[run.id] = {};
  if (filamentThicknessCache[run.id][filamentId]) return filamentThicknessCache[run.id][filamentId];
  const blob = await loadSurfaceBlob(url);
  if (!blob) return null;
  filamentThicknessCache[run.id][filamentId] = blob;
  return blob;
}

// ── View predicates ────────────────────────────────────────────────────
function isSolveCapDiffView(view) {
  return view === "cap_diff";
}
function isSolveFilamentDiffView(view) {
  return view === "filament_diff";
}
function isSolveThicknessDiffView(view) {
  return isSolveCapDiffView(view) || isSolveFilamentDiffView(view);
}

// ── Filament selection helpers ─────────────────────────────────────────
function getRunFilamentMapInfo(run, filamentId) {
  if (!run?.results || !filamentId) return null;
  const maps = run.results.filament_maps || [];
  return maps.find((m) => m.filament_id === filamentId) || null;
}

function getSolveFilamentDiffOptions(selectedRuns = getSelectedRuns().filter((r) => r.results)) {
  const options = [];
  const seen = new Set();
  selectedRuns.forEach((run) => {
    const maps = run.results?.filament_maps || [];
    maps.forEach((mapInfo) => {
      const filamentId = mapInfo?.filament_id || "";
      if (!filamentId || filamentId.startsWith("__") || seen.has(filamentId)) return;
      const fil = filamentById(filamentId);
      options.push({
        filament_id: filamentId,
        label: fil?.color_name || filamentId,
        hex: fil?.hex || "#888",
      });
      seen.add(filamentId);
    });
  });
  return options;
}

function ensureSolveFilamentDiffSelection(selectedRuns = getSelectedRuns().filter((r) => r.results)) {
  const options = getSolveFilamentDiffOptions(selectedRuns);
  if (!options.length) {
    solveFilamentDiffId = "";
    return "";
  }
  if (!options.some((opt) => opt.filament_id === solveFilamentDiffId)) {
    solveFilamentDiffId = options[0].filament_id;
  }
  return solveFilamentDiffId;
}

function syncSolveFilamentDiffControl(selectedRuns = getSelectedRuns().filter((r) => r.results)) {
  const select = $("#solveFilamentDiffSelect");
  if (!select) return;
  const options = getSolveFilamentDiffOptions(selectedRuns);
  const activeId = ensureSolveFilamentDiffSelection(selectedRuns);
  select.innerHTML = options.map((opt) => `<option value="${esc(opt.filament_id)}">${esc(opt.label)}</option>`).join("");
  select.disabled = !options.length;
  select.value = activeId;
}

// ── Diff compute ───────────────────────────────────────────────────────
function computeSolveCapDiff(beforeCap, afterCap, eps = 1e-6) {
  if (!beforeCap || !afterCap) return null;
  if (beforeCap.width !== afterCap.width || beforeCap.height !== afterCap.height) return null;
  const len = beforeCap.data.length;
  const delta = new Float32Array(len);
  let changedPx = 0;
  let addedPx = 0;
  let removedPx = 0;
  let beforeActivePx = 0;
  let afterActivePx = 0;
  let maxAbsDelta = 0;
  let totalAbsDelta = 0;
  for (let i = 0; i < len; i++) {
    const before = beforeCap.data[i];
    const after = afterCap.data[i];
    const diff = after - before;
    const absDiff = Math.abs(diff);
    delta[i] = diff;
    if (before > eps) beforeActivePx++;
    if (after > eps) afterActivePx++;
    if (absDiff > eps) {
      changedPx++;
      totalAbsDelta += absDiff;
      if (diff > eps) addedPx++;
      else if (diff < -eps) removedPx++;
      if (absDiff > maxAbsDelta) maxAbsDelta = absDiff;
    }
  }
  return {
    width: beforeCap.width,
    height: beforeCap.height,
    delta,
    changedPx,
    addedPx,
    removedPx,
    beforeActivePx,
    afterActivePx,
    maxAbsDelta,
    meanAbsDelta: changedPx ? (totalAbsDelta / changedPx) : 0,
    eps,
  };
}

// ── Diff render ────────────────────────────────────────────────────────
function renderSolveCapDiffCanvas(canvas, diff, mode) {
  if (!canvas || !diff) return;
  canvas.width = diff.width;
  canvas.height = diff.height;
  const ctx = canvas.getContext("2d");
  const img = ctx.createImageData(diff.width, diff.height);
  const px = img.data;
  const scale = diff.maxAbsDelta > diff.eps ? diff.maxAbsDelta : 1.0;
  for (let i = 0; i < diff.delta.length; i++) {
    const d = diff.delta[i];
    const absDiff = Math.abs(d);
    const off = i * 4;
    let r = 0, g = 0, b = 0;
    if (mode === "changed") {
      if (absDiff > diff.eps) r = g = b = 255;
    } else if (mode === "added") {
      if (d > diff.eps) g = 255;
    } else if (mode === "removed") {
      if (d < -diff.eps) r = 255;
    } else if (absDiff > diff.eps) {
      const intensity = Math.max(32, Math.min(255, Math.round((absDiff / scale) * 255)));
      if (d > 0) g = intensity;
      else r = intensity;
    }
    px[off] = r;
    px[off + 1] = g;
    px[off + 2] = b;
    px[off + 3] = 255;
  }
  ctx.putImageData(img, 0, 0);
}

// ── Inline diff summary (rendered into solve-diff-summary div) ─────────
function formatSolveDiffMm(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return "—";
  return `${num.toFixed(3).replace(/\.?0+$/, "")} mm`;
}

function getCurrentSolveCapDiffFromCache() {
  if (!isSolveCapDiffView(solveView)) return null;
  const selectedRuns = getSelectedRuns().filter((r) => r.results);
  if (selectedRuns.length !== 2) return null;
  const [beforeRun, afterRun] = selectedRuns;
  const beforeCap = capThicknessCache[beforeRun.id];
  const afterCap = capThicknessCache[afterRun.id];
  if (!beforeCap || !afterCap) return null;
  return computeSolveCapDiff(beforeCap, afterCap);
}

function getCurrentSolveFilamentDiffFromCache() {
  if (!isSolveFilamentDiffView(solveView)) return null;
  const selectedRuns = getSelectedRuns().filter((r) => r.results);
  if (selectedRuns.length !== 2) return null;
  const filamentId = ensureSolveFilamentDiffSelection(selectedRuns);
  if (!filamentId) return null;
  const [beforeRun, afterRun] = selectedRuns;
  const beforeFil = filamentThicknessCache[beforeRun.id]?.[filamentId];
  const afterFil = filamentThicknessCache[afterRun.id]?.[filamentId];
  if (!beforeFil || !afterFil) return null;
  return computeSolveCapDiff(beforeFil, afterFil);
}

function buildSolveCapDiffSummaryHtml(diff) {
  if (!diff) return `<div class="muted-line">Diff unavailable for this pair.</div>`;
  return `
    <div class="solve-diff-stat"><span class="solve-diff-stat-label">Changed</span><span class="solve-diff-stat-value">${diff.changedPx.toLocaleString()} px</span></div>
    <div class="solve-diff-stat"><span class="solve-diff-stat-label">Added</span><span class="solve-diff-stat-value">${diff.addedPx.toLocaleString()} px</span></div>
    <div class="solve-diff-stat"><span class="solve-diff-stat-label">Removed</span><span class="solve-diff-stat-value">${diff.removedPx.toLocaleString()} px</span></div>
    <div class="solve-diff-stat"><span class="solve-diff-stat-label">Before active</span><span class="solve-diff-stat-value">${diff.beforeActivePx.toLocaleString()} px</span></div>
    <div class="solve-diff-stat"><span class="solve-diff-stat-label">After active</span><span class="solve-diff-stat-value">${diff.afterActivePx.toLocaleString()} px</span></div>
    <div class="solve-diff-stat"><span class="solve-diff-stat-label">Max abs delta</span><span class="solve-diff-stat-value">${formatSolveDiffMm(diff.maxAbsDelta)}</span></div>
    <div class="solve-diff-stat"><span class="solve-diff-stat-label">Mean abs delta</span><span class="solve-diff-stat-value">${formatSolveDiffMm(diff.meanAbsDelta)}</span></div>`;
}

// ── Inspector card primitives ─────────────────────────────────────────
function buildSolveInspectorBlock(title, items, extraMeta = "", chipsHtml = "") {
  const rows = items.map(({ label, value }) => `
      <div class="solve-inspector-item">
        <span>${esc(label)}</span>
        <strong>${esc(value ?? "—")}</strong>
      </div>`).join("");
  return `
    <section class="solve-inspector-block">
      <div class="solve-inspector-title">${esc(title)}</div>
      ${chipsHtml ? `<div class="solve-inspector-chips">${chipsHtml}</div>` : ""}
      ${extraMeta ? `<div class="muted-line" style="margin-bottom:8px">${esc(extraMeta)}</div>` : ""}
      <div class="solve-inspector-items">${rows}</div>
    </section>`;
}

function getSolveRunSummaryItems(run) {
  const profileInfo = describeSolveRunProfile(run);
  return [
    { label: "Profile", value: profileInfo.name },
    ...getSolveRunEssentialsItems(run),
  ];
}

function buildSolveRunCardMetadataFooter(run) {
  const items = getSolveRunSummaryItems(run);
  const itemsHtml = items.map(({ label, value }) => `
      <span class="solve-card-meta-item">
        <span>${esc(label)}</span>
        <strong>${esc(value ?? "—")}</strong>
      </span>`).join("");
  return `
    <div class="solve-card-run-meta">
      <div class="solve-card-meta-items">${itemsHtml}</div>
    </div>`;
}

// ── Settings-diff machinery (Inspector "Changed Settings" blocks) ─────
const SOLVE_DIFF_SETTING_LABELS = {
  image_sample_pitch_mm: "Solve Pitch",
  solver_fine_pitch_mm: "Solve Pitch",
  layer_height: "Layer Height",
  d_wb: "White Base Thickness",
  d_wc_min: "Min White Cap",
  t_max: "Max Total Thickness",
  k_max: "Max Colors per Region",
  de_threshold: "Color Mismatch Tolerance",
  chroma_weight: "Chroma Weight",
  luminance_mode: "Luminance Mode",
  cell_mode: "Region Method",
  stage1_coarsening_factor: "Region Planning Scale",
  color_region_target_mm: "Color Region Target",
  smooth_radius_mm: "Cliff Smooth Radius",
  hybrid_split_ratio: "Hybrid Split Ratio",
  detail_cap_smoothing_enabled: "Detail Cap Smoothing",
  detail_cap_smoothing_exact_speckle_max_px: "Detail Exact Speckle Cleanup",
  detail_cap_smoothing_cumulative_component_max_px: "Detail Island Cleanup",
  detail_cap_smoothing_cumulative_hole_max_px: "Detail Hole Cleanup",
  cap_mode: "Boundary Cap Mode",
  boundary_cap_de_budget: "Boundary Cap Appearance Budget",
  smooth_kernel: "Smoothing Radius",
};

function humanizeModuleName(name) {
  return String(name || "").replace(/_/g, " ");
}

function getSolveSettingLabel(key) {
  if (SOLVE_DIFF_SETTING_LABELS[key]) return SOLVE_DIFF_SETTING_LABELS[key];
  for (const mod of moduleData || []) {
    const param = Object.values(mod.params || {}).find(
      (p) => p.name === key || p.storage_key === key,
    );
    if (param?.label) return param.label;
  }
  return humanizeModuleName(key);
}

function formatSolveSettingValue(key, value) {
  if (typeof value === "boolean") return value ? "on" : "off";
  if (value == null || value === "") return "—";
  if (Array.isArray(value)) {
    if (key === "palette") {
      return value.map((id) => filamentById(id)?.color_name || id).join(", ");
    }
    return value.map((item) => formatSolveSettingValue(key, item)).join(", ");
  }
  if (typeof value === "number") {
    if (/(_mm|_deg)$/.test(key) || ["layer_height", "d_wb", "d_wc_min", "t_max", "de_threshold", "smooth_kernel", "boundary_cap_de_budget"].includes(key)) {
      return String(value).includes(".")
        ? value.toFixed(3).replace(/\.?0+$/, "")
        : String(value);
    }
    if (["chroma_weight", "hybrid_split_ratio"].includes(key)) {
      return value.toFixed(3).replace(/\.?0+$/, "");
    }
    return String(value);
  }
  if (typeof value === "string" && (key === "cap_mode" || key === "luminance_mode")) {
    return value.replace(/_/g, " ");
  }
  if (typeof value === "string" && key === "cell_mode") {
    return formatRegionMethod(value);
  }
  return String(value);
}

function getSolveRunSettingsSnapshot(run) {
  return _cloneValue(run?.recipe_snapshot?.profile_snapshot?.settings || run?.config || {});
}

function getSolveRunModulesSnapshot(run) {
  return _normalizeSettingsProfileModules(
    run?.recipe_snapshot?.profile_snapshot?.modules,
    getSolveRunSettingsSnapshot(run),
  );
}

const SOLVE_DIFF_CATEGORY_ORDER = [
  "geometry",
  "preprocessing",
  "solver",
  "white-cap",
  "other",
];

const SOLVE_DIFF_CATEGORY_TITLES = {
  geometry: "Changed Settings · Essentials",
  preprocessing: "Changed Settings · Preprocessing",
  solver: "Changed Settings · Color Solver",
  "white-cap": "Changed Settings · White Cap",
  other: "Changed Settings · Other",
};

function categorizeSolveSettingDiff(key, kind = "setting") {
  if (kind === "preprocessing") return "preprocessing";
  if ([
    "layer_height", "d_wb", "d_wc_min", "t_max", "k_max",
    "base_filament", "cap_filament",
  ].includes(key)) return "geometry";
  if ([
    "image_sample_pitch_mm", "solver_fine_pitch_mm",
    "color_region_target_mm", "chroma_weight", "luminance_mode", "cell_mode",
    "luminance_handler_enabled",
    "luminance_handler_mode",
    "luminance_handler_strength",
    "luminance_handler_optical_authority_fraction",
    "luminance_base_shading_limit_fraction",
    "luminance_handler_boundary_percentile",
    "luminance_handler_boundary_sigma_px",
    "luminance_handler_response_curve",
    "luminance_handler_response_gamma",
    "luminance_handler_detail_residual",
    "luminance_handler_include_solver_detail",
    "luminance_detail_authoring_printability",
  ].includes(key)) return "solver";
  if ([
    "cap_mode", "boundary_cap_de_budget", "smooth_kernel",
    "smooth_radius_mm", "hybrid_split_ratio",
    "detail_cap_max_layers",
    "detail_cap_smoothing_enabled",
    "detail_cap_smoothing_exact_speckle_max_px",
    "detail_cap_smoothing_cumulative_component_max_px",
    "detail_cap_smoothing_cumulative_hole_max_px",
  ].includes(key)) return "white-cap";
  return "other";
}

function getSortedModuleParams(mod) {
  return Object.values(mod?.params || {}).sort((a, b) => {
    const orderA = a?.order ?? 0;
    const orderB = b?.order ?? 0;
    if (orderA !== orderB) return orderA - orderB;
    return String(a?.name || "").localeCompare(String(b?.name || ""));
  });
}

function getSolveModuleParamLabel(moduleId, param) {
  const prefix = humanizeModuleName(moduleId);
  const suffix = param?.label || humanizeModuleName(moduleParamStorageKey(moduleId, param));
  return `${prefix} · ${suffix}`;
}

function collectPreprocessingSettingDiffs(beforeSettings, afterSettings, beforeModules, afterModules) {
  const diffs = [];
  const preprocessingModules = (moduleData || []).filter((entry) => entry.slot === "preprocessing");
  for (const mod of preprocessingModules) {
    const moduleName = mod.name;
    const beforeEnabled = !!beforeModules[moduleName];
    const afterEnabled = !!afterModules[moduleName];
    if (!beforeEnabled && !afterEnabled) continue;
    for (const param of getSortedModuleParams(mod)) {
      const beforeValue = getModuleParamValue(beforeSettings, moduleName, param);
      const afterValue = getModuleParamValue(afterSettings, moduleName, param);
      if (_settingsProfileValuesEqual(beforeValue, afterValue)) continue;
      const valueKey = moduleParamStorageKey(moduleName, param);
      diffs.push({
        label: getSolveModuleParamLabel(moduleName, param),
        before: formatSolveSettingValue(valueKey, beforeValue),
        after: formatSolveSettingValue(valueKey, afterValue),
        sortKey: `${moduleName}:${valueKey}`,
        category: "preprocessing",
      });
    }
  }
  return diffs;
}

function collectSolveRunSettingDiffs(beforeRun, afterRun) {
  const beforeSettings = getSolveRunSettingsSnapshot(beforeRun);
  const afterSettings = getSolveRunSettingsSnapshot(afterRun);
  const beforeModules = getSolveRunModulesSnapshot(beforeRun);
  const afterModules = getSolveRunModulesSnapshot(afterRun);
  const diffs = [];

  for (const key of SETTINGS_PROFILE_KEYS) {
    if (key === "preprocessing_params") continue;
    if (_settingsProfileValuesEqual(beforeSettings[key], afterSettings[key])) continue;
    diffs.push({
      label: getSolveSettingLabel(key),
      before: formatSolveSettingValue(key, beforeSettings[key]),
      after: formatSolveSettingValue(key, afterSettings[key]),
      sortKey: key,
      category: categorizeSolveSettingDiff(key, "setting"),
    });
  }

  diffs.push(...collectPreprocessingSettingDiffs(
    beforeSettings, afterSettings, beforeModules, afterModules,
  ));

  for (const mod of (moduleData || []).filter((entry) => entry.slot === "preprocessing")) {
    const name = mod.name;
    const beforeEnabled = !!beforeModules[name];
    const afterEnabled = !!afterModules[name];
    if (beforeEnabled === afterEnabled) continue;
    diffs.push({
      label: `${humanizeModuleName(name)} module`,
      before: beforeEnabled ? "on" : "off",
      after: afterEnabled ? "on" : "off",
      sortKey: `module:${name}`,
      category: "preprocessing",
    });
  }

  return diffs.sort((a, b) => {
    const categoryDelta = SOLVE_DIFF_CATEGORY_ORDER.indexOf(a.category) - SOLVE_DIFF_CATEGORY_ORDER.indexOf(b.category);
    if (categoryDelta !== 0) return categoryDelta;
    return a.label.localeCompare(b.label);
  });
}

function buildGroupedSolveSettingDiffBlocks(beforeRun, afterRun, diffs) {
  const groups = new Map();
  for (const diff of diffs) {
    if (!groups.has(diff.category)) groups.set(diff.category, []);
    groups.get(diff.category).push(diff);
  }
  const meta = `${beforeRun.label} -> ${afterRun.label}`;
  const blocks = [];
  let first = true;
  for (const category of SOLVE_DIFF_CATEGORY_ORDER) {
    const entries = groups.get(category);
    if (!entries?.length) continue;
    blocks.push(buildSolveInspectorBlock(
      SOLVE_DIFF_CATEGORY_TITLES[category] || "Changed Settings",
      entries.map((diff) => ({
        label: diff.label,
        value: `${diff.before} -> ${diff.after}`,
      })),
      first ? meta : "",
    ));
    first = false;
  }
  return blocks;
}

// ── Inspector card content (per-run + diff summary blocks) ────────────
function getSolveRunActiveModulesForSlot(run, slot) {
  const diagnostics = run?.results?.solve_start_diagnostics || {};
  const active = diagnostics.active_modules || {};
  if (Array.isArray(active[`${slot}s`]) && active[`${slot}s`].length) {
    return active[`${slot}s`];
  } else if (slot === "preprocessing" && Array.isArray(active.preprocessing) && active.preprocessing.length) {
    return active.preprocessing;
  }
  const modules = getSolveRunModulesSnapshot(run);
  return (moduleData || [])
    .filter((mod) => mod.slot === slot && modules[mod.name])
    .map((mod) => mod.name);
}

function buildSolveRunInspectorBlock(run) {
  // Shared run summary: palette chips + the run-bound essentials list.
  return buildSolveInspectorBlock(
    run.label,
    getSolveRunSummaryItems(run),
    "",
    buildSolveRunPaletteChips(run),
  );
}

function buildSolveRunEssentialsSummary(run) {
  // Compact run-bound summary used by the shared hover/focus preview.
  return buildSolveRunInspectorBlock(run);
}

const READ_ONLY_RUN_SETTING_SECTIONS = [
  {
    key: "essentials",
    title: "Essentials",
    rows: [
      { key: "luminance_mode", label: "Solve Mode", format: "solve-mode" },
      { key: "solver_fine_pitch_mm", fallbackKey: "image_sample_pitch_mm", label: "Solve Pitch", unit: "mm" },
      { key: "t_max", label: "Max Total Thickness", unit: "mm" },
      { key: "layer_height", label: "Layer Height", unit: "mm" },
      { key: "base_filament", label: "White Base/Cap Filament", format: "filament" },
      { key: "d_wb", label: "Base Thickness", unit: "mm" },
    ],
  },
  {
    key: "preprocessing",
    title: "Pre-processing",
    rows: [
      { key: "source_resample_kernel", label: "Resample Kernel", format: "title", advanced: true },
    ],
  },
  {
    key: "solver",
    title: "Color Solver",
    rows: [
      { key: "appearance_model_provider", label: "Appearance Model", format: "appearance-model" },
      { key: "use_corrections", label: "Color Corrections" },
      { key: "k_max", label: "Max Colors per Region" },
      { key: "color_region_target_mm", label: "Color Region Target", unit: "mm" },
      { key: "gamut_mode", label: "Out-of-gamut Handling", format: "gamut-mode", advanced: true },
      { key: "gamut_white_rescale", label: "White-point Rescale", advanced: true },
      { key: "de_threshold", label: "Color Mismatch Tolerance", unit: "dE", advanced: true },
      { key: "chroma_weight", label: "Chroma Weight", advanced: true },
      { key: "cell_mode", label: "Region Method", format: "region-method", advanced: true },
      { key: "stage1_coarsening_factor", label: "Region Planning Scale", format: "region-scale", advanced: true },
      { key: "stage2_fine_override_enabled", label: "Local Recipe Corrections", advanced: true },
      { key: "stage2_boundary_mutation_enabled", label: "Boundary Mutation", advanced: true },
      { key: "stage2_boundary_mutation_max_passes", label: "Mutation Passes", advanced: true },
      { key: "stage2_boundary_mutation_current_de_percentile", label: "Mutation Current-dE Percentile", advanced: true },
      { key: "stage2_boundary_mutation_min_gain", label: "Mutation Min Gain", unit: "dE", advanced: true },
      { key: "stage2_boundary_mutation_min_component_mm", label: "Mutation Min Contact", unit: "mm", advanced: true },
      { key: "luminance_base_shading_limit_fraction", label: "Shading Balance", format: "percent", advanced: true },
      { key: "luminance_detail_authoring_printability", label: "Detail Printability", advanced: true },
    ],
  },
  {
    key: "white-cap",
    title: "White Cap",
    rows: [
      { key: "d_wc_min", label: "Min Cap Layers", format: "cap-layers" },
      { key: "smooth_kernel", label: "Smoothing Radius", format: "smooth-radius" },
      { key: "detail_cap_max_layers", label: "Detail Depth", unit: "layers" },
      { key: "cap_mode", label: "Boundary Cap", format: "cap-mode", advanced: true },
      { key: "boundary_cap_de_budget", label: "Appearance Budget", unit: "dE", advanced: true },
      { key: "detail_cap_smoothing_enabled", label: "Detail Smoothing", advanced: true },
      { key: "detail_cap_smoothing_exact_speckle_max_px", label: "Exact Speckle Limit", unit: "px", advanced: true },
      { key: "detail_cap_smoothing_cumulative_component_max_px", label: "Component Limit", unit: "px", advanced: true },
      { key: "detail_cap_smoothing_cumulative_hole_max_px", label: "Hole Limit", unit: "px", advanced: true },
    ],
  },
];

function getFrozenSolveRunSnapshot(run) {
  const runConfig = _cloneValue(run?.config || {});
  const profile = _cloneValue(run?.recipe_snapshot?.profile_snapshot || {});
  const diagnostics = _cloneValue(run?.results?.solve_start_diagnostics || {});
  const profileSettings = profile.settings && typeof profile.settings === "object"
    ? profile.settings
    : {};
  const resolvedSettings = diagnostics.resolved_settings && typeof diagnostics.resolved_settings === "object"
    ? diagnostics.resolved_settings
    : {};
  const settings = {
    ...runConfig,
    ...profileSettings,
    ...resolvedSettings,
  };

  const profileModules = profile.modules && typeof profile.modules === "object"
    ? profile.modules
    : {};
  const profileModulesKnown = Object.keys(profileModules).length > 0;
  const activeDiagnostics = diagnostics.active_modules && typeof diagnostics.active_modules === "object"
    ? diagnostics.active_modules
    : {};
  const diagnosticModuleState = diagnostics.module_state && typeof diagnostics.module_state === "object"
    ? diagnostics.module_state
    : {};
  const diagnosticModuleStateKnown = Object.keys(diagnosticModuleState).length > 0;
  const diagnosticPreprocessing = Array.isArray(activeDiagnostics.preprocessing)
    ? activeDiagnostics.preprocessing.map(String)
    : null;
  const normalizedModules = _normalizeSettingsProfileModules(profileModules, settings);
  const activePreprocessing = diagnosticPreprocessing != null
    ? diagnosticPreprocessing
    : diagnosticModuleStateKnown
      ? Object.keys(diagnosticModuleState).filter((name) => (
        diagnosticModuleState[name] && moduleDescriptorById(name)?.slot === "preprocessing"
      ))
      : profileModulesKnown
        ? Object.keys(normalizedModules).filter((name) => (
          normalizedModules[name] && moduleDescriptorById(name)?.slot === "preprocessing"
        ))
        : [];

  const diagnosticModuleSettings = diagnostics.module_settings && typeof diagnostics.module_settings === "object"
    ? diagnostics.module_settings
    : {};
  const preprocessingParams = _cloneValue(settings.preprocessing_params || {});
  for (const [moduleId, values] of Object.entries(diagnosticModuleSettings)) {
    if (!values || typeof values !== "object") continue;
    preprocessingParams[moduleId] = {
      ...(preprocessingParams[moduleId] || {}),
      ...values,
    };
  }
  settings.preprocessing_params = preprocessingParams;

  return {
    settings,
    activePreprocessing: new Set(activePreprocessing),
    preprocessingStateKnown: diagnosticPreprocessing != null || diagnosticModuleStateKnown || profileModulesKnown,
    hasDiagnostics: Object.keys(diagnostics).length > 0,
  };
}

function formatReadOnlyRunSetting(row, value, settings) {
  if (value == null || value === "") return "Not recorded";
  if (typeof value === "boolean") return value ? "Enabled" : "Disabled";
  if (row.format === "solve-mode") {
    return normalizeLuminanceMode(value) === "luminance_detail" ? "Luminance" : "Color";
  }
  if (row.format === "filament") {
    const fil = filamentById(value);
    return railHoverFilamentLabel(fil, String(value));
  }
  if (row.format === "appearance-model") {
    if (value === "photo_stack_bundle") return "Color Model v2";
    if (value === "legacy_lut") return "Color Model v1";
  }
  if (row.format === "title") {
    return String(value).replace(/_/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
  }
  if (row.format === "gamut-mode") {
    return value === "hue_preserving" ? "Preserve hue" : "Nearest reachable color";
  }
  if (row.format === "region-method") {
    const label = formatRegionMethod(value);
    return label.charAt(0).toUpperCase() + label.slice(1);
  }
  if (row.format === "region-scale") return formatRegionPlanningScale(value);
  if (row.format === "cap-mode") {
    return value === "appearance_bounded_smooth" ? "Detail Aware" : "Smooth";
  }
  if (row.format === "percent") {
    const numeric = Number(value);
    return Number.isFinite(numeric) ? `${Math.round(numeric * 100)}%` : String(value);
  }
  if (row.format === "cap-layers") {
    const thickness = Number(value);
    const layerHeight = Number(settings.layer_height);
    return Number.isFinite(thickness) && Number.isFinite(layerHeight) && layerHeight > 0
      ? `${Math.max(1, Math.round(thickness / layerHeight))} layers`
      : "Not recorded";
  }
  if (row.format === "smooth-radius") {
    const pitch = settings.solver_fine_pitch_mm ?? settings.image_sample_pitch_mm;
    const numeric = smoothingRadiusMmFromCells(value, pitch);
    return `${formatSolveSettingValue("smooth_radius_mm", numeric)} mm`;
  }
  const formatted = formatSolveSettingValue(row.key, value);
  return row.unit && formatted !== "—" ? `${formatted} ${row.unit}` : formatted;
}

function buildReadOnlyRunSectionRows(section, frozen) {
  const rows = section.rows.map((row) => {
    let value = frozen.settings[row.key];
    if ((value == null || value === "") && row.fallbackKey) value = frozen.settings[row.fallbackKey];
    return {
      label: row.label,
      value: formatReadOnlyRunSetting(row, value, frozen.settings),
      advanced: !!row.advanced,
    };
  });

  if (section.key !== "preprocessing") return rows;
  const preprocessingModules = (moduleData || []).filter((entry) => entry.slot === "preprocessing");
  for (const mod of preprocessingModules) {
    const enabled = frozen.activePreprocessing.has(mod.name);
    rows.push({
      label: moduleDisplayName(mod),
      value: frozen.preprocessingStateKnown ? (enabled ? "On" : "Off") : "Not recorded",
      advanced: false,
    });
    if (!frozen.preprocessingStateKnown || !enabled) continue;
    const projected = projectModuleConfigValues(mod.name, mod, frozen.settings);
    for (const param of getSortedModuleParams(mod)) {
      if (!isModuleParamVisibleInSummary(param, projected)) continue;
      rows.push({
        label: `${moduleDisplayName(mod)} · ${param.label || humanizeModuleName(param.name)}`,
        value: String(formatSolveSummaryValue(param, projected[param.name] ?? param.default)),
        advanced: true,
      });
    }
  }
  const knownModuleIds = new Set(preprocessingModules.map((mod) => mod.name));
  for (const moduleId of frozen.activePreprocessing) {
    if (knownModuleIds.has(moduleId)) continue;
    const label = humanizeModuleName(moduleId).replace(/\b\w/g, (letter) => letter.toUpperCase());
    rows.push({ label, value: "On", advanced: false });
    const values = frozen.settings.preprocessing_params?.[moduleId];
    if (!values || typeof values !== "object") continue;
    for (const [key, value] of Object.entries(values)) {
      rows.push({
        label: `${label} · ${getSolveSettingLabel(key)}`,
        value: formatSolveSettingValue(key, value),
        advanced: true,
      });
    }
  }
  return rows;
}

function buildReadOnlyRunSettingsHtml(run) {
  const frozen = getFrozenSolveRunSnapshot(run);
  const sections = READ_ONLY_RUN_SETTING_SECTIONS.map((section) => {
    const rows = buildReadOnlyRunSectionRows(section, frozen)
      .map((row) => `<div class="run-settings-row${row.advanced ? " is-advanced" : ""}">
        <span class="run-settings-label">${esc(row.label)}</span>
        <span class="run-settings-value">${esc(row.value)}</span>
      </div>`)
      .join("");
    return `<section class="run-settings-section" data-run-settings-section="${esc(section.key)}">
      <h4 class="settings-group-cap run-settings-section-cap">${esc(section.title)}</h4>
      <div class="run-settings-rows">${rows}</div>
    </section>`;
  }).join("");
  const archiveNote = frozen.hasDiagnostics
    ? "Values captured when this solve started."
    : "Older saved run: unavailable values are marked as not recorded.";
  return `<div class="run-settings-note">${esc(archiveNote)}</div>${sections}`;
}

function clearSolveRunHoverTimer() {
  if (solveRunHoverTimer) clearTimeout(solveRunHoverTimer);
  solveRunHoverTimer = null;
  solveRunHoverPendingRunId = null;
}

function clearSolveRunHoverCloseTimer() {
  if (solveRunHoverCloseTimer) clearTimeout(solveRunHoverCloseTimer);
  solveRunHoverCloseTimer = null;
}

function isSolveRunHoverBlockedTarget(target) {
  return Boolean(target?.closest?.(".solve-run-card-actions, .solve-run-settings-btn, button, a, input, select"));
}

function positionSolveRunHoverPreview(panel, anchorEl) {
  const rect = anchorEl.getBoundingClientRect();
  const panelRect = panel.getBoundingClientRect();
  const gap = 10;
  const pad = 10;
  const isPreviewSidebar = anchorEl.closest("#solveRunCards");
  let left = isPreviewSidebar ? rect.left - panelRect.width - gap : rect.right + gap;
  if (left < pad || left + panelRect.width > window.innerWidth - pad) {
    left = isPreviewSidebar ? rect.right + gap : rect.left - panelRect.width - gap;
  }
  left = Math.max(pad, Math.min(left, window.innerWidth - panelRect.width - pad));
  const top = Math.max(pad, Math.min(rect.top, window.innerHeight - panelRect.height - pad));
  panel.style.left = `${Math.round(left)}px`;
  panel.style.top = `${Math.round(top)}px`;
}

function showSolveRunHoverPreview(runId, anchorEl) {
  clearSolveRunHoverTimer();
  clearSolveRunHoverCloseTimer();
  const run = solveRuns.find((entry) => entry.id === runId);
  if (!run || !anchorEl || !document.body.contains(anchorEl)) return;
  hideSolveRunHoverPreview();
  solveRunHoverPreviewEl = document.createElement("div");
  solveRunHoverPreviewEl.className = "solve-run-hover-preview";
  solveRunHoverPreviewEl.setAttribute("role", "tooltip");
  solveRunHoverPreviewEl.innerHTML = buildSolveRunEssentialsSummary(run);
  solveRunHoverPreviewEl.addEventListener("mouseenter", () => clearSolveRunHoverCloseTimer());
  solveRunHoverPreviewEl.addEventListener("mouseleave", () => scheduleHideSolveRunHoverPreview(100));
  document.body.appendChild(solveRunHoverPreviewEl);
  solveRunHoverRunId = runId;
  positionSolveRunHoverPreview(solveRunHoverPreviewEl, anchorEl);
  requestAnimationFrame(() => solveRunHoverPreviewEl?.classList.add("is-visible"));
}

function scheduleSolveRunHoverPreview(runId, anchorEl, delayMs = 380) {
  if (solveRunHoverRunId === runId && solveRunHoverPreviewEl?.classList.contains("is-visible")) return;
  if (solveRunHoverPendingRunId === runId) return;
  clearSolveRunHoverTimer();
  clearSolveRunHoverCloseTimer();
  solveRunHoverPendingRunId = runId;
  solveRunHoverTimer = setTimeout(() => {
    solveRunHoverPendingRunId = null;
    showSolveRunHoverPreview(runId, anchorEl);
  }, delayMs);
}

function scheduleHideSolveRunHoverPreview(delayMs = 160) {
  clearSolveRunHoverTimer();
  clearSolveRunHoverCloseTimer();
  solveRunHoverCloseTimer = setTimeout(hideSolveRunHoverPreview, delayMs);
}

function hideSolveRunHoverPreview() {
  clearSolveRunHoverTimer();
  clearSolveRunHoverCloseTimer();
  solveRunHoverPreviewEl?.remove();
  solveRunHoverPreviewEl = null;
  solveRunHoverRunId = null;
}

function positionSolveRunSettingsPanel(panel, anchorEl, context) {
  const sidebar = anchorEl.closest(".solve-deck-sidebar") || anchorEl;
  const rect = sidebar.getBoundingClientRect();
  const panelRect = panel.getBoundingClientRect();
  const gap = 10;
  const pad = 10;
  let left = context === "preview" ? rect.left - panelRect.width - gap : rect.right + gap;
  const fitsPreferred = left >= pad && left + panelRect.width <= window.innerWidth - pad;
  if (!fitsPreferred) {
    const alternate = context === "preview" ? rect.right + gap : rect.left - panelRect.width - gap;
    if (alternate >= pad && alternate + panelRect.width <= window.innerWidth - pad) left = alternate;
    else left = Math.max(pad, Math.min((window.innerWidth - panelRect.width) / 2, window.innerWidth - panelRect.width - pad));
  }
  const top = Math.max(pad, Math.min(rect.top, window.innerHeight - panelRect.height - pad));
  panel.style.left = `${Math.round(left)}px`;
  panel.style.top = `${Math.round(top)}px`;
}

function renderSolveRunSettingsPanel() {
  if (!solveRunSettingsPanelEl || !solveRunSettingsPanelRunId) return;
  const run = solveRuns.find((entry) => entry.id === solveRunSettingsPanelRunId);
  if (!run) {
    hideSolveRunSettingsPanel();
    return;
  }
  solveRunSettingsPanelEl.classList.toggle("show-advanced-settings", solveRunSettingsAdvancedVisible);
  const toggle = solveRunSettingsPanelEl.querySelector(".run-settings-advanced-toggle");
  if (toggle) {
    toggle.textContent = `Advanced: ${solveRunSettingsAdvancedVisible ? "On" : "Off"}`;
    toggle.setAttribute("aria-pressed", solveRunSettingsAdvancedVisible ? "true" : "false");
  }
  const body = solveRunSettingsPanelEl.querySelector(".run-settings-body");
  if (body) body.innerHTML = buildReadOnlyRunSettingsHtml(run);
}

function showSolveRunSettingsPanel(runId, context, anchorEl) {
  const run = solveRuns.find((entry) => entry.id === runId);
  if (!run || !anchorEl) return;
  hideSolveRunHoverPreview();
  hideSolveRunSettingsPanel();
  solveRunSettingsPanelRunId = runId;
  solveRunSettingsPanelContext = context;
  solveRunSettingsPanelEl = document.createElement("aside");
  solveRunSettingsPanelEl.className = "run-settings-panel surface-window";
  solveRunSettingsPanelEl.setAttribute("role", "dialog");
  solveRunSettingsPanelEl.setAttribute("aria-label", `Settings used by ${run.label}`);
  solveRunSettingsPanelEl.innerHTML = `
    <div class="surface-header run-settings-header">
      <div class="window-header__title-slot run-settings-title-slot">
        <h3 class="surface-title">Run Settings</h3>
        <span class="run-settings-run-label">${esc(run.label)}</span>
      </div>
      <div class="window-header__actions surface-header-actions">
        <button class="ghost-button xxs run-settings-load-btn" type="button" title="Apply these captured settings as a temporary Settings Profile">Load Settings</button>
        <button class="view-option-toggle run-settings-advanced-toggle" type="button" aria-pressed="false">Advanced: Off</button>
        <div class="surface-window-controls">
          <button class="close-button window-header__button surface-header-button surface-close run-settings-close" type="button" aria-label="Close run settings" title="Close run settings">${xIconSvg()}</button>
        </div>
      </div>
    </div>
    <div class="surface-body run-settings-body"></div>`;
  document.body.appendChild(solveRunSettingsPanelEl);
  solveRunSettingsPanelEl.querySelector(".run-settings-close")?.addEventListener("click", hideSolveRunSettingsPanel);
  solveRunSettingsPanelEl.querySelector(".run-settings-load-btn")?.addEventListener("click", async () => {
    try {
      const loaded = await _loadTemporarySettingsFromRun(run, {
        kind: "solve-card",
        run_id: run.id,
        label: run.label,
      });
      if (loaded) hideSolveRunSettingsPanel();
    } catch (error) {
      showToast(`Settings could not be loaded: ${error.message}`, "error");
    }
  });
  solveRunSettingsPanelEl.querySelector(".run-settings-advanced-toggle")?.addEventListener("click", () => {
    solveRunSettingsAdvancedVisible = !solveRunSettingsAdvancedVisible;
    renderSolveRunSettingsPanel();
    positionSolveRunSettingsPanel(solveRunSettingsPanelEl, anchorEl, context);
  });
  renderSolveRunSettingsPanel();
  positionSolveRunSettingsPanel(solveRunSettingsPanelEl, anchorEl, context);
}

function hideSolveRunSettingsPanel() {
  solveRunSettingsPanelEl?.remove();
  solveRunSettingsPanelEl = null;
  solveRunSettingsPanelRunId = null;
  solveRunSettingsPanelContext = null;
}

function bindSolveRunCardAuxiliaryInteractions(container, context) {
  container.querySelectorAll(".solve-run-card").forEach((card) => {
    const runId = card.dataset.runId || card.dataset.exportRunId;
    if (!runId) return;
    card.addEventListener("mousemove", (event) => {
      if (isSolveRunHoverBlockedTarget(event.target)) {
        hideSolveRunHoverPreview();
        return;
      }
      scheduleSolveRunHoverPreview(runId, card);
    });
    card.addEventListener("mouseleave", () => scheduleHideSolveRunHoverPreview());
    card.addEventListener("focusin", (event) => {
      if (isSolveRunHoverBlockedTarget(event.target)) return;
      scheduleSolveRunHoverPreview(runId, card, 120);
    });
    card.addEventListener("focusout", (event) => {
      if (!card.contains(event.relatedTarget)) scheduleHideSolveRunHoverPreview(100);
    });
  });
  container.querySelectorAll(".solve-run-settings-btn").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      if (solveRunSettingsPanelRunId === button.dataset.runId && solveRunSettingsPanelEl) {
        hideSolveRunSettingsPanel();
        return;
      }
      showSolveRunSettingsPanel(button.dataset.runId, context, button.closest(".solve-run-card"));
    });
  });
  container.onscroll = () => hideSolveRunHoverPreview();
}

function renderSolveInspectorPanel(selectedRuns, view) {
  const card = $("#solveInspectorCard");
  const grid = $("#solveInspectorGrid");
  if (!card || !grid) return;
  if (!selectedRuns.length) {
    card.style.display = "none";
    grid.innerHTML = "";
    return;
  }
  const blocks = [];
  if (isSolveThicknessDiffView(view) && selectedRuns.length === 2) {
    const [beforeRun, afterRun] = selectedRuns;
    const settingDiffs = collectSolveRunSettingDiffs(beforeRun, afterRun);
    if (settingDiffs.length) {
      blocks.push(...buildGroupedSolveSettingDiffBlocks(beforeRun, afterRun, settingDiffs));
    }
    if (isSolveCapDiffView(view)) {
      const beforeCap = capThicknessCache[beforeRun.id];
      const afterCap = capThicknessCache[afterRun.id];
      if (beforeCap && afterCap) {
        const diff = computeSolveCapDiff(beforeCap, afterCap);
        if (diff) {
          blocks.push(buildSolveInspectorBlock("Cap Diff Summary", [
            { label: "Changed pixels", value: `${diff.changedPx.toLocaleString()} px` },
            { label: "Added pixels", value: `${diff.addedPx.toLocaleString()} px` },
            { label: "Removed pixels", value: `${diff.removedPx.toLocaleString()} px` },
            { label: "Max abs delta", value: formatSolveDiffMm(diff.maxAbsDelta) },
            { label: "Mean abs delta", value: formatSolveDiffMm(diff.meanAbsDelta) },
          ], `${beforeRun.label} -> ${afterRun.label}`));
        }
      }
    } else {
      const filamentId = ensureSolveFilamentDiffSelection(selectedRuns);
      const fil = filamentById(filamentId);
      const beforeFil = filamentThicknessCache[beforeRun.id]?.[filamentId];
      const afterFil = filamentThicknessCache[afterRun.id]?.[filamentId];
      if (beforeFil && afterFil) {
        const diff = computeSolveCapDiff(beforeFil, afterFil);
        if (diff) {
          blocks.push(buildSolveInspectorBlock(`${fil?.color_name || filamentId} Diff Summary`, [
            { label: "Changed pixels", value: `${diff.changedPx.toLocaleString()} px` },
            { label: "Added pixels", value: `${diff.addedPx.toLocaleString()} px` },
            { label: "Removed pixels", value: `${diff.removedPx.toLocaleString()} px` },
            { label: "Before active", value: `${diff.beforeActivePx.toLocaleString()} px` },
            { label: "After active", value: `${diff.afterActivePx.toLocaleString()} px` },
            { label: "Max abs delta", value: formatSolveDiffMm(diff.maxAbsDelta) },
            { label: "Mean abs delta", value: formatSolveDiffMm(diff.meanAbsDelta) },
          ], `${beforeRun.label} -> ${afterRun.label}`));
        }
      }
    }
  }
  if (!blocks.length) {
    card.style.display = "none";
    grid.innerHTML = "";
    return;
  }
  grid.innerHTML = blocks.join("");
  card.style.display = "";
}

// ── Diff column markup ────────────────────────────────────────────────
function buildSolveCapDiffColumn(beforeRun, afterRun, aspect) {
  return `
    <div class="solve-grid-column is-diff" data-solve-card-kind="diff">
      <div class="solve-grid-column-header">
        <h4>Cap Diff</h4>
        <div class="comparison-column-chips" aria-hidden="true" style="visibility:hidden"><span class="comparison-chip"></span></div>
        <div class="comparison-column-stats" id="solveCapDiffStats">Loading diff...</div>
      </div>
      <div class="solve-grid-img-wrapper solve-grid-capdiff-wrap" style="--img-aspect:${aspect}">
        <canvas class="solve-grid-capdiff-canvas" id="solveCapDiffCanvas" aria-label="Cap difference"></canvas>
      </div>
      ${buildSolveCardScaleBarSlot()}
      <div class="solve-diff-summary" id="solveCapDiffSummary"></div>
    </div>`;
}

function buildSolveFilamentRunColumn(run, aspect, filamentId) {
  const mapInfo = getRunFilamentMapInfo(run, filamentId);
  const fil = filamentById(filamentId);
  const label = fil?.color_name || filamentId;
  const statsLine = `${(mapInfo?.active_px || 0).toLocaleString()} px · max ${(mapInfo?.max_d || 0).toFixed(2)} mm`;
  const visual = mapInfo?.map_url
    ? `<img class="solve-grid-img solve-grid-filament-thickness-img" src="${esc(mapInfo.map_url)}" alt="${esc(label)}">`
    : `<div class="solve-grid-empty-map">No thickness</div>`;
  const thicknessData = mapInfo?.map_url
    ? ` data-thickness-url="${esc(mapInfo.map_url)}" data-thickness-label="${esc(`${run.label} · ${label}`)}"`
    : "";
  return `
    <div class="solve-grid-column" data-solve-card-kind="thickness" data-run-id="${esc(run.id)}"${thicknessData}>
      <div class="solve-grid-column-header">
        <h4>${esc(run.label)}</h4>
        <div class="comparison-column-chips"><span class="comparison-chip" style="background:${esc(fil?.hex || "#888")}"></span></div>
        <div class="comparison-column-stats">${esc(label)} · ${statsLine}</div>
      </div>
      <div class="solve-grid-img-wrapper" style="--img-aspect:${aspect}">
        ${visual}
      </div>
      ${buildSolveCardScaleBarSlot()}
    </div>`;
}

function buildSolveFilamentDiffColumn(beforeRun, afterRun, filamentId, aspect) {
  const fil = filamentById(filamentId);
  const label = fil?.color_name || filamentId;
  return `
    <div class="solve-grid-column is-diff" data-solve-card-kind="diff">
      <div class="solve-grid-column-header">
        <h4>${esc(label)} Diff</h4>
        <div class="comparison-column-chips" aria-hidden="true"><span class="comparison-chip" style="background:${esc(fil?.hex || "#888")}"></span></div>
        <div class="comparison-column-stats" id="solveFilamentDiffStats">Loading diff...</div>
      </div>
      <div class="solve-grid-img-wrapper solve-grid-capdiff-wrap" style="--img-aspect:${aspect}">
        <canvas class="solve-grid-capdiff-canvas" id="solveFilamentDiffCanvas" aria-label="${esc(label)} difference"></canvas>
      </div>
      ${buildSolveCardScaleBarSlot()}
      <div class="solve-diff-summary" id="solveFilamentDiffSummary"></div>
    </div>`;
}

// ── Diff render dispatchers ───────────────────────────────────────────
async function renderSolveCapDiffColumn(beforeRun, afterRun) {
  const statsEl = $("#solveCapDiffStats");
  const summaryEl = $("#solveCapDiffSummary");
  const canvas = $("#solveCapDiffCanvas");
  if (!statsEl || !summaryEl || !canvas) return;
  const displayedView = solveView;
  statsEl.textContent = "Loading diff...";
  summaryEl.innerHTML = `<div class="muted-line">Loading exact cap delta...</div>`;
  const [beforeCap, afterCap] = await Promise.all([
    ensureCapThickness(beforeRun),
    ensureCapThickness(afterRun),
  ]);
  // If the user switched views during the async load, don't clobber the new view's
  // shared legend/inspector with stale diff state.
  if (solveView !== displayedView) return;
  if (!beforeCap || !afterCap) {
    statsEl.textContent = "Cap blob unavailable";
    summaryEl.innerHTML = `<div class="muted-line">Couldn't load cap thickness data for this pair. Older runs may need to be re-solved after this patch.</div>`;
    updateSolveLegend();
    renderSolveInspectorPanel(getSelectedRuns().filter(r => r.results), solveView);
    return;
  }
  const diff = computeSolveCapDiff(beforeCap, afterCap);
  if (!diff) {
    statsEl.textContent = "Cap diff unavailable";
    summaryEl.innerHTML = `<div class="muted-line">Cap maps do not share the same dimensions.</div>`;
    updateSolveLegend();
    renderSolveInspectorPanel(getSelectedRuns().filter(r => r.results), solveView);
    return;
  }
  renderSolveCapDiffCanvas(canvas, diff, solveCapDiffMode);
  statsEl.textContent = `${diff.changedPx.toLocaleString()} px changed · max ${formatSolveDiffMm(diff.maxAbsDelta)}`;
  summaryEl.innerHTML = buildSolveCapDiffSummaryHtml(diff);
  updateSolveLegend();
  renderSolveInspectorPanel(getSelectedRuns().filter(r => r.results), solveView);
}

async function renderSolveFilamentDiffColumn(beforeRun, afterRun, filamentId) {
  const statsEl = $("#solveFilamentDiffStats");
  const summaryEl = $("#solveFilamentDiffSummary");
  const canvas = $("#solveFilamentDiffCanvas");
  if (!statsEl || !summaryEl || !canvas) return;
  const fil = filamentById(filamentId);
  const label = fil?.color_name || filamentId || "Selected filament";
  const displayedView = solveView;
  statsEl.textContent = "Loading diff...";
  summaryEl.innerHTML = `<div class="muted-line">Loading exact ${esc(label)} thickness delta...</div>`;
  const [beforeFil, afterFil] = await Promise.all([
    ensureFilamentThickness(beforeRun, filamentId),
    ensureFilamentThickness(afterRun, filamentId),
  ]);
  // If the user switched views during the async load, don't clobber the new view's
  // shared legend/inspector with stale diff state.
  if (solveView !== displayedView) return;
  if (!beforeFil || !afterFil) {
    statsEl.textContent = "Filament blob unavailable";
    summaryEl.innerHTML = `<div class="muted-line">Couldn't load thickness data for ${esc(label)}. Older runs may need to be re-solved after this patch.</div>`;
    updateSolveLegend();
    renderSolveInspectorPanel(getSelectedRuns().filter((r) => r.results), solveView);
    return;
  }
  const diff = computeSolveCapDiff(beforeFil, afterFil);
  if (!diff) {
    statsEl.textContent = "Color diff unavailable";
    summaryEl.innerHTML = `<div class="muted-line">Selected filament maps do not share the same dimensions.</div>`;
    updateSolveLegend();
    renderSolveInspectorPanel(getSelectedRuns().filter((r) => r.results), solveView);
    return;
  }
  renderSolveCapDiffCanvas(canvas, diff, solveCapDiffMode);
  statsEl.textContent = `${diff.changedPx.toLocaleString()} px changed · max ${formatSolveDiffMm(diff.maxAbsDelta)}`;
  summaryEl.innerHTML = buildSolveCapDiffSummaryHtml(diff);
  updateSolveLegend();
  renderSolveInspectorPanel(getSelectedRuns().filter((r) => r.results), solveView);
}

async function ensureSurfaceData(run) {
  const id = run.id;
  if (surfaceDataCache[id]) return surfaceDataCache[id];
  const r = run.results;
  if (!r?.total_surface_bin_url || !r?.color_ceiling_bin_url) return null;
  const [surface, ceiling] = await Promise.all([
    loadSurfaceBlob(r.total_surface_bin_url),
    loadSurfaceBlob(r.color_ceiling_bin_url),
  ]);
  if (!surface || !ceiling) return null;
  surfaceDataCache[id] = { surface, ceiling };
  return surfaceDataCache[id];
}

function rgbFromHex(hex, fallback = [128, 128, 128]) {
  if (typeof hex !== "string" || !/^#[0-9a-f]{6}$/i.test(hex)) return fallback;
  return [
    parseInt(hex.slice(1, 3), 16),
    parseInt(hex.slice(3, 5), 16),
    parseInt(hex.slice(5, 7), 16),
  ];
}

function mixRgb(a, b, amount) {
  const t = Math.max(0, Math.min(1, amount));
  return [
    Math.round(a[0] * (1 - t) + b[0] * t),
    Math.round(a[1] * (1 - t) + b[1] * t),
    Math.round(a[2] * (1 - t) + b[2] * t),
  ];
}

function getRunBaseFilamentId(run) {
  const snapshot = getSolveRunSettingsSnapshot(run);
  return snapshot.base_filament || snapshot.white_base || DEFAULT_BASE_FILAMENT;
}

function getRunCapFilamentId(run) {
  const snapshot = getSolveRunSettingsSnapshot(run);
  const baseId = getRunBaseFilamentId(run);
  const capId = snapshot.cap_filament || snapshot.white_cap || "__same__";
  return !capId || capId === "__same__" ? baseId : capId;
}

async function ensureExplorerMaterialData(run) {
  if (!run?.results) return null;
  if (explorerMaterialDataCache[run.id]) return explorerMaterialDataCache[run.id];

  const r = run.results;
  if (!r.explorer_stack_label_bin_url || !Array.isArray(r.explorer_stack_table)) return null;
  const [surfaceData, stackLabels, capThickness] = await Promise.all([
    ensureSurfaceData(run),
    loadUint32Blob(r.explorer_stack_label_bin_url),
    ensureCapThickness(run),
  ]);
  const [boundaryCapThickness, detailCapThickness] = await Promise.all([
    r.boundary_cap_height_bin_url ? loadSurfaceBlob(r.boundary_cap_height_bin_url) : Promise.resolve(null),
    r.detail_cap_height_bin_url ? loadSurfaceBlob(r.detail_cap_height_bin_url) : Promise.resolve(null),
  ]);
  if (!surfaceData || !stackLabels) return null;
  const { width, height } = surfaceData.surface;
  if (stackLabels.width !== width || stackLabels.height !== height) return null;
  const capData = capThickness?.width === width && capThickness?.height === height
    ? capThickness
    : null;
  const boundaryCapData = boundaryCapThickness?.width === width && boundaryCapThickness?.height === height
    ? boundaryCapThickness
    : null;
  const detailCapData = detailCapThickness?.width === width && detailCapThickness?.height === height
    ? detailCapThickness
    : null;

  const stackTable = r.explorer_stack_table.map((stack) => {
    if (!Array.isArray(stack)) return [];
    return stack.map((entry) => {
      const filamentId = String(entry?.filament_id || entry?.filamentId || "");
      const fil = filamentById(filamentId);
      return {
        filamentId,
        thicknessMm: Math.max(0, Number(entry?.thickness_mm ?? entry?.thicknessMm ?? 0) || 0),
        rgb: rgbFromHex(fil?.hex, [136, 136, 136]),
      };
    }).filter((entry) => entry.filamentId && entry.thicknessMm > 1e-9);
  });

  const baseId = r.explorer_base_filament_id || getRunBaseFilamentId(run);
  const capId = r.explorer_cap_filament_id || getRunCapFilamentId(run);
  const baseFil = filamentById(baseId);
  const capFil = filamentById(capId);
  const snapshot = getSolveRunSettingsSnapshot(run);
  const baseThickness = Number(
    r.explorer_base_thickness_mm
      ?? snapshot.d_wb
      ?? getSolveSurfaceBaseThickness()
  ) || getSolveSurfaceBaseThickness();
  explorerMaterialDataCache[run.id] = {
    surface: surfaceData.surface,
    ceiling: surfaceData.ceiling,
    cap: capData,
    boundaryCap: boundaryCapData,
    detailCap: detailCapData,
    stackLabels,
    stackTable,
    baseThickness,
    baseRgb: rgbFromHex(baseFil?.hex, [245, 240, 224]),
    capRgb: rgbFromHex(capFil?.hex, [245, 240, 224]),
    boundaryCapRgb: mixRgb(rgbFromHex(capFil?.hex, [245, 240, 224]), [128, 128, 128], 0.22),
  };
  return explorerMaterialDataCache[run.id];
}

function isSolveSurfaceView(view) {
  return view === "surface_highpass" || view === "surface_explorer";
}

function getSolveSurfaceBaseThickness() {
  return parseFloat(config.d_wb) || 0.20;
}

function getSolveSurfaceLayerHeight() {
  return parseFloat(config.layer_height) || 0.08;
}

function getSolveSurfaceTMax() {
  return parseFloat(config.t_max) || 3.0;
}

function getSolveSurfaceExtraSteps() {
  const base = getSolveSurfaceBaseThickness();
  const lh = getSolveSurfaceLayerHeight();
  const tMax = getSolveSurfaceTMax();
  return Math.max(0, Math.round((tMax - base) / lh));
}

const DIAGNOSTIC_PALETTE_INFERNO = "inferno-v1";
const DIAGNOSTIC_PALETTE_LEGACY = "legacy-approximate";
const INFERNO_RGB8_HEX = "00000401000501010601010802010a02020c02020e03021004031204031405041706041907051b08051d09061f0a07220b07240c08260d08290e092b10092d110a30120a32140b34150b37160b39180c3c190c3e1b0c411c0c431e0c451f0c48210c4a230c4c240c4f260c51280b53290b552b0b572d0b592f0a5b310a5c320a5e340a5f3609613809623909633b09643d09653e0966400a67420a68440a68450a69470b6a490b6a4a0c6b4c0c6b4d0d6c4f0d6c510e6c520e6d540f6d550f6d57106e59106e5a116e5c126e5d126e5f136e61136e62146e64156e65156e67166e69166e6a176e6c186e6d186e6f196e71196e721a6e741a6e751b6e771c6d781c6d7a1d6d7c1d6d7d1e6d7f1e6c801f6c82206c84206b85216b87216b88226a8a226a8c23698d23698f24699025689225689326679526679727669827669a28659b29649d29649f2a63a02a63a22b62a32c61a52c60a62d60a82e5fa92e5eab2f5ead305dae305cb0315bb1325ab3325ab43359b63458b73557b93556ba3655bc3754bd3853bf3952c03a51c13a50c33b4fc43c4ec63d4dc73e4cc83f4bca404acb4149cc4248ce4347cf4446d04545d24644d34743d44842d54a41d74b3fd84c3ed94d3dda4e3cdb503bdd513ade5238df5337e05536e15635e25734e35933e45a31e55c30e65d2fe75e2ee8602de9612bea632aeb6429eb6628ec6726ed6925ee6a24ef6c23ef6e21f06f20f1711ff1731df2741cf3761bf37819f47918f57b17f57d15f67e14f68013f78212f78410f8850ff8870ef8890cf98b0bf98c0af98e09fa9008fa9207fa9407fb9606fb9706fb9906fb9b06fb9d07fc9f07fca108fca309fca50afca60cfca80dfcaa0ffcac11fcae12fcb014fcb216fcb418fbb61afbb81dfbba1ffbbc21fbbe23fac026fac228fac42afac62df9c72ff9c932f9cb35f8cd37f8cf3af7d13df7d340f6d543f6d746f5d949f5db4cf4dd4ff4df53f4e156f3e35af3e55df2e661f2e865f2ea69f1ec6df1ed71f1ef75f1f179f2f27df2f482f3f586f3f68af4f88ef5f992f6fa96f8fb9af9fc9dfafda1fcffa4";
const INFERNO_RGB8 = Array.from({ length: 256 }, (_, index) => {
  const offset = index * 6;
  return [0, 2, 4].map(channel => parseInt(INFERNO_RGB8_HEX.slice(offset + channel, offset + channel + 2), 16));
});

function legacyScalarPalette(t) {
  t = Math.max(0, Math.min(1, t));
  return [
    Math.floor(Math.max(0, Math.min(255, ( 0.267 + 2.173*t - 1.802*t*t) * 255))),
    Math.floor(Math.max(0, Math.min(255, (-0.004 + 1.874*t - 0.870*t*t) * 255))),
    Math.floor(Math.max(0, Math.min(255, ( 0.329 - 1.120*t + 0.791*t*t) * 255))),
  ];
}

function inferno(t) {
  const position = Math.max(0, Math.min(1, Number(t) || 0)) * 255;
  const lower = Math.floor(position);
  const upper = Math.min(255, lower + 1);
  const fraction = position - lower;
  return INFERNO_RGB8[lower].map((value, channel) => (
    Math.floor(value * (1 - fraction) + INFERNO_RGB8[upper][channel] * fraction + 0.5)
  ));
}

function getRunDiagnosticPaletteVersion(run) {
  return run?.results?.diagnostic_palette_version === DIAGNOSTIC_PALETTE_INFERNO
    ? DIAGNOSTIC_PALETTE_INFERNO
    : DIAGNOSTIC_PALETTE_LEGACY;
}

function sampleScalarPalette(t, paletteVersion = DIAGNOSTIC_PALETTE_INFERNO) {
  return paletteVersion === DIAGNOSTIC_PALETTE_INFERNO ? inferno(t) : legacyScalarPalette(t);
}

const SURFACE_CONTOUR_SCALE = 3;
const SOLVE_CONTOUR_STROKE = "rgba(0, 0, 0, 0.72)";

function strokeLayerContourPaths(ctx, data, width, height, layerHeight, scale = SURFACE_CONTOUR_SCALE) {
  if (!ctx || !data || width <= 0 || height <= 0 || !(layerHeight > 0)) return;
  const active = new Uint8Array(data.length);
  const levels = new Int32Array(data.length);
  for (let i = 0; i < data.length; i++) {
    const value = data[i];
    if (value > 1e-9) {
      active[i] = 1;
      levels[i] = Math.floor((value + 1e-6) / layerHeight);
    }
  }

  ctx.beginPath();
  for (let y = 0; y < height; y++) {
    const row = y * width;
    for (let x = 0; x < width - 1; x++) {
      const i = row + x;
      const j = i + 1;
      if (active[i] && active[j] && levels[i] !== levels[j]) {
        const px = (x + 1) * scale + 0.5;
        ctx.moveTo(px, y * scale);
        ctx.lineTo(px, (y + 1) * scale);
      }
    }
  }
  for (let y = 0; y < height - 1; y++) {
    const row = y * width;
    const nextRow = row + width;
    for (let x = 0; x < width; x++) {
      const i = row + x;
      const j = nextRow + x;
      if (active[i] && active[j] && levels[i] !== levels[j]) {
        const py = (y + 1) * scale + 0.5;
        ctx.moveTo(x * scale, py);
        ctx.lineTo((x + 1) * scale, py);
      }
    }
  }
  ctx.strokeStyle = SOLVE_CONTOUR_STROKE;
  ctx.lineWidth = 1;
  ctx.stroke();
}

function strokeDiscreteLabelBoundaries(ctx, boundaries, scale = SURFACE_CONTOUR_SCALE) {
  if (!ctx || !boundaries) return;
  const { width, height, vertical, horizontal } = boundaries;
  if (width <= 0 || height <= 0 || !vertical || !horizontal) return;
  ctx.beginPath();
  for (let y = 0; y < height; y++) {
    const boundaryRow = y * Math.max(0, width - 1);
    for (let x = 0; x < width - 1; x++) {
      if (!vertical[boundaryRow + x]) continue;
      const px = (x + 1) * scale + 0.5;
      ctx.moveTo(px, y * scale);
      ctx.lineTo(px, (y + 1) * scale);
    }
  }
  for (let y = 0; y < height - 1; y++) {
    const row = y * width;
    for (let x = 0; x < width; x++) {
      if (!horizontal[row + x]) continue;
      const py = (y + 1) * scale + 0.5;
      ctx.moveTo(x * scale, py);
      ctx.lineTo((x + 1) * scale, py);
    }
  }
  ctx.strokeStyle = SOLVE_CONTOUR_STROKE;
  ctx.lineWidth = 1;
  ctx.stroke();
}

/**
 * Render total surface data onto a canvas with highpass threshold.
 * Pixels below threshold are black. The scalar palette scale is [0, tMax].
 */
function renderHighpass(canvas, surfaceData, tMax, threshold, paletteVersion = DIAGNOSTIC_PALETTE_INFERNO) {
  const { width, height, data } = surfaceData;
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext("2d");
  const img = ctx.createImageData(width, height);
  const px = img.data;
  for (let i = 0; i < data.length; i++) {
    const v = data[i];
    const off = i * 4;
    if (v >= threshold) {
      const [r, g, b] = sampleScalarPalette(v / tMax, paletteVersion);
      px[off] = r; px[off+1] = g; px[off+2] = b; px[off+3] = 255;
    } else {
      px[off] = 0; px[off+1] = 0; px[off+2] = 0; px[off+3] = 255;
    }
  }
  ctx.putImageData(img, 0, 0);
}

let _solveHighpassControlsBound = false;
let _solveExplorerControlsBound = false;

function initHighpassControls() {
  const slider = $("#highpassThresholdSlider");
  const valueEl = $("#highpassThresholdValue");
  const hintEl = $("#highpassLayerCount");
  if (!slider) return;

  function updateSliderRange() {
    const steps = getSolveSurfaceExtraSteps();
    slider.min = 0;
    slider.max = steps;
    slider.value = steps;  // start at top
    slider.step = 1;
  }

  function getThreshold() {
    return getSolveSurfaceBaseThickness() + (parseInt(slider.value) * getSolveSurfaceLayerHeight());
  }

  function updateDisplay() {
    const th = getThreshold();
    const layers = parseInt(slider.value);
    valueEl.textContent = `${th.toFixed(2)} mm`;
    hintEl.textContent = `(${layers} layers)`;
  }

  updateSliderRange();
  updateDisplay();

  if (_solveHighpassControlsBound) return;
  _solveHighpassControlsBound = true;

  slider.addEventListener("input", () => {
    updateDisplay();
    renderSolveSurfaceCanvases();
    updateSolveLegend();
    updateSolveViewCaption();
  });

  slider.addEventListener("wheel", (e) => {
    e.preventDefault();
    const dir = e.deltaY > 0 ? -1 : 1;
    slider.value = Math.max(
      parseInt(slider.min),
      Math.min(parseInt(slider.max), parseInt(slider.value) + dir)
    );
    updateDisplay();
    renderSolveSurfaceCanvases();
    updateSolveLegend();
    updateSolveViewCaption();
  }, { passive: false });
}

/** Uniform fill for "this is color stack, not cap" regions.
 *  Mid-gray — clearly not on the scalar diagnostic scale. */
const COLOR_FLOOR_FILL = [128, 128, 128];

/**
 * Render total surface with band window and color-floor distinction.
 *
 * - Out of band: black
 * - In band AND above color ceiling: versioned scalar palette (fixed [0, tMax])
 * - In band AND at or below color ceiling: uniform gray
 */
function renderExplorer(canvas, surfaceData, ceilingData, tMax, center, halfBand, paletteVersion = DIAGNOSTIC_PALETTE_INFERNO) {
  const { width, height, data: surface } = surfaceData;
  const ceiling = ceilingData.data;
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext("2d");
  const img = ctx.createImageData(width, height);
  const px = img.data;
  const lo = center - halfBand;
  const hi = center + halfBand;

  for (let i = 0; i < surface.length; i++) {
    const v = surface[i];
    const off = i * 4;
    if (v >= lo && v <= hi) {
      if (center <= ceiling[i]) {
        px[off] = COLOR_FLOOR_FILL[0];
        px[off+1] = COLOR_FLOOR_FILL[1];
        px[off+2] = COLOR_FLOOR_FILL[2];
      } else {
        const [r, g, b] = sampleScalarPalette(v / tMax, paletteVersion);
        px[off] = r; px[off+1] = g; px[off+2] = b;
      }
      px[off+3] = 255;
    } else {
      px[off] = 0; px[off+1] = 0; px[off+2] = 0; px[off+3] = 255;
    }
  }
  ctx.putImageData(img, 0, 0);
}

function renderExplorerRich(canvas, materialData, center, halfBand) {
  const {
    surface,
    ceiling,
    cap,
    boundaryCap,
    detailCap,
    stackLabels,
    stackTable,
    baseThickness,
    baseRgb,
    capRgb,
    boundaryCapRgb,
  } = materialData;
  const { width, height, data: surfaceValues } = surface;
  const ceilingValues = ceiling.data;
  const capValues = cap?.data || null;
  const boundaryCapValues = boundaryCap?.data || null;
  const detailCapValues = detailCap?.data || null;
  const stackLabelValues = stackLabels.data;
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext("2d");
  const img = ctx.createImageData(width, height);
  const px = img.data;
  const sampleHeight = Math.max(0, center - (getSolveSurfaceLayerHeight() / 2));
  const eps = 1e-6;

  for (let i = 0; i < surfaceValues.length; i++) {
    const top = surfaceValues[i];
    const off = i * 4;
    if (sampleHeight > top + eps) {
      px[off] = 0; px[off+1] = 0; px[off+2] = 0; px[off+3] = 255;
      continue;
    }

    let rgb = baseRgb;
    const colorCeiling = ceilingValues[i];
    const capThickness = capValues ? Math.max(0, capValues[i] || 0) : 0;
    const capFloor = capValues
      ? Math.max(colorCeiling, top - capThickness)
      : colorCeiling;
    if (sampleHeight <= baseThickness + eps) {
      rgb = baseRgb;
    } else if (sampleHeight > capFloor + eps) {
      // Always distinguish boundary cap vs detail cap (two-tone) when the data is present.
      if (boundaryCapValues || detailCapValues) {
        const boundaryThickness = boundaryCapValues ? Math.max(0, boundaryCapValues[i] || 0) : 0;
        const detailThickness = detailCapValues ? Math.max(0, detailCapValues[i] || 0) : 0;
        const detailFloor = capFloor + boundaryThickness;
        rgb = detailThickness > eps && sampleHeight > detailFloor + eps
          ? capRgb
          : (boundaryCapRgb || capRgb);
      } else {
        rgb = capRgb;
      }
    } else {
      const localHeight = Math.max(0, sampleHeight - baseThickness);
      let cumulative = 0;
      let fallbackRgb = null;
      const stackId = stackLabelValues[i];
      const materials = stackTable[stackId] || [];
      for (const material of materials) {
        const d = material.thicknessMm;
        if (d <= eps) continue;
        fallbackRgb = material.rgb;
        if (localHeight <= cumulative + d + eps) {
          fallbackRgb = material.rgb;
          break;
        }
        cumulative += d;
      }
      rgb = fallbackRgb || COLOR_FLOOR_FILL;
    }

    px[off] = rgb[0];
    px[off+1] = rgb[1];
    px[off+2] = rgb[2];
    px[off+3] = 255;
  }
  ctx.putImageData(img, 0, 0);
}

function initExplorerControls() {
  const heightSlider = $("#explorerHeightSlider");
  const bandSlider = $("#explorerBandSlider");
  const heightValueEl = $("#explorerHeightValue");
  const heightHintEl = $("#explorerHeightLayers");
  const bandValueEl = $("#explorerBandValue");
  const bandHintEl = $("#explorerBandLayers");
  if (!heightSlider || !bandSlider) return;

  function updateSliderRanges() {
    const steps = getSolveSurfaceExtraSteps();
    const prevHeight = Number.parseInt(heightSlider.value, 10);
    const prevBand = Number.parseInt(bandSlider.value, 10);
    const nextHeight = Number.isFinite(prevHeight) ? prevHeight : steps;
    const nextBand = Number.isFinite(prevBand) ? prevBand : Math.min(3, Math.floor(steps / 2));
    heightSlider.min = 0;
    heightSlider.max = steps;
    heightSlider.value = Math.max(0, Math.min(steps, nextHeight));
    heightSlider.step = 1;
    bandSlider.min = 1;
    bandSlider.max = Math.max(1, Math.floor(steps / 2));
    bandSlider.value = Math.max(1, Math.min(parseInt(bandSlider.max), nextBand));
    bandSlider.step = 1;
  }

  function getCenter() {
    return getSolveSurfaceBaseThickness() + (parseInt(heightSlider.value) * getSolveSurfaceLayerHeight());
  }

  function getHalfBand() {
    return parseInt(bandSlider.value) * getSolveSurfaceLayerHeight();
  }

  function updateDisplay() {
    const center = getCenter();
    const halfBand = getHalfBand();
    heightValueEl.textContent = `${center.toFixed(2)} mm`;
    heightHintEl.textContent = `(layer ${parseInt(heightSlider.value)})`;
    bandValueEl.textContent = `± ${halfBand.toFixed(2)} mm`;
    bandHintEl.textContent = `(${parseInt(bandSlider.value)} layers)`;
  }

  updateSliderRanges();
  updateDisplay();

  if (_solveExplorerControlsBound) return;
  _solveExplorerControlsBound = true;

  heightSlider.addEventListener("input", () => {
    updateDisplay();
    renderSolveSurfaceCanvases();
    updateSolveLegend();
    updateSolveViewCaption();
  });
  bandSlider.addEventListener("input", () => {
    updateDisplay();
    renderSolveSurfaceCanvases();
    updateSolveLegend();
    updateSolveViewCaption();
  });

  heightSlider.addEventListener("wheel", (e) => {
    e.preventDefault();
    const dir = e.deltaY > 0 ? -1 : 1;
    heightSlider.value = Math.max(
      parseInt(heightSlider.min),
      Math.min(parseInt(heightSlider.max), parseInt(heightSlider.value) + dir)
    );
    updateDisplay();
    renderSolveSurfaceCanvases();
    updateSolveLegend();
    updateSolveViewCaption();
  }, { passive: false });

  bandSlider.addEventListener("wheel", (e) => {
    e.preventDefault();
    const dir = e.deltaY > 0 ? -1 : 1;
    bandSlider.value = Math.max(
      parseInt(bandSlider.min),
      Math.min(parseInt(bandSlider.max), parseInt(bandSlider.value) + dir)
    );
    updateDisplay();
    renderSolveSurfaceCanvases();
    updateSolveLegend();
    updateSolveViewCaption();
  }, { passive: false });
}

// ── End surface diagnostic utilities ────────────────────────────────────────

function renderSolveComparisonGrid() {
  const grid = $("#solveComparisonGrid");
  const emptyMsg = $("#solveGridEmpty");
  const viewBar = $("#solveViewBar");
  const subControls = $("#solveSubControls");
  const thickCard = $("#solveThicknessCard");
  const legendRow = $("#solveLegend");
  const legendContent = $("#solveLegendContent");
  const viewCaption = $("#solveViewCaption");
  const mapsGrid = $("#filamentMapsGrid");
  if (!grid) return;

  // Removed views (Palette Fit, old Diagnostic Views) and the parked diff views must never render
  // for normal users; coerce stale/console-set state to predicted before anything reads solveView.
  coerceSolveViewForAccess();
  const devViews = isSolveDevViewsEnabled();
  const parkedGroup = $("#solveParkedViews");
  const parkedLabel = $("#solveParkedLabel");
  if (parkedGroup) parkedGroup.hidden = !devViews;
  if (parkedLabel) parkedLabel.hidden = !devViews;

  const selected = getSelectedRuns().filter(r => r.results);
  if (selected.length === 0) {
    grid.style.display = "";
    grid.innerHTML = "";
    if (emptyMsg) {
      grid.appendChild(emptyMsg);
      emptyMsg.classList.remove("is-hidden");
    }
    if (thickCard) thickCard.style.display = "none";
    if (mapsGrid) mapsGrid.innerHTML = "";
    if (legendRow) legendRow.classList.add("is-hidden");
    if (legendContent) legendContent.innerHTML = "";
    if (viewCaption) viewCaption.classList.add("is-hidden");
    if (viewBar) viewBar.style.display = "none";
    if (subControls) subControls.style.display = "none";
    renderSolveInspectorPanel([], solveView);
    return;
  }
  if (emptyMsg) emptyMsg.classList.add("is-hidden");
  if (viewBar) viewBar.style.display = "";
  if (subControls) subControls.style.display = "";

  const view = solveView;
  syncSolveViewToggleActive();

  if (view === "thickness_maps") {
    grid.style.display = "none";
    if (thickCard) thickCard.style.display = "";
    // Keep the shared sub-controls host visible so the contextual caption remains aligned
    // with the other solve views. Thickness Maps itself has no selectable sub-view state.
    updateSolveSubControls();
    updateSolveLegend(view);
    renderSolveInspectorPanel(selected, view);
    renderSolveThicknessMaps(selected);
    return;
  }
  grid.style.display = "";
  if (thickCard) thickCard.style.display = "none";

  if (view === "cap_diff" || view === "filament_diff") {
    syncSolveFilamentDiffControl(selected);
    updateSolveSubControls();
    if (selected.length !== 2) {
      grid.innerHTML = `<p class="muted-line">Select exactly two completed solve runs to view ${view === "cap_diff" ? "Cap Diff" : "Color Diff"}.</p>`;
      renderSolveInspectorPanel(selected, view);
      updateSolveLegend();
      return;
    }
    const [beforeRun, afterRun] = selected;
    const aspect = _runAspect(afterRun);
    let html = shouldShowSolveSourceColumn(view) ? buildSolveSourceColumn(beforeRun, _runAspect(beforeRun), view) : "";
    if (view === "cap_diff") {
      const capDiffOpts = (run) => ({ cardKind: "thickness", lightboxUrl: run.results.total_surface_url, lightboxLabel: `${run.label} · Top Surface` });
      html += buildSolveRunVisualColumn(beforeRun, _runAspect(beforeRun), "total_surface", capDiffOpts(beforeRun));
      html += buildSolveRunVisualColumn(afterRun, aspect, "total_surface", capDiffOpts(afterRun));
      html += buildSolveCapDiffColumn(beforeRun, afterRun, aspect);
    } else {
      const filamentId = ensureSolveFilamentDiffSelection(selected);
      if (!filamentId) {
        grid.innerHTML = `<p class="muted-line">No color filament thickness maps are available for these selected runs.</p>`;
        renderSolveInspectorPanel(selected, view);
        updateSolveLegend();
        return;
      }
      html += buildSolveFilamentRunColumn(beforeRun, _runAspect(beforeRun), filamentId);
      html += buildSolveFilamentRunColumn(afterRun, aspect, filamentId);
      html += buildSolveFilamentDiffColumn(beforeRun, afterRun, filamentId, aspect);
    }
    grid.innerHTML = html;

    updateSolveColumnImages();
    updateSolveLegend();
    if (view === "cap_diff") renderSolveCapDiffColumn(beforeRun, afterRun);
    else renderSolveFilamentDiffColumn(beforeRun, afterRun, ensureSolveFilamentDiffSelection(selected));

    // Clicks are handled by the delegated #solveComparisonGrid listener via
    // openSolveCardLightboxFromElement(), keyed on each card's data-solve-card-kind.
    return;
  }
  renderSolveInspectorPanel(selected, view);

  let html = shouldShowSolveSourceColumn(view) ? buildSolveSourceColumn(selected[0], _runAspect(selected[0]), view) : "";
  selected.forEach((run) => {
    // The Color Regions tab has a sub-tab toggle (solveColorRegionsView): one
    // card region shows EITHER the color-ceiling height map (default) OR the
    // recipe viewer — never both. The recipe card needs its data (color-only
    // render + cookbook); compare-mode / pre-feature runs lack it, so we fall
    // back to the ceiling card there rather than a blank, dead card.
    const _rr = run.results || {};
    const recipeReady = _rr.predicted_color_only_appearance_url
      && _rr.color_recipe_breakdown_cookbook_url;
    if (view === "color_ceiling" && solveColorRegionsView === "recipe_regions" && recipeReady) {
      html += buildSolveRecipeColumn(run, _runAspect(run));
    } else {
      html += buildSolveRunColumn(run, _runAspect(run));
    }
  });
  grid.innerHTML = html;

  updateSolveSubControls();
  updateSolveColumnImages();
  updateSolveLegend();

  // Clicks are handled by the delegated #solveComparisonGrid listener via
  // openSolveCardLightboxFromElement(), keyed on each card's data-solve-card-kind.
}

function isBandedSolveRun(run) {
  const grouping = run?.results?.staged_metrics?.swap_grouping;
  return Array.isArray(grouping?.groups) && grouping.groups.length > 0;
}

function hasRecipeViewerArtifacts(run) {
  const result = run?.results || {};
  return Boolean(
    result.predicted_color_only_appearance_url
    && result.color_recipe_breakdown_cookbook_url,
  );
}

function shouldDefaultColorRegionsToRecipe() {
  const selected = getSelectedRuns().filter(run => run.results);
  return selected.length > 0
    && selected.some(isBandedSolveRun)
    && selected.every(hasRecipeViewerArtifacts);
}

function isSolveSourceColumnView(view) {
  return view !== "thickness_maps";
}

function shouldShowSolveSourceColumn(view) {
  return isSolveSourceColumnView(view) && solveShowSourceImage;
}

// Reserved per-card color-scale slot. Always present at a fixed height so card chrome
// never resizes between views (UI-stability rule, spec S4 §6.5). Empty for now; the
// deferred TM-SCALE work will populate per-card thickness/height scale bars here without
// reflowing the card.
function buildSolveCardScaleBarSlot() {
  return `<div class="solve-card-scalebar" aria-hidden="true"></div>`;
}

function buildSolveSourceColumn(run, aspect, view = solveView) {
  const srcUrl = run.results.source_url || "";
  const targetKind = getSolveSourceTargetKindForView(run, view);
  return `
    <div class="solve-grid-column is-source" data-solve-card-kind="source" data-run-id="${esc(run.id)}" data-view="${esc(view)}" data-source-target-kind="${esc(targetKind)}">
      <div class="solve-grid-column-header">
        <h4>Source</h4>
        <div class="comparison-column-chips" aria-hidden="true" style="visibility:hidden"><span class="comparison-chip"></span></div>
        <div class="comparison-column-stats" aria-hidden="true" style="visibility:hidden">RMSE % 0.000</div>
      </div>
      <div class="solve-grid-img-wrapper" style="--img-aspect:${aspect}">
        <img class="solve-grid-img solve-grid-result-img" data-run-id="${esc(run.id)}" src="${esc(srcUrl)}" alt="Source image">
      </div>
      ${buildSolveCardScaleBarSlot()}
    </div>`;
}

function getSolveSourceTargetKindForView(run, view = solveView) {
  if (isSolveSurfaceView(view)) return "surface";
  const r = run?.results || {};
  const recipeReady = r.predicted_color_only_appearance_url
    && r.color_recipe_breakdown_cookbook_url;
  if (view === "color_ceiling" && solveColorRegionsView === "recipe_regions" && recipeReady) {
    return "recipe";
  }
  return "run";
}

function getSolveRunHeaderStats(result, view) {
  if (!result) return "";
  if (view === "color_ceiling") {
    const grouping = result.staged_metrics?.swap_grouping;
    if (Array.isArray(grouping?.groups) && grouping.groups.length > 0) {
      return `flat ${result.color_ceiling_max_d?.toFixed(2) || "0.00"} mm`;
    }
    return `max ${result.color_ceiling_max_d?.toFixed(2) || "0.00"} mm`;
  }
  if (view === "total_surface") {
    return `max ${result.total_surface_max_d?.toFixed(2) || "0.00"} mm`;
  }
  if (view === "cap_map") {
    // White Cap is a height map only; prefer surface-height stats, fall back for old runs.
    const value = result.cap_surface_height_max_d ?? result.total_surface_max_d ?? result.cap_map_max_d;
    return `max ${Number(value || 0).toFixed(2)} mm`;
  }
  if (view === "boundary_cap_map") {
    const value = result.boundary_cap_surface_height_max_d ?? result.boundary_cap_map_max_d;
    return `max ${Number(value || 0).toFixed(2)} mm`;
  }
  if (view === "detail_cap_map") {
    const value = result.detail_cap_surface_height_max_d ?? result.detail_cap_map_max_d;
    return `max ${Number(value || 0).toFixed(2)} mm`;
  }
  if (view === "recipe_regions") {
    // The color-only recipe card shows its recipe count, not a color RMSE.
    // (updateSolveColumnImages refreshes this from here, so it must be handled
    // explicitly or it falls through to the full-render RMSE.)
    const n = result.color_recipe_breakdown_summary?.color_recipe_count;
    return n != null ? `${n} recipe${n === 1 ? "" : "s"} · click to explore` : "click to explore";
  }
  return formatColorRmse(result);
}

function getSolveContourUrl(result, view) {
  if (!result || !isSolveContourView(view)) return "";
  if (view === "recipe_regions") return "";
  if (view === "color_ceiling") {
    return result.color_ceiling_contour_bin_url || result.color_ceiling_bin_url || "";
  }
  if (view === "total_surface") {
    return result.total_surface_contour_bin_url || result.total_surface_bin_url || "";
  }
  // White Cap is a height map only; use surface-height contours, fall back to legacy for old runs.
  if (view === "cap_map") {
    return result.cap_surface_height_contour_bin_url || result.cap_map_contour_bin_url || "";
  }
  if (view === "boundary_cap_map") {
    return result.boundary_cap_surface_height_contour_bin_url || result.boundary_cap_contour_bin_url || "";
  }
  if (view === "detail_cap_map") {
    return result.detail_cap_surface_height_contour_bin_url || result.detail_cap_contour_bin_url || "";
  }
  return "";
}

async function loadSolveContourData(url) {
  if (!url) return null;
  if (solveContourDataCache[url]) return solveContourDataCache[url];
  const blob = await loadSurfaceBlob(url);
  if (!blob) return null;
  solveContourDataCache[url] = blob;
  return blob;
}

function drawSolveContourOverlay(canvas, blob) {
  if (!canvas || !blob) return;
  const { width, height, data } = blob;
  const contourScale = SURFACE_CONTOUR_SCALE;
  canvas.width = width * contourScale;
  canvas.height = height * contourScale;
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);

  strokeLayerContourPaths(ctx, data, width, height, getSolveSurfaceLayerHeight(), contourScale);
  canvas.style.display = "block";
}

function hasRecipeContourArtifacts(run) {
  const result = run?.results || {};
  return Boolean(
    result.explorer_stack_label_bin_url
    && Array.isArray(result.explorer_stack_table),
  );
}

function drawRecipeBoundaryOverlay(canvas, boundaries) {
  if (!canvas || !boundaries) return;
  const { width, height } = boundaries;
  const contourScale = SURFACE_CONTOUR_SCALE;
  canvas.width = width * contourScale;
  canvas.height = height * contourScale;
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  strokeDiscreteLabelBoundaries(ctx, boundaries, contourScale);
  canvas.style.display = "block";
}

function positionContourCanvasOverImage(canvas, blob) {
  if (!canvas || !blob) return false;
  const media = canvas.parentElement;
  const target = media?.querySelector(".solve-grid-result-img, .solve-grid-surface-canvas, .surface-lightbox-canvas, .comp-lightbox-img");
  if (!media || !target) return false;

  const boxW = target.clientWidth || target.getBoundingClientRect().width;
  const boxH = target.clientHeight || target.getBoundingClientRect().height;
  if (!boxW || !boxH) return false;

  const imgAspect = blob.width / Math.max(blob.height, 1);
  const boxAspect = boxW / Math.max(boxH, 1);
  let drawW = boxW;
  let drawH = boxH;
  let insetX = 0;
  let insetY = 0;
  if (boxAspect > imgAspect) {
    drawH = boxH;
    drawW = boxH * imgAspect;
    insetX = (boxW - drawW) / 2;
  } else if (boxAspect < imgAspect) {
    drawW = boxW;
    drawH = boxW / imgAspect;
    insetY = (boxH - drawH) / 2;
  }

  canvas.style.left = `${target.offsetLeft + insetX}px`;
  canvas.style.top = `${target.offsetTop + insetY}px`;
  canvas.style.width = `${drawW}px`;
  canvas.style.height = `${drawH}px`;
  return true;
}

function renderSurfaceContourOverlay(canvas, surfaceBlob) {
  if (!canvas || !surfaceBlob) {
    if (canvas) canvas.style.display = "none";
    return;
  }
  drawSolveContourOverlay(canvas, surfaceBlob);
  if (!positionContourCanvasOverImage(canvas, surfaceBlob)) {
    canvas.style.display = "none";
  }
}

function renderSolveContourCanvasForRun(canvas, run, view) {
  if (!canvas || !run?.results || !solveContoursEnabled || !isSolveContourView(view)) {
    if (canvas) canvas.style.display = "none";
    return;
  }
  const runId = run.id;
  if (view === "recipe_regions") {
    const sourceKey = `recipe:${runId}`;
    canvas.dataset.runId = runId;
    canvas.dataset.contourSource = sourceKey;
    ensureRecipeArtifactData(run).then((recipeData) => {
      const boundaries = recipeData?.recipeBoundaries;
      if (
        !boundaries
        || !canvas.isConnected
        || canvas.dataset.runId !== runId
        || canvas.dataset.contourSource !== sourceKey
        || !solveContoursEnabled
      ) {
        canvas.style.display = "none";
        return;
      }
      drawRecipeBoundaryOverlay(canvas, boundaries);
      if (!positionContourCanvasOverImage(canvas, boundaries)) {
        canvas.style.display = "none";
        return;
      }
      const img = canvas.parentElement?.querySelector(".solve-grid-result-img, .comp-lightbox-img");
      if (img && !img.complete) {
        img.addEventListener("load", () => {
          if (canvas.dataset.contourSource === sourceKey) {
            positionContourCanvasOverImage(canvas, boundaries);
          }
        }, { once: true });
      }
    });
    return;
  }
  const url = getSolveContourUrl(run.results, view);
  if (!url) {
    canvas.style.display = "none";
    return;
  }
  canvas.dataset.runId = runId;
  canvas.dataset.contourSource = url;
  loadSolveContourData(url).then(blob => {
    if (!blob || !canvas.isConnected || canvas.dataset.runId !== runId || canvas.dataset.contourSource !== url || !solveContoursEnabled || !isSolveContourView(view)) {
      canvas.style.display = "none";
      return;
    }
    drawSolveContourOverlay(canvas, blob);
    if (!positionContourCanvasOverImage(canvas, blob)) {
      canvas.style.display = "none";
      return;
    }
    const img = canvas.parentElement?.querySelector(".solve-grid-result-img, .comp-lightbox-img");
    if (img && !img.complete) {
      img.addEventListener("load", () => {
        if (canvas.dataset.contourSource === url) positionContourCanvasOverImage(canvas, blob);
      }, { once: true });
    }
  });
}

function renderSolveContourCanvases() {
  document.querySelectorAll(".solve-grid-contour-canvas[data-run-id]").forEach(canvas => {
    const run = solveRuns.find(r => r.id === canvas.dataset.runId);
    // The contour overlay belongs to the card it sits in, not the global view.
    const colView = canvas.closest(".solve-grid-column")?.dataset.view || solveView;
    renderSolveContourCanvasForRun(canvas, run, colView);
  });
}

function renderSolveLightboxContours(run, view) {
  const canvas = document.querySelector(".solve-lightbox-contour-canvas");
  renderSolveContourCanvasForRun(canvas, run, view);
}

// Mirror the global solveContoursEnabled state onto the recipe lightbox's own
// Contours button (it shares state with the main #solveContoursToggle).
function syncRecipeLightboxContoursToggle() {
  const btn = document.getElementById("recipeLightboxContoursToggle");
  if (!btn) return;
  const available = btn.dataset.contoursAvailable === "true";
  btn.disabled = !available;
  btn.classList.toggle("is-active", available && solveContoursEnabled);
  btn.setAttribute("aria-pressed", available && solveContoursEnabled ? "true" : "false");
}

function refreshVisibleSolveContours() {
  if (isSolveSurfaceView(solveView)) renderSolveSurfaceCanvases();
  if (isSolveContourView(solveView)) renderSolveContourCanvases();
  if (_solveLightboxState?.kind === "solve") {
    const run = solveRuns.find(r => r.id === _solveLightboxState.runId);
    if (run) renderSolveLightboxContours(run, _solveLightboxState.view ?? solveView);
  } else if (_solveLightboxState?.kind === "recipe") {
    const run = solveRuns.find(r => r.id === _solveLightboxState.runId);
    if (run) renderSolveLightboxContours(run, "recipe_regions");
  } else if (_solveLightboxState?.kind === "surface") {
    const run = solveRuns.find(r => r.id === _solveLightboxState.runId);
    const canvas = $("#lbSurfaceContourCanvas");
    if (run && canvas) {
      if (solveView === "surface_explorer") {
        // Explorer is always rich; rich shows material identity, not a contour overlay.
        canvas.style.display = "none";
      } else {
        ensureSurfaceData(run).then(cached => {
          if (cached) renderSurfaceContourOverlay(canvas, cached.surface);
        });
      }
    }
  }
}

function buildSolveRunColumn(run, aspect) {
  return buildSolveRunVisualColumn(run, aspect, solveView);
}

// Recipe-viewer card for the Color Regions view: the color-only predicted
// appearance (white cap omitted). Clicking it opens the rich recipe lightbox
// (cookbook tree + region highlighting). The card honors the contours toggle
// using boundaries between distinct canonical physical recipes.
function buildSolveRecipeColumn(run, aspect) {
  const view = "recipe_regions";
  const chips = (run.palette || []).map(fid => {
    const fil = allFilaments.find(f => f.filament_id === fid);
    const hex = fil?.hex || "#888";
    return `<span class="comparison-chip" style="background:${hex}"></span>`;
  }).join("");
  const summary = run.results?.color_recipe_breakdown_summary;
  const recipeCount = summary?.color_recipe_count;
  const statsLine = recipeCount != null
    ? `${recipeCount} recipe${recipeCount === 1 ? "" : "s"} · click to explore`
    : "Recipe viewer · click to explore";
  const contour = isSolveContourView(view)
    ? `<canvas class="solve-grid-contour-canvas" data-run-id="${escAttr(run.id)}" aria-label="${escAttr(run.label)} recipe boundaries"></canvas>`
    : "";
  return `
    <div class="solve-grid-column" data-solve-card-kind="recipe" data-run-id="${esc(run.id)}" data-view="${esc(view)}">
      <div class="solve-grid-column-header">
        <h4>${esc(run.label)} · Recipes</h4>
        <div class="comparison-column-chips">${chips}</div>
        <div class="comparison-column-stats">${esc(statsLine)}</div>
      </div>
      <div class="solve-grid-img-wrapper solve-grid-overlay-container" style="--img-aspect:${aspect}">
        <img class="solve-grid-img solve-grid-result-img" data-run-id="${esc(run.id)}" alt="${esc(run.label)} color-only render">
        ${contour}
      </div>
      ${buildSolveCardScaleBarSlot()}
      ${buildSolveRunCardMetadataFooter(run)}
    </div>`;
}

function buildSolveRunVisualColumn(run, aspect, view, opts = {}) {
  const r = run.results;
  const chips = (run.palette || []).map(fid => {
    const fil = allFilaments.find(f => f.filament_id === fid);
    const hex = fil?.hex || "#888";
    return `<span class="comparison-chip" style="background:${hex}"></span>`;
  }).join("");

  const statsLine = getSolveRunHeaderStats(r, view);
  const visual = isSolveSurfaceView(view)
    ? `<canvas class="solve-grid-img solve-grid-surface-canvas" data-run-id="${esc(run.id)}" data-view="${esc(view)}" aria-label="${esc(run.label)}"></canvas>
        <canvas class="solve-grid-contour-canvas solve-grid-surface-contour-canvas" data-run-id="${esc(run.id)}" aria-label="${esc(run.label)} layer contours"></canvas>`
    : `<img class="solve-grid-img solve-grid-result-img" data-run-id="${esc(run.id)}" alt="${esc(run.label)}">
        ${isSolveContourView(view) ? `<canvas class="solve-grid-contour-canvas" data-run-id="${esc(run.id)}" aria-label="${esc(run.label)} layer contours"></canvas>` : ""}`;
  const wrapperClass = "solve-grid-img-wrapper solve-grid-overlay-container";

  // Card metadata for the unified click dispatcher. Normal views resolve to a "run" or
  // "surface" card; diff-view callers override to a "thickness" card with an explicit
  // lightbox target (the diff run columns open a plain thickness lightbox, not the rich one).
  const cardKind = opts.cardKind || (isSolveSurfaceView(view) ? "surface" : "run");
  const thicknessData = opts.lightboxUrl
    ? ` data-thickness-url="${esc(opts.lightboxUrl)}" data-thickness-label="${esc(opts.lightboxLabel || "")}"`
    : "";

  return `
    <div class="solve-grid-column" data-solve-card-kind="${esc(cardKind)}" data-run-id="${esc(run.id)}" data-view="${esc(view)}"${thicknessData}>
      <div class="solve-grid-column-header">
        <h4>${esc(run.label)}</h4>
        <div class="comparison-column-chips">${chips}</div>
        <div class="comparison-column-stats">${statsLine}</div>
      </div>
      <div class="${wrapperClass}" style="--img-aspect:${aspect}">
        ${visual}
      </div>
      ${buildSolveCardScaleBarSlot()}
      ${buildSolveRunCardMetadataFooter(run)}
    </div>`;
}

function getSolveHighpassThreshold() {
  return getSolveSurfaceBaseThickness() + (parseInt($("#highpassThresholdSlider")?.value || "0") * getSolveSurfaceLayerHeight());
}

function getSolveExplorerCenter() {
  return getSolveSurfaceBaseThickness() + (parseInt($("#explorerHeightSlider")?.value || "0") * getSolveSurfaceLayerHeight());
}

function getSolveExplorerHalfBand() {
  return parseInt($("#explorerBandSlider")?.value || "1") * getSolveSurfaceLayerHeight();
}

function renderSolveSurfaceCanvases() {
  const view = solveView;
  if (!isSolveSurfaceView(view)) return;
  const tMax = parseFloat(config.t_max) || 3.0;
  const threshold = getSolveHighpassThreshold();
  const center = getSolveExplorerCenter();
  const halfBand = getSolveExplorerHalfBand();
  document.querySelectorAll(".solve-grid-surface-canvas[data-run-id]").forEach((canvas) => {
    const runId = canvas.dataset.runId;
    const run = solveRuns.find(r => r.id === runId);
    if (!run) return;
    const contourCanvas = canvas.parentElement?.querySelector(".solve-grid-surface-contour-canvas");
    ensureSurfaceData(run).then(cached => {
      // Bail if the user switched away from this surface view during the async load.
      if (solveView !== view) return;
      if (!cached) {
        if (contourCanvas) contourCanvas.style.display = "none";
        return;
      }
      if (view === "surface_highpass") {
        renderHighpass(canvas, cached.surface, tMax, threshold, getRunDiagnosticPaletteVersion(run));
      } else {
        // Explorer is always rich; fall back to the plain renderer only if material data is
        // missing (an error/loading state, not a user-selectable mode).
        ensureExplorerMaterialData(run).then(materialData => {
          if (solveView !== view) return;
          if (materialData) {
            renderExplorerRich(canvas, materialData, center, halfBand);
            if (contourCanvas) contourCanvas.style.display = "none";
          } else {
            renderExplorer(canvas, cached.surface, cached.ceiling, tMax, center, halfBand, getRunDiagnosticPaletteVersion(run));
            renderSurfaceContourOverlay(contourCanvas, cached.surface);
          }
        });
        return;
      }
      renderSurfaceContourOverlay(contourCanvas, cached.surface);
    });
  });
}

function updateSolveColumnImages() {
  const view = solveView;
  if (isSolveSurfaceView(view)) {
    renderSolveSurfaceCanvases();
    document.querySelectorAll(".solve-grid-column[data-run-id]:not(.is-source)").forEach(col => {
      const runId = col.dataset.runId;
      const run = solveRuns.find(r => r.id === runId);
      if (!run || !run.results) return;
      const statsEl = col.querySelector(".comparison-column-stats");
      if (statsEl) statsEl.textContent = getSolveRunHeaderStats(run.results, view);
    });
    return;
  }
  document.querySelectorAll(".solve-grid-column[data-run-id]:not(.is-source)").forEach(col => {
    const runId = col.dataset.runId;
    const run = solveRuns.find(r => r.id === runId);
    if (!run || !run.results) return;
    // Cards carry their own data-view, so multi-card views (Color Regions:
    // recipe card + ceiling card) resolve each card's image independently.
    const colView = col.dataset.view || view;
    const img = col.querySelector(".solve-grid-result-img");
    const contourCanvas = col.querySelector(".solve-grid-contour-canvas");
    if (img) {
      img.src = _getSolveRunResultUrl(run.results, colView) || "";
      img.style.opacity = "";
    }
    if (contourCanvas) contourCanvas.style.display = "none";
    const statsEl = col.querySelector(".comparison-column-stats");
    if (statsEl) statsEl.textContent = getSolveRunHeaderStats(run.results, colView);
  });
  if (isSolveContourView(view)) renderSolveContourCanvases();
}

function isWhiteCapMapView(view) {
  return view === "cap_map" || view === "boundary_cap_map" || view === "detail_cap_map";
}

function isSolveWhiteCapView(view) {
  return isWhiteCapMapView(view);
}

function isSolveContourView(view) {
  return isSolveWhiteCapView(view) || view === "color_ceiling" || view === "total_surface" || view === "recipe_regions";
}

function getSolveTopLevelView(view) {
  if (isSolveWhiteCapView(view)) return "white_cap";
  return view;
}

// Cap Diff / Color Diff are parked: kept in code but reachable only via ?dev=1.
function isSolveDevViewsEnabled() {
  try {
    return new URLSearchParams(window.location.search).get("dev") === "1";
  } catch (_e) {
    return false;
  }
}

// Views removed for normal users (Palette Fit + the old Diagnostic Views set). If any of these
// reaches solveView through stale in-memory state, console, or a test, normalize to predicted.
const SOLVE_REMOVED_VIEWS = new Set([
  "de_perceptual", "diagnostic_views",
  "printability_hard_fail", "stage2_boundary_mutation", "printability_width_loss",
]);

function coerceSolveViewForAccess() {
  if (SOLVE_REMOVED_VIEWS.has(solveView)) {
    solveView = "predicted";
  } else if (isSolveThicknessDiffView(solveView) && !isSolveDevViewsEnabled()) {
    solveView = "predicted";
  }
}

const SOLVE_ADVANCED_VIEWS = new Set(["thickness_maps", "surface_highpass"]);

function setSolveAdvancedViewsOpen(next) {
  solveAdvancedViewsOpen = !!next;
  const group = $("#solveAdvancedViews");
  const toggle = $("#solveAdvancedToggle");
  if (group) group.hidden = !solveAdvancedViewsOpen;
  if (toggle) {
    toggle.setAttribute("aria-expanded", solveAdvancedViewsOpen ? "true" : "false");
    toggle.classList.toggle("is-open", solveAdvancedViewsOpen);
  }
}

function syncSolveViewToggleActive() {
  const activeView = getSolveTopLevelView(solveView);
  // Never hide the active view's own button: if an advanced view is active, force the group open.
  if (SOLVE_ADVANCED_VIEWS.has(activeView) && !solveAdvancedViewsOpen) {
    setSolveAdvancedViewsOpen(true);
  }
  $$("#solveViewBar .view-toggle-btn").forEach((btn) => {
    btn.classList.toggle("is-active", btn.dataset.view === activeView);
  });
}

function _getSolveRunResultUrl(result, view) {
  switch (view) {
    case "predicted":
      // Appearance = F(predicted), baked server-side; strict, no fallback —
      // a broken image is the loud signal that the bake is missing.
      return result.predicted_appearance_url;
    // White Cap is a height map only; fall back to legacy thickness URLs for old runs.
    case "cap_map":
      return result.cap_surface_height_url || result.cap_map_url;
    case "boundary_cap_map":
      return result.boundary_cap_surface_height_url || result.boundary_cap_map_url || result.cap_surface_height_url || result.cap_map_url;
    case "detail_cap_map":
      return result.detail_cap_surface_height_url || result.detail_cap_map_url || result.cap_surface_height_url || result.cap_map_url;
    case "cap_diff":       return result.total_surface_url || result.predicted_url;
    case "color_ceiling": return result.color_ceiling_url;
    case "recipe_regions":
      // Color-only predicted appearance (white cap omitted), baked through F.
      return result.predicted_color_only_appearance_url || result.predicted_appearance_url;
    case "total_surface":  return result.total_surface_url;
    default:               return result.predicted_url;
  }
}

function updateSolveSubControls() {
  const view = solveView;
  const highpassCtrl = $("#solveHighpassControls");
  const explorerCtrl = $("#solveExplorerControls");
  const capDiffCtrl = $("#solveCapDiffControls");
  const filamentDiffCtrl = $("#solveFilamentDiffControls");
  const whiteCapCtrl = $("#solveWhiteCapControls");
  const contourCtrl = $("#solveContourControls");
  const sourceCtrl = $("#solveSourceControls");
  const sourceToggle = $("#solveSourceImageToggle");
  const contoursToggle = $("#solveContoursToggle");
  syncSolveViewToggleActive();
  document.querySelectorAll("[data-solve-white-cap-view]").forEach(btn => {
    const active = btn.dataset.solveWhiteCapView === solveView;
    btn.classList.toggle("is-active", active);
    btn.setAttribute("aria-checked", active ? "true" : "false");
  });
  document.querySelectorAll("[data-cap-diff-mode]").forEach(btn => {
    const active = btn.dataset.capDiffMode === solveCapDiffMode;
    btn.classList.toggle("is-active", active);
    btn.setAttribute("aria-checked", active ? "true" : "false");
  });
  // Explorer is always rich, which samples a single center layer (ignores band), so the band
  // controls stay hidden for Explorer — same as the old rich mode.
  document.querySelectorAll(".explorer-band-control").forEach(el => {
    el.classList.toggle("is-hidden", view === "surface_explorer");
  });
  if (contoursToggle) {
    const recipeBoundaryMode = view === "color_ceiling" && solveColorRegionsView === "recipe_regions";
    contoursToggle.classList.toggle("is-active", solveContoursEnabled);
    contoursToggle.setAttribute("aria-pressed", solveContoursEnabled ? "true" : "false");
    contoursToggle.textContent = `Contours: ${solveContoursEnabled ? "On" : "Off"}`;
    contoursToggle.title = recipeBoundaryMode
      ? "Show or hide recipe boundaries"
      : "Show or hide layer-height contour lines";
  }
  if (sourceCtrl) sourceCtrl.classList.toggle("is-hidden", !isSolveSourceColumnView(view));
  if (sourceToggle) {
    sourceToggle.classList.toggle("is-active", solveShowSourceImage);
    sourceToggle.setAttribute("aria-pressed", solveShowSourceImage ? "true" : "false");
    sourceToggle.textContent = `Source Image: ${solveShowSourceImage ? "On" : "Off"}`;
  }
  const colorRegionsCtrl = $("#solveColorRegionsControls");
  if (colorRegionsCtrl) colorRegionsCtrl.classList.toggle("is-hidden", view !== "color_ceiling");
  document.querySelectorAll("[data-solve-color-regions-view]").forEach(btn => {
    const active = btn.dataset.solveColorRegionsView === solveColorRegionsView;
    btn.classList.toggle("is-active", active);
    btn.setAttribute("aria-checked", active ? "true" : "false");
  });
  if (whiteCapCtrl) whiteCapCtrl.classList.toggle("is-hidden", !isSolveWhiteCapView(view));
  if (contourCtrl) contourCtrl.classList.toggle("is-hidden", !isSolveContourView(view));
  if (highpassCtrl) highpassCtrl.classList.toggle("is-hidden", view !== "surface_highpass");
  if (explorerCtrl) explorerCtrl.classList.toggle("is-hidden", view !== "surface_explorer");
  if (capDiffCtrl) capDiffCtrl.classList.toggle("is-hidden", !isSolveThicknessDiffView(view));
  if (filamentDiffCtrl) {
    filamentDiffCtrl.classList.toggle("is-hidden", !isSolveFilamentDiffView(view));
    if (isSolveFilamentDiffView(view)) syncSolveFilamentDiffControl();
  }
  if (view === "surface_highpass") initHighpassControls();
  if (view === "surface_explorer") initExplorerControls();
  updateSolveViewCaption(view);
}

function getSolveViewCaption(view = solveView) {
  if (view === "predicted") {
    return {
      title: "Predicted Appearance",
      body: "Shows how each selected run is expected to look when printed and lit.",
    };
  }
  if (view === "cap_map") {
    return {
      title: "Total White Cap",
      body: "Shows the full white cap surface added above the color layers.",
    };
  }
  if (view === "boundary_cap_map") {
    return {
      title: "Boundary Cap",
      body: "Shows the smooth white cap coverage that sits over the color layers.",
    };
  }
  if (view === "detail_cap_map") {
    return {
      title: "Detail Cap",
      body: "Shows the extra white cap material used to bring back image detail.",
    };
  }
  if (view === "color_ceiling") {
    if (solveColorRegionsView === "recipe_regions") {
      return {
        title: "Recipe Viewer",
        body: "Shows the color-only preview and the recipes used in each color region.",
      };
    }
    return {
      title: "Color Regions",
      body: "Shows the combined height of the colored material and base before the white cap is added.",
    };
  }
  if (view === "total_surface") {
    return {
      title: "Top Surface",
      body: "Shows the final top surface after color layers and white cap are combined.",
    };
  }
  if (view === "surface_explorer") {
    const height = getSolveExplorerCenter();
    return {
      title: "Surface Explorer",
      body: `Shows which material appears at ${height.toFixed(2)} mm from the base.`,
    };
  }
  if (view === "thickness_maps") {
    return {
      title: "Thickness Maps",
      body: "Shows how much of each filament is used, including total, boundary, and detail white-cap maps.",
    };
  }
  if (view === "surface_highpass") {
    const threshold = getSolveHighpassThreshold();
    return {
      title: "Surface Highpass",
      body: `Shows only areas where the surface is at least ${threshold.toFixed(2)} mm high.`,
    };
  }
  if (view === "cap_diff") {
    return {
      title: "Cap Diff",
      body: `Compares the white cap between two selected runs using ${solveCapDiffMode} mode.`,
    };
  }
  if (view === "filament_diff") {
    const filamentId = ensureSolveFilamentDiffSelection();
    const fil = filamentById(filamentId);
    return {
      title: "Color Diff",
      body: `Compares where ${fil?.color_name || "the selected filament"} is used in two selected runs.`,
    };
  }
  return {
    title: "Preview",
    body: "Shows the selected solve preview.",
  };
}

function updateSolveViewCaption(view = solveView) {
  const el = $("#solveViewCaption");
  if (!el) return;
  const caption = getSolveViewCaption(view);
  el.innerHTML = `
    <span class="solve-view-caption-title">${esc(caption.title)}</span>
    <span class="solve-view-caption-body">${esc(caption.body)}</span>`;
  el.classList.remove("is-hidden");
}

function getDiagnosticPaletteLegendState(runs) {
  const versions = new Set((runs || []).map(getRunDiagnosticPaletteVersion));
  const mixed = versions.size > 1;
  const version = mixed ? null : (versions.values().next().value || DIAGNOSTIC_PALETTE_INFERNO);
  return {
    mixed,
    version,
    gradient: version === DIAGNOSTIC_PALETTE_LEGACY
      ? "linear-gradient(to right, #440053, #b16819, #e6b600, #e1e800, #a2ff00)"
      : "linear-gradient(to right, #000004, #420a68, #932667, #dd513a, #fca50a, #fcffa4)",
  };
}

function diagnosticPaletteLegendHtml(state) {
  if (state.mixed) {
    return `<div class="solve-palette-warning" role="note">Mixed diagnostic palettes. Rerun the legacy solve for direct color comparison.</div>`;
  }
  return `<div class="legend-bar" data-diagnostic-palette="${esc(state.version)}" style="background:${state.gradient}"></div>`;
}

function updateSolveLegend(view = solveView) {
  const row = $("#solveLegend");
  const el = $("#solveLegendContent");
  if (!row || !el) return;
  const selected = getSelectedRuns().filter(r => r.results);
  const result = selected[0]?.results;
  const paletteLegend = getDiagnosticPaletteLegendState(selected);
  const contourLegendHtml = (label = "Layer-height contour") => `
    <div class="solve-contour-legend">
      <span class="solve-contour-legend-line"></span>
      <span>${label}</span>
    </div>`;

  if (view === "thickness_maps") {
    // Per-filament maps self-normalize to their own max (no shared scale yet — see TM-SCALE).
    el.innerHTML = `
      <div class="sub-legend-block">
        <span class="sub-legend-desc">Per-filament thickness maps (each self-normalized to its own max) &middot; White cap: Total, Boundary, and Detail</span>
        ${diagnosticPaletteLegendHtml(paletteLegend)}
      </div>`;
    row.classList.remove("is-hidden");
    return;
  }

  if (view === "color_ceiling" && solveColorRegionsView === "recipe_regions") {
    const boundaryAvailable = selected.some(hasRecipeContourArtifacts);
    const contourLegend = solveContoursEnabled && boundaryAvailable
      ? contourLegendHtml("Recipe boundary")
      : "";
    el.innerHTML = `
      <div class="sub-legend-block">
        <span class="sub-legend-desc">Recipe Viewer (color-only appearance)</span>
        ${contourLegend ? `<div class="solve-legend-inline">${contourLegend}</div>` : ""}
      </div>`;
    row.classList.remove("is-hidden");
    return;
  }

  if (view === "cap_map" || view === "boundary_cap_map" || view === "detail_cap_map") {
    // White Cap is a height map only — absolute surface height on the shared t_max scale.
    const scaleMax = getSolveSurfaceTMax();
    const valueKind = "absolute surface height";
    const label = view === "boundary_cap_map"
      ? `Boundary Cap (${valueKind}, shared absolute scale)`
      : view === "detail_cap_map"
        ? `Detail Cap (${valueKind}, shared absolute scale)`
        : `Total White Cap (${valueKind}, shared absolute scale)`;
    const zeroLabel = view === "boundary_cap_map"
      ? "No boundary cap here"
      : view === "detail_cap_map"
        ? "No detail cap here"
        : "No white cap here";
    const contourLegend = solveContoursEnabled
      ? contourLegendHtml()
      : "";
    el.innerHTML = `
      <div class="sub-legend-block">
        <span class="sub-legend-desc">${label}</span>
        ${diagnosticPaletteLegendHtml(paletteLegend)}
        <div class="legend-labels"><span>0 mm</span><span>${(scaleMax / 2).toFixed(1)} mm</span><span>${scaleMax.toFixed(1)} mm</span></div>
        <div class="solve-legend-inline">
          <span><span class="solve-legend-swatch is-empty"></span> ${zeroLabel}</span>
          ${contourLegend}
        </div>
      </div>`;
    row.classList.remove("is-hidden");
  } else if (view === "color_ceiling" || view === "total_surface") {
    const tMax = config.t_max || 3.0;
    const label = view === "color_ceiling" ? "Color Ceiling (base + colors)" : "Top Surface (base + colors + cap)";
    const contourLegend = isSolveContourView(view) && solveContoursEnabled
      ? contourLegendHtml()
      : "";
    el.innerHTML = `
      <div class="sub-legend-block">
        <span class="sub-legend-desc">${label}</span>
        ${diagnosticPaletteLegendHtml(paletteLegend)}
        <div class="legend-labels"><span>0 mm</span><span>${(tMax/2).toFixed(1)} mm</span><span>${tMax.toFixed(1)} mm</span></div>
        ${contourLegend ? `<div class="solve-legend-inline">${contourLegend}</div>` : ""}
      </div>`;
    row.classList.remove("is-hidden");
  } else if (view === "surface_highpass") {
    const tMax = config.t_max || 3.0;
    const threshold = getSolveHighpassThreshold();
    el.innerHTML = `
      <div class="sub-legend-block">
        <span class="sub-legend-desc">Surface Highpass (≥ threshold)</span>
        ${diagnosticPaletteLegendHtml(paletteLegend)}
        <div class="legend-labels"><span>${threshold.toFixed(2)} mm</span><span>${(tMax/2).toFixed(1)} mm</span><span>${tMax.toFixed(1)} mm</span></div>
      </div>`;
    row.classList.remove("is-hidden");
  } else if (view === "surface_explorer") {
    el.innerHTML = `
      <div class="sub-legend-block">
        <span class="sub-legend-desc">Explorer (color = solved material identity in the selected layer)</span>
        <div class="solve-legend-inline">
          <span><span class="solve-legend-swatch is-empty"></span> No material at this layer</span>
          <span><span class="solve-legend-swatch is-muted-cap"></span> Muted cap color = boundary cap; cap filament color = detail cap</span>
        </div>
      </div>`;
    row.classList.remove("is-hidden");
  } else if (view === "cap_diff" || view === "filament_diff") {
    const diff = view === "cap_diff"
      ? getCurrentSolveCapDiffFromCache()
      : getCurrentSolveFilamentDiffFromCache();
    const maxDeltaLabel = diff ? formatSolveDiffMm(diff.maxAbsDelta) : null;
    const diffLabel = view === "cap_diff" ? "cap-thickness" : "filament-thickness";
    const subjectLabel = view === "cap_diff" ? "cap" : "filament";
    let desc = `White = changed ${diffLabel} pixels`;
    let gradient = "linear-gradient(to right, #000000, #ffffff)";
    let labels = ["none", "", "changed"];
    if (solveCapDiffMode === "added") {
      desc = maxDeltaLabel
        ? `Green = ${diffLabel} increase (mm, max +${maxDeltaLabel})`
        : `Green = ${diffLabel} added`;
      gradient = "linear-gradient(to right, #000000, #0f7a3c)";
      labels = maxDeltaLabel ? ["0 mm", "", `+${maxDeltaLabel}`] : ["none", "", "added"];
    } else if (solveCapDiffMode === "removed") {
      desc = maxDeltaLabel
        ? `Red = ${diffLabel} decrease (mm, max -${maxDeltaLabel})`
        : `Red = ${diffLabel} removed`;
      gradient = "linear-gradient(to right, #000000, #b00020)";
      labels = maxDeltaLabel ? ["0 mm", "", `-${maxDeltaLabel}`] : ["none", "", "removed"];
    } else if (solveCapDiffMode === "signed") {
      desc = maxDeltaLabel
        ? `Green = thicker ${subjectLabel}, red = thinner ${subjectLabel} (${diffLabel} delta in mm, max ±${maxDeltaLabel})`
        : `Green = thicker ${subjectLabel}, red = thinner ${subjectLabel} (${diffLabel} delta in mm)`;
      gradient = "linear-gradient(to right, #8b0000, #10151c, #0f7a3c)";
      labels = maxDeltaLabel ? [`-${maxDeltaLabel}`, "0", `+${maxDeltaLabel}`] : ["thinner", "0", "thicker"];
    }
    el.innerHTML = `
      <div class="sub-legend-block">
        <span class="sub-legend-desc">${esc(desc)}</span>
        <div class="legend-bar" style="background:${gradient}"></div>
        <div class="legend-labels"><span>${esc(labels[0])}</span><span>${esc(labels[1])}</span><span>${esc(labels[2])}</span></div>
      </div>`;
    row.classList.remove("is-hidden");
  } else {
    row.classList.add("is-hidden");
  }
}

function renderSolveThicknessMaps(selectedRuns) {
  const mapsGrid = $("#filamentMapsGrid");
  if (!mapsGrid) return;
  mapsGrid.innerHTML = selectedRuns.map(run => {
    const r = run.results;
    if (!r) return "";
    const showLabel = selectedRuns.length > 1;
    let html = showLabel ? `<div class="filament-maps-label"><strong>${esc(run.label)}</strong></div>` : "";
    html += (r.filament_maps || []).map((m) => {
      const fil = filamentById(m.filament_id);
      const label = m.display_name || fil?.color_name || fil?.display_name || m.filament_id;
      const mapKey = `filament:${m.filament_id}`;
      const volume = formatThicknessMapVolume(m.volume_mm3);
      const statsLine = [
        volume,
        `max ${m.max_d?.toFixed(2) || 0} mm`,
        `${m.active_px?.toLocaleString() || 0} px`,
      ].filter(Boolean).join(" · ");
      const clickAttrs = m.map_url ? ` data-solve-card-kind="thickness" data-run-id="${esc(run.id)}"` : "";
      return `
        <div class="filament-map-card${m.map_url ? ' is-clickable' : ''}" data-map-key="${escAttr(mapKey)}"${clickAttrs}>
          <div class="solve-grid-column-header">
            <h4>${esc(label)}</h4>
            <div class="comparison-column-chips"><span class="comparison-chip" style="background:${esc(fil?.hex || '#ccc')}"></span></div>
            <div class="comparison-column-stats">${esc(statsLine)}</div>
          </div>
          ${m.map_url ? `<img src="${esc(m.map_url)}" alt="${esc(m.filament_id)}">` : `<div class="solve-grid-empty-map">No thickness</div>`}
          ${buildSolveCardScaleBarSlot()}
        </div>`;
    }).join("");
    const capItems = getSolveWhiteCapThicknessItems(run);
    capItems.forEach((item) => {
      const capStatsLine = item.available ? [
          formatThicknessMapVolume(item.volumeMm3),
          `max ${item.maxD.toFixed(2)} mm`,
          `${item.activePx.toLocaleString()} px`,
        ].filter(Boolean).join(" · ") : "Unavailable";
      const clickAttrs = item.available
        ? ` data-solve-card-kind="thickness" data-run-id="${esc(run.id)}"`
        : "";
      html += `
        <div class="filament-map-card${item.available ? ' is-clickable' : ' is-unavailable'}" data-map-key="${escAttr(item.key)}"${clickAttrs}>
          <div class="solve-grid-column-header">
            <h4>${esc(item.label)}</h4>
            <div class="comparison-column-chips"><span class="comparison-chip" style="background:#F4EFEB"></span></div>
            <div class="comparison-column-stats">${esc(capStatsLine)}</div>
          </div>
          ${item.available ? `<img src="${esc(item.url)}" alt="${esc(item.label)}">` : `<div class="solve-grid-empty-map">Unavailable</div>`}
          ${buildSolveCardScaleBarSlot()}
        </div>`;
    });
    return html;
  }).join("");
}

function formatThicknessMapVolume(volumeMm3) {
  const volume = Number(volumeMm3);
  if (!Number.isFinite(volume) || volume < 0) return "";
  if (volume >= 1000) return `vol ${(volume / 1000).toFixed(2)} cm3`;
  return `vol ${volume.toFixed(volume >= 10 ? 0 : 2)} mm3`;
}

let _solveLightboxState = null;

function getSolveLightboxViewLabel(view) {
  switch (view) {
    case "source": return "Source";
    case "predicted": return "Predicted";
    case "cap_map": return "Total White Cap";
    case "boundary_cap_map": return "Boundary Cap";
    case "detail_cap_map": return "Detail Cap";
    case "color_ceiling": return "Color Ceiling";
    case "recipe_regions": return "Color Regions";
    case "total_surface": return "Top Surface";
    case "surface_highpass": return "Highpass";
    case "surface_explorer": return "Explorer";
    default: return "Result";
  }
}

function buildSolveRunPaletteChips(run) {
  return (run.palette || []).map((fid) => {
    const fil = allFilaments.find((f) => f.filament_id === fid);
    const hex = fil?.hex || "#888";
    const title = fil?.display_name || fil?.color_name || fid;
    return `<span class="comp-lightbox-chip" style="background:${hex}" title="${esc(title)}"></span>`;
  }).join("");
}

function buildSolveLightboxHeader(run, viewLabel, trailingControls = "") {
  const paletteChips = buildSolveRunPaletteChips(run);
  return `
    <div class="comp-lightbox-topbar surface-lightbox-topbar">
      <div class="comp-lightbox-runmeta">
        <span class="comp-lightbox-runtitle">${esc(run.label)}</span>
        <span class="comp-lightbox-viewtag">${esc(viewLabel)}</span>
      </div>
      <div class="comp-lightbox-header-end">
        ${trailingControls}
        <div class="comp-lightbox-palette" aria-label="Palette">${paletteChips}</div>
      </div>
    </div>
  `;
}

function getSelectedSolveRunsWithResults() {
  return getSelectedRuns().filter(r => r.results);
}

// White-cap thickness maps for the Thickness Maps view. These are actual cap *thickness*
// maps (distinct from the height maps shown in the White Cap view). Keep all semantic slots,
// including unavailable legacy artifacts, so labels and card order can never shift.
function getSolveWhiteCapThicknessItems(run) {
  const r = run?.results || {};
  return [
    {
      key: "cap:total",
      label: "Total White Cap",
      viewLabel: "Total White Cap Thickness",
      url: r.cap_map_url,
      activePx: Number(r.cap_map_active_px || 0),
      maxD: Number(r.cap_map_max_d || 0),
      volumeMm3: r.cap_map_volume_mm3,
    }, {
      key: "cap:boundary",
      label: "Boundary Cap",
      viewLabel: "Boundary Cap Thickness",
      url: r.boundary_cap_map_url,
      activePx: Number(r.boundary_cap_map_active_px || 0),
      maxD: Number(r.boundary_cap_map_max_d || 0),
      volumeMm3: r.boundary_cap_map_volume_mm3,
    }, {
      key: "cap:detail",
      label: "Detail Cap",
      viewLabel: "Detail Cap Thickness",
      url: r.detail_cap_map_url,
      activePx: Number(r.detail_cap_map_active_px || 0),
      maxD: Number(r.detail_cap_map_max_d || 0),
      volumeMm3: r.detail_cap_map_volume_mm3,
    },
  ].map(item => ({ ...item, available: Boolean(item.url) }));
}

function getSolveThicknessDisplayItems(run) {
  if (!run?.results) return [];
  const maps = run.results.filament_maps || [];
  const items = maps.map((m) => {
    const fil = filamentById(m.filament_id);
    return {
      key: `filament:${m.filament_id}`,
      label: m.display_name || fil?.color_name || fil?.display_name || m.filament_id,
      viewLabel: `${m.display_name || fil?.color_name || fil?.display_name || m.filament_id} Thickness`,
      url: m.map_url || "",
      available: Boolean(m.map_url),
    };
  });
  items.push(...getSolveWhiteCapThicknessItems(run));
  return items;
}

function getSolveThicknessItems(run) {
  return getSolveThicknessDisplayItems(run).filter(item => item.available);
}

function openThicknessLightboxForKey(runId, mapKey) {
  const run = solveRuns.find(r => r.id === runId);
  const items = getSolveThicknessItems(run);
  const idx = items.findIndex(candidate => candidate.key === mapKey);
  const item = idx >= 0 ? items[idx] : null;
  if (!run || !item) return;
  _solveLightboxState = { kind: "thickness", runId, mapKey: item.key, mapIndex: idx };

  const lb = $("#compLightbox");
  const content = $("#compLightboxContent");
  if (!lb || !content) return;
  const lifecycle = beginLightboxLifecycle();
  const zoomControls = buildStaticLightboxZoomControls();
  content.innerHTML = `
    <div class="comp-lightbox-pane">
      ${buildSolveLightboxHeader(run, item.viewLabel, zoomControls)}
      <div class="comp-lightbox-media static-zoom-media">
        <img class="comp-lightbox-img" src="${esc(item.url)}" style="image-rendering:pixelated;" alt="${esc(item.label)}">
      </div>
    </div>`;
  lb.classList.remove("is-hidden");
  setupStaticLightboxZoom(content, lifecycle);
}

function solveRunById(runId) {
  if (!runId) return null;
  return solveRuns.find(r => r.id === runId) || null;
}

function openSolvePreviewLightboxForRun(run, view = solveView, targetKind = "run") {
  if (!run) return;
  if (targetKind === "surface") {
    openSurfaceLightbox(view, run.id);
  } else if (targetKind === "recipe") {
    openRecipeLightbox(run.id);
  } else {
    openSolveRunLightbox(run.id, view);
  }
}

// Single click→lightbox dispatcher for every solve result card. Each card declares its
// kind + payload via data attributes (data-solve-card-kind, plus data-run-id / data-view /
// data-map-index / data-thickness-url). This replaces the three previously divergent click
// paths (normal grid columns, dev-only diff columns, and the thickness-map grid).
function openSolveCardLightboxFromElement(card) {
  if (!card) return;
  const kind = card.dataset.solveCardKind || "";
  const runId = card.dataset.runId || "";
  if (kind === "source") {
    const run = solveRunById(runId);
    if (run) {
      openSolveSourceLightbox(
        run,
        card.dataset.view || solveView,
        card.dataset.sourceTargetKind || "run",
      );
    }
    return;
  }
  if (kind === "run") {
    if (runId) openSolveRunLightbox(runId, card.dataset.view || solveView);
    return;
  }
  if (kind === "surface") {
    if (runId) openSurfaceLightbox(card.dataset.view || solveView, runId);
    return;
  }
  if (kind === "recipe") {
    if (runId) openRecipeLightbox(runId);
    return;
  }
  if (kind === "thickness") {
    if (runId && card.dataset.mapKey) openThicknessLightboxForKey(runId, card.dataset.mapKey);
    return;
  }
  // kind === "diff" (or unknown): no lightbox — preserves the prior no-op for diff columns.
}

let _lightboxCleanup = null;
let _lightboxInstanceToken = 0;

function beginLightboxLifecycle() {
  if (_lightboxCleanup) _lightboxCleanup();
  const token = ++_lightboxInstanceToken;
  const cleanups = [];
  let active = true;
  const cleanup = () => {
    if (!active) return;
    active = false;
    while (cleanups.length) {
      const dispose = cleanups.pop();
      try { dispose(); } catch (_error) { /* cleanup remains best-effort and idempotent */ }
    }
    if (_lightboxCleanup === cleanup) _lightboxCleanup = null;
  };
  _lightboxCleanup = cleanup;
  return {
    token,
    addCleanup(dispose) { if (active) cleanups.push(dispose); else dispose(); },
    isActive() { return active && token === _lightboxInstanceToken; },
    cleanup,
  };
}

function computeLightboxScaleBounds({
  intrinsicWidth,
  intrinsicHeight,
  viewportWidth,
  viewportHeight,
  headerHeight = 0,
  headerMinWidth = 0,
  inset = 24,
}) {
  const imageWidth = Math.max(1, Number(intrinsicWidth) || 1);
  const imageHeight = Math.max(1, Number(intrinsicHeight) || 1);
  const usableWidth = Math.max(1, (Number(viewportWidth) || 1) - inset * 2);
  const usableHeight = Math.max(1, (Number(viewportHeight) || 1) - inset * 2);
  const mediaHeight = Math.max(1, usableHeight - Math.max(0, Number(headerHeight) || 0));
  const fitScale = Math.max(0.0001, Math.min(usableWidth / imageWidth, mediaHeight / imageHeight));
  const collapsed = fitScale < 1;
  const minScale = collapsed ? fitScale : 1;
  const maxScale = fitScale;
  return {
    minScale,
    maxScale,
    collapsed,
    usableWidth,
    usableHeight,
    headerWidth: Math.min(usableWidth, Math.max(0, Number(headerMinWidth) || 0)),
  };
}

function normalizeStaticZoomWheelDelta(event, viewportHeight = window.innerHeight) {
  const multiplier = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? viewportHeight : 1;
  return Number(event.deltaY || 0) * multiplier;
}

function applyStaticZoomWheelDelta(value, accumulatedDelta, deltaPixels, threshold = 36, step = 5) {
  let accumulated = accumulatedDelta + deltaPixels;
  let next = Number(value) || 0;
  while (Math.abs(accumulated) >= threshold) {
    const direction = accumulated < 0 ? 1 : -1;
    const candidate = Math.max(0, Math.min(100, next + direction * step));
    accumulated -= Math.sign(accumulated) * threshold;
    if (candidate === next) {
      accumulated = 0;
      break;
    }
    next = candidate;
  }
  return { value: next, accumulatedDelta: accumulated, changed: next !== Number(value) };
}

function buildStaticLightboxZoomControls() {
  return `
    <label class="comp-lightbox-zoom">
      <span class="comp-lightbox-zoom-label">Zoom</span>
      <span class="comp-lightbox-zoom-endpoint">Min</span>
      <input class="comp-lightbox-zoom-slider" type="range" min="0" max="100" step="1" value="100" aria-label="Zoom" aria-valuetext="100%, maximum">
      <span class="comp-lightbox-zoom-endpoint">Max</span>
    </label>`;
}

function setupStaticLightboxZoom(content, lifecycle, onLayout = null) {
  const pane = content?.querySelector(".comp-lightbox-pane");
  const header = pane?.querySelector(".comp-lightbox-topbar");
  const media = pane?.querySelector(".static-zoom-media");
  const image = media?.querySelector(".comp-lightbox-img");
  const slider = header?.querySelector(".comp-lightbox-zoom-slider");
  if (!pane || !header || !media || !image || !slider) return;
  let wheelDelta = 0;

  const updateAccessibleValue = () => {
    const value = Number(slider.value);
    const endpoint = value === 0 ? ", minimum" : value === 100 ? ", maximum" : "";
    slider.setAttribute("aria-valuetext", `${value}%${endpoint}`);
  };

  const relayout = () => {
    if (!lifecycle.isActive()) return;
    const intrinsicWidth = image.naturalWidth;
    const intrinsicHeight = image.naturalHeight;
    if (!intrinsicWidth || !intrinsicHeight) return;
    pane.style.width = "auto";
    const bounds = computeLightboxScaleBounds({
      intrinsicWidth,
      intrinsicHeight,
      viewportWidth: window.innerWidth,
      viewportHeight: window.innerHeight,
      headerHeight: header.getBoundingClientRect().height,
      headerMinWidth: header.scrollWidth,
    });
    slider.disabled = bounds.collapsed;
    const normalized = bounds.collapsed ? 100 : Number(slider.value) / 100;
    const scale = bounds.minScale + (bounds.maxScale - bounds.minScale) * normalized;
    const width = Math.max(1, Math.floor(intrinsicWidth * scale));
    const height = Math.max(1, Math.floor(intrinsicHeight * scale));
    pane.style.width = `${Math.ceil(Math.max(width, bounds.headerWidth))}px`;
    media.style.width = `${width}px`;
    media.style.height = `${height}px`;
    image.style.width = `${width}px`;
    image.style.height = `${height}px`;
    updateAccessibleValue();
    if (onLayout) onLayout();
  };

  const onInput = () => relayout();
  const onWheel = (event) => {
    if (event.ctrlKey || event.metaKey) return;
    const outcome = applyStaticZoomWheelDelta(
      Number(slider.value),
      wheelDelta,
      normalizeStaticZoomWheelDelta(event),
    );
    wheelDelta = outcome.accumulatedDelta;
    if (!outcome.changed) return;
    slider.value = String(outcome.value);
    relayout();
    event.preventDefault();
  };
  const onResize = () => relayout();
  const onLoad = () => relayout();
  slider.addEventListener("input", onInput);
  media.addEventListener("wheel", onWheel, { passive: false });
  image.addEventListener("load", onLoad);
  window.addEventListener("resize", onResize);
  lifecycle.addCleanup(() => slider.removeEventListener("input", onInput));
  lifecycle.addCleanup(() => media.removeEventListener("wheel", onWheel));
  lifecycle.addCleanup(() => image.removeEventListener("load", onLoad));
  lifecycle.addCleanup(() => window.removeEventListener("resize", onResize));
  if (typeof ResizeObserver !== "undefined") {
    const observer = new ResizeObserver(() => relayout());
    observer.observe(header);
    lifecycle.addCleanup(() => observer.disconnect());
  }
  if (image.complete && image.naturalWidth) relayout();
  if (typeof image.decode === "function") {
    image.decode().catch(() => {}).then(() => { if (lifecycle.isActive()) relayout(); });
  }
}

function openSolveRunLightbox(runId, view = solveView) {
  const run = solveRuns.find(r => r.id === runId);
  if (!run || !run.results) return;
  // Capture the displayed view in the lightbox state so arrow-key navigation between runs
  // keeps showing the same view, independent of any later global solveView change.
  _solveLightboxState = { kind: "solve", runId, view };

  const lb = $("#compLightbox");
  const content = $("#compLightboxContent");
  if (!lb || !content) return;
  const lifecycle = beginLightboxLifecycle();

  const url = _getSolveRunResultUrl(run.results, view) || "";
  const pixelated = "image-rendering:pixelated;";
  const zoomControls = buildStaticLightboxZoomControls();

  const contourLabel = view === "recipe_regions" ? "recipe boundaries" : "layer contours";
  const contourCanvas = isSolveContourView(view) && solveContoursEnabled
    ? `<canvas class="solve-lightbox-contour-canvas" aria-label="${escAttr(run.label)} ${contourLabel}"></canvas>`
    : "";
  content.innerHTML = `
        <div class="comp-lightbox-pane">
          ${buildSolveLightboxHeader(run, getSolveLightboxViewLabel(view), zoomControls)}
          <div class="comp-lightbox-media static-zoom-media">
            <img class="comp-lightbox-img" src="${url}" style="${pixelated}">
            ${contourCanvas}
          </div>
        </div>`;
  lb.classList.remove("is-hidden");

  const renderContours = () => {
    if (isSolveContourView(view) && solveContoursEnabled) renderSolveLightboxContours(run, view);
  };
  setupStaticLightboxZoom(content, lifecycle, renderContours);
}

function openSolveSourceLightbox(run, view = solveView, targetKind = "run") {
  if (!run || !run.results) return;
  _solveLightboxState = { kind: "source", runId: run.id, view, targetKind };
  const lb = $("#compLightbox");
  const content = $("#compLightboxContent");
  if (!lb || !content) return;
  const lifecycle = beginLightboxLifecycle();
  const pixelated = "image-rendering:pixelated;";
  const zoomControls = buildStaticLightboxZoomControls();
  content.innerHTML = `
        <div class="comp-lightbox-pane">
          ${buildSolveLightboxHeader(run, getSolveLightboxViewLabel("source"), zoomControls)}
          <div class="comp-lightbox-media static-zoom-media">
            <img class="comp-lightbox-img" src="${esc(run.results.source_url || "")}" style="${pixelated}" alt="Source image">
          </div>
        </div>`;
  lb.classList.remove("is-hidden");
  setupStaticLightboxZoom(content, lifecycle);
}

// ── Surface lightbox (interactive fullscreen) ─────────────────────────

async function openSurfaceLightbox(viewType, runId = null) {
  const lb = $("#compLightbox");
  const content = $("#compLightboxContent");
  if (!lb || !content) return;
  const selected = getSelectedRuns().filter(r => r.results);
  if (!selected.length) return;
  const run = runId ? solveRuns.find(r => r.id === runId) : selected[0];
  if (!run) return;
  const lifecycle = beginLightboxLifecycle();
  _solveLightboxState = { kind: "surface", runId: run.id, viewType };
  const cached = await ensureSurfaceData(run);
  if (!lifecycle.isActive() || !cached) return;

  const tMax = getSolveSurfaceTMax();
  const lh = getSolveSurfaceLayerHeight();
  const base = getSolveSurfaceBaseThickness();
  const steps = getSolveSurfaceExtraSteps();

  // Copy current slider values from the inline controls
  const curThreshold = parseInt($("#highpassThresholdSlider")?.value ?? steps);
  const curHeight = parseInt($("#explorerHeightSlider")?.value ?? steps);
  const curBand = parseInt($("#explorerBandSlider")?.value ?? 3);

  let html = `<div class="surface-lightbox-wrap">
    ${buildSolveLightboxHeader(run, getSolveLightboxViewLabel(viewType))}`;
  if (viewType === "surface_highpass") {
    html += `
      <div class="surface-controls surface-lightbox-controls">
        <label class="surface-control-label">
          Threshold:
          <span class="surface-control-value" id="lbHighpassValue"></span>
          <span class="surface-control-hint" id="lbHighpassHint"></span>
        </label>
        <input type="range" id="lbHighpassSlider" min="0" max="${steps}" value="${curThreshold}" step="1" class="surface-slider">
      </div>`;
  } else {
    // Explorer is always rich (samples a single center layer), so no band control.
    html += `
      <div class="surface-controls surface-lightbox-controls">
        <label class="surface-control-label">
          Height:
          <span class="surface-control-value" id="lbExplorerHeightValue"></span>
          <span class="surface-control-hint" id="lbExplorerHeightHint"></span>
        </label>
        <input type="range" id="lbExplorerHeightSlider" min="0" max="${steps}" value="${curHeight}" step="1" class="surface-slider">
      </div>`;
  }
  html += `
      <div class="surface-lightbox-frame">
        <canvas class="surface-lightbox-canvas" id="lbSurfaceCanvas"></canvas>
        <canvas class="solve-lightbox-contour-canvas surface-lightbox-contour-canvas" id="lbSurfaceContourCanvas" aria-label="Layer contours"></canvas>
      </div>
    </div>`;
  content.innerHTML = html;
  lb.classList.remove("is-hidden");

  // Prevent clicks inside the surface controls from closing the lightbox
  const wrap = content.querySelector(".surface-lightbox-wrap");
  if (wrap) wrap.addEventListener("click", (e) => e.stopPropagation());

  const canvas = $("#lbSurfaceCanvas");
  const contourCanvas = $("#lbSurfaceContourCanvas");
  const header = wrap?.querySelector(".comp-lightbox-topbar");
  const controls = wrap?.querySelector(".surface-lightbox-controls");
  const frame = wrap?.querySelector(".surface-lightbox-frame");
  const cleanups = [];
  const paletteVersion = getRunDiagnosticPaletteVersion(run);

  // Scale canvas CSS to fill lightbox while preserving aspect ratio.
  // Called after first render (which sets canvas.width/height to data dims) and
  // measures the actual header/control strips instead of reserving a fixed budget.
  function scaleCanvasToFit() {
    if (!lifecycle.isActive()) return;
    const dataW = canvas.width, dataH = canvas.height;
    if (!dataW || !dataH) return;
    const ar = dataW / dataH;
    const maxW = Math.max(1, window.innerWidth - 48);
    const maxH = Math.max(
      1,
      window.innerHeight - 48
        - (header?.getBoundingClientRect().height || 0)
        - (controls?.getBoundingClientRect().height || 0),
    );
    let w, h;
    if (maxW / maxH > ar) {
      h = maxH; w = h * ar;
    } else {
      w = maxW; h = w / ar;
    }
    canvas.style.width = `${Math.floor(w)}px`;
    canvas.style.height = `${Math.floor(h)}px`;
    if (frame) {
      frame.style.width = canvas.style.width;
      frame.style.height = canvas.style.height;
    }
  }

  const onResize = () => {
    scaleCanvasToFit();
    // Highpass always shows this overlay. Explorer shows it only for the legacy/plain
    // fallback, so preserve whichever rendered state is currently visible.
    if (contourCanvas?.style.display !== "none") {
      renderSurfaceContourOverlay(contourCanvas, cached.surface);
    }
  };
  window.addEventListener("resize", onResize);
  cleanups.push(() => window.removeEventListener("resize", onResize));
  if (typeof ResizeObserver !== "undefined" && header && controls) {
    const observer = new ResizeObserver(() => onResize());
    observer.observe(header);
    observer.observe(controls);
    cleanups.push(() => observer.disconnect());
  }

  if (viewType === "surface_highpass") {
    const slider = $("#lbHighpassSlider");
    const valEl = $("#lbHighpassValue");
    const hintEl = $("#lbHighpassHint");

    function render() {
      const th = base + (parseInt(slider.value) * lh);
      const layers = parseInt(slider.value);
      valEl.textContent = `${th.toFixed(2)} mm`;
      hintEl.textContent = `(${layers} layers)`;
      renderHighpass(canvas, cached.surface, tMax, th, paletteVersion);
      scaleCanvasToFit();
      renderSurfaceContourOverlay(contourCanvas, cached.surface);
      // Sync inline slider
      const inline = $("#highpassThresholdSlider");
      if (inline) inline.value = slider.value;
    }

    const onInput = () => render();
    const onWheel = (e) => {
      e.preventDefault();
      const dir = e.deltaY > 0 ? -1 : 1;
      slider.value = Math.max(0, Math.min(steps, parseInt(slider.value) + dir));
      render();
    };
    slider.addEventListener("input", onInput);
    slider.addEventListener("wheel", onWheel, { passive: false });
    cleanups.push(() => { slider.removeEventListener("input", onInput); slider.removeEventListener("wheel", onWheel); });
    render();

  } else {
    const hSlider = $("#lbExplorerHeightSlider");
    const bSlider = $("#lbExplorerBandSlider");
    const hVal = $("#lbExplorerHeightValue");
    const hHint = $("#lbExplorerHeightHint");
    const bVal = $("#lbExplorerBandValue");
    const bHint = $("#lbExplorerBandHint");

    async function render() {
      const center = base + (parseInt(hSlider.value) * lh);
      const halfBand = bSlider ? parseInt(bSlider.value) * lh : lh / 2;
      hVal.textContent = `${center.toFixed(2)} mm`;
      hHint.textContent = `(layer ${parseInt(hSlider.value)})`;
      if (bVal) bVal.textContent = `± ${halfBand.toFixed(2)} mm`;
      if (bHint && bSlider) bHint.textContent = `(${parseInt(bSlider.value)} layers)`;
      let renderedRich = false;
      // Always rich; plain renderer only as a fallback when material data is unavailable.
      const materialData = await ensureExplorerMaterialData(run);
      if (!lifecycle.isActive()) return;
      if (materialData) {
        renderExplorerRich(canvas, materialData, center, halfBand);
        renderedRich = true;
      } else {
        renderExplorer(canvas, cached.surface, cached.ceiling, tMax, center, halfBand, paletteVersion);
      }
      scaleCanvasToFit();
      if (renderedRich) {
        if (contourCanvas) contourCanvas.style.display = "none";
      } else {
        renderSurfaceContourOverlay(contourCanvas, cached.surface);
      }
      // Sync inline sliders
      const inH = $("#explorerHeightSlider");
      const inB = $("#explorerBandSlider");
      if (inH) inH.value = hSlider.value;
      if (inB && bSlider) inB.value = bSlider.value;
    }

    const onHInput = () => render();
    const onBInput = () => render();
    const onHWheel = (e) => {
      e.preventDefault();
      hSlider.value = Math.max(0, Math.min(steps, parseInt(hSlider.value) + (e.deltaY > 0 ? -1 : 1)));
      render();
    };
    const onBWheel = (e) => {
      e.preventDefault();
      const max = parseInt(bSlider.max);
      bSlider.value = Math.max(1, Math.min(max, parseInt(bSlider.value) + (e.deltaY > 0 ? -1 : 1)));
      render();
    };
    hSlider.addEventListener("input", onHInput);
    if (bSlider) bSlider.addEventListener("input", onBInput);
    hSlider.addEventListener("wheel", onHWheel, { passive: false });
    if (bSlider) bSlider.addEventListener("wheel", onBWheel, { passive: false });
    cleanups.push(() => {
      hSlider.removeEventListener("input", onHInput);
      if (bSlider) bSlider.removeEventListener("input", onBInput);
      hSlider.removeEventListener("wheel", onHWheel);
      if (bSlider) bSlider.removeEventListener("wheel", onBWheel);
    });
    render();
  }

  cleanups.forEach(dispose => lifecycle.addCleanup(dispose));
}

// ── Recipe viewer lightbox (Color Regions) ────────────────────────────
// Interactive cookbook explorer: the color-only predicted image with a side
// panel that IS the recipe taxonomy (family -> combo -> specific recipe). Select
// any node to highlight the union of regions whose stack maps to a recipe under
// it; hover/click a region to read its exact filament thicknesses. All region
// data is derived client-side from the explorer stack-label bin + stack table
// (no new per-pixel backend data) plus the recipe cookbook artifact.

const recipeDataCache = {};
const recipeDataPromiseCache = {};
const recipeCookbookPromiseCache = {};
const recipeDataGeneration = {};

// Canonical, format-stable recipe key computed identically for cookbook recipe
// nodes and stack-table entries, so the two match regardless of how Python
// formatted the backend recipe_key. Banded entries preserve their physical band
// index so same-white fill intervals cannot collapse into one recipe key.
function _recipeKeyFromEntries(entries) {
  const norm = (entries || [])
    .map((e) => ({
      fid: String(e?.filament_id ?? e?.filamentId ?? ""),
      th: Number(e?.thickness_mm ?? e?.thicknessMm ?? 0) || 0,
      band: e?.band_index ?? e?.bandIndex ?? null,
    }))
    .filter((e) => e.fid && e.th > 1e-9)
    .sort((a, b) => {
      const aBand = a.band == null ? -1 : Number(a.band);
      const bBand = b.band == null ? -1 : Number(b.band);
      if (aBand !== bBand) return aBand - bBand;
      return a.fid < b.fid ? -1 : a.fid > b.fid ? 1 : a.th - b.th;
    });
  if (!norm.length) return "base-only";
  return norm.map((e) => `${e.band == null ? "" : `b${Number(e.band)}:`}${e.fid}:${e.th.toFixed(4)}`).join(" | ");
}

function buildRecipeIdentityMap(stackLabels, stackKeyById) {
  if (!stackLabels?.data || !Array.isArray(stackKeyById)) return null;
  const { width, height, data: stackIds } = stackLabels;
  if (stackIds.length !== width * height) return null;

  const identityByKey = new Map();
  const identityByStackId = new Uint32Array(stackKeyById.length);
  stackKeyById.forEach((key, stackId) => {
    if (!identityByKey.has(key)) identityByKey.set(key, identityByKey.size);
    identityByStackId[stackId] = identityByKey.get(key);
  });

  const data = new Uint32Array(stackIds.length);
  for (let i = 0; i < stackIds.length; i++) {
    const stackId = stackIds[i];
    if (stackId >= identityByStackId.length) return null;
    data[i] = identityByStackId[stackId];
  }
  return { width, height, data };
}

function buildDiscreteLabelBoundaryMask(identityMap) {
  if (!identityMap?.data) return null;
  const { width, height, data } = identityMap;
  if (width <= 0 || height <= 0 || data.length !== width * height) return null;
  const vertical = new Uint8Array(Math.max(0, width - 1) * height);
  const horizontal = new Uint8Array(width * Math.max(0, height - 1));

  for (let y = 0; y < height; y++) {
    const row = y * width;
    const boundaryRow = y * Math.max(0, width - 1);
    for (let x = 0; x < width - 1; x++) {
      vertical[boundaryRow + x] = data[row + x] === data[row + x + 1] ? 0 : 1;
    }
  }
  for (let y = 0; y < height - 1; y++) {
    const row = y * width;
    const nextRow = row + width;
    for (let x = 0; x < width; x++) {
      horizontal[row + x] = data[row + x] === data[nextRow + x] ? 0 : 1;
    }
  }
  return { width, height, vertical, horizontal };
}

async function ensureRecipeArtifactData(run) {
  if (!run?.results) return null;
  if (recipeDataCache[run.id]) return recipeDataCache[run.id];
  if (recipeDataPromiseCache[run.id]) return recipeDataPromiseCache[run.id];
  const r = run.results;
  if (!r.explorer_stack_label_bin_url || !Array.isArray(r.explorer_stack_table)) return null;
  const generation = recipeDataGeneration[run.id] || 0;

  const pending = (async () => {
    const stackLabels = await loadUint32Blob(r.explorer_stack_label_bin_url);
    if (!stackLabels) return null;

    // Per stack id -> canonical recipe key + raw color entries (for readout
    // and identity contours). Different stack ids with the same physical
    // recipe intentionally share one canonical key.
    const stackKeyById = r.explorer_stack_table.map((stack) => _recipeKeyFromEntries(stack));
    const stackEntriesById = r.explorer_stack_table.map((stack) =>
      (Array.isArray(stack) ? stack : [])
        .map((e) => ({
          filamentId: String(e?.filament_id ?? e?.filamentId ?? ""),
          thicknessMm: Number(e?.thickness_mm ?? e?.thicknessMm ?? 0) || 0,
          bandIndex: e?.band_index ?? e?.bandIndex ?? null,
          materialRole: String(e?.material_role ?? e?.materialRole ?? ""),
        }))
        .filter((e) => e.filamentId && e.thicknessMm > 1e-9)
    );
    const recipeIdentityMap = buildRecipeIdentityMap(stackLabels, stackKeyById);
    const recipeBoundaries = buildDiscreteLabelBoundaryMask(recipeIdentityMap);
    return {
      cookbook: null,
      stackLabels,
      stackKeyById,
      stackEntriesById,
      recipeIdentityMap,
      recipeBoundaries,
    };
  })();

  recipeDataPromiseCache[run.id] = pending;
  try {
    const data = await pending;
    if (!data || (recipeDataGeneration[run.id] || 0) !== generation) return null;
    recipeDataCache[run.id] = data;
    return data;
  } finally {
    if (recipeDataPromiseCache[run.id] === pending) delete recipeDataPromiseCache[run.id];
  }
}

async function ensureRecipeCookbook(run, recipeData) {
  if (!run?.results || !recipeData) return null;
  if (recipeData.cookbook) return recipeData.cookbook;
  if (recipeCookbookPromiseCache[run.id]) return recipeCookbookPromiseCache[run.id];
  const url = run.results.color_recipe_breakdown_cookbook_url;
  if (!url) return null;
  const generation = recipeDataGeneration[run.id] || 0;
  const pending = (async () => {
    try {
      const resp = await fetch(url);
      return resp.ok ? await resp.json() : null;
    } catch {
      return null;
    }
  })();
  recipeCookbookPromiseCache[run.id] = pending;
  try {
    const cookbook = await pending;
    if (cookbook && (recipeDataGeneration[run.id] || 0) === generation) {
      recipeData.cookbook = cookbook;
    }
    return cookbook;
  } finally {
    if (recipeCookbookPromiseCache[run.id] === pending) delete recipeCookbookPromiseCache[run.id];
  }
}

async function ensureRecipeData(run) {
  const recipeData = await ensureRecipeArtifactData(run);
  if (!recipeData) return null;
  await ensureRecipeCookbook(run, recipeData);
  return recipeData;
}

function recipePct(fraction) {
  const pct = (Number(fraction) || 0) * 100;
  if (pct > 0 && pct < 0.1) return "<0.1%";
  return `${pct.toFixed(1)}%`;
}

function recipeFilamentLabel(fid) {
  const fil = filamentById(fid);
  return fil?.color_name || fil?.display_name || fid;
}

function recipeFilamentChip(fid) {
  const fil = filamentById(fid);
  const hex = fil?.hex || "#888";
  return `<span class="recipe-fil-chip" style="background:${hex}" title="${esc(recipeFilamentLabel(fid))}"></span>`;
}

// Collect every canonical recipe key that lives under a cookbook node, so a
// selection at any depth highlights the union of its regions.
function recipeKeysForNode(node) {
  const keys = new Set();
  if (!node) return keys;
  if (node.kind === "recipe") {
    keys.add(_recipeKeyFromEntries(node.data.recipe));
  } else if (node.kind === "combo") {
    (node.data.recipes || []).forEach((rec) => keys.add(_recipeKeyFromEntries(rec.recipe)));
  } else if (node.kind === "family") {
    if (node.data.n_colors === 0) {
      keys.add("base-only");
    } else {
      (node.data.combos || []).forEach((combo) =>
        (combo.recipes || []).forEach((rec) => keys.add(_recipeKeyFromEntries(rec.recipe))),
      );
    }
  }
  return keys;
}

// Build the tree panel HTML from the cookbook. Each node carries a data-node-id
// that indexes into a flat registry so click handlers can resolve the node.
// The navigable hierarchy: family -> combo ONLY. The specific thickness recipes
// (the noisy long tail) are NOT inlined here — they live in the attached bucket
// (renderRecipeBucket), which fills when a family/combo is selected. Each node's
// data-node-id is its flat index into `registry`; combos/families record the
// registry ids of their recipes so the bucket can list them.
function renderRecipeTree(cookbook, registry) {
  const families = cookbook?.families || [];
  if (!families.length) return `<p class="recipe-tree-empty muted-line">No color recipes in this solve.</p>`;
  const lines = [];
  families.forEach((family) => {
    const famNode = { kind: "family", data: family, recipeIds: [] };
    const famIdx = registry.length;
    registry.push(famNode);
    if (family.n_colors === 0) {
      // Base-only collapses: family == combo == recipe. Register its single
      // recipe so the bucket can show it; the tree node itself is a leaf.
      const recIdx = registry.length;
      registry.push({ kind: "recipe", data: {
        recipe: [], recipe_key: "base-only",
        area_fraction: family.area_fraction, pixel_count: family.pixel_count,
      } });
      famNode.recipeIds.push(recIdx);
      lines.push(`<div class="recipe-node recipe-node-family" data-node-id="${famIdx}" tabindex="0" role="button">
        <span class="recipe-node-label">Base only</span>
        <span class="recipe-node-pct">${recipePct(family.area_fraction)}</span>
      </div>`);
      return;
    }
    lines.push(`<div class="recipe-node recipe-node-family" data-node-id="${famIdx}" tabindex="0" role="button">
      <span class="recipe-node-label">${family.n_colors}-color</span>
      <span class="recipe-node-pct">${recipePct(family.area_fraction)}</span>
    </div>`);
    (family.combos || []).forEach((combo) => {
      const comboNode = { kind: "combo", data: combo, recipeIds: [] };
      const comboIdx = registry.length;
      registry.push(comboNode);
      (combo.recipes || []).forEach((rec) => {
        const recIdx = registry.length;
        registry.push({ kind: "recipe", data: rec });
        comboNode.recipeIds.push(recIdx);
        famNode.recipeIds.push(recIdx);
      });
      const comboChips = (combo.filaments || []).map(recipeFilamentChip).join("");
      const comboName = (combo.filaments || []).map(recipeFilamentLabel).join(" + ");
      const nRecipes = (combo.recipes || []).length;
      lines.push(`<div class="recipe-node recipe-node-combo" data-node-id="${comboIdx}" tabindex="0" role="button">
        <span class="recipe-node-label">${comboChips}<span class="recipe-node-name">${esc(comboName)}</span></span>
        <span class="recipe-node-count" title="${nRecipes} recipe${nRecipes === 1 ? "" : "s"}">${nRecipes}</span>
        <span class="recipe-node-pct">${recipePct(combo.area_fraction)}</span>
      </div>`);
    });
  });
  return lines.join("");
}

// Decide how many of the (area_fraction-desc-sorted) recipe rows to show
// outright vs collapse into an expandable "<0.1%" tail. Recipes carrying real
// area (>= threshold of the whole image) always show; the rest collapse. A
// minimum always shows so a combo of all-tiny recipes is never an empty table,
// and a tail of fewer than two rows is shown inline (no point hiding one row).
// Expects `fractions` pre-sorted descending (the bucket sorts before calling).
function partitionRecipeTail(fractions, opts) {
  const threshold = opts && opts.threshold != null ? opts.threshold : 0.001;
  const minVisible = opts && opts.minVisible != null ? opts.minVisible : 3;
  const n = fractions.length;
  let above = 0;
  for (let i = 0; i < n; i++) if ((fractions[i] || 0) >= threshold) above++;
  let visible = Math.max(above, Math.min(minVisible, n));
  let tailCount = n - visible;
  if (tailCount < 2) { visible = n; tailCount = 0; }
  let tailFraction = 0;
  for (let i = visible; i < n; i++) tailFraction += (fractions[i] || 0);
  return { visible, tailCount, tailFraction };
}

// The attached recipe bucket, rendered as a compact TABLE. Every recipe under a
// node shares the same filament set (or, for a family, a small union), so the
// filament names are shown ONCE as the column-header chips and each recipe row
// lists only its thicknesses (no repeated names, no per-cell units — the unit is
// stated once in the caption). Sorted by image coverage so the dominant recipes
// lead. The sub-0.1% long tail collapses behind one expandable row. Each row
// highlights only its own regions on click.
function renderRecipeBucket(node, registry) {
  if (!node || (node.kind !== "family" && node.kind !== "combo")) {
    return `<div class="recipe-bucket-empty muted-line">Select a family or combo to list its recipes.</div>`;
  }
  const ids = (node.recipeIds || []).slice().sort(
    (a, b) => (registry[b].data.area_fraction || 0) - (registry[a].data.area_fraction || 0),
  );
  if (!ids.length) return `<div class="recipe-bucket-empty muted-line">No recipes.</div>`;

  // Column filaments = the filaments used across these recipes, canonical order.
  // A combo shares one set; a family unions its combos' (rows leave "—" gaps).
  const cols = [];
  const seen = new Set();
  ids.forEach((idx) => (registry[idx].data.recipe || []).forEach((e) => {
    if (!seen.has(e.filament_id)) { seen.add(e.filament_id); cols.push(e.filament_id); }
  }));
  cols.sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));

  if (!cols.length) {
    // 0-color / base-only: nothing to tabulate.
    const idx = ids[0];
    return `<div class="recipe-bucket-table"><div class="recipe-node recipe-bucket-row" data-node-id="${idx}" tabindex="0" role="button">
      <span class="recipe-bucket-cell recipe-bucket-cell-name">Base only</span>
      <span class="recipe-bucket-cell recipe-bucket-pct">${recipePct(registry[idx].data.area_fraction)}</span>
    </div></div>`;
  }

  const head = `<div class="recipe-bucket-head">
    ${cols.map((fid) => `<span class="recipe-bucket-cell recipe-bucket-hcell">${recipeFilamentChip(fid)}</span>`).join("")}
    <span class="recipe-bucket-cell recipe-bucket-pct">%</span>
  </div>`;
  const renderRow = (idx, extraCls) => {
    const rec = registry[idx].data;
    const byFid = {};
    (rec.recipe || []).forEach((e) => { byFid[e.filament_id] = e.thickness_mm; });
    const cells = cols.map((fid) =>
      `<span class="recipe-bucket-cell">${byFid[fid] != null ? Number(byFid[fid]).toFixed(2) : "—"}</span>`,
    ).join("");
    return `<div class="recipe-node recipe-bucket-row${extraCls}" data-node-id="${idx}" tabindex="0" role="button">
      ${cells}<span class="recipe-bucket-cell recipe-bucket-pct">${recipePct(rec.area_fraction)}</span>
    </div>`;
  };

  const fractions = ids.map((idx) => registry[idx].data.area_fraction || 0);
  const { visible, tailCount, tailFraction } = partitionRecipeTail(fractions);
  const headRows = ids.slice(0, visible).map((idx) => renderRow(idx, "")).join("");
  let tailHtml = "";
  if (tailCount > 0) {
    const tailRows = ids.slice(visible).map((idx) => renderRow(idx, " recipe-bucket-tail-row")).join("");
    tailHtml = `<div class="recipe-bucket-tail-toggle" data-tail-count="${tailCount}" tabindex="0" role="button" aria-expanded="false">
      <span class="recipe-bucket-tail-chev" aria-hidden="true">▸</span>
      <span class="recipe-bucket-tail-label">+${tailCount} more under 0.1%</span>
      <span class="recipe-bucket-cell recipe-bucket-pct">${recipePct(tailFraction)}</span>
    </div>${tailRows}`;
  }

  return `<div class="recipe-bucket-table">
    <div class="recipe-bucket-caption muted-line">thickness · mm</div>
    ${head}${headRows}${tailHtml}
  </div>`;
}

// Paint the highlight overlay: dim non-selected pixels, leave selected regions
// at full clarity with a tint, so the selected union pops on the color image.
function highlightRecipeRegions(canvas, recipeData, selectedKeys) {
  if (!canvas || !recipeData) return;
  const { stackLabels, stackKeyById } = recipeData;
  const { width, height, data } = stackLabels;
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext("2d");
  const img = ctx.createImageData(width, height);
  const px = img.data;
  const hasSelection = selectedKeys && selectedKeys.size > 0;
  for (let i = 0; i < data.length; i++) {
    const off = i * 4;
    const key = stackKeyById[data[i]];
    const selected = hasSelection && key !== undefined && selectedKeys.has(key);
    if (!hasSelection) {
      px[off + 3] = 0; // nothing selected: fully transparent overlay
    } else if (selected) {
      // Cyan tint marks the selected union.
      px[off] = 0; px[off + 1] = 220; px[off + 2] = 255; px[off + 3] = 90;
    } else {
      // Dim everything else so the selection reads clearly.
      px[off] = 0; px[off + 1] = 0; px[off + 2] = 0; px[off + 3] = 150;
    }
  }
  ctx.putImageData(img, 0, 0);
  canvas.style.display = "block";
}

function recipeReadoutHtml(entries) {
  if (!entries || !entries.length) {
    return `<div class="recipe-region-readout-empty">Base only — no color filament here.</div>`;
  }
  const rows = entries
    .map((e) => {
      const band = e.bandIndex == null ? "" : `Band ${Number(e.bandIndex) + 1} · `;
      const fill = e.materialRole === "white_fill" ? "White fill · " : "";
      return `<div class="recipe-readout-row">${recipeFilamentChip(e.filamentId)}<span class="recipe-readout-name">${esc(`${band}${fill}${recipeFilamentLabel(e.filamentId)}`)}</span><span class="recipe-readout-th">${Number(e.thicknessMm).toFixed(2)} mm</span></div>`;
    })
    .join("");
  return rows;
}

async function openRecipeLightbox(runId) {
  const lb = $("#compLightbox");
  const content = $("#compLightboxContent");
  if (!lb || !content) return;
  const run = solveRuns.find((r) => r.id === runId);
  if (!run || !run.results) return;
  const lifecycle = beginLightboxLifecycle();
  _solveLightboxState = { kind: "recipe", runId };

  const recipeData = await ensureRecipeData(run);
  if (!lifecycle.isActive()) return;
  const imgUrl = _getSolveRunResultUrl(run.results, "recipe_regions") || "";
  const recipeBoundariesAvailable = Boolean(recipeData?.recipeBoundaries);
  const registry = [];
  const treeHtml = recipeData?.cookbook
    ? renderRecipeTree(recipeData.cookbook, registry)
    : `<p class="recipe-tree-empty muted-line">Recipe data unavailable for this run.</p>`;

  content.innerHTML = `
    <div class="recipe-lightbox-wrap">
      <div class="recipe-lightbox-media">
        ${buildSolveLightboxHeader(run, getSolveLightboxViewLabel("recipe_regions"))}
        <div class="recipe-lightbox-toolbar">
          <button class="sub-toggle-btn${recipeBoundariesAvailable && solveContoursEnabled ? " is-active" : ""}" id="recipeLightboxContoursToggle" type="button" data-contours-available="${recipeBoundariesAvailable ? "true" : "false"}" aria-pressed="${recipeBoundariesAvailable && solveContoursEnabled ? "true" : "false"}" title="${recipeBoundariesAvailable ? "Show recipe boundaries on the image" : "Recipe boundaries are unavailable for this older run"}"${recipeBoundariesAvailable ? "" : " disabled aria-disabled=\"true\""}>Contours</button>
        </div>
        <div class="recipe-lightbox-frame comp-lightbox-media">
          <img class="comp-lightbox-img recipe-lightbox-img" src="${esc(imgUrl)}" style="image-rendering:pixelated;">
          <canvas class="recipe-lightbox-contour-canvas solve-lightbox-contour-canvas" aria-label="${escAttr(run.label)} recipe boundaries"></canvas>
          <canvas class="recipe-lightbox-highlight-canvas" aria-hidden="true"></canvas>
          <div class="recipe-region-readout" id="recipeRegionReadoutPanel" data-anchor="top-right">
            <div class="recipe-region-readout-title">Region Recipe</div>
            <div class="recipe-region-readout-body" id="recipeRegionReadout">
              <div class="recipe-region-readout-empty">Hover a highlighted region for its thicknesses.</div>
            </div>
          </div>
        </div>
      </div>
      <aside class="recipe-lightbox-panel">
        <div class="recipe-panel-cols">
          <div class="recipe-panel-col recipe-hierarchy-col">
            <div class="recipe-panel-title">Recipe taxonomy <span class="recipe-panel-hint">select to highlight</span></div>
            <div class="recipe-tree" id="recipeTree">${treeHtml}</div>
          </div>
          <div class="recipe-panel-col recipe-bucket-col">
            <div class="recipe-panel-title">Recipes <span class="recipe-panel-hint" id="recipeBucketHint"></span></div>
            <div class="recipe-bucket" id="recipeBucket"><div class="recipe-bucket-empty muted-line">Select a family or combo to list its recipes.</div></div>
          </div>
        </div>
      </aside>
    </div>`;
  lb.classList.remove("is-hidden");

  const wrap = content.querySelector(".recipe-lightbox-wrap");
  if (wrap) {
    wrap.addEventListener("click", (e) => {
      // Clicks on the image, side panel, header, or toolbar keep the lightbox
      // open; clicking the empty matte around them closes it.
      if (e.target.closest(".recipe-lightbox-panel, .recipe-lightbox-img, .recipe-region-readout, .comp-lightbox-topbar, .recipe-lightbox-toolbar")) {
        e.stopPropagation();
        return;
      }
      closeCompLightbox();
    });
  }

  if (!recipeData) {
    return;
  }

  const tree = content.querySelector("#recipeTree");
  const bucket = content.querySelector("#recipeBucket");
  const bucketHint = content.querySelector("#recipeBucketHint");
  const canvas = content.querySelector(".recipe-lightbox-highlight-canvas");
  const imgEl = content.querySelector(".recipe-lightbox-img");
  const frame = content.querySelector(".recipe-lightbox-frame");
  const readoutPanel = content.querySelector("#recipeRegionReadoutPanel");
  const readout = content.querySelector("#recipeRegionReadout");
  let selectedKeys = null;

  function sizeCanvasToImage() {
    if (!imgEl || !canvas) return;
    const w = imgEl.clientWidth, h = imgEl.clientHeight;
    if (!w || !h) return;
    canvas.style.width = `${w}px`;
    canvas.style.height = `${h}px`;
  }

  // Upscale the (low-resolution, pixelated) region image to fill its available
  // area, preserving aspect. The frame fills the space left of the panel; the
  // image is sized to the contain-fit of that frame and the highlight canvas is
  // synced to match, so the side panel can never crunch the enlarged image.
  function fitImage() {
    if (!imgEl) return;
    const frame = imgEl.closest(".recipe-lightbox-frame");
    if (!frame) return;
    const natW = imgEl.naturalWidth || (recipeData && recipeData.stackLabels.width) || 0;
    const natH = imgEl.naturalHeight || (recipeData && recipeData.stackLabels.height) || 0;
    if (!natW || !natH) return;
    const availW = frame.clientWidth, availH = frame.clientHeight;
    if (availW <= 0 || availH <= 0) return;
    const scale = Math.min(availW / natW, availH / natH);
    imgEl.style.width = `${Math.max(1, Math.floor(natW * scale))}px`;
    imgEl.style.height = `${Math.max(1, Math.floor(natH * scale))}px`;
    sizeCanvasToImage();
  }

  function applySelection(node, nodeEl) {
    if (nodeEl?.classList.contains("is-active")) {
      selectedKeys = null;
      content.querySelectorAll(".recipe-node.is-active").forEach((el) => el.classList.remove("is-active"));
      highlightRecipeRegions(canvas, recipeData, null);
      if (bucket) bucket.innerHTML = `<div class="recipe-bucket-empty muted-line">Select a family or combo to list its recipes.</div>`;
      if (bucketHint) bucketHint.textContent = "";
      if (readout) readout.innerHTML = `<div class="recipe-region-readout-empty">Hover a region for its thicknesses.</div>`;
      sizeCanvasToImage();
      return;
    }
    selectedKeys = recipeKeysForNode(node);
    content.querySelectorAll(".recipe-node.is-active").forEach((el) => el.classList.remove("is-active"));
    if (nodeEl) nodeEl.classList.add("is-active");
    highlightRecipeRegions(canvas, recipeData, selectedKeys);
    sizeCanvasToImage();
    // A family/combo selection refills the bucket with its recipes; clicking a
    // recipe IN the bucket leaves the bucket as-is and just re-highlights.
    if (node.kind === "family" || node.kind === "combo") {
      if (bucket) bucket.innerHTML = renderRecipeBucket(node, registry);
      if (bucketHint) {
        bucketHint.textContent = node.kind === "family"
          ? (node.data.n_colors === 0 ? "Base only" : `${node.data.n_colors}-color`)
          : (node.data.filaments || []).map(recipeFilamentLabel).join(" + ");
      }
    }
  }

  // One handler for both panes — every node carries its registry index.
  const onNodeClick = (e) => {
    const nodeEl = e.target.closest(".recipe-node[data-node-id]");
    if (!nodeEl) return;
    const node = registry[parseInt(nodeEl.dataset.nodeId, 10)];
    if (node) applySelection(node, nodeEl);
  };
  tree.addEventListener("click", onNodeClick);
  const onBucketClick = (e) => {
    // The sub-0.1% tail toggle reveals/hides the collapsed rows; everything
    // else in the bucket is a recipe node that highlights its regions.
    const toggle = e.target.closest(".recipe-bucket-tail-toggle");
    if (toggle) {
      const table = toggle.closest(".recipe-bucket-table");
      if (table) {
        const expanded = table.classList.toggle("tail-expanded");
        toggle.setAttribute("aria-expanded", expanded ? "true" : "false");
        const count = toggle.dataset.tailCount;
        const label = toggle.querySelector(".recipe-bucket-tail-label");
        if (label) label.textContent = expanded ? `Hide ${count} under 0.1%` : `+${count} more under 0.1%`;
      }
      return;
    }
    onNodeClick(e);
  };
  if (bucket) bucket.addEventListener("click", onBucketClick);

  function updateRecipeReadoutAnchor(e) {
    if (!readoutPanel || !frame) return;
    const anchor = readoutPanel.dataset.anchor === "top-left" ? "top-left" : "top-right";
    const panelRect = readoutPanel.getBoundingClientRect();
    const pad = 10;
    const nearPanel = (
      e.clientX >= panelRect.left - pad &&
      e.clientX <= panelRect.right + pad &&
      e.clientY >= panelRect.top - pad &&
      e.clientY <= panelRect.bottom + pad
    );
    if (nearPanel) {
      readoutPanel.dataset.anchor = anchor === "top-left" ? "top-right" : "top-left";
    }
  }

  // Hover-to-peek: map the pointer to a pixel, read that region's thicknesses.
  const onFrameMove = (e) => {
    updateRecipeReadoutAnchor(e);
    if (!imgEl) return;
    const rect = imgEl.getBoundingClientRect();
    const w = recipeData.stackLabels.width, h = recipeData.stackLabels.height;
    const px = Math.floor(((e.clientX - rect.left) / rect.width) * w);
    const py = Math.floor(((e.clientY - rect.top) / rect.height) * h);
    if (px < 0 || py < 0 || px >= w || py >= h) return;
    const stackId = recipeData.stackLabels.data[py * w + px];
    const key = recipeData.stackKeyById[stackId];
    // Only read out regions that are part of the current selection (or any
    // region when nothing is selected yet). Hovering outside the selection
    // clears the readout rather than leaving a stale region's thicknesses.
    if (selectedKeys && selectedKeys.size > 0 && !selectedKeys.has(key)) {
      if (readout) readout.innerHTML = `<div class="recipe-region-readout-empty">Hover a highlighted region for its thicknesses.</div>`;
      return;
    }
    const entries = recipeData.stackEntriesById[stackId] || [];
    if (readout) readout.innerHTML = recipeReadoutHtml(entries);
  };
  const onFrameLeave = () => {
    if (readoutPanel) readoutPanel.dataset.anchor = "top-right";
  };
  if (frame) {
    frame.addEventListener("mousemove", onFrameMove);
    frame.addEventListener("mouseleave", onFrameLeave);
  }

  // Keep the image fitted AND the contour overlay positioned over it.
  const relayout = () => {
    fitImage();
    renderSolveLightboxContours(run, "recipe_regions");
  };
  const onResize = () => relayout();
  window.addEventListener("resize", onResize);
  if (imgEl) {
    if (imgEl.complete && imgEl.naturalWidth) relayout();
    else imgEl.addEventListener("load", relayout, { once: true });
  }
  relayout();

  // Contours toggle: shares the global solveContoursEnabled state with the main
  // Color Regions toggle, so flipping either keeps both (and the grid) in sync.
  const contoursBtn = content.querySelector("#recipeLightboxContoursToggle");
  if (contoursBtn && !contoursBtn.disabled) {
    syncRecipeLightboxContoursToggle();
    contoursBtn.addEventListener("click", () => {
      solveContoursEnabled = !solveContoursEnabled;
      updateSolveSubControls();    // re-syncs the main #solveContoursToggle button
      updateSolveColumnImages();   // keep the (hidden) grid correct for when this closes
      updateSolveLegend();
      renderSolveLightboxContours(run, "recipe_regions");
      syncRecipeLightboxContoursToggle();
    });
  }

  lifecycle.addCleanup(() => {
    tree.removeEventListener("click", onNodeClick);
    if (bucket) bucket.removeEventListener("click", onBucketClick);
    if (frame) {
      frame.removeEventListener("mousemove", onFrameMove);
      frame.removeEventListener("mouseleave", onFrameLeave);
    }
    window.removeEventListener("resize", onResize);
  });
}

// Map a filament id to its display label for user-facing messages.
function solveFilamentLabel(fid) {
  const f = allFilaments.find((x) => x.filament_id === fid);
  return f?.color_name || f?.display_name || fid;
}

// Build a clear message for a palette that can't be solved with: filaments
// excluded from the active color model, or with no calibration profile yet.
// A filament that is both excluded AND profile-less is reported once, as
// excluded — the actionable reason.
function buildUnsolvablePaletteMessage(check) {
  const unavailable = check?.unavailable || [];
  const missing = (check?.missing || []).filter((fid) => !unavailable.includes(fid));
  const parts = [];
  if (unavailable.length) {
    const names = unavailable.map(solveFilamentLabel).join(", ");
    const one = unavailable.length === 1;
    parts.push(`${names} ${one ? "is" : "are"} excluded from the current color model — remove ${one ? "it" : "them"} from the palette, or re-include and re-fit in calibration.`);
  }
  if (missing.length) {
    const names = missing.map(solveFilamentLabel).join(", ");
    const one = missing.length === 1;
    parts.push(`${names} ${one ? "has" : "have"} no calibration profile yet — calibrate ${one ? "it" : "them"} before solving.`);
  }
  return `Can't solve. ${parts.join(" ")}`.trim();
}

async function handleStartSolve() {
  if (solveStartPending || solveStatus.status === "running") return;
  solveStartPending = true;
  try {
    updateSolveReadiness();
    if (exportRunning) {
      showToast("Please wait for meshing to finish", "warn");
      return;
    }
    try {
      const exportStatus = await getExportStatus();
      if (["running", "cancelling"].includes(exportStatus?.status)) {
        showToast("Please wait for meshing to finish", "warn");
        return;
      }
    } catch {
      // If the status check fails, continue through the normal solve-start path
      // and let the server return any authoritative error.
    }
    try {
      await syncConfigToServer({ throwOnError: true, showErrorToast: true });
    } catch {
      return;
    }

    const settingsIssues = getSolveSettingsPreflightIssues();
    if (settingsIssues.length) {
      showToast(buildSolveSettingsPreflightMessage(settingsIssues), "error");
      return;
    }

    const palette = getActivePalette();

    const gatingIssues = getPaletteGatingIssues(palette);
    if (paletteGatingIssueCount(gatingIssues)) {
      showToast(buildPaletteGatingMessage(gatingIssues, "Can't solve."), "error");
      return;
    }

    // Preflight: refuse a solve whose palette contains filaments that can't be
    // solved with (excluded from the active model, or uncalibrated) and say so
    // clearly — rather than letting the backend fail mid-solve. Frontend-only:
    // reuses the existing /api/palette/validate precheck endpoint.
    try {
      const check = await apiPost("/palette/validate", { palette });
      if (check && check.valid === false) {
        showToast(buildUnsolvablePaletteMessage(check), "error");
        return;
      }
    } catch {
      // Precheck unavailable — fall through and let the normal solve path run.
    }

    const recipeContext = buildSolveRecipeContext(palette, _currentSettingsSnapshot());
    const run = createSolveRun(palette, { ...config }, recipeContext);
    delete surfaceDataCache[run.id];
    delete explorerMaterialDataCache[run.id];
    solveRuns.push(run);
    selectedRunIds.add(run.id);
    activeSolveRunId = run.id;

    try {
      const started = await startSolve({
        palette,
        runId: run.id,
        profileRef: run.profile_ref,
        profileNameAtSolve: run.profile_name_at_solve,
        isProfileModifiedAtSolve: run.is_profile_modified_at_solve,
        recipeSnapshot: run.recipe_snapshot,
      });
      activeSolveJobId = started?.job_id || null;
      if (!activeSolveJobId) throw new Error("Solve start did not return a job id.");
      solveCancelPending = false;
      resetOperationElapsedSeconds();
      solveStatus = {
        status: "running",
        job_id: activeSolveJobId,
        card_id: run.id,
        progress: "Starting...",
        progress_detail: { overall_pct: 0 },
        elapsed_s: 0,
        result: null,
      };
      // Solve is now a global top-bar action: auto-collapse the settings drawer (close it
      // fully, per the resolved decision) and land on the Preview/results page so the fresh
      // result is visible. switchTab("solve") renders the results page.
      if (settingsDrawerOpen) closeSettingsDrawer();
      switchTab("solve");
      startSolvePolling(run);
    } catch (err) {
      resetSolveRunDeleteConfirm({ render: false });
      solveRuns = solveRuns.filter(r => r.id !== run.id);
      selectedRunIds.delete(run.id);
      if (activeSolveRunId === run.id) activeSolveRunId = null;
      showToast(`Solve failed to start: ${err.message}`, "error");
    }
  } finally {
    solveStartPending = false;
    updateSolveReadiness();
  }
}

async function handleCancelSolve() {
  if (solveCancelPending || solveStatus.status !== "running") return;
  const cancellationJobId = activeSolveJobId;
  if (!cancellationJobId) return;
  solveCancelPending = true;
  renderSolveProgress();
  try {
    const response = await cancelSolve(cancellationJobId);
    if (activeSolveJobId !== cancellationJobId) return;
    if (response?.requested) assertPolledJobIdentity(response, cancellationJobId);
    if (!response?.requested) {
      solveCancelPending = false;
      renderSolveProgress();
      return;
    }
    showToast("Cancellation requested", "warn");
  } catch {
    if (activeSolveJobId !== cancellationJobId) return;
    solveCancelPending = false;
    renderSolveProgress();
    showToast("Could not request cancellation", "error");
  }
}

function startSolvePolling(run) {
  if (solvePollingOwner) solvePollingOwner.cancelled = true;
  const pollingJobId = activeSolveJobId;
  const pollingOwner = { jobId: pollingJobId, cancelled: false };
  solvePollingOwner = pollingOwner;
  void (async () => {
    try {
      const status = await pollJobUntilTerminal({
        jobId: pollingJobId,
        fetchStatus: () => getSolveStatus(),
        isTerminal: (next) => !["running", "cancelling"].includes(next.status),
        shouldContinue: () => (
          !pollingOwner.cancelled
          && solvePollingOwner === pollingOwner
          && activeSolveJobId === pollingJobId
        ),
        intervalMs: 500,
        onStatus: (next) => {
          solveStatus = next;
          renderSolveProgress();
          if (["running", "cancelling"].includes(next.status)) {
            renderSolveRunSidebar();
            updateRail();
          }
        },
        onTransientError: () => {
          solveStatus = {
            ...solveStatus,
            progress: "Connection interrupted; retrying solve status...",
            progress_detail: { ...(solveStatus.progress_detail || {}), stage_label: "Reconnecting to solve..." },
          };
          renderSolveProgress();
        },
      });
      if (!status || solvePollingOwner !== pollingOwner) return;
      solveCancelPending = false;
      if (status.status === "complete" && status.result) {
        run.results = status.result;
        run.solve_elapsed_s = Number.isFinite(Number(status.elapsed_s)) ? Math.max(0, Number(status.elapsed_s)) : null;
        if (activeSolveRunId === run.id) activeSolveRunId = null;
      } else if (status.status === "cancelled") {
        removePendingSolveRun(run.id);
        if (activeSolveRunId === run.id) activeSolveRunId = null;
      } else if (status.status === "error") {
        if (activeSolveRunId === run.id) activeSolveRunId = null;
      }
      activeSolveJobId = null;
      renderSolveTab();
      updateRail();
      if (status.status === "complete") {
        showToast("Solve complete!", "success");
      } else if (status.status === "error") {
        showToast(`Solve error: ${status.progress}`, "error");
      }
    } catch (err) {
      if (solvePollingOwner !== pollingOwner) return;
      console.warn("[solve] polling error:", err.message);
      showToast(`Solve status could not be verified: ${err.message}`, "error");
    } finally {
      if (solvePollingOwner === pollingOwner) solvePollingOwner = null;
    }
  })();
}

  // ── Export Tab ────────────────────────────────────────────────────────────────

  function getCompletedExportRuns() {
    return solveRuns.filter((run) => run.results && (run.results.card_id || run.id));
  }

  function getExportSelectedRun() {
    return solveRuns.find((run) => run.id === exportSelectedRunId && run.results && (run.results.card_id || run.id)) || null;
  }

  function ensureSolveRunExportState(run) {
    if (!run || typeof run !== "object") return null;
    if (!Array.isArray(run.exportRecords)) run.exportRecords = [];
    if (typeof run.selectedExportId !== "string") run.selectedExportId = null;
    return run;
  }

  function getRunExportRecords(run) {
    return ensureSolveRunExportState(run)?.exportRecords || [];
  }

  function createExportRecord(result, completedAt = Date.now(), durationSeconds = null) {
    if (!result || typeof result !== "object") {
      throw new Error("Completed export did not include a result.");
    }
    const exportId = String(result.export_id || "").trim();
    const outputFormat = String(result.output_format || "").trim();
    const geometrySource = String(result.geometry_source || "").trim();
    const fieldScale = Number(result.field_scale);
    if (!exportId || !outputFormat || !geometrySource || !Number.isFinite(fieldScale) || fieldScale <= 0) {
      throw new Error("Completed export response is missing its canonical identity or settings.");
    }
    return {
      id: exportId,
      completedAt: Number.isFinite(Number(completedAt)) ? Number(completedAt) : Date.now(),
      durationSeconds: Number.isFinite(Number(durationSeconds)) ? Math.max(0, Number(durationSeconds)) : null,
      outputFormat,
      geometrySource,
      fieldScale,
      result: _cloneValue(result),
      swapPlan: result.swap_plan && typeof result.swap_plan === "object"
        ? _cloneValue(result.swap_plan)
        : null,
    };
  }

  function appendExportRecordToRun(run, result, completedAt = Date.now(), durationSeconds = null) {
    const state = ensureSolveRunExportState(run);
    if (!state) throw new Error("The solve run for this export is no longer available.");
    const record = createExportRecord(result, completedAt, durationSeconds);
    state.exportRecords.push(record);
    state.selectedExportId = record.id;
    return record;
  }

  function selectRunExportRecord(run, exportId) {
    const state = ensureSolveRunExportState(run);
    if (!state) return null;
    const record = state.exportRecords.find((candidate) => candidate.id === exportId) || null;
    if (record) state.selectedExportId = record.id;
    return record;
  }

  function getSelectedExportRecord(run = getExportSelectedRun()) {
    const state = ensureSolveRunExportState(run);
    if (!state || !state.exportRecords.length) return null;
    const selected = state.exportRecords.find((record) => record.id === state.selectedExportId)
      || state.exportRecords[state.exportRecords.length - 1];
    state.selectedExportId = selected.id;
    return selected;
  }

  function getSelectedExportResult() {
    return getSelectedExportRecord()?.result || null;
  }

  function getSelectedSwapInstructions() {
    return getSelectedExportRecord()?.swapPlan?.instructions || "";
  }

  function updateExportFieldScaleState() {
    const sourceEl = $("#exportGeometrySource");
    const scaleEl = $("#exportFieldScale");
    if (!sourceEl || !scaleEl) return;
    const outputEl = $("#exportOutputFormat");
    sourceEl.disabled = exportRunning;
    if (outputEl) outputEl.disabled = exportRunning;
    const disabled = exportRunning || sourceEl.value !== "field_derived";
    scaleEl.disabled = disabled;
    scaleEl.title = exportRunning
      ? "Export settings are locked until the active export finishes."
      : disabled
      ? "Exact solved raster export uses the solve grid directly."
      : "Controls the white-cap field reconstruction detail.";
  }

  function formatExportGeometrySourceLabel(value) {
    return value === "exact_raster" ? "Exact solved raster" : "Field-derived white cap";
  }

  function formatExportOutputFormatLabel(value) {
    return value === "3mf" ? "single 3MF" : "individual STLs";
  }

  function formatExportFieldScaleLabel(value) {
    const scale = parseInt(value || "4", 10) || 4;
    return `${scale}x`;
  }

  function getExportSolvePreviewUrl(run) {
    const result = run?.results || {};
    return result.predicted_appearance_url
      || result.predicted_url
      || result.predicted_color_only_appearance_url
      || result.source_url
      || "";
  }

  function getExportSolveDimensions(run) {
    const result = run?.results || {};
    const imageWidthPx = Number(result.image_w);
    const imageHeightPx = Number(result.image_h);
    const imageWidthMm = Number(result.image_domain_width_mm);
    const imageHeightMm = Number(result.image_domain_height_mm);
    const configuredBorderWidthMm = Number(run?.config?.border_width_mm);
    const borderEnabled = Boolean(run?.config?.border)
      && Number.isFinite(configuredBorderWidthMm)
      && configuredBorderWidthMm > 0;
    const hasPixels = Number.isFinite(imageWidthPx) && imageWidthPx > 0
      && Number.isFinite(imageHeightPx) && imageHeightPx > 0;
    const hasPhysicalSize = Number.isFinite(imageWidthMm) && imageWidthMm > 0
      && Number.isFinite(imageHeightMm) && imageHeightMm > 0;
    const borderWidthMm = borderEnabled ? configuredBorderWidthMm : 0;
    return {
      imageWidthPx: hasPixels ? Math.round(imageWidthPx) : null,
      imageHeightPx: hasPixels ? Math.round(imageHeightPx) : null,
      imageWidthMm: hasPhysicalSize ? imageWidthMm : null,
      imageHeightMm: hasPhysicalSize ? imageHeightMm : null,
      totalWidthMm: hasPhysicalSize ? imageWidthMm + (2 * borderWidthMm) : null,
      totalHeightMm: hasPhysicalSize ? imageHeightMm + (2 * borderWidthMm) : null,
      borderEnabled,
      borderWidthMm,
    };
  }

  function formatExportDimensionMm(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return "";
    return number.toFixed(2).replace(/\.?0+$/, "");
  }

  function renderExportSolvePreview(run) {
    const media = $("#exportSolvePreview .export-solve-preview-media");
    const image = $("#exportSolvePreviewImg");
    const empty = $("#exportSolvePreviewEmpty");
    const caption = $("#exportSolvePreviewCaption");
    const dimensions = $("#exportSolvePreviewDimensions");
    if (!media || !image || !empty || !caption) return;

    image.onload = null;
    image.onerror = null;
    image.hidden = true;
    image.removeAttribute("src");
    image.dataset.previewUrl = "";
    media.style.aspectRatio = run ? _runAspect(run) : "";
    caption.textContent = run ? run.label : "Selected solve";
    if (dimensions) {
      const solved = getExportSolveDimensions(run);
      const imageSizeParts = [];
      if (solved.imageWidthMm !== null) {
        imageSizeParts.push(
          `${formatExportDimensionMm(solved.imageWidthMm)} × ${formatExportDimensionMm(solved.imageHeightMm)} mm`,
        );
      }
      if (solved.imageWidthPx !== null) {
        imageSizeParts.push(`${solved.imageWidthPx} × ${solved.imageHeightPx} px`);
      }
      const footprint = solved.totalWidthMm !== null
        ? `${formatExportDimensionMm(solved.totalWidthMm)} × ${formatExportDimensionMm(solved.totalHeightMm)} mm`
        : "Unavailable";
      const totalFootprintRow = solved.borderEnabled
        ? `<span><strong>Total footprint</strong>${footprint} · includes ${formatExportDimensionMm(solved.borderWidthMm)} mm border</span>`
        : "";
      dimensions.innerHTML = `
        <span><strong>Image area</strong>${imageSizeParts.join(" · ") || "Unavailable"}</span>
        ${totalFootprintRow}
      `;
    }

    const url = getExportSolvePreviewUrl(run);
    if (!url) {
      empty.hidden = false;
      empty.textContent = run ? "Preview unavailable for this solved run" : "Select a solved run to preview it";
      return;
    }

    empty.hidden = false;
    empty.textContent = "Loading solve preview...";
    image.alt = `${run.label} solve preview`;
    image.dataset.previewUrl = url;
    image.onload = () => {
      if (image.dataset.previewUrl !== url) return;
      image.hidden = false;
      empty.hidden = true;
    };
    image.onerror = () => {
      if (image.dataset.previewUrl !== url) return;
      image.hidden = true;
      empty.hidden = false;
      empty.textContent = "Preview could not be loaded";
    };
    image.src = url;
  }

  function formatExportRecordTime(completedAt) {
    const date = new Date(Number(completedAt));
    if (!Number.isFinite(date.getTime())) return "Time unavailable";
    return date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" });
  }

  function renderExportRecordSelector(run, selectedRecord = getSelectedExportRecord(run)) {
    const container = $("#exportRecordList");
    if (!container) return;
    const records = getRunExportRecords(run);
    if (!run || !records.length) {
      container.innerHTML = `<div class="export-record-empty">${run ? "No exports generated for this solve yet" : "Select a solved run to see its exports"}</div>`;
      return;
    }

    container.innerHTML = [...records].reverse().map((record) => {
      const originalIndex = records.indexOf(record);
      const methodLabel = record.geometrySource === "exact_raster" ? "Exact raster" : "Field-derived";
      const formatLabel = record.outputFormat === "3mf" ? "3MF" : "STLs";
      const detailBadge = record.geometrySource === "field_derived"
        ? `<span class="export-record-badge">${esc(formatExportFieldScaleLabel(record.fieldScale))} detail</span>`
        : "";
      return `
        <button class="export-record-card ${record.id === selectedRecord?.id ? "is-selected" : ""}"
                type="button" data-export-record-id="${esc(record.id)}"
                aria-pressed="${record.id === selectedRecord?.id ? "true" : "false"}">
          <span class="export-record-card-title">
            <span>${esc(run.label)} · Export ${originalIndex + 1}</span>
            <span class="export-record-card-time">${esc(formatExportRecordTime(record.completedAt))}${record.durationSeconds != null ? ` · ${esc(formatDurationSeconds(record.durationSeconds))}` : ""}</span>
          </span>
          <span class="export-record-card-badges">
            <span class="export-record-badge">${esc(formatLabel)}</span>
            <span class="export-record-badge">${esc(methodLabel)}</span>
            ${detailBadge}
          </span>
        </button>
      `;
    }).join("");

    container.querySelectorAll(".export-record-card[data-export-record-id]").forEach((button) => {
      button.addEventListener("click", () => {
        if (!selectRunExportRecord(run, button.dataset.exportRecordId)) return;
        renderExportTab();
        updateRail();
      });
    });
  }

  function describeExportPolicy() {
    const source = $("#exportGeometrySource")?.value || "field_derived";
    const output = $("#exportOutputFormat")?.value || "3mf";
    const fieldScale = $("#exportFieldScale")?.value || "4";
    const sourceLabel = formatExportGeometrySourceLabel(source);
    const outputLabel = formatExportOutputFormatLabel(output);
    const scaleLabel = source === "field_derived"
      ? ` · mesh detail ${formatExportFieldScaleLabel(fieldScale)}`
      : "";
    return `${sourceLabel} · ${outputLabel}${scaleLabel}`;
  }

  function handleExportOptionChange() {
    updateExportFieldScaleState();
    renderExportTab();
    updateRail();
  }

  function ensureExportRunSelection() {
    const completed = getCompletedExportRuns();
    if (!completed.length) {
      exportSelectedRunId = null;
      return null;
    }
    const selected = completed.find((run) => run.id === exportSelectedRunId) || completed[completed.length - 1];
    exportSelectedRunId = selected.id;
    getSelectedExportRecord(selected);
    return selected;
  }

  function renderExportRunSidebar() {
    const container = $("#exportRunCards");
    if (!container) return;
    hideSolveRunHoverPreview();

    const completed = getCompletedExportRuns();
    if (!completed.length) {
      container.innerHTML = `<p class="muted-line" id="exportRunEmpty">No completed solves yet</p>`;
      return;
    }

    let html = "";
    for (let i = completed.length - 1; i >= 0; i--) {
      const run = completed[i];
      const isSelected = run.id === exportSelectedRunId;
      const chips = (run.palette || []).map(fid => {
        const fil = allFilaments.find(f => f.filament_id === fid);
        const hex = fil?.hex || "#888";
        return `<span class="comp-deck-chip" style="background:${hex}"></span>`;
      }).join("");
      const solveDuration = formatDurationSeconds(run.solve_elapsed_s ?? run.results?.elapsed_s);
      const stats = `<span class="solve-run-card-rmse">${formatSolveRunCardRmse(run.results)}${solveDuration ? ` · ${esc(solveDuration)}` : ""}</span>`;
      const staleBadge = run.cache_unavailable
        ? `<span class="solve-run-stale-badge">Unavailable after restart</span>`
        : "";
      html += `<div class="solve-run-card ${isSelected ? "is-selected" : ""}" data-export-run-id="${esc(run.id)}" tabindex="0">
        <div class="solve-run-card-header">
          <span class="solve-run-label">${esc(run.label)}${staleBadge}</span>
          <div class="solve-run-card-actions">
            ${buildSolveRunDeleteButton(run)}
          </div>
        </div>
        <div class="comp-deck-card-chips">${chips}</div>
        <div class="solve-run-card-meta">
          <button class="solve-run-settings-btn" data-run-id="${esc(run.id)}" title="View the settings captured for this run">Settings</button>
          ${stats}
        </div>
      </div>`;
    }
    container.innerHTML = html;

    container.querySelectorAll(".solve-run-card[data-export-run-id]").forEach((el) => {
      el.addEventListener("click", (e) => {
        if (e.target.closest(".solve-run-delete-btn")) return;
        if (e.target.closest(".solve-run-settings-btn")) return;
        const runId = el.dataset.exportRunId;
        if (!runId || runId === exportSelectedRunId) return;
        exportSelectedRunId = runId;
        renderExportTab();
      });
    });

    container.querySelectorAll(".solve-run-delete-btn").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        if (btn.disabled) return;
        handleSolveRunDeleteClick(btn.dataset.runId);
      });
    });
    bindSolveRunCardAuxiliaryInteractions(container, "export");
  }

  function renderExportTab() {
    const exportRun = ensureExportRunSelection();
    const exportRecord = getSelectedExportRecord(exportRun);
    const exportResult = exportRecord?.result || null;
    const swapInstructions = exportRecord?.swapPlan?.instructions || "";
    renderExportRunSidebar();
    renderExportSolvePreview(exportRun);
    renderExportRecordSelector(exportRun, exportRecord);

    const canExport = !!exportRun && !exportRun.cache_unavailable && apiConnected && !exportRunning;
    $("#exportFilesBtn").disabled = !canExport;
    updateExportFieldScaleState();
    const downloadAllBtn = $("#downloadAllBtn");
    if (downloadAllBtn && !exportResult?.zip_url) downloadAllBtn.disabled = true;
    const targetLine = $("#exportTargetLine");
    if (targetLine) {
      targetLine.textContent = exportRun?.cache_unavailable
        ? `${exportRun.label} is no longer available after Prisma restarted. Load a saved run or solve it again before exporting.`
        : exportRun
        ? `Export target: ${exportRun.label} · ${describeSolveRunProfile(exportRun).name} · ${describeExportPolicy()}`
        : "Select one completed solve run to export.";
    }
    const copySwapBtn = $("#copySwapBtn");
    if (copySwapBtn) copySwapBtn.disabled = !swapInstructions;

    if (exportResult) renderExportResults();
    else {
      const quality = $("#exportQualityTable");
      const files = $("#exportFileList");
      if (quality) quality.innerHTML = `<span class="muted-line">Select an export to view its mesh report</span>`;
      if (files) files.innerHTML = `<span class="muted-line">Select an export to view its generated files</span>`;
    }
    if (swapInstructions) renderSwapInstructions(swapInstructions);
    else {
      const swapEl = $("#swapInstructions");
      if (swapEl) swapEl.textContent = exportRecord
        ? "No swap instructions were included with this export"
        : exportRun
          ? "Generate print files to create export-specific swap instructions"
          : "Select a solve run to view swap instructions";
    }
  }

function getExportMeshQualityEntries() {
  const quality = getSelectedExportResult()?.manifest?.quality || {};
  return Object.entries(quality).map(([key, value]) => ({
    key,
    quality: value && typeof value === "object" ? value : {},
  }));
}

function exportMeshQualityIssues(entry) {
  const q = entry.quality || {};
  const issues = [];
  const openEdges = Number(q.n_open_edges || 0);
  const pinchEdges = Number(q.n_pinch_edges || 0);
  const faces = Number(q.n_faces || 0);

  if (openEdges > 0 || q.has_holes === true) {
    const openEdgeText = openEdges > 0
      ? `${openEdges.toLocaleString()} open ${openEdges === 1 ? "edge" : "edges"}`
      : "open edges detected";
    issues.push({ severity: "error", text: openEdgeText });
  }
  if (q.is_watertight === false) {
    issues.push({ severity: "error", text: "not watertight" });
  }
  if (q.is_winding_consistent === false) {
    issues.push({ severity: "warn", text: "winding inconsistency" });
  }
  if (pinchEdges > 0) {
    issues.push({ severity: "warn", text: `${pinchEdges.toLocaleString()} pinch ${pinchEdges === 1 ? "edge" : "edges"}` });
  }
  if (Number.isFinite(faces) && faces <= 0) {
    issues.push({ severity: "warn", text: "no mesh faces reported" });
  }
  return issues;
}

function exportMeshQualityLabel(entry) {
  const q = entry.quality || {};
  return q.label || entry.key || "mesh object";
}

function renderExportChecks() {
  const qualityDiv = $("#exportQualityTable");
  const exportResult = getSelectedExportResult();
  if (!qualityDiv || !exportResult) return;

  const entries = getExportMeshQualityEntries();
  if (entries.length === 0) {
    qualityDiv.innerHTML = `
      <div class="export-check-summary is-warn">
        <span class="status-pill warn">Missing</span>
        <span>No per-mesh quality data was found in the export manifest.</span>
      </div>
    `;
    return;
  }

  const checked = entries.map((entry) => ({
    entry,
    issues: exportMeshQualityIssues(entry),
  }));
  const failing = checked.filter(({ issues }) => issues.some(issue => issue.severity === "error"));
  const warnings = checked.filter(({ issues }) => issues.length > 0 && !issues.some(issue => issue.severity === "error"));
  const okCount = checked.length - failing.length - warnings.length;

  if (failing.length === 0 && warnings.length === 0) {
    qualityDiv.innerHTML = `
      <div class="export-check-summary is-ok">
        <span class="status-pill ok">OK</span>
        <span>No mesh problems detected across ${entries.length} ${entries.length === 1 ? "object" : "objects"}.</span>
      </div>
    `;
    return;
  }

  const issueRows = [...failing, ...warnings].map(({ entry, issues }) => {
    const severity = issues.some(issue => issue.severity === "error") ? "error" : "warn";
    const pill = severity === "error"
      ? `<span class="status-pill error">Problem</span>`
      : `<span class="status-pill warn">Warning</span>`;
    return `
      <div class="export-check-row is-${severity}">
        <div class="export-check-row-main">
          <span class="export-check-object">${esc(exportMeshQualityLabel(entry))}</span>
          ${pill}
        </div>
        <div class="export-check-issues">${issues.map(issue => esc(issue.text)).join(" · ")}</div>
      </div>
    `;
  }).join("");
  const summaryPill = failing.length > 0
    ? `<span class="status-pill error">Problem</span>`
    : `<span class="status-pill warn">Warning</span>`;
  const summaryText = failing.length > 0
    ? `${failing.length} ${failing.length === 1 ? "object needs" : "objects need"} attention.`
    : `${warnings.length} ${warnings.length === 1 ? "object has" : "objects have"} non-blocking warnings.`;

  qualityDiv.innerHTML = `
    <div class="export-check-summary is-${failing.length > 0 ? "error" : "warn"}">
      ${summaryPill}
      <span>${summaryText} ${okCount > 0 ? `${okCount} ${okCount === 1 ? "object passed" : "objects passed"}.` : ""}</span>
    </div>
    <div class="export-check-list">${issueRows}</div>
  `;
}

function exportFileSizeMb(f) {
  const mb = Number(f?.size_mb);
  if (Number.isFinite(mb)) return mb;
  return (Number(f?.size_kb) || 0) / 1024;
}

function renderExportResults() {
  const exportRecord = getSelectedExportRecord();
  const exportResult = exportRecord?.result || null;
  if (!exportResult) return;

  const files = exportResult.files || [];
  renderExportChecks();

  const fileListDiv = $("#exportFileList");
  const outDir = exportResult.out_dir || "";
  fileListDiv.innerHTML = `
    <div class="export-files-list">
      <div class="export-outdir-row">
        <span class="muted-line">Output:</span>
        <code class="export-outdir-path" title="${esc(outDir)}">${esc(outDir)}</code>
        <button class="ghost-button xxs open-export-folder-btn" data-export-id="${esc(exportResult.export_id || exportRecord.id || "")}" title="Open the export folder">Open Folder</button>
        <button class="ghost-button xxs copy-path-btn" data-copy-path="${esc(outDir)}" title="Copy folder path">Copy Path</button>
      </div>
      ${files.map((f) => {
        const href = f.url || exportFileUrl(f.name, exportRecord.id);
        const absPath = f.abs_path || (outDir ? `${outDir}\\${f.name}` : f.name);
        return `
        <div class="export-file-row">
          <span class="file-name" title="${esc(f.name)}">${esc(f.name)}</span>
          <span class="file-size">${exportFileSizeMb(f).toFixed(2)} MB</span>
          <div class="export-file-actions">
            <a class="ghost-button xxs export-file-download" href="${esc(href)}" download="${esc(f.name)}" title="Download this file">Download</a>
            <button class="ghost-button xxs copy-path-btn" data-copy-path="${esc(absPath)}" title="Copy file path">Copy Path</button>
          </div>
        </div>
      `;
      }).join("")}
    </div>
  `;

  // Enable Download All when we have files + a zip URL
  const dlAllBtn = $("#downloadAllBtn");
  if (dlAllBtn) {
    const hasZip = files.length > 0 && !!exportResult.zip_url;
    dlAllBtn.disabled = !hasZip;
    dlAllBtn.title = hasZip ? "Download all export files as a ZIP" : "Export files first";
  }
}

async function copyToClipboard(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
    } else {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.left = "-9999px";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
    }
    showToast("Path copied", "success");
  } catch (err) {
    showToast(`Copy failed: ${err.message}`, "error");
  }
}

  function renderSwapInstructions(instructions = getSelectedSwapInstructions()) {
    $("#swapInstructions").textContent = instructions || "No swap instructions available";
  }

  async function handleExportFiles() {
    const btn = $("#exportFilesBtn");
    const exportRun = getExportSelectedRun();
    if (!btn || !exportRun || exportRun.cache_unavailable || exportRunning) return;
    const originatingRunId = exportRun.id;
    const requestedPolicy = Object.freeze({
      geometrySource: $("#exportGeometrySource")?.value || "field_derived",
      fieldScale: parseInt($("#exportFieldScale")?.value || "4", 10) || 4,
      outputFormat: $("#exportOutputFormat")?.value || "3mf",
    });
    const validateWrittenMeshes = false; // Reload-validation control removed (Stage 12b); defaults off
    const cardId = exportRun.results.card_id || exportRun.id;
    if (requestedPolicy.geometrySource === "field_derived" && requestedPolicy.fieldScale > 4) {
      const confirmed = await appConfirm(
        `${requestedPolicy.fieldScale}x mesh detail can take much longer and produce a much larger export file. Continue?`,
        { title: "High Mesh Detail", ok: "Generate", cancel: "Cancel" },
      );
      if (!confirmed) return;
    }

    exportRunning = true;
    activeExportRunId = originatingRunId;
    activeExportJobId = "";
    exportCancelPending = false;
    updateSolveReadiness();
    updateExportFieldScaleState();
    btn.disabled = true;
    btn.textContent = "Exporting...";
    startProgress("Starting export...", "export");

    let pollingOwner = null;
    try {
      const started = await startExportPrintFiles({
        geometrySource: requestedPolicy.geometrySource,
        fieldScale: requestedPolicy.fieldScale,
        outputFormat: requestedPolicy.outputFormat,
        validateWrittenMeshes,
        cardId,
      });
      activeExportJobId = String(started?.job_id || "");
      if (!activeExportJobId) throw new Error("Export did not return a job id.");
      renderExportCancellationState();
      const pollingJobId = activeExportJobId;
      if (exportPollingOwner) exportPollingOwner.cancelled = true;
      pollingOwner = { jobId: pollingJobId, cancelled: false };
      exportPollingOwner = pollingOwner;
      const status = await pollJobUntilTerminal({
        jobId: pollingJobId,
        fetchStatus: () => getExportStatus(),
        isTerminal: (next) => !["running", "cancelling"].includes(next.status),
        shouldContinue: () => (
          exportRunning
          && activeExportJobId === pollingJobId
          && !pollingOwner.cancelled
          && exportPollingOwner === pollingOwner
        ),
        intervalMs: 500,
        onStatus: (next) => updateOperationProgressFromStatus(next, "Exporting files..."),
        onTransientError: () => updateOperationProgressFromStatus(
          { status: "running", progress: "Connection interrupted; retrying export status..." },
          "Exporting files...",
        ),
      });
      if (!status) return;
      if (status.status === "complete" && status.result) {
        const originatingRun = solveRuns.find((run) => run.id === originatingRunId) || null;
        appendExportRecordToRun(originatingRun, status.result, Date.now(), status.elapsed_s);
      } else if (status.status === "cancelled") {
        const cancelled = new Error("Export cancelled");
        cancelled.name = "AbortError";
        throw cancelled;
      } else {
        throw new Error(status.progress || "Export failed");
      }
      showToast("Export complete!", "success");

      renderExportTab();
      updateRail();
    } catch (err) {
      if (err.name === "AbortError") return;
      if (pollingOwner && exportPollingOwner !== pollingOwner) return;
      if (/No cached solve found/i.test(String(err?.message || ""))) {
        const staleRun = solveRuns.find((run) => run.id === originatingRunId);
        if (staleRun) staleRun.cache_unavailable = true;
        showToast("This solve is no longer available after Prisma restarted. Load a saved run or solve it again before exporting.", "warn");
        renderExportTab();
        return;
      }
      const prefix = err.name === "JobPollingIdentityError"
        ? "Export status could not be verified"
        : "Export failed";
      showToast(`${prefix}: ${err.message}`, "error");
    } finally {
      if (pollingOwner && exportPollingOwner !== pollingOwner) return;
      if (exportPollingOwner === pollingOwner) exportPollingOwner = null;
      exportRunning = false;
      if (activeExportRunId === originatingRunId) activeExportRunId = null;
      activeExportJobId = "";
      exportCancelPending = false;
      stopProgress();
      btn.textContent = "Generate Print Files";
      const currentExportRun = getExportSelectedRun();
      btn.disabled = !currentExportRun || currentExportRun.cache_unavailable || !apiConnected;
      updateExportFieldScaleState();
      updateSolveReadiness();
    }
  }

// ── Module Settings Renderer ────────────────────────────────────────────────

let moduleData = [];       // module descriptors from server
let moduleState = {};      // {module_id: true/false}
const MODULE_UI_VISIBLE_SLOTS = new Set(["preprocessing"]);

function modulesForSlot(slot) {
  return (moduleData || []).filter((m) => m.slot === slot);
}

function moduleDisplayName(mod) {
  if (!mod) return "";
  const display = MODULE_DISPLAY[mod.name];
  if (display?.label) return display.label;
  return String(mod.name || "")
    .replace(/^[a-z]\d_/, "")
    .replace(/_/g, " ")
    .replace(/\b\w/g, (ch) => ch.toUpperCase());
}

function moduleDisplayTooltip(mod) {
  if (!mod) return "";
  return MODULE_DISPLAY[mod.name]?.tooltip || mod.description || mod.name || "";
}

function moduleParamStorageKey(moduleId, param) {
  return param?.storage_key || param?.name;
}

function moduleDescriptorById(moduleId) {
  return (moduleData || []).find((mod) => mod.name === moduleId) || null;
}

function getModuleParamValue(configValues, moduleId, param) {
  const module = moduleDescriptorById(moduleId);
  if (module?.slot === "preprocessing") {
    const block = (configValues?.preprocessing_params || {})[moduleId] || {};
    if (Object.prototype.hasOwnProperty.call(block, param.name)) {
      return block[param.name];
    }
    return param.default;
  }
  const storageKey = moduleParamStorageKey(moduleId, param);
  if (Object.prototype.hasOwnProperty.call(configValues || {}, storageKey)) {
    return configValues[storageKey];
  }
  return param.default;
}

function setModuleParamValue(moduleId, param, value) {
  const module = moduleDescriptorById(moduleId);
  if (module?.slot === "preprocessing") {
    if (!config.preprocessing_params || typeof config.preprocessing_params !== "object") {
      config.preprocessing_params = {};
    }
    if (!config.preprocessing_params[moduleId] || typeof config.preprocessing_params[moduleId] !== "object") {
      config.preprocessing_params[moduleId] = {};
    }
    config.preprocessing_params[moduleId][param.name] = value;
    return;
  }
  const storageKey = moduleParamStorageKey(moduleId, param);
  config[storageKey] = value;
}

function projectModuleConfigValues(moduleId, mod, configValues) {
  const projected = { ...(configValues || {}) };
  for (const param of Object.values(mod?.params || {})) {
    projected[param.name] = getModuleParamValue(configValues, moduleId, param);
  }
  return projected;
}

function preprocessingPresetSpec(moduleId) {
  return PREPROCESSING_PRESET_UI[moduleId] || null;
}

function preprocessingParamBlock(moduleId, configValues = config) {
  const block = configValues?.preprocessing_params?.[moduleId];
  return block && typeof block === "object" ? block : null;
}

function preprocessingParamBlockHasValues(moduleId, configValues = config) {
  const block = preprocessingParamBlock(moduleId, configValues);
  return !!block && Object.keys(block).length > 0;
}

function valuesMatchPresetValue(actual, expected) {
  if (typeof expected === "number") {
    const numeric = Number(actual);
    return Number.isFinite(numeric) && Math.abs(numeric - expected) < 1e-9;
  }
  if (typeof expected === "boolean") return Boolean(actual) === expected;
  return String(actual) === String(expected);
}

function preprocessingPresetValuesMatch(moduleId, values, preset) {
  if (!preset?.values) return false;
  return Object.entries(preset.values).every(([key, expected]) => (
    valuesMatchPresetValue(values?.[key], expected)
  ));
}

function currentPreprocessingPresetKey(moduleId, configValues = config) {
  const spec = preprocessingPresetSpec(moduleId);
  if (!spec) return null;
  if (!moduleState[moduleId]) return "off";
  const mod = moduleDescriptorById(moduleId);
  const values = projectModuleConfigValues(moduleId, mod, configValues);
  const match = (spec.presets || []).find((preset) => (
    preset.values && preprocessingPresetValuesMatch(moduleId, values, preset)
  ));
  return match?.key || "custom";
}

function applyPreprocessingPresetValues(moduleId, presetKey) {
  const spec = preprocessingPresetSpec(moduleId);
  const mod = moduleDescriptorById(moduleId);
  if (!spec || !mod) return false;
  const preset = (spec.presets || []).find((entry) => entry.key === presetKey);
  if (!preset?.values) return false;
  const paramsByName = mod.params || {};
  for (const [paramName, value] of Object.entries(preset.values)) {
    const param = paramsByName[paramName];
    if (!param) continue;
    setModuleParamValue(moduleId, param, value);
  }
  return true;
}

function applyDefaultPreprocessingPresetIfNeeded(moduleId) {
  const spec = preprocessingPresetSpec(moduleId);
  if (!spec || preprocessingParamBlockHasValues(moduleId)) return false;
  return applyPreprocessingPresetValues(moduleId, spec.defaultPreset);
}

function refreshPreprocessingPresetSelect(moduleId) {
  const sel = document.getElementById(`mod_${moduleId}_preset`);
  if (!sel) return;
  const key = currentPreprocessingPresetKey(moduleId);
  if (key && sel.value !== key) sel.value = key;
}

function syncConfigFromModuleState() {
  // Module state is authoritative and no longer mirrored into config.
}

function refreshModuleDrivenViews(moduleId = null) {
  if (moduleId) applyShowWhenRules(moduleId);
  if (moduleId) refreshPreprocessingPresetSelect(moduleId);
  updateSettingsSummaries();
  updateDerivedParams();
  updateAccordionSummaries();
  checkPresetModified();
}

function coerceNumberValue(rawValue, fallback, {
  parse = parseFloat,
  min = null,
  max = null,
  integer = false,
} = {}) {
  const parsed = parse(rawValue);
  if (!Number.isFinite(parsed)) {
    return { ok: false, value: fallback };
  }
  let value = integer ? Math.trunc(parsed) : parsed;
  if (min != null && value < Number(min)) value = Number(min);
  if (max != null && value > Number(max)) value = Number(max);
  return { ok: true, value };
}

function coerceNumericParamValue(param, rawValue, fallback) {
  return coerceNumberValue(rawValue, fallback, {
    parse: param.type === "int" ? (value) => parseInt(value, 10) : parseFloat,
    min: param.min,
    max: param.max,
    integer: param.type === "int",
  });
}

/**
 * Render a single param as a settings table row (<tr>).
 */
function renderParamRow(moduleId, param, configValues) {
  const tr = document.createElement("tr");
  const inputId = `mod_${moduleId}_${param.name}`;
  const presetSpec = preprocessingPresetSpec(moduleId);
  const tooltip = presetSpec?.paramTooltips?.[param.name] || param.tooltip || param.description || "";
  const currentValue = configValues[param.name] ?? param.default;
  if (presetSpec) tr.classList.add("advanced-setting", "module-advanced-param-row");

  // Label cell
  const tdLabel = document.createElement("td");
  tdLabel.title = tooltip;
  tdLabel.textContent = presetSpec?.paramLabels?.[param.name] || param.label;
  if (param.min != null && param.max != null) {
    const rangeSpan = document.createElement("span");
    rangeSpan.className = "stg-range";
    rangeSpan.textContent = ` ${param.min}–${param.max}`;
    tdLabel.appendChild(rangeSpan);
  }

  // Value cell
  const tdValue = document.createElement("td");

  if (param.type === "bool") {
    const label = document.createElement("label");
    label.className = "stg-check";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.id = inputId;
    cb.checked = !!currentValue;
    cb.addEventListener("change", () => {
      setModuleParamValue(moduleId, param, cb.checked);
      syncConfigToServer();
      refreshModuleDrivenViews(moduleId);
    });
    label.appendChild(cb);
    label.appendChild(document.createTextNode(" Enabled"));
    tdValue.appendChild(label);

  } else if (param.type === "choice") {
    const sel = document.createElement("select");
    sel.className = "stg-select";
    sel.id = inputId;
    (param.choices || []).forEach(c => {
      const opt = document.createElement("option");
      opt.value = c;
      const choiceLabels = presetSpec?.choiceLabels?.[param.name] || param.choice_labels;
      opt.textContent = (choiceLabels && Object.prototype.hasOwnProperty.call(choiceLabels, c))
        ? choiceLabels[c]
        : c;
      if (c === currentValue) opt.selected = true;
      sel.appendChild(opt);
    });
    sel.addEventListener("change", () => {
      setModuleParamValue(moduleId, param, sel.value);
      syncConfigToServer();
      refreshModuleDrivenViews(moduleId);
    });
    tdValue.appendChild(sel);

  } else if (param.type === "html") {
    tdLabel.remove();
    tr.appendChild(tdValue);
    tdValue.colSpan = 2;
    tdValue.innerHTML = param.default;
    return tr;

  } else if (param.type === "computed_text") {
    tdLabel.remove();
    const div = document.createElement("div");
    div.className = "stg-footnote";
    div.id = inputId;
    div.dataset.template = param.default;
    updateComputedText(div, configValues);
    tdValue.colSpan = 2;
    tdValue.appendChild(div);
    tr.appendChild(tdValue);
    return tr;

  } else {
    // int or float — text input with unit
    const wrapper = document.createElement("div");
    wrapper.className = "input-with-unit stg-iwu";
    const inp = document.createElement("input");
    inp.type = "number";
    inp.className = "unit-input";
    inp.id = inputId;
    inp.value = currentValue;
    inp.inputMode = param.type === "int" ? "numeric" : "decimal";
    inp.step = param.type === "int" ? "1" : "any";
    if (param.min != null) inp.min = param.min;
    if (param.max != null) inp.max = param.max;
    inp.addEventListener("change", () => {
      const fallback = getModuleParamValue(config, moduleId, param);
      const coerced = coerceNumericParamValue(param, inp.value, fallback);
      inp.value = coerced.value;
      if (coerced.ok) {
        setModuleParamValue(moduleId, param, coerced.value);
        syncConfigToServer();
        refreshModuleDrivenViews(moduleId);
      }
    });
    inp.addEventListener("input", () => {
      const coerced = coerceNumericParamValue(param, inp.value, null);
      if (coerced.ok) {
        setModuleParamValue(moduleId, param, coerced.value);
        refreshPreprocessingPresetSelect(moduleId);
      }
    });
    wrapper.appendChild(inp);
    if (param.unit) {
      const suffix = document.createElement("span");
      suffix.className = "unit-suffix";
      suffix.textContent = param.unit;
      wrapper.appendChild(suffix);
    }
    tdValue.appendChild(wrapper);
  }

  tr.appendChild(tdLabel);
  tr.appendChild(tdValue);

  tr.dataset.moduleId = moduleId;

  // Store show_when for later
  if (param.show_when) {
    tr.dataset.showWhen = JSON.stringify(param.show_when);
  }

  return tr;
}

function renderPreprocessingPresetRow(mod) {
  const spec = preprocessingPresetSpec(mod?.name);
  if (!spec) return null;

  const tr = document.createElement("tr");
  tr.className = "module-preset-row";
  tr.dataset.moduleId = mod.name;

  const tdLabel = document.createElement("td");
  tdLabel.textContent = spec.controlLabel || "Preset";
  tdLabel.title = moduleDisplayTooltip(mod);

  const tdValue = document.createElement("td");
  const sel = document.createElement("select");
  sel.className = "stg-select module-preset-select";
  sel.id = `mod_${mod.name}_preset`;

  const currentKey = currentPreprocessingPresetKey(mod.name);
  (spec.presets || []).forEach((preset) => {
    const opt = document.createElement("option");
    opt.value = preset.key;
    opt.textContent = preset.label;
    if (preset.custom && currentKey !== "custom") opt.disabled = true;
    if (preset.key === currentKey) opt.selected = true;
    sel.appendChild(opt);
  });

  sel.addEventListener("change", async () => {
    const key = sel.value;
    if (key === "custom") {
      refreshPreprocessingPresetSelect(mod.name);
      return;
    }
    if (key === "off") {
      moduleState[mod.name] = false;
      await toggleModule(mod.name, false);
      syncConfigFromModuleState();
      syncConfigToServer();
      renderModulePanel();
      renderDynamicSettings();
      refreshModuleDrivenViews();
      return;
    }
    applyPreprocessingPresetValues(mod.name, key);
    syncConfigToServer();
    renderDynamicSettings();
    refreshModuleDrivenViews(mod.name);
  });

  tdValue.appendChild(sel);
  tr.appendChild(tdLabel);
  tr.appendChild(tdValue);
  return tr;
}

/**
 * Update a computed_text element by substituting {key} placeholders.
 */
function updateComputedText(el, configValues) {
  const template = el.dataset.template || "";
  el.textContent = template.replace(/\{(\w+)\}/g, (_, key) => {
    return configValues[key] ?? key;
  });
}

/**
 * Apply show_when visibility rules for a module's params.
 */
function applyShowWhenRules(moduleId) {
  const rows = document.querySelectorAll(`tr[data-module-id="${moduleId}"]`);
  rows.forEach(tr => {
    const rule = JSON.parse(tr.dataset.showWhen || "{}");
    const visible = showWhenRuleMatches(rule, param => {
      const input = document.getElementById(`mod_${moduleId}_${param}`);
      if (!input) return undefined;
      return input.type === "checkbox" ? input.checked :
             input.tagName === "SELECT" ? input.value :
             input.value;
    });
    tr.classList.toggle("is-hidden", !visible);
  });
}

/**
 * Render an entire module's settings as a DOM fragment.
 */
function renderModuleSection(mod, configValues) {
  // Sort params by order
  const params = Object.values(mod.params).sort((a, b) => (a.order || 0) - (b.order || 0));
  const moduleConfigValues = projectModuleConfigValues(mod.name, mod, configValues);

  // Modules with zero configurable params would render as an empty header bar.
  // Skip them — their presence is already shown in the module toggle panel.
  if (params.length === 0) return null;

  const section = document.createElement("div");
  section.className = "module-settings-section";
  section.dataset.moduleId = mod.name;

  // Module heading
  const h4 = document.createElement("h4");
  h4.className = "settings-subsection-head";
  h4.textContent = moduleDisplayName(mod);
  h4.title = moduleDisplayTooltip(mod);
  section.appendChild(h4);

  const presetRow = renderPreprocessingPresetRow(mod);
  if (presetRow) {
    const presetTable = document.createElement("table");
    presetTable.className = "settings-table module-preset-table";
    presetTable.appendChild(presetRow);
    section.appendChild(presetTable);
  }

  // Group params
  let currentGroup = null;
  let table = null;

  for (const param of params) {
    const group = param.group || "";
    if (group !== currentGroup || !table) {
      table = document.createElement("table");
      table.className = "settings-table";
      section.appendChild(table);
      currentGroup = group;
    }
    table.appendChild(renderParamRow(mod.name, param, moduleConfigValues));
  }

  return section;
}

/**
 * Load modules from server and render the control panel + settings.
 */
async function loadModules() {
  try {
    const data = await fetchModules();
    moduleData = data.modules || [];
    moduleState = {};
    moduleData.forEach(m => { moduleState[m.name] = m.enabled; });
    syncConfigFromModuleState();
    renderModulePanel();
    renderDynamicSettings();
    refreshModuleDrivenViews();
  } catch (err) {
    console.warn("[modules] load failed:", err.message);
  }
}

function getModulePosture(mod) {
  return MODULE_POSTURE[mod?.name] || null;
}

/**
 * Render the module toggles panel.
 */
function renderModulePanel() {
  const container = document.getElementById("moduleToggles");
  if (!container) return;
  container.innerHTML = "";

  const group = renderModuleToggleGroup("preprocessing", modulesForSlot("preprocessing"), {
    showSlotLabel: false,
  });
  if (group) container.appendChild(group);
}

function renderModuleToggleGroup(slot, modules, { showSlotLabel = true } = {}) {
  if (!modules || modules.length === 0) return null;

  const group = document.createElement("div");
  group.className = "module-toggle-group";

  if (showSlotLabel) {
    const slotLabel = document.createElement("span");
    slotLabel.className = "module-slot-label";
    slotLabel.textContent = slot === "preprocessing"
      ? "Pre-processing"
      : slot.charAt(0).toUpperCase() + slot.slice(1);
    group.appendChild(slotLabel);
  }

  modules
    .slice()
    .sort((a, b) => {
      const postureA = getModulePosture(a);
      const postureB = getModulePosture(b);
      const orderA = postureA?.order ?? 99;
      const orderB = postureB?.order ?? 99;
      if (orderA !== orderB) return orderA - orderB;
      return a.name.localeCompare(b.name);
    })
    .forEach(m => {
      const row = document.createElement("label");
      row.className = "module-toggle-row";
      const posture = getModulePosture(m);

      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = !!moduleState[m.name];
      cb.addEventListener("change", async () => {
        moduleState[m.name] = cb.checked;
        if (cb.checked && m.slot === "preprocessing") {
          applyDefaultPreprocessingPresetIfNeeded(m.name);
        }
        await toggleModule(m.name, cb.checked);
        syncConfigFromModuleState();
        syncConfigToServer();
        renderModulePanel();
        renderDynamicSettings();
        refreshModuleDrivenViews();
      });
      row.appendChild(cb);

      const copy = document.createElement("span");
      copy.className = "module-toggle-copy";
      copy.title = moduleDisplayTooltip(m);

      const text = document.createElement("span");
      text.className = "module-toggle-label";
      text.textContent = moduleDisplayName(m);
      copy.appendChild(text);

      if (posture?.label) {
        const badge = document.createElement("span");
        badge.className = `module-posture-badge is-${posture.tone}`;
        badge.textContent = posture.label;
        copy.appendChild(badge);
      }

      row.appendChild(copy);

      group.appendChild(row);
    });

  return group;
}

/**
 * Render settings sections for all enabled modules.
 */
function renderDynamicSettings() {
  restoreSettingsFlowUnits(document.querySelector(".settings-grid"));
  const slotContainers = {
    preprocessing: document.getElementById("preprocessingSettingsContainer"),
  };
  Object.values(slotContainers).forEach(c => {
    if (c) c.innerHTML = "";
  });

  moduleData
    .filter(m => moduleState[m.name] && MODULE_UI_VISIBLE_SLOTS.has(m.slot))
    .forEach((m, index) => {
      const target = slotContainers[m.slot];
      if (!target) return;
      const section = renderModuleSection(m, config);
      if (!section) return;
      section.dataset.settingsFlowOrder = String(index);
      target.appendChild(section);
      applyShowWhenRules(m.name);
    });

  // Re-distribute columns since dynamic sections changed
  distributeSettingsColumns();
}

// ── Collapsible Settings Sections ────────────────────────────────────────────

function restoreSettingsFlowUnits(grid = document.querySelector(".settings-grid")) {
  if (!grid) return;
  const units = Array.from(grid.querySelectorAll("[data-settings-flow-owner]")).sort((a, b) => {
    const orderA = Number(a.dataset.settingsFlowOrder);
    const orderB = Number(b.dataset.settingsFlowOrder);
    if (Number.isFinite(orderA) && Number.isFinite(orderB) && orderA !== orderB) return orderA - orderB;
    return 0;
  });
  units.forEach((unit) => {
    const ownerId = unit.dataset.settingsFlowOwner;
    const owner = ownerId ? document.getElementById(ownerId) : null;
    if (!owner) return;
    owner.appendChild(unit);
    unit.classList.remove("preprocessing-flow-unit");
    unit.removeAttribute("data-settings-flow-owner");
    unit.removeAttribute("data-bucket");
  });
}

function extractPreprocessingFlowUnits(grid) {
  const group = grid?.querySelector('[data-settings-group="preprocessing"]');
  const owner = document.getElementById("preprocessingSettingsContainer");
  if (!group || !owner || !group.classList.contains("is-expanded")) return;

  let insertionPoint = group;
  Array.from(owner.children)
    .filter((unit) => unit.classList.contains("module-settings-section"))
    .sort((a, b) => Number(a.dataset.settingsFlowOrder) - Number(b.dataset.settingsFlowOrder))
    .forEach((unit) => {
    unit.classList.add("preprocessing-flow-unit");
    unit.dataset.settingsFlowOwner = owner.id;
    unit.dataset.bucket = "preprocessing";
    insertionPoint.after(unit);
    insertionPoint = unit;
    });
}

/**
 * Distribute settings-grid children into explicit column divs.
 * Fills each column top-to-bottom before starting the next.
 * Called once on init and on window resize.
 */
function distributeSettingsColumns() {
  const grid = document.querySelector(".settings-grid");
  if (!grid) return;
  const inDrawer = grid.classList.contains("in-drawer");
  const colWidth = 380;
  const colGap = 24;
  const drawerMaxWidth = 1280;
  const drawerRevealWidth = 32;
  const drawerBodyPadding = 24;
  const drawerChrome = 2;
  // Abort if not measurable — unwrapping + re-running with clientWidth=0 would
  // collapse the grid to a single column and the result would stick once visible.
  if (grid.clientWidth < 100) return;

  // Reparenting a focused live control can make some browsers drop focus or
  // text selection. Preserve both across responsive redistribution.
  const focusedElement = grid.contains(document.activeElement) ? document.activeElement : null;
  const focusedSelection = focusedElement && typeof focusedElement.selectionStart === "number"
    ? {
        start: focusedElement.selectionStart,
        end: focusedElement.selectionEnd,
        direction: focusedElement.selectionDirection,
      }
    : null;

  const drawer = inDrawer ? document.getElementById("settingsDrawer") : null;
  if (drawer) {
    drawer.style.setProperty("--settings-drawer-width", "1280px");
  }

  // Restore canonical dynamic ownership before unwrapping and measuring.
  restoreSettingsFlowUnits(grid);

  // Unwrap existing column wrappers first — move their children back to grid
  grid.querySelectorAll(".settings-column").forEach(col => {
    while (col.firstChild) grid.appendChild(col.firstChild);
    col.remove();
  });

  // Enabled preprocessing module cards are independent layout units. Moving
  // the live nodes lets them flow across columns without splitting a card or
  // losing input values, event handlers, focus, or slider state.
  extractPreprocessingFlowUnits(grid);

  // All items participate in column distribution — no exceptions
  const items = Array.from(grid.children);
  if (items.length === 0) return;

  // Calculate how many columns fit — fixed width matching drawer
  let availWidth = grid.clientWidth;
  if (inDrawer && drawer) {
    const hostWidth = drawer.parentElement?.clientWidth || window.innerWidth || availWidth;
    const targetDrawerWidth = Math.min(
      drawerMaxWidth,
      hostWidth <= 700 ? hostWidth : Math.max(420, hostWidth - drawerRevealWidth),
    );
    // During the slide animation the live grid can still measure at an intermediate width.
    // Plan columns against the intended drawer width, then shrink after layout.
    availWidth = Math.max(
      availWidth,
      targetDrawerWidth - drawerBodyPadding - drawerChrome,
    );
  }
  const maxCols = Math.max(1, Math.floor((availWidth + colGap) / (colWidth + colGap)));

  // Measure available height — the drawer body when open, else the
  // Settings tab (#tabSettings is 0-height while hidden, so prefer the live host).
  const measureEl = inDrawer
    ? document.getElementById("settingsDrawerBody")
    : document.getElementById("tabSettings");
  let availHeight = (measureEl ? measureEl.clientHeight : grid.parentElement.clientHeight) || 800;
  if (measureEl && inDrawer) {
    const measureStyle = getComputedStyle(measureEl);
    const verticalPadding =
      (parseFloat(measureStyle.paddingTop) || 0)
      + (parseFloat(measureStyle.paddingBottom) || 0);
    // clientHeight includes padding, but the columns live inside that padding.
    // Budget against the content box or the drawer gains a tiny
    // phantom scrollbar even when the visible columns fit.
    availHeight = Math.max(120, availHeight - verticalPadding);
  }

  // Measure each item's height
  const heights = items.map(el => el.getBoundingClientRect().height);

  // Fill each column top-to-bottom before starting the next
  const columns = [];
  let colIdx = 0;
  let colHeight = 0;

  for (let i = 0; i < items.length; i++) {
    if (colIdx >= maxCols) colIdx = maxCols - 1; // overflow into last column
    if (!columns[colIdx]) columns[colIdx] = [];

    // If this column is non-empty and adding this item would overflow, start next column
    if (columns[colIdx].length > 0 && colHeight + heights[i] > availHeight && colIdx < maxCols - 1) {
      colIdx++;
      colHeight = 0;
      if (!columns[colIdx]) columns[colIdx] = [];
    }

    columns[colIdx].push(items[i]);
    colHeight += heights[i] + 8; // 8px gap
  }

  // Build column divs and move items into them
  columns.forEach(colItems => {
    const colDiv = document.createElement("div");
    colDiv.className = "settings-column";
    colItems.forEach(el => colDiv.appendChild(el));
    grid.appendChild(colDiv);
  });

  if (focusedElement && grid.contains(focusedElement)) {
    focusedElement.focus({ preventScroll: true });
    if (focusedSelection && typeof focusedElement.setSelectionRange === "function") {
      focusedElement.setSelectionRange(
        focusedSelection.start,
        focusedSelection.end,
        focusedSelection.direction,
      );
    }
  }

  if (inDrawer) {
    const usedColumns = Math.max(1, columns.length);
    const contentWidth = (usedColumns * colWidth) + ((usedColumns - 1) * colGap);
    drawer?.style.setProperty(
      "--settings-drawer-width",
      `${contentWidth + drawerBodyPadding + drawerChrome}px`,
    );
  }
}

let _resizeTimer;
window.addEventListener("resize", () => {
  clearTimeout(_resizeTimer);
  _resizeTimer = setTimeout(distributeSettingsColumns, 200);
});

/**
 * Convert each .settings-group into a collapsible panel.
 * The <h4> becomes a clickable header bar; everything else becomes the body.
 * All sections start expanded. The preset-bar is skipped.
 */
function initCollapsibleSections() {
  document.querySelectorAll(".settings-grid .settings-group").forEach(group => {
    const h4 = group.querySelector(".settings-section-head");
    if (!h4) return;

    // Build header bar
    const header = document.createElement("div");
    header.className = "section-collapse-header";
    header.setAttribute("role", "button");
    header.setAttribute("tabindex", "0");
    header.setAttribute("aria-expanded", "true");
    const arrow = document.createElement("span");
    arrow.className = "section-collapse-arrow";
    arrow.setAttribute("aria-hidden", "true");
    const title = document.createElement("span");
    title.className = "section-collapse-title";
    title.textContent = h4.textContent;
    header.appendChild(arrow);
    header.appendChild(title);

    // Wrap remaining content in a body div
    const body = document.createElement("div");
    body.className = "section-collapse-body";
    // Move all children except h4 into body
    while (group.children.length > 0) {
      const child = group.children[0];
      if (child === h4) { h4.remove(); continue; }
      body.appendChild(child);
    }

    group.appendChild(header);
    group.appendChild(body);
    group.classList.add("is-collapsible", "is-expanded");

    const setExpanded = (expanded) => {
      group.classList.toggle("is-expanded", expanded);
      header.setAttribute("aria-expanded", expanded ? "true" : "false");
      body.classList.toggle("is-hidden", !expanded);
      distributeSettingsColumns();
    };

    header.addEventListener("click", () => {
      const expanded = group.classList.toggle("is-expanded");
      setExpanded(expanded);
    });
    header.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      setExpanded(!group.classList.contains("is-expanded"));
    });
  });
}

// ── Event Binding ────────────────────────────────────────────────────────────

function bindEvents() {
  window.addEventListener("resize", refreshVisibleSolveContours);
  window.addEventListener("resize", syncCreationSidePanelSizing);

  // Operation progress cancel
  const opCancelBtn = $("#opProgressCancel");
  if (opCancelBtn) opCancelBtn.addEventListener("click", cancelProgress);

  // Tab switches
  $$("#tabSwitch .mode-button").forEach((btn) => {
    btn.addEventListener("click", () => switchTab(btn.dataset.tab));
  });

  // Hardcoded settings fields should write through to the draft config on
  // every keystroke so rerenders and actions don't depend on blur-time DOM
  // scraping.
  [
    ["cfgLayerHeight", (raw) => {
      const ok = applyDraftNumberField("layer_height", raw, { isValid: (v) => v > 0 });
      if (ok) {
        const capLayers = Math.max(1, parseInt($("#cfgDWcMin")?.value, 10) || minCapLayersFromThickness(config.d_wc_min, config.layer_height));
        config.d_wc_min = minCapThicknessFromLayers(capLayers, config.layer_height);
      }
      return ok;
    }],
    ["cfgDWb", (raw) => applyDraftNumberField("d_wb", raw, { isValid: (v) => v > 0 })],
    ["cfgDWcMin", (raw) => {
      const layers = parseInt(raw, 10);
      if (!Number.isFinite(layers) || layers < 1) return false;
      config.d_wc_min = minCapThicknessFromLayers(layers, config.layer_height);
      return true;
    }],
    ["cfgTMax", (raw) => applyDraftNumberField("t_max", raw, { isValid: (v) => v > 0 })],
    ["cfgKMax", (raw) => applyDraftNumberField("k_max", raw, { parse: parseInt, isValid: (v) => v >= 1 && v <= 7 })],
    ["cfgDeThreshold", (raw) => applyDraftNumberField("de_threshold", raw, { isValid: (v) => v >= 0 })],
    ["cfgSmoothKernel", (raw) => {
      const radiusMm = parseFloat(raw);
      if (!Number.isFinite(radiusMm) || radiusMm < 0) return false;
      config.smooth_kernel = smoothingCellsFromRadiusMm(radiusMm, getCurrentSolvePitch());
      return true;
    }],
    ["cfgBoundaryCapDeBudget", (raw) => applyDraftNumberField("boundary_cap_de_budget", raw, { isValid: (v) => v >= 0 })],
    ["cfgBorderWidth", (raw) => applyDraftNumberField("border_width_mm", raw, { isValid: (v) => v >= 0 })],
    ["cfgBorderHeight", (raw) => applyDraftNumberField("border_height_mm", raw, { isValid: (v) => v >= 0 })],
  ].forEach(([id, applyDraft]) => bindDraftNumberInput(id, applyDraft));

  // Image tab — upload
  $("#imageUploadInput").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    try {
      const result = await uploadImage(file);
      showToast(`Uploaded ${result.filename}`, "success");
      await loadImages();
      selectedImage = availableImages.find((i) => i.filename === result.filename);
      if (selectedImage) applyImageAspectDefault();  // Stage 11: default to image aspect, short side 120mm
      renderImageTab();
      updateRail();
    } catch (err) {
      showToast(`Upload failed: ${err.message}`, "error");
    }
  });

  // Solve Pitch control (Settings tab)
  const spInput = $("#cfgSolvePitch");
  if (spInput) spInput.addEventListener("input", () => {
    applySolvePitchDraft(spInput.value);
  });
  if (spInput) spInput.addEventListener("change", () => {
    if (applySolvePitchDraft(spInput.value)) syncConfigToServer();
  });

  $$("#cfgLuminanceMode .segmented-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      setSolveModeControlValue(btn.dataset.value || "standard");
      updateLuminanceModeFields();
      updateCapModeFields();
      updateStage4DetailFields();
      readConfigFromUI();
      renderSettingsTab({ preservePendingUi: true });
      updateSettingsSummaries();
      updateDerivedParams();
      updateAccordionSummaries();
      checkPresetModified();
      syncConfigToServer();
    });
  });

  // ── AR button group ──────────────────────────────────────────────────────
  $$("#arButtonGroup .ar-button").forEach((btn) => {
    btn.addEventListener("click", () => {
      const mode = btn.dataset.ar;
      if (mode === "ratio") {
        openRatioDialog();
      } else {
        setARMode(mode);
      }
    });
  });

  // Ratio dialog confirm / cancel
  const ratioConfirm = $("#ratioDialogConfirm");
  if (ratioConfirm) ratioConfirm.addEventListener("click", () => {
    const x = parseFloat($("#ratioDialogX").value) || 1;
    const y = parseFloat($("#ratioDialogY").value) || 1;
    frameState.customRatio = { x, y };
    closeRatioDialog();
    setARMode("ratio");
  });
  const ratioCancel = $("#ratioDialogCancel");
  if (ratioCancel) ratioCancel.addEventListener("click", closeRatioDialog);
  const ratioClose = $("#ratioDialogClose");
  if (ratioClose) ratioClose.addEventListener("click", closeRatioDialog);

  // ── Output dimension fields (with AR coupling) ──────────────────────────
  const owInput = $("#outputWidthMm");
  const ohInput = $("#outputHeightMm");
  if (owInput) owInput.addEventListener("change", () => {
    const v = parseFloat(owInput.value);
    if (!v || v <= 0) return;
    const oldW = frameState.widthMm, oldH = frameState.heightMm;
    frameState.widthMm = clampFrameWidth(v);
    lastTouchedDim = "width";
    if (frameState.arMode !== "specified") applyARToHeight();
    adjustScaleForFrameChange(oldW, oldH, frameState.widthMm, frameState.heightMm);
    syncDimFields();
    syncWidthSlider();
    syncHeightSlider();
    renderFrameCanvas();
    updateInfoGrid();
    syncConfigToServer();
  });
  if (ohInput) ohInput.addEventListener("change", () => {
    const v = parseFloat(ohInput.value);
    if (!v || v <= 0) return;
    const oldW = frameState.widthMm, oldH = frameState.heightMm;
    frameState.heightMm = clampFrameHeight(v);
    lastTouchedDim = "height";
    if (frameState.arMode !== "specified") applyARToWidth();
    adjustScaleForFrameChange(oldW, oldH, frameState.widthMm, frameState.heightMm);
    syncDimFields();
    syncWidthSlider();
    syncHeightSlider();
    renderFrameCanvas();
    updateInfoGrid();
    syncConfigToServer();
  });

  // ── Scale slider + input + fill button ──────────────────────────────────
  const scaleSlider = $("#scaleSlider");
  const scaleInput = $("#scaleInput");
  if (scaleSlider) scaleSlider.addEventListener("input", () => {
    frameState.scale = parseFloat(scaleSlider.value);
    if (scaleInput) scaleInput.value = Math.round(frameState.scale);
    renderFrameCanvas();

  });
  if (scaleSlider) scaleSlider.addEventListener("change", () => syncConfigToServer());
  if (scaleInput) scaleInput.addEventListener("change", () => {
    const v = parseFloat(scaleInput.value);
    if (!isNaN(v)) {
      frameState.scale = clamp(v, 100, 1000);
      syncScaleSlider();
      renderFrameCanvas();
  
      syncConfigToServer();
    }
  });
  const fitImageBtn = $("#fitImageBtn");
  if (fitImageBtn) fitImageBtn.addEventListener("click", () => {
    resetCropToFitSource();
    finishFrameModelUpdate();
  });
  const fillWBtn = $("#fillWidthBtn");
  if (fillWBtn) fillWBtn.addEventListener("click", () => {
    fitFrameToSourceWidth();
    finishFrameModelUpdate();
  });
  const fillHBtn = $("#fillHeightBtn");
  if (fillHBtn) fillHBtn.addEventListener("click", () => {
    fitFrameToSourceHeight();
    finishFrameModelUpdate();
  });

  // ── Rotation slider + input ─────────────────────────────────────────────
  const rotSlider = $("#rotationSlider");
  const rotInput = $("#rotationInput");
  if (rotSlider) rotSlider.addEventListener("input", () => {
    frameState.rotation = parseFloat(rotSlider.value);
    if (rotInput) rotInput.value = frameState.rotation.toFixed(1);
    renderFrameCanvas();

  });
  if (rotSlider) rotSlider.addEventListener("change", () => syncConfigToServer());
  if (rotInput) rotInput.addEventListener("change", () => {
    const v = parseFloat(rotInput.value);
    if (!isNaN(v)) {
      frameState.rotation = clamp(v, -180, 180);
      syncRotationSlider();
      renderFrameCanvas();
  
      syncConfigToServer();
    }
  });

  // ── Rotation buttons: 90° L/R, H-flip, V-flip ──────────────────────────
  const rot90L = $("#rotate90LBtn");
  const rot90R = $("#rotate90RBtn");
  const hFlip = $("#flipHBtn");
  const vFlip = $("#flipVBtn");

  if (rot90L) rot90L.addEventListener("click", () => {
    frameState.rotation = clamp(frameState.rotation - 90, -180, 180);
    syncRotationSlider();
    renderFrameCanvas();

    syncConfigToServer();
  });
  if (rot90R) rot90R.addEventListener("click", () => {
    frameState.rotation = clamp(frameState.rotation + 90, -180, 180);
    syncRotationSlider();
    renderFrameCanvas();

    syncConfigToServer();
  });
  if (hFlip) hFlip.addEventListener("click", () => {
    frameState.flipH = !frameState.flipH;
    renderFrameCanvas();
    syncConfigToServer();
  });
  if (vFlip) vFlip.addEventListener("click", () => {
    frameState.flipV = !frameState.flipV;
    renderFrameCanvas();
    syncConfigToServer();
  });

  // ── Frame editor sub-tabs ────────────────────────────────────────────────
  $$("#frameEditorTabs .frame-tab").forEach(btn => {
    btn.addEventListener("click", () => switchFrameEditorTab(btn.dataset.ftab));
  });

  // ── Direction toggle ────────────────────────────────────────────────────
  $$("#directionToggle .toggle-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      imageDirection = btn.dataset.dir;
      $$("#directionToggle .toggle-btn").forEach(b => b.classList.toggle("is-active", b === btn));
      // Swap width/height
      const oldW = frameState.widthMm, oldH = frameState.heightMm;
      [frameState.widthMm, frameState.heightMm] = [frameState.heightMm, frameState.widthMm];
      // Re-apply AR if in a ratio mode
      if (frameState.arMode !== "specified") {
        applyARFromLastTouched();
      }
      adjustScaleForFrameChange(oldW, oldH, frameState.widthMm, frameState.heightMm);
      updateARButtons();
      syncDimFields();
      syncWidthSlider();
      syncHeightSlider();
      renderFrameCanvas();
      updateInfoGrid();
      syncConfigToServer();
    });
  });

  // ── Dimension lock button ────────────────────────────────────────────────
  const wLockBtn = $("#widthLockBtn");
  if (wLockBtn) wLockBtn.addEventListener("click", (e) => {
    e.preventDefault();
    widthLocked = !widthLocked;
    wLockBtn.classList.toggle("is-locked", widthLocked);
    syncDimLockState();
  });
  const hLockBtn = $("#heightLockBtn");
  if (hLockBtn) hLockBtn.addEventListener("click", (e) => {
    e.preventDefault();
    heightLocked = !heightLocked;
    hLockBtn.classList.toggle("is-locked", heightLocked);
    syncDimLockState();
  });

  // ── Width/Height sliders ────────────────────────────────────────────────
  const widthSlider = $("#widthSlider");
  if (widthSlider) widthSlider.addEventListener("input", () => {
    const oldW = frameState.widthMm, oldH = frameState.heightMm;
    frameState.widthMm = clampFrameWidth(parseFloat(widthSlider.value));
    lastTouchedDim = "width";
    if (frameState.arMode !== "specified") applyARToHeight();
    adjustScaleForFrameChange(oldW, oldH, frameState.widthMm, frameState.heightMm);
    syncDimFields();
    syncHeightSlider();
    renderFrameCanvas();
    updateInfoGrid();
  });
  if (widthSlider) widthSlider.addEventListener("change", () => syncConfigToServer());

  const heightSlider = $("#heightSlider");
  if (heightSlider) heightSlider.addEventListener("input", () => {
    const oldW = frameState.widthMm, oldH = frameState.heightMm;
    frameState.heightMm = clampFrameHeight(parseFloat(heightSlider.value));
    lastTouchedDim = "height";
    if (frameState.arMode !== "specified") applyARToWidth();
    adjustScaleForFrameChange(oldW, oldH, frameState.widthMm, frameState.heightMm);
    syncDimFields();
    syncWidthSlider();
    renderFrameCanvas();
    updateInfoGrid();
  });
  if (heightSlider) heightSlider.addEventListener("change", () => syncConfigToServer());

  // ── Image adjustment controls ───────────────────────────────────────────
  // B/W | Color toggle
  $$("#bwColorToggle .toggle-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      imageAdjust.mode = btn.dataset.val;
      $$("#bwColorToggle .toggle-btn").forEach(b => b.classList.toggle("is-active", b === btn));
      const colorCtrl = $("#colorControls");
      if (colorCtrl) colorCtrl.style.display = imageAdjust.mode === "bw" ? "none" : "";
      renderFrameCanvas();
      syncConfigToServer();
    });
  });

  // Reset button — restore all framing + adjustment settings to defaults
  const resetBtn = $("#frameResetBtn");
  if (resetBtn) resetBtn.addEventListener("click", () => {
    // Reset frame state
    frameState.customRatio = { x: 1, y: 1 };
    frameState.scale = 100.0;
    frameState.rotation = 0;
    frameState.panX = 0;
    frameState.panY = 0;
    frameState.flipH = false;
    frameState.flipV = false;
    if (selectedImage) {
      applyImageAspectDefault();   // Stage 11: reset to image aspect (short side 120mm), not a square
    } else {
      frameState.arMode = "specified";
      frameState.widthMm = 100;
      frameState.heightMm = 100;
    }

    // Reset image adjustments
    imageAdjust = {
      mode: "color", exposure: 0, contrast: 0, highlight: 0, shadow: 0,
      tint_hue: 0, tint_strength: 0, saturation: 0, temperature: 0,
    };
    const adjustResets = [
      ["adjustExposure", 0], ["adjustContrast", 0],
      ["adjustHighlight", 0], ["adjustShadow", 0],
      ["adjustTintHue", 0], ["adjustTintStrength", 0],
      ["adjustSaturation", 0], ["adjustTemp", 0],
    ];
    for (const [id, val] of adjustResets) {
      const inp = $(`#${id}`);
      const sld = $(`#${id}Slider`);
      if (inp) inp.value = val;
      if (sld) sld.value = val;
    }
    // Reset B/W toggle
    $$("#bwColorToggle .toggle-btn").forEach(b =>
      b.classList.toggle("is-active", b.dataset.val === "color"));
    const colorCtrl = $("#colorControls");
    if (colorCtrl) colorCtrl.style.display = "";

    // Sync frame UI
    syncScaleSlider();
    syncRotationSlider();
    syncDimFields();
    updateARButtons();
    renderFrameCanvas();
    updateInfoGrid();
    syncConfigToServer();
  });

  // Generic binder for image adjustment sliders
  const adjustPairs = [
    ["adjustExposure", "adjustExposureSlider", "exposure"],
    ["adjustContrast", "adjustContrastSlider", "contrast"],
    ["adjustHighlight", "adjustHighlightSlider", "highlight"],
    ["adjustShadow", "adjustShadowSlider", "shadow"],
    ["adjustTintHue", "adjustTintHueSlider", "tint_hue"],
    ["adjustTintStrength", "adjustTintStrengthSlider", "tint_strength"],
    ["adjustSaturation", "adjustSaturationSlider", "saturation"],
    ["adjustTemp", "adjustTempSlider", "temperature"],
  ];
  adjustPairs.forEach(([inputId, sliderId, key]) => {
    const inp = $(`#${inputId}`);
    const sld = $(`#${sliderId}`);
    if (inp && sld) {
      const applyInputValue = () => {
        const value = parseFloat(inp.value);
        if (Number.isNaN(value)) return false;
        imageAdjust[key] = value;
        sld.value = imageAdjust[key];
        renderFrameCanvas();
        return true;
      };
      sld.addEventListener("input", () => {
        imageAdjust[key] = parseFloat(sld.value);
        inp.value = imageAdjust[key];
        renderFrameCanvas();
      });
      sld.addEventListener("change", () => syncConfigToServer());
      inp.addEventListener("input", applyInputValue);
      inp.addEventListener("change", () => {
        if (applyInputValue()) syncConfigToServer();
      });
    }
  });

  // ── Frame canvas interaction (pan, zoom, edge-drag, drag-drop) ──────────
  initFrameInteraction();

  // Image Library pane state transitions
  const libraryResizeBtn = $("#libraryResizeBtn");
  if (libraryResizeBtn) libraryResizeBtn.addEventListener("click", () => {
    toggleLibraryPaneState();
  });
  const imageLibraryRefreshBtn = $("#imageLibraryRefreshBtn");
  if (imageLibraryRefreshBtn) imageLibraryRefreshBtn.addEventListener("click", async () => {
    imageLibraryRefreshBtn.disabled = true;
    try {
      await refreshImageLibrary({ announce: true });
    } catch (err) {
      showToast(`Refresh failed: ${err.message}`, "error");
    } finally {
      imageLibraryRefreshBtn.disabled = false;
    }
  });
  $("#imageLibraryOpenFolderBtn")?.addEventListener("click", handleOpenImageLibraryFolder);
  bindImageLibraryWheelScroll();

  // Border toggle switch + inline fields
  const borderToggle = $("#borderToggle");
  const borderCheck = $("#cfgBorder");
  if (borderToggle && borderCheck) {
    borderToggle.addEventListener("click", () => {
      borderCheck.checked = !borderCheck.checked;
      updateBorderVisibility();
      readConfigFromUI();
      updateInfoGrid();
      renderFrameCanvas();
      renderPreview();
      syncConfigToServer();
    });
  }
  const borderW = $("#cfgBorderWidth");
  if (borderW) borderW.addEventListener("input", () => { readConfigFromUI(); updateInfoGrid(); renderPreview(); });
  if (borderW) borderW.addEventListener("change", () => { readConfigFromUI(); updateInfoGrid(); renderFrameCanvas(); syncConfigToServer(); });
  const borderH = $("#cfgBorderHeight");
  if (borderH) borderH.addEventListener("change", () => { readConfigFromUI(); renderFrameCanvas(); syncConfigToServer(); });

  // Leading-zero normalization for border numeric inputs
  for (const id of ["cfgBorderWidth", "cfgBorderHeight"]) {
    const el = $(`#${id}`);
    if (el) el.addEventListener("blur", () => {
      const v = parseFloat(el.value);
      if (!isNaN(v)) el.value = v.toFixed(2);
    });
  }

  // White base/cap filament dropdown
  const bcHandler = () => {
    readConfigFromUI();
    readPrinterConfig();
    updateSuggestSlotHint();
    renderCreationTab();
    syncConfigToServer();
  };
  const cfgBase = $("#cfgBaseFilament");
  if (cfgBase) cfgBase.addEventListener("change", bcHandler);

  // ── Creation mode tabs ─────────────────────────────────────────────────
  $$(".creation-mode-tabs .segmented-btn[data-panel]").forEach((btn) => {
    btn.addEventListener("click", () => toggleCreationMode(btn.dataset.panel));
  });

  // Candidate All / None buttons
  const candAll = $("#candidateSelectAll");
  if (candAll) candAll.addEventListener("click", () => { selectAllCandidates(); renderCreationTab(); });
  const candNone = $("#candidateSelectNone");
  if (candNone) candNone.addEventListener("click", () => { candidateSelection.clear(); renderCreationTab(); });

  // Max colors input → update AMS preview
  const maxColorsInput = $("#targetFilamentCount");
  if (maxColorsInput) maxColorsInput.addEventListener("change", () => renderAmsPreview());

  // Palette — manual builder actions
  const mintBtn = $("#mintPaletteBtn");
  if (mintBtn) mintBtn.addEventListener("click", mintPaletteToDeck);
  const clearBtn = $("#clearComposerBtn");
  if (clearBtn) clearBtn.addEventListener("click", clearManualSlots);

  // Palette — deck actions
  const railLoadBtn = $("#railLoadPaletteBtn");
  if (railLoadBtn) railLoadBtn.addEventListener("click", () => showLoadPaletteMenu(railLoadBtn));

  // Staging-pad Clear: empties the staging pad only (never touches the persistent deck).
  const clearDeckBtn = $("#clearDeckBtn");
  if (clearDeckBtn) {
    let confirmPending = false;
    clearDeckBtn.addEventListener("click", () => {
      if (stagingDeck.length === 0) return;
      if (!confirmPending) {
        confirmPending = true;
        clearDeckBtn.textContent = "Confirm";
        clearDeckBtn.classList.add("danger");
        setTimeout(() => {
          confirmPending = false;
          clearDeckBtn.textContent = "Clear";
          clearDeckBtn.classList.remove("danger");
        }, 3000);
      } else {
        confirmPending = false;
        clearDeckBtn.textContent = "Clear";
        clearDeckBtn.classList.remove("danger");
        stagingDeck = [];
        suggestCapacityNote = "";
        renderCreationTab();
      }
    });
  }

  // Persistent-deck Clear (rail): empties the persistent deck only + clears the active palette.
  const railClearDeckBtn = $("#railClearDeckBtn");
  if (railClearDeckBtn) {
    let confirmPending = false;
    railClearDeckBtn.addEventListener("click", () => {
      if (deck.length === 0) return;
      if (!confirmPending) {
        confirmPending = true;
        railClearDeckBtn.textContent = "Confirm";
        railClearDeckBtn.classList.add("danger");
        setTimeout(() => {
          confirmPending = false;
          railClearDeckBtn.textContent = "Clear";
          railClearDeckBtn.classList.remove("danger");
        }, 3000);
      } else {
        confirmPending = false;
        railClearDeckBtn.textContent = "Clear";
        railClearDeckBtn.classList.remove("danger");
        deck = [];
        activeDeckId = null;
        renderDeckCards();
        updateRail();
        syncConfigToServer();
      }
    });
  }
  const railDeckList = $("#railDeckList");
  if (railDeckList) railDeckList.addEventListener("scroll", () => hideRailDeckHoverPreview(), { passive: true });
  const tabContentArea = document.querySelector(".tab-content-area");
  if (tabContentArea) tabContentArea.addEventListener("scroll", () => hideRailDeckHoverPreview(), { passive: true });
  window.addEventListener("resize", () => {
    hideRailDeckHoverPreview();
    hideSolveRunHoverPreview();
    hideSolveRunSettingsPanel();
  });

  // Palette suggestion
  const suggestBtn = $("#suggestPalettesBtn");
  if (suggestBtn) suggestBtn.addEventListener("click", handleSuggestPalettes);
  const luminanceGuessBtn = $("#cfgBaseShadingLimitSuggest");
  if (luminanceGuessBtn) luminanceGuessBtn.addEventListener("click", handleSuggestBaseShadingLimit);
  DECK_GENERATION_FIELD_MAP.forEach(({ paletteId }) => {
    const el = $(`#${paletteId}`);
    if (!el) return;
    const eventName = el.type === "checkbox" ? "change" : "input";
    el.addEventListener(eventName, () => syncDeckGenerationSettingsUI("palette"));
    el.addEventListener("change", () => {
      syncDeckGenerationSettingsUI("palette");
      readConfigFromUI();
      syncConfigToServer();
    });
  });

  // Library filter (modal)
  const railLibBtn = $("#railLibraryBtn");
  if (railLibBtn) railLibBtn.addEventListener("click", openLibraryModal);
  const libModalClose = $("#libraryModalClose");
  if (libModalClose) libModalClose.addEventListener("click", closeLibraryModal);
  const libBackdrop = $("#libraryModalBackdrop");
  if (libBackdrop) libBackdrop.addEventListener("click", (e) => { if (e.target === libBackdrop) closeLibraryModal(); });
  const filterAll = $("#libraryFilterSelectAll");
  if (filterAll) filterAll.addEventListener("click", handleLibraryFilterSelectAll);
  const filterNone = $("#libraryFilterDeselectAll");
  if (filterNone) filterNone.addEventListener("click", handleLibraryFilterDeselectAll);

  // Published Model Libraries manager
  $("#modelLibrariesBtn")?.addEventListener("click", openModelLibrariesModal);
  $("#modelLibrariesCloseBtn")?.addEventListener("click", closeModelLibrariesModal);
  $("#modelLibrariesRefreshBtn")?.addEventListener("click", () => loadModelLibraries());
  $("#modelLibrariesOpenFolderBtn")?.addEventListener("click", handleOpenModelLibrariesFolder);
  $("#modelLibraryPackageInput")?.addEventListener("change", event => {
    const file = event.target?.files?.[0];
    if (file) handleInstallModelLibrary(file);
  });
  const modelLibrariesModal = $("#modelLibrariesModal");
  if (modelLibrariesModal) {
    modelLibrariesModal.addEventListener("click", event => {
      if (event.target === modelLibrariesModal) closeModelLibrariesModal();
    });
  }

  // Detail drawer (legacy)
  const closeDrawerBtnLegacy = $("#closeDetailDrawer");
  if (closeDrawerBtnLegacy) closeDrawerBtnLegacy.addEventListener("click", closeDetailDrawer);
  const drawerOverlay = $("#drawerOverlay");
  if (drawerOverlay) drawerOverlay.addEventListener("click", () => {
    // The settings drawer is a persistent overlay with no scrim; only the detail
    // drawer uses #drawerOverlay, so an overlay click only closes that.
    closeDetailDrawer();
  });

  // Settings drawer
  const settingsDrawerBtn = $("#settingsDrawerBtn");
  if (settingsDrawerBtn) settingsDrawerBtn.addEventListener("click", toggleSettingsDrawer);
  const closeSettingsDrawerBtn = $("#closeSettingsDrawer");
  if (closeSettingsDrawerBtn) closeSettingsDrawerBtn.addEventListener("click", closeSettingsDrawer);

  const advancedToggle = $("#settingsAdvancedToggle");
  if (advancedToggle) {
    advancedToggle.addEventListener("click", () => {
      settingsAdvancedVisible = !settingsAdvancedVisible;
      saveSettingsAdvancedVisible(settingsAdvancedVisible);
      updateAdvancedSettingsVisibility();
      distributeSettingsColumns();
    });
  }

  // Lightbox close
  const lbClose = $("#compLightboxClose");
  if (lbClose) lbClose.addEventListener("click", closeCompLightbox);
  const lb = $("#compLightbox");
  if (lb) lb.addEventListener("click", () => closeCompLightbox());

  // Solve result cards → lightbox (single delegated dispatcher, keyed on data-solve-card-kind).
  const comparisonGrid = $("#solveComparisonGrid");
  if (comparisonGrid) {
    comparisonGrid.addEventListener("click", (e) => {
      const card = e.target.closest(".solve-grid-column[data-solve-card-kind]");
      if (card) openSolveCardLightboxFromElement(card);
    });
  }

  // Thickness map thumbnails → lightbox (same dispatcher).
  const mapsGrid = $("#filamentMapsGrid");
  if (mapsGrid) {
    mapsGrid.addEventListener("click", (e) => {
      const card = e.target.closest(".filament-map-card[data-solve-card-kind]");
      if (card) openSolveCardLightboxFromElement(card);
    });
  }

  // Accordion
  bindAccordions();

  // Settings inputs — auto-sync on change
  bindSettingsAutoSyncControls();

  // Solve
  $("#startSolveBtn").addEventListener("click", handleStartSolve);

  // Clear solve history
  ["clearSolveHistoryBtn", "exportClearSolveHistoryBtn"].forEach((id) => {
    const btn = $(`#${id}`);
    if (btn) btn.addEventListener("click", handleSolveHistoryClearClick);
  });

  // Clear all temp files (solve runs + LUTs)
  const clearAllBtn = $("#clearAllTempBtn");
  if (clearAllBtn) {
    clearAllBtn.addEventListener("click", async () => {
      const ok = await appConfirm(
        "Delete ALL cached temp files (solve runs + LUTs)? Your exported files and saved runs are kept.",
        { ok: "Delete", cancel: "Cancel", title: "Clear Temp Files" });
      if (!ok) return;
      try {
        const r = await fetch("/api/cache/clear-all", { method: "POST" });
        if (r.status === 409) {
          showToast("A solve, export, or palette suggestion is still running — wait for it to finish before clearing.", "warn");
        } else if (!r.ok) {
          console.warn("clear-all failed", r.status);
        }
      } catch (e) { console.warn(e); }
    });
  }

  // Saved Runs browser (Stage 9b)
  ["savedRunsBtn", "exportSavedRunsBtn"].forEach((id) => {
    const btn = $(`#${id}`);
    if (btn) btn.addEventListener("click", () => openSavedRunsModal("run"));
  });
  const savedRunsCloseBtn = $("#savedRunsCloseBtn");
  if (savedRunsCloseBtn) savedRunsCloseBtn.addEventListener("click", () => _setSavedRunsModalOpen(false));
  const savedRunLoadBtn = $("#savedRunLoadBtn");
  if (savedRunLoadBtn) savedRunLoadBtn.addEventListener("click", activateSelectedSavedRun);
  const savedRunLoadSettingsBtn = $("#savedRunLoadSettingsBtn");
  if (savedRunLoadSettingsBtn) savedRunLoadSettingsBtn.addEventListener("click", () => {
    const selected = getSelectedSavedRun();
    if (selected) loadSettingsFromSavedRun(selected);
  });
  const savedRunDownloadBtn = $("#savedRunDownloadBtn");
  if (savedRunDownloadBtn) savedRunDownloadBtn.addEventListener("click", downloadSelectedSavedRun);
  const savedRunSaveBtn = $("#savedRunSaveBtn");
  if (savedRunSaveBtn) savedRunSaveBtn.addEventListener("click", promoteSelectedSavedRun);
  const savedRunRenameBtn = $("#savedRunRenameBtn");
  if (savedRunRenameBtn) savedRunRenameBtn.addEventListener("click", () => {
    const selected = getSelectedSavedRun();
    if (selected && selected.tier === "saved") openRenameSavedRunDialog(selected);
  });
  const savedRunDeleteBtn = $("#savedRunDeleteBtn");
  if (savedRunDeleteBtn) savedRunDeleteBtn.addEventListener("click", deleteSelectedSavedRun);
  const savedRunUpload = $("#savedRunUpload");
  if (savedRunUpload) savedRunUpload.addEventListener("change", async (e) => {
    const file = e.target.files && e.target.files[0];
    if (!file) return;
    try {
      const body = await uploadSavedRun(file);
      await applyLoadedRun(body);
      _setSavedRunsModalOpen(false);
      showToast("Loaded uploaded run", "");
    } catch (err) { showToast(err.message, "error"); }
    finally { e.target.value = ""; }
  });

  // Advanced (demoted) views inline disclosure
  const solveAdvancedToggle = $("#solveAdvancedToggle");
  if (solveAdvancedToggle) {
    solveAdvancedToggle.addEventListener("click", () => {
      setSolveAdvancedViewsOpen(!solveAdvancedViewsOpen);
    });
  }

  // Solve view toggle (everyday + advanced segments)
  $$("#solveViewBar .view-toggle-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const requestedView = btn.dataset.view;
      solveView = requestedView === "white_cap" ? solveWhiteCapView : requestedView;
      if (isSolveWhiteCapView(solveView)) solveWhiteCapView = solveView;
      if (
        requestedView === "color_ceiling"
        && !solveColorRegionsViewWasExplicitlySelected
        && shouldDefaultColorRegionsToRecipe()
      ) {
        solveColorRegionsView = "recipe_regions";
      }
      renderSolveComparisonGrid();
    });
  });

  const solveSourceToggle = $("#solveSourceImageToggle");
  if (solveSourceToggle) {
    solveSourceToggle.addEventListener("click", () => {
      solveShowSourceImage = !solveShowSourceImage;
      renderSolveComparisonGrid();
    });
  }

  document.querySelectorAll("[data-solve-color-regions-view]").forEach(btn => {
    btn.addEventListener("click", () => {
      solveColorRegionsViewWasExplicitlySelected = true;
      solveColorRegionsView = btn.dataset.solveColorRegionsView === "recipe_regions"
        ? "recipe_regions" : "color_ceiling";
      renderSolveComparisonGrid();
    });
  });

  document.querySelectorAll("[data-solve-white-cap-view]").forEach(btn => {
    btn.addEventListener("click", () => {
      const nextView = btn.dataset.solveWhiteCapView || "cap_map";
      solveWhiteCapView = nextView;
      solveView = nextView;
      renderSolveComparisonGrid();
    });
  });

  const solveContoursToggle = $("#solveContoursToggle");
  if (solveContoursToggle) {
    solveContoursToggle.addEventListener("click", () => {
      solveContoursEnabled = !solveContoursEnabled;
      updateSolveSubControls();
      updateSolveColumnImages();
      updateSolveLegend();
    });
  }

  // Cap / Color Diff mode sub-buttons (shared mode; re-rasterises without re-fetching)
  document.querySelectorAll("[data-cap-diff-mode]").forEach((btn) => {
    btn.addEventListener("click", () => {
      solveCapDiffMode = btn.dataset.capDiffMode;
      document.querySelectorAll("[data-cap-diff-mode]").forEach((b) => {
        const active = b === btn;
        b.classList.toggle("is-active", active);
        b.setAttribute("aria-checked", active ? "true" : "false");
      });
      // Re-rasterise whichever diff canvas is currently mounted.
      const capCanvas = document.querySelector("#solveCapDiffCanvas");
      const filCanvas = document.querySelector("#solveFilamentDiffCanvas");
      if (capCanvas) {
        const diff = getCurrentSolveCapDiffFromCache();
        if (diff) renderSolveCapDiffCanvas(capCanvas, diff, solveCapDiffMode);
      } else if (filCanvas) {
        const diff = getCurrentSolveFilamentDiffFromCache();
        if (diff) renderSolveCapDiffCanvas(filCanvas, diff, solveCapDiffMode);
      }
      updateSolveLegend();
    });
  });

  // Color Diff filament dropdown
  const filDiffSel = $("#solveFilamentDiffSelect");
  if (filDiffSel) {
    filDiffSel.addEventListener("change", () => {
      solveFilamentDiffId = filDiffSel.value || "";
      const selected = getSelectedRuns().filter((r) => r.results);
      if (selected.length === 2 && isSolveFilamentDiffView(solveView)) {
        renderSolveComparisonGrid();
      }
    });
  }

  // Export
  $("#exportFilesBtn").addEventListener("click", handleExportFiles);
  const exportOutputFormat = $("#exportOutputFormat");
  if (exportOutputFormat) exportOutputFormat.addEventListener("change", handleExportOptionChange);
  const exportGeometrySource = $("#exportGeometrySource");
  if (exportGeometrySource) exportGeometrySource.addEventListener("change", handleExportOptionChange);
  const exportFieldScale = $("#exportFieldScale");
  if (exportFieldScale) exportFieldScale.addEventListener("change", handleExportOptionChange);
  $("#copySwapBtn").addEventListener("click", () => {
    const text = $("#swapInstructions").textContent;
    navigator.clipboard.writeText(text).then(
      () => showToast("Copied to clipboard", "success"),
      () => showToast("Copy failed", "error")
    );
  });

  // Copy-path buttons in the export file list (delegated)
  const fileListDiv = $("#exportFileList");
  if (fileListDiv) {
    fileListDiv.addEventListener("click", (e) => {
      const openBtn = e.target.closest(".open-export-folder-btn[data-export-id]");
      if (openBtn) {
        e.preventDefault();
        openExportFolder(openBtn.dataset.exportId)
          .then(() => showToast("Opened export folder", "success"))
          .catch((err) => showToast(`The export folder could not be opened: ${err.message}`, "error"));
        return;
      }
      const btn = e.target.closest(".copy-path-btn[data-copy-path]");
      if (!btn) return;
      e.preventDefault();
      copyToClipboard(btn.dataset.copyPath);
    });
  }

  // Download All → zip of current output dir
  const downloadAllBtn = $("#downloadAllBtn");
  if (downloadAllBtn) {
    downloadAllBtn.addEventListener("click", () => {
      if (downloadAllBtn.disabled) return;
      const url = getSelectedExportResult()?.zip_url;
      if (!url) {
        showToast("Export files first", "error");
        return;
      }
      window.location.href = url;
    });
  }

  // ── Settings Profile controls ──────────────────────────────────────────
  const profileBrowse = $("#settingsProfileBrowseBtn");
  if (profileBrowse) profileBrowse.addEventListener("click", handleSettingsProfilesBrowse);
  const profileSave = $("#settingsProfileSaveBtn");
  if (profileSave) profileSave.addEventListener("click", handleSettingsProfileSave);
  const profileSaveAs = $("#settingsProfileSaveAsBtn");
  if (profileSaveAs) profileSaveAs.addEventListener("click", handleSettingsProfileSaveAs);

  // ── Printer configuration ────────────────────────────────────────────────
  const pcBtn = $("#printerConfigBtn");
  if (pcBtn) pcBtn.addEventListener("click", () => {
    const page = $("#printerConfigPage");
    if (page && !page.classList.contains("is-hidden")) {
      hidePrinterConfigPage();
    } else {
      showPrinterConfigPage();
    }
  });
  const pcClose = $("#printerConfigClose");
  if (pcClose) pcClose.addEventListener("click", () => hidePrinterConfigPage());
  // Click outside card (on the dimmed backdrop) closes config
  const pcPage = $("#printerConfigPage");
  if (pcPage) pcPage.addEventListener("click", (e) => {
    if (e.target === pcPage) hidePrinterConfigPage();
  });

  const pcNew = $("#pcNewPrinterBtn");
  if (pcNew) pcNew.addEventListener("click", () => {
    _readPrinterFromConfigPage();
    resetPrinterDeleteConfirm();
    const id = "printer-" + Date.now();
    printersData.printers.push({
      id,
      name: "New Printer",
      max_print_area: { x: 256, y: 256 },
      ams_units: 1,
      slots_per_ams: 4,
      nozzle_profiles: [
        { size: 0.4, min_layer_height: 0.08, max_layer_height: 0.32, ...defaultNozzleLineWidths(0.4) },
      ],
    });
    printerConfigEditingId = id;
    printersData.active_printer_id = id;
    printersData.active_nozzle_size = 0.4;
    renderPrinterConfigPage();
    // Focus the name field for immediate editing
    const nameField = $("#pcName");
    if (nameField) { nameField.focus(); nameField.select(); }
  });

  const pcDelete = $("#pcDeletePrinterBtn");
  if (pcDelete) pcDelete.addEventListener("click", async () => {
    const delId = currentPrinterConfigId();
    if (!delId) return;
    if (!printerDeleteConfirmPending) {
      printerDeleteConfirmPending = true;
      pcDelete.textContent = "Confirm?";
      pcDelete.classList.add("confirm-pending");
      pcDelete.title = "Click again to delete selected printer";
      printerDeleteConfirmTimer = setTimeout(resetPrinterDeleteConfirm, 2200);
      return;
    }
    resetPrinterDeleteConfirm();
    printersData.printers = printersData.printers.filter(p => p.id !== delId);
    printerConfigEditingId = printersData.printers[0]?.id || null;
    printersData.active_printer_id = printerConfigEditingId;
    syncPrinterConfigActiveNozzle();
    try {
      await savePrinters(printersData);
      await loadPrinters();
      printerConfigEditingId = printersData?.active_printer_id || printersData?.printers?.[0]?.id || null;
      renderPrinterConfigPage();
      showToast("Printer deleted", "success");
    } catch (e) {
      showToast("Delete failed: " + e.message, "error");
    }
  });

  const pcAddNozzle = $("#pcAddNozzleBtn");
  if (pcAddNozzle) pcAddNozzle.addEventListener("click", () => {
    const printer = printersData.printers.find(p => p.id === currentPrinterConfigId());
    if (!printer) return;
    // Read current table state before adding
    _readPrinterFromConfigPage();
    printer.nozzle_profiles.push({ size: 0.4, min_layer_height: 0.08, max_layer_height: 0.32, ...defaultNozzleLineWidths(0.4) });
    renderPrinterConfigPage();
  });

  // Keyboard: Escape closes drawers/modals/config page
  document.addEventListener("keydown", (e) => {
    const tag = e.target?.tagName;
    const isTypingTarget = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
    if (!isTypingTarget && ["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(e.key)) {
      if (navigateSolveLightbox(e.key)) {
        e.preventDefault();
        return;
      }
    }
    if (e.key === "Escape") {
      if (_lightboxIdx !== -1 || _solveLightboxState) { closeCompLightbox(); return; }
      const modelLibrariesModal = $("#modelLibrariesModal");
      if (modelLibrariesModal && !modelLibrariesModal.classList.contains("is-hidden")) {
        closeModelLibrariesModal();
        return;
      }
      const pcPage = $("#printerConfigPage");
      if (pcPage && !pcPage.classList.contains("is-hidden")) {
        hidePrinterConfigPage();
        return;
      }
      const ratioDialog = $("#ratioDialog");
      if (ratioDialog && ratioDialog.getAttribute("aria-hidden") === "false") {
        closeRatioDialog();
        return;
      }
      const libModal = $("#libraryModalBackdrop");
      if (libModal && !libModal.classList.contains("is-hidden")) {
        closeLibraryModal();
        return;
      }
      const profileModal = $("#settingsProfileModal");
      if (profileModal && !profileModal.classList.contains("is-hidden")) {
        $("#settingsProfileModalClose")?.click();
        return;
      }
      if (settingsDrawerOpen) {
        closeSettingsDrawer();
        return;
      }
      const drawer = $("#detailDrawer");
      if (drawer && drawer.getAttribute("aria-hidden") === "false") {
        closeDetailDrawer();
      }
    }
  });
}

// ── Data Loading ─────────────────────────────────────────────────────────────

async function loadFilaments() {
  try {
    const filaments = await fetchFilaments();
    allFilaments = filaments;
    apiConnected = true;
  } catch {
    allFilaments = [...STATIC_FILAMENTS];
    apiConnected = false;
  }
  // This is only an in-memory fallback until authoritative runtime-library
  // status arrives. Offline/recovery states must never write scoped choices.
  enabledFilamentRuntimeLibraryId = null;
  enabledFilamentPersistenceReady = false;
  applyEnabledFilamentSelection(getGenerationEligibleFilamentIds(), { persist: false, render: false });
}

async function loadImages() {
  try {
    availableImages = await fetchImages();
  } catch {
    availableImages = [];
  }
}

// ── Initialization ──────────────────────────────────────────��────────────────

async function init() {
  initAllEnhancedSliders();
  bindEvents();
  updateRail();
  renderSettingsTab();

  await loadFilaments();
  if (apiConnected) await loadModelLibraries({ openOnRecovery: true, silent: true });
  await loadPrinters();
  await loadSavedPalettes();
  if (apiConnected) {
    await loadImages();
    try {
      const session = await fetchSession();
      if (session.config) {
        Object.assign(config, session.config);
        // Deck is ephemeral — don't restore stale server palette.
        // Saved palettes can be loaded via the Load button.
        if (session.config.image_path) {
          selectedImage = availableImages.find((i) => i.filename === session.config.image_path);
        }
        if (session.config.frame) {
          const f = session.config.frame;
          frameState.widthMm = clampFrameWidth(f.width_mm ?? 100);
          frameState.heightMm = clampFrameHeight(f.height_mm ?? 100);
          frameState.scale = f.scale ?? 100;
          frameState.rotation = f.rotation ?? 0;
          frameState.panX = f.pan_x ?? 0;
          frameState.panY = f.pan_y ?? 0;
        }
      }
      if (session.solve) {
        solveStatus = session.solve;
        if (session.solve.status === "running") {
          const recoveredRun = createSolveRun(config.palette || [], { ...config });
          recoveredRun.id = session.solve.card_id || recoveredRun.id;
          solveRuns.push(recoveredRun);
          selectedRunIds.add(recoveredRun.id);
          activeSolveRunId = recoveredRun.id;
          activeSolveJobId = session.solve.job_id || null;
          solveCancelPending = !!session.solve.cancel_requested;
          startSolvePolling(recoveredRun);
        }
      }
    } catch { /* ignore */ }
  }

  // Apply saved Settings Profile after session restore so profile values survive server restart.
  await loadModules();
  // Apply the saved Settings Profile after module load so its module state is authoritative.
  await loadPresets();
  renderSettingsTab();
  initCollapsibleSections();

  // Re-render rail and library count now that data is loaded
  updateRail();
  updateLibraryFilterStatus();
  // The Image tab is the default visible tab but switchTab() never fires at startup, so render
  // its library grid now that availableImages is populated (otherwise it stays empty until the
  // first upload/tab-switch).
  renderImageTab();

  showToast(apiConnected ? "Connected to Prisma server" : "Offline mode \u2014 start server to enable full features", apiConnected ? "success" : "");
}

init();
