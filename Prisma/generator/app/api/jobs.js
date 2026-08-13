import { apiFetch, apiPost } from "./client.js?v=2026-08-04-saving-loading-fixes-v1";

export function startSolve({
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
  return apiPost("/solve/start", body);
}
export function cancelSolve(jobId = "") {
  const query = jobId ? `?job_id=${encodeURIComponent(jobId)}` : "";
  return apiPost(`/solve/cancel${query}`);
}
export function getSolveStatus() { return apiFetch("/solve/status"); }
export function startPaletteBatch(payload) {
  return apiPost("/solve/palette-batch/start", payload);
}
export function getPaletteBatchResult(jobId, resultId) {
  return apiFetch(
    `/solve/palette-batch/${encodeURIComponent(jobId)}/results/${encodeURIComponent(resultId)}`,
  );
}

export function startExportPrintFiles({
  geometrySource = "field_derived",
  fieldScale = 4,
  outputFormat = "3mf",
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
  return apiPost("/export/files/start", body);
}
export function getExportStatus() { return apiFetch("/export/files/status"); }
export function cancelExport(jobId = "") {
  const query = jobId ? `?job_id=${encodeURIComponent(jobId)}` : "";
  return apiPost(`/export/files/cancel${query}`);
}
export function openExportFolder(exportId) {
  return apiPost("/export/files/open-folder", { export_id: exportId });
}
export function exportFileUrl(filename, exportId = "") {
  const query = exportId ? `?dir=${encodeURIComponent(exportId)}` : "";
  const encodedPath = String(filename || "").split("/").map(encodeURIComponent).join("/");
  return `/api/export/files/${encodedPath}${query}`;
}
