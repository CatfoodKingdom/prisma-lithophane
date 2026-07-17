"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

const appDir = path.resolve(__dirname, "../../Prisma/generator/app");
const moduleUrl = (relativePath) => pathToFileURL(path.join(appDir, relativePath)).href;

function rgb(hex) {
  return [1, 3, 5].map((offset) => Number.parseInt(hex.slice(offset, offset + 2), 16));
}

function luminance(hex) {
  const channels = rgb(hex).map((value) => {
    const normalized = value / 255;
    return normalized <= 0.04045 ? normalized / 12.92 : ((normalized + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
}

function contrast(left, right) {
  const [bright, dark] = [luminance(left), luminance(right)].sort((a, b) => b - a);
  return (bright + 0.05) / (dark + 0.05);
}

function eventWith(type, values) {
  const event = new Event(type);
  Object.assign(event, values);
  return event;
}

async function createThemeHarness({ stored = "system", storageThrows = false, systemDark = false } = {}) {
  const { createLifecycle } = await import(moduleUrl("core/lifecycle.js"));
  const { installFeaturesShellTheme } = await import(moduleUrl("features/shell/theme.js"));
  const writes = [];
  const app = {
    commands: {},
    lifecycle: createLifecycle(),
    persistence: {
      read() { if (storageThrows) throw new Error("blocked"); return stored; },
      write(key, value) { if (storageThrows) throw new Error("blocked"); writes.push([key, value]); },
    },
    state: { ui: { $: () => null, themePreference: "system", themeResolved: "light" } },
  };
  installFeaturesShellTheme(app);
  const root = { dataset: {}, style: {} };
  const mediaQuery = new EventTarget();
  mediaQuery.matches = systemDark;
  const storageEvents = new EventTarget();
  const documentEvents = new EventTarget();
  const viewport = new EventTarget();
  app.commands.initializeThemeController({
    documentRoot: root,
    systemTheme: mediaQuery,
    storageEvents,
    documentEvents,
    viewport,
  });
  return { app, documentEvents, mediaQuery, root, storageEvents, viewport, writes };
}

test("theme preference validation and resolution are deterministic", async () => {
  const { applyResolvedTheme, normalizeThemePreference, resolveTheme } = await import(moduleUrl("core/theme.js"));
  assert.equal(normalizeThemePreference("dark"), "dark");
  assert.equal(normalizeThemePreference("sepia"), "system");
  assert.equal(resolveTheme("system", true), "dark");
  assert.equal(resolveTheme("system", false), "light");
  assert.equal(resolveTheme("light", true), "light");

  const root = { dataset: {}, style: {} };
  assert.equal(applyResolvedTheme(root, "invalid", true), "dark");
  assert.deepEqual(root.dataset, { themePreference: "system", theme: "dark" });
  assert.equal(root.style.colorScheme, "dark");
});

test("stored explicit theme wins over the operating-system scheme", async () => {
  const { app, mediaQuery, root } = await createThemeHarness({ stored: "light", systemDark: true });
  assert.equal(app.state.ui.themePreference, "light");
  assert.equal(root.dataset.theme, "light");

  mediaQuery.matches = false;
  mediaQuery.dispatchEvent(new Event("change"));
  assert.equal(root.dataset.theme, "light");
});

test("System follows live scheme changes while explicit preferences do not", async () => {
  const { app, mediaQuery, root, writes } = await createThemeHarness({ systemDark: false });
  mediaQuery.matches = true;
  mediaQuery.dispatchEvent(new Event("change"));
  assert.equal(root.dataset.theme, "dark");

  app.commands.applyThemePreference("light");
  assert.deepEqual(writes.at(-1), ["prisma_generator_theme", "light"]);
  mediaQuery.matches = false;
  mediaQuery.dispatchEvent(new Event("change"));
  assert.equal(root.dataset.theme, "light");
});

test("storage synchronization validates remote preferences without writing back", async () => {
  const { app, root, storageEvents, writes } = await createThemeHarness();
  storageEvents.dispatchEvent(eventWith("storage", { key: "prisma_generator_theme", newValue: "dark" }));
  assert.equal(app.state.ui.themePreference, "dark");
  assert.equal(root.dataset.theme, "dark");
  assert.deepEqual(writes, []);

  storageEvents.dispatchEvent(eventWith("storage", { key: "prisma_generator_theme", newValue: "sepia" }));
  assert.equal(app.state.ui.themePreference, "system");
});

test("blocked storage is non-fatal and lifecycle disposal removes system ownership", async () => {
  const { app, mediaQuery, root } = await createThemeHarness({ storageThrows: true, systemDark: false });
  assert.doesNotThrow(() => app.commands.applyThemePreference("dark"));
  assert.equal(root.dataset.theme, "dark");
  app.commands.applyThemePreference("system", { persist: false });
  app.lifecycle.dispose();
  mediaQuery.matches = true;
  mediaQuery.dispatchEvent(new Event("change"));
  assert.equal(root.dataset.theme, "light");
});

test("dark theme ordinary text and control pairs meet WCAG AA contrast", () => {
  const source = fs.readFileSync(path.join(appDir, "styles/tokens.css"), "utf8");
  const block = source.match(/:root\[data-theme="dark"\]\s*\{([\s\S]*?)\}/)?.[1] || "";
  const tokens = Object.fromEntries(
    [...block.matchAll(/--([\w-]+):\s*(#[0-9a-f]{6})\s*;/gi)].map((match) => [match[1], match[2]]),
  );
  const pairs = [
    ["ink", "bg"],
    ["ink", "panel"],
    ["muted", "panel"],
    ["accent", "bg"],
    ["on-accent", "accent-fill"],
    ["table-head-ink", "table-head"],
    ["ok-ink", "ok-bg"],
    ["warn-ink", "warn-bg"],
    ["idle-ink", "idle-bg"],
    ["error-ink", "error-bg"],
    ["inverse-ink", "inverse-bg"],
  ];
  for (const [foreground, background] of pairs) {
    assert.ok(tokens[foreground], `missing dark token --${foreground}`);
    assert.ok(tokens[background], `missing dark token --${background}`);
    assert.ok(
      contrast(tokens[foreground], tokens[background]) >= 4.5,
      `--${foreground} on --${background} must meet 4.5:1 contrast`,
    );
  }
});
