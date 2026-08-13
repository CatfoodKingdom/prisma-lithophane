/**
 * Install the palette/library feature commands.
 * @param {import("../../core/types.js").ApplicationContext} app
 */
export function installFeaturesPaletteLibrary(app) {
function isGenerationEligibleFilament(filament) {
    return !!(
      filament
      && filament.has_profile
      && filament.exclude_from_model !== true
      && filament.generation_available !== false
    );
  }

function getGenerationEligibleFilamentIds() {
    return app.state.session.allFilaments
      .filter(app.commands.isGenerationEligibleFilament)
      .map(filament => filament.filament_id);
  }

function normalizeEnabledFilamentEntry(entry) {
    if (!entry || !Array.isArray(entry.eligible_ids) || !Array.isArray(entry.enabled_ids)) return null;
    const stringsOnly = values => values.filter(value => typeof value === "string" && value.trim());
    return {
      eligible_ids: stringsOnly(entry.eligible_ids),
      enabled_ids: stringsOnly(entry.enabled_ids),
    };
  }

function reconcileEnabledFilamentIds(eligibleIds, savedEntry) {
    const eligible = [...new Set(eligibleIds)];
    const normalized = app.commands.normalizeEnabledFilamentEntry(savedEntry);
    if (!normalized) return eligible;

    const previouslyEligible = new Set(normalized.eligible_ids);
    const previouslyEnabled = new Set(normalized.enabled_ids);
    return eligible.filter(id => previouslyEnabled.has(id) || !previouslyEligible.has(id));
  }

function readEnabledFilamentStore() {
    try {
      const parsed = app.persistence.readJson(app.state.ui.ENABLED_FILAMENTS_STORAGE_KEY, null);
      if (
        parsed
        && parsed.schema_version === app.state.ui.ENABLED_FILAMENTS_STORAGE_VERSION
        && parsed.libraries
        && typeof parsed.libraries === "object"
        && !Array.isArray(parsed.libraries)
      ) {
        return parsed;
      }
    } catch { /* discard malformed state after the runtime library is known */ }
    return { schema_version: app.state.ui.ENABLED_FILAMENTS_STORAGE_VERSION, libraries: {} };
  }

function authoritativeRuntimeLibraryId() {
    const status = app.state.session.modelLibraryManager.status;
    if (!status || status.active_state_error || !status.runtime_active_library_id) return null;
    return String(status.runtime_active_library_id);
  }

function saveEnabledFilaments() {
    // A teaching guide owns an ephemeral environment. Its baseline and any
    // hands-on filament changes must not overwrite the user's once-per-library
    // preference; recovery restores the in-memory selection from its snapshot.
    if (app.state.guides.workspaceSessionId) return false;
    const runtimeLibraryId = app.commands.authoritativeRuntimeLibraryId();
    if (
      !app.state.palette.enabledFilamentPersistenceReady
      || !runtimeLibraryId
      || runtimeLibraryId !== app.state.palette.enabledFilamentRuntimeLibraryId
    ) return false;

    const eligibleIds = app.commands.getGenerationEligibleFilamentIds();
    const eligibleSet = new Set(eligibleIds);
    const store = app.commands.readEnabledFilamentStore();
    store.libraries[runtimeLibraryId] = {
      eligible_ids: eligibleIds,
      enabled_ids: [...app.state.palette.enabledFilaments].filter(id => eligibleSet.has(id)),
    };
    try {
      app.persistence.writeJson(app.state.ui.ENABLED_FILAMENTS_STORAGE_KEY, store);
      return true;
    } catch {
      return false;
    }
  }

function refreshEnabledFilamentConsumers({ reopenDetailId = null } = {}) {
    app.commands.renderLibraryFilterGrid();
    app.commands.renderCreationTab();
    app.commands.updateRail();
    if (reopenDetailId) app.commands.openFilamentDetail(reopenDetailId);
  }

function applyEnabledFilamentSelection(nextIds, { persist = true, render = true, reopenDetailId = null } = {}) {
    const eligibleIds = new Set(app.commands.getGenerationEligibleFilamentIds());
    app.state.palette.enabledFilaments = new Set([...nextIds].filter(id => eligibleIds.has(id)));
    app.state.palette.candidateSelection = new Set([...app.state.palette.candidateSelection].filter(id => app.state.palette.enabledFilaments.has(id)));
    app.state.palette.manualSlots = app.state.palette.manualSlots.filter(id => app.state.palette.enabledFilaments.has(id));
    if (persist) app.commands.saveEnabledFilaments();
    if (render) app.commands.refreshEnabledFilamentConsumers({ reopenDetailId });
  }

function setFilamentEnabled(filamentId, enabled, { reopenDetail = false } = {}) {
    const next = new Set(app.state.palette.enabledFilaments);
    if (enabled) next.add(filamentId);
    else next.delete(filamentId);
    app.commands.applyEnabledFilamentSelection(next, { reopenDetailId: reopenDetail ? filamentId : null });
  }

function reconcileEnabledFilamentsForRuntimeLibrary() {
    const runtimeLibraryId = app.commands.authoritativeRuntimeLibraryId();
    if (!runtimeLibraryId) {
      app.state.palette.enabledFilamentRuntimeLibraryId = null;
      app.state.palette.enabledFilamentPersistenceReady = false;
      app.commands.applyEnabledFilamentSelection(app.commands.getGenerationEligibleFilamentIds(), { persist: false, render: false });
      return false;
    }

    const store = app.commands.readEnabledFilamentStore();
    const eligibleIds = app.commands.getGenerationEligibleFilamentIds();
    const savedEntry = app.commands.normalizeEnabledFilamentEntry(store.libraries[runtimeLibraryId]);
    const reconciledIds = app.commands.reconcileEnabledFilamentIds(eligibleIds, savedEntry);
    app.state.palette.enabledFilamentRuntimeLibraryId = runtimeLibraryId;
    app.state.palette.enabledFilamentPersistenceReady = true;
    app.commands.applyEnabledFilamentSelection(reconciledIds, { persist: false, render: false });

    // The legacy value has no library provenance. Retire it only after the new,
    // authoritative runtime-scoped record has been written successfully.
    if (app.commands.saveEnabledFilaments()) {
      try { app.persistence.remove(app.state.ui.LEGACY_ENABLED_FILAMENTS_STORAGE_KEY); } catch { /* ignore */ }
    }
    return true;
  }

function openLibraryModal() {
    const backdrop = app.state.ui.$("#libraryModalBackdrop");
    if (backdrop) {
      backdrop.classList.remove("is-hidden");
      backdrop.setAttribute("aria-hidden", "false");
    }
    app.commands.renderLibraryFilterGrid();
  }

function closeLibraryModal() {
    const backdrop = app.state.ui.$("#libraryModalBackdrop");
    if (backdrop) {
      backdrop.classList.add("is-hidden");
      backdrop.setAttribute("aria-hidden", "true");
    }
  }

function renderLibraryFilterGrid() {
    const grid = app.state.ui.$("#libraryFilterGrid");
    if (!grid) return;

    // Group by manufacturer (same pattern as palette library pane)
    const groups = new Map();
    for (const fil of app.state.session.allFilaments) {
      const mfg = fil.manufacturer || "Other";
      if (!groups.has(mfg)) groups.set(mfg, []);
      groups.get(mfg).push(fil);
    }

    let html = "";
    for (const [mfg, fils] of groups) {
      html += `<div class="library-group-header">${app.commands.esc(mfg)}</div>`;
      html += `<div class="library-group-cards">`;
      for (const fil of fils) {
        const isEnabled = app.state.palette.enabledFilaments.has(fil.filament_id);
        const hasProfile = fil.has_profile;
        const isEligible = app.commands.isGenerationEligibleFilament(fil);
        const textCol = app.commands.textColorForHex(fil.hex);
        const stateClass = !isEligible ? "no-profile" : isEnabled ? "is-selected" : "is-disabled-filter";
        const unavailableTitle = !hasProfile
          ? "No calibration profile available"
          : !isEligible
            ? "Unavailable in this model library"
            : "";
        html += `
          <div class="filament-card ${stateClass}"
               data-filament-id="${fil.filament_id}"
               ${unavailableTitle ? `title="${unavailableTitle}"` : ""}>
            <div class="filter-check">${isEnabled && isEligible ? "\u2713" : ""}</div>
            <div class="filament-swatch" style="background:${fil.hex};color:${textCol}">
              ${!isEligible ? "?" : ""}
            </div>
            <div class="filament-copy">
              <div class="filament-detail">${app.commands.esc(fil.color_name)}</div>
            </div>
          </div>
        `;
      }
      html += `</div>`;
    }
    grid.innerHTML = html;

    grid.querySelectorAll(".filament-card").forEach((card) => {
      const fid = card.dataset.filamentId;
      const fil = app.commands.filamentById(fid);
      if (!app.commands.isGenerationEligibleFilament(fil)) return;

      card.addEventListener("click", () => {
        app.commands.setFilamentEnabled(fid, !app.state.palette.enabledFilaments.has(fid));
      });
    });

    app.commands.updateLibraryFilterStatus();
  }

function handleLibraryFilterSelectAll() {
    app.commands.applyEnabledFilamentSelection(app.commands.getGenerationEligibleFilamentIds());
  }

function handleLibraryFilterDeselectAll() {
    app.commands.applyEnabledFilamentSelection([]);
  }

  Object.assign(app.commands, {
    isGenerationEligibleFilament,
    getGenerationEligibleFilamentIds,
    normalizeEnabledFilamentEntry,
    reconcileEnabledFilamentIds,
    readEnabledFilamentStore,
    authoritativeRuntimeLibraryId,
    saveEnabledFilaments,
    refreshEnabledFilamentConsumers,
    applyEnabledFilamentSelection,
    setFilamentEnabled,
    reconcileEnabledFilamentsForRuntimeLibrary,
    openLibraryModal,
    closeLibraryModal,
    renderLibraryFilterGrid,
    handleLibraryFilterSelectAll,
    handleLibraryFilterDeselectAll,
  });
}
