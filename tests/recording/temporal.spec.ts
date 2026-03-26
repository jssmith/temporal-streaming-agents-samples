/**
 * Temporal UI recorder — runs in parallel with demo.spec.ts.
 *
 * Records the Temporal dev server UI while the app demo runs. Actively
 * navigates: starts on the workflow list, clicks into running workflows
 * to show activity history, and navigates back between steps.
 */

import { test, expect } from "@playwright/test";
import { barrier, waitFor, waitForDone } from "./sync";

const PAUSE = 2000;

async function pause(ms: number) {
  await new Promise((r) => setTimeout(r, ms));
}

test("Temporal UI recording", async ({ page }) => {
  test.setTimeout(600_000);

  // Start on the "Running" workflow filter so we only see active workflows
  await page.goto("/namespaces/default/workflows");
  await expect(page.locator("main")).toBeVisible({ timeout: 15_000 });

  // Click the "Running" saved view to filter out old completed workflows
  await page.getByText("Running", { exact: true }).last().click();
  await pause(1000);

  // Signal readiness and wait for the app test
  await barrier("temporal", "app");

  // --- Step 1: App sends first query — a new workflow will appear ---
  await waitFor("app", "query_sent");
  // Wait a moment for the workflow to appear, then refresh
  await pause(3000);
  await page.reload();
  await pause(2000);

  // Click into the newest workflow (first row link)
  const firstWorkflowLink = page.locator("table a[href*='workflow']").first();
  await expect(firstWorkflowLink).toBeVisible({ timeout: 10_000 });
  await firstWorkflowLink.click();
  await pause(PAUSE);

  // Wait for history to load — we should see activity events
  await expect(page.locator("main")).toBeVisible();
  await pause(PAUSE);

  // Stay on the workflow detail until the query completes
  await waitFor("app", "query_done");
  await pause(PAUSE);

  // --- Step 2: Follow-up query (same workflow) ---
  await waitFor("app", "followup_sent");
  // Reload to show new activities appearing
  await pause(3000);
  await page.reload();
  await pause(PAUSE);

  await waitFor("app", "followup_done");
  await pause(PAUSE);

  // --- Step 3: New session — go back to workflow list to see the new workflow ---
  await waitFor("app", "session2_sent");
  await page.goto("/namespaces/default/workflows");
  await pause(1000);
  await page.getByText("Running", { exact: true }).last().click();
  await pause(3000);
  await page.reload();
  await pause(2000);

  // Click into the newest workflow (the new session)
  const newWorkflowLink = page.locator("table a[href*='workflow']").first();
  await expect(newWorkflowLink).toBeVisible({ timeout: 10_000 });
  await newWorkflowLink.click();
  await pause(PAUSE);

  await waitFor("app", "session2_done");
  await pause(PAUSE);

  // --- Step 5: Interrupt — stay on detail to see what happens ---
  await waitFor("app", "interrupt_sent");
  await pause(3000);
  await page.reload();
  await pause(PAUSE);

  await waitFor("app", "interrupt_done");
  await pause(PAUSE);

  // Wait for the app to be fully done
  await waitForDone("app");
  await pause(3000);
});
