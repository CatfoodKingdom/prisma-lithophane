/** Error raised when a polling response belongs to a different job. */
export class JobPollingIdentityError extends Error {
  constructor(expectedJobId, actualJobId) {
    super(
      actualJobId
        ? `Polled job identity mismatch: expected ${expectedJobId || "(missing)"}, received ${actualJobId}.`
        : `Polled job response omitted the expected job id ${expectedJobId || "(missing)"}.`,
    );
    this.name = "JobPollingIdentityError";
    this.expectedJobId = expectedJobId;
    this.actualJobId = actualJobId;
  }
}

export function assertPolledJobIdentity(status, expectedJobId) {
  const expected = String(expectedJobId || "");
  const actual = String(status?.job_id || "");
  if (!expected || actual !== expected) {
    throw new JobPollingIdentityError(expected, actual);
  }
}

/**
 * Poll one job sequentially until it reaches an application-defined terminal state.
 * All time and transport dependencies are injectable for deterministic tests.
 */
export async function pollJobUntilTerminal({
  jobId,
  fetchStatus,
  isTerminal,
  onStatus = () => {},
  shouldContinue = () => true,
  onTransientError = () => {},
  onRecovered = () => {},
  intervalMs = 500,
  maxRetryDelayMs = 4000,
  wait = (delayMs) => new Promise((resolve) => setTimeout(resolve, delayMs)),
}) {
  const expectedJobId = String(jobId || "");
  if (!expectedJobId) throw new Error("Cannot poll a job without a job id.");
  let consecutiveFailures = 0;
  let sequence = 0;

  while (shouldContinue()) {
    let status;
    try {
      status = await fetchStatus(expectedJobId);
    } catch (error) {
      if (!shouldContinue()) return null;
      consecutiveFailures += 1;
      const retryDelayMs = Math.min(
        maxRetryDelayMs,
        intervalMs * (2 ** Math.min(consecutiveFailures - 1, 3)),
      );
      onTransientError(error, { consecutiveFailures, retryDelayMs });
      await wait(retryDelayMs);
      continue;
    }

    if (!shouldContinue()) return null;
    assertPolledJobIdentity(status, expectedJobId);
    sequence += 1;
    if (consecutiveFailures > 0) {
      onRecovered({ consecutiveFailures, sequence });
      consecutiveFailures = 0;
    }
    await onStatus(status, { sequence });
    if (isTerminal(status)) return status;
    await wait(intervalMs);
  }
  return null;
}
