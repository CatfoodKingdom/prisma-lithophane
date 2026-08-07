"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {
  createFeatureHarness,
  fakeElement,
} = require("./support/application_harness.cjs");

const vectors = JSON.parse(fs.readFileSync(
  path.join(__dirname, "fixtures", "solve_grid_rounding_cases.json"),
  "utf8",
));

function authoritativeGrid({
  requestedWidth = 100.2,
  requestedHeight = 100.2,
  resolvedWidth = 100.4,
  resolvedHeight = 100.4,
  widthCells = 251,
  heightCells = 251,
  pitch = 0.4,
  aligned = false,
  widthAligned = aligned,
  heightAligned = aligned,
} = {}) {
  return {
    rounding_mode: "half_up",
    pitch_mm: pitch,
    requested: { width_mm: requestedWidth, height_mm: requestedHeight },
    cells: { width: widthCells, height: heightCells },
    resolved: { width_mm: resolvedWidth, height_mm: resolvedHeight },
    delta: {
      width_mm: resolvedWidth - requestedWidth,
      height_mm: resolvedHeight - requestedHeight,
    },
    aligned: {
      width: widthAligned,
      height: heightAligned,
      all: widthAligned && heightAligned,
    },
  };
}

test("JavaScript grid mirror consumes the shared half-up rounding vectors", async () => {
  const { app } = await createFeatureHarness();
  for (const vector of vectors) {
    const result = app.commands.resolveSolveGrid(
      vector.width_mm,
      vector.height_mm,
      vector.pitch_mm,
    );
    assert.deepEqual(result.cells, vector.cells, vector.name);
    assert.deepEqual(result.resolved, vector.resolved, vector.name);
    assert.equal(result.aligned.all, vector.aligned, vector.name);
  }
});

test("Image warning and summary use resolved dimensions and exact border footprint", async () => {
  const warning = fakeElement();
  warning.classList.add("is-hidden");
  const printSize = fakeElement();
  const solvePixels = fakeElement();
  const original = fakeElement();
  const widthMarker = fakeElement();
  const heightMarker = fakeElement();
  const widthInput = fakeElement();
  const heightInput = fakeElement();
  const pitch = fakeElement();
  pitch.value = "0.4";
  const { app } = await createFeatureHarness({ elements: {
    "#imageSolveGridWarning": warning,
    "#infoPrintSize": printSize,
    "#infoSolvePx": solvePixels,
    "#infoOrigDims": original,
    "#outputWidthSolveGridWarning": widthMarker,
    "#outputHeightSolveGridWarning": heightMarker,
    "#outputWidthMm": widthInput,
    "#outputHeightMm": heightInput,
    "#cfgSolvePitch": pitch,
  } });
  app.state.image.selectedImage = { filename: "test.png", width: 640, height: 480 };
  app.state.image.frameState.widthMm = 100.2;
  app.state.image.frameState.heightMm = 100.2;
  app.state.settings.config.border = true;
  app.state.settings.config.border_width_mm = 3;

  app.commands.updateInfoGrid();

  assert.equal(warning.classList.contains("is-hidden"), false);
  assert.equal(
    warning.textContent,
    "⚠ Width & Height must be divisible by the 0.4 mm Solve Pitch. If not changed, the current Width & Height will be rounded to 100.4 × 100.4 mm.",
  );
  assert.equal(widthMarker.classList.contains("is-visible"), true);
  assert.equal(heightMarker.classList.contains("is-visible"), true);
  assert.equal(widthInput.getAttribute("aria-invalid"), "true");
  assert.equal(heightInput.getAttribute("aria-invalid"), "true");
  assert.equal(printSize.textContent, "106.4 × 106.4 mm");
  assert.equal(solvePixels.textContent, "251 × 251 = 63,001 px");
  assert.match(
    printSize.innerHTML,
    /data-axis="width">106\.4<\/span> × <span class="summary-axis-value is-solve-grid-incompatible" data-axis="height">106\.4/,
  );
  assert.match(
    solvePixels.innerHTML,
    /data-axis="width">251<\/span> × <span class="summary-axis-value is-solve-grid-incompatible" data-axis="height">251/,
  );
  assert.equal(app.commands.formatPhysicalMm(100), "100.0");
  assert.equal(app.commands.formatPhysicalMm(100.1234564), "100.123456");
  assert.equal(
    app.commands.buildSolveGridAdjustmentMessage(authoritativeGrid()),
    "The requested lithophane size is incompatible with the selected 0.4 mm Solve Pitch because neither the width nor the height resolves to a whole number of solve pixels.\n\n"
      + "Requested size: 100.2 × 100.2 mm\nAdjusted size: 100.4 × 100.4 mm (251 × 251 px)\n\n"
      + "Select Accept & Continue to apply the adjusted size. Select Cancel to return to the Image page without changing the dimensions.\n\n"
      + "With the border, the finished footprint will be 106.4 × 106.4 mm.",
  );
});

test("Image warning and markers identify only the incompatible axis", async () => {
  const warning = fakeElement();
  warning.classList.add("is-hidden");
  const widthMarker = fakeElement();
  const heightMarker = fakeElement();
  const widthInput = fakeElement();
  const heightInput = fakeElement();
  const pitch = fakeElement();
  pitch.value = "0.4";
  const { app } = await createFeatureHarness({ elements: {
    "#imageSolveGridWarning": warning,
    "#outputWidthSolveGridWarning": widthMarker,
    "#outputHeightSolveGridWarning": heightMarker,
    "#outputWidthMm": widthInput,
    "#outputHeightMm": heightInput,
    "#cfgSolvePitch": pitch,
  } });
  app.state.image.selectedImage = { filename: "test.png", width: 640, height: 480 };

  app.commands.renderSolveGridWarning(authoritativeGrid({
    requestedWidth: 100,
    requestedHeight: 100.2,
    resolvedWidth: 100,
    resolvedHeight: 100.4,
    widthCells: 250,
    heightCells: 251,
    widthAligned: true,
    heightAligned: false,
  }));

  assert.equal(
    warning.textContent,
    "⚠ Height must be divisible by the 0.4 mm Solve Pitch. If not changed, the current Height will be rounded to 100.4 mm.",
  );
  assert.equal(widthMarker.classList.contains("is-visible"), false);
  assert.equal(heightMarker.classList.contains("is-visible"), true);
  assert.equal(widthInput.getAttribute("aria-invalid"), "false");
  assert.equal(heightInput.getAttribute("aria-invalid"), "true");

  app.commands.renderSolveGridWarning(authoritativeGrid({
    requestedWidth: 100.2,
    requestedHeight: 100,
    resolvedWidth: 100.4,
    resolvedHeight: 100,
    widthCells: 251,
    heightCells: 250,
    widthAligned: false,
    heightAligned: true,
  }));

  assert.equal(
    warning.textContent,
    "⚠ Width must be divisible by the 0.4 mm Solve Pitch. If not changed, the current Width will be rounded to 100.4 mm.",
  );
  assert.equal(widthMarker.classList.contains("is-visible"), true);
  assert.equal(heightMarker.classList.contains("is-visible"), false);
});

test("Print and rendered summaries color only the incompatible dimension", async () => {
  const printSize = fakeElement();
  const solvePixels = fakeElement();
  const pitch = fakeElement();
  pitch.value = "0.4";
  const { app } = await createFeatureHarness({ elements: {
    "#infoPrintSize": printSize,
    "#infoSolvePx": solvePixels,
    "#cfgSolvePitch": pitch,
  } });
  app.state.image.selectedImage = { filename: "test.png", width: 640, height: 480 };
  app.state.image.frameState.widthMm = 100;
  app.state.image.frameState.heightMm = 100.2;

  app.commands.updateInfoGrid();

  assert.match(
    printSize.innerHTML,
    /class="summary-axis-value" data-axis="width">100\.0<\/span> × <span class="summary-axis-value is-solve-grid-incompatible" data-axis="height">100\.4/,
  );
  assert.match(
    solvePixels.innerHTML,
    /class="summary-axis-value" data-axis="width">250<\/span> × <span class="summary-axis-value is-solve-grid-incompatible" data-axis="height">251/,
  );
});

test("dimension remediation cancellation preserves requested dimensions and aspect mode", async () => {
  const { app } = await createFeatureHarness();
  app.state.image.selectedImage = { filename: "test.png", width: 10, height: 10 };
  app.state.image.frameState.widthMm = 100.2;
  app.state.image.frameState.heightMm = 100.2;
  app.state.image.frameState.arMode = "image";
  app.commands.syncConfigToServer = async () => ({ resolved_solve_grid: authoritativeGrid() });
  let promptMessage = null;
  let promptOptions = null;
  app.commands.appConfirm = async (message, options) => {
    promptMessage = message;
    promptOptions = options;
    return false;
  };

  const result = await app.commands.syncSolveDimensionsWithGridRemediation();

  assert.equal(result.proceed, false);
  assert.equal(app.state.image.frameState.widthMm, 100.2);
  assert.equal(app.state.image.frameState.heightMm, 100.2);
  assert.equal(app.state.image.frameState.arMode, "image");
  assert.match(promptMessage, /Select Accept & Continue to apply the adjusted size/);
  assert.equal(promptOptions.title, "Error: Invalid Lithophane Size");
  assert.equal(promptOptions.ok, "Accept & Continue");
  assert.deepEqual(promptOptions.emphasis, [
    "0.4 mm",
    "100.2 × 100.2 mm",
    "100.4 × 100.4 mm (251 × 251 px)",
  ]);
});

test("dimension remediation message names one incompatible dimension", async () => {
  const { app } = await createFeatureHarness();
  const message = app.commands.buildSolveGridAdjustmentMessage(authoritativeGrid({
    requestedWidth: 100,
    requestedHeight: 100.2,
    resolvedWidth: 100,
    resolvedHeight: 100.4,
    widthCells: 250,
    heightCells: 251,
    widthAligned: true,
    heightAligned: false,
  }));

  assert.equal(
    message,
    "The requested lithophane size is incompatible with the selected 0.4 mm Solve Pitch because the height does not resolve to a whole number of solve pixels.\n\n"
      + "Requested size: 100.0 × 100.2 mm\nAdjusted size: 100.0 × 100.4 mm (250 × 251 px)\n\n"
      + "Select Accept & Continue to apply the adjusted size. Select Cancel to return to the Image page without changing the dimensions.",
  );
});

test("dimension remediation accepts, switches to W×H, and verifies a second sync", async () => {
  const elements = {
    "#outputWidthMm": fakeElement(),
    "#outputHeightMm": fakeElement(),
  };
  const { app } = await createFeatureHarness({ elements });
  app.state.image.selectedImage = { filename: "test.png", width: 10, height: 10 };
  app.state.image.frameState.widthMm = 100.2;
  app.state.image.frameState.heightMm = 100.2;
  app.state.image.frameState.arMode = "image";
  let syncCount = 0;
  app.commands.syncConfigToServer = async () => {
    syncCount += 1;
    return {
      resolved_solve_grid: syncCount === 1
        ? authoritativeGrid()
        : authoritativeGrid({
          requestedWidth: 100.4,
          requestedHeight: 100.4,
          aligned: true,
        }),
    };
  };
  app.commands.appConfirm = async () => true;
  app.commands.renderFrameCanvas = () => {};
  app.commands.renderPreview = () => {};
  app.commands.updateInfoGrid = () => {};

  const result = await app.commands.syncSolveDimensionsWithGridRemediation();

  assert.equal(result.proceed, true);
  assert.equal(result.corrected, true);
  assert.equal(syncCount, 2);
  assert.equal(app.state.image.frameState.widthMm, 100.4);
  assert.equal(app.state.image.frameState.heightMm, 100.4);
  assert.equal(app.state.image.frameState.arMode, "specified");
  assert.equal(elements["#outputWidthMm"].value, "100.4");
  assert.equal(elements["#outputHeightMm"].value, "100.4");
});

test("dimension remediation aborts when the verification sync is inconsistent", async () => {
  const { app } = await createFeatureHarness();
  app.state.image.selectedImage = { filename: "test.png", width: 10, height: 10 };
  let syncCount = 0;
  app.commands.syncConfigToServer = async () => ({
    resolved_solve_grid: ++syncCount === 1 ? authoritativeGrid() : authoritativeGrid(),
  });
  app.commands.appConfirm = async () => true;
  app.commands.renderFrameCanvas = () => {};
  app.commands.renderPreview = () => {};
  app.commands.updateInfoGrid = () => {};
  const toasts = [];
  app.commands.showToast = (...args) => toasts.push(args);

  const result = await app.commands.syncSolveDimensionsWithGridRemediation();

  assert.equal(result.proceed, false);
  assert.equal(result.corrected, true);
  assert.equal(syncCount, 2);
  assert.equal(toasts.at(-1)[1], "error");
});

test("dimension remediation aborts before prompting on an incomplete server response", async () => {
  const { app } = await createFeatureHarness();
  app.state.image.selectedImage = { filename: "test.png", width: 10, height: 10 };
  app.commands.syncConfigToServer = async () => ({ config: {} });
  let prompts = 0;
  app.commands.appConfirm = async () => { prompts += 1; return true; };
  const toasts = [];
  app.commands.showToast = (...args) => toasts.push(args);

  const result = await app.commands.syncSolveDimensionsWithGridRemediation();

  assert.equal(result.proceed, false);
  assert.equal(prompts, 0);
  assert.equal(toasts.at(-1)[1], "error");
});

test("nozzle remediation says Continue when its new pitch requires size remediation", async () => {
  const pitchInput = fakeElement();
  pitchInput.value = "0.2";
  const { app } = await createFeatureHarness({ elements: { "#cfgSolvePitch": pitchInput } });
  app.state.image.selectedImage = { filename: "test.png", width: 10, height: 10 };
  app.state.image.frameState.widthMm = 100.2;
  app.state.image.frameState.heightMm = 100.2;
  app.state.session.activeNozzle = { size: 0.4 };
  const mismatch = app.commands.getSolvePitchNozzleMismatch();
  app.commands.getSolveSettingsPreflightIssues = () => [mismatch.message];
  let options = null;
  app.commands.appConfirm = async (_message, value) => { options = value; return false; };

  await app.commands.syncSolveSettingsWithPitchRemediation({ intent: "single" });

  assert.equal(options.ok, "Use 0.4 mm and Continue");
});

test("single solve runs blocking checks before exactly one dimension preflight", async () => {
  const events = [];
  const { app } = await createFeatureHarness({ api: {
    getExportStatus: async () => ({ status: "idle" }),
    apiPost: async () => {
      events.push("palette validation");
      return { valid: true };
    },
    startSolve: async () => {
      events.push("solve start");
      return { job_id: "solve-grid-test" };
    },
  } });
  app.state.session.apiConnected = true;
  app.state.image.selectedImage = { filename: "test.png", width: 10, height: 10 };
  app.commands.updateSolveReadiness = () => {};
  app.commands.syncSolveSettingsWithPitchRemediation = async () => {
    events.push("pitch remediation");
    return { proceed: true, corrected: true };
  };
  app.commands.getSolveSettingsPreflightIssues = () => {
    events.push("blocking settings");
    return [];
  };
  app.commands.getActivePalette = () => ["red"];
  app.commands.getPaletteGatingIssues = () => ({ missing: [], unavailable: [], disabled: [] });
  app.commands.paletteGatingIssueCount = () => 0;
  let dimensionCalls = 0;
  app.commands.syncSolveDimensionsWithGridRemediation = async () => {
    dimensionCalls += 1;
    events.push("dimension preflight");
    return { proceed: true, corrected: false };
  };
  app.commands.buildSolveRecipeContext = () => ({
    profile_ref: { kind: "system", id: "default" },
    profile_name_at_solve: "Default",
    is_profile_modified_at_solve: false,
    recipe_snapshot: {},
  });
  app.commands._currentSettingsSnapshot = () => ({});
  app.commands.createSolveRun = () => ({
    id: "run-grid-test",
    profile_ref: { kind: "system", id: "default" },
    profile_name_at_solve: "Default",
    is_profile_modified_at_solve: false,
    recipe_snapshot: {},
  });
  app.commands.resetOperationElapsedSeconds = () => {};
  app.commands.switchTab = () => {};
  app.commands.startSolvePolling = () => {};

  await app.commands.handleStartSolve();

  assert.equal(dimensionCalls, 1);
  assert.deepEqual(events, [
    "pitch remediation",
    "blocking settings",
    "palette validation",
    "dimension preflight",
    "solve start",
  ]);
});

test("blocking solve settings prevent the advisory dimension preflight", async () => {
  let dimensionCalls = 0;
  let paletteCalls = 0;
  const { app } = await createFeatureHarness({ api: {
    getExportStatus: async () => ({ status: "idle" }),
    apiPost: async () => { paletteCalls += 1; return { valid: true }; },
  } });
  app.state.session.apiConnected = true;
  app.state.image.selectedImage = { filename: "test.png", width: 10, height: 10 };
  app.commands.updateSolveReadiness = () => {};
  app.commands.syncSolveSettingsWithPitchRemediation = async () => ({ proceed: true });
  app.commands.getSolveSettingsPreflightIssues = () => ["Layer Height is invalid"];
  app.commands.syncSolveDimensionsWithGridRemediation = async () => {
    dimensionCalls += 1;
    return { proceed: true };
  };
  app.commands.showToast = () => {};

  await app.commands.handleStartSolve();

  assert.equal(paletteCalls, 0);
  assert.equal(dimensionCalls, 0);
});
