/** Install features/images/index commands. */
export function installFeaturesImagesIndex(app) {
  function showImportToast(message, kind, options = {}) {
    const existing = document.querySelector(".import-toast");
    if (existing) existing.remove();
    const toast = document.createElement("div");
    toast.className = `import-toast${kind ? " is-" + kind : ""}`;
    toast.textContent = message;
    document.body.appendChild(toast);
    setTimeout(() => toast.remove(), options.durationMs || 2500);
  }

  async function loadImportData() {
    app.state.images.importState.loading = true;
    app.state.images.importState.loadingMessage = "Scanning image inbox";
    app.commands.renderImportView();
    try {
      const [images, blanks] = await Promise.all([
        app.api.fetchImages().catch(() => []),
        app.api.fetchBlanks().catch(() => []),
      ]);
      app.state.images.importState.loadingMessage =
        "Organizing image assignments";
      app.commands.renderImportView();
      app.commands.syncImportStateFromRecords(images || [], blanks || []);
      app.state.images.importState.loaded = true;
    } catch (err) {
      console.warn("[import] Failed to load import data:", err.message);
      app.state.images.importState.loaded = true;
    }
    app.state.images.importState.loading = false;
    app.state.images.importState.loadingMessage = "";
  }

  function syncImportStateFromRecords(images = [], blanks = []) {
    app.state.images.importState.images = Array.isArray(images) ? images : [];
    for (const img of app.state.images.importState.images) {
      const rot = Number(img.rotation_cw ?? 0) || 0;
      if (rot) {
        app.state.session.data.image_overrides =
          app.state.session.data.image_overrides || {};
        app.state.session.data.image_overrides[img.filename] = {
          rotation_cw: rot,
        };
      }
    }
    app.state.images.importState.blanks = Array.isArray(blanks) ? blanks : [];
    app.commands.buildImageAssignmentMap();

    if (
      app.state.images.importState.selectedImage &&
      !app.state.images.importState.images.some(
        (img) => img.filename === app.state.images.importState.selectedImage,
      )
    ) {
      app.state.images.importState.selectedImage = null;
    }
    if (
      app.state.images.importState.selectedBlank &&
      !app.state.images.importState.blanks.some(
        (blank) =>
          blank.blank_id === app.state.images.importState.selectedBlank ||
          blank.filename === app.state.images.importState.selectedBlank ||
          blank.original_filename ===
            app.state.images.importState.selectedBlank,
      )
    ) {
      app.state.images.importState.selectedBlank = null;
    }
    if (
      app.state.images.importState.selectedSample &&
      !app.state.session.data.samples.some(
        (sample) =>
          sample.sample_id === app.state.images.importState.selectedSample,
      )
    ) {
      app.state.images.importState.selectedSample = null;
    }
  }

  function syncLoadedImportStateFromAppData() {
    if (!app.state.images.importState.loaded) return;
    app.commands.syncImportStateFromRecords(
      app.state.session.data.images || [],
      app.state.session.data.blanks || [],
    );
  }

  function importInboxSummaryMessage(result) {
    const imported = Array.isArray(result?.imported)
      ? result.imported.length
      : 0;
    const skippedItems = Array.isArray(result?.skipped) ? result.skipped : [];
    const movedDuplicates = skippedItems.filter(
      (item) => item?.reason === "already_imported" && item?.removed_path,
    ).length;
    const skipped = skippedItems.length;
    const skippedWithoutMovedDuplicates = skipped - movedDuplicates;
    const errors = Array.isArray(result?.errors) ? result.errors.length : 0;
    const managedLocation = result?.managed_storage_path
      ? ` in ${result.managed_storage_path}`
      : "";
    if (errors) {
      const duplicateText = movedDuplicates
        ? `; moved ${movedDuplicates} duplicate${movedDuplicates === 1 ? "" : "s"} to Removed Images`
        : "";
      return `Imported ${imported}${managedLocation}${duplicateText}; skipped ${skippedWithoutMovedDuplicates}; ${errors} error${errors === 1 ? "" : "s"}`;
    }
    if (imported === 0 && skipped === 0) {
      return "No new inbox images found";
    }
    if (movedDuplicates && skippedWithoutMovedDuplicates) {
      return `Imported ${imported}${managedLocation}; moved ${movedDuplicates} duplicate${movedDuplicates === 1 ? "" : "s"} to Removed Images; skipped ${skippedWithoutMovedDuplicates}`;
    }
    if (movedDuplicates) {
      return `Imported ${imported}${managedLocation}; moved ${movedDuplicates} duplicate${movedDuplicates === 1 ? "" : "s"} to Removed Images`;
    }
    if (skipped) {
      return `Imported ${imported}${managedLocation}; skipped ${skipped} already in Prisma`;
    }
    return `${imported} image${imported === 1 ? " was" : "s were"} successfully imported and moved to managed storage${managedLocation}`;
  }

  function cleanupUnusedSummaryMessage(result) {
    const removed = Array.isArray(result?.removed) ? result.removed.length : 0;
    const skipped = Array.isArray(result?.skipped) ? result.skipped.length : 0;
    const errors = Array.isArray(result?.errors) ? result.errors.length : 0;
    if (errors) {
      return `Moved ${removed} to Removed Images; skipped ${skipped}; ${errors} error${errors === 1 ? "" : "s"}`;
    }
    if (removed === 0 && skipped === 0) {
      return "No unused images to move";
    }
    if (skipped) {
      return `Moved ${removed} to Removed Images; skipped ${skipped}`;
    }
    return `Moved ${removed} unused image${removed === 1 ? "" : "s"} to Removed Images`;
  }

  function importJobIsTerminal(status) {
    return ["succeeded", "failed", "cancelled"].includes(
      String(status || "").toLowerCase(),
    );
  }

  function importProgressHtml(job) {
    const progress = job?.progress || {};
    const percent = Number(progress.percent || 0);
    const current = Number(progress.current_count || 0);
    const total = Number(progress.total_count || 0);
    const message =
      job?.message || progress.message || "Importing inbox images";
    const filename = progress.filename || "";
    const countText = total ? `${current} / ${total}` : "";
    return `
      <div class="backup-progress import-progress" role="status" aria-live="polite">
        <div class="backup-progress-topline">
          <strong>${app.commands.escapeHtml(message)}</strong>
          <span>${app.commands.escapeHtml(countText)}</span>
        </div>
        <div class="backup-progress-bar" aria-hidden="true">
          <div class="backup-progress-fill" style="width: ${Math.max(0, Math.min(100, percent)).toFixed(0)}%;"></div>
        </div>
        <div class="backup-progress-meta">
          <span>${app.commands.escapeHtml(filename || progress.phase || job?.status || "")}</span>
          <span>${Number.isFinite(percent) ? percent.toFixed(0) : "0"}%</span>
        </div>
      </div>
    `;
  }

  function importResultSummaryHtml(result) {
    if (!result) return "";
    const imported = Array.isArray(result.imported)
      ? result.imported.length
      : 0;
    const skipped = Array.isArray(result.skipped) ? result.skipped.length : 0;
    const errors = Array.isArray(result.errors) ? result.errors.length : 0;
    const duplicateMoves = (result.skipped || []).filter(
      (item) => item?.reason === "already_imported" && item?.removed_path,
    ).length;
    const rows = [
      ["Total files", result.total ?? imported + skipped + errors],
      ["Imported", imported],
      ["Duplicates moved", duplicateMoves],
      ["Other skipped", Math.max(0, skipped - duplicateMoves)],
      ["Errors", errors],
    ];
    return `
      <div class="backup-restore-result import-progress-result">
        ${rows
          .map(
            ([label, value]) => `
          <div class="backup-restore-row">
            <span>${app.commands.escapeHtml(label)}</span>
            <strong>${app.commands.escapeHtml(value)}</strong>
          </div>
        `,
          )
          .join("")}
        ${
          result.session_label
            ? `
          <div class="backup-restore-row">
            <span>Import session</span>
            <strong>${app.commands.escapeHtml(result.session_label)}</strong>
          </div>
        `
            : ""
        }
      </div>
    `;
  }

  function importFailureListHtml(result, jobError) {
    const errors = Array.isArray(result?.errors) ? result.errors : [];
    const errorMessage =
      jobError?.message || (typeof jobError === "string" ? jobError : "");
    if (!errors.length && !errorMessage) return "";
    const rows = errors
      .map(
        (item) => `
      <div class="import-progress-error-row">
        <span title="${app.commands._escAttr(item.filename || "")}">${app.commands.escapeHtml(item.filename || "Unknown file")}</span>
        <strong>${app.commands.escapeHtml(item.error || item.reason || "Import failed")}</strong>
      </div>
    `,
      )
      .join("");
    return `
      <div class="import-progress-errors">
        <h4>${errors.length ? "Files That Need Attention" : "Import Error"}</h4>
        ${errorMessage ? `<p>${app.commands.escapeHtml(errorMessage)}</p>` : ""}
        ${rows}
      </div>
    `;
  }

  function importProgressStatusMessage(job, startError) {
    if (startError) {
      return `<div class="backup-restore-message is-error">${app.commands.escapeHtml(startError)}</div>`;
    }
    const status = String(job?.status || "");
    const result = job?.result || null;
    if (status === "cancelled") {
      return `<div class="backup-restore-message is-warning">Import cancelled. Files already touched by the import were rolled back when possible.</div>`;
    }
    if (status === "failed") {
      const text = result?.errors?.length
        ? "Import could not complete because one or more inbox files failed validation."
        : job?.error?.message || job?.message || "Inbox import failed.";
      return `<div class="backup-restore-message is-error">${app.commands.escapeHtml(text)}</div>`;
    }
    if (status === "succeeded") {
      const kind =
        result?.ok === false || result?.errors?.length
          ? "is-error"
          : "is-success";
      return `<div class="backup-restore-message ${kind}">${app.commands.escapeHtml(app.commands.importInboxSummaryMessage(result || {}))}</div>`;
    }
    return "";
  }

  function showImportProgressDialog() {
    return new Promise((resolve) => {
      const overlay = document.createElement("div");
      overlay.className = "info-dialog-overlay import-progress-overlay";
      const state = {
        job: null,
        startError: "",
        actionError: "",
        cancelling: false,
        closed: false,
      };

      const terminal = () =>
        app.commands.importJobIsTerminal(state.job?.status) ||
        !!state.startError;
      const cleanup = () => {
        if (!terminal()) return;
        state.closed = true;
        document.removeEventListener("keydown", handleKeydown);
        overlay.remove();
        resolve(
          state.job || {
            status: "failed",
            error: { message: state.startError },
          },
        );
      };
      const requestCancel = async () => {
        if (!state.job?.job_id || terminal() || state.cancelling) return;
        const cancellationJobId = String(state.job.job_id);
        state.cancelling = true;
        state.actionError = "";
        render();
        try {
          const response =
            await app.api.cancelImportInboxImagesJob(cancellationJobId);
          if (String(state.job?.job_id || "") !== cancellationJobId) return;
          app.commands.assertPolledJobIdentity(response, cancellationJobId);
          state.job = response;
        } catch (err) {
          if (String(state.job?.job_id || "") !== cancellationJobId) return;
          if (terminal()) return;
          state.actionError = err.message || "Could not cancel import.";
        } finally {
          if (String(state.job?.job_id || "") !== cancellationJobId) return;
          state.cancelling =
            state.job?.status === "cancelling" ||
            Boolean(state.job?.cancel_requested);
          render();
        }
      };
      const handleKeydown = (event) => {
        if (event.key === "Escape" && terminal()) cleanup();
      };

      const render = () => {
        const isTerminal = terminal();
        const active = !isTerminal;
        const result = state.job?.result || null;
        overlay.innerHTML = `
          <div class="info-dialog import-progress-dialog" role="dialog" aria-modal="true" aria-labelledby="importProgressTitle">
            ${app.commands.renderDialogHeader({
              title: "Import Images",
              titleId: "importProgressTitle",
              closeButtonHtml: app.commands.renderWindowCloseButton({
                id: "importProgressClose",
                className: "info-dialog-close",
                disabled: active,
              }),
            })}
            <div class="info-dialog-body import-progress-body">
              ${app.commands.importProgressStatusMessage(state.job, state.startError)}
              ${state.actionError ? `<div class="backup-restore-message is-error">${app.commands.escapeHtml(state.actionError)}</div>` : ""}
              ${state.job ? app.commands.importProgressHtml(state.job) : `<div class="backup-progress import-progress"><div class="backup-progress-topline"><strong>Starting inbox image import</strong><span></span></div><div class="backup-progress-bar" aria-hidden="true"><div class="backup-progress-fill" style="width:0%;"></div></div></div>`}
              ${result ? app.commands.importResultSummaryHtml(result) : ""}
              ${app.commands.importFailureListHtml(result, state.job?.error)}
            </div>
            <div class="info-dialog-footer">
              ${active ? `<button class="ghost-button small" type="button" id="importProgressCancel" ${state.cancelling ? "disabled" : ""}>${state.cancelling ? "Cancelling..." : "Cancel"}</button>` : ""}
              <button class="primary-button small" type="button" id="importProgressDone" ${active ? "disabled" : ""}>Close</button>
            </div>
          </div>
        `;
        overlay
          .querySelector("#importProgressClose")
          ?.addEventListener("click", cleanup);
        overlay
          .querySelector("#importProgressDone")
          ?.addEventListener("click", cleanup);
        overlay
          .querySelector("#importProgressCancel")
          ?.addEventListener("click", requestCancel);
      };

      overlay.addEventListener("click", (event) => {
        if (event.target === overlay && terminal()) cleanup();
      });
      document.addEventListener("keydown", handleKeydown);
      document.body.appendChild(overlay);
      render();

      (async () => {
        try {
          const started = await app.api.startImportInboxImagesJob();
          const jobId = String(started?.job_id || "");
          if (!jobId) throw new Error("Inbox import did not return a job id.");
          state.job = started;
          render();
          await app.commands.pollJobUntilTerminal({
            jobId,
            fetchStatus: () => app.api.fetchImportInboxImagesJobStatus(jobId),
            isTerminal: (job) => app.commands.importJobIsTerminal(job.status),
            shouldContinue: () =>
              !state.closed &&
              overlay.isConnected &&
              String(state.job?.job_id || "") === jobId,
            intervalMs: 500,
            onStatus: (job) => {
              state.job = job;
              state.cancelling =
                job.status === "cancelling" || Boolean(job.cancel_requested);
              state.actionError = "";
              render();
            },
            onTransientError: () => {
              state.job = {
                ...(state.job || {}),
                job_id: jobId,
                message: "Connection interrupted; retrying import status...",
              };
              render();
            },
          });
        } catch (err) {
          state.startError = err.message || "Inbox import failed to start.";
          render();
        }
      })();
    });
  }

  async function handleImportInboxImages() {
    const confirmed = await app.commands.showImportConfirmDialog();
    if (!confirmed) return;
    try {
      const job = await app.commands.showImportProgressDialog();
      const result = job?.result || null;
      if (job?.status === "cancelled") {
        app.commands.showImportToast("Inbox import cancelled", "warning");
      } else if (result) {
        app.commands.showImportToast(
          app.commands.importInboxSummaryMessage(result),
          result?.ok === false ||
            result?.errors?.length ||
            job?.status === "failed"
            ? "error"
            : "success",
        );
      } else if (job?.error?.message) {
        app.commands.showImportToast(job.error.message, "error");
      }
      await app.commands.loadImportData();
    } catch (err) {
      app.commands.showImportToast(
        err.message || "Inbox import failed",
        "error",
      );
      await app.commands.loadImportData();
    } finally {
      app.state.images.importState.loading = false;
      app.state.images.importState.loadingMessage = "";
      app.commands.renderImportView();
    }
  }

  async function handleOpenImageInboxFolder() {
    try {
      await app.api.openImageInboxFolder();
      app.commands.showImportToast("Opened Calibration Inbox folder", "ok");
    } catch (err) {
      app.commands.showImportToast(
        err.message || "Could not open the Calibration Inbox folder",
        "error",
      );
    }
  }

  async function handleCleanupUnusedImages() {
    app.state.images.importState.loading = true;
    app.state.images.importState.loadingMessage = "Cleaning up unused images";
    app.commands.renderImportView();
    try {
      const result = await app.api.cleanupUnusedImages();
      app.commands.showImportToast(
        app.commands.cleanupUnusedSummaryMessage(result),
        result?.ok === false || result?.errors?.length ? "error" : "success",
      );
      await app.commands.loadImportData();
    } catch (err) {
      app.commands.showImportToast(
        err.message || "Image cleanup failed",
        "error",
      );
      await app.commands.loadImportData();
    } finally {
      app.state.images.importState.loading = false;
      app.state.images.importState.loadingMessage = "";
      app.commands.renderImportView();
    }
  }

  function renderImportLoadingState(message, detail = "") {
    const blankList = document.getElementById("importBlankList");
    const imageGrid = document.getElementById("importImageGrid");
    const sampleList = document.getElementById("importSampleList");
    const importBtn = document.getElementById("importInboxImportBtn");
    const cleanupBtn = document.getElementById("importInboxCleanupBtn");
    const sampleChip = document.getElementById("importSampleChip");
    const selectedImage = document.getElementById("importSelImage");
    const selectedBlank = document.getElementById("importSelBlank");
    const selectedSample = document.getElementById("importSelSample");
    const assignBtn = document.getElementById("importAssignBtn");
    const assignBlankBtn = document.getElementById("importAssignBlankBtn");
    const regBlankBtn = document.getElementById("importRegisterBlankBtn");

    if (importBtn) importBtn.disabled = true;
    if (cleanupBtn) cleanupBtn.disabled = true;
    if (sampleChip) sampleChip.textContent = "Loading";
    if (selectedImage) selectedImage.textContent = "None";
    if (selectedBlank) selectedBlank.textContent = "None";
    if (selectedSample) selectedSample.textContent = "None";
    if (assignBtn) assignBtn.disabled = true;
    if (assignBlankBtn) assignBlankBtn.disabled = true;
    if (regBlankBtn) regBlankBtn.disabled = true;

    const panel = `
      <div class="import-loading-panel">
        <div class="import-loading-spinner" aria-hidden="true"></div>
        <div>
          <strong>${app.commands.escapeHtml(message || "Loading import data")}</strong>
          <p class="small-copy">${app.commands.escapeHtml(detail || "Preparing sample assignments and image metadata.")}</p>
        </div>
      </div>
    `;
    if (blankList) blankList.innerHTML = panel;
    if (imageGrid) imageGrid.innerHTML = panel;
    if (sampleList) sampleList.innerHTML = panel;
  }

  function renderImportView() {
    const importView = document.getElementById("importView");
    if (!importView) return;

    const apiState =
      typeof app.api.getApiLoadingState === "function"
        ? app.api.getApiLoadingState()
        : { state: "ready" };
    if (apiState.state === "loading") {
      app.commands.renderImportLoadingState(
        "Loading sample assignments",
        "Waiting for live sample records before sorting the image inbox.",
      );
      return;
    }

    if (
      app.state.images.importState.loading ||
      !app.state.images.importState.loaded
    ) {
      app.commands.renderImportLoadingState(
        app.state.images.importState.loadingMessage || "Loading image inbox",
        "Scanning images, blanks, and cached metadata.",
      );
      return;
    }

    // Build assignment map from current sample data
    app.commands.buildImageAssignmentMap();
    if (
      app.state.images.importState.selectedImage &&
      !app.commands.isImportImageSourceAvailable(
        app.state.images.importState.selectedImage,
      )
    ) {
      app.state.images.importState.selectedImage = null;
    }
    if (
      app.state.images.importState.selectedBlank &&
      !app.commands.isImportBlankSourceAvailable(
        app.state.images.importState.selectedBlank,
      )
    ) {
      app.state.images.importState.selectedBlank = null;
    }

    // Filter out registered blanks and images belonging to already-processed samples
    const registeredBlankFilenames = new Set(
      app.state.images.importState.blanks.map((b) => b.original_filename),
    );
    const processedImageFilenames = new Set(
      app.state.session.data.samples
        .filter(
          (e) =>
            e._processing_status === "processed" ||
            e._processing_status === "failed" ||
            e._processing_status === "flagged",
        )
        .map((e) => e._assigned_image)
        .filter(Boolean),
    );
    const inboxImages = app.state.images.importState.images.filter(
      (img) =>
        !registeredBlankFilenames.has(img.filename) &&
        !processedImageFilenames.has(img.filename),
    );
    const unassignedImages = inboxImages.filter(
      (img) => !app.state.images.importState.imageAssignments[img.filename],
    );
    const assignedImages = inboxImages.filter(
      (img) => app.state.images.importState.imageAssignments[img.filename],
    );

    // Samples — "ready" requires image + blank + orientation, and NOT already processed/failed/flagged
    const isProcessed = (exp) =>
      exp._processing_status === "processed" ||
      exp._processing_status === "failed" ||
      exp._processing_status === "flagged";
    const isReady = (exp) =>
      exp._assigned_image &&
      exp._assigned_blank_id &&
      exp._orientation_rots != null &&
      app.commands.sampleHasAvailableImportEvidence(exp) &&
      !isProcessed(exp);
    const fullyAssigned = app.state.session.data.samples.filter(isReady);
    const pendingSamples = app.state.session.data.samples.filter(
      (exp) => !isReady(exp) && !isProcessed(exp),
    );
    const assignedSamples = fullyAssigned;

    // Update chips
    const sampleChip = document.getElementById("importSampleChip");
    if (sampleChip)
      sampleChip.textContent = `${pendingSamples.length} pending, ${assignedSamples.length} ready`;

    // Render all three panes
    app.commands.renderImportBlankPane();
    app.commands.renderImportImageGrid(unassignedImages, assignedImages);
    app.commands.renderImportSampleList(pendingSamples, assignedSamples);

    // Bind blank drop zone and footer action buttons AFTER rendering
    app.commands.bindBlankPaneDropZone();
    app.commands.bindImportActionButtons();
  }

  function sourceAvailabilityInfo(source, noun = "Image") {
    if (!source) {
      return {
        available: true,
        state: "active",
        label: "Available",
        hint: "",
        message: "",
      };
    }
    const state = String(source.source_custody_state || "active").toLowerCase();
    const filename = source.filename || source.original_filename || noun;
    if (state === "archived") {
      return {
        available: false,
        state,
        label: "Archived",
        hint: "Restore before use",
        message: `${noun} '${filename}' is archived. Restore archived RAW images before assigning or reprocessing it.`,
      };
    }
    if (state === "external") {
      return {
        available: false,
        state,
        label: "External",
        hint: "Not available locally",
        message: `${noun} '${filename}' is in external custody and is not available locally.`,
      };
    }
    if (state === "missing" || source.path_exists === false) {
      return {
        available: false,
        state: state === "missing" ? state : "missing-file",
        label: state === "missing" ? "Missing" : "Missing File",
        hint: "Restore before use",
        message: `${noun} '${filename}' is not available locally. Restore the source image before assigning or reprocessing it.`,
      };
    }
    return {
      available: true,
      state,
      label: "Available",
      hint: "",
      message: "",
    };
  }

  function importSourceStateClass(state) {
    return String(state || "active")
      .toLowerCase()
      .replace(/[^a-z0-9_-]+/g, "-");
  }

  function findImportImage(filename) {
    if (!filename) return null;
    return (
      app.state.images.importState.images.find(
        (img) => img.filename === filename,
      ) || null
    );
  }

  function findImportBlankByFilename(filename) {
    if (!filename) return null;
    return (
      app.state.images.importState.blanks.find(
        (blank) =>
          blank.original_filename === filename || blank.filename === filename,
      ) || null
    );
  }

  function findImportBlankById(blankId) {
    if (!blankId) return null;
    return (
      app.state.images.importState.blanks.find(
        (blank) => blank.blank_id === blankId,
      ) || null
    );
  }

  function importImageSourceAvailability(filename) {
    return app.commands.sourceAvailabilityInfo(
      app.commands.findImportImage(filename),
      "Image",
    );
  }

  function importBlankSourceAvailability(filename) {
    return app.commands.sourceAvailabilityInfo(
      app.commands.findImportImage(filename) ||
        app.commands.findImportBlankByFilename(filename),
      "Blank image",
    );
  }

  function isImportImageSourceAvailable(filename) {
    return app.commands.importImageSourceAvailability(filename).available;
  }

  function isImportBlankSourceAvailable(filename) {
    return app.commands.importBlankSourceAvailability(filename).available;
  }

  function showSourceUnavailableToast(availability) {
    if (!availability || availability.available) return;
    app.commands.showImportToast(
      availability.message ||
        "Source image is not available locally. Restore it before continuing.",
      "error",
    );
  }

  function sampleHasAvailableImportEvidence(exp) {
    if (
      exp?._assigned_image &&
      !app.commands.isImportImageSourceAvailable(exp._assigned_image)
    )
      return false;
    if (exp?._assigned_blank_id) {
      const blankInfo = app.commands.findImportBlankById(
        exp._assigned_blank_id,
      );
      if (!blankInfo) return false;
      const blankFilename =
        blankInfo?.original_filename || blankInfo?.filename || "";
      if (
        blankFilename &&
        !app.commands.isImportBlankSourceAvailable(blankFilename)
      )
        return false;
    }
    return true;
  }

  function _imageCardHtml(img) {
    const isAssigned =
      !!app.state.images.importState.imageAssignments[img.filename];
    const assignedTo =
      app.state.images.importState.imageAssignments[img.filename] || "";
    const isSelected =
      app.state.images.importState.selectedImage === img.filename;
    const isIgnored = !!img.ignored;
    const ext = (img.filename || "").split(".").pop() || "";
    const rotationCw = Number(img.rotation_cw || 0) % 4;
    const rotationLabel = rotationCw ? ` · rot ${rotationCw * 90}\u00b0` : "";
    const availability = app.commands.sourceAvailabilityInfo(img, "Image");
    const unavailable = !availability.available;

    let classes = "import-image-card";
    if (isSelected) classes += " is-selected";
    if (isAssigned) classes += " is-assigned";
    if (isIgnored) classes += " is-ignored";
    if (unavailable)
      classes += ` is-source-unavailable is-source-${app.commands.importSourceStateClass(availability.state)}`;

    const stem = img.filename.replace(/\.[^.]+$/, "");
    const filenameAttr = app.commands._escAttr(img.filename);
    // Only show ignore/restore on unassigned images
    let ignoreBtn = "";
    if (isIgnored) {
      ignoreBtn = `<button class="import-ignore-btn is-ignored" data-ignore-file="${filenameAttr}" title="Unignore image">&#x21A9;</button>`;
    } else if (!isAssigned && !unavailable) {
      ignoreBtn = `<button class="import-ignore-btn" data-ignore-file="${filenameAttr}" title="Ignore image">&#x2715;</button>`;
    }
    const rotateBtn =
      !isIgnored && !unavailable
        ? `<button class="import-rotate-btn" data-rotate-file="${filenameAttr}" title="Rotate thumbnail 90 degrees clockwise">&#x21BB;</button>`
        : "";
    const unavailableOverlay = unavailable
      ? `<div class="import-source-unavailable-overlay">
           <span class="import-source-badge is-${app.commands.importSourceStateClass(availability.state)}">${app.commands._escHtml(availability.label)}</span>
           <span>${app.commands._escHtml(availability.hint)}</span>
         </div>`
      : "";
    const thumbContent = `<img class="import-card-thumb" src="${app.commands.previewUrl(img.filename)}" alt="${app.commands._escAttr(stem)}"
               draggable="false" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'">
          <div class="import-card-icon" style="display:none">&#128247;</div>
          ${img.exif_timestamp ? `<div class="import-card-exif">${app.commands.formatExifDate(img.exif_timestamp)}</div>` : ""}
          ${unavailableOverlay}`;

    return `
      <div class="${classes}" data-import-image="${filenameAttr}" data-source-unavailable="${unavailable ? "true" : "false"}" data-source-message="${app.commands._escAttr(availability.message)}" draggable="${!isAssigned && !isIgnored && !unavailable}">
        ${ignoreBtn}
        ${rotateBtn}
        <div class="import-card-filename${stem.length > 25 ? " is-long" : ""}" title="${app.commands._escAttr(img.filename)}">${app.commands._escHtml(stem).replace(/[-_]/g, "$&\u200B")}</div>
        <div class="import-card-thumb-wrap">
          ${thumbContent}
          ${isAssigned ? `<div class="import-card-assigned">${app.commands._escHtml(assignedTo)}</div>` : ""}
        </div>
        <div class="import-card-size">.${ext.toUpperCase()} ${app.commands.formatFileSize(img.size_bytes)}${rotationLabel}</div>
      </div>
    `;
  }

  function renderImportImageGrid(unassigned, assigned) {
    const grid = document.getElementById("importImageGrid");
    if (!grid) return;

    if (app.state.images.importState.images.length === 0) {
      grid.innerHTML = `<p class="small-copy">No images found in inbox. Place images in the Inbox folder and click Import from Inbox.</p>`;
      return;
    }

    // Split unassigned into active vs ignored
    const activeImages = unassigned.filter((img) => !img.ignored);
    const ignoredImages = unassigned.filter((img) => img.ignored);

    // Initialize collapse state if not set
    if (!app.state.images.importState._collapseState)
      app.state.images.importState._collapseState = {};

    const unassignedCollapsed =
      app.state.images.importState._collapseState["img-unassigned"];
    const assignedCollapsed =
      app.state.images.importState._collapseState["img-assigned"];
    const ignoredCollapsed =
      app.state.images.importState._collapseState["img-ignored"];

    const activeCards = activeImages.map(app.commands._imageCardHtml).join("");
    const assignedCards = assigned.map(app.commands._imageCardHtml).join("");
    const ignoredCards = ignoredImages
      .map(app.commands._imageCardHtml)
      .join("");

    const unassignedSection = `<div class="import-section-title" data-collapse-key="img-unassigned">
        <div class="import-section-title-main">
          <span class="collapse-caret">${unassignedCollapsed ? "&#x25B6;" : "&#x25BC;"}</span><span>Unassigned (${activeImages.length})</span>
        </div>
      </div>${unassignedCollapsed ? "" : activeCards}`;

    const assignedSection = `<div class="import-section-title" data-collapse-key="img-assigned">
        <div class="import-section-title-main">
          <span class="collapse-caret">${assignedCollapsed ? "&#x25B6;" : "&#x25BC;"}</span><span>Assigned (${assigned.length})</span>
        </div>
      </div>${assignedCollapsed ? "" : assignedCards}`;

    // Ignore drop zone is a ghost card inside the Ignored section content
    const ignoreGhostCard = `<div class="import-ignore-ghost-card" id="importIgnoreDropZone">
        <span class="ignore-ghost-label">drop here to ignore</span>
      </div>`;

    const ignoredSection = `<div class="import-section-title" data-collapse-key="img-ignored">
        <div class="import-section-title-main">
          <span class="collapse-caret">${ignoredCollapsed ? "&#x25B6;" : "&#x25BC;"}</span><span>Ignored (${ignoredImages.length})</span>
        </div>
      </div>${ignoredCollapsed ? "" : ignoreGhostCard + ignoredCards}`;

    grid.innerHTML = unassignedSection + assignedSection + ignoredSection;
    app.commands.bindImportImageCards();
    app.commands.bindIgnoreDropZone();
    app.commands.bindCollapsibleSections(grid);
  }

  function renderImportSampleList(pending, assigned) {
    const list = document.getElementById("importSampleList");
    if (!list) return;

    const ORIENTATION_ARROWS = ["↑", "→", "↓", "←"];

    function sampleCardHtml(exp, isFullyAssigned) {
      const materialLines = app.commands.sampleMaterialLines(exp);
      const isSelected =
        app.state.images.importState.selectedSample === exp.sample_id;
      const imgName = exp._assigned_image || "";
      const blankId = exp._assigned_blank_id || "";
      const orientRot = exp._orientation_rots;
      const imageAvailability = imgName
        ? app.commands.importImageSourceAvailability(imgName)
        : null;
      const imageUnavailable =
        !!imageAvailability && !imageAvailability.available;
      const blankInfo = blankId
        ? app.commands.findImportBlankById(blankId)
        : null;
      const blankFilename = blankInfo?.original_filename || "";
      const blankAvailability = blankFilename
        ? app.commands.importBlankSourceAvailability(blankFilename)
        : null;
      const blankUnavailable =
        !!blankAvailability && !blankAvailability.available;

      let classes = "import-sample-card";
      if (isSelected) classes += " is-selected";
      if (isFullyAssigned) classes += " is-fully-assigned";

      // Photo slot
      const photoSlot = imgName
        ? `<div class="sc-slot sc-slot-filled${imageUnavailable ? " sc-slot-source-unavailable" : ""}" title="${app.commands._escAttr(imageUnavailable ? imageAvailability.message : imgName)}">
             ${
               imageUnavailable
                 ? `<div class="sc-slot-source-status"><span>${app.commands._escHtml(imageAvailability.label)}</span></div>`
                 : `<img src="${app.commands.previewUrl(imgName)}" alt="photo" onerror="this.style.display='none'">`
             }
             <button class="sc-slot-unassign" data-unassign-type="image" data-unassign-sample="${exp.sample_id}" title="Unassign"><svg width="8" height="8" viewBox="0 0 10 10"><path d="M1 1L9 9M9 1L1 9" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg></button>
             <span class="sc-slot-label">${app.commands._escHtml(imgName.replace(/\.[^.]+$/, ""))}</span>
           </div>`
        : `<div class="sc-slot sc-slot-empty sc-drop-image" data-drop-sample="${exp.sample_id}">
             <span class="sc-slot-placeholder">photo</span>
           </div>`;

      // Blank slot
      const blankSlot = blankId
        ? `<div class="sc-slot sc-slot-filled sc-slot-blank${blankUnavailable ? " sc-slot-source-unavailable" : ""}" title="${app.commands._escAttr(blankUnavailable ? blankAvailability.message : blankId)}">
             ${
               blankUnavailable
                 ? `<div class="sc-slot-source-status"><span>${app.commands._escHtml(blankAvailability.label)}</span></div>`
                 : `<img src="${app.commands.previewUrl(blankFilename)}" alt="blank" onerror="this.style.display='none'">`
             }
             <button class="sc-slot-unassign" data-unassign-type="blank" data-unassign-sample="${exp.sample_id}" title="Unassign"><svg width="8" height="8" viewBox="0 0 10 10"><path d="M1 1L9 9M9 1L1 9" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg></button>
             <span class="sc-slot-label">${app.commands._escHtml(blankId)}</span>
           </div>`
        : `<div class="sc-slot sc-slot-empty sc-drop-blank" data-drop-sample="${exp.sample_id}">
             <span class="sc-slot-placeholder">blank</span>
           </div>`;

      // Orientation d-pad
      function arrowBtn(rot, arrow) {
        const pos = ["top", "right", "bottom", "left"][rot];
        const tip = [
          "Open side up",
          "Open side right",
          "Open side down",
          "Open side left",
        ][rot];
        const active = orientRot === rot ? " is-active" : "";
        return `<button class="sc-orient-btn sc-orient-${pos}${active}" data-orient-rot="${rot}" data-orient-sample="${exp.sample_id}" title="${tip}">${arrow}</button>`;
      }
      const orientPad = `
        <div class="sc-orient-pad">
          ${arrowBtn(0, "&#x2191;")}
          ${arrowBtn(3, "&#x2190;")}
          <div class="sc-orient-center"></div>
          ${arrowBtn(1, "&#x2192;")}
          ${arrowBtn(2, "&#x2193;")}
        </div>`;

      // Ready badge
      const readyBadge = isFullyAssigned
        ? `<span class="import-ready-label">ready</span>`
        : "";
      const materialList =
        materialLines
          .map(
            (line) => `
        <div class="sc-material-line" title="${app.commands._escAttr(line.name)}">
          <span class="color-chip sc-material-chip" style="background:${line.hex}"></span>
          <span class="import-sample-filament">${app.commands._escHtml(line.name)}</span>
        </div>
      `,
          )
          .join("") ||
        (app.commands.isStructuredGeometryBackend() && !(exp.roles || []).length
          ? `<div class="strip-diagram-contract-error">Missing geometry role data</div>`
          : "");

      return `
        <div class="${classes}" data-import-sample="${exp.sample_id}">
          ${readyBadge}
          <div class="sc-card-main">
            <div class="import-sample-info">
              <div class="import-sample-id">${exp.sample_id}</div>
            </div>
            <div class="sc-material-list">
              ${materialList}
            </div>
          </div>
          <div class="sc-strip-mini">${app.commands.buildStripMiniTable(exp)}</div>
          <div class="sc-assign-row">
            ${photoSlot}
            ${blankSlot}
            ${orientPad}
          </div>
        </div>
      `;
    }

    if (!app.state.images.importState._collapseState)
      app.state.images.importState._collapseState = {};

    const readyCollapsed =
      app.state.images.importState._collapseState["samp-ready"];
    const needsCollapsed =
      app.state.images.importState._collapseState["samp-needs"];

    const readyCards = assigned
      .map((exp) => sampleCardHtml(exp, true))
      .join("");
    const pendingCards = pending
      .map((exp) => sampleCardHtml(exp, false))
      .join("");

    const readySection = `<div class="import-section-title" data-collapse-key="samp-ready">
        <div class="import-section-title-main">
          <span class="collapse-caret">${readyCollapsed ? "&#x25B6;" : "&#x25BC;"}</span><span>Ready For Processing (${assigned.length})</span>
        </div>
      </div>${readyCollapsed ? "" : readyCards}`;

    const needsSection = `<div class="import-section-title" data-collapse-key="samp-needs">
        <div class="import-section-title-main">
          <span class="collapse-caret">${needsCollapsed ? "&#x25B6;" : "&#x25BC;"}</span><span>Needs Assignment (${pending.length})</span>
        </div>
      </div>${needsCollapsed ? "" : pendingCards}`;

    list.innerHTML =
      readySection + needsSection ||
      `<p class="small-copy">No samples loaded.</p>`;

    app.commands.bindImportSampleCards();
    app.commands.bindCollapsibleSections(list);
  }

  function renderImportBlankPane() {
    const list = document.getElementById("importBlankList");
    if (!list) return;

    if (app.state.images.importState.blanks.length === 0) {
      list.innerHTML = `<p class="small-copy" style="margin-top:4px">No blanks registered yet.</p>`;
      return;
    }

    const cards = app.state.images.importState.blanks
      .map((blank) => {
        const filename = blank.original_filename || blank.filename || "";
        const filenameMatch = filename.match(/^(.*?)(\.[^.]+)?$/);
        const stem = filenameMatch?.[1] || filename;
        const ext = filenameMatch?.[2] || "";
        const availability =
          app.commands.importBlankSourceAvailability(filename);
        const unavailable = !availability.available;
        const isSelected =
          app.state.images.importState.selectedBlank === filename;
        let classes = "import-blank-card";
        if (isSelected) classes += " is-selected";
        if (unavailable)
          classes += ` is-source-unavailable is-source-${app.commands.importSourceStateClass(availability.state)}`;
        const unavailableOverlay = unavailable
          ? `<div class="import-source-unavailable-overlay">
             <span class="import-source-badge is-${app.commands.importSourceStateClass(availability.state)}">${app.commands._escHtml(availability.label)}</span>
             <span>${app.commands._escHtml(availability.hint)}</span>
           </div>`
          : "";
        const thumbContent = `<img class="import-card-thumb" src="${app.commands.previewUrl(filename)}" alt="${app.commands._escAttr(filename)}"
                 draggable="false" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'">
           <div class="import-card-icon import-card-icon-small" style="display:none">&#128247;</div>
           ${blank.exif_timestamp ? `<div class="import-card-exif">${app.commands.formatExifDate(blank.exif_timestamp)}</div>` : ""}
           ${unavailableOverlay}`;
        return `
        <div class="${classes}" data-blank-filename="${app.commands._escAttr(filename)}" data-source-unavailable="${unavailable ? "true" : "false"}" data-source-message="${app.commands._escAttr(availability.message)}" draggable="${unavailable ? "false" : "true"}">
          <span class="import-unregister-x" data-blank-id="${app.commands._escAttr(blank.blank_id)}" title="Unregister blank"><svg width="10" height="10" viewBox="0 0 10 10"><path d="M1 1L9 9M9 1L1 9" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg></span>
          <div class="import-blank-card-id">${app.commands._escHtml(blank.blank_id)}</div>
          <div class="import-blank-card-filename" title="${app.commands._escAttr(filename)}"><span class="import-filename-stem">${app.commands._escHtml(stem)}</span><span class="import-filename-ext">${app.commands._escHtml(ext)}</span></div>
          <div class="import-card-thumb-wrap">
            ${thumbContent}
          </div>
        </div>
      `;
      })
      .join("");

    list.innerHTML = cards;

    // Bind blank card interactions
    list.querySelectorAll(".import-blank-card").forEach((card) => {
      const filename = card.dataset.blankFilename;

      // Click to select
      card.addEventListener("click", () => {
        if (card.dataset.sourceUnavailable === "true") {
          app.commands.showImportToast(
            card.dataset.sourceMessage ||
              "Blank source image is not available locally. Restore it before assigning.",
            "error",
          );
          return;
        }
        app.state.images.importState.selectedBlank =
          app.state.images.importState.selectedBlank === filename
            ? null
            : filename;
        list.querySelectorAll(".import-blank-card").forEach((c) => {
          c.classList.toggle(
            "is-selected",
            c.dataset.blankFilename ===
              app.state.images.importState.selectedBlank,
          );
        });
        app.commands.updateImportSelectionBar();
      });

      // Drag start — same pattern as image cards
      card.addEventListener("dragstart", (e) => {
        if (card.dataset.sourceUnavailable === "true") {
          e.preventDefault();
          app.commands.showImportToast(
            card.dataset.sourceMessage ||
              "Blank source image is not available locally. Restore it before assigning.",
            "error",
          );
          return;
        }
        e.dataTransfer.setData("text/plain", filename);
        e.dataTransfer.effectAllowed = "all";
        app.state.images.importState.selectedImage = filename;
        app.commands.updateImportSelectionBar();
      });
    });

    // Unregister buttons
    list.querySelectorAll(".import-unregister-x").forEach((x) => {
      const blankId = x.dataset.blankId;
      app.commands.bindConfirmAction(x, {
        armedText: "confirm?",
        onConfirm: async () => {
          try {
            await app.api.unregisterBlank(blankId);
            await app.commands.loadImportData();
            app.commands.renderImportView();
            app.commands.showImportToast(`Unregistered ${blankId}`, "success");
          } catch (err) {
            app.commands.showImportToast(
              err.message || "Unregister failed",
              "error",
            );
          }
        },
      });
    });
  }

  function bindBlankPaneDropZone() {
    const dropChip = document.getElementById("importBlankDropZone");
    const panel = document.getElementById("importBlanksPanel");
    if (!dropChip || !panel || panel._dropBound) return;
    panel._dropBound = true;

    let dragDepth = 0;
    const setActive = (active) => {
      panel.classList.toggle("is-drag-over", active);
      dropChip.classList.toggle("is-drag-over", active);
    };

    panel.addEventListener("dragenter", (e) => {
      e.preventDefault();
      dragDepth += 1;
      setActive(true);
    });

    // CRITICAL: preventDefault on dragover is REQUIRED for drop to fire
    panel.addEventListener("dragover", (e) => {
      e.preventDefault();
      e.dataTransfer.dropEffect = "link";
      setActive(true);
    });

    panel.addEventListener("dragleave", (e) => {
      if (!panel.contains(e.relatedTarget)) {
        dragDepth = 0;
        setActive(false);
        return;
      }
      dragDepth = Math.max(0, dragDepth - 1);
      if (dragDepth === 0) setActive(false);
    });

    panel.addEventListener("drop", async (e) => {
      e.preventDefault();
      dragDepth = 0;
      setActive(false);

      const filename = e.dataTransfer.getData("text/plain");
      // drop on blank zone
      if (!filename) return;
      const sourceAvailability =
        app.commands.importImageSourceAvailability(filename);
      if (!sourceAvailability.available) {
        app.commands.showSourceUnavailableToast(sourceAvailability);
        return;
      }

      if (
        app.state.images.importState.blanks.some(
          (b) => b.original_filename === filename,
        )
      ) {
        app.commands.showImportToast(
          `${filename} is already registered as a blank`,
          "error",
        );
        return;
      }

      if (app.state.images.importState.imageAssignments[filename]) {
        app.commands.showImportToast(
          `${filename} is assigned to ${app.state.images.importState.imageAssignments[filename]} — unassign first`,
          "error",
        );
        return;
      }

      try {
        const result = await app.api.registerBlank(filename);
        app.commands.showImportToast(
          `Registered ${filename} as ${result?.blank_id || "blank"}`,
          "success",
        );
        app.state.images.importState.selectedImage = null;
        await app.commands.loadImportData();
        app.commands.renderImportView();
      } catch (err) {
        app.commands.showImportToast(
          `Registration failed: ${err.message}`,
          "error",
        );
      }
    });
  }

  function bindIgnoreDropZone() {
    const dropZone = document.getElementById("importIgnoreDropZone");
    if (!dropZone) return;

    dropZone.addEventListener("dragover", (e) => {
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      dropZone.classList.add("is-drag-over");
    });

    dropZone.addEventListener("dragleave", (e) => {
      if (!dropZone.contains(e.relatedTarget)) {
        dropZone.classList.remove("is-drag-over");
      }
    });

    dropZone.addEventListener("drop", async (e) => {
      e.preventDefault();
      dropZone.classList.remove("is-drag-over");

      const filename = e.dataTransfer.getData("text/plain");
      if (!filename) return;
      const sourceAvailability =
        app.commands.importImageSourceAvailability(filename);
      if (!sourceAvailability.available) {
        app.commands.showSourceUnavailableToast(sourceAvailability);
        return;
      }

      // Don't ignore if it's assigned to a sample
      if (app.state.images.importState.imageAssignments[filename]) {
        app.commands.showImportToast(
          `${filename} is assigned to ${app.state.images.importState.imageAssignments[filename]} — unassign first`,
          "error",
        );
        return;
      }

      // Don't ignore if it's a registered blank
      if (
        app.state.images.importState.blanks.some(
          (b) => b.original_filename === filename,
        )
      ) {
        app.commands.showImportToast(
          `${filename} is a registered blank — unregister first`,
          "error",
        );
        return;
      }

      try {
        await app.api.ignoreImage(filename);
        app.commands.showImportToast(`Ignored ${filename}`, "success");
        await app.commands.loadImportData();
        app.commands.renderImportView();
      } catch (err) {
        app.commands.showImportToast(`Ignore failed: ${err.message}`, "error");
      }
    });
  }

  function bindCollapsibleSections(container) {
    container.querySelectorAll(".import-section-title").forEach((title) => {
      title.addEventListener("click", () => {
        const key = title.dataset.collapseKey;
        if (!app.state.images.importState._collapseState)
          app.state.images.importState._collapseState = {};
        app.state.images.importState._collapseState[key] =
          !app.state.images.importState._collapseState[key];
        app.commands.renderImportView();
      });
    });
  }

  function updateImportSelectionBar() {
    const selImage = document.getElementById("importSelImage");
    const selSample = document.getElementById("importSelSample");
    const assignBtn = document.getElementById("importAssignBtn");
    const assignBlankBtn = document.getElementById("importAssignBlankBtn");

    const selBlank = document.getElementById("importSelBlank");
    if (selImage)
      selImage.textContent =
        app.state.images.importState.selectedImage || "None";
    if (selBlank)
      selBlank.textContent =
        app.state.images.importState.selectedBlank || "None";
    if (selSample)
      selSample.textContent =
        app.state.images.importState.selectedSample || "None";

    // Register as Blank button — only needs an image selected
    const regBlankBtn = document.getElementById("importRegisterBlankBtn");
    const imageBlocked =
      app.state.images.importState.selectedImage &&
      !app.commands.isImportImageSourceAvailable(
        app.state.images.importState.selectedImage,
      );
    const blankBlocked =
      app.state.images.importState.selectedBlank &&
      !app.commands.isImportBlankSourceAvailable(
        app.state.images.importState.selectedBlank,
      );
    if (regBlankBtn) regBlankBtn.disabled = !!imageBlocked;
    if (assignBtn) assignBtn.disabled = !!imageBlocked;
    if (assignBlankBtn) assignBlankBtn.disabled = !!blankBlocked;
  }

  function bindImportImageCards() {
    const grid = document.getElementById("importImageGrid");
    if (!grid) return;

    grid.querySelectorAll(".import-image-card").forEach((card) => {
      const filename = card.dataset.importImage;

      card.addEventListener("click", () => {
        if (card.dataset.sourceUnavailable === "true") {
          app.commands.showImportToast(
            card.dataset.sourceMessage ||
              "Source image is not available locally. Restore it before assigning.",
            "error",
          );
          return;
        }
        if (app.state.images.importState.selectedImage === filename) {
          app.state.images.importState.selectedImage = null;
        } else {
          app.state.images.importState.selectedImage = filename;
        }
        // Re-highlight without full re-render
        grid.querySelectorAll(".import-image-card").forEach((c) => {
          c.classList.toggle(
            "is-selected",
            c.dataset.importImage ===
              app.state.images.importState.selectedImage,
          );
        });
        app.commands.updateImportSelectionBar();
      });

      // Drag start
      card.addEventListener("dragstart", (e) => {
        if (card.dataset.sourceUnavailable === "true") {
          e.preventDefault();
          app.commands.showImportToast(
            card.dataset.sourceMessage ||
              "Source image is not available locally. Restore it before assigning.",
            "error",
          );
          return;
        }
        if (app.state.images.importState.imageAssignments[filename]) {
          e.preventDefault();
          return;
        }
        e.dataTransfer.setData("text/plain", filename);
        e.dataTransfer.effectAllowed = "all";
        app.state.images.importState.selectedImage = filename;
        grid.querySelectorAll(".import-image-card").forEach((c) => {
          c.classList.toggle("is-selected", c.dataset.importImage === filename);
        });
        app.commands.updateImportSelectionBar();
      });
    });

    // Unregister blank buttons
    grid.querySelectorAll(".import-unregister-x").forEach((x) => {
      const blankId = x.dataset.blankId;
      app.commands.bindConfirmAction(x, {
        armedText: "confirm?",
        onConfirm: async () => {
          try {
            await app.api.unregisterBlank(blankId);
            await app.commands.loadImportData();
            app.commands.renderImportView();
            app.commands.showImportToast(`Unregistered ${blankId}`, "success");
          } catch (err) {
            app.commands.showImportToast(
              err.message || "Unregister failed",
              "error",
            );
          }
        },
      });
    });

    // Ignore / unignore buttons
    grid.querySelectorAll(".import-ignore-btn").forEach((btn) => {
      btn.addEventListener("click", async (e) => {
        e.stopPropagation();
        const filename = btn.dataset.ignoreFile;
        const isIgnored = btn.classList.contains("is-ignored");
        try {
          if (isIgnored) {
            await app.api.unignoreImage(filename);
          } else {
            await app.api.ignoreImage(filename);
          }
          await app.commands.loadImportData();
          app.commands.renderImportView();
        } catch (err) {
          app.commands.showImportToast(
            err.message || "Failed to update",
            "error",
          );
        }
      });
    });

    grid.querySelectorAll(".import-rotate-btn").forEach((btn) => {
      btn.addEventListener("click", async (e) => {
        e.stopPropagation();
        const filename = btn.dataset.rotateFile;
        const current = app.commands.getImageRotationCw(filename);
        const next = (current + 1) % 4;
        try {
          const result = await app.api.rotateImage(filename, next);
          await app.commands.handleRefresh({ ensureAssets: false });
          await app.commands.loadImportData();
          app.commands.renderWorkspace();
          const affected = Number(result?.affected_samples || 0);
          const extra = affected
            ? ` ${affected} assigned sample${affected === 1 ? "" : "s"} reset for re-orientation.`
            : "";
          app.commands.showImportToast(
            `Rotated ${filename} to ${next * 90}\u00b0.${extra}`,
            "success",
          );
        } catch (err) {
          app.commands.showImportToast(err.message || "Rotate failed", "error");
        }
      });
    });
  }

  function bindImportSampleCards() {
    const list = document.getElementById("importSampleList");
    if (!list) return;

    list.querySelectorAll(".import-sample-card").forEach((card) => {
      const sampleId = card.dataset.importSample;

      // Click card to select
      card.addEventListener("click", (e) => {
        // Don't toggle selection if clicking a button or slot
        if (e.target.closest(".sc-orient-btn, .sc-slot-unassign, .sc-slot"))
          return;
        if (app.state.images.importState.selectedSample === sampleId) {
          app.state.images.importState.selectedSample = null;
        } else {
          app.state.images.importState.selectedSample = sampleId;
        }
        list.querySelectorAll(".import-sample-card").forEach((c) => {
          c.classList.toggle(
            "is-selected",
            c.dataset.importSample ===
              app.state.images.importState.selectedSample,
          );
        });
        app.commands.updateImportSelectionBar();
      });

      // Drop on individual photo/blank slots
      card
        .querySelectorAll(".sc-slot-empty, .sc-slot-filled")
        .forEach((slot) => {
          slot.addEventListener("dragover", (e) => {
            e.preventDefault();
            e.stopPropagation();
            e.dataTransfer.dropEffect = "link";
            slot.classList.add("is-drag-over");
          });
          slot.addEventListener("dragleave", (e) => {
            if (!slot.contains(e.relatedTarget))
              slot.classList.remove("is-drag-over");
          });
          slot.addEventListener("drop", async (e) => {
            e.preventDefault();
            e.stopPropagation();
            slot.classList.remove("is-drag-over");
            const filename = e.dataTransfer.getData("text/plain");
            if (!filename || !sampleId) return;

            const blankInfo = app.commands.findImportBlankByFilename(filename);
            const isBlankSlot =
              slot.classList.contains("sc-drop-blank") ||
              slot.classList.contains("sc-slot-blank");

            if (blankInfo) {
              const blankAvailability =
                app.commands.importBlankSourceAvailability(filename);
              if (!blankAvailability.available) {
                app.commands.showSourceUnavailableToast(blankAvailability);
                return;
              }
              // Dropping a blank
              try {
                await app.api.assignBlank(sampleId, blankInfo.blank_id);
                const exp = app.state.session.data.samples.find(
                  (x) => x.sample_id === sampleId,
                );
                if (exp) {
                  exp._assigned_blank_id = blankInfo.blank_id;
                  app.commands.updateProcessingStatus(exp);
                }
                app.commands.showImportToast(
                  `Assigned ${blankInfo.blank_id} to ${sampleId}`,
                  "success",
                );
                app.commands.renderImportView();
              } catch (err) {
                app.commands.showImportToast(
                  `Blank assignment failed: ${err.message}`,
                  "error",
                );
              }
            } else if (!isBlankSlot) {
              const sourceAvailability =
                app.commands.importImageSourceAvailability(filename);
              if (!sourceAvailability.available) {
                app.commands.showSourceUnavailableToast(sourceAvailability);
                return;
              }
              // Dropping an image on photo slot
              await app.commands.doAssignImage(sampleId, filename);
            } else {
              app.commands.showImportToast(
                "That's not a registered blank — drag to the photo slot instead",
                "error",
              );
            }
          });
        });

      // Also accept drops on the whole card as fallback (auto-detect type)
      card.addEventListener("dragover", (e) => {
        if (e.target.closest(".sc-slot")) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = "link";
        card.classList.add("is-drop-target");
      });
      card.addEventListener("dragleave", (e) => {
        if (!card.contains(e.relatedTarget))
          card.classList.remove("is-drop-target");
      });
      card.addEventListener("drop", async (e) => {
        if (e.target.closest(".sc-slot")) return;
        e.preventDefault();
        card.classList.remove("is-drop-target");
        const filename = e.dataTransfer.getData("text/plain");
        if (!filename || !sampleId) return;
        const blankInfo = app.commands.findImportBlankByFilename(filename);
        if (blankInfo) {
          const blankAvailability =
            app.commands.importBlankSourceAvailability(filename);
          if (!blankAvailability.available) {
            app.commands.showSourceUnavailableToast(blankAvailability);
            return;
          }
          try {
            await app.api.assignBlank(sampleId, blankInfo.blank_id);
            const exp = app.state.session.data.samples.find(
              (x) => x.sample_id === sampleId,
            );
            if (exp) {
              exp._assigned_blank_id = blankInfo.blank_id;
              app.commands.updateProcessingStatus(exp);
            }
            app.commands.showImportToast(
              `Assigned ${blankInfo.blank_id} to ${sampleId}`,
              "success",
            );
            app.commands.renderImportView();
          } catch (err) {
            app.commands.showImportToast(
              `Blank assignment failed: ${err.message}`,
              "error",
            );
          }
        } else {
          const sourceAvailability =
            app.commands.importImageSourceAvailability(filename);
          if (!sourceAvailability.available) {
            app.commands.showSourceUnavailableToast(sourceAvailability);
            return;
          }
          await app.commands.doAssignImage(sampleId, filename);
        }
      });
    });

    // Unassign × buttons on slots
    list.querySelectorAll(".sc-slot-unassign").forEach((btn) => {
      const sampleId = btn.dataset.unassignSample;
      const type = btn.dataset.unassignType;
      app.commands.bindConfirmAction(btn, {
        armedText: "?",
        timeout: 2000,
        onConfirm: async () => {
          try {
            if (type === "blank") {
              await app.api.assignBlank(sampleId, null);
              const exp = app.state.session.data.samples.find(
                (x) => x.sample_id === sampleId,
              );
              if (exp) {
                exp._assigned_blank_id = null;
                app.commands.updateProcessingStatus(exp);
              }
              app.commands.showImportToast(
                `Unassigned blank from ${sampleId}`,
                "success",
              );
            } else {
              await app.api.unassignImage(sampleId);
              const exp = app.state.session.data.samples.find(
                (x) => x.sample_id === sampleId,
              );
              if (exp) {
                exp._assigned_image = null;
                exp.source_image = null;
                exp._orientation_rots = null;
                app.commands.updateProcessingStatus(exp);
              }
              app.commands.buildImageAssignmentMap();
              app.commands.showImportToast(
                `Unassigned image from ${sampleId}`,
                "success",
              );
            }
            app.commands.renderImportView();
          } catch (err) {
            app.commands.showImportToast(
              `Unassign failed: ${err.message}`,
              "error",
            );
          }
        },
      });
    });

    // Orientation d-pad buttons
    list.querySelectorAll(".sc-orient-btn").forEach((btn) => {
      btn.addEventListener("click", async (e) => {
        e.stopPropagation();
        const rot = Number(btn.dataset.orientRot);
        const sid = btn.dataset.orientSample;
        const exp = app.state.session.data.samples.find(
          (x) => x.sample_id === sid,
        );
        if (!exp) return;
        if (exp._assigned_image) {
          const sourceAvailability = app.commands.importImageSourceAvailability(
            exp._assigned_image,
          );
          if (!sourceAvailability.available) {
            app.commands.showSourceUnavailableToast(sourceAvailability);
            return;
          }
        }

        // If already active, deselect
        if (exp._orientation_rots === rot) {
          exp._orientation_rots = null;
        } else {
          exp._orientation_rots = rot;
        }

        // Persist if image is assigned
        if (exp._assigned_image) {
          try {
            await app.api.assignImage(
              sid,
              exp._assigned_image,
              exp._orientation_rots,
            );
          } catch (err) {
            app.commands.showImportToast(
              `Orientation save failed: ${err.message}`,
              "error",
            );
          }
        }

        // Update processing status (orientation is required for "assigned")
        app.commands.updateProcessingStatus(exp);

        // Update buttons locally without full re-render
        const pad = btn.closest(".sc-orient-pad");
        pad.querySelectorAll(".sc-orient-btn").forEach((b) => {
          b.classList.toggle(
            "is-active",
            Number(b.dataset.orientRot) === exp._orientation_rots,
          );
        });

        // Re-render sample list so ready badge updates
        app.commands.renderImportView();
      });
    });
  }

  function bindImportActionButtons() {
    const importBtn = document.getElementById("importInboxImportBtn");
    const openFolderBtn = document.getElementById("importInboxOpenFolderBtn");
    const cleanupBtn = document.getElementById("importInboxCleanupBtn");
    const csvAssignmentBtn = document.getElementById("importCsvAssignmentBtn");
    const assignBtn = document.getElementById("importAssignBtn");
    const assignBlankBtn = document.getElementById("importAssignBlankBtn");

    if (importBtn) {
      importBtn.disabled = !!app.state.images.importState.loading;
      if (!importBtn._importBound) {
        importBtn._importBound = true;
        importBtn.addEventListener(
          "click",
          app.commands.handleImportInboxImages,
        );
      }
    }

    if (openFolderBtn && !openFolderBtn._openFolderBound) {
      openFolderBtn._openFolderBound = true;
      openFolderBtn.addEventListener(
        "click",
        app.commands.handleOpenImageInboxFolder,
      );
    }

    if (cleanupBtn) {
      cleanupBtn.disabled = !!app.state.images.importState.loading;
      if (!cleanupBtn._cleanupBound) {
        cleanupBtn._cleanupBound = true;
        app.commands.bindConfirmAction(cleanupBtn, {
          armedText: "Confirm Cleanup",
          onConfirm: app.commands.handleCleanupUnusedImages,
        });
      }
    }

    if (csvAssignmentBtn) {
      csvAssignmentBtn.disabled = !!app.state.images.importState.loading;
      if (!csvAssignmentBtn._csvAssignmentBound) {
        csvAssignmentBtn._csvAssignmentBound = true;
        csvAssignmentBtn.addEventListener(
          "click",
          app.commands.showCsvAssignmentImportDialog,
        );
      }
    }

    // Assign Image button
    if (assignBtn && !assignBtn._importBound) {
      assignBtn._importBound = true;
      assignBtn.addEventListener("click", async () => {
        if (
          !app.state.images.importState.selectedImage &&
          !app.state.images.importState.selectedSample
        ) {
          app.commands.showImportToast("No image or sample selected", "error");
          return;
        }
        if (!app.state.images.importState.selectedImage) {
          app.commands.showImportToast("No image selected", "error");
          return;
        }
        if (!app.state.images.importState.selectedSample) {
          app.commands.showImportToast("No sample selected", "error");
          return;
        }
        const sourceAvailability = app.commands.importImageSourceAvailability(
          app.state.images.importState.selectedImage,
        );
        if (!sourceAvailability.available) {
          app.commands.showSourceUnavailableToast(sourceAvailability);
          return;
        }
        await app.commands.doAssignImage(
          app.state.images.importState.selectedSample,
          app.state.images.importState.selectedImage,
        );
      });
    }

    // Assign Blank button (from selected blank in left pane)
    if (assignBlankBtn && !assignBlankBtn._importBound) {
      assignBlankBtn._importBound = true;
      assignBlankBtn.addEventListener("click", async () => {
        if (
          !app.state.images.importState.selectedBlank &&
          !app.state.images.importState.selectedSample
        ) {
          app.commands.showImportToast("No blank or sample selected", "error");
          return;
        }
        if (!app.state.images.importState.selectedBlank) {
          app.commands.showImportToast("No blank selected", "error");
          return;
        }
        if (!app.state.images.importState.selectedSample) {
          app.commands.showImportToast("No sample selected", "error");
          return;
        }
        const blankObj = app.commands.findImportBlankByFilename(
          app.state.images.importState.selectedBlank,
        );
        const blankId = blankObj?.blank_id;
        if (!blankId) {
          app.commands.showImportToast("Selected blank not found", "error");
          return;
        }
        const blankAvailability = app.commands.importBlankSourceAvailability(
          app.state.images.importState.selectedBlank,
        );
        if (!blankAvailability.available) {
          app.commands.showSourceUnavailableToast(blankAvailability);
          return;
        }
        try {
          await app.api.assignBlank(
            app.state.images.importState.selectedSample,
            blankId,
          );
          const exp = app.state.session.data.samples.find(
            (x) => x.sample_id === app.state.images.importState.selectedSample,
          );
          if (exp) {
            exp._assigned_blank_id = blankId;
            app.commands.updateProcessingStatus(exp);
          }
          app.commands.showImportToast(
            `Assigned ${blankId} to ${app.state.images.importState.selectedSample}`,
            "success",
          );
          app.commands.renderImportView();
        } catch (err) {
          app.commands.showImportToast(
            `Blank assignment failed: ${err.message}`,
            "error",
          );
        }
      });
    }

    // Register as Blank button (from selected image in middle pane)
    const regBlankBtn = document.getElementById("importRegisterBlankBtn");
    if (regBlankBtn && !regBlankBtn._importBound) {
      regBlankBtn._importBound = true;
      regBlankBtn.addEventListener("click", async () => {
        if (!app.state.images.importState.selectedImage) {
          app.commands.showImportToast("No image selected", "error");
          return;
        }
        const filename = app.state.images.importState.selectedImage;
        const sourceAvailability =
          app.commands.importImageSourceAvailability(filename);
        if (!sourceAvailability.available) {
          app.commands.showSourceUnavailableToast(sourceAvailability);
          return;
        }
        if (
          app.state.images.importState.blanks.some(
            (b) => b.original_filename === filename,
          )
        ) {
          app.commands.showImportToast(
            `${filename} is already registered as a blank`,
            "error",
          );
          return;
        }
        if (app.state.images.importState.imageAssignments[filename]) {
          app.commands.showImportToast(
            `${filename} is assigned to ${app.state.images.importState.imageAssignments[filename]} — unassign first`,
            "error",
          );
          return;
        }
        try {
          const result = await app.api.registerBlank(filename);
          app.commands.showImportToast(
            `Registered ${filename} as ${result?.blank_id || "blank"}`,
            "success",
          );
          app.state.images.importState.selectedImage = null;
          await app.commands.loadImportData();
          app.commands.renderImportView();
        } catch (err) {
          app.commands.showImportToast(
            `Registration failed: ${err.message}`,
            "error",
          );
        }
      });
    }
  }

  function updateProcessingStatus(exp) {
    // Require image + blank + orientation to be "assigned" (ready for processing)
    if (
      exp._assigned_image &&
      exp._assigned_blank_id &&
      exp._orientation_rots != null
    ) {
      if (exp._processing_status === "unassigned") {
        exp._processing_status = "assigned";
      }
    } else if (!exp._assigned_image || !exp._assigned_blank_id) {
      // If either is cleared, revert to unassigned and clear results
      exp._processing_status = "unassigned";
      exp.processed = false;
      exp._measurements = null;
      exp._flag_reason = null;
    }
  }

  async function doAssignImage(sampleId, filename) {
    const sourceAvailability =
      app.commands.importImageSourceAvailability(filename);
    if (!sourceAvailability.available) {
      app.commands.showSourceUnavailableToast(sourceAvailability);
      return;
    }
    try {
      const exp = app.state.session.data.samples.find(
        (x) => x.sample_id === sampleId,
      );
      const orientation = exp ? exp._orientation_rots : null;
      await app.api.assignImage(sampleId, filename, orientation);
      // Update local state
      if (exp) {
        exp._assigned_image = filename;
        app.commands.updateProcessingStatus(exp);
      }
      app.state.images.importState.imageAssignments[filename] = sampleId;
      app.state.images.importState.assignedCount++;
      app.state.images.importState.selectedImage = null;
      app.state.images.importState.selectedSample = null;
      app.commands.showImportToast(
        `Assigned ${filename} to ${sampleId}`,
        "success",
      );
      app.commands.renderImportView();
    } catch (err) {
      app.commands.showImportToast(
        `Assignment failed: ${err.message}`,
        "error",
      );
    }
  }

  Object.assign(app.commands, {
    showImportToast,
    loadImportData,
    syncImportStateFromRecords,
    syncLoadedImportStateFromAppData,
    importInboxSummaryMessage,
    cleanupUnusedSummaryMessage,
    importJobIsTerminal,
    importProgressHtml,
    importResultSummaryHtml,
    importFailureListHtml,
    importProgressStatusMessage,
    showImportProgressDialog,
    handleImportInboxImages,
    handleOpenImageInboxFolder,
    handleCleanupUnusedImages,
    renderImportLoadingState,
    renderImportView,
    sourceAvailabilityInfo,
    importSourceStateClass,
    findImportImage,
    findImportBlankByFilename,
    findImportBlankById,
    importImageSourceAvailability,
    importBlankSourceAvailability,
    isImportImageSourceAvailable,
    isImportBlankSourceAvailable,
    showSourceUnavailableToast,
    sampleHasAvailableImportEvidence,
    _imageCardHtml,
    renderImportImageGrid,
    renderImportSampleList,
    renderImportBlankPane,
    bindBlankPaneDropZone,
    bindIgnoreDropZone,
    bindCollapsibleSections,
    updateImportSelectionBar,
    bindImportImageCards,
    bindImportSampleCards,
    bindImportActionButtons,
    updateProcessingStatus,
    doAssignImage,
  });
}
