"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

const root = path.resolve(__dirname, "..", "..");
const modulePaths = {
  generator: path.join(
    root,
    "Prisma",
    "generator",
    "app",
    "core",
    "polling.js",
  ),
  calibration: path.join(
    root,
    "Prisma",
    "calibration",
    "app",
    "core",
    "polling.js",
  ),
};

async function loadPolling(appName) {
  return import(pathToFileURL(modulePaths[appName]));
}

for (const appName of Object.keys(modulePaths)) {
  test(`${appName} polling is sequential and recovers after one transient failure`, async () => {
    const context = await loadPolling(appName);
    const statuses = [];
    const retryEvents = [];
    const recovered = [];
    const waits = [];
    let call = 0;
    let activeRequests = 0;
    let maxActiveRequests = 0;
    const result = await context.pollJobUntilTerminal({
      jobId: "job-1",
      fetchStatus: async () => {
        activeRequests += 1;
        maxActiveRequests = Math.max(maxActiveRequests, activeRequests);
        call += 1;
        await Promise.resolve();
        activeRequests -= 1;
        if (call === 1) throw new Error("temporary disconnect");
        return call === 2
          ? { job_id: "job-1", status: "running" }
          : { job_id: "job-1", status: "succeeded" };
      },
      isTerminal: (status) => status.status === "succeeded",
      onStatus: (status, meta) => statuses.push([status.status, meta.sequence]),
      onTransientError: (_error, meta) => retryEvents.push(meta),
      onRecovered: (meta) => recovered.push(meta),
      intervalMs: 10,
      maxRetryDelayMs: 25,
      wait: async (delay) => {
        waits.push(delay);
      },
    });

    assert.equal(result.status, "succeeded");
    assert.equal(maxActiveRequests, 1);
    assert.deepEqual(statuses, [
      ["running", 1],
      ["succeeded", 2],
    ]);
    assert.deepEqual(waits, [10, 10]);
    assert.equal(retryEvents.length, 1);
    assert.equal(retryEvents[0].consecutiveFailures, 1);
    assert.equal(recovered.length, 1);
    assert.equal(recovered[0].consecutiveFailures, 1);
  });

  test(`${appName} polling rejects a mismatched job before applying status`, async () => {
    const context = await loadPolling(appName);
    let applied = 0;
    await assert.rejects(
      context.pollJobUntilTerminal({
        jobId: "job-1",
        fetchStatus: async () => ({ job_id: "job-2", status: "succeeded" }),
        isTerminal: () => true,
        onStatus: () => {
          applied += 1;
        },
        wait: async () => {},
      }),
      (error) =>
        error.name === "JobPollingIdentityError" &&
        error.expectedJobId === "job-1",
    );
    assert.equal(applied, 0);
  });

  test(`${appName} delayed response cannot update an obsolete polling owner`, async () => {
    const context = await loadPolling(appName);
    let resolveStatus;
    let current = true;
    let applied = 0;
    const pending = context.pollJobUntilTerminal({
      jobId: "job-1",
      fetchStatus: () =>
        new Promise((resolve) => {
          resolveStatus = resolve;
        }),
      isTerminal: () => true,
      shouldContinue: () => current,
      onStatus: () => {
        applied += 1;
      },
      wait: async () => {},
    });
    await Promise.resolve();
    current = false;
    resolveStatus({ job_id: "newer-job", status: "succeeded" });

    assert.equal(await pending, null);
    assert.equal(applied, 0);
  });

  test(`${appName} authoritative failed status is returned as terminal`, async () => {
    const context = await loadPolling(appName);
    const result = await context.pollJobUntilTerminal({
      jobId: "job-1",
      fetchStatus: async () => ({
        job_id: "job-1",
        status: "failed",
        error: { message: "boom" },
      }),
      isTerminal: (status) =>
        ["succeeded", "failed", "cancelled"].includes(status.status),
      wait: async () => {},
    });
    assert.equal(result.status, "failed");
    assert.equal(result.error.message, "boom");
  });
}
