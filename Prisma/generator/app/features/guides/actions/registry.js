/** Declarative, validated guide actions shared by every guide definition. */
export function installFeaturesGuideActionRegistry(app) {
  const actions = new Map();
  const dispositions = new Set(["none", "transient", "persistent"]);
  const resultContracts = Object.freeze({
    "workspace.reset": "workspace-reset-result",
    "printer.mount_ghost": "printer-profile",
    "printer.select": "active-printer",
    "printer.select_print_setup": "active-printer",
    "settings.load_basic": "settings-profile",
    "settings.require_basic": "settings-profile",
    "settings.override": "settings-values",
    "settings.set_module_state": "module-state",
    "presentation.lock": "presentation-locks",
    "filaments.enable_all": "filament-id-list",
    "filaments.require": "filament-id-list",
    "filaments.select": "filament-id-list",
    "guide.allocate_names": "guide-runtime-values",
    "palette_candidates.select_all": "filament-id-list",
    "palette_candidates.configure": "palette-controls",
    "palette.deck.clear": "palette-card-list",
    "palette.deck.replace": "palette-card-list",
    "palette.deck.activate": "palette-card",
    "image.mount_guide_asset": "image-library-entry",
    "image.select": "image-library-entry",
    "image.configure": "image-configuration",
    "palette.suggest": "palette-card-list",
    "solve.single": "solve-job",
    "solve.batch": "batch-solve-job",
    "export.set_policy": "export-policy",
    "export.generate": "export-job",
  });
  const objectResult = (result, contract) => {
    if (!result || typeof result !== "object" || Array.isArray(result)) {
      throw new Error(`Guide action returned an invalid ${contract}`);
    }
  };
  const arrayResult = (result, contract) => {
    if (!Array.isArray(result)) throw new Error(`Guide action returned an invalid ${contract}`);
  };
  const resultValidators = Object.freeze({
    "workspace-reset-result": result => objectResult(result, "workspace reset result"),
    "printer-profile": result => {
      objectResult(result, "printer profile");
      if (!result.id) throw new Error("Guide action returned a printer profile without an ID");
    },
    "active-printer": result => {
      objectResult(result, "active printer");
      objectResult(result.printer, "active printer profile");
    },
    "settings-profile": result => {
      objectResult(result, "Settings Profile");
      if (!result.id || !result.settings) throw new Error("Guide action returned an incomplete Settings Profile");
    },
    "settings-values": result => objectResult(result, "settings values"),
    "module-state": result => objectResult(result, "module state"),
    "presentation-locks": result => arrayResult(result, "presentation locks"),
    "guide-runtime-values": result => objectResult(result, "guide runtime values"),
    "filament-id-list": result => arrayResult(result, "filament ID list"),
    "palette-controls": result => objectResult(result, "palette controls"),
    "palette-card-list": result => arrayResult(result, "palette card list"),
    "palette-card": result => {
      objectResult(result, "palette card");
      if (!result.id) throw new Error("Guide action returned a palette card without an ID");
    },
    "image-library-entry": result => {
      objectResult(result, "image entry");
      if (!result.filename) throw new Error("Guide action returned an image entry without a filename");
    },
    "image-configuration": result => {
      objectResult(result, "image configuration");
      objectResult(result.frame, "image frame");
      objectResult(result.appearance, "image appearance");
    },
    "solve-job": result => {
      objectResult(result, "solve job");
      if (!result.run_id) throw new Error("Guide action returned a solve job without a run ID");
    },
    "batch-solve-job": result => {
      objectResult(result, "batch solve job");
      if (!result.job_id) throw new Error("Guide action returned a batch solve job without a job ID");
    },
    "export-policy": result => objectResult(result, "export policy"),
    "export-job": result => {
      objectResult(result, "export job");
      if (!result.job_id || !result.export_id) {
        throw new Error("Guide action returned an incomplete export job");
      }
    },
  });

  function stableSerialize(value) {
    if (Array.isArray(value)) return `[${value.map(stableSerialize).join(",")}]`;
    if (value && typeof value === "object") {
      return `{${Object.keys(value).sort().map(key => (
        `${JSON.stringify(key)}:${stableSerialize(value[key])}`
      )).join(",")}}`;
    }
    return JSON.stringify(value);
  }

  function shortStableHash(value) {
    const text = typeof value === "string" ? value : stableSerialize(value);
    let hash = 0x811c9dc5;
    for (let index = 0; index < text.length; index += 1) {
      hash ^= text.charCodeAt(index);
      hash = Math.imul(hash, 0x01000193);
    }
    return (hash >>> 0).toString(36);
  }

  function requireString(input, key, actionId) {
    if (typeof input[key] !== "string" || !input[key].trim()) {
      throw new Error(`${actionId} requires ${key}`);
    }
  }

  function requireArray(input, key, actionId) {
    if (!Array.isArray(input[key])) throw new Error(`${actionId} requires ${key}`);
  }

  function registerGuideAction(id, definition) {
    if (!id || actions.has(id)) throw new Error(`Duplicate guide action: ${id}`);
    if (typeof definition?.run !== "function") throw new Error(`Guide action ${id} requires a run function`);
    const resultContract = definition.resultContract || resultContracts[id];
    const resolved = {
      idempotent: false,
      workspacePolicies: ["basic-teaching", "non-destructive", "preview"],
      resourceDisposition: "transient",
      executionTiming: ["preparation", "enter", "complete"],
      validate: () => {},
      validateResult: resultValidators[resultContract] || (() => {}),
      ...definition,
      resultContract,
    };
    if (!resolved.resultContract) throw new Error(`Guide action ${id} requires a result contract`);
    if (!dispositions.has(resolved.resourceDisposition)) {
      throw new Error(`Guide action ${id} has an invalid resource disposition`);
    }
    actions.set(id, Object.freeze(resolved));
  }

  async function executeGuideAction(descriptor, executionContext = {}) {
    if (!descriptor?.action) throw new Error("Guide action descriptors require an action ID");
    const definition = actions.get(descriptor.action);
    if (!definition) throw new Error(`Unknown guide action: ${descriptor.action}`);
    const guide = executionContext.guide || app.state.guides.currentGuide;
    const policy = guide?.workspace_policy || "non-destructive";
    if (!definition.workspacePolicies.includes(policy)) {
      throw new Error(`Guide action ${descriptor.action} is not valid for ${policy} guides`);
    }
    const phase = executionContext.phase || "enter";
    if (!definition.executionTiming.includes(phase)) {
      throw new Error(`Guide action ${descriptor.action} cannot run during ${phase}`);
    }
    definition.validate(descriptor.input || {});
    const idempotencyKey = [
      app.state.guides.workspaceSessionId || "no-session",
      guide?.canonical_guide_id || guide?.id || "no-guide",
      guide?.route_id || "full",
      executionContext.step?.id || "preparation",
      phase,
      Number.isInteger(executionContext.descriptorIndex)
        ? executionContext.descriptorIndex
        : 0,
      descriptor.action,
      shortStableHash(descriptor.input || {}),
    ].join(":");
    app.api.setRequestContext?.({ guideActionIdempotencyKey: idempotencyKey });
    try {
      const result = await definition.run(descriptor.input || {}, {
        ...executionContext,
        idempotencyKey,
      });
      definition.validateResult(result);
      if (descriptor.result_key) {
        app.state.guides.runtimeContext ||= {};
        app.state.guides.runtimeContext[descriptor.result_key] = result;
      }
      return result;
    } finally {
      app.api.setRequestContext?.({ guideActionIdempotencyKey: null });
    }
  }

  async function executeGuideActions(descriptors = [], executionContext = {}) {
    const results = [];
    for (let index = 0; index < descriptors.length; index += 1) {
      results.push(await executeGuideAction(descriptors[index], {
        ...executionContext,
        descriptorIndex: index,
      }));
    }
    return results;
  }

  registerGuideAction("workspace.reset", {
    idempotent: true,
    workspacePolicies: ["basic-teaching"],
    executionTiming: ["preparation"],
    async run() {
      const result = await app.api.resetGuideRuntime(app.state.guides.workspaceSessionId);
      app.commands.resetGuideFrontendWorkspace();
      return result;
    },
  });
  registerGuideAction("printer.mount_ghost", {
    idempotent: true,
    workspacePolicies: ["basic-teaching"],
    async run() {
      const mounted = await app.api.mountGuidePrinter(app.state.guides.workspaceSessionId);
      await app.commands.loadPrinters();
      return mounted.profile;
    },
  });
  registerGuideAction("printer.select", {
    idempotent: true,
    workspacePolicies: ["basic-teaching"],
    validate(input) { requireString(input, "printer_id", "printer.select"); },
    async run(input) {
      const active = await app.commands.selectActivePrintSetup({
        intent_kind: "select_printer",
        active_printer_id: input.printer_id,
        review_policy: "guide_authorized",
      });
      if (!active) throw new Error("Printer selection was cancelled");
      await app.commands.loadPrinters();
      return active;
    },
  });
  registerGuideAction("printer.select_print_setup", {
    idempotent: true,
    workspacePolicies: ["basic-teaching"],
    validate(input) {
      requireString(input, "nozzle_id", "printer.select_print_setup");
      if (!Number.isInteger(input.extrusion_width_um) || input.extrusion_width_um <= 0) {
        throw new Error("printer.select_print_setup extrusion_width_um must be a positive integer");
      }
    },
    async run(input) {
      const active = await app.commands.selectActivePrintSetup({
        intent_kind: "select_extrusion_width",
        active_nozzle_id: input.nozzle_id,
        current_width_um: input.extrusion_width_um,
        review_policy: "guide_authorized",
      });
      if (!active) throw new Error("Extrusion Width selection was cancelled");
      await app.commands.loadPrinters();
      return active;
    },
  });
  registerGuideAction("settings.load_basic", {
    idempotent: true,
    workspacePolicies: ["basic-teaching"],
    executionTiming: ["preparation", "enter"],
    validate(input) {
      if (input.load_as != null && !["tutorial-copy", "system"].includes(input.load_as)) {
        throw new Error("settings.load_basic load_as must be tutorial-copy or system");
      }
    },
    async run(input, context) {
      if ((input.load_as || "tutorial-copy") === "tutorial-copy") {
        return app.commands.loadGuideBasicSettings(context.guide);
      }
      const basic = app.commands.findSettingsProfile(app.state.ui.SYSTEM_SETTINGS_PROFILE_ID);
      if (!basic) throw new Error("the built-in Basic Settings Profile is unavailable");
      await app.commands._doLoadSettingsProfile(basic);
      return basic;
    },
  });
  registerGuideAction("settings.require_basic", {
    idempotent: true,
    resourceDisposition: "none",
    executionTiming: ["preflight"],
    workspacePolicies: ["basic-teaching"],
    validate(input) {
      if (!input.values || typeof input.values !== "object" || Array.isArray(input.values)) {
        throw new Error("settings.require_basic requires values");
      }
    },
    run(input) {
      const basic = app.commands.findSettingsProfile(app.state.ui.SYSTEM_SETTINGS_PROFILE_ID);
      if (!basic || basic.kind !== "system") throw new Error("the built-in Basic Settings Profile is unavailable");
      for (const [key, expected] of Object.entries(input.values)) {
        if (JSON.stringify(basic.settings?.[key]) !== JSON.stringify(expected)) {
          throw new Error(`Basic requires ${key}=${expected} for this guide`);
        }
      }
      return basic;
    },
  });
  registerGuideAction("settings.override", {
    idempotent: true,
    workspacePolicies: ["basic-teaching"],
    validate(input) {
      if (!input.values || typeof input.values !== "object" || Array.isArray(input.values)) {
        throw new Error("settings.override requires a values object");
      }
      const knownSettings = new Set(app.state.settings.SETTINGS_PROFILE_KEYS || []);
      const unknown = knownSettings.size
        ? Object.keys(input.values).filter(key => !knownSettings.has(key))
        : [];
      if (unknown.length) {
        throw new Error(`settings.override contains unknown settings: ${unknown.join(", ")}`);
      }
    },
    async run(input) {
      const values = JSON.parse(JSON.stringify(input.values));
      Object.assign(app.state.settings.config, values);
      if (app.state.session.apiConnected) {
        const evaluationTicket = app.commands.beginSettingsEvaluationRequest();
        const runSync = async () => {
          // Send the declared override as a patch. Re-reading the whole drawer
          // here can expand one declarative mode change into derived/internal
          // fields that the server owns (notably the Luminance preset).
          const response = await app.api.updateConfig(values);
          if (response?.config) Object.assign(app.state.settings.config, response.config);
          app.commands.applySettingsEvaluationResponse(response, evaluationTicket);
          return response;
        };
        const pendingSync = app.state.settings._configSyncChain.catch(() => {}).then(runSync);
        app.state.settings._configSyncChain = pendingSync.catch(() => {});
        await pendingSync;
        app.events.emit("config.synced", {
          config: app.commands._cloneValue(app.state.settings.config),
        });
      }
      if (globalThis.document) {
        app.commands.updateDerivedParams?.();
        app.commands.renderSettingsTab();
      }
      return values;
    },
  });
  registerGuideAction("filaments.enable_all", {
    idempotent: true,
    workspacePolicies: ["basic-teaching"],
    run() {
      app.commands.applyEnabledFilamentSelection(
        app.commands.getGenerationEligibleFilamentIds(),
        { persist: false, render: false },
      );
      if (globalThis.document) app.commands.refreshEnabledFilamentConsumers();
      return [...app.state.palette.enabledFilaments];
    },
  });
  registerGuideAction("settings.set_module_state", {
    idempotent: true,
    workspacePolicies: ["basic-teaching"],
    validate(input) {
      if (!input.state || typeof input.state !== "object" || Array.isArray(input.state)) {
        throw new Error("settings.set_module_state requires a state object");
      }
      if (Object.values(input.state).some(value => typeof value !== "boolean")) {
        throw new Error("settings.set_module_state values must be boolean");
      }
    },
    async run(input) {
      const knownIds = (app.state.settings.moduleData || []).map(module => module.name).sort();
      const providedIds = Object.keys(input.state).sort();
      if (
        knownIds.length === 0
        || knownIds.length !== providedIds.length
        || knownIds.some((id, index) => id !== providedIds[index])
      ) {
        throw new Error("settings.set_module_state requires the complete authoritative module map");
      }
      return app.commands._applyModuleSnapshot(
        input.state,
        app.state.settings.config,
        true,
      );
    },
  });
  registerGuideAction("presentation.lock", {
    idempotent: true,
    validate(input) {
      const knownLocks = new Set(["settings-drawer-open", "settings-advanced-on"]);
      if (
        !Array.isArray(input.locks)
        || input.locks.some(lock => typeof lock !== "string" || !knownLocks.has(lock))
      ) {
        throw new Error("presentation.lock contains an unknown lock");
      }
    },
    run(input) {
      app.state.guides.presentationLocks ||= new Set();
      input.locks.forEach(lock => app.state.guides.presentationLocks.add(lock));
      return [...app.state.guides.presentationLocks];
    },
  });
  registerGuideAction("filaments.require", {
    idempotent: true,
    resourceDisposition: "none",
    executionTiming: ["preflight"],
    workspacePolicies: ["basic-teaching"],
    validate(input) {
      requireArray(input, "filament_ids", "filaments.require");
      if (new Set(input.filament_ids).size !== input.filament_ids.length) {
        throw new Error("filaments.require filament_ids must be unique");
      }
    },
    run(input) {
      const byId = new Map(app.state.session.allFilaments.map(item => [item.filament_id, item]));
      for (const id of input.filament_ids) {
        const filament = byId.get(id);
        if (!filament || !app.commands.isGenerationEligibleFilament(filament)) {
          throw new Error(`Required guide Filament is unavailable: ${id}`);
        }
      }
      return [...input.filament_ids];
    },
  });
  registerGuideAction("filaments.select", {
    idempotent: true,
    workspacePolicies: ["basic-teaching"],
    validate(input) {
      requireArray(input, "filament_ids", "filaments.select");
      if (new Set(input.filament_ids).size !== input.filament_ids.length) {
        throw new Error("filaments.select filament_ids must be unique");
      }
    },
    run(input) {
      const eligible = new Set(app.commands.getGenerationEligibleFilamentIds());
      if (input.filament_ids.some(id => !eligible.has(id))) {
        throw new Error("A required Guide Filament is no longer available");
      }
      app.commands.applyEnabledFilamentSelection(input.filament_ids, { persist: false, render: false });
      if (globalThis.document) app.commands.refreshEnabledFilamentConsumers();
      return [...app.state.palette.enabledFilaments];
    },
  });
  registerGuideAction("guide.allocate_names", {
    idempotent: true,
    resourceDisposition: "none",
    executionTiming: ["preflight"],
    workspacePolicies: ["basic-teaching"],
    validate(input) {
      for (const key of ["palette", "profile", "run"]) requireString(input, key, "guide.allocate_names");
    },
    async run(input) {
      const [palettes, profiles, runs] = await Promise.all([
        app.api.fetchSavedPalettes(),
        app.api.fetchSettingsProfiles(),
        app.api.listSavedRuns(),
      ]);
      const allocate = (base, names) => {
        const used = new Set(names.map(name => String(name || "").trim().toLocaleLowerCase()));
        for (let suffix = 1; ; suffix += 1) {
          const candidate = suffix === 1 ? base.trim() : `${base.trim()} ${suffix}`;
          if (!used.has(candidate.toLocaleLowerCase())) return candidate;
        }
      };
      return {
        savedPaletteName: allocate(input.palette, (palettes?.palettes || []).map(item => item.name)),
        savedProfileName: allocate(input.profile, (profiles?.profiles || []).map(item => item.name)),
        savedRunName: allocate(input.run, (runs || []).map(item => item.label)),
        startupSettingsProfileId: profiles?.user_default_profile_id || null,
      };
    },
  });
  registerGuideAction("palette_candidates.select_all", {
    idempotent: true,
    workspacePolicies: ["basic-teaching"],
    run() {
      app.commands.selectAllCandidates();
      app.state.palette.candidateInitialized = true;
      return [...app.state.palette.candidateSelection];
    },
  });
  registerGuideAction("palette_candidates.configure", {
    idempotent: true,
    workspacePolicies: ["basic-teaching"],
    validate(input) {
      if (
        input.target_filament_count != null
        && (!Number.isInteger(Number(input.target_filament_count))
          || Number(input.target_filament_count) < 2
          || Number(input.target_filament_count) > 16)
      ) {
        throw new Error("palette_candidates.configure target_filament_count must be 2–16");
      }
      if (
        input.suggestion_count != null
        && (!Number.isInteger(Number(input.suggestion_count))
          || Number(input.suggestion_count) < 1
          || Number(input.suggestion_count) > 10)
      ) {
        throw new Error("palette_candidates.configure suggestion_count must be 1–10");
      }
      if (
        input.suggestion_mode != null
        && !["standard", "luminance_detail"].includes(input.suggestion_mode)
      ) {
        throw new Error("palette_candidates.configure has an invalid suggestion_mode");
      }
    },
    run(input) {
      const values = {
        targetFilamentCount: input.target_filament_count,
        targetSuggestCount: input.suggestion_count,
        paletteSuggestMode: input.suggestion_mode,
      };
      for (const [id, value] of Object.entries(values)) {
        const control = app.state.ui.$(`#${id}`);
        if (control && value != null) control.value = String(value);
      }
      if (input.suggestion_mode != null) {
        app.commands.applyPaletteSuggestModeToSettings?.(input.suggestion_mode);
      }
      return values;
    },
  });
  registerGuideAction("palette.deck.clear", {
    idempotent: true,
    workspacePolicies: ["basic-teaching"],
    run() {
      app.state.palette.deck = [];
      app.state.palette.stagingDeck = [];
      app.state.palette.activeDeckId = null;
      app.commands.renderDeckCards();
      app.commands.renderCreationTab();
      app.commands.updateRail();
      return [];
    },
  });
  registerGuideAction("palette.deck.replace", {
    idempotent: true,
    workspacePolicies: ["basic-teaching"],
    validate(input) {
      requireArray(input, "palettes", "palette.deck.replace");
      if (input.palettes.some(item => !Array.isArray(item?.filament_ids) || !item.filament_ids.length)) {
        throw new Error("palette.deck.replace palettes require filament_ids");
      }
    },
    run(input, context) {
      const identity = shortStableHash(context.idempotencyKey);
      app.state.palette.deck = input.palettes.map((palette, index) => ({
        ...app.commands.createPaletteDeckCard({
          idPrefix: "guide",
          name: palette.name || `Guide Palette ${index + 1}`,
          filamentIds: palette.filament_ids,
          saved: false,
        }),
        id: `guide-${identity}-${index + 1}`,
      }));
      app.state.palette.stagingDeck = [];
      app.state.palette.activeDeckId = app.state.palette.deck[0]?.id || null;
      app.commands.renderDeckCards();
      app.commands.renderCreationTab();
      app.commands.updateRail();
      return [...app.state.palette.deck];
    },
  });
  registerGuideAction("palette.deck.activate", {
    idempotent: true,
    workspacePolicies: ["basic-teaching"],
    validate(input) {
      if (input.card_id == null && !Number.isInteger(input.index)) {
        throw new Error("palette.deck.activate requires card_id or index");
      }
    },
    run(input) {
      const card = input.card_id
        ? app.state.palette.deck.find(item => item.id === input.card_id)
        : app.state.palette.deck[input.index];
      if (!card) throw new Error("Guide palette card is unavailable");
      app.commands.activateDeckCard(card.id, { sync: false });
      return card;
    },
  });
  registerGuideAction("image.mount_guide_asset", {
    idempotent: true,
    workspacePolicies: ["basic-teaching"],
    validate(input) { requireString(input, "asset_id", "image.mount_guide_asset"); },
    async run(input) {
      const image = await app.api.mountGuideAsset(app.state.guides.workspaceSessionId, input.asset_id);
      return app.commands.mountGuideImage(image);
    },
  });
  registerGuideAction("image.select", {
    idempotent: true,
    workspacePolicies: ["basic-teaching"],
    validate(input) {
      if (!input.asset_id && !input.source_ref && !input.filename) {
        throw new Error("image.select requires asset_id, source_ref, or filename");
      }
      for (const key of ["width_mm", "height_mm"]) {
        if (input[key] != null && (!Number.isFinite(Number(input[key])) || Number(input[key]) <= 0)) {
          throw new Error(`image.select ${key} must be positive`);
        }
      }
    },
    async run(input) {
      const expectedRef = input.asset_id ? `guide-image:${input.asset_id}` : input.source_ref;
      const image = app.commands.imageLibraryEntries().find(candidate => (
        (!expectedRef || candidate.source_ref === expectedRef)
        && (!input.filename || candidate.filename === input.filename)
      ));
      if (!image) throw new Error("Guide image is unavailable");
      await app.commands.selectImageLibraryEntry(image, { sync: false });
      if (input.width_mm != null) app.state.image.frameState.widthMm = Number(input.width_mm);
      if (input.height_mm != null) app.state.image.frameState.heightMm = Number(input.height_mm);
      app.commands.syncDimFields?.();
      app.commands.renderFrameCanvas();
      await app.commands.syncConfigToServer({ throwOnError: true });
      return image;
    },
  });
  registerGuideAction("image.configure", {
    idempotent: true,
    workspacePolicies: ["basic-teaching"],
    validate(input) {
      if (input.width_mm != null && Number(input.width_mm) <= 0) throw new Error("image width must be positive");
      if (input.height_mm != null && Number(input.height_mm) <= 0) throw new Error("image height must be positive");
    },
    async run(input) {
      if (!app.state.image.selectedImage) throw new Error("Select an image before configuring it");
      const frameKeys = ["scale", "rotation", "panX", "panY", "flipH", "flipV"];
      for (const key of frameKeys) if (input[key] != null) app.state.image.frameState[key] = input[key];
      if (input.width_mm != null) app.state.image.frameState.widthMm = Number(input.width_mm);
      if (input.height_mm != null) app.state.image.frameState.heightMm = Number(input.height_mm);
      if (input.appearance && typeof input.appearance === "object") {
        Object.assign(app.state.image.imageAdjust, input.appearance);
      }
      app.commands.syncDimFields?.();
      app.commands.renderFrameCanvas();
      app.commands.renderPreview();
      await app.commands.syncConfigToServer({ throwOnError: true });
      return { frame: { ...app.state.image.frameState }, appearance: { ...app.state.image.imageAdjust } };
    },
  });
  registerGuideAction("palette.suggest", {
    workspacePolicies: ["basic-teaching"],
    async run() {
      const before = new Set(app.state.palette.stagingDeck.map(card => card.id));
      const result = await app.commands.handleSuggestPalettes({ throwOnError: true });
      if (!result) throw new Error("Guide palette suggestion did not complete");
      const created = app.state.palette.stagingDeck.filter(card => !before.has(card.id));
      if (!created.length) throw new Error("Guide palette suggestion returned no palettes");
      return created;
    },
  });
  registerGuideAction("solve.single", {
    workspacePolicies: ["basic-teaching"],
    async run(_input, context) {
      app.commands.setSolveMode("single");
      const runId = `guide-run-${shortStableHash(context.idempotencyKey)}`;
      const accepted = await app.commands.handleStartSolve({
        runId,
        throwOnError: true,
      });
      const run = accepted?.run || app.state.solve.solveRuns.find(item => item.id === runId);
      if (!run) throw new Error("Guide solve did not start");
      const jobId = accepted?.started?.job_id || app.state.solve.activeSolveJobId;
      if (jobId) await app.api.registerGuideJob(app.state.guides.workspaceSessionId, "solve", jobId);
      return { run_id: run.id, job_id: jobId };
    },
  });
  registerGuideAction("solve.batch", {
    workspacePolicies: ["basic-teaching"],
    validate(input) {
      if (input.card_ids == null) return;
      if (
        !Array.isArray(input.card_ids)
        || input.card_ids.length < 2
        || input.card_ids.length > 10
        || new Set(input.card_ids).size !== input.card_ids.length
        || input.card_ids.some(id => typeof id !== "string" || !id.trim())
      ) {
        throw new Error("solve.batch card_ids must contain 2–10 unique card IDs");
      }
    },
    async run(input) {
      app.commands.setSolveMode("batch");
      app.state.solve.batchSelectedDeckIds = new Set(
        input.card_ids || app.state.palette.deck.map(card => card.id),
      );
      const accepted = await app.commands.handleStartPaletteBatch({
        throwOnError: true,
        confirmCost: false,
      });
      const jobId = accepted?.job_id || app.state.solve.activeSolveJobId;
      if (!jobId) throw new Error("Guide batch solve did not start");
      await app.api.registerGuideJob(app.state.guides.workspaceSessionId, "palette_batch", jobId);
      return { job_id: jobId };
    },
  });
  registerGuideAction("export.set_policy", {
    idempotent: true,
    workspacePolicies: ["basic-teaching"],
    validate(input) {
      if (input.output_format != null && !["3mf", "stls"].includes(input.output_format)) {
        throw new Error("export.set_policy has an invalid output_format");
      }
      if (
        input.geometry_source != null
        && !["field_derived", "exact_raster"].includes(input.geometry_source)
      ) {
        throw new Error("export.set_policy has an invalid geometry_source");
      }
      if (input.field_scale != null && ![2, 4, 8, 16].includes(Number(input.field_scale))) {
        throw new Error("export.set_policy field_scale must be 2, 4, 8, or 16");
      }
    },
    run(input) {
      const values = {
        exportOutputFormat: input.output_format || "3mf",
        exportGeometrySource: input.geometry_source || "field_derived",
        exportFieldScale: String(input.field_scale || 4),
      };
      for (const [id, value] of Object.entries(values)) {
        const control = app.state.ui.$(`#${id}`);
        if (control) control.value = value;
      }
      app.commands.handleExportOptionChange?.();
      return values;
    },
  });
  registerGuideAction("export.generate", {
    workspacePolicies: ["basic-teaching"],
    resourceDisposition: "persistent",
    async run() {
      const accepted = await app.commands.handleExportFiles({ throwOnError: true });
      const jobId = accepted?.status?.job_id || app.state.export.activeExportJobId || null;
      if (jobId) await app.api.registerGuideJob(app.state.guides.workspaceSessionId, "export", jobId);
      return {
        export_id: accepted?.exportRecord?.id || app.state.export.currentExportId || null,
        job_id: jobId,
      };
    },
  });

  Object.assign(app.commands, {
    registerGuideAction,
    executeGuideAction,
    executeGuideActions,
    guideActionDefinitions: actions,
  });
}
