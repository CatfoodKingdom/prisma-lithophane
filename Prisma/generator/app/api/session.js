import { apiFetch, apiPost } from "./client.js";

export function fetchFilaments() { return apiFetch("/filaments"); }
export function fetchSession() { return apiFetch("/session"); }
export function updateConfig(config) { return apiPost("/session/config", config); }
export function fetchSavedPalettes() { return apiFetch("/palettes"); }
export function savePalettesToServer(data) {
  return apiFetch("/palettes", { method: "PUT", body: JSON.stringify(data) });
}
