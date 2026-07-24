/**
 * Install the image/index feature commands.
 * @param {import("../../core/types.js").ApplicationContext} app
 */
export function installFeaturesImageIndex(app) {
  async function refreshImageLibrary({ announce = false } = {}) {
    if (!app.state.session.apiConnected) {
      app.commands.showToast("Start the server to refresh the image library", "warn");
      return;
    }

    const selectedBeforeRefresh = app.state.image.selectedImage?.filename || null;
    const previousFilename = selectedBeforeRefresh || app.state.image.pendingSelectedFilename || null;
    app.state.image.availableImages = await app.api.fetchImages();
    const refreshedSelection = previousFilename
      ? app.state.image.availableImages.find((img) => img.filename === previousFilename) || null
      : null;
    const selectionWasRemoved = !!previousFilename && !refreshedSelection;
    app.state.image.selectedImage = refreshedSelection;
    if (refreshedSelection) app.state.image.pendingSelectedFilename = null;

    app.commands.renderImageTab();
    app.commands.updateRail();

    if (selectionWasRemoved && selectedBeforeRefresh) {
      await app.commands.syncConfigToServer({ showErrorToast: true });
      app.commands.showToast(`Removed missing image "${previousFilename}" from the current setup`, "warn");
    } else if (announce) {
      const count = app.state.image.availableImages.length;
      app.commands.showToast(`Image library refreshed (${count} ${count === 1 ? "image" : "images"})`, "success");
    }
  }

  function renderImageGrid() {
    const grid = app.state.ui.$("#imageGrid");

    if (app.state.image.availableImages.length === 0) {
      grid.innerHTML = `<p class="muted-line" style="text-align:center;padding:20px 0">
        ${app.state.session.apiConnected ? "No images found" : "Connect to server"}
      </p>`;
      return;
    }
    grid.innerHTML = app.state.image.availableImages.map((img) => {
      const sizeKb = img.size_kb || 0;
      const sizeStr = sizeKb >= 1024 ? (sizeKb / 1024).toFixed(1) + " MB" : sizeKb.toFixed(0) + " KB";
      return `<div class="image-card${app.state.image.selectedImage?.filename === img.filename ? " is-selected" : ""}"
           data-filename="${img.filename}" draggable="true">
        <img class="image-card-thumb" src="${app.api.imagePreviewUrl(img.filename)}" alt="${img.filename}"
             loading="lazy" onerror="this.style.display='none'">
        <div class="image-card-info">
          <strong>${app.commands.esc(img.filename)}</strong>
          ${img.width}&times;${img.height} &middot; ${sizeStr}
        </div>
      </div>`;
    }).join("");

    grid.querySelectorAll(".image-card").forEach((card) => {
      card.addEventListener("click", () => {
        const filename = card.dataset.filename;
        const newImage = app.state.image.availableImages.find((i) => i.filename === filename) || null;
        const isNewImage = newImage?.filename !== app.state.image.selectedImage?.filename;
        if (isNewImage) {
          app.state.image.frameState.scale = 100.0;
          app.state.image.frameState.rotation = 0;
          app.state.image.frameState.panX = 0;
          app.state.image.frameState.panY = 0;
          app.state.image.frameState.flipH = false;
          app.state.image.frameState.flipV = false;
        }
        app.state.image.selectedImage = newImage;
        if (newImage && isNewImage) {
          app.commands.applyImageAspectDefault();              // Stage 11: default to image aspect, short side 120mm
        } else if (app.state.image.frameState.arMode === "image" && newImage) {
          app.commands.applyARFromLastTouched();
        }
        app.commands.renderImageTab();
        app.commands.updateRail();
      });
      card.addEventListener("dragstart", (e) => {
        e.dataTransfer.setData("application/json", JSON.stringify({
          type: "image", filename: card.dataset.filename,
        }));
        e.dataTransfer.effectAllowed = "copy";
      });
    });
  }

  function bindImageLibraryWheelScroll() {
    const grid = app.state.ui.$("#imageGrid");
    if (!grid || grid._horizontalWheelScrollAttached) return;
    grid.addEventListener("wheel", (e) => {
      if (app.state.ui.currentTab !== "image" || e.ctrlKey) return;
      if (grid.scrollWidth <= grid.clientWidth + 1) return;
      const delta = Math.abs(e.deltaY) >= Math.abs(e.deltaX) ? e.deltaY : e.deltaX;
      if (!delta) return;
      const before = grid.scrollLeft;
      grid.scrollLeft += delta;
      if (grid.scrollLeft !== before) e.preventDefault();
    }, { passive: false });
    grid._horizontalWheelScrollAttached = true;
  }

  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

  function getEffectiveAR() {
    const m = app.state.image.frameState.arMode;
    if (m === "specified") return app.state.image.frameState.widthMm / app.state.image.frameState.heightMm;
    if (m === "image") {
      if (app.state.image.selectedImage) {
        const bounds = app.commands.getTransformedSourceBounds();
        return bounds.width / bounds.height;
      }
      return app.state.image.frameState.widthMm / app.state.image.frameState.heightMm;  // fallback if no image
    }
    if (m === "ratio") {
      const raw = app.state.image.frameState.customRatio.x / app.state.image.frameState.customRatio.y;
      return app.state.image.imageDirection === "portrait" ? 1 / raw : raw;
    }
    if (m === "1:1") return 1;
    const [a, b] = m.split(":").map(Number);
    const raw = a / b;
    return app.state.image.imageDirection === "portrait" ? 1 / raw : raw;
  }

  function openRatioDialog() {
    const dialog = app.state.ui.$("#ratioDialog");
    if (!dialog) return;
    dialog.setAttribute("aria-hidden", "false");
    const rx = app.state.ui.$("#ratioDialogX");
    const ry = app.state.ui.$("#ratioDialogY");
    if (rx) rx.value = app.state.image.frameState.customRatio.x;
    if (ry) ry.value = app.state.image.frameState.customRatio.y;
    if (rx) rx.focus();
  }

  function closeRatioDialog() {
    app.state.ui.$("#ratioDialog")?.setAttribute("aria-hidden", "true");
  }

  function rotatedBounds(width, height, rotationDeg) {
    const rad = Math.abs(rotationDeg || 0) * Math.PI / 180;
    const cosR = Math.abs(Math.cos(rad));
    const sinR = Math.abs(Math.sin(rad));
    return {
      width: width * cosR + height * sinR,
      height: width * sinR + height * cosR,
    };
  }

  function cropCoverImageGeometry(frameW, frameH, imgW, imgH, scalePct, rotationDeg) {
    const rotated = app.commands.rotatedBounds(imgW, imgH, rotationDeg);
    const baseScale = Math.max(
      frameW / Math.max(rotated.width, 1),
      frameH / Math.max(rotated.height, 1),
    );
    const zoom = Math.max(1, (Number(scalePct) || 100) / 100);
    const imgScale = baseScale * zoom;
    const displayW = imgW * imgScale;
    const displayH = imgH * imgScale;
    const visualW = rotated.width * imgScale;
    const visualH = rotated.height * imgScale;
    return { imgScale, displayW, displayH, visualW, visualH };
  }

  function getTransformedSourceBounds() {
    if (!app.state.image.selectedImage) return { width: 1, height: 1 };
    return app.commands.rotatedBounds(app.state.image.selectedImage.width, app.state.image.selectedImage.height, app.state.image.frameState.rotation);
  }

  function largestContainedCrop(bounds, aspect) {
    const safeAspect = Math.max(Number(aspect) || 1, 1e-9);
    const boundsAR = bounds.width / Math.max(bounds.height, 1e-9);
    if (boundsAR >= safeAspect) {
      return { width: bounds.height * safeAspect, height: bounds.height };
    }
    return { width: bounds.width, height: bounds.width / safeAspect };
  }

  function cropModelFromFrameState() {
    const bounds = app.commands.getTransformedSourceBounds();
    const aspect = app.commands.getEffectiveAR();
    const base = app.commands.largestContainedCrop(bounds, aspect);
    const zoom = Math.max(1, (Number(app.state.image.frameState.scale) || 100) / 100);
    const width = Math.max(1, base.width / zoom);
    const height = Math.max(1, base.height / zoom);
    const slackX = Math.max(0, bounds.width - width);
    const slackY = Math.max(0, bounds.height - height);
    const panX = slackX <= 1e-6 ? 0 : app.commands.clamp(Number(app.state.image.frameState.panX) || 0, -1, 1);
    const panY = slackY <= 1e-6 ? 0 : app.commands.clamp(Number(app.state.image.frameState.panY) || 0, -1, 1);
    app.state.image.frameState.panX = panX;
    app.state.image.frameState.panY = panY;
    return {
      bounds,
      aspect,
      width,
      height,
      left: (bounds.width - width) / 2 + panX * slackX / 2,
      top: (bounds.height - height) / 2 + panY * slackY / 2,
      slackX,
      slackY,
      scalePxPerSource: 1,
    };
  }

  function projectCropToFrame(frameL, frameT, frameW, frameH) {
    const crop = app.commands.cropModelFromFrameState();
    const sourceScale = frameW / Math.max(crop.width, 1e-9);
    const visualW = crop.bounds.width * sourceScale;
    const visualH = crop.bounds.height * sourceScale;
    const displayW = app.state.image.selectedImage.width * sourceScale;
    const displayH = app.state.image.selectedImage.height * sourceScale;
    const visualLeft = frameL - crop.left * sourceScale;
    const visualTop = frameT - crop.top * sourceScale;
    const visualCenterX = visualLeft + visualW / 2;
    const visualCenterY = visualTop + visualH / 2;
    return {
      crop,
      imgScale: sourceScale,
      displayW,
      displayH,
      visualW,
      visualH,
      imgL: visualCenterX - displayW / 2,
      imgT: visualCenterY - displayH / 2,
      visualLeft,
      visualTop,
      visualRight: visualLeft + visualW,
      visualBottom: visualTop + visualH,
    };
  }

  function projectDragCropToFrame() {
    const snap = app.state.image.frameDragState?.projection;
    if (!snap) return null;
    return {
      crop: app.commands.cropModelFromFrameState(),
      imgScale: snap.imgScale,
      displayW: snap.displayW,
      displayH: snap.displayH,
      visualW: snap.visualW,
      visualH: snap.visualH,
      imgL: snap.imgL,
      imgT: snap.imgT,
      visualLeft: snap.visualLeft,
      visualTop: snap.visualTop,
      visualRight: snap.visualRight,
      visualBottom: snap.visualBottom,
    };
  }

  function resetCropToFitSource() {
    app.state.image.frameState.scale = 100.0;
    app.state.image.frameState.panX = 0;
    app.state.image.frameState.panY = 0;
  }

  function roundFrameMm(value) {
    return Math.round(Number(value) * 100) / 100;
  }

  function applyCropModel(targetCrop, { anchor = "height" } = {}) {
    if (!app.state.image.selectedImage) return false;
    const bounds = app.commands.getTransformedSourceBounds();
    let targetW = app.commands.clamp(Number(targetCrop.width) || bounds.width, 1, bounds.width);
    let targetH = app.commands.clamp(Number(targetCrop.height) || bounds.height, 1, bounds.height);
    let targetLeft = app.commands.clamp(Number(targetCrop.left) || 0, 0, Math.max(0, bounds.width - targetW));
    let targetTop = app.commands.clamp(Number(targetCrop.top) || 0, 0, Math.max(0, bounds.height - targetH));
    let aspect = targetW / Math.max(targetH, 1e-9);

    if (anchor === "height") {
      const heightMm = app.commands.clampFrameHeight(app.state.image.frameState.heightMm);
      const widthMm = app.commands.clampFrameWidth(heightMm * aspect);
      const actualAspect = widthMm / Math.max(heightMm, 1e-9);
      if (Math.abs(actualAspect - aspect) > 1e-6) {
        const centerX = targetLeft + targetW / 2;
        targetW = app.commands.clamp(targetH * actualAspect, 1, bounds.width);
        targetLeft = app.commands.clamp(centerX - targetW / 2, 0, Math.max(0, bounds.width - targetW));
        aspect = actualAspect;
      }
      app.state.image.frameState.widthMm = app.commands.roundFrameMm(widthMm);
      app.state.image.frameState.heightMm = app.commands.roundFrameMm(heightMm);
      app.state.image.lastTouchedDim = "width";
    } else {
      const widthMm = app.commands.clampFrameWidth(app.state.image.frameState.widthMm);
      const heightMm = app.commands.clampFrameHeight(widthMm / Math.max(aspect, 1e-9));
      const actualAspect = widthMm / Math.max(heightMm, 1e-9);
      if (Math.abs(actualAspect - aspect) > 1e-6) {
        const centerY = targetTop + targetH / 2;
        targetH = app.commands.clamp(targetW / Math.max(actualAspect, 1e-9), 1, bounds.height);
        targetTop = app.commands.clamp(centerY - targetH / 2, 0, Math.max(0, bounds.height - targetH));
        aspect = actualAspect;
      }
      app.state.image.frameState.widthMm = app.commands.roundFrameMm(widthMm);
      app.state.image.frameState.heightMm = app.commands.roundFrameMm(heightMm);
      app.state.image.lastTouchedDim = "height";
    }

    app.state.image.frameState.arMode = "specified";
    const base = app.commands.largestContainedCrop(bounds, aspect);
    const zoom = Math.max(
      1,
      Math.min(base.width / Math.max(targetW, 1e-9), base.height / Math.max(targetH, 1e-9)),
    );
    app.state.image.frameState.scale = app.commands.clamp(zoom * 100, 100, 1000);

    const actualW = base.width / Math.max(app.state.image.frameState.scale / 100, 1e-9);
    const actualH = base.height / Math.max(app.state.image.frameState.scale / 100, 1e-9);
    const slackX = Math.max(0, bounds.width - actualW);
    const slackY = Math.max(0, bounds.height - actualH);
    app.state.image.frameState.panX = slackX <= 1e-6
      ? 0
      : app.commands.clamp((targetLeft - (bounds.width - actualW) / 2) * 2 / slackX, -1, 1);
    app.state.image.frameState.panY = slackY <= 1e-6
      ? 0
      : app.commands.clamp((targetTop - (bounds.height - actualH) / 2) * 2 / slackY, -1, 1);
    return true;
  }

  function fitFrameToSourceWidth() {
    if (app.state.image.widthLocked || !app.state.image.selectedImage) return;
    const current = app.commands.cropModelFromFrameState();
    app.commands.applyCropModel({
      left: 0,
      top: current.top,
      width: current.bounds.width,
      height: current.height,
    }, { anchor: "height" });
  }

  function fitFrameToSourceHeight() {
    if (app.state.image.heightLocked || !app.state.image.selectedImage) return;
    const current = app.commands.cropModelFromFrameState();
    app.commands.applyCropModel({
      left: current.left,
      top: 0,
      width: current.width,
      height: current.bounds.height,
    }, { anchor: "width" });
  }

  function finishFrameModelUpdate({ syncServer = true } = {}) {
    app.commands.syncDimFields();
    app.commands.syncWidthSlider();
    app.commands.syncHeightSlider();
    app.commands.syncScaleSlider();
    app.commands.updateARButtons();
    app.commands.renderFrameCanvas();
    app.commands.updateInfoGrid();
    if (syncServer) app.commands.syncConfigToServer();
  }

  function _ensureSvgFilter() {
    if (document.getElementById("imgAdjustSVG")) return;
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("width", "0");
    svg.setAttribute("height", "0");
    svg.style.position = "absolute";
    const defs = document.createElementNS(ns, "defs");
    const filter = document.createElementNS(ns, "filter");
    filter.setAttribute("id", "imgAdjustSVG");
    filter.setAttribute("color-interpolation-filters", "sRGB");
    const ct = document.createElementNS(ns, "feComponentTransfer");
    ct.setAttribute("id", "imgAdjustCT");
    for (const ch of ["R", "G", "B"]) {
      const func = document.createElementNS(ns, `feFunc${ch}`);
      func.setAttribute("id", `imgAdjustFunc${ch}`);
      func.setAttribute("type", "table");
      func.setAttribute("tableValues", "0 0.25 0.5 0.75 1");
      ct.appendChild(func);
    }
    filter.appendChild(ct);
    defs.appendChild(filter);
    svg.appendChild(defs);
    document.body.appendChild(svg);
  }

  function _updateSvgFilter() {
    app.commands._ensureSvgFilter();
    const hl = app.state.image.imageAdjust.highlight;   // -1..1
    const sh = app.state.image.imageAdjust.shadow;      // -1..1
    const temp = app.state.image.imageAdjust.temperature / 100;  // -1..1

    // Build a 5-point table transfer curve per channel.
    // Points at t = 0, 0.25, 0.5, 0.75, 1.0
    // Shadow adjustment shifts the dark end (t=0, 0.25)
    // Highlight adjustment shifts the bright end (t=0.75, 1.0)
    // Temperature shifts R up/B down (warm) or R down/B up (cool)
    for (const ch of ["R", "G", "B"]) {
      // Per-channel temperature bias (warm = boost R, cut B)
      let bias = 0;
      if (ch === "R") bias = temp * 0.25;
      if (ch === "B") bias = -temp * 0.25;

      // Identity curve + adjustments
      // Shadow slider shifts dark end (p0, p1); highlight shifts bright end (p3, p4)
      const p0 = Math.max(0, Math.min(1, 0.0  + sh * 0.30 + bias));
      const p1 = Math.max(0, Math.min(1, 0.25 + sh * 0.18 + bias));
      const p2 = Math.max(0, Math.min(1, 0.50 + bias));
      const p3 = Math.max(0, Math.min(1, 0.75 + hl * 0.18 + bias));
      const p4 = Math.max(0, Math.min(1, 1.0  + hl * 0.30 + bias));

      const func = document.getElementById(`imgAdjustFunc${ch}`);
      if (func) func.setAttribute("tableValues",
        `${p0.toFixed(4)} ${p1.toFixed(4)} ${p2.toFixed(4)} ${p3.toFixed(4)} ${p4.toFixed(4)}`);
    }
  }

  function buildAdjustFilterCSS() {
    const parts = [];
    if (app.state.image.imageAdjust.mode === "bw") parts.push("grayscale(100%)");
    if (app.state.image.imageAdjust.exposure !== 0) parts.push(`brightness(${1 + app.state.image.imageAdjust.exposure})`);
    if (app.state.image.imageAdjust.contrast !== 0) parts.push(`contrast(${1 + app.state.image.imageAdjust.contrast})`);
    // Tint: approximate with sepia base + hue rotation + strength via saturation blend
    if (app.state.image.imageAdjust.tint_strength > 0) {
      parts.push(`sepia(${app.state.image.imageAdjust.tint_strength})`);
      parts.push(`hue-rotate(${app.state.image.imageAdjust.tint_hue}deg)`);
    }
    if (app.state.image.imageAdjust.saturation !== 0) parts.push(`saturate(${1 + app.state.image.imageAdjust.saturation})`);

    // SVG filter for highlights, shadows, temperature (tone curve + channel bias)
    const needsSvg = app.state.image.imageAdjust.highlight !== 0
                  || app.state.image.imageAdjust.shadow !== 0
                  || app.state.image.imageAdjust.temperature !== 0;
    if (needsSvg) {
      app.commands._updateSvgFilter();
      parts.push("url(#imgAdjustSVG)");
    }

    return parts.join(" ") || "none";
  }

  function renderFrameCanvas() {
    const canvas = app.state.ui.$("#frameCanvas");
    const img = app.state.ui.$("#frameImage");
    const placeholder = app.state.ui.$("#framePlaceholder");
    const mask = app.state.ui.$("#frameMask");
    const win = app.state.ui.$("#frameWindow");
    if (!canvas) return;

    // Toggle specified-mode class for edge handles
    canvas.classList.toggle("specified-mode", app.state.image.frameState.arMode === "specified");

    if (!app.state.image.selectedImage) {
      img.style.display = "none";
      placeholder.style.display = "";
      mask.style.display = "none";
      win.style.display = "none";
      return;
    }

    placeholder.style.display = "none";
    img.style.display = "";
    mask.style.display = "";
    win.style.display = "";

    // Load image if src changed
    const url = app.api.imagePreviewUrl(app.state.image.selectedImage.filename);
    if (img.src !== url && !img.src.endsWith(url)) {
      img.src = url;
    }

    const doLayout = () => {
      const canvasRect = canvas.getBoundingClientRect();
      const cW = canvasRect.width;
      const cH = canvasRect.height;
      if (cW === 0 || cH === 0) return;

      const ar = app.commands.getEffectiveAR();
      const CANVAS_PAD_FRAC = 0.05; // visual breathing room inside the editor

      // Compute content frame size to fit within the editor.
      const availW = cW * (1 - 2 * CANVAS_PAD_FRAC);
      const availH = cH * (1 - 2 * CANVAS_PAD_FRAC);
      const dragProjection = app.state.image.frameDragState?.projection;
      let frameW, frameH;
      let frameL, frameT;
      if (dragProjection) {
        frameW = app.state.image.frameState.widthMm * dragProjection.pxPerMm;
        frameH = app.state.image.frameState.heightMm * dragProjection.pxPerMm;
        frameL = dragProjection.centerX - frameW / 2;
        frameT = dragProjection.centerY - frameH / 2;
      } else if (availW / availH > ar) {
        frameH = availH;
        frameW = frameH * ar;
        frameL = (cW - frameW) / 2;
        frameT = (cH - frameH) / 2;
      } else {
        frameW = availW;
        frameH = frameW / ar;
        frameL = (cW - frameW) / 2;
        frameT = (cH - frameH) / 2;
      }

      // Position frame window
      win.style.left = frameL + "px";
      win.style.top = frameT + "px";
      win.style.width = frameW + "px";
      win.style.height = frameH + "px";

      // Mask: use clip-path to cut out only the selected content frame.
      // There is no generated fill region in crop-only framing.
      mask.style.clipPath = `polygon(
        0% 0%, 100% 0%, 100% 100%, 0% 100%, 0% 0%,
        ${frameL}px ${frameT}px,
        ${frameL}px ${frameT + frameH}px,
        ${frameL + frameW}px ${frameT + frameH}px,
        ${frameL + frameW}px ${frameT}px,
        ${frameL}px ${frameT}px
      )`;

      // Position image: the crop model owns source-space crop semantics; the
      // editor only projects that model into CSS pixels.
      const rotation = app.state.image.frameState.rotation;
      const projection = app.state.image.frameDragState?.projection
        ? app.commands.projectDragCropToFrame()
        : app.commands.projectCropToFrame(frameL, frameT, frameW, frameH);
      const displayW = projection.displayW;
      const displayH = projection.displayH;
      const imgL = projection.imgL;
      const imgT = projection.imgT;

      img.style.left = imgL + "px";
      img.style.top = imgT + "px";
      img.style.width = displayW + "px";
      img.style.height = displayH + "px";
      const sx = app.state.image.frameState.flipH ? -1 : 1;
      const sy = app.state.image.frameState.flipV ? -1 : 1;
      img.style.transform = `rotate(${rotation}deg) scale(${sx}, ${sy})`;
      img.style.filter = app.commands.buildAdjustFilterCSS();

      // Position edge handles — square handles at corners and edge midpoints
      const hs = 5; // half handle size
      canvas.querySelectorAll(".frame-edge").forEach((el) => {
        const edge = el.dataset.edge;
        // Edge midpoints
        if (edge === "n")  { el.style.left = (frameL + frameW / 2 - hs) + "px"; el.style.top = (frameT - hs) + "px"; }
        if (edge === "s")  { el.style.left = (frameL + frameW / 2 - hs) + "px"; el.style.top = (frameT + frameH - hs) + "px"; }
        if (edge === "e")  { el.style.left = (frameL + frameW - hs) + "px"; el.style.top = (frameT + frameH / 2 - hs) + "px"; }
        if (edge === "w")  { el.style.left = (frameL - hs) + "px"; el.style.top = (frameT + frameH / 2 - hs) + "px"; }
        // Corners
        if (edge === "nw") { el.style.left = (frameL - hs) + "px"; el.style.top = (frameT - hs) + "px"; }
        if (edge === "ne") { el.style.left = (frameL + frameW - hs) + "px"; el.style.top = (frameT - hs) + "px"; }
        if (edge === "sw") { el.style.left = (frameL - hs) + "px"; el.style.top = (frameT + frameH - hs) + "px"; }
        if (edge === "se") { el.style.left = (frameL + frameW - hs) + "px"; el.style.top = (frameT + frameH - hs) + "px"; }
      });

      // Store frame geometry for interaction handlers
      canvas._frameGeom = {
        frameL,
        frameT,
        frameW,
        frameH,
        cW,
        cH,
        imgScale: projection.imgScale,
        displayW,
        displayH,
        visualW: projection.visualW,
        visualH: projection.visualH,
        visualLeft: projection.visualLeft,
        visualTop: projection.visualTop,
        visualRight: projection.visualRight,
        visualBottom: projection.visualBottom,
        imgL,
        imgT,
        crop: projection.crop,
      };

      // Dimension annotations
      app.commands.renderDimensionAnnotations(frameL, frameT, frameW, frameH);
    };

    if (img.complete && img.naturalWidth > 0) doLayout();
    else img.addEventListener("load", doLayout, { once: true });

    // Keep preview in sync
    app.commands.renderPreview();
  }

  function renderPreview() {
    const viewport = app.state.ui.$("#previewViewport");
    const placeholder = app.state.ui.$("#previewPlaceholder");
    const mat = app.state.ui.$("#previewMat");
    const pImg = app.state.ui.$("#previewImg");
    if (!viewport || !pImg) return;

    if (!app.state.image.selectedImage) {
      pImg.style.display = "none";
      if (placeholder) placeholder.style.display = "";
      return;
    }

    if (placeholder) placeholder.style.display = "none";
    pImg.style.display = "";

    const url = app.api.imagePreviewUrl(app.state.image.selectedImage.filename);
    if (pImg.src !== url && !pImg.src.endsWith(url)) {
      pImg.src = url;
    }

    const doPreview = () => {
      const vw = viewport.clientWidth;
      const vh = viewport.clientHeight;
      if (!vw || !vh) return;

      const ar = app.commands.getEffectiveAR();
      const imgNatW = pImg.naturalWidth || app.state.image.selectedImage.width;
      const imgNatH = pImg.naturalHeight || app.state.image.selectedImage.height;

      // Fit total print footprint (image frame + border) within viewport
      const margin = 0.12;
      const availW = vw * (1 - margin * 2);
      const availH = vh * (1 - margin * 2);

      // Compute border ratio: border_mm / frame_width_mm gives relative border size
      const bwMm = (app.state.settings.config.border && app.state.settings.config.border_width_mm > 0) ? app.state.settings.config.border_width_mm : 0;
      // Total footprint AR includes border: (W + 2*bw) / (H + 2*bw)
      const footW = app.state.image.frameState.widthMm + 2 * bwMm;
      const footH = app.state.image.frameState.heightMm + 2 * bwMm;
      const footAR = footW / footH;

      // Fit the full footprint within available space
      let totalW, totalH;
      if (availW / availH > footAR) {
        totalH = availH;
        totalW = totalH * footAR;
      } else {
        totalW = availW;
        totalH = totalW / footAR;
      }

      // Derive image frame and border sizes in pixels from the fitted total
      // Round to whole CSS pixels to avoid sub-pixel alignment shifts
      const borderPx = bwMm > 0 ? Math.max(2, Math.round(totalW * (bwMm / footW))) : 0;
      const frameW = Math.round(totalW) - 2 * borderPx;
      const frameH = Math.round(totalH) - 2 * borderPx;
      const borderEl = app.state.ui.$("#previewBorder");

      // Mat = full print footprint (image frame + border on all sides)
      const matW = frameW + 2 * borderPx;
      const matH = frameH + 2 * borderPx;
      mat.style.width = matW + "px";
      mat.style.height = matH + "px";
      mat.style.overflow = "hidden";
      mat.style.position = "relative";
      mat.style.background = "#000";
      mat.style.outline = "1px solid rgba(255,255,255,0.2)";

      // Border overlay — white band around image frame, drawn inward from mat edge
      if (borderEl) {
        if (borderPx > 0) {
          borderEl.style.inset = "0";
          borderEl.style.borderWidth = borderPx + "px";
          borderEl.style.borderColor = "#fff";
          borderEl.style.borderStyle = "solid";
          borderEl.style.outline = "1px solid rgba(0,0,0,0.15)";
          borderEl.style.outlineOffset = "0px";
        } else {
          borderEl.style.inset = "0";
          borderEl.style.borderWidth = "0";
          borderEl.style.borderStyle = "none";
          borderEl.style.outline = "none";
        }
      }

      // Image transform within the frame (matching canvas logic)
      const rotation = app.state.image.frameState.rotation;

      const imageGeom = app.commands.cropCoverImageGeometry(
        frameW,
        frameH,
        imgNatW,
        imgNatH,
        app.state.image.frameState.scale,
        rotation,
      );
      const displayW = imageGeom.displayW;
      const displayH = imageGeom.displayH;

      const slackX = Math.max(0, imageGeom.visualW - frameW);
      const slackY = Math.max(0, imageGeom.visualH - frameH);
      const offsetX = app.state.image.frameState.panX * slackX / 2;
      const offsetY = app.state.image.frameState.panY * slackY / 2;

      // Image positioned within the image frame area (inset by borderPx)
      const imgL = borderPx + frameW / 2 - displayW / 2 - offsetX;
      const imgT = borderPx + frameH / 2 - displayH / 2 - offsetY;

      pImg.style.position = "absolute";
      pImg.style.left = imgL + "px";
      pImg.style.top = imgT + "px";
      pImg.style.width = displayW + "px";
      pImg.style.height = displayH + "px";
      pImg.style.maxWidth = "none";
      pImg.style.maxHeight = "none";
      const sx = app.state.image.frameState.flipH ? -1 : 1;
      const sy = app.state.image.frameState.flipV ? -1 : 1;
      pImg.style.transform = `rotate(${rotation}deg) scale(${sx}, ${sy})`;
      pImg.style.transformOrigin = "center center";
      pImg.style.filter = app.commands.buildAdjustFilterCSS();

      // Dimension annotations in preview
      const dimSvg = app.state.ui.$("#previewDimensions");
      if (dimSvg) {
        const matRect = mat.getBoundingClientRect();
        const vpRect = viewport.getBoundingClientRect();
        // Mat is the full print footprint (image frame + border)
        const mL = matRect.left - vpRect.left;
        const mT = matRect.top - vpRect.top;
        const mW = matRect.width;
        const mH = matRect.height;

        const wMm = (app.state.image.frameState.widthMm + 2 * bwMm).toFixed(1);
        const hMm = (app.state.image.frameState.heightMm + 2 * bwMm).toFixed(1);
        const off = 20;  // offset from mat edge
        const cap = 6;   // end cap half-height
        const ah = 6;    // arrowhead size

        // Width: below mat — line with end caps and arrowheads
        const wy = mT + mH + off;
        const wxL = mL;
        const wxR = mL + mW;
        const wxMid = mL + mW / 2;

        // Height: right of mat
        const hx = mL + mW + off;
        const hyT = mT;
        const hyB = mT + mH;
        const hyMid = mT + mH / 2;

        const c = "rgba(255,255,255,0.7)";
        const tc = "rgba(255,255,255,0.9)";

        dimSvg.innerHTML = `
          <!-- Width dimension -->
          <line x1="${wxL}" y1="${wy}" x2="${wxR}" y2="${wy}" stroke="${c}" stroke-width="1"/>
          <line x1="${wxL}" y1="${wy-cap}" x2="${wxL}" y2="${wy+cap}" stroke="${c}" stroke-width="1"/>
          <line x1="${wxR}" y1="${wy-cap}" x2="${wxR}" y2="${wy+cap}" stroke="${c}" stroke-width="1"/>
          <polygon points="${wxL},${wy} ${wxL+ah},${wy-ah/2} ${wxL+ah},${wy+ah/2}" fill="${c}"/>
          <polygon points="${wxR},${wy} ${wxR-ah},${wy-ah/2} ${wxR-ah},${wy+ah/2}" fill="${c}"/>
          <text x="${wxMid}" y="${wy-6}" text-anchor="middle" fill="${tc}" font-size="12" font-family="Segoe UI,sans-serif" font-weight="600">${wMm} mm</text>
          <!-- Height dimension -->
          <line x1="${hx}" y1="${hyT}" x2="${hx}" y2="${hyB}" stroke="${c}" stroke-width="1"/>
          <line x1="${hx-cap}" y1="${hyT}" x2="${hx+cap}" y2="${hyT}" stroke="${c}" stroke-width="1"/>
          <line x1="${hx-cap}" y1="${hyB}" x2="${hx+cap}" y2="${hyB}" stroke="${c}" stroke-width="1"/>
          <polygon points="${hx},${hyT} ${hx-ah/2},${hyT+ah} ${hx+ah/2},${hyT+ah}" fill="${c}"/>
          <polygon points="${hx},${hyB} ${hx-ah/2},${hyB-ah} ${hx+ah/2},${hyB-ah}" fill="${c}"/>
          <text x="${hx}" y="${hyMid}" text-anchor="middle" fill="${tc}" font-size="12" font-family="Segoe UI,sans-serif" font-weight="600" transform="rotate(-90,${hx},${hyMid})" dy="-8">${hMm} mm</text>
        `;
      }

    };

    if (pImg.complete && pImg.naturalWidth > 0) {
      doPreview();
    } else {
      pImg.addEventListener("load", doPreview, { once: true });
    }

    // Also update border overlay even if image hasn't changed layout
    const borderEl2 = app.state.ui.$("#previewBorder");
    if (borderEl2 && mat.style.width) {
      const fw = parseFloat(mat.style.width);
      if (app.state.settings.config.border && app.state.settings.config.border_width_mm > 0 && fw > 0) {
        const bPx = Math.max(2, (app.state.settings.config.border_width_mm / app.state.image.frameState.widthMm) * fw);
        borderEl2.style.borderWidth = bPx + "px";
        borderEl2.style.borderColor = "#fff";
        borderEl2.style.borderStyle = "solid";
        borderEl2.style.outline = "1px solid rgba(0,0,0,0.15)";
        borderEl2.style.outlineOffset = "0px";
      } else {
        borderEl2.style.borderWidth = "0";
        borderEl2.style.borderStyle = "none";
        borderEl2.style.outline = "none";
      }
    }
  }

  function syncWidthSlider() {
    const sl = app.state.ui.$("#widthSlider");
    if (sl) sl.value = app.state.image.frameState.widthMm;
  }

  function syncHeightSlider() {
    const sl = app.state.ui.$("#heightSlider");
    if (sl) sl.value = app.state.image.frameState.heightMm;
  }

  function renderDimensionAnnotations(frameL, frameT, frameW, frameH) {
    const svg = app.state.ui.$("#frameDimensions");
    if (!svg) return;
    if (frameW <= 0 || frameH <= 0) { svg.innerHTML = ""; return; }

    const wMm = app.state.image.frameState.widthMm.toFixed(1);
    const hMm = app.state.image.frameState.heightMm.toFixed(1);
    const off = 18; // offset from frame edge
    const ah = 4;   // arrowhead size

    // Width annotation: below frame
    const wy = frameT + frameH + off;
    const wxL = frameL;
    const wxR = frameL + frameW;
    const wxMid = frameL + frameW / 2;

    // Height annotation: right of frame
    const hx = frameL + frameW + off;
    const hyT = frameT;
    const hyB = frameT + frameH;
    const hyMid = frameT + frameH / 2;

    svg.innerHTML = `
      <!-- Width -->
      <line x1="${wxL}" y1="${wy}" x2="${wxR}" y2="${wy}" stroke="rgba(255,255,255,0.6)" stroke-width="1"/>
      <line x1="${wxL}" y1="${wy-ah}" x2="${wxL}" y2="${wy+ah}" stroke="rgba(255,255,255,0.6)" stroke-width="1"/>
      <line x1="${wxR}" y1="${wy-ah}" x2="${wxR}" y2="${wy+ah}" stroke="rgba(255,255,255,0.6)" stroke-width="1"/>
      <text x="${wxMid}" y="${wy+13}" text-anchor="middle" fill="rgba(255,255,255,0.85)" font-size="10" font-family="Segoe UI, sans-serif" font-weight="600">${wMm} mm</text>
      <!-- Height -->
      <line x1="${hx}" y1="${hyT}" x2="${hx}" y2="${hyB}" stroke="rgba(255,255,255,0.6)" stroke-width="1"/>
      <line x1="${hx-ah}" y1="${hyT}" x2="${hx+ah}" y2="${hyT}" stroke="rgba(255,255,255,0.6)" stroke-width="1"/>
      <line x1="${hx-ah}" y1="${hyB}" x2="${hx+ah}" y2="${hyB}" stroke="rgba(255,255,255,0.6)" stroke-width="1"/>
      <text x="${hx+13}" y="${hyMid+3}" text-anchor="middle" fill="rgba(255,255,255,0.85)" font-size="10" font-family="Segoe UI, sans-serif" font-weight="600" transform="rotate(90, ${hx+13}, ${hyMid+3})">${hMm} mm</text>
    `;
  }

  function initEnhancedSlider(sliderEl, opts = {}) {
    const { center, onUpdate, snapThreshold = 0.03 } = opts;

    // Mark center position via CSS custom property (rendered by CSS)
    if (center !== undefined) {
      const min = parseFloat(sliderEl.min);
      const max = parseFloat(sliderEl.max);
      const pct = ((center - min) / (max - min)) * 100;
      sliderEl.style.setProperty("--center-pct", `${pct}%`);
      sliderEl.classList.add("has-center-tick");
    }

    // Magnetic snap to center on slider input (not scroll wheel)
    let fromWheel = false;

    sliderEl.addEventListener("input", () => {
      let val = parseFloat(sliderEl.value);
      if (!fromWheel && center !== undefined) {
        const range = parseFloat(sliderEl.max) - parseFloat(sliderEl.min);
        const snapRange = range * snapThreshold;
        if (Math.abs(val - center) < snapRange) {
          val = center;
          sliderEl.value = center;
        }
      }
      fromWheel = false;
      if (onUpdate) onUpdate(val);
    });

    // Scroll wheel: progressive increment, no snap
    sliderEl.addEventListener("wheel", (e) => {
      e.preventDefault();
      fromWheel = true;
      const step = parseFloat(sliderEl.step) || 1;
      const speed = Math.min(10, Math.max(1, Math.abs(e.deltaY) / 30));
      const delta = (e.deltaY > 0 ? -1 : 1) * step * speed;
      const min = parseFloat(sliderEl.min);
      const max = parseFloat(sliderEl.max);
      const newVal = Math.min(max, Math.max(min, parseFloat(sliderEl.value) + delta));
      sliderEl.value = newVal;
      if (onUpdate) onUpdate(newVal);
      // Fire input event so other handlers (from bindEvents) also react
      sliderEl.dispatchEvent(new Event("input", { bubbles: true }));
    }, { passive: false });
  }

  function initAllEnhancedSliders() {
    // Scale slider — center at 100%
    const scaleSlider = app.state.ui.$("#scaleSlider");
    if (scaleSlider) app.commands.initEnhancedSlider(scaleSlider, { center: 100 });

    // Rotation slider — center at 0°
    const rotSlider = app.state.ui.$("#rotationSlider");
    if (rotSlider) app.commands.initEnhancedSlider(rotSlider, { center: 0 });

    // Image adjustment sliders — center at 0
    const adjustSliders = [
      "adjustExposureSlider", "adjustContrastSlider",
      "adjustHighlightSlider", "adjustShadowSlider",
      "adjustSaturationSlider", "adjustTempSlider",
    ];
    adjustSliders.forEach(id => {
      const el = app.state.ui.$(`#${id}`);
      if (el) app.commands.initEnhancedSlider(el, { center: 0 });
    });

    // Tint sliders — hue has no center (0-360), strength has no center (0-1)
    const tintHueSlider = app.state.ui.$("#adjustTintHueSlider");
    if (tintHueSlider) app.commands.initEnhancedSlider(tintHueSlider, {});
    const tintStrSlider = app.state.ui.$("#adjustTintStrengthSlider");
    if (tintStrSlider) app.commands.initEnhancedSlider(tintStrSlider, {});

    // Width/Height sliders — scroll wheel support
    const widthSlider = app.state.ui.$("#widthSlider");
    if (widthSlider) app.commands.initEnhancedSlider(widthSlider, {
      onUpdate: (v) => {
        const oldW = app.state.image.frameState.widthMm, oldH = app.state.image.frameState.heightMm;
        app.state.image.frameState.widthMm = app.commands.clampFrameWidth(v);
        app.state.image.lastTouchedDim = "width";
        if (app.state.image.frameState.arMode !== "specified") app.commands.applyARToHeight();
        app.commands.adjustScaleForFrameChange(oldW, oldH, app.state.image.frameState.widthMm, app.state.image.frameState.heightMm);
        app.commands.syncDimFields();
        app.commands.syncHeightSlider();
        app.commands.renderFrameCanvas();
        app.commands.updateInfoGrid();
      }
    });
    const heightSlider = app.state.ui.$("#heightSlider");
    if (heightSlider) app.commands.initEnhancedSlider(heightSlider, {
      onUpdate: (v) => {
        const oldW = app.state.image.frameState.widthMm, oldH = app.state.image.frameState.heightMm;
        app.state.image.frameState.heightMm = app.commands.clampFrameHeight(v);
        app.state.image.lastTouchedDim = "height";
        if (app.state.image.frameState.arMode !== "specified") app.commands.applyARToWidth();
        app.commands.adjustScaleForFrameChange(oldW, oldH, app.state.image.frameState.widthMm, app.state.image.frameState.heightMm);
        app.commands.syncDimFields();
        app.commands.syncWidthSlider();
        app.commands.renderFrameCanvas();
        app.commands.updateInfoGrid();
      }
    });
  }

  function updateInfoGrid() {
    if (!app.state.image.selectedImage) {
      app.state.ui.$("#infoOrigDims").textContent = "\u2014";
      app.state.ui.$("#infoPrintSize").textContent = "\u2014";
      app.state.ui.$("#infoSolvePx").textContent = "\u2014";
      return;
    }

    const w = app.state.image.selectedImage.width;
    const h = app.state.image.selectedImage.height;
    const pxSize = app.commands.getCurrentSolvePitch();
    const printW = app.state.image.frameState.widthMm;
    const printH = app.state.image.frameState.heightMm;
    const solveW = Math.round(printW / pxSize);
    const solveH = Math.round(printH / pxSize);
    const totalPx = solveW * solveH;

    app.state.settings.config.max_dim_mm = Math.max(printW, printH);

    // Print size = image frame + border if enabled
    const bw = (app.state.settings.config.border && app.state.settings.config.border_width_mm > 0) ? app.state.settings.config.border_width_mm : 0;
    const lithW = printW + 2 * bw;
    const lithH = printH + 2 * bw;

    app.state.ui.$("#infoOrigDims").textContent = `${w} \u00d7 ${h} px`;
    app.state.ui.$("#infoPrintSize").textContent = `${lithW.toFixed(1)} \u00d7 ${lithH.toFixed(1)} mm`;
    app.state.ui.$("#infoSolvePx").textContent = `${solveW} \u00d7 ${solveH} = ${totalPx.toLocaleString()} px`;
  }

  function adjustScaleForFrameChange(oldW, oldH, newW, newH) {
    // Kept as a compatibility hook for the existing event wiring. In crop-only
    // framing, physical dimension edits change output size/aspect; they should
    // not implicitly alter source crop zoom.
    app.state.image.frameState.scale = app.commands.clamp(Number(app.state.image.frameState.scale) || 100, 100, 1000);
    app.commands.syncScaleSlider();
  }

  function syncDimLockState() {
    const wSlider = app.state.ui.$("#widthSlider");
    const hSlider = app.state.ui.$("#heightSlider");
    const wInput = app.state.ui.$("#outputWidthMm");
    const hInput = app.state.ui.$("#outputHeightMm");
    if (wSlider) wSlider.disabled = app.state.image.widthLocked;
    if (hSlider) hSlider.disabled = app.state.image.heightLocked;
    if (wInput) wInput.disabled = app.state.image.widthLocked;
    if (hInput) hInput.disabled = app.state.image.heightLocked;
  }

  function syncDimFields() {
    const owInput = app.state.ui.$("#outputWidthMm");
    const ohInput = app.state.ui.$("#outputHeightMm");
    if (owInput) owInput.value = app.state.image.frameState.widthMm.toFixed(1);
    if (ohInput) ohInput.value = app.state.image.frameState.heightMm.toFixed(1);
  }

  function syncScaleSlider() {
    const slider = app.state.ui.$("#scaleSlider");
    const input = app.state.ui.$("#scaleInput");
    if (slider) slider.value = app.state.image.frameState.scale;
    if (input) input.value = Math.round(app.state.image.frameState.scale);
  }

  function syncRotationSlider() {
    const slider = app.state.ui.$("#rotationSlider");
    const input = app.state.ui.$("#rotationInput");
    if (slider) slider.value = app.state.image.frameState.rotation;
    if (input) input.value = app.state.image.frameState.rotation.toFixed(1);
  }

  function frameDimensionMin(axis) {
    const slider = axis === "height" ? app.state.ui.$("#heightSlider") : app.state.ui.$("#widthSlider");
    const parsed = slider ? parseFloat(slider.min) : NaN;
    return Number.isFinite(parsed) && parsed > 0 ? parsed : 10;
  }

  function frameDimensionMax(axis) {
    const slider = axis === "height" ? app.state.ui.$("#heightSlider") : app.state.ui.$("#widthSlider");
    const parsed = slider ? parseFloat(slider.max) : NaN;
    return Number.isFinite(parsed) && parsed > 0 ? parsed : 300;
  }

  function clampFrameWidth(value) {
    return app.commands.clamp(Number(value) || app.commands.frameDimensionMin("width"), app.commands.frameDimensionMin("width"), app.commands.frameDimensionMax("width"));
  }

  function clampFrameHeight(value) {
    return app.commands.clamp(Number(value) || app.commands.frameDimensionMin("height"), app.commands.frameDimensionMin("height"), app.commands.frameDimensionMax("height"));
  }

  function updateARButtons() {
    app.state.ui.$$("#arButtonGroup .ar-button").forEach((btn) => {
      btn.classList.toggle("is-active", btn.dataset.ar === app.state.image.frameState.arMode);
      const ar = btn.dataset.ar;
      // Disable "Image" when no image is loaded
      if (ar === "image") btn.disabled = !app.state.image.selectedImage;
      // Update label to reflect direction
      if (ar && ar.includes(":") && ar !== "1:1") {
        const [a, b] = ar.split(":");
        btn.textContent = app.state.image.imageDirection === "portrait" ? `${b}:${a}` : `${a}:${b}`;
      }
    });
  }

  function applyARToHeight() {
    // Adjust height to match the current AR, keeping width as anchor
    const ar = app.commands.getEffectiveAR();
    app.state.image.frameState.widthMm = app.commands.clampFrameWidth(app.state.image.frameState.widthMm);
    let heightMm = app.state.image.frameState.widthMm / ar;
    if (heightMm > app.commands.frameDimensionMax("height")) {
      heightMm = app.commands.frameDimensionMax("height");
      app.state.image.frameState.widthMm = app.commands.clampFrameWidth(heightMm * ar);
    }
    app.state.image.frameState.heightMm = Math.round(app.commands.clampFrameHeight(heightMm) * 100) / 100;
  }

  function applyARToWidth() {
    // Adjust width to match the current AR, keeping height as anchor
    const ar = app.commands.getEffectiveAR();
    app.state.image.frameState.heightMm = app.commands.clampFrameHeight(app.state.image.frameState.heightMm);
    let widthMm = app.state.image.frameState.heightMm * ar;
    if (widthMm > app.commands.frameDimensionMax("width")) {
      widthMm = app.commands.frameDimensionMax("width");
      app.state.image.frameState.heightMm = app.commands.clampFrameHeight(widthMm / ar);
    }
    app.state.image.frameState.widthMm = Math.round(app.commands.clampFrameWidth(widthMm) * 100) / 100;
  }

  function applyARFromLastTouched() {
    if (app.state.image.lastTouchedDim === "height") app.commands.applyARToWidth();
    else app.commands.applyARToHeight();
  }

  function applyImageAspectDefault() {
    if (!app.state.image.selectedImage) return;
    app.state.image.frameState.arMode = "image";
    const ar = app.commands.getEffectiveAR(); // width / height for the selected image in "image" mode
    if (!(ar > 0) || !isFinite(ar)) return;
    if (ar >= 1) {
      // landscape or square: the height is the short side
      app.state.image.frameState.heightMm = app.commands.clampFrameHeight(app.state.ui.IMAGE_ASPECT_SHORT_SIDE_MM);
      app.state.image.frameState.widthMm = app.commands.clampFrameWidth(app.commands.roundFrameMm(app.state.ui.IMAGE_ASPECT_SHORT_SIDE_MM * ar));
    } else {
      // portrait: the width is the short side
      app.state.image.frameState.widthMm = app.commands.clampFrameWidth(app.state.ui.IMAGE_ASPECT_SHORT_SIDE_MM);
      app.state.image.frameState.heightMm = app.commands.clampFrameHeight(app.commands.roundFrameMm(app.state.ui.IMAGE_ASPECT_SHORT_SIDE_MM / ar));
    }
    app.state.image.lastTouchedDim = "width";
  }

  function setARMode(mode) {
    app.state.image.frameState.arMode = mode;
    if (mode !== "specified") {
      // Apply ratio to dimensions using last-touched anchor
      if (mode === "ratio") {
        // Will be applied after dialog confirm
      } else {
        app.commands.applyARFromLastTouched();
      }
    }
    app.commands.renderFrameCanvas();
    app.commands.renderPreview();
    app.commands.updateInfoGrid();
    app.commands.syncDimFields();
    app.commands.updateARButtons();
    app.commands.syncConfigToServer();
  }

  function initFrameInteraction() {
    const canvas = app.state.ui.$("#frameCanvas");
    if (!canvas) return;

    // Pan: drag on canvas
    canvas.addEventListener("mousedown", (e) => {
      if (!app.state.image.selectedImage) return;
      if (app.state.image.frameEditorTab === "image") return; // locked in Image mode
      // Don't intercept edge handle drags
      if (e.target.dataset?.edge) return;
      e.preventDefault();
      app.state.image.panDragState = {
        startX: e.clientX,
        startY: e.clientY,
        startPanX: app.state.image.frameState.panX,
        startPanY: app.state.image.frameState.panY,
      };
      canvas.style.cursor = "grabbing";
    });

    document.addEventListener("mousemove", (e) => {
      if (app.state.image.panDragState) {
        e.preventDefault();
        const geom = canvas._frameGeom;
        if (!geom) return;
        // Convert editor pixels through the source-space crop model.
        const crop = geom.crop;
        const sourceDx = (e.clientX - app.state.image.panDragState.startX) / Math.max(geom.imgScale, 1e-9);
        const sourceDy = (e.clientY - app.state.image.panDragState.startY) / Math.max(geom.imgScale, 1e-9);
        const dx = crop.slackX <= 1e-6 ? 0 : sourceDx / (crop.slackX / 2);
        const dy = crop.slackY <= 1e-6 ? 0 : sourceDy / (crop.slackY / 2);
        app.state.image.frameState.panX = crop.slackX <= 1e-6 ? 0 : app.commands.clamp(app.state.image.panDragState.startPanX - dx, -1, 1);
        app.state.image.frameState.panY = crop.slackY <= 1e-6 ? 0 : app.commands.clamp(app.state.image.panDragState.startPanY - dy, -1, 1);
        app.commands.renderFrameCanvas();

      }
      if (app.state.image.frameDragState) {
        e.preventDefault();
        const geom = canvas._frameGeom;
        if (!geom) return;
        // Symmetric resize from center in Specified mode
        const dx = (e.clientX - app.state.image.frameDragState.startX);
        const dy = (e.clientY - app.state.image.frameDragState.startY);
        const edge = app.state.image.frameDragState.edge;
        const pxPerMm = app.state.image.frameDragState.projection.pxPerMm;

        let newW = app.state.image.frameDragState.startWMm;
        let newH = app.state.image.frameDragState.startHMm;

        // Symmetric: both sides move equally, so delta is doubled
        if (!app.state.image.widthLocked && (edge.includes("e") || edge.includes("w"))) newW = Math.max(10, app.state.image.frameDragState.startWMm + Math.abs(dx) * 2 / pxPerMm * Math.sign(edge.includes("e") ? dx : -dx));
        if (!app.state.image.heightLocked && (edge.includes("s") || edge.includes("n"))) newH = Math.max(10, app.state.image.frameDragState.startHMm + Math.abs(dy) * 2 / pxPerMm * Math.sign(edge.includes("s") ? dy : -dy));
        if (edge.length === 1 && (edge === "e" || edge === "w")) {
          newW = Math.min(newW, newH * app.state.image.frameDragState.projection.sourceAR);
        }
        if (edge.length === 1 && (edge === "n" || edge === "s")) {
          newH = Math.min(newH, newW / app.state.image.frameDragState.projection.sourceAR);
        }

        // Corner: resize both (unlocked axes only)
        if (edge.length === 2) {
          // Use both
        }

        const oldW = app.state.image.frameState.widthMm, oldH = app.state.image.frameState.heightMm;
        if (!app.state.image.widthLocked) app.state.image.frameState.widthMm = Math.round(newW * 100) / 100;
        if (!app.state.image.heightLocked) app.state.image.frameState.heightMm = Math.round(newH * 100) / 100;
        app.commands.adjustScaleForFrameChange(oldW, oldH, app.state.image.frameState.widthMm, app.state.image.frameState.heightMm);
        app.commands.finishFrameModelUpdate({ syncServer: false });
      }
    });

    document.addEventListener("mouseup", () => {
      if (app.state.image.panDragState) {
        app.state.image.panDragState = null;
        canvas.style.cursor = "";
        app.commands.syncConfigToServer();
      }
      if (app.state.image.frameDragState) {
        app.state.image.frameDragState = null;
        canvas.style.cursor = "";
        app.commands.syncConfigToServer();
      }
    });

    // Edge handles for Specified mode
    canvas.querySelectorAll(".frame-edge").forEach((el) => {
      el.addEventListener("mousedown", (e) => {
        if (app.state.image.frameState.arMode !== "specified" || !app.state.image.selectedImage || app.state.image.frameEditorTab === "image") return;
        e.preventDefault();
        e.stopPropagation();
        const geom = canvas._frameGeom;
        if (!geom) return;
        const bounds = app.commands.getTransformedSourceBounds();
        const pxPerMm = geom.frameW / Math.max(app.state.image.frameState.widthMm, 1e-9);
        app.state.image.frameDragState = {
          edge: el.dataset.edge,
          startX: e.clientX,
          startY: e.clientY,
          startWMm: app.state.image.frameState.widthMm,
          startHMm: app.state.image.frameState.heightMm,
          projection: {
            pxPerMm,
            centerX: geom.frameL + geom.frameW / 2,
            centerY: geom.frameT + geom.frameH / 2,
            sourceAR: bounds.width / Math.max(bounds.height, 1e-9),
            imgScale: geom.imgScale,
            displayW: geom.displayW,
            displayH: geom.displayH,
            visualW: geom.visualW,
            visualH: geom.visualH,
            imgL: geom.imgL,
            imgT: geom.imgT,
            visualLeft: geom.visualLeft,
            visualTop: geom.visualTop,
            visualRight: geom.visualRight,
            visualBottom: geom.visualBottom,
          },
        };
        canvas.style.cursor = getComputedStyle(el).cursor;
      });
    });

    // Zoom: scroll wheel on canvas
    canvas.addEventListener("wheel", (e) => {
      if (!app.state.image.selectedImage) return;
      if (app.state.image.frameEditorTab === "image") return; // locked in Image mode
      e.preventDefault();
      const delta = e.deltaY > 0 ? -5 : 5; // scroll down = zoom out
      app.state.image.frameState.scale = app.commands.clamp(app.state.image.frameState.scale + delta, 100, 1000);
      app.commands.syncScaleSlider();
      app.commands.renderFrameCanvas();

    }, { passive: false });

    // Resize observer
    const ro = new ResizeObserver(() => {
      if (app.state.image.selectedImage) app.commands.renderFrameCanvas();
    });
    ro.observe(canvas);

    // Drag-drop images onto canvas
    canvas.addEventListener("dragover", (e) => {
      e.preventDefault();
      e.dataTransfer.dropEffect = "copy";
      canvas.classList.add("drag-target");
    });
    canvas.addEventListener("dragleave", () => {
      canvas.classList.remove("drag-target");
    });
    canvas.addEventListener("drop", (e) => {
      e.preventDefault();
      canvas.classList.remove("drag-target");
      try {
        const data = JSON.parse(e.dataTransfer.getData("application/json"));
        if (data.type === "image" && data.filename) {
          const isNewImage = data.filename !== app.state.image.selectedImage?.filename;
          if (isNewImage) {
            app.state.image.frameState.scale = 100.0;
            app.state.image.frameState.rotation = 0;
            app.state.image.frameState.panX = 0;
            app.state.image.frameState.panY = 0;
            app.state.image.frameState.flipH = false;
            app.state.image.frameState.flipV = false;
          }
          app.state.image.selectedImage = app.state.image.availableImages.find((i) => i.filename === data.filename) || null;
          if (app.state.image.selectedImage && isNewImage) app.commands.applyImageAspectDefault();  // Stage 11: default to image aspect
          app.commands.renderImageTab();
          app.commands.updateRail();
        }
      } catch { /* ignore non-image drags */ }
    });
  }

  function toggleCreationMode(mode) {
    app.state.palette.creationMode = mode;
    const autoPanel = app.state.ui.$("#panelAutoSuggest");
    const manualPanel = app.state.ui.$("#panelManualBuilder");
    const layout = document.querySelector(".creation-layout");
    const deckPanel = app.state.ui.$("#creationDeckPanel");
    const manualPalettePanel = app.state.ui.$("#manualPalettePanel");
    const isAuto = mode === "auto";
    if (autoPanel) {
      autoPanel.classList.toggle("is-expanded", isAuto);
      autoPanel.hidden = !isAuto;
    }
    if (manualPanel) {
      manualPanel.classList.toggle("is-expanded", !isAuto);
      manualPanel.hidden = isAuto;
    }
    if (layout) layout.classList.toggle("is-manual-mode", !isAuto);
    if (deckPanel) deckPanel.hidden = !isAuto;
    if (manualPalettePanel) manualPalettePanel.hidden = isAuto;
    app.state.ui.$$(".creation-mode-tabs .segmented-btn").forEach((btn) => {
      const active = btn.dataset.panel === mode;
      btn.classList.toggle("is-active", active);
      btn.setAttribute("aria-selected", active ? "true" : "false");
    });
    app.commands.renderCreationTab();
  }

  function syncCreationSidePanelSizing() {
    const sourcePanel = app.state.palette.creationMode === "manual" ? app.state.ui.$("#panelManualBuilder") : app.state.ui.$("#panelAutoSuggest");
    const sidePanel = app.state.palette.creationMode === "manual" ? app.state.ui.$("#manualPalettePanel") : app.state.ui.$("#creationDeckPanel");
    if (!sourcePanel || !sidePanel || sidePanel.hidden) return;
    sidePanel.style.minHeight = "";
    const sourceHeight = Math.ceil(sourcePanel.getBoundingClientRect().height);
    if (sourceHeight > 0) sidePanel.style.minHeight = `${sourceHeight}px`;
  }

  function syncDeckGenerationSettingsUI(source = "settings") {
    for (const field of app.state.ui.DECK_GENERATION_FIELD_MAP) {
      const el = app.state.ui.$(`#${field.paletteId}`);
      if (!el) continue;
      if (source === "palette") {
        if (field.prop === "checked") {
          app.state.settings.config[field.configKey] = !!el.checked;
        } else {
          const parsed = parseFloat(el.value);
          if (Number.isFinite(parsed)) app.state.settings.config[field.configKey] = parsed;
        }
      } else {
        el[field.prop] = field.prop === "checked" ? !!app.state.settings.config[field.configKey] : app.state.settings.config[field.configKey];
      }
    }
  }

  Object.assign(app.commands, {
    refreshImageLibrary,
    renderImageGrid,
    bindImageLibraryWheelScroll,
    clamp,
    getEffectiveAR,
    openRatioDialog,
    closeRatioDialog,
    rotatedBounds,
    cropCoverImageGeometry,
    getTransformedSourceBounds,
    largestContainedCrop,
    cropModelFromFrameState,
    projectCropToFrame,
    projectDragCropToFrame,
    resetCropToFitSource,
    roundFrameMm,
    applyCropModel,
    fitFrameToSourceWidth,
    fitFrameToSourceHeight,
    finishFrameModelUpdate,
    _ensureSvgFilter,
    _updateSvgFilter,
    buildAdjustFilterCSS,
    renderFrameCanvas,
    renderPreview,
    syncWidthSlider,
    syncHeightSlider,
    renderDimensionAnnotations,
    initEnhancedSlider,
    initAllEnhancedSliders,
    updateInfoGrid,
    adjustScaleForFrameChange,
    syncDimLockState,
    syncDimFields,
    syncScaleSlider,
    syncRotationSlider,
    frameDimensionMin,
    frameDimensionMax,
    clampFrameWidth,
    clampFrameHeight,
    updateARButtons,
    applyARToHeight,
    applyARToWidth,
    applyARFromLastTouched,
    applyImageAspectDefault,
    setARMode,
    initFrameInteraction,
    toggleCreationMode,
    syncCreationSidePanelSizing,
    syncDeckGenerationSettingsUI,
  });}
