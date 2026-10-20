import { expect, test, waitForState } from "./state";

for (const outcome of ["success", "lost-reply", "recovery-denied", "lookup-not-observed", "session-unavailable", "session-unauthenticated", "watch-denied", "denied", "stale-build", "preflight-unavailable", "held-resume", "held-resume-denied", "held-resume-lost-reply", "settlement-excluded", "settlement-lost-reply", "settlement-denied", "settlement-observed", "replay-conflict-settlement", "settlement-mismatch"] as const) {
  test(`testing rebuild keeps its address and original request after ${outcome} @responsive`, async ({ page }, testInfo) => {
    await page.goto("/ui/products?fixture=empty");
    const fixtures = await page.evaluate(async () => {
      const module = await import("/ui/src/dev-fixtures.ts");
      return { identity: module.fixtureIdentity, products: module.productsForFixture("products"), environment: module.environmentForFixture("products", "atlas-commerce", "testing")! };
    });
    const environment = fixtures.environment;
    const readAttempt = () => page.evaluate(async product => {
      const { readTestingRebuildAttempt } = await import("/ui/src/testing-rebuild-operation.ts");
      return readTestingRebuildAttempt(product);
    }, environment.product);
    environment.driver_id = "odoo";
    environment.driver_extensions.odoo = {
      allowed_rebuild_sources: ["empty", "production_restore", "upstream_restore"], data_authority: "disposable",
      destructive_replacement_proof_required: false, prelaunch_rebuild_allowed: true,
      prelaunch_rebuild_approval_issue_url: "", prelaunch_rebuild_data_source_mode: "empty",
      requires_runtime_identity: true, rollback_artifact_id: "", rollback_deployment_record_id: "", upstream_source: "configured",
    };
    const writes: { key: string; body: Record<string, unknown> }[] = [];
    const selectedCommit = "c".repeat(40);
    const csrfToken = "fixture-csrf";
    const originalScope = "fixture-original-administrator";
    const settling = outcome.startsWith("settlement-") || outcome === "replay-conflict-settlement";
    let settlements = 0;
    let polled = false;
    let reads = 0;
    let lookups = 0;
    let sessionUnavailable = false;
    const heldOutcome = outcome.startsWith("held-resume");
    const heldUpdatedAt = new Date().toISOString();
    const heldAttempt = 1;
    const resumes: Record<string, unknown>[] = [];
    let resumed = false;
    await page.route("**/v1/**", route => route.fulfill({ status: 503, json: { error: { code: "fixture_unavailable", message: "Other evidence is unavailable in this isolated journey." } } }));
    await page.route("**/v1/auth/session", route => sessionUnavailable
      ? route.fulfill({ status: outcome === "session-unauthenticated" ? 401 : 503, json: { trace_id: "session-unavailable", error: { code: "session_unavailable", message: "Session lookup unavailable." } } })
      : route.fulfill({ json: { status: "ok", trace_id: "session", csrf_token: csrfToken, identity: { ...fixtures.identity, role: "admin" } } }));
    await page.route("**/v1/products", route => route.fulfill({ json: { status: "ok", trace_id: "products", products: fixtures.products } }));
    await page.route("**/v1/products/atlas-commerce/environments/testing", route => route.fulfill({ json: { status: "ok", trace_id: "lane", environment } }));
    await page.route("**/v1/products/atlas-commerce/testing-rebuild/inputs?*", route => outcome === "preflight-unavailable"
      ? route.fulfill({ status: 503, json: { trace_id: "preflight-unavailable", error: { code: "testing_preflight_unavailable", message: "Testing inspection is unavailable." } } })
      : route.fulfill({ json: {
      status: "pass", product: environment.product, context: environment.context, instance: "testing", base_url: environment.base_url,
      artifact_id: "current-verified-build", source_git_ref: selectedCommit, data_source_mode: new URL(route.request().url()).searchParams.get("data_source_mode"),
      observed_at: new Date().toISOString(), candidate_verification: "pending",
      original_scope: originalScope,
    } }));
    const receipt = (status: string) => ({ trace_id: "rebuild-trace", operation: { product: environment.product, instance: "testing", operation_id: "native-original", status, phase: status === "pass" ? "completed" : "running", updated_at: status === "reconciliation_required" ? heldUpdatedAt : new Date().toISOString(), attempt: heldAttempt } });
    await page.route("**/v1/products/atlas-commerce/testing-rebuild", async route => {
      expect(route.request().headers()["x-csrf-token"]).toBe(csrfToken);
      writes.push({ key: route.request().headers()["idempotency-key"], body: route.request().postDataJSON() });
      if ((["lost-reply", "recovery-denied", "lookup-not-observed"].includes(outcome) || settling) && writes.length === 1) return route.abort();
      if (outcome === "replay-conflict-settlement") return route.fulfill({ status: 409, json: { trace_id: "replay-conflict", error: { code: "testing_rebuild_not_ready", message: "The inspected build changed." } } });
      if (settling) return route.fulfill({ status: 403, json: { trace_id: "recovery-denial", error: { code: "authorization_denied", message: "Recovery was denied." } } });
      if (["denied", "recovery-denied", "lookup-not-observed"].includes(outcome)) return route.fulfill({ status: 403, json: { trace_id: "denied-trace", error: { code: "authorization_denied", message: "This request is denied." } } });
      if (outcome === "stale-build") return route.fulfill({ status: 409, json: { trace_id: "stale-trace", error: { code: "testing_rebuild_not_ready", message: "The inspected build changed. Inspect again." } } });
      return route.fulfill({ json: receipt("pending") });
    });
    await page.route("**/v1/products/atlas-commerce/testing-rebuild/settle", route => {
      settlements++;
      expect(route.request().headers()["x-csrf-token"]).toBe(csrfToken);
      expect({ key: route.request().headers()["idempotency-key"], body: route.request().postDataJSON() }).toEqual(writes[0]);
      if (outcome === "settlement-lost-reply" && settlements === 1) return route.abort();
      if (outcome === "settlement-denied" && settlements === 1) return route.fulfill({ status: 403, json: { trace_id: "settlement-denial", error: { code: "authorization_denied", message: "Settlement is denied." } } });
      const originalRequest = { ...writes[0].body };
      if (outcome === "settlement-mismatch" && settlements === 1) originalRequest.original_scope = "foreign-actor";
      return route.fulfill({ json: {
        trace_id: "settlement-trace", product: environment.product, original_key: writes[0].key,
        original_request: originalRequest, outcome: outcome === "settlement-observed" ? "observed" : "excluded",
        operation: outcome === "settlement-observed" ? receipt("running").operation : null,
      } });
    });
    await page.route("**/v1/products/atlas-commerce/testing-rebuild/lookup", route => {
      lookups++;
      expect(route.request().headers()["x-csrf-token"]).toBe(csrfToken);
      expect({ key: route.request().headers()["idempotency-key"], body: route.request().postDataJSON() }).toEqual(writes[0]);
      if (outcome === "lookup-not-observed" && lookups === 1) return route.fulfill({ status: 404, json: { trace_id: "not-observed", error: { code: "testing_rebuild_not_observed", message: "The original operation is not observed yet." } } });
      return route.fulfill({ json: receipt("pending") });
    });
    await page.route("**/v1/products/atlas-commerce/testing-rebuild/operations/native-original", route => {
      polled = true; reads++;
      if (heldOutcome && !resumed) return route.fulfill({ json: receipt("reconciliation_required") });
      if (outcome === "watch-denied" && reads === 1) return route.fulfill({ status: 403, json: { trace_id: "read-denied", error: { code: "authorization_denied", message: "Watching is temporarily denied." } } });
      return route.fulfill({ json: receipt("pass") });
    });
    await page.route("**/v1/products/atlas-commerce/testing-rebuild/operations/native-original/resume", route => {
      expect(route.request().headers()["x-csrf-token"]).toBe(csrfToken);
      expect(route.request().headers()["idempotency-key"]).toBe(writes[0].key);
      const payload = route.request().postDataJSON();
      resumes.push(payload);
      expect(payload).toEqual({ expected_updated_at: heldUpdatedAt, expected_attempt: heldAttempt });
      if (outcome === "held-resume-denied" && resumes.length === 1) return route.fulfill({ status: 403, json: { trace_id: "resume-denial", error: { code: "authorization_denied", message: "Resuming this request is denied." } } });
      resumed = true;
      if (outcome === "held-resume-lost-reply") return route.abort();
      return route.fulfill({ json: receipt("pending") });
    });
    await page.goto("/ui/products/atlas-commerce/environments/testing/actions");
    const panel = page.getByRole("region", { name: "Rebuild testing at the same address" });
    await expect(panel).toBeVisible();
    if (outcome === "preflight-unavailable") {
      await expect(panel.getByRole("button", { name: "Refresh inspection" })).toBeEnabled();
      await expect(panel.getByRole("button", { name: "Rebuild testing", exact: true })).toBeDisabled();
      expect(writes.length).toBe(0);
      await panel.screenshot({ path: testInfo.outputPath("testing-rebuild-unavailable.png") });
      return;
    }
    const selectedSource = outcome === "success" ? "empty" : outcome === "stale-build" ? "production_restore" : "upstream_restore";
    if (selectedSource !== "empty") await panel.getByLabel("Start with").selectOption(selectedSource);
    await expect(panel).toContainText(selectedCommit.slice(0, 12));
    if (["lost-reply", "recovery-denied", "lookup-not-observed"].includes(outcome) || settling) {
      await panel.getByText("Reason (optional edit)", { exact: true }).click();
      await panel.getByLabel("Reason", { exact: true }).fill("Prepare a fresh testing copy.");
    }
    await expect(panel.getByRole("button", { name: "Rebuild testing", exact: true })).toBeEnabled();
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({ path: testInfo.outputPath("testing-rebuild-viewport.png") });
    await panel.screenshot({ path: testInfo.outputPath("testing-rebuild-ready.png") });
    sessionUnavailable = ["session-unavailable", "session-unauthenticated"].includes(outcome);
    await panel.getByRole("button", { name: "Rebuild testing", exact: true }).click();
    if (["lost-reply", "recovery-denied", "lookup-not-observed"].includes(outcome) || settling) {
      await expect(panel.getByRole("button", { name: "Recover this request" })).toBeEnabled();
      await page.reload();
      await expect(panel.getByRole("button", { name: "Recover this request" })).toBeEnabled();
      expect(writes.length).toBe(1);
      await panel.getByRole("button", { name: "Recover this request" }).click();
      await waitForState(() => writes.length).toBe(2);
      expect(writes[1]).toEqual(writes[0]);
      if (settling) {
        const settle = panel.getByRole("button", { name: "Settle this request", exact: true });
        await expect(settle).toBeEnabled();
        await settle.click();
        if (["settlement-lost-reply", "settlement-denied", "settlement-mismatch"].includes(outcome)) {
          await expect(settle).toBeEnabled();
          await waitForState(async () => (await readAttempt())?.readOnlyRecovery).toBe(true);
          expect((await readAttempt())?.key).toBe(writes[0].key);
          expect((await readAttempt())?.payload.original_scope).toBe(originalScope);
          expect(settlements).toBe(1);
          await expect(panel.getByRole("button", { name: "Rebuild testing", exact: true })).toHaveCount(0);
          await panel.screenshot({ path: testInfo.outputPath("testing-rebuild-settlement-retained.png") });
          await page.reload();
          await expect(settle).toBeEnabled();
          expect(settlements).toBe(1);
          await settle.click();
        }
        if (outcome === "settlement-observed") {
          await expect(panel.getByRole("button", { name: "Resume watching this rebuild", exact: true })).toBeEnabled();
          expect((await readAttempt())?.operationId).toBe("native-original");
          await panel.getByRole("button", { name: "Resume watching this rebuild", exact: true }).click();
        }
        await waitForState(readAttempt).toBe(null);
        await expect(panel.getByRole("button", { name: "Rebuild testing", exact: true })).toBeEnabled();
        expect(writes.length).toBe(2);
        await panel.screenshot({ path: testInfo.outputPath("testing-rebuild-settled.png") });
        return;
      }
      if (["recovery-denied", "lookup-not-observed"].includes(outcome)) {
        await expect(panel.getByRole("button", { name: "Find the original rebuild" })).toBeEnabled();
        await panel.screenshot({ path: testInfo.outputPath("testing-rebuild-read-only-recovery.png") });
        await page.reload();
        await panel.getByRole("button", { name: "Find the original rebuild" }).click();
        await waitForState(() => lookups).toBe(1);
        expect(writes.length).toBe(2);
        if (outcome === "lookup-not-observed") {
          await waitForState(async () => (await readAttempt())?.readOnlyRecovery).toBe(true);
          await expect(panel.getByRole("button", { name: "Rebuild testing", exact: true })).toHaveCount(0);
          await panel.getByRole("button", { name: "Find the original rebuild" }).click();
          await waitForState(() => lookups).toBe(2);
          expect(writes.length).toBe(2);
        }
      }
    }
    if (["stale-build", "session-unavailable", "session-unauthenticated"].includes(outcome)) {
      await waitForState(readAttempt).toBe(null);
      await expect(panel.getByRole("button", { name: "Rebuild testing", exact: true })).toBeEnabled();
      expect(writes.length).toBe(outcome === "stale-build" ? 1 : 0);
      expect(polled).toBe(false);
    } else if (outcome === "denied") {
      await waitForState(async () => (await readAttempt())?.denied).toBe(true);
      await page.reload();
      await expect(panel.getByRole("button", { name: "Recover this request" })).toHaveCount(0);
      expect(writes.length).toBe(1);
      expect(polled).toBe(false);
      await panel.screenshot({ path: testInfo.outputPath("testing-rebuild-denied.png") });
      await panel.getByRole("button", { name: "Settle this request" }).click();
      await expect(panel.getByRole("button", { name: "Rebuild testing", exact: true })).toBeEnabled();
      expect(writes.length).toBe(1);
    } else {
      await waitForState(async () => (await readAttempt())?.operationId).toBe("native-original");
      await page.clock.runFor(2500);
      if (heldOutcome) {
        await expect(panel.getByRole("button", { name: "Resume this rebuild", exact: true })).toBeEnabled();
        await page.reload();
        await expect(panel.getByRole("button", { name: "Resume this rebuild", exact: true })).toBeEnabled();
        expect(resumes.length).toBe(0);
        await panel.screenshot({ path: testInfo.outputPath("testing-rebuild-held.png") });
        await panel.getByRole("button", { name: "Resume this rebuild", exact: true }).click();
        if (outcome !== "held-resume") {
          await waitForState(async () => (await readAttempt())?.resumeNeedsRead).toBe(true);
          await expect(panel.getByRole("button", { name: "Resume this rebuild", exact: true })).toBeDisabled();
          expect(resumes.length).toBe(1);
          await panel.screenshot({ path: testInfo.outputPath("testing-rebuild-resume-needs-read.png") });
          await panel.getByRole("button", { name: "Resume watching this rebuild", exact: true }).click();
          if (outcome === "held-resume-denied") {
            await expect(panel.getByRole("button", { name: "Resume this rebuild", exact: true })).toBeEnabled();
            expect(resumes.length).toBe(1);
            await panel.getByRole("button", { name: "Resume this rebuild", exact: true }).click();
          }
        }
        expect(writes.length).toBe(1);
      }
      if (outcome === "watch-denied") {
        await expect(panel.getByRole("button", { name: "Resume watching this rebuild" })).toBeEnabled();
        expect(writes.length).toBe(1);
        await page.reload();
      }
      await waitForState(readAttempt).toBe(null);
      await expect(panel.getByRole("button", { name: "Rebuild testing", exact: true })).toBeEnabled();
      if (heldOutcome) expect(resumes.length).toBe(outcome === "held-resume-denied" ? 2 : 1);
      expect(polled).toBe(true);
      expect(writes[0].body.data_source_mode).toBe(selectedSource);
      expect(writes[0].body).not.toHaveProperty("instance");
    }
    await expect(panel).toContainText(environment.base_url);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await panel.screenshot({ path: testInfo.outputPath("testing-rebuild-result.png") });
  });
}
