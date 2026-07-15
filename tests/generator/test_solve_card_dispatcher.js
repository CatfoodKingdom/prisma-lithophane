const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const APP_JS = fs.readFileSync(
  path.resolve(__dirname, '../../Prisma/generator/app/app.js'),
  'utf8',
);

test('S4a: a single card lightbox dispatcher exists and handles every card kind', () => {
  assert.ok(
    APP_JS.includes('function openSolveCardLightboxFromElement(card)'),
    'openSolveCardLightboxFromElement dispatcher must exist',
  );
  for (const kind of ['source', 'run', 'surface', 'thickness']) {
    assert.ok(
      APP_JS.includes(`kind === "${kind}"`),
      `dispatcher must branch on kind === "${kind}"`,
    );
  }
});

test('S4a: the three divergent per-column click handlers are gone', () => {
  assert.ok(
    !APP_JS.includes('grid.querySelectorAll(".solve-grid-column").forEach'),
    'no per-render .solve-grid-column click-wiring loop should remain',
  );
  assert.ok(
    !APP_JS.includes('openSurfaceLightbox(view, runId)'),
    'the old direct normal-grid surface-lightbox call must be removed (now via dispatcher)',
  );
});

test('S4a: both result grids delegate clicks to the shared dispatcher', () => {
  assert.ok(
    APP_JS.includes('closest(".solve-grid-column[data-solve-card-kind]")'),
    'the comparison grid must delegate via data-solve-card-kind',
  );
  assert.ok(
    APP_JS.includes('closest(".filament-map-card[data-solve-card-kind]")'),
    'the thickness-map grid must delegate via data-solve-card-kind',
  );
});

test('S4a: cards carry the data-solve-card-kind contract', () => {
  // Literal kinds emitted directly in markup.
  assert.ok(APP_JS.includes('data-solve-card-kind="source"'), 'source card kind');
  assert.ok(APP_JS.includes('data-solve-card-kind="diff"'), 'diff card kind');
  assert.ok(APP_JS.includes('data-solve-card-kind="thickness"'), 'thickness card kind');
  // Run vs surface is resolved at build time for normal result columns.
  assert.ok(
    APP_JS.includes('isSolveSurfaceView(view) ? "surface" : "run"'),
    'run/surface card kind must be derived from the view',
  );
});

test('S4a: source card carries its run id; updateSolveColumnImages skips source', () => {
  assert.ok(
    APP_JS.includes('is-source" data-solve-card-kind="source" data-run-id='),
    'source column must expose its run id so the dispatcher can open the right source lightbox',
  );
  assert.ok(
    APP_JS.includes('.solve-grid-column[data-run-id]:not(.is-source)'),
    'updateSolveColumnImages must exclude the source card so its stats placeholder is not clobbered',
  );
});

test('S4a: source card uses the shared result image contract', () => {
  assert.ok(
    APP_JS.includes('data-source-target-kind="${esc(targetKind)}"'),
    'source card must declare the lightbox target kind for arrow-key navigation',
  );
  assert.ok(
    APP_JS.includes('class="solve-grid-img solve-grid-result-img" data-run-id="${esc(run.id)}" src="${esc(srcUrl)}"'),
    'source card image should use the same result-image class and escaping as solve preview cards',
  );
});

test('S4a: source lightbox uses the shared solve lightbox pane and sizing', () => {
  const start = APP_JS.indexOf('function openSolveSourceLightbox');
  assert.notEqual(start, -1, 'source lightbox opener must exist');
  const body = APP_JS.slice(start, APP_JS.indexOf('// ── Surface lightbox', start));
  assert.match(body, /buildSolveLightboxHeader\(run, "source"\)/);
  assert.match(body, /class="comp-lightbox-pane"/);
  assert.match(body, /class="comp-lightbox-img"/);
  assert.match(body, /min-width:min\(70vh, 85vw\);/);
});
