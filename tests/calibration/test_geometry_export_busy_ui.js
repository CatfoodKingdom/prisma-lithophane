"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..", "..");
const source = fs.readFileSync(
  path.join(root, "Prisma", "calibration", "app", "app.js"),
  "utf8",
);

function geometryExportSource() {
  const start = source.indexOf("function openGeometryExportDialog(geometryId, alias = \"\") {");
  const end = source.indexOf("function populateStepValues", start);
  assert.notEqual(start, -1, "Geometry export dialog must exist");
  assert.notEqual(end, -1, "Geometry export dialog must have a stable boundary");
  return source.slice(start, end);
}

test("direct geometry export blocks every dismiss path while its request is active", () => {
  const dialog = geometryExportSource();
  assert.match(dialog, /let busy = false/);
  assert.match(dialog, /if \(busy && !force\) return false/);
  assert.match(dialog, /if \(event\.key === "Escape"\) cleanup\(\)/);
  assert.match(dialog, /if \(event\.target === overlay\) cleanup\(\)/);
  assert.match(dialog, /querySelectorAll\("button, input"\)/);
  assert.match(dialog, /cleanup\(\{ force: true \}\)/);
});

test("a second geometry export cannot remove a busy export dialog", () => {
  const dialog = geometryExportSource();
  assert.match(dialog, /if \(activeGeometryExportDialogCleanup\?\.\(\) === false\) return/);
});

test("overwrite confirmation retains the parent dialog's busy guard", () => {
  const dialog = geometryExportSource();
  const conflictStart = dialog.indexOf("const conflict = !overwrite");
  const retryEnd = dialog.indexOf("return generateGeometryArtifacts", conflictStart);
  const conflictBranch = dialog.slice(conflictStart, retryEnd);
  assert.doesNotMatch(conflictBranch, /setBusy\(false\)/);
});

test("geometry export failure restores controls and success closes authoritatively", () => {
  const dialog = geometryExportSource();
  assert.match(dialog, /catch \(err\) \{[\s\S]*Export failed[\s\S]*\} finally \{\s*setBusy\(false\)/);
  assert.match(dialog, /await handleRefresh\(\);\s*cleanup\(\{ force: true \}\)/);
});
