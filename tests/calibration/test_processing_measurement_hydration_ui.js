"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

const appDir = path.resolve(__dirname, "../../Prisma/calibration/app");
const moduleUrl = (relative) => pathToFileURL(path.join(appDir, relative)).href;
let api;
let commands;

test.before(async () => {
  const { createCalibrationApi } = await import(moduleUrl("api/index.js"));
  api = createCalibrationApi({ fetchImpl: async () => ({ ok: true, json: async () => ({}) }) });
  const { installFeaturesLogbookIndex } = await import(moduleUrl("features/logbook/index.js"));
  const app = {
    commands: {}, api, constants: {}, dom: {},
    state: { session: { data: { steps: [], filaments: [] } }, modeling: { profilesState: { profileCache: {} } }, logbook: {}, ui: {} },
  };
  installFeaturesLogbookIndex(app);
  commands = app.commands;
});

function rawSample(status) {
  return {
    sample_id: `sample-${status}`,
    has_measurements: false,
    processing_status: status,
    filaments: { variable: "filament-a", fixed: [] },
    strip_definition: { variable_thicknesses_mm: [0.2, 0.3] },
  };
}

test("processed review-pending samples retain processed output state", () => {
  for (const status of ["processed", "flagged"]) {
    const transformed = api.transformSampleToData(rawSample(status), {
      "filament-a": { color_name: "A", hex: "#123456" },
    });
    assert.equal(transformed.processed, true);
    assert.equal(transformed.strip_count, 1);
  }
  assert.equal(api.transformSampleToData(rawSample("assigned"), {}).processed, false);
});

test("measurement hydration eligibility follows workflow status", () => {
  assert.equal(commands.sampleHasMeasurementOutput({ processed: false, _processing_status: "processed" }), true);
  assert.equal(commands.sampleHasMeasurementOutput({ processed: false, _processing_status: "flagged" }), true);
  assert.equal(commands.sampleHasMeasurementOutput({ processed: false, _processing_status: "assigned" }), false);
});

test("measured display colors remain authoritative for post-processing mock swatches", () => {
  assert.deepEqual(
    commands.swatchDisplayDomain({ display: { hex: "#345678", R: 52, G: 86, B: 120 } }),
    { hex: "#345678", R: 52, G: 86, B: 120 },
  );
  assert.equal(commands.swatchDisplayDomain(null).hex, "");
});
