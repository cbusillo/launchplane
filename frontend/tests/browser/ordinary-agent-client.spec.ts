import { expect, test } from "@playwright/test";

// This journey tests routing through a simulated login, not OAuth authority.
test("an exact operation link survives sign-in without an operation search", async ({ page }) => {
  let authenticated = false;
  const requested: string[] = [];
  const operationId = "privileged-operation-selected";
  const target = `/ui/engineering/privileged-operations?operation_id=${operationId}`;
  await page.route("**/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    requested.push(path);
    if (path === "/v1/auth/session") {
      await route.fulfill({ status: authenticated ? 200 : 401, json: authenticated ? {
        status: "ok", csrf_token: "test-browser-csrf", identity: {
          provider: "github", login: "test-operator", github_id: 1001,
          name: "Test operator", email: "operator@example.invalid",
          organizations: [], teams: [], role: "admin",
        },
      } : { error: { code: "authentication_required", message: "Sign in" } } });
    } else if (path === "/v1/products") {
      await route.fulfill({ json: { status: "ok", products: [] } });
    } else if (path === `/v1/privileged-operations/plans/${operationId}/review`) {
      await route.fulfill({ json: { status: "ok", trace_id: "test-exact-review", review: {
        schema_version: 1, operation_id: operationId, descriptor_id: "managed-secret-reencryption",
        descriptor_version: 1, operation_class: "managed_secret_reencryption",
        safety_class: "secret_backed", title: "Managed-secret re-encryption review",
        requested_by_kind: "github_human",
        lifecycle: { status: "planned", generated_at: "2026-09-09T10:00:00Z",
          created_at: "2026-09-09T10:00:00Z", updated_at: "2026-09-09T10:00:00Z",
          expires_at: "2026-09-09T11:00:00Z", expiry_state: "active", terminal_at: "",
          terminal_reason_available: false, approval_recorded: false, execution_recorded: false },
        blockers: { state: "clear", policy_safety_blocker_count: 0,
          operational_readiness_blocker_count: 0, unreadable_secret_count: 0, codes: [] },
        change: { summary: "The exact requested operation.", changed: true, metrics: [] },
        blast_radius: { scope: "managed_secret_store", summary: "Requested integration only.", affected_count: 1 },
        rollback: { rollback_class: "key_retained", summary: "Existing key remains retained." },
        evidence: { result_status: "ok", raw_detail_available: false, redaction: "semantic_only", digests: [] },
        activity: [], can_approve: false, can_revoke: false,
        authorizes_approval: false, authorizes_execution: false, persists_state: false,
      } } });
    } else {
      await route.fulfill({ status: 404, json: { error: { message: "Unexpected request" } } });
    }
  });
  await page.route("**/auth/github/login?*", async (route) => {
    const destination = new URL(route.request().url()).searchParams.get("return_to");
    expect(destination).toBe(target);
    authenticated = true;
    await route.fulfill({ status: 302, headers: { location: destination! } });
  });
  await page.goto(target);
  await page.getByRole("link", { name: "Sign in with GitHub" }).click();
  await expect(page).toHaveURL(new RegExp(`${operationId}$`));
  await expect(page.getByRole("heading", { name: "Managed-secret re-encryption review" })).toBeVisible();
  await expect(page.getByRole("link", { name: "All operation plans" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Refresh plans" })).toBeVisible();
  await page.getByRole("button", { name: "Refresh plans" }).click();
  await expect.poll(() => requested.filter((path) => path.endsWith(`/${operationId}/review`)).length).toBeGreaterThan(1);
  expect(requested).not.toContain("/v1/privileged-operations/plans");
  expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
  await page.screenshot({ path: `../tmp/browser-smoke/exact-operation-${test.info().project.name}.png`, fullPage: true });
});

for (const withSession of [false, true]) {
  test(`ordinary connection ${withSession ? "with session" : "without session"} uses one clear approval and real controls`, async ({ page }) => {
    let status: "pending" | "approved" | "revoked" | "cancelled" = "pending";
    let applied = false;
    let sessionRevoked = false;
    const mutations: string[] = [];
    const principalId = "agent_browser";
    const operationId = "connection-browser";
    const path = `/v1/ordinary-agent-operations/${principalId}/${operationId}`;
    const target = `/ui/engineering/privileged-operations?principal_id=${principalId}&operation_id=${operationId}`;
    const now = Math.floor(Date.now() / 1000);
    const view = () => ({
      schema_version: 1, principal_id: principalId, operation_id: operationId,
      kind: "initial", status, reason_code: null, requester_kind: "terminal_agent",
      requester_subject: "CLI agent", requester_token_label: "workstation",
      credential_expires_at: now + 3600, delivery_expires_at: now + 600,
      credential_id: "credential_browser", credential_version: 1,
      current_policy_actions: withSession ? ["self_read", "preflight", "guarded_merge"] : ["self_read", "preflight"],
      current_policy_execution_profile: withSession ? "guarded_executor" : "read_only",
      attenuation: withSession ? { actions: ["self_read", "preflight", "guarded_merge"],
        session_expires_at: now + 1800, lease_expires_at: now + 900,
        action_limit: 6, pull_request_limit: 3, refresh_allowance: 2, continuation_expires_at: null } : null,
      target: { repository_id: 123, repository: "example/project", base_branch: "main" },
      session_id: withSession && applied ? "session_browser" : null,
      session_expires_at: withSession && applied ? now + 1800 : null,
      applied, can_approve: status === "pending",
    });
    await page.route("**/v1/**", async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.pathname === "/v1/auth/session") {
        await route.fulfill({ json: { status: "ok", csrf_token: "test-csrf", identity: {
          provider: "github", login: "operator", github_id: 1001, name: "Operator",
          email: "operator@example.invalid", organizations: [], teams: [], role: "admin",
        } } });
        return;
      }
      if (url.pathname === "/v1/products") {
        await route.fulfill({ json: { status: "ok", products: [] } });
        return;
      }
      if (request.method() === "POST") {
        mutations.push(url.pathname);
        expect(request.headers()["x-csrf-token"]).toBe("test-csrf");
        if (url.pathname === path + "/approve") { status = "approved"; applied = true; }
        else if (url.pathname === path + "/cancel") { status = "cancelled"; }
        else if (url.pathname === `/v1/ordinary-agent-sessions/${principalId}/session_browser/revoke`) { status = "revoked"; sessionRevoked = true; }
        else if (url.pathname === `/v1/ordinary-agent-connections/${principalId}/disconnect`) {
          status = "revoked";
          await route.fulfill({ json: { schema_version: 1, principal_id: principalId, status } });
          return;
        } else throw new Error(`Unexpected mutation ${url.pathname}`);
        await route.fulfill({ json: { schema_version: 1, operation: view(), review_url: target } });
        return;
      }
      if (url.pathname === path) {
        await route.fulfill({ json: { schema_version: 1, operation: view(), review_url: target } });
        return;
      }
      throw new Error(`Unexpected read ${url.pathname}`);
    });
    page.on("dialog", () => { throw new Error("No audit-reason or code-paste dialog should appear"); });
    await page.goto(target);
    await expect(page.getByRole("heading", { name: "Connect this agent" })).toBeVisible();
    await expect(page.getByText("CLI agent · workstation", { exact: true })).toBeVisible();
    await expect(page.getByRole("heading", { name: "example/project" })).toBeVisible();
    if (withSession) {
      await expect(page.getByText("Merge through Launchplane when required checks pass", { exact: true })).toBeVisible();
      await expect(page.getByText("Engineering permission expires", { exact: true })).toBeVisible();
    } else {
      await expect(page.getByText("Connect the agent. Engineering work needs a separately approved session.")).toBeVisible();
      await expect(page.getByText("Action limit", { exact: true })).toHaveCount(0);
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
    await page.screenshot({ path: `../tmp/browser-smoke/ordinary-${withSession ? "session" : "readonly"}-${test.info().project.name}.png`, fullPage: true });
    await page.getByRole("button", { name: "Approve connection", exact: true }).click();
    await expect(page.getByText("Connection prepared.", { exact: true })).toBeVisible();
    expect(mutations.filter((value) => value.endsWith("/approve"))).toHaveLength(1);
    if (withSession) {
      await page.getByRole("button", { name: "Revoke this session", exact: true }).click();
      await expect(page.getByText("Access has been revoked", { exact: true })).toBeVisible();
      expect(sessionRevoked).toBe(true);
    }
    await page.getByRole("button", { name: "Disconnect agent (all sessions)", exact: true }).click();
    await expect(page.getByText("Agent disconnected. All of its sessions are revoked.", { exact: true })).toBeVisible();
  });
}
