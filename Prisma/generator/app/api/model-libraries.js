import { apiFetch, apiPost } from "./client.js";

export function fetchModelLibraries() { return apiFetch("/model-libraries"); }
export function installModelLibrary(file) {
  const formData = new FormData();
  formData.append("package", file);
  return apiFetch("/model-libraries/install", { method: "POST", headers: {}, body: formData });
}
export function activateModelLibrary(libraryId) {
  return apiPost("/model-libraries/activate", { library_id: libraryId });
}
export function removeModelLibrary(libraryId) {
  return apiPost("/model-libraries/remove", { library_id: libraryId });
}
export function openModelLibrariesFolder() { return apiPost("/model-libraries/open-folder"); }
export function restartPrisma() { return apiPost("/system/restart"); }
