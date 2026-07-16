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
    INDEX_HTML.indexOf('id="solveContourControls"'),
  );
  assert.ok(whiteCapControls.includes('View:'), 'White Cap selector label should read View');
  assert.ok(!whiteCapControls.includes('Map:'), 'White Cap selector label should not read Map');
});

test('Thickness Maps selector and modal state are removed', () => {
  assert.ok(!INDEX_HTML.includes('id="solveThicknessMapControls"'), 'Thickness Maps selector row must be gone');
  assert.ok(!INDEX_HTML.includes('data-solve-thickness-map-kind'), 'Thickness kind buttons must be gone');
  assert.ok(!APP_JS.includes('solveThicknessMapKind'), 'Thickness selector state must be gone');
});

test('Thickness Maps always defines the stable white-cap triad', () => {
  const start = APP_JS.indexOf('function getSolveWhiteCapThicknessItems');
  const end = APP_JS.indexOf('function getSolveThicknessDisplayItems', start);
  const body = APP_JS.slice(start, end);
  for (const [key, label] of [
    ['cap:total', 'Total White Cap'],
    ['cap:boundary', 'Boundary Cap'],
    ['cap:detail', 'Detail Cap'],
  ]) {
    assert.ok(body.includes(`key: "${key}"`), `missing stable key ${key}`);
    assert.ok(body.includes(`label: "${label}"`), `missing card ${label}`);
  }
  assert.ok(body.includes('available: Boolean(item.url)'), 'legacy-missing artifacts must remain explicit slots');
  assert.ok(APP_JS.includes('solve-grid-empty-map">Unavailable'), 'missing maps should render Unavailable');
});

test('semantic item construction keeps unavailable cap slots but omits them from navigation', () => {
  const start = APP_JS.indexOf('function getSolveWhiteCapThicknessItems');
  const end = APP_JS.indexOf('function openThicknessLightboxForKey', start);
  const runtime = Function(
    'filamentById',
    `${APP_JS.slice(start, end)}; return {
      getSolveWhiteCapThicknessItems,
      getSolveThicknessDisplayItems,
      getSolveThicknessItems,
    };`,
  )(id => ({ color_name: id === 'red' ? 'Warm Red' : 'Cool Blue' }));
  const run = {
    results: {
      filament_maps: [
        { filament_id: 'blue', map_url: '/blue.png' },
        { filament_id: 'red', map_url: '/red.png' },
      ],
      cap_map_url: '/total.png',
      boundary_cap_map_url: '/boundary.png',
      detail_cap_map_url: null,
    },
  };
  assert.deepEqual(
    runtime.getSolveThicknessDisplayItems(run).map(item => item.key),
    ['filament:blue', 'filament:red', 'cap:total', 'cap:boundary', 'cap:detail'],
  );
  assert.deepEqual(
    runtime.getSolveThicknessItems(run).map(item => item.key),
    ['filament:blue', 'filament:red', 'cap:total', 'cap:boundary'],
  );
  const missing = runtime.getSolveWhiteCapThicknessItems(run)[2];
  assert.equal(missing.label, 'Detail Cap');
  assert.equal(missing.available, false);
});
