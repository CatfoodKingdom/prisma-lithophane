const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const APP_JS = fs.readFileSync(
  path.resolve(__dirname, '../../Prisma/generator/app/app.js'),
  'utf8',
);

function paletteRuntime() {
  const start = APP_JS.indexOf('const DIAGNOSTIC_PALETTE_INFERNO');
  const end = APP_JS.indexOf('const SURFACE_CONTOUR_SCALE', start);
  const legendStart = APP_JS.indexOf('function getDiagnosticPaletteLegendState');
  const legendEnd = APP_JS.indexOf('function updateSolveLegend', legendStart);
  assert.ok(start >= 0 && end > start && legendStart >= 0 && legendEnd > legendStart);
  return Function(
    'esc',
    `${APP_JS.slice(start, end)}
     ${APP_JS.slice(legendStart, legendEnd)}
     return {
       DIAGNOSTIC_PALETTE_INFERNO,
       DIAGNOSTIC_PALETTE_LEGACY,
       inferno,
       legacyScalarPalette,
       sampleScalarPalette,
       getRunDiagnosticPaletteVersion,
       getDiagnosticPaletteLegendState,
       diagnosticPaletteLegendHtml,
     };`,
  )(value => String(value));
}

test('frontend samples the canonical Inferno vector with documented half-up interpolation', () => {
  const runtime = paletteRuntime();
  assert.deepEqual(runtime.inferno(0), [0, 0, 4]);
  assert.deepEqual(runtime.inferno(1 / 255), [1, 0, 5]);
  assert.deepEqual(runtime.inferno(64 / 255), [87, 16, 110]);
  assert.deepEqual(runtime.inferno(0.5), [187, 55, 85]);
  assert.deepEqual(runtime.inferno(128 / 255), [188, 55, 84]);
  assert.deepEqual(runtime.inferno(192 / 255), [249, 142, 9]);
  assert.deepEqual(runtime.inferno(1), [252, 255, 164]);
});

test('unmarked runs use the exact legacy renderer and marked runs use Inferno', () => {
  const runtime = paletteRuntime();
  const legacyRun = { results: {} };
  const newRun = { results: { diagnostic_palette_version: 'inferno-v1' } };
  assert.equal(runtime.getRunDiagnosticPaletteVersion(legacyRun), 'legacy-approximate');
  assert.equal(runtime.getRunDiagnosticPaletteVersion(newRun), 'inferno-v1');
  assert.deepEqual(runtime.sampleScalarPalette(0, 'legacy-approximate'), [68, 0, 83]);
  assert.deepEqual(runtime.sampleScalarPalette(0, 'inferno-v1'), [0, 0, 4]);
});

test('legends dispatch by provenance and mixed selections show a warning instead of one gradient', () => {
  const runtime = paletteRuntime();
  const legacy = { results: {} };
  const current = { results: { diagnostic_palette_version: 'inferno-v1' } };
  const infernoState = runtime.getDiagnosticPaletteLegendState([current]);
  assert.equal(infernoState.mixed, false);
  assert.match(infernoState.gradient, /#000004/);
  assert.match(runtime.diagnosticPaletteLegendHtml(infernoState), /data-diagnostic-palette="inferno-v1"/);

  const mixed = runtime.getDiagnosticPaletteLegendState([legacy, current]);
  assert.equal(mixed.mixed, true);
  const html = runtime.diagnosticPaletteLegendHtml(mixed);
  assert.match(html, /Mixed diagnostic palettes/);
  assert.doesNotMatch(html, /legend-bar/);
});

test('categorical and appearance views retain their existing color semantics', () => {
  const richStart = APP_JS.indexOf('function renderExplorerRich');
  const richBody = APP_JS.slice(richStart, APP_JS.indexOf('function initExplorerControls', richStart));
  const recipeStart = APP_JS.indexOf('async function openRecipeLightbox');
  const recipeBody = APP_JS.slice(recipeStart, APP_JS.indexOf('// Map a filament id', recipeStart));
  assert.ok(!richBody.includes('sampleScalarPalette'));
  assert.ok(!recipeBody.includes('sampleScalarPalette'));
  assert.match(APP_JS, /case "predicted"[\s\S]*?return result\.predicted_appearance_url/);
});
