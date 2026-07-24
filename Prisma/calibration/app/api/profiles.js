export function installApiProfiles(api) {
  async function fetchSamplesRaw() {
    return api.apiFetch("/samples");
  }

  async function fetchFilamentsRaw() {
    return api.apiFetch("/filaments");
  }

  async function fetchProfileDetail(filamentId) {
    return api.apiFetch(`/profiles/${encodeURIComponent(filamentId)}`);
  }

  async function fitProfile(filamentId) {
    return api.apiPost(`/profiles/${encodeURIComponent(filamentId)}/fit`);
  }

  async function startFitAllProfilesJob() {
    return api.apiPost("/profiles/fit-all/start");
  }

  async function fetchFitAllProfilesJobStatus(jobId) {
    return api.apiFetch(
      `/profiles/fit-all/status/${encodeURIComponent(jobId)}`,
    );
  }

  async function activateProfile(filamentId) {
    return api.apiPost(`/profiles/${encodeURIComponent(filamentId)}/activate`);
  }

  async function deactivateProfile(filamentId) {
    return api.apiPost(
      `/profiles/${encodeURIComponent(filamentId)}/deactivate`,
    );
  }

  async function fetchModelPublicationReadiness() {
    return api.apiFetch("/models/publication/readiness");
  }

  async function exportCurrentModelLibrary(metadata) {
    return api.apiPost("/models/publication/export", metadata);
  }

  async function installCurrentModelLibrary(metadata) {
    return api.apiPost("/models/publication/install", metadata);
  }

  async function openPublishedModelsFolder() {
    return api.apiPost("/models/publication/open-folder", {});
  }

  async function fetchProfileCurve(filamentId) {
    return api.apiFetch(`/profiles/${encodeURIComponent(filamentId)}/curve`);
  }

  async function fetchProfileSwatches(filamentId) {
    return api.apiFetch(`/profiles/${encodeURIComponent(filamentId)}/swatches`);
  }

  async function fetchProfileErrors(filamentId) {
    return api.apiFetch(`/profiles/${encodeURIComponent(filamentId)}/errors`);
  }

  Object.assign(api, {
    fetchSamplesRaw,
    fetchFilamentsRaw,
    fetchProfileDetail,
    fitProfile,
    startFitAllProfilesJob,
    fetchFitAllProfilesJobStatus,
    activateProfile,
    deactivateProfile,
    fetchModelPublicationReadiness,
    exportCurrentModelLibrary,
    installCurrentModelLibrary,
    openPublishedModelsFolder,
    fetchProfileCurve,
    fetchProfileSwatches,
    fetchProfileErrors,
  });
}
