"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const appDir = path.resolve(__dirname, "../../Prisma/generator/app");
const entry = path.join(appDir, "bootstrap.js");

function importsFor(file) {
  const source = fs.readFileSync(file, "utf8");
  return [...source.matchAll(/(?:from\s+|import\s*)["'](\.[^"']+)["']/g)]
    .map((match) => path.resolve(path.dirname(file), match[1]));
}

test("Generator frontend module graph has no missing files or cycles", () => {
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
  assert.ok(visited.size >= 10, "the module graph should include the split API and core modules");
});

test("Generator loads through one native-module entry point", () => {
  const html = fs.readFileSync(path.join(appDir, "index.html"), "utf8");
  const scripts = [...html.matchAll(/<script\b[^>]*src=/g)];
  assert.equal(scripts.length, 1);
  assert.match(html, /<script type="module" src="bootstrap\.js\?v=/);
  assert.match(html, /<script data-prisma-theme-bootstrap>/);
  assert.match(html, /prisma_generator_theme/);
  assert.doesNotMatch(html, /data-prisma-theme-bootstrap[^>]*\bsrc=/);
  assert.ok(
    html.indexOf("data-prisma-theme-bootstrap") < html.indexOf("rel=\"stylesheet\""),
    "the synchronous theme bootstrap must run before CSS loads",
  );
  assert.equal(fs.existsSync(path.join(appDir, "app.js")), false);
});

test("feature modules communicate through injected context instead of importing peers", () => {
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
    const source = fs.readFileSync(file, "utf8");
    assert.doesNotMatch(
      source,
      /\bwindow\.[A-Za-z_$][\w$]*\s*=(?!=)/,
      `${path.relative(appDir, file)} assigns a window global`,
    );
  }
});

test("features use injected API clients instead of legacy unbound helpers", () => {
  const featureDir = path.join(appDir, "features");
  const files = fs.readdirSync(featureDir, { recursive: true })
    .filter((name) => name.endsWith(".js"))
    .map((name) => path.join(featureDir, name));

  for (const file of files) {
    const source = fs.readFileSync(file, "utf8")
      .replaceAll("app.api.apiFetch", "injectedFetch")
      .replaceAll("app.api.apiPost", "injectedPost");
    assert.doesNotMatch(
      source,
      /\bapi(?:Fetch|Post)\s*\(/,
      `${path.relative(appDir, file)} calls an unbound legacy API helper`,
    );
  }
});

test("Generator CSS classifies every literal color as a token or feature visualization variable", () => {
  const stylesDir = path.join(appDir, "styles");
  const files = fs.readdirSync(stylesDir)
    .filter((name) => name.endsWith(".css") && name !== "tokens.css")
    .map((name) => path.join(stylesDir, name));
  const literalColor = /#[0-9a-f]{3,8}\b|rgba?\([^)]*\)|hsla?\([^)]*\)/i;

  for (const file of files) {
    const lines = fs.readFileSync(file, "utf8").split(/\r?\n/);
    lines.forEach((line, index) => {
      if (!literalColor.test(line)) return;
      assert.match(
        line.trim(),
        /^--[\w-]+\s*:/,
        `${path.relative(appDir, file)}:${index + 1} has an unclassified literal color`,
      );
    });
  }
});
