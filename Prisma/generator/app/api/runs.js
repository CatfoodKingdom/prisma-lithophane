import { API_BASE, apiFetch, apiPost } from "./client.js";

export function saveRun(cardId, label) {
  return apiPost("/runs/save", { card_id: cardId, label: label || null });
}
export function listSavedRuns() { return apiFetch("/runs/saved"); }
export function loadSavedRun(saveId, tier = "saved") {
  return apiPost("/runs/load", { save_id: saveId, tier });
}
export function loadSavedRunSettings(saveId, tier = "saved") {
  return apiPost("/runs/settings", { save_id: saveId, tier });
}
export function savedRunPreviewUrl(save) {
  if (!save?.save_id) return "";
  const tier = save.tier === "auto" ? "auto" : "saved";
  return `${API_BASE}/runs/${tier}/${encodeURIComponent(save.save_id)}/preview`;
}
export function promoteAutoRun(saveId) {
  return apiPost(`/runs/auto/${encodeURIComponent(saveId)}/promote`, {});
}
export function deleteSavedRun(saveId) {
  return apiFetch(`/runs/saved/${encodeURIComponent(saveId)}`, { method: "DELETE" });
}
export function deleteAutoRun(saveId) {
  return apiFetch(`/runs/auto/${encodeURIComponent(saveId)}`, { method: "DELETE" });
}
export function renameSavedRun(saveId, label) {
  return apiPost(`/runs/saved/${encodeURIComponent(saveId)}/rename`, { label });
}
export function uploadSavedRun(file) {
  const formData = new FormData();
  formData.append("file", file);
  return apiFetch("/runs/load-upload", { method: "POST", headers: {}, body: formData });
}
