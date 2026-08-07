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
    .map((match) => path.resolve(path.dirname(file), match[1].split(/[?#]/, 1)[0]));
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

test("feature modules may compose within a domain but do not import across feature domains", () => {
  const featureDir = path.join(appDir, "features");
  const files = fs.readdirSync(featureDir, { recursive: true })
    .filter((name) => name.endsWith(".js"))
    .map((name) => path.join(featureDir, name));

  for (const file of files) {
    for (const dependency of importsFor(file)) {
      if (!dependency.startsWith(featureDir)) continue;
      const sourceDomain = path.relative(featureDir, file).split(path.sep)[0];
      const dependencyDomain = path.relative(featureDir, dependency).split(path.sep)[0];
      assert.equal(
        dependencyDomain,
        sourceDomain,
        `${path.relative(appDir, file)} imports another feature domain ${path.relative(appDir, dependency)}`,
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

test("solve history controls and cards keep compact stable confirmation geometry", () => {
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
  assert.match(shellCss, /\.compact-history-clear-slot\s*{[\s\S]*?flex:\s*0 0 42px;[\s\S]*?width:\s*42px;/);
  assert.match(shellCss, /\.compact-history-clear\s*{[\s\S]*?width:\s*42px;[\s\S]*?min-width:\s*42px;[\s\S]*?white-space:\s*nowrap;/);
  assert.match(css, /\.solve-run-card-actions\s*{[\s\S]*?flex:\s*0 0 54px;[\s\S]*?width:\s*54px;/);
  assert.doesNotMatch(css, /solve-run-use-palette-btn/);
  assert.match(css, /\.solve-run-delete-slot\s*{[\s\S]*?width:\s*18px;[\s\S]*?min-width:\s*18px;/);
  assert.match(shellCss, /\.rail-deck-remove,\s*\.compact-deck-card-remove\s*{[\s\S]*?width:\s*18px;[\s\S]*?min-width:\s*18px;[\s\S]*?color:\s*var\(--muted\);/);
  assert.doesNotMatch(css, /\.solve-run-delete-btn\s*{[\s\S]*?(?:border-color:\s*transparent|background:\s*transparent);/);
  assert.doesNotMatch(css.match(/\.solve-run-card\s*{[\s\S]*?\}/)?.[0] || "", /transition:/);
  assert.doesNotMatch(controller, /loaded_from_archive|solve-run-loaded-badge/);
  assert.match(controller, /armed \? "!" : app\.commands\.xIconSvg\(\)/);
  assert.match(controller, /btn\.textContent = armed \? "Clear\?" : "Clear"/);
  assert.match(events, /railClearDeckBtn\.textContent = "Clear\?"/);
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

  assert.match(html, /<label for="targetFilamentCount">Palette Colors<\/label>/);
  assert.match(html, /<label for="paletteSuggestMode">Solve Mode<\/label>[\s\S]*?<option value="standard">Color<\/option>[\s\S]*?<option value="luminance_detail">Luminance<\/option>/);
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
  assert.doesNotMatch(html, /id="suggestSlotHint"/);
  assert.match(paletteCss, /\.filament-card\.is-base-cap-reserved\s*{[^}]*var\(--white-filament-line\)[^}]*var\(--white-filament-surface\)/);
  assert.match(paletteCss, /--white-filament-ink:\s*#1f1f1b;/);
  assert.match(paletteCss, /--white-filament-label:\s*#62625c;/);
  assert.match(paletteCss, /\.ams-slot\.is-white \.ams-slot-name\s*{\s*color:\s*var\(--white-filament-ink\);\s*}/);
  assert.match(paletteCss, /\.ams-slot\.is-white \.ams-slot-label\s*{\s*color:\s*var\(--white-filament-label\);\s*}/);
});

test("changed frontend modules carry their current bootstrap cache versions", () => {
  const bootstrap = fs.readFileSync(path.join(appDir, "bootstrap.js"), "utf8");
  for (const [relativePath, version] of Object.entries({
    "features/shell/index.js": "2026-07-30-generator-basics-v3",
    "features/shell/theme.js": "2026-08-02-topbar-menu-switch-v1",
    "features/printers/index.js": "2026-08-04-saving-loading-fixes-v1",
    "core/application-context.js": "2026-08-04-settings-contract-v2",
    "features/image/index.js": "2026-08-04-saving-loading-fixes-v1",
    "features/palette/suggestions.js": "2026-08-04-saving-loading-fixes-v1",
    "features/palette/deck.js": "2026-08-02-exact-palette-suggestions-v1",
    "features/settings/controller.js": "2026-08-04-settings-contract-v5",
    "features/guides/registry.js": "2026-08-04-saving-loading-fixes-v1",
    "features/guides/targets.js": "2026-08-04-saving-loading-fixes-v1",
    "features/guides/controller.js": "2026-08-04-saving-loading-fixes-v1",
    "features/event-bindings.js": "2026-08-02-exact-palette-suggestions-v1",
    "features/palette/library.js": "2026-08-04-saving-loading-fixes-v1",
    "features/settings/profiles.js": "2026-08-04-settings-contract-v2",
    "features/settings/modules.js": "2026-08-04-settings-layout-v3",
    "features/settings/layout.js": "2026-08-04-settings-layout-v3",
    "features/solve/run.js": "2026-08-04-settings-review-v3",
    "features/solve/batch.js": "2026-08-04-saving-loading-fixes-v1",
    "features/solve/recipe-viewer.js": "2026-08-04-saving-loading-fixes-v1",
    "features/guides/overlay.js": "2026-08-04-saving-loading-fixes-v1",
    "features/application.js": "2026-08-04-saving-loading-fixes-v1",
    "api/index.js": "2026-08-04-saving-loading-fixes-v1",
  })) {
    assert.match(
      bootstrap,
      new RegExp(`${relativePath.replaceAll("/", "\\/").replace(".", "\\.")}\\?v=${version}`),
    );
  }
  const apiIndex = fs.readFileSync(path.join(appDir, "api", "index.js"), "utf8");
  for (const apiModule of [
    "cache.js", "client.js", "guides.js", "images.js", "jobs.js",
    "model-libraries.js", "modules.js", "printers.js", "runs.js",
    "session.js", "settings.js",
  ]) {
    assert.match(
      apiIndex,
      new RegExp(`${apiModule.replace(".", "\\.")}\\?v=2026-08-04-saving-loading-fixes-v1`),
    );
  }
});

test("extra-extra-small ghost actions share button geometry across element types", () => {
  const shellCss = fs.readFileSync(path.join(appDir, "styles", "shell.css"), "utf8");

  assert.match(
    shellCss,
    /\.ghost-button\.xxs\s*{[^}]*line-height:\s*normal;[^}]*}/,
  );
});

test("export folder actions keep their labels on one line", () => {
  const exportCss = fs.readFileSync(path.join(appDir, "styles", "export.css"), "utf8");

  assert.match(
    exportCss,
    /\.export-outdir-row \.open-export-folder-btn\s*{[^}]*white-space:\s*nowrap;[^}]*}/,
  );
});

test("main Solve is an accessible split action and Auto-Suggest owns no solve action", () => {
  const html = fs.readFileSync(path.join(appDir, "index.html"), "utf8");
  const batch = fs.readFileSync(
    path.join(appDir, "features", "solve", "batch.js"),
    "utf8",
  );
  const suggestions = fs.readFileSync(
    path.join(appDir, "features", "palette", "suggestions.js"),
    "utf8",
  );
  const deck = fs.readFileSync(
    path.join(appDir, "features", "palette", "deck.js"),
    "utf8",
  );

  assert.match(html, /id="solveModeMenuBtn"[\s\S]*?aria-haspopup="menu"/);
  assert.match(html, /id="solveModeMenu" role="menu" aria-label="Solve mode"/);
  assert.match(html, /role="menuitemradio" data-solve-mode="single"/);
  assert.match(html, /role="menuitemradio" data-solve-mode="batch"/);
  assert.match(batch, /ArrowDown[\s\S]*?ArrowUp[\s\S]*?Home[\s\S]*?End/);
  assert.match(batch, /event\.key === "Escape"/);
  assert.match(deck, /aria-multiselectable/);
  assert.match(deck, /is-batch-selected/);
  assert.doesNotMatch(suggestions, /paletteBatch|PaletteBatch|startPaletteBatch/);
  assert.doesNotMatch(html, new RegExp("solvePalette" + "BatchBtn"));
  assert.match(
    html,
    /class="modal-dialog modal-dialog-window surface-window app-dialog"[\s\S]*?role="dialog" aria-modal="true"[\s\S]*?aria-labelledby="appDialogTitle" aria-describedby="appDialogMsg"/,
  );
});

test("Palette Deck cards expose an accessible variant menu without replacing remove", () => {
  const html = fs.readFileSync(path.join(appDir, "index.html"), "utf8");
  const deck = fs.readFileSync(path.join(appDir, "features/palette/deck.js"), "utf8");

  assert.match(html, /id="deckCardMenu" role="menu" aria-label="Palette actions"/);
  assert.match(html, /data-deck-card-action="variant"[\s\S]*?Create Variant/);
  assert.match(html, /data-deck-card-action="save"[\s\S]*?Save Palette/);
  assert.match(deck, /class="ghost-button xxs rail-deck-menu-button"/);
  assert.match(deck, /aria-haspopup="menu" aria-expanded="false" aria-controls="deckCardMenu"/);
  assert.match(deck, /class="ghost-button xxs rail-deck-remove compact-deck-card-remove"/);
  assert.match(deck, /event\.key === "Escape"/);
});

test("abandoned suggestion-owned batch surface is absent", () => {
  const files = [
    "index.html",
    "features/palette/suggestions.js",
    "features/solve/batch.js",
    "features/solve/controller.js",
    "styles/solve.css",
  ];
  const combined = files
    .map(relative => fs.readFileSync(path.join(appDir, relative), "utf8"))
    .join("\n");
  const abandoned = [
    "Solve " + "Top",
    "solvePalette" + "BatchBtn",
    "stagePaletteBatch" + "Candidates",
    "fetchPaletteBatch" + "CandidateResult",
    "batch_" + "rank",
    "Use " + "Palette",
  ];
  for (const relic of abandoned) assert.doesNotMatch(combined, new RegExp(relic));
});

test("retired swap-tier suggestion controls and styles are absent", () => {
  const files = [
    "index.html",
    "core/application-context.js",
    "features/event-bindings.js",
    "features/image/index.js",
    "features/palette/suggestions.js",
    "features/settings/controller.js",
    "features/settings/profiles.js",
    "features/guides/targets.js",
    "styles/palette.css",
  ];
  const combined = files
    .map(relative => fs.readFileSync(path.join(appDir, relative), "utf8"))
    .join("\n");
  for (const retired of [
    "targetSwapCount",
    "paletteSwapThreshold",
    "paletteForceAllTiers",
    "swap_improvement_threshold",
    "force_all_tiers",
    "creation-settings-shell",
    "creation-settings-card",
  ]) {
    assert.doesNotMatch(combined, new RegExp(retired));
  }
  assert.match(combined, /id="targetFilamentCount"[\s\S]*?min="2" max="16"/);
  assert.match(combined, /id="amsPreview"[\s\S]*?aria-live="polite"[\s\S]*?aria-atomic="true"/);
});
