"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

const coreDir = path.resolve(__dirname, "../../Prisma/generator/app/core");

function importCore(name) {
  return import(pathToFileURL(path.join(coreDir, name)).href);
}

function memoryStorage(initial = {}) {
  const values = new Map(Object.entries(initial));
  return {
    getItem(key) { return values.has(key) ? values.get(key) : null; },
    setItem(key, value) { values.set(key, String(value)); },
    removeItem(key) { values.delete(key); },
  };
}

test("lifecycle disposal removes owned listeners and is idempotent", async () => {
  const { createLifecycle } = await importCore("lifecycle.js");
  const lifecycle = createLifecycle();
  const target = new EventTarget();
  let calls = 0;

  lifecycle.listen(target, "change", () => { calls += 1; });
  target.dispatchEvent(new Event("change"));
  lifecycle.dispose();
  lifecycle.dispose();
  target.dispatchEvent(new Event("change"));

  assert.equal(calls, 1);
  assert.equal(lifecycle.disposed, true);
  assert.throws(() => lifecycle.listen(target, "change", () => {}), /disposed lifecycle/);
});

test("store updates one named slice and notifies only its subscribers", async () => {
  const { createStore } = await importCore("store.js");
  const store = createStore({ session: { ready: false }, ui: { tab: "image" } });
  const notifications = [];
  const unsubscribe = store.subscribe("session", (slice, state) => {
    notifications.push([slice.ready, state.ui.tab]);
  });

  store.update("session", (session) => { session.ready = true; });
  store.update("ui", (ui) => { ui.tab = "solve"; });
  unsubscribe();
  store.update("session", (session) => { session.ready = false; });

  assert.deepEqual(notifications, [[true, "image"]]);
  assert.equal(store.getState().ui.tab, "solve");
  assert.throws(() => store.update("missing", () => {}), /Unknown state slice/);
});

test("persistence provides string and JSON storage with safe fallbacks", async () => {
  const { createPersistence } = await importCore("persistence.js");
  const storage = memoryStorage({ malformed: "{" });
  const persistence = createPersistence(storage);

  assert.equal(persistence.read("missing", "fallback"), "fallback");
  assert.deepEqual(persistence.readJson("malformed", { safe: true }), { safe: true });
  persistence.write("flag", true);
  persistence.writeJson("profile", { name: "draft" });
  assert.equal(persistence.read("flag"), "true");
  assert.deepEqual(persistence.readJson("profile", null), { name: "draft" });
  persistence.remove("profile");
  assert.equal(persistence.read("profile"), null);
});
