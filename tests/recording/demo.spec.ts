/**
 * Demo recording script — produces a video walkthrough of the analytics agent.
 *
 * This is NOT a test suite. It's designed to produce a watchable screen
 * recording showing the agent's key capabilities. Run it with:
 *
 *   npx playwright test tests/recording/demo.spec.ts
 *
 * Videos are saved to test-results/recording/.
 *
 * Requires OPENAI_API_KEY and backend + frontend running (or uses webServer
 * config from playwright.recording.config.ts).
 */

import { test, expect, Page } from "@playwright/test";

// ---------------------------------------------------------------------------
// Timing helpers — slow everything down so the video is watchable
// ---------------------------------------------------------------------------

const TYPING_DELAY = 60; // ms per keystroke
const PAUSE_AFTER_COMPLETE = 3000; // pause after agent finishes so viewer can read
const PAUSE_BEFORE_ACTION = 1000; // pause before clicking/typing something new

const IDLE_PLACEHOLDER = "Ask anything...";
const RUNNING_PLACEHOLDER = "Type to steer the agent or queue a follow-up";
const EMPTY_STATE_TEXT = "Ask anything about the Chinook music store database";

// ---------------------------------------------------------------------------
// Click indicator — injects a ripple animation at each mouse click
// ---------------------------------------------------------------------------

const CLICK_INDICATOR_SCRIPT = `
document.addEventListener('click', (e) => {
  // Solid dot that appears instantly at the click point
  const dot = document.createElement('div');
  const ds = dot.style;
  ds.position = 'fixed';
  ds.left = (e.clientX - 12) + 'px';
  ds.top = (e.clientY - 12) + 'px';
  ds.width = '24px';
  ds.height = '24px';
  ds.borderRadius = '50%';
  ds.background = 'rgba(250, 204, 21, 0.85)';
  ds.pointerEvents = 'none';
  ds.zIndex = '999999';
  ds.animation = 'click-dot 0.8s ease-out forwards';
  document.body.appendChild(dot);

  // Expanding ring
  const ring = document.createElement('div');
  const rs = ring.style;
  rs.position = 'fixed';
  rs.left = (e.clientX - 24) + 'px';
  rs.top = (e.clientY - 24) + 'px';
  rs.width = '48px';
  rs.height = '48px';
  rs.borderRadius = '50%';
  rs.border = '3px solid rgba(250, 204, 21, 0.7)';
  rs.pointerEvents = 'none';
  rs.zIndex = '999998';
  rs.animation = 'click-ring 0.8s ease-out forwards';
  document.body.appendChild(ring);

  setTimeout(() => { dot.remove(); ring.remove(); }, 900);
}, true);

const style = document.createElement('style');
style.textContent = \`
  @keyframes click-dot {
    0%   { transform: scale(1);   opacity: 0.7; }
    30%  { transform: scale(1.2); opacity: 0.5; }
    100% { transform: scale(0.5); opacity: 0; }
  }
  @keyframes click-ring {
    0%   { transform: scale(0.5); opacity: 0.8; }
    100% { transform: scale(2.5); opacity: 0; }
  }
\`;
document.head.appendChild(style);
`;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

async function pause(ms: number) {
  await new Promise((r) => setTimeout(r, ms));
}

async function slowType(page: Page, text: string) {
  const textarea = page.locator("textarea");
  await textarea.click();
  await textarea.pressSequentially(text, { delay: TYPING_DELAY });
}

async function waitForAgentComplete(page: Page) {
  // Wait for streaming to start
  await expect(
    page.locator(`textarea[placeholder="${RUNNING_PLACEHOLDER}"]`)
  ).toBeVisible({ timeout: 60_000 });
  // Wait for turn to finish
  await expect(
    page.locator(`textarea[placeholder="${IDLE_PLACEHOLDER}"]`)
  ).toBeVisible({ timeout: 180_000 });
}

async function cleanSessions(page: Page) {
  const res = await page.request.get("/api/sessions");
  const sessions: { session_id: string }[] = await res.json();
  await Promise.all(
    sessions.map((s) => page.request.delete(`/api/sessions/${s.session_id}`))
  );
}

// ---------------------------------------------------------------------------
// Recording
// ---------------------------------------------------------------------------

test.describe("Demo Recording", () => {
  test.beforeEach(async ({ page }) => {
    if (!process.env.OPENAI_API_KEY) {
      test.skip(true, "OPENAI_API_KEY not set");
    }
    await cleanSessions(page);
  });

  test("analytics agent walkthrough", async ({ page }) => {
    // Generous timeout — the agent takes time and we add pauses
    test.setTimeout(300_000);

    // Inject click indicator so clicks are visible in the recording
    await page.addInitScript(CLICK_INDICATOR_SCRIPT);

    await page.goto("/");
    await expect(page.locator("textarea")).toBeVisible();
    await expect(page.getByText(EMPTY_STATE_TEXT)).toBeVisible();

    // --- Step 1: Click a suggested prompt ---
    await pause(PAUSE_BEFORE_ACTION);
    const prompt = page.getByText("Find the top 5 artists by track count and revenue side by side");
    await prompt.click();
    await waitForAgentComplete(page);
    await pause(PAUSE_AFTER_COMPLETE);

    // --- Step 2: Multi-turn follow-up ---
    await pause(PAUSE_BEFORE_ACTION);
    await slowType(page, "Now show me the top 3 albums for the #1 artist");
    await pause(500);
    await page.locator("textarea").press("Enter");
    await waitForAgentComplete(page);
    await pause(PAUSE_AFTER_COMPLETE);

    // --- Step 3: New session + different query ---
    await pause(PAUSE_BEFORE_ACTION);
    await page.getByRole("button", { name: "New chat" }).click();
    await expect(page.getByText(EMPTY_STATE_TEXT)).toBeVisible();
    await pause(PAUSE_BEFORE_ACTION);

    await slowType(
      page,
      "What are the top 5 genres by total revenue?"
    );
    await pause(500);
    await page.locator("textarea").press("Enter");
    await waitForAgentComplete(page);
    await pause(PAUSE_AFTER_COMPLETE);

    // --- Step 4: Switch back to first session ---
    await pause(PAUSE_BEFORE_ACTION);
    const nav = page.locator("nav");
    await nav.getByText("Find the top 5 artists").first().click();
    await pause(2000); // let session replay render

    // --- Step 5: Interrupt ---
    await pause(PAUSE_BEFORE_ACTION);
    await slowType(
      page,
      "Give me a detailed breakdown of every customer's purchase history including all invoices and line items"
    );
    await pause(500);
    await page.locator("textarea").press("Enter");

    // Wait for streaming to start, then interrupt
    await expect(
      page.locator(`textarea[placeholder="${RUNNING_PLACEHOLDER}"]`)
    ).toBeVisible({ timeout: 60_000 });
    await pause(3000); // let some content stream in
    await page.keyboard.press("Escape");
    await expect(
      page.locator(`textarea[placeholder="${IDLE_PLACEHOLDER}"]`)
    ).toBeVisible({ timeout: 10_000 });
    await pause(PAUSE_AFTER_COMPLETE);
  });
});
