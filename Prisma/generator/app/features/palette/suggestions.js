/**
 * Install the palette/suggestions feature commands.
 * @param {import("../../core/types.js").ApplicationContext} app
 */
export function installFeaturesPaletteSuggestions(app) {
function renderCreationTab() {
    const suggestionMode = app.state.ui.$("#paletteSuggestMode");
    if (suggestionMode) {
      suggestionMode.value = app.commands.normalizeLuminanceMode(
        app.state.settings.config.luminance_mode || app.commands.settingDefault("luminance_mode"),
      );
    }
    const modeNote = app.state.ui.$("#creationModeNote");
    if (modeNote) {
      modeNote.textContent = app.state.palette.creationMode === "manual"
        ? app.state.palette.manualVariantDraft
          ? "Change this copy in the Manual builder. The source Palette Deck card remains unchanged."
          : "Select filaments to be included in the manual palette. The number chosen can exceed the available AMS slots, but will require swapping filaments mid-print."
        : "Choose the exact number of palette colors and how many suggestions to create. Choose the Solve Mode you will use. Suggested Palettes need to be added to the Palette Deck before use in a Solve.";
    }

    if (app.state.palette.creationMode === "auto") {
      app.commands.renderCandidateLibrary();   // may selectAllCandidates() on first render
      app.commands.updateSuggestSlotHint();
      app.commands.renderAmsPreview();
    } else {
      app.commands.renderManualLibrary();
      app.commands.renderManualAmsSlots();
    }

    // Update header chips (after renderCandidateLibrary so candidateSelection is populated)
    const profiled = app.state.session.allFilaments.filter(
      f => app.commands.isGenerationEligibleFilament(f)
        && app.state.palette.enabledFilaments.has(f.filament_id),
    );
    const reservedCount = profiled.filter(
      f => app.commands.getBaseCapIds().has(f.filament_id),
    ).length;
    const candidateChip = app.state.ui.$("#candidateCountChip");
    if (candidateChip) {
      candidateChip.textContent = `${app.state.palette.candidateSelection.size}/${profiled.length}`;
      candidateChip.title = reservedCount
        ? `${app.state.palette.candidateSelection.size} selected for suggestions; ${reservedCount} reserved for base/cap`
        : `${app.state.palette.candidateSelection.size} selected for suggestions`;
    }
    const manualChip = app.state.ui.$("#manualCountChip");
    if (manualChip) {
      manualChip.textContent = `${app.commands.manualVariantFilamentIds().length}`;
    }
    app.commands.renderDeckCards();
    app.commands.syncCreationSidePanelSizing();
    app.commands.updateLibraryFilterStatus();
  }

function getRequestedPaletteSuggestionCount() {
    const input = app.state.ui.$("#targetSuggestCount");
    const count = Math.max(1, Math.min(10, parseInt(input?.value, 10) || 5));
    if (input) input.value = String(count);
    return count;
  }

function applyPaletteSuggestModeToSettings(mode) {
    const normalized = app.commands.normalizeLuminanceMode(mode);
    const select = app.state.ui.$("#paletteSuggestMode");
    if (select) select.value = normalized;
    app.commands.setSolveModeControlValue(normalized);
    app.commands.updateLuminanceModeFields();
    app.commands.updateCapModeFields();
    app.commands.updateStage4DetailFields();
    app.commands.readConfigFromUI();
    app.commands.renderSettingsTab({ preservePendingUi: true });
    app.commands.updateDerivedParams();
    app.commands.updateAccordionSummaries();
    app.commands.checkPresetModified();
    return normalized;
  }

function buildPaletteSuggestionPayload() {
    const targetCount = Number(app.state.ui.$("#targetFilamentCount")?.value);
    const availableIds = [...app.state.palette.candidateSelection]
      .filter(fid => !app.commands.getBaseCapIds().has(fid));
    const paletteMode = app.commands.normalizeLuminanceMode(
      app.state.ui.$("#paletteSuggestMode")?.value || "standard",
    );
    return {
      image_path: app.state.image.selectedImage?.filename || "",
      image_source_ref: app.state.image.selectedImage?.source_ref || null,
      n_filaments: targetCount,
      top_k: app.commands.getRequestedPaletteSuggestionCount(),
      filament_ids: availableIds,
      palette_mode: paletteMode,
    };
  }

function selectAllCandidates() {
    const profiled = app.state.session.allFilaments.filter(f => app.commands.isGenerationEligibleFilament(f) && !app.commands.getBaseCapIds().has(f.filament_id) && app.state.palette.enabledFilaments.has(f.filament_id));
    app.state.palette.candidateSelection = new Set(profiled.map(f => f.filament_id));
  }

function renderCandidateLibrary() {
    const grid = app.state.ui.$("#candidateGrid");
    if (!grid) return;

    const baseCapIds = app.commands.getBaseCapIds();
    for (const filamentId of baseCapIds) {
      app.state.palette.candidateSelection.delete(filamentId);
    }
    const libraryFils = app.state.session.allFilaments.filter(
      f => app.commands.isGenerationEligibleFilament(f)
        && app.state.palette.enabledFilaments.has(f.filament_id),
    );

    // Auto-select all on first render only
    if (!app.state.palette.candidateInitialized && libraryFils.length > 0) {
      app.state.palette.candidateInitialized = true;
      app.commands.selectAllCandidates();
    }

    // Group by manufacturer
    const groups = new Map();
    for (const fil of libraryFils) {
      const mfg = fil.manufacturer || "Other";
      if (!groups.has(mfg)) groups.set(mfg, []);
      groups.get(mfg).push(fil);
    }

    let html = "";
    for (const [mfg, fils] of groups) {
      html += `<div class="library-group-header">${app.commands.esc(mfg)}</div>`;
      for (const fil of fils) {
        const reserved = baseCapIds.has(fil.filament_id);
        const selected = app.state.palette.candidateSelection.has(fil.filament_id);
        const stateClass = reserved
          ? "is-base-cap-reserved"
          : selected ? "is-candidate" : "is-deselected-candidate";
        const textCol = app.commands.textColorForHex(fil.hex);
        const name = fil.color_name || fil.display_name || fil.filament_id;
        const reservedAttributes = reserved
          ? `aria-disabled="true" title="${app.commands.esc(`${name} is reserved for the white base and cap and cannot be used as a suggested color.`)}"`
          : "";
        html += `<div class="filament-card ${stateClass}" data-filament-id="${fil.filament_id}" ${reservedAttributes}>
          <div class="filament-swatch" style="background:${fil.hex};color:${textCol}"></div>
          <div class="filament-copy"><div class="filament-detail">${app.commands.esc(name)}</div></div>
          ${reserved ? '<span class="filament-reserved-label">BASE/CAP</span>' : ""}
        </div>`;
      }
    }
    grid.innerHTML = html;

    // Bind toggle clicks
    grid.querySelectorAll(".filament-card").forEach(card => {
      card.addEventListener("click", () => {
        const fid = card.dataset.filamentId;
        if (app.commands.getBaseCapIds().has(fid)) return;
        if (app.state.palette.candidateSelection.has(fid)) {
          app.state.palette.candidateSelection.delete(fid);
        } else {
          app.state.palette.candidateSelection.add(fid);
        }
        app.commands.renderCreationTab();
      });
    });
  }

function getAmsPreviewGeometry() {
    const units = Math.max(1, Math.trunc(Number(app.state.session.printerConfig.ams_units) || 1));
    const slotsPerUnit = Math.max(1, Math.trunc(Number(app.state.session.printerConfig.slots_per_unit) || 4));
    const totalSlots = units * slotsPerUnit;
    const whiteSlots = Math.min(
      totalSlots,
      Math.max(0, Math.trunc(Number(app.commands.getBaseCapSlots()) || 0)),
    );
    return {
      units,
      slotsPerUnit,
      totalSlots,
      whiteSlots,
      colorSlots: Math.max(0, totalSlots - whiteSlots),
    };
  }

function validatePaletteSuggestionRequest() {
    const requested = Number(app.state.ui.$("#targetFilamentCount")?.value);
    if (!Number.isInteger(requested) || requested < 2 || requested > 16) {
      app.commands.showToast("Palette Colors must be a whole number from 2 to 16", "warn");
      return false;
    }
    const eligibleSelected = [...app.state.palette.candidateSelection]
      .filter(fid => !app.commands.getBaseCapIds().has(fid));
    if (eligibleSelected.length < requested) {
      app.commands.showToast(
        `Palette Colors is ${requested}, but only ${eligibleSelected.length} eligible color filaments are selected. Select more filaments or reduce Palette Colors.`,
        "warn",
      );
      return false;
    }
    return true;
  }

function renderAmsPreview() {
    const container = app.state.ui.$("#amsPreview");
    if (!container) return;

    const rawRequestedColors = String(app.state.ui.$("#targetFilamentCount")?.value ?? "").trim();
    const parsedRequestedColors = Number(rawRequestedColors);
    const requestedColors = rawRequestedColors
      && Number.isInteger(parsedRequestedColors)
      && parsedRequestedColors >= 2
      && parsedRequestedColors <= 16
      ? parsedRequestedColors
      : null;
    const {
      units,
      slotsPerUnit,
      totalSlots,
      whiteSlots,
      colorSlots,
    } = app.commands.getAmsPreviewGeometry();
    const filledCount = requestedColors === null ? 0 : Math.min(requestedColors, colorSlots);
    const additionalColors = requestedColors === null ? 0 : Math.max(0, requestedColors - colorSlots);
    const visualColumns = Math.min(4, slotsPerUnit);

    let html = "";
    let slotIdx = 0;

    for (let u = 0; u < units; u++) {
      html += `<div class="ams-preview-unit-label">AMS ${u + 1}</div>`;
      html += `<div class="ams-preview-slots" style="--ams-preview-columns:${visualColumns}">`;
      for (let s = 0; s < slotsPerUnit; s++) {
        if (slotIdx < whiteSlots) {
          html += `<div class="ams-preview-slot is-white"><span class="ams-preview-base-label"><span>BASE/</span><span>CAP</span></span></div>`;
        } else if (slotIdx < whiteSlots + filledCount) {
          html += `<div class="ams-preview-slot is-filled"><span class="ams-lozenge"></span></div>`;
        } else {
          html += `<div class="ams-preview-slot"><span class="ams-lozenge"></span></div>`;
        }
        slotIdx++;
      }
      html += `</div>`;
    }

    html += requestedColors === null
      ? `<div class="ams-preview-status">Select 2–16 palette colors</div>`
      : `<div class="ams-preview-status">${filledCount} / ${colorSlots} color slots</div>`;

    if (additionalColors > 0) {
      const overflowSlots = Array.from(
        { length: additionalColors },
        () => `<span class="ams-preview-overflow-slot"><span class="ams-preview-overflow-pip"></span></span>`,
      ).join("");
      html += `<div class="ams-preview-overflow">
        <div class="ams-preview-overflow-label">Additional colors (${additionalColors})</div>
        <div class="ams-preview-overflow-slots" style="--ams-preview-columns:${visualColumns}" aria-hidden="true">${overflowSlots}</div>
        <div class="ams-preview-overflow-note">May require filament swaps</div>
      </div>`;
    }

    const reservedSummary = whiteSlots === 1
      ? "One slot is reserved for the base and cap."
      : `${whiteSlots} slots are reserved for the base and cap.`;
    let accessibleSummary;
    if (requestedColors === null) {
      accessibleSummary = `AMS capacity preview. ${reservedSummary} Select 2 to 16 palette colors to preview occupancy.`;
    } else if (additionalColors > 0) {
      accessibleSummary = `${requestedColors} palette colors: ${filledCount} fit in ${colorSlots} simultaneous color slots, with ${additionalColors} additional colors. May require filament swaps. ${reservedSummary}`;
    } else {
      accessibleSummary = `Palette uses ${requestedColors} colors. ${filledCount} of ${colorSlots} simultaneous color slots are occupied. ${reservedSummary}`;
    }

    container.innerHTML = html;
    container.setAttribute("role", "img");
    container.setAttribute("aria-label", accessibleSummary);
  }

function renderManualLibrary() {
    const grid = app.state.ui.$("#manualLibraryGrid");
    const chip = app.state.ui.$("#manualLibraryCountChip");
    if (!grid) return;

    const libraryFils = app.state.session.allFilaments.filter(
      f => app.commands.isGenerationEligibleFilament(f) && !app.commands.getBaseCapIds().has(f.filament_id) && app.state.palette.enabledFilaments.has(f.filament_id)
    );
    const selectedFilaments = app.commands.manualVariantFilamentIds();
    const availableCount = libraryFils.filter(
      f => !selectedFilaments.includes(f.filament_id),
    ).length;
    if (chip) chip.textContent = availableCount;

    const groups = new Map();
    for (const fil of libraryFils) {
      const mfg = fil.manufacturer || "Other";
      if (!groups.has(mfg)) groups.set(mfg, []);
      groups.get(mfg).push(fil);
    }

    let html = "";
    for (const [mfg, fils] of groups) {
      html += `<div class="library-group-header">${app.commands.esc(mfg)}</div>`;
      for (const fil of fils) {
        const placed = selectedFilaments.includes(fil.filament_id);
        const stateClass = placed ? "is-placed" : "";
        const textCol = app.commands.textColorForHex(fil.hex);
        html += `<div class="filament-card ${stateClass}" data-filament-id="${fil.filament_id}">
          <div class="filament-swatch" style="background:${fil.hex};color:${textCol}"></div>
          <div class="filament-copy"><div class="filament-detail">${app.commands.esc(fil.color_name)}</div></div>
        </div>`;
      }
    }
    grid.innerHTML = html;

    grid.querySelectorAll(".filament-card:not(.is-placed)").forEach(card => {
      card.addEventListener("click", () => {
        const fid = card.dataset.filamentId;
        if (app.commands.addManualFilament(fid)) {
          app.commands.renderCreationTab();
        }
      });
    });
  }

function renderManualAmsSlots() {
    const container = app.state.ui.$("#manualAmsSlots");
    const statusEl = app.state.ui.$("#manualAmsStatus");
    const mintBtn = app.state.ui.$("#mintPaletteBtn");
    if (!container) return;

    const units = app.state.session.printerConfig.ams_units || 1;
    const slotsPerUnit = app.state.session.printerConfig.slots_per_unit || 4;
    const totalSlots = app.state.session.printerConfig.ams_slots || 4;
    const whiteSlots = app.commands.getBaseCapSlots();
    const colorSlots = totalSlots - whiteSlots;

    // Build white slot(s)
    const whiteHtml = [];
    const baseFil = app.commands.filamentById(app.commands.getBaseFilament());
    const baseHex = baseFil?.hex || "#f5f0e0";
    const baseName = baseFil?.color_name || app.commands.getBaseFilament();
    whiteHtml.push(`<div class="ams-slot is-white">
      <span class="ams-slot-swatch" style="background:${baseHex};border:1px solid #ddd;"></span>
      <span class="ams-slot-name">${app.commands.esc(baseName)}</span>
      <span class="ams-slot-label">BASE/CAP</span>
    </div>`);

    const variantDraft = app.state.palette.manualVariantDraft;
    const orderedSlots = variantDraft
      ? variantDraft.workingSlots
      : app.state.palette.manualSlots;
    const amsFilaments = orderedSlots.slice(0, colorSlots);
    const swapFilaments = orderedSlots.slice(colorSlots);
    const selectedCount = app.commands.manualVariantFilamentIds().length;

    let html = "";
    let colorIdx = 0;

    for (let u = 0; u < units; u++) {
      html += `<div class="ams-unit-label">AMS ${u + 1}</div>`;
      for (let s = 0; s < slotsPerUnit; s++) {
        const globalSlot = u * slotsPerUnit + s;
        if (globalSlot < whiteSlots) {
          html += whiteHtml[globalSlot];
        } else {
          const manualIndex = colorIdx;
          const fid = amsFilaments[colorIdx];
          colorIdx++;
          if (fid) {
            const fil = app.commands.filamentById(fid);
            html += `<div class="ams-slot" data-filament-id="${fid}" data-manual-index="${manualIndex}">
              <span class="ams-slot-swatch" style="background:${fil?.hex || '#ccc'}"></span>
              <span class="ams-slot-name">${app.commands.esc(fil?.color_name || fid)}</span>
              <span class="ams-slot-remove" data-manual-index="${manualIndex}" aria-label="Remove filament" title="Remove filament">${app.commands.xIconSvg()}</span>
            </div>`;
          } else {
            html += `<div class="ams-slot is-empty"></div>`;
          }
        }
      }
    }

    // Swap overflow
    const activeSwapFilaments = swapFilaments
      .map((filamentId, index) => ({
        filamentId,
        manualIndex: colorSlots + index,
      }))
      .filter(entry => entry.filamentId);
    if (activeSwapFilaments.length > 0) {
      html += `<div class="ams-swap-label">&#9888; SWAP (${activeSwapFilaments.length})</div>`;
      for (const { filamentId: fid, manualIndex } of activeSwapFilaments) {
        const fil = app.commands.filamentById(fid);
        html += `<div class="ams-slot is-swap" data-filament-id="${fid}" data-manual-index="${manualIndex}">
          <span class="ams-slot-swatch" style="background:${fil?.hex || '#ccc'}"></span>
          <span class="ams-slot-name">${app.commands.esc(fil?.color_name || fid)}</span>
          <span class="ams-slot-remove" data-manual-index="${manualIndex}" aria-label="Remove filament" title="Remove filament">${app.commands.xIconSvg()}</span>
        </div>`;
      }
    }

    container.innerHTML = html;

    // Bind remove buttons
    container.querySelectorAll(".ams-slot-remove").forEach(btn => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        app.commands.removeManualFilamentAt(
          Number.parseInt(btn.dataset.manualIndex, 10),
        );
        app.commands.renderCreationTab();
      });
    });

    // Status
    const amsCount = amsFilaments.filter(Boolean).length;
    const swapText = activeSwapFilaments.length > 0
      ? ` + ${activeSwapFilaments.length} swap`
      : "";
    if (statusEl) statusEl.textContent = `${amsCount} / ${colorSlots} slots${swapText}`;
    if (mintBtn) {
      mintBtn.textContent = variantDraft ? "Add Variant to Deck" : "Add to Deck";
      mintBtn.disabled = selectedCount === 0
        || (variantDraft && !app.commands.manualVariantHasChanged());
    }
    const clearBtn = app.state.ui.$("#clearComposerBtn");
    if (clearBtn) clearBtn.textContent = variantDraft ? "Cancel Variant" : "Clear";
    const title = app.state.ui.$("#manualPaletteTitle");
    if (title) {
      title.textContent = variantDraft
        ? `Variant of “${variantDraft.sourceName}”`
        : "Manual Palette";
      title.title = variantDraft ? `Variant of ${variantDraft.sourceName}` : "";
    }
  }

function clearManualSlots() {
    app.state.palette.manualSlots = [];
    app.commands.renderCreationTab();
  }

async function handleSuggestPalettes({ throwOnError = false } = {}) {
    const btn = app.state.ui.$("#suggestPalettesBtn");
    if (!btn) return;

    if (!app.state.image.selectedImage) {
      app.commands.showToast("Load an image first before generating palette suggestions", "warn");
      return;
    }
    if (app.commands.settingsBlocksOperation("suggest")) {
      app.commands.showToast("Current print settings must be corrected before suggesting palettes", "warn");
      return;
    }
    if (!app.commands.validatePaletteSuggestionRequest()) return;

    btn.disabled = true;
    btn.textContent = "...";

    const payload = app.commands.buildPaletteSuggestionPayload();

    let pollingOwner = null;
    try {
      const started = await app.api.apiPost("/palette/suggest", payload);
      app.state.ui.activeSuggestJobId = started?.job_id || null;
      if (!app.state.ui.activeSuggestJobId) throw new Error("Suggestion start did not return a job ID");
      app.state.palette.suggestCancelPending = false;
      app.commands.startProgress("Suggesting palettes...", "suggest");

      const pollingJobId = app.state.ui.activeSuggestJobId;
      if (app.state.ui._suggestPolling) app.state.ui._suggestPolling.cancelled = true;
      pollingOwner = { jobId: pollingJobId, cancelled: false };
      app.state.ui._suggestPolling = pollingOwner;
      const st = await app.services.pollJobUntilTerminal({
        jobId: pollingJobId,
        fetchStatus: () => app.api.apiFetch("/palette/suggest/status"),
        isTerminal: (status) => !["running", "cancelling"].includes(status.status),
        shouldContinue: () => (
          !pollingOwner.cancelled
          && app.state.ui._suggestPolling === pollingOwner
          && app.state.ui.activeSuggestJobId === pollingJobId
        ),
        intervalMs: 1000,
        onStatus: (status) => app.commands.updateOperationProgressFromStatus(status, "Suggesting palettes..."),
        onTransientError: () => app.commands.updateOperationProgressFromStatus(
          { status: "running", progress: "Connection interrupted; retrying suggestion status..." },
          "Suggesting palettes...",
        ),
      });
      if (!st || app.state.ui._suggestPolling !== pollingOwner) return;
      if (st.status === "complete" && st.result) {
        app.commands._processSuggestResults(st.result);
        return st.result;
      } else if (st.status === "cancelled") {
        if (st.result) app.commands._processSuggestResults(st.result);
        app.commands.showToast("Suggestion cancelled", "warn");
      } else if (st.status === "error") {
        app.commands.showToast(`Suggestion failed: ${st.progress}`, "error");
      }
    } catch (err) {
      if (err.name === "AbortError") return;
      if (pollingOwner && app.state.ui._suggestPolling !== pollingOwner) return;
      const prefix = pollingOwner ? "Palette suggestion status failed" : "Palette suggestion failed to start";
      app.commands.showToast(`${prefix}: ${err.message}`, "error");
      if (throwOnError) throw err;
    } finally {
      if (pollingOwner && app.state.ui._suggestPolling !== pollingOwner) return;
      if (app.state.ui._suggestPolling === pollingOwner) app.state.ui._suggestPolling = null;
      app.state.ui.activeSuggestJobId = null;
      app.state.palette.suggestCancelPending = false;
      app.commands.stopProgress();
      btn.textContent = "Suggest Palettes";
      btn.disabled = false;
    }
  }

async function handleSuggestBaseShadingLimit() {
    const btn = app.state.ui.$("#cfgBaseShadingLimitSuggest");
    const input = app.commands.getBaseShadingLimitInput();
    if (!btn || !input) return;
    if (!app.state.image.selectedImage) {
      app.commands.showToast("Select an image before suggesting a shading balance", "warn");
      return;
    }
    btn.disabled = true;
    const oldText = btn.textContent;
    btn.textContent = "...";
    try {
      const result = await app.api.apiPost("/luminance/base-shading-limit/recommend", {
        image_path: app.state.image.selectedImage.filename,
        image_source_ref: app.state.image.selectedImage.source_ref || null,
      });
      const value = app.commands.setLuminanceBaseShadingLimitFraction(
        result.recommended_base_shading_limit_fraction
          ?? result.recommended_authority_fraction
          ?? app.commands.settingDefault("luminance_base_shading_limit_fraction"),
      );
      app.commands.syncBaseShadingLimitControls(app.commands.formatLuminanceBaseShadingLimitPercent(value));
      app.commands.checkPresetModified();
      await app.commands.syncConfigToServer();
      app.commands.showToast(`Shading balance set to ${app.commands.formatLuminanceBaseShadingLimitPercent(value)}%`, "success");
    } catch (err) {
      app.commands.showToast(`Shading balance suggestion failed: ${err.message}`, "error");
    } finally {
      btn.textContent = oldText || "Suggest";
      btn.disabled = false;
    }
  }

function _processSuggestResults(suggestions) {
    let addedCount = 0;
    const addedCardIds = [];
    const seenSets = new Set();
    const paletteMode = app.commands.normalizeLuminanceMode(
      suggestions?.palette_mode || app.state.ui.$("#paletteSuggestMode")?.value || "standard",
    );
    const modePrefix = paletteMode === "luminance_detail" ? "Luminance" : "Color";
    const makeKey = (ids = []) => [...ids].map(String).sort().join("\u0001");
    const pushSuggestionCard = (cand, name, extra = {}) => {
      const key = makeKey(cand.filament_ids || []);
      if (!key || seenSets.has(key)) return false;
      seenSets.add(key);
      const card = {
        id: "suggest-" + Date.now() + "-" + Math.random().toString(36).slice(2, 6),
        name,
        filament_ids: cand.filament_ids,
        gamut: {
          status: "done",
          coverage_pct: cand.coverage_pct || 0,
          mean_de: cand.mean_de || 0,
          suggestion_mean_de: cand.suggestion_mean_de ?? cand.mean_de ?? 0,
        },
        quality_metrics: null,
        saved: false,
        ...extra,
      };
      app.state.palette.stagingDeck.push(card);
      addedCardIds.push(card.id);
      addedCount++;
      return true;
    };

    const candidates = (suggestions.candidates || [])
      .slice(0, app.commands.getRequestedPaletteSuggestionCount());
    for (const cand of candidates) {
      pushSuggestionCard(cand, `${modePrefix} suggested ${app.state.palette.nextDeckNum++}`);
    }

    if (addedCount === 0) {
      app.commands.showToast("No palette suggestions found", "error");
      return;
    }
    app.commands.renderCreationTab();
    app.commands.updateRail();
    app.commands.showToast(`Staged ${addedCount} suggested palettes`, "success");
    app.events.emit("palette.suggestions.completed", {
      cardIds: addedCardIds,
      count: addedCount,
    });
  }

  Object.assign(app.commands, {
    renderCreationTab,
    selectAllCandidates,
    renderCandidateLibrary,
    getAmsPreviewGeometry,
    validatePaletteSuggestionRequest,
    renderAmsPreview,
    renderManualLibrary,
    renderManualAmsSlots,
    clearManualSlots,
    getRequestedPaletteSuggestionCount,
    applyPaletteSuggestModeToSettings,
    buildPaletteSuggestionPayload,
    handleSuggestPalettes,
    handleSuggestBaseShadingLimit,
    _processSuggestResults,
  });
}
