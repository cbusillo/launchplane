import { expect, test } from "./state";

const request = {
  status: "ok", trace_id: "test-read", product: "example-site", display_name: "Example Site", environment: "testing", can_submit: true,
  fields: [{ integration: "runtime_environment", binding_key: "SMTP_PASSWORD", label: "Mail credential", instructions: "Enter the account's app credential.", environments: ["testing"], request_revision: "a".repeat(64), submitted_at: "", submission_version_id: "" }],
};

test.beforeEach(async ({ page }) => {
  await page.route("**/v1/auth/session", route => route.fulfill({ json: { status: "ok", trace_id: "test-session", csrf_token: "test-csrf", identity: { login: "owner", github_id: 9001, name: "Site Owner", email: "", role: "read_only", organizations: [], teams: [] } } }));
});

test("Owner input clears before dispatch and a receipt survives reload without the value", async ({ page }) => {
  let received = false;
  const result = () => ({ ...request, fields: request.fields.map(field => ({ ...field, submitted_at: received ? "2026-09-24T12:00:00Z" : "", submission_version_id: received ? "submitted-version" : "" })) });
  await page.route("**/v1/owner-secret-inputs?*", route => route.fulfill({ json: result() }));
  await page.route("**/v1/owner-secret-inputs/submit", async route => {
    expect(route.request().postDataJSON().value).toBe("sample-app-credential");
    expect(route.request().headers()["x-csrf-token"]).toBe("test-csrf");
    await expect(page.getByLabel("Mail credential", { exact: true })).toHaveValue("");
    received = true;
    await route.fulfill({ json: result() });
  });
  await page.goto("/ui/owner-secrets?product=example-site&environment=testing");
  await page.getByLabel("Mail credential", { exact: true }).fill("sample-app-credential");
  await page.getByRole("button", { name: "Save credential" }).click();
  await expect(page.getByRole("status")).toBeVisible();
  await expect(page.locator("body")).not.toContainText("sample-app-credential");
  await page.reload();
  await expect(page.getByText("Last received:", { exact: false })).toBeVisible();
  await expect(page.getByLabel("Mail credential", { exact: true })).toHaveValue("");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test("unavailable Owner request exposes no input", async ({ page }) => {
  await page.route("**/v1/owner-secret-inputs?*", route => route.fulfill({ status: 403, json: { error: { code: "owner_secret_input_unavailable", message: "Unavailable" } } }));
  await page.goto("/ui/owner-secrets?product=example-site&environment=testing");
  await expect(page.getByRole("alert")).toContainText("This credential request is unavailable");
  await expect(page.locator('input[type="password"]')).toHaveCount(0);
});

test("Director saves both service bindings on the existing credential route", { tag: "@layout" }, async ({ page }) => {
  const received = new Set<string>();
  const serviceRequest = () => ({ ...request, application_required: false, product: "launchplane", environment: "ghcr_artifact_read", display_name: "Artifact scan credentials", fields: ["username", "password"].map((key, index) => ({
    ...request.fields[0], integration: "ghcr_artifact_read", binding_key: key,
    label: key === "username" ? "GHCR pull-only username" : "GHCR pull-only token",
    request_revision: String(index).repeat(64), environments: ["launchplane"],
    submitted_at: received.has(key) ? "2026-10-23T21:00:00Z" : "",
    submission_version_id: received.has(key) ? `version-${key}` : "",
  })) });
  await page.route("**/v1/auth/session", route => route.fulfill({ json: { status: "ok", trace_id: "director-session", csrf_token: "test-csrf", identity: { login: "director", github_id: 9004, name: "Director", email: "", role: "admin", organizations: [], teams: [] } } }));
  await page.route("**/v1/owner-secret-inputs?*", route => route.fulfill({ json: serviceRequest() }));
  await page.route("**/v1/owner-secret-inputs/submit", async route => {
    const payload = route.request().postDataJSON();
    const field = serviceRequest().fields.find(field => field.request_revision === payload.request_revision)!;
    expect(payload.product).toBe("launchplane");
    expect(payload.environment).toBe("ghcr_artifact_read");
    await expect(page.getByLabel(field.label, { exact: true })).toHaveValue("");
    received.add(field.binding_key);
    await route.fulfill({ json: serviceRequest() });
  });
  await page.goto("/ui/owner-secrets?product=launchplane&environment=ghcr_artifact_read");
  for (const label of ["GHCR pull-only username", "GHCR pull-only token"]) {
    const form = page.getByRole("heading", { name: label, exact: true }).locator("..");
    await page.getByLabel(label, { exact: true }).fill("synthetic-service-credential");
    await form.getByRole("button", { name: "Save credential" }).click();
    await expect(form.getByRole("status")).toBeVisible();
  }
  await page.reload();
  await expect(page.getByText("Last received:", { exact: false })).toHaveCount(2);
  await expect(page.locator("body")).not.toContainText("synthetic-service-credential");
  for (const input of await page.locator('input[type="password"]').all()) await expect(input).toHaveValue("");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});
