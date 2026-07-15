// api.js — API client for the Prisma generator backend
//
// Communicates with the FastAPI server at /api/* and provides
// typed helper functions for all pipeline operations.

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
    let msg = body.detail || body.error || response.statusText;
    if (Array.isArray(msg)) {
      msg = msg.map((item) => {
        if (typeof item === 'string') return item;
        if (item && typeof item.msg === 'string') return item.msg;
        try { return JSON.stringify(item); } catch { return String(item); }
      }).join("; ");
    } else if (msg && typeof msg === "object") {
      try {
        msg = JSON.stringify(msg);
      } catch {
        msg = String(msg);
      }
    }
    throw new Error(`API ${response.status}: ${msg}`);
  }
  return response.json();
}

async function apiPost(path, body = {}) {
  return apiFetch(path, {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

// ── Filaments ────────────────────────────────────────────────────────────────

async function fetchFilaments() {
  return apiFetch('/filaments');
}

// ── Images ─��─────────────────────────────────────────────────────────────────

async function fetchImages() {
  return apiFetch('/images');
}

async function openImagesFolder() {
  return apiPost('/images/open-folder');
}

function imagePreviewUrl(filename) {
  return `${API_BASE}/images/preview/${encodeURIComponent(filename)}`;
}

async function uploadImage(file) {
  const formData = new FormData();
  formData.append('file', file);
  const response = await fetch(`${API_BASE}/images/upload`, {
    method: 'POST',
    body: formData,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || 'Upload failed');
  }
  return response.json();
}

// ── Session / Config ─────────────────────────────────────────────────────────

async function fetchSession() {
  return apiFetch('/session');
}

async function updateConfig(config) {
  return apiPost('/session/config', config);
}

// ── Palette ──────────────────────────────────────────────────────────────────

async function validatePalette(paletteIds) {
  return apiPost('/palette/validate', { palette: paletteIds });
}

async function suggestPalettes(imagePath, nFilaments = 7, topK = 5) {
  return apiPost('/palette/suggest', {
    image_path: imagePath,
    n_filaments: nFilaments,
    top_k: topK,
  });
}

async function getSuggestStatus() {
  return apiFetch('/palette/suggest/status');
}

async function cancelSuggest() {
  return apiPost('/palette/suggest/cancel');
}

// ── Gamut Preview ─��──────────────────────────────────────────────────────────

async function runGamutPreview(palette = null) {
  return apiPost('/gamut-preview', palette ? { palette } : {});
}

// ── Saved Palettes ──────────────────────────────────────────────────────

async function fetchSavedPalettes() {
  return apiFetch('/palettes');
}

async function savePalettesToServer(data) {
  return apiFetch('/palettes', { method: 'PUT', body: JSON.stringify(data) });
}

// ── Solve ───���────────────────────────────────────────────────────────────────

async function startSolve({
  palette = null,
  runId = null,
  profileRef = null,
  profileNameAtSolve = null,
  isProfileModifiedAtSolve = null,
  recipeSnapshot = null,
} = {}) {
  const body = {};
  if (palette) body.palette = palette;
  if (runId) body.card_id = runId;
  if (profileRef) body.profile_ref = profileRef;
  if (profileNameAtSolve) body.profile_name_at_solve = profileNameAtSolve;
  if (typeof isProfileModifiedAtSolve === "boolean") {
    body.is_profile_modified_at_solve = isProfileModifiedAtSolve;
  }
  if (recipeSnapshot) body.recipe_snapshot = recipeSnapshot;
  return apiPost('/solve/start', body);
}

async function cancelSolve(jobId = '') {
  const qs = jobId ? `?job_id=${encodeURIComponent(jobId)}` : '';
  return apiPost(`/solve/cancel${qs}`);
}

async function getSolveStatus() {
  return apiFetch('/solve/status');
}

// ── Export ────────────────────────────────────────────────────────────────────

async function startExportPrintFiles({
  geometrySource = 'field_derived',
  fieldScale = 4,
  outputFormat = '3mf',
  validateWrittenMeshes = false,
  cardId = null,
} = {}) {
  const body = {
    geometry_source: geometrySource,
    field_scale: fieldScale,
    output_format: outputFormat,
    validate_written_meshes: !!validateWrittenMeshes,
  };
  if (cardId) body.card_id = cardId;
  return apiPost('/export/files/start', body);
}

async function getExportStatus() {
  return apiFetch('/export/files/status');
}

async function cancelExport(jobId = '') {
  const qs = jobId ? `?job_id=${encodeURIComponent(jobId)}` : '';
  return apiPost(`/export/files/cancel${qs}`);
}

async function openExportFolder(exportId) {
  return apiPost('/export/files/open-folder', { export_id: exportId });
}

function exportFileUrl(filename, exportId = '') {
  const qs = exportId ? `?dir=${encodeURIComponent(exportId)}` : '';
  const encodedPath = String(filename || '').split('/').map(encodeURIComponent).join('/');
  return `${API_BASE}/export/files/${encodedPath}${qs}`;
}

// ── Printers ─────────────────────────────────────────────────────────────────

async function fetchPrinters() {
  return apiFetch('/printers');
}

async function savePrinters(data) {
  return apiFetch('/printers', { method: 'PUT', body: JSON.stringify(data) });
}

async function fetchActivePrinter() {
  return apiFetch('/printers/active');
}

async function setActivePrinter(payload) {
  return apiFetch('/printers/active', { method: 'PUT', body: JSON.stringify(payload) });
}

// ── Settings Profiles ────────────────────────────────────────────────────────

async function fetchSettingsProfiles() {
  return apiFetch('/settings-profiles');
}

async function createSettingsProfile(payload) {
  return apiFetch('/settings-profiles', { method: 'POST', body: JSON.stringify(payload) });
}

async function updateSettingsProfile(profileId, payload) {
  return apiFetch(`/settings-profiles/${encodeURIComponent(profileId)}`, {
    method: 'PUT',
    body: JSON.stringify(payload),
  });
}

async function deleteSettingsProfile(profileId) {
  return apiFetch(`/settings-profiles/${encodeURIComponent(profileId)}`, { method: 'DELETE' });
}

async function setUserDefaultSettingsProfile(profileId) {
  return apiFetch('/settings-profiles/user-default', {
    method: 'PUT',
    body: JSON.stringify({ profile_id: profileId }),
  });
}

async function restoreSystemSettingsProfile() {
  return apiFetch('/settings-profiles/restore-system', { method: 'POST' });
}

// ── Published Model Libraries ───────────────────────────────────────────────

async function fetchModelLibraries() {
  return apiFetch('/model-libraries');
}

async function installModelLibrary(file) {
  const formData = new FormData();
  formData.append('package', file);
  return apiFetch('/model-libraries/install', {
    method: 'POST',
    headers: {},
    body: formData,
  });
}

async function activateModelLibrary(libraryId) {
  return apiPost('/model-libraries/activate', { library_id: libraryId });
}

async function removeModelLibrary(libraryId) {
  return apiPost('/model-libraries/remove', { library_id: libraryId });
}

async function openModelLibrariesFolder() {
  return apiPost('/model-libraries/open-folder');
}

async function restartPrisma() {
  return apiPost('/system/restart');
}

// ── Modules ─────────────────────────────────────────────────────────────────

async function fetchModules() {
  return apiFetch('/modules');
}

async function toggleModule(moduleId, enabled) {
  return apiPost('/modules/toggle', { module_id: moduleId, enabled });
}

async function setModuleState(state) {
  return apiFetch('/modules/state', { method: 'PUT', body: JSON.stringify({ state }) });
}

// ── Saved Runs (Stage 9b) ─────────────────────────────────────────────────────

async function saveRun(cardId, label) {
  return apiPost('/runs/save', { card_id: cardId, label: label || null });
}

async function listSavedRuns() {
  return apiFetch('/runs/saved');
}

async function loadSavedRun(saveId, tier = "saved") {
  return apiPost('/runs/load', { save_id: saveId, tier });
}

async function loadSavedRunSettings(saveId, tier = "saved") {
  return apiPost('/runs/settings', { save_id: saveId, tier });
}

function savedRunPreviewUrl(save) {
  if (!save?.save_id) return "";
  const tier = save.tier === "auto" ? "auto" : "saved";
  return `${API_BASE}/runs/${tier}/${encodeURIComponent(save.save_id)}/preview`;
}

async function promoteAutoRun(saveId) {
  return apiPost(`/runs/auto/${encodeURIComponent(saveId)}/promote`, {});
}

async function deleteSavedRun(saveId) {
  return apiFetch(`/runs/saved/${encodeURIComponent(saveId)}`, { method: 'DELETE' });
}

async function deleteAutoRun(saveId) {
  return apiFetch(`/runs/auto/${encodeURIComponent(saveId)}`, { method: 'DELETE' });
}

async function renameSavedRun(saveId, label) {
  return apiPost(`/runs/saved/${encodeURIComponent(saveId)}/rename`, { label });
}

async function uploadSavedRun(file) {
  const fd = new FormData();
  fd.append('file', file);
  // Multipart route. Pass headers:{} so apiFetch's trailing `...options` OVERWRITES
  // the computed { 'Content-Type': 'application/json' } header with an empty object —
  // dropping the JSON content-type so the browser sets the multipart boundary itself.
  return apiFetch('/runs/load-upload', { method: 'POST', headers: {}, body: fd });
}

// ── Health check ─────────────────────────────────────────────────────────────

async function checkApiHealth() {
  try {
    await apiFetch('/filaments');
    return true;
  } catch {
    return false;
  }
}
