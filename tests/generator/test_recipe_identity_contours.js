'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP_JS = fs.readFileSync(
  path.resolve(__dirname, '../../Prisma/generator/app/app.js'),
  'utf8',
);
const INDEX_HTML = fs.readFileSync(
  path.resolve(__dirname, '../../Prisma/generator/app/index.html'),
  'utf8',
);

function extractFunction(signature) {
  const start = APP_JS.indexOf(signature);
  assert.notEqual(start, -1, `${signature} should exist`);
  const closeParams = APP_JS.indexOf(')', start);
  const open = APP_JS.indexOf('{', closeParams);
  let depth = 0;
  for (let i = open; i < APP_JS.length; i += 1) {
    if (APP_JS[i] === '{') depth += 1;
    if (APP_JS[i] === '}' && --depth === 0) return APP_JS.slice(start, i + 1);
  }
  throw new Error(`Could not extract ${signature}`);
}

function loadIdentityHelpers() {
  const context = { Map, Uint8Array, Uint32Array };
  vm.runInNewContext([
    extractFunction('function _recipeKeyFromEntries'),
    extractFunction('function buildRecipeIdentityMap'),
    extractFunction('function buildDiscreteLabelBoundaryMask'),
  ].join('\n\n'), context);
  return context;
}

test('recipe contours separate equal-height physical recipes and merge duplicate stack ids', () => {
  const context = loadIdentityHelpers();
  const chestnutOnly = [{ filament_id: 'chestnut-brown', thickness_mm: 0.16 }];
  const chestnutBlueGray = [
    { filament_id: 'chestnut-brown', thickness_mm: 0.08 },
    { filament_id: 'blue-gray', thickness_mm: 0.08 },
  ];
  const singlePixelRecipe = [{ filament_id: 'ochre', thickness_mm: 0.16 }];
  const keys = [
    context._recipeKeyFromEntries(chestnutOnly),
    context._recipeKeyFromEntries(chestnutBlueGray),
    context._recipeKeyFromEntries(chestnutOnly), // different stack id, same physical recipe
    context._recipeKeyFromEntries(singlePixelRecipe),
  ];
  const stackLabels = {
    width: 3,
    height: 3,
    data: new Uint32Array([
      0, 0, 1,
      0, 2, 1,
      3, 2, 1,
    ]),
  };

  const identity = context.buildRecipeIdentityMap(stackLabels, keys);
  const boundaries = context.buildDiscreteLabelBoundaryMask(identity);

  assert.equal(identity.data[0], identity.data[4], 'duplicate canonical recipes should share identity');
  assert.notEqual(identity.data[0], identity.data[2], 'equal total height must not merge distinct recipes');
  assert.deepEqual(Array.from(boundaries.vertical), [0, 1, 0, 1, 1, 1]);
  assert.deepEqual(Array.from(boundaries.horizontal), [0, 0, 0, 1, 0, 0]);
  assert.equal(boundaries.vertical.length, 6, 'image exterior is not encoded as a recipe boundary');
  assert.equal(boundaries.horizontal.length, 6, 'image exterior is not encoded as a recipe boundary');
});

test('diagonal contact creates no diagonal connector', () => {
  const context = loadIdentityHelpers();
  const identity = {
    width: 2,
    height: 2,
    data: new Uint32Array([0, 1, 2, 0]),
  };
  const boundaries = context.buildDiscreteLabelBoundaryMask(identity);
  assert.deepEqual(Array.from(boundaries.vertical), [1, 1]);
  assert.deepEqual(Array.from(boundaries.horizontal), [1, 1]);
  assert.deepEqual(Object.keys(boundaries).sort(), ['height', 'horizontal', 'vertical', 'width']);
});

test('recipe artifact loading is independent, promise-cached, and invalidated with its run', () => {
  const artifacts = extractFunction('async function ensureRecipeArtifactData');
  const cookbook = extractFunction('async function ensureRecipeCookbook');
  const combined = extractFunction('async function ensureRecipeData');
  const invalidation = extractFunction('function invalidateSolveRunCaches');

  assert.match(artifacts, /recipeDataPromiseCache\[run\.id\]/);
  assert.match(artifacts, /explorer_stack_label_bin_url/);
  assert.match(artifacts, /buildRecipeIdentityMap[\s\S]*buildDiscreteLabelBoundaryMask/);
  assert.doesNotMatch(artifacts, /color_recipe_breakdown_cookbook_url|fetch\(/);
  assert.match(cookbook, /color_recipe_breakdown_cookbook_url[\s\S]*fetch\(url\)/);
  assert.match(combined, /ensureRecipeArtifactData\(run\)[\s\S]*ensureRecipeCookbook\(run, recipeData\)/);
  assert.match(invalidation, /delete recipeDataCache\[runId\]/);
  assert.match(invalidation, /delete recipeDataPromiseCache\[runId\]/);
  assert.match(invalidation, /recipeDataGeneration\[runId\]/);
  assert.doesNotMatch(invalidation, /^\s*invalidateSolveRunCaches\(/m, 'cache invalidation must not recurse');
  assert.match(APP_JS, /function deleteSolveRun[\s\S]*?invalidateSolveRunCaches\(run\)/);
});

test('recipe view dispatches discrete boundaries and never reuses height contours', () => {
  const contourUrl = extractFunction('function getSolveContourUrl');
  const renderer = extractFunction('function renderSolveContourCanvasForRun');
  assert.match(contourUrl, /view === "recipe_regions"\) return ""/);
  assert.match(renderer, /view === "recipe_regions"[\s\S]*ensureRecipeArtifactData\(run\)/);
  assert.match(renderer, /drawRecipeBoundaryOverlay\(canvas, boundaries\)/);
  const recipeBranch = renderer.slice(
    renderer.indexOf('if (view === "recipe_regions")'),
    renderer.indexOf('const url = getSolveContourUrl'),
  );
  assert.doesNotMatch(recipeBranch, /loadSolveContourData|strokeLayerContourPaths|color_ceiling/);
});

test('recipe contour copy and unavailable state describe recipe boundaries', () => {
  assert.match(APP_JS, /aria-label="\$\{escAttr\(run\.label\)\} recipe boundaries"/);
  assert.match(APP_JS, /contourLegendHtml\("Recipe boundary"\)/);
  assert.match(APP_JS, /Recipe boundaries are unavailable for this older run/);
  assert.match(APP_JS, /data-contours-available=/);
  assert.match(INDEX_HTML, /Show or hide boundaries for the current view/);
  assert.doesNotMatch(APP_JS, /Show layer-height contours on the image|recipe card[^\n]*shares color_ceiling contours/i);
});
