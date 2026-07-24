export function installApiClient(api) {
  async function apiFetch(path, options = {}) {
    const url = `${api.constants.API_BASE}${path}`;
    const response = await api.fetchImpl(url, {
      headers: { "Content-Type": "application/json", ...options.headers },
      ...options,
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      const detail = body.detail || body.error || response.statusText;
      const msg =
        typeof detail === "string"
          ? detail
          : detail.message || JSON.stringify(detail);
      const error = new Error(`API ${response.status}: ${msg}`);
      error.status = response.status;
      error.detail = detail;
      error.body = body;
      throw error;
    }
    return response.json();
  }

  async function apiPost(path, body = {}) {
    return api.apiFetch(path, {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  async function apiPut(path, body = {}) {
    return api.apiFetch(path, {
      method: "PUT",
      body: JSON.stringify(body),
    });
  }

  async function apiPatch(path, body = {}) {
    return api.apiFetch(path, {
      method: "PATCH",
      body: JSON.stringify(body),
    });
  }

  async function apiDelete(path) {
    return api.apiFetch(path, { method: "DELETE" });
  }

  Object.assign(api, {
    apiFetch,
    apiPost,
    apiPut,
    apiPatch,
    apiDelete,
  });
}
