import { expect, test, type Page } from "@playwright/test";
import type { PrepareOrdinaryAgentDeliveryPolicyData } from "../../src/generated/openapi.ts";

const preparationPath = "/v1/privileged-operations/authorization-candidates/ordinary-agent-delivery/prepare";
const operationId = "privileged-operation-client-policy";
const entry = "/ui/engineering/privileged-operations?descriptor_id=ordinary-agent-delivery-activation";

async function setup(
  page: Page,
  loseFirstResponse: boolean,
  rejectFirst = false,
  terminalState: string | string[] = "ready",
) {
  const proposals: Array<PrepareOrdinaryAgentDeliveryPolicyData["body"]> = [];
  const unexpected: string[] = [];
  let inputReads = 0;
  await page.route("**/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/v1/auth/session") {
      await route.fulfill({ json: {
        status: "ok", csrf_token: "browser-test-csrf", identity: {
          provider: "github", login: "operator", github_id: 1001,
          name: "Operator", email: "operator@example.invalid",
          organizations: [], teams: [], role: "admin",
        },
      } });
    } else if (path === "/v1/products") {
      await route.fulfill({ json: { status: "ok", products: [] } });
    } else if (path === "/v1/privileged-operations/plans") {
      await route.fulfill({ json: { status: "ok", trace_id: "plans", total: 0, reviews: [] } });
    } else if (path.endsWith("/ordinary-agent-delivery/inputs")) {
      const state = Array.isArray(terminalState)
        ? terminalState[Math.min(inputReads, terminalState.length - 1)]
        : terminalState;
      inputReads += 1;
      await route.fulfill({ json: {
        status: "ok", schema_version: 1, trace_id: "inputs",
        observed_at: "2026-09-14T15:00:00Z",
        authorization_policy: { record_id: "authz-r4", revision: 4, schema_version: 2, policy_sha256: "1".repeat(64) },
        terminal_enrollment: { state },
        inventory_state: "complete", merge_policy_state: "available",
        merge_policy: { record_id: "merge-r2", policy_sha256: "2".repeat(64), updated_at: "2026-09-14T14:00:00Z" },
        repositories: [{
          record_id: "inventory-r1", repository_id: "9001", repository: "example/project",
          inventory_revision: 1, inventory_sha256: "3".repeat(64),
          recorded_at: "2026-09-14T14:00:00Z", configured_branches: ["main"],
        }], diagnostics: [],
      } });
    } else if (path.endsWith("/ordinary-agent-delivery-activation/options")) {
      await route.fulfill({ json: {
        status: "ok", trace_id: "options",
        duration_options: [{ duration_seconds: 86400, activation_expires_at: "2026-09-15T15:00:00Z", label: "1 day" }],
        setup_options: proposals.length ? [{
          policy_operation_id: operationId,
          repository_inventory_record_id: "inventory-r1", predecessor: null,
          scope: { target: { repository_id: 9001, repository: "example/project", base_branch: "main" }, managed_set_id: "ordinary-client.test", managed_rule_id: "delivery" },
          label: "example/project · main · prepared just now",
        }] : [], revoke_options: [],
      } });
    } else if (path === preparationPath && request.method() === "POST") {
      expect(request.headers()["x-csrf-token"]).toBe("browser-test-csrf");
      proposals.push(request.postDataJSON());
      if (rejectFirst && proposals.length === 1) {
        await route.fulfill({ status: 409, json: { trace_id: "conflict", error: { code: "ordinary_agent_policy_preparation_conflict", message: "The selected delivery branch is no longer configured." } } });
      } else if (loseFirstResponse && proposals.length === 1) {
        await route.abort("failed");
      } else {
        await route.fulfill({ json: {
          trace_id: "prepared", state: "planned", operation_id: operationId,
          principal_id: proposals[0].intent.principal_id,
        } });
      }
    } else if (
      path === "/v1/privileged-operations/authorization-candidates/prepare" &&
      request.method() === "POST"
    ) {
      const body = request.postDataJSON();
      expect(body).toMatchObject({
        candidate_id: "ordinary-agent-enrollment-requester",
        intent: "add",
      });
      expect(Object.keys(body).sort()).toEqual(["candidate_id", "intent", "source_event_id"]);
      await route.fulfill({ json: {
        trace_id: "terminal-prepared", state: "planned", operation_id: "privileged-operation-terminal-policy",
      } });
    } else if (
      path === "/v1/privileged-operations/plans/privileged-operation-terminal-policy/review" &&
      request.method() === "GET"
    ) {
      await route.fulfill({ json: {
        status: "ok",
        trace_id: "terminal-review",
        review: {
          schema_version: 1,
          operation_id: "privileged-operation-terminal-policy",
          descriptor_id: "managed-authz-policy-set",
          descriptor_version: 1,
          operation_class: "managed_authz_policy_set",
          safety_class: "policy_admin",
          title: "Review terminal client connection requests",
          requested_by_kind: "github_human",
          lifecycle: {
            status: "planned", generated_at: "2026-09-14T15:00:00Z", expiry_state: "active",
            created_at: "2026-09-14T15:00:00Z", updated_at: "2026-09-14T15:00:00Z",
            expires_at: "2026-09-15T15:00:00Z", terminal_at: "", terminal_reason_available: false,
            approval_recorded: false, execution_recorded: false,
          },
          blockers: { state: "clear", policy_safety_blocker_count: 0, operational_readiness_blocker_count: 0, unreadable_secret_count: 0, codes: [] },
          change: {
            changed: true,
            summary: "Allow the configured terminal to submit client connection requests through Launchplane until this access is removed. Every client connection still needs separate administrator approval.",
            metrics: [],
          },
          blast_radius: { scope: "authorization_policy", summary: "One trusted terminal; client connection requests only.", affected_count: 1 },
          rollback: { rollback_class: "policy_cas", summary: "A reviewed removal can withdraw this connection-request permission." },
          evidence: { result_status: "ok", digests: [], raw_detail_available: true, redaction: "semantic_only" },
          activity: [], can_approve: false, can_revoke: false,
          authorizes_approval: false, authorizes_execution: false, persists_state: false,
        },
      } });
    } else {
      unexpected.push(`${request.method()} ${path}`);
      await route.fulfill({ status: 404, json: { error: { message: "Unexpected test request" } } });
    }
  });
  return { proposals, unexpected };
}

for (const interrupted of [false, true]) {
  test(`new client policy carries into setup${interrupted ? " after an interrupted response and reload" : ""}`, async ({ page }, testInfo) => {
    const { proposals, unexpected } = await setup(page, interrupted);
    await page.goto(entry);
    await page.getByRole("button", { name: "Check setup prerequisites", exact: true }).click();
    const form = page.getByRole("region", { name: "Prepare client access", exact: true });
    await form.getByLabel("Client name", { exact: true }).fill("My CLI client");
    await form.getByRole("combobox", { name: "Project", exact: true }).selectOption({ label: "example/project" });
    await expect(form.getByRole("combobox", { name: "Delivery branch", exact: true })).toHaveValue("main");
    if (!interrupted) {
      await form.screenshot({ path: testInfo.outputPath("client-access-form.png") });
    }
    await form.getByRole("button", { name: "Prepare client access for review" }).click();

    if (interrupted) {
      await expect(form.getByText(/could not confirm the setup request/)).toBeVisible();
      await expect(form.getByLabel("Client name", { exact: true })).toBeDisabled();
      await page.reload();
      await page.getByRole("button", { name: "Check setup prerequisites", exact: true }).click();
      await expect(form.getByLabel("Client name", { exact: true })).toHaveValue("My CLI client");
      await form.getByRole("button", { name: "Retry saved setup" }).click();
      await expect.poll(() => proposals.length).toBe(2);
      expect(proposals[1]).toEqual(proposals[0]);
    }

    await expect(page).toHaveURL(new RegExp(`policy_operation_id=${operationId}$`));
    await expect(page.getByRole("combobox", { name: "Project and branch", exact: true })).toHaveValue(operationId);
    await expect(page.getByRole("link", { name: "Review client access plan" })).toHaveAttribute("href", new RegExp(`operation_id=${operationId}$`));
    if (!interrupted) {
      await page.screenshot({ path: testInfo.outputPath("client-access-handoff.png"), fullPage: true });
    }
    expect(proposals[0].intent.principal_id).toMatch(/^agent_[a-f0-9]{32}$/);
    expect(proposals[0].source_event_id).toMatch(/^ui:ordinary-policy:/);
    expect(Object.keys(proposals[0]).sort()).toEqual(["intent", "schema_version", "source_event_id"]);
    expect(unexpected).toEqual([]);
    expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
    expect(await page.evaluate(() => sessionStorage.getItem("launchplane:ordinary-agent-policy-preparation:v1"))).toBeNull();
  });
}


test("a rejected saved setup can be discarded and edited", async ({ page }) => {
  const { proposals, unexpected } = await setup(page, false, true);
  await page.goto(entry);
  await page.getByRole("button", { name: "Check setup prerequisites", exact: true }).click();
  const form = page.getByRole("region", { name: "Prepare client access", exact: true });
  await form.getByLabel("Client name", { exact: true }).fill("First client");
  await form.getByRole("combobox", { name: "Project", exact: true }).selectOption({ label: "example/project" });
  await form.getByRole("button", { name: "Prepare client access for review" }).click();
  await expect(form.getByText("The selected delivery branch is no longer configured.")).toBeVisible();
  await form.getByRole("button", { name: "Discard saved setup", exact: true }).click();
  await form.getByLabel("Client name", { exact: true }).fill("Revised client");
  await form.getByRole("button", { name: "Prepare client access for review" }).click();
  await expect(page).toHaveURL(new RegExp(`policy_operation_id=${operationId}$`));
  expect(proposals).toHaveLength(2);
  expect(proposals[1].intent.client_label).toBe("Revised client");
  expect(proposals[1].intent.principal_id).not.toBe(proposals[0].intent.principal_id);
  expect(proposals[1].source_event_id).not.toBe(proposals[0].source_event_id);
  expect(unexpected).toEqual([]);
});

test("a saved plan that is not eligible explains how to recover", async ({ page }) => {
  const { unexpected } = await setup(page, false);
  await page.goto(`${entry}&policy_operation_id=unavailable-plan`);
  await expect(page.getByText(/The saved access plan is not available for delivery setup/)).toBeVisible();
  await expect(page.getByRole("link", { name: "Review client access plan" })).toHaveAttribute("href", /operation_id=unavailable-plan$/);
  await expect(page.getByRole("button", { name: "Review setup", exact: true })).toHaveCount(0);
  expect(unexpected).toEqual([]);
});

test("a missing terminal capability is prepared before client access", async ({ page }) => {
  const { proposals, unexpected } = await setup(page, false, false, "missing");
  await page.goto(entry);
  await page.getByRole("button", { name: "Check setup prerequisites", exact: true }).click();
  const form = page.getByRole("region", { name: "Prepare client access", exact: true });
  await expect(form.getByText("The configured trusted terminal cannot yet request a client connection.")).toBeVisible();
  await expect(form.getByLabel("Client name", { exact: true })).toHaveCount(0);
  await form.getByRole("button", { name: "Allow the trusted terminal to request a client connection" }).click();
  await expect(page).toHaveURL(/operation_id=privileged-operation-terminal-policy$/);
  await expect(page.getByRole("heading", { name: "Review terminal client connection requests" })).toBeVisible();
  await expect(page.getByText(/until this access is removed/)).toBeVisible();
  await expect(page.getByText(/separate administrator approval/)).toBeVisible();
  await expect(page.getByText("One trusted terminal; client connection requests only.")).toBeVisible();
  expect(proposals).toEqual([]);
  expect(unexpected).toEqual([]);
});

test("a saved client request remains recoverable when terminal readiness becomes unavailable", async ({ page }) => {
  const { proposals, unexpected } = await setup(page, true, false, ["ready", "unavailable"]);
  await page.goto(entry);
  await page.getByRole("button", { name: "Check setup prerequisites", exact: true }).click();
  const form = page.getByRole("region", { name: "Prepare client access", exact: true });
  await form.getByLabel("Client name", { exact: true }).fill("Interrupted client");
  await form.getByRole("combobox", { name: "Project", exact: true }).selectOption({ label: "example/project" });
  await form.getByRole("button", { name: "Prepare client access for review" }).click();
  await expect(form.getByText(/could not confirm the setup request/)).toBeVisible();
  await page.reload();
  await page.getByRole("button", { name: "Check setup prerequisites", exact: true }).click();
  await expect(form.getByText(/could not check terminal access/)).toBeVisible();
  await expect(form.getByLabel("Client name", { exact: true })).toHaveValue("Interrupted client");
  await expect(form.getByRole("button", { name: "Discard saved setup", exact: true })).toBeVisible();
  await form.getByRole("button", { name: "Retry saved setup" }).click();
  await expect.poll(() => proposals.length).toBe(2);
  expect(proposals[1]).toEqual(proposals[0]);
  expect(unexpected).toEqual([]);
});
