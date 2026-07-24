"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

const appDir = path.resolve(__dirname, "../../Prisma/calibration/app");
const moduleUrl = (relative) => pathToFileURL(path.join(appDir, relative)).href;

test("Calibration lifecycle removes owned listeners and disposes once", async () => {
  const { createLifecycle } = await import(moduleUrl("core/lifecycle.js"));
  const lifecycle = createLifecycle();
  const target = new EventTarget();
  let calls = 0;
  lifecycle.listen(target, "change", () => { calls += 1; });
  target.dispatchEvent(new Event("change"));
  lifecycle.dispose();
  lifecycle.dispose();
  target.dispatchEvent(new Event("change"));
  assert.equal(calls, 1);
  assert.equal(lifecycle.disposed, true);
});

test("busy dialog guard blocks every ordinary dismiss path until authoritative completion", async () => {
  const { installCoreDialogs } = await import(moduleUrl("core/dialogs.js"));
  const app = { commands: {} };
  installCoreDialogs(app);
  let removals = 0;
  let closes = 0;
  const guard = app.commands.createBusyDialogGuard({
    element: { remove: () => { removals += 1; } },
    onClose: () => { closes += 1; },
  });
  guard.setBusy(true);
  assert.equal(guard.close(), false);
  assert.equal(guard.closed, false);
  assert.equal(removals, 0);
  assert.equal(guard.close({ force: true }), true);
  assert.equal(guard.close({ force: true }), true);
  assert.equal(removals, 1);
  assert.equal(closes, 1);
});

test("Calibration API injects transport and preserves request contracts", async () => {
  const { createCalibrationApi } = await import(moduleUrl("api/index.js"));
  const requests = [];
  const api = createCalibrationApi({
    fetchImpl: async (url, options) => {
      requests.push({ url, options });
      return { ok: true, json: async () => ({ accepted: true }) };
    },
  });
  assert.deepEqual(await api.apiPost("/example", { value: 3 }), { accepted: true });
  assert.equal(requests[0].url, "/api/example");
  assert.equal(requests[0].options.method, "POST");
  assert.equal(requests[0].options.body, JSON.stringify({ value: 3 }));
});

test("rendering helpers preserve legacy output while providing explicit safe boundaries", async () => {
  const { installCoreRendering } = await import(moduleUrl("core/rendering.js"));
  const app = { commands: {} };
  installCoreRendering(app);
  assert.equal(app.commands.escapeHtml(`<a title="x">Tom & 'Jo'</a>`), "&lt;a title=&quot;x&quot;&gt;Tom &amp; &#39;Jo&#39;&lt;/a&gt;");
  assert.equal(app.commands.escapeAttribute(`a'b"<&`), "a&#39;b&quot;&lt;&amp;");
  assert.equal(app.commands._escHtml(`a'b"<&>`), `a'b"&lt;&amp;&gt;`);
  assert.equal(app.commands._escAttr(`a'b"<&>`), `a'b&quot;&lt;&amp;>`);
  const target = { innerHTML: "old" };
  app.commands.setTrustedHtml(target, "<strong>known markup</strong>");
  assert.equal(target.innerHTML, "<strong>known markup</strong>");
});

test("job polling remains sequential and rejects mismatched identities", async () => {
  const { installCorePolling } = await import(moduleUrl("core/polling.js"));
  const app = { commands: {} };
  installCorePolling(app);
  let active = 0;
  let maximumActive = 0;
  let calls = 0;
  const terminal = await app.commands.pollJobUntilTerminal({
    jobId: "job-1",
    fetchStatus: async () => {
      active += 1;
      maximumActive = Math.max(maximumActive, active);
      calls += 1;
      active -= 1;
      return { job_id: "job-1", status: calls === 2 ? "done" : "running" };
    },
    isTerminal: (status) => status.status === "done",
    wait: async () => {},
  });
  assert.equal(terminal.status, "done");
  assert.equal(maximumActive, 1);
  await assert.rejects(
    app.commands.pollJobUntilTerminal({
      jobId: "expected",
      fetchStatus: async () => ({ job_id: "wrong", status: "done" }),
      isTerminal: () => true,
      wait: async () => {},
    }),
    /identity mismatch/,
  );
});

test("job polling retries transient failures with bounded backoff and reports recovery", async () => {
  const { installCorePolling } = await import(moduleUrl("core/polling.js"));
  const app = { commands: {} };
  installCorePolling(app);
  const delays = [];
  const transient = [];
  const recovered = [];
  let calls = 0;
  const terminal = await app.commands.pollJobUntilTerminal({
    jobId: "recovering-job",
    fetchStatus: async () => {
      calls += 1;
      if (calls <= 3) throw new Error(`offline ${calls}`);
      return { job_id: "recovering-job", status: "done" };
    },
    isTerminal: (status) => status.status === "done",
    intervalMs: 100,
    maxRetryDelayMs: 250,
    wait: async (delay) => { delays.push(delay); },
    onTransientError: (_error, meta) => { transient.push(meta); },
    onRecovered: (meta) => { recovered.push(meta); },
  });
  assert.equal(terminal.status, "done");
  assert.deepEqual(delays, [100, 200, 250]);
  assert.deepEqual(transient.map((entry) => entry.consecutiveFailures), [1, 2, 3]);
  assert.deepEqual(recovered, [{ consecutiveFailures: 3, sequence: 1 }]);
});

test("job polling stops without publishing stale responses after its owner is disposed", async () => {
  const { installCorePolling } = await import(moduleUrl("core/polling.js"));
  const app = { commands: {} };
  installCorePolling(app);
  let active = true;
  let statuses = 0;
  const terminal = await app.commands.pollJobUntilTerminal({
    jobId: "detached-job",
    fetchStatus: async () => {
      active = false;
      return { job_id: "detached-job", status: "done" };
    },
    isTerminal: () => true,
    shouldContinue: () => active,
    onStatus: () => { statuses += 1; },
    wait: async () => {},
  });
  assert.equal(terminal, null);
  assert.equal(statuses, 0);
});
