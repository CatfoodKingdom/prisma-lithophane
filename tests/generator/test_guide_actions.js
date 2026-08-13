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
    "workspace.reset", "printer.mount_ghost", "printer.select", "printer.select_print_setup",
    "settings.load_basic", "settings.require_basic", "settings.override", "filaments.enable_all",
    "settings.set_module_state", "presentation.lock",
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

test("Settings Drawer actions validate authoritative settings, modules, and presentation locks", async () => {
  const { app } = await createFeatureHarness();
  const moduleIds = [
    "a1_bilateral_denoise", "b1_printscale_bilateral", "b3_tv_flatten",
    "c1_achievable_tonemap", "c2_soft_gamut_compress",
  ];
  const moduleAction = app.commands.guideActionDefinitions.get("settings.set_module_state");
  assert.throws(() => moduleAction.validate({ state: [] }), /state object/);
  assert.throws(() => moduleAction.validate({ state: { [moduleIds[0]]: 1 } }), /boolean/);
  app.state.settings.moduleData = moduleIds.map(name => ({ name, default_enabled: false }));
  app.state.settings.moduleState = Object.fromEntries(moduleIds.map(id => [id, false]));
  app.state.settings.config.preprocessing_params = { retained: { exact: true } };
  app.state.session.apiConnected = false;
  app.commands.renderModulePanel = () => {};
  app.commands.renderDynamicSettings = () => {};
  app.commands.refreshModuleDrivenViews = () => {};
  await assert.rejects(
    app.commands.executeGuideAction({
      action: "settings.set_module_state",
      input: { state: { [moduleIds[0]]: true } },
    }, { guide: { workspace_policy: "basic-teaching" } }),
    /complete authoritative module map/,
  );
  const complete = Object.fromEntries(moduleIds.map(id => [id, id === moduleIds[2]]));
  await app.commands.executeGuideAction({
    action: "settings.set_module_state",
    input: { state: complete },
  }, { guide: { workspace_policy: "basic-teaching" } });
  assert.deepEqual(app.state.settings.moduleState, complete);
  assert.deepEqual(app.state.settings.config.preprocessing_params, { retained: { exact: true } });

  app.state.settings.SETTINGS_PROFILE_KEYS = ["known_key"];
  const override = app.commands.guideActionDefinitions.get("settings.override");
  assert.throws(() => override.validate({ values: { typo_key: true } }), /unknown settings/);

  await app.commands.executeGuideAction({
    action: "presentation.lock",
    input: { locks: ["settings-drawer-open"] },
  });
  await app.commands.executeGuideAction({
    action: "presentation.lock",
    input: { locks: ["settings-advanced-on"] },
  });
  assert.equal(app.commands.guidePresentationLocked("settings-drawer-open"), true);
  assert.equal(app.commands.guidePresentationLocked("settings-advanced-on"), true);
  assert.throws(
    () => app.commands.guideActionDefinitions.get("presentation.lock").validate({ locks: ["unknown"] }),
    /unknown lock/,
  );
});

test("ensure-on-entry reruns on review while ordinary entry actions remain once-only", async () => {
  const { app } = await createFeatureHarness();
  const calls = [];
  app.commands.executeGuideAction = async descriptor => calls.push(descriptor.action);
  app.commands.renderGuideStep = () => {};
  app.commands.revealGuideTarget = () => {};
  app.commands.captureGuidePresentation = () => ({});
  app.commands.restoreGuidePresentation = () => {};
  const hub = manualStep("hub", { enter: [
    { action: "ensure", ensure_on_entry: true },
    { action: "ordinary" },
  ] });
  const detourStep = manualStep("detour-step");
  const guide = {
    id: "ensure-review",
    steps: [manualStep("before"), hub],
    detours: [{
      id: "route",
      offer_step_id: "hub",
      return_step_id: "hub",
      steps: [detourStep],
    }],
  };
  app.state.guides.currentGuide = guide;
  app.state.guides.currentStep = guide.steps[0];
  app.state.guides.runtimeState = "presenting";
  app.state.guides.completedStepIds = new Set();
  app.state.guides.completedDetourIds = new Set();
  app.state.guides.visitedDetourIds = new Set();
  app.state.guides.actionLedger = new Set();
  app.state.guides.activeDetour = null;
  assert.equal(app.commands.nextGuideStep(), true);
  await settle();
  assert.deepEqual(calls, ["ensure", "ordinary"]);
  assert.equal(app.commands.startGuideDetour("route"), true);
  assert.equal(app.commands.exitGuideDetour(), true);
  await settle();
  assert.deepEqual(calls, ["ensure", "ordinary", "ensure"]);
});

test("presentation locks survive cleanup failure and clear before restoration on retry", async () => {
  const { app } = await createFeatureHarness();
  const guide = { id: "locked", restore_presentation: true };
  app.state.guides.currentGuide = guide;
  app.state.guides.currentStep = manualStep("locked-step");
  app.state.guides.runtimeState = "presenting";
  app.state.guides.presentationLocks = new Set(["settings-drawer-open"]);
  app.state.guides.presentationSnapshot = { settingsDrawerOpen: false };
  let attempts = 0;
  app.commands.cleanupGuideRuntime = async () => {
    attempts += 1;
    if (attempts === 1) throw new Error("retry me");
    return { present: [] };
  };
  const locksAtRestore = [];
  app.commands.restoreGuidePresentation = () => {
    locksAtRestore.push([...app.state.guides.presentationLocks]);
  };
  assert.equal(await app.commands.endGuide(), false);
  assert.equal(app.state.guides.runtimeState, "recovering");
  assert.equal(app.commands.guidePresentationLocked("settings-drawer-open"), true);
  assert.equal(await app.commands.endGuide(), true);
  assert.deepEqual(locksAtRestore, [[]]);
  assert.equal(app.commands.guidePresentationLocked("settings-drawer-open"), false);
});

test("registered guide overrides use hydrated settings keys and Settings Drawer stays non-destructive", async () => {
  const { app } = await createFeatureHarness();
  assert.ok(app.state.settings.SETTINGS_PROFILE_KEYS.length > 0);
  const override = app.commands.guideActionDefinitions.get("settings.override");
  for (const guide of app.state.guides.definitions) {
    const descriptors = [
      ...(guide.preflight_actions || []),
      ...(guide.preparation_actions || []),
      ...app.commands.getAllGuideSteps(guide).flatMap(step => [
        ...(step.enter_actions || []),
        ...(step.complete_actions || []),
      ]),
    ];
    for (const descriptor of descriptors.filter(item => item.action === "settings.override")) {
      assert.doesNotThrow(() => override.validate(descriptor.input), `${guide.id} override`);
    }
  }

  const guide = app.commands.getGuideDefinition("settings-drawer");
  const steps = app.commands.getAllGuideSteps(guide);
  const forbidden = /^(?:palette\.suggest|solve\.(?:single|batch)|export\.(?:set_policy|generate)|palette\.deck\.|palette_candidates\.)/;
  for (const current of steps) {
    const actions = [...current.enter_actions, ...current.complete_actions].map(item => item.action);
    assert.equal(actions.some(action => forbidden.test(action)), false, current.id);
  }
  assert.equal(steps.filter(step => step.completion.kind !== "manual").length, 1);
  const suggest = steps.find(step => step.id === "settings-drawer.luminance.shading-balance-suggest");
  assert.equal(suggest.completion.kind, "manual");
  assert.equal(guide.durable_mutation_policy, undefined);
  const preprocessing = guide.detours.find(route => route.id === "settings-drawer.preprocessing");
  for (const current of preprocessing.steps) {
    const overrideAction = current.enter_actions.find(action => action.action === "settings.override");
    assert.ok(overrideAction, `${current.id} restores exact preprocessing params`);
    assert.ok(Object.hasOwn(overrideAction.input.values, "preprocessing_params"));
    const moduleAction = current.enter_actions.find(action => action.action === "settings.set_module_state");
    assert.equal(Object.keys(moduleAction.input.state).length, 5);
  }
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

test("event gates can observe multiple events for one combined completion predicate", async () => {
  const { app } = await createFeatureHarness();
  let ready = false;
  const first = {
    ...manualStep("first"),
    completion: Object.freeze({
      kind: "event",
      events: Object.freeze(["test.first-changed", "test.second-changed"]),
      predicate_id: "test.both-ready",
      auto_advance: true,
    }),
  };
  const guide = Object.freeze({
    id: "multi-event-guide",
    title: "Multi Event Guide",
    workspace_policy: "non-destructive",
    restore_presentation: false,
    steps: Object.freeze([first, manualStep("second")]),
    chapters: Object.freeze([{
      id: "main", label: "Main", step_ids: Object.freeze(["first", "second"]),
    }]),
  });
  const originalLookup = app.commands.getGuideDefinition;
  app.commands.getGuideDefinition = id => id === guide.id ? guide : originalLookup(id);
  app.commands.guidePredicateSatisfied = id => id === "test.both-ready" && ready;
  app.commands.captureGuidePresentation = () => ({});
  app.commands.renderGuideStep = () => {};
  app.commands.revealGuideTarget = () => {};

  assert.equal(await app.commands.startGuide(guide.id), true);
  app.events.emit("test.first-changed", {});
  assert.equal(app.state.guides.currentStep.id, "first");
  ready = true;
  app.events.emit("test.second-changed", {});
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
