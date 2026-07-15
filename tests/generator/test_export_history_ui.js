"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const repoRoot = path.resolve(__dirname, "..", "..");
const appSource = fs.readFileSync(path.join(repoRoot, "Prisma", "generator", "app", "app.js"), "utf8");
const apiSource = fs.readFileSync(path.join(repoRoot, "Prisma", "generator", "app", "api.js"), "utf8");
const htmlSource = fs.readFileSync(path.join(repoRoot, "Prisma", "generator", "app", "index.html"), "utf8");
const styleSource = fs.readFileSync(path.join(repoRoot, "Prisma", "generator", "app", "style.css"), "utf8");

function extractFunction(name) {
  const start = appSource.indexOf(`function ${name}`);
  assert.notEqual(start, -1, `${name} must exist`);
  const paramsStart = appSource.indexOf("(", start);
  let parenDepth = 0;
  let paramsEnd = -1;
  for (let i = paramsStart; i < appSource.length; i += 1) {
    if (appSource[i] === "(") parenDepth += 1;
    if (appSource[i] === ")") {
      parenDepth -= 1;
      if (parenDepth === 0) {
        paramsEnd = i;
        break;
      }
    }
  }
  const bodyStart = appSource.indexOf("{", paramsEnd);
  let braceDepth = 0;
  for (let i = bodyStart; i < appSource.length; i += 1) {
    if (appSource[i] === "{") braceDepth += 1;
    if (appSource[i] === "}") {
      braceDepth -= 1;
      if (braceDepth === 0) return appSource.slice(start, i + 1);
    }
  }
  throw new Error(`Could not extract ${name}`);
}

function loadExportStateHelpers() {
  const names = [
    "ensureSolveRunExportState",
    "getRunExportRecords",
    "createExportRecord",
    "appendExportRecordToRun",
    "selectRunExportRecord",
    "getSelectedExportRecord",
  ];
  return Function(`
    const _cloneValue = (value) => structuredClone(value);
    ${names.map(extractFunction).join("\n")}
    return { ${names.join(", ")} };
  `)();
}

function result(exportId, overrides = {}) {
  return {
    export_id: exportId,
    output_format: "3mf",
    geometry_source: "field_derived",
    field_scale: 4,
    files: [{ name: `${exportId}.3mf`, url: `/files/${exportId}` }],
    zip_url: `/zip/${exportId}`,
    manifest: { quality: { color: { is_watertight: true } } },
    swap_plan: { instructions: `Swap plan ${exportId}` },
    ...overrides,
  };
}

test("new and older loaded solve runs receive isolated export state", () => {
  assert.match(appSource, /function createSolveRun[\s\S]*exportRecords: \[\],[\s\S]*selectedExportId: null/);
  assert.match(appSource, /async function applyLoadedRun[\s\S]*exportRecords: \[\],[\s\S]*selectedExportId: null/);

  const helpers = loadExportStateHelpers();
  const oldRun = { id: "old" };
  assert.deepEqual(helpers.getRunExportRecords(oldRun), []);
  assert.equal(oldRun.selectedExportId, null);
});

test("exports append per run and retain independent selection", () => {
  const helpers = loadExportStateHelpers();
  const runA = { id: "run-a", exportRecords: [], selectedExportId: null };
  const runB = { id: "run-b", exportRecords: [], selectedExportId: null };

  helpers.appendExportRecordToRun(runA, result("a-1"), 100);
  helpers.appendExportRecordToRun(runA, result("a-2"), 200);
  helpers.appendExportRecordToRun(runB, result("b-1"), 300);

  assert.deepEqual(runA.exportRecords.map((record) => record.id), ["a-1", "a-2"]);
  assert.deepEqual(runB.exportRecords.map((record) => record.id), ["b-1"]);
  assert.equal(helpers.getSelectedExportRecord(runA).id, "a-2");
  assert.equal(helpers.selectRunExportRecord(runA, "a-1").id, "a-1");
  assert.equal(helpers.getSelectedExportRecord(runA).result.zip_url, "/zip/a-1");
  assert.equal(helpers.getSelectedExportRecord(runA).swapPlan.instructions, "Swap plan a-1");
  assert.equal(helpers.getSelectedExportRecord(runB).id, "b-1");
});

test("identical requested policies still append distinct backend exports", () => {
  const helpers = loadExportStateHelpers();
  const run = { id: "run", exportRecords: [], selectedExportId: null };
  helpers.appendExportRecordToRun(run, result("same-policy-1"));
  helpers.appendExportRecordToRun(run, result("same-policy-2"));
  assert.equal(run.exportRecords.length, 2);
  assert.deepEqual(run.exportRecords.map((record) => record.id), ["same-policy-1", "same-policy-2"]);
});

test("completed record identity and settings come only from the backend result", () => {
  const helpers = loadExportStateHelpers();
  const backend = result("canonical", {
    output_format: "stl",
    geometry_source: "exact_raster",
    field_scale: 16,
  });
  const record = helpers.createExportRecord(backend, 1234);
  backend.output_format = "mutated-after-completion";

  assert.deepEqual(
    {
      id: record.id,
      completedAt: record.completedAt,
      outputFormat: record.outputFormat,
      geometrySource: record.geometrySource,
      fieldScale: record.fieldScale,
      frozenResultFormat: record.result.output_format,
    },
    {
      id: "canonical",
      completedAt: 1234,
      outputFormat: "stl",
      geometrySource: "exact_raster",
      fieldScale: 16,
      frozenResultFormat: "stl",
    },
  );
  assert.throws(
    () => helpers.createExportRecord({ export_id: "incomplete" }),
    /missing its canonical identity or settings/,
  );
});

test("pending option changes preserve completed export records", () => {
  const source = extractFunction("handleExportOptionChange");
  assert.doesNotMatch(source, /exportRecords|selectedExportId|clearExport/);
  assert.match(source, /updateExportFieldScaleState\(\)/);
});

test("export completion is bound to the captured originating run", () => {
  const handler = extractFunction("handleExportFiles");
  assert.match(handler, /const originatingRunId = exportRun\.id;/);
  assert.match(handler, /activeExportRunId = originatingRunId;/);
  assert.match(handler, /solveRuns\.find\(\(run\) => run\.id === originatingRunId\)/);
  assert.match(handler, /appendExportRecordToRun\(originatingRun, status\.result/);
  assert.doesNotMatch(handler, /getSwapInstructions|syncConfigToServer/);
  assert.match(handler, /status\.status === "complete"[\s\S]*appendExportRecordToRun/);
});

test("cancelled and failed export branches do not append false records", () => {
  const handler = extractFunction("handleExportFiles");
  const appendAt = handler.indexOf("appendExportRecordToRun");
  const cancelledAt = handler.indexOf('status.status === "cancelled"');
  const failureAt = handler.indexOf('throw new Error(status.progress || "Export failed")');
  assert.ok(appendAt >= 0 && appendAt < cancelledAt && cancelledAt < failureAt);
  assert.equal((handler.match(/appendExportRecordToRun/g) || []).length, 1);
});

test("active export source deletion is blocked and record consumers use selection accessors", () => {
  const deletion = extractFunction("deleteSolveRun");
  assert.match(deletion, /exportRunning && runId === activeExportRunId/);
  assert.match(deletion, /Cancel the export before removing its source run/);
  assert.match(extractFunction("renderExportResults"), /getSelectedExportRecord\(\)/);
  assert.match(extractFunction("getExportMeshQualityEntries"), /getSelectedExportResult\(\)/);
  assert.match(appSource, /const url = getSelectedExportResult\(\)\?\.zip_url/);
  assert.match(extractFunction("renderExportResults"), /exportFileUrl\(f\.name, exportRecord\.id\)/);
  assert.match(apiSource, /split\('\/'\)\.map\(encodeURIComponent\)\.join\('\/'\)/);
});

test("selected solve preview uses the intended fallback order", () => {
  const previewUrl = Function(`${extractFunction("getExportSolvePreviewUrl")}; return getExportSolvePreviewUrl;`)();
  assert.equal(previewUrl({ results: { predicted_appearance_url: "appearance", predicted_url: "predicted" } }), "appearance");
  assert.equal(previewUrl({ results: { predicted_url: "predicted", source_url: "source" } }), "predicted");
  assert.equal(previewUrl({ results: { predicted_color_only_appearance_url: "color", source_url: "source" } }), "color");
  assert.equal(previewUrl({ results: { source_url: "source" } }), "source");
  assert.equal(previewUrl({ results: {} }), "");
  assert.match(extractFunction("renderExportSolvePreview"), /image\.dataset\.previewUrl !== url/);
  assert.match(extractFunction("renderExportSolvePreview"), /exportSolvePreviewDimensions/);
  assert.match(htmlSource, /id="exportSolvePreviewDimensions"/);
});

test("selected solve dimensions use solved pixels, physical content, and frozen border settings", () => {
  const dimensions = Function(`${extractFunction("getExportSolveDimensions")}; return getExportSolveDimensions;`)();
  const run = {
    image: { width: 4000, height: 3000 },
    config: { border: true, border_width_mm: 0.4 },
    results: {
      image_w: 800,
      image_h: 600,
      image_domain_width_mm: 160,
      image_domain_height_mm: 120,
    },
  };
  assert.deepEqual(dimensions(run), {
    imageWidthPx: 800,
    imageHeightPx: 600,
    imageWidthMm: 160,
    imageHeightMm: 120,
    totalWidthMm: 160.8,
    totalHeightMm: 120.8,
    borderEnabled: true,
    borderWidthMm: 0.4,
  });
  assert.doesNotMatch(extractFunction("renderExportSolvePreview"), /run\?\.image\?\.width|run\?\.image\?\.height/);
  assert.match(extractFunction("renderExportSolvePreview"), /Image area/);
  assert.match(extractFunction("renderExportSolvePreview"), /Total footprint/);
  assert.match(extractFunction("renderExportSolvePreview"), /solved\.borderEnabled/);
  assert.match(styleSource, /\.export-solve-preview-dimensions span/);
});

test("export history records and displays solve/export durations plus folder actions", () => {
  const helpers = loadExportStateHelpers();
  const run = { id: "run", exportRecords: [], selectedExportId: null };
  const record = helpers.appendExportRecordToRun(run, result("duration"), 100, 12.6);
  assert.equal(record.durationSeconds, 12.6);
  assert.match(extractFunction("renderExportRecordSelector"), /record\.durationSeconds/);
  assert.match(extractFunction("renderExportRunSidebar"), /solve_elapsed_s/);
  assert.match(extractFunction("renderExportResults"), /open-export-folder-btn/);
  assert.match(appSource, /No cached solve found/);
  assert.match(appSource, /Unavailable after restart/);
  assert.match(apiSource, /openExportFolder\(exportId\)/);
  assert.match(apiSource, /export\/files\/open-folder/);
});

test("export page unifies files, swap instructions, and mesh report under selectable exports", () => {
  const resultsAt = htmlSource.indexOf("export-results-panel");
  const recordsAt = htmlSource.indexOf('id="exportRecordList"', resultsAt);
  const filesAt = htmlSource.indexOf('id="exportFileList"', resultsAt);
  const swapAt = htmlSource.indexOf('id="swapInstructions"', resultsAt);
  const qualityAt = htmlSource.indexOf('id="exportQualityTable"', resultsAt);
  assert.ok(resultsAt >= 0 && recordsAt < filesAt && filesAt < swapAt && swapAt < qualityAt);
  assert.doesNotMatch(htmlSource, /id="generateSwapBtn"/);
  assert.match(htmlSource, /id="exportSolvePreviewImg"/);
  assert.match(htmlSource, />Generated Exports<\/h3>/);
});

test("export selector labels and switches immutable records using canonical record fields", () => {
  const renderer = extractFunction("renderExportRecordSelector");
  assert.match(renderer, /record\.geometrySource/);
  assert.match(renderer, /record\.outputFormat/);
  assert.match(renderer, /record\.fieldScale/);
  assert.match(renderer, /selectRunExportRecord\(run, button\.dataset\.exportRecordId\)/);
  assert.doesNotMatch(renderer, /exportGeometrySource|exportOutputFormat|exportFieldScale/);
});

test("export layout retains a narrow single-column command fallback", () => {
  assert.match(styleSource, /\.export-command-grid\s*\{[\s\S]*grid-template-columns: minmax\(300px, 1fr\) minmax\(180px, 240px\)/);
  assert.match(styleSource, /@media \(max-width: 600px\)[\s\S]*\.export-command-grid \{ grid-template-columns: 1fr; \}/);
  assert.match(styleSource, /\.export-record-card\.is-selected/);
});

test("export workspace is left-aligned and capped at a readable desktop width", () => {
  const layoutRule = styleSource.match(/\.export-layout\s*\{(?<body>[\s\S]*?)}/)?.groups?.body || "";
  assert.match(layoutRule, /width:\s*min\(100%, 1240px\);/);
  assert.match(layoutRule, /max-width:\s*1240px;/);
  assert.doesNotMatch(layoutRule, /margin-inline:\s*auto;/, "Export should retain the workspace's left anchor");
});
