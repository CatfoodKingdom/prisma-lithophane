const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const APP = path.resolve(__dirname, '../../Prisma/generator/app');
const JS = fs.readFileSync(path.join(APP, 'app.js'), 'utf8');
const HTML = fs.readFileSync(path.join(APP, 'index.html'), 'utf8');
const CSS = fs.readFileSync(path.join(APP, 'style.css'), 'utf8');

function cssRuleBody(selector) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const match = CSS.match(new RegExp(`${escaped}\\s*\\{([\\s\\S]*?)\\}`));
  assert.ok(match, `style.css must contain ${selector}`);
  return match[1];
}

test('clear-history removes cards without deleting cached or automatic runs', () => {
  const clearStart = JS.indexOf('function clearSolveHistory()');
  assert.notEqual(clearStart, -1, 'clear history handler must exist');
  const clearBody = JS.slice(clearStart, JS.indexOf('function getSolveHistoryClearButtons', clearStart));
  assert.doesNotMatch(clearBody, /fetch\s*\(/);
  assert.doesNotMatch(clearBody, /\/api\/cache\/clear-runs/);
  assert.match(clearBody, /solveRuns = \[\];/);
});

test('clear-history resets solve readiness state and Preview pip source of truth', () => {
  assert.match(
    JS,
    /if \(tab === "solve"\) complete = solveRuns\.some\(run => !!run\.results\);/,
    'Preview workflow pip should be complete only when a completed solve card exists',
  );
  const clearStart = JS.indexOf('function clearSolveHistory()');
  assert.notEqual(clearStart, -1, 'clear history handler must exist');
  const clearBody = JS.slice(clearStart, JS.indexOf('function getSolveHistoryClearButtons', clearStart));
  assert.match(clearBody, /solveRuns = \[\];/);
  assert.match(
    clearBody,
    /solveStatus = \{ status: "idle", progress: "", elapsed_s: 0, result: null \};/,
    'clearing solve history should reset the latched complete solve status',
  );
  assert.match(
    JS,
    /\["clearSolveHistoryBtn", "exportClearSolveHistoryBtn"\]\.forEach[\s\S]*handleSolveHistoryClearClick/,
    'Solve and Export clear buttons should share the same clear handler',
  );
});

test('solve-history Clear uses click-to-confirm before clearing runs', () => {
  assert.match(JS, /let solveHistoryClearConfirmPending = false;/);
  assert.match(JS, /function armSolveHistoryClearConfirm\(\)[\s\S]*textContent = "Confirm\?"/);
  assert.match(JS, /function resetSolveHistoryClearConfirm\(\)[\s\S]*textContent = "Clear"/);
  assert.match(
    JS,
    /function handleSolveHistoryClearClick\(\)[\s\S]*if \(!solveHistoryClearConfirmPending\)[\s\S]*armSolveHistoryClearConfirm\(\);[\s\S]*return;[\s\S]*clearSolveHistory\(\);/,
    'first click should arm, second click should clear',
  );
  assert.match(CSS, /\.deck-header \.ghost-button\.confirm-pending \{/);
});

test('a Clear All Temp control exists and calls clear-all', () => {
  assert.ok(/clearAllTempBtn/.test(HTML), 'index.html needs #clearAllTempBtn');
  assert.ok(/Clear Temp Files/.test(HTML), 'top bar temp clear button should use the concise label');
  assert.equal(/Clear All[\s\S]*Temporary Files/.test(HTML), false, 'top bar temp clear button should not use the old two-line label');
  assert.match(CSS, /\.mode-switch \.topbar-utility-button\s*\{[\s\S]*?height:\s*30px;[\s\S]*?min-height:\s*30px;/, 'top-bar utility buttons should share a stable height');
  assert.match(CSS, /\.mode-switch \.bar-action\s*\{[\s\S]*?height:\s*30px;[\s\S]*?min-height:\s*30px;/, 'Solve button should share the top-bar action height');
  assert.equal(cssRuleBody('.top-temp-clear-btn').includes('flex-direction'), false, 'temp clear button should not keep old two-line column sizing');
  assert.ok(/\/api\/cache\/clear-all/.test(JS), 'app.js must POST /api/cache/clear-all');
});
