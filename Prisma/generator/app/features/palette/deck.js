/**
 * Install the palette/deck feature commands.
 * @param {import("../../core/types.js").ApplicationContext} app
 */
export function installFeaturesPaletteDeck(app) {
let deckCardMenu = null;
let deckCardMenuAnchor = null;
let deckCardMenuCardId = null;
let deckCardMenuViewport = null;

function normalizeSupportFromLoadedConfig(cfg = {}) {
    const base = cfg.base_filament || cfg.white_base || app.state.session.DEFAULT_BASE_FILAMENT;
    return {
      base,
      capEffective: base,
      capSelector: "__same__",
      base_filament: base,
      cap_filament: "__same__",
      white_base: base,
      white_cap: null,
    };
  }

function normalizeSupportFromPaletteRecord(record = {}, { requireExplicit = false } = {}) {
    const hasBase = Boolean(record.base_filament || record.white_base);
    const hasCap = Object.prototype.hasOwnProperty.call(record, "cap_filament") ||
      Object.prototype.hasOwnProperty.call(record, "white_cap");
    if (requireExplicit && (!hasBase || !hasCap)) return null;
    if (!hasBase && !hasCap) return null;
    return app.commands.normalizeSupportFromLoadedConfig(record);
  }

function selectLoadedPalette(body = {}, cfg = body.config || {}) {
    const filterPalette = (items) => Array.isArray(items)
      ? items.filter(id => typeof id === "string" && id.trim())
      : [];
    const bodyPalette = filterPalette(body.palette);
    if (bodyPalette.length) return bodyPalette;
    return filterPalette(cfg.palette);
  }

function getPaletteGatingIssues(filamentIds) {
    const issues = { missing: [], unavailable: [], disabled: [] };
    const seen = new Set();
    for (const filamentId of Array.isArray(filamentIds) ? filamentIds : []) {
      if (typeof filamentId !== "string" || !filamentId.trim() || seen.has(filamentId)) continue;
      seen.add(filamentId);
      const filament = app.commands.filamentById(filamentId);
      if (!filament) issues.missing.push(filamentId);
      else if (!app.commands.isGenerationEligibleFilament(filament)) issues.unavailable.push(filamentId);
      else if (!app.state.palette.enabledFilaments.has(filamentId)) issues.disabled.push(filamentId);
    }
    return issues;
  }

function paletteGatingIssueCount(issues) {
    return (issues?.missing?.length || 0)
      + (issues?.unavailable?.length || 0)
      + (issues?.disabled?.length || 0);
  }

function buildPaletteGatingMessage(issues, prefix = "Can't use this palette.") {
    const parts = [];
    if (issues.missing.length) {
      parts.push(`${issues.missing.map(app.commands.solveFilamentLabel).join(", ")} ${issues.missing.length === 1 ? "is" : "are"} not present in the active model library.`);
    }
    if (issues.unavailable.length) {
      parts.push(`${issues.unavailable.map(app.commands.solveFilamentLabel).join(", ")} ${issues.unavailable.length === 1 ? "is" : "are"} unavailable for generation in the active model library.`);
    }
    if (issues.disabled.length) {
      parts.push(`${issues.disabled.map(app.commands.solveFilamentLabel).join(", ")} ${issues.disabled.length === 1 ? "is" : "are"} disabled in Manage Filaments. Enable ${issues.disabled.length === 1 ? "it" : "them"} before continuing.`);
    }
    return `${prefix} ${parts.join(" ")}`.trim();
  }

function makePaletteSignature(filamentIds, support) {
    const ids = Array.isArray(filamentIds)
      ? filamentIds.filter(id => typeof id === "string" && id.trim())
      : [];
    const normalizedSupport = support || app.commands.normalizeSupportFromLoadedConfig({});
    return {
      filamentIds: [...ids],
      base: normalizedSupport.base,
      capEffective: normalizedSupport.capEffective,
    };
  }

function paletteSignaturesEqual(a, b) {
    if (!a || !b) return false;
    if (a.base !== b.base || a.capEffective !== b.capEffective) return false;
    if (a.filamentIds.length !== b.filamentIds.length) return false;
    return a.filamentIds.every((id, idx) => id === b.filamentIds[idx]);
  }

function signatureForPaletteRecord(record, inheritedSupport) {
    const explicitSupport = app.commands.normalizeSupportFromPaletteRecord(record);
    return app.commands.makePaletteSignature(record?.filament_ids || [], explicitSupport || inheritedSupport);
  }

function findMatchingDeckCard(signature, deckCards = app.state.palette.deck) {
    return (deckCards || []).find(card => app.commands.paletteSignaturesEqual(
      signature,
      app.commands.signatureForPaletteRecord(card, {
        base: signature.base,
        capEffective: signature.capEffective,
      }),
    )) || null;
  }

function findMatchingSavedPaletteIndex(signature, savedPalettes = app.state.palette.savedPalettesData?.palettes || []) {
    return (savedPalettes || []).findIndex(record => app.commands.paletteSignaturesEqual(
      signature,
      app.commands.signatureForPaletteRecord(record, {
        base: signature.base,
        capEffective: signature.capEffective,
      }),
    ));
  }

function chooseLoadedPaletteRestoreAction({ filamentIds, support, deckCards = app.state.palette.deck, savedPalettes = app.state.palette.savedPalettesData?.palettes || [] }) {
    const signature = app.commands.makePaletteSignature(filamentIds, support);
    if (!signature.filamentIds.length) return { kind: "none" };
    const existing = app.commands.findMatchingDeckCard(signature, deckCards);
    if (existing) return { kind: "reuse-deck", cardId: existing.id };
    const savedIndex = app.commands.findMatchingSavedPaletteIndex(signature, savedPalettes);
    if (savedIndex >= 0) return { kind: "load-saved", savedIndex };
    return { kind: "add-ad-hoc", filamentIds: [...signature.filamentIds] };
  }

function createPaletteDeckCard({ idPrefix = "deck", name, filamentIds, saved = false }) {
    let suffix = 0;
    let id = `${idPrefix}-${Date.now()}`;
    while (app.state.palette.deck.some(card => card.id === id) || app.state.palette.stagingDeck.some(card => card.id === id)) {
      suffix += 1;
      id = `${idPrefix}-${Date.now()}-${suffix}`;
    }
    return {
      id,
      name,
      filament_ids: [...filamentIds],
      gamut: null,
      saved,
    };
  }

function addLoadedAdHocPaletteToDeck(filamentIds, label = "Loaded run palette") {
    const card = app.commands.createPaletteDeckCard({
      idPrefix: "loaded-run",
      name: label,
      filamentIds,
      saved: false,
    });
    app.state.palette.deck.push(card);
    app.commands.activateDeckCard(card.id, { sync: false });
    return card;
  }

function mintPaletteToDeck() {
    if (
      app.state.palette.creationMode === "manual"
      && app.state.palette.manualVariantDraft
    ) {
      return app.commands.commitPaletteVariant();
    }
    const filaments = app.state.palette.creationMode === "manual"
      ? app.state.palette.manualSlots
      : app.state.palette.composerPalette;
    if (filaments.length === 0) return;
    const card = app.commands.createPaletteDeckCard({
      idPrefix: app.state.palette.creationMode === "manual" ? "manual" : "deck",
      name: "Palette " + app.state.palette.nextDeckNum++,
      filamentIds: filaments,
      saved: false,
    });
    if (app.state.palette.creationMode === "manual") {
      app.state.palette.deck.push(card);
      if (!app.state.palette.activeDeckId) app.state.palette.activeDeckId = card.id;
      app.state.palette.manualSlots = [];
      app.commands.syncConfigToServer();
      app.commands.showToast(`Added "${card.name}" to the deck`, "success");
      app.events.emit("palette.deck.updated", { action: "added", card });
    } else {
      app.state.palette.stagingDeck.push(card);
      app.state.palette.composerPalette = [];
      app.commands.showToast(`Staged "${card.name}" with ${card.filament_ids.length} filaments`, "success");
    }
    app.commands.renderCreationTab();
    app.commands.updateRail();
  }

function manualVariantFilamentIds() {
    const draft = app.state.palette.manualVariantDraft;
    if (!draft) return [...app.state.palette.manualSlots];
    return draft.workingSlots.filter(
      filamentId => typeof filamentId === "string" && filamentId.trim(),
    );
  }

function manualVariantHasChanged() {
    const draft = app.state.palette.manualVariantDraft;
    if (!draft) return false;
    const current = app.commands.manualVariantFilamentIds();
    return current.length !== draft.sourceFilamentIds.length
      || current.some((filamentId, index) => (
        filamentId !== draft.sourceFilamentIds[index]
      ));
  }

function addManualFilament(filamentId) {
    if (!filamentId || app.commands.manualVariantFilamentIds().includes(filamentId)) {
      return false;
    }
    const draft = app.state.palette.manualVariantDraft;
    if (!draft) {
      app.state.palette.manualSlots.push(filamentId);
      return true;
    }
    const vacancy = draft.workingSlots.findIndex(value => value === null);
    if (vacancy >= 0) draft.workingSlots[vacancy] = filamentId;
    else draft.workingSlots.push(filamentId);
    return true;
  }

function removeManualFilamentAt(index) {
    const draft = app.state.palette.manualVariantDraft;
    if (!draft) {
      if (index < 0 || index >= app.state.palette.manualSlots.length) return false;
      app.state.palette.manualSlots.splice(index, 1);
      return true;
    }
    if (index < 0 || index >= draft.workingSlots.length) return false;
    draft.workingSlots[index] = null;
    return true;
  }

function nextVariantPaletteName(sourceName) {
    const source = String(sourceName || "").trim() || "Palette";
    const base = `${source} Variant`;
    const existing = new Set(
      app.state.palette.deck.map(card => String(card.name || "").trim().toLocaleLowerCase()),
    );
    if (!existing.has(base.toLocaleLowerCase())) return base;
    let suffix = 2;
    while (existing.has(`${base} ${suffix}`.toLocaleLowerCase())) suffix += 1;
    return `${base} ${suffix}`;
  }

async function beginPaletteVariant(cardId) {
    const card = app.state.palette.deck.find(entry => entry.id === cardId);
    if (!card) {
      app.commands.showToast("That palette is no longer in the deck.", "warn");
      return false;
    }
    const gatingIssues = app.commands.getPaletteGatingIssues(card.filament_ids);
    if (app.commands.paletteGatingIssueCount(gatingIssues)) {
      app.commands.showToast(
        app.commands.buildPaletteGatingMessage(
          gatingIssues,
          `Can't create a variant of “${card.name}”.`,
        ),
        "error",
      );
      return false;
    }

    const existingDraft = app.state.palette.manualVariantDraft;
    if (existingDraft?.sourceCardId === card.id) {
      app.commands.switchTab("creation");
      app.commands.toggleCreationMode("manual");
      app.events.emit("palette.variant.started", { cardId: card.id });
      return true;
    }
    if (existingDraft || app.state.palette.manualSlots.length > 0) {
      const confirmed = await app.commands.appConfirm(
        existingDraft
          ? "Replace the current palette variant draft? The underlying Manual palette draft will remain available if you cancel the new variant."
          : "Use the Manual builder for this variant? Your current Manual palette draft will return if you cancel.",
        {
          title: existingDraft ? "Replace Variant Draft" : "Open Palette Variant",
          ok: "Create Variant",
          cancel: "Cancel",
        },
      );
      if (!confirmed) return false;
    }

    app.state.palette.manualVariantDraft = {
      sourceCardId: card.id,
      sourceName: card.name,
      sourceFilamentIds: [...card.filament_ids],
      workingSlots: [...card.filament_ids],
    };
    app.commands.switchTab("creation");
    app.commands.toggleCreationMode("manual");
    app.events.emit("palette.variant.started", { cardId: card.id });
    return true;
  }

function cancelPaletteVariant() {
    if (!app.state.palette.manualVariantDraft) return false;
    app.state.palette.manualVariantDraft = null;
    app.commands.renderCreationTab();
    app.commands.showToast("Palette variant cancelled", "warn");
    return true;
  }

async function commitPaletteVariant() {
    const draft = app.state.palette.manualVariantDraft;
    if (!draft) return null;
    const filamentIds = app.commands.manualVariantFilamentIds();
    if (!filamentIds.length || !app.commands.manualVariantHasChanged()) return null;

    const card = app.commands.createPaletteDeckCard({
      idPrefix: "variant",
      name: app.commands.nextVariantPaletteName(draft.sourceName),
      filamentIds,
      saved: false,
    });
    const sourceIndex = app.state.palette.deck.findIndex(
      entry => entry.id === draft.sourceCardId,
    );
    if (sourceIndex >= 0) app.state.palette.deck.splice(sourceIndex + 1, 0, card);
    else app.state.palette.deck.push(card);

    if (app.state.solve.solveMode !== "batch") {
      app.state.palette.activeDeckId = card.id;
    }
    app.state.palette.manualVariantDraft = null;
    app.state.palette.manualSlots = [];
    app.commands.renderCreationTab();
    app.commands.updateRail();
    await app.commands.syncConfigToServer();
    app.commands.showToast(`Added "${card.name}" to the deck`, "success");
    app.events.emit("palette.deck.updated", {
      action: "added",
      card,
      sourceCardId: draft.sourceCardId,
    });
    return card;
  }

function handleManualSecondaryAction() {
    if (app.state.palette.manualVariantDraft) {
      return app.commands.cancelPaletteVariant();
    }
    app.commands.clearManualSlots();
    return true;
  }

function deckCardMenuItems() {
    return [...(deckCardMenu?.querySelectorAll("[data-deck-card-action]") || [])]
      .filter(item => !item.hidden);
  }

function positionDeckCardMenu() {
    if (!deckCardMenu || !deckCardMenuAnchor || deckCardMenu.hidden) return;
    const rect = deckCardMenuAnchor.getBoundingClientRect();
    const margin = 8;
    const gap = 8;
    const width = deckCardMenu.offsetWidth;
    const height = deckCardMenu.offsetHeight;
    const viewportWidth = deckCardMenuViewport?.innerWidth
      ?? document.documentElement.clientWidth;
    const viewportHeight = deckCardMenuViewport?.innerHeight
      ?? document.documentElement.clientHeight;
    let left = rect.right + gap;
    if (left + width > viewportWidth - margin) {
      left = Math.max(margin, rect.left - width - gap);
    }
    const top = Math.max(
      margin,
      Math.min(rect.top - 4, viewportHeight - height - margin),
    );
    deckCardMenu.style.left = `${Math.round(left)}px`;
    deckCardMenu.style.top = `${Math.round(top)}px`;
  }

function closeDeckCardMenu({ restoreFocus = false } = {}) {
    if (!deckCardMenu || deckCardMenu.hidden) return;
    deckCardMenu.hidden = true;
    if (deckCardMenuAnchor) {
      deckCardMenuAnchor.setAttribute("aria-expanded", "false");
      if (restoreFocus && document.body.contains(deckCardMenuAnchor)) {
        deckCardMenuAnchor.focus();
      }
    }
    deckCardMenuAnchor = null;
    deckCardMenuCardId = null;
  }

function openDeckCardMenu(cardId, anchor, { focus = "first" } = {}) {
    const card = app.state.palette.deck.find(entry => entry.id === cardId);
    if (!card || !deckCardMenu || !anchor) return false;
    app.commands.closeDeckCardMenu();
    deckCardMenuAnchor = anchor;
    deckCardMenuCardId = card.id;
    deckCardMenu.setAttribute("aria-label", `Palette actions for ${card.name}`);
    const saveItem = deckCardMenu.querySelector('[data-deck-card-action="save"]');
    if (saveItem) saveItem.hidden = !!card.saved;
    deckCardMenu.hidden = false;
    anchor.setAttribute("aria-expanded", "true");
    app.commands.hideRailDeckHoverPreview();
    app.commands.positionDeckCardMenu();
    const items = app.commands.deckCardMenuItems();
    if (items.length) {
      (focus === "last" ? items.at(-1) : items[0]).focus();
    }
    return true;
  }

function toggleDeckCardMenu(cardId, anchor) {
    if (
      !deckCardMenu?.hidden
      && deckCardMenuCardId === cardId
      && deckCardMenuAnchor === anchor
    ) {
      app.commands.closeDeckCardMenu({ restoreFocus: true });
      return false;
    }
    return app.commands.openDeckCardMenu(cardId, anchor);
  }

function handleDeckCardMenuKeydown(event) {
    const items = app.commands.deckCardMenuItems();
    if (!items.length) return;
    const currentIndex = Math.max(0, items.indexOf(event.target));
    let nextIndex = null;
    if (event.key === "ArrowDown") nextIndex = (currentIndex + 1) % items.length;
    if (event.key === "ArrowUp") nextIndex = (currentIndex - 1 + items.length) % items.length;
    if (event.key === "Home") nextIndex = 0;
    if (event.key === "End") nextIndex = items.length - 1;
    if (nextIndex !== null) {
      event.preventDefault();
      items[nextIndex].focus();
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      app.commands.closeDeckCardMenu({ restoreFocus: true });
      return;
    }
    if (event.key === "Tab") app.commands.closeDeckCardMenu();
  }

function initializeDeckCardMenuController({
    viewport = window,
    documentEvents = document,
  } = {}) {
    deckCardMenu = app.state.ui.$("#deckCardMenu");
    deckCardMenuViewport = viewport;
    if (deckCardMenu) {
      app.lifecycle.listen(deckCardMenu, "click", async event => {
        const item = event.target.closest?.("[data-deck-card-action]");
        if (!item || !deckCardMenuCardId) return;
        const cardId = deckCardMenuCardId;
        const action = item.dataset.deckCardAction;
        app.commands.closeDeckCardMenu();
        if (action === "variant") await app.commands.beginPaletteVariant(cardId);
        if (action === "save") await app.commands.saveDeckCard(cardId);
      });
      app.lifecycle.listen(
        deckCardMenu,
        "keydown",
        app.commands.handleDeckCardMenuKeydown,
      );
    }
    app.lifecycle.listen(documentEvents, "pointerdown", event => {
      if (
        deckCardMenu?.hidden
        || deckCardMenu?.contains(event.target)
        || deckCardMenuAnchor?.contains(event.target)
      ) return;
      app.commands.closeDeckCardMenu();
    });
    app.lifecycle.listen(viewport, "resize", () => app.commands.closeDeckCardMenu());
    app.lifecycle.listen(
      viewport,
      "scroll",
      () => app.commands.closeDeckCardMenu(),
      true,
    );
  }

function activateDeckCard(cardId, { sync = true } = {}) {
    app.state.palette.activeDeckId = cardId;
    app.commands.renderDeckCards();
    app.commands.updateRail();
    if (sync) app.commands.syncConfigToServer();
    app.events.emit("palette.deck.activated", { cardId });
  }

function setActiveDeckCard(cardId) {
    app.commands.activateDeckCard(cardId);
  }

async function removeDeckCard(cardId) {
    if (
      app.state.solve.batchDeckLocked
      && app.state.solve.batchLockedDeckIds.has(cardId)
    ) {
      app.commands.showToast("This palette is locked while its batch is running.", "warn");
      return false;
    }
    const removedCard = app.state.palette.deck.find(d => d.id === cardId) || null;
    app.state.palette.deck = app.state.palette.deck.filter(d => d.id !== cardId);
    app.commands.removeBatchDeckSelection(cardId);
    if (app.state.palette.activeDeckId === cardId) {
      app.state.palette.activeDeckId = app.state.palette.deck.length > 0 ? app.state.palette.deck[0].id : null;
    }
    app.commands.renderDeckCards();
    app.commands.updateRail();
    app.commands.syncConfigToServer();
    app.events.emit("palette.deck.updated", {
      action: "removed",
      cardId,
      card: removedCard,
    });
    return true;
  }

function promoteStagedCard(cardId) {
    const idx = app.state.palette.stagingDeck.findIndex(c => c.id === cardId);
    if (idx < 0) return;
    const [card] = app.state.palette.stagingDeck.splice(idx, 1);
    app.state.palette.deck.push(card);
    // Activate only if it becomes the sole persistent card (matches mint/load); never steals
    // an existing active selection.
    if (app.state.palette.deck.length === 1) app.state.palette.activeDeckId = card.id;
    app.commands.renderDeckCards();
    app.commands.updateRail();
    app.commands.syncConfigToServer();
    app.commands.showToast(`Promoted "${card.name}" to the deck`, "success");
    app.events.emit("palette.deck.updated", { action: "added", card });
  }

function removeStagingCard(cardId) {
    app.state.palette.stagingDeck = app.state.palette.stagingDeck.filter(c => c.id !== cardId);
    app.commands.renderDeckCards();
  }

function syncStagingClearButton() {
    const button = app.state.ui.$("#clearDeckBtn");
    if (!button) return;
    const empty = app.state.palette.stagingDeck.length === 0;
    if (empty && app.state.palette.stagingClearConfirmPending) {
      app.commands.resetStagingClearConfirm({ sync: false });
    }
    const armed = app.state.palette.stagingClearConfirmPending && !empty;
    button.disabled = empty;
    button.setAttribute("aria-disabled", empty ? "true" : "false");
    button.textContent = armed ? "Confirm?" : "Clear";
    button.classList.toggle("confirm-pending", armed);
    button.classList.remove("danger");
    button.title = armed
      ? "Click again to clear all suggested palettes"
      : empty
        ? "No suggested palettes to clear"
        : "Clear all suggested palettes";
    button.setAttribute(
      "aria-label",
      armed
        ? "Confirm clearing all suggested palettes"
        : empty
          ? "No suggested palettes to clear"
          : "Clear suggested palettes",
    );
  }

function resetStagingClearConfirm({ sync = true } = {}) {
    if (app.state.palette.stagingClearConfirmTimer) {
      clearTimeout(app.state.palette.stagingClearConfirmTimer);
      app.state.palette.stagingClearConfirmTimer = null;
    }
    app.state.palette.stagingClearConfirmPending = false;
    if (sync) app.commands.syncStagingClearButton();
  }

function armStagingClearConfirm() {
    if (app.state.palette.stagingDeck.length === 0) {
      app.commands.resetStagingClearConfirm();
      return;
    }
    if (app.state.palette.stagingClearConfirmTimer) {
      clearTimeout(app.state.palette.stagingClearConfirmTimer);
    }
    app.state.palette.stagingClearConfirmPending = true;
    app.commands.syncStagingClearButton();
    app.state.palette.stagingClearConfirmTimer = setTimeout(() => {
      app.state.palette.stagingClearConfirmTimer = null;
      app.state.palette.stagingClearConfirmPending = false;
      app.commands.syncStagingClearButton();
    }, 3000);
  }

function handleStagingClearClick() {
    if (app.state.palette.stagingDeck.length === 0) {
      app.commands.resetStagingClearConfirm();
      return;
    }
    if (!app.state.palette.stagingClearConfirmPending) {
      app.commands.armStagingClearConfirm();
      return;
    }
    app.commands.resetStagingClearConfirm({ sync: false });
    app.state.palette.stagingDeck = [];
    app.commands.renderCreationTab();
    app.commands.syncStagingClearButton();
  }

async function clearPaletteDeck({ force = false } = {}) {
    if (app.state.solve.batchDeckLocked && !force) {
      app.commands.showToast("The Palette Deck cannot be cleared while a batch is running.", "warn");
      return false;
    }
    if (!app.state.palette.deck.length) return false;
    const previousDeck = app.commands._cloneValue(app.state.palette.deck);
    const previousActiveId = app.state.palette.activeDeckId;
    const previousBatchSelection = new Set(app.state.solve.batchSelectedDeckIds);
    app.state.palette.deck = [];
    app.state.palette.activeDeckId = null;
    app.state.solve.batchSelectedDeckIds.clear();
    app.commands.renderDeckCards();
    app.commands.updateRail();
    try {
      await app.commands.syncConfigToServer({ throwOnError: true });
      if (!force) app.events.emit("palette.deck.cleared", { cardIds: previousDeck.map(card => card.id) });
      return true;
    } catch (error) {
      app.state.palette.deck = previousDeck;
      app.state.palette.activeDeckId = previousActiveId;
      app.state.solve.batchSelectedDeckIds = previousBatchSelection;
      app.commands.renderDeckCards();
      app.commands.updateRail();
      if (!force) app.commands.showToast(`Clear failed: ${error.message}`, "error");
      return false;
    }
  }

async function saveDeckCard(cardId) {
    const card = app.state.palette.deck.find(d => d.id === cardId);
    if (!card) return;
    const alias = await app.commands.showPaletteSaveModal(card.name);
    if (alias === null) return; // cancelled
    const name = alias || card.name;
    if (app.commands.authorizeGuideDurableMutation && !app.commands.authorizeGuideDurableMutation("palette.saved.create", {
      deck_card_id: card.id,
      palette_signature: [...card.filament_ids],
      name,
    })) return;
    const current = app.state.palette.savedPalettesData || { palettes: [] };
    const proposed = app.commands._cloneValue(current);
    const existing = proposed.palettes.findIndex(p => p.id === card.id);
    const entry = { id: card.id, name, filament_ids: [...card.filament_ids] };
    if (existing >= 0) {
      proposed.palettes[existing] = entry;
    } else {
      proposed.palettes.push(entry);
    }
    try {
      const persist = () => app.api.savePalettesToServer(proposed);
      if (app.commands.performGuideDurableMutation) {
        await app.commands.performGuideDurableMutation({
          direction: "create",
          operationId: "saving-loading-palette",
          kind: "palette",
          id: entry.id,
          name,
          fingerprint: [...entry.filament_ids],
        }, persist);
      } else {
        await persist();
      }
      app.state.palette.savedPalettesData = proposed;
      card.name = name;
      card.saved = true;
      card.saved_source_id = entry.id;
      app.commands.showToast(`Saved "${card.name}"`, "success");
      app.commands.renderDeckCards();
      app.events.emit("palette.saved.created", {
        deckCardId: card.id,
        savedRecordId: entry.id,
        name,
        filamentIds: [...entry.filament_ids],
      });
    } catch (err) {
      app.commands.showToast(`Save failed: ${err.message}`, "error");
    }
  }

async function loadSavedPalettes() {
    if (!app.state.session.apiConnected) return;
    try {
      app.state.palette.savedPalettesData = await app.api.fetchSavedPalettes();
    } catch { app.state.palette.savedPalettesData = { palettes: [] }; }
  }

function showLoadPaletteMenu(anchorBtn = null) {
    if (!app.state.palette.savedPalettesData || app.state.palette.savedPalettesData.palettes.length === 0) {
      app.commands.showToast("No saved palettes to load", "error");
      return;
    }
    const btn = anchorBtn || app.state.ui.$("#railLoadPaletteBtn");
    if (!btn) return;

    // Remove any existing popover
    const old = document.querySelector(".load-palette-popover");
    if (old) {
      const sameAnchor = old.dataset.anchorId && btn.id && old.dataset.anchorId === btn.id;
      old.remove();
      if (sameAnchor) return;
    }

    const pop = document.createElement("div");
    pop.className = "load-palette-popover surface-menu";
    if (btn.id) pop.dataset.anchorId = btn.id;
    const isRailAnchor = btn.id === "railLoadPaletteBtn";
    if (isRailAnchor) {
      pop.classList.add("is-rail-popout");
      const rect = btn.getBoundingClientRect();
      pop.style.position = "fixed";
      pop.style.top = `${Math.round(rect.top)}px`;
      pop.style.left = `${Math.round(rect.right + 10)}px`;
    }
    function renderPopoverItems() {
      if (!app.state.palette.savedPalettesData || app.state.palette.savedPalettesData.palettes.length === 0) {
        pop.remove();
        return;
      }
      pop.innerHTML = app.state.palette.savedPalettesData.palettes.map((p, i) => {
        const chips = p.filament_ids.map(fid => {
          const fil = app.commands.filamentById(fid);
          return `<span class="color-chip" style="background:${fil?.hex || '#ccc'};width:8px;height:12px;border-radius:2px;display:inline-block"></span>`;
        }).join("");
        return `<div class="load-palette-item surface-menu-item" data-index="${i}" data-saved-id="${app.commands.esc(p.id)}">
          <span class="load-palette-name">${app.commands.esc(p.name)}</span>
          <span class="load-palette-chips">${chips}</span>
          <span class="load-palette-delete" data-saved-id="${app.commands.esc(p.id)}" title="Delete saved palette" aria-label="Delete saved palette">${app.commands.xIconSvg()}</span>
        </div>`;
      }).join("");
      bindPopoverItems();
    }

    function bindPopoverItems() {
      pop.querySelectorAll(".load-palette-item").forEach(item => {
        item.addEventListener("click", (e) => {
          if (e.target.closest(".load-palette-delete")) return;
          pop.remove();
          app.commands.loadPaletteByIndex(parseInt(item.dataset.index));
        });
      });
      pop.querySelectorAll(".load-palette-delete").forEach(delBtn => {
        let confirmPending = false;
        delBtn.addEventListener("click", async (e) => {
          e.stopPropagation();
          if (confirmPending) {
            const savedId = delBtn.dataset.savedId;
            const record = app.state.palette.savedPalettesData.palettes.find(item => item.id === savedId);
            if (!record) return;
            if (app.commands.authorizeGuideDurableMutation && !app.commands.authorizeGuideDurableMutation("palette.saved.delete", {
              saved_record_id: record.id,
            })) return;
            const proposed = app.commands._cloneValue(app.state.palette.savedPalettesData);
            proposed.palettes = proposed.palettes.filter(item => item.id !== record.id);
            try {
              const persist = () => app.api.savePalettesToServer(proposed);
              if (app.commands.performGuideDurableMutation) {
                await app.commands.performGuideDurableMutation({
                  direction: "delete",
                  operationId: "saving-loading-palette",
                  kind: "palette",
                  id: record.id,
                  name: record.name,
                  fingerprint: [...record.filament_ids],
                }, persist);
              } else {
                await persist();
              }
              app.state.palette.savedPalettesData = proposed;
              for (const card of app.state.palette.deck) {
                if (card.saved_source_id === record.id || card.id === record.id) {
                  card.saved_source_id = null;
                  card.saved = false;
                }
              }
              app.commands.renderDeckCards();
              app.commands.showToast(`Deleted "${record.name}"`, "success");
              app.events.emit("palette.saved.deleted", { savedRecordId: record.id, name: record.name });
              renderPopoverItems();
            } catch (error) {
              app.commands.showToast(`Delete failed: ${error.message}`, "error");
            }
            return;
          }
          confirmPending = true;
          delBtn.textContent = "Del?";
          delBtn.classList.add("confirm-pending");
          setTimeout(() => {
            confirmPending = false;
            delBtn.innerHTML = app.commands.xIconSvg();
            delBtn.classList.remove("confirm-pending");
          }, 2000);
        });
      });
    }

    renderPopoverItems();
    const mount = isRailAnchor ? document.body : btn.parentElement;
    if (!mount) return;
    if (!isRailAnchor) mount.style.position = "relative";
    mount.appendChild(pop);
    // Close on outside click
    setTimeout(() => {
      document.addEventListener("click", function closer(e) {
        if (!pop.contains(e.target) && e.target !== btn) {
          pop.remove();
          document.removeEventListener("click", closer);
        }
      });
    }, 0);
  }

function loadPaletteByIndex(idx, { forceActive = true, sync = true, silent = false, allowUnavailable = false } = {}) {
    const saved = app.state.palette.savedPalettesData.palettes[idx];
    if (!saved) return null;
    const gatingIssues = app.commands.getPaletteGatingIssues(saved.filament_ids);
    if (!allowUnavailable && app.commands.paletteGatingIssueCount(gatingIssues)) {
      app.commands.showToast(app.commands.buildPaletteGatingMessage(gatingIssues, `Can't load “${saved.name}”.`), "error");
      return null;
    }
    const card = app.commands.createPaletteDeckCard({
      idPrefix: "loaded",
      name: saved.name,
      filamentIds: saved.filament_ids,
      saved: true,
    });
    card.saved_source_id = saved.id;
    const previousActiveId = app.state.palette.activeDeckId;
    app.state.palette.deck.push(card);
    if (forceActive || app.state.palette.deck.length === 1) app.state.palette.activeDeckId = card.id;
    app.commands.renderCreationTab();
    app.commands.updateRail();
    const emitLoaded = () => app.events.emit("palette.saved.loaded", {
      savedRecordId: saved.id,
      deckCardId: card.id,
      name: card.name,
    });
    const finishLoaded = () => {
      emitLoaded();
      if (!silent) app.commands.showToast(`Loaded "${card.name}"`, "success");
    };
    if (sync) void app.commands.syncConfigToServer({ throwOnError: true }).then(finishLoaded).catch(error => {
      app.state.palette.deck = app.state.palette.deck.filter(item => item.id !== card.id);
      if (app.state.palette.activeDeckId === card.id) app.state.palette.activeDeckId = previousActiveId;
      app.commands.renderCreationTab();
      app.commands.updateRail();
      app.commands.showToast(`Load failed: ${error.message}`, "error");
    });
    else finishLoaded();
    return card;
  }

function renderDeckCards() {
    const container = app.state.ui.$("#deckCards");
    app.commands.syncStagingClearButton();
    if (app.state.palette.stagingDeck.length === 0) {
      container.innerHTML = `<p class="muted-line palette-empty-msg">Auto-suggest palettes to stage them here</p>`;
      app.commands.renderRailDeck();
      return;
    }

    container.innerHTML = app.state.palette.stagingDeck.map((card) => {
      const chips = card.filament_ids.map((fid) => {
        const fil = app.commands.filamentById(fid);
        return `<span class="color-chip" style="background:${fil?.hex || '#ccc'}" title="${app.commands.escAttr(fil?.color_name || fid)}"></span>`;
      }).join("");
      const supportChips = app.commands.buildDeckSupportChipsHtml();
      let gamutHtml = "";
      if (card.gamut?.status === "checking") {
        const pct = card.gamut.pct != null ? ` · ${Math.round(card.gamut.pct)}%` : "";
        const elapsed = card.gamut.elapsed_s != null ? ` · ${Math.round(card.gamut.elapsed_s)}s` : "";
        gamutHtml = `<div class="deck-card-gamut rail-deck-card-meta"><span>${app.commands.esc(card.gamut.progress || "Checking gamut...")}${pct}${elapsed}</span></div>`;
      } else if (card.gamut?.status === "done") {
        const g = card.gamut;
        const oogPart = g.total_pixels > 0 ? ` &middot; ${g.n_out_of_gamut.toLocaleString()} OOG` : "";
        gamutHtml = `
          <div class="deck-card-gamut rail-deck-card-meta">
            <span>${app.commands.formatColorRmse(g)}${oogPart}</span>
          </div>
        `;
      } else if (card.gamut?.status === "error") {
        gamutHtml = `<div class="deck-card-gamut rail-deck-card-meta" style="color:var(--error-ink)"><span>Gamut check failed</span></div>`;
      }

      return `
        <div class="deck-card compact-deck-card" data-card-id="${card.id}">
          <div class="deck-card-header compact-deck-card-header">
            <span class="deck-card-title compact-deck-card-title" title="${app.commands.escAttr(card.name)}">${app.commands.esc(card.name)}</span>
            <div class="deck-card-actions compact-deck-card-actions">
              <button class="ghost-button xxs deck-promote-btn compact-deck-card-save" data-card-id="${card.id}" title="Add this palette to the palette deck">Add to Deck</button>
              <button class="ghost-button xxs deck-delete-btn compact-deck-card-remove" data-card-id="${card.id}" aria-label="Remove ${app.commands.escAttr(card.name)} from suggestions" title="Remove from suggestions">${app.commands.xIconSvg()}</button>
            </div>
          </div>
          <div class="deck-card-chips rail-deck-card-chips">
            <div class="deck-card-palette-chips rail-deck-palette-chips">${chips}</div>
            ${supportChips}
          </div>
          ${gamutHtml}
        </div>
      `;
    }).join("");

    // Staging-pad card actions: Promote (move to persistent deck) and remove. Staging cards are
    // never active and have no Save — Save lives on the persistent rail deck.
    container.querySelectorAll(".deck-promote-btn").forEach((btn) => {
      btn.addEventListener("click", (e) => { e.stopPropagation(); app.commands.promoteStagedCard(btn.dataset.cardId); });
    });
    container.querySelectorAll(".deck-delete-btn").forEach((btn) => {
      let confirmPending = false;
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        if (confirmPending) {
          app.commands.removeStagingCard(btn.dataset.cardId);
          return;
        }
        const originalLabel = btn.getAttribute("aria-label");
        confirmPending = true;
        btn.textContent = "!";
        btn.classList.add("confirm-pending");
        btn.title = "Click again to remove";
        btn.setAttribute("aria-label", `Confirm ${originalLabel}`);
        setTimeout(() => {
          confirmPending = false;
          btn.innerHTML = app.commands.xIconSvg();
          btn.classList.remove("confirm-pending");
          btn.title = "Remove from suggestions";
          btn.setAttribute("aria-label", originalLabel);
        }, 2000);
      });
    });
    app.commands.renderRailDeck();
  }

function renderRailDeck() {
    app.commands.closeDeckCardMenu();
    app.commands.hideRailDeckHoverPreview();
    const list = app.state.ui.$("#railDeckList");
    if (!list) return;
    const clearButton = app.state.ui.$("#railClearDeckBtn");
    if (clearButton) {
      const empty = app.state.palette.deck.length === 0;
      const locked = app.state.solve.batchDeckLocked;
      clearButton.disabled = empty || locked;
      clearButton.setAttribute("aria-disabled", empty || locked ? "true" : "false");
      clearButton.setAttribute(
        "aria-label",
        locked ? "Palette deck locked during batch solve" : empty ? "No palettes to clear" : "Clear palette deck",
      );
      clearButton.title = locked
        ? "The Palette Deck cannot be cleared while a batch is running"
        : empty
        ? "No palettes to clear"
        : "Remove all palettes from the persistent deck";
    }
    if (app.state.palette.deck.length === 0) {
      list.innerHTML = `<span class="rail-deck-empty">No palettes yet</span>`;
      list.removeAttribute("role");
      list.removeAttribute("aria-multiselectable");
      return;
    }
    const batchMode = app.state.solve.solveMode === "batch";
    list.setAttribute("role", "listbox");
    list.setAttribute("aria-label", batchMode ? "Palette Deck batch selection" : "Palette Deck");
    list.setAttribute("aria-multiselectable", batchMode ? "true" : "false");
    list.innerHTML = app.state.palette.deck.map((card) => {
      const isActive = !batchMode && card.id === app.state.palette.activeDeckId;
      const isBatchSelected = batchMode && app.state.solve.batchSelectedDeckIds.has(card.id);
      const removalLocked = app.state.solve.batchDeckLocked
        && app.state.solve.batchLockedDeckIds.has(card.id);
      const chips = card.filament_ids.map((fid) => {
        const fil = app.commands.filamentById(fid);
        return `<span class="color-chip" style="background:${fil?.hex || '#ccc'}"></span>`;
      }).join("");
      const supportChips = app.commands.buildDeckSupportChipsHtml();
      const statusBits = [];
      if (card.gamut?.status === "checking") {
        statusBits.push("Checking gamut");
      } else if (card.gamut?.status === "done") {
        statusBits.push(app.commands.formatColorRmse(card.gamut));
      } else if (card.gamut?.status === "error") {
        statusBits.push("Gamut failed");
      }
      const tags = [
        card.saved ? `<span class="rail-deck-tag is-saved">Saved</span>` : "",
      ].filter(Boolean).join("");
      return `<div class="rail-deck-card compact-deck-card${isActive ? " is-active" : ""}${isBatchSelected ? " is-batch-selected" : ""}" data-card-id="${card.id}" role="option" tabindex="0" aria-selected="${batchMode ? (isBatchSelected ? "true" : "false") : (isActive ? "true" : "false")}">
        <div class="rail-deck-card-header compact-deck-card-header">
          <div class="rail-deck-card-titlebar">
            ${batchMode ? `<span class="rail-deck-batch-check" aria-hidden="true">${isBatchSelected ? "✓" : ""}</span>` : ""}
            <span class="rail-deck-card-title compact-deck-card-title" title="${app.commands.escAttr(card.name)}">${app.commands.esc(card.name)}</span>
          </div>
          <div class="rail-deck-card-actions compact-deck-card-actions">
            ${tags}
            <button class="ghost-button xxs rail-deck-menu-button" type="button" data-card-id="${card.id}"
                    aria-haspopup="menu" aria-expanded="false" aria-controls="deckCardMenu"
                    aria-label="Palette actions for ${app.commands.escAttr(card.name)}"
                    title="Palette actions">⋯</button>
            <button class="ghost-button xxs rail-deck-remove compact-deck-card-remove" data-card-id="${card.id}" title="${removalLocked ? "Locked while this batch is running" : "Remove from deck"}" aria-label="${removalLocked ? `Cannot remove ${app.commands.escAttr(card.name)} while its batch is running` : `Remove ${app.commands.escAttr(card.name)}`}"${removalLocked ? " disabled aria-disabled=\"true\"" : ""}>${app.commands.xIconSvg()}</button>
          </div>
        </div>
        <div class="rail-deck-card-chips">
          <div class="rail-deck-palette-chips">${chips}</div>
          ${supportChips}
        </div>
        <div class="rail-deck-card-meta">
          ${statusBits.map((bit) => `<span>${app.commands.esc(bit)}</span>`).join("")}
        </div>
      </div>`;
    }).join("");
    list.querySelectorAll(".rail-deck-card").forEach((el) => {
      const select = () => {
        if (app.state.solve.solveMode === "batch") {
          app.commands.toggleBatchDeckSelection(el.dataset.cardId);
        } else {
          app.commands.setActiveDeckCard(el.dataset.cardId);
        }
      };
      el.addEventListener("click", event => {
        if (event.target.closest?.(".rail-deck-card-actions")) return;
        select();
      });
      el.addEventListener("keydown", event => {
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        select();
      });
      el.addEventListener("mousemove", (e) => app.commands.handleRailDeckCardHoverMove(el, e));
      el.addEventListener("mouseleave", () => app.commands.scheduleHideRailDeckHoverPreview());
    });
    list.querySelectorAll(".rail-deck-menu-button").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        app.commands.toggleDeckCardMenu(btn.dataset.cardId, btn);
      });
      btn.addEventListener("keydown", event => {
        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
          event.preventDefault();
          event.stopPropagation();
          app.commands.openDeckCardMenu(
            btn.dataset.cardId,
            btn,
            { focus: event.key === "ArrowUp" ? "last" : "first" },
          );
        } else if (event.key === "Escape") {
          event.preventDefault();
          event.stopPropagation();
          app.commands.closeDeckCardMenu({ restoreFocus: true });
        }
      });
    });
    list.querySelectorAll(".rail-deck-remove").forEach((btn) => {
      let confirmPending = false;
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        if (btn.disabled) return;
        if (confirmPending) {
          app.commands.removeDeckCard(btn.dataset.cardId);
          return;
        }
        confirmPending = true;
        btn.textContent = "!";
        btn.classList.add("confirm-pending");
        btn.title = "Click again to remove";
        setTimeout(() => {
          confirmPending = false;
          btn.innerHTML = app.commands.xIconSvg();
          btn.classList.remove("confirm-pending");
          btn.title = "Remove from deck";
        }, 1800);
      });
    });
  }

function clearRailDeckHoverTimer() {
    if (app.state.palette.railDeckHoverTimer) {
      clearTimeout(app.state.palette.railDeckHoverTimer);
      app.state.palette.railDeckHoverTimer = null;
    }
    app.state.palette.railDeckHoverPendingCardId = null;
  }

function clearRailDeckHoverCloseTimer() {
    if (app.state.palette.railDeckHoverCloseTimer) {
      clearTimeout(app.state.palette.railDeckHoverCloseTimer);
      app.state.palette.railDeckHoverCloseTimer = null;
    }
  }

function scheduleRailDeckHoverPreview(cardId, anchorEl) {
    if (app.state.palette.railDeckHoverPreviewCardId === cardId && app.state.palette.railDeckHoverPreviewEl?.classList.contains("is-visible")) return;
    if (app.state.palette.railDeckHoverPendingCardId === cardId) return;
    app.commands.clearRailDeckHoverTimer();
    app.commands.clearRailDeckHoverCloseTimer();
    app.state.palette.railDeckHoverPendingCardId = cardId;
    app.state.palette.railDeckHoverTimer = setTimeout(() => {
      app.state.palette.railDeckHoverPendingCardId = null;
      app.commands.showRailDeckHoverPreview(cardId, anchorEl);
    }, 420);
  }

function isRailDeckHoverBlockedTarget(target) {
    return Boolean(target?.closest?.(".rail-deck-card-actions button, .rail-deck-card-actions [role='button'], .rail-deck-card-actions a"));
  }

function handleRailDeckCardHoverMove(cardEl, event) {
    if (app.commands.isRailDeckHoverBlockedTarget(event.target)) {
      app.commands.hideRailDeckHoverPreview();
      return;
    }
    app.commands.scheduleRailDeckHoverPreview(cardEl.dataset.cardId, cardEl);
  }

function scheduleHideRailDeckHoverPreview(delayMs = 180) {
    app.commands.clearRailDeckHoverTimer();
    app.commands.clearRailDeckHoverCloseTimer();
    app.state.palette.railDeckHoverCloseTimer = setTimeout(() => {
      app.commands.hideRailDeckHoverPreview();
    }, delayMs);
  }

function buildDeckSupportChipsHtml() {
    const baseId = app.state.settings.config.base_filament || app.state.session.DEFAULT_BASE_FILAMENT;
    const supportEntries = baseId ? [{ id: baseId, role: "White Base/Cap" }] : [];
    if (!supportEntries.length) return "";

    const slotHtml = Array.from({ length: 1 }, (_, index) => {
      const entry = supportEntries[index];
      if (!entry) {
        return `<span class="deck-support-slot is-empty" aria-hidden="true"></span>`;
      }
      const fil = app.commands.filamentById(entry.id);
      const label = fil ? `${fil.manufacturer} ${fil.color_name}` : (entry.id || "Unset");
      const hex = fil?.hex || "#ccc";
      return `<span class="deck-support-slot is-filled${app.commands.isLightHex(hex) ? " is-light" : ""}" title="${app.commands.esc(`${entry.role}: ${label}`)}" aria-label="${app.commands.esc(`${entry.role}: ${label}`)}">
        <span class="color-chip deck-support-chip" style="background:${hex}"></span>
      </span>`;
    }).join("");

    return `<div class="deck-support-tray" aria-label="Reserved white base and cap filament">${slotHtml}</div>`;
  }

function railHoverFilamentLabel(fil, fallback = "Unset") {
    const colorName = fil?.color_name || fallback;
    const manufacturer = fil?.manufacturer || "";
    if (!manufacturer) return colorName;
    return colorName.toLocaleLowerCase().startsWith(manufacturer.toLocaleLowerCase())
      ? colorName
      : `${manufacturer} ${colorName}`;
  }

function buildRailDeckHoverPreview(card) {
    const baseId = app.state.settings.config.base_filament || app.state.session.DEFAULT_BASE_FILAMENT;
    let metricHtml = "";
    if (card.gamut?.status === "checking") {
      metricHtml += `<span>Checking gamut</span>`;
    } else if (card.gamut?.status === "done") {
      metricHtml += `<span>${app.commands.formatColorRmse(card.gamut)}</span>`;
    } else if (card.gamut?.status === "error") {
      metricHtml += `<span>Gamut check failed</span>`;
    }

    const filamentRows = card.filament_ids.map((fid) => {
      const fil = app.commands.filamentById(fid);
      const colorName = fil?.color_name || fid;
      const title = app.commands.railHoverFilamentLabel(fil, fid);
      return `<div class="rail-hover-filament-row" title="${app.commands.esc(title)}">
        <span class="color-chip" style="background:${fil?.hex || '#ccc'}"></span>
        <span class="rail-hover-filament-copy">
          <span class="rail-hover-filament-name">${app.commands.esc(colorName)}</span>
        </span>
      </div>`;
    }).join("");

    const supportEntries = baseId ? [{ id: baseId, badges: ["Base/Cap"] }] : [];
    const supportRows = supportEntries.map((entry) => {
      const fil = app.commands.filamentById(entry.id);
      const colorName = fil?.color_name || entry.id || "Unset";
      const title = app.commands.railHoverFilamentLabel(fil, entry.id || "Unset");
      const badges = entry.badges.map((label) => `<span class="rail-hover-filament-badge">${app.commands.esc(label)}</span>`).join("");
      return `<div class="rail-hover-filament-row rail-hover-filament-row-support" title="${app.commands.esc(title)}">
        <span class="color-chip" style="background:${fil?.hex || '#ccc'}"></span>
        <span class="rail-hover-filament-copy">
          <span class="rail-hover-filament-name">${app.commands.esc(colorName)}</span>
        </span>
        <span class="rail-hover-filament-badges">${badges}</span>
      </div>`;
    }).join("");
    const filamentStack = filamentRows + supportRows;

    return `
      <div class="rail-hover-head">
        <div class="rail-hover-title-row">
          <div class="rail-hover-title">${app.commands.esc(card.name)}</div>
        </div>
      </div>
      ${metricHtml ? `<div class="rail-hover-metrics">${metricHtml}</div>` : ""}
      <div class="rail-hover-filament-list">${filamentStack}</div>
    `;
  }

function positionRailDeckHoverPreview(panel, anchorEl) {
    const rect = anchorEl.getBoundingClientRect();
    const gap = 12;
    const viewportPad = 12;
    const panelRect = panel.getBoundingClientRect();
    let left = rect.right + gap;
    if (left + panelRect.width > window.innerWidth - viewportPad) {
      left = Math.max(viewportPad, rect.left - panelRect.width - gap);
    }
    let top = rect.top - 4;
    const maxTop = window.innerHeight - panelRect.height - viewportPad;
    top = Math.max(viewportPad, Math.min(top, maxTop));
    panel.style.left = `${Math.round(left)}px`;
    panel.style.top = `${Math.round(top)}px`;
  }

function showRailDeckHoverPreview(cardId, anchorEl) {
    app.commands.clearRailDeckHoverTimer();
    app.commands.clearRailDeckHoverCloseTimer();
    const card = app.state.palette.deck.find((entry) => entry.id === cardId);
    if (!card || !anchorEl || !document.body.contains(anchorEl)) return;

    if (!app.state.palette.railDeckHoverPreviewEl) {
      app.state.palette.railDeckHoverPreviewEl = document.createElement("div");
      app.state.palette.railDeckHoverPreviewEl.className = "rail-deck-hover-preview";
      app.state.palette.railDeckHoverPreviewEl.addEventListener("mouseenter", () => app.commands.clearRailDeckHoverCloseTimer());
      app.state.palette.railDeckHoverPreviewEl.addEventListener("mouseleave", () => app.commands.scheduleHideRailDeckHoverPreview(120));
    }

    app.state.palette.railDeckHoverPreviewCardId = cardId;
    app.state.palette.railDeckHoverPreviewEl.innerHTML = app.commands.buildRailDeckHoverPreview(card);
    if (!app.state.palette.railDeckHoverPreviewEl.parentElement) {
      document.body.appendChild(app.state.palette.railDeckHoverPreviewEl);
    }
    app.state.palette.railDeckHoverPreviewEl.classList.remove("is-visible");
    app.commands.positionRailDeckHoverPreview(app.state.palette.railDeckHoverPreviewEl, anchorEl);
    requestAnimationFrame(() => {
      if (app.state.palette.railDeckHoverPreviewEl) app.state.palette.railDeckHoverPreviewEl.classList.add("is-visible");
    });
  }

function hideRailDeckHoverPreview() {
    app.commands.clearRailDeckHoverTimer();
    app.commands.clearRailDeckHoverCloseTimer();
    if (app.state.palette.railDeckHoverPreviewEl) {
      app.state.palette.railDeckHoverPreviewEl.remove();
      app.state.palette.railDeckHoverPreviewEl = null;
    }
    app.state.palette.railDeckHoverPreviewCardId = null;
  }

  Object.assign(app.commands, {
    normalizeSupportFromLoadedConfig,
    normalizeSupportFromPaletteRecord,
    selectLoadedPalette,
    getPaletteGatingIssues,
    paletteGatingIssueCount,
    buildPaletteGatingMessage,
    makePaletteSignature,
    paletteSignaturesEqual,
    signatureForPaletteRecord,
    findMatchingDeckCard,
    findMatchingSavedPaletteIndex,
    chooseLoadedPaletteRestoreAction,
    createPaletteDeckCard,
    addLoadedAdHocPaletteToDeck,
    mintPaletteToDeck,
    manualVariantFilamentIds,
    manualVariantHasChanged,
    addManualFilament,
    removeManualFilamentAt,
    nextVariantPaletteName,
    beginPaletteVariant,
    cancelPaletteVariant,
    commitPaletteVariant,
    handleManualSecondaryAction,
    deckCardMenuItems,
    positionDeckCardMenu,
    closeDeckCardMenu,
    openDeckCardMenu,
    toggleDeckCardMenu,
    handleDeckCardMenuKeydown,
    initializeDeckCardMenuController,
    activateDeckCard,
    setActiveDeckCard,
    removeDeckCard,
    promoteStagedCard,
    removeStagingCard,
    syncStagingClearButton,
    resetStagingClearConfirm,
    armStagingClearConfirm,
    handleStagingClearClick,
    saveDeckCard,
    clearPaletteDeck,
    loadSavedPalettes,
    showLoadPaletteMenu,
    loadPaletteByIndex,
    renderDeckCards,
    renderRailDeck,
    clearRailDeckHoverTimer,
    clearRailDeckHoverCloseTimer,
    scheduleRailDeckHoverPreview,
    isRailDeckHoverBlockedTarget,
    handleRailDeckCardHoverMove,
    scheduleHideRailDeckHoverPreview,
    buildDeckSupportChipsHtml,
    railHoverFilamentLabel,
    buildRailDeckHoverPreview,
    positionRailDeckHoverPreview,
    showRailDeckHoverPreview,
    hideRailDeckHoverPreview,
  });
}
