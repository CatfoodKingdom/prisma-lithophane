const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const APP_JS = fs.readFileSync(
  path.resolve(__dirname, '../../Prisma/generator/app/app.js'),
  'utf8',
);

test('S4c: legend/source/lightbox helpers accept an explicit view context', () => {
  assert.ok(
    APP_JS.includes('function updateSolveLegend(view = solveView)'),
    'updateSolveLegend must accept an explicit view',
  );
  assert.ok(
    APP_JS.includes('function buildSolveSourceColumn(run, aspect, view = solveView'),
    'buildSolveSourceColumn must accept an explicit view',
  );
  assert.ok(
    APP_JS.includes('function openSolveRunLightbox(runId, view = solveView)'),
    'openSolveRunLightbox must accept an explicit view',
  );
});

test('S4c: the run lightbox captures and reuses its displayed view', () => {
  assert.ok(
    APP_JS.includes('{ kind: "solve", runId, view }'),
    'openSolveRunLightbox must store the displayed view in the lightbox state',
  );
  assert.ok(
    APP_JS.includes('openSolveRunLightbox(runId, card.dataset.view || solveView)'),
    'the card dispatcher must pass the clicked card\'s frozen view to the lightbox',
  );
  assert.ok(
    APP_JS.includes('openSolveRunLightbox(nextRun.id, _solveLightboxState.view)'),
    'arrow-key run navigation must reuse the stored lightbox view, not the global one',
  );
});

test('S4c: arrow-key lightbox navigation includes the source card', () => {
  assert.ok(
    APP_JS.includes('openSolveSourceLightbox(selectedRuns[0], sourceView, sourceTargetKind)'),
    'ArrowLeft from the first solve preview should open the source lightbox',
  );
  assert.ok(
    APP_JS.includes('openSolvePreviewLightboxForRun('),
    'ArrowRight from the source lightbox should return to the first solve preview card',
  );
  assert.ok(
    APP_JS.includes('_solveLightboxState.targetKind || "run"'),
    'source lightbox navigation should preserve whether the preview target is run, surface, or recipe',
  );
});

test('Thickness Maps legend describes the fixed cap triad', () => {
  assert.ok(
    APP_JS.includes('Per-filament thickness maps (each self-normalized'),
    'updateSolveLegend must render a Thickness Maps legend',
  );
  assert.ok(APP_JS.includes('White cap: Total, Boundary, and Detail'));
  assert.ok(!APP_JS.includes('solveThicknessMapKind'));
});

test('S4c: async render callbacks bail when the displayed view changed', () => {
  assert.ok(
    APP_JS.includes('if (solveView !== displayedView) return;'),
    'diff renderers must verify the view is still displayed after their async load',
  );
  assert.ok(
    APP_JS.includes('if (solveView !== view) return;'),
    'surface-canvas async callbacks must verify the view is still displayed',
  );
});

test('thickness lightbox Up/Down preserves the semantic map key', () => {
  assert.ok(
    APP_JS.includes('item.key === _solveLightboxState.mapKey'),
    'adjacent-run lookup must require the same map key',
  );
  assert.ok(
    APP_JS.includes('openThicknessLightboxForKey(nextRun.id, _solveLightboxState.mapKey)'),
    'Up/Down must open by stable semantic key',
  );
  assert.ok(!APP_JS.includes('openThicknessLightboxForPosition'));
});

test('Thickness, Highpass, and Explorer use resolved standard headers', () => {
  const thicknessStart = APP_JS.indexOf('function openThicknessLightboxForKey');
  const thicknessBody = APP_JS.slice(thicknessStart, APP_JS.indexOf('function solveRunById', thicknessStart));
  assert.match(thicknessBody, /buildSolveLightboxHeader\(run, item\.viewLabel, zoomControls\)/);
  assert.match(thicknessBody, /setupStaticLightboxZoom\(content, lifecycle\)/);

  const surfaceStart = APP_JS.indexOf('async function openSurfaceLightbox');
  const surfaceBody = APP_JS.slice(surfaceStart, APP_JS.indexOf('// ── Recipe viewer lightbox', surfaceStart));
  assert.match(surfaceBody, /buildSolveLightboxHeader\(run, getSolveLightboxViewLabel\(viewType\)\)/);
  assert.match(surfaceBody, /header\?\.getBoundingClientRect\(\)\.height/);
  assert.match(surfaceBody, /controls\?\.getBoundingClientRect\(\)\.height/);
  assert.ok(!surfaceBody.includes('controlBudget'));
  assert.ok(!surfaceBody.includes('buildStaticLightboxZoomControls'));
});

test('Explorer contextual caption resolves the live center-height helper', () => {
  const captionStart = APP_JS.indexOf('function getSolveViewCaption');
  const captionBody = APP_JS.slice(captionStart, APP_JS.indexOf('function updateSolveViewCaption', captionStart));
  assert.match(captionBody, /view === "surface_explorer"[\s\S]*?getSolveExplorerCenter\(\)/);
  assert.ok(!captionBody.includes('getSolveExplorerHeight'));
});

test('Thickness Maps show emitted filament volume before max thickness', () => {
  assert.ok(
    APP_JS.includes('function formatThicknessMapVolume(volumeMm3)'),
    'expected a thickness-map volume formatter',
  );
  assert.match(
    APP_JS,
    /formatThicknessMapVolume\(m\.volume_mm3\)[\s\S]*?`max \$\{m\.max_d\?\.toFixed\(2\) \|\| 0\} mm`[\s\S]*?`\$\{m\.active_px\?\.toLocaleString\(\) \|\| 0\} px`/,
    'filament-map card stats should include volume, max thickness, and active pixels',
  );
});

test('Thickness Maps show volume for every white-cap map variant', () => {
  for (const field of [
    'cap_map_volume_mm3',
    'boundary_cap_map_volume_mm3',
    'detail_cap_map_volume_mm3',
  ]) {
    assert.ok(APP_JS.includes(`volumeMm3: r.${field}`), `missing ${field}`);
  }
  assert.match(
    APP_JS,
    /item\.available \? \[[\s\S]*?formatThicknessMapVolume\(item\.volumeMm3\)[\s\S]*?`max \$\{item\.maxD\.toFixed\(2\)\} mm`[\s\S]*?`\$\{item\.activePx\.toLocaleString\(\)\} px`/,
    'white-cap cards should render emitted volume alongside their map statistics',
  );
});
