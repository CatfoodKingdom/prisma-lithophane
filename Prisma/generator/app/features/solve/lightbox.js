/**
 * Install the solve/lightbox feature commands.
 * @param {import("../../core/types.js").ApplicationContext} app
 */
export function installFeaturesSolveLightbox(app) {
function getSolveLightboxViewLabel(view) {
    switch (view) {
      case "source": return "Source";
      case "predicted": return "Predicted";
      case "cap_map": return "Total White Cap";
      case "boundary_cap_map": return "Boundary Cap";
      case "detail_cap_map": return "Detail Cap";
      case "color_ceiling": return "Color Ceiling";
      case "recipe_regions": return "Color Regions";
      case "total_surface": return "Top Surface";
      case "surface_highpass": return "Highpass";
      case "surface_explorer": return "Explorer";
      default: return "Result";
    }
  }

function buildSolveRunPaletteChips(run) {
    return (run.palette || []).map((fid) => {
      const fil = app.state.session.allFilaments.find((f) => f.filament_id === fid);
      const hex = fil?.hex || "#888";
      const title = fil?.display_name || fil?.color_name || fid;
      return `<span class="comp-lightbox-chip" style="background:${hex}" title="${app.commands.esc(title)}"></span>`;
    }).join("");
  }

function buildSolveLightboxHeader(run, viewLabel, trailingControls = "") {
    const paletteChips = app.commands.buildSolveRunPaletteChips(run);
    return `
      <div class="comp-lightbox-topbar surface-lightbox-topbar">
        <div class="comp-lightbox-runmeta">
          <span class="comp-lightbox-runtitle">${app.commands.esc(run.label)}</span>
          <span class="comp-lightbox-viewtag">${app.commands.esc(viewLabel)}</span>
        </div>
        <div class="comp-lightbox-header-end">
          ${trailingControls}
          <div class="comp-lightbox-palette" aria-label="Palette">${paletteChips}</div>
        </div>
      </div>
    `;
  }

function getSelectedSolveRunsWithResults() {
    return app.commands.getSelectedRuns().filter(r => r.results);
  }

function getSolveWhiteCapThicknessItems(run) {
    const r = run?.results || {};
    return [
      {
        key: "cap:total",
        label: "Total White Cap",
        viewLabel: "Total White Cap Thickness",
        url: r.cap_map_url,
        activePx: Number(r.cap_map_active_px || 0),
        maxD: Number(r.cap_map_max_d || 0),
        volumeMm3: r.cap_map_volume_mm3,
      }, {
        key: "cap:boundary",
        label: "Boundary Cap",
        viewLabel: "Boundary Cap Thickness",
        url: r.boundary_cap_map_url,
        activePx: Number(r.boundary_cap_map_active_px || 0),
        maxD: Number(r.boundary_cap_map_max_d || 0),
        volumeMm3: r.boundary_cap_map_volume_mm3,
      }, {
        key: "cap:detail",
        label: "Detail Cap",
        viewLabel: "Detail Cap Thickness",
        url: r.detail_cap_map_url,
        activePx: Number(r.detail_cap_map_active_px || 0),
        maxD: Number(r.detail_cap_map_max_d || 0),
        volumeMm3: r.detail_cap_map_volume_mm3,
      },
    ].map(item => ({ ...item, available: Boolean(item.url) }));
  }

function getSolveThicknessDisplayItems(run) {
    if (!run?.results) return [];
    const maps = run.results.filament_maps || [];
    const items = maps.map((m) => {
      const fil = app.commands.filamentById(m.filament_id);
      return {
        key: `filament:${m.filament_id}`,
        label: m.display_name || fil?.color_name || fil?.display_name || m.filament_id,
        viewLabel: `${m.display_name || fil?.color_name || fil?.display_name || m.filament_id} Thickness`,
        url: m.map_url || "",
        available: Boolean(m.map_url),
      };
    });
    items.push(...app.commands.getSolveWhiteCapThicknessItems(run));
    return items;
  }

function getSolveThicknessItems(run) {
    return app.commands.getSolveThicknessDisplayItems(run).filter(item => item.available);
  }

function openThicknessLightboxForKey(runId, mapKey) {
    const run = app.state.solve.solveRuns.find(r => r.id === runId);
    const items = app.commands.getSolveThicknessItems(run);
    const idx = items.findIndex(candidate => candidate.key === mapKey);
    const item = idx >= 0 ? items[idx] : null;
    if (!run || !item) return;
    app.state.solve._solveLightboxState = { kind: "thickness", runId, mapKey: item.key, mapIndex: idx };

    const lb = app.state.ui.$("#compLightbox");
    const content = app.state.ui.$("#compLightboxContent");
    if (!lb || !content) return;
    const lifecycle = app.commands.beginLightboxLifecycle();
    const zoomControls = app.commands.buildStaticLightboxZoomControls();
    content.innerHTML = `
      <div class="comp-lightbox-pane">
        ${app.commands.buildSolveLightboxHeader(run, item.viewLabel, zoomControls)}
        <div class="comp-lightbox-media static-zoom-media">
          <img class="comp-lightbox-img" src="${app.commands.esc(item.url)}" style="image-rendering:pixelated;" alt="${app.commands.esc(item.label)}">
        </div>
      </div>`;
    lb.classList.remove("is-hidden");
    app.commands.setupStaticLightboxZoom(content, lifecycle);
  }

function solveRunById(runId) {
    if (!runId) return null;
    return app.state.solve.solveRuns.find(r => r.id === runId) || null;
  }

function openSolvePreviewLightboxForRun(run, view = app.state.solve.solveView, targetKind = "run") {
    if (!run) return;
    if (targetKind === "surface") {
      app.commands.openSurfaceLightbox(view, run.id);
    } else if (targetKind === "recipe") {
      app.commands.openRecipeLightbox(run.id);
    } else {
      app.commands.openSolveRunLightbox(run.id, view);
    }
  }

function openSolveCardLightboxFromElement(card) {
    if (!card) return;
    const kind = card.dataset.solveCardKind || "";
    const runId = card.dataset.runId || "";
    if (kind === "source") {
      const run = app.commands.solveRunById(runId);
      if (run) {
        app.commands.openSolveSourceLightbox(
          run,
          card.dataset.view || app.state.solve.solveView,
          card.dataset.sourceTargetKind || "run",
        );
      }
      return;
    }
    if (kind === "run") {
      if (runId) app.commands.openSolveRunLightbox(runId, card.dataset.view || app.state.solve.solveView);
      return;
    }
    if (kind === "surface") {
      if (runId) app.commands.openSurfaceLightbox(card.dataset.view || app.state.solve.solveView, runId);
      return;
    }
    if (kind === "recipe") {
      if (runId) app.commands.openRecipeLightbox(runId);
      return;
    }
    if (kind === "thickness") {
      if (runId && card.dataset.mapKey) app.commands.openThicknessLightboxForKey(runId, card.dataset.mapKey);
      return;
    }
    // kind === "diff" (or unknown): no lightbox — preserves the prior no-op for diff columns.
  }

function beginLightboxLifecycle() {
    if (app.state.solve._lightboxCleanup) app.state.solve._lightboxCleanup();
    const token = ++app.state.solve._lightboxInstanceToken;
    const cleanups = [];
    let active = true;
    const cleanup = () => {
      if (!active) return;
      active = false;
      while (cleanups.length) {
        const dispose = cleanups.pop();
        try { dispose(); } catch (_error) { /* cleanup remains best-effort and idempotent */ }
      }
      if (app.state.solve._lightboxCleanup === cleanup) app.state.solve._lightboxCleanup = null;
    };
    app.state.solve._lightboxCleanup = cleanup;
    return {
      token,
      addCleanup(dispose) { if (active) cleanups.push(dispose); else dispose(); },
      isActive() { return active && token === app.state.solve._lightboxInstanceToken; },
      cleanup,
    };
  }

function computeLightboxScaleBounds({
    intrinsicWidth,
    intrinsicHeight,
    viewportWidth,
    viewportHeight,
    headerHeight = 0,
    headerMinWidth = 0,
    inset = 24,
  }) {
    const imageWidth = Math.max(1, Number(intrinsicWidth) || 1);
    const imageHeight = Math.max(1, Number(intrinsicHeight) || 1);
    const usableWidth = Math.max(1, (Number(viewportWidth) || 1) - inset * 2);
    const usableHeight = Math.max(1, (Number(viewportHeight) || 1) - inset * 2);
    const mediaHeight = Math.max(1, usableHeight - Math.max(0, Number(headerHeight) || 0));
    const fitScale = Math.max(0.0001, Math.min(usableWidth / imageWidth, mediaHeight / imageHeight));
    const collapsed = fitScale < 1;
    const minScale = collapsed ? fitScale : 1;
    const maxScale = fitScale;
    return {
      minScale,
      maxScale,
      collapsed,
      usableWidth,
      usableHeight,
      headerWidth: Math.min(usableWidth, Math.max(0, Number(headerMinWidth) || 0)),
    };
  }

function normalizeStaticZoomWheelDelta(event, viewportHeight = window.innerHeight) {
    const multiplier = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? viewportHeight : 1;
    return Number(event.deltaY || 0) * multiplier;
  }

function applyStaticZoomWheelDelta(value, accumulatedDelta, deltaPixels, threshold = 36, step = 5) {
    let accumulated = accumulatedDelta + deltaPixels;
    let next = Number(value) || 0;
    while (Math.abs(accumulated) >= threshold) {
      const direction = accumulated < 0 ? 1 : -1;
      const candidate = Math.max(0, Math.min(100, next + direction * step));
      accumulated -= Math.sign(accumulated) * threshold;
      if (candidate === next) {
        accumulated = 0;
        break;
      }
      next = candidate;
    }
    return { value: next, accumulatedDelta: accumulated, changed: next !== Number(value) };
  }

function buildStaticLightboxZoomControls() {
    return `
      <label class="comp-lightbox-zoom">
        <span class="comp-lightbox-zoom-label">Zoom</span>
        <span class="comp-lightbox-zoom-endpoint">Min</span>
        <input class="comp-lightbox-zoom-slider" type="range" min="0" max="100" step="1" value="100" aria-label="Zoom" aria-valuetext="100%, maximum">
        <span class="comp-lightbox-zoom-endpoint">Max</span>
      </label>`;
  }

function setupStaticLightboxZoom(content, lifecycle, onLayout = null) {
    const pane = content?.querySelector(".comp-lightbox-pane");
    const header = pane?.querySelector(".comp-lightbox-topbar");
    const media = pane?.querySelector(".static-zoom-media");
    const image = media?.querySelector(".comp-lightbox-img");
    const slider = header?.querySelector(".comp-lightbox-zoom-slider");
    if (!pane || !header || !media || !image || !slider) return;
    let wheelDelta = 0;

    const updateAccessibleValue = () => {
      const value = Number(slider.value);
      const endpoint = value === 0 ? ", minimum" : value === 100 ? ", maximum" : "";
      slider.setAttribute("aria-valuetext", `${value}%${endpoint}`);
    };

    const relayout = () => {
      if (!lifecycle.isActive()) return;
      const intrinsicWidth = image.naturalWidth;
      const intrinsicHeight = image.naturalHeight;
      if (!intrinsicWidth || !intrinsicHeight) return;
      const bounds = app.commands.computeLightboxScaleBounds({
        intrinsicWidth,
        intrinsicHeight,
        viewportWidth: window.innerWidth,
        viewportHeight: window.innerHeight,
        headerHeight: header.getBoundingClientRect().height,
        headerMinWidth: header.scrollWidth,
      });
      slider.disabled = bounds.collapsed;
      const normalized = bounds.collapsed ? 100 : Number(slider.value) / 100;
      const scale = bounds.minScale + (bounds.maxScale - bounds.minScale) * normalized;
      const width = Math.max(1, Math.floor(intrinsicWidth * scale));
      const height = Math.max(1, Math.floor(intrinsicHeight * scale));
      image.style.width = `${width}px`;
      image.style.height = `${height}px`;
      updateAccessibleValue();
      if (onLayout) onLayout();
    };

    const onInput = () => relayout();
    const onWheel = (event) => {
      if (event.ctrlKey || event.metaKey) return;
      const outcome = app.commands.applyStaticZoomWheelDelta(
        Number(slider.value),
        wheelDelta,
        app.commands.normalizeStaticZoomWheelDelta(event),
      );
      wheelDelta = outcome.accumulatedDelta;
      if (!outcome.changed) return;
      slider.value = String(outcome.value);
      relayout();
      event.preventDefault();
    };
    const onResize = () => relayout();
    const onLoad = () => relayout();
    slider.addEventListener("input", onInput);
    media.addEventListener("wheel", onWheel, { passive: false });
    image.addEventListener("load", onLoad);
    window.addEventListener("resize", onResize);
    lifecycle.addCleanup(() => slider.removeEventListener("input", onInput));
    lifecycle.addCleanup(() => media.removeEventListener("wheel", onWheel));
    lifecycle.addCleanup(() => image.removeEventListener("load", onLoad));
    lifecycle.addCleanup(() => window.removeEventListener("resize", onResize));
    if (typeof ResizeObserver !== "undefined") {
      const observer = new ResizeObserver(() => relayout());
      observer.observe(header);
      lifecycle.addCleanup(() => observer.disconnect());
    }
    if (image.complete && image.naturalWidth) relayout();
    if (typeof image.decode === "function") {
      image.decode().catch(() => {}).then(() => { if (lifecycle.isActive()) relayout(); });
    }
  }

function openSolveRunLightbox(runId, view = app.state.solve.solveView) {
    const run = app.state.solve.solveRuns.find(r => r.id === runId);
    if (!run || !run.results) return;
    // Capture the displayed view in the lightbox state so arrow-key navigation between runs
    // keeps showing the same view, independent of any later global solveView change.
    app.state.solve._solveLightboxState = { kind: "solve", runId, view };

    const lb = app.state.ui.$("#compLightbox");
    const content = app.state.ui.$("#compLightboxContent");
    if (!lb || !content) return;
    const lifecycle = app.commands.beginLightboxLifecycle();

    const url = app.commands._getSolveRunResultUrl(run.results, view) || "";
    const pixelated = "image-rendering:pixelated;";
    const zoomControls = app.commands.buildStaticLightboxZoomControls();

    const contourLabel = view === "recipe_regions" ? "recipe boundaries" : "layer contours";
    const contourCanvas = app.commands.isSolveContourView(view) && app.state.solve.solveContoursEnabled
      ? `<canvas class="solve-lightbox-contour-canvas" aria-label="${app.commands.escAttr(run.label)} ${contourLabel}"></canvas>`
      : "";
    content.innerHTML = `
          <div class="comp-lightbox-pane">
            ${app.commands.buildSolveLightboxHeader(run, app.commands.getSolveLightboxViewLabel(view), zoomControls)}
            <div class="comp-lightbox-media static-zoom-media">
              <img class="comp-lightbox-img" src="${url}" style="${pixelated}">
              ${contourCanvas}
            </div>
          </div>`;
    lb.classList.remove("is-hidden");

    const renderContours = () => {
      if (app.commands.isSolveContourView(view) && app.state.solve.solveContoursEnabled) app.commands.renderSolveLightboxContours(run, view);
    };
    app.commands.setupStaticLightboxZoom(content, lifecycle, renderContours);
  }

function openSolveSourceLightbox(run, view = app.state.solve.solveView, targetKind = "run") {
    if (!run || !run.results) return;
    app.state.solve._solveLightboxState = { kind: "source", runId: run.id, view, targetKind };
    const lb = app.state.ui.$("#compLightbox");
    const content = app.state.ui.$("#compLightboxContent");
    if (!lb || !content) return;
    const lifecycle = app.commands.beginLightboxLifecycle();
    const pixelated = "image-rendering:pixelated;";
    const zoomControls = app.commands.buildStaticLightboxZoomControls();
    content.innerHTML = `
          <div class="comp-lightbox-pane">
            ${app.commands.buildSolveLightboxHeader(run, app.commands.getSolveLightboxViewLabel("source"), zoomControls)}
            <div class="comp-lightbox-media static-zoom-media">
              <img class="comp-lightbox-img" src="${app.commands.esc(run.results.source_url || "")}" style="${pixelated}" alt="Source image">
            </div>
          </div>`;
    lb.classList.remove("is-hidden");
    app.commands.setupStaticLightboxZoom(content, lifecycle);
  }

async function openSurfaceLightbox(viewType, runId = null) {
    const lb = app.state.ui.$("#compLightbox");
    const content = app.state.ui.$("#compLightboxContent");
    if (!lb || !content) return;
    const selected = app.commands.getSelectedRuns().filter(r => r.results);
    if (!selected.length) return;
    const run = runId ? app.state.solve.solveRuns.find(r => r.id === runId) : selected[0];
    if (!run) return;
    const lifecycle = app.commands.beginLightboxLifecycle();
    app.state.solve._solveLightboxState = { kind: "surface", runId: run.id, viewType };
    const cached = await app.commands.ensureSurfaceData(run);
    if (!lifecycle.isActive() || !cached) return;

    const tMax = app.commands.getSolveSurfaceTMax();
    const lh = app.commands.getSolveSurfaceLayerHeight();
    const base = app.commands.getSolveSurfaceBaseThickness();
    const steps = app.commands.getSolveSurfaceExtraSteps();

    // Copy current slider values from the inline controls
    const curThreshold = parseInt(app.state.ui.$("#highpassThresholdSlider")?.value ?? steps);
    const curHeight = parseInt(app.state.ui.$("#explorerHeightSlider")?.value ?? steps);
    const curBand = parseInt(app.state.ui.$("#explorerBandSlider")?.value ?? 3);

    let html = `<div class="surface-lightbox-wrap">
      ${app.commands.buildSolveLightboxHeader(run, app.commands.getSolveLightboxViewLabel(viewType))}`;
    if (viewType === "surface_highpass") {
      html += `
        <div class="surface-controls surface-lightbox-controls">
          <label class="surface-control-label">
            Threshold:
            <span class="surface-control-value" id="lbHighpassValue"></span>
            <span class="surface-control-hint" id="lbHighpassHint"></span>
          </label>
          <input type="range" id="lbHighpassSlider" min="0" max="${steps}" value="${curThreshold}" step="1" class="surface-slider">
        </div>`;
    } else {
      // Explorer is always rich (samples a single center layer), so no band control.
      html += `
        <div class="surface-controls surface-lightbox-controls">
          <label class="surface-control-label">
            Height:
            <span class="surface-control-value" id="lbExplorerHeightValue"></span>
            <span class="surface-control-hint" id="lbExplorerHeightHint"></span>
          </label>
          <input type="range" id="lbExplorerHeightSlider" min="0" max="${steps}" value="${curHeight}" step="1" class="surface-slider">
        </div>`;
    }
    html += `
        <div class="surface-lightbox-frame">
          <canvas class="surface-lightbox-canvas" id="lbSurfaceCanvas"></canvas>
          <canvas class="solve-lightbox-contour-canvas surface-lightbox-contour-canvas" id="lbSurfaceContourCanvas" aria-label="Layer contours"></canvas>
        </div>
      </div>`;
    content.innerHTML = html;
    lb.classList.remove("is-hidden");

    // Prevent clicks inside the surface controls from closing the lightbox
    const wrap = content.querySelector(".surface-lightbox-wrap");
    if (wrap) wrap.addEventListener("click", (e) => e.stopPropagation());

    const canvas = app.state.ui.$("#lbSurfaceCanvas");
    const contourCanvas = app.state.ui.$("#lbSurfaceContourCanvas");
    const header = wrap?.querySelector(".comp-lightbox-topbar");
    const controls = wrap?.querySelector(".surface-lightbox-controls");
    const frame = wrap?.querySelector(".surface-lightbox-frame");
    const cleanups = [];
    const paletteVersion = app.commands.getRunDiagnosticPaletteVersion(run);

    // Scale canvas CSS to fill lightbox while preserving aspect ratio.
    // Called after first render (which sets canvas.width/height to data dims) and
    // measures the actual header/control strips instead of reserving a fixed budget.
    function scaleCanvasToFit() {
      if (!lifecycle.isActive()) return;
      const dataW = canvas.width, dataH = canvas.height;
      if (!dataW || !dataH) return;
      const ar = dataW / dataH;
      const maxW = Math.max(1, window.innerWidth - 48);
      const maxH = Math.max(
        1,
        window.innerHeight - 48
          - (header?.getBoundingClientRect().height || 0)
          - (controls?.getBoundingClientRect().height || 0),
      );
      let w, h;
      if (maxW / maxH > ar) {
        h = maxH; w = h * ar;
      } else {
        w = maxW; h = w / ar;
      }
      canvas.style.width = `${Math.floor(w)}px`;
      canvas.style.height = `${Math.floor(h)}px`;
      if (frame) {
        frame.style.width = canvas.style.width;
        frame.style.height = canvas.style.height;
      }
    }

    const onResize = () => {
      scaleCanvasToFit();
      // Highpass always shows this overlay. Explorer shows it only for the legacy/plain
      // fallback, so preserve whichever rendered state is currently visible.
      if (contourCanvas?.style.display !== "none") {
        app.commands.renderSurfaceContourOverlay(contourCanvas, cached.surface);
      }
    };
    window.addEventListener("resize", onResize);
    cleanups.push(() => window.removeEventListener("resize", onResize));
    if (typeof ResizeObserver !== "undefined" && header && controls) {
      const observer = new ResizeObserver(() => onResize());
      observer.observe(header);
      observer.observe(controls);
      cleanups.push(() => observer.disconnect());
    }

    if (viewType === "surface_highpass") {
      const slider = app.state.ui.$("#lbHighpassSlider");
      const valEl = app.state.ui.$("#lbHighpassValue");
      const hintEl = app.state.ui.$("#lbHighpassHint");

      function render() {
        const th = base + (parseInt(slider.value) * lh);
        const layers = parseInt(slider.value);
        valEl.textContent = `${th.toFixed(2)} mm`;
        hintEl.textContent = `(${layers} layers)`;
        app.commands.renderHighpass(canvas, cached.surface, tMax, th, paletteVersion);
        scaleCanvasToFit();
        app.commands.renderSurfaceContourOverlay(contourCanvas, cached.surface);
        // Sync inline slider
        const inline = app.state.ui.$("#highpassThresholdSlider");
        if (inline) inline.value = slider.value;
      }

      const onInput = () => render();
      const onWheel = (e) => {
        e.preventDefault();
        const dir = e.deltaY > 0 ? -1 : 1;
        slider.value = Math.max(0, Math.min(steps, parseInt(slider.value) + dir));
        render();
      };
      slider.addEventListener("input", onInput);
      slider.addEventListener("wheel", onWheel, { passive: false });
      cleanups.push(() => { slider.removeEventListener("input", onInput); slider.removeEventListener("wheel", onWheel); });
      render();

    } else {
      const hSlider = app.state.ui.$("#lbExplorerHeightSlider");
      const bSlider = app.state.ui.$("#lbExplorerBandSlider");
      const hVal = app.state.ui.$("#lbExplorerHeightValue");
      const hHint = app.state.ui.$("#lbExplorerHeightHint");
      const bVal = app.state.ui.$("#lbExplorerBandValue");
      const bHint = app.state.ui.$("#lbExplorerBandHint");

      async function render() {
        const center = base + (parseInt(hSlider.value) * lh);
        const halfBand = bSlider ? parseInt(bSlider.value) * lh : lh / 2;
        hVal.textContent = `${center.toFixed(2)} mm`;
        hHint.textContent = `(layer ${parseInt(hSlider.value)})`;
        if (bVal) bVal.textContent = `± ${halfBand.toFixed(2)} mm`;
        if (bHint && bSlider) bHint.textContent = `(${parseInt(bSlider.value)} layers)`;
        let renderedRich = false;
        // Always rich; plain renderer only as a fallback when material data is unavailable.
        const materialData = await app.commands.ensureExplorerMaterialData(run);
        if (!lifecycle.isActive()) return;
        if (materialData) {
          app.commands.renderExplorerRich(canvas, materialData, center, halfBand);
          renderedRich = true;
        } else {
          app.commands.renderExplorer(canvas, cached.surface, cached.ceiling, tMax, center, halfBand, paletteVersion);
        }
        scaleCanvasToFit();
        if (renderedRich) {
          if (contourCanvas) contourCanvas.style.display = "none";
        } else {
          app.commands.renderSurfaceContourOverlay(contourCanvas, cached.surface);
        }
        // Sync inline sliders
        const inH = app.state.ui.$("#explorerHeightSlider");
        const inB = app.state.ui.$("#explorerBandSlider");
        if (inH) inH.value = hSlider.value;
        if (inB && bSlider) inB.value = bSlider.value;
      }

      const onHInput = () => render();
      const onBInput = () => render();
      const onHWheel = (e) => {
        e.preventDefault();
        hSlider.value = Math.max(0, Math.min(steps, parseInt(hSlider.value) + (e.deltaY > 0 ? -1 : 1)));
        render();
      };
      const onBWheel = (e) => {
        e.preventDefault();
        const max = parseInt(bSlider.max);
        bSlider.value = Math.max(1, Math.min(max, parseInt(bSlider.value) + (e.deltaY > 0 ? -1 : 1)));
        render();
      };
      hSlider.addEventListener("input", onHInput);
      if (bSlider) bSlider.addEventListener("input", onBInput);
      hSlider.addEventListener("wheel", onHWheel, { passive: false });
      if (bSlider) bSlider.addEventListener("wheel", onBWheel, { passive: false });
      cleanups.push(() => {
        hSlider.removeEventListener("input", onHInput);
        if (bSlider) bSlider.removeEventListener("input", onBInput);
        hSlider.removeEventListener("wheel", onHWheel);
        if (bSlider) bSlider.removeEventListener("wheel", onBWheel);
      });
      render();
    }

    cleanups.forEach(dispose => lifecycle.addCleanup(dispose));
  }

  Object.assign(app.commands, {
    getSolveLightboxViewLabel,
    buildSolveRunPaletteChips,
    buildSolveLightboxHeader,
    getSelectedSolveRunsWithResults,
    getSolveWhiteCapThicknessItems,
    getSolveThicknessDisplayItems,
    getSolveThicknessItems,
    openThicknessLightboxForKey,
    solveRunById,
    openSolvePreviewLightboxForRun,
    openSolveCardLightboxFromElement,
    beginLightboxLifecycle,
    computeLightboxScaleBounds,
    normalizeStaticZoomWheelDelta,
    applyStaticZoomWheelDelta,
    buildStaticLightboxZoomControls,
    setupStaticLightboxZoom,
    openSolveRunLightbox,
    openSolveSourceLightbox,
    openSurfaceLightbox,
  });
}
