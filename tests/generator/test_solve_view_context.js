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

test('S4c: Thickness Maps legend reflects the selected solveThicknessMapKind', () => {
  assert.ok(
    APP_JS.includes('Per-filament thickness maps (each self-normalized'),
    'updateSolveLegend must render a Thickness Maps legend',
  );
  // The kind label is derived from solveThicknessMapKind inside the thickness legend branch.
  const idx = APP_JS.indexOf('Per-filament thickness maps');
  const branch = APP_JS.slice(Math.max(0, idx - 400), idx);
  assert.ok(
    branch.includes('solveThicknessMapKind'),
    'the Thickness Maps legend must be keyed on solveThicknessMapKind',
  );
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

test('S4c: thickness lightbox Up/Down preserves the current map index (nit-2)', () => {
  assert.ok(
    APP_JS.includes('openThicknessLightboxForPosition(selectedRuns[runIndex + 1].id, _solveLightboxState.mapIndex)'),
    'ArrowDown must keep the current map index on the adjacent run',
  );
  assert.ok(
    APP_JS.includes('openThicknessLightboxForPosition(selectedRuns[runIndex - 1].id, _solveLightboxState.mapIndex)'),
    'ArrowUp must keep the current map index on the adjacent run',
  );
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
    /formatThicknessMapVolume\(item\.volumeMm3\)[\s\S]*?`max \$\{item\.maxD\.toFixed\(2\)\} mm`[\s\S]*?`\$\{item\.activePx\.toLocaleString\(\)\} px`/,
    'white-cap cards should render emitted volume alongside their map statistics',
  );
});
