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

test("light headers use the blue semantic token without changing dark mode or visualizations", () => {
  const stylesDir = path.join(appDir, "styles");
  const tokens = fs.readFileSync(path.join(stylesDir, "tokens.css"), "utf8");
  assert.match(tokens, /:root\s*{[\s\S]*?--header-bg:\s*#dce3e8;/);
  assert.match(tokens, /\[data-theme="dark"\]\s*{[\s\S]*?--header-bg:\s*#2c312d;/);
  const luminance = (hex) => [1, 3, 5]
    .map((index) => Number.parseInt(hex.slice(index, index + 2), 16) / 255)
    .map((channel) => (
      channel <= 0.04045
        ? channel / 12.92
        : ((channel + 0.055) / 1.055) ** 2.4
    ))
    .reduce((total, channel, index) => (
      total + channel * [0.2126, 0.7152, 0.0722][index]
    ), 0);
  const headerLuminance = luminance("#dce3e8");
  const mutedLuminance = luminance("#62625c");
  const contrast = (headerLuminance + 0.05) / (mutedLuminance + 0.05);
  assert.ok(contrast >= 4.5, `light header contrast is only ${contrast.toFixed(2)}:1`);

  const consumers = fs.readdirSync(stylesDir)
    .filter((name) => name.endsWith(".css"))
    .filter((name) => fs.readFileSync(path.join(stylesDir, name), "utf8").includes("var(--header-bg)"))
    .sort();
  assert.deepEqual(consumers, [
    "export.css",
    "image.css",
    "palette.css",
    "profiles.css",
    "solve.css",
  ]);
});

test("solve history controls and cards reserve stable confirmation geometry", () => {
  const html = fs.readFileSync(path.join(appDir, "index.html"), "utf8");
  const css = fs.readFileSync(path.join(appDir, "styles", "solve.css"), "utf8");
  const shellCss = fs.readFileSync(path.join(appDir, "styles", "shell.css"), "utf8");
  const controller = fs.readFileSync(
    path.join(appDir, "features", "solve", "controller.js"),
    "utf8",
  );
  const events = fs.readFileSync(
    path.join(appDir, "features", "event-bindings.js"),
    "utf8",
  );

  assert.equal((html.match(/solve-history-clear/g) || []).length, 2);
  assert.match(html, /compact-history-header deck-header solve-history-header/);
  assert.match(shellCss, /\.compact-history-clear\s*{[\s\S]*?width:\s*60px;[\s\S]*?min-width:\s*60px;[\s\S]*?white-space:\s*nowrap;/);
  assert.match(css, /\.solve-run-card-actions\s*{[\s\S]*?flex:\s*0 0 84px;[\s\S]*?width:\s*84px;/);
  assert.match(css, /\.solve-run-delete-slot\s*{[\s\S]*?width:\s*48px;[\s\S]*?min-width:\s*48px;/);
  assert.match(css, /\.solve-run-delete-btn\s*{[\s\S]*?width:\s*48px;[\s\S]*?min-width:\s*48px;[\s\S]*?border-color:\s*transparent;/);
  assert.doesNotMatch(css.match(/\.solve-run-card\s*{[\s\S]*?\}/)?.[0] || "", /transition:/);
  assert.doesNotMatch(controller, /loaded_from_archive|solve-run-loaded-badge/);
  assert.match(events, /railClearDeckBtn\.textContent = "Confirm\?"/);
  assert.match(events, /clearTimeout\(confirmTimer\)/);
});

test("printer configuration exposes derived width and whole-nozzle minimum length", () => {
  const html = fs.readFileSync(path.join(appDir, "index.html"), "utf8");
  const printerController = fs.readFileSync(
    path.join(appDir, "features", "printers", "index.js"),
    "utf8",
  );
  const settingsController = fs.readFileSync(
    path.join(appDir, "features", "settings", "profiles.js"),
    "utf8",
  );

  assert.doesNotMatch(html, /<th>Min W<\/th>/);
  assert.match(html, /Min Len \(× nozzle\)/);
  assert.match(
    printerController,
    /class="nz-min-ll-mult"[\s\S]*?step="1" min="2" max="10"/,
  );
  assert.match(printerController, /class="nz-min-ll-derived"/);
  assert.doesNotMatch(printerController, /class="nz-min-lw"/);
  assert.match(settingsController, /activePrintability\?\.minimum_extrusion_width_mm/);
  assert.match(settingsController, /activePrintability\?\.minimum_line_length_mm/);
  assert.equal(fs.existsSync(path.join(appDir, "printers.json")), false);
});

test("palette suggestions use solve-mode naming and compact deck-card geometry", () => {
  const html = fs.readFileSync(path.join(appDir, "index.html"), "utf8");
  const suggestions = fs.readFileSync(
    path.join(appDir, "features", "palette", "suggestions.js"),
    "utf8",
  );
  const deck = fs.readFileSync(
    path.join(appDir, "features", "palette", "deck.js"),
    "utf8",
  );
  const imageController = fs.readFileSync(
    path.join(appDir, "features", "image", "index.js"),
    "utf8",
  );
  const paletteCss = fs.readFileSync(path.join(appDir, "styles", "palette.css"), "utf8");
  const surfacesCss = fs.readFileSync(path.join(appDir, "styles", "surfaces.css"), "utf8");

  assert.match(html, /<label for="paletteSuggestMode">Solve mode<\/label>[\s\S]*?<option value="standard">Color<\/option>[\s\S]*?<option value="luminance_detail">Luminance<\/option>/);
  assert.doesNotMatch(html, /aria-labeledby=/);
  assert.doesNotMatch(html, /Source color|Luminance detail/);
  assert.match(suggestions, /modePrefix\s*=\s*paletteMode === "luminance_detail" \? "Luminance" : "Color"/);
  assert.match(deck, /class="deck-card compact-deck-card"/);
  assert.match(deck, /class="deck-card-header compact-deck-card-header"/);
  assert.match(deck, /class="deck-card-chips rail-deck-card-chips"/);
  assert.match(deck, /class="deck-card-palette-chips rail-deck-palette-chips"/);
  assert.match(deck, /class="deck-card-gamut rail-deck-card-meta"/);
  assert.doesNotMatch(deck, /<strong>\$\{app\.commands\.formatColorRmse\(g\)\}<\/strong>/);
  assert.match(deck, /resetStagingClearConfirm\(\{ sync: false \}\)/);
  assert.match(deck, /btn\.textContent = "!"/);
  assert.match(paletteCss, /--palette-suggestion-panel-width:\s*320px;/);
  assert.match(paletteCss, /#creationDeckPanel\s*{[\s\S]*?width:\s*var\(--palette-suggestion-panel-width\);/);
  assert.match(surfacesCss, /@media \(max-width: 980px\)[\s\S]*?\.creation-layout\s*{\s*flex-direction:\s*column;/);
  assert.match(imageController, /getComputedStyle\(layout\)\.flexDirection === "column"/);
});
