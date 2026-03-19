import { test, expect, Page } from "@playwright/test";

const EMPTY_STATE_TEXT = "Ask anything about the Chinook music store database";
const IDLE_PLACEHOLDER = "Ask anything...";
const RUNNING_PLACEHOLDER = "Type to steer the agent or queue a follow-up";

// Skip all E2E tests if OPENAI_API_KEY is not set.
// Delete all sessions before each test for isolation.
test.beforeEach(async ({ page }) => {
  if (!process.env.OPENAI_API_KEY) {
    test.skip(true, "OPENAI_API_KEY not set");
  }

  // Clean up all sessions from the backend
  const res = await page.request.get("/api/sessions");
  const sessions: { session_id: string }[] = await res.json();
  await Promise.all(
    sessions.map((s) =>
      page.request.delete(`/api/sessions/${s.session_id}`)
    )
  );
});

/** Send a message and wait for the agent turn to complete. */
async function sendAndWait(page: Page, text: string) {
  await page.locator("textarea").fill(text);
  await page.locator("textarea").press("Enter");
  // Wait for streaming to start: placeholder changes when appState → "running"
  await expect(
    page.locator(`textarea[placeholder="${RUNNING_PLACEHOLDER}"]`)
  ).toBeVisible({ timeout: 60_000 });
  // Wait for turn to finish: placeholder changes back when appState → "idle"
  await expect(
    page.locator(`textarea[placeholder="${IDLE_PLACEHOLDER}"]`)
  ).toBeVisible({ timeout: 120_000 });
}

/** Navigate to the app and start a fresh (empty) session. */
async function newChat(page: Page) {
  await page.goto("/");
  await expect(page.locator("textarea")).toBeVisible();
  // Click "New chat" to ensure we're not in an existing session
  await page.getByRole("button", { name: "New chat" }).click();
  // Wait for CLEAR dispatch to render the empty state
  await expect(page.getByText(EMPTY_STATE_TEXT)).toBeVisible();
}

test.describe("Chat E2E", () => {
  test("happy path: send message and get response", async ({ page }) => {
    await newChat(page);
    await sendAndWait(page, "How many artists are in the database?");

    // User message should be in the chat
    await expect(
      page.getByRole("main").getByText("How many artists are in the database?").first()
    ).toBeVisible();
  });

  test("tool execution: SQL query shows tool call step", async ({ page }) => {
    await newChat(page);

    await page.locator("textarea").fill("Run this exact SQL: SELECT COUNT(*) FROM Artist");
    await page.locator("textarea").press("Enter");

    // The CodeExecution component renders the label as "SQL"
    await expect(
      page.getByRole("main").getByText("SQL").first()
    ).toBeVisible({ timeout: 60_000 });

    // Wait for turn to finish
    await expect(
      page.locator(`textarea[placeholder="${IDLE_PLACEHOLDER}"]`)
    ).toBeVisible({ timeout: 120_000 });
  });

  test("new session: creates and shows in sidebar", async ({ page }) => {
    await newChat(page);
    await sendAndWait(page, "New session test alpha");

    await page.getByRole("button", { name: "New chat" }).click();
    await expect(page.getByText(EMPTY_STATE_TEXT)).toBeVisible();
    await sendAndWait(page, "New session test beta");

    // Both sessions should appear in the sidebar
    const nav = page.locator("nav");
    await expect(nav.getByText("New session test alpha").first()).toBeVisible({ timeout: 10_000 });
    await expect(nav.getByText("New session test beta").first()).toBeVisible({ timeout: 10_000 });
  });

  test("session switching: each session shows its own messages", async ({ page }) => {
    await newChat(page);
    await sendAndWait(page, "Switching test AAA");

    await page.getByRole("button", { name: "New chat" }).click();
    await expect(page.getByText(EMPTY_STATE_TEXT)).toBeVisible();
    await sendAndWait(page, "Switching test BBB");

    // Main area should show session 2 content
    const main = page.getByRole("main");
    await expect(main.getByText("Switching test BBB").first()).toBeVisible();

    // Switch back to session 1 via sidebar
    await page.locator("nav").getByText("Switching test AAA").first().click();

    // Wait for stream replay to replace the empty state with session 1 content
    await expect(page.getByText(EMPTY_STATE_TEXT)).not.toBeVisible();
    await expect(main.getByText("Switching test AAA").first()).toBeVisible();
    // Use .first() since LLM response may echo the text
    await expect(main.getByText("Switching test BBB").first()).not.toBeVisible();
  });

  test("reload persistence: messages survive page reload", async ({ page }) => {
    await newChat(page);
    await sendAndWait(page, "Persistence reload check xyz");

    await expect(
      page.getByRole("main").getByText("Persistence reload check xyz").first()
    ).toBeVisible();

    await page.reload();

    // After reload: wait for session list to load, then stream replay to render messages
    await expect(page.locator("nav").locator("button").first()).toBeVisible();
    await expect(page.getByText(EMPTY_STATE_TEXT)).not.toBeVisible();
    await expect(
      page.getByRole("main").getByText("Persistence reload check xyz").first()
    ).toBeVisible();
  });

  test("interrupt: Escape stops streaming", async ({ page }) => {
    await newChat(page);

    await page.locator("textarea").fill(
      "Write a very long and detailed 5000-word essay about every music genre"
    );
    await page.locator("textarea").press("Enter");

    // Wait for streaming to start
    await expect(
      page.locator(`textarea[placeholder="${RUNNING_PLACEHOLDER}"]`)
    ).toBeVisible({ timeout: 60_000 });

    await page.keyboard.press("Escape");

    // Should return to idle
    await expect(
      page.locator(`textarea[placeholder="${IDLE_PLACEHOLDER}"]`)
    ).toBeVisible({ timeout: 10_000 });
  });
});
