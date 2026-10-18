import { expect, test, waitForState } from "./state";

for (const outcome of ["success", "lost-reply", "denied", "stale-build", "preflight-unavailable"] as const) {
  test(`testing rebuild keeps its address and original request after ${outcome} @responsive`, async ({ page }, testInfo) => {
    await page.goto("/ui/products?fixture=empty");
    const fixtures = await page.evaluate(async () => {
      const module = await import("/ui/src/dev-fixtures.ts");
      return { identity: module.fixtureIdentity, products: module.productsForFixture("products"), environment: module.environmentForFixture("products", "atlas-commerce", "testing")! };
    });
    const environment = fixtures.environment;
    environment.driver_id = "odoo";
    environment.driver_extensions.odoo = {
      allowed_rebuild_sources: ["empty", "production_restore", "upstream_restore"], data_authority: "disposable",
      destructive_replacement_proof_required: false, prelaunch_rebuild_allowed: true,
      prelaunch_rebuild_approval_issue_url: "", prelaunch_rebuild_data_source_mode: "empty",
      requires_runtime_identity: true, rollback_artifact_id: "", rollback_deployment_record_id: "", upstream_source: "configured",
    };
    const writes: { key: string; body: Record<string, unknown> }[] = [];
    let polled = false;
    await page.route("**/v1/**", route => route.fulfill({ status: 503, json: { error: { code: "fixture_unavailable", message: "Other evidence is unavailable in this isolated journey." } } }));
    await page.route("**/v1/auth/session", route => route.fulfill({ json: { status: "ok", trace_id: "session", csrf_token: "fixture-csrf", identity: { ...fixtures.identity, role: "admin" } } }));
    await page.route("**/v1/products", route => route.fulfill({ json: { status: "ok", trace_id: "products", products: fixtures.products } }));
    await page.route("**/v1/products/atlas-commerce/environments/testing", route => route.fulfill({ json: { status: "ok", trace_id: "lane", environment } }));
    await page.route("**/v1/products/atlas-commerce/testing-rebuild/inputs?*", route => outcome === "preflight-unavailable"
      ? route.fulfill({ status: 503, json: { trace_id: "preflight-unavailable", error: { code: "testing_preflight_unavailable", message: "Testing inspection is unavailable." } } })
      : route.fulfill({ json: {
      status: "pass", product: environment.product, context: environment.context, instance: "testing", base_url: environment.base_url,
      artifact_id: "current-verified-build", source_git_ref: "c".repeat(40), data_source_mode: new URL(route.request().url()).searchParams.get("data_source_mode"),
      observed_at: new Date().toISOString(), candidate_verification: "pending",
    } }));
    const receipt = (status: string) => ({ trace_id: "rebuild-trace", operation: { product: environment.product, instance: "testing", operation_id: "native-original", status, phase: status === "pass" ? "completed" : "running", updated_at: new Date().toISOString() } });
    await page.route("**/v1/products/atlas-commerce/testing-rebuild", async route => {
      expect(route.request().headers()["x-csrf-token"]).toBe("fixture-csrf");
      writes.push({ key: route.request().headers()["idempotency-key"], body: route.request().postDataJSON() });
      if (outcome === "lost-reply" && writes.length === 1) return route.abort();
      if (outcome === "denied") return route.fulfill({ status: 403, json: { trace_id: "denied-trace", error: { code: "authorization_denied", message: "This request is denied." } } });
      if (outcome === "stale-build") return route.fulfill({ status: 409, json: { trace_id: "stale-trace", error: { code: "testing_rebuild_not_ready", message: "The inspected build changed. Inspect again." } } });
      return route.fulfill({ json: receipt("pending") });
    });
    await page.route("**/v1/products/atlas-commerce/testing-rebuild/operations/native-original", route => { polled = true; return route.fulfill({ json: receipt("pass") }); });
    await page.goto("/ui/products/atlas-commerce/environments/testing/actions");
    const panel = page.getByRole("region", { name: "Rebuild testing at the same address" });
    await expect(panel).toBeVisible();
    if (outcome === "preflight-unavailable") {
      await expect(panel.getByText("Testing inspection is unavailable.")).toBeVisible();
      await expect(panel.getByRole("button", { name: "Rebuild testing", exact: true })).toBeDisabled();
      expect(writes.length).toBe(0);
      await panel.screenshot({ path: testInfo.outputPath("testing-rebuild-unavailable.png") });
      return;
    }
    const selectedSource = outcome === "success" ? "empty" : outcome === "stale-build" ? "production_restore" : "upstream_restore";
    if (selectedSource !== "empty") await panel.getByLabel("Start with").selectOption(selectedSource);
    await expect(panel.getByText("Latest verified build selected.", { exact: false })).toBeVisible();
    if (outcome === "lost-reply") {
      await panel.getByText("Reason (optional edit)", { exact: true }).click();
      await panel.getByLabel("Reason", { exact: true }).fill("Prepare a fresh testing copy.");
    }
    await expect(panel.getByRole("button", { name: "Rebuild testing", exact: true })).toBeEnabled();
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({ path: testInfo.outputPath("testing-rebuild-viewport.png") });
    await panel.screenshot({ path: testInfo.outputPath("testing-rebuild-ready.png") });
    await panel.getByRole("button", { name: "Rebuild testing", exact: true }).click();
    if (outcome === "lost-reply") {
      await expect(panel.getByRole("button", { name: "Recover this request" })).toBeEnabled();
      await page.reload();
      await expect(panel.getByRole("button", { name: "Recover this request" })).toBeEnabled();
      expect(writes.length).toBe(1);
      await panel.getByRole("button", { name: "Recover this request" }).click();
      await waitForState(() => writes.length).toBe(2);
      expect(writes[1]).toEqual(writes[0]);
    }
    if (outcome === "stale-build") {
      await expect(panel.getByRole("status")).toContainText("Review the fresh inspection");
      await expect(panel.getByRole("button", { name: "Rebuild testing", exact: true })).toBeEnabled();
      expect(writes.length).toBe(1);
      expect(polled).toBe(false);
    } else if (outcome === "denied") {
      await expect(panel.getByText("This request was denied and will not be submitted again.", { exact: false })).toBeVisible();
      await page.reload();
      await expect(panel.getByRole("button", { name: "Recover this request" })).toHaveCount(0);
      expect(writes.length).toBe(1);
      expect(polled).toBe(false);
    } else {
      await waitForState(() => page.evaluate(() => JSON.parse(sessionStorage.getItem("launchplane:testing-rebuild:atlas-commerce") ?? "null")?.operationId)).toBe("native-original");
      await page.clock.runFor(2500);
      await expect(panel.getByRole("status")).toContainText("Testing rebuilt and verified healthy at the same address.");
      expect(polled).toBe(true);
      expect(writes[0].body.data_source_mode).toBe(selectedSource);
      expect(writes[0].body).not.toHaveProperty("instance");
    }
    await expect(panel).toContainText(environment.base_url);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await panel.screenshot({ path: testInfo.outputPath("testing-rebuild-result.png") });
  });
}
