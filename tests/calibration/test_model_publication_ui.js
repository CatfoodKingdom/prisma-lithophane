"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

const appDir = path.resolve(__dirname, "../../Prisma/calibration/app");
const apiUrl = pathToFileURL(path.join(appDir, "api/index.js")).href;

test("model publication API preserves readiness, export, install, and folder contracts", async () => {
  const { createCalibrationApi } = await import(apiUrl);
  const requests = [];
  const api = createCalibrationApi({
    fetchImpl: async (url, options = {}) => {
      requests.push({ url, options });
      return { ok: true, json: async () => ({ ready: true }) };
    },
  });
  const metadata = { name: "Example", version: "1.2.3", publisher: "Prisma" };
  await api.fetchModelPublicationReadiness();
  await api.exportCurrentModelLibrary(metadata);
  await api.installCurrentModelLibrary(metadata);
  await api.openPublishedModelsFolder();

  assert.deepEqual(requests.map(({ url }) => url), [
    "/api/models/publication/readiness",
    "/api/models/publication/export",
    "/api/models/publication/install",
    "/api/models/publication/open-folder",
  ]);
  assert.equal(requests[0].options.method, undefined);
  assert.deepEqual(requests.slice(1).map(({ options }) => options.method), ["POST", "POST", "POST"]);
  assert.equal(requests[1].options.body, JSON.stringify(metadata));
  assert.equal(requests[2].options.body, JSON.stringify(metadata));
});

test("publication failures retain structured readiness detail", async () => {
  const { createCalibrationApi } = await import(apiUrl);
  const readiness = { ready: false, blockers: ["missing model"] };
  const api = createCalibrationApi({
    fetchImpl: async () => ({
      ok: false,
      status: 409,
      statusText: "Conflict",
      json: async () => ({ detail: { message: "not ready", readiness } }),
    }),
  });
  await assert.rejects(
    api.installCurrentModelLibrary({ name: "Example" }),
    (error) => error.status === 409 && error.detail.readiness === readiness,
  );
});
