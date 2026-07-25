import { assertPolledJobIdentity } from "../../core/polling.js";

/**
 * Install the solve/recipe-viewer feature commands.
 * @param {import("../../core/types.js").ApplicationContext} app
 */
export function installFeaturesSolveRecipeViewer(app) {
function _recipeKeyFromEntries(entries) {
    const norm = (entries || [])
      .map((e) => ({
        fid: String(e?.filament_id ?? e?.filamentId ?? ""),
        th: Number(e?.thickness_mm ?? e?.thicknessMm ?? 0) || 0,
        band: e?.band_index ?? e?.bandIndex ?? null,
      }))
      .filter((e) => e.fid && e.th > 1e-9)
      .sort((a, b) => {
        const aBand = a.band == null ? -1 : Number(a.band);
        const bBand = b.band == null ? -1 : Number(b.band);
        if (aBand !== bBand) return aBand - bBand;
        return a.fid < b.fid ? -1 : a.fid > b.fid ? 1 : a.th - b.th;
      });
    if (!norm.length) return "base-only";
    return norm.map((e) => `${e.band == null ? "" : `b${Number(e.band)}:`}${e.fid}:${e.th.toFixed(4)}`).join(" | ");
  }

function buildRecipeIdentityMap(stackLabels, stackKeyById) {
    if (!stackLabels?.data || !Array.isArray(stackKeyById)) return null;
    const { width, height, data: stackIds } = stackLabels;
    if (stackIds.length !== width * height) return null;

    const identityByKey = new Map();
    const identityByStackId = new Uint32Array(stackKeyById.length);
    stackKeyById.forEach((key, stackId) => {
      if (!identityByKey.has(key)) identityByKey.set(key, identityByKey.size);
      identityByStackId[stackId] = identityByKey.get(key);
    });

    const data = new Uint32Array(stackIds.length);
    for (let i = 0; i < stackIds.length; i++) {
      const stackId = stackIds[i];
      if (stackId >= identityByStackId.length) return null;
      data[i] = identityByStackId[stackId];
    }
    return { width, height, data };
  }
  function buildDiscreteLabelBoundaryMask(identityMap) {
    if (!identityMap?.data) return null;
    const { width, height, data } = identityMap;
    if (width <= 0 || height <= 0 || data.length !== width * height) return null;
    const vertical = new Uint8Array(Math.max(0, width - 1) * height);
    const horizontal = new Uint8Array(width * Math.max(0, height - 1));

    for (let y = 0; y < height; y++) {
      const row = y * width;
      const boundaryRow = y * Math.max(0, width - 1);
      for (let x = 0; x < width - 1; x++) {
        vertical[boundaryRow + x] = data[row + x] === data[row + x + 1] ? 0 : 1;
      }
    }
    for (let y = 0; y < height - 1; y++) {
      const row = y * width;
      const nextRow = row + width;
      for (let x = 0; x < width; x++) {
        horizontal[row + x] = data[row + x] === data[nextRow + x] ? 0 : 1;
      }
    }
    return { width, height, vertical, horizontal };
  }

  async function ensureRecipeArtifactData(run) {
    if (!run?.results) return null;
    if (app.state.ui.recipeDataCache[run.id]) return app.state.ui.recipeDataCache[run.id];
    if (app.state.ui.recipeDataPromiseCache[run.id]) return app.state.ui.recipeDataPromiseCache[run.id];
    const r = run.results;
    if (!r.explorer_stack_label_bin_url || !Array.isArray(r.explorer_stack_table)) return null;
    const generation = app.state.ui.recipeDataGeneration[run.id] || 0;

    const pending = (async () => {
      const stackLabels = await app.commands.loadUint32Blob(r.explorer_stack_label_bin_url);
      if (!stackLabels) return null;

      // Per stack id -> canonical recipe key + raw color entries (for readout
      // and identity contours). Different stack ids with the same physical
      // recipe intentionally share one canonical key.
      const stackKeyById = r.explorer_stack_table.map((stack) => app.commands._recipeKeyFromEntries(stack));
      const stackEntriesById = r.explorer_stack_table.map((stack) =>
        (Array.isArray(stack) ? stack : [])
          .map((e) => ({
            filamentId: String(e?.filament_id ?? e?.filamentId ?? ""),
            thicknessMm: Number(e?.thickness_mm ?? e?.thicknessMm ?? 0) || 0,
            bandIndex: e?.band_index ?? e?.bandIndex ?? null,
            materialRole: String(e?.material_role ?? e?.materialRole ?? ""),
          }))
          .filter((e) => e.filamentId && e.thicknessMm > 1e-9)
      );
      const recipeIdentityMap = app.commands.buildRecipeIdentityMap(stackLabels, stackKeyById);
      const recipeBoundaries = app.commands.buildDiscreteLabelBoundaryMask(recipeIdentityMap);
      return {
        cookbook: null,
        stackLabels,
        stackKeyById,
        stackEntriesById,
        recipeIdentityMap,
        recipeBoundaries,
      };
    })();

    app.state.ui.recipeDataPromiseCache[run.id] = pending;
    try {
      const data = await pending;
      if (!data || (app.state.ui.recipeDataGeneration[run.id] || 0) !== generation) return null;
      app.state.ui.recipeDataCache[run.id] = data;
      return data;
    } finally {
      if (app.state.ui.recipeDataPromiseCache[run.id] === pending) delete app.state.ui.recipeDataPromiseCache[run.id];
    }
  }

  async function ensureRecipeCookbook(run, recipeData) {
    if (!run?.results || !recipeData) return null;
    if (recipeData.cookbook) return recipeData.cookbook;
    if (app.state.ui.recipeCookbookPromiseCache[run.id]) return app.state.ui.recipeCookbookPromiseCache[run.id];
    const url = run.results.color_recipe_breakdown_cookbook_url;
    if (!url) return null;
    const generation = app.state.ui.recipeDataGeneration[run.id] || 0;
    const pending = (async () => {
      try {
        const resp = await fetch(url);
        return resp.ok ? await resp.json() : null;
      } catch {
        return null;
      }
    })();
    app.state.ui.recipeCookbookPromiseCache[run.id] = pending;
    try {
      const cookbook = await pending;
      if (cookbook && (app.state.ui.recipeDataGeneration[run.id] || 0) === generation) {
        recipeData.cookbook = cookbook;
      }
      return cookbook;
    } finally {
      if (app.state.ui.recipeCookbookPromiseCache[run.id] === pending) delete app.state.ui.recipeCookbookPromiseCache[run.id];
    }
  }

  async function ensureRecipeData(run) {
    const recipeData = await app.commands.ensureRecipeArtifactData(run);
    if (!recipeData) return null;
    await app.commands.ensureRecipeCookbook(run, recipeData);
    return recipeData;
  }

  function recipePct(fraction) {
    const pct = (Number(fraction) || 0) * 100;
    if (pct > 0 && pct < 0.1) return "<0.1%";
    return `${pct.toFixed(1)}%`;
  }

  function recipeFilamentLabel(fid) {
    const fil = app.commands.filamentById(fid);
    return fil?.color_name || fil?.display_name || fid;
  }

  function recipeFilamentChip(fid) {
    const fil = app.commands.filamentById(fid);
    const hex = fil?.hex || "#888";
    return `<span class="recipe-fil-chip" style="background:${hex}" title="${app.commands.esc(app.commands.recipeFilamentLabel(fid))}"></span>`;
  }

  function recipeKeysForNode(node) {
    const keys = new Set();
    if (!node) return keys;
    if (node.kind === "recipe") {
      keys.add(app.commands._recipeKeyFromEntries(node.data.recipe));
    } else if (node.kind === "combo") {
      (node.data.recipes || []).forEach((rec) => keys.add(app.commands._recipeKeyFromEntries(rec.recipe)));
    } else if (node.kind === "family") {
      if (node.data.n_colors === 0) {
        keys.add("base-only");
      } else {
        (node.data.combos || []).forEach((combo) =>
          (combo.recipes || []).forEach((rec) => keys.add(app.commands._recipeKeyFromEntries(rec.recipe))),
        );
      }
    }
    return keys;
  }

  function renderRecipeTree(cookbook, registry) {
    const families = cookbook?.families || [];
    if (!families.length) return `<p class="recipe-tree-empty muted-line">No color recipes in this solve.</p>`;
    const lines = [];
    families.forEach((family) => {
      const famNode = { kind: "family", data: family, recipeIds: [] };
      const famIdx = registry.length;
      registry.push(famNode);
      if (family.n_colors === 0) {
        // Base-only collapses: family == combo == recipe. Register its single
        // recipe so the bucket can show it; the tree node itself is a leaf.
        const recIdx = registry.length;
        registry.push({ kind: "recipe", data: {
          recipe: [], recipe_key: "base-only",
          area_fraction: family.area_fraction, pixel_count: family.pixel_count,
        } });
        famNode.recipeIds.push(recIdx);
        lines.push(`<div class="recipe-node recipe-node-family" data-node-id="${famIdx}" tabindex="0" role="button">
          <span class="recipe-node-label">Base only</span>
          <span class="recipe-node-pct">${app.commands.recipePct(family.area_fraction)}</span>
        </div>`);
        return;
      }
      lines.push(`<div class="recipe-node recipe-node-family" data-node-id="${famIdx}" tabindex="0" role="button">
        <span class="recipe-node-label">${family.n_colors}-color</span>
        <span class="recipe-node-pct">${app.commands.recipePct(family.area_fraction)}</span>
      </div>`);
      (family.combos || []).forEach((combo) => {
        const comboNode = { kind: "combo", data: combo, recipeIds: [] };
        const comboIdx = registry.length;
        registry.push(comboNode);
        (combo.recipes || []).forEach((rec) => {
          const recIdx = registry.length;
          registry.push({ kind: "recipe", data: rec });
          comboNode.recipeIds.push(recIdx);
          famNode.recipeIds.push(recIdx);
        });
        const comboChips = (combo.filaments || []).map(app.commands.recipeFilamentChip).join("");
        const comboName = (combo.filaments || []).map(app.commands.recipeFilamentLabel).join(" + ");
        const nRecipes = (combo.recipes || []).length;
        lines.push(`<div class="recipe-node recipe-node-combo" data-node-id="${comboIdx}" tabindex="0" role="button">
          <span class="recipe-node-label">${comboChips}<span class="recipe-node-name">${app.commands.esc(comboName)}</span></span>
          <span class="recipe-node-count" title="${nRecipes} recipe${nRecipes === 1 ? "" : "s"}">${nRecipes}</span>
          <span class="recipe-node-pct">${app.commands.recipePct(combo.area_fraction)}</span>
        </div>`);
      });
    });
    return lines.join("");
  }

  function partitionRecipeTail(fractions, opts) {
    const threshold = opts && opts.threshold != null ? opts.threshold : 0.001;
    const minVisible = opts && opts.minVisible != null ? opts.minVisible : 3;
    const n = fractions.length;
    let above = 0;
    for (let i = 0; i < n; i++) if ((fractions[i] || 0) >= threshold) above++;
    let visible = Math.max(above, Math.min(minVisible, n));
    let tailCount = n - visible;
    if (tailCount < 2) { visible = n; tailCount = 0; }
    let tailFraction = 0;
    for (let i = visible; i < n; i++) tailFraction += (fractions[i] || 0);
    return { visible, tailCount, tailFraction };
  }

  function renderRecipeBucket(node, registry) {
    if (!node || (node.kind !== "family" && node.kind !== "combo")) {
      return `<div class="recipe-bucket-empty muted-line">Select a family or combo to list its recipes.</div>`;
    }
    const ids = (node.recipeIds || []).slice().sort(
      (a, b) => (registry[b].data.area_fraction || 0) - (registry[a].data.area_fraction || 0),
    );
    if (!ids.length) return `<div class="recipe-bucket-empty muted-line">No recipes.</div>`;

    // Column filaments = the filaments used across these recipes, canonical order.
    // A combo shares one set; a family unions its combos' (rows leave "—" gaps).
    const cols = [];
    const seen = new Set();
    ids.forEach((idx) => (registry[idx].data.recipe || []).forEach((e) => {
      if (!seen.has(e.filament_id)) { seen.add(e.filament_id); cols.push(e.filament_id); }
    }));
    cols.sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));

    if (!cols.length) {
      // 0-color / base-only: nothing to tabulate.
      const idx = ids[0];
      return `<div class="recipe-bucket-table"><div class="recipe-node recipe-bucket-row" data-node-id="${idx}" tabindex="0" role="button">
        <span class="recipe-bucket-cell recipe-bucket-cell-name">Base only</span>
        <span class="recipe-bucket-cell recipe-bucket-pct">${app.commands.recipePct(registry[idx].data.area_fraction)}</span>
      </div></div>`;
    }

    const head = `<div class="recipe-bucket-head">
      ${cols.map((fid) => `<span class="recipe-bucket-cell recipe-bucket-hcell">${app.commands.recipeFilamentChip(fid)}</span>`).join("")}
      <span class="recipe-bucket-cell recipe-bucket-pct">%</span>
    </div>`;
    const renderRow = (idx, extraCls) => {
      const rec = registry[idx].data;
      const byFid = {};
      (rec.recipe || []).forEach((e) => { byFid[e.filament_id] = e.thickness_mm; });
      const cells = cols.map((fid) =>
        `<span class="recipe-bucket-cell">${byFid[fid] != null ? Number(byFid[fid]).toFixed(2) : "—"}</span>`,
      ).join("");
      return `<div class="recipe-node recipe-bucket-row${extraCls}" data-node-id="${idx}" tabindex="0" role="button">
        ${cells}<span class="recipe-bucket-cell recipe-bucket-pct">${app.commands.recipePct(rec.area_fraction)}</span>
      </div>`;
    };

    const fractions = ids.map((idx) => registry[idx].data.area_fraction || 0);
    const { visible, tailCount, tailFraction } = app.commands.partitionRecipeTail(fractions);
    const headRows = ids.slice(0, visible).map((idx) => renderRow(idx, "")).join("");
    let tailHtml = "";
    if (tailCount > 0) {
      const tailRows = ids.slice(visible).map((idx) => renderRow(idx, " recipe-bucket-tail-row")).join("");
      tailHtml = `<div class="recipe-bucket-tail-toggle" data-tail-count="${tailCount}" tabindex="0" role="button" aria-expanded="false">
        <span class="recipe-bucket-tail-chev" aria-hidden="true">▸</span>
        <span class="recipe-bucket-tail-label">+${tailCount} more under 0.1%</span>
        <span class="recipe-bucket-cell recipe-bucket-pct">${app.commands.recipePct(tailFraction)}</span>
      </div>${tailRows}`;
    }

    return `<div class="recipe-bucket-table">
      <div class="recipe-bucket-caption muted-line">thickness · mm</div>
      ${head}${headRows}${tailHtml}
    </div>`;
  }

  function highlightRecipeRegions(canvas, recipeData, selectedKeys) {
    if (!canvas || !recipeData) return;
    const { stackLabels, stackKeyById } = recipeData;
    const { width, height, data } = stackLabels;
    canvas.width = width;
    canvas.height = height;
    const ctx = canvas.getContext("2d");
    const img = ctx.createImageData(width, height);
    const px = img.data;
    const hasSelection = selectedKeys && selectedKeys.size > 0;
    for (let i = 0; i < data.length; i++) {
      const off = i * 4;
      const key = stackKeyById[data[i]];
      const selected = hasSelection && key !== undefined && selectedKeys.has(key);
      if (!hasSelection) {
        px[off + 3] = 0; // nothing selected: fully transparent overlay
      } else if (selected) {
        // Cyan tint marks the selected union.
        px[off] = 0; px[off + 1] = 220; px[off + 2] = 255; px[off + 3] = 90;
      } else {
        // Dim everything else so the selection reads clearly.
        px[off] = 0; px[off + 1] = 0; px[off + 2] = 0; px[off + 3] = 150;
      }
    }
    ctx.putImageData(img, 0, 0);
    canvas.style.display = "block";
  }

  function recipeReadoutHtml(entries) {
    if (!entries || !entries.length) {
      return `<div class="recipe-region-readout-empty">Base only — no color filament here.</div>`;
    }
    const rows = entries
      .map((e) => {
        const band = e.bandIndex == null ? "" : `Band ${Number(e.bandIndex) + 1} · `;
        const fill = e.materialRole === "white_fill" ? "White fill · " : "";
        return `<div class="recipe-readout-row">${app.commands.recipeFilamentChip(e.filamentId)}<span class="recipe-readout-name">${app.commands.esc(`${band}${fill}${app.commands.recipeFilamentLabel(e.filamentId)}`)}</span><span class="recipe-readout-th">${Number(e.thicknessMm).toFixed(2)} mm</span></div>`;
      })
      .join("");
    return rows;
  }

  async function openRecipeLightbox(runId) {
    const lb = app.state.ui.$("#compLightbox");
    const content = app.state.ui.$("#compLightboxContent");
    if (!lb || !content) return;
    const run = app.state.solve.solveRuns.find((r) => r.id === runId);
    if (!run || !run.results) return;
    const lifecycle = app.commands.beginLightboxLifecycle();
    app.state.solve._solveLightboxState = { kind: "recipe", runId };

    const recipeData = await app.commands.ensureRecipeData(run);
    if (!lifecycle.isActive()) return;
    const imgUrl = app.commands._getSolveRunResultUrl(run.results, "recipe_regions") || "";
    const recipeBoundariesAvailable = Boolean(recipeData?.recipeBoundaries);
    const registry = [];
    const treeHtml = recipeData?.cookbook
      ? app.commands.renderRecipeTree(recipeData.cookbook, registry)
      : `<p class="recipe-tree-empty muted-line">Recipe data unavailable for this run.</p>`;

    content.innerHTML = `
      <div class="recipe-lightbox-wrap">
        <div class="recipe-lightbox-media">
          ${app.commands.buildSolveLightboxHeader(run, app.commands.getSolveLightboxViewLabel("recipe_regions"))}
          <div class="recipe-lightbox-toolbar">
            <button class="sub-toggle-btn${recipeBoundariesAvailable && app.state.solve.solveContoursEnabled ? " is-active" : ""}" id="recipeLightboxContoursToggle" type="button" data-contours-available="${recipeBoundariesAvailable ? "true" : "false"}" aria-pressed="${recipeBoundariesAvailable && app.state.solve.solveContoursEnabled ? "true" : "false"}" title="${recipeBoundariesAvailable ? "Show recipe boundaries on the image" : "Recipe boundaries are unavailable for this older run"}"${recipeBoundariesAvailable ? "" : " disabled aria-disabled=\"true\""}>Contours</button>
          </div>
          <div class="recipe-lightbox-frame comp-lightbox-media">
            <img class="comp-lightbox-img recipe-lightbox-img" src="${app.commands.esc(imgUrl)}" style="image-rendering:pixelated;">
            <canvas class="recipe-lightbox-contour-canvas solve-lightbox-contour-canvas" aria-label="${app.commands.escAttr(run.label)} recipe boundaries"></canvas>
            <canvas class="recipe-lightbox-highlight-canvas" aria-hidden="true"></canvas>
            <div class="recipe-region-readout" id="recipeRegionReadoutPanel" data-anchor="top-right">
              <div class="recipe-region-readout-title">Region Recipe</div>
              <div class="recipe-region-readout-body" id="recipeRegionReadout">
                <div class="recipe-region-readout-empty">Hover a highlighted region for its thicknesses.</div>
              </div>
            </div>
          </div>
        </div>
        <aside class="recipe-lightbox-panel">
          <div class="recipe-panel-cols">
            <div class="recipe-panel-col recipe-hierarchy-col">
              <div class="recipe-panel-title">Recipe taxonomy <span class="recipe-panel-hint">select to highlight</span></div>
              <div class="recipe-tree" id="recipeTree">${treeHtml}</div>
            </div>
            <div class="recipe-panel-col recipe-bucket-col">
              <div class="recipe-panel-title">Recipes <span class="recipe-panel-hint" id="recipeBucketHint"></span></div>
              <div class="recipe-bucket" id="recipeBucket"><div class="recipe-bucket-empty muted-line">Select a family or combo to list its recipes.</div></div>
            </div>
          </div>
        </aside>
      </div>`;
    lb.classList.remove("is-hidden");

    const wrap = content.querySelector(".recipe-lightbox-wrap");
    if (wrap) {
      wrap.addEventListener("click", (e) => {
        // Clicks on the image, side panel, header, or toolbar keep the lightbox
        // open; clicking the empty matte around them closes it.
        if (e.target.closest(".recipe-lightbox-panel, .recipe-lightbox-img, .recipe-region-readout, .comp-lightbox-topbar, .recipe-lightbox-toolbar")) {
          e.stopPropagation();
          return;
        }
        app.commands.closeCompLightbox();
      });
    }

    if (!recipeData) {
      return;
    }

    const tree = content.querySelector("#recipeTree");
    const bucket = content.querySelector("#recipeBucket");
    const bucketHint = content.querySelector("#recipeBucketHint");
    const canvas = content.querySelector(".recipe-lightbox-highlight-canvas");
    const imgEl = content.querySelector(".recipe-lightbox-img");
    const frame = content.querySelector(".recipe-lightbox-frame");
    const readoutPanel = content.querySelector("#recipeRegionReadoutPanel");
    const readout = content.querySelector("#recipeRegionReadout");
    let selectedKeys = null;

    function sizeCanvasToImage() {
      if (!imgEl || !canvas) return;
      const w = imgEl.clientWidth, h = imgEl.clientHeight;
      if (!w || !h) return;
      canvas.style.width = `${w}px`;
      canvas.style.height = `${h}px`;
    }

    // Upscale the (low-resolution, pixelated) region image to fill its available
    // area, preserving aspect. The frame fills the space left of the panel; the
    // image is sized to the contain-fit of that frame and the highlight canvas is
    // synced to match, so the side panel can never crunch the enlarged image.
    function fitImage() {
      if (!imgEl) return;
      const frame = imgEl.closest(".recipe-lightbox-frame");
      if (!frame) return;
      const natW = imgEl.naturalWidth || (recipeData && recipeData.stackLabels.width) || 0;
      const natH = imgEl.naturalHeight || (recipeData && recipeData.stackLabels.height) || 0;
      if (!natW || !natH) return;
      const availW = frame.clientWidth, availH = frame.clientHeight;
      if (availW <= 0 || availH <= 0) return;
      const scale = Math.min(availW / natW, availH / natH);
      imgEl.style.width = `${Math.max(1, Math.floor(natW * scale))}px`;
      imgEl.style.height = `${Math.max(1, Math.floor(natH * scale))}px`;
      sizeCanvasToImage();
    }

    function applySelection(node, nodeEl) {
      if (nodeEl?.classList.contains("is-active")) {
        selectedKeys = null;
        content.querySelectorAll(".recipe-node.is-active").forEach((el) => el.classList.remove("is-active"));
        app.commands.highlightRecipeRegions(canvas, recipeData, null);
        if (bucket) bucket.innerHTML = `<div class="recipe-bucket-empty muted-line">Select a family or combo to list its recipes.</div>`;
        if (bucketHint) bucketHint.textContent = "";
        if (readout) readout.innerHTML = `<div class="recipe-region-readout-empty">Hover a region for its thicknesses.</div>`;
        sizeCanvasToImage();
        return;
      }
      selectedKeys = app.commands.recipeKeysForNode(node);
      content.querySelectorAll(".recipe-node.is-active").forEach((el) => el.classList.remove("is-active"));
      if (nodeEl) nodeEl.classList.add("is-active");
      app.commands.highlightRecipeRegions(canvas, recipeData, selectedKeys);
      sizeCanvasToImage();
      // A family/combo selection refills the bucket with its recipes; clicking a
      // recipe IN the bucket leaves the bucket as-is and just re-highlights.
      if (node.kind === "family" || node.kind === "combo") {
        if (bucket) bucket.innerHTML = app.commands.renderRecipeBucket(node, registry);
        if (bucketHint) {
          bucketHint.textContent = node.kind === "family"
            ? (node.data.n_colors === 0 ? "Base only" : `${node.data.n_colors}-color`)
            : (node.data.filaments || []).map(app.commands.recipeFilamentLabel).join(" + ");
        }
      }
    }

    // One handler for both panes — every node carries its registry index.
    const onNodeClick = (e) => {
      const nodeEl = e.target.closest(".recipe-node[data-node-id]");
      if (!nodeEl) return;
      const node = registry[parseInt(nodeEl.dataset.nodeId, 10)];
      if (node) applySelection(node, nodeEl);
    };
    tree.addEventListener("click", onNodeClick);
    const onBucketClick = (e) => {
      // The sub-0.1% tail toggle reveals/hides the collapsed rows; everything
      // else in the bucket is a recipe node that highlights its regions.
      const toggle = e.target.closest(".recipe-bucket-tail-toggle");
      if (toggle) {
        const table = toggle.closest(".recipe-bucket-table");
        if (table) {
          const expanded = table.classList.toggle("tail-expanded");
          toggle.setAttribute("aria-expanded", expanded ? "true" : "false");
          const count = toggle.dataset.tailCount;
          const label = toggle.querySelector(".recipe-bucket-tail-label");
          if (label) label.textContent = expanded ? `Hide ${count} under 0.1%` : `+${count} more under 0.1%`;
        }
        return;
      }
      onNodeClick(e);
    };
    if (bucket) bucket.addEventListener("click", onBucketClick);

    function updateRecipeReadoutAnchor(e) {
      if (!readoutPanel || !frame) return;
      const anchor = readoutPanel.dataset.anchor === "top-left" ? "top-left" : "top-right";
      const panelRect = readoutPanel.getBoundingClientRect();
      const pad = 10;
      const nearPanel = (
        e.clientX >= panelRect.left - pad &&
        e.clientX <= panelRect.right + pad &&
        e.clientY >= panelRect.top - pad &&
        e.clientY <= panelRect.bottom + pad
      );
      if (nearPanel) {
        readoutPanel.dataset.anchor = anchor === "top-left" ? "top-right" : "top-left";
      }
    }

    // Hover-to-peek: map the pointer to a pixel, read that region's thicknesses.
    const onFrameMove = (e) => {
      updateRecipeReadoutAnchor(e);
      if (!imgEl) return;
      const rect = imgEl.getBoundingClientRect();
      const w = recipeData.stackLabels.width, h = recipeData.stackLabels.height;
      const px = Math.floor(((e.clientX - rect.left) / rect.width) * w);
      const py = Math.floor(((e.clientY - rect.top) / rect.height) * h);
      if (px < 0 || py < 0 || px >= w || py >= h) return;
      const stackId = recipeData.stackLabels.data[py * w + px];
      const key = recipeData.stackKeyById[stackId];
      // Only read out regions that are part of the current selection (or any
      // region when nothing is selected yet). Hovering outside the selection
      // clears the readout rather than leaving a stale region's thicknesses.
      if (selectedKeys && selectedKeys.size > 0 && !selectedKeys.has(key)) {
        if (readout) readout.innerHTML = `<div class="recipe-region-readout-empty">Hover a highlighted region for its thicknesses.</div>`;
        return;
      }
      const entries = recipeData.stackEntriesById[stackId] || [];
      if (readout) readout.innerHTML = app.commands.recipeReadoutHtml(entries);
    };
    const onFrameLeave = () => {
      if (readoutPanel) readoutPanel.dataset.anchor = "top-right";
    };
    if (frame) {
      frame.addEventListener("mousemove", onFrameMove);
      frame.addEventListener("mouseleave", onFrameLeave);
    }

    // Keep the image fitted AND the contour overlay positioned over it.
    const relayout = () => {
      fitImage();
      app.commands.renderSolveLightboxContours(run, "recipe_regions");
    };
    const onResize = () => relayout();
    window.addEventListener("resize", onResize);
    if (imgEl) {
      if (imgEl.complete && imgEl.naturalWidth) relayout();
      else imgEl.addEventListener("load", relayout, { once: true });
    }
    relayout();

    // Contours toggle: shares the global solveContoursEnabled state with the main
    // Color Regions toggle, so flipping either keeps both (and the grid) in sync.
    const contoursBtn = content.querySelector("#recipeLightboxContoursToggle");
    if (contoursBtn && !contoursBtn.disabled) {
      app.commands.syncRecipeLightboxContoursToggle();
      contoursBtn.addEventListener("click", () => {
        app.state.solve.solveContoursEnabled = !app.state.solve.solveContoursEnabled;
        app.commands.updateSolveSubControls();    // re-syncs the main #solveContoursToggle button
        app.commands.updateSolveColumnImages();   // keep the (hidden) grid correct for when this closes
        app.commands.updateSolveLegend();
        app.commands.renderSolveLightboxContours(run, "recipe_regions");
        app.commands.syncRecipeLightboxContoursToggle();
      });
    }

    lifecycle.addCleanup(() => {
      tree.removeEventListener("click", onNodeClick);
      if (bucket) bucket.removeEventListener("click", onBucketClick);
      if (frame) {
        frame.removeEventListener("mousemove", onFrameMove);
        frame.removeEventListener("mouseleave", onFrameLeave);
      }
      window.removeEventListener("resize", onResize);
    });
  }

  function solveFilamentLabel(fid) {
    const f = app.state.session.allFilaments.find((x) => x.filament_id === fid);
    return f?.color_name || f?.display_name || fid;
  }

  function buildUnsolvablePaletteMessage(check) {
    const unavailable = check?.unavailable || [];
    const missing = (check?.missing || []).filter((fid) => !unavailable.includes(fid));
    const parts = [];
    if (unavailable.length) {
      const names = unavailable.map(app.commands.solveFilamentLabel).join(", ");
      const one = unavailable.length === 1;
      parts.push(`${names} ${one ? "is" : "are"} excluded from the current color model — remove ${one ? "it" : "them"} from the palette, or re-include and re-fit in calibration.`);
    }
    if (missing.length) {
      const names = missing.map(app.commands.solveFilamentLabel).join(", ");
      const one = missing.length === 1;
      parts.push(`${names} ${one ? "has" : "have"} no calibration profile yet — calibrate ${one ? "it" : "them"} before solving.`);
    }
    return `Can't solve. ${parts.join(" ")}`.trim();
  }

  async function handleStartSolve() {
    if (app.state.solve.solveStartPending || app.state.solve.solveStatus.status === "running") return;
    app.state.solve.solveStartPending = true;
    try {
      app.commands.updateSolveReadiness();
      if (app.state.export.exportRunning) {
        app.commands.showToast("Please wait for meshing to finish", "warn");
        return;
      }
      try {
        const exportStatus = await app.api.getExportStatus();
        if (["running", "cancelling"].includes(exportStatus?.status)) {
          app.commands.showToast("Please wait for meshing to finish", "warn");
          return;
        }
      } catch {
        // If the status check fails, continue through the normal solve-start path
        // and let the server return any authoritative error.
      }
      try {
        await app.commands.syncConfigToServer({ throwOnError: true, showErrorToast: true });
      } catch {
        return;
      }

      const settingsIssues = app.commands.getSolveSettingsPreflightIssues();
      if (settingsIssues.length) {
        app.commands.showToast(app.commands.buildSolveSettingsPreflightMessage(settingsIssues), "error");
        return;
      }

      const palette = app.commands.getActivePalette();

      const gatingIssues = app.commands.getPaletteGatingIssues(palette);
      if (app.commands.paletteGatingIssueCount(gatingIssues)) {
        app.commands.showToast(app.commands.buildPaletteGatingMessage(gatingIssues, "Can't solve."), "error");
        return;
      }

      // Preflight: refuse a solve whose palette contains filaments that can't be
      // solved with (excluded from the active model, or uncalibrated) and say so
      // clearly — rather than letting the backend fail mid-solve. Frontend-only:
      // reuses the existing /api/palette/validate precheck endpoint.
      try {
        const check = await app.api.apiPost("/palette/validate", { palette });
        if (check && check.valid === false) {
          app.commands.showToast(app.commands.buildUnsolvablePaletteMessage(check), "error");
          return;
        }
      } catch {
        // Precheck unavailable — fall through and let the normal solve path run.
      }

      const recipeContext = app.commands.buildSolveRecipeContext(palette, app.commands._currentSettingsSnapshot());
      const run = app.commands.createSolveRun(palette, { ...app.state.settings.config }, recipeContext);
      delete app.state.ui.surfaceDataCache[run.id];
      delete app.state.ui.explorerMaterialDataCache[run.id];
      app.state.solve.solveRuns.push(run);
      app.state.solve.selectedRunIds.add(run.id);
      app.state.solve.activeSolveRunId = run.id;

      try {
        const started = await app.api.startSolve({
          palette,
          runId: run.id,
          profileRef: run.profile_ref,
          profileNameAtSolve: run.profile_name_at_solve,
          isProfileModifiedAtSolve: run.is_profile_modified_at_solve,
          recipeSnapshot: run.recipe_snapshot,
        });
        app.state.solve.activeSolveJobId = started?.job_id || null;
        if (!app.state.solve.activeSolveJobId) throw new Error("Solve start did not return a job id.");
        app.state.solve.solveCancelPending = false;
        app.commands.resetOperationElapsedSeconds();
        app.state.solve.solveStatus = {
          status: "running",
          job_id: app.state.solve.activeSolveJobId,
          card_id: run.id,
          progress: "Starting...",
          progress_detail: { overall_pct: 0 },
          elapsed_s: 0,
          result: null,
        };
        // Solve is now a global top-bar action: auto-collapse the settings drawer (close it
        // fully, per the resolved decision) and land on the Preview/results page so the fresh
        // result is visible. switchTab("solve") renders the results page.
        if (app.state.settings.settingsDrawerOpen) app.commands.closeSettingsDrawer();
        app.commands.switchTab("solve");
        app.commands.startSolvePolling(run);
      } catch (err) {
        app.commands.resetSolveRunDeleteConfirm({ render: false });
        app.state.solve.solveRuns = app.state.solve.solveRuns.filter(r => r.id !== run.id);
        app.state.solve.selectedRunIds.delete(run.id);
        if (app.state.solve.activeSolveRunId === run.id) app.state.solve.activeSolveRunId = null;
        app.commands.showToast(`Solve failed to start: ${err.message}`, "error");
      }
    } finally {
      app.state.solve.solveStartPending = false;
      app.commands.updateSolveReadiness();
    }
  }

  async function handleCancelSolve() {
    if (app.state.solve.solveCancelPending || app.state.solve.solveStatus.status !== "running") return;
    const cancellationJobId = app.state.solve.activeSolveJobId;
    if (!cancellationJobId) return;
    app.state.solve.solveCancelPending = true;
    app.commands.renderSolveProgress();
    try {
      const response = await app.api.cancelSolve(cancellationJobId);
      if (app.state.solve.activeSolveJobId !== cancellationJobId) return;
      if (response?.requested) assertPolledJobIdentity(response, cancellationJobId);
      if (!response?.requested) {
        app.state.solve.solveCancelPending = false;
        app.commands.renderSolveProgress();
        return;
      }
      app.commands.showToast("Cancellation requested", "warn");
    } catch {
      if (app.state.solve.activeSolveJobId !== cancellationJobId) return;
      app.state.solve.solveCancelPending = false;
      app.commands.renderSolveProgress();
      app.commands.showToast("Could not request cancellation", "error");
    }
  }

  function startSolvePolling(run) {
    if (app.state.solve.solvePollingOwner) app.state.solve.solvePollingOwner.cancelled = true;
    const pollingJobId = app.state.solve.activeSolveJobId;
    const pollingOwner = { jobId: pollingJobId, cancelled: false };
    app.state.solve.solvePollingOwner = pollingOwner;
    void (async () => {
      try {
        const status = await app.services.pollJobUntilTerminal({
          jobId: pollingJobId,
          fetchStatus: () => app.api.getSolveStatus(),
          isTerminal: (next) => !["running", "cancelling"].includes(next.status),
          shouldContinue: () => (
            !pollingOwner.cancelled
            && app.state.solve.solvePollingOwner === pollingOwner
            && app.state.solve.activeSolveJobId === pollingJobId
          ),
          intervalMs: 500,
          onStatus: (next) => {
            app.state.solve.solveStatus = next;
            app.commands.renderSolveProgress();
            if (["running", "cancelling"].includes(next.status)) {
              app.commands.renderSolveRunSidebar();
              app.commands.updateRail();
            }
          },
          onTransientError: () => {
            app.state.solve.solveStatus = {
              ...app.state.solve.solveStatus,
              progress: "Connection interrupted; retrying solve status...",
              progress_detail: { ...(app.state.solve.solveStatus.progress_detail || {}), stage_label: "Reconnecting to solve..." },
            };
            app.commands.renderSolveProgress();
          },
        });
        if (!status || app.state.solve.solvePollingOwner !== pollingOwner) return;
        app.state.solve.solveCancelPending = false;
        if (status.status === "complete" && status.result) {
          run.results = status.result;
          run.solve_elapsed_s = Number.isFinite(Number(status.elapsed_s)) ? Math.max(0, Number(status.elapsed_s)) : null;
          if (app.state.solve.activeSolveRunId === run.id) app.state.solve.activeSolveRunId = null;
        } else if (status.status === "cancelled") {
          app.commands.removePendingSolveRun(run.id);
          if (app.state.solve.activeSolveRunId === run.id) app.state.solve.activeSolveRunId = null;
        } else if (status.status === "error") {
          if (app.state.solve.activeSolveRunId === run.id) app.state.solve.activeSolveRunId = null;
        }
        app.state.solve.activeSolveJobId = null;
        app.commands.renderSolveTab();
        app.commands.updateRail();
        if (status.status === "complete") {
          app.commands.showToast("Solve complete!", "success");
        } else if (status.status === "error") {
          app.commands.showToast(`Solve error: ${status.progress}`, "error");
        }
      } catch (err) {
        if (app.state.solve.solvePollingOwner !== pollingOwner) return;
        console.warn("[solve] polling error:", err.message);
        app.commands.showToast(`Solve status could not be verified: ${err.message}`, "error");
      } finally {
        if (app.state.solve.solvePollingOwner === pollingOwner) app.state.solve.solvePollingOwner = null;
      }
    })();
  }

  function getCompletedExportRuns() {
      return app.state.solve.solveRuns.filter((run) => run.results && (run.results.card_id || run.id));
    }

  function getExportSelectedRun() {
      return app.state.solve.solveRuns.find((run) => run.id === app.state.export.exportSelectedRunId && run.results && (run.results.card_id || run.id)) || null;
    }

  function ensureSolveRunExportState(run) {
      if (!run || typeof run !== "object") return null;
      if (!Array.isArray(run.exportRecords)) run.exportRecords = [];
      if (typeof run.selectedExportId !== "string") run.selectedExportId = null;
      return run;
    }

  function getRunExportRecords(run) {
      return app.commands.ensureSolveRunExportState(run)?.exportRecords || [];
    }

  function createExportRecord(result, completedAt = Date.now(), durationSeconds = null) {
      if (!result || typeof result !== "object") {
        throw new Error("Completed export did not include a result.");
      }
      const exportId = String(result.export_id || "").trim();
      const outputFormat = String(result.output_format || "").trim();
      const geometrySource = String(result.geometry_source || "").trim();
      const fieldScale = Number(result.field_scale);
      if (!exportId || !outputFormat || !geometrySource || !Number.isFinite(fieldScale) || fieldScale <= 0) {
        throw new Error("Completed export response is missing its canonical identity or settings.");
      }
      return {
        id: exportId,
        completedAt: Number.isFinite(Number(completedAt)) ? Number(completedAt) : Date.now(),
        durationSeconds: Number.isFinite(Number(durationSeconds)) ? Math.max(0, Number(durationSeconds)) : null,
        outputFormat,
        geometrySource,
        fieldScale,
        result: app.commands._cloneValue(result),
        swapPlan: result.swap_plan && typeof result.swap_plan === "object"
          ? app.commands._cloneValue(result.swap_plan)
          : null,
      };
    }

  function appendExportRecordToRun(run, result, completedAt = Date.now(), durationSeconds = null) {
      const state = app.commands.ensureSolveRunExportState(run);
      if (!state) throw new Error("The solve run for this export is no longer available.");
      const record = app.commands.createExportRecord(result, completedAt, durationSeconds);
      state.exportRecords.push(record);
      state.selectedExportId = record.id;
      return record;
    }

  function selectRunExportRecord(run, exportId) {
      const state = app.commands.ensureSolveRunExportState(run);
      if (!state) return null;
      const record = state.exportRecords.find((candidate) => candidate.id === exportId) || null;
      if (record) state.selectedExportId = record.id;
      return record;
    }

  function getSelectedExportRecord(run = app.commands.getExportSelectedRun()) {
      const state = app.commands.ensureSolveRunExportState(run);
      if (!state || !state.exportRecords.length) return null;
      const selected = state.exportRecords.find((record) => record.id === state.selectedExportId)
        || state.exportRecords[state.exportRecords.length - 1];
      state.selectedExportId = selected.id;
      return selected;
    }

  function getSelectedExportResult() {
      return app.commands.getSelectedExportRecord()?.result || null;
    }

  function getSelectedSwapInstructions() {
      return app.commands.getSelectedExportRecord()?.swapPlan?.instructions || "";
    }

  function updateExportFieldScaleState() {
      const sourceEl = app.state.ui.$("#exportGeometrySource");
      const scaleEl = app.state.ui.$("#exportFieldScale");
      if (!sourceEl || !scaleEl) return;
      const outputEl = app.state.ui.$("#exportOutputFormat");
      sourceEl.disabled = app.state.export.exportRunning;
      if (outputEl) outputEl.disabled = app.state.export.exportRunning;
      const disabled = app.state.export.exportRunning || sourceEl.value !== "field_derived";
      scaleEl.disabled = disabled;
      scaleEl.title = app.state.export.exportRunning
        ? "Export settings are locked until the active export finishes."
        : disabled
        ? "Exact solved raster export uses the solve grid directly."
        : "Controls the white-cap field reconstruction detail.";
    }

  function formatExportGeometrySourceLabel(value) {
      return value === "exact_raster" ? "Exact solved raster" : "Field-derived white cap";
    }

  function formatExportOutputFormatLabel(value) {
      return value === "3mf" ? "single 3MF" : "individual STLs";
    }

  function formatExportFieldScaleLabel(value) {
      const scale = parseInt(value || "4", 10) || 4;
      return `${scale}x`;
    }

  function getExportSolvePreviewUrl(run) {
      const result = run?.results || {};
      return result.predicted_appearance_url
        || result.predicted_url
        || result.predicted_color_only_appearance_url
        || result.source_url
        || "";
    }

  function getExportSolveDimensions(run) {
      const result = run?.results || {};
      const imageWidthPx = Number(result.image_w);
      const imageHeightPx = Number(result.image_h);
      const imageWidthMm = Number(result.image_domain_width_mm);
      const imageHeightMm = Number(result.image_domain_height_mm);
      const configuredBorderWidthMm = Number(run?.config?.border_width_mm);
      const borderEnabled = Boolean(run?.config?.border)
        && Number.isFinite(configuredBorderWidthMm)
        && configuredBorderWidthMm > 0;
      const hasPixels = Number.isFinite(imageWidthPx) && imageWidthPx > 0
        && Number.isFinite(imageHeightPx) && imageHeightPx > 0;
      const hasPhysicalSize = Number.isFinite(imageWidthMm) && imageWidthMm > 0
        && Number.isFinite(imageHeightMm) && imageHeightMm > 0;
      const borderWidthMm = borderEnabled ? configuredBorderWidthMm : 0;
      return {
        imageWidthPx: hasPixels ? Math.round(imageWidthPx) : null,
        imageHeightPx: hasPixels ? Math.round(imageHeightPx) : null,
        imageWidthMm: hasPhysicalSize ? imageWidthMm : null,
        imageHeightMm: hasPhysicalSize ? imageHeightMm : null,
        totalWidthMm: hasPhysicalSize ? imageWidthMm + (2 * borderWidthMm) : null,
        totalHeightMm: hasPhysicalSize ? imageHeightMm + (2 * borderWidthMm) : null,
        borderEnabled,
        borderWidthMm,
      };
    }

  function formatExportDimensionMm(value) {
      const number = Number(value);
      if (!Number.isFinite(number)) return "";
      return number.toFixed(2).replace(/\.?0+$/, "");
    }

  function renderExportSolvePreview(run) {
      const media = app.state.ui.$("#exportSolvePreview .export-solve-preview-media");
      const image = app.state.ui.$("#exportSolvePreviewImg");
      const empty = app.state.ui.$("#exportSolvePreviewEmpty");
      const caption = app.state.ui.$("#exportSolvePreviewCaption");
      const dimensions = app.state.ui.$("#exportSolvePreviewDimensions");
      if (!media || !image || !empty || !caption) return;

      image.onload = null;
      image.onerror = null;
      image.hidden = true;
      image.removeAttribute("src");
      image.dataset.previewUrl = "";
      media.style.aspectRatio = run ? app.commands._runAspect(run) : "";
      caption.textContent = run ? run.label : "Selected solve";
      if (dimensions) {
        const solved = app.commands.getExportSolveDimensions(run);
        const imageSizeParts = [];
        if (solved.imageWidthMm !== null) {
          imageSizeParts.push(
            `${app.commands.formatExportDimensionMm(solved.imageWidthMm)} × ${app.commands.formatExportDimensionMm(solved.imageHeightMm)} mm`,
          );
        }
        if (solved.imageWidthPx !== null) {
          imageSizeParts.push(`${solved.imageWidthPx} × ${solved.imageHeightPx} px`);
        }
        const footprint = solved.totalWidthMm !== null
          ? `${app.commands.formatExportDimensionMm(solved.totalWidthMm)} × ${app.commands.formatExportDimensionMm(solved.totalHeightMm)} mm`
          : "Unavailable";
        const totalFootprintRow = solved.borderEnabled
          ? `<span><strong>Total footprint</strong>${footprint} · includes ${app.commands.formatExportDimensionMm(solved.borderWidthMm)} mm border</span>`
          : "";
        dimensions.innerHTML = `
          <span><strong>Image area</strong>${imageSizeParts.join(" · ") || "Unavailable"}</span>
          ${totalFootprintRow}
        `;
      }

      const url = app.commands.getExportSolvePreviewUrl(run);
      if (!url) {
        empty.hidden = false;
        empty.textContent = run ? "Preview unavailable for this solved run" : "Select a solved run to preview it";
        return;
      }

      empty.hidden = false;
      empty.textContent = "Loading solve preview...";
      image.alt = `${run.label} solve preview`;
      image.dataset.previewUrl = url;
      image.onload = () => {
        if (image.dataset.previewUrl !== url) return;
        image.hidden = false;
        empty.hidden = true;
      };
      image.onerror = () => {
        if (image.dataset.previewUrl !== url) return;
        image.hidden = true;
        empty.hidden = false;
        empty.textContent = "Preview could not be loaded";
      };
      image.src = url;
    }

  function formatExportRecordTime(completedAt) {
      const date = new Date(Number(completedAt));
      if (!Number.isFinite(date.getTime())) return "Time unavailable";
      return date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" });
    }

  function renderExportRecordSelector(run, selectedRecord = app.commands.getSelectedExportRecord(run)) {
      const container = app.state.ui.$("#exportRecordList");
      if (!container) return;
      const records = app.commands.getRunExportRecords(run);
      if (!run || !records.length) {
        container.innerHTML = `<div class="export-record-empty">${run ? "No exports generated for this solve yet" : "Select a solved run to see its exports"}</div>`;
        return;
      }

      container.innerHTML = [...records].reverse().map((record) => {
        const originalIndex = records.indexOf(record);
        const methodLabel = record.geometrySource === "exact_raster" ? "Exact raster" : "Field-derived";
        const formatLabel = record.outputFormat === "3mf" ? "3MF" : "STLs";
        const detailBadge = record.geometrySource === "field_derived"
          ? `<span class="export-record-badge">${app.commands.esc(app.commands.formatExportFieldScaleLabel(record.fieldScale))} detail</span>`
          : "";
        return `
          <button class="export-record-card ${record.id === selectedRecord?.id ? "is-selected" : ""}"
                  type="button" data-export-record-id="${app.commands.esc(record.id)}"
                  aria-pressed="${record.id === selectedRecord?.id ? "true" : "false"}">
            <span class="export-record-card-title">
              <span>${app.commands.esc(run.label)} · Export ${originalIndex + 1}</span>
              <span class="export-record-card-time">${app.commands.esc(app.commands.formatExportRecordTime(record.completedAt))}${record.durationSeconds != null ? ` · ${app.commands.esc(app.commands.formatDurationSeconds(record.durationSeconds))}` : ""}</span>
            </span>
            <span class="export-record-card-badges">
              <span class="export-record-badge">${app.commands.esc(formatLabel)}</span>
              <span class="export-record-badge">${app.commands.esc(methodLabel)}</span>
              ${detailBadge}
            </span>
          </button>
        `;
      }).join("");

      container.querySelectorAll(".export-record-card[data-export-record-id]").forEach((button) => {
        button.addEventListener("click", () => {
          if (!app.commands.selectRunExportRecord(run, button.dataset.exportRecordId)) return;
          app.commands.renderExportTab();
          app.commands.updateRail();
        });
      });
    }

  function describeExportPolicy() {
      const source = app.state.ui.$("#exportGeometrySource")?.value || "field_derived";
      const output = app.state.ui.$("#exportOutputFormat")?.value || "3mf";
      const fieldScale = app.state.ui.$("#exportFieldScale")?.value || "4";
      const sourceLabel = app.commands.formatExportGeometrySourceLabel(source);
      const outputLabel = app.commands.formatExportOutputFormatLabel(output);
      const scaleLabel = source === "field_derived"
        ? ` · mesh detail ${app.commands.formatExportFieldScaleLabel(fieldScale)}`
        : "";
      return `${sourceLabel} · ${outputLabel}${scaleLabel}`;
    }

  function handleExportOptionChange() {
      app.commands.updateExportFieldScaleState();
      app.commands.renderExportTab();
      app.commands.updateRail();
    }

  function ensureExportRunSelection() {
      const completed = app.commands.getCompletedExportRuns();
      if (!completed.length) {
        app.state.export.exportSelectedRunId = null;
        return null;
      }
      const selected = completed.find((run) => run.id === app.state.export.exportSelectedRunId) || completed[completed.length - 1];
      app.state.export.exportSelectedRunId = selected.id;
      app.commands.getSelectedExportRecord(selected);
      return selected;
    }

  function renderExportRunSidebar() {
      const container = app.state.ui.$("#exportRunCards");
      if (!container) return;
      app.commands.hideSolveRunHoverPreview();
      container.setAttribute("role", "listbox");
      container.setAttribute("aria-label", "Export run selection");
      app.commands.syncSolveHistoryClearButtons();

      const completed = app.commands.getCompletedExportRuns();
      if (!completed.length) {
        container.innerHTML = `<p class="muted-line" id="exportRunEmpty">No completed solves yet</p>`;
        return;
      }

      let html = "";
      for (let i = completed.length - 1; i >= 0; i--) {
        const run = completed[i];
        const isSelected = run.id === app.state.export.exportSelectedRunId;
        const chips = (run.palette || []).map(fid => {
          const fil = app.state.session.allFilaments.find(f => f.filament_id === fid);
          const hex = fil?.hex || "#888";
          const label = fil?.color_name || fil?.display_name || fid;
          return `<span class="comp-deck-chip color-chip" style="background:${hex}" title="${app.commands.escAttr(label)}"></span>`;
        }).join("");
        const supportChips = app.commands.buildSolveRunSupportChipsHtml(run);
        const solveDuration = app.commands.formatDurationSeconds(run.solve_elapsed_s ?? run.results?.elapsed_s);
        const stats = `<span class="solve-run-card-rmse">${app.commands.formatSolveRunCardRmse(run.results)}${solveDuration ? ` · ${app.commands.esc(solveDuration)}` : ""}</span>`;
        const staleBadge = run.cache_unavailable
          ? `<span class="solve-run-stale-badge">Unavailable after restart</span>`
          : "";
        html += `<div class="solve-run-card compact-deck-card ${isSelected ? "is-selected" : ""}" data-export-run-id="${app.commands.escAttr(run.id)}" tabindex="0" role="option" aria-selected="${isSelected ? "true" : "false"}">
          <div class="solve-run-card-header compact-deck-card-header">
            <span class="solve-run-label compact-deck-card-title" title="${app.commands.escAttr(run.label)}">${app.commands.esc(run.label)}${staleBadge}</span>
            <div class="solve-run-card-actions compact-deck-card-actions">
              ${app.commands.buildSolveRunDeleteButton(run)}
            </div>
          </div>
          <div class="comp-deck-card-chips solve-run-card-chips rail-deck-card-chips">
            <div class="solve-run-palette-chips">${chips}</div>
            ${supportChips}
          </div>
          <div class="solve-run-card-meta">
            <button class="solve-run-settings-btn" data-run-id="${app.commands.esc(run.id)}" title="View the settings captured for this run">Settings</button>
            ${stats}
          </div>
        </div>`;
      }
      container.innerHTML = html;

      const selectExportRun = (el) => {
        const runId = el.dataset.exportRunId;
        if (!runId || runId === app.state.export.exportSelectedRunId) return;
        app.state.export.exportSelectedRunId = runId;
        app.commands.renderExportTab();
      };
      container.querySelectorAll(".solve-run-card[data-export-run-id]").forEach((el) => {
        el.addEventListener("click", (e) => {
          if (app.commands.isCardInteractionTarget(e.target)) return;
          selectExportRun(el);
        });
        el.addEventListener("keydown", (e) => {
          if (!["Enter", " "].includes(e.key) || app.commands.isCardInteractionTarget(e.target)) return;
          e.preventDefault();
          selectExportRun(el);
        });
      });

      container.querySelectorAll(".solve-run-delete-btn").forEach((btn) => {
        btn.addEventListener("click", (e) => {
          e.stopPropagation();
          if (btn.disabled) return;
          app.commands.handleSolveRunDeleteClick(btn.dataset.runId);
        });
      });
      app.commands.bindSolveRunCardAuxiliaryInteractions(container, "export");
    }

  function renderExportTab() {
      const exportRun = app.commands.ensureExportRunSelection();
      const exportRecord = app.commands.getSelectedExportRecord(exportRun);
      const exportResult = exportRecord?.result || null;
      const swapInstructions = exportRecord?.swapPlan?.instructions || "";
      app.commands.renderExportRunSidebar();
      app.commands.renderExportSolvePreview(exportRun);
      app.commands.renderExportRecordSelector(exportRun, exportRecord);

      const canExport = !!exportRun && !exportRun.cache_unavailable && app.state.session.apiConnected && !app.state.export.exportRunning;
      app.state.ui.$("#exportFilesBtn").disabled = !canExport;
      app.commands.updateExportFieldScaleState();
      const downloadAllBtn = app.state.ui.$("#downloadAllBtn");
      if (downloadAllBtn && !exportResult?.zip_url) downloadAllBtn.disabled = true;
      const targetLine = app.state.ui.$("#exportTargetLine");
      if (targetLine) {
        targetLine.textContent = exportRun?.cache_unavailable
          ? `${exportRun.label} is no longer available after Prisma restarted. Load a saved run or solve it again before exporting.`
          : exportRun
          ? `Export target: ${exportRun.label} · ${app.commands.describeSolveRunProfile(exportRun).name} · ${app.commands.describeExportPolicy()}`
          : "Select one completed solve run to export.";
      }
      const copySwapBtn = app.state.ui.$("#copySwapBtn");
      if (copySwapBtn) copySwapBtn.disabled = !swapInstructions;

      if (exportResult) app.commands.renderExportResults();
      else {
        const quality = app.state.ui.$("#exportQualityTable");
        const files = app.state.ui.$("#exportFileList");
        if (quality) quality.innerHTML = `<span class="muted-line">Select an export to view its mesh report</span>`;
        if (files) files.innerHTML = `<span class="muted-line">Select an export to view its generated files</span>`;
      }
      if (swapInstructions) app.commands.renderSwapInstructions(swapInstructions);
      else {
        const swapEl = app.state.ui.$("#swapInstructions");
        if (swapEl) swapEl.textContent = exportRecord
          ? "No swap instructions were included with this export"
          : exportRun
            ? "Generate print files to create export-specific swap instructions"
            : "Select a solve run to view swap instructions";
      }
    }

  function getExportMeshQualityEntries() {
    const quality = app.commands.getSelectedExportResult()?.manifest?.quality || {};
    return Object.entries(quality).map(([key, value]) => ({
      key,
      quality: value && typeof value === "object" ? value : {},
    }));
  }

  function exportMeshQualityIssues(entry) {
    const q = entry.quality || {};
    const issues = [];
    const openEdges = Number(q.n_open_edges || 0);
    const pinchEdges = Number(q.n_pinch_edges || 0);
    const faces = Number(q.n_faces || 0);

    if (openEdges > 0 || q.has_holes === true) {
      const openEdgeText = openEdges > 0
        ? `${openEdges.toLocaleString()} open ${openEdges === 1 ? "edge" : "edges"}`
        : "open edges detected";
      issues.push({ severity: "error", text: openEdgeText });
    }
    if (q.is_watertight === false) {
      issues.push({ severity: "error", text: "not watertight" });
    }
    if (q.is_winding_consistent === false) {
      issues.push({ severity: "warn", text: "winding inconsistency" });
    }
    if (pinchEdges > 0) {
      issues.push({ severity: "warn", text: `${pinchEdges.toLocaleString()} pinch ${pinchEdges === 1 ? "edge" : "edges"}` });
    }
    if (Number.isFinite(faces) && faces <= 0) {
      issues.push({ severity: "warn", text: "no mesh faces reported" });
    }
    return issues;
  }

  function exportMeshQualityLabel(entry) {
    const q = entry.quality || {};
    return q.label || entry.key || "mesh object";
  }

  function renderExportChecks() {
    const qualityDiv = app.state.ui.$("#exportQualityTable");
    const exportResult = app.commands.getSelectedExportResult();
    if (!qualityDiv || !exportResult) return;

    const entries = app.commands.getExportMeshQualityEntries();
    if (entries.length === 0) {
      qualityDiv.innerHTML = `
        <div class="export-check-summary is-warn">
          <span class="status-pill warn">Missing</span>
          <span>No per-mesh quality data was found in the export manifest.</span>
        </div>
      `;
      return;
    }

    const checked = entries.map((entry) => ({
      entry,
      issues: app.commands.exportMeshQualityIssues(entry),
    }));
    const failing = checked.filter(({ issues }) => issues.some(issue => issue.severity === "error"));
    const warnings = checked.filter(({ issues }) => issues.length > 0 && !issues.some(issue => issue.severity === "error"));
    const okCount = checked.length - failing.length - warnings.length;

    if (failing.length === 0 && warnings.length === 0) {
      qualityDiv.innerHTML = `
        <div class="export-check-summary is-ok">
          <span class="status-pill ok">OK</span>
          <span>No mesh problems detected across ${entries.length} ${entries.length === 1 ? "object" : "objects"}.</span>
        </div>
      `;
      return;
    }

    const issueRows = [...failing, ...warnings].map(({ entry, issues }) => {
      const severity = issues.some(issue => issue.severity === "error") ? "error" : "warn";
      const pill = severity === "error"
        ? `<span class="status-pill error">Problem</span>`
        : `<span class="status-pill warn">Warning</span>`;
      return `
        <div class="export-check-row is-${severity}">
          <div class="export-check-row-main">
            <span class="export-check-object">${app.commands.esc(app.commands.exportMeshQualityLabel(entry))}</span>
            ${pill}
          </div>
          <div class="export-check-issues">${issues.map(issue => app.commands.esc(issue.text)).join(" · ")}</div>
        </div>
      `;
    }).join("");
    const summaryPill = failing.length > 0
      ? `<span class="status-pill error">Problem</span>`
      : `<span class="status-pill warn">Warning</span>`;
    const summaryText = failing.length > 0
      ? `${failing.length} ${failing.length === 1 ? "object needs" : "objects need"} attention.`
      : `${warnings.length} ${warnings.length === 1 ? "object has" : "objects have"} non-blocking warnings.`;

    qualityDiv.innerHTML = `
      <div class="export-check-summary is-${failing.length > 0 ? "error" : "warn"}">
        ${summaryPill}
        <span>${summaryText} ${okCount > 0 ? `${okCount} ${okCount === 1 ? "object passed" : "objects passed"}.` : ""}</span>
      </div>
      <div class="export-check-list">${issueRows}</div>
    `;
  }

  function exportFileSizeMb(f) {
    const mb = Number(f?.size_mb);
    if (Number.isFinite(mb)) return mb;
    return (Number(f?.size_kb) || 0) / 1024;
  }

  function renderExportResults() {
    const exportRecord = app.commands.getSelectedExportRecord();
    const exportResult = exportRecord?.result || null;
    if (!exportResult) return;

    const files = exportResult.files || [];
    app.commands.renderExportChecks();

    const fileListDiv = app.state.ui.$("#exportFileList");
    const outDir = exportResult.out_dir || "";
    fileListDiv.innerHTML = `
      <div class="export-files-list">
        <div class="export-outdir-row">
          <span class="muted-line">Output:</span>
          <code class="export-outdir-path" title="${app.commands.esc(outDir)}">${app.commands.esc(outDir)}</code>
          <button class="ghost-button xxs open-export-folder-btn" data-export-id="${app.commands.esc(exportResult.export_id || exportRecord.id || "")}" title="Open the export folder">Open Folder</button>
          <button class="ghost-button xxs copy-path-btn" data-copy-path="${app.commands.esc(outDir)}" title="Copy folder path">Copy Path</button>
        </div>
        ${files.map((f) => {
          const href = f.url || app.api.exportFileUrl(f.name, exportRecord.id);
          const absPath = f.abs_path || (outDir ? `${outDir}\\${f.name}` : f.name);
          return `
          <div class="export-file-row">
            <span class="file-name" title="${app.commands.esc(f.name)}">${app.commands.esc(f.name)}</span>
            <span class="file-size">${app.commands.exportFileSizeMb(f).toFixed(2)} MB</span>
            <div class="export-file-actions">
              <a class="ghost-button xxs export-file-download" href="${app.commands.esc(href)}" download="${app.commands.esc(f.name)}" title="Download this file">Download</a>
              <button class="ghost-button xxs copy-path-btn" data-copy-path="${app.commands.esc(absPath)}" title="Copy file path">Copy Path</button>
            </div>
          </div>
        `;
        }).join("")}
      </div>
    `;

    // Enable Download All when we have files + a zip URL
    const dlAllBtn = app.state.ui.$("#downloadAllBtn");
    if (dlAllBtn) {
      const hasZip = files.length > 0 && !!exportResult.zip_url;
      dlAllBtn.disabled = !hasZip;
      dlAllBtn.title = hasZip ? "Download all export files as a ZIP" : "Export files first";
    }
  }

  async function copyToClipboard(text) {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
      } else {
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.left = "-9999px";
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        document.body.removeChild(ta);
      }
      app.commands.showToast("Path copied", "success");
    } catch (err) {
      app.commands.showToast(`Copy failed: ${err.message}`, "error");
    }
  }

  function renderSwapInstructions(instructions = app.commands.getSelectedSwapInstructions()) {
      app.state.ui.$("#swapInstructions").textContent = instructions || "No swap instructions available";
    }

  async function handleExportFiles() {
      const btn = app.state.ui.$("#exportFilesBtn");
      const exportRun = app.commands.getExportSelectedRun();
      if (!btn || !exportRun || exportRun.cache_unavailable || app.state.export.exportRunning) return;
      const originatingRunId = exportRun.id;
      const requestedPolicy = Object.freeze({
        geometrySource: app.state.ui.$("#exportGeometrySource")?.value || "field_derived",
        fieldScale: parseInt(app.state.ui.$("#exportFieldScale")?.value || "4", 10) || 4,
        outputFormat: app.state.ui.$("#exportOutputFormat")?.value || "3mf",
      });
      const validateWrittenMeshes = false; // Reload-validation control removed (Stage 12b); defaults off
      const cardId = exportRun.results.card_id || exportRun.id;
      if (requestedPolicy.geometrySource === "field_derived" && requestedPolicy.fieldScale > 4) {
        const confirmed = await app.commands.appConfirm(
          `${requestedPolicy.fieldScale}x mesh detail can take much longer and produce a much larger export file. Continue?`,
          { title: "High Mesh Detail", ok: "Generate", cancel: "Cancel" },
        );
        if (!confirmed) return;
      }

      app.state.export.exportRunning = true;
      app.state.export.activeExportRunId = originatingRunId;
      app.state.export.activeExportJobId = "";
      app.state.export.exportCancelPending = false;
      app.commands.updateSolveReadiness();
      app.commands.updateExportFieldScaleState();
      btn.disabled = true;
      btn.textContent = "Exporting...";
      app.commands.startProgress("Starting export...", "export");

      let pollingOwner = null;
      try {
        const started = await app.api.startExportPrintFiles({
          geometrySource: requestedPolicy.geometrySource,
          fieldScale: requestedPolicy.fieldScale,
          outputFormat: requestedPolicy.outputFormat,
          validateWrittenMeshes,
          cardId,
        });
        app.state.export.activeExportJobId = String(started?.job_id || "");
        if (!app.state.export.activeExportJobId) throw new Error("Export did not return a job id.");
        app.commands.renderExportCancellationState();
        const pollingJobId = app.state.export.activeExportJobId;
        if (app.state.export.exportPollingOwner) app.state.export.exportPollingOwner.cancelled = true;
        pollingOwner = { jobId: pollingJobId, cancelled: false };
        app.state.export.exportPollingOwner = pollingOwner;
        const status = await app.services.pollJobUntilTerminal({
          jobId: pollingJobId,
          fetchStatus: () => app.api.getExportStatus(),
          isTerminal: (next) => !["running", "cancelling"].includes(next.status),
          shouldContinue: () => (
            app.state.export.exportRunning
            && app.state.export.activeExportJobId === pollingJobId
            && !pollingOwner.cancelled
            && app.state.export.exportPollingOwner === pollingOwner
          ),
          intervalMs: 500,
          onStatus: (next) => app.commands.updateOperationProgressFromStatus(next, "Exporting files..."),
          onTransientError: () => app.commands.updateOperationProgressFromStatus(
            { status: "running", progress: "Connection interrupted; retrying export status..." },
            "Exporting files...",
          ),
        });
        if (!status) return;
        if (status.status === "complete" && status.result) {
          const originatingRun = app.state.solve.solveRuns.find((run) => run.id === originatingRunId) || null;
          app.commands.appendExportRecordToRun(originatingRun, status.result, Date.now(), status.elapsed_s);
        } else if (status.status === "cancelled") {
          const cancelled = new Error("Export cancelled");
          cancelled.name = "AbortError";
          throw cancelled;
        } else {
          throw new Error(status.progress || "Export failed");
        }
        app.commands.showToast("Export complete!", "success");

        app.commands.renderExportTab();
        app.commands.updateRail();
      } catch (err) {
        if (err.name === "AbortError") return;
        if (pollingOwner && app.state.export.exportPollingOwner !== pollingOwner) return;
        if (/No cached solve found/i.test(String(err?.message || ""))) {
          const staleRun = app.state.solve.solveRuns.find((run) => run.id === originatingRunId);
          if (staleRun) staleRun.cache_unavailable = true;
          app.commands.showToast("This solve is no longer available after Prisma restarted. Load a saved run or solve it again before exporting.", "warn");
          app.commands.renderExportTab();
          return;
        }
        const prefix = err.name === "JobPollingIdentityError"
          ? "Export status could not be verified"
          : "Export failed";
        app.commands.showToast(`${prefix}: ${err.message}`, "error");
      } finally {
        if (pollingOwner && app.state.export.exportPollingOwner !== pollingOwner) return;
        if (app.state.export.exportPollingOwner === pollingOwner) app.state.export.exportPollingOwner = null;
        app.state.export.exportRunning = false;
        if (app.state.export.activeExportRunId === originatingRunId) app.state.export.activeExportRunId = null;
        app.state.export.activeExportJobId = "";
        app.state.export.exportCancelPending = false;
        app.commands.stopProgress();
        btn.textContent = "Generate Print Files";
        const currentExportRun = app.commands.getExportSelectedRun();
        btn.disabled = !currentExportRun || currentExportRun.cache_unavailable || !app.state.session.apiConnected;
        app.commands.updateExportFieldScaleState();
        app.commands.updateSolveReadiness();
      }
    }

  function modulesForSlot(slot) {
    return (app.state.settings.moduleData || []).filter((m) => m.slot === slot);
  }

  function moduleDisplayName(mod) {
    if (!mod) return "";
    const display = app.state.ui.MODULE_DISPLAY[mod.name];
    if (display?.label) return display.label;
    return String(mod.name || "")
      .replace(/^[a-z]\d_/, "")
      .replace(/_/g, " ")
      .replace(/\b\w/g, (ch) => ch.toUpperCase());
  }

  function moduleDisplayTooltip(mod) {
    if (!mod) return "";
    return app.state.ui.MODULE_DISPLAY[mod.name]?.tooltip || mod.description || mod.name || "";
  }

  function moduleParamStorageKey(moduleId, param) {
    return param?.storage_key || param?.name;
  }

  function moduleDescriptorById(moduleId) {
    return (app.state.settings.moduleData || []).find((mod) => mod.name === moduleId) || null;
  }

  function getModuleParamValue(configValues, moduleId, param) {
    const module = app.commands.moduleDescriptorById(moduleId);
    if (module?.slot === "preprocessing") {
      const block = (configValues?.preprocessing_params || {})[moduleId] || {};
      if (Object.prototype.hasOwnProperty.call(block, param.name)) {
        return block[param.name];
      }
      return param.default;
    }
    const storageKey = app.commands.moduleParamStorageKey(moduleId, param);
    if (Object.prototype.hasOwnProperty.call(configValues || {}, storageKey)) {
      return configValues[storageKey];
    }
    return param.default;
  }

  Object.assign(app.commands, {
    _recipeKeyFromEntries,
    buildRecipeIdentityMap,
    buildDiscreteLabelBoundaryMask,
    ensureRecipeArtifactData,
    ensureRecipeCookbook,
    ensureRecipeData,
    recipePct,
    recipeFilamentLabel,
    recipeFilamentChip,
    recipeKeysForNode,
    renderRecipeTree,
    partitionRecipeTail,
    renderRecipeBucket,
    highlightRecipeRegions,
    recipeReadoutHtml,
    openRecipeLightbox,
    solveFilamentLabel,
    buildUnsolvablePaletteMessage,
    handleStartSolve,
    handleCancelSolve,
    startSolvePolling,
    getCompletedExportRuns,
    getExportSelectedRun,
    ensureSolveRunExportState,
    getRunExportRecords,
    createExportRecord,
    appendExportRecordToRun,
    selectRunExportRecord,
    getSelectedExportRecord,
    getSelectedExportResult,
    getSelectedSwapInstructions,
    updateExportFieldScaleState,
    formatExportGeometrySourceLabel,
    formatExportOutputFormatLabel,
    formatExportFieldScaleLabel,
    getExportSolvePreviewUrl,
    getExportSolveDimensions,
    formatExportDimensionMm,
    renderExportSolvePreview,
    formatExportRecordTime,
    renderExportRecordSelector,
    describeExportPolicy,
    handleExportOptionChange,
    ensureExportRunSelection,
    renderExportRunSidebar,
    renderExportTab,
    getExportMeshQualityEntries,
    exportMeshQualityIssues,
    exportMeshQualityLabel,
    renderExportChecks,
    exportFileSizeMb,
    renderExportResults,
    copyToClipboard,
    renderSwapInstructions,
    handleExportFiles,
    modulesForSlot,
    moduleDisplayName,
    moduleDisplayTooltip,
    moduleParamStorageKey,
    moduleDescriptorById,
    getModuleParamValue,
  });}
