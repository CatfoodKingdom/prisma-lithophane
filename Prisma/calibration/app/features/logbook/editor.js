/** Install features/logbook/editor commands. */
export function installFeaturesLogbookEditor(app) {
  function syncSampleStepCacheFromData() {
    app.state.logbook._sampleCreateSteps = Array.isArray(
      app.state.session.data.steps,
    )
      ? [...app.state.session.data.steps]
      : [];
  }

  function renderFilamentSelectorField(button, filamentId) {
    if (!button) return;
    button.dataset.filamentId = filamentId || "";
    const fil = app.commands.filamentMeta(filamentId);
    if (!fil) {
      button.innerHTML = `<span class="filament-selector-placeholder">Select filament</span>`;
      return;
    }
    button.innerHTML = `
      <span class="color-chip" style="background:${fil.hex || "#cccccc"}"></span>
      <span class="filament-selector-field-name">${app.commands._escHtml(fil.color_name || fil.display_name || fil.filament_id)}</span>
      <span class="filament-selector-field-meta">${app.commands._escHtml(fil.manufacturer || "")}</span>
    `;
  }

  function _sampleBatchPreviewName(startId, offset) {
    const match = String(startId || "").match(/^(.*?)(\d+)$/);
    if (!match) return `Sample ${offset + 1}`;
    const prefix = match[1];
    const width = match[2].length;
    const next = String(Number(match[2]) + offset).padStart(width, "0");
    return `${prefix}${next}`;
  }

  function _bundleStepReferenceLabel(index) {
    let n = index + 1;
    let label = "";
    while (n > 0) {
      n -= 1;
      label = String.fromCharCode(65 + (n % 26)) + label;
      n = Math.floor(n / 26);
    }
    return label;
  }

  function _buildBundleStepTable(steps) {
    return `
      <div class="bundle-step-table">
        ${steps
          .map((step, index) => {
            const label = app.commands._geometryLabelForStep(step);
            return `
          <div class="bundle-step-row">
            <span class="bundle-step-ref">${app.commands._escHtml(app.commands._bundleStepReferenceLabel(index))}</span>
            <span class="bundle-step-name" title="${app.commands._escAttr(label)}">${app.commands._escHtml(label)}</span>
          </div>
        `;
          })
          .join("")}
      </div>
    `;
  }

  async function openBulkSampleCreateDrawer() {
    app.state.logbook._sampleDrawerMode = "create";
    app.state.filaments._filamentDrawerMode = null;
    app.state.filaments._filamentDrawerData = null;
    app.state.logbook.selectedRecord = { kind: "sample", id: "__bulk__" };
    app.dom.recordDrawer.classList.remove("narrow-drawer");
    app.dom.recordDrawer.classList.remove("sample-expanded");
    app.dom.recordDrawer.classList.remove("model-filament-drawer");
    app.dom.recordDrawer.classList.add("sample-set-drawer");
    try {
      const [stepsResp, bundlesResp, idResp] = await Promise.all([
        app.api.fetchSteps(),
        app.api.fetchBundles(),
        app.api.fetchNextSampleId(),
      ]);
      app.state.logbook._sampleCreateSteps = stepsResp || [];
      app.state.logbook._bulkCreateBundles = bundlesResp || [];
      app.state.logbook._bulkCreateNextId = idResp?.next_id || "...";
    } catch (err) {
      console.warn("[bulk-create] Failed to fetch steps/bundles/next-id:", err);
      app.state.logbook._sampleCreateSteps = [];
      app.state.logbook._bulkCreateBundles = [];
      app.state.logbook._bulkCreateNextId = "...";
    }

    app.commands._renderBulkSampleCreateDrawer();
    app.commands.openRecordDrawer();
  }

  function _bulkFilamentButtonHtml(
    filamentId,
    placeholder = "Select filament",
  ) {
    const fil = app.commands.filamentMeta(filamentId);
    if (!fil)
      return `<span class="filament-selector-placeholder">${app.commands._escHtml(placeholder)}</span>`;
    return `
      <span class="color-chip" style="background:${app.commands._escAttr(fil.hex || "#cccccc")}"></span>
      <span class="filament-selector-field-name">${app.commands._escHtml(fil.color_name || fil.display_name || filamentId)}</span>
      <span class="filament-selector-field-meta">${app.commands._escHtml(fil.manufacturer || "")}</span>
    `;
  }

  function _bulkSelectedFilamentChips(filamentIds = []) {
    const ids = [...new Set((filamentIds || []).filter(Boolean))];
    if (!ids.length) return "";
    return `
      <div class="sample-batch-selected-list bulk-selected-filaments">
        ${ids
          .map((fid) => {
            const fil = app.commands.filamentMeta(fid);
            if (!fil) return "";
            return `
            <span class="sample-batch-selected-chip">
              <span class="color-chip" style="background:${app.commands._escAttr(fil.hex || "#cccccc")}"></span>
              <span>${app.commands._escHtml(fil.color_name || fil.display_name || fid)}</span>
            </span>
          `;
          })
          .join("")}
      </div>
    `;
  }

  function _bulkGeometrySlots(step) {
    return [...(step?.roles || [])]
      .sort((a, b) => Number(b.role_index || 0) - Number(a.role_index || 0))
      .map((role) => ({
        slot_id: `role:${Number(role.role_index || 0)}`,
        role_index: Number(role.role_index || 0),
        label: app.commands.formatLayerRoleLabel(role),
        role_kind: role.role_kind || "",
        fixed_thickness_mm: role.fixed_thickness_mm,
      }));
  }

  function _bulkBundleSlots(bundle) {
    return [...(bundle?.material_slots || [])]
      .sort((a, b) => Number(a.position || 0) - Number(b.position || 0))
      .map((slot) => ({
        slot_id: slot.material_slot_id,
        label:
          `Bundle Role ${slot.key || app.commands._bundleSlotKey(Number(slot.position || 0))}`.trim(),
        key: slot.key || "",
        color: app.commands._bundleSlotColor(slot.key || slot.position || 0),
      }));
  }

  function _bulkGeometrySlotAssignments(state, batchFilamentId = "") {
    const assignments = {};
    (state.slots || []).forEach((slot) => {
      assignments[slot.slot_id] =
        state.batchSlotId === slot.slot_id
          ? batchFilamentId
          : state.slotAssignments[slot.slot_id] || "";
    });
    return assignments;
  }

  function _bulkGeometryRolePayload(state, batchFilamentId = "") {
    const slotAssignments = app.commands._bulkGeometrySlotAssignments(
      state,
      batchFilamentId,
    );
    const variableSlot = (state.slots || []).find(
      (slot) => slot.role_kind === "variable",
    );
    const variableFilamentId = variableSlot
      ? slotAssignments[variableSlot.slot_id] || ""
      : "";
    const fixedByRole = new Map();
    (state.slots || []).forEach((slot) => {
      if (slot.role_kind === "fixed") {
        fixedByRole.set(
          Number(slot.role_index || 0),
          slotAssignments[slot.slot_id] || "",
        );
      }
    });
    return {
      variableFilamentId,
      fixedByRole,
      fixedIds: app.commands.canonicalFixedFilamentIdsFromMap(
        state.source || {},
        fixedByRole,
      ),
      fixedThicknesses: app.commands.fixedLayerCanonicalThicknesses(
        state.source?.fixed_layers || [],
      ),
      roleAssignments: (state.slots || []).map((slot) => ({
        role_index: Number(slot.role_index || 0),
        filament_id: slotAssignments[slot.slot_id] || "",
      })),
    };
  }

  function _bulkGeometryPreviewChipIds(state, batchFilamentId = "") {
    const payload = app.commands._bulkGeometryRolePayload(
      state,
      batchFilamentId,
    );
    return app.commands.filamentIdsFromRoleAssignments(
      state.source || {},
      payload.roleAssignments,
    );
  }

  function _bulkBundleSlotAssignments(state, batchFilamentId = "") {
    const assignments = {};
    (state.slots || []).forEach((slot) => {
      assignments[slot.slot_id] =
        state.batchSlotId === slot.slot_id
          ? batchFilamentId
          : state.slotAssignments[slot.slot_id] || "";
    });
    return assignments;
  }

  function _bulkBundlePreviewChipIds(state, member, batchFilamentId = "") {
    const assignmentBySlot = app.commands._bulkBundleSlotAssignments(
      state,
      batchFilamentId,
    );
    return [...(member?.roles || [])]
      .sort((a, b) => Number(b.role_index || 0) - Number(a.role_index || 0))
      .map((role) => assignmentBySlot[role.material_slot_id] || "");
  }

  function _bulkPreviewChips(filamentIds = []) {
    return (filamentIds || [])
      .map((fid) => {
        const fil = app.commands.filamentMeta(fid);
        const hex = fil?.hex || "#cccccc";
        const title = fil
          ? `${fil.manufacturer || ""} ${fil.color_name || fil.display_name || fid}`.trim()
          : "Unselected filament";
        return `<span class="sample-batch-preview-chip${fil ? "" : " is-missing"}" style="background:${app.commands._escAttr(hex)}" title="${app.commands._escAttr(title)}"></span>`;
      })
      .join("");
  }

  function _bulkColoredGeometryStripMiniTable(
    step,
    roleAssignments = [],
    { slotIdByRoleIndex = new Map() } = {},
  ) {
    const variableSlots = [...(step?.swatch_slots || [])].sort(
      (a, b) => Number(a.swatch_index || 0) - Number(b.swatch_index || 0),
    );
    if (!variableSlots.length) {
      return `<div class="strip-diagram-contract-error">Missing geometry swatch slots</div>`;
    }
    const swatchCount =
      variableSlots.length ||
      Number(step?.swatch_count || step?.layout_columns || 8);
    const filamentByRole = new Map(
      (roleAssignments || []).map((assignment) => [
        Number(assignment.role_index || 0),
        assignment.filament_id || "",
      ]),
    );
    const roles = [...(step?.roles || [])].sort(
      (a, b) => Number(b.role_index || 0) - Number(a.role_index || 0),
    );
    if (!roles.length) {
      return `<div class="strip-diagram-contract-error">Missing geometry role data</div>`;
    }
    const rows = roles.map((role) => {
      const roleIndex = Number(role.role_index || 0);
      const slotId =
        slotIdByRoleIndex instanceof Map
          ? slotIdByRoleIndex.get(roleIndex) || ""
          : slotIdByRoleIndex?.[roleIndex] || "";
      const slotAttr = slotId
        ? ` data-bulk-preview-slot="${app.commands._escAttr(slotId)}"`
        : "";
      const filamentId = filamentByRole.get(Number(role.role_index || 0)) || "";
      const fil = app.commands.filamentMeta(filamentId);
      const hex =
        fil?.hex || (role.role_kind === "variable" ? "#d7d7d3" : "#ececea");
      const color = app.commands.textColor(hex);
      if (role.role_kind === "variable") {
        const cells = variableSlots
          .map(
            (slot) =>
              `<td${slotAttr} style="background:${app.commands._escAttr(hex)};color:${app.commands._escAttr(color)}">${Number(slot.variable_thickness_mm || 0).toFixed(2)}</td>`,
          )
          .join("");
        return `<tr class="bulk-preview-role-row"${slotAttr}>${cells}</tr>`;
      }
      const value = Number(role.fixed_thickness_mm || 0).toFixed(2);
      return `<tr class="bulk-preview-role-row"${slotAttr}><td${slotAttr} colspan="${swatchCount}" style="background:${app.commands._escAttr(hex)};color:${app.commands._escAttr(color)}">${value}mm</td></tr>`;
    });
    return `<table class="mini-strip-table bulk-sample-diagram-table">${rows.join("")}</table>`;
  }

  function _bulkBundleMemberRoleAssignments(member, slotAssignments = {}) {
    return [...(member?.roles || [])].map((role) => ({
      role_index: Number(role.role_index || 0),
      filament_id: slotAssignments[role.material_slot_id] || "",
    }));
  }

  function _bulkGeometrySlotIdByRoleIndex(state) {
    return new Map(
      (state.slots || []).map((slot) => [
        Number(slot.role_index || 0),
        slot.slot_id,
      ]),
    );
  }

  function _bulkBundleSlotIdByRoleIndex(member) {
    return new Map(
      (member?.roles || []).map((role) => [
        Number(role.role_index || 0),
        role.material_slot_id || "",
      ]),
    );
  }

  function _bulkRoleAssignmentListHtml(step, roleAssignments = []) {
    const filamentByRole = new Map(
      (roleAssignments || []).map((assignment) => [
        Number(assignment.role_index || 0),
        assignment.filament_id || "",
      ]),
    );
    const rows = [...(step?.roles || [])]
      .sort((a, b) => Number(b.role_index || 0) - Number(a.role_index || 0))
      .map((role) => {
        const roleIndex = Number(role.role_index || 0);
        const fid = filamentByRole.get(roleIndex) || "";
        const fil = app.commands.filamentMeta(fid);
        const label = app.commands.compactLayerRoleToken(
          role.role_label,
          roleIndex,
          `LR_${String(roleIndex).padStart(2, "0")}`,
        );
        const name = fil
          ? app.commands.sampleFilamentDisplayName(fid)
          : "Unassigned";
        const chip = fil
          ? `<span class="color-chip" style="background:${app.commands._escAttr(fil.hex || "#cccccc")}"></span>`
          : `<span class="color-chip is-missing"></span>`;
        return `
          <div class="sample-set-role-assignment${fil ? "" : " is-missing"}">
            <span class="mono">${app.commands._escHtml(label)}</span>
            <span>${chip}${app.commands._escHtml(name)}</span>
          </div>
        `;
      })
      .join("");
    return `<div class="sample-set-role-list">${rows}</div>`;
  }

  function _bulkGeometryCount(state) {
    if (state.sourceKind === "bundle")
      return (state.source?.members || []).length;
    if (state.sourceKind === "geometry" && state.source) return 1;
    return 0;
  }

  function _bulkCreateCount(state) {
    const batchCount = state.batchSlotId ? state.batchFilamentIds.length : 1;
    return app.commands._bulkGeometryCount(state) * Math.max(1, batchCount);
  }

  function _bulkValidation(state) {
    if (!state.sourceKind || !state.source) {
      return {
        valid: false,
        message: "Select a single geometry or mapped geometry bundle.",
      };
    }
    if (!state.slots.length) {
      return {
        valid: false,
        message: "Selected source has no assignable role slots.",
      };
    }
    if (state.sourceKind === "bundle" && !state.source.creation_eligible) {
      return {
        valid: false,
        message:
          "Selected bundle must be fully mapped before creating samples.",
      };
    }
    if (
      state.sourceKind === "geometry" &&
      !state.slots.some((slot) => slot.role_kind === "variable")
    ) {
      return {
        valid: false,
        message:
          "Selected geometry must have a variable role to create calibration samples.",
      };
    }
    if (state.batchSlotId) {
      if (state.batchFilamentIds.length < 2) {
        return {
          valid: false,
          message:
            "Select at least two filaments, or switch the slot back to Single.",
        };
      }
    }
    for (const slot of state.slots) {
      if (slot.slot_id === state.batchSlotId) continue;
      if (!state.slotAssignments[slot.slot_id]) {
        return {
          valid: false,
          message: `Select a filament for ${slot.label}.`,
        };
      }
    }
    return { valid: true, message: "" };
  }

  function _bulkSourcePickerHtml(state) {
    if (!state.source) {
      return `<span class="filament-selector-placeholder">Select ${state.sourceKind === "bundle" ? "geometry bundle" : "single geometry"}</span>`;
    }
    if (state.sourceKind === "geometry") {
      return `
        <span class="filament-selector-field-name">${app.commands._escHtml(app.commands._geometryLabelForStep(state.source))}</span>
        <span class="filament-selector-field-meta">${app.commands._escHtml(app.commands._geometryMetaLineForStep(state.source))}</span>
      `;
    }
    const bundle = state.source;
    const roleCount = (bundle.material_slots || []).length;
    return `
      <span class="filament-selector-field-name">${app.commands._escHtml(bundle.name || bundle.alias || "Bundle")}</span>
      <span class="filament-selector-field-meta">${(bundle.step_ids || []).length} geometr${(bundle.step_ids || []).length === 1 ? "y" : "ies"} · ${roleCount} bundle role${roleCount === 1 ? "" : "s"}</span>
    `;
  }

  function _bulkSourcePreview(state) {
    if (state.sourceKind === "geometry" && state.source) {
      return app.commands.buildGeometryStripMiniTable(state.source);
    }
    if (state.sourceKind === "bundle" && state.source) {
      const steps = (state.source.step_ids || [])
        .map((stepId) => app.commands.stepRecordByRef(stepId))
        .filter(Boolean);
      return steps.length
        ? app.commands._buildBundleStepTable(steps)
        : `<div class="sample-batch-preview-empty small-copy">No current geometry records found for this bundle.</div>`;
    }
    return `<div class="sample-batch-preview-empty small-copy">Choose a source to preview its geometries.</div>`;
  }

  function openBulkBundleSelector(options = {}) {
    const selectedId = options.selectedBundleId || "";
    const bundles = app.commands._sortedBundles(options.bundles || []);
    const overlay = document.createElement("div");
    overlay.className = "info-dialog-overlay geometry-selector-overlay";
    overlay.innerHTML = `
      <div class="info-dialog geometry-selector-dialog bulk-bundle-selector-dialog" role="dialog" aria-modal="true" aria-labelledby="bulkBundleSelectorTitle">
        ${app.commands.renderDialogHeader({
          title: "Select Geometry Bundle",
          titleId: "bulkBundleSelectorTitle",
          headerClass: "geometry-selector-header",
          closeButtonHtml: app.commands.renderWindowCloseButton({
            className: "info-dialog-close",
            label: "Close selector",
            title: "Close selector",
            attributes: "data-bulk-bundle-close",
          }),
        })}
        <div class="geometry-selector-body">
          <section class="geometry-selector-panel geometry-selector-select-panel" aria-label="Select geometry bundles">
            <div class="geometry-selector-panel-head">
              <h4>Select Bundle</h4>
            </div>
            <div class="geometry-selector-panel-body bulk-bundle-selector-panel-body">
              <div class="geometry-selector-results-frame">
                <div class="geometry-selector-results" id="bulkBundleSelectorResults"></div>
              </div>
            </div>
          </section>
          <aside class="geometry-selector-panel geometry-selector-preview-panel" aria-label="Selected bundle preview">
            <div class="geometry-selector-panel-head">
              <h4>Bundle Preview</h4>
            </div>
            <div class="geometry-selector-preview" id="bulkBundleSelectorPreview"></div>
          </aside>
        </div>
        <div class="info-dialog-footer geometry-selector-footer">
          <button class="ghost-button small" type="button" data-bulk-bundle-cancel>Cancel</button>
          <button class="primary-button small" type="button" id="bulkBundleSelectorApply">Select Bundle</button>
        </div>
      </div>
    `;

    const results = overlay.querySelector("#bulkBundleSelectorResults");
    const preview = overlay.querySelector("#bulkBundleSelectorPreview");
    const applyButton = overlay.querySelector("#bulkBundleSelectorApply");
    let activeIndex = Math.max(
      0,
      bundles.findIndex(
        (bundle) =>
          (bundle?.geometry_bundle_id || bundle?.name || "") === selectedId,
      ),
    );
    let tentativeKey = selectedId || "";

    function bundleKey(bundle) {
      return bundle?.geometry_bundle_id || bundle?.name || "";
    }

    function bundleByKey(key) {
      return bundles.find((bundle) => bundleKey(bundle) === key) || null;
    }

    function bundleMemberRefs(bundle) {
      const members = bundle?.members || [];
      if (members.length) {
        return members.map((member, index) => ({
          member,
          index,
          geometryId: member.geometry_id,
          step: app.commands.stepRecordByRef(member.geometry_id),
        }));
      }
      return (bundle?.step_ids || []).map((stepId, index) => ({
        member: null,
        index,
        geometryId: stepId,
        step: app.commands.stepRecordByRef(stepId),
      }));
    }

    function bundleName(bundle) {
      return bundle?.name || bundle?.alias || "Bundle";
    }

    function bundleGeometryCount(bundle) {
      return Math.max(
        (bundle?.members || []).length,
        (bundle?.step_ids || []).length,
      );
    }

    function bundleMeta(bundle) {
      const geometryCount = bundleGeometryCount(bundle);
      const slotCount = (bundle?.material_slots || []).length;
      return `${geometryCount} geometr${geometryCount === 1 ? "y" : "ies"} · ${slotCount} shared role${slotCount === 1 ? "" : "s"}`;
    }

    function bundleSlotSummaryHtml(bundle) {
      const slots = bundle?.material_slots || [];
      return slots.length
        ? slots
            .map(
              (slot) => `
          <span class="bundle-slot-summary-chip" title="${app.commands._escAttr(slot.label || `Shared Filament ${slot.key}`)}">
            <span class="bundle-slot-summary-color" style="background:${app.commands._escAttr(app.commands._bundleSlotColor(slot.key || slot.position || 0))}"></span>
            <strong>${app.commands._escHtml(slot.key || "")}</strong>
          </span>
        `,
            )
            .join("")
        : `<span class="bundle-slot-summary-empty">No shared roles mapped</span>`;
    }

    function close() {
      overlay.remove();
    }

    function optionButtons() {
      return Array.from(
        results.querySelectorAll(".bulk-bundle-selector-option"),
      );
    }

    function activateOption(index, { focus = false } = {}) {
      const buttons = optionButtons();
      if (buttons.length === 0) {
        activeIndex = 0;
        return;
      }
      activeIndex = Math.max(0, Math.min(index, buttons.length - 1));
      buttons.forEach((button, i) => {
        const active = i === activeIndex;
        button.classList.toggle("is-active", active);
        button.tabIndex = active ? 0 : -1;
        button.setAttribute("aria-current", active ? "true" : "false");
      });
      if (focus) {
        buttons[activeIndex].focus();
        buttons[activeIndex].scrollIntoView({
          block: "nearest",
          inline: "nearest",
        });
      }
    }

    function tentativeIsVisible() {
      return optionButtons().some(
        (button) => button.dataset.bundleKey === tentativeKey,
      );
    }

    function selectedBundle() {
      return tentativeIsVisible() ? bundleByKey(tentativeKey) : null;
    }

    function renderPreview() {
      const bundle = selectedBundle();
      if (!bundle) {
        preview.innerHTML = `
          <div class="geometry-selector-preview-empty small-copy">
            Select a bundle to inspect its member geometries.
          </div>
        `;
        applyButton.disabled = true;
        return;
      }
      const selectable = !!bundle.creation_eligible;
      const members = bundleMemberRefs(bundle);
      applyButton.disabled = !selectable;
      preview.innerHTML = `
        <div class="bulk-bundle-selector-preview-summary">
          <div class="bulk-bundle-selector-preview-topline">
            <div class="geometry-selector-preview-head">
              <strong>${app.commands._escHtml(bundleName(bundle))}</strong>
              <span>${app.commands._escHtml(bundleMeta(bundle))}</span>
            </div>
            ${app.commands._renderBundleMappingStatusPill(bundle)}
          </div>
          <div class="bundle-slot-summary">${bundleSlotSummaryHtml(bundle)}</div>
          ${selectable ? "" : `<div class="bulk-bundle-selector-preview-note small-copy">Bundle is not fully mapped.</div>`}
        </div>
        ${
          members.length
            ? `
          <div class="geometry-selector-preview-list bulk-bundle-selector-preview-list">
            ${members
              .map(({ member, step, geometryId, index }) => {
                const label =
                  member?.geometry_alias ||
                  app.commands._geometryLabelForStep(step) ||
                  geometryId ||
                  "Geometry";
                return `
                <div class="geometry-selector-preview-card bulk-bundle-selector-preview-card">
                  <div class="geometry-selector-preview-head">
                    <strong>${app.commands._escHtml(label)}</strong>
                  </div>
                  <div class="geometry-selector-preview-diagram bulk-bundle-selector-preview-diagram">
                    ${step ? app.commands._buildBundleMemberPreviewDiagram(bundle, member, step) : `<div class="strip-diagram-contract-error">Missing geometry record</div>`}
                  </div>
                </div>
              `;
              })
              .join("")}
          </div>
        `
            : `<div class="geometry-selector-preview-empty small-copy">This bundle has no geometries.</div>`
        }
      `;
    }

    function updateSelectionState() {
      optionButtons().forEach((button) => {
        const bundle = bundleByKey(button.dataset.bundleKey || "");
        button.classList.toggle(
          "is-selected",
          !!bundle?.creation_eligible &&
            button.dataset.bundleKey === tentativeKey,
        );
      });
      renderPreview();
    }

    function render() {
      const filtered = bundles;
      if (filtered.length === 0) {
        activeIndex = 0;
        results.innerHTML = `<div class="geometry-selector-empty small-copy">No geometry bundles exist yet.</div>`;
        renderPreview();
        return;
      }
      results.innerHTML = filtered
        .map((bundle, index) => {
          const key = bundleKey(bundle);
          const selectable = !!bundle.creation_eligible;
          const selected = selectable && key === tentativeKey;
          return `
          <button type="button" class="geometry-selector-option bulk-bundle-selector-option${selected ? " is-selected" : ""}${selectable ? "" : " is-unavailable"}" data-bundle-key="${app.commands._escAttr(key)}" data-selectable="${selectable ? "true" : "false"}" data-option-index="${index}" tabindex="-1">
            <span class="geometry-selector-check" aria-hidden="true"></span>
            <div class="geometry-selector-option-main">
              <div class="geometry-selector-option-head">
                <span class="geometry-selector-option-name">${app.commands._escHtml(bundleName(bundle))}</span>
                <span class="geometry-selector-option-meta">${app.commands._escHtml(bundleMeta(bundle))}</span>
              </div>
              <div class="geometry-selector-option-bundles">
                ${app.commands._renderBundleMappingStatusPill(bundle)}
                ${bundleSlotSummaryHtml(bundle)}
              </div>
            </div>
          </button>
        `;
        })
        .join("");
      activateOption(activeIndex);
      updateSelectionState();
    }

    function chooseOption() {
      const bundle = selectedBundle();
      if (!bundle?.creation_eligible) return;
      try {
        if (typeof options.onApply === "function") options.onApply(bundle);
      } finally {
        close();
      }
    }

    overlay.addEventListener("pointerdown", (event) => {
      event.stopPropagation();
    });
    overlay.addEventListener("click", (event) => {
      event.stopPropagation();
      if (event.target === overlay) {
        close();
        return;
      }
      if (
        event.target.closest(
          "[data-bulk-bundle-close], [data-bulk-bundle-cancel]",
        )
      ) {
        close();
        return;
      }
      const option = event.target.closest(".bulk-bundle-selector-option");
      if (!option) return;
      activeIndex = Number(option.dataset.optionIndex || activeIndex);
      tentativeKey = option.dataset.bundleKey || "";
      activateOption(activeIndex, { focus: true });
      updateSelectionState();
    });
    applyButton.addEventListener("click", chooseOption);
    overlay.addEventListener("keydown", (event) => {
      const buttons = optionButtons();
      if (event.key === "Escape") {
        event.preventDefault();
        close();
        return;
      }
      if (event.key === "ArrowDown" || event.key === "ArrowRight") {
        if (buttons.length === 0) return;
        event.preventDefault();
        activateOption((activeIndex + 1) % buttons.length, { focus: true });
        return;
      }
      if (event.key === "ArrowUp" || event.key === "ArrowLeft") {
        if (buttons.length === 0) return;
        event.preventDefault();
        activateOption((activeIndex - 1 + buttons.length) % buttons.length, {
          focus: true,
        });
        return;
      }
      if (event.key === "Home") {
        if (buttons.length === 0) return;
        event.preventDefault();
        activateOption(0, { focus: true });
        return;
      }
      if (event.key === "End") {
        if (buttons.length === 0) return;
        event.preventDefault();
        activateOption(buttons.length - 1, { focus: true });
        return;
      }
      if (
        event.key === "Enter" &&
        event.target?.classList?.contains("bulk-bundle-selector-option")
      ) {
        event.preventDefault();
        tentativeKey = event.target.dataset.bundleKey || "";
        updateSelectionState();
      }
    });

    document.body.appendChild(overlay);
    render();
    if (optionButtons().length) activateOption(activeIndex, { focus: true });
  }

  function _renderBulkSampleCreateDrawer() {
    app.commands.setDetailSidebarStackMode("form");
    app.commands.setDrawerHeading("New Samples");
    app.dom.drawerStatusPill.innerHTML = "";
    app.dom.detailWindowArea.innerHTML = "";
    app.dom.detailActionArea.innerHTML = `
      <button class="primary-button small drawer-header-action" id="bulkSampleCreateBtn">Create</button>
    `;

    app.dom.detailSidebar.innerHTML = `
      <div class="sample-set-layout">
        <div class="sample-set-controls">
          ${app.commands.buildDrawerFormModule(
            "Select Geometry Source",
            `
            <div class="bulk-source-row">
              <div class="bulk-source-mode" role="group" aria-label="Geometry source type">
                <button type="button" class="bulk-source-mode-btn" id="bulkSourceGeometryMode" data-source-kind="geometry">Single Geometry</button>
                <button type="button" class="bulk-source-mode-btn" id="bulkSourceBundleMode" data-source-kind="bundle">Geometry Bundle</button>
              </div>
              <button type="button" class="filament-selector-field bulk-source-picker" id="bulkSourcePickerBtn"></button>
            </div>
          `,
            { density: "form" },
          )}
          ${app.commands.buildDrawerFormModule(
            "Selected Geometry Preview",
            `
            <div id="bulkSourcePreview"></div>
          `,
            { density: "compact", bodyClass: "sample-preview-module-body" },
          )}
          <div id="bulkSlotFields"></div>
          ${app.commands.buildDrawerFormModule(
            "Notes",
            `
            <div class="sample-create-field">
              <textarea id="bulkSampleNotes" class="sample-create-textarea" rows="2" placeholder="Optional notes for all samples..."></textarea>
            </div>
          `,
            { density: "large" },
          )}
          ${app.commands.buildDrawerFormModule(
            "Sample Summary",
            `
            <div class="bulk-create-summary" id="bulkCreateSummary"></div>
            <div class="sample-batch-record-preview sample-bundle-record-preview" id="bulkRecordPreview"></div>
          `,
            { density: "table" },
          )}
          <div class="filament-validation" id="bulkSampleValidation"></div>
        </div>
        <section class="sample-set-preview-panel" aria-label="Sample preview">
          <div class="sample-set-preview-cap">
            <span class="sidebar-label">Sample Preview</span>
          </div>
          <div class="sample-set-preview-body" id="bulkDiagramPreview"></div>
        </section>
      </div>
    `;

    app.commands._bindBulkSampleCreateControls();
  }

  function _bindBulkSampleCreateControls() {
    const sourceGeometryModeBtn = document.getElementById(
      "bulkSourceGeometryMode",
    );
    const sourceBundleModeBtn = document.getElementById("bulkSourceBundleMode");
    const sourcePickerBtn = document.getElementById("bulkSourcePickerBtn");
    const sourcePreviewEl = document.getElementById("bulkSourcePreview");
    const slotFieldsEl = document.getElementById("bulkSlotFields");
    const notesEl = document.getElementById("bulkSampleNotes");
    const createBtn = document.getElementById("bulkSampleCreateBtn");
    const validationEl = document.getElementById("bulkSampleValidation");
    const summaryEl = document.getElementById("bulkCreateSummary");
    const recordPreviewEl = document.getElementById("bulkRecordPreview");
    const diagramPreviewEl = document.getElementById("bulkDiagramPreview");

    const state = {
      sourceKind: "geometry",
      source: null,
      slots: [],
      slotAssignments: {},
      batchSlotId: null,
      batchFilamentIds: [],
    };

    function resetAssignments() {
      state.slotAssignments = {};
      state.batchSlotId = null;
      state.batchFilamentIds = [];
    }

    function setSourceKind(kind) {
      const nextKind = kind === "bundle" ? "bundle" : "geometry";
      if (state.sourceKind === nextKind) {
        render();
        return;
      }
      state.sourceKind = nextKind;
      state.source = null;
      state.slots = [];
      resetAssignments();
      render();
    }

    function selectGeometry(stepId) {
      const step =
        app.state.logbook._sampleCreateSteps.find(
          (candidate) => candidate.step_id === stepId,
        ) || app.commands.stepRecordByRef(stepId);
      state.sourceKind = "geometry";
      state.source = step || null;
      state.slots = app.commands._bulkGeometrySlots(step);
      resetAssignments();
      render();
    }

    function selectBundle(bundle) {
      state.sourceKind = "bundle";
      state.source = bundle || null;
      state.slots = app.commands._bulkBundleSlots(bundle);
      resetAssignments();
      render();
    }

    function setSlotMode(slotId, mode) {
      if (mode === "batch") {
        delete state.slotAssignments[slotId];
        state.batchSlotId = slotId;
        state.batchFilamentIds = [];
      } else if (state.batchSlotId === slotId) {
        state.batchSlotId = null;
        state.batchFilamentIds = [];
      }
      render();
    }

    function renderSlotRow(slot) {
      const isBatch = state.batchSlotId === slot.slot_id;
      const otherBatchActive = !!state.batchSlotId && !isBatch;
      const singleValue = state.slotAssignments[slot.slot_id] || "";
      const batchCount = state.batchFilamentIds.length;
      const roleUseCount =
        state.sourceKind === "bundle" && state.source?.members
          ? (state.source.members || []).reduce(
              (count, member) =>
                count +
                (member.roles || []).filter(
                  (role) => role.material_slot_id === slot.slot_id,
                ).length,
              0,
            )
          : 0;
      const titleMain =
        state.sourceKind === "geometry"
          ? `${app.commands.compactLayerRoleToken(slot.role_label, Number(slot.role_index || 0), `LR_${String(Number(slot.role_index || 0)).padStart(2, "0")}`)}${slot.role_kind === "fixed" ? ` Fixed${slot.fixed_thickness_mm != null ? ` - ${Number(slot.fixed_thickness_mm).toFixed(2)} mm` : ""}` : ""}`
          : slot.label;
      const titleMeta =
        state.sourceKind === "bundle"
          ? `${roleUseCount} role use${roleUseCount === 1 ? "" : "s"}`
          : "";
      const colorChip =
        state.sourceKind === "bundle"
          ? `<span class="bundle-slot-summary-color" style="background:${app.commands._escAttr(slot.color || "#cccccc")}"></span>`
          : "";
      const selectorHtml = isBatch
        ? `
          <button type="button" class="filament-selector-field filament-selector-field-multi bulk-slot-filament-btn" data-slot-id="${app.commands._escAttr(slot.slot_id)}">
            <span class="filament-selector-field-name">${batchCount ? `${batchCount} filament${batchCount === 1 ? "" : "s"} selected` : "Select filaments"}</span>
          </button>
          ${app.commands._bulkSelectedFilamentChips(state.batchFilamentIds)}
        `
        : `
          <button type="button" class="filament-selector-field bulk-slot-filament-btn" data-slot-id="${app.commands._escAttr(slot.slot_id)}">
            ${app.commands._bulkFilamentButtonHtml(singleValue)}
          </button>
        `;
      return app.commands.buildDrawerFormModule(
        `
        <span class="bulk-slot-title">${colorChip}<span class="bulk-slot-title-name">${app.commands._escHtml(titleMain)}</span>${titleMeta ? `<span class="bulk-slot-title-meta"> - ${app.commands._escHtml(titleMeta)}</span>` : ""}</span>
      `,
        `
        <div class="bulk-slot-row" data-slot-id="${app.commands._escAttr(slot.slot_id)}">
          <div class="bulk-slot-head">
            <div class="bulk-slot-mode" role="group" aria-label="${app.commands._escAttr(`${slot.label} selection mode`)}">
              <button type="button" class="bulk-slot-mode-btn${!isBatch ? " is-active" : ""}" data-slot-id="${app.commands._escAttr(slot.slot_id)}" data-mode="single">Single</button>
              <button type="button" class="bulk-slot-mode-btn${isBatch ? " is-active" : ""}" data-slot-id="${app.commands._escAttr(slot.slot_id)}" data-mode="batch" ${otherBatchActive ? "disabled" : ""}>Multi</button>
            </div>
          </div>
          ${selectorHtml}
        </div>
      `,
        {
          density: "form",
          classes: isBatch ? "bulk-slot-module is-batch" : "bulk-slot-module",
        },
      );
    }

    function renderSlots() {
      if (!slotFieldsEl) return;
      if (!state.source) {
        slotFieldsEl.innerHTML = "";
        return;
      }
      slotFieldsEl.innerHTML = state.slots.length
        ? state.slots.map(renderSlotRow).join("")
        : app.commands.buildDrawerFormModule(
            "Role Slots",
            `<div class="sample-batch-preview-empty small-copy">Selected source has no assignable role slots.</div>`,
            { density: "table" },
          );
    }

    function previewRows() {
      const validation = app.commands._bulkValidation(state);
      if (!state.source) {
        return `<div class="sample-batch-preview-empty small-copy">Select a source to preview records.</div>`;
      }
      if (!validation.valid) {
        return `<div class="sample-batch-preview-empty small-copy">${app.commands._escHtml(validation.message)}</div>`;
      }
      let offset = 0;
      if (state.sourceKind === "geometry") {
        const batchIds = state.batchSlotId ? state.batchFilamentIds : [""];
        return batchIds
          .map((batchId) => {
            const chips = app.commands._bulkPreviewChips(
              app.commands._bulkGeometryPreviewChipIds(state, batchId),
            );
            const batchFil = batchId
              ? app.commands.filamentMeta(batchId)
              : null;
            const batchLabel = batchFil
              ? batchFil.color_name || batchFil.display_name || batchId
              : app.commands._geometryLabelForStep(state.source);
            const row = `
            <div class="sample-batch-preview-row sample-bundle-preview-row">
              <span class="mono sample-batch-preview-name">${app.commands._escHtml(app.commands._sampleBatchPreviewName(app.state.logbook._bulkCreateNextId, offset))}</span>
              <span class="mono sample-bundle-preview-step" title="${app.commands._escAttr(batchLabel)}">${app.commands._escHtml(state.batchSlotId ? batchLabel : "Geometry")}</span>
              <span class="sample-batch-preview-chips">${chips}</span>
            </div>
          `;
            offset += 1;
            return row;
          })
          .join("");
      }
      const members = state.source?.members || [];
      const batchIds = state.batchSlotId ? state.batchFilamentIds : [""];
      return batchIds
        .map((batchId) => {
          const batchFil = batchId ? app.commands.filamentMeta(batchId) : null;
          const groupHeader = state.batchSlotId
            ? `
          <div class="bulk-preview-group-label">${app.commands._escHtml(batchFil?.color_name || batchFil?.display_name || batchId)}</div>
        `
            : "";
          const rows = members
            .map((member, index) => {
              const step = app.commands.stepRecordByRef(member.geometry_id);
              const chips = app.commands._bulkPreviewChips(
                app.commands._bulkBundlePreviewChipIds(state, member, batchId),
              );
              const row = `
            <div class="sample-batch-preview-row sample-bundle-preview-row">
              <span class="mono sample-batch-preview-name">${app.commands._escHtml(app.commands._sampleBatchPreviewName(app.state.logbook._bulkCreateNextId, offset))}</span>
              <span class="mono sample-bundle-preview-step" title="${app.commands._escAttr(member.geometry_alias || app.commands._geometryLabelForStep(step) || member.geometry_id)}">Geometry ${app.commands._escHtml(app.commands._bundleStepReferenceLabel(index))}</span>
              <span class="sample-batch-preview-chips">${chips}</span>
            </div>
          `;
              offset += 1;
              return row;
            })
            .join("");
          return `${groupHeader}${rows}`;
        })
        .join("");
    }

    function diagramPreviewCards() {
      if (!state.source) {
        return `<div class="sample-set-preview-empty small-copy">Select a source and filaments to preview the samples in this set.</div>`;
      }
      let offset = 0;
      if (state.sourceKind === "geometry") {
        const batchIds = state.batchSlotId
          ? state.batchFilamentIds.length
            ? state.batchFilamentIds
            : [""]
          : [""];
        const slotIdByRoleIndex =
          app.commands._bulkGeometrySlotIdByRoleIndex(state);
        return batchIds
          .map((batchId) => {
            const payload = app.commands._bulkGeometryRolePayload(
              state,
              batchId,
            );
            const batchFil = batchId
              ? app.commands.filamentMeta(batchId)
              : null;
            const batchLabel = batchFil
              ? batchFil.color_name || batchFil.display_name || batchId
              : "";
            const html = `
            <article class="sample-set-preview-card">
              <div class="sample-set-preview-card-head">
                <strong class="mono">${app.commands._escHtml(app.commands._sampleBatchPreviewName(app.state.logbook._bulkCreateNextId, offset))}</strong>
                <span title="${app.commands._escAttr(app.commands._geometryLabelForStep(state.source))}">${app.commands._escHtml(app.commands._geometryLabelForStep(state.source))}</span>
                ${batchLabel ? `<small>${app.commands._escHtml(batchLabel)}</small>` : ""}
              </div>
              <div class="sample-set-preview-card-body">
                <div class="sample-set-preview-diagram">${app.commands._bulkColoredGeometryStripMiniTable(state.source, payload.roleAssignments, { slotIdByRoleIndex })}</div>
                ${app.commands._bulkRoleAssignmentListHtml(state.source, payload.roleAssignments)}
              </div>
            </article>
          `;
            offset += 1;
            return html;
          })
          .join("");
      }
      const members = state.source?.members || [];
      const batchIds = state.batchSlotId
        ? state.batchFilamentIds.length
          ? state.batchFilamentIds
          : [""]
        : [""];
      return batchIds
        .map((batchId) => {
          const batchFil = batchId ? app.commands.filamentMeta(batchId) : null;
          const groupHeader = state.batchSlotId
            ? `
          <div class="sample-set-preview-group">${app.commands._escHtml(batchFil?.color_name || batchFil?.display_name || batchId)}</div>
        `
            : "";
          const slotAssignments = app.commands._bulkBundleSlotAssignments(
            state,
            batchId,
          );
          const cards = members
            .map((member, index) => {
              const step = app.commands.stepRecordByRef(member.geometry_id);
              const label =
                member.geometry_alias ||
                app.commands._geometryLabelForStep(step) ||
                member.geometry_id;
              const roleAssignments =
                app.commands._bulkBundleMemberRoleAssignments(
                  member,
                  slotAssignments,
                );
              const diagram = step
                ? app.commands._bulkColoredGeometryStripMiniTable(
                    step,
                    roleAssignments,
                    {
                      slotIdByRoleIndex:
                        app.commands._bulkBundleSlotIdByRoleIndex(member),
                    },
                  )
                : `<div class="strip-diagram-contract-error">Missing geometry record</div>`;
              const html = `
            <article class="sample-set-preview-card">
              <div class="sample-set-preview-card-head">
                <strong class="mono">${app.commands._escHtml(app.commands._sampleBatchPreviewName(app.state.logbook._bulkCreateNextId, offset))}</strong>
                <span title="${app.commands._escAttr(label)}">${app.commands._escHtml(label)}</span>
                <small>Geometry ${app.commands._escHtml(app.commands._bundleStepReferenceLabel(index))}</small>
              </div>
              <div class="sample-set-preview-card-body">
                <div class="sample-set-preview-diagram">${diagram}</div>
                ${step ? app.commands._bulkRoleAssignmentListHtml(step, roleAssignments) : ""}
              </div>
            </article>
          `;
              offset += 1;
              return html;
            })
            .join("");
          return `${groupHeader}${cards}`;
        })
        .join("");
    }

    function renderPreview() {
      const validation = app.commands._bulkValidation(state);
      const count = validation.valid ? app.commands._bulkCreateCount(state) : 0;
      if (summaryEl) {
        summaryEl.innerHTML = `
          <span>${count} sample${count === 1 ? "" : "s"}</span>
          ${state.batchSlotId ? `<span>${state.batchFilamentIds.length} multi filament${state.batchFilamentIds.length === 1 ? "" : "s"}</span>` : ""}
        `;
      }
      if (recordPreviewEl) {
        recordPreviewEl.innerHTML = previewRows();
      }
      if (diagramPreviewEl) {
        diagramPreviewEl.innerHTML = diagramPreviewCards();
      }
    }

    function render() {
      if (notesEl) notesEl.value = notesEl.value || "";
      if (sourceGeometryModeBtn)
        sourceGeometryModeBtn.classList.toggle(
          "is-active",
          state.sourceKind !== "bundle",
        );
      if (sourceBundleModeBtn)
        sourceBundleModeBtn.classList.toggle(
          "is-active",
          state.sourceKind === "bundle",
        );
      if (sourcePickerBtn)
        sourcePickerBtn.innerHTML = app.commands._bulkSourcePickerHtml(state);
      if (sourcePreviewEl)
        sourcePreviewEl.innerHTML = app.commands._bulkSourcePreview(state);
      renderSlots();
      renderPreview();
      if (validationEl) {
        validationEl.textContent = "";
        validationEl.className = "filament-validation";
      }
      bindDynamicControls();
    }

    function bindDynamicControls() {
      const setPreviewSlotHighlight = (slotId = "") => {
        diagramPreviewEl
          ?.querySelectorAll(".is-slot-highlight")
          .forEach((node) => node.classList.remove("is-slot-highlight"));
        slotFieldsEl
          ?.querySelectorAll(".bulk-slot-row.is-preview-target")
          .forEach((node) => node.classList.remove("is-preview-target"));
        if (!slotId) return;
        let matchedRows = 0;
        diagramPreviewEl
          ?.querySelectorAll(".bulk-preview-role-row[data-bulk-preview-slot]")
          .forEach((node) => {
            if (node.getAttribute("data-bulk-preview-slot") === slotId) {
              node.classList.add("is-slot-highlight");
              matchedRows += 1;
            }
          });
        if (!matchedRows) {
          diagramPreviewEl
            ?.querySelectorAll("td[data-bulk-preview-slot]")
            .forEach((node) => {
              if (node.getAttribute("data-bulk-preview-slot") === slotId) {
                node.classList.add("is-slot-highlight");
              }
            });
        }
        slotFieldsEl?.querySelectorAll(".bulk-slot-row").forEach((row) => {
          if (row.dataset.slotId === slotId) {
            row.classList.add("is-preview-target");
          }
        });
      };

      slotFieldsEl
        ?.querySelectorAll(".bulk-slot-mode-btn")
        .forEach((button) => {
          button.addEventListener("click", () => {
            setSlotMode(
              button.dataset.slotId || "",
              button.dataset.mode || "single",
            );
          });
        });
      slotFieldsEl?.querySelectorAll(".bulk-slot-row").forEach((row) => {
        const slotId = row.dataset.slotId || "";
        row.addEventListener("mouseenter", () =>
          setPreviewSlotHighlight(slotId),
        );
        row.addEventListener("mouseleave", () => setPreviewSlotHighlight(""));
        row.addEventListener("focusin", () => setPreviewSlotHighlight(slotId));
        row.addEventListener("focusout", (event) => {
          if (!row.contains(event.relatedTarget)) setPreviewSlotHighlight("");
        });
      });
      slotFieldsEl
        ?.querySelectorAll(".bulk-slot-filament-btn")
        .forEach((button) => {
          button.addEventListener("click", () => {
            const slotId = button.dataset.slotId || "";
            if (!slotId) return;
            const isBatch = state.batchSlotId === slotId;
            app.commands.openFilamentSelector({
              mode: isBatch ? "multi" : "single",
              title: isBatch ? "Select Filaments" : "Select Filament",
              selectedIds: isBatch
                ? state.batchFilamentIds
                : state.slotAssignments[slotId]
                  ? [state.slotAssignments[slotId]]
                  : [],
              onApply: (ids) => {
                if (isBatch) {
                  state.batchFilamentIds = [
                    ...new Set((ids || []).filter(Boolean)),
                  ];
                } else {
                  state.slotAssignments[slotId] = ids[0] || "";
                }
                render();
              },
            });
          });
        });
    }

    sourceGeometryModeBtn?.addEventListener("click", () =>
      setSourceKind("geometry"),
    );
    sourceBundleModeBtn?.addEventListener("click", () =>
      setSourceKind("bundle"),
    );
    sourcePickerBtn?.addEventListener("click", () => {
      if (state.sourceKind === "bundle") {
        app.commands.openBulkBundleSelector({
          bundles: app.state.logbook._bulkCreateBundles,
          selectedBundleId: state.source?.geometry_bundle_id || "",
          onApply: (bundle) => selectBundle(bundle),
        });
        return;
      }
      app.commands.openGeometrySelector({
        title: "Select Single Geometry",
        selectedStepId: state.source?.step_id || "",
        steps: app.state.logbook._sampleCreateSteps,
        onApply: (stepId) => selectGeometry(stepId),
      });
    });

    createBtn?.addEventListener("click", async () => {
      const validation = app.commands._bulkValidation(state);
      if (!validation.valid) {
        validationEl.textContent = validation.message;
        validationEl.className = "filament-validation is-error";
        return;
      }
      const notes = (notesEl?.value || "").trim();
      createBtn.disabled = true;
      createBtn.textContent = "Creating...";
      validationEl.textContent = "";
      validationEl.className = "filament-validation";

      try {
        let result;
        if (state.sourceKind === "geometry") {
          if (state.batchSlotId) {
            const firstBatchId = state.batchFilamentIds[0] || "";
            const payload = app.commands._bulkGeometryRolePayload(
              state,
              firstBatchId,
            );
            const batchSlot = state.slots.find(
              (slot) => slot.slot_id === state.batchSlotId,
            );
            const batchRole =
              batchSlot?.role_kind === "variable"
                ? "variable"
                : `role:${Number(batchSlot?.role_index || 0)}`;
            result = await app.api.createSampleBatch(
              state.source.step_id,
              batchRole,
              state.batchFilamentIds,
              payload.variableFilamentId,
              payload.fixedIds,
              payload.fixedThicknesses,
              payload.roleAssignments,
              notes,
            );
          } else {
            const payload = app.commands._bulkGeometryRolePayload(state);
            const created = await app.api.createSample(
              state.source.step_id,
              payload.variableFilamentId,
              payload.fixedIds,
              notes,
              payload.fixedThicknesses,
              payload.roleAssignments,
            );
            result = { created: [created], errors: [] };
          }
        } else {
          const materialAssignments = state.slots
            .filter((slot) => slot.slot_id !== state.batchSlotId)
            .map((slot) => ({
              material_slot_id: slot.slot_id,
              filament_id: state.slotAssignments[slot.slot_id],
            }));
          result = await app.api.createSamplesFromGeometryBundle({
            bundle_id: state.source.geometry_bundle_id,
            material_slot_assignments: materialAssignments,
            batch_material_slot_id: state.batchSlotId || undefined,
            batch_filament_ids: state.batchSlotId ? state.batchFilamentIds : [],
            notes,
          });
        }
        const count = (result.created || []).length;
        const errCount = (result.errors || []).length;
        let msg = `Created ${count} sample${count === 1 ? "" : "s"}`;
        if (errCount > 0)
          msg += `, ${errCount} error${errCount === 1 ? "" : "s"}`;
        app.commands.showProfileToast(msg);
        app.state.logbook._sampleDrawerMode = null;
        app.commands.clearSelectionAndDrawer();
        await app.commands.handleRefresh();
      } catch (err) {
        validationEl.textContent = err.message || "Failed to create samples";
        validationEl.className = "filament-validation is-error";
        createBtn.disabled = false;
        createBtn.textContent = "Create";
      }
    });

    render();
  }

  function buildSampleEditFixedRows(step, selectedByRole = new Map()) {
    const fixedLayers = step?.fixed_layers || [];
    if (fixedLayers.length === 0) return "";

    return app.commands
      .fixedLayerDisplayEntries(fixedLayers)
      .map(({ layer: fl, index }, displayIndex) => {
        const roleIndex = Number(fl.role_index || index + 1);
        const curFid =
          selectedByRole instanceof Map
            ? selectedByRole.get(roleIndex) || ""
            : selectedByRole[displayIndex] || "";

        return `
        <div class="drawer-subtitle-fixed">
          <button type="button" class="filament-selector-field sample-edit-fixed-select" data-fixed-index="${index}" data-role-index="${roleIndex}" data-filament-id="${app.commands._escAttr(curFid)}"></button>
          <span class="muted-line">${app.commands._escHtml(app.commands.formatLayerRoleLabel({ role_index: roleIndex, role_label: fl.role_label, role_kind: "fixed" }))}</span>
        </div>
      `;
      })
      .join("");
  }

  async function openSampleEditDrawer(exp, options = {}) {
    app.state.logbook._sampleDrawerMode = "edit";
    app.state.filaments._filamentDrawerMode = null;
    app.state.filaments._filamentDrawerData = null;
    const expanded =
      options.expanded != null
        ? !!options.expanded
        : app.state.logbook._sampleInspectExpanded;
    app.commands.setSampleInspectExpandedPreference(expanded);
    app.state.logbook.selectedRecord = { kind: "sample", id: exp.sample_id };
    app.dom.recordDrawer.classList.remove("narrow-drawer");
    app.dom.recordDrawer.classList.remove("sample-set-drawer");
    app.dom.recordDrawer.classList.remove("model-filament-drawer");
    app.dom.recordDrawer.classList.toggle("sample-expanded", expanded);
    try {
      app.state.logbook._sampleCreateSteps = (await app.api.fetchSteps()) || [];
    } catch (err) {
      console.warn("[sample-edit] Failed to fetch steps:", err);
      if (app.state.logbook._sampleCreateSteps.length === 0) {
        app.commands.syncSampleStepCacheFromData();
      }
    }
    if (
      expanded &&
      app.commands.sampleHasMeasurementOutput(exp) &&
      !exp._measurements
    ) {
      await app.commands.hydrateSampleMeasurements(exp.sample_id);
    }

    app.commands._renderSampleDrawerEdit(exp, { expanded });
    app.commands.openRecordDrawer();
  }

  function _renderSampleDrawerEdit(exp, options = {}) {
    app.commands.setDetailSidebarStackMode("form");
    const sampleId = exp.sample_id;
    const expanded =
      options.expanded != null
        ? !!options.expanded
        : app.state.logbook._sampleInspectExpanded;
    app.commands.setSampleInspectExpandedPreference(expanded);
    app.dom.recordDrawer.classList.remove("narrow-drawer");
    app.dom.recordDrawer.classList.remove("model-filament-drawer");
    app.dom.recordDrawer.classList.toggle("sample-expanded", expanded);
    app.commands.setDrawerHeading(sampleId);
    const status = app.commands.sampleStatusMeta(exp);
    app.dom.drawerStatusPill.innerHTML = `<span class="status-pill ${status.cls}">${status.label}</span>`;
    app.dom.detailWindowArea.innerHTML =
      app.commands.sampleWindowToggleButtonHtml(expanded);
    app.dom.detailActionArea.innerHTML = `
      <button class="primary-button xs drawer-header-action" id="sampleSaveBtn">Save</button>
      <button class="ghost-button xs drawer-header-action" id="sampleEditDiscardBtn">Discard</button>
      <button class="delete-button xs drawer-header-action" id="sampleEditDeleteBtn">Delete</button>
    `;

    const currentStep = app.commands.sampleStepId(exp);
    const currentVarFil = exp.variable_filament_id || "";

    const media = app.commands.resolveSampleMedia(exp);

    function safeThumb(src, label) {
      if (!src) return app.commands.placeholderThumb(label);
      return `<img class="drawer-thumb" src="${src}" alt="${label}" onload="this.style.display='block'" onerror="this.style.display='none'; this.nextElementSibling.style.display='flex'"><div class="thumb-placeholder" style="display:none"><span>${label}</span></div>`;
    }

    const selectedStepObj =
      app.state.logbook._sampleCreateSteps.find(
        (s) => s.step_id === currentStep,
      ) || null;
    const existingFixedByRole =
      app.commands.fixedFilamentIdsByRoleFromSample(exp);
    const fixedEditRows = app.commands.buildSampleEditFixedRows(
      selectedStepObj,
      existingFixedByRole,
    );
    const variableEditRole = app.commands.variableRoleForStep(selectedStepObj);

    const filamentsModule = app.commands.buildDrawerFormModule(
      "Filaments",
      `
        <div class="drawer-subtitle">
          <button type="button" id="sampleEditVarFilSelect" class="filament-selector-field sample-edit-filament-selector" data-filament-id="${app.commands._escAttr(currentVarFil)}"></button>
          <span class="muted-line">${app.commands._escHtml(variableEditRole ? app.commands.formatLayerRoleLabel(variableEditRole) : "Variable Layer")}</span>
        </div>
        <div id="sampleEditFixedRows">${fixedEditRows}</div>
      `,
      { density: "compact" },
    );

    const imagesModule = app.commands.buildDrawerFormModule(
      "Images",
      `
        <div class="drawer-image-pair">
          <div class="drawer-image-card">
            <span class="sidebar-label" style="font-size:10px">Source</span>
            ${safeThumb(media.sourceImageFile ? app.commands.previewUrl(media.sourceImageFile) : null, "No preview")}
            <span class="mono small-copy">${media.sourceName}</span>
          </div>
          <div class="drawer-image-card">
            <span class="sidebar-label" style="font-size:10px">Blank</span>
            ${safeThumb(media.blankObj?.blank_id ? app.commands.blankPreviewUrl(media.blankObj.blank_id) : null, "No blank")}
            <span class="mono small-copy">${media.blankLabel}</span>
          </div>
        </div>
      `,
      {
        density: "compact",
        actionsHtml: `
          <button class="ghost-button xs step-copy-button sample-unassign-image-btn" type="button" data-unassign-kind="source" ${exp._assigned_image ? "" : "disabled"} title="Unassign source image">Unassign source</button>
          <button class="ghost-button xs step-copy-button sample-unassign-image-btn" type="button" data-unassign-kind="blank" ${exp._assigned_blank_id ? "" : "disabled"} title="Unassign blank image">Unassign blank</button>
        `,
      },
    );

    const stripModule = app.commands.buildDrawerFormModule(
      "Strip",
      `
        <div id="sampleEditStepPreview"></div>
      `,
      { density: "compact" },
    );

    const stepModule = app.commands.buildDrawerFormModule(
      "Sample Geometry",
      `
        <div class="sample-create-field">
          <button type="button" id="sampleEditGeometrySelectBtn" class="filament-selector-field geometry-selector-field"></button>
        </div>
      `,
      { density: "form" },
    );

    const notesModule = app.commands.buildDrawerFormModule(
      "Notes",
      `
        <div class="sample-create-field">
          <textarea id="sampleEditNotes" class="sample-create-textarea sample-notes-input" rows="3" placeholder="Optional notes...">${app.commands._escHtml(exp.notes || "")}</textarea>
        </div>
      `,
      { density: "large", classes: "sample-notes-module" },
    );

    const fitControlsModule = app.commands.buildDrawerFormModule(
      "Model Fit",
      `
        <label class="filament-option-row sample-fit-option-row">
          <input id="sampleEditFitExclude" type="checkbox" ${exp._fit_exclude ? "checked" : ""}>
          <span>Exclude this sample from model fits</span>
        </label>
        ${app.commands.buildSampleSwatchFitHook(exp)}
      `,
      { density: "compact", classes: "sample-fit-controls-module" },
    );

    const validationBlock = `<div class="filament-validation" id="sampleEditValidation" style="display:none"></div>`;

    if (expanded) {
      app.dom.detailSidebar.innerHTML =
        app.commands.buildSampleInspectFrameHtml(
          `
        <div class="sample-expanded-shell">
          <div class="sample-expanded-left compact-sidebar-stack">
            ${filamentsModule}
            ${imagesModule}
            ${stripModule}
            ${stepModule}
            ${notesModule}
            ${fitControlsModule}
            ${validationBlock}
          </div>
          <div class="sample-expanded-right">
            ${app.commands.buildSampleExpandedAnalysisPane(exp)}
          </div>
        </div>
      `,
          true,
        );
    } else {
      app.dom.detailSidebar.innerHTML = `
        ${filamentsModule}
        ${imagesModule}
        ${stripModule}
        ${stepModule}
        ${notesModule}
        ${fitControlsModule}
        ${validationBlock}
      `;
    }

    app.commands._bindSampleEditControls(exp, { expanded });
  }

  function _bindSampleEditControls(exp, options = {}) {
    const sampleId = exp.sample_id;
    const geometryBtn = document.getElementById("sampleEditGeometrySelectBtn");
    const varFilSelect = document.getElementById("sampleEditVarFilSelect");
    const stepPreview = document.getElementById("sampleEditStepPreview");
    const fixedContainer = document.getElementById("sampleEditFixedRows");
    const notesEl = document.getElementById("sampleEditNotes");
    const fitExcludeEl = document.getElementById("sampleEditFitExclude");
    const saveBtn = document.getElementById("sampleSaveBtn");
    const discardBtn = document.getElementById("sampleEditDiscardBtn");
    const deleteBtn = document.getElementById("sampleEditDeleteBtn");
    const validation = document.getElementById("sampleEditValidation");
    const expanded =
      options.expanded != null
        ? !!options.expanded
        : app.state.logbook._sampleInspectExpanded;

    let selectedStep =
      app.state.logbook._sampleCreateSteps.find(
        (s) => s.step_id === app.commands.sampleStepId(exp),
      ) ||
      app.commands.stepRecordByRef(app.commands.sampleStepId(exp)) ||
      null;
    app.commands.renderGeometrySelectorField(
      geometryBtn,
      selectedStep?.step_id || "",
      "Select Geometry",
      selectedStep,
    );
    app.commands.renderFilamentSelectorField(
      varFilSelect,
      varFilSelect?.dataset?.filamentId || exp.variable_filament_id || "",
    );

    function currentVariableFilamentId() {
      return varFilSelect?.dataset?.filamentId || "";
    }

    function bindFixedSelects() {
      app.dom.detailSidebar
        .querySelectorAll(".sample-edit-fixed-select")
        .forEach((button) => {
          app.commands.renderFilamentSelectorField(
            button,
            button.dataset.filamentId || "",
          );
          button.addEventListener("click", () => {
            app.commands.openFilamentSelector({
              mode: "single",
              title: "Select Fixed Filament",
              selectedIds: button.dataset.filamentId
                ? [button.dataset.filamentId]
                : [],
              onApply: (ids) => {
                const nextId = ids[0] || "";
                app.commands.renderFilamentSelectorField(button, nextId);
                updatePreview();
              },
            });
          });
        });
    }

    function bindVariableSelect() {
      varFilSelect?.addEventListener("click", () => {
        app.commands.openFilamentSelector({
          mode: "single",
          title: "Select Variable Filament",
          selectedIds: currentVariableFilamentId()
            ? [currentVariableFilamentId()]
            : [],
          onApply: (ids) => {
            const nextId = ids[0] || "";
            app.commands.renderFilamentSelectorField(varFilSelect, nextId);
            updatePreview();
          },
        });
      });
    }

    function updatePreview() {
      const varFilId = currentVariableFilamentId();
      const varFil = app.commands.filamentMeta(varFilId);
      const varHex = varFil ? varFil.hex : "#cccccc";

      if (!selectedStep) {
        stepPreview.innerHTML = `<div class="sample-strip-tight">${app.commands.buildStripMiniTable(exp)}</div>`;
        return;
      }

      const fixedByRole = app.commands.collectFixedSelectValuesByRole(
        ".sample-edit-fixed-select",
      );
      const fixedIds = app.commands.canonicalFixedFilamentIdsFromMap(
        selectedStep,
        fixedByRole,
      );
      const fixedThicknesses = app.commands.fixedLayerCanonicalThicknesses(
        selectedStep.fixed_layers || [],
      );
      const roleAssignments = app.commands.buildRoleAssignmentsForStep(
        selectedStep,
        varFilId,
        fixedByRole,
      );

      const expLike = {
        variable_hex: varHex,
        variable_thicknesses_mm: selectedStep.variable_thicknesses_mm || [],
        fixed_thicknesses_mm: fixedThicknesses,
        fixed_filament_ids: fixedIds,
        roles: app.commands.buildAssignedGeometryRolesFromAssignments(
          selectedStep,
          roleAssignments,
        ),
      };

      stepPreview.innerHTML = `<div class="sample-strip-tight">${app.commands.buildStripMiniTable(expLike)}</div>`;
    }

    function selectStep(stepId) {
      const priorFixedByRole = app.commands.collectFixedSelectValuesByRole(
        ".sample-edit-fixed-select",
      );
      selectedStep =
        app.state.logbook._sampleCreateSteps.find(
          (s) => s.step_id === stepId,
        ) || app.commands.stepRecordByRef(stepId);
      app.commands.renderGeometrySelectorField(
        geometryBtn,
        selectedStep?.step_id || "",
        "Select Geometry",
        selectedStep,
      );

      if (selectedStep && (selectedStep.fixed_layers || []).length > 0) {
        const fallbackFixedByRole =
          priorFixedByRole.size > 0
            ? priorFixedByRole
            : app.commands.fixedFilamentIdsByRoleFromSample(exp);
        fixedContainer.innerHTML = app.commands.buildSampleEditFixedRows(
          selectedStep,
          fallbackFixedByRole,
        );
        bindFixedSelects();
      } else {
        fixedContainer.innerHTML = "";
      }

      updatePreview();
    }

    geometryBtn.addEventListener("click", () => {
      app.commands.openGeometrySelector({
        title: "Select Geometry",
        selectedStepId: selectedStep?.step_id || "",
        steps: app.state.logbook._sampleCreateSteps,
        onApply: (stepId) => selectStep(stepId),
      });
    });

    bindVariableSelect();
    bindFixedSelects();
    app.commands.bindSampleSwatchFitToggles(exp);
    updatePreview();

    const hasDerivedOutputs = Boolean(
      exp.processed ||
        ["processed", "failed", "flagged"].includes(exp._processing_status),
    );

    async function unassignSampleImage(kind, btn) {
      btn.disabled = true;
      const originalText = btn.textContent;
      btn.textContent = "Clearing...";
      try {
        if (kind === "blank") {
          await app.api.assignBlank(sampleId, null);
          app.commands.showProfileToast(`Unassigned blank from ${sampleId}`);
        } else {
          await app.api.unassignImage(sampleId);
          app.commands.showProfileToast(`Unassigned source from ${sampleId}`);
        }
        await app.commands.handleRefresh();
      } catch (err) {
        btn.disabled = false;
        btn.textContent = originalText;
        app.commands.showProfileToast(
          err.message || "Failed to unassign image",
        );
      }
    }

    app.dom.detailSidebar
      .querySelectorAll(".sample-unassign-image-btn")
      .forEach((btn) => {
        if (btn.disabled) return;
        const kind = btn.dataset.unassignKind;
        if (hasDerivedOutputs) {
          app.commands.bindConfirmAction(btn, {
            armedText: "Clear data?",
            onConfirm: () => unassignSampleImage(kind, btn),
          });
        } else {
          btn.addEventListener("click", () => unassignSampleImage(kind, btn));
        }
      });

    document
      .getElementById("toggleSampleInspectBtn")
      ?.addEventListener("click", () => {
        app.commands._renderSampleDrawerEdit(exp, { expanded: !expanded });
      });

    discardBtn.addEventListener("click", () => {
      app.state.logbook._sampleDrawerMode = null;
      app.commands.renderSidebarForSample(exp, {
        expanded: app.state.logbook._sampleInspectExpanded,
      });
    });

    if (deleteBtn) {
      app.commands.bindConfirmAction(deleteBtn, {
        armedText: "Confirm Delete",
        onConfirm: async () => {
          try {
            await app.api.deleteSample(exp.sample_id);
            app.commands.showProfileToast(`Deleted ${exp.sample_id}`);
            app.commands.clearSelectionAndDrawer();
            await app.commands.handleRefresh();
          } catch (err) {
            app.commands.showProfileToast(`Delete failed: ${err.message}`);
          }
        },
      });
    }

    saveBtn.addEventListener("click", async () => {
      const newStepId = selectedStep?.step_id || "";
      const newVarFil = currentVariableFilamentId();

      if (!newStepId) {
        validation.textContent = "Please select a sample geometry.";
        validation.className = "filament-validation is-error";
        validation.style.display = "";
        return;
      }
      if (!newVarFil) {
        validation.textContent = "Please select a variable filament.";
        validation.className = "filament-validation is-error";
        validation.style.display = "";
        return;
      }

      const fixedByRole = app.commands.collectFixedSelectValuesByRole(
        ".sample-edit-fixed-select",
      );
      const fixedIds = app.commands.canonicalFixedFilamentIdsFromMap(
        selectedStep,
        fixedByRole,
      );
      const fixedThicknesses = app.commands.fixedLayerCanonicalThicknesses(
        selectedStep.fixed_layers || [],
      );
      const roleAssignments = app.commands.buildRoleAssignmentsForStep(
        selectedStep,
        newVarFil,
        fixedByRole,
      );
      let missingFixed = false;
      fixedIds.forEach((fixedId) => {
        if (!fixedId) missingFixed = true;
      });

      if (missingFixed) {
        validation.textContent = "Please select all fixed layer filaments.";
        validation.className = "filament-validation is-error";
        validation.style.display = "";
        return;
      }

      const updates = {};
      const origStep = app.commands.sampleStepId(exp);
      const origVar = exp.variable_filament_id || "";
      const originalStepObj =
        app.state.logbook._sampleCreateSteps.find(
          (s) => s.step_id === origStep,
        ) || app.commands.stepRecordByRef(origStep);
      const origFixed = app.commands.canonicalFixedFilamentIdsFromMap(
        originalStepObj,
        app.commands.fixedFilamentIdsByRoleFromSample(exp),
      );
      const origRoleAssignments = app.commands.sampleRoleAssignmentTuple(exp);
      const newNotes = notesEl ? notesEl.value || "" : "";
      const origNotes = exp.notes || "";
      const newFitExclude = !!(fitExcludeEl && fitExcludeEl.checked);
      const origFitExclude = !!exp._fit_exclude;
      const fitExcludeChanged =
        !!fitExcludeEl && newFitExclude !== origFitExclude;

      if (newStepId !== origStep) updates.step_id = newStepId;
      if (newVarFil !== origVar) updates.variable_filament_id = newVarFil;
      if (JSON.stringify(fixedIds) !== JSON.stringify(origFixed))
        updates.fixed_filament_ids = fixedIds;
      if (newStepId !== origStep)
        updates.fixed_thicknesses_mm = fixedThicknesses;
      if (
        newStepId !== origStep ||
        JSON.stringify(roleAssignments) !== JSON.stringify(origRoleAssignments)
      ) {
        updates.role_assignments = roleAssignments;
        updates.fixed_filament_ids = fixedIds;
        updates.fixed_thicknesses_mm = fixedThicknesses;
      }
      if (newNotes !== origNotes) updates.notes = newNotes;
      const updateKeys = Object.keys(updates);

      if (updateKeys.length === 0 && !fitExcludeChanged) {
        app.commands.showProfileToast("No changes");
        app.state.logbook._sampleDrawerMode = null;
        app.commands.renderSidebarForSample(exp, {
          expanded: app.state.logbook._sampleInspectExpanded,
        });
        return;
      }

      saveBtn.disabled = true;
      saveBtn.textContent = "Saving...";
      validation.textContent = "";
      validation.className = "filament-validation";

      let savedSampleFields = false;
      try {
        if (updateKeys.length > 0) {
          await app.api.updateSample(sampleId, updates);
          savedSampleFields = true;
        }
        if (fitExcludeChanged) {
          const result = await app.api.updateSampleFitExclusion(sampleId, {
            fit_exclude: newFitExclude,
          });
          app.commands.applyFitControlMutationResponse(result);
        }
        app.commands.showProfileToast(`Updated ${sampleId}`);
        app.state.logbook._sampleDrawerMode = null;
        await app.commands.handleRefresh();
      } catch (err) {
        validation.textContent = savedSampleFields
          ? `Sample fields were saved, but model-fit control failed: ${err.message || "Unknown error"}`
          : err.message || "Failed to update sample";
        validation.className = "filament-validation is-error";
        saveBtn.disabled = false;
        saveBtn.textContent = "Save";
      }
    });
  }

  Object.assign(app.commands, {
    syncSampleStepCacheFromData,
    renderFilamentSelectorField,
    _sampleBatchPreviewName,
    _bundleStepReferenceLabel,
    _buildBundleStepTable,
    openBulkSampleCreateDrawer,
    _bulkFilamentButtonHtml,
    _bulkSelectedFilamentChips,
    _bulkGeometrySlots,
    _bulkBundleSlots,
    _bulkGeometrySlotAssignments,
    _bulkGeometryRolePayload,
    _bulkGeometryPreviewChipIds,
    _bulkBundleSlotAssignments,
    _bulkBundlePreviewChipIds,
    _bulkPreviewChips,
    _bulkColoredGeometryStripMiniTable,
    _bulkBundleMemberRoleAssignments,
    _bulkGeometrySlotIdByRoleIndex,
    _bulkBundleSlotIdByRoleIndex,
    _bulkRoleAssignmentListHtml,
    _bulkGeometryCount,
    _bulkCreateCount,
    _bulkValidation,
    _bulkSourcePickerHtml,
    _bulkSourcePreview,
    openBulkBundleSelector,
    _renderBulkSampleCreateDrawer,
    _bindBulkSampleCreateControls,
    buildSampleEditFixedRows,
    openSampleEditDrawer,
    _renderSampleDrawerEdit,
    _bindSampleEditControls,
  });
}
