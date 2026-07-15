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
const styleSource = fs.readFileSync(
  path.join(repoRoot, "Prisma", "calibration", "app", "style.css"),
  "utf8",
);

function managementLogbookSource() {
  const start = appSource.indexOf("function renderManagementLogbook() {");
  const end = appSource.indexOf("function renderFilamentLibrary() {", start);
  assert.notEqual(start, -1, "management Logbook renderer must exist");
  assert.notEqual(end, -1, "management Logbook renderer must have a stable boundary");
  return appSource.slice(start, end);
}

function rendererSource(startName, endName) {
  const start = appSource.indexOf(`function ${startName}() {`);
  const end = appSource.indexOf(`function ${endName}() {`, start);
  assert.notEqual(start, -1, `${startName} renderer must exist`);
  assert.notEqual(end, -1, `${startName} renderer must have a stable boundary`);
  return appSource.slice(start, end);
}

test("empty management Logbook renders a seven-column empty state", () => {
  const source = managementLogbookSource();
  assert.match(source, /<table class="data-table management-library-table">/);
  assert.match(source, /<tbody>\$\{rows \|\|/);
  assert.match(source, /colspan="7" class="empty-cell"/);
  assert.match(source, /No samples yet\. Use \+ New Samples/);
});

test("blank filament and geometry libraries use the same first-run treatment", () => {
  const filamentSource = rendererSource("renderFilamentLibrary", "renderStepLibrary");
  const geometrySource = rendererSource("renderStepLibrary", "renderProcessedData");

  for (const source of [filamentSource, geometrySource]) {
    assert.match(source, /<table class="data-table management-library-table">/);
    assert.match(source, /<tbody>\$\{rows \|\|/);
    assert.match(source, /colspan="6" class="empty-cell"/);
  }
  assert.match(filamentSource, /No filaments yet\. Use \+ New Filament/);
  assert.match(geometrySource, /No sample geometries yet\. Use \+ New Sample Geometry/);
});

test("management Logbook retains enough width for its tabs and columns", () => {
  assert.match(
    styleSource,
    /\.data-table\.management-library-table\s*\{[^}]*min-width:\s*min\(720px, calc\(100vw - 64px\)\);[^}]*\}/s,
  );
});
