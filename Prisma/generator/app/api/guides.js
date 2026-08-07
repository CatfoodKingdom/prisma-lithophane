import { apiFetch, getRequestContext } from "./client.js?v=2026-08-04-saving-loading-fixes-v1";

export function fetchGuideState() {
  return apiFetch("/guides/state");
}

export function putGuideState(state, expectedRevision) {
  return apiFetch("/guides/state", {
    method: "PUT",
    body: JSON.stringify({
      expected_revision: expectedRevision,
      state,
    }),
  });
}

export function fetchGuideRuntime({ includeSnapshot = false } = {}) {
  const { pageId } = getRequestContext();
  const query = new URLSearchParams({
    page_id: pageId,
    include_snapshot: includeSnapshot ? "true" : "false",
  });
  return apiFetch(`/guides/runtime?${query}`);
}

export function acquireGuideRuntime() {
  const { pageId } = getRequestContext();
  return apiFetch("/guides/runtime/acquire", {
    method: "POST",
    body: JSON.stringify({ page_id: pageId }),
  });
}

export function releaseGuideRuntime(leaseId) {
  const { pageId } = getRequestContext();
  return apiFetch("/guides/runtime/release", {
    method: "POST",
    body: JSON.stringify({ page_id: pageId, lease_id: leaseId }),
  });
}

export function beginGuideRuntime({ leaseId, guideId, routeId, clientSnapshot }) {
  const { pageId } = getRequestContext();
  return apiFetch("/guides/runtime/begin", {
    method: "POST",
    body: JSON.stringify({
      page_id: pageId,
      lease_id: leaseId,
      guide_id: guideId,
      route_id: routeId,
      client_snapshot: clientSnapshot,
    }),
  });
}

function sessionBody(sessionId) {
  return { session_id: sessionId, page_id: getRequestContext().pageId };
}

export function heartbeatGuideRuntime(sessionId) {
  return apiFetch("/guides/runtime/heartbeat", {
    method: "POST",
    body: JSON.stringify(sessionBody(sessionId)),
  });
}

export function claimGuideRuntimeRecovery(sessionId) {
  return apiFetch("/guides/runtime/claim-recovery", {
    method: "POST",
    body: JSON.stringify(sessionBody(sessionId)),
  });
}

export function resetGuideRuntime(sessionId, { beginRecovery = false } = {}) {
  return apiFetch("/guides/runtime/reset", {
    method: "POST",
    body: JSON.stringify({
      ...sessionBody(sessionId),
      begin_recovery: !!beginRecovery,
    }),
  });
}

export function restoreGuideRuntimeServer(sessionId) {
  return apiFetch("/guides/runtime/restore-server", {
    method: "POST",
    body: JSON.stringify(sessionBody(sessionId)),
  });
}

export function finalizeGuideRuntime(sessionId) {
  return apiFetch("/guides/runtime/finalize", {
    method: "POST",
    body: JSON.stringify(sessionBody(sessionId)),
  });
}

export function abandonGuideRuntime() {
  const { pageId } = getRequestContext();
  return apiFetch("/guides/runtime/abandon", {
    method: "POST",
    body: JSON.stringify({ page_id: pageId }),
  });
}

export function openGuideRuntimeConfigFolder() {
  return apiFetch("/guides/runtime/open-config-folder", { method: "POST", body: "{}" });
}

export function mountGuidePrinter(sessionId) {
  return apiFetch("/guides/runtime/mount-printer", {
    method: "POST",
    body: JSON.stringify(sessionBody(sessionId)),
  });
}

export function mountGuideAsset(sessionId, assetId) {
  return apiFetch(`/guides/runtime/assets/${encodeURIComponent(assetId)}/mount`, {
    method: "POST",
    body: JSON.stringify(sessionBody(sessionId)),
  });
}

export function registerGuideJob(sessionId, kind, jobId) {
  return apiFetch("/guides/runtime/jobs", {
    method: "POST",
    body: JSON.stringify({ ...sessionBody(sessionId), kind, job_id: jobId }),
  });
}

export function transitionGuideResource(sessionId, resource) {
  return apiFetch("/guides/runtime/resources", {
    method: "POST",
    body: JSON.stringify({ ...sessionBody(sessionId), resource }),
  });
}

export function reconcileGuideResources(sessionId) {
  return apiFetch("/guides/runtime/resources/reconcile", {
    method: "POST",
    body: JSON.stringify(sessionBody(sessionId)),
  });
}
