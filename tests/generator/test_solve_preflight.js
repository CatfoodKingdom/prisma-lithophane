// Gate for the solve-start palette preflight.
//
// Behavioral: pulls the pure message builder out of app.js and runs it, so the
// excluded-vs-uncalibrated wording + de-duplication are actually exercised.
// Source-string: pins that handleStartSolve calls the /api/palette/validate
// precheck and blocks the solve on an invalid result.

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const APP_JS = fs.readFileSync(
  path.resolve(__dirname, '../../Prisma/generator/app/app.js'),
  'utf8',
);

// Extract a single function definition by name via brace balance (safe for the
// pure helpers here — no braces inside strings/regex).
function extractFn(source, name) {
  const sig = `function ${name}(`;
  const start = source.indexOf(sig);
  if (start === -1) throw new Error(`${name} not found in app.js`);
  const open = source.indexOf('{', start);
  let depth = 0;
  let end = open;
  for (; end < source.length; end++) {
    const c = source[end];
    if (c === '{') depth++;
    else if (c === '}') { depth--; if (depth === 0) { end++; break; } }
  }
  return source.slice(start, end);
}

// solveFilamentLabel references the global `allFilaments`; inject it as a param
// so the extracted builder resolves labels (empty list -> falls back to fid).
const builderSrc =
  extractFn(APP_JS, 'solveFilamentLabel') +
  '\n' +
  extractFn(APP_JS, 'buildUnsolvablePaletteMessage') +
  '\nreturn buildUnsolvablePaletteMessage;';
// eslint-disable-next-line no-new-func
const buildUnsolvablePaletteMessage = new Function('allFilaments', builderSrc)([]);

test('excluded filament message names it and gives the re-include remedy', () => {
  const msg = buildUnsolvablePaletteMessage({ valid: false, unavailable: ['bambu-translucent-orange'], missing: [] });
  assert.match(msg, /bambu-translucent-orange/);
  assert.match(msg, /excluded from the current color model/);
  assert.match(msg, /remove it/);
  assert.doesNotMatch(msg, /no calibration profile/);
});

test('uncalibrated filament message names it and says to calibrate', () => {
  const msg = buildUnsolvablePaletteMessage({ valid: false, unavailable: [], missing: ['elegoo-matte-sakura-pink'] });
  assert.match(msg, /elegoo-matte-sakura-pink/);
  assert.match(msg, /no calibration profile yet/);
  assert.match(msg, /calibrate it/);
  assert.doesNotMatch(msg, /excluded from the current color model/);
});

test('a filament that is both excluded and profile-less is reported once (as excluded)', () => {
  const msg = buildUnsolvablePaletteMessage({ valid: false, unavailable: ['orange'], missing: ['orange', 'pink'] });
  // "orange" appears under excluded, NOT also under the missing clause.
  assert.match(msg, /orange is excluded/);
  assert.match(msg, /pink has no calibration profile/);
  assert.equal((msg.match(/orange/g) || []).length, 1);
});

test('plural agreement for multiple excluded filaments', () => {
  const msg = buildUnsolvablePaletteMessage({ valid: false, unavailable: ['a', 'b'], missing: [] });
  assert.match(msg, /a, b are excluded/);
  assert.match(msg, /remove them/);
});

test('handleStartSolve preflights via /api/palette/validate and blocks invalid palettes', () => {
  assert.match(APP_JS, /apiPost\("\/palette\/validate", \{ palette \}\)/);
  assert.match(APP_JS, /check\.valid === false/);
  assert.ok(
    APP_JS.includes('buildUnsolvablePaletteMessage(check)'),
    'an invalid precheck must surface the clear unsolvable-palette message',
  );
});
