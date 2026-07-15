const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP_JS = fs.readFileSync(
  path.resolve(__dirname, '../../Prisma/generator/app/app.js'),
  'utf8',
);
const CSS = fs.readFileSync(
  path.resolve(__dirname, '../../Prisma/generator/app/style.css'),
  'utf8',
);

function extractFunction(signature) {
  const start = APP_JS.indexOf(signature);
  assert.notEqual(start, -1, `${signature} should exist`);
  const open = APP_JS.indexOf('{', APP_JS.indexOf(')', start));
  let depth = 0;
  for (let i = open; i < APP_JS.length; i += 1) {
    if (APP_JS[i] === '{') depth += 1;
    if (APP_JS[i] === '}' && --depth === 0) return APP_JS.slice(start, i + 1);
  }
  throw new Error(`Could not extract ${signature}`);
}

test('solve history cards always expose Settings, including the displayed run', () => {
  assert.equal(
    APP_JS.includes('run.id === displayedRunId ? ""'),
    false,
    'Settings action must not be suppressed for the displayed/selected run',
  );
  assert.match(
    APP_JS,
    /<button class="solve-run-settings-btn"[\s\S]*?>Settings<\/button>/,
    'solve history cards should render a Settings action on the unified card path',
  );
});

test('solve history card actions and RMSE use the compact card layout', () => {
  assert.match(
    APP_JS,
    /<div class="solve-run-card-actions">[\s\S]*?solve-run-save-btn[\s\S]*?solve-run-delete-btn[\s\S]*?<\/div>/,
    'Save and delete should be clustered in the card header actions',
  );
  assert.match(
    APP_JS,
    /<div class="solve-run-card-meta">[\s\S]*?solve-run-settings-btn[\s\S]*?\$\{stats\}[\s\S]*?<\/div>/,
    'Settings and RMSE should share the bottom metadata row',
  );
  assert.match(
    APP_JS,
    /function formatSolveRunCardRmse\(result, digits = 3\)[\s\S]*?return `RMSE \$\{\(rmse \* 100\)\.toFixed\(digits\)\}%`;/,
    'card RMSE should render as RMSE #.###%',
  );
  assert.match(CSS, /\.solve-run-card-actions \{[\s\S]*?margin-left: auto;[\s\S]*?gap: 4px;/);
  assert.match(CSS, /\.solve-run-card-meta \{[\s\S]*?justify-content: space-between;/);
  assert.match(CSS, /\.solve-run-card-rmse \{[\s\S]*?margin-left: auto;/);
});

test('solve history card Save prompts for a run name before saving', () => {
  const saveHandlerStart = APP_JS.indexOf('container.querySelectorAll(".solve-run-save-btn")');
  assert.notEqual(saveHandlerStart, -1, 'solve history Save handler should exist');
  const saveHandlerEnd = APP_JS.indexOf('bindSolveRunCardAuxiliaryInteractions(container, "preview")', saveHandlerStart);
  assert.notEqual(saveHandlerEnd, -1, 'solve history Save handler should be bounded by shared card bindings');
  const saveHandler = APP_JS.slice(saveHandlerStart, saveHandlerEnd);
  assert.match(saveHandler, /appPrompt\([\s\S]*Save this run as:/, 'Save should ask for a user-facing run name');
  assert.match(saveHandler, /\{ title: "Save Run", validate:/, 'Save prompt should have a clear dialog title');
  assert.match(
    saveHandler,
    /validate: value => String\(value \|\| ""\)\.trim\(\) \? "" : "Run name cannot be empty\."/,
    'blank labels should remain in the prompt with an actionable validation message',
  );
  assert.match(saveHandler, /const trimmed = label\.trim\(\);/, 'accepted labels should be normalized before save');
  assert.match(saveHandler, /saveRun\(btn\.dataset\.runId, trimmed\)/, 'Save should pass the prompted name, not null');
  assert.doesNotMatch(saveHandler, /saveRun\(btn\.dataset\.runId, null\)/, 'Save should not silently mint a generated label');
});

test('export run cards expose delete without rendering Save or hijacking selection', () => {
  const exportStart = APP_JS.indexOf('function renderExportRunSidebar()');
  assert.notEqual(exportStart, -1, 'export run sidebar renderer should exist');
  const exportBody = APP_JS.slice(exportStart, APP_JS.indexOf('function renderExportTab()', exportStart));
  assert.match(
    exportBody,
    /<div class="solve-run-card-actions">[\s\S]*?buildSolveRunDeleteButton\(run\)[\s\S]*?<\/div>/,
    'export run cards should render delete actions',
  );
  assert.doesNotMatch(exportBody, /solve-run-save-btn/, 'export run cards should not render Save');
  assert.match(exportBody, /solve-run-settings-btn[\s\S]*?>Settings<\/button>/, 'export cards should expose run settings');
  assert.match(exportBody, /if \(e\.target\.closest\("\.solve-run-delete-btn"\)\) return;/);
  assert.match(exportBody, /if \(e\.target\.closest\("\.solve-run-settings-btn"\)\) return;/);
  assert.match(exportBody, /\$\{buildSolveRunDeleteButton\(run\)\}/);
  assert.match(exportBody, /handleSolveRunDeleteClick\(btn\.dataset\.runId\);/);
});

test('active pending solve card cannot be removed from history', () => {
  assert.match(
    APP_JS,
    /function isActivePendingSolveRun\(run\)[\s\S]*?!run\.results[\s\S]*?run\.id === activeSolveRunId[\s\S]*?solveStatus\.status === "running"/,
    'active pending solve cards should be detected from run id and global running status',
  );
  assert.match(
    APP_JS,
    /function deleteSolveRun\(runId, \{ force = false \} = \{\}\)[\s\S]*?!force && isActivePendingSolveRun\(run\)[\s\S]*?return false;/,
    'normal delete should refuse to remove the active pending solve card',
  );
  assert.match(
    APP_JS,
    /function getSolveRunDeleteBlockReason\(run\)[\s\S]*?isActivePendingSolveRun\(run\)[\s\S]*?Cancel this solve before removing its card/,
    'active pending solve cards should provide a shared disabled reason',
  );
  assert.match(APP_JS, /buildSolveRunDeleteButton\(run\)[\s\S]*?disabled aria-disabled=/);
  assert.match(
    APP_JS,
    /deleteSolveRun\(runId, \{ force: true \}\);/,
    'cancel cleanup should still be able to remove its own pending run card',
  );
  assert.match(CSS, /\.solve-run-delete-btn:disabled \{[\s\S]*?cursor: not-allowed;/);
});

test('Preview and Export share one three-second solve-run delete confirmation', () => {
  const deleted = [];
  const timers = [];
  let renders = 0;
  const context = {
    solveRuns: [{ id: 'run-a', results: {} }, { id: 'run-b', results: {} }],
    solveRunDeleteArmedId: null,
    solveRunDeleteConfirmTimer: null,
    exportRunning: false,
    activeExportRunId: null,
    isActivePendingSolveRun: () => false,
    renderSolveRunDeleteState: () => { renders += 1; },
    deleteSolveRun: (runId) => { deleted.push(runId); return true; },
    setTimeout: (callback, ms) => {
      const timer = { callback, ms };
      timers.push(timer);
      return timer;
    },
    clearTimeout: () => {},
  };
  vm.runInNewContext([
    extractFunction('function getSolveRunDeleteBlockReason'),
    extractFunction('function resetSolveRunDeleteConfirm'),
    extractFunction('function armSolveRunDeleteConfirm'),
    extractFunction('function handleSolveRunDeleteClick'),
  ].join('\n\n'), context);

  assert.equal(context.handleSolveRunDeleteClick('run-a'), false);
  assert.equal(context.solveRunDeleteArmedId, 'run-a');
  assert.equal(timers.at(-1).ms, 3000);
  assert.deepEqual(deleted, []);

  assert.equal(context.handleSolveRunDeleteClick('run-a'), true);
  assert.equal(context.solveRunDeleteArmedId, null);
  assert.deepEqual(deleted, ['run-a']);

  context.handleSolveRunDeleteClick('run-a');
  context.handleSolveRunDeleteClick('run-b');
  assert.equal(context.solveRunDeleteArmedId, 'run-b', 'arming another run should replace the prior owner');
  assert.ok(renders >= 3);

  assert.match(APP_JS, /function renderSolveRunDeleteState\(\)[\s\S]*?renderSolveRunSidebar\(\)[\s\S]*?currentTab === "export"[\s\S]*?renderExportRunSidebar\(\)/);
  assert.match(APP_JS, /function buildSolveRunDeleteButton\(run\)[\s\S]*?solveRunDeleteArmedId === run\.id[\s\S]*?Confirm\?/);
  assert.match(APP_JS, /function deleteSolveRun[\s\S]*?resetSolveRunDeleteConfirm\(\{ render: false \}\)/);
  assert.match(APP_JS, /function clearSolveHistory[\s\S]*?resetSolveRunDeleteConfirm\(\{ render: false \}\)/);
  assert.match(CSS, /\.solve-run-delete-btn \{[\s\S]*?width: 48px;/, 'delete action should reserve its confirmation footprint');
  assert.match(CSS, /\.solve-run-delete-btn\.confirm-pending \{[\s\S]*?font-weight: 700;/);
});

test('run sidebars recreate their empty states after the last card is removed', () => {
  const preview = extractFunction('function renderSolveRunSidebar');
  const exportSidebar = extractFunction('function renderExportRunSidebar');
  assert.match(preview, /solveRuns\.length === 0[\s\S]*?id="solveRunEmpty">No solves yet/);
  assert.match(exportSidebar, /!completed\.length[\s\S]*?id="exportRunEmpty">No completed solves yet/);
  assert.doesNotMatch(preview, /appendChild\(emptyMsg\)/);
  assert.doesNotMatch(exportSidebar, /appendChild\(emptyMsg\)/);
});

test('solve details use one ordered run summary block', () => {
  assert.equal(
    APP_JS.includes('solve-run-profile-row'),
    false,
    'profile name/status should not render in solve history cards',
  );
  assert.equal(
    CSS.includes('.solve-run-profile-row'),
    false,
    'old profile-row card styling should not remain',
  );
  assert.equal(APP_JS.includes('function buildSolveRunProfileInspectorBlock'), false);
  assert.equal(APP_JS.includes('Settings Profile", ['), false);
  assert.match(APP_JS, /function getSolveRunSummaryItems\(run\)[\s\S]*Profile[\s\S]*getSolveRunEssentialsItems\(run\)/);
  assert.match(
    APP_JS,
    /function buildSolveRunInspectorBlock\(run\)[\s\S]*buildSolveInspectorBlock\([\s\S]*run\.label[\s\S]*getSolveRunSummaryItems\(run\)/,
    'run details should render profile and essentials in the main run block',
  );
});

test('preview metadata is attached to each result card footer', () => {
  assert.match(
    APP_JS,
    /function buildSolveRunCardMetadataFooter\(run\)[\s\S]*const items = getSolveRunSummaryItems\(run\)/,
    'preview card footer should combine profile metadata and run essentials',
  );
  const footerStart = APP_JS.indexOf('function buildSolveRunCardMetadataFooter(run)');
  const footerEnd = APP_JS.indexOf('// ── Settings-diff machinery', footerStart);
  const footerSource = APP_JS.slice(footerStart, footerEnd);
  assert.equal(footerSource.includes('State'), false, 'preview card footer should not render profile state');
  assert.match(
    APP_JS,
    /function buildSolveRunVisualColumn\(run,[\s\S]*?\$\{buildSolveRunCardMetadataFooter\(run\)\}/,
    'normal result cards should include run metadata in the card footer',
  );
  assert.match(
    APP_JS,
    /function buildSolveRecipeColumn\(run,[\s\S]*?\$\{buildSolveRunCardMetadataFooter\(run\)\}/,
    'recipe result cards should include the same run metadata footer',
  );
  assert.match(
    CSS,
    /\.solve-card-run-meta \{[\s\S]*?border-top: 1px solid var\(--line\);/,
    'run metadata should be styled as card footer chrome',
  );
  assert.equal(
    APP_JS.includes('solve-card-meta-note'),
    false,
    'preview card footer should not render implementation-source profile notes',
  );
});

test('solve history card color is quiet by default and selected state owns the blue accent', () => {
  assert.match(CSS, /\.solve-run-card \{[\s\S]*background: var\(--panel, #fff\);/);
  assert.match(CSS, /\.solve-run-card\.is-selected \{[\s\S]*box-shadow: inset 3px 0 0 var\(--accent\);/);
});
