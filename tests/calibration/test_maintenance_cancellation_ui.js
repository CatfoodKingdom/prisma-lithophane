"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const repoRoot = path.resolve(__dirname, "..", "..");
const appSource = fs.readFileSync(
  path.join(repoRoot, "Prisma", "calibration", "app", "app.js"),
  "utf8",
);

function maintenanceWorkflowSource() {
  const start = appSource.indexOf("function showMaintenanceWorkflow(operation, onComplete, options = {}) {");
  const end = appSource.indexOf("function showStepDeleteDialog", start);
  assert.notEqual(start, -1, "Maintenance workflow must exist");
  assert.notEqual(end, -1, "Maintenance workflow must have a stable boundary");
  return appSource.slice(start, end);
}

test("Maintenance workflow advertises API-provided cancellation capability", () => {
  const source = maintenanceWorkflowSource();
  assert.match(source, /operation\.cancellable \? "Cancelable between items" : "Runs to completion"/);
});

test("Maintenance Cancel follows authoritative job availability and requested state", () => {
  const source = maintenanceWorkflowSource();
  assert.match(source, /if \(!state\.running \|\| !state\.job\?\.cancellable\) return "";/);
  assert.match(source, /if \(!requested && !state\.job\?\.cancel_available\) return "";/);
  assert.match(source, /requested \? "Cancelling\.\.\." : "Cancel"/);
  assert.match(source, /if \(!state\.job\?\.job_id \|\| !state\.job\.cancel_available\) return;/);
});

test("terminal cancellation is not rendered as a workflow error", () => {
  const source = maintenanceWorkflowSource();
  assert.match(source, /state\.error = nextJob\.status === "failed"/);
  assert.doesNotMatch(source, /state\.error = nextJob\.error\?\.message \|\| nextJob\.message \|\| `Maintenance \$\{nextJob\.status\}`/);
});
