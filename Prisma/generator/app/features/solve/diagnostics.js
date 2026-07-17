/**
 * Install the solve/diagnostics feature commands.
 * @param {import("../../core/types.js").ApplicationContext} app
 */
export function installFeaturesSolveDiagnostics(app) {
async function ensureSurfaceData(run) {
    const id = run.id;
    if (app.state.ui.surfaceDataCache[id]) return app.state.ui.surfaceDataCache[id];
    const r = run.results;
    if (!r?.total_surface_bin_url || !r?.color_ceiling_bin_url) return null;
    const [surface, ceiling] = await Promise.all([
      app.commands.loadSurfaceBlob(r.total_surface_bin_url),
      app.commands.loadSurfaceBlob(r.color_ceiling_bin_url),
    ]);
    if (!surface || !ceiling) return null;
    app.state.ui.surfaceDataCache[id] = { surface, ceiling };
    return app.state.ui.surfaceDataCache[id];
  }

function rgbFromHex(hex, fallback = [128, 128, 128]) {
    if (typeof hex !== "string" || !/^#[0-9a-f]{6}$/i.test(hex)) return fallback;
    return [
      parseInt(hex.slice(1, 3), 16),
      parseInt(hex.slice(3, 5), 16),
      parseInt(hex.slice(5, 7), 16),
    ];
  }

function mixRgb(a, b, amount) {
    const t = Math.max(0, Math.min(1, amount));
    return [
      Math.round(a[0] * (1 - t) + b[0] * t),
      Math.round(a[1] * (1 - t) + b[1] * t),
      Math.round(a[2] * (1 - t) + b[2] * t),
    ];
  }

function getRunBaseFilamentId(run) {
    const snapshot = app.commands.getSolveRunSettingsSnapshot(run);
    return snapshot.base_filament || snapshot.white_base || app.state.session.DEFAULT_BASE_FILAMENT;
  }

function getRunCapFilamentId(run) {
    const snapshot = app.commands.getSolveRunSettingsSnapshot(run);
    const baseId = app.commands.getRunBaseFilamentId(run);
    const capId = snapshot.cap_filament || snapshot.white_cap || "__same__";
    return !capId || capId === "__same__" ? baseId : capId;
  }

async function ensureExplorerMaterialData(run) {
    if (!run?.results) return null;
    if (app.state.ui.explorerMaterialDataCache[run.id]) return app.state.ui.explorerMaterialDataCache[run.id];

    const r = run.results;
    if (!r.explorer_stack_label_bin_url || !Array.isArray(r.explorer_stack_table)) return null;
    const [surfaceData, stackLabels, capThickness] = await Promise.all([
      app.commands.ensureSurfaceData(run),
      app.commands.loadUint32Blob(r.explorer_stack_label_bin_url),
      app.commands.ensureCapThickness(run),
    ]);
    const [boundaryCapThickness, detailCapThickness] = await Promise.all([
      r.boundary_cap_height_bin_url ? app.commands.loadSurfaceBlob(r.boundary_cap_height_bin_url) : Promise.resolve(null),
      r.detail_cap_height_bin_url ? app.commands.loadSurfaceBlob(r.detail_cap_height_bin_url) : Promise.resolve(null),
    ]);
    if (!surfaceData || !stackLabels) return null;
    const { width, height } = surfaceData.surface;
    if (stackLabels.width !== width || stackLabels.height !== height) return null;
    const capData = capThickness?.width === width && capThickness?.height === height
      ? capThickness
      : null;
    const boundaryCapData = boundaryCapThickness?.width === width && boundaryCapThickness?.height === height
      ? boundaryCapThickness
      : null;
    const detailCapData = detailCapThickness?.width === width && detailCapThickness?.height === height
      ? detailCapThickness
      : null;

    const stackTable = r.explorer_stack_table.map((stack) => {
      if (!Array.isArray(stack)) return [];
      return stack.map((entry) => {
        const filamentId = String(entry?.filament_id || entry?.filamentId || "");
        const fil = app.commands.filamentById(filamentId);
        return {
          filamentId,
          thicknessMm: Math.max(0, Number(entry?.thickness_mm ?? entry?.thicknessMm ?? 0) || 0),
          rgb: app.commands.rgbFromHex(fil?.hex, [136, 136, 136]),
        };
      }).filter((entry) => entry.filamentId && entry.thicknessMm > 1e-9);
    });

    const baseId = r.explorer_base_filament_id || app.commands.getRunBaseFilamentId(run);
    const capId = r.explorer_cap_filament_id || app.commands.getRunCapFilamentId(run);
    const baseFil = app.commands.filamentById(baseId);
    const capFil = app.commands.filamentById(capId);
    const snapshot = app.commands.getSolveRunSettingsSnapshot(run);
    const baseThickness = Number(
      r.explorer_base_thickness_mm
        ?? snapshot.d_wb
        ?? app.commands.getSolveSurfaceBaseThickness()
    ) || app.commands.getSolveSurfaceBaseThickness();
    app.state.ui.explorerMaterialDataCache[run.id] = {
      surface: surfaceData.surface,
      ceiling: surfaceData.ceiling,
      cap: capData,
      boundaryCap: boundaryCapData,
      detailCap: detailCapData,
      stackLabels,
      stackTable,
      baseThickness,
      baseRgb: app.commands.rgbFromHex(baseFil?.hex, [245, 240, 224]),
      capRgb: app.commands.rgbFromHex(capFil?.hex, [245, 240, 224]),
      boundaryCapRgb: app.commands.mixRgb(app.commands.rgbFromHex(capFil?.hex, [245, 240, 224]), [128, 128, 128], 0.22),
    };
    return app.state.ui.explorerMaterialDataCache[run.id];
  }

function isSolveSurfaceView(view) {
    return view === "surface_highpass" || view === "surface_explorer";
  }

function getSolveSurfaceBaseThickness() {
    return parseFloat(app.state.settings.config.d_wb) || 0.20;
  }

function getSolveSurfaceLayerHeight() {
    return parseFloat(app.state.settings.config.layer_height) || 0.08;
  }

function getSolveSurfaceTMax() {
    return parseFloat(app.state.settings.config.t_max) || 3.0;
  }

function getSolveSurfaceExtraSteps() {
    const base = app.commands.getSolveSurfaceBaseThickness();
    const lh = app.commands.getSolveSurfaceLayerHeight();
    const tMax = app.commands.getSolveSurfaceTMax();
    return Math.max(0, Math.round((tMax - base) / lh));
  }

function legacyScalarPalette(t) {
    t = Math.max(0, Math.min(1, t));
    return [
      Math.floor(Math.max(0, Math.min(255, ( 0.267 + 2.173*t - 1.802*t*t) * 255))),
      Math.floor(Math.max(0, Math.min(255, (-0.004 + 1.874*t - 0.870*t*t) * 255))),
      Math.floor(Math.max(0, Math.min(255, ( 0.329 - 1.120*t + 0.791*t*t) * 255))),
    ];
  }

function inferno(t) {
    const position = Math.max(0, Math.min(1, Number(t) || 0)) * 255;
    const lower = Math.floor(position);
    const upper = Math.min(255, lower + 1);
    const fraction = position - lower;
    return app.state.ui.INFERNO_RGB8[lower].map((value, channel) => (
      Math.floor(value * (1 - fraction) + app.state.ui.INFERNO_RGB8[upper][channel] * fraction + 0.5)
    ));
  }

function getRunDiagnosticPaletteVersion(run) {
    return run?.results?.diagnostic_palette_version === app.state.ui.DIAGNOSTIC_PALETTE_INFERNO
      ? app.state.ui.DIAGNOSTIC_PALETTE_INFERNO
      : app.state.ui.DIAGNOSTIC_PALETTE_LEGACY;
  }

function sampleScalarPalette(t, paletteVersion = app.state.ui.DIAGNOSTIC_PALETTE_INFERNO) {
    return paletteVersion === app.state.ui.DIAGNOSTIC_PALETTE_INFERNO ? app.commands.inferno(t) : app.commands.legacyScalarPalette(t);
  }

function strokeLayerContourPaths(ctx, data, width, height, layerHeight, scale = app.state.ui.SURFACE_CONTOUR_SCALE) {
    if (!ctx || !data || width <= 0 || height <= 0 || !(layerHeight > 0)) return;
    const active = new Uint8Array(data.length);
    const levels = new Int32Array(data.length);
    for (let i = 0; i < data.length; i++) {
      const value = data[i];
      if (value > 1e-9) {
        active[i] = 1;
        levels[i] = Math.floor((value + 1e-6) / layerHeight);
      }
    }

    ctx.beginPath();
    for (let y = 0; y < height; y++) {
      const row = y * width;
      for (let x = 0; x < width - 1; x++) {
        const i = row + x;
        const j = i + 1;
        if (active[i] && active[j] && levels[i] !== levels[j]) {
          const px = (x + 1) * scale + 0.5;
          ctx.moveTo(px, y * scale);
          ctx.lineTo(px, (y + 1) * scale);
        }
      }
    }
    for (let y = 0; y < height - 1; y++) {
      const row = y * width;
      const nextRow = row + width;
      for (let x = 0; x < width; x++) {
        const i = row + x;
        const j = nextRow + x;
        if (active[i] && active[j] && levels[i] !== levels[j]) {
          const py = (y + 1) * scale + 0.5;
          ctx.moveTo(x * scale, py);
          ctx.lineTo((x + 1) * scale, py);
        }
      }
    }
    ctx.strokeStyle = app.state.ui.SOLVE_CONTOUR_STROKE;
    ctx.lineWidth = 1;
    ctx.stroke();
  }

function strokeDiscreteLabelBoundaries(ctx, boundaries, scale = app.state.ui.SURFACE_CONTOUR_SCALE) {
    if (!ctx || !boundaries) return;
    const { width, height, vertical, horizontal } = boundaries;
    if (width <= 0 || height <= 0 || !vertical || !horizontal) return;
    ctx.beginPath();
    for (let y = 0; y < height; y++) {
      const boundaryRow = y * Math.max(0, width - 1);
      for (let x = 0; x < width - 1; x++) {
        if (!vertical[boundaryRow + x]) continue;
        const px = (x + 1) * scale + 0.5;
        ctx.moveTo(px, y * scale);
        ctx.lineTo(px, (y + 1) * scale);
      }
    }
    for (let y = 0; y < height - 1; y++) {
      const row = y * width;
      for (let x = 0; x < width; x++) {
        if (!horizontal[row + x]) continue;
        const py = (y + 1) * scale + 0.5;
        ctx.moveTo(x * scale, py);
        ctx.lineTo((x + 1) * scale, py);
      }
    }
    ctx.strokeStyle = app.state.ui.SOLVE_CONTOUR_STROKE;
    ctx.lineWidth = 1;
    ctx.stroke();
  }

function renderHighpass(canvas, surfaceData, tMax, threshold, paletteVersion = app.state.ui.DIAGNOSTIC_PALETTE_INFERNO) {
    const { width, height, data } = surfaceData;
    canvas.width = width;
    canvas.height = height;
    const ctx = canvas.getContext("2d");
    const img = ctx.createImageData(width, height);
    const px = img.data;
    for (let i = 0; i < data.length; i++) {
      const v = data[i];
      const off = i * 4;
      if (v >= threshold) {
        const [r, g, b] = app.commands.sampleScalarPalette(v / tMax, paletteVersion);
        px[off] = r; px[off+1] = g; px[off+2] = b; px[off+3] = 255;
      } else {
        px[off] = 0; px[off+1] = 0; px[off+2] = 0; px[off+3] = 255;
      }
    }
    ctx.putImageData(img, 0, 0);
  }

function initHighpassControls() {
    const slider = app.state.ui.$("#highpassThresholdSlider");
    const valueEl = app.state.ui.$("#highpassThresholdValue");
    const hintEl = app.state.ui.$("#highpassLayerCount");
    if (!slider) return;

    function updateSliderRange() {
      const steps = app.commands.getSolveSurfaceExtraSteps();
      slider.min = 0;
      slider.max = steps;
      slider.value = steps;  // start at top
      slider.step = 1;
    }

    function getThreshold() {
      return app.commands.getSolveSurfaceBaseThickness() + (parseInt(slider.value) * app.commands.getSolveSurfaceLayerHeight());
    }

    function updateDisplay() {
      const th = getThreshold();
      const layers = parseInt(slider.value);
      valueEl.textContent = `${th.toFixed(2)} mm`;
      hintEl.textContent = `(${layers} layers)`;
    }

    updateSliderRange();
    updateDisplay();

    if (app.state.solve._solveHighpassControlsBound) return;
    app.state.solve._solveHighpassControlsBound = true;

    slider.addEventListener("input", () => {
      updateDisplay();
      app.commands.renderSolveSurfaceCanvases();
      app.commands.updateSolveLegend();
      app.commands.updateSolveViewCaption();
    });

    slider.addEventListener("wheel", (e) => {
      e.preventDefault();
      const dir = e.deltaY > 0 ? -1 : 1;
      slider.value = Math.max(
        parseInt(slider.min),
        Math.min(parseInt(slider.max), parseInt(slider.value) + dir)
      );
      updateDisplay();
      app.commands.renderSolveSurfaceCanvases();
      app.commands.updateSolveLegend();
      app.commands.updateSolveViewCaption();
    }, { passive: false });
  }

function renderExplorer(canvas, surfaceData, ceilingData, tMax, center, halfBand, paletteVersion = app.state.ui.DIAGNOSTIC_PALETTE_INFERNO) {
    const { width, height, data: surface } = surfaceData;
    const ceiling = ceilingData.data;
    canvas.width = width;
    canvas.height = height;
    const ctx = canvas.getContext("2d");
    const img = ctx.createImageData(width, height);
    const px = img.data;
    const lo = center - halfBand;
    const hi = center + halfBand;

    for (let i = 0; i < surface.length; i++) {
      const v = surface[i];
      const off = i * 4;
      if (v >= lo && v <= hi) {
        if (center <= ceiling[i]) {
          px[off] = app.state.ui.COLOR_FLOOR_FILL[0];
          px[off+1] = app.state.ui.COLOR_FLOOR_FILL[1];
          px[off+2] = app.state.ui.COLOR_FLOOR_FILL[2];
        } else {
          const [r, g, b] = app.commands.sampleScalarPalette(v / tMax, paletteVersion);
          px[off] = r; px[off+1] = g; px[off+2] = b;
        }
        px[off+3] = 255;
      } else {
        px[off] = 0; px[off+1] = 0; px[off+2] = 0; px[off+3] = 255;
      }
    }
    ctx.putImageData(img, 0, 0);
  }

function renderExplorerRich(canvas, materialData, center, halfBand) {
    const {
      surface,
      ceiling,
      cap,
      boundaryCap,
      detailCap,
      stackLabels,
      stackTable,
      baseThickness,
      baseRgb,
      capRgb,
      boundaryCapRgb,
    } = materialData;
    const { width, height, data: surfaceValues } = surface;
    const ceilingValues = ceiling.data;
    const capValues = cap?.data || null;
    const boundaryCapValues = boundaryCap?.data || null;
    const detailCapValues = detailCap?.data || null;
    const stackLabelValues = stackLabels.data;
    canvas.width = width;
    canvas.height = height;
    const ctx = canvas.getContext("2d");
    const img = ctx.createImageData(width, height);
    const px = img.data;
    const sampleHeight = Math.max(0, center - (app.commands.getSolveSurfaceLayerHeight() / 2));
    const eps = 1e-6;

    for (let i = 0; i < surfaceValues.length; i++) {
      const top = surfaceValues[i];
      const off = i * 4;
      if (sampleHeight > top + eps) {
        px[off] = 0; px[off+1] = 0; px[off+2] = 0; px[off+3] = 255;
        continue;
      }

      let rgb = baseRgb;
      const colorCeiling = ceilingValues[i];
      const capThickness = capValues ? Math.max(0, capValues[i] || 0) : 0;
      const capFloor = capValues
        ? Math.max(colorCeiling, top - capThickness)
        : colorCeiling;
      if (sampleHeight <= baseThickness + eps) {
        rgb = baseRgb;
      } else if (sampleHeight > capFloor + eps) {
        // Always distinguish boundary cap vs detail cap (two-tone) when the data is present.
        if (boundaryCapValues || detailCapValues) {
          const boundaryThickness = boundaryCapValues ? Math.max(0, boundaryCapValues[i] || 0) : 0;
          const detailThickness = detailCapValues ? Math.max(0, detailCapValues[i] || 0) : 0;
          const detailFloor = capFloor + boundaryThickness;
          rgb = detailThickness > eps && sampleHeight > detailFloor + eps
            ? capRgb
            : (boundaryCapRgb || capRgb);
        } else {
          rgb = capRgb;
        }
      } else {
        const localHeight = Math.max(0, sampleHeight - baseThickness);
        let cumulative = 0;
        let fallbackRgb = null;
        const stackId = stackLabelValues[i];
        const materials = stackTable[stackId] || [];
        for (const material of materials) {
          const d = material.thicknessMm;
          if (d <= eps) continue;
          fallbackRgb = material.rgb;
          if (localHeight <= cumulative + d + eps) {
            fallbackRgb = material.rgb;
            break;
          }
          cumulative += d;
        }
        rgb = fallbackRgb || app.state.ui.COLOR_FLOOR_FILL;
      }

      px[off] = rgb[0];
      px[off+1] = rgb[1];
      px[off+2] = rgb[2];
      px[off+3] = 255;
    }
    ctx.putImageData(img, 0, 0);
  }

function initExplorerControls() {
    const heightSlider = app.state.ui.$("#explorerHeightSlider");
    const bandSlider = app.state.ui.$("#explorerBandSlider");
    const heightValueEl = app.state.ui.$("#explorerHeightValue");
    const heightHintEl = app.state.ui.$("#explorerHeightLayers");
    const bandValueEl = app.state.ui.$("#explorerBandValue");
    const bandHintEl = app.state.ui.$("#explorerBandLayers");
    if (!heightSlider || !bandSlider) return;

    function updateSliderRanges() {
      const steps = app.commands.getSolveSurfaceExtraSteps();
      const prevHeight = Number.parseInt(heightSlider.value, 10);
      const prevBand = Number.parseInt(bandSlider.value, 10);
      const nextHeight = Number.isFinite(prevHeight) ? prevHeight : steps;
      const nextBand = Number.isFinite(prevBand) ? prevBand : Math.min(3, Math.floor(steps / 2));
      heightSlider.min = 0;
      heightSlider.max = steps;
      heightSlider.value = Math.max(0, Math.min(steps, nextHeight));
      heightSlider.step = 1;
      bandSlider.min = 1;
      bandSlider.max = Math.max(1, Math.floor(steps / 2));
      bandSlider.value = Math.max(1, Math.min(parseInt(bandSlider.max), nextBand));
      bandSlider.step = 1;
    }

    function getCenter() {
      return app.commands.getSolveSurfaceBaseThickness() + (parseInt(heightSlider.value) * app.commands.getSolveSurfaceLayerHeight());
    }

    function getHalfBand() {
      return parseInt(bandSlider.value) * app.commands.getSolveSurfaceLayerHeight();
    }

    function updateDisplay() {
      const center = getCenter();
      const halfBand = getHalfBand();
      heightValueEl.textContent = `${center.toFixed(2)} mm`;
      heightHintEl.textContent = `(layer ${parseInt(heightSlider.value)})`;
      bandValueEl.textContent = `± ${halfBand.toFixed(2)} mm`;
      bandHintEl.textContent = `(${parseInt(bandSlider.value)} layers)`;
    }

    updateSliderRanges();
    updateDisplay();

    if (app.state.solve._solveExplorerControlsBound) return;
    app.state.solve._solveExplorerControlsBound = true;

    heightSlider.addEventListener("input", () => {
      updateDisplay();
      app.commands.renderSolveSurfaceCanvases();
      app.commands.updateSolveLegend();
      app.commands.updateSolveViewCaption();
    });
    bandSlider.addEventListener("input", () => {
      updateDisplay();
      app.commands.renderSolveSurfaceCanvases();
      app.commands.updateSolveLegend();
      app.commands.updateSolveViewCaption();
    });

    heightSlider.addEventListener("wheel", (e) => {
      e.preventDefault();
      const dir = e.deltaY > 0 ? -1 : 1;
      heightSlider.value = Math.max(
        parseInt(heightSlider.min),
        Math.min(parseInt(heightSlider.max), parseInt(heightSlider.value) + dir)
      );
      updateDisplay();
      app.commands.renderSolveSurfaceCanvases();
      app.commands.updateSolveLegend();
      app.commands.updateSolveViewCaption();
    }, { passive: false });

    bandSlider.addEventListener("wheel", (e) => {
      e.preventDefault();
      const dir = e.deltaY > 0 ? -1 : 1;
      bandSlider.value = Math.max(
        parseInt(bandSlider.min),
        Math.min(parseInt(bandSlider.max), parseInt(bandSlider.value) + dir)
      );
      updateDisplay();
      app.commands.renderSolveSurfaceCanvases();
      app.commands.updateSolveLegend();
      app.commands.updateSolveViewCaption();
    }, { passive: false });
  }

function isBandedSolveRun(run) {
    const grouping = run?.results?.staged_metrics?.swap_grouping;
    return Array.isArray(grouping?.groups) && grouping.groups.length > 0;
  }

function hasRecipeViewerArtifacts(run) {
    const result = run?.results || {};
    return Boolean(
      result.predicted_color_only_appearance_url
      && result.color_recipe_breakdown_cookbook_url,
    );
  }

function shouldDefaultColorRegionsToRecipe() {
    const selected = app.commands.getSelectedRuns().filter(run => run.results);
    return selected.length > 0
      && selected.some(app.commands.isBandedSolveRun)
      && selected.every(app.commands.hasRecipeViewerArtifacts);
  }

function isSolveSourceColumnView(view) {
    return view !== "thickness_maps";
  }

function shouldShowSolveSourceColumn(view) {
    return app.commands.isSolveSourceColumnView(view) && app.state.solve.solveShowSourceImage;
  }

function buildSolveCardScaleBarSlot() {
    return `<div class="solve-card-scalebar" aria-hidden="true"></div>`;
  }

function buildSolveSourceColumn(run, aspect, view = app.state.solve.solveView) {
    const srcUrl = run.results.source_url || "";
    const targetKind = app.commands.getSolveSourceTargetKindForView(run, view);
    return `
      <div class="solve-grid-column is-source" data-solve-card-kind="source" data-run-id="${app.commands.esc(run.id)}" data-view="${app.commands.esc(view)}" data-source-target-kind="${app.commands.esc(targetKind)}">
        <div class="solve-grid-column-header">
          <h4>Source</h4>
          <div class="comparison-column-chips" aria-hidden="true" style="visibility:hidden"><span class="comparison-chip"></span></div>
          <div class="comparison-column-stats" aria-hidden="true" style="visibility:hidden">RMSE % 0.000</div>
        </div>
        <div class="solve-grid-img-wrapper" style="--img-aspect:${aspect}">
          <img class="solve-grid-img solve-grid-result-img" data-run-id="${app.commands.esc(run.id)}" src="${app.commands.esc(srcUrl)}" alt="Source image">
        </div>
        ${app.commands.buildSolveCardScaleBarSlot()}
      </div>`;
  }

function getSolveSourceTargetKindForView(run, view = app.state.solve.solveView) {
    if (app.commands.isSolveSurfaceView(view)) return "surface";
    const r = run?.results || {};
    const recipeReady = r.predicted_color_only_appearance_url
      && r.color_recipe_breakdown_cookbook_url;
    if (view === "color_ceiling" && app.state.solve.solveColorRegionsView === "recipe_regions" && recipeReady) {
      return "recipe";
    }
    return "run";
  }

function getSolveRunHeaderStats(result, view) {
    if (!result) return "";
    if (view === "color_ceiling") {
      const grouping = result.staged_metrics?.swap_grouping;
      if (Array.isArray(grouping?.groups) && grouping.groups.length > 0) {
        return `flat ${result.color_ceiling_max_d?.toFixed(2) || "0.00"} mm`;
      }
      return `max ${result.color_ceiling_max_d?.toFixed(2) || "0.00"} mm`;
    }
    if (view === "total_surface") {
      return `max ${result.total_surface_max_d?.toFixed(2) || "0.00"} mm`;
    }
    if (view === "cap_map") {
      // White Cap is a height map only; prefer surface-height stats, fall back for old runs.
      const value = result.cap_surface_height_max_d ?? result.total_surface_max_d ?? result.cap_map_max_d;
      return `max ${Number(value || 0).toFixed(2)} mm`;
    }
    if (view === "boundary_cap_map") {
      const value = result.boundary_cap_surface_height_max_d ?? result.boundary_cap_map_max_d;
      return `max ${Number(value || 0).toFixed(2)} mm`;
    }
    if (view === "detail_cap_map") {
      const value = result.detail_cap_surface_height_max_d ?? result.detail_cap_map_max_d;
      return `max ${Number(value || 0).toFixed(2)} mm`;
    }
    if (view === "recipe_regions") {
      // The color-only recipe card shows its recipe count, not a color RMSE.
      // (updateSolveColumnImages refreshes this from here, so it must be handled
      // explicitly or it falls through to the full-render RMSE.)
      const n = result.color_recipe_breakdown_summary?.color_recipe_count;
      return n != null ? `${n} recipe${n === 1 ? "" : "s"} · click to explore` : "click to explore";
    }
    return app.commands.formatColorRmse(result);
  }

function getSolveContourUrl(result, view) {
    if (!result || !app.commands.isSolveContourView(view)) return "";
    if (view === "recipe_regions") return "";
    if (view === "color_ceiling") {
      return result.color_ceiling_contour_bin_url || result.color_ceiling_bin_url || "";
    }
    if (view === "total_surface") {
      return result.total_surface_contour_bin_url || result.total_surface_bin_url || "";
    }
    // White Cap is a height map only; use surface-height contours, fall back to legacy for old runs.
    if (view === "cap_map") {
      return result.cap_surface_height_contour_bin_url || result.cap_map_contour_bin_url || "";
    }
    if (view === "boundary_cap_map") {
      return result.boundary_cap_surface_height_contour_bin_url || result.boundary_cap_contour_bin_url || "";
    }
    if (view === "detail_cap_map") {
      return result.detail_cap_surface_height_contour_bin_url || result.detail_cap_contour_bin_url || "";
    }
    return "";
  }

async function loadSolveContourData(url) {
    if (!url) return null;
    if (app.state.solve.solveContourDataCache[url]) return app.state.solve.solveContourDataCache[url];
    const blob = await app.commands.loadSurfaceBlob(url);
    if (!blob) return null;
    app.state.solve.solveContourDataCache[url] = blob;
    return blob;
  }

function drawSolveContourOverlay(canvas, blob) {
    if (!canvas || !blob) return;
    const { width, height, data } = blob;
    const contourScale = app.state.ui.SURFACE_CONTOUR_SCALE;
    canvas.width = width * contourScale;
    canvas.height = height * contourScale;
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, canvas.width, canvas.height);

    app.commands.strokeLayerContourPaths(ctx, data, width, height, app.commands.getSolveSurfaceLayerHeight(), contourScale);
    canvas.style.display = "block";
  }

function hasRecipeContourArtifacts(run) {
    const result = run?.results || {};
    return Boolean(
      result.explorer_stack_label_bin_url
      && Array.isArray(result.explorer_stack_table),
    );
  }

function drawRecipeBoundaryOverlay(canvas, boundaries) {
    if (!canvas || !boundaries) return;
    const { width, height } = boundaries;
    const contourScale = app.state.ui.SURFACE_CONTOUR_SCALE;
    canvas.width = width * contourScale;
    canvas.height = height * contourScale;
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    app.commands.strokeDiscreteLabelBoundaries(ctx, boundaries, contourScale);
    canvas.style.display = "block";
  }

function positionContourCanvasOverImage(canvas, blob) {
    if (!canvas || !blob) return false;
    const media = canvas.parentElement;
    const target = media?.querySelector(".solve-grid-result-img, .solve-grid-surface-canvas, .surface-lightbox-canvas, .comp-lightbox-img");
    if (!media || !target) return false;

    const boxW = target.clientWidth || target.getBoundingClientRect().width;
    const boxH = target.clientHeight || target.getBoundingClientRect().height;
    if (!boxW || !boxH) return false;

    const imgAspect = blob.width / Math.max(blob.height, 1);
    const boxAspect = boxW / Math.max(boxH, 1);
    let drawW = boxW;
    let drawH = boxH;
    let insetX = 0;
    let insetY = 0;
    if (boxAspect > imgAspect) {
      drawH = boxH;
      drawW = boxH * imgAspect;
      insetX = (boxW - drawW) / 2;
    } else if (boxAspect < imgAspect) {
      drawW = boxW;
      drawH = boxW / imgAspect;
      insetY = (boxH - drawH) / 2;
    }

    canvas.style.left = `${target.offsetLeft + insetX}px`;
    canvas.style.top = `${target.offsetTop + insetY}px`;
    canvas.style.width = `${drawW}px`;
    canvas.style.height = `${drawH}px`;
    return true;
  }

function renderSurfaceContourOverlay(canvas, surfaceBlob) {
    if (!canvas || !surfaceBlob) {
      if (canvas) canvas.style.display = "none";
      return;
    }
    app.commands.drawSolveContourOverlay(canvas, surfaceBlob);
    if (!app.commands.positionContourCanvasOverImage(canvas, surfaceBlob)) {
      canvas.style.display = "none";
    }
  }

function renderSolveContourCanvasForRun(canvas, run, view) {
    if (!canvas || !run?.results || !app.state.solve.solveContoursEnabled || !app.commands.isSolveContourView(view)) {
      if (canvas) canvas.style.display = "none";
      return;
    }
    const runId = run.id;
    if (view === "recipe_regions") {
      const sourceKey = `recipe:${runId}`;
      canvas.dataset.runId = runId;
      canvas.dataset.contourSource = sourceKey;
      app.commands.ensureRecipeArtifactData(run).then((recipeData) => {
        const boundaries = recipeData?.recipeBoundaries;
        if (
          !boundaries
          || !canvas.isConnected
          || canvas.dataset.runId !== runId
          || canvas.dataset.contourSource !== sourceKey
          || !app.state.solve.solveContoursEnabled
        ) {
          canvas.style.display = "none";
          return;
        }
        app.commands.drawRecipeBoundaryOverlay(canvas, boundaries);
        if (!app.commands.positionContourCanvasOverImage(canvas, boundaries)) {
          canvas.style.display = "none";
          return;
        }
        const img = canvas.parentElement?.querySelector(".solve-grid-result-img, .comp-lightbox-img");
        if (img && !img.complete) {
          img.addEventListener("load", () => {
            if (canvas.dataset.contourSource === sourceKey) {
              app.commands.positionContourCanvasOverImage(canvas, boundaries);
            }
          }, { once: true });
        }
      });
      return;
    }
    const url = app.commands.getSolveContourUrl(run.results, view);
    if (!url) {
      canvas.style.display = "none";
      return;
    }
    canvas.dataset.runId = runId;
    canvas.dataset.contourSource = url;
    app.commands.loadSolveContourData(url).then(blob => {
      if (!blob || !canvas.isConnected || canvas.dataset.runId !== runId || canvas.dataset.contourSource !== url || !app.state.solve.solveContoursEnabled || !app.commands.isSolveContourView(view)) {
        canvas.style.display = "none";
        return;
      }
      app.commands.drawSolveContourOverlay(canvas, blob);
      if (!app.commands.positionContourCanvasOverImage(canvas, blob)) {
        canvas.style.display = "none";
        return;
      }
      const img = canvas.parentElement?.querySelector(".solve-grid-result-img, .comp-lightbox-img");
      if (img && !img.complete) {
        img.addEventListener("load", () => {
          if (canvas.dataset.contourSource === url) app.commands.positionContourCanvasOverImage(canvas, blob);
        }, { once: true });
      }
    });
  }

function renderSolveContourCanvases() {
    document.querySelectorAll(".solve-grid-contour-canvas[data-run-id]").forEach(canvas => {
      const run = app.state.solve.solveRuns.find(r => r.id === canvas.dataset.runId);
      // The contour overlay belongs to the card it sits in, not the global view.
      const colView = canvas.closest(".solve-grid-column")?.dataset.view || app.state.solve.solveView;
      app.commands.renderSolveContourCanvasForRun(canvas, run, colView);
    });
  }

function renderSolveLightboxContours(run, view) {
    const canvas = document.querySelector(".solve-lightbox-contour-canvas");
    app.commands.renderSolveContourCanvasForRun(canvas, run, view);
  }

function syncRecipeLightboxContoursToggle() {
    const btn = document.getElementById("recipeLightboxContoursToggle");
    if (!btn) return;
    const available = btn.dataset.contoursAvailable === "true";
    btn.disabled = !available;
    btn.classList.toggle("is-active", available && app.state.solve.solveContoursEnabled);
    btn.setAttribute("aria-pressed", available && app.state.solve.solveContoursEnabled ? "true" : "false");
  }

function refreshVisibleSolveContours() {
    if (app.commands.isSolveSurfaceView(app.state.solve.solveView)) app.commands.renderSolveSurfaceCanvases();
    if (app.commands.isSolveContourView(app.state.solve.solveView)) app.commands.renderSolveContourCanvases();
    if (app.state.solve._solveLightboxState?.kind === "solve") {
      const run = app.state.solve.solveRuns.find(r => r.id === app.state.solve._solveLightboxState.runId);
      if (run) app.commands.renderSolveLightboxContours(run, app.state.solve._solveLightboxState.view ?? app.state.solve.solveView);
    } else if (app.state.solve._solveLightboxState?.kind === "recipe") {
      const run = app.state.solve.solveRuns.find(r => r.id === app.state.solve._solveLightboxState.runId);
      if (run) app.commands.renderSolveLightboxContours(run, "recipe_regions");
    } else if (app.state.solve._solveLightboxState?.kind === "surface") {
      const run = app.state.solve.solveRuns.find(r => r.id === app.state.solve._solveLightboxState.runId);
      const canvas = app.state.ui.$("#lbSurfaceContourCanvas");
      if (run && canvas) {
        if (app.state.solve.solveView === "surface_explorer") {
          // Explorer is always rich; rich shows material identity, not a contour overlay.
          canvas.style.display = "none";
        } else {
          app.commands.ensureSurfaceData(run).then(cached => {
            if (cached) app.commands.renderSurfaceContourOverlay(canvas, cached.surface);
          });
        }
      }
    }
  }

function buildSolveRunColumn(run, aspect) {
    return app.commands.buildSolveRunVisualColumn(run, aspect, app.state.solve.solveView);
  }

function buildSolveRecipeColumn(run, aspect) {
    const view = "recipe_regions";
    const chips = (run.palette || []).map(fid => {
      const fil = app.state.session.allFilaments.find(f => f.filament_id === fid);
      const hex = fil?.hex || "#888";
      return `<span class="comparison-chip" style="background:${hex}"></span>`;
    }).join("");
    const summary = run.results?.color_recipe_breakdown_summary;
    const recipeCount = summary?.color_recipe_count;
    const statsLine = recipeCount != null
      ? `${recipeCount} recipe${recipeCount === 1 ? "" : "s"} · click to explore`
      : "Recipe viewer · click to explore";
    const contour = app.commands.isSolveContourView(view)
      ? `<canvas class="solve-grid-contour-canvas" data-run-id="${app.commands.escAttr(run.id)}" aria-label="${app.commands.escAttr(run.label)} recipe boundaries"></canvas>`
      : "";
    return `
      <div class="solve-grid-column" data-solve-card-kind="recipe" data-run-id="${app.commands.esc(run.id)}" data-view="${app.commands.esc(view)}">
        <div class="solve-grid-column-header">
          <h4>${app.commands.esc(run.label)} · Recipes</h4>
          <div class="comparison-column-chips">${chips}</div>
          <div class="comparison-column-stats">${app.commands.esc(statsLine)}</div>
        </div>
        <div class="solve-grid-img-wrapper solve-grid-overlay-container" style="--img-aspect:${aspect}">
          <img class="solve-grid-img solve-grid-result-img" data-run-id="${app.commands.esc(run.id)}" alt="${app.commands.esc(run.label)} color-only render">
          ${contour}
        </div>
        ${app.commands.buildSolveCardScaleBarSlot()}
        ${app.commands.buildSolveRunCardMetadataFooter(run)}
      </div>`;
  }

function buildSolveRunVisualColumn(run, aspect, view, opts = {}) {
    const r = run.results;
    const chips = (run.palette || []).map(fid => {
      const fil = app.state.session.allFilaments.find(f => f.filament_id === fid);
      const hex = fil?.hex || "#888";
      return `<span class="comparison-chip" style="background:${hex}"></span>`;
    }).join("");

    const statsLine = app.commands.getSolveRunHeaderStats(r, view);
    const visual = app.commands.isSolveSurfaceView(view)
      ? `<canvas class="solve-grid-img solve-grid-surface-canvas" data-run-id="${app.commands.esc(run.id)}" data-view="${app.commands.esc(view)}" aria-label="${app.commands.esc(run.label)}"></canvas>
          <canvas class="solve-grid-contour-canvas solve-grid-surface-contour-canvas" data-run-id="${app.commands.esc(run.id)}" aria-label="${app.commands.esc(run.label)} layer contours"></canvas>`
      : `<img class="solve-grid-img solve-grid-result-img" data-run-id="${app.commands.esc(run.id)}" alt="${app.commands.esc(run.label)}">
          ${app.commands.isSolveContourView(view) ? `<canvas class="solve-grid-contour-canvas" data-run-id="${app.commands.esc(run.id)}" aria-label="${app.commands.esc(run.label)} layer contours"></canvas>` : ""}`;
    const wrapperClass = "solve-grid-img-wrapper solve-grid-overlay-container";

    // Card metadata for the unified click dispatcher. Normal views resolve to a "run" or
    // "surface" card; diff-view callers override to a "thickness" card with an explicit
    // lightbox target (the diff run columns open a plain thickness lightbox, not the rich one).
    const cardKind = opts.cardKind || (app.commands.isSolveSurfaceView(view) ? "surface" : "run");
    const thicknessData = opts.lightboxUrl
      ? ` data-thickness-url="${app.commands.esc(opts.lightboxUrl)}" data-thickness-label="${app.commands.esc(opts.lightboxLabel || "")}"`
      : "";

    return `
      <div class="solve-grid-column" data-solve-card-kind="${app.commands.esc(cardKind)}" data-run-id="${app.commands.esc(run.id)}" data-view="${app.commands.esc(view)}"${thicknessData}>
        <div class="solve-grid-column-header">
          <h4>${app.commands.esc(run.label)}</h4>
          <div class="comparison-column-chips">${chips}</div>
          <div class="comparison-column-stats">${statsLine}</div>
        </div>
        <div class="${wrapperClass}" style="--img-aspect:${aspect}">
          ${visual}
        </div>
        ${app.commands.buildSolveCardScaleBarSlot()}
        ${app.commands.buildSolveRunCardMetadataFooter(run)}
      </div>`;
  }

function getSolveHighpassThreshold() {
    return app.commands.getSolveSurfaceBaseThickness() + (parseInt(app.state.ui.$("#highpassThresholdSlider")?.value || "0") * app.commands.getSolveSurfaceLayerHeight());
  }

function getSolveExplorerCenter() {
    return app.commands.getSolveSurfaceBaseThickness() + (parseInt(app.state.ui.$("#explorerHeightSlider")?.value || "0") * app.commands.getSolveSurfaceLayerHeight());
  }

function getSolveExplorerHalfBand() {
    return parseInt(app.state.ui.$("#explorerBandSlider")?.value || "1") * app.commands.getSolveSurfaceLayerHeight();
  }

function renderSolveSurfaceCanvases() {
    const view = app.state.solve.solveView;
    if (!app.commands.isSolveSurfaceView(view)) return;
    const tMax = parseFloat(app.state.settings.config.t_max) || 3.0;
    const threshold = app.commands.getSolveHighpassThreshold();
    const center = app.commands.getSolveExplorerCenter();
    const halfBand = app.commands.getSolveExplorerHalfBand();
    document.querySelectorAll(".solve-grid-surface-canvas[data-run-id]").forEach((canvas) => {
      const runId = canvas.dataset.runId;
      const run = app.state.solve.solveRuns.find(r => r.id === runId);
      if (!run) return;
      const contourCanvas = canvas.parentElement?.querySelector(".solve-grid-surface-contour-canvas");
      app.commands.ensureSurfaceData(run).then(cached => {
        // Bail if the user switched away from this surface view during the async load.
        if (app.state.solve.solveView !== view) return;
        if (!cached) {
          if (contourCanvas) contourCanvas.style.display = "none";
          return;
        }
        if (view === "surface_highpass") {
          app.commands.renderHighpass(canvas, cached.surface, tMax, threshold, app.commands.getRunDiagnosticPaletteVersion(run));
        } else {
          // Explorer is always rich; fall back to the plain renderer only if material data is
          // missing (an error/loading state, not a user-selectable mode).
          app.commands.ensureExplorerMaterialData(run).then(materialData => {
            if (app.state.solve.solveView !== view) return;
            if (materialData) {
              app.commands.renderExplorerRich(canvas, materialData, center, halfBand);
              if (contourCanvas) contourCanvas.style.display = "none";
            } else {
              app.commands.renderExplorer(canvas, cached.surface, cached.ceiling, tMax, center, halfBand, app.commands.getRunDiagnosticPaletteVersion(run));
              app.commands.renderSurfaceContourOverlay(contourCanvas, cached.surface);
            }
          });
          return;
        }
        app.commands.renderSurfaceContourOverlay(contourCanvas, cached.surface);
      });
    });
  }

function updateSolveColumnImages() {
    const view = app.state.solve.solveView;
    if (app.commands.isSolveSurfaceView(view)) {
      app.commands.renderSolveSurfaceCanvases();
      document.querySelectorAll(".solve-grid-column[data-run-id]:not(.is-source)").forEach(col => {
        const runId = col.dataset.runId;
        const run = app.state.solve.solveRuns.find(r => r.id === runId);
        if (!run || !run.results) return;
        const statsEl = col.querySelector(".comparison-column-stats");
        if (statsEl) statsEl.textContent = app.commands.getSolveRunHeaderStats(run.results, view);
      });
      return;
    }
    document.querySelectorAll(".solve-grid-column[data-run-id]:not(.is-source)").forEach(col => {
      const runId = col.dataset.runId;
      const run = app.state.solve.solveRuns.find(r => r.id === runId);
      if (!run || !run.results) return;
      // Cards carry their own data-view, so multi-card views (Color Regions:
      // recipe card + ceiling card) resolve each card's image independently.
      const colView = col.dataset.view || view;
      const img = col.querySelector(".solve-grid-result-img");
      const contourCanvas = col.querySelector(".solve-grid-contour-canvas");
      if (img) {
        img.src = app.commands._getSolveRunResultUrl(run.results, colView) || "";
        img.style.opacity = "";
      }
      if (contourCanvas) contourCanvas.style.display = "none";
      const statsEl = col.querySelector(".comparison-column-stats");
      if (statsEl) statsEl.textContent = app.commands.getSolveRunHeaderStats(run.results, colView);
    });
    if (app.commands.isSolveContourView(view)) app.commands.renderSolveContourCanvases();
  }

function isWhiteCapMapView(view) {
    return view === "cap_map" || view === "boundary_cap_map" || view === "detail_cap_map";
  }

function isSolveWhiteCapView(view) {
    return app.commands.isWhiteCapMapView(view);
  }

function isSolveContourView(view) {
    return app.commands.isSolveWhiteCapView(view) || view === "color_ceiling" || view === "total_surface" || view === "recipe_regions";
  }

function getSolveTopLevelView(view) {
    if (app.commands.isSolveWhiteCapView(view)) return "white_cap";
    return view;
  }

function isSolveDevViewsEnabled() {
    try {
      return new URLSearchParams(window.location.search).get("dev") === "1";
    } catch (_e) {
      return false;
    }
  }

function coerceSolveViewForAccess() {
    if (app.state.ui.SOLVE_REMOVED_VIEWS.has(app.state.solve.solveView)) {
      app.state.solve.solveView = "predicted";
    } else if (app.commands.isSolveThicknessDiffView(app.state.solve.solveView) && !app.commands.isSolveDevViewsEnabled()) {
      app.state.solve.solveView = "predicted";
    }
  }

function setSolveAdvancedViewsOpen(next) {
    app.state.solve.solveAdvancedViewsOpen = !!next;
    const group = app.state.ui.$("#solveAdvancedViews");
    const toggle = app.state.ui.$("#solveAdvancedToggle");
    if (group) group.hidden = !app.state.solve.solveAdvancedViewsOpen;
    if (toggle) {
      toggle.setAttribute("aria-expanded", app.state.solve.solveAdvancedViewsOpen ? "true" : "false");
      toggle.classList.toggle("is-open", app.state.solve.solveAdvancedViewsOpen);
    }
  }

function syncSolveViewToggleActive() {
    const activeView = app.commands.getSolveTopLevelView(app.state.solve.solveView);
    // Never hide the active view's own button: if an advanced view is active, force the group open.
    if (app.state.ui.SOLVE_ADVANCED_VIEWS.has(activeView) && !app.state.solve.solveAdvancedViewsOpen) {
      app.commands.setSolveAdvancedViewsOpen(true);
    }
    app.state.ui.$$("#solveViewBar .view-toggle-btn").forEach((btn) => {
      btn.classList.toggle("is-active", btn.dataset.view === activeView);
    });
  }

function _getSolveRunResultUrl(result, view) {
    switch (view) {
      case "predicted":
        // Appearance = F(predicted), baked server-side; strict, no fallback —
        // a broken image is the loud signal that the bake is missing.
        return result.predicted_appearance_url;
      // White Cap is a height map only; fall back to legacy thickness URLs for old runs.
      case "cap_map":
        return result.cap_surface_height_url || result.cap_map_url;
      case "boundary_cap_map":
        return result.boundary_cap_surface_height_url || result.boundary_cap_map_url || result.cap_surface_height_url || result.cap_map_url;
      case "detail_cap_map":
        return result.detail_cap_surface_height_url || result.detail_cap_map_url || result.cap_surface_height_url || result.cap_map_url;
      case "cap_diff":       return result.total_surface_url || result.predicted_url;
      case "color_ceiling": return result.color_ceiling_url;
      case "recipe_regions":
        // Color-only predicted appearance (white cap omitted), baked through F.
        return result.predicted_color_only_appearance_url || result.predicted_appearance_url;
      case "total_surface":  return result.total_surface_url;
      default:               return result.predicted_url;
    }
  }

function updateSolveSubControls() {
    const view = app.state.solve.solveView;
    const highpassCtrl = app.state.ui.$("#solveHighpassControls");
    const explorerCtrl = app.state.ui.$("#solveExplorerControls");
    const capDiffCtrl = app.state.ui.$("#solveCapDiffControls");
    const filamentDiffCtrl = app.state.ui.$("#solveFilamentDiffControls");
    const whiteCapCtrl = app.state.ui.$("#solveWhiteCapControls");
    const contourCtrl = app.state.ui.$("#solveContourControls");
    const sourceCtrl = app.state.ui.$("#solveSourceControls");
    const sourceToggle = app.state.ui.$("#solveSourceImageToggle");
    const contoursToggle = app.state.ui.$("#solveContoursToggle");
    app.commands.syncSolveViewToggleActive();
    document.querySelectorAll("[data-solve-white-cap-view]").forEach(btn => {
      const active = btn.dataset.solveWhiteCapView === app.state.solve.solveView;
      btn.classList.toggle("is-active", active);
      btn.setAttribute("aria-checked", active ? "true" : "false");
    });
    document.querySelectorAll("[data-cap-diff-mode]").forEach(btn => {
      const active = btn.dataset.capDiffMode === app.state.solve.solveCapDiffMode;
      btn.classList.toggle("is-active", active);
      btn.setAttribute("aria-checked", active ? "true" : "false");
    });
    // Explorer is always rich, which samples a single center layer (ignores band), so the band
    // controls stay hidden for Explorer — same as the old rich mode.
    document.querySelectorAll(".explorer-band-control").forEach(el => {
      el.classList.toggle("is-hidden", view === "surface_explorer");
    });
    if (contoursToggle) {
      const recipeBoundaryMode = view === "color_ceiling" && app.state.solve.solveColorRegionsView === "recipe_regions";
      contoursToggle.classList.toggle("is-active", app.state.solve.solveContoursEnabled);
      contoursToggle.setAttribute("aria-pressed", app.state.solve.solveContoursEnabled ? "true" : "false");
      contoursToggle.textContent = `Contours: ${app.state.solve.solveContoursEnabled ? "On" : "Off"}`;
      contoursToggle.title = recipeBoundaryMode
        ? "Show or hide recipe boundaries"
        : "Show or hide layer-height contour lines";
    }
    if (sourceCtrl) sourceCtrl.classList.toggle("is-hidden", !app.commands.isSolveSourceColumnView(view));
    if (sourceToggle) {
      sourceToggle.classList.toggle("is-active", app.state.solve.solveShowSourceImage);
      sourceToggle.setAttribute("aria-pressed", app.state.solve.solveShowSourceImage ? "true" : "false");
      sourceToggle.textContent = `Source Image: ${app.state.solve.solveShowSourceImage ? "On" : "Off"}`;
    }
    const colorRegionsCtrl = app.state.ui.$("#solveColorRegionsControls");
    if (colorRegionsCtrl) colorRegionsCtrl.classList.toggle("is-hidden", view !== "color_ceiling");
    document.querySelectorAll("[data-solve-color-regions-view]").forEach(btn => {
      const active = btn.dataset.solveColorRegionsView === app.state.solve.solveColorRegionsView;
      btn.classList.toggle("is-active", active);
      btn.setAttribute("aria-checked", active ? "true" : "false");
    });
    if (whiteCapCtrl) whiteCapCtrl.classList.toggle("is-hidden", !app.commands.isSolveWhiteCapView(view));
    if (contourCtrl) contourCtrl.classList.toggle("is-hidden", !app.commands.isSolveContourView(view));
    if (highpassCtrl) highpassCtrl.classList.toggle("is-hidden", view !== "surface_highpass");
    if (explorerCtrl) explorerCtrl.classList.toggle("is-hidden", view !== "surface_explorer");
    if (capDiffCtrl) capDiffCtrl.classList.toggle("is-hidden", !app.commands.isSolveThicknessDiffView(view));
    if (filamentDiffCtrl) {
      filamentDiffCtrl.classList.toggle("is-hidden", !app.commands.isSolveFilamentDiffView(view));
      if (app.commands.isSolveFilamentDiffView(view)) app.commands.syncSolveFilamentDiffControl();
    }
    if (view === "surface_highpass") app.commands.initHighpassControls();
    if (view === "surface_explorer") app.commands.initExplorerControls();
    app.commands.updateSolveViewCaption(view);
  }

function getSolveViewCaption(view = app.state.solve.solveView) {
    if (view === "predicted") {
      return {
        title: "Predicted Appearance",
        body: "Shows how each selected run is expected to look when printed and lit.",
      };
    }
    if (view === "cap_map") {
      return {
        title: "Total White Cap",
        body: "Shows the full white cap surface added above the color layers.",
      };
    }
    if (view === "boundary_cap_map") {
      return {
        title: "Boundary Cap",
        body: "Shows the smooth white cap coverage that sits over the color layers.",
      };
    }
    if (view === "detail_cap_map") {
      return {
        title: "Detail Cap",
        body: "Shows the extra white cap material used to bring back image detail.",
      };
    }
    if (view === "color_ceiling") {
      if (app.state.solve.solveColorRegionsView === "recipe_regions") {
        return {
          title: "Recipe Viewer",
          body: "Shows the color-only preview and the recipes used in each color region.",
        };
      }
      return {
        title: "Color Regions",
        body: "Shows the combined height of the colored material and base before the white cap is added.",
      };
    }
    if (view === "total_surface") {
      return {
        title: "Top Surface",
        body: "Shows the final top surface after color layers and white cap are combined.",
      };
    }
    if (view === "surface_explorer") {
      const height = app.commands.getSolveExplorerCenter();
      return {
        title: "Surface Explorer",
        body: `Shows which material appears at ${height.toFixed(2)} mm from the base.`,
      };
    }
    if (view === "thickness_maps") {
      return {
        title: "Thickness Maps",
        body: "Shows how much of each filament is used, including total, boundary, and detail white-cap maps.",
      };
    }
    if (view === "surface_highpass") {
      const threshold = app.commands.getSolveHighpassThreshold();
      return {
        title: "Surface Highpass",
        body: `Shows only areas where the surface is at least ${threshold.toFixed(2)} mm high.`,
      };
    }
    if (view === "cap_diff") {
      return {
        title: "Cap Diff",
        body: `Compares the white cap between two selected runs using ${app.state.solve.solveCapDiffMode} mode.`,
      };
    }
    if (view === "filament_diff") {
      const filamentId = app.commands.ensureSolveFilamentDiffSelection();
      const fil = app.commands.filamentById(filamentId);
      return {
        title: "Color Diff",
        body: `Compares where ${fil?.color_name || "the selected filament"} is used in two selected runs.`,
      };
    }
    return {
      title: "Preview",
      body: "Shows the selected solve preview.",
    };
  }

function updateSolveViewCaption(view = app.state.solve.solveView) {
    const el = app.state.ui.$("#solveViewCaption");
    if (!el) return;
    const caption = app.commands.getSolveViewCaption(view);
    el.innerHTML = `
      <span class="solve-view-caption-title">${app.commands.esc(caption.title)}</span>
      <span class="solve-view-caption-body">${app.commands.esc(caption.body)}</span>`;
    el.classList.remove("is-hidden");
  }

function getDiagnosticPaletteLegendState(runs) {
    const versions = new Set((runs || []).map(app.commands.getRunDiagnosticPaletteVersion));
    const mixed = versions.size > 1;
    const version = mixed ? null : (versions.values().next().value || app.state.ui.DIAGNOSTIC_PALETTE_INFERNO);
    return {
      mixed,
      version,
      gradient: version === app.state.ui.DIAGNOSTIC_PALETTE_LEGACY
        ? "linear-gradient(to right, #440053, #b16819, #e6b600, #e1e800, #a2ff00)"
        : "linear-gradient(to right, #000004, #420a68, #932667, #dd513a, #fca50a, #fcffa4)",
    };
  }

function diagnosticPaletteLegendHtml(state) {
    if (state.mixed) {
      return `<div class="solve-palette-warning" role="note">Mixed diagnostic palettes. Rerun the legacy solve for direct color comparison.</div>`;
    }
    return `<div class="legend-bar" data-diagnostic-palette="${app.commands.esc(state.version)}" style="background:${state.gradient}"></div>`;
  }

function updateSolveLegend(view = app.state.solve.solveView) {
    const row = app.state.ui.$("#solveLegend");
    const el = app.state.ui.$("#solveLegendContent");
    if (!row || !el) return;
    const selected = app.commands.getSelectedRuns().filter(r => r.results);
    const result = selected[0]?.results;
    const paletteLegend = app.commands.getDiagnosticPaletteLegendState(selected);
    const contourLegendHtml = (label = "Layer-height contour") => `
      <div class="solve-contour-legend">
        <span class="solve-contour-legend-line"></span>
        <span>${label}</span>
      </div>`;

    if (view === "thickness_maps") {
      // Per-filament maps self-normalize to their own max (no shared scale yet — see TM-SCALE).
      el.innerHTML = `
        <div class="sub-legend-block">
          <span class="sub-legend-desc">Per-filament thickness maps (each self-normalized to its own max) &middot; White cap: Total, Boundary, and Detail</span>
          ${app.commands.diagnosticPaletteLegendHtml(paletteLegend)}
        </div>`;
      row.classList.remove("is-hidden");
      return;
    }

    if (view === "color_ceiling" && app.state.solve.solveColorRegionsView === "recipe_regions") {
      const boundaryAvailable = selected.some(app.commands.hasRecipeContourArtifacts);
      const contourLegend = app.state.solve.solveContoursEnabled && boundaryAvailable
        ? contourLegendHtml("Recipe boundary")
        : "";
      el.innerHTML = `
        <div class="sub-legend-block">
          <span class="sub-legend-desc">Recipe Viewer (color-only appearance)</span>
          ${contourLegend ? `<div class="solve-legend-inline">${contourLegend}</div>` : ""}
        </div>`;
      row.classList.remove("is-hidden");
      return;
    }

    if (view === "cap_map" || view === "boundary_cap_map" || view === "detail_cap_map") {
      // White Cap is a height map only — absolute surface height on the shared t_max scale.
      const scaleMax = app.commands.getSolveSurfaceTMax();
      const valueKind = "absolute surface height";
      const label = view === "boundary_cap_map"
        ? `Boundary Cap (${valueKind}, shared absolute scale)`
        : view === "detail_cap_map"
          ? `Detail Cap (${valueKind}, shared absolute scale)`
          : `Total White Cap (${valueKind}, shared absolute scale)`;
      const zeroLabel = view === "boundary_cap_map"
        ? "No boundary cap here"
        : view === "detail_cap_map"
          ? "No detail cap here"
          : "No white cap here";
      const contourLegend = app.state.solve.solveContoursEnabled
        ? contourLegendHtml()
        : "";
      el.innerHTML = `
        <div class="sub-legend-block">
          <span class="sub-legend-desc">${label}</span>
          ${app.commands.diagnosticPaletteLegendHtml(paletteLegend)}
          <div class="legend-labels"><span>0 mm</span><span>${(scaleMax / 2).toFixed(1)} mm</span><span>${scaleMax.toFixed(1)} mm</span></div>
          <div class="solve-legend-inline">
            <span><span class="solve-legend-swatch is-empty"></span> ${zeroLabel}</span>
            ${contourLegend}
          </div>
        </div>`;
      row.classList.remove("is-hidden");
    } else if (view === "color_ceiling" || view === "total_surface") {
      const tMax = app.state.settings.config.t_max || 3.0;
      const label = view === "color_ceiling" ? "Color Ceiling (base + colors)" : "Top Surface (base + colors + cap)";
      const contourLegend = app.commands.isSolveContourView(view) && app.state.solve.solveContoursEnabled
        ? contourLegendHtml()
        : "";
      el.innerHTML = `
        <div class="sub-legend-block">
          <span class="sub-legend-desc">${label}</span>
          ${app.commands.diagnosticPaletteLegendHtml(paletteLegend)}
          <div class="legend-labels"><span>0 mm</span><span>${(tMax/2).toFixed(1)} mm</span><span>${tMax.toFixed(1)} mm</span></div>
          ${contourLegend ? `<div class="solve-legend-inline">${contourLegend}</div>` : ""}
        </div>`;
      row.classList.remove("is-hidden");
    } else if (view === "surface_highpass") {
      const tMax = app.state.settings.config.t_max || 3.0;
      const threshold = app.commands.getSolveHighpassThreshold();
      el.innerHTML = `
        <div class="sub-legend-block">
          <span class="sub-legend-desc">Surface Highpass (≥ threshold)</span>
          ${app.commands.diagnosticPaletteLegendHtml(paletteLegend)}
          <div class="legend-labels"><span>${threshold.toFixed(2)} mm</span><span>${(tMax/2).toFixed(1)} mm</span><span>${tMax.toFixed(1)} mm</span></div>
        </div>`;
      row.classList.remove("is-hidden");
    } else if (view === "surface_explorer") {
      el.innerHTML = `
        <div class="sub-legend-block">
          <span class="sub-legend-desc">Explorer (color = solved material identity in the selected layer)</span>
          <div class="solve-legend-inline">
            <span><span class="solve-legend-swatch is-empty"></span> No material at this layer</span>
            <span><span class="solve-legend-swatch is-muted-cap"></span> Muted cap color = boundary cap; cap filament color = detail cap</span>
          </div>
        </div>`;
      row.classList.remove("is-hidden");
    } else if (view === "cap_diff" || view === "filament_diff") {
      const diff = view === "cap_diff"
        ? app.commands.getCurrentSolveCapDiffFromCache()
        : app.commands.getCurrentSolveFilamentDiffFromCache();
      const maxDeltaLabel = diff ? app.commands.formatSolveDiffMm(diff.maxAbsDelta) : null;
      const diffLabel = view === "cap_diff" ? "cap-thickness" : "filament-thickness";
      const subjectLabel = view === "cap_diff" ? "cap" : "filament";
      let desc = `White = changed ${diffLabel} pixels`;
      let gradient = "linear-gradient(to right, #000000, #ffffff)";
      let labels = ["none", "", "changed"];
      if (app.state.solve.solveCapDiffMode === "added") {
        desc = maxDeltaLabel
          ? `Green = ${diffLabel} increase (mm, max +${maxDeltaLabel})`
          : `Green = ${diffLabel} added`;
        gradient = "linear-gradient(to right, #000000, #0f7a3c)";
        labels = maxDeltaLabel ? ["0 mm", "", `+${maxDeltaLabel}`] : ["none", "", "added"];
      } else if (app.state.solve.solveCapDiffMode === "removed") {
        desc = maxDeltaLabel
          ? `Red = ${diffLabel} decrease (mm, max -${maxDeltaLabel})`
          : `Red = ${diffLabel} removed`;
        gradient = "linear-gradient(to right, #000000, #b00020)";
        labels = maxDeltaLabel ? ["0 mm", "", `-${maxDeltaLabel}`] : ["none", "", "removed"];
      } else if (app.state.solve.solveCapDiffMode === "signed") {
        desc = maxDeltaLabel
          ? `Green = thicker ${subjectLabel}, red = thinner ${subjectLabel} (${diffLabel} delta in mm, max ±${maxDeltaLabel})`
          : `Green = thicker ${subjectLabel}, red = thinner ${subjectLabel} (${diffLabel} delta in mm)`;
        gradient = "linear-gradient(to right, #8b0000, #10151c, #0f7a3c)";
        labels = maxDeltaLabel ? [`-${maxDeltaLabel}`, "0", `+${maxDeltaLabel}`] : ["thinner", "0", "thicker"];
      }
      el.innerHTML = `
        <div class="sub-legend-block">
          <span class="sub-legend-desc">${app.commands.esc(desc)}</span>
          <div class="legend-bar" style="background:${gradient}"></div>
          <div class="legend-labels"><span>${app.commands.esc(labels[0])}</span><span>${app.commands.esc(labels[1])}</span><span>${app.commands.esc(labels[2])}</span></div>
        </div>`;
      row.classList.remove("is-hidden");
    } else {
      row.classList.add("is-hidden");
    }
  }

function renderSolveThicknessMaps(selectedRuns) {
    const mapsGrid = app.state.ui.$("#filamentMapsGrid");
    if (!mapsGrid) return;
    mapsGrid.innerHTML = selectedRuns.map(run => {
      const r = run.results;
      if (!r) return "";
      const showLabel = selectedRuns.length > 1;
      let html = showLabel ? `<div class="filament-maps-label"><strong>${app.commands.esc(run.label)}</strong></div>` : "";
      html += (r.filament_maps || []).map((m) => {
        const fil = app.commands.filamentById(m.filament_id);
        const label = m.display_name || fil?.color_name || fil?.display_name || m.filament_id;
        const mapKey = `filament:${m.filament_id}`;
        const volume = app.commands.formatThicknessMapVolume(m.volume_mm3);
        const statsLine = [
          volume,
          `max ${m.max_d?.toFixed(2) || 0} mm`,
          `${m.active_px?.toLocaleString() || 0} px`,
        ].filter(Boolean).join(" · ");
        const clickAttrs = m.map_url ? ` data-solve-card-kind="thickness" data-run-id="${app.commands.esc(run.id)}"` : "";
        return `
          <div class="filament-map-card${m.map_url ? ' is-clickable' : ''}" data-map-key="${app.commands.escAttr(mapKey)}"${clickAttrs}>
            <div class="solve-grid-column-header">
              <h4>${app.commands.esc(label)}</h4>
              <div class="comparison-column-chips"><span class="comparison-chip" style="background:${app.commands.esc(fil?.hex || '#ccc')}"></span></div>
              <div class="comparison-column-stats">${app.commands.esc(statsLine)}</div>
            </div>
            ${m.map_url ? `<img src="${app.commands.esc(m.map_url)}" alt="${app.commands.esc(m.filament_id)}">` : `<div class="solve-grid-empty-map">No thickness</div>`}
            ${app.commands.buildSolveCardScaleBarSlot()}
          </div>`;
      }).join("");
      const capItems = app.commands.getSolveWhiteCapThicknessItems(run);
      capItems.forEach((item) => {
        const capStatsLine = item.available ? [
            app.commands.formatThicknessMapVolume(item.volumeMm3),
            `max ${item.maxD.toFixed(2)} mm`,
            `${item.activePx.toLocaleString()} px`,
          ].filter(Boolean).join(" · ") : "Unavailable";
        const clickAttrs = item.available
          ? ` data-solve-card-kind="thickness" data-run-id="${app.commands.esc(run.id)}"`
          : "";
        html += `
          <div class="filament-map-card${item.available ? ' is-clickable' : ' is-unavailable'}" data-map-key="${app.commands.escAttr(item.key)}"${clickAttrs}>
            <div class="solve-grid-column-header">
              <h4>${app.commands.esc(item.label)}</h4>
              <div class="comparison-column-chips"><span class="comparison-chip" style="background:#F4EFEB"></span></div>
              <div class="comparison-column-stats">${app.commands.esc(capStatsLine)}</div>
            </div>
            ${item.available ? `<img src="${app.commands.esc(item.url)}" alt="${app.commands.esc(item.label)}">` : `<div class="solve-grid-empty-map">Unavailable</div>`}
            ${app.commands.buildSolveCardScaleBarSlot()}
          </div>`;
      });
      return html;
    }).join("");
  }

function formatThicknessMapVolume(volumeMm3) {
    const volume = Number(volumeMm3);
    if (!Number.isFinite(volume) || volume < 0) return "";
    if (volume >= 1000) return `vol ${(volume / 1000).toFixed(2)} cm3`;
    return `vol ${volume.toFixed(volume >= 10 ? 0 : 2)} mm3`;
  }

  Object.assign(app.commands, {
    ensureSurfaceData,
    rgbFromHex,
    mixRgb,
    getRunBaseFilamentId,
    getRunCapFilamentId,
    ensureExplorerMaterialData,
    isSolveSurfaceView,
    getSolveSurfaceBaseThickness,
    getSolveSurfaceLayerHeight,
    getSolveSurfaceTMax,
    getSolveSurfaceExtraSteps,
    legacyScalarPalette,
    inferno,
    getRunDiagnosticPaletteVersion,
    sampleScalarPalette,
    strokeLayerContourPaths,
    strokeDiscreteLabelBoundaries,
    renderHighpass,
    initHighpassControls,
    renderExplorer,
    renderExplorerRich,
    initExplorerControls,
    isBandedSolveRun,
    hasRecipeViewerArtifacts,
    shouldDefaultColorRegionsToRecipe,
    isSolveSourceColumnView,
    shouldShowSolveSourceColumn,
    buildSolveCardScaleBarSlot,
    buildSolveSourceColumn,
    getSolveSourceTargetKindForView,
    getSolveRunHeaderStats,
    getSolveContourUrl,
    loadSolveContourData,
    drawSolveContourOverlay,
    hasRecipeContourArtifacts,
    drawRecipeBoundaryOverlay,
    positionContourCanvasOverImage,
    renderSurfaceContourOverlay,
    renderSolveContourCanvasForRun,
    renderSolveContourCanvases,
    renderSolveLightboxContours,
    syncRecipeLightboxContoursToggle,
    refreshVisibleSolveContours,
    buildSolveRunColumn,
    buildSolveRecipeColumn,
    buildSolveRunVisualColumn,
    getSolveHighpassThreshold,
    getSolveExplorerCenter,
    getSolveExplorerHalfBand,
    renderSolveSurfaceCanvases,
    updateSolveColumnImages,
    isWhiteCapMapView,
    isSolveWhiteCapView,
    isSolveContourView,
    getSolveTopLevelView,
    isSolveDevViewsEnabled,
    coerceSolveViewForAccess,
    setSolveAdvancedViewsOpen,
    syncSolveViewToggleActive,
    _getSolveRunResultUrl,
    updateSolveSubControls,
    getSolveViewCaption,
    updateSolveViewCaption,
    getDiagnosticPaletteLegendState,
    diagnosticPaletteLegendHtml,
    updateSolveLegend,
    renderSolveThicknessMaps,
    formatThicknessMapVolume,
  });
}
