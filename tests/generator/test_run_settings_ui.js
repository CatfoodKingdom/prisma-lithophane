"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const appDir = path.resolve(__dirname, "..", "..", "Prisma", "generator", "app");
const source = fs.readFileSync(path.join(appDir, "app.js"), "utf8");
const css = fs.readFileSync(path.join(appDir, "style.css"), "utf8");

function extractFunction(name) {
  const start = source.indexOf(`function ${name}`);
  assert.notEqual(start, -1, `${name} must exist`);
  const paramsStart = source.indexOf("(", start);
  let paramsDepth = 0;
  let paramsEnd = -1;
  for (let index = paramsStart; index < source.length; index += 1) {
    if (source[index] === "(") paramsDepth += 1;
    if (source[index] === ")") {
      paramsDepth -= 1;
      if (paramsDepth === 0) {
        paramsEnd = index;
        break;
      }
    }
  }
  const bodyStart = source.indexOf("{", paramsEnd);
  let depth = 0;
  for (let index = bodyStart; index < source.length; index += 1) {
    if (source[index] === "{") depth += 1;
    if (source[index] === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(start, index + 1);
    }
  }
  throw new Error(`Could not extract ${name}`);
}

test("shared prompt confirms with Enter, cancels with Escape, and cleans every handler", () => {
  const prompt = extractFunction("appPrompt");
  assert.match(prompt, /let settled = false/);
  assert.match(prompt, /if \(settled\) return/);
  assert.match(prompt, /e\.key === "Enter"[\s\S]*submit\(\)/);
  assert.match(prompt, /const submit = \(\) =>[\s\S]*close\(input\.value\)/);
  assert.match(prompt, /e\.key === "Escape"[\s\S]*close\(null\)/);
  assert.match(prompt, /document\.addEventListener\("keydown", onDocumentKeyDown\)/);
  assert.match(prompt, /document\.removeEventListener\("keydown", onDocumentKeyDown\)/);
  assert.match(prompt, /const previousFocus = document\.activeElement/);
  assert.match(prompt, /previousFocus\.focus\(\)/);
  for (const cleanup of [
    "overlay.onclick = null",
    "input.onkeydown = null",
    "input.oninput = null",
    "cancelBtn.onclick = null",
    "okBtn.onclick = null",
    "closeBtn.onclick = null",
  ]) assert.ok(prompt.includes(cleanup), `${cleanup} should be cleaned`);
});

test("frozen run settings use the documented precedence and diagnostic module truth", () => {
  const getSnapshot = Function(`
    const _cloneValue = value => value == null ? value : structuredClone(value);
    const _normalizeSettingsProfileModules = modules => ({ ...(modules || {}) });
    ${extractFunction("getFrozenSolveRunSnapshot")}
    return getFrozenSolveRunSnapshot;
  `)();
  const run = {
    config: {
      layer_height: 0.10,
      d_wb: 0.20,
      preprocessing_params: { denoise: { strength: 1 } },
    },
    recipe_snapshot: {
      profile_snapshot: {
        settings: {
          layer_height: 0.08,
          d_wb: 0.24,
          preprocessing_params: { denoise: { strength: 2 } },
        },
        modules: { denoise: true },
      },
    },
    results: {
      solve_start_diagnostics: {
        resolved_settings: { layer_height: 0.06 },
        active_modules: { preprocessing: [] },
        module_settings: { denoise: { strength: 3 } },
      },
    },
  };
  const frozen = getSnapshot(run);
  assert.equal(frozen.settings.layer_height, 0.06, "server-resolved value should win");
  assert.equal(frozen.settings.d_wb, 0.24, "profile snapshot should override broad run config");
  assert.equal(frozen.settings.preprocessing_params.denoise.strength, 3, "diagnostic module params should win");
  assert.equal(frozen.activePreprocessing.has("denoise"), false, "an authoritative empty active list means off");
  assert.equal(frozen.preprocessingStateKnown, true);
  const snapshotSource = extractFunction("getFrozenSolveRunSnapshot");
  assert.equal(snapshotSource.includes("config."), false, "global live config must not be read");
  assert.equal(snapshotSource.includes("_currentSettingsSnapshot"), false, "live settings helpers must not be read");
});

test("older runs fall back to their own archived config without consulting live state", () => {
  const snapshotSource = extractFunction("getFrozenSolveRunSnapshot");
  assert.match(snapshotSource, /run\?\.config/);
  assert.match(snapshotSource, /profile\.settings/);
  assert.match(snapshotSource, /diagnostics\.resolved_settings/);
  assert.match(snapshotSource, /diagnostics\.module_settings/);
  assert.match(snapshotSource, /diagnostics\.module_state/);
  assert.match(snapshotSource, /preprocessingStateKnown: diagnosticPreprocessing != null \|\| diagnosticModuleStateKnown \|\| profileModulesKnown/);
  assert.match(extractFunction("buildReadOnlyRunSectionRows"), /frozen\.preprocessingStateKnown[\s\S]*"Not recorded"/);
  assert.match(source, /Older saved run: unavailable values are marked as not recorded/);
});

test("Preview and Export cards share hover and Settings behavior", () => {
  assert.match(source, /bindSolveRunCardAuxiliaryInteractions\(container, "preview"\)/);
  assert.match(source, /bindSolveRunCardAuxiliaryInteractions\(container, "export"\)/);
  assert.match(extractFunction("bindSolveRunCardAuxiliaryInteractions"), /mousemove[\s\S]*focusin[\s\S]*focusout/);
  assert.match(extractFunction("isSolveRunHoverBlockedTarget"), /\.solve-run-card-actions[\s\S]*button/);
  assert.match(extractFunction("scheduleSolveRunHoverPreview"), /solveRunHoverPendingRunId === runId/);
  assert.match(source, /tabindex="0"/);
  assert.doesNotMatch(source, /solve-run-summary-link|openSolveRunSummaryId|solve-run-summary-flyout/);
  assert.match(css, /\.solve-run-hover-preview[\s\S]*position: fixed/);
});

test("read-only run settings are purpose-built text rows with independent Advanced state", () => {
  const builder = extractFunction("buildReadOnlyRunSettingsHtml");
  const renderer = extractFunction("renderSolveRunSettingsPanel");
  assert.match(source, /const READ_ONLY_RUN_SETTING_SECTIONS = \[[\s\S]*Essentials[\s\S]*Pre-processing[\s\S]*Color Solver[\s\S]*White Cap/);
  assert.match(builder, /run-settings-label/);
  assert.match(builder, /run-settings-value/);
  assert.doesNotMatch(builder, /<input|<select|type="range"|stg-range/);
  assert.match(renderer, /solveRunSettingsAdvancedVisible/);
  assert.match(source, /run-settings-load-btn/);
  assert.match(source, /_loadTemporarySettingsFromRun\(run/);
  assert.match(source, /Loaded settings from .* as TEMP/);
  assert.doesNotMatch(renderer, /settingsAdvancedVisible/);
  assert.match(css, /\.run-settings-row\.is-advanced\s*\{[\s\S]*display: none/);
  assert.match(css, /\.run-settings-panel\.show-advanced-settings \.run-settings-row\.is-advanced/);
});

test("run settings panel is anchored inward according to the owning sidebar", () => {
  const positioner = extractFunction("positionSolveRunSettingsPanel");
  assert.match(positioner, /context === "preview" \? rect\.left - panelRect\.width - gap : rect\.right \+ gap/);
  assert.match(positioner, /window\.innerWidth/);
  assert.match(positioner, /window\.innerHeight/);
  assert.match(css, /\.run-settings-panel\s*\{[\s\S]*position: fixed/);
});
