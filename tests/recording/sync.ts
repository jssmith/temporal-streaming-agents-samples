/**
 * File-based synchronization for parallel demo recordings.
 *
 * Two Playwright tests (app + temporal UI) run in parallel. This module
 * provides a barrier so both videos are rolling before the demo starts,
 * step-level signals so the temporal test can react to demo progress,
 * and a "done" signal so the passive recorder knows when to stop.
 */

import * as fs from "fs";
import * as path from "path";

const SYNC_FILE = path.join(
  __dirname,
  "../../test-results/recording/sync.json"
);
const POLL_INTERVAL = 100;
const BARRIER_TIMEOUT = 60_000;

interface SyncState {
  [source: string]: { [event: string]: number | string };
}

function readSync(): SyncState {
  try {
    return JSON.parse(fs.readFileSync(SYNC_FILE, "utf-8"));
  } catch {
    return {};
  }
}

function writeEvent(source: string, event: string, value: number | string = Date.now()) {
  const state = readSync();
  if (!state[source]) state[source] = {};
  state[source][event] = value;
  fs.mkdirSync(path.dirname(SYNC_FILE), { recursive: true });
  fs.writeFileSync(SYNC_FILE, JSON.stringify(state, null, 2));
}

async function waitForEvent(
  source: string,
  event: string,
  timeout: number
): Promise<number | string> {
  const start = Date.now();
  while (Date.now() - start < timeout) {
    const state = readSync();
    if (state[source]?.[event] !== undefined) return state[source][event];
    await new Promise((r) => setTimeout(r, POLL_INTERVAL));
  }
  throw new Error(`Timed out waiting for ${source}.${event}`);
}

/** Both sides call barrier — blocks until the other side is also ready. */
export async function barrier(self: string, other: string) {
  writeEvent(self, "ready");
  await waitForEvent(other, "ready", BARRIER_TIMEOUT);
}

/** Signal a named step (e.g., "step1_started"). Optionally attach data. */
export function signal(source: string, event: string, value?: string) {
  writeEvent(source, event, value ?? Date.now());
}

/** Wait for a named step from the other side. */
export async function waitFor(source: string, event: string, timeout = 300_000) {
  return waitForEvent(source, event, timeout);
}

/** The driver (app test) calls this when the demo is finished. */
export function signalDone(source: string) {
  writeEvent(source, "done");
}

/** The passive recorder waits for this before closing. */
export async function waitForDone(source: string, timeout = 600_000) {
  await waitForEvent(source, "done", timeout);
}
