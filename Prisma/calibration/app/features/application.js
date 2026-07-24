/** Install features/application commands. */
export function installFeaturesApplication(app) {
  function resetDefaultSurfaceChrome() {
    const defaultContent = document.getElementById("defaultContent");
    const panel = defaultContent?.querySelector(".main-logbook");
    const sectionHead = panel?.querySelector(".section-head");
    defaultContent?.classList.remove(
      "model-overview-content",
      "modeling-overview-content",
      "modeling-filaments-content",
    );
    panel?.classList.remove(
      "model-overview-panel",
      "model-tab-shell",
      "modeling-overview-panel",
      "modeling-filaments-panel",
    );
    sectionHead?.classList.remove("model-status-section-head");
  }

  function renderWorkspace() {
    app.commands.renderModeButtons();
    app.commands.renderSubtabs();
    app.commands.renderStatusSummary();
    app.commands.resetDefaultSurfaceChrome();
    app.dom.detailActionArea.innerHTML = "";
    app.dom.drawerStatusPill.innerHTML = "";
    app.dom.detailWindowArea.innerHTML = "";

    const defaultContent = document.getElementById("defaultContent");
    const importView = document.getElementById("importView");

    if (
      app.state.navigation.currentMode === "imageProcessing" &&
      app.state.navigation.currentSubtab === "associate"
    ) {
      // Associate Images uses the full import view layout
      if (defaultContent) defaultContent.style.display = "none";
      if (importView) importView.classList.remove("is-hidden");
      app.commands.mountSubtabsInOwnedSurface();
      app.commands.closeStepBuilderDrawer();
      app.commands.closeFilamentBuilderPanel();
      app.commands.renderImportView();
    } else {
      if (defaultContent) defaultContent.style.display = "";
      if (importView) importView.classList.add("is-hidden");
      app.commands.mountSubtabsInOwnedSurface();

      if (app.state.navigation.currentMode === "logbook") {
        app.commands.renderManagementLogbook();
      } else if (app.state.navigation.currentMode === "filaments") {
        app.commands.closeStepBuilderDrawer();
        app.commands.closeBundleMgmtDrawer();
        app.commands.renderFilamentLibrary();
      } else if (app.state.navigation.currentMode === "geometries") {
        app.commands.closeFilamentBuilderPanel();
        app.commands.renderStepLibrary();
      } else if (app.state.navigation.currentMode === "profiles") {
        app.commands.closeStepBuilderDrawer();
        app.commands.closeFilamentBuilderPanel();
        app.commands.renderModelsView();
      } else if (app.state.navigation.currentMode === "imageProcessing") {
        app.commands.closeStepBuilderDrawer();
        app.commands.closeFilamentBuilderPanel();
        if (app.state.navigation.currentSubtab === "queue")
          app.commands.renderProcessingDashboard();
      }
    }

    app.commands.syncModeTabRowWidth();
    app.commands.syncRecordDrawerPosition();
    app.commands.bindRowSelection();
  }

  function openImageLightbox(src, title) {
    if (!app.dom.imageLightboxOverlay || !app.dom.imageLightboxImg) return;
    app.dom.imageLightboxImg.src = src;
    app.dom.imageLightboxTitle.textContent = title || "Image Preview";
    app.dom.imageLightboxOverlay.classList.add("is-open");
    app.dom.imageLightboxOverlay.setAttribute("aria-hidden", "false");
  }

  function closeImageLightboxPanel() {
    if (!app.dom.imageLightboxOverlay || !app.dom.imageLightboxImg) return;
    app.dom.imageLightboxOverlay.classList.remove("is-open");
    app.dom.imageLightboxOverlay.setAttribute("aria-hidden", "true");
    app.dom.imageLightboxImg.removeAttribute("src");
  }

  function handleDrawerEscape() {
    if (app.dom.linkedSampleDrawer?.classList.contains("is-open")) {
      app.commands.closeLinkedSampleDrawer();
      return true;
    }

    if (app.dom.recordDrawer?.classList.contains("is-open")) {
      if (
        app.state.logbook._sampleDrawerMode === "edit" &&
        app.state.logbook.selectedRecord.kind === "sample" &&
        app.state.logbook.selectedRecord.id
      ) {
        const exp = app.state.session.data.samples.find(
          (item) => item.sample_id === app.state.logbook.selectedRecord.id,
        );
        if (!exp) return false;
        app.state.logbook._sampleDrawerMode = null;
        app.commands.renderSidebarForSample(exp, {
          expanded: app.state.logbook._sampleInspectExpanded,
        });
        return true;
      }

      if (
        app.state.filaments._filamentDrawerMode === "edit" &&
        app.state.logbook.selectedRecord.kind === "filament" &&
        app.state.logbook.selectedRecord.id
      ) {
        const fil =
          app.state.session.data.filaments.find(
            (item) => item.filament_id === app.state.logbook.selectedRecord.id,
          ) || app.state.filaments._filamentDrawerData;
        if (!fil) return false;
        app.state.filaments._filamentDrawerMode = "view";
        app.state.filaments._filamentDrawerData = fil;
        app.commands._renderFilamentDrawerView(fil);
        return true;
      }

      if (
        app.state.logbook.selectedRecord.kind === "step" &&
        document.getElementById("discardStepBtn")
      ) {
        return false;
      }

      app.commands.clearSelectionAndDrawer();
      return true;
    }

    if (app.commands.isStepBuilderOpen()) {
      app.commands.closeStepBuilderDrawer();
      return true;
    }

    if (app.commands.isBundleMgmtOpen()) {
      app.commands.closeBundleMgmtDrawer();
      return true;
    }

    return false;
  }

  function bindDrawerLightboxButtons(root = app.dom.detailSidebar) {
    root?.querySelectorAll("[data-lightbox-src]").forEach((button) => {
      button.addEventListener("click", () => {
        app.commands.openImageLightbox(
          button.dataset.lightboxSrc,
          button.dataset.lightboxTitle || "Image Preview",
        );
      });
    });
  }

  function renderDataSourceBadge() {
    let badge = document.getElementById("dataSourceBadge");
    if (!badge) {
      badge = document.createElement("span");
      badge.id = "dataSourceBadge";
      badge.className = "toolbar-chip data-source-badge";
      const topbarActions = document.querySelector(".topbar-actions");
      if (topbarActions) topbarActions.appendChild(badge);
    }
    const loadState =
      typeof app.api.getApiLoadingState === "function"
        ? app.api.getApiLoadingState()
        : { state: "idle", error: "" };

    if (loadState.state === "loading") {
      badge.textContent = "Loading\u2026";
      badge.className = "toolbar-chip data-source-badge is-loading";
    } else if (app.state.session._dataSource === "api") {
      badge.textContent = "Live API";
      badge.className = "toolbar-chip data-source-badge is-live";
    } else if (loadState.state === "error") {
      badge.textContent = "API error";
      badge.className = "toolbar-chip data-source-badge is-error";
      badge.title = loadState.error;
    } else {
      badge.textContent = "Waiting for API";
      badge.className = "toolbar-chip data-source-badge is-loading";
    }
  }

  function nextMaintenanceCacheBust() {
    app.state.session.maintenanceCacheBust.version += 1;
    return app.state.session.maintenanceCacheBust.version;
  }

  function applyMaintenanceCacheBust(impact = {}) {
    const previewImpact = impact.invalidate_preview_cache || {};
    const sampleImpact = impact.invalidate_sample_thumbnails || {};
    if (previewImpact.all) {
      app.state.session.maintenanceCacheBust.allPreviews =
        app.commands.nextMaintenanceCacheBust();
    }
    (previewImpact.filenames || []).forEach((filename) => {
      if (filename)
        app.state.session.maintenanceCacheBust.previews.set(
          filename,
          app.commands.nextMaintenanceCacheBust(),
        );
    });
    (previewImpact.blank_ids || []).forEach((blankId) => {
      if (blankId)
        app.state.session.maintenanceCacheBust.blankPreviews.set(
          blankId,
          app.commands.nextMaintenanceCacheBust(),
        );
    });
    if (sampleImpact.all) {
      app.state.session.maintenanceCacheBust.allSampleThumbnails =
        app.commands.nextMaintenanceCacheBust();
    }
    const sampleIds = sampleImpact.sample_ids || [];
    const kinds = sampleImpact.kinds || [
      "source",
      "blank",
      "strip",
      "appearance",
    ];
    sampleIds.forEach((sampleId) => {
      kinds.forEach((kind) => {
        if (sampleId && kind) {
          app.state.session.maintenanceCacheBust.sampleThumbnails.set(
            `${sampleId}:${kind}`,
            app.commands.nextMaintenanceCacheBust(),
          );
        }
      });
    });
  }

  async function applyMaintenanceRefreshImpact(impact = {}) {
    if (!impact || impact.kind === "none") return;
    app.commands.applyMaintenanceCacheBust(impact);
    const reloadImportData = impact.reload_import_data === true;
    const geometryImpact = impact.invalidate_geometry_artifacts || {};
    const reloadGeometryArtifacts =
      geometryImpact.all === true ||
      (geometryImpact.geometry_ids || []).length > 0;
    const reloadAppData =
      reloadImportData ||
      reloadGeometryArtifacts ||
      impact.reload_app_data === true ||
      impact.reload_library_data === true;
    if (reloadAppData) {
      await app.commands.handleRefresh({ reloadImportData });
      return;
    }
    if (impact.rerender_workspace) {
      app.commands.renderWorkspace();
    }
    if (impact.rerender_open_drawers) {
      await app.commands.rerenderOpenRecordDrawerAfterRefresh();
      app.commands.rerenderLinkedSampleDrawerAfterRefresh();
    }
    if (reloadImportData && typeof app.commands.loadImportData === "function") {
      await app.commands.loadImportData();
      app.commands.renderWorkspace();
    }
  }

  function normalizeRefreshOptions(options = {}) {
    if (!options || typeof options !== "object") return {};
    if (typeof Event !== "undefined" && options instanceof Event)
      return { userInitiated: true };
    return options;
  }

  function setRefreshButtonBusy(isBusy) {
    const button = document.getElementById("refreshDataBtn");
    if (!button) return;
    button.disabled = !!isBusy;
    button.setAttribute("aria-busy", isBusy ? "true" : "false");
    button.textContent = isBusy ? "Refreshing..." : "Refresh Data";
  }

  function resetRefreshableUiCaches() {
    app.state.modeling.modelFittingState.predictionCache = {};
    app.state.modeling.profilesState.profileCache = {};
    app.state.modeling.profilesState.curveCache = {};
    app.state.modeling.profilesState.swatchCache = {};
    app.state.modeling.profilesState.errorCache = {};
    app.commands.invalidateModelingPayloads();

    if (
      !app.state.modeling.photoStackModelState.isFitting &&
      !app.state.modeling.photoStackModelState.loadingCandidate
    ) {
      app.state.modeling.photoStackModelState.latest = null;
      app.state.modeling.photoStackModelState.candidate = null;
      app.state.modeling.photoStackModelState.predictions = null;
      app.state.modeling.photoStackModelState.loadingCandidate = false;
      app.state.modeling.photoStackModelState.requestedInitialLoad = false;
      app.state.modeling.photoStackModelState.error = null;
    }
    if (!app.state.modeling.cameraTransformState.isBuilding) {
      app.state.modeling.cameraTransformState.current = null;
      app.state.modeling.cameraTransformState.requestedInitialLoad = false;
      app.state.modeling.cameraTransformState.error = null;
    }
  }

  async function handleRefresh(_options = {}) {
    const options = app.commands.normalizeRefreshOptions(_options);
    if (app.state.session._refreshPromise) {
      await app.state.session._refreshPromise;
      if (
        options.reloadImportData === true &&
        !app.state.images.importState.loading
      ) {
        app.state.images.importState.loading = true;
        app.state.images.importState.loaded = false;
        app.state.images.importState.loadingMessage = "Loading image inbox";
        if (
          app.state.navigation.currentMode === "imageProcessing" &&
          app.state.navigation.currentSubtab === "associate"
        ) {
          app.commands.renderWorkspace();
        }
        await app.commands.loadImportData();
        if (
          app.state.navigation.currentMode === "imageProcessing" &&
          app.state.navigation.currentSubtab === "associate"
        ) {
          app.commands.renderWorkspace();
        }
      }
      return;
    }
    app.state.session._refreshPromise = app.commands.runRefresh(options);
    try {
      await app.state.session._refreshPromise;
    } finally {
      app.state.session._refreshPromise = null;
    }
  }

  async function runRefresh(options = {}) {
    if (typeof app.api.initializeData !== "function") return;
    const shouldReloadImportData =
      options.reloadImportData === true ||
      (app.state.navigation.currentMode === "imageProcessing" &&
        app.state.navigation.currentSubtab === "associate" &&
        options.ensureAssets !== false);

    app.commands.setRefreshButtonBusy(true);
    if (shouldReloadImportData) {
      app.state.images.importState.loading = true;
      app.state.images.importState.loaded = false;
      app.state.images.importState.loadingMessage = "Loading image inbox";
      app.commands.renderWorkspace();
    }

    app.commands.renderDataSourceBadge();
    try {
      const source = await app.api.initializeData(app.state.session.data);
      app.state.session._dataSource = source;
      if (typeof app.commands.loadServerConfig === "function") {
        await app.commands.loadServerConfig();
      }
      // Re-initialize step metadata from API-returned alias/bundle
      (app.state.session.data.steps || []).forEach((step) => {
        app.state.logbook.stepMetadata[step.step_id || step.file_name] = {
          alias: step.alias || "",
          bundle: step.bundle || "",
          deleted: false,
        };
      });
      app.commands.resetRefreshableUiCaches();
      if (!shouldReloadImportData) {
        app.commands.syncLoadedImportStateFromAppData();
      }
      app.commands.syncSampleStepCacheFromData();
      app.commands.renderBundleOptions();
      app.commands.renderSummaryRail();
      app.commands.renderWorkspace();
      if (shouldReloadImportData) {
        await app.commands.loadImportData();
        app.commands.renderWorkspace();
      }
      await app.commands.rerenderOpenRecordDrawerAfterRefresh();
      app.commands.rerenderLinkedSampleDrawerAfterRefresh();
      if (app.state.navigation.currentMode === "profiles") {
        app.commands.renderModelsView();
      }
      app.commands.renderDataSourceBadge();
      if (options.userInitiated) {
        app.commands.showImportToast("Data refreshed", "ok");
      }
    } catch (err) {
      app.state.session._dataSource = "static";
      app.commands.renderDataSourceBadge();
      console.error("[app] Refresh failed:", err);
      if (options.userInitiated) {
        app.commands.showImportToast(
          err.message ? `Refresh failed: ${err.message}` : "Refresh failed",
          "error",
        );
      }
    } finally {
      app.commands.setRefreshButtonBusy(false);
    }
  }

  async function rerenderOpenRecordDrawerAfterRefresh() {
    if (!app.dom.recordDrawer?.classList.contains("is-open")) return;

    if (
      app.state.logbook.selectedRecord.kind === "step" &&
      app.state.logbook.selectedRecord.id
    ) {
      app.commands.renderStepDetailDrawer(app.state.logbook.selectedRecord.id, {
        preserveReturn: true,
      });
      return;
    }

    if (
      app.state.logbook.selectedRecord.kind !== "sample" ||
      !app.state.logbook.selectedRecord.id
    )
      return;

    if (app.state.logbook._sampleDrawerMode === "create") {
      if (app.state.logbook.selectedRecord.id === "__bulk__") {
        try {
          const [stepsResp, bundlesResp, idResp] = await Promise.all([
            app.api.fetchSteps(),
            app.api.fetchBundles(),
            app.api.fetchNextSampleId(),
          ]);
          app.state.logbook._sampleCreateSteps =
            stepsResp || app.state.logbook._sampleCreateSteps || [];
          app.state.logbook._bulkCreateBundles =
            bundlesResp || app.state.logbook._bulkCreateBundles || [];
          app.state.logbook._bulkCreateNextId =
            idResp?.next_id || app.state.logbook._bulkCreateNextId || "...";
        } catch (err) {
          console.warn("[refresh] Failed to refresh bulk-create data:", err);
        }
        app.commands._renderBulkSampleCreateDrawer();
        return;
      }
    }

    const exp = app.state.session.data.samples.find(
      (item) => item.sample_id === app.state.logbook.selectedRecord.id,
    );
    if (!exp) return;

    if (app.state.logbook._sampleDrawerMode === "edit") {
      app.commands._renderSampleDrawerEdit(exp, {
        expanded: app.state.logbook._sampleInspectExpanded,
      });
      return;
    }

    app.commands.renderSidebarForSample(exp, {
      expanded: app.state.logbook._sampleInspectExpanded,
    });
  }

  function rerenderLinkedSampleDrawerAfterRefresh() {
    if (
      !app.dom.linkedSampleDrawer?.classList.contains("is-open") ||
      !app.state.logbook._linkedSampleDrawerState.sampleId
    )
      return;
    const exp = app.state.session.data.samples.find(
      (item) =>
        item.sample_id === app.state.logbook._linkedSampleDrawerState.sampleId,
    );
    if (!exp) {
      app.commands.closeLinkedSampleDrawer({ restoreFocus: false });
      return;
    }
    app.commands.renderLinkedSampleDrawer(exp);
    app.commands.syncLinkedSampleDrawerPosition();
  }

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  function formatRestorePointTimestamp(value = "") {
    if (!value) return "Unknown time";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return date.toLocaleString();
  }

  function formatBytes(value) {
    const bytes = Number(value || 0);
    if (!Number.isFinite(bytes) || bytes <= 0) return "";
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    if (bytes < 1024 * 1024 * 1024)
      return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
    return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
  }

  function showSqliteRestorePointRecoveryDialog(status = {}) {
    const existing = document.getElementById("sqliteRecoveryOverlay");
    if (existing) existing.remove();
    const overlay = document.createElement("div");
    overlay.id = "sqliteRecoveryOverlay";
    overlay.className = "info-dialog-overlay sqlite-recovery-overlay";
    const points = status.restore_points || [];
    const requiredConfirmation =
      status.required_confirmation ||
      "Restore the selected SQLite restore point";
    const startupError =
      status.startup_error ||
      status.startup_status?.error ||
      "The SQLite database did not pass startup checks.";
    const selectedPath = points[0]?.sqlite_path || "";
    let state = {
      selectedPath,
      confirmation: "",
      working: false,
      error: "",
      result: null,
    };

    const closeAfterSuccess = async () => {
      overlay.remove();
      await app.commands.handleRefresh();
      app.commands.loadServerConfig();
    };

    const render = () => {
      const isRestoreReady = () =>
        !!state.selectedPath &&
        state.confirmation.trim().toLowerCase() ===
          requiredConfirmation.toLowerCase() &&
        !state.working &&
        !state.result;
      const canRestore = isRestoreReady();
      overlay.innerHTML = `
        <div class="info-dialog sqlite-recovery-dialog" role="dialog" aria-modal="true" aria-labelledby="sqliteRecoveryTitle">
          <div class="info-dialog-header">
            <h3 id="sqliteRecoveryTitle">SQLite Recovery Required</h3>
          </div>
          <div class="info-dialog-body sqlite-recovery-body">
            <section class="drawer-module sqlite-recovery-alert">
              <div class="drawer-module-cap">Startup Check Failed</div>
              <div class="drawer-module-body">
                <p>Prisma could not open the calibration SQLite database safely.</p>
                <p class="small-copy">${app.commands.escapeHtml(startupError)}</p>
              </div>
            </section>
            <section class="drawer-module">
              <div class="drawer-module-cap">Available Restore Points</div>
              <div class="drawer-module-body">
                ${
                  points.length
                    ? `
                  <div class="sqlite-restore-point-list">
                    ${points
                      .map((point, index) => {
                        const path = point.sqlite_path || "";
                        const checked =
                          path === state.selectedPath ||
                          (!state.selectedPath && index === 0);
                        return `
                        <label class="sqlite-restore-point-row ${checked ? "is-active" : ""}">
                          <input type="radio" name="sqliteRestorePoint" value="${app.commands.escapeHtml(path)}" ${checked ? "checked" : ""} ${state.working || state.result ? "disabled" : ""}>
                          <span>
                            <strong>${app.commands.escapeHtml(app.commands.formatRestorePointTimestamp(point.created_at))}</strong>
                            <small>${app.commands.escapeHtml(app.commands.formatBytes(point.sqlite_size_bytes) || "SQLite restore point")}</small>
                            <code>${app.commands.escapeHtml(path)}</code>
                          </span>
                        </label>
                      `;
                      })
                      .join("")}
                  </div>
                `
                    : `
                  <p>No automatic SQLite restore points are available.</p>
                  <p class="small-copy">Close Prisma and restore from a normal Backup / Restore package or a manual database copy.</p>
                `
                }
              </div>
            </section>
            ${
              points.length && !state.result
                ? `
              <section class="drawer-module">
                <div class="drawer-module-cap">Confirm Restore</div>
                <div class="drawer-module-body">
                  <p>Restoring replaces only the SQLite database. The current database file is preserved first for inspection.</p>
                  <label class="sqlite-recovery-confirm">
                    <span>Type this phrase to continue:</span>
                    <code>${app.commands.escapeHtml(requiredConfirmation)}</code>
                    <input type="text" id="sqliteRecoveryConfirmation" value="${app.commands.escapeHtml(state.confirmation)}" ${state.working ? "disabled" : ""}>
                  </label>
                </div>
              </section>
            `
                : ""
            }
            ${state.working ? `<div class="backup-restore-message">Restoring SQLite restore point...</div>` : ""}
            ${state.error ? `<div class="backup-restore-message is-error">${app.commands.escapeHtml(state.error)}</div>` : ""}
            ${
              state.result
                ? `
              <section class="drawer-module sqlite-recovery-success">
                <div class="drawer-module-cap">Restore Complete</div>
                <div class="drawer-module-body">
                  <p>SQLite was restored and Prisma reloaded the database.</p>
                  <div class="backup-result-card">
                    <div><span>Restored From</span><strong>${app.commands.escapeHtml(state.result.restore_point?.sqlite_path || state.selectedPath)}</strong></div>
                    <div><span>Preserved Previous DB</span><strong>${app.commands.escapeHtml(state.result.preserved_current_sqlite?.recovery_dir || "")}</strong></div>
                  </div>
                </div>
              </section>
            `
                : ""
            }
          </div>
          <div class="info-dialog-footer">
            ${
              state.result
                ? `
              <button class="primary-button small" type="button" id="sqliteRecoveryContinue">Continue</button>
            `
                : `
              <button class="primary-button small" type="button" id="sqliteRecoveryRestore" ${canRestore ? "" : "disabled"}>${state.working ? "Restoring..." : "Restore SQLite"}</button>
            `
            }
          </div>
        </div>
      `;
      overlay
        .querySelectorAll("input[name='sqliteRestorePoint']")
        .forEach((input) => {
          input.addEventListener("change", () => {
            state.selectedPath = input.value || "";
            render();
          });
        });
      overlay
        .querySelector("#sqliteRecoveryConfirmation")
        ?.addEventListener("input", (event) => {
          state.confirmation = event.target.value || "";
          const restoreButton = overlay.querySelector("#sqliteRecoveryRestore");
          if (restoreButton) {
            restoreButton.disabled = !isRestoreReady();
          }
        });
      overlay
        .querySelector("#sqliteRecoveryRestore")
        ?.addEventListener("click", async () => {
          if (!isRestoreReady()) return;
          state.working = true;
          state.error = "";
          render();
          try {
            const response = await app.api.restoreSqliteRestorePoint(
              state.selectedPath,
              state.confirmation,
            );
            state.result = response.result || {};
          } catch (err) {
            state.error =
              err.message || "Could not restore SQLite restore point.";
          } finally {
            state.working = false;
            render();
          }
        });
      overlay
        .querySelector("#sqliteRecoveryContinue")
        ?.addEventListener("click", closeAfterSuccess);
    };

    document.body.appendChild(overlay);
    render();
  }

  async function bootstrapApplication() {
    try {
      if (typeof app.api.fetchSqliteRestorePointStatus === "function") {
        const status = await app.api.fetchSqliteRestorePointStatus();
        if (status?.recovery_required) {
          app.commands.renderDataSourceBadge();
          app.commands.showSqliteRestorePointRecoveryDialog(status);
          return;
        }
      }
    } catch (err) {
      console.warn(
        "[startup] Could not check SQLite restore-point status:",
        err,
      );
    }
    await app.commands.handleRefresh();
  }

  Object.assign(app.commands, {
    resetDefaultSurfaceChrome,
    renderWorkspace,
    openImageLightbox,
    closeImageLightboxPanel,
    handleDrawerEscape,
    bindDrawerLightboxButtons,
    renderDataSourceBadge,
    nextMaintenanceCacheBust,
    applyMaintenanceCacheBust,
    applyMaintenanceRefreshImpact,
    normalizeRefreshOptions,
    setRefreshButtonBusy,
    resetRefreshableUiCaches,
    handleRefresh,
    runRefresh,
    rerenderOpenRecordDrawerAfterRefresh,
    rerenderLinkedSampleDrawerAfterRefresh,
    sleep,
    formatRestorePointTimestamp,
    formatBytes,
    showSqliteRestorePointRecoveryDialog,
    bootstrapApplication,
  });
}

/** Bind static UI and start the Calibration application. */
export function startCalibrationApp(app) {
  app.lifecycle.listen(document, "keydown", (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const keyboardNavKeys = new Set([
      "Tab",
      "ArrowLeft",
      "ArrowRight",
      "ArrowUp",
      "ArrowDown",
      "Home",
      "End",
    ]);
    if (keyboardNavKeys.has(e.key)) {
      app.commands.enableKeyboardNavigationMode();
    }
  });

  app.lifecycle.listen(
    document,
    "pointerdown",
    app.commands.disableKeyboardNavigationMode,
  );

  app.dom.modeSwitch.querySelectorAll(".mode-button").forEach((button) => {
    app.lifecycle.listen(button, "click", async () => {
      await app.commands.activateMode(button.dataset.mode);
    });
  });

  app.commands.bindArrowTabNavigation(app.dom.modeSwitch, ".mode-button", {
    activate: (button) => app.commands.activateMode(button.dataset.mode),
    focusActive: () => app.commands.focusModeButton(),
    onArrowDown: () => {
      const subtabs =
        app.constants.modeConfig[app.state.navigation.currentMode]?.subtabs ||
        [];
      if (subtabs.length > 0) {
        app.commands.focusSubtabButton();
      }
    },
  });

  app.commands.bindArrowTabNavigation(app.dom.subtabRow, ".subtab-button", {
    activate: (button) => app.commands.activateSubtab(button.dataset.subtab),
    focusActive: () => app.commands.focusSubtabButton(),
    onArrowUp: () => app.commands.focusModeButton(),
  });

  app.lifecycle.listen(
    app.dom.closeImageLightbox,
    "click",
    app.commands.closeImageLightboxPanel,
  );

  app.lifecycle.listen(app.dom.imageLightboxOverlay, "click", () => {
    app.commands.closeImageLightboxPanel();
  });

  app.lifecycle.listen(document, "keydown", (e) => {
    if (e.key !== "Escape") return;
    if (app.dom.imageLightboxOverlay?.classList.contains("is-open")) {
      e.preventDefault();
      app.commands.closeImageLightboxPanel();
      return;
    }
    if (!app.commands.handleDrawerEscape()) return;
    e.preventDefault();
  });

  app.lifecycle.listen(document, "keydown", async (e) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    if (!app.dom.recordDrawer?.classList.contains("is-open")) return;
    if (app.state.logbook.selectedRecord.kind !== "model_sample") return;
    if (app.commands.shouldIgnoreModelingSampleArrowKey(e)) return;
    const nav = app.commands.modelReviewSampleNavigationMeta(
      app.state.logbook.selectedRecord.id,
    );
    const nextId = e.key === "ArrowLeft" ? nav.previousId : nav.nextId;
    if (!nextId) return;
    e.preventDefault();
    await app.commands.navigateModelingSampleDetail(
      e.key === "ArrowLeft" ? -1 : 1,
    );
  });

  if (app.dom.refreshDataBtn) {
    app.lifecycle.listen(app.dom.refreshDataBtn, "click", () =>
      app.commands.handleRefresh({ userInitiated: true }),
    );
  }

  if (app.dom.maintenanceBtn) {
    app.lifecycle.listen(
      app.dom.maintenanceBtn,
      "click",
      app.commands.showMaintenanceDialog,
    );
  }

  if (app.dom.backupRestoreBtn) {
    app.lifecycle.listen(
      app.dom.backupRestoreBtn,
      "click",
      app.commands.showBackupRestoreDialog,
    );
  }

  if (app.dom.publishModelsBtn) {
    app.lifecycle.listen(
      app.dom.publishModelsBtn,
      "click",
      app.commands.showModelPublicationDialog,
    );
  }

  app.lifecycle.listen(window, "resize", () => {
    app.commands.syncModeTabRowWidth();
    if (app.dom.recordDrawer?.classList.contains("is-open")) {
      app.commands.syncRecordDrawerPosition();
      app.commands.syncLinkedSampleDrawerPosition();
      app.commands.updateLinkedSampleTriggers(app.dom.detailSidebar);
    }
  });

  app.commands.renderBundleOptions();

  app.commands.renderSummaryRail();

  app.commands.renderWorkspace();

  app.commands.renderDataSourceBadge();

  if (typeof app.api.initializeData === "function") {
    app.commands.bootstrapApplication();
  }

  app.lifecycle.listen(app.dom.closeRecordDrawer, "click", () => {
    app.commands.clearSelectionAndDrawer();
  });

  app.lifecycle.listen(app.dom.closeLinkedSampleDrawerBtn, "click", () => {
    app.commands.closeLinkedSampleDrawer();
  });

  app.lifecycle.listen(
    document.getElementById("closeStepBuilderDrawerBtn"),
    "click",
    () => {
      app.commands.closeStepBuilderDrawer();
    },
  );

  app.lifecycle.listen(
    document.getElementById("closeBundleMgmtDrawer"),
    "click",
    () => {
      app.commands.closeBundleMgmtDrawer();
    },
  );

  app.lifecycle.listen(
    document,
    "click",
    app.commands.handleOutsideDrawerDismiss,
  );

  if (app.dom.manualProcCanvas) {
    let _dragIndex = -1;
    let _didDrag = false;
    const DRAG_HIT_RADIUS = 10; // canvas pixels

    function _canvasToImg(e) {
      const rect = app.dom.manualProcCanvas.getBoundingClientRect();
      const scale = app.state.processing._manualProc.previewScale || 1;
      return {
        x: (e.clientX - rect.left) / scale,
        y: (e.clientY - rect.top) / scale,
      };
    }

    function _hitTestCorner(canvasX, canvasY) {
      const scale = app.state.processing._manualProc.previewScale || 1;
      for (
        let i = 0;
        i < app.state.processing._manualProc.corners.length;
        i++
      ) {
        const cx = app.state.processing._manualProc.corners[i].x * scale;
        const cy = app.state.processing._manualProc.corners[i].y * scale;
        const dx = canvasX - cx;
        const dy = canvasY - cy;
        if (Math.sqrt(dx * dx + dy * dy) <= DRAG_HIT_RADIUS) return i;
      }
      return -1;
    }

    app.lifecycle.listen(app.dom.manualProcCanvas, "mousedown", (e) => {
      if (app.state.processing._manualProc.processing) return;
      const rect = app.dom.manualProcCanvas.getBoundingClientRect();
      const canvasX = e.clientX - rect.left;
      const canvasY = e.clientY - rect.top;

      const hit = _hitTestCorner(canvasX, canvasY);
      if (hit >= 0) {
        _dragIndex = hit;
        _didDrag = false;
        app.dom.manualProcCanvas.style.cursor = "grabbing";
        e.preventDefault();
      }
    });

    app.lifecycle.listen(app.dom.manualProcCanvas, "mousemove", (e) => {
      if (_dragIndex >= 0) {
        _didDrag = true;
        const pt = _canvasToImg(e);
        app.state.processing._manualProc.corners[_dragIndex] = {
          x: pt.x,
          y: pt.y,
        };
        app.commands._drawManualCanvas();
        app.commands._updateManualProcUI();
        e.preventDefault();
      } else {
        // Show grab cursor when hovering over a placed corner
        const rect = app.dom.manualProcCanvas.getBoundingClientRect();
        const hit = _hitTestCorner(e.clientX - rect.left, e.clientY - rect.top);
        app.dom.manualProcCanvas.style.cursor = hit >= 0 ? "grab" : "crosshair";
      }
    });

    app.lifecycle.listen(app.dom.manualProcCanvas, "mouseup", (e) => {
      if (_dragIndex >= 0) {
        if (_didDrag) {
          const pt = _canvasToImg(e);
          app.state.processing._manualProc.corners[_dragIndex] = {
            x: pt.x,
            y: pt.y,
          };
          app.commands._drawManualCanvas();
          app.commands._updateManualProcUI();
        }
        _dragIndex = -1;
        _didDrag = false;
        app.dom.manualProcCanvas.style.cursor = "crosshair";
        return;
      }

      // Not dragging — place a new corner if we have room
      if (app.state.processing._manualProc.corners.length >= 4) return;
      const pt = _canvasToImg(e);
      app.state.processing._manualProc.corners.push({ x: pt.x, y: pt.y });
      app.commands._drawManualCanvas();
      app.commands._updateManualProcUI();
    });

    app.lifecycle.listen(app.dom.manualProcCanvas, "mouseleave", () => {
      if (_dragIndex >= 0) {
        _dragIndex = -1;
        _didDrag = false;
        app.dom.manualProcCanvas.style.cursor = "crosshair";
      }
    });
  }

  if (app.dom.mpResetBtn) {
    app.lifecycle.listen(app.dom.mpResetBtn, "click", () => {
      app.commands._resetManualCorners();
      app.commands._drawManualCanvas();
      app.commands._updateManualProcUI();
      document.getElementById("manualProcResultBlock").style.display = "none";
    });
  }

  if (app.dom.mpExtractBtn) {
    app.lifecycle.listen(app.dom.mpExtractBtn, "click", () =>
      app.commands._handleManualExtract(),
    );
  }

  if (app.dom.mpCancelBtn) {
    app.lifecycle.listen(app.dom.mpCancelBtn, "click", async () => {
      if (!app.state.processing._manualProc.currentJobId) return;
      const cancellationJobId = app.state.processing._manualProc.currentJobId;
      app.state.processing._manualProc.cancelling = true;
      app.commands._updateManualProcUI();
      try {
        const response = await app.api.cancelReextractJob(cancellationJobId);
        if (app.state.processing._manualProc.currentJobId !== cancellationJobId)
          return;
        app.commands.assertPolledJobIdentity(response, cancellationJobId);
      } catch (err) {
        if (app.state.processing._manualProc.currentJobId !== cancellationJobId)
          return;
        app.state.processing._manualProc.cancelling = false;
        app.commands._updateManualProcUI();
        app.commands._showManualResult(
          false,
          err.message || "Cancel request failed.",
        );
      }
    });
  }

  if (app.dom.mpAcceptBtn) {
    app.lifecycle.listen(app.dom.mpAcceptBtn, "click", () =>
      app.commands._handleManualAccept(),
    );
  }

  if (app.dom.mpRetryBtn) {
    app.lifecycle.listen(app.dom.mpRetryBtn, "click", () =>
      app.commands._handleManualRetry(),
    );
  }

  if (app.dom.mpCloseBtn) {
    app.lifecycle.listen(app.dom.mpCloseBtn, "click", () =>
      app.commands.closeManualProcessing(),
    );
  }

  if (app.dom.mpOverlay) {
    app.lifecycle.listen(app.dom.mpOverlay, "click", (e) => {
      if (e.target === app.dom.mpOverlay) app.commands.closeManualProcessing();
    });
  }
}
