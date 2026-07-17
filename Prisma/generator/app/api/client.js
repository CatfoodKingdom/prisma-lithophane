const API_BASE = "/api";

/** Create an API client whose transport can be replaced by unit tests. */
export function createApiClient(fetchImpl = globalThis.fetch.bind(globalThis)) {
  async function apiFetch(path, options = {}) {
    const response = await fetchImpl(`${API_BASE}${path}`, {
      headers: { "Content-Type": "application/json", ...options.headers },
      ...options,
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
      throw new Error(`API ${response.status}: ${message}`);
    }
    return response.json();
  }

  function apiPost(path, body = {}) {
    return apiFetch(path, { method: "POST", body: JSON.stringify(body) });
  }

  return Object.freeze({ apiFetch, apiPost });
}

export const { apiFetch, apiPost } = createApiClient();
export { API_BASE };
