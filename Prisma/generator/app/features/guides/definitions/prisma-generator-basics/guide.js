import { chapter, freezeGuide } from "../../core/schema.js";
import {
  BASICS_DETOURS,
  BASICS_EXPORT_SPINE,
  BASICS_IMAGE_SPINE,
  BASICS_INTRODUCTION,
  BASICS_PALETTE_SPINE,
  BASICS_PREVIEW_SPINE,
  BASICS_SETTINGS_SPINE,
  BASICS_STEPS,
  EXPORT_GUIDE_CONTENT,
  EXPORT_GUIDE_STEPS,
  IMAGE_GUIDE_CONTENT,
  IMAGE_GUIDE_PREPARATION,
  IMAGE_GUIDE_STEPS,
  PALETTE_GUIDE_CONTENT,
  PALETTE_GUIDE_PREPARATION,
  PALETTE_GUIDE_STEPS,
  PREVIEW_GUIDE_CONTENT,
  PREVIEW_GUIDE_STEPS,
  SETTINGS_GUIDE_STEPS,
  SOLVE_A_PREPARATION,
} from "./content.js";

const TUTORIAL_BASELINE = Object.freeze({
  ghost_printer: true,
  guide_assets: Object.freeze(["bubba-blanket"]),
  settings_overrides: Object.freeze({
    image_sample_pitch_mm: 0.4,
    solver_fine_pitch_mm: 0.4,
  }),
  palette_controls: Object.freeze({ target_filament_count: 3, target_suggest_count: 5 }),
});

const SETTINGS_BASELINE = Object.freeze({
  ghost_printer: false,
  guide_assets: Object.freeze([]),
});

const ROUTES = Object.freeze({
  full: Object.freeze({
    id: "full",
    launch_id: "prisma-generator-basics",
    version: 18,
    title: "Prisma Generator Basics",
    summary: "Create, compare, inspect, and export a complete tutorial lithophane.",
    catalog: Object.freeze({ group: "Getting Started", order: 20 }),
    baseline: TUTORIAL_BASELINE,
    chapters: Object.freeze([
      chapter("introduction", "Introduction", BASICS_INTRODUCTION),
      chapter("image", "Image", BASICS_IMAGE_SPINE),
      chapter("palette", "Palette", BASICS_PALETTE_SPINE),
      chapter("settings", "Settings", BASICS_SETTINGS_SPINE),
      chapter("preview", "Preview", BASICS_PREVIEW_SPINE),
      chapter("export", "Export", BASICS_EXPORT_SPINE),
    ]),
    detours: BASICS_DETOURS,
    steps: BASICS_STEPS,
  }),
  image: Object.freeze({
    id: "image",
    launch_id: "image-guide",
    version: 6,
    title: "Image Guide",
    summary: "Prepare and frame the tutorial image while exploring Image controls.",
    catalog: Object.freeze({ group: "Page Guides", order: 10 }),
    baseline: TUTORIAL_BASELINE,
    chapters: Object.freeze([
      chapter("preparation", "Preparation", IMAGE_GUIDE_PREPARATION),
      chapter("image", "Image", IMAGE_GUIDE_CONTENT),
    ]),
    steps: IMAGE_GUIDE_STEPS,
  }),
  palette: Object.freeze({
    id: "palette",
    launch_id: "palette-guide",
    version: 7,
    title: "Palette Guide",
    summary: "Prepare suggested palettes and explore the Deck and Manual builder.",
    catalog: Object.freeze({ group: "Page Guides", order: 20 }),
    baseline: TUTORIAL_BASELINE,
    chapters: Object.freeze([
      chapter("preparation", "Preparation", PALETTE_GUIDE_PREPARATION),
      chapter("palette", "Palette", PALETTE_GUIDE_CONTENT),
    ]),
    steps: PALETTE_GUIDE_STEPS,
  }),
  settings: Object.freeze({
    id: "settings",
    launch_id: "settings-guide",
    version: 6,
    title: "Settings Guide",
    summary: "Explore Settings Profiles, solver controls, White Cap, and Advanced options.",
    catalog: Object.freeze({ group: "Page Guides", order: 30 }),
    baseline: SETTINGS_BASELINE,
    chapters: Object.freeze([chapter("settings", "Settings", SETTINGS_GUIDE_STEPS)]),
    steps: SETTINGS_GUIDE_STEPS,
  }),
  preview: Object.freeze({
    id: "preview",
    launch_id: "preview-guide",
    version: 7,
    title: "Preview Guide",
    summary: "Create two tutorial solves, then compare and inspect their results.",
    catalog: Object.freeze({ group: "Page Guides", order: 40, note: "Creates two tutorial solves." }),
    baseline: TUTORIAL_BASELINE,
    chapters: Object.freeze([
      chapter("preparation", "Preparation", SOLVE_A_PREPARATION),
      chapter("preview", "Preview", PREVIEW_GUIDE_CONTENT),
    ]),
    steps: PREVIEW_GUIDE_STEPS,
  }),
  export: Object.freeze({
    id: "export",
    launch_id: "export-guide",
    version: 7,
    title: "Export Guide",
    summary: "Create a tutorial solve and generate its print files.",
    catalog: Object.freeze({
      group: "Page Guides",
      order: 50,
      note: "Creates one tutorial solve and print files.",
    }),
    baseline: TUTORIAL_BASELINE,
    chapters: Object.freeze([
      chapter("preparation", "Preparation", SOLVE_A_PREPARATION),
      chapter("export", "Export", EXPORT_GUIDE_CONTENT),
    ]),
    steps: EXPORT_GUIDE_STEPS,
  }),
});

function routeView(route) {
  const preparationActions = [
    Object.freeze({ action: "settings.load_basic" }),
    Object.freeze({ action: "filaments.enable_all" }),
    Object.freeze({ action: "palette_candidates.select_all" }),
    Object.freeze({
      action: "palette_candidates.configure",
      input: Object.freeze({
        target_filament_count: route.baseline.palette_controls?.target_filament_count || 3,
        suggestion_count: route.baseline.palette_controls?.target_suggest_count || 5,
      }),
    }),
    Object.freeze({
      action: "export.set_policy",
      input: Object.freeze({ output_format: "3mf", geometry_source: "field_derived", field_scale: 4 }),
    }),
  ];
  if (Object.keys(route.baseline.settings_overrides || {}).length) {
    preparationActions.splice(1, 0, Object.freeze({
      action: "settings.override",
      input: Object.freeze({ values: route.baseline.settings_overrides }),
    }));
  }
  if (route.baseline.ghost_printer) {
    preparationActions.push(Object.freeze({ action: "printer.mount_ghost", result_key: "tutorialPrinterProfile" }));
  }
  for (const assetId of route.baseline.guide_assets || []) {
    preparationActions.push(Object.freeze({
      action: "image.mount_guide_asset",
      input: Object.freeze({ asset_id: assetId }),
      ...(assetId === "bubba-blanket" ? { result_key: "tutorialImage" } : {}),
    }));
  }
  return freezeGuide({
    id: route.launch_id,
    kind: route.id === "full" ? "teaching" : "teaching-route",
    workspace_policy: "basic-teaching",
    canonical_guide_id: "prisma-generator-basics",
    route_id: route.id,
    version: route.version,
    title: route.title,
    summary: route.summary,
    restore_presentation: false,
    baseline: route.baseline,
    preparation_actions: Object.freeze(preparationActions),
    catalog: route.catalog,
    chapters: route.chapters,
    ...(route.detours ? { detours: route.detours } : {}),
    steps: route.steps,
  });
}

export const BASICS_GUIDE = freezeGuide({
  ...routeView(ROUTES.full),
  routes: ROUTES,
  launch_aliases: Object.freeze({
    "image-guide": "image",
    "palette-guide": "palette",
    "settings-guide": "settings",
    "preview-guide": "preview",
    "export-guide": "export",
  }),
});

export const BASICS_ROUTE_VIEWS = Object.freeze(Object.fromEntries(
  Object.entries(ROUTES)
    .filter(([routeId]) => routeId !== "full")
    .map(([routeId, route]) => [routeId, routeView(route)]),
));

export const BASICS_LAUNCH_ALIASES = Object.freeze(Object.fromEntries(
  Object.values(ROUTES).map(route => [route.launch_id, route.id]),
));
