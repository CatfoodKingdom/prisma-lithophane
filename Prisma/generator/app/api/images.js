import { API_BASE, apiFetch, apiPost } from "./client.js?v=2026-08-04-saving-loading-fixes-v1";

export function fetchImages() { return apiFetch("/images"); }
export function openImagesFolder() { return apiPost("/images/open-folder"); }
export function imagePreviewUrl(filename, sourceRef = null) {
  const base = `${API_BASE}/images/preview/${encodeURIComponent(filename)}`;
  return sourceRef
    ? `${base}?image_source_ref=${encodeURIComponent(sourceRef)}`
    : base;
}
export function uploadImage(file) {
  const formData = new FormData();
  formData.append("file", file);
  return apiFetch("/images/upload", { method: "POST", headers: {}, body: formData });
}
export function importImages(files) {
  const formData = new FormData();
  for (const file of files) formData.append("files", file);
  return apiFetch("/images/import", { method: "POST", headers: {}, body: formData });
}
export function refreshImages() {
  return apiFetch("/images/refresh", { method: "POST" });
}
export function getImageImportStatus(batchId) {
  return apiFetch(`/images/imports/${encodeURIComponent(batchId)}`);
}
