export function installApiImages(api) {
  async function fetchBlanks() {
    return api.apiFetch("/blanks");
  }

  async function fetchImages() {
    return api.apiFetch("/images");
  }

  async function openImageInboxFolder() {
    return api.apiPost("/images/open-inbox", {});
  }

  async function startImportInboxImagesJob() {
    return api.apiPost("/images/import-inbox/start");
  }

  async function fetchImportInboxImagesJobStatus(jobId) {
    return api.apiFetch(
      `/images/import-inbox/status/${encodeURIComponent(jobId)}`,
    );
  }

  async function cancelImportInboxImagesJob(jobId) {
    return api.apiPost(
      `/images/import-inbox/cancel/${encodeURIComponent(jobId)}`,
    );
  }

  async function cleanupUnusedImages() {
    return api.apiPost("/images/cleanup-unused");
  }

  function sampleAssignmentTemplateUrl() {
    return `${api.constants.API_BASE}/samples/assignment-template.csv`;
  }

  async function validateSampleAssignmentCsv(file) {
    const formData = new FormData();
    formData.append("file", file);
    const response = await api.fetchImpl(
      `${api.constants.API_BASE}/samples/assignment-import/validate`,
      {
        method: "POST",
        body: formData,
      },
    );
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      const detail = body.detail || body.error || response.statusText;
      const msg =
        typeof detail === "string"
          ? detail
          : detail.message || JSON.stringify(detail);
      const error = new Error(`API ${response.status}: ${msg}`);
      error.status = response.status;
      error.detail = detail;
      error.body = body;
      throw error;
    }
    return response.json();
  }

  async function fetchImageOverrides() {
    return api.apiFetch("/images/overrides");
  }

  Object.assign(api, {
    fetchBlanks,
    fetchImages,
    openImageInboxFolder,
    startImportInboxImagesJob,
    fetchImportInboxImagesJobStatus,
    cancelImportInboxImagesJob,
    cleanupUnusedImages,
    sampleAssignmentTemplateUrl,
    validateSampleAssignmentCsv,
    fetchImageOverrides,
  });
}
