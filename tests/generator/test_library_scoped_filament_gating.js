const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const APP_JS = fs.readFileSync(
  path.resolve(__dirname, '../../Prisma/generator/app/app.js'),
  'utf8',
);

function extractFunction(name) {
  const signature = `function ${name}(`;
  const start = APP_JS.indexOf(signature);
  assert.notEqual(start, -1, `missing ${name}`);
  const parameterOpen = APP_JS.indexOf('(', start);
  let parameterDepth = 0;
  let open = -1;
  for (let index = parameterOpen; index < APP_JS.length; index += 1) {
    if (APP_JS[index] === '(') parameterDepth += 1;
    else if (APP_JS[index] === ')') {
      parameterDepth -= 1;
      if (parameterDepth === 0) {
        open = APP_JS.indexOf('{', index);
        break;
      }
    }
  }
  assert.notEqual(open, -1, `missing body for ${name}`);
  let depth = 0;
  let quote = null;
  let escaped = false;
  for (let index = open; index < APP_JS.length; index += 1) {
    const char = APP_JS[index];
    if (quote) {
      if (escaped) escaped = false;
      else if (char === '\\') escaped = true;
      else if (char === quote) quote = null;
      continue;
    }
    if (char === '"' || char === "'" || char === '`') {
      quote = char;
      continue;
    }
    if (char === '{') depth += 1;
    else if (char === '}') {
      depth -= 1;
      if (depth === 0) return APP_JS.slice(start, index + 1);
    }
  }
  throw new Error(`unterminated ${name}`);
}

const pureSource = [
  extractFunction('isGenerationEligibleFilament'),
  extractFunction('normalizeEnabledFilamentEntry'),
  extractFunction('reconcileEnabledFilamentIds'),
  'return { isGenerationEligibleFilament, reconcileEnabledFilamentIds };',
].join('\n');

// eslint-disable-next-line no-new-func
const pure = new Function(pureSource)();

test('generation eligibility rejects unprofiled and backend-excluded filaments', () => {
  assert.equal(pure.isGenerationEligibleFilament({ has_profile: true }), true);
  assert.equal(pure.isGenerationEligibleFilament({ has_profile: false }), false);
  assert.equal(pure.isGenerationEligibleFilament({ has_profile: true, exclude_from_model: true }), false);
  assert.equal(pure.isGenerationEligibleFilament({ has_profile: true, generation_available: false }), false);
});

test('a runtime library with no scoped record defaults every eligible filament on', () => {
  assert.deepEqual(
    pure.reconcileEnabledFilamentIds(['red', 'blue', 'white'], null),
    ['red', 'blue', 'white'],
  );
});

test('reconciliation retains known choices, defaults new IDs on, and prunes removed IDs', () => {
  const saved = {
    eligible_ids: ['red', 'blue', 'removed'],
    enabled_ids: ['red', 'removed'],
  };
  assert.deepEqual(
    pure.reconcileEnabledFilamentIds(['red', 'blue', 'new'], saved),
    ['red', 'new'],
  );
});

test('persistence is keyed to authoritative runtime identity, never next-launch selection', () => {
  const runtime = extractFunction('authoritativeRuntimeLibraryId');
  assert.match(runtime, /status\.runtime_active_library_id/);
  assert.doesNotMatch(runtime, /status\.active_library_id/);

  const save = extractFunction('saveEnabledFilaments');
  assert.match(save, /store\.libraries\[runtimeLibraryId\]/);
  assert.match(save, /eligible_ids:\s*eligibleIds/);
  assert.match(save, /enabled_ids:/);
});

test('legacy unscoped state is retired only after a successful scoped write', () => {
  const reconcile = extractFunction('reconcileEnabledFilamentsForRuntimeLibrary');
  assert.match(reconcile, /if \(saveEnabledFilaments\(\)\)[\s\S]*removeItem\(LEGACY_ENABLED_FILAMENTS_STORAGE_KEY\)/);
  assert.doesNotMatch(reconcile, /getItem\(LEGACY_ENABLED_FILAMENTS_STORAGE_KEY\)/);
});

test('offline and recovery paths cannot persist a scoped record', () => {
  const reconcile = extractFunction('reconcileEnabledFilamentsForRuntimeLibrary');
  assert.match(reconcile, /if \(!runtimeLibraryId\)[\s\S]*enabledFilamentPersistenceReady = false/);
  assert.match(reconcile, /persist:\s*false/);
  const load = extractFunction('loadFilaments');
  assert.match(load, /enabledFilamentPersistenceReady = false/);
  assert.match(load, /persist:\s*false/);
});

test('all enabled-set mutations flow through the synchronizing selection helper', () => {
  assert.doesNotMatch(APP_JS, /enabledFilaments\.(?:add|delete|clear)\(/);
  const selection = extractFunction('applyEnabledFilamentSelection');
  assert.match(selection, /candidateSelection = new Set/);
  assert.match(selection, /manualSlots = manualSlots\.filter/);
  assert.match(selection, /saveEnabledFilaments\(\)/);
  assert.match(selection, /refreshEnabledFilamentConsumers/);
});

test('saved palette loading and solve start both block gated filament choices', () => {
  const loadPalette = extractFunction('loadPaletteByIndex');
  assert.match(loadPalette, /getPaletteGatingIssues\(saved\.filament_ids\)/);
  assert.match(loadPalette, /if \(!allowUnavailable && paletteGatingIssueCount\(gatingIssues\)\)/);

  const solve = extractFunction('handleStartSolve');
  assert.match(solve, /getPaletteGatingIssues\(palette\)/);
  assert.match(solve, /buildPaletteGatingMessage\(gatingIssues, "Can't solve\."\)/);
});
