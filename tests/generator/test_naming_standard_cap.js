const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const APP_DIR = path.resolve(__dirname, '../../Prisma/generator/app');
const APP_JS = fs.readFileSync(path.join(APP_DIR, 'app.js'), 'utf8');
const INDEX_HTML = fs.readFileSync(path.join(APP_DIR, 'index.html'), 'utf8');

test('boundary cap labels use current Detail Aware and Smooth product names', () => {
  // The implementation-flavored mode NAME is gone from every user-facing surface.
  assert.ok(!/Smoothed contiguous cap/i.test(APP_JS), 'app.js must not show "Smoothed contiguous cap"');
  assert.ok(!/Smoothed contiguous cap/i.test(INDEX_HTML), 'index.html must not show "Smoothed contiguous cap"');
  // User-facing labels should describe the current product behavior.
  assert.ok(
    INDEX_HTML.includes('<option value="appearance_bounded_smooth">Detail Aware</option>'),
    'the appearance-bounded cap option should be labeled "Detail Aware"',
  );
  assert.ok(
    INDEX_HTML.includes('<option value="smooth_variable">Smooth</option>'),
    'the smooth_variable cap option should be labeled "Smooth"',
  );
  // The backend config value is unchanged.
  assert.ok(INDEX_HTML.includes('value="smooth_variable"'), 'the smooth_variable config value is preserved');
});

test('detail-aware boundary cap is present and default while fixed cap remains retired', () => {
  assert.ok(
    INDEX_HTML.includes('<option value="appearance_bounded_smooth">Detail Aware</option>'),
    'Detail Aware boundary cap option should be available',
  );
  assert.ok(
    /value === "appearance_bounded_smooth" \? "Detail Aware" : "Smooth"/.test(APP_JS),
    'living settings-value formatting should label the Detail Aware cap mode',
  );
  assert.ok(
    /cap_mode:\s*"appearance_bounded_smooth"/.test(APP_JS),
    'initial cap mode should default to Detail Aware',
  );
  assert.ok(
    !INDEX_HTML.includes('value="fixed"'),
    'fixed thickness cap option should not be user-facing',
  );
  assert.ok(
    !INDEX_HTML.includes('cfgCapFixedThickness'),
    'fixed thickness input should not be user-facing',
  );
});
