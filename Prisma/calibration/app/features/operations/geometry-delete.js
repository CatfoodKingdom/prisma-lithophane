/** Install geometry-delete commands. */
export function installFeaturesOperationsGeometryDelete(app) {
  function showStepDeleteDialog(stepId = "") {
    if (app.commands.isStructuredGeometryBackend()) {
      const step = app.commands.stepRecordByRef(stepId);
      const label = step?.alias || stepId;
      const overlay = document.createElement("div");
      overlay.className = "info-dialog-overlay";
      overlay.innerHTML = `
        <div class="info-dialog info-dialog-wide" role="dialog" aria-modal="true" aria-labelledby="geometryDeleteTitle">
          ${app.commands.renderDialogHeader({
            title: "Delete Sample Geometry",
            titleId: "geometryDeleteTitle",
            closeButtonHtml: app.commands.renderWindowCloseButton({
              className: "info-dialog-close",
              attributes: "data-geometry-delete-close",
            }),
          })}
          <div class="info-dialog-body">
            <p class="info-dialog-lede">Delete <strong>${app.commands.escapeHtml(label)}</strong> from the Sample Geometry registry?</p>
            <p class="small-copy">This is allowed only when no samples or bundles reference it.</p>
            <div class="sb-validation-error" id="geometryDeleteError"></div>
          </div>
          <div class="info-dialog-footer">
            <button class="delete-button small" id="geometryDeleteConfirm">Delete</button>
            <button class="ghost-button small" id="infoDialogCancel">Cancel</button>
          </div>
        </div>
      `;
      document.body.appendChild(overlay);
      const cleanup = () => overlay.remove();
      overlay
        .querySelector("[data-geometry-delete-close]")
        ?.addEventListener("click", cleanup);
      overlay
        .querySelector("#infoDialogCancel")
        ?.addEventListener("click", cleanup);
      overlay.addEventListener("click", (e) => {
        if (e.target === overlay) cleanup();
      });
      overlay
        .querySelector("#geometryDeleteConfirm")
        ?.addEventListener("click", async () => {
          const errorEl = overlay.querySelector("#geometryDeleteError");
          try {
            await app.api.deleteGeometry(stepId);
            app.commands.showImportToast(`Deleted ${label}`, "success");
            cleanup();
            app.state.logbook.selectedRecord = { kind: null, id: null };
            app.commands.closeDrawer();
            await app.commands.handleRefresh();
          } catch (err) {
            const msg = err.message || "Delete failed";
            if (errorEl) {
              errorEl.style.display = "block";
              errorEl.textContent = msg;
            }
            app.commands.showImportToast(msg, "error");
          }
        });
      return;
    }

    const overlay = document.createElement("div");
    overlay.className = "info-dialog-overlay";

    // Highlight the Refresh Data button with a cutout
    const btn = document.getElementById("refreshDataBtn");
    let highlight = null;
    if (btn) {
      const rect = btn.getBoundingClientRect();
      highlight = document.createElement("div");
      highlight.className = "refresh-highlight";
      highlight.style.cssText = `
        position:fixed; top:${rect.top - 4}px; left:${rect.left - 4}px;
        width:${rect.width + 8}px; height:${rect.height + 8}px;
        border-radius:6px; z-index:1001; pointer-events:none;
        box-shadow: 0 0 0 3px #2563eb, 0 0 12px rgba(37,99,235,0.4);
      `;
      document.body.appendChild(highlight);
    }

    overlay.innerHTML = `
      <div class="info-dialog info-dialog-wide" role="dialog" aria-modal="true" aria-labelledby="geometryDeleteFileTitle">
        ${app.commands.renderDialogHeader({
          title: "Delete Sample Geometry File",
          titleId: "geometryDeleteFileTitle",
          closeButtonHtml: app.commands.renderWindowCloseButton({
            className: "info-dialog-close",
            attributes: "data-geometry-delete-file-close",
          }),
        })}
        <div class="info-dialog-body">
          <p class="info-dialog-lede">To remove a sample geometry file from the Sample Geometry library:</p>
          <ol class="info-dialog-list">
          <li>Manually delete the unwanted file from the file system</li>
          <li>Click the <strong>Refresh Data</strong> button to update the Sample Geometry library</li>
          </ol>
        </div>
        <div class="info-dialog-footer">
          <button class="primary-button small" id="infoDialogOk">OK</button>
        </div>
      </div>
    `;
    document.body.appendChild(overlay);

    const cleanup = () => {
      overlay.remove();
      if (highlight) highlight.remove();
    };
    overlay.querySelector("#infoDialogOk").addEventListener("click", cleanup);
    overlay
      .querySelector("[data-geometry-delete-file-close]")
      .addEventListener("click", cleanup);
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) cleanup();
    });
  }

  Object.assign(app.commands, {
    showStepDeleteDialog,
  });
}
