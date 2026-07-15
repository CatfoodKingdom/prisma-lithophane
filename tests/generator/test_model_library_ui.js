const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const APP = path.resolve(__dirname, '../../Prisma/generator/app');
const HTML = fs.readFileSync(path.join(APP, 'index.html'), 'utf8');
const CSS = fs.readFileSync(path.join(APP, 'style.css'), 'utf8');
const API = fs.readFileSync(path.join(APP, 'api.js'), 'utf8');
const JS = fs.readFileSync(path.join(APP, 'app.js'), 'utf8');

function functionBody(signature, nextSignature) {
  const start = JS.indexOf(signature);
  assert.notEqual(start, -1, `missing ${signature}`);
  const end = nextSignature ? JS.indexOf(nextSignature, start) : JS.length;
  assert.ok(end > start, `missing boundary after ${signature}`);
  return JS.slice(start, end);
}

test('model-library manager exposes the complete non-programmer workflow', () => {
  for (const id of [
    'modelLibrariesBtn',
    'modelLibrariesModal',
    'modelLibraryPackageInput',
    'modelLibrariesOpenFolderBtn',
    'modelLibrariesList',
    'modelLibraryDetails',
  ]) {
    assert.match(HTML, new RegExp(`id="${id}"`), `missing #${id}`);
  }
  assert.match(HTML, /role="dialog" aria-modal="true" aria-labelledby="modelLibrariesModalTitle"/);
  assert.match(HTML, /Install Library…/);
  assert.match(HTML, /Open Folder/);
  assert.match(HTML, /style\.css\?v=2026-07-14-ui31/);
  assert.match(HTML, /api\.js\?v=2026-07-14-ui31/);
  assert.match(HTML, /app\.js\?v=2026-07-14-ui31/);
});

test('model-library control lives in the sidebar configuration group', () => {
  const rail = HTML.slice(HTML.indexOf('<aside class="left-rail">'), HTML.indexOf('</aside>'));
  const modelIndex = rail.indexOf('id="railModelLibrary"');
  const filamentIndex = rail.indexOf('class="rail-library"');
  const printerIndex = rail.indexOf('class="rail-printer"');
  assert.ok(modelIndex >= 0 && modelIndex < filamentIndex && filamentIndex < printerIndex);
  assert.match(rail, /id="modelLibrariesBtn"[\s\S]*?>\s*Manage/);
  const workflow = HTML.slice(HTML.indexOf('id="tabSwitch"'), HTML.indexOf('</section>', HTML.indexOf('id="tabSwitch"')));
  assert.doesNotMatch(workflow, /id="modelLibrariesBtn"/);
});

test('sidebar model-library summary covers authoritative lifecycle states', () => {
  const start = JS.indexOf('function modelLibraryDisplayName');
  const end = JS.indexOf('function renderModelLibraryRail', start);
  assert.ok(start >= 0 && end > start);
  const source = JS.slice(start, end);
  const summarize = Function(`${source}; return modelLibraryRailSummary;`)();
  const libraries = [
    { library_id: 'live', library_name: 'Live Library', valid: true, runtime_active: true },
    { library_id: 'next', library_name: 'Next Library', valid: true, selected_for_next_launch: true },
  ];
  assert.deepEqual(
    summarize({ libraries: [libraries[0]], runtime_active_library_id: 'live', active_library_id: 'live', restart_required: false }),
    { name: 'Live Library', state: 'In Use', kind: 'ok', detail: 'This running Generator is using this model library' },
  );
  const pending = summarize({ libraries, runtime_active_library_id: 'live', active_library_id: 'next', restart_required: true });
  assert.equal(pending.name, 'Live Library');
  assert.equal(pending.state, 'Restart Required');
  const invalid = summarize({
    libraries: [{ library_id: 'broken', directory_name: 'broken', valid: false, selected_for_next_launch: true, error: 'corrupt' }],
    runtime_active_library_id: null,
    active_library_id: 'broken',
    restart_required: true,
  });
  assert.equal(invalid.state, 'Invalid');
  assert.equal(invalid.detail, 'corrupt');
  const empty = summarize({ libraries: [], runtime_active_library_id: null, active_library_id: null, restart_required: false });
  assert.equal(empty.name, 'No library selected');
  assert.equal(empty.state, 'No library selected');
  const unselectedInvalid = summarize({
    libraries: [{ library_id: null, directory_name: 'unselected-broken', valid: false, selected_for_next_launch: false }],
    runtime_active_library_id: null,
    active_library_id: null,
    restart_required: false,
  });
  assert.equal(unselectedInvalid.state, 'No library selected');
});

test('model-library API helpers preserve multipart upload boundaries and cover every action', () => {
  assert.match(API, /async function fetchModelLibraries\(\)[\s\S]*apiFetch\('\/model-libraries'\)/);
  assert.match(API, /async function installModelLibrary\(file\)[\s\S]*headers: \{\}[\s\S]*body: formData/);
  assert.match(API, /apiPost\('\/model-libraries\/activate', \{ library_id: libraryId \}\)/);
  assert.match(API, /apiPost\('\/model-libraries\/remove', \{ library_id: libraryId \}\)/);
  assert.match(API, /apiPost\('\/model-libraries\/open-folder'\)/);
  assert.match(API, /apiPost\('\/system\/restart'\)/);
});

test('install, activate, and remove replace local state with authoritative backend status', () => {
  const install = functionBody('async function handleInstallModelLibrary', 'async function waitForModelLibraryRestart');
  const activate = functionBody('async function handleActivateModelLibrary', 'async function handleRemoveModelLibrary');
  const remove = functionBody('async function handleRemoveModelLibrary', 'async function handleOpenModelLibrariesFolder');

  assert.match(install, /modelLibraryManager\.status = response\.status/);
  assert.match(activate, /const response = await activateModelLibrary\(item\.library_id\)/);
  assert.match(activate, /modelLibraryManager\.status = response\.status/);
  const activationResponse = activate.slice(activate.indexOf('const response = await activateModelLibrary'));
  assert.ok(
    activationResponse.indexOf('modelLibraryManager.status = response.status') < activationResponse.indexOf('await requestModelLibraryRestart'),
    'activation response must update UI state before restart begins',
  );
  assert.match(remove, /modelLibraryManager\.status = response\.status/);
  assert.match(remove, /item\.runtime_active \|\| item\.selected_for_next_launch/);
});

test('restart waits for backend runtime identity rather than assuming success', () => {
  const wait = functionBody('async function waitForModelLibraryRestart', 'async function requestModelLibraryRestart');
  assert.match(wait, /const status = await fetchModelLibraries\(\)/);
  assert.match(wait, /status\.runtime_active_library_id === targetId && !status\.restart_required/);
  assert.match(wait, /window\.location\.reload\(\)/);
});

test('backend validation errors are presented without API transport jargon', () => {
  assert.match(JS, /function modelLibraryErrorMessage[\s\S]*replace\(\/\^API\\s\+\\d\+:\\s\*\/i, ""\)/);
  assert.match(JS, /setModelLibraryMessage\(modelLibraryErrorMessage\(error, "The model library could not be installed"\), "error"\)/);
});

test('recovery state opens the manager and model-dependent attention remains visible', () => {
  const load = functionBody('async function loadModelLibraries', 'async function handleInstallModelLibrary');
  assert.match(load, /!status\.runtime_active_library_id \|\| status\.active_state_error/);
  assert.match(load, /modelLibraryAutoOpened = true/);
  assert.match(JS, /modelLibrariesAttention/);
  assert.match(JS, /selectedNextLibrary && !selectedNextLibrary\.valid[\s\S]*Choose a valid library before restarting Prisma/);
  assert.match(JS, /status && !libraries\.length[\s\S]*No model libraries are installed/);
  assert.match(JS, /Selected next launch/);
  assert.match(JS, /renderModelLibraryRail\(\)/);
});

test('visual rules reuse the app surface system and include a narrow layout', () => {
  assert.match(CSS, /\.model-libraries-modal[\s\S]*background: var\(--bg\)/);
  assert.match(CSS, /\.model-libraries-list-panel,[\s\S]*border: 1px solid color-mix/);
  assert.match(CSS, /\.model-library-list-item\.is-selected/);
  assert.match(CSS, /\.model-libraries-header-actions \.window-header__button:not\(\.surface-close\)[\s\S]*font-size: 11px/);
  assert.match(CSS, /@media \(max-width: 720px\)[\s\S]*\.model-libraries-body[\s\S]*grid-template-columns: 1fr/);
  assert.doesNotMatch(HTML, /modelLibrariesCloseFooterBtn/);
});
