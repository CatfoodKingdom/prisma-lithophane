/** Install maintenance-workflow commands. */
export function installFeaturesOperationsMaintenanceWorkflow(app) {
  function showMaintenanceWorkflow(operation, onComplete, options = {}) {
    const overlay = document.createElement("div");
    overlay.className = "info-dialog-overlay maintenance-workflow-overlay";
    const workflowOptions = options || {};
    const modes = operation.modes || [];
    const hasModeChoice = modes.length > 1;
    const defaultMode = operation.default_mode || modes[0] || "audit";
    const isExportGeometryWorkflow =
      operation.operation_id === "export_geometry_files";
    const isModelFitWorkflow =
      operation.operation_id === "refit_calibration_models";
    const initialExportGeometryScope =
      workflowOptions.exportGeometryScope === "all_geometries"
        ? "all_geometries"
        : "used_by_samples";
    const state = {
      busy: false,
      preflighting: false,
      running: false,
      operation,
      mode: defaultMode,
      preflight: null,
      preflightToken: "",
      preflightExpanded: true,
      job: null,
      result: null,
      error: "",
      exportGeometryScope: initialExportGeometryScope,
      exportOutputTypes: new Set(["step", "stl"]),
      forceCameraTransform: Boolean(workflowOptions.forceCameraTransform),
      confirmation: "",
    };

    const close = () => {
      if (state.running) return;
      overlay.remove();
    };

    function exportScopePayload() {
      if (isModelFitWorkflow) {
        return {
          force_camera_transform: Boolean(state.forceCameraTransform),
        };
      }
      if (!isExportGeometryWorkflow) return {};
      return {
        geometry_scope: state.exportGeometryScope,
        output_types: ["step", "stl"].filter((type) =>
          state.exportOutputTypes.has(type),
        ),
      };
    }

    function requiredConfirmation() {
      if (!isExportGeometryWorkflow) return "";
      return (
        state.preflight?.required_confirmation ||
        state.preflight?.summary?.required_confirmation ||
        ""
      );
    }

    function confirmationMatches() {
      const phrase = requiredConfirmation();
      if (!phrase) return true;
      return (
        app.commands.normalizeRestoreConfirmation(state.confirmation) ===
        app.commands.normalizeRestoreConfirmation(phrase)
      );
    }

    function canRun() {
      return (
        !state.busy &&
        state.preflight?.enabled !== false &&
        operation.enabled !== false &&
        !state.result &&
        confirmationMatches()
      );
    }

    function cancellationRequested() {
      return (
        state.job?.status === "cancelling" ||
        Boolean(state.job?.cancel_requested)
      );
    }

    function cancellationControlHtml() {
      if (!state.running || !state.job?.cancellable) return "";
      const requested = cancellationRequested();
      if (!requested && !state.job?.cancel_available) return "";
      return `<button class="ghost-button small" type="button" id="maintenanceWorkflowCancelJob" ${requested ? "disabled" : ""}>${requested ? "Cancelling..." : "Cancel"}</button>`;
    }

    function modeSelectionHtml(disabled = false) {
      if (!hasModeChoice) return "";
      const help = app.commands.maintenanceModeHelp(operation);
      return `
        <fieldset class="maintenance-mode-fieldset" ${disabled ? "disabled" : ""}>
          <legend>Mode</legend>
          <div class="maintenance-mode-options">
            ${modes
              .map(
                (item) => `
              <label class="maintenance-mode-option ${item === state.mode ? "is-active" : ""}">
                <input type="radio" name="maintenanceWorkflowMode" value="${app.commands.escapeHtml(item)}" ${item === state.mode ? "checked" : ""}>
                <span>${app.commands.escapeHtml(app.commands.maintenanceModeLabel(item, operation))}</span>
              </label>
            `,
              )
              .join("")}
          </div>
          ${help ? `<p class="maintenance-detail-meta">${app.commands.escapeHtml(help)}</p>` : ""}
        </fieldset>
      `;
    }

    function exportControlsHtml(disabled = false) {
      if (!isExportGeometryWorkflow) return "";
      const typeChecked = (type) =>
        state.exportOutputTypes.has(type) ? "checked" : "";
      return `
        <fieldset class="maintenance-mode-fieldset maintenance-export-fieldset" ${disabled ? "disabled" : ""}>
          <legend>Export Options</legend>
          <div class="maintenance-mode-options">
            <label class="maintenance-mode-option ${state.exportGeometryScope === "used_by_samples" ? "is-active" : ""}">
              <input type="radio" name="maintenanceExportGeometryScope" value="used_by_samples" ${state.exportGeometryScope === "used_by_samples" ? "checked" : ""}>
              <span>Only Geometries Used By Samples</span>
            </label>
            <label class="maintenance-mode-option ${state.exportGeometryScope === "all_geometries" ? "is-active" : ""}">
              <input type="radio" name="maintenanceExportGeometryScope" value="all_geometries" ${state.exportGeometryScope === "all_geometries" ? "checked" : ""}>
              <span>All Geometries</span>
            </label>
          </div>
          <div class="maintenance-export-output-types" role="group" aria-label="Geometry export file types">
            <label class="maintenance-export-type ${state.exportOutputTypes.has("step") ? "is-active" : ""}">
              <input type="checkbox" name="maintenanceExportOutputType" value="step" ${typeChecked("step")}>
              <span>STEP</span>
            </label>
            <label class="maintenance-export-type ${state.exportOutputTypes.has("stl") ? "is-active" : ""}">
              <input type="checkbox" name="maintenanceExportOutputType" value="stl" ${typeChecked("stl")}>
              <span>STL</span>
            </label>
          </div>
        </fieldset>
      `;
    }

    function modelFitControlsHtml(disabled = false) {
      if (!isModelFitWorkflow) return "";
      return `
        <fieldset class="maintenance-mode-fieldset maintenance-model-fit-fieldset" ${disabled ? "disabled" : ""}>
          <legend>Fit Options</legend>
          <div class="maintenance-export-output-types" role="group" aria-label="Model fit options">
            <label class="maintenance-export-type ${state.forceCameraTransform ? "is-active" : ""}">
              <input type="checkbox" name="maintenanceForceCameraTransform" ${state.forceCameraTransform ? "checked" : ""}>
              <span>Force Camera Transform</span>
            </label>
          </div>
        </fieldset>
      `;
    }

    function exportConfirmationHtml() {
      const phrase = requiredConfirmation();
      if (!phrase) return "";
      return `
        <label class="sample-create-field backup-restore-confirm maintenance-export-confirm">
          <span>Existing public geometry exports will be overwritten. Type this phrase to continue:</span>
          <span class="backup-restore-confirm-phrase">${app.commands.escapeHtml(phrase)}</span>
          <input type="text" id="maintenanceExportConfirm" value="${app.commands.escapeHtml(state.confirmation)}" autocomplete="off">
        </label>
      `;
    }

    function bodyHtml() {
      const preflightSummary = state.preflight?.summary || {};
      const resultSummary = state.result?.summary || {};
      const resourceSentence =
        app.commands.maintenanceResourceSentence(operation);
      return `
        ${state.preflighting ? `<div class="backup-restore-message">Running preflight...</div>` : ""}
        ${state.error ? `<div class="backup-restore-message is-error">${app.commands.escapeHtml(state.error)}</div>` : ""}
        <div class="maintenance-workflow-operation">
          <div class="maintenance-detail-body">
            <p>${app.commands.escapeHtml(operation.description || "")}</p>
            ${resourceSentence ? `<p class="maintenance-detail-meta"><strong>Touches:</strong> ${app.commands.escapeHtml(resourceSentence)}.</p>` : ""}
            ${modeSelectionHtml(Boolean(state.preflight || state.running || state.result || state.preflighting))}
            ${exportControlsHtml(Boolean(state.preflight || state.running || state.result || state.preflighting))}
            ${modelFitControlsHtml(Boolean(state.preflight || state.running || state.result || state.preflighting))}
            <div class="maintenance-workflow-tags">
              <span>${app.commands.escapeHtml(app.commands.maintenanceModeLabel(state.mode, operation))}</span>
              <span>${app.commands.escapeHtml(app.commands.maintenanceRiskLabel(operation.risk_class))}</span>
              <span>${app.commands.escapeHtml(app.commands.maintenanceWriteLabel(operation))}</span>
              <span>${operation.cancellable ? "Cancelable between items" : "Runs to completion"}</span>
            </div>
          </div>
        </div>
        ${
          !state.preflight && !state.preflighting && !state.result
            ? `
          <div class="backup-workflow-actions">
            <button class="primary-button small" type="button" id="maintenanceWorkflowPreflight">Run Preflight</button>
          </div>
        `
            : ""
        }
        ${
          state.preflight
            ? `
          <div class="maintenance-workflow-section">
            <div class="drawer-module-cap maintenance-collapsible-cap">
              <span class="sidebar-label">Preflight</span>
              ${
                state.result
                  ? `
                <div class="drawer-module-cap-actions">
                  <button class="drawer-utility-button" type="button" id="maintenanceWorkflowTogglePreflight" aria-expanded="${state.preflightExpanded ? "true" : "false"}">
                    ${state.preflightExpanded ? "Hide" : "Show"}
                  </button>
                </div>
              `
                  : ""
              }
            </div>
            ${
              state.preflightExpanded
                ? `
              <div class="maintenance-detail-body">
                ${app.commands.maintenanceSummaryHtml(preflightSummary)}
                ${app.commands.maintenanceWarningsHtml(state.preflight.warnings || [])}
                ${app.commands.maintenanceFindingsHtml({ blocked: state.preflight.blocked || [] })}
                ${state.preflight.enabled === false ? `<div class="backup-restore-message is-warning">${app.commands.escapeHtml((state.preflight.blocked || [])[0]?.reason || "This operation is not available.")}</div>` : ""}
              </div>
            `
                : ""
            }
          </div>
        `
            : ""
        }
        ${
          state.preflight && !state.result
            ? `
          ${exportConfirmationHtml()}
          <div class="backup-workflow-actions">
            <button class="primary-button small" type="button" id="maintenanceWorkflowRun" ${!canRun() ? "disabled" : ""}>
              ${app.commands.escapeHtml(app.commands.maintenanceRunButtonLabel(operation))}
            </button>
            ${cancellationControlHtml()}
          </div>
        `
            : ""
        }
        ${app.commands.maintenanceProgressHtml(state.job, state.running, "Running maintenance")}
        ${
          state.result
            ? `
          <div class="maintenance-workflow-section" id="maintenanceWorkflowResult">
            <div class="drawer-module-cap">
              <span class="sidebar-label">Result</span>
            </div>
            <div class="maintenance-detail-body">
              <div class="backup-restore-message ${state.result.status === "completed" ? "is-success" : "is-warning"}">${app.commands.escapeHtml(state.result.status === "completed" ? "Maintenance operation complete." : `Maintenance operation ${state.result.status || "finished"}.`)}</div>
              ${app.commands.maintenanceSummaryHtml(resultSummary)}
              ${app.commands.maintenanceModelResultsHtml(state.result)}
              ${app.commands.maintenanceWarningsHtml(state.result.warnings || [])}
              ${state.result.report_path ? `<div class="backup-restore-path mono" title="${app.commands.escapeHtml(state.result.report_path)}">${app.commands.escapeHtml(state.result.report_path)}</div>` : ""}
              ${app.commands.maintenanceFindingsHtml(state.result)}
            </div>
          </div>
        `
            : ""
        }
      `;
    }

    function render() {
      const disableClose = state.running;
      overlay.innerHTML = `
        <div class="info-dialog maintenance-workflow-dialog" role="dialog" aria-modal="true" aria-labelledby="maintenanceWorkflowTitle">
          ${app.commands.renderDialogHeader({
            title: operation.name || "Maintenance Workflow",
            titleId: "maintenanceWorkflowTitle",
            closeButtonHtml: app.commands.renderWindowCloseButton({
              id: "maintenanceWorkflowClose",
              className: "info-dialog-close",
              disabled: disableClose,
            }),
          })}
          <div class="info-dialog-body maintenance-workflow-body">
            ${bodyHtml()}
          </div>
        </div>
      `;
      overlay
        .querySelector("#maintenanceWorkflowClose")
        ?.addEventListener("click", close);
      overlay
        .querySelectorAll("input[name='maintenanceWorkflowMode']")
        .forEach((input) => {
          input.addEventListener("change", () => {
            if (
              state.preflight ||
              state.running ||
              state.result ||
              state.preflighting
            )
              return;
            state.mode = input.value || defaultMode;
            state.confirmation = "";
            render();
          });
        });
      overlay
        .querySelectorAll("input[name='maintenanceExportGeometryScope']")
        .forEach((input) => {
          input.addEventListener("change", () => {
            if (
              state.preflight ||
              state.running ||
              state.result ||
              state.preflighting
            )
              return;
            state.exportGeometryScope =
              input.value === "all_geometries"
                ? "all_geometries"
                : "used_by_samples";
            state.confirmation = "";
            render();
          });
        });
      overlay
        .querySelectorAll("input[name='maintenanceExportOutputType']")
        .forEach((input) => {
          input.addEventListener("change", () => {
            if (
              state.preflight ||
              state.running ||
              state.result ||
              state.preflighting
            )
              return;
            const type = input.value === "stl" ? "stl" : "step";
            if (input.checked) {
              state.exportOutputTypes.add(type);
            } else if (state.exportOutputTypes.size > 1) {
              state.exportOutputTypes.delete(type);
            }
            state.confirmation = "";
            render();
          });
        });
      overlay
        .querySelector("input[name='maintenanceForceCameraTransform']")
        ?.addEventListener("change", (event) => {
          if (
            state.preflight ||
            state.running ||
            state.result ||
            state.preflighting
          )
            return;
          state.forceCameraTransform = Boolean(event.target.checked);
          render();
        });
      overlay
        .querySelector("#maintenanceExportConfirm")
        ?.addEventListener("input", (event) => {
          state.confirmation = event.target.value || "";
          const button = overlay.querySelector("#maintenanceWorkflowRun");
          if (button) button.disabled = !canRun();
        });
      overlay
        .querySelector("#maintenanceWorkflowPreflight")
        ?.addEventListener("click", runPreflight);
      overlay
        .querySelector("#maintenanceWorkflowRun")
        ?.addEventListener("click", runJob);
      overlay
        .querySelector("#maintenanceWorkflowTogglePreflight")
        ?.addEventListener("click", () => {
          state.preflightExpanded = !state.preflightExpanded;
          render();
        });
      overlay
        .querySelector("#maintenanceWorkflowCancelJob")
        ?.addEventListener("click", async () => {
          if (!state.job?.job_id || !state.job.cancel_available) return;
          const cancellationJobId = String(state.job.job_id);
          try {
            const response =
              await app.api.cancelMaintenanceJob(cancellationJobId);
            if (String(state.job?.job_id || "") !== cancellationJobId) return;
            app.commands.assertPolledJobIdentity(response, cancellationJobId);
            state.job = response;
            render();
          } catch (err) {
            if (String(state.job?.job_id || "") !== cancellationJobId) return;
            state.error = err.message || "Cancel request failed";
            render();
          }
        });
    }

    function scrollMaintenanceWorkflowToResult() {
      window.setTimeout(() => {
        const body = overlay.querySelector(".maintenance-workflow-body");
        const result = overlay.querySelector("#maintenanceWorkflowResult");
        if (!body || !result) return;
        const bodyBox = body.getBoundingClientRect();
        const resultBox = result.getBoundingClientRect();
        body.scrollTo({
          top: Math.max(0, body.scrollTop + resultBox.top - bodyBox.top - 8),
          behavior: "smooth",
        });
      }, 0);
    }

    async function runJob() {
      if (!canRun()) return;
      state.busy = true;
      state.running = true;
      state.error = "";
      state.job = null;
      render();
      try {
        const job = await app.api.startMaintenanceJob(
          operation.operation_id,
          state.mode,
          state.preflightToken,
          exportScopePayload(),
          isExportGeometryWorkflow ? state.confirmation : "",
        );
        state.job = job;
        render();
        const jobId = String(job?.job_id || "");
        if (!jobId) throw new Error("Maintenance job did not return a job id.");
        const nextJob = await app.commands.pollJobUntilTerminal({
          jobId,
          fetchStatus: () => app.api.fetchMaintenanceJobStatus(jobId),
          isTerminal: (status) =>
            ["succeeded", "failed", "cancelled"].includes(
              String(status.status || ""),
            ),
          shouldContinue: () => overlay.isConnected && state.running,
          intervalMs: 700,
          onStatus: (status) => {
            state.job = status;
            render();
          },
          onTransientError: () => {
            state.job = {
              ...(state.job || {}),
              job_id: jobId,
              message: "Connection interrupted; retrying maintenance status...",
            };
            render();
          },
        });
        if (!nextJob) return;
        if (nextJob.status === "succeeded") {
          state.result = nextJob.result || null;
          state.preflightExpanded = false;
          state.error = "";
          state.running = false;
          state.busy = false;
          await app.commands.applyMaintenanceRefreshImpact(
            state.result?.ui_refresh || {},
          );
          if (onComplete) await onComplete(state.result);
          app.commands.showImportToast(
            `${operation.name || "Maintenance"} complete`,
            "success",
          );
          render();
          scrollMaintenanceWorkflowToResult();
          return;
        }
        state.result = nextJob.result || null;
        state.preflightExpanded = false;
        state.error =
          nextJob.status === "failed"
            ? nextJob.error?.message || nextJob.message || "Maintenance failed"
            : "";
        state.running = false;
        state.busy = false;
        render();
        scrollMaintenanceWorkflowToResult();
      } catch (err) {
        state.error = err.message || "Maintenance job failed";
        state.running = false;
        state.busy = false;
        render();
      }
    }

    async function runPreflight() {
      if (state.preflighting || state.running || state.result) return;
      state.busy = true;
      state.preflighting = true;
      state.error = "";
      state.preflight = null;
      state.preflightToken = "";
      state.preflightExpanded = true;
      state.confirmation = "";
      render();
      try {
        const payload = await app.api.preflightMaintenanceOperation(
          operation.operation_id,
          state.mode,
          exportScopePayload(),
        );
        state.preflight = payload?.preflight || null;
        state.preflightToken = payload?.preflight_token || "";
        state.preflighting = false;
        state.busy = false;
        render();
      } catch (err) {
        state.error = err.message || "Maintenance preflight failed";
        state.preflighting = false;
        state.busy = false;
        render();
      }
    }

    document.body.appendChild(overlay);
    render();
  }

  Object.assign(app.commands, {
    showMaintenanceWorkflow,
  });
}
