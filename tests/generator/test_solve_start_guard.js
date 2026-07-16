const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP_JS = path.resolve(__dirname, '../../Prisma/generator/app/app.js');
const SOURCE = fs.readFileSync(APP_JS, 'utf8');
const INDEX_HTML = fs.readFileSync(
  path.resolve(__dirname, '../../Prisma/generator/app/index.html'),
  'utf8',
);

function extractFunction(signature) {
  const start = SOURCE.indexOf(signature);
  if (start === -1) {
    throw new Error(`Could not find function signature: ${signature}`);
  }
  const closeParams = SOURCE.indexOf(')', start);
  if (closeParams === -1) {
    throw new Error(`Could not find closing paren for: ${signature}`);
  }
  const openBrace = SOURCE.indexOf('{', closeParams);
  if (openBrace === -1) {
    throw new Error(`Could not find opening brace for: ${signature}`);
  }
  let depth = 0;
  for (let i = openBrace; i < SOURCE.length; i += 1) {
    const ch = SOURCE[i];
    if (ch === '{') depth += 1;
    if (ch === '}') {
      depth -= 1;
      if (depth === 0) {
        return SOURCE.slice(start, i + 1);
      }
    }
  }
  throw new Error(`Could not find closing brace for: ${signature}`);
}

function loadSolveStartFunctions(updateConfigImpl, startSolveImpl) {
  const toasts = [];
  const context = {
    console,
    apiConnected: true,
    _configSyncChain: Promise.resolve(),
    config: {
      color_region_target_mm: 0.6,
      layer_height: 0.08,
      d_wb: 0.20,
      d_wc_min: 0.08,
      t_max: 3.00,
    },
    activeNozzle: null,
    syncConfigFromModuleState() {},
    readConfigFromUI() {},
    updateConfig: updateConfigImpl,
    selectedImage: null,
    frameState: {
      widthMm: 100,
      heightMm: 100,
      scale: 100,
      rotation: 0,
      panX: 0,
      panY: 0,
      flipH: false,
      flipV: false,
    },
    imageAdjust: {},
    getActivePalette() {
      return ['demo-filament'];
    },
    getPaletteGatingIssues() {
      return { missing: [], unavailable: [], disabled: [] };
    },
    paletteGatingIssueCount() {
      return 0;
    },
    buildPaletteGatingMessage() {
      return "Can't solve.";
    },
    getBaseFilament() {
      return 'demo-white';
    },
    printerConfig: { ams_slots: 4, white_slots: 1 },
    solveStatus: { status: 'idle' },
    solveStartPending: false,
    exportRunning: false,
    getExportStatus: async () => ({ status: 'idle' }),
    settingsDrawerOpen: false,
    closeSettingsDrawer() {},
    switchTab() {},
    buildSolveRecipeContext() {
      throw new Error('handleStartSolve should not build a run after config sync failure');
    },
    _currentSettingsSnapshot() {
      return {};
    },
    createSolveRun() {
      throw new Error('handleStartSolve should not create a run after config sync failure');
    },
    surfaceDataCache: {},
    explorerMaterialDataCache: {},
    solveRuns: [],
    selectedRunIds: new Set(),
    startSolve: startSolveImpl,
    renderSolveTab() {},
    updateSolveReadiness() {},
    resetSolveRunDeleteConfirm() {},
    resetOperationElapsedSeconds() {},
    startSolvePolling() {},
    showToast(message, type) {
      toasts.push({ message, type });
    },
  };

  const script = [
    extractFunction('function minCapLayersFromThickness'),
    extractFunction('function minCapThicknessFromLayers'),
    extractFunction('function _formatConfigSyncError'),
    extractFunction('function readSolvePreflightNumber'),
    extractFunction('function readSolvePreflightMinCapLayers'),
    extractFunction('function calculateStackLayerAlignment'),
    extractFunction('function buildStackLayerAlignmentIssue'),
    extractFunction('function buildSolvePitchNozzleIssue'),
    extractFunction('function getSolveSettingsPreflightIssues'),
    extractFunction('function buildSolveSettingsPreflightMessage'),
    extractFunction('async function syncConfigToServer'),
    extractFunction('async function handleStartSolve'),
  ].join('\n\n');
  vm.runInNewContext(script, context, { filename: APP_JS });
  return { context, toasts };
}

test('quick solve controls and doubled-pitch state are removed', () => {
  assert.equal(/data-solve-mode/.test(INDEX_HTML), false);
  assert.equal(/solveModeQuick|solveModeFull|solveModePixelLabel/.test(INDEX_HTML), false);
  assert.equal(/solveQuickMode|pixel_size_used|pixelSizeOverride|getSolvePitchForMode|updateSolveModeLabel/.test(SOURCE), false);
});

test('global solve control lives in the top bar; cancel lives with progress', () => {
  assert.equal((INDEX_HTML.match(/id="startSolveBtn"/g) || []).length, 1);
  assert.equal((INDEX_HTML.match(/id="cancelSolveBtn"/g) || []).length, 0);
  assert.equal((INDEX_HTML.match(/id="opProgressCancel"/g) || []).length, 1);
});

test('solve controls stay adjacent to workflow tabs, before utility actions', () => {
  const exportIdx = INDEX_HTML.indexOf('data-tab="export"');
  const solveIdx = INDEX_HTML.indexOf('id="startSolveBtn"');
  const spacerIdx = INDEX_HTML.indexOf('mode-utility-spacer');
  const clearTempIdx = INDEX_HTML.indexOf('id="clearAllTempBtn"');
  assert.ok(exportIdx >= 0 && solveIdx > exportIdx, 'Solve should follow the workflow steps');
  assert.ok(spacerIdx > solveIdx, 'the right-side utility spacer should come after Solve');
  assert.ok(clearTempIdx > spacerIdx, 'Clear All Temporary Files belongs with right-side utilities');
  assert.equal(INDEX_HTML.includes('id="helpBtn"'), false, 'in-app Help button should be removed');
});

test('handleStartSolve stops before solve start when config sync fails', async () => {
  let startSolveCalled = false;
  const { context, toasts } = loadSolveStartFunctions(
    async () => {
      throw new Error('API 500: config write failed');
    },
    async () => {
      startSolveCalled = true;
    },
  );

  await context.handleStartSolve();

  assert.equal(startSolveCalled, false);
  assert.equal(context.solveRuns.length, 0);
  assert.equal(toasts.length, 1);
  assert.equal(toasts[0].type, 'error');
  assert.match(toasts[0].message, /Couldn't sync settings to the server/i);
  assert.match(toasts[0].message, /config write failed/i);
  assert.equal(context.solveStartPending, false);
});

test('rapid Solve activation owns startup synchronously and performs one preflight', async () => {
  let resolveExportStatus;
  let statusChecks = 0;
  const { context, toasts } = loadSolveStartFunctions(async () => ({}), async () => ({}));
  context.getExportStatus = async () => {
    statusChecks += 1;
    return await new Promise((resolve) => { resolveExportStatus = resolve; });
  };

  const first = context.handleStartSolve();
  const duplicate = context.handleStartSolve();
  await duplicate;

  assert.equal(statusChecks, 1);
  assert.equal(context.solveStartPending, true);
  resolveExportStatus({ status: 'running' });
  await first;
  assert.equal(context.solveStartPending, false);
  assert.equal(toasts.length, 1);
  assert.match(toasts[0].message, /wait for meshing/i);
});

test('accepted job transfers readiness ownership from startup to running state', async () => {
  let pollingRun = null;
  const { context } = loadSolveStartFunctions(
    async (payload) => ({ config: { ...payload } }),
    async () => ({ job_id: 'job-accepted' }),
  );
  context.selectedImage = { filename: 'demo.png' };
  context.buildSolveRecipeContext = () => ({
    profile_ref: { kind: 'transient' },
    profile_name_at_solve: 'Default',
    is_profile_modified_at_solve: false,
    recipe_snapshot: {},
  });
  context._currentSettingsSnapshot = () => ({});
  context.createSolveRun = (_palette, snapshot, recipe) => ({
    id: 'accepted-run',
    profile_ref: recipe.profile_ref,
    profile_name_at_solve: recipe.profile_name_at_solve,
    is_profile_modified_at_solve: false,
    recipe_snapshot: recipe.recipe_snapshot,
    config: snapshot,
  });
  context.startSolvePolling = (run) => { pollingRun = run; };

  await context.handleStartSolve();

  assert.equal(context.solveStartPending, false);
  assert.equal(context.solveStatus.status, 'running');
  assert.equal(context.solveStatus.job_id, 'job-accepted');
  assert.equal(context.solveRuns.length, 1);
  assert.equal(pollingRun?.id, 'accepted-run');
});

test('handleStartSolve blocks before solve start when layer height is below the active nozzle minimum', async () => {
  let startSolveCalled = false;
  const { context, toasts } = loadSolveStartFunctions(
    async (payload) => ({ config: { ...payload } }),
    async () => {
      startSolveCalled = true;
    },
  );
  Object.assign(context.config, { layer_height: 0.04, d_wb: 0.20, d_wc_min: 0.08, t_max: 3.00 });
  context.activeNozzle = { size: 0.4, min_layer_height: 0.08, max_layer_height: 0.32 };

  await context.handleStartSolve();

  assert.equal(startSolveCalled, false);
  assert.equal(context.solveRuns.length, 0);
  assert.equal(toasts.length, 1);
  assert.equal(toasts[0].type, 'error');
  assert.match(toasts[0].message, /Can't solve\. Fix settings/i);
  assert.match(toasts[0].message, /below the 0\.4 mm nozzle minimum \(0\.08 mm\)/);
  assert.equal(context.solveStartPending, false);
});

test('handleStartSolve blocks when Solve Pitch is smaller than the active nozzle', async () => {
  let startSolveCalled = false;
  const { context, toasts } = loadSolveStartFunctions(
    async (payload) => ({ config: { ...payload } }),
    async () => { startSolveCalled = true; },
  );
  Object.assign(context.config, {
    layer_height: 0.08,
    d_wb: 0.20,
    d_wc_min: 0.08,
    t_max: 3.00,
    image_sample_pitch_mm: 0.20,
    solver_fine_pitch_mm: 0.20,
  });
  context.activeNozzle = { size: 0.4, min_layer_height: 0.08, max_layer_height: 0.32 };

  await context.handleStartSolve();

  assert.equal(startSolveCalled, false);
  assert.equal(context.solveRuns.length, 0);
  assert.equal(toasts.length, 1);
  assert.match(toasts[0].message, /Solve Pitch \(0\.2 mm\).*nozzle diameter \(0\.4 mm\)/);
  assert.match(toasts[0].message, /Increase Solve Pitch or choose a smaller nozzle/);
  assert.equal(context.solveStartPending, false);
});

test('handleStartSolve blocks before solve start when stack thickness is not layer-aligned', async () => {
  let startSolveCalled = false;
  const { context, toasts } = loadSolveStartFunctions(
    async (payload) => ({ config: { ...payload } }),
    async () => {
      startSolveCalled = true;
    },
  );
  Object.assign(context.config, { layer_height: 0.08, d_wb: 0.20, d_wc_min: 0.08, t_max: 2.51 });

  await context.handleStartSolve();

  assert.equal(startSolveCalled, false);
  assert.equal(context.solveRuns.length, 0);
  assert.equal(toasts.length, 1);
  assert.equal(toasts[0].type, 'error');
  assert.match(toasts[0].message, /Max Total Thickness cannot be allocated in whole Layer Height steps/);
  assert.match(toasts[0].message, /Set Max Total Thickness/);
  assert.doesNotMatch(toasts[0].message, /Thinnest cap|unused in color budget/i);
  assert.equal(context.solveStartPending, false);
});

test('Solve Pitch guidance describes the nozzle value as a minimum', () => {
  assert.match(INDEX_HTML, /id="cfgSolvePitchHint">minimum 0\.20 mm/);
  assert.doesNotMatch(INDEX_HTML, /smaller values preserve more detail/);
  assert.doesNotMatch(SOURCE, /Thinnest cap|unused in color budget/);
  assert.match(SOURCE, /solveStartPending = true;[\s\S]*?finally \{[\s\S]*?solveStartPending = false;/);
  assert.match(SOURCE, /btn\.disabled = !\(canSolve && !solveStartPending && !isRunning && !exportRunning\)/);
});

test('handleStartSolve waits behind older config syncs so solve start sees the latest config', async () => {
  const requests = [];
  const pending = [];
  const serverState = { color_region_target_mm: 0.6 };
  let startSolvePayload = null;

  const { context } = loadSolveStartFunctions(
    async (payload) => {
      requests.push(payload.color_region_target_mm);
      return await new Promise((resolve) => {
        pending.push({ payload, resolve });
      });
    },
    async (body) => {
      startSolvePayload = {
        recipeColorRegionTarget: body.recipeSnapshot.profile_snapshot.settings.color_region_target_mm,
        serverColorRegionTarget: serverState.color_region_target_mm,
      };
    },
  );
  context.selectedImage = { filename: 'demo.png' };
  context.buildSolveRecipeContext = () => ({
    profile_ref: { kind: 'transient', name: 'Default' },
    profile_name_at_solve: 'Default',
    isProfileModifiedAtSolve: true,
    is_profile_modified_at_solve: true,
    recipe_snapshot: {
      profile_snapshot: {
        settings: { color_region_target_mm: context.config.color_region_target_mm },
        modules: {},
      },
    },
  });
  context._currentSettingsSnapshot = () => ({
    color_region_target_mm: context.config.color_region_target_mm,
  });
  context.createSolveRun = (_palette, configSnapshot, recipeContext) => ({
    id: 'race-run',
    profile_ref: recipeContext.profile_ref,
    profile_name_at_solve: recipeContext.profile_name_at_solve,
    is_profile_modified_at_solve: true,
    recipe_snapshot: recipeContext.recipe_snapshot,
    config: { ...configSnapshot },
  });

  const staleSync = context.syncConfigToServer();
  context.config.color_region_target_mm = 10;
  const startPromise = context.handleStartSolve();
  await new Promise((resolve) => setTimeout(resolve, 0));

  assert.deepEqual(requests, [0.6]);

  serverState.color_region_target_mm = 0.6;
  pending.shift().resolve({ config: { color_region_target_mm: 0.6 } });
  await new Promise((resolve) => setTimeout(resolve, 0));

  assert.deepEqual(requests, [0.6, 10]);

  serverState.color_region_target_mm = 10;
  pending.shift().resolve({ config: { color_region_target_mm: 10 } });

  await Promise.all([staleSync, startPromise]);

  assert.deepEqual(requests, [0.6, 10]);
  assert.deepEqual(startSolvePayload, {
    recipeColorRegionTarget: 10,
    serverColorRegionTarget: 10,
  });
});
