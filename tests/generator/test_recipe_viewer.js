// Phase 3 source-string gate for the interactive recipe viewer.
//
// Mirrors the existing JS gates (test_solve_explorer.js): read index.html and
// app.js as text and assert the wiring is present. These are not behavioral
// tests — they pin that the color-only recipe card, the cookbook fetch, the
// tree render, and the node->region highlight wiring exist and stay wired, so
// a future refactor that removes them fails loudly.

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
const STYLE_CSS = fs.readFileSync(
  path.resolve(__dirname, '../../Prisma/generator/app/style.css'),
  'utf8',
);

test('Recipe viewer card shows the color-only predicted appearance', () => {
  assert.ok(
    APP_JS.includes('predicted_color_only_appearance_url'),
    'recipe card must read the color-only appearance URL',
  );
  assert.ok(
    APP_JS.includes('buildSolveRecipeColumn'),
    'a dedicated recipe-card builder must exist',
  );
});

test('Recipe lightbox fetches and renders the cookbook tree', () => {
  assert.ok(
    APP_JS.includes('color_recipe_breakdown_cookbook_url'),
    'recipe lightbox must fetch the cookbook artifact',
  );
  assert.ok(
    APP_JS.includes('openRecipeLightbox'),
    'a dedicated recipe lightbox opener must exist',
  );
  assert.ok(
    APP_JS.includes('renderRecipeTree') || APP_JS.includes('buildRecipeTree'),
    'the cookbook must be rendered as a tree',
  );
  assert.ok(
    APP_JS.includes('area_fraction'),
    'tree nodes must surface area_fraction as a percentage',
  );
});

test('Specific recipes live in an attached bucket, not inlined in the tree', () => {
  // The long tail of thickness recipes would bloat the hierarchy; it lives in a
  // separate master-detail bucket that fills when a family/combo is selected.
  assert.ok(
    APP_JS.includes('renderRecipeBucket'),
    'a dedicated recipe-bucket renderer must exist',
  );
  assert.ok(
    APP_JS.includes('recipeBucket') && APP_JS.includes('recipe-bucket'),
    'the lightbox panel must contain a recipe bucket pane',
  );
  assert.ok(
    !APP_JS.includes('recipe-node-recipe'),
    'specific-recipe leaves must no longer be inlined in the hierarchy tree',
  );
  assert.ok(
    APP_JS.includes('recipe-bucket-head') && APP_JS.includes('recipe-bucket-cell'),
    'the bucket renders recipes as a table (chip header row + thickness cells)',
  );
});

test('Recipe lightbox gives the taxonomy pane extra width', () => {
  assert.match(
    STYLE_CSS,
    /\.recipe-lightbox-panel \{[\s\S]*?width: 680px;/,
    'recipe lightbox panel should be wide enough for taxonomy labels',
  );
  assert.match(
    STYLE_CSS,
    /\.recipe-hierarchy-col \{[\s\S]*?flex: 1\.35 1 0;/,
    'taxonomy pane should be wider than the attached recipe bucket',
  );
});

test('Sub-0.1% recipes collapse behind an expandable tail row', () => {
  // A combo can hold hundreds of near-zero recipes; only the real-area ones
  // (>= 0.1% of the image) show, the rest hide behind one expander row. The
  // behavioral partition is covered in test_recipe_tail.js; here we pin wiring.
  assert.ok(
    APP_JS.includes('partitionRecipeTail'),
    'a tail-partition helper must decide which recipes show vs collapse',
  );
  assert.ok(
    APP_JS.includes('recipe-bucket-tail-toggle') &&
      APP_JS.includes('recipe-bucket-tail-row'),
    'the bucket must render an expander row + collapsible tail rows',
  );
  assert.ok(
    APP_JS.includes('tail-expanded'),
    'clicking the expander must toggle the collapsed tail open',
  );
});

test('collapsed tail rows hide via a rule scoped under .recipe-bucket-table', () => {
  // Tail rows also carry .recipe-node (display:flex, defined LATER in the sheet).
  // A bare `.recipe-bucket-tail-row { display:none }` ties on specificity and
  // loses by source order, so the rows never actually collapse. The hide rule
  // must be scoped under .recipe-bucket-table so it out-specifies .recipe-node.
  assert.ok(
    /\.recipe-bucket-table\s+\.recipe-bucket-tail-row\s*\{[^}]*display:\s*none/.test(STYLE_CSS),
    'collapsed tail-row hide rule must be scoped under .recipe-bucket-table to beat .recipe-node',
  );
});

test('Color Regions shows ONE card via a Ceiling / Recipe Viewer sub-tab', () => {
  // The recipe viewer is no longer an always-on second card; a sub-tab toggles
  // which single card the Color Regions tab shows.
  assert.ok(
    APP_JS.includes('solveColorRegionsView') &&
      APP_JS.includes('data-solve-color-regions-view'),
    'a sub-tab state + control must drive which Color Regions card shows',
  );
  assert.ok(
    APP_JS.includes('solveColorRegionsView === "recipe_regions"'),
    'the recipe card must render only when the Recipe Viewer sub-tab is active',
  );
});

test('Recipe lightbox wires node selection to region highlighting', () => {
  // Highlighting derives from the per-pixel stack-label bin + stack_table,
  // mapped to recipe keys — no new per-pixel backend data needed.
  assert.ok(
    APP_JS.includes('loadUint32Blob'),
    'region highlighting must decode the per-pixel stack-label bin',
  );
  assert.ok(
    APP_JS.includes('explorer_stack_label_bin_url'),
    'recipe highlighting reuses the explorer stack-label blob',
  );
  assert.ok(
    APP_JS.includes('explorer_stack_table'),
    'recipe highlighting reuses the explorer stack table',
  );
  assert.ok(
    APP_JS.includes('highlightRecipeRegions') ||
      APP_JS.includes('recipeRegionHighlight'),
    'node selection must drive a region-highlight render',
  );
});

test('Clicking the active recipe taxonomy node clears the selection', () => {
  const selection = APP_JS.slice(APP_JS.indexOf('function applySelection('), APP_JS.indexOf('// One handler for both panes', APP_JS.indexOf('function applySelection(')));
  assert.match(selection, /classList\.contains\("is-active"\)/);
  assert.match(selection, /selectedKeys = null/);
  assert.match(selection, /highlightRecipeRegions\(canvas, recipeData, null\)/);
  assert.match(selection, /Select a family or combo to list its recipes/);
});

test('Recipe lightbox shows a per-region thickness readout', () => {
  assert.ok(
    APP_JS.includes('recipe-region-readout') ||
      APP_JS.includes('recipeRegionReadout'),
    'hovering/clicking a region must show its exact thicknesses',
  );
});

test('Recipe lightbox region readout is an attached preview inspector', () => {
  assert.ok(
    APP_JS.includes('id="recipeRegionReadoutPanel"') &&
      APP_JS.includes('data-anchor="top-right"'),
    'region readout should be mounted as a preview-frame inspector',
  );
  assert.ok(
    APP_JS.includes('updateRecipeReadoutAnchor') &&
      APP_JS.includes('anchor === "top-left" ? "top-right" : "top-left"'),
    'region readout should relocate away from either side when the pointer would collide with it',
  );
  assert.ok(
    /\.recipe-region-readout\s*\{[\s\S]*?position:\s*absolute/.test(STYLE_CSS),
    'region readout should be positioned over the preview frame, not in the taxonomy panel flow',
  );
});

test('Recipe card declares a dedicated lightbox kind for the dispatcher', () => {
  assert.ok(
    APP_JS.includes('"recipe"') || APP_JS.includes("'recipe'"),
    'the click dispatcher must route a recipe card kind',
  );
});

test('Recipe lightbox has a Contours toggle sharing the global contour state', () => {
  // The toolbar Contours button drives the same solveContoursEnabled state as
  // the main Color Regions toggle, and a contour canvas overlays the image.
  assert.ok(
    APP_JS.includes('recipeLightboxContoursToggle'),
    'the lightbox toolbar must render a Contours toggle button',
  );
  assert.ok(
    APP_JS.includes('syncRecipeLightboxContoursToggle'),
    'the lightbox toggle must mirror the shared solveContoursEnabled state',
  );
  assert.ok(
    APP_JS.includes('recipe-lightbox-contour-canvas') &&
      APP_JS.includes('renderSolveLightboxContours(run, "recipe_regions")'),
    'a contour canvas must be mounted and rendered over the recipe image',
  );
});

test('Recipe lightbox closes when clicking the empty matte', () => {
  // Clicks land on the image/panel/header/toolbar keep it open; the empty
  // surround closes via closeCompLightbox.
  assert.ok(
    APP_JS.includes('.recipe-lightbox-panel, .recipe-lightbox-img, .recipe-region-readout, .comp-lightbox-topbar, .recipe-lightbox-toolbar'),
    'the wrap click handler must distinguish interactive areas from the empty matte',
  );
});

test('Recipe viewer preserves swap-band fill and band identity in stack readouts', () => {
  assert.ok(
    APP_JS.includes('band_index') && APP_JS.includes('white_fill'),
    'banded stack-table entries must retain their band and white-fill metadata',
  );
  assert.ok(
    APP_JS.includes('White fill') && APP_JS.includes('Band ${Number(e.bandIndex) + 1}'),
    'the per-region readout must name the physical band and white fill explicitly',
  );
});
