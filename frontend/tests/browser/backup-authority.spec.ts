import { waitForState, expect, test } from "./state";
for (const outcome of ["approve", "decline", "reload", "second-tab", "uncertain", "stale", "read-only", "other-lane"]) {
  test(`server-held backup policy handles ${outcome}`, { tag: "@layout" }, async ({ page }, testInfo) => {
    await page.goto("/ui/products?fixture=empty");
    const fixtures = await page.evaluate(async () => {
      const m = await import("/ui/src/dev-fixtures.ts");
      return { identity: m.fixtureIdentity, products: m.productsForFixture("products"), environment: m.environmentForFixture("products", "atlas-commerce", "prod")! };
    });
    const operationId = `privileged-operation-${"a".repeat(32)}`;
    const protection = { product: fixtures.environment.product, context: fixtures.environment.context, instance: "prod", promotion_action: "generic_web_prod_promotion.execute",
      policy_revision: 1, retention_count: 3, snapshot_max_age_seconds: 7200, backup_max_age_seconds: 7200,
      effective_at: "2026-10-01T00:00:00Z", review_after: "2027-01-01T00:00:00Z", plan_digest: "c".repeat(64), result_status: "ok" };
    let status = "planned";
    const requests: Array<{ body: unknown }> = [];
    const proposal = () => ({ operation_id: operationId, status, protection: outcome === "other-lane" ? { ...protection, context: "another-context" } : protection, can_approve: status === "planned", can_decline: status === "planned" });
    const context = page.context();
    await context.route("**/v1/auth/session", r => r.fulfill({ json: { status: "ok", trace_id: "session", csrf_token: "fixture-csrf", identity: { ...fixtures.identity, role: outcome === "read-only" ? "read_only" : "admin" } } }));
    await context.route("**/v1/products", r => r.fulfill({ json: { status: "ok", trace_id: "products", products: fixtures.products } }));
    await context.route("**/v1/products/atlas-commerce/environments/prod", r => r.fulfill({ json: { status: "ok", trace_id: "lane", environment: fixtures.environment } }));
    await context.route("**/promotion-status", r => r.fulfill({ status: 503, json: { error: { code: "fixture_unavailable", message: "Outside this approval journey." } } }));
    await context.route("**/operational-readiness?*", r => r.fulfill({ status: 503, json: { error: { code: "fixture_unavailable", message: "No readiness evidence in this journey." } } }));
    await context.route("**/v1/product-profiles/atlas-commerce", r => r.fulfill({ json: { status: "ok", trace_id: "profile", profile: { lanes: [] } } }));
    await context.route("**/v1/production-backup-authority/proposals?*", r => r.fulfill({ json: { status: "ok", trace_id: "proposals", proposals: [proposal()] } }));
    await context.route("**/v1/production-backup-authority/proposals/*/decision", r => {
      expect(r.request().headers()["x-csrf-token"]).toBe("fixture-csrf");
      const body = r.request().postDataJSON(); requests.push({ body });
      expect(Object.keys(body).sort()).toEqual(["decision", "source_event_id"]);
      if (outcome === "stale") return r.fulfill({ status: 409, json: { trace_id: "stale", error: { code: "backup_proposal_conflict", message: "The authority changed; request a new proposal." } } });
      if (outcome === "uncertain" && requests.length === 1) return r.abort();
      status = body.decision === "approve" ? "executed" : "cancelled";
      return r.fulfill({ json: { status: "ok", trace_id: "decision", proposal: proposal() } });
    });
    const url = `/ui/products/atlas-commerce/environments/prod/actions?backup_proposal=${operationId}`;
    await page.goto(url);
    let activePage = page;
    if (outcome === "reload") await page.reload();
    if (outcome === "second-tab") { activePage = await context.newPage(); await activePage.goto(url); }
    let panel = activePage.getByRole("region", { name: "Production backup authority", exact: true });
    if (outcome === "other-lane") { await expect(panel.getByRole("alert")).toBeVisible(); expect(requests).toHaveLength(0); return; }
    await expect(panel.getByText("3 snapshots", { exact: true })).toBeVisible();
    expect(await panel.locator('input[type="file"]').count()).toBe(0);
    await expect(panel.getByText(protection.plan_digest, { exact: true })).toHaveCount(0);
    const approve = panel.getByRole("button", { name: "Approve", exact: true });
    if (outcome === "read-only") { await expect(approve).toBeDisabled(); expect(requests).toHaveLength(0); return; }
    await expect(approve).toBeEnabled();
    if (outcome === "approve") {
      await panel.scrollIntoViewIfNeeded();
      await panel.screenshot({ path: testInfo.outputPath("backup-authority-reviewed.png") });
      await activePage.screenshot({ path: testInfo.outputPath("backup-authority-viewport.png") });
      expect(await activePage.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    }
    await panel.getByRole("button", { name: outcome === "decline" ? "Decline" : "Approve", exact: true }).click();
    if (outcome === "stale") { await expect(panel.getByText("The authority changed; request a new proposal.")).toBeVisible(); expect(requests).toHaveLength(1); return; }
    if (outcome === "uncertain") {
      await expect(panel.getByRole("button", { name: "Retry original decision" })).toBeEnabled();
      await activePage.reload(); panel = activePage.getByRole("region", { name: "Production backup authority", exact: true });
      await panel.getByRole("button", { name: "Retry original decision" }).click();
      await waitForState(() => requests.length).toBe(2); expect(requests[0]).toEqual(requests[1]);
    }
    await expect(panel.getByRole("status", { name: "Backup authority result", exact: true })).toHaveText(outcome === "decline" ? "Backup policy proposal declined." : "Backup policy approved. No capture or release was started.");
    expect(requests).toHaveLength(outcome === "uncertain" ? 2 : 1);
  });
}
