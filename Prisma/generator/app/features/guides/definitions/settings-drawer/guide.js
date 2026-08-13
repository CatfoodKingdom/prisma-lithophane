import {
  chapter,
  completion,
  detour,
  freezeGuide,
  step,
} from "../../core/schema.js?v=2026-08-12-guide-companion-v4";
import { SETTINGS_DRAWER_COPY } from "./content.js?v=2026-08-11-settings-ia-v1";
import {
  SETTINGS_DRAWER_ALL_MODULES_OFF,
  SETTINGS_DRAWER_BASELINE,
  SETTINGS_DRAWER_MODULE_PRESETS,
  settingsDrawerModuleMap,
} from "./state.js";

const ensureAction = (action, input) => Object.freeze({
  action,
  input: Object.freeze(input),
  ensure_on_entry: true,
});

const moduleActions = (moduleId = null) => Object.freeze([
  ensureAction("settings.override", {
    values: Object.freeze({
      preprocessing_params: moduleId
        ? Object.freeze({ [moduleId]: SETTINGS_DRAWER_MODULE_PRESETS[moduleId] })
        : Object.freeze({}),
    }),
  }),
  ensureAction("settings.set_module_state", {
    state: moduleId ? settingsDrawerModuleMap(moduleId) : SETTINGS_DRAWER_ALL_MODULES_OFF,
  }),
]);

const TARGETS = Object.freeze({
  "settings-drawer.essentials.stack": "settings-drawer.essentials.stack",
  "settings-drawer.essentials.solve-mode": "settings-drawer.essentials.solve-mode",
  "settings-drawer.essentials.solve-mode-choice": "settings-drawer.essentials.solve-mode",
  "settings-drawer.essentials.solve-pitch": "settings-drawer.essentials.solve-pitch",
  "settings-drawer.essentials.solve-pitch-matching": "settings-drawer.essentials.solve-pitch",
  "settings-drawer.essentials.max-total-thickness": "settings-drawer.essentials.max-total-thickness",
  "settings-drawer.essentials.thickness-budget": "settings-drawer.essentials.thickness-budget",
  "settings-drawer.essentials.layer-height": "settings-drawer.essentials.layer-height",
  "settings-drawer.essentials.layer-height-tradeoff": "settings-drawer.essentials.layer-height",
  "settings-drawer.essentials.white-filament": "settings-drawer.essentials.white-filament",
  "settings-drawer.essentials.base-thickness": "settings-drawer.essentials.base-thickness",
  "settings-drawer.essentials.min-cap-layers": "settings-drawer.essentials.min-cap-layers",
  "settings-drawer.preprocessing.resample-kernel": "settings-drawer.preprocessing.resample-kernel",
  "settings-drawer.preprocessing.order": "settings-drawer.preprocessing.order",
  "settings-drawer.preprocessing.noise-reduction": "settings-drawer.preprocessing.noise-reduction",
  "settings-drawer.preprocessing.print-scale-smoothing": "settings-drawer.preprocessing.print-scale-smoothing",
  "settings-drawer.preprocessing.flat-area-smoothing": "settings-drawer.preprocessing.flat-area-smoothing",
  "settings-drawer.preprocessing.palette-tone-fit": "settings-drawer.preprocessing.palette-tone-fit",
  "settings-drawer.preprocessing.palette-saturation-fit": "settings-drawer.preprocessing.palette-saturation-fit",
  "settings-drawer.solver.appearance-model": "settings-drawer.solver.appearance-model",
  "settings-drawer.solver.white-point-rescale": "settings-drawer.solver.white-point-rescale",
  "settings-drawer.solver.max-colors": "settings-drawer.solver.max-colors",
  "settings-drawer.solver.mismatch-tolerance": "settings-drawer.solver.mismatch-tolerance",
  "settings-drawer.solver.out-of-gamut": "settings-drawer.solver.out-of-gamut",
  "settings-drawer.solver.chroma-weight": "settings-drawer.solver.chroma-weight",
  "settings-drawer.solver.region-method": "settings-drawer.solver.region-method",
  "settings-drawer.solver.region-target": "settings-drawer.solver.region-target",
  "settings-drawer.solver.planning-scale": "settings-drawer.solver.planning-scale",
  "settings-drawer.solver.neutral-field": "settings-drawer.solver.neutral-field",
  "settings-drawer.solver.local-corrections": "settings-drawer.solver.local-corrections",
  "settings-drawer.solver.boundary-mutation": "settings-drawer.solver.boundary-mutation",
  "settings-drawer.solver.mutation-controls": "settings-drawer.solver.mutation-controls",
  "settings-drawer.white-cap.cap-style": "settings-drawer.white-cap.cap-style",
  "settings-drawer.white-cap.appearance-budget": "settings-drawer.white-cap.appearance-budget",
  "settings-drawer.white-cap.smoothing-radius": "settings-drawer.white-cap.smoothing-radius",
  "settings-drawer.white-cap.detail-depth": "settings-drawer.white-cap.detail-depth",
  "settings-drawer.luminance.what-changes": "settings-drawer.essentials.solve-mode",
  "settings-drawer.luminance.drawer-changes": "settings-drawer.luminance.drawer-changes",
  "settings-drawer.luminance.max-total-thickness": "settings-drawer.essentials.max-total-thickness",
  "settings-drawer.luminance.white-filament": "settings-drawer.essentials.white-filament",
  "settings-drawer.luminance.base-thickness": "settings-drawer.essentials.base-thickness",
  "settings-drawer.luminance.preprocessing": "settings-drawer.luminance.preprocessing",
  "settings-drawer.luminance.appearance-model": "settings-drawer.solver.appearance-model",
  "settings-drawer.luminance.white-point-rescale": "settings-drawer.solver.white-point-rescale",
  "settings-drawer.luminance.chroma-weight": "settings-drawer.solver.chroma-weight",
  "settings-drawer.luminance.region-controls": "settings-drawer.luminance.region-controls",
  "settings-drawer.luminance.min-cap-layers": "settings-drawer.essentials.min-cap-layers",
  "settings-drawer.luminance.shading-balance": "settings-drawer.luminance.shading-balance",
  "settings-drawer.luminance.shading-balance-suggest": "settings-drawer.luminance.shading-balance-suggest",
  "settings-drawer.luminance.smoothing-radius": "settings-drawer.white-cap.smoothing-radius",
  "settings-drawer.luminance.detail-depth": "settings-drawer.white-cap.detail-depth",
});

const PLACEMENT_GROUPS = Object.freeze({
  essentials: "sd-essentials",
  preprocessing: "sd-preprocessing",
  "solver.appearance-model": "sd-solver-matching",
  "solver.white-point-rescale": "sd-solver-matching",
  "solver.max-colors": "sd-solver-matching",
  "solver.mismatch-tolerance": "sd-solver-matching",
  "solver.out-of-gamut": "sd-solver-matching",
  "solver.chroma-weight": "sd-solver-matching",
  "solver.region-method": "sd-solver-regions",
  "solver.region-target": "sd-solver-regions",
  "solver.planning-scale": "sd-solver-regions",
  "solver.neutral-field": "sd-solver-refinement",
  "solver.local-corrections": "sd-solver-refinement",
  "solver.boundary-mutation": "sd-solver-refinement",
  "solver.mutation-controls": "sd-solver-refinement",
  "white-cap": "sd-white-cap",
  "luminance.what-changes": "sd-essentials",
  "luminance.drawer-changes": "sd-white-cap",
  "luminance.max-total-thickness": "sd-essentials",
  "luminance.base-thickness": "sd-essentials",
  "luminance.min-cap-layers": "sd-essentials",
  "luminance.white-filament": "sd-essentials",
  "luminance.preprocessing": "sd-preprocessing",
  "luminance.appearance-model": "sd-solver-matching",
  "luminance.white-point-rescale": "sd-solver-matching",
  "luminance.chroma-weight": "sd-solver-matching",
  "luminance.region-controls": "sd-solver-refinement",
  "luminance.shading-balance": "sd-white-cap",
  "luminance.shading-balance-suggest": "sd-white-cap",
  "luminance.smoothing-radius": "sd-white-cap",
  "luminance.detail-depth": "sd-white-cap",
});

function placementGroupFor(id) {
  const suffix = id.replace("settings-drawer.", "");
  if (suffix.startsWith("essentials.")) return PLACEMENT_GROUPS.essentials;
  if (suffix.startsWith("preprocessing.")) return PLACEMENT_GROUPS.preprocessing;
  if (suffix.startsWith("white-cap.")) return PLACEMENT_GROUPS["white-cap"];
  return PLACEMENT_GROUPS[suffix] || null;
}

function buildStep(id, options = {}) {
  const copy = SETTINGS_DRAWER_COPY[id];
  const target = Object.prototype.hasOwnProperty.call(options, "target")
    ? options.target
    : TARGETS[id];
  const targetless = target == null;
  return step({
    id,
    ...copy,
    target: targetless ? null : target,
    ...(targetless ? {
      overlayMode: "full-scrim",
      viewportAnchor: "center",
      placements: [],
    } : {
      placementGroup: placementGroupFor(id),
      placements: ["left", "bottom", "top", "right"],
    }),
    ...options,
  });
}

const SPINE = Object.freeze([
  buildStep("settings-drawer.intro", { allowPrevious: false, allowSkip: false }),
  buildStep("settings-drawer.enable-advanced", {
    target: "settings-drawer.open-and-advanced",
    reveal: "settings-drawer.topbar-settings",
    placementGroup: "sd-drawer-header",
    viewportAnchor: "center",
    allowSkip: false,
    completionMode: completion("event", {
      events: ["settings.opened", "settings.advanced-changed"],
      predicate_id: "settings.drawer-open-and-advanced",
      accept_preexisting: true,
      auto_advance: true,
    }),
    completeActions: [
      { action: "presentation.lock", input: { locks: ["settings-drawer-open", "settings-advanced-on"] } },
    ],
  }),
  buildStep("settings-drawer.chapters", {
    nextLabel: "Finish",
    allowEnd: false,
    detourLayout: "button-description",
    cardSize: "wide",
    enterActions: [
      ensureAction("settings.override", { values: SETTINGS_DRAWER_BASELINE }),
      ensureAction("settings.set_module_state", { state: SETTINGS_DRAWER_ALL_MODULES_OFF }),
    ],
  }),
]);

const ESSENTIAL_IDS = Object.freeze([
  "intro", "stack", "solve-mode", "solve-mode-choice", "solve-pitch",
  "solve-pitch-matching", "layer-height", "layer-height-tradeoff",
  "max-total-thickness", "thickness-budget", "base-thickness",
  "min-cap-layers", "white-filament",
].map(leaf => `settings-drawer.essentials.${leaf}`));

const PREPROCESSING_IDS = Object.freeze([
  "intro", "resample-kernel", "order", "noise-reduction",
  "print-scale-smoothing", "flat-area-smoothing", "palette-tone-fit",
  "palette-saturation-fit",
].map(leaf => `settings-drawer.preprocessing.${leaf}`));

const SOLVER_IDS = Object.freeze([
  "intro", "appearance-model", "white-point-rescale", "max-colors",
  "mismatch-tolerance", "out-of-gamut", "chroma-weight", "region-method",
  "region-target", "planning-scale", "neutral-field", "local-corrections",
  "boundary-mutation", "mutation-controls",
].map(leaf => `settings-drawer.solver.${leaf}`));

const WHITE_CAP_IDS = Object.freeze([
  "intro", "cap-style", "appearance-budget", "smoothing-radius", "detail-depth",
].map(leaf => `settings-drawer.white-cap.${leaf}`));

const LUMINANCE_IDS = Object.freeze([
  "intro", "what-changes", "drawer-changes", "max-total-thickness",
  "base-thickness", "min-cap-layers", "white-filament", "preprocessing",
  "appearance-model", "white-point-rescale", "chroma-weight", "region-controls",
  "shading-balance", "shading-balance-suggest", "smoothing-radius", "detail-depth",
].map(leaf => `settings-drawer.luminance.${leaf}`));

const PREPROCESSING_MODULE_BY_STEP = Object.freeze({
  "settings-drawer.preprocessing.noise-reduction": "a1_bilateral_denoise",
  "settings-drawer.preprocessing.print-scale-smoothing": "b1_printscale_bilateral",
  "settings-drawer.preprocessing.flat-area-smoothing": "b3_tv_flatten",
  "settings-drawer.preprocessing.palette-tone-fit": "c1_achievable_tonemap",
  "settings-drawer.preprocessing.palette-saturation-fit": "c2_soft_gamut_compress",
});

const ESSENTIAL_STEPS = Object.freeze(ESSENTIAL_IDS.map(id => buildStep(id, {
  ...(id === "settings-drawer.essentials.stack" ? {
    target: null,
    companion: {
      layout: "single",
      preferredPlacements: ["right", "left", "bottom", "top"],
      items: [{
        type: "image",
        src: "/assets/guides/settings-drawer/lithophane-stack-placeholder-v1.svg",
        alt: "Cross-section of a lithophane showing its white base, color layers, white cap, layer height, pixel size, and maximum thickness",
        expandable: true,
      }],
    },
  } : {}),
})));
const PREPROCESSING_STEPS = Object.freeze(PREPROCESSING_IDS.map(id => buildStep(id, {
  enterActions: moduleActions(PREPROCESSING_MODULE_BY_STEP[id] || null),
})));
const SOLVER_STEPS = Object.freeze(SOLVER_IDS.map(id => buildStep(id, {
  ...(id === "settings-drawer.solver.neutral-field" ? {
    enterActions: [ensureAction("settings.override", {
      values: Object.freeze({
        neutral_field_protection_enabled: true,
        neutral_field_protection_cutoff: 0.023,
      }),
    })],
  } : {}),
  ...(id === "settings-drawer.solver.boundary-mutation" ? {
    enterActions: [ensureAction("settings.override", {
      values: Object.freeze({ stage2_boundary_mutation_enabled: true }),
    })],
  } : {}),
})));
const WHITE_CAP_STEPS = Object.freeze(WHITE_CAP_IDS.map(id => buildStep(id, {
  ...(id === "settings-drawer.white-cap.appearance-budget" ? {
    enterActions: [ensureAction("settings.override", {
      values: Object.freeze({ cap_mode: "appearance_bounded_smooth" }),
    })],
  } : {}),
})));
const LUMINANCE_STEPS = Object.freeze(LUMINANCE_IDS.map(id => buildStep(id, {
  ...(id === "settings-drawer.luminance.intro" ? {
    enterActions: [ensureAction("settings.override", {
      values: Object.freeze({ luminance_mode: "luminance_detail" }),
    })],
  } : {}),
})));

const chapterRoute = (id, label, description, steps) => detour({
  id,
  label,
  description,
  offerStepId: "settings-drawer.chapters",
  returnStepId: "settings-drawer.chapters",
  repeatable: true,
  previousReturnsToOffer: true,
  suppressPreviousOnReturn: true,
  exitLabel: "Back to chapters",
  allowExitOnFinal: true,
  buttonLabel: label,
  showStatus: false,
  detourSteps: steps,
});

export const SETTINGS_DRAWER_GUIDE = freezeGuide({
  id: "settings-drawer",
  kind: "teaching",
  workspace_policy: "basic-teaching",
  version: 1,
  title: "Settings Drawer",
  summary: "Explore how every stage of a lithophane solve is shaped by Settings.",
  catalog: Object.freeze({ group: "Page Guides", order: 35 }),
  baseline: Object.freeze({
    ghost_printer: true,
    guide_assets: Object.freeze(["bubba-blanket"]),
    settings_overrides: SETTINGS_DRAWER_BASELINE,
  }),
  preflight_actions: Object.freeze([
    Object.freeze({ action: "settings.require_basic", input: Object.freeze({ values: Object.freeze({}) }) }),
  ]),
  preparation_actions: Object.freeze([
    Object.freeze({ action: "workspace.reset" }),
    Object.freeze({ action: "printer.mount_ghost", result_key: "tutorialPrinterProfile" }),
    Object.freeze({ action: "printer.select", input: Object.freeze({ printer_id: "tutorial-printer" }) }),
    Object.freeze({ action: "printer.select_print_setup", input: Object.freeze({ nozzle_id: "nozzle-200", extrusion_width_um: 200 }) }),
    Object.freeze({ action: "image.mount_guide_asset", input: Object.freeze({ asset_id: "bubba-blanket" }), result_key: "tutorialImage" }),
    Object.freeze({ action: "image.select", input: Object.freeze({ asset_id: "bubba-blanket" }) }),
    Object.freeze({ action: "settings.load_basic" }),
    Object.freeze({ action: "settings.override", input: Object.freeze({ values: SETTINGS_DRAWER_BASELINE }) }),
    Object.freeze({ action: "settings.set_module_state", input: Object.freeze({ state: SETTINGS_DRAWER_ALL_MODULES_OFF }) }),
  ]),
  chapters: Object.freeze([
    chapter("settings-drawer.opening", "Getting started", SPINE),
  ]),
  detours: Object.freeze([
    chapterRoute("settings-drawer.essentials", "Essentials", "The size, thickness, and white materials of the print itself.", ESSENTIAL_STEPS),
    chapterRoute("settings-drawer.preprocessing", "Preprocessing", "Changes made to your image before the solver sees it.", PREPROCESSING_STEPS),
    chapterRoute("settings-drawer.solver", "Color Solver", "How Prisma chooses colors and shapes the areas it puts them in.", SOLVER_STEPS),
    chapterRoute("settings-drawer.white-cap", "White Cap", "The white surface you look through, and the relief on it.", WHITE_CAP_STEPS),
    chapterRoute("settings-drawer.luminance", "Luminance Mode", "What changes when the white cap carries the image.", LUMINANCE_STEPS),
  ]),
  steps: SPINE,
});
