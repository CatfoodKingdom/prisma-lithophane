"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { createFeatureHarness } = require("./support/application_harness.cjs");

let app;
test.before(async () => { ({ app } = await createFeatureHarness()); });

test("computeLightboxScaleBounds covers aspect, header, resize, and collapsed ranges", () => {
  const bounds = app.commands.computeLightboxScaleBounds;
  const landscape = bounds({
    intrinsicWidth: 150, intrinsicHeight: 100,
    viewportWidth: 1000, viewportHeight: 700, headerHeight: 44, headerMinWidth: 420,
  });
  assert.equal(landscape.minScale, 1);
  assert.equal(landscape.maxScale, 6.08);
  assert.equal(landscape.headerWidth, 420);

  const portrait = bounds({
    intrinsicWidth: 100, intrinsicHeight: 150,
    viewportWidth: 1000, viewportHeight: 700, headerHeight: 44,
  });
  assert.ok(Math.abs(portrait.maxScale - (608 / 150)) < 1e-9);

  const large = bounds({
    intrinsicWidth: 2000, intrinsicHeight: 1500,
    viewportWidth: 800, viewportHeight: 600, headerHeight: 50,
  });
  assert.equal(large.collapsed, true);
  assert.equal(large.minScale, large.maxScale);
  assert.ok(large.maxScale < 1);
});

test("wheel normalization accumulates trackpad deltas and clamps endpoints", () => {
  assert.equal(app.commands.normalizeStaticZoomWheelDelta({ deltaY: 2, deltaMode: 1 }, 900), 32);
  assert.equal(app.commands.normalizeStaticZoomWheelDelta({ deltaY: 1, deltaMode: 2 }, 900), 900);
  let state = { value: 50, accumulatedDelta: 0 };
  for (let index = 0; index < 3; index += 1) {
    state = app.commands.applyStaticZoomWheelDelta(state.value, state.accumulatedDelta, -10);
    assert.equal(state.changed, false);
  }
  state = app.commands.applyStaticZoomWheelDelta(state.value, state.accumulatedDelta, -10);
  assert.equal(state.value, 55);
  assert.equal(state.changed, true);
  assert.deepEqual(app.commands.applyStaticZoomWheelDelta(100, 0, -100), {
    value: 100, accumulatedDelta: 0, changed: false,
  });
});

class FakeElement {
  constructor() {
    this.listeners = new Map(); this.style = {}; this.attributes = new Map();
    this.value = "100"; this.disabled = false; this.complete = false;
    this.naturalWidth = 0; this.naturalHeight = 0; this.scrollWidth = 0;
    this.rectHeight = 0; this.queries = new Map();
  }
  querySelector(selector) { return this.queries.get(selector) || null; }
  addEventListener(type, handler) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type).add(handler);
  }
  removeEventListener(type, handler) { this.listeners.get(type)?.delete(handler); }
  dispatch(type, event = {}) { for (const handler of this.listeners.get(type) || []) handler(event); }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getBoundingClientRect() { return { height: this.rectHeight }; }
  decode() { return Promise.resolve(); }
}

function staticLightboxFixture() {
  const content = new FakeElement(); const pane = new FakeElement();
  const header = new FakeElement(); const media = new FakeElement();
  const image = new FakeElement(); const slider = new FakeElement();
  image.complete = true; image.naturalWidth = 150; image.naturalHeight = 100;
  header.rectHeight = 44; header.scrollWidth = 420;
  content.queries.set(".comp-lightbox-pane", pane);
  pane.queries.set(".comp-lightbox-topbar", header);
  pane.queries.set(".static-zoom-media", media);
  media.queries.set(".comp-lightbox-img", image);
  header.queries.set(".comp-lightbox-zoom-slider", slider);
  return { content, media, image, slider };
}

test("static controller preserves normalized resize state and disposes listeners", () => {
  const previousWindow = global.window;
  const previousResizeObserver = global.ResizeObserver;
  const fakeWindow = new FakeElement();
  fakeWindow.innerWidth = 1000; fakeWindow.innerHeight = 700;
  class FakeResizeObserver { observe() {} disconnect() { this.disconnected = true; } }
  global.window = fakeWindow;
  global.ResizeObserver = FakeResizeObserver;
  try {
    const fixture = staticLightboxFixture();
    const lifecycle = app.commands.beginLightboxLifecycle();
    app.commands.setupStaticLightboxZoom(fixture.content, lifecycle);
    assert.equal(fixture.slider.value, "100");
    assert.equal(fixture.image.style.width, "912px");

    fixture.slider.value = "0";
    fixture.slider.dispatch("input");
    assert.equal(fixture.image.style.width, "150px");

    fixture.slider.value = "37";
    fakeWindow.innerWidth = 700; fakeWindow.innerHeight = 500;
    fakeWindow.dispatch("resize");
    assert.equal(fixture.slider.value, "37");

    app.commands.beginLightboxLifecycle();
    assert.equal(fixture.media.listeners.get("wheel").size, 0);
    assert.equal(fixture.slider.listeners.get("input").size, 0);
    assert.equal(lifecycle.isActive(), false);
  } finally {
    global.window = previousWindow;
    global.ResizeObserver = previousResizeObserver;
  }
});

test("static zoom is opt-in and safely ignores incompatible lightboxes", () => {
  const lifecycle = app.commands.beginLightboxLifecycle();
  assert.doesNotThrow(() => app.commands.setupStaticLightboxZoom(new FakeElement(), lifecycle));
  assert.match(app.commands.buildStaticLightboxZoomControls(), /aria-label="Zoom"/);
});
