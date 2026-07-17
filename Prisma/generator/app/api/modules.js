import { apiFetch, apiPost } from "./client.js";

export function fetchModules() { return apiFetch("/modules"); }
export function toggleModule(moduleId, enabled) {
  return apiPost("/modules/toggle", { module_id: moduleId, enabled });
}
export function setModuleState(state) {
  return apiFetch("/modules/state", { method: "PUT", body: JSON.stringify({ state }) });
}
