import { apiPost } from "./client.js?v=2026-08-04-saving-loading-fixes-v1";

export function clearAllTempFiles() {
  return apiPost("/cache/clear-all", {});
}
