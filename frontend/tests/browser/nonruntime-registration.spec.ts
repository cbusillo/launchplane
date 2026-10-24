import { expect, test, type Page } from "./state";

for (const outcome of ["saved", "lost-reply", "persistent-refusal", "storage-failure", "readback-failure", "readback-absent", "definitive-conflict"]) {
  const lostReply = outcome === "lost-reply";
  const persistentRefusal = outcome === "persistent-refusal";
  test(`Director product registration ${outcome} @responsive`, async ({ page, context }, testInfo) => {
    await page.goto("/ui/products?fixture=products");
    const identity = await page.evaluate(async () => (await import("/ui/src/dev-fixtures.ts")).fixtureIdentity);
    let profile: Record<string, unknown> | null = null;
    let separateProfile: Record<string, unknown> | null = null;
    let readStatus = outcome.startsWith("readback-") ? 503 : 200;
    let reads = 0;
    let plans = 0;
    const applies: Array<{ key: string; body: unknown }> = [];
    const accessRefusal = "Caller cannot apply product onboarding.";
    const conflictRefusal = "Non-runtime onboarding requires a new product.";
    const sessionCookies: string[] = [];
    const sessionCookie = { name: "registration-session-fixture", value: "same-admin-session", url: new URL(page.url()).origin };
    if (persistentRefusal) await context.addCookies([sessionCookie]);
    await context.route("**/v1/auth/session", route => {
      sessionCookies.push(route.request().headers().cookie ?? "");
      return route.fulfill({ json: { status: "ok", trace_id: "session", csrf_token: "csrf", identity } });
    });
    await context.route("**/v1/products", route => route.fulfill({ json: { status: "ok", trace_id: "products", products: [] } }));
    await context.route("**/v1/product-profiles/desktop-tool*", route => {
      reads += 1;
      const saved = route.request().url().endsWith("/desktop-tool-separate") ? separateProfile : profile;
      return route.fulfill({ status: readStatus, json: readStatus === 200 ? { status: "ok", trace_id: "profile", profile: saved } : { error: { code: "read_failed", message: "Profile unavailable" } } });
    });
    await context.route("**/v1/product-onboarding/apply", async route => {
      const body = route.request().postDataJSON();
      const planned = { ...body.manifest, owner: { ...body.manifest.owner, github_login: "director" }, lifecycle_state: "active", lanes: [], updated_at: "2026-10-22T12:00:00Z", source: "fixture" };
      if (body.mode === "apply") {
        applies.push({ key: route.request().headers()["idempotency-key"], body });
        if (lostReply && applies.length === 2) {
          await route.fulfill({ status: 403, json: { error: { code: "authorization_denied", message: accessRefusal } } });
          return;
        }
        if (persistentRefusal && body.manifest.product === "desktop-tool") {
          if (applies.length === 1) {
            profile = { ...planned, display_name: "Independent registration" };
            await route.abort("failed"); return;
          }
          await route.fulfill({ status: 400, json: { error: { code: "invalid_product_onboarding_manifest", message: conflictRefusal } } });
          return;
        }
        if (body.manifest.product === "desktop-tool-separate") separateProfile = planned;
        else profile = planned;
        if (lostReply && applies.length === 1) { await route.abort("failed"); return; }
        if (outcome === "definitive-conflict" && applies.length === 1) {
          profile = { ...planned, display_name: "Independent registration" };
          await route.fulfill({ status: 400, json: { error: { code: "invalid_product_onboarding_manifest", message: conflictRefusal } } });
          return;
        }
      } else plans += 1;
      const replayed = lostReply && body.mode === "apply" && applies.length === 3;
      await route.fulfill({ status: 202, json: { status: "accepted", trace_id: replayed ? "registration-replay" : "registration", ...(replayed ? { replayed: true, original_trace_id: "registration" } : {}), records: {}, result: { mode: body.mode, plan_sha256: "a".repeat(64), product_profile: planned } } });
    });
    await page.goto("/ui/products");
    await page.getByText("Register a Director product", { exact: true }).click();
    const panel = page.getByRole("region", { name: "Director product registration" });
    await fillRegistration(page);
    await expect(panel.getByRole("button", { name: "Register product", exact: true })).toBeDisabled();
    await panel.getByRole("button", { name: "Review registration", exact: true }).click();
    await expect(panel.getByRole("button", { name: "Register product", exact: true })).toBeEnabled();
    await panel.getByLabel("Product name", { exact: true }).fill("Renamed desktop tool");
    await expect(panel.getByRole("button", { name: "Register product", exact: true })).toBeDisabled();
    await panel.getByRole("button", { name: "Review registration", exact: true }).click();
    await expect(panel.getByRole("button", { name: "Register product", exact: true })).toBeEnabled();
    if (outcome === "storage-failure") await page.evaluate(() => {
      Storage.prototype.setItem = () => { throw new DOMException("Storage unavailable", "QuotaExceededError"); };
    });
    await panel.getByRole("button", { name: "Register product", exact: true }).click();
    if (outcome === "storage-failure") {
      await expect(panel.getByRole("alert")).toBeVisible();
      expect(applies).toHaveLength(0);
      await page.screenshot({ path: testInfo.outputPath("registration-storage-failure.png"), fullPage: true });
      return;
    }
    if (lostReply) {
      await expect(panel.getByRole("button", { name: "Retry same registration" })).toBeEnabled();
      const originalLogin = await panel.getByLabel("Client GitHub login", { exact: true }).inputValue();
      await page.reload();
      await page.getByText("Register a Director product", { exact: true }).click();
      await page.getByRole("button", { name: "Retry same registration" }).click();
      await expect(panel.getByRole("status")).toContainText(accessRefusal);
      await expect(panel.getByRole("button", { name: "Retry same registration" })).toBeEnabled();
      await expect(panel.getByLabel("Product key", { exact: true })).toBeDisabled();
      await expect(panel.getByRole("button", { name: "Review registration", exact: true })).toBeDisabled();
      await expect(panel.getByRole("button", { name: "Review registration again", exact: true })).toHaveCount(0);
      expect(applies[1]).toEqual(applies[0]);
      await page.reload();
      await page.getByText("Register a Director product", { exact: true }).click();
      await expect(panel.getByLabel("Client GitHub login", { exact: true })).toHaveValue(originalLogin);
      await expect(panel.getByLabel("Product key", { exact: true })).toBeDisabled();
      await expect(panel.getByRole("button", { name: "Review registration again", exact: true })).toHaveCount(0);
      await page.getByRole("button", { name: "Retry same registration" }).click();
    }
    if (persistentRefusal) {
      const retry = panel.getByRole("button", { name: "Retry same registration", exact: true });
      await expect(retry).toBeEnabled();
      await retry.click();
      await expect(panel.getByRole("status")).toContainText(conflictRefusal);
      expect(applies[1]).toEqual(applies[0]);
      const originalLogin = await panel.getByLabel("Client GitHub login", { exact: true }).inputValue();
      await expect(panel.getByRole("link", { name: "Open separate registration", exact: true })).toBeVisible();
      const separatePage = context.waitForEvent("page");
      await panel.getByRole("link", { name: "Open separate registration", exact: true }).click();
      const fresh = await separatePage;
      await fresh.waitForURL("**/ui/products");
      expect(await fresh.evaluate(() => window.opener === null)).toBe(true);
      expect(applies).toHaveLength(2);
      await fresh.getByText("Register a Director product", { exact: true }).click();
      await expect(fresh.getByLabel("Product key", { exact: true })).toBeEnabled();
      await expect(fresh.getByLabel("Product key", { exact: true })).toHaveValue("");
      await fillRegistration(fresh);
      await fresh.getByLabel("Product key", { exact: true }).fill("desktop-tool-separate");
      await fresh.getByLabel("Repository", { exact: true }).fill("example/desktop-tool-separate");
      await expect(fresh.getByRole("button", { name: "Register product", exact: true })).toBeDisabled();
      const priorPlans = plans;
      await fresh.getByRole("button", { name: "Review registration", exact: true }).click();
      await expect(fresh.getByRole("button", { name: "Register product", exact: true })).toBeEnabled();
      expect(plans).toBe(priorPlans + 1);
      expect(applies).toHaveLength(2);
      await fresh.getByRole("button", { name: "Register product", exact: true }).click();
      await expect(fresh.getByRole("button", { name: "Register another product", exact: true })).toBeVisible();
      expect(applies[2].key).not.toBe(applies[0].key);
      expect(applies[2].body).toMatchObject({ manifest: { product: "desktop-tool-separate" } });
      expect(sessionCookies.length).toBeGreaterThanOrEqual(2);
      expect(sessionCookies.every(cookie => cookie.includes(`${sessionCookie.name}=${sessionCookie.value}`))).toBe(true);
      await expect(panel.getByLabel("Product key", { exact: true })).toBeDisabled();
      await expect(panel.getByLabel("Client GitHub login", { exact: true })).toHaveValue(originalLogin);
      await fresh.close();
      await page.reload();
      await page.getByText("Register a Director product", { exact: true }).click();
      await expect(panel.getByLabel("Product key", { exact: true })).toBeDisabled();
      await expect(panel.getByLabel("Client GitHub login", { exact: true })).toHaveValue(originalLogin);
      await expect(panel.getByRole("button", { name: "Review registration again", exact: true })).toHaveCount(0);
      await retry.click();
      await expect(panel.getByRole("status")).toContainText(conflictRefusal);
      expect(applies).toHaveLength(4);
      expect(applies[3]).toEqual(applies[0]);
      await expect(retry).toBeEnabled();
      await expect(panel.getByLabel("Product key", { exact: true })).toBeDisabled();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await page.screenshot({ path: testInfo.outputPath("registration-original-still-unknown.png"), fullPage: true });
      return;
    }
    if (outcome === "definitive-conflict") {
      await expect(panel.getByRole("status")).toContainText(conflictRefusal);
      await page.reload();
      await page.getByText("Register a Director product", { exact: true }).click();
      await panel.getByRole("button", { name: "Read registration", exact: true }).click();
      await expect(panel.getByRole("alert")).toBeVisible();
      await expect(panel.getByLabel("Product key", { exact: true })).toBeDisabled();
      await expect(panel.getByRole("button", { name: "Review registration again", exact: true })).toBeEnabled();
      await panel.getByRole("button", { name: "Review registration again", exact: true }).click();
      await expect(panel.getByLabel("Product key", { exact: true })).toHaveValue("desktop-tool");
      await expect(panel.getByLabel("Product name", { exact: true })).toHaveValue("Renamed desktop tool");
      await expect(panel.getByRole("button", { name: "Register product", exact: true })).toBeDisabled();
      expect(applies).toHaveLength(1);
      await panel.getByLabel("Product key", { exact: true }).fill("desktop-tool-new");
      await panel.getByLabel("Repository", { exact: true }).fill("example/desktop-tool-new");
      const priorPlans = plans;
      await panel.getByRole("button", { name: "Review registration", exact: true }).click();
      await expect(panel.getByRole("button", { name: "Register product", exact: true })).toBeEnabled();
      expect(plans).toBe(priorPlans + 1);
      expect(applies).toHaveLength(1);
      await panel.getByRole("button", { name: "Register product", exact: true }).click();
    }
    if (outcome.startsWith("readback-")) {
      await expect(panel.getByRole("alert")).toBeVisible();
      await page.reload();
      await page.getByText("Register a Director product", { exact: true }).click();
      await expect(panel.getByLabel("Product key", { exact: true })).toBeDisabled();
      const read = panel.getByRole("button", { name: "Read registration", exact: true });
      if (outcome === "readback-absent") {
        readStatus = 404;
        await read.click();
        await panel.getByRole("button", { name: "Review registration again", exact: true }).click();
        await expect(panel.getByRole("button", { name: "Register product", exact: true })).toBeDisabled();
        const priorPlans = plans;
        await panel.getByRole("button", { name: "Review registration", exact: true }).click();
        await expect(panel.getByRole("button", { name: "Register product", exact: true })).toBeEnabled();
        expect(plans).toBe(priorPlans + 1);
      } else {
        for (const status of [403, 503, 200]) {
          readStatus = status;
          const saved = profile;
          if (status === 200) profile = { ...profile, repository: "example/different" };
          await read.click();
          await expect(panel.getByRole("alert")).toBeVisible();
          await expect(read).toBeEnabled();
          expect(applies).toHaveLength(1);
          profile = saved;
        }
      }
      readStatus = 200;
      if (outcome === "readback-absent") await panel.getByRole("button", { name: "Register product", exact: true }).click();
      else await read.click();
    }
    await expect(panel.getByRole("button", { name: "Register another product", exact: true })).toBeVisible();
    expect(reads).toBeGreaterThan(0);
    expect(applies).toHaveLength(lostReply ? 3 : outcome === "readback-absent" || outcome === "definitive-conflict" ? 2 : 1);
    if (lostReply) for (const attempt of applies.slice(1)) expect(attempt).toEqual(applies[0]);
    if (outcome === "definitive-conflict") {
      expect(applies[1].key).not.toBe(applies[0].key);
      expect(applies[1].body).toMatchObject({ manifest: { product: "desktop-tool-new" } });
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({ path: testInfo.outputPath(`registration-${lostReply ? "recovered" : "saved"}.png`), fullPage: true });
  });
}

async function fillRegistration(page: Page): Promise<void> {
  await page.getByLabel("Product key", { exact: true }).fill("desktop-tool");
  await page.getByLabel("Product name", { exact: true }).fill("Desktop tool");
  await page.getByLabel("Repository", { exact: true }).fill("example/desktop-tool");
  await page.getByLabel("Client GitHub login", { exact: true }).fill(" @DiReCtOr ");
  await page.getByLabel("Client GitHub ID", { exact: true }).fill("1234");
  await page.getByRole("region", { name: "Director product registration" }).getByRole("checkbox").check();
}
