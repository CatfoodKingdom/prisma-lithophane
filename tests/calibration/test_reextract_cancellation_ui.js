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

function functionSource(startText, endText) {
  const start = appSource.indexOf(startText);
  const end = appSource.indexOf(endText, start + startText.length);
  assert.notEqual(start, -1, `${startText} must exist`);
  assert.notEqual(end, -1, `${startText} must have a stable boundary`);
  return appSource.slice(start, end);
}

test("re-extraction Cancel failure restores the actionable state while polling remains attached", () => {
  const source = functionSource(
    "async function cancelActiveReextractJob()",
    "function confirmDeleteCandidateSetDialog()",
  );
  assert.match(source, /state\.cancelling = state\.job\?\.status === "cancelling" \|\| Boolean\(state\.job\?\.cancel_requested\);/);
  assert.doesNotMatch(source, /state\.running = false/);
});

test("manual re-extraction cannot close and detach from a running job", () => {
  const source = functionSource(
    "function closeManualProcessing()",
    "function _resetManualCorners()",
  );
  assert.match(source, /if \(_manualProc\.currentJobId \|\| _manualProc\.processing\) return;/);
  assert.doesNotMatch(source, /cancelReextractJob/);
  assert.match(appSource, /mpCloseBtn\.disabled = _manualProc\.processing \|\| Boolean\(_manualProc\.currentJobId\);/);
});

test("manual re-extraction Cancel failure restores Cancel and leaves the job id intact", () => {
  const marker = 'mpCancelBtn.addEventListener("click", async () => {';
  const source = functionSource(marker, "if (mpAcceptBtn)");
  assert.match(source, /_manualProc\.cancelling = false;/);
  assert.match(source, /_updateManualProcUI\(\);/);
  assert.doesNotMatch(source, /_manualProc\.currentJobId = ""/);
});
