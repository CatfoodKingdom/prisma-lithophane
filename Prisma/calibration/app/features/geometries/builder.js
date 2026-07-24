/** Install features/geometries/builder commands. */
export function installFeaturesGeometriesBuilder(app) {
  function buildStepFilename() {
    const layerCount =
      1 + app.state.geometries.stepBuilderState.fixedLayers.length;
    const variablePart = `v-${app.state.geometries.stepBuilderState.values.map((value) => app.commands.formatStepNumber(value)).join("-")}`;
    const fixedThicknesses = app.commands.getStepBuilderFixedThicknesses();
    const fixedPart = fixedThicknesses.length
      ? `_f-${fixedThicknesses.map((thickness) => app.commands.formatStepNumber(thickness)).join("-")}`
      : "";
    const lhEl = app.commands._sbEl("stepLayerHeight");
    return `${layerCount}L_${variablePart}${fixedPart}_lh${app.commands.formatStepNumber(lhEl ? lhEl.value : "0.08")}.step`;
  }

  function defaultStepBuilderValues(count = 8) {
    return Array.from({ length: count }, (_, index) =>
      app.commands.formatStepNumber(0.2 + 0.08 * index),
    );
  }

  function makeStepBuilderRole(kind, values = null) {
    const role = {
      id: `role-${app.state.geometries.stepBuilderState.nextLayerRoleId++}`,
      kind,
    };
    if (kind === "variable") {
      role.values = Array.isArray(values)
        ? values.slice()
        : app.commands.defaultStepBuilderValues(
            app.commands.stepBuilderSwatchCount(),
          );
    } else {
      role.thickness = values == null ? "0.20" : String(values);
    }
    return role;
  }

  function initializeStepBuilderLayerRoles(values = null) {
    app.state.geometries.stepBuilderState.nextLayerRoleId = 1;
    app.state.geometries.stepBuilderState.layerRoles = [
      app.commands.makeStepBuilderRole(
        "variable",
        Array.isArray(values)
          ? values
          : app.commands.defaultStepBuilderValues(8),
      ),
    ];
    app.commands.syncStepBuilderLegacyLayerState();
  }

  function stepBuilderVariableRole() {
    let variable = app.state.geometries.stepBuilderState.layerRoles.find(
      (role) => role.kind === "variable",
    );
    if (!variable) {
      variable = app.commands.makeStepBuilderRole(
        "variable",
        app.state.geometries.stepBuilderState.values,
      );
      app.state.geometries.stepBuilderState.layerRoles.unshift(variable);
    }
    return variable;
  }

  function syncStepBuilderLegacyLayerState() {
    const variable = app.commands.stepBuilderVariableRole();
    app.state.geometries.stepBuilderState.values = variable.values || [];
    app.state.geometries.stepBuilderState.fixedLayers =
      app.state.geometries.stepBuilderState.layerRoles
        .filter((role) => role.kind === "fixed")
        .slice()
        .reverse()
        .map((role) => ({ thickness: role.thickness, roleId: role.id }));
  }

  function getStepBuilderFixedThicknesses() {
    if (
      app.commands.isStructuredGeometryBackend() &&
      app.state.geometries.stepBuilderState.layerRoles.length
    ) {
      app.commands.syncStepBuilderLegacyLayerState();
    }
    return app.state.geometries.stepBuilderState.fixedLayers.map(
      (layer) => layer.thickness,
    );
  }

  function isStructuredGeometryBackend() {
    return (
      (app.state.session._serverConfig?.backend || "").toLowerCase() ===
      "sqlite"
    );
  }

  function stepBuilderSwatchCount() {
    const countEl = app.commands._sbEl("stepColumnCount");
    const raw = countEl
      ? Number.parseInt(countEl.value, 10)
      : app.state.geometries.stepBuilderState.values.length;
    return Math.max(
      1,
      Math.min(
        48,
        Number.isFinite(raw)
          ? raw
          : app.state.geometries.stepBuilderState.values.length || 8,
      ),
    );
  }

  function updateStepBuilderDrawerWidth() {
    if (!app.dom.stepBuilderDrawer) return;
    if (!app.commands.isStructuredGeometryBackend()) {
      app.dom.stepBuilderDrawer.style.removeProperty("--step-builder-width");
      app.dom.stepBuilderDrawer.style.removeProperty(
        "--step-builder-form-width",
      );
      return;
    }
    const count = app.commands.stepBuilderSwatchCount();
    const swatchGridWidth = count * 35 + Math.max(0, count - 1);
    const roleChromeWidth = 72 + 4 + 4 + 22 + 32;
    const formWidth = Math.max(
      520,
      Math.min(900, swatchGridWidth + roleChromeWidth),
    );
    const visualWidth = 408;
    const columnGap = 8;
    const desired = Math.min(1320, formWidth + visualWidth + columnGap);
    app.dom.stepBuilderDrawer.style.setProperty(
      "--step-builder-form-width",
      `${formWidth}px`,
    );
    app.dom.stepBuilderDrawer.style.setProperty(
      "--step-builder-width",
      `${desired}px`,
    );
  }

  function resizeStepBuilderValues(count) {
    const normalized = Math.max(
      1,
      Math.min(48, Number.parseInt(count, 10) || 8),
    );
    const variable =
      app.commands.isStructuredGeometryBackend() &&
      app.state.geometries.stepBuilderState.layerRoles.length
        ? app.commands.stepBuilderVariableRole()
        : null;
    const source = variable
      ? variable.values || []
      : app.state.geometries.stepBuilderState.values;
    const current = source.slice(0, normalized);
    while (current.length < normalized) {
      const previous = current.length
        ? app.commands.numericValue(current[current.length - 1], 0)
        : 0.2;
      current.push(app.commands.formatStepNumber(previous + 0.08));
    }
    if (variable) {
      variable.values = current;
      app.commands.syncStepBuilderLegacyLayerState();
    } else {
      app.state.geometries.stepBuilderState.values = current;
    }
  }

  function stepBuilderStackHeightMax() {
    const fixedSum = app.commands
      .getStepBuilderFixedThicknesses()
      .map((value) => app.commands.numericValue(value, 0))
      .reduce((sum, value) => sum + value, 0);
    const variableMax = Math.max(
      ...app.state.geometries.stepBuilderState.values.map((value) =>
        app.commands.numericValue(value, 0),
      ),
    );
    return fixedSum + variableMax;
  }

  function defaultSpineTotalThickness() {
    return app.commands.formatStepNumber(
      app.commands.stepBuilderStackHeightMax() + 0.08,
    );
  }

  function structuredGeometryPayloadFromBuilder() {
    const rows = 1;
    const columns = app.commands.stepBuilderSwatchCount();
    const aliasEl = app.commands._sbEl("stepBuilderAlias");
    const widthEl = app.commands._sbEl("stepSwatchWidth");
    const heightEl = app.commands._sbEl("stepSwatchHeight");
    const spineWidthEl = app.commands._sbEl("stepSpineWidth");
    const spineTotalEl = app.commands._sbEl("stepSpineTotalThickness");
    app.commands.syncStepBuilderLegacyLayerState();
    const orderedBottomToTop = app.state.geometries.stepBuilderState.layerRoles
      .slice()
      .reverse();
    const roles = orderedBottomToTop.map((role, index) => ({
      role_index: index + 1,
      role_label: `LR_${String(index + 1).padStart(2, "0")}`,
      role_kind: role.kind,
      fixed_thickness_mm:
        role.kind === "fixed"
          ? app.commands.numericValue(role.thickness, NaN)
          : null,
    }));
    const variableThicknesses = app.commands
      .stepBuilderVariableRole()
      .values.map((thickness) => app.commands.numericValue(thickness, NaN));
    return {
      alias: aliasEl ? aliasEl.value.trim() : "",
      layout_rows: rows,
      layout_columns: columns,
      swatch_width_mm: app.commands.numericValue(
        widthEl ? widthEl.value : "12.00",
        NaN,
      ),
      swatch_height_mm: app.commands.numericValue(
        heightEl ? heightEl.value : "20.00",
        NaN,
      ),
      spine_width_mm: app.commands.numericValue(
        spineWidthEl ? spineWidthEl.value : "3.00",
        NaN,
      ),
      spine_total_thickness_mm: app.commands.numericValue(
        spineTotalEl
          ? spineTotalEl.value
          : app.commands.defaultSpineTotalThickness(),
        NaN,
      ),
      roles,
      swatch_slots: variableThicknesses.map((thickness, index) => ({
        swatch_index: index,
        row_index: Math.floor(index / columns),
        column_index: index % columns,
        variable_thickness_mm: thickness,
      })),
    };
  }

  function projectGeometryVisualDraft(payload, viewportSpec = {}) {
    const unavailable = (reason) => ({ available: false, reason });
    const viewport =
      viewportSpec && typeof viewportSpec === "object" ? viewportSpec : {};
    const isFiniteNumber = (value) =>
      typeof value === "number" && Number.isFinite(value);
    const isPositiveNumber = (value) => isFiniteNumber(value) && value > 0;
    const isPositiveInteger = (value) => Number.isInteger(value) && value > 0;
    const readViewportValue = (key, fallback) => {
      const value = viewport[key];
      return value === undefined ? fallback : value;
    };

    if (!payload || typeof payload !== "object")
      return unavailable("missing-payload");

    const rows = payload.layout_rows;
    const columns = payload.layout_columns;
    const swatchWidth = payload.swatch_width_mm;
    const swatchHeight = payload.swatch_height_mm;
    const spineWidth = payload.spine_width_mm;
    const spineTotal = payload.spine_total_thickness_mm;
    if (rows !== 1) return unavailable("unsupported-row-count");
    if (!isPositiveInteger(columns)) return unavailable("invalid-column-count");
    if (
      ![swatchWidth, swatchHeight, spineWidth, spineTotal].every(
        isPositiveNumber,
      )
    ) {
      return unavailable("invalid-dimensions");
    }

    const roles = Array.isArray(payload.roles) ? payload.roles.slice() : [];
    if (roles.length === 0) return unavailable("missing-roles");
    if (roles.some((role) => !role || typeof role !== "object")) {
      return unavailable("invalid-role");
    }
    roles.sort((a, b) => a.role_index - b.role_index);
    const expectedRoleIndexes = roles.map((_, index) => index + 1);
    if (
      roles.some(
        (role, index) =>
          !role || role.role_index !== expectedRoleIndexes[index],
      )
    ) {
      return unavailable("invalid-role-order");
    }
    if (roles.filter((role) => role.role_kind === "variable").length !== 1) {
      return unavailable("invalid-variable-role-count");
    }
    for (const role of roles) {
      if (role.role_kind === "fixed") {
        if (!isPositiveNumber(role.fixed_thickness_mm))
          return unavailable("invalid-fixed-thickness");
      } else if (role.role_kind === "variable") {
        if (
          role.fixed_thickness_mm !== null &&
          role.fixed_thickness_mm !== undefined
        ) {
          return unavailable("variable-role-has-fixed-thickness");
        }
      } else {
        return unavailable("invalid-role-kind");
      }
    }

    const slots = Array.isArray(payload.swatch_slots)
      ? payload.swatch_slots.slice()
      : [];
    if (slots.length !== rows * columns)
      return unavailable("invalid-slot-count");
    if (slots.some((slot) => !slot || typeof slot !== "object")) {
      return unavailable("invalid-swatch");
    }
    slots.sort((a, b) => a.swatch_index - b.swatch_index);
    const occupiedPositions = new Set();
    for (let index = 0; index < slots.length; index += 1) {
      const slot = slots[index];
      if (!slot || slot.swatch_index !== index)
        return unavailable("invalid-swatch-order");
      if (!Number.isInteger(slot.row_index) || slot.row_index !== 0) {
        return unavailable("invalid-swatch-row");
      }
      if (
        !Number.isInteger(slot.column_index) ||
        slot.column_index < 0 ||
        slot.column_index >= columns
      ) {
        return unavailable("invalid-swatch-column");
      }
      const positionKey = `${slot.row_index}:${slot.column_index}`;
      if (occupiedPositions.has(positionKey))
        return unavailable("duplicate-swatch-position");
      occupiedPositions.add(positionKey);
      if (
        !isFiniteNumber(slot.variable_thickness_mm) ||
        slot.variable_thickness_mm < 0
      ) {
        return unavailable("invalid-variable-thickness");
      }
    }

    const topWidth = readViewportValue("topWidth", 520);
    const topHeight = readViewportValue("topHeight", 220);
    const topPaddingX = readViewportValue("topPaddingX", 42);
    const topPaddingTop = readViewportValue("topPaddingTop", 28);
    const topPaddingBottom = readViewportValue("topPaddingBottom", 34);
    const sideWidth = readViewportValue("sideWidth", topWidth);
    const sideHeight = readViewportValue("sideHeight", 160);
    const sidePaddingTop = readViewportValue("sidePaddingTop", 18);
    const sidePaddingBottom = readViewportValue("sidePaddingBottom", 22);
    const viewportValues = [
      topWidth,
      topHeight,
      topPaddingX,
      topPaddingTop,
      topPaddingBottom,
      sideWidth,
      sideHeight,
      sidePaddingTop,
      sidePaddingBottom,
    ];
    if (!viewportValues.every((value) => isFiniteNumber(value) && value >= 0)) {
      return unavailable("invalid-viewport");
    }
    if (
      topWidth <= 2 * topPaddingX ||
      topHeight <= topPaddingTop + topPaddingBottom ||
      sideWidth !== topWidth ||
      sideHeight <= sidePaddingTop + sidePaddingBottom
    ) {
      return unavailable("invalid-viewport-bounds");
    }

    const footprintWidth = columns * swatchWidth + 2 * spineWidth;
    const footprintHeight = rows * swatchHeight + spineWidth;
    const topAvailableWidth = topWidth - 2 * topPaddingX;
    const topAvailableHeight = topHeight - topPaddingTop - topPaddingBottom;
    const sharedXScale = Math.min(
      topAvailableWidth / footprintWidth,
      topAvailableHeight / footprintHeight,
    );
    if (!isPositiveNumber(sharedXScale))
      return unavailable("invalid-top-scale");

    const topDrawWidth = footprintWidth * sharedXScale;
    const topDrawHeight = footprintHeight * sharedXScale;
    const sharedXOrigin = topPaddingX + (topAvailableWidth - topDrawWidth) / 2;
    const topYOrigin = topPaddingTop + (topAvailableHeight - topDrawHeight) / 2;
    const topRect = (xMm, yMm, widthMm, heightMm) => ({
      x: sharedXOrigin + xMm * sharedXScale,
      y: topYOrigin + (footprintHeight - yMm - heightMm) * sharedXScale,
      width: widthMm * sharedXScale,
      height: heightMm * sharedXScale,
      xMm,
      yMm,
      widthMm,
      heightMm,
    });

    const topSpines = [
      { part: "left", ...topRect(0, 0, spineWidth, footprintHeight) },
      {
        part: "right",
        ...topRect(
          spineWidth + columns * swatchWidth,
          0,
          spineWidth,
          footprintHeight,
        ),
      },
      {
        part: "top",
        ...topRect(
          spineWidth,
          rows * swatchHeight,
          columns * swatchWidth,
          spineWidth,
        ),
      },
    ];
    const topSwatches = slots.map((slot) => ({
      swatchIndex: slot.swatch_index,
      rowIndex: slot.row_index,
      columnIndex: slot.column_index,
      ...topRect(
        spineWidth + slot.column_index * swatchWidth,
        slot.row_index * swatchHeight,
        swatchWidth,
        swatchHeight,
      ),
    }));

    const sideStacks = slots.map((slot) => {
      let z = 0;
      const layers = roles.map((role) => {
        const thickness =
          role.role_kind === "fixed"
            ? role.fixed_thickness_mm
            : slot.variable_thickness_mm;
        const zMin = z;
        const zMax = zMin + thickness;
        z = zMax;
        return {
          roleIndex: role.role_index,
          roleKind: role.role_kind,
          thicknessMm: thickness,
          zMinMm: zMin,
          zMaxMm: zMax,
        };
      });
      return {
        swatchIndex: slot.swatch_index,
        columnIndex: slot.column_index,
        xMm: spineWidth + slot.column_index * swatchWidth,
        widthMm: swatchWidth,
        stackHeightMm: z,
        layers,
      };
    });
    const maxStackHeight = Math.max(
      ...sideStacks.map((stack) => stack.stackHeightMm),
    );
    if (spineTotal + 1e-9 < maxStackHeight)
      return unavailable("spine-shorter-than-stack");

    const sideAvailableHeight = sideHeight - sidePaddingTop - sidePaddingBottom;
    const sideZScale =
      sideAvailableHeight / Math.max(spineTotal, maxStackHeight);
    if (!isPositiveNumber(sideZScale)) return unavailable("invalid-side-scale");
    const sideBaseline = sideHeight - sidePaddingBottom;
    const sideRect = (xMm, zMinMm, widthMm, heightMm) => ({
      x: sharedXOrigin + xMm * sharedXScale,
      y: sideBaseline - (zMinMm + heightMm) * sideZScale,
      width: widthMm * sharedXScale,
      height: heightMm * sideZScale,
      xMm,
      zMinMm,
      widthMm,
      heightMm,
    });

    sideStacks.forEach((stack) => {
      stack.x = sharedXOrigin + stack.xMm * sharedXScale;
      stack.width = stack.widthMm * sharedXScale;
      stack.layers.forEach((layer) => {
        Object.assign(
          layer,
          sideRect(stack.xMm, layer.zMinMm, stack.widthMm, layer.thicknessMm),
        );
      });
    });
    const sideSpines = [
      { part: "left", ...sideRect(0, 0, spineWidth, spineTotal) },
      {
        part: "right",
        ...sideRect(
          spineWidth + columns * swatchWidth,
          0,
          spineWidth,
          spineTotal,
        ),
      },
    ];

    return {
      available: true,
      footprint: {
        widthMm: footprintWidth,
        heightMm: footprintHeight,
        swatchWidthMm: swatchWidth,
        swatchHeightMm: swatchHeight,
        spineWidthMm: spineWidth,
        spineTotalMm: spineTotal,
      },
      scales: {
        sharedX: sharedXScale,
        sideZ: sideZScale,
      },
      top: {
        width: topWidth,
        height: topHeight,
        xOrigin: sharedXOrigin,
        yOrigin: topYOrigin,
        drawWidth: topDrawWidth,
        drawHeight: topDrawHeight,
        spines: topSpines,
        swatches: topSwatches,
      },
      side: {
        width: sideWidth,
        height: sideHeight,
        xOrigin: sharedXOrigin,
        baseline: sideBaseline,
        maxStackHeightMm: maxStackHeight,
        spines: sideSpines,
        stacks: sideStacks,
      },
    };
  }

  function geometryVisualSvgNumber(value) {
    return Number(value).toFixed(3);
  }

  function buildGeometryVisualTopSvg(projected) {
    const rect = (item, className) => `
      <rect class="${className}"
        x="${app.commands.geometryVisualSvgNumber(item.x)}" y="${app.commands.geometryVisualSvgNumber(item.y)}"
        width="${app.commands.geometryVisualSvgNumber(item.width)}" height="${app.commands.geometryVisualSvgNumber(item.height)}"
        vector-effect="non-scaling-stroke"></rect>`;
    return `
      <svg class="geometry-visual-svg geometry-visual-top-svg"
        viewBox="0 0 ${projected.top.width} ${projected.top.height}"
        role="img" aria-label="Top view of the calibration strip footprint"
        preserveAspectRatio="xMidYMid meet">
        <g class="geometry-visual-top-swatches">
          ${projected.top.swatches.map((swatch) => rect(swatch, "geometry-visual-swatch")).join("")}
        </g>
        <g class="geometry-visual-spines">
          ${projected.top.spines.map((spine) => rect(spine, "geometry-visual-spine")).join("")}
        </g>
      </svg>`;
  }

  function buildGeometryVisualSideSvg(projected) {
    const rect = (item, className, attributes = "") => `
      <rect class="${className}" ${attributes}
        x="${app.commands.geometryVisualSvgNumber(item.x)}" y="${app.commands.geometryVisualSvgNumber(item.y)}"
        width="${app.commands.geometryVisualSvgNumber(item.width)}" height="${app.commands.geometryVisualSvgNumber(item.height)}"
        vector-effect="non-scaling-stroke"></rect>`;
    const layerRects = projected.side.stacks
      .flatMap((stack) => stack.layers)
      .filter((layer) => layer.height > 0)
      .map((layer) => {
        const classes = [
          "geometry-visual-layer",
          layer.roleKind === "variable" ? "is-variable" : "is-fixed",
          `role-tone-${(layer.roleIndex - 1) % 3}`,
        ].join(" ");
        return rect(layer, classes, `data-role-index="${layer.roleIndex}"`);
      })
      .join("");
    return `
      <svg class="geometry-visual-svg geometry-visual-side-svg"
        viewBox="0 0 ${projected.side.width} ${projected.side.height}"
        role="img" aria-label="Side view of the calibration strip layer profile"
        preserveAspectRatio="xMidYMid meet">
        <g class="geometry-visual-side-layers">${layerRects}</g>
        <g class="geometry-visual-spines">
          ${projected.side.spines.map((spine) => rect(spine, "geometry-visual-spine")).join("")}
        </g>
      </svg>`;
  }

  function renderStepGeometryVisualPreview(payload) {
    const root = app.commands._sbEl("stepGeometryVisualPreview");
    if (!root) return;
    const topSurface = app.commands._sbEl("stepGeometryTopView");
    const sideSurface = app.commands._sbEl("stepGeometrySideView");
    const footprintLabel = app.commands._sbEl("stepGeometryFootprintLabel");
    const projected = app.commands.projectGeometryVisualDraft(payload);
    root.dataset.previewState = projected.available
      ? "available"
      : "unavailable";

    if (!projected.available) {
      const unavailableHtml = `<div class="geometry-visual-unavailable">Complete valid strip dimensions to see this view.</div>`;
      if (topSurface) topSurface.innerHTML = unavailableHtml;
      if (sideSurface) sideSurface.innerHTML = unavailableHtml;
      if (footprintLabel) footprintLabel.textContent = "";
      return;
    }

    if (topSurface)
      topSurface.innerHTML = app.commands.buildGeometryVisualTopSvg(projected);
    if (sideSurface)
      sideSurface.innerHTML =
        app.commands.buildGeometryVisualSideSvg(projected);
    if (footprintLabel) {
      footprintLabel.textContent = `Overall footprint: ${app.commands.formatStepNumber(projected.footprint.widthMm)} × ${app.commands.formatStepNumber(projected.footprint.heightMm)} mm`;
    }
  }

  function markStepBuilderInvalid(input) {
    if (!input) return;
    input.classList.add("is-invalid");
  }

  function clearStepBuilderValidationHighlights() {
    if (!app.dom.stepBuilderBody) return;
    app.dom.stepBuilderBody
      .querySelectorAll(".is-invalid")
      .forEach((node) => node.classList.remove("is-invalid"));
  }

  function parentPathFromExportPath(path) {
    const raw = String(path || "");
    const slashIndex = raw.lastIndexOf("/");
    const backslashIndex = raw.lastIndexOf("\\");
    const index = Math.max(slashIndex, backslashIndex);
    return index > 0 ? raw.slice(0, index) : raw;
  }

  function geometryExportDestinationLabel(paths = []) {
    const exportPaths = Array.isArray(paths)
      ? paths.filter((path) => !!path)
      : [];
    if (exportPaths.length === 0) return "";
    if (exportPaths.length === 1) return exportPaths[0];
    const folders = [
      ...new Set(
        exportPaths.map((path) => app.commands.parentPathFromExportPath(path)),
      ),
    ].filter((path) => !!path);
    if (folders.length === 1) return folders[0];
    return `${folders[0]} (+${folders.length - 1} more)`;
  }

  function geometryExportToastMessage(exportName, manifest) {
    const destinations =
      manifest?.export_destinations || manifest?.export_paths || [];
    const exportedCount = (manifest?.export_paths || destinations).length;
    const fileWord = exportedCount === 1 ? "file" : "files";
    const destination =
      app.commands.geometryExportDestinationLabel(destinations);
    const countText = exportedCount ? ` (${exportedCount} ${fileWord})` : "";
    return destination
      ? `Exported ${exportName}${countText} to ${destination}`
      : `Exported ${exportName}${countText}`;
  }

  function geometryExportConflictDetail(err) {
    const detail = err?.detail;
    if (err?.status === 409 && detail?.requires_overwrite) return detail;
    return null;
  }

  function showGeometryOverwriteConfirmDialog(detail, exportName) {
    return new Promise((resolve) => {
      const conflicts = Array.isArray(detail?.conflicts)
        ? detail.conflicts.filter((path) => !!path)
        : [];
      const rows = conflicts.slice(0, 6);
      const extraCount = Math.max(0, conflicts.length - rows.length);
      const overlay = document.createElement("div");
      overlay.className = "info-dialog-overlay";
      overlay.innerHTML = `
        <div class="info-dialog info-dialog-wide" role="dialog" aria-modal="true" aria-labelledby="geometryOverwriteTitle">
          ${app.commands.renderDialogHeader({
            title: "Replace Existing Export?",
            titleId: "geometryOverwriteTitle",
            closeButtonHtml: app.commands.renderWindowCloseButton({
              id: "geometryOverwriteClose",
              className: "info-dialog-close",
            }),
          })}
          <div class="info-dialog-body">
            <p class="info-dialog-lede">An export named <strong>${app.commands.escapeHtml(exportName)}</strong> already exists in the public output folder.</p>
            <p class="small-copy">Continuing will overwrite the existing file or STL folder contents.</p>
            ${
              rows.length
                ? `
              <div class="artifact-path-list geometry-conflict-list">
                ${rows
                  .map(
                    (path) => `
                  <div class="artifact-path-row geometry-conflict-row">
                    <span class="artifact-kind">Path</span>
                    <span class="mono muted-line drawer-break-all" title="${app.commands.escapeHtml(path)}">${app.commands.escapeHtml(app.commands.displayPathFromPrismaRoot(path))}</span>
                  </div>
                `,
                  )
                  .join("")}
                ${extraCount ? `<span class="small-copy">+${extraCount} more</span>` : ""}
              </div>
            `
                : ""
            }
          </div>
          <div class="info-dialog-footer">
            <button class="delete-button small" id="geometryOverwriteConfirm">Overwrite</button>
            <button class="ghost-button small" id="geometryOverwriteCancel">Cancel</button>
          </div>
        </div>
      `;
      document.body.appendChild(overlay);
      const cleanup = (value) => {
        overlay.remove();
        document.removeEventListener("keydown", handleKeydown);
        resolve(value);
      };
      const handleKeydown = (event) => {
        if (event.key === "Escape") cleanup(false);
      };
      document.addEventListener("keydown", handleKeydown);
      overlay
        .querySelector("#geometryOverwriteConfirm")
        ?.addEventListener("click", () => cleanup(true));
      overlay
        .querySelector("#geometryOverwriteCancel")
        ?.addEventListener("click", () => cleanup(false));
      overlay
        .querySelector("#geometryOverwriteClose")
        ?.addEventListener("click", () => cleanup(false));
      overlay.addEventListener("click", (event) => {
        if (event.target === overlay) cleanup(false);
      });
    });
  }

  function openGeometryExportDialog(geometryId, alias = "") {
    if (app.state.geometries.activeGeometryExportDialogCleanup?.() === false)
      return;
    document
      .querySelectorAll(".geometry-export-dialog")
      .forEach((existing) => existing.remove());
    const overlay = document.createElement("div");
    overlay.className = "info-dialog-overlay geometry-export-dialog";
    overlay.innerHTML = `
      <div class="info-dialog" role="dialog" aria-modal="true" aria-labelledby="geometryExportTitle">
        ${app.commands.renderDialogHeader({
          title: "Export Sample Geometry",
          titleId: "geometryExportTitle",
          closeButtonHtml: app.commands.renderWindowCloseButton({
            id: "geometryExportClose",
            className: "info-dialog-close",
            label: "Close export dialog",
            title: "Close export dialog",
          }),
        })}
        <div class="info-dialog-body geometry-export-body">
          <label class="geometry-export-name">
            <span class="field-label">File name</span>
            <input type="text" id="geometryExportName" value="${app.commands.escapeHtml(alias || geometryId)}" />
          </label>
          <div class="sb-validation-error" id="geometryExportError"></div>
        </div>
        <div class="info-dialog-footer geometry-export-footer">
          <button class="ghost-button small" id="geometryExportCancelBtn">Cancel</button>
          <div class="geometry-export-action-group" role="group" aria-label="Export format">
            <button class="primary-button small" id="geometryExportStepBtn">Export STEP</button>
            <button class="primary-button small" id="geometryExportStlBtn">Export STLs</button>
          </div>
        </div>
      </div>
    `;
    document.body.appendChild(overlay);
    const nameInput = overlay.querySelector("#geometryExportName");
    const errorEl = overlay.querySelector("#geometryExportError");
    const setBusy = (nextBusy) => {
      dialogGuard.setBusy(nextBusy);
      if (!overlay.isConnected) return;
      overlay.querySelectorAll("button, input").forEach((control) => {
        control.disabled = dialogGuard.busy;
      });
    };
    const onKeyDown = (event) => {
      if (event.key === "Escape") cleanup();
    };
    const dialogGuard = app.commands.createBusyDialogGuard({
      element: overlay,
      onClose: () => {
        document.removeEventListener("keydown", onKeyDown);
        if (
          app.state.geometries.activeGeometryExportDialogCleanup === cleanup
        ) {
          app.state.geometries.activeGeometryExportDialogCleanup = null;
        }
      },
    });
    const cleanup = (options = {}) => dialogGuard.close(options);
    app.state.geometries.activeGeometryExportDialogCleanup = cleanup;
    const doExport = async ({ includeStep, includeStls }) => {
      const exportName = (nameInput?.value || "").trim();
      nameInput?.classList.remove("is-invalid");
      if (!exportName) {
        if (errorEl) {
          errorEl.style.display = "block";
          errorEl.textContent = "File name is required";
        }
        app.commands.markStepBuilderInvalid(nameInput);
        app.commands.showImportToast("File name is required", "error");
        return;
      }
      const exportOptions = {
        export_name: exportName,
        include_step: includeStep,
        include_stls: includeStls,
      };
      const submitExport = async (overwrite = false) => {
        try {
          return await app.api.generateGeometryArtifacts(geometryId, {
            ...exportOptions,
            overwrite,
          });
        } catch (err) {
          const conflict = !overwrite
            ? app.commands.geometryExportConflictDetail(err)
            : null;
          if (!conflict) throw err;
          const confirmed =
            await app.commands.showGeometryOverwriteConfirmDialog(
              conflict,
              exportName,
            );
          if (!confirmed) return null;
          return app.api.generateGeometryArtifacts(geometryId, {
            ...exportOptions,
            overwrite: true,
          });
        }
      };
      setBusy(true);
      try {
        const manifest = await submitExport(false);
        if (!manifest) return;
        app.commands.showImportToast(
          app.commands.geometryExportToastMessage(exportName, manifest),
          "success",
          { durationMs: 6500 },
        );
        await app.commands.handleRefresh();
        cleanup({ force: true });
      } catch (err) {
        const msg = err.message || "Export failed";
        if (errorEl) {
          errorEl.style.display = "block";
          errorEl.textContent = msg;
        }
        app.commands.showImportToast(msg, "error");
      } finally {
        setBusy(false);
      }
    };
    overlay
      .querySelector("#geometryExportClose")
      ?.addEventListener("click", cleanup);
    overlay
      .querySelector("#geometryExportCancelBtn")
      ?.addEventListener("click", cleanup);
    overlay.addEventListener("click", (event) => {
      if (event.target === overlay) cleanup();
    });
    overlay
      .querySelector("#geometryExportStepBtn")
      ?.addEventListener("click", () =>
        doExport({ includeStep: true, includeStls: false }),
      );
    overlay
      .querySelector("#geometryExportStlBtn")
      ?.addEventListener("click", () =>
        doExport({ includeStep: false, includeStls: true }),
      );
    document.addEventListener("keydown", onKeyDown);
    nameInput?.focus();
    nameInput?.select();
  }

  function populateStepValues() {
    const startEl = app.commands._sbEl("stepStartValue");
    const incEl = app.commands._sbEl("stepIncrementValue");
    const count = app.commands.stepBuilderSwatchCount();
    const start = app.commands.numericValue(
      startEl ? startEl.value : "0.20",
      0.2,
    );
    const increment = app.commands.numericValue(
      incEl ? incEl.value : "0.08",
      0.08,
    );
    const values = Array.from({ length: count }, (_, index) => {
      return app.commands.formatStepNumber(start + increment * index);
    });
    if (
      app.commands.isStructuredGeometryBackend() &&
      app.state.geometries.stepBuilderState.layerRoles.length
    ) {
      app.commands.stepBuilderVariableRole().values = values;
      app.commands.syncStepBuilderLegacyLayerState();
    } else {
      app.state.geometries.stepBuilderState.values = values;
    }
  }

  function updateStepPreview() {
    const previewArea = app.commands._sbEl("stepPreviewArea");
    const filenameEl = app.commands._sbEl("stepFilenamePreview");
    if (!previewArea) return;
    if (
      app.commands.isStructuredGeometryBackend() &&
      app.state.geometries.stepBuilderState.layerRoles.length
    ) {
      app.commands.syncStepBuilderLegacyLayerState();
    }
    const structuredPayload = app.commands.isStructuredGeometryBackend()
      ? app.commands.structuredGeometryPayloadFromBuilder()
      : null;
    const incEl = app.commands._sbEl("stepIncrementValue");
    const lhEl = app.commands._sbEl("stepLayerHeight");
    const widthEl = app.commands._sbEl("stepSwatchWidth");
    const heightEl = app.commands._sbEl("stepSwatchHeight");
    const spineWidthEl = app.commands._sbEl("stepSpineWidth");
    const spineTotalEl = app.commands._sbEl("stepSpineTotalThickness");
    const previewFixed = app.commands.buildFixedLayerPreviewValues(
      app.state.geometries.stepBuilderState.fixedLayers.map((layer) => ({
        thickness_mm: app.commands.numericValue(layer.thickness, 0),
      })),
    );
    const expLike = {
      variable_hex: "#cccccc",
      variable_thicknesses_mm: app.state.geometries.stepBuilderState.values.map(
        (v) => app.commands.numericValue(v, 0),
      ),
      fixed_thicknesses_mm: previewFixed.thicknesses,
      fixed_filament_ids: [],
      roles: structuredPayload ? structuredPayload.roles : [],
    };
    const uniformIncrement = app.commands.fixedSwatchIncrement(
      app.state.geometries.stepBuilderState.values,
    );
    previewArea.innerHTML = `
      ${app.commands.buildStripMiniTable(expLike)}
      <div class="sb-summary">
        <span class="sb-summary-line">${app.commands.isStructuredGeometryBackend() ? `${app.commands.stepBuilderSwatchCount()} swatches · ${app.commands.formatStepNumber(widthEl ? widthEl.value : "12.00")} × ${app.commands.formatStepNumber(heightEl ? heightEl.value : "20.00")} mm` : `${app.commands.formatStepNumber(lhEl ? lhEl.value : "0.08")} mm layer height`}</span>
        ${app.commands.isStructuredGeometryBackend() ? `<span class="sb-summary-line">${app.commands.formatStepNumber(spineWidthEl ? spineWidthEl.value : "3.00")} mm spine · ${app.commands.formatStepNumber(spineTotalEl ? spineTotalEl.value : app.commands.defaultSpineTotalThickness())} mm total spine height</span>` : ""}
        ${uniformIncrement === null ? "" : `<span class="sb-summary-line">${app.commands.formatStepNumber(uniformIncrement)} mm swatch increment</span>`}
      </div>
    `;
    if (filenameEl) filenameEl.textContent = app.commands.buildStepFilename();
    if (filenameEl && app.commands.isStructuredGeometryBackend()) {
      const aliasEl = app.commands._sbEl("stepBuilderAlias");
      filenameEl.textContent =
        (aliasEl?.value || "").trim() || "enter an alias";
    }
    if (structuredPayload)
      app.commands.renderStepGeometryVisualPreview(structuredPayload);
  }

  function renderStepBuilder() {
    const valueGrid = app.commands._sbEl("stepValueGrid");
    if (!valueGrid) return;
    if (app.commands.isStructuredGeometryBackend()) {
      app.commands.renderStructuredStepBuilder();
      return;
    }
    const suspectIndexes = app.commands.getSuspectSwatchIndexes();
    const columns = app.state.geometries.stepBuilderState.values.length;
    const gridStyle = `grid-template-columns: repeat(${columns}, 42px); width: calc(42px * ${columns} + 1px * ${Math.max(0, columns - 1)});`;

    valueGrid.innerHTML = `
      <div class="sb-variable-row">
        <span class="row-label">Variable Layer</span>
        <div class="sb-content-col">
          <div class="sb-swatch-inputs" style="${gridStyle}">
            ${app.state.geometries.stepBuilderState.values
              .map(
                (value, index) => `
              <div class="sb-swatch${suspectIndexes.includes(index) ? " is-suspect" : ""}">
                <span class="sb-swatch-number">#${index + 1}</span>
                <input type="text" class="step-manual-input" inputmode="decimal" data-step-index="${index}" value="${app.commands.formatStepNumber(value)}" />
              </div>
            `,
              )
              .join("")}
          </div>
          ${app.commands
            .fixedLayerDisplayEntries(
              app.state.geometries.stepBuilderState.fixedLayers,
            )
            .map(
              ({ layer, index }) => `
            <div class="sb-fixed-inline">
              <input type="text" class="fixed-layer-input" inputmode="decimal" data-fixed-index="${index}" value="${app.commands.formatStepNumber(layer.thickness)}" />
              <span class="sb-fixed-label">mm — Fixed Layer ${index + 1}</span>
              <button class="ghost-button small remove-fixed-layer" type="button" data-fixed-index="${index}" style="color:#c62828">Remove</button>
            </div>
          `,
            )
            .join("")}
          <button class="ghost-button small sb-add-fixed-btn" type="button" id="inlineAddFixedLayerBtn">+ Add Fixed Layer</button>
        </div>
      </div>
    `;

    valueGrid.querySelectorAll(".step-manual-input").forEach((input) => {
      app.commands.bindStepDecimalInput(input, {
        onInput: (value) => {
          app.state.geometries.stepBuilderState.values[
            Number(input.dataset.stepIndex)
          ] = value;
          const spineTotalEl = app.commands._sbEl("stepSpineTotalThickness");
          if (spineTotalEl && !spineTotalEl.dataset.userEdited)
            spineTotalEl.value = app.commands.defaultSpineTotalThickness();
          app.commands.updateStepPreview();
        },
      });
    });

    valueGrid.querySelectorAll(".fixed-layer-input").forEach((input) => {
      app.commands.bindStepDecimalInput(input, {
        onInput: (value) => {
          app.state.geometries.stepBuilderState.fixedLayers[
            Number(input.dataset.fixedIndex)
          ].thickness = value;
          const spineTotalEl = app.commands._sbEl("stepSpineTotalThickness");
          if (spineTotalEl && !spineTotalEl.dataset.userEdited)
            spineTotalEl.value = app.commands.defaultSpineTotalThickness();
          app.commands.updateStepPreview();
        },
      });
    });

    valueGrid.querySelectorAll(".remove-fixed-layer").forEach((button) => {
      button.addEventListener("click", () => {
        app.state.geometries.stepBuilderState.fixedLayers.splice(
          Number(button.dataset.fixedIndex),
          1,
        );
        app.commands.renderStepBuilder();
        const spineTotalEl = app.commands._sbEl("stepSpineTotalThickness");
        if (spineTotalEl && !spineTotalEl.dataset.userEdited)
          spineTotalEl.value = app.commands.defaultSpineTotalThickness();
        app.commands.updateStepPreview();
      });
    });

    const inlineAddFixedLayerBtn = document.getElementById(
      "inlineAddFixedLayerBtn",
    );
    if (inlineAddFixedLayerBtn) {
      inlineAddFixedLayerBtn.addEventListener("click", () => {
        app.state.geometries.stepBuilderState.fixedLayers.push({
          thickness: "0.20",
        });
        app.commands.renderStepBuilder();
        const spineTotalEl = app.commands._sbEl("stepSpineTotalThickness");
        if (spineTotalEl && !spineTotalEl.dataset.userEdited)
          spineTotalEl.value = app.commands.defaultSpineTotalThickness();
        app.commands.updateStepPreview();
      });
    }

    app.commands.updateStepPreview();
  }

  function structuredRoleLabel(displayIndex) {
    const roleIndex =
      app.state.geometries.stepBuilderState.layerRoles.length - displayIndex;
    return `LR_${String(roleIndex).padStart(2, "0")}`;
  }

  function renderStructuredStepBuilder() {
    const valueGrid = app.commands._sbEl("stepValueGrid");
    if (!valueGrid) return;
    app.commands.syncStepBuilderLegacyLayerState();
    app.commands.updateStepBuilderDrawerWidth();
    const variable = app.commands.stepBuilderVariableRole();
    const suspectIndexes = app.commands.getSuspectSwatchIndexes();
    const columns = variable.values.length;
    const gridStyle = `grid-template-columns: repeat(${columns}, 35px); width: calc(35px * ${columns} + 1px * ${Math.max(0, columns - 1)});`;

    valueGrid.innerHTML = `
      <div class="sb-layer-role-stack">
        ${app.state.geometries.stepBuilderState.layerRoles
          .map((role, displayIndex) => {
            const roleLabel = app.commands.structuredRoleLabel(displayIndex);
            if (role.kind === "variable") {
              return `
              <div class="sb-layer-role-card is-variable" data-role-id="${role.id}">
                <div class="sb-layer-role-side">
                  <button class="sb-layer-drag-handle" type="button" draggable="true" data-role-id="${role.id}" title="Drag to reorder" aria-label="Drag ${roleLabel} to reorder">⋮⋮</button>
                  <div class="sb-layer-role-meta">
                    <span class="sb-layer-role-id">${roleLabel}</span>
                    <span class="sb-layer-role-kind">Variable</span>
                  </div>
                </div>
                <div class="sb-layer-role-content">
                  <div class="sb-swatch-inputs" style="${gridStyle}">
                    ${role.values
                      .map(
                        (value, index) => `
                      <div class="sb-swatch${suspectIndexes.includes(index) ? " is-suspect" : ""}">
                        <input type="text" class="step-manual-input" inputmode="decimal" data-step-index="${index}" value="${app.commands.formatStepNumber(value)}" />
                      </div>
                    `,
                      )
                      .join("")}
                  </div>
                </div>
              </div>
            `;
            }
            return `
            <div class="sb-layer-role-card is-fixed" data-role-id="${role.id}">
              <div class="sb-layer-role-side">
                <button class="sb-layer-drag-handle" type="button" draggable="true" data-role-id="${role.id}" title="Drag to reorder" aria-label="Drag ${roleLabel} to reorder">⋮⋮</button>
                <div class="sb-layer-role-meta">
                  <span class="sb-layer-role-id">${roleLabel}</span>
                  <span class="sb-layer-role-kind">Fixed</span>
                </div>
              </div>
              <div class="sb-layer-role-content">
                <label class="sb-fixed-role-field" style="${gridStyle}">
                  <input type="text" class="fixed-layer-input" inputmode="decimal" data-role-id="${role.id}" value="${app.commands.formatStepNumber(role.thickness)}" />
                </label>
              </div>
              <button class="sb-layer-remove-button" type="button" data-role-id="${role.id}" title="Remove layer role" aria-label="Remove ${roleLabel}">
                <svg viewBox="0 0 12 12" aria-hidden="true" focusable="false">
                  <path d="M3 3 L9 9 M9 3 L3 9"></path>
                </svg>
              </button>
            </div>
          `;
          })
          .join("")}
        <button class="ghost-button small sb-add-fixed-btn" type="button" id="inlineAddFixedLayerBtn">+ Add Layer Role</button>
      </div>
    `;

    valueGrid.querySelectorAll(".step-manual-input").forEach((input) => {
      app.commands.bindStepDecimalInput(input, {
        onInput: (value) => {
          variable.values[Number(input.dataset.stepIndex)] = value;
          app.commands.syncStepBuilderLegacyLayerState();
          const spineTotalEl = app.commands._sbEl("stepSpineTotalThickness");
          if (spineTotalEl && !spineTotalEl.dataset.userEdited)
            spineTotalEl.value = app.commands.defaultSpineTotalThickness();
          app.commands.updateStepPreview();
        },
      });
    });

    valueGrid.querySelectorAll(".fixed-layer-input").forEach((input) => {
      app.commands.bindStepDecimalInput(input, {
        onInput: (value) => {
          const role = app.state.geometries.stepBuilderState.layerRoles.find(
            (item) => item.id === input.dataset.roleId,
          );
          if (role) role.thickness = value;
          app.commands.syncStepBuilderLegacyLayerState();
          const spineTotalEl = app.commands._sbEl("stepSpineTotalThickness");
          if (spineTotalEl && !spineTotalEl.dataset.userEdited)
            spineTotalEl.value = app.commands.defaultSpineTotalThickness();
          app.commands.updateStepPreview();
        },
      });
    });

    valueGrid.querySelectorAll(".sb-layer-remove-button").forEach((button) => {
      button.addEventListener("click", () => {
        const roleId = button.dataset.roleId;
        app.state.geometries.stepBuilderState.layerRoles =
          app.state.geometries.stepBuilderState.layerRoles.filter(
            (role) => role.id !== roleId || role.kind === "variable",
          );
        app.commands.syncStepBuilderLegacyLayerState();
        app.commands.renderStepBuilder();
        const spineTotalEl = app.commands._sbEl("stepSpineTotalThickness");
        if (spineTotalEl && !spineTotalEl.dataset.userEdited)
          spineTotalEl.value = app.commands.defaultSpineTotalThickness();
        app.commands.updateStepPreview();
      });
    });

    valueGrid.querySelectorAll(".sb-layer-drag-handle").forEach((handle) => {
      handle.addEventListener("dragstart", (event) => {
        event.dataTransfer?.setData("text/plain", handle.dataset.roleId || "");
        if (event.dataTransfer) event.dataTransfer.effectAllowed = "move";
        handle.closest(".sb-layer-role-card")?.classList.add("is-dragging");
      });
      handle.addEventListener("dragend", () => {
        valueGrid
          .querySelectorAll(".sb-layer-role-card")
          .forEach((card) =>
            card.classList.remove("is-dragging", "is-drop-target"),
          );
      });
    });

    valueGrid.querySelectorAll(".sb-layer-role-card").forEach((card) => {
      card.addEventListener("dragover", (event) => {
        event.preventDefault();
        card.classList.add("is-drop-target");
        if (event.dataTransfer) event.dataTransfer.dropEffect = "move";
      });
      card.addEventListener("dragleave", () =>
        card.classList.remove("is-drop-target"),
      );
      card.addEventListener("drop", (event) => {
        event.preventDefault();
        card.classList.remove("is-drop-target");
        const fromId = event.dataTransfer?.getData("text/plain");
        const toId = card.dataset.roleId;
        if (!fromId || !toId || fromId === toId) return;
        const fromIndex =
          app.state.geometries.stepBuilderState.layerRoles.findIndex(
            (role) => role.id === fromId,
          );
        const toIndex =
          app.state.geometries.stepBuilderState.layerRoles.findIndex(
            (role) => role.id === toId,
          );
        if (fromIndex < 0 || toIndex < 0) return;
        const [moved] = app.state.geometries.stepBuilderState.layerRoles.splice(
          fromIndex,
          1,
        );
        app.state.geometries.stepBuilderState.layerRoles.splice(
          toIndex,
          0,
          moved,
        );
        app.commands.syncStepBuilderLegacyLayerState();
        app.commands.renderStepBuilder();
        const spineTotalEl = app.commands._sbEl("stepSpineTotalThickness");
        if (spineTotalEl && !spineTotalEl.dataset.userEdited)
          spineTotalEl.value = app.commands.defaultSpineTotalThickness();
        app.commands.updateStepPreview();
      });
    });

    const inlineAddFixedLayerBtn = document.getElementById(
      "inlineAddFixedLayerBtn",
    );
    if (inlineAddFixedLayerBtn) {
      inlineAddFixedLayerBtn.addEventListener("click", () => {
        app.state.geometries.stepBuilderState.layerRoles.push(
          app.commands.makeStepBuilderRole("fixed", "0.20"),
        );
        app.commands.syncStepBuilderLegacyLayerState();
        app.commands.renderStepBuilder();
        const spineTotalEl = app.commands._sbEl("stepSpineTotalThickness");
        if (spineTotalEl && !spineTotalEl.dataset.userEdited)
          spineTotalEl.value = app.commands.defaultSpineTotalThickness();
        app.commands.updateStepPreview();
      });
    }

    app.commands.updateStepPreview();
  }

  function openStepBuilderDrawer() {
    if (!app.dom.stepBuilderDrawer || !app.dom.stepBuilderBody) return;

    // Close other drawers — mutually exclusive
    app.commands.closeDrawer();
    app.commands.closeBundleMgmtDrawer();

    // Reset builder state to defaults
    app.state.geometries.stepBuilderState.values = [
      "0.20",
      "0.28",
      "0.36",
      "0.44",
      "0.52",
      "0.60",
      "0.68",
      "0.76",
    ];
    app.state.geometries.stepBuilderState.fixedLayers = [];
    app.state.geometries.stepBuilderState.alias = "";
    app.state.geometries.stepBuilderState.bundle = "";
    const structuredMode = app.commands.isStructuredGeometryBackend();
    if (structuredMode)
      app.commands.initializeStepBuilderLayerRoles(
        app.state.geometries.stepBuilderState.values,
      );
    app.dom.stepBuilderDrawer.classList.toggle("is-structured", structuredMode);
    const saveBtn = document.getElementById("stepBuilderSave");
    const createExportBtn = document.getElementById("stepBuilderCreateExport");
    if (saveBtn) saveBtn.textContent = structuredMode ? "Create" : "Generate";
    if (createExportBtn)
      createExportBtn.style.display = structuredMode ? "" : "none";

    app.dom.stepBuilderBody.innerHTML = `
      ${
        structuredMode
          ? `
        <div class="step-builder-layout">
          <div class="step-builder-visual-column">
            ${app.commands.buildDrawerFormModule(
              "Visual Preview",
              `
              <div class="geometry-visual-preview" id="stepGeometryVisualPreview" data-preview-state="unavailable">
                <section class="geometry-visual-view" aria-labelledby="stepGeometryTopHeading">
                  <div class="geometry-visual-view-heading" id="stepGeometryTopHeading">Top View</div>
                  <div class="geometry-visual-surface geometry-visual-top-surface" id="stepGeometryTopView">
                    <div class="geometry-visual-unavailable">Complete valid strip dimensions to see this view.</div>
                  </div>
                  <div class="geometry-visual-derived" id="stepGeometryFootprintLabel"></div>
                </section>
                <section class="geometry-visual-view" aria-labelledby="stepGeometrySideHeading">
                  <div class="geometry-visual-view-heading" id="stepGeometrySideHeading">Side View</div>
                  <div class="geometry-visual-surface geometry-visual-side-surface" id="stepGeometrySideView">
                    <div class="geometry-visual-unavailable">Complete valid strip dimensions to see this view.</div>
                  </div>
                  <div class="geometry-visual-note">Vertical thickness is exaggerated for visibility.</div>
                </section>
              </div>
            `,
              {
                classes: "step-builder-module step-builder-visual-module",
                bodyClass: "drawer-module-body-tight",
                density: "compact",
              },
            )}
          </div>
          <div class="step-builder-form-column">
      `
          : ""
      }
      ${
        structuredMode
          ? app.commands.buildDrawerFormModule(
              "Strip Layout",
              `
        <div class="sb-param-line sb-strip-layout-row">
          <label class="sb-field-inline">
            <span class="field-label">Swatch Width (mm)</span>
            <input type="text" id="stepSwatchWidth" inputmode="decimal" value="12.00" />
          </label>
          <label class="sb-field-inline">
            <span class="field-label">Swatch Height (mm)</span>
            <input type="text" id="stepSwatchHeight" inputmode="decimal" value="20.00" />
          </label>
          <label class="sb-field-inline">
            <span class="field-label">Number of Swatches</span>
            <input type="number" id="stepColumnCount" min="1" max="48" step="1" value="8" />
          </label>
        </div>
        <div class="sb-param-line sb-strip-layout-row">
          <label class="sb-field-inline">
            <span class="field-label">Spine Width (mm)</span>
            <input type="text" id="stepSpineWidth" inputmode="decimal" value="3.00" />
          </label>
          <label class="sb-field-inline">
            <span class="field-label">Spine Thickness (mm)</span>
            <input type="text" id="stepSpineTotalThickness" inputmode="decimal" value="${app.commands.defaultSpineTotalThickness()}" />
          </label>
        </div>
      `,
              {
                classes: "step-builder-module",
                bodyClass: "drawer-module-body-tight",
                density: "compact",
              },
            )
          : ""
      }

      ${app.commands.buildDrawerFormModule(
        structuredMode ? "Variable Layer Increment" : "Parameters",
        `
        ${structuredMode ? `<p class="sb-module-caption">Auto-fill swatch thicknesses for strips with constant swatch-to-swatch thickness increments (optional)</p>` : ""}
        ${
          !structuredMode
            ? `
          <div class="sb-param-line">
            <label class="sb-field-inline">
              <span class="field-label">Layer<br>Height (mm)</span>
              <input type="text" id="stepLayerHeight" inputmode="decimal" value="0.08" />
            </label>
          </div>
        `
            : ""
        }
        <div class="sb-param-line sb-param-line-actions">
          <label class="sb-field-inline">
            <span class="field-label">First Swatch Thickness (mm)</span>
            <input type="text" id="stepStartValue" inputmode="decimal" value="0.20" />
          </label>
          <label class="sb-field-inline">
            <span class="field-label">Swatch-to-Swatch Increment (mm)</span>
            <input type="text" id="stepIncrementValue" inputmode="decimal" value="0.08" />
          </label>
          <div class="sb-params-actions">
            <button class="primary-button small" type="button" id="populateStepBtn">Fill Values</button>
          </div>
        </div>
      `,
        {
          classes: "step-builder-module",
          bodyClass: "drawer-module-body-tight",
          density: "compact",
        },
      )}

      ${app.commands.buildDrawerFormModule(
        "Strip Definition",
        `<div id="stepValueGrid"></div>`,
        {
          classes: "step-builder-module",
          bodyClass: "drawer-module-body-tight",
          density: "compact",
        },
      )}

      ${app.commands.buildDrawerFormModule(
        structuredMode ? "Strip Diagram Preview" : "Preview",
        `<div id="stepPreviewArea" class="sb-preview"></div>`,
        {
          classes: "step-builder-module",
          bodyClass: "drawer-module-body-tight",
          density: "compact",
        },
      )}

      ${app.commands.buildDrawerFormModule(
        "Metadata",
        `
        <div class="sb-meta-stack">
          <label class="sb-field-full">
            <span class="field-label">Alias</span>
            <input type="text" id="stepBuilderAlias" placeholder="e.g. thin over white" value="" />
          </label>
          <label class="sb-field-full">
            <span class="field-label">Bundle</span>
            <select id="stepBuilderBundle"><option value="">— no bundles exist —</option></select>
          </label>
        </div>
      `,
        {
          classes: "step-builder-module",
          bodyClass: "drawer-module-body-tight",
          density: "form",
        },
      )}

      ${app.commands.buildDrawerFormModule(
        "Output",
        `
        <div class="sb-output-stack">
          <div class="sb-output-field">
            <span class="field-label">${structuredMode ? "Default export name:" : "Filename:"}</span>
            <code class="mono" id="stepFilenamePreview"></code>
          </div>
          <div class="sb-output-field">
            <span class="field-label">STEP Export Path:</span>
            <span class="mono muted-line" id="stepLibraryPathDisplay" style="font-size:11px">${app.state.session._serverConfig ? app.state.session._serverConfig.step_export_relative || app.state.session._serverConfig.step_library_relative : "output/steps/"}</span>
            <button class="copy-pill drawer-utility-button" id="copyStepBuilderPath" type="button">Copy Full Export Path</button>
          </div>
        </div>
        <div class="sb-validation-error" id="stepValidationError"></div>
      `,
        {
          classes: "step-builder-module",
          bodyClass: "drawer-module-body-tight",
          density: "compact",
        },
      )}
      ${
        structuredMode
          ? `
          </div>
        </div>
      `
          : ""
      }
    `;

    app.dom.stepBuilderDrawer.classList.add("is-open");
    app.dom.stepBuilderDrawer.setAttribute("aria-hidden", "false");

    app.commands.bindStepBuilderControls();
    app.commands.renderStepBuilder();
    app.commands.populateStepBuilderBundleDropdown();
  }

  async function populateStepBuilderBundleDropdown() {
    const select = document.getElementById("stepBuilderBundle");
    if (!select) return;
    try {
      const bundles = await app.api.fetchBundles();
      if (bundles.length === 0) {
        select.innerHTML = `<option value="">— no bundles exist —</option>`;
      } else {
        select.innerHTML =
          `<option value="">— none —</option>` +
          bundles
            .map((b) => `<option value="${b.name}">${b.name}</option>`)
            .join("");
      }
    } catch (_) {
      select.innerHTML = `<option value="">— no bundles available —</option>`;
    }
  }

  function closeStepBuilderDrawer() {
    if (!app.dom.stepBuilderDrawer) return;
    app.dom.stepBuilderDrawer.classList.remove("is-open");
    app.dom.stepBuilderDrawer.classList.remove("is-structured");
    app.dom.stepBuilderDrawer.setAttribute("aria-hidden", "true");
    app.dom.stepBuilderDrawer.style.removeProperty("--step-builder-width");
    app.dom.stepBuilderDrawer.style.removeProperty("--step-builder-form-width");
    if (app.dom.stepBuilderBody) app.dom.stepBuilderBody.innerHTML = "";
    const saveBtn = document.getElementById("stepBuilderSave");
    const createExportBtn = document.getElementById("stepBuilderCreateExport");
    if (saveBtn) saveBtn.textContent = "Generate";
    if (createExportBtn) createExportBtn.style.display = "";
  }

  function isStepBuilderOpen() {
    return (
      app.dom.stepBuilderDrawer &&
      app.dom.stepBuilderDrawer.classList.contains("is-open")
    );
  }

  function bindStepBuilderButton() {
    const openButton = document.getElementById("openStepBuilderBtn");
    if (!openButton) return;
    openButton.addEventListener("click", () => {
      if (app.commands.isStepBuilderOpen()) {
        app.commands.closeStepBuilderDrawer();
      } else {
        app.commands.openStepBuilderDrawer();
      }
    });
  }

  function bindGeometryLibraryExportButton() {
    const button = document.getElementById("exportGeometryFilesBtn");
    if (!button) return;
    button.addEventListener("click", async () => {
      if (document.querySelector(".maintenance-workflow-overlay")) {
        app.commands.showImportToast(
          "Close the active maintenance workflow before opening another.",
          "warning",
        );
        return;
      }
      const originalText = button.textContent;
      button.disabled = true;
      button.textContent = "Loading...";
      try {
        const operation = await app.commands.maintenanceOperationById(
          "export_geometry_files",
        );
        if (!operation) {
          app.commands.showImportToast(
            "Export Geometry Files is not available",
            "error",
          );
          return;
        }
        if (operation.enabled === false) {
          app.commands.showImportToast(
            operation.unavailable_reason ||
              operation.disabled_reason ||
              "Export Geometry Files is unavailable",
            "warning",
          );
          return;
        }
        app.commands.showMaintenanceWorkflow(operation, null, {
          exportGeometryScope: "all_geometries",
        });
      } catch (err) {
        app.commands.showImportToast(
          err.message || "Could not open geometry export workflow",
          "error",
        );
      } finally {
        if (button.isConnected) {
          button.disabled = false;
          button.textContent = originalText;
        }
      }
    });
  }

  async function refreshBundleOptionsFromRegistry() {
    try {
      const bundles = await app.api.fetchBundles();
      if (app.dom.stepBundleOptions) {
        app.dom.stepBundleOptions.innerHTML = bundles
          .map((b) => `<option value="${b.name}"></option>`)
          .join("");
      }
    } catch (_) {
      // Fall back to existing stepMetadata-based bundle names
      app.commands.renderBundleOptions();
    }
  }

  function bindStepStoragePathButton(stepStoragePath) {
    const button = document.getElementById("copyStepStoragePath");
    if (!button) return;

    button.addEventListener("click", async () => {
      let copied = false;
      try {
        if (navigator.clipboard?.writeText) {
          await navigator.clipboard.writeText(stepStoragePath);
          copied = true;
        }
      } catch (error) {
        copied = false;
      }

      if (!copied) {
        const tempInput = document.createElement("input");
        tempInput.value = stepStoragePath;
        document.body.appendChild(tempInput);
        tempInput.select();
        document.execCommand("copy");
        document.body.removeChild(tempInput);
        copied = true;
      }

      const originalText = button.textContent;
      button.textContent = copied ? "Copied export path" : originalText;
      setTimeout(() => {
        button.textContent = originalText;
      }, 1200);
    });
  }

  function bindStepBuilderControls() {
    const populateBtn = app.commands._sbEl("populateStepBtn");
    const saveBtn = document.getElementById("stepBuilderSave");
    const createExportBtn = document.getElementById("stepBuilderCreateExport");
    const startInput = app.commands._sbEl("stepStartValue");
    const incInput = app.commands._sbEl("stepIncrementValue");
    const lhInput = app.commands._sbEl("stepLayerHeight");
    const countInput = app.commands._sbEl("stepColumnCount");
    const swatchWidthInput = app.commands._sbEl("stepSwatchWidth");
    const swatchHeightInput = app.commands._sbEl("stepSwatchHeight");
    const spineWidthInput = app.commands._sbEl("stepSpineWidth");
    const spineTotalInput = app.commands._sbEl("stepSpineTotalThickness");
    const aliasInput = app.commands._sbEl("stepBuilderAlias");
    const bundleInput = app.commands._sbEl("stepBuilderBundle");
    const copyPathBtn = app.commands._sbEl("copyStepBuilderPath");
    const structuredMode = app.commands.isStructuredGeometryBackend();

    if (populateBtn)
      populateBtn.addEventListener("click", () => {
        app.commands.populateStepValues();
        app.commands.renderStepBuilder();
      });

    const doCreate = async ({ openExport = false } = {}) => {
      // Validate all inputs
      const validationEl = app.commands._sbEl("stepValidationError");
      const errors = [];
      app.commands.clearStepBuilderValidationHighlights();
      const lhVal = app.commands.numericValue(
        lhInput ? lhInput.value : "",
        NaN,
      );
      if (!structuredMode && (isNaN(lhVal) || lhVal <= 0))
        errors.push("Layer height must be a positive number");
      if (structuredMode) {
        const payload = app.commands.structuredGeometryPayloadFromBuilder();
        const rawCount = Number(countInput?.value || "");
        if (!Number.isInteger(rawCount) || rawCount < 1 || rawCount > 48) {
          errors.push("Swatch count must be a whole number from 1 to 48");
          app.commands.markStepBuilderInvalid(countInput);
        }
        if (!payload.alias) {
          errors.push("Alias is required");
          app.commands.markStepBuilderInvalid(aliasInput);
        }
        if (
          !Number.isFinite(payload.swatch_width_mm) ||
          payload.swatch_width_mm <= 0
        ) {
          errors.push("Swatch width must be a positive number");
          app.commands.markStepBuilderInvalid(swatchWidthInput);
        }
        if (
          !Number.isFinite(payload.swatch_height_mm) ||
          payload.swatch_height_mm <= 0
        ) {
          errors.push("Swatch height must be a positive number");
          app.commands.markStepBuilderInvalid(swatchHeightInput);
        }
        if (
          !Number.isFinite(payload.spine_width_mm) ||
          payload.spine_width_mm <= 0
        ) {
          errors.push("Spine width must be a positive number");
          app.commands.markStepBuilderInvalid(spineWidthInput);
        }
        if (
          !Number.isFinite(payload.spine_total_thickness_mm) ||
          payload.spine_total_thickness_mm <= 0
        ) {
          errors.push("Spine total height must be a positive number");
          app.commands.markStepBuilderInvalid(spineTotalInput);
        }
        if (
          payload.spine_total_thickness_mm + 1e-9 <
          app.commands.stepBuilderStackHeightMax()
        ) {
          errors.push(
            "Spine total height must be at least as tall as the thickest swatch stack",
          );
          app.commands.markStepBuilderInvalid(spineTotalInput);
        }
      }
      app.state.geometries.stepBuilderState.values.forEach((v, i) => {
        if (isNaN(app.commands.numericValue(v, NaN))) {
          errors.push(`Swatch #${i + 1} is not a valid number`);
          app.commands.markStepBuilderInvalid(
            document.querySelector(`[data-step-index="${i}"]`),
          );
        }
      });
      app.state.geometries.stepBuilderState.fixedLayers.forEach((fl, i) => {
        if (isNaN(app.commands.numericValue(fl.thickness, NaN))) {
          errors.push(`Fixed layer ${i + 1} is not a valid number`);
          app.commands.markStepBuilderInvalid(
            fl.roleId
              ? document.querySelector(
                  `.fixed-layer-input[data-role-id="${fl.roleId}"]`,
                )
              : document.querySelector(
                  `.fixed-layer-input[data-fixed-index="${i}"]`,
                ),
          );
        }
      });
      const filename = app.commands.buildStepFilename();

      if (errors.length > 0) {
        if (validationEl) {
          validationEl.style.display = "block";
          validationEl.textContent = errors[0];
        }
        app.commands.showImportToast(errors[0], "error");
        return;
      }
      if (validationEl) validationEl.style.display = "none";

      // Generate STEP + STL files
      const varThick = app.state.geometries.stepBuilderState.values.map((v) =>
        app.commands.numericValue(v, NaN),
      );
      const fixThick = app.commands
        .getStepBuilderFixedThicknesses()
        .map((thickness) => app.commands.numericValue(thickness, NaN));

      [saveBtn, createExportBtn].forEach((button) => {
        if (button) button.disabled = true;
      });
      if (saveBtn)
        saveBtn.textContent = structuredMode ? "Creating..." : "Generating...";
      try {
        const alias = aliasInput ? aliasInput.value.trim() : "";
        const bundle = bundleInput ? bundleInput.value.trim() : "";
        if (structuredMode) {
          const geometry = await app.api.createGeometry(
            app.commands.structuredGeometryPayloadFromBuilder(),
          );
          app.state.logbook.stepMetadata[geometry.geometry_id] = {
            alias: geometry.alias || alias,
            bundle,
            deleted: false,
          };
          let bundleWarning = "";
          if (bundle && typeof app.api.addStepToBundle === "function") {
            try {
              await app.api.addStepToBundle(bundle, geometry.geometry_id);
            } catch (bundleErr) {
              bundleWarning = ` Bundle link failed: ${bundleErr.message || "unknown error"}`;
            }
          }
          app.commands.showImportToast(
            `Created ${geometry.alias || geometry.geometry_id}.${bundleWarning}`,
            bundleWarning ? "warning" : "success",
          );
          await app.commands.handleRefresh();
          app.commands.closeStepBuilderDrawer();
          if (openExport)
            app.commands.openGeometryExportDialog(
              geometry.geometry_id,
              geometry.alias || alias,
            );
        } else {
          const result = await app.api.generateStepFile(
            varThick,
            fixThick,
            lhVal,
            filename,
          );

          // Save alias/bundle metadata locally and persist to server
          const generatedStepId = result.step_id || filename;
          app.state.logbook.stepMetadata[generatedStepId] = {
            alias,
            bundle,
            deleted: false,
          };
          if (typeof app.api.updateStepMetadata === "function") {
            await app.api.updateStepMetadata(generatedStepId, alias, bundle);
          }
          app.commands.showImportToast(
            `${result.reused ? "Reused" : "Saved"} ${result.artifact_filename || filename} + ${result.stl_files.length} STL(s)`,
            "success",
          );
          app.commands.handleRefresh();
          app.commands.closeStepBuilderDrawer();
        }
      } catch (err) {
        const msg = err.message || "Generation failed";
        app.commands.showImportToast(msg, "error");
        if (validationEl) {
          validationEl.style.display = "block";
          validationEl.textContent = msg;
        }
      } finally {
        [saveBtn, createExportBtn].forEach((button) => {
          if (button) button.disabled = false;
        });
        if (saveBtn)
          saveBtn.textContent = structuredMode ? "Create" : "Generate";
      }
    };

    if (saveBtn) saveBtn.onclick = () => doCreate({ openExport: false });
    if (createExportBtn)
      createExportBtn.onclick = () => doCreate({ openExport: true });

    [
      startInput,
      incInput,
      lhInput,
      swatchWidthInput,
      swatchHeightInput,
      spineWidthInput,
    ].forEach((input) => {
      app.commands.bindStepDecimalInput(input, {
        onInput: () => app.commands.updateStepPreview(),
      });
    });

    if (spineTotalInput) {
      app.commands.bindStepDecimalInput(spineTotalInput, {
        onInput: () => {
          spineTotalInput.dataset.userEdited = "true";
          app.commands.updateStepPreview();
        },
      });
    }

    if (countInput) {
      const normalizeCount = () => {
        countInput.value = String(countInput.value || "").replace(/[^\d]/g, "");
      };
      countInput.addEventListener("change", () => {
        normalizeCount();
        app.commands.resizeStepBuilderValues(countInput.value);
        app.commands.renderStepBuilder();
      });
      countInput.addEventListener("input", () => {
        normalizeCount();
        app.commands.resizeStepBuilderValues(countInput.value);
        app.commands.renderStepBuilder();
      });
    }

    if (aliasInput)
      aliasInput.addEventListener("input", () => {
        app.state.geometries.stepBuilderState.alias = aliasInput.value.trim();
        app.commands.updateStepPreview();
      });

    if (bundleInput)
      bundleInput.addEventListener("change", () => {
        app.state.geometries.stepBuilderState.bundle = bundleInput.value.trim();
        app.commands.updateStepPreview();
      });

    if (copyPathBtn) {
      copyPathBtn.addEventListener("click", async () => {
        // Copy the full system path — the server provides data_root info
        const fullPath =
          app.state.session.data._stepLibraryFullPath ||
          (app.state.session._serverConfig
            ? app.state.session._serverConfig.step_library_path
            : "step/");
        try {
          await navigator.clipboard.writeText(fullPath);
        } catch (e) {
          const t = document.createElement("input");
          t.value = fullPath;
          document.body.appendChild(t);
          t.select();
          document.execCommand("copy");
          document.body.removeChild(t);
        }
        const orig = copyPathBtn.textContent;
        copyPathBtn.textContent = "Copied!";
        setTimeout(() => {
          copyPathBtn.textContent = orig;
        }, 1200);
      });
    }
  }

  Object.assign(app.commands, {
    buildStepFilename,
    defaultStepBuilderValues,
    makeStepBuilderRole,
    initializeStepBuilderLayerRoles,
    stepBuilderVariableRole,
    syncStepBuilderLegacyLayerState,
    getStepBuilderFixedThicknesses,
    isStructuredGeometryBackend,
    stepBuilderSwatchCount,
    updateStepBuilderDrawerWidth,
    resizeStepBuilderValues,
    stepBuilderStackHeightMax,
    defaultSpineTotalThickness,
    structuredGeometryPayloadFromBuilder,
    projectGeometryVisualDraft,
    geometryVisualSvgNumber,
    buildGeometryVisualTopSvg,
    buildGeometryVisualSideSvg,
    renderStepGeometryVisualPreview,
    markStepBuilderInvalid,
    clearStepBuilderValidationHighlights,
    parentPathFromExportPath,
    geometryExportDestinationLabel,
    geometryExportToastMessage,
    geometryExportConflictDetail,
    showGeometryOverwriteConfirmDialog,
    openGeometryExportDialog,
    populateStepValues,
    updateStepPreview,
    renderStepBuilder,
    structuredRoleLabel,
    renderStructuredStepBuilder,
    openStepBuilderDrawer,
    populateStepBuilderBundleDropdown,
    closeStepBuilderDrawer,
    isStepBuilderOpen,
    bindStepBuilderButton,
    bindGeometryLibraryExportButton,
    refreshBundleOptionsFromRegistry,
    bindStepStoragePathButton,
    bindStepBuilderControls,
  });
}
