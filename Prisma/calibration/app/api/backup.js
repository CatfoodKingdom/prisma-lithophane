export function installApiBackup(api) {
  async function createBackupJob(options = {}) {
    return api.apiPost("/backup/create-job", {
      package_type: options.packageType || "working_state",
      include_raw_images: options.includeRawImages !== false,
    });
  }

  async function createRawArchiveJob() {
    return api.apiPost("/raw-archives/create-job", {});
  }

  async function fetchBackupJobStatus(jobId) {
    return api.apiFetch(`/backup/jobs/${encodeURIComponent(jobId)}`);
  }

  function backupDownloadUrl(backupId) {
    return `${api.constants.API_BASE}/backup/download/${encodeURIComponent(backupId)}`;
  }

  async function validateRestoreBackup(file) {
    const formData = new FormData();
    formData.append("file", file);
    const response = await api.fetchImpl(
      `${api.constants.API_BASE}/backup/validate-restore`,
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

  async function validateRestoreBackupPath(path) {
    return api.apiPost("/backup/validate-restore-path", { path });
  }

  async function deleteRestorePreview(restoreToken) {
    if (!restoreToken) return { ok: true, removed: false };
    return api.apiFetch(
      `/backup/restore-preview/${encodeURIComponent(restoreToken)}`,
      {
        method: "DELETE",
      },
    );
  }

  async function validateRawArchive(file) {
    const formData = new FormData();
    formData.append("file", file);
    const response = await api.fetchImpl(
      `${api.constants.API_BASE}/raw-archives/validate`,
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

  async function validateRawArchivePath(path) {
    return api.apiPost("/raw-archives/validate-path", { path });
  }

  async function deleteRawArchivePreview(archiveToken) {
    if (!archiveToken) return { ok: true, removed: false };
    return api.apiFetch(
      `/raw-archives/preview/${encodeURIComponent(archiveToken)}`,
      {
        method: "DELETE",
      },
    );
  }

  async function createRawArchiveImportJob(archiveToken, imageAssetIds = null) {
    return api.apiPost("/raw-archives/import-job", {
      archive_token: archiveToken,
      image_asset_ids: imageAssetIds,
    });
  }

  async function createRawArchiveReleaseJob(
    archiveToken,
    confirmation,
    imageAssetIds = null,
  ) {
    return api.apiPost("/raw-archives/release-job", {
      archive_token: archiveToken,
      confirmation,
      image_asset_ids: imageAssetIds,
    });
  }

  async function createRestoreJob(restoreToken, confirmation) {
    return api.apiPost("/backup/restore-job", {
      restore_token: restoreToken,
      confirmation,
    });
  }

  async function commitSampleAssignmentCsv(previewToken, options = {}) {
    return api.apiPost("/samples/assignment-import/commit", {
      preview_token: previewToken,
      register_unregistered_blanks: !!options.registerUnregisteredBlanks,
    });
  }

  Object.assign(api, {
    createBackupJob,
    createRawArchiveJob,
    fetchBackupJobStatus,
    backupDownloadUrl,
    validateRestoreBackup,
    validateRestoreBackupPath,
    deleteRestorePreview,
    validateRawArchive,
    validateRawArchivePath,
    deleteRawArchivePreview,
    createRawArchiveImportJob,
    createRawArchiveReleaseJob,
    createRestoreJob,
    commitSampleAssignmentCsv,
  });
}
