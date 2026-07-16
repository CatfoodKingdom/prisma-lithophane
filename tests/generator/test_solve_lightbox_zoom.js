const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const APP_JS = fs.readFileSync(
  path.resolve(__dirname, '../../Prisma/generator/app/app.js'),
  'utf8',
);

function zoomRuntime(windowObject, ResizeObserverClass = undefined) {
  const start = APP_JS.indexOf('let _lightboxCleanup = null;');
  const end = APP_JS.indexOf('function openSolveRunLightbox', start);
  assert.ok(start >= 0 && end > start, 'expected the pure zoom/lifecycle block');
  return Function(
    'window',
    'ResizeObserver',
    `${APP_JS.slice(start, end)}; return {
      beginLightboxLifecycle,
      computeLightboxScaleBounds,
      normalizeStaticZoomWheelDelta,
      applyStaticZoomWheelDelta,
      setupStaticLightboxZoom,
    };`,
  )(windowObject, ResizeObserverClass);
}

test('computeLightboxScaleBounds covers aspect, header, resize, and collapsed ranges', () => {
  const { computeLightboxScaleBounds } = zoomRuntime({ innerHeight: 700 });
  const landscape = computeLightboxScaleBounds({
    intrinsicWidth: 150, intrinsicHeight: 100,
    viewportWidth: 1000, viewportHeight: 700, headerHeight: 44, headerMinWidth: 420,
  });
  assert.equal(landscape.minScale, 1);
  assert.equal(landscape.maxScale, 6.08);
  assert.equal(landscape.headerWidth, 420);

  const portrait = computeLightboxScaleBounds({
    intrinsicWidth: 100, intrinsicHeight: 150,
    viewportWidth: 1000, viewportHeight: 700, headerHeight: 44,
  });
  assert.ok(Math.abs(portrait.maxScale - (608 / 150)) < 1e-9);

  const headerWide = computeLightboxScaleBounds({
    intrinsicWidth: 40, intrinsicHeight: 40,
    viewportWidth: 800, viewportHeight: 600, headerHeight: 50, headerMinWidth: 700,
  });
  assert.equal(headerWide.headerWidth, 700);

  const large = computeLightboxScaleBounds({
    intrinsicWidth: 2000, intrinsicHeight: 1500,
    viewportWidth: 800, viewportHeight: 600, headerHeight: 50,
  });
  assert.equal(large.collapsed, true);
  assert.equal(large.minScale, large.maxScale);
  assert.ok(large.maxScale < 1);

  const resized = computeLightboxScaleBounds({
    intrinsicWidth: 150, intrinsicHeight: 100,
    viewportWidth: 500, viewportHeight: 400, headerHeight: 44,
  });
  assert.ok(resized.maxScale < landscape.maxScale);
});

test('wheel normalization accumulates trackpad deltas, clamps endpoints, and handles modes', () => {
  const runtime = zoomRuntime({ innerHeight: 900 });
  assert.equal(runtime.normalizeStaticZoomWheelDelta({ deltaY: 2, deltaMode: 1 }, 900), 32);
  assert.equal(runtime.normalizeStaticZoomWheelDelta({ deltaY: 1, deltaMode: 2 }, 900), 900);

  let state = { value: 50, accumulatedDelta: 0 };
  for (let index = 0; index < 3; index += 1) {
    state = runtime.applyStaticZoomWheelDelta(state.value, state.accumulatedDelta, -10);
    assert.equal(state.changed, false);
  }
  state = runtime.applyStaticZoomWheelDelta(state.value, state.accumulatedDelta, -10);
  assert.equal(state.value, 55);
  assert.equal(state.changed, true);
  const clamped = runtime.applyStaticZoomWheelDelta(100, 0, -100);
  assert.equal(clamped.value, 100);
  assert.equal(clamped.changed, false);
});

class FakeElement {
  constructor() {
    this.listeners = new Map();
    this.style = {};
    this.attributes = new Map();
    this.value = '100';
    this.disabled = false;
    this.complete = false;
    this.naturalWidth = 0;
    this.naturalHeight = 0;
    this.scrollWidth = 0;
    this.rectHeight = 0;
    this.queries = new Map();
  }
  querySelector(selector) { return this.queries.get(selector) || null; }
  addEventListener(type, handler) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type).add(handler);
  }
  removeEventListener(type, handler) { this.listeners.get(type)?.delete(handler); }
  dispatch(type, event = {}) {
    for (const handler of this.listeners.get(type) || []) handler(event);
  }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getBoundingClientRect() { return { height: this.rectHeight }; }
  decode() { return Promise.resolve(); }
}

function staticLightboxFixture() {
  const content = new FakeElement();
  const pane = new FakeElement();
  const header = new FakeElement();
  const media = new FakeElement();
  const image = new FakeElement();
  const slider = new FakeElement();
  image.complete = true;
  image.naturalWidth = 150;
  image.naturalHeight = 100;
  header.rectHeight = 44;
  header.scrollWidth = 420;
  content.queries.set('.comp-lightbox-pane', pane);
  pane.queries.set('.comp-lightbox-topbar', header);
  pane.queries.set('.static-zoom-media', media);
  media.queries.set('.comp-lightbox-img', image);
  header.queries.set('.comp-lightbox-zoom-slider', slider);
  return { content, pane, header, media, image, slider };
}

test('static controller defaults to Max, preserves normalized resize state, isolates modifiers, and cleans up', () => {
  const fakeWindow = new FakeElement();
  fakeWindow.innerWidth = 1000;
  fakeWindow.innerHeight = 700;
  class FakeResizeObserver {
    observe() {}
    disconnect() { this.disconnected = true; }
  }
  const runtime = zoomRuntime(fakeWindow, FakeResizeObserver);
  const fixture = staticLightboxFixture();
  const lifecycle = runtime.beginLightboxLifecycle();
  runtime.setupStaticLightboxZoom(fixture.content, lifecycle);

  assert.equal(fixture.slider.value, '100');
  assert.equal(fixture.image.style.width, '912px');
  assert.match(fixture.slider.attributes.get('aria-valuetext'), /maximum/);

  fixture.slider.value = '0';
  fixture.slider.dispatch('input');
  assert.equal(fixture.image.style.width, '150px');
  assert.match(fixture.slider.attributes.get('aria-valuetext'), /minimum/);

  let prevented = false;
  fixture.media.dispatch('wheel', {
    deltaY: -72, deltaMode: 0, ctrlKey: false, metaKey: false,
    preventDefault() { prevented = true; },
  });
  assert.equal(fixture.slider.value, '10');
  assert.equal(prevented, true);

  const beforeModifier = fixture.slider.value;
  fixture.media.dispatch('wheel', {
    deltaY: -360, deltaMode: 0, ctrlKey: true, metaKey: false,
    preventDefault() { throw new Error('browser zoom gesture must not be consumed'); },
  });
  assert.equal(fixture.slider.value, beforeModifier);

  fixture.slider.value = '37';
  fakeWindow.innerWidth = 700;
  fakeWindow.innerHeight = 500;
  fakeWindow.dispatch('resize');
  assert.equal(fixture.slider.value, '37', 'resize preserves normalized position');

  runtime.beginLightboxLifecycle();
  assert.equal(fixture.media.listeners.get('wheel').size, 0);
  assert.equal(fixture.slider.listeners.get('input').size, 0);
  assert.equal(lifecycle.isActive(), false);
});

test('static zoom is opt-in and excluded lightboxes never build or attach it', () => {
  const recipeStart = APP_JS.indexOf('async function openRecipeLightbox');
  const recipeBody = APP_JS.slice(recipeStart, APP_JS.indexOf('// Map a filament id', recipeStart));
  const surfaceStart = APP_JS.indexOf('async function openSurfaceLightbox');
  const surfaceBody = APP_JS.slice(surfaceStart, APP_JS.indexOf('// ── Recipe viewer lightbox', surfaceStart));
  assert.ok(!recipeBody.includes('buildStaticLightboxZoomControls'));
  assert.ok(!recipeBody.includes('setupStaticLightboxZoom'));
  assert.ok(!surfaceBody.includes('buildStaticLightboxZoomControls'));
  assert.ok(!surfaceBody.includes('setupStaticLightboxZoom'));
  assert.ok(surfaceBody.includes('slider.addEventListener("wheel", onWheel'));
  assert.ok(surfaceBody.includes('hSlider.addEventListener("wheel", onHWheel'));
});
