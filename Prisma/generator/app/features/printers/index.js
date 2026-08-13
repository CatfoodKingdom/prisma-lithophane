/**
 * Install the printers/index feature commands.
 * @param {import("../../core/types.js").ApplicationContext} app
 */
export function installFeaturesPrintersIndex(app) {
  const MIN_LINE_LENGTH_MULTIPLIER = 2;
  const MAX_LINE_LENGTH_MULTIPLIER = 10;

  function createPrintSetupId(prefix) {
    return `${prefix}-${globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`}`;
  }

  function umToMm(value) {
    return Number(value) / 1000;
  }

  function mmToUm(value) {
    const numeric = Number(value);
    if (!Number.isFinite(numeric) || numeric <= 0) return null;
    const normalized = Math.round(numeric * 1000);
    return Math.abs(numeric - normalized / 1000) <= 5e-7 ? normalized : null;
  }

  function applyAuthoritativePrinterState(printersData, active, response = active) {
    const authoritativePrinters = response?.printers_data || active?.printers_data || printersData;
    const incomingRevision = Number(authoritativePrinters?.revision);
    const acceptedRevision = Number(app.state.session.printersData?.revision);
    if (Number.isInteger(incomingRevision) && Number.isInteger(acceptedRevision) && incomingRevision < acceptedRevision) {
      return false;
    }
    app.state.session.printersData = authoritativePrinters;
    const printer = active?.printer || null;
    app.state.session.activeNozzle = active?.nozzle || null;
    app.state.session.activeExtrusionWidth = active?.extrusion_width || null;
    app.state.session.activePrintability = active?.printability || null;
    app.state.session.resolvedPrintSetup = response?.resolved_print_setup || active?.resolved_print_setup || null;
    const authoritativeConfig = response?.config || active?.config;
    if (authoritativeConfig) Object.assign(app.state.settings.config, authoritativeConfig);
    app.state.settings.config.printability_extrusion_width_mm =
      active?.printability?.extrusion_width_mm ?? null;
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
    app.commands.renderSolvePitchControl?.();
    app.commands.updateDerivedParams();
    app.commands.updateSuggestSlotHint?.();
    app.commands.renderAmsPreview?.();
    app.commands.updateInfoGrid?.();
    const evaluationResponse = response?.settings_evaluation ? response : active;
    if (evaluationResponse?.settings_evaluation) {
      const ticket = app.commands.beginSettingsEvaluationRequest();
      app.commands.applySettingsEvaluationResponse(evaluationResponse, ticket);
    }
    if (response?.print_setup_repair || active?.print_setup_repair) {
      app.commands.showToast("Solve Pitch was adjusted to the nearest supported value.", "warning");
    }
    return true;
  }

  function stepPrinterIntegerInput(input, direction) {
    if (!input || input.disabled) return null;
    const step = Number(input.step || input.getAttribute?.("step") || 1);
    const minimum = Number(input.min || input.getAttribute?.("min"));
    const maximum = Number(input.max || input.getAttribute?.("max"));
    const current = Number(input.value);
    if (!Number.isFinite(step) || step <= 0 || !Number.isFinite(current)) return null;
    let next = current + (direction < 0 ? -step : step);
    if (Number.isFinite(minimum)) next = Math.max(minimum, next);
    if (Number.isFinite(maximum)) next = Math.min(maximum, next);
    const decimalPlaces = String(step).includes(".") ? String(step).split(".")[1].length : 0;
    input.value = decimalPlaces ? next.toFixed(decimalPlaces) : String(Math.round(next));
    input.dispatchEvent?.(new Event("input", { bubbles: true }));
    input.dispatchEvent?.(new Event("change", { bubbles: true }));
    return next;
  }

  function installPrinterIntegerSteppers(root) {
    root?.querySelectorAll?.(".pc-integer-stepper").forEach(stepper => {
      const input = stepper.querySelector("input");
      const buttons = [...stepper.querySelectorAll("[data-step-direction]")];
      const sync = () => {
        const value = Number(input?.value);
        const minimum = Number(input?.min);
        const maximum = Number(input?.max);
        for (const button of buttons) {
          const direction = Number(button.dataset.stepDirection);
          button.disabled = !input || input.disabled
            || (direction < 0 && Number.isFinite(minimum) && value <= minimum)
            || (direction > 0 && Number.isFinite(maximum) && value >= maximum);
        }
      };
      for (const button of buttons) {
        button.onclick = () => {
          app.commands.stepPrinterIntegerInput(input, Number(button.dataset.stepDirection));
          sync();
          input.focus();
        };
      }
      input?.addEventListener("input", sync);
      sync();
    });
  }

  function syncNozzleDerivedMinimum(row) {
    const diameterInput = row?.querySelector?.(".nz-diameter");
    const minimumWidthInput = row?.querySelector?.(".nz-min-ew");
    const title = row?.querySelector?.(".pc-nozzle-title");
    const diameter = diameterInput?.value || "";
    if (minimumWidthInput) {
      minimumWidthInput.value = diameter;
      minimumWidthInput.disabled = true;
    }
    if (title) title.textContent = diameter ? `${diameter} mm Nozzle` : "Nozzle Profile";
    row?.setAttribute?.("aria-label", diameter ? `${diameter} mm Nozzle Profile` : "Nozzle Profile");
  }

  function layerHeightConflict(error) {
    const detail = error?.body?.detail;
    return error?.status === 409
      && detail?.error === "layer_height_incompatible_with_nozzle"
      ? detail
      : null;
  }

  async function withLayerHeightCorrection(request, payload) {
    try {
      return await request(payload);
    } catch (error) {
      const conflict = layerHeightConflict(error);
      if (!conflict) throw error;
      const confirmed = await app.commands.appConfirm(
        `This Extrusion Width uses a ${conflict.nozzle_diameter_mm} mm nozzle, which supports Layer Heights from ${conflict.minimum_layer_height_mm} mm to ${conflict.maximum_layer_height_mm} mm. Change Layer Height from ${conflict.requested_layer_height_mm} mm to ${conflict.nearest_layer_height_mm} mm and continue?`,
        { title: "Change Layer Height?", ok: "Change and continue" },
      );
      if (!confirmed) return null;
      return request({
        ...payload,
        accept_layer_height_correction: true,
        expected_layer_height_mm: conflict.requested_layer_height_mm,
      });
    }
  }

  function formatReviewMm(value) {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return "—";
    return `${numeric.toFixed(3).replace(/0+$/, "").replace(/\.$/, "")} mm`;
  }

  function reviewValue(field, value) {
    if (!value) return "—";
    if (field === "printer") return value.name || value.id || "—";
    if (field === "nozzle") return formatReviewMm(Number(value.diameter_um) / 1000);
    if (field === "extrusion_width") return formatReviewMm(Number(value.width_um) / 1000);
    return String(value);
  }

  function reviewChangeRow(label, before, after) {
    return `<li><span>${app.commands.esc2(label)}</span><strong>${app.commands.esc2(before)} → ${app.commands.esc2(after)}</strong></li>`;
  }

  function assertPrintSetupReview(review) {
    const intentKinds = new Set([
      "select_printer",
      "select_nozzle",
      "select_extrusion_width",
      "add_and_select_extrusion_width",
    ]);
    if (review?.schema_version !== 1 || !intentKinds.has(review?.intent?.kind)) {
      throw new Error("Unsupported print-setup review schema");
    }
    for (const field of ["requested_changes", "dependent_changes", "derived_consequences", "attention_items"]) {
      if (!Array.isArray(review[field])) throw new Error(`Malformed print-setup review: ${field}`);
    }
    const requestedFields = new Set(["printer", "nozzle", "extrusion_width"]);
    const dependentFields = new Set([...requestedFields, "layer_height", "solve_pitch_multiplier"]);
    const derivedFields = new Set(["solve_pitch", "minimum_line_length", "filament_capacity"]);
    const attentionCodes = new Set([
      "image_dimensions_not_solve_pitch_aligned",
      "image_dimensions_exceed_print_area",
      "settings_context_requires_attention",
    ]);
    if (review.requested_changes.some(item => !requestedFields.has(item?.field))
      || review.dependent_changes.some(item => !dependentFields.has(item?.field))
      || review.derived_consequences.some(item => !derivedFields.has(item?.field))
      || review.attention_items.some(item => !attentionCodes.has(item?.code))) {
      throw new Error("Unsupported print-setup review item");
    }
  }

  function printSetupReviewPresentation(review) {
    assertPrintSetupReview(review);
    const requested = (review?.requested_changes || []).map(item => {
      const label = {
        printer: "Printer",
        nozzle: "Nozzle",
        extrusion_width: "Extrusion Width",
      }[item.field] || item.field;
      return reviewChangeRow(label, reviewValue(item.field, item.before), reviewValue(item.field, item.after));
    }).join("");
    const dependent = (review?.dependent_changes || []).map(item => {
      if (item.field === "layer_height") {
        return reviewChangeRow("Layer Height", formatReviewMm(item.before_mm), formatReviewMm(item.after_mm));
      }
      if (item.field === "solve_pitch_multiplier") {
        return reviewChangeRow("Solve Pitch multiplier", String(item.before), String(item.after));
      }
      const label = { printer: "Printer", nozzle: "Nozzle", extrusion_width: "Extrusion Width" }[item.field] || item.field;
      return reviewChangeRow(label, reviewValue(item.field, item.before), reviewValue(item.field, item.after));
    }).join("");
    const derived = (review?.derived_consequences || []).map(item => {
      if (item.field === "solve_pitch") {
        return reviewChangeRow("Solve Pitch", formatReviewMm(item.before_mm), formatReviewMm(item.after_mm));
      }
      if (item.field === "minimum_line_length") {
        return reviewChangeRow("Minimum Line Length", formatReviewMm(item.before_mm), formatReviewMm(item.after_mm));
      }
      if (item.field === "filament_capacity") {
        return reviewChangeRow("Filament capacity", `${item.before_slots} slots`, `${item.after_slots} slots`);
      }
      return "";
    }).join("");
    const attention = (review?.attention_items || []).map(item => {
      if (item.code === "image_dimensions_not_solve_pitch_aligned") {
        const axes = (item.affected || []).map(axis => axis[0].toUpperCase() + axis.slice(1)).join(" and ");
        return `<li><strong>Image size:</strong> ${app.commands.esc2(axes || "Width and Height")} must align to the new ${app.commands.esc2(formatReviewMm(item.pitch_mm))} Solve Pitch. The current ${app.commands.esc2(formatReviewMm(item.requested?.width_mm))} × ${app.commands.esc2(formatReviewMm(item.requested?.height_mm))} size resolves to ${app.commands.esc2(formatReviewMm(item.resolved?.width_mm))} × ${app.commands.esc2(formatReviewMm(item.resolved?.height_mm))}.</li>`;
      }
      if (item.code === "image_dimensions_exceed_print_area") {
        return `<li><strong>Image size:</strong> The current dimensions exceed this printer's ${app.commands.esc2(formatReviewMm(item.maximum?.width_mm))} × ${app.commands.esc2(formatReviewMm(item.maximum?.height_mm))} print area.</li>`;
      }
      if (item.code === "settings_context_requires_attention") {
        const messages = (item.issues || []).map(issue => {
          if (issue.code === "max_total_thickness_below_minimum") {
            return "Maximum Thickness is below the minimum required by the proposed Layer Height.";
          }
          if (issue.code === "thickness_not_whole_layers") {
            return "The current thickness settings do not form a whole number of layers at the proposed Layer Height.";
          }
          return "A Settings value is not valid under the proposed print setup.";
        });
        return [...new Set(messages)].map(message => `<li><strong>Settings:</strong> ${app.commands.esc2(message)} Review the highlighted setting after applying this change.</li>`).join("");
      }
      return "";
    }).join("");
    const section = (title, rows, className = "") => rows
      ? `<section class="print-setup-review-section ${className}"><h4>${app.commands.esc2(title)}</h4><ul>${rows}</ul></section>`
      : "";
    return {
      title: {
        select_printer: "Review Printer Change",
        select_nozzle: "Review Nozzle Change",
        select_extrusion_width: "Review Extrusion Width Change",
        add_and_select_extrusion_width: "Review New Extrusion Width",
      }[review?.intent?.kind] || "Review Change",
      detailHtml: [
        section("You requested", requested),
        section("Prisma must also change", dependent),
        section("This changes", derived),
        section("Needs attention", attention, "is-attention"),
      ].join(""),
    };
  }

  async function completeReviewedPrintSetupRequest(request, payload, { silentlyAcceptReview = false } = {}) {
    const response = await request(payload);
    if (response?.status === "stale") {
      const authoritative = response.authoritative;
      if (authoritative) app.commands.applyAuthoritativePrinterState(authoritative.printers_data, authoritative);
      app.commands.showToast("Printer settings changed while the review was open. Review the current values and try again.", "warning");
      return null;
    }
    if (response?.status !== "review_required") return response;
    assertPrintSetupReview(response.review);
    if (!silentlyAcceptReview) {
      const presentation = printSetupReviewPresentation(response.review);
      const accepted = await app.commands.appConfirm(
        "Review the requested change and everything it affects before applying it.",
        {
          title: presentation.title,
          ok: "Apply changes",
          cancel: "Cancel",
          detailHtml: presentation.detailHtml,
        },
      );
      if (!accepted) return null;
    }
    const acceptedResponse = await request({
      ...payload,
      acceptance_token: response.acceptance_token,
    });
    if (acceptedResponse?.status === "stale") {
      const authoritative = acceptedResponse.authoritative;
      if (authoritative) app.commands.applyAuthoritativePrinterState(authoritative.printers_data, authoritative);
      app.commands.showToast("Printer settings changed while the review was open. Review the current values and try again.", "warning");
      return null;
    }
    return acceptedResponse;
  }

  function printSetupMutationId() {
    return globalThis.crypto?.randomUUID?.() || `print-setup-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  async function selectActivePrintSetup(payload) {
    await app.commands.syncConfigToServer?.({ throwOnError: true, showErrorToast: true });
    const guideAuthorized = payload.review_policy === "guide_authorized"
      || app.commands.guideAuthorizesPrintSetupIntent?.(payload) === true;
    const { review_policy: _reviewPolicy, ...requestPayload } = payload;
    return completeReviewedPrintSetupRequest(app.api.setActivePrinter, {
      expected_revision: app.state.session.printersData?.revision,
      mutation_id: printSetupMutationId(),
      ...requestPayload,
    }, { silentlyAcceptReview: guideAuthorized });
  }

  function savePrinterConfigurations(payload) {
    return withLayerHeightCorrection(app.api.savePrinters, payload);
  }

  async function addPrinterWidthShortcut(payload) {
    await app.commands.syncConfigToServer?.({ throwOnError: true, showErrorToast: true });
    const guideAuthorized = payload.review_policy === "guide_authorized"
      || app.commands.guideAuthorizesPrintSetupIntent?.(payload) === true;
    const { review_policy: _reviewPolicy, ...requestPayload } = payload;
    return completeReviewedPrintSetupRequest(app.api.addPrinterWidthShortcut, {
      expected_revision: app.state.session.printersData?.revision,
      mutation_id: printSetupMutationId(),
      ...requestPayload,
    }, { silentlyAcceptReview: guideAuthorized });
  }

  function removePrinterWidthShortcut(payload) {
    return app.api.removePrinterWidthShortcut({
      expected_revision: app.state.session.printersData?.revision,
      ...payload,
    });
  }

  async function loadPrinters() {
    try {
      const printersData = await app.api.fetchPrinters();
      const active = await app.api.fetchActivePrinter();
      app.commands.applyAuthoritativePrinterState(active?.printers_data || printersData, active);
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
    app.state.session.printerConfigOriginal = structuredClone(app.state.session.printersData);
    app.state.session.printerConfigDraft = structuredClone(app.state.session.printersData);
    app.state.session.printerConfigDeletedNozzles = [];
    app.state.session.printerConfigEditingId = app.state.session.printerConfigDraft?.active_printer_id || app.state.session.printerConfigDraft?.printers?.[0]?.id || null;
    app.commands.updatePrinterConfigValidationMessage("");
    app.commands.renderPrinterConfigPage();
    app.events.emit("printer-config.opened", { source: "printer-configuration" });
  }

  async function hidePrinterConfigPage(navigateTo) {
    if (!app.commands._readPrinterFromConfigPage()) return false;
    const draft = app.commands.printerConfigData();
    if (app.state.session.printerConfigEditingId && draft?.printers?.some(p => p.id === app.state.session.printerConfigEditingId)) {
      draft.active_printer_id = app.state.session.printerConfigEditingId;
    }
    if (!await app.commands.resolvePrinterConfigDependentState()) return false;
    let saved;
    try {
      saved = await app.commands.savePrinterConfigurations(draft);
    } catch (e) {
      if (e?.status === 409 && e?.body?.detail?.error === "printer_revision_conflict") {
        await app.commands.loadPrinters();
        app.state.session.printerConfigOriginal = structuredClone(app.state.session.printersData);
        app.state.session.printerConfigDraft = structuredClone(app.state.session.printersData);
        app.state.session.printerConfigDeletedNozzles = [];
        app.state.session.printerConfigEditingId = app.state.session.printerConfigDraft?.active_printer_id
          || app.state.session.printerConfigDraft?.printers?.[0]?.id
          || null;
        app.commands.renderPrinterConfigPage();
        app.commands.showToast("Printer setup changed elsewhere. The configuration window now shows the latest saved values; repeat your edit if it is still needed.", "warning");
        return false;
      }
      app.commands.showToast("Failed to save printer config: " + e.message, "error");
      return false;
    }
    if (!saved) return false;
    const printersData = {
      schema_version: saved.schema_version,
      revision: saved.revision,
      printers: saved.printers || [],
      active_printer_id: saved.active_printer_id,
      printer_setup_state: saved.printer_setup_state || {},
    };
    try {
      app.commands.applyAuthoritativePrinterState(printersData, saved.active, saved);
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
    app.state.session.printerConfigDraft = null;
    app.state.session.printerConfigOriginal = null;
    app.state.session.printerConfigDeletedNozzles = [];
    app.commands.switchTab(target);
    app.events.emit("printer.active-changed", {
      printerId: saved.active_printer_id || null,
      source: "printer-configuration",
    });
    app.events.emit("printer-config.closed", { source: "printer-configuration" });
    return true;
  }

  function discardPrinterConfigDraft() {
    const page = app.state.ui.$("#printerConfigPage");
    if (page) page.classList.add("is-hidden");
    app.state.ui.$$(".mode-button").forEach(btn => btn.classList.remove("is-dimmed"));
    const target = app.state.session.printerConfigOriginTab || app.state.ui.currentTab;
    app.state.session.printerConfigOriginTab = null;
    app.state.session.printerConfigEditingId = null;
    app.state.session.printerConfigDraft = null;
    app.state.session.printerConfigOriginal = null;
    app.state.session.printerConfigDeletedNozzles = [];
    app.commands.updatePrinterConfigValidationMessage("");
    app.commands.switchTab(target);
    app.events.emit("printer-config.closed", { source: "printer-configuration", discarded: true });
    return true;
  }

  function createDefaultNozzleProfile(diameterUm = 400) {
    return {
      id: createPrintSetupId("nozzle"),
      diameter_um: diameterUm,
      min_layer_height_um: diameterUm <= 200 ? 50 : 80,
      max_layer_height_um: diameterUm <= 200 ? 150 : 320,
      max_extrusion_width_um: Math.round(diameterUm * 1.25),
      minimum_line_length_multiplier: MIN_LINE_LENGTH_MULTIPLIER,
    };
  }

  function createDefaultPrinterProfile(id) {
    const fine = createDefaultNozzleProfile(200);
    const standard = createDefaultNozzleProfile(400);
    return {
      id,
      name: "New Printer",
      max_print_area: { x: 256, y: 256 },
      ams_units: 1,
      slots_per_ams: 4,
      nozzle_profiles: [fine, standard],
    };
  }

  function createDefaultPrinterSetup(printer) {
    const nozzles = printer?.nozzle_profiles || [];
    return {
      active_nozzle_id: nozzles[0]?.id || null,
      nozzle_width_state: Object.fromEntries(nozzles.map(nozzle => [
        nozzle.id,
        { current_width_um: nozzle.diameter_um, saved_widths_um: [nozzle.diameter_um] },
      ])),
    };
  }

  function printerConfigData() {
    return app.state.session.printerConfigDraft || app.state.session.printersData;
  }

  function updatePrinterConfigValidationMessage(message) {
    const output = app.state.ui.$("#pcValidationMessage");
    if (output) {
      output.textContent = message || "";
      output.classList.toggle("is-hidden", !message);
    }
  }

  function readPrinterIntegerField(selector, { label, minimum, maximum }) {
    const input = app.state.ui.$(selector);
    const numeric = Number(input?.value);
    const valid = Number.isInteger(numeric) && numeric >= minimum && numeric <= maximum;
    if (input) {
      input.setAttribute("aria-invalid", valid ? "false" : "true");
      input.dataset.validationMessage = `${label} must be a whole number from ${minimum} through ${maximum}.`;
    }
    return { input, valid, value: numeric };
  }

  function readPrinterCapabilityFields({ validate = true } = {}) {
    const fields = {
      areaX: readPrinterIntegerField("#pcAreaX", { label: "Print Area X", minimum: 50, maximum: 500 }),
      areaY: readPrinterIntegerField("#pcAreaY", { label: "Print Area Y", minimum: 50, maximum: 500 }),
      amsUnits: readPrinterIntegerField("#pcAmsUnits", { label: "AMS Units", minimum: 1, maximum: 4 }),
      slotsPerAms: readPrinterIntegerField("#pcSlotsPerAms", { label: "Slots per AMS", minimum: 1, maximum: 16 }),
    };
    const invalid = Object.values(fields).find(field => !field.valid);
    if (invalid && validate) {
      invalid.input?.focus();
      app.commands.updatePrinterConfigValidationMessage(invalid.input?.dataset.validationMessage || "Fix the invalid Printer Profile value before saving.");
      app.commands.showToast(invalid.input?.dataset.validationMessage || "Fix the invalid Printer Profile value before saving.", "error");
      return null;
    }
    return fields;
  }

  function validateNozzleRow(row) {
    const fields = [".nz-diameter", ".nz-min-lh", ".nz-max-lh", ".nz-max-ew"];
    const values = fields.map(selector => mmToUm(row?.querySelector(selector)?.value));
    const [diameter, minLayer, maxLayer, maxWidth] = values;
    const multiplier = Number(row?.querySelector(".nz-min-ll-mult")?.value);
    const rows = Array.from(row?.parentElement?.querySelectorAll?.(".pc-nozzle-row") || []);
    const duplicateDiameter = diameter !== null && rows.some(candidate => (
      candidate !== row && mmToUm(candidate.querySelector(".nz-diameter")?.value) === diameter
    ));
    const validMultiplier = Number.isInteger(multiplier)
      && multiplier >= MIN_LINE_LENGTH_MULTIPLIER
      && multiplier <= MAX_LINE_LENGTH_MULTIPLIER;
    const valid = values.every(value => value !== null)
      && minLayer <= maxLayer
      && maxWidth >= diameter
      && !duplicateDiameter
      && validMultiplier;
    const diameterInput = row?.querySelector(".nz-diameter");
    const minLayerInput = row?.querySelector(".nz-min-lh");
    const maxWidthInput = row?.querySelector(".nz-max-ew");
    const multiplierInput = row?.querySelector(".nz-min-ll-mult");
    diameterInput?.setAttribute("aria-invalid", (!diameter || duplicateDiameter) ? "true" : "false");
    minLayerInput?.setAttribute("aria-invalid", (!minLayer || !maxLayer || minLayer > maxLayer) ? "true" : "false");
    maxWidthInput?.setAttribute("aria-invalid", (!maxWidth || !diameter || maxWidth < diameter) ? "true" : "false");
    multiplierInput?.setAttribute("aria-invalid", validMultiplier ? "false" : "true");
    if (diameterInput) {
      diameterInput.dataset.validationMessage = duplicateDiameter
        ? "Nozzle Diameters must be unique within one Printer Profile."
        : "Enter a positive Nozzle Diameter with no more than three decimal places.";
    }
    if (minLayerInput) minLayerInput.dataset.validationMessage = "Minimum Layer Height must not exceed the maximum.";
    if (maxWidthInput) maxWidthInput.dataset.validationMessage = "Maximum Extrusion Width must be at or above the Nozzle Diameter.";
    if (multiplierInput) multiplierInput.dataset.validationMessage = "Minimum Line Length must be a whole number from 2 through 10 Extrusion Widths.";
    row?.classList.toggle("is-invalid", !valid);
    return valid;
  }

  function currentPrinterConfigId() {
    const data = app.commands.printerConfigData();
    const printers = data?.printers || [];
    if (!printers.length) {
      app.state.session.printerConfigEditingId = null;
      return null;
    }
    if (!app.state.session.printerConfigEditingId || !printers.some(p => p.id === app.state.session.printerConfigEditingId)) {
      app.state.session.printerConfigEditingId = data.active_printer_id || printers[0].id;
    }
    if (!printers.some(p => p.id === app.state.session.printerConfigEditingId)) {
      app.state.session.printerConfigEditingId = printers[0].id;
    }
    return app.state.session.printerConfigEditingId;
  }

  function syncPrinterConfigSetupState(data = app.commands.printerConfigData()) {
    if (!data) return;
    const printerIds = new Set((data.printers || []).map(printer => printer.id));
    data.printer_setup_state ||= {};
    Object.keys(data.printer_setup_state).forEach(printerId => {
      if (!printerIds.has(printerId)) delete data.printer_setup_state[printerId];
    });
    (data.printers || []).forEach(printer => {
      const nozzles = printer.nozzle_profiles || [];
      const nozzleIds = new Set(nozzles.map(nozzle => nozzle.id));
      const setup = data.printer_setup_state[printer.id] ||= app.commands.createDefaultPrinterSetup(printer);
      setup.nozzle_width_state ||= {};
      Object.keys(setup.nozzle_width_state).forEach(nozzleId => {
        if (!nozzleIds.has(nozzleId)) delete setup.nozzle_width_state[nozzleId];
      });
      nozzles.forEach(nozzle => {
        setup.nozzle_width_state[nozzle.id] ||= {
          current_width_um: nozzle.diameter_um,
          saved_widths_um: [nozzle.diameter_um],
        };
      });
      if (!nozzleIds.has(setup.active_nozzle_id)) setup.active_nozzle_id = nozzles[0]?.id || null;
    });
    if (!printerIds.has(data.active_printer_id)) data.active_printer_id = data.printers?.[0]?.id || null;
  }

  function nextUnusedNozzleDiameter(printer) {
    const used = new Set((printer?.nozzle_profiles || []).map(nozzle => Number(nozzle.diameter_um)));
    const standard = [200, 400, 600, 800, 1000];
    const available = standard.find(value => !used.has(value));
    if (available) return available;
    let candidate = 100;
    while (used.has(candidate)) candidate += 100;
    return candidate;
  }

  function addNozzleProfileToDraft(printer) {
    const diameterUm = app.commands.nextUnusedNozzleDiameter(printer);
    const nozzle = app.commands.createDefaultNozzleProfile(diameterUm);
    printer.nozzle_profiles.push(nozzle);
    const data = app.commands.printerConfigData();
    const setup = data.printer_setup_state[printer.id];
    setup.nozzle_width_state[nozzle.id] = {
      current_width_um: diameterUm,
      saved_widths_um: [diameterUm],
    };
    if (!setup.active_nozzle_id) setup.active_nozzle_id = nozzle.id;
    return nozzle;
  }

  function deleteNozzleProfileFromDraft(printer, nozzleIndex) {
    if ((printer?.nozzle_profiles || []).length <= 1) return false;
    const [removed] = printer.nozzle_profiles.splice(nozzleIndex, 1);
    const setup = app.commands.printerConfigData()?.printer_setup_state?.[printer.id];
    const removedState = removed ? structuredClone(setup?.nozzle_width_state?.[removed.id]) : null;
    if (setup && removed) {
      delete setup.nozzle_width_state?.[removed.id];
      if (setup.active_nozzle_id === removed.id) {
        setup.active_nozzle_id = printer.nozzle_profiles[0]?.id || null;
      }
    }
    if (removed) {
      app.state.session.printerConfigDeletedNozzles.push({
        printerId: printer.id,
        nozzle: structuredClone(removed),
        widthState: removedState,
        index: nozzleIndex,
      });
    }
    return true;
  }

  function restoreLastDeletedNozzle() {
    const deletion = app.state.session.printerConfigDeletedNozzles.pop();
    if (!deletion) return false;
    const data = app.commands.printerConfigData();
    const printer = data?.printers?.find(item => item.id === deletion.printerId);
    if (!printer) return false;
    printer.nozzle_profiles.splice(Math.min(deletion.index, printer.nozzle_profiles.length), 0, deletion.nozzle);
    const setup = data.printer_setup_state[printer.id];
    setup.nozzle_width_state[deletion.nozzle.id] = deletion.widthState || {
      current_width_um: deletion.nozzle.diameter_um,
      saved_widths_um: [deletion.nozzle.diameter_um],
    };
    app.commands.renderPrinterConfigPage();
    return true;
  }

  function selectPrinterConfigId(nextId) {
    if (!nextId || nextId === app.state.session.printerConfigEditingId) return true;
    if (!app.commands._readPrinterFromConfigPage()) return false;
    app.commands.resetPrinterDeleteConfirm();
    app.state.session.printerConfigEditingId = nextId;
    app.commands.printerConfigData().active_printer_id = nextId;
    app.commands.syncPrinterConfigSetupState();
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
    const data = app.commands.printerConfigData();
    if (!data) return;
    const printers = data.printers || [];
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
    const protectedPrinter = printer.editable === false || printer.guide_only === true;

    // Fill fields
    const pcName = app.state.ui.$("#pcName");
    if (pcName) {
      pcName.value = printer.name;
      pcName.disabled = protectedPrinter;
      pcName.oninput = () => {
        const nextName = (pcName.value || "").trim() || "New Printer";
        printer.name = nextName;
        app.commands.updatePrinterConfigDropdownLabel(printer.id, nextName);
      };
    }
    const pcAreaX = app.state.ui.$("#pcAreaX");
    if (pcAreaX) { pcAreaX.value = printer.max_print_area?.x || 256; pcAreaX.disabled = protectedPrinter; }
    const pcAreaY = app.state.ui.$("#pcAreaY");
    if (pcAreaY) { pcAreaY.value = printer.max_print_area?.y || 256; pcAreaY.disabled = protectedPrinter; }
    const pcAmsUnits = app.state.ui.$("#pcAmsUnits");
    if (pcAmsUnits) { pcAmsUnits.value = printer.ams_units || 1; pcAmsUnits.disabled = protectedPrinter; }
    const pcSlotsPerAms = app.state.ui.$("#pcSlotsPerAms");
    if (pcSlotsPerAms) { pcSlotsPerAms.value = printer.slots_per_ams || 4; pcSlotsPerAms.disabled = protectedPrinter; }

    // Nozzle-owned hardware and printability constraints. Active/saved widths live in the rail.
    const tbody = app.state.ui.$("#pcNozzleBody");
    if (tbody) {
      tbody.innerHTML = (printer.nozzle_profiles || []).map((nozzle, nozzleIndex) => {
        return `
          <section class="pc-nozzle-group pc-nozzle-row" data-nozzle-idx="${nozzleIndex}" aria-label="Nozzle profile ${nozzleIndex + 1}">
            <div class="pc-nozzle-group-header">
              <span class="pc-nozzle-title">${umToMm(nozzle.diameter_um)} mm Nozzle</span>
              <button class="ghost-button xxs danger nz-delete" data-nozzle-idx="${nozzleIndex}" aria-label="Delete nozzle profile">Delete</button>
            </div>
            <div class="pc-nozzle-constraints">
              <label class="pc-constraint-field">
                <span class="pc-property-label">Nozzle Diameter</span>
                <span class="pc-value-with-unit"><input type="number" class="pc-number-input nz-diameter" value="${umToMm(nozzle.diameter_um)}" step="0.001" min="0.001" aria-label="Nozzle Diameter in millimeters"><span>mm</span></span>
              </label>
              <label class="pc-constraint-field">
                <span class="pc-property-label">Layer Height Range</span>
                <span class="pc-range-controls"><input type="number" class="pc-number-input nz-min-lh" value="${umToMm(nozzle.min_layer_height_um)}" step="0.001" min="0.001" aria-label="Minimum Layer Height in millimeters"><span aria-hidden="true">–</span><input type="number" class="pc-number-input nz-max-lh" value="${umToMm(nozzle.max_layer_height_um)}" step="0.001" min="0.001" aria-label="Maximum Layer Height in millimeters"><span>mm</span></span>
              </label>
              <label class="pc-constraint-field">
                <span class="pc-property-label">Extrusion Width Range</span>
                <span class="pc-range-controls"><input type="number" class="pc-number-input pc-derived-bound nz-min-ew" value="${umToMm(nozzle.diameter_um)}" disabled title="The lower bound is the Nozzle Diameter" aria-label="Minimum supported Extrusion Width in millimeters"><span aria-hidden="true">–</span><input type="number" class="pc-number-input nz-max-ew" value="${umToMm(nozzle.max_extrusion_width_um)}" step="0.001" min="0.001" aria-label="Maximum supported Extrusion Width in millimeters"><span>mm</span></span>
              </label>
              <div class="pc-constraint-field">
                <span class="pc-property-label">Minimum Line Length</span>
                <span class="pc-minimum-line-controls">
                  <span class="pc-integer-stepper">
                    <input type="number" class="pc-number-input nz-min-ll-mult" value="${nozzle.minimum_line_length_multiplier}" step="1" min="${MIN_LINE_LENGTH_MULTIPLIER}" max="${MAX_LINE_LENGTH_MULTIPLIER}" aria-label="Minimum Line Length in Extrusion Widths">
                    <span class="settings-number-steppers">
                      <button type="button" class="settings-number-step settings-number-step-up" data-step-direction="1" aria-label="Increase Minimum Line Length">▲</button>
                      <button type="button" class="settings-number-step settings-number-step-down" data-step-direction="-1" aria-label="Decrease Minimum Line Length">▼</button>
                    </span>
                  </span>
                  <span>× Extrusion Width</span>
                </span>
              </div>
            </div>
          </section>`;
      }).join("");

      tbody.querySelectorAll(".nz-delete").forEach(btn => {
        const lastNozzle = printer.nozzle_profiles.length <= 1;
        btn.disabled = protectedPrinter || lastNozzle;
        btn.hidden = protectedPrinter || lastNozzle;
        btn.addEventListener("click", async () => {
          const idx = Number(btn.dataset.nozzleIdx);
          const nozzle = printer.nozzle_profiles[idx];
          const confirmed = await app.commands.appConfirm(
            `Are you sure you want to delete the ${umToMm(nozzle.diameter_um)} mm Nozzle Profile and its saved Extrusion Widths?`,
            { title: "Delete Nozzle Profile?", ok: "Delete Nozzle" },
          );
          if (!confirmed) return;
          app.commands.deleteNozzleProfileFromDraft(printer, idx);
          app.commands.renderPrinterConfigPage();
        });
      });
      tbody.querySelectorAll("input").forEach(input => {
        input.disabled = protectedPrinter || input.classList.contains("pc-derived-bound");
      });
      tbody.querySelectorAll(".pc-nozzle-row").forEach(row => {
        const diameterInput = row.querySelector(".nz-diameter");
        diameterInput?.addEventListener("input", () => {
          app.commands.syncNozzleDerivedMinimum(row);
          app.commands.validateNozzleRow(row);
        });
        row.querySelectorAll(".nz-min-lh, .nz-max-lh, .nz-max-ew, .nz-min-ll-mult").forEach(input => {
          input.addEventListener("input", () => app.commands.validateNozzleRow(row));
        });
        app.commands.syncNozzleDerivedMinimum(row);
        app.commands.validateNozzleRow(row);
      });
    }

    const deletionNotice = app.state.ui.$("#pcNozzleDeletionNotice");
    const deletionText = app.state.ui.$("#pcNozzleDeletionText");
    const deletion = app.state.session.printerConfigDeletedNozzles.at(-1);
    deletionNotice?.classList.toggle("is-hidden", !deletion);
    if (deletionText) deletionText.textContent = deletion
      ? `${umToMm(deletion.nozzle.diameter_um)} mm Nozzle Profile deleted from this draft.`
      : "";
    const undoDelete = app.state.ui.$("#pcUndoNozzleDelete");
    if (undoDelete) undoDelete.onclick = app.commands.restoreLastDeletedNozzle;

    // Delete printer button visibility
    const delBtn = app.state.ui.$("#pcDeletePrinterBtn");
    if (delBtn) {
      delBtn.style.display = printers.length > 1 && !protectedPrinter ? "" : "none";
      if (!app.state.session.printerDeleteConfirmPending) {
        delBtn.textContent = "Delete";
        delBtn.title = "Delete selected printer";
        delBtn.classList.remove("confirm-pending");
      }
    }
    app.commands.installPrinterIntegerSteppers(app.state.ui.$("#printerConfigPage"));
  }

  function _readPrinterFromConfigPage({ validate = true } = {}) {
    const data = app.commands.printerConfigData();
    const printers = data?.printers || [];
    const sel = app.state.ui.$("#pcPrinterSelect");
    const selectedId = app.state.session.printerConfigEditingId || (sel ? sel.value : data?.active_printer_id);
    const printer = printers.find(p => p.id === selectedId);
    if (!printer) return null;
    if (printer.editable === false || printer.guide_only === true) return printer;

    const capabilityFields = app.commands.readPrinterCapabilityFields({ validate });
    if (!capabilityFields) return null;
    printer.name = (app.state.ui.$("#pcName")?.value || printer.name).trim();
    printer.max_print_area = {
      x: capabilityFields.areaX.value,
      y: capabilityFields.areaY.value,
    };
    printer.ams_units = capabilityFields.amsUnits.value;
    printer.slots_per_ams = capabilityFields.slotsPerAms.value;

    // Read nozzle rows from table
    const nozzleRows = app.state.ui.$$("#pcNozzleBody .pc-nozzle-row");
    if (!nozzleRows.length) {
      app.commands.showToast("A printer must have at least one nozzle profile.", "error");
      return null;
    }
    const invalidRow = validate
      ? Array.from(nozzleRows).find(row => !app.commands.validateNozzleRow(row))
      : null;
    if (invalidRow && validate) {
      const invalidInput = invalidRow.querySelector('[aria-invalid="true"]');
      const message = invalidInput?.dataset.validationMessage || "Fix the invalid Nozzle Profile before saving.";
      invalidInput?.focus();
      app.commands.updatePrinterConfigValidationMessage(message);
      app.commands.showToast(message, "error");
      return null;
    }
    app.commands.updatePrinterConfigValidationMessage("");
    const previousNozzles = printer.nozzle_profiles;
    printer.nozzle_profiles = Array.from(nozzleRows).map(row => {
      const index = Number(row.dataset.nozzleIdx);
      const previous = previousNozzles[index];
      return {
        id: previous?.id || createPrintSetupId("nozzle"),
        diameter_um: mmToUm(row.querySelector(".nz-diameter")?.value),
        min_layer_height_um: mmToUm(row.querySelector(".nz-min-lh")?.value),
        max_layer_height_um: mmToUm(row.querySelector(".nz-max-lh")?.value),
        max_extrusion_width_um: mmToUm(row.querySelector(".nz-max-ew")?.value),
        minimum_line_length_multiplier: Number(row.querySelector(".nz-min-ll-mult")?.value),
      };
    });
    app.commands.syncPrinterConfigSetupState(data);
    return printer;
  }

  async function resolvePrinterConfigDependentState() {
    const draft = app.commands.printerConfigData();
    if (!draft) return false;
    const proposed = structuredClone(draft);
    const changes = [];
    app.commands.syncPrinterConfigSetupState(proposed);
    (proposed.printers || []).forEach(printer => {
      const setup = proposed.printer_setup_state[printer.id];
      (printer.nozzle_profiles || []).forEach(nozzle => {
        const state = setup.nozzle_width_state[nozzle.id];
        const minimum = nozzle.diameter_um;
        const maximum = nozzle.max_extrusion_width_um;
        const previousCurrent = state.current_width_um;
        const invalidSaved = (state.saved_widths_um || []).filter(
          width => !Number.isInteger(width) || width < minimum || width > maximum,
        );
        state.saved_widths_um = [...new Set((state.saved_widths_um || []).filter(
          width => Number.isInteger(width) && width >= minimum && width <= maximum,
        ))].sort((a, b) => a - b);
        if (invalidSaved.length) {
          changes.push(`${printer.name}: remove saved Extrusion Width${invalidSaved.length === 1 ? "" : "s"} ${invalidSaved.map(umToMm).join(", ")} mm because they are outside the revised range.`);
        }
        if (previousCurrent < minimum || previousCurrent > maximum) {
          state.current_width_um = minimum;
          if (!state.saved_widths_um.includes(minimum)) state.saved_widths_um.unshift(minimum);
          changes.push(`${printer.name}: ${umToMm(previousCurrent)} mm is outside the revised ${umToMm(minimum)}–${umToMm(maximum)} mm range; the active Extrusion Width will become ${umToMm(minimum)} mm.`);
        }
      });
    });
    if (changes.length) {
      const confirmed = await app.commands.appConfirm(
        `Saving these Nozzle Profile changes also updates active print setup:\n\n${changes.join("\n")}`,
        { title: "Update active print setup?", ok: "Update and save" },
      );
      if (!confirmed) return false;
    }
    draft.printer_setup_state = proposed.printer_setup_state;
    draft.active_printer_id = proposed.active_printer_id;
    return true;
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
    stepPrinterIntegerInput,
    installPrinterIntegerSteppers,
    syncNozzleDerivedMinimum,
    selectActivePrintSetup,
    savePrinterConfigurations,
    addPrinterWidthShortcut,
    removePrinterWidthShortcut,
    loadPrinters,
    showPrinterConfigPage,
    hidePrinterConfigPage,
    discardPrinterConfigDraft,
    createDefaultNozzleProfile,
    createDefaultPrinterProfile,
    createDefaultPrinterSetup,
    printerConfigData,
    updatePrinterConfigValidationMessage,
    readPrinterCapabilityFields,
    validateNozzleRow,
    currentPrinterConfigId,
    syncPrinterConfigSetupState,
    nextUnusedNozzleDiameter,
    addNozzleProfileToDraft,
    deleteNozzleProfileFromDraft,
    restoreLastDeletedNozzle,
    selectPrinterConfigId,
    resetPrinterDeleteConfirm,
    updatePrinterConfigDropdownLabel,
    renderPrinterConfigPage,
    _readPrinterFromConfigPage,
    resolvePrinterConfigDependentState,
    switchFrameEditorTab,
    setLibraryPaneState,
    toggleLibraryPaneState,
    renderImageTab,
  });}
