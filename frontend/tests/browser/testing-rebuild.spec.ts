import { expect, test, waitForState } from "./state";

for (const outcome of ["success", "lost-reply", "recovery-denied", "lookup-not-observed", "session-unavailable", "watch-denied", "denied", "stale-build", "preflight-unavailable", "held-resume", "held-resume-denied", "held-resume-lost-reply"] as const) {
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
    const selectedCommit = "c".repeat(40);
    const csrfToken = "fixture-csrf";
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
      ? route.fulfill({ status: 503, json: { trace_id: "session-unavailable", error: { code: "session_unavailable", message: "Session lookup unavailable." } } })
      : route.fulfill({ json: { status: "ok", trace_id: "session", csrf_token: csrfToken, identity: { ...fixtures.identity, role: "admin" } } }));
    await page.route("**/v1/products", route => route.fulfill({ json: { status: "ok", trace_id: "products", products: fixtures.products } }));
    await page.route("**/v1/products/atlas-commerce/environments/testing", route => route.fulfill({ json: { status: "ok", trace_id: "lane", environment } }));
    await page.route("**/v1/products/atlas-commerce/testing-rebuild/inputs?*", route => outcome === "preflight-unavailable"
      ? route.fulfill({ status: 503, json: { trace_id: "preflight-unavailable", error: { code: "testing_preflight_unavailable", message: "Testing inspection is unavailable." } } })
      : route.fulfill({ json: {
      status: "pass", product: environment.product, context: environment.context, instance: "testing", base_url: environment.base_url,
      artifact_id: "current-verified-build", source_git_ref: selectedCommit, data_source_mode: new URL(route.request().url()).searchParams.get("data_source_mode"),
      observed_at: new Date().toISOString(), candidate_verification: "pending",
    } }));
    const receipt = (status: string) => ({ trace_id: "rebuild-trace", operation: { product: environment.product, instance: "testing", operation_id: "native-original", status, phase: status === "pass" ? "completed" : "running", updated_at: status === "reconciliation_required" ? heldUpdatedAt : new Date().toISOString(), attempt: heldAttempt } });
    await page.route("**/v1/products/atlas-commerce/testing-rebuild", async route => {
      expect(route.request().headers()["x-csrf-token"]).toBe(csrfToken);
      writes.push({ key: route.request().headers()["idempotency-key"], body: route.request().postDataJSON() });
      if (["lost-reply", "recovery-denied", "lookup-not-observed"].includes(outcome) && writes.length === 1) return route.abort();
      if (["denied", "recovery-denied", "lookup-not-observed"].includes(outcome)) return route.fulfill({ status: 403, json: { trace_id: "denied-trace", error: { code: "authorization_denied", message: "This request is denied." } } });
      if (outcome === "stale-build") return route.fulfill({ status: 409, json: { trace_id: "stale-trace", error: { code: "testing_rebuild_not_ready", message: "The inspected build changed. Inspect again." } } });
      return route.fulfill({ json: receipt("pending") });
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
      await expect(panel.getByText("Testing inspection is unavailable.")).toBeVisible();
      await expect(panel.getByRole("button", { name: "Rebuild testing", exact: true })).toBeDisabled();
      expect(writes.length).toBe(0);
      await panel.screenshot({ path: testInfo.outputPath("testing-rebuild-unavailable.png") });
      return;
    }
    const selectedSource = outcome === "success" ? "empty" : outcome === "stale-build" ? "production_restore" : "upstream_restore";
    if (selectedSource !== "empty") await panel.getByLabel("Start with").selectOption(selectedSource);
    await expect(panel).toContainText(selectedCommit.slice(0, 12));
    if (["lost-reply", "recovery-denied", "lookup-not-observed"].includes(outcome)) {
      await panel.getByText("Reason (optional edit)", { exact: true }).click();
      await panel.getByLabel("Reason", { exact: true }).fill("Prepare a fresh testing copy.");
    }
    await expect(panel.getByRole("button", { name: "Rebuild testing", exact: true })).toBeEnabled();
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({ path: testInfo.outputPath("testing-rebuild-viewport.png") });
    await panel.screenshot({ path: testInfo.outputPath("testing-rebuild-ready.png") });
    sessionUnavailable = outcome === "session-unavailable";
    await panel.getByRole("button", { name: "Rebuild testing", exact: true }).click();
    if (["lost-reply", "recovery-denied", "lookup-not-observed"].includes(outcome)) {
      await expect(panel.getByRole("button", { name: "Recover this request" })).toBeEnabled();
      await page.reload();
      await expect(panel.getByRole("button", { name: "Recover this request" })).toBeEnabled();
      expect(writes.length).toBe(1);
      await panel.getByRole("button", { name: "Recover this request" }).click();
      await waitForState(() => writes.length).toBe(2);
      expect(writes[1]).toEqual(writes[0]);
      if (["recovery-denied", "lookup-not-observed"].includes(outcome)) {
        await expect(panel.getByRole("button", { name: "Find the original rebuild" })).toBeEnabled();
        await panel.screenshot({ path: testInfo.outputPath("testing-rebuild-read-only-recovery.png") });
        await page.reload();
        await panel.getByRole("button", { name: "Find the original rebuild" }).click();
        await waitForState(() => lookups).toBe(1);
        expect(writes.length).toBe(2);
        if (outcome === "lookup-not-observed") {
          await expect(panel.getByRole("status")).toContainText("Keep this request");
          await expect(panel.getByRole("button", { name: "Rebuild testing", exact: true })).toHaveCount(0);
          await panel.getByRole("button", { name: "Find the original rebuild" }).click();
          await waitForState(() => lookups).toBe(2);
          expect(writes.length).toBe(2);
        }
      }
    }
    if (outcome === "stale-build" || outcome === "session-unavailable") {
      await expect(panel.getByRole("status")).toContainText("Review the fresh inspection");
      await expect(panel.getByRole("button", { name: "Rebuild testing", exact: true })).toBeEnabled();
      expect(writes.length).toBe(outcome === "session-unavailable" ? 0 : 1);
      expect(polled).toBe(false);
    } else if (outcome === "denied") {
      await expect(panel.getByText("This request was denied and will not be submitted again.", { exact: false })).toBeVisible();
      await page.reload();
      await expect(panel.getByRole("button", { name: "Recover this request" })).toHaveCount(0);
      expect(writes.length).toBe(1);
      expect(polled).toBe(false);
      await panel.screenshot({ path: testInfo.outputPath("testing-rebuild-denied.png") });
      await panel.getByRole("button", { name: "Discard this denied request" }).click();
      await expect(panel.getByRole("button", { name: "Rebuild testing", exact: true })).toBeEnabled();
      expect(writes.length).toBe(1);
    } else {
      await waitForState(() => page.evaluate(() => JSON.parse(sessionStorage.getItem("launchplane:testing-rebuild:atlas-commerce") ?? "null")?.operationId)).toBe("native-original");
      await page.clock.runFor(2500);
      if (heldOutcome) {
        await expect(panel.getByRole("button", { name: "Resume this rebuild", exact: true })).toBeEnabled();
        await page.reload();
        await expect(panel.getByRole("button", { name: "Resume this rebuild", exact: true })).toBeEnabled();
        expect(resumes.length).toBe(0);
        await panel.screenshot({ path: testInfo.outputPath("testing-rebuild-held.png") });
        await panel.getByRole("button", { name: "Resume this rebuild", exact: true }).click();
        if (outcome !== "held-resume") {
          await expect(panel.getByRole("status")).toContainText("Read the original operation");
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
      await expect(panel.getByRole("status")).toContainText("Testing rebuilt and verified healthy at the same address.");
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
