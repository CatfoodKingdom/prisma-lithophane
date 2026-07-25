import { apiPost } from "./client.js";

export function clearAllTempFiles() {
  return apiPost("/cache/clear-all", {});
}
