import { assertPolledJobIdentity } from "../../core/polling.js";
import { createAnchoredMenuController } from "../../core/anchored-menu.js?v=2026-08-11-rail-selectors-v1";

/**
 * Install the shell/index feature commands.
 * @param {import("../../core/types.js").ApplicationContext} app
 */
export function installFeaturesShellIndex(app) {
  let railPrintSetupMenuControllers = [];
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

  function loadLastColorCapMode(fallback = null) {
    try {
      const stored = app.persistence.read(app.state.settings.COLOR_CAP_MODE_STORAGE_KEY);
      if (stored === "appearance_bounded_smooth" || stored === "smooth_variable") {
        return stored;
      }
    } catch { /* ignore */ }
    return app.commands.normalizeColorCapModeForStorage(fallback || app.commands.settingDefault("cap_mode"));
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

  function formatRegionPlanningScale(value = app.state.settings.config.stage1_coarsening_factor || app.commands.settingDefault("stage1_coarsening_factor")) {
    const factor = Math.max(1, parseInt(value, 10) || 1);
    return factor === 1 ? "1x (full detail)" : `${factor}x (coarser regions)`;
  }

  function formatRegionMethod(mode = app.state.settings.config.cell_mode || app.commands.settingDefault("cell_mode")) {
    switch (String(mode || app.commands.settingDefault("cell_mode")).toLowerCase()) {
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
    return Number.isFinite(configValue) && configValue > 0 ? configValue : app.commands.settingDefault("layer_height");
  }

  function minimumCapThicknessMm(layerCount = app.state.settings.config.min_cap_layers, layerHeight = app.commands.getCurrentLayerHeight()) {
    const layers = Math.max(1, Math.trunc(Number(layerCount) || 1));
    const lh = Math.max(Number(layerHeight) || app.commands.settingDefault("layer_height"), 1e-9);
    return Math.round(layers * lh * 1e6) / 1e6;
  }

  function normalizeLuminanceMode(mode) {
    const raw = String(mode || app.commands.settingDefault("luminance_mode")).trim().toLowerCase();
    if (["luminance", "luminance-detail", "luminance_detail", "detail"].includes(raw)) {
      return "luminance_detail";
    }
    return "standard";
  }

  function clampLuminanceBaseShadingLimitFraction(value) {
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) return app.commands.settingDefault("luminance_base_shading_limit_fraction");
    return Math.max(0.0, Math.min(1.0, parsed));
  }

  function getLuminanceBaseShadingLimitFraction() {
    const defaultValue = app.commands.settingDefault("luminance_base_shading_limit_fraction");
    const current = app.state.settings.config.luminance_base_shading_limit_fraction;
    const legacy = app.state.settings.config.luminance_handler_optical_authority_fraction;
    const currentParsed = Number(current);
    const legacyParsed = Number(legacy);
    if (
      Number.isFinite(legacyParsed)
      && (!Number.isFinite(currentParsed) || (currentParsed === defaultValue && legacyParsed !== defaultValue))
    ) {
      return app.commands.clampLuminanceBaseShadingLimitFraction(legacyParsed);
    }
    return app.commands.clampLuminanceBaseShadingLimitFraction(
      Number.isFinite(currentParsed) ? currentParsed : defaultValue,
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
    if (!Number.isFinite(parsed)) return app.commands.settingDefault("luminance_base_shading_limit_fraction");
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
    const normalized = String(mode || app.commands.settingDefault("gamut_mode")).trim().toLowerCase();
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
    const rawWeight = app.commands.normalizeChromaWeight(app.state.settings.config.chroma_weight ?? app.commands.settingDefault("chroma_weight"));
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
    return selected?.dataset.value || app.state.settings.config.luminance_mode || app.commands.settingDefault("luminance_mode");
  }

  function setSolveModeControlValue(mode) {
    const normalized = app.commands.normalizeLuminanceMode(mode);
    document.querySelectorAll("#cfgLuminanceMode .segmented-btn").forEach(btn => {
      const active = app.commands.normalizeLuminanceMode(btn.dataset.value) === normalized;
      btn.classList.toggle("is-active", active);
      btn.setAttribute("aria-checked", active ? "true" : "false");
    });
    const paletteMode = app.state.ui.$("#paletteSuggestMode");
    if (paletteMode) paletteMode.value = normalized;
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
      deck_card_id: app.state.palette.activeDeckId || null,
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
      app.state.ui.$("#suggestPalettesBtn"), app.state.ui.$("#startSolveBtn"),
      app.state.ui.$("#exportFilesBtn"),
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

  function appConfirm(
    message,
    {
      ok = "OK",
      cancel = "Cancel",
      title = "Confirm",
      restoreFocus = null,
      emphasis = [],
      detailHtml = "",
    } = {},
  ) {
    return new Promise(resolve => {
      const overlay = app.state.ui.$("#appDialog");
      const dialog = overlay?.querySelector?.(".app-dialog");
      const titleEl = app.state.ui.$("#appDialogTitle");
      const msg = app.state.ui.$("#appDialogMsg");
      const input = app.state.ui.$("#appDialogInput");
      const buttons = app.state.ui.$("#appDialogButtons");
      const closeBtn = app.state.ui.$("#appDialogClose");
      const hint = app.state.ui.$("#appDialogHint");
      if (!overlay || !msg || !input || !buttons) {
        resolve(false);
        return;
      }
      const dialogDocument = overlay.ownerDocument || document;
      const previousFocus = dialogDocument.activeElement;
      const focusRestoreTarget = restoreFocus || previousFocus;
      if (titleEl) titleEl.textContent = title;
      const emphasisValues = [...new Set(
        (Array.isArray(emphasis) ? emphasis : [])
          .map(value => String(value))
          .filter(Boolean),
      )];
      if (emphasisValues.length) {
        const source = String(message);
        let cursor = 0;
        let messageHtml = "";
        while (cursor < source.length) {
          let nextIndex = -1;
          let nextValue = "";
          for (const value of emphasisValues) {
            const index = source.indexOf(value, cursor);
            if (
              index >= 0
              && (nextIndex < 0 || index < nextIndex || (index === nextIndex && value.length > nextValue.length))
            ) {
              nextIndex = index;
              nextValue = value;
            }
          }
          if (nextIndex < 0) {
            messageHtml += app.commands.esc2(source.slice(cursor));
            break;
          }
          messageHtml += app.commands.esc2(source.slice(cursor, nextIndex));
          messageHtml += `<strong class="app-dialog-emphasis">${app.commands.esc2(nextValue)}</strong>`;
          cursor = nextIndex + nextValue.length;
        }
        msg.innerHTML = messageHtml;
      } else {
        msg.textContent = message;
      }
      input.style.display = "none";
      if (hint) {
        hint.innerHTML = detailHtml || "";
        hint.classList.toggle("is-hidden", !detailHtml);
      }
      dialog?.classList.toggle("is-detailed", Boolean(detailHtml));
      dialog?.setAttribute("aria-describedby", detailHtml ? "appDialogMsg appDialogHint" : "appDialogMsg");
      buttons.innerHTML = `
        <button class="ghost-button small" id="appDialogNo">${app.commands.esc2(cancel)}</button>
        <button class="primary-button small" id="appDialogYes">${app.commands.esc2(ok)}</button>
      `;
      overlay.setAttribute("aria-hidden", "false");
      let settled = false;
      const cancelBtn = app.state.ui.$("#appDialogNo");
      const okBtn = app.state.ui.$("#appDialogYes");
      const focusable = [closeBtn, cancelBtn, okBtn].filter(
        element => element && !element.disabled && typeof element.focus === "function",
      );
      const onDocumentKeyDown = (event) => {
        if (overlay.getAttribute("aria-hidden") !== "false") return;
        if (event.key === "Escape") {
          event.preventDefault();
          close(false);
          return;
        }
        if (event.key !== "Tab" || !focusable.length) return;
        const currentIndex = focusable.indexOf(dialogDocument.activeElement);
        let nextIndex = null;
        if (event.shiftKey && currentIndex <= 0) nextIndex = focusable.length - 1;
        if (!event.shiftKey && (currentIndex < 0 || currentIndex === focusable.length - 1)) nextIndex = 0;
        if (nextIndex === null) return;
        event.preventDefault();
        focusable[nextIndex].focus();
      };
      const cleanup = () => {
        dialogDocument.removeEventListener("keydown", onDocumentKeyDown);
        overlay.onclick = null;
        if (cancelBtn) cancelBtn.onclick = null;
        if (okBtn) okBtn.onclick = null;
        if (closeBtn) closeBtn.onclick = null;
        dialog?.classList.remove("is-detailed");
        dialog?.setAttribute("aria-describedby", "appDialogMsg");
      };
      const close = (val) => {
        if (settled) return;
        settled = true;
        cleanup();
        overlay.setAttribute("aria-hidden", "true");
        resolve(val);
        // Solve-start controls remain disabled until the awaiting handler's
        // finally block runs. Restore focus on the next task so the original
        // control can receive it after that cleanup.
        setTimeout(() => {
          if (
            focusRestoreTarget
            && dialogDocument.body?.contains(focusRestoreTarget)
            && typeof focusRestoreTarget.focus === "function"
            && !focusRestoreTarget.disabled
          ) {
            focusRestoreTarget.focus();
          }
        }, 0);
      };
      if (cancelBtn) cancelBtn.onclick = () => close(false);
      if (okBtn) okBtn.onclick = () => close(true);
      if (closeBtn) closeBtn.onclick = () => close(false);
      overlay.onclick = (e) => { if (e.target === overlay) close(false); };
      dialogDocument.addEventListener("keydown", onDocumentKeyDown);
      setTimeout(() => {
        if (settled || overlay.getAttribute("aria-hidden") !== "false") return;
        okBtn?.focus();
      }, 50);
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
    app.events.emit("tab.changed", { tab });
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
      let solveGrid = null;
      try {
        solveGrid = app.commands.getCurrentResolvedSolveGrid();
      } catch {
        // Invalid pitches are reported by the Image-page warning and rejected
        // by config sync. Keep the unpixelated preview usable in the meantime.
      }
      if (solveGrid) {
        const gridW = solveGrid.cells.width;
        const gridH = solveGrid.cells.height;

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
    const data = app.state.session.printersData;
    if (!data) return;
    const printers = data.printers || [];
    const activeId = data.active_printer_id;
    const container = app.state.ui.$("#railPrinterSelector");
    if (!container) return;

    for (const controller of railPrintSetupMenuControllers.splice(0)) controller.destroy();
    for (const menuId of ["#railPrinterMenu", "#railNozzleMenu", "#railExtrusionWidthMenu"]) {
      const menu = app.state.ui.$(menuId);
      if (!menu) continue;
      menu.hidden = true;
      menu.innerHTML = "";
      menu.style.left = "";
      menu.style.top = "";
    }

    const restoreRailFocus = selector => setTimeout(() => {
      app.state.ui.$(selector)?.focus();
    }, 0);

    const renderSelectorTrigger = ({ target, id, menuId, value, label, disabled = false }) => {
      target.innerHTML = `
        <button id="${id}" class="rail-selector-trigger" type="button"
                aria-label="${app.commands.escAttr(label)}" aria-haspopup="menu"
                aria-expanded="false" aria-controls="${menuId}"${disabled ? " disabled" : ""}>
          <span class="rail-selector-value">${app.commands.esc(value)}</span>
          <span class="rail-selector-chevron" aria-hidden="true"></span>
        </button>`;
      return app.state.ui.$(`#${id}`);
    };

    const renderStaticValue = ({ target, value, label }) => {
      target.innerHTML = `<div class="rail-selector-static" title="${app.commands.escAttr(label)}"><span class="rail-selector-value">${app.commands.esc(value)}</span></div>`;
    };

    const installRailMenu = ({ button, menu, onActivate, onClose = null }) => {
      if (!button || !menu) return null;
      const controller = createAnchoredMenuController({
        button,
        menu,
        itemSelector: ".rail-selector-menu-action",
        onActivate,
        onClose,
      });
      railPrintSetupMenuControllers.push(controller);
      return controller;
    };

    const handleMutationError = async (error, label) => {
      if (error?.status === 409 && error?.body?.detail?.error === "printer_revision_conflict") {
        await app.commands.loadPrinters();
        app.commands.showToast("Printer setup changed elsewhere. Review the latest values and try again.", "warning");
        return;
      }
      app.commands.showToast(`Failed to change ${label}: ${error.message}`, "error");
      app.commands.renderPrinterRail();
    };

    const printerMenu = app.state.ui.$("#railPrinterMenu");
    if (printers.length <= 1) {
      const printerName = printers.length
        ? (printers[0].name || app.state.session.printerConfig.name || "Unnamed printer")
        : "No printer configured";
      renderStaticValue({ target: container, value: printerName, label: printerName });
    } else {
      const activePrinter = printers.find(p => p.id === activeId) || printers[0];
      const printerButton = renderSelectorTrigger({
        target: container,
        id: "railPrinterButton",
        menuId: "railPrinterMenu",
        value: activePrinter?.name || "Active printer",
        label: "Active Printer",
      });
      printerMenu.innerHTML = printers.map(item => `
        <button class="rail-selector-option rail-selector-menu-action" type="button" role="menuitemradio"
                data-printer-id="${app.commands.escAttr(item.id)}" aria-checked="${item.id === activePrinter?.id ? "true" : "false"}">
          <span>${app.commands.esc(item.name || "Unnamed printer")}</span>
        </button>`).join("");
      installRailMenu({
        button: printerButton,
        menu: printerMenu,
        onActivate: async item => {
          printerButton.disabled = true;
          let active;
          try {
            active = await app.commands.selectActivePrintSetup({
              intent_kind: "select_printer",
              active_printer_id: item.dataset.printerId,
            });
          } catch (error) {
            await handleMutationError(error, "Printer");
            return;
          }
          if (!active) { app.commands.renderPrinterRail(); return; }
          try {
            app.commands.applyAuthoritativePrinterState(data, active);
            restoreRailFocus("#railPrinterButton");
            app.events.emit("printer.active-changed", {
              printerId: active.printer?.id || null,
              source: "sidebar",
            });
          } catch (error) {
            console.error("[printers] active printer could not be rendered:", error);
            app.commands.showToast(
              "Printer changed, but the display could not refresh. Reload Prisma.",
              "error",
            );
          }
        },
      });
    }

    // Nozzle and numeric Extrusion Width are independent active-print-setup controls.
    const printer = printers.find(p => p.id === activeId) || printers[0];
    const setup = data.printer_setup_state?.[printer?.id];
    const nozzles = printer?.nozzle_profiles || [];
    const nozzle = nozzles.find(item => item.id === setup?.active_nozzle_id) || nozzles[0];
    const setupMmLabel = valueUm => {
      const fixed = (Number(valueUm) / 1000).toFixed(3);
      return fixed.endsWith("0") ? fixed.slice(0, -1) : fixed;
    };
    const nozzleContainer = app.state.ui.$("#railNozzleSelector");
    const nozzleMenu = app.state.ui.$("#railNozzleMenu");
    if (nozzleContainer) {
      if (!nozzles.length) {
        renderStaticValue({ target: nozzleContainer, value: "No Nozzle Profile", label: "No Nozzle Profile" });
      } else {
        const nozzleButton = renderSelectorTrigger({
          target: nozzleContainer,
          id: "railNozzleButton",
          menuId: "railNozzleMenu",
          value: `${setupMmLabel(nozzle.diameter_um)} mm`,
          label: "Active Nozzle",
        });
        nozzleMenu.innerHTML = nozzles.map(item => `
          <button class="rail-selector-option rail-selector-menu-action" type="button" role="menuitemradio"
                  data-nozzle-id="${app.commands.escAttr(item.id)}" aria-checked="${item.id === nozzle?.id ? "true" : "false"}">
            <span>${setupMmLabel(item.diameter_um)} mm</span>
          </button>`).join("");
        installRailMenu({
          button: nozzleButton,
          menu: nozzleMenu,
          onActivate: async item => {
            nozzleButton.disabled = true;
            let active;
            try {
              active = await app.commands.selectActivePrintSetup({
                intent_kind: "select_nozzle",
                active_printer_id: printer.id,
                active_nozzle_id: item.dataset.nozzleId,
              });
            } catch (error) {
              await handleMutationError(error, "Nozzle");
              return;
            }
            if (!active) { app.commands.renderPrinterRail(); return; }
            app.commands.applyAuthoritativePrinterState(data, active);
            restoreRailFocus("#railNozzleButton");
            app.events.emit("printer.nozzle-changed", {
              printerId: active.printer?.id || null,
              nozzleId: active.nozzle?.id || null,
              nozzleDiameterMm: Number(active.nozzle?.diameter_um) / 1000,
            });
          },
        });
      }
    }

    const picker = app.state.ui.$("#railExtrusionWidthPicker");
    const widthMenu = app.state.ui.$("#railExtrusionWidthMenu");
    if (picker && widthMenu) {
      const widthState = setup?.nozzle_width_state?.[nozzle?.id];
      const currentWidthUm = widthState?.current_width_um;
      const savedWidths = widthState?.saved_widths_um || [];
      const widthLabel = widthUm => `${setupMmLabel(widthUm)} mm`;
      const widthButton = renderSelectorTrigger({
        target: picker,
        id: "railExtrusionWidthButton",
        menuId: "railExtrusionWidthMenu",
        value: currentWidthUm ? widthLabel(currentWidthUm) : "No Extrusion Width",
        label: "Active Extrusion Width",
        disabled: !nozzle || !widthState,
      });
      if (!nozzle || !widthState) return;
      widthMenu.innerHTML = `
        <div class="rail-selector-options">
          ${savedWidths.map(widthUm => `
            <div class="rail-selector-row" data-width-um="${widthUm}">
              <button class="rail-selector-option rail-selector-menu-action" type="button" role="menuitemradio"
                      data-width-um="${widthUm}" aria-checked="${widthUm === currentWidthUm ? "true" : "false"}">
                <span>${app.commands.esc(widthLabel(widthUm))}</span>
              </button>
              <button class="rail-selector-remove" type="button" data-width-um="${widthUm}" aria-label="Remove ${app.commands.escAttr(widthLabel(widthUm))} from saved widths">${app.commands.xIconSvg("icon-x rail-selector-remove-icon")}</button>
            </div>`).join("")}
        </div>
        <div class="rail-selector-add-row">
          <button class="rail-selector-option rail-selector-add rail-selector-menu-action" type="button" role="menuitem"
                  data-action="add-width" data-menu-stay-open="true"><span>Add New</span></button>
          <div class="rail-selector-add-form is-hidden">
            <div class="rail-selector-add-controls">
              <input id="railWidthNewValue" type="text" inputmode="decimal" autocomplete="off"
                     aria-label="New Extrusion Width in millimeters"><span>mm</span>
              <button type="button" class="rail-selector-add-save">Add</button>
            </div>
          </div>
        </div>`;

      const options = widthMenu.querySelector(".rail-selector-options");
      const addButton = widthMenu.querySelector(".rail-selector-add");
      const addForm = widthMenu.querySelector(".rail-selector-add-form");
      const addInput = widthMenu.querySelector("#railWidthNewValue");

      const showWidthList = ({ focus = false } = {}) => {
        options.classList.remove("is-hidden");
        addButton.classList.remove("is-hidden");
        addForm.classList.add("is-hidden");
        addInput.removeAttribute("aria-invalid");
        if (focus) addButton.focus();
      };

      const selectWidth = async requestedWidthUm => {
        let active;
        try {
          active = await app.commands.selectActivePrintSetup({
            intent_kind: "select_extrusion_width",
            active_printer_id: printer.id,
            active_nozzle_id: nozzle.id,
            current_width_um: requestedWidthUm,
          });
        } catch (error) {
          await handleMutationError(error, "Extrusion Width");
          return;
        }
        if (!active) { app.commands.renderPrinterRail(); return; }
        app.commands.applyAuthoritativePrinterState(data, active);
        restoreRailFocus("#railExtrusionWidthButton");
        app.events.emit("printer.extrusion-width-changed", {
          printerId: active.printer?.id || null,
          extrusionWidthUm: active.extrusion_width?.width_um || null,
          extrusionWidthMm: Number(active.extrusion_width?.width_um) / 1000,
          nozzleDiameterMm: Number(active.nozzle?.diameter_um) / 1000,
        });
      };

      const showAddWidth = () => {
        addButton.classList.add("is-hidden");
        addForm.classList.remove("is-hidden");
        addInput.value = currentWidthUm ? Number(currentWidthUm) / 1000 : Number(nozzle.diameter_um) / 1000;
        addInput.removeAttribute("aria-invalid");
        widthController.position();
        addInput.focus();
        addInput.select();
      };

      const widthController = installRailMenu({
        button: widthButton,
        menu: widthMenu,
        onActivate: item => {
          if (item.dataset.action === "add-width") showAddWidth();
          else void selectWidth(Number(item.dataset.widthUm));
        },
        onClose: () => showWidthList(),
      });

      widthMenu.querySelectorAll(".rail-selector-remove").forEach(button => {
        button.onclick = async () => {
          if (button.dataset.confirm !== "true") {
            widthMenu.querySelectorAll(".rail-selector-remove").forEach(item => {
              item.dataset.confirm = "false";
              item.innerHTML = app.commands.xIconSvg("icon-x rail-selector-remove-icon");
              item.classList.remove("is-confirming");
            });
            button.dataset.confirm = "true";
            button.textContent = "?";
            button.classList.add("is-confirming");
            button.setAttribute("aria-label", `Confirm removal of ${widthLabel(Number(button.dataset.widthUm))}`);
            setTimeout(() => {
              if (!button.isConnected || button.dataset.confirm !== "true") return;
              button.dataset.confirm = "false";
              button.innerHTML = app.commands.xIconSvg("icon-x rail-selector-remove-icon");
              button.classList.remove("is-confirming");
              button.setAttribute("aria-label", `Remove ${widthLabel(Number(button.dataset.widthUm))} from saved widths`);
            }, 2000);
            return;
          }
          try {
            const active = await app.commands.removePrinterWidthShortcut({
              active_printer_id: printer.id,
              active_nozzle_id: nozzle.id,
              width_um: Number(button.dataset.widthUm),
            });
            app.commands.applyAuthoritativePrinterState(data, active);
            restoreRailFocus("#railExtrusionWidthButton");
          } catch (error) {
            await handleMutationError(error, "saved Extrusion Widths");
          }
        };
      });
      const submitWidth = async () => {
        const rawWidth = addInput.value.trim();
        const numeric = Number(rawWidth);
        const widthUm = Math.round(numeric * 1000);
        const exact = /^(?:\d+(?:\.\d{1,3})?|\.\d{1,3})$/.test(rawWidth)
          && Number.isFinite(numeric)
          && numeric > 0
          && Math.abs(numeric - widthUm / 1000) <= 5e-7;
        if (!exact || widthUm < nozzle.diameter_um || widthUm > nozzle.max_extrusion_width_um) {
          addInput.setAttribute("aria-invalid", "true");
          app.commands.showToast(
            `Enter an Extrusion Width from ${widthLabel(nozzle.diameter_um)} through ${widthLabel(nozzle.max_extrusion_width_um)}, with no more than three decimal places.`,
            "error",
          );
          addInput.focus();
          return;
        }
        try {
          const active = await app.commands.addPrinterWidthShortcut({
            intent_kind: "add_and_select_extrusion_width",
            active_printer_id: printer.id,
            active_nozzle_id: nozzle.id,
            width_um: widthUm,
          });
          if (active) {
            app.commands.applyAuthoritativePrinterState(data, active);
            restoreRailFocus("#railExtrusionWidthButton");
          }
          else app.commands.renderPrinterRail();
        } catch (error) {
          await handleMutationError(error, "Extrusion Width");
        }
      };
      widthMenu.querySelector(".rail-selector-add-save").onclick = submitWidth;
      addInput.oninput = () => addInput.removeAttribute("aria-invalid");
      addInput.onkeydown = event => {
        if (event.key === "Enter") { event.preventDefault(); submitWidth(); }
        if (event.key === "Escape") {
          event.preventDefault();
          event.stopPropagation();
          showWidthList({ focus: true });
          widthController.position();
        }
      };
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
    minimumCapThicknessMm,
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
