import { expect, test, type Page } from "./state";
import type { WaitingRequest } from "../../src/generated/openapi.ts";

const sources = [
  { repository: "team/site", products: ["Example site"], available: true },
  { repository: "team/app", products: ["Example app"], available: true },
];
const request: WaitingRequest = {
  source_id: "original-request", source_url: "https://git.example.test/team/site/issues/7",
  source_kind: "request", repository: sources[0].repository, title: "Inventory menu is missing",
  products: sources[0].products, requester: "client-agent", owning_agent: null,
  next_actor: "agent:Supervisor", next_action: "Investigate the missing menu.",
  created_at: "2026-10-30T18:00:00Z", age_seconds: 7200,
};

async function setup(page: Page) {
  const state = { mode: "open", sourceReads: 0 };
  const mutations: string[] = [];
  page.on("request", call => { if (call.method() !== "GET") mutations.push(call.url()); });
  await page.route("**/v1/auth/session", route => route.fulfill({ json: {
    status: "ok", trace_id: "session", csrf_token: "fixture",
    identity: { provider: "github", login: "admin-fixture", github_id: 42, name: "Admin",
      email: "fixture@example.invalid", organizations: [], teams: [], role: "admin" },
  } }));
  await page.route("**/v1/attention/requests?*", route => {
    const url = new URL(route.request().url());
    const repository = url.searchParams.get("repository");
    if (state.mode === "denied") return route.fulfill({ status: 403, json: {
      status: "rejected", trace_id: "denied", error: { code: "authorization_denied", message: "Administrator access is required." },
    } });
    if (repository) state.sourceReads++;
    if (state.mode === "partial" && repository === sources[1].repository) return route.fulfill({ status: 503, json: {
      status: "rejected", trace_id: "unread", error: { code: "unavailable", message: "Source is unavailable." },
    } });
    const requests = !repository || state.mode === "closed" ? [] : repository === sources[0].repository
      ? [request]
      : [{ ...request, source_id: "owned-change", source_kind: "change", title: "Fix import mappings",
        repository: sources[1].repository, products: sources[1].products, owning_agent: "worker-two", next_actor: "agent:worker-two" }];
    if (state.mode !== "closed" && repository === sources[1].repository) requests.push({ ...request, source_id: "person-wait", title: "Check menu with client", next_actor: "person:client", repository: sources[1].repository, products: sources[1].products });
    return route.fulfill({ json: { status: "ok", trace_id: "attention",
      observed_at: "2026-10-30T20:00:00Z", sources, requests,
      truncated: false,
    } });
  });
  return { state, mutations };
}

test("waiting requests route unowned work and keep replies visible", { tag: "@responsive" }, async ({ page }, testInfo) => {
  const { state, mutations } = await setup(page);
  await page.goto("/ui/engineering/request-attention?fixture=products");
  await expect(page.getByRole("heading", { name: "Waiting requests", level: 1 })).toBeFocused();
  const list = page.getByRole("list", { name: "Waiting requests" });
  await expect(list.getByRole("listitem")).toHaveCount(3);
  expect(state.sourceReads).toBeGreaterThanOrEqual(sources.length);
  await expect(list).toContainText(request.requester);
  await page.getByLabel(/Supervisor \(/).check();
  await expect(list.getByRole("listitem")).toHaveCount(1);
  await expect(list.getByRole("link", { name: request.title })).toHaveAttribute("href", request.source_url);
  state.mode = "replied";
  await page.getByRole("button", { name: "Refresh requests" }).click();
  await expect(list).toContainText(request.title);
  await page.getByLabel(/Supervisor \(/).uncheck();
  await page.getByLabel("Filter requests by product").selectOption(sources[1].products[0]);
  await expect(list).toContainText("worker-two");
  await page.getByLabel("Filter requests by product").selectOption("");
  await expect(list.getByRole("listitem")).toHaveCount(3);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("waiting-requests.png"), fullPage: true });
  state.mode = "closed";
  await page.getByRole("button", { name: "Refresh requests" }).click();
  await expect(list).toHaveCount(0);
  expect(mutations).toEqual([]);
});

test("partial collection and revoked access stay explicit", async ({ page }) => {
  const { state, mutations } = await setup(page);
  state.mode = "partial";
  await page.goto("/ui/engineering/request-attention?fixture=products");
  await expect(page.getByRole("alert")).toBeVisible();
  await expect(page.getByRole("list", { name: "Waiting requests" })).toContainText(request.title);
  state.mode = "denied";
  await page.getByRole("button", { name: "Refresh requests" }).click();
  await expect(page.getByRole("list", { name: "Waiting requests" })).toHaveCount(0);
  expect(mutations).toEqual([]);
});
