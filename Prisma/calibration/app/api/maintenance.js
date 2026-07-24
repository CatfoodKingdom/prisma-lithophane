export function installApiMaintenance(api) {
  async function fetchMaintenanceOperations() {
    return api.apiFetch("/maintenance/operations");
  }

  async function preflightMaintenanceOperation(
    operationId,
    mode = null,
    scope = {},
  ) {
    return api.apiPost("/maintenance/preflight", {
      operation_id: operationId,
      mode,
      scope,
    });
  }

  async function startMaintenanceJob(
    operationId,
    mode = null,
    preflightToken = "",
    scope = {},
    confirmation = "",
  ) {
    const payload = {
      operation_id: operationId,
      mode,
      preflight_token: preflightToken,
      scope,
    };
    if (confirmation) payload.confirmation = confirmation;
    return api.apiPost("/maintenance/jobs", payload);
  }

  async function fetchMaintenanceJobStatus(jobId) {
    return api.apiFetch(`/maintenance/jobs/${encodeURIComponent(jobId)}`);
  }

  async function cancelMaintenanceJob(jobId) {
    return api.apiPost(`/maintenance/jobs/${encodeURIComponent(jobId)}/cancel`);
  }

  async function fetchMaintenanceReports() {
    return api.apiFetch("/maintenance/reports");
  }

  async function clearMaintenanceReports() {
    return api.apiDelete("/maintenance/reports");
  }

  async function startReextractPreflightJob(scope = {}) {
    return api.apiPost("/maintenance/reextract-sample-images/preflight/jobs", {
      scope,
    });
  }

  async function startReextractCandidateSetJob(scope = {}, preflight = null) {
    const payload = { scope };
    if (preflight) payload.preflight = preflight;
    return api.apiPost(
      "/maintenance/reextract-sample-images/candidate-sets/jobs",
      payload,
    );
  }

  async function fetchReextractJobStatus(jobId) {
    return api.apiFetch(
      `/maintenance/reextract-sample-images/jobs/${encodeURIComponent(jobId)}`,
    );
  }

  async function cancelReextractJob(jobId) {
    return api.apiPost(
      `/maintenance/reextract-sample-images/jobs/${encodeURIComponent(jobId)}/cancel`,
      {},
    );
  }

  async function fetchReextractCandidateSets() {
    return api.apiFetch("/maintenance/reextract-sample-images/candidate-sets");
  }

  async function fetchReextractCandidateSet(candidateSetId) {
    return api.apiFetch(
      `/maintenance/reextract-sample-images/candidate-sets/${encodeURIComponent(candidateSetId)}`,
    );
  }

  async function deleteReextractCandidateSet(candidateSetId) {
    return api.apiDelete(
      `/maintenance/reextract-sample-images/candidate-sets/${encodeURIComponent(candidateSetId)}`,
    );
  }

  async function fetchReextractCandidateSamples(candidateSetId) {
    return api.apiFetch(
      `/maintenance/reextract-sample-images/candidate-sets/${encodeURIComponent(candidateSetId)}/samples`,
    );
  }

  async function fetchReextractCandidateSample(candidateSetId, sampleId) {
    return api.apiFetch(
      `/maintenance/reextract-sample-images/candidate-sets/${encodeURIComponent(candidateSetId)}/samples/${encodeURIComponent(sampleId)}`,
    );
  }

  function reextractCandidateArtifactUrl(candidateSetId, sampleId, kind) {
    return `/api/maintenance/reextract-sample-images/candidate-sets/${encodeURIComponent(candidateSetId)}/artifacts/${encodeURIComponent(sampleId)}/${encodeURIComponent(kind)}`;
  }

  async function setReextractCandidateDecision(
    candidateSetId,
    sampleId,
    decision,
    note = "",
  ) {
    return api.apiPost(
      `/maintenance/reextract-sample-images/candidate-sets/${encodeURIComponent(candidateSetId)}/samples/${encodeURIComponent(sampleId)}/review`,
      {
        decision,
        note,
      },
    );
  }

  async function setReextractCandidateDecisionBulk(
    candidateSetId,
    decision,
    sampleIds = null,
    note = "",
  ) {
    const payload = { decision, note };
    if (Array.isArray(sampleIds)) payload.sample_ids = sampleIds;
    return api.apiPost(
      `/maintenance/reextract-sample-images/candidate-sets/${encodeURIComponent(candidateSetId)}/review`,
      payload,
    );
  }

  async function startRetryReextractCandidateJob(candidateSetId, sampleId) {
    return api.apiPost(
      `/maintenance/reextract-sample-images/candidate-sets/${encodeURIComponent(candidateSetId)}/samples/${encodeURIComponent(sampleId)}/retry/jobs`,
      {},
    );
  }

  async function startManualReextractCandidateJob(
    candidateSetId,
    sampleId,
    payload,
  ) {
    return api.apiPost(
      `/maintenance/reextract-sample-images/candidate-sets/${encodeURIComponent(candidateSetId)}/samples/${encodeURIComponent(sampleId)}/manual-corners/jobs`,
      payload,
    );
  }

  async function startApplyReextractCandidateSetJob(
    candidateSetId,
    acceptedSampleIds = null,
  ) {
    const payload = {};
    if (Array.isArray(acceptedSampleIds))
      payload.accepted_sample_ids = acceptedSampleIds;
    return api.apiPost(
      `/maintenance/reextract-sample-images/candidate-sets/${encodeURIComponent(candidateSetId)}/apply/jobs`,
      payload,
    );
  }

  async function fetchSqliteRestorePointStatus() {
    return api.apiFetch("/system/sqlite-restore-points/status");
  }

  async function restoreSqliteRestorePoint(restorePointPath, confirmation) {
    return api.apiPost("/system/sqlite-restore-points/restore", {
      restore_point_path: restorePointPath,
      confirmation,
    });
  }

  async function fetchSampleDetail(sampleId) {
    return api.apiFetch(`/samples/${encodeURIComponent(sampleId)}`);
  }

  Object.assign(api, {
    fetchMaintenanceOperations,
    preflightMaintenanceOperation,
    startMaintenanceJob,
    fetchMaintenanceJobStatus,
    cancelMaintenanceJob,
    fetchMaintenanceReports,
    clearMaintenanceReports,
    startReextractPreflightJob,
    startReextractCandidateSetJob,
    fetchReextractJobStatus,
    cancelReextractJob,
    fetchReextractCandidateSets,
    fetchReextractCandidateSet,
    deleteReextractCandidateSet,
    fetchReextractCandidateSamples,
    fetchReextractCandidateSample,
    reextractCandidateArtifactUrl,
    setReextractCandidateDecision,
    setReextractCandidateDecisionBulk,
    startRetryReextractCandidateJob,
    startManualReextractCandidateJob,
    startApplyReextractCandidateSetJob,
    fetchSqliteRestorePointStatus,
    restoreSqliteRestorePoint,
    fetchSampleDetail,
  });
}
