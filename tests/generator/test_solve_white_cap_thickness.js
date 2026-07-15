const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const INDEX_HTML = fs.readFileSync(
  path.resolve(__dirname, '../../Prisma/generator/app/index.html'),
  'utf8',
);
const APP_JS = fs.readFileSync(
  path.resolve(__dirname, '../../Prisma/generator/app/app.js'),
  'utf8',
);

test('White Cap is height-only: no Thickness/Height mode toggle', () => {
  assert.ok(!INDEX_HTML.includes('data-solve-white-cap-mode'), 'White Cap mode toggle must be gone from markup');
  assert.ok(!APP_JS.includes('solveWhiteCapMode'), 'solveWhiteCapMode state must be removed');
});

test('White Cap still exposes Total/Boundary/Detail subviews', () => {
  assert.ok(INDEX_HTML.includes('data-solve-white-cap-view="cap_map"'), 'Total subview should remain');
  assert.ok(INDEX_HTML.includes('data-solve-white-cap-view="boundary_cap_map"'), 'Boundary subview should remain');
  assert.ok(INDEX_HTML.includes('data-solve-white-cap-view="detail_cap_map"'), 'Detail subview should remain');
  const whiteCapControls = INDEX_HTML.slice(
    INDEX_HTML.indexOf('id="solveWhiteCapControls"'),
    INDEX_HTML.indexOf('id="solveThicknessMapControls"'),
  );
  assert.ok(whiteCapControls.includes('View:'), 'White Cap selector label should read View');
  assert.ok(!whiteCapControls.includes('Map:'), 'White Cap selector label should not read Map');
});

test('Thickness Maps exposes a Total/Boundary/Detail selector', () => {
  assert.ok(INDEX_HTML.includes('id="solveThicknessMapControls"'), 'Thickness Maps selector row should exist');
  assert.ok(INDEX_HTML.includes('data-solve-thickness-map-kind="total"'), 'Total kind button should exist');
  assert.ok(INDEX_HTML.includes('data-solve-thickness-map-kind="boundary"'), 'Boundary kind button should exist');
  assert.ok(INDEX_HTML.includes('data-solve-thickness-map-kind="detail"'), 'Detail kind button should exist');
});

test('Thickness Maps selector is wired to state', () => {
  assert.ok(APP_JS.includes('let solveThicknessMapKind'), 'solveThicknessMapKind state must exist');
  assert.ok(
    APP_JS.includes('byKind[solveThicknessMapKind]'),
    'white-cap thickness items must be selected by solveThicknessMapKind',
  );
});
