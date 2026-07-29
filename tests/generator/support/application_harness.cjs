"use strict";

const path = require("node:path");
const { pathToFileURL } = require("node:url");

const appDir = path.resolve(__dirname, "../../../Prisma/generator/app");
const defaultFeatures = [
  "features/shell/theme.js",
  "features/shell/index.js",
  "features/printers/index.js",
  "features/image/index.js",
  "features/palette/index.js",
  "features/palette/suggestions.js",
  "features/palette/deck.js",
  "features/palette/library.js",
  "features/palette/model-libraries.js",
  "features/settings/controller.js",
  "features/settings/profiles.js",
  "features/solve/controller.js",
  "features/solve/batch.js",
  "features/solve/comparison.js",
  "features/solve/run.js",
  "features/solve/diagnostics.js",
  "features/solve/lightbox.js",
  "features/solve/recipe-viewer.js",
  "features/settings/modules.js",
  "features/settings/layout.js",
];

function moduleUrl(relativePath) {
  return pathToFileURL(path.join(appDir, relativePath)).href;
}

function memoryStorage(initial = {}) {
  const values = new Map(Object.entries(initial));
  return {
    getItem(key) { return values.has(key) ? values.get(key) : null; },
    setItem(key, value) { values.set(key, String(value)); },
    removeItem(key) { values.delete(key); },
    values,
  };
}

function fakeElement() {
  const classes = new Set();
  return {
    ariaHidden: "true",
    checked: false,
    children: [],
    classList: {
      add(...names) { names.forEach((name) => classes.add(name)); },
      remove(...names) { names.forEach((name) => classes.delete(name)); },
      contains(name) { return classes.has(name); },
      toggle(name, force) {
        const enabled = force === undefined ? !classes.has(name) : Boolean(force);
        if (enabled) classes.add(name); else classes.delete(name);
        return enabled;
      },
    },
    dataset: {},
    disabled: false,
    hidden: false,
    innerHTML: "",
    style: {},
    textContent: "",
    value: "",
    addEventListener() {},
    appendChild(child) { this.children.push(child); return child; },
    getAttribute(name) { return this[name] ?? null; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    removeAttribute(name) { delete this[name]; },
    removeEventListener() {},
    setAttribute(name, value) { this[name] = String(value); },
  };
}

async function createFeatureHarness({
  api = {},
  elements = {},
  filaments = [],
  features = defaultFeatures,
  services = {},
  storage = memoryStorage(),
} = {}) {
  const fallbackElement = fakeElement();
  const root = {
    querySelector(selector) { return elements[selector] || fallbackElement; },
    querySelectorAll() { return []; },
  };
  const { createApplicationContext, initializeApplicationState } = await import(
    moduleUrl("core/application-context.js")
  );
  const app = createApplicationContext({
    api,
    data: { STATIC_FILAMENTS: filaments },
    services: { pollJobUntilTerminal: async () => ({}), ...services },
    root,
    storage,
  });

  for (const relativePath of features) {
    const feature = await import(moduleUrl(relativePath));
    const installer = Object.entries(feature).find(
      ([name, value]) => name.startsWith("installFeatures") && typeof value === "function",
    )?.[1];
    if (!installer) throw new Error(`No feature installer exported by ${relativePath}`);
    installer(app);
  }
  initializeApplicationState(app);
  app.state.ui.$ = (selector) => root.querySelector(selector);
  app.state.ui.$$ = (selector) => root.querySelectorAll(selector);
  return { app, elements, storage };
}

module.exports = {
  appDir,
  createFeatureHarness,
  fakeElement,
  memoryStorage,
  moduleUrl,
};
