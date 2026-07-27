/**
 * Install the solve/controller feature commands.
 * @param {import("../../core/types.js").ApplicationContext} app
 */
export function installFeaturesSolveController(app) {
  const RESERVED_RUN_LABEL_RE = /^run\s+\d+$/i;
  const RESERVED_RUN_LABEL_MESSAGE = "Names in the form ‘Run 7’ are reserved for automatic run labels.";

  function validateWritableRunLabel(value) {
    const label = String(value || "").trim();
    if (!label) return "Run name cannot be empty.";
    if (RESERVED_RUN_LABEL_RE.test(label)) return RESERVED_RUN_LABEL_MESSAGE;
    return "";
  }

  function initialSaveRunLabel(run) {
    const label = String(run?.label || "").trim();
    return RESERVED_RUN_LABEL_RE.test(label) ? "" : label;
  }

  function isCardInteractionTarget(target) {
    return Boolean(target?.closest?.("button, a, input, select, textarea, [role='button']"));
  }

  function isActivePendingSolveRun(run) {
    return !!run
      && !run.results
      && run.id === app.state.solve.activeSolveRunId
      && app.state.solve.solveStatus.status === "running";
  }

  function getSolveRunDeleteBlockReason(run) {
    if (app.commands.isActivePendingSolveRun(run)) {
      return "Cancel this solve before removing its card";
    }
    if (run && app.state.export.exportRunning && run.id === app.state.export.activeExportRunId) {
      return "Cancel this export before removing its source run";
    }
    return "";
  }

  function buildSolveRunDeleteButton(run) {
    const blockReason = app.commands.getSolveRunDeleteBlockReason(run);
    const armed = !blockReason && app.state.solve.solveRunDeleteArmedId === run.id;
    const label = armed ? "Click again to delete this run" : (blockReason || "Delete this run");
    return `<span class="solve-run-delete-slot"><button class="solve-run-delete-btn compact-deck-card-remove ghost-button xxs${armed ? " confirm-pending" : ""}" data-run-id="${app.commands.escAttr(run.id)}" title="${app.commands.escAttr(label)}" aria-label="${app.commands.escAttr(label)}"${blockReason ? " disabled aria-disabled=\"true\"" : ""}>${armed ? "!" : app.commands.xIconSvg()}</button></span>`;
  }

  function buildSolveRunSupportChipsHtml(run) {
    const snapshot = app.commands.getSolveRunSettingsSnapshot?.(run) || run?.config || {};
    const baseId = snapshot.base_filament
      || snapshot.white_base
      || app.state.session.DEFAULT_BASE_FILAMENT;
    const configuredCapId = snapshot.cap_filament || snapshot.white_cap || "__same__";
    const capId = !configuredCapId || configuredCapId === "__same__" ? baseId : configuredCapId;
    const entries = [];
    if (baseId) entries.push({ id: baseId, role: baseId === capId ? "White Base/Cap" : "White Base" });
    if (capId && capId !== baseId) entries.push({ id: capId, role: "White Cap" });
    if (!entries.length) return "";

    const slots = entries.map((entry) => {
      const filament = app.commands.filamentById(entry.id);
      const filamentName = filament
        ? [filament.manufacturer, filament.color_name || filament.display_name]
          .filter(Boolean)
          .join(" ")
        : entry.id;
      const label = `${entry.role}: ${filamentName || "Unset"}`;
      const hex = filament?.hex || "#ccc";
      const lightClass = app.commands.isLightHex?.(hex) ? " is-light" : "";
      return `<span class="deck-support-slot is-filled${lightClass}" title="${app.commands.escAttr(label)}" aria-label="${app.commands.escAttr(label)}">
        <span class="color-chip deck-support-chip" style="background:${hex}"></span>
      </span>`;
    }).join("");
    const distinctClass = entries.length > 1 ? " has-distinct-cap" : "";
    return `<div class="deck-support-tray solve-run-support-tray${distinctClass}" aria-label="White base and cap filament">${slots}</div>`;
  }

  function renderSolveRunDeleteState() {
    app.commands.renderSolveRunSidebar();
    if (app.state.ui.currentTab === "export") app.commands.renderExportRunSidebar();
  }

  function resetSolveRunDeleteConfirm({ render = true } = {}) {
    if (app.state.solve.solveRunDeleteConfirmTimer) {
      clearTimeout(app.state.solve.solveRunDeleteConfirmTimer);
      app.state.solve.solveRunDeleteConfirmTimer = null;
    }
    const changed = app.state.solve.solveRunDeleteArmedId !== null;
    app.state.solve.solveRunDeleteArmedId = null;
    if (render && changed) app.commands.renderSolveRunDeleteState();
  }

  function armSolveRunDeleteConfirm(runId) {
    if (!runId) return;
    if (app.state.solve.solveRunDeleteConfirmTimer) clearTimeout(app.state.solve.solveRunDeleteConfirmTimer);
    app.state.solve.solveRunDeleteArmedId = runId;
    app.state.solve.solveRunDeleteConfirmTimer = setTimeout(() => {
      app.state.solve.solveRunDeleteConfirmTimer = null;
      app.state.solve.solveRunDeleteArmedId = null;
      app.commands.renderSolveRunDeleteState();
    }, 3000);
    app.commands.renderSolveRunDeleteState();
  }

  function handleSolveRunDeleteClick(runId) {
    const run = app.state.solve.solveRuns.find((candidate) => candidate.id === runId);
    if (!run || app.commands.getSolveRunDeleteBlockReason(run)) return false;
    if (app.state.solve.solveRunDeleteArmedId !== runId) {
      app.commands.armSolveRunDeleteConfirm(runId);
      return false;
    }
    app.commands.resetSolveRunDeleteConfirm({ render: false });
    return app.commands.deleteSolveRun(runId);
  }

  function deleteSolveRun(runId, { force = false } = {}) {
    const run = app.state.solve.solveRuns.find(r => r.id === runId);
    if (!force && app.commands.isActivePendingSolveRun(run)) {
      app.commands.showToast("Cancel the solve before removing its pending card.", "warn");
      return false;
    }
    if (!force && app.state.export.exportRunning && runId === app.state.export.activeExportRunId) {
      app.commands.showToast("Cancel the export before removing its source run.", "warn");
      return false;
    }
    const idx = app.state.solve.solveRuns.findIndex(r => r.id === runId);
    if (idx === -1) return false;
    app.commands.resetSolveRunDeleteConfirm({ render: false });
    app.state.solve.solveRuns.splice(idx, 1);
    app.state.solve.selectedRunIds.delete(runId);
    if (app.state.export.exportSelectedRunId === runId) {
      app.state.export.exportSelectedRunId = null;
    }
    if (app.state.solve.solveRunHoverRunId === runId) app.commands.hideSolveRunHoverPreview();
    if (app.state.solve.solveRunSettingsPanelRunId === runId) app.commands.hideSolveRunSettingsPanel();
    app.commands.invalidateSolveRunCaches(run);
    app.commands.renderSolveTab();
    if (app.state.ui.currentTab === "export") app.commands.renderExportTab();
    return true;
  }

  function removePendingSolveRun(runId) {
    if (!runId) return false;
    const run = app.state.solve.solveRuns.find(r => r.id === runId);
    if (!run || run.results) return false;
    app.commands.deleteSolveRun(runId, { force: true });
    return true;
  }

  function clearSolveHistory({ force = false } = {}) {
    if (!force && (app.state.solve.solveStatus.status === "running" || app.state.export.exportRunning)) {
      app.commands.showToast("Wait for the active solve or export to finish before clearing history.", "warn");
      return;
    }
    app.commands.resetSolveRunDeleteConfirm({ render: false });
    app.state.solve.solveRuns.forEach((run) => app.commands.invalidateSolveRunCaches(run));
    app.state.solve.solveRuns = [];
    app.state.solve.solveRunCounter = 0;
    app.state.solve.solveStatus = { status: "idle", progress: "", elapsed_s: 0, result: null };
    if (force) {
      app.state.solve.activeSolveRunId = null;
      app.state.solve.activeSolveJobId = null;
      app.state.solve.solveStartPending = false;
      app.state.solve.solveCancelPending = false;
      app.state.export.exportRunning = false;
      app.state.export.exportPollingOwner = null;
      app.state.export.activeExportRunId = null;
      app.state.export.activeExportJobId = "";
      app.state.export.exportCancelPending = false;
    }
    app.state.solve.selectedRunIds.clear();
    app.state.export.exportSelectedRunId = null;
    app.commands.renderSolveTab();
    if (app.state.ui.currentTab === "export") app.commands.renderExportTab();
  }

  function getSolveHistoryClearButtons() {
    return ["clearSolveHistoryBtn", "exportClearSolveHistoryBtn"]
      .map(id => app.state.ui.$(`#${id}`))
      .filter(Boolean);
  }

  function isSolveHistoryClearDisabled() {
    return !app.state.solve.solveRuns.length
      || app.state.solve.solveStatus.status === "running"
      || app.state.export.exportRunning;
  }

  function syncSolveHistoryClearButtons() {
    const disabled = app.commands.isSolveHistoryClearDisabled();
    if (disabled && app.state.solve.solveHistoryClearConfirmPending) {
      if (app.state.solve.solveHistoryClearConfirmTimer) {
        clearTimeout(app.state.solve.solveHistoryClearConfirmTimer);
        app.state.solve.solveHistoryClearConfirmTimer = null;
      }
      app.state.solve.solveHistoryClearConfirmPending = false;
    }
    const armed = app.state.solve.solveHistoryClearConfirmPending;
    const title = disabled
      ? (!app.state.solve.solveRuns.length
          ? "No solve runs to clear"
          : "Wait for the active solve or export to finish before clearing history")
      : armed
        ? "Click again to clear all solve runs"
        : "Clear all solve runs";
    const accessibleLabel = disabled
      ? title
      : armed
        ? "Confirm clearing all solve runs"
        : "Clear all solve runs";
    app.commands.getSolveHistoryClearButtons().forEach((btn) => {
      btn.textContent = armed ? "Clear?" : "Clear";
      btn.classList.toggle("confirm-pending", armed);
      btn.title = title;
      btn.setAttribute("aria-label", accessibleLabel);
      btn.disabled = disabled;
      btn.setAttribute("aria-disabled", disabled ? "true" : "false");
    });
  }

  function resetSolveHistoryClearConfirm() {
    if (app.state.solve.solveHistoryClearConfirmTimer) {
      clearTimeout(app.state.solve.solveHistoryClearConfirmTimer);
      app.state.solve.solveHistoryClearConfirmTimer = null;
    }
    app.state.solve.solveHistoryClearConfirmPending = false;
    app.commands.syncSolveHistoryClearButtons();
  }

  function armSolveHistoryClearConfirm() {
    if (app.commands.isSolveHistoryClearDisabled()) {
      app.commands.syncSolveHistoryClearButtons();
      return;
    }
    if (app.state.solve.solveHistoryClearConfirmTimer) {
      clearTimeout(app.state.solve.solveHistoryClearConfirmTimer);
    }
    app.state.solve.solveHistoryClearConfirmPending = true;
    app.commands.syncSolveHistoryClearButtons();
    app.state.solve.solveHistoryClearConfirmTimer = setTimeout(app.commands.resetSolveHistoryClearConfirm, 3000);
  }

  function handleSolveHistoryClearClick() {
    if (app.commands.isSolveHistoryClearDisabled()) {
      app.commands.syncSolveHistoryClearButtons();
      return;
    }
    if (!app.state.solve.solveHistoryClearConfirmPending) {
      app.commands.armSolveHistoryClearConfirm();
      return;
    }
    app.commands.resetSolveHistoryClearConfirm();
    app.commands.clearSolveHistory();
  }

  function renderSolveRunSidebar() {
    const container = app.state.ui.$("#solveRunCards");
    if (!container) return;
    app.commands.hideSolveRunHoverPreview();
    container.setAttribute("role", "listbox");
    container.setAttribute("aria-label", "Solve history");
    container.setAttribute("aria-multiselectable", "true");
    app.commands.syncSolveHistoryClearButtons();

    if (app.state.solve.solveRuns.length === 0) {
      container.innerHTML = `<p class="muted-line" id="solveRunEmpty">No solves yet</p>`;
      return;
    }

    let html = "";
    for (let i = app.state.solve.solveRuns.length - 1; i >= 0; i--) {
      const run = app.state.solve.solveRuns[i];
      const isSelected = app.state.solve.selectedRunIds.has(run.id);
      const chips = (run.palette || []).map(fid => {
        const fil = app.state.session.allFilaments.find(f => f.filament_id === fid);
        const hex = fil?.hex || "#888";
        const label = fil?.color_name || fil?.display_name || fid;
        return `<span class="comp-deck-chip color-chip" style="background:${hex}" title="${app.commands.escAttr(label)}"></span>`;
      }).join("");
      const supportChips = app.commands.buildSolveRunSupportChipsHtml(run);

      const stats = run.results
        ? `<span class="solve-run-card-rmse">${app.commands.formatSolveRunCardRmse(run.results)}</span>`
        : `<span class="solve-run-card-rmse is-pending">solving...</span>`;
      const saveDisabled = !run.results || run.save_pending;
      const saveTitle = !run.results
        ? "Save is available after this solve completes"
        : run.save_pending
          ? "Saving this run"
          : "Save this run to a portable archive";
      html += `<div class="solve-run-card compact-deck-card ${isSelected ? "is-selected" : ""}" data-run-id="${app.commands.escAttr(run.id)}" tabindex="0" role="option" aria-selected="${isSelected ? "true" : "false"}">
        <div class="solve-run-card-header compact-deck-card-header">
          <span class="solve-run-label compact-deck-card-title" title="${app.commands.escAttr(run.label)}">${app.commands.esc(run.label)}</span>
          <div class="solve-run-card-actions compact-deck-card-actions">
            <button class="solve-run-save-btn compact-deck-card-save ghost-button xxs" data-run-id="${app.commands.escAttr(run.id)}" title="${app.commands.escAttr(saveTitle)}"${saveDisabled ? " disabled aria-disabled=\"true\"" : ""}${run.save_pending ? " aria-busy=\"true\"" : ""}>Save</button>
            ${app.commands.buildSolveRunDeleteButton(run)}
          </div>
        </div>
        <div class="comp-deck-card-chips solve-run-card-chips rail-deck-card-chips">
          <div class="solve-run-palette-chips">${chips}</div>
          ${supportChips}
        </div>
        <div class="solve-run-card-meta">
          <button class="solve-run-settings-btn" data-run-id="${app.commands.esc(run.id)}" title="View the settings captured for this run">Settings</button>
          ${stats}
        </div>
      </div>`;
    }
    container.innerHTML = html;

    const toggleRunSelection = (el) => {
      const runId = el.dataset.runId;
      if (app.state.solve.selectedRunIds.has(runId)) app.state.solve.selectedRunIds.delete(runId);
      else app.state.solve.selectedRunIds.add(runId);
      app.commands.renderSolveRunSidebar();
      app.commands.renderSolveComparisonGrid();
    };
    container.querySelectorAll(".solve-run-card").forEach(el => {
      el.addEventListener("click", (e) => {
        if (app.commands.isCardInteractionTarget(e.target)) return;
        toggleRunSelection(el);
      });
      el.addEventListener("keydown", (e) => {
        if (!["Enter", " "].includes(e.key) || app.commands.isCardInteractionTarget(e.target)) return;
        e.preventDefault();
        toggleRunSelection(el);
      });
    });

    container.querySelectorAll(".solve-run-delete-btn").forEach(btn => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        if (btn.disabled) return;
        app.commands.handleSolveRunDeleteClick(btn.dataset.runId);
      });
    });

    container.querySelectorAll(".solve-run-save-btn").forEach(btn => {
      btn.addEventListener("click", async (e) => {
        e.stopPropagation();
        const run = app.state.solve.solveRuns.find(r => r.id === btn.dataset.runId);
        if (!run?.results || run.save_pending) return;
        const label = await app.commands.appPrompt(
          "Save this run as:",
          app.commands.initialSaveRunLabel(run),
          { title: "Save Run", validate: app.commands.validateWritableRunLabel },
        );
        if (label == null) return;
        await app.commands.saveSolveRun(btn.dataset.runId, label, btn);
      });
    });

    app.commands.bindSolveRunCardAuxiliaryInteractions(container, "preview");
  }

  async function saveSolveRun(runId, label, initiatingButton = null) {
    const run = app.state.solve.solveRuns.find((candidate) => candidate.id === runId);
    const validationError = app.commands.validateWritableRunLabel(label);
    if (!run?.results || run.save_pending || validationError) return false;
    const trimmed = String(label).trim();
    run.save_pending = true;
    if (initiatingButton) {
      initiatingButton.disabled = true;
      initiatingButton.setAttribute("aria-disabled", "true");
      initiatingButton.setAttribute("aria-busy", "true");
      initiatingButton.title = "Saving this run";
    }
    try {
      const response = await app.api.saveRun(runId, trimmed);
      const serverLabel = String(response?.label ?? "").trim();
      if (!serverLabel) throw new Error("Save response did not include a run label.");
      const currentRun = app.state.solve.solveRuns.find((candidate) => candidate.id === runId);
      if (currentRun) {
        const previousLabel = currentRun.label;
        currentRun.label = serverLabel;
        currentRun.save_pending = false;
        app.commands.renderSolveTab();
        if (app.state.ui.currentTab === "export") app.commands.renderExportTab();
        if (app.state.solve.solveRunSettingsPanelRunId === runId && app.state.solve.solveRunSettingsPanelEl) {
          app.commands.renderSolveRunSettingsPanel();
        }
        app.commands.refreshOpenSolveRunLabels(currentRun, previousLabel);
      }
      app.commands.showToast(`Saved as "${serverLabel}"`, "");
      return true;
    } catch (err) {
      const currentRun = app.state.solve.solveRuns.find((candidate) => candidate.id === runId);
      if (currentRun) {
        currentRun.save_pending = false;
        app.commands.renderSolveTab();
        if (app.state.ui.currentTab === "export") app.commands.renderExportTab();
      }
      if (initiatingButton?.isConnected !== false) {
        initiatingButton.disabled = false;
        initiatingButton.removeAttribute("aria-disabled");
        initiatingButton.removeAttribute("aria-busy");
        initiatingButton.title = "Save this run to a portable archive";
      }
      app.commands.showToast(err.message, "error");
      return false;
    }
  }

  function refreshOpenSolveRunLabels(run, previousLabel = "") {
    if (!run || app.state.solve._solveLightboxState?.runId !== run.id) return;
    const content = app.state.ui.$("#compLightboxContent");
    if (!content) return;
    const title = content.querySelector(".comp-lightbox-runtitle");
    if (title) title.textContent = run.label;
    if (!previousLabel) return;
    content.querySelectorAll("[aria-label]").forEach((element) => {
      const label = element.getAttribute("aria-label") || "";
      if (label === previousLabel || label.startsWith(`${previousLabel} `)) {
        element.setAttribute("aria-label", `${run.label}${label.slice(previousLabel.length)}`);
      }
    });
  }

  function _setSavedRunsModalOpen(open) {
    const modal = app.state.ui.$("#savedRunsModal");
    if (!modal) return;
    modal.classList.toggle("is-hidden", !open);
    modal.setAttribute("aria-hidden", open ? "false" : "true");
    if (!open) {
      app.commands.resetSavedRunDeleteConfirm();
      app.state.solve.savedRunsModalMode = "run";
    }
  }

  async function openSavedRunsModal(mode = "run") {
    app.state.solve.savedRunsModalMode = mode === "settings" ? "settings" : "run";
    const title = app.state.ui.$("#savedRunsModalTitle");
    if (title) title.textContent = app.state.solve.savedRunsModalMode === "settings"
      ? "Load Settings from Saved Run"
      : "Saved Runs";
    const uploadLabel = app.state.ui.$("#savedRunUploadLabel");
    if (uploadLabel) {
      // The upload endpoint rehydrates a whole run (image/cache/history card),
      // so do not expose it in settings-only mode where that side effect would
      // contradict the action's contract.
      uploadLabel.hidden = app.state.solve.savedRunsModalMode === "settings";
    }
    app.commands._setSavedRunsModalOpen(true);
    await app.commands.refreshSavedRunRows();
  }

  function savedRunKey(save) {
    if (!save) return null;
    const tier = save.tier === "auto" ? "auto" : "saved";
    return `${tier}:${save.save_id}`;
  }

  function getSelectedSavedRun() {
    return app.state.solve.savedRunRowsCache.find(save => app.commands.savedRunKey(save) === app.state.ui.selectedSavedRunKey) || null;
  }

  function savedRunTierLabel(save) {
    return save?.tier === "auto" ? "Autosave" : "Saved";
  }

  function formatSavedRunTimestamp(value) {
    const raw = String(value || "").trim();
    if (!raw) return "";
    const compact = raw.match(/^(\d{4})(\d{2})(\d{2})[-_ ]?(\d{2})(\d{2})(\d{2})$/);
    if (compact) {
      return `${compact[1]}-${compact[2]}-${compact[3]} ${compact[4]}:${compact[5]}`;
    }
    const iso = raw.match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/);
    if (iso) {
      return `${iso[1]}-${iso[2]}-${iso[3]} ${iso[4]}:${iso[5]}`;
    }
    return raw;
  }

  function savedRunDownloadUrl(save) {
    if (!save) return "";
    const tier = save.tier === "auto" ? "auto" : "saved";
    return tier === "auto"
      ? `/api/runs/auto/${encodeURIComponent(save.save_id)}/download`
      : `/api/runs/saved/${encodeURIComponent(save.save_id)}/download`;
  }

  async function loadSettingsFromSavedRun(save) {
    if (!save) return false;
    try {
      const body = await app.api.loadSavedRunSettings(save.save_id, save.tier === "auto" ? "auto" : "saved");
      const loaded = await app.commands._loadTemporarySettingsFromRun(body, {
        kind: "saved-run",
        save_id: save.save_id,
        tier: save.tier === "auto" ? "auto" : "saved",
        label: save.label || body.label || save.save_id,
      });
      if (loaded) app.commands._setSavedRunsModalOpen(false);
      return loaded;
    } catch (error) {
      app.commands.showToast(`Settings could not be loaded: ${error.message}`, "error");
      return false;
    }
  }

  async function activateSelectedSavedRun() {
    const selected = app.commands.getSelectedSavedRun();
    if (!selected) return;
    if (app.state.solve.savedRunsModalMode === "settings") {
      await app.commands.loadSettingsFromSavedRun(selected);
    } else {
      await app.commands.onLoadSavedRun(selected.save_id, selected.tier);
    }
  }

  function resetSavedRunDeleteConfirm() {
    if (app.state.solve.savedRunDeleteConfirmTimer) {
      clearTimeout(app.state.solve.savedRunDeleteConfirmTimer);
      app.state.solve.savedRunDeleteConfirmTimer = null;
    }
    app.state.solve.savedRunDeleteConfirmPending = false;
    const delBtn = app.state.ui.$("#savedRunDeleteBtn");
    if (delBtn) {
      delBtn.textContent = "Delete";
      delBtn.classList.remove("confirm-pending");
      delBtn.title = "Delete selected run";
    }
  }

  function updateSavedRunFooterActions() {
    const selected = app.commands.getSelectedSavedRun();
    const tier = selected?.tier === "auto" ? "auto" : "saved";
    const hasSelection = !!selected;
    const downloadBtn = app.state.ui.$("#savedRunDownloadBtn");
    const saveBtn = app.state.ui.$("#savedRunSaveBtn");
    const renameBtn = app.state.ui.$("#savedRunRenameBtn");
    const deleteBtn = app.state.ui.$("#savedRunDeleteBtn");
    const loadBtn = app.state.ui.$("#savedRunLoadBtn");
    const loadSettingsBtn = app.state.ui.$("#savedRunLoadSettingsBtn");

    if (downloadBtn) downloadBtn.disabled = !hasSelection;
    if (loadBtn) {
      loadBtn.disabled = !hasSelection;
      loadBtn.hidden = app.state.solve.savedRunsModalMode === "settings";
    }
    if (loadSettingsBtn) {
      loadSettingsBtn.disabled = !hasSelection;
      loadSettingsBtn.hidden = false;
    }
    if (saveBtn) {
      saveBtn.hidden = !hasSelection || tier !== "auto";
      saveBtn.disabled = !hasSelection || tier !== "auto";
    }
    if (renameBtn) {
      renameBtn.hidden = !hasSelection || tier !== "saved";
      renameBtn.disabled = !hasSelection || tier !== "saved";
    }
    if (deleteBtn) {
      deleteBtn.hidden = !hasSelection;
      deleteBtn.disabled = !hasSelection;
      deleteBtn.title = hasSelection
        ? `Delete selected ${tier === "auto" ? "autosave" : "saved run"}`
        : "Delete selected run";
      if (!hasSelection) app.commands.resetSavedRunDeleteConfirm();
    }
  }

  async function refreshSavedRunRows() {
    const rows = app.state.ui.$("#savedRunRows");
    if (!rows) return;
    rows.innerHTML = "";
    let saves = [];
    try { saves = await app.api.listSavedRuns(); }
    catch (e) { app.commands.showToast(e.message, "error"); return; }
    app.state.solve.savedRunRowsCache = saves.map(save => ({
      ...save,
      tier: save.tier === "auto" ? "auto" : "saved",
    }));
    if (!app.state.solve.savedRunRowsCache.some(save => app.commands.savedRunKey(save) === app.state.ui.selectedSavedRunKey)) {
      app.state.ui.selectedSavedRunKey = app.state.solve.savedRunRowsCache.length ? app.commands.savedRunKey(app.state.solve.savedRunRowsCache[0]) : null;
    }
    app.commands.resetSavedRunDeleteConfirm();
    if (!app.state.solve.savedRunRowsCache.length) {
      rows.innerHTML = '<p class="muted-line saved-run-empty">No saved runs</p>';
      app.commands.updateSavedRunFooterActions();
      return;
    }
    for (const s of app.state.solve.savedRunRowsCache) {
      const tier = s.tier;
      const key = app.commands.savedRunKey(s);
      const isSelected = key === app.state.ui.selectedSavedRunKey;
      const row = document.createElement("div");
      row.className = `saved-run-row${isSelected ? " is-selected" : ""}`;
      row.dataset.savedRunKey = key;
      row.setAttribute("role", "option");
      row.setAttribute("aria-selected", isSelected ? "true" : "false");
      row.tabIndex = 0;
      const formattedSavedAt = app.commands.formatSavedRunTimestamp(s.saved_at);
      const previewUrl = app.api.savedRunPreviewUrl(s);
      row.innerHTML = `
        <div class="saved-run-preview" aria-hidden="true">
          ${previewUrl ? `<img src="${app.commands.escAttr(previewUrl)}" alt="" loading="lazy" onerror="this.closest('.saved-run-preview')?.classList.add('is-unavailable')">` : ""}
          <span class="saved-run-preview-placeholder">Preview</span>
        </div>
        <div class="saved-run-main">
          <span class="saved-run-label">${app.commands.esc(s.label || s.save_id)}</span>
          <span class="saved-run-tier">${app.commands.esc(app.commands.savedRunTierLabel(s))}</span>
        </div>
        <div class="saved-run-meta">
          <span class="saved-run-source">${app.commands.esc(s.source_image_name || "Unknown source")}</span>
          <span class="saved-run-date" title="${app.commands.esc(s.saved_at || "")}">${app.commands.esc(formattedSavedAt)}</span>
        </div>
      `;
      const select = () => {
        app.state.ui.selectedSavedRunKey = key;
        app.commands.resetSavedRunDeleteConfirm();
        app.commands.refreshSavedRunSelection();
      };
      row.addEventListener("click", select);
      row.addEventListener("dblclick", app.commands.activateSelectedSavedRun);
      row.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          app.commands.activateSelectedSavedRun();
          return;
        }
        if (e.key === " " || e.key === "Spacebar") {
          e.preventDefault();
          select();
        }
      });
      rows.appendChild(row);
    }
    app.commands.updateSavedRunFooterActions();
  }

  function refreshSavedRunSelection() {
    document.querySelectorAll(".saved-run-row[data-saved-run-key]").forEach(row => {
      const selected = row.dataset.savedRunKey === app.state.ui.selectedSavedRunKey;
      row.classList.toggle("is-selected", selected);
      row.setAttribute("aria-selected", selected ? "true" : "false");
    });
    app.commands.updateSavedRunFooterActions();
  }

  async function promoteSelectedSavedRun() {
    const selected = app.commands.getSelectedSavedRun();
    if (!selected || selected.tier !== "auto") return;
    try {
      const promoted = await app.api.promoteAutoRun(selected.save_id);
      app.state.ui.selectedSavedRunKey = app.commands.savedRunKey(promoted);
      await app.commands.refreshSavedRunRows();
      app.commands.showToast("Saved run promoted", "");
    } catch (err) { app.commands.showToast(err.message, "error"); }
  }

  async function deleteSelectedSavedRun() {
    const selected = app.commands.getSelectedSavedRun();
    if (!selected) return;
    const tier = selected.tier === "auto" ? "auto" : "saved";
    const delBtn = app.state.ui.$("#savedRunDeleteBtn");
    if (app.state.solve.savedRunDeleteConfirmPending) {
      app.commands.resetSavedRunDeleteConfirm();
      try {
        if (tier === "auto") {
          await app.api.deleteAutoRun(selected.save_id);
        } else {
          await app.api.deleteSavedRun(selected.save_id);
        }
        app.state.ui.selectedSavedRunKey = null;
        await app.commands.refreshSavedRunRows();
      } catch (err) { app.commands.showToast(err.message, "error"); }
      return;
    }
    app.state.solve.savedRunDeleteConfirmPending = true;
    if (delBtn) {
      delBtn.textContent = "Confirm?";
      delBtn.classList.add("confirm-pending");
      delBtn.title = `Click again to delete selected ${tier === "auto" ? "autosave" : "saved run"}`;
    }
    app.state.solve.savedRunDeleteConfirmTimer = setTimeout(app.commands.resetSavedRunDeleteConfirm, 1800);
  }

  function downloadSelectedSavedRun() {
    const selected = app.commands.getSelectedSavedRun();
    if (!selected) return;
    window.location.href = app.commands.savedRunDownloadUrl(selected);
  }

  function openRenameSavedRunDialog(save) {
    const overlay = app.state.ui.$("#renameSavedRunModal");
    const display = app.state.ui.$("#renameSavedRunDisplay");
    const diskName = app.state.ui.$("#renameSavedRunDiskName");
    const submit = app.state.ui.$("#renameSavedRunSubmit");
    const validation = app.state.ui.$("#renameSavedRunValidation");
    const cancelBtn = app.state.ui.$("#renameSavedRunCancelBtn");
    const closeBtn = app.state.ui.$("#renameSavedRunCancel");
    if (!overlay || !display || !diskName || !submit) return;
    display.value = save.label || "";
    diskName.value = save.save_id || "";
    const setValidation = (message = "") => {
      if (validation) {
        validation.textContent = message;
        validation.hidden = !message;
      }
      display.setAttribute("aria-invalid", message ? "true" : "false");
      display.setAttribute("aria-describedby", "renameSavedRunValidation");
    };
    setValidation();
    const setOpen = (open) => {
      overlay.classList.toggle("is-hidden", !open);
      overlay.setAttribute("aria-hidden", open ? "false" : "true");
    };
    const close = () => {
      setOpen(false);
      submit.onclick = null;
      display.oninput = null;
      if (cancelBtn) cancelBtn.onclick = null;
      if (closeBtn) closeBtn.onclick = null;
      overlay.onclick = null;
    };
    submit.onclick = async () => {
      const newLabel = display.value.trim();
      const validationError = app.commands.validateWritableRunLabel(newLabel);
      setValidation(validationError);
      if (validationError || submit.disabled) return;
      submit.disabled = true;
      submit.setAttribute("aria-busy", "true");
      try {
        await app.api.renameSavedRun(save.save_id, newLabel);
        await app.commands.refreshSavedRunRows();
        close();
      } catch (e) {
        setValidation(e.message);
        app.commands.showToast(e.message, "error");
      } finally {
        submit.disabled = false;
        submit.removeAttribute("aria-busy");
      }
    };
    display.oninput = () => setValidation();
    if (cancelBtn) cancelBtn.onclick = close;
    if (closeBtn) closeBtn.onclick = close;
    overlay.onclick = (e) => { if (e.target === overlay) close(); };
    setOpen(true);
    setTimeout(() => { display.focus(); display.select(); }, 50);
  }

  async function onLoadSavedRun(saveId, tier = "saved") {
    try {
      const body = await app.api.loadSavedRun(saveId, tier);
      await app.commands.applyLoadedRun(body);
      app.commands._setSavedRunsModalOpen(false);
      app.commands.showToast("Loaded saved run", "");
    } catch (e) { app.commands.showToast(e.message, "error"); }
  }

  async function restoreLoadedRunPaletteToDeck(body, support) {
    const cfg = body.config || {};
    const filamentIds = app.commands.selectLoadedPalette(body, cfg);
    if (!filamentIds.length) return null;

    if (!app.state.palette.savedPalettesData) {
      try {
        await app.commands.loadSavedPalettes();
      } catch {
        app.state.palette.savedPalettesData = app.state.palette.savedPalettesData || { palettes: [] };
      }
      if (!app.state.palette.savedPalettesData) app.state.palette.savedPalettesData = { palettes: [] };
    }

    const action = app.commands.chooseLoadedPaletteRestoreAction({
      filamentIds,
      support,
      deckCards: app.state.palette.deck,
      savedPalettes: app.state.palette.savedPalettesData?.palettes || [],
    });

    if (action.kind === "reuse-deck") {
      const existing = app.state.palette.deck.find(card => card.id === action.cardId);
      if (!existing) return null;
      app.commands.activateDeckCard(existing.id, { sync: false });
      existing.gamut = null;
      return existing;
    }

    if (action.kind === "load-saved") {
      return app.commands.loadPaletteByIndex(action.savedIndex, { forceActive: true, sync: false, silent: true, allowUnavailable: true });
    }

    if (action.kind === "add-ad-hoc") {
      return app.commands.addLoadedAdHocPaletteToDeck(action.filamentIds, body.label || "Loaded run palette");
    }

    return null;
  }

  function captureLoadedRunApplicationState() {
    return {
      solveRuns: [...app.state.solve.solveRuns],
      solveRunCounter: app.state.solve.solveRunCounter,
      selectedRunIds: new Set(app.state.solve.selectedRunIds),
      selectedImage: app.state.image.selectedImage,
      pendingSelectedFilename: app.state.image.pendingSelectedFilename,
      config: app.commands._cloneValue(app.state.settings.config),
      frameState: app.commands._cloneValue(app.state.image.frameState),
      imageAdjust: app.commands._cloneValue(app.state.image.imageAdjust),
      deck: app.commands._cloneValue(app.state.palette.deck),
      activeDeckId: app.state.palette.activeDeckId,
    };
  }

  function restoreLoadedRunApplicationState(previous) {
    app.state.solve.solveRuns = previous.solveRuns;
    app.state.solve.solveRunCounter = previous.solveRunCounter;
    app.state.solve.selectedRunIds = previous.selectedRunIds;
    app.state.image.selectedImage = previous.selectedImage;
    app.state.image.pendingSelectedFilename = previous.pendingSelectedFilename;
    for (const key of Object.keys(app.state.settings.config)) delete app.state.settings.config[key];
    Object.assign(app.state.settings.config, previous.config);
    for (const key of Object.keys(app.state.image.frameState)) delete app.state.image.frameState[key];
    Object.assign(app.state.image.frameState, previous.frameState);
    for (const key of Object.keys(app.state.image.imageAdjust)) delete app.state.image.imageAdjust[key];
    Object.assign(app.state.image.imageAdjust, previous.imageAdjust);
    app.state.palette.deck = previous.deck;
    app.state.palette.activeDeckId = previous.activeDeckId;
    app.commands.renderSettingsTab();
    app.commands.renderImageTab();
    app.commands.renderSolveTab();
    if (app.state.ui.currentTab === "export") app.commands.renderExportTab();
    app.commands.renderFrameCanvas();
    app.commands.updateRail();
  }

  async function applyLoadedRun(body) {
    if (app.state.session.clearTempRunning) {
      throw new Error("Wait for Clear Temp Files to finish before loading a saved run.");
    }
    if (app.state.solve.loadedRunApplyRunning) {
      throw new Error("Another saved run is still loading.");
    }
    const previous = app.commands.captureLoadedRunApplicationState();
    app.state.solve.loadedRunApplyRunning = true;
    try {
      const cfg = body.config || {};
    const loadedPalette = app.commands.selectLoadedPalette(body, cfg);
    const loadedSupport = app.commands.normalizeSupportFromLoadedConfig(cfg);
    const normalizedConfig = {
      ...app.commands._cloneValue(cfg),
      palette: [...loadedPalette],
      base_filament: loadedSupport.base_filament,
      cap_filament: loadedSupport.cap_filament,
      white_base: loadedSupport.white_base,
      white_cap: loadedSupport.white_cap,
    };
    // 1. New history card (fresh server card_id; never clobbers an existing run).
    app.state.solve.solveRunCounter++;
    const loadedLabel = String(body.label || "").trim();
    app.state.solve.solveRuns.push({
      id: body.card_id,
      label: loadedLabel || `Loaded ${app.state.solve.solveRunCounter}`,
      image: body.source_image
        ? app.commands._cloneValue(body.source_image)
        : normalizedConfig.image_path
          ? { filename: normalizedConfig.image_path }
          : null,
      palette: [...loadedPalette],
      config: app.commands._cloneValue(normalizedConfig),
      ar: app.commands.getEffectiveAR(),
      profile_ref: null,
      profile_name_at_solve: null,
      is_profile_modified_at_solve: false,
      recipe_snapshot: null,
      results: body.result || null,
      exportRecords: [],
      selectedExportId: null,
      timestamp: Date.now(),
    });
    app.state.solve.selectedRunIds.clear();
    app.state.solve.selectedRunIds.add(body.card_id);
    // 2. Repopulate the active image. Exact library matches remain ordinary
    //    library selections; otherwise source_image is a private, temporary
    //    archive snapshot and must stay outside availableImages.
    if (normalizedConfig.image_path) {
      await app.commands.loadImages();
      const sourceImage = body.source_image || null;
      if (sourceImage?.source_ref) {
        app.state.image.selectedImage = app.commands._cloneValue(sourceImage);
      } else {
        const activeFilename = sourceImage?.filename || normalizedConfig.image_path;
        app.state.image.selectedImage = (app.state.image.availableImages || [])
          .find(i => i.filename === activeFilename) || null;
      }
      if (app.state.image.selectedImage) app.commands.applyImageAspectDefault();
    }
    // 3. Repopulate wizard settings so a re-solve reproduces the run. `config` is the
    //    source of truth (no inverse "read controls into config"), so copy loaded keys
    //    in, mirroring _applySettingsProfileToConfig.
    for (const [k, v] of Object.entries(normalizedConfig)) app.state.settings.config[k] = app.commands._cloneValue(v);
    app.state.settings.config.image_path = app.state.image.selectedImage?.filename || null;
    app.state.settings.config.image_source_ref = app.state.image.selectedImage?.source_ref || null;
    // 3b. Restore the live frame + image-adjust globals. The solve payload is built from
    //     these (see syncConfigToServer: cfg.frame ← frameState, cfg.image_adjust ←
    //     imageAdjust), NOT from `config`, so without this a re-solve would use stale
    //     crop/rotation/pan/flip/adjustment. Mirror the session-startup restore (init()),
    //     adding the flips it omits.
    if (normalizedConfig.frame) {
      const f = normalizedConfig.frame;
      app.state.image.frameState.widthMm = app.commands.clampFrameWidth(f.width_mm ?? 100);
      app.state.image.frameState.heightMm = app.commands.clampFrameHeight(f.height_mm ?? 100);
      app.state.image.frameState.scale = f.scale ?? 100;
      app.state.image.frameState.rotation = f.rotation ?? 0;
      app.state.image.frameState.panX = f.pan_x ?? 0;
      app.state.image.frameState.panY = f.pan_y ?? 0;
      app.state.image.frameState.flipH = f.flip_h ?? false;
      app.state.image.frameState.flipV = f.flip_v ?? false;
    }
    if (normalizedConfig.image_adjust) Object.assign(app.state.image.imageAdjust, normalizedConfig.image_adjust);
    // 4. Re-render controls + image tab + solve history. renderSettingsTab() is the real
    //    settings-controls re-render the settings-profile load path (_doLoadSettingsProfile)
    //    calls after _applySettingsProfileToConfig.
    app.commands.renderSettingsTab();
    await app.commands.restoreLoadedRunPaletteToDeck({ ...body, config: normalizedConfig, palette: loadedPalette }, loadedSupport);
    app.commands.renderImageTab();
    // 4b. renderImageTab() re-syncs the frame controls from frameState (syncScaleSlider /
    //     syncRotationSlider / syncDimFields / width+height sliders), but it does NOT touch
    //     the image-adjust sliders/inputs or the B/W toggle. Push imageAdjust into those
    //     DOM controls explicitly, mirroring the Reset-button sync block (~frameResetBtn).
    const adjustSyncPairs = [
      ["adjustExposure", "exposure"], ["adjustContrast", "contrast"],
      ["adjustHighlight", "highlight"], ["adjustShadow", "shadow"],
      ["adjustTintHue", "tint_hue"], ["adjustTintStrength", "tint_strength"],
      ["adjustSaturation", "saturation"], ["adjustTemp", "temperature"],
    ];
    for (const [id, key] of adjustSyncPairs) {
      const val = app.state.image.imageAdjust[key] ?? 0;
      const inp = app.state.ui.$(`#${id}`);
      const sld = app.state.ui.$(`#${id}Slider`);
      if (inp) inp.value = val;
      if (sld) sld.value = val;
    }
    app.state.ui.$$("#bwColorToggle .toggle-btn").forEach(b =>
      b.classList.toggle("is-active", b.dataset.val === app.state.image.imageAdjust.mode));
    const colorCtrl = app.state.ui.$("#colorControls");
    if (colorCtrl) colorCtrl.style.display = app.state.image.imageAdjust.mode === "bw" ? "none" : "";
    app.commands.renderFrameCanvas();
    app.commands.renderSolveTab();
    if (app.state.ui.currentTab === "export") app.commands.renderExportTab();
      await app.commands.syncConfigToServer({ throwOnError: true });
    } catch (error) {
      app.commands.restoreLoadedRunApplicationState(previous);
      try {
        await app.commands.syncConfigToServer({ throwOnError: true });
      } catch {
        // The original error remains authoritative. A later ordinary settings
        // synchronization will retry the restored client configuration.
      }
      throw error;
    } finally {
      app.state.solve.loadedRunApplyRunning = false;
    }
  }

  function showWhenRuleMatches(rule, getActualValue) {
    if (!rule) return true;
    for (const [param, expected] of Object.entries(rule)) {
      const actual = getActualValue(param);
      const expectedValues = Array.isArray(expected) ? expected : [expected];
      if (!expectedValues.some(value => String(actual) === String(value))) {
        return false;
      }
    }
    return true;
  }

  function isModuleParamVisibleInSummary(param, configValues) {
    return app.commands.showWhenRuleMatches(param.show_when, key => configValues[key]);
  }

  function formatSolveSummaryValue(param, rawValue) {
    if (typeof rawValue === "boolean") return rawValue ? "on" : "off";
    if (param?.type === "choice" && typeof rawValue === "string") {
      return rawValue.replace(/_/g, " ");
    }
    return param?.unit ? `${rawValue} ${param.unit}` : rawValue;
  }

  function formatSolveSummaryMm(rawValue) {
    const num = Number(rawValue);
    if (!Number.isFinite(num)) return "\u2014";
    return `${num.toFixed(2).replace(/\.?0+$/, "")} mm`;
  }

  function getSolveRunEssentialsItems(run) {
    // Stage 8 essentials, bound to a completed run's recipe snapshot (never live config).
    const settings = app.commands.getSolveRunSettingsSnapshot(run);
    const isLuminance = app.commands.normalizeLuminanceMode(settings.luminance_mode) === "luminance_detail";
    const items = [
      { label: "Mode", value: isLuminance ? "Luminance" : "Color" },
      { label: "Solve pitch", value: app.commands.formatSolveSummaryMm(settings.solver_fine_pitch_mm || settings.image_sample_pitch_mm) },
      { label: "Layer height", value: app.commands.formatSolveSummaryMm(settings.layer_height) },
      { label: "Max thickness", value: app.commands.formatSolveSummaryMm(settings.t_max) },
      { label: "Color region target", value: `${settings.color_region_target_mm ?? 0.60} mm` },
      { label: "Detail limit", value: `${settings.detail_cap_max_layers ?? 5} layers` },
      { label: "Base thickness", value: app.commands.formatSolveSummaryMm(settings.d_wb) },
    ];
    const preprocessing = app.commands.getSolveRunActiveModulesForSlot(run, "preprocessing").map((name) => {
      const desc = app.commands.moduleDescriptorById(name);
      return desc ? app.commands.moduleDisplayName(desc) : app.commands.humanizeModuleName(name);
    });
    if (preprocessing.length) {
      items.push({ label: "Pre-processing", value: preprocessing.join(", ") });
    }
    const swapGrouping = run?.results?.staged_metrics?.swap_grouping;
    const swapGroups = Array.isArray(swapGrouping?.groups) ? swapGrouping.groups : [];
    if (swapGroups.length) {
      const groupSizes = swapGroups.map((group) => Array.isArray(group) ? group.length : 0);
      items.push({
        label: "Swap groups",
        value: `${swapGroups.length} (${groupSizes.join(" + ")} colors)`,
      });
    }
    let bandHeights = Array.isArray(swapGrouping?.band_heights_mm)
      ? swapGrouping.band_heights_mm
      : [];
    if (!bandHeights.length && Array.isArray(swapGrouping?.band_layers)) {
      const bandLayerHeight = Number(swapGrouping?.layer_height_mm);
      if (Number.isFinite(bandLayerHeight)) {
        bandHeights = swapGrouping.band_layers.map((layers) => Number(layers) * bandLayerHeight);
      }
    }
    if (bandHeights.length && bandHeights.every((height) => Number.isFinite(Number(height)))) {
      items.push({
        label: "Band heights",
        value: bandHeights.map((height) => `${Number(height).toFixed(2)} mm`).join(" / "),
      });
    }
    const pauseHeights = Array.isArray(swapGrouping?.pause_z_mm) ? swapGrouping.pause_z_mm : [];
    if (swapGroups.length) {
      const pauseValue = pauseHeights.length
        ? `${pauseHeights.length} (${pauseHeights.map((height) => `z=${Number(height).toFixed(2)} mm`).join(", ")})`
        : "0";
      items.push({ label: "Pause count", value: pauseValue });
    }
    const medianBandingCost = swapGrouping?.banding_cost?.median_de_delta;
    if (Number.isFinite(medianBandingCost)) {
      const sign = medianBandingCost >= 0 ? "+" : "";
      items.push({
        label: "Swap banding cost",
        value: `median ${sign}${medianBandingCost.toFixed(2)} dE`,
      });
    }
    const swapAvailability = run?.results?.staged_metrics?.swap_plan_availability;
    if (swapAvailability?.available === false && swapAvailability?.reason) {
      items.push({ label: "Swap plan", value: swapAvailability.reason });
    }
    return items;
  }

  function renderSolveProgress() {
    // Reuse the shared op-progress floating bar (same as compare tab)
    const el = app.state.ui.$("#opProgress");
    const lbl = app.state.ui.$("#opProgressLabel");
    const elapsed = app.state.ui.$("#opProgressElapsed");
    const fill = el?.querySelector(".op-progress-fill");
    const cancelBtn = app.state.ui.$("#opProgressCancel");

    if (app.state.solve.solveStatus.status === "idle") {
      // Only hide if we were the one showing it
      if (el && el.dataset.owner === "solve") {
        el.classList.add("is-hidden");
        el.dataset.owner = "";
      }
      return;
    }

    if (app.state.solve.solveStatus.status === "running") {
      clearTimeout(app.state.solve.solveProgressHideTimer);
      app.state.solve.solveProgressHideTimer = null;
      const d = app.state.solve.solveStatus.progress_detail || {};
      el.classList.remove("is-hidden");
      el.dataset.owner = "solve";
      el.dataset.cancellable = "true";
      if (cancelBtn) cancelBtn.hidden = false;

      // Label: stage info + stage label
      let label = d.stage_label || app.state.solve.solveStatus.progress || "Solving...";
      if (app.state.solve.solveCancelPending || app.state.solve.solveStatus.cancel_requested) {
        label = `Cancellation requested: ${label}`;
      }
      if (d.stage_index && d.stage_count) {
        label = `Step ${d.stage_index}/${d.stage_count}: ${label}`;
      }
      if (lbl) lbl.textContent = label;
      if (cancelBtn) cancelBtn.disabled = !!(app.state.solve.solveCancelPending || app.state.solve.solveStatus.cancel_requested);

      // Progress bar
      if (fill) {
        const overallPct = Number(d.overall_pct);
        if (d.overall_pct != null && Number.isFinite(overallPct)) {
          const boundedPct = Math.max(0, Math.min(100, overallPct));
          fill.className = "op-progress-fill";
          fill.style.width = `${boundedPct}%`;
          el.setAttribute("role", "progressbar");
          el.setAttribute("aria-valuemin", "0");
          el.setAttribute("aria-valuemax", "100");
          el.setAttribute("aria-valuenow", String(Math.round(boundedPct)));
        } else {
          fill.className = "op-progress-fill indeterminate";
          fill.style.width = "";
          el.setAttribute("role", "progressbar");
          el.removeAttribute("aria-valuenow");
        }
      }

      // Elapsed
      const elapsedVal = app.state.solve.solveStatus.elapsed_s ?? d.elapsed_s ?? 0;
      if (elapsed) app.commands.setOperationElapsedSeconds(elapsedVal);

    } else if (app.state.solve.solveStatus.status === "complete") {
      if (cancelBtn) cancelBtn.disabled = false;
      if (el && el.dataset.owner === "solve") {
        if (lbl) lbl.textContent = "Solve complete";
        if (fill) {
          fill.className = "op-progress-fill";
          fill.style.width = "100%";
        }
        el.setAttribute("role", "progressbar");
        el.setAttribute("aria-valuemin", "0");
        el.setAttribute("aria-valuemax", "100");
        el.setAttribute("aria-valuenow", "100");
        clearTimeout(app.state.solve.solveProgressHideTimer);
        app.state.solve.solveProgressHideTimer = setTimeout(() => {
          if (el.dataset.owner === "solve") {
            el.classList.add("is-hidden");
            el.dataset.owner = "";
          }
        }, 700);
      }
    } else if (app.state.solve.solveStatus.status === "error" || app.state.solve.solveStatus.status === "cancelled") {
      if (cancelBtn) cancelBtn.disabled = false;
      if (el && el.dataset.owner === "solve") {
        if (lbl) {
          lbl.textContent = app.state.solve.solveStatus.status === "cancelled"
            ? "Solve cancelled"
            : `Error: ${app.state.solve.solveStatus.progress}`;
        }
        // Auto-hide after a moment
        const terminalStatus = app.state.solve.solveStatus.status;
        clearTimeout(app.state.solve.solveProgressHideTimer);
        app.state.solve.solveProgressHideTimer = setTimeout(() => {
          if (el.dataset.owner === "solve" && app.state.solve.solveStatus.status === terminalStatus) {
            el.classList.add("is-hidden");
            el.dataset.owner = "";
          }
        }, 3000);
      }
    }
  }

  async function loadSurfaceBlob(url) {
    try {
      const resp = await fetch(url);
      if (!resp.ok) return null;
      const buf = await resp.arrayBuffer();
      const header = new Uint32Array(buf, 0, 2);
      const height = header[0], width = header[1];
      const data = new Float32Array(buf, 8);
      if (data.length !== width * height) return null;
      return { width, height, data };
    } catch { return null; }
  }

  async function loadUint32Blob(url) {
    try {
      const resp = await fetch(url);
      if (!resp.ok) return null;
      const buf = await resp.arrayBuffer();
      if (buf.byteLength < 8 || ((buf.byteLength - 8) % 4) !== 0) return null;
      const header = new Uint32Array(buf, 0, 2);
      const height = header[0], width = header[1];
      const data = new Uint32Array(buf, 8);
      if (data.length !== width * height) return null;
      return { width, height, data };
    } catch { return null; }
  }

  Object.assign(app.commands, {
    validateWritableRunLabel,
    initialSaveRunLabel,
    isCardInteractionTarget,
    isActivePendingSolveRun,
    getSolveRunDeleteBlockReason,
    buildSolveRunDeleteButton,
    buildSolveRunSupportChipsHtml,
    renderSolveRunDeleteState,
    resetSolveRunDeleteConfirm,
    armSolveRunDeleteConfirm,
    handleSolveRunDeleteClick,
    deleteSolveRun,
    removePendingSolveRun,
    clearSolveHistory,
    getSolveHistoryClearButtons,
    isSolveHistoryClearDisabled,
    syncSolveHistoryClearButtons,
    resetSolveHistoryClearConfirm,
    armSolveHistoryClearConfirm,
    handleSolveHistoryClearClick,
    renderSolveRunSidebar,
    saveSolveRun,
    refreshOpenSolveRunLabels,
    _setSavedRunsModalOpen,
    openSavedRunsModal,
    savedRunKey,
    getSelectedSavedRun,
    savedRunTierLabel,
    formatSavedRunTimestamp,
    savedRunDownloadUrl,
    loadSettingsFromSavedRun,
    activateSelectedSavedRun,
    resetSavedRunDeleteConfirm,
    updateSavedRunFooterActions,
    refreshSavedRunRows,
    refreshSavedRunSelection,
    promoteSelectedSavedRun,
    deleteSelectedSavedRun,
    downloadSelectedSavedRun,
    openRenameSavedRunDialog,
    onLoadSavedRun,
    restoreLoadedRunPaletteToDeck,
    captureLoadedRunApplicationState,
    restoreLoadedRunApplicationState,
    applyLoadedRun,
    showWhenRuleMatches,
    isModuleParamVisibleInSummary,
    formatSolveSummaryValue,
    formatSolveSummaryMm,
    getSolveRunEssentialsItems,
    renderSolveProgress,
    loadSurfaceBlob,
    loadUint32Blob,
  });}
