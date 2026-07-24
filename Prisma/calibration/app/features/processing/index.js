/** Install features/processing/index commands. */
export function installFeaturesProcessingIndex(app) {
  function getProcessingCards() {
    // Only samples that need action — not already-processed ones
    const cards = app.state.session.data.samples.filter((e) => {
      return (
        e._processing_status === "assigned" ||
        e._processing_status === "failed" ||
        e._processing_status === "flagged" ||
        e._flag_reason
      );
    });
    const statusOrder = (e) => {
      if (e._processing_status === "assigned") return 0; // ready to process — show first
      if (e._processing_status === "failed") return 1;
      if (e._processing_status === "flagged" || e._flag_reason) return 2;
      return 3; // processed
    };
    return [...cards].sort((a, b) => statusOrder(a) - statusOrder(b));
  }

  function processingCardStatus(exp) {
    const status = app.commands.sampleStatusMeta(exp);
    if (status.cls === "ready")
      return { label: "ready to process", cls: "ready" };
    return status;
  }

  function renderProcessingCard(exp) {
    const status = app.commands.processingCardStatus(exp);
    const fil = app.commands.filamentMeta(exp.variable_filament_id);
    const filName = fil
      ? fil.display_name || fil.color_name
      : exp.variable_filament_id;
    const hex = fil?.hex || exp.variable_hex || "#ddd";
    const sid = exp.sample_id;
    const isProcessing =
      app.state.processing.processingState.singleRunningSampleIds?.has(sid);
    const disableActions = app.state.processing.processingState.batchRunning;
    const hasResults =
      exp._processing_status === "processed" ||
      exp._processing_status === "flagged" ||
      exp._processing_status === "failed";
    const flagReason = exp._flag_reason ? ` — ${exp._flag_reason}` : "";

    const ORIENT_ARROWS = ["\u2191", "\u2192", "\u2193", "\u2190"];
    const orientRot = exp._orientation_rots;
    const orientLabel = orientRot != null ? ORIENT_ARROWS[orientRot] : "";
    const imgName = exp._assigned_image || "";
    const blankId = exp._assigned_blank_id || "";

    const photoThumb = imgName
      ? `<div class="pq-thumb"><img src="${app.commands.previewUrl(imgName)}" alt="photo" onerror="this.style.display='none'"><span class="pq-thumb-label">${imgName.replace(/\.[^.]+$/, "")}</span></div>`
      : `<div class="pq-thumb pq-thumb-empty"><span>no photo</span></div>`;
    const blankFilename =
      blankId && app.state.session.data.blanks
        ? app.state.session.data.blanks.find((b) => b.blank_id === blankId)
            ?.original_filename || ""
        : "";
    const blankThumb = blankId
      ? `<div class="pq-thumb"><img src="${app.commands.previewUrl(blankFilename)}" alt="blank" onerror="this.style.display='none'"><span class="pq-thumb-label">${blankId}</span></div>`
      : `<div class="pq-thumb pq-thumb-empty"><span>no blank</span></div>`;

    // For "assigned" (not yet processed) samples, show an info card with strip + thumbnails
    if (!hasResults) {
      return `
        <div class="proc-card" data-sample-id="${sid}">
          <div class="proc-card-header">
            <span class="color-chip" style="background:${hex}"></span>
            <span class="mono">${sid}</span>
            <span class="proc-card-filament">${filName}</span>
            <span class="pq-header-actions" style="margin-left:auto">
              <button class="ghost-button xs proc-reassign-btn" data-sample-id="${sid}" ${disableActions ? "disabled" : ""}>Reassign</button>
              <button class="ghost-button xs proc-flag-btn" data-sample-id="${sid}" ${disableActions ? "disabled" : ""}>Flag</button>
              <button class="ghost-button xs proc-reprocess-btn${isProcessing ? " is-busy" : ""}" data-sample-id="${sid}" ${isProcessing || disableActions ? "disabled" : ""}>
                ${isProcessing ? '<span class="proc-spinner"></span> Processing...' : "Process"}
              </button>
            </span>
          </div>
          <div class="pq-info-row">
            <div class="pq-strip-col">${app.commands.buildStripMiniTable(exp)}</div>
            ${photoThumb}
            ${blankThumb}
            ${orientLabel ? `<div class="pq-orient-arrow" title="Open side direction">${orientLabel}</div>` : ""}
          </div>
        </div>
      `;
    }

    return `
      <div class="proc-card" data-sample-id="${sid}">
        <div class="proc-card-header">
          <span class="color-chip" style="background:${hex}"></span>
          <span class="mono">${sid}</span>
          <span class="proc-card-filament">${filName}</span>
          <span class="status-pill ${status.cls}">${status.label}</span>
          <span class="pq-header-actions" style="margin-left:auto">
            <button class="ghost-button xs proc-reassign-btn" data-sample-id="${sid}" ${disableActions ? "disabled" : ""}>Reassign</button>
            <button class="ghost-button xs proc-unflag-btn" data-sample-id="${sid}" style="color:#2e7d32;border-color:#2e7d3266" ${disableActions ? "disabled" : ""}>Unflag</button>
            <button class="ghost-button xs proc-manual-btn" data-sample-id="${sid}" ${disableActions ? "disabled" : ""}>Manual Process</button>
          </span>
        </div>
        <div class="pq-info-row">
          <div class="pq-strip-col">${app.commands.buildStripMiniTable(exp)}</div>
          ${photoThumb}
          ${blankThumb}
          ${orientLabel ? `<div class="pq-orient-arrow" title="Open side direction">${orientLabel}</div>` : ""}
        </div>
      </div>
    `;
  }

  function renderPostProcessingCard(exp) {
    const fil = app.commands.filamentMeta(exp.variable_filament_id);
    const filName = fil
      ? fil.display_name || fil.color_name
      : exp.variable_filament_id;
    const hex = fil?.hex || exp.variable_hex || "#ddd";
    const sid = exp.sample_id;
    const variableBrand = fil?.manufacturer || exp.manufacturer || "";
    const variableName =
      fil?.color_name ||
      fil?.display_name ||
      exp.variable_color_name ||
      exp.variable_filament_id;
    const fixedHeaderRun = app.commands
      .sampleFilamentRoleLines(exp)
      .filter((line) => line.roleKind === "fixed")
      .map((line) => {
        return `
        <span class="proc-filament-run is-fixed" title="${app.commands._escAttr(line.layerLabel)}">
          <span class="color-chip" style="background:${line.hex || "#cccccc"}"></span>
          <span class="proc-filament-token">${app.commands._escHtml(line.name)}</span>
        </span>
      `;
      })
      .join("");

    // Thumbnails: source + blank
    const imgName = exp._assigned_image || "";
    const blankId = exp._assigned_blank_id || "";
    const blankFilename =
      blankId && app.state.session.data.blanks
        ? app.state.session.data.blanks.find(
            (blank) => blank.blank_id === blankId,
          )?.original_filename || ""
        : "";
    const srcThumb = imgName
      ? `<div class="pq-thumb"><img src="${app.commands.sampleThumbnailUrl(sid, "source", true)}" alt="source" onerror="this.style.display='none'"><span class="pq-thumb-label">Source</span></div>`
      : "";
    const blankThumb = blankId
      ? `<div class="pq-thumb"><img src="${app.commands.previewUrl(blankFilename)}" alt="blank" onerror="this.style.display='none'"><span class="pq-thumb-label">Blank</span></div>`
      : "";

    // Build extracted strip with geometry-aligned labels, plus a mock strip for comparison.
    const swatches = exp._measurements?.swatches;
    const thicknesses = exp.variable_thicknesses_mm || [];
    const n = swatches?.length || thicknesses.length;
    let stripPairHtml = "";
    if (n > 0) {
      const metrics = app.commands.sampleStripMetrics(exp, n);
      const hasMeasuredSwatches =
        Array.isArray(swatches) && swatches.length > 0;
      const swatchData = hasMeasuredSwatches
        ? [...swatches].sort((a, b) => {
            const ai = Number(a.swatch_index ?? Number.MAX_SAFE_INTEGER);
            const bi = Number(b.swatch_index ?? Number.MAX_SAFE_INTEGER);
            if (ai !== bi) return ai - bi;
            return (
              Number(a.nominal_thickness_mm ?? 0) -
              Number(b.nominal_thickness_mm ?? 0)
            );
          })
        : thicknesses.map((t) => ({ nominal_thickness_mm: t, display: null }));
      const displaySwatches = Array.from(
        { length: metrics.n },
        (_, index) => swatchData[index] || null,
      );
      const labelCells = displaySwatches
        .map((sw) => {
          const thickness = Number(sw?.nominal_thickness_mm);
          return `<span class="ppr-thickness-label">${Number.isFinite(thickness) ? thickness.toFixed(2) : ""}</span>`;
        })
        .join("");
      const thicknessLabels = `<span class="ppr-thickness-spacer" aria-hidden="true"></span>${labelCells}<span class="ppr-thickness-spacer" aria-hidden="true"></span>`;
      const extractedEl = `<div class="ppr-extracted-stack">
        <div class="ppr-extracted-strip">
          <img src="${app.commands.sampleThumbnailUrl(sid, "strip", true)}" alt="extracted" onerror="this.parentElement.innerHTML='<span class=small-copy>No strip</span>'">
        </div>
        <div class="ppr-thickness-row" style="grid-template-columns:${metrics.gridCols}">${thicknessLabels}</div>
      </div>`;

      // Mock strip: same swatch count as the physical strip geometry.
      const cols = `repeat(${metrics.n}, 1fr)`;
      const maxThickness = Math.max(
        ...displaySwatches
          .map((sw) => Number(sw?.nominal_thickness_mm))
          .filter(Number.isFinite),
        0.01,
      );
      const mockTiles = displaySwatches
        .map((sw) => {
          const displayHex = app.commands.swatchDisplayDomain(sw).hex;
          const bg = displayHex || hex;
          const thickness = Number(sw?.nominal_thickness_mm);
          const opacity = displayHex
            ? 1
            : Math.max(
                0.08,
                Math.min(
                  1,
                  Number.isFinite(thickness) ? thickness / maxThickness : 0.08,
                ),
              );
          return `<div class="ppr-mock-swatch" style="background:${bg};${displayHex ? "" : `opacity:${opacity.toFixed(2)};`}"></div>`;
        })
        .join("");
      const mockEl = `<div class="ppr-mock-strip" style="grid-template-columns:${cols}">${mockTiles}</div>`;
      stripPairHtml = `<div class="ppr-strip-stack">
        <div class="ppr-strip-pair">${extractedEl}${mockEl}</div>
      </div>`;
    }

    return `
      <div class="proc-card" data-sample-id="${sid}">
        <div class="proc-card-header">
          <span class="mono">${sid}</span>
          <div class="proc-card-filament-run">
            <span class="proc-filament-run is-variable">
              <span class="color-chip" style="background:${hex}"></span>
              <span class="proc-filament-token">${variableBrand ? `${variableBrand} ` : ""}${variableName}</span>
            </span>
            ${fixedHeaderRun}
          </div>
          <span class="pq-header-actions" style="margin-left:auto">
            <button class="ghost-button xs pq-reprocess-btn" data-sample-id="${sid}" style="color:#e65100;border-color:#e6510066">Reject</button>
            <button class="ghost-button xs pq-dismiss-btn" data-sample-id="${sid}" style="color:#2e7d32;border-color:#2e7d3266">Accept</button>
          </span>
        </div>
        <div class="ppr-body">
          <div class="ppr-thumbs">${srcThumb}${blankThumb}</div>
          ${stripPairHtml}
        </div>
      </div>
    `;
  }

  async function hydrateSampleMeasurements(sampleId, { force = false } = {}) {
    const exp = app.state.session.data.samples.find(
      (e) => e.sample_id === sampleId,
    );
    if (!exp) return null;
    if (!force && exp._measurements) return exp._measurements;
    if (app.state.processing._measurementHydrationInFlight.has(sampleId))
      return null;
    app.state.processing._measurementHydrationInFlight.add(sampleId);
    try {
      const detail = await app.api.fetchSampleDetail(sampleId);
      exp._measurements = (detail && detail.measurements) || null;
      // Keep the summary counts coherent with freshly-fetched detail.
      const sw = exp._measurements?.swatches || [];
      exp._n_swatches = sw.length;
      exp._n_excluded = sw.filter((s) => s.fit_state === "excluded").length;
      return exp._measurements;
    } catch (err) {
      console.error(`[detail] hydrate ${sampleId} failed:`, err);
      return null;
    } finally {
      app.state.processing._measurementHydrationInFlight.delete(sampleId);
    }
  }

  function ensureMeasurementsThenRerender(sampleIds, rerenderFn) {
    const pending = [];
    for (const sid of sampleIds) {
      const exp = app.state.session.data.samples.find(
        (e) => e.sample_id === sid,
      );
      if (
        exp &&
        app.commands.sampleHasMeasurementOutput(exp) &&
        !exp._measurements &&
        !app.state.processing._measurementHydrationInFlight.has(sid)
      ) {
        pending.push(sid);
      }
    }
    if (pending.length === 0) return;
    Promise.all(
      pending.map((sid) => app.commands.hydrateSampleMeasurements(sid)),
    ).then((results) => {
      if (results.some(Boolean)) rerenderFn();
    });
  }

  function renderProcessingDashboard() {
    app.dom.tableToolbar.className = "toolbar-inline";
    app.dom.tableSummary.textContent = "";
    app.dom.tableToolbar.innerHTML = "";

    // Left pane data: pre-processing
    const allCards = app.commands.getProcessingCards();
    const assigned = allCards.filter(
      (e) => e._processing_status === "assigned",
    );
    const failed = allCards.filter((e) => e._processing_status === "failed");
    const flagged = allCards.filter(
      (e) =>
        e._processing_status === "flagged" ||
        (e._flag_reason && e._processing_status !== "processed"),
    );

    // Right pane data: post-processing (exclude accepted/reviewed)
    const processed = app.state.session.data.samples.filter(
      (e) => e._processing_status === "processed" && !e._review_accepted,
    );

    if (!app.state.processing.processingState._collapseState)
      app.state.processing.processingState._collapseState = {};
    const cs = app.state.processing.processingState._collapseState;

    function sectionHtml(
      key,
      label,
      items,
      titleActionsHtml = "",
      extraAfterTitle = "",
    ) {
      const collapsed = cs[key];
      const caret = collapsed ? "&#x25B6;" : "&#x25BC;";
      const content = collapsed
        ? ""
        : items.length > 0
          ? items.map((cardFn) => cardFn()).join("")
          : `<div class="pq-empty-section"><span class="small-copy">None</span></div>`;
      const extra = !collapsed && extraAfterTitle ? extraAfterTitle : "";
      return `<div class="import-section-title" data-collapse-key="${key}">
          <div class="import-section-title-main">
            <span class="collapse-caret">${caret}</span><span>${label} (${items.length})</span>
          </div>
          ${titleActionsHtml ? `<div class="import-section-title-actions">${titleActionsHtml}</div>` : ""}
        </div>${extra}${content}`;
    }

    const processAllBtnLabel =
      app.state.processing.processingState.batchRunning &&
      app.state.processing.processingState.batchProgress
        ? `<span class="proc-spinner"></span> Processing ${Math.min(app.state.processing.processingState.batchProgress.completed || 0, app.state.processing.processingState.batchProgress.total || 0)} / ${app.state.processing.processingState.batchProgress.total || assigned.length}`
        : `Process All (${assigned.length})`;
    const processAllBtn =
      assigned.length > 0
        ? `<button class="primary-button xs" id="processAllBtn" ${app.state.processing.processingState.batchRunning ? "disabled" : ""}>${processAllBtnLabel}</button>`
        : "";

    const manualFailedBtn =
      failed.length > 0
        ? `<div class="pq-section-action">
        <button class="ghost-button xs manual-all-btn" data-queue="failed" ${app.state.processing.processingState.batchRunning ? "disabled" : ""}>Manual Process All (${failed.length})</button>
      </div>`
        : "";

    const manualFlaggedBtn =
      flagged.length > 0
        ? `<div class="pq-section-action">
        <button class="ghost-button xs manual-all-btn" data-queue="flagged" ${app.state.processing.processingState.batchRunning ? "disabled" : ""}>Manual Process All (${flagged.length})</button>
      </div>`
        : "";

    const batch = app.state.processing.processingState.batchProgress;
    const processedTitleActions =
      processed.length > 0
        ? `
      <button class="ghost-button xs" id="processedRejectAllBtn" ${app.state.processing.processingState.batchRunning ? "disabled" : ""}>Reject All</button>
      <button class="ghost-button xs" id="processedAcceptAllBtn" ${app.state.processing.processingState.batchRunning ? "disabled" : ""}>Accept All</button>
    `
        : "";
    const batchProgressPanel = batch
      ? (() => {
          const completed = Math.min(batch.completed || 0, batch.total || 0);
          const total = batch.total || 0;
          const percent = total > 0 ? Math.round((completed / total) * 100) : 0;
          const remaining = Math.max(total - completed, 0);
          const heading = batch.done ? "Batch complete" : "Batch processing";
          const currentLabel = batch.currentSampleId
            ? `<span>Current: <span class="mono">${batch.currentSampleId}</span></span>`
            : batch.done
              ? `<span>Current: <span class="mono">done</span></span>`
              : "";
          return `
        <div class="pq-batch-status${batch.done ? " is-complete" : ""}">
          <div class="pq-batch-head">
            <strong>${heading}</strong>
            <span>${completed} / ${total}</span>
          </div>
          <div class="pq-batch-bar">
            <div class="pq-batch-bar-fill" style="width:${percent}%"></div>
          </div>
          <div class="pq-batch-meta">
            ${currentLabel}
            <span>Processed: ${batch.succeeded || 0}</span>
            <span>Flagged: ${batch.flagged || 0}</span>
            <span>Failed: ${batch.failed || 0}</span>
            <span>Remaining: ${remaining}</span>
          </div>
        </div>
      `;
        })()
      : "";

    const leftSections = [
      sectionHtml(
        "pq-assigned",
        "Ready to Process",
        assigned.map((e) => () => app.commands.renderProcessingCard(e)),
        processAllBtn,
      ),
      sectionHtml(
        "pq-failed",
        "Failed",
        failed.map((e) => () => app.commands.renderProcessingCard(e)),
        manualFailedBtn,
      ),
      sectionHtml(
        "pq-flagged",
        "Flagged for Manual Processing",
        flagged.map((e) => () => app.commands.renderProcessingCard(e)),
        manualFlaggedBtn,
      ),
    ].join("");

    const rightSections = sectionHtml(
      "pq-processed",
      "Processed",
      processed.map((e) => () => app.commands.renderPostProcessingCard(e)),
      processedTitleActions,
    );

    app.dom.tableContainer.innerHTML = `
      <div class="proc-split-layout">
        <div class="proc-split-pane">
          <div class="proc-pane-title">Pre-Processing Queue</div>
          ${batchProgressPanel}
          <div class="proc-card-list">
            ${leftSections}
          </div>
        </div>
        <div class="proc-split-divider"></div>
        <div class="proc-split-pane proc-review-pane">
          <div class="proc-pane-title">Post-Processing Review</div>
          <div class="proc-card-list">
            ${rightSections}
          </div>
        </div>
      </div>
    `;

    const processAllBtnEl = document.getElementById("processAllBtn");
    if (processAllBtnEl && !app.state.processing.processingState.batchRunning) {
      processAllBtnEl.addEventListener("click", (e) => {
        e.stopPropagation();
        app.commands.handleProcessAll();
      });
    }

    document
      .getElementById("processedAcceptAllBtn")
      ?.addEventListener("click", async (e) => {
        e.stopPropagation();
        let accepted = 0;
        const errors = [];
        for (const exp of processed) {
          try {
            await app.api.updateSample(exp.sample_id, {
              review_accepted: true,
            });
            accepted += 1;
          } catch (err) {
            errors.push(`${exp.sample_id}: ${err.message}`);
          }
        }
        await app.commands.handleRefresh();
        app.commands.renderProcessingDashboard();
        if (errors.length) {
          app.commands.showImportToast(
            `Accepted ${accepted}; ${errors.length} failed`,
            "error",
          );
        } else {
          app.commands.showImportToast(
            `Accepted ${accepted} processed samples`,
            "success",
          );
        }
      });

    document
      .getElementById("processedRejectAllBtn")
      ?.addEventListener("click", async (e) => {
        e.stopPropagation();
        let rejected = 0;
        const errors = [];
        for (const exp of processed) {
          try {
            await app.api.rejectSample(exp.sample_id);
            rejected += 1;
          } catch (err) {
            errors.push(`${exp.sample_id}: ${err.message}`);
          }
        }
        await app.commands.handleRefresh();
        app.commands.renderProcessingDashboard();
        if (errors.length) {
          app.commands.showImportToast(
            `Rejected ${rejected}; ${errors.length} failed`,
            "error",
          );
        } else {
          app.commands.showImportToast(
            `Rejected ${rejected} processed samples`,
            "success",
          );
        }
      });

    // Collapsible section toggling
    app.dom.tableContainer
      .querySelectorAll(".import-section-title-actions")
      .forEach((actions) => {
        actions.addEventListener("click", (e) => e.stopPropagation());
      });
    app.dom.tableContainer
      .querySelectorAll(".import-section-title[data-collapse-key]")
      .forEach((title) => {
        title.addEventListener("click", () => {
          const key = title.dataset.collapseKey;
          if (!app.state.processing.processingState._collapseState)
            app.state.processing.processingState._collapseState = {};
          app.state.processing.processingState._collapseState[key] =
            !app.state.processing.processingState._collapseState[key];
          app.commands.renderProcessingDashboard();
        });
      });

    app.dom.tableContainer
      .querySelectorAll(".proc-reprocess-btn")
      .forEach((btn) => {
        btn.addEventListener("click", (e) => {
          e.stopPropagation();
          app.commands.handleReprocessSingle(btn.dataset.sampleId);
        });
      });
    app.dom.tableContainer.querySelectorAll(".proc-flag-btn").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        app.commands.handleFlagSample(btn.dataset.sampleId, "Manual flag");
      });
    });
    app.dom.tableContainer
      .querySelectorAll(".proc-unflag-btn")
      .forEach((btn) => {
        btn.addEventListener("click", (e) => {
          e.stopPropagation();
          app.commands.handleUnflagSample(btn.dataset.sampleId);
        });
      });

    // Reassign buttons — unassign image, sending sample back to Assign Images
    app.dom.tableContainer
      .querySelectorAll(".proc-reassign-btn")
      .forEach((btn) => {
        btn.addEventListener("click", async (e) => {
          e.stopPropagation();
          const sid = btn.dataset.sampleId;
          try {
            await app.api.unassignImage(sid);
            await app.api.unflagSample(sid).catch(() => {}); // clear flag if any
            await app.commands.handleRefresh();
            app.commands.renderProcessingDashboard();
            app.commands.showImportToast(
              `${sid} sent back to Assign Images`,
              "ok",
            );
          } catch (err) {
            app.commands.showImportToast(
              err.message || "Failed to reassign",
              "error",
            );
          }
        });
      });

    // Manual process buttons (single + batch)
    app.dom.tableContainer
      .querySelectorAll(".proc-manual-btn")
      .forEach((btn) => {
        btn.addEventListener("click", (e) => {
          e.stopPropagation();
          app.commands.openManualProcessing([btn.dataset.sampleId]);
        });
      });
    app.dom.tableContainer
      .querySelectorAll(".manual-all-btn")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          const queue = btn.dataset.queue;
          const items = queue === "failed" ? failed : flagged;
          app.commands.openManualProcessing(items.map((e) => e.sample_id));
        });
      });

    // Post-processing pane: Reject → clear measurements and return to assigned
    app.dom.tableContainer
      .querySelectorAll(".pq-reprocess-btn")
      .forEach((btn) => {
        btn.addEventListener("click", async (e) => {
          e.stopPropagation();
          const sid = btn.dataset.sampleId;
          try {
            await app.api.rejectSample(sid);
            await app.commands.handleRefresh();
            app.commands.renderProcessingDashboard();
            app.commands.showImportToast(
              `${sid} sent back for reprocessing`,
              "ok",
            );
          } catch (err) {
            app.commands.showImportToast(
              err.message || "Reject failed",
              "error",
            );
          }
        });
      });

    // Post-processing pane: Dismiss → accept the result (no action needed, just remove from view)
    app.dom.tableContainer
      .querySelectorAll(".pq-dismiss-btn")
      .forEach((btn) => {
        btn.addEventListener("click", async (e) => {
          e.stopPropagation();
          const sid = btn.dataset.sampleId;
          // Persist review_accepted to the server so it survives reload
          try {
            await app.api.updateSample(sid, { review_accepted: true });
          } catch (err) {
            console.error("[review] Failed to persist accept for", sid, err);
          }
          // Also update local data so the card disappears immediately
          const exp = app.state.session.data.samples.find(
            (x) => x.sample_id === sid,
          );
          if (exp) exp._review_accepted = true;
          app.commands.renderProcessingDashboard();
        });
      });

    // The "Processed" review cards render per-swatch hex; hydrate their color
    // from the detail endpoint (the slim list omits it) and re-render once it lands.
    app.commands.ensureMeasurementsThenRerender(
      processed.map((e) => e.sample_id),
      app.commands.renderProcessingDashboard,
    );
  }

  async function handleProcessAll() {
    const targets = app.commands
      .getProcessingCards()
      .filter((exp) => exp._processing_status === "assigned")
      .map((exp) => exp.sample_id);
    if (!targets.length) return;

    app.state.processing.processingState.batchRunning = true;
    app.state.processing.processingState.batchProgress = {
      total: targets.length,
      completed: 0,
      succeeded: 0,
      flagged: 0,
      failed: 0,
      currentSampleId: null,
      errors: [],
      done: false,
    };
    app.commands.renderWorkspace();

    for (const sampleId of targets) {
      app.state.processing.processingState.batchProgress.currentSampleId =
        sampleId;
      app.state.processing.processingState.singleRunningSampleIds.add(sampleId);
      app.commands.renderWorkspace();

      try {
        const result =
          typeof app.api.processSingle === "function"
            ? await app.api.processSingle(sampleId)
            : null;
        const status = result?.status || "failed_detection";
        if (status === "success") {
          app.state.processing.processingState.batchProgress.succeeded += 1;
        } else if (status === "low_confidence") {
          app.state.processing.processingState.batchProgress.flagged += 1;
        } else {
          app.state.processing.processingState.batchProgress.failed += 1;
          app.state.processing.processingState.batchProgress.errors.push({
            sample_id: sampleId,
            error: result?.error_detail || status,
          });
        }
      } catch (err) {
        console.error(`[processing] Batch item failed for ${sampleId}:`, err);
        app.state.processing.processingState.batchProgress.failed += 1;
        app.state.processing.processingState.batchProgress.errors.push({
          sample_id: sampleId,
          error: err.message,
        });
      } finally {
        app.state.processing.processingState.batchProgress.completed += 1;
        app.state.processing.processingState.batchProgress.currentSampleId =
          null;
        app.state.processing.processingState.singleRunningSampleIds.delete(
          sampleId,
        );
      }

      if (typeof app.commands.handleRefresh === "function") {
        await app.commands.handleRefresh({ ensureAssets: false });
      } else {
        app.commands.renderWorkspace();
      }
    }

    app.state.processing.processingState.batchRunning = false;
    if (app.state.processing.processingState.batchProgress) {
      app.state.processing.processingState.batchProgress.done = true;
      app.state.processing.processingState.batchProgress.currentSampleId = null;
    }
    app.commands.renderWorkspace();
  }

  async function handleReprocessSingle(sampleId) {
    app.state.processing.processingState.singleRunningSampleIds.add(sampleId);
    app.commands.renderWorkspace();
    try {
      if (typeof app.api.processSingle === "function")
        await app.api.processSingle(sampleId);
      if (typeof app.commands.handleRefresh === "function")
        await app.commands.handleRefresh();
    } catch (err) {
      console.error("[processing] Reprocess failed:", err);
      app.commands.showImportToast(
        `Re-process failed: ${err.message}`,
        "error",
        { durationMs: 4500 },
      );
    } finally {
      app.state.processing.processingState.singleRunningSampleIds.delete(
        sampleId,
      );
    }
    app.commands.renderWorkspace();
  }

  async function handleFlagSample(sampleId, reason) {
    try {
      if (typeof app.api.flagSample === "function")
        await app.api.flagSample(sampleId, reason || "Manual flag");
      if (typeof app.commands.handleRefresh === "function")
        await app.commands.handleRefresh();
    } catch (err) {
      console.error("[processing] Flag failed:", err);
    }
    app.commands.renderWorkspace();
  }

  async function handleUnflagSample(sampleId) {
    try {
      if (typeof app.api.unflagSample === "function")
        await app.api.unflagSample(sampleId);
      if (typeof app.commands.handleRefresh === "function")
        await app.commands.handleRefresh();
    } catch (err) {
      console.error("[processing] Unflag failed:", err);
    }
    app.commands.renderWorkspace();
    app.commands.renderProcessingDashboard();
  }

  Object.assign(app.commands, {
    getProcessingCards,
    processingCardStatus,
    renderProcessingCard,
    renderPostProcessingCard,
    hydrateSampleMeasurements,
    ensureMeasurementsThenRerender,
    renderProcessingDashboard,
    handleProcessAll,
    handleReprocessSingle,
    handleFlagSample,
    handleUnflagSample,
  });
}
