"use strict";

const path = require("node:path");
const { pathToFileURL } = require("node:url");
const { TEST_SETTINGS_CONTRACT } = require("./settings_contract_fixture.cjs");

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
  "features/settings/contract.js",
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
  "features/guides/actions/registry.js",
  "features/guides/actions/workspace.js",
  "features/guides/targets.js",
  "features/guides/registry.js",
  "features/guides/overlay.js",
  "features/guides/controller.js",
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
    closest() { return null; },
    focus() {},
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
  const root = {
    querySelector(selector) {
      if (!elements[selector]) elements[selector] = fakeElement();
      return elements[selector];
    },
    querySelectorAll() { return []; },
  };
  const { createApplicationContext, initializeApplicationState } = await import(
    moduleUrl("core/application-context.js")
  );
  let guideRuntimeSnapshot = null;
  const guideApi = {
    setRequestContext() {},
    async acquireGuideRuntime() {
      return { workspace_epoch: 0, lease: { lease_id: "test-lease", owned_by_page: true } };
    },
    async releaseGuideRuntime() { return { workspace_epoch: 0, lease: null, session: null }; },
    async beginGuideRuntime(payload) {
      guideRuntimeSnapshot = payload.clientSnapshot;
      return {
        session_id: "test-guide-session",
        workspace_epoch: 0,
        images_folder: "C:\\PrismaRuntime\\Images",
      };
    },
    async resetGuideRuntime() { return { removed: 0, config: {} }; },
    async mountGuidePrinter() {
      return {
        profile: {
          id: "tutorial-printer", name: "Tutorial Printer", max_print_area: { x: 256, y: 256 },
          nozzle_profiles: [
            { id: "nozzle-200", diameter_um: 200, min_layer_height_um: 50, max_layer_height_um: 150, max_extrusion_width_um: 250, minimum_line_length_multiplier: 2 },
            { id: "nozzle-400", diameter_um: 400, min_layer_height_um: 80, max_layer_height_um: 320, max_extrusion_width_um: 500, minimum_line_length_multiplier: 2 },
          ],
        },
      };
    },
    async mountGuideAsset() {
      return {
        asset_id: "bubba-blanket", filename: "Prisma Tutorial - Bubba Blanket.jpg",
        width: 1200, height: 1600, size_kb: 100, source_format: "jpeg",
        source_ref: "guide-image:bubba-blanket", virtual: true, deletable: false, renameable: false,
      };
    },
    async heartbeatGuideRuntime() { return {}; },
    async claimGuideRuntimeRecovery() { return {}; },
    async abandonGuideRuntime() { guideRuntimeSnapshot = null; return { workspace_epoch: 1 }; },
    async openGuideRuntimeConfigFolder() { return { opened: true }; },
    async fetchGuideRuntime() {
      return {
        workspace_epoch: 0,
        session: guideRuntimeSnapshot
          ? { session_id: "test-guide-session", snapshot: { client: guideRuntimeSnapshot } }
          : null,
      };
    },
    async restoreGuideRuntimeServer() { return {}; },
    async finalizeGuideRuntime() { guideRuntimeSnapshot = null; return { workspace_epoch: 1 }; },
    async getSolveStatus() { return { status: "idle" }; },
    async getExportStatus() { return { status: "idle" }; },
    async apiFetch() { return { status: "idle" }; },
    async apiPost() { return { requested: true }; },
    async cancelSolve() { return { requested: true }; },
    async cancelExport() { return { requested: true }; },
    async updateConfig(config) { return { config }; },
    imagePreviewUrl(filename) { return `/api/images/preview/${encodeURIComponent(filename)}`; },
    async setActivePrinter() { return {}; },
    async registerGuideJob() { return { owned_jobs: {} }; },
  };
  const app = createApplicationContext({
    api: { ...guideApi, ...api },
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
  app.commands.hydrateSettingsContract(TEST_SETTINGS_CONTRACT);
  return { app, elements, storage };
}

module.exports = {
  appDir,
  createFeatureHarness,
  fakeElement,
  memoryStorage,
  moduleUrl,
};
