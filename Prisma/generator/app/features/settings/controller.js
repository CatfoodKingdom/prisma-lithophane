/**
 * Install the settings/controller feature commands.
 * @param {import("../../core/types.js").ApplicationContext} app
 */
export function installFeaturesSettingsController(app) {
  function closeSettingsDrawer() {
    const grid = app.state.ui.$(".settings-grid");
    const tabSettings = app.state.ui.$("#tabSettings");
    const drawer = app.state.ui.$("#settingsDrawer");

    // Return dynamic preprocessing cards to their canonical container before
    // removing responsive wrappers. This keeps rerenders from leaving detached
    // duplicate controls behind.
    app.commands.restoreSettingsFlowUnits(grid);

    // Remove responsive multi-column wrappers before returning the grid to its hidden host.
    grid.querySelectorAll(".settings-column").forEach(col => {
      while (col.firstChild) grid.appendChild(col.firstChild);
      col.remove();
    });
    grid.classList.remove("in-drawer");

    // Reparent settings grid back to its hidden host
    tabSettings.appendChild(grid);

    // Hide drawer — the settings drawer no longer owns #drawerOverlay (detail drawer does).
    drawer.setAttribute("aria-hidden", "true");
    app.state.settings.settingsDrawerOpen = false;
    app.events.emit("settings.closed", { source: "settings-drawer" });
  }

  function scheduleSettingsDrawerDistribution() {
    const distributeIfStillOpen = () => {
      const grid = app.state.ui.$(".settings-grid");
      if (
        !app.state.settings.settingsDrawerOpen
        || !grid?.classList.contains("in-drawer")
      ) {
        return;
      }
      app.commands.distributeSettingsColumns();
    };
    requestAnimationFrame(() => requestAnimationFrame(distributeIfStillOpen));
    // The drawer slides for 200ms, so repeat after its final width is measurable.
    window.setTimeout(distributeIfStillOpen, 240);
  }

  function loadSettingsAdvancedVisible() {
    try {
      const stored = app.persistence.read(app.state.settings.SETTINGS_ADVANCED_VISIBLE_STORAGE_KEY);
      if (stored === "true") return true;
      if (stored === "false") return false;
    } catch { /* ignore */ }
    return false;
  }

  function saveSettingsAdvancedVisible(visible) {
    try {
      app.persistence.write(
        app.state.settings.SETTINGS_ADVANCED_VISIBLE_STORAGE_KEY,
        visible ? "true" : "false",
      );
    } catch { /* ignore */ }
  }

  function updateAdvancedSettingsVisibility() {
    document.querySelectorAll(".settings-grid").forEach(grid => {
      grid.classList.toggle("show-advanced-settings", app.state.settings.settingsAdvancedVisible);
    });
    document.querySelectorAll(".settings-advanced-only").forEach(el => {
      el.classList.toggle("is-hidden", !app.state.settings.settingsAdvancedVisible);
      el.toggleAttribute("hidden", !app.state.settings.settingsAdvancedVisible);
    });
    const toggle = app.state.ui.$("#settingsAdvancedToggle");
    if (toggle) {
      toggle.classList.toggle("is-active", app.state.settings.settingsAdvancedVisible);
      toggle.setAttribute("aria-pressed", app.state.settings.settingsAdvancedVisible ? "true" : "false");
      toggle.textContent = `Advanced: ${app.state.settings.settingsAdvancedVisible ? "On" : "Off"}`;
    }
  }

  function openFilamentDetail(filamentId) {
    const fil = app.commands.filamentById(filamentId);
    if (!fil) return;

    const inManual = app.state.palette.manualSlots.includes(filamentId);
    const isCandidate = app.state.palette.candidateSelection.has(filamentId);
    const isEnabled = app.state.palette.enabledFilaments.has(filamentId);
    const isEligible = app.commands.isGenerationEligibleFilament(fil);

    const bodyHtml = `
      <div class="drawer-filament-header">
        <div class="drawer-filament-swatch" style="background:${fil.hex}"></div>
        <div>
          <div class="drawer-filament-name">${app.commands.esc(fil.display_name || fil.color_name)}</div>
          <div class="drawer-filament-meta">${app.commands.esc(fil.manufacturer)} &middot; ${app.commands.esc(fil.filament_id)}</div>
        </div>
      </div>

      <div class="drawer-section">
        <div class="drawer-section-title">Properties</div>
        <div class="drawer-info-row">
          <span class="drawer-info-label">Hex Color</span>
          <span class="drawer-info-value" style="display:flex;align-items:center;gap:6px">
            <span class="color-chip" style="background:${fil.hex}"></span> ${app.commands.esc(fil.hex)}
          </span>
        </div>
        <div class="drawer-info-row">
          <span class="drawer-info-label">Manufacturer</span>
          <span class="drawer-info-value">${app.commands.esc(fil.manufacturer)}</span>
        </div>
        <div class="drawer-info-row">
          <span class="drawer-info-label">Color Name</span>
          <span class="drawer-info-value">${app.commands.esc(fil.color_name)}</span>
        </div>
        <div class="drawer-info-row">
          <span class="drawer-info-label">Has Profile</span>
          <span class="drawer-info-value">${fil.has_profile ? '<span class="status-pill ok">Yes</span>' : '<span class="status-pill error">No</span>'}</span>
        </div>
        <div class="drawer-info-row">
          <span class="drawer-info-label">Available in Model</span>
          <span class="drawer-info-value">${isEligible ? '<span class="status-pill ok">Yes</span>' : '<span class="status-pill error">No</span>'}</span>
        </div>
        <div class="drawer-info-row">
          <span class="drawer-info-label">Enabled</span>
          <span class="drawer-info-value">${isEnabled ? '<span class="status-pill ok">Yes</span>' : '<span class="status-pill idle">No</span>'}</span>
        </div>
      </div>

      <div class="drawer-section">
        <div class="drawer-section-title">Actions</div>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          ${app.state.palette.creationMode === "manual" && !inManual && isEnabled && fil.has_profile ?
            `<button class="primary-button small" onclick="manualSlots.push('${filamentId}');renderCreationTab();openFilamentDetail('${filamentId}')">Add to Manual Palette</button>` :
            app.state.palette.creationMode === "manual" && inManual ?
            `<button class="ghost-button small" onclick="manualSlots=manualSlots.filter(id=>id!=='${filamentId}');renderCreationTab();openFilamentDetail('${filamentId}')">Remove from Manual Palette</button>` : ""}
          ${isEligible ?
            `<button class="ghost-button small" id="filamentAvailabilityActionBtn" type="button">${isEnabled ? "Disable" : "Enable"}</button>` : ""}
        </div>
      </div>
    `;

    app.commands.openDetailDrawer(app.commands.esc(fil.color_name), bodyHtml);
    app.state.ui.$("#filamentAvailabilityActionBtn")?.addEventListener("click", () => {
      app.commands.setFilamentEnabled(filamentId, !isEnabled, { reopenDetail: true });
    });
  }

  function bindAccordions() {
    app.state.ui.$$(".accordion-header").forEach((header) => {
      header.addEventListener("click", () => {
        const group = header.closest(".accordion-group");
        group.classList.toggle("is-open");
      });
    });
  }

  function updateAccordionSummaries() {
    const geom = app.state.ui.$("#accordionSummaryGeometry");
    if (geom) {
      const lh = parseFloat(app.state.ui.$("#cfgLayerHeight")?.value) || 0.08;
      const tmax = parseFloat(app.state.ui.$("#cfgTMax")?.value) || 2.5;
      geom.textContent = `${lh} mm layers, ${tmax} mm max`;
    }

    const solver = app.state.ui.$("#accordionSummarySolver");
    if (solver) {
      const kmax = parseInt(app.state.ui.$("#cfgKMax")?.value) || 3;
      solver.textContent = `k=${kmax}`;
    }

    const mesh = app.state.ui.$("#accordionSummaryMesh");
    if (mesh) {
      mesh.textContent = "post-solve export";
    }

    // Printer accordion removed — printer config is its own page now
  }

  function isWhiteCapEligibleFilament(filament) {
    return !!(
      filament
      && filament.has_profile
      && filament.white_cap_eligible === true
      && filament.exclude_from_model !== true
      && filament.generation_available !== false
    );
  }

  function filamentSelectLabel(filament) {
    return filament.display_name
      || [filament.manufacturer, filament.color_name].filter(Boolean).join(" ")
      || filament.filament_id;
  }

  function populateBaseCapDropdowns() {
    const whiteFils = app.state.session.allFilaments.filter(app.commands.isWhiteCapEligibleFilament);
    const baseEl = app.state.ui.$("#cfgBaseFilament");
    if (!baseEl) return;

    const currentBase = app.state.settings.config.base_filament || baseEl.value || app.state.session.DEFAULT_BASE_FILAMENT;

    const sorted = [...whiteFils].sort((a, b) =>
      (a.color_name || "").localeCompare(b.color_name || "") ||
      (a.manufacturer || "").localeCompare(b.manufacturer || "")
    );

    if (sorted.length === 0) {
      baseEl.innerHTML = `<option value="">No profiled white filaments</option>`;
      app.state.settings.config.base_filament = "";
      app.state.settings.config.cap_filament = "__same__";
      baseEl.value = "";
      return;
    }

    const eligibleIds = new Set(sorted.map(f => f.filament_id));
    const fallbackBase = eligibleIds.has(app.state.session.DEFAULT_BASE_FILAMENT)
      ? app.state.session.DEFAULT_BASE_FILAMENT
      : sorted[0].filament_id;
    const resolvedBase = eligibleIds.has(currentBase) ? currentBase : fallbackBase;
    const opts = sorted.map(f =>
      `<option value="${f.filament_id}">${app.commands.esc(app.commands.filamentSelectLabel(f))}</option>`
    ).join("");

    baseEl.innerHTML = opts;

    app.state.settings.config.base_filament = resolvedBase;
    app.state.settings.config.cap_filament = "__same__";
    baseEl.value = resolvedBase;
  }

  function updateSuggestSlotHint() {
    const totalSlots = app.state.session.printerConfig.ams_slots || 4;
    const bcSlots = app.commands.getBaseCapSlots();
    const ceiling = Math.max(1, totalSlots - bcSlots);
    // Sync max constraint but don't overwrite user's chosen value
    const input = app.state.ui.$("#targetFilamentCount");
    if (input) {
      input.max = ceiling;
      const current = parseInt(input.value);
      if (!current || current > ceiling) {
        input.value = ceiling;
      }
    }

  }

  function renderSettingsTab(options = {}) {
    const { preservePendingUi = false } = options;
    // Some callers are just refreshing the live settings view; let those absorb
    // any in-progress hardcoded-field edits before config -> DOM rendering.
    if (preservePendingUi) app.commands.readConfigFromUI();
    app.commands.syncConfigFromModuleState();
    app.commands.applyLuminanceMode(app.state.settings.config.luminance_mode || "standard");
    app.commands.applyMandatoryProductSettings();
    app.commands.populateBaseCapDropdowns();
    const baseEl = app.state.ui.$("#cfgBaseFilament");
    if (baseEl) baseEl.value = app.state.settings.config.base_filament || app.state.session.DEFAULT_BASE_FILAMENT;
    app.state.settings.config.cap_filament = "__same__";
    app.state.ui.$("#cfgLayerHeight").value = app.state.settings.config.layer_height;
    app.state.ui.$("#cfgDWb").value = app.state.settings.config.d_wb;
    app.state.ui.$("#cfgDWcMin").value = app.commands.minCapLayersFromThickness();
    app.state.ui.$("#cfgTMax").value = app.state.settings.config.t_max;
    const solvePitchVal = app.state.settings.config.image_sample_pitch_mm || 0.20;
    const solvePitchEl = app.state.ui.$("#cfgSolvePitch");
    if (solvePitchEl) solvePitchEl.value = solvePitchVal;
    // These elements may be dynamically rendered by modules — guard against null
    const _set = (sel, val) => { const el = app.state.ui.$(sel); if (el) el.value = val; };
    const _chk = (sel, val) => { const el = app.state.ui.$(sel); if (el) el.checked = val; };
    _set("#cfgSourceResampleKernel", app.state.settings.config.source_resample_kernel || "lanczos");
    _set("#cfgDetailCapMaxLayers", app.state.settings.config.detail_cap_max_layers ?? 5);
    _set("#cfgCellMode", app.state.settings.config.cell_mode || "felzenszwalb");
    _set("#cfgAppearanceModelProvider", app.state.settings.config.appearance_model_provider || "photo_stack_bundle");
    _set("#cfgStage1Coarsening", app.state.settings.config.stage1_coarsening_factor ?? 1);
    _set("#cfgColorRegionTarget", app.state.settings.config.color_region_target_mm ?? 0.60);
    _set("#cfgNeutralFieldProtection", app.state.settings.config.neutral_field_protection_mode || "off");
    _chk("#cfgStage2FineOverride", app.state.settings.config.stage2_fine_override_enabled !== false);
    _chk("#cfgStage2BoundaryMutation", app.state.settings.config.stage2_boundary_mutation_enabled);
    app.commands.setOptionalNumberInput("cfgStage2BoundaryMutationPercentile", app.state.settings.config.stage2_boundary_mutation_current_de_percentile);
    app.commands.setOptionalNumberInput("cfgStage2BoundaryMutationMaxPasses", app.state.settings.config.stage2_boundary_mutation_max_passes ?? 1);
    app.commands.setOptionalNumberInput("cfgStage2BoundaryMutationMinGain", app.state.settings.config.stage2_boundary_mutation_min_gain);
    app.commands.setOptionalNumberInput("cfgStage2BoundaryMutationMinComponent", app.state.settings.config.stage2_boundary_mutation_min_component_mm);
    _set("#cfgKMax", app.state.settings.config.k_max);
    _set("#cfgDeThreshold", app.state.settings.config.de_threshold);
    _set("#cfgSmoothKernel", app.commands.smoothingRadiusMmFromCells(app.state.settings.config.smooth_kernel));
    _chk("#cfgBorder", app.state.settings.config.border);
    _set("#cfgBorderWidth", app.state.settings.config.border_width_mm);
    _set("#cfgBorderHeight", app.state.settings.config.border_height_mm);
    _chk("#cfgUseCorrections", app.state.settings.config.use_corrections);
    const capModeEl = app.state.ui.$("#cfgCapMode");
    if (capModeEl) capModeEl.value = app.state.settings.config.cap_mode || "appearance_bounded_smooth";
    const capDeBudgetEl = app.state.ui.$("#cfgBoundaryCapDeBudget");
    if (capDeBudgetEl) capDeBudgetEl.value = app.state.settings.config.boundary_cap_de_budget ?? 0.004;
    app.commands.syncChromaWeightControlFromConfig();
    _set("#cfgGamutMode", app.commands.normalizeActiveGamutMode(app.state.settings.config.gamut_mode || "hull"));
    _chk("#cfgGamutWhiteRescale", app.state.settings.config.gamut_white_rescale);
    app.commands.setSolveModeControlValue(app.state.settings.config.luminance_mode || "standard");
    app.commands.syncBaseShadingLimitControls();
    app.commands.updateLuminanceModeFields();
    app.commands.updateCapModeFields();
    app.commands.updateStage4DetailFields();
    app.commands.updateBoundaryMutationFields();
    app.commands.syncDeckGenerationSettingsUI("settings");

    // Printer summary removed — info is in the left rail printer card

    app.commands.renderPresetBar();
    app.commands.updateBorderVisibility();
    app.commands.updateDerivedParams();
    app.commands.updateAccordionSummaries();
    app.commands.updateSuggestSlotHint();
    app.commands.updateAdvancedSettingsVisibility();
    app.commands.bindSettingsAutoSyncControls();
  }

  function updateSolveModeFields() {
    // Solver-specific settings are fully module-driven now.
  }

  function bindSettingsAutoSyncControls() {
    app.state.ui.$$(".settings-grid input, .settings-grid select").forEach((input) => {
      if (input.dataset.settingsAutosyncBound === "1") return;
      input.dataset.settingsAutosyncBound = "1";
      if (input.id === "cfgBaseShadingLimitSlider") {
        input.addEventListener("input", () => {
          app.commands.syncBaseShadingLimitControls(input.value);
        });
      }
      if (input.id === "cfgChromaWeight") {
        input.addEventListener("input", () => {
          app.commands.applyChromaWeightSliderInput(input.value);
          app.commands.checkPresetModified();
        });
      }
      input.addEventListener("change", () => {
        app.commands.updateSolveModeFields();
        app.commands.updateLuminanceModeFields();
        app.commands.updateCapModeFields();
        app.commands.updateBoundaryMutationFields();
        app.commands.updateStage4DetailFields();
        app.commands.readConfigFromUI();
        app.commands.updateBorderVisibility();
        app.commands.updateDerivedParams();
        app.commands.updateAccordionSummaries();
        app.commands.checkPresetModified();
        app.commands.syncDeckGenerationSettingsUI("settings");
        app.commands.syncConfigToServer();
      });
    });
  }

  function updateLuminanceModeFields() {
    const enabled = app.commands.normalizeLuminanceMode(app.commands.getSolveModeControlValue()) === "luminance_detail";
    const capMode = app.state.ui.$("#cfgCapMode");
    const configAlreadyLuminance = app.commands.normalizeLuminanceMode(app.state.settings.config.luminance_mode) === "luminance_detail";
    if (enabled) {
      if (capMode) {
        if (!app.state.settings.capModeForcedByLuminance && !configAlreadyLuminance) {
          app.commands.saveLastColorCapMode(capMode.value || app.state.settings.config.cap_mode || app.state.settings.lastColorCapMode);
        }
        capMode.value = "smooth_variable";
        app.state.settings.capModeForcedByLuminance = true;
      }
    } else {
      const restored = app.state.settings.lastColorCapMode || app.state.settings.config.cap_mode || "appearance_bounded_smooth";
      if (capMode && app.state.settings.capModeForcedByLuminance) {
        capMode.value = restored;
        app.state.settings.capModeForcedByLuminance = false;
      }
    }
    document.querySelectorAll(".luminance-mode-field").forEach(row => {
      row.classList.toggle("is-hidden", !enabled);
      row.classList.toggle("is-disabled", !enabled);
      row.querySelectorAll("input, select, button").forEach(inp => inp.disabled = !enabled);
    });
  }

  function updateCapModeFields() {
    const capModeEl = app.state.ui.$("#cfgCapMode");
    const luminanceMode = app.commands.normalizeLuminanceMode(app.commands.getSolveModeControlValue()) === "luminance_detail";
    const appearanceBoundOption = capModeEl?.querySelector('option[value="appearance_bounded_smooth"]');
    if (appearanceBoundOption) appearanceBoundOption.disabled = luminanceMode;
    if (
      luminanceMode
      && capModeEl?.value === "appearance_bounded_smooth"
    ) {
      app.commands.saveLastColorCapMode("appearance_bounded_smooth");
      capModeEl.value = "smooth_variable";
      app.state.settings.capModeForcedByLuminance = true;
    }
    const mode = capModeEl?.value || "appearance_bounded_smooth";
    const isAppearanceBounded = mode === "appearance_bounded_smooth";
    document.querySelectorAll(".cap-mode-field").forEach(row => {
      row.classList.toggle("is-hidden", luminanceMode);
      row.classList.toggle("is-disabled", luminanceMode);
      row.querySelectorAll("input, select").forEach(inp => inp.disabled = luminanceMode);
    });
    // Smooth-mode fields
    document.querySelectorAll(".cap-smooth-field").forEach(row => {
      row.classList.toggle("is-hidden", false);
      row.classList.toggle("is-disabled", false);
      row.querySelectorAll("input, select").forEach(inp => inp.disabled = false);
    });
    document.querySelectorAll(".cap-appearance-bound-field").forEach(row => {
      const visible = isAppearanceBounded && !luminanceMode;
      row.classList.toggle("is-hidden", !visible);
      row.classList.toggle("is-disabled", !visible);
      row.querySelectorAll("input, select").forEach(inp => inp.disabled = !visible);
    });
    document.querySelectorAll(".detail-section-head").forEach(row => {
      row.classList.toggle("is-hidden", false);
      row.classList.toggle("is-disabled", false);
    });
  }

  function updateBoundaryMutationFields() {
    const enabled = app.state.ui.$("#cfgStage2BoundaryMutation")?.checked || false;
    document.querySelectorAll(".boundary-mutation-field").forEach(row => {
      row.classList.toggle("is-hidden", !enabled);
      row.classList.toggle("is-disabled", !enabled);
      row.querySelectorAll("input, select").forEach(inp => inp.disabled = !enabled);
    });
  }

  function updateStage4DetailFields() {
    document.querySelectorAll(".detail-surface-field").forEach(row => {
      row.classList.toggle("is-hidden", false);
      row.classList.toggle("is-disabled", false);
      row.querySelectorAll("input, select").forEach(inp => inp.disabled = false);
    });
  }

  function readPrinterConfig() {
    // Printer config is now loaded from the server via loadPrinters().
    app.state.session.printerConfig.ams_slots = app.state.session.printerConfig.ams_units * app.state.session.printerConfig.slots_per_unit;
    app.state.session.printerConfig.white_slots = app.commands.getBaseCapSlots();
  }

  function _currentSettingsSnapshot() {
    app.commands.applyMandatoryProductSettings();
    app.commands.readConfigFromUI();
    const snap = {};
    for (const k of app.state.settings.SETTINGS_PROFILE_KEYS) snap[k] = app.commands._cloneValue(app.state.settings.config[k]);
    return snap;
  }

  Object.assign(app.commands, {
    closeSettingsDrawer,
    scheduleSettingsDrawerDistribution,
    loadSettingsAdvancedVisible,
    saveSettingsAdvancedVisible,
    updateAdvancedSettingsVisibility,
    openFilamentDetail,
    bindAccordions,
    updateAccordionSummaries,
    isWhiteCapEligibleFilament,
    filamentSelectLabel,
    populateBaseCapDropdowns,
    updateSuggestSlotHint,
    renderSettingsTab,
    updateSolveModeFields,
    bindSettingsAutoSyncControls,
    updateLuminanceModeFields,
    updateCapModeFields,
    updateBoundaryMutationFields,
    updateStage4DetailFields,
    readPrinterConfig,
    _currentSettingsSnapshot,
  });}
