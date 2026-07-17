/**
 * Install the printers/index feature commands.
 * @param {import("../../core/types.js").ApplicationContext} app
 */
export function installFeaturesPrintersIndex(app) {
  async function loadPrinters() {
    try {
      app.state.session.printersData = await app.api.fetchPrinters();
      const active = await app.api.fetchActivePrinter();
      const printer = active.printer;
      app.state.session.activeNozzle = active.nozzle;
      if (printer) {
        app.state.session.printerConfig.name = printer.name;
        app.state.session.printerConfig.max_x_mm = printer.max_print_area?.x || 256;
        app.state.session.printerConfig.max_y_mm = printer.max_print_area?.y || 256;
        app.state.session.printerConfig.ams_units = printer.ams_units || 1;
        app.state.session.printerConfig.slots_per_unit = printer.slots_per_ams || 4;
        app.state.session.printerConfig.ams_slots = app.state.session.printerConfig.ams_units * app.state.session.printerConfig.slots_per_unit;
      }
      app.commands.renderPrinterRail();
      app.commands.updateDerivedParams();
    } catch (e) {
      console.warn("[printers] load failed:", e.message);
    }
  }

  function showPrinterConfigPage() {
    // The settings drawer is a content-area overlay; close it so printer config isn't behind it.
    if (app.state.settings.settingsDrawerOpen) app.commands.closeSettingsDrawer();
    // Record which tab the user came from
    app.state.session.printerConfigOriginTab = app.state.ui.currentTab;
    // Dim the origin tab (still visually marked, but muted)
    app.state.ui.$$(".mode-button").forEach(btn => {
      btn.classList.remove("is-active");
      btn.classList.toggle("is-dimmed", btn.dataset.tab === app.state.session.printerConfigOriginTab);
    });
    // Show config overlay on top of current tab content (don't hide it)
    const page = app.state.ui.$("#printerConfigPage");
    if (page) page.classList.remove("is-hidden");
    app.state.session.printerConfigEditingId = app.state.session.printersData?.active_printer_id || app.state.session.printersData?.printers?.[0]?.id || null;
    app.commands.renderPrinterConfigPage();
  }

  async function hidePrinterConfigPage(navigateTo) {
    // Auto-save on exit
    app.commands._readPrinterFromConfigPage();
    if (app.state.session.printerConfigEditingId && app.state.session.printersData?.printers?.some(p => p.id === app.state.session.printerConfigEditingId)) {
      app.state.session.printersData.active_printer_id = app.state.session.printerConfigEditingId;
      app.commands.syncPrinterConfigActiveNozzle();
    }
    try {
      await app.api.savePrinters(app.state.session.printersData);
      await app.commands.loadPrinters();
    } catch (e) {
      app.commands.showToast("Failed to save printer config: " + e.message, "error");
    }
    const page = app.state.ui.$("#printerConfigPage");
    if (page) page.classList.add("is-hidden");
    // Clear dimmed state
    app.state.ui.$$(".mode-button").forEach(btn => btn.classList.remove("is-dimmed"));
    // Navigate to the requested tab (or back to origin)
    const target = navigateTo || app.state.session.printerConfigOriginTab || app.state.ui.currentTab;
    app.state.session.printerConfigOriginTab = null;
    app.state.session.printerConfigEditingId = null;
    app.commands.switchTab(target);
  }

  function defaultNozzleLineWidths(size) {
    const nozzleSize = parseFloat(size) || 0.4;
    if (Math.abs(nozzleSize - 0.2) < 1e-6) {
      return {
        line_width: 0.22,
        min_line_width: 0.16,
        max_line_width: 0.25,
        min_line_length: 0.40,
      };
    }
    if (Math.abs(nozzleSize - 0.4) < 1e-6) {
      return {
        line_width: 0.42,
        min_line_width: 0.32,
        max_line_width: 0.5,
        min_line_length: 0.50,
      };
    }
    const minLineWidth = Math.round(nozzleSize * 0.8 * 100) / 100;
    const minLineLength = Math.round(Math.max(0.40, minLineWidth + 0.10) * 100) / 100;
    return {
      line_width: Math.round(nozzleSize * 1.05 * 100) / 100,
      min_line_width: minLineWidth,
      max_line_width: Math.round(nozzleSize * 1.25 * 100) / 100,
      min_line_length: minLineLength,
    };
  }

  function normalizeNozzleProfile(profile) {
    const nozzle = { ...(profile || {}) };
    const size = parseFloat(nozzle.size) || 0.4;
    const defaults = app.commands.defaultNozzleLineWidths(size);
    let minLayerHeight = parseFloat(nozzle.min_layer_height);
    let maxLayerHeight = parseFloat(nozzle.max_layer_height);
    if (!Number.isFinite(minLayerHeight) || minLayerHeight <= 0) minLayerHeight = 0.08;
    if (!Number.isFinite(maxLayerHeight) || maxLayerHeight <= 0) maxLayerHeight = 0.32;
    if (minLayerHeight > maxLayerHeight) [minLayerHeight, maxLayerHeight] = [maxLayerHeight, minLayerHeight];
    let minLineWidth = parseFloat(nozzle.min_line_width);
    let maxLineWidth = parseFloat(nozzle.max_line_width);
    if (!Number.isFinite(minLineWidth)) minLineWidth = defaults.min_line_width;
    if (!Number.isFinite(maxLineWidth)) maxLineWidth = defaults.max_line_width;
    if (minLineWidth > maxLineWidth) [minLineWidth, maxLineWidth] = [maxLineWidth, minLineWidth];
    let lineWidth = parseFloat(nozzle.line_width);
    if (!Number.isFinite(lineWidth)) lineWidth = defaults.line_width;
    lineWidth = Math.min(Math.max(lineWidth, minLineWidth), maxLineWidth);
    let minLineLength = parseFloat(nozzle.min_line_length);
    if (!Number.isFinite(minLineLength) || minLineLength <= 0) {
      minLineLength = defaults.min_line_length;
    }
    return {
      size,
      min_layer_height: minLayerHeight,
      max_layer_height: maxLayerHeight,
      line_width: lineWidth,
      min_line_width: minLineWidth,
      max_line_width: maxLineWidth,
      min_line_length: minLineLength,
    };
  }

  function currentPrinterConfigId() {
    const printers = app.state.session.printersData?.printers || [];
    if (!printers.length) {
      app.state.session.printerConfigEditingId = null;
      return null;
    }
    if (!app.state.session.printerConfigEditingId || !printers.some(p => p.id === app.state.session.printerConfigEditingId)) {
      app.state.session.printerConfigEditingId = app.state.session.printersData.active_printer_id || printers[0].id;
    }
    if (!printers.some(p => p.id === app.state.session.printerConfigEditingId)) {
      app.state.session.printerConfigEditingId = printers[0].id;
    }
    return app.state.session.printerConfigEditingId;
  }

  function syncPrinterConfigActiveNozzle() {
    const printer = (app.state.session.printersData?.printers || []).find(p => p.id === app.state.session.printerConfigEditingId);
    const profiles = printer?.nozzle_profiles || [];
    if (!profiles.some(n => Number(n.size) === Number(app.state.session.printersData.active_nozzle_size))) {
      app.state.session.printersData.active_nozzle_size = profiles.length ? Number(profiles[0].size) : null;
    }
  }

  function selectPrinterConfigId(nextId) {
    if (!nextId || nextId === app.state.session.printerConfigEditingId) return;
    app.commands._readPrinterFromConfigPage();
    app.commands.resetPrinterDeleteConfirm();
    app.state.session.printerConfigEditingId = nextId;
    app.state.session.printersData.active_printer_id = nextId;
    app.commands.syncPrinterConfigActiveNozzle();
  }

  function resetPrinterDeleteConfirm() {
    if (app.state.session.printerDeleteConfirmTimer) {
      clearTimeout(app.state.session.printerDeleteConfirmTimer);
      app.state.session.printerDeleteConfirmTimer = null;
    }
    app.state.session.printerDeleteConfirmPending = false;
    const btn = app.state.ui.$("#pcDeletePrinterBtn");
    if (btn) {
      btn.textContent = "Delete";
      btn.classList.remove("confirm-pending");
      btn.title = "Delete selected printer";
    }
  }

  function updatePrinterConfigDropdownLabel(printerId, label) {
    const sel = app.state.ui.$("#pcPrinterSelect");
    const option = Array.from(sel?.options || []).find(opt => opt.value === String(printerId));
    if (option) option.textContent = label;
  }

  function renderPrinterConfigPage() {
    if (!app.state.session.printersData) return;
    const printers = app.state.session.printersData.printers || [];
    const selectedId = app.commands.currentPrinterConfigId();

    // Printer selector
    const sel = app.state.ui.$("#pcPrinterSelect");
    if (sel) {
      sel.innerHTML = printers.map(p =>
        `<option value="${p.id}"${p.id === selectedId ? " selected" : ""}>${app.commands.esc(p.name)}</option>`
      ).join("");
      sel.value = selectedId || "";
      sel.onchange = () => {
        app.commands.selectPrinterConfigId(sel.value);
        app.commands.renderPrinterConfigPage();
      };
    }

    const printer = printers.find(p => p.id === selectedId);
    if (!printer) return;

    // Fill fields
    const pcName = app.state.ui.$("#pcName");
    if (pcName) {
      pcName.value = printer.name;
      pcName.oninput = () => {
        const nextName = (pcName.value || "").trim() || "New Printer";
        printer.name = nextName;
        app.commands.updatePrinterConfigDropdownLabel(printer.id, nextName);
      };
    }
    const pcAreaX = app.state.ui.$("#pcAreaX");
    if (pcAreaX) pcAreaX.value = printer.max_print_area?.x || 256;
    const pcAreaY = app.state.ui.$("#pcAreaY");
    if (pcAreaY) pcAreaY.value = printer.max_print_area?.y || 256;
    const pcAmsUnits = app.state.ui.$("#pcAmsUnits");
    if (pcAmsUnits) pcAmsUnits.value = printer.ams_units || 1;
    const pcSlotsPerAms = app.state.ui.$("#pcSlotsPerAms");
    if (pcSlotsPerAms) pcSlotsPerAms.value = printer.slots_per_ams || 4;

    // Nozzle table
    const tbody = app.state.ui.$("#pcNozzleBody");
    if (tbody) {
      tbody.innerHTML = (printer.nozzle_profiles || []).map((profile, i) => {
        const n = app.commands.normalizeNozzleProfile(profile);
        return `
        <tr data-idx="${i}">
          <td><input type="number" class="nz-size" value="${n.size}" step="0.1" min="0.1" max="1.0"></td>
          <td><input type="number" class="nz-min-lh" value="${n.min_layer_height}" step="0.01" min="0.01" max="1.0"></td>
          <td><input type="number" class="nz-max-lh" value="${n.max_layer_height}" step="0.01" min="0.01" max="1.0"></td>
          <td><input type="number" class="nz-min-lw" value="${n.min_line_width}" step="0.01" min="0.05" max="2.0"></td>
          <td><input type="number" class="nz-min-ll" value="${n.min_line_length}" step="0.01" min="0.05" max="10.0"></td>
          <td><button class="ghost-button xs danger nz-delete" data-idx="${i}" aria-label="Delete nozzle profile" title="Delete nozzle profile">${app.commands.xIconSvg()}</button></td>
        </tr>
      `;
      }).join("");

      tbody.querySelectorAll(".nz-delete").forEach(btn => {
        btn.addEventListener("click", () => {
          const idx = parseInt(btn.dataset.idx);
          printer.nozzle_profiles.splice(idx, 1);
          app.commands.renderPrinterConfigPage();
        });
      });
    }

    // Delete printer button visibility
    const delBtn = app.state.ui.$("#pcDeletePrinterBtn");
    if (delBtn) {
      delBtn.style.display = printers.length > 1 ? "" : "none";
      if (!app.state.session.printerDeleteConfirmPending) {
        delBtn.textContent = "Delete";
        delBtn.title = "Delete selected printer";
        delBtn.classList.remove("confirm-pending");
      }
    }
  }

  function _readPrinterFromConfigPage() {
    const printers = app.state.session.printersData.printers || [];
    const sel = app.state.ui.$("#pcPrinterSelect");
    const selectedId = app.state.session.printerConfigEditingId || (sel ? sel.value : app.state.session.printersData.active_printer_id);
    const printer = printers.find(p => p.id === selectedId);
    if (!printer) return null;

    printer.name = (app.state.ui.$("#pcName")?.value || printer.name).trim();
    printer.max_print_area = {
      x: parseFloat(app.state.ui.$("#pcAreaX")?.value) || 256,
      y: parseFloat(app.state.ui.$("#pcAreaY")?.value) || 256,
    };
    printer.ams_units = parseInt(app.state.ui.$("#pcAmsUnits")?.value) || 1;
    printer.slots_per_ams = parseInt(app.state.ui.$("#pcSlotsPerAms")?.value) || 4;

    // Read nozzle rows from table
    const rows = app.state.ui.$$("#pcNozzleBody tr");
    printer.nozzle_profiles = Array.from(rows).map(row => {
      const size = parseFloat(row.querySelector(".nz-size")?.value) || 0.4;
      const defaults = app.commands.defaultNozzleLineWidths(size);
      let minLineWidth = parseFloat(row.querySelector(".nz-min-lw")?.value);
      if (!Number.isFinite(minLineWidth)) minLineWidth = defaults.min_line_width;
      const maxLineWidth = Math.max(defaults.max_line_width, minLineWidth);
      let lineWidth = defaults.line_width;
      lineWidth = Math.min(Math.max(lineWidth, minLineWidth), maxLineWidth);
      let minLineLength = parseFloat(row.querySelector(".nz-min-ll")?.value);
      if (!Number.isFinite(minLineLength) || minLineLength <= 0) {
        minLineLength = defaults.min_line_length;
      }
      return {
        size,
        min_layer_height: parseFloat(row.querySelector(".nz-min-lh")?.value) || 0.08,
        max_layer_height: parseFloat(row.querySelector(".nz-max-lh")?.value) || 0.32,
        line_width: lineWidth,
        min_line_width: minLineWidth,
        max_line_width: maxLineWidth,
        min_line_length: minLineLength,
      };
    });

    return printer;
  }

  function switchFrameEditorTab(tab) {
    app.state.image.frameEditorTab = tab;
    app.state.ui.$$(".frame-tab").forEach(btn => btn.classList.toggle("is-active", btn.dataset.ftab === tab));
    const sizeCtrl = app.state.ui.$("#frameControlsSize");
    const imgCtrl = app.state.ui.$("#frameControlsImage");
    if (sizeCtrl) sizeCtrl.classList.toggle("is-hidden", tab !== "size");
    if (imgCtrl) imgCtrl.classList.toggle("is-hidden", tab !== "image");
    // Lock canvas interaction in Image mode
    const canvas = app.state.ui.$("#frameCanvas");
    if (canvas) canvas.classList.toggle("interaction-locked", tab === "image");
  }

  function setLibraryPaneState(state) {
    app.state.image.libraryPaneState = state;
    const panel = app.state.ui.$("#imageLibraryPanel");
    if (panel) panel.dataset.state = state;
    const resizeBtn = app.state.ui.$("#libraryResizeBtn");
    if (resizeBtn) {
      const expanded = state === "expanded";
      const label = expanded ? "Compact image library" : "Expand image library";
      resizeBtn.innerHTML = app.commands.panelResizeIconSvg(expanded);
      resizeBtn.setAttribute("aria-pressed", expanded ? "true" : "false");
      resizeBtn.title = label;
      resizeBtn.setAttribute("aria-label", resizeBtn.title);
    }
  }

  function toggleLibraryPaneState() {
    app.commands.setLibraryPaneState(app.state.image.libraryPaneState === "expanded" ? "contracted" : "expanded");
  }

  function renderImageTab() {
    app.commands.setLibraryPaneState(app.state.image.libraryPaneState);
    app.commands.renderImageGrid();
    app.commands.renderFrameCanvas();
    app.commands.renderPreview();
    app.commands.updateInfoGrid();
    app.commands.updateBorderVisibility();
    app.commands.syncDimFields();
    app.commands.syncScaleSlider();
    app.commands.syncRotationSlider();
    app.commands.updateARButtons();
    app.commands.switchFrameEditorTab(app.state.image.frameEditorTab);
    app.commands.syncWidthSlider();
    app.commands.syncHeightSlider();
  }

  Object.assign(app.commands, {
    loadPrinters,
    showPrinterConfigPage,
    hidePrinterConfigPage,
    defaultNozzleLineWidths,
    normalizeNozzleProfile,
    currentPrinterConfigId,
    syncPrinterConfigActiveNozzle,
    selectPrinterConfigId,
    resetPrinterDeleteConfirm,
    updatePrinterConfigDropdownLabel,
    renderPrinterConfigPage,
    _readPrinterFromConfigPage,
    switchFrameEditorTab,
    setLibraryPaneState,
    toggleLibraryPaneState,
    renderImageTab,
  });}
