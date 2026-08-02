function completion(kind, options = {}) {
  return Object.freeze({ kind, ...options });
}

function step({
  id,
  title,
  body,
  target,
  placementGroup = null,
  placements = ["right", "bottom", "left", "top"],
  completionMode = completion("manual"),
  allowPrevious = true,
  allowSkip = true,
  nextLabel = null,
  followup = null,
}) {
  return Object.freeze({
    id,
    title,
    body,
    target_id: target,
    reveal_id: target,
    preferred_placements: Object.freeze(placements),
    ...(placementGroup ? { placement_group: placementGroup } : {}),
    completion: completionMode,
    allow_previous: allowPrevious,
    allow_skip: allowSkip,
    ...(nextLabel ? { next_label: nextLabel } : {}),
    ...(followup ? { followup: Object.freeze(followup) } : {}),
  });
}

const BASICS_LESSONS = Object.freeze([
  step({
    id: "introduction",
    title: "Prisma Generator Basics",
    body: "This guide will show you the basic steps for converting an image into the 3D meshes used to print a color lithophane.\n\nAlong the way, you will learn how Prisma Generator organizes images, palettes, settings, solve results, and print files. You can end this guide at any time.",
    target: null,
    placements: [],
    allowPrevious: false,
    allowSkip: false,
    nextLabel: "Begin",
  }),
  step({
    id: "core-terminology",
    title: "Palette and solve",
    body: "There are two terms you should know before continuing:\n\n• A **palette** is a small set of colored filaments that can be layered together to produce the colors in a finished lithophane. They act like “primary colors” that Prisma combines to reproduce the colors in the source image.\n\nFor any palette, there are many possible ways to arrange its filaments into layers across an image.\n\n• A **solve** is Prisma’s process for deciding how to arrange those layers to reproduce the prepared image.",
    target: null,
    placements: [],
  }),
  step({
    id: "workflow",
    title: "The Generator workflow",
    body: "Prisma’s numbered workflow moves from a source image to printable files:\n\nImage → Palette → Preview → Export\n\nSettings are available throughout the workflow and affect the next solve. You can start a solve from any workflow page; it uses the prepared image, active palette, and current settings at the moment it begins.\n\nEach stage will be explained in more detail as you move through this guide.",
    target: "workflow.overview",
    placements: ["bottom", "right", "top", "left"],
  }),
  step({
    id: "printer-select",
    title: "Select the Tutorial Printer profile",
    body: "This guide uses the Tutorial Printer profile to ensure that all learners get the same experience.\n\nSelect Tutorial Printer from the Active printer list. If it is already active, the guide will continue automatically.",
    target: "sidebar.active-printer",
    placementGroup: "basics-printer",
    completionMode: completion("event", {
      event: "printer.active-changed",
      predicate_id: "basics.tutorial-printer-active",
      accept_preexisting: true,
    }),
  }),
  step({
    id: "tutorial-nozzle",
    title: "Confirm the tutorial nozzle",
    body: "Smaller nozzles can produce higher-quality lithophanes, but they take longer to solve and print. This guide uses a 0.4 mm nozzle to save time while you learn the basics.\n\nConfirm that the nozzle selector is set to 0.4 mm. If necessary, change the selection, then select Next.",
    target: "sidebar.active-nozzle",
    placementGroup: "basics-printer",
    completionMode: completion("manual", {
      predicate_id: "basics.tutorial-nozzle-active",
    }),
    allowSkip: false,
  }),
  step({
    id: "image-introduction",
    title: "Image: prepare the source",
    body: "The Image page is where you prepare the source image Prisma will reproduce as a lithophane.\n\nHere you choose an image, frame it within a physical canvas, and adjust its appearance. The prepared image guides palette suggestions and becomes the visual target for each solve.",
    target: null,
    placements: [],
  }),
  step({
    id: "choose-image",
    title: "Choose tutorial image",
    body: "The Image Library contains source images available to Prisma Generator. Select {{tutorialImage}} to use it for this guide.",
    target: "basics.tutorial-image",
    placementGroup: "basics-image-library",
    completionMode: completion("event", {
      event: "image.selected",
      predicate_id: "basics.tutorial-image-selected",
      accept_preexisting: true,
    }),
  }),
  step({
    id: "image-library",
    title: "Managing source images",
    body: "Add Image imports one or more images. Open Folder opens the workspace Images folder. Refresh checks that folder for changes. The rightmost button expands or contracts the Image Library when you want more browsing room.",
    target: "image.library-management",
    placementGroup: "basics-image-library",
  }),
  step({
    id: "image-preview",
    title: "Preview and Adjustments",
    body: "Preview shows how the source image is framed and updates as you change the canvas, crop, or appearance.\n\nAdjustments controls the lithophane’s shape, physical size, framing, and image appearance.\n\nThese changes affect future solves but never modify the original image.",
    target: "image.adjustments",
    placementGroup: "basics-image-adjustments",
    placements: ["left", "bottom", "top", "right"],
  }),
  step({
    id: "image-adjustments",
    title: "Adjustments panel",
    body: "The Adjustments panel sets the lithophane’s shape and physical size, how the photograph fits inside it, and the photograph’s appearance.\n\nWe will look at its Size and Image controls next.",
    target: "image.adjustments",
    placementGroup: "basics-image-adjustments",
    placements: ["left", "bottom", "top", "right"],
  }),
  step({
    id: "aspect-ratios",
    title: "Aspect-ratio choices",
    body: "The Size tab offers W×H for independent dimensions, Ratio for a custom ratio, Image for the source ratio, and common 3:2, 4:3, 5:4, and 1:1 ratios.\n\nLandscape and Portrait set the canvas orientation. Feel free to try these controls; the guide does not require a particular choice at this point.",
    target: "image.aspect-experiment",
    placementGroup: "basics-image-adjustments",
    placements: ["left", "bottom", "top", "right"],
  }),
  step({
    id: "crop-fit",
    title: "Crop-fit shortcuts",
    body: "The first Crop Fit button resets the image zoom and centers it. The other two resize the frame to use the source image’s full width or full height.\n\nEach button provides a quick way to establish an initial crop; you can continue refining the framing afterward. Feel free to try all three.",
    target: "image.crop-fit",
    placementGroup: "basics-image-adjustments",
    placements: ["left", "bottom", "top", "right"],
  }),
  step({
    id: "interactive-framing",
    title: "Interactive framing",
    body: "Use the mouse wheel over the editor to zoom, then drag the image to choose what remains inside the frame.\n\nScale and Rotate provide precise controls, and the nearby buttons rotate by 90 degrees or flip the source. Experiment if you like; Reset will remove these changes.",
    target: "image.framing",
    placementGroup: "basics-image-adjustments",
    placements: ["left", "bottom", "top", "right"],
  }),
  step({
    id: "physical-dimensions",
    title: "Physical dimensions",
    body: "Width and Height set the lithophane’s physical dimensions in millimeters. The lock buttons determine whether changing one dimension also updates the other.\n\nYou do not need to change these values yet. The guide will ask you to set the tutorial dimensions after this tour.",
    target: "image.physical-dimensions",
    placementGroup: "basics-image-adjustments",
    placements: ["left", "bottom", "top", "right"],
  }),
  step({
    id: "border",
    title: "Optional printed border",
    body: "The Border control adds a raised frame around the lithophane, printed in the same color as the cap. It can provide a finished edge and extra support.\n\nTurn the border on if you want to preview it, then turn it off before continuing.",
    target: "image.border",
    placementGroup: "basics-image-adjustments",
    placements: ["left", "bottom", "top", "right"],
  }),
  step({
    id: "open-image-adjustments",
    title: "Open the Image controls",
    body: "Select Image at the top of the Adjustments panel to open the image-adjustment controls. The guide will continue automatically.",
    target: "image.adjustment-image-tab",
    placementGroup: "basics-image-adjustments",
    placements: ["left", "bottom", "top", "right"],
    completionMode: completion("event", {
      event: "image.adjustment-tab.changed",
      predicate_id: "image.adjustment-tab-image",
      accept_preexisting: true,
    }),
    allowSkip: false,
  }),
  step({
    id: "appearance",
    title: "Image appearance",
    body: "B/W and Color choose the overall image mode.\n\nExposure changes brightness; Contrast separates light and dark values; Highlight and Shadow target the brightest and darkest areas. Tint, Saturation, and Temperature adjust color balance and intensity.\n\nFeel free to experiment with these controls. You will reset them before setting the tutorial canvas.",
    target: "image.appearance",
    placementGroup: "basics-image-adjustments",
    placements: ["left", "bottom", "top", "right"],
  }),
  step({
    id: "reset-image-controls",
    title: "Reset the image controls",
    body: "Before setting the tutorial dimensions, select Reset to clear any framing or appearance changes you tried.\n\nReset restores the selected image’s Size and Image controls to their defaults. Select Next after resetting.",
    target: "image.reset-framing",
    placementGroup: "basics-image-adjustments",
    placements: ["left", "bottom", "top", "right"],
    completionMode: completion("manual", {
      predicate_id: "basics.image-reset",
    }),
    allowSkip: false,
  }),
  step({
    id: "tutorial-canvas",
    title: "Set the tutorial canvas",
    body: "Set the physical canvas to exactly 90 × 120 mm (Width 90, Height 120). You may use either W×H or Image for this tutorial image.\n\nIf you experimented with the border, turn it off before continuing. Using the same canvas size keeps the tutorial results consistent for everyone.\n\nSelect Next when the canvas is 90 × 120 mm and the border is off.",
    target: "image.tutorial-canvas",
    placementGroup: "basics-image-adjustments",
    placements: ["left", "bottom", "top", "right"],
    completionMode: completion("manual", {
      predicate_id: "basics.canvas-ready",
    }),
    allowSkip: false,
  }),
  step({
    id: "image-summary",
    title: "Resolution summary",
    body: "Image Size is the source image’s resolution. Print Size is the physical size of the lithophane. Rendered Size is the pixel grid that Prisma will solve.\n\nA larger canvas or a smaller Solve Pitch (the physical spacing between pixels in Prisma’s solver grid) creates more solve pixels and requires more time.",
    target: "image.summary",
    placementGroup: "basics-image-adjustments",
  }),
  step({
    id: "open-palette",
    title: "Continue to Palette",
    body: "The source image is ready. Open Palette to choose the colored filaments Prisma may use.",
    target: "workflow.palette",
    placements: ["bottom", "right", "top", "left"],
    completionMode: completion("event", {
      event: "tab.changed",
      predicate_id: "tab.creation",
      accept_preexisting: false,
    }),
    allowSkip: false,
  }),
  step({
    id: "palette-introduction",
    title: "Palette: choose your filaments",
    body: "A **palette** is the set of filaments Prisma will use to recreate the colors in an image.\n\nOn the Palette page, you can ask Prisma to recommend palettes for the prepared image or build one manually. The active palette will be used by the next solve.",
    target: null,
    placements: [],
  }),
  step({
    id: "palette-methods",
    title: "Building a palette",
    body: "Auto-Suggest recommends palettes whose filaments can be combined to reproduce the colors in the source image. Its recommendations can include only the filaments selected in this panel. Deselecting a filament prevents Auto-Suggest from including it in later recommendations.\n\nThe filament selected for the base and White Cap in Settings—which this guide will introduce later—is reserved for those parts and cannot be selected as a palette color.\n\nManual palette building and advanced suggestion controls are available when you need more control.",
    target: "palette.autosuggest-overview",
    placementGroup: "basics-palette-suggestions",
    placements: ["bottom", "right", "top", "left"],
  }),
  step({
    id: "candidate-filaments",
    title: "Auto-Suggest filaments",
    body: "Every active filament that can be used for color is already selected here.\n\nThis does not add every selected filament to one palette. It tells Auto-Suggest which filaments it may combine when building suggestions.\n\nThe filament reserved for the white base and cap cannot also be selected as a color. Leave the current selection unchanged for this guide.",
    target: "palette.candidates",
    placementGroup: "basics-palette-suggestions",
    placements: ["right", "bottom", "top", "left"],
  }),
  step({
    id: "suggest-palettes",
    title: "Suggest palettes",
    body: "Select Suggest Palettes.\n\nPrisma will compare combinations of the selected filaments against this image. This may take some time; the progress display shows its status and lets you cancel if needed.",
    target: "palette.suggest",
    placementGroup: "basics-palette-suggestions",
    completionMode: completion("event", {
      event: "palette.suggestions.completed",
      predicate_id: "basics.two-suggestions-ready",
    }),
    allowSkip: false,
  }),
  step({
    id: "add-suggestions",
    title: "Suggested palettes",
    body: "Suggested cards show their filaments and image-specific metrics. Metrics help rank palettes under the model, but only partly correspond to whether a real lithophane looks good.\n\nFor this guide, use the two highest-ranked results: {{paletteA}} and {{paletteB}}. Add both to the Palette Deck.",
    target: "palette.suggestions",
    placementGroup: "basics-palette-suggestions",
    completionMode: completion("event", {
      event: "palette.deck.updated",
      predicate_id: "basics.tutorial-palettes-in-deck",
    }),
    allowSkip: false,
  }),
  step({
    id: "palette-deck",
    title: "The Palette Deck",
    body: "The sidebar Deck contains palettes ready to solve.\n\nLoad retrieves a saved palette, and Clear removes all Deck cards after confirmation.\n\nSelecting a card makes it active. A card menu can create a Manual variant or save it; × removes only that card.",
    target: "palette.deck",
  }),
  step({
    id: "manual-palette-add",
    title: "Build a Manual palette",
    body: "The Manual builder is now open.\n\nChoose one or more colored filaments in any order, then select Add to Deck. The guide will continue after your new palette appears in the sidebar Deck.",
    target: "palette.manual",
    placements: ["right", "bottom", "top", "left"],
    completionMode: completion("event", {
      event: "palette.deck.updated",
      predicate_id: "basics.manual-card-added",
    }),
    allowSkip: false,
  }),
  step({
    id: "manual-palette-remove",
    title: "Remove the Manual palette",
    body: "This is the temporary palette you just added.\n\nSelect its × button once; it changes to ! to ask for confirmation. Select it a second time to remove the card.\n\nThe two suggested tutorial palettes will remain.",
    target: "basics.manual-card",
    placementGroup: "basics-palette-deck",
    placements: ["right", "bottom", "top", "left"],
    completionMode: completion("event", {
      event: "palette.deck.updated",
      predicate_id: "basics.manual-card-removed",
    }),
    allowSkip: false,
  }),
  step({
    id: "activate-first",
    title: "Activate first palette",
    body: "Select {{paletteA}} in the Deck. The highlighted card is the active palette used by the next solve.",
    target: "basics.palette-a",
    placementGroup: "basics-palette-deck",
    completionMode: completion("event", {
      event: "palette.deck.activated",
      predicate_id: "basics.palette-a-active",
      accept_preexisting: true,
    }),
    allowSkip: false,
  }),
  step({
    id: "open-settings",
    title: "Open Settings",
    body: "Settings controls how Prisma converts the prepared image and active palette into a printable layer plan. It covers physical structure, image preprocessing, solver behavior, and the white cap.\n\nOpen Settings. This guide uses the prepared tutorial defaults.",
    target: "topbar.settings",
    completionMode: completion("event", {
      event: "settings.opened",
      predicate_id: "settings.drawer-open",
    }),
  }),
  step({
    id: "settings-profile",
    title: "Profiles and Essentials",
    body: "A Settings Profile is a reusable collection of solve settings. Tutorial Basics is a temporary copy of the built-in Basic profile with Solve Pitch set to 0.4 mm for faster tutorial solves.\n\nEssentials contains Solve Mode, Solve Pitch, Max Total Thickness, Layer Height, White Base/Cap Filament, and Base Thickness. The diagram shows the base, color layers, and white cap.\n\nProfiles… is where you load other Settings Profiles. Leave Tutorial Basics unchanged and select Next.",
    target: "settings.profile-essentials",
    placementGroup: "basics-settings",
    placements: ["left", "bottom", "top", "right"],
    completionMode: completion("manual", {
      predicate_id: "basics.tutorial-profile-ready",
    }),
    allowSkip: false,
  }),
  step({
    id: "preprocessing-solver",
    title: "Solver settings",
    body: "Preprocessing can reduce noise, smooth printable-scale features, or reshape tone and saturation before solving.\n\nThe Color Solver controls how Prisma divides the image and searches for printable filament recipes.\n\nDifficult photographs may benefit from these tools. Leave them unchanged for this tutorial image.",
    target: "settings.preprocessing-solver",
    placementGroup: "basics-settings",
    placements: ["left", "bottom", "top", "right"],
  }),
  step({
    id: "white-cap",
    title: "White Cap",
    body: "The White Cap covers the color stack.\n\nIts boundary portion keeps the print structurally covered; its detail portion can restore fine appearance information. Smoothing and depth determine how that material is distributed.\n\nKeep the tutorial profile’s White Cap settings unchanged for this guide.",
    target: "settings.white-cap",
    placementGroup: "basics-settings",
    placements: ["left", "bottom", "top", "right"],
  }),
  step({
    id: "advanced-settings",
    title: "Advanced settings",
    body: "Turn Advanced on.\n\nIt reveals less frequently changed controls without changing their values. Look around, then turn Advanced off again to continue.",
    target: "settings.advanced",
    placementGroup: "basics-settings",
    placements: ["left", "bottom", "top", "right"],
    completionMode: completion("event", {
      event: "settings.advanced-changed",
      predicate_id: "basics.settings-detour-ready",
    }),
  }),
  step({
    id: "close-settings",
    title: "Close Settings",
    body: "Settings are captured when a solve begins, so every run retains the values that produced it.\n\nClose Settings to continue.",
    target: "settings.drawer",
    placementGroup: "basics-settings",
    placements: ["left", "bottom", "top", "right"],
    completionMode: completion("event", {
      event: "settings.closed",
      predicate_id: "settings.drawer-closed",
    }),
  }),
  step({
    id: "solve-first",
    title: "Solve: first palette",
    body: "Select Solve.\n\nA solve can be started from any workflow page. It uses the prepared image, active palette, and current settings at the moment it begins. Prisma will move to Preview while it works.",
    target: "topbar.solve",
    completionMode: completion("event", {
      event: "solve.completed",
      predicate_id: "basics.first-solve-complete",
    }),
    allowSkip: false,
  }),
  step({
    id: "preview-introduction",
    title: "Preview: compare and inspect results",
    body: "Preview is where Prisma collects completed solve runs so you can compare their predicted appearance, inspect their printable structures, and review the settings that produced them.\n\nA run is one solve result created from a particular image, palette, and collection of settings.\n\nWe will create a second run before exploring the available inspection tools.",
    target: null,
    placements: [],
  }),
  step({
    id: "preview-arrival",
    title: "First solve result",
    body: "You are now in Preview. The completed run appears in Solve History, while its predicted result appears in the comparison area.\n\nThese two regions stay linked as you add and select runs.",
    target: "preview.overview",
    placementGroup: "basics-preview",
    placements: ["left", "bottom", "top", "right"],
  }),
  step({
    id: "activate-second",
    title: "Activate the second palette",
    body: "Without leaving Preview, select {{paletteB}} in the Deck. The guide will continue when it becomes the active palette.",
    target: "basics.palette-b",
    completionMode: completion("event", {
      event: "palette.deck.activated",
      predicate_id: "basics.palette-b-active",
      accept_preexisting: true,
    }),
    allowSkip: false,
  }),
  step({
    id: "solve-second",
    title: "Solve the second palette",
    body: "Select Solve again.\n\nSolve is available throughout the workflow and uses the active palette, so you do not need to leave Preview. This second run will give you two results to compare.",
    target: "topbar.solve",
    completionMode: completion("event", {
      event: "solve.completed",
      predicate_id: "basics.second-solve-complete",
    }),
    allowSkip: false,
  }),
  step({
    id: "compare-runs",
    title: "Compare the solves",
    body: "Both guide runs should now be selected and displayed side by side in the comparison area.\n\nThis is where you judge the visible differences between palettes. Select Next after looking at both results.",
    target: "preview.comparison",
    placementGroup: "basics-preview",
    placements: ["left", "bottom", "top", "right"],
    completionMode: completion("manual", {
      predicate_id: "basics.both-runs-selected",
    }),
    allowSkip: false,
  }),
  step({
    id: "solve-history",
    title: "Solve History",
    body: "Solve History contains one card for each completed run. Selecting cards controls which results appear in the comparison area.\n\nEach card also provides captured settings and actions for saving or removing the run.",
    target: "preview.history",
    placementGroup: "basics-preview",
    placements: ["left", "bottom", "top", "right"],
  }),
  step({
    id: "preview-views",
    title: "Preview views",
    body: "Predicted estimates the illuminated result. White Cap separates the Total, Boundary, and Detail cap geometry. Color Regions contains Color Ceiling and Recipe Viewer.\n\nTop Surface shows the final combined surface. Explorer examines materials around a selected height. Advanced contains the diagnostic Thickness Maps and Highpass views.\n\nVisit any view you like. A later guide will explain these tools in depth.",
    target: "preview.views",
    placementGroup: "basics-preview",
    placements: ["bottom", "left", "top", "right"],
  }),
  step({
    id: "closer-inspection",
    title: "Inspect the previews",
    body: "Preview images can be opened for larger inspection.\n\nSubview controls change which part of a solve is shown, and contextual controls appear only where relevant. Feel free to inspect the runs or move among views.",
    target: "preview.comparison",
    placementGroup: "basics-preview",
    placements: ["right", "top", "bottom", "left"],
  }),
  step({
    id: "captured-settings",
    title: "Run settings and saves",
    body: "A run’s Settings view records exactly what was captured when its solve began.\n\nUse These Settings applies that snapshot as the current temporary Settings Profile for a future solve.\n\nSolve History is session-only unless a run is explicitly saved; Load opens Saved Runs.\n\nYou do not need to save or load a run during this guide.",
    target: "preview.history",
    placementGroup: "basics-preview",
    placements: ["left", "bottom", "top", "right"],
  }),
  step({
    id: "open-export",
    title: "Open Export",
    body: "Export creates files for one completed run at a time. Open Export.",
    target: "workflow.export",
    placements: ["bottom", "right", "top", "left"],
    completionMode: completion("event", {
      event: "tab.changed",
      predicate_id: "tab.export",
      accept_preexisting: true,
    }),
  }),
  step({
    id: "export-introduction",
    title: "Export: create print files",
    body: "Export converts one completed run into files for slicing and printing.\n\nHere you choose the run and how its geometry should be packaged. Prisma then creates the meshes, filament instructions, and mesh report from that run’s stored data.\n\nExporting does not repeat the solve or change the completed run.",
    target: null,
    placements: [],
  }),
  step({
    id: "export-choices-overview",
    title: "Export choices",
    body: "Output format chooses one bundled 3MF or separate STL files. Mesh method controls how the solved surface becomes printable geometry, and Mesh detail controls reconstruction scale for the field-derived method.\n\nThese choices affect file packaging and mesh construction; they do not repeat or change the solve.",
    target: "export.options",
    placementGroup: "basics-export",
    placements: ["left", "bottom", "top", "right"],
  }),
  step({
    id: "select-export-run",
    title: "Select the export run",
    body: "Export begins with one completed run.\n\nSelect the run made with {{paletteA}}. Its preview and dimensions confirm which solve will be exported.",
    target: "basics.export-run-a",
    placementGroup: "basics-export",
    completionMode: completion("event", {
      event: "export.run-selected",
      predicate_id: "basics.export-run-a-selected",
      accept_preexisting: true,
    }),
    allowSkip: false,
  }),
  step({
    id: "export-options",
    title: "Print-file options",
    body: "Output format chooses one bundled 3MF or individual STL files. Mesh method chooses how the solved surface becomes geometry. Mesh detail controls reconstruction scale for the field-derived method.\n\nUse Single 3MF file, Field-derived white cap, and 4x balanced, then select Next.",
    target: "export.options",
    placementGroup: "basics-export",
    placements: ["left", "bottom", "top", "right"],
    completionMode: completion("manual", {
      predicate_id: "basics.export-options-ready",
    }),
    allowSkip: false,
  }),
  step({
    id: "generate-files",
    title: "Generate Print Files",
    body: "Select Generate Print Files.\n\nPrisma builds 3D model files and filament-loading or swap instructions from the selected run’s stored data.",
    target: "export.generate",
    placementGroup: "basics-export",
    completionMode: completion("event", {
      event: "export.completed",
      predicate_id: "basics.export-complete",
    }),
    allowSkip: false,
  }),
  step({
    id: "export-results",
    title: "Files are already saved",
    body: "The export record preserves its options and lists generated files, filament-loading or swap instructions, and the mesh report.\n\nThese files are already saved in the Output folder shown below. Use Open Folder to view them.\n\nDownload .zip creates a packaged copy in a location you choose. Use it only when you want a copy outside the workspace.",
    target: "export.results",
    placementGroup: "basics-export",
    placements: ["left", "top", "bottom", "right"],
  }),
  step({
    id: "complete",
    title: "Project complete",
    body: "You completed Prisma Generator’s normal workflow: selected the Tutorial Printer profile, prepared a source image, built and compared palettes, reviewed settings, made two solves, inspected Preview, and generated print files.\n\nYour previous Settings Profile will be restored when this guide closes. The Tutorial Printer profile and its 0.4 mm nozzle will remain active. Before starting your own project, select the profile and nozzle for your printer.\n\nHappy printing!",
    target: null,
    placements: [],
    allowSkip: false,
  }),
]);

const BASICS_LESSONS_BY_ID = new Map(BASICS_LESSONS.map(current => [current.id, current]));

function lessons(...ids) {
  return Object.freeze(ids.map((id) => {
    const current = BASICS_LESSONS_BY_ID.get(id);
    if (!current) throw new Error(`Unknown shared Basics lesson: ${id}`);
    return current;
  }));
}

function chapter(id, label, chapterSteps) {
  return Object.freeze({
    id,
    label,
    step_ids: Object.freeze(chapterSteps.map(current => current.id)),
  });
}

function detour({
  id,
  label,
  description,
  offerStepId,
  returnStepId,
  detourSteps,
  returnPredicateId = null,
}) {
  return Object.freeze({
    id,
    label,
    description,
    offer_step_id: offerStepId,
    return_step_id: returnStepId,
    return_predicate_id: returnPredicateId,
    steps: detourSteps,
  });
}

const BASICS_INTRODUCTION = lessons(
  "introduction",
  "core-terminology",
  "workflow",
  "printer-select",
  "tutorial-nozzle",
);
const BASICS_IMAGE_SPINE = lessons(
  "image-introduction",
  "choose-image",
  "image-library",
  "image-preview",
  "reset-image-controls",
  "tutorial-canvas",
  "open-palette",
);
const BASICS_PALETTE_SPINE = lessons(
  "palette-introduction",
  "palette-methods",
  "suggest-palettes",
  "add-suggestions",
  "palette-deck",
  "activate-first",
);
const BASICS_SETTINGS_SPINE = lessons(
  "open-settings",
  "settings-profile",
  "close-settings",
  "solve-first",
);
const BASICS_PREVIEW_SPINE = lessons(
  "preview-introduction",
  "preview-arrival",
  "activate-second",
  "solve-second",
  "compare-runs",
  "open-export",
);
const BASICS_EXPORT_SPINE = lessons(
  "export-introduction",
  "select-export-run",
  "export-options",
  "generate-files",
  "export-results",
  "complete",
);
const BASICS_STEPS = Object.freeze([
  ...BASICS_INTRODUCTION,
  ...BASICS_IMAGE_SPINE,
  ...BASICS_PALETTE_SPINE,
  ...BASICS_SETTINGS_SPINE,
  ...BASICS_PREVIEW_SPINE,
  ...BASICS_EXPORT_SPINE,
]);

const BASICS_DETOURS = Object.freeze([
  detour({
    id: "image-controls",
    label: "Image Controls",
    description: "Explore framing, appearance, borders, and physical-size controls.",
    offerStepId: "image-preview",
    returnStepId: "reset-image-controls",
    detourSteps: lessons(
      "image-adjustments",
      "aspect-ratios",
      "crop-fit",
      "interactive-framing",
      "physical-dimensions",
      "border",
      "image-summary",
      "open-image-adjustments",
      "appearance",
    ),
  }),
  detour({
    id: "palette-tools",
    label: "Palette Tools",
    description: "Explore filament eligibility and build a temporary Manual palette.",
    offerStepId: "palette-deck",
    returnStepId: "activate-first",
    detourSteps: lessons(
      "candidate-filaments",
      "manual-palette-add",
      "manual-palette-remove",
    ),
  }),
  detour({
    id: "settings-tools",
    label: "Settings",
    description: "Explore solver, White Cap, and Advanced settings without changing the tutorial profile.",
    offerStepId: "settings-profile",
    returnStepId: "close-settings",
    returnPredicateId: "basics.settings-detour-ready",
    detourSteps: lessons(
      "preprocessing-solver",
      "white-cap",
      "advanced-settings",
    ),
  }),
  detour({
    id: "preview-tools",
    label: "Preview",
    description: "Explore Solve History, result views, inspection, and captured settings.",
    offerStepId: "compare-runs",
    returnStepId: "open-export",
    detourSteps: lessons(
      "solve-history",
      "preview-views",
      "closer-inspection",
      "captured-settings",
    ),
  }),
  detour({
    id: "export-choices",
    label: "Export Choices",
    description: "Learn what output format, mesh method, and mesh detail control.",
    offerStepId: "export-introduction",
    returnStepId: "select-export-run",
    detourSteps: lessons("export-choices-overview"),
  }),
]);

const STANDALONE_COMPLETIONS = Object.freeze({
  image: step({
    id: "image-guide-complete",
    title: "Image Guide complete",
    body: "You explored Prisma’s image framing and appearance controls and restored the tutorial image to its 90 × 120 mm borderless canvas.",
    target: null,
    placements: [],
    allowSkip: false,
  }),
  palette: step({
    id: "palette-guide-complete",
    title: "Palette Guide complete",
    body: "You prepared two suggested palettes, explored the Palette Deck and Manual builder, and left Palette A active.",
    target: null,
    placements: [],
    allowSkip: false,
  }),
  settings: step({
    id: "settings-guide-complete",
    title: "Settings Guide complete",
    body: "You explored the major Settings groups and returned Advanced to Off. Your previous Settings Profile will be restored when this guide closes.",
    target: null,
    placements: [],
    allowSkip: false,
  }),
  preview: step({
    id: "preview-guide-complete",
    title: "Preview Guide complete",
    body: "You created two tutorial solves and explored comparison, Solve History, result views, and captured settings.",
    target: null,
    placements: [],
    allowSkip: false,
  }),
  export: step({
    id: "export-guide-complete",
    title: "Export Guide complete",
    body: "You created a tutorial solve, generated its print files, and reviewed where Prisma stores the resulting files and instructions.",
    target: null,
    placements: [],
    allowSkip: false,
  }),
});

const IMAGE_GUIDE_PREPARATION = lessons("printer-select", "tutorial-nozzle");
const IMAGE_GUIDE_CONTENT = Object.freeze([
  ...lessons("image-introduction", "choose-image", "image-library", "image-preview"),
  ...BASICS_DETOURS.find(item => item.id === "image-controls").steps,
  ...lessons("reset-image-controls", "tutorial-canvas"),
  STANDALONE_COMPLETIONS.image,
]);
const IMAGE_GUIDE_STEPS = Object.freeze([...IMAGE_GUIDE_PREPARATION, ...IMAGE_GUIDE_CONTENT]);

const PALETTE_GUIDE_PREPARATION = lessons(
  "printer-select",
  "tutorial-nozzle",
  "choose-image",
  "reset-image-controls",
  "tutorial-canvas",
  "open-palette",
);
const PALETTE_GUIDE_CONTENT = Object.freeze([
  ...lessons(
    "palette-introduction",
    "palette-methods",
    "candidate-filaments",
    "suggest-palettes",
    "add-suggestions",
    "palette-deck",
    "manual-palette-add",
    "manual-palette-remove",
    "activate-first",
  ),
  STANDALONE_COMPLETIONS.palette,
]);
const PALETTE_GUIDE_STEPS = Object.freeze([...PALETTE_GUIDE_PREPARATION, ...PALETTE_GUIDE_CONTENT]);

const SETTINGS_GUIDE_STEPS = Object.freeze([
  ...lessons(
    "open-settings",
    "settings-profile",
    "preprocessing-solver",
    "white-cap",
    "advanced-settings",
    "close-settings",
  ),
  STANDALONE_COMPLETIONS.settings,
]);

const SOLVE_A_PREPARATION = lessons(
  "printer-select",
  "tutorial-nozzle",
  "choose-image",
  "reset-image-controls",
  "tutorial-canvas",
  "open-palette",
  "palette-methods",
  "suggest-palettes",
  "add-suggestions",
  "activate-first",
  "open-settings",
  "settings-profile",
  "close-settings",
  "solve-first",
);
const PREVIEW_GUIDE_CONTENT = Object.freeze([
  ...lessons(
    "preview-introduction",
    "preview-arrival",
    "activate-second",
    "solve-second",
    "compare-runs",
    "solve-history",
    "preview-views",
    "closer-inspection",
    "captured-settings",
  ),
  STANDALONE_COMPLETIONS.preview,
]);
const PREVIEW_GUIDE_STEPS = Object.freeze([...SOLVE_A_PREPARATION, ...PREVIEW_GUIDE_CONTENT]);

const EXPORT_GUIDE_CONTENT = Object.freeze([
  ...lessons(
    "open-export",
    "export-introduction",
    "select-export-run",
    "export-choices-overview",
    "export-options",
    "generate-files",
    "export-results",
  ),
  STANDALONE_COMPLETIONS.export,
]);
const EXPORT_GUIDE_STEPS = Object.freeze([...SOLVE_A_PREPARATION, ...EXPORT_GUIDE_CONTENT]);

const GUIDE_DEFINITIONS = Object.freeze([
  Object.freeze({
    id: "guided-setup",
    version: 1,
    title: "Guided Setup",
    summary: "Configure Prisma Generator for your printer and learn its essential utility controls.",
    restore_presentation: false,
    catalog: Object.freeze({ group: "Getting Started", order: 10 }),
    chapters: Object.freeze([
      Object.freeze({
        id: "setup",
        label: "Setup",
        step_ids: Object.freeze([
          "printer-open",
          "printer-configuration",
          "active-nozzle",
          "model-library",
          "active-filaments",
          "theme",
          "temporary-files",
          "help-and-guides",
          "complete",
        ]),
      }),
    ]),
    steps: Object.freeze([
      step({
        id: "printer-open",
        title: "Configure your printer",
        body: "Let's begin by configuring Prisma Generator for your printer. Open Printer Configuration.",
        target: "sidebar.printer",
        completionMode: completion("event", {
          event: "printer-config.opened",
          predicate_id: "printer-config.open",
          accept_preexisting: true,
        }),
      }),
      step({
        id: "printer-configuration",
        title: "Review printer setup",
        body: "Update the printer profile to match the bed dimensions and number of AMS units that your printer has, then close the Printer Configuration window.",
        target: "printer.configuration",
        placements: ["left", "bottom", "top", "right"],
        completionMode: completion("event", {
          event: "printer-config.closed",
          predicate_id: "printer-config.closed",
        }),
        allowPrevious: false,
      }),
      step({
        id: "active-nozzle",
        title: "Select the active nozzle",
        body: "Confirm that the selected nozzle matches the nozzle installed on your printer, and change it if necessary. Prisma uses this choice when evaluating whether model features can be printed reliably.",
        target: "sidebar.active-nozzle",
        placementGroup: "setup-sidebar",
      }),
      step({
        id: "model-library",
        title: "Model Libraries",
        body: "A Model Library supplies Prisma with calibrated filament models. Prisma includes a library containing many common filaments. Use Manage later when you want to install a library published by Prisma Calibration or change the active Model Library. Changing the active library restarts Prisma Generator, so leave it unchanged during this guide.",
        target: "sidebar.model-library",
        placementGroup: "setup-sidebar",
      }),
      step({
        id: "active-filaments",
        title: "Active Filaments",
        body: "Active Filaments chooses which filaments Prisma may use. Mark filaments you do not own or use as inactive so they are excluded from palette tools. Your choices persist between sessions.",
        target: "sidebar.active-filaments",
        placementGroup: "setup-sidebar",
      }),
      step({
        id: "theme",
        title: "Choose a theme",
        body: "This button changes Prisma Generator’s color theme. If the current theme is not the one you prefer, choose another.",
        target: "topbar.theme",
        placementGroup: "setup-utilities",
        placements: ["left", "top", "right", "bottom"],
      }),
      step({
        id: "temporary-files",
        title: "Temporary files",
        body: "Prisma automatically clears temporary working data when a new session starts. Clear Temp Files lets you remove it manually during or at the end of the current session.",
        target: "topbar.clear-temp",
        placementGroup: "setup-utilities",
        placements: ["bottom", "left", "top", "right"],
      }),
      step({
        id: "help-and-guides",
        title: "Help & Guides",
        body: "You can reopen Guided Setup and launch other available guides from Help & Guides.",
        target: "topbar.help-guides",
        placementGroup: "setup-utilities",
        placements: ["bottom", "left", "top", "right"],
      }),
      step({
        id: "complete",
        title: "Ready to create",
        body: "Guided Setup is complete, and Prisma Generator is ready to use. Happy printing!",
        target: null,
        placements: [],
        allowSkip: false,
        followup: {
          guide_id: "prisma-generator-basics",
          text: "If you are new to Prisma Generator, continue with the guided introduction to its features and workflow.",
          label: "Start Prisma Generator Basics",
        },
      }),
    ]),
  }),
  Object.freeze({
    id: "prisma-generator-basics",
    version: 7,
    title: "Prisma Generator Basics",
    summary: "Create, compare, inspect, and export a complete tutorial lithophane.",
    restore_presentation: false,
    prepare_id: "basics",
    preparation: Object.freeze({
      tutorial_printer: true,
      tutorial_image: true,
      tutorial_settings: true,
      palette_controls: true,
    }),
    catalog: Object.freeze({ group: "Getting Started", order: 20 }),
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
  Object.freeze({
    id: "image-guide",
    version: 1,
    title: "Image Guide",
    summary: "Prepare and frame the tutorial image while exploring Image controls.",
    restore_presentation: false,
    prepare_id: "basics",
    preparation: Object.freeze({
      tutorial_printer: true,
      tutorial_image: true,
      tutorial_settings: true,
      palette_controls: false,
    }),
    catalog: Object.freeze({ group: "Page Guides", order: 10 }),
    chapters: Object.freeze([
      chapter("preparation", "Preparation", IMAGE_GUIDE_PREPARATION),
      chapter("image", "Image", IMAGE_GUIDE_CONTENT),
    ]),
    steps: IMAGE_GUIDE_STEPS,
  }),
  Object.freeze({
    id: "palette-guide",
    version: 1,
    title: "Palette Guide",
    summary: "Prepare suggested palettes and explore the Deck and Manual builder.",
    restore_presentation: false,
    prepare_id: "basics",
    preparation: Object.freeze({
      tutorial_printer: true,
      tutorial_image: true,
      tutorial_settings: true,
      palette_controls: true,
    }),
    catalog: Object.freeze({ group: "Page Guides", order: 20 }),
    chapters: Object.freeze([
      chapter("preparation", "Preparation", PALETTE_GUIDE_PREPARATION),
      chapter("palette", "Palette", PALETTE_GUIDE_CONTENT),
    ]),
    steps: PALETTE_GUIDE_STEPS,
  }),
  Object.freeze({
    id: "settings-guide",
    version: 1,
    title: "Settings Guide",
    summary: "Explore Settings Profiles, solver controls, White Cap, and Advanced options.",
    restore_presentation: false,
    prepare_id: "basics",
    preparation: Object.freeze({
      tutorial_printer: false,
      tutorial_image: false,
      tutorial_settings: true,
      palette_controls: false,
    }),
    catalog: Object.freeze({ group: "Page Guides", order: 30 }),
    chapters: Object.freeze([chapter("settings", "Settings", SETTINGS_GUIDE_STEPS)]),
    steps: SETTINGS_GUIDE_STEPS,
  }),
  Object.freeze({
    id: "preview-guide",
    version: 1,
    title: "Preview Guide",
    summary: "Create two tutorial solves, then compare and inspect their results.",
    restore_presentation: false,
    prepare_id: "basics",
    preparation: Object.freeze({
      tutorial_printer: true,
      tutorial_image: true,
      tutorial_settings: true,
      palette_controls: true,
    }),
    catalog: Object.freeze({
      group: "Page Guides",
      order: 40,
      note: "Creates two tutorial solves.",
    }),
    chapters: Object.freeze([
      chapter("preparation", "Preparation", SOLVE_A_PREPARATION),
      chapter("preview", "Preview", PREVIEW_GUIDE_CONTENT),
    ]),
    steps: PREVIEW_GUIDE_STEPS,
  }),
  Object.freeze({
    id: "export-guide",
    version: 1,
    title: "Export Guide",
    summary: "Create a tutorial solve and generate its print files.",
    restore_presentation: false,
    prepare_id: "basics",
    preparation: Object.freeze({
      tutorial_printer: true,
      tutorial_image: true,
      tutorial_settings: true,
      palette_controls: true,
    }),
    catalog: Object.freeze({
      group: "Page Guides",
      order: 50,
      note: "Creates one tutorial solve and print files.",
    }),
    chapters: Object.freeze([
      chapter("preparation", "Preparation", SOLVE_A_PREPARATION),
      chapter("export", "Export", EXPORT_GUIDE_CONTENT),
    ]),
    steps: EXPORT_GUIDE_STEPS,
  }),
  Object.freeze({
    id: "guided-setup-help-pointer",
    version: 1,
    title: "Find guides later",
    summary: "Point to Help & Guides after Guided Setup is declined.",
    restore_presentation: false,
    internal: true,
    chapters: Object.freeze([
      Object.freeze({ id: "help", label: "Help", step_ids: Object.freeze(["help-and-guides"]) }),
    ]),
    steps: Object.freeze([
      step({
        id: "help-and-guides",
        title: "Help is always available",
        body: "You can reopen Guided Setup and launch other available guides here whenever you need them.",
        target: "topbar.help-guides",
        completionMode: completion("interaction"),
        allowSkip: false,
      }),
    ]),
  }),
  Object.freeze({
    id: "interface-preview",
    version: 1,
    title: "Explore the Interface",
    summary: "Preview how Prisma can identify controls without changing them.",
    restore_presentation: true,
    catalog: Object.freeze({
      group: "Preview",
      order: 10,
      badge: "Preview",
    }),
    chapters: Object.freeze([
      Object.freeze({
        id: "interface",
        label: "Interface",
        step_ids: Object.freeze(["workflow-tabs", "settings-button", "white-point-rescale", "image-library"]),
      }),
    ]),
    steps: Object.freeze([
      step({
        id: "workflow-tabs",
        title: "Prisma's workflow",
        body: "These numbered tabs follow the normal path from choosing an image through exporting a finished lithophane.",
        target: "workflow.tabs",
        placements: ["bottom", "right", "top", "left"],
      }),
      step({
        id: "settings-button",
        title: "Open Settings",
        body: "Select Settings. This guide continues after Prisma confirms that the drawer opened.",
        target: "topbar.settings",
        placements: ["bottom", "left", "top", "right"],
        completionMode: completion("event", {
          event: "settings.opened",
          predicate_id: "settings.drawer-open",
        }),
      }),
      step({
        id: "white-point-rescale",
        title: "Settings can be identified in place",
        body: "This is White-point rescale. The guide frames the existing row without changing the setting or layout.",
        target: "settings.white-point-rescale",
        placements: ["left", "bottom", "top", "right"],
      }),
      step({
        id: "image-library",
        title: "Guides can follow the workflow",
        body: "Prisma can reveal another part of the interface and keep the explanation attached across different window sizes and zoom levels.",
        target: "image.library",
      }),
    ]),
  }),
]);

function validateDefinitions(definitions) {
  const guideIds = new Set();
  for (const guide of definitions) {
    if (!guide.id || guideIds.has(guide.id)) throw new Error(`Duplicate or missing guide id: ${guide.id}`);
    guideIds.add(guide.id);
    if (!Number.isInteger(guide.version) || guide.version < 1) {
      throw new Error(`Guide ${guide.id} has an invalid version`);
    }
    if (!Array.isArray(guide.steps) || guide.steps.length === 0) {
      throw new Error(`Guide ${guide.id} has no steps`);
    }
    if (guide.catalog && (!guide.catalog.group || !Number.isFinite(guide.catalog.order))) {
      throw new Error(`Guide ${guide.id} has incomplete catalog metadata`);
    }
    const preparationKeys = new Set([
      "tutorial_printer",
      "tutorial_image",
      "tutorial_settings",
      "palette_controls",
    ]);
    for (const [key, value] of Object.entries(guide.preparation || {})) {
      if (!preparationKeys.has(key) || typeof value !== "boolean") {
        throw new Error(`Guide ${guide.id} has invalid preparation metadata`);
      }
    }
    const detourIds = new Set();
    const detourSteps = [];
    for (const currentDetour of guide.detours || []) {
      if (!currentDetour.id || detourIds.has(currentDetour.id)) {
        throw new Error(`Guide ${guide.id} has a duplicate or missing detour id`);
      }
      detourIds.add(currentDetour.id);
      if (
        !currentDetour.label
        || !currentDetour.description
        || !currentDetour.offer_step_id
        || !currentDetour.return_step_id
        || !Array.isArray(currentDetour.steps)
        || currentDetour.steps.length === 0
      ) {
        throw new Error(`Guide ${guide.id} detour ${currentDetour.id} is incomplete`);
      }
      if (
        !guide.steps.some(current => current.id === currentDetour.offer_step_id)
        || !guide.steps.some(current => current.id === currentDetour.return_step_id)
      ) {
        throw new Error(`Guide ${guide.id} detour ${currentDetour.id} references an unknown spine step`);
      }
      if (currentDetour.steps.some(current => current.detour)) {
        throw new Error(`Guide ${guide.id} detour ${currentDetour.id} contains a nested detour`);
      }
      detourSteps.push(...currentDetour.steps);
    }
    const stepIds = new Set();
    for (const current of [...guide.steps, ...detourSteps]) {
      if (!current.id || stepIds.has(current.id)) throw new Error(`Guide ${guide.id} has a duplicate step id`);
      stepIds.add(current.id);
      const targetless = current.target_id === null && current.reveal_id === null;
      if ((!targetless && (!current.target_id || !current.reveal_id)) || !current.completion?.kind) {
        throw new Error(`Guide ${guide.id} step ${current.id} is incomplete`);
      }
      if (current.completion.kind === "event" && (!current.completion.event || !current.completion.predicate_id)) {
        throw new Error(`Guide ${guide.id} step ${current.id} has an incomplete event contract`);
      }
      if (current.completion.accept_preexisting && current.completion.kind !== "event") {
        throw new Error(`Guide ${guide.id} step ${current.id} accepts preexisting state without an event contract`);
      }
      if (current.followup && (!current.followup.guide_id || !current.followup.text || !current.followup.label)) {
        throw new Error(`Guide ${guide.id} step ${current.id} has an incomplete follow-up action`);
      }
    }
    if (!Array.isArray(guide.chapters) || guide.chapters.length === 0) {
      throw new Error(`Guide ${guide.id} has no chapter metadata`);
    }
    const chapterIds = new Set();
    const chapterStepIds = [];
    for (const currentChapter of guide.chapters) {
      if (!currentChapter.id || chapterIds.has(currentChapter.id) || !currentChapter.label) {
        throw new Error(`Guide ${guide.id} has invalid chapter metadata`);
      }
      chapterIds.add(currentChapter.id);
      if (!Array.isArray(currentChapter.step_ids) || currentChapter.step_ids.length === 0) {
        throw new Error(`Guide ${guide.id} chapter ${currentChapter.id} has no steps`);
      }
      chapterStepIds.push(...currentChapter.step_ids);
    }
    const spineIds = guide.steps.map(current => current.id);
    if (
      chapterStepIds.length !== spineIds.length
      || new Set(chapterStepIds).size !== chapterStepIds.length
      || spineIds.some(id => !chapterStepIds.includes(id))
    ) {
      throw new Error(`Guide ${guide.id} chapters do not cover the spine exactly once`);
    }
  }
  for (const guide of definitions) {
    for (const current of [...guide.steps, ...(guide.detours || []).flatMap(item => item.steps)]) {
      if (current.followup && !guideIds.has(current.followup.guide_id)) {
        throw new Error(`Guide ${guide.id} step ${current.id} references an unknown follow-up guide`);
      }
    }
  }
  return true;
}

/** Install immutable guide definitions and lookup commands. */
export function installFeaturesGuidesDefinitions(app) {
  validateDefinitions(GUIDE_DEFINITIONS);
  const guidesById = new Map(GUIDE_DEFINITIONS.map(guide => [guide.id, guide]));

  function getGuideDefinition(guideId) {
    return guidesById.get(guideId) || null;
  }

  function getGuideStep(guide, stepId) {
    return [
      ...(guide?.steps || []),
      ...(guide?.detours || []).flatMap(current => current.steps),
    ].find(current => current.id === stepId) || null;
  }

  function getGuideDetour(guide, detourId) {
    return guide?.detours?.find(current => current.id === detourId) || null;
  }

  function getAllGuideSteps(guide) {
    return Object.freeze([
      ...(guide?.steps || []),
      ...(guide?.detours || []).flatMap(current => current.steps),
    ]);
  }

  function getCatalogGuides() {
    return Object.freeze(GUIDE_DEFINITIONS
      .filter(guide => !guide.internal && guide.catalog)
      .sort((left, right) => (
        left.catalog.group.localeCompare(right.catalog.group)
        || left.catalog.order - right.catalog.order
      )));
  }

  Object.assign(app.commands, {
    getGuideDefinition,
    getGuideDetour,
    getGuideStep,
    getAllGuideSteps,
    getCatalogGuides,
    validateGuideDefinitions: () => validateDefinitions(GUIDE_DEFINITIONS),
  });
  app.state.guides.definitions = GUIDE_DEFINITIONS;
}
