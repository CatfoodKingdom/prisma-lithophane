import { apiFetch } from "./client.js";

export function fetchPrinters() { return apiFetch("/printers"); }
export function savePrinters(data) {
  return apiFetch("/printers", { method: "PUT", body: JSON.stringify(data) });
}
export function fetchActivePrinter() { return apiFetch("/printers/active"); }
export function setActivePrinter(payload) {
  return apiFetch("/printers/active", { method: "PUT", body: JSON.stringify(payload) });
}
