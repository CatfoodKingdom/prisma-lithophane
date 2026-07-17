"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const {
  createFeatureHarness,
  fakeElement,
  memoryStorage,
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
