import { apiFetch } from "./client.js?v=2026-08-04-saving-loading-fixes-v1";

export function fetchPrinters() { return apiFetch("/printers"); }
export function savePrinters(data) {
  return apiFetch("/printers", { method: "PUT", body: JSON.stringify(data) });
}
export function fetchActivePrinter() { return apiFetch("/printers/active"); }
export function setActivePrinter(payload) {
  return apiFetch("/printers/active", { method: "PUT", body: JSON.stringify(payload) });
}
