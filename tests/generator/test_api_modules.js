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
  const { createApiClient } = await import(clientUrl);
  const client = createApiClient(async (url, options) => {
    requests.push({ url, options });
    return { ok: true, json: async () => ({ accepted: true }) };
  });

  assert.deepEqual(await client.apiPost("/example", { value: 3 }), { accepted: true });
  assert.equal(requests[0].url, "/api/example");
  assert.equal(requests[0].options.method, "POST");
  assert.equal(requests[0].options.headers["Content-Type"], "application/json");
  assert.equal(requests[0].options.body, JSON.stringify({ value: 3 }));
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
  assert.deepEqual(request.options.headers, {});
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

  await assert.rejects(client.apiFetch("/failure"), /API 422: first; second/);
});
