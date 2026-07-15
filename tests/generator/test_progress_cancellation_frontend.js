"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const root = path.resolve(__dirname, "..", "..");
const source = fs.readFileSync(
  path.join(root, "Prisma", "generator", "app", "app.js"),
  "utf8",
);

function extractFunction(signature) {
  const start = source.indexOf(signature);
  assert.notEqual(start, -1, `Missing ${signature}`);
  const parameterEnd = source.indexOf(") {", start);
  assert.notEqual(parameterEnd, -1, `Missing function body for ${signature}`);
  const brace = parameterEnd + 2;
  let depth = 0;
  for (let index = brace; index < source.length; index += 1) {
    if (source[index] === "{") depth += 1;
    if (source[index] === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(start, index + 1);
    }
  }
  throw new Error(`Unterminated ${signature}`);
}

function classList() {
  const values = new Set(["is-hidden"]);
  return {
    add: (...items) => items.forEach((item) => values.add(item)),
    remove: (...items) => items.forEach((item) => values.delete(item)),
    contains: (item) => values.has(item),
  };
}

test("noncancelable shared progress hides and disables Cancel", () => {
  const fill = { className: "", style: {} };
  const overlay = {
    dataset: {},
    classList: classList(),
    querySelector: () => fill,
  };
  const label = { textContent: "" };
  const elapsed = { textContent: "" };
  const cancel = { hidden: false, disabled: false };
  const context = {
    _opAbort: null,
    _opStartTime: 0,
    _opLastElapsedSeconds: 0,
    _opTimer: null,
    AbortController: class { constructor() { this.signal = {}; } },
    Date,
    clearInterval: () => {},
    setInterval: () => 1,
    setOperationElapsedSeconds: () => {},
    _slowButtons: () => [],
    $: (selector) => ({
      "#opProgress": overlay,
      "#opProgressLabel": label,
      "#opProgressElapsed": elapsed,
      "#opProgressCancel": cancel,
    })[selector] || null,
  };
  vm.createContext(context);
  vm.runInContext(extractFunction("function startProgress"), context);

  context.startProgress("Generating...", "swap-instructions", { cancellable: false });

  assert.equal(overlay.dataset.owner, "swap-instructions");
  assert.equal(overlay.dataset.cancellable, "false");
  assert.equal(cancel.hidden, true);
  assert.equal(cancel.disabled, true);
  assert.equal(overlay.classList.contains("is-hidden"), false);
});

test("shared Cancel delegates suggestion cancellation without hiding progress", () => {
  let requests = 0;
  let stops = 0;
  let aborts = 0;
  const overlay = { dataset: { owner: "suggest", cancellable: "true" } };
  const context = {
    _opAbort: { abort: () => { aborts += 1; } },
    requestSuggestCancellation: () => { requests += 1; },
    handleCancelSolve: () => {},
    apiPost: async () => ({}),
    showToast: () => {},
    stopProgress: () => { stops += 1; },
    $: (selector) => selector === "#opProgress" ? overlay : null,
  };
  vm.createContext(context);
  vm.runInContext(extractFunction("function cancelProgress"), context);

  context.cancelProgress();

  assert.equal(requests, 1);
  assert.equal(stops, 0);
  assert.equal(aborts, 0);
});

test("suggestion cancellation stays pending until polling observes terminal state", async () => {
  const calls = [];
  const context = {
    _suggestPolling: 1,
    activeSuggestJobId: "suggest-1",
    suggestCancelPending: false,
    renderSuggestCancellationState: () => calls.push("render"),
    assertPolledJobIdentity: (response, jobId) => {
      if (response.job_id !== jobId) throw new Error("identity mismatch");
    },
    apiPost: async (url) => {
      calls.push(url);
      return { cancelled: true, job_id: "suggest-1" };
    },
    showToast: (message) => calls.push(message),
  };
  vm.createContext(context);
  vm.runInContext(extractFunction("async function requestSuggestCancellation"), context);

  await context.requestSuggestCancellation();

  assert.equal(context.suggestCancelPending, true);
  assert.deepEqual(calls, [
    "render",
    "/palette/suggest/cancel?job_id=suggest-1",
    "Suggestion cancellation requested",
  ]);
});

test("failed suggestion cancellation restores the available Cancel control", async () => {
  const calls = [];
  const context = {
    _suggestPolling: 1,
    activeSuggestJobId: "suggest-1",
    suggestCancelPending: false,
    renderSuggestCancellationState: () => calls.push("render"),
    apiPost: async () => { throw new Error("offline"); },
    showToast: (message, type) => calls.push(`${type}:${message}`),
  };
  vm.createContext(context);
  vm.runInContext(extractFunction("async function requestSuggestCancellation"), context);

  await context.requestSuggestCancellation();

  assert.equal(context.suggestCancelPending, false);
  assert.deepEqual(calls, [
    "render",
    "render",
    "error:Could not request suggestion cancellation: offline",
  ]);
});

test("swap instructions are owned by completed exports instead of a second live request", () => {
  assert.doesNotMatch(source, /function handleGenerateSwapInstructions/);
  assert.doesNotMatch(source, /addEventListener\("click", handleGenerateSwapInstructions\)/);
  assert.match(source, /exportRecord\?\.swapPlan\?\.instructions/);
});

test("solve progress explicitly reclaims its cancellation control", () => {
  const renderer = extractFunction("function renderSolveProgress");
  assert.match(renderer, /el\.dataset\.cancellable = "true"/);
  assert.match(renderer, /if \(cancelBtn\) cancelBtn\.hidden = false/);
});

test("suggestion polling owns the started job and survives a Cancel request", () => {
  const handler = extractFunction("async function handleSuggestPalettes");
  const cancel = extractFunction("async function requestSuggestCancellation");
  assert.match(handler, /activeSuggestJobId = started\?\.job_id \|\| null/);
  assert.match(handler, /pollJobUntilTerminal\(\{/);
  assert.match(handler, /jobId: pollingJobId/);
  assert.match(handler, /_suggestPolling === pollingOwner/);
  assert.match(handler, /suggestCancelPending = false/);
  assert.doesNotMatch(cancel, /clearInterval\(_suggestPolling\)/);
  assert.doesNotMatch(cancel, /stopProgress\(\)/);
});
