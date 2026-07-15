"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const repoRoot = path.resolve(__dirname, "..", "..");
const appSource = fs.readFileSync(path.join(repoRoot, "Prisma", "generator", "app", "app.js"), "utf8");
const apiSource = fs.readFileSync(path.join(repoRoot, "Prisma", "generator", "app", "api.js"), "utf8");

function section(startText, endText) {
  const start = appSource.indexOf(startText);
  const end = appSource.indexOf(endText, start + startText.length);
  assert.notEqual(start, -1, `${startText} must exist`);
  assert.notEqual(end, -1, `${startText} must have a stable end marker`);
  return appSource.slice(start, end);
}

test("export cancellation retains polling state and restores Cancel after request failure", () => {
  const source = section("async function requestExportCancellation()", "function cancelProgress()");
  assert.match(source, /exportCancelPending = true;/);
  assert.match(source, /const cancellationJobId = activeExportJobId;/);
  assert.match(source, /await cancelExport\(cancellationJobId\)/);
  assert.match(source, /assertPolledJobIdentity\(response, cancellationJobId\)/);
  assert.match(source, /catch \(err\) \{[\s\S]*exportCancelPending = false;[\s\S]*renderExportCancellationState\(\);/);
  assert.doesNotMatch(source, /stopProgress\(\)/);
  assert.doesNotMatch(source, /activeExportJobId = ""/);
});

test("export polling treats cancelling as active and validates job identity", () => {
  const source = section("async function handleExportFiles()", "// ── Module Settings Renderer");
  assert.match(source, /activeExportJobId = String\(started\?\.job_id \|\| ""\);/);
  assert.match(source, /pollJobUntilTerminal\(\{/);
  assert.match(source, /jobId: pollingJobId/);
  assert.match(source, /!\["running", "cancelling"\]\.includes\(next\.status\)/);
  assert.match(source, /cancelled\.name = "AbortError";/);
});

test("export Cancel remains disabled until a job id can scope the request", () => {
  const renderSource = section("function renderExportCancellationState()", "async function requestExportCancellation()");
  const handlerSource = section("async function handleExportFiles()", "// ── Module Settings Renderer");
  assert.match(renderSource, /cancelBtn\.disabled = !activeExportJobId \|\| exportCancelPending/);
  assert.match(handlerSource, /activeExportJobId = String\(started\?\.job_id \|\| ""\);[\s\S]*renderExportCancellationState\(\);/);
});

test("export Cancel API carries the active job id", () => {
  assert.match(apiSource, /async function cancelExport\(jobId = ''\)/);
  assert.match(apiSource, /job_id=\$\{encodeURIComponent\(jobId\)\}/);
});
