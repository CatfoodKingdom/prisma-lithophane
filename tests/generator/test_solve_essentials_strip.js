const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP_JS = path.resolve(__dirname, '../../Prisma/generator/app/app.js');
const SOURCE = fs.readFileSync(APP_JS, 'utf8');
const INDEX_HTML = fs.readFileSync(
  path.resolve(__dirname, '../../Prisma/generator/app/index.html'),
  'utf8',
);

function extractFunction(signature) {
  const start = SOURCE.indexOf(signature);
  if (start === -1) throw new Error(`Could not find: ${signature}`);
  const openBrace = SOURCE.indexOf('{', start);
  let depth = 0;
  for (let i = openBrace; i < SOURCE.length; i += 1) {
    if (SOURCE[i] === '{') depth += 1;
    else if (SOURCE[i] === '}') {
      depth -= 1;
      if (depth === 0) return SOURCE.slice(start, i + 1);
    }
  }
  throw new Error(`No closing brace for: ${signature}`);
}

test('config grid and Last Solve strip are gone; essentials live on preview cards', () => {
  assert.ok(!INDEX_HTML.includes('id="solveRunEssentialsStrip"'), 'top-level Last Solve strip should be removed');
  assert.ok(!INDEX_HTML.includes('id="solveConfigGrid"'), '#solveConfigGrid must be removed');
  assert.ok(!SOURCE.includes('function renderSolveRunEssentialsStrip'), 'Last Solve strip renderer should be removed');
  assert.ok(SOURCE.includes('function buildSolveRunCardMetadataFooter'), 'run essentials should live on preview cards');
  assert.ok(!SOURCE.includes('function renderSolveConfigSummary'), 'renderSolveConfigSummary must be removed');
});

test('essentials list: detail limit is visible and modules are names-only', () => {
  const ctx = {
    getSolveRunSettingsSnapshot: (run) => run.config,
    normalizeLuminanceMode: (m) =>
      ['luminance', 'luminance_detail', 'detail', 'luminance-detail'].includes(String(m || '').toLowerCase())
        ? 'luminance_detail'
        : 'standard',
    formatSolveSummaryMm: (v) => `${v} mm`,
    getSolveRunActiveModulesForSlot: () => ['printscale_bilateral'],
    moduleDescriptorById: () => null,
    moduleDisplayName: (d) => d.label,
    humanizeModuleName: (n) => `H:${n}`,
  };
  vm.runInNewContext(`${extractFunction('function getSolveRunEssentialsItems')}; this.fn = getSolveRunEssentialsItems;`, ctx);

  const standard = ctx.fn({ config: { luminance_mode: 'standard', detail_cap_max_layers: 5, color_region_target_mm: 0.6 } });
  const stdLabels = standard.map((i) => i.label);
  assert.equal(JSON.stringify(stdLabels.slice(0, 7)), JSON.stringify([
    'Mode',
    'Solve pitch',
    'Layer height',
    'Max thickness',
    'Color region target',
    'Detail limit',
    'Base thickness',
  ]));
  assert.equal(standard.find((i) => i.label === 'Mode').value, 'Color');
  assert.ok(!stdLabels.includes('Detail cap'), 'detail cap no longer has an on/off summary');
  assert.equal(standard.find((i) => i.label === 'Detail limit').value, '5 layers');
  assert.ok(stdLabels.includes('Color region target'), 'shows color region target');
  // Module shown by name only (fallback humanizer), never a param row.
  assert.equal(standard.find((i) => i.label === 'Pre-processing').value, 'H:printscale_bilateral');

  const luminance = ctx.fn({
    config: {
      luminance_mode: 'luminance_detail',
      detail_cap_max_layers: 20,
    },
  });
  const lumLabels = luminance.map((i) => i.label);
  assert.ok(lumLabels.includes('Detail limit'), 'luminance mode shows Detail limit');
  assert.equal(luminance.find((i) => i.label === 'Mode').value, 'Luminance');

  const banded = ctx.fn({
    config: { luminance_mode: 'standard', detail_cap_max_layers: 5 },
    results: { staged_metrics: { swap_grouping: {
      groups: [['a', 'c'], ['b']],
      band_heights_mm: [0.8, 1.04],
      pause_z_mm: [1.0],
      banding_cost: { median_de_delta: 0.375 },
    } } },
  });
  assert.equal(banded.find((i) => i.label === 'Swap groups').value, '2 (2 + 1 colors)');
  assert.equal(banded.find((i) => i.label === 'Band heights').value, '0.80 mm / 1.04 mm');
  assert.equal(banded.find((i) => i.label === 'Pause count').value, '1 (z=1.00 mm)');
  assert.equal(banded.find((i) => i.label === 'Swap banding cost').value, 'median +0.38 dE');

  const unavailable = ctx.fn({
    config: { luminance_mode: 'standard', detail_cap_max_layers: 5 },
    results: { staged_metrics: { swap_plan_availability: { available: false, reason: 'swap plan unavailable' } } },
  });
  assert.equal(unavailable.find((i) => i.label === 'Swap plan').value, 'swap plan unavailable');
});
