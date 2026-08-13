"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const { pathToFileURL } = require("node:url");
const { appDir } = require("./support/application_harness.cjs");

const moduleUrl = relative => pathToFileURL(path.join(appDir, relative)).href;

function rectFor(position, size) {
  return {
    left: position.left,
    top: position.top,
    right: position.left + size.width,
    bottom: position.top + size.height,
  };
}

function overlaps(left, right) {
  return left.left < right.right
    && left.right > right.left
    && left.top < right.bottom
    && left.bottom > right.top;
}

function inside(rect, viewport, margin = 8) {
  return rect.left >= viewport.left + margin
    && rect.top >= viewport.top + margin
    && rect.right <= viewport.right - margin
    && rect.bottom <= viewport.bottom - margin;
}

test("guide companion schema normalizes and freezes strict bundled content", async () => {
  const { step } = await import(moduleUrl("features/guides/core/schema.js"));
  const result = step({
    id: "companion-contract",
    title: "Companion contract",
    body: "Body",
    companion: {
      title: "Supporting figure",
      layout: "single",
      items: [{
        type: "image",
        src: "/assets/guides/example/figure-v1.svg",
        alt: "Example figure",
      }],
    },
  });

  assert.deepEqual(result.companion.preferred_placements, ["right", "left", "bottom", "top"]);
  assert.equal(result.companion.items[0].expandable, true);
  assert.ok(Object.isFrozen(result.companion));
  assert.ok(Object.isFrozen(result.companion.items));
  assert.ok(Object.isFrozen(result.companion.items[0]));

  const base = {
    id: "invalid-companion",
    title: "Invalid",
    body: "Body",
  };
  assert.throws(() => step({
    ...base,
    companion: false,
  }), /must be an object/);
  assert.throws(() => step({
    ...base,
    companion: {
      layout: "single",
      items: [{ type: "image", src: "https://example.com/figure.svg" }],
    },
  }), /must begin with \/assets\/guides\//);
  assert.throws(() => step({
    ...base,
    companion: {
      layout: "single",
      items: [{ type: "image", src: "/assets/guides/../figure.svg" }],
    },
  }), /invalid path segment/);
  assert.throws(() => step({
    ...base,
    companion: {
      layout: "single",
      items: [
        { type: "text", text: "One" },
        { type: "text", text: "Two" },
      ],
    },
  }), /requires exactly one item/);
  assert.throws(() => step({
    ...base,
    companion: {
      layout: "single",
      unexpected: true,
      items: [{ type: "text", text: "One" }],
    },
  }), /unknown fields/);
});

test("paired guide geometry keeps both surfaces visible and clear of the focus target", async () => {
  const { chooseGuideSurfaceLayout } = await import(moduleUrl("core/guide-geometry.js"));
  const viewport = { left: 0, top: 0, right: 1000, bottom: 700, width: 1000, height: 700 };
  const target = { left: 400, top: 250, right: 600, bottom: 350, width: 200, height: 100 };
  const cardSize = { width: 260, height: 140 };
  const companionSize = { width: 300, height: 200 };
  const placement = chooseGuideSurfaceLayout({
    targetRect: target,
    cardSize,
    companionSize,
    viewportRect: viewport,
    avoidRects: [target],
  });
  const card = rectFor(placement.card, cardSize);
  const companion = rectFor(placement.companion, companionSize);

  assert.ok(inside(card, viewport));
  assert.ok(inside(companion, viewport));
  assert.equal(overlaps(card, target), false);
  assert.equal(overlaps(companion, target), false);
  assert.equal(overlaps(card, companion), false);
});

test("paired guide geometry retains a valid pair and constrains only as fallback", async () => {
  const { chooseGuideSurfaceLayout } = await import(moduleUrl("core/guide-geometry.js"));
  const viewport = { left: 0, top: 0, right: 1000, bottom: 700, width: 1000, height: 700 };
  const retained = chooseGuideSurfaceLayout({
    targetRect: { left: 400, top: 250, right: 600, bottom: 350, width: 200, height: 100 },
    cardSize: { width: 260, height: 140 },
    companionSize: { width: 240, height: 160 },
    viewportRect: viewport,
    previousPosition: { left: 40, top: 40 },
    previousCompanionPlacement: { side: "right", alignment: "center" },
  });
  assert.equal(retained.card.placement, "retained");
  assert.equal(retained.companion.placement, "right-center");
  assert.equal(retained.companionConstraints, null);

  const targetlessRetained = chooseGuideSurfaceLayout({
    cardSize: { width: 260, height: 140 },
    companionSize: { width: 240, height: 160 },
    viewportRect: viewport,
    previousPosition: { left: 60, top: 80 },
    previousCompanionPlacement: { side: "bottom", alignment: "end" },
  });
  assert.equal(targetlessRetained.card.placement, "retained");
  assert.equal(targetlessRetained.companion.placement, "bottom-end");

  const smallViewport = { left: 0, top: 0, right: 800, bottom: 600, width: 800, height: 600 };
  const constrained = chooseGuideSurfaceLayout({
    cardSize: { width: 360, height: 240 },
    companionSize: { width: 600, height: 450 },
    viewportRect: smallViewport,
  });
  assert.ok(constrained.companionConstraints);
  assert.ok(constrained.companionConstraints.maxWidth < 600);
  assert.ok(constrained.companionConstraints.maxHeight > 0);
});
