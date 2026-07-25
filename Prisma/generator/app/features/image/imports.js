const TERMINAL_IMPORT_STATES = new Set(["complete", "partial", "failed"]);

/**
 * Own image-import polling, progress presentation, and failure recovery.
 * @param {import("../../core/types.js").ApplicationContext} app
 */
export function installFeaturesImageImports(app) {
  function renderImageImportNotice() {
    const notice = app.state.ui.$("#imageImportNotice");
    if (!notice) return;
    const batch = app.state.image.importBatch;
    const active = batch && !TERMINAL_IMPORT_STATES.has(batch.status);
    const failed = Number(batch?.failed || 0);
    const pollingError = app.state.image.importPollingError;
    notice.classList.toggle("is-hidden", !active && !failed && !pollingError);
    notice.classList.toggle("is-warning", failed > 0 || !!pollingError);
    notice.classList.toggle("is-error", batch?.status === "failed");

    const text = notice.querySelector("#imageImportNoticeText");
    const progress = notice.querySelector("#imageImportProgress");
    const fill = notice.querySelector("#imageImportProgressFill");
    const details = notice.querySelector("#imageImportDetailsBtn");
    if (active) {
      const completed = Number(batch.completed || 0);
      const total = Number(batch.total || 0);
      const current = batch.current_filename ? ` — ${batch.current_filename}` : "";
      if (text) text.textContent = `Preparing ${completed} of ${total} images${current}`;
      if (progress) progress.hidden = false;
      if (fill) fill.style.width = `${total ? Math.round((completed / total) * 100) : 0}%`;
    } else if (pollingError) {
      if (text) text.textContent = "Image preparation was interrupted. Prisma will keep trying.";
      if (progress) progress.hidden = true;
    } else if (failed) {
      if (text) {
        text.textContent = `${failed} ${failed === 1 ? "image could" : "images could"} not be prepared`;
      }
      if (progress) progress.hidden = true;
    }
    if (details) details.hidden = !failed;
  }

  function openImageImportIssues() {
    const modal = app.state.ui.$("#imageImportIssuesModal");
    const list = app.state.ui.$("#imageImportIssuesList");
    const retry = app.state.ui.$("#imageImportRetryBtn");
    const batch = app.state.image.importBatch;
    if (!modal || !list || !batch) return;
    const failed = (batch.items || []).filter((item) => item.status === "failed");
    list.innerHTML = failed.map((item) => `
      <li>
        <strong>${app.commands.esc(item.requested_name || "Unnamed file")}</strong>
        <span>${app.commands.esc(item.error || "Prisma could not prepare this image.")}</span>
      </li>
    `).join("");
    if (retry) retry.textContent = batch.origin === "scan" ? "Retry Failed" : "Choose Files Again";
    modal.classList.remove("is-hidden");
    modal.setAttribute("aria-hidden", "false");
    app.state.ui.$("#imageImportIssuesClose")?.focus();
  }

  function closeImageImportIssues() {
    const modal = app.state.ui.$("#imageImportIssuesModal");
    if (!modal) return;
    modal.classList.add("is-hidden");
    modal.setAttribute("aria-hidden", "true");
    app.state.ui.$("#imageImportDetailsBtn")?.focus();
  }

  async function applyCompletedImageImport(batch, { selectFirst = false, announce = false } = {}) {
    await app.commands.refreshImageLibrary({ announce: false });
    const successful = (batch.items || []).filter((item) => item.status === "complete" && item.stored_name);
    if (selectFirst && successful.length) {
      const selected = app.state.image.availableImages.find(
        (image) => image.filename === successful[0].stored_name,
      ) || null;
      const changed = !!app.state.image.selectedImage?.source_ref
        || selected?.filename !== app.state.image.selectedImage?.filename;
      app.state.image.selectedImage = selected;
      if (selected && changed) app.commands.applyImageAspectDefault();
      app.commands.renderImageTab();
      app.commands.updateRail();
      await app.commands.syncConfigToServer({ showErrorToast: true });
      requestAnimationFrame(() => {
        const card = Array.from(app.state.ui.$$(".image-card")).find(
          (candidate) => candidate.dataset.filename === selected?.filename,
        );
        card?.scrollIntoView({ block: "nearest", inline: "nearest" });
      });
    }

    const succeeded = Number(batch.succeeded || 0);
    const failed = Number(batch.failed || 0);
    if (batch.origin === "upload") {
      if (succeeded && failed) {
        app.commands.showToast(
          `Added ${succeeded} ${succeeded === 1 ? "image" : "images"}; ${failed} could not be prepared`,
          "warn",
        );
      } else if (succeeded) {
        app.commands.showToast(
          `Added ${succeeded} ${succeeded === 1 ? "image" : "images"}`,
          "success",
        );
      } else if (failed) {
        app.commands.showToast("No images could be added", "error");
      }
    } else if (announce) {
      const count = app.state.image.availableImages.length;
      const suffix = failed ? `; ${failed} failed` : "";
      app.commands.showToast(
        `Image library refreshed (${count} ${count === 1 ? "image" : "images"}${suffix})`,
        failed ? "warn" : "success",
      );
    } else if (succeeded) {
      app.commands.showToast(
        `Prepared ${succeeded} new ${succeeded === 1 ? "image" : "images"}`,
        "success",
      );
    }
  }

  async function pollImageImport(batch, options = {}) {
    const batchId = String(batch.batch_id || batch.job_id || "");
    if (!batchId) return null;
    app.state.image.activeImportBatchId = batchId;
    app.state.image.importBatch = batch;
    app.state.image.importPollingError = "";
    app.commands.renderImageImportNotice();
    let lastSucceeded = Number(batch.succeeded || 0);

    const terminal = await app.services.pollJobUntilTerminal({
      jobId: batchId,
      fetchStatus: app.api.getImageImportStatus,
      isTerminal: (status) => TERMINAL_IMPORT_STATES.has(status.status),
      shouldContinue: () => app.state.image.activeImportBatchId === batchId,
      intervalMs: 300,
      onStatus: async (status) => {
        app.state.image.importBatch = status;
        app.state.image.importPollingError = "";
        app.commands.renderImageImportNotice();
        if (Number(status.succeeded || 0) > lastSucceeded) {
          lastSucceeded = Number(status.succeeded || 0);
          await app.commands.refreshImageLibrary({ announce: false });
        }
      },
      onTransientError: (error) => {
        app.state.image.importPollingError = error?.message || "Image preparation status unavailable";
        app.commands.renderImageImportNotice();
      },
    });
    if (!terminal || app.state.image.activeImportBatchId !== batchId) return terminal;
    app.state.image.activeImportBatchId = null;
    app.state.image.importBatch = terminal;
    app.state.image.importPollingError = "";
    await app.commands.applyCompletedImageImport(terminal, options);
    app.commands.renderImageImportNotice();
    return terminal;
  }

  async function startFolderImageRefresh({ announce = false } = {}) {
    const batch = await app.api.refreshImages();
    if (!batch.total) {
      await app.commands.refreshImageLibrary({ announce });
      app.state.image.importBatch = null;
      app.commands.renderImageImportNotice();
      return batch;
    }
    void app.commands.pollImageImport(batch, { announce, selectFirst: false });
    return batch;
  }

  async function startImageBatchImport(files) {
    const chosen = Array.from(files || []);
    if (!chosen.length) return null;
    const batch = await app.api.importImages(chosen);
    void app.commands.pollImageImport(batch, { announce: true, selectFirst: true });
    return batch;
  }

  function bindImageImportEvents() {
    const modal = app.state.ui.$("#imageImportIssuesModal");
    app.lifecycle.listen(app.state.ui.$("#imageImportDetailsBtn"), "click", openImageImportIssues);
    app.lifecycle.listen(app.state.ui.$("#imageImportIssuesClose"), "click", closeImageImportIssues);
    app.lifecycle.listen(app.state.ui.$("#imageImportIssuesDone"), "click", closeImageImportIssues);
    app.lifecycle.listen(app.state.ui.$("#imageImportOpenFolderBtn"), "click", () => {
      app.commands.handleOpenImageLibraryFolder();
    });
    app.lifecycle.listen(app.state.ui.$("#imageImportRetryBtn"), "click", async () => {
      closeImageImportIssues();
      if (app.state.image.importBatch?.origin === "scan") {
        await startFolderImageRefresh({ announce: true });
      } else {
        app.state.ui.$("#imageUploadInput")?.click();
      }
    });
    app.lifecycle.listen(modal, "click", (event) => {
      if (event.target === modal) closeImageImportIssues();
    });
    app.lifecycle.listen(document, "keydown", (event) => {
      if (event.key === "Escape" && modal && !modal.classList.contains("is-hidden")) {
        closeImageImportIssues();
      }
    });
  }

  Object.assign(app.commands, {
    applyCompletedImageImport,
    bindImageImportEvents,
    closeImageImportIssues,
    openImageImportIssues,
    pollImageImport,
    renderImageImportNotice,
    startFolderImageRefresh,
    startImageBatchImport,
  });
}
