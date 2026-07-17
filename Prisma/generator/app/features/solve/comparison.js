/**
 * Install the solve/comparison feature commands.
 * @param {import("../../core/types.js").ApplicationContext} app
 */
export function installFeaturesSolveComparison(app) {
function invalidateSolveRunCaches(runOrId) {
    const run = typeof runOrId === "string" ? null : runOrId;
    const runId = typeof runOrId === "string" ? runOrId : run?.id;
    if (!runId) return;
    delete app.state.ui.surfaceDataCache[runId];
    delete app.state.ui.explorerMaterialDataCache[runId];
    delete app.state.ui.capThicknessCache[runId];
    delete app.state.ui.filamentThicknessCache[runId];
    delete app.state.ui.recipeDataCache[runId];
    delete app.state.ui.recipeDataPromiseCache[runId];
    delete app.state.ui.recipeCookbookPromiseCache[runId];
    app.state.ui.recipeDataGeneration[runId] = (app.state.ui.recipeDataGeneration[runId] || 0) + 1;
    if (run?.results) {
      Object.values(run.results).forEach((value) => {
        if (typeof value === "string") delete app.state.solve.solveContourDataCache[value];
      });
    } else {
      Object.keys(app.state.solve.solveContourDataCache).forEach((key) => delete app.state.solve.solveContourDataCache[key]);
    }
  }

async function ensureCapThickness(run) {
    if (!run?.results?.cap_height_bin_url) return null;
    if (app.state.ui.capThicknessCache[run.id]) return app.state.ui.capThicknessCache[run.id];
    const blob = await app.commands.loadSurfaceBlob(run.results.cap_height_bin_url);
    if (!blob) return null;
    app.state.ui.capThicknessCache[run.id] = blob;
    return blob;
  }

async function ensureFilamentThickness(run, filamentId) {
    const url = run?.results?.filament_bin_urls?.[filamentId];
    if (!url) return null;
    if (!app.state.ui.filamentThicknessCache[run.id]) app.state.ui.filamentThicknessCache[run.id] = {};
    if (app.state.ui.filamentThicknessCache[run.id][filamentId]) return app.state.ui.filamentThicknessCache[run.id][filamentId];
    const blob = await app.commands.loadSurfaceBlob(url);
    if (!blob) return null;
    app.state.ui.filamentThicknessCache[run.id][filamentId] = blob;
    return blob;
  }

function isSolveCapDiffView(view) {
    return view === "cap_diff";
  }

function isSolveFilamentDiffView(view) {
    return view === "filament_diff";
  }

function isSolveThicknessDiffView(view) {
    return app.commands.isSolveCapDiffView(view) || app.commands.isSolveFilamentDiffView(view);
  }

function getRunFilamentMapInfo(run, filamentId) {
    if (!run?.results || !filamentId) return null;
    const maps = run.results.filament_maps || [];
    return maps.find((m) => m.filament_id === filamentId) || null;
  }

function getSolveFilamentDiffOptions(selectedRuns = app.commands.getSelectedRuns().filter((r) => r.results)) {
    const options = [];
    const seen = new Set();
    selectedRuns.forEach((run) => {
      const maps = run.results?.filament_maps || [];
      maps.forEach((mapInfo) => {
        const filamentId = mapInfo?.filament_id || "";
        if (!filamentId || filamentId.startsWith("__") || seen.has(filamentId)) return;
        const fil = app.commands.filamentById(filamentId);
        options.push({
          filament_id: filamentId,
          label: fil?.color_name || filamentId,
          hex: fil?.hex || "#888",
        });
        seen.add(filamentId);
      });
    });
    return options;
  }

function ensureSolveFilamentDiffSelection(selectedRuns = app.commands.getSelectedRuns().filter((r) => r.results)) {
    const options = app.commands.getSolveFilamentDiffOptions(selectedRuns);
    if (!options.length) {
      app.state.solve.solveFilamentDiffId = "";
      return "";
    }
    if (!options.some((opt) => opt.filament_id === app.state.solve.solveFilamentDiffId)) {
      app.state.solve.solveFilamentDiffId = options[0].filament_id;
    }
    return app.state.solve.solveFilamentDiffId;
  }

function syncSolveFilamentDiffControl(selectedRuns = app.commands.getSelectedRuns().filter((r) => r.results)) {
    const select = app.state.ui.$("#solveFilamentDiffSelect");
    if (!select) return;
    const options = app.commands.getSolveFilamentDiffOptions(selectedRuns);
    const activeId = app.commands.ensureSolveFilamentDiffSelection(selectedRuns);
    select.innerHTML = options.map((opt) => `<option value="${app.commands.esc(opt.filament_id)}">${app.commands.esc(opt.label)}</option>`).join("");
    select.disabled = !options.length;
    select.value = activeId;
  }

function computeSolveCapDiff(beforeCap, afterCap, eps = 1e-6) {
    if (!beforeCap || !afterCap) return null;
    if (beforeCap.width !== afterCap.width || beforeCap.height !== afterCap.height) return null;
    const len = beforeCap.data.length;
    const delta = new Float32Array(len);
    let changedPx = 0;
    let addedPx = 0;
    let removedPx = 0;
    let beforeActivePx = 0;
    let afterActivePx = 0;
    let maxAbsDelta = 0;
    let totalAbsDelta = 0;
    for (let i = 0; i < len; i++) {
      const before = beforeCap.data[i];
      const after = afterCap.data[i];
      const diff = after - before;
      const absDiff = Math.abs(diff);
      delta[i] = diff;
      if (before > eps) beforeActivePx++;
      if (after > eps) afterActivePx++;
      if (absDiff > eps) {
        changedPx++;
        totalAbsDelta += absDiff;
        if (diff > eps) addedPx++;
        else if (diff < -eps) removedPx++;
        if (absDiff > maxAbsDelta) maxAbsDelta = absDiff;
      }
    }
    return {
      width: beforeCap.width,
      height: beforeCap.height,
      delta,
      changedPx,
      addedPx,
      removedPx,
      beforeActivePx,
      afterActivePx,
      maxAbsDelta,
      meanAbsDelta: changedPx ? (totalAbsDelta / changedPx) : 0,
      eps,
    };
  }

function renderSolveCapDiffCanvas(canvas, diff, mode) {
    if (!canvas || !diff) return;
    canvas.width = diff.width;
    canvas.height = diff.height;
    const ctx = canvas.getContext("2d");
    const img = ctx.createImageData(diff.width, diff.height);
    const px = img.data;
    const scale = diff.maxAbsDelta > diff.eps ? diff.maxAbsDelta : 1.0;
    for (let i = 0; i < diff.delta.length; i++) {
      const d = diff.delta[i];
      const absDiff = Math.abs(d);
      const off = i * 4;
      let r = 0, g = 0, b = 0;
      if (mode === "changed") {
        if (absDiff > diff.eps) r = g = b = 255;
      } else if (mode === "added") {
        if (d > diff.eps) g = 255;
      } else if (mode === "removed") {
        if (d < -diff.eps) r = 255;
      } else if (absDiff > diff.eps) {
        const intensity = Math.max(32, Math.min(255, Math.round((absDiff / scale) * 255)));
        if (d > 0) g = intensity;
        else r = intensity;
      }
      px[off] = r;
      px[off + 1] = g;
      px[off + 2] = b;
      px[off + 3] = 255;
    }
    ctx.putImageData(img, 0, 0);
  }

function formatSolveDiffMm(value) {
    const num = Number(value);
    if (!Number.isFinite(num)) return "—";
    return `${num.toFixed(3).replace(/\.?0+$/, "")} mm`;
  }

function getCurrentSolveCapDiffFromCache() {
    if (!app.commands.isSolveCapDiffView(app.state.solve.solveView)) return null;
    const selectedRuns = app.commands.getSelectedRuns().filter((r) => r.results);
    if (selectedRuns.length !== 2) return null;
    const [beforeRun, afterRun] = selectedRuns;
    const beforeCap = app.state.ui.capThicknessCache[beforeRun.id];
    const afterCap = app.state.ui.capThicknessCache[afterRun.id];
    if (!beforeCap || !afterCap) return null;
    return app.commands.computeSolveCapDiff(beforeCap, afterCap);
  }

function getCurrentSolveFilamentDiffFromCache() {
    if (!app.commands.isSolveFilamentDiffView(app.state.solve.solveView)) return null;
    const selectedRuns = app.commands.getSelectedRuns().filter((r) => r.results);
    if (selectedRuns.length !== 2) return null;
    const filamentId = app.commands.ensureSolveFilamentDiffSelection(selectedRuns);
    if (!filamentId) return null;
    const [beforeRun, afterRun] = selectedRuns;
    const beforeFil = app.state.ui.filamentThicknessCache[beforeRun.id]?.[filamentId];
    const afterFil = app.state.ui.filamentThicknessCache[afterRun.id]?.[filamentId];
    if (!beforeFil || !afterFil) return null;
    return app.commands.computeSolveCapDiff(beforeFil, afterFil);
  }

function buildSolveCapDiffSummaryHtml(diff) {
    if (!diff) return `<div class="muted-line">Diff unavailable for this pair.</div>`;
    return `
      <div class="solve-diff-stat"><span class="solve-diff-stat-label">Changed</span><span class="solve-diff-stat-value">${diff.changedPx.toLocaleString()} px</span></div>
      <div class="solve-diff-stat"><span class="solve-diff-stat-label">Added</span><span class="solve-diff-stat-value">${diff.addedPx.toLocaleString()} px</span></div>
      <div class="solve-diff-stat"><span class="solve-diff-stat-label">Removed</span><span class="solve-diff-stat-value">${diff.removedPx.toLocaleString()} px</span></div>
      <div class="solve-diff-stat"><span class="solve-diff-stat-label">Before active</span><span class="solve-diff-stat-value">${diff.beforeActivePx.toLocaleString()} px</span></div>
      <div class="solve-diff-stat"><span class="solve-diff-stat-label">After active</span><span class="solve-diff-stat-value">${diff.afterActivePx.toLocaleString()} px</span></div>
      <div class="solve-diff-stat"><span class="solve-diff-stat-label">Max abs delta</span><span class="solve-diff-stat-value">${app.commands.formatSolveDiffMm(diff.maxAbsDelta)}</span></div>
      <div class="solve-diff-stat"><span class="solve-diff-stat-label">Mean abs delta</span><span class="solve-diff-stat-value">${app.commands.formatSolveDiffMm(diff.meanAbsDelta)}</span></div>`;
  }

function renderSolveInspectorPanel(selectedRuns, view) {
    const card = app.state.ui.$("#solveInspectorCard");
    const grid = app.state.ui.$("#solveInspectorGrid");
    if (!card || !grid) return;
    if (!selectedRuns.length) {
      card.style.display = "none";
      grid.innerHTML = "";
      return;
    }
    const blocks = [];
    if (app.commands.isSolveThicknessDiffView(view) && selectedRuns.length === 2) {
      const [beforeRun, afterRun] = selectedRuns;
      const settingDiffs = app.commands.collectSolveRunSettingDiffs(beforeRun, afterRun);
      if (settingDiffs.length) {
        blocks.push(...app.commands.buildGroupedSolveSettingDiffBlocks(beforeRun, afterRun, settingDiffs));
      }
      if (app.commands.isSolveCapDiffView(view)) {
        const beforeCap = app.state.ui.capThicknessCache[beforeRun.id];
        const afterCap = app.state.ui.capThicknessCache[afterRun.id];
        if (beforeCap && afterCap) {
          const diff = app.commands.computeSolveCapDiff(beforeCap, afterCap);
          if (diff) {
            blocks.push(app.commands.buildSolveInspectorBlock("Cap Diff Summary", [
              { label: "Changed pixels", value: `${diff.changedPx.toLocaleString()} px` },
              { label: "Added pixels", value: `${diff.addedPx.toLocaleString()} px` },
              { label: "Removed pixels", value: `${diff.removedPx.toLocaleString()} px` },
              { label: "Max abs delta", value: app.commands.formatSolveDiffMm(diff.maxAbsDelta) },
              { label: "Mean abs delta", value: app.commands.formatSolveDiffMm(diff.meanAbsDelta) },
            ], `${beforeRun.label} -> ${afterRun.label}`));
          }
        }
      } else {
        const filamentId = app.commands.ensureSolveFilamentDiffSelection(selectedRuns);
        const fil = app.commands.filamentById(filamentId);
        const beforeFil = app.state.ui.filamentThicknessCache[beforeRun.id]?.[filamentId];
        const afterFil = app.state.ui.filamentThicknessCache[afterRun.id]?.[filamentId];
        if (beforeFil && afterFil) {
          const diff = app.commands.computeSolveCapDiff(beforeFil, afterFil);
          if (diff) {
            blocks.push(app.commands.buildSolveInspectorBlock(`${fil?.color_name || filamentId} Diff Summary`, [
              { label: "Changed pixels", value: `${diff.changedPx.toLocaleString()} px` },
              { label: "Added pixels", value: `${diff.addedPx.toLocaleString()} px` },
              { label: "Removed pixels", value: `${diff.removedPx.toLocaleString()} px` },
              { label: "Before active", value: `${diff.beforeActivePx.toLocaleString()} px` },
              { label: "After active", value: `${diff.afterActivePx.toLocaleString()} px` },
              { label: "Max abs delta", value: app.commands.formatSolveDiffMm(diff.maxAbsDelta) },
              { label: "Mean abs delta", value: app.commands.formatSolveDiffMm(diff.meanAbsDelta) },
            ], `${beforeRun.label} -> ${afterRun.label}`));
          }
        }
      }
    }
    if (!blocks.length) {
      card.style.display = "none";
      grid.innerHTML = "";
      return;
    }
    grid.innerHTML = blocks.join("");
    card.style.display = "";
  }

function buildSolveCapDiffColumn(beforeRun, afterRun, aspect) {
    return `
      <div class="solve-grid-column is-diff" data-solve-card-kind="diff">
        <div class="solve-grid-column-header">
          <h4>Cap Diff</h4>
          <div class="comparison-column-chips" aria-hidden="true" style="visibility:hidden"><span class="comparison-chip"></span></div>
          <div class="comparison-column-stats" id="solveCapDiffStats">Loading diff...</div>
        </div>
        <div class="solve-grid-img-wrapper solve-grid-capdiff-wrap" style="--img-aspect:${aspect}">
          <canvas class="solve-grid-capdiff-canvas" id="solveCapDiffCanvas" aria-label="Cap difference"></canvas>
        </div>
        ${app.commands.buildSolveCardScaleBarSlot()}
        <div class="solve-diff-summary" id="solveCapDiffSummary"></div>
      </div>`;
  }

function buildSolveFilamentRunColumn(run, aspect, filamentId) {
    const mapInfo = app.commands.getRunFilamentMapInfo(run, filamentId);
    const fil = app.commands.filamentById(filamentId);
    const label = fil?.color_name || filamentId;
    const statsLine = `${(mapInfo?.active_px || 0).toLocaleString()} px · max ${(mapInfo?.max_d || 0).toFixed(2)} mm`;
    const visual = mapInfo?.map_url
      ? `<img class="solve-grid-img solve-grid-filament-thickness-img" src="${app.commands.esc(mapInfo.map_url)}" alt="${app.commands.esc(label)}">`
      : `<div class="solve-grid-empty-map">No thickness</div>`;
    const thicknessData = mapInfo?.map_url
      ? ` data-thickness-url="${app.commands.esc(mapInfo.map_url)}" data-thickness-label="${app.commands.esc(`${run.label} · ${label}`)}"`
      : "";
    return `
      <div class="solve-grid-column" data-solve-card-kind="thickness" data-run-id="${app.commands.esc(run.id)}"${thicknessData}>
        <div class="solve-grid-column-header">
          <h4>${app.commands.esc(run.label)}</h4>
          <div class="comparison-column-chips"><span class="comparison-chip" style="background:${app.commands.esc(fil?.hex || "#888")}"></span></div>
          <div class="comparison-column-stats">${app.commands.esc(label)} · ${statsLine}</div>
        </div>
        <div class="solve-grid-img-wrapper" style="--img-aspect:${aspect}">
          ${visual}
        </div>
        ${app.commands.buildSolveCardScaleBarSlot()}
      </div>`;
  }

function buildSolveFilamentDiffColumn(beforeRun, afterRun, filamentId, aspect) {
    const fil = app.commands.filamentById(filamentId);
    const label = fil?.color_name || filamentId;
    return `
      <div class="solve-grid-column is-diff" data-solve-card-kind="diff">
        <div class="solve-grid-column-header">
          <h4>${app.commands.esc(label)} Diff</h4>
          <div class="comparison-column-chips" aria-hidden="true"><span class="comparison-chip" style="background:${app.commands.esc(fil?.hex || "#888")}"></span></div>
          <div class="comparison-column-stats" id="solveFilamentDiffStats">Loading diff...</div>
        </div>
        <div class="solve-grid-img-wrapper solve-grid-capdiff-wrap" style="--img-aspect:${aspect}">
          <canvas class="solve-grid-capdiff-canvas" id="solveFilamentDiffCanvas" aria-label="${app.commands.esc(label)} difference"></canvas>
        </div>
        ${app.commands.buildSolveCardScaleBarSlot()}
        <div class="solve-diff-summary" id="solveFilamentDiffSummary"></div>
      </div>`;
  }

async function renderSolveCapDiffColumn(beforeRun, afterRun) {
    const statsEl = app.state.ui.$("#solveCapDiffStats");
    const summaryEl = app.state.ui.$("#solveCapDiffSummary");
    const canvas = app.state.ui.$("#solveCapDiffCanvas");
    if (!statsEl || !summaryEl || !canvas) return;
    const displayedView = app.state.solve.solveView;
    statsEl.textContent = "Loading diff...";
    summaryEl.innerHTML = `<div class="muted-line">Loading exact cap delta...</div>`;
    const [beforeCap, afterCap] = await Promise.all([
      app.commands.ensureCapThickness(beforeRun),
      app.commands.ensureCapThickness(afterRun),
    ]);
    // If the user switched views during the async load, don't clobber the new view's
    // shared legend/inspector with stale diff state.
    if (app.state.solve.solveView !== displayedView) return;
    if (!beforeCap || !afterCap) {
      statsEl.textContent = "Cap blob unavailable";
      summaryEl.innerHTML = `<div class="muted-line">Couldn't load cap thickness data for this pair. Older runs may need to be re-solved after this patch.</div>`;
      app.commands.updateSolveLegend();
      app.commands.renderSolveInspectorPanel(app.commands.getSelectedRuns().filter(r => r.results), app.state.solve.solveView);
      return;
    }
    const diff = app.commands.computeSolveCapDiff(beforeCap, afterCap);
    if (!diff) {
      statsEl.textContent = "Cap diff unavailable";
      summaryEl.innerHTML = `<div class="muted-line">Cap maps do not share the same dimensions.</div>`;
      app.commands.updateSolveLegend();
      app.commands.renderSolveInspectorPanel(app.commands.getSelectedRuns().filter(r => r.results), app.state.solve.solveView);
      return;
    }
    app.commands.renderSolveCapDiffCanvas(canvas, diff, app.state.solve.solveCapDiffMode);
    statsEl.textContent = `${diff.changedPx.toLocaleString()} px changed · max ${app.commands.formatSolveDiffMm(diff.maxAbsDelta)}`;
    summaryEl.innerHTML = app.commands.buildSolveCapDiffSummaryHtml(diff);
    app.commands.updateSolveLegend();
    app.commands.renderSolveInspectorPanel(app.commands.getSelectedRuns().filter(r => r.results), app.state.solve.solveView);
  }

async function renderSolveFilamentDiffColumn(beforeRun, afterRun, filamentId) {
    const statsEl = app.state.ui.$("#solveFilamentDiffStats");
    const summaryEl = app.state.ui.$("#solveFilamentDiffSummary");
    const canvas = app.state.ui.$("#solveFilamentDiffCanvas");
    if (!statsEl || !summaryEl || !canvas) return;
    const fil = app.commands.filamentById(filamentId);
    const label = fil?.color_name || filamentId || "Selected filament";
    const displayedView = app.state.solve.solveView;
    statsEl.textContent = "Loading diff...";
    summaryEl.innerHTML = `<div class="muted-line">Loading exact ${app.commands.esc(label)} thickness delta...</div>`;
    const [beforeFil, afterFil] = await Promise.all([
      app.commands.ensureFilamentThickness(beforeRun, filamentId),
      app.commands.ensureFilamentThickness(afterRun, filamentId),
    ]);
    // If the user switched views during the async load, don't clobber the new view's
    // shared legend/inspector with stale diff state.
    if (app.state.solve.solveView !== displayedView) return;
    if (!beforeFil || !afterFil) {
      statsEl.textContent = "Filament blob unavailable";
      summaryEl.innerHTML = `<div class="muted-line">Couldn't load thickness data for ${app.commands.esc(label)}. Older runs may need to be re-solved after this patch.</div>`;
      app.commands.updateSolveLegend();
      app.commands.renderSolveInspectorPanel(app.commands.getSelectedRuns().filter((r) => r.results), app.state.solve.solveView);
      return;
    }
    const diff = app.commands.computeSolveCapDiff(beforeFil, afterFil);
    if (!diff) {
      statsEl.textContent = "Color diff unavailable";
      summaryEl.innerHTML = `<div class="muted-line">Selected filament maps do not share the same dimensions.</div>`;
      app.commands.updateSolveLegend();
      app.commands.renderSolveInspectorPanel(app.commands.getSelectedRuns().filter((r) => r.results), app.state.solve.solveView);
      return;
    }
    app.commands.renderSolveCapDiffCanvas(canvas, diff, app.state.solve.solveCapDiffMode);
    statsEl.textContent = `${diff.changedPx.toLocaleString()} px changed · max ${app.commands.formatSolveDiffMm(diff.maxAbsDelta)}`;
    summaryEl.innerHTML = app.commands.buildSolveCapDiffSummaryHtml(diff);
    app.commands.updateSolveLegend();
    app.commands.renderSolveInspectorPanel(app.commands.getSelectedRuns().filter((r) => r.results), app.state.solve.solveView);
  }

function renderSolveComparisonGrid() {
    const grid = app.state.ui.$("#solveComparisonGrid");
    const emptyMsg = app.state.ui.$("#solveGridEmpty");
    const viewBar = app.state.ui.$("#solveViewBar");
    const subControls = app.state.ui.$("#solveSubControls");
    const thickCard = app.state.ui.$("#solveThicknessCard");
    const legendRow = app.state.ui.$("#solveLegend");
    const legendContent = app.state.ui.$("#solveLegendContent");
    const viewCaption = app.state.ui.$("#solveViewCaption");
    const mapsGrid = app.state.ui.$("#filamentMapsGrid");
    if (!grid) return;

    // Removed views (Palette Fit, old Diagnostic Views) and the parked diff views must never render
    // for normal users; coerce stale/console-set state to predicted before anything reads solveView.
    app.commands.coerceSolveViewForAccess();
    const devViews = app.commands.isSolveDevViewsEnabled();
    const parkedGroup = app.state.ui.$("#solveParkedViews");
    const parkedLabel = app.state.ui.$("#solveParkedLabel");
    if (parkedGroup) parkedGroup.hidden = !devViews;
    if (parkedLabel) parkedLabel.hidden = !devViews;

    const selected = app.commands.getSelectedRuns().filter(r => r.results);
    if (selected.length === 0) {
      grid.style.display = "";
      grid.innerHTML = "";
      if (emptyMsg) {
        grid.appendChild(emptyMsg);
        emptyMsg.classList.remove("is-hidden");
      }
      if (thickCard) thickCard.style.display = "none";
      if (mapsGrid) mapsGrid.innerHTML = "";
      if (legendRow) legendRow.classList.add("is-hidden");
      if (legendContent) legendContent.innerHTML = "";
      if (viewCaption) viewCaption.classList.add("is-hidden");
      if (viewBar) viewBar.style.display = "none";
      if (subControls) subControls.style.display = "none";
      app.commands.renderSolveInspectorPanel([], app.state.solve.solveView);
      return;
    }
    if (emptyMsg) emptyMsg.classList.add("is-hidden");
    if (viewBar) viewBar.style.display = "";
    if (subControls) subControls.style.display = "";

    const view = app.state.solve.solveView;
    app.commands.syncSolveViewToggleActive();

    if (view === "thickness_maps") {
      grid.style.display = "none";
      if (thickCard) thickCard.style.display = "";
      // Keep the shared sub-controls host visible so the contextual caption remains aligned
      // with the other solve views. Thickness Maps itself has no selectable sub-view state.
      app.commands.updateSolveSubControls();
      app.commands.updateSolveLegend(view);
      app.commands.renderSolveInspectorPanel(selected, view);
      app.commands.renderSolveThicknessMaps(selected);
      return;
    }
    grid.style.display = "";
    if (thickCard) thickCard.style.display = "none";

    if (view === "cap_diff" || view === "filament_diff") {
      app.commands.syncSolveFilamentDiffControl(selected);
      app.commands.updateSolveSubControls();
      if (selected.length !== 2) {
        grid.innerHTML = `<p class="muted-line">Select exactly two completed solve runs to view ${view === "cap_diff" ? "Cap Diff" : "Color Diff"}.</p>`;
        app.commands.renderSolveInspectorPanel(selected, view);
        app.commands.updateSolveLegend();
        return;
      }
      const [beforeRun, afterRun] = selected;
      const aspect = app.commands._runAspect(afterRun);
      let html = app.commands.shouldShowSolveSourceColumn(view) ? app.commands.buildSolveSourceColumn(beforeRun, app.commands._runAspect(beforeRun), view) : "";
      if (view === "cap_diff") {
        const capDiffOpts = (run) => ({ cardKind: "thickness", lightboxUrl: run.results.total_surface_url, lightboxLabel: `${run.label} · Top Surface` });
        html += app.commands.buildSolveRunVisualColumn(beforeRun, app.commands._runAspect(beforeRun), "total_surface", capDiffOpts(beforeRun));
        html += app.commands.buildSolveRunVisualColumn(afterRun, aspect, "total_surface", capDiffOpts(afterRun));
        html += app.commands.buildSolveCapDiffColumn(beforeRun, afterRun, aspect);
      } else {
        const filamentId = app.commands.ensureSolveFilamentDiffSelection(selected);
        if (!filamentId) {
          grid.innerHTML = `<p class="muted-line">No color filament thickness maps are available for these selected runs.</p>`;
          app.commands.renderSolveInspectorPanel(selected, view);
          app.commands.updateSolveLegend();
          return;
        }
        html += app.commands.buildSolveFilamentRunColumn(beforeRun, app.commands._runAspect(beforeRun), filamentId);
        html += app.commands.buildSolveFilamentRunColumn(afterRun, aspect, filamentId);
        html += app.commands.buildSolveFilamentDiffColumn(beforeRun, afterRun, filamentId, aspect);
      }
      grid.innerHTML = html;

      app.commands.updateSolveColumnImages();
      app.commands.updateSolveLegend();
      if (view === "cap_diff") app.commands.renderSolveCapDiffColumn(beforeRun, afterRun);
      else app.commands.renderSolveFilamentDiffColumn(beforeRun, afterRun, app.commands.ensureSolveFilamentDiffSelection(selected));

      // Clicks are handled by the delegated #solveComparisonGrid listener via
      // openSolveCardLightboxFromElement(), keyed on each card's data-solve-card-kind.
      return;
    }
    app.commands.renderSolveInspectorPanel(selected, view);

    let html = app.commands.shouldShowSolveSourceColumn(view) ? app.commands.buildSolveSourceColumn(selected[0], app.commands._runAspect(selected[0]), view) : "";
    selected.forEach((run) => {
      // The Color Regions tab has a sub-tab toggle (solveColorRegionsView): one
      // card region shows EITHER the color-ceiling height map (default) OR the
      // recipe viewer — never both. The recipe card needs its data (color-only
      // render + cookbook); compare-mode / pre-feature runs lack it, so we fall
      // back to the ceiling card there rather than a blank, dead card.
      const _rr = run.results || {};
      const recipeReady = _rr.predicted_color_only_appearance_url
        && _rr.color_recipe_breakdown_cookbook_url;
      if (view === "color_ceiling" && app.state.solve.solveColorRegionsView === "recipe_regions" && recipeReady) {
        html += app.commands.buildSolveRecipeColumn(run, app.commands._runAspect(run));
      } else {
        html += app.commands.buildSolveRunColumn(run, app.commands._runAspect(run));
      }
    });
    grid.innerHTML = html;

    app.commands.updateSolveSubControls();
    app.commands.updateSolveColumnImages();
    app.commands.updateSolveLegend();

    // Clicks are handled by the delegated #solveComparisonGrid listener via
    // openSolveCardLightboxFromElement(), keyed on each card's data-solve-card-kind.
  }

  Object.assign(app.commands, {
    invalidateSolveRunCaches,
    ensureCapThickness,
    ensureFilamentThickness,
    isSolveCapDiffView,
    isSolveFilamentDiffView,
    isSolveThicknessDiffView,
    getRunFilamentMapInfo,
    getSolveFilamentDiffOptions,
    ensureSolveFilamentDiffSelection,
    syncSolveFilamentDiffControl,
    computeSolveCapDiff,
    renderSolveCapDiffCanvas,
    formatSolveDiffMm,
    getCurrentSolveCapDiffFromCache,
    getCurrentSolveFilamentDiffFromCache,
    buildSolveCapDiffSummaryHtml,
    renderSolveInspectorPanel,
    buildSolveCapDiffColumn,
    buildSolveFilamentRunColumn,
    buildSolveFilamentDiffColumn,
    renderSolveCapDiffColumn,
    renderSolveFilamentDiffColumn,
    renderSolveComparisonGrid,
  });
}
