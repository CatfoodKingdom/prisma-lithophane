/**
 * Install authoritative settings-contract and contextual-evaluation commands.
 * @param {import("../../core/types.js").ApplicationContext} app
 */
export function installFeaturesSettingsContract(app) {
  function clone(value) {
    return value == null ? value : JSON.parse(JSON.stringify(value));
  }

  function hydrateSettingsContract(contract) {
    if (!contract || !Array.isArray(contract.settings) || !Array.isArray(contract.profile_keys)) {
      throw new Error("Settings contract response is incomplete");
    }
    const specs = Object.fromEntries(contract.settings.map((spec) => [spec.key, clone(spec)]));
    for (const key of contract.profile_keys) {
      if (!specs[key]) throw new Error(`Settings contract is missing ${key}`);
    }
    app.state.settings.settingsContract = clone(contract);
    app.state.settings.settingsSpecs = specs;
    app.state.settings.lastColorCapMode = app.commands.loadLastColorCapMode(
      app.state.settings.config.cap_mode,
    );
    app.state.settings.SETTINGS_PROFILE_KEYS = [...contract.profile_keys];
    app.state.settings.SETTINGS_PROFILE_DEFAULTS = Object.fromEntries(
      contract.profile_keys.map((key) => [key, clone(specs[key].default)]),
    );
    for (const key of contract.profile_keys) {
      if (!Object.prototype.hasOwnProperty.call(app.state.settings.config, key)) {
        app.state.settings.config[key] = clone(specs[key].default);
      }
    }
    app.state.settings.settingsContractAvailable = true;
    applyContractConstraints();
    annotateSettingsRows();
    setSettingsContractDisconnected(false);
    return contract;
  }

  function settingSpec(key) {
    return app.state.settings.settingsSpecs?.[key] || null;
  }

  function settingDefault(key) {
    return clone(settingSpec(key)?.default);
  }

  function settingNumberOptions(key, overrides = {}) {
    const spec = settingSpec(key) || {};
    return {
      ...(spec.minimum != null ? { min: spec.minimum } : {}),
      ...(spec.maximum != null ? { max: spec.maximum } : {}),
      ...(spec.kind === "int" ? { integer: true, parse: (value) => parseInt(value, 10) } : {}),
      ...overrides,
    };
  }

  function annotateSettingsRows() {
    for (const section of app.state.ui.SETTINGS_PRESENTATION || []) {
      for (const row of section.rows || []) {
        const control = row.controlId ? app.state.ui.$(`#${row.controlId}`) : null;
        const tableRow = control?.closest?.("tr");
        if (tableRow) tableRow.dataset.settingKey = row.key;
      }
    }
    for (const mod of app.state.settings.moduleData || []) {
      const preset = app.state.ui.$(`#mod_${mod.name}_preset`)?.closest?.("tr");
      if (preset) preset.dataset.settingKey = `module:${mod.name}`;
      for (const param of Object.values(mod.params || {})) {
        const row = app.state.ui.$(`#mod_${mod.name}_${param.name}`)?.closest?.("tr");
        if (row) row.dataset.settingKey = `module:${mod.name}:${param.name}`;
      }
    }
  }

  function applyContractConstraints() {
    for (const section of app.state.ui.SETTINGS_PRESENTATION || []) {
      for (const row of section.rows || []) {
        const spec = settingSpec(row.key);
        const control = row.controlId ? app.state.ui.$(`#${row.controlId}`) : null;
        if (!spec || !control) continue;
        const displayScale = row.format === "percent" ? 100 : 1;
        const displayMinimum = spec.minimum == null ? null : spec.minimum * displayScale;
        const displayMaximum = spec.maximum == null ? null : spec.maximum * displayScale;
        const transformOwnsInputRange = !!row.inputTransform;
        if (displayMinimum != null) {
          control.dataset.contractMin = String(displayMinimum);
          if (!transformOwnsInputRange) control.min = String(displayMinimum);
        }
        if (displayMaximum != null) {
          control.dataset.contractMax = String(displayMaximum);
          if (!transformOwnsInputRange) control.max = String(displayMaximum);
        }
      }
    }
  }

  function settingsContextIdentity() {
    const activePrinter = app.state.session.printersData?.active_printer_id || null;
    const activeNozzle = app.state.session.activeNozzle?.diameter_um != null
      ? Number(app.state.session.activeNozzle.diameter_um) / 1000
      : null;
    const activeExtrusionWidth = app.state.session.activeExtrusionWidth?.width_um != null
      ? Number(app.state.session.activeExtrusionWidth.width_um) / 1000
      : null;
    const selected = app.state.image.selectedImage || {};
    return JSON.stringify({
      printer_id: activePrinter,
      nozzle_size_mm: activeNozzle,
      extrusion_width_mm: activeExtrusionWidth,
      image_path: selected.filename || app.state.settings.config.image_path || null,
      image_source_ref: selected.source_ref || app.state.settings.config.image_source_ref || null,
      frame: app.state.settings.config.frame || null,
      module_state: Object.fromEntries(Object.entries(app.state.settings.moduleState || {}).sort()),
      appearance_model_provider: app.state.settings.config.appearance_model_provider || null,
      base_filament: app.state.settings.config.base_filament || null,
    });
  }

  function beginSettingsEvaluationRequest() {
    app.state.settings.settingsEvaluationGeneration += 1;
    // Keep the last authoritative evaluation for operation blocking, but stop
    // presenting it as soon as a newer local settings state is awaiting
    // evaluation. Otherwise dependency badges can contradict controls that
    // have already updated (for example Shading balance after selecting
    // Luminance mode).
    app.state.settings.settingsEvaluationPresentationCurrent = false;
    renderSettingsEvaluation();
    return {
      generation: app.state.settings.settingsEvaluationGeneration,
      contextIdentity: settingsContextIdentity(),
    };
  }

  function evaluationContextMatchesCurrent(evaluation) {
    const context = evaluation?.context;
    if (!context) return false;
    const current = JSON.parse(settingsContextIdentity());
    const source = context.source_identity || {};
    const appearance = context.appearance_identity || {};
    const normalizedModules = Object.fromEntries(Object.entries(context.module_state || {}).sort());
    return context.printer_id === current.printer_id
      && Number(context.nozzle_size_mm) === Number(current.nozzle_size_mm)
      && Number(context.extrusion_width_mm) === Number(current.extrusion_width_mm)
      && (source.image_path || null) === current.image_path
      && (source.image_source_ref || null) === current.image_source_ref
      && JSON.stringify(source.frame || null) === JSON.stringify(current.frame || null)
      && JSON.stringify(normalizedModules) === JSON.stringify(current.module_state)
      && (appearance.provider || null) === current.appearance_model_provider
      && (appearance.base_filament || null) === current.base_filament;
  }

  function applySettingsEvaluationResponse(response, ticket = null) {
    const evaluation = response?.settings_evaluation;
    if (!evaluation) return false;
    const request = ticket || {
      generation: app.state.settings.settingsEvaluationGeneration,
      contextIdentity: settingsContextIdentity(),
    };
    if (request.generation !== app.state.settings.settingsEvaluationGeneration) return false;
    if (request.contextIdentity !== settingsContextIdentity()) return false;
    if (!evaluationContextMatchesCurrent(evaluation)) return false;
    app.state.settings.settingsEvaluation = clone(evaluation);
    app.state.settings.settingsEvaluationFingerprint = evaluation.context_fingerprint || null;
    app.state.settings.settingsEvaluationPresentationCurrent = true;
    renderSettingsEvaluation();
    return true;
  }

  function formatEvaluationNumber(value) {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return String(value ?? "");
    return numeric.toFixed(6).replace(/0+$/, "").replace(/\.$/, "");
  }

  function appendCompactStatus(row, status) {
    const label = {
      inactive: "Inactive",
      no_op: "No effect now",
      unavailable: "Unavailable",
    }[status];
    if (!row || !label) return;
    const cell = row.querySelector?.("td:first-child");
    if (!cell) return;
    const badge = document.createElement("span");
    badge.className = `settings-row-status is-${status}`;
    badge.textContent = label;
    cell.appendChild(badge);
  }

  function renderContextSummary(id, lines) {
    const host = app.state.ui.$(`#${id}`);
    if (!host) return;
    host.textContent = "";
    for (const line of lines.filter(Boolean)) {
      const item = document.createElement("div");
      item.textContent = line;
      host.appendChild(item);
    }
  }

  function renderBaseThicknessRecommendation(evaluation) {
    const host = app.state.ui.$("#baseThicknessRecommendation");
    if (!host) return;
    const recommendation = evaluation.values?.d_wb?.recommendation;
    const nozzle = evaluation.context?.nozzle_size_mm;
    if (!recommendation || nozzle == null) {
      host.textContent = "";
      host.hidden = true;
      return;
    }
    const recommended = recommendation.value != null
      ? `${formatEvaluationNumber(recommendation.value)} mm`
      : `${formatEvaluationNumber(recommendation.minimum)}–${formatEvaluationNumber(recommendation.maximum)} mm`;
    host.textContent = `Base thickness: ${recommended} recommended for ${formatEvaluationNumber(nozzle)} mm nozzle`;
    host.hidden = false;
  }

  function renderSettingsEvaluation() {
    annotateSettingsRows();
    applyContractConstraints();
    // Keep the last authoritative evaluation visible while its replacement is
    // pending. Ranges and summaries occupy layout space, so clearing them here
    // makes the drawer collapse and then snap back when the response arrives.
    const evaluation = app.state.settings.settingsEvaluation || {};
    const statusEvaluation = app.state.settings.settingsEvaluationPresentationCurrent
      ? evaluation
      : {};
    for (const row of app.state.ui.$$("[data-setting-key]") || []) {
      row.querySelector?.(".settings-row-status")?.remove?.();
      row.classList?.remove?.("is-setting-inactive", "is-setting-invalid");
    }
    for (const [key, item] of Object.entries(statusEvaluation.values || {})) {
      const row = app.state.ui.$(`[data-setting-key="${key}"]`);
      row?.classList?.toggle?.("is-setting-inactive", item?.status === "inactive");
      row?.classList?.toggle?.("is-setting-invalid", item?.status === "invalid_context");
      appendCompactStatus(row, item?.status);
    }
    for (const [moduleId, item] of Object.entries(statusEvaluation.modules || {})) {
      if (!["no_op", "inactive", "unavailable"].includes(item.status)) continue;
      const row = app.state.ui.$(`[data-setting-key="module:${moduleId}"]`);
      appendCompactStatus(row, item.status);
    }

    const planning = evaluation.values?.stage1_coarsening_factor?.derived || {};
    const planningCells = planning.cells
      ? ` · ${planning.cells.width} × ${planning.cells.height} cells`
      : "";
    const regionTarget = evaluation.values?.color_region_target_mm;
    renderContextSummary("regionPlanningSummary", [
      planning.pitch_mm != null
        ? `Planning spacing: ${formatEvaluationNumber(planning.pitch_mm)} mm${planningCells}`
        : "",
      regionTarget?.status === "adjusted"
        ? `Color region target: ${formatEvaluationNumber(regionTarget.requested)} mm requested · ${formatEvaluationNumber(regionTarget.effective)} mm effective`
        : "",
    ]);

    const cap = evaluation.values?.min_cap_layers?.derived || {};
    renderContextSummary("boundaryCapSummary", [
      cap.minimum_cap_layers != null && cap.minimum_cap_thickness_mm != null
        ? `Minimum cap: ${cap.minimum_cap_layers} layers = ${formatEvaluationNumber(cap.minimum_cap_thickness_mm)} mm`
        : "",
    ]);
    renderBaseThicknessRecommendation(evaluation);
  }

  function settingsBlocksOperation(operation) {
    return (app.state.settings.settingsEvaluation?.issues || []).some(
      (issue) => (issue.blocked_operations || []).includes(operation),
    );
  }

  function setSettingsContractDisconnected(disconnected, message = "Settings are unavailable. Reconnect to edit them.") {
    app.state.settings.settingsContractAvailable = !disconnected;
    const host = app.state.ui.$("#settingsDrawerBody") || app.state.ui.$("#tabSettings");
    if (!host) return;
    let notice = host.querySelector?.("#settingsContractStatus") || null;
    if (disconnected && !notice) {
      notice = document.createElement("div");
      notice.id = "settingsContractStatus";
      notice.className = "settings-contract-status stg-warn";
      host.prepend?.(notice);
    }
    if (notice) {
      notice.textContent = message;
      notice.hidden = !disconnected;
    }
    for (const control of host.querySelectorAll?.("input, select, button") || []) {
      if (control.id === "settingsDrawerClose") continue;
      if (disconnected) {
        if (control.dataset.settingsContractWasDisabled == null) {
          control.dataset.settingsContractWasDisabled = control.disabled ? "1" : "0";
        }
        control.disabled = true;
      } else if (control.dataset.settingsContractWasDisabled != null) {
        control.disabled = control.dataset.settingsContractWasDisabled === "1";
        delete control.dataset.settingsContractWasDisabled;
      }
    }
  }

  Object.assign(app.commands, {
    annotateSettingsRows,
    applyContractConstraints,
    applySettingsEvaluationResponse,
    beginSettingsEvaluationRequest,
    hydrateSettingsContract,
    renderSettingsEvaluation,
    setSettingsContractDisconnected,
    settingSpec,
    settingDefault,
    settingNumberOptions,
    settingsContextIdentity,
    settingsBlocksOperation,
  });
}
