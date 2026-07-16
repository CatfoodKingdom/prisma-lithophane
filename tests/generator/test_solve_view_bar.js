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
const CSS = fs.readFileSync(
  path.resolve(__dirname, '../../Prisma/generator/app/style.css'),
  'utf8',
);

// Scope assertions to the Preview view bar (#solveViewBar) so later slices that
// re-introduce parked views elsewhere (e.g. a dev-only frame) don't trip them.
function viewBarRegion() {
  const start = INDEX_HTML.indexOf('id="solveViewBar"');
  assert.notEqual(start, -1, 'expected #solveViewBar in index.html');
  const end = INDEX_HTML.indexOf('id="solveSubControls"', start);
  assert.notEqual(end, -1, 'expected #solveSubControls after the view bar');
  return INDEX_HTML.slice(start, end);
}

function solveSubControlsRegion() {
  const start = INDEX_HTML.indexOf('id="solveSubControls"');
  assert.notEqual(start, -1, 'expected #solveSubControls in index.html');
  const end = INDEX_HTML.indexOf('id="solveComparisonGrid"', start);
  assert.notEqual(end, -1, 'expected #solveComparisonGrid after sub-controls');
  return INDEX_HTML.slice(start, end);
}

test('everyday views are present in the view bar', () => {
  const bar = viewBarRegion();
  for (const view of ['predicted', 'white_cap', 'color_ceiling', 'total_surface', 'surface_explorer']) {
    assert.ok(bar.includes(`data-view="${view}"`), `expected everyday view ${view}`);
  }
  assert.ok(bar.includes('>Color Regions<'), 'the color_ceiling tab must be labeled "Color Regions"');
});

test('advanced views live inside the Advanced disclosure group', () => {
  const bar = viewBarRegion();
  const groupStart = bar.indexOf('id="solveAdvancedViews"');
  assert.notEqual(groupStart, -1, 'expected #solveAdvancedViews group');
  const group = bar.slice(groupStart);
  assert.ok(group.includes('data-view="thickness_maps"'), 'thickness_maps in advanced group');
  assert.ok(group.includes('data-view="surface_highpass"'), 'surface_highpass in advanced group');
});

test('removed views are gone entirely; diff views only in the dev parked frame', () => {
  const bar = viewBarRegion();
  // Palette Fit + Diagnostic Views are removed for everyone — nowhere in the bar.
  for (const view of ['de_perceptual', 'diagnostic_views']) {
    assert.ok(!bar.includes(`data-view="${view}"`), `removed view ${view} must not be in the bar`);
  }
  // Cap/Color Diff are parked: absent from the everyday + advanced groups, present only in
  // the dev-only #solveParkedViews frame.
  const everydayEnd = bar.indexOf('id="solveParkedViews"');
  assert.notEqual(everydayEnd, -1, 'expected the dev parked frame');
  const beforeParked = bar.slice(0, everydayEnd);
  const parked = bar.slice(everydayEnd);
  for (const view of ['cap_diff', 'filament_diff']) {
    assert.ok(!beforeParked.includes(`data-view="${view}"`), `${view} must not be in the everyday/advanced bar`);
    assert.ok(parked.includes(`data-view="${view}"`), `${view} should live in the dev parked frame`);
  }
});

test('parked frame is dev-gated and hidden by default', () => {
  const bar = viewBarRegion();
  const frameIdx = bar.indexOf('id="solveParkedViews"');
  const openTag = bar.slice(bar.lastIndexOf('<', frameIdx), bar.indexOf('>', frameIdx) + 1);
  assert.ok(openTag.includes('hidden'), 'parked frame must be hidden by default');
  const APP_JS = path.resolve(__dirname, '../../Prisma/generator/app/app.js');
  const src = fs.readFileSync(APP_JS, 'utf8');
  assert.ok(src.includes('function isSolveDevViewsEnabled'), 'expected the dev-views gate helper');
  assert.ok(src.includes('function coerceSolveViewForAccess'), 'expected the stale-view coercion guard');
});

test('Preview predicted display is appearance-only', () => {
  assert.equal(INDEX_HTML.includes('id="solveViewingModeControls"'), false);
  assert.equal(INDEX_HTML.includes('data-solve-viewing-mode'), false);
  assert.equal(APP_JS.includes('solveViewingMode'), false);
  assert.equal(APP_JS.includes('data-solve-viewing-mode'), false);
  assert.match(
    APP_JS,
    /case "predicted":\s*\/\/ Appearance = F\(predicted\), baked server-side;[\s\S]*?return result\.predicted_appearance_url;/,
    'Predicted view must render the baked appearance image',
  );
});

test('Source Image toggle lives in the Preview view bar as a separated view option', () => {
  const bar = viewBarRegion();
  assert.ok(bar.includes('id="solveSourceControls"'), 'source controls should live in the view bar');
  assert.ok(bar.includes('class="view-option-group is-hidden"'), 'source controls should use the view-option group styling');
  assert.ok(bar.includes('id="solveSourceImageToggle"'), 'source toggle should be in the view bar');
  assert.ok(bar.includes('Source Image: On'), 'source toggle should expose its On/Off state');

  const subStart = INDEX_HTML.indexOf('id="solveSubControls"');
  assert.notEqual(subStart, -1, 'expected #solveSubControls in index.html');
  const subRegion = INDEX_HTML.slice(subStart);
  assert.equal(
    subRegion.includes('id="solveSourceControls"'),
    false,
    'source controls should not remain in the contextual sub-control stack',
  );
  assert.ok(
    APP_JS.includes('sourceToggle.textContent = `Source Image: ${solveShowSourceImage ? "On" : "Off"}`;'),
    'source toggle text should stay synchronized with state',
  );
});

test('Contours toggle is a contextual On/Off view option', () => {
  const subRegion = solveSubControlsRegion();
  assert.ok(
    subRegion.includes('class="comparison-sub-row view-option-group is-hidden" id="solveContourControls"'),
    'Contours controls should use the separated option group styling in the contextual row',
  );
  assert.ok(
    subRegion.includes('class="view-option-toggle is-active" id="solveContoursToggle"'),
    'Contours button should use the same option-toggle visual treatment as Source Image',
  );
  assert.ok(subRegion.includes('Contours: On'), 'Contours toggle should expose its On/Off state');
  assert.ok(
    APP_JS.includes('contoursToggle.textContent = `Contours: ${solveContoursEnabled ? "On" : "Off"}`;'),
    'Contours toggle text should stay synchronized with state',
  );
  assert.match(
    CSS,
    /\.comparison-sub-controls \.view-option-toggle\s*\{[\s\S]*?padding:\s*3px 8px;[\s\S]*?font-size:\s*11px;/,
    'contextual option toggles should be compact enough to sit with sub-control buttons',
  );
  assert.match(
    CSS,
    /\.comparison-sub-controls \.view-option-group\s*\{[\s\S]*?padding-left:\s*10px;[\s\S]*?margin-left:\s*4px;/,
    'contextual option divider should not crowd the button',
  );
});

test('Preview submode choices use segmented controls', () => {
  const subRegion = solveSubControlsRegion();
  for (const id of [
    'solveColorRegionsControls',
    'solveWhiteCapControls',
    'solveCapDiffControls',
  ]) {
    const start = subRegion.indexOf(`id="${id}"`);
    assert.ok(start >= 0, `missing ${id}`);
    const row = subRegion.slice(start, subRegion.indexOf('</div>', start + 1) + 6);
    assert.ok(
      row.includes('class="segmented-control preview-segmented-control"'),
      `${id} should use the shared segmented-control primitive`,
    );
    assert.ok(row.includes('class="segmented-btn'), `${id} should use segmented buttons`);
    assert.ok(row.includes('role="radiogroup"'), `${id} should expose a radiogroup`);
  }
  assert.equal(
    subRegion.includes('id="solveContourControls">\n                <span class="sub-label"'),
    false,
    'independent contour toggle should remain outside the segmented submode groups',
  );
  assert.match(
    CSS,
    /\.preview-segmented-control \.segmented-btn\s*\{[\s\S]*?min-width:\s*0;[\s\S]*?font-size:\s*11px;/,
    'preview segmented controls should use the compact shared primitive variant',
  );
});

test('Preview view and subview buttons have tooltips', () => {
  for (const [label, region] of [
    ['view bar', viewBarRegion()],
    ['sub-controls', solveSubControlsRegion()],
  ]) {
    const buttons = [...region.matchAll(/<button\b[^>]*>/g)].map((m) => m[0]);
    assert.ok(buttons.length > 0, `expected buttons in ${label}`);
    for (const button of buttons) {
      assert.match(button, /\btitle="[^"]+"/, `${label} button lacks tooltip: ${button}`);
    }
  }
  assert.match(solveSubControlsRegion(), /<select\b[^>]*id="solveFilamentDiffSelect"[^>]*title="[^"]+"/);
});

test('Preview subtabs expose a caption surface and caption copy', () => {
  assert.ok(INDEX_HTML.includes('id="solveViewCaption"'), 'preview should include a caption surface');
  const subRegion = solveSubControlsRegion();
  assert.ok(subRegion.includes('id="solveViewCaption"'), 'caption should live inside contextual controls');
  assert.ok(
    INDEX_HTML.indexOf('id="solveSubControls"') < INDEX_HTML.indexOf('id="solveViewCaption"')
      && INDEX_HTML.indexOf('id="solveViewCaption"') < INDEX_HTML.indexOf('id="solveColorRegionsControls"'),
    'caption should sit at the top of the contextual button cluster',
  );
  assert.match(CSS, /\.solve-view-caption\s*\{[\s\S]*?flex:\s*1 0 100%;[\s\S]*?font-size:\s*11px;/);
  assert.ok(APP_JS.includes('function getSolveViewCaption(view = solveView)'), 'caption helper should be view-driven');
  for (const text of [
    'Predicted Appearance',
    'Total White Cap',
    'Boundary Cap',
    'Detail Cap',
    'Color Regions',
    'Recipe Viewer',
    'Top Surface',
    'Surface Explorer',
    'Thickness Maps',
    'Surface Highpass',
    'Cap Diff',
    'Color Diff',
  ]) {
    assert.ok(APP_JS.includes(text), `caption copy missing: ${text}`);
  }
});

test('banded Color Regions defaults to the emitted recipe viewer, not its flat physical ceiling', () => {
  assert.ok(APP_JS.includes('function isBandedSolveRun(run)'), 'expected banded-run helper');
  assert.ok(APP_JS.includes('function hasRecipeViewerArtifacts(run)'), 'expected recipe-artifact helper');
  assert.ok(APP_JS.includes('function shouldDefaultColorRegionsToRecipe()'), 'expected Color Regions default helper');
  assert.match(
    APP_JS,
    /requestedView === "color_ceiling"[\s\S]*?!solveColorRegionsViewWasExplicitlySelected[\s\S]*?shouldDefaultColorRegionsToRecipe\(\)[\s\S]*?solveColorRegionsView = "recipe_regions"/,
    'entering Color Regions should prefer the recipe viewer for a banded run with recipe artifacts',
  );
  assert.match(
    APP_JS,
    /return `flat \$\{result\.color_ceiling_max_d\?\.toFixed\(2\) \|\| "0\.00"\} mm`;/,
    'the physical ceiling card should report that a banded ceiling is flat',
  );
});
