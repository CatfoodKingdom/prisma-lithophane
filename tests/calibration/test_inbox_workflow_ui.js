"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..", "..");
const html = fs.readFileSync(path.join(root, "Prisma", "calibration", "app", "index.html"), "utf8");
const api = fs.readFileSync(path.join(root, "Prisma", "calibration", "app", "api.js"), "utf8");
const app = fs.readFileSync(path.join(root, "Prisma", "calibration", "app", "app.js"), "utf8");
const css = fs.readFileSync(path.join(root, "Prisma", "calibration", "app", "style.css"), "utf8");

test("Image Inbox groups maintenance and primary actions without a count chip", () => {
  assert.match(html, /id="importInboxImportBtn">Import from Inbox<\/button>/);
  assert.match(html, /id="importInboxOpenFolderBtn">Open Inbox Folder<\/button>/);
  assert.match(html, /id="importInboxCleanupBtn">Clean Up Unused<\/button>/);
  assert.match(html, /id="importCsvAssignmentBtn">CSV Bulk Assignment<\/button>/);
  assert.doesNotMatch(html, /id="importInboxChip"/);
  assert.match(html, /import-inbox-maintenance-actions[\s\S]*import-inbox-primary-actions/);
  assert.match(css, /\.import-inbox-maintenance-actions[\s\S]*margin-right: 0/);
  assert.match(css, /\.import-inbox-panel > \.section-head[\s\S]*display: flex/);
  assert.match(css, /\.import-inbox-primary-actions[\s\S]*margin-left: auto/);
  assert.match(css, /\.import-inbox-maintenance-actions[\s\S]*flex-wrap: nowrap/);
  assert.match(css, /\.import-card-grid > \.small-copy[\s\S]*grid-column: 1 \/ -1/);
});

test("Inbox folder action uses the backend-owned local folder", () => {
  assert.match(api, /apiPost\('\/images\/open-inbox', \{\}\)/);
  assert.match(app, /async function handleOpenImageInboxFolder\(\)/);
  assert.match(app, /openImageInboxFolder\(\)/);
  assert.match(app, /importInboxOpenFolderBtn/);
  const bindStart = app.indexOf("function bindImportActionButtons()");
  const bindEnd = app.indexOf("function updateProcessingStatus", bindStart);
  const bindSource = app.slice(bindStart, bindEnd);
  assert.match(bindSource, /openFolderBtn\.addEventListener\("click", handleOpenImageInboxFolder\)/);
});

test("empty inbox guidance explains the visible folder and import step", () => {
  assert.match(app, /No images found in inbox\. Place images in the Inbox folder and click Import from Inbox\./);
  assert.match(html, /<div class="import-inbox-content">[\s\S]*import-inbox-caption[\s\S]*importImageGrid/);
  assert.match(html, /Place source and blank images in the Inbox folder, then import them into Calibration/);
});

test("CSV Bulk Assignment dialog gives the required import order and filename guidance", () => {
  assert.match(app, /title: "CSV Bulk Assignment"/);
  assert.match(app, /Place every sample image and blank image referenced by the CSV in the Calibration Inbox folder/);
  assert.match(app, /Click <strong>Import from Inbox<\/strong> before validating this CSV/);
  assert.match(app, /Use the exact filenames shown in the Inbox/);
  assert.match(css, /\.csv-assignment-instructions/);
});
