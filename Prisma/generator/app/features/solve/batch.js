import { assertPolledJobIdentity } from "../../core/polling.js";

/**
 * Own the main Solve mode selector and deck-selected batch orchestration.
 * @param {import("../../core/types.js").ApplicationContext} app
 */
export function installFeaturesSolveBatch(app) {
  let menuButton = null;
  let menu = null;
  let viewportTarget = null;

  function batchIsActive(status = app.state.solve.solveStatus) {
    return status?.job_kind === "palette_batch"
      && ["running", "cancelling"].includes(status.status);
  }

  function selectedBatchDeckCards() {
    const selected = app.state.solve.batchSelectedDeckIds;
    return app.state.palette.deck.filter(card => selected.has(card.id));
  }

  function pruneBatchDeckSelection() {
    const valid = new Set(app.state.palette.deck.map(card => card.id));
    for (const id of app.state.solve.batchSelectedDeckIds) {
      if (!valid.has(id)) app.state.solve.batchSelectedDeckIds.delete(id);
    }
  }

  function solveModeMenuItems() {
    return [...(menu?.querySelectorAll("[data-solve-mode]") || [])];
  }

  function solveModeControlsLocked() {
    return !!(
      app.state.solve.batchDeckLocked
      || app.state.solve.paletteBatchStartPending
      || app.state.solve.solveStartPending
    );
  }

  function positionSolveModeMenu() {
    if (!menuButton || !menu || menu.hidden) return;
    const rect = menuButton.getBoundingClientRect();
    const margin = 8;
    const gap = 6;
    const viewportWidth = viewportTarget?.innerWidth ?? document.documentElement.clientWidth;
    const viewportHeight = viewportTarget?.innerHeight ?? document.documentElement.clientHeight;
    const left = Math.max(
      margin,
      Math.min(rect.right - menu.offsetWidth, viewportWidth - menu.offsetWidth - margin),
    );
    const below = rect.bottom + gap;
    const top = below + menu.offsetHeight <= viewportHeight - margin
      ? below
      : Math.max(margin, rect.top - menu.offsetHeight - gap);
    menu.style.left = `${Math.round(left)}px`;
    menu.style.top = `${Math.round(top)}px`;
  }

  function closeSolveModeMenu({ restoreFocus = false } = {}) {
    if (!menu || menu.hidden) return;
    menu.hidden = true;
    app.state.solve.solveModeMenuOpen = false;
    menuButton?.setAttribute("aria-expanded", "false");
    if (restoreFocus) menuButton?.focus();
  }

  function openSolveModeMenu({ focus = "checked" } = {}) {
    if (!menu || !menuButton || solveModeControlsLocked()) return;
    menu.hidden = false;
    app.state.solve.solveModeMenuOpen = true;
    menuButton.setAttribute("aria-expanded", "true");
    positionSolveModeMenu();
    const items = solveModeMenuItems();
    if (!items.length) return;
    const target = focus === "first"
      ? items[0]
      : focus === "last"
        ? items.at(-1)
        : items.find(item => item.getAttribute("aria-checked") === "true") || items[0];
    target.focus({ preventScroll: true });
  }

  function toggleSolveModeMenu() {
    if (menu?.hidden) openSolveModeMenu();
    else closeSolveModeMenu({ restoreFocus: true });
  }

  function setSolveMode(mode, { recovery = false } = {}) {
    const normalized = mode === "batch" ? "batch" : "single";
    if (solveModeControlsLocked() && !recovery) return false;
    app.state.solve.solveMode = normalized;
    app.state.solve.batchRecoveryOwnsToolbar = recovery && normalized === "batch";
    if (
      normalized === "batch"
      && app.state.solve.batchSelectedDeckIds.size === 0
      && app.state.palette.activeDeckId
    ) {
      app.state.solve.batchSelectedDeckIds.add(app.state.palette.activeDeckId);
    }
    closeSolveModeMenu();
    app.commands.renderDeckCards();
    app.commands.syncSolveModeUi();
    app.commands.updateSolveReadiness();
    return true;
  }

  function syncSolveModeUi() {
    pruneBatchDeckSelection();
    const isBatch = app.state.solve.solveMode === "batch";
    const count = selectedBatchDeckCards().length;
    const main = app.state.ui.$("#startSolveBtn");
    if (main) {
      main.textContent = isBatch ? `Batch Solve (${count})` : "Solve";
      main.setAttribute("aria-label", isBatch ? `Batch Solve ${count} selected palettes` : "Solve active palette");
    }
    if (menuButton) {
      const modeLocked = solveModeControlsLocked();
      menuButton.disabled = modeLocked;
      menuButton.setAttribute("aria-disabled", modeLocked ? "true" : "false");
      menuButton.title = modeLocked
        ? "Solve mode is locked while a solve is starting or running"
        : "Choose solve mode";
      if (modeLocked) closeSolveModeMenu();
    }
    const modeLocked = solveModeControlsLocked();
    solveModeMenuItems().forEach(item => {
      item.setAttribute("aria-checked", item.dataset.solveMode === app.state.solve.solveMode ? "true" : "false");
      item.disabled = modeLocked;
    });
  }

  function setBatchDeckLocked(locked, deckIds = []) {
    app.state.solve.batchDeckLocked = !!locked;
    app.state.solve.batchLockedDeckIds = locked ? new Set(deckIds) : new Set();
    if (!locked) app.state.solve.paletteBatchStartPending = false;
    app.commands.renderDeckCards();
    app.commands.syncSolveModeUi();
    app.commands.updateSolveReadiness();
  }

  function toggleBatchDeckSelection(cardId) {
    if (app.state.solve.solveMode !== "batch") return false;
    if (app.state.solve.batchDeckLocked) {
      app.commands.showToast("Palette selection is locked while the batch is running.", "warn");
      return false;
    }
    const card = app.state.palette.deck.find(entry => entry.id === cardId);
    if (!card) return false;
    const selected = app.state.solve.batchSelectedDeckIds;
    if (selected.has(cardId)) {
      selected.delete(cardId);
    } else if (selected.size >= 10) {
      app.commands.showToast("A batch can contain at most 10 palettes.", "warn");
      return false;
    } else {
      selected.add(cardId);
    }
    app.commands.renderDeckCards();
    app.commands.syncSolveModeUi();
    app.commands.updateSolveReadiness();
    return true;
  }

  function removeBatchDeckSelection(cardId) {
    app.state.solve.batchSelectedDeckIds.delete(cardId);
    app.state.solve.batchLockedDeckIds.delete(cardId);
  }

  function handleSolveModeMenuKeydown(event) {
    const items = solveModeMenuItems();
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
      closeSolveModeMenu({ restoreFocus: true });
      return;
    }
    if (event.key === "Tab") closeSolveModeMenu();
  }

  function initializeSolveModeController({ viewport = window, documentEvents = document } = {}) {
    menuButton = app.state.ui.$("#solveModeMenuBtn");
    menu = app.state.ui.$("#solveModeMenu");
    viewportTarget = viewport;
    app.commands.syncSolveModeUi();
    if (menuButton) {
      app.lifecycle.listen(menuButton, "click", toggleSolveModeMenu);
      app.lifecycle.listen(menuButton, "keydown", event => {
        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
          event.preventDefault();
          openSolveModeMenu({ focus: event.key === "ArrowDown" ? "first" : "last" });
        } else if (event.key === "Escape") {
          closeSolveModeMenu({ restoreFocus: true });
        }
      });
    }
    if (menu) {
      app.lifecycle.listen(menu, "click", event => {
        const item = event.target.closest?.("[data-solve-mode]");
        if (!item || item.disabled) return;
        setSolveMode(item.dataset.solveMode);
        menuButton?.focus();
      });
      app.lifecycle.listen(menu, "keydown", handleSolveModeMenuKeydown);
    }
    app.lifecycle.listen(documentEvents, "pointerdown", event => {
      if (menu?.hidden || menu?.contains(event.target) || menuButton?.contains(event.target)) return;
      closeSolveModeMenu();
    });
    app.lifecycle.listen(viewport, "resize", () => closeSolveModeMenu());
    app.lifecycle.listen(viewport, "scroll", () => closeSolveModeMenu());
  }

  function ensureBatchPreviewRuns(status) {
    const items = Array.isArray(status?.items) ? status.items : [];
    if (!items.length) return;
    app.state.solve.selectedRunIds.clear();
    for (const item of items) {
      let run = app.state.solve.solveRuns.find(entry => entry.id === item.result_id);
      if (!run) {
        const recipeContext = app.commands.buildSolveRecipeContext(
          item.palette || [],
          app.commands._currentSettingsSnapshot(),
        );
        run = app.commands.createSolveRun(
          item.palette || [],
          { ...app.state.settings.config, palette: [...(item.palette || [])] },
          recipeContext,
        );
        run.id = item.result_id;
        run.label = item.label || `Palette ${item.position}`;
        run.batch_job_id = status.job_id;
        run.batch_deck_card_id = item.deck_card_id;
        run.batch_position = item.position;
        run.batch_status = item.status || "queued";
        app.state.solve.solveRuns.push(run);
      }
      if (!["error", "cancelled"].includes(item.status)) {
        app.state.solve.selectedRunIds.add(run.id);
      }
    }
    app.commands.renderSolveTab();
    app.commands.updateRail();
  }

  async function fetchPaletteBatchItemResult(jobId, resultId) {
    if (app.state.solve.paletteBatchFetchedResultIds.has(resultId)) return null;
    if (app.state.solve.paletteBatchResultFetches.has(resultId)) {
      return app.state.solve.paletteBatchResultFetches.get(resultId);
    }
    const request = (async () => {
      const body = await app.api.getPaletteBatchResult(jobId, resultId);
      const run = app.state.solve.solveRuns.find(entry => entry.id === resultId);
      if (!run) return body;
      run.label = body.label || run.label;
      run.palette = [...(body.palette || run.palette || [])];
      run.config = app.commands._cloneValue(body.config || run.config || {});
      run.profile_ref = app.commands._cloneValue(body.profile_ref || {});
      run.profile_name_at_solve = body.profile_name_at_solve || null;
      run.is_profile_modified_at_solve = !!body.is_profile_modified_at_solve;
      run.recipe_snapshot = app.commands._cloneValue(body.recipe_snapshot || {});
      run.solve_elapsed_s = Number.isFinite(Number(body.elapsed_s))
        ? Math.max(0, Number(body.elapsed_s))
        : null;
      run.results = body.result || null;
      run.batch_status = "complete";
      run.batch_result_error = "";
      app.state.solve.paletteBatchFetchedResultIds.add(resultId);
      app.commands.renderSolveTab();
      app.commands.updateRail();
      return body;
    })();
    app.state.solve.paletteBatchResultFetches.set(resultId, request);
    try {
      return await request;
    } catch (error) {
      const run = app.state.solve.solveRuns.find(entry => entry.id === resultId);
      if (run) run.batch_result_error = error?.message || "Result could not be loaded";
      throw error;
    } finally {
      app.state.solve.paletteBatchResultFetches.delete(resultId);
    }
  }

  async function reconcilePaletteBatchStatus(status, { awaitResults = false } = {}) {
    const items = Array.isArray(status?.items) ? status.items : [];
    app.commands.ensureBatchPreviewRuns(status);
    const resultRequests = [];
    for (const item of items) {
      const run = app.state.solve.solveRuns.find(entry => entry.id === item.result_id);
      if (!run) continue;
      run.batch_status = item.status || run.batch_status;
      run.batch_error = item.error || "";
      if (["error", "cancelled"].includes(item.status)) {
        app.state.solve.selectedRunIds.delete(run.id);
      }
      if (item.result_available && !run.results) {
        const request = app.commands.fetchPaletteBatchItemResult(
          status.job_id,
          item.result_id,
        ).catch(error => {
          if (!run.batch_result_error_reported) {
            run.batch_result_error_reported = true;
            app.commands.showToast(`Could not load ${run.label}: ${error.message}`, "error");
          }
          return null;
        });
        resultRequests.push(request);
      }
    }
    if (awaitResults && resultRequests.length) await Promise.all(resultRequests);
    if (status.status === "cancelled") {
      const incompleteIds = new Set(
        items
          .filter(item => item.status === "cancelled")
          .map(item => item.result_id),
      );
      app.state.solve.solveRuns = app.state.solve.solveRuns.filter(
        run => !incompleteIds.has(run.id) || !!run.results,
      );
      incompleteIds.forEach(id => app.state.solve.selectedRunIds.delete(id));
    }
    app.commands.renderSolveTab();
    app.commands.updateRail();
  }

  function settleBatchToolbar(status) {
    const recovered = app.state.solve.batchRecoveryOwnsToolbar;
    setBatchDeckLocked(false);
    if (recovered) setSolveMode("single", { recovery: true });
    app.state.solve.batchRecoveryOwnsToolbar = false;
    app.commands.syncSolveModeUi();
    app.commands.updateSolveReadiness();
    return status;
  }

  function startPaletteBatchPolling(initialStatus = null) {
    if (app.state.solve.solvePollingOwner) app.state.solve.solvePollingOwner.cancelled = true;
    const pollingJobId = initialStatus?.job_id || app.state.solve.activeSolveJobId;
    if (!pollingJobId) return;
    const pollingOwner = { jobId: pollingJobId, cancelled: false };
    app.state.solve.solvePollingOwner = pollingOwner;
    void (async () => {
      try {
        if (initialStatus) {
          app.state.solve.solveStatus = initialStatus;
          await app.commands.reconcilePaletteBatchStatus(initialStatus);
          app.commands.renderSolveProgress();
        }
        const status = await app.services.pollJobUntilTerminal({
          jobId: pollingJobId,
          fetchStatus: () => app.api.getSolveStatus(),
          isTerminal: next => !["running", "cancelling"].includes(next.status),
          shouldContinue: () => (
            !pollingOwner.cancelled
            && app.state.solve.solvePollingOwner === pollingOwner
            && app.state.solve.activeSolveJobId === pollingJobId
          ),
          intervalMs: 500,
          onStatus: next => {
            assertPolledJobIdentity(next, pollingJobId);
            app.state.solve.solveStatus = next;
            void app.commands.reconcilePaletteBatchStatus(next);
            app.commands.renderSolveProgress();
          },
          onTransientError: () => {
            app.state.solve.solveStatus = {
              ...app.state.solve.solveStatus,
              progress: "Connection interrupted; retrying palette batch status...",
              progress_detail: {
                ...(app.state.solve.solveStatus.progress_detail || {}),
                stage_label: "Reconnecting to palette batch...",
              },
            };
            app.commands.renderSolveProgress();
          },
        });
        if (!status || app.state.solve.solvePollingOwner !== pollingOwner) return;
        assertPolledJobIdentity(status, pollingJobId);
        app.state.solve.solveStatus = status;
        app.state.solve.solveCancelPending = false;
        await app.commands.reconcilePaletteBatchStatus(status, { awaitResults: true });
        app.state.solve.activeSolveJobId = null;
        app.state.solve.activeSolveRunId = null;
        app.commands.renderSolveProgress();
        const succeeded = (status.items || []).filter(item => item.status === "complete").length;
        const failed = (status.items || []).filter(item => item.status === "error").length;
        if (status.status === "complete") {
          app.commands.showToast(`Palette batch complete: ${succeeded} solved`, "success");
        } else if (status.status === "partial") {
          app.commands.showToast(`Palette batch complete: ${succeeded} solved, ${failed} failed`, "warn");
        } else if (status.status === "cancelled") {
          app.commands.showToast(`Palette batch cancelled: ${succeeded} completed`, "warn");
        } else {
          app.commands.showToast(status.progress || "Palette batch failed", "error");
        }
        settleBatchToolbar(status);
      } catch (error) {
        if (app.state.solve.solvePollingOwner !== pollingOwner) return;
        app.commands.showToast(`Palette batch status failed: ${error.message}`, "error");
      } finally {
        if (app.state.solve.solvePollingOwner === pollingOwner) {
          app.state.solve.solvePollingOwner = null;
        }
        app.state.solve.paletteBatchStartPending = false;
        app.commands.syncSolveModeUi();
        app.commands.updateSolveReadiness();
      }
    })();
  }

  async function handleStartPaletteBatch({ throwOnError = false, confirmCost = true } = {}) {
    if (
      app.state.solve.paletteBatchStartPending
      || ["running", "cancelling"].includes(app.state.solve.solveStatus.status)
    ) return;
    if (!app.state.image.selectedImage) {
      app.commands.showToast("Load an image before starting a palette batch.", "warn");
      return;
    }
    pruneBatchDeckSelection();
    let cards = selectedBatchDeckCards();
    if (cards.length < 2 || cards.length > 10) {
      app.commands.showToast("Select between 2 and 10 Palette Deck cards for a batch.", "warn");
      return;
    }
    if (app.state.export.exportRunning) {
      app.commands.showToast("Please wait for meshing to finish.", "warn");
      return;
    }
    const gating = cards
      .map(card => ({ card, issues: app.commands.getPaletteGatingIssues(card.filament_ids) }))
      .filter(entry => app.commands.paletteGatingIssueCount(entry.issues));
    if (gating.length) {
      app.commands.showToast(
        app.commands.buildPaletteGatingMessage(
          gating[0].issues,
          `Can't batch solve "${gating[0].card.name}".`,
        ),
        "error",
      );
      return;
    }

    app.state.solve.paletteBatchStartPending = true;
    app.commands.syncSolveModeUi();
    app.commands.updateSolveReadiness();
    let acceptedStatus = null;
    try {
      try {
        await app.api.getExportStatus().then(status => {
          if (["running", "cancelling"].includes(status?.status)) {
            throw new Error("Please wait for meshing to finish.");
          }
        });
      } catch (error) {
        if (error?.message === "Please wait for meshing to finish.") throw error;
      }
      const preparedSettings = await app.commands.syncSolveSettings();
      if (!preparedSettings.proceed) return;
      const settingsIssues = app.commands.getSolveSettingsPreflightIssues();
      if (settingsIssues.length) {
        throw new Error(app.commands.buildSolveSettingsPreflightMessage(settingsIssues));
      }

      // Selection stays editable until the final cost confirmation. Re-read it
      // after asynchronous settings preparation so validation and execution
      // use the exact cards the user is about to confirm.
      pruneBatchDeckSelection();
      cards = selectedBatchDeckCards();
      if (cards.length < 2 || cards.length > 10) {
        throw new Error("Select between 2 and 10 Palette Deck cards for a batch.");
      }
      const refreshedGating = cards
        .map(card => ({ card, issues: app.commands.getPaletteGatingIssues(card.filament_ids) }))
        .filter(entry => app.commands.paletteGatingIssueCount(entry.issues));
      if (refreshedGating.length) {
        throw new Error(app.commands.buildPaletteGatingMessage(
          refreshedGating[0].issues,
          `Can't batch solve "${refreshedGating[0].card.name}".`,
        ));
      }
      for (const card of cards) {
        const check = await app.api.apiPost("/palette/validate", { palette: card.filament_ids });
        if (check?.valid === false) {
          throw new Error(`${card.name}: ${app.commands.buildUnsolvablePaletteMessage(check)}`);
        }
      }

      const preparedDimensions = await app.commands.syncSolveDimensionsWithGridRemediation({
        intent: "batch",
      });
      if (!preparedDimensions.proceed) return;

      const names = cards.map((card, index) => `${index + 1}. ${card.name}`).join("\n");
      const confirmed = !confirmCost || await app.commands.appConfirm(
          `Prisma will run ${cards.length} full production solves sequentially using the same frozen image and settings.\n\n${names}\n\nThis may take roughly ${cards.length} times a normal solve and use substantial temporary storage. Unsaved results are removed when Prisma restarts.`,
          {
            ok: `Solve ${cards.length} Palettes`,
            cancel: "Cancel",
            title: "Solve Palette Batch",
          },
        );
      if (!confirmed) return;

      setBatchDeckLocked(true, cards.map(card => card.id));
      const recipeContext = app.commands.buildSolveRecipeContext(
        cards[0].filament_ids,
        app.commands._currentSettingsSnapshot(),
      );
      const status = await app.api.startPaletteBatch({
        image_path: app.state.image.selectedImage.filename,
        image_source_ref: app.state.image.selectedImage.source_ref || null,
        deck_palettes: cards.map(card => ({
          deck_card_id: card.id,
          deck_card_name: card.name,
          filament_ids: [...card.filament_ids],
        })),
        profile_ref: recipeContext.profile_ref,
        profile_name_at_solve: recipeContext.profile_name_at_solve,
        is_profile_modified_at_solve: recipeContext.is_profile_modified_at_solve,
        recipe_snapshot: recipeContext.recipe_snapshot,
      });
      const jobId = status?.job_id || null;
      if (!jobId) throw new Error("Palette batch start did not return a job ID.");
      acceptedStatus = status;
      app.state.solve.paletteBatchFetchedResultIds.clear();
      app.state.solve.paletteBatchResultFetches.clear();
      app.state.solve.activeSolveJobId = jobId;
      app.state.solve.solveCancelPending = false;
      app.state.solve.solveStatus = status;
      app.commands.resetOperationElapsedSeconds();
      app.commands.ensureBatchPreviewRuns(status);
      if (app.state.settings.settingsDrawerOpen) app.commands.closeSettingsDrawer();
      app.commands.switchTab("solve");
      app.commands.renderSolveProgress();
      app.commands.startPaletteBatchPolling(status);
      return status;
    } catch (error) {
      if (acceptedStatus?.job_id) {
        app.state.solve.activeSolveJobId = acceptedStatus.job_id;
        app.state.solve.solveStatus = acceptedStatus;
        setBatchDeckLocked(true, cards.map(card => card.id));
        app.commands.showToast(
          `Palette batch started, but Preview could not initialize: ${error.message}. Prisma will keep tracking the running batch.`,
          "warn",
        );
        app.commands.startPaletteBatchPolling(acceptedStatus);
        return acceptedStatus;
      }
      setBatchDeckLocked(false);
      app.commands.showToast(`Palette batch failed to start: ${error.message}`, "error");
      if (throwOnError) throw error;
    } finally {
      app.state.solve.paletteBatchStartPending = false;
      app.commands.syncSolveModeUi();
      app.commands.updateSolveReadiness();
    }
  }

  async function recoverPaletteBatch(status) {
    if (!status?.job_id || status?.job_kind !== "palette_batch") return false;
    const active = ["running", "cancelling"].includes(status.status);
    app.state.solve.paletteBatchFetchedResultIds.clear();
    app.state.solve.paletteBatchResultFetches.clear();
    app.state.solve.activeSolveJobId = active ? status.job_id : null;
    app.state.solve.solveCancelPending = !!status.cancel_requested;
    if (active) {
      setSolveMode("batch", { recovery: true });
      setBatchDeckLocked(true);
    }
    await app.commands.reconcilePaletteBatchStatus(status, { awaitResults: !active });
    app.commands.switchTab("solve");
    if (active) {
      app.commands.startPaletteBatchPolling(status);
    } else {
      setSolveMode("single", { recovery: true });
    }
    return true;
  }

  function handlePrimarySolveAction() {
    if (app.state.solve.solveMode === "batch") return app.commands.handleStartPaletteBatch();
    return app.commands.handleStartSolve();
  }

  Object.assign(app.commands, {
    batchIsActive,
    closeSolveModeMenu,
    ensureBatchPreviewRuns,
    fetchPaletteBatchItemResult,
    handlePrimarySolveAction,
    handleStartPaletteBatch,
    initializeSolveModeController,
    openSolveModeMenu,
    positionSolveModeMenu,
    pruneBatchDeckSelection,
    reconcilePaletteBatchStatus,
    recoverPaletteBatch,
    removeBatchDeckSelection,
    selectedBatchDeckCards,
    setBatchDeckLocked,
    setSolveMode,
    settleBatchToolbar,
    startPaletteBatchPolling,
    syncSolveModeUi,
    toggleBatchDeckSelection,
    toggleSolveModeMenu,
  });
}
