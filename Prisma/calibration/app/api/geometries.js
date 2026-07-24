export function installApiGeometries(api) {
  async function fetchBundles() {
    return api.apiFetch("/bundles");
  }

  async function fetchGeometryBundle(bundleId) {
    return api.apiFetch(`/geometry-bundles/${encodeURIComponent(bundleId)}`);
  }

  async function saveGeometryBundleMapping(bundleId, payload) {
    return api.apiPut(
      `/geometry-bundles/${encodeURIComponent(bundleId)}/mapping`,
      payload,
    );
  }

  async function createSamplesFromGeometryBundle(payload) {
    return api.apiPost("/samples/from-geometry-bundle", payload);
  }

  async function createBundle(name, stepIds = []) {
    return api.apiPost("/bundles", { name, step_ids: stepIds });
  }

  async function updateBundle(name, updates = {}) {
    // updates: { new_name?, step_ids? }
    return api.apiPatch(`/bundles/${encodeURIComponent(name)}`, updates);
  }

  async function deleteBundle(name) {
    return api.apiDelete(`/bundles/${encodeURIComponent(name)}`);
  }

  async function addStepToBundle(name, stepId) {
    return api.apiPost(`/bundles/${encodeURIComponent(name)}/add-step`, {
      step_id: stepId,
    });
  }

  async function removeStepFromBundle(name, stepId) {
    return api.apiPost(`/bundles/${encodeURIComponent(name)}/remove-step`, {
      step_id: stepId,
    });
  }

  async function generateStepFile(
    variableThicknesses,
    fixedThicknesses,
    layerHeight,
    filename,
  ) {
    return api.apiPost("/steps/generate", {
      variable_thicknesses: variableThicknesses,
      fixed_thicknesses: fixedThicknesses,
      layer_height: layerHeight,
      filename: filename,
    });
  }

  async function createGeometry(payload) {
    return api.apiPost("/geometries", payload);
  }

  async function generateGeometryArtifacts(geometryId, options = {}) {
    return api.apiPost(
      `/geometries/${encodeURIComponent(geometryId)}/artifacts`,
      options,
    );
  }

  async function deleteGeometry(geometryId) {
    return api.apiDelete(`/geometries/${encodeURIComponent(geometryId)}`);
  }

  async function updateStepMetadata(filename, alias, bundle) {
    return api.apiPut(`/steps/${encodeURIComponent(filename)}/metadata`, {
      alias,
      bundle,
    });
  }

  async function fetchConfig() {
    return api.apiFetch("/config");
  }

  Object.assign(api, {
    fetchBundles,
    fetchGeometryBundle,
    saveGeometryBundleMapping,
    createSamplesFromGeometryBundle,
    createBundle,
    updateBundle,
    deleteBundle,
    addStepToBundle,
    removeStepFromBundle,
    generateStepFile,
    createGeometry,
    generateGeometryArtifacts,
    deleteGeometry,
    updateStepMetadata,
    fetchConfig,
  });
}
