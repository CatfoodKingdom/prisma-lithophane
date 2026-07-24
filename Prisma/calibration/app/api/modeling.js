export function installApiModeling(api) {
  async function fetchSamplePredictions(filamentId) {
    return api.apiFetch(
      `/fitting/${encodeURIComponent(filamentId)}/sample-predictions`,
    );
  }

  async function fetchPhotoStackJobStatus(jobId) {
    return api.apiFetch(`/photo-stack/status/${encodeURIComponent(jobId)}`);
  }

  async function fetchPhotoStackLatest() {
    return api.apiFetch("/photo-stack/latest");
  }

  async function fetchPhotoStackCandidate(runId) {
    return api.apiFetch(`/photo-stack/candidates/${encodeURIComponent(runId)}`);
  }

  async function fetchPhotoStackSamplePredictions(runId, options = {}) {
    const params = new URLSearchParams();
    if (options.sample_id) params.set("sample_id", options.sample_id);
    if (options.evidence_class && options.evidence_class !== "all") {
      params.set("evidence_class", options.evidence_class);
    }
    if (options.limit != null) params.set("limit", String(options.limit));
    const qs = params.toString();
    return api.apiFetch(
      `/photo-stack/candidates/${encodeURIComponent(runId)}/sample-predictions${qs ? `?${qs}` : ""}`,
    );
  }

  async function fetchCameraTransformJobStatus(jobId) {
    return api.apiFetch(
      `/camera-transform/status/${encodeURIComponent(jobId)}`,
    );
  }

  async function fetchCameraTransformCurrent() {
    return api.apiFetch("/camera-transform/current");
  }

  async function fetchModelsStatus() {
    return api.apiFetch("/models/status");
  }

  async function fetchModelingOverview() {
    return api.apiFetch("/models/review/overview");
  }

  async function fetchModelingSamples(options = {}) {
    const params = new URLSearchParams();
    if (options.filter) params.set("filter", options.filter);
    if (options.filament_id) params.set("filament_id", options.filament_id);
    if (Array.isArray(options.filament_ids)) {
      options.filament_ids.forEach((id) => {
        if (id) params.append("filament_ids", id);
      });
    }
    if (options.sort) params.set("sort", options.sort);
    if (options.sort_dir) params.set("sort_dir", options.sort_dir);
    if (options.offset != null) params.set("offset", String(options.offset));
    if (options.limit != null) params.set("limit", String(options.limit));
    const qs = params.toString();
    return api.apiFetch(`/models/review/samples${qs ? `?${qs}` : ""}`);
  }

  async function fetchModelingSample(sampleId) {
    return api.apiFetch(
      `/models/review/samples/${encodeURIComponent(sampleId)}`,
    );
  }

  async function fetchModelingFilament(filamentId) {
    return api.apiFetch(
      `/models/review/filaments/${encodeURIComponent(filamentId)}`,
    );
  }

  async function fetchModelingFilaments(options = {}) {
    const params = new URLSearchParams();
    if (options.sort) params.set("sort", options.sort);
    if (options.sort_dir) params.set("sort_dir", options.sort_dir);
    if (options.offset != null) params.set("offset", String(options.offset));
    if (options.limit != null) params.set("limit", String(options.limit));
    const qs = params.toString();
    return api.apiFetch(`/models/review/filaments${qs ? `?${qs}` : ""}`);
  }

  Object.assign(api, {
    fetchSamplePredictions,
    fetchPhotoStackJobStatus,
    fetchPhotoStackLatest,
    fetchPhotoStackCandidate,
    fetchPhotoStackSamplePredictions,
    fetchCameraTransformJobStatus,
    fetchCameraTransformCurrent,
    fetchModelsStatus,
    fetchModelingOverview,
    fetchModelingSamples,
    fetchModelingSample,
    fetchModelingFilament,
    fetchModelingFilaments,
  });
}
