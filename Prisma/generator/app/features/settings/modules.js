/**
 * Install the settings/modules feature commands.
 * @param {import("../../core/types.js").ApplicationContext} app
 */
export function installFeaturesSettingsModules(app) {
  function setModuleParamValue(moduleId, param, value) {
    const module = app.commands.moduleDescriptorById(moduleId);
    if (module?.slot === "preprocessing") {
      if (!app.state.settings.config.preprocessing_params || typeof app.state.settings.config.preprocessing_params !== "object") {
        app.state.settings.config.preprocessing_params = {};
      }
      if (!app.state.settings.config.preprocessing_params[moduleId] || typeof app.state.settings.config.preprocessing_params[moduleId] !== "object") {
        app.state.settings.config.preprocessing_params[moduleId] = {};
      }
      app.state.settings.config.preprocessing_params[moduleId][param.name] = value;
      return;
    }
    const storageKey = app.commands.moduleParamStorageKey(moduleId, param);
    app.state.settings.config[storageKey] = value;
  }

  function projectModuleConfigValues(moduleId, mod, configValues) {
    const projected = { ...(configValues || {}) };
    for (const param of Object.values(mod?.params || {})) {
      projected[param.name] = app.commands.getModuleParamValue(configValues, moduleId, param);
    }
    return projected;
  }

  function preprocessingPresetSpec(moduleId) {
    const descriptor = app.commands.moduleDescriptorById(moduleId);
    const presetUi = descriptor?.preset_ui;
    if (!presetUi) return null;
    return {
      controlLabel: presetUi.control_label,
      defaultPreset: presetUi.default_preset,
      presets: presetUi.presets || [],
    };
  }

  function preprocessingParamBlock(moduleId, configValues = app.state.settings.config) {
    const block = configValues?.preprocessing_params?.[moduleId];
    return block && typeof block === "object" ? block : null;
  }

  function preprocessingParamBlockHasValues(moduleId, configValues = app.state.settings.config) {
    const block = app.commands.preprocessingParamBlock(moduleId, configValues);
    return !!block && Object.keys(block).length > 0;
  }

  function valuesMatchPresetValue(actual, expected) {
    if (typeof expected === "number") {
      const numeric = Number(actual);
      return Number.isFinite(numeric) && Math.abs(numeric - expected) < 1e-9;
    }
    if (typeof expected === "boolean") return Boolean(actual) === expected;
    return String(actual) === String(expected);
  }

  function preprocessingPresetValuesMatch(moduleId, values, preset) {
    if (!preset?.values) return false;
    return Object.entries(preset.values).every(([key, expected]) => (
      app.commands.valuesMatchPresetValue(values?.[key], expected)
    ));
  }

  function currentPreprocessingPresetKey(moduleId, configValues = app.state.settings.config) {
    const spec = app.commands.preprocessingPresetSpec(moduleId);
    if (!spec) return null;
    if (!app.state.settings.moduleState[moduleId]) return "off";
    const mod = app.commands.moduleDescriptorById(moduleId);
    const values = app.commands.projectModuleConfigValues(moduleId, mod, configValues);
    const match = (spec.presets || []).find((preset) => (
      preset.values && app.commands.preprocessingPresetValuesMatch(moduleId, values, preset)
    ));
    return match?.key || "custom";
  }

  function applyPreprocessingPresetValues(moduleId, presetKey) {
    const spec = app.commands.preprocessingPresetSpec(moduleId);
    const mod = app.commands.moduleDescriptorById(moduleId);
    if (!spec || !mod) return false;
    const preset = (spec.presets || []).find((entry) => entry.key === presetKey);
    if (!preset?.values) return false;
    const paramsByName = mod.params || {};
    for (const [paramName, value] of Object.entries(preset.values)) {
      const param = paramsByName[paramName];
      if (!param) continue;
      app.commands.setModuleParamValue(moduleId, param, value);
    }
    return true;
  }

  function applyDefaultPreprocessingPresetIfNeeded(moduleId) {
    const spec = app.commands.preprocessingPresetSpec(moduleId);
    if (!spec || app.commands.preprocessingParamBlockHasValues(moduleId)) return false;
    return app.commands.applyPreprocessingPresetValues(moduleId, spec.defaultPreset);
  }

  function refreshPreprocessingPresetSelect(moduleId) {
    const sel = document.getElementById(`mod_${moduleId}_preset`);
    if (!sel) return;
    const key = app.commands.currentPreprocessingPresetKey(moduleId);
    if (key && sel.value !== key) sel.value = key;
  }

  function syncConfigFromModuleState() {
    // Module state is authoritative and no longer mirrored into config.
  }

  function refreshModuleDrivenViews(moduleId = null) {
    if (moduleId) app.commands.applyShowWhenRules(moduleId);
    if (moduleId) app.commands.refreshPreprocessingPresetSelect(moduleId);
    app.commands.updateDerivedParams();
    app.commands.updateAccordionSummaries();
    app.commands.checkPresetModified();
  }

  function coerceNumberValue(rawValue, fallback, {
    parse = parseFloat,
    min = null,
    max = null,
    integer = false,
  } = {}) {
    const parsed = parse(rawValue);
    if (!Number.isFinite(parsed)) {
      return { ok: false, value: fallback };
    }
    let value = integer ? Math.trunc(parsed) : parsed;
    if (min != null && value < Number(min)) value = Number(min);
    if (max != null && value > Number(max)) value = Number(max);
    return { ok: true, value };
  }

  function coerceNumericParamValue(param, rawValue, fallback) {
    return app.commands.coerceNumberValue(rawValue, fallback, {
      parse: param.type === "int" ? (value) => parseInt(value, 10) : parseFloat,
      min: param.min,
      max: param.max,
      integer: param.type === "int",
    });
  }

  function renderParamRow(moduleId, param, configValues) {
    const tr = document.createElement("tr");
    const inputId = `mod_${moduleId}_${param.name}`;
    const presetSpec = app.commands.preprocessingPresetSpec(moduleId);
    const tooltip = param.tooltip || param.description || "";
    const currentValue = configValues[param.name] ?? param.default;
    if (presetSpec) {
      tr.classList.add(
        "advanced-setting",
        "module-advanced-param-row",
        "settings-child-row",
      );
    }

    // Label cell
    const tdLabel = document.createElement("td");
    tdLabel.title = tooltip;
    tdLabel.textContent = param.label;

    // Value cell
    const tdValue = document.createElement("td");

    if (param.type === "bool") {
      const label = document.createElement("label");
      label.className = "stg-check";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.id = inputId;
      cb.checked = !!currentValue;
      cb.addEventListener("change", () => {
        app.commands.setModuleParamValue(moduleId, param, cb.checked);
        app.commands.syncConfigToServer();
        app.commands.refreshModuleDrivenViews(moduleId);
      });
      label.appendChild(cb);
      label.appendChild(document.createTextNode(" Enabled"));
      tdValue.appendChild(label);

    } else if (param.type === "choice") {
      const sel = document.createElement("select");
      sel.className = "stg-select";
      sel.id = inputId;
      (param.choices || []).forEach(c => {
        const opt = document.createElement("option");
        opt.value = c;
        const choiceLabels = param.choice_labels;
        opt.textContent = (choiceLabels && Object.prototype.hasOwnProperty.call(choiceLabels, c))
          ? choiceLabels[c]
          : c;
        if (c === currentValue) opt.selected = true;
        sel.appendChild(opt);
      });
      sel.addEventListener("change", () => {
        app.commands.setModuleParamValue(moduleId, param, sel.value);
        app.commands.syncConfigToServer();
        app.commands.refreshModuleDrivenViews(moduleId);
      });
      tdValue.appendChild(sel);

    } else if (param.type === "html") {
      tdLabel.remove();
      tr.appendChild(tdValue);
      tdValue.colSpan = 2;
      tdValue.innerHTML = param.default;
      return tr;

    } else if (param.type === "computed_text") {
      tdLabel.remove();
      const div = document.createElement("div");
      div.className = "stg-footnote";
      div.id = inputId;
      div.dataset.template = param.default;
      app.commands.updateComputedText(div, configValues);
      tdValue.colSpan = 2;
      tdValue.appendChild(div);
      tr.appendChild(tdValue);
      return tr;

    } else {
      // int or float — text input with unit
      const wrapper = document.createElement("div");
      wrapper.className = "input-with-unit stg-iwu";
      const inp = document.createElement("input");
      // All module parameters use text inputs. Integer parameters still get
      // the custom quantized controls below; keeping them out of native
      // number inputs prevents a second browser-specific wheel implementation
      // from competing with the custom one.
      inp.type = "text";
      inp.className = "unit-input";
      inp.id = inputId;
      inp.value = app.commands.formatSettingsInputNumber
        ? app.commands.formatSettingsInputNumber(currentValue)
        : currentValue;
      inp.inputMode = param.type === "int" ? "numeric" : "decimal";
      inp.step = param.type === "int" ? "1" : "any";
      if (param.type === "int") {
        inp.dataset.settingsQuantized = "1";
        inp.dataset.settingsInteger = "1";
        if (param.default != null) inp.dataset.settingsDefaultValue = String(param.default);
      }
      if (param.min != null) inp.min = param.min;
      if (param.max != null) inp.max = param.max;
      inp.addEventListener("change", () => {
        const fallback = app.commands.getModuleParamValue(app.state.settings.config, moduleId, param);
        const coerced = app.commands.coerceNumericParamValue(param, inp.value, fallback);
        inp.value = app.commands.formatSettingsInputNumber
          ? app.commands.formatSettingsInputNumber(coerced.value)
          : coerced.value;
        if (coerced.ok) {
          app.commands.setModuleParamValue(moduleId, param, coerced.value);
          app.commands.syncConfigToServer();
          app.commands.refreshModuleDrivenViews(moduleId);
        }
      });
      inp.addEventListener("input", () => {
        const coerced = app.commands.coerceNumericParamValue(param, inp.value, null);
        if (coerced.ok) {
          app.commands.setModuleParamValue(moduleId, param, coerced.value);
          app.commands.refreshPreprocessingPresetSelect(moduleId);
        }
      });
      wrapper.appendChild(inp);
      if (param.unit) {
        const suffix = document.createElement("span");
        suffix.className = "unit-suffix";
        suffix.textContent = param.unit;
        wrapper.appendChild(suffix);
      }
      tdValue.appendChild(wrapper);
    }

    tr.appendChild(tdLabel);
    tr.appendChild(tdValue);

    tr.dataset.moduleId = moduleId;

    // Store show_when for later
    if (param.show_when) {
      tr.dataset.showWhen = JSON.stringify(param.show_when);
    }

    return tr;
  }

  function renderPreprocessingPresetRow(mod) {
    const spec = app.commands.preprocessingPresetSpec(mod?.name);
    if (!spec) return null;

    const tr = document.createElement("tr");
    tr.className = "module-preset-row";
    tr.dataset.moduleId = mod.name;

    const tdLabel = document.createElement("td");
    tdLabel.textContent = spec.controlLabel || "Preset";
    tdLabel.title = app.commands.moduleDisplayTooltip(mod);

    const tdValue = document.createElement("td");
    const sel = document.createElement("select");
    sel.className = "stg-select module-preset-select";
    sel.id = `mod_${mod.name}_preset`;

    const currentKey = app.commands.currentPreprocessingPresetKey(mod.name);
    (spec.presets || []).forEach((preset) => {
      const opt = document.createElement("option");
      opt.value = preset.key;
      opt.textContent = preset.label;
      if (preset.custom && currentKey !== "custom") opt.disabled = true;
      if (preset.key === currentKey) opt.selected = true;
      sel.appendChild(opt);
    });

    sel.addEventListener("change", async () => {
      const key = sel.value;
      if (key === "custom") {
        app.commands.refreshPreprocessingPresetSelect(mod.name);
        return;
      }
      if (key === "off") {
        app.state.settings.moduleState[mod.name] = false;
        const ticket = app.commands.beginSettingsEvaluationRequest();
        const response = await app.api.toggleModule(mod.name, false);
        app.commands.applySettingsEvaluationResponse(response, ticket);
        app.commands.syncConfigFromModuleState();
        app.commands.syncConfigToServer();
        app.commands.renderModulePanel();
        app.commands.renderDynamicSettings();
        app.commands.refreshModuleDrivenViews();
        return;
      }
      app.commands.applyPreprocessingPresetValues(mod.name, key);
      app.commands.syncConfigToServer();
      app.commands.renderDynamicSettings();
      app.commands.refreshModuleDrivenViews(mod.name);
    });

    tdValue.appendChild(sel);
    tr.appendChild(tdLabel);
    tr.appendChild(tdValue);
    return tr;
  }

  function updateComputedText(el, configValues) {
    const template = el.dataset.template || "";
    el.textContent = template.replace(/\{(\w+)\}/g, (_, key) => {
      return configValues[key] ?? key;
    });
  }

  function applyShowWhenRules(moduleId) {
    const rows = document.querySelectorAll(`tr[data-module-id="${moduleId}"]`);
    rows.forEach(tr => {
      const rule = JSON.parse(tr.dataset.showWhen || "{}");
      const visible = app.commands.showWhenRuleMatches(rule, param => {
        const input = document.getElementById(`mod_${moduleId}_${param}`);
        if (!input) return undefined;
        return input.type === "checkbox" ? input.checked :
               input.tagName === "SELECT" ? input.value :
               input.value;
      });
      tr.classList.toggle("is-hidden", !visible);
    });
  }

  function renderModuleSection(mod, configValues) {
    // Sort params by order
    const params = Object.values(mod.params).sort((a, b) => (a.order || 0) - (b.order || 0));
    const moduleConfigValues = app.commands.projectModuleConfigValues(mod.name, mod, configValues);

    // Modules with zero configurable params would render as an empty header bar.
    // Skip them — their presence is already shown in the module toggle panel.
    if (params.length === 0) return null;

    const section = document.createElement("div");
    section.className = "module-settings-section";
    section.dataset.moduleId = mod.name;

    // Module heading
    const h4 = document.createElement("h4");
    h4.className = "settings-subsection-head";
    h4.textContent = app.commands.moduleDisplayName(mod);
    h4.title = app.commands.moduleDisplayTooltip(mod);
    section.appendChild(h4);

    const presetRow = app.commands.renderPreprocessingPresetRow(mod);
    if (presetRow) {
      const presetTable = document.createElement("table");
      presetTable.className = "settings-table settings-subsection-table module-preset-table";
      presetTable.appendChild(presetRow);
      section.appendChild(presetTable);
    }

    // Group params
    let currentGroup = null;
    let table = null;

    for (const param of params) {
      const group = param.group || "";
      if (group !== currentGroup || !table) {
        table = document.createElement("table");
        table.className = "settings-table settings-subsection-table";
        section.appendChild(table);
        currentGroup = group;
      }
      table.appendChild(app.commands.renderParamRow(mod.name, param, moduleConfigValues));
    }

    return section;
  }

  async function loadModules() {
    try {
      const data = await app.api.fetchModules();
      app.state.settings.moduleData = data.modules || [];
      app.state.settings.moduleState = {};
      app.state.settings.moduleData.forEach(m => { app.state.settings.moduleState[m.name] = m.enabled; });
      const ticket = app.commands.beginSettingsEvaluationRequest();
      app.commands.applySettingsEvaluationResponse(data, ticket);
      app.commands.syncConfigFromModuleState();
      app.commands.renderModulePanel();
      app.commands.renderDynamicSettings();
      app.commands.refreshModuleDrivenViews();
    } catch (err) {
      console.warn("[modules] load failed:", err.message);
    }
  }

  function getModulePosture(mod) {
    return app.state.ui.MODULE_POSTURE[mod?.name] || null;
  }

  function renderModulePanel() {
    const container = document.getElementById("moduleToggles");
    if (!container) return;
    container.innerHTML = "";

    const group = app.commands.renderModuleToggleGroup("preprocessing", app.commands.modulesForSlot("preprocessing"), {
      showSlotLabel: false,
    });
    if (group) container.appendChild(group);
  }

  function renderModuleToggleGroup(slot, modules, { showSlotLabel = true } = {}) {
    if (!modules || modules.length === 0) return null;

    const table = document.createElement("table");
    table.className = "settings-table module-toggle-table";

    if (showSlotLabel) {
      table.setAttribute("aria-label", slot === "preprocessing"
        ? "Pre-processing"
        : slot.charAt(0).toUpperCase() + slot.slice(1));
    }

    modules
      .slice()
      .sort((a, b) => {
        const postureA = app.commands.getModulePosture(a);
        const postureB = app.commands.getModulePosture(b);
        const orderA = postureA?.order ?? 99;
        const orderB = postureB?.order ?? 99;
        if (orderA !== orderB) return orderA - orderB;
        return a.name.localeCompare(b.name);
      })
      .forEach(m => {
        const row = document.createElement("tr");
        row.className = "module-toggle-row";
        row.dataset.guideModuleToggle = m.name;
        const posture = app.commands.getModulePosture(m);

        const tdLabel = document.createElement("td");
        tdLabel.title = app.commands.moduleDisplayTooltip(m);

        const text = document.createElement("span");
        text.className = "module-toggle-label";
        text.textContent = app.commands.moduleDisplayName(m);
        tdLabel.appendChild(text);

        if (posture?.label) {
          const badge = document.createElement("span");
          badge.className = `module-posture-badge is-${posture.tone}`;
          badge.textContent = posture.label;
          tdLabel.appendChild(badge);
        }

        const tdValue = document.createElement("td");
        const enabledLabel = document.createElement("label");
        enabledLabel.className = "stg-check";

        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.checked = !!app.state.settings.moduleState[m.name];
        cb.addEventListener("change", async () => {
          app.state.settings.moduleState[m.name] = cb.checked;
          if (cb.checked && m.slot === "preprocessing") {
            app.commands.applyDefaultPreprocessingPresetIfNeeded(m.name);
          }
          const ticket = app.commands.beginSettingsEvaluationRequest();
          const response = await app.api.toggleModule(m.name, cb.checked);
          app.commands.applySettingsEvaluationResponse(response, ticket);
          app.commands.syncConfigFromModuleState();
          app.commands.syncConfigToServer();
          app.commands.renderModulePanel();
          app.commands.renderDynamicSettings();
          app.commands.refreshModuleDrivenViews();
        });
        enabledLabel.appendChild(cb);
        enabledLabel.appendChild(document.createTextNode(" Enabled"));
        tdValue.appendChild(enabledLabel);
        row.appendChild(tdLabel);
        row.appendChild(tdValue);
        table.appendChild(row);
      });

    return table;
  }

  function renderDynamicSettings() {
    app.commands.restoreSettingsFlowUnits(document.querySelector(".settings-grid"));
    const slotContainers = {
      preprocessing: document.getElementById("preprocessingSettingsContainer"),
    };
    Object.values(slotContainers).forEach(c => {
      if (c) c.innerHTML = "";
    });

    app.state.settings.moduleData
      .filter(m => app.state.settings.moduleState[m.name] && app.state.ui.MODULE_UI_VISIBLE_SLOTS.has(m.slot))
      .forEach((m, index) => {
        const target = slotContainers[m.slot];
        if (!target) return;
        const section = app.commands.renderModuleSection(m, app.state.settings.config);
        if (!section) return;
        section.dataset.settingsFlowOrder = String(index);
        target.appendChild(section);
        app.commands.applyShowWhenRules(m.name);
      });

    // Re-distribute columns since dynamic sections changed
    app.commands.distributeSettingsColumns();
    app.commands.enhanceSettingsNumericInputs?.();
    app.commands.annotateSettingsRows?.();
    app.commands.renderSettingsEvaluation?.();
  }











  Object.assign(app.commands, {
    setModuleParamValue,
    projectModuleConfigValues,
    preprocessingPresetSpec,
    preprocessingParamBlock,
    preprocessingParamBlockHasValues,
    valuesMatchPresetValue,
    preprocessingPresetValuesMatch,
    currentPreprocessingPresetKey,
    applyPreprocessingPresetValues,
    applyDefaultPreprocessingPresetIfNeeded,
    refreshPreprocessingPresetSelect,
    syncConfigFromModuleState,
    refreshModuleDrivenViews,
    coerceNumberValue,
    coerceNumericParamValue,
    renderParamRow,
    renderPreprocessingPresetRow,
    updateComputedText,
    applyShowWhenRules,
    renderModuleSection,
    loadModules,
    getModulePosture,
    renderModulePanel,
    renderModuleToggleGroup,
    renderDynamicSettings,
  });}
