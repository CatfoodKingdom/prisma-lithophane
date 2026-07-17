/**
 * Install the palette/index feature commands.
 * @param {import("../../core/types.js").ApplicationContext} app
 */
export function installFeaturesPaletteIndex(app) {
function closeCompLightbox() {
    app.state.solve._lightboxIdx = -1;
    app.state.solve._solveLightboxState = null;
    if (app.state.solve._lightboxCleanup) app.state.solve._lightboxCleanup();
    const lb = app.state.ui.$("#compLightbox");
    if (lb) lb.classList.add("is-hidden");
  }

function navigateSolveLightbox(key) {
    if (!app.state.solve._solveLightboxState) return false;
    const selectedRuns = app.commands.getSelectedSolveRunsWithResults();
    if (!selectedRuns.length) return false;

    if (app.state.solve._solveLightboxState.kind === "source") {
      if (key !== "ArrowRight") return false;
      app.commands.openSolvePreviewLightboxForRun(
        selectedRuns[0],
        app.state.solve._solveLightboxState.view || app.state.solve.solveView,
        app.state.solve._solveLightboxState.targetKind || "run",
      );
      return true;
    }

    if (app.state.solve._solveLightboxState.kind === "thickness") {
      const runIndex = selectedRuns.findIndex(r => r.id === app.state.solve._solveLightboxState.runId);
      if (runIndex < 0) return false;
      const run = selectedRuns[runIndex];
      const items = app.commands.getSolveThicknessItems(run);
      if (!items.length) return false;

      if (key === "ArrowRight") {
        if (app.state.solve._solveLightboxState.mapIndex < items.length - 1) {
          app.commands.openThicknessLightboxForKey(run.id, items[app.state.solve._solveLightboxState.mapIndex + 1].key);
          return true;
        }
        return false;
      }
      if (key === "ArrowLeft") {
        if (app.state.solve._solveLightboxState.mapIndex > 0) {
          app.commands.openThicknessLightboxForKey(run.id, items[app.state.solve._solveLightboxState.mapIndex - 1].key);
          return true;
        }
        return false;
      }
      if (key === "ArrowDown") {
        if (runIndex < selectedRuns.length - 1) {
          const nextRun = selectedRuns[runIndex + 1];
          if (app.commands.getSolveThicknessItems(nextRun).some(item => item.key === app.state.solve._solveLightboxState.mapKey)) {
            app.commands.openThicknessLightboxForKey(nextRun.id, app.state.solve._solveLightboxState.mapKey);
            return true;
          }
        }
        return false;
      }
      if (key === "ArrowUp") {
        if (runIndex > 0) {
          const nextRun = selectedRuns[runIndex - 1];
          if (app.commands.getSolveThicknessItems(nextRun).some(item => item.key === app.state.solve._solveLightboxState.mapKey)) {
            app.commands.openThicknessLightboxForKey(nextRun.id, app.state.solve._solveLightboxState.mapKey);
            return true;
          }
        }
        return false;
      }
      return false;
    }

    if (app.state.solve._solveLightboxState.kind === "solve" || app.state.solve._solveLightboxState.kind === "surface" || app.state.solve._solveLightboxState.kind === "recipe") {
      if (key !== "ArrowRight" && key !== "ArrowLeft") return false;
      const runIndex = selectedRuns.findIndex(r => r.id === app.state.solve._solveLightboxState.runId);
      if (runIndex < 0) return false;
      const delta = key === "ArrowRight" ? 1 : -1;
      const nextIndex = runIndex + delta;
      if (nextIndex < 0) {
        if (key === "ArrowLeft") {
          const sourceView = app.state.solve._solveLightboxState.kind === "surface"
            ? app.state.solve._solveLightboxState.viewType
            : (app.state.solve._solveLightboxState.kind === "recipe" ? "color_ceiling" : app.state.solve._solveLightboxState.view);
          const sourceTargetKind = app.state.solve._solveLightboxState.kind === "surface"
            ? "surface"
            : (app.state.solve._solveLightboxState.kind === "recipe" ? "recipe" : "run");
          if (app.commands.shouldShowSolveSourceColumn(sourceView)) {
            app.commands.openSolveSourceLightbox(selectedRuns[0], sourceView, sourceTargetKind);
            return true;
          }
        }
        return false;
      }
      if (nextIndex >= selectedRuns.length) return false;
      const nextRun = selectedRuns[nextIndex];
      if (app.state.solve._solveLightboxState.kind === "surface") {
        app.commands.openSurfaceLightbox(app.state.solve._solveLightboxState.viewType, nextRun.id);
      } else if (app.state.solve._solveLightboxState.kind === "recipe") {
        app.commands.openRecipeLightbox(nextRun.id);
      } else {
        app.commands.openSolveRunLightbox(nextRun.id, app.state.solve._solveLightboxState.view);
      }
      return true;
    }

    return false;
  }

function updateLibraryFilterStatus() {
    const eligibleIds = new Set(app.commands.getGenerationEligibleFilamentIds());
    const enabledCount = [...app.state.palette.enabledFilaments].filter(fid => eligibleIds.has(fid)).length;
    const totalEligible = eligibleIds.size;
    const label = `${enabledCount} / ${totalEligible} enabled`;
    const statusEl = app.state.ui.$("#libraryFilterStatus");
    if (statusEl) statusEl.textContent = label;
    const railCount = app.state.ui.$("#railLibraryCount");
    if (railCount) railCount.textContent = `(${enabledCount}/${totalEligible})`;
  }

async function handleOpenImageLibraryFolder() {
    try {
      await app.api.openImagesFolder();
      app.commands.showToast("Opened Images folder", "success");
    } catch (err) {
      app.commands.showToast(`The Images folder could not be opened: ${err.message}`, "error");
    }
  }

function openDetailDrawer(title, bodyHtml) {
    if (app.state.settings.settingsDrawerOpen) app.commands.closeSettingsDrawer();
    const drawer = app.state.ui.$("#detailDrawer");
    const overlay = app.state.ui.$("#drawerOverlay");
    app.state.ui.$("#drawerTitle").textContent = title;
    app.state.ui.$("#drawerBody").innerHTML = bodyHtml;
    drawer.setAttribute("aria-hidden", "false");
    overlay.setAttribute("aria-hidden", "false");
  }

function closeDetailDrawer() {
    const drawer = app.state.ui.$("#detailDrawer");
    const overlay = app.state.ui.$("#drawerOverlay");
    drawer.setAttribute("aria-hidden", "true");
    overlay.setAttribute("aria-hidden", "true");
  }

function toggleSettingsDrawer() {
    // Settings opener(s) toggle: a second click on an already-open drawer closes it.
    if (app.state.settings.settingsDrawerOpen) app.commands.closeSettingsDrawer();
    else app.commands.openSettingsDrawer();
  }

function openSettingsDrawer() {
    // Close detail drawer if open
    const detailDrawer = app.state.ui.$("#detailDrawer");
    if (detailDrawer && detailDrawer.getAttribute("aria-hidden") === "false") {
      app.commands.closeDetailDrawer();
    }

    const grid = app.state.ui.$(".settings-grid");
    const drawerBody = app.state.ui.$("#settingsDrawerBody");
    const drawer = app.state.ui.$("#settingsDrawer");

    // Reparent settings grid into drawer
    drawerBody.appendChild(grid);
    grid.classList.add("in-drawer");

    // Show drawer — persistent overlay, NO scrim: settings must not dim or close the page.
    drawer.setAttribute("aria-hidden", "false");
    app.state.settings.settingsDrawerOpen = true;
    app.commands.scheduleSettingsDrawerDistribution();
  }

  Object.assign(app.commands, {
    closeCompLightbox,
    navigateSolveLightbox,
    updateLibraryFilterStatus,
    handleOpenImageLibraryFolder,
    openDetailDrawer,
    closeDetailDrawer,
    toggleSettingsDrawer,
    openSettingsDrawer,
  });
}
