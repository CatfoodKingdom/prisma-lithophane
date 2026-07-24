/** Install features/modeling/workflow commands. */
export function installFeaturesModelingWorkflow(app) {
  function _getProcessedByFilament() {
    // Group processed samples by their variable filament
    const byFil = {};
    for (const exp of app.state.session.data.samples) {
      if (exp._processing_status !== "processed") continue;
      const fid = exp.variable_filament_id;
      if (!byFil[fid]) byFil[fid] = [];
      byFil[fid].push(exp);
    }
    return byFil;
  }

  function _shortSampleDescription(exp) {
    const thicknesses = exp.variable_thicknesses_mm || [];
    if (thicknesses.length === 0) return "—";
    const first = Number(thicknesses[0]);
    const second = thicknesses.length > 1 ? Number(thicknesses[1]) : null;
    const last = Number(thicknesses[thicknesses.length - 1]);
    if (first === 0 || first < 0.001) {
      // Blank in first swatch: "0.00, [swatch2]-[swatch8] mm"
      return second != null
        ? `0.00, ${second.toFixed(2)}\u2013${last.toFixed(2)} mm`
        : `0.00 mm`;
    }
    // No blank: "[swatch1]-[swatch8] mm"
    return `${first.toFixed(2)}\u2013${last.toFixed(2)} mm`;
  }

  function _mfBuildMockStrip(swatches, label) {
    // Simple mock strip from hex values — no exclusion logic
    if (!swatches || swatches.length === 0) return "";
    const n = swatches.length;
    const tiles = swatches
      .map((sw) => {
        const hex =
          (label === "Pred." ? sw.predicted_hex : sw.measured_hex) || "#888";
        return `<div class="mf-mock-swatch" style="background:${hex}"></div>`;
      })
      .join("");
    return `
      <div class="mf-strip-row">
        <span class="mf-strip-label">${label}</span>
        <div class="mf-mock-strip" style="grid-template-columns:repeat(${n},1fr)">${tiles}</div>
      </div>`;
  }

  function _mfBuildDiagram(exp) {
    // Clean swatch diagram — no exclusion logic
    const variableHex = exp.variable_hex || "#dddddd";
    const variableText = app.commands.textColor(variableHex);
    const thicknesses = exp.variable_thicknesses_mm || [];
    const cells = thicknesses
      .map(
        (t) =>
          `<td style="background:${variableHex};color:${variableText}">${Number(t).toFixed(2)}</td>`,
      )
      .join("");
    return `<table class="mini-strip-table mf-diagram"><tr>${cells}</tr></table>`;
  }

  function _mfBuildDeltaEBars(swatches) {
    // Bar graph aligned 1:1 with swatches above, with Y-axis scale
    if (!swatches || swatches.length === 0) return "";
    const n = swatches.length;
    const rawMax = Math.max(...swatches.map((s) => s.delta_e || 0), 0.01);
    // Round up to a clean tick value
    const maxDE =
      rawMax <= 1
        ? Math.ceil(rawMax * 10) / 10
        : rawMax <= 5
          ? Math.ceil(rawMax)
          : Math.ceil(rawMax / 5) * 5;
    const barH = 32; // max bar height px

    const bars = swatches
      .map((sw) => {
        const de = sw.delta_e || 0;
        const h = Math.max(Math.round((de / maxDE) * barH), 1);
        const cls =
          de < 2 ? "mf-bar-good" : de < 5 ? "mf-bar-ok" : "mf-bar-bad";
        return `<div class="mf-de-bar ${cls}" style="height:${h}px" title="ΔE ${de.toFixed(1)}"></div>`;
      })
      .join("");

    const midDE = maxDE / 2;

    return `
      <div class="mf-strip-row">
        <span class="mf-strip-label">ΔE</span>
        <div class="mf-de-chart">
          <div class="mf-de-axis">
            <span class="mf-de-tick">${maxDE.toFixed(2)}</span>
            <span class="mf-de-tick">${midDE.toFixed(2)}</span>
            <span class="mf-de-tick">0</span>
          </div>
          <div class="mf-de-bar-row" style="grid-template-columns:repeat(${n},1fr);height:${barH}px">${bars}</div>
        </div>
      </div>`;
  }

  function _nmEvidenceLabel(key) {
    const labels = {
      single_color_sandwich: "single color sandwich",
      cross_color_multilayer_sandwich: "multicolor sandwich",
      color_over_white: "color over white",
      multicolor_over_white: "multicolor over white",
      naked_single_filament: "naked single",
      white_only: "white only",
      unsupported_or_diagnostic: "diagnostic",
    };
    return labels[key] || String(key || "unknown").replaceAll("_", " ");
  }

  function _nmSampleSearchText(sample) {
    const parts = [
      sample.sample_id,
      sample.evidence_class,
      sample.stack_signature,
    ];
    for (const swatch of sample.swatches || []) {
      for (const layer of swatch.stack || []) {
        const fil = app.commands.filamentMeta(layer.filament_id) || {};
        parts.push(layer.filament_id, fil.color_name, fil.manufacturer);
      }
    }
    return parts.filter(Boolean).join(" ").toLowerCase();
  }

  function _nmFilteredSamples() {
    const payload = app.state.modeling.photoStackModelState.predictions || {};
    const raw = Array.isArray(payload.samples) ? payload.samples : [];
    const q = (app.state.modeling.photoStackModelState.search || "")
      .trim()
      .toLowerCase();
    const evidenceClass =
      app.state.modeling.photoStackModelState.evidenceClass || "all";
    return raw
      .filter(
        (sample) =>
          evidenceClass === "all" || sample.evidence_class === evidenceClass,
      )
      .filter(
        (sample) => !q || app.commands._nmSampleSearchText(sample).includes(q),
      )
      .sort(
        (a, b) =>
          app.commands._nmSampleNumber(a.sample_id) -
          app.commands._nmSampleNumber(b.sample_id),
      );
  }

  function _nmBuildChipStrip(swatches, key, label) {
    if (!swatches || swatches.length === 0) return "";
    const tiles = swatches
      .map((swatch) => {
        const hex = swatch?.[key]?.hex || "#eeeeee";
        return `<div class="nm-chip" style="background:${hex}" title="${app.commands._escAttr(hex)}"></div>`;
      })
      .join("");
    return `
      <div class="nm-chip-row">
        <span class="nm-chip-label">${label}</span>
        <div class="nm-chip-strip" style="grid-template-columns:repeat(${swatches.length},1fr)">${tiles}</div>
      </div>`;
  }

  function _nmPredictionSpecs() {
    const payload = app.state.modeling.photoStackModelState.predictions || {};
    const rows = Array.isArray(payload.prediction_rows)
      ? payload.prediction_rows
      : [];
    if (rows.length) return rows;
    const sample = (payload.samples || []).find((entry) =>
      (entry.swatches || []).some((swatch) => swatch.predictions),
    );
    const predictions =
      sample?.swatches?.find((swatch) => swatch.predictions)?.predictions || {};
    const keys = Object.keys(predictions);
    if (keys.length) {
      const labels = {
        photo_stack_corrected: "Photo stack + corrections",
        photo_stack: "Photo stack",
      };
      return keys.map((key) => ({ key, label: labels[key] || key }));
    }
    return [{ key: "predicted", label: "Photo stack" }];
  }

  function _nmBuildPredictionChipStrip(swatches, spec) {
    if (!swatches || swatches.length === 0) return "";
    const key = spec?.key || "predicted";
    const label = spec?.label || key;
    const tiles = swatches
      .map((swatch) => {
        const pred =
          (swatch.predictions && swatch.predictions[key]) ||
          (key === "predicted" ? swatch.predicted : null) ||
          swatch.predicted ||
          {};
        const hex = pred.hex || "#eeeeee";
        return `<div class="nm-chip" style="background:${hex}" title="${app.commands._escAttr(hex)}"></div>`;
      })
      .join("");
    return `
      <div class="nm-chip-row">
        <span class="nm-chip-label">${app.commands._escHtml(label)}</span>
        <div class="nm-chip-strip" style="grid-template-columns:repeat(${swatches.length},1fr)">${tiles}</div>
      </div>`;
  }

  function _nmDeltaClass(delta) {
    const d = Number(delta);
    if (!Number.isFinite(d)) return "";
    if (d <= 0.035) return "is-good";
    if (d <= 0.075) return "is-ok";
    return "is-bad";
  }

  function _nmBuildDeltaPills(swatches, spec = null) {
    if (!swatches || swatches.length === 0) return "";
    const key = spec?.key || null;
    const label = spec?.label || "d";
    const pills = swatches
      .map((swatch) => {
        const pred = key && swatch.predictions ? swatch.predictions[key] : null;
        const d = Number(pred?.oklab_delta ?? swatch.oklab_delta);
        const text = Number.isFinite(d) ? d.toFixed(3) : "—";
        return `<span class="nm-delta-pill ${app.commands._nmDeltaClass(d)}">${text}</span>`;
      })
      .join("");
    return `
      <div class="nm-delta-row">
        <span class="nm-chip-label">${app.commands._escHtml(label === "d" ? "d" : `d ${label}`)}</span>
        <div class="nm-delta-strip" style="grid-template-columns:repeat(${swatches.length},1fr)">${pills}</div>
      </div>`;
  }

  function _nmLayerLabel(fid) {
    const fil = app.commands.filamentMeta(fid) || {};
    return fil.color_name || fid || "unknown";
  }

  function _nmSampleNumber(sampleId) {
    const match = String(sampleId || "").match(/\d+/);
    return match ? Number(match[0]) : Number.MAX_SAFE_INTEGER;
  }

  function _nmBuildDiagramExp(sample) {
    return (
      (app.state.session.data.samples || []).find(
        (exp) => exp.sample_id === sample.sample_id,
      ) || null
    );
  }

  function _nmBuildDiagramLabels(sample) {
    const exp = app.commands._nmBuildDiagramExp(sample);
    if (!exp) return "";
    const roles = [...(exp.roles || [])].sort(
      (a, b) => Number(b.role_index || 0) - Number(a.role_index || 0),
    );
    return roles
      .map((role) => {
        const fid = role.filament_id || "";
        if (!fid) return "";
        const fil = app.commands.filamentMeta(fid) || {};
        const hex = fil.hex || "#dddddd";
        return `
        <div class="nm-diagram-label">
          <span class="color-chip tiny" style="background:${hex}"></span>
          <span>${app.commands._escHtml(app.commands._nmLayerLabel(fid))}</span>
        </div>`;
      })
      .filter(Boolean)
      .join("");
  }

  function _nmBuildStackDiagram(sample) {
    const exp = app.commands._nmBuildDiagramExp(sample);
    if (!exp)
      return `<div class="strip-diagram-contract-error">Missing canonical sample data</div>`;
    const labels = app.commands._nmBuildDiagramLabels(sample);
    return `
      <div class="nm-diagram-wrap">
        <div class="sample-strip-tight nm-stack-diagram">${app.commands.buildStripMiniTable(exp)}</div>
        <div class="nm-diagram-labels">${labels}</div>
      </div>`;
  }

  function _nmBuildSampleRow(sample) {
    const swatches = sample.swatches || [];
    const predictionSpecs = app.commands._nmPredictionSpecs();
    const mean = Number(sample.mean_oklab_delta);
    const max = Number(sample.max_oklab_delta);
    const stats = [
      Number.isFinite(mean) ? `mean ${mean.toFixed(3)}` : null,
      Number.isFinite(max) ? `max ${max.toFixed(3)}` : null,
    ]
      .filter(Boolean)
      .join(" / ");
    return `
      <div class="nm-review-row">
        <div class="nm-review-main">
          <div class="nm-review-head">
            <strong>${app.commands._escHtml(sample.sample_id || "")}</strong>
            <span>${app.commands._escHtml(app.commands._nmEvidenceLabel(sample.evidence_class))}</span>
            ${stats ? `<span class="mono">${stats}</span>` : ""}
          </div>
          ${app.commands._nmBuildChipStrip(swatches, "measured", "Measured")}
          ${predictionSpecs.map((spec) => app.commands._nmBuildPredictionChipStrip(swatches, spec)).join("")}
        </div>
        <div class="nm-error-cell">${predictionSpecs.map((spec) => app.commands._nmBuildDeltaPills(swatches, spec)).join("")}</div>
        <div class="nm-diagram-cell">${app.commands._nmBuildStackDiagram(sample)}</div>
      </div>`;
  }

  function _nmRenderProgressPanel() {
    const job = app.state.modeling.photoStackModelState.status;
    if (!app.state.modeling.photoStackModelState.isFitting || !job) return "";
    const progress = job.progress || {};
    const pct = Math.max(0, Math.min(100, Number(progress.percent || 0)));
    const message = progress.message || "Fitting photo stack model";
    const phase = progress.phase || job.status || "running";
    const target = progress.target
      ? `<span class="mono">${app.commands._escHtml(progress.target)}</span>`
      : "";
    return `
      <div class="nm-progress-panel">
        <div class="validate-progress-head">
          <strong>Fitting Photo Stack Model</strong>
          <span class="small-copy mono">${pct.toFixed(0)}%</span>
        </div>
        <div class="profile-progress-track">
          <div class="profile-progress-fill validate-progress-fill" style="width:${pct}%"></div>
        </div>
        <div class="validate-progress-meta">
          <span>${app.commands._escHtml(message)}</span>
          <span class="mono">${app.commands._escHtml(phase)}</span>
          ${target}
        </div>
      </div>`;
  }

  function _nmUpdateProgressOnly() {
    const panel = document.querySelector(".nm-progress-panel");
    const html = app.commands._nmRenderProgressPanel();
    if (panel && html) {
      panel.outerHTML = html;
      return;
    }
    app.commands.renderModelFitting();
  }

  function _nmRenderCandidatePanel() {
    const candidate = app.state.modeling.photoStackModelState.candidate;
    const predictions = app.state.modeling.photoStackModelState.predictions;
    const samples = app.commands._nmFilteredSamples();
    const evidenceClasses = Array.from(
      new Set(
        (predictions?.samples || [])
          .map((sample) => sample.evidence_class)
          .filter(Boolean),
      ),
    ).sort();
    const classOptions = [`<option value="all">All sample types</option>`]
      .concat(
        evidenceClasses.map(
          (cls) =>
            `<option value="${app.commands._escAttr(cls)}"${app.state.modeling.photoStackModelState.evidenceClass === cls ? " selected" : ""}>${app.commands._escHtml(app.commands._nmEvidenceLabel(cls))}</option>`,
        ),
      )
      .join("");
    const loading = app.state.modeling.photoStackModelState.loadingCandidate
      ? `<div class="mf-placeholder">Loading candidate...</div>`
      : "";
    const error = app.state.modeling.photoStackModelState.error
      ? `<div class="mf-placeholder nm-error-text">${app.commands._escHtml(app.state.modeling.photoStackModelState.error)}</div>`
      : "";
    const rows = samples
      .map((sample) => app.commands._nmBuildSampleRow(sample))
      .join("");
    const runId =
      candidate?.run_id ||
      app.state.modeling.photoStackModelState.latest?.run_id ||
      "";
    const engine =
      candidate?.review_summary?.engine_status ||
      predictions?.engine_status ||
      "";
    const sampleCount =
      predictions?.total_samples ?? predictions?.sample_count ?? 0;
    return `
      <div class="nm-panel" data-panel="photo-stack-fit">
        <div class="nm-panel-head">
          <div>
            <div class="nm-title">Photo Stack Model</div>
            <div class="small-copy">Latest Photo Stack model generated by the Fit Models workflow.</div>
          </div>
          <div class="nm-head-actions">
            ${runId ? `<span class="toolbar-chip mono">${app.commands._escHtml(runId)}</span>` : ""}
            ${engine ? `<span class="toolbar-chip">${app.commands._escHtml(engine)}</span>` : ""}
          </div>
        </div>
        ${app.commands._nmRenderProgressPanel()}
        <div class="nm-controls">
          <button class="ghost-button small" id="nmRefreshBtn" ${app.state.modeling.photoStackModelState.loadingCandidate ? "disabled" : ""}>Reload Latest Model</button>
          <input class="nm-search" id="nmSearchInput" type="search" value="${app.commands._escAttr(app.state.modeling.photoStackModelState.search)}" placeholder="Search samples or filaments">
          <select class="nm-filter" id="nmEvidenceFilter">${classOptions}</select>
          <span class="small-copy">${samples.length} shown / ${sampleCount || 0} samples</span>
        </div>
        ${loading}
        ${error}
        ${!loading && !error && !candidate ? `<div class="mf-placeholder">No photo stack model has been generated yet. Run Fit Models to create one.</div>` : ""}
        ${rows ? `<div class="nm-review-list">${rows}</div>` : !loading && candidate ? `<div class="mf-placeholder">No samples match the current filters.</div>` : ""}
      </div>`;
  }

  function _ctRenderProgressPanel() {
    const job = app.state.modeling.cameraTransformState.status;
    if (!app.state.modeling.cameraTransformState.isBuilding || !job) return "";
    const progress = job.progress || {};
    const pct = Math.max(0, Math.min(100, Number(progress.percent || 0)));
    const message = progress.message || "Building Camera Transform";
    const phase = progress.phase || job.status || "running";
    return `
      <div class="nm-progress-panel">
        <div class="validate-progress-head">
          <strong>Building Camera Transform</strong>
          <span class="small-copy mono">${pct.toFixed(0)}%</span>
        </div>
        <div class="profile-progress-track">
          <div class="profile-progress-fill validate-progress-fill" style="width:${pct}%"></div>
        </div>
        <div class="validate-progress-meta">
          <span>${app.commands._escHtml(message)}</span>
          <span class="mono">${app.commands._escHtml(phase)}</span>
        </div>
      </div>`;
  }

  function _ctRenderPanel() {
    const current = app.state.modeling.cameraTransformState.current || {};
    const status = current.status || "missing";
    const manifest = current.manifest || {};
    const corpus = manifest.corpus || {};
    const validation = manifest.validation_dE76_CIELAB || {};
    const error = app.state.modeling.cameraTransformState.error
      ? `<div class="mf-placeholder nm-error-text">${app.commands._escHtml(app.state.modeling.cameraTransformState.error)}</div>`
      : "";
    const createdAt = current.created_at || manifest.created_at || "";
    const validationMean = Number(
      current.validation_mean_de76 ?? validation.mean,
    );
    const corpusSize = Number(
      current.corpus_size ?? corpus.usable_swatch_count,
    );
    const statusText =
      status === "present"
        ? "READY"
        : status === "invalid"
          ? "INVALID"
          : "MISSING";
    return `
      <div class="nm-panel" data-panel="camera-transform">
        <div class="nm-panel-head">
          <div>
            <div class="nm-title">Camera Transform</div>
            <div class="small-copy">Transmission to camera-rendered appearance transform used by generator ingress and previews.</div>
          </div>
          <div class="nm-head-actions">
            <span class="toolbar-chip">${app.commands._escHtml(statusText)}</span>
            ${createdAt ? `<span class="toolbar-chip mono">${app.commands._escHtml(createdAt)}</span>` : ""}
          </div>
        </div>
        ${app.commands._ctRenderProgressPanel()}
        <div class="nm-controls">
          <button class="ghost-button small" id="ctRefreshBtn">Reload Status</button>
          ${Number.isFinite(validationMean) ? `<span class="small-copy">cross-validation mean dE76 ${validationMean.toFixed(3)}</span>` : ""}
          ${Number.isFinite(corpusSize) ? `<span class="small-copy">${corpusSize} swatches</span>` : ""}
        </div>
        ${status !== "present" && current.reason ? `<div class="mf-placeholder">${app.commands._escHtml(current.reason)}</div>` : ""}
        ${error}
      </div>`;
  }

  async function _ctLoadCurrent(force = false) {
    if (!force && app.state.modeling.cameraTransformState.current) return;
    if (typeof app.api.fetchCameraTransformCurrent !== "function") {
      app.state.modeling.cameraTransformState.error =
        "Camera Transform API is unavailable in static mode.";
      return;
    }
    try {
      app.state.modeling.cameraTransformState.current =
        await app.api.fetchCameraTransformCurrent();
      app.state.modeling.cameraTransformState.error = null;
    } catch (err) {
      app.state.modeling.cameraTransformState.error = String(
        err?.message || err || "Failed to load Camera Transform status",
      );
    }
  }

  async function pollCameraTransformJob(jobId) {
    while (true) {
      const status = await app.api.fetchCameraTransformJobStatus(jobId);
      app.state.modeling.cameraTransformState.status = status;
      app.state.modeling.cameraTransformState.isBuilding =
        status.status === "queued" || status.status === "running";
      // Targeted panel swap only — NOT renderModelFitting(). A full re-render here
      // rebuilt the entire (heavy) Model Fitting tab incl. the photo-stack candidate
      // chip grid every 700ms for the whole multi-minute build, ballooning browser
      // memory to ~12 GB. _ctRenderPanel() embeds the live progress sub-panel, so
      // this updates the progress bar without rebuilding the rest of the tab.
      app.commands._refreshCameraTransformPanel();
      if (status.status === "completed") return status.result || status;
      if (status.status === "failed")
        throw new Error(status.error || "Camera Transform build failed");
      await app.commands.sleep(700);
    }
  }

  async function _nmLoadLatestCandidate(force = false) {
    if (app.state.modeling.photoStackModelState.loadingCandidate) return;
    if (
      !force &&
      app.state.modeling.photoStackModelState.latest &&
      app.state.modeling.photoStackModelState.candidate &&
      app.state.modeling.photoStackModelState.predictions
    )
      return;
    if (!force && app.state.modeling.photoStackModelState.error) return;
    if (typeof app.api.fetchPhotoStackLatest !== "function") {
      app.state.modeling.photoStackModelState.error =
        "Photo stack model API is unavailable in static mode.";
      return;
    }
    app.state.modeling.photoStackModelState.loadingCandidate = true;
    app.state.modeling.photoStackModelState.error = null;
    try {
      const latest = await app.api.fetchPhotoStackLatest();
      const runId = latest?.run_id;
      if (!runId)
        throw new Error("Latest candidate response did not include run_id");
      const [candidate, predictions] = await Promise.all([
        app.api.fetchPhotoStackCandidate(runId),
        app.api.fetchPhotoStackSamplePredictions(runId, { limit: 1000 }),
      ]);
      app.state.modeling.photoStackModelState.latest = latest;
      app.state.modeling.photoStackModelState.candidate = candidate;
      app.state.modeling.photoStackModelState.predictions = predictions;
    } catch (err) {
      app.state.modeling.photoStackModelState.latest = null;
      app.state.modeling.photoStackModelState.candidate = null;
      app.state.modeling.photoStackModelState.predictions = null;
      app.state.modeling.photoStackModelState.error = String(
        err?.message || err || "Failed to load candidate",
      );
    } finally {
      app.state.modeling.photoStackModelState.loadingCandidate = false;
    }
  }

  async function pollPhotoStackJob(jobId) {
    while (true) {
      const status = await app.api.fetchPhotoStackJobStatus(jobId);
      app.state.modeling.photoStackModelState.status = status;
      app.state.modeling.photoStackModelState.isFitting =
        status.status === "queued" || status.status === "running";
      app.commands._nmUpdateProgressOnly();
      if (status.status === "completed") return status.result || status;
      if (status.status === "failed")
        throw new Error(status.error || "Photo stack model fit failed");
      await app.commands.sleep(700);
    }
  }

  function _mfBuildSampleCard(pred, exp) {
    // Card for summary view: measured strip, predicted strip, diagram, ΔE bars
    const sid = pred.sample_id;
    const swatches = pred.swatches || [];
    const desc = app.commands._shortSampleDescription(exp);
    const nFixed = pred.n_fixed || 0;
    const layerLabel = nFixed === 0 ? "" : `, ${nFixed + 1}L`;

    let cardHtml = `<div class="mf-sample-card" data-mf-sample="${sid}">`;
    cardHtml += `<div class="mf-card-header"><span class="mono mf-card-id">${sid}</span><span class="mf-card-desc">${desc}${layerLabel}</span></div>`;

    if (!pred.can_predict) {
      const missing = (pred.missing_profiles || []).join(", ");
      cardHtml += `<div class="mf-placeholder">Cannot predict — missing profile for: ${missing}</div>`;
    } else {
      cardHtml += app.commands._mfBuildMockStrip(swatches, "Meas.");
      cardHtml += app.commands._mfBuildMockStrip(swatches, "Pred.");
      cardHtml += `<div class="mf-strip-row"><span class="mf-strip-label"></span>${app.commands._mfBuildDiagram(exp)}</div>`;
      cardHtml += app.commands._mfBuildDeltaEBars(swatches);
    }

    cardHtml += `</div>`;
    return cardHtml;
  }

  function _mfBuildDetailView(pred, exp) {
    // Individual sample detail: strip photo, measured, predicted, diagram, ΔE bars
    const sid = pred.sample_id;
    const swatches = pred.swatches || [];
    const desc = app.commands._shortSampleDescription(exp);
    const nFixed = pred.n_fixed || 0;

    // Fixed layers info
    let fixedHtml = "";
    const fixedLines = app.commands
      .sampleFilamentRoleLines(exp)
      .filter((line) => line.roleKind === "fixed");
    if (fixedLines.length > 0) {
      fixedHtml = fixedLines
        .map((line) => {
          const ft =
            line.thicknessMm != null ? line.thicknessMm.toFixed(2) : "?";
          return `<div class="mf-detail-fixed">${app.commands._escHtml(line.layerLabel)}: ${ft}mm ${app.commands._escHtml(line.name)} <span class="color-chip" style="background:${line.hex || "#ddd"}"></span></div>`;
        })
        .join("");
    }

    let html = `<div class="mf-detail-view">`;
    html += `<div class="mf-detail-back"><a href="#" id="mfBackToGrid">&larr; All Samples</a></div>`;
    html += `<div class="mf-detail-header">`;
    html += `<span class="mono" style="font-size:14px;font-weight:700">${sid}</span>`;
    html += `<span style="font-size:12px;color:var(--muted)">${desc}${nFixed > 0 ? `, ${nFixed + 1}L` : ""}</span>`;
    html += `</div>`;
    if (fixedHtml)
      html += `<div class="mf-detail-fixed-block">${fixedHtml}</div>`;

    // Strip image
    html += `<div class="mf-detail-strip-img"><img src="${app.commands.sampleThumbnailUrl(sid, "strip", true)}" alt="Extracted strip" onerror="this.outerHTML='<span class=small-copy>No strip image</span>'"></div>`;

    if (!pred.can_predict) {
      const missing = (pred.missing_profiles || []).join(", ");
      html += `<div class="mf-placeholder">Cannot predict — missing profile for: ${missing}</div>`;
    } else {
      html += app.commands._mfBuildMockStrip(swatches, "Meas.");
      html += app.commands._mfBuildMockStrip(swatches, "Pred.");
      html += `<div class="mf-strip-row"><span class="mf-strip-label"></span>${app.commands._mfBuildDiagram(exp)}</div>`;
      html += app.commands._mfBuildDeltaEBars(swatches);
    }

    html += `</div>`;
    return html;
  }

  function _mfLoadingPlaceholder(message = "Loading fit results") {
    return `
      <div class="mf-placeholder mf-loading-state">
        <span class="proc-spinner" aria-hidden="true"></span>
        <span>${app.commands.escapeHtml(message)}</span>
      </div>
    `;
  }

  function _mfRenderLayout({
    filListHtml,
    sampleCardsHtml,
    samples,
    selSample,
    rightPaneTitle,
    rightPaneContent,
  }) {
    app.dom.tableContainer.innerHTML = `
      ${app.commands.renderProfileFitProgressPanel()}
      ${app.commands._ctRenderPanel()}
      ${app.commands._nmRenderCandidatePanel()}
      <div class="fi-layout">
        <div class="fi-left-pane">
          <div class="fi-pane-title">Filaments</div>
          <div class="fi-filament-list">${filListHtml}</div>
        </div>
        <div class="fi-center-pane">
          <div class="fi-pane-title">Samples</div>
          <div class="fi-sample-list">
            <div class="fi-sample-card fi-all-samples-card${!selSample ? " is-selected" : ""}" id="mfAllSamplesCard">
              <div class="fi-sample-header">
                <span style="font-size:11px;font-weight:600">All Samples</span>
                <span class="fi-sample-count">${samples.length}</span>
              </div>
            </div>
            ${sampleCardsHtml}
          </div>
        </div>
        <div class="fi-right-pane">
          <div class="fi-pane-title">${rightPaneTitle}</div>
          <div class="mf-right-content">${rightPaneContent}</div>
        </div>
      </div>
    `;
    app.commands._bindModelFittingActions();
    app.commands._bindCameraTransformActions();
    app.commands._bindPhotoStackPanelActions();
  }

  async function refreshModelsAfterWorkflow() {
    if (typeof app.commands.handleRefresh === "function") {
      await app.commands.handleRefresh({ ensureAssets: false });
    }
    app.commands.invalidateModelingPayloads();
  }

  async function openFitModelsWorkflow(
    button = null,
    onComplete = app.commands.refreshModelsAfterWorkflow,
  ) {
    if (app.state.modeling.fitModelsWorkflowLaunchBusy) return;
    if (document.querySelector(".maintenance-workflow-overlay")) {
      app.commands.showImportToast(
        "Close the active maintenance workflow before opening another.",
        "warning",
      );
      return;
    }
    const originalText = button?.textContent || "";
    app.state.modeling.fitModelsWorkflowLaunchBusy = true;
    if (button) {
      button.disabled = true;
      button.textContent = "Loading...";
    }
    try {
      const operation = await app.commands.maintenanceOperationById(
        "refit_calibration_models",
      );
      if (!operation) {
        app.commands.showImportToast("Fit Models is not available", "error");
        return;
      }
      if (operation.enabled === false) {
        app.commands.showImportToast(
          operation.unavailable_reason ||
            operation.disabled_reason ||
            "Fit Models is unavailable",
          "warning",
        );
        return;
      }
      app.commands.showMaintenanceWorkflow(operation, onComplete);
    } catch (err) {
      app.commands.showImportToast(
        err.message || "Could not open Fit Models workflow",
        "error",
      );
    } finally {
      app.state.modeling.fitModelsWorkflowLaunchBusy = false;
      if (button?.isConnected) {
        button.disabled = false;
        button.textContent = originalText;
      }
    }
  }

  function openFitModelsWorkflowFromModelFitting(button = null) {
    return app.commands.openFitModelsWorkflow(button);
  }

  async function renderModelFitting() {
    const renderSeq = ++app.state.modeling.modelFittingState.renderSeq;
    app.dom.tableToolbar.className = "toolbar-inline";
    if (
      !app.state.modeling.photoStackModelState.requestedInitialLoad &&
      !app.state.modeling.photoStackModelState.loadingCandidate
    ) {
      app.state.modeling.photoStackModelState.requestedInitialLoad = true;
      app.commands
        ._nmLoadLatestCandidate(false)
        .then(() => {
          if (
            app.state.navigation.currentMode === "imageProcessing" &&
            app.state.navigation.currentSubtab === "model_fitting"
          ) {
            app.commands._refreshPhotoStackPanel();
          }
        })
        .catch((err) => {
          app.state.modeling.photoStackModelState.error =
            err.message || String(err);
          if (
            app.state.navigation.currentMode === "imageProcessing" &&
            app.state.navigation.currentSubtab === "model_fitting"
          ) {
            app.commands._refreshPhotoStackPanel();
          }
        });
    }
    if (!app.state.modeling.cameraTransformState.requestedInitialLoad) {
      app.state.modeling.cameraTransformState.requestedInitialLoad = true;
      app.commands
        ._ctLoadCurrent(false)
        .then(() => {
          if (
            app.state.navigation.currentMode === "imageProcessing" &&
            app.state.navigation.currentSubtab === "model_fitting"
          ) {
            app.commands.renderModelFitting();
          }
        })
        .catch((err) => {
          app.state.modeling.cameraTransformState.error =
            err.message || String(err);
          if (
            app.state.navigation.currentMode === "imageProcessing" &&
            app.state.navigation.currentSubtab === "model_fitting"
          ) {
            app.commands.renderModelFitting();
          }
        });
    }

    const byFil = app.commands._getProcessedByFilament();
    const filamentIds = Object.keys(byFil).sort((a, b) => {
      const fa = app.commands.filamentMeta(a),
        fb = app.commands.filamentMeta(b);
      return (fa?.color_name || a).localeCompare(fb?.color_name || b);
    });

    const totalProcessed = Object.values(byFil).reduce(
      (s, arr) => s + arr.length,
      0,
    );
    app.dom.tableSummary.textContent = `${filamentIds.length} filaments with data, ${totalProcessed} processed samples`;

    const fitAllRunning = app.commands.isProfileFitJobRunning();
    app.dom.tableToolbar.innerHTML = `
      <button class="primary-button small" type="button" id="mfFitModelsBtn">Fit Models</button>`;

    // Left pane: filament list
    const selFil =
      app.state.modeling.modelFittingState.selectedFilamentId ||
      filamentIds[0] ||
      null;
    if (!app.state.modeling.modelFittingState.selectedFilamentId && selFil)
      app.state.modeling.modelFittingState.selectedFilamentId = selFil;

    const filListHtml = filamentIds
      .map((fid) => {
        const fil = app.commands.filamentMeta(fid) || {};
        const count = byFil[fid].length;
        const selected = fid === selFil;
        // Check if profile exists
        const hasProfile = fil.has_profile;
        const statusDot = hasProfile
          ? '<span class="mf-profile-dot mf-has-profile" title="Profile saved"></span>'
          : '<span class="mf-profile-dot" title="No profile yet"></span>';
        return `
        <div class="fi-filament-item${selected ? " is-selected" : ""}" data-mf-filament="${fid}">
          <span class="color-chip" style="background:${fil.hex || "#ddd"}"></span>
          <span class="fi-filament-name">${fil.color_name || fid}</span>
          ${statusDot}
          <span class="fi-sample-count">${count}</span>
        </div>`;
      })
      .join("");

    // Center pane: sample cards (compact list)
    const samples = selFil ? byFil[selFil] || [] : [];
    const selSample = app.state.modeling.modelFittingState.selectedSampleId;

    const sampleCardsHtml = samples
      .map((exp) => {
        const sid = exp.sample_id;
        const desc = app.commands._shortSampleDescription(exp);
        const selected = sid === selSample;
        const fixedLines = app.commands
          .sampleFilamentRoleLines(exp)
          .filter((line) => line.roleKind === "fixed");

        let fixedLayersHtml = "";
        if (fixedLines.length > 0) {
          fixedLayersHtml = fixedLines
            .map((line) => {
              const ft =
                line.thicknessMm != null ? line.thicknessMm.toFixed(2) : "?";
              return `<div class="fi-fixed-layer">${app.commands._escHtml(line.layerLabel)}: ${ft}mm ${app.commands._escHtml(line.name)} <span class="color-chip tiny" style="background:${line.hex || "#ddd"}"></span></div>`;
            })
            .join("");
        }

        return `
        <div class="fi-sample-card${selected ? " is-selected" : ""}" data-mf-sample="${sid}">
          <div class="fi-sample-header">
            <span class="mono fi-sample-id">${sid}</span>
            <span class="fi-sample-desc">${desc}</span>
          </div>
          ${fixedLayersHtml ? `<div class="fi-fixed-layers">${fixedLayersHtml}</div>` : ""}
          <div class="fi-strip-thumb">
            <img src="${app.commands.sampleThumbnailUrl(sid, "strip", true)}" alt="" onerror="this.style.display='none'">
          </div>
        </div>`;
      })
      .join("");

    // Right pane content — depends on whether predictions are loaded
    let rightPaneContent = "";
    let rightPaneTitle = "Fit Results";

    // Fetch predictions for selected filament (cached)
    let predictions = null;
    const needsPredictionFetch = !!(
      selFil &&
      !app.state.modeling.modelFittingState.predictionCache[selFil] &&
      !fitAllRunning
    );
    if (needsPredictionFetch) {
      app.commands._mfRenderLayout({
        filListHtml,
        sampleCardsHtml,
        samples,
        selSample,
        rightPaneTitle,
        rightPaneContent: app.commands._mfLoadingPlaceholder(
          "Loading fit results",
        ),
      });
    }

    if (selFil) {
      if (app.state.modeling.modelFittingState.predictionCache[selFil]) {
        predictions =
          app.state.modeling.modelFittingState.predictionCache[selFil];
      } else if (!fitAllRunning) {
        try {
          predictions = await app.api.fetchSamplePredictions(selFil);
          if (predictions.ok) {
            app.state.modeling.modelFittingState.predictionCache[selFil] =
              predictions;
          }
        } catch (e) {
          // No profile yet — that's ok
          predictions = null;
        }
      }
    }

    if (
      renderSeq !== app.state.modeling.modelFittingState.renderSeq ||
      app.state.navigation.currentMode !== "imageProcessing" ||
      app.state.navigation.currentSubtab !== "model_fitting" ||
      app.state.modeling.modelFittingState.selectedFilamentId !== selFil
    ) {
      return;
    }

    if (selSample && predictions && predictions.ok) {
      // Individual sample detail view
      const allPreds = [
        ...(predictions.groups.single || []),
        ...(predictions.groups.two_layer || []),
        ...(predictions.groups.three_layer || []),
      ];
      const pred = allPreds.find((p) => p.sample_id === selSample);
      const exp = samples.find((e) => e.sample_id === selSample);
      if (pred && exp) {
        rightPaneTitle = `${selSample} Detail`;
        rightPaneContent = app.commands._mfBuildDetailView(pred, exp);
      } else if (exp) {
        rightPaneTitle = `${selSample} Detail`;
        rightPaneContent = `<div class="mf-placeholder">No legacy spline prediction data available. Run Fit Models first.</div>`;
      }
    } else if (predictions && predictions.ok) {
      // Summary view — grouped sections
      const sectionOrder = [
        { key: "single", label: "Single Filament + Cross-cal" },
        { key: "two_layer", label: "2-Layer Strips" },
        { key: "three_layer", label: "3-Layer Strips" },
      ];

      let sectionsHtml = "";
      for (const sec of sectionOrder) {
        const preds = predictions.groups[sec.key] || [];
        if (preds.length === 0) continue;

        sectionsHtml += `<div class="mf-section">`;
        sectionsHtml += `<div class="mf-section-header">${sec.label} <span class="muted-line">(${preds.length})</span></div>`;
        sectionsHtml += `<div class="mf-section-cards">`;
        for (const pred of preds) {
          const exp = samples.find((e) => e.sample_id === pred.sample_id);
          if (exp) sectionsHtml += app.commands._mfBuildSampleCard(pred, exp);
        }
        sectionsHtml += `</div></div>`;
      }

      rightPaneContent =
        sectionsHtml ||
        '<div class="mf-placeholder">No legacy spline predictions available. Run Fit Models first.</div>';
    } else if (predictions && !predictions.has_profile) {
      rightPaneContent = `<div class="mf-placeholder">No legacy spline profile for this filament. Run Fit Models to generate profiles.</div>`;
    } else if (fitAllRunning) {
      rightPaneContent = `<div class="mf-placeholder">Profile fitting is running. Fit results will update when fitting completes.</div>`;
    } else {
      rightPaneContent = `<div class="mf-placeholder">Select a filament to view fit results.</div>`;
    }

    app.commands._mfRenderLayout({
      filListHtml,
      sampleCardsHtml,
      samples,
      selSample,
      rightPaneTitle,
      rightPaneContent,
    });
  }

  function _refreshPhotoStackPanel() {
    const panel = document.querySelector('[data-panel="photo-stack-fit"]');
    if (panel) {
      panel.outerHTML = app.commands._nmRenderCandidatePanel();
      app.commands._bindPhotoStackPanelActions();
    }
  }

  function _refreshCameraTransformPanel() {
    const panel = document.querySelector('[data-panel="camera-transform"]');
    if (panel) {
      panel.outerHTML = app.commands._ctRenderPanel();
      app.commands._bindCameraTransformActions();
    }
  }

  function _bindCameraTransformActions() {
    const refreshBtn = document.getElementById("ctRefreshBtn");
    if (refreshBtn) {
      refreshBtn.addEventListener("click", async () => {
        await app.commands._ctLoadCurrent(true);
        app.commands._refreshCameraTransformPanel();
      });
    }
  }

  function _bindPhotoStackPanelActions() {
    const nmRefreshBtn = document.getElementById("nmRefreshBtn");
    if (nmRefreshBtn) {
      nmRefreshBtn.addEventListener("click", async () => {
        await app.commands._nmLoadLatestCandidate(true);
        app.commands._refreshPhotoStackPanel();
      });
    }

    const nmSearchInput = document.getElementById("nmSearchInput");
    if (nmSearchInput) {
      nmSearchInput.addEventListener("input", () => {
        app.state.modeling.photoStackModelState.search =
          nmSearchInput.value || "";
        app.commands._refreshPhotoStackPanel();
        const refreshed = document.getElementById("nmSearchInput");
        refreshed?.focus();
      });
    }

    const nmEvidenceFilter = document.getElementById("nmEvidenceFilter");
    if (nmEvidenceFilter) {
      nmEvidenceFilter.addEventListener("change", () => {
        app.state.modeling.photoStackModelState.evidenceClass =
          nmEvidenceFilter.value || "all";
        app.commands._refreshPhotoStackPanel();
      });
    }
  }

  function _bindModelFittingActions() {
    const fitModelsBtn = document.getElementById("mfFitModelsBtn");
    if (fitModelsBtn) {
      fitModelsBtn.onclick = () =>
        app.commands.openFitModelsWorkflowFromModelFitting(fitModelsBtn);
    }

    // Filament selection
    app.dom.tableContainer
      .querySelectorAll("[data-mf-filament]")
      .forEach((el) => {
        el.addEventListener("click", () => {
          app.state.modeling.modelFittingState.selectedFilamentId =
            el.dataset.mfFilament;
          app.state.modeling.modelFittingState.selectedSampleId = null;
          app.commands.renderModelFitting();
        });
      });

    // Sample selection
    app.dom.tableContainer
      .querySelectorAll("[data-mf-sample]")
      .forEach((el) => {
        el.addEventListener("click", () => {
          const sid = el.dataset.mfSample;
          app.state.modeling.modelFittingState.selectedSampleId =
            app.state.modeling.modelFittingState.selectedSampleId === sid
              ? null
              : sid;
          app.commands.renderModelFitting();
        });
      });

    // All Samples card
    const allCard = document.getElementById("mfAllSamplesCard");
    if (allCard) {
      allCard.addEventListener("click", () => {
        app.state.modeling.modelFittingState.selectedSampleId = null;
        app.commands.renderModelFitting();
      });
    }

    // Back to grid
    const backLink = document.getElementById("mfBackToGrid");
    if (backLink) {
      backLink.addEventListener("click", (e) => {
        e.preventDefault();
        app.state.modeling.modelFittingState.selectedSampleId = null;
        app.commands.renderModelFitting();
      });
    }
  }

  Object.assign(app.commands, {
    _getProcessedByFilament,
    _shortSampleDescription,
    _mfBuildMockStrip,
    _mfBuildDiagram,
    _mfBuildDeltaEBars,
    _nmEvidenceLabel,
    _nmSampleSearchText,
    _nmFilteredSamples,
    _nmBuildChipStrip,
    _nmPredictionSpecs,
    _nmBuildPredictionChipStrip,
    _nmDeltaClass,
    _nmBuildDeltaPills,
    _nmLayerLabel,
    _nmSampleNumber,
    _nmBuildDiagramExp,
    _nmBuildDiagramLabels,
    _nmBuildStackDiagram,
    _nmBuildSampleRow,
    _nmRenderProgressPanel,
    _nmUpdateProgressOnly,
    _nmRenderCandidatePanel,
    _ctRenderProgressPanel,
    _ctRenderPanel,
    _ctLoadCurrent,
    pollCameraTransformJob,
    _nmLoadLatestCandidate,
    pollPhotoStackJob,
    _mfBuildSampleCard,
    _mfBuildDetailView,
    _mfLoadingPlaceholder,
    _mfRenderLayout,
    refreshModelsAfterWorkflow,
    openFitModelsWorkflow,
    openFitModelsWorkflowFromModelFitting,
    renderModelFitting,
    _refreshPhotoStackPanel,
    _refreshCameraTransformPanel,
    _bindCameraTransformActions,
    _bindPhotoStackPanelActions,
    _bindModelFittingActions,
  });
}
