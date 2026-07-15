// Behavioral gate for the recipe-bucket tail cutoff.
//
// Unlike the source-string gates in test_recipe_viewer.js, this one exercises
// the REAL partition logic: it pulls the pure `partitionRecipeTail` helper out
// of app.js (a browser script that can't be require()d wholesale) by
// brace-matching its definition, then runs it. The partition decision — which
// recipes show outright vs collapse into the "<0.1%" expander — is the one
// genuinely-fallible piece here, so it gets a true behavioral test.

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const APP_JS = fs.readFileSync(
  path.resolve(__dirname, '../../Prisma/generator/app/app.js'),
  'utf8',
);

// Extract a single pure function from the source by name + brace balance.
// Safe here because partitionRecipeTail is plain array math with no braces in
// strings/regex/comments.
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
  const src = source.slice(start, end);
  // eslint-disable-next-line no-eval
  return eval(`(${src})`);
}

const partitionRecipeTail = extractFn(APP_JS, 'partitionRecipeTail');

test('no tail when every recipe carries real area', () => {
  const f = [0.4, 0.3, 0.2, 0.1]; // all >= 0.1% of the image
  const r = partitionRecipeTail(f);
  assert.equal(r.visible, 4);
  assert.equal(r.tailCount, 0);
  assert.equal(r.tailFraction, 0);
});

test('a long sub-0.1% tail collapses while above-threshold rows stay', () => {
  const above = [0.30, 0.05, 0.02, 0.005, 0.0012]; // 5 rows >= 0.001
  const tail = Array.from({ length: 431 }, () => 0.0001); // 431 rows < 0.001
  const r = partitionRecipeTail(above.concat(tail));
  assert.equal(r.visible, 5);
  assert.equal(r.tailCount, 431);
  assert.ok(Math.abs(r.tailFraction - 431 * 0.0001) < 1e-9);
});

test('a tiny all-sub-threshold combo shows its rows, not an empty table', () => {
  const r = partitionRecipeTail([0.0005, 0.0004]); // both < 0.001, only 2 rows
  assert.equal(r.visible, 2);
  assert.equal(r.tailCount, 0);
});

test('a large all-sub-threshold combo shows the min floor, collapses the rest', () => {
  const r = partitionRecipeTail(Array.from({ length: 50 }, () => 0.0002));
  assert.equal(r.visible, 3); // minVisible default
  assert.equal(r.tailCount, 47);
});

test('a 1-row tail is shown inline, not hidden behind an expander', () => {
  const f = [0.30, 0.05, 0.02, 0.005, 0.0012, 0.0009]; // 5 above, 1 below
  const r = partitionRecipeTail(f);
  assert.equal(r.visible, 6);
  assert.equal(r.tailCount, 0);
});

test('a single recipe never has a tail', () => {
  const r = partitionRecipeTail([0.0003]);
  assert.equal(r.visible, 1);
  assert.equal(r.tailCount, 0);
});

test('threshold and minVisible are configurable', () => {
  const f = [0.5, 0.05, 0.005, 0.0005];
  const r = partitionRecipeTail(f, { threshold: 0.01, minVisible: 1 });
  assert.equal(r.visible, 2); // 0.5 + 0.05 clear 1%
  assert.equal(r.tailCount, 2); // 0.005 + 0.0005 collapse
});

test('an empty recipe set partitions to nothing', () => {
  const r = partitionRecipeTail([]);
  assert.equal(r.visible, 0);
  assert.equal(r.tailCount, 0);
  assert.equal(r.tailFraction, 0);
});
