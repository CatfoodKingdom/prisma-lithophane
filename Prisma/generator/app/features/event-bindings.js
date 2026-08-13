/**
 * Install the event-bindings feature commands.
 * @param {import("../core/types.js").ApplicationContext} app
 */
export function installFeaturesEventBindings(app) {
  function activeImageIdentity() {
    const image = app.state.image.selectedImage;
    return `${image?.source_ref || ""}\u0000${image?.filename || ""}`;
  }

  async function clearAllTempFiles() {
    if (app.state.session.clearTempRunning) return;
    if (app.state.solve.loadedRunApplyRunning) {
      app.commands.showToast("Wait for the saved run to finish loading before clearing temporary files.", "warn");
      return;
    }
    const ok = await app.commands.appConfirm(
      "Delete ALL cached temp files (solve runs, LUTs, and prepared source images)? Your original images, exported files, and saved runs are kept.",
      { ok: "Delete", cancel: "Cancel", title: "Clear Temp Files" },
    );
    if (!ok || app.state.session.clearTempRunning) return;
    if (app.state.solve.loadedRunApplyRunning) {
      app.commands.showToast("Wait for the saved run to finish loading before clearing temporary files.", "warn");
      return;
    }

    const clearAllBtn = app.state.ui.$("#clearAllTempBtn");
    app.state.session.clearTempRunning = true;
    if (clearAllBtn) {
      clearAllBtn.disabled = true;
      clearAllBtn.setAttribute("aria-busy", "true");
    }
    try {
      await app.state.settings._configSyncChain.catch(() => {});
      await app.commands.syncConfigToServer({ throwOnError: true });
      const selectionAtRequest = activeImageIdentity();
      const privateSelectionAtRequest = app.state.image.selectedImage?.source_ref || null;
      const body = await app.api.clearAllTempFiles();
      app.commands.clearSolveHistory({ force: true });
      app.state.solve.paletteBatchFetchedResultIds.clear();
      app.state.solve.paletteBatchResultFetches.clear();
      const selectionUnchanged = activeImageIdentity() === selectionAtRequest;
      if (selectionUnchanged && body.config) {
        Object.assign(app.state.settings.config, body.config);
      }
      if (
        privateSelectionAtRequest
        && app.state.image.selectedImage?.source_ref === privateSelectionAtRequest
      ) {
        app.state.image.selectedImage = null;
        app.state.image.pendingSelectedFilename = null;
        app.state.settings.config.image_path = null;
        app.state.settings.config.image_source_ref = null;
      } else if (!selectionUnchanged) {
        await app.commands.syncConfigToServer({ throwOnError: true });
      }
      app.commands.renderImageTab();
      app.commands.updateRail();
      app.commands.showToast(
        privateSelectionAtRequest || body.cleared_source_ref
          ? "Temporary saved-run source cleared. Load the saved run again to restore it."
          : "Temporary files cleared",
        "success",
      );
    } catch (error) {
      if (error?.status === 409) {
        app.commands.showToast("A solve, export, palette suggestion, or image import is still running — wait for it to finish before clearing.", "warn");
      } else {
        app.commands.showToast(error?.message || "Clear Temp Files failed", "error");
      }
    } finally {
      app.state.session.clearTempRunning = false;
      if (clearAllBtn) {
        clearAllBtn.disabled = false;
        clearAllBtn.removeAttribute("aria-busy");
      }
    }
  }

function bindEvents() {
    app.commands.bindImageImportEvents();
    app.lifecycle.listen(window, "resize", app.commands.refreshVisibleSolveContours);
    app.lifecycle.listen(window, "resize", app.commands.syncCreationSidePanelSizing);

    // Operation progress cancel
    const opCancelBtn = app.state.ui.$("#opProgressCancel");
    if (opCancelBtn) app.lifecycle.listen(opCancelBtn, "click", app.commands.cancelProgress);

    // Tab switches
    app.state.ui.$$("#tabSwitch .mode-button").forEach((btn) => {
      app.lifecycle.listen(btn, "click", () => app.commands.switchTab(btn.dataset.tab));
    });

    // Hardcoded settings fields should write through to the draft config on
    // every keystroke so rerenders and actions don't depend on blur-time DOM
    // scraping.
    [
      ["cfgLayerHeight", (raw) => {
        return app.commands.applyDraftNumberField("layer_height", raw, { isValid: (v) => v > 0 });
      }],
      ["cfgDWb", (raw) => app.commands.applyDraftNumberField("d_wb", raw, { isValid: (v) => v > 0 })],
      ["cfgDWcMin", (raw) => {
        const layers = parseInt(raw, 10);
        if (!Number.isFinite(layers) || layers < 1) return false;
        app.state.settings.config.min_cap_layers = layers;
        return true;
      }],
      ["cfgTMax", (raw) => app.commands.applyDraftNumberField("t_max", raw, { isValid: (v) => v > 0 })],
      ["cfgKMax", (raw) => app.commands.applyDraftNumberField("k_max", raw, { parse: parseInt, isValid: (v) => v >= 1 && v <= 7 })],
      ["cfgDeThreshold", (raw) => app.commands.applyDraftNumberField("de_threshold", raw, { isValid: (v) => v >= 0 })],
      ["cfgSmoothKernel", (raw) => {
        const radiusMm = parseFloat(raw);
        if (!Number.isFinite(radiusMm) || radiusMm < 0) return false;
        app.state.settings.config.boundary_cap_smoothing_radius_mm = radiusMm;
        return true;
      }],
      ["cfgBoundaryCapDeBudget", (raw) => app.commands.applyDraftNumberField("boundary_cap_de_budget", raw, { isValid: (v) => v >= 0 })],
      ["cfgBorderWidth", (raw) => app.commands.applyDraftNumberField("border_width_mm", raw, { isValid: (v) => v >= 0 })],
      ["cfgBorderHeight", (raw) => app.commands.applyDraftNumberField("border_height_mm", raw, { isValid: (v) => v >= 0 })],
    ].forEach(([id, applyDraft]) => app.commands.bindDraftNumberInput(id, applyDraft));

    // Image tab — upload
    app.lifecycle.listen(app.state.ui.$("#imageUploadInput"), "change", async (e) => {
      const files = Array.from(e.target.files || []);
      e.target.value = "";
      if (!files.length) return;
      try {
        await app.commands.startImageBatchImport(files);
      } catch (err) {
        app.commands.showToast(`Upload failed: ${err.message}`, "error");
      }
    });

    // Solve Pitch is a display-only whole-Extrusion-Width stepper.
    const stepSolvePitch = async delta => {
      if (!app.commands.stepSolvePitchMultiplier(delta)) return;
      app.commands.renderSettingsTab({ preservePendingUi: true });
      app.commands.updateDerivedParams();
      app.commands.updateInfoGrid();
      app.commands.renderPreview();
      app.commands.checkPresetModified();
      await app.commands.syncConfigToServer();
    };
    const spValue = app.state.ui.$("#cfgSolvePitch");
    const spMinus = app.state.ui.$("#cfgSolvePitchMinus");
    const spPlus = app.state.ui.$("#cfgSolvePitchPlus");
    if (spMinus) app.lifecycle.listen(spMinus, "click", () => stepSolvePitch(-1));
    if (spPlus) app.lifecycle.listen(spPlus, "click", () => stepSolvePitch(1));
    if (spValue) {
      const spControl = spValue.closest(".solve-pitch-control") || spValue;
      app.lifecycle.listen(spControl, "click", (event) => {
        if (event.target?.closest?.("button")) return;
        spValue.focus();
      });
      app.lifecycle.listen(spValue, "keydown", (event) => {
        if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
        event.preventDefault();
        void stepSolvePitch(event.key === "ArrowUp" ? 1 : -1);
      });
      app.lifecycle.listen(spControl, "wheel", (event) => {
        const multiplier = app.commands.settingsWheelMultiplier(event.deltaY);
        if (!multiplier) return;
        event.preventDefault();
        spValue.focus();
        void stepSolvePitch(multiplier);
      }, { passive: false });
    }

    app.state.ui.$$("#cfgLuminanceMode .segmented-btn").forEach((btn) => {
      app.lifecycle.listen(btn, "click", () => {
        app.commands.setSolveModeControlValue(btn.dataset.value || "standard");
        const paletteMode = app.state.ui.$("#paletteSuggestMode");
        if (paletteMode) {
          paletteMode.value = app.commands.normalizeLuminanceMode(
            btn.dataset.value || "standard",
          );
        }
        app.commands.updateLuminanceModeFields();
        app.commands.updateCapModeFields();
        app.commands.updateStage4DetailFields();
        app.commands.readConfigFromUI();
        app.commands.renderSettingsTab({ preservePendingUi: true });
        app.commands.updateDerivedParams();
        app.commands.updateAccordionSummaries();
        app.commands.checkPresetModified();
        app.commands.syncConfigToServer();
      });
    });

    // ── AR button group ──────────────────────────────────────────────────────
    app.state.ui.$$("#arButtonGroup .ar-button").forEach((btn) => {
      app.lifecycle.listen(btn, "click", () => {
        const mode = btn.dataset.ar;
        if (mode === "ratio") {
          app.commands.openRatioDialog();
        } else {
          app.commands.setARMode(mode);
        }
      });
    });

    // Ratio dialog confirm / cancel
    const ratioConfirm = app.state.ui.$("#ratioDialogConfirm");
    if (ratioConfirm) app.lifecycle.listen(ratioConfirm, "click", () => {
      const x = parseFloat(app.state.ui.$("#ratioDialogX").value) || 1;
      const y = parseFloat(app.state.ui.$("#ratioDialogY").value) || 1;
      app.state.image.frameState.customRatio = { x, y };
      app.commands.closeRatioDialog();
      app.commands.setARMode("ratio");
    });
    const ratioCancel = app.state.ui.$("#ratioDialogCancel");
    if (ratioCancel) app.lifecycle.listen(ratioCancel, "click", app.commands.closeRatioDialog);
    const ratioClose = app.state.ui.$("#ratioDialogClose");
    if (ratioClose) app.lifecycle.listen(ratioClose, "click", app.commands.closeRatioDialog);

    // ── Output dimension fields (with AR coupling) ──────────────────────────
    const owInput = app.state.ui.$("#outputWidthMm");
    const ohInput = app.state.ui.$("#outputHeightMm");
    if (owInput) app.lifecycle.listen(owInput, "change", () => {
      const v = parseFloat(owInput.value);
      if (!v || v <= 0) return;
      const oldW = app.state.image.frameState.widthMm, oldH = app.state.image.frameState.heightMm;
      app.state.image.frameState.widthMm = app.commands.clampFrameWidth(v);
      app.state.image.lastTouchedDim = "width";
      if (app.state.image.frameState.arMode !== "specified") app.commands.applyARToHeight();
      app.commands.adjustScaleForFrameChange(oldW, oldH, app.state.image.frameState.widthMm, app.state.image.frameState.heightMm);
      app.commands.syncDimFields();
      app.commands.syncWidthSlider();
      app.commands.syncHeightSlider();
      app.commands.renderFrameCanvas();
      app.commands.updateInfoGrid();
      app.commands.syncConfigToServer();
    });
    if (ohInput) app.lifecycle.listen(ohInput, "change", () => {
      const v = parseFloat(ohInput.value);
      if (!v || v <= 0) return;
      const oldW = app.state.image.frameState.widthMm, oldH = app.state.image.frameState.heightMm;
      app.state.image.frameState.heightMm = app.commands.clampFrameHeight(v);
      app.state.image.lastTouchedDim = "height";
      if (app.state.image.frameState.arMode !== "specified") app.commands.applyARToWidth();
      app.commands.adjustScaleForFrameChange(oldW, oldH, app.state.image.frameState.widthMm, app.state.image.frameState.heightMm);
      app.commands.syncDimFields();
      app.commands.syncWidthSlider();
      app.commands.syncHeightSlider();
      app.commands.renderFrameCanvas();
      app.commands.updateInfoGrid();
      app.commands.syncConfigToServer();
    });

    // ── Scale slider + input + fill button ──────────────────────────────────
    const scaleSlider = app.state.ui.$("#scaleSlider");
    const scaleInput = app.state.ui.$("#scaleInput");
    if (scaleSlider) app.lifecycle.listen(scaleSlider, "input", () => {
      app.state.image.frameState.scale = parseFloat(scaleSlider.value);
      if (scaleInput) scaleInput.value = Math.round(app.state.image.frameState.scale);
      app.commands.renderFrameCanvas();

    });
    if (scaleSlider) app.lifecycle.listen(scaleSlider, "change", () => app.commands.syncConfigToServer());
    if (scaleInput) app.lifecycle.listen(scaleInput, "change", () => {
      const v = parseFloat(scaleInput.value);
      if (!isNaN(v)) {
        app.state.image.frameState.scale = app.commands.clamp(v, 100, 1000);
        app.commands.syncScaleSlider();
        app.commands.renderFrameCanvas();

        app.commands.syncConfigToServer();
      }
    });
    const fitImageBtn = app.state.ui.$("#fitImageBtn");
    if (fitImageBtn) app.lifecycle.listen(fitImageBtn, "click", () => {
      app.commands.resetCropToFitSource();
      app.commands.finishFrameModelUpdate();
    });
    const fillWBtn = app.state.ui.$("#fillWidthBtn");
    if (fillWBtn) app.lifecycle.listen(fillWBtn, "click", () => {
      app.commands.fitFrameToSourceWidth();
      app.commands.finishFrameModelUpdate();
    });
    const fillHBtn = app.state.ui.$("#fillHeightBtn");
    if (fillHBtn) app.lifecycle.listen(fillHBtn, "click", () => {
      app.commands.fitFrameToSourceHeight();
      app.commands.finishFrameModelUpdate();
    });

    // ── Rotation slider + input ─────────────────────────────────────────────
    const rotSlider = app.state.ui.$("#rotationSlider");
    const rotInput = app.state.ui.$("#rotationInput");
    if (rotSlider) app.lifecycle.listen(rotSlider, "input", () => {
      app.state.image.frameState.rotation = parseFloat(rotSlider.value);
      if (rotInput) rotInput.value = app.state.image.frameState.rotation.toFixed(1);
      app.commands.renderFrameCanvas();

    });
    if (rotSlider) app.lifecycle.listen(rotSlider, "change", () => app.commands.syncConfigToServer());
    if (rotInput) app.lifecycle.listen(rotInput, "change", () => {
      const v = parseFloat(rotInput.value);
      if (!isNaN(v)) {
        app.state.image.frameState.rotation = app.commands.clamp(v, -180, 180);
        app.commands.syncRotationSlider();
        app.commands.renderFrameCanvas();

        app.commands.syncConfigToServer();
      }
    });

    // ── Rotation buttons: 90° L/R, H-flip, V-flip ──────────────────────────
    const rot90L = app.state.ui.$("#rotate90LBtn");
    const rot90R = app.state.ui.$("#rotate90RBtn");
    const hFlip = app.state.ui.$("#flipHBtn");
    const vFlip = app.state.ui.$("#flipVBtn");

    if (rot90L) app.lifecycle.listen(rot90L, "click", () => {
      app.state.image.frameState.rotation = app.commands.clamp(app.state.image.frameState.rotation - 90, -180, 180);
      app.commands.syncRotationSlider();
      app.commands.renderFrameCanvas();

      app.commands.syncConfigToServer();
    });
    if (rot90R) app.lifecycle.listen(rot90R, "click", () => {
      app.state.image.frameState.rotation = app.commands.clamp(app.state.image.frameState.rotation + 90, -180, 180);
      app.commands.syncRotationSlider();
      app.commands.renderFrameCanvas();

      app.commands.syncConfigToServer();
    });
    if (hFlip) app.lifecycle.listen(hFlip, "click", () => {
      app.state.image.frameState.flipH = !app.state.image.frameState.flipH;
      app.commands.renderFrameCanvas();
      app.commands.syncConfigToServer();
    });
    if (vFlip) app.lifecycle.listen(vFlip, "click", () => {
      app.state.image.frameState.flipV = !app.state.image.frameState.flipV;
      app.commands.renderFrameCanvas();
      app.commands.syncConfigToServer();
    });

    // ── Frame editor sub-tabs ────────────────────────────────────────────────
    app.state.ui.$$("#frameEditorTabs .frame-tab").forEach(btn => {
      app.lifecycle.listen(btn, "click", () => app.commands.switchFrameEditorTab(btn.dataset.ftab));
    });

    // ── Direction toggle ────────────────────────────────────────────────────
    app.state.ui.$$("#directionToggle .toggle-btn").forEach(btn => {
      app.lifecycle.listen(btn, "click", () => {
        app.state.image.imageDirection = btn.dataset.dir;
        app.state.ui.$$("#directionToggle .toggle-btn").forEach(b => b.classList.toggle("is-active", b === btn));
        // Swap width/height
        const oldW = app.state.image.frameState.widthMm, oldH = app.state.image.frameState.heightMm;
        [app.state.image.frameState.widthMm, app.state.image.frameState.heightMm] = [app.state.image.frameState.heightMm, app.state.image.frameState.widthMm];
        // Re-apply AR if in a ratio mode
        if (app.state.image.frameState.arMode !== "specified") {
          app.commands.applyARFromLastTouched();
        }
        app.commands.adjustScaleForFrameChange(oldW, oldH, app.state.image.frameState.widthMm, app.state.image.frameState.heightMm);
        app.commands.updateARButtons();
        app.commands.syncDimFields();
        app.commands.syncWidthSlider();
        app.commands.syncHeightSlider();
        app.commands.renderFrameCanvas();
        app.commands.updateInfoGrid();
        app.commands.syncConfigToServer();
      });
    });

    // ── Dimension lock button ────────────────────────────────────────────────
    const wLockBtn = app.state.ui.$("#widthLockBtn");
    if (wLockBtn) app.lifecycle.listen(wLockBtn, "click", (e) => {
      e.preventDefault();
      app.state.image.widthLocked = !app.state.image.widthLocked;
      wLockBtn.classList.toggle("is-locked", app.state.image.widthLocked);
      app.commands.syncDimLockState();
    });
    const hLockBtn = app.state.ui.$("#heightLockBtn");
    if (hLockBtn) app.lifecycle.listen(hLockBtn, "click", (e) => {
      e.preventDefault();
      app.state.image.heightLocked = !app.state.image.heightLocked;
      hLockBtn.classList.toggle("is-locked", app.state.image.heightLocked);
      app.commands.syncDimLockState();
    });

    // ── Width/Height sliders ────────────────────────────────────────────────
    const widthSlider = app.state.ui.$("#widthSlider");
    if (widthSlider) app.lifecycle.listen(widthSlider, "input", () => {
      const oldW = app.state.image.frameState.widthMm, oldH = app.state.image.frameState.heightMm;
      app.state.image.frameState.widthMm = app.commands.clampFrameWidth(parseFloat(widthSlider.value));
      app.state.image.lastTouchedDim = "width";
      if (app.state.image.frameState.arMode !== "specified") app.commands.applyARToHeight();
      app.commands.adjustScaleForFrameChange(oldW, oldH, app.state.image.frameState.widthMm, app.state.image.frameState.heightMm);
      app.commands.syncDimFields();
      app.commands.syncHeightSlider();
      app.commands.renderFrameCanvas();
      app.commands.updateInfoGrid();
    });
    if (widthSlider) app.lifecycle.listen(widthSlider, "change", () => app.commands.syncConfigToServer());

    const heightSlider = app.state.ui.$("#heightSlider");
    if (heightSlider) app.lifecycle.listen(heightSlider, "input", () => {
      const oldW = app.state.image.frameState.widthMm, oldH = app.state.image.frameState.heightMm;
      app.state.image.frameState.heightMm = app.commands.clampFrameHeight(parseFloat(heightSlider.value));
      app.state.image.lastTouchedDim = "height";
      if (app.state.image.frameState.arMode !== "specified") app.commands.applyARToWidth();
      app.commands.adjustScaleForFrameChange(oldW, oldH, app.state.image.frameState.widthMm, app.state.image.frameState.heightMm);
      app.commands.syncDimFields();
      app.commands.syncWidthSlider();
      app.commands.renderFrameCanvas();
      app.commands.updateInfoGrid();
    });
    if (heightSlider) app.lifecycle.listen(heightSlider, "change", () => app.commands.syncConfigToServer());

    // ── Image adjustment controls ───────────────────────────────────────────
    // B/W | Color toggle
    app.state.ui.$$("#bwColorToggle .toggle-btn").forEach(btn => {
      app.lifecycle.listen(btn, "click", () => {
        app.state.image.imageAdjust.mode = btn.dataset.val;
        app.state.ui.$$("#bwColorToggle .toggle-btn").forEach(b => b.classList.toggle("is-active", b === btn));
        const colorCtrl = app.state.ui.$("#colorControls");
        if (colorCtrl) colorCtrl.style.display = app.state.image.imageAdjust.mode === "bw" ? "none" : "";
        app.commands.renderFrameCanvas();
        app.commands.syncConfigToServer();
      });
    });

    // Reset button — restore all framing + adjustment settings to defaults
    const resetBtn = app.state.ui.$("#frameResetBtn");
    if (resetBtn) app.lifecycle.listen(resetBtn, "click", () => {
      // Reset frame state
      app.state.image.frameState.customRatio = { x: 1, y: 1 };
      app.state.image.frameState.scale = 100.0;
      app.state.image.frameState.rotation = 0;
      app.state.image.frameState.panX = 0;
      app.state.image.frameState.panY = 0;
      app.state.image.frameState.flipH = false;
      app.state.image.frameState.flipV = false;
      if (app.state.image.selectedImage) {
        app.commands.applyImageAspectDefault();   // Stage 11: reset to image aspect (short side 120mm), not a square
      } else {
        app.state.image.frameState.arMode = "specified";
        app.state.image.frameState.widthMm = 100;
        app.state.image.frameState.heightMm = 100;
      }

      // Reset image adjustments
      app.state.image.imageAdjust = {
        mode: "color", exposure: 0, contrast: 0, highlight: 0, shadow: 0,
        tint_hue: 0, tint_strength: 0, saturation: 0, temperature: 0,
      };
      const adjustResets = [
        ["adjustExposure", 0], ["adjustContrast", 0],
        ["adjustHighlight", 0], ["adjustShadow", 0],
        ["adjustTintHue", 0], ["adjustTintStrength", 0],
        ["adjustSaturation", 0], ["adjustTemp", 0],
      ];
      for (const [id, val] of adjustResets) {
        const inp = app.state.ui.$(`#${id}`);
        const sld = app.state.ui.$(`#${id}Slider`);
        if (inp) inp.value = val;
        if (sld) sld.value = val;
      }
      // Reset B/W toggle
      app.state.ui.$$("#bwColorToggle .toggle-btn").forEach(b =>
        b.classList.toggle("is-active", b.dataset.val === "color"));
      const colorCtrl = app.state.ui.$("#colorControls");
      if (colorCtrl) colorCtrl.style.display = "";

      // Sync frame UI
      app.commands.syncScaleSlider();
      app.commands.syncRotationSlider();
      app.commands.syncDimFields();
      app.commands.updateARButtons();
      app.commands.renderFrameCanvas();
      app.commands.updateInfoGrid();
      app.commands.syncConfigToServer();
      app.events.emit("image.controls-reset", { source: "adjustments-reset" });
    });

    // Generic binder for image adjustment sliders
    const adjustPairs = [
      ["adjustExposure", "adjustExposureSlider", "exposure"],
      ["adjustContrast", "adjustContrastSlider", "contrast"],
      ["adjustHighlight", "adjustHighlightSlider", "highlight"],
      ["adjustShadow", "adjustShadowSlider", "shadow"],
      ["adjustTintHue", "adjustTintHueSlider", "tint_hue"],
      ["adjustTintStrength", "adjustTintStrengthSlider", "tint_strength"],
      ["adjustSaturation", "adjustSaturationSlider", "saturation"],
      ["adjustTemp", "adjustTempSlider", "temperature"],
    ];
    adjustPairs.forEach(([inputId, sliderId, key]) => {
      const inp = app.state.ui.$(`#${inputId}`);
      const sld = app.state.ui.$(`#${sliderId}`);
      if (inp && sld) {
        const applyInputValue = () => {
          const value = parseFloat(inp.value);
          if (Number.isNaN(value)) return false;
          app.state.image.imageAdjust[key] = value;
          sld.value = app.state.image.imageAdjust[key];
          app.commands.renderFrameCanvas();
          return true;
        };
        app.lifecycle.listen(sld, "input", () => {
          app.state.image.imageAdjust[key] = parseFloat(sld.value);
          inp.value = app.state.image.imageAdjust[key];
          app.commands.renderFrameCanvas();
        });
        app.lifecycle.listen(sld, "change", () => app.commands.syncConfigToServer());
        app.lifecycle.listen(inp, "input", applyInputValue);
        app.lifecycle.listen(inp, "change", () => {
          if (applyInputValue()) app.commands.syncConfigToServer();
        });
      }
    });

    // ── Frame canvas interaction (pan, zoom, edge-drag, drag-drop) ──────────
    app.commands.initFrameInteraction();

    // Image Library pane state transitions
    const libraryResizeBtn = app.state.ui.$("#libraryResizeBtn");
    if (libraryResizeBtn) app.lifecycle.listen(libraryResizeBtn, "click", () => {
      app.commands.toggleLibraryPaneState();
    });
    const imageLibraryRefreshBtn = app.state.ui.$("#imageLibraryRefreshBtn");
    if (imageLibraryRefreshBtn) app.lifecycle.listen(imageLibraryRefreshBtn, "click", async () => {
      imageLibraryRefreshBtn.disabled = true;
      try {
        await app.commands.startFolderImageRefresh({ announce: true });
      } catch (err) {
        app.commands.showToast(`Refresh failed: ${err.message}`, "error");
      } finally {
        imageLibraryRefreshBtn.disabled = false;
      }
    });
    app.lifecycle.listen(app.state.ui.$("#imageLibraryOpenFolderBtn"), "click", app.commands.handleOpenImageLibraryFolder);
    app.commands.bindImageLibraryWheelScroll();

    // Border toggle switch + inline fields
    const borderToggle = app.state.ui.$("#borderToggle");
    const borderCheck = app.state.ui.$("#cfgBorder");
    if (borderToggle && borderCheck) {
      app.lifecycle.listen(borderToggle, "click", () => {
        borderCheck.checked = !borderCheck.checked;
        app.commands.updateBorderVisibility();
        app.commands.renderBorderHeightWarning();
        app.commands.readConfigFromUI();
        app.commands.updateInfoGrid();
        app.commands.renderFrameCanvas();
        app.commands.renderPreview();
        app.commands.syncConfigToServer();
      });
    }
    const borderW = app.state.ui.$("#cfgBorderWidth");
    if (borderW) app.lifecycle.listen(borderW, "input", () => { app.commands.readConfigFromUI(); app.commands.updateInfoGrid(); app.commands.renderPreview(); });
    if (borderW) app.lifecycle.listen(borderW, "change", () => { app.commands.readConfigFromUI(); app.commands.updateInfoGrid(); app.commands.renderFrameCanvas(); app.commands.syncConfigToServer(); });
    const borderH = app.state.ui.$("#cfgBorderHeight");
    if (borderH) app.lifecycle.listen(borderH, "input", app.commands.renderBorderHeightWarning);
    if (borderH) app.lifecycle.listen(borderH, "change", () => { app.commands.readConfigFromUI(); app.commands.renderBorderHeightWarning(); app.commands.renderFrameCanvas(); app.commands.syncConfigToServer(); });

    // Leading-zero normalization for border numeric inputs
    for (const id of ["cfgBorderWidth", "cfgBorderHeight"]) {
      const el = app.state.ui.$(`#${id}`);
      if (el) app.lifecycle.listen(el, "blur", () => {
        const v = parseFloat(el.value);
        if (!isNaN(v)) el.value = v.toFixed(2);
      });
    }

    // Base/cap filament dropdown
    const bcHandler = () => {
      app.commands.readConfigFromUI();
      app.commands.readPrinterConfig();
      app.commands.updateSuggestSlotHint();
      app.commands.renderCreationTab();
      app.commands.syncConfigToServer();
    };
    const cfgBase = app.state.ui.$("#cfgBaseFilament");
    if (cfgBase) app.lifecycle.listen(cfgBase, "change", bcHandler);

    // ── Creation mode tabs ─────────────────────────────────────────────────
    app.state.ui.$$(".creation-mode-tabs .segmented-btn[data-panel]").forEach((btn) => {
      app.lifecycle.listen(btn, "click", () => app.commands.toggleCreationMode(btn.dataset.panel));
    });

    // Candidate All / None buttons
    const candAll = app.state.ui.$("#candidateSelectAll");
    if (candAll) app.lifecycle.listen(candAll, "click", () => { app.commands.selectAllCandidates(); app.commands.renderCreationTab(); });
    const candNone = app.state.ui.$("#candidateSelectNone");
    if (candNone) app.lifecycle.listen(candNone, "click", () => { app.state.palette.candidateSelection.clear(); app.commands.renderCreationTab(); });

    // Exact palette-size input → update physical capacity context live.
    const paletteColorsInput = app.state.ui.$("#targetFilamentCount");
    if (paletteColorsInput) {
      app.lifecycle.listen(paletteColorsInput, "input", () => app.commands.renderAmsPreview());
      app.lifecycle.listen(paletteColorsInput, "change", () => app.commands.renderAmsPreview());
    }

    // Palette — manual builder actions
    const mintBtn = app.state.ui.$("#mintPaletteBtn");
    if (mintBtn) app.lifecycle.listen(mintBtn, "click", app.commands.mintPaletteToDeck);
    const clearBtn = app.state.ui.$("#clearComposerBtn");
    if (clearBtn) app.lifecycle.listen(clearBtn, "click", app.commands.handleManualSecondaryAction);

    // Palette — deck actions
    const railLoadBtn = app.state.ui.$("#railLoadPaletteBtn");
    if (railLoadBtn) app.lifecycle.listen(railLoadBtn, "click", () => app.commands.showLoadPaletteMenu(railLoadBtn));

    // Staging-pad Clear: empties the staging pad only (never touches the persistent deck).
    const clearDeckBtn = app.state.ui.$("#clearDeckBtn");
    if (clearDeckBtn) app.lifecycle.listen(clearDeckBtn, "click", app.commands.handleStagingClearClick);

    // Persistent-deck Clear (rail): empties the persistent deck only + clears the active palette.
    const railClearDeckBtn = app.state.ui.$("#railClearDeckBtn");
    if (railClearDeckBtn) {
      let confirmPending = false;
      let confirmTimer = null;
      app.lifecycle.listen(railClearDeckBtn, "click", () => {
        if (app.state.solve.batchDeckLocked) {
          app.commands.showToast("The Palette Deck cannot be cleared while a batch is running.", "warn");
          return;
        }
        if (app.state.palette.deck.length === 0) return;
        if (!confirmPending) {
          confirmPending = true;
          railClearDeckBtn.textContent = "Clear?";
          railClearDeckBtn.classList.add("confirm-pending");
          railClearDeckBtn.title = "Click again to clear all palette deck cards";
          railClearDeckBtn.setAttribute("aria-label", "Confirm clearing all palette deck cards");
          confirmTimer = setTimeout(() => {
            confirmTimer = null;
            confirmPending = false;
            railClearDeckBtn.textContent = "Clear";
            railClearDeckBtn.classList.remove("confirm-pending");
            railClearDeckBtn.title = "Remove all palettes from the persistent deck";
            railClearDeckBtn.setAttribute("aria-label", "Clear palette deck");
          }, 3000);
        } else {
          if (confirmTimer) clearTimeout(confirmTimer);
          confirmTimer = null;
          confirmPending = false;
          railClearDeckBtn.textContent = "Clear";
          railClearDeckBtn.classList.remove("confirm-pending");
          railClearDeckBtn.title = "No palettes to clear";
          railClearDeckBtn.setAttribute("aria-label", "No palettes to clear");
          void app.commands.clearPaletteDeck();
        }
      });
    }
    const railDeckList = app.state.ui.$("#railDeckList");
    if (railDeckList) app.lifecycle.listen(railDeckList, "scroll", () => app.commands.hideRailDeckHoverPreview(), { passive: true });
    const tabContentArea = document.querySelector(".tab-content-area");
    if (tabContentArea) app.lifecycle.listen(tabContentArea, "scroll", () => app.commands.hideRailDeckHoverPreview(), { passive: true });
    app.lifecycle.listen(window, "resize", () => {
      app.commands.hideRailDeckHoverPreview();
      app.commands.hideSolveRunHoverPreview();
      app.commands.hideSolveRunSettingsPanel();
    });

    // Palette suggestion
    const suggestBtn = app.state.ui.$("#suggestPalettesBtn");
    if (suggestBtn) app.lifecycle.listen(suggestBtn, "click", app.commands.handleSuggestPalettes);
    const paletteSuggestMode = app.state.ui.$("#paletteSuggestMode");
    if (paletteSuggestMode) {
      app.lifecycle.listen(paletteSuggestMode, "change", () => {
        app.commands.applyPaletteSuggestModeToSettings(paletteSuggestMode.value);
        app.commands.syncConfigToServer();
      });
    }
    const luminanceGuessBtn = app.state.ui.$("#cfgBaseShadingLimitSuggest");
    if (luminanceGuessBtn) app.lifecycle.listen(luminanceGuessBtn, "click", app.commands.handleSuggestBaseShadingLimit);
    // Library filter (modal)
    const railLibBtn = app.state.ui.$("#railLibraryBtn");
    if (railLibBtn) app.lifecycle.listen(railLibBtn, "click", app.commands.openLibraryModal);
    const libModalClose = app.state.ui.$("#libraryModalClose");
    if (libModalClose) app.lifecycle.listen(libModalClose, "click", app.commands.closeLibraryModal);
    const libBackdrop = app.state.ui.$("#libraryModalBackdrop");
    if (libBackdrop) app.lifecycle.listen(libBackdrop, "click", (e) => { if (e.target === libBackdrop) app.commands.closeLibraryModal(); });
    const filterAll = app.state.ui.$("#libraryFilterSelectAll");
    if (filterAll) app.lifecycle.listen(filterAll, "click", app.commands.handleLibraryFilterSelectAll);
    const filterNone = app.state.ui.$("#libraryFilterDeselectAll");
    if (filterNone) app.lifecycle.listen(filterNone, "click", app.commands.handleLibraryFilterDeselectAll);

    // Published Model Libraries manager
    app.lifecycle.listen(app.state.ui.$("#modelLibrariesBtn"), "click", app.commands.openModelLibrariesModal);
    app.lifecycle.listen(app.state.ui.$("#modelLibrariesCloseBtn"), "click", app.commands.closeModelLibrariesModal);
    app.lifecycle.listen(app.state.ui.$("#modelLibrariesRefreshBtn"), "click", () => app.commands.loadModelLibraries());
    app.lifecycle.listen(app.state.ui.$("#modelLibrariesOpenFolderBtn"), "click", app.commands.handleOpenModelLibrariesFolder);
    app.lifecycle.listen(app.state.ui.$("#modelLibraryPackageInput"), "change", event => {
      const file = event.target?.files?.[0];
      if (file) app.commands.handleInstallModelLibrary(file);
    });
    const modelLibrariesModal = app.state.ui.$("#modelLibrariesModal");
    if (modelLibrariesModal) {
      app.lifecycle.listen(modelLibrariesModal, "click", event => {
        if (event.target === modelLibrariesModal) app.commands.closeModelLibrariesModal();
      });
    }

    // Detail drawer (legacy)
    const closeDrawerBtnLegacy = app.state.ui.$("#closeDetailDrawer");
    if (closeDrawerBtnLegacy) app.lifecycle.listen(closeDrawerBtnLegacy, "click", app.commands.closeDetailDrawer);
    const drawerOverlay = app.state.ui.$("#drawerOverlay");
    if (drawerOverlay) app.lifecycle.listen(drawerOverlay, "click", () => {
      // The settings drawer is a persistent overlay with no scrim; only the detail
      // drawer uses #drawerOverlay, so an overlay click only closes that.
      app.commands.closeDetailDrawer();
    });

    // Settings drawer
    const settingsDrawerBtn = app.state.ui.$("#settingsDrawerBtn");
    if (settingsDrawerBtn) app.lifecycle.listen(settingsDrawerBtn, "click", app.commands.toggleSettingsDrawer);
    const closeSettingsDrawerBtn = app.state.ui.$("#closeSettingsDrawer");
    if (closeSettingsDrawerBtn) app.lifecycle.listen(closeSettingsDrawerBtn, "click", app.commands.closeSettingsDrawer);

    const advancedToggle = app.state.ui.$("#settingsAdvancedToggle");
    if (advancedToggle) {
      app.lifecycle.listen(advancedToggle, "click", () => {
        if (
          app.state.settings.settingsAdvancedVisible
          && app.commands.guidePresentationLocked?.("settings-advanced-on")
        ) {
          app.commands.showToast?.("Advanced settings stay on during this guide.", "info");
          return;
        }
        app.state.settings.settingsAdvancedVisible = !app.state.settings.settingsAdvancedVisible;
        app.commands.saveSettingsAdvancedVisible(app.state.settings.settingsAdvancedVisible);
        app.commands.updateAdvancedSettingsVisibility();
        app.commands.distributeSettingsColumns();
        app.events.emit("settings.advanced-changed", {
          visible: app.state.settings.settingsAdvancedVisible,
        });
      });
    }

    // Lightbox close
    const lbClose = app.state.ui.$("#compLightboxClose");
    if (lbClose) app.lifecycle.listen(lbClose, "click", app.commands.closeCompLightbox);
    const lb = app.state.ui.$("#compLightbox");
    if (lb) app.lifecycle.listen(lb, "click", () => app.commands.closeCompLightbox());

    // Solve result cards → lightbox (single delegated dispatcher, keyed on data-solve-card-kind).
    const comparisonGrid = app.state.ui.$("#solveComparisonGrid");
    if (comparisonGrid) {
      app.lifecycle.listen(comparisonGrid, "click", (e) => {
        const card = e.target.closest(".solve-grid-column[data-solve-card-kind]");
        if (card) app.commands.openSolveCardLightboxFromElement(card);
      });
    }

    // Thickness map thumbnails → lightbox (same dispatcher).
    const mapsGrid = app.state.ui.$("#filamentMapsGrid");
    if (mapsGrid) {
      app.lifecycle.listen(mapsGrid, "click", (e) => {
        const card = e.target.closest(".filament-map-card[data-solve-card-kind]");
        if (card) app.commands.openSolveCardLightboxFromElement(card);
      });
    }

    // Accordion
    app.commands.bindAccordions();

    // Settings inputs — auto-sync on change
    app.commands.bindSettingsAutoSyncControls();

    // Solve
    app.lifecycle.listen(app.state.ui.$("#startSolveBtn"), "click", app.commands.handlePrimarySolveAction);

    // Clear solve history
    ["clearSolveHistoryBtn", "exportClearSolveHistoryBtn"].forEach((id) => {
      const btn = app.state.ui.$(`#${id}`);
      if (btn) app.lifecycle.listen(btn, "click", app.commands.handleSolveHistoryClearClick);
    });

    // Clear all temp files (solve runs + LUTs)
    const clearAllBtn = app.state.ui.$("#clearAllTempBtn");
    if (clearAllBtn) {
      app.lifecycle.listen(clearAllBtn, "click", app.commands.clearAllTempFiles);
    }

    // Saved Runs browser (Stage 9b)
    ["savedRunsBtn", "exportSavedRunsBtn"].forEach((id) => {
      const btn = app.state.ui.$(`#${id}`);
      if (btn) app.lifecycle.listen(btn, "click", () => app.commands.openSavedRunsModal("run"));
    });
    const savedRunsCloseBtn = app.state.ui.$("#savedRunsCloseBtn");
    if (savedRunsCloseBtn) app.lifecycle.listen(savedRunsCloseBtn, "click", () => app.commands._setSavedRunsModalOpen(false));
    const savedRunLoadBtn = app.state.ui.$("#savedRunLoadBtn");
    if (savedRunLoadBtn) app.lifecycle.listen(savedRunLoadBtn, "click", app.commands.activateSelectedSavedRun);
    const savedRunLoadSettingsBtn = app.state.ui.$("#savedRunLoadSettingsBtn");
    if (savedRunLoadSettingsBtn) app.lifecycle.listen(savedRunLoadSettingsBtn, "click", () => {
      const selected = app.commands.getSelectedSavedRun();
      if (selected) app.commands.loadSettingsFromSavedRun(selected);
    });
    const savedRunDownloadBtn = app.state.ui.$("#savedRunDownloadBtn");
    if (savedRunDownloadBtn) app.lifecycle.listen(savedRunDownloadBtn, "click", app.commands.downloadSelectedSavedRun);
    const savedRunSaveBtn = app.state.ui.$("#savedRunSaveBtn");
    if (savedRunSaveBtn) app.lifecycle.listen(savedRunSaveBtn, "click", app.commands.promoteSelectedSavedRun);
    const savedRunRenameBtn = app.state.ui.$("#savedRunRenameBtn");
    if (savedRunRenameBtn) app.lifecycle.listen(savedRunRenameBtn, "click", () => {
      const selected = app.commands.getSelectedSavedRun();
      if (selected && selected.tier === "saved") app.commands.openRenameSavedRunDialog(selected);
    });
    const savedRunDeleteBtn = app.state.ui.$("#savedRunDeleteBtn");
    if (savedRunDeleteBtn) app.lifecycle.listen(savedRunDeleteBtn, "click", app.commands.deleteSelectedSavedRun);
    const savedRunUpload = app.state.ui.$("#savedRunUpload");
    if (savedRunUpload) app.lifecycle.listen(savedRunUpload, "click", (event) => {
      if (app.commands.authorizeGuideDurableMutation && !app.commands.authorizeGuideDurableMutation("solve.saved-run.upload", {})) {
        event.preventDefault();
      }
    });
    if (savedRunUpload) app.lifecycle.listen(savedRunUpload, "change", async (e) => {
      const file = e.target.files && e.target.files[0];
      if (!file) return;
      try {
        const body = await app.api.uploadSavedRun(file);
        await app.commands.applyLoadedRun(body);
        app.commands._setSavedRunsModalOpen(false);
        app.commands.showToast("Loaded uploaded run", "");
      } catch (err) { app.commands.showToast(err.message, "error"); }
      finally { e.target.value = ""; }
    });

    // Advanced (demoted) views inline disclosure
    const solveAdvancedToggle = app.state.ui.$("#solveAdvancedToggle");
    if (solveAdvancedToggle) {
      app.lifecycle.listen(solveAdvancedToggle, "click", () => {
        app.commands.setSolveAdvancedViewsOpen(!app.state.solve.solveAdvancedViewsOpen);
      });
    }

    // Solve view toggle (everyday + advanced segments)
    app.state.ui.$$("#solveViewBar .view-toggle-btn").forEach((btn) => {
      app.lifecycle.listen(btn, "click", () => {
        const requestedView = btn.dataset.view;
        app.state.solve.solveView = requestedView === "white_cap" ? app.state.solve.solveWhiteCapView : requestedView;
        if (app.commands.isSolveWhiteCapView(app.state.solve.solveView)) app.state.solve.solveWhiteCapView = app.state.solve.solveView;
        if (
          requestedView === "color_ceiling"
          && !app.state.solve.solveColorRegionsViewWasExplicitlySelected
          && app.commands.shouldDefaultColorRegionsToRecipe()
        ) {
          app.state.solve.solveColorRegionsView = "recipe_regions";
        }
        app.commands.renderSolveComparisonGrid();
      });
    });

    const solveSourceToggle = app.state.ui.$("#solveSourceImageToggle");
    if (solveSourceToggle) {
      app.lifecycle.listen(solveSourceToggle, "click", () => {
        app.state.solve.solveShowSourceImage = !app.state.solve.solveShowSourceImage;
        app.commands.renderSolveComparisonGrid();
      });
    }

    document.querySelectorAll("[data-solve-color-regions-view]").forEach(btn => {
      app.lifecycle.listen(btn, "click", () => {
        app.state.solve.solveColorRegionsViewWasExplicitlySelected = true;
        app.state.solve.solveColorRegionsView = btn.dataset.solveColorRegionsView === "recipe_regions"
          ? "recipe_regions" : "color_ceiling";
        app.commands.renderSolveComparisonGrid();
      });
    });

    document.querySelectorAll("[data-solve-white-cap-view]").forEach(btn => {
      app.lifecycle.listen(btn, "click", () => {
        const nextView = btn.dataset.solveWhiteCapView || "cap_map";
        app.state.solve.solveWhiteCapView = nextView;
        app.state.solve.solveView = nextView;
        app.commands.renderSolveComparisonGrid();
      });
    });

    const solveContoursToggle = app.state.ui.$("#solveContoursToggle");
    if (solveContoursToggle) {
      app.lifecycle.listen(solveContoursToggle, "click", () => {
        app.state.solve.solveContoursEnabled = !app.state.solve.solveContoursEnabled;
        app.commands.updateSolveSubControls();
        app.commands.updateSolveColumnImages();
        app.commands.updateSolveLegend();
      });
    }

    // Cap / Color Diff mode sub-buttons (shared mode; re-rasterises without re-fetching)
    document.querySelectorAll("[data-cap-diff-mode]").forEach((btn) => {
      app.lifecycle.listen(btn, "click", () => {
        app.state.solve.solveCapDiffMode = btn.dataset.capDiffMode;
        document.querySelectorAll("[data-cap-diff-mode]").forEach((b) => {
          const active = b === btn;
          b.classList.toggle("is-active", active);
          b.setAttribute("aria-checked", active ? "true" : "false");
        });
        // Re-rasterise whichever diff canvas is currently mounted.
        const capCanvas = document.querySelector("#solveCapDiffCanvas");
        const filCanvas = document.querySelector("#solveFilamentDiffCanvas");
        if (capCanvas) {
          const diff = app.commands.getCurrentSolveCapDiffFromCache();
          if (diff) app.commands.renderSolveCapDiffCanvas(capCanvas, diff, app.state.solve.solveCapDiffMode);
        } else if (filCanvas) {
          const diff = app.commands.getCurrentSolveFilamentDiffFromCache();
          if (diff) app.commands.renderSolveCapDiffCanvas(filCanvas, diff, app.state.solve.solveCapDiffMode);
        }
        app.commands.updateSolveLegend();
      });
    });

    // Color Diff filament dropdown
    const filDiffSel = app.state.ui.$("#solveFilamentDiffSelect");
    if (filDiffSel) {
      app.lifecycle.listen(filDiffSel, "change", () => {
        app.state.solve.solveFilamentDiffId = filDiffSel.value || "";
        const selected = app.commands.getSelectedRuns().filter((r) => r.results);
        if (selected.length === 2 && app.commands.isSolveFilamentDiffView(app.state.solve.solveView)) {
          app.commands.renderSolveComparisonGrid();
        }
      });
    }

    // Export
    app.lifecycle.listen(app.state.ui.$("#exportFilesBtn"), "click", app.commands.handleExportFiles);
    const exportOutputFormat = app.state.ui.$("#exportOutputFormat");
    if (exportOutputFormat) app.lifecycle.listen(exportOutputFormat, "change", app.commands.handleExportOptionChange);
    const exportGeometrySource = app.state.ui.$("#exportGeometrySource");
    if (exportGeometrySource) app.lifecycle.listen(exportGeometrySource, "change", app.commands.handleExportOptionChange);
    const exportFieldScale = app.state.ui.$("#exportFieldScale");
    if (exportFieldScale) app.lifecycle.listen(exportFieldScale, "change", app.commands.handleExportOptionChange);
    app.lifecycle.listen(app.state.ui.$("#copySwapBtn"), "click", () => {
      const text = app.state.ui.$("#swapInstructions").textContent;
      navigator.clipboard.writeText(text).then(
        () => app.commands.showToast("Copied to clipboard", "success"),
        () => app.commands.showToast("Copy failed", "error")
      );
    });

    // Copy-path buttons in the export file list (delegated)
    const fileListDiv = app.state.ui.$("#exportFileList");
    if (fileListDiv) {
      app.lifecycle.listen(fileListDiv, "click", (e) => {
        const openBtn = e.target.closest(".open-export-folder-btn[data-export-id]");
        if (openBtn) {
          e.preventDefault();
          app.api.openExportFolder(openBtn.dataset.exportId)
            .then(() => app.commands.showToast("Opened export folder", "success"))
            .catch((err) => app.commands.showToast(`The export folder could not be opened: ${err.message}`, "error"));
          return;
        }
        const btn = e.target.closest(".copy-path-btn[data-copy-path]");
        if (!btn) return;
        e.preventDefault();
        app.commands.copyToClipboard(btn.dataset.copyPath);
      });
    }

    // Download All → zip of current output dir
    const downloadAllBtn = app.state.ui.$("#downloadAllBtn");
    if (downloadAllBtn) {
      app.lifecycle.listen(downloadAllBtn, "click", () => {
        if (downloadAllBtn.disabled) return;
        const url = app.commands.getSelectedExportResult()?.zip_url;
        if (!url) {
          app.commands.showToast("Export files first", "error");
          return;
        }
        window.location.href = url;
      });
    }

    // ── Settings Profile controls ──────────────────────────────────────────
    const profileBrowse = app.state.ui.$("#settingsProfileBrowseBtn");
    if (profileBrowse) app.lifecycle.listen(profileBrowse, "click", app.commands.handleSettingsProfilesBrowse);
    const profileSave = app.state.ui.$("#settingsProfileSaveBtn");
    if (profileSave) app.lifecycle.listen(profileSave, "click", app.commands.handleSettingsProfileSave);
    const profileSaveAs = app.state.ui.$("#settingsProfileSaveAsBtn");
    if (profileSaveAs) app.lifecycle.listen(profileSaveAs, "click", app.commands.handleSettingsProfileSaveAs);

    // ── Printer configuration ────────────────────────────────────────────────
    const pcBtn = app.state.ui.$("#printerConfigBtn");
    if (pcBtn) app.lifecycle.listen(pcBtn, "click", () => {
      const page = app.state.ui.$("#printerConfigPage");
      if (page && !page.classList.contains("is-hidden")) {
        app.commands.hidePrinterConfigPage();
      } else {
        app.commands.showPrinterConfigPage();
      }
    });
    const pcClose = app.state.ui.$("#printerConfigClose");
    if (pcClose) app.lifecycle.listen(pcClose, "click", () => app.commands.hidePrinterConfigPage());
    // Click outside card (on the dimmed backdrop) closes config
    const pcPage = app.state.ui.$("#printerConfigPage");
    if (pcPage) app.lifecycle.listen(pcPage, "click", (e) => {
      if (e.target === pcPage) app.commands.hidePrinterConfigPage();
    });

    const pcNew = app.state.ui.$("#pcNewPrinterBtn");
    if (pcNew) app.lifecycle.listen(pcNew, "click", () => {
      if (!app.commands._readPrinterFromConfigPage()) return;
      app.commands.resetPrinterDeleteConfirm();
      const id = "printer-" + Date.now();
      const draft = app.commands.printerConfigData();
      const printer = app.commands.createDefaultPrinterProfile(id);
      draft.printers.push(printer);
      draft.printer_setup_state[id] = app.commands.createDefaultPrinterSetup(printer);
      app.state.session.printerConfigEditingId = id;
      draft.active_printer_id = id;
      app.commands.renderPrinterConfigPage();
      // Focus the name field for immediate editing
      const nameField = app.state.ui.$("#pcName");
      if (nameField) { nameField.focus(); nameField.select(); }
    });

    const pcDelete = app.state.ui.$("#pcDeletePrinterBtn");
    if (pcDelete) app.lifecycle.listen(pcDelete, "click", () => {
      const delId = app.commands.currentPrinterConfigId();
      if (!delId) return;
      if (!app.state.session.printerDeleteConfirmPending) {
        app.state.session.printerDeleteConfirmPending = true;
        pcDelete.textContent = "Confirm?";
        pcDelete.classList.add("confirm-pending");
        pcDelete.title = "Click again to delete selected printer";
        app.state.session.printerDeleteConfirmTimer = setTimeout(app.commands.resetPrinterDeleteConfirm, 2200);
        return;
      }
      app.commands.resetPrinterDeleteConfirm();
      const draft = app.commands.printerConfigData();
      draft.printers = draft.printers.filter(p => p.id !== delId);
      delete draft.printer_setup_state?.[delId];
      app.state.session.printerConfigEditingId = draft.printers[0]?.id || null;
      draft.active_printer_id = app.state.session.printerConfigEditingId;
      app.commands.syncPrinterConfigSetupState(draft);
      app.commands.renderPrinterConfigPage();
    });

    const pcAddNozzle = app.state.ui.$("#pcAddNozzleBtn");
    if (pcAddNozzle) app.lifecycle.listen(pcAddNozzle, "click", () => {
      const printer = app.commands.printerConfigData()?.printers?.find(p => p.id === app.commands.currentPrinterConfigId());
      if (!printer) return;
      if (!app.commands._readPrinterFromConfigPage()) return;
      app.commands.addNozzleProfileToDraft(printer);
      app.commands.renderPrinterConfigPage();
    });

    const pcDiscard = app.state.ui.$("#pcDiscardBtn");
    if (pcDiscard) app.lifecycle.listen(pcDiscard, "click", app.commands.discardPrinterConfigDraft);

    // Keyboard: Escape closes drawers/modals/config page
    app.lifecycle.listen(document, "keydown", (e) => {
      const tag = e.target?.tagName;
      const isTypingTarget = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
      if (!isTypingTarget && ["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(e.key)) {
        if (app.commands.navigateSolveLightbox(e.key)) {
          e.preventDefault();
          return;
        }
      }
      if (e.key === "Escape") {
        if (app.state.solve._lightboxIdx !== -1 || app.state.solve._solveLightboxState) { app.commands.closeCompLightbox(); return; }
        const modelLibrariesModal = app.state.ui.$("#modelLibrariesModal");
        if (modelLibrariesModal && !modelLibrariesModal.classList.contains("is-hidden")) {
          app.commands.closeModelLibrariesModal();
          return;
        }
        const pcPage = app.state.ui.$("#printerConfigPage");
        if (pcPage && !pcPage.classList.contains("is-hidden")) {
          app.commands.hidePrinterConfigPage();
          return;
        }
        const ratioDialog = app.state.ui.$("#ratioDialog");
        if (ratioDialog && ratioDialog.getAttribute("aria-hidden") === "false") {
          app.commands.closeRatioDialog();
          return;
        }
        const libModal = app.state.ui.$("#libraryModalBackdrop");
        if (libModal && !libModal.classList.contains("is-hidden")) {
          app.commands.closeLibraryModal();
          return;
        }
        const renameSavedRunModal = app.state.ui.$("#renameSavedRunModal");
        if (renameSavedRunModal && !renameSavedRunModal.classList.contains("is-hidden")) {
          return;
        }
        const savedRunsModal = app.state.ui.$("#savedRunsModal");
        if (savedRunsModal && !savedRunsModal.classList.contains("is-hidden")) {
          app.commands._setSavedRunsModalOpen(false);
          return;
        }
        const profileModal = app.state.ui.$("#settingsProfileModal");
        if (profileModal && !profileModal.classList.contains("is-hidden")) {
          app.state.ui.$("#settingsProfileModalClose")?.click();
          return;
        }
        if (app.state.settings.settingsDrawerOpen) {
          app.commands.closeSettingsDrawer();
          return;
        }
        const drawer = app.state.ui.$("#detailDrawer");
        if (drawer && drawer.getAttribute("aria-hidden") === "false") {
          app.commands.closeDetailDrawer();
        }
      }
    });
  }

  Object.assign(app.commands, {
    bindEvents,
    clearAllTempFiles,
  });
}
