export function installApiRecords(api) {
  async function assignImage(sampleId, filename, orientationRots) {
    const body = { filename };
    if (orientationRots != null) body.orientation_rots = orientationRots;
    return api.apiPost(
      `/samples/${encodeURIComponent(sampleId)}/assign-image`,
      body,
    );
  }

  async function assignBlank(sampleId, blankId) {
    return api.apiPost(
      `/samples/${encodeURIComponent(sampleId)}/assign-blank`,
      { blank_id: blankId },
    );
  }

  async function flagSample(sampleId, reason) {
    return api.apiPost(`/samples/${encodeURIComponent(sampleId)}/flag`, {
      reason,
    });
  }

  async function unflagSample(sampleId) {
    return api.apiPost(`/samples/${encodeURIComponent(sampleId)}/unflag`);
  }

  async function rejectSample(sampleId) {
    return api.apiPost(`/samples/${encodeURIComponent(sampleId)}/reject`);
  }

  async function ignoreImage(filename) {
    return api.apiPost(`/images/${encodeURIComponent(filename)}/ignore`);
  }

  async function unignoreImage(filename) {
    return api.apiPost(`/images/${encodeURIComponent(filename)}/unignore`);
  }

  async function rotateImage(filename, rotationCw) {
    return api.apiPost(`/images/${encodeURIComponent(filename)}/rotation`, {
      rotation_cw: rotationCw,
    });
  }

  async function registerBlank(filename, sessionTag) {
    const body = { filename };
    if (sessionTag) body.session_tag = sessionTag;
    return api.apiPost("/blanks/register", body);
  }

  async function unregisterBlank(blankId) {
    return api.apiFetch(`/blanks/${encodeURIComponent(blankId)}`, {
      method: "DELETE",
    });
  }

  async function unassignImage(sampleId) {
    return api.apiPost(
      `/samples/${encodeURIComponent(sampleId)}/assign-image`,
      { filename: null },
    );
  }

  async function excludeSwatch(sampleId, swatchIndex, reason) {
    return api.apiPost(
      `/samples/${encodeURIComponent(sampleId)}/exclude-swatch`,
      {
        swatch_index: swatchIndex,
        reason: reason || "",
      },
    );
  }

  async function includeSwatch(sampleId, swatchIndex) {
    return api.apiPost(
      `/samples/${encodeURIComponent(sampleId)}/include-swatch`,
      {
        swatch_index: swatchIndex,
      },
    );
  }

  async function updateSampleFitExclusion(sampleId, updates) {
    return api.apiPatch(
      `/samples/${encodeURIComponent(sampleId)}/fit-exclusion`,
      updates,
    );
  }

  async function updateSampleSwatchFitExclusions(
    sampleId,
    excludedSwatches = [],
  ) {
    return api.updateSampleFitExclusion(sampleId, {
      excluded_swatches: excludedSwatches,
    });
  }

  async function createSample(
    stepId,
    variableFilamentId,
    fixedFilamentIds = [],
    notes = "",
    fixedThicknessesMm = [],
    roleAssignments = null,
  ) {
    const payload = {
      step_id: stepId,
      variable_filament_id: variableFilamentId,
      fixed_filament_ids: fixedFilamentIds,
      fixed_thicknesses_mm: fixedThicknessesMm,
      notes: notes || undefined,
    };
    if (Array.isArray(roleAssignments)) {
      payload.role_assignments = roleAssignments;
    }
    return api.apiPost("/samples", payload);
  }

  async function fetchNextSampleId() {
    return api.apiFetch("/samples/next-id");
  }

  async function fetchSteps() {
    return api.apiFetch("/steps");
  }

  async function updateSample(sampleId, updates) {
    // updates: { step_id?, variable_filament_id?, fixed_filament_ids?, role_assignments?, notes? }
    return api.apiPut(`/samples/${encodeURIComponent(sampleId)}`, updates);
  }

  async function createFilament(manufacturer, colorName, hex, options = {}) {
    return api.apiPost("/filaments", {
      manufacturer,
      color_name: colorName,
      hex,
      material: options.material || "unknown",
      white_cap_eligible: !!options.whiteCapEligible,
      special_roles: options.specialRoles || [],
      exclude_from_model: !!options.excludeFromModel,
      notes: options.notes || "",
    });
  }

  async function updateFilament(filamentId, updates = {}) {
    return api.apiPatch(
      `/filaments/${encodeURIComponent(filamentId)}`,
      updates,
    );
  }

  async function deleteFilament(filamentId) {
    return api.apiFetch(`/filaments/${encodeURIComponent(filamentId)}`, {
      method: "DELETE",
    });
  }

  async function deleteSample(sampleId) {
    return api.apiDelete(`/samples/${encodeURIComponent(sampleId)}`);
  }

  async function createSampleBatch(
    stepId,
    batchRole,
    batchFilamentIds,
    variableFilamentId = "",
    fixedFilamentIds = [],
    fixedThicknessesMm = [],
    roleAssignments = null,
    notes = "",
  ) {
    const payload = {
      step_id: stepId,
      batch_role: batchRole,
      batch_filament_ids: batchFilamentIds,
      variable_filament_id: variableFilamentId || undefined,
      fixed_filament_ids: fixedFilamentIds,
      fixed_thicknesses_mm: fixedThicknessesMm,
      notes: notes || undefined,
    };
    if (Array.isArray(roleAssignments)) {
      payload.role_assignments = roleAssignments;
    }
    return api.apiPost("/samples/batch", payload);
  }

  async function processSingle(sampleId) {
    return api.apiPost(`/process/single/${encodeURIComponent(sampleId)}`);
  }

  Object.assign(api, {
    assignImage,
    assignBlank,
    flagSample,
    unflagSample,
    rejectSample,
    ignoreImage,
    unignoreImage,
    rotateImage,
    registerBlank,
    unregisterBlank,
    unassignImage,
    excludeSwatch,
    includeSwatch,
    updateSampleFitExclusion,
    updateSampleSwatchFitExclusions,
    createSample,
    fetchNextSampleId,
    fetchSteps,
    updateSample,
    createFilament,
    updateFilament,
    deleteFilament,
    deleteSample,
    createSampleBatch,
    processSingle,
  });
}
