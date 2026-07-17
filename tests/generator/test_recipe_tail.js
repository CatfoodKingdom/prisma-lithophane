"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { createFeatureHarness } = require("./support/application_harness.cjs");

let partitionRecipeTail;
test.before(async () => {
  ({ app: { commands: { partitionRecipeTail } } } = await createFeatureHarness());
});

test("no tail when every recipe carries real area", () => {
  assert.deepEqual(partitionRecipeTail([0.4, 0.3, 0.2, 0.1]), {
    visible: 4, tailCount: 0, tailFraction: 0,
  });
});

test("a long sub-0.1% tail collapses while above-threshold rows stay", () => {
  const tail = Array.from({ length: 431 }, () => 0.0001);
  const result = partitionRecipeTail([0.30, 0.05, 0.02, 0.005, 0.0012, ...tail]);
  assert.equal(result.visible, 5);
  assert.equal(result.tailCount, 431);
  assert.ok(Math.abs(result.tailFraction - 431 * 0.0001) < 1e-9);
});

test("small all-sub-threshold sets remain visible", () => {
  assert.deepEqual(partitionRecipeTail([0.0005, 0.0004]), {
    visible: 2, tailCount: 0, tailFraction: 0,
  });
});

test("large all-sub-threshold sets retain the minimum visible floor", () => {
  const result = partitionRecipeTail(Array.from({ length: 50 }, () => 0.0002));
  assert.equal(result.visible, 3);
  assert.equal(result.tailCount, 47);
});

test("a one-row tail stays inline", () => {
  const result = partitionRecipeTail([0.30, 0.05, 0.02, 0.005, 0.0012, 0.0009]);
  assert.equal(result.visible, 6);
  assert.equal(result.tailCount, 0);
});

test("threshold and minimum visible count are configurable", () => {
  const result = partitionRecipeTail([0.5, 0.05, 0.005, 0.0005], {
    threshold: 0.01, minVisible: 1,
  });
  assert.equal(result.visible, 2);
  assert.equal(result.tailCount, 2);
});

test("empty recipe sets partition to nothing", () => {
  assert.deepEqual(partitionRecipeTail([]), { visible: 0, tailCount: 0, tailFraction: 0 });
});
