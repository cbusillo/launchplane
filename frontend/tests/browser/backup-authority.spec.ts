import { expect, test } from "@playwright/test";
for (const outcome of ["approved", "denied", "different-lane", "wrong-action", "credential", "changed", "uncertain", "uncertain-stale", "uncertain-denied", "stale", "readback-delayed", "read-only"]) {
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
    const requests: Array<{ key: string; body: any }> = []; const digest = "c".repeat(64); let ready = false; let releaseReadback: (() => Promise<void>) | null = null;
    await page.route("**/v1/auth/session", r => r.fulfill({ json: { status: "ok", trace_id: "session", csrf_token: "fixture-csrf", identity: { ...fixtures.identity, role: outcome === "read-only" ? "read_only" : "admin" } } }));
    await page.route("**/v1/products", r => r.fulfill({ json: { status: "ok", trace_id: "products", products: fixtures.products } }));
    await page.route("**/v1/products/atlas-commerce/environments/prod", r => r.fulfill({ json: { status: "ok", trace_id: "lane", environment: fixtures.environment } }));
    await page.route("**/promotion-status", r => r.fulfill({ status: 503, json: { error: { code: "fixture_unavailable", message: "Promotion is outside this isolated approval journey." } } }));
    await page.route("**/operational-readiness?*", r => r.fulfill({ status: 503, json: { error: { code: "fixture_unavailable", message: "No readiness evidence in this isolated approval journey." } } }));
    await page.route("**/v1/products/atlas-commerce/environments/prod/config-status", r => r.fulfill({ json: { status: "ok", trace_id: "requirements", config_status: {
      product: policy.product, context: policy.context, environment: policy.instance, production_backup_authorities: [{ promotion_action: policy.promotion_action }] } } }));
    await page.route("**/v1/product-profiles/atlas-commerce", r => r.fulfill({ json: { status: "ok", trace_id: "profile", profile: { lanes: [] } } }));
    await page.route("**/v1/production-backup-authority?*", r => {
      const answer = { json: { status: "ok", trace_id: "authority", authority: { ...policy, state: ready ? "ready" : "missing", ready, policy: ready ? { record_id: "fixture-policy-r1" } : null, targets: [], summary: "Fixture authority" } } };
      if (outcome === "readback-delayed" && ready) { releaseReadback = () => r.fulfill(answer); return; }
      return r.fulfill(answer);
    });
    await page.route("**/v1/production-backup-authority/apply", r => {
      expect(r.request().headers()["x-csrf-token"]).toBe("fixture-csrf");
      const body = r.request().postDataJSON(); requests.push({ key: r.request().headers()["idempotency-key"], body });
      if (outcome === "denied") return r.fulfill({ status: 403, json: { trace_id: "denied", error: { code: "authorization_denied", message: "Backup authority write is denied for this lane." } } });
      if (body.mode === "apply") {
        expect(body.reviewed_authority_digest).toBe(digest); expect(body.policy).toEqual(policy);
        if (["uncertain", "uncertain-stale", "uncertain-denied"].includes(outcome) && requests.filter(x => x.body.mode === "apply").length === 1) return r.abort();
        if (outcome === "uncertain-denied") return r.fulfill({ status: 403, json: { trace_id: "denied-retry", error: { code: "authorization_denied", message: "Grant changed after the lost response." } } });
        if (["stale", "uncertain-stale"].includes(outcome)) return r.fulfill({ status: 409, json: { trace_id: "stale", error: { code: "production_backup_authority_conflict", message: "Authority changed after review." } } });
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
    const loaded = outcome === "different-lane" ? { ...proposal, policy: { ...policy, context: "another-product" } } : outcome === "wrong-action" ? { ...proposal, policy: { ...policy, promotion_action: "wrong_promotion.execute" } } : proposal;
    await file.setInputFiles({ name: "private-proposal.json", mimeType: "application/json", buffer: Buffer.from(JSON.stringify(loaded)) });
    if (outcome === "credential") { await expect(panel.getByRole("alert")).toBeVisible(); await expect(approve).toBeDisabled(); expect(requests).toHaveLength(0); return; }
    if (["different-lane", "wrong-action"].includes(outcome)) { await expect(panel.getByRole("alert")).toBeVisible(); await expect(approve).toBeDisabled(); expect(requests).toHaveLength(0); return; }
    await expect(panel.getByText("missing", { exact: true })).toBeVisible();
    await panel.getByRole("button", { name: "Run backup authority dry run" }).click();
    if (outcome === "denied") { await expect(panel.locator('.operation-notice[data-phase="failed"]')).toBeVisible(); await expect(approve).toBeDisabled(); expect(requests.every(x => x.body.mode === "dry_run")).toBe(true); return; }
    await expect(approve).toBeEnabled(); await expect(panel.getByText(digest, { exact: true })).toBeVisible(); await expect(panel.getByText(new RegExp(`${policy.fast_snapshot.retention_count} snapshots`))).toBeVisible();
    if (outcome === "changed") { await file.setInputFiles({ name: "changed.json", mimeType: "application/json", buffer: Buffer.from(JSON.stringify({ ...proposal, policy: { ...policy, reason: "New reason" } })) }); await expect(approve).toBeDisabled(); expect(requests.every(x => x.body.mode === "dry_run")).toBe(true); return; }
    if (outcome === "approved") { await panel.scrollIntoViewIfNeeded(); await panel.screenshot({ path: testInfo.outputPath("backup-authority-reviewed.png") }); await page.screenshot({ path: testInfo.outputPath("backup-authority-viewport.png") }); expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true); }
    await approve.click();
    if (["uncertain", "uncertain-stale", "uncertain-denied"].includes(outcome)) {
      await expect(panel.getByRole("button", { name: "Retry original backup approval" })).toBeEnabled(); await expect(file).toBeDisabled(); await page.reload();
      panel = page.getByRole("region", { name: "Production backup authority", exact: true }); await panel.getByRole("button", { name: "Retry original backup approval" }).click();
      await expect.poll(() => requests.filter(x => x.body.mode === "apply").length).toBe(2); const applies = requests.filter(x => x.body.mode === "apply"); expect(applies[0]).toEqual(applies[1]);
    }
    if (["stale", "uncertain-stale"].includes(outcome)) { await expect(panel.locator('.operation-notice[data-phase="failed"]')).toBeVisible(); await expect(file).toBeEnabled(); await expect(panel.getByRole("button", { name: "Approve this product's backup policy", exact: true })).toBeDisabled(); return; }
    if (outcome === "uncertain-denied") {
      await expect(panel.getByRole("button", { name: "Retry original backup approval" })).toBeEnabled();
      await expect(file).toBeDisabled(); return;
    }
    if (outcome === "readback-delayed") {
      await expect.poll(() => releaseReadback !== null).toBe(true);
      await expect(file).toBeDisabled(); await expect(approve).toBeDisabled();
      await (releaseReadback as unknown as () => Promise<void>)();
    }
    await expect(panel.getByRole("status", { name: "Backup authority result", exact: true })).toBeVisible();
    expect(requests.filter(x => x.body.mode === "apply").every(x => x.key)).toBe(true);
  });
}
