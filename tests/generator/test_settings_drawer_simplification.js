"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const appDir = path.resolve(__dirname, "..", "..", "Prisma", "generator", "app");
const source = fs.readFileSync(path.join(appDir, "app.js"), "utf8");
const html = fs.readFileSync(path.join(appDir, "index.html"), "utf8");
const css = fs.readFileSync(path.join(appDir, "style.css"), "utf8");

function section(startText, endText) {
  const start = source.indexOf(startText);
  const end = source.indexOf(endText, start + startText.length);
  assert.notEqual(start, -1, `${startText} must exist`);
  assert.notEqual(end, -1, `${endText} must exist after ${startText}`);
  return source.slice(start, end);
}

test("live Settings Drawer has one responsive expanded mode", () => {
  for (const retired of [
    "settingsDrawerWidthMode",
    "SETTINGS_DRAWER_WIDTH_STORAGE_KEY",
    "settingsDrawerWidthToggle",
    "settingsDrawerNav",
    "settingsDrawerPage",
    "setSettingsDrawerPage",
    "drawer-page-active",
    "is-compact",
  ]) {
    assert.doesNotMatch(source + html + css, new RegExp(retired), `${retired} should be retired`);
  }
  assert.match(css, /\.settings-drawer\s*\{[\s\S]*width: min\(calc\(100% - 32px\), var\(--settings-drawer-width\)\)/);
  assert.match(css, /@media \(max-width: 700px\)[\s\S]*\.settings-drawer\s*\{[\s\S]*width: 100%/);
});

test("opening and closing preserve one grid and safely unwrap generated columns", () => {
  const open = section("function openSettingsDrawer()", "function closeSettingsDrawer()");
  const close = section("function closeSettingsDrawer()", "function scheduleSettingsDrawerDistribution()");
  assert.match(open, /drawerBody\.appendChild\(grid\)/);
  assert.match(open, /grid\.classList\.add\("in-drawer"\)/);
  assert.match(open, /scheduleSettingsDrawerDistribution\(\)/);
  assert.match(close, /grid\.querySelectorAll\("\.settings-column"\)/);
  assert.match(close, /while \(col\.firstChild\) grid\.appendChild\(col\.firstChild\)/);
  assert.match(close, /restoreSettingsFlowUnits\(grid\)/);
  assert.match(close, /tabSettings\.appendChild\(grid\)/);
});

test("preprocessing module cards flow as live units with canonical ownership", () => {
  const restore = section("function restoreSettingsFlowUnits(", "function extractPreprocessingFlowUnits(");
  const extract = section("function extractPreprocessingFlowUnits(", "/**\n * Distribute settings-grid");
  const distribute = section("function distributeSettingsColumns()", "let _resizeTimer");
  const dynamic = section("function renderDynamicSettings()", "// ── Collapsible Settings Sections");
  assert.match(restore, /owner\.appendChild\(unit\)/);
  assert.match(restore, /settingsFlowOrder/);
  assert.match(extract, /sort\(\(a, b\) => Number\(a\.dataset\.settingsFlowOrder\)/);
  assert.match(restore, /removeAttribute\("data-settings-flow-owner"\)/);
  assert.match(extract, /group\.classList\.contains\("is-expanded"\)/);
  assert.match(extract, /Array\.from\(owner\.children\)/);
  assert.match(extract, /insertionPoint\.after\(unit\)/);
  assert.doesNotMatch(extract, /cloneNode|innerHTML/);
  assert.match(distribute, /restoreSettingsFlowUnits\(grid\)[\s\S]*extractPreprocessingFlowUnits\(grid\)/);
  assert.match(distribute, /grid\.contains\(document\.activeElement\)/);
  assert.match(distribute, /focusedElement\.focus\(\{ preventScroll: true \}\)/);
  assert.match(distribute, /focusedElement\.setSelectionRange/);
  assert.match(dynamic, /restoreSettingsFlowUnits\(document\.querySelector\("\.settings-grid"\)\)/);
  assert.match(dynamic, /section\.dataset\.settingsFlowOrder = String\(index\)/);
  assert.match(
    css,
    /\.preprocessing-flow-unit \.settings-subsection-head::before\s*\{[\s\S]*content:\s*"Preprocessing · ";/,
  );
});

test("column distribution always treats an open drawer as the responsive host", () => {
  const distribute = section("function distributeSettingsColumns()", "let _resizeTimer");
  assert.match(distribute, /const inDrawer = grid\.classList\.contains\("in-drawer"\)/);
  assert.match(distribute, /const drawer = inDrawer \? document\.getElementById\("settingsDrawer"\) : null/);
  assert.match(distribute, /const measureEl = inDrawer/);
  assert.doesNotMatch(distribute, /drawerExpanded|settingsDrawerWidthMode/);
});

test("Advanced toggle remains independent and redistributes without losing live controls", () => {
  const visibility = section("function updateAdvancedSettingsVisibility()", "function openFilamentDetail");
  const bindings = section("// Settings drawer", "// Lightbox close");
  assert.match(visibility, /grid\.classList\.toggle\("show-advanced-settings", settingsAdvancedVisible\)/);
  assert.match(bindings, /settingsAdvancedVisible = !settingsAdvancedVisible/);
  assert.match(bindings, /saveSettingsAdvancedVisible\(settingsAdvancedVisible\)/);
  assert.match(bindings, /updateAdvancedSettingsVisibility\(\)/);
  assert.match(bindings, /distributeSettingsColumns\(\)/);
});

test("collapsing a settings section redistributes preprocessing continuation units", () => {
  const collapsible = section("function initCollapsibleSections()", "// ── Event Binding");
  assert.match(collapsible, /body\.classList\.toggle\("is-hidden", !expanded\)/);
  assert.match(collapsible, /distributeSettingsColumns\(\)/);
});

test("preprocessing preset selectors use one compact responsive width", () => {
  const match = css.match(/\.module-preset-select\s*\{([^}]*)\}/);
  assert.ok(match, "module preset selector rule should exist");
  assert.match(match[1], /width: 8rem;/);
  assert.match(match[1], /max-width: 100%;/);
  assert.match(match[1], /margin-left: auto;/);
  assert.doesNotMatch(match[1], /^\s*width: 100%;/m);
});
