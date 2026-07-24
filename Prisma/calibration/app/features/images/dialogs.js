/** Install features/images/dialogs commands. */
export function installFeaturesImagesDialogs(app) {
  function formatExifDate(isoStr) {
    if (!isoStr) return "";
    try {
      const d = new Date(isoStr);
      const month = String(d.getMonth() + 1).padStart(2, "0");
      const day = String(d.getDate()).padStart(2, "0");
      const h = String(d.getHours()).padStart(2, "0");
      const m = String(d.getMinutes()).padStart(2, "0");
      return `${month}/${day} ${h}:${m}`;
    } catch {
      return isoStr.slice(0, 16);
    }
  }

  function formatFileSize(bytes) {
    if (bytes == null || isNaN(bytes)) return "—";
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
    return (bytes / (1024 * 1024)).toFixed(1) + " MB";
  }

  function buildImageAssignmentMap() {
    const map = {};
    for (const exp of app.state.session.data.samples) {
      const img = exp._assigned_image;
      if (img) {
        map[img] = exp.sample_id;
      }
    }
    app.state.images.importState.imageAssignments = map;
  }

  function showInfoDialog(message, title = "Warning") {
    const overlay = document.createElement("div");
    overlay.className = "info-dialog-overlay";
    overlay.innerHTML = `
      <div class="info-dialog" role="dialog" aria-modal="true" aria-labelledby="infoDialogTitle">
        ${app.commands.renderDialogHeader({
          title,
          titleId: "infoDialogTitle",
          closeButtonHtml: app.commands.renderWindowCloseButton({
            className: "info-dialog-close",
            attributes: "data-info-dialog-close",
          }),
        })}
        <div class="info-dialog-body">
          <p>${message}</p>
        </div>
        <div class="info-dialog-footer">
          <button class="primary-button small" id="infoDialogOk">OK</button>
        </div>
      </div>
    `;
    document.body.appendChild(overlay);
    const cleanup = () => overlay.remove();
    overlay.querySelector("#infoDialogOk").addEventListener("click", cleanup);
    overlay
      .querySelector("[data-info-dialog-close]")
      .addEventListener("click", cleanup);
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) cleanup();
    });
  }

  function showImportConfirmDialog() {
    return new Promise((resolve) => {
      const overlay = document.createElement("div");
      overlay.className = "info-dialog-overlay";
      overlay.innerHTML = `
        <div class="info-dialog" role="dialog" aria-modal="true" aria-labelledby="importConfirmTitle">
          ${app.commands.renderDialogHeader({
            title: "Import Images",
            titleId: "importConfirmTitle",
            closeButtonHtml: app.commands.renderWindowCloseButton({
              id: "importConfirmClose",
              className: "info-dialog-close",
            }),
          })}
          <div class="info-dialog-body">
            <p class="info-dialog-lede">By importing, images in the inbox will be moved into Prisma-managed storage. Are you sure you wish to continue?</p>
          </div>
          <div class="info-dialog-footer">
            <button class="primary-button small" id="importConfirmProceed">Import</button>
            <button class="ghost-button small" id="importConfirmCancel">Cancel</button>
          </div>
        </div>
      `;
      document.body.appendChild(overlay);
      const cleanup = (value) => {
        overlay.remove();
        document.removeEventListener("keydown", handleKeydown);
        resolve(value);
      };
      const handleKeydown = (event) => {
        if (event.key === "Escape") cleanup(false);
      };
      document.addEventListener("keydown", handleKeydown);
      overlay
        .querySelector("#importConfirmProceed")
        ?.addEventListener("click", () => cleanup(true));
      overlay
        .querySelector("#importConfirmCancel")
        ?.addEventListener("click", () => cleanup(false));
      overlay
        .querySelector("#importConfirmClose")
        ?.addEventListener("click", () => cleanup(false));
      overlay.addEventListener("click", (event) => {
        if (event.target === overlay) cleanup(false);
      });
    });
  }

  function findImageRecord(filename) {
    if (!filename || filename === "—") return null;
    return (
      (app.state.session.data.images || []).find(
        (img) =>
          img.filename === filename || img.original_filename === filename,
      ) ||
      (app.state.images.importState.images || []).find(
        (img) =>
          img.filename === filename || img.original_filename === filename,
      ) ||
      null
    );
  }

  function imageCustodyBadgeHtml(filename, noun = "Image") {
    const availability = app.commands.sourceAvailabilityInfo(
      app.commands.findImageRecord(filename),
      noun,
    );
    if (availability.available) return "";
    return `<span class="status-pill source-custody-pill ${app.commands._escAttr(app.commands.importSourceStateClass(availability.state))}" title="${app.commands._escAttr(availability.message)}">${app.commands._escHtml(availability.label)}</span>`;
  }

  function showCsvAssignmentBlankRegistrationDialog(pendingBlanks = []) {
    return new Promise((resolve) => {
      const overlay = document.createElement("div");
      overlay.className = "info-dialog-overlay csv-assignment-confirm-overlay";
      overlay.innerHTML = `
        <div class="info-dialog" role="dialog" aria-modal="true" aria-labelledby="csvBlankRegistrationTitle">
          ${app.commands.renderDialogHeader({
            title: "Register Blank Images",
            titleId: "csvBlankRegistrationTitle",
            closeButtonHtml: app.commands.renderWindowCloseButton({
              id: "csvBlankRegistrationClose",
              className: "info-dialog-close",
            }),
          })}
          <div class="info-dialog-body">
            <p class="info-dialog-lede">The following images were used as a blank but are not registered:</p>
            <div class="csv-blank-registration-list">
              ${(pendingBlanks || [])
                .map(
                  (blank) => `
                <div class="csv-blank-registration-row">
                  <span title="${app.commands.escapeHtml(blank.filename || "")}">${app.commands.escapeHtml(blank.filename || blank.image_asset_id || "")}</span>
                  <strong>${Number(blank.uses || 0)} use${Number(blank.uses || 0) === 1 ? "" : "s"}</strong>
                </div>
              `,
                )
                .join("")}
            </div>
            <p>Would you like to register these images as blanks and continue?</p>
          </div>
          <div class="info-dialog-footer">
            <button class="primary-button small" type="button" id="csvBlankRegistrationProceed">Register and Continue</button>
            <button class="ghost-button small" type="button" id="csvBlankRegistrationCancel">Cancel</button>
          </div>
        </div>
      `;
      document.body.appendChild(overlay);
      const cleanup = (value) => {
        overlay.remove();
        document.removeEventListener("keydown", handleKeydown);
        resolve(value);
      };
      const handleKeydown = (event) => {
        if (event.key === "Escape") cleanup(false);
      };
      document.addEventListener("keydown", handleKeydown);
      overlay
        .querySelector("#csvBlankRegistrationProceed")
        ?.addEventListener("click", () => cleanup(true));
      overlay
        .querySelector("#csvBlankRegistrationCancel")
        ?.addEventListener("click", () => cleanup(false));
      overlay
        .querySelector("#csvBlankRegistrationClose")
        ?.addEventListener("click", () => cleanup(false));
      overlay.addEventListener("click", (event) => {
        if (event.target === overlay) cleanup(false);
      });
    });
  }

  function showCsvAssignmentImportDialog() {
    const overlay = document.createElement("div");
    overlay.className = "info-dialog-overlay csv-assignment-overlay";
    const state = {
      file: null,
      preview: null,
      validating: false,
      committing: false,
      error: "",
    };

    const close = () => {
      overlay.remove();
      document.removeEventListener("keydown", handleKeydown);
    };
    const handleKeydown = (event) => {
      if (event.key === "Escape" && !state.validating && !state.committing)
        close();
    };

    function previewRows() {
      const preview = state.preview || {};
      return [
        ...(preview.valid_rows || []),
        ...(preview.error_rows || []),
      ].sort((a, b) => Number(a.row_number || 0) - Number(b.row_number || 0));
    }

    function rowHtml(row) {
      const status = row.valid
        ? `<span class="csv-assignment-status is-valid">${row.blank_registration_required ? "Ready + blank" : "Ready"}</span>`
        : `<span class="csv-assignment-status is-error">Blocked</span>`;
      const errors = (row.errors || []).length
        ? `<div class="csv-assignment-errors">${(row.errors || []).map((err) => `<div>${app.commands.escapeHtml(err)}</div>`).join("")}</div>`
        : "";
      return `
        <tr class="${row.valid ? "is-valid" : "is-error"}">
          <td class="mono">${app.commands.escapeHtml(String(row.row_number || ""))}</td>
          <td class="mono">${app.commands.escapeHtml(row.sample_id || "")}</td>
          <td>${app.commands.escapeHtml(row.sample_image || "")}</td>
          <td>${app.commands.escapeHtml(row.blank_id || row.blank_image || "")}</td>
          <td class="mono">${app.commands.escapeHtml(row.orientation || "")}</td>
          <td>${status}${errors}</td>
        </tr>
      `;
    }

    function render() {
      const preview = state.preview;
      const hasValid = Number(preview?.valid_count || 0) > 0;
      const hasErrors = Number(preview?.error_count || 0) > 0;
      const commitLabel = hasErrors
        ? "Import Valid Rows"
        : "Import Assignments";
      const selectedName = state.file?.name || "No CSV selected";
      const rows = previewRows();
      const warnings = preview?.warnings || [];
      const pendingBlanks = preview?.pending_blank_registrations || [];
      overlay.innerHTML = `
        <div class="info-dialog csv-assignment-dialog" role="dialog" aria-modal="true" aria-labelledby="csvAssignmentTitle">
          ${app.commands.renderDialogHeader({
            title: "CSV Bulk Assignment",
            titleId: "csvAssignmentTitle",
            closeButtonHtml: app.commands.renderWindowCloseButton({
              id: "csvAssignmentClose",
              className: "info-dialog-close",
              disabled: state.validating || state.committing,
            }),
          })}
          <div class="info-dialog-body csv-assignment-body">
            <div class="csv-assignment-instructions">
              <strong>Before you begin</strong>
              <ul>
                <li>Place every sample image and blank image referenced by the CSV in the Calibration Inbox folder.</li>
                <li>Click <strong>Import from Inbox</strong> before validating this CSV so the images are registered in Calibration.</li>
                <li>Use the exact filenames shown in the Inbox, and keep the required columns from the template: Sample ID, Sample Image, Blank Image, and Orientation.</li>
                <li>Validation does not change assignments. Review blocked rows first; only ready rows are committed.</li>
              </ul>
            </div>
            <div class="csv-assignment-controls">
              <a class="ghost-button small" href="${app.api.sampleAssignmentTemplateUrl()}" download="prisma_sample_assignment_template.csv">Download CSV Template</a>
              <label class="ghost-button small csv-file-picker">
                <input type="file" id="csvAssignmentFile" accept=".csv,text/csv">
                Choose CSV
              </label>
              <span class="csv-assignment-file" title="${app.commands.escapeHtml(selectedName)}">${app.commands.escapeHtml(selectedName)}</span>
              <button class="primary-button small" type="button" id="csvAssignmentValidate" ${!state.file || state.validating || state.committing ? "disabled" : ""}>
                ${state.validating ? "Validating..." : "Validate CSV"}
              </button>
            </div>
            ${state.error ? `<div class="csv-assignment-message is-error">${app.commands.escapeHtml(state.error)}</div>` : ""}
            ${
              preview
                ? `
              <div class="csv-assignment-summary">
                <span><strong>${preview.total_rows || 0}</strong> rows</span>
                <span class="is-valid"><strong>${preview.valid_count || 0}</strong> ready</span>
                <span class="${hasErrors ? "is-error" : ""}"><strong>${preview.error_count || 0}</strong> blocked</span>
                ${pendingBlanks.length ? `<span><strong>${pendingBlanks.length}</strong> blank${pendingBlanks.length === 1 ? "" : "s"} to register</span>` : ""}
                ${warnings.length ? `<span><strong>${warnings.length}</strong> warning${warnings.length === 1 ? "" : "s"}</span>` : ""}
              </div>
              ${hasErrors && hasValid ? `<div class="csv-assignment-message is-warning">Blocked rows will be skipped. Only ready rows will be assigned.</div>` : ""}
              ${pendingBlanks.length ? `<div class="csv-assignment-message is-warning">Some ready rows use images that are not yet registered as blanks. You will be asked to register them before import.</div>` : ""}
              ${warnings.length ? `<div class="csv-assignment-warnings">${warnings.map((warning) => `<div>${app.commands.escapeHtml(warning.message || String(warning))}</div>`).join("")}</div>` : ""}
              <div class="csv-assignment-table-wrap">
                <table class="csv-assignment-table">
                  <thead>
                    <tr>
                      <th>Row</th>
                      <th>Sample</th>
                      <th>Sample Image</th>
                      <th>Blank</th>
                      <th>Ori</th>
                      <th>Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    ${rows.map(rowHtml).join("")}
                  </tbody>
                </table>
              </div>
            `
                : `<p class="small-copy">Choose a CSV after all referenced images have been imported from the Calibration Inbox.</p>`
            }
          </div>
          <div class="info-dialog-footer csv-assignment-footer">
            <button class="ghost-button small" type="button" id="csvAssignmentCancel" ${state.validating || state.committing ? "disabled" : ""}>Cancel</button>
            <button class="primary-button small" type="button" id="csvAssignmentCommit" ${!hasValid || state.validating || state.committing ? "disabled" : ""}>
              ${state.committing ? "Importing..." : commitLabel}
            </button>
          </div>
        </div>
      `;
      bind();
    }

    function bind() {
      overlay
        .querySelector("#csvAssignmentClose")
        ?.addEventListener("click", close);
      overlay
        .querySelector("#csvAssignmentCancel")
        ?.addEventListener("click", close);
      overlay
        .querySelector("#csvAssignmentFile")
        ?.addEventListener("change", (event) => {
          state.file = event.target.files?.[0] || null;
          state.preview = null;
          state.error = "";
          render();
        });
      overlay
        .querySelector("#csvAssignmentValidate")
        ?.addEventListener("click", async () => {
          if (!state.file) return;
          state.validating = true;
          state.error = "";
          render();
          try {
            state.preview = await app.api.validateSampleAssignmentCsv(
              state.file,
            );
          } catch (err) {
            state.preview = null;
            state.error = err.message || "CSV validation failed";
          } finally {
            state.validating = false;
            render();
          }
        });
      overlay
        .querySelector("#csvAssignmentCommit")
        ?.addEventListener("click", async () => {
          const token = state.preview?.preview_token;
          if (!token) return;
          const pendingBlanks =
            state.preview?.pending_blank_registrations || [];
          let registerUnregisteredBlanks = false;
          if (pendingBlanks.length) {
            const confirmed =
              await app.commands.showCsvAssignmentBlankRegistrationDialog(
                pendingBlanks,
              );
            if (!confirmed) return;
            registerUnregisteredBlanks = true;
          }
          state.committing = true;
          state.error = "";
          render();
          try {
            const result = await app.api.commitSampleAssignmentCsv(token, {
              registerUnregisteredBlanks,
            });
            const blankCount = Number(result.registered_blank_count || 0);
            const blankMsg = blankCount
              ? ` and registered ${blankCount} blank${blankCount === 1 ? "" : "s"}`
              : "";
            app.commands.showImportToast(
              `Imported ${result.committed_count || 0} assignment${(result.committed_count || 0) === 1 ? "" : "s"}${blankMsg}`,
              "success",
            );
            close();
            await app.commands.handleRefresh();
          } catch (err) {
            state.error = err.message || "CSV assignment import failed";
            state.committing = false;
            render();
          }
        });
    }

    document.body.appendChild(overlay);
    document.addEventListener("keydown", handleKeydown);
    overlay.addEventListener("click", (event) => {
      if (event.target === overlay && !state.validating && !state.committing)
        close();
    });
    render();
  }

  Object.assign(app.commands, {
    formatExifDate,
    formatFileSize,
    buildImageAssignmentMap,
    showInfoDialog,
    showImportConfirmDialog,
    findImageRecord,
    imageCustodyBadgeHtml,
    showCsvAssignmentBlankRegistrationDialog,
    showCsvAssignmentImportDialog,
  });
}
