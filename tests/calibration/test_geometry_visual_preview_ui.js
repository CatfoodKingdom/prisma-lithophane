"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

let geometry;

test.before(async () => {
  const url = pathToFileURL(path.resolve(
    __dirname,
    "../../Prisma/calibration/app/features/geometries/builder.js",
  )).href;
  const { installFeaturesGeometriesBuilder } = await import(url);
  const app = { commands: {}, api: {}, constants: {}, dom: {}, state: { geometries: {}, session: {}, logbook: {}, ui: {} } };
  installFeaturesGeometriesBuilder(app);
  geometry = app.commands;
});

function makePayload({
  columns = 8,
  swatchWidth = 12,
  swatchHeight = 20,
  spineWidth = 3,
  spineTotal = 0.8,
  roles = [{ role_index: 1, role_kind: "variable", fixed_thickness_mm: null }],
  variableThicknesses = null,
} = {}) {
  const values = variableThicknesses || Array.from({ length: columns }, (_, index) => 0.2 + index * 0.08);
  return {
    alias: "preview fixture",
    layout_rows: 1,
    layout_columns: columns,
    swatch_width_mm: swatchWidth,
    swatch_height_mm: swatchHeight,
    spine_width_mm: spineWidth,
    spine_total_thickness_mm: spineTotal,
    roles: roles.map((role) => ({ role_label: `LR_${String(role.role_index).padStart(2, "0")}`, ...role })),
    swatch_slots: values.map((variableThickness, index) => ({
      swatch_index: index,
      row_index: 0,
      column_index: index,
      variable_thickness_mm: variableThickness,
    })),
  };
}

test("canonical default projection matches backend footprint formulas", () => {
  const projected = geometry.projectGeometryVisualDraft(makePayload());
  assert.equal(projected.available, true);
  assert.equal(projected.footprint.widthMm, 102);
  assert.equal(projected.footprint.heightMm, 23);
  assert.equal(projected.top.swatches.length, 8);
  assert.deepEqual(
    [projected.top.swatches[0].xMm, projected.top.swatches[7].xMm, projected.top.swatches[0].yMm],
    [3, 87, 0],
  );
});

test("Top View uses one physical scale and Side View shares its x projection", () => {
  const projected = geometry.projectGeometryVisualDraft(makePayload(), {
    topWidth: 520, topHeight: 220, sideWidth: 520, sideHeight: 160,
  });
  const firstTop = projected.top.swatches[0];
  const firstSide = projected.side.stacks[0];
  assert.equal(firstTop.width / 12, projected.scales.sharedX);
  assert.equal(firstTop.height / 20, projected.scales.sharedX);
  assert.equal(projected.top.xOrigin, projected.side.xOrigin);
  assert.equal(firstTop.x, firstSide.x);
  assert.equal(firstTop.width, firstSide.width);
});

test("Top View projects the canonical U spine and every swatch boundary", () => {
  const projected = geometry.projectGeometryVisualDraft(makePayload({ columns: 4 }));
  const [left, right, top] = projected.top.spines;
  assert.deepEqual(
    [left.part, left.xMm, left.heightMm, right.part, right.xMm, top.part, top.yMm, top.widthMm],
    ["left", 0, 23, "right", 51, "top", 20, 48],
  );
  assert.deepEqual(Array.from(projected.top.swatches, (swatch) => swatch.xMm), [3, 15, 27, 39]);
});

test("Side View independently bounds z while retaining exact layer proportions", () => {
  const projected = geometry.projectGeometryVisualDraft(makePayload({
    columns: 2,
    roles: [
      { role_index: 1, role_kind: "fixed", fixed_thickness_mm: 0.2 },
      { role_index: 2, role_kind: "variable", fixed_thickness_mm: null },
      { role_index: 3, role_kind: "fixed", fixed_thickness_mm: 0.1 },
    ],
    variableThicknesses: [0.1, 0.4],
    spineTotal: 0.7,
  }));
  const [first, second] = projected.side.stacks;
  assert.ok(Math.abs(projected.side.maxStackHeightMm - 0.7) < 1e-9);
  assert.deepEqual(
    Array.from(first.layers, (layer) => [layer.roleIndex, layer.zMinMm, layer.zMaxMm]),
    [[1, 0, 0.2], [2, 0.2, 0.30000000000000004], [3, 0.30000000000000004, 0.4]],
  );
  assert.equal(second.layers[1].height / first.layers[1].height, 4);
  assert.notEqual(projected.scales.sideZ, projected.scales.sharedX);
});

test("variable role intervals remain correct at bottom, middle, and top", () => {
  const variants = [
    [{ role_index: 1, role_kind: "variable" }, { role_index: 2, role_kind: "fixed", fixed_thickness_mm: 0.2 }],
    [{ role_index: 1, role_kind: "fixed", fixed_thickness_mm: 0.2 }, { role_index: 2, role_kind: "variable" }, { role_index: 3, role_kind: "fixed", fixed_thickness_mm: 0.1 }],
    [{ role_index: 1, role_kind: "fixed", fixed_thickness_mm: 0.2 }, { role_index: 2, role_kind: "variable" }],
  ];
  const expected = [[0, 0.3], [0.2, 0.5], [0.2, 0.5]];
  variants.forEach((roles, index) => {
    const projected = geometry.projectGeometryVisualDraft(makePayload({ columns: 1, roles, variableThicknesses: [0.3] }));
    const variable = projected.side.stacks[0].layers.find((layer) => layer.roleKind === "variable");
    assert.deepEqual([variable.zMinMm, variable.zMaxMm], expected[index]);
  });
});

test("descending thicknesses and a zero variable layer remain truthful", () => {
  const projected = geometry.projectGeometryVisualDraft(makePayload({
    columns: 4, variableThicknesses: [0.6, 0.4, 0.2, 0], spineTotal: 0.6,
  }));
  assert.deepEqual(Array.from(projected.side.stacks, (stack) => stack.stackHeightMm), [0.6, 0.4, 0.2, 0]);
  assert.equal(projected.side.stacks[3].layers[0].height, 0);
});

test("one and forty-eight swatches both fit the bounded Top View", () => {
  for (const columns of [1, 48]) {
    const projected = geometry.projectGeometryVisualDraft(makePayload({
      columns, variableThicknesses: Array.from({ length: columns }, () => 0.2),
    }));
    assert.equal(projected.available, true);
    assert.ok(projected.top.xOrigin >= 42);
    assert.ok(projected.top.xOrigin + projected.top.drawWidth <= 478 + 1e-9);
    assert.ok(projected.top.yOrigin + projected.top.drawHeight <= 186 + 1e-9);
  }
});

test("invalid or unsupported drafts return an unavailable state", () => {
  const mutations = [
    (p) => { p.layout_rows = 2; },
    (p) => { p.layout_columns = 0; p.swatch_slots = []; },
    (p) => { p.swatch_width_mm = 0; },
    (p) => { p.spine_total_thickness_mm = Number.POSITIVE_INFINITY; },
    (p) => { p.roles[0] = null; },
    (p) => { p.swatch_slots[0].variable_thickness_mm = -0.01; },
    (p) => { p.swatch_slots[1].column_index = 0; },
  ];
  mutations.forEach((mutate) => {
    const payload = makePayload();
    mutate(payload);
    assert.equal(geometry.projectGeometryVisualDraft(payload).available, false);
  });
});

test("SVG builders consume the projected model and distinguish the variable role", () => {
  const projected = geometry.projectGeometryVisualDraft(makePayload({
    columns: 2,
    roles: [
      { role_index: 1, role_kind: "fixed", fixed_thickness_mm: 0.2 },
      { role_index: 2, role_kind: "variable" },
    ],
    variableThicknesses: [0.2, 0.4],
    spineTotal: 0.6,
  }));
  const topSvg = geometry.buildGeometryVisualTopSvg(projected);
  const sideSvg = geometry.buildGeometryVisualSideSvg(projected);
  assert.equal((topSvg.match(/class="geometry-visual-swatch"/g) || []).length, 2);
  assert.equal((sideSvg.match(/geometry-visual-layer is-variable/g) || []).length, 2);
  assert.equal((sideSvg.match(/geometry-visual-layer is-fixed/g) || []).length, 2);
  assert.match(sideSvg, /vector-effect="non-scaling-stroke"/);
});
