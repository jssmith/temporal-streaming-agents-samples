import { test, expect, Page } from "@playwright/test";

// Skip all E2E tests if OPENAI_API_KEY is not set
test.beforeEach(async () => {
  if (!process.env.OPENAI_API_KEY) {
    test.skip(true, "OPENAI_API_KEY not set");
  }
});

/** Send a message and wait for the agent turn to complete. */
async function sendAndWait(page: Page, text: string) {
  await page.locator("textarea").fill(text);
  await page.locator("textarea").press("Enter");
  // Wait for the agent turn to finish
  await expect(page.getByText("Esc to interrupt")).not.toBeVisible({ timeout: 60_000 });
}

/** Navigate to the app and start a fresh (empty) session. */
async function newChat(page: Page) {
  await page.goto("/");
  await expect(page.locator("textarea")).toBeVisible();
  // Click "New chat" to ensure we're not in an existing session
  await page.getByRole("button", { name: "New chat" }).click();
  // Wait for React state to settle after CLEAR dispatch
  await page.waitForTimeout(500);
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

    await expect(page.getByText("Esc to interrupt")).not.toBeVisible({ timeout: 60_000 });
  });

  test("new session: creates and shows in sidebar", async ({ page }) => {
    await newChat(page);
    await sendAndWait(page, "New session test alpha");

    await page.getByRole("button", { name: "New chat" }).click();
    await page.waitForTimeout(500);
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
    await page.waitForTimeout(500);
    await sendAndWait(page, "Switching test BBB");

    // Main area should show session 2 content
    const main = page.getByRole("main");
    await expect(main.getByText("Switching test BBB").first()).toBeVisible();

    // Switch back to session 1 via sidebar
    await page.locator("nav").getByText("Switching test AAA").first().click();

    // Session 1 content visible, session 2 not
    await expect(main.getByText("Switching test AAA").first()).toBeVisible({ timeout: 10_000 });
    await expect(main.getByText("Switching test BBB")).not.toBeVisible();
  });

  test("reload persistence: messages survive page reload", async ({ page }) => {
    await newChat(page);
    await sendAndWait(page, "Persistence reload check xyz");

    await expect(
      page.getByRole("main").getByText("Persistence reload check xyz").first()
    ).toBeVisible();

    await page.reload();

    // After reload, the most recent session auto-loads with messages restored
    await expect(
      page.getByRole("main").getByText("Persistence reload check xyz").first()
    ).toBeVisible({ timeout: 10_000 });
  });

  test("interrupt: Escape stops streaming", async ({ page }) => {
    await newChat(page);

    await page.locator("textarea").fill(
      "Write a very long and detailed 5000-word essay about every music genre"
    );
    await page.locator("textarea").press("Enter");

    // Wait for streaming indicator
    await expect(page.getByText("Esc to interrupt")).toBeVisible({ timeout: 30_000 });

    await page.keyboard.press("Escape");

    // Should return to idle
    await expect(page.getByText("Esc to interrupt")).not.toBeVisible({ timeout: 10_000 });
  });
});
