import assert from "node:assert/strict";
import test from "node:test";

import { pollJobUntilTerminal } from "../../Prisma/generator/app/core/polling.js";
import { installFeaturesImageImports } from "../../Prisma/generator/app/features/image/imports.js";

class FakeClassList {
  constructor() { this.values = new Set(); }
  add(...values) { values.forEach((value) => this.values.add(value)); }
  remove(...values) { values.forEach((value) => this.values.delete(value)); }
  contains(value) { return this.values.has(value); }
  toggle(value, force) {
    if (force === undefined ? !this.values.has(value) : force) this.values.add(value);
    else this.values.delete(value);
  }
}

class FakeElement {
  constructor() {
    this.classList = new FakeClassList();
    this.hidden = false;
    this.style = {};
    this.textContent = "";
    this.innerHTML = "";
    this.dataset = {};
    this.children = new Map();
  }
  addEventListener() {}
  removeEventListener() {}
  setAttribute() {}
  focus() {}
  click() {}
  querySelector(selector) { return this.children.get(selector) || null; }
}

function makeApp(statuses) {
  const elements = new Map();
  for (const id of [
    "imageImportNotice", "imageImportDetailsBtn", "imageImportIssuesModal",
    "imageImportIssuesList", "imageImportRetryBtn", "imageImportIssuesClose",
    "imageImportIssuesDone", "imageImportOpenFolderBtn", "imageUploadInput",
  ]) elements.set(`#${id}`, new FakeElement());
  const notice = elements.get("#imageImportNotice");
  for (const selector of [
    "#imageImportNoticeText", "#imageImportProgress", "#imageImportProgressFill",
    "#imageImportDetailsBtn",
  ]) notice.children.set(selector, elements.get(selector) || new FakeElement());
  notice.classList.add("is-hidden");
  elements.get("#imageImportIssuesModal").classList.add("is-hidden");

  const toasts = [];
  const app = {
    api: {
      getImageImportStatus: async () => statuses.shift(),
      refreshImages: async () => ({ batch_id: "empty", job_id: "empty", status: "complete", total: 0 }),
      importImages: async () => ({ batch_id: "batch-1", job_id: "batch-1", status: "queued", total: 1, succeeded: 0, failed: 0, items: [] }),
    },
    services: { pollJobUntilTerminal },
    lifecycle: { listen() {} },
    state: {
      image: {
        activeImportBatchId: null,
        importBatch: null,
        importPollingError: "",
        availableImages: [],
        selectedImage: null,
      },
      ui: {
        $: (selector) => elements.get(selector) || null,
        $$: () => [],
      },
    },
    commands: {
      esc: (value) => String(value),
      handleOpenImageLibraryFolder() {},
      refreshImageLibrary: async () => {
        app.state.image.availableImages = [{ filename: "phone.heic", width: 29, height: 100 }];
      },
      applyImageAspectDefault: () => { app.aspectDefaults += 1; },
      renderImageTab: () => { app.renders += 1; },
      updateRail: () => {},
      showToast: (message, kind) => toasts.push({ message, kind }),
    },
    aspectDefaults: 0,
    renders: 0,
    toasts,
  };
  installFeaturesImageImports(app);
  return app;
}

globalThis.document = new FakeElement();
globalThis.requestAnimationFrame = (callback) => callback();

test("completed batch refreshes the library and selects the first successful image", async () => {
  const terminal = {
    batch_id: "batch-1",
    job_id: "batch-1",
    origin: "upload",
    status: "complete",
    total: 1,
    completed: 1,
    succeeded: 1,
    failed: 0,
    current_filename: null,
    items: [{ requested_name: "phone.heic", stored_name: "phone.heic", status: "complete" }],
  };
  const app = makeApp([terminal]);

  await app.commands.pollImageImport(
    { ...terminal, status: "queued", completed: 0, succeeded: 0 },
    { selectFirst: true },
  );

  assert.equal(app.state.image.selectedImage.filename, "phone.heic");
  assert.equal(app.aspectDefaults, 1);
  assert.deepEqual(app.toasts.at(-1), { message: "Added 1 image", kind: "success" });
  assert.equal(app.state.image.activeImportBatchId, null);
});

test("partial import keeps an actionable warning and does not hide failures", async () => {
  const terminal = {
    batch_id: "batch-2",
    job_id: "batch-2",
    origin: "upload",
    status: "partial",
    total: 2,
    completed: 2,
    succeeded: 1,
    failed: 1,
    current_filename: null,
    items: [
      { requested_name: "good.png", stored_name: "good.png", status: "complete" },
      { requested_name: "bad.heic", stored_name: null, status: "failed", error: "Cannot decode" },
    ],
  };
  const app = makeApp([terminal]);

  await app.commands.pollImageImport(
    { ...terminal, status: "queued", completed: 0, succeeded: 0, failed: 0 },
  );

  const notice = app.state.ui.$("#imageImportNotice");
  assert.equal(notice.classList.contains("is-hidden"), false);
  assert.equal(notice.classList.contains("is-warning"), true);
  assert.deepEqual(app.toasts.at(-1), {
    message: "Added 1 image; 1 could not be prepared",
    kind: "warn",
  });
});

test("refresh with no conversion work preserves the direct refresh behavior", async () => {
  const app = makeApp([]);

  const result = await app.commands.startFolderImageRefresh({ announce: true });

  assert.equal(result.total, 0);
  assert.equal(app.state.image.availableImages[0].filename, "phone.heic");
  assert.equal(app.state.image.importBatch, null);
});
