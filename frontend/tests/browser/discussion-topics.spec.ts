import { expect, test, type Page } from "./state";

const topic = {
  product: "site-a", topic_id: "contact", title: "Should we keep the contact links?",
  issue_url: "https://git.example.test/team/site/issues/7", added_at: "2026-10-20T16:00:00Z", closed_at: null,
};

async function session(page: Page, role = "owner") {
  await page.route("**/v1/auth/session", route => route.fulfill({ json: {
    status: "ok", csrf_token: "", identity: { login: "client", name: "Client", github_id: 9001, role, organizations: [], teams: [] },
  } }));
}

test("Client topics show questions, context and owning issues; refresh removes closed topics", { tag: "@responsive" }, async ({ page }, testInfo) => {
  await session(page);
  let topics = [topic];
  const unexpected: string[] = [];
  await page.route("**/v1/**", route => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/v1/products/site-a/discussion-topics") return route.fulfill({ json: { status: "ok", trace_id: "topics", product: "site-a", display_name: "Example Shop", topics } });
    if (path === "/v1/auth/session") return route.fallback();
    unexpected.push(path);
    return route.fulfill({ status: 403, json: {} });
  });
  await page.goto("/ui/products/site-a/topics");
  await expect(page.getByRole("heading", { name: "Example Shop" })).toBeVisible();
  await expect(page.getByText(topic.title, { exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: /^View discussion/ })).toHaveAttribute("href", topic.issue_url);
  await expect(page.locator("time")).toHaveAttribute("datetime", topic.added_at);
  await expect(page.getByText("1 open topic", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: /accept|deploy|merge|close topic/i })).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("topics-populated.png"), fullPage: true });
  topics = [];
  await page.getByRole("button", { name: "Refresh topics" }).click();
  await expect(page.getByText("No open topics to discuss.")).toBeVisible();
  await expect(page.getByText(topic.title, { exact: true })).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath("topics-empty.png"), fullPage: true });
  expect(unexpected).toEqual([]);
});

test("failed refresh hides old topics and retry recovers; another product is denied", async ({ page }) => {
  await session(page);
  let fails = false;
  await page.route("**/v1/products/*/discussion-topics", route => {
    if (route.request().url().includes("site-b")) return route.fulfill({ status: 403, json: { error: { message: "denied" } } });
    return route.fulfill(fails ? { status: 503, json: { error: { message: "unavailable" } } } : { json: { status: "ok", trace_id: "topics", product: "site-a", display_name: "Example Shop", topics: [topic] } });
  });
  await page.goto("/ui/products/site-a/topics");
  await expect(page.getByText(topic.title, { exact: true })).toBeVisible();
  fails = true;
  await page.getByRole("button", { name: "Refresh topics" }).click();
  await expect(page.getByRole("alert")).toContainText("could not be loaded");
  await expect(page.getByText(topic.title, { exact: true })).toHaveCount(0);
  fails = false;
  await page.getByRole("button", { name: "Refresh topics" }).click();
  await expect(page.getByText(topic.title, { exact: true })).toBeVisible();
  await page.goto("/ui/products/site-b/topics");
  await expect(page.getByRole("alert")).toContainText("not available to you");
  await expect(page.getByText(topic.title, { exact: true })).toHaveCount(0);
  await expect(page.getByText("Example Shop", { exact: true })).toHaveCount(0);
});

test("signed-out topics return to the same product after sign-in", async ({ page }) => {
  await page.route("**/v1/auth/session", route => route.fulfill({ status: 401, json: { error: { message: "Sign in" } } }));
  await page.goto("/ui/products/site-a/topics");
  await expect(page.getByRole("link", { name: "Sign in with GitHub" })).toHaveAttribute("href", "/auth/github/login?return_to=%2Fui%2Fproducts%2Fsite-a%2Ftopics");
  await expect(page.getByRole("heading", { name: "Sign in to view topics" })).toBeVisible();
});

test("Director opens topics from a product workspace and returns to overview", async ({ page }) => {
  await page.route("**/v1/products/atlas-commerce/discussion-topics", route => route.fulfill({ json: {
    status: "ok", trace_id: "topics", product: "atlas-commerce", display_name: "Atlas Commerce", topics: [],
  } }));
  await page.goto("/ui/products/atlas-commerce?fixture=products");
  await page.getByRole("link", { name: "Topics to discuss", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Atlas Commerce", exact: true })).toBeVisible();
  await expect(page.getByText("No open topics to discuss.")).toBeVisible();
  await page.getByRole("link", { name: "Product overview", exact: true }).click();
  await expect(page.getByRole("navigation", { name: "Atlas Commerce workspace" })).toBeVisible();
});

test("Client review links to that product's topics", async ({ page }) => {
  await page.route("**/v1/products/site-a/discussion-topics", route => route.fulfill({ json: {
    status: "ok", trace_id: "topics", product: "site-a", display_name: "Example Shop", topics: [topic],
  } }));
  await page.goto("/ui/owner-review?fixture=products&product=site-a");
  await page.getByRole("link", { name: "Topics to discuss", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Example Shop" })).toBeVisible();
  await expect(page.getByText(topic.title, { exact: true })).toBeVisible();
});
