const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const APP = path.resolve(__dirname, '../../Prisma/generator/app');
const JS = fs.readFileSync(path.join(APP, 'app.js'), 'utf8');
const API = fs.readFileSync(path.join(APP, 'api.js'), 'utf8');
const HTML = fs.readFileSync(path.join(APP, 'index.html'), 'utf8');
const CSS = fs.readFileSync(path.join(APP, 'style.css'), 'utf8');

function extractFunctionSource(name) {
  const start = JS.indexOf(`function ${name}`);
  assert.notEqual(start, -1, `app.js must define ${name}`);
  const paramsStart = JS.indexOf('(', start);
  assert.notEqual(paramsStart, -1, `${name} must have a parameter list`);
  let parenDepth = 0;
  let paramsEnd = -1;
  for (let i = paramsStart; i < JS.length; i++) {
    const ch = JS[i];
    if (ch === '(') parenDepth += 1;
    if (ch === ')') {
      parenDepth -= 1;
      if (parenDepth === 0) {
        paramsEnd = i;
        break;
      }
    }
  }
  assert.notEqual(paramsEnd, -1, `${name} must close its parameter list`);
  const bodyStart = JS.indexOf('{', paramsEnd);
  assert.notEqual(bodyStart, -1, `${name} must have a function body`);
  let depth = 0;
  for (let i = bodyStart; i < JS.length; i++) {
    const ch = JS[i];
    if (ch === '{') depth += 1;
    if (ch === '}') {
      depth -= 1;
      if (depth === 0) return JS.slice(start, i + 1);
    }
  }
  throw new Error(`Could not extract ${name}`);
}

function loadPaletteRestoreHelpers() {
  const names = [
    'normalizeSupportFromLoadedConfig',
    'normalizeSupportFromPaletteRecord',
    'makePaletteSignature',
    'paletteSignaturesEqual',
    'signatureForPaletteRecord',
    'findMatchingDeckCard',
    'findMatchingSavedPaletteIndex',
    'chooseLoadedPaletteRestoreAction',
  ];
  const source = [
    'const DEFAULT_BASE_FILAMENT = "default-white";',
    ...names.map(extractFunctionSource),
    'return { normalizeSupportFromLoadedConfig, normalizeSupportFromPaletteRecord, makePaletteSignature, paletteSignaturesEqual, chooseLoadedPaletteRestoreAction };',
  ].join('\n');
  return Function(source)();
}

function loadDropdownHelpers() {
  const source = [
    'let allFilaments = [];',
    'let config = {};',
    'const DEFAULT_BASE_FILAMENT = "default-white";',
    'function $(sel) { return sel === "#cfgBaseFilament" ? baseEl : null; }',
    'function esc(value) { return String(value ?? "").replace(/[&<>"\']/g, ""); }',
    'function Option(label, value) { return { text: String(label), value: String(value) }; }',
    extractFunctionSource('isWhiteCapEligibleFilament'),
    extractFunctionSource('filamentSelectLabel'),
    extractFunctionSource('populateBaseCapDropdowns'),
    'return { run({ filaments, cfg, baseValue = "" }) { allFilaments = filaments; config = { ...cfg }; baseEl = { value: baseValue, innerHTML: "" }; populateBaseCapDropdowns(); const baseOptions = [...baseEl.innerHTML.matchAll(/value="([^"]+)"/g)].map(match => match[1]); return { config, baseValue: baseEl.value, baseOptions, baseHtml: baseEl.innerHTML }; } };',
  ].join('\n');
  return Function('let baseEl;\n' + source)();
}

test('Saved Runs button + modal exist', () => {
  assert.ok(/savedRunsBtn/.test(HTML), 'index.html needs #savedRunsBtn');
  assert.ok(/savedRunsModal/.test(HTML), 'index.html needs #savedRunsModal');
  for (const id of [
    'savedRunLoadBtn',
    'savedRunDownloadBtn',
    'savedRunSaveBtn',
    'savedRunRenameBtn',
    'savedRunDeleteBtn',
  ]) {
    assert.ok(HTML.includes(`id="${id}"`), `saved-runs footer needs #${id}`);
  }
});
test('solve history header mirrors deck Load/Clear action order', () => {
  assert.ok(
    /id="savedRunsBtn"[\s\S]*?>Load<\/button>[\s\S]*?id="clearSolveHistoryBtn"[\s\S]*?>Clear<\/button>/.test(HTML),
    'solve history header should show Load on the left and Clear on the right',
  );
  assert.ok(
    /id="exportSavedRunsBtn"[\s\S]*?>Load<\/button>[\s\S]*?id="exportClearSolveHistoryBtn"[\s\S]*?>Clear<\/button>/.test(HTML),
    'export run header should expose the same Load/Clear actions',
  );
  assert.equal(
    /id="savedRunsBtn"[\s\S]*?>Saved Runs<\/button>/.test(HTML),
    false,
    'saved-runs trigger should use the concise Load label',
  );
  assert.match(
    JS,
    /\["savedRunsBtn", "exportSavedRunsBtn"\]\.forEach/,
    'Solve and Export Load buttons should share the saved-runs modal handler',
  );
});
test('api client posts to /runs/save and /runs/load', () => {
  assert.ok(/\/runs\/save/.test(API), 'api.js must call /runs/save');
  assert.ok(/\/runs\/load/.test(API), 'api.js must call /runs/load');
  assert.ok(/\/runs\/settings/.test(API), 'api.js must expose settings-only archive loading');
});
test('api client supports auto-run promote and tiered load', () => {
  assert.ok(/function loadSavedRun[\s\S]*tier/.test(API) || /async function loadSavedRun[\s\S]*tier/.test(API),
    'loadSavedRun must accept/send a tier');
  assert.ok(/\/runs\/auto\/.*\/promote/.test(API),
    'api.js must expose promote auto endpoint');
  assert.ok(/deleteAutoRun[\s\S]*\/runs\/auto\/.*DELETE/.test(API),
    'api.js must expose auto-run delete endpoint');
});
test('promoting an autosave selects the newly minted saved-run id', () => {
  const body = extractFunctionSource('promoteSelectedSavedRun');
  assert.match(body, /const promoted = await promoteAutoRun\(selected\.save_id\)/,
    'promotion must retain the returned saved-run sidecar');
  assert.match(body, /selectedSavedRunKey = savedRunKey\(promoted\)/,
    'selection must use the returned saved-run id rather than the retired autosave id');
  assert.equal(body.includes('selectedSavedRunKey = `saved:'), false,
    'the old autosave id must not be reused as a saved-run selection key');
});
test('promotion response keeps its collision-suffixed row selected after refresh', async () => {
  const harness = Function(`
    let selectedSavedRunKey = null;
    function getSelectedSavedRun() { return { tier: "auto", save_id: "old-auto-id" }; }
    async function promoteAutoRun() { return { tier: "saved", save_id: "new-saved-id-2" }; }
    async function refreshSavedRunRows() {}
    function showToast() {}
    ${extractFunctionSource('savedRunKey')}
    ${extractFunctionSource('promoteSelectedSavedRun').replace(/^function /, 'async function ')}
    return async function run() {
      await promoteSelectedSavedRun();
      return selectedSavedRunKey;
    };
  `)();

  assert.equal(await harness(), 'saved:new-saved-id-2');
});
test('Saved Runs modal is a single-select list with footer actions by tier', () => {
  const body = extractFunctionSource('refreshSavedRunRows');
  assert.ok(/selectedSavedRunKey/.test(body), 'rows should update a single selected saved-run key');
  assert.ok(/role",\s*"option"/.test(body), 'rows should be listbox options');
  assert.ok(/dblclick/.test(body), 'double-clicking a row should load it');
  assert.equal(/saved-run-actions/.test(body), false, 'rows should not render per-row action button clusters');
  assert.ok(/formatSavedRunTimestamp/.test(body), 'rows should display formatted timestamps');
  const footer = extractFunctionSource('updateSavedRunFooterActions');
  assert.ok(/saveBtn\.hidden\s*=\s*!hasSelection \|\| tier !== "auto"/.test(footer), 'Save should only show for selected autosaves');
  assert.ok(/renameBtn\.hidden\s*=\s*!hasSelection \|\| tier !== "saved"/.test(footer), 'Rename should only show for selected persisted saves');
  assert.ok(/deleteBtn\.hidden\s*=\s*!hasSelection/.test(footer), 'Delete should show for any selected run');
  assert.ok(/loadBtn\.disabled\s*=\s*!hasSelection/.test(footer), 'Load should require a selected row');
  assert.ok(/downloadBtn\.disabled\s*=\s*!hasSelection/.test(footer), 'Download should require a selected row');
});
test('Saved Runs rows use archive-owned preview URLs and loaded cards retain labels', () => {
  assert.match(API, /function savedRunPreviewUrl\(save\)/);
  assert.match(API, /runs\/\$\{tier\}\/\$\{encodeURIComponent\(save\.save_id\)\}\/preview/);
  assert.match(JS, /class="saved-run-preview"/);
  assert.match(JS, /loaded_from_archive/);
  assert.match(JS, /solve-run-loaded-badge/);
  assert.match(CSS, /\.saved-run-preview:not\(\.is-unavailable\) img \+ \.saved-run-preview-placeholder/,
    'failed archive previews must retain their truthful placeholder');
});
test('Saved Runs exposes a distinct settings-only action', () => {
  assert.match(HTML, /id="savedRunLoadSettingsBtn"/);
  assert.match(JS, /savedRunLoadSettingsBtn/);
  assert.match(JS, /loadSettingsFromSavedRun/);
  assert.match(JS, /savedRunsModalMode === "settings"/);
  assert.match(HTML, /id="savedRunUploadLabel"/);
  assert.match(JS, /uploadLabel\.hidden = savedRunsModalMode === "settings"/,
    'whole-run archive upload must be hidden in settings-only mode');
});
test('Saved Runs footer keeps contextual actions left of static actions', () => {
  assert.ok(/saved-runs-context-actions[\s\S]*savedRunSaveBtn[\s\S]*savedRunRenameBtn[\s\S]*savedRunDeleteBtn/.test(HTML),
    'contextual saved-run actions should be grouped together');
  assert.ok(/saved-runs-static-actions[\s\S]*savedRunDownloadBtn[\s\S]*savedRunLoadBtn/.test(HTML),
    'static saved-run actions should stay grouped at the far right');
  assert.ok(/saved-runs-context-actions[\s\S]*saved-runs-static-actions/.test(HTML),
    'contextual actions should appear before the static action group');
});
test('Saved Runs timestamps are formatted for display', () => {
  const body = extractFunctionSource('formatSavedRunTimestamp');
  assert.ok(/compact/.test(body), 'timestamp formatter should handle compact archive timestamps');
  assert.ok(/\$\{compact\[1\]\}-\$\{compact\[2\]\}-\$\{compact\[3\]\}/.test(body),
    'compact timestamps should render as YYYY-MM-DD HH:MM');
});
test('Saved Runs explicit delete keeps the two-click confirm timer', () => {
  const body = extractFunctionSource('deleteSelectedSavedRun');
  assert.ok(/savedRunDeleteConfirmPending/.test(body), 'saved delete must still arm a confirm-pending state');
  assert.ok(/setTimeout/.test(body), 'saved delete must still revert with a timer');
  assert.ok(/Confirm\?/.test(body), 'saved delete must still show Confirm? on first click');
  assert.ok(/deleteSavedRun/.test(body), 'saved delete must still call deleteSavedRun on second click');
  assert.ok(/deleteAutoRun/.test(body), 'saved delete must also delete selected autosaves');
});
test('run_logging user-facing toggle is removed', () => {
  assert.equal(/cfgRunLogging/.test(HTML), false, 'index.html must not expose #cfgRunLogging');
  assert.equal(/cfgRunLogging/.test(JS), false, 'app.js must not read/write #cfgRunLogging');
  assert.equal(/run_logging/.test(JS), false, 'app.js must not send run_logging');
});
test('app wires save + load handlers', () => {
  assert.ok(/applyLoadedRun/.test(JS), 'app.js must define applyLoadedRun');
  assert.ok(/openSavedRunsModal/.test(JS), 'app.js must define openSavedRunsModal');
});
test('applyLoadedRun restores live frame + image-adjust globals on load', () => {
  // The solve payload is built from frameState / imageAdjust, not from `config`,
  // so applyLoadedRun must restore both or a re-solve uses stale crop/adjustments.
  assert.ok(/applyLoadedRun[\s\S]*?frameState/.test(JS),
    'applyLoadedRun must restore frameState');
  assert.ok(/applyLoadedRun[\s\S]*?imageAdjust/.test(JS),
    'applyLoadedRun must restore imageAdjust');
  assert.ok(/applyLoadedRun[\s\S]*?cfg\.image_adjust/.test(JS),
    'applyLoadedRun must read cfg.image_adjust');
});
test('Saved Runs footer exposes Rename for persisted saves', () => {
  assert.ok(/savedRunRenameBtn/.test(HTML), 'saved-runs footer needs a Rename action');
  assert.ok(/openRenameSavedRunDialog\(selected\)/.test(JS), 'footer Rename should open the in-app rename dialog for the selected save');
});
test('Rename uses an in-app dialog, not browser prompt()', () => {
  // Rename must drive the in-app rename dialog (two labeled fields) rather than
  // the native prompt(), so wording/sizing is under our control.
  assert.ok(/renameSavedRunModal/.test(HTML), 'index.html needs #renameSavedRunModal dialog');
  assert.ok(/renameSavedRunDisplay/.test(HTML), 'rename dialog needs an editable Display name field');
  assert.ok(/renameSavedRunDiskName/.test(HTML), 'rename dialog needs a read-only On-disk name field');
  assert.ok(/openRenameSavedRunDialog/.test(JS), 'app.js must open the in-app rename dialog');
  assert.ok(!/\bprompt\(/.test(JS), 'app.js must not call browser prompt()');
});
test('Clear All Temp confirm is an in-app dialog, not browser confirm()', () => {
  // The clear-all handler must route through appConfirm (in-app modal) instead of
  // the native confirm(); no confirm( should remain anywhere in app.js.
  assert.ok(/clearAllTempBtn[\s\S]*?appConfirm\(/.test(JS),
    'clear-all handler must use appConfirm');
  assert.ok(!/\bconfirm\(/.test(JS), 'app.js must not call browser confirm()');
});
test('Saved-run Delete uses a two-click armed confirm pattern', () => {
  // Delete arms on first click (confirm-pending state, "Confirm?" label) and
  // deletes on a second click within the timeout — mirroring .rail-deck-remove.
  assert.ok(/saved-run-delete/.test(HTML), 'delete button needs the saved-run-delete class');
  assert.ok(/savedRunDeleteConfirmPending/.test(JS), 'delete must track an armed/confirm-pending state');
  assert.ok(/Confirm\?/.test(JS), 'delete must show a "Confirm?" armed label');
});
test('Clear 409 toast wording is the reworded, actionable copy', () => {
  assert.ok(/wait for it to finish before clearing/.test(JS),
    'both clear 409 paths must use the reworded toast');
});
test('loaded palette planner is order-sensitive and honors collision precedence', () => {
  const helpers = loadPaletteRestoreHelpers();
  const support = helpers.normalizeSupportFromLoadedConfig({
    base_filament: 'white-base',
    white_cap: null,
  });
  assert.equal(support.base, 'white-base');
  assert.equal(support.capEffective, 'white-base');
  assert.equal(support.capSelector, '__same__');
  assert.equal(support.white_cap, null);

  const sameCap = helpers.normalizeSupportFromLoadedConfig({
    white_base: 'wb',
    cap_filament: '__same__',
  });
  assert.deepEqual(
    { base: sameCap.base, capEffective: sameCap.capEffective, capSelector: sameCap.capSelector, white_cap: sameCap.white_cap },
    { base: 'wb', capEffective: 'wb', capSelector: '__same__', white_cap: null },
  );

  assert.ok(helpers.paletteSignaturesEqual(
    helpers.makePaletteSignature(['a', 'b'], support),
    helpers.makePaletteSignature(['a', 'b'], support),
  ));
  assert.equal(helpers.paletteSignaturesEqual(
    helpers.makePaletteSignature(['a', 'b'], support),
    helpers.makePaletteSignature(['b', 'a'], support),
  ), false);
  assert.equal(helpers.paletteSignaturesEqual(
    helpers.makePaletteSignature(['a', 'a', 'b'], support),
    helpers.makePaletteSignature(['a', 'b', 'a'], support),
  ), false);

  assert.deepEqual(
    helpers.chooseLoadedPaletteRestoreAction({
      filamentIds: ['a', 'b'],
      support,
      deckCards: [{ id: 'deck-match', filament_ids: ['a', 'b'] }],
      savedPalettes: [{ filament_ids: ['a', 'b'] }],
    }),
    { kind: 'reuse-deck', cardId: 'deck-match' },
  );
  assert.deepEqual(
    helpers.chooseLoadedPaletteRestoreAction({
      filamentIds: ['a', 'b'],
      support,
      deckCards: [],
      savedPalettes: [{ filament_ids: ['x'] }, { filament_ids: ['a', 'b'] }],
    }),
    { kind: 'load-saved', savedIndex: 1 },
  );
  assert.deepEqual(
    helpers.chooseLoadedPaletteRestoreAction({
      filamentIds: ['unknown-a', 'b'],
      support,
      deckCards: [],
      savedPalettes: [{ filament_ids: ['b', 'unknown-a'] }],
    }),
    { kind: 'add-ad-hoc', filamentIds: ['unknown-a', 'b'] },
  );
  assert.deepEqual(
    helpers.chooseLoadedPaletteRestoreAction({
      filamentIds: [],
      support,
      deckCards: [{ id: 'empty-would-be-wrong', filament_ids: [] }],
      savedPalettes: [],
    }),
    { kind: 'none' },
  );
  assert.deepEqual(
    helpers.chooseLoadedPaletteRestoreAction({
      filamentIds: ['a', 'b'],
      support,
      deckCards: [],
      savedPalettes: [{ filament_ids: ['a', 'b'], base_filament: 'other-white', cap_filament: '__same__' }],
    }),
    { kind: 'add-ad-hoc', filamentIds: ['a', 'b'] },
  );
});
test('white base/cap dropdown uses eligible white filaments and forces shared cap', () => {
  const helpers = loadDropdownHelpers();
  const result = helpers.run({
    filaments: [
      { filament_id: 'known-white', has_profile: true, white_cap_eligible: true, manufacturer: 'Known', color_name: 'White' },
      { filament_id: 'not-white', has_profile: true, white_cap_eligible: false, manufacturer: 'Known', color_name: 'Red' },
    ],
    cfg: { base_filament: 'known-white', cap_filament: 'old-separate-cap' },
  });
  assert.equal(result.baseValue, 'known-white');
  assert.deepEqual(result.baseOptions, ['known-white']);
  assert.equal(result.config.cap_filament, '__same__');

  const staleDom = helpers.run({
    filaments: [{ filament_id: 'old-white', has_profile: true, white_cap_eligible: true, manufacturer: 'Old', color_name: 'White' }],
    cfg: { base_filament: 'loaded-missing-base', cap_filament: 'loaded-missing-cap' },
    baseValue: 'old-white',
  });
  assert.equal(staleDom.baseValue, 'old-white',
    'missing loaded support ids are replaced by an eligible white filament');
  assert.equal(staleDom.config.cap_filament, '__same__');

  const noWhites = helpers.run({
    filaments: [{ filament_id: 'red', has_profile: true, white_cap_eligible: false, manufacturer: 'Known', color_name: 'Red' }],
    cfg: { base_filament: 'known-white', cap_filament: '__same__' },
  });
  assert.equal(noWhites.baseValue, '');
  assert.equal(noWhites.config.base_filament, '');
  assert.equal(noWhites.config.cap_filament, '__same__');
});
test('applyLoadedRun restores palette after support render without eager sync or saved-library writes', () => {
  const applySource = extractFunctionSource('applyLoadedRun');
  assert.ok(/normalizeSupportFromLoadedConfig/.test(applySource), 'applyLoadedRun must normalize loaded support');
  assert.ok(/selectLoadedPalette/.test(applySource), 'applyLoadedRun must pick the first non-empty loaded palette source');
  assert.ok(/renderSettingsTab\(\)[\s\S]*await restoreLoadedRunPaletteToDeck/.test(applySource),
    'applyLoadedRun must render settings/support controls before restoring the deck');
  assert.equal(/syncConfigToServer\(/.test(applySource), false,
    'applyLoadedRun must not eagerly sync config during load');
  const restoreSource = extractFunctionSource('restoreLoadedRunPaletteToDeck');
  assert.equal(/savePalettesToServer/.test(restoreSource), false,
    'load-restore helper must not write to the saved palette library');
  assert.ok(/chooseLoadedPaletteRestoreAction/.test(restoreSource),
    'load-restore helper must execute the pure planner action');
  assert.ok(/loadPaletteByIndex\([^)]*forceActive:\s*true[^)]*sync:\s*false[^)]*silent:\s*true/s.test(restoreSource),
    'saved-library branch must load via loadPaletteByIndex without sync/toast and force activation');
  assert.ok(/existing\.gamut\s*=\s*null/.test(restoreSource),
    'reused deck cards must clear stale gamut metrics');
});
test('manual saved palette loads activate the loaded deck card by default', () => {
  const loadSource = extractFunctionSource('loadPaletteByIndex');
  assert.ok(/forceActive\s*=\s*true/.test(loadSource),
    'clicking a saved palette should make it the active solve palette even when a deck already exists');
});
