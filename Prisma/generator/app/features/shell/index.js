import { assertPolledJobIdentity } from "../../core/polling.js";

/**
 * Install the shell/index feature commands.
 * @param {import("../../core/types.js").ApplicationContext} app
 */
export function installFeaturesShellIndex(app) {
  function getBaseFilament() {
    const sel = app.state.ui.$("#cfgBaseFilament");
    return sel ? sel.value : app.state.session.DEFAULT_BASE_FILAMENT;
  }

  function getBaseCapIds() {
    const s = new Set();
    s.add(app.commands.getBaseFilament());
    return s;
  }

  function getBaseCapSlots() {
    return 1;
  }

  function xIconSvg(className = "icon-x") {
    return `<svg class="${className}" viewBox="0 0 12 12" aria-hidden="true" focusable="false"><path d="M2 2l8 8M10 2L2 10"></path></svg>`;
  }

  function panelResizeIconSvg(expanded) {
    return expanded
      ? `
        <svg class="panel-resize-icon" viewBox="0 0 14 14" aria-hidden="true" focusable="false">
          <path d="M3 5.25H8.75V11H3Z"></path>
          <path d="M5.25 3H11V8.75H9.25"></path>
        </svg>
      `
      : `
        <svg class="panel-resize-icon" viewBox="0 0 14 14" aria-hidden="true" focusable="false">
          <path d="M3 3H11V11H3Z"></path>
        </svg>
      `;
  }

  function getActivePalette() {
    const card = app.state.palette.deck.find(d => d.id === app.state.palette.activeDeckId);
    return card ? card.filament_ids : [];
  }

  function normalizeColorCapModeForStorage(mode) {
    return mode === "smooth_variable" ? "smooth_variable" : "appearance_bounded_smooth";
  }

  function loadLastColorCapMode(fallback = "appearance_bounded_smooth") {
    try {
      const stored = app.persistence.read(app.state.settings.COLOR_CAP_MODE_STORAGE_KEY);
      if (stored === "appearance_bounded_smooth" || stored === "smooth_variable") {
        return stored;
      }
    } catch { /* ignore */ }
    return app.commands.normalizeColorCapModeForStorage(fallback);
  }

  function saveLastColorCapMode(mode) {
    app.state.settings.lastColorCapMode = app.commands.normalizeColorCapModeForStorage(mode);
    try {
      app.persistence.write(app.state.settings.COLOR_CAP_MODE_STORAGE_KEY, app.state.settings.lastColorCapMode);
    } catch { /* ignore */ }
    return app.state.settings.lastColorCapMode;
  }

  function applyMandatoryProductSettings() {
    app.state.settings.config.model_domain_ingress = true;
    app.state.settings.config.detail_cap_enabled = true;
    app.state.settings.config.enforce_printability = true;
    app.state.settings.config.cap_continuity_cleanup = true;
    app.state.settings.config.color_region_target_from_printability = true;
    app.state.settings.config.stage2_final_printability_gate_fine_override = true;
    app.state.settings.config.stage2_printability_gate_fine_override = true;
    app.state.settings.config.stage2_printability_repair_fine_override = true;
    app.state.settings.config.stage4_printability_gate_detail = true;
  }

  function formatRegionPlanningScale(value = app.state.settings.config.stage1_coarsening_factor || 1) {
    const factor = Math.max(1, parseInt(value, 10) || 1);
    return factor === 1 ? "1x (full detail)" : `${factor}x (coarser regions)`;
  }

  function formatRegionMethod(mode = app.state.settings.config.cell_mode || "felzenszwalb") {
    switch (String(mode || "felzenszwalb").toLowerCase()) {
      case "grid": return "fixed grid";
      case "slic": return "superpixels";
      case "felzenszwalb":
      default:
        return "image regions";
    }
  }

  function getCurrentLayerHeight() {
    const domValue = parseFloat(app.state.ui.$("#cfgLayerHeight")?.value);
    if (Number.isFinite(domValue) && domValue > 0) return domValue;
    const configValue = parseFloat(app.state.settings.config.layer_height);
    return Number.isFinite(configValue) && configValue > 0 ? configValue : 0.08;
  }

  function minCapLayersFromThickness(thicknessMm = app.state.settings.config.d_wc_min, layerHeight = app.commands.getCurrentLayerHeight()) {
    const lh = Math.max(Number(layerHeight) || 0.08, 1e-9);
    const thickness = Math.max(Number(thicknessMm) || lh, lh);
    return Math.max(1, Math.ceil(thickness / lh - 1e-9));
  }

  function minCapThicknessFromLayers(layerCount, layerHeight = app.commands.getCurrentLayerHeight()) {
    const layers = Math.max(1, Math.trunc(Number(layerCount) || 1));
    const lh = Math.max(Number(layerHeight) || 0.08, 1e-9);
    return Math.round(layers * lh * 1e6) / 1e6;
  }

  function smoothingRadiusMmFromCells(cells = app.state.settings.config.smooth_kernel, solvePitch = app.commands.getCurrentSolvePitch()) {
    const cellCount = Math.max(0, Number(cells) || 0);
    const pitch = Math.max(Number(solvePitch) || 0.20, 1e-9);
    return Math.round(cellCount * pitch * 1e6) / 1e6;
  }

  function smoothingCellsFromRadiusMm(radiusMm, solvePitch = app.commands.getCurrentSolvePitch()) {
    const radius = Math.max(0, Number(radiusMm) || 0);
    const pitch = Math.max(Number(solvePitch) || 0.20, 1e-9);
    return radius / pitch;
  }

  function normalizeLuminanceMode(mode) {
    const raw = String(mode || "standard").trim().toLowerCase();
    if (["luminance", "luminance-detail", "luminance_detail", "detail"].includes(raw)) {
      return "luminance_detail";
    }
    return "standard";
  }

  function clampLuminanceBaseShadingLimitFraction(value) {
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) return 0.75;
    return Math.max(0.0, Math.min(1.0, parsed));
  }

  function getLuminanceBaseShadingLimitFraction() {
    const current = app.state.settings.config.luminance_base_shading_limit_fraction;
    const legacy = app.state.settings.config.luminance_handler_optical_authority_fraction;
    const currentParsed = Number(current);
    const legacyParsed = Number(legacy);
    if (
      Number.isFinite(legacyParsed)
      && (!Number.isFinite(currentParsed) || (currentParsed === 0.75 && legacyParsed !== 0.75))
    ) {
      return app.commands.clampLuminanceBaseShadingLimitFraction(legacyParsed);
    }
    return app.commands.clampLuminanceBaseShadingLimitFraction(
      Number.isFinite(currentParsed) ? currentParsed : 0.75,
    );
  }

  function setLuminanceBaseShadingLimitFraction(value) {
    const clamped = app.commands.clampLuminanceBaseShadingLimitFraction(value);
    app.state.settings.config.luminance_base_shading_limit_fraction = clamped;
    app.state.settings.config.luminance_handler_optical_authority_fraction = clamped;
    return clamped;
  }

  function formatLuminanceBaseShadingLimitPercent(value) {
    return String(Math.round(app.commands.clampLuminanceBaseShadingLimitFraction(value) * 100));
  }

  function parseLuminanceBaseShadingLimitPercent(value) {
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) return 0.75;
    return app.commands.clampLuminanceBaseShadingLimitFraction(parsed / 100);
  }

  function getBaseShadingLimitInput() {
    return app.state.ui.$("#cfgBaseShadingLimit");
  }

  function getBaseShadingLimitSlider() {
    return app.state.ui.$("#cfgBaseShadingLimitSlider");
  }

  function syncBaseShadingLimitControls(percentValue = null) {
    const percent = percentValue == null
      ? app.commands.formatLuminanceBaseShadingLimitPercent(app.commands.getLuminanceBaseShadingLimitFraction())
      : String(Math.max(0, Math.min(100, Math.round(Number(percentValue) || 0))));
    const input = app.commands.getBaseShadingLimitInput();
    if (input) input.value = percent;
    const slider = app.commands.getBaseShadingLimitSlider();
    if (slider) slider.value = percent;
  }

  function applyLuminanceMode(mode, options = {}) {
    const { resetStandard = false } = options;
    const normalized = app.commands.normalizeLuminanceMode(mode);
    app.state.settings.config.luminance_mode = normalized;
    if (normalized === "luminance_detail") {
      app.state.settings.config.luminance_handler_enabled = true;
      app.state.settings.config.luminance_handler_mode = "boundary_ceiling";
      app.state.settings.config.luminance_handler_strength = 1.0;
      app.commands.setLuminanceBaseShadingLimitFraction(app.commands.getLuminanceBaseShadingLimitFraction());
      app.state.settings.config.luminance_handler_boundary_percentile = 95.0;
      app.state.settings.config.luminance_handler_boundary_sigma_px = null;
      app.state.settings.config.luminance_handler_response_curve = "linear";
      app.state.settings.config.luminance_handler_response_gamma = 1.0;
      app.state.settings.config.luminance_handler_detail_residual = true;
      app.state.settings.config.luminance_handler_include_solver_detail = true;
      app.state.settings.config.detail_cap_enabled = true;
      app.state.settings.config.luminance_detail_authoring_printability = "absolute_finalgate";
      app.state.settings.config.enforce_printability = true;
      app.state.settings.config.emit_blueprint_printability = true;
    } else if (resetStandard) {
      app.state.settings.config.luminance_handler_enabled = false;
      app.state.settings.config.luminance_detail_authoring_printability = "off";
    }
    app.commands.applyMandatoryProductSettings();
    return normalized;
  }

  function normalizeActiveGamutMode(mode = "hull") {
    const normalized = String(mode || "hull").trim().toLowerCase();
    return normalized === "chroma" ? "hue_preserving" : normalized;
  }

  function normalizeChromaWeight(weight) {
    const numeric = Number(weight);
    return Number.isFinite(numeric) && numeric > 0 ? numeric : 1.0;
  }

  function chromaWeightToSliderPosition(weight) {
    return Math.log2(app.commands.normalizeChromaWeight(weight));
  }

  function chromaWeightFromSliderPosition(position) {
    const numeric = Number(position);
    return Math.pow(2, Number.isFinite(numeric) ? numeric : 0);
  }

  function formatChromaWeightReadout(weight) {
    return app.commands.normalizeChromaWeight(weight).toFixed(2);
  }

  function syncChromaWeightControlFromConfig() {
    const rawWeight = app.commands.normalizeChromaWeight(app.state.settings.config.chroma_weight ?? 1.0);
    const slider = app.state.ui.$("#cfgChromaWeight");
    if (slider) {
      const position = app.commands.chromaWeightToSliderPosition(rawWeight);
      slider.value = String(Math.max(
        app.state.ui.CHROMA_WEIGHT_SLIDER_MIN,
        Math.min(app.state.ui.CHROMA_WEIGHT_SLIDER_MAX, position)
      ));
    }
    const readout = app.state.ui.$("#cfgChromaWeightReadout");
    if (readout) readout.textContent = app.commands.formatChromaWeightReadout(rawWeight);
  }

  function applyChromaWeightSliderInput(position) {
    app.state.settings.config.chroma_weight = app.commands.chromaWeightFromSliderPosition(position);
    app.commands.syncChromaWeightControlFromConfig();
    return app.state.settings.config.chroma_weight;
  }

  function getSolveModeControlValue() {
    const selected = document.querySelector("#cfgLuminanceMode .segmented-btn.is-active");
    return selected?.dataset.value || app.state.settings.config.luminance_mode || "standard";
  }

  function setSolveModeControlValue(mode) {
    const normalized = app.commands.normalizeLuminanceMode(mode);
    document.querySelectorAll("#cfgLuminanceMode .segmented-btn").forEach(btn => {
      const active = app.commands.normalizeLuminanceMode(btn.dataset.value) === normalized;
      btn.classList.toggle("is-active", active);
      btn.setAttribute("aria-checked", active ? "true" : "false");
    });
  }

  function buildSolveRecipeContext(palette, settingsSnapshot = null) {
    const baseProfile = app.state.settings.loadedProfileRef || {
      id: app.state.ui.SYSTEM_SETTINGS_PROFILE_ID,
      kind: "system",
      name: app.state.ui.SYSTEM_SETTINGS_PROFILE_NAME,
    };
    const modified = app.commands.isSettingsProfileModified();
    const settings = app.commands._cloneValue(settingsSnapshot || app.commands._currentSettingsSnapshot());
    const modules = app.commands._currentSettingsProfileModulesSnapshot();
    const profileRef = modified
      ? {
          kind: "transient",
          source_kind: baseProfile.kind || "system",
          source_id: baseProfile.id || app.state.ui.SYSTEM_SETTINGS_PROFILE_ID,
          source_name: baseProfile.name || app.state.ui.SYSTEM_SETTINGS_PROFILE_NAME,
          name: baseProfile.name || app.state.ui.SYSTEM_SETTINGS_PROFILE_NAME,
        }
      : {
          kind: baseProfile.kind || "system",
          id: baseProfile.id || app.state.ui.SYSTEM_SETTINGS_PROFILE_ID,
          name: baseProfile.name || app.state.ui.SYSTEM_SETTINGS_PROFILE_NAME,
        };
    return {
      profile_ref: profileRef,
      profile_name_at_solve: baseProfile.name || app.state.ui.SYSTEM_SETTINGS_PROFILE_NAME,
      is_profile_modified_at_solve: modified,
      recipe_snapshot: {
        palette: [...palette],
        profile_ref: app.commands._cloneValue(profileRef),
        profile_snapshot: {
          name: baseProfile.name || app.state.ui.SYSTEM_SETTINGS_PROFILE_NAME,
          settings,
          modules,
        },
      },
    };
  }

  function describeSolveRunProfile(run) {
    const profileRef = run?.profile_ref || {};
    const name = run?.recipe_snapshot?.profile_snapshot?.name
      || run?.profile_name_at_solve
      || profileRef.name
      || app.state.ui.SYSTEM_SETTINGS_PROFILE_NAME;
    if (run?.is_profile_modified_at_solve) {
      const sourceName = profileRef.source_name || name;
      return {
        name,
        badgeClass: "settings-profile-mini-badge is-warn",
        badgeLabel: "modified draft",
        meta: `Draft based on ${sourceName}`,
      };
    }
    if ((profileRef.kind || "").toLowerCase() === "system" || profileRef.id === app.state.ui.SYSTEM_SETTINGS_PROFILE_ID) {
      return {
        name,
        badgeClass: "settings-profile-mini-badge",
        badgeLabel: "system",
        meta: "System default profile",
      };
    }
    return {
      name,
      badgeClass: "settings-profile-mini-badge is-accent",
      badgeLabel: "saved",
      meta: "Named Settings Profile",
    };
  }

  function createSolveRun(palette, configSnapshot, recipeContext = null) {
    app.state.solve.solveRunCounter++;
    const currentRecipeContext = recipeContext || app.commands.buildSolveRecipeContext(palette);
    return {
      id: `run-${Date.now()}`,
      label: `Run ${app.state.solve.solveRunCounter}`,
      image: app.state.image.selectedImage ? {
        filename: app.state.image.selectedImage.filename,
        width: app.state.image.selectedImage.width,
        height: app.state.image.selectedImage.height,
        scale_pct: configSnapshot.scale_pct || 100,
      } : null,
      palette: [...palette],
      config: app.commands._cloneValue(configSnapshot),
      ar: app.commands.getEffectiveAR(),
      profile_ref: app.commands._cloneValue(currentRecipeContext.profile_ref),
      profile_name_at_solve: currentRecipeContext.profile_name_at_solve,
      is_profile_modified_at_solve: !!currentRecipeContext.is_profile_modified_at_solve,
      recipe_snapshot: app.commands._cloneValue(currentRecipeContext.recipe_snapshot),
      results: null,
      exportRecords: [],
      selectedExportId: null,
      timestamp: Date.now(),
    };
  }

  function _runAspect(run) {
    const ar = (run && typeof run.ar === "number" && isFinite(run.ar) && run.ar > 0)
      ? run.ar
      : app.commands.getEffectiveAR();
    return `${Math.round(ar * 1000)} / 1000`;
  }

  function getSelectedRuns() {
    return app.state.solve.solveRuns.filter(r => app.state.solve.selectedRunIds.has(r.id));
  }

  function showToast(message, type) {
    const toast = app.state.ui.$("#toast");
    toast.textContent = message;
    toast.className = "toast is-visible" + (type ? ` toast-${type}` : "");
    clearTimeout(toast._timer);
    toast._timer = setTimeout(() => toast.classList.remove("is-visible"), 3000);
  }

  function _slowButtons() {
    return [
      app.state.ui.$("#suggestPalettesBtn"), app.state.ui.$("#exportFilesBtn"),
    ].filter(Boolean);
  }

  function startProgress(label, owner = "", { cancellable = true } = {}) {
    app.state.ui._opAbort = new AbortController();
    app.state.ui._opStartTime = Date.now();
    app.state.ui._opLastElapsedSeconds = 0;
    const el = app.state.ui.$("#opProgress");
    const lbl = app.state.ui.$("#opProgressLabel");
    const elapsed = app.state.ui.$("#opProgressElapsed");
    const cancelBtn = app.state.ui.$("#opProgressCancel");
    const fill = el?.querySelector(".op-progress-fill");
    if (lbl) lbl.textContent = label;
    if (elapsed) elapsed.textContent = "0s";
    if (cancelBtn) {
      cancelBtn.hidden = !cancellable;
      cancelBtn.disabled = !cancellable;
    }
    if (fill) {
      fill.className = "op-progress-fill indeterminate";
      fill.style.width = "";
    }
    if (el) {
      el.dataset.owner = owner;
      el.dataset.cancellable = cancellable ? "true" : "false";
      el.classList.remove("is-hidden");
    }
    clearInterval(app.state.ui._opTimer);
    app.state.ui._opTimer = setInterval(() => {
      const s = Math.round((Date.now() - app.state.ui._opStartTime) / 1000);
      app.commands.setOperationElapsedSeconds(s);
    }, 500);
    app.commands._slowButtons().forEach(b => b.disabled = true);
    return app.state.ui._opAbort.signal;
  }

  function setOperationElapsedSeconds(seconds) {
    const elapsed = app.state.ui.$("#opProgressElapsed");
    const next = Math.max(0, Math.round(Number(seconds) || 0));
    app.state.ui._opLastElapsedSeconds = Math.max(app.state.ui._opLastElapsedSeconds || 0, next);
    if (elapsed) elapsed.textContent = `${app.state.ui._opLastElapsedSeconds}s`;
  }

  function formatDurationSeconds(seconds) {
    const value = Number(seconds);
    if (!Number.isFinite(value) || value < 0) return "";
    const rounded = Math.round(value);
    if (rounded < 60) return `${rounded}s`;
    const minutes = Math.floor(rounded / 60);
    const remainder = rounded % 60;
    return remainder ? `${minutes}m ${remainder}s` : `${minutes}m`;
  }

  function resetOperationElapsedSeconds() {
    app.state.ui._opLastElapsedSeconds = 0;
    const elapsed = app.state.ui.$("#opProgressElapsed");
    if (elapsed) elapsed.textContent = "0s";
  }

  function stopProgress() {
    const el = app.state.ui.$("#opProgress");
    const cancelBtn = app.state.ui.$("#opProgressCancel");
    if (el) {
      el.classList.add("is-hidden");
      el.dataset.owner = "";
      el.dataset.cancellable = "";
    }
    if (cancelBtn) {
      cancelBtn.hidden = false;
      cancelBtn.disabled = false;
    }
    clearInterval(app.state.ui._opTimer);
    app.state.ui._opTimer = null;
    app.state.ui._opAbort = null;
    app.state.ui._opLastElapsedSeconds = 0;
    app.commands._slowButtons().forEach(b => b.disabled = false);
  }

  function renderSuggestCancellationState() {
    const el = app.state.ui.$("#opProgress");
    if (!el || el.dataset.owner !== "suggest") return;
    const label = app.state.ui.$("#opProgressLabel");
    const cancelBtn = app.state.ui.$("#opProgressCancel");
    const prefix = "Cancellation requested: ";
    if (label) {
      const current = String(label.textContent || "");
      if (app.state.palette.suggestCancelPending && !current.startsWith(prefix)) {
        label.textContent = `${prefix}${current || "Suggesting palettes..."}`;
      } else if (!app.state.palette.suggestCancelPending && current.startsWith(prefix)) {
        label.textContent = current.slice(prefix.length);
      }
    }
    if (cancelBtn) cancelBtn.disabled = app.state.palette.suggestCancelPending;
  }

  async function requestSuggestCancellation() {
    if (!app.state.ui._suggestPolling || !app.state.ui.activeSuggestJobId || app.state.palette.suggestCancelPending) return;
    const cancellationJobId = app.state.ui.activeSuggestJobId;
    app.state.palette.suggestCancelPending = true;
    app.commands.renderSuggestCancellationState();
    try {
      const response = await app.api.apiPost(
        `/palette/suggest/cancel?job_id=${encodeURIComponent(cancellationJobId)}`,
        {},
      );
      if (app.state.ui.activeSuggestJobId !== cancellationJobId) return;
      if (response?.cancelled) assertPolledJobIdentity(response, cancellationJobId);
      if (!response?.cancelled) {
        app.state.palette.suggestCancelPending = false;
        app.commands.renderSuggestCancellationState();
        return;
      }
      app.commands.showToast("Suggestion cancellation requested", "warn");
    } catch (err) {
      if (app.state.ui.activeSuggestJobId !== cancellationJobId) return;
      app.state.palette.suggestCancelPending = false;
      app.commands.renderSuggestCancellationState();
      app.commands.showToast(`Could not request suggestion cancellation: ${err.message}`, "error");
    }
  }

  function renderExportCancellationState() {
    const el = app.state.ui.$("#opProgress");
    if (!el || el.dataset.owner !== "export") return;
    const label = app.state.ui.$("#opProgressLabel");
    const cancelBtn = app.state.ui.$("#opProgressCancel");
    const prefix = "Cancellation requested: ";
    if (label) {
      const current = String(label.textContent || "");
      if (app.state.export.exportCancelPending && !current.startsWith(prefix)) {
        label.textContent = `${prefix}${current || "Exporting files..."}`;
      } else if (!app.state.export.exportCancelPending && current.startsWith(prefix)) {
        label.textContent = current.slice(prefix.length);
      }
    }
    if (cancelBtn) cancelBtn.disabled = !app.state.export.activeExportJobId || app.state.export.exportCancelPending;
  }

  async function requestExportCancellation() {
    if (!app.state.export.exportRunning || !app.state.export.activeExportJobId || app.state.export.exportCancelPending) return;
    const cancellationJobId = app.state.export.activeExportJobId;
    app.state.export.exportCancelPending = true;
    app.commands.renderExportCancellationState();
    try {
      const response = await app.api.cancelExport(cancellationJobId);
      if (app.state.export.activeExportJobId !== cancellationJobId) return;
      if (response?.cancelled) assertPolledJobIdentity(response, cancellationJobId);
      if (!response?.cancelled) {
        app.state.export.exportCancelPending = false;
        app.commands.renderExportCancellationState();
        return;
      }
      app.commands.showToast("Export cancellation requested", "warn");
    } catch (err) {
      if (app.state.export.activeExportJobId !== cancellationJobId) return;
      app.state.export.exportCancelPending = false;
      app.commands.renderExportCancellationState();
      app.commands.showToast(`Could not request export cancellation: ${err.message}`, "error");
    }
  }

  function cancelProgress() {
    const el = app.state.ui.$("#opProgress");
    if (el && el.dataset.owner === "solve") {
      // Delegate to solve cancel handler
      app.commands.handleCancelSolve();
      return;
    }
    if (el && el.dataset.owner === "export") {
      void app.commands.requestExportCancellation();
      return;
    }
    if (el && el.dataset.owner === "suggest") {
      app.commands.requestSuggestCancellation();
      return;
    }
    if (el?.dataset.cancellable === "false") return;
    if (app.state.ui._opAbort) app.state.ui._opAbort.abort();
    app.commands.showToast("Cancelled", "warn");
    app.commands.stopProgress();
  }

  function updateOperationProgressFromStatus(status, fallbackLabel = "Working...") {
    const el = app.state.ui.$("#opProgress");
    const lbl = app.state.ui.$("#opProgressLabel");
    const elapsed = app.state.ui.$("#opProgressElapsed");
    const fill = el?.querySelector(".op-progress-fill");
    const d = status?.progress_detail || {};

    let label = d.stage_label || status?.progress || fallbackLabel;
    if (el?.dataset.owner === "suggest") {
      app.state.palette.suggestCancelPending = app.state.palette.suggestCancelPending || Boolean(status?.cancel_requested);
    }
    if (el?.dataset.owner === "export") {
      app.state.export.exportCancelPending = app.state.export.exportCancelPending
        || status?.status === "cancelling"
        || Boolean(status?.cancel_requested);
    }
    if (d.stage_index && d.stage_count) {
      label = `Step ${d.stage_index}/${d.stage_count}: ${label}`;
    }
    if (lbl) lbl.textContent = label;
    app.commands.renderSuggestCancellationState();
    app.commands.renderExportCancellationState();

    const pct = d.overall_pct ?? d.stage_pct;
    if (fill) {
      if (pct != null && Number.isFinite(Number(pct))) {
        fill.className = "op-progress-fill";
        fill.style.width = `${Math.max(0, Math.min(100, Number(pct)))}%`;
      } else {
        fill.className = "op-progress-fill indeterminate";
        fill.style.width = "";
      }
    }

    const elapsedVal = status?.elapsed_s ?? d.elapsed_s;
    if (elapsed && elapsedVal != null) app.commands.setOperationElapsedSeconds(elapsedVal);
  }

  function appConfirm(message, { ok = "OK", cancel = "Cancel", title = "Confirm" } = {}) {
    return new Promise(resolve => {
      const overlay = app.state.ui.$("#appDialog");
      const titleEl = app.state.ui.$("#appDialogTitle");
      const msg = app.state.ui.$("#appDialogMsg");
      const input = app.state.ui.$("#appDialogInput");
      const buttons = app.state.ui.$("#appDialogButtons");
      const closeBtn = app.state.ui.$("#appDialogClose");
      const hint = app.state.ui.$("#appDialogHint");
      if (titleEl) titleEl.textContent = title;
      msg.textContent = message;
      input.style.display = "none";
      if (hint) {
        hint.classList.add("is-hidden");
        hint.innerHTML = "";
      }
      buttons.innerHTML = `
        <button class="ghost-button small" id="appDialogNo">${app.commands.esc2(cancel)}</button>
        <button class="primary-button small" id="appDialogYes">${app.commands.esc2(ok)}</button>
      `;
      overlay.setAttribute("aria-hidden", "false");
      const close = (val) => { overlay.setAttribute("aria-hidden", "true"); resolve(val); };
      app.state.ui.$("#appDialogNo").onclick = () => close(false);
      app.state.ui.$("#appDialogYes").onclick = () => close(true);
      if (closeBtn) closeBtn.onclick = () => close(false);
      overlay.onclick = (e) => { if (e.target === overlay) close(false); };
    });
  }

  function appPrompt(message, defaultValue = "", { title = "Input", validate = null } = {}) {
    return new Promise(resolve => {
      const overlay = app.state.ui.$("#appDialog");
      const titleEl = app.state.ui.$("#appDialogTitle");
      const msg = app.state.ui.$("#appDialogMsg");
      const input = app.state.ui.$("#appDialogInput");
      const buttons = app.state.ui.$("#appDialogButtons");
      const closeBtn = app.state.ui.$("#appDialogClose");
      const validationClass = "app-dialog-validation";
      const previousFocus = document.activeElement;
      if (titleEl) titleEl.textContent = title;
      msg.textContent = message;
      input.style.display = "";
      input.value = defaultValue;
      buttons.innerHTML = `
        <button class="ghost-button small" id="appDialogNo">Cancel</button>
        <button class="primary-button small" id="appDialogYes">OK</button>
      `;
      overlay.setAttribute("aria-hidden", "false");
      let settled = false;
      const cancelBtn = app.state.ui.$("#appDialogNo");
      const okBtn = app.state.ui.$("#appDialogYes");
      const onDocumentKeyDown = (e) => {
        if (overlay.getAttribute("aria-hidden") !== "false") return;
        if (e.key === "Escape") {
          e.preventDefault();
          close(null);
        }
      };
      const cleanup = () => {
        document.removeEventListener("keydown", onDocumentKeyDown);
        overlay.onclick = null;
        input.onkeydown = null;
        input.oninput = null;
        if (cancelBtn) cancelBtn.onclick = null;
        if (okBtn) okBtn.onclick = null;
        if (closeBtn) closeBtn.onclick = null;
        input.removeAttribute("aria-invalid");
        overlay.querySelector(`.${validationClass}`)?.remove();
      };
      const close = (val) => {
        if (settled) return;
        settled = true;
        cleanup();
        overlay.setAttribute("aria-hidden", "true");
        if (previousFocus && document.body.contains(previousFocus) && typeof previousFocus.focus === "function") {
          previousFocus.focus();
        } else {
          input.blur();
        }
        resolve(val);
      };
      const clearValidation = () => {
        input.removeAttribute("aria-invalid");
        overlay.querySelector(`.${validationClass}`)?.remove();
      };
      const showValidation = (messageText) => {
        input.setAttribute("aria-invalid", "true");
        let validation = overlay.querySelector(`.${validationClass}`);
        if (!validation) {
          validation = document.createElement("div");
          validation.className = validationClass;
          input.parentElement?.appendChild(validation);
        }
        validation.textContent = String(messageText);
      };
      const submit = () => {
        const validationMessage = typeof validate === "function" ? validate(input.value) : "";
        if (validationMessage) {
          showValidation(validationMessage);
          input.focus();
          return;
        }
        close(input.value);
      };
      if (cancelBtn) cancelBtn.onclick = () => close(null);
      if (okBtn) okBtn.onclick = submit;
      if (closeBtn) closeBtn.onclick = () => close(null);
      input.oninput = clearValidation;
      overlay.onclick = (e) => { if (e.target === overlay) close(null); };
      input.onkeydown = (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          submit();
        } else if (e.key === "Escape") {
          e.preventDefault();
          close(null);
        }
      };
      document.addEventListener("keydown", onDocumentKeyDown);
      setTimeout(() => {
        if (settled || overlay.getAttribute("aria-hidden") !== "false") return;
        input.focus();
        input.select();
      }, 50);
    });
  }

  function appChoice(message, options = [], { title = "Choose" } = {}) {
    return new Promise(resolve => {
      const overlay = app.state.ui.$("#appDialog");
      const titleEl = app.state.ui.$("#appDialogTitle");
      const msg = app.state.ui.$("#appDialogMsg");
      const input = app.state.ui.$("#appDialogInput");
      const buttons = app.state.ui.$("#appDialogButtons");
      const closeBtn = app.state.ui.$("#appDialogClose");
      const hint = app.state.ui.$("#appDialogHint");
      if (titleEl) titleEl.textContent = title;
      msg.textContent = message;
      input.style.display = "none";
      if (hint) {
        hint.classList.add("is-hidden");
        hint.innerHTML = "";
      }
      buttons.innerHTML = options.map((opt, index) => {
        const btnClass = opt.kind === "primary"
          ? "primary-button small"
          : opt.kind === "danger"
            ? "ghost-button small danger"
            : "ghost-button small";
        const id = `appDialogChoice${index}`;
        return `<button class="${btnClass}" id="${id}">${app.commands.esc2(opt.label)}</button>`;
      }).join("");
      overlay.setAttribute("aria-hidden", "false");
      const close = (val) => { overlay.setAttribute("aria-hidden", "true"); resolve(val); };
      options.forEach((opt, index) => {
        app.state.ui.$(`#appDialogChoice${index}`).onclick = () => close(opt.value);
      });
      if (closeBtn) closeBtn.onclick = () => close(null);
      overlay.onclick = (e) => { if (e.target === overlay) close(null); };
    });
  }

  function showPaletteSaveModal(defaultValue = "") {
    return new Promise(resolve => {
      const overlay = app.state.ui.$("#paletteSaveModal");
      const input = app.state.ui.$("#paletteSaveNameInput");
      const cancel = app.state.ui.$("#paletteSaveCancel");
      const submit = app.state.ui.$("#paletteSaveSubmit");
      const closeBtn = app.state.ui.$("#paletteSaveModalClose");
      if (!overlay || !input || !cancel || !submit || !closeBtn) {
        resolve(null);
        return;
      }

      let settled = false;
      const close = (value) => {
        if (settled) return;
        settled = true;
        overlay.classList.add("is-hidden");
        overlay.setAttribute("aria-hidden", "true");
        overlay.onclick = null;
        cancel.onclick = null;
        submit.onclick = null;
        closeBtn.onclick = null;
        input.onkeydown = null;
        document.removeEventListener("keydown", onKeyDown);
        resolve(value);
      };
      const onKeyDown = (e) => {
        if (overlay.getAttribute("aria-hidden") !== "false") return;
        if (e.key === "Escape") close(null);
      };

      input.value = defaultValue || "";
      overlay.classList.remove("is-hidden");
      overlay.setAttribute("aria-hidden", "false");
      overlay.onclick = (e) => { if (e.target === overlay) close(null); };
      cancel.onclick = () => close(null);
      closeBtn.onclick = () => close(null);
      submit.onclick = () => close(input.value);
      input.onkeydown = (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          close(input.value);
        } else if (e.key === "Escape") {
          e.preventDefault();
          close(null);
        }
      };
      document.addEventListener("keydown", onKeyDown);
      setTimeout(() => {
        input.focus();
        input.select();
      }, 0);
    });
  }

  function esc2(str) { const el = document.createElement("span"); el.textContent = str; return el.innerHTML; }

  function esc(str) {
    const el = document.createElement("span");
    el.textContent = str;
    return el.innerHTML;
  }

  function escAttr(str) {
    return String(str ?? "").replace(/[&<>"']/g, (char) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    })[char]);
  }

  function colorRmseValue(result) {
    if (!result) return null;
    const value = result.source_rms_de ?? result.color_rmse ?? result.suggestion_mean_de ?? result.mean_de;
    const numeric = Number(value);
    return Number.isFinite(numeric) ? numeric : null;
  }

  function formatColorRmse(result, digits = 3) {
    const rmse = app.commands.colorRmseValue(result);
    if (rmse == null) return "RMSE % --";
    if (result?.suggestion_mean_de != null && result?.source_rms_de == null && result?.color_rmse == null) {
      return `Suggest dE ${rmse.toFixed(digits)}`;
    }
    return `RMSE % ${(rmse * 100).toFixed(digits)}`;
  }

  function formatSolveRunCardRmse(result, digits = 3) {
    const rmse = app.commands.colorRmseValue(result);
    if (rmse == null) return "RMSE --";
    return `RMSE ${(rmse * 100).toFixed(digits)}%`;
  }

  function textColorForHex(hex) {
    const r = parseInt(hex.slice(1, 3), 16);
    const g = parseInt(hex.slice(3, 5), 16);
    const b = parseInt(hex.slice(5, 7), 16);
    return (0.299 * r + 0.587 * g + 0.114 * b) > 140 ? "#1f1b18" : "#fffdf8";
  }

  function isLightHex(hex) {
    if (typeof hex !== "string" || !/^#[0-9a-f]{6}$/i.test(hex)) return false;
    const r = parseInt(hex.slice(1, 3), 16);
    const g = parseInt(hex.slice(3, 5), 16);
    const b = parseInt(hex.slice(5, 7), 16);
    return (0.299 * r + 0.587 * g + 0.114 * b) > 180;
  }

  function filamentById(id) {
    return app.state.session.allFilaments.find((f) => f.filament_id === id);
  }

  function switchTab(tab) {
    // If printer config is open and this is an external tab click, close config first
    const pcPage = app.state.ui.$("#printerConfigPage");
    if (pcPage && !pcPage.classList.contains("is-hidden") && app.state.session.printerConfigOriginTab !== null) {
      // hidePrinterConfigPage will call switchTab again with the target
      app.commands.hidePrinterConfigPage(tab);
      return;
    }
    app.commands.hideSolveRunHoverPreview();
    app.commands.hideSolveRunSettingsPanel();
    app.state.ui.currentTab = tab;
    app.state.ui.$$(".mode-button").forEach((btn) => {
      btn.classList.toggle("is-active", btn.dataset.tab === tab);
    });
    app.state.ui.$$(".tab-content").forEach((section) => {
      section.classList.toggle("is-hidden", section.id !== `tab${app.commands.capitalize(tab)}`);
    });
    if (tab === "image") app.commands.renderImageTab();
    if (tab === "creation") app.commands.renderCreationTab();
    if (tab === "solve") app.commands.renderSolveTab();
    if (tab === "export") app.commands.renderExportTab();
    app.commands.updateRail();
  }

  function capitalize(s) { return s.charAt(0).toUpperCase() + s.slice(1); }

  function updateTabStates() {
    app.state.ui.$$(".mode-button").forEach((btn) => {
      const tab = btn.dataset.tab;
      let complete = false;
      if (tab === "image") complete = !!app.state.image.selectedImage;
      if (tab === "creation") complete = app.commands.getActivePalette().length > 0;
      if (tab === "settings") complete = !!app.state.image.selectedImage && app.commands.getActivePalette().length > 0;
      if (tab === "solve") complete = app.state.solve.solveRuns.some(run => !!run.results);
      if (tab === "export") complete = app.state.solve.solveRuns.some((run) => app.commands.getRunExportRecords(run).length > 0);
      btn.classList.toggle("step-complete", complete);
    });
  }

  function updateRailFramedPreview(container) {
    // Render the framed preview (with crop, rotation, adjustments, border) to a
    // small canvas and display it in the sidebar thumbnail.
    const srcImg = app.state.ui.$("#previewImg");
    if (!srcImg || !srcImg.naturalWidth || !app.state.image.selectedImage) return;

    const ar = app.commands.getEffectiveAR();
    const bwMm = (app.state.settings.config.border && app.state.settings.config.border_width_mm > 0) ? app.state.settings.config.border_width_mm : 0;
    const footW = app.state.image.frameState.widthMm + 2 * bwMm;
    const footH = app.state.image.frameState.heightMm + 2 * bwMm;
    const footAR = footW / footH;

    // Thumbnail size (fit within ~200px)
    const maxDim = 200;
    let thumbW, thumbH;
    if (footAR > 1) { thumbW = maxDim; thumbH = maxDim / footAR; }
    else { thumbH = maxDim; thumbW = maxDim * footAR; }

    const borderFrac = bwMm / footW;
    const bPx = borderFrac * thumbW;
    const frameW = thumbW - 2 * bPx;
    const frameH = thumbH - 2 * bPx;

    const canvas = document.createElement("canvas");
    canvas.width = thumbW;
    canvas.height = thumbH;
    const ctx = canvas.getContext("2d");

    // White border band
    if (bPx > 0) {
      ctx.fillStyle = "#fff";
      ctx.fillRect(0, 0, thumbW, thumbH);
    }

    // Black fallback behind the cropped image frame.
    ctx.fillStyle = "#000";
    ctx.fillRect(bPx, bPx, frameW, frameH);

    // Draw image within the frame area, applying crop/scale/rotation/flip.
    // The image always covers the frame; aspect differences crop source content.
    ctx.save();
    ctx.beginPath();
    ctx.rect(bPx, bPx, frameW, frameH);
    ctx.clip();

    const imgNatW = srcImg.naturalWidth;
    const imgNatH = srcImg.naturalHeight;
    const geom = app.commands.cropCoverImageGeometry(frameW, frameH, imgNatW, imgNatH, app.state.image.frameState.scale, app.state.image.frameState.rotation);
    const displayW = geom.displayW;
    const displayH = geom.displayH;

    const slackX = Math.max(0, geom.visualW - frameW);
    const slackY = Math.max(0, geom.visualH - frameH);
    const offsetX = app.state.image.frameState.panX * slackX / 2;
    const offsetY = app.state.image.frameState.panY * slackY / 2;

    const imgL = bPx + frameW / 2 - offsetX;
    const imgT = bPx + frameH / 2 - offsetY;

    ctx.translate(imgL, imgT);
    ctx.rotate(app.state.image.frameState.rotation * Math.PI / 180);
    ctx.scale(app.state.image.frameState.flipH ? -1 : 1, app.state.image.frameState.flipV ? -1 : 1);
    ctx.drawImage(srcImg, -displayW / 2, -displayH / 2, displayW, displayH);
    ctx.restore();

    // Pixelation pass — always on in sidebar (shows actual print resolution)
    {
      const pxSizeMm = app.state.settings.config.image_sample_pitch_mm || 0.20;
      const gridW = Math.max(1, Math.round(app.state.image.frameState.widthMm / pxSizeMm));
      const gridH = Math.max(1, Math.round(app.state.image.frameState.heightMm / pxSizeMm));

      // Draw image only at solve-grid resolution, using the same crop-cover model.
      const tmp = document.createElement("canvas");
      tmp.width = gridW;
      tmp.height = gridH;
      const tmpCtx = tmp.getContext("2d");
      const gGeom = app.commands.cropCoverImageGeometry(gridW, gridH, imgNatW, imgNatH, app.state.image.frameState.scale, app.state.image.frameState.rotation);
      const gDispW = gGeom.displayW;
      const gDispH = gGeom.displayH;
      const gSlackX = Math.max(0, gGeom.visualW - gridW);
      const gSlackY = Math.max(0, gGeom.visualH - gridH);
      const gOffX = app.state.image.frameState.panX * gSlackX / 2;
      const gOffY = app.state.image.frameState.panY * gSlackY / 2;
      tmpCtx.save();
      tmpCtx.translate(gridW / 2 - gOffX, gridH / 2 - gOffY);
      tmpCtx.rotate(app.state.image.frameState.rotation * Math.PI / 180);
      tmpCtx.scale(app.state.image.frameState.flipH ? -1 : 1, app.state.image.frameState.flipV ? -1 : 1);
      tmpCtx.drawImage(srcImg, -gDispW / 2, -gDispH / 2, gDispW, gDispH);
      tmpCtx.restore();

      // Black background then pixelated image on top
      ctx.save();
      ctx.beginPath();
      ctx.rect(bPx, bPx, frameW, frameH);
      ctx.clip();
      ctx.fillStyle = "#000";
      ctx.fillRect(bPx, bPx, frameW, frameH);
      ctx.imageSmoothingEnabled = false;
      ctx.drawImage(tmp, 0, 0, gridW, gridH, bPx, bPx, frameW, frameH);
      ctx.restore();
    }

    // Thin outline around the whole thing
    ctx.strokeStyle = "rgba(0,0,0,0.15)";
    ctx.lineWidth = 1;
    ctx.strokeRect(0.5, 0.5, thumbW - 1, thumbH - 1);

    container.innerHTML = "";
    const img = document.createElement("img");
    img.src = canvas.toDataURL();
    img.alt = app.state.image.selectedImage.filename;
    container.appendChild(img);
  }

  function updateRail() {
    const preview = app.state.ui.$("#railImagePreview");
    if (app.state.image.selectedImage) {
      if (app.state.ui.currentTab === "image") {
        // Tab 1: show source image
        preview.innerHTML = `<img src="${app.api.imagePreviewUrl(
          app.state.image.selectedImage.filename,
          app.state.image.selectedImage.source_ref,
        )}" alt="${app.commands.escAttr(app.state.image.selectedImage.filename)}">`;
      } else {
        // Other tabs: show framed preview snapshot
        app.commands.updateRailFramedPreview(preview);
      }
    } else {
      preview.innerHTML = `<span class="muted-line">No image selected</span>`;
    }

    app.commands.updateTabStates();

    const badge = app.state.ui.$("#dataSourceBadge");
    badge.textContent = app.state.session.apiConnected ? "connected" : "offline";
    badge.classList.toggle("connected", app.state.session.apiConnected);
    app.commands.renderModelLibraryRail();
    app.commands.updateSolveReadiness();
  }

  function renderPrinterRail() {
    if (!app.state.session.printersData) return;
    const printers = app.state.session.printersData.printers || [];
    const activeId = app.state.session.printersData.active_printer_id;
    const container = app.state.ui.$("#railPrinterSelector");
    if (!container) return;

    if (printers.length <= 1) {
      const printerName = printers.length
        ? (printers[0].name || app.state.session.printerConfig.name || "Unnamed printer")
        : "No printer configured";
      container.innerHTML = `<div class="rail-printer-name" title="${app.commands.escAttr(printerName)}">${app.commands.esc(printerName)}</div>`;
    } else {
      const activePrinter = printers.find(p => p.id === activeId) || printers[0];
      container.innerHTML = `<select id="railPrinterSelect" class="rail-select" title="${app.commands.escAttr(activePrinter?.name || "Active printer")}" aria-label="Active printer">
        ${printers.map(p => `<option value="${p.id}"${p.id === activeId ? " selected" : ""}>${app.commands.esc(p.name)}</option>`).join("")}
      </select>`;
      const sel = app.state.ui.$("#railPrinterSelect");
      if (sel) sel.addEventListener("change", async () => {
        const previousId = app.state.session.printersData.active_printer_id;
        sel.disabled = true;
        let active;
        try {
          active = await app.api.setActivePrinter({ active_printer_id: sel.value });
        } catch (error) {
          app.state.session.printersData.active_printer_id = previousId;
          app.commands.renderPrinterRail();
          app.commands.showToast(`Failed to change printer: ${error.message}`, "error");
          return;
        }
        app.state.session.printersData.active_printer_id = active.printer?.id || sel.value;
        app.state.session.printersData.active_nozzle_size = active.nozzle?.size ?? null;
        try {
          app.commands.applyAuthoritativePrinterState(app.state.session.printersData, active);
        } catch (error) {
          console.error("[printers] active printer could not be rendered:", error);
          app.commands.showToast(
            "Printer changed, but the display could not refresh. Reload Prisma.",
            "error",
          );
        }
      });
    }

    // Nozzle dropdown
    const printer = printers.find(p => p.id === activeId) || printers[0];
    const nozzleSel = app.state.ui.$("#railNozzleSelect");
    if (nozzleSel && printer) {
      const profiles = printer.nozzle_profiles || [];
      nozzleSel.innerHTML = profiles.map(n =>
        `<option value="${n.size}"${n.size === app.state.session.printersData.active_nozzle_size ? " selected" : ""}>${n.size}mm</option>`
      ).join("");
      nozzleSel.onchange = async () => {
        const previousSize = app.state.session.printersData.active_nozzle_size;
        nozzleSel.disabled = true;
        let active;
        try {
          active = await app.api.setActivePrinter({ active_nozzle_size: parseFloat(nozzleSel.value) });
        } catch (error) {
          app.state.session.printersData.active_nozzle_size = previousSize;
          app.commands.renderPrinterRail();
          app.commands.showToast(`Failed to change nozzle: ${error.message}`, "error");
          return;
        }
        app.state.session.printersData.active_nozzle_size = active.nozzle?.size ?? null;
        try {
          app.commands.applyAuthoritativePrinterState(app.state.session.printersData, active);
        } catch (error) {
          console.error("[printers] active nozzle could not be rendered:", error);
          app.commands.showToast(
            "Nozzle changed, but the display could not refresh. Reload Prisma.",
            "error",
          );
        }
      };
      nozzleSel.disabled = profiles.length === 0;
      nozzleSel.title = profiles.length ? "Active nozzle" : "No nozzle profiles configured";
    } else if (nozzleSel) {
      nozzleSel.innerHTML = "";
      nozzleSel.onchange = null;
      nozzleSel.disabled = true;
      nozzleSel.title = "No printer configured";
    }
  }

  Object.assign(app.commands, {
    getBaseFilament,
    getBaseCapIds,
    getBaseCapSlots,
    xIconSvg,
    panelResizeIconSvg,
    getActivePalette,
    normalizeColorCapModeForStorage,
    loadLastColorCapMode,
    saveLastColorCapMode,
    applyMandatoryProductSettings,
    formatRegionPlanningScale,
    formatRegionMethod,
    getCurrentLayerHeight,
    minCapLayersFromThickness,
    minCapThicknessFromLayers,
    smoothingRadiusMmFromCells,
    smoothingCellsFromRadiusMm,
    normalizeLuminanceMode,
    clampLuminanceBaseShadingLimitFraction,
    getLuminanceBaseShadingLimitFraction,
    setLuminanceBaseShadingLimitFraction,
    formatLuminanceBaseShadingLimitPercent,
    parseLuminanceBaseShadingLimitPercent,
    getBaseShadingLimitInput,
    getBaseShadingLimitSlider,
    syncBaseShadingLimitControls,
    applyLuminanceMode,
    normalizeActiveGamutMode,
    normalizeChromaWeight,
    chromaWeightToSliderPosition,
    chromaWeightFromSliderPosition,
    formatChromaWeightReadout,
    syncChromaWeightControlFromConfig,
    applyChromaWeightSliderInput,
    getSolveModeControlValue,
    setSolveModeControlValue,
    buildSolveRecipeContext,
    describeSolveRunProfile,
    createSolveRun,
    _runAspect,
    getSelectedRuns,
    showToast,
    _slowButtons,
    startProgress,
    setOperationElapsedSeconds,
    formatDurationSeconds,
    resetOperationElapsedSeconds,
    stopProgress,
    renderSuggestCancellationState,
    requestSuggestCancellation,
    renderExportCancellationState,
    requestExportCancellation,
    cancelProgress,
    updateOperationProgressFromStatus,
    appConfirm,
    appPrompt,
    appChoice,
    showPaletteSaveModal,
    esc2,
    esc,
    escAttr,
    colorRmseValue,
    formatColorRmse,
    formatSolveRunCardRmse,
    textColorForHex,
    isLightHex,
    filamentById,
    switchTab,
    capitalize,
    updateTabStates,
    updateRailFramedPreview,
    updateRail,
    renderPrinterRail,
  });}
