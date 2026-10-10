import { expect, test } from "@playwright/test";
import type { GovernanceProjectionResponse } from "../../src/generated/openapi.ts";

test("legacy observation chronology stays visible and unqualified", async ({ page }, testInfo) => {
  await page.goto("/ui/engineering/governance-projection?fixture=products&scenario=15");
  await expect(page.getByRole("heading", { name: "Governance evidence", exact: true })).toBeVisible();
  const fixtures = await page.evaluate(async () => {
    const path = "/ui/src/dev-fixtures.ts";
    const data = await import(path);
    return { identity: data.fixtureIdentity, response: data.governanceProjectionForFixture("products") as GovernanceProjectionResponse };
  });
  const projection = fixtures.response.projection;
  projection.merge_admission.record!.created_at = "2026-07-14T14:32:00.000000Z";
  const outcome = projection.landing_outcome.record!;
  outcome.observed_at = "2026-07-14T14:31:00.000000Z";
  const mutations: string[] = [];
  const errors: string[] = [];
  page.on("request", request => { if (!["GET", "HEAD"].includes(request.method())) mutations.push(request.url()); });
  page.on("pageerror", error => errors.push(error.message));
  await page.route("**/v1/auth/session", route => route.fulfill({ json: { status: "ok", identity: fixtures.identity, csrf_token: "fixture", trace_id: "time-auth" } }));
  await page.route("**/v1/governance/projection?**", route => route.fulfill({ json: fixtures.response }));
  await page.goto("/ui/engineering/governance-projection?repository=example%2Ftenant-site&pull_request_number=308&base_branch=main");
  const landing = page.getByRole("region", { name: "Separate landing outcome" });
  await expect(landing.getByText("Recorded time (unqualified)", { exact: true })).toBeVisible();
  await expect(landing).toContainText("actual observation time is unqualified");
  await expect(landing).toContainText("Merge commit");
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: testInfo.outputPath("legacy-observation.png"), fullPage: true });
  outcome.observed_at = "2026-07-14T14:33:00.000000Z";
  await page.getByRole("button", { name: "Refresh governance", exact: true }).click();
  await expect(landing.getByText("Observed", { exact: true })).toBeVisible();
  await expect(landing).not.toContainText("actual observation time is unqualified");
  expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
  expect(mutations).toEqual([]);
  expect(errors).toEqual([]);
});
