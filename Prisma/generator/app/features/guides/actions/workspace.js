const TERMINAL_JOB_STATES = new Set(["idle", "complete", "error", "cancelled"]);

function clone(value) {
  return value == null ? value : JSON.parse(JSON.stringify(value));
}

/** Crash-recoverable workspace lifecycle for destructive teaching guides. */
export function installFeaturesGuideWorkspaceActions(app) {
  let heartbeatTimer = null;

  function showWorkspaceLockInterstitial(message) {
    const root = app.state.ui.$("#workspaceLockInterstitial");
    const copy = app.state.ui.$("#workspaceLockMessage");
    if (!root) return;
    if (copy && message) copy.textContent = message;
    root.classList.remove("is-hidden");
    root.setAttribute("aria-hidden", "false");
    app.state.ui.$("#workspaceLockReload")?.focus?.();
  }

  function hideWorkspaceLockInterstitial() {
    const root = app.state.ui.$("#workspaceLockInterstitial");
    if (!root) return;
    root.classList.add("is-hidden");
    root.setAttribute("aria-hidden", "true");
  }

  function initializeWorkspaceLockInterstitial() {
    const reloadButton = app.state.ui.$("#workspaceLockReload");
    if (reloadButton && !reloadButton._workspaceLockBound) {
      reloadButton._workspaceLockBound = true;
      app.lifecycle.listen(reloadButton, "click", () => globalThis.location?.reload?.());
    }
    if (
      typeof globalThis.addEventListener === "function"
      && !app.state.guides.workspaceLockListenerBound
    ) {
      app.state.guides.workspaceLockListenerBound = true;
      app.lifecycle.listen(globalThis, "prisma:workspace-locked", event => {
        showWorkspaceLockInterstitial(event?.detail?.message);
      });
    }
  }

  function captureControl(id) {
    return app.state.ui.$(`#${id}`)?.value ?? null;
  }

  function captureGuideClientSnapshot() {
    return {
      settings: app.commands._captureLiveSettingsProfileState(),
      enabled_filaments: {
        runtime_library_id: app.state.palette.enabledFilamentRuntimeLibraryId,
        enabled_ids: [...app.state.palette.enabledFilaments],
      },
      palette_candidates: {
        selected_ids: [...app.state.palette.candidateSelection],
        initialized: !!app.state.palette.candidateInitialized,
      },
      palette_controls: {
        target_filament_count: captureControl("targetFilamentCount"),
        target_suggest_count: captureControl("targetSuggestCount"),
        suggestion_mode: captureControl("paletteSuggestMode"),
      },
      solve_mode: app.state.solve.solveMode,
      export_policy: {
        output_format: captureControl("exportOutputFormat"),
        geometry_source: captureControl("exportGeometrySource"),
        field_scale: captureControl("exportFieldScale"),
      },
    };
  }

  function describePurgeableGuideWork() {
    return {
      selected_images: app.state.image.selectedImage ? 1 : 0,
      palettes: (app.state.palette.deck?.length || 0) + (app.state.palette.stagingDeck?.length || 0),
      manual_drafts: app.state.palette.manualVariantDraft || app.state.palette.composerPalette?.length ? 1 : 0,
      solved_runs: app.state.solve.solveRuns?.length || 0,
    };
  }

  function purgeableWorkMessage(counts) {
    const labels = [
      [counts.selected_images, "selected image setup"],
      [counts.palettes, "palette"],
      [counts.manual_drafts, "unfinished palette draft"],
      [counts.solved_runs, "solved run"],
    ].filter(([count]) => count > 0).map(([count, label]) => `${count} ${label}${count === 1 ? "" : "s"}`);
    if (!labels.length) return null;
    return `Starting this guide will clear ${labels.join(", ")}. Save anything you want to keep before continuing.`;
  }

  function resetGuideFrontendWorkspace() {
    app.state.image.selectedImage = null;
    app.state.image.pendingSelectedFilename = null;
    app.state.settings.config.image_path = null;
    app.state.settings.config.image_source_ref = null;
    app.state.settings.config.frame = null;
    app.state.settings.config.image_adjust = null;
    app.state.settings.config.palette = [];
    app.state.palette.deck = [];
    app.state.palette.stagingDeck = [];
    app.state.palette.manualSlots = [];
    app.state.palette.manualVariantDraft = null;
    app.state.palette.composerPalette = [];
    app.state.palette.activeDeckId = null;
    app.state.solve.batchSelectedDeckIds?.clear?.();
    app.commands.clearSolveHistory({ force: true });
    app.commands.clearGuideImages();
    app.commands.renderImageTab();
    app.commands.renderCreationTab();
    app.commands.updateRail();
  }

  async function loadGuideBasicSettings(guide) {
    const basic = app.commands.findSettingsProfile(app.state.ui.SYSTEM_SETTINGS_PROFILE_ID);
    if (!basic) throw new Error("the built-in Basic Settings Profile is unavailable");
    const baseline = {
      ...clone(basic),
      id: `temporary-guide-${guide.id}`,
      kind: "temporary",
      name: "Tutorial Basic Settings",
      settings: clone(basic.settings || {}),
      modules: clone(basic.modules || {}),
      source: { kind: "guide", label: guide.title },
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    };
    await app.commands._doLoadSettingsProfile(baseline);
    return baseline;
  }

  async function establishGuideBaseline(guide) {
    await app.commands.executeGuideActions(
      guide.preparation_actions || [],
      { guide, phase: "preparation" },
    );
  }

  function startHeartbeat(sessionId) {
    stopHeartbeat();
    heartbeatTimer = globalThis.setInterval(() => {
      void app.api.heartbeatGuideRuntime(sessionId).catch((error) => {
        console.warn("[guides] workspace heartbeat failed:", error);
      });
    }, 10_000);
    // Node's contract tests should not be kept alive solely by the browser
    // heartbeat. Browser timer handles simply do not expose `unref`.
    heartbeatTimer?.unref?.();
  }

  function stopHeartbeat() {
    if (heartbeatTimer !== null) globalThis.clearInterval(heartbeatTimer);
    heartbeatTimer = null;
  }

  async function prepareGuideWorkspace(guide) {
    app.state.guides.runtimeContext = {};
    if (guide.workspace_policy !== "basic-teaching") return true;
    app.state.guides.runtimeState = "acquiring";
    let leaseId = null;
    try {
      const acquired = await app.api.acquireGuideRuntime();
      leaseId = acquired.lease?.lease_id;
      if (!leaseId) throw new Error("the guide workspace lease was not returned");
      app.state.guides.workspaceLeaseId = leaseId;
      await app.commands.executeGuideActions(
        guide.preflight_actions || [],
        { guide, phase: "preflight" },
      );
      const counts = describePurgeableGuideWork();
      const otherWindowCount = Number(acquired.other_active_windows || 0);
      const otherWindowNotice = otherWindowCount > 0
        ? `${otherWindowCount} other Prisma window${otherWindowCount === 1 ? " is" : "s are"} open and will remain read-only until the guide ends.`
        : null;
      const purgeWarning = purgeableWorkMessage(counts);
      const warning = [purgeWarning, otherWindowNotice].filter(Boolean).join("\n\n") || null;
      if (warning) {
        const confirmed = await app.commands.appConfirm(warning, {
          title: "Clear Workspace for Guide",
          ok: "Clear and Start",
          cancel: "Cancel",
        });
        if (!confirmed) {
          await app.api.releaseGuideRuntime(leaseId);
          app.state.guides.runtimeState = "idle";
          return false;
        }
      }
      const started = await app.api.beginGuideRuntime({
        leaseId,
        guideId: guide.canonical_guide_id || guide.id,
        routeId: guide.route_id || "full",
        clientSnapshot: captureGuideClientSnapshot(),
      });
      app.state.guides.workspaceSessionId = started.session_id;
      app.api.setRequestContext({
        workspaceEpoch: started.workspace_epoch,
        guideSessionId: started.session_id,
      });
      startHeartbeat(started.session_id);
      app.state.guides.runtimeState = "preparing";
      await app.commands.executeGuideAction(
        { action: "workspace.reset" },
        { guide, phase: "preparation" },
      );
      await establishGuideBaseline(guide);
      const tutorialImage = app.state.guides.runtimeContext?.tutorialImage;
      app.state.guides.runtimeContext = {
        ...app.state.guides.runtimeContext,
        imagesFolder: started.images_folder || "the Images folder",
        tutorialSettingsSnapshot: app.commands._configSettingsProfileSnapshot(),
        tutorialImageFilename: tutorialImage?.filename || null,
        tutorialImageSourceRef: tutorialImage?.source_ref || null,
        paletteA: null,
        paletteB: null,
        manualCardId: null,
        manualCardRemoved: false,
        variantCardId: null,
        variantCardRemoved: false,
        advancedSeenOn: false,
        runAId: null,
        runBId: null,
        exportId: null,
      };
      return true;
    } catch (error) {
      app.commands.showToast(`Could not prepare ${guide.title}: ${error.message}`, "error");
      if (app.state.guides.workspaceSessionId) {
        while (app.state.guides.workspaceSessionId) {
          try {
            await cleanupGuideWorkspace(guide);
            break;
          } catch (cleanupError) {
            app.state.guides.runtimeState = "recovering";
            const choice = await resolveBlockedRecovery(
              `Prisma could not restore the environment after guide preparation failed: ${cleanupError.message}`,
            );
            if (choice === "abandoned") break;
          }
        }
      } else if (leaseId) {
        try { await app.api.releaseGuideRuntime(leaseId); } catch { /* lease expires */ }
      }
      app.state.guides.runtimeState = "idle";
      return false;
    }
  }

  async function waitForGuideJobs() {
    const initial = await Promise.all([
      app.api.getSolveStatus().catch(() => ({ status: "idle" })),
      app.api.getExportStatus().catch(() => ({ status: "idle" })),
      app.api.apiFetch("/palette/suggest/status").catch(() => ({ status: "idle" })),
    ]);
    if (!TERMINAL_JOB_STATES.has(initial[0].status)) {
      await app.api.cancelSolve(initial[0].job_id || "").catch(() => {});
    }
    if (!TERMINAL_JOB_STATES.has(initial[1].status)) {
      await app.api.cancelExport(initial[1].job_id || "").catch(() => {});
    }
    if (!TERMINAL_JOB_STATES.has(initial[2].status)) {
      const query = initial[2].job_id ? `?job_id=${encodeURIComponent(initial[2].job_id)}` : "";
      await app.api.apiPost(`/palette/suggest/cancel${query}`).catch(() => {});
    }
    const deadline = Date.now() + 120_000;
    while (Date.now() < deadline) {
      const [solve, exportStatus, suggest] = await Promise.all([
        app.api.getSolveStatus().catch(() => ({ status: "idle" })),
        app.api.getExportStatus().catch(() => ({ status: "idle" })),
        app.api.apiFetch("/palette/suggest/status").catch(() => ({ status: "idle" })),
      ]);
      if ([solve.status, exportStatus.status, suggest.status].every(status => TERMINAL_JOB_STATES.has(status))) return;
      await new Promise(resolve => globalThis.setTimeout(resolve, 250));
    }
    throw new Error("Prisma is still cancelling guide-owned work");
  }

  async function restoreGuideClientSnapshot(snapshot) {
    if (!snapshot) throw new Error("the saved guide environment is unavailable");
    if (snapshot.settings) {
      // The durable server-owned snapshot has already restored settings and
      // modules. Rebuild the browser-owned profile identity without admitting
      // ordinary mutations while the runtime is in its recovery phase.
      await app.commands._restoreLiveSettingsProfileState(snapshot.settings, { syncServer: false });
    }
    const enabled = snapshot.enabled_filaments;
    const sameRuntimeLibrary = (
      !enabled?.runtime_library_id
      || enabled.runtime_library_id === app.state.palette.enabledFilamentRuntimeLibraryId
    );
    if (enabled?.enabled_ids && sameRuntimeLibrary) {
      app.commands.applyEnabledFilamentSelection(enabled.enabled_ids, { persist: false, render: false });
    }
    const candidates = snapshot.palette_candidates;
    const eligibleCandidates = new Set(app.commands.getGenerationEligibleFilamentIds());
    app.state.palette.candidateSelection = new Set(
      (candidates?.selected_ids || []).filter(id => eligibleCandidates.has(id)),
    );
    app.state.palette.candidateInitialized = !!candidates?.initialized;
    const controls = snapshot.palette_controls || {};
    const controlValues = {
      targetFilamentCount: controls.target_filament_count,
      targetSuggestCount: controls.target_suggest_count,
      paletteSuggestMode: controls.suggestion_mode,
      exportOutputFormat: snapshot.export_policy?.output_format,
      exportGeometrySource: snapshot.export_policy?.geometry_source,
      exportFieldScale: snapshot.export_policy?.field_scale,
    };
    for (const [id, value] of Object.entries(controlValues)) {
      const control = app.state.ui.$(`#${id}`);
      if (control && value != null) control.value = value;
    }
    if (globalThis.document) {
      app.commands.setSolveMode?.(snapshot.solve_mode || "single", { recovery: true });
      app.commands.renderCreationTab();
      app.commands.renderSettingsTab();
      app.commands.renderExportTab?.();
    } else {
      app.state.solve.solveMode = snapshot.solve_mode || "single";
    }
    app.commands.updateRail();
  }

  async function cleanupGuideWorkspace(guide, { recoverySnapshot = null, normalCompletion = false } = {}) {
    if (guide?.workspace_policy !== "basic-teaching" && !recoverySnapshot) return;
    const sessionId = app.state.guides.workspaceSessionId;
    if (!sessionId) return;
    app.state.guides.runtimeState = recoverySnapshot ? "recovering" : "ending";
    await waitForGuideJobs();
    let resourceStatus = { resources: [], present: [] };
    if (guide?.durable_mutation_policy && app.api.reconcileGuideResources) {
      resourceStatus = await app.api.reconcileGuideResources(sessionId);
      if (normalCompletion && resourceStatus.present?.length) {
        const names = resourceStatus.present.map(resource => `“${resource.name}”`).join(", ");
        throw new Error(`Finish deleting the Guide-created saves before completing: ${names}`);
      }
    }
    await app.api.resetGuideRuntime(sessionId, { beginRecovery: true });
    const status = recoverySnapshot
      ? { session: { snapshot: { client: recoverySnapshot } } }
      : await app.api.fetchGuideRuntime({ includeSnapshot: true });
    await app.api.restoreGuideRuntimeServer(sessionId);
    app.commands.clearGuideImages();
    await app.commands.loadPrinters();
    await restoreGuideClientSnapshot(status.session?.snapshot?.client);
    if (app.api.fetchSettingsProfiles && app.commands._refreshSettingsProfilesFromResponse) {
      app.commands._refreshSettingsProfilesFromResponse(await app.api.fetchSettingsProfiles());
    }
    const finalized = await app.api.finalizeGuideRuntime(sessionId);
    stopHeartbeat();
    app.api.setRequestContext({ workspaceEpoch: finalized.workspace_epoch, guideSessionId: null });
    app.state.guides.workspaceSessionId = null;
    app.state.guides.workspaceLeaseId = null;
    app.state.guides.runtimeState = "idle";
    hideWorkspaceLockInterstitial();
    return resourceStatus;
  }

  async function resolveBlockedRecovery(message, { allowAbandon = true } = {}) {
    while (true) {
      const retry = await app.commands.appConfirm(message, {
        title: "Guide Recovery Required",
        ok: "Retry Recovery",
        cancel: "Recovery Options",
      });
      if (retry) return "retry";
      if (!allowAbandon) {
        await app.api.openGuideRuntimeConfigFolder().catch(error => {
          app.commands.showToast(`Could not open the configuration folder: ${error.message}`, "error");
        });
        continue;
      }
      const openFolder = await app.commands.appConfirm(
        "You can inspect the preserved recovery record in Prisma's configuration folder. Choose Cancel only if you want to review the option to abandon recovery.",
        { title: "Recovery Options", ok: "Open Folder", cancel: "Consider Abandoning" },
      );
      if (openFolder) {
        await app.api.openGuideRuntimeConfigFolder().catch(error => {
          app.commands.showToast(`Could not open the configuration folder: ${error.message}`, "error");
        });
        continue;
      }
      const abandon = await app.commands.appConfirm(
        "Abandoning recovery preserves the damaged record for inspection, but Prisma will no longer be able to restore the environment saved before the guide. Purged project work cannot be recovered.",
        { title: "Abandon Guide Recovery?", ok: "Abandon Recovery", cancel: "Go Back" },
      );
      if (!abandon) continue;
      const result = await app.api.abandonGuideRuntime();
      stopHeartbeat();
      app.api.setRequestContext({ workspaceEpoch: result.workspace_epoch, guideSessionId: null });
      app.state.guides.workspaceSessionId = null;
      hideWorkspaceLockInterstitial();
      return "abandoned";
    }
  }

  async function prepareStartupGuideRecovery() {
    while (true) {
      const status = await app.api.fetchGuideRuntime({ includeSnapshot: true });
      app.api.setRequestContext({ workspaceEpoch: status.workspace_epoch });
      if (status.corrupt) {
        hideWorkspaceLockInterstitial();
        const choice = await resolveBlockedRecovery(
          status.error || "Guide recovery data is corrupt. Prisma has preserved it and blocked workspace changes.",
        );
        if (choice === "abandoned") return null;
        continue;
      }
      if (!status.session) {
        hideWorkspaceLockInterstitial();
        return null;
      }
      const otherOwnerActive = !!status.lease && !status.lease.owned_by_page;
      if (otherOwnerActive) {
        showWorkspaceLockInterstitial(
          "Another Prisma window is currently running a guide. Prisma will continue here after that guide ends.",
        );
        await new Promise(resolve => globalThis.setTimeout(resolve, 1_000));
        continue;
      }
      hideWorkspaceLockInterstitial();
      try {
        app.state.guides.workspaceSessionId = status.session.session_id;
        await app.api.claimGuideRuntimeRecovery(status.session.session_id);
        app.api.setRequestContext({ guideSessionId: status.session.session_id });
        startHeartbeat(status.session.session_id);
        await waitForGuideJobs();
        const resourceStatus = app.api.reconcileGuideResources
          ? await app.api.reconcileGuideResources(status.session.session_id)
          : { present: [] };
        app.state.guides.recoveredResourceNames = (resourceStatus.present || [])
          .map(resource => resource.name);
        await app.api.resetGuideRuntime(status.session.session_id);
        await app.api.restoreGuideRuntimeServer(status.session.session_id);
        return status.session.snapshot?.client || null;
      } catch (error) {
        const choice = await resolveBlockedRecovery(
          `Prisma could not restore the interrupted guide environment: ${error.message}`,
        );
        if (choice === "abandoned") return null;
      }
    }
  }

  async function finishStartupGuideRecovery(snapshot) {
    if (!snapshot || !app.state.guides.workspaceSessionId) return false;
    while (app.state.guides.workspaceSessionId) {
      try {
        resetGuideFrontendWorkspace();
        await restoreGuideClientSnapshot(snapshot);
        if (app.api.fetchSettingsProfiles && app.commands._refreshSettingsProfilesFromResponse) {
          app.commands._refreshSettingsProfilesFromResponse(await app.api.fetchSettingsProfiles());
        }
        const finalized = await app.api.finalizeGuideRuntime(app.state.guides.workspaceSessionId);
        stopHeartbeat();
        app.api.setRequestContext({ workspaceEpoch: finalized.workspace_epoch, guideSessionId: null });
        app.state.guides.workspaceSessionId = null;
        hideWorkspaceLockInterstitial();
        const remaining = app.state.guides.recoveredResourceNames || [];
        const disclosure = remaining.length
          ? ` Guide-created saves preserved: ${remaining.join(", ")}.`
          : "";
        app.commands.showToast(`An interrupted guide was cleaned up and your previous environment was restored.${disclosure}`, "success");
        app.state.guides.recoveredResourceNames = [];
        return true;
      } catch (error) {
        app.state.guides.runtimeState = "recovering";
        const choice = await resolveBlockedRecovery(
          `Prisma restored the server workspace but could not restore this window's saved guide environment: ${error.message}`,
        );
        if (choice === "abandoned") return false;
      }
    }
    return false;
  }

  Object.assign(app.commands, {
    captureGuideClientSnapshot,
    describePurgeableGuideWork,
    resetGuideFrontendWorkspace,
    loadGuideBasicSettings,
    prepareGuideWorkspace,
    cleanupGuideWorkspace,
    prepareStartupGuideRecovery,
    finishStartupGuideRecovery,
    restoreGuideClientSnapshot,
    showWorkspaceLockInterstitial,
    hideWorkspaceLockInterstitial,
    initializeWorkspaceLockInterstitial,
  });
}
