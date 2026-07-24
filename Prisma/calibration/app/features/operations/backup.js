/** Install features/operations/backup commands. */
export function installFeaturesOperationsBackup(app) {
  function formatBackupPackageType(packageType = "") {
    const key = String(packageType || "");
    if (key === "core_library") return "Essential Data Only";
    if (key === "working_state_with_raw")
      return "All Data and Artifacts + Raw Images";
    if (key === "working_state_no_raw")
      return "All Data and Artifacts, No Raw Images";
    if (key === "raw_image_archive") return "RAW Image Archive";
    if (key === "normal_backup") return "Legacy Backup";
    if (key === "emergency_core_library_backup")
      return "Emergency Essential Data Backup";
    if (key === "emergency_pre_restore_backup")
      return "Emergency Pre-Restore Backup";
    return key.replace(/_/g, " ") || "Backup";
  }

  function normalizeRestoreConfirmation(value = "") {
    return String(value || "")
      .trim()
      .replace(/\s+/g, " ")
      .toLocaleLowerCase();
  }

  function showBackupRestoreDialog() {
    const overlay = document.createElement("div");
    overlay.className = "info-dialog-overlay backup-restore-overlay";
    const createFileSource = () => ({
      mode: "path",
      pathText: "",
      file: null,
    });
    const state = {
      packageType: "working_state",
      includeRawImages: true,
      restoreSource: createFileSource(),
      rawArchiveSource: createFileSource(),
      rawArchiveCleanupSource: createFileSource(),
      backupCompactResult: null,
      restoreCompactResult: null,
      rawArchiveCompactResult: null,
      rawArchiveCleanupCompactResult: null,
      rawArchiveRestoreCompactResult: null,
      error: "",
    };

    function cleanupRestorePreview(preview) {
      const token = preview?.restore_token;
      if (!token) return;
      app.api.deleteRestorePreview(token).catch((err) => {
        console.warn("Could not clean up restore preview", err);
      });
    }

    function cleanupRawArchivePreview(preview) {
      const token = preview?.archive_token;
      if (!token) return;
      app.api.deleteRawArchivePreview(token).catch((err) => {
        console.warn("Could not clean up RAW archive preview", err);
      });
    }

    const close = () => {
      overlay.remove();
      document.removeEventListener("keydown", handleKeydown);
    };
    const handleKeydown = (event) => {
      if (document.querySelector(".backup-workflow-overlay")) return;
      if (event.key === "Escape") close();
    };

    function warningHtml(warnings = []) {
      if (!warnings.length) return "";
      return `
        <div class="backup-restore-warnings">
          ${warnings.map((warning) => `<div>${app.commands.escapeHtml(warning.message || String(warning))}</div>`).join("")}
        </div>
      `;
    }

    function backupSummaryHtml(result) {
      if (!result) return "";
      const manifest = result.manifest || {};
      const download = result.backup_id
        ? app.api.backupDownloadUrl(result.backup_id)
        : "";
      const packageNoun =
        manifest.package_type === "raw_image_archive" ? "Archive" : "Backup";
      return `
        <div class="backup-restore-result">
          <div class="backup-restore-row"><span>${packageNoun}</span><strong>${app.commands.escapeHtml(result.filename || "")}</strong></div>
          <div class="backup-restore-row"><span>Type</span><strong>${app.commands.escapeHtml(app.commands.formatBackupPackageType(manifest.package_type || ""))}</strong></div>
          <div class="backup-restore-row"><span>Files</span><strong>${Number(manifest.file_count || 0)}</strong></div>
          <div class="backup-restore-row"><span>Size</span><strong>${app.commands.formatFileSize(Number(manifest.size_bytes || 0))}</strong></div>
          <div class="backup-restore-path mono" title="${app.commands.escapeHtml(result.path || "")}">${app.commands.escapeHtml(result.path || "")}</div>
          ${download ? `<a class="ghost-button small backup-restore-download" href="${download}" download="${app.commands.escapeHtml(result.filename || "prisma_backup.zip")}">Download</a>` : ""}
          ${warningHtml(manifest.warnings || [])}
        </div>
      `;
    }

    function operationProgressHtml(job, active, fallbackMessage) {
      if (!job || !active) return "";
      const progress = job.progress || {};
      const indeterminate = progress.indeterminate === true;
      const percent = Number(progress.percent ?? 0);
      const phase = job.message || progress.message || fallbackMessage;
      const currentPath = progress.current_path || "";
      const currentCount = Number(progress.current_count || 0);
      const totalCount = Number(progress.total_count || 0);
      const currentBytes = Number(progress.current_bytes || 0);
      const totalBytes = Number(progress.total_bytes || 0);
      return `
        <div class="backup-progress" role="status" aria-live="polite">
          <div class="backup-progress-topline">
            <strong>${app.commands.escapeHtml(phase)}</strong>
            <span>${indeterminate ? "Working" : `${percent.toFixed(1)}%`}</span>
          </div>
          <div class="backup-progress-bar" aria-hidden="true">
            <div class="backup-progress-fill${indeterminate ? " is-indeterminate" : ""}"${indeterminate ? "" : ` style="width: ${Math.max(0, Math.min(100, percent)).toFixed(1)}%;"`}></div>
          </div>
          <div class="backup-progress-meta">
            ${!indeterminate && totalCount ? `<span>${currentCount} / ${totalCount} files</span>` : ""}
            ${!indeterminate && totalBytes ? `<span>${app.commands.formatFileSize(currentBytes)} / ${app.commands.formatFileSize(totalBytes)}</span>` : ""}
          </div>
          ${currentPath ? `<div class="backup-progress-path mono" title="${app.commands.escapeHtml(currentPath)}">${app.commands.escapeHtml(currentPath)}</div>` : ""}
        </div>
      `;
    }

    function backupErrorMessage(err) {
      const detail = err?.detail || err;
      if (detail && typeof detail === "object") {
        const lines = [
          detail.message || err.message || "Backup creation failed",
        ];
        if (detail.preserved_temp_path) {
          lines.push(
            `Validated package preserved at: ${detail.preserved_temp_path}`,
          );
        }
        if (detail.automatic_recovery) {
          lines.push(
            "Prisma will automatically retry moving this package into the Backups folder.",
          );
        }
        if (detail.intended_final_path) {
          lines.push(`Intended final path: ${detail.intended_final_path}`);
        }
        return lines.filter(Boolean).join("\n");
      }
      return err?.message || "Backup creation failed";
    }

    function compactResultHtml(result, fallbackFilename = "prisma_backup.zip") {
      if (!result?.path) return "";
      const download = result.backup_id
        ? app.api.backupDownloadUrl(result.backup_id)
        : "";
      return `
        <div class="backup-compact-result">
          <input class="backup-compact-path mono" type="text" readonly value="${app.commands.escapeHtml(result.path || "")}" title="${app.commands.escapeHtml(result.path || "")}" aria-label="Created package path">
          ${download ? `<a class="ghost-button small backup-compact-download" href="${download}" download="${app.commands.escapeHtml(result.filename || fallbackFilename)}">Download</a>` : ""}
        </div>
      `;
    }

    function compactStatusHtml(result) {
      if (!result?.message) return "";
      return `<div class="backup-compact-status">${app.commands.escapeHtml(result.message)}</div>`;
    }

    function sourceDisplayValue(source) {
      if (source.mode === "file" && source.file) return source.file.name || "";
      return source.pathText || "";
    }

    function hasFileSource(source) {
      return (
        Boolean(source.mode === "file" && source.file) ||
        Boolean(String(source.pathText || "").trim())
      );
    }

    function sourceControlHtml(id, label, source, placeholder) {
      const value = sourceDisplayValue(source);
      const note =
        source.mode === "file" && source.file
          ? `Selected with file picker: ${source.file.name || ""}`
          : "";
      return `
        <div class="backup-file-source">
          <label class="sidebar-label" for="${id}Path">${app.commands.escapeHtml(label)}</label>
          <div class="backup-file-source-row">
            <input class="backup-file-source-input" type="text" id="${id}Path" value="${app.commands.escapeHtml(value)}" placeholder="${app.commands.escapeHtml(placeholder)}" autocomplete="off" spellcheck="false">
            <label class="ghost-button small backup-file-picker">
              <input type="file" id="${id}File" accept=".zip,application/zip">
              Open File
            </label>
          </div>
          ${note ? `<span class="backup-file-source-note is-upload">${app.commands.escapeHtml(note)}</span>` : ""}
        </div>
      `;
    }

    function updateLauncherActionStates() {
      const restoreBtn = overlay.querySelector("#backupRestoreLaunch");
      if (restoreBtn) restoreBtn.disabled = !hasFileSource(state.restoreSource);
      const rawRestoreBtn = overlay.querySelector("#rawArchiveRestoreLaunch");
      if (rawRestoreBtn)
        rawRestoreBtn.disabled = !hasFileSource(state.rawArchiveSource);
      const rawCleanupBtn = overlay.querySelector("#rawArchiveCleanupLaunch");
      if (rawCleanupBtn)
        rawCleanupBtn.disabled = !hasFileSource(state.rawArchiveCleanupSource);
    }

    function bindSourceControl(id, source) {
      const input = overlay.querySelector(`#${id}Path`);
      input?.addEventListener("input", (event) => {
        source.pathText = event.target.value || "";
        if (source.mode === "file" || source.file) {
          source.mode = "path";
          source.file = null;
          render();
          const nextInput = overlay.querySelector(`#${id}Path`);
          nextInput?.focus();
          if (nextInput) {
            const caret = nextInput.value.length;
            nextInput.setSelectionRange(caret, caret);
          }
          return;
        }
        source.mode = "path";
        updateLauncherActionStates();
      });
      overlay
        .querySelector(`#${id}File`)
        ?.addEventListener("change", (event) => {
          const file = event.target.files?.[0] || null;
          source.file = file;
          source.mode = file ? "file" : "path";
          source.pathText = "";
          state.error = "";
          render();
        });
    }

    async function validateRestoreSource(source) {
      if (source.mode === "file" && source.file) {
        return app.api.validateRestoreBackup(source.file);
      }
      return app.api.validateRestoreBackupPath(
        String(source.pathText || "").trim(),
      );
    }

    async function validateRawArchiveSource(source) {
      if (source.mode === "file" && source.file) {
        return app.api.validateRawArchive(source.file);
      }
      return app.api.validateRawArchivePath(
        String(source.pathText || "").trim(),
      );
    }

    function createWorkflowHost(title, stateRef, bodyHtml, bindBody, onClose) {
      const workflowOverlay = document.createElement("div");
      workflowOverlay.className = "info-dialog-overlay backup-workflow-overlay";
      const workflow = {
        render() {
          const busy = Boolean(stateRef.busy);
          workflowOverlay.innerHTML = `
            <div class="info-dialog backup-workflow-dialog" role="dialog" aria-modal="true" aria-labelledby="backupWorkflowTitle">
              ${app.commands.renderDialogHeader({
                title,
                titleId: "backupWorkflowTitle",
                closeButtonHtml: app.commands.renderWindowCloseButton({
                  id: "backupWorkflowClose",
                  className: "info-dialog-close",
                  disabled: busy,
                }),
              })}
              <div class="info-dialog-body backup-workflow-body">
                ${bodyHtml()}
              </div>
            </div>
          `;
          const closeWorkflow = () => {
            if (stateRef.busy) return;
            if (onClose) onClose();
            workflowOverlay.remove();
          };
          workflowOverlay
            .querySelector("#backupWorkflowClose")
            ?.addEventListener("click", closeWorkflow);
          bindBody?.(workflow);
        },
        isConnected() {
          return workflowOverlay.isConnected;
        },
      };
      document.body.appendChild(workflowOverlay);
      workflow.render();
      return workflow;
    }

    function restoreSummaryHtml(preview) {
      if (!preview) {
        return "";
      }
      const summary = preview.summary || {};
      const restoreSupported =
        summary.restore_supported !== false && Boolean(preview.restore_token);
      const restoreImpact = summary.restore_impact || "";
      const safety = summary.safety_backup || {};
      const impactText = (() => {
        if (summary.restore_support_reason)
          return summary.restore_support_reason;
        if (restoreImpact === "replace_library")
          return "This restore will replace the current Prisma library, including source images.";
        if (restoreImpact === "replace_library_except_source_images")
          return "This restore will replace the current Prisma library state but preserve current source images.";
        if (restoreImpact === "replace_core_database")
          return "This restore will replace only the Prisma database/core semantic state.";
        if (restoreImpact === "raw_archive_import_only")
          return "This is a RAW image archive. It cannot be used for library restore.";
        return "Review the detected package type before restoring.";
      })();
      const safetyText = safety.required
        ? safety.recent_available
          ? `Recent core backup found: ${safety.newest_core_created_at || ""}`
          : "No recent core backup was found. Prisma will create a safety backup before restore."
        : "No pre-restore safety backup is required for this package.";
      return `
        <div class="backup-restore-result">
          <div class="backup-restore-row"><span>Package</span><strong>${app.commands.escapeHtml(summary.source_filename || "")}</strong></div>
          <div class="backup-restore-row"><span>Type</span><strong>${app.commands.escapeHtml(app.commands.formatBackupPackageType(summary.package_type || ""))}</strong></div>
          <div class="backup-restore-row"><span>Raw images</span><strong>${summary.contains_raw_images ? "Included" : "Not included"}</strong></div>
          <div class="backup-restore-row"><span>Assets</span><strong>${Number(summary.asset_file_count || 0)}</strong></div>
          <div class="backup-restore-row"><span>STEP/STL</span><strong>${Number(summary.step_export_file_count || 0)}</strong></div>
          <div class="backup-restore-row"><span>SQLite</span><strong>${app.commands.formatFileSize(Number(summary.sqlite_size_bytes || 0))}</strong></div>
          ${warningHtml(summary.warnings || [])}
        </div>
        <div class="backup-restore-message ${restoreSupported ? "is-warning" : "is-error"}">${app.commands.escapeHtml(impactText)}</div>
        <div class="backup-restore-message is-warning">${app.commands.escapeHtml(safetyText)}</div>
      `;
    }

    function restoreResultHtml(result) {
      if (!result) return "";
      const preserved = result.preserved || {};
      const audit = result.audit || {};
      const warnings = result.warnings || [];
      const currentRaw = Number(preserved.current_raw_file_count || 0);
      const referencedRaw = Number(preserved.referenced_raw_file_count || 0);
      const orphanRaw = Number(preserved.orphan_raw_file_count || 0);
      const missingRefs = Number(audit.missing_referenced_file_count || 0);
      const staleRefs = Number(audit.stale_referenced_file_count || 0);
      return `
        <div class="backup-restore-result">
          <div class="backup-restore-row"><span>Pre-restore backup</span><strong>${app.commands.escapeHtml(result.pre_restore_backup_id || "")}</strong></div>
          <div class="backup-restore-path mono" title="${app.commands.escapeHtml(result.pre_restore_backup_path || "")}">${app.commands.escapeHtml(result.pre_restore_backup_path || "")}</div>
          ${
            currentRaw || referencedRaw || orphanRaw
              ? `
            <div class="backup-restore-row"><span>Preserved RAW files</span><strong>${currentRaw}</strong></div>
            <div class="backup-restore-row"><span>Referenced RAW files</span><strong>${referencedRaw}</strong></div>
            <div class="backup-restore-row"><span>Unreferenced RAW files</span><strong>${orphanRaw}</strong></div>
          `
              : ""
          }
          ${
            missingRefs || staleRefs
              ? `
            <div class="backup-restore-row"><span>Missing referenced files</span><strong>${missingRefs}</strong></div>
            <div class="backup-restore-row"><span>Hash mismatches</span><strong>${staleRefs}</strong></div>
          `
              : ""
          }
          ${warningHtml(warnings)}
        </div>
      `;
    }

    function rawArchiveSummaryHtml(preview, options = {}) {
      if (!preview) return "";
      const summary = preview.summary || {};
      const counts = summary.reconciliation?.counts || {};
      const warnings = summary.warnings || [];
      const isCleanup = options.mode === "cleanup";
      return `
        <div class="backup-restore-result">
          <div class="backup-restore-row"><span>Package</span><strong>${app.commands.escapeHtml(summary.source_filename || "")}</strong></div>
          <div class="backup-restore-row"><span>Type</span><strong>${app.commands.escapeHtml(app.commands.formatBackupPackageType(summary.package_type || ""))}</strong></div>
          <div class="backup-restore-row"><span>Source images</span><strong>${Number(summary.source_image_count || 0)}</strong></div>
          <div class="backup-restore-row"><span>Source bytes</span><strong>${app.commands.formatFileSize(Number(summary.source_image_bytes || 0))}</strong></div>
          ${
            isCleanup
              ? `
            <div class="backup-restore-row"><span>In active library</span><strong>${Number(counts.already_present || 0)}</strong></div>
          `
              : `
            <div class="backup-restore-row"><span>Restorable missing</span><strong>${Number(counts.restorable_missing || 0)}</strong></div>
            <div class="backup-restore-row"><span>Already present</span><strong>${Number(counts.already_present || 0)}</strong></div>
          `
          }
          <div class="backup-restore-row"><span>Conflicts</span><strong>${Number(counts.present_conflict || 0) + Number(counts.archive_conflict || 0)}</strong></div>
          ${
            isCleanup
              ? `
            <div class="backup-restore-row"><span>Active not in archive</span><strong>${Number(counts.not_in_archive || 0)}</strong></div>
          `
              : `
            <div class="backup-restore-row"><span>Archive-only</span><strong>${Number(counts.archive_only || 0)}</strong></div>
            <div class="backup-restore-row"><span>Current not in archive</span><strong>${Number(counts.not_in_archive || 0)}</strong></div>
          `
          }
          ${warningHtml(warnings)}
        </div>
      `;
    }

    function rawArchiveImportResultHtml(result) {
      if (!result) return "";
      const thumbnailSummary = result.thumbnail_regeneration || {};
      const thumbnailCandidateCount = Number(
        thumbnailSummary.candidate_count || 0,
      );
      const missingVisualSamples = Array.isArray(thumbnailSummary.still_missing)
        ? thumbnailSummary.still_missing.length
        : 0;
      return `
        <div class="backup-restore-result">
          <div class="backup-restore-row"><span>Restored</span><strong>${Number(result.restored_count || 0)}</strong></div>
          <div class="backup-restore-row"><span>Restored bytes</span><strong>${app.commands.formatFileSize(Number(result.restored_size_bytes || 0))}</strong></div>
          <div class="backup-restore-row"><span>Already present</span><strong>${Number(result.already_present_count || 0)}</strong></div>
          <div class="backup-restore-row"><span>Skipped</span><strong>${Number(result.skipped_count || 0)}</strong></div>
          <div class="backup-restore-row"><span>Conflicts</span><strong>${Number(result.conflict_count || 0)}</strong></div>
          ${thumbnailCandidateCount && missingVisualSamples ? `<div class="backup-restore-row"><span>Extraction visuals</span><strong>${missingVisualSamples} samples need Maintenance rebuild</strong></div>` : ""}
          ${warningHtml(result.warnings || [])}
        </div>
      `;
    }

    function rawArchiveReleaseResultHtml(result) {
      if (!result) return "";
      return `
        <div class="backup-restore-result">
          <div class="backup-restore-row"><span>Removed</span><strong>${Number(result.released_count || 0)}</strong></div>
          <div class="backup-restore-row"><span>Freed</span><strong>${app.commands.formatFileSize(Number(result.released_size_bytes || 0))}</strong></div>
          <div class="backup-restore-row"><span>Skipped</span><strong>${Number(result.skipped_count || 0)}</strong></div>
          <div class="backup-restore-row"><span>Conflicts</span><strong>${Number(result.conflict_count || 0)}</strong></div>
          <div class="backup-restore-row"><span>Failures</span><strong>${Number(result.failure_count || 0)}</strong></div>
          ${warningHtml(result.warnings || [])}
        </div>
      `;
    }

    function render() {
      const isCoreBackup = state.packageType === "core_library";
      overlay.innerHTML = `
        <div class="info-dialog backup-restore-dialog" role="dialog" aria-modal="true" aria-labelledby="backupRestoreTitle">
          ${app.commands.renderDialogHeader({
            title: "Backup / Restore",
            titleId: "backupRestoreTitle",
            closeButtonHtml: app.commands.renderWindowCloseButton({
              id: "backupRestoreClose",
              className: "info-dialog-close",
            }),
          })}
          <div class="info-dialog-body backup-restore-body">
            ${state.error ? `<div class="backup-restore-message is-error">${app.commands.escapeHtml(state.error)}</div>` : ""}
            <section class="backup-restore-panel">
              <div class="drawer-module-cap">
                <span class="sidebar-label">Create Backup</span>
              </div>
              <div class="backup-restore-panel-body">
                <p class="backup-restore-instruction">Select backup type</p>
                <div class="backup-type-grid" role="radiogroup" aria-label="Backup type">
                  <div class="backup-type-card ${isCoreBackup ? "is-active" : "is-inactive"}" id="backupPackageCore" role="radio" aria-checked="${isCoreBackup ? "true" : "false"}" tabindex="0" data-backup-package="core_library">
                    <span class="maintenance-operation-radio" aria-hidden="true"></span>
                    <span class="maintenance-operation-copy">
                      <span class="maintenance-operation-title">Essential Data Only</span>
                      <span class="maintenance-operation-description">Small backup of the Prisma database and essential semantic state.</span>
                      <span class="maintenance-operation-meta">Does not include source images, model artifacts, or generated exports.</span>
                    </span>
                  </div>
                  <div class="backup-type-card ${!isCoreBackup ? "is-active" : "is-inactive"}" id="backupPackageWorking" role="radio" aria-checked="${!isCoreBackup ? "true" : "false"}" tabindex="0" data-backup-package="working_state">
                    <span class="maintenance-operation-radio" aria-hidden="true"></span>
                    <span class="maintenance-operation-copy">
                      <span class="maintenance-operation-title">All Data and Artifacts</span>
                      <span class="maintenance-operation-description">Preserves the operational Prisma library, including derived artifacts and STEP/STL exports.</span>
                      ${
                        !isCoreBackup
                          ? `
                        <label class="backup-raw-toggle">
                          <input type="checkbox" id="backupIncludeRawImages" ${state.includeRawImages ? "checked" : ""}>
                          <span>Include raw image files</span>
                        </label>
                        <span class="maintenance-operation-meta">Turning this off keeps SQLite, extracted data, generated artifacts, and geometry exports, but omits camera raw source files from the ZIP.</span>
                      `
                          : `<span class="maintenance-operation-meta">Source image files can be included after selecting this option.</span>`
                      }
                    </span>
                  </div>
                </div>
                ${compactResultHtml(state.backupCompactResult)}
                <div class="backup-restore-actions">
                  <button class="primary-button small" type="button" id="backupCreateBtn">Create Backup</button>
                </div>
              </div>
            </section>
            <section class="backup-restore-panel">
              <div class="drawer-module-cap">
                <span class="sidebar-label">Restore From Backup</span>
              </div>
              <div class="backup-restore-panel-body">
                <p class="backup-restore-instruction">Enter or select a Prisma backup ZIP. Restore will validate the package before any changes are made.</p>
                ${sourceControlHtml("backupRestoreSource", "Backup File", state.restoreSource, "Type or select the backup .zip file path")}
                ${compactStatusHtml(state.restoreCompactResult)}
                <div class="backup-restore-actions">
                  <button class="primary-button small" type="button" id="backupRestoreLaunch" ${!hasFileSource(state.restoreSource) ? "disabled" : ""}>Restore Backup</button>
                </div>
              </div>
            </section>
            <section class="backup-restore-panel">
              <div class="drawer-module-cap">
                <span class="sidebar-label">Archive RAW Images</span>
              </div>
              <div class="backup-restore-panel-body">
                <p class="backup-restore-instruction">Create a source image archive, then optionally remove archived images from the active library after the archive has been verified.</p>
                <div class="raw-archive-operation-grid">
                  <div class="raw-archive-operation">
                    <div class="drawer-module-cap">
                      <span class="sidebar-label">Create Archive</span>
                    </div>
                    <div class="raw-archive-operation-body">
                      <p class="small-copy">Package current source images into a RAW image archive without deleting or moving active files.</p>
                      ${compactResultHtml(state.rawArchiveCompactResult, "prisma_raw_image_archive.zip")}
                      <div class="raw-archive-operation-actions">
                        <button class="primary-button small" type="button" id="rawArchiveCreateBtn">Create RAW Image Archive</button>
                      </div>
                    </div>
                  </div>
                  <div class="raw-archive-operation">
                    <div class="drawer-module-cap">
                      <span class="sidebar-label">Remove Archived Images From Active Library</span>
                    </div>
                    <div class="raw-archive-operation-body">
                      <p class="small-copy">After storing source images somewhere safe, remove matching source image files from the active Prisma library.</p>
                      ${sourceControlHtml("rawArchiveCleanupSource", "Archive or Backup File", state.rawArchiveCleanupSource, "Type or select the RAW archive or all-data backup .zip file path")}
                      ${compactStatusHtml(state.rawArchiveCleanupCompactResult)}
                      <div class="raw-archive-operation-actions">
                        <button class="delete-button small" type="button" id="rawArchiveCleanupLaunch" ${!hasFileSource(state.rawArchiveCleanupSource) ? "disabled" : ""}>Remove Archived Images From Active Library</button>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            </section>
            <section class="backup-restore-panel">
              <div class="drawer-module-cap">
                <span class="sidebar-label">Restore Archived Images</span>
              </div>
              <div class="backup-restore-panel-body">
                <p class="backup-restore-instruction">Enter or select a RAW image archive ZIP or an all-data backup ZIP that includes RAW images. Prisma will restore only missing compatible source images.</p>
                ${sourceControlHtml("rawArchiveSource", "Archive or Backup File", state.rawArchiveSource, "Type or select the RAW archive or all-data backup .zip file path")}
                ${compactStatusHtml(state.rawArchiveRestoreCompactResult)}
                <div class="backup-restore-actions">
                  <button class="primary-button small" type="button" id="rawArchiveRestoreLaunch" ${!hasFileSource(state.rawArchiveSource) ? "disabled" : ""}>Restore Archived Images</button>
                </div>
              </div>
            </section>
          </div>
        </div>
      `;
      bind();
    }

    async function pollBackupWorkflowJob(
      host,
      workflowState,
      jobId,
      fallbackMessage,
    ) {
      return app.commands.pollJobUntilTerminal({
        jobId,
        fetchStatus: () => app.api.fetchBackupJobStatus(jobId),
        isTerminal: (job) =>
          ["succeeded", "failed", "cancelled"].includes(
            String(job.status || ""),
          ),
        shouldContinue: () => host.isConnected(),
        intervalMs: 800,
        onStatus: (job) => {
          workflowState.job = job;
          host.render();
        },
        onTransientError: () => {
          workflowState.job = {
            ...(workflowState.job || {}),
            job_id: jobId,
            message: `Connection interrupted; retrying ${fallbackMessage || "operation"} status...`,
          };
          host.render();
        },
      });
    }

    function showCreateBackupWorkflow() {
      const workflowState = {
        busy: true,
        starting: true,
        job: null,
        result: null,
        error: "",
      };
      const workflow = createWorkflowHost(
        "Create Backup",
        workflowState,
        () => `
          ${workflowState.error ? `<div class="backup-restore-message is-error">${app.commands.escapeHtml(workflowState.error)}</div>` : ""}
          ${workflowState.result ? `<div class="backup-restore-message is-success">Backup created.</div>` : ""}
          ${operationProgressHtml(workflowState.job, workflowState.busy, "Creating backup")}
          ${workflowState.starting && !workflowState.job ? `<div class="backup-restore-message">Starting backup...</div>` : ""}
          ${backupSummaryHtml(workflowState.result)}
        `,
        null,
      );
      async function poll(jobId) {
        try {
          const job = await pollBackupWorkflowJob(
            workflow,
            workflowState,
            jobId,
            "backup",
          );
          if (!job) return;
          if (job.status === "succeeded") {
            workflowState.result = job.result || null;
            workflowState.busy = false;
            state.backupCompactResult = workflowState.result;
            render();
            workflow.render();
            return;
          }
          workflowState.error =
            job.status === "cancelled"
              ? "Backup cancelled."
              : backupErrorMessage(job.error || job);
          workflowState.busy = false;
          workflow.render();
        } catch (err) {
          workflowState.error = backupErrorMessage(err);
          workflowState.busy = false;
          workflow.render();
        }
      }
      (async () => {
        try {
          const job = await app.api.createBackupJob({
            packageType: state.packageType,
            includeRawImages:
              state.packageType === "working_state"
                ? state.includeRawImages
                : false,
          });
          workflowState.starting = false;
          workflowState.job = job;
          workflow.render();
          const jobId = String(job?.job_id || "");
          if (!jobId) throw new Error("Backup did not return a job id.");
          void poll(jobId);
        } catch (err) {
          workflowState.error = backupErrorMessage(err);
          workflowState.busy = false;
          workflowState.starting = false;
          workflow.render();
        }
      })();
    }

    function showRawArchiveCreateWorkflow() {
      const workflowState = {
        busy: true,
        starting: true,
        job: null,
        result: null,
        error: "",
      };
      const workflow = createWorkflowHost(
        "Archive RAW Images",
        workflowState,
        () => `
          ${workflowState.error ? `<div class="backup-restore-message is-error">${app.commands.escapeHtml(workflowState.error)}</div>` : ""}
          ${workflowState.result ? `<div class="backup-restore-message is-success">RAW image archive created.</div>` : ""}
          ${operationProgressHtml(workflowState.job, workflowState.busy, "Creating RAW archive")}
          ${workflowState.starting && !workflowState.job ? `<div class="backup-restore-message">Starting RAW image archive...</div>` : ""}
          ${backupSummaryHtml(workflowState.result)}
        `,
        null,
      );
      async function poll(jobId) {
        try {
          const job = await pollBackupWorkflowJob(
            workflow,
            workflowState,
            jobId,
            "RAW archive",
          );
          if (!job) return;
          if (job.status === "succeeded") {
            workflowState.result = job.result || null;
            workflowState.busy = false;
            state.rawArchiveCompactResult = workflowState.result;
            if (workflowState.result?.path) {
              state.rawArchiveCleanupSource = {
                mode: "path",
                pathText: workflowState.result.path,
                file: null,
              };
            }
            render();
            workflow.render();
            return;
          }
          workflowState.error =
            job.status === "cancelled"
              ? "RAW archive creation cancelled."
              : backupErrorMessage(job.error || job);
          workflowState.busy = false;
          workflow.render();
        } catch (err) {
          workflowState.error = backupErrorMessage(err);
          workflowState.busy = false;
          workflow.render();
        }
      }
      (async () => {
        try {
          const job = await app.api.createRawArchiveJob();
          workflowState.starting = false;
          workflowState.job = job;
          workflow.render();
          const jobId = String(job?.job_id || "");
          if (!jobId) throw new Error("RAW archive did not return a job id.");
          void poll(jobId);
        } catch (err) {
          workflowState.error = backupErrorMessage(err);
          workflowState.busy = false;
          workflowState.starting = false;
          workflow.render();
        }
      })();
    }

    function showRawArchiveCleanupWorkflow() {
      const source = { ...state.rawArchiveCleanupSource };
      const cleanupPhrase = "Remove archived images from active library";
      const workflowState = {
        busy: true,
        validating: true,
        removing: false,
        preview: null,
        confirmation: "",
        job: null,
        result: null,
        error: "",
      };
      const workflow = createWorkflowHost(
        "Remove Archived Images From Active Library",
        workflowState,
        () => {
          const counts =
            workflowState.preview?.summary?.reconciliation?.counts || {};
          const removalCandidateCount = Number(counts.already_present || 0);
          const canRemove =
            removalCandidateCount > 0 &&
            app.commands.normalizeRestoreConfirmation(
              workflowState.confirmation,
            ) === app.commands.normalizeRestoreConfirmation(cleanupPhrase) &&
            !workflowState.busy;
          const noRemovalNeeded =
            workflowState.preview && removalCandidateCount <= 0;
          return `
            ${workflowState.validating ? `<div class="backup-restore-message">Validating RAW image archive...</div>` : ""}
            ${workflowState.error ? `<div class="backup-restore-message is-error">${app.commands.escapeHtml(workflowState.error)}</div>` : ""}
            ${rawArchiveSummaryHtml(workflowState.preview, { mode: "cleanup" })}
            ${noRemovalNeeded ? `<div class="backup-restore-message is-success">No matching active-library source images are covered by this archive.</div>` : ""}
            ${
              workflowState.preview?.archive_token &&
              !workflowState.result &&
              removalCandidateCount > 0
                ? `
              <div class="backup-remove-panel">
                <div class="backup-restore-message is-warning">
                  This will delete matching local RAW/source image files from the active Prisma library.
                  Continue only after this archive has been stored somewhere safe.
                </div>
                <label class="sample-create-field backup-restore-confirm">
                  <span class="sidebar-label">Type confirmation phrase</span>
                  <span class="backup-restore-confirm-phrase">${app.commands.escapeHtml(cleanupPhrase)}</span>
                  <input type="text" id="rawArchiveRemoveConfirm" value="${app.commands.escapeHtml(workflowState.confirmation)}" autocomplete="off" spellcheck="false" ${workflowState.busy ? "disabled" : ""}>
                </label>
                <div class="backup-workflow-actions">
                  <button class="delete-button small" type="button" id="rawArchiveWorkflowRemoveStart" ${!canRemove ? "disabled" : ""}>Remove Archived Images From Active Library</button>
                </div>
              </div>
            `
                : ""
            }
            ${operationProgressHtml(workflowState.job, workflowState.removing, "Removing archived images")}
            ${workflowState.result ? `<div class="backup-restore-message is-success">Archived images removed from active library.</div>` : ""}
            ${rawArchiveReleaseResultHtml(workflowState.result)}
          `;
        },
        (host) => {
          const confirmationInput = document.getElementById(
            "rawArchiveRemoveConfirm",
          );
          confirmationInput?.addEventListener("input", (event) => {
            workflowState.confirmation = event.target.value || "";
            const button = document.getElementById(
              "rawArchiveWorkflowRemoveStart",
            );
            if (button) {
              button.disabled =
                app.commands.normalizeRestoreConfirmation(
                  workflowState.confirmation,
                ) !== app.commands.normalizeRestoreConfirmation(cleanupPhrase);
            }
          });
          document
            .getElementById("rawArchiveWorkflowRemoveStart")
            ?.addEventListener("click", async () => {
              const token = workflowState.preview?.archive_token;
              if (!token) return;
              workflowState.busy = true;
              workflowState.removing = true;
              workflowState.error = "";
              workflowState.result = null;
              workflowState.job = null;
              host.render();
              try {
                const job = await app.api.createRawArchiveReleaseJob(
                  token,
                  workflowState.confirmation,
                );
                workflowState.job = job;
                host.render();
                const jobId = String(job?.job_id || "");
                if (!jobId)
                  throw new Error(
                    "Archived image removal did not return a job id.",
                  );
                const nextJob = await pollBackupWorkflowJob(
                  host,
                  workflowState,
                  jobId,
                  "archived image removal",
                );
                if (!nextJob) return;
                if (nextJob.status === "succeeded") {
                  workflowState.result = nextJob.result || null;
                  workflowState.preview = null;
                  workflowState.busy = false;
                  workflowState.removing = false;
                  state.rawArchiveCleanupSource = createFileSource();
                  state.rawArchiveCleanupCompactResult = {
                    message: "Archived images removed from active library.",
                  };
                  await app.commands.handleRefresh({ reloadImportData: true });
                  render();
                  host.render();
                  return;
                }
                workflowState.error =
                  nextJob.status === "cancelled"
                    ? "Archived image removal cancelled."
                    : backupErrorMessage(nextJob.error || nextJob);
                workflowState.busy = false;
                workflowState.removing = false;
                host.render();
              } catch (err) {
                workflowState.error =
                  err.message || "Archived image removal failed";
                workflowState.busy = false;
                workflowState.removing = false;
                host.render();
              }
            });
        },
        () => cleanupRawArchivePreview(workflowState.preview),
      );
      (async () => {
        try {
          workflowState.preview = await validateRawArchiveSource(source);
          workflowState.validating = false;
          workflowState.busy = false;
          workflow.render();
        } catch (err) {
          workflowState.error = err.message || "RAW archive validation failed";
          workflowState.validating = false;
          workflowState.busy = false;
          workflow.render();
        }
      })();
    }

    function showRestoreBackupWorkflow() {
      const source = { ...state.restoreSource };
      const workflowState = {
        busy: true,
        validating: true,
        restoring: false,
        preview: null,
        confirmation: "",
        job: null,
        result: null,
        error: "",
      };
      const workflow = createWorkflowHost(
        "Restore Backup",
        workflowState,
        () => {
          const summary = workflowState.preview?.summary || {};
          const requiredConfirmation = summary.required_confirmation || "";
          const canRestore =
            Boolean(workflowState.preview?.restore_token) &&
            summary.restore_supported !== false &&
            app.commands.normalizeRestoreConfirmation(
              workflowState.confirmation,
            ) ===
              app.commands.normalizeRestoreConfirmation(requiredConfirmation) &&
            !workflowState.busy;
          return `
            ${operationProgressHtml(
              {
                message: "Validating backup...",
                progress: { indeterminate: true },
              },
              workflowState.validating,
              "Validating backup...",
            )}
            ${workflowState.error ? `<div class="backup-restore-message is-error">${app.commands.escapeHtml(workflowState.error)}</div>` : ""}
            ${restoreSummaryHtml(workflowState.preview)}
            ${
              workflowState.preview?.restore_token &&
              summary.restore_supported !== false &&
              !workflowState.result
                ? `
              <label class="sample-create-field backup-restore-confirm">
                <span class="sidebar-label">Type confirmation phrase</span>
                <span class="backup-restore-confirm-phrase">${app.commands.escapeHtml(requiredConfirmation)}</span>
                <input type="text" id="backupWorkflowConfirm" value="${app.commands.escapeHtml(workflowState.confirmation)}" autocomplete="off" spellcheck="false" ${workflowState.busy ? "disabled" : ""}>
              </label>
              <div class="backup-workflow-actions">
                <button class="primary-button small" type="button" id="backupWorkflowRestoreStart" ${!canRestore ? "disabled" : ""}>Restore Backup</button>
              </div>
            `
                : ""
            }
            ${operationProgressHtml(workflowState.job, workflowState.restoring, "Restoring backup")}
            ${workflowState.result ? `<div class="backup-restore-message is-success">Restore complete. Data refreshed.</div>` : ""}
            ${restoreResultHtml(workflowState.result)}
          `;
        },
        (host) => {
          const input = document.getElementById("backupWorkflowConfirm");
          input?.addEventListener("input", (event) => {
            workflowState.confirmation = event.target.value || "";
            const expected =
              workflowState.preview?.summary?.required_confirmation || "";
            const button = document.getElementById(
              "backupWorkflowRestoreStart",
            );
            if (button) {
              button.disabled =
                app.commands.normalizeRestoreConfirmation(
                  workflowState.confirmation,
                ) !== app.commands.normalizeRestoreConfirmation(expected);
            }
          });
          document
            .getElementById("backupWorkflowRestoreStart")
            ?.addEventListener("click", async () => {
              const token = workflowState.preview?.restore_token;
              const expected =
                workflowState.preview?.summary?.required_confirmation || "";
              if (
                !token ||
                app.commands.normalizeRestoreConfirmation(
                  workflowState.confirmation,
                ) !== app.commands.normalizeRestoreConfirmation(expected)
              )
                return;
              workflowState.busy = true;
              workflowState.restoring = true;
              workflowState.error = "";
              workflowState.result = null;
              workflowState.job = null;
              host.render();
              try {
                const job = await app.api.createRestoreJob(
                  token,
                  workflowState.confirmation,
                );
                workflowState.job = job;
                host.render();
                const jobId = String(job?.job_id || "");
                if (!jobId) throw new Error("Restore did not return a job id.");
                const nextJob = await pollBackupWorkflowJob(
                  host,
                  workflowState,
                  jobId,
                  "restore",
                );
                if (!nextJob) return;
                if (nextJob.status === "succeeded") {
                  workflowState.result = nextJob.result || null;
                  workflowState.preview = null;
                  workflowState.busy = false;
                  workflowState.restoring = false;
                  state.restoreSource = createFileSource();
                  state.restoreCompactResult = {
                    message: "Restore complete. Data refreshed.",
                  };
                  await app.commands.handleRefresh({ reloadImportData: true });
                  render();
                  host.render();
                  return;
                }
                workflowState.error =
                  nextJob.status === "cancelled"
                    ? "Restore cancelled."
                    : backupErrorMessage(nextJob.error || nextJob);
                workflowState.busy = false;
                workflowState.restoring = false;
                host.render();
              } catch (err) {
                workflowState.error = err.message || "Restore failed";
                workflowState.busy = false;
                workflowState.restoring = false;
                host.render();
              }
            });
        },
        () => cleanupRestorePreview(workflowState.preview),
      );
      (async () => {
        try {
          workflowState.preview = await validateRestoreSource(source);
          workflowState.validating = false;
          workflowState.busy = false;
          workflow.render();
        } catch (err) {
          workflowState.error = err.message || "Backup validation failed";
          workflowState.validating = false;
          workflowState.busy = false;
          workflow.render();
        }
      })();
    }

    function showRawArchiveRestoreWorkflow() {
      const source = { ...state.rawArchiveSource };
      const workflowState = {
        busy: true,
        validating: true,
        importing: false,
        preview: null,
        job: null,
        result: null,
        error: "",
      };
      const workflow = createWorkflowHost(
        "Restore Archived Images",
        workflowState,
        () => {
          const counts =
            workflowState.preview?.summary?.reconciliation?.counts || {};
          const canRestore =
            Number(counts.restorable_missing || 0) > 0 && !workflowState.busy;
          const noRestoreNeeded =
            workflowState.preview &&
            Number(counts.restorable_missing || 0) <= 0;
          return `
            ${workflowState.validating ? `<div class="backup-restore-message">Validating RAW image archive...</div>` : ""}
            ${workflowState.error ? `<div class="backup-restore-message is-error">${app.commands.escapeHtml(workflowState.error)}</div>` : ""}
            ${rawArchiveSummaryHtml(workflowState.preview)}
            ${noRestoreNeeded ? `<div class="backup-restore-message is-success">No missing compatible source images need to be restored.</div>` : ""}
            ${
              workflowState.preview?.archive_token &&
              !workflowState.result &&
              !noRestoreNeeded
                ? `
              <div class="backup-workflow-actions">
                <button class="primary-button small" type="button" id="rawArchiveWorkflowRestoreStart" ${!canRestore ? "disabled" : ""}>Restore Archived Images</button>
              </div>
            `
                : ""
            }
            ${operationProgressHtml(workflowState.job, workflowState.importing, "Restoring source images")}
            ${workflowState.result ? `<div class="backup-restore-message is-success">Archived image restore complete.</div>` : ""}
            ${rawArchiveImportResultHtml(workflowState.result)}
          `;
        },
        (host) => {
          document
            .getElementById("rawArchiveWorkflowRestoreStart")
            ?.addEventListener("click", async () => {
              const token = workflowState.preview?.archive_token;
              if (!token) return;
              workflowState.busy = true;
              workflowState.importing = true;
              workflowState.error = "";
              workflowState.result = null;
              workflowState.job = null;
              host.render();
              try {
                const job = await app.api.createRawArchiveImportJob(token);
                workflowState.job = job;
                host.render();
                const jobId = String(job?.job_id || "");
                if (!jobId)
                  throw new Error(
                    "Archived image restore did not return a job id.",
                  );
                const nextJob = await pollBackupWorkflowJob(
                  host,
                  workflowState,
                  jobId,
                  "archived image restore",
                );
                if (!nextJob) return;
                if (nextJob.status === "succeeded") {
                  workflowState.result = nextJob.result || null;
                  workflowState.preview = null;
                  workflowState.busy = false;
                  workflowState.importing = false;
                  state.rawArchiveSource = createFileSource();
                  state.rawArchiveRestoreCompactResult = {
                    message: "Archived image restore complete.",
                  };
                  await app.commands.handleRefresh({ reloadImportData: true });
                  render();
                  host.render();
                  return;
                }
                workflowState.error =
                  nextJob.status === "cancelled"
                    ? "Archived image restore cancelled."
                    : backupErrorMessage(nextJob.error || nextJob);
                workflowState.busy = false;
                workflowState.importing = false;
                host.render();
              } catch (err) {
                workflowState.error =
                  err.message || "RAW archive import failed";
                workflowState.busy = false;
                workflowState.importing = false;
                host.render();
              }
            });
        },
        () => cleanupRawArchivePreview(workflowState.preview),
      );
      (async () => {
        try {
          workflowState.preview = await validateRawArchiveSource(source);
          workflowState.validating = false;
          workflowState.busy = false;
          workflow.render();
        } catch (err) {
          workflowState.error = err.message || "RAW archive validation failed";
          workflowState.validating = false;
          workflowState.busy = false;
          workflow.render();
        }
      })();
    }

    function bind() {
      overlay
        .querySelector("#backupRestoreClose")
        ?.addEventListener("click", close);
      overlay.querySelectorAll("[data-backup-package]").forEach((card) => {
        const selectPackage = () => {
          const packageType = card.dataset.backupPackage || "";
          if (!packageType || state.packageType === packageType) return;
          state.packageType = packageType;
          state.error = "";
          render();
        };
        card.addEventListener("click", selectPackage);
        card.addEventListener("keydown", (event) => {
          if (event.key !== "Enter" && event.key !== " ") return;
          event.preventDefault();
          selectPackage();
        });
      });
      overlay
        .querySelector(".backup-raw-toggle")
        ?.addEventListener("click", (event) => {
          event.stopPropagation();
        });
      overlay
        .querySelector("#backupIncludeRawImages")
        ?.addEventListener("click", (event) => {
          event.stopPropagation();
        });
      overlay
        .querySelector("#backupIncludeRawImages")
        ?.addEventListener("change", (event) => {
          event.stopPropagation();
          state.includeRawImages = Boolean(event.target.checked);
          state.error = "";
          render();
        });
      bindSourceControl("backupRestoreSource", state.restoreSource);
      bindSourceControl("rawArchiveSource", state.rawArchiveSource);
      bindSourceControl(
        "rawArchiveCleanupSource",
        state.rawArchiveCleanupSource,
      );
      overlay
        .querySelector("#backupCreateBtn")
        ?.addEventListener("click", () => {
          state.error = "";
          showCreateBackupWorkflow();
        });
      overlay
        .querySelector("#rawArchiveCreateBtn")
        ?.addEventListener("click", () => {
          state.error = "";
          showRawArchiveCreateWorkflow();
        });
      overlay
        .querySelector("#rawArchiveCleanupLaunch")
        ?.addEventListener("click", () => {
          if (!hasFileSource(state.rawArchiveCleanupSource)) return;
          state.error = "";
          showRawArchiveCleanupWorkflow();
        });
      overlay
        .querySelector("#backupRestoreLaunch")
        ?.addEventListener("click", () => {
          if (!hasFileSource(state.restoreSource)) return;
          state.error = "";
          showRestoreBackupWorkflow();
        });
      overlay
        .querySelector("#rawArchiveRestoreLaunch")
        ?.addEventListener("click", () => {
          if (!hasFileSource(state.rawArchiveSource)) return;
          state.error = "";
          showRawArchiveRestoreWorkflow();
        });
    }

    document.body.appendChild(overlay);
    document.addEventListener("keydown", handleKeydown);
    render();
  }

  function modelPublicationStatusMeta(component = {}) {
    const status = String(component.status || "unavailable").toLowerCase();
    if (status === "current" || status === "ready") {
      return {
        cls: "processed",
        label: status === "current" ? "Current" : "Ready",
      };
    }
    if (status === "stale") return { cls: "stale", label: "Stale" };
    if (status === "missing") return { cls: "failed", label: "Missing" };
    if (status === "invalid") return { cls: "failed", label: "Invalid" };
    return { cls: "failed", label: "Unavailable" };
  }

  function modelPublicationErrorMessage(
    error,
    fallback = "Model publication failed",
  ) {
    const detail = error?.detail;
    if (detail && typeof detail === "object" && detail.message)
      return String(detail.message);
    const message = String(error?.message || "")
      .replace(/^API\s+\d+:\s*/, "")
      .trim();
    return message || fallback;
  }

  function modelPublicationMetadataPayload(form = {}) {
    return {
      library_name: String(form.libraryName || "").trim(),
      library_version: String(form.libraryVersion || "").trim(),
      publisher: String(form.publisher || "").trim(),
      description: String(form.description || "").trim(),
      release_notes: String(form.releaseNotes || "").trim(),
    };
  }

  function showModelPublicationDialog() {
    const overlay = document.createElement("div");
    overlay.className = "info-dialog-overlay model-publication-overlay";
    const state = {
      readiness: null,
      loading: true,
      working: "",
      error: "",
      refreshWarning: "",
      notice: "",
      result: null,
      resultAction: "",
      form: {
        libraryName: "",
        libraryVersion: "",
        publisher: "",
        description: "",
        releaseNotes: "",
      },
    };

    const isBusy = () => Boolean(state.working);
    const close = () => {
      if (isBusy()) return;
      overlay.remove();
      document.removeEventListener("keydown", handleKeydown);
    };
    const handleKeydown = (event) => {
      if (event.key === "Escape" && !isBusy()) close();
    };

    function readForm() {
      state.form.libraryName =
        overlay.querySelector("#modelPublicationName")?.value || "";
      state.form.libraryVersion =
        overlay.querySelector("#modelPublicationVersion")?.value || "";
      state.form.publisher =
        overlay.querySelector("#modelPublicationPublisher")?.value || "";
      state.form.description =
        overlay.querySelector("#modelPublicationDescription")?.value || "";
      state.form.releaseNotes =
        overlay.querySelector("#modelPublicationReleaseNotes")?.value || "";
    }

    function formComplete() {
      const payload = app.commands.modelPublicationMetadataPayload(state.form);
      return Boolean(
        payload.library_name && payload.library_version && payload.publisher,
      );
    }

    function readinessHtml() {
      if (state.loading && !state.readiness) {
        return `<div class="model-publication-loading" role="status">Checking current models...</div>`;
      }
      const report = state.readiness || {};
      const components = report.components || {};
      const orderedKeys = [
        "legacy_spline",
        "photo_stack_v2",
        "camera_transform",
        "filament_catalog",
      ];
      const rows = orderedKeys
        .map((key) => {
          const component = components[key] || {
            label:
              key === "filament_catalog"
                ? "Filament catalog"
                : key.replace(/_/g, " "),
            status: "unavailable",
            reason: "Readiness could not be determined.",
          };
          const meta = app.commands.modelPublicationStatusMeta(component);
          const count =
            key === "filament_catalog" && Number(component.filament_count || 0)
              ? `<span class="model-publication-component-meta">${Number(component.filament_count)} filaments</span>`
              : "";
          return `
          <div class="model-publication-component">
            <div class="model-publication-component-main">
              <strong>${app.commands.escapeHtml(component.label || key.replace(/_/g, " "))}</strong>
              ${component.reason ? `<span>${app.commands.escapeHtml(component.reason)}</span>` : ""}
            </div>
            <div class="model-publication-component-status">
              ${count}
              <span class="status-pill ${meta.cls}">${app.commands.escapeHtml(meta.label)}</span>
            </div>
          </div>
        `;
        })
        .join("");
      const blockers = report.blocking_reasons || [];
      return `
        <div class="model-publication-readiness-summary ${report.ready ? "is-ready" : "is-blocked"}">
          <strong>${report.ready ? "Ready to publish" : "Publication blocked"}</strong>
          <span>${
            report.ready
              ? "All required current models and public filament data passed validation."
              : "Resolve the items below, then refresh readiness."
          }</span>
        </div>
        <div class="model-publication-components">${rows}</div>
        ${
          blockers.length
            ? `
          <div class="backup-restore-message is-warning model-publication-blockers">
            ${blockers.map((reason) => `<div>${app.commands.escapeHtml(reason)}</div>`).join("")}
          </div>
        `
            : ""
        }
      `;
    }

    function resultHtml() {
      const result = state.result || null;
      if (!result) return "";
      const isExport = state.resultAction === "export";
      return `
        <div class="backup-restore-message is-success">
          ${
            isExport
              ? "Library package created."
              : "Library published to Generator. It is installed but not active; select it from Generator's Models menu when you are ready to use it."
          }
        </div>
        <div class="backup-restore-result model-publication-result">
          <div class="backup-restore-row"><span>Library</span><strong>${app.commands.escapeHtml(result.library_name || "")}</strong></div>
          <div class="backup-restore-row"><span>Version</span><strong>${app.commands.escapeHtml(result.library_version || "")}</strong></div>
          <div class="backup-restore-row"><span>Filaments</span><strong>${Number(result.filament_count || 0)}</strong></div>
          <div class="backup-restore-row"><span>Payload</span><strong>${app.commands.formatFileSize(Number(result.total_bytes || 0))}</strong></div>
          ${
            isExport
              ? `<div class="backup-restore-row"><span>Package</span><strong>${app.commands.escapeHtml(result.package_filename || "")}</strong></div>
               <div class="backup-restore-path mono" title="${app.commands.escapeHtml(result.package_path || "")}">${app.commands.escapeHtml(result.package_path || "")}</div>`
              : `<div class="backup-restore-row"><span>Generator status</span><strong>Installed · Not active</strong></div>
               <div class="backup-restore-row"><span>Library ID</span><strong class="mono">${app.commands.escapeHtml(result.library_id || "")}</strong></div>`
          }
        </div>
      `;
    }

    function progressHtml() {
      if (!state.working) return "";
      const label =
        state.working === "export"
          ? "Building and validating library package..."
          : "Building, validating, and installing library...";
      return `
        <div class="backup-progress model-publication-progress" role="status" aria-live="polite">
          <div class="backup-progress-topline"><strong>${label}</strong><span>Working</span></div>
          <div class="backup-progress-bar" aria-hidden="true">
            <div class="backup-progress-fill model-publication-progress-fill"></div>
          </div>
          <div class="backup-progress-meta"><span>Keep this window open until publication finishes.</span></div>
        </div>
      `;
    }

    function render() {
      const ready = Boolean(state.readiness?.ready);
      const canPublish = ready && formComplete() && !state.loading && !isBusy();
      overlay.innerHTML = `
        <div class="info-dialog model-publication-dialog" role="dialog" aria-modal="true" aria-labelledby="modelPublicationTitle">
          ${app.commands.renderDialogHeader({
            title: "Publish Models",
            titleId: "modelPublicationTitle",
            subtitle:
              "Create a static Generator library from Calibration's exact current models.",
            closeButtonHtml: app.commands.renderWindowCloseButton({
              id: "modelPublicationClose",
              className: "info-dialog-close",
              disabled: isBusy(),
            }),
          })}
          <div class="info-dialog-body model-publication-body">
            ${state.error ? `<div class="backup-restore-message is-error">${app.commands.escapeHtml(state.error)}</div>` : ""}
            ${state.refreshWarning ? `<div class="backup-restore-message is-warning">${app.commands.escapeHtml(state.refreshWarning)}</div>` : ""}
            ${state.notice ? `<div class="backup-restore-message is-success">${app.commands.escapeHtml(state.notice)}</div>` : ""}
            <div class="model-publication-layout">
              <section class="backup-restore-panel model-publication-panel">
                <div class="drawer-module-cap model-publication-panel-cap">
                  <span class="sidebar-label">Publication Readiness</span>
                  <button class="ghost-button small" type="button" id="modelPublicationRefresh" ${state.loading || isBusy() ? "disabled" : ""}>${state.loading ? "Checking..." : "Refresh"}</button>
                </div>
                <div class="backup-restore-panel-body">${readinessHtml()}</div>
              </section>
              <section class="backup-restore-panel model-publication-panel">
                <div class="drawer-module-cap">
                  <span class="sidebar-label">Library Details</span>
                </div>
                <div class="backup-restore-panel-body model-publication-form">
                  <div class="model-publication-required-grid">
                    <label class="model-publication-field">
                      <span>Library name <em>Required</em></span>
                      <input id="modelPublicationName" maxlength="120" value="${app.commands.escapeHtml(state.form.libraryName)}" placeholder="My Prisma Model Library" ${isBusy() ? "disabled" : ""}>
                    </label>
                    <label class="model-publication-field">
                      <span>Version <em>Required</em></span>
                      <input id="modelPublicationVersion" maxlength="64" value="${app.commands.escapeHtml(state.form.libraryVersion)}" placeholder="1.0" ${isBusy() ? "disabled" : ""}>
                    </label>
                  </div>
                  <label class="model-publication-field">
                    <span>Publisher or author <em>Required</em></span>
                    <input id="modelPublicationPublisher" maxlength="120" value="${app.commands.escapeHtml(state.form.publisher)}" placeholder="Your name" ${isBusy() ? "disabled" : ""}>
                  </label>
                  <label class="model-publication-field">
                    <span>Description <small>Optional</small></span>
                    <textarea id="modelPublicationDescription" maxlength="2000" rows="3" placeholder="What this model library contains" ${isBusy() ? "disabled" : ""}>${app.commands.escapeHtml(state.form.description)}</textarea>
                  </label>
                  <label class="model-publication-field">
                    <span>Release notes <small>Optional</small></span>
                    <textarea id="modelPublicationReleaseNotes" maxlength="8000" rows="4" placeholder="What changed in this version" ${isBusy() ? "disabled" : ""}>${app.commands.escapeHtml(state.form.releaseNotes)}</textarea>
                  </label>
                  <p class="small-copy model-publication-form-note">Prisma assigns a new library identity and compatibility metadata automatically. Published copies do not change when Calibration is edited or refitted later.</p>
                </div>
              </section>
            </div>
            <section class="backup-restore-panel model-publication-panel model-publication-actions-panel">
              <div class="drawer-module-cap"><span class="sidebar-label">Publish</span></div>
              <div class="backup-restore-panel-body">
                <p class="small-copy model-publication-action-help">Publish to Generator installs a new inactive library in this Prisma folder. Export Library Package creates a ZIP for transfer or a GitHub Release.</p>
                ${progressHtml()}
                ${resultHtml()}
                <div class="model-publication-actions">
                  <button class="ghost-button small" type="button" id="modelPublicationOpenFolder" ${isBusy() ? "disabled" : ""}>Open Published Models Folder</button>
                  <div class="model-publication-primary-actions">
                    <button class="secondary-button small" type="button" id="modelPublicationExport" ${canPublish ? "" : "disabled"}>${state.working === "export" ? "Exporting..." : "Export Library Package"}</button>
                    <button class="primary-button small" type="button" id="modelPublicationInstall" ${canPublish ? "" : "disabled"}>${state.working === "install" ? "Publishing..." : "Publish to Generator"}</button>
                  </div>
                </div>
              </div>
            </section>
          </div>
        </div>
      `;
      bind();
    }

    async function refreshReadiness({ initial = false } = {}) {
      readForm();
      state.loading = true;
      state.error = "";
      if (!initial) state.refreshWarning = "";
      render();
      try {
        state.readiness = await app.api.fetchModelPublicationReadiness();
      } catch (error) {
        state.readiness = null;
        state.error = app.commands.modelPublicationErrorMessage(
          error,
          "Could not check publication readiness",
        );
      } finally {
        state.loading = false;
        render();
      }
    }

    async function runPublication(action) {
      readForm();
      const payload = app.commands.modelPublicationMetadataPayload(state.form);
      if (
        !payload.library_name ||
        !payload.library_version ||
        !payload.publisher
      ) {
        state.error =
          "Enter a library name, version, and publisher before publishing.";
        render();
        return;
      }
      if (!state.readiness?.ready || isBusy()) return;
      state.working = action;
      state.error = "";
      state.refreshWarning = "";
      state.notice = "";
      state.result = null;
      state.resultAction = "";
      render();
      try {
        const response =
          action === "export"
            ? await app.api.exportCurrentModelLibrary(payload)
            : await app.api.installCurrentModelLibrary(payload);
        state.result = response?.result || null;
        state.resultAction = action;
        try {
          state.readiness = await app.api.fetchModelPublicationReadiness();
        } catch (refreshError) {
          state.refreshWarning = `Publication succeeded, but readiness could not be refreshed: ${app.commands.modelPublicationErrorMessage(refreshError)}`;
        }
      } catch (error) {
        if (error?.detail?.readiness) state.readiness = error.detail.readiness;
        state.error = app.commands.modelPublicationErrorMessage(error);
      } finally {
        state.working = "";
        render();
      }
    }

    async function openFolder() {
      if (isBusy()) return;
      readForm();
      state.error = "";
      state.notice = "";
      try {
        await app.api.openPublishedModelsFolder();
        state.notice = "Opened the Published Models folder.";
      } catch (error) {
        state.error = app.commands.modelPublicationErrorMessage(
          error,
          "Could not open the Published Models folder",
        );
      }
      render();
    }

    function bind() {
      overlay
        .querySelector("#modelPublicationClose")
        ?.addEventListener("click", close);
      overlay
        .querySelector("#modelPublicationRefresh")
        ?.addEventListener("click", () => refreshReadiness());
      overlay
        .querySelector("#modelPublicationExport")
        ?.addEventListener("click", () => runPublication("export"));
      overlay
        .querySelector("#modelPublicationInstall")
        ?.addEventListener("click", () => runPublication("install"));
      overlay
        .querySelector("#modelPublicationOpenFolder")
        ?.addEventListener("click", openFolder);
      overlay
        .querySelectorAll(
          ".model-publication-field input, .model-publication-field textarea",
        )
        .forEach((field) => {
          field.addEventListener("input", () => {
            readForm();
            const enabled = Boolean(
              state.readiness?.ready &&
                formComplete() &&
                !state.loading &&
                !isBusy(),
            );
            const exportBtn = overlay.querySelector("#modelPublicationExport");
            const installBtn = overlay.querySelector(
              "#modelPublicationInstall",
            );
            if (exportBtn) exportBtn.disabled = !enabled;
            if (installBtn) installBtn.disabled = !enabled;
          });
        });
    }

    overlay.addEventListener("click", (event) => {
      if (event.target === overlay && !isBusy()) close();
    });
    document.body.appendChild(overlay);
    document.addEventListener("keydown", handleKeydown);
    render();
    refreshReadiness({ initial: true });
  }

  Object.assign(app.commands, {
    formatBackupPackageType,
    normalizeRestoreConfirmation,
    showBackupRestoreDialog,
    modelPublicationStatusMeta,
    modelPublicationErrorMessage,
    modelPublicationMetadataPayload,
    showModelPublicationDialog,
  });
}
