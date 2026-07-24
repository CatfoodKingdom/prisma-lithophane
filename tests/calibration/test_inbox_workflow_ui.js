"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

const appDir = path.resolve(__dirname, "../../Prisma/calibration/app");
const apiUrl = pathToFileURL(path.join(appDir, "api/index.js")).href;

test("Image Inbox actions use backend-owned endpoints through the injected transport", async () => {
  const { createCalibrationApi } = await import(apiUrl);
  const requests = [];
  const api = createCalibrationApi({
    fetchImpl: async (url, options = {}) => {
      requests.push({ url, options });
      return { ok: true, json: async () => ({ accepted: true }) };
    },
  });

  await api.openImageInboxFolder();
  await api.startImportInboxImagesJob();
  await api.fetchImportInboxImagesJobStatus("job / 1");
  await api.cancelImportInboxImagesJob("job / 1");
  await api.cleanupUnusedImages();

  assert.deepEqual(requests.map(({ url }) => url), [
    "/api/images/open-inbox",
    "/api/images/import-inbox/start",
    "/api/images/import-inbox/status/job%20%2F%201",
    "/api/images/import-inbox/cancel/job%20%2F%201",
    "/api/images/cleanup-unused",
  ]);
  assert.deepEqual(requests.map(({ options }) => options.method || "GET"), ["POST", "POST", "GET", "POST", "POST"]);
});

test("CSV assignment validation sends the selected file as multipart form data", async () => {
  const { createCalibrationApi } = await import(apiUrl);
  const requests = [];
  const api = createCalibrationApi({
    fetchImpl: async (url, options = {}) => {
      requests.push({ url, options });
      return { ok: true, json: async () => ({ valid: true }) };
    },
  });
  const file = new Blob(["sample_id,sample_image"], { type: "text/csv" });
  const result = await api.validateSampleAssignmentCsv(file);
  assert.deepEqual(result, { valid: true });
  assert.equal(requests[0].url, "/api/samples/assignment-import/validate");
  assert.equal(requests[0].options.method, "POST");
  const uploaded = requests[0].options.body.get("file");
  assert.equal(uploaded.type, file.type);
  assert.equal(await uploaded.text(), await file.text());
  assert.equal(api.sampleAssignmentTemplateUrl(), "/api/samples/assignment-template.csv");
});
