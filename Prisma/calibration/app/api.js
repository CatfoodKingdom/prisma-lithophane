// api.js — API client for unified calibration backend
//
// Fetches data from the FastAPI backend at /api/* and transforms
// it into the compatibility shape that app.js expects.

const API_BASE = '/api';

// ── Low-level fetch helpers ──────────────────────────────────────────────────

async function apiFetch(path, options = {}) {
  const url = `${API_BASE}${path}`;
  const response = await fetch(url, {
    headers: { 'Content-Type': 'application/json', ...options.headers },
    ...options,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const detail = body.detail || body.error || response.statusText;
    const msg = typeof detail === "string" ? detail : (detail.message || JSON.stringify(detail));
    const error = new Error(`API ${response.status}: ${msg}`);
    error.status = response.status;
    error.detail = detail;
    error.body = body;
    throw error;
  }
  return response.json();
}

async function apiPost(path, body = {}) {
  return apiFetch(path, {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

async function apiPut(path, body = {}) {
  return apiFetch(path, {
    method: 'PUT',
    body: JSON.stringify(body),
  });
}

async function apiPatch(path, body = {}) {
  return apiFetch(path, {
    method: 'PATCH',
    body: JSON.stringify(body),
  });
}

async function apiDelete(path) {
  return apiFetch(path, { method: 'DELETE' });
}

// ── Raw API calls ────────────────────────────────────────────────────────────

async function fetchSamplesRaw() {
  return apiFetch('/samples');
}

async function fetchFilamentsRaw() {
  return apiFetch('/filaments');
}

async function fetchProfilesList() {
  return apiFetch('/profiles');
}

async function fetchProfileDetail(filamentId) {
  return apiFetch(`/profiles/${encodeURIComponent(filamentId)}`);
}

async function fitProfile(filamentId) {
  return apiPost(`/profiles/${encodeURIComponent(filamentId)}/fit`);
}

async function fitAllProfiles() {
  return apiPost('/profiles/fit-all');
}

async function startFitAllProfilesJob() {
  return apiPost('/profiles/fit-all/start');
}

async function fetchFitAllProfilesJobStatus(jobId) {
  return apiFetch(`/profiles/fit-all/status/${encodeURIComponent(jobId)}`);
}

async function activateProfile(filamentId) {
  return apiPost(`/profiles/${encodeURIComponent(filamentId)}/activate`);
}

async function deactivateProfile(filamentId) {
  return apiPost(`/profiles/${encodeURIComponent(filamentId)}/deactivate`);
}

async function fetchProfileData(filamentId) {
  return apiFetch(`/profiles/${encodeURIComponent(filamentId)}/data`);
}

async function fetchModelPublicationReadiness() {
  return apiFetch('/models/publication/readiness');
}

async function exportCurrentModelLibrary(metadata) {
  return apiPost('/models/publication/export', metadata);
}

async function installCurrentModelLibrary(metadata) {
  return apiPost('/models/publication/install', metadata);
}

async function openPublishedModelsFolder() {
  return apiPost('/models/publication/open-folder', {});
}

async function fetchProfileCurve(filamentId) {
  return apiFetch(`/profiles/${encodeURIComponent(filamentId)}/curve`);
}

async function fetchProfileSwatches(filamentId) {
  return apiFetch(`/profiles/${encodeURIComponent(filamentId)}/swatches`);
}

async function fetchProfileErrors(filamentId) {
  return apiFetch(`/profiles/${encodeURIComponent(filamentId)}/errors`);
}

async function fetchProfilesAudit() {
  return apiFetch('/profiles/audit');
}

async function fetchBlanks() {
  return apiFetch('/blanks');
}

async function fetchImages() {
  return apiFetch('/images');
}

async function openImageInboxFolder() {
  return apiPost('/images/open-inbox', {});
}

async function importInboxImages() {
  return apiPost('/images/import-inbox');
}

async function startImportInboxImagesJob() {
  return apiPost('/images/import-inbox/start');
}

async function fetchImportInboxImagesJobStatus(jobId) {
  return apiFetch(`/images/import-inbox/status/${encodeURIComponent(jobId)}`);
}

async function cancelImportInboxImagesJob(jobId) {
  return apiPost(`/images/import-inbox/cancel/${encodeURIComponent(jobId)}`);
}

async function cleanupUnusedImages() {
  return apiPost('/images/cleanup-unused');
}

function sampleAssignmentTemplateUrl() {
  return `${API_BASE}/samples/assignment-template.csv`;
}

async function validateSampleAssignmentCsv(file) {
  const formData = new FormData();
  formData.append('file', file);
  const response = await fetch(`${API_BASE}/samples/assignment-import/validate`, {
    method: 'POST',
    body: formData,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const detail = body.detail || body.error || response.statusText;
    const msg = typeof detail === "string" ? detail : (detail.message || JSON.stringify(detail));
    const error = new Error(`API ${response.status}: ${msg}`);
    error.status = response.status;
    error.detail = detail;
    error.body = body;
    throw error;
  }
  return response.json();
}

async function createBackup(options = {}) {
  return apiPost('/backup/create', {
    package_type: options.packageType || 'working_state',
    include_raw_images: options.includeRawImages !== false,
  });
}

async function createBackupJob(options = {}) {
  return apiPost('/backup/create-job', {
    package_type: options.packageType || 'working_state',
    include_raw_images: options.includeRawImages !== false,
  });
}

async function createRawArchiveJob() {
  return apiPost('/raw-archives/create-job', {});
}

async function fetchBackupJobStatus(jobId) {
  return apiFetch(`/backup/jobs/${encodeURIComponent(jobId)}`);
}

function backupDownloadUrl(backupId) {
  return `${API_BASE}/backup/download/${encodeURIComponent(backupId)}`;
}

async function validateRestoreBackup(file) {
  const formData = new FormData();
  formData.append('file', file);
  const response = await fetch(`${API_BASE}/backup/validate-restore`, {
    method: 'POST',
    body: formData,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const detail = body.detail || body.error || response.statusText;
    const msg = typeof detail === "string" ? detail : (detail.message || JSON.stringify(detail));
    const error = new Error(`API ${response.status}: ${msg}`);
    error.status = response.status;
    error.detail = detail;
    error.body = body;
    throw error;
  }
  return response.json();
}

async function validateRestoreBackupPath(path) {
  return apiPost('/backup/validate-restore-path', { path });
}

async function deleteRestorePreview(restoreToken) {
  if (!restoreToken) return { ok: true, removed: false };
  return apiFetch(`/backup/restore-preview/${encodeURIComponent(restoreToken)}`, {
    method: 'DELETE',
  });
}

async function validateRawArchive(file) {
  const formData = new FormData();
  formData.append('file', file);
  const response = await fetch(`${API_BASE}/raw-archives/validate`, {
    method: 'POST',
    body: formData,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const detail = body.detail || body.error || response.statusText;
    const msg = typeof detail === "string" ? detail : (detail.message || JSON.stringify(detail));
    const error = new Error(`API ${response.status}: ${msg}`);
    error.status = response.status;
    error.detail = detail;
    error.body = body;
    throw error;
  }
  return response.json();
}

async function validateRawArchivePath(path) {
  return apiPost('/raw-archives/validate-path', { path });
}

async function deleteRawArchivePreview(archiveToken) {
  if (!archiveToken) return { ok: true, removed: false };
  return apiFetch(`/raw-archives/preview/${encodeURIComponent(archiveToken)}`, {
    method: 'DELETE',
  });
}

async function createRawArchiveImportJob(archiveToken, imageAssetIds = null) {
  return apiPost('/raw-archives/import-job', {
    archive_token: archiveToken,
    image_asset_ids: imageAssetIds,
  });
}

async function createRawArchiveReleaseJob(archiveToken, confirmation, imageAssetIds = null) {
  return apiPost('/raw-archives/release-job', {
    archive_token: archiveToken,
    confirmation,
    image_asset_ids: imageAssetIds,
  });
}

async function createRestoreJob(restoreToken, confirmation) {
  return apiPost('/backup/restore-job', {
    restore_token: restoreToken,
    confirmation,
  });
}

async function commitSampleAssignmentCsv(previewToken, options = {}) {
  return apiPost('/samples/assignment-import/commit', {
    preview_token: previewToken,
    register_unregistered_blanks: !!options.registerUnregisteredBlanks,
  });
}

async function fetchImageOverrides() {
  return apiFetch('/images/overrides');
}

async function fetchProcessingSummary() {
  return apiFetch('/process/summary');
}

async function fetchMaintenanceOperations() {
  return apiFetch('/maintenance/operations');
}

async function preflightMaintenanceOperation(operationId, mode = null, scope = {}) {
  return apiPost('/maintenance/preflight', {
    operation_id: operationId,
    mode,
    scope,
  });
}

async function startMaintenanceJob(operationId, mode = null, preflightToken = "", scope = {}, confirmation = "") {
  const payload = {
    operation_id: operationId,
    mode,
    preflight_token: preflightToken,
    scope,
  };
  if (confirmation) payload.confirmation = confirmation;
  return apiPost('/maintenance/jobs', payload);
}

async function fetchMaintenanceJobStatus(jobId) {
  return apiFetch(`/maintenance/jobs/${encodeURIComponent(jobId)}`);
}

async function cancelMaintenanceJob(jobId) {
  return apiPost(`/maintenance/jobs/${encodeURIComponent(jobId)}/cancel`);
}

async function fetchMaintenanceReports() {
  return apiFetch('/maintenance/reports');
}

async function clearMaintenanceReports() {
  return apiDelete('/maintenance/reports');
}

async function fetchMaintenanceReport(reportId) {
  return apiFetch(`/maintenance/reports/${encodeURIComponent(reportId)}`);
}

async function preflightReextractSampleImages(scope = {}) {
  return apiPost('/maintenance/reextract-sample-images/preflight', { scope });
}

async function startReextractPreflightJob(scope = {}) {
  return apiPost('/maintenance/reextract-sample-images/preflight/jobs', { scope });
}

async function startReextractCandidateSetJob(scope = {}, preflight = null) {
  const payload = { scope };
  if (preflight) payload.preflight = preflight;
  return apiPost('/maintenance/reextract-sample-images/candidate-sets/jobs', payload);
}

async function fetchReextractJobStatus(jobId) {
  return apiFetch(`/maintenance/reextract-sample-images/jobs/${encodeURIComponent(jobId)}`);
}

async function cancelReextractJob(jobId) {
  return apiPost(`/maintenance/reextract-sample-images/jobs/${encodeURIComponent(jobId)}/cancel`, {});
}

async function createReextractCandidateSet(scope = {}) {
  return apiPost('/maintenance/reextract-sample-images/candidate-sets', { scope });
}

async function fetchReextractCandidateSets() {
  return apiFetch('/maintenance/reextract-sample-images/candidate-sets');
}

async function fetchReextractCandidateSet(candidateSetId) {
  return apiFetch(`/maintenance/reextract-sample-images/candidate-sets/${encodeURIComponent(candidateSetId)}`);
}

async function deleteReextractCandidateSet(candidateSetId) {
  return apiDelete(`/maintenance/reextract-sample-images/candidate-sets/${encodeURIComponent(candidateSetId)}`);
}

async function fetchReextractCandidateSamples(candidateSetId) {
  return apiFetch(`/maintenance/reextract-sample-images/candidate-sets/${encodeURIComponent(candidateSetId)}/samples`);
}

async function fetchReextractCandidateSample(candidateSetId, sampleId) {
  return apiFetch(`/maintenance/reextract-sample-images/candidate-sets/${encodeURIComponent(candidateSetId)}/samples/${encodeURIComponent(sampleId)}`);
}

function reextractCandidateArtifactUrl(candidateSetId, sampleId, kind) {
  return `/api/maintenance/reextract-sample-images/candidate-sets/${encodeURIComponent(candidateSetId)}/artifacts/${encodeURIComponent(sampleId)}/${encodeURIComponent(kind)}`;
}

async function setReextractCandidateDecision(candidateSetId, sampleId, decision, note = '') {
  return apiPost(`/maintenance/reextract-sample-images/candidate-sets/${encodeURIComponent(candidateSetId)}/samples/${encodeURIComponent(sampleId)}/review`, {
    decision,
    note,
  });
}

async function setReextractCandidateDecisionBulk(candidateSetId, decision, sampleIds = null, note = '') {
  const payload = { decision, note };
  if (Array.isArray(sampleIds)) payload.sample_ids = sampleIds;
  return apiPost(`/maintenance/reextract-sample-images/candidate-sets/${encodeURIComponent(candidateSetId)}/review`, payload);
}

async function retryReextractCandidate(candidateSetId, sampleId) {
  return apiPost(`/maintenance/reextract-sample-images/candidate-sets/${encodeURIComponent(candidateSetId)}/samples/${encodeURIComponent(sampleId)}/retry`, {});
}

async function startRetryReextractCandidateJob(candidateSetId, sampleId) {
  return apiPost(`/maintenance/reextract-sample-images/candidate-sets/${encodeURIComponent(candidateSetId)}/samples/${encodeURIComponent(sampleId)}/retry/jobs`, {});
}

async function createManualReextractCandidate(candidateSetId, sampleId, payload) {
  return apiPost(`/maintenance/reextract-sample-images/candidate-sets/${encodeURIComponent(candidateSetId)}/samples/${encodeURIComponent(sampleId)}/manual-corners`, payload);
}

async function startManualReextractCandidateJob(candidateSetId, sampleId, payload) {
  return apiPost(`/maintenance/reextract-sample-images/candidate-sets/${encodeURIComponent(candidateSetId)}/samples/${encodeURIComponent(sampleId)}/manual-corners/jobs`, payload);
}

async function applyReextractCandidateSet(candidateSetId) {
  return apiPost(`/maintenance/reextract-sample-images/candidate-sets/${encodeURIComponent(candidateSetId)}/apply`, {});
}

async function startApplyReextractCandidateSetJob(candidateSetId, acceptedSampleIds = null) {
  const payload = {};
  if (Array.isArray(acceptedSampleIds)) payload.accepted_sample_ids = acceptedSampleIds;
  return apiPost(`/maintenance/reextract-sample-images/candidate-sets/${encodeURIComponent(candidateSetId)}/apply/jobs`, payload);
}

async function fetchSqliteRestorePointStatus() {
  return apiFetch('/system/sqlite-restore-points/status');
}

async function restoreSqliteRestorePoint(restorePointPath, confirmation) {
  return apiPost('/system/sqlite-restore-points/restore', {
    restore_point_path: restorePointPath,
    confirmation,
  });
}

async function fetchSampleDetail(sampleId) {
  return apiFetch(`/samples/${encodeURIComponent(sampleId)}`);
}

// ── Mutation API calls ───────────────────────────────────────────────────────

async function assignImage(sampleId, filename, orientationRots) {
  const body = { filename };
  if (orientationRots != null) body.orientation_rots = orientationRots;
  return apiPost(`/samples/${encodeURIComponent(sampleId)}/assign-image`, body);
}

async function assignBlank(sampleId, blankId) {
  return apiPost(`/samples/${encodeURIComponent(sampleId)}/assign-blank`, { blank_id: blankId });
}

async function swapImages(sampleIdA, sampleIdB) {
  return apiPost('/samples/swap-images', { sample_id_a: sampleIdA, sample_id_b: sampleIdB });
}

async function flagSample(sampleId, reason) {
  return apiPost(`/samples/${encodeURIComponent(sampleId)}/flag`, { reason });
}

async function unflagSample(sampleId) {
  return apiPost(`/samples/${encodeURIComponent(sampleId)}/unflag`);
}

async function rejectSample(sampleId) {
  return apiPost(`/samples/${encodeURIComponent(sampleId)}/reject`);
}

async function ignoreImage(filename) {
  return apiPost(`/images/${encodeURIComponent(filename)}/ignore`);
}

async function unignoreImage(filename) {
  return apiPost(`/images/${encodeURIComponent(filename)}/unignore`);
}

async function rotateImage(filename, rotationCw) {
  return apiPost(`/images/${encodeURIComponent(filename)}/rotation`, {
    rotation_cw: rotationCw,
  });
}

async function registerBlank(filename, sessionTag) {
  const body = { filename };
  if (sessionTag) body.session_tag = sessionTag;
  return apiPost('/blanks/register', body);
}

async function unregisterBlank(blankId) {
  return apiFetch(`/blanks/${encodeURIComponent(blankId)}`, { method: 'DELETE' });
}

async function unassignImage(sampleId) {
  return apiPost(`/samples/${encodeURIComponent(sampleId)}/assign-image`, { filename: null });
}

async function excludeSwatch(sampleId, swatchIndex, reason) {
  return apiPost(`/samples/${encodeURIComponent(sampleId)}/exclude-swatch`, {
    swatch_index: swatchIndex,
    reason: reason || '',
  });
}

async function includeSwatch(sampleId, swatchIndex) {
  return apiPost(`/samples/${encodeURIComponent(sampleId)}/include-swatch`, {
    swatch_index: swatchIndex,
  });
}

async function updateSampleFitExclusion(sampleId, updates) {
  return apiPatch(`/samples/${encodeURIComponent(sampleId)}/fit-exclusion`, updates);
}

async function updateSampleSwatchFitExclusions(sampleId, excludedSwatches = []) {
  return updateSampleFitExclusion(sampleId, { excluded_swatches: excludedSwatches });
}

async function createSample(stepId, variableFilamentId, fixedFilamentIds = [], notes = '', fixedThicknessesMm = [], roleAssignments = null) {
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
  return apiPost('/samples', payload);
}

async function fetchNextSampleId() {
  return apiFetch('/samples/next-id');
}

async function fetchSteps() {
  return apiFetch('/steps');
}

async function updateSample(sampleId, updates) {
  // updates: { step_id?, variable_filament_id?, fixed_filament_ids?, role_assignments?, notes? }
  return apiPut(`/samples/${encodeURIComponent(sampleId)}`, updates);
}

async function createFilament(manufacturer, colorName, hex, options = {}) {
  return apiPost('/filaments', {
    manufacturer,
    color_name: colorName,
    hex,
    material: options.material || 'unknown',
    white_cap_eligible: !!options.whiteCapEligible,
    special_roles: options.specialRoles || [],
    exclude_from_model: !!options.excludeFromModel,
    notes: options.notes || '',
  });
}

async function updateFilament(filamentId, updates = {}) {
  return apiPatch(`/filaments/${encodeURIComponent(filamentId)}`, updates);
}

async function deleteFilament(filamentId) {
  return apiFetch(`/filaments/${encodeURIComponent(filamentId)}`, { method: 'DELETE' });
}

async function deleteSample(sampleId) {
  return apiDelete(`/samples/${encodeURIComponent(sampleId)}`);
}

async function createSampleBundle(variableFilamentId, stepIds, fixedFilamentIds = [], notes = '', roleAssignmentsByStep = null) {
  const payload = {
    variable_filament_id: variableFilamentId,
    step_ids: stepIds,
    fixed_filament_ids: fixedFilamentIds,
    notes: notes || undefined,
  };
  if (roleAssignmentsByStep && typeof roleAssignmentsByStep === 'object') {
    payload.role_assignments_by_step = roleAssignmentsByStep;
  }
  return apiPost('/samples/from-bundle', payload);
}

async function createSampleBatch(stepId, batchRole, batchFilamentIds, variableFilamentId = '', fixedFilamentIds = [], fixedThicknessesMm = [], roleAssignments = null, notes = '') {
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
  return apiPost('/samples/batch', payload);
}

async function processBatch() {
  return apiPost('/process/batch');
}

async function processSingle(sampleId) {
  return apiPost(`/process/single/${encodeURIComponent(sampleId)}`);
}

// ── Bundle API calls ────────────────────────────────────────────────────────

async function fetchBundles() {
  return apiFetch('/bundles');
}

async function fetchGeometryBundle(bundleId) {
  return apiFetch(`/geometry-bundles/${encodeURIComponent(bundleId)}`);
}

async function saveGeometryBundleMapping(bundleId, payload) {
  return apiPut(`/geometry-bundles/${encodeURIComponent(bundleId)}/mapping`, payload);
}

async function createSamplesFromGeometryBundle(payload) {
  return apiPost('/samples/from-geometry-bundle', payload);
}

async function createBundle(name, stepIds = []) {
  return apiPost('/bundles', { name, step_ids: stepIds });
}

async function updateBundle(name, updates = {}) {
  // updates: { new_name?, step_ids? }
  return apiPatch(`/bundles/${encodeURIComponent(name)}`, updates);
}

async function deleteBundle(name) {
  return apiDelete(`/bundles/${encodeURIComponent(name)}`);
}

async function addStepToBundle(name, stepId) {
  return apiPost(`/bundles/${encodeURIComponent(name)}/add-step`, { step_id: stepId });
}

async function removeStepFromBundle(name, stepId) {
  return apiPost(`/bundles/${encodeURIComponent(name)}/remove-step`, { step_id: stepId });
}

async function generateStepFile(variableThicknesses, fixedThicknesses, layerHeight, filename) {
  return apiPost('/steps/generate', {
    variable_thicknesses: variableThicknesses,
    fixed_thicknesses: fixedThicknesses,
    layer_height: layerHeight,
    filename: filename,
  });
}

async function createGeometry(payload) {
  return apiPost('/geometries', payload);
}

async function generateGeometryArtifacts(geometryId, options = {}) {
  return apiPost(`/geometries/${encodeURIComponent(geometryId)}/artifacts`, options);
}

async function deleteGeometry(geometryId) {
  return apiDelete(`/geometries/${encodeURIComponent(geometryId)}`);
}

async function ensureStepArtifact(stepId) {
  return apiPost(`/steps/${encodeURIComponent(stepId)}/ensure-artifact`);
}

async function updateStepMetadata(filename, alias, bundle) {
  return apiPut(`/steps/${encodeURIComponent(filename)}/metadata`, { alias, bundle });
}

async function fetchConfig() {
  return apiFetch('/config');
}

// ── Model Fitting API ────────────────────────────────────────────────────────

async function fetchSamplePredictions(filamentId) {
  return apiFetch(`/fitting/${encodeURIComponent(filamentId)}/sample-predictions`);
}

async function startPhotoStackFitJob() {
  return apiPost('/photo-stack/start');
}

async function fetchPhotoStackJobStatus(jobId) {
  return apiFetch(`/photo-stack/status/${encodeURIComponent(jobId)}`);
}

async function fetchPhotoStackLatest() {
  return apiFetch('/photo-stack/latest');
}

async function fetchPhotoStackCandidate(runId) {
  return apiFetch(`/photo-stack/candidates/${encodeURIComponent(runId)}`);
}

async function fetchPhotoStackSamplePredictions(runId, options = {}) {
  const params = new URLSearchParams();
  if (options.sample_id) params.set('sample_id', options.sample_id);
  if (options.evidence_class && options.evidence_class !== 'all') {
    params.set('evidence_class', options.evidence_class);
  }
  if (options.limit != null) params.set('limit', String(options.limit));
  const qs = params.toString();
  return apiFetch(`/photo-stack/candidates/${encodeURIComponent(runId)}/sample-predictions${qs ? `?${qs}` : ''}`);
}

async function startCameraTransformBuildJob() {
  return apiPost('/camera-transform/build');
}

async function fetchCameraTransformJobStatus(jobId) {
  return apiFetch(`/camera-transform/status/${encodeURIComponent(jobId)}`);
}

async function fetchCameraTransformCurrent() {
  return apiFetch('/camera-transform/current');
}

async function fetchModelsStatus() {
  return apiFetch('/models/status');
}

async function fetchModelingOverview() {
  return apiFetch('/models/review/overview');
}

async function fetchModelingSamples(options = {}) {
  const params = new URLSearchParams();
  if (options.filter) params.set('filter', options.filter);
  if (options.filament_id) params.set('filament_id', options.filament_id);
  if (Array.isArray(options.filament_ids)) {
    options.filament_ids.forEach((id) => {
      if (id) params.append('filament_ids', id);
    });
  }
  if (options.sort) params.set('sort', options.sort);
  if (options.sort_dir) params.set('sort_dir', options.sort_dir);
  if (options.offset != null) params.set('offset', String(options.offset));
  if (options.limit != null) params.set('limit', String(options.limit));
  const qs = params.toString();
  return apiFetch(`/models/review/samples${qs ? `?${qs}` : ''}`);
}

async function fetchModelingSample(sampleId) {
  return apiFetch(`/models/review/samples/${encodeURIComponent(sampleId)}`);
}

async function fetchModelingFilament(filamentId) {
  return apiFetch(`/models/review/filaments/${encodeURIComponent(filamentId)}`);
}

async function fetchModelingFilaments(options = {}) {
  const params = new URLSearchParams();
  if (options.sort) params.set('sort', options.sort);
  if (options.sort_dir) params.set('sort_dir', options.sort_dir);
  if (options.offset != null) params.set('offset', String(options.offset));
  if (options.limit != null) params.set('limit', String(options.limit));
  const qs = params.toString();
  return apiFetch(`/models/review/filaments${qs ? `?${qs}` : ''}`);
}

// ── Transform API responses into the app.js compatibility shape ──────────────

function buildFilamentLookup(filaments) {
  const map = {};
  for (const f of filaments) {
    map[f.filament_id] = f;
  }
  return map;
}

function transformSampleToData(sample, filamentMap) {
  // The API returns a Sample object; transforms it into the shape app.js expects.
  const varFilId = sample.filaments?.variable || '';
  const fixedFilIds = sample.filaments?.fixed || [];
  const varFil = filamentMap[varFilId] || {};
  const stripDef = sample.strip_definition || {};

  const photos = sample.photos || [];
  const photoCount = photos.length;
  const photoName = photos[0] || null;

  // Determine source_image — prefer assigned_image, fall back to photo name
  const sourceImage = sample.assigned_image || photoName || null;
  const blankImage = sample.blank_image || null;

  if (sample.has_measurements == null) {
    throw new Error(`Sample ${sample.sample_id || '(unknown)'} is missing has_measurements in /api/samples payload`);
  }
  // A processed sample may still be awaiting review. In that state the slim
  // list can report has_measurements=false even though extraction output is
  // available from the detail endpoint.
  const workflowStatus = String(sample.processing_status || '').toLowerCase();
  const processed = !!sample.has_measurements || workflowStatus === 'processed' || workflowStatus === 'flagged';

  // Build variable thicknesses from strip_definition
  const variableThicknesses = stripDef.variable_thicknesses_mm || [];
  const fixedThicknesses = stripDef.fixed_thicknesses_mm || [];

  // Build range label from variable thicknesses
  let rangeLabel = '';
  if (variableThicknesses.length >= 2) {
    const first = variableThicknesses[0];
    const last = variableThicknesses[variableThicknesses.length - 1];
    rangeLabel = `${Number(first).toFixed(2)}-${Number(last).toFixed(2)}`;
  }

  return {
    sample_id: sample.sample_id,
    created: sample.created || '',
    name: sample.name || '',
    notes: sample.notes || '',
    roles: sample.roles || [],
    variable_filament_id: varFilId,
    variable_color_name: varFil.color_name || varFilId,
    variable_hex: varFil.hex || '#dddddd',
    manufacturer: varFil.manufacturer || '',
    fixed_filament_ids: fixedFilIds,
    n_layers: stripDef.n_layers || 1,
    layer_height_mm: stripDef.layer_height_mm || 0.1,
    mode: stripDef.mode || 'linear',
    range_label: rangeLabel,
    photo_count: photoCount,
    photo_name: photoName,
    step_id: sample.step_id || '',
    blank_image: blankImage,
    step_file: sample.step_file || '',
    processed: processed,
    strip_count: processed ? 1 : 0,
    source_image: sourceImage,
    variable_thicknesses_mm: variableThicknesses,
    fixed_thicknesses_mm: fixedThicknesses,
    // Preserve API-specific fields for future use:
    _processing_status: sample.processing_status,
    _assigned_image: sample.assigned_image,
    _assigned_blank_id: sample.assigned_blank_id,
    _orientation_rots: sample.orientation_rots != null ? sample.orientation_rots : null,
    _flag_reason: sample.flag_reason,
    // Per-swatch color is NOT in the slim list response; it is hydrated lazily
    // from GET /api/samples/{id}. Null here on the API path; the static snapshot
    // path still populates it inline.
    _measurements: sample.measurements || null,
    // Measurement summary from the slim list (drives counts + excluded badges
    // without the per-swatch payload).
    _n_swatches: sample.n_swatches != null
      ? sample.n_swatches
      : (sample.measurements?.swatches?.length || 0),
    _n_excluded: sample.n_excluded != null ? sample.n_excluded : 0,
    _review_accepted: sample.review_accepted || false,
    _fit_exclude: sample.fit_exclude || false,
    _excluded_swatches: sample.excluded_swatches || [],
  };
}

function transformStepToData(step) {
  return {
    ...step,
    step_id: step.step_id,
    file_name: step.file_name || step.artifact_filename || '',
    full_path: step.export_path || step.artifact_path || step.step_id,
    artifact_filename: step.artifact_filename || step.file_name || '',
    artifact_exists: step.artifact_exists !== false,
    artifact_path: step.artifact_path || '',
    export_exists: step.export_exists !== false,
    export_path: step.export_path || '',
    bundle_names: step.bundle_names || [],
    bundle: step.bundle || '',
  };
}

function buildProcessedSamples(samples, filamentMap) {
  // Build the processed_samples array from samples that have been processed
  let stripCounter = 0;
  const results = [];
  for (const exp of samples) {
    if (!exp.processed) continue;
    stripCounter++;
    results.push({
      filament_id: exp.variable_filament_id,
      color_name: exp.variable_color_name,
      hex: exp.variable_hex,
      manufacturer: exp.manufacturer,
      sample_id: exp.sample_id,
      strip_id: `strip-${String(stripCounter).padStart(3, '0')}`,
      source_image: exp.source_image || null,
      blank_image: exp.blank_image || null,
      mode: exp.mode || 'linear',
      printed_thicknesses_mm: exp.variable_thicknesses_mm || [],
      swatch_count: (exp.variable_thicknesses_mm || []).length,
      fixed_layers: exp.fixed_thicknesses_mm || [],
    });
  }
  return results;
}

function buildSummary(samples, filaments, steps, processedSamples) {
  const processedFilaments = new Set();
  for (const ps of processedSamples) {
    processedFilaments.add(ps.filament_id);
  }
  return {
    filament_count: filaments.length,
    sample_count: samples.length,
    step_count: steps.length,
    processed_filament_count: processedFilaments.size,
    processed_sample_count: processedSamples.length,
  };
}

function annotateFilaments(filaments, samples) {
  // Add sample_count and processed_count to each filament,
  // matching the aggregate fields app.js expects.
  const sampleCounts = {};
  const procCounts = {};
  for (const exp of samples) {
    const fid = exp.variable_filament_id;
    sampleCounts[fid] = (sampleCounts[fid] || 0) + 1;
    if (exp.processed) {
      procCounts[fid] = (procCounts[fid] || 0) + 1;
    }
    // Also count fixed filaments
    for (const fixedId of (exp.fixed_filament_ids || [])) {
      sampleCounts[fixedId] = (sampleCounts[fixedId] || 0) + 1;
    }
  }
  return filaments.map((f) => ({
    ...f,
    sample_count: sampleCounts[f.filament_id] || 0,
    processed_count: procCounts[f.filament_id] || 0,
  }));
}

// ── Main data loader ─────────────────────────────────────────────────────────

// Global loading state for the UI
let _apiLoadingState = 'idle'; // 'idle' | 'loading' | 'ready' | 'error'
let _apiErrorMessage = '';

function getApiLoadingState() {
  return { state: _apiLoadingState, error: _apiErrorMessage };
}

async function loadFromApi() {
  _apiLoadingState = 'loading';
  _apiErrorMessage = '';

  try {
    // Fetch samples, filaments, and steps in parallel
    const [samplesRaw, filamentsRaw, stepsRaw, blanksRaw, imageOverrides, imagesRaw, modelsStatus] = await Promise.all([
      fetchSamplesRaw(),
      fetchFilamentsRaw(),
      fetchSteps(),
      fetchBlanks(),
      fetchImageOverrides().catch(() => ({})),
      fetchImages().catch(() => []),
      fetchModelsStatus(),
    ]);

    const filamentMap = buildFilamentLookup(filamentsRaw);

    // Transform samples into the shape app.js expects
    const samples = samplesRaw.map((s) => transformSampleToData(s, filamentMap));

    // Build processed_samples from samples
    const processedSamples = buildProcessedSamples(samples, filamentMap);

    const steps = (stepsRaw || []).map((step) => transformStepToData(step));

    // Annotate filaments with sample counts
    const filaments = annotateFilaments(filamentsRaw, samples);

    // Build summary
    const summary = buildSummary(samples, filaments, steps, processedSamples);

    _apiLoadingState = 'ready';

    // Build blank lookup for use across tabs
    const blanks = blanksRaw || [];

    return {
      summary,
      filaments,
      samples,
      steps,
      blanks,
      images: imagesRaw || [],
      image_overrides: imageOverrides || {},
      model_status: modelsStatus || {},
      processed_samples: processedSamples,
    };
  } catch (err) {
    _apiLoadingState = 'error';
    _apiErrorMessage = err.message || 'Failed to load data from API';
    throw err;
  }
}

// ── Initialization ──────────────────────────────────────────────────────────

async function initializeData() {
  try {
    const apiData = await loadFromApi();
    // Replace the global `data` variable used by app.js
    Object.assign(data, apiData);
    console.log('[api] Loaded data from API:', {
      filaments: apiData.filaments.length,
      samples: apiData.samples.length,
      processed: apiData.processed_samples.length,
    });
    return 'api';
  } catch (err) {
    console.error('[api] API unavailable; calibration data must be served by the configured backend:', err.message);
    throw err;
  }
}
