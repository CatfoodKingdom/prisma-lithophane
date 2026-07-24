/** Install features/operations/maintenance commands. */
export function installFeaturesOperationsMaintenance(app) {
  function maintenanceModeLabel(mode = "", operation = null) {
    const key = String(mode || "");
    if (key === "audit") return "Audit";
    if (key === "missing_only") return "Missing Only";
    if (key === "force") {
      if (operation?.operation_id === "export_geometry_files")
        return "Force Export";
      return "Force Rebuild";
    }
    if (key === "cleanup") return "Quarantine";
    if (key === "stale_only") return "Stale Only";
    if (key === "fit") return "Fit";
    if (key === "reextract") return "Re-extract";
    return key.replace(/_/g, " ") || "Default";
  }

  function maintenanceRiskLabel(riskClass = "") {
    const key = String(riskClass || "");
    if (key === "read_only") return "Read-only";
    if (key === "writes_derived_files") return "Writes Derived Files";
    if (key === "writes_user_output") return "Writes User Output";
    if (key === "changes_semantic_data") return "Modifies Data";
    if (key === "cleanup") return "Cleanup";
    return key.replace(/_/g, " ") || "Maintenance";
  }

  function maintenanceWriteLabel(operation = {}) {
    if (!operation?.writes) return "Read-only";
    if (operation.risk_class === "cleanup") return "Moves or removes files";
    if (operation.risk_class === "changes_semantic_data")
      return "Modifies data";
    if (operation.risk_class === "writes_user_output")
      return "Writes user-facing files";
    return "Writes derived files";
  }

  function maintenanceOperationGroup(operation = {}) {
    const id = operation.operation_id || "";
    if (operation.enabled === false) return "Unavailable Operations";
    const category = String(operation.category || "").trim();
    if (category === "Visual Caches") return "Images";
    if (
      category === "Database Maintenance" ||
      category === "Database Housekeeping"
    )
      return "System Maintenance";
    if (category === "Cleanup") return "System Maintenance";
    if (category === "Repair / Rebuild" && id === "reextract_sample_images")
      return "Images";
    if (category) return category;
    if (id === "refit_calibration_models") return "Calibration Models";
    if (operation.risk_class === "cleanup" || id.startsWith("quarantine_"))
      return "System Maintenance";
    if (operation.risk_class === "writes_user_output")
      return "Geometry Exports";
    if (id.startsWith("rebuild_") || id.startsWith("regenerate_"))
      return "Repair / Rebuild";
    return "Health Checks";
  }

  function maintenanceOperationBrief(operation = {}) {
    if (operation.enabled === false) return "Unavailable";
    return app.commands.maintenanceWriteLabel(operation);
  }

  function maintenanceResourceSentence(operation = {}) {
    const claims = operation.resource_claims || [];
    if (!claims.length) return "";
    return claims.map((claim) => claim.replace(/_/g, " ")).join(", ");
  }

  function maintenanceModeHelp(operation = {}) {
    const modes = operation.modes || [];
    if (modes.includes("missing_only") && modes.includes("force")) {
      if (operation.operation_id === "export_geometry_files") {
        return "Missing Only exports public STEP/STL files that are absent. Force Export can overwrite existing public exports after confirmation.";
      }
      return "Missing Only runs a preflight first and then acts only on missing or repairable targets. Force Rebuild rewrites every repairable target in scope.";
    }
    return "";
  }

  function maintenanceRunButtonLabel(operation = {}) {
    if (operation.operation_id === "export_geometry_files")
      return "Export Files";
    if (operation.operation_id === "refit_calibration_models")
      return "Fit Models";
    return operation.writes ? "Run Maintenance" : "Run Audit";
  }

  async function loadMaintenanceOperationsForAction() {
    if (Array.isArray(app.state.operations.maintenanceState.operations))
      return app.state.operations.maintenanceState.operations;
    if (app.state.operations.maintenanceState.loadPromise)
      return app.state.operations.maintenanceState.loadPromise;
    app.state.operations.maintenanceState.loading = true;
    app.state.operations.maintenanceState.error = "";
    app.state.operations.maintenanceState.loadPromise = app.api
      .fetchMaintenanceOperations()
      .then((payload) => {
        const operations = payload?.operations || [];
        app.state.operations.maintenanceState.operations = operations;
        return operations;
      })
      .catch((err) => {
        app.state.operations.maintenanceState.error =
          err.message || "Failed to load maintenance operations";
        throw err;
      })
      .finally(() => {
        app.state.operations.maintenanceState.loading = false;
        app.state.operations.maintenanceState.loadPromise = null;
      });
    return app.state.operations.maintenanceState.loadPromise;
  }

  async function maintenanceOperationById(operationId) {
    const operations = await app.commands.loadMaintenanceOperationsForAction();
    return (
      operations.find((operation) => operation.operation_id === operationId) ||
      null
    );
  }

  function maintenanceSummaryHtml(summary = {}) {
    const entries = Object.entries(summary || {}).filter(
      ([, value]) => value !== null && value !== undefined && value !== "",
    );
    if (!entries.length) return "";
    const formatSummaryValue = (value) => {
      if (Array.isArray(value)) {
        return value
          .map((item) =>
            typeof item === "object" ? JSON.stringify(item) : String(item),
          )
          .join(", ");
      }
      if (value && typeof value === "object") {
        return Object.entries(value)
          .map(
            ([childKey, childValue]) =>
              `${childKey.replace(/_/g, " ")}: ${childValue}`,
          )
          .join(", ");
      }
      return String(value);
    };
    return `
      <div class="maintenance-summary-grid">
        ${entries
          .map(
            ([key, value]) => `
          <div class="maintenance-summary-row">
            <span>${app.commands.escapeHtml(key.replace(/_/g, " "))}</span>
            <strong>${app.commands.escapeHtml(formatSummaryValue(value))}</strong>
          </div>
        `,
          )
          .join("")}
      </div>
    `;
  }

  function maintenanceModelResultsHtml(result = {}) {
    const modelResults = result?.model_results || {};
    const keys = ["legacy_spline", "photo_stack_v2", "camera_transform"].filter(
      (key) => modelResults[key],
    );
    if (!keys.length) return "";
    const labels = {
      legacy_spline: "Color Model v1",
      photo_stack_v2: "Color Model v2",
      camera_transform: "Camera Transform",
    };
    const displayStatus = (item = {}) => {
      const raw = String(item.status || "").toLowerCase();
      if (raw === "skipped") return "Skipped";
      if (raw === "failed" || item.error) return "Failed";
      return "Completed";
    };
    const detailText = (key, item = {}) => {
      if (key === "legacy_spline") {
        return `${Number(item.fitted || 0)} fitted · ${Number(item.failed || 0)} failed · ${Number(item.skipped || 0)} skipped`;
      }
      if (key === "photo_stack_v2") {
        const summary = item.summary || {};
        const bits = [];
        if (item.run_id) bits.push(`run ${item.run_id}`);
        if (summary.swatch_count !== undefined)
          bits.push(`${summary.swatch_count} swatches`);
        if (summary.sample_count !== undefined)
          bits.push(`${summary.sample_count} samples`);
        return bits.join(" · ") || "candidate written";
      }
      if (key === "camera_transform") {
        const summary = item.summary || {};
        const bits = [];
        if (item.status === "skipped") bits.push(item.reason || "up to date");
        if (summary.params_sha256)
          bits.push(`params ${String(summary.params_sha256).slice(0, 12)}`);
        if (summary.usable_swatch_count !== undefined)
          bits.push(`${summary.usable_swatch_count} swatches`);
        return bits.join(" · ") || item.status || "complete";
      }
      return item.status || "";
    };
    return `
      <div class="maintenance-model-result-list">
        ${keys
          .map((key) => {
            const item = modelResults[key] || {};
            return `
            <div class="maintenance-model-result-row">
              <span>
                <strong>${app.commands.escapeHtml(labels[key] || key.replace(/_/g, " "))}</strong>
                <small>${app.commands.escapeHtml(detailText(key, item))}</small>
              </span>
              <span class="maintenance-model-result-status">${app.commands.escapeHtml(displayStatus(item))}</span>
            </div>
          `;
          })
          .join("")}
      </div>
    `;
  }

  function maintenanceWarningsHtml(warnings = []) {
    const source = Array.isArray(warnings) ? warnings : [warnings];
    const items = source.filter(
      (warning) => warning !== null && warning !== undefined && warning !== "",
    );
    if (!items.length) return "";
    return `
      <div class="backup-restore-warnings maintenance-workflow-warnings">
        ${items.map((warning) => `<div>${app.commands.escapeHtml(warning.message || String(warning))}</div>`).join("")}
      </div>
    `;
  }

  function maintenanceFindingsHtml(result = {}) {
    const findings = result.findings || [];
    const rawBlocked = result.blocked || [];
    const manualRequired = [
      ...(result.manual_required || []),
      ...rawBlocked.filter((item) => item?.category === "manual_required"),
    ];
    const blocked = rawBlocked.filter(
      (item) => item?.category !== "manual_required",
    );
    const errors = result.errors || [];
    const findingTargetLabel = (item = {}) => {
      if (item.target) return String(item.target);
      if (item.sample_id && item.kind) return `${item.sample_id}/${item.kind}`;
      if (item.sample_id) return String(item.sample_id);
      if (item.filename) return String(item.filename);
      if (item.blank_id) return String(item.blank_id);
      if (item.geometry_id) return String(item.geometry_id);
      if (item.path) return String(item.path);
      return "";
    };
    const findingMessage = (item = {}) => {
      const direct = item.message || item.reason;
      if (direct && typeof direct === "object") {
        return JSON.stringify(direct);
      }
      if (direct) return String(direct);
      if (item.original_path && item.quarantine_path) {
        return `Moved to ${app.commands.displayPathFromPrismaRoot(item.quarantine_path)}`;
      }
      if (item.category) return String(item.category).replace(/_/g, " ");
      return String(item);
    };
    const findingItemHtml = (item = {}) => {
      const targetLabel = findingTargetLabel(item);
      return `
        <div class="maintenance-finding">
          ${item.severity ? `<span class="maintenance-finding-severity">${app.commands.escapeHtml(item.severity)}</span>` : ""}
          ${targetLabel ? `<span class="mono">${app.commands.escapeHtml(targetLabel)}</span>` : ""}
          <span>${app.commands.escapeHtml(findingMessage(item))}</span>
        </div>
      `;
    };
    const groups = [
      {
        title: "Errors",
        items: errors.map((message) => ({ message })),
        kind: "error",
      },
      { title: "Needs Manual Corners", items: manualRequired, kind: "warning" },
      { title: "Blocked", items: blocked, kind: "warning" },
      { title: "Findings", items: findings.slice(0, 80), kind: "" },
    ].filter((group) => group.items.length);
    if (!groups.length) return "";
    return `
      <div class="maintenance-report-sections">
        ${groups
          .map(
            (group) => `
          <div class="maintenance-report-section ${group.kind ? `is-${group.kind}` : ""}">
            <strong>${app.commands.escapeHtml(group.title)}</strong>
            <div class="maintenance-finding-list">
              ${group.items.map(findingItemHtml).join("")}
            </div>
          </div>
        `,
          )
          .join("")}
      </div>
    `;
  }

  function maintenanceProgressHtml(job, active, fallbackMessage) {
    if (!job || !active) return "";
    const progress = job.progress || {};
    const percent = Number(progress.percent || 0);
    const message =
      job.message ||
      progress.message ||
      fallbackMessage ||
      "Running maintenance";
    const current = Number(progress.current || 0);
    const total = Number(progress.total || 0);
    const target = progress.target || "";
    const summary = progress.summary || {};
    const stageProgress = summary.stage_progress || {};
    const stageCurrent = Number(stageProgress.current || 0);
    const stageTotal = Number(stageProgress.total || 0);
    const stageLabel = summary.stage
      ? String(summary.stage).replace(/_/g, " ")
      : "";
    const countText = stageTotal
      ? `${Math.min(stageCurrent, stageTotal)} / ${stageTotal}`
      : total
        ? `${Math.min(current, total)} / ${total}`
        : "";
    return `
      <div class="backup-progress maintenance-progress" role="status" aria-live="polite">
        <div class="backup-progress-topline">
          <strong>${app.commands.escapeHtml(message)}</strong>
          <span>${Math.max(0, Math.min(100, percent)).toFixed(0)}%</span>
        </div>
        <div class="backup-progress-bar" aria-hidden="true">
          <div class="backup-progress-fill" style="width: ${Math.max(0, Math.min(100, percent)).toFixed(0)}%;"></div>
        </div>
        <div class="backup-progress-meta">
          ${stageLabel ? `<span>${app.commands.escapeHtml(stageLabel)}</span>` : ""}
          ${countText ? `<span>${app.commands.escapeHtml(countText)}</span>` : ""}
          ${target ? `<span class="mono">${app.commands.escapeHtml(target)}</span>` : ""}
        </div>
      </div>
    `;
  }

  function reextractProgressHtml(
    job,
    active,
    fallbackMessage = "Running re-extraction",
  ) {
    if (!job || !active) return "";
    const progress = job.progress || {};
    const percent = Number(progress.percent || 0);
    const message =
      job.message ||
      progress.message ||
      progress.action_label ||
      fallbackMessage;
    const action = progress.action_label || progress.action || "";
    const sampleId =
      progress.sample_id || progress.target || job.sample_id || "";
    const current = Number(progress.current || 0);
    const total = Number(progress.total || 0);
    const sampleIndex = Number(progress.sample_index || 0);
    const sampleTotal = Number(progress.sample_total || total || 0);
    const actionIndex = Number(progress.action_index || 0);
    const actionTotal = Number(progress.action_total || 0);
    const elapsed = Number(progress.elapsed_seconds || 0);
    const clampedPercent = Math.max(0, Math.min(100, percent));
    const etaSeconds =
      elapsed > 3 && clampedPercent > 2 && clampedPercent < 100
        ? Math.max(0, elapsed / (clampedPercent / 100) - elapsed)
        : 0;
    const etaText = etaSeconds
      ? etaSeconds >= 60
        ? `${Math.ceil(etaSeconds / 60)}m remaining`
        : `${Math.ceil(etaSeconds)}s remaining`
      : "";
    const countText = sampleTotal
      ? `${Math.min(Math.max(1, sampleIndex || Math.ceil(current)), sampleTotal)} / ${sampleTotal} samples`
      : total
        ? `${Math.min(current, total)} / ${total}`
        : "";
    return `
      <div class="backup-progress maintenance-progress reextract-progress" role="status" aria-live="polite">
        <div class="backup-progress-topline">
          <strong>${app.commands.escapeHtml(message)}</strong>
          <span>${clampedPercent.toFixed(1)}%</span>
        </div>
        <div class="backup-progress-bar" aria-hidden="true">
          <div class="backup-progress-fill" style="width: ${clampedPercent.toFixed(1)}%;"></div>
        </div>
        <div class="backup-progress-meta">
          ${countText ? `<span>${app.commands.escapeHtml(countText)}</span>` : ""}
          ${sampleId ? `<span class="mono">${app.commands.escapeHtml(sampleId)}</span>` : ""}
          ${action ? `<span>${app.commands.escapeHtml(action)}</span>` : ""}
          ${actionTotal ? `<span>${Math.min(actionIndex, actionTotal)} / ${actionTotal} actions</span>` : ""}
          ${elapsed ? `<span>${elapsed.toFixed(1)}s elapsed</span>` : ""}
          ${etaText ? `<span>${app.commands.escapeHtml(etaText)}</span>` : ""}
        </div>
      </div>
    `;
  }

  function showMaintenanceDialog() {
    const overlay = document.createElement("div");
    overlay.className = "info-dialog-overlay maintenance-overlay";
    const state = {
      operations: app.state.operations.maintenanceState.operations || [],
      reports: [],
      loading: !app.state.operations.maintenanceState.operations,
      error: "",
      selectedOperationId: null,
    };
    let cardHeightFrame = 0;

    const close = () => {
      if (cardHeightFrame) cancelAnimationFrame(cardHeightFrame);
      overlay.remove();
      document.removeEventListener("keydown", handleKeydown);
      window.removeEventListener("resize", scheduleCardHeightSync);
    };
    const handleKeydown = (event) => {
      if (
        document.querySelector(
          ".maintenance-workflow-overlay, .maintenance-activity-overlay",
        )
      )
        return;
      if (event.key === "Escape") close();
    };

    function syncCardHeights() {
      cardHeightFrame = 0;
      const dialog = overlay.querySelector(".maintenance-dialog");
      const cards = [
        ...overlay.querySelectorAll(".maintenance-operation-card"),
      ];
      if (!dialog || !cards.length) return;
      dialog.style.removeProperty("--maintenance-operation-card-height");
      const maxHeight = Math.ceil(
        Math.max(...cards.map((card) => card.getBoundingClientRect().height)),
      );
      if (Number.isFinite(maxHeight) && maxHeight > 0) {
        dialog.style.setProperty(
          "--maintenance-operation-card-height",
          `${maxHeight}px`,
        );
      }
    }

    function scheduleCardHeightSync() {
      if (cardHeightFrame) cancelAnimationFrame(cardHeightFrame);
      cardHeightFrame = requestAnimationFrame(syncCardHeights);
    }

    function selectedOperation() {
      return (
        state.operations.find(
          (operation) => operation.operation_id === state.selectedOperationId,
        ) ||
        state.operations[0] ||
        null
      );
    }

    function groupedOperations() {
      const groups = new Map();
      state.operations.forEach((operation) => {
        const category = app.commands.maintenanceOperationGroup(operation);
        if (!groups.has(category)) groups.set(category, []);
        groups.get(category).push(operation);
      });
      const categoryOrder = new Map([
        ["Audit", 0],
        ["Health Checks", 0],
        ["Images", 10],
        ["Calibration Models", 11],
        ["Geometry Artifacts", 20],
        ["Geometry Exports", 20],
        ["System Maintenance", 21],
        ["Unavailable Operations", 99],
      ]);
      return [...groups.entries()].sort(([categoryA], [categoryB]) => {
        const rankA = categoryOrder.get(categoryA) ?? 50;
        const rankB = categoryOrder.get(categoryB) ?? 50;
        if (rankA !== rankB) return rankA - rankB;
        return String(categoryA).localeCompare(String(categoryB));
      });
    }

    async function refreshReports() {
      try {
        const payload = await app.api.fetchMaintenanceReports();
        state.reports = payload?.reports || [];
      } catch (err) {
        console.warn("[maintenance] Failed to load reports:", err);
      }
    }

    function reportTableHtml() {
      if (!state.reports.length) {
        return `<p class="small-copy">No maintenance reports yet.</p>`;
      }
      return `
        <div class="maintenance-activity-table" role="table" aria-label="Maintenance activity log">
          <div class="maintenance-activity-head" role="row">
            <span role="columnheader">Name</span>
            <span role="columnheader">Type</span>
            <span role="columnheader">Status</span>
            <span role="columnheader">File Name</span>
          </div>
          <div class="maintenance-activity-rows">
            ${state.reports
              .map((report) => {
                const reportOperation =
                  state.operations.find(
                    (operation) =>
                      operation.operation_id === report.operation_id,
                  ) || null;
                const reportName =
                  reportOperation?.name ||
                  report.operation_id ||
                  report.report_id ||
                  "";
                return `
              <div class="maintenance-activity-row" role="row">
                <span role="cell" title="${app.commands.escapeHtml(reportName)}">${app.commands.escapeHtml(reportName)}</span>
                <span role="cell" title="${app.commands.escapeHtml(app.commands.maintenanceModeLabel(report.mode || "", reportOperation))}">${app.commands.escapeHtml(app.commands.maintenanceModeLabel(report.mode || "", reportOperation))}</span>
                <span role="cell" title="${app.commands.escapeHtml(report.status || "")}">${app.commands.escapeHtml(report.status || "")}</span>
                <span role="cell" title="${app.commands.escapeHtml(report.report_id || "")}">${app.commands.escapeHtml(report.report_id || "")}</span>
              </div>
            `;
              })
              .join("")}
          </div>
        </div>
      `;
    }

    function showActivityLog() {
      const logOverlay = document.createElement("div");
      logOverlay.className = "info-dialog-overlay maintenance-activity-overlay";
      const activityState = {
        clearing: false,
        message: "",
        messageKind: "success",
        childDialogOpen: false,
      };
      const closeLog = () => {
        logOverlay.remove();
        document.removeEventListener("keydown", handleLogKeydown);
      };
      const handleLogKeydown = (event) => {
        if (activityState.childDialogOpen) return;
        if (event.key === "Escape") closeLog();
      };

      const clearConfirmDialog = () =>
        new Promise((resolve) => {
          activityState.childDialogOpen = true;
          const confirmOverlay = document.createElement("div");
          confirmOverlay.className =
            "info-dialog-overlay maintenance-clear-confirm-overlay";
          confirmOverlay.innerHTML = `
          <div class="info-dialog" role="dialog" aria-modal="true" aria-labelledby="maintenanceClearTitle">
            ${app.commands.renderDialogHeader({
              title: "Clear Activity Log",
              titleId: "maintenanceClearTitle",
              closeButtonHtml: app.commands.renderWindowCloseButton({
                id: "maintenanceClearClose",
                className: "info-dialog-close",
              }),
            })}
            <div class="info-dialog-body">
              <p class="info-dialog-lede">Clear maintenance activity log?</p>
              <p>This deletes saved maintenance report JSON files. It does not delete backups, RAW archives, source images, generated artifacts, quarantine files, SQLite data, or sample records.</p>
            </div>
            <div class="info-dialog-footer">
              <button class="delete-button small" type="button" id="maintenanceClearConfirm">Clear Log</button>
              <button class="ghost-button small" type="button" id="maintenanceClearCancel">Cancel</button>
            </div>
          </div>
        `;
          const cleanup = (value) => {
            confirmOverlay.remove();
            document.removeEventListener("keydown", handleConfirmKeydown);
            activityState.childDialogOpen = false;
            resolve(value);
          };
          const handleConfirmKeydown = (event) => {
            if (event.key === "Escape") cleanup(false);
          };
          confirmOverlay
            .querySelector("#maintenanceClearConfirm")
            ?.addEventListener("click", () => cleanup(true));
          confirmOverlay
            .querySelector("#maintenanceClearCancel")
            ?.addEventListener("click", () => cleanup(false));
          confirmOverlay
            .querySelector("#maintenanceClearClose")
            ?.addEventListener("click", () => cleanup(false));
          document.body.appendChild(confirmOverlay);
          document.addEventListener("keydown", handleConfirmKeydown);
        });

      const handleClearLog = async () => {
        if (activityState.clearing || !state.reports.length) return;
        const confirmed = await clearConfirmDialog();
        if (!confirmed) return;
        activityState.clearing = true;
        activityState.message = "";
        renderActivityLog();
        try {
          const result = await app.api.clearMaintenanceReports();
          await refreshReports();
          const failed = Number(result?.failed_count || 0);
          const deleted = Number(result?.deleted_count || 0);
          const skipped = Number(result?.skipped_count || 0);
          if (failed > 0) {
            activityState.messageKind = "warning";
            activityState.message = `Deleted ${deleted} report${deleted === 1 ? "" : "s"}; ${failed} could not be deleted.`;
          } else if (skipped > 0) {
            activityState.messageKind = "warning";
            activityState.message = `Deleted ${deleted} report${deleted === 1 ? "" : "s"}; skipped ${skipped} non-report item${skipped === 1 ? "" : "s"}.`;
          } else {
            activityState.messageKind = "success";
            activityState.message = deleted
              ? `Deleted ${deleted} maintenance report${deleted === 1 ? "" : "s"}.`
              : "No maintenance reports to clear.";
          }
        } catch (err) {
          activityState.messageKind = "error";
          activityState.message =
            err.message || "Could not clear maintenance reports.";
        } finally {
          activityState.clearing = false;
          renderActivityLog();
        }
      };

      function activityMessageHtml() {
        if (!activityState.message) return "";
        const className =
          activityState.messageKind === "error"
            ? "backup-restore-message is-error"
            : activityState.messageKind === "warning"
              ? "backup-restore-message is-warning"
              : "backup-restore-message is-success";
        return `<div class="${className}">${app.commands.escapeHtml(activityState.message)}</div>`;
      }

      function renderActivityLog() {
        logOverlay.innerHTML = `
          <div class="info-dialog maintenance-activity-dialog" role="dialog" aria-modal="true" aria-labelledby="maintenanceActivityTitle">
            ${app.commands.renderDialogHeader({
              title: "Maintenance Activity Log",
              titleId: "maintenanceActivityTitle",
              closeButtonHtml: app.commands.renderWindowCloseButton({
                id: "maintenanceActivityClose",
                className: "info-dialog-close",
              }),
            })}
            <div class="maintenance-activity-body">
              ${activityMessageHtml()}
              ${reportTableHtml()}
            </div>
            <div class="info-dialog-footer maintenance-activity-footer">
              <button class="ghost-button small" type="button" id="maintenanceActivityClear" ${activityState.clearing || !state.reports.length ? "disabled" : ""}>${activityState.clearing ? "Clearing..." : "Clear Log"}</button>
              <button class="ghost-button small" type="button" id="maintenanceActivityDone">Close</button>
            </div>
          </div>
        `;
        logOverlay
          .querySelector("#maintenanceActivityClose")
          ?.addEventListener("click", closeLog);
        logOverlay
          .querySelector("#maintenanceActivityDone")
          ?.addEventListener("click", closeLog);
        logOverlay
          .querySelector("#maintenanceActivityClear")
          ?.addEventListener("click", handleClearLog);
      }

      document.body.appendChild(logOverlay);
      document.addEventListener("keydown", handleLogKeydown);
      renderActivityLog();
    }

    function operationListHtml() {
      if (state.loading) {
        return `<div class="maintenance-loading">Loading maintenance operations...</div>`;
      }
      if (state.error) {
        return `<div class="backup-restore-message is-error">${app.commands.escapeHtml(state.error)}</div>`;
      }
      const groupHtml = ([category, operations]) => `
        <div class="maintenance-operation-group">
          <div class="drawer-module-cap">
            <span class="sidebar-label">${app.commands.escapeHtml(category)}</span>
          </div>
          <div class="maintenance-operation-list">
            ${operations
              .map((operation) => {
                const active =
                  selectedOperation()?.operation_id === operation.operation_id;
                const disabled = operation.enabled === false;
                return `
                <button class="maintenance-operation-card ${active ? "is-active" : ""} ${disabled ? "is-disabled" : ""}" type="button" data-maintenance-operation="${app.commands.escapeHtml(operation.operation_id)}">
                  <span class="maintenance-operation-radio" aria-hidden="true"></span>
                  <span class="maintenance-operation-copy">
                    <span class="maintenance-operation-title">${app.commands.escapeHtml(operation.name || operation.operation_id)}</span>
                    <span class="maintenance-operation-description">${app.commands.escapeHtml(operation.description || "")}</span>
                    <span class="maintenance-operation-meta">${app.commands.escapeHtml(app.commands.maintenanceOperationBrief(operation))}</span>
                  </span>
                </button>
              `;
              })
              .join("")}
          </div>
        </div>
      `;
      const columnIndexForCategory = (category) => {
        const key = String(category || "").toLowerCase();
        if (key === "audit" || key === "health checks") return 0;
        if (key === "images" || key === "calibration models") return 1;
        if (
          key === "system maintenance" ||
          key === "geometry artifacts" ||
          key === "geometry exports"
        )
          return 2;
        return 2;
      };
      const columns = [[], [], []];
      groupedOperations().forEach((group) => {
        columns[columnIndexForCategory(group[0])].push(group);
      });
      return columns
        .filter((column) => column.length)
        .map(
          (column) => `
          <div class="maintenance-operation-column">
            ${column.map(groupHtml).join("")}
          </div>
        `,
        )
        .join("");
    }

    function operationDetailHtml(operation) {
      if (!operation) {
        return `<p class="small-copy">Select a maintenance operation.</p>`;
      }
      const unavailableReason =
        operation.enabled === false
          ? operation.unavailable_reason || operation.disabled_reason || ""
          : "";
      return `
        <div class="maintenance-detail-card ${operation.enabled === false ? "is-disabled" : ""}">
          <div class="maintenance-detail-body">
            <div class="maintenance-detail-heading maintenance-detail-copy">
              <h4>${app.commands.escapeHtml(operation.name || operation.operation_id)}</h4>
              ${unavailableReason ? `<p class="maintenance-detail-meta maintenance-disabled-reason">${app.commands.escapeHtml(unavailableReason)}</p>` : ""}
            </div>
            <div class="maintenance-detail-actions">
              <button class="primary-button small" type="button" id="maintenanceStartWorkflow" ${operation.enabled === false ? "disabled" : ""}>Start Workflow</button>
            </div>
          </div>
        </div>
      `;
    }

    function render() {
      const operation = selectedOperation();
      overlay.innerHTML = `
        <div class="info-dialog maintenance-dialog" role="dialog" aria-modal="true" aria-labelledby="maintenanceTitle">
          ${app.commands.renderDialogHeader({
            title: "Maintenance",
            titleId: "maintenanceTitle",
            actionsHtml: `<button class="ghost-button small dialog-header-action" type="button" id="maintenanceActivityLog">Activity Log</button>`,
            closeButtonHtml: app.commands.renderWindowCloseButton({
              id: "maintenanceClose",
              className: "info-dialog-close",
            }),
          })}
          <div class="maintenance-body">
            <section class="maintenance-operations-surface">
              ${operationListHtml()}
            </section>
            <section class="maintenance-panel maintenance-detail-panel">
              <div class="drawer-module-cap">
                <span class="sidebar-label">Selected Operation</span>
              </div>
              <div class="maintenance-panel-body">
                ${operationDetailHtml(operation)}
              </div>
            </section>
          </div>
        </div>
      `;
      bind();
      scheduleCardHeightSync();
    }

    function bind() {
      overlay
        .querySelector("#maintenanceClose")
        ?.addEventListener("click", close);
      overlay
        .querySelector("#maintenanceActivityLog")
        ?.addEventListener("click", async () => {
          await refreshReports();
          showActivityLog();
        });
      overlay
        .querySelectorAll("[data-maintenance-operation]")
        .forEach((button) => {
          button.addEventListener("click", () => {
            state.selectedOperationId =
              button.dataset.maintenanceOperation || null;
            render();
          });
        });
      overlay
        .querySelector("#maintenanceStartWorkflow")
        ?.addEventListener("click", () => {
          const operation = selectedOperation();
          if (!operation || operation.enabled === false) return;
          if (operation.operation_id === "reextract_sample_images") {
            app.commands.showReextractSampleImagesWorkflow(
              operation,
              async () => {
                await refreshReports();
                render();
              },
            );
            return;
          }
          app.commands.showMaintenanceWorkflow(operation, async () => {
            await refreshReports();
            render();
          });
        });
    }

    document.body.appendChild(overlay);
    document.addEventListener("keydown", handleKeydown);
    window.addEventListener("resize", scheduleCardHeightSync);
    render();
    (async () => {
      try {
        const [operationsPayload] = await Promise.all([
          app.api.fetchMaintenanceOperations(),
          refreshReports(),
        ]);
        state.operations = operationsPayload?.operations || [];
        app.state.operations.maintenanceState.operations = state.operations;
        if (!state.selectedOperationId && state.operations.length) {
          state.selectedOperationId = state.operations[0].operation_id;
        }
        state.loading = false;
        render();
      } catch (err) {
        state.loading = false;
        state.error = err.message || "Failed to load maintenance operations";
        render();
      }
    })();
  }

  Object.assign(app.commands, {
    maintenanceModeLabel,
    maintenanceRiskLabel,
    maintenanceWriteLabel,
    maintenanceOperationGroup,
    maintenanceOperationBrief,
    maintenanceResourceSentence,
    maintenanceModeHelp,
    maintenanceRunButtonLabel,
    loadMaintenanceOperationsForAction,
    maintenanceOperationById,
    maintenanceSummaryHtml,
    maintenanceModelResultsHtml,
    maintenanceWarningsHtml,
    maintenanceFindingsHtml,
    maintenanceProgressHtml,
    reextractProgressHtml,
    showMaintenanceDialog,
  });
}
