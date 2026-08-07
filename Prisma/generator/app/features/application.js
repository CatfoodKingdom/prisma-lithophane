/**
 * Install the application feature commands.
 * @param {import("../core/types.js").ApplicationContext} app
 */
export function installFeaturesApplication(app) {
  async function loadFilaments() {
    try {
      const filaments = await app.api.fetchFilaments();
      app.state.session.allFilaments = filaments;
      app.state.session.apiConnected = true;
    } catch {
      app.state.session.allFilaments = [...app.data.STATIC_FILAMENTS];
      app.state.session.apiConnected = false;
    }
    // This is only an in-memory fallback until authoritative runtime-library
    // status arrives. Offline/recovery states must never write scoped choices.
    app.state.palette.enabledFilamentRuntimeLibraryId = null;
    app.state.palette.enabledFilamentPersistenceReady = false;
    app.commands.applyEnabledFilamentSelection(app.commands.getGenerationEligibleFilamentIds(), { persist: false, render: false });
  }

  async function loadImages() {
    try {
      app.state.image.availableImages = await app.api.fetchImages();
    } catch {
      app.state.image.availableImages = [];
    }
  }

  async function startGeneratorApp() {
    app.commands.initializeWorkspaceLockInterstitial();
    app.commands.initializeThemeController();
    app.commands.initializeSolveModeController();
    app.commands.initializeDeckCardMenuController();
    app.commands.initializeGuidesController();
    app.commands.initAllEnhancedSliders();
    app.commands.bindEvents();
    app.commands.updateRail();
    app.commands.renderSettingsTab();

    // Recovery is a startup barrier. Aside from the connectivity probe above,
    // do not restore ordinary session work or resume jobs until an interrupted
    // destructive guide has been unwound.
    try {
      await app.api.apiFetch("/system/health");
      app.state.session.apiConnected = true;
    } catch {
      app.state.session.apiConnected = false;
    }
    const guideRecoverySnapshot = app.state.session.apiConnected
      ? await app.commands.prepareStartupGuideRecovery()
      : null;
    await app.commands.loadFilaments();
    if (app.state.session.apiConnected) await app.commands.loadModelLibraries({ openOnRecovery: true, silent: true });
    await app.commands.loadPrinters();
    await app.commands.loadSavedPalettes();
    if (app.state.session.apiConnected && !guideRecoverySnapshot) {
      await app.commands.loadImages();
      try {
        const session = await app.api.fetchSession();
        if (session.config) {
          Object.assign(app.state.settings.config, session.config);
          // Deck is ephemeral — don't restore stale server palette.
          // Saved palettes can be loaded via the Load button.
          if (session.config.image_path) {
            app.state.image.selectedImage = session.config.image_source_ref && session.source_image
              ? session.source_image
              : app.state.image.availableImages.find((i) => i.filename === session.config.image_path);
            if (!app.state.image.selectedImage) {
              app.state.image.pendingSelectedFilename = session.config.image_path;
            }
          }
          if (session.config.frame) {
            const f = session.config.frame;
            app.state.image.frameState.widthMm = app.commands.clampFrameWidth(f.width_mm ?? 100);
            app.state.image.frameState.heightMm = app.commands.clampFrameHeight(f.height_mm ?? 100);
            app.state.image.frameState.scale = f.scale ?? 100;
            app.state.image.frameState.rotation = f.rotation ?? 0;
            app.state.image.frameState.panX = f.pan_x ?? 0;
            app.state.image.frameState.panY = f.pan_y ?? 0;
          }
        }
        if (session.solve) {
          app.state.solve.solveStatus = session.solve;
          if (session.solve.job_kind === "palette_batch") {
            await app.commands.recoverPaletteBatch(session.solve);
          } else if (session.solve.status === "running") {
            const recoveredRun = app.commands.createSolveRun(app.state.settings.config.palette || [], { ...app.state.settings.config });
            recoveredRun.id = session.solve.card_id || recoveredRun.id;
            app.state.solve.solveRuns.push(recoveredRun);
            app.state.solve.selectedRunIds.add(recoveredRun.id);
            app.state.solve.activeSolveRunId = recoveredRun.id;
            app.state.solve.activeSolveJobId = session.solve.job_id || null;
            app.state.solve.solveCancelPending = !!session.solve.cancel_requested;
            app.commands.startSolvePolling(recoveredRun);
          }
        }
      } catch { /* ignore */ }
    }

    // Apply saved Settings Profile after session restore so profile values survive server restart.
    await app.commands.loadModules();
    // Apply the saved Settings Profile after module load so its module state is authoritative.
    await app.commands.loadPresets(guideRecoverySnapshot
      ? { applyPreferred: false, syncServer: false }
      : {});
    if (guideRecoverySnapshot) {
      await app.commands.finishStartupGuideRecovery(guideRecoverySnapshot);
      await app.commands.loadImages();
    }
    app.commands.renderSettingsTab();
    app.commands.initCollapsibleSections();

    // Re-render rail and library count now that data is loaded
    app.commands.updateRail();
    app.commands.updateLibraryFilterStatus();
    // The Image tab is the default visible tab but switchTab() never fires at startup, so render
    // its library grid now that availableImages is populated (otherwise it stays empty until the
    // first upload/tab-switch).
    app.commands.renderImageTab();
    if (app.state.session.apiConnected && !guideRecoverySnapshot) {
      void app.commands.startFolderImageRefresh({ announce: false }).catch((error) => {
        app.state.image.importPollingError = error?.message || "Image preparation could not start";
        app.commands.renderImageImportNotice();
      });
      await app.commands.loadGuideState();
      await app.commands.maybeOfferGuidedSetup();
    }

    app.commands.showToast(app.state.session.apiConnected ? "Connected to Prisma server" : "Offline mode \u2014 start server to enable full features", app.state.session.apiConnected ? "success" : "");
  }

  Object.assign(app.commands, {
    loadFilaments,
    loadImages,
    startGeneratorApp,
  });
  app.lifecycle.listen(window, "resize", () => {
    clearTimeout(app.state.ui._resizeTimer);
    app.state.ui._resizeTimer = setTimeout(app.commands.distributeSettingsColumns, 200);
  });
}
