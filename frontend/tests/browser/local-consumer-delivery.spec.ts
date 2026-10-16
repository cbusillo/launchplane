import { expect, test, waitForState } from "./state";
import { readFile } from "node:fs/promises";
import type { ConsumerDeliveryEnvelope } from "../../src/generated/openapi";

test("testing recipient review preserves its original encrypted delivery after an uncertain response", { tag: "@layout" }, async ({ page }, testInfo) => {
  await page.goto("/ui/products/atlas-commerce/environments/testing/managed-secrets?fixture=products");
  const data = await page.evaluate(async () => {
    const f = await import("/ui/src/dev-fixtures.ts");
    return { products: f.productsForFixture("products"), identity: f.fixtureIdentity,
      config: f.configStatusForFixture("products", "atlas-commerce", "testing"),
      environment: f.environmentForFixture("products", "atlas-commerce", "testing") };
  });
  const plans: ConsumerDeliveryEnvelope[] = [];
  const applies: ConsumerDeliveryEnvelope[] = [];
  await page.route("**/v1/**", async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/v1/local-consumer-deliveries") {
      const body = route.request().postDataJSON() as ConsumerDeliveryEnvelope;
      if (body.mode === "apply") {
        applies.push(body);
        if (applies.length === 1) {
          await route.fulfill({ status: 502, json: { error: { code: "gateway_error", message: "Unavailable" } } });
          return;
        }
      } else plans.push(body);
      await route.fulfill({ json: { trace_id: "delivery-fixture", request_fingerprint: "a".repeat(64),
        plan_digest: "b".repeat(64), ready_to_deliver: plans.length > 1, version_id: "testing-version",
        package: body.mode === "apply" ? { request: body.request, ciphertext: "encrypted-only" } : null } });
      return;
    }
    const response = path.endsWith("/auth/session") ? { csrf_token: "csrf", identity: data.identity }
      : path.endsWith("/secret-bindings") ? { bindings: [] }
      : path.endsWith("/owner-secret-inputs") ? { fields: [], can_submit: false }
      : path.endsWith("/config-status") ? { config_status: data.config }
      : path.endsWith("/environments/testing") ? { environment: data.environment }
      : path.endsWith("/products") ? { products: data.products } : { product: data.products[0] };
    await route.fulfill({ json: { status: "ok", trace_id: "read", ...response } });
  });
  await page.goto("/ui/products/atlas-commerce/environments/testing/managed-secrets");
  await page.getByRole("heading", { name: "Deliver to a testing client" }).waitFor();
  const request = { schema_version: 1, kind: "agent_session_token_file_v1", request_id: "c".repeat(64),
    product: "atlas-commerce", context: data.config.context, environment: "testing",
    service_origin: new URL(page.url()).origin, endpoint_key: "testing-endpoint",
    server_url: "wss://testing.internal.example/agent-session/connect", directory: "/private/testing-client",
    public_key: "fixture-public-recipient" };
  const recipient = page.getByLabel("Local recipient request");
  await recipient.setInputFiles({ name: "request.json", mimeType: "application/json", buffer: Buffer.from(JSON.stringify({ ...request, environment: "prod" })) });
  await expect(page.getByRole("alert")).toBeVisible();
  await expect(page.getByRole("button", { name: "Run delivery dry-run" })).toHaveCount(0);
  await recipient.setInputFiles({ name: "request.json", mimeType: "application/json", buffer: Buffer.from(JSON.stringify(request)) });
  await page.getByRole("button", { name: "Run delivery dry-run" }).click();
  await expect(page.getByRole("button", { name: "Recheck original destination" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Deliver to this testing client" })).toHaveCount(0);
  await page.getByRole("button", { name: "Recheck original destination" }).click();
  const deliver = page.getByRole("button", { name: "Deliver to this testing client" });
  await expect(deliver).toBeDisabled();
  const panel = page.locator("section").filter({ has: page.getByRole("heading", { name: "Deliver to a testing client" }) }).last();
  await panel.getByRole("checkbox").check();
  await deliver.click();
  await expect(panel.getByRole("alert")).toBeVisible();
  await expect(recipient).toBeDisabled();
  await deliver.click();
  await waitForState(() => applies.length).toBe(2);
  expect(applies[1]).toEqual(applies[0]);
  expect(plans[1].request).toEqual(plans[0].request);
  await expect(page.getByRole("button", { name: "Download encrypted delivery" })).toBeVisible();
  const downloaded = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download encrypted delivery" }).click();
  const download = await downloaded;
  const downloadedPath = await download.path();
  expect(downloadedPath).not.toBeNull();
  expect(JSON.parse(await readFile(downloadedPath!, "utf8")).request).toEqual(applies[0].request);
  await panel.scrollIntoViewIfNeeded();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("testing-encrypted-delivery.png") });
});

test("production has no local consumer delivery control", async ({ page }) => {
  await page.goto("/ui/products/atlas-commerce/environments/prod/managed-secrets?fixture=products");
  await page.getByRole("heading", { name: "Managed secrets", level: 1, exact: true }).waitFor();
  await expect(page.getByRole("heading", { name: "Deliver to a testing client" })).toHaveCount(0);
});
