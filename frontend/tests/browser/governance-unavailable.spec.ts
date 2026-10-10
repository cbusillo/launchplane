import { expect, test } from "@playwright/test";
import type { GovernanceProjectionResponse } from "../../src/generated/openapi.ts";

test("unavailable current evidence preserves stored history without granting authority", async ({ page }, testInfo) => {
  await page.goto("/ui/engineering/governance-projection?fixture=products&scenario=15");
  await expect(page.getByRole("heading", { name: "Governance evidence", exact: true })).toBeVisible();
  const fixtures = await page.evaluate(async () => {
    const path = "/ui/src/dev-fixtures.ts";
    const data = await import(path);
    return { identity: data.fixtureIdentity, response: data.governanceProjectionForFixture("products") as GovernanceProjectionResponse };
  });
  const projection = fixtures.response.projection;
  projection.requested_target = { repository: "example/tenant-site", pull_request_number: 308 };
  projection.target = null;
  projection.merge_readiness = {
    ...projection.merge_readiness, availability: "unavailable", reason_code: "current_evidence_unavailable", result: null,
    detail: "Current repository evidence is unavailable.",
  };
  projection.merge_admission.status = "admitted_unknown_target";
  projection.landing_outcome.target_status = "unknown";
  const mutations: string[] = [], errors: string[] = [];
  page.on("request", request => { if (!["GET", "HEAD"].includes(request.method())) mutations.push(request.url()); });
  page.on("pageerror", error => errors.push(error.message));
  await page.route("**/v1/auth/session", route => route.fulfill({ json: { status: "ok", identity: fixtures.identity, csrf_token: "fixture", trace_id: "history-auth" } }));
  await page.route("**/v1/governance/projection?**", route => route.fulfill({ json: fixtures.response }));
  await page.goto("/ui/engineering/governance-projection?repository=example%2Ftenant-site&pull_request_number=308&base_branch=main");
  const readiness = page.getByRole("region", { name: "Current merge readiness" });
  const admission = page.getByRole("region", { name: "Immutable merge admission" });
  const landing = page.getByRole("region", { name: "Separate landing outcome" });
  await expect(readiness).toContainText("Current repository evidence is unavailable");
  await expect(admission).toContainText("Recorded admission · target unknown");
  await expect(landing).toContainText("Merge commit");
  await page.screenshot({ path: testInfo.outputPath("unavailable-history.png"), fullPage: true });
  projection.merge_admission = { ...projection.merge_admission, status: "not_recorded", authorizes: [], record: null };
  projection.landing_outcome = { ...projection.landing_outcome, status: "not_observed", target_status: "none", landed: false, record: null };
  await page.getByRole("button", { name: "Refresh governance", exact: true }).click();
  await expect(admission).toContainText("No admission recorded");
  await expect(landing).toContainText(/not observed/i);
  await expect(readiness).toContainText("Current repository evidence is unavailable");
  expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
  expect(mutations).toEqual([]); expect(errors).toEqual([]);
});
