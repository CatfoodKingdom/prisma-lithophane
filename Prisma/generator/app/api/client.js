const API_BASE = "/api";
const PAGE_ID_STORAGE_KEY = "prisma.generator.page-id";
const PAGE_ID_GLOBAL_KEY = Symbol.for("prisma.generator.page-id");

function createPageId() {
  return globalThis.crypto?.randomUUID?.()
    || `page-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

/**
 * A browser tab keeps one identity across actual reloads so it can immediately
 * reclaim its own Guide lease. Other navigation types receive a new identity:
 * browsers may copy sessionStorage into a duplicated tab, and two live Prisma
 * tabs must never share an owner ID.
 */
function getNavigationType() {
  try {
    return globalThis.performance?.getEntriesByType?.("navigation")?.[0]?.type || null;
  } catch {
    return null;
  }
}

export function getOrCreatePageId(
  storage = globalThis.sessionStorage,
  navigationType = getNavigationType(),
) {
  try {
    const stored = storage?.getItem?.(PAGE_ID_STORAGE_KEY);
    if (stored && navigationType === "reload") return stored;
    const created = createPageId();
    storage?.setItem?.(PAGE_ID_STORAGE_KEY, created);
    return created;
  } catch {
    return createPageId();
  }
}

const pageId = globalThis[PAGE_ID_GLOBAL_KEY] || getOrCreatePageId();
globalThis[PAGE_ID_GLOBAL_KEY] = pageId;

const requestContext = {
  pageId,
  workspaceEpoch: 0,
  guideSessionId: null,
  guideActionIdempotencyKey: null,
};

function notifyPageDeparture(event) {
  if (event?.persisted || typeof globalThis.navigator?.sendBeacon !== "function") return false;
  const payload = new globalThis.Blob(
    [JSON.stringify({ page_id: requestContext.pageId })],
    { type: "application/json" },
  );
  return globalThis.navigator.sendBeacon(`${API_BASE}/guides/runtime/depart`, payload);
}

if (typeof globalThis.addEventListener === "function") {
  globalThis.addEventListener("pagehide", notifyPageDeparture);
}

export function getRequestContext() {
  return { ...requestContext };
}

export function setRequestContext({ workspaceEpoch, guideSessionId, guideActionIdempotencyKey } = {}) {
  if (Number.isInteger(workspaceEpoch) && workspaceEpoch >= 0) {
    requestContext.workspaceEpoch = workspaceEpoch;
  }
  if (typeof guideSessionId !== "undefined") {
    requestContext.guideSessionId = guideSessionId || null;
  }
  if (typeof guideActionIdempotencyKey !== "undefined") {
    requestContext.guideActionIdempotencyKey = guideActionIdempotencyKey || null;
  }
  return getRequestContext();
}

/** Create an API client whose transport can be replaced by unit tests. */
export function createApiClient(fetchImpl = globalThis.fetch.bind(globalThis)) {
  async function apiFetch(path, options = {}) {
    const { headers: requestedHeaders, ...requestOptions } = options;
    const contextHeaders = {
      "X-Prisma-Page-Id": requestContext.pageId,
      "X-Prisma-Workspace-Epoch": String(requestContext.workspaceEpoch),
      ...(requestContext.guideSessionId
        ? { "X-Prisma-Guide-Session": requestContext.guideSessionId }
        : {}),
      ...(requestContext.guideActionIdempotencyKey
        ? { "X-Prisma-Idempotency-Key": requestContext.guideActionIdempotencyKey }
        : {}),
    };
    const response = await fetchImpl(`${API_BASE}${path}`, {
      headers: {
        ...(typeof requestedHeaders === "undefined" ? { "Content-Type": "application/json" } : {}),
        ...contextHeaders,
        ...(requestedHeaders || {}),
      },
      ...requestOptions,
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      let message = body.detail || body.error || response.statusText;
      if (Array.isArray(message)) {
        message = message.map((item) => {
          if (typeof item === "string") return item;
          if (item && typeof item.msg === "string") return item.msg;
          try { return JSON.stringify(item); } catch { return String(item); }
        }).join("; ");
      } else if (message && typeof message === "object") {
        try { message = JSON.stringify(message); } catch { message = String(message); }
      }
      const error = new Error(`API ${response.status}: ${message}`);
      error.status = response.status;
      error.body = body;
      if (
        response.status === 423
        && typeof globalThis.dispatchEvent === "function"
        && typeof globalThis.CustomEvent === "function"
      ) {
        globalThis.dispatchEvent(new globalThis.CustomEvent("prisma:workspace-locked", {
          detail: { message: String(message || "Another Prisma window is running a guide") },
        }));
      }
      throw error;
    }
    return response.json();
  }

  function apiPost(path, body = {}) {
    return apiFetch(path, { method: "POST", body: JSON.stringify(body) });
  }

  return Object.freeze({ apiFetch, apiPost });
}

export const { apiFetch, apiPost } = createApiClient();
export { API_BASE, PAGE_ID_STORAGE_KEY, notifyPageDeparture };
