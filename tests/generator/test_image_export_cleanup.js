const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const APP_JS = fs.readFileSync(
  path.resolve(__dirname, '../../Prisma/generator/app/app.js'),
  'utf8',
);
const INDEX_HTML = fs.readFileSync(
  path.resolve(__dirname, '../../Prisma/generator/app/index.html'),
  'utf8',
);

test('S11: image selection defaults the frame to the source aspect (120mm short side)', () => {
  assert.ok(APP_JS.includes('function applyImageAspectDefault()'), 'applyImageAspectDefault helper must exist');
  assert.ok(
    /IMAGE_ASPECT_SHORT_SIDE_MM\s*=\s*120/.test(APP_JS),
    'the short-side pin must be 120mm',
  );
  // The helper sets image aspect mode and derives the long side from the short side.
  const helper = APP_JS.slice(
    APP_JS.indexOf('function applyImageAspectDefault()'),
    APP_JS.indexOf('function applyImageAspectDefault()') + 700,
  );
  assert.ok(helper.includes('frameState.arMode = "image"'), 'helper must switch to image aspect mode');
  assert.ok(
    helper.includes('IMAGE_ASPECT_SHORT_SIDE_MM * ar') && helper.includes('IMAGE_ASPECT_SHORT_SIDE_MM / ar'),
    'helper must derive the long side from the short side for both orientations',
  );
});

test('S11: new-image selection paths call the aspect default (not a square)', () => {
  // Called from card-click, drop, and upload selection handlers.
  const calls = APP_JS.split('applyImageAspectDefault()').length - 1;
  assert.ok(calls >= 4, `aspect default must be applied on selection + reset (>=4 call sites, found ${calls})`);
});

test('S12a: export buttons are relabeled to clarify generate vs download', () => {
  assert.ok(INDEX_HTML.includes('>Generate Print Files</button>'), 'primary export button reads "Generate Print Files"');
  assert.ok(INDEX_HTML.includes('>Download .zip</button>'), 'download button reads "Download .zip"');
  assert.ok(
    INDEX_HTML.indexOf('id="downloadAllBtn"') > INDEX_HTML.indexOf('export-results-panel'),
    'zip download action should live with the generated exports surface',
  );
  assert.ok(!INDEX_HTML.includes('>Export Print Files</button>'), 'old "Export Print Files" label is gone');
  assert.ok(!INDEX_HTML.includes('>Download All</button>'), 'old "Download All" label is gone');
  // The post-export button-text restore matches the new label.
  assert.ok(APP_JS.includes('btn.textContent = "Generate Print Files"'), 'button text restore uses the new label');
});

test('generated file rows use explicit small actions instead of click-to-download filenames', () => {
  assert.ok(APP_JS.includes('<span class="file-name"'), 'file names should render as inert text');
  assert.ok(!APP_JS.includes('<a class="file-name"'), 'file names should not be download links');
  assert.ok(APP_JS.includes('class="ghost-button xxs export-file-download"'), 'each file should have an explicit small Download action');
  assert.ok(APP_JS.includes('>Copy Path</button>'), 'copy path actions should use title case');
  assert.ok(!APP_JS.includes('>Copy path</button>'), 'old lowercase file copy label should be gone');
  assert.ok(!APP_JS.includes('title="Copy folder path">Copy</button>'), 'folder copy action should use the full label');
});

test('high mesh detail export prompts before starting generation', () => {
  const start = APP_JS.indexOf('async function handleExportFiles()');
  const body = APP_JS.slice(start, start + 1400);
  assert.ok(body.includes('requestedPolicy.fieldScale > 4'), 'high detail warning should apply above 4x');
  assert.ok(body.includes('appConfirm('), 'high detail warning should use the in-app confirmation dialog');
  assert.ok(body.indexOf('appConfirm(') < body.indexOf('exportRunning = true'), 'confirmation should happen before export enters running state');
});

test('S12b: the Reload validation control is removed; flag defaults off', () => {
  assert.ok(!INDEX_HTML.includes('exportValidateWrittenMeshes'), 'Reload validation checkbox markup must be gone');
  assert.ok(!APP_JS.includes('exportValidateWrittenMeshes'), 'no JS should reference the removed checkbox');
  assert.ok(
    /const validateWrittenMeshes = false;/.test(APP_JS),
    'validate_written_meshes must default to false now the control is gone',
  );
});

test('export report summarizes manifest mesh quality instead of package file rows', () => {
  assert.ok(INDEX_HTML.includes('>Mesh Report</h4>'), 'the unified export surface should use the user-facing report title');
  assert.ok(!INDEX_HTML.includes('>Export Checks</p>'), 'export checks should not duplicate the panel title');
  assert.ok(APP_JS.includes('function renderExportChecks()'), 'export checks should have a dedicated renderer');
  assert.ok(
    APP_JS.includes('getSelectedExportResult()?.manifest?.quality'),
    'export checks should read object-level quality from the manifest',
  );
  assert.ok(
    APP_JS.includes('renderExportChecks();'),
    'post-export rendering should refresh the checks panel',
  );
  assert.ok(
    !APP_JS.includes('<th>File</th><th>Faces</th><th>Size (MB)</th><th>Status</th>'),
    'checks panel should not duplicate the generated-files table',
  );
});

test('export page uses a numbered primary workflow without a duplicate page title', () => {
  assert.ok(!INDEX_HTML.includes('<h3 class="tab-title">Export &amp; Print</h3>'), 'export page should not keep a redundant page title');
  assert.ok(INDEX_HTML.includes('class="export-step-badge">1</span>'), 'workflow starts with selecting a run');
  assert.ok(INDEX_HTML.includes('class="export-step-badge">3</span>'), 'workflow numbering reaches generated files');
  assert.ok(!INDEX_HTML.includes('class="export-step-badge">4</span>'), 'mesh checks should not be numbered as a workflow step');
  assert.ok(!INDEX_HTML.includes('class="export-step-badge">5</span>'), 'swap preview should not be numbered as a workflow step');
});
