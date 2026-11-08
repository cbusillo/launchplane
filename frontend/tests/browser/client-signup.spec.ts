import { expect, test } from "./state";

test("only the Client sees eligible signup buttons and minting happens only on click", async ({ page }) => {
  await page.goto("/ui/owner-review?fixture=products&repository=example%2Fcontrol-plane&pull_request=308");
  const review = await page.evaluate(async () => {
    const path = "/ui/src/dev-fixtures.ts";
    const fixtures = await import(path);
    return fixtures.productReviewForFixture("products");
  });
  let client = true;
  let eligible = true;
  let privateReads = 0;
  let mints = 0;
  const binding = "a".repeat(64);
  const user = { xmlid: "fixture.user_client", name: "Example Client", login: "client@example.invalid" };
  const csrf = "fixture";
  await page.route("**/v1/auth/session", route => route.fulfill({ json: { status: "ok", csrf_token: csrf, identity: { login: "example-owner", github_id: 9001, role: "read_only", organizations: [], teams: [] } } }));
  await page.route("**/v1/product-review?*", route => route.fulfill({ json: { ...review, viewer_is_owner: client, can_decide: client } }));
  await page.route("**/v1/product-review/sign-in?*", route => {
    privateReads += 1;
    expect(new URL(route.request().url()).searchParams.get("generation_id")).toBe(review.preview_generation_id);
    return route.fulfill({ json: { users: eligible ? [user] : [], runtime_binding: binding } });
  });
  await page.route("**/v1/product-review/sign-in", route => {
    mints += 1;
    expect(route.request().postDataJSON().xmlid).toBe(user.xmlid);
    expect(route.request().postDataJSON().reviewed_runtime_binding).toBe(binding);
    expect(route.request().headers()["x-csrf-token"]).toBe(csrf);
    return route.fulfill({ json: { name: user.name, sign_in_url: new URL("/web/signup?token=fictional", review.preview_url).toString() } });
  });
  await page.context().route("https://site.preview.example.invalid/web/signup?*", route => route.fulfill({ contentType: "text/html", body: "<h1>Native signup</h1>" }));
  const path = "/ui/owner-review?repository=example%2Fcontrol-plane&pull_request=308";
  await page.goto(path);
  const button = page.getByRole("button", { name: "Sign in as Example Client" });
  await expect(button).toBeVisible();
  expect(mints).toBe(0);
  await expect(page.locator('a[href*="token="]')).toHaveCount(0);
  eligible = false;
  await page.reload();
  await expect(page.getByRole("link", { name: "Open the preview" })).toBeVisible();
  await expect(button).toHaveCount(0);
  const readsBeforeNonClient = privateReads;
  client = false;
  await page.reload();
  await expect(page.getByRole("link", { name: "Open the preview" })).toBeVisible();
  await expect(button).toHaveCount(0);
  expect(privateReads).toBe(readsBeforeNonClient);
  client = true;
  eligible = true;
  await page.reload();
  await expect(button).toBeVisible();
  const popup = page.waitForEvent("popup");
  await button.click();
  const nativePage = await popup;
  await expect(nativePage.getByRole("heading", { name: "Native signup" })).toBeVisible();
  await expect(page.getByRole("checkbox", { name: "I looked at this preview and it is right." })).toBeEnabled();
  await nativePage.close();
  expect(mints).toBe(1);
  const readsBeforeHistory = privateReads;
  await page.goto(path + "&decision_id=historical-decision");
  await expect(page.getByRole("link", { name: "View latest review" })).toBeVisible();
  await expect(button).toHaveCount(0);
  expect(privateReads).toBe(readsBeforeHistory);
});

test("a live hotfix review never offers testing-lane signup", async ({ page }) => {
  await page.goto("/ui/owner-review?product=example-site&fixture=operator");
  const response = await page.evaluate(async () => {
    const fixtures = await import("/ui/src/dev-fixtures.ts");
    return fixtures.releaseReviewForFixture("operator");
  });
  response.viewer_is_owner = true;
  response.can_start_temporary_hotfix = false;
  response.reviewing_temporary_hotfix = true;
  response.live_site_url = "https://live.example.invalid/";
  let reads = 0;
  await page.route("**/v1/auth/session", route => route.fulfill({ json: { status: "ok", csrf_token: "fixture", identity: { login: "example-owner", github_id: 9001, role: "read_only", organizations: [], teams: [] } } }));
  await page.route("**/v1/release-review?*", route => route.fulfill({ json: response }));
  await page.route("**/v1/product-review/sign-in?*", route => {
    reads += 1;
    return route.fulfill({ json: { users: [{ xmlid: "fixture.user_client", name: "Example Client", login: "client@example.invalid" }], runtime_binding: "a".repeat(64) } });
  });
  await page.goto("/ui/owner-review?product=example-site");
  await expect(page.getByRole("link", { name: "Open the live site", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Sign in as Example Client" })).toHaveCount(0);
  expect(reads).toBe(0);
  response.reviewing_temporary_hotfix = false;
  await page.reload();
  await expect(page.getByRole("button", { name: "Sign in as Example Client" })).toBeVisible();
});
