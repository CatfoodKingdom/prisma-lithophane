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
const css = fs.readFileSync(
  path.join(root, "Prisma", "calibration", "app", "style.css"),
  "utf8",
);

function section(startText, endText) {
  const start = source.indexOf(startText);
  const end = source.indexOf(endText, start + startText.length);
  assert.notEqual(start, -1, `${startText} must exist`);
  assert.notEqual(end, -1, `${startText} must have a stable end marker`);
  return source.slice(start, end);
}

test("backup and restore jobs share sequential identity-checked polling", () => {
  const backupDialog = section("function showBackupRestoreDialog()", "function showModelPublicationDialog");
  const restore = section("function showRestoreBackupWorkflow()", "function showRawArchiveRestoreWorkflow()");
  assert.match(backupDialog, /function pollBackupWorkflowJob/);
  assert.match(backupDialog, /pollJobUntilTerminal\(\{/);
  assert.match(backupDialog, /fetchStatus: \(\) => fetchBackupJobStatus\(jobId\)/);
  assert.match(restore, /pollBackupWorkflowJob\(host, workflowState, jobId, "restore"\)/);
  assert.doesNotMatch(restore, /while \(host\.isConnected\(\)\)/);
});

test("restore validation uses honest indeterminate progress", () => {
  const renderer = section("function operationProgressHtml", "function backupErrorMessage");
  const restore = section("function showRestoreBackupWorkflow()", "function showRawArchiveRestoreWorkflow()");
  assert.match(renderer, /progress\.indeterminate === true/);
  assert.match(renderer, /indeterminate \? "Working"/);
  assert.match(renderer, /backup-progress-fill\$\{indeterminate \? " is-indeterminate"/);
  assert.match(restore, /message: "Validating backup\.\.\."/);
  assert.match(restore, /progress: \{ indeterminate: true \}/);
  assert.match(css, /\.backup-progress-fill\.is-indeterminate/);
});

test("re-extraction and Maintenance use the shared polling contract", () => {
  const reextract = section("async function runReextractJob", "async function handleTerminalReextractJob");
  const maintenance = section("async function runJob()", "async function runPreflight()");
  assert.match(reextract, /pollJobUntilTerminal\(\{/);
  assert.match(reextract, /jobId,/);
  assert.match(reextract, /overlay\.isConnected && state\.running/);
  assert.doesNotMatch(reextract, /while \(/);
  assert.match(maintenance, /pollJobUntilTerminal\(\{/);
  assert.match(maintenance, /fetchMaintenanceJobStatus\(jobId\)/);
  assert.match(maintenance, /Connection interrupted; retrying maintenance status/);
  assert.doesNotMatch(maintenance, /while \(/);
});

test("Inbox import keeps transport failures nonterminal and polling sequential", () => {
  const dialog = section("function showImportProgressDialog()", "async function handleImportInboxImages()");
  assert.match(dialog, /pollJobUntilTerminal\(\{/);
  assert.match(dialog, /Connection interrupted; retrying import status/);
  assert.match(dialog, /actionError/);
  assert.doesNotMatch(dialog, /pollTimer|schedulePoll|setTimeout\(poll/);
  assert.doesNotMatch(dialog, /state\.startError = err\.message \|\| "Could not check import progress/);
});

test("manual re-extraction uses sequential polling and scoped cancellation", () => {
  const extract = section("async function _handleManualExtract()", "async function _handleManualAccept()");
  const cancel = section("if (mpCancelBtn) {", "if (mpAcceptBtn)");
  assert.match(extract, /pollJobUntilTerminal\(\{/);
  assert.match(extract, /_manualProc\.currentJobId === jobId/);
  assert.doesNotMatch(extract, /while \(/);
  assert.match(cancel, /const cancellationJobId = _manualProc\.currentJobId/);
  assert.match(cancel, /assertPolledJobIdentity\(response, cancellationJobId\)/);
});

test("Calibration cancellation responses are checked before replacing current state", () => {
  const reextract = section("async function cancelActiveReextractJob()", "function confirmDeleteCandidateSetDialog()");
  const maintenance = section(
    'overlay.querySelector("#maintenanceWorkflowCancelJob")',
    "function scrollMaintenanceWorkflowToResult()",
  );
  const inbox = section("const requestCancel = async () =>", "const handleKeydown");
  for (const handler of [reextract, maintenance, inbox]) {
    assert.match(handler, /const cancellationJobId/);
    assert.match(handler, /assertPolledJobIdentity\(response, cancellationJobId\)/);
  }
});
