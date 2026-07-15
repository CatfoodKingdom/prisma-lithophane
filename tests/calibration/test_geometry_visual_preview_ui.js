"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const repoRoot = path.resolve(__dirname, "..", "..");
const appSource = fs.readFileSync(
  path.join(repoRoot, "Prisma", "calibration", "app", "app.js"),
  "utf8",
);
const styleSource = fs.readFileSync(
  path.join(repoRoot, "Prisma", "calibration", "app", "style.css"),
  "utf8",
);

function loadProjectionHelper() {
  const start = appSource.indexOf("function projectGeometryVisualDraft(payload, viewportSpec = {}) {");
  const end = appSource.indexOf("function markStepBuilderInvalid", start);
  assert.notEqual(start, -1, "geometry visual projection helper must exist");
  assert.notEqual(end, -1, "geometry visual projection helper must have a stable boundary");
  const context = {};
  vm.runInNewContext(appSource.slice(start, end), context);
  return context;
}

const projectionHelpers = loadProjectionHelper();
const { projectGeometryVisualDraft } = projectionHelpers;

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
    roles: roles.map((role) => ({
      role_label: `LR_${String(role.role_index).padStart(2, "0")}`,
      ...role,
    })),
    swatch_slots: values.map((variableThickness, index) => ({
      swatch_index: index,
      row_index: 0,
      column_index: index,
      variable_thickness_mm: variableThickness,
    })),
  };
}

test("canonical default projection matches backend footprint formulas", () => {
  const projected = projectGeometryVisualDraft(makePayload());

  assert.equal(projected.available, true);
  assert.equal(projected.footprint.widthMm, 102);
  assert.equal(projected.footprint.heightMm, 23);
  assert.equal(projected.top.swatches.length, 8);
  assert.equal(projected.top.swatches[0].xMm, 3);
  assert.equal(projected.top.swatches[7].xMm, 87);
  assert.equal(projected.top.swatches[0].yMm, 0);
});

test("Top View uses one physical scale and Side View shares its x projection", () => {
  const projected = projectGeometryVisualDraft(makePayload(), {
    topWidth: 520,
    topHeight: 220,
    sideWidth: 520,
    sideHeight: 160,
  });

  const firstTop = projected.top.swatches[0];
  const firstSide = projected.side.stacks[0];
  assert.equal(firstTop.width / 12, projected.scales.sharedX);
  assert.equal(firstTop.height / 20, projected.scales.sharedX);
  assert.equal(projected.top.xOrigin, projected.side.xOrigin);
  assert.equal(firstTop.x, firstSide.x);
  assert.equal(firstTop.width, firstSide.width);
  assert.equal(projected.side.spines[0].width / 3, projected.scales.sharedX);
});

test("Top View projects the canonical U spine and every swatch boundary", () => {
  const projected = projectGeometryVisualDraft(makePayload({ columns: 4 }));
  const [left, right, top] = projected.top.spines;

  assert.equal(left.part, "left");
  assert.equal(left.xMm, 0);
  assert.equal(left.yMm, 0);
  assert.equal(left.widthMm, 3);
  assert.equal(left.heightMm, 23);
  assert.equal(right.part, "right");
  assert.equal(right.xMm, 51);
  assert.equal(right.heightMm, 23);
  assert.equal(top.part, "top");
  assert.equal(top.xMm, 3);
  assert.equal(top.yMm, 20);
  assert.equal(top.widthMm, 48);
  assert.equal(top.heightMm, 3);
  assert.deepEqual(
    Array.from(projected.top.swatches, (swatch) => swatch.xMm),
    [3, 15, 27, 39],
  );
});

test("Side View independently bounds z while retaining exact layer proportions", () => {
  const roles = [
    { role_index: 1, role_kind: "fixed", fixed_thickness_mm: 0.2 },
    { role_index: 2, role_kind: "variable", fixed_thickness_mm: null },
    { role_index: 3, role_kind: "fixed", fixed_thickness_mm: 0.1 },
  ];
  const projected = projectGeometryVisualDraft(makePayload({
    columns: 2,
    roles,
    variableThicknesses: [0.1, 0.4],
    spineTotal: 0.7,
  }));
  const first = projected.side.stacks[0];
  const second = projected.side.stacks[1];

  assert.equal(projected.available, true);
  assert.ok(Math.abs(projected.side.maxStackHeightMm - 0.7) < 1e-9);
  assert.equal(first.stackHeightMm, 0.4);
  assert.ok(Math.abs(second.stackHeightMm - 0.7) < 1e-9);
  assert.deepEqual(
    Array.from(first.layers, (layer) => [layer.roleIndex, layer.zMinMm, layer.zMaxMm]),
    [[1, 0, 0.2], [2, 0.2, 0.30000000000000004], [3, 0.30000000000000004, 0.4]],
  );
  assert.equal(second.layers[1].height / first.layers[1].height, 4);
  assert.notEqual(projected.scales.sideZ, projected.scales.sharedX);
});

test("variable role intervals remain correct at bottom, middle, and top", () => {
  const variants = [
    [
      { role_index: 1, role_kind: "variable", fixed_thickness_mm: null },
      { role_index: 2, role_kind: "fixed", fixed_thickness_mm: 0.2 },
    ],
    [
      { role_index: 1, role_kind: "fixed", fixed_thickness_mm: 0.2 },
      { role_index: 2, role_kind: "variable", fixed_thickness_mm: null },
      { role_index: 3, role_kind: "fixed", fixed_thickness_mm: 0.1 },
    ],
    [
      { role_index: 1, role_kind: "fixed", fixed_thickness_mm: 0.2 },
      { role_index: 2, role_kind: "variable", fixed_thickness_mm: null },
    ],
  ];
  const expectedVariableIntervals = [[0, 0.3], [0.2, 0.5], [0.2, 0.5]];

  variants.forEach((roles, index) => {
    const projected = projectGeometryVisualDraft(makePayload({
      columns: 1,
      roles,
      variableThicknesses: [0.3],
      spineTotal: 0.8,
    }));
    const variable = projected.side.stacks[0].layers.find((layer) => layer.roleKind === "variable");
    assert.deepEqual(
      [variable.zMinMm, variable.zMaxMm],
      expectedVariableIntervals[index],
    );
  });
});

test("descending thicknesses and a zero variable layer remain truthful", () => {
  const projected = projectGeometryVisualDraft(makePayload({
    columns: 4,
    variableThicknesses: [0.6, 0.4, 0.2, 0],
    spineTotal: 0.6,
  }));

  assert.equal(projected.available, true);
  assert.deepEqual(
    Array.from(projected.side.stacks, (stack) => stack.stackHeightMm),
    [0.6, 0.4, 0.2, 0],
  );
  assert.equal(projected.side.stacks[3].layers[0].height, 0);
});

test("one and forty-eight swatches both fit the bounded Top View", () => {
  for (const columns of [1, 48]) {
    const projected = projectGeometryVisualDraft(makePayload({
      columns,
      variableThicknesses: Array.from({ length: columns }, () => 0.2),
    }));
    assert.equal(projected.available, true);
    assert.ok(projected.top.xOrigin >= 42);
    assert.ok(projected.top.xOrigin + projected.top.drawWidth <= 520 - 42 + 1e-9);
    assert.ok(projected.top.yOrigin >= 28);
    assert.ok(projected.top.yOrigin + projected.top.drawHeight <= 220 - 34 + 1e-9);
  }
});

test("invalid or unsupported drafts return an unavailable state", () => {
  const cases = [
    (payload) => { payload.layout_rows = 2; },
    (payload) => { payload.layout_columns = 0; payload.swatch_slots = []; },
    (payload) => { payload.swatch_width_mm = 0; },
    (payload) => { payload.swatch_height_mm = -1; },
    (payload) => { payload.spine_width_mm = ""; },
    (payload) => { payload.spine_total_thickness_mm = Number.POSITIVE_INFINITY; },
    (payload) => { payload.roles[0].role_kind = "fixed"; payload.roles[0].fixed_thickness_mm = 0; },
    (payload) => { payload.roles[0] = null; },
    (payload) => { payload.swatch_slots[0].variable_thickness_mm = -0.01; },
    (payload) => { payload.swatch_slots[0].variable_thickness_mm = Number.NaN; },
    (payload) => { payload.swatch_slots[0] = null; },
    (payload) => { payload.spine_total_thickness_mm = 0.1; },
    (payload) => { payload.swatch_slots[1].column_index = 0; },
  ];

  cases.forEach((mutate) => {
    const payload = makePayload();
    mutate(payload);
    assert.equal(projectGeometryVisualDraft(payload).available, false);
  });
});

test("SVG builders consume the projected model and distinguish the variable role", () => {
  const projected = projectGeometryVisualDraft(makePayload({
    columns: 2,
    roles: [
      { role_index: 1, role_kind: "fixed", fixed_thickness_mm: 0.2 },
      { role_index: 2, role_kind: "variable", fixed_thickness_mm: null },
    ],
    variableThicknesses: [0.2, 0.4],
    spineTotal: 0.6,
  }));
  const topSvg = projectionHelpers.buildGeometryVisualTopSvg(projected);
  const sideSvg = projectionHelpers.buildGeometryVisualSideSvg(projected);

  assert.match(topSvg, /geometry-visual-top-svg/);
  assert.equal((topSvg.match(/class="geometry-visual-swatch"/g) || []).length, 2);
  assert.match(sideSvg, /geometry-visual-side-svg/);
  assert.equal((sideSvg.match(/geometry-visual-layer is-variable/g) || []).length, 2);
  assert.equal((sideSvg.match(/geometry-visual-layer is-fixed/g) || []).length, 2);
  assert.match(sideSvg, /vector-effect="non-scaling-stroke"/);
});

test("structured drawer owns a two-column visual module and renamed diagram module", () => {
  const start = appSource.indexOf("function openStepBuilderDrawer() {");
  const end = appSource.indexOf("async function populateStepBuilderBundleDropdown", start);
  const drawerSource = appSource.slice(start, end);

  assert.match(drawerSource, /class="step-builder-layout"/);
  assert.match(drawerSource, /class="step-builder-visual-column"/);
  assert.match(drawerSource, /buildDrawerFormModule\("Visual Preview"/);
  assert.match(drawerSource, />Top View</);
  assert.match(drawerSource, />Side View</);
  assert.match(drawerSource, /structuredMode \? "Strip Diagram Preview" : "Preview"/);
  assert.match(drawerSource, /class="step-builder-form-column"/);
});

test("visual preview is rendered from the same structured draft used by the diagram", () => {
  const start = appSource.indexOf("function updateStepPreview() {");
  const end = appSource.indexOf("function renderStepBuilder() {", start);
  const updateSource = appSource.slice(start, end);

  assert.equal((updateSource.match(/structuredGeometryPayloadFromBuilder\(\)/g) || []).length, 1);
  assert.match(updateSource, /roles: structuredPayload \? structuredPayload\.roles : \[\]/);
  assert.match(updateSource, /renderStepGeometryVisualPreview\(structuredPayload\)/);
});

test("invalid visual drafts replace both drawings instead of retaining stale SVG", () => {
  const start = appSource.indexOf("function renderStepGeometryVisualPreview(payload) {");
  const end = appSource.indexOf("function markStepBuilderInvalid", start);
  const renderSource = appSource.slice(start, end);

  assert.match(renderSource, /root\.dataset\.previewState = projected\.available \? "available" : "unavailable"/);
  assert.match(renderSource, /if \(!projected\.available\)/);
  assert.match(renderSource, /topSurface\.innerHTML = unavailableHtml/);
  assert.match(renderSource, /sideSurface\.innerHTML = unavailableHtml/);
  assert.match(renderSource, /footprintLabel\.textContent = ""/);
});

test("structured drawer width adds a bounded visual column without changing legacy mode", () => {
  const start = appSource.indexOf("function updateStepBuilderDrawerWidth() {");
  const end = appSource.indexOf("function resizeStepBuilderValues", start);
  const widthSource = appSource.slice(start, end);

  assert.match(widthSource, /if \(!isStructuredGeometryBackend\(\)\)/);
  assert.match(widthSource, /removeProperty\("--step-builder-width"\)/);
  assert.match(widthSource, /removeProperty\("--step-builder-form-width"\)/);
  assert.match(widthSource, /const visualWidth = 408/);
  assert.match(widthSource, /Math\.min\(1320, formWidth \+ visualWidth \+ columnGap\)/);
  assert.match(
    styleSource,
    /\.step-builder-drawer\s*\{[^}]*width:\s*min\(var\(--step-builder-width, 520px\), 82vw, 900px\)/s,
  );
  assert.match(
    styleSource,
    /\.step-builder-drawer\.is-structured\s*\{[^}]*1320px/s,
  );
});

test("structured drawer collapses to one scroll column at narrow widths", () => {
  assert.match(
    styleSource,
    /\.step-builder-layout\s*\{[^}]*grid-template-columns:\s*minmax\(360px, 1fr\) minmax\(0, var\(--step-builder-form-width, 520px\)\)/s,
  );
  assert.match(
    styleSource,
    /@media \(max-width: 1100px\)[\s\S]*?\.step-builder-layout\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\)[^}]*grid-auto-rows:\s*max-content/,
  );
  assert.match(styleSource, /\.step-builder-visual-module\s*\{[^}]*max-width:\s*520px/s);
  assert.match(
    styleSource,
    /\.step-builder-visual-column,\s*\.step-builder-form-column\s*\{[^}]*grid-auto-rows:\s*max-content/s,
  );
  assert.match(styleSource, /\.geometry-visual-top-surface\s*\{[^}]*aspect-ratio:\s*520 \/ 220/s);
  assert.match(styleSource, /\.geometry-visual-side-surface\s*\{[^}]*aspect-ratio:\s*520 \/ 160/s);
});

test("structured geometry form groups layout rows and explains variable increments", () => {
  const start = appSource.indexOf('buildDrawerFormModule("Strip Layout"');
  const end = appSource.indexOf('buildDrawerFormModule(structuredMode ? "Variable Layer Increment"', start);
  const layoutSource = appSource.slice(start, end);
  assert.match(layoutSource, /Swatch Width \(mm\)/);
  assert.match(layoutSource, /Swatch Height \(mm\)/);
  assert.match(layoutSource, /Number of Swatches/);
  assert.match(layoutSource, /Spine Width \(mm\)/);
  assert.match(layoutSource, /Spine Thickness \(mm\)/);
  assert.match(appSource, /Auto-fill swatch thicknesses for strips with constant swatch-to-swatch thickness increments \(optional\)/);
  assert.match(appSource, /First Swatch Thickness \(mm\)/);
  assert.match(appSource, /Swatch-to-Swatch Increment \(mm\)/);
  assert.match(appSource, /id="populateStepBtn">Fill Values/);
  assert.match(styleSource, /\.sb-strip-layout-row \+ \.sb-strip-layout-row/);
  assert.match(styleSource, /\.sb-module-caption/);
});
