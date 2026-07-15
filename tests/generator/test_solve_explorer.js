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

test('Explorer is always-rich: no Default/Rich mode toggle, no Cap Parts toggle', () => {
  assert.ok(!INDEX_HTML.includes('data-solve-explorer-mode'), 'Default/Rich mode toggle must be gone');
  assert.ok(!INDEX_HTML.includes('solveExplorerCapPartsToggle'), 'Cap Parts toggle must be gone');
  assert.ok(!APP_JS.includes('solveExplorerMode'), 'solveExplorerMode state must be removed');
  assert.ok(!APP_JS.includes('solveExplorerCapPartsEnabled'), 'cap-parts state must be removed');
});

test('Explorer keeps its Height control', () => {
  assert.ok(INDEX_HTML.includes('id="explorerHeightSlider"'), 'Explorer height slider should remain');
});
