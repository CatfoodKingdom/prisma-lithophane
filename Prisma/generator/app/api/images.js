import { API_BASE, apiFetch, apiPost } from "./client.js";

export function fetchImages() { return apiFetch("/images"); }
export function openImagesFolder() { return apiPost("/images/open-folder"); }
export function imagePreviewUrl(filename) {
  return `${API_BASE}/images/preview/${encodeURIComponent(filename)}`;
}
export function uploadImage(file) {
  const formData = new FormData();
  formData.append("file", file);
  return apiFetch("/images/upload", { method: "POST", headers: {}, body: formData });
}
