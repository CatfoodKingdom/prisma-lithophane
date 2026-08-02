/**
 * Install the printers/index feature commands.
 * @param {import("../../core/types.js").ApplicationContext} app
 */
export function installFeaturesPrintersIndex(app) {
  function applyAuthoritativePrinterState(printersData, active) {
    if (active?.printer?.id) {
      printersData.active_printer_id = active.printer.id;
    }
    printersData.active_nozzle_size = active?.nozzle?.size ?? null;
    app.state.session.printersData = printersData;
    const printer = active?.printer || null;
    app.state.session.activeNozzle = active?.nozzle || null;
    app.state.session.activePrintability = active?.printability || null;
    app.state.settings.config.printability_minimum_extrusion_width_mm =
      active?.printability?.minimum_extrusion_width_mm ?? null;
    app.state.settings.config.printability_minimum_line_length_mm =
      active?.printability?.minimum_line_length_mm ?? null;
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
  }

  async function loadPrinters() {
    try {
      const printersData = await app.api.fetchPrinters();
      const active = await app.api.fetchActivePrinter();
      app.commands.applyAuthoritativePrinterState(printersData, active);
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
    app.events.emit("printer-config.opened", { source: "printer-configuration" });
  }

  async function hidePrinterConfigPage(navigateTo) {
    // Auto-save on exit
    if (!app.commands._readPrinterFromConfigPage()) return false;
    if (app.state.session.printerConfigEditingId && app.state.session.printersData?.printers?.some(p => p.id === app.state.session.printerConfigEditingId)) {
      app.state.session.printersData.active_printer_id = app.state.session.printerConfigEditingId;
      app.commands.syncPrinterConfigActiveNozzle();
    }
    let saved;
    try {
      saved = await app.api.savePrinters(app.state.session.printersData);
    } catch (e) {
      app.commands.showToast("Failed to save printer config: " + e.message, "error");
      return false;
    }
    const printersData = {
      printers: saved.printers || [],
      active_printer_id: saved.active_printer_id,
      active_nozzle_size: saved.active_nozzle_size,
    };
    try {
      app.commands.applyAuthoritativePrinterState(printersData, saved.active);
    } catch (e) {
      console.error("[printers] saved state could not be rendered:", e);
      app.commands.showToast(
        "Printer config was saved, but the display could not refresh. Reopen Printer Configuration.",
        "error",
      );
      return false;
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
    app.events.emit("printer.active-changed", {
      printerId: saved.active_printer_id || null,
      source: "printer-configuration",
    });
    app.events.emit("printer-config.closed", { source: "printer-configuration" });
    return true;
  }

  function defaultNozzleLineWidths(size) {
    const nozzleSize = parseFloat(size) || 0.4;
    if (Math.abs(nozzleSize - 0.2) < 1e-6) {
      return {
        line_width: 0.22,
        max_line_width: 0.25,
        min_line_length_multiplier: 2,
      };
    }
    if (Math.abs(nozzleSize - 0.4) < 1e-6) {
      return {
        line_width: 0.42,
        max_line_width: 0.5,
        min_line_length_multiplier: 2,
      };
    }
    return {
      line_width: Math.round(nozzleSize * 1.05 * 100) / 100,
      max_line_width: Math.round(nozzleSize * 1.25 * 100) / 100,
      min_line_length_multiplier: 2,
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
    let maxLineWidth = parseFloat(nozzle.max_line_width);
    if (!Number.isFinite(maxLineWidth)) maxLineWidth = defaults.max_line_width;
    maxLineWidth = Math.max(size, maxLineWidth);
    let lineWidth = parseFloat(nozzle.line_width);
    if (!Number.isFinite(lineWidth)) lineWidth = defaults.line_width;
    lineWidth = Math.min(Math.max(lineWidth, size), maxLineWidth);
    const rawMultiplier = Number(nozzle.min_line_length_multiplier);
    const minLineLengthMultiplier = Number.isInteger(rawMultiplier)
      ? rawMultiplier
      : defaults.min_line_length_multiplier;
    return {
      size,
      min_layer_height: minLayerHeight,
      max_layer_height: maxLayerHeight,
      line_width: lineWidth,
      max_line_width: maxLineWidth,
      min_line_length_multiplier: minLineLengthMultiplier,
    };
  }

  function formatNozzleDerivedLengthMm(size, multiplier) {
    const canonicalSize = Number(Number(size).toFixed(6));
    const value = canonicalSize * Number(multiplier);
    if (!Number.isFinite(value)) return "\u2014";
    return value.toFixed(6).replace(/\.?0+$/, "");
  }

  function syncNozzleDerivedLength(row) {
    const size = Number(row?.querySelector(".nz-size")?.value);
    const multiplier = Number(row?.querySelector(".nz-min-ll-mult")?.value);
    const output = row?.querySelector(".nz-min-ll-derived");
    if (output) {
      const resolved = app.commands.formatNozzleDerivedLengthMm(size, multiplier);
      output.textContent = resolved === "\u2014"
        ? "\u00d7 nozzle = \u2014"
        : `\u00d7 nozzle = ${resolved} mm`;
    }
  }

  function validateNozzleRow(row) {
    const sizeInput = row?.querySelector(".nz-size");
    const multiplierInput = row?.querySelector(".nz-min-ll-mult");
    const size = Number(sizeInput?.value);
    const multiplier = Number(multiplierInput?.value);
    const sizeValid = Number.isFinite(size) && size > 0;
    const multiplierValid = Number.isInteger(multiplier) && multiplier >= 2 && multiplier <= 10;
    sizeInput?.setCustomValidity(sizeValid ? "" : "Nozzle size must be a positive number.");
    multiplierInput?.setCustomValidity(
      multiplierValid ? "" : "Minimum line length must be a whole number from 2 through 10 nozzle diameters.",
    );
    row?.classList.toggle("is-invalid", !(sizeValid && multiplierValid));
    return sizeValid && multiplierValid;
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
    if (!nextId || nextId === app.state.session.printerConfigEditingId) return true;
    if (!app.commands._readPrinterFromConfigPage()) return false;
    app.commands.resetPrinterDeleteConfirm();
    app.state.session.printerConfigEditingId = nextId;
    app.state.session.printersData.active_printer_id = nextId;
    app.commands.syncPrinterConfigActiveNozzle();
    return true;
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
        if (!app.commands.selectPrinterConfigId(sel.value)) {
          sel.value = app.state.session.printerConfigEditingId || "";
          return;
        }
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
          <td><input type="number" class="nz-size" value="${n.size}" step="0.1" min="0.1" max="1.0" aria-label="Nozzle diameter in millimeters"></td>
          <td><input type="number" class="nz-min-lh" value="${n.min_layer_height}" step="0.01" min="0.01" max="1.0"></td>
          <td><input type="number" class="nz-max-lh" value="${n.max_layer_height}" step="0.01" min="0.01" max="1.0"></td>
          <td>
            <div class="nz-min-ll-cell">
              <input type="number" class="nz-min-ll-mult" value="${n.min_line_length_multiplier}" step="1" min="2" max="10"
                aria-label="Minimum line length in nozzle diameters" aria-describedby="pcNozzleLengthHelp nzMinLenHelp-${i}">
              <span class="nz-min-ll-derived" id="nzMinLenHelp-${i}">× nozzle = ${app.commands.formatNozzleDerivedLengthMm(n.size, n.min_line_length_multiplier)} mm</span>
            </div>
          </td>
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
      tbody.querySelectorAll("tr").forEach(row => {
        const sync = () => {
          app.commands.validateNozzleRow(row);
          app.commands.syncNozzleDerivedLength(row);
        };
        row.querySelector(".nz-size")?.addEventListener("input", sync);
        row.querySelector(".nz-min-ll-mult")?.addEventListener("input", sync);
        sync();
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
    const invalidRow = Array.from(rows).find(row => !app.commands.validateNozzleRow(row));
    if (invalidRow) {
      const invalidInput = invalidRow.querySelector(":invalid");
      invalidInput?.focus();
      invalidInput?.reportValidity();
      app.commands.showToast("Fix the invalid nozzle profile before saving.", "error");
      return null;
    }
    printer.nozzle_profiles = Array.from(rows).map(row => {
      const index = Number(row.dataset.idx);
      const previous = printer.nozzle_profiles[index] || {};
      const size = Number(row.querySelector(".nz-size")?.value);
      const defaults = app.commands.defaultNozzleLineWidths(size);
      const maxLineWidth = Math.max(size, Number(previous.max_line_width) || defaults.max_line_width);
      const lineWidth = Math.min(
        Math.max(Number(previous.line_width) || defaults.line_width, size),
        maxLineWidth,
      );
      return {
        size,
        min_layer_height: parseFloat(row.querySelector(".nz-min-lh")?.value) || 0.08,
        max_layer_height: parseFloat(row.querySelector(".nz-max-lh")?.value) || 0.32,
        line_width: lineWidth,
        max_line_width: maxLineWidth,
        min_line_length_multiplier: Number(row.querySelector(".nz-min-ll-mult")?.value),
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
    app.events.emit("image.adjustment-tab.changed", { tab });
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
    const sourceNotice = app.state.ui.$("#savedRunSourceNotice");
    const sourceNoticeName = app.state.ui.$("#savedRunSourceNoticeName");
    const privateSource = app.state.image.selectedImage?.source_ref
      ? app.state.image.selectedImage
      : null;
    sourceNotice?.classList.toggle("is-hidden", !privateSource);
    if (sourceNoticeName) {
      sourceNoticeName.textContent = privateSource?.filename || "";
      sourceNoticeName.title = privateSource?.filename || "";
    }
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
    applyAuthoritativePrinterState,
    loadPrinters,
    showPrinterConfigPage,
    hidePrinterConfigPage,
    defaultNozzleLineWidths,
    normalizeNozzleProfile,
    formatNozzleDerivedLengthMm,
    syncNozzleDerivedLength,
    validateNozzleRow,
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
