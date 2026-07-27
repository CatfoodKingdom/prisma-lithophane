"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const {
  createFeatureHarness,
  fakeElement,
  memoryStorage,
  moduleUrl,
} = require("./support/application_harness.cjs");

const filaments = [
  { filament_id: "white", display_name: "White", has_profile: true },
  { filament_id: "red", display_name: "Red", has_profile: true },
  { filament_id: "blue", display_name: "Blue", has_profile: true },
  { filament_id: "excluded", display_name: "Excluded", has_profile: true, exclude_from_model: true },
  { filament_id: "uncalibrated", display_name: "Uncalibrated", has_profile: false },
];

async function harness(options = {}) {
  return createFeatureHarness({ filaments, ...options });
}

test("profile application overwrites owned values and drops retired aliases", async () => {
  const { app } = await harness();
  app.state.settings.config.solver_fine_pitch_mm = 0.8;
  app.state.settings.config.image_sample_pitch_mm = 0.7;
  app.commands._applySettingsProfileToConfig({
    solver_fine_pitch_mm: 0.4,
    image_sample_pitch_mm: 0.3,
    gamut_mode: "chroma",
    guided_surface_enabled: true,
    tv_weight: 99,
  });
  assert.equal(app.state.settings.config.solver_fine_pitch_mm, 0.4);
  assert.equal(app.state.settings.config.image_sample_pitch_mm, 0.3);
  assert.equal(app.state.settings.config.gamut_mode, "hue_preserving");
  assert.equal("guided_surface_enabled" in app.state.settings.config, false);
  assert.equal("tv_weight" in app.state.settings.config, false);
});

test("profile keys own current controls but not mandatory product safety", async () => {
  const { app } = await harness();
  const keys = app.state.settings.SETTINGS_PROFILE_KEYS;
  assert.ok(keys.includes("luminance_mode"));
  assert.ok(keys.includes("detail_cap_max_layers"));
  assert.ok(keys.includes("color_region_target_mm"));
  assert.ok(!keys.includes("enforce_printability"));
  assert.ok(!keys.includes("cap_continuity_cleanup"));
  assert.ok(!keys.includes("printability_minimum_extrusion_width_mm"));
  assert.ok(!keys.includes("printability_minimum_line_length_mm"));
  assert.ok(!keys.includes("min_line_length_multiplier"));
  assert.equal(app.state.settings.config.enforce_printability, true);
  assert.equal(app.state.settings.config.cap_continuity_cleanup, true);
});

test("module snapshots use descriptor defaults and compare nested values", async () => {
  const { app } = await harness();
  app.state.settings.moduleData = [
    { name: "demo", slot: "preprocessing", default_enabled: true },
    { name: "off", slot: "preprocessing", default_enabled: false },
  ];
  assert.deepEqual(app.commands._normalizeSettingsProfileModules({}, {}), { demo: true, off: false });
  assert.equal(app.commands._settingsProfileModulesEqual({ demo: true }, { demo: true }), true);
  assert.equal(app.commands._settingsProfileModulesEqual({ demo: true }, { demo: false }), false);
  assert.equal(
    app.commands._settingsProfileValuesEqual(
      { preprocessing_params: { demo: { radius: 2 } } },
      { preprocessing_params: { demo: { radius: 2 } } },
    ),
    true,
  );
});

test("temporary settings profiles prefer durable run snapshots", async () => {
  const { app } = await harness();
  app.state.settings.moduleData = [
    { name: "denoise", slot: "preprocessing", default_enabled: false },
  ];
  const profile = app.commands.buildTemporarySettingsProfileFromRun({
    id: "run-1",
    config: { t_max: 9 },
    recipe_snapshot: {
      profile_snapshot: {
        settings: { t_max: 3, solver_fine_pitch_mm: 0.4 },
        modules: { denoise: true },
      },
    },
  }, { label: "Loaded run" });
  assert.equal(profile.kind, "temporary");
  assert.equal(profile.settings.t_max, 3);
  assert.equal(profile.settings.solver_fine_pitch_mm, 0.4);
  assert.deepEqual(profile.modules, { denoise: true });
});

test("chroma controls round-trip raw multipliers without quantization", async () => {
  const { app } = await harness();
  for (let quarterSteps = -12; quarterSteps <= 12; quarterSteps += 1) {
    const position = quarterSteps / 4;
    const weight = app.commands.chromaWeightFromSliderPosition(position);
    assert.ok(Math.abs(app.commands.chromaWeightToSliderPosition(weight) - position) < 1e-12);
  }
  assert.equal(app.commands.formatChromaWeightReadout(5), "5.00");
  assert.equal(app.commands.normalizeChromaWeight(0), 1);
});

test("luminance mode expands to authoritative backend flags", async () => {
  const { app } = await harness();
  app.commands.applyLuminanceMode("luminance_detail");
  assert.equal(app.state.settings.config.luminance_mode, "luminance_detail");
  assert.equal(app.state.settings.config.luminance_handler_enabled, true);
  assert.equal(app.state.settings.config.luminance_handler_mode, "boundary_ceiling");
  assert.equal(app.state.settings.config.enforce_printability, true);
  assert.equal(app.state.settings.config.luminance_detail_authoring_printability, "absolute_finalgate");
  app.commands.applyLuminanceMode("standard", { resetStandard: true });
  assert.equal(app.state.settings.config.luminance_handler_enabled, false);
  assert.equal(app.state.settings.config.luminance_detail_authoring_printability, "off");
});

test("solve preflight math reports layer alignment and nozzle constraints", async () => {
  const { app } = await harness();
  const aligned = app.commands.calculateStackLayerAlignment(0.08, 0.2, 0.08, 2.28);
  assert.equal(aligned.remainderMm, 0);
  const misaligned = app.commands.calculateStackLayerAlignment(0.08, 0.2, 0.08, 2.3);
  assert.ok(misaligned.remainderMm > 0);
  assert.match(app.commands.buildStackLayerAlignmentIssue(misaligned), /whole Layer Height steps/);
  assert.match(app.commands.buildSolvePitchNozzleIssue(0.2, 0.4), /cannot be smaller/);
});

test("printer printability uses bounded whole-nozzle length multipliers", async () => {
  const { app } = await harness();
  assert.deepEqual(app.commands.defaultNozzleLineWidths(0.2), {
    line_width: 0.22,
    max_line_width: 0.25,
    min_line_length_multiplier: 2,
  });
  assert.equal(app.commands.formatNozzleDerivedLengthMm(0.2, 2), "0.4");
  assert.equal(app.commands.formatNozzleDerivedLengthMm(0.4, 10), "4");
  assert.equal(app.commands.formatNozzleDerivedLengthMm(0.3333333, 3), "0.999999");

  const normalized = app.commands.normalizeNozzleProfile({
    size: 0.4,
    min_layer_height: 0.08,
    max_layer_height: 0.32,
    line_width: 0.2,
    max_line_width: 0.3,
    min_line_length_multiplier: 3,
  });
  assert.equal(normalized.line_width, 0.4);
  assert.equal(normalized.max_line_width, 0.4);
  assert.equal(normalized.min_line_length_multiplier, 3);
  assert.equal("min_line_width" in normalized, false);
  assert.equal("min_line_length" in normalized, false);

  const sizeInput = fakeElement();
  sizeInput.value = "0.2";
  sizeInput.setCustomValidity = message => { sizeInput.validationMessage = message; };
  const multiplierInput = fakeElement();
  multiplierInput.value = "1";
  multiplierInput.setCustomValidity = message => { multiplierInput.validationMessage = message; };
  const output = fakeElement();
  const row = fakeElement();
  row.querySelector = selector => ({
    ".nz-size": sizeInput,
    ".nz-min-ll-mult": multiplierInput,
    ".nz-min-ll-derived": output,
  }[selector] || null);
  assert.equal(app.commands.validateNozzleRow(row), false);
  assert.match(multiplierInput.validationMessage, /whole number from 2 through 10/);
  assert.equal(row.classList.contains("is-invalid"), true);

  multiplierInput.value = "3";
  app.commands.syncNozzleDerivedLength(row);
  assert.equal(output.textContent, "× nozzle = 0.6 mm");
  assert.equal(app.commands.validateNozzleRow(row), true);
  assert.equal(multiplierInput.validationMessage, "");
  assert.equal(row.classList.contains("is-invalid"), false);
});

test("live config uses server-authoritative active printer thresholds", async () => {
  const { app } = await harness();
  app.state.session.activeNozzle = {
    size: 0.4,
    min_line_length_multiplier: 2,
  };
  app.state.session.activePrintability = {
    minimum_extrusion_width_mm: 0.4,
    minimum_line_length_mm: 0.8,
    minimum_component_area_mm2: 0.32,
  };
  global.document = {
    querySelector() { return null; },
    querySelectorAll() { return []; },
  };
  try {
    app.commands.readConfigFromUI();
    assert.equal(app.state.settings.config.printability_minimum_extrusion_width_mm, 0.4);
    assert.equal(app.state.settings.config.printability_minimum_line_length_mm, 0.8);
  } finally {
    delete global.document;
  }
});

test("printer editor stays open with its draft after a failed save", async () => {
  const printerPage = fakeElement();
  const draft = {
    printers: [{
      id: "printer-a",
      name: "Unsaved draft",
      nozzle_profiles: [{ size: 0.2, min_line_length_multiplier: 3 }],
    }],
    active_printer_id: "printer-a",
    active_nozzle_size: 0.2,
  };
  const { app } = await harness({
    api: {
      savePrinters: async () => { throw new Error("offline"); },
    },
    elements: { "#printerConfigPage": printerPage },
  });
  app.state.session.printersData = draft;
  app.state.session.printerConfigEditingId = "printer-a";
  app.commands._readPrinterFromConfigPage = () => draft.printers[0];
  app.commands.showToast = () => {};
  app.commands.switchTab = () => { throw new Error("must not navigate"); };

  assert.equal(await app.commands.hidePrinterConfigPage("image"), false);
  assert.equal(printerPage.classList.contains("is-hidden"), false);
  assert.equal(app.state.session.printersData.printers[0].name, "Unsaved draft");
  assert.equal(app.state.session.printersData.printers[0].nozzle_profiles[0].min_line_length_multiplier, 3);
});

test("successful printer saves reconcile active printability from the server", async () => {
  const printerPage = fakeElement();
  const draft = {
    printers: [{
      id: "printer-a",
      name: "Draft name",
      nozzle_profiles: [{ size: 0.2, min_line_length_multiplier: 3 }],
    }],
    active_printer_id: "printer-a",
    active_nozzle_size: 0.2,
  };
  const authoritative = {
    ok: true,
    printers: [{
      id: "printer-a",
      name: "Canonical name",
      nozzle_profiles: [{ size: 0.2, min_line_length_multiplier: 3 }],
    }],
    active_printer_id: "printer-a",
    active_nozzle_size: 0.2,
    active: {
      printer: { id: "printer-a", name: "Canonical name", max_print_area: { x: 180, y: 180 } },
      nozzle: { size: 0.2, min_line_length_multiplier: 3 },
      printability: {
        minimum_extrusion_width_mm: 0.2,
        minimum_line_length_mm: 0.6,
        minimum_component_area_mm2: 0.12,
      },
    },
  };
  const { app } = await harness({
    api: { savePrinters: async () => authoritative },
    elements: { "#printerConfigPage": printerPage },
  });
  app.state.session.printersData = draft;
  app.state.session.printerConfigEditingId = "printer-a";
  app.commands._readPrinterFromConfigPage = () => draft.printers[0];
  app.commands.renderPrinterRail = () => {};
  app.commands.updateDerivedParams = () => {};
  app.commands.switchTab = () => {};

  assert.equal(await app.commands.hidePrinterConfigPage("image"), true);
  assert.equal(printerPage.classList.contains("is-hidden"), true);
  assert.equal(app.state.session.printersData.printers[0].name, "Canonical name");
  assert.equal(app.state.session.activePrintability.minimum_line_length_mm, 0.6);
  assert.equal(app.state.settings.config.printability_minimum_line_length_mm, 0.6);
});

test("a post-save render failure never rolls back authoritative printer state", async () => {
  const printerPage = fakeElement();
  const draft = {
    printers: [{
      id: "printer-a",
      name: "Draft",
      nozzle_profiles: [{ size: 0.2, min_line_length_multiplier: 2 }],
    }],
    active_printer_id: "printer-a",
    active_nozzle_size: 0.2,
  };
  const authoritative = {
    printers: [{
      id: "printer-a",
      name: "Saved",
      nozzle_profiles: [{ size: 0.2, min_line_length_multiplier: 4 }],
    }],
    active_printer_id: "printer-a",
    active_nozzle_size: 0.2,
    active: {
      printer: { id: "printer-a", name: "Saved" },
      nozzle: { size: 0.2, min_line_length_multiplier: 4 },
      printability: {
        minimum_extrusion_width_mm: 0.2,
        minimum_line_length_mm: 0.8,
        minimum_component_area_mm2: 0.16,
      },
    },
  };
  const { app } = await harness({
    api: { savePrinters: async () => authoritative },
    elements: { "#printerConfigPage": printerPage },
  });
  app.state.session.printersData = draft;
  app.state.session.printerConfigEditingId = "printer-a";
  app.commands._readPrinterFromConfigPage = () => draft.printers[0];
  app.commands.renderPrinterRail = () => {};
  app.commands.updateDerivedParams = () => { throw new Error("render failed"); };
  app.commands.switchTab = () => { throw new Error("must not navigate"); };
  let toast = "";
  app.commands.showToast = message => { toast = message; };
  const originalConsoleError = console.error;
  console.error = () => {};
  try {
    assert.equal(await app.commands.hidePrinterConfigPage("image"), false);
  } finally {
    console.error = originalConsoleError;
  }

  assert.equal(printerPage.classList.contains("is-hidden"), false);
  assert.equal(app.state.session.printersData.printers[0].name, "Saved");
  assert.equal(app.state.session.activePrintability.minimum_line_length_mm, 0.8);
  assert.match(toast, /was saved/);
});

test("filament eligibility and reconciliation preserve explicit choices and default new IDs on", async () => {
  const { app } = await harness();
  assert.equal(app.commands.isGenerationEligibleFilament(filaments[1]), true);
  assert.equal(app.commands.isGenerationEligibleFilament(filaments[3]), false);
  assert.equal(app.commands.isGenerationEligibleFilament(filaments[4]), false);
  assert.deepEqual(
    app.commands.reconcileEnabledFilamentIds(["red", "blue", "new"], {
      eligible_ids: ["red", "blue", "removed"],
      enabled_ids: ["blue", "removed"],
    }),
    ["blue", "new"],
  );
});

test("palette gating distinguishes missing, unavailable, and user-disabled filaments", async () => {
  const { app } = await harness();
  app.state.palette.enabledFilaments = new Set(["white"]);
  const issues = app.commands.getPaletteGatingIssues(["missing", "excluded", "red"]);
  assert.deepEqual(issues, {
    missing: ["missing"], unavailable: ["excluded"], disabled: ["red"],
  });
  const message = app.commands.buildPaletteGatingMessage(issues);
  assert.match(message, /not present/);
  assert.match(message, /unavailable for generation/);
  assert.match(message, /Manage Filaments/);
});

test("palette suggestions use solve-mode names for newly generated cards", async () => {
  const { app } = await harness();
  app.state.palette.stagingDeck = [];
  app.state.palette.nextDeckNum = 1;
  app.commands.renderCreationTab = () => {};
  app.commands.updateRail = () => {};
  app.commands.showToast = () => {};

  app.commands._processSuggestResults({
    palette_mode: "standard",
    candidates: [{ filament_ids: ["red"], mean_de: 1.2 }],
  });
  app.commands._processSuggestResults({
    palette_mode: "luminance_detail",
    candidates: [{ filament_ids: ["blue"], mean_de: 1.1 }],
  });

  assert.equal(app.state.palette.stagingDeck[0].name, "Color suggested 1");
  assert.equal(app.state.palette.stagingDeck[1].name, "Luminance suggested 2");
  assert.equal("n_out_of_gamut" in app.state.palette.stagingDeck[0].gamut, false);
  assert.equal("total_pixels" in app.state.palette.stagingDeck[0].gamut, false);
});

test("palette hover preview omits the internal out-of-gamut diagnostic", async () => {
  const { app } = await harness();
  app.commands.esc = (value) => String(value);
  const html = app.commands.buildRailDeckHoverPreview({
    name: "Preview palette",
    filament_ids: ["red"],
    gamut: {
      status: "done",
      suggestion_mean_de: 0.024,
      n_out_of_gamut: 37,
      total_pixels: 100,
    },
  });

  assert.match(html, /Suggest dE 0\.024/);
  assert.doesNotMatch(html, /\bOOG\b|>37</);
});

test("suggestion clear confirmation cancels stale timers and synchronizes accessibility", async () => {
  const clearButton = fakeElement();
  const { app } = await harness({ elements: { "#clearDeckBtn": clearButton } });
  app.commands.renderCreationTab = () => {};
  app.state.palette.stagingDeck = [{ id: "suggest-1" }];

  app.commands.syncStagingClearButton();
  assert.equal(clearButton.disabled, false);
  assert.equal(clearButton.textContent, "Clear");
  assert.equal(clearButton["aria-label"], "Clear suggested palettes");

  app.commands.armStagingClearConfirm();
  assert.equal(clearButton.textContent, "Confirm?");
  assert.equal(clearButton["aria-label"], "Confirm clearing all suggested palettes");
  assert.ok(app.state.palette.stagingClearConfirmTimer);

  app.commands.handleStagingClearClick();
  assert.deepEqual(app.state.palette.stagingDeck, []);
  assert.equal(app.state.palette.stagingClearConfirmTimer, null);
  assert.equal(app.state.palette.stagingClearConfirmPending, false);
  assert.equal(clearButton.disabled, true);
  assert.equal(clearButton.textContent, "Clear");
  assert.equal(clearButton.title, "No suggested palettes to clear");
});

test("runtime-scoped filament persistence never falls back to ambient legacy identity", async () => {
  const storage = memoryStorage({ prisma_enabled_filaments: '["red"]' });
  const { app } = await harness({ storage });
  assert.equal(app.commands.authoritativeRuntimeLibraryId(), null);
  assert.equal(app.state.palette.enabledFilamentPersistenceReady, false);
  app.state.session.modelLibraryManager.status = { runtime_active_library_id: "library-a" };
  assert.equal(app.commands.authoritativeRuntimeLibraryId(), "library-a");
  app.commands.reconcileEnabledFilamentsForRuntimeLibrary();
  const scoped = JSON.parse(storage.getItem("prisma_enabled_filaments_by_library"));
  assert.ok(scoped.libraries["library-a"]);
  assert.equal(storage.getItem("prisma_enabled_filaments"), null);
});

test("loaded palette restoration prefers deck, then saved library, then ad hoc", async () => {
  const { app } = await harness();
  const support = { base: "white", capEffective: "white" };
  const deck = [{ id: "deck-a", filament_ids: ["red", "blue"], support }];
  assert.deepEqual(app.commands.chooseLoadedPaletteRestoreAction({
    filamentIds: ["red", "blue"], support, deckCards: deck, savedPalettes: [],
  }), { kind: "reuse-deck", cardId: "deck-a" });
  assert.equal(app.commands.chooseLoadedPaletteRestoreAction({
    filamentIds: ["red"], support, deckCards: [], savedPalettes: [],
  }).kind, "add-ad-hoc");
});

test("solve runs own isolated immutable export histories", async () => {
  const { app } = await harness();
  const first = { id: "run-a", palette: ["red"], config: { t_max: 2 } };
  const second = { id: "run-b", palette: ["blue"], config: { t_max: 3 } };
  app.commands.ensureSolveRunExportState(first);
  app.commands.ensureSolveRunExportState(second);
  const result = {
    export_id: "export-a", output_format: "3mf", geometry_source: "field_derived",
    field_scale: 4, swap_plan: { instructions: "None" }, files: [],
  };
  const record = app.commands.appendExportRecordToRun(first, result, 100, 2.5);
  result.output_format = "stl";
  assert.equal(record.outputFormat, "3mf");
  assert.equal(first.exportRecords.length, 1);
  assert.equal(second.exportRecords.length, 0);
  assert.equal(app.commands.getSelectedExportRecord(first).id, "export-a");
});

test("export preview and dimensions use solved appearance and frozen border settings", async () => {
  const { app } = await harness();
  const run = {
    config: { border: true, border_width_mm: 2 },
    results: {
      predicted_appearance_url: "/appearance.png", predicted_url: "/predicted.png",
      image_w: 60, image_h: 40, image_domain_width_mm: 24, image_domain_height_mm: 16,
    },
  };
  assert.equal(app.commands.getExportSolvePreviewUrl(run), "/appearance.png");
  assert.deepEqual(app.commands.getExportSolveDimensions(run), {
    imageWidthPx: 60, imageHeightPx: 40,
    imageWidthMm: 24, imageHeightMm: 16,
    totalWidthMm: 28, totalHeightMm: 20,
    borderEnabled: true, borderWidthMm: 2,
  });
});

test("white-cap thickness semantics keep stable total, boundary, and detail slots", async () => {
  const { app } = await harness();
  const items = app.commands.getSolveWhiteCapThicknessItems({ results: {
    cap_map_url: "/total.png", detail_cap_map_url: "/detail.png",
  } });
  assert.deepEqual(items.map((item) => item.key), ["cap:total", "cap:boundary", "cap:detail"]);
  assert.deepEqual(items.map((item) => item.available), [true, false, true]);
  assert.deepEqual(app.commands.getSolveThicknessItems({ results: {
    filament_maps: [], cap_map_url: "/total.png", detail_cap_map_url: "/detail.png",
  } }).map((item) => item.key), ["cap:total", "cap:detail"]);
});

test("card lightbox dispatcher routes semantic card kinds through explicit commands", async () => {
  const { app } = await harness();
  const calls = [];
  app.commands.openSolveRunLightbox = (...args) => calls.push(["run", ...args]);
  app.commands.openSurfaceLightbox = (...args) => calls.push(["surface", ...args]);
  app.commands.openRecipeLightbox = (...args) => calls.push(["recipe", ...args]);
  app.commands.openThicknessLightboxForKey = (...args) => calls.push(["thickness", ...args]);
  app.commands.openSolveCardLightboxFromElement({ dataset: {
    solveCardKind: "run", runId: "run-a", view: "predicted",
  } });
  app.commands.openSolveCardLightboxFromElement({ dataset: {
    solveCardKind: "surface", runId: "run-a", view: "surface_explorer",
  } });
  app.commands.openSolveCardLightboxFromElement({ dataset: {
    solveCardKind: "recipe", runId: "run-a",
  } });
  app.commands.openSolveCardLightboxFromElement({ dataset: {
    solveCardKind: "thickness", runId: "run-a", mapKey: "cap:detail",
  } });
  assert.deepEqual(calls, [
    ["run", "run-a", "predicted"],
    ["surface", "surface_explorer", "run-a"],
    ["recipe", "run-a"],
    ["thickness", "run-a", "cap:detail"],
  ]);
});

test("active solve and export ownership block destructive run deletion", async () => {
  const { app } = await harness();
  const run = { id: "run-a", results: null };
  app.state.solve.activeSolveRunId = run.id;
  app.state.solve.solveStatus = { status: "running" };
  assert.match(app.commands.getSolveRunDeleteBlockReason(run), /Cancel this solve/);
  run.results = {};
  app.state.solve.solveStatus = { status: "idle" };
  app.state.export.exportRunning = true;
  app.state.export.activeExportRunId = run.id;
  assert.match(app.commands.getSolveRunDeleteBlockReason(run), /Cancel this export/);
});

test("image aspect default uses 120 mm on the source short side", async () => {
  const { app } = await harness();
  app.state.image.selectedImage = { width: 800, height: 400 };
  app.commands.applyImageAspectDefault();
  assert.equal(app.state.image.frameState.heightMm, 120);
  assert.equal(app.state.image.frameState.widthMm, 240);
  app.state.image.selectedImage = { width: 400, height: 800 };
  app.commands.applyImageAspectDefault();
  assert.equal(app.state.image.frameState.widthMm, 120);
  assert.equal(app.state.image.frameState.heightMm, 240);
});

test("saved-run identity, timestamp, and tiers are stable behavior", async () => {
  const { app } = await harness();
  const saved = { save_id: "save-a", tier: "saved", saved_at: "2026-07-16T12:00:00Z" };
  assert.equal(app.commands.savedRunKey(saved), "saved:save-a");
  assert.equal(app.commands.savedRunTierLabel(saved), "Saved");
  assert.match(app.commands.formatSavedRunTimestamp(saved.saved_at), /2026|Jul/);
});

test("run name validation reserves automatic labels and clears automatic save defaults", async () => {
  const { app } = await harness();
  for (const label of ["Run 7", " run 007 ", "rUn 9"]) {
    assert.match(app.commands.validateWritableRunLabel(label), /reserved for automatic run labels/);
  }
  assert.equal(app.commands.validateWritableRunLabel("Run Seven"), "");
  assert.equal(app.commands.validateWritableRunLabel(""), "Run name cannot be empty.");
  assert.equal(app.commands.initialSaveRunLabel({ label: "Run 42" }), "");
  assert.equal(app.commands.initialSaveRunLabel({ label: "Portrait" }), "Portrait");
});

test("generic numeric module controls clamp the flattening cap to descriptor bounds", async () => {
  const { app } = await harness();
  const descriptor = { type: "int", min: 2, max: 500 };
  assert.deepEqual(app.commands.coerceNumericParamValue(descriptor, "1", 100), {
    ok: true, value: 2,
  });
  assert.deepEqual(app.commands.coerceNumericParamValue(descriptor, "501", 100), {
    ok: true, value: 500,
  });
  assert.deepEqual(app.commands.coerceNumericParamValue(descriptor, "2", 100), {
    ok: true, value: 2,
  });
});

test("saving a run is single-flight and applies the server label authoritatively", async () => {
  let releaseSave;
  let saveCalls = 0;
  const saveResponse = new Promise((resolve) => { releaseSave = resolve; });
  const button = fakeElement();
  const { app } = await harness({ api: {
    saveRun: async () => { saveCalls += 1; return saveResponse; },
  } });
  const run = { id: "run-a", label: "Run 1", results: {} };
  app.state.solve.solveRuns = [run];
  let solveRenders = 0;
  app.commands.renderSolveTab = () => { solveRenders += 1; };
  app.commands.showToast = () => {};

  const first = app.commands.saveSolveRun(run.id, "Portrait", button);
  const duplicate = await app.commands.saveSolveRun(run.id, "Duplicate", button);
  assert.equal(duplicate, false);
  assert.equal(saveCalls, 1);
  assert.equal(button.disabled, true);
  assert.equal(button["aria-busy"], "true");

  releaseSave({ save_id: "stable-save-id", label: "Server Portrait" });
  assert.equal(await first, true);
  assert.equal(run.label, "Server Portrait");
  assert.equal(run.save_pending, false);
  assert.equal(solveRenders, 1);
});

test("failed or orphaned saves do not mutate unrelated run state", async () => {
  const button = fakeElement();
  const { app } = await harness({ api: {
    saveRun: async () => { throw new Error("save failed"); },
  } });
  const run = { id: "run-a", label: "Original", results: {} };
  app.state.solve.solveRuns = [run];
  app.commands.showToast = () => {};
  app.commands.renderSolveTab = () => {};
  assert.equal(await app.commands.saveSolveRun(run.id, "New name", button), false);
  assert.equal(run.label, "Original");
  assert.equal(run.save_pending, false);
  assert.equal(button.disabled, false);
  assert.equal(button["aria-busy"], undefined);

  let releaseSave;
  app.api.saveRun = () => new Promise((resolve) => { releaseSave = resolve; });
  let renderCount = 0;
  app.commands.renderSolveTab = () => { renderCount += 1; };
  const orphaned = app.commands.saveSolveRun(run.id, "Orphan", button);
  app.state.solve.solveRuns = [];
  releaseSave({ save_id: "save-a", label: "Server Orphan" });
  assert.equal(await orphaned, true);
  assert.equal(renderCount, 0);
});

test("authoritative run labels refresh open lightbox and settings surfaces", async () => {
  const { app } = await harness();
  const title = fakeElement();
  title.textContent = "Run 1";
  const contour = fakeElement();
  contour.setAttribute("aria-label", "Run 1 layer contours");
  const lightboxContent = fakeElement();
  lightboxContent.querySelector = (selector) => (
    selector === ".comp-lightbox-runtitle" ? title : null
  );
  lightboxContent.querySelectorAll = (selector) => (
    selector === "[aria-label]" ? [contour] : []
  );
  app.state.ui.$ = (selector) => (
    selector === "#compLightboxContent" ? lightboxContent : null
  );
  app.state.solve._solveLightboxState = { runId: "run-a", kind: "solve" };
  app.commands.refreshOpenSolveRunLabels(
    { id: "run-a", label: "Server Portrait" },
    "Run 1",
  );
  assert.equal(title.textContent, "Server Portrait");
  assert.equal(contour["aria-label"], "Server Portrait layer contours");

  const settingsLabel = fakeElement();
  const settingsToggle = fakeElement();
  const settingsBody = fakeElement();
  const settingsPanel = fakeElement();
  settingsPanel.querySelector = (selector) => ({
    ".run-settings-run-label": settingsLabel,
    ".run-settings-advanced-toggle": settingsToggle,
    ".run-settings-body": settingsBody,
  })[selector] || null;
  app.state.solve.solveRuns = [{ id: "run-a", label: "Server Portrait" }];
  app.state.solve.solveRunSettingsPanelEl = settingsPanel;
  app.state.solve.solveRunSettingsPanelRunId = "run-a";
  app.commands.buildReadOnlyRunSettingsHtml = () => "settings";
  app.commands.renderSolveRunSettingsPanel();
  assert.equal(settingsPanel["aria-label"], "Settings used by Server Portrait");
  assert.equal(settingsLabel.textContent, "Server Portrait");
});

test("solve-history clear controls stay synchronized and disable invalid actions", async () => {
  const solveClear = fakeElement();
  const exportClear = fakeElement();
  const { app } = await harness({ elements: {
    "#clearSolveHistoryBtn": solveClear,
    "#exportClearSolveHistoryBtn": exportClear,
  } });
  app.state.solve.solveRuns = [{ id: "run-a", results: {} }];
  app.state.solve.solveStatus = { status: "idle" };
  app.state.export.exportRunning = false;

  app.commands.syncSolveHistoryClearButtons();
  assert.equal(solveClear.disabled, false);
  assert.equal(exportClear.disabled, false);
  app.commands.armSolveHistoryClearConfirm();
  assert.equal(solveClear.textContent, "Clear?");
  assert.equal(exportClear.textContent, "Clear?");
  assert.equal(solveClear["aria-label"], "Confirm clearing all solve runs");
  app.commands.resetSolveHistoryClearConfirm();
  assert.equal(solveClear.textContent, "Clear");
  assert.equal(exportClear.textContent, "Clear");

  app.state.solve.solveRuns = [];
  app.commands.syncSolveHistoryClearButtons();
  assert.equal(solveClear.disabled, true);
  assert.equal(exportClear.disabled, true);
  assert.equal(solveClear.title, "No solve runs to clear");
});

test("solve cards expose listbox semantics, omit loaded provenance, and gate Save by completion", async () => {
  const cards = fakeElement();
  const { app } = await harness({ elements: { "#solveRunCards": cards } });
  app.commands.hideSolveRunHoverPreview = () => {};
  app.commands.bindSolveRunCardAuxiliaryInteractions = () => {};
  app.commands.formatSolveRunCardRmse = () => "1.2 dE";
  app.commands.esc = (value) => String(value);
  app.commands.escAttr = (value) => String(value);
  app.state.solve.solveRuns = [{
    id: "run-a", label: "Loaded portrait", loaded_from_archive: true,
    palette: ["red"], results: null,
    config: { base_filament: "white", cap_filament: "__same__" },
  }];

  app.commands.renderSolveRunSidebar();

  assert.equal(cards.role, "listbox");
  assert.equal(cards["aria-multiselectable"], "true");
  assert.match(cards.innerHTML, /role="option"/);
  assert.match(cards.innerHTML, /aria-selected="false"/);
  assert.match(cards.innerHTML, /solve-run-save-btn[^>]+disabled/);
  assert.match(cards.innerHTML, /solve-run-delete-slot/);
  assert.doesNotMatch(cards.innerHTML, />Confirm\?</);
  assert.match(cards.innerHTML, /solve-run-support-tray/);
  assert.match(cards.innerHTML, /White Base\/Cap: White/);
  assert.doesNotMatch(cards.innerHTML, />Loaded<\/span>|solve-run-loaded-badge/);
});

test("image library refresh preserves a private saved-run source outside available images", async () => {
  const privateSource = {
    filename: "portrait.jpg",
    width: 1200,
    height: 800,
    source_ref: "loaded-run:loaded-1",
    temporary: true,
  };
  const { app } = await harness({ api: {
    fetchImages: async () => [{ filename: "library.jpg", width: 640, height: 480 }],
  } });
  app.state.session.apiConnected = true;
  app.state.image.selectedImage = privateSource;
  app.commands.renderImageTab = () => {};
  app.commands.updateRail = () => {};
  let syncCount = 0;
  app.commands.syncConfigToServer = async () => { syncCount += 1; };

  await app.commands.refreshImageLibrary();

  assert.equal(app.state.image.selectedImage, privateSource);
  assert.deepEqual(app.state.image.availableImages.map(image => image.filename), ["library.jpg"]);
  assert.equal(syncCount, 0);
});

test("config synchronization carries the active private source reference and clears it for library images", async () => {
  const payloads = [];
  const { app } = await harness({ api: {
    updateConfig: async (payload) => {
      payloads.push(payload);
      return { config: payload };
    },
  } });
  app.state.session.apiConnected = true;
  app.state.session.printerConfig = { ams_slots: 4, white_slots: 1 };
  app.commands.syncConfigFromModuleState = () => {};
  app.commands.readConfigFromUI = () => {};
  app.commands.getActivePalette = () => ["red"];
  app.commands.getBaseFilament = () => "white";
  app.state.image.selectedImage = {
    filename: "portrait.jpg",
    source_ref: "loaded-run:loaded-1",
  };

  await app.commands.syncConfigToServer({ throwOnError: true });
  app.state.image.selectedImage = { filename: "library.jpg" };
  await app.commands.syncConfigToServer({ throwOnError: true });

  assert.equal(payloads[0].image_path, "portrait.jpg");
  assert.equal(payloads[0].image_source_ref, "loaded-run:loaded-1");
  assert.equal(payloads[1].image_path, "library.jpg");
  assert.equal(payloads[1].image_source_ref, null);
});

test("failed loaded-run synchronization rolls back all staged client state", async () => {
  const { app } = await harness({ api: {
    fetchImages: async () => [],
  } });
  const previousImage = { filename: "library.jpg" };
  const previousDeck = [{ id: "deck-1", filament_ids: ["blue"] }];
  app.state.image.selectedImage = previousImage;
  app.state.settings.config.t_max = 3;
  app.state.palette.deck = previousDeck;
  app.state.palette.activeDeckId = "deck-1";
  app.commands.loadImages = async () => {};
  app.commands.applyImageAspectDefault = () => {};
  app.commands.renderSettingsTab = () => {};
  app.commands.renderImageTab = () => {};
  app.commands.renderSolveTab = () => {};
  app.commands.renderExportTab = () => {};
  app.commands.renderFrameCanvas = () => {};
  app.commands.updateRail = () => {};
  app.commands.restoreLoadedRunPaletteToDeck = async () => {
    app.state.palette.deck = [{ id: "loaded-deck", filament_ids: ["red"] }];
    app.state.palette.activeDeckId = "loaded-deck";
  };
  let syncCount = 0;
  app.commands.syncConfigToServer = async () => {
    syncCount += 1;
    if (syncCount === 1) throw new Error("network interrupted");
  };

  await assert.rejects(
    app.commands.applyLoadedRun({
      card_id: "loaded-1",
      label: "Portrait",
      config: {
        image_path: "portrait.jpg",
        image_source_ref: "loaded-run:loaded-1",
        palette: ["red"],
        white_base: "white",
        t_max: 9,
      },
      source_image: {
        filename: "portrait.jpg",
        source_ref: "loaded-run:loaded-1",
        temporary: true,
      },
      result: {},
    }),
    /network interrupted/,
  );

  assert.equal(app.state.solve.solveRuns.length, 0);
  assert.equal(app.state.solve.solveRunCounter, 0);
  assert.equal(app.state.solve.loadedRunApplyRunning, false);
  assert.equal(app.state.image.selectedImage, previousImage);
  assert.equal(app.state.settings.config.t_max, 3);
  assert.deepEqual(app.state.palette.deck, previousDeck);
  assert.equal(app.state.palette.activeDeckId, "deck-1");
  assert.equal(syncCount, 2);
});

test("loaded result keeps historical printability while re-solve state uses active printer", async () => {
  const { app } = await harness();
  app.state.session.activePrintability = {
    minimum_extrusion_width_mm: 0.4,
    minimum_line_length_mm: 0.8,
    minimum_component_area_mm2: 0.32,
  };
  app.commands.renderSettingsTab = () => {};
  app.commands.renderImageTab = () => {};
  app.commands.renderSolveTab = () => {};
  app.commands.renderFrameCanvas = () => {};
  app.commands.updateRail = () => {};
  app.commands.restoreLoadedRunPaletteToDeck = async () => {};
  app.commands.syncConfigToServer = async () => {
    app.state.settings.config.printability_minimum_extrusion_width_mm =
      app.state.session.activePrintability.minimum_extrusion_width_mm;
    app.state.settings.config.printability_minimum_line_length_mm =
      app.state.session.activePrintability.minimum_line_length_mm;
  };

  await app.commands.applyLoadedRun({
    card_id: "loaded-1",
    label: "Historical run",
    config: {
      palette: ["red"],
      white_base: "white",
      printability_minimum_extrusion_width_mm: 0.16,
      printability_minimum_line_length_mm: 0.4,
    },
    result: {},
  });

  const loadedCard = app.state.solve.solveRuns[0];
  assert.equal(loadedCard.config.printability_minimum_extrusion_width_mm, 0.16);
  assert.equal(loadedCard.config.printability_minimum_line_length_mm, 0.4);
  assert.equal(app.state.settings.config.printability_minimum_extrusion_width_mm, 0.4);
  assert.equal(app.state.settings.config.printability_minimum_line_length_mm, 0.8);
});

test("Clear Temp drains config sync and force-reconciles private source and stale histories", async () => {
  let clearCalled = false;
  const { app } = await harness({ api: {
    clearAllTempFiles: async () => {
      clearCalled = true;
      return {
        cleared_source_ref: "loaded-run:loaded-1",
        active_image_cleared: true,
        config: { image_path: null, image_source_ref: null },
      };
    },
  } });
  const eventBindings = await import(moduleUrl("features/event-bindings.js"));
  eventBindings.installFeaturesEventBindings(app);
  const clearButton = fakeElement();
  app.state.ui.$ = (selector) => selector === "#clearAllTempBtn" ? clearButton : null;
  app.commands.appConfirm = async () => true;
  app.commands.showToast = () => {};
  app.commands.renderImageTab = () => {};
  app.commands.updateRail = () => {};
  app.commands.renderSolveTab = () => {};
  app.commands.renderExportTab = () => {};
  app.state.image.selectedImage = {
    filename: "portrait.jpg",
    source_ref: "loaded-run:loaded-1",
  };
  app.state.settings.config.image_path = "portrait.jpg";
  app.state.settings.config.image_source_ref = "loaded-run:loaded-1";
  app.state.solve.solveRuns = [{ id: "loaded-1", results: {} }];
  app.state.solve.solveStatus = { status: "running" };
  app.state.export.exportRunning = true;
  let releaseSync;
  app.state.settings._configSyncChain = new Promise((resolve) => {
    releaseSync = resolve;
  });
  app.commands.syncConfigToServer = async () => {};
  const clearing = app.commands.clearAllTempFiles();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(clearCalled, false);
  assert.equal(clearButton.disabled, true);
  releaseSync();
  await clearing;

  assert.equal(clearCalled, true);
  assert.equal(app.state.image.selectedImage, null);
  assert.equal(app.state.solve.solveRuns.length, 0);
  assert.equal(app.state.solve.solveStatus.status, "idle");
  assert.equal(app.state.export.exportRunning, false);
  assert.equal(clearButton.disabled, false);
  assert.equal(app.state.session.clearTempRunning, false);
});

test("operation progress exposes cancellation only to its owning workflow", async () => {
  const overlay = fakeElement(); const cancel = fakeElement(); const fill = fakeElement();
  const label = fakeElement(); const elapsed = fakeElement();
  overlay.querySelector = () => fill;
  const elements = {
    "#dataSourceBadge": fakeElement(), "#tabSwitch": fakeElement(),
    "#settingsDrawer": fakeElement(), "#opProgress": overlay,
    "#compLightbox": fakeElement(), "#opProgressCancel": cancel,
    "#opProgressLabel": label, "#opProgressElapsed": elapsed,
  };
  const { app } = await harness({ elements });
  app.state.ui.$ = (selector) => elements[selector] || null;
  app.commands.startProgress("Generating", "swap-instructions", { cancellable: false });
  assert.equal(overlay.dataset.owner, "swap-instructions");
  assert.equal(overlay.dataset.cancellable, "false");
  assert.equal(cancel.hidden, true);
  assert.equal(cancel.disabled, true);
  app.commands.stopProgress();
});

test("profile loading drains stale sync work before applying and persisting saved values", async () => {
  const { app } = await harness();
  app.state.session.apiConnected = true;
  app.state.settings.config.t_max = 4;
  let releaseStaleSync;
  app.state.settings._configSyncChain = new Promise((resolve) => {
    releaseStaleSync = () => { app.state.settings.config.t_max = 4; resolve(); };
  });
  let syncedValue = null;
  app.commands._applyModuleSnapshot = async () => {};
  app.commands.renderSettingsTab = () => {};
  app.commands.renderSettingsProfileBar = () => {};
  app.commands.syncConfigToServer = async () => { syncedValue = app.state.settings.config.t_max; };

  const loading = app.commands._doLoadSettingsProfile({
    id: "saved", kind: "named", name: "Saved", settings: { t_max: 3 }, modules: {},
  });
  await Promise.resolve();
  releaseStaleSync();
  await loading;
  assert.equal(app.state.settings.config.t_max, 3);
  assert.equal(syncedValue, 3);
  assert.equal(app.state.settings.loadedProfileSnapshot.settings.t_max, 3);
});

test("profile load failures restore prior local state and do not claim clean success", async () => {
  const { app } = await harness();
  app.state.session.apiConnected = true;
  app.state.settings.config.t_max = 4;
  app.commands.renderSettingsTab = () => {};
  app.commands.renderSettingsProfileBar = () => {};
  app.commands._applyModuleSnapshot = async () => {};
  app.commands.syncConfigToServer = async () => { throw new Error("config write failed"); };
  await assert.rejects(
    app.commands._doLoadSettingsProfile({
      id: "broken", kind: "named", name: "Broken", settings: { t_max: 3 }, modules: {},
    }),
    /config write failed/,
  );
  assert.equal(app.state.settings.config.t_max, 4);
  assert.equal(app.state.settings.loadedProfileRef, null);
  assert.equal(app.state.settings.loadedProfileSnapshot, null);
});

test("solve start uses the injected palette preflight and blocks invalid palettes", async () => {
  const calls = [];
  const { app } = await harness({ api: {
    getExportStatus: async () => ({ status: "idle" }),
    apiPost: async (path, body) => {
      calls.push([path, body]);
      return { valid: false, excluded: ["red"] };
    },
    startSolve: async () => { calls.push(["start"]); return { job_id: "unexpected" }; },
  } });
  app.commands.updateSolveReadiness = () => {};
  app.commands.syncConfigToServer = async () => {};
  app.commands.getSolveSettingsPreflightIssues = () => [];
  app.commands.getActivePalette = () => ["red"];
  app.commands.getPaletteGatingIssues = () => ({ missing: [], unavailable: [], disabled: [] });
  app.commands.showToast = (message, kind) => calls.push([kind, message]);
  await app.commands.handleStartSolve();
  assert.deepEqual(calls[0], ["/palette/validate", { palette: ["red"] }]);
  assert.equal(calls.some(([kind]) => kind === "start"), false);
  assert.equal(calls.some(([kind]) => kind === "error"), true);
  assert.equal(app.state.solve.solveStartPending, false);
});

test("suggestion and export cancellation validate captured job ownership", async () => {
  const calls = [];
  const { app } = await harness({ api: {
    apiPost: async (path) => {
      calls.push(path);
      return { cancelled: true, job_id: "suggest-1" };
    },
    cancelExport: async (jobId) => ({ cancelled: true, job_id: jobId }),
  } });
  app.commands.renderSuggestCancellationState = () => {};
  app.commands.renderExportCancellationState = () => {};
  app.commands.showToast = (message) => calls.push(message);
  app.state.ui._suggestPolling = { jobId: "suggest-1" };
  app.state.ui.activeSuggestJobId = "suggest-1";
  await app.commands.requestSuggestCancellation();
  assert.equal(app.state.palette.suggestCancelPending, true);
  assert.match(calls[0], /suggest\/cancel\?job_id=suggest-1/);

  app.state.export.exportRunning = true;
  app.state.export.activeExportJobId = "export-1";
  await app.commands.requestExportCancellation();
  assert.equal(app.state.export.exportCancelPending, true);
  assert.ok(calls.includes("Export cancellation requested"));
});
