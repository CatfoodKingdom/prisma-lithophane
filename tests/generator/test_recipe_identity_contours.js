"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { createFeatureHarness } = require("./support/application_harness.cjs");

let app;
test.before(async () => { ({ app } = await createFeatureHarness()); });

test("recipe contours separate equal-height recipes and merge duplicate stack identities", () => {
  const chestnutOnly = [{ filament_id: "chestnut-brown", thickness_mm: 0.16 }];
  const mixed = [
    { filament_id: "chestnut-brown", thickness_mm: 0.08 },
    { filament_id: "blue-gray", thickness_mm: 0.08 },
  ];
  const keys = [
    app.commands._recipeKeyFromEntries(chestnutOnly),
    app.commands._recipeKeyFromEntries(mixed),
    app.commands._recipeKeyFromEntries(chestnutOnly),
    app.commands._recipeKeyFromEntries([{ filament_id: "ochre", thickness_mm: 0.16 }]),
  ];
  const labels = {
    width: 3, height: 3,
    data: new Uint32Array([0, 0, 1, 0, 2, 1, 3, 2, 1]),
  };
  const identity = app.commands.buildRecipeIdentityMap(labels, keys);
  const boundaries = app.commands.buildDiscreteLabelBoundaryMask(identity);
  assert.equal(identity.data[0], identity.data[4]);
  assert.notEqual(identity.data[0], identity.data[2]);
  assert.deepEqual(Array.from(boundaries.vertical), [0, 1, 0, 1, 1, 1]);
  assert.deepEqual(Array.from(boundaries.horizontal), [0, 0, 0, 1, 0, 0]);
});

test("diagonal contact creates no diagonal recipe connector", () => {
  const boundaries = app.commands.buildDiscreteLabelBoundaryMask({
    width: 2, height: 2, data: new Uint32Array([0, 1, 2, 0]),
  });
  assert.deepEqual(Array.from(boundaries.vertical), [1, 1]);
  assert.deepEqual(Array.from(boundaries.horizontal), [1, 1]);
});

test("recipe cache invalidation is run-scoped and generation-aware", () => {
  const run = { id: "run-a", results: { color_ceiling_bin_url: "/a.bin" } };
  app.state.ui.recipeDataCache[run.id] = { cached: true };
  app.state.ui.recipeDataPromiseCache[run.id] = Promise.resolve({});
  app.state.ui.recipeCookbookPromiseCache[run.id] = Promise.resolve({});
  app.state.solve.solveContourDataCache["/a.bin"] = { cached: true };
  app.commands.invalidateSolveRunCaches(run);
  assert.equal(app.state.ui.recipeDataCache[run.id], undefined);
  assert.equal(app.state.ui.recipeDataPromiseCache[run.id], undefined);
  assert.equal(app.state.ui.recipeCookbookPromiseCache[run.id], undefined);
  assert.equal(app.state.ui.recipeDataGeneration[run.id], 1);
  assert.equal(app.state.solve.solveContourDataCache["/a.bin"], undefined);
});

test("recipe contours dispatch independently from scalar height contours", () => {
  const result = {
    color_ceiling_bin_url: "/height.bin",
    explorer_stack_label_bin_url: "/recipes.bin",
    explorer_stack_table: [[]],
  };
  assert.equal(app.commands.getSolveContourUrl(result, "recipe_regions"), "");
  assert.equal(app.commands.getSolveContourUrl(result, "color_ceiling"), "/height.bin");
  assert.equal(app.commands.hasRecipeContourArtifacts({ results: result }), true);
});

test("recipe keys preserve filament, thickness, and band identity", () => {
  const first = app.commands._recipeKeyFromEntries([
    { filament_id: "red", thickness_mm: 0.08, band_index: 0 },
    { filament_id: "blue", thickness_mm: 0.08, band_index: 1 },
  ]);
  const reordered = app.commands._recipeKeyFromEntries([
    { filament_id: "blue", thickness_mm: 0.08, band_index: 1 },
    { filament_id: "red", thickness_mm: 0.08, band_index: 0 },
  ]);
  const differentBand = app.commands._recipeKeyFromEntries([
    { filament_id: "red", thickness_mm: 0.08, band_index: 1 },
    { filament_id: "blue", thickness_mm: 0.08, band_index: 0 },
  ]);
  assert.equal(first, reordered);
  assert.notEqual(first, differentBand);
});
