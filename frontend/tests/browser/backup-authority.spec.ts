import { expect, test } from "@playwright/test";
for (const outcome of ["approved", "denied", "different-lane", "credential", "changed", "uncertain", "stale", "read-only"]) {
  test(`production backup proposal handles ${outcome}`, { tag: "@layout" }, async ({ page }, testInfo) => {
    await page.goto("/ui/products?fixture=empty");
    const fixtures = await page.evaluate(async () => {
      const m = await import("/ui/src/dev-fixtures.ts");
      return { identity: m.fixtureIdentity, products: m.productsForFixture("products"), environment: m.environmentForFixture("products", "atlas-commerce", "prod")! };
    });
    const policy = { product: fixtures.environment.product, context: fixtures.environment.context, instance: "prod", promotion_action: "generic_web_prod_promotion.execute", policy_revision: 1, status: "active",
      fast_snapshot: { source_target_id: "fixture-guest", snapshot_prefix: "fixture", retention_count: 3, max_evidence_age_seconds: 7200 },
      independent_backup: { source_target_id: "fixture-guest", destination_target_id: "fixture-pbs", max_evidence_age_seconds: 7200 },
      effective_at: "2026-10-01T00:00:00Z", review_after: "2027-01-01T00:00:00Z", source: "browser-test", reason: "Protect this product before release." };
    const base = { target_revision: 1, effective_at: policy.effective_at, review_after: policy.review_after, source: "browser-test", reason: policy.reason };
    const proposal = { mode: "dry_run", policy, targets: [
      { ...base, target_id: "fixture-guest", destination: { destination_kind: "proxmox_guest", host: "proxmox.example.invalid", username: "fixture-capture", guest_kind: "lxc", guest_id: "123" } },
      { ...base, target_id: "fixture-pbs", destination: { destination_kind: "proxmox_storage", host: "proxmox.example.invalid", username: "fixture-capture", storage_id: "pbs-fixture" } },
    ] };
    const requests: Array<{ key: string; body: any }> = []; const digest = "c".repeat(64); let ready = false;
    await page.route("**/v1/auth/session", r => r.fulfill({ json: { status: "ok", trace_id: "session", csrf_token: "fixture-csrf", identity: { ...fixtures.identity, role: outcome === "read-only" ? "read_only" : "admin" } } }));
    await page.route("**/v1/products", r => r.fulfill({ json: { status: "ok", trace_id: "products", products: fixtures.products } }));
    await page.route("**/v1/products/atlas-commerce/environments/prod", r => r.fulfill({ json: { status: "ok", trace_id: "lane", environment: fixtures.environment } }));
    await page.route("**/promotion-status", r => r.fulfill({ status: 503, json: { error: { code: "fixture_unavailable", message: "Promotion is outside this isolated approval journey." } } }));
    await page.route("**/operational-readiness?*", r => r.fulfill({ status: 503, json: { error: { code: "fixture_unavailable", message: "No readiness evidence in this isolated approval journey." } } }));
    await page.route("**/v1/product-profiles/atlas-commerce", r => r.fulfill({ json: { status: "ok", trace_id: "profile", profile: { lanes: [] } } }));
    await page.route("**/v1/production-backup-authority?*", r => r.fulfill({ json: { status: "ok", trace_id: "authority", authority: { ...policy, state: ready ? "ready" : "missing", ready, policy: ready ? { record_id: "fixture-policy-r1" } : null, targets: [], summary: "Fixture authority" } } }));
    await page.route("**/v1/production-backup-authority/apply", r => {
      expect(r.request().headers()["x-csrf-token"]).toBe("fixture-csrf");
      const body = r.request().postDataJSON(); requests.push({ key: r.request().headers()["idempotency-key"], body });
      if (outcome === "denied") return r.fulfill({ status: 403, json: { trace_id: "denied", error: { code: "authorization_denied", message: "Backup authority write is denied for this lane." } } });
      if (body.mode === "apply") {
        expect(body.reviewed_authority_digest).toBe(digest); expect(body.policy).toEqual(policy);
        if (outcome === "uncertain" && requests.filter(x => x.body.mode === "apply").length === 1) return r.abort();
        if (outcome === "stale") return r.fulfill({ status: 409, json: { trace_id: "stale", error: { code: "production_backup_authority_conflict", message: "Authority changed after review." } } });
        ready = true;
      }
      return r.fulfill({ json: { status: "ok", trace_id: "backup-review", result: { mode: body.mode, status: body.mode === "apply" ? "applied" : "would_apply", authority_digest: digest, policy: { ...policy, record_id: "fixture-policy-r1" }, targets: [] } } });
    });
    await page.goto("/ui/products/atlas-commerce/environments/prod/actions");
    let panel = page.getByRole("region", { name: "Production backup authority", exact: true });
    const file = panel.getByLabel("Prepared backup proposal", { exact: true });
    const approve = panel.getByRole("button", { name: "Approve this product's backup policy", exact: true });
    await expect(approve).toBeDisabled();
    if (outcome === "read-only") { await expect(file).toBeDisabled(); expect(requests).toHaveLength(0); return; }
    await expect(file).toBeEnabled();
    if (outcome === "credential") (proposal.targets[0].destination as Record<string, unknown>).private_key = "not-a-real-secret";
    const loaded = outcome === "different-lane" ? { ...proposal, policy: { ...policy, context: "another-product" } } : proposal;
    await file.setInputFiles({ name: "private-proposal.json", mimeType: "application/json", buffer: Buffer.from(JSON.stringify(loaded)) });
    if (outcome === "credential") { await expect(panel.getByRole("alert")).toContainText("without credentials"); await expect(approve).toBeDisabled(); expect(requests).toHaveLength(0); return; }
    if (outcome === "different-lane") { await expect(panel.getByRole("alert")).toContainText("different product"); await expect(approve).toBeDisabled(); expect(requests).toHaveLength(0); return; }
    await expect(panel.getByText("missing", { exact: true })).toBeVisible();
    await panel.getByRole("button", { name: "Run backup authority dry run" }).click();
    if (outcome === "denied") { await expect(panel.getByText("Backup authority write is denied for this lane.")).toBeVisible(); await expect(approve).toBeDisabled(); expect(requests.every(x => x.body.mode === "dry_run")).toBe(true); return; }
    await expect(approve).toBeEnabled(); await expect(panel.getByText(digest, { exact: true })).toBeVisible(); await expect(panel.getByText(/3 snapshots/)).toBeVisible();
    if (outcome === "changed") { await file.setInputFiles({ name: "changed.json", mimeType: "application/json", buffer: Buffer.from(JSON.stringify({ ...proposal, policy: { ...policy, reason: "New reason" } })) }); await expect(approve).toBeDisabled(); expect(requests.every(x => x.body.mode === "dry_run")).toBe(true); return; }
    if (outcome === "approved") { await panel.scrollIntoViewIfNeeded(); await panel.screenshot({ path: testInfo.outputPath("backup-authority-reviewed.png") }); await page.screenshot({ path: testInfo.outputPath("backup-authority-viewport.png") }); expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true); }
    await approve.click();
    if (outcome === "uncertain") {
      await expect(panel.getByRole("button", { name: "Retry original backup approval" })).toBeEnabled(); await expect(file).toBeDisabled(); await page.reload();
      panel = page.getByRole("region", { name: "Production backup authority", exact: true }); await panel.getByRole("button", { name: "Retry original backup approval" }).click();
      await expect.poll(() => requests.filter(x => x.body.mode === "apply").length).toBe(2); const applies = requests.filter(x => x.body.mode === "apply"); expect(applies[0]).toEqual(applies[1]);
    }
    if (outcome === "stale") { await expect(panel.getByText("Authority changed after review.")).toBeVisible(); await expect(panel.getByRole("button", { name: "Approve this product's backup policy", exact: true })).toBeDisabled(); return; }
    await expect(panel.getByRole("status").filter({ hasText: "Backup policy approved and read back" })).toBeVisible();
    expect(requests.filter(x => x.body.mode === "apply").every(x => x.key)).toBe(true);
  });
}
