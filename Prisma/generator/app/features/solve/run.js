/**
 * Install the solve/run feature commands.
 * @param {import("../../core/types.js").ApplicationContext} app
 */
export function installFeaturesSolveRun(app) {
function buildSolveInspectorBlock(title, items, extraMeta = "", chipsHtml = "") {
    const rows = items.map(({ label, value }) => `
        <div class="solve-inspector-item">
          <span>${app.commands.esc(label)}</span>
          <strong>${app.commands.esc(value ?? "—")}</strong>
        </div>`).join("");
    return `
      <section class="solve-inspector-block">
        <div class="solve-inspector-title">${app.commands.esc(title)}</div>
        ${chipsHtml ? `<div class="solve-inspector-chips">${chipsHtml}</div>` : ""}
        ${extraMeta ? `<div class="muted-line" style="margin-bottom:8px">${app.commands.esc(extraMeta)}</div>` : ""}
        <div class="solve-inspector-items">${rows}</div>
      </section>`;
  }

function getSolveRunSummaryItems(run) {
    const profileInfo = app.commands.describeSolveRunProfile(run);
    return [
      { label: "Profile", value: profileInfo.name },
      ...app.commands.getSolveRunEssentialsItems(run),
    ];
  }

function buildSolveRunCardMetadataFooter(run) {
    const items = app.commands.getSolveRunSummaryItems(run);
    const itemsHtml = items.map(({ label, value }) => `
        <span class="solve-card-meta-item">
          <span>${app.commands.esc(label)}</span>
          <strong>${app.commands.esc(value ?? "—")}</strong>
        </span>`).join("");
    return `
      <div class="solve-card-run-meta">
        <div class="solve-card-meta-items">${itemsHtml}</div>
      </div>`;
  }

function humanizeModuleName(name) {
    return String(name || "").replace(/_/g, " ");
  }

function getSolveSettingLabel(key) {
    if (app.state.ui.SOLVE_DIFF_SETTING_LABELS[key]) return app.state.ui.SOLVE_DIFF_SETTING_LABELS[key];
    for (const mod of app.state.settings.moduleData || []) {
      const param = Object.values(mod.params || {}).find(
        (p) => p.name === key || p.storage_key === key,
      );
      if (param?.label) return param.label;
    }
    return app.commands.humanizeModuleName(key);
  }

function formatSolveSettingValue(key, value) {
    if (typeof value === "boolean") return value ? "on" : "off";
    if (value == null || value === "") return "—";
    if (Array.isArray(value)) {
      if (key === "palette") {
        return value.map((id) => app.commands.filamentById(id)?.color_name || id).join(", ");
      }
      return value.map((item) => app.commands.formatSolveSettingValue(key, item)).join(", ");
    }
    if (typeof value === "number") {
      if (/(_mm|_deg)$/.test(key) || ["layer_height", "d_wb", "t_max", "de_threshold", "boundary_cap_de_budget"].includes(key)) {
        return String(value).includes(".")
          ? value.toFixed(3).replace(/\.?0+$/, "")
          : String(value);
      }
      if (["chroma_weight", "hybrid_split_ratio"].includes(key)) {
        return value.toFixed(3).replace(/\.?0+$/, "");
      }
      return String(value);
    }
    if (typeof value === "string" && (key === "cap_mode" || key === "luminance_mode")) {
      return value.replace(/_/g, " ");
    }
    if (typeof value === "string" && key === "cell_mode") {
      return app.commands.formatRegionMethod(value);
    }
    return String(value);
  }

function getSolveRunSettingsSnapshot(run) {
    const settings = app.commands._cloneValue(
      run?.recipe_snapshot?.profile_snapshot?.settings || run?.config || {},
    );
    return settings;
  }

function getSolveRunModulesSnapshot(run) {
    return app.commands._normalizeSettingsProfileModules(
      run?.recipe_snapshot?.profile_snapshot?.modules,
      app.commands.getSolveRunSettingsSnapshot(run),
    );
  }

function categorizeSolveSettingDiff(key, kind = "setting") {
    if (kind === "preprocessing") return "preprocessing";
    if ([
      "layer_height", "d_wb", "min_cap_layers", "t_max", "k_max",
      "base_filament", "cap_filament",
    ].includes(key)) return "geometry";
    if ([
      "image_sample_pitch_mm", "solver_fine_pitch_mm",
      "color_region_target_mm", "chroma_weight", "luminance_mode", "cell_mode",
      "neutral_field_protection_enabled",
      "luminance_handler_enabled",
      "luminance_handler_mode",
      "luminance_handler_strength",
      "luminance_handler_optical_authority_fraction",
      "luminance_base_shading_limit_fraction",
      "luminance_handler_boundary_percentile",
      "luminance_handler_boundary_sigma_px",
      "luminance_handler_response_curve",
      "luminance_handler_response_gamma",
      "luminance_handler_detail_residual",
      "luminance_handler_include_solver_detail",
      "luminance_detail_authoring_printability",
    ].includes(key)) return "solver";
    if ([
      "cap_mode", "boundary_cap_de_budget", "boundary_cap_smoothing_radius_mm",
      "smooth_radius_mm", "hybrid_split_ratio",
      "detail_cap_max_layers",
      "detail_cap_smoothing_enabled",
      "detail_cap_smoothing_exact_speckle_max_px",
      "detail_cap_smoothing_cumulative_component_max_px",
      "detail_cap_smoothing_cumulative_hole_max_px",
    ].includes(key)) return "white-cap";
    return "other";
  }

function getSortedModuleParams(mod) {
    return Object.values(mod?.params || {}).sort((a, b) => {
      const orderA = a?.order ?? 0;
      const orderB = b?.order ?? 0;
      if (orderA !== orderB) return orderA - orderB;
      return String(a?.name || "").localeCompare(String(b?.name || ""));
    });
  }

function getSolveModuleParamLabel(moduleId, param) {
    const prefix = app.commands.humanizeModuleName(moduleId);
    const suffix = param?.label || app.commands.humanizeModuleName(app.commands.moduleParamStorageKey(moduleId, param));
    return `${prefix} · ${suffix}`;
  }

function collectPreprocessingSettingDiffs(beforeSettings, afterSettings, beforeModules, afterModules) {
    const diffs = [];
    const preprocessingModules = (app.state.settings.moduleData || []).filter((entry) => entry.slot === "preprocessing");
    for (const mod of preprocessingModules) {
      const moduleName = mod.name;
      const beforeEnabled = !!beforeModules[moduleName];
      const afterEnabled = !!afterModules[moduleName];
      if (!beforeEnabled && !afterEnabled) continue;
      for (const param of app.commands.getSortedModuleParams(mod)) {
        const beforeValue = app.commands.getModuleParamValue(beforeSettings, moduleName, param);
        const afterValue = app.commands.getModuleParamValue(afterSettings, moduleName, param);
        if (app.commands._settingsProfileValuesEqual(beforeValue, afterValue)) continue;
        const valueKey = app.commands.moduleParamStorageKey(moduleName, param);
        diffs.push({
          label: app.commands.getSolveModuleParamLabel(moduleName, param),
          before: app.commands.formatSolveSettingValue(valueKey, beforeValue),
          after: app.commands.formatSolveSettingValue(valueKey, afterValue),
          sortKey: `${moduleName}:${valueKey}`,
          category: "preprocessing",
        });
      }
    }
    return diffs;
  }

function collectSolveRunSettingDiffs(beforeRun, afterRun) {
    const beforeSettings = app.commands.getSolveRunSettingsSnapshot(beforeRun);
    const afterSettings = app.commands.getSolveRunSettingsSnapshot(afterRun);
    const beforeModules = app.commands.getSolveRunModulesSnapshot(beforeRun);
    const afterModules = app.commands.getSolveRunModulesSnapshot(afterRun);
    const diffs = [];

    for (const key of app.state.settings.SETTINGS_PROFILE_KEYS) {
      if (key === "preprocessing_params") continue;
      if (app.commands._settingsProfileValuesEqual(beforeSettings[key], afterSettings[key])) continue;
      diffs.push({
        label: app.commands.getSolveSettingLabel(key),
        before: app.commands.formatSolveSettingValue(key, beforeSettings[key]),
        after: app.commands.formatSolveSettingValue(key, afterSettings[key]),
        sortKey: key,
        category: app.commands.categorizeSolveSettingDiff(key, "setting"),
      });
    }

    diffs.push(...app.commands.collectPreprocessingSettingDiffs(
      beforeSettings, afterSettings, beforeModules, afterModules,
    ));

    for (const mod of (app.state.settings.moduleData || []).filter((entry) => entry.slot === "preprocessing")) {
      const name = mod.name;
      const beforeEnabled = !!beforeModules[name];
      const afterEnabled = !!afterModules[name];
      if (beforeEnabled === afterEnabled) continue;
      diffs.push({
        label: `${app.commands.humanizeModuleName(name)} module`,
        before: beforeEnabled ? "on" : "off",
        after: afterEnabled ? "on" : "off",
        sortKey: `module:${name}`,
        category: "preprocessing",
      });
    }

    return diffs.sort((a, b) => {
      const categoryDelta = app.state.ui.SOLVE_DIFF_CATEGORY_ORDER.indexOf(a.category) - app.state.ui.SOLVE_DIFF_CATEGORY_ORDER.indexOf(b.category);
      if (categoryDelta !== 0) return categoryDelta;
      return a.label.localeCompare(b.label);
    });
  }

function buildGroupedSolveSettingDiffBlocks(beforeRun, afterRun, diffs) {
    const groups = new Map();
    for (const diff of diffs) {
      if (!groups.has(diff.category)) groups.set(diff.category, []);
      groups.get(diff.category).push(diff);
    }
    const meta = `${beforeRun.label} -> ${afterRun.label}`;
    const blocks = [];
    let first = true;
    for (const category of app.state.ui.SOLVE_DIFF_CATEGORY_ORDER) {
      const entries = groups.get(category);
      if (!entries?.length) continue;
      blocks.push(app.commands.buildSolveInspectorBlock(
        app.state.ui.SOLVE_DIFF_CATEGORY_TITLES[category] || "Changed Settings",
        entries.map((diff) => ({
          label: diff.label,
          value: `${diff.before} -> ${diff.after}`,
        })),
        first ? meta : "",
      ));
      first = false;
    }
    return blocks;
  }

function getSolveRunActiveModulesForSlot(run, slot) {
    const diagnostics = run?.results?.solve_start_diagnostics || {};
    const active = diagnostics.active_modules || {};
    if (Array.isArray(active[`${slot}s`]) && active[`${slot}s`].length) {
      return active[`${slot}s`];
    } else if (slot === "preprocessing" && Array.isArray(active.preprocessing) && active.preprocessing.length) {
      return active.preprocessing;
    }
    const modules = app.commands.getSolveRunModulesSnapshot(run);
    return (app.state.settings.moduleData || [])
      .filter((mod) => mod.slot === slot && modules[mod.name])
      .map((mod) => mod.name);
  }

function buildSolveRunInspectorBlock(run) {
    // Shared run summary: palette chips + the run-bound essentials list.
    return app.commands.buildSolveInspectorBlock(
      run.label,
      app.commands.getSolveRunSummaryItems(run),
      "",
      app.commands.buildSolveRunPaletteChips(run),
    );
  }

function buildSolveRunEssentialsSummary(run) {
    // Compact run-bound summary used by the shared hover/focus preview.
    return app.commands.buildSolveRunInspectorBlock(run);
  }

function getFrozenSolveRunSnapshot(run) {
    const runConfig = app.commands._cloneValue(run?.config || {});
    const profile = app.commands._cloneValue(run?.recipe_snapshot?.profile_snapshot || {});
    const diagnostics = app.commands._cloneValue(run?.results?.solve_start_diagnostics || {});
    const profileSettings = profile.settings && typeof profile.settings === "object"
      ? profile.settings
      : {};
    const resolvedSettings = diagnostics.resolved_settings && typeof diagnostics.resolved_settings === "object"
      ? diagnostics.resolved_settings
      : {};
    const settings = {
      ...runConfig,
      ...profileSettings,
      ...resolvedSettings,
    };
    const profileModules = profile.modules && typeof profile.modules === "object"
      ? profile.modules
      : {};
    const profileModulesKnown = Object.keys(profileModules).length > 0;
    const activeDiagnostics = diagnostics.active_modules && typeof diagnostics.active_modules === "object"
      ? diagnostics.active_modules
      : {};
    const diagnosticModuleState = diagnostics.module_state && typeof diagnostics.module_state === "object"
      ? diagnostics.module_state
      : {};
    const diagnosticModuleStateKnown = Object.keys(diagnosticModuleState).length > 0;
    const diagnosticPreprocessing = Array.isArray(activeDiagnostics.preprocessing)
      ? activeDiagnostics.preprocessing.map(String)
      : null;
    const activePreprocessing = diagnosticPreprocessing != null
      ? diagnosticPreprocessing.filter((name) => app.commands.moduleDescriptorById(name)?.slot === "preprocessing")
      : diagnosticModuleStateKnown
        ? Object.keys(diagnosticModuleState).filter((name) => (
          diagnosticModuleState[name]
          && app.commands.moduleDescriptorById(name)?.slot === "preprocessing"
        ))
        : profileModulesKnown
          ? Object.keys(profileModules).filter((name) => (
            profileModules[name]
            && app.commands.moduleDescriptorById(name)?.slot === "preprocessing"
          ))
          : [];

    const diagnosticModuleSettings = diagnostics.module_settings && typeof diagnostics.module_settings === "object"
      ? diagnostics.module_settings
      : {};
    const preprocessingParams = app.commands._cloneValue(settings.preprocessing_params || {});
    for (const [moduleId, values] of Object.entries(diagnosticModuleSettings)) {
      if (!values || typeof values !== "object") continue;
      preprocessingParams[moduleId] = {
        ...(preprocessingParams[moduleId] || {}),
        ...values,
      };
    }
    settings.preprocessing_params = preprocessingParams;

    return {
      settings,
      activePreprocessing: new Set(activePreprocessing),
      preprocessingStateKnown: diagnosticPreprocessing != null || diagnosticModuleStateKnown || profileModulesKnown,
      hasDiagnostics: Object.keys(diagnostics).length > 0,
    };
  }

function formatReadOnlyRunSetting(row, value, settings, { defaultValue } = {}) {
    const missing = value == null || value === "";
    const resolved = missing && defaultValue !== undefined ? defaultValue : value;
    if (resolved == null || resolved === "") return "Unavailable in saved run";
    const prefix = missing ? "Default: " : "";
    if (typeof resolved === "boolean") return `${prefix}${resolved ? "Enabled" : "Disabled"}`;
    if (row.format === "solve-mode") {
      return `${prefix}${app.commands.normalizeLuminanceMode(resolved) === "luminance_detail" ? "Luminance" : "Color"}`;
    }
    if (row.format === "filament") {
      const fil = app.commands.filamentById(resolved);
      return `${prefix}${app.commands.railHoverFilamentLabel(fil, String(resolved))}`;
    }
    if (row.format === "appearance-model") {
      if (resolved === "photo_stack_bundle") return `${prefix}Color Model v2`;
      if (resolved === "legacy_lut" || resolved === "historical_spline") return `${prefix}Color Model v1`;
    }
    if (row.format === "title") {
      return `${prefix}${String(resolved).replace(/_/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase())}`;
    }
    if (row.format === "gamut-mode") {
      return `${prefix}${resolved === "hue_preserving" ? "Preserve hue" : "Nearest reachable color"}`;
    }
    if (row.format === "region-method") {
      const label = app.commands.formatRegionMethod(resolved);
      return `${prefix}${label.charAt(0).toUpperCase() + label.slice(1)}`;
    }
    if (row.format === "region-scale") return `${prefix}${app.commands.formatRegionPlanningScale(resolved)}`;
    if (row.format === "cap-mode") {
      return `${prefix}${resolved === "appearance_bounded_smooth" ? "Detail Aware" : "Smooth"}`;
    }
    if (row.format === "percent") {
      const numeric = Number(resolved);
      return `${prefix}${Number.isFinite(numeric) ? `${Math.round(numeric * 100)}%` : String(resolved)}`;
    }
    const formatted = app.commands.formatSolveSettingValue(row.key, resolved);
    return `${prefix}${row.unit && formatted !== "—" ? `${formatted} ${row.unit}` : formatted}`;
  }

function buildReadOnlyRunSectionRows(section, frozen) {
    const rows = [];
    for (const row of section.rows) {
      let value = frozen.settings[row.runKey || row.key];
      if ((value == null || value === "") && row.fallbackKey) value = frozen.settings[row.fallbackKey];
      rows.push({
        label: row.label,
        value: app.commands.formatReadOnlyRunSetting(row, value, frozen.settings, {
          defaultValue: app.commands.settingSpec(row.key)?.default,
        }),
        advanced: !!row.advanced,
        group: row.group || "",
        controlId: row.controlId || null,
      });
      if (row.key === "neutral_field_protection_enabled" && Boolean(value)) {
        const cutoff = frozen.settings.neutral_field_protection_cutoff;
        rows.push({
          label: "Preset",
          value: app.commands.neutralFieldProtectionPresetForCutoff(cutoff)
            .replace(/\b\w/g, (letter) => letter.toUpperCase()),
          advanced: false,
          group: row.group || "",
          controlId: "cfgNeutralFieldProtectionPreset",
        });
      }
    }

    if (section.key !== "preprocessing") return rows;
    const preprocessingModules = (app.state.settings.moduleData || []).filter((entry) => entry.slot === "preprocessing");
    for (const mod of preprocessingModules) {
      const enabled = frozen.activePreprocessing.has(mod.name);
      rows.push({
        label: app.commands.moduleDisplayName(mod),
        value: frozen.preprocessingStateKnown ? (enabled ? "On" : "Off") : "Unavailable in saved run",
        advanced: false,
        // Keep module enablement in the main preprocessing block. Individual
        // module headings are reserved for the advanced parameters that need
        // an ownership label.
        group: "",
        controlId: `module:${mod.name}`,
      });
      if (!frozen.preprocessingStateKnown || !enabled) continue;
      const projected = app.commands.projectModuleConfigValues(mod.name, mod, frozen.settings);
      const presetSpec = app.commands.preprocessingPresetSpec(mod.name);
      if (presetSpec) {
        const preset = (presetSpec.presets || []).find((candidate) => (
          candidate.values && Object.entries(candidate.values).every(([key, expected]) => {
            const actual = projected[key];
            return typeof expected === "number"
              ? Number.isFinite(Number(actual)) && Math.abs(Number(actual) - expected) < 1e-9
              : String(actual) === String(expected);
          })
        ));
        rows.push({
          label: "Preset",
          value: preset?.label || "Custom",
          advanced: true,
          group: app.commands.moduleDisplayName(mod),
          controlId: `mod_${mod.name}_preset`,
        });
      }
      for (const param of app.commands.getSortedModuleParams(mod)) {
        if (!app.commands.isModuleParamVisibleInSummary(param, projected)) continue;
        const choiceLabels = param.choice_labels || {};
        const parameterBlock = frozen.settings.preprocessing_params?.[mod.name];
        const hasCapturedValue = parameterBlock
          && Object.prototype.hasOwnProperty.call(parameterBlock, param.name)
          && projected[param.name] != null;
        let paramValue = hasCapturedValue ? projected[param.name] : param.default;
        if (param.type === "choice" && choiceLabels[paramValue]) paramValue = choiceLabels[paramValue];
        const formattedValue = String(app.commands.formatSolveSummaryValue(param, paramValue));
        rows.push({
          label: param.label || app.commands.humanizeModuleName(param.name),
          value: `${hasCapturedValue ? "" : "Default: "}${formattedValue}`,
          advanced: true,
          group: app.commands.moduleDisplayName(mod),
          controlId: `mod_${mod.name}_${param.name}`,
        });
      }
    }
    return rows;
  }

function buildReadOnlyRunSettingsHtml(run) {
    const frozen = app.commands.getFrozenSolveRunSnapshot(run);
    const sections = app.state.ui.SETTINGS_PRESENTATION.map((section) => {
      const groupedRows = new Map();
      for (const row of app.commands.buildReadOnlyRunSectionRows(section, frozen)) {
        const group = row.group || "";
        if (!groupedRows.has(group)) groupedRows.set(group, []);
        groupedRows.get(group).push(row);
      }
      const blocks = Array.from(groupedRows.entries()).map(([group, blockRows]) => {
        const advancedOnly = blockRows.length > 0 && blockRows.every((row) => row.advanced);
        const rows = blockRows
          .map((row) => `<div class="run-settings-row${row.advanced ? " is-advanced" : ""}"${row.controlId ? ` data-control-id="${app.commands.esc(row.controlId)}"` : ""}>
            <span class="run-settings-label">${app.commands.esc(row.label)}</span>
            <span class="run-settings-value">${app.commands.esc(row.value)}</span>
          </div>`)
          .join("");
        return `${group ? `<h5 class="run-settings-subsection-cap${advancedOnly ? " is-advanced-only" : ""}">${app.commands.esc(group)}</h5>` : ""}
          <div class="run-settings-rows${advancedOnly ? " is-advanced-only" : ""}">${rows}</div>`;
      }).join("");
      return `<section class="run-settings-section" data-run-settings-section="${app.commands.esc(section.key)}">
        <h4 class="settings-group-cap run-settings-section-cap">${app.commands.esc(section.title)}</h4>
        ${blocks}
      </section>`;
    }).join("");
    const archiveNote = frozen.hasDiagnostics
      ? "Values captured when this solve started."
      : "Older saved run: missing values are shown as product defaults or unavailable.";
    return `<div class="run-settings-note">${app.commands.esc(archiveNote)}</div><div class="run-settings-sections">${sections}</div>`;
  }

function clearSolveRunHoverTimer() {
    if (app.state.solve.solveRunHoverTimer) clearTimeout(app.state.solve.solveRunHoverTimer);
    app.state.solve.solveRunHoverTimer = null;
    app.state.solve.solveRunHoverPendingRunId = null;
  }

function clearSolveRunHoverCloseTimer() {
    if (app.state.solve.solveRunHoverCloseTimer) clearTimeout(app.state.solve.solveRunHoverCloseTimer);
    app.state.solve.solveRunHoverCloseTimer = null;
  }

function isSolveRunHoverBlockedTarget(target) {
    return Boolean(target?.closest?.(".solve-run-card-actions, .solve-run-settings-btn, button, a, input, select"));
  }

function positionSolveRunHoverPreview(panel, anchorEl) {
    const rect = anchorEl.getBoundingClientRect();
    const panelRect = panel.getBoundingClientRect();
    const gap = 10;
    const pad = 10;
    const isPreviewSidebar = anchorEl.closest("#solveRunCards");
    let left = isPreviewSidebar ? rect.left - panelRect.width - gap : rect.right + gap;
    if (left < pad || left + panelRect.width > window.innerWidth - pad) {
      left = isPreviewSidebar ? rect.right + gap : rect.left - panelRect.width - gap;
    }
    left = Math.max(pad, Math.min(left, window.innerWidth - panelRect.width - pad));
    const top = Math.max(pad, Math.min(rect.top, window.innerHeight - panelRect.height - pad));
    panel.style.left = `${Math.round(left)}px`;
    panel.style.top = `${Math.round(top)}px`;
  }

function showSolveRunHoverPreview(runId, anchorEl) {
    app.commands.clearSolveRunHoverTimer();
    app.commands.clearSolveRunHoverCloseTimer();
    const run = app.state.solve.solveRuns.find((entry) => entry.id === runId);
    if (!run || !anchorEl || !document.body.contains(anchorEl)) return;
    app.commands.hideSolveRunHoverPreview();
    app.state.solve.solveRunHoverPreviewEl = document.createElement("div");
    app.state.solve.solveRunHoverPreviewEl.className = "solve-run-hover-preview";
    app.state.solve.solveRunHoverPreviewEl.setAttribute("role", "tooltip");
    app.state.solve.solveRunHoverPreviewEl.innerHTML = app.commands.buildSolveRunEssentialsSummary(run);
    app.state.solve.solveRunHoverPreviewEl.addEventListener("mouseenter", () => app.commands.clearSolveRunHoverCloseTimer());
    app.state.solve.solveRunHoverPreviewEl.addEventListener("mouseleave", () => app.commands.scheduleHideSolveRunHoverPreview(100));
    document.body.appendChild(app.state.solve.solveRunHoverPreviewEl);
    app.state.solve.solveRunHoverRunId = runId;
    app.commands.positionSolveRunHoverPreview(app.state.solve.solveRunHoverPreviewEl, anchorEl);
    requestAnimationFrame(() => app.state.solve.solveRunHoverPreviewEl?.classList.add("is-visible"));
  }

function scheduleSolveRunHoverPreview(runId, anchorEl, delayMs = 380) {
    if (app.state.solve.solveRunHoverRunId === runId && app.state.solve.solveRunHoverPreviewEl?.classList.contains("is-visible")) return;
    if (app.state.solve.solveRunHoverPendingRunId === runId) return;
    app.commands.clearSolveRunHoverTimer();
    app.commands.clearSolveRunHoverCloseTimer();
    app.state.solve.solveRunHoverPendingRunId = runId;
    app.state.solve.solveRunHoverTimer = setTimeout(() => {
      app.state.solve.solveRunHoverPendingRunId = null;
      app.commands.showSolveRunHoverPreview(runId, anchorEl);
    }, delayMs);
  }

function scheduleHideSolveRunHoverPreview(delayMs = 160) {
    app.commands.clearSolveRunHoverTimer();
    app.commands.clearSolveRunHoverCloseTimer();
    app.state.solve.solveRunHoverCloseTimer = setTimeout(app.commands.hideSolveRunHoverPreview, delayMs);
  }

function hideSolveRunHoverPreview() {
    app.commands.clearSolveRunHoverTimer();
    app.commands.clearSolveRunHoverCloseTimer();
    app.state.solve.solveRunHoverPreviewEl?.remove();
    app.state.solve.solveRunHoverPreviewEl = null;
    app.state.solve.solveRunHoverRunId = null;
  }

function positionSolveRunSettingsPanel(panel, anchorEl, context) {
    if (!panel || !anchorEl) return;
    const sidebar = anchorEl.closest(".solve-deck-sidebar") || anchorEl;
    const rect = sidebar.getBoundingClientRect();
    const panelRect = panel.getBoundingClientRect();
    const gap = 10;
    const pad = 10;
    let left = context === "preview" ? rect.left - panelRect.width - gap : rect.right + gap;
    const fitsPreferred = left >= pad && left + panelRect.width <= window.innerWidth - pad;
    if (!fitsPreferred) {
      const alternate = context === "preview" ? rect.right + gap : rect.left - panelRect.width - gap;
      if (alternate >= pad && alternate + panelRect.width <= window.innerWidth - pad) left = alternate;
      else left = Math.max(pad, Math.min(left, window.innerWidth - panelRect.width - pad));
    }
    const top = Math.max(pad, Math.min(rect.top, window.innerHeight - panelRect.height - pad));
    panel.style.left = `${Math.round(left)}px`;
    panel.style.top = `${Math.round(top)}px`;
  }

function renderSolveRunSettingsPanel() {
    if (!app.state.solve.solveRunSettingsPanelEl || !app.state.solve.solveRunSettingsPanelRunId) return;
    const run = app.state.solve.solveRuns.find((entry) => entry.id === app.state.solve.solveRunSettingsPanelRunId);
    if (!run) {
      app.commands.hideSolveRunSettingsPanel();
      return;
    }
    app.state.solve.solveRunSettingsPanelEl.setAttribute("aria-label", `Settings used by ${run.label}`);
    const runLabel = app.state.solve.solveRunSettingsPanelEl.querySelector(".run-settings-run-label");
    if (runLabel) runLabel.textContent = run.label;
    app.state.solve.solveRunSettingsPanelEl.classList.toggle("show-advanced-settings", app.state.solve.solveRunSettingsAdvancedVisible);
    const toggle = app.state.solve.solveRunSettingsPanelEl.querySelector(".run-settings-advanced-toggle");
    if (toggle) {
      toggle.textContent = `Advanced: ${app.state.solve.solveRunSettingsAdvancedVisible ? "On" : "Off"}`;
      toggle.classList.toggle("is-active", app.state.solve.solveRunSettingsAdvancedVisible);
      toggle.setAttribute("aria-pressed", app.state.solve.solveRunSettingsAdvancedVisible ? "true" : "false");
    }
    const body = app.state.solve.solveRunSettingsPanelEl.querySelector(".run-settings-body");
    if (body) body.innerHTML = app.commands.buildReadOnlyRunSettingsHtml(run);
  }

function showSolveRunSettingsPanel(runId, context, anchorEl) {
    const run = app.state.solve.solveRuns.find((entry) => entry.id === runId);
    if (!run || !anchorEl) return;
    app.commands.hideSolveRunHoverPreview();
    app.commands.hideSolveRunSettingsPanel();
    app.state.solve.solveRunSettingsPanelRunId = runId;
    app.state.solve.solveRunSettingsPanelContext = context;
    app.state.solve.solveRunSettingsPanelEl = document.createElement("aside");
    app.state.solve.solveRunSettingsPanelEl.className = "run-settings-panel surface-window";
    app.state.solve.solveRunSettingsPanelEl.setAttribute("role", "dialog");
    app.state.solve.solveRunSettingsPanelEl.setAttribute("aria-label", `Settings used by ${run.label}`);
    app.state.solve.solveRunSettingsPanelEl.innerHTML = `
      <div class="surface-header run-settings-header">
        <div class="window-header__title-slot run-settings-title-slot">
          <h3 class="surface-title">Run Settings</h3>
          <span class="run-settings-run-label">${app.commands.esc(run.label)}</span>
        </div>
        <div class="window-header__actions surface-header-actions">
          <button class="ghost-button window-header__button surface-header-button run-settings-load-btn" type="button" title="Use these captured settings as a temporary Settings Profile">Use These Settings</button>
          <button class="view-option-toggle settings-advanced-header-toggle run-settings-advanced-toggle" type="button" aria-pressed="false">Advanced: Off</button>
          <div class="surface-window-controls">
            <button class="close-button window-header__button surface-header-button surface-close run-settings-close" type="button" aria-label="Close run settings" title="Close run settings">${app.commands.xIconSvg()}</button>
          </div>
        </div>
      </div>
      <div class="surface-body run-settings-body"></div>`;
    document.body.appendChild(app.state.solve.solveRunSettingsPanelEl);
    const reposition = () => requestAnimationFrame(() => app.commands.positionSolveRunSettingsPanel(
      app.state.solve.solveRunSettingsPanelEl,
      anchorEl,
      context,
    ));
    app.state.solve.solveRunSettingsPanelReposition = reposition;
    window.addEventListener("resize", reposition);
    app.state.solve.solveRunSettingsPanelEl.querySelector(".run-settings-close")?.addEventListener("click", app.commands.hideSolveRunSettingsPanel);
    app.state.solve.solveRunSettingsPanelEl.querySelector(".run-settings-load-btn")?.addEventListener("click", async () => {
      try {
        const loaded = await app.commands._loadTemporarySettingsFromRun(run, {
          kind: "solve-card",
          run_id: run.id,
          label: run.label,
        });
        if (loaded) {
          app.commands.hideSolveRunSettingsPanel();
          app.events.emit("settings.temp-profile.loaded", {
            profileId: app.state.settings.loadedProfileRef?.id,
            source: app.state.settings.temporarySettingsProfile?.source,
          });
        }
      } catch (error) {
        app.commands.showToast(`Settings could not be loaded: ${error.message}`, "error");
      }
    });
    app.state.solve.solveRunSettingsPanelEl.querySelector(".run-settings-advanced-toggle")?.addEventListener("click", () => {
      app.state.solve.solveRunSettingsAdvancedVisible = !app.state.solve.solveRunSettingsAdvancedVisible;
      app.commands.renderSolveRunSettingsPanel();
      reposition();
    });
    app.commands.renderSolveRunSettingsPanel();
    reposition();
    app.events.emit("solve.run-settings.opened", { runId });
  }

function hideSolveRunSettingsPanel() {
    if (app.state.solve.solveRunSettingsPanelReposition) {
      window.removeEventListener("resize", app.state.solve.solveRunSettingsPanelReposition);
      app.state.solve.solveRunSettingsPanelReposition = null;
    }
    app.state.solve.solveRunSettingsPanelEl?.remove();
    app.state.solve.solveRunSettingsPanelEl = null;
    app.state.solve.solveRunSettingsPanelRunId = null;
    app.state.solve.solveRunSettingsPanelContext = null;
  }

function bindSolveRunCardAuxiliaryInteractions(container, context) {
    container.querySelectorAll(".solve-run-card").forEach((card) => {
      const runId = card.dataset.runId || card.dataset.exportRunId;
      if (!runId) return;
      card.addEventListener("mousemove", (event) => {
        if (app.commands.isSolveRunHoverBlockedTarget(event.target)) {
          app.commands.hideSolveRunHoverPreview();
          return;
        }
        app.commands.scheduleSolveRunHoverPreview(runId, card);
      });
      card.addEventListener("mouseleave", () => app.commands.scheduleHideSolveRunHoverPreview());
      card.addEventListener("focusin", (event) => {
        if (app.commands.isSolveRunHoverBlockedTarget(event.target)) return;
        app.commands.scheduleSolveRunHoverPreview(runId, card, 120);
      });
      card.addEventListener("focusout", (event) => {
        if (!card.contains(event.relatedTarget)) app.commands.scheduleHideSolveRunHoverPreview(100);
      });
    });
    container.querySelectorAll(".solve-run-settings-btn").forEach((button) => {
      button.addEventListener("click", (event) => {
        event.stopPropagation();
        if (app.state.solve.solveRunSettingsPanelRunId === button.dataset.runId && app.state.solve.solveRunSettingsPanelEl) {
          app.commands.hideSolveRunSettingsPanel();
          return;
        }
        app.commands.showSolveRunSettingsPanel(button.dataset.runId, context, button.closest(".solve-run-card"));
      });
    });
    container.onscroll = () => app.commands.hideSolveRunHoverPreview();
  }

  Object.assign(app.commands, {
    buildSolveInspectorBlock,
    getSolveRunSummaryItems,
    buildSolveRunCardMetadataFooter,
    humanizeModuleName,
    getSolveSettingLabel,
    formatSolveSettingValue,
    getSolveRunSettingsSnapshot,
    getSolveRunModulesSnapshot,
    categorizeSolveSettingDiff,
    getSortedModuleParams,
    getSolveModuleParamLabel,
    collectPreprocessingSettingDiffs,
    collectSolveRunSettingDiffs,
    buildGroupedSolveSettingDiffBlocks,
    getSolveRunActiveModulesForSlot,
    buildSolveRunInspectorBlock,
    buildSolveRunEssentialsSummary,
    getFrozenSolveRunSnapshot,
    formatReadOnlyRunSetting,
    buildReadOnlyRunSectionRows,
    buildReadOnlyRunSettingsHtml,
    clearSolveRunHoverTimer,
    clearSolveRunHoverCloseTimer,
    isSolveRunHoverBlockedTarget,
    positionSolveRunHoverPreview,
    showSolveRunHoverPreview,
    scheduleSolveRunHoverPreview,
    scheduleHideSolveRunHoverPreview,
    hideSolveRunHoverPreview,
    positionSolveRunSettingsPanel,
    renderSolveRunSettingsPanel,
    showSolveRunSettingsPanel,
    hideSolveRunSettingsPanel,
    bindSolveRunCardAuxiliaryInteractions,
  });
}
