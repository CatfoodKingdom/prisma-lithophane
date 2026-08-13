import { apiFetch, apiPost } from "./client.js?v=2026-08-04-saving-loading-fixes-v1";

export function fetchFilaments() { return apiFetch("/filaments"); }
export function fetchSession() { return apiFetch("/session"); }
export function fetchSettingsContract() { return apiFetch("/settings/contract"); }
export function updateConfig(config) { return apiPost("/session/config", config); }
export function fetchSavedPalettes() { return apiFetch("/palettes"); }
export function savePalettesToServer(data) {
  return apiFetch("/palettes", { method: "PUT", body: JSON.stringify(data) });
}
