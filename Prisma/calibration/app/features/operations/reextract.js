/** Install reextract commands. */
export function installFeaturesOperationsReextract(app) {
  function showReextractSampleImagesWorkflow(operation, onComplete) {
    const overlay = document.createElement("div");
    overlay.className =
      "info-dialog-overlay maintenance-workflow-overlay reextract-workflow-overlay";
    let reviewOverlay = null;
    const state = {
      operation,
      domainMode: "complete",
      segmentationMode: "existing_coordinates",
      sampleScopeMode: "all_accepted",
      sampleIdsText: "",
      preflight: null,
      preflightExpanded: true,
      candidateSummaryExpanded: true,
      generationReport: null,
      candidateSetId: "",
      candidateSet: null,
      samples: [],
      selectedSampleId: "",
      selectedSample: null,
      reviewDialogMode: "",
      applyReport: null,
      job: null,
      jobKind: "",
      running: false,
      cancelling: false,
      busy: false,
      loading: false,
      error: "",
    };
    const parseReextractSampleIds = () => {
      const seen = new Set();
      return String(state.sampleIdsText || "")
        .split(/[\s,;]+/)
        .map((item) => item.trim())
        .filter((item) => {
          if (!item || seen.has(item)) return false;
          seen.add(item);
          return true;
        });
    };
    const scopePayload = () => ({
      domain_mode: state.domainMode,
      segmentation_mode: state.segmentationMode,
      sample_scope: (() => {
        if (state.sampleScopeMode !== "sample_ids")
          return { kind: "all_accepted" };
        const sampleIds = parseReextractSampleIds();
        if (!sampleIds.length) throw new Error("Enter at least one sample ID.");
        return { kind: "sample_ids", sample_ids: sampleIds };
      })(),
    });
    const statusLabel = (status = "") => {
      const normalized = String(status || "");
      if (normalized === "ready_changed" || normalized === "ready_unchanged")
        return "Ready";
      return normalized.replace(/_/g, " ");
    };
    const reextractWorkflowDescription =
      "Re-extract color data from samples which have already been successfully processed and accepted. Re-extracted data will not replace the existing data until the user accepts the re-extracted results.";
    const reextractDomainLabel = (value = state.domainMode) => {
      if (value === "complete") return "Complete";
      if (value === "transmission_only") return "Transmission only";
      if (value === "appearance_only") return "Appearance only";
      return String(value || "").replace(/_/g, " ") || "Complete";
    };
    const reextractSegmentationLabel = (value = state.segmentationMode) => {
      if (value === "existing_coordinates") return "Use accepted coordinates";
      if (value === "redetect_from_scratch") return "Re-detect strip";
      return (
        String(value || "").replace(/_/g, " ") || "Use accepted coordinates"
      );
    };
    const reextractSummaryRowsHtml = (summary = {}) => {
      const automated = Number(summary.targets || 0);
      const manual = Number(summary.manual_required || 0);
      const blocked =
        Number(summary.blocked || 0) +
        Number(summary.unsupported_provenance || 0);
      const total = automated + manual + blocked;
      const row = (label, value, extraClass = "") => `
        <div class="maintenance-summary-row ${extraClass}">
          <span>${app.commands.escapeHtml(label)}</span>
          <strong>${app.commands.escapeHtml(String(value ?? ""))}</strong>
        </div>
      `;
      const rows = [
        ["Samples to Re-extract", total, ""],
        ["Automated", automated, "is-child"],
        ["Manual", manual, "is-child"],
        ["Blocked", blocked, "is-child"],
        [
          "Data",
          reextractDomainLabel(summary.domain_mode || state.domainMode),
          "",
        ],
        [
          "Coordinates",
          reextractSegmentationLabel(
            summary.segmentation_mode || state.segmentationMode,
          ),
          "",
        ],
      ];
      return `<div class="maintenance-summary-grid">${rows.map(([label, value, extraClass]) => row(label, value, extraClass)).join("")}</div>`;
    };
    const reextractManualNoticeHtml = (items = []) => {
      const samples = (items || [])
        .filter((item) => item?.category === "manual_required")
        .map((item) => String(item.target || item.sample_id || "").trim())
        .filter(Boolean);
      if (!samples.length) return "";
      return `
        <div class="reextract-manual-notice">
          The following samples will require manual re-processing: ${samples.map((sampleId) => `<span class="mono">${app.commands.escapeHtml(sampleId)}</span>`).join(", ")}.
        </div>
      `;
    };
    const reextractManualFindings = (preflight = {}) =>
      (preflight.blocked || []).filter(
        (item) => item?.category === "manual_required",
      );
    const reextractBlockedFindings = (preflight = {}) =>
      (preflight.blocked || []).filter(
        (item) => item?.category !== "manual_required",
      );
    const reextractCandidateSummaryHtml = (summary = {}) => {
      const automated = Number(summary.targets || 0);
      const changed = Number(summary.ready_changed || 0);
      const unchanged = Number(summary.ready_unchanged || 0);
      const manual = Number(summary.manual_required || 0);
      const failed = Number(summary.failed || 0);
      const blocked = Number(summary.blocked || 0);
      const total = automated + manual + failed + blocked;
      const row = (label, value, extraClass = "") => `
        <div class="maintenance-summary-row ${extraClass}">
          <span>${app.commands.escapeHtml(label)}</span>
          <strong>${app.commands.escapeHtml(String(value ?? ""))}</strong>
        </div>
      `;
      return `
        <div class="maintenance-summary-grid">
          ${row("Sample Count", total)}
          ${row("Automated", automated)}
          ${row("Changed", changed, "is-child")}
          ${row("Unchanged", unchanged, "is-child")}
          ${row("Manual (pending)", manual)}
          ${row("Failed", failed)}
          ${row("Blocked", blocked)}
        </div>
      `;
    };
    const workflowSectionCap = (
      label,
      expanded,
      toggleId,
      toggleEnabled = true,
    ) => `
      <div class="drawer-module-cap maintenance-collapsible-cap">
        <span class="sidebar-label">${app.commands.escapeHtml(label)}</span>
        ${
          toggleEnabled
            ? `
          <div class="drawer-module-cap-actions">
            <button class="drawer-utility-button" type="button" id="${app.commands._escAttr(toggleId)}" aria-expanded="${expanded ? "true" : "false"}">
              ${expanded ? "Hide" : "Show"}
            </button>
          </div>
        `
            : ""
        }
      </div>
    `;
    const readySamples = () =>
      state.samples.filter(
        (sample) =>
          sample.status === "ready_changed" ||
          sample.status === "ready_unchanged",
      );
    const isReviewableCandidate = (sample) =>
      sample?.status === "ready_changed" ||
      sample?.status === "ready_unchanged";
    const candidateDecision = (sample) =>
      sample?.review?.decision ||
      (isReviewableCandidate(sample) ? "pending" : "skip");
    const candidateDecisionMeta = (sample) => {
      if (!isReviewableCandidate(sample)) {
        if (sample?.status === "manual_required")
          return { label: "Manual", cls: "flagged" };
        if (sample?.status === "failed")
          return { label: "Failed", cls: "failed" };
        if (sample?.status === "blocked")
          return { label: "Blocked", cls: "failed" };
        if (sample?.status === "stale") return { label: "Stale", cls: "stale" };
        return { label: "Not applicable", cls: "none" };
      }
      const decision = candidateDecision(sample);
      if (decision === "save") return { label: "Save", cls: "accepted" };
      if (decision === "skip") return { label: "Skip", cls: "planned" };
      return { label: "Pending", cls: "pending" };
    };
    const candidateDecisionPill = (sample) => {
      const meta = candidateDecisionMeta(sample);
      return `<span class="status-pill ${app.commands._escAttr(meta.cls)}">${app.commands.escapeHtml(meta.label)}</span>`;
    };
    const savedReadySamples = () =>
      readySamples().filter((sample) => candidateDecision(sample) === "save");
    const pendingReadySamples = () =>
      readySamples().filter(
        (sample) => candidateDecision(sample) === "pending",
      );
    const readiness = () => state.candidateSet?.readiness || {};
    const hasManualPending = () =>
      Number(readiness().manual_pending_count || 0) > 0 ||
      state.samples.some((sample) => sample.status === "manual_required");
    const finalReviewReady = () =>
      Boolean(readiness().final_review_ready) && !hasManualPending();
    const saveReady = () =>
      Boolean(readiness().save_ready) && !pendingReadySamples().length;
    const reextractApplyResultMessage = (status) => {
      const normalized = String(status || "");
      if (normalized === "completed") return "Saved results applied.";
      if (normalized === "partial")
        return "Some saved results could not be applied.";
      if (normalized === "cancelled") return "Save cancelled.";
      if (normalized === "failed") return "Save failed.";
      return `Save ${normalized || "finished"}.`;
    };
    const selectedSample = () =>
      state.selectedSample ||
      state.samples.find(
        (sample) => sample.sample_id === state.selectedSampleId,
      ) ||
      null;
    const jobIsActive = (job = state.job) =>
      ["queued", "running", "cancelling"].includes(String(job?.status || ""));
    const terminalJobStatus = (job) =>
      ["succeeded", "failed", "cancelled"].includes(String(job?.status || ""));
    const clearPreflightState = () => {
      state.preflight = null;
      state.preflightExpanded = true;
      state.candidateSummaryExpanded = true;
      state.generationReport = null;
      state.applyReport = null;
      state.error = "";
    };
    const candidateArtifactImg = (sample, kind, label, extraClass = "") => {
      if (!sample?.artifacts?.[kind] || !state.candidateSetId) return "";
      const version = encodeURIComponent(
        sample.created_at || sample.applied_at || sample.status || "",
      );
      const src = `${app.api.reextractCandidateArtifactUrl(state.candidateSetId, sample.sample_id, kind)}${version ? `?v=${version}` : ""}`;
      return `
        <figure class="reextract-artifact ${extraClass}">
          <img src="${src}" alt="${app.commands._escAttr(label)}">
          <figcaption>${app.commands.escapeHtml(label)}</figcaption>
        </figure>
      `;
    };
    const candidateSwatches = (sample) => {
      return [
        ...(sample?.replacement_extraction_result?.measurements?.swatches ||
          []),
      ].sort((a, b) => {
        const ai = Number(a.swatch_index ?? Number.MAX_SAFE_INTEGER);
        const bi = Number(b.swatch_index ?? Number.MAX_SAFE_INTEGER);
        if (ai !== bi) return ai - bi;
        return (
          Number(a.nominal_thickness_mm ?? 0) -
          Number(b.nominal_thickness_mm ?? 0)
        );
      });
    };
    const candidateChipHex = (sample, swatch, domain) => {
      if (domain === "appearance") {
        const indexed =
          sample?.colors_by_swatch_index?.[String(swatch.swatch_index)];
        if (Array.isArray(indexed) && indexed.length >= 3) {
          return app.commands.rgbValuesToHex(
            indexed[0],
            indexed[1],
            indexed[2],
          );
        }
        const appearance = swatch?.appearance || null;
        return appearance
          ? app.commands.rgbValuesToHex(
              appearance.jpeg_r,
              appearance.jpeg_g,
              appearance.jpeg_b,
            )
          : "";
      }
      return swatch?.display?.hex || "";
    };
    const candidateStripRatio = (sample) => {
      const geometry = sample?.diagnostics?.visual_geometry || {};
      const width = Number(geometry.strip_width);
      const height = Number(geometry.strip_height);
      return width > 0 && height > 0 ? width / height : 4;
    };
    const candidateMockStripHtml = (sample, domain, renderKey) => {
      const swatches = candidateSwatches(sample);
      const n = Math.max(
        swatches.length || Number(sample?.swatch_count || 0) || 0,
        1,
      );
      const geometry = sample?.diagnostics?.visual_geometry || {};
      const boundaries = Array.isArray(geometry.boundaries)
        ? geometry.boundaries.map(Number).filter(Number.isFinite)
        : [];
      const columnWidths =
        boundaries.length >= n + 1
          ? boundaries
              .slice(0, n)
              .map((value, index) =>
                Math.max(1, Number(boundaries[index + 1]) - Number(value)),
              )
          : [];
      const gridTemplate =
        columnWidths.length === n
          ? columnWidths.map((value) => `${value}fr`).join(" ")
          : `repeat(${n}, minmax(0, 1fr))`;
      const tiles = swatches.length
        ? swatches
            .map((sw, index) => {
              const hex = candidateChipHex(sample, sw, domain);
              return `<div class="sample-render-swatch${index > 0 ? " has-divider" : ""}${hex ? "" : " is-missing"}" style="background:${hex || "#d8d5cc"}"></div>`;
            })
            .join("")
        : `<div class="sample-render-swatch is-missing"></div>`;
      return `
        <div class="sample-strip-frame reextract-strip-frame" data-reextract-strip-render-frame="${app.commands._escAttr(renderKey)}" style="--strip-ratio:${candidateStripRatio(sample)}">
          <div class="sample-render-stage sample-render-stage-sync"
               data-reextract-strip-render="${app.commands._escAttr(renderKey)}"
               data-inner-x="${app.commands._escAttr(geometry.inner_x ?? "")}"
               data-inner-y="${app.commands._escAttr(geometry.inner_y ?? "")}"
               data-inner-w="${app.commands._escAttr(geometry.inner_w ?? "")}"
               data-inner-h="${app.commands._escAttr(geometry.inner_h ?? "")}"
               data-strip-w="${app.commands._escAttr(geometry.strip_width ?? "")}"
               data-strip-h="${app.commands._escAttr(geometry.strip_height ?? "")}"
               data-swatches="${n}">
            <div class="sample-render-shell" style="grid-template-columns:${app.commands._escAttr(gridTemplate)}">
              ${tiles}
            </div>
          </div>
        </div>
      `;
    };
    const candidateStripReviewRow = (sample, kind, label, domain) => {
      if (!sample?.artifacts?.[kind] || !state.candidateSetId) return "";
      const renderKey = `${sample.sample_id}:${kind}`;
      const version = encodeURIComponent(
        sample.created_at || sample.applied_at || sample.status || "",
      );
      const src = `${app.api.reextractCandidateArtifactUrl(state.candidateSetId, sample.sample_id, kind)}${version ? `?v=${version}` : ""}`;
      return `
        <div class="sample-strip-row reextract-strip-review-row">
          <div class="sample-strip-label-bubble">${app.commands.escapeHtml(label).replace(/\s+/g, "<br>")}</div>
          <div class="sample-strip-row-content" style="--strip-ratio:${candidateStripRatio(sample)}">
            <div class="sample-strip-frame reextract-strip-frame" data-reextract-strip-source-frame="${app.commands._escAttr(renderKey)}" style="--strip-ratio:${candidateStripRatio(sample)}">
              <button class="drawer-thumb-button" type="button" data-lightbox-src="${app.commands._escAttr(src)}" data-lightbox-title="${app.commands._escAttr(`${sample.sample_id} ${label}`)}">
                <img class="drawer-thumb drawer-thumb-strip" src="${app.commands._escAttr(src)}" alt="${app.commands._escAttr(`${sample.sample_id} ${label}`)}" data-reextract-strip-source="${app.commands._escAttr(renderKey)}">
              </button>
            </div>
            ${candidateMockStripHtml(sample, domain, renderKey)}
          </div>
        </div>
      `;
    };
    const reextractCoordinateSourceLabel = (coordinateSpace = "") => {
      const normalized = String(coordinateSpace || "");
      if (normalized.includes("manual_full_image"))
        return "Accepted manual coordinates";
      if (normalized.includes("automatic_full_image"))
        return "Accepted automatic coordinates";
      if (state.segmentationMode === "redetect_from_scratch")
        return "Re-detected strip";
      return "Accepted coordinates";
    };
    const reextractAlignmentLabel = (strategy = "") => {
      const normalized = String(strategy || "");
      if (!normalized) return "";
      if (normalized.includes("legacy_resize_fallback"))
        return "Matched blank to source with fallback";
      if (normalized.includes("homography")) return "Matched blank to source";
      return normalized.replace(/_/g, " ");
    };
    const candidateReviewSummaryHtml = (sample) => {
      const diagnostics = sample?.diagnostics || {};
      const rows = [];
      const addRow = (label, value) => {
        if (value === null || value === undefined || value === "") return;
        rows.push([label, value]);
      };
      addRow(
        "Data",
        reextractDomainLabel(
          diagnostics.domain_mode || sample?.domain_mode || state.domainMode,
        ),
      );
      addRow(
        "Coordinates",
        reextractCoordinateSourceLabel(diagnostics.coordinate_space),
      );
      addRow(
        "Alignment",
        reextractAlignmentLabel(diagnostics.registration_strategy),
      );
      if (
        diagnostics.blank_orientation_rotations !== null &&
        diagnostics.blank_orientation_rotations !== undefined
      ) {
        const rotations = Number(diagnostics.blank_orientation_rotations || 0);
        addRow(
          "Blank rotation",
          rotations ? `${rotations * 90} degrees` : "None",
        );
      }
      if (
        diagnostics.strip_orientation_flipped !== null &&
        diagnostics.strip_orientation_flipped !== undefined
      ) {
        addRow(
          "Strip order",
          diagnostics.strip_orientation_flipped
            ? "Flipped to match swatches"
            : "Kept as captured",
        );
      }
      if (
        diagnostics.appearance_orientation_flipped !== null &&
        diagnostics.appearance_orientation_flipped !== undefined
      ) {
        addRow(
          "Appearance order",
          diagnostics.appearance_orientation_flipped
            ? "Flipped to match swatches"
            : "Kept as captured",
        );
      }
      if (
        Array.isArray(diagnostics.missing_required_artifacts) &&
        diagnostics.missing_required_artifacts.length
      ) {
        addRow(
          "Missing review images",
          diagnostics.missing_required_artifacts.join(", "),
        );
      }
      if (diagnostics.appearance_error) {
        addRow("Appearance", diagnostics.appearance_error);
      }
      if (!rows.length) return "";
      return `
        <div class="maintenance-summary-grid reextract-review-summary" aria-label="Extraction summary">
          ${rows
            .map(
              ([label, value]) => `
            <div class="maintenance-summary-row">
              <span>${app.commands.escapeHtml(label)}</span>
              <strong>${app.commands.escapeHtml(String(value))}</strong>
            </div>
          `,
            )
            .join("")}
        </div>
      `;
    };
    const applyReextractStripGeometry = (img) => {
      const key = img?.dataset?.reextractStripSource;
      if (
        !key ||
        !img.naturalWidth ||
        !img.naturalHeight ||
        !reviewOverlay?.isConnected
      )
        return;
      const escapedKey = CSS.escape(key);
      const sourceFrame = reviewOverlay.querySelector(
        `[data-reextract-strip-source-frame="${escapedKey}"]`,
      );
      const renderFrames = Array.from(
        reviewOverlay.querySelectorAll(
          `[data-reextract-strip-render-frame="${escapedKey}"]`,
        ),
      );
      const renderStages = Array.from(
        reviewOverlay.querySelectorAll(
          `[data-reextract-strip-render="${escapedKey}"]`,
        ),
      );
      if (!sourceFrame || !renderFrames.length || !renderStages.length) return;

      const sw = Number(img.naturalWidth);
      const sh = Number(img.naturalHeight);
      const metricStage = renderStages[0];
      let innerX = Number(metricStage.dataset.innerX);
      let innerY = Number(metricStage.dataset.innerY);
      let innerW = Number(metricStage.dataset.innerW);
      let innerH = Number(metricStage.dataset.innerH);
      if (
        ![innerX, innerY, innerW, innerH].every(Number.isFinite) ||
        innerW <= 0 ||
        innerH <= 0
      ) {
        const n = Number(metricStage.dataset.swatches || 8);
        const borderMm = 3;
        const stepWMm = 12;
        const stepHMm = 20;
        const deskewPad = 6;
        const totalWmm = 2 * borderMm + n * stepWMm;
        const plasticWPx = Math.max(1, sw - 2 * deskewPad);
        const pxPerMm = plasticWPx / totalWmm;
        innerX = Math.round(deskewPad + borderMm * pxPerMm);
        innerY = Math.round(deskewPad + borderMm * pxPerMm);
        innerW = Math.round(n * stepWMm * pxPerMm);
        innerH = Math.round(stepHMm * pxPerMm * 0.95);
      } else {
        const sourceW = Number(metricStage.dataset.stripW);
        const sourceH = Number(metricStage.dataset.stripH);
        if (
          Number.isFinite(sourceW) &&
          sourceW > 0 &&
          Number.isFinite(sourceH) &&
          sourceH > 0
        ) {
          const sx = sw / sourceW;
          const sy = sh / sourceH;
          innerX *= sx;
          innerW *= sx;
          innerY *= sy;
          innerH *= sy;
        }
      }

      renderStages.forEach((stage) => {
        stage.style.setProperty("--render-left", `${(innerX / sw) * 100}%`);
        stage.style.setProperty("--render-top", `${(innerY / sh) * 100}%`);
        stage.style.setProperty("--render-width", `${(innerW / sw) * 100}%`);
        stage.style.setProperty("--render-height", `${(innerH / sh) * 100}%`);
      });
    };
    const bindReextractStripGeometry = () => {
      if (!reviewOverlay?.isConnected) return;
      reviewOverlay
        .querySelectorAll("img[data-reextract-strip-source]")
        .forEach((img) => {
          if (img.complete && img.naturalWidth) {
            applyReextractStripGeometry(img);
          } else {
            img.addEventListener(
              "load",
              () => applyReextractStripGeometry(img),
              { once: true },
            );
          }
        });
    };
    const renderCandidateList = () => {
      if (!state.candidateSetId)
        return `<div class="mf-placeholder">No extracted images yet.</div>`;
      if (state.loading)
        return `<div class="maintenance-loading">Loading extracted images...</div>`;
      if (!state.samples.length)
        return `<div class="mf-placeholder">No extracted images in this run.</div>`;
      return `
        <div class="reextract-list-actions">
          <button class="ghost-button xs" type="button" id="reextractSaveAll" ${state.busy || state.running ? "disabled" : ""}>Save All</button>
          <button class="ghost-button xs" type="button" id="reextractSkipAll" ${state.busy || state.running ? "disabled" : ""}>Skip All</button>
        </div>
        <div class="reextract-candidate-list">
          ${state.samples
            .map((sample) => {
              const active = sample.sample_id === state.selectedSampleId;
              return `
              <button class="reextract-candidate-row ${active ? "is-active" : ""}" type="button" data-reextract-sample="${app.commands._escAttr(sample.sample_id)}">
                <span class="mono">${app.commands.escapeHtml(sample.sample_id)}</span>
                ${candidateDecisionPill(sample)}
              </button>
            `;
            })
            .join("")}
        </div>
      `;
    };
    const renderCandidateDetail = () => {
      const sample = selectedSample();
      if (!sample) return `<div class="mf-placeholder">Select a sample.</div>`;
      const canReview = isReviewableCandidate(sample);
      const canRetry = sample.status === "failed" || sample.status === "stale";
      const canManual = state.segmentationMode === "redetect_from_scratch";
      return `
        <div class="reextract-detail">
          <div class="reextract-detail-head">
            <div>
              <strong class="mono">${app.commands.escapeHtml(sample.sample_id)}</strong>
              <span class="reextract-status is-${app.commands._escAttr(sample.status || "unknown")}">${app.commands.escapeHtml(statusLabel(sample.status))}</span>
            </div>
            <div class="reextract-detail-actions">
              ${
                canReview
                  ? `
                <button class="ghost-button xs ${candidateDecision(sample) === "save" ? "is-active" : ""}" type="button" data-reextract-decision="save">Save</button>
                <button class="ghost-button xs ${candidateDecision(sample) === "skip" ? "is-active" : ""}" type="button" data-reextract-decision="skip">Skip</button>
              `
                  : ""
              }
              ${canRetry ? `<button class="ghost-button xs" type="button" id="reextractRetryCandidate">Retry</button>` : ""}
              ${canManual ? `<button class="ghost-button xs" type="button" id="reextractManualCandidate">Manual Corners</button>` : ""}
            </div>
          </div>
          ${sample.error ? `<div class="backup-restore-message is-error">${app.commands.escapeHtml(sample.error)}</div>` : ""}
          <div class="reextract-artifact-grid">
            ${candidateArtifactImg(sample, "source", "Source Strip Boundary")}
            ${candidateArtifactImg(sample, "blank", "Blank Strip Boundary")}
          </div>
          <div class="reextract-strip-comparison">
            ${candidateStripReviewRow(sample, sample.artifacts?.transmission_roi ? "transmission_roi" : "strip", "Extracted Transmission", "transmission")}
            ${candidateStripReviewRow(sample, "appearance", "Extracted Appearance", "appearance")}
          </div>
          ${candidateReviewSummaryHtml(sample)}
        </div>
      `;
    };
    async function selectAdjacenterviewSample(delta) {
      if (state.busy || state.running || state.loading || !state.samples.length)
        return;
      const currentIndex = state.samples.findIndex(
        (sample) => sample.sample_id === state.selectedSampleId,
      );
      const baseIndex = currentIndex >= 0 ? currentIndex : 0;
      const nextIndex = Math.max(
        0,
        Math.min(state.samples.length - 1, baseIndex + delta),
      );
      const nextSample = state.samples[nextIndex];
      if (!nextSample || nextSample.sample_id === state.selectedSampleId)
        return;
      state.selectedSampleId = nextSample.sample_id;
      state.selectedSample = await app.api.fetchReextractCandidateSample(
        state.candidateSetId,
        state.selectedSampleId,
      );
      renderReviewDialog();
    }
    const renderControls = (disabled) => {
      const modeOption = (group, value, label, active, disabledAttr = "") => `
        <label class="maintenance-mode-option ${active ? "is-active" : ""} ${disabledAttr ? "is-disabled" : ""}">
          <input type="radio" name="${group}" value="${app.commands._escAttr(value)}" ${active ? "checked" : ""} ${disabled || disabledAttr ? "disabled" : ""}>
          <span>${app.commands.escapeHtml(label)}</span>
        </label>
      `;
      const redetectPartial =
        state.segmentationMode === "redetect_from_scratch";
      const sampleIds = parseReextractSampleIds();
      return `
        <div class="reextract-control-grid">
          <fieldset class="maintenance-mode-fieldset" ${disabled ? "disabled" : ""}>
            <legend>Data</legend>
            <div class="maintenance-mode-options">
              ${modeOption("reextractDomainMode", "complete", "Complete", state.domainMode === "complete")}
              ${modeOption("reextractDomainMode", "transmission_only", "Transmission Only", state.domainMode === "transmission_only", redetectPartial ? "disabled" : "")}
              ${modeOption("reextractDomainMode", "appearance_only", "Appearance Only", state.domainMode === "appearance_only", redetectPartial ? "disabled" : "")}
            </div>
          </fieldset>
          <fieldset class="maintenance-mode-fieldset" ${disabled ? "disabled" : ""}>
            <legend>Coordinates</legend>
            <div class="maintenance-mode-options">
              ${modeOption("reextractSegmentationMode", "existing_coordinates", "Use Accepted Coordinates", state.segmentationMode === "existing_coordinates")}
              ${modeOption("reextractSegmentationMode", "redetect_from_scratch", "Re-detect Strip", state.segmentationMode === "redetect_from_scratch")}
            </div>
          </fieldset>
          <fieldset class="maintenance-mode-fieldset reextract-sample-fieldset" ${disabled ? "disabled" : ""}>
            <legend>Samples</legend>
            <div class="maintenance-mode-options">
              ${modeOption("reextractSampleScope", "all_accepted", "All Accepted", state.sampleScopeMode !== "sample_ids")}
              ${modeOption("reextractSampleScope", "sample_ids", "Selected Samples", state.sampleScopeMode === "sample_ids")}
            </div>
            ${
              state.sampleScopeMode === "sample_ids"
                ? `
              <textarea
                id="reextractSampleIds"
                class="reextract-sample-scope-input"
                rows="3"
                placeholder="exp-055, exp-056, exp-165"
                ${disabled ? "disabled" : ""}
              >${app.commands.escapeHtml(state.sampleIdsText || "")}</textarea>
            `
                : ""
            }
            <p class="small-copy" data-reextract-sample-count>${state.sampleScopeMode === "sample_ids" ? `${sampleIds.length} selected` : "All accepted processed samples."}</p>
          </fieldset>
        </div>
      `;
    };
    async function loadSamples(selectSampleId = "") {
      if (!state.candidateSetId) return;
      state.loading = true;
      renderAll();
      try {
        const [candidateSet, payload] = await Promise.all([
          app.api.fetchReextractCandidateSet(state.candidateSetId),
          app.api.fetchReextractCandidateSamples(state.candidateSetId),
        ]);
        state.candidateSet = candidateSet || null;
        state.samples = payload?.samples || [];
        const sampleIds = new Set(
          state.samples.map((sample) => sample.sample_id),
        );
        const preferredSampleId =
          selectSampleId || state.selectedSampleId || "";
        state.selectedSampleId = sampleIds.has(preferredSampleId)
          ? preferredSampleId
          : state.samples[0]?.sample_id || "";
        state.selectedSample = state.selectedSampleId
          ? await app.api.fetchReextractCandidateSample(
              state.candidateSetId,
              state.selectedSampleId,
            )
          : null;
      } finally {
        state.loading = false;
        renderAll();
      }
    }
    async function withBusy(fn) {
      if (state.busy) return;
      state.busy = true;
      state.error = "";
      renderAll();
      try {
        await fn();
      } catch (err) {
        state.error = err.message || String(err || "Re-extraction failed");
      } finally {
        state.busy = false;
        renderAll();
      }
    }
    async function runReextractJob(kind, startFn) {
      if (state.busy || state.running) return;
      state.busy = true;
      state.running = true;
      state.cancelling = false;
      state.job = null;
      state.jobKind = kind;
      state.error = "";
      renderAll();
      try {
        const started = await startFn();
        const jobId = started?.job_id;
        if (!jobId)
          throw new Error("Re-extraction job did not return a job id.");
        state.job = started;
        state.cancelling =
          started.status === "cancelling" || Boolean(started.cancel_requested);
        renderAll();
        const nextJob = await app.commands.pollJobUntilTerminal({
          jobId,
          fetchStatus: () => app.api.fetchReextractJobStatus(jobId),
          isTerminal: terminalJobStatus,
          shouldContinue: () =>
            overlay.isConnected && state.running && state.jobKind === kind,
          intervalMs: 700,
          onStatus: (job) => {
            state.job = job;
            state.cancelling =
              job.status === "cancelling" || Boolean(job.cancel_requested);
            renderAll();
          },
          onTransientError: () => {
            state.job = {
              ...(state.job || {}),
              job_id: jobId,
              message:
                "Connection interrupted; retrying re-extraction status...",
            };
            renderAll();
          },
        });
        if (!nextJob) return;
        await handleTerminalReextractJob(kind, nextJob);
      } catch (err) {
        const recovered = await recoverReextractCandidateSetAfterJobLoss(
          kind,
        ).catch(() => false);
        state.error = recovered
          ? "The job status was lost, but the extracted images were recovered."
          : err.message || String(err || "Re-extraction failed");
        state.busy = false;
        state.running = false;
        state.cancelling = false;
        renderAll();
      }
    }
    async function handleTerminalReextractJob(kind, job) {
      const result = job.result || {};
      const status = String(job.status || "");
      const succeeded = status === "succeeded";
      if (kind === "preflight" && succeeded) {
        state.preflight = result.preflight || null;
        state.preflightExpanded = true;
      }
      if (kind === "generate") {
        state.generationReport =
          result.report || state.generationReport || null;
        state.candidateSetId =
          result.candidate_set_id ||
          job.candidate_set_id ||
          state.generationReport?.candidate_set_id ||
          state.candidateSetId ||
          "";
        if (succeeded) {
          state.preflightExpanded = false;
          state.candidateSummaryExpanded = true;
        }
        if (state.candidateSetId) await loadSamples(state.selectedSampleId);
      }
      if (kind === "apply") {
        state.applyReport = result.report || state.applyReport || null;
        const candidateSetDeleted = Boolean(
          state.applyReport?.candidate_set_deleted,
        );
        if (succeeded) {
          state.candidateSummaryExpanded = false;
        }
        if (succeeded && candidateSetDeleted) {
          closeReviewDialog();
          state.candidateSetId = "";
          state.candidateSet = null;
          state.samples = [];
          state.selectedSample = null;
          state.selectedSampleId = "";
        } else if (state.candidateSetId) {
          await loadSamples(state.selectedSampleId);
        }
        if (succeeded) {
          await app.commands.handleRefresh({ ensureAssets: false });
          await onComplete?.();
        }
      }
      if (kind === "retry" || kind === "manual") {
        const sampleId =
          result.sample_id || job.sample_id || state.selectedSampleId;
        if (state.candidateSetId) await loadSamples(sampleId);
      }
      if (!succeeded) {
        state.error =
          job.error?.message ||
          job.message ||
          `Re-extraction ${status || "failed"}`;
      } else {
        state.error = "";
      }
      state.busy = false;
      state.running = false;
      state.cancelling = false;
      renderAll();
    }
    async function recoverReextractCandidateSetAfterJobLoss(kind) {
      const knownId =
        state.job?.candidate_set_id ||
        state.job?.progress?.candidate_set_id ||
        state.candidateSetId;
      if (knownId) {
        state.candidateSetId = knownId;
        await loadSamples(state.selectedSampleId);
        return true;
      }
      if (kind !== "generate") return false;
      const payload = await app.api.fetchReextractCandidateSets();
      const candidateSet = (payload?.candidate_sets || [])[0] || null;
      if (!candidateSet?.candidate_set_id) return false;
      state.candidateSetId = candidateSet.candidate_set_id;
      state.candidateSet = candidateSet;
      if (!state.generationReport) {
        state.generationReport = {
          status: candidateSet.status || "unknown",
          summary: candidateSet.counts_by_status || {},
        };
      }
      await loadSamples(state.selectedSampleId);
      return true;
    }
    async function cancelActiveReextractJob() {
      if (!state.job?.job_id || !jobIsActive(state.job)) return;
      const cancellationJobId = String(state.job.job_id);
      try {
        state.cancelling = true;
        const response = await app.api.cancelReextractJob(cancellationJobId);
        if (String(state.job?.job_id || "") !== cancellationJobId) return;
        app.commands.assertPolledJobIdentity(response, cancellationJobId);
        state.job = response;
        renderAll();
      } catch (err) {
        if (String(state.job?.job_id || "") !== cancellationJobId) return;
        state.cancelling =
          state.job?.status === "cancelling" ||
          Boolean(state.job?.cancel_requested);
        state.error = err.message || "Cancel request failed";
        renderAll();
      }
    }
    function confirmDeleteCandidateSetDialog() {
      return new Promise((resolve) => {
        const confirmOverlay = document.createElement("div");
        confirmOverlay.className =
          "info-dialog-overlay maintenance-clear-confirm-overlay";
        confirmOverlay.innerHTML = `
          <div class="info-dialog" role="dialog" aria-modal="true" aria-labelledby="reextractDeleteConfirmTitle">
            ${app.commands.renderDialogHeader({
              title: "Discard Results",
              titleId: "reextractDeleteConfirmTitle",
              closeButtonHtml: app.commands.renderWindowCloseButton({
                id: "reextractDeleteConfirmClose",
                className: "info-dialog-close",
              }),
            })}
            <div class="info-dialog-body">
              <div class="maintenance-detail-body">
                <p>Discard the staged re-extraction results?</p>
                <p class="small-copy">Accepted sample data will not be changed.</p>
                ${state.candidateSetId ? `<div class="backup-restore-path mono">${app.commands.escapeHtml(state.candidateSetId)}</div>` : ""}
              </div>
            </div>
            <div class="info-dialog-footer">
              <button class="delete-button small" type="button" id="reextractDeleteConfirm">Discard Results</button>
              <button class="ghost-button small" type="button" id="reextractDeleteCancel">Cancel</button>
            </div>
          </div>
        `;
        const cleanup = (confirmed) => {
          confirmOverlay.remove();
          document.removeEventListener("keydown", handleKeydown);
          resolve(Boolean(confirmed));
        };
        const handleKeydown = (event) => {
          if (event.key === "Escape") cleanup(false);
        };
        confirmOverlay
          .querySelector("#reextractDeleteConfirm")
          ?.addEventListener("click", () => cleanup(true));
        confirmOverlay
          .querySelector("#reextractDeleteCancel")
          ?.addEventListener("click", () => cleanup(false));
        confirmOverlay
          .querySelector("#reextractDeleteConfirmClose")
          ?.addEventListener("click", () => cleanup(false));
        document.addEventListener("keydown", handleKeydown);
        document.body.appendChild(confirmOverlay);
      });
    }
    function confirmSaveCandidateSetDialog() {
      return new Promise((resolve) => {
        const confirmOverlay = document.createElement("div");
        confirmOverlay.className =
          "info-dialog-overlay maintenance-clear-confirm-overlay";
        const r = readiness();
        confirmOverlay.innerHTML = `
          <div class="info-dialog" role="dialog" aria-modal="true" aria-labelledby="reextractSaveConfirmTitle">
            ${app.commands.renderDialogHeader({
              title: "Save Results",
              titleId: "reextractSaveConfirmTitle",
              closeButtonHtml: app.commands.renderWindowCloseButton({
                id: "reextractSaveConfirmClose",
                className: "info-dialog-close",
              }),
            })}
            <div class="info-dialog-body">
              <div class="maintenance-detail-body">
                <p>Save staged re-extraction results for samples marked Save?</p>
                <div class="maintenance-summary-grid">
                  <div class="maintenance-summary-row"><span>Save</span><strong>${app.commands.escapeHtml(String(r.save_count || savedReadySamples().length || 0))}</strong></div>
                  <div class="maintenance-summary-row"><span>Skip</span><strong>${app.commands.escapeHtml(String(r.skip_count || 0))}</strong></div>
                  <div class="maintenance-summary-row"><span>Failed</span><strong>${app.commands.escapeHtml(String(r.failed_count || 0))}</strong></div>
                  <div class="maintenance-summary-row"><span>Blocked</span><strong>${app.commands.escapeHtml(String(r.blocked_count || 0))}</strong></div>
                </div>
                <p class="small-copy">Skipped rows will not change accepted sample data.</p>
              </div>
            </div>
            <div class="info-dialog-footer">
              <button class="primary-button small" type="button" id="reextractSaveConfirm">Save Results</button>
              <button class="ghost-button small" type="button" id="reextractSaveCancel">Cancel</button>
            </div>
          </div>
        `;
        const cleanup = (confirmed) => {
          confirmOverlay.remove();
          document.removeEventListener("keydown", handleKeydown);
          resolve(Boolean(confirmed));
        };
        const handleKeydown = (event) => {
          if (event.key === "Escape") cleanup(false);
        };
        confirmOverlay
          .querySelector("#reextractSaveConfirm")
          ?.addEventListener("click", () => cleanup(true));
        confirmOverlay
          .querySelector("#reextractSaveCancel")
          ?.addEventListener("click", () => cleanup(false));
        confirmOverlay
          .querySelector("#reextractSaveConfirmClose")
          ?.addEventListener("click", () => cleanup(false));
        document.addEventListener("keydown", handleKeydown);
        document.body.appendChild(confirmOverlay);
      });
    }
    function candidateSetSummaryHtml() {
      if (!state.candidateSetId) return "";
      const summary = state.generationReport?.summary || {};
      const manualPending = hasManualPending();
      const actionLabel = manualPending
        ? "Process Manual Samples"
        : "Review Results";
      const actionId = manualPending
        ? "reextractOpenManualStep"
        : "reextractOpenReview";
      return `
        <section class="maintenance-workflow-section">
          ${workflowSectionCap("Extracted Images", state.candidateSummaryExpanded, "reextractToggleCandidateSummary", Boolean(state.applyReport))}
          ${
            state.candidateSummaryExpanded
              ? `
            <div class="maintenance-detail-body">
              <div class="backup-restore-message is-success">Images extracted. Review them before saving staged results.</div>
              <div class="backup-restore-path mono">${app.commands.escapeHtml(String(state.candidateSetId))}</div>
              ${reextractCandidateSummaryHtml(summary)}
              <div class="backup-workflow-actions">
                <button class="primary-button small" type="button" id="${actionId}" ${state.busy || state.running ? "disabled" : ""}>${actionLabel}</button>
              </div>
            </div>
          `
              : ""
          }
        </section>
      `;
    }
    function closeReviewDialog() {
      if (!reviewOverlay) return;
      reviewOverlay.remove();
      reviewOverlay = null;
      state.reviewDialogMode = "";
    }
    const manualPendingRows = () =>
      state.samples.filter((sample) => sample.status === "manual_required");
    function renderManualStepDialog() {
      if (state.reviewDialogMode !== "manual") return;
      if (!reviewOverlay?.isConnected) return;
      const rows = manualPendingRows();
      const disableClose = state.running;
      reviewOverlay.innerHTML = `
        <div class="info-dialog maintenance-workflow-dialog reextract-review-dialog" role="dialog" aria-modal="true" aria-labelledby="reextractManualTitle">
          ${app.commands.renderDialogHeader({
            title: "Process Manual Samples",
            titleId: "reextractManualTitle",
            closeButtonHtml: app.commands.renderWindowCloseButton({
              id: "reextractManualClose",
              className: "info-dialog-close",
              disabled: disableClose,
            }),
          })}
          <div class="info-dialog-body reextract-review-dialog-body">
            ${state.error ? `<div class="backup-restore-message is-error">${app.commands.escapeHtml(state.error)}</div>` : ""}
            <section class="maintenance-workflow-section reextract-review-section">
              <div class="drawer-module-cap">
                <span class="sidebar-label">Samples</span>
                <span>${rows.length} manual</span>
              </div>
              <div class="maintenance-detail-body">
                <p class="small-copy">These samples need manual corners before the re-extraction results can be reviewed.</p>
                <div class="reextract-candidate-list">
                  ${
                    rows.length
                      ? rows
                          .map(
                            (sample) => `
                    <button class="reextract-candidate-row" type="button" data-reextract-manual-sample="${app.commands._escAttr(sample.sample_id)}">
                      <span class="mono">${app.commands.escapeHtml(sample.sample_id)}</span>
                      ${candidateDecisionPill(sample)}
                    </button>
                  `,
                          )
                          .join("")
                      : `<div class="mf-placeholder">No manual samples remain.</div>`
                  }
                </div>
              </div>
            </section>
          </div>
          <div class="info-dialog-footer backup-workflow-footer">
            <button class="ghost-button small" type="button" id="reextractManualCloseFooter" ${disableClose ? "disabled" : ""}>Close</button>
            <button class="primary-button small" type="button" id="reextractManualReviewResults" ${!finalReviewReady() ? "disabled" : ""}>Review Results</button>
          </div>
        </div>
      `;
      bindManualStepDialog();
    }
    async function openManualStepDialog() {
      if (!state.candidateSetId) return;
      if (!state.samples.length && !state.loading) {
        await loadSamples(state.selectedSampleId);
      }
      if (!reviewOverlay?.isConnected) {
        reviewOverlay = document.createElement("div");
        reviewOverlay.className =
          "info-dialog-overlay maintenance-workflow-overlay reextract-review-overlay";
        document.body.appendChild(reviewOverlay);
      }
      state.reviewDialogMode = "manual";
      renderManualStepDialog();
    }
    function bindManualStepDialog() {
      if (!reviewOverlay?.isConnected) return;
      reviewOverlay
        .querySelector("#reextractManualClose")
        ?.addEventListener("click", closeReviewDialog);
      reviewOverlay
        .querySelector("#reextractManualCloseFooter")
        ?.addEventListener("click", closeReviewDialog);
      reviewOverlay
        .querySelector("#reextractManualReviewResults")
        ?.addEventListener("click", () => {
          void openCandidateReviewDialog();
        });
      reviewOverlay
        .querySelectorAll("[data-reextract-manual-sample]")
        .forEach((button) => {
          button.addEventListener("click", () => {
            const sampleId = button.dataset.reextractManualSample || "";
            if (!sampleId) return;
            app.commands.openManualProcessing([sampleId], {
              context: "reextract-candidate",
              candidateSetId: state.candidateSetId,
              onCandidateComplete: async () => {
                await loadSamples(sampleId);
                renderManualStepDialog();
              },
            });
          });
        });
    }
    function renderReviewDialog() {
      if (state.reviewDialogMode !== "review") return;
      if (!reviewOverlay?.isConnected) return;
      const activeJob = jobIsActive(state.job);
      const disableClose = state.running;
      const canSave =
        state.candidateSetId &&
        saveReady() &&
        !state.busy &&
        !state.running &&
        !state.loading;
      const pendingCount = Number(
        readiness().pending_decision_count || pendingReadySamples().length || 0,
      );
      const applyStatus = String(state.applyReport?.status || "");
      const applyMessage = reextractApplyResultMessage(applyStatus);
      reviewOverlay.innerHTML = `
        <div class="info-dialog maintenance-workflow-dialog reextract-review-dialog" role="dialog" aria-modal="true" aria-labelledby="reextractReviewTitle">
          ${app.commands.renderDialogHeader({
            title: "Review Extracted Images",
            titleId: "reextractReviewTitle",
            closeButtonHtml: app.commands.renderWindowCloseButton({
              id: "reextractReviewClose",
              className: "info-dialog-close",
              disabled: disableClose,
            }),
          })}
          <div class="info-dialog-body reextract-review-dialog-body">
            ${state.error ? `<div class="backup-restore-message is-error">${app.commands.escapeHtml(state.error)}</div>` : ""}
            ${pendingCount ? `<div class="backup-restore-message is-warning">Choose Save or Skip for ${pendingCount} pending sample${pendingCount === 1 ? "" : "s"} before saving results.</div>` : ""}
            ${app.commands.reextractProgressHtml(state.job, activeJob, "Running re-extraction")}
            ${
              state.applyReport
                ? `
              <section class="maintenance-workflow-section">
                <div class="drawer-module-cap"><span class="sidebar-label">Apply Result</span></div>
                <div class="maintenance-detail-body">
                  <div class="backup-restore-message ${applyStatus === "completed" ? "is-success" : "is-warning"}">${app.commands.escapeHtml(applyMessage)}</div>
                  ${app.commands.maintenanceSummaryHtml(state.applyReport.summary || {})}
                  ${app.commands.maintenanceFindingsHtml(state.applyReport)}
                </div>
              </section>
            `
                : ""
            }
            <section class="maintenance-workflow-section reextract-review-section">
              <div class="reextract-review-body">
                <aside class="reextract-review-list-panel">
                  <div class="drawer-module-cap">
                    <span class="sidebar-label">Samples</span>
                    <span>${app.commands.escapeHtml(String(state.samples.length))}</span>
                  </div>
                  <div class="reextract-review-panel-body">
                    ${renderCandidateList()}
                  </div>
                </aside>
                <div class="reextract-review-detail-panel">
                  <div class="drawer-module-cap">
                    <span class="sidebar-label">Sample Detail</span>
                    <span>${app.commands.escapeHtml(String(state.candidateSetId || ""))}</span>
                  </div>
                  <div class="reextract-review-panel-body">
                    ${renderCandidateDetail()}
                  </div>
                </div>
              </div>
            </section>
          </div>
          <div class="info-dialog-footer backup-workflow-footer">
            <button class="delete-button small" type="button" id="reextractReviewDeleteSet" ${state.busy || state.running ? "disabled" : ""}>Discard Results</button>
            ${activeJob ? `<button class="ghost-button small" type="button" id="reextractCancelReviewJob" ${state.cancelling ? "disabled" : ""}>${state.cancelling ? "Cancelling..." : "Cancel"}</button>` : ""}
            <span class="dialog-footer-spacer"></span>
            <button class="ghost-button small" type="button" id="reextractReviewCloseFooter" ${disableClose ? "disabled" : ""}>Close</button>
            <button class="primary-button small" type="button" id="reextractReviewApply" ${!canSave ? "disabled" : ""}>Save Results</button>
          </div>
        </div>
      `;
      bindReviewDialog();
    }
    async function openCandidateReviewDialog() {
      if (!state.candidateSetId) return;
      if (!finalReviewReady()) {
        state.error = hasManualPending()
          ? "Process manual samples before reviewing results."
          : "Extracted images are not ready for final review.";
        renderAll();
        return;
      }
      if (!reviewOverlay?.isConnected) {
        reviewOverlay = document.createElement("div");
        reviewOverlay.className =
          "info-dialog-overlay maintenance-workflow-overlay reextract-review-overlay";
        document.body.appendChild(reviewOverlay);
      }
      state.reviewDialogMode = "review";
      renderReviewDialog();
      if (!state.samples.length && !state.loading) {
        await loadSamples(state.selectedSampleId);
      }
    }
    function bindReviewDialog() {
      if (!reviewOverlay?.isConnected) return;
      bindReextractStripGeometry();
      app.commands.bindDrawerLightboxButtons(reviewOverlay);
      reviewOverlay.tabIndex = -1;
      reviewOverlay.onkeydown = (event) => {
        const targetTag = String(event.target?.tagName || "").toLowerCase();
        if (
          ["input", "textarea", "select"].includes(targetTag) ||
          event.target?.isContentEditable
        )
          return;
        if (event.key === "ArrowDown" || event.key === "ArrowRight") {
          event.preventDefault();
          void selectAdjacenterviewSample(1);
        } else if (event.key === "ArrowUp" || event.key === "ArrowLeft") {
          event.preventDefault();
          void selectAdjacenterviewSample(-1);
        }
      };
      if (!reviewOverlay.contains(document.activeElement)) {
        reviewOverlay.focus({ preventScroll: true });
      }
      reviewOverlay
        .querySelector("#reextractReviewClose")
        ?.addEventListener("click", closeReviewDialog);
      reviewOverlay
        .querySelector("#reextractReviewCloseFooter")
        ?.addEventListener("click", closeReviewDialog);
      reviewOverlay
        .querySelector("#reextractCancelReviewJob")
        ?.addEventListener("click", cancelActiveReextractJob);
      reviewOverlay
        .querySelector("#reextractSaveAll")
        ?.addEventListener("click", () =>
          withBusy(async () => {
            await app.api.setReextractCandidateDecisionBulk(
              state.candidateSetId,
              "save",
            );
            await loadSamples(state.selectedSampleId);
          }),
        );
      reviewOverlay
        .querySelector("#reextractSkipAll")
        ?.addEventListener("click", () =>
          withBusy(async () => {
            await app.api.setReextractCandidateDecisionBulk(
              state.candidateSetId,
              "skip",
            );
            await loadSamples(state.selectedSampleId);
          }),
        );
      reviewOverlay
        .querySelectorAll("[data-reextract-sample]")
        .forEach((button) => {
          button.addEventListener("click", () =>
            withBusy(async () => {
              state.selectedSampleId = button.dataset.reextractSample || "";
              state.selectedSample =
                await app.api.fetchReextractCandidateSample(
                  state.candidateSetId,
                  state.selectedSampleId,
                );
            }),
          );
        });
      reviewOverlay
        .querySelectorAll("[data-reextract-decision]")
        .forEach((button) => {
          button.addEventListener("click", () =>
            withBusy(async () => {
              const sample = selectedSample();
              if (!sample) return;
              const payload = await app.api.setReextractCandidateDecision(
                state.candidateSetId,
                sample.sample_id,
                button.dataset.reextractDecision,
              );
              state.selectedSample = payload?.candidate || null;
              await loadSamples(sample.sample_id);
            }),
          );
        });
      reviewOverlay
        .querySelector("#reextractRetryCandidate")
        ?.addEventListener("click", async () => {
          const sample = selectedSample();
          if (!sample) return;
          await runReextractJob("retry", () =>
            app.api.startRetryReextractCandidateJob(
              state.candidateSetId,
              sample.sample_id,
            ),
          );
        });
      reviewOverlay
        .querySelector("#reextractManualCandidate")
        ?.addEventListener("click", () => {
          const sample = selectedSample();
          if (!sample) return;
          app.commands.openManualProcessing([sample.sample_id], {
            context: "reextract-candidate",
            candidateSetId: state.candidateSetId,
            onCandidateComplete: async () => {
              await loadSamples(sample.sample_id);
            },
          });
        });
      reviewOverlay
        .querySelector("#reextractReviewApply")
        ?.addEventListener("click", async () => {
          const confirmed = await confirmSaveCandidateSetDialog();
          if (!confirmed) return;
          await runReextractJob("apply", () =>
            app.api.startApplyReextractCandidateSetJob(state.candidateSetId),
          );
        });
      reviewOverlay
        .querySelector("#reextractReviewDeleteSet")
        ?.addEventListener("click", async () => {
          const confirmed = await confirmDeleteCandidateSetDialog();
          if (!confirmed) return;
          await withBusy(async () => {
            await app.api.deleteReextractCandidateSet(state.candidateSetId);
            closeReviewDialog();
            state.candidateSetId = "";
            state.samples = [];
            state.selectedSample = null;
            state.selectedSampleId = "";
            state.generationReport = null;
            state.applyReport = null;
            state.preflightExpanded = true;
            state.candidateSummaryExpanded = true;
            await onComplete?.();
          });
        });
    }
    function close() {
      if (state.running) return;
      closeReviewDialog();
      overlay.remove();
    }
    function render() {
      const activeJob = jobIsActive(state.job);
      const disableClose = state.running;
      const canGenerate =
        state.preflight?.enabled !== false &&
        state.preflight &&
        !state.busy &&
        !state.running;
      const showPreflightAction = !state.preflight && !state.candidateSetId;
      const showGenerateAction =
        Boolean(state.preflight) && !state.candidateSetId;
      const showInitialActionRow =
        showPreflightAction || (activeJob && !state.preflight);
      const showPostPreflightActionRow =
        Boolean(state.preflight) && (showGenerateAction || activeJob);
      const applyStatus = String(state.applyReport?.status || "");
      const applyMessage = reextractApplyResultMessage(applyStatus);
      overlay.innerHTML = `
        <div class="info-dialog maintenance-workflow-dialog reextract-workflow-dialog" role="dialog" aria-modal="true" aria-labelledby="reextractWorkflowTitle">
          ${app.commands.renderDialogHeader({
            title: operation.name || "Re-extract Sample Images",
            titleId: "reextractWorkflowTitle",
            closeButtonHtml: app.commands.renderWindowCloseButton({
              id: "reextractWorkflowClose",
              className: "info-dialog-close",
              disabled: disableClose,
            }),
          })}
          <div class="info-dialog-body maintenance-workflow-body reextract-workflow-body">
            ${state.error ? `<div class="backup-restore-message is-error">${app.commands.escapeHtml(state.error)}</div>` : ""}
            <section class="maintenance-workflow-operation">
              <div class="maintenance-detail-body">
                <p>${app.commands.escapeHtml(reextractWorkflowDescription)}</p>
                ${renderControls(Boolean(state.candidateSetId || state.busy))}
                <div class="maintenance-workflow-tags">
                  <span>${app.commands.escapeHtml(app.commands.maintenanceRiskLabel(operation.risk_class))}</span>
                </div>
              </div>
            </section>
            ${
              showInitialActionRow
                ? `
              <div class="backup-workflow-actions">
                ${showPreflightAction ? `<button class="primary-button small" type="button" id="reextractPreflight" ${state.busy || state.running ? "disabled" : ""}>Run Preflight</button>` : ""}
                ${activeJob ? `<button class="ghost-button small" type="button" id="reextractCancelJob" ${state.cancelling ? "disabled" : ""}>${state.cancelling ? "Cancelling..." : "Cancel"}</button>` : ""}
              </div>
            `
                : ""
            }
            ${
              state.preflight
                ? `
              <section class="maintenance-workflow-section">
                ${workflowSectionCap("Preflight", state.preflightExpanded, "reextractTogglePreflight", Boolean(state.candidateSetId || state.generationReport || state.applyReport))}
                ${
                  state.preflightExpanded
                    ? `
                  <div class="maintenance-detail-body">
                    ${reextractSummaryRowsHtml(state.preflight.summary || {})}
                    ${reextractManualNoticeHtml(reextractManualFindings(state.preflight))}
                    ${app.commands.maintenanceFindingsHtml({ blocked: reextractBlockedFindings(state.preflight) })}
                  </div>
                `
                    : ""
                }
              </section>
            `
                : ""
            }
            ${
              showPostPreflightActionRow
                ? `
              <div class="backup-workflow-actions">
                ${showGenerateAction ? `<button class="primary-button small" type="button" id="reextractGenerate" ${!canGenerate ? "disabled" : ""}>Extract Images</button>` : ""}
                ${activeJob ? `<button class="ghost-button small" type="button" id="reextractCancelJob" ${state.cancelling ? "disabled" : ""}>${state.cancelling ? "Cancelling..." : "Cancel"}</button>` : ""}
              </div>
            `
                : ""
            }
            ${app.commands.reextractProgressHtml(state.job, activeJob, "Running re-extraction")}
            ${candidateSetSummaryHtml()}
            ${
              state.applyReport
                ? `
              <section class="maintenance-workflow-section">
                <div class="drawer-module-cap"><span class="sidebar-label">Apply Result</span></div>
                <div class="maintenance-detail-body">
                  <div class="backup-restore-message ${applyStatus === "completed" ? "is-success" : "is-warning"}">${app.commands.escapeHtml(applyMessage)}</div>
                  ${app.commands.maintenanceSummaryHtml(state.applyReport.summary || {})}
                  ${app.commands.maintenanceFindingsHtml(state.applyReport)}
                </div>
              </section>
            `
                : ""
            }
          </div>
        </div>
      `;
      bind();
    }
    function bind() {
      overlay
        .querySelector("#reextractWorkflowClose")
        ?.addEventListener("click", close);
      overlay
        .querySelector("#reextractCancelJob")
        ?.addEventListener("click", cancelActiveReextractJob);
      overlay
        .querySelectorAll("input[name='reextractDomainMode']")
        .forEach((input) => {
          input.addEventListener("change", (event) => {
            if (state.candidateSetId || state.busy) return;
            state.domainMode = event.target.value;
            clearPreflightState();
            render();
          });
        });
      overlay
        .querySelectorAll("input[name='reextractSegmentationMode']")
        .forEach((input) => {
          input.addEventListener("change", (event) => {
            if (state.candidateSetId || state.busy) return;
            state.segmentationMode = event.target.value;
            if (state.segmentationMode === "redetect_from_scratch")
              state.domainMode = "complete";
            clearPreflightState();
            render();
          });
        });
      overlay
        .querySelectorAll("input[name='reextractSampleScope']")
        .forEach((input) => {
          input.addEventListener("change", (event) => {
            if (state.candidateSetId || state.busy) return;
            state.sampleScopeMode =
              event.target.value === "sample_ids"
                ? "sample_ids"
                : "all_accepted";
            clearPreflightState();
            render();
          });
        });
      overlay
        .querySelector("#reextractSampleIds")
        ?.addEventListener("input", (event) => {
          if (state.candidateSetId || state.busy) return;
          state.sampleIdsText = event.target.value;
          if (state.preflight || state.generationReport || state.applyReport) {
            clearPreflightState();
            render();
            return;
          }
          const countNode = overlay.querySelector(
            "[data-reextract-sample-count]",
          );
          if (countNode && state.sampleScopeMode === "sample_ids") {
            countNode.textContent = `${parseReextractSampleIds().length} selected`;
          }
        });
      overlay
        .querySelector("#reextractPreflight")
        ?.addEventListener("click", () =>
          runReextractJob("preflight", () =>
            app.api.startReextractPreflightJob(scopePayload()),
          ),
        );
      overlay
        .querySelector("#reextractGenerate")
        ?.addEventListener("click", () =>
          runReextractJob("generate", () =>
            app.api.startReextractCandidateSetJob(
              scopePayload(),
              state.preflight,
            ),
          ),
        );
      overlay
        .querySelector("#reextractOpenReview")
        ?.addEventListener("click", () => {
          void openCandidateReviewDialog();
        });
      overlay
        .querySelector("#reextractOpenManualStep")
        ?.addEventListener("click", () => {
          void openManualStepDialog();
        });
      overlay
        .querySelector("#reextractTogglePreflight")
        ?.addEventListener("click", () => {
          state.preflightExpanded = !state.preflightExpanded;
          render();
        });
      overlay
        .querySelector("#reextractToggleCandidateSummary")
        ?.addEventListener("click", () => {
          state.candidateSummaryExpanded = !state.candidateSummaryExpanded;
          render();
        });
    }
    function renderAll() {
      render();
      if (state.reviewDialogMode === "manual") {
        renderManualStepDialog();
      } else if (state.reviewDialogMode === "review") {
        renderReviewDialog();
      }
    }
    document.body.appendChild(overlay);
    render();
  }

  Object.assign(app.commands, {
    showReextractSampleImagesWorkflow,
  });
}
