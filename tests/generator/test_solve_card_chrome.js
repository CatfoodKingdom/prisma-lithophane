const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const APP_JS = fs.readFileSync(
  path.resolve(__dirname, '../../Prisma/generator/app/app.js'),
  'utf8',
);
const STYLE_CSS = fs.readFileSync(
  path.resolve(__dirname, '../../Prisma/generator/app/style.css'),
  'utf8',
);

test('S4b: a shared reserved scale-bar slot helper exists and emits the slot', () => {
  assert.ok(
    APP_JS.includes('function buildSolveCardScaleBarSlot()'),
    'buildSolveCardScaleBarSlot helper must exist',
  );
  assert.ok(
    APP_JS.includes('class="solve-card-scalebar"'),
    'the helper must emit a .solve-card-scalebar element',
  );
});

test('S4b: every result card builder appends the scale-bar slot', () => {
  // source, run/surface, cap-diff, filament-diff, filament-run, + two thickness blocks.
  const calls = APP_JS.split('buildSolveCardScaleBarSlot()').length - 1;
  assert.ok(
    calls >= 7,
    `expected the slot helper to be used by every card builder (>=7 call sites), found ${calls}`,
  );
});

test('S4b: thickness cards adopt the shared top-header chrome', () => {
  assert.ok(
    !APP_JS.includes('filament-map-info'),
    'thickness cards must no longer emit the old bottom .filament-map-info block',
  );
  assert.ok(
    !APP_JS.includes('filament-map-stats'),
    'thickness cards must no longer emit the old .filament-map-stats block',
  );
  // The unified header (h4 + chips + stats) is shared with solve-grid columns.
  assert.ok(
    APP_JS.includes('class="comparison-column-stats">${esc(statsLine)}'),
    'thickness filament cards must render px/max via the shared comparison-column-stats header',
  );
  assert.ok(
    APP_JS.includes('class="comparison-column-stats">${esc(capStatsLine)}'),
    'thickness white-cap card must render px/max via the shared comparison-column-stats header',
  );
});

test('S4b: shared card header rows are height-stable (no wrap-driven growth)', () => {
  assert.ok(
    /\.solve-grid-column-header h4\s*\{[^}]*white-space:\s*nowrap[^}]*text-overflow:\s*ellipsis/.test(STYLE_CSS),
    'card title (h4) must be single-line with ellipsis so long labels cannot grow the header',
  );
  assert.ok(
    /\.solve-grid-column-header \.comparison-column-chips\s*\{[^}]*flex-wrap:\s*nowrap/.test(STYLE_CSS),
    'header chips must stay on one row (no flex-wrap) so many palette chips cannot grow the header',
  );
  assert.ok(
    /\.solve-grid-column-header \.comparison-column-stats\s*\{[^}]*white-space:\s*nowrap/.test(STYLE_CSS),
    'header stats must be single-line so long stats cannot grow the header',
  );
});

test('S4b: scale-bar slot has a fixed height; old thickness-info CSS is retired', () => {
  assert.ok(
    /\.solve-card-scalebar\s*\{[^}]*height:\s*24px/.test(STYLE_CSS),
    '.solve-card-scalebar must reserve a fixed 24px height',
  );
  assert.ok(
    !STYLE_CSS.includes('.filament-map-stats'),
    'the old .filament-map-stats CSS rule must be removed',
  );
  assert.ok(
    !/\.filament-map-card\s+\.filament-map-info\s*\{/.test(STYLE_CSS),
    'the old .filament-map-info CSS rule must be removed',
  );
});
