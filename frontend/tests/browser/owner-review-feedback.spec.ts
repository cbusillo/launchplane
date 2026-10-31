import { waitForState, expect, test, type Page } from "./state";

async function openAndConfirmPreview(page: Page): Promise<void> {
  const link = page.getByRole("link", { name: "Open the preview" });
  const url = await link.getAttribute("href");
  await page.context().route(url!, route => route.fulfill({ contentType: "text/html", body: "<h1>Preview</h1>" }));
  const [preview] = await Promise.all([page.waitForEvent("popup"), link.click()]);
  await expect(preview.getByRole("heading", { name: "Preview" })).toBeVisible();
  await preview.close();
  await page.getByRole("checkbox", { name: "I looked at this preview and it is right." }).check();
}

test("a carried acceptance explains invisible follow-ups and can be replaced", { tag: "@responsive" }, async ({ page }, testInfo) => {
  await page.goto("/ui/owner-review?fixture=products&repository=example%2Fcontrol-plane&pull_request=308&scenario=carried-invisible-decision");
  const decision = page.getByLabel("Recorded decision");
  const carryNote = decision.locator('[data-carry-reason="client_impact_none"]');
  await expect(carryNote).toBeVisible();
  await expect(decision.locator(":scope > p").filter({ hasText: /still applies|applies to preview version/i })).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("owner-acceptance-invisible.png"), fullPage: true });
  await page.getByRole("textbox").fill("Please revise the feature.");
  await page.getByRole("button", { name: "Request changes" }).click();
  await expect(decision).toContainText("Changes requested");
  await expect(carryNote).toHaveCount(0);
});

for (const scenario of ["carried-decision", "carried-base-integration"]) {
test(`a saved base-update carry can be replaced by a new decision: ${scenario}`, { tag: "@responsive" }, async ({ page }, testInfo) => {
  await page.goto(`/ui/owner-review?fixture=products&repository=example%2Fcontrol-plane&pull_request=308&scenario=${scenario}`);
  const decision = page.getByLabel("Recorded decision");
  await expect(decision.locator('[data-carry-reason]')).toBeVisible();
  await expect(decision.locator(":scope > p").filter({ hasText: /still applies|applies to preview version/i })).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("owner-acceptance-carried.png"), fullPage: true });
  await page.getByRole("textbox").fill("Please change this feature again.");
  await page.getByRole("button", { name: "Request changes" }).click();
  await expect(decision).toContainText("Changes requested");
  await expect(decision.locator("[data-carry-reason]")).toHaveCount(0);
  await expect(decision.locator("blockquote")).toHaveText("Please change this feature again.");
});
}

for (const scenario of ["former-client-carried-decision", "earlier-carried-decision", "carried-without-preview"]) {
  test(`saved carry history does not claim current acceptance: ${scenario}`, { tag: "@responsive" }, async ({ page }, testInfo) => {
    await page.goto(`/ui/owner-review?fixture=products&repository=example%2Fcontrol-plane&pull_request=308&scenario=${scenario}`);
    const decision = page.getByLabel("Recorded decision");
    await expect(decision.locator('[data-carry-reason="client_impact_none"]')).toBeVisible();
    await expect(decision.locator(":scope > p").filter({ hasText: /still applies|applies to preview version/i })).toHaveCount(0);
    const accept = page.getByRole("button", { name: "Accept", exact: true });
    if (scenario === "carried-without-preview") {
      await expect(accept).toHaveCount(0);
    } else {
      await openAndConfirmPreview(page);
      await expect(accept).toBeEnabled();
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`${scenario}.png`), fullPage: true });
  });
}

test("a saved decision with pending delivery keeps the reason available to retry", async ({ page }, testInfo) => {
  await page.goto("/ui/owner-review?fixture=products&repository=example%2Fcontrol-plane&pull_request=308&scenario=delivery-pending");
  const reason = "Please adjust the checkout flow.\nKeep the contact details visible.";
  await page.getByRole("textbox").fill(reason);
  await page.getByRole("button", { name: "Request changes" }).click();
  await expect(page.getByLabel("Recorded decision")).toContainText("Your decision is saved, but delivery to the agent is pending.");
  await expect(page.getByRole("textbox")).toHaveValue(reason);
  await expect(page.getByLabel("Recorded decision").locator("blockquote")).toHaveText(reason);
  await page.getByRole("button", { name: "Retry delivery", exact: true }).click();
  await openAndConfirmPreview(page);
  await expect(page.getByRole("button", { name: "Accept", exact: true })).toBeEnabled();
  await page.getByRole("button", { name: "Request changes" }).click();
  await expect(page.getByLabel("Recorded decision")).toHaveCount(1);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("owner-feedback-pending.png"), fullPage: true });
});

test("an earlier accepted decision cannot look like approval of the current preview", async ({ page }, testInfo) => {
  await page.goto("/ui/owner-review?fixture=products&repository=example%2Fcontrol-plane&pull_request=308&scenario=earlier-decision");
  await expect(page.getByLabel("Recorded decision")).toContainText("Earlier preview version bbbbbbb");
  await expect(page.getByLabel("Recorded decision")).toContainText("This decision does not apply to the current preview.");
  await expect(page.getByText("Preview version aaaaaaa", { exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("owner-feedback-history.png"), fullPage: true });
});

for (const samePreview of [true, false]) {
  test(`retry delivery preserves eligibility only for the same reviewed preview: ${samePreview}`, { tag: "@responsive" }, async ({ page }) => {
    await page.goto("/ui/owner-review?fixture=products&repository=example%2Fcontrol-plane&pull_request=308");
    const original = await page.evaluate(async () => {
      const modulePath = "/ui/src/dev-fixtures.ts";
      const fixtures = await import(modulePath);
      return fixtures.productReviewForFixture("products", { ...fixtures.productReviewDecisionForFixture("changes_requested", "Fix the price."), feedback_url: "" });
    });
    const delivered = {
      ...original,
      preview_generation_id: samePreview ? original.preview_generation_id : "generation-next",
      preview_is_current: false,
      cannot_accept_reason: "We could not check the latest version of this change. Refresh the review.",
      latest_decision: { ...original.latest_decision, feedback_url: `${original.pull_request_url}#issuecomment-1` },
    };
    await page.route("**/v1/auth/session", route => route.fulfill({ json: { status: "ok", csrf_token: "fixture", identity: { login: "example-owner", github_id: 9001, role: "read_only", organizations: [], teams: [] } } }));
    await page.route("**/v1/product-review?*", route => route.fulfill({ json: original }));
    await page.route("**/v1/product-review/feedback/retry", route => route.fulfill({ json: delivered }));
    await page.goto("/ui/owner-review?repository=example%2Fcontrol-plane&pull_request=308");
    await openAndConfirmPreview(page);
    await expect(page.getByRole("button", { name: "Accept", exact: true })).toBeEnabled();
    await page.getByRole("button", { name: "Retry delivery", exact: true }).click();
    await expect(page.getByLabel("Recorded decision")).not.toContainText("delivery to the agent is pending");
    if (samePreview) {
      await expect(page.getByRole("button", { name: "Accept", exact: true })).toBeEnabled();
    } else {
      await expect(page.getByRole("button", { name: "Accept", exact: true })).toHaveCount(0);
      await expect(page.getByRole("button", { name: "Refresh review", exact: true })).toBeVisible();
    }
  });
}

test("an unavailable notes read after a saved decision keeps Accept disabled", async ({ page }) => {
  await page.goto("/ui/owner-review?fixture=products&repository=example%2Fcontrol-plane&pull_request=308");
  const original = await page.evaluate(async () => {
    const modulePath = "/ui/src/dev-fixtures.ts";
    return (await import(modulePath)).productReviewForFixture("products");
  });
  const unread = { ...original, pull_request_title: "", client_test_notes: "", client_test_notes_error: "The test notes could not be loaded. Refresh the review." };
  await page.route("**/v1/auth/session", route => route.fulfill({ json: { status: "ok", csrf_token: "fixture", identity: { login: "example-owner", github_id: 9001, role: "read_only", organizations: [], teams: [] } } }));
  await page.route("**/v1/product-review?*", route => route.fulfill({ json: original }));
  await page.route("**/v1/product-review/decisions", route => route.fulfill({ json: unread }));
  await page.goto("/ui/owner-review?repository=example%2Fcontrol-plane&pull_request=308");
  await page.getByRole("textbox").fill("Please revise the price.");
  await page.getByRole("button", { name: "Request changes", exact: true }).click();
  await expect(page.getByRole("button", { name: "Accept", exact: true })).toBeDisabled();
});

test("delivery of a historical saved decision can be retried without a preview", async ({ page }, testInfo) => {
  await page.goto("/ui/owner-review?fixture=products&repository=example%2Fcontrol-plane&pull_request=308&scenario=pending-without-preview&decision_id=saved");
  const decision = page.getByLabel("Recorded decision");
  await expect(decision).toContainText("Reviewed preview version bbbbbbb");
  await expect(decision).toContainText("No preview is currently ready for comparison.");
  await expect(page.getByRole("button", { name: "Accept", exact: true })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "View latest review" })).toBeVisible();
  await page.getByRole("button", { name: "Retry delivery", exact: true }).click();
  await expect(decision).not.toContainText("delivery to the agent is pending");
  await expect(decision).toContainText("Reviewed preview version bbbbbbb");
  await expect(decision).toContainText("Accepted");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("owner-feedback-retried.png"), fullPage: true });
});

test("legacy feedback is shared only when the Owner explicitly sends it", async ({ page }) => {
  await page.goto("/ui/owner-review?fixture=products&repository=example%2Fcontrol-plane&pull_request=308&scenario=legacy-decision&decision_id=saved");
  const decision = page.getByLabel("Recorded decision");
  await expect(decision).toContainText("This saved decision has not been shared on the pull request.");
  await expect(page.getByRole("button", { name: "Accept", exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "Send feedback to the agent", exact: true }).click();
  await expect(decision).not.toContainText("has not been shared");
  await expect(decision).toContainText("Reviewed preview version aaaaaaa");
});

for (const action of ["Accept", "Request changes"]) {
  test(`a stale ${action} stays disabled until the current preview is loaded`, { tag: "@layout" }, async ({ page }, testInfo) => {
    await page.goto("/ui/owner-review?fixture=products&repository=example%2Fcontrol-plane&pull_request=308");
    await expect(page.getByRole("button", { name: "Accept", exact: true })).toBeVisible();
    const original = await page.evaluate(async () => {
      const modulePath = "/ui/src/dev-fixtures.ts";
      return (await import(modulePath)).productReviewForFixture("products");
    });
    const current = { ...original, head_sha: "b".repeat(40), preview_generation_id: "generation-next" };
    let review = original;
    let refreshFails = false;
    const submitted: Record<string, unknown>[] = [];
    await page.route("**/v1/auth/session", route => route.fulfill({ json: { status: "ok", csrf_token: "fixture", identity: { login: "example-owner", github_id: 9001, role: "read_only", organizations: [], teams: [] } } }));
    await page.route("**/v1/product-review?*", route => refreshFails
      ? route.fulfill({ status: 503, json: { error: { code: "unavailable", message: "Unavailable" } } })
      : route.fulfill({ json: review }));
    await page.route("**/v1/product-review/decisions", route => {
      submitted.push(route.request().postDataJSON());
      if (submitted.length === 1) {
        return route.fulfill({ status: 409, json: { error: { code: "product_review_preview_changed", message: "The preview changed. Refresh the review and look at the current preview before deciding again." } } });
      }
      return route.fulfill({ json: current });
    });
    await page.goto("/ui/owner-review?repository=example%2Fcontrol-plane&pull_request=308");
    await expect(page.getByText(`Preview version ${original.head_sha.slice(0, 7)}`, { exact: true })).toBeVisible();
    if (action === "Request changes") await page.getByRole("textbox").fill("Please fix the price.");
    else await openAndConfirmPreview(page);
    await page.getByRole("button", { name: action, exact: true }).click();
    await expect(page.getByRole("alert")).toContainText("The preview changed");
    await expect(page.getByRole("button", { name: "Accept", exact: true })).toBeDisabled();
    await expect(page.getByRole("button", { name: "Request changes", exact: true })).toBeDisabled();
    expect(submitted).toHaveLength(1);
    expect(submitted[0]).toMatchObject({ reviewed_head_sha: original.head_sha, reviewed_preview_url: original.preview_url, reviewed_generation_id: original.preview_generation_id });
    await page.screenshot({ path: testInfo.outputPath("stale-preview-refused.png"), fullPage: true });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    // A failed refresh cannot unlock the old review.
    refreshFails = true;
    await page.getByRole("button", { name: "Refresh review" }).click();
    await expect(page.getByRole("alert")).toBeVisible();
    await expect(page.getByRole("button", { name: "Accept", exact: true })).toHaveCount(0);
    refreshFails = false;
    review = current;
    await page.getByRole("button", { name: "Refresh review" }).click();
    await expect(page.getByText(`Preview version ${current.head_sha.slice(0, 7)}`, { exact: true })).toBeVisible();
    expect(submitted).toHaveLength(1);
    await expect(page.getByRole("button", { name: "Accept", exact: true })).toBeDisabled();
    await openAndConfirmPreview(page);
    await expect(page.getByRole("button", { name: "Accept", exact: true })).toBeEnabled();
    await page.getByRole("button", { name: "Accept", exact: true }).click();
    await expect(page.getByRole("region", { name: `Decision for ${current.display_name}` }).getByRole("status")).toBeVisible();
    await waitForState(() => submitted).toHaveLength(2);
    expect(submitted[1]).toMatchObject({ reviewed_head_sha: current.head_sha, reviewed_preview_url: current.preview_url, reviewed_generation_id: current.preview_generation_id });
  });
}

test("the Client sees the change title and what to check before deciding", { tag: "@responsive" }, async ({ page }, testInfo) => {
  await page.goto("/ui/owner-review?fixture=products&repository=example%2Fcontrol-plane&pull_request=308");
  const card = page.locator(".owner-review-card");
  await expect(card.getByRole("heading", { name: "Show the repair price on each model" })).toBeVisible();
  await expect(card.getByRole("heading", { name: "What to check" })).toBeVisible();
  await expect(card.locator(".release-review-notes li")).toHaveText(["Open iPhone 15 repairs.", "Confirm each repair shows its price."]);
  await expect(card.locator(".release-review-notes strong")).toHaveText("iPhone 15");
  const notesBox = await card.locator(".release-review-notes").boundingBox();
  const acceptBox = await page.getByRole("button", { name: "Accept", exact: true }).boundingBox();
  expect(notesBox!.y).toBeLessThan(acceptBox!.y);
  await expect(page.getByRole("link", { name: "Topics to discuss" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Accept", exact: true })).toBeDisabled();
  await expect(page.getByRole("checkbox")).toBeDisabled();
  await openAndConfirmPreview(page);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("owner-review-notes.png"), fullPage: true });
  await page.getByRole("button", { name: "Accept", exact: true }).click();
  await expect(page.getByLabel("Recorded decision")).toContainText("Accepted");
  await expect(card.locator(".release-review-notes li")).toHaveCount(2);
});

for (const [scenario, message] of [
  ["missing-notes", "Test notes are missing for this change."],
  ["nothing-to-test", "Nothing for you to test: CI tests only."],
  ["notes-unavailable", "The test notes could not be loaded. Open the pull request to read them."],
]) {
  test(`the review explains when there is nothing to read: ${scenario}`, async ({ page }) => {
    await page.goto(`/ui/owner-review?fixture=products&repository=example%2Fcontrol-plane&pull_request=308&scenario=${scenario}`);
    await expect(page.locator(".owner-review-card")).toContainText(message);
  });
}

for (const scenario of ["missing-notes", "notes-unavailable", "open-questions", "answered-questions"]) {
  test(`Accept follows the notes gate: ${scenario}`, { tag: "@responsive" }, async ({ page }) => {
    await page.goto(`/ui/owner-review?fixture=products&repository=example%2Fcontrol-plane&pull_request=308&scenario=${scenario}`);
    const accept = page.getByRole("button", { name: "Accept", exact: true });
    await openAndConfirmPreview(page);
    if (scenario === "answered-questions") {
      await expect(accept).toBeEnabled();
    } else {
      await expect(accept).toBeDisabled();
    }
    if (scenario === "open-questions") {
      await expect(page.locator(".release-review-notes")).toContainText("What is the public business address?");
    }
    await page.getByRole("textbox").fill("Please supply the missing facts.");
    await expect(page.getByRole("button", { name: "Request changes", exact: true })).toBeEnabled();
  });
}

test("an older serving preview offers feedback while the latest preview is being updated", { tag: "@responsive" }, async ({ page }) => {
  await page.goto("/ui/owner-review?fixture=products&repository=example%2Fcontrol-plane&pull_request=308&scenario=stale-preview");
  await expect(page.getByRole("button", { name: "Accept", exact: true })).toHaveCount(0);
  await expect(page.getByRole("checkbox")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Refresh review", exact: true })).toBeVisible();
  await page.getByRole("textbox").fill("The older preview still has the wrong price.");
  await page.getByRole("button", { name: "Request changes", exact: true }).click();
  await expect(page.getByLabel("Recorded decision")).toContainText("Changes requested");
});
