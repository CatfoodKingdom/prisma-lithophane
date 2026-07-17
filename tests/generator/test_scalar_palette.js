"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { createFeatureHarness } = require("./support/application_harness.cjs");

let app;
test.before(async () => { ({ app } = await createFeatureHarness()); });

test("frontend samples the canonical Inferno vector with half-up interpolation", () => {
  const inferno = (value) => app.commands.sampleScalarPalette(value, "inferno-v1");
  assert.deepEqual(inferno(0), [0, 0, 4]);
  assert.deepEqual(inferno(1 / 255), [1, 0, 5]);
  assert.deepEqual(inferno(64 / 255), [87, 16, 110]);
  assert.deepEqual(inferno(0.5), [187, 55, 85]);
  assert.deepEqual(inferno(128 / 255), [188, 55, 84]);
  assert.deepEqual(inferno(192 / 255), [249, 142, 9]);
  assert.deepEqual(inferno(1), [252, 255, 164]);
});

test("run provenance selects legacy or Inferno diagnostic rendering", () => {
  const legacyRun = { results: {} };
  const currentRun = { results: { diagnostic_palette_version: "inferno-v1" } };
  assert.equal(app.commands.getRunDiagnosticPaletteVersion(legacyRun), "legacy-approximate");
  assert.equal(app.commands.getRunDiagnosticPaletteVersion(currentRun), "inferno-v1");
  assert.deepEqual(app.commands.sampleScalarPalette(0, "legacy-approximate"), [68, 0, 83]);
  assert.deepEqual(app.commands.sampleScalarPalette(0, "inferno-v1"), [0, 0, 4]);
});

test("mixed diagnostic provenance produces a warning instead of one gradient", () => {
  app.commands.esc = (value) => String(value);
  const legacy = { results: {} };
  const current = { results: { diagnostic_palette_version: "inferno-v1" } };
  const infernoState = app.commands.getDiagnosticPaletteLegendState([current]);
  assert.equal(infernoState.mixed, false);
  assert.match(infernoState.gradient, /#000004/);
  assert.match(app.commands.diagnosticPaletteLegendHtml(infernoState), /data-diagnostic-palette="inferno-v1"/);

  const mixed = app.commands.getDiagnosticPaletteLegendState([legacy, current]);
  assert.equal(mixed.mixed, true);
  const html = app.commands.diagnosticPaletteLegendHtml(mixed);
  assert.match(html, /Mixed diagnostic palettes/);
  assert.doesNotMatch(html, /legend-bar/);
});

test("appearance and recipe views retain distinct result semantics", () => {
  const run = {
    results: {
      predicted_appearance_url: "/appearance.png",
      color_ceiling_url: "/regions.png",
    },
  };
  assert.equal(app.commands._getSolveRunResultUrl(run.results, "predicted"), "/appearance.png");
  assert.equal(app.commands.getSolveContourUrl(run, "recipe_regions"), "");
});
