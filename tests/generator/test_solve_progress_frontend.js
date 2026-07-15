const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ROOT = path.resolve(__dirname, '..', '..');
const APP_JS = path.join(ROOT, 'Prisma', 'generator', 'app', 'app.js');
const SOURCE = fs.readFileSync(APP_JS, 'utf8');

function extractFunction(signature) {
  const start = SOURCE.indexOf(signature);
  assert.ok(start >= 0, `missing ${signature}`);
  const brace = SOURCE.indexOf('{', start);
  let depth = 0;
  for (let i = brace; i < SOURCE.length; i++) {
    if (SOURCE[i] === '{') depth++;
    if (SOURCE[i] === '}') {
      depth--;
      if (depth === 0) return SOURCE.slice(start, i + 1);
    }
  }
  throw new Error(`unterminated ${signature}`);
}

function classList() {
  const values = new Set();
  return {
    add: (...items) => items.forEach(item => values.add(item)),
    remove: (...items) => items.forEach(item => values.delete(item)),
    contains: item => values.has(item),
  };
}

function progressDom() {
  const fill = { className: '', style: {} };
  const attrs = new Map();
  const overlay = {
    classList: classList(),
    dataset: {},
    querySelector: () => fill,
    setAttribute: (key, value) => attrs.set(key, value),
    removeAttribute: key => attrs.delete(key),
  };
  const label = { textContent: '' };
  const elapsed = { textContent: '' };
  return { fill, attrs, overlay, label, elapsed };
}

test('solve progress bar renders overall percentage, not local stage percentage', () => {
  const dom = progressDom();
  const context = {
    solveStatus: {
      status: 'running',
      progress: 'Local work',
      elapsed_s: 9,
      progress_detail: {
        stage_label: 'Local work',
        stage_index: 4,
        stage_count: 7,
        stage_pct: 12,
        overall_pct: 41,
      },
    },
    solveCancelPending: false,
    solveProgressHideTimer: null,
    setOperationElapsedSeconds: seconds => { dom.elapsed.textContent = `${seconds}s`; },
    clearTimeout: () => {},
    setTimeout: () => 1,
    $: selector => ({
      '#opProgress': dom.overlay,
      '#opProgressLabel': dom.label,
      '#opProgressElapsed': dom.elapsed,
    })[selector] || null,
  };
  vm.createContext(context);
  vm.runInContext(extractFunction('function renderSolveProgress'), context);
  context.renderSolveProgress();

  assert.equal(dom.fill.style.width, '41%');
  assert.equal(dom.attrs.get('aria-valuenow'), '41');
  assert.equal(dom.label.textContent, 'Step 4/7: Local work');
  assert.equal(dom.elapsed.textContent, '9s');
});

test('completion renders 100 percent before the acknowledgement hides', () => {
  const dom = progressDom();
  dom.overlay.dataset.owner = 'solve';
  const cancel = { disabled: true };
  let hideDelay = null;
  let hideCallback = null;
  const context = {
    solveStatus: { status: 'complete' },
    solveCancelPending: false,
    solveProgressHideTimer: null,
    clearTimeout: () => {},
    setTimeout: (callback, delay) => {
      hideCallback = callback;
      hideDelay = delay;
      return 1;
    },
    $: selector => ({
      '#opProgress': dom.overlay,
      '#opProgressLabel': dom.label,
      '#opProgressElapsed': dom.elapsed,
      '#opProgressCancel': cancel,
    })[selector] || null,
  };
  vm.createContext(context);
  vm.runInContext(extractFunction('function renderSolveProgress'), context);
  context.renderSolveProgress();

  assert.equal(dom.fill.style.width, '100%');
  assert.equal(dom.attrs.get('aria-valuenow'), '100');
  assert.equal(dom.label.textContent, 'Solve complete');
  assert.equal(dom.overlay.classList.contains('is-hidden'), false);
  assert.equal(hideDelay, 700);
  hideCallback();
  assert.equal(dom.overlay.classList.contains('is-hidden'), true);
});

test('a cancelled solve cannot hide a newer running solve', () => {
  const dom = progressDom();
  dom.overlay.dataset.owner = 'solve';
  let hideCallback = null;
  const context = {
    solveStatus: { status: 'cancelled' },
    solveCancelPending: false,
    solveProgressHideTimer: null,
    clearTimeout: () => {},
    setTimeout: callback => {
      hideCallback = callback;
      return 1;
    },
    $: selector => ({
      '#opProgress': dom.overlay,
      '#opProgressLabel': dom.label,
      '#opProgressElapsed': dom.elapsed,
      '#opProgressCancel': { disabled: true },
    })[selector] || null,
  };
  vm.createContext(context);
  vm.runInContext(extractFunction('function renderSolveProgress'), context);
  context.renderSolveProgress();

  context.solveStatus = { status: 'running' };
  hideCallback();
  assert.equal(dom.overlay.classList.contains('is-hidden'), false);
});

test('shared operation progress retains stage percentage fallback', () => {
  const dom = progressDom();
  const context = {
    setOperationElapsedSeconds: seconds => { dom.elapsed.textContent = `${seconds}s`; },
    suggestCancelPending: false,
    renderSuggestCancellationState: () => {},
    renderExportCancellationState: () => {},
    $: selector => ({
      '#opProgress': dom.overlay,
      '#opProgressLabel': dom.label,
      '#opProgressElapsed': dom.elapsed,
    })[selector] || null,
  };
  vm.createContext(context);
  vm.runInContext(extractFunction('function updateOperationProgressFromStatus'), context);
  context.updateOperationProgressFromStatus({
    elapsed_s: 4,
    progress_detail: {
      stage_label: 'Exporting',
      stage_index: 2,
      stage_count: 3,
      stage_pct: 37,
    },
  });

  assert.equal(dom.fill.style.width, '37%');
  assert.equal(dom.label.textContent, 'Step 2/3: Exporting');
  assert.equal(dom.elapsed.textContent, '4s');
});

test('a new solve resets elapsed time inherited from the previous solve', () => {
  const elapsed = { textContent: '42s' };
  const context = {
    _opLastElapsedSeconds: 42,
    $: selector => selector === '#opProgressElapsed' ? elapsed : null,
  };
  vm.createContext(context);
  vm.runInContext(extractFunction('function resetOperationElapsedSeconds'), context);
  context.resetOperationElapsedSeconds();

  assert.equal(context._opLastElapsedSeconds, 0);
  assert.equal(elapsed.textContent, '0s');
  assert.match(extractFunction('async function handleStartSolve'), /resetOperationElapsedSeconds\(\)/);
});

test('cancel request remains pending and does not tear down polling state', async () => {
  const calls = [];
  const context = {
    solveCancelPending: false,
    solveStatus: { status: 'running' },
    activeSolveJobId: 'job-1',
    renderSolveProgress: () => calls.push('render'),
    assertPolledJobIdentity: (response, jobId) => {
      if (response.job_id !== jobId) throw new Error('identity mismatch');
    },
    cancelSolve: async () => ({ requested: true, job_id: 'job-1' }),
    showToast: message => calls.push(message),
  };
  vm.createContext(context);
  vm.runInContext(extractFunction('async function handleCancelSolve'), context);
  await context.handleCancelSolve();

  assert.equal(context.solveCancelPending, true);
  assert.deepEqual(calls, ['render', 'Cancellation requested']);
  assert.equal(/clearInterval\(solvePollingTimer\)/.test(extractFunction('async function handleCancelSolve')), false);
});

test('cancel race does not claim an unaccepted request', async () => {
  const calls = [];
  const context = {
    solveCancelPending: false,
    solveStatus: { status: 'running' },
    activeSolveJobId: 'job-1',
    renderSolveProgress: () => calls.push('render'),
    assertPolledJobIdentity: () => {},
    cancelSolve: async () => ({ cancelled: false, reason: 'not running' }),
    showToast: message => calls.push(message),
  };
  vm.createContext(context);
  vm.runInContext(extractFunction('async function handleCancelSolve'), context);
  await context.handleCancelSolve();

  assert.equal(context.solveCancelPending, false);
  assert.deepEqual(calls, ['render', 'render']);
});

test('polling rejects stale job responses and initialization resumes running solves', () => {
  const polling = extractFunction('function startSolvePolling');
  assert.match(polling, /const pollingJobId = activeSolveJobId/);
  assert.match(polling, /pollJobUntilTerminal\(\{/);
  assert.match(polling, /jobId: pollingJobId/);
  assert.match(polling, /activeSolveJobId === pollingJobId/);
  assert.doesNotMatch(polling, /setInterval/);
  assert.match(SOURCE, /session\.solve\.status === "running"/);
  assert.match(SOURCE, /startSolvePolling\(recoveredRun\)/);
});
