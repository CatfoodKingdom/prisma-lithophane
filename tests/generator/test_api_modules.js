"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

const clientUrl = pathToFileURL(
  path.resolve(__dirname, "../../Prisma/generator/app/api/client.js"),
);

test("API client injects transport and serializes JSON posts", async () => {
  const requests = [];
  const { createApiClient, getRequestContext, setRequestContext } = await import(clientUrl);
  setRequestContext({
    workspaceEpoch: 7,
    guideSessionId: "guide-session",
    guideActionIdempotencyKey: "guide-action-key",
  });
  const client = createApiClient(async (url, options) => {
    requests.push({ url, options });
    return { ok: true, json: async () => ({ accepted: true }) };
  });

  assert.deepEqual(await client.apiPost("/example", { value: 3 }), { accepted: true });
  assert.equal(requests[0].url, "/api/example");
  assert.equal(requests[0].options.method, "POST");
  assert.equal(requests[0].options.headers["Content-Type"], "application/json");
  assert.equal(requests[0].options.headers["X-Prisma-Page-Id"], getRequestContext().pageId);
  assert.equal(requests[0].options.headers["X-Prisma-Workspace-Epoch"], "7");
  assert.equal(requests[0].options.headers["X-Prisma-Guide-Session"], "guide-session");
  assert.equal(requests[0].options.headers["X-Prisma-Idempotency-Key"], "guide-action-key");
  assert.equal(requests[0].options.body, JSON.stringify({ value: 3 }));
  setRequestContext({
    workspaceEpoch: 0,
    guideSessionId: null,
    guideActionIdempotencyKey: null,
  });
});

test("API page identity remains stable for a tab across reloads", async () => {
  const { getOrCreatePageId, PAGE_ID_STORAGE_KEY } = await import(clientUrl);
  const values = new Map();
  const storage = {
    getItem: key => values.get(key) || null,
    setItem: (key, value) => values.set(key, value),
  };

  const first = getOrCreatePageId(storage, "navigate");
  const afterReload = getOrCreatePageId(storage, "reload");

  assert.ok(first);
  assert.equal(afterReload, first);
  assert.equal(values.get(PAGE_ID_STORAGE_KEY), first);
});

test("a duplicated tab does not reuse copied page identity", async () => {
  const { getOrCreatePageId, PAGE_ID_STORAGE_KEY } = await import(clientUrl);
  const values = new Map([[PAGE_ID_STORAGE_KEY, "copied-page-id"]]);
  const copiedStorage = {
    getItem: key => values.get(key) || null,
    setItem: (key, value) => values.set(key, value),
  };

  const duplicatePageId = getOrCreatePageId(copiedStorage, "navigate");

  assert.notEqual(duplicatePageId, "copied-page-id");
  assert.equal(values.get(PAGE_ID_STORAGE_KEY), duplicatePageId);
});

test("API client leaves multipart content type to the browser", async () => {
  let request;
  const { createApiClient } = await import(clientUrl);
  const client = createApiClient(async (url, options) => {
    request = { url, options };
    return { ok: true, json: async () => ({}) };
  });
  const body = { multipart: true };

  await client.apiFetch("/upload", { method: "POST", headers: {}, body });
  assert.equal("Content-Type" in request.options.headers, false);
  assert.ok(request.options.headers["X-Prisma-Page-Id"]);
  assert.equal(request.options.headers["X-Prisma-Workspace-Epoch"], "0");
  assert.equal(request.options.body, body);
});

test("API client normalizes structured backend errors", async () => {
  const { createApiClient } = await import(clientUrl);
  const client = createApiClient(async () => ({
    ok: false,
    status: 422,
    statusText: "Unprocessable Entity",
    json: async () => ({ detail: [{ msg: "first" }, "second"] }),
  }));

  await assert.rejects(
    client.apiFetch("/failure"),
    (error) => error.status === 422 && /API 422: first; second/.test(error.message),
  );
});
