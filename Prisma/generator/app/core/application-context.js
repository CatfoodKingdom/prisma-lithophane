import { createLifecycle } from "./lifecycle.js";
import { createDomRegistry } from "./dom.js";
import { createEventBus } from "./events.js";
import { createPersistence } from "./persistence.js";
import { createStore } from "./store.js";

/**
 * @param {{api: Record<string, Function>, data: Object, services: Object}} dependencies
 * @returns {import("./types.js").ApplicationContext}
 */
export function createApplicationContext({ api, data, services, root = document, storage = localStorage }) {
  const store = createStore({
    session: {}, image: {}, palette: {}, settings: {}, solve: {}, export: {}, guides: {}, ui: {},
  });
  return {
    api, data, services, commands: {},
    dom: createDomRegistry(root),
    events: createEventBus(),
    lifecycle: createLifecycle(),
    persistence: createPersistence(storage),
    store,
    state: store.getState(),
  };
}

/** @param {import("./types.js").ApplicationContext} app */
export function initializeApplicationState(app) {
  app.state.ui.currentTab = "image";
  app.state.ui.themePreference = "system";
  app.state.ui.themeResolved = "light";
  app.state.guides.runtimeState = "idle";
  app.state.guides.targetUnavailable = false;
  app.state.guides.onboardingState = {
    schema_version: 2,
    revision: 0,
    welcome_status: "not_offered",
  };
  app.state.guides.onboardingPersistenceAvailable = false;
  app.state.guides.forceGuidedSetup = false;
  app.state.guides.currentGuide = null;
  app.state.guides.currentStep = null;
  app.state.guides.completedStepIds = new Set();
  app.state.guides.reviewingCompletedStep = false;
  app.state.guides.runtimeContext = null;
  app.state.guides.workspaceLeaseId = null;
  app.state.guides.workspaceSessionId = null;
  app.state.guides.actionLedger = new Set();
  app.state.guides.presentationSnapshot = null;
  app.state.guides.dockIndex = null;
  app.state.settings.settingsDrawerOpen = false;
  app.state.settings.SETTINGS_ADVANCED_VISIBLE_STORAGE_KEY = "prisma_settings_advanced_visible";
  app.state.settings.COLOR_CAP_MODE_STORAGE_KEY = "prisma_color_cap_mode";
  app.state.ui.LEGACY_ENABLED_FILAMENTS_STORAGE_KEY = "prisma_enabled_filaments";
  app.state.ui.ENABLED_FILAMENTS_STORAGE_KEY = "prisma_enabled_filaments_by_library";
  app.state.ui.ENABLED_FILAMENTS_STORAGE_VERSION = 1;
  app.state.settings.settingsAdvancedVisible = app.commands.loadSettingsAdvancedVisible();
  app.state.solve.solveRunHoverTimer = null;
  app.state.solve.solveRunHoverCloseTimer = null;
  app.state.solve.solveRunHoverPendingRunId = null;
  app.state.solve.solveRunHoverPreviewEl = null;
  app.state.solve.solveRunHoverRunId = null;
  app.state.solve.solveRunSettingsPanelEl = null;
  app.state.solve.solveRunSettingsPanelRunId = null;
  app.state.solve.solveRunSettingsPanelContext = null;
  app.state.solve.solveRunSettingsAdvancedVisible = false;
  app.state.session.apiConnected = false;
  app.state.session.clearTempRunning = false;
  app.state.session.modelLibraryAutoOpened = false;
  app.state.session.modelLibraryManager = {
  status: null,
  selectedKey: null,
  loading: false,
  busy: false,
  restarting: false,
  message: "",
  messageKind: "",
  error: "",
};
  app.state.session.allFilaments = [...app.data.STATIC_FILAMENTS];
  app.state.session.DEFAULT_BASE_FILAMENT = "bambu-tough-white";
  app.state.palette.enabledFilaments = new Set(
  app.data.STATIC_FILAMENTS.filter(app.commands.isGenerationEligibleFilament).map(filament => filament.filament_id),
);
  app.state.palette.enabledFilamentRuntimeLibraryId = null;
  app.state.palette.enabledFilamentPersistenceReady = false;
  app.state.image.availableImages = [];
  app.state.image.guideImages = [];
  app.state.image.selectedImage = null;
  app.state.image.pendingSelectedFilename = null;
  app.state.image.activeImportBatchId = null;
  app.state.image.importBatch = null;
  app.state.image.importPollingError = "";
  app.state.image.frameState = {
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
  app.state.image.lastTouchedDim = "width";
  app.state.image.widthLocked = false;
  app.state.image.heightLocked = false;
  app.state.image.panDragState = null;
  app.state.image.frameDragState = null;
  app.state.image.frameEditorTab = "size";
  app.state.image.libraryPaneState = "contracted";
  app.state.image.imageDirection = "landscape";
  app.state.image.imageAdjust = {
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
  app.state.palette.creationMode = "auto";
  app.state.palette.candidateSelection = new Set();
  app.state.palette.candidateInitialized = false;
  app.state.palette.manualSlots = [];
  app.state.palette.manualVariantDraft = null;
  app.state.palette.composerPalette = [];
  app.state.palette.deck = [];
  app.state.palette.stagingDeck = [];
  app.state.palette.stagingClearConfirmPending = false;
  app.state.palette.stagingClearConfirmTimer = null;
  app.state.palette.activeDeckId = null;
  app.state.solve.solveRuns = [];
  app.state.solve.solveRunCounter = 0;
  app.state.solve.loadedRunApplyRunning = false;
  app.state.solve.selectedRunIds = new Set();
  app.state.solve.solveShowSourceImage = true;
  app.state.solve.solveColorRegionsView = "color_ceiling";
  app.state.solve.solveColorRegionsViewWasExplicitlySelected = false;
  app.state.solve.solveWhiteCapView = "cap_map";
  app.state.solve.solveContoursEnabled = true;
  app.state.solve.solveAdvancedViewsOpen = false;
  app.state.solve.solveCapDiffMode = "changed";
  app.state.solve.solveFilamentDiffId = "";
  app.state.solve.savedRunRowsCache = [];
  app.state.ui.selectedSavedRunKey = null;
  app.state.solve.savedRunsModalMode = "run";
  app.state.solve.savedRunDeleteConfirmPending = false;
  app.state.solve.savedRunDeleteConfirmTimer = null;
  app.state.solve.solveHistoryClearConfirmPending = false;
  app.state.solve.solveHistoryClearConfirmTimer = null;
  app.state.solve.solveRunDeleteArmedId = null;
  app.state.solve.solveRunDeleteConfirmTimer = null;
  app.state.palette.nextDeckNum = 1;
  app.state.palette.savedPalettesData = null;
  app.state.palette.railDeckHoverTimer = null;
  app.state.palette.railDeckHoverCloseTimer = null;
  app.state.palette.railDeckHoverPreviewEl = null;
  app.state.palette.railDeckHoverPreviewCardId = null;
  app.state.palette.railDeckHoverPendingCardId = null;
  app.state.session.printerConfigOriginTab = null;
  app.state.session.printerConfigEditingId = null;
  app.state.session.printerConfigDraft = null;
  app.state.session.printerConfigOriginal = null;
  app.state.session.printerConfigDeletedNozzles = [];
  app.state.session.printerDeleteConfirmPending = false;
  app.state.session.printerDeleteConfirmTimer = null;
  app.state.session.printerConfig = {
  name: "Bambu X1C",
  max_x_mm: 256,
  max_y_mm: 256,
  ams_units: 1,
  slots_per_unit: 4,
  ams_slots: 4,
  white_slots: 1,
};
  app.state.session.printersData = null;
  app.state.session.activeNozzle = null;
  app.state.session.activeExtrusionWidth = null;
  app.state.session.activePrintability = null;
  app.state.session.resolvedPrintSetup = null;
  app.state.settings.settingsProfiles = [];
  app.state.settings.settingsContract = null;
  app.state.settings.settingsSpecs = {};
  app.state.settings.settingsContractAvailable = false;
  app.state.settings.settingsEvaluation = null;
  app.state.settings.settingsEvaluationFingerprint = null;
  app.state.settings.settingsEvaluationGeneration = 0;
  app.state.settings.settingsEvaluationPresentationCurrent = false;
  app.state.settings.temporarySettingsProfile = null;
  app.state.settings.loadedProfileRef = null;
  app.state.settings.loadedProfileSnapshot = null;
  app.state.settings.userDefaultProfileId = null;
  app.state.ui.MODULE_POSTURE = {};
  app.state.ui.MODULE_DISPLAY = {
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
  app.state.settings.config = {
  max_dim_mm: 130,
  solve_pitch_extrusion_width_multiplier: 1,
  image_sample_pitch_mm: 0.20,
  solver_fine_pitch_mm: 0.20,
  detail_cap_enabled: true,
  emit_pressure_diagnostics: false,
  emit_geometry_attribution: false,
  emit_blueprint_printability: true,
  printability_extrusion_width_mm: null,
  printability_minimum_line_length_mm: null,
  enforce_printability: true,
  color_region_target_from_printability: true,
  color_region_target_width_multiplier: 2.0,
  stage2_final_printability_gate_fine_override: true,
  stage2_printability_gate_fine_override: true,
  stage2_printability_repair_fine_override: true,
  stage4_printability_gate_detail: true,
  border: false,
  border_width_mm: 3.0,
  border_height_mm: 3.0,
  // Color corrections are mandatory in the Generator-facing product contract.
  use_corrections: true,
  cap_continuity_cleanup: true,
  // Camera Transform ingress is mandatory for current solves.
  model_domain_ingress: true,
  luminance_handler_enabled: false,
  luminance_handler_mode: "boundary_prior",
  luminance_handler_strength: 1.0,
  luminance_handler_optical_authority_fraction: 0.75,
  luminance_handler_boundary_percentile: 95.0,
  luminance_handler_boundary_sigma_px: null,
  luminance_handler_response_curve: "linear",
  luminance_handler_response_gamma: 1.0,
  luminance_handler_detail_residual: true,
  luminance_handler_include_solver_detail: true,
};
  app.state.settings.lastColorCapMode = null;
  app.state.settings.capModeForcedByLuminance = false;
  app.state.ui.CHROMA_WEIGHT_SLIDER_MIN = -3;
  app.state.ui.CHROMA_WEIGHT_SLIDER_MAX = 3;
  app.state.solve.solveStatus = { status: "idle", progress: "", elapsed_s: 0, result: null };
  app.state.solve.solvePollingOwner = null;
  app.state.solve.activeSolveRunId = null;
  app.state.solve.activeSolveJobId = null;
  app.state.solve.solveStartPending = false;
  app.state.solve.solveCancelPending = false;
  app.state.solve.solveProgressHideTimer = null;
  app.state.solve.solveMode = "single";
  app.state.solve.solveModeMenuOpen = false;
  app.state.solve.batchSelectedDeckIds = new Set();
  app.state.solve.batchLockedDeckIds = new Set();
  app.state.solve.batchDeckLocked = false;
  app.state.solve.batchRecoveryOwnsToolbar = false;
  app.state.solve.paletteBatchStartPending = false;
  app.state.solve.paletteBatchResultFetches = new Map();
  app.state.solve.paletteBatchFetchedResultIds = new Set();
  app.state.export.exportRunning = false;
  app.state.export.exportPollingOwner = null;
  app.state.export.exportSelectedRunId = null;
  app.state.export.activeExportRunId = null;
  app.state.solve.solveView = "predicted";
  app.state.ui._suggestPolling = null;
  app.state.ui.activeSuggestJobId = null;
  app.state.palette.suggestCancelPending = false;
  app.state.ui.$ = (sel) => document.querySelector(sel);
  app.state.ui.$$ = (sel) => document.querySelectorAll(sel);
  app.state.ui._opAbort = null;
  app.state.ui._opTimer = null;
  app.state.ui._opStartTime = 0;
  app.state.ui._opLastElapsedSeconds = 0;
  app.state.export.activeExportJobId = "";
  app.state.export.exportCancelPending = false;
  app.state.ui.IMAGE_ASPECT_SHORT_SIDE_MM = 120;
  app.state.solve._lightboxIdx = -1;
  app.state.ui.SYSTEM_SETTINGS_PROFILE_ID = "system-default";
  app.state.ui.SYSTEM_SETTINGS_PROFILE_NAME = "Basic";
  app.state.settings.SETTINGS_PROFILE_FORBIDDEN_NAME_CHARS = new Set('<>:"/\\|?*'.split(""));
  app.state.settings.SETTINGS_PROFILE_KEYS = [];
  app.state.settings.SETTINGS_PROFILE_DEFAULTS = {};
  app.state.settings._configSyncChain = Promise.resolve();
  app.state.ui.surfaceDataCache = {};
  app.state.solve.solveContourDataCache = {};
  app.state.ui.explorerMaterialDataCache = {};
  app.state.ui.capThicknessCache = {};
  app.state.ui.filamentThicknessCache = {};
  app.state.ui.SOLVE_DIFF_SETTING_LABELS = {
  solve_pitch_extrusion_width_multiplier: "Solve Pitch",
  layer_height: "Layer Height",
  d_wb: "White Base Thickness",
  min_cap_layers: "Min Cap Layers",
  t_max: "Max Thickness",
  k_max: "Max Colors per Region",
  de_threshold: "Color Mismatch Tolerance",
  chroma_weight: "Chroma Weight",
  luminance_mode: "Luminance Mode",
  cell_mode: "Region Method",
  stage1_coarsening_factor: "Region Planning Scale",
  neutral_field_protection_enabled: "Neutral-field protection",
  neutral_field_protection_cutoff: "Neutral Chroma Cutoff",
  color_region_target_mm: "Color Region Target",
  smooth_radius_mm: "Cliff Smooth Radius",
  hybrid_split_ratio: "Hybrid Split Ratio",
  detail_cap_smoothing_enabled: "Detail Cap Smoothing",
  detail_cap_smoothing_exact_speckle_max_px: "Detail Exact Speckle Cleanup",
  detail_cap_smoothing_cumulative_component_max_px: "Detail Island Cleanup",
  detail_cap_smoothing_cumulative_hole_max_px: "Detail Hole Cleanup",
  cap_mode: "Boundary Cap Style",
  boundary_cap_de_budget: "Boundary Cap Appearance Budget",
  boundary_cap_smoothing_radius_mm: "Smoothing Radius",
};
  app.state.ui.SOLVE_DIFF_CATEGORY_ORDER = [
  "geometry",
  "preprocessing",
  "solver",
  "white-cap",
  "other",
];
  app.state.ui.SOLVE_DIFF_CATEGORY_TITLES = {
  geometry: "Changed Settings · Essentials",
  preprocessing: "Changed Settings · Preprocessing",
  solver: "Changed Settings · Color Solver",
  "white-cap": "Changed Settings · White Cap",
  other: "Changed Settings · Other",
};
  app.state.ui.READ_ONLY_RUN_SETTING_SECTIONS = [
  {
    key: "essentials",
    title: "Essentials",
    rows: [
      { key: "luminance_mode", controlId: "cfgLuminanceMode", label: "Solve Mode", format: "solve-mode" },
      { key: "solve_pitch_extrusion_width_multiplier", runKey: "solver_fine_pitch_mm", controlId: "cfgSolvePitch", fallbackKey: "image_sample_pitch_mm", label: "Solve Pitch", unit: "mm" },
      { key: "layer_height", controlId: "cfgLayerHeight", label: "Layer Height", unit: "mm" },
      { key: "t_max", controlId: "cfgTMax", label: "Max Thickness", unit: "mm" },
      { key: "d_wb", controlId: "cfgDWb", label: "Base Thickness", unit: "mm" },
      { key: "min_cap_layers", controlId: "cfgDWcMin", label: "Min Cap Layers", unit: "layers" },
      { key: "base_filament", controlId: "cfgBaseFilament", label: "Base/Cap Filament", format: "filament" },
    ],
  },
  {
    key: "preprocessing",
    title: "Preprocessing",
    rows: [
      { key: "source_resample_kernel", controlId: "cfgSourceResampleKernel", label: "Resample kernel", format: "title", advanced: true },
    ],
  },
  {
    key: "solver",
    title: "Color Solver",
    rows: [
      { key: "appearance_model_provider", controlId: "cfgAppearanceModelProvider", label: "Appearance Model", format: "appearance-model", advanced: true },
      { key: "gamut_white_rescale", controlId: "cfgGamutWhiteRescale", label: "White-point rescale" },
      { key: "k_max", controlId: "cfgKMax", label: "Max colors per region" },
      { key: "gamut_mode", controlId: "cfgGamutMode", label: "Out-of-gamut handling", format: "gamut-mode", advanced: true, group: "Color Matching" },
      { key: "de_threshold", controlId: "cfgDeThreshold", label: "Color mismatch tolerance", unit: "dE", advanced: true, group: "Color Matching" },
      { key: "chroma_weight", controlId: "cfgChromaWeight", label: "Chroma weight", inputTransform: "log2", advanced: true, group: "Color Matching" },
      { key: "cell_mode", controlId: "cfgCellMode", label: "Region method", format: "region-method", advanced: true, group: "Region Planning" },
      { key: "stage1_coarsening_factor", controlId: "cfgStage1Coarsening", label: "Region planning scale", format: "region-scale", advanced: true, group: "Region Planning" },
      { key: "color_region_target_mm", controlId: "cfgColorRegionTarget", label: "Color region target", unit: "mm", group: "Region Planning" },
      { key: "neutral_field_protection_enabled", controlId: "cfgNeutralFieldProtectionEnabled", label: "Neutral-field protection", format: "enabled", group: "Region Refinement" },
      { key: "neutral_field_protection_cutoff", controlId: "cfgNeutralFieldProtectionCutoff", label: "Neutral chroma cutoff", advanced: true, group: "Region Refinement" },
      { key: "stage2_fine_override_enabled", controlId: "cfgStage2FineOverride", label: "Local recipe corrections", advanced: true, group: "Region Refinement" },
      { key: "stage2_boundary_mutation_enabled", controlId: "cfgStage2BoundaryMutation", label: "Boundary mutation", advanced: true, group: "Region Refinement" },
      { key: "stage2_boundary_mutation_max_passes", controlId: "cfgStage2BoundaryMutationMaxPasses", label: "Mutation passes", advanced: true, group: "Region Refinement" },
      { key: "stage2_boundary_mutation_min_gain", controlId: "cfgStage2BoundaryMutationMinGain", label: "Mutation min gain", unit: "dE", advanced: true, group: "Region Refinement" },
    ],
  },
  {
    key: "white-cap",
    title: "White Cap",
    rows: [
      { key: "boundary_cap_smoothing_radius_mm", controlId: "cfgSmoothKernel", label: "Smoothing radius", unit: "mm", group: "Boundary Cap" },
      { key: "detail_cap_max_layers", controlId: "cfgDetailCapMaxLayers", label: "Max Detail Layers", unit: "layers", group: "Detail Cap" },
      { key: "cap_mode", controlId: "cfgCapMode", label: "Boundary cap style", format: "cap-mode", advanced: true, group: "Boundary Cap" },
      { key: "boundary_cap_de_budget", controlId: "cfgBoundaryCapDeBudget", label: "Appearance budget", unit: "dE", advanced: true, group: "Boundary Cap" },
      { key: "luminance_base_shading_limit_fraction", controlId: "cfgBaseShadingLimit", label: "Shading balance", format: "percent", group: "Boundary Cap" },
    ],
  },
  ];
  // The drawer and saved-run review both consume this descriptor. Keep the
  // alias explicit so future presentation code cannot quietly introduce a
  // second, divergent settings vocabulary.
  app.state.ui.SETTINGS_PRESENTATION = app.state.ui.READ_ONLY_RUN_SETTING_SECTIONS;
  app.state.ui.SETTINGS_PRESENTATION_VERSION = 5;
  app.state.ui.DIAGNOSTIC_PALETTE_INFERNO = "inferno-v1";
  app.state.ui.DIAGNOSTIC_PALETTE_LEGACY = "legacy-approximate";
  app.state.ui.INFERNO_RGB8_HEX = "00000401000501010601010802010a02020c02020e03021004031204031405041706041907051b08051d09061f0a07220b07240c08260d08290e092b10092d110a30120a32140b34150b37160b39180c3c190c3e1b0c411c0c431e0c451f0c48210c4a230c4c240c4f260c51280b53290b552b0b572d0b592f0a5b310a5c320a5e340a5f3609613809623909633b09643d09653e0966400a67420a68440a68450a69470b6a490b6a4a0c6b4c0c6b4d0d6c4f0d6c510e6c520e6d540f6d550f6d57106e59106e5a116e5c126e5d126e5f136e61136e62146e64156e65156e67166e69166e6a176e6c186e6d186e6f196e71196e721a6e741a6e751b6e771c6d781c6d7a1d6d7c1d6d7d1e6d7f1e6c801f6c82206c84206b85216b87216b88226a8a226a8c23698d23698f24699025689225689326679526679727669827669a28659b29649d29649f2a63a02a63a22b62a32c61a52c60a62d60a82e5fa92e5eab2f5ead305dae305cb0315bb1325ab3325ab43359b63458b73557b93556ba3655bc3754bd3853bf3952c03a51c13a50c33b4fc43c4ec63d4dc73e4cc83f4bca404acb4149cc4248ce4347cf4446d04545d24644d34743d44842d54a41d74b3fd84c3ed94d3dda4e3cdb503bdd513ade5238df5337e05536e15635e25734e35933e45a31e55c30e65d2fe75e2ee8602de9612bea632aeb6429eb6628ec6726ed6925ee6a24ef6c23ef6e21f06f20f1711ff1731df2741cf3761bf37819f47918f57b17f57d15f67e14f68013f78212f78410f8850ff8870ef8890cf98b0bf98c0af98e09fa9008fa9207fa9407fb9606fb9706fb9906fb9b06fb9d07fc9f07fca108fca309fca50afca60cfca80dfcaa0ffcac11fcae12fcb014fcb216fcb418fbb61afbb81dfbba1ffbbc21fbbe23fac026fac228fac42afac62df9c72ff9c932f9cb35f8cd37f8cf3af7d13df7d340f6d543f6d746f5d949f5db4cf4dd4ff4df53f4e156f3e35af3e55df2e661f2e865f2ea69f1ec6df1ed71f1ef75f1f179f2f27df2f482f3f586f3f68af4f88ef5f992f6fa96f8fb9af9fc9dfafda1fcffa4";
  app.state.ui.INFERNO_RGB8 = Array.from({ length: 256 }, (_, index) => {
  const offset = index * 6;
  return [0, 2, 4].map(channel => parseInt(app.state.ui.INFERNO_RGB8_HEX.slice(offset + channel, offset + channel + 2), 16));
});
  app.state.ui.SURFACE_CONTOUR_SCALE = 3;
  app.state.ui.SOLVE_CONTOUR_STROKE = "rgba(0, 0, 0, 0.72)";
  app.state.solve._solveHighpassControlsBound = false;
  app.state.solve._solveExplorerControlsBound = false;
  app.state.ui.COLOR_FLOOR_FILL = [128, 128, 128];
  app.state.ui.SOLVE_REMOVED_VIEWS = new Set([
  "de_perceptual", "diagnostic_views",
  "printability_hard_fail", "stage2_boundary_mutation", "printability_width_loss",
]);
  app.state.ui.SOLVE_ADVANCED_VIEWS = new Set(["thickness_maps", "surface_highpass"]);
  app.state.solve._solveLightboxState = null;
  app.state.solve._lightboxCleanup = null;
  app.state.solve._lightboxInstanceToken = 0;
  app.state.ui.recipeDataCache = {};
  app.state.ui.recipeDataPromiseCache = {};
  app.state.ui.recipeCookbookPromiseCache = {};
  app.state.ui.recipeDataGeneration = {};
  app.state.settings.moduleData = [];
  app.state.settings.moduleState = {};
  app.state.ui.MODULE_UI_VISIBLE_SLOTS = new Set(["preprocessing"]);
  app.state.ui._resizeTimer = undefined;
  return app.state;
}
