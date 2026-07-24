/** Install features/geometries/preview commands. */
export function installFeaturesGeometriesPreview(app) {
  function sanitizeStepDecimalInput(raw) {
    let sanitized = String(raw ?? "").replace(/[^\d.]/g, "");
    const firstDot = sanitized.indexOf(".");
    if (firstDot >= 0) {
      sanitized =
        sanitized.slice(0, firstDot + 1) +
        sanitized.slice(firstDot + 1).replace(/\./g, "");
    }
    if (sanitized.startsWith(".")) sanitized = `0${sanitized}`;
    return sanitized;
  }

  function normalizeStepDecimalInput(raw, fallback = "0.00") {
    const sanitized = app.commands.sanitizeStepDecimalInput(raw);
    const numeric = app.commands.numericValue(sanitized, NaN);
    if (!Number.isFinite(numeric)) return fallback;
    return app.commands.formatStepNumber(numeric);
  }

  function bindStepDecimalInput(input, { onInput, onBlur } = {}) {
    if (!input) return;
    input.setAttribute("inputmode", "decimal");
    input.setAttribute("autocomplete", "off");
    input.setAttribute("spellcheck", "false");

    input.addEventListener("input", () => {
      const sanitized = app.commands.sanitizeStepDecimalInput(input.value);
      if (input.value !== sanitized) input.value = sanitized;
      if (onInput) onInput(sanitized, input);
    });

    input.addEventListener("blur", () => {
      const normalized = app.commands.normalizeStepDecimalInput(input.value);
      input.value = normalized;
      if (onBlur) onBlur(normalized, input);
      else if (onInput) onInput(normalized, input);
    });
  }

  function getSuspectSwatchIndexes() {
    const lhEl = app.commands._sbEl("stepLayerHeight");
    const layerHeight = app.commands.numericValue(lhEl ? lhEl.value : NaN, NaN);
    if (!Number.isFinite(layerHeight) || layerHeight <= 0) return [];

    const baseIndex = 0;
    const baseValue = app.commands.numericValue(
      app.state.geometries.stepBuilderState.values[baseIndex],
      NaN,
    );
    if (!Number.isFinite(baseValue)) return [];

    return app.state.geometries.stepBuilderState.values.reduce(
      (suspectIndexes, value, index) => {
        if (index < baseIndex) return suspectIndexes;
        const numeric = app.commands.numericValue(value, NaN);
        if (!Number.isFinite(numeric)) {
          suspectIndexes.push(index);
          return suspectIndexes;
        }
        const delta = numeric - baseValue;
        const steps = delta / layerHeight;
        if (Math.abs(steps - Math.round(steps)) >= 1e-6) {
          suspectIndexes.push(index);
        }
        return suspectIndexes;
      },
      [],
    );
  }

  function buildStripMiniTable(exp) {
    const roleRows = [...(exp.roles || [])].sort(
      (a, b) => Number(b.role_index || 0) - Number(a.role_index || 0),
    );
    if (roleRows.length === 0) {
      return `<div class="strip-diagram-contract-error">Missing geometry role data</div>`;
    }
    const variableRole = roleRows.find((role) => role.role_kind === "variable");
    if (!variableRole) {
      return `<div class="strip-diagram-contract-error">Missing variable layer role</div>`;
    }
    const variableFilament = app.commands.filamentMeta(
      variableRole.filament_id || "",
    );
    const variableHex = variableFilament?.hex || "#dddddd";
    const variableText = app.commands.textColor(variableHex);
    if (!(exp.variable_thicknesses_mm || []).length) {
      return `<div class="strip-diagram-contract-error">Missing swatch thickness data</div>`;
    }
    const variableCells = (exp.variable_thicknesses_mm || [])
      .map(
        (thickness) =>
          `<td style="background:${variableHex};color:${variableText}">${Number(thickness).toFixed(2)}</td>`,
      )
      .join("");
    const swatchCount = (exp.variable_thicknesses_mm || []).length;

    const rows = roleRows.map((role) => {
      if (role.role_kind === "variable") return `<tr>${variableCells}</tr>`;
      if (role.fixed_thickness_mm == null) {
        return `<tr><td colspan="${swatchCount}"><div class="strip-diagram-contract-error">Missing fixed role thickness</div></td></tr>`;
      }
      const thickness = role.fixed_thickness_mm;
      const fixedId = role.filament_id || "";
      const fixed = app.commands.filamentMeta(fixedId);
      const fixedHex = fixed?.hex || "#eeeeee";
      const fixedText = app.commands.textColor(fixedHex);
      const label = `${Number(thickness).toFixed(2)}mm`;
      return `<tr><td colspan="${swatchCount}" style="background:${fixedHex};color:${fixedText}">${label}</td></tr>`;
    });

    return `<table class="mini-strip-table">${rows.join("")}</table>`;
  }

  function buildAssignedGeometryRolesFromAssignments(
    step,
    roleAssignments = [],
  ) {
    const filamentByRole = new Map(
      (roleAssignments || []).map((assignment) => [
        Number(assignment.role_index),
        assignment.filament_id || "",
      ]),
    );
    return [...(step?.roles || [])]
      .sort((a, b) => Number(a.role_index || 0) - Number(b.role_index || 0))
      .map((role) => ({
        ...role,
        filament_id: filamentByRole.get(Number(role.role_index)) || "",
      }));
  }

  function buildGeometryStripMiniTable(step) {
    const variableSlots = [...(step?.swatch_slots || [])].sort(
      (a, b) => Number(a.swatch_index || 0) - Number(b.swatch_index || 0),
    );
    if (!variableSlots.length) {
      return `<div class="strip-diagram-contract-error">Missing geometry swatch slots</div>`;
    }
    const variableThicknesses = variableSlots.map((slot) =>
      Number(slot.variable_thickness_mm || 0),
    );

    const variableCells = variableThicknesses
      .map(
        (thickness) =>
          `<td style="background:#d7d7d3;color:#222">${Number(thickness).toFixed(2)}</td>`,
      )
      .join("");
    const swatchCount =
      variableThicknesses.length ||
      Number(step?.swatch_count || step?.layout_columns || 8);

    const roles = [...(step?.roles || [])].sort(
      (a, b) => Number(b.role_index || 0) - Number(a.role_index || 0),
    );
    if (roles.length === 0) {
      return `<div class="strip-diagram-contract-error">Missing geometry role data</div>`;
    }
    const rows = roles.map((role) => {
      if (role.role_kind === "variable") {
        return `<tr>${variableCells}</tr>`;
      }
      const value = Number(role.fixed_thickness_mm || 0).toFixed(2);
      return `<tr><td colspan="${swatchCount}" style="background:#ececea;color:#222">${value}mm</td></tr>`;
    });
    return `<table class="mini-strip-table">${rows.join("")}</table>`;
  }

  function fixedLayerDisplayEntries(fixedLayers = []) {
    return (fixedLayers || [])
      .map((layer, index) => ({ layer, index }))
      .reverse();
  }

  function fixedLayerCanonicalEntries(fixedLayers = []) {
    return (fixedLayers || [])
      .map((layer, index) => ({ layer, index }))
      .sort(
        (a, b) =>
          Number(a.layer.role_index || a.index + 1) -
          Number(b.layer.role_index || b.index + 1),
      );
  }

  function collectFixedSelectValuesByRole(selector) {
    const byRole = new Map();
    app.dom.detailSidebar.querySelectorAll(selector).forEach((el) => {
      const roleIndex = Number(el.dataset?.roleIndex || 0);
      if (Number.isFinite(roleIndex) && roleIndex > 0) {
        byRole.set(roleIndex, el.dataset?.filamentId || el.value || "");
      }
    });
    return byRole;
  }

  function canonicalFixedFilamentIdsFromMap(step, fixedIdByRole) {
    return app.commands
      .fixedLayerCanonicalEntries(step?.fixed_layers || [])
      .map(
        ({ layer }) => fixedIdByRole.get(Number(layer.role_index || 0)) || "",
      );
  }

  function fixedLayerCanonicalThicknesses(fixedLayers = []) {
    return app.commands
      .fixedLayerCanonicalEntries(fixedLayers)
      .map((entry) => entry.layer.thickness_mm || 0);
  }

  function buildRoleAssignmentsForStep(
    step,
    variableFilamentId,
    fixedIdByRole,
  ) {
    return [...(step?.roles || [])]
      .sort((a, b) => Number(a.role_index || 0) - Number(b.role_index || 0))
      .map((role) => ({
        role_index: Number(role.role_index || 0),
        filament_id:
          role.role_kind === "variable"
            ? variableFilamentId
            : fixedIdByRole.get(Number(role.role_index || 0)) || "",
      }));
  }

  function filamentIdsFromRoleAssignments(
    step,
    roleAssignments = [],
    { order = "top-to-bottom" } = {},
  ) {
    return app.commands
      .buildAssignedGeometryRolesFromAssignments(step, roleAssignments)
      .sort((a, b) => {
        const delta = Number(a.role_index || 0) - Number(b.role_index || 0);
        return order === "bottom-to-top" ? delta : -delta;
      })
      .map((role) => role.filament_id || "");
  }

  function sampleRoleAssignmentTuple(exp) {
    return [...(exp?.roles || [])]
      .sort((a, b) => Number(a.role_index || 0) - Number(b.role_index || 0))
      .map((role) => ({
        role_index: Number(role.role_index || 0),
        filament_id: role.filament_id || "",
      }));
  }

  function fixedFilamentIdsByRoleFromSample(exp) {
    const byRole = new Map();
    (exp?.roles || []).forEach((role) => {
      if (role.role_kind === "fixed") {
        byRole.set(Number(role.role_index || 0), role.filament_id || "");
      }
    });
    return byRole;
  }

  function variableRoleForStep(step) {
    return (
      (step?.roles || []).find((role) => role.role_kind === "variable") || null
    );
  }

  function buildFixedLayerPreviewValues(
    fixedLayers = [],
    fixedIdsByIndex = [],
  ) {
    return app.commands.fixedLayerDisplayEntries(fixedLayers).reduce(
      (acc, entry) => {
        acc.thicknesses.push(entry.layer.thickness_mm || 0);
        acc.ids.push(fixedIdsByIndex[entry.index] || "");
        return acc;
      },
      { thicknesses: [], ids: [] },
    );
  }

  function stepMeta(stepId) {
    const canonicalId = app.commands.stepIdFromRef(stepId);
    if (!app.state.logbook.stepMetadata[canonicalId]) {
      const step = app.commands.stepRecordByRef(stepId);
      app.state.logbook.stepMetadata[canonicalId] = {
        alias: step?.alias || "",
        bundle: step?.bundle || "",
        deleted: false,
      };
    }
    return app.state.logbook.stepMetadata[canonicalId];
  }

  function existingBundleNames() {
    return [
      ...new Set(
        Object.values(app.state.logbook.stepMetadata)
          .map((meta) => (meta.bundle || "").trim())
          .filter(Boolean),
      ),
    ].sort((a, b) => a.localeCompare(b));
  }

  function renderBundleOptions() {
    if (!app.dom.stepBundleOptions) return;
    // Try to load from bundles registry, fall back to stepMetadata-based names
    app.commands.refreshBundleOptionsFromRegistry().catch(() => {
      app.dom.stepBundleOptions.innerHTML = app.commands
        .existingBundleNames()
        .map((bundle) => `<option value="${bundle}"></option>`)
        .join("");
    });
  }

  function resetStepEditorState(stepId = null) {
    const meta = stepId
      ? app.commands.stepMeta(stepId)
      : { alias: "", bundle: "" };
    app.state.geometries.stepEditorState = {
      stepId,
      isEditing: false,
      draftAlias: meta.alias || "",
      draftBundle: meta.bundle || "",
      confirmDelete: false,
      deleteMessage: "",
      deleteMessageKind: "",
    };
  }

  function stepUsageCount(stepId) {
    const canonicalId = app.commands.stepIdFromRef(stepId);
    return app.state.session.data.samples.filter(
      (exp) => app.commands.sampleStepId(exp) === canonicalId,
    ).length;
  }

  Object.assign(app.commands, {
    sanitizeStepDecimalInput,
    normalizeStepDecimalInput,
    bindStepDecimalInput,
    getSuspectSwatchIndexes,
    buildStripMiniTable,
    buildAssignedGeometryRolesFromAssignments,
    buildGeometryStripMiniTable,
    fixedLayerDisplayEntries,
    fixedLayerCanonicalEntries,
    collectFixedSelectValuesByRole,
    canonicalFixedFilamentIdsFromMap,
    fixedLayerCanonicalThicknesses,
    buildRoleAssignmentsForStep,
    filamentIdsFromRoleAssignments,
    sampleRoleAssignmentTuple,
    fixedFilamentIdsByRoleFromSample,
    variableRoleForStep,
    buildFixedLayerPreviewValues,
    stepMeta,
    existingBundleNames,
    renderBundleOptions,
    resetStepEditorState,
    stepUsageCount,
  });
}
