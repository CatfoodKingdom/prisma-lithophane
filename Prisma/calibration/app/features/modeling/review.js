/** Install features/modeling/review commands. */
export function installFeaturesModelingReview(app) {
  function loadModelingDetailSettings() {
    const defaults = { includeCorrections: true, domain: "appearance" };
    try {
      const raw = window.sessionStorage?.getItem(
        app.constants.MODELING_DETAIL_SETTINGS_KEY,
      );
      if (!raw) return defaults;
      const parsed = JSON.parse(raw);
      return {
        includeCorrections: parsed.includeCorrections !== false,
        domain:
          parsed.domain === "transmission" ? "transmission" : "appearance",
      };
    } catch {
      return defaults;
    }
  }

  function persistModelingDetailSettings() {
    try {
      window.sessionStorage?.setItem(
        app.constants.MODELING_DETAIL_SETTINGS_KEY,
        JSON.stringify(app.state.modeling.modelingDetailSettings),
      );
    } catch {
      // Session persistence is a convenience; rendering should continue if unavailable.
    }
  }

  function modelOverviewStatusMeta(entry = {}) {
    const raw = String(
      entry.status || entry.model_currentness?.currentness_state || "missing",
    ).toLowerCase();
    if (raw === "current") return { label: "Current", cls: "is-current" };
    if (raw === "stale") return { label: "Stale", cls: "is-stale" };
    if (raw === "missing") return { label: "Missing", cls: "is-missing" };
    if (raw === "invalid" || raw === "failed")
      return { label: "Failed", cls: "is-failed" };
    return { label: "Unknown", cls: "is-unknown" };
  }

  function modelOverviewDateText(entry = {}) {
    const raw =
      entry.generated_at || entry.model_currentness?.generated_at || "";
    const text = String(raw || "");
    const isoDate = text.match(/^(\d{4}-\d{2}-\d{2})/);
    if (isoDate) return isoDate[1];
    if (!text) return "";
    const parsed = new Date(text);
    if (Number.isNaN(parsed.getTime())) return "";
    const year = parsed.getFullYear();
    const month = String(parsed.getMonth() + 1).padStart(2, "0");
    const day = String(parsed.getDate()).padStart(2, "0");
    return `${year}-${month}-${day}`;
  }

  function modelOverviewStatusTitle(label, entry = {}) {
    const meta = app.commands.modelOverviewStatusMeta(entry);
    const parts = [`${label}: ${meta.label}`];
    if (entry.generated_at) parts.push(`Generated ${entry.generated_at}`);
    const staleReason =
      entry.stale_reason || entry.model_currentness?.stale_reason;
    if (staleReason) parts.push(staleReason);
    return parts.join(" | ");
  }

  function renderModelOverviewStatusLine(
    modelStatus = app.state.session.data.model_status,
  ) {
    const models = modelStatus?.models || {};
    return app.constants.MODEL_OVERVIEW_ORDER.map(({ key, label }) => {
      const entry = models[key] || { status: "missing" };
      const meta = app.commands.modelOverviewStatusMeta(entry);
      const date = app.commands.modelOverviewDateText(entry);
      return `
        <span class="model-status-text ${meta.cls}" title="${app.commands._escAttr(app.commands.modelOverviewStatusTitle(label, entry))}">
          <strong>${app.commands._escHtml(label)}:</strong>
          <span><span class="model-status-state">${app.commands._escHtml(meta.label.toUpperCase())}</span>${date ? ` (${app.commands._escHtml(date)})` : ""}</span>
        </span>
      `;
    }).join("");
  }

  function renderModelOverviewHeaderStatus() {
    return app.commands.renderModelOverviewStatusLine(
      app.state.session.data.model_status,
    );
  }

  function renderModelOverviewStatusBlock(
    modelStatus = app.state.session.data.model_status,
  ) {
    return app.commands.renderModelOverviewStatusLine(
      modelStatus?.models ? modelStatus : app.state.session.data.model_status,
    );
  }

  function invalidateModelingPayloads() {
    app.state.modeling.modelingState.overview = null;
    app.state.modeling.modelingState.samples = null;
    app.state.modeling.modelingState.filaments = null;
    app.state.modeling.modelingState.detailSamplePayload = null;
    app.state.modeling.modelingState.detailFilamentPayload = null;
    app.state.modeling.modelingState.error = "";
  }

  function modelingCurrentTab() {
    return app.state.navigation.currentSubtab || "overview";
  }

  function modelingPayloadForTab(tab) {
    if (tab === "samples") return app.state.modeling.modelingState.samples;
    if (tab === "filaments") return app.state.modeling.modelingState.filaments;
    return app.state.modeling.modelingState.overview;
  }

  function setModelingPayloadForTab(tab, payload) {
    if (tab === "samples") app.state.modeling.modelingState.samples = payload;
    else if (tab === "filaments")
      app.state.modeling.modelingState.filaments = payload;
    else app.state.modeling.modelingState.overview = payload;
    if (payload?.model_status)
      app.state.session.data.model_status = payload.model_status;
  }

  async function fetchAllModelingSamples(options = {}) {
    const firstPage = await app.api.fetchModelingSamples({
      ...options,
      offset: 0,
      limit: app.constants.MODELING_REVIEW_PAGE_SIZE,
    });
    const rows = [...(firstPage.rows || [])];
    const total = Number(firstPage.total || rows.length);
    let offset = rows.length;

    while (offset < total) {
      const page = await app.api.fetchModelingSamples({
        ...options,
        offset,
        limit: app.constants.MODELING_REVIEW_PAGE_SIZE,
      });
      const pageRows = page.rows || [];
      if (!pageRows.length) break;
      rows.push(...pageRows);
      offset += pageRows.length;
    }

    return {
      ...firstPage,
      offset: 0,
      limit: rows.length,
      rows,
    };
  }

  async function loadModelingTab(
    tab = app.commands.modelingCurrentTab(),
    options = {},
  ) {
    if (app.state.modeling.modelingState.loadingTab === tab && !options.force)
      return;
    app.state.modeling.modelingState.loadingTab = tab;
    app.state.modeling.modelingState.error = "";
    if (app.state.navigation.currentMode === "profiles")
      app.commands.renderModelsView({ skipEnsure: true });
    try {
      let payload;
      if (tab === "samples") {
        payload = await app.commands.fetchAllModelingSamples({
          filter: app.state.modeling.modelingState.samplesFilter,
          filament_ids: app.state.modeling.modelingState.samplesFilamentIds,
          sort: app.state.modeling.modelingState.samplesSort,
          sort_dir: app.state.modeling.modelingState.samplesSortDir,
        });
      } else if (tab === "filaments") {
        payload = await app.api.fetchModelingFilaments({
          sort: app.state.modeling.modelingState.filamentsSort,
          sort_dir: app.state.modeling.modelingState.filamentsSortDir,
          limit: 500,
        });
      } else {
        payload = await app.api.fetchModelingOverview();
      }
      app.commands.setModelingPayloadForTab(tab, payload);
    } catch (err) {
      app.state.modeling.modelingState.error =
        err.message || "Failed to load Modeling data";
    } finally {
      if (app.state.modeling.modelingState.loadingTab === tab)
        app.state.modeling.modelingState.loadingTab = null;
      if (app.state.navigation.currentMode === "profiles")
        app.commands.renderModelsView({ skipEnsure: true });
    }
  }

  function ensureModelingTabLoaded(tab = app.commands.modelingCurrentTab()) {
    if (
      app.commands.modelingPayloadForTab(tab) ||
      app.state.modeling.modelingState.loadingTab === tab
    )
      return;
    app.commands.loadModelingTab(tab);
  }

  function modelStatusAttentionHtml() {
    const models = app.state.session.data.model_status?.models || {};
    const stale = Object.values(models).filter(
      (entry) => entry.status === "stale",
    );
    const missing = Object.values(models).filter(
      (entry) => entry.status === "missing",
    );
    if (!stale.length && !missing.length) return "";
    const parts = [];
    if (stale.length) parts.push(`${stale.length} stale`);
    if (missing.length) parts.push(`${missing.length} missing`);
    return `
      <div class="model-review-alert">
        <strong>Fit Models needed</strong>
        <span>${app.commands._escHtml(parts.join(" · "))}</span>
      </div>
    `;
  }

  function modelingLoadingHtml(tab) {
    if (app.state.modeling.modelingState.error) {
      return `<div class="model-review-empty is-error">${app.commands._escHtml(app.state.modeling.modelingState.error)}</div>`;
    }
    if (app.state.modeling.modelingState.loadingTab === tab) {
      return `<div class="model-review-empty">Loading ${app.commands._escHtml(tab)}...</div>`;
    }
    return `<div class="model-review-empty">No Modeling data loaded yet.</div>`;
  }

  function modelReviewOverviewTableHtml(rows = [], emptyMessage = "No rows") {
    const body = rows.length
      ? rows
          .map(
            (row) => `
      <tr>
        <td><strong>${app.commands._escHtml(row.label || "")}</strong></td>
        <td>${app.commands._escHtml(row.value || "")}</td>
        <td>${row.detail ? app.commands._escHtml(row.detail) : `<span class="muted-line">None</span>`}</td>
      </tr>
    `,
          )
          .join("")
      : `<tr><td colspan="3"><div class="model-review-empty">${app.commands._escHtml(emptyMessage)}</div></td></tr>`;
    return `
      <table class="data-table compact-table model-review-summary-table model-review-overview-table">
        <thead>
          <tr>
            <th>Item</th>
            <th>Value</th>
            <th>Detail</th>
          </tr>
        </thead>
        <tbody>${body}</tbody>
      </table>
    `;
  }

  function renderModelingOverview(payload) {
    if (!payload) return app.commands.modelingLoadingHtml("overview");
    const summary = payload.inclusion_summary || {};
    const attention = payload.attention || {};
    const stale = attention.stale_models || [];
    const missing = attention.missing_models || [];
    const filamentBlocked = Number(
      attention.samples_with_excluded_filaments ||
        summary.samples_blocked_by_filament ||
        0,
    );
    const evidenceRows = [
      {
        label: "Evidence Samples",
        value: `${Number(summary.samples_included || 0)} / ${Number(summary.samples_total || 0)}`,
        detail: "with included swatches",
      },
      {
        label: "Swatches",
        value: `${Number(summary.swatches_included || 0)} / ${Number(summary.swatches_total || 0)}`,
        detail: "included",
      },
      {
        label: "Excluded Samples",
        value: String(Number(summary.samples_excluded || 0)),
        detail: "",
      },
      {
        label: "Excluded Swatches",
        value: String(Number(summary.swatches_excluded || 0)),
        detail: "",
      },
    ];
    const attentionRows = [
      ...stale.map((item) => ({
        label: item.label || item.model_kind,
        value: "Stale",
        detail: item.reason || "Model needs refitting",
      })),
      ...missing.map((item) => ({
        label: item.label || item.model_kind,
        value: "Missing",
        detail: "No current model artifact",
      })),
      ...(filamentBlocked
        ? [
            {
              label: "Samples blocked by excluded filaments",
              value: String(filamentBlocked),
              detail: "",
            },
          ]
        : []),
      ...(Number(attention.samples_without_accepted_extraction || 0)
        ? [
            {
              label: "Samples without accepted extraction",
              value: String(
                Number(attention.samples_without_accepted_extraction || 0),
              ),
              detail: "",
            },
          ]
        : []),
    ];
    return `
      <div class="model-review-page model-review-overview-page">
        ${app.commands.modelStatusAttentionHtml()}
        <section class="model-review-section">
          <div class="model-review-section-head">
            <h3>Current Evidence</h3>
          </div>
          ${app.commands.modelReviewOverviewTableHtml(evidenceRows)}
        </section>
        <section class="model-review-section">
          <div class="model-review-section-head">
            <h3>Needs Attention</h3>
          </div>
          ${app.commands.modelReviewOverviewTableHtml(attentionRows, "No immediate blockers.")}
        </section>
      </div>
    `;
  }

  function modelReviewStripHtml(colors = []) {
    const swatches = (colors || []).slice(0, 24);
    if (!swatches.length || !swatches.some(Boolean)) {
      return `<span class="model-review-strip is-empty"></span>`;
    }
    return `
      <span class="model-review-strip" aria-hidden="true">
        ${swatches.map((hex) => `<span style="background:${app.commands._escAttr(hex || "#f1f1f1")}"></span>`).join("")}
      </span>
    `;
  }

  function modelReviewDetailStripHtml(colors = []) {
    const swatches = colors || [];
    if (!swatches.length || !swatches.some(Boolean)) {
      return `<span class="model-review-detail-strip is-empty"></span>`;
    }
    return `
      <span class="model-review-detail-strip" aria-hidden="true">
        ${swatches.map((hex) => `<span style="background:${app.commands._escAttr(hex || "#f1f1f1")}"></span>`).join("")}
      </span>
    `;
  }

  function modelReviewDetailSwatchCount(sample = {}, detail = {}) {
    const counts = [
      Number(sample.swatch_count || 0),
      (sample.geometry?.variable_thicknesses_mm || []).length,
    ];
    Object.values(detail.domains || {}).forEach((domainPayload) => {
      ["measured", "photo_stack_v2", "legacy_spline"].forEach((key) => {
        const series = domainPayload?.[key] || {};
        if (Array.isArray(series.hex)) counts.push(series.hex.length);
        ["corrected", "uncorrected"].forEach((variant) => {
          const variantSeries = series?.[variant] || {};
          if (Array.isArray(variantSeries.hex))
            counts.push(variantSeries.hex.length);
        });
      });
    });
    return Math.max(
      1,
      ...counts.map((count) => (Number.isFinite(count) ? count : 0)),
    );
  }

  function modelReviewStripAlignmentStyle(swatchCount) {
    const n = Math.max(1, Number(swatchCount || 0));
    const borderMm = 3;
    const stepWMm = 12;
    const totalMm = 2 * borderMm + n * stepWMm;
    const left = (borderMm / totalMm) * 100;
    const width = ((n * stepWMm) / totalMm) * 100;
    return `--model-render-left:${left}%;--model-render-width:${width}%;`;
  }

  function modelReviewNormalizeHexes(series = {}, swatchCount = 1) {
    const source = Array.isArray(series.hex) ? series.hex : [];
    const count = Math.max(1, Number(swatchCount || source.length || 1));
    return Array.from({ length: count }, (_item, index) => source[index] || "");
  }

  function modelReviewDomainStripHtml(series = {}, swatchCount = 1) {
    const hexes = app.commands.modelReviewNormalizeHexes(series, swatchCount);
    if (!hexes.some(Boolean)) {
      return `<span class="model-review-domain-strip is-empty" aria-hidden="true"></span>`;
    }
    return `
      <span class="model-review-domain-strip" style="grid-template-columns:repeat(${hexes.length}, minmax(0, 1fr))" aria-hidden="true">
        ${hexes.map((hex) => `<span class="${hex ? "" : "is-missing"}" style="background:${app.commands._escAttr(hex || "#eef1f3")}"></span>`).join("")}
      </span>
    `;
  }

  function modelReviewLinearTriplet(value) {
    if (!Array.isArray(value) || value.length < 3) return null;
    const triplet = value.slice(0, 3).map((item) => Number(item));
    if (triplet.some((item) => !Number.isFinite(item))) return null;
    return triplet.map((item) => Math.max(0, Math.min(1, item)));
  }

  function modelReviewLinearToOklab(value) {
    const rgb = app.commands.modelReviewLinearTriplet(value);
    if (!rgb) return null;
    const [r, g, b] = rgb;
    const l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b;
    const m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b;
    const s = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b;
    const lRoot = Math.cbrt(l);
    const mRoot = Math.cbrt(m);
    const sRoot = Math.cbrt(s);
    return {
      L: 0.2104542553 * lRoot + 0.793617785 * mRoot - 0.0040720468 * sRoot,
      a: 1.9779984951 * lRoot - 2.428592205 * mRoot + 0.4505937099 * sRoot,
      b: 0.0259040371 * lRoot + 0.7827717662 * mRoot - 0.808675766 * sRoot,
    };
  }

  function modelReviewHexToOklab(hex) {
    const text = String(hex || "").trim();
    const match = text.match(/^#?([0-9a-fA-F]{6})$/);
    if (!match) return null;
    const srgb = [0, 2, 4].map(
      (offset) => parseInt(match[1].slice(offset, offset + 2), 16) / 255,
    );
    const linear = srgb.map((value) =>
      value <= 0.04045 ? value / 12.92 : Math.pow((value + 0.055) / 1.055, 2.4),
    );
    return app.commands.modelReviewLinearToOklab(linear);
  }

  function modelReviewOklabDeltaFromLabs(labA, labB) {
    if (!labA || !labB) return null;
    const dL = labA.L - labB.L;
    const da = labA.a - labB.a;
    const db = labA.b - labB.b;
    return Math.sqrt(dL * dL + da * da + db * db);
  }

  function modelReviewSeriesOklab(
    series = {},
    domain = "appearance",
    index = 0,
  ) {
    if (domain === "transmission") {
      const rgb = Array.isArray(series.rgb) ? series.rgb[index] : null;
      return app.commands.modelReviewLinearToOklab(rgb);
    }
    const hex = Array.isArray(series.hex) ? series.hex[index] : "";
    return app.commands.modelReviewHexToOklab(hex);
  }

  function modelReviewOklabErrors(
    measuredSeries = {},
    predictedSeries = {},
    swatchCount = 1,
    domain = "appearance",
  ) {
    const measured = app.commands.modelReviewNormalizeHexes(
      measuredSeries,
      swatchCount,
    );
    return measured.map((_hex, index) =>
      app.commands.modelReviewOklabDeltaFromLabs(
        app.commands.modelReviewSeriesOklab(measuredSeries, domain, index),
        app.commands.modelReviewSeriesOklab(predictedSeries, domain, index),
      ),
    );
  }

  function modelReviewFormatOklabError(value) {
    return Number.isFinite(value) ? value.toFixed(3) : "-";
  }

  function modelReviewOklabErrorStats(errors = []) {
    const finite = errors.filter((value) => Number.isFinite(value));
    if (!finite.length) return null;
    return {
      mean: finite.reduce((sum, value) => sum + value, 0) / finite.length,
      max: Math.max(...finite),
    };
  }

  function modelReviewOklabErrorGraphHtml(
    v2Errors = [],
    v1Errors = [],
    swatchCount = 1,
  ) {
    const count = Math.max(
      1,
      Number(swatchCount || v2Errors.length || v1Errors.length || 1),
    );
    const v2Values = Array.from(
      { length: count },
      (_item, index) => v2Errors[index],
    );
    const v1Values = Array.from(
      { length: count },
      (_item, index) => v1Errors[index],
    );
    const finite = [...v2Values, ...v1Values].filter((value) =>
      Number.isFinite(value),
    );
    if (!finite.length) {
      return `
        <div class="model-review-domain-error is-empty">
          <div class="model-review-domain-error-summary">
            <span>OKLab error</span>
            <span>unavailable</span>
          </div>
          <div class="model-review-domain-error-empty">No comparable prediction</div>
        </div>
      `;
    }
    const maxValue = app.constants.MODEL_REVIEW_OKLAB_ERROR_SCALE_MAX;
    const landmarks = app.constants.MODEL_REVIEW_OKLAB_ERROR_LANDMARKS.map(
      (mark) => {
        const bottomPct = Math.max(
          0,
          Math.min(100, (mark.value / maxValue) * 100),
        );
        return `
        <div class="model-review-domain-error-landmark" style="bottom:${bottomPct}%"></div>
      `;
      },
    ).join("");
    const axisLabels = [
      {
        value: maxValue,
        label: app.commands.modelReviewFormatOklabError(maxValue),
      },
      ...app.constants.MODEL_REVIEW_OKLAB_ERROR_LANDMARKS,
      { value: 0, label: "0" },
    ]
      .map((mark) => {
        const bottomPct = Math.max(
          0,
          Math.min(100, (mark.value / maxValue) * 100),
        );
        return `<span style="bottom:${bottomPct}%">${app.commands._escHtml(mark.label)}</span>`;
      })
      .join("");
    const pairedBars = Array.from({ length: count }, (_item, index) => {
      const v2 = v2Values[index];
      const v1 = v1Values[index];
      const bar = (value, cls, label) => {
        const heightPct = Number.isFinite(value)
          ? Math.max(4, Math.min(100, (value / maxValue) * 100))
          : 0;
        const clipNote =
          Number.isFinite(value) && value > maxValue
            ? " (clipped to scale)"
            : "";
        const title = Number.isFinite(value)
          ? `Swatch ${index + 1}: ${label} OKLab error ${app.commands.modelReviewFormatOklabError(value)}${clipNote}`
          : `Swatch ${index + 1}: ${label} OKLab error unavailable`;
        return `<span class="model-review-domain-error-bar ${cls}${Number.isFinite(value) ? "" : " is-missing"}" style="height:${heightPct}%" title="${app.commands._escAttr(title)}"></span>`;
      };
      return `
        <span class="model-review-domain-error-pair">
          ${bar(v2, "is-v2", "Color Model V2")}
          ${bar(v1, "is-v1", "Color Model V1")}
        </span>
      `;
    }).join("");
    return `
      <div class="model-review-domain-error">
        <div class="model-review-domain-error-summary">
          <span>OKLab error</span>
          <span><i class="model-review-domain-error-key is-v2"></i>V2 · <i class="model-review-domain-error-key is-v1"></i>V1</span>
        </div>
        <div class="model-review-domain-error-plot" aria-label="OKLab prediction error graph">
          <div class="model-review-domain-error-axis" aria-hidden="true">${axisLabels}</div>
          <div class="model-review-domain-error-stage">
            ${landmarks}
            <div class="model-review-domain-error-bars" style="grid-template-columns:repeat(${count}, minmax(0, 1fr))">
              ${pairedBars}
            </div>
          </div>
        </div>
      </div>
    `;
  }

  function modelReviewDomainSeriesHtml(label, series, swatchCount) {
    const payload = series || {};
    const reason =
      !payload.available && payload.reason
        ? `<span class="model-review-domain-reason">${app.commands._escHtml(payload.reason)}</span>`
        : "";
    return `
      <div class="model-review-domain-series">
        <div class="model-review-domain-series-label">${app.commands._escHtml(label)}</div>
        <div class="model-review-domain-strip-track">
          <div class="model-review-domain-strip-inner">
            ${app.commands.modelReviewDomainStripHtml(payload, swatchCount)}
          </div>
        </div>
        ${reason}
      </div>
    `;
  }

  function modelReviewDomainErrorSeriesHtml(v2Errors, v1Errors, swatchCount) {
    return `
      <div class="model-review-domain-series model-review-domain-error-series">
        <div class="model-review-domain-series-label">Prediction Error</div>
        <div class="model-review-domain-strip-track">
          <div class="model-review-domain-strip-inner">
            ${app.commands.modelReviewOklabErrorGraphHtml(v2Errors, v1Errors, swatchCount)}
          </div>
        </div>
      </div>
    `;
  }

  function modelReviewDomainPanelHtml(
    sample,
    detail,
    domain,
    variant,
    swatchCount,
  ) {
    const domainPayload = (detail.domains || {})[domain] || {};
    const measured = domainPayload.measured || {};
    const photoStack = (domainPayload.photo_stack_v2 || {})[variant] || {};
    const legacySpline = (domainPayload.legacy_spline || {})[variant] || {};
    const domainName = domain === "appearance" ? "Appearance" : "Transmission";
    const sampleId = sample.sample_id || "";
    const stripSrc =
      domain === "appearance" && sampleId
        ? app.commands.sampleThumbnailUrl(sampleId, "strip")
        : "";
    const photoStackErrors = app.commands.modelReviewOklabErrors(
      measured,
      photoStack,
      swatchCount,
      domain,
    );
    const legacySplineErrors = app.commands.modelReviewOklabErrors(
      measured,
      legacySpline,
      swatchCount,
      domain,
    );
    const referenceBlock = stripSrc
      ? `
          <div class="model-review-domain-image-block">
            <div class="model-review-domain-series-label">Extracted Strip</div>
            <div class="model-review-domain-image">
              <img src="${app.commands._escAttr(stripSrc)}"
                   alt="${app.commands._escAttr(`${sampleId} extracted strip`)}"
                   data-model-strip-source
                   onerror="this.closest('.model-review-domain-image-block').remove()">
            </div>
          </div>
    `
      : `
          <div class="model-review-domain-reference-block">
            <div class="model-review-domain-series-label">Strip</div>
            <div class="model-review-domain-reference-shell">
              <div class="sample-strip-tight">${app.commands.modelReviewStripDiagramHtml(sample)}</div>
            </div>
          </div>
    `;

    return `
      <section class="model-review-domain-panel" aria-label="${app.commands._escAttr(domainName)} Domain">
        <h4 class="model-review-domain-title">${app.commands._escHtml(domainName)} Domain</h4>
        <div class="model-review-domain-strip-sync"
             data-model-strip-sync
             data-border-mm="3"
             data-step-w-mm="12"
             data-swatches="${app.commands._escAttr(String(swatchCount))}"
             style="${app.commands.modelReviewStripAlignmentStyle(swatchCount)}">
          ${referenceBlock}
          ${app.commands.modelReviewDomainSeriesHtml(`Measured ${domainName}`, measured, swatchCount)}
          ${app.commands.modelReviewDomainSeriesHtml(`Predicted ${domainName} (Color Model V2)`, photoStack, swatchCount)}
          ${app.commands.modelReviewDomainSeriesHtml(`Predicted ${domainName} (Color Model V1)`, legacySpline, swatchCount)}
          ${app.commands.modelReviewDomainErrorSeriesHtml(photoStackErrors, legacySplineErrors, swatchCount)}
        </div>
      </section>
    `;
  }

  function applyModelReviewStripGeometry(img) {
    const sync = img?.closest?.("[data-model-strip-sync]");
    if (!sync || !img.naturalWidth || !img.naturalHeight) return;
    const sw = Number(img.naturalWidth);
    const borderMm = Number(sync.dataset.borderMm || 3);
    const stepWMm = Number(sync.dataset.stepWMm || 12);
    const n = Math.max(1, Number(sync.dataset.swatches || 8));
    const deskewPad = 6;
    const totalWmm = 2 * borderMm + n * stepWMm;
    const plasticWPx = Math.max(1, sw - 2 * deskewPad);
    const pxPerMm = plasticWPx / totalWmm;
    const innerX = Math.round(deskewPad + borderMm * pxPerMm);
    const innerW = Math.round(n * stepWMm * pxPerMm);
    sync.style.setProperty("--model-render-left", `${(innerX / sw) * 100}%`);
    sync.style.setProperty("--model-render-width", `${(innerW / sw) * 100}%`);
  }

  function bindModelReviewStripGeometry(root = app.dom.detailSidebar) {
    root?.querySelectorAll?.("img[data-model-strip-source]").forEach((img) => {
      if (img.complete && img.naturalWidth) {
        app.commands.applyModelReviewStripGeometry(img);
      } else {
        img.addEventListener(
          "load",
          () => app.commands.applyModelReviewStripGeometry(img),
          { once: true },
        );
      }
    });
  }

  function modelReviewFilamentStackHtml(filaments = []) {
    if (!filaments.length)
      return `<span class="muted-line">No filaments</span>`;
    return filaments
      .map(
        (fil) => `
      <div class="model-review-stack-line">
        <span class="color-chip" style="background:${app.commands._escAttr(fil.hex || "#999999")}"></span>
        <span>${app.commands._escHtml(fil.name || fil.filament_id || "")}</span>
      </div>
    `,
      )
      .join("");
  }

  function modelReviewSampleFilamentRoleLabel(fil = {}) {
    if (fil.role_kind === "variable") return "Variable";
    if (fil.role_kind === "fixed") {
      const fixedThickness = Number(fil.fixed_thickness_mm);
      return Number.isFinite(fixedThickness)
        ? `Fixed ${fixedThickness.toFixed(2)} mm`
        : "Fixed";
    }
    return fil.role_label || "";
  }

  function modelReviewSampleFilamentsHtml(sample = {}) {
    const filaments = sample.filaments || [];
    if (!filaments.length) return "";
    return `
      <div class="model-review-sample-filaments" aria-label="Sample filaments">
        <span class="model-review-sample-filaments-label">Filaments</span>
        <div class="model-review-sample-filament-chips">
          ${filaments
            .map((fil) => {
              const name =
                fil.name || fil.display_name || fil.filament_id || "";
              const roleLabel =
                app.commands.modelReviewSampleFilamentRoleLabel(fil);
              return `
              <span class="model-review-sample-filament-chip">
                <span class="color-chip" style="background:${app.commands._escAttr(fil.hex || "#999999")}"></span>
                <span class="model-review-sample-filament-name">${app.commands._escHtml(name)}</span>
                ${roleLabel ? `<span class="model-review-sample-filament-role">${app.commands._escHtml(roleLabel)}</span>` : ""}
                ${app.commands.modelReviewFilamentExclusionPillHtml(fil)}
              </span>
            `;
            })
            .join("")}
        </div>
      </div>
    `;
  }

  function modelReviewStripDiagramHtml(row) {
    const expLike = {
      roles: row?.filaments || [],
      variable_thicknesses_mm: row?.geometry?.variable_thicknesses_mm || [],
    };
    return app.commands.buildStripMiniTable(expLike);
  }

  function modelReviewExcludedFilaments(row = {}) {
    const payload = Array.isArray(row.excluded_model_filaments)
      ? row.excluded_model_filaments
      : [];
    if (payload.length) return payload;
    return (row.filaments || []).filter((fil) => fil.exclude_from_model);
  }

  function modelReviewExcludedFilamentText(row = {}) {
    const names = app.commands
      .modelReviewExcludedFilaments(row)
      .map((fil) => fil.name || fil.display_name || fil.filament_id)
      .filter(Boolean);
    if (!names.length) return "";
    return names.join(", ");
  }

  function modelReviewFitStateHtml(row) {
    const excludedFilamentText =
      app.commands.modelReviewExcludedFilamentText(row);
    if (excludedFilamentText) {
      const sampleNote = row.fit_exclude ? " Sample is also excluded." : "";
      return `<span class="status-pill model-review-state incomplete" title="Model fitting blocked by excluded filament: ${app.commands._escAttr(excludedFilamentText)}.${app.commands._escAttr(sampleNote)}">Filament Excluded</span>`;
    }
    if (row.fit_exclude)
      return `<span class="status-pill model-review-state planned">Excluded</span>`;
    if (Number(row.excluded_swatch_count || 0) > 0) {
      return `<span class="status-pill model-review-state stale">${Number(row.excluded_swatch_count || 0)} swatch${Number(row.excluded_swatch_count || 0) === 1 ? "" : "es"} excluded</span>`;
    }
    return `<span class="status-pill model-review-state processed">Included</span>`;
  }

  function modelReviewExcludedSwatchSet(sample = {}) {
    return new Set(
      (sample.excluded_swatches || [])
        .map((idx) => Number(idx))
        .filter((idx) => Number.isInteger(idx) && idx >= 0),
    );
  }

  function modelReviewSwatchThicknessLabel(sample = {}, index = 0) {
    const thicknesses = sample.geometry?.variable_thicknesses_mm || [];
    const value = Number(thicknesses[index]);
    return Number.isFinite(value) ? value.toFixed(2) : String(index + 1);
  }

  function modelReviewSampleExclusionGeometryHtml(
    sample = {},
    swatchCount = 1,
  ) {
    const roles = [...(sample.filaments || [])].sort(
      (a, b) => Number(b.role_index || 0) - Number(a.role_index || 0),
    );
    const variableRole =
      roles.find((role) => role.role_kind === "variable") || {};
    const variableHex = variableRole.hex || "#dddddd";
    const variableText = app.commands.textColor(variableHex);
    const excluded = app.commands.modelReviewExcludedSwatchSet(sample);
    const count = Math.max(
      1,
      Number(
        swatchCount ||
          sample.swatch_count ||
          (sample.geometry?.variable_thicknesses_mm || []).length ||
          1,
      ),
    );
    const variableCells = Array.from({ length: count }, (_item, index) => {
      const isExcluded = excluded.has(index);
      const label = app.commands.modelReviewSwatchThicknessLabel(sample, index);
      return `
        <td>
          <button class="model-swatch-exclusion-cell ${isExcluded ? "is-excluded" : "is-included"}"
                  type="button"
                  data-model-toggle-swatch="${index}"
                  data-next-excluded="${isExcluded ? "false" : "true"}"
                  style="--swatch-bg:${app.commands._escAttr(variableHex)};--swatch-fg:${app.commands._escAttr(variableText)}"
                  title="Swatch ${index + 1}: ${isExcluded ? "excluded" : "included"} in model fits"
                  aria-pressed="${isExcluded ? "true" : "false"}">
            <span class="model-swatch-exclusion-label">${app.commands._escHtml(label)}</span>
            <span class="model-swatch-exclusion-status ${isExcluded ? "is-excluded" : "is-included"}">${isExcluded ? "Exclude" : "Include"}</span>
          </button>
        </td>
      `;
    }).join("");
    const rows = roles.length
      ? roles
          .map((role) => {
            if (role.role_kind === "variable")
              return `<tr>${variableCells}</tr>`;
            const fixedHex = role.hex || "#eeeeee";
            const fixedText = app.commands.textColor(fixedHex);
            const fixedThickness = Number(role.fixed_thickness_mm);
            const fixedLabel = Number.isFinite(fixedThickness)
              ? `${fixedThickness.toFixed(2)}mm`
              : role.role_label || "Fixed";
            return `
        <tr>
          <td colspan="${count}">
            <span class="model-swatch-exclusion-fixed" style="background:${app.commands._escAttr(fixedHex)};color:${app.commands._escAttr(fixedText)}">${app.commands._escHtml(fixedLabel)}</span>
          </td>
        </tr>
      `;
          })
          .join("")
      : `<tr>${variableCells}</tr>`;
    return `<table class="model-swatch-exclusion-table" aria-label="Sample swatch fit inclusion">${rows}</table>`;
  }

  function modelReviewSampleExclusionHtml(sample = {}, swatchCount = 1) {
    const sampleId = sample.sample_id || "";
    const excludedFilamentText =
      app.commands.modelReviewExcludedFilamentText(sample);
    const fitExcluded = !!sample.fit_exclude;
    const swatchExcludedCount = Number(
      sample.excluded_swatch_count ||
        app.commands.modelReviewExcludedSwatchSet(sample).size ||
        0,
    );
    return `
      <div class="model-sample-exclusion-panel">
        <button class="model-fit-control-button ${fitExcluded ? "is-include" : "is-exclude"}"
                type="button"
                id="modelSampleFitToggle"
                data-model-toggle-sample="${app.commands._escAttr(sampleId)}"
                data-next-fit-exclude="${fitExcluded ? "false" : "true"}">
          ${fitExcluded ? "Include Sample" : "Exclude Sample"}
        </button>
        <div class="model-sample-exclusion-summary">
          <strong>${fitExcluded ? "Sample excluded from fits" : "Sample included in fits"}</strong>
          <span>${swatchExcludedCount ? `${swatchExcludedCount} swatch${swatchExcludedCount === 1 ? "" : "es"} excluded` : "No swatches excluded"}</span>
        </div>
        ${excludedFilamentText ? `<span class="status-pill model-review-state incomplete" title="Blocked by excluded filament: ${app.commands._escAttr(excludedFilamentText)}">Filament Excluded</span>` : ""}
      </div>
    `;
  }

  function modelReviewSwatchExclusionHtml(sample = {}, swatchCount = 1) {
    return `
      <div class="model-swatch-exclusion-panel">
        ${app.commands.modelReviewSampleExclusionGeometryHtml(sample, swatchCount)}
      </div>
    `;
  }

  function modelingSortArrow(activeKey, key, direction) {
    if (activeKey !== key) return "";
    return direction === "asc" ? " ↓" : " ↑";
  }

  function modelingAriaSort(activeKey, key, direction) {
    if (activeKey !== key) return "none";
    return direction === "asc" ? "ascending" : "descending";
  }

  function modelingSortableHeader(label, key, activeKey, direction, scope) {
    return `
      <th class="sortable" data-model-sort-scope="${app.commands._escAttr(scope)}" data-model-sort="${app.commands._escAttr(key)}" aria-sort="${app.commands._escAttr(app.commands.modelingAriaSort(activeKey, key, direction))}">
        ${app.commands._escHtml(label)}${app.commands.modelingSortArrow(activeKey, key, direction)}
      </th>
    `;
  }

  function renderModelingSampleDetail(payload) {
    const sample = payload?.sample || {};
    const detail = sample.model_detail || {};
    const variant = app.state.modeling.modelingDetailSettings.includeCorrections
      ? "corrected"
      : "uncorrected";
    const swatchCount = app.commands.modelReviewDetailSwatchCount(
      sample,
      detail,
    );

    return `
      <div class="model-review-detail">
        <div class="model-review-detail-controls" role="group" aria-label="Model detail display options">
          <button class="model-review-toggle-button${app.state.modeling.modelingDetailSettings.includeCorrections ? " is-active" : ""}" type="button" id="modelDetailCorrectionsToggle" aria-pressed="${app.state.modeling.modelingDetailSettings.includeCorrections ? "true" : "false"}">Color Corrections: ${app.state.modeling.modelingDetailSettings.includeCorrections ? "On" : "Off"}</button>
          ${app.commands.modelReviewSampleFilamentsHtml(sample)}
        </div>
        <div class="model-review-domain-grid">
          ${app.commands.modelReviewDomainPanelHtml(sample, detail, "transmission", variant, swatchCount)}
          ${app.commands.modelReviewDomainPanelHtml(sample, detail, "appearance", variant, swatchCount)}
        </div>
        <div class="model-review-exclusion-grid">
          ${app.commands.buildDrawerFormModule("Sample Exclusion", app.commands.modelReviewSampleExclusionHtml(sample, swatchCount), { density: "compact", classes: "model-review-detail-module model-sample-exclusion-module" })}
          ${app.commands.buildDrawerFormModule("Swatch Exclusion", app.commands.modelReviewSwatchExclusionHtml(sample, swatchCount), { density: "compact", classes: "model-review-detail-module model-swatch-exclusion-module" })}
        </div>
      </div>
    `;
  }

  function modelReviewSampleNavigationIds() {
    const rows = app.state.modeling.modelingState.samples?.rows?.length
      ? app.state.modeling.modelingState.samples.rows
      : app.state.modeling.modelingState.sampleDetailReturnFilamentPayload
          ?.samples || [];
    return Array.from(
      new Set((rows || []).map((row) => row.sample_id).filter(Boolean)),
    );
  }

  function modelReviewSampleNavigationMeta(sampleId) {
    const ids = app.commands.modelReviewSampleNavigationIds();
    const index = ids.indexOf(sampleId);
    return {
      ids,
      index,
      previousId: index > 0 ? ids[index - 1] : "",
      nextId: index >= 0 && index < ids.length - 1 ? ids[index + 1] : "",
    };
  }

  function modelReviewSampleHeaderActionsHtml(sampleId) {
    const nav = app.commands.modelReviewSampleNavigationMeta(sampleId);
    const returnButton = app.state.modeling.modelingState
      .sampleDetailReturnSampleContext
      ? `<button class="secondary-button small drawer-header-action" type="button" id="modelSampleReturnSourceSampleBtn">Return to Sample</button>`
      : app.state.modeling.modelingState.sampleDetailReturnFilamentId
        ? `
        <button class="secondary-button small drawer-header-action" type="button" id="modelSampleReturnFilamentBtn">Return to Filament</button>
      `
        : "";
    return `
      ${returnButton}
      <button class="secondary-button small drawer-header-action" type="button" id="modelSamplePrevBtn" ${nav.previousId ? "" : "disabled"}>Previous</button>
      <button class="secondary-button small drawer-header-action" type="button" id="modelSampleNextBtn" ${nav.nextId ? "" : "disabled"}>Next</button>
    `;
  }

  function bindModelingSampleHeaderActions() {
    document
      .getElementById("modelSampleReturnSourceSampleBtn")
      ?.addEventListener("click", () => {
        const context =
          app.state.modeling.modelingState.sampleDetailReturnSampleContext;
        app.state.modeling.modelingState.sampleDetailReturnSampleContext = null;
        app.commands.returnToSampleInspectDrawer(context || {});
      });
    document
      .getElementById("modelSampleReturnFilamentBtn")
      ?.addEventListener("click", async () => {
        const filamentId =
          app.state.modeling.modelingState.sampleDetailReturnFilamentId;
        if (!filamentId) return;
        await app.commands.openModelingFilamentDetailDrawer(filamentId);
      });
    document
      .getElementById("modelSamplePrevBtn")
      ?.addEventListener("click", async () => {
        await app.commands.navigateModelingSampleDetail(-1);
      });
    document
      .getElementById("modelSampleNextBtn")
      ?.addEventListener("click", async () => {
        await app.commands.navigateModelingSampleDetail(1);
      });
  }

  function updateModelingSampleDetailHeaderActions(sampleId) {
    app.dom.detailActionArea.innerHTML =
      app.commands.modelReviewSampleHeaderActionsHtml(sampleId);
    app.commands.bindModelingSampleHeaderActions();
  }

  async function navigateModelingSampleDetail(direction) {
    if (
      app.state.logbook.selectedRecord.kind !== "model_sample" ||
      !app.state.logbook.selectedRecord.id
    )
      return false;
    const nav = app.commands.modelReviewSampleNavigationMeta(
      app.state.logbook.selectedRecord.id,
    );
    const nextId = direction < 0 ? nav.previousId : nav.nextId;
    if (!nextId) return false;
    await app.commands.openModelingSampleDetailDrawer(nextId, null, {
      preserveReturn: true,
    });
    return true;
  }

  function shouldIgnoreModelingSampleArrowKey(event) {
    if (event.metaKey || event.ctrlKey || event.altKey) return true;
    const target = event.target;
    if (!(target instanceof Element)) return false;
    return !!target.closest(
      "input, textarea, select, button, a, [contenteditable='true']",
    );
  }

  function renderLoadedModelingSampleDetail(payload, fallbackSampleId = "") {
    app.state.modeling.modelingState.detailSamplePayload = payload;
    if (payload?.model_status)
      app.state.session.data.model_status = payload.model_status;
    const sample = payload?.sample || {};
    const sampleId = sample.sample_id || fallbackSampleId;
    app.commands.setDrawerHeading(sampleId);
    app.dom.drawerStatusPill.innerHTML =
      app.commands.modelReviewFitStateHtml(sample);
    app.commands.updateModelingSampleDetailHeaderActions(sampleId);
    app.dom.detailSidebar.innerHTML =
      app.commands.renderModelingSampleDetail(payload);
    app.commands.bindModelingSampleDetailControls();
  }

  async function refreshOpenModelingSampleDetail(sampleId) {
    if (
      app.state.logbook.selectedRecord.kind !== "model_sample" ||
      app.state.logbook.selectedRecord.id !== sampleId
    )
      return;
    const requestSeq = ++app.state.modeling.modelingState.detailRequestSeq;
    const payload = await app.api.fetchModelingSample(sampleId);
    if (
      requestSeq !== app.state.modeling.modelingState.detailRequestSeq ||
      app.state.logbook.selectedRecord.kind !== "model_sample" ||
      app.state.logbook.selectedRecord.id !== sampleId
    )
      return;
    app.commands.renderLoadedModelingSampleDetail(payload, sampleId);
  }

  function bindModelingSampleDetailControls() {
    document
      .getElementById("modelDetailCorrectionsToggle")
      ?.addEventListener("click", () => {
        app.state.modeling.modelingDetailSettings = {
          ...app.state.modeling.modelingDetailSettings,
          includeCorrections:
            !app.state.modeling.modelingDetailSettings.includeCorrections,
        };
        app.commands.persistModelingDetailSettings();
        if (app.state.modeling.modelingState.detailSamplePayload) {
          app.dom.detailSidebar.innerHTML =
            app.commands.renderModelingSampleDetail(
              app.state.modeling.modelingState.detailSamplePayload,
            );
          app.commands.bindModelingSampleDetailControls();
        }
      });
    const runSampleFitToggle = async (button) => {
      button.disabled = true;
      const nextFitExclude = button.dataset.nextFitExclude === "true";
      await app.commands.handleModelingSampleToggle(
        button.dataset.modelToggleSample,
        nextFitExclude,
      );
      if (document.body.contains(button)) button.disabled = false;
    };
    const sampleFitToggle = document.getElementById("modelSampleFitToggle");
    if (sampleFitToggle) {
      if (sampleFitToggle.dataset.nextFitExclude === "true") {
        app.commands.bindConfirmAction(sampleFitToggle, {
          armedText: "Confirm Exclude",
          onConfirm: async () => runSampleFitToggle(sampleFitToggle),
        });
      } else {
        sampleFitToggle.addEventListener("click", async () =>
          runSampleFitToggle(sampleFitToggle),
        );
      }
    }
    const runSwatchFitToggle = async (button) => {
      button.disabled = true;
      const sampleId =
        app.state.modeling.modelingState.detailSamplePayload?.sample
          ?.sample_id || app.state.logbook.selectedRecord.id;
      const swatchIndex = Number(button.dataset.modelToggleSwatch);
      const nextExcluded = button.dataset.nextExcluded === "true";
      await app.commands.handleModelingSampleSwatchToggle(
        sampleId,
        swatchIndex,
        nextExcluded,
      );
      if (document.body.contains(button)) button.disabled = false;
    };
    app.dom.detailSidebar
      .querySelectorAll("[data-model-toggle-swatch]")
      .forEach((button) => {
        if (button.dataset.nextExcluded === "true") {
          app.commands.bindConfirmAction(button, {
            armedText: "Confirm",
            onConfirm: async () => runSwatchFitToggle(button),
          });
          return;
        }
        button.addEventListener("click", async () =>
          runSwatchFitToggle(button),
        );
      });
    app.commands.bindModelReviewStripGeometry();
  }

  async function openModelingSampleDetailDrawer(
    sampleId,
    returnFocusEl = null,
    options = {},
  ) {
    if (!app.dom.recordDrawer || !app.dom.detailSidebar) return;
    const requestSeq = ++app.state.modeling.modelingState.detailRequestSeq;
    app.state.logbook.selectedRecord = { kind: "model_sample", id: sampleId };
    app.state.modeling.modelingState.detailSamplePayload = null;
    app.state.modeling.modelingState.detailFilamentPayload = null;
    if (!options.preserveReturn) {
      app.state.modeling.modelingState.sampleDetailReturnFilamentId = null;
      app.state.modeling.modelingState.sampleDetailReturnFilamentPayload = null;
      app.state.modeling.modelingState.sampleDetailReturnSampleContext = null;
    }
    if (options.returnFilamentId || options.returnFilamentPayload) {
      app.state.modeling.modelingState.sampleDetailReturnFilamentId =
        options.returnFilamentId ||
        options.returnFilamentPayload?.filament?.filament_id ||
        null;
      app.state.modeling.modelingState.sampleDetailReturnFilamentPayload =
        options.returnFilamentPayload || null;
      app.state.modeling.modelingState.sampleDetailReturnSampleContext = null;
    }
    if (options.returnSampleContext) {
      app.state.modeling.modelingState.sampleDetailReturnSampleContext =
        options.returnSampleContext;
      app.state.modeling.modelingState.sampleDetailReturnFilamentId = null;
      app.state.modeling.modelingState.sampleDetailReturnFilamentPayload = null;
    }
    app.commands.closeLinkedSampleDrawer({ restoreFocus: false });
    app.dom.recordDrawer.classList.remove(
      "narrow-drawer",
      "sample-set-drawer",
      "model-filament-drawer",
    );
    app.dom.recordDrawer.classList.add("sample-expanded");
    app.commands.setDetailSidebarStackMode("default");
    app.state.filaments._filamentDrawerMode = null;
    app.state.filaments._filamentDrawerData = null;
    app.state.logbook._sampleDrawerMode = null;
    app.commands.setDrawerHeading(sampleId);
    app.dom.drawerStatusPill.innerHTML = "";
    app.dom.detailActionArea.innerHTML =
      app.commands.modelReviewSampleHeaderActionsHtml(sampleId);
    app.commands.bindModelingSampleHeaderActions();
    app.dom.detailWindowArea.innerHTML = "";
    app.dom.detailSidebar.innerHTML = `<div class="model-review-empty">Loading model comparison...</div>`;
    app.commands.openRecordDrawer();

    try {
      const payload = await app.api.fetchModelingSample(sampleId);
      if (
        requestSeq !== app.state.modeling.modelingState.detailRequestSeq ||
        app.state.logbook.selectedRecord.kind !== "model_sample" ||
        app.state.logbook.selectedRecord.id !== sampleId
      )
        return;
      app.commands.renderLoadedModelingSampleDetail(payload, sampleId);
    } catch (err) {
      if (
        requestSeq !== app.state.modeling.modelingState.detailRequestSeq ||
        app.state.logbook.selectedRecord.kind !== "model_sample" ||
        app.state.logbook.selectedRecord.id !== sampleId
      )
        return;
      app.dom.drawerStatusPill.innerHTML = `<span class="status-pill failed">Failed</span>`;
      app.dom.detailSidebar.innerHTML = `<div class="model-review-empty is-error">${app.commands._escHtml(err.message || "Failed to load model comparison")}</div>`;
      if (returnFocusEl instanceof HTMLElement) returnFocusEl.focus();
    }
  }

  function modelReviewHealthStatusClass(state) {
    if (state === "good") return "processed";
    if (state === "partial" || state === "sparse" || state === "stale")
      return "stale";
    if (state === "excluded_only" || state === "missing") return "planned";
    return "none";
  }

  function modelReviewWhiteCapStatusHtml(filament = {}) {
    return `<span class="status-pill model-review-state ${filament.white_cap_eligible ? "processed" : "none"}">${filament.white_cap_eligible ? "White Cap" : "No White Cap"}</span>`;
  }

  function modelReviewFilamentExclusionPillHtml(filament = {}) {
    return filament.exclude_from_model
      ? `<span class="status-pill model-review-state planned">Excluded</span>`
      : "";
  }

  function modelReviewFilamentLookup(filamentId) {
    const id = String(filamentId || "");
    const sourceFilament = (app.state.session.data.filaments || []).find(
      (fil) => fil.filament_id === id,
    );
    if (sourceFilament) {
      return {
        filament_id: id,
        brand: sourceFilament.manufacturer || sourceFilament.brand || "",
        name: sourceFilament.color_name || sourceFilament.display_name || id,
        display_name:
          sourceFilament.display_name || sourceFilament.color_name || id,
        hex: sourceFilament.hex || "#999999",
      };
    }
    const reviewFilament = (
      app.state.modeling.modelingState.filaments?.rows || []
    ).find((fil) => fil.filament_id === id);
    if (reviewFilament) return reviewFilament;
    return {
      filament_id: id,
      brand: "",
      name: id,
      display_name: id,
      hex: "#999999",
    };
  }

  function modelReviewFilamentFilterHtml() {
    const selectedIds =
      app.state.modeling.modelingState.samplesFilamentIds || [];
    const chips = selectedIds
      .map((id) => {
        const fil = app.commands.modelReviewFilamentLookup(id);
        return `
        <span class="model-review-filter-chip">
          <span class="color-chip" style="background:${app.commands._escAttr(fil.hex || "#999999")}"></span>
          <span>${app.commands._escHtml(fil.name || fil.filament_id)}</span>
          <button type="button" data-model-sample-filter-remove="${app.commands._escAttr(id)}" aria-label="Remove ${app.commands._escAttr(fil.name || id)}">
            <svg viewBox="0 0 12 12" aria-hidden="true" focusable="false">
              <path d="M2 2 L10 10 M10 2 L2 10"></path>
            </svg>
          </button>
        </span>
      `;
      })
      .join("");
    return `
      <div class="model-review-filament-filter">
        <button class="secondary-button small model-filter-picker-button" type="button" id="modelSamplesFilamentFilterBtn">Filaments...</button>
        <div class="model-review-filter-chips" aria-label="Selected filament filters">
          ${chips || `<span class="model-review-filter-placeholder">All filaments</span>`}
        </div>
        ${selectedIds.length ? `<button class="ghost-button small model-filter-clear-button" type="button" id="modelSamplesClearFilaments">Clear</button>` : ""}
      </div>
    `;
  }

  function modelReviewModelStateHtml(modelStatus = {}) {
    return `
      <div class="model-filament-model-state model-filament-model-state-list">
        ${app.commands.renderModelOverviewStatusBlock(modelStatus)}
      </div>
    `;
  }

  function modelReviewFilamentSummaryHtml(filament = {}) {
    const excluded = !!filament.exclude_from_model;
    return `
      <div class="model-filament-summary">
        <div class="model-filament-title-line">
          <span class="color-chip" style="background:${app.commands._escAttr(filament.hex || "#999999")}"></span>
          <strong>${app.commands._escHtml(filament.name || filament.filament_id || "")}</strong>
          <span class="muted-line">${app.commands._escHtml(filament.brand || "")}</span>
          ${app.commands.modelReviewFilamentExclusionPillHtml(filament)}
          <span class="model-filament-title-spacer"></span>
          <button class="model-fit-control-button ${excluded ? "is-include" : "is-exclude"}"
                  type="button"
                  id="modelFilamentFitToggle"
                  data-model-toggle-filament="${app.commands._escAttr(filament.filament_id || "")}"
                  data-next-exclude="${excluded ? "false" : "true"}">
            ${excluded ? "Include in Fits" : "Exclude from Fits"}
          </button>
        </div>
      </div>
    `;
  }

  function modelReviewFilamentCoverageHtml(filament = {}) {
    const includedSamples = Number(filament.included_sample_count || 0);
    const totalSamples = Number(filament.sample_count || 0);
    const includedSwatches = Number(filament.included_swatch_count || 0);
    const totalSwatches = Number(filament.swatch_count || 0);
    const excludedSwatches = Number(filament.excluded_swatch_count || 0);
    const excludedSwatchNoun = excludedSwatches === 1 ? "swatch" : "swatches";
    return `
      <div class="model-filament-coverage-lines">
        <div class="model-filament-coverage-line"><strong>${includedSamples}/${totalSamples}</strong> Samples included</div>
        <div class="model-filament-coverage-line"><strong>${includedSwatches}/${totalSwatches}</strong> swatches included</div>
        <div class="model-filament-coverage-line"><strong>${excludedSwatches}</strong> ${excludedSwatchNoun} excluded</div>
      </div>
    `;
  }

  function modelReviewFilamentEvidenceHtml(payload = {}) {
    const filamentId = payload.filament?.filament_id || "";
    const samples = payload.samples || [];
    const colgroup = `
      <colgroup>
        <col class="model-filament-sample-col">
        <col class="model-filament-strip-col">
        <col class="model-filament-role-col">
        <col class="model-filament-state-col">
        <col class="model-filament-appearance-col">
      </colgroup>
    `;
    return `
      <div class="model-filament-samples-table">
        <table class="data-table compact-table model-filament-evidence-table">
          ${colgroup}
          <thead>
            <tr>
              <th>Sample</th>
              <th>Strip</th>
              <th>Roles</th>
              <th>Fit State</th>
              <th>Extracted Appearance</th>
            </tr>
          </thead>
          <tbody>
            ${
              samples
                .map(
                  (row) => `
              <tr class="data-row" data-model-filament-sample-id="${app.commands._escAttr(row.sample_id)}">
                <td><strong>${app.commands._escHtml(row.sample_id)}</strong></td>
                <td>${app.commands.modelReviewStripDiagramHtml(row)}</td>
                <td>${(row.roles_for_filament || []).map((role) => app.commands._escHtml(role.role_kind || role.role_label || "role")).join(" · ") || `<span class="muted-line">None</span>`}</td>
                <td>${app.commands.modelReviewFitStateHtml(row)}</td>
                <td>${app.commands.modelReviewStripHtml(row.observed_appearance?.hex || [])}</td>
              </tr>
            `,
                )
                .join("") ||
              `<tr><td colspan="5"><div class="model-review-empty">No samples contain ${app.commands._escHtml(filamentId)}.</div></td></tr>`
            }
          </tbody>
        </table>
      </div>
    `;
  }

  function renderModelingFilamentDetail(payload) {
    const filament = payload?.filament || {};
    return `
      <div class="model-review-detail model-filament-detail">
        ${app.commands.buildDrawerFormModule("Summary", app.commands.modelReviewFilamentSummaryHtml(filament), { density: "compact", classes: "model-review-detail-module" })}
        <div class="model-filament-metrics-grid">
          ${app.commands.buildDrawerFormModule("Coverage", app.commands.modelReviewFilamentCoverageHtml(filament), { density: "compact", classes: "model-review-detail-module model-filament-metric-module" })}
          ${app.commands.buildDrawerFormModule("Model State", app.commands.modelReviewModelStateHtml(payload?.model_status || {}), { density: "compact", classes: "model-review-detail-module model-filament-metric-module" })}
        </div>
        ${app.commands.buildDrawerFormModule("Samples", app.commands.modelReviewFilamentEvidenceHtml(payload), { density: "table", classes: "model-review-detail-module model-filament-samples-module" })}
      </div>
    `;
  }

  function bindModelingFilamentDetailActions(payload) {
    const filamentId = payload?.filament?.filament_id || "";
    const runFilamentFitToggle = async (button) => {
      button.disabled = true;
      const nextExclude = button.dataset.nextExclude === "true";
      await app.commands.handleModelingFilamentFitToggle(
        button.dataset.modelToggleFilament,
        nextExclude,
      );
      if (document.body.contains(button)) button.disabled = false;
    };
    const filamentFitToggle = document.getElementById("modelFilamentFitToggle");
    if (filamentFitToggle) {
      if (filamentFitToggle.dataset.nextExclude === "true") {
        app.commands.bindConfirmAction(filamentFitToggle, {
          armedText: "Confirm Exclude",
          onConfirm: async () => runFilamentFitToggle(filamentFitToggle),
        });
      } else {
        filamentFitToggle.addEventListener("click", async () =>
          runFilamentFitToggle(filamentFitToggle),
        );
      }
    }
    document
      .getElementById("modelFilamentShowSamplesBtn")
      ?.addEventListener("click", async () => {
        if (!filamentId) return;
        app.state.modeling.modelingState.samplesFilamentIds = [filamentId];
        app.state.modeling.modelingState.samplesFilter = "all";
        app.state.modeling.modelingState.samples = null;
        app.state.navigation.currentSubtab = "samples";
        app.commands.renderWorkspace();
        await app.commands.loadModelingTab("samples", { force: true });
      });
    app.dom.detailSidebar
      .querySelectorAll("[data-model-filament-sample-id]")
      .forEach((row) => {
        row.addEventListener("click", async () => {
          const sampleId = row.dataset.modelFilamentSampleId;
          await app.commands.openModelingSampleDetailDrawer(sampleId, row, {
            returnFilamentId: filamentId,
            returnFilamentPayload: payload,
          });
        });
      });
  }

  async function handleModelingFilamentFitToggle(filamentId, nextExclude) {
    if (!filamentId) return;
    try {
      const updated = await app.api.updateFilament(filamentId, {
        exclude_from_model: !!nextExclude,
      });
      const index = app.state.session.data.filaments.findIndex(
        (fil) => fil.filament_id === filamentId,
      );
      if (index >= 0)
        app.state.session.data.filaments[index] = {
          ...app.state.session.data.filaments[index],
          ...updated,
        };
      app.commands.invalidateModelingPayloads();
      app.commands.showProfileToast(
        `${updated.display_name || updated.color_name || filamentId} ${nextExclude ? "excluded" : "included"} for model fits`,
      );
      await app.commands.loadModelingTab(app.commands.modelingCurrentTab(), {
        force: true,
      });
      if (
        app.state.logbook.selectedRecord.kind === "model_filament" &&
        app.state.logbook.selectedRecord.id === filamentId
      ) {
        await app.commands.openModelingFilamentDetailDrawer(filamentId);
      }
    } catch (err) {
      app.commands.showProfileToast(
        err.message || "Failed to update filament model-fit state",
      );
    }
  }

  async function openModelingFilamentDetailDrawer(
    filamentId,
    returnFocusEl = null,
  ) {
    if (!app.dom.recordDrawer || !app.dom.detailSidebar) return;
    const requestSeq = ++app.state.modeling.modelingState.detailRequestSeq;
    app.state.logbook.selectedRecord = {
      kind: "model_filament",
      id: filamentId,
    };
    app.state.modeling.modelingState.detailSamplePayload = null;
    app.state.modeling.modelingState.detailFilamentPayload = null;
    app.state.modeling.modelingState.sampleDetailReturnFilamentId = null;
    app.state.modeling.modelingState.sampleDetailReturnFilamentPayload = null;
    app.state.modeling.modelingState.sampleDetailReturnSampleContext = null;
    app.commands.closeLinkedSampleDrawer({ restoreFocus: false });
    app.dom.recordDrawer.classList.remove(
      "narrow-drawer",
      "sample-set-drawer",
      "sample-expanded",
    );
    app.dom.recordDrawer.classList.add("model-filament-drawer");
    app.commands.setDetailSidebarStackMode("default");
    app.state.filaments._filamentDrawerMode = null;
    app.state.filaments._filamentDrawerData = null;
    app.state.logbook._sampleDrawerMode = null;
    app.commands.setDrawerHeading(filamentId);
    app.dom.drawerStatusPill.innerHTML = "";
    app.dom.detailActionArea.innerHTML = `<button class="secondary-button small drawer-header-action" type="button" id="modelFilamentShowSamplesBtn" disabled>Show In Samples</button>`;
    app.dom.detailWindowArea.innerHTML = "";
    app.dom.detailSidebar.innerHTML = `<div class="model-review-empty">Loading filament modeling detail...</div>`;
    app.commands.openRecordDrawer();

    try {
      const payload = await app.api.fetchModelingFilament(filamentId);
      if (
        requestSeq !== app.state.modeling.modelingState.detailRequestSeq ||
        app.state.logbook.selectedRecord.kind !== "model_filament" ||
        app.state.logbook.selectedRecord.id !== filamentId
      )
        return;
      app.state.modeling.modelingState.detailFilamentPayload = payload;
      if (payload?.model_status)
        app.state.session.data.model_status = payload.model_status;
      const filament = payload?.filament || {};
      app.commands.setDrawerHeading(
        filament.name ||
          filament.display_name ||
          filament.filament_id ||
          filamentId,
      );
      app.dom.drawerStatusPill.innerHTML =
        app.commands.modelReviewWhiteCapStatusHtml(filament);
      app.dom.detailActionArea.innerHTML = `<button class="secondary-button small drawer-header-action" type="button" id="modelFilamentShowSamplesBtn">Show In Samples</button>`;
      app.dom.detailSidebar.innerHTML =
        app.commands.renderModelingFilamentDetail(payload);
      app.commands.bindModelingFilamentDetailActions(payload);
    } catch (err) {
      if (
        requestSeq !== app.state.modeling.modelingState.detailRequestSeq ||
        app.state.logbook.selectedRecord.kind !== "model_filament" ||
        app.state.logbook.selectedRecord.id !== filamentId
      )
        return;
      app.dom.drawerStatusPill.innerHTML = `<span class="status-pill failed">Failed</span>`;
      app.dom.detailActionArea.innerHTML = "";
      app.dom.detailSidebar.innerHTML = `<div class="model-review-empty is-error">${app.commands._escHtml(err.message || "Failed to load filament modeling detail")}</div>`;
      if (returnFocusEl instanceof HTMLElement) returnFocusEl.focus();
    }
  }

  function renderModelingSamples(payload) {
    if (!payload) return app.commands.modelingLoadingHtml("samples");
    const rows = payload.rows || [];
    const total = Number(payload.total || rows.length);
    const sampleCountLabel =
      rows.length === total
        ? `${total} samples`
        : `${rows.length} / ${total} samples shown`;
    const filterOptions = [
      ["all", "All"],
      ["included", "Included"],
      ["excluded", "Excluded"],
      ["filament_excluded", "Filament Excluded"],
      ["has_excluded_swatches", "Has Excluded Swatches"],
      ["stale", "Stale"],
      ["missing_model", "Missing Model"],
    ];
    return `
      <div class="model-review-page">
        ${app.commands.modelStatusAttentionHtml()}
        <div class="model-review-controls">
          <div class="model-review-controls-main">
            <label class="model-review-control-field">
              <span>Filter</span>
              <select id="modelSamplesFilter">
                ${filterOptions.map(([value, label]) => `<option value="${value}"${app.state.modeling.modelingState.samplesFilter === value ? " selected" : ""}>${label}</option>`).join("")}
              </select>
            </label>
            ${app.commands.modelReviewFilamentFilterHtml()}
          </div>
          <span class="model-review-result-count">${app.commands._escHtml(sampleCountLabel)}</span>
        </div>
        <table class="data-table compact-table model-review-table">
          <thead>
            <tr>
              ${app.commands.modelingSortableHeader("Sample", "sample_id", app.state.modeling.modelingState.samplesSort, app.state.modeling.modelingState.samplesSortDir, "samples")}
              <th>Strip</th>
              ${app.commands.modelingSortableHeader("Filaments", "filament", app.state.modeling.modelingState.samplesSort, app.state.modeling.modelingState.samplesSortDir, "samples")}
              ${app.commands.modelingSortableHeader("Fit State", "status", app.state.modeling.modelingState.samplesSort, app.state.modeling.modelingState.samplesSortDir, "samples")}
              <th>Extracted Appearance</th>
            </tr>
          </thead>
          <tbody>
            ${
              rows
                .map(
                  (row) => `
              <tr class="data-row" data-model-sample-id="${app.commands._escAttr(row.sample_id)}">
                <td><strong>${app.commands._escHtml(row.sample_id)}</strong></td>
                <td>${app.commands.modelReviewStripDiagramHtml(row)}</td>
                <td>${app.commands.modelReviewFilamentStackHtml(row.filaments)}</td>
                <td>${app.commands.modelReviewFitStateHtml(row)}</td>
                <td>${app.commands.modelReviewStripHtml(row.observed_appearance?.hex || [])}</td>
              </tr>
            `,
                )
                .join("") ||
              `<tr><td colspan="5"><div class="model-review-empty">No samples match this filter.</div></td></tr>`
            }
          </tbody>
        </table>
      </div>
    `;
  }

  function renderModelingFilaments(payload) {
    if (!payload) return app.commands.modelingLoadingHtml("filaments");
    const rows = payload.rows || [];
    return `
      <div class="model-review-page model-review-filaments-page">
        ${app.commands.modelStatusAttentionHtml()}
        <div class="model-review-filaments-meta">
          <span class="model-review-result-count">${Number(payload.total || 0)} filaments</span>
        </div>
        <div class="model-review-table-scroll model-review-filament-table-scroll">
          <table class="data-table compact-table model-review-table model-review-filament-table">
            <colgroup>
              <col class="model-review-filament-name-col">
              <col class="model-review-filament-brand-col">
              <col class="model-review-filament-total-samples-col">
              <col class="model-review-filament-fit-samples-col">
              <col class="model-review-filament-swatch-count-col">
              <col class="model-review-filament-swatch-count-col">
              <col class="model-review-filament-health-col">
            </colgroup>
            <thead>
              <tr>
                ${app.commands.modelingSortableHeader("Filament", "name", app.state.modeling.modelingState.filamentsSort, app.state.modeling.modelingState.filamentsSortDir, "filaments")}
                ${app.commands.modelingSortableHeader("Brand", "brand", app.state.modeling.modelingState.filamentsSort, app.state.modeling.modelingState.filamentsSortDir, "filaments")}
                ${app.commands.modelingSortableHeader("Samples", "sample_count", app.state.modeling.modelingState.filamentsSort, app.state.modeling.modelingState.filamentsSortDir, "filaments")}
                ${app.commands.modelingSortableHeader("Fit Samples", "included_sample_count", app.state.modeling.modelingState.filamentsSort, app.state.modeling.modelingState.filamentsSortDir, "filaments")}
                ${app.commands.modelingSortableHeader("Incl. Swatches", "included_swatch_count", app.state.modeling.modelingState.filamentsSort, app.state.modeling.modelingState.filamentsSortDir, "filaments")}
                ${app.commands.modelingSortableHeader("Excl. Swatches", "excluded_swatch_count", app.state.modeling.modelingState.filamentsSort, app.state.modeling.modelingState.filamentsSortDir, "filaments")}
                ${app.commands.modelingSortableHeader("Health", "health", app.state.modeling.modelingState.filamentsSort, app.state.modeling.modelingState.filamentsSortDir, "filaments")}
              </tr>
            </thead>
            <tbody>
              ${
                rows
                  .map(
                    (row) => `
                <tr class="data-row" data-model-open-filament="${app.commands._escAttr(row.filament_id)}">
                  <td>
                    <span class="model-filament-name-cell">
                      <span class="color-chip model-filament-table-chip" style="background:${app.commands._escAttr(row.hex || "#999999")}"></span>
                      <strong>${app.commands._escHtml(row.name || row.filament_id)}</strong>
                      ${app.commands.modelReviewFilamentExclusionPillHtml(row)}
                    </span>
                  </td>
                  <td>${app.commands._escHtml(row.brand || "")}</td>
                  <td>${Number(row.sample_count || 0)}</td>
                  <td>${Number(row.included_sample_count || 0)}</td>
                  <td>${Number(row.included_swatch_count || 0)}</td>
                  <td>${Number(row.excluded_swatch_count || 0)}</td>
                  <td><span class="status-pill model-review-state ${app.commands._escAttr(app.commands.modelReviewHealthStatusClass(row.health?.state || "unknown"))}">${app.commands._escHtml(row.health?.label || "Unknown")}</span></td>
                </tr>
              `,
                  )
                  .join("") ||
                `<tr><td colspan="7"><div class="model-review-empty">No filament coverage rows.</div></td></tr>`
              }
            </tbody>
          </table>
        </div>
      </div>
    `;
  }

  function applyFitControlMutationResponse(result) {
    if (result?.model_status)
      app.state.session.data.model_status = result.model_status;
    const sampleId = result?.sample_id;
    if (sampleId) {
      const exp = app.state.session.data.samples.find(
        (item) => item.sample_id === sampleId,
      );
      if (exp) {
        exp._fit_exclude = !!result.fit_exclude;
        exp._excluded_swatches = Array.isArray(result.excluded_swatches)
          ? result.excluded_swatches
          : exp._excluded_swatches;
        exp._n_excluded = exp._excluded_swatches.length;
      }
    }
    app.commands.invalidateModelingPayloads();
  }

  async function handleModelingSampleToggle(sampleId, nextFitExclude) {
    const exp = app.state.session.data.samples.find(
      (item) => item.sample_id === sampleId,
    );
    try {
      const result = await app.api.updateSampleFitExclusion(sampleId, {
        fit_exclude: !!nextFitExclude,
      });
      app.commands.applyFitControlMutationResponse(result);
      app.commands.showProfileToast(
        `${sampleId} ${nextFitExclude ? "excluded" : "included"} for model fits`,
      );
      await app.commands.loadModelingTab(app.commands.modelingCurrentTab(), {
        force: true,
      });
      if (
        app.state.logbook.selectedRecord.kind === "sample" &&
        app.state.logbook.selectedRecord.id === sampleId &&
        exp
      ) {
        exp._fit_exclude = !!nextFitExclude;
        app.commands.renderSidebarForSample(exp, {
          expanded: app.state.logbook._sampleInspectExpanded,
        });
      }
      if (
        app.state.logbook.selectedRecord.kind === "model_sample" &&
        app.state.logbook.selectedRecord.id === sampleId
      ) {
        await app.commands.refreshOpenModelingSampleDetail(sampleId);
      }
    } catch (err) {
      app.commands.showProfileToast(
        err.message || "Failed to update model-fit state",
      );
    }
  }

  async function handleModelingSampleSwatchToggle(
    sampleId,
    swatchIndex,
    nextExcluded,
  ) {
    if (!sampleId || !Number.isInteger(swatchIndex) || swatchIndex < 0) return;
    const sample =
      app.state.modeling.modelingState.detailSamplePayload?.sample || {};
    const excluded = app.commands.modelReviewExcludedSwatchSet(sample);
    if (nextExcluded) excluded.add(swatchIndex);
    else excluded.delete(swatchIndex);
    const nextExcludedSwatches = Array.from(excluded).sort((a, b) => a - b);
    try {
      const result = await app.api.updateSampleSwatchFitExclusions(
        sampleId,
        nextExcludedSwatches,
      );
      app.commands.applyFitControlMutationResponse(result);
      app.commands.showProfileToast(
        `Swatch ${swatchIndex + 1} ${nextExcluded ? "excluded" : "included"} for model fits`,
      );
      await app.commands.loadModelingTab(app.commands.modelingCurrentTab(), {
        force: true,
      });
      if (
        app.state.logbook.selectedRecord.kind === "model_sample" &&
        app.state.logbook.selectedRecord.id === sampleId
      ) {
        await app.commands.refreshOpenModelingSampleDetail(sampleId);
      }
    } catch (err) {
      app.commands.showProfileToast(
        err.message || "Failed to update swatch fit state",
      );
    }
  }

  async function handleModelingHeaderSort(scope, key) {
    if (scope === "filaments") {
      if (app.state.modeling.modelingState.filamentsSort === key) {
        app.state.modeling.modelingState.filamentsSortDir =
          app.state.modeling.modelingState.filamentsSortDir === "asc"
            ? "desc"
            : "asc";
      } else {
        app.state.modeling.modelingState.filamentsSort = key;
        app.state.modeling.modelingState.filamentsSortDir = "asc";
      }
      app.state.modeling.modelingState.filaments = null;
      app.state.navigation.currentSubtab = "filaments";
      app.commands.renderWorkspace();
      await app.commands.loadModelingTab("filaments", { force: true });
      return;
    }

    if (app.state.modeling.modelingState.samplesSort === key) {
      app.state.modeling.modelingState.samplesSortDir =
        app.state.modeling.modelingState.samplesSortDir === "asc"
          ? "desc"
          : "asc";
    } else {
      app.state.modeling.modelingState.samplesSort = key;
      app.state.modeling.modelingState.samplesSortDir = "asc";
    }
    app.state.modeling.modelingState.samples = null;
    app.state.navigation.currentSubtab = "samples";
    app.commands.renderWorkspace();
    await app.commands.loadModelingTab("samples", { force: true });
  }

  function bindModelingActions() {
    const fitModelsBtn = document.getElementById("modelOverviewFitModelsBtn");
    if (fitModelsBtn) {
      fitModelsBtn.onclick = () =>
        app.commands.openFitModelsWorkflow(fitModelsBtn, async () => {
          app.commands.invalidateModelingPayloads();
          await app.commands.refreshModelsAfterWorkflow();
          await app.commands.loadModelingTab(
            app.commands.modelingCurrentTab(),
            { force: true },
          );
        });
    }
    const samplesFilter = document.getElementById("modelSamplesFilter");
    if (samplesFilter) {
      samplesFilter.addEventListener("change", async () => {
        app.state.modeling.modelingState.samplesFilter =
          samplesFilter.value || "all";
        app.state.modeling.modelingState.samples = null;
        await app.commands.loadModelingTab("samples", { force: true });
      });
    }
    if (!app.dom.tableContainer) return;
    app.dom.tableContainer
      .querySelectorAll("[data-model-sort]")
      .forEach((header) => {
        header.addEventListener("click", async () => {
          await app.commands.handleModelingHeaderSort(
            header.dataset.modelSortScope || "samples",
            header.dataset.modelSort || "sample_id",
          );
        });
      });
    document
      .getElementById("modelSamplesFilamentFilterBtn")
      ?.addEventListener("click", () => {
        app.commands.openFilamentSelector({
          title: "Filter Samples by Filament",
          mode: "multi",
          selectedIds:
            app.state.modeling.modelingState.samplesFilamentIds || [],
          onApply: async (ids) => {
            app.state.modeling.modelingState.samplesFilamentIds = Array.from(
              new Set(ids || []),
            );
            app.state.modeling.modelingState.samples = null;
            app.state.navigation.currentSubtab = "samples";
            app.commands.renderWorkspace();
            await app.commands.loadModelingTab("samples", { force: true });
          },
        });
      });
    document
      .getElementById("modelSamplesClearFilaments")
      ?.addEventListener("click", async () => {
        app.state.modeling.modelingState.samplesFilamentIds = [];
        app.state.modeling.modelingState.samples = null;
        app.state.navigation.currentSubtab = "samples";
        app.commands.renderWorkspace();
        await app.commands.loadModelingTab("samples", { force: true });
      });
    app.dom.tableContainer
      .querySelectorAll("[data-model-sample-filter-remove]")
      .forEach((button) => {
        button.addEventListener("click", async () => {
          const removeId = button.dataset.modelSampleFilterRemove;
          app.state.modeling.modelingState.samplesFilamentIds = (
            app.state.modeling.modelingState.samplesFilamentIds || []
          ).filter((id) => id !== removeId);
          app.state.modeling.modelingState.samples = null;
          app.state.navigation.currentSubtab = "samples";
          app.commands.renderWorkspace();
          await app.commands.loadModelingTab("samples", { force: true });
        });
      });
    app.dom.tableContainer
      .querySelectorAll("[data-model-sample-id]")
      .forEach((row) => {
        row.addEventListener("click", async (event) => {
          if (
            event.target.closest(
              "button, a, input, select, textarea, label, [role='button']",
            )
          )
            return;
          const sampleId = row.dataset.modelSampleId;
          await app.commands.openModelingSampleDetailDrawer(sampleId, row);
        });
      });
    app.dom.tableContainer
      .querySelectorAll("[data-model-open-filament]")
      .forEach((row) => {
        row.addEventListener("click", async () => {
          const filamentId = row.dataset.modelOpenFilament || "";
          await app.commands.openModelingFilamentDetailDrawer(filamentId, row);
        });
      });
  }

  function renderModelsView(options = {}) {
    const defaultContent = document.getElementById("defaultContent");
    const panel = defaultContent?.querySelector(".main-logbook");
    const sectionHead = panel?.querySelector(".section-head");
    const tab = app.commands.modelingCurrentTab();
    defaultContent?.classList.add("model-overview-content");
    defaultContent?.classList.toggle(
      "modeling-overview-content",
      tab === "overview",
    );
    defaultContent?.classList.toggle(
      "modeling-filaments-content",
      tab === "filaments",
    );
    panel?.classList.add("model-overview-panel", "model-tab-shell");
    panel?.classList.toggle("modeling-overview-panel", tab === "overview");
    panel?.classList.toggle("modeling-filaments-panel", tab === "filaments");
    sectionHead?.classList.add("model-status-section-head");

    app.dom.tableSummary.textContent = "";
    app.dom.tableToolbar.className = "toolbar-inline model-status-header";
    app.dom.tableToolbar.innerHTML = `
      <div class="model-status-list" role="list" aria-label="Calibration model status">
        ${app.commands.renderModelOverviewHeaderStatus()}
      </div>
      <button class="primary-button small model-status-fit-button" type="button" id="modelOverviewFitModelsBtn">Fit Models</button>
    `;
    if (!options.skipEnsure) app.commands.ensureModelingTabLoaded(tab);
    if (tab === "samples")
      app.dom.tableContainer.innerHTML = app.commands.renderModelingSamples(
        app.state.modeling.modelingState.samples,
      );
    else if (tab === "filaments")
      app.dom.tableContainer.innerHTML = app.commands.renderModelingFilaments(
        app.state.modeling.modelingState.filaments,
      );
    else
      app.dom.tableContainer.innerHTML = app.commands.renderModelingOverview(
        app.state.modeling.modelingState.overview,
      );
    app.commands.bindModelingActions();
  }

  Object.assign(app.commands, {
    loadModelingDetailSettings,
    persistModelingDetailSettings,
    modelOverviewStatusMeta,
    modelOverviewDateText,
    modelOverviewStatusTitle,
    renderModelOverviewStatusLine,
    renderModelOverviewHeaderStatus,
    renderModelOverviewStatusBlock,
    invalidateModelingPayloads,
    modelingCurrentTab,
    modelingPayloadForTab,
    setModelingPayloadForTab,
    fetchAllModelingSamples,
    loadModelingTab,
    ensureModelingTabLoaded,
    modelStatusAttentionHtml,
    modelingLoadingHtml,
    modelReviewOverviewTableHtml,
    renderModelingOverview,
    modelReviewStripHtml,
    modelReviewDetailStripHtml,
    modelReviewDetailSwatchCount,
    modelReviewStripAlignmentStyle,
    modelReviewNormalizeHexes,
    modelReviewDomainStripHtml,
    modelReviewLinearTriplet,
    modelReviewLinearToOklab,
    modelReviewHexToOklab,
    modelReviewOklabDeltaFromLabs,
    modelReviewSeriesOklab,
    modelReviewOklabErrors,
    modelReviewFormatOklabError,
    modelReviewOklabErrorStats,
    modelReviewOklabErrorGraphHtml,
    modelReviewDomainSeriesHtml,
    modelReviewDomainErrorSeriesHtml,
    modelReviewDomainPanelHtml,
    applyModelReviewStripGeometry,
    bindModelReviewStripGeometry,
    modelReviewFilamentStackHtml,
    modelReviewSampleFilamentRoleLabel,
    modelReviewSampleFilamentsHtml,
    modelReviewStripDiagramHtml,
    modelReviewExcludedFilaments,
    modelReviewExcludedFilamentText,
    modelReviewFitStateHtml,
    modelReviewExcludedSwatchSet,
    modelReviewSwatchThicknessLabel,
    modelReviewSampleExclusionGeometryHtml,
    modelReviewSampleExclusionHtml,
    modelReviewSwatchExclusionHtml,
    modelingSortArrow,
    modelingAriaSort,
    modelingSortableHeader,
    renderModelingSampleDetail,
    modelReviewSampleNavigationIds,
    modelReviewSampleNavigationMeta,
    modelReviewSampleHeaderActionsHtml,
    bindModelingSampleHeaderActions,
    updateModelingSampleDetailHeaderActions,
    navigateModelingSampleDetail,
    shouldIgnoreModelingSampleArrowKey,
    renderLoadedModelingSampleDetail,
    refreshOpenModelingSampleDetail,
    bindModelingSampleDetailControls,
    openModelingSampleDetailDrawer,
    modelReviewHealthStatusClass,
    modelReviewWhiteCapStatusHtml,
    modelReviewFilamentExclusionPillHtml,
    modelReviewFilamentLookup,
    modelReviewFilamentFilterHtml,
    modelReviewModelStateHtml,
    modelReviewFilamentSummaryHtml,
    modelReviewFilamentCoverageHtml,
    modelReviewFilamentEvidenceHtml,
    renderModelingFilamentDetail,
    bindModelingFilamentDetailActions,
    handleModelingFilamentFitToggle,
    openModelingFilamentDetailDrawer,
    renderModelingSamples,
    renderModelingFilaments,
    applyFitControlMutationResponse,
    handleModelingSampleToggle,
    handleModelingSampleSwatchToggle,
    handleModelingHeaderSort,
    bindModelingActions,
    renderModelsView,
  });
}
