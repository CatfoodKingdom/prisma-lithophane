"use strict";

const assert = require("node:assert/strict");
const path = require("node:path");
const test = require("node:test");
const { pathToFileURL } = require("node:url");

const { createFeatureHarness } = require("./support/application_harness.cjs");

function manualStep(id, actions = {}) {
  return Object.freeze({
    id,
    title: id,
    body: id,
    target_id: null,
    reveal_id: null,
    overlay_mode: "full-scrim",
    preferred_placements: Object.freeze([]),
    completion: Object.freeze({ kind: "manual" }),
    allow_previous: true,
    allow_skip: true,
    enter_actions: Object.freeze(actions.enter || []),
    complete_actions: Object.freeze(actions.complete || []),
  });
}

async function settle() {
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));
}

async function loadGuideSchema() {
  const filename = path.resolve(
    __dirname,
    "../../Prisma/generator/app/features/guides/core/schema.js",
  );
  return import(pathToFileURL(filename));
}

test("shared actions declare lifecycle, policy, disposition, and result contracts", async () => {
  const { app } = await createFeatureHarness();
  const required = [
    "workspace.reset", "printer.mount_ghost", "printer.select", "printer.select_nozzle",
    "settings.load_basic", "settings.require_basic", "settings.override", "filaments.enable_all",
    "filaments.require", "filaments.select", "guide.allocate_names",
    "palette_candidates.select_all", "palette_candidates.configure", "palette.deck.clear",
    "palette.deck.replace", "palette.deck.activate", "image.mount_guide_asset", "image.select",
    "image.configure", "palette.suggest", "solve.single", "solve.batch",
    "export.set_policy", "export.generate",
  ];
  assert.deepEqual([...app.commands.guideActionDefinitions.keys()], required);
  for (const [id, definition] of app.commands.guideActionDefinitions) {
    assert.equal(typeof definition.validate, "function", `${id} input validator`);
    assert.equal(typeof definition.validateResult, "function", `${id} result validator`);
    assert.ok(definition.resultContract, `${id} result contract`);
    assert.ok(["none", "transient", "persistent"].includes(definition.resourceDisposition));
    assert.ok(definition.executionTiming.length > 0);
    assert.ok(definition.workspacePolicies.length > 0);
  }
  assert.equal(
    app.commands.guideActionDefinitions.get("export.generate").resourceDisposition,
    "persistent",
  );
});

test("schema rejects malformed gates, aliases, detour nesting, and incompatible actions", async () => {
  const { validateGuideDefinitions } = await loadGuideSchema();
  const actionDefinitions = new Map([["teaching.only", {
    idempotent: true,
    workspacePolicies: ["basic-teaching"],
    executionTiming: ["enter"],
    validate() {},
  }]]);
  const makeStep = (id, overrides = {}) => ({
    id,
    title: id,
    body: id,
    target_id: null,
    reveal_id: null,
    overlay_mode: "full-scrim",
    preferred_placements: [],
    completion: { kind: "manual" },
    allow_previous: true,
    allow_skip: true,
    enter_actions: [],
    complete_actions: [],
    ...overrides,
  });
  const makeGuide = overrides => ({
    id: "schema-guide",
    version: 1,
    title: "Schema Guide",
    summary: "Exercises guide schema validation.",
    workspace_policy: "non-destructive",
    steps: [makeStep("first"), makeStep("second")],
    chapters: [{ id: "main", label: "Main", step_ids: ["first", "second"] }],
    ...overrides,
  });

  assert.throws(
    () => validateGuideDefinitions([
      makeGuide({ steps: [makeStep("same"), makeStep("same")] }),
    ], { actionDefinitions }),
    /duplicate step/i,
  );
  assert.throws(
    () => validateGuideDefinitions([
      makeGuide({
        steps: [makeStep("first", { completion: { kind: "mystery" } }), makeStep("second")],
      }),
    ], { actionDefinitions }),
    /invalid completion kind/i,
  );
  assert.throws(
    () => validateGuideDefinitions([makeGuide()], {
      aliases: new Map([["missing-alias", "missing-guide"]]),
      actionDefinitions,
    }),
    /invalid guide launch alias/i,
  );
  assert.throws(
    () => validateGuideDefinitions([
      makeGuide({
        detours: [{
          id: "nested",
          label: "Nested",
          description: "Not allowed",
          offer_step_id: "first",
          return_step_id: "second",
          steps: [makeStep("nested-step", { detour: { id: "too-deep" } })],
        }],
      }),
    ], { actionDefinitions }),
    /nested detour/i,
  );
  assert.throws(
    () => validateGuideDefinitions([
      makeGuide({
        steps: [
          makeStep("first", { enter_actions: [{ action: "teaching.only" }] }),
          makeStep("second"),
        ],
      }),
    ], { actionDefinitions }),
    /incompatible action/i,
  );
  assert.throws(
    () => validateGuideDefinitions([
      makeGuide({ steps: [makeStep("first", { body: "" }), makeStep("second")] }),
    ], { actionDefinitions }),
    /missing user-facing text/i,
  );
  assert.throws(
    () => validateGuideDefinitions([
      makeGuide({
        steps: [
          makeStep("first", { preferred_placements: ["diagonal"] }),
          makeStep("second"),
        ],
      }),
    ], { actionDefinitions }),
    /placement metadata/i,
  );
  assert.throws(
    () => validateGuideDefinitions([
      makeGuide({
        steps: [makeStep("first", { participating_surfaces: ["#unknownDialog"] }), makeStep("second")],
      }),
    ], { actionDefinitions }),
    /participating surfaces/i,
  );
  assert.throws(
    () => validateGuideDefinitions([
      makeGuide({ text_substitutions: { "{{name}}": "invalid..path" } }),
    ], { actionDefinitions }),
    /text substitution/i,
  );
  assert.throws(
    () => validateGuideDefinitions([
      makeGuide({
        durable_mutation_policy: {
          default: "deny",
          steps: {
            first: [{
              operation: "palette.saved.delete",
              match: { unknown_id: { context: "savedPaletteId" } },
            }],
          },
        },
      }),
    ], { actionDefinitions }),
    /match every detail field|invalid detail matcher/i,
  );
});

test("teaching sessions never persist temporary enabled-filament changes", async () => {
  const { app, storage } = await createFeatureHarness();
  app.state.guides.workspaceSessionId = "active-guide-session";
  app.state.palette.enabledFilamentPersistenceReady = true;
  app.state.palette.enabledFilamentRuntimeLibraryId = "library-a";
  app.state.session.modelLibraryManager.status = {
    runtime_active_library_id: "library-a",
    active_state_error: null,
  };

  assert.equal(app.commands.saveEnabledFilaments(), false);
  assert.equal(storage.values.size, 0);
});

test("action descriptors execute in order and publish named results", async () => {
  const { app } = await createFeatureHarness();
  const order = [];
  app.commands.registerGuideAction("test.record", {
    idempotent: true,
    resultContract: "test-number",
    validate(input) {
      if (!Number.isInteger(input.value)) throw new Error("value must be an integer");
    },
    validateResult(result) {
      if (!Number.isInteger(result)) throw new Error("result must be an integer");
    },
    async run(input) {
      order.push(input.value);
      return input.value;
    },
  });
  const guide = { id: "test-guide", workspace_policy: "non-destructive" };
  const results = await app.commands.executeGuideActions([
    { action: "test.record", input: { value: 1 }, result_key: "first" },
    { action: "test.record", input: { value: 2 }, result_key: "second" },
  ], { guide, phase: "enter" });

  assert.deepEqual(order, [1, 2]);
  assert.deepEqual(results, [1, 2]);
  assert.equal(app.state.guides.runtimeContext.first, 1);
  assert.equal(app.state.guides.runtimeContext.second, 2);
});

test("step actions run once, serialize transitions, and retry with one idempotency key", async () => {
  const { app } = await createFeatureHarness();
  const retryKeys = [];
  let attempts = 0;
  let completionCalls = 0;
  let releaseCompletion;
  app.commands.registerGuideAction("test.retry", {
    idempotent: true,
    resultContract: "test-ok",
    run(_input, context) {
      attempts += 1;
      retryKeys.push(context.idempotencyKey);
      if (attempts === 1) throw new Error("temporary failure");
      return true;
    },
  });
  app.commands.registerGuideAction("test.complete", {
    idempotent: true,
    resultContract: "test-ok",
    run() {
      completionCalls += 1;
      return new Promise(resolve => { releaseCompletion = () => resolve(true); });
    },
  });
  const steps = [
    manualStep("first", { enter: [{ action: "test.retry" }] }),
    manualStep("second", { complete: [{ action: "test.complete" }] }),
  ];
  const guide = Object.freeze({
    id: "action-guide",
    title: "Action Guide",
    workspace_policy: "non-destructive",
    restore_presentation: false,
    steps: Object.freeze(steps),
    chapters: Object.freeze([{ id: "main", label: "Main", step_ids: Object.freeze(["first", "second"]) }]),
  });
  const originalLookup = app.commands.getGuideDefinition;
  app.commands.getGuideDefinition = id => id === guide.id ? guide : originalLookup(id);
  app.commands.appConfirm = async () => true;
  app.commands.captureGuidePresentation = () => ({});
  app.commands.renderGuideStep = () => {};
  app.commands.hideGuideOverlay = () => {};

  assert.equal(await app.commands.startGuide(guide.id), true);
  await settle();
  assert.equal(attempts, 2);
  assert.equal(new Set(retryKeys).size, 1);
  assert.equal(app.state.guides.runtimeState, "presenting");

  assert.equal(app.commands.nextGuideStep(), true);
  assert.equal(app.commands.previousGuideStep(), true);
  assert.equal(app.state.guides.currentStep.id, "first");
  assert.equal(app.state.guides.reviewingCompletedStep, true);
  assert.equal(attempts, 2);
  assert.equal(app.commands.nextGuideStep(), true);

  assert.equal(app.commands.nextGuideStep(), false);
  assert.equal(app.state.guides.runtimeState, "completing");
  assert.equal(app.commands.nextGuideStep(), false);
  assert.equal(completionCalls, 1);
  releaseCompletion();
  await settle();
  await app.commands.endGuide();
  assert.equal(completionCalls, 1);
  assert.equal(app.state.guides.runtimeState, "idle");
});

test("event gates can require an explicit Next after their action completes", async () => {
  const { app } = await createFeatureHarness();
  const first = {
    ...manualStep("first"),
    completion: Object.freeze({
      kind: "event",
      event: "test.completed",
      predicate_id: "test.ready",
      auto_advance: false,
    }),
  };
  const guide = Object.freeze({
    id: "manual-advance-guide",
    title: "Manual Advance Guide",
    workspace_policy: "non-destructive",
    restore_presentation: false,
    steps: Object.freeze([first, manualStep("second")]),
    chapters: Object.freeze([{
      id: "main", label: "Main", step_ids: Object.freeze(["first", "second"]),
    }]),
  });
  const originalLookup = app.commands.getGuideDefinition;
  app.commands.getGuideDefinition = id => id === guide.id ? guide : originalLookup(id);
  app.commands.guidePredicateSatisfied = id => id === "test.ready";
  app.commands.captureGuidePresentation = () => ({});
  app.commands.renderGuideStep = () => {};
  app.commands.revealGuideTarget = () => {};

  assert.equal(await app.commands.startGuide(guide.id), true);
  app.events.emit("test.completed", {});
  assert.equal(app.state.guides.currentStep.id, "first");
  assert.equal(app.state.guides.completedStepIds.has("first"), true);
  assert.equal(app.commands.nextGuideStep(), true);
  assert.equal(app.state.guides.currentStep.id, "second");
});

test("ending a guide drains an in-flight action without advancing during cleanup", async () => {
  const { app } = await createFeatureHarness();
  let releaseAction;
  let releaseCleanup;
  app.commands.registerGuideAction("test.cleanup-race", {
    resultContract: "test-ok",
    validateResult() {},
    run() {
      return new Promise(resolve => { releaseAction = () => resolve(true); });
    },
  });
  const guide = Object.freeze({
    id: "cleanup-race-guide",
    title: "Cleanup Race Guide",
    workspace_policy: "non-destructive",
    restore_presentation: false,
    steps: Object.freeze([
      manualStep("first", { complete: [{ action: "test.cleanup-race" }] }),
      manualStep("second"),
    ]),
    chapters: Object.freeze([{
      id: "main", label: "Main", step_ids: Object.freeze(["first", "second"]),
    }]),
  });
  const originalLookup = app.commands.getGuideDefinition;
  app.commands.getGuideDefinition = id => id === guide.id ? guide : originalLookup(id);
  app.commands.captureGuidePresentation = () => ({});
  app.commands.renderGuideStep = () => {};
  app.commands.hideGuideOverlay = () => {};
  app.commands.cleanupGuideRuntime = () => new Promise(resolve => { releaseCleanup = resolve; });

  await app.commands.startGuide(guide.id);
  assert.equal(app.commands.nextGuideStep(), false);
  const ending = app.commands.endGuide();
  await settle();
  releaseAction();
  await settle();

  assert.equal(app.state.guides.currentStep.id, "first");
  assert.equal(app.state.guides.runtimeState, "ending");
  releaseCleanup();
  assert.equal(await ending, true);
  assert.equal(app.state.guides.runtimeState, "idle");
});

test("concurrent launch gestures share one preparation transaction", async () => {
  const { app } = await createFeatureHarness();
  let releasePreparation;
  let preparationCalls = 0;
  app.commands.prepareGuideRuntime = async () => {
    preparationCalls += 1;
    await new Promise(resolve => { releasePreparation = resolve; });
    return true;
  };
  app.commands.captureGuidePresentation = () => ({});
  app.commands.renderGuideStep = () => {};

  const first = app.commands.startGuide("interface-preview");
  const second = app.commands.startGuide("interface-preview");
  assert.equal(first, second);
  await settle();
  assert.equal(preparationCalls, 1);
  releasePreparation();
  assert.equal(await first, true);
});

test("palette replacement produces stable unique identities", async () => {
  const { app } = await createFeatureHarness();
  app.commands.renderDeckCards = () => {};
  app.commands.renderCreationTab = () => {};
  app.commands.updateRail = () => {};
  const descriptor = {
    action: "palette.deck.replace",
    input: {
      palettes: [
        { name: "A", filament_ids: ["a"] },
        { name: "B", filament_ids: ["b"] },
        { name: "C", filament_ids: ["c"] },
      ],
    },
  };
  const context = {
    guide: { id: "identity-guide", workspace_policy: "basic-teaching" },
    step: { id: "replace" },
    phase: "enter",
  };

  const first = await app.commands.executeGuideAction(descriptor, context);
  const second = await app.commands.executeGuideAction(descriptor, context);
  assert.equal(new Set(first.map(card => card.id)).size, 3);
  assert.deepEqual(second.map(card => card.id), first.map(card => card.id));
});

test("Skip advances a skippable step whose target is unavailable", async () => {
  const { app } = await createFeatureHarness();
  const guide = Object.freeze({
    id: "skip-guide",
    title: "Skip Guide",
    workspace_policy: "non-destructive",
    restore_presentation: false,
    steps: Object.freeze([manualStep("first"), manualStep("second")]),
    chapters: Object.freeze([{
      id: "main", label: "Main", step_ids: Object.freeze(["first", "second"]),
    }]),
  });
  const originalLookup = app.commands.getGuideDefinition;
  app.commands.getGuideDefinition = id => id === guide.id ? guide : originalLookup(id);
  app.commands.captureGuidePresentation = () => ({});
  app.commands.renderGuideStep = () => {};
  app.commands.showGuideTargetUnavailable = () => {};

  await app.commands.startGuide(guide.id);
  app.commands.handleGuideTargetUnavailable(null);
  assert.equal(app.state.guides.targetUnavailable, true);
  assert.equal(app.commands.skipGuideStep(), true);
  assert.equal(app.state.guides.currentStep.id, "second");
});

test("retry resumes at the failed descriptor without rerunning prior step actions", async () => {
  const { app } = await createFeatureHarness();
  let firstCalls = 0;
  let secondCalls = 0;
  app.commands.registerGuideAction("test.first-once", {
    resultContract: "test-ok",
    validateResult() {},
    run() { firstCalls += 1; return true; },
  });
  app.commands.registerGuideAction("test.second-retry", {
    resultContract: "test-ok",
    validateResult() {},
    run() {
      secondCalls += 1;
      if (secondCalls === 1) throw new Error("retry me");
      return true;
    },
  });
  const guide = Object.freeze({
    id: "descriptor-retry-guide",
    title: "Descriptor Retry Guide",
    workspace_policy: "non-destructive",
    restore_presentation: false,
    steps: Object.freeze([
      manualStep("first", { enter: [
        { action: "test.first-once" },
        { action: "test.second-retry" },
      ] }),
      manualStep("second"),
    ]),
    chapters: Object.freeze([{
      id: "main", label: "Main", step_ids: Object.freeze(["first", "second"]),
    }]),
  });
  const originalLookup = app.commands.getGuideDefinition;
  app.commands.getGuideDefinition = id => id === guide.id ? guide : originalLookup(id);
  app.commands.appConfirm = async () => true;
  app.commands.captureGuidePresentation = () => ({});
  app.commands.renderGuideStep = () => {};

  await app.commands.startGuide(guide.id);
  await settle();
  assert.equal(firstCalls, 1);
  assert.equal(secondCalls, 2);
  assert.equal(app.state.guides.runtimeState, "presenting");
});
