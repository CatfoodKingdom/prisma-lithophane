import { apiFetch } from "./client.js?v=2026-08-04-saving-loading-fixes-v1";

export function fetchSettingsProfiles() { return apiFetch("/settings-profiles"); }
export function createSettingsProfile(payload) {
  return apiFetch("/settings-profiles", { method: "POST", body: JSON.stringify(payload) });
}
export function updateSettingsProfile(profileId, payload) {
  return apiFetch(`/settings-profiles/${encodeURIComponent(profileId)}`, {
    method: "PUT", body: JSON.stringify(payload),
  });
}
export function deleteSettingsProfile(profileId) {
  return apiFetch(`/settings-profiles/${encodeURIComponent(profileId)}`, { method: "DELETE" });
}
export function setUserDefaultSettingsProfile(profileId) {
  return apiFetch("/settings-profiles/user-default", {
    method: "PUT", body: JSON.stringify({ profile_id: profileId }),
  });
}
export function restoreSystemSettingsProfile() {
  return apiFetch("/settings-profiles/restore-system", { method: "POST" });
}
