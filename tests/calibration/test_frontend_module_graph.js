"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const appDir = path.resolve(__dirname, "../../Prisma/calibration/app");
const entry = path.join(appDir, "bootstrap.js");

function importsFor(file) {
  const source = fs.readFileSync(file, "utf8");
  return [...source.matchAll(/(?:from\s+|import\s*)["'](\.[^"']+)["']/g)]
    .map((match) => path.resolve(path.dirname(file), match[1]));
}

test("Calibration module graph has no missing files or cycles", () => {
  const visited = new Set();
  const active = new Set();
  function visit(file) {
    assert.ok(fs.existsSync(file), `missing imported frontend module: ${file}`);
    assert.ok(!active.has(file), `frontend module import cycle reaches ${file}`);
    if (visited.has(file)) return;
    active.add(file);
    for (const dependency of importsFor(file)) visit(dependency);
    active.delete(file);
    visited.add(file);
  }
  visit(entry);
  assert.ok(visited.size >= 20, "module graph should include API, core, and feature boundaries");
});

test("Calibration loads through one native-module entry point", () => {
  const html = fs.readFileSync(path.join(appDir, "index.html"), "utf8");
  assert.equal([...html.matchAll(/<script\b[^>]*src=/g)].length, 1);
  assert.match(html, /<script\s+type="module"\s+src="bootstrap\.js\?v=/);
  assert.doesNotMatch(html, /src="(?:app|api|data)\.js/);
  for (const retired of ["app.js", "api.js", "data.js"]) {
    assert.equal(fs.existsSync(path.join(appDir, retired)), false, `${retired} must not return as a compatibility monolith`);
  }
});

test("dialogs expose their visible headers as accessible names", () => {
  const files = [path.join(appDir, "index.html")]
    .concat(fs.readdirSync(path.join(appDir, "features"), { recursive: true })
      .filter((name) => name.endsWith(".js"))
      .map((name) => path.join(appDir, "features", name)));
  for (const file of files) {
    assert.doesNotMatch(
      fs.readFileSync(file, "utf8"),
      /aria-labeledby=/,
      `${path.relative(appDir, file)} uses the misspelled ARIA attribute`,
    );
  }
});

test("feature modules communicate through injected context instead of peer imports", () => {
  const featureDir = path.join(appDir, "features");
  const files = fs.readdirSync(featureDir, { recursive: true })
    .filter((name) => name.endsWith(".js"))
    .map((name) => path.join(featureDir, name));
  for (const file of files) {
    for (const dependency of importsFor(file)) {
      assert.equal(
        dependency.startsWith(featureDir),
        false,
        `${path.relative(appDir, file)} imports peer feature ${path.relative(appDir, dependency)}`,
      );
    }
  }
});

test("modules do not publish application state through window globals", () => {
  const files = fs.readdirSync(appDir, { recursive: true })
    .filter((name) => name.endsWith(".js"))
    .map((name) => path.join(appDir, name));
  for (const file of files) {
    const source = fs.readFileSync(file, "utf8")
      .replaceAll("app.api.apiFetch", "injectedFetch")
      .replaceAll("app.api.apiPost", "injectedPost");
    assert.doesNotMatch(
      source,
      /\bwindow\.[A-Za-z_$][\w$]*\s*=(?!=)/,
      `${path.relative(appDir, file)} assigns a window global`,
    );
  }
});

test("feature modules use the injected API surface", () => {
  const featureDir = path.join(appDir, "features");
  const files = fs.readdirSync(featureDir, { recursive: true })
    .filter((name) => name.endsWith(".js"))
    .map((name) => path.join(featureDir, name));
  for (const file of files) {
    const source = fs.readFileSync(file, "utf8")
      .replaceAll("app.api.apiFetch", "injectedFetch")
      .replaceAll("app.api.apiPost", "injectedPost");
    assert.doesNotMatch(source, /\b(?:apiFetch|apiPost)\s*\(/, path.relative(appDir, file));
  }
});

test("Calibration stylesheet manifest resolves balanced concern files", () => {
  const manifest = fs.readFileSync(path.join(appDir, "style.css"), "utf8");
  const imports = [...manifest.matchAll(/@import url\(["'](.+?)["']\);/g)]
    .map((match) => path.resolve(appDir, match[1]));
  assert.ok(imports.length >= 10, "the stylesheet should remain split by concern");
  const declarations = manifest
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/@import[^;]+;/g, "");
  assert.equal(declarations.trim(), "", "style.css must remain import-only");
  for (const file of imports) {
    assert.ok(fs.existsSync(file), `missing imported stylesheet: ${file}`);
    const source = fs.readFileSync(file, "utf8");
    assert.equal(
      (source.match(/\{/g) || []).length,
      (source.match(/\}/g) || []).length,
      `${path.relative(appDir, file)} has unbalanced rule blocks`,
    );
    assert.doesNotMatch(
      source,
      /,\s*}/,
      `${path.relative(appDir, file)} has a dangling selector before a rule close`,
    );
    assert.doesNotMatch(
      source,
      /(?:\r?\n){2}$/,
      `${path.relative(appDir, file)} has redundant blank lines at EOF`,
    );
    assert.ok(
      source.split(/\r?\n/).length <= 1800,
      `${path.relative(appDir, file)} has become another stylesheet monolith`,
    );
  }
});
