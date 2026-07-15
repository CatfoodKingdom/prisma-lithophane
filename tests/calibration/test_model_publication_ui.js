const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..", "..");
const html = fs.readFileSync(path.join(root, "Prisma", "calibration", "app", "index.html"), "utf8");
const api = fs.readFileSync(path.join(root, "Prisma", "calibration", "app", "api.js"), "utf8");
const app = fs.readFileSync(path.join(root, "Prisma", "calibration", "app", "app.js"), "utf8");
const css = fs.readFileSync(path.join(root, "Prisma", "calibration", "app", "style.css"), "utf8");

test("Publish Models is a topbar utility with cache-busted assets", () => {
  assert.match(html, /id="publishModelsBtn">Publish Models<\/button>/);
  assert.match(html, /style\.css\?v=20260714-calibration-inbox-geometry-v8/);
  assert.match(html, /api\.js\?v=20260714-calibration-inbox-geometry-v8/);
  assert.match(html, /app\.js\?v=20260714-calibration-inbox-geometry-v8/);
});

test("publication API client covers readiness, both actions, and fixed folder access", () => {
  assert.match(api, /apiFetch\('\/models\/publication\/readiness'\)/);
  assert.match(api, /apiPost\('\/models\/publication\/export', metadata\)/);
  assert.match(api, /apiPost\('\/models\/publication\/install', metadata\)/);
  assert.match(api, /apiPost\('\/models\/publication\/open-folder', \{\}\)/);
});

test("dialog renders the established header, panels, status pills, and required metadata", () => {
  assert.match(app, /function showModelPublicationDialog\(\)/);
  assert.match(app, /renderDialogHeader\(\{/);
  assert.match(app, /backup-restore-panel model-publication-panel/);
  assert.match(app, /status-pill \$\{meta\.cls\}/);
  for (const id of [
    "modelPublicationName",
    "modelPublicationVersion",
    "modelPublicationPublisher",
    "modelPublicationDescription",
    "modelPublicationReleaseNotes",
  ]) {
    assert.match(app, new RegExp(`id="${id}"`));
  }
});

test("publication state stays authoritative across stale responses and successful actions", () => {
  assert.match(app, /if \(error\?\.detail\?\.readiness\) state\.readiness = error\.detail\.readiness/);
  assert.match(app, /state\.readiness = await fetchModelPublicationReadiness\(\)/);
  assert.match(app, /state\.result = response\?\.result \|\| null/);
  assert.match(app, /state\.working = "";\s*render\(\)/);
  assert.match(app, /const canPublish = ready && formComplete\(\) && !state\.loading && !isBusy\(\)/);
});

test("unsafe cancellation is not offered and closing is disabled while publishing", () => {
  assert.match(app, /if \(isBusy\(\)\) return;\s*overlay\.remove\(\)/);
  assert.match(app, /disabled: isBusy\(\)/);
  assert.doesNotMatch(app, /modelPublicationCancel/);
  assert.match(app, /Keep this window open until publication finishes/);
});

test("publication styling reuses app colors and provides responsive working states", () => {
  assert.match(css, /\.model-publication-dialog/);
  assert.match(css, /\.model-publication-readiness-summary\.is-ready/);
  assert.match(css, /\.model-publication-field input/);
  assert.match(css, /\.model-publication-progress-fill/);
  assert.match(css, /@keyframes modelPublicationProgress/);
  assert.match(css, /@media \(max-width: 760px\)/);
});
