import { expect, test, waitForState } from "./state";

for (const outcome of ["success", "uncertain", "uncertain-stale", "uncertain-staging", "key-storage-unavailable", "stale", "denied", "read-only", "narrow-admin"]) {
  test(`testing hold lift handles ${outcome}`, { tag: "@layout" }, async ({ page }, testInfo) => {
    await page.goto("/ui/products?fixture=empty");
    const fixtures = await page.evaluate(async () => {
      const module = await import("/ui/src/dev-fixtures.ts");
      return { identity: module.fixtureIdentity, products: module.productsForFixture("products"),
        environment: module.environmentForFixture("products", "atlas-commerce", "testing")! };
    });
    const scope = { product: "atlas-commerce", context: fixtures.environment.context, instance: "testing" };
    const receiptTrace = "lift-receipt";
    const original = { reason: "Renderer diagnosis. https://issues.example/diagnosis/42", recorded_by: "diagnosis-agent", recorded_at: "2026-10-20T20:00:00Z" };
    let hold: typeof original & { purpose?: "staff_testing" | "isolation_staging" } | null = original;
    const administrator = !["read-only", "narrow-admin"].includes(outcome);
    let sessionReads = 0;
    await page.route("**/v1/auth/session", route => { sessionReads += 1; return route.fulfill({ json: {
      status: "ok", trace_id: "session", csrf_token: "fixture-csrf", is_administrator: administrator,
      identity: { ...fixtures.identity, role: outcome === "read-only" ? "read_only" : "admin" },
    } }); });
    await page.route("**/v1/products", route => route.fulfill({ json: { status: "ok", trace_id: "products", products: fixtures.products } }));
    await page.route("**/v1/products/atlas-commerce/environments/testing", route => route.fulfill({ json: { status: "ok", trace_id: "lane", environment: fixtures.environment } }));
    await page.route("**/public-ingress/incidents", route => route.fulfill({ json: { status: "ok", trace_id: "incidents", incidents: [] } }));
    let failNextHoldRead = false;
    await page.route("**/v1/product-config/testing-hold?*", route => {
      if (failNextHoldRead) {
        failNextHoldRead = false;
        return route.fulfill({ status: 503, json: { error: { code: "hold_read_unavailable", message: "Hold evidence is temporarily unavailable." } } });
      }
      return route.fulfill({ json: { status: "accepted", trace_id: "hold-read", records: {}, result: { ...scope, hold, record_sha256: "b".repeat(64) } } });
    });
    const applies: Array<{ key: string; body: unknown }> = [];
    await page.route("**/v1/product-config/testing-hold/apply", async route => {
      const body = route.request().postDataJSON();
      expect(route.request().headers()["x-csrf-token"]).toBe("fixture-csrf");
      expect(body.hold).toBe(false);
      expect(body.rehearsal).not.toBe(true);
      if (body.mode === "apply") {
        applies.push({ key: route.request().headers()["idempotency-key"], body });
        expect(body.reviewed_plan_sha256).toBe("a".repeat(64));
        if (outcome.startsWith("uncertain") && applies.length === 1) {
          if (outcome === "uncertain-staging") hold = { ...original, purpose: "isolation_staging" };
          if (outcome === "uncertain-stale") failNextHoldRead = true;
          await route.abort(); return;
        }
        if (["uncertain-stale", "uncertain-staging"].includes(outcome) && applies.length === 2) {
          await route.fulfill({ status: 409, json: { status: "rejected", trace_id: "stale-original", error: { code: "stale", message: "The current hold changed." } } }); return;
        }
        if (outcome === "stale" || outcome === "denied") {
          await route.fulfill({ status: outcome === "stale" ? 409 : 403, json: { status: "rejected", trace_id: "refused", error: { code: outcome === "stale" ? "stale" : "authorization_denied", message: "The reviewed hold could not be lifted." } } }); return;
        }
        hold = null;
      }
      await route.fulfill({ json: { status: "accepted", trace_id: receiptTrace, records: {}, result: {
        ...scope, action: "clear", changed: true, applied: body.mode === "apply", before: original,
        plan_sha256: "a".repeat(64), reason: body.reason, read_back_matches: true, reconcile_requested: body.mode === "apply",
      } } });
    });
    await page.goto("/ui/products/atlas-commerce/environments/testing");
    if (!administrator) {
      await expect(page.getByRole("heading", { name: "Testing", exact: true })).toBeVisible();
      await waitForState(() => sessionReads).toBeGreaterThanOrEqual(2);
      // Let the session promise and its React render finish before asserting absence.
      await page.clock.runFor(1_000);
      await expect(page.getByRole("region", { name: "Testing hold", exact: true })).toHaveCount(0);
      expect(applies).toEqual([]); return;
    }
    let panel = page.getByRole("region", { name: "Testing hold", exact: true });
    await expect(panel.getByText(original.reason)).toBeVisible();
    await expect(panel.getByText(original.recorded_by)).toBeVisible();
    await expect(panel.getByRole("link", { name: "Linked issue" })).toHaveAttribute("href", "https://issues.example/diagnosis/42");
    await panel.getByLabel("Reason for lifting").fill("Diagnosis fixed; resume testing.");
    await panel.getByRole("button", { name: "Review lift", exact: true }).click();
    await expect(panel.getByRole("button", { name: "Lift testing hold", exact: true })).toBeDisabled();
    await panel.getByRole("checkbox").check();
    if (outcome === "key-storage-unavailable") {
      await page.evaluate(() => {
        const originalSet = Storage.prototype.setItem;
        Storage.prototype.setItem = function(key, value) {
          let operation: { phase?: string; identity?: { idempotencyKey?: string; requestFingerprint?: string } } | null = null;
          try { operation = JSON.parse(value); } catch { /* Other storage values are unaffected. */ }
          if (operation?.phase === "submitting" && operation.identity?.idempotencyKey && operation.identity.requestFingerprint) {
            throw new DOMException("Storage full", "QuotaExceededError");
          }
          return originalSet.call(this, key, value);
        };
      });
    }
    await panel.screenshot({ path: testInfo.outputPath("testing-hold-reviewed.png") });
    await panel.getByRole("button", { name: "Lift testing hold", exact: true }).click();
    if (outcome === "key-storage-unavailable") {
      await expect(panel.getByRole("status").first()).toBeVisible();
      expect(applies).toEqual([]); return;
    }
    if (outcome.startsWith("uncertain")) {
      await expect(panel.getByRole("button", { name: "Retry original lift", exact: true })).toBeEnabled();
      if (outcome === "uncertain-stale") {
        await expect(panel.getByRole("alert")).toBeVisible();
        await panel.getByRole("button", { name: "Refresh hold", exact: true }).click();
        await expect(panel.getByRole("button", { name: "Review current hold separately", exact: true })).toBeEnabled();
        expect(applies).toHaveLength(1);
      }
      await page.reload();
      panel = page.getByRole("region", { name: "Testing hold", exact: true });
      await panel.getByRole("button", { name: "Retry original lift", exact: true }).click();
      await waitForState(() => applies.length).toBe(2);
      expect(applies[1]).toEqual(applies[0]);
      if (["uncertain-stale", "uncertain-staging"].includes(outcome)) {
        await panel.getByRole("button", { name: "Review current hold separately", exact: true }).click();
        await expect(panel.getByText(applies[0].key, { exact: true })).toBeVisible();
        if (outcome === "uncertain-staging") {
          await expect(panel.getByRole("button", { name: "Review lift", exact: true })).toHaveCount(0);
          await expect(panel.getByRole("button", { name: "Lift testing hold", exact: true })).toHaveCount(0);
          expect(hold?.purpose).toBe("isolation_staging"); return;
        }
        await page.reload();
        panel = page.getByRole("region", { name: "Testing hold", exact: true });
        await expect(panel.getByText(applies[0].key, { exact: true })).toBeVisible();
        await panel.getByLabel("Reason for lifting").fill("Review the current hold separately.");
        await panel.getByRole("button", { name: "Review lift", exact: true }).click();
        await panel.getByRole("checkbox").check();
        await panel.getByRole("button", { name: "Lift testing hold", exact: true }).click();
        await waitForState(() => applies.length).toBe(3);
        expect(applies[2].key).not.toBe(applies[0].key);
      }
    }
    if (outcome === "stale" || outcome === "denied") {
      await expect(panel.getByText("The reviewed hold could not be lifted.", { exact: false }).first()).toBeVisible();
      await expect(panel.getByRole("button", { name: "Lift testing hold", exact: true })).toHaveCount(0);
      expect(hold).toEqual(original);
    } else {
      await expect(panel.getByRole("status").filter({ hasText: receiptTrace })).toBeVisible();
      expect(hold).toBeNull();
    }
    expect(applies[0].key).toBeTruthy();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  });
}
