"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..", "..");
const api = fs.readFileSync(path.join(root, "Prisma", "calibration", "app", "api.js"), "utf8");
const app = fs.readFileSync(path.join(root, "Prisma", "calibration", "app", "app.js"), "utf8");

test("processed review-pending samples retain processed output state", () => {
  assert.match(api, /const workflowStatus = String\(sample\.processing_status \|\| ''\)\.toLowerCase\(\)/);
  assert.match(api, /workflowStatus === 'processed' \|\| workflowStatus === 'flagged'/);
});

test("measurement hydration follows workflow status instead of accepted-only summary", () => {
  assert.match(app, /function sampleHasMeasurementOutput\(exp = \{\}\)/);
  assert.match(app, /workflowStatus === "processed" \|\| workflowStatus === "flagged"/);
  const hydrationStart = app.indexOf("function ensureMeasurementsThenRerender");
  const hydrationEnd = app.indexOf("function renderProcessingDashboard", hydrationStart);
  const hydrationSource = app.slice(hydrationStart, hydrationEnd);
  assert.match(hydrationSource, /sampleHasMeasurementOutput\(exp\)/);
});

test("post-processing mock swatches still use measured display colors after hydration", () => {
  const renderStart = app.indexOf("function renderPostProcessingCard");
  const renderEnd = app.indexOf("// ── Lazy per-swatch measurement hydration", renderStart);
  const renderSource = app.slice(renderStart, renderEnd);
  assert.match(renderSource, /swatchDisplayDomain\(sw\)\.hex/);
  assert.match(renderSource, /const bg = displayHex \|\| hex/);
});
