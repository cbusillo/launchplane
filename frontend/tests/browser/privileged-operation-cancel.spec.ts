import { expect, test, waitForState, type Page } from "./state";

const operationId = "privileged-operation-abcdef0123456789abcdef0123456789";
const operationPath = `/v1/privileged-operations/plans/${operationId}`;

async function fixture(page: Page, options: {
  status?: string;
  deny?: boolean;
  unavailableDetails?: boolean;
  unavailableReadback?: boolean;
  hold?: Promise<void>;
  readOnly?: boolean;
} = {}) {
  await page.goto("/ui/?fixture=products");
  const identity = await page.evaluate(async () => (await import("/ui/src/dev-fixtures.ts")).fixtureIdentity);
  if (options.readOnly) identity.role = "read_only";
  let status = options.status ?? "planned";
  let reason = "";
  let readbacks = 0;
  const mutations: { path: string; body: { reason: string; source_event_id: string }; csrf: string | undefined }[] = [];
  const unexpected: string[] = [];
  const event = () => ({ event_id: "cancel-event", action: "cancelled", occurred_at: "2026-10-16T12:00:00Z", actor: { identity_type: "github_human", github_id: 123, login: "fixture-director" }, reason });
  await page.route("**/v1/**", async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/v1/auth/session") {
      await route.fulfill({ json: { status: "ok", identity, csrf_token: "cancel-fixture-csrf" } });
    } else if (path === "/v1/products") {
      await route.fulfill({ json: { status: "ok", products: [] } });
    } else if (path === `${operationPath}/review` && request.method() === "GET") {
      await route.fulfill({ json: { status: "ok", trace_id: "fixture-review", review: {
        operation_id: operationId, descriptor_id: "managed-merge-train-policy-import", operation_class: "managed_merge_train_policy_import",
        title: "Review agent policy proposal", requested_by_kind: "local_operator",
        lifecycle: { status, created_at: "2026-10-16T10:00:00Z", updated_at: "2026-10-16T12:00:00Z", expires_at: "", expiry_state: "active", approval_recorded: status === "approved", execution_recorded: false, terminal_reason_available: status === "cancelled" },
        blockers: { state: options.unavailableDetails ? "blocked" : "clear" },
        change: { summary: "Fixture scheduler policy proposal.", metrics: [] },
        blast_radius: { summary: "Fixture policy only." }, rollback: { summary: "Separate reversal." },
        evidence: { result_status: "ok", digests: [], raw_detail_available: true }, activity: [],
        can_approve: status === "planned" && !options.unavailableDetails, can_revoke: status === "approved",
      } } });
    } else if (path === operationPath && request.method() === "GET") {
      if (status === "cancelled") readbacks += 1;
      if ((status === "planned" && options.unavailableDetails) || (status === "cancelled" && options.unavailableReadback)) {
        await route.fulfill({ status: 503, json: { error: { message: "Fixture details unavailable." } } });
      } else {
        await route.fulfill({ json: { status: "ok", trace_id: "fixture-detail", record: { operation_id: operationId, status, terminal_reason: reason, requested_by: { identity_type: "local_operator" }, request: {}, evidence: {} }, events: status === "cancelled" ? [event()] : [] } });
      }
    } else if (path === `${operationPath}/cancel` && request.method() === "POST") {
      const body = request.postDataJSON();
      mutations.push({ path, body, csrf: request.headers()["x-csrf-token"] });
      if (options.hold) await options.hold;
      if (options.deny) {
        await route.fulfill({ status: 403, json: { trace_id: "fixture-denial", error: { code: "authorization_denied", message: "This identity cannot cancel this plan." } } });
      } else {
        status = "cancelled";
        reason = body.reason;
        await route.fulfill({ json: { status: "ok", trace_id: "fixture-cancel", record: { operation_id: operationId, status, terminal_reason: reason }, events: [event()] } });
      }
    } else {
      unexpected.push(`${request.method()} ${path}`);
      await route.fulfill({ status: 404, json: { error: { message: "Unexpected fixture request." } } });
    }
  });
  await page.goto(`/ui/engineering/privileged-operations?operation_id=${operationId}`);
  return { mutations, unexpected, readbacks: () => readbacks };
}

test("Director cancels a pending plan with a reason and reads the terminal result", { tag: "@layout" }, async ({ page }, testInfo) => {
  const state = await fixture(page);
  const cancel = page.getByRole("button", { name: "Reject / Cancel plan", exact: true });
  const approve = page.getByRole("button", { name: "Approve plan", exact: true });
  await expect(approve).toBeEnabled();
  await expect(cancel).toBeDisabled();
  await page.getByLabel("Reason for cancellation").fill("   ");
  await expect(cancel).toBeDisabled();
  expect(state.mutations).toEqual([]);
  await page.getByLabel("Reason for cancellation").fill("  Superseded by the replacement plan  ");
  await expect(cancel).toBeEnabled();
  await page.screenshot({ path: testInfo.outputPath("pending-cancel-control.png"), fullPage: true });
  await cancel.click();
  await expect(page.getByRole("region", { name: "Cancellation result" })).toContainText("Superseded by the replacement plan");
  await expect(page.getByRole("region", { name: "Cancellation result" })).toContainText("fixture-director (GitHub ID 123)");
  await expect(page.getByText("Plan cancelled. It can no longer be approved or executed.", { exact: true })).toBeVisible();
  await expect(approve).toHaveCount(0);
  await expect(cancel).toHaveCount(0);
  expect(state.readbacks()).toBeGreaterThan(0);
  expect(state.mutations).toHaveLength(1);
  expect(state.mutations[0].body.reason).toBe("Superseded by the replacement plan");
  expect(state.mutations[0].body.source_event_id).toBeTruthy();
  expect(state.mutations[0].csrf).toBe("cancel-fixture-csrf");
  await page.screenshot({ path: testInfo.outputPath("cancelled-result.png"), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.reload();
  await expect(page.locator(".privileged-operation-status")).toHaveText("cancelled");
  await expect(approve).toHaveCount(0);
  await expect(cancel).toHaveCount(0);
  expect(state.unexpected).toEqual([]);
});

test("cancellation stays available for a blocked plan with unreadable proposal details", async ({ page }) => {
  const state = await fixture(page, { unavailableDetails: true });
  await expect(page.getByText("Fixture details unavailable.", { exact: true })).toBeVisible();
  await page.getByLabel("Reason for cancellation").fill("Obsolete plan");
  await page.getByRole("button", { name: "Reject / Cancel plan", exact: true }).click();
  await expect(page.locator(".privileged-operation-status")).toHaveText("cancelled");
  await expect(page.getByRole("button", { name: "Approve plan", exact: true })).toHaveCount(0);
  expect(state.mutations).toHaveLength(1);
  expect(state.unexpected).toEqual([]);
});

test("cancellation disables competing actions until its request settles", async ({ page }) => {
  let release!: () => void;
  const hold = new Promise<void>(resolve => { release = resolve; });
  const state = await fixture(page, { hold });
  await expect(page.getByRole("button", { name: "Approve plan", exact: true })).toBeEnabled();
  await page.getByLabel("Reason for cancellation").fill("Superseded");
  await page.getByRole("button", { name: "Reject / Cancel plan", exact: true }).click();
  await waitForState(() => state.mutations.length).toBe(1);
  await expect(page.getByRole("button", { name: "Approve plan", exact: true })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Updating plan…", exact: true })).toBeDisabled();
  release();
  await expect(page.locator(".privileged-operation-status")).toHaveText("cancelled");
  expect(state.mutations).toHaveLength(1);
});

test("denied cancellation reports the refusal and leaves the pending plan unchanged", async ({ page }) => {
  const state = await fixture(page, { deny: true });
  await page.getByLabel("Reason for cancellation").fill("Superseded");
  await page.getByRole("button", { name: "Reject / Cancel plan", exact: true }).click();
  await expect(page.getByText("This identity cannot cancel this plan.", { exact: true })).toBeVisible();
  await expect(page.locator(".privileged-operation-status")).toHaveText("planned");
  await expect(page.getByRole("region", { name: "Cancellation result" })).toHaveCount(0);
  expect(state.readbacks()).toBe(0);
  expect(state.mutations).toHaveLength(1);
});

test("failed refreshed readback cannot restore approval after cancellation", async ({ page }) => {
  const state = await fixture(page, { unavailableReadback: true });
  await page.getByLabel("Reason for cancellation").fill("Superseded");
  await page.getByRole("button", { name: "Reject / Cancel plan", exact: true }).click();
  await expect(page.getByText("Cancellation recorded, but the refreshed result could not be read. Refresh plans to check it.", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Approve plan", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Reject / Cancel plan", exact: true })).toHaveCount(0);
  expect(state.readbacks()).toBeGreaterThan(0);
});

test("an approved operation offers revocation rather than pending-plan cancellation", async ({ page }) => {
  const state = await fixture(page, { status: "approved" });
  await expect(page.getByRole("button", { name: "Revoke approval", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Reject / Cancel plan", exact: true })).toHaveCount(0);
  expect(state.mutations).toEqual([]);
});

test("a read-only browser session has no cancellation control", async ({ page }) => {
  const state = await fixture(page, { readOnly: true });
  await expect(page.locator(".privileged-operation-status")).toHaveText("planned");
  await expect(page.getByRole("button", { name: "Reject / Cancel plan", exact: true })).toHaveCount(0);
  await expect(page.getByLabel("Reason for cancellation")).toHaveCount(0);
  expect(state.mutations).toEqual([]);
});
