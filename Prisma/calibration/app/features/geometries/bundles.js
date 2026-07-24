/** Install features/geometries/bundles commands. */
export function installFeaturesGeometriesBundles(app) {
  function isBundleMgmtOpen() {
    return (
      app.dom.bundleMgmtDrawer &&
      app.dom.bundleMgmtDrawer.classList.contains("is-open")
    );
  }

  function closeBundleMgmtDrawer() {
    if (!app.dom.bundleMgmtDrawer) return;
    app.dom.bundleMgmtDrawer.classList.remove("is-open");
    app.dom.bundleMgmtDrawer.setAttribute("aria-hidden", "true");
    if (app.dom.bundleMgmtBody) app.dom.bundleMgmtBody.innerHTML = "";
    app.state.geometries._bundleDrawerState.selectedBundleName = null;
    app.state.geometries._bundleDrawerState.showNewInput = false;
    app.state.geometries._bundleDrawerState.renamingBundleName = null;
  }

  function handleOutsideDrawerDismiss(event) {
    const target = event.target;
    if (!(target instanceof Element)) return;

    const anyDrawerOpen =
      app.dom.recordDrawer?.classList.contains("is-open") ||
      app.dom.linkedSampleDrawer?.classList.contains("is-open") ||
      app.commands.isStepBuilderOpen() ||
      app.commands.isBundleMgmtOpen();
    if (!anyDrawerOpen) return;

    if (
      target.closest(
        ".record-drawer, .linked-record-drawer, .step-builder-drawer, .bundle-mgmt-drawer, .manual-proc-overlay, .image-lightbox-overlay",
      )
    ) {
      return;
    }

    if (
      target.closest(
        "button, a, input, select, textarea, label, canvas, [role='button'], .data-row, .import-section-title",
      )
    ) {
      return;
    }

    if (app.dom.recordDrawer?.classList.contains("is-open")) {
      app.commands.clearSelectionAndDrawer();
    }
    if (app.commands.isStepBuilderOpen()) {
      app.commands.closeStepBuilderDrawer();
    }
    if (app.commands.isBundleMgmtOpen()) {
      app.commands.closeBundleMgmtDrawer();
    }
  }

  async function openBundleManagementDrawer() {
    // Close other drawers — mutually exclusive
    app.commands.closeDrawer();
    app.commands.closeStepBuilderDrawer();

    try {
      app.state.geometries._bundleDrawerState.bundles =
        await app.api.fetchBundles();
      const bundles = app.commands._sortedBundles(
        app.state.geometries._bundleDrawerState.bundles || [],
      );
      app.state.geometries._bundleDrawerState.selectedBundleName =
        bundles[0]?.name || null;
      app.state.geometries._bundleDrawerState.renamingBundleName = null;
    } catch (err) {
      app.commands.showImportToast(
        "Failed to load bundles: " + err.message,
        "error",
      );
      return;
    }

    app.dom.bundleMgmtDrawer.classList.add("is-open");
    app.dom.bundleMgmtDrawer.setAttribute("aria-hidden", "false");
    app.commands.renderBundleMgmtBody();
    app.commands.bindBundleMgmtEvents();
  }

  function renderBundleMgmtBody() {
    if (!app.dom.bundleMgmtBody) return;
    const bundles = app.commands._sortedBundles(
      app.state.geometries._bundleDrawerState.bundles || [],
    );
    const selected = app.commands._selectedBundle();
    const selectedStepIds = selected?.step_ids || [];
    const availableSteps =
      app.commands._availableBundleGeometries(selectedStepIds);

    const newBundleBlock = `
      <div class="bundle-new-input-row" id="bundleNewInputRow">
        <input type="text" id="bundleNewNameInput" placeholder="New bundle name..." />
        <button class="primary-button small" id="bundleNewSaveBtn">Create</button>
      </div>
    `;

    const bundleList = bundles.length
      ? bundles
          .map((bundle) => {
            const count = (bundle.step_ids || []).length;
            const isSelected = selected?.name === bundle.name;
            return `
            <button type="button" class="bundle-list-item${isSelected ? " is-selected" : ""}" data-bundle="${app.commands._escAttr(bundle.name)}">
              <span class="bundle-list-main">
                <span class="bundle-list-name">${app.commands._escHtml(bundle.name)}</span>
                <span class="bundle-list-count" aria-label="${count} geometr${count === 1 ? "y" : "ies"}">(${count})</span>
              </span>
              ${app.commands._renderBundleMappingStatusPill(bundle)}
            </button>
          `;
          })
          .join("")
      : `<div class="bundle-empty-msg">No bundles defined yet.</div>`;

    const detailHtml = selected
      ? app.commands._renderBundleDetail(
          selected,
          selectedStepIds,
          availableSteps,
        )
      : `
        <div class="bundle-detail-empty">
          <strong>No bundle selected</strong>
          <span>Create a bundle or select one from the list.</span>
        </div>
      `;

    app.dom.bundleMgmtBody.innerHTML = `
      <div class="bundle-manager-layout">
        <section class="bundle-list-pane" aria-label="Geometry bundles">
          <div class="bundle-pane-cap">
            <span class="sidebar-label">Geometry Bundles</span>
          </div>
          <div class="bundle-pane-body">
            ${newBundleBlock}
            <div class="bundle-list">${bundleList}</div>
          </div>
        </section>
        <section class="bundle-detail-pane" aria-label="Selected bundle">
          ${detailHtml}
        </section>
      </div>
    `;
    app.commands.bindBundleMgmtInteractions();
  }

  function _sortedBundles(bundles) {
    return [...(bundles || [])].sort((a, b) =>
      (a.name || "").localeCompare(b.name || "", undefined, {
        sensitivity: "base",
      }),
    );
  }

  function _selectedBundle() {
    const bundles = app.commands._sortedBundles(
      app.state.geometries._bundleDrawerState.bundles || [],
    );
    if (bundles.length === 0) return null;
    const selected = bundles.find(
      (bundle) =>
        bundle.name ===
        app.state.geometries._bundleDrawerState.selectedBundleName,
    );
    if (selected) return selected;
    app.state.geometries._bundleDrawerState.selectedBundleName =
      bundles[0].name;
    return bundles[0];
  }

  function _bundleSlotKey(position) {
    let n = Math.max(0, Number(position) || 0);
    let label = "";
    while (true) {
      const rem = n % 26;
      label = String.fromCharCode(65 + rem) + label;
      n = Math.floor(n / 26) - 1;
      if (n < 0) return label;
    }
  }

  function _bundleSlotColor(positionOrKey) {
    let index = Number(positionOrKey);
    if (typeof positionOrKey === "string") {
      index = 0;
      for (const char of positionOrKey.toUpperCase()) {
        const code = char.charCodeAt(0) - 64;
        if (code < 1 || code > 26) break;
        index = index * 26 + code;
      }
      index = Math.max(0, index - 1);
    }
    if (!Number.isFinite(index)) index = 0;
    return app.constants.BUNDLE_MAPPING_SLOT_COLORS[
      index % app.constants.BUNDLE_MAPPING_SLOT_COLORS.length
    ];
  }

  function _bundleStatusMeta(bundle) {
    const status = String(bundle?.mapping_status || "unmapped").toLowerCase();
    if (status === "mapped") return { label: "Mapped", className: "mapped" };
    if (status === "incomplete")
      return { label: "Incomplete", className: "incomplete" };
    if (status === "invalid") return { label: "Invalid", className: "failed" };
    return { label: "Unmapped", className: "unmapped" };
  }

  function _renderBundleMappingStatusPill(bundle) {
    const meta = app.commands._bundleStatusMeta(bundle);
    return `<span class="status-pill bundle-status-pill ${app.commands._escAttr(meta.className)}">${app.commands._escHtml(meta.label)}</span>`;
  }

  function _bundleSlotById(bundle, materialSlotId) {
    return (
      (bundle?.material_slots || []).find(
        (slot) => slot.material_slot_id === materialSlotId,
      ) || null
    );
  }

  function _renderBundleMappingChips(bundle, member) {
    const roles = member?.roles || [];
    if (!roles.length)
      return `<span class="bundle-role-chip is-unmapped">No roles</span>`;
    return roles
      .map((role) => {
        const slot = app.commands._bundleSlotById(
          bundle,
          role.material_slot_id,
        );
        const roleLabel = app.commands.formatLayerRoleLabel(role);
        if (!slot) {
          return `<span class="bundle-role-chip is-unmapped" title="${app.commands._escAttr(roleLabel)}">${app.commands._escHtml(roleLabel)} · unmapped</span>`;
        }
        const color = app.commands._bundleSlotColor(
          slot.key || slot.position || 0,
        );
        return `
        <span class="bundle-role-chip" title="${app.commands._escAttr(`${roleLabel}: Shared Filament ${slot.key}`)}">
          <span class="bundle-role-chip-color" style="background:${app.commands._escAttr(color)}"></span>
          <span>${app.commands._escHtml(roleLabel)}</span>
          <strong>${app.commands._escHtml(slot.key)}</strong>
        </span>
      `;
      })
      .join("");
  }

  function _buildBundleMemberPreviewDiagram(bundle, member, step) {
    const variableSlots = [...(step?.swatch_slots || [])].sort(
      (a, b) => Number(a.swatch_index || 0) - Number(b.swatch_index || 0),
    );
    if (!variableSlots.length) {
      return `<div class="strip-diagram-contract-error">Missing geometry swatch slots</div>`;
    }
    const stepRoles = [...(step?.roles || [])];
    const memberRoles = [...(member?.roles || [])];
    const roleSource = stepRoles.length ? stepRoles : memberRoles;
    const roles = roleSource
      .map((role) => ({
        ...role,
        role_index: Number(role.role_index || 0),
        _memberRole:
          memberRoles.find(
            (candidate) =>
              Number(candidate.role_index || 0) ===
              Number(role.role_index || 0),
          ) || null,
      }))
      .sort((a, b) => Number(b.role_index || 0) - Number(a.role_index || 0));
    if (!roles.length) {
      return `<div class="strip-diagram-contract-error">Missing geometry role data</div>`;
    }

    const swatchCount =
      variableSlots.length ||
      Number(step?.swatch_count || step?.layout_columns || 8);
    const rowHtml = [];
    const labelHtml = [];
    roles.forEach((role) => {
      const memberRole = role._memberRole || {};
      const roleIndex = Number(role.role_index || 0);
      const roleKind = String(
        memberRole.role_kind || role.role_kind || "",
      ).toLowerCase();
      const slot = app.commands._bundleSlotById(
        bundle,
        memberRole.material_slot_id || role.material_slot_id || "",
      );
      const slotKey = slot
        ? slot.key || app.commands._bundleSlotKey(Number(slot.position || 0))
        : "";
      const slotColor = slot
        ? app.commands._bundleSlotColor(slotKey || slot.position || 0)
        : roleKind === "variable"
          ? "#d7d7d3"
          : "#ececea";
      const rowStyle = `--bundle-preview-row-color:${app.commands._escAttr(slotColor)}`;
      const roleToken = app.commands.compactLayerRoleToken(
        memberRole.role_label || role.role_label,
        roleIndex,
        `LR_${String(roleIndex).padStart(2, "0")}`,
      );
      labelHtml.push(`
        <div class="bundle-selector-role-map-label${slot ? "" : " is-unmapped"}">
          <span>${app.commands._escHtml(roleToken)} -&gt;</span>
          <strong>${app.commands._escHtml(slotKey || "?")}</strong>
        </div>
      `);

      if (roleKind === "variable") {
        const cells = variableSlots
          .map(
            (slotInfo) =>
              `<td style="${rowStyle}">${Number(slotInfo.variable_thickness_mm || 0).toFixed(2)}</td>`,
          )
          .join("");
        rowHtml.push(`<tr>${cells}</tr>`);
        return;
      }
      const fixedThickness =
        memberRole.fixed_thickness_mm ?? role.fixed_thickness_mm;
      const thickness = Number(fixedThickness);
      const label = Number.isFinite(thickness)
        ? `${thickness.toFixed(2)}mm`
        : "";
      rowHtml.push(
        `<tr><td colspan="${swatchCount}" style="${rowStyle}">${app.commands._escHtml(label)}</td></tr>`,
      );
    });

    return `
      <div class="bundle-selector-member-diagram">
        <div class="bundle-selector-role-map-labels">${labelHtml.join("")}</div>
        <table class="mini-strip-table bulk-bundle-strip-table">${rowHtml.join("")}</table>
      </div>
    `;
  }

  function _geometryAliasFromRef(stepRef) {
    const step = app.commands.stepRecordByRef(stepRef);
    return (
      step?.alias ||
      step?.name ||
      app.commands.stepFileNameFromRef(stepRef) ||
      stepRef ||
      ""
    );
  }

  function _geometryLabelForStep(step) {
    return (
      step?.alias || step?.name || step?.display_name || "Unnamed geometry"
    );
  }

  function _geometryMetaLineForStep(step) {
    if (!step) return "";
    const pieces = [];
    if (step.swatch_count != null) pieces.push(`${step.swatch_count} swatches`);
    else if (Array.isArray(step.variable_thicknesses_mm))
      pieces.push(`${step.variable_thicknesses_mm.length} swatches`);
    if (step.layer_count != null)
      pieces.push(
        `${step.layer_count} role${Number(step.layer_count) === 1 ? "" : "s"}`,
      );
    return pieces.filter(Boolean).join(" · ");
  }

  function _geometryMetaLine(stepRef) {
    return app.commands._geometryMetaLineForStep(
      app.commands.stepRecordByRef(stepRef),
    );
  }

  function _geometryBundleNames(stepRef) {
    const stepId = app.commands.stepIdFromRef(stepRef);
    const names = new Set();
    const step = app.commands.stepRecordByRef(stepId);
    (step?.bundle_names || []).forEach((name) => {
      if (name) names.add(name);
    });
    const metaBundle =
      app.commands.stepMeta(stepId).bundle || step?.bundle || "";
    if (metaBundle) names.add(metaBundle);
    (app.state.session.data.bundles || []).forEach((bundle) => {
      if ((bundle.step_ids || []).includes(stepId) && bundle.name)
        names.add(bundle.name);
    });
    return Array.from(names).sort((a, b) =>
      a.localeCompare(b, undefined, { sensitivity: "base" }),
    );
  }

  function _geometrySelectorSearchText(step) {
    return [
      app.commands._geometryLabelForStep(step),
      app.commands._geometryMetaLineForStep(step),
      ...app.commands._geometryBundleNames(step?.step_id),
    ]
      .filter(Boolean)
      .join(" ")
      .toLowerCase();
  }

  function _availableBundleGeometries(selectedStepIds) {
    const selected = new Set(selectedStepIds || []);
    return [...(app.state.session.data.steps || [])]
      .filter((step) => step?.step_id && !selected.has(step.step_id))
      .sort((a, b) =>
        app.commands
          ._geometryLabelForStep(a)
          .localeCompare(app.commands._geometryLabelForStep(b), undefined, {
            sensitivity: "base",
          }),
      );
  }

  function availableGeometryRecords() {
    return [...(app.state.session.data.steps || [])]
      .filter(
        (step) => step?.step_id && !app.commands.stepMeta(step.step_id).deleted,
      )
      .sort((a, b) =>
        app.commands
          ._geometryLabelForStep(a)
          .localeCompare(app.commands._geometryLabelForStep(b), undefined, {
            sensitivity: "base",
          }),
      );
  }

  function renderGeometrySelectorField(
    button,
    stepId,
    placeholder = "Select Geometry",
    stepRecord = null,
  ) {
    if (!button) return;
    const step = stepRecord || app.commands.stepRecordByRef(stepId);
    if (!step) {
      button.dataset.stepId = "";
      button.innerHTML = `<span class="filament-selector-placeholder">${app.commands._escHtml(placeholder)}</span>`;
      return;
    }
    const bundles = app.commands._geometryBundleNames(step.step_id);
    const bundleText = bundles.length ? bundles.join(", ") : "No bundle";
    button.dataset.stepId = step.step_id;
    button.innerHTML = `
      <span class="filament-selector-field-name">${app.commands._escHtml(app.commands._geometryLabelForStep(step))}</span>
      <span class="filament-selector-field-meta">${app.commands._escHtml(app.commands._geometryMetaLineForStep(step))}</span>
      <span class="geometry-selector-field-bundles" title="${app.commands._escAttr(bundleText)}">${app.commands._escHtml(bundleText)}</span>
    `;
  }

  function openGeometrySelector(options = {}) {
    const mode = options.mode === "multi" ? "multi" : "single";
    const isMulti = mode === "multi";
    const selectedId = options.selectedStepId || "";
    const selectedIds = new Set(
      options.selectedStepIds || (selectedId ? [selectedId] : []),
    );
    const selectPanelTitle =
      options.selectPanelTitle ||
      (isMulti ? "Select Geometries" : "Select Geometry");
    const previewPanelTitle =
      options.previewPanelTitle ||
      (isMulti ? "Selected Geometry Preview" : "Geometry Preview");
    const overlay = document.createElement("div");
    overlay.className = "info-dialog-overlay geometry-selector-overlay";
    overlay.innerHTML = `
      <div class="info-dialog geometry-selector-dialog" role="dialog" aria-modal="true" aria-labelledby="geometrySelectorTitle">
        ${app.commands.renderDialogHeader({
          title: options.title || "Select Geometry",
          titleId: "geometrySelectorTitle",
          headerClass: "geometry-selector-header",
          closeButtonHtml: app.commands.renderWindowCloseButton({
            className: "info-dialog-close",
            label: "Close selector",
            title: "Close selector",
            attributes: "data-geometry-selector-close",
          }),
        })}
        <div class="geometry-selector-body">
          <section class="geometry-selector-panel geometry-selector-select-panel" aria-label="Select sample geometries">
            <div class="geometry-selector-panel-head">
              <h4>${app.commands._escHtml(selectPanelTitle)}</h4>
            </div>
            <div class="geometry-selector-panel-body">
              <div class="geometry-selector-filter-group">
                <div class="geometry-selector-filter-title">Filter By:</div>
                <div class="geometry-selector-controls">
                  <label class="geometry-selector-filter">
                    <span>Bundle</span>
                    <select id="geometrySelectorBundleFilter"></select>
                  </label>
                  <label class="geometry-selector-filter">
                    <span>Name</span>
                    <input class="geometry-selector-search" id="geometrySelectorSearch" type="search" placeholder="Search geometries">
                  </label>
                </div>
              </div>
              <div class="geometry-selector-results-frame">
                <div class="geometry-selector-results" id="geometrySelectorResults"></div>
              </div>
            </div>
          </section>
          <aside class="geometry-selector-panel geometry-selector-preview-panel" aria-label="Selected geometry preview">
            <div class="geometry-selector-panel-head">
              <h4>${app.commands._escHtml(previewPanelTitle)}</h4>
            </div>
            <div class="geometry-selector-preview" id="geometrySelectorPreview"></div>
          </aside>
        </div>
        <div class="info-dialog-footer geometry-selector-footer">
          <button class="ghost-button small" type="button" id="geometrySelectorCancel">Cancel</button>
          <button class="primary-button small" type="button" id="geometrySelectorApply">${app.commands._escHtml(options.applyLabel || (isMulti ? "Add Selected" : "Select Geometry"))}</button>
        </div>
      </div>
    `;

    const search = overlay.querySelector("#geometrySelectorSearch");
    const bundleFilter = overlay.querySelector("#geometrySelectorBundleFilter");
    const results = overlay.querySelector("#geometrySelectorResults");
    const preview = overlay.querySelector("#geometrySelectorPreview");
    const applyButton = overlay.querySelector("#geometrySelectorApply");
    let activeIndex = 0;
    let tentativeId = selectedId;

    function close() {
      overlay.remove();
    }

    function optionButtons() {
      return Array.from(results.querySelectorAll(".geometry-selector-option"));
    }

    function sourceGeometryRecords() {
      return Array.isArray(options.steps)
        ? options.steps
        : app.commands.availableGeometryRecords();
    }

    function sourceGeometryById(stepId) {
      return (
        sourceGeometryRecords().find((step) => step?.step_id === stepId) ||
        app.commands.stepRecordByRef(stepId)
      );
    }

    function bundleFilterOptions() {
      const bundleNames = new Set();
      let hasUnbundled = false;
      sourceGeometryRecords().forEach((step) => {
        const names = app.commands._geometryBundleNames(step?.step_id);
        if (names.length === 0) {
          hasUnbundled = true;
          return;
        }
        names.forEach((name) => bundleNames.add(name));
      });
      const options = [
        `<option value="">All geometries</option>`,
        ...Array.from(bundleNames)
          .sort((a, b) =>
            a.localeCompare(b, undefined, { sensitivity: "base" }),
          )
          .map(
            (name) =>
              `<option value="${app.commands._escAttr(name)}">${app.commands._escHtml(name)}</option>`,
          ),
      ];
      if (hasUnbundled) {
        options.push(`<option value="__none__">No bundle</option>`);
      }
      return options.join("");
    }

    function activateOption(index, { focus = false } = {}) {
      const options = optionButtons();
      if (options.length === 0) {
        activeIndex = 0;
        return;
      }
      activeIndex = Math.max(0, Math.min(index, options.length - 1));
      options.forEach((button, i) => {
        const isActive = i === activeIndex;
        button.classList.toggle("is-active", isActive);
        button.tabIndex = isActive ? 0 : -1;
        button.setAttribute("aria-current", isActive ? "true" : "false");
      });
      if (focus) {
        options[activeIndex].focus();
        options[activeIndex].scrollIntoView({
          block: "nearest",
          inline: "nearest",
        });
      }
    }

    function updateSelectionState() {
      optionButtons().forEach((button) => {
        const stepId = button.dataset.stepId || "";
        button.classList.toggle(
          "is-selected",
          isMulti ? selectedIds.has(stepId) : stepId === tentativeId,
        );
      });
      renderPreview();
    }

    function tentativeIsVisible() {
      return optionButtons().some(
        (button) => button.dataset.stepId === tentativeId,
      );
    }

    function chooseOption() {
      if (isMulti) {
        const chosenIds = Array.from(selectedIds).filter((stepId) =>
          sourceGeometryById(stepId),
        );
        const chosenSteps = chosenIds
          .map((stepId) => sourceGeometryById(stepId))
          .filter(Boolean);
        if (chosenSteps.length === 0) return;
        try {
          if (typeof options.onApply === "function") {
            options.onApply(chosenIds, chosenSteps);
          }
        } finally {
          close();
        }
        return;
      }
      if (!tentativeIsVisible()) return;
      const step = sourceGeometryById(tentativeId);
      if (!step) return;
      try {
        if (typeof options.onApply === "function") {
          options.onApply(step.step_id, step);
        }
      } finally {
        close();
      }
    }

    function renderPreview() {
      if (isMulti) {
        const selectedSteps = Array.from(selectedIds)
          .map((stepId) => sourceGeometryById(stepId))
          .filter(Boolean);
        if (selectedSteps.length === 0) {
          preview.innerHTML = `
            <div class="geometry-selector-preview-empty small-copy">
              Select one or more geometries to add to the bundle.
            </div>
          `;
          applyButton.disabled = true;
          return;
        }
        preview.innerHTML = `
          <div class="geometry-selector-selection-count">${selectedSteps.length} selected</div>
          <div class="geometry-selector-preview-list">
            ${selectedSteps
              .map((step) => {
                const bundles = app.commands._geometryBundleNames(step.step_id);
                const bundleHtml = bundles.length
                  ? bundles
                      .map(
                        (name) =>
                          `<span class="geometry-selector-bundle-chip">${app.commands._escHtml(name)}</span>`,
                      )
                      .join("")
                  : `<span class="geometry-selector-bundle-empty">No bundle</span>`;
                return `
                <div class="geometry-selector-preview-card">
                  <div class="geometry-selector-preview-head">
                    <strong>${app.commands._escHtml(app.commands._geometryLabelForStep(step))}</strong>
                  </div>
                  <div class="geometry-selector-option-bundles">${bundleHtml}</div>
                  <div class="geometry-selector-preview-diagram">${app.commands.buildGeometryStripMiniTable(step)}</div>
                </div>
              `;
              })
              .join("")}
          </div>
        `;
        applyButton.disabled = false;
        return;
      }
      const step = tentativeIsVisible()
        ? sourceGeometryById(tentativeId)
        : null;
      if (!step) {
        preview.innerHTML = `
          <div class="geometry-selector-preview-empty small-copy">
            Select a geometry to inspect its strip diagram.
          </div>
        `;
        applyButton.disabled = true;
        return;
      }
      const bundles = app.commands._geometryBundleNames(step.step_id);
      const bundleHtml = bundles.length
        ? bundles
            .map(
              (name) =>
                `<span class="geometry-selector-bundle-chip">${app.commands._escHtml(name)}</span>`,
            )
            .join("")
        : `<span class="geometry-selector-bundle-empty">No bundle</span>`;
      preview.innerHTML = `
        <div class="geometry-selector-preview-head">
          <strong>${app.commands._escHtml(app.commands._geometryLabelForStep(step))}</strong>
          <span>${app.commands._escHtml(app.commands._geometryMetaLineForStep(step))}</span>
        </div>
        <div class="geometry-selector-option-bundles">${bundleHtml}</div>
        <div class="geometry-selector-preview-diagram">${app.commands.buildGeometryStripMiniTable(step)}</div>
      `;
      applyButton.disabled = false;
    }

    function render() {
      const q = (search.value || "").trim().toLowerCase();
      const selectedBundle = bundleFilter.value || "";
      const geometries = sourceGeometryRecords().filter((step) => {
        const bundles = app.commands._geometryBundleNames(step?.step_id);
        const bundleMatches =
          !selectedBundle ||
          (selectedBundle === "__none__"
            ? bundles.length === 0
            : bundles.includes(selectedBundle));
        const searchMatches =
          !q || app.commands._geometrySelectorSearchText(step).includes(q);
        return bundleMatches && searchMatches;
      });
      if (geometries.length === 0) {
        activeIndex = 0;
        results.innerHTML = `<div class="geometry-selector-empty small-copy">No matching geometries</div>`;
        renderPreview();
        return;
      }
      results.innerHTML = geometries
        .map((step, index) => {
          const alias = app.commands._geometryLabelForStep(step);
          const meta = app.commands._geometryMetaLineForStep(step);
          const bundles = app.commands._geometryBundleNames(step.step_id);
          const isSelected = isMulti
            ? selectedIds.has(step.step_id)
            : step.step_id === tentativeId;
          const bundleHtml = bundles.length
            ? bundles
                .map(
                  (name) =>
                    `<span class="geometry-selector-bundle-chip">${app.commands._escHtml(name)}</span>`,
                )
                .join("")
            : `<span class="geometry-selector-bundle-empty">No bundle</span>`;
          return `
          <button type="button" class="geometry-selector-option${isSelected ? " is-selected" : ""}" data-step-id="${app.commands._escAttr(step.step_id)}" data-option-index="${index}" tabindex="-1">
            <span class="geometry-selector-check" aria-hidden="true"></span>
            <div class="geometry-selector-option-main">
              <div class="geometry-selector-option-head">
                <span class="geometry-selector-option-name">${app.commands._escHtml(alias)}</span>
                <span class="geometry-selector-option-meta">${app.commands._escHtml(meta)}</span>
              </div>
              <div class="geometry-selector-option-bundles">${bundleHtml}</div>
            </div>
          </button>
        `;
        })
        .join("");
      activateOption(activeIndex);
      updateSelectionState();
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
      const closeBtn = event.target.closest(
        "[data-geometry-selector-close], #geometrySelectorCancel",
      );
      if (closeBtn) {
        close();
        return;
      }
      const option = event.target.closest(".geometry-selector-option");
      if (!option) return;
      activeIndex = Number(option.dataset.optionIndex || activeIndex);
      tentativeId = option.dataset.stepId || "";
      if (isMulti) {
        if (selectedIds.has(tentativeId)) {
          selectedIds.delete(tentativeId);
        } else if (tentativeId) {
          selectedIds.add(tentativeId);
        }
      }
      activateOption(activeIndex, { focus: true });
      updateSelectionState();
    });

    applyButton.addEventListener("click", chooseOption);
    search.addEventListener("input", () => {
      activeIndex = 0;
      render();
    });
    bundleFilter.addEventListener("change", () => {
      activeIndex = 0;
      render();
      search.focus();
    });
    search.addEventListener("keydown", (event) => {
      if (event.key !== "Tab" || event.shiftKey) return;
      const options = optionButtons();
      if (options.length === 0) return;
      event.preventDefault();
      activateOption(activeIndex, { focus: true });
    });
    overlay.addEventListener("keydown", (event) => {
      const options = optionButtons();
      if (event.key === "Escape") {
        event.preventDefault();
        close();
        return;
      }
      if (event.key === "ArrowDown" || event.key === "ArrowRight") {
        if (options.length === 0) return;
        event.preventDefault();
        activateOption((activeIndex + 1) % options.length, { focus: true });
        return;
      }
      if (event.key === "ArrowUp" || event.key === "ArrowLeft") {
        if (options.length === 0) return;
        event.preventDefault();
        activateOption((activeIndex - 1 + options.length) % options.length, {
          focus: true,
        });
        return;
      }
      if (event.key === "Home") {
        if (options.length === 0) return;
        event.preventDefault();
        activateOption(0, { focus: true });
        return;
      }
      if (event.key === "End") {
        if (options.length === 0) return;
        event.preventDefault();
        activateOption(options.length - 1, { focus: true });
        return;
      }
      if (
        event.key === "Enter" &&
        event.target?.classList?.contains("geometry-selector-option")
      ) {
        event.preventDefault();
        tentativeId = event.target.dataset.stepId || "";
        if (isMulti) {
          if (selectedIds.has(tentativeId)) {
            selectedIds.delete(tentativeId);
          } else if (tentativeId) {
            selectedIds.add(tentativeId);
          }
        }
        updateSelectionState();
      }
    });

    document.body.appendChild(overlay);
    bundleFilter.innerHTML = bundleFilterOptions();
    render();
    search.focus();
  }

  function _renderBundleDetail(bundle, stepIds, availableSteps) {
    const count = stepIds.length;
    const isRenaming =
      app.state.geometries._bundleDrawerState.renamingBundleName ===
      bundle.name;
    const membersByGeometry = new Map(
      (bundle.members || []).map((member) => [member.geometry_id, member]),
    );
    const mappingButtonDisabled = count === 0 ? "disabled" : "";
    const slotSummary = (bundle.material_slots || []).length
      ? (bundle.material_slots || [])
          .map(
            (slot) => `
          <span class="bundle-slot-summary-chip" title="${app.commands._escAttr(slot.label || `Shared Filament ${slot.key}`)}">
            <span class="bundle-slot-summary-color" style="background:${app.commands._escAttr(app.commands._bundleSlotColor(slot.key || slot.position || 0))}"></span>
            <strong>${app.commands._escHtml(slot.key || "")}</strong>
          </span>
        `,
          )
          .join("")
      : `<span class="bundle-slot-summary-empty">No shared filament slots mapped</span>`;
    const titleBlock = isRenaming
      ? `
      <div class="bundle-rename-row">
        <input type="text" class="bundle-rename-input" id="bundleRenameInput" value="${app.commands._escAttr(bundle.name)}" />
        <button class="primary-button small" id="bundleRenameSaveBtn" data-bundle="${app.commands._escAttr(bundle.name)}">Save</button>
        <button class="ghost-button small" id="bundleRenameCancelBtn">Cancel</button>
      </div>
    `
      : `
      <div>
        <h4>${app.commands._escHtml(bundle.name)}</h4>
        <span>${count} geometr${count === 1 ? "y" : "ies"} in this bundle</span>
        ${app.commands._renderBundleMappingStatusPill(bundle)}
      </div>
      <div class="bundle-detail-actions">
        <button type="button" class="ghost-button small bundle-rename-btn" data-bundle="${app.commands._escAttr(bundle.name)}">Rename</button>
        <button type="button" class="delete-button small bundle-delete-btn" data-bundle="${app.commands._escAttr(bundle.name)}">Delete</button>
      </div>
    `;

    const members = stepIds.length
      ? stepIds
          .map((stepId) => {
            const step = app.commands.stepRecordByRef(stepId);
            const member = membersByGeometry.get(stepId);
            const label = app.commands._geometryLabelForStep(step);
            const meta = app.commands._geometryMetaLine(stepId);
            const diagramHtml = step
              ? app.commands.buildGeometryStripMiniTable(step)
              : "";
            const chipHtml = member
              ? app.commands._renderBundleMappingChips(bundle, member)
              : `<span class="bundle-role-chip is-unmapped">No mapping data</span>`;
            return `
        <div class="bundle-member-row" tabindex="0">
          <div class="bundle-member-main">
            <span class="bundle-member-name" title="${app.commands._escAttr(label)}">${app.commands._escHtml(label)}</span>
            <span class="bundle-member-meta" title="${app.commands._escAttr(meta)}">${app.commands._escHtml(meta)}</span>
          </div>
          <button type="button" class="sb-layer-remove-button bundle-remove-btn" data-bundle="${app.commands._escAttr(bundle.name)}" data-step="${app.commands._escAttr(stepId)}" title="Remove geometry" aria-label="Remove geometry">
            <svg viewBox="0 0 12 12" aria-hidden="true" focusable="false">
              <path d="M3 3 L9 9 M9 3 L3 9"></path>
            </svg>
          </button>
          ${
            diagramHtml
              ? `
            <div class="bundle-member-preview" aria-hidden="true">
              <div class="bundle-member-preview-title">${app.commands._escHtml(label)}</div>
              ${diagramHtml}
              <div class="bundle-member-preview-mapping">${chipHtml}</div>
            </div>
          `
              : ""
          }
        </div>
      `;
          })
          .join("")
      : `<div class="bundle-member-empty">This bundle has no geometries yet.</div>`;

    const addControl = availableSteps.length
      ? `
      <button type="button" class="primary-button small bundle-add-step-field" data-bundle="${app.commands._escAttr(bundle.name)}">
        Add Geometries...
      </button>
    `
      : "";
    const availabilityNotice = !availableSteps.length
      ? `<div class="bundle-member-empty">All sample geometries are already in this bundle.</div>`
      : "";

    return `
      <div class="bundle-detail-header">${titleBlock}</div>
      <div class="bundle-detail-section">
        <div class="bundle-section-cap">
          <span class="bundle-section-label">Bundle Geometries</span>
          ${addControl}
        </div>
        <div class="bundle-section-body bundle-member-section-body">
          <div class="bundle-member-list">${availabilityNotice}${members}</div>
        </div>
      </div>
      <div class="bundle-detail-section">
        <div class="bundle-section-cap">
          <span class="bundle-section-label">Bundle Filament Mapping</span>
        </div>
        <div class="bundle-section-body">
          <div class="bundle-mapping-summary">
            <div class="bundle-slot-summary">${slotSummary}</div>
            <button type="button" class="ghost-button small bundle-open-mapping-btn" data-bundle-id="${app.commands._escAttr(bundle.geometry_bundle_id || "")}" data-bundle="${app.commands._escAttr(bundle.name)}" ${mappingButtonDisabled}>
              Edit Mapping
            </button>
          </div>
        </div>
      </div>
    `;
  }

  function filamentSelectorGroups(
    filaments = app.state.session.data.filaments,
    query = "",
  ) {
    const q = query.trim().toLowerCase();
    const filtered = [...(filaments || [])].filter((fil) => {
      if (!q) return true;
      const haystack = [
        fil.manufacturer,
        fil.color_name,
        fil.display_name,
        fil.filament_id,
      ]
        .filter(Boolean)
        .join(" ")
        .toLowerCase();
      const reverseName = [fil.color_name, fil.manufacturer]
        .filter(Boolean)
        .join(" ")
        .toLowerCase();
      return haystack.includes(q) || reverseName.includes(q);
    });
    filtered.sort(
      (a, b) =>
        (a.manufacturer || "").localeCompare(b.manufacturer || "") ||
        (a.color_name || a.display_name || "").localeCompare(
          b.color_name || b.display_name || "",
        ),
    );
    return filtered.reduce((groups, fil) => {
      const manufacturer = (fil.manufacturer || "Other").trim() || "Other";
      if (!groups[manufacturer]) groups[manufacturer] = [];
      groups[manufacturer].push(fil);
      return groups;
    }, {});
  }

  function openFilamentSelector(options = {}) {
    const mode = options.mode === "multi" ? "multi" : "single";
    const selected = new Set(options.selectedIds || []);
    const overlay = document.createElement("div");
    overlay.className = "info-dialog-overlay filament-selector-overlay";
    overlay.innerHTML = `
      <div class="info-dialog filament-selector-dialog" role="dialog" aria-modal="true" aria-labelledby="filamentSelectorTitle">
        ${app.commands.renderDialogHeader({
          title: options.title || "Select Filament",
          titleId: "filamentSelectorTitle",
          headerClass: "filament-selector-header",
          closeButtonHtml: app.commands.renderWindowCloseButton({
            className: "info-dialog-close",
            label: "Close selector",
            title: "Close selector",
            attributes: "data-filament-selector-close",
          }),
        })}
        <div class="filament-selector-body">
          <input class="filament-selector-search" id="filamentSelectorSearch" type="search" placeholder="Search filaments">
          <div class="filament-selector-results" id="filamentSelectorResults"></div>
        </div>
        ${
          mode === "multi"
            ? `
          <div class="info-dialog-footer filament-selector-footer">
            <span class="filament-selector-count" id="filamentSelectorCount"></span>
            <button class="ghost-button small" type="button" id="filamentSelectorCancel">Cancel</button>
            <button class="primary-button small" type="button" id="filamentSelectorApply">Apply</button>
          </div>
        `
            : ""
        }
      </div>
    `;

    const search = overlay.querySelector("#filamentSelectorSearch");
    const results = overlay.querySelector("#filamentSelectorResults");
    const count = overlay.querySelector("#filamentSelectorCount");
    let activeIndex = 0;

    function close() {
      overlay.remove();
    }

    function applySelection() {
      try {
        if (typeof options.onApply === "function") {
          options.onApply(Array.from(selected));
        }
      } finally {
        close();
      }
    }

    function optionButtons() {
      return Array.from(results.querySelectorAll(".filament-selector-option"));
    }

    function activateOption(index, { focus = false } = {}) {
      const options = optionButtons();
      if (options.length === 0) {
        activeIndex = 0;
        return;
      }
      activeIndex = Math.max(0, Math.min(index, options.length - 1));
      options.forEach((button, i) => {
        const isActive = i === activeIndex;
        button.classList.toggle("is-active", isActive);
        button.tabIndex = isActive ? 0 : -1;
        button.setAttribute("aria-current", isActive ? "true" : "false");
      });
      if (focus) {
        options[activeIndex].focus();
        options[activeIndex].scrollIntoView({
          block: "nearest",
          inline: "nearest",
        });
      }
    }

    function gridNavigationIndex(key) {
      const options = optionButtons();
      if (options.length === 0) return activeIndex;
      if (key === "ArrowLeft")
        return (activeIndex - 1 + options.length) % options.length;
      if (key === "ArrowRight") return (activeIndex + 1) % options.length;

      const active = options[activeIndex];
      if (!active) return activeIndex;
      const activeRect = active.getBoundingClientRect();
      const activeX = activeRect.left + activeRect.width / 2;
      const activeY = activeRect.top + activeRect.height / 2;
      const movingDown = key === "ArrowDown";
      const rowEpsilon = Math.max(4, activeRect.height * 0.35);

      const candidates = options
        .map((button, index) => {
          const rect = button.getBoundingClientRect();
          const centerX = rect.left + rect.width / 2;
          const centerY = rect.top + rect.height / 2;
          return {
            index,
            dx: Math.abs(centerX - activeX),
            dy: Math.abs(centerY - activeY),
            centerY,
          };
        })
        .filter((candidate) =>
          movingDown
            ? candidate.centerY > activeY + rowEpsilon
            : candidate.centerY < activeY - rowEpsilon,
        )
        .sort((a, b) => a.dy - b.dy || a.dx - b.dx || a.index - b.index);

      if (candidates.length === 0) {
        return activeIndex;
      }

      const targetRowY = candidates[0].centerY;
      const sameRow = candidates
        .filter(
          (candidate) => Math.abs(candidate.centerY - targetRowY) <= rowEpsilon,
        )
        .sort((a, b) => a.dx - b.dx || a.index - b.index);
      return sameRow[0]?.index ?? activeIndex;
    }

    function chooseOption(id, { focusAfter = false } = {}) {
      if (!id) return;
      if (mode === "single") {
        selected.clear();
        selected.add(id);
        applySelection();
        return;
      }
      if (selected.has(id)) selected.delete(id);
      else selected.add(id);
      render();
      if (focusAfter) {
        activateOption(activeIndex, { focus: true });
      }
    }

    function render() {
      const groups = app.commands.filamentSelectorGroups(
        app.state.session.data.filaments,
        search.value || "",
      );
      const groupNames = Object.keys(groups).sort((a, b) => a.localeCompare(b));
      if (count) {
        count.textContent = `${selected.size} selected`;
      }
      if (groupNames.length === 0) {
        activeIndex = 0;
        results.innerHTML = `<div class="filament-selector-empty small-copy">No matching filaments</div>`;
        return;
      }
      let optionIndex = 0;
      results.innerHTML = groupNames
        .map(
          (manufacturer) => `
        <section class="filament-selector-group">
          <h4>${app.commands._escHtml(manufacturer)}</h4>
          <div class="filament-selector-group-grid">
            ${groups[manufacturer]
              .map((fil) => {
                const isSelected = selected.has(fil.filament_id);
                const thisIndex = optionIndex++;
                return `
                <button type="button" class="filament-selector-option${isSelected ? " is-selected" : ""}" data-filament-id="${app.commands._escAttr(fil.filament_id)}" data-option-index="${thisIndex}" tabindex="-1">
                  <span class="filament-selector-check" aria-hidden="true"></span>
                  <span class="color-chip" style="background:${app.commands._escAttr(fil.hex || "#cccccc")}"></span>
                  <span class="filament-selector-option-name">${app.commands._escHtml(fil.color_name || fil.display_name || fil.filament_id)}</span>
                </button>
              `;
              })
              .join("")}
          </div>
        </section>
      `,
        )
        .join("");
      activateOption(activeIndex);
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
      const closeBtn = event.target.closest(
        "[data-filament-selector-close], #filamentSelectorCancel",
      );
      if (closeBtn) {
        close();
        return;
      }
      const option = event.target.closest(".filament-selector-option");
      if (!option) return;
      activeIndex = Number(option.dataset.optionIndex || activeIndex);
      chooseOption(option.dataset.filamentId);
    });

    overlay
      .querySelector("#filamentSelectorApply")
      ?.addEventListener("click", applySelection);
    search.addEventListener("input", () => {
      activeIndex = 0;
      render();
    });
    search.addEventListener("keydown", (event) => {
      if (event.key !== "Tab" || event.shiftKey) return;
      const options = optionButtons();
      if (options.length === 0) return;
      event.preventDefault();
      activateOption(activeIndex, { focus: true });
    });
    overlay.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        close();
        return;
      }

      if (
        ["ArrowDown", "ArrowRight", "ArrowUp", "ArrowLeft"].includes(event.key)
      ) {
        const options = optionButtons();
        if (options.length === 0) return;
        event.preventDefault();
        activateOption(gridNavigationIndex(event.key), { focus: true });
        return;
      }

      if (event.key === "Home") {
        const options = optionButtons();
        if (options.length === 0) return;
        event.preventDefault();
        activateOption(0, { focus: true });
        return;
      }

      if (event.key === "End") {
        const options = optionButtons();
        if (options.length === 0) return;
        event.preventDefault();
        activateOption(options.length - 1, { focus: true });
        return;
      }

      if (
        event.key === "Enter" &&
        event.target?.classList?.contains("filament-selector-option")
      ) {
        event.preventDefault();
        chooseOption(event.target.dataset.filamentId, { focusAfter: true });
      }
    });

    document.body.appendChild(overlay);
    render();
    search.focus();
  }

  function showBundleIncompleteMappingConfirmDialog(bundleName = "") {
    return new Promise((resolve) => {
      const overlay = document.createElement("div");
      overlay.className = "info-dialog-overlay bundle-mapping-confirm-overlay";
      overlay.innerHTML = `
        <div class="info-dialog" role="dialog" aria-modal="true" aria-labelledby="bundleMappingIncompleteTitle">
          ${app.commands.renderDialogHeader({
            title: "Save Incomplete Mapping",
            titleId: "bundleMappingIncompleteTitle",
            closeButtonHtml: app.commands.renderWindowCloseButton({
              id: "bundleMappingIncompleteClose",
              className: "info-dialog-close",
            }),
          })}
          <div class="info-dialog-body">
            <p class="info-dialog-lede">${app.commands._escHtml(bundleName || "This bundle")} has not been fully mapped yet.</p>
            <p>Bundles must be fully mapped before they can be used to create samples. Save it anyway?</p>
          </div>
          <div class="info-dialog-footer">
            <button class="primary-button small" type="button" id="bundleMappingIncompleteProceed">Save Anyway</button>
            <button class="ghost-button small" type="button" id="bundleMappingIncompleteCancel">Cancel</button>
          </div>
        </div>
      `;
      document.body.appendChild(overlay);
      const cleanup = (value) => {
        overlay.remove();
        document.removeEventListener("keydown", onKeydown);
        resolve(value);
      };
      const onKeydown = (event) => {
        if (event.key === "Escape") cleanup(false);
      };
      document.addEventListener("keydown", onKeydown);
      overlay
        .querySelector("#bundleMappingIncompleteProceed")
        ?.addEventListener("click", () => cleanup(true));
      overlay
        .querySelector("#bundleMappingIncompleteCancel")
        ?.addEventListener("click", () => cleanup(false));
      overlay
        .querySelector("#bundleMappingIncompleteClose")
        ?.addEventListener("click", () => cleanup(false));
      overlay.addEventListener("click", (event) => {
        if (event.target === overlay) cleanup(false);
      });
    });
  }

  function _bundleMappingDraftFromDetail(bundle) {
    const savedSlots = Array.isArray(bundle?.material_slots)
      ? bundle.material_slots
      : [];
    const draftSlots = savedSlots.length
      ? savedSlots.map((slot, index) => ({
          draft_slot_id: slot.material_slot_id || `slot-${index}`,
          saved_material_slot_id: slot.material_slot_id || "",
        }))
      : [{ draft_slot_id: "draft-a" }];
    const assignments = {};
    (bundle?.members || []).forEach((member) => {
      (member.roles || []).forEach((role) => {
        if (role.material_slot_id)
          assignments[role.geometry_role_id] = role.material_slot_id;
      });
    });
    return {
      bundle,
      draftSlots,
      assignments,
      selectedDraftSlotId: null,
      dragDraftSlotId: null,
      validation: "",
      stale: false,
    };
  }

  function _bundleMappingAssignedSlotIds(state) {
    return new Set(Object.values(state.assignments || {}).filter(Boolean));
  }

  function _bundleMappingRoleCount(state) {
    return (state.bundle?.members || []).reduce(
      (total, member) => total + (member.roles || []).length,
      0,
    );
  }

  function _bundleMappingMappedRoleCount(state) {
    return (state.bundle?.members || []).reduce((total, member) => {
      return (
        total +
        (member.roles || []).filter(
          (role) => !!state.assignments[role.geometry_role_id],
        ).length
      );
    }, 0);
  }

  function _bundleMappingIsComplete(state) {
    const total = app.commands._bundleMappingRoleCount(state);
    return (
      total > 0 && app.commands._bundleMappingMappedRoleCount(state) === total
    );
  }

  function _bundleMappingSlotIndex(state, draftSlotId) {
    const index = (state.draftSlots || []).findIndex(
      (slot) => slot.draft_slot_id === draftSlotId,
    );
    return index >= 0 ? index : 0;
  }

  function _bundleMappingSlotLabel(state, draftSlotId) {
    return `Shared Filament ${app.commands._bundleSlotKey(app.commands._bundleMappingSlotIndex(state, draftSlotId))}`;
  }

  function _bundleMappingAddSlot(state) {
    state.draftSlots.push({
      draft_slot_id: `draft-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`,
    });
  }

  function _bundleMappingRemoveSlot(state, draftSlotId) {
    const assigned = app.commands._bundleMappingAssignedSlotIds(state);
    if (assigned.has(draftSlotId)) {
      app.commands.showImportToast(
        "Shared filament slot is in use. Clear its assignments before removing it.",
        "warning",
      );
      return false;
    }
    if ((state.draftSlots || []).length <= 1) {
      app.commands.showImportToast(
        "At least one shared filament slot must remain while editing.",
        "warning",
      );
      return false;
    }
    state.draftSlots = state.draftSlots.filter(
      (slot) => slot.draft_slot_id !== draftSlotId,
    );
    if (state.selectedDraftSlotId === draftSlotId)
      state.selectedDraftSlotId = null;
    return true;
  }

  async function _refreshBundleDrawerAfterMapping(bundleName) {
    app.state.geometries._bundleDrawerState.bundles =
      await app.api.fetchBundles();
    const match = (app.state.geometries._bundleDrawerState.bundles || []).find(
      (bundle) => bundle.name === bundleName,
    );
    app.state.geometries._bundleDrawerState.selectedBundleName =
      match?.name || bundleName;
    app.commands.renderBundleMgmtBody();
    app.commands.bindBundleMgmtEvents();
  }

  async function openBundleMappingDialog(bundleSummary) {
    const bundleId = bundleSummary?.geometry_bundle_id;
    if (!bundleId) {
      app.commands.showImportToast("Bundle is missing a stable id", "error");
      return;
    }

    let state;
    try {
      state = app.commands._bundleMappingDraftFromDetail(
        await app.api.fetchGeometryBundle(bundleId),
      );
    } catch (err) {
      app.commands.showImportToast(
        "Failed to load bundle mapping: " + err.message,
        "error",
      );
      return;
    }

    const overlay = document.createElement("div");
    overlay.className = "info-dialog-overlay bundle-mapping-overlay";
    document.body.appendChild(overlay);

    const close = () => {
      document.removeEventListener("keydown", onKeydown);
      overlay.remove();
    };

    const reload = async () => {
      try {
        state = app.commands._bundleMappingDraftFromDetail(
          await app.api.fetchGeometryBundle(bundleId),
        );
        app.commands.showImportToast("Reloaded bundle mapping", "success");
        render();
      } catch (err) {
        app.commands.showImportToast(
          "Failed to reload bundle mapping: " + err.message,
          "error",
        );
      }
    };

    const assignRole = (roleId, draftSlotId) => {
      if (!roleId || !draftSlotId) return;
      state.assignments[roleId] = draftSlotId;
      state.validation = "";
      state.stale = false;
      render();
    };

    const clearRole = (roleId) => {
      delete state.assignments[roleId];
      state.validation = "";
      render();
    };

    const save = async () => {
      const complete = app.commands._bundleMappingIsComplete(state);
      if (!complete) {
        const proceed =
          await app.commands.showBundleIncompleteMappingConfirmDialog(
            state.bundle?.name || bundleSummary?.name || "",
          );
        if (!proceed) return;
      }
      const payload = {
        expected_updated_at: state.bundle?.updated_at || "",
        allow_incomplete: !complete,
        draft_material_slots: state.draftSlots.map((slot) => ({
          draft_slot_id: slot.draft_slot_id,
          label: app.commands._bundleMappingSlotLabel(
            state,
            slot.draft_slot_id,
          ),
        })),
        members: (state.bundle?.members || []).map((member) => ({
          geometry_bundle_member_id: member.geometry_bundle_member_id,
          role_slot_map: (member.roles || []).map((role) => ({
            geometry_role_id: role.geometry_role_id,
            draft_slot_id: state.assignments[role.geometry_role_id] || null,
          })),
        })),
      };
      const saveBtn = overlay.querySelector("#bundleMappingSave");
      if (saveBtn) {
        saveBtn.disabled = true;
        saveBtn.textContent = "Saving...";
      }
      try {
        const saved = await app.api.saveGeometryBundleMapping(
          bundleId,
          payload,
        );
        state = app.commands._bundleMappingDraftFromDetail(saved);
        await app.commands._refreshBundleDrawerAfterMapping(
          saved.name || bundleSummary?.name || "",
        );
        app.commands.showImportToast(
          `Saved mapping for "${saved.name || bundleSummary?.name || "bundle"}"`,
          "success",
        );
        close();
      } catch (err) {
        state.validation = err.message || "Failed to save bundle mapping";
        state.stale = Number(err.status) === 409;
        render();
        app.commands.showImportToast(
          state.validation,
          state.stale ? "warning" : "error",
        );
      }
    };

    function renderSlotToken(slot, index) {
      const key = app.commands._bundleSlotKey(index);
      const label = `Shared Filament ${key}`;
      const color = app.commands._bundleSlotColor(key);
      const used = app.commands
        ._bundleMappingAssignedSlotIds(state)
        .has(slot.draft_slot_id);
      const selected = state.selectedDraftSlotId === slot.draft_slot_id;
      return `
        <div class="bundle-map-slot-row">
          <button type="button"
            class="bundle-map-token${selected ? " is-selected" : ""}"
            draggable="true"
            data-draft-slot-id="${app.commands._escAttr(slot.draft_slot_id)}"
            aria-pressed="${selected ? "true" : "false"}"
            title="${app.commands._escAttr(label)}">
            <span class="bundle-map-token-color" style="background:${app.commands._escAttr(color)}"></span>
            <span class="bundle-map-token-label">${app.commands._escHtml(label)}</span>
          </button>
          <button type="button" class="sb-layer-remove-button bundle-map-remove-slot" data-draft-slot-id="${app.commands._escAttr(slot.draft_slot_id)}" aria-label="Remove ${app.commands._escAttr(label)}" title="${used ? "Clear assignments before removing" : "Remove shared filament slot"}">
            <svg viewBox="0 0 12 12" aria-hidden="true" focusable="false">
              <path d="M3 3 L9 9 M9 3 L3 9"></path>
            </svg>
          </button>
        </div>
      `;
    }

    function renderDropZone(role) {
      const draftSlotId = state.assignments[role.geometry_role_id] || "";
      const assignedIndex = draftSlotId
        ? app.commands._bundleMappingSlotIndex(state, draftSlotId)
        : -1;
      const key =
        assignedIndex >= 0 ? app.commands._bundleSlotKey(assignedIndex) : "";
      const color =
        assignedIndex >= 0 ? app.commands._bundleSlotColor(key) : "";
      const text = color ? app.commands.textColor(color) : "";
      const roleToken = app.commands.compactLayerRoleToken(
        role.role_label,
        Number(role.role_index || 0),
      );
      const style = color
        ? ` style="--bundle-map-slot-color:${app.commands._escAttr(color)};--bundle-map-slot-text:${app.commands._escAttr(text)}"`
        : "";
      return `
        <div class="bundle-map-role-zone${draftSlotId ? " is-assigned" : ""}" data-role-id="${app.commands._escAttr(role.geometry_role_id)}" tabindex="0" role="button" aria-label="${app.commands._escAttr(app.commands.formatLayerRoleLabel(role))}"${style}>
          <span class="bundle-map-role-label">${app.commands._escHtml(roleToken)}</span>
          ${
            draftSlotId
              ? `
            <span class="bundle-map-assigned-token">
              <span class="bundle-map-token-color" style="background:${app.commands._escAttr(color)}"></span>
              <span>${app.commands._escHtml(key)}</span>
            </span>
            <button type="button" class="sb-layer-remove-button bundle-map-clear-role" data-role-id="${app.commands._escAttr(role.geometry_role_id)}" aria-label="Clear assignment" title="Clear assignment">
              <svg viewBox="0 0 12 12" aria-hidden="true" focusable="false">
                <path d="M3 3 L9 9 M9 3 L3 9"></path>
              </svg>
            </button>
          `
              : `<span class="bundle-map-unmapped">Unmapped</span>`
          }
        </div>
      `;
    }

    function renderMappedStripDiagram(step, member) {
      const variableSlots = [...(step?.swatch_slots || [])].sort(
        (a, b) => Number(a.swatch_index || 0) - Number(b.swatch_index || 0),
      );
      if (!variableSlots.length) {
        return `<div class="strip-diagram-contract-error">Missing geometry swatch slots</div>`;
      }
      const swatchCount =
        variableSlots.length ||
        Number(step?.swatch_count || step?.layout_columns || 8);
      const stepRoleByIndex = new Map(
        (step?.roles || []).map((role) => [Number(role.role_index || 0), role]),
      );
      const roles = [...(member?.roles || [])].sort(
        (a, b) => Number(b.role_index || 0) - Number(a.role_index || 0),
      );
      if (!roles.length) {
        return `<div class="strip-diagram-contract-error">Missing geometry role data</div>`;
      }
      const rows = roles.map((role) => {
        const stepRole =
          stepRoleByIndex.get(Number(role.role_index || 0)) || {};
        const roleKind = role.role_kind || stepRole.role_kind || "";
        const draftSlotId = state.assignments[role.geometry_role_id] || "";
        const assignedIndex = draftSlotId
          ? app.commands._bundleMappingSlotIndex(state, draftSlotId)
          : -1;
        const key =
          assignedIndex >= 0 ? app.commands._bundleSlotKey(assignedIndex) : "";
        const color =
          assignedIndex >= 0 ? app.commands._bundleSlotColor(key) : "#ececea";
        const text = app.commands.textColor(color);
        if (roleKind === "variable") {
          const cells = variableSlots
            .map(
              (slot) =>
                `<td style="background:${app.commands._escAttr(color)};color:${app.commands._escAttr(text)}">${Number(slot.variable_thickness_mm || 0).toFixed(2)}</td>`,
            )
            .join("");
          return `<tr>${cells}</tr>`;
        }
        const fixedThickness =
          role.fixed_thickness_mm ?? stepRole.fixed_thickness_mm;
        const value = Number(fixedThickness || 0).toFixed(2);
        return `<tr><td colspan="${swatchCount}" style="background:${app.commands._escAttr(color)};color:${app.commands._escAttr(text)}">${value}mm</td></tr>`;
      });
      return `<table class="mini-strip-table bundle-map-strip-table">${rows.join("")}</table>`;
    }

    function renderMemberCard(member) {
      const step = app.commands.stepRecordByRef(member.geometry_id);
      const label =
        member.geometry_alias ||
        app.commands._geometryLabelForStep(step) ||
        member.geometry_id;
      const roleZones = [...(member.roles || [])]
        .sort((a, b) => Number(b.role_index || 0) - Number(a.role_index || 0))
        .map(renderDropZone)
        .join("");
      return `
        <section class="bundle-map-member-card">
          <div class="bundle-map-member-head">
            <h4>${app.commands._escHtml(label)}</h4>
          </div>
          <div class="bundle-map-member-body">
            <div class="bundle-map-member-strip">${step ? renderMappedStripDiagram(step, member) : ""}</div>
            <div class="bundle-map-role-grid">
              ${roleZones}
            </div>
          </div>
        </section>
      `;
    }

    function bindDialogEvents() {
      overlay.querySelectorAll(".bundle-map-token").forEach((token) => {
        token.addEventListener("click", () => {
          const id = token.dataset.draftSlotId || "";
          state.selectedDraftSlotId =
            state.selectedDraftSlotId === id ? null : id;
          render();
        });
        token.addEventListener("dragstart", (event) => {
          state.dragDraftSlotId = token.dataset.draftSlotId || "";
          event.dataTransfer?.setData("text/plain", state.dragDraftSlotId);
          event.dataTransfer.effectAllowed = "copy";
        });
        token.addEventListener("dragend", () => {
          state.dragDraftSlotId = null;
          overlay
            .querySelectorAll(".bundle-map-role-zone.is-drag-over")
            .forEach((zone) => zone.classList.remove("is-drag-over"));
        });
      });

      overlay.querySelectorAll(".bundle-map-remove-slot").forEach((button) => {
        button.addEventListener("click", () => {
          if (
            app.commands._bundleMappingRemoveSlot(
              state,
              button.dataset.draftSlotId || "",
            )
          )
            render();
        });
      });

      overlay.querySelectorAll(".bundle-map-role-zone").forEach((zone) => {
        zone.addEventListener("dragover", (event) => {
          event.preventDefault();
          event.dataTransfer.dropEffect = "copy";
          zone.classList.add("is-drag-over");
        });
        zone.addEventListener("dragleave", () =>
          zone.classList.remove("is-drag-over"),
        );
        zone.addEventListener("drop", (event) => {
          event.preventDefault();
          zone.classList.remove("is-drag-over");
          const draftSlotId =
            event.dataTransfer?.getData("text/plain") ||
            state.dragDraftSlotId ||
            "";
          assignRole(zone.dataset.roleId || "", draftSlotId);
        });
        zone.addEventListener("click", (event) => {
          if (event.target.closest(".bundle-map-clear-role")) return;
          if (state.selectedDraftSlotId)
            assignRole(zone.dataset.roleId || "", state.selectedDraftSlotId);
        });
        zone.addEventListener("keydown", (event) => {
          if (
            (event.key === "Enter" || event.key === " ") &&
            state.selectedDraftSlotId
          ) {
            event.preventDefault();
            assignRole(zone.dataset.roleId || "", state.selectedDraftSlotId);
          }
        });
      });

      overlay.querySelectorAll(".bundle-map-clear-role").forEach((button) => {
        button.addEventListener("click", (event) => {
          event.stopPropagation();
          clearRole(button.dataset.roleId || "");
        });
      });

      overlay
        .querySelector("#bundleMappingAddSlot")
        ?.addEventListener("click", () => {
          app.commands._bundleMappingAddSlot(state);
          render();
        });
      overlay
        .querySelector("#bundleMappingSave")
        ?.addEventListener("click", save);
      overlay
        .querySelector("#bundleMappingReload")
        ?.addEventListener("click", reload);
      overlay
        .querySelector("#bundleMappingCancel")
        ?.addEventListener("click", close);
      overlay
        .querySelector("[data-bundle-mapping-close]")
        ?.addEventListener("click", close);
    }

    function render() {
      const mapped = app.commands._bundleMappingMappedRoleCount(state);
      const total = app.commands._bundleMappingRoleCount(state);
      const complete = app.commands._bundleMappingIsComplete(state);
      overlay.innerHTML = `
        <div class="info-dialog bundle-mapping-dialog" role="dialog" aria-modal="true" aria-labelledby="bundleMappingTitle">
          ${app.commands.renderDialogHeader({
            title: "Bundle Filament Mapping",
            titleId: "bundleMappingTitle",
            subtitle: `${state.bundle?.name || bundleSummary?.name || "Bundle"} · ${mapped}/${total} roles mapped`,
            headerClass: "bundle-mapping-header",
            closeButtonHtml: app.commands.renderWindowCloseButton({
              className: "info-dialog-close",
              label: "Close mapping dialog",
              title: "Close mapping dialog",
              attributes: "data-bundle-mapping-close",
            }),
          })}
          <div class="bundle-mapping-body">
            <section class="bundle-map-members-pane" aria-label="Bundle geometries">
              ${(state.bundle?.members || []).length ? (state.bundle.members || []).map(renderMemberCard).join("") : `<div class="bundle-member-empty">This bundle has no geometries yet.</div>`}
            </section>
            <aside class="bundle-map-slots-pane" aria-label="Shared filament slots">
              <div class="bundle-map-slots-head">
                <h4>Shared Filament Slots</h4>
              </div>
              <div class="bundle-map-token-list">
                ${state.draftSlots.map(renderSlotToken).join("")}
                <button type="button" class="bundle-map-add-slot-button" id="bundleMappingAddSlot">+ Add Shared Filament Slot</button>
              </div>
              <div class="bundle-map-slot-note small-copy">${state.selectedDraftSlotId ? "Click a role slot to assign the selected shared filament." : "Drag a token onto a role slot, or click a token then click a role slot."}</div>
            </aside>
          </div>
          <div class="bundle-mapping-validation${state.validation ? " is-visible" : ""}">
            ${state.validation ? app.commands._escHtml(state.validation) : ""}
          </div>
          <div class="info-dialog-footer bundle-mapping-footer">
            <span class="bundle-mapping-footer-status ${complete ? "is-complete" : "is-incomplete"}">${complete ? "Fully mapped" : "Incomplete mapping"}</span>
            ${state.stale ? `<button class="ghost-button small" type="button" id="bundleMappingReload">Reload</button>` : ""}
            <button class="ghost-button small" type="button" id="bundleMappingCancel">Cancel</button>
            <button class="primary-button small" type="button" id="bundleMappingSave">Save Mapping</button>
          </div>
        </div>
      `;
      bindDialogEvents();
    }

    function onKeydown(event) {
      if (event.key === "Escape") close();
    }

    overlay.addEventListener("click", (event) => {
      event.stopPropagation();
      if (event.target === overlay) close();
    });
    overlay.addEventListener("pointerdown", (event) => {
      event.stopPropagation();
    });
    document.addEventListener("keydown", onKeydown);
    render();
  }

  function bindBundleMgmtEvents() {}

  function _positionBundleMemberPreview(row) {
    const preview = row?.querySelector?.(".bundle-member-preview");
    if (!preview) return;
    row.classList.remove("is-preview-above");
    requestAnimationFrame(() => {
      const list = row.closest(".bundle-member-list");
      const bounds = (
        list || app.dom.bundleMgmtBody
      )?.getBoundingClientRect?.();
      if (!bounds) return;
      const rowRect = row.getBoundingClientRect();
      const previewRect = preview.getBoundingClientRect();
      const belowSpace = bounds.bottom - rowRect.bottom;
      const aboveSpace = rowRect.top - bounds.top;
      row.classList.toggle(
        "is-preview-above",
        previewRect.height + 8 > belowSpace && aboveSpace > belowSpace,
      );
    });
  }

  function bindBundleMgmtInteractions() {
    // New bundle create
    const newSaveBtn = document.getElementById("bundleNewSaveBtn");
    const newNameInput = document.getElementById("bundleNewNameInput");

    if (newSaveBtn && newNameInput) {
      const doCreate = async () => {
        const name = newNameInput.value.trim();
        if (!name) {
          app.commands.showImportToast("Bundle name cannot be empty", "error");
          return;
        }
        try {
          await app.api.createBundle(name);
          app.state.geometries._bundleDrawerState.bundles =
            await app.api.fetchBundles();
          app.state.geometries._bundleDrawerState.selectedBundleName = name;
          app.state.geometries._bundleDrawerState.renamingBundleName = null;
          app.commands.showImportToast(`Created bundle "${name}"`, "success");
          app.commands.renderBundleMgmtBody();
        } catch (err) {
          app.commands.showImportToast(
            "Failed to create bundle: " + err.message,
            "error",
          );
        }
      };
      newSaveBtn.addEventListener("click", doCreate);
      newNameInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") doCreate();
        if (e.key === "Escape") {
          newNameInput.value = "";
        }
      });
    }

    app.dom.bundleMgmtBody
      .querySelectorAll(".bundle-list-item")
      .forEach((button) => {
        button.addEventListener("click", () => {
          app.state.geometries._bundleDrawerState.selectedBundleName =
            button.dataset.bundle;
          app.state.geometries._bundleDrawerState.renamingBundleName = null;
          app.commands.renderBundleMgmtBody();
          app.commands.bindBundleMgmtEvents();
        });
      });

    app.dom.bundleMgmtBody
      .querySelectorAll(".bundle-rename-btn")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          app.state.geometries._bundleDrawerState.renamingBundleName =
            btn.dataset.bundle;
          app.commands.renderBundleMgmtBody();
          app.commands.bindBundleMgmtEvents();
          const input = document.getElementById("bundleRenameInput");
          if (input) {
            input.focus();
            input.select();
          }
        });
      });

    const renameInput = document.getElementById("bundleRenameInput");
    const renameSaveBtn = document.getElementById("bundleRenameSaveBtn");
    const renameCancelBtn = document.getElementById("bundleRenameCancelBtn");
    if (renameInput && renameSaveBtn) {
      const oldName = renameSaveBtn.dataset.bundle;
      const doRename = async () => {
        const newName = renameInput.value.trim();
        if (!newName) {
          app.commands.showImportToast("Bundle name cannot be empty", "error");
          return;
        }
        if (newName === oldName) {
          app.state.geometries._bundleDrawerState.renamingBundleName = null;
          app.commands.renderBundleMgmtBody();
          app.commands.bindBundleMgmtEvents();
          return;
        }
        try {
          await app.api.updateBundle(oldName, { new_name: newName });
          app.state.geometries._bundleDrawerState.bundles =
            await app.api.fetchBundles();
          app.state.geometries._bundleDrawerState.selectedBundleName = newName;
          app.state.geometries._bundleDrawerState.renamingBundleName = null;
          app.commands.showImportToast(
            `Renamed bundle to "${newName}"`,
            "success",
          );
          app.commands.renderBundleMgmtBody();
          app.commands.bindBundleMgmtEvents();
        } catch (err) {
          app.commands.showImportToast(
            "Failed to rename bundle: " + err.message,
            "error",
          );
        }
      };
      renameSaveBtn.addEventListener("click", doRename);
      renameInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") doRename();
        if (e.key === "Escape") {
          app.state.geometries._bundleDrawerState.renamingBundleName = null;
          app.commands.renderBundleMgmtBody();
          app.commands.bindBundleMgmtEvents();
        }
      });
    }
    if (renameCancelBtn) {
      renameCancelBtn.addEventListener("click", () => {
        app.state.geometries._bundleDrawerState.renamingBundleName = null;
        app.commands.renderBundleMgmtBody();
        app.commands.bindBundleMgmtEvents();
      });
    }

    app.dom.bundleMgmtBody
      .querySelectorAll(".bundle-remove-btn")
      .forEach((btn) => {
        btn.addEventListener("click", async (e) => {
          e.stopPropagation();
          const bundleName = btn.dataset.bundle;
          const stepId = btn.dataset.step;
          try {
            await app.api.removeStepFromBundle(bundleName, stepId);
            app.state.geometries._bundleDrawerState.bundles =
              await app.api.fetchBundles();
            app.state.geometries._bundleDrawerState.selectedBundleName =
              bundleName;
            app.commands.renderBundleMgmtBody();
            app.commands.bindBundleMgmtEvents();
          } catch (err) {
            app.commands.showImportToast(
              "Failed to remove geometry: " + err.message,
              "error",
            );
          }
        });
      });

    app.dom.bundleMgmtBody
      .querySelectorAll(".bundle-member-row")
      .forEach((row) => {
        row.addEventListener("mouseenter", () =>
          app.commands._positionBundleMemberPreview(row),
        );
        row.addEventListener("focusin", () =>
          app.commands._positionBundleMemberPreview(row),
        );
      });

    app.dom.bundleMgmtBody
      .querySelectorAll(".bundle-add-step-field")
      .forEach((field) => {
        field.addEventListener("click", () => {
          const bundleName = field.dataset.bundle;
          const selected = (
            app.state.geometries._bundleDrawerState.bundles || []
          ).find((bundle) => bundle.name === bundleName);
          const availableSteps = app.commands._availableBundleGeometries(
            selected?.step_ids || [],
          );
          app.commands.openGeometrySelector({
            title: "Add Geometries to Bundle",
            mode: "multi",
            applyLabel: "Add Selected",
            selectPanelTitle: "Select Geometries to Add",
            previewPanelTitle: "Selected Geometry Preview",
            steps: availableSteps,
            onApply: async (stepIds) => {
              const ids = Array.isArray(stepIds) ? stepIds : [];
              if (ids.length === 0) return;
              const failed = [];
              for (const stepId of ids) {
                try {
                  await app.api.addStepToBundle(bundleName, stepId);
                } catch (err) {
                  failed.push({
                    stepId,
                    message: err.message || "unknown error",
                  });
                }
              }
              try {
                app.state.geometries._bundleDrawerState.bundles =
                  await app.api.fetchBundles();
                app.state.geometries._bundleDrawerState.selectedBundleName =
                  bundleName;
                app.commands.renderBundleMgmtBody();
                app.commands.bindBundleMgmtEvents();
              } catch (err) {
                app.commands.showImportToast(
                  "Added geometries, but failed to refresh bundles: " +
                    err.message,
                  "warning",
                );
                return;
              }
              const addedCount = ids.length - failed.length;
              if (failed.length) {
                app.commands.showImportToast(
                  `Added ${addedCount}; ${failed.length} failed while updating "${bundleName}"`,
                  addedCount ? "warning" : "error",
                );
              } else {
                app.commands.showImportToast(
                  `Added ${ids.length} geometr${ids.length === 1 ? "y" : "ies"} to "${bundleName}"`,
                  "success",
                );
              }
            },
          });
        });
      });

    app.dom.bundleMgmtBody
      .querySelectorAll(".bundle-open-mapping-btn")
      .forEach((btn) => {
        btn.addEventListener("click", async () => {
          const bundleName = btn.dataset.bundle || "";
          const bundleId = btn.dataset.bundleId || "";
          const bundle = (
            app.state.geometries._bundleDrawerState.bundles || []
          ).find(
            (candidate) =>
              candidate.geometry_bundle_id === bundleId ||
              candidate.name === bundleName,
          );
          await app.commands.openBundleMappingDialog(
            bundle || { geometry_bundle_id: bundleId, name: bundleName },
          );
        });
      });

    app.dom.bundleMgmtBody
      .querySelectorAll(".bundle-delete-btn")
      .forEach((btn) => {
        const bundleName = btn.dataset.bundle;
        app.commands.bindConfirmAction(btn, {
          armedText: "Confirm delete?",
          onConfirm: async () => {
            try {
              await app.api.deleteBundle(bundleName);
              app.state.geometries._bundleDrawerState.bundles =
                await app.api.fetchBundles();
              const bundles = app.commands._sortedBundles(
                app.state.geometries._bundleDrawerState.bundles || [],
              );
              app.state.geometries._bundleDrawerState.selectedBundleName =
                bundles[0]?.name || null;
              app.state.geometries._bundleDrawerState.renamingBundleName = null;
              app.commands.showImportToast(
                `Deleted bundle "${bundleName}"`,
                "success",
              );
              app.commands.renderBundleMgmtBody();
              app.commands.bindBundleMgmtEvents();
            } catch (err) {
              app.commands.showImportToast(
                "Failed to delete bundle: " + err.message,
                "error",
              );
            }
          },
        });
      });
  }

  Object.assign(app.commands, {
    isBundleMgmtOpen,
    closeBundleMgmtDrawer,
    handleOutsideDrawerDismiss,
    openBundleManagementDrawer,
    renderBundleMgmtBody,
    _sortedBundles,
    _selectedBundle,
    _bundleSlotKey,
    _bundleSlotColor,
    _bundleStatusMeta,
    _renderBundleMappingStatusPill,
    _bundleSlotById,
    _renderBundleMappingChips,
    _buildBundleMemberPreviewDiagram,
    _geometryAliasFromRef,
    _geometryLabelForStep,
    _geometryMetaLineForStep,
    _geometryMetaLine,
    _geometryBundleNames,
    _geometrySelectorSearchText,
    _availableBundleGeometries,
    availableGeometryRecords,
    renderGeometrySelectorField,
    openGeometrySelector,
    _renderBundleDetail,
    filamentSelectorGroups,
    openFilamentSelector,
    showBundleIncompleteMappingConfirmDialog,
    _bundleMappingDraftFromDetail,
    _bundleMappingAssignedSlotIds,
    _bundleMappingRoleCount,
    _bundleMappingMappedRoleCount,
    _bundleMappingIsComplete,
    _bundleMappingSlotIndex,
    _bundleMappingSlotLabel,
    _bundleMappingAddSlot,
    _bundleMappingRemoveSlot,
    _refreshBundleDrawerAfterMapping,
    openBundleMappingDialog,
    bindBundleMgmtEvents,
    _positionBundleMemberPreview,
    bindBundleMgmtInteractions,
  });
}
