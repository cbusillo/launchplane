import assert from "node:assert/strict";
import { test } from "node:test";
import { environmentOperationalTone, expireProductEvidence, expireEnvironmentEvidence } from "../src/product-environment-signal.ts";

const originalWindow = Object.getOwnPropertyDescriptor(globalThis, "window");
Object.defineProperty(globalThis, "window", { configurable: true, value: { location: { search: "" } } });
const { productsForFixture } = await import("../src/dev-fixtures.ts");
if (originalWindow) Object.defineProperty(globalThis, "window", originalWindow);
else Reflect.deleteProperty(globalThis, "window");

function environment() {
  const lane = structuredClone(productsForFixture("products")[0].environments.find(lane => lane.environment === "testing"));
  lane.trust_state = "recorded";
  lane.provenance.freshness_status = "verified";
  lane.provenance.stale_after = new Date(Date.now() + 60_000).toISOString();
  const freshen = value => {
    if (!value || typeof value !== "object") return;
    if ("stale_after" in value) value.stale_after = lane.provenance.stale_after;
    Object.values(value).forEach(freshen);
  };
  freshen(lane.topology);
  lane.warnings = [];
  lane.topology.warnings = [];
  lane.topology.observed.placement.runtime_identity_status = "match";
  lane.health_monitoring.checks = [structuredClone(lane.health_monitoring.checks[0])];
  const check = lane.health_monitoring.checks[0];
  check.probe_effective = true;
  check.status = "pass";
  check.trust_state = "verified";
  check.incident_status = "";
  check.provenance.stale_after = lane.provenance.stale_after;
  return lane;
}

test("a fresh identity and health verification makes a monitored lane green", () =>
  assert.equal(environmentOperationalTone(environment()), "verified"));

test("a canonical generated occurrence blocks green independently of declared checks", () => {
  const lane = environment();
  const incident = structuredClone(productsForFixture("products")[0].environments.find(lane => lane.environment === "prod").health_monitoring.open_incidents[0]);
  incident.check_name = "launchplane-deploy-fence";
  incident.check_kind = "provider";
  lane.health_monitoring.open_incidents = [incident];
  assert.equal(environmentOperationalTone(lane), "danger");
  lane.health_monitoring.open_incidents = [];
  assert.equal(environmentOperationalTone(lane), "verified");
});

test("informational topology stays visible without blocking current verification", () => {
  const lane = environment();
  lane.topology.warnings = [{ code: "external_ingress_internals_unsupported", severity: "info", scope: "ingress", detail: "External proxy internals are unavailable.", domain_name: "" }];
  assert.equal(environmentOperationalTone(lane), "verified");
  lane.health_monitoring.checks[0].status = "fail";
  assert.equal(environmentOperationalTone(lane), "danger");
  lane.health_monitoring.checks[0].status = "pass";
  lane.health_monitoring.checks[0].incident_status = "open";
  assert.equal(environmentOperationalTone(lane), "danger");
  lane.health_monitoring.checks[0].incident_status = "";
  lane.provenance.freshness_status = "stale";
  assert.equal(environmentOperationalTone(lane), "warning");
});

test("no website never bypasses health or runtime identity", () => {
  const lane = environment();
  lane.topology.desired.public_website = "none";
  lane.health_monitoring.checks[0].kind = "private_http";
  lane.topology.warnings = [{ code: "public_website_not_applicable", severity: "info", scope: "authority", detail: "No public website declared.", domain_name: "" }];
  assert.equal(environmentOperationalTone(lane), "verified");
  for (const state of ["missing", "unsupported"]) {
    lane.trust_state = state;
    assert.equal(environmentOperationalTone(lane), "verified");
  }
  lane.trust_state = "recorded";
  lane.health_monitoring.checks[0].probe_effective = false;
  assert.notEqual(environmentOperationalTone(lane), "verified");
  lane.health_monitoring.checks[0].probe_effective = true;
  lane.topology.observed.placement.runtime_identity_status = "mismatch";
  assert.equal(environmentOperationalTone(lane), "danger");
  lane.topology.observed.placement.runtime_identity_status = "match";
  lane.topology.warnings.push({ code: "stale_route_authority", severity: "error", scope: "authority", detail: "Stale authority", domain_name: "" });
  assert.equal(environmentOperationalTone(lane), "verified");
});

test("negative TLS remains blocking beside informational evidence", () => {
  const lane = environment();
  lane.topology.warnings = [{ code: "external_ingress_internals_unsupported", severity: "info", scope: "ingress", detail: "External proxy internals unavailable", domain_name: "" }];
  for (const status of ["expired", "hostname_mismatch", "untrusted", "self_signed", "unreachable"]) {
    const domain = structuredClone(productsForFixture("products")[0].environments[0].topology.observed.tls_domains[0]);
    domain.status = status;
    domain.provenance.stale_after = lane.provenance.stale_after;
    lane.topology.observed.tls_domains = [domain];
    assert.equal(environmentOperationalTone(lane), "danger");
  }
});

test("stale or unverified identity cannot show green with passing HTTP", () => {
  /** @type {import("../src/generated/openapi.ts").DataProvenance["freshness_status"][]} */
  const statuses = ["stale", "recorded", "missing"];
  for (const freshness_status of statuses) {
    const lane = environment();
    lane.provenance.freshness_status = freshness_status;
    assert.notEqual(environmentOperationalTone(lane), "verified");
  }
});

test("health failure or a mismatch incident turns the lane red", () => {
  for (const check of [{ status: "fail" }, { incident_status: "open" }]) {
    const lane = environment();
    Object.assign(lane.health_monitoring.checks[0], check);
    assert.equal(environmentOperationalTone(lane), "danger");
  }
});

test("every effective check and topology warning still matters", () => {
  const lane = environment();
  const check = structuredClone(lane.health_monitoring.checks[0]);
  check.status = "missing";
  check.trust_state = "missing";
  lane.health_monitoring.checks.push(check);
  assert.equal(environmentOperationalTone(lane), "warning");
  lane.health_monitoring.checks.pop();
  const product = productsForFixture("products")[0];
  const warning = structuredClone(product.environments.flatMap(environment => environment.topology.warnings)[0]);
  warning.severity = "warning";
  lane.topology.warnings.push(warning);
  assert.equal(environmentOperationalTone(lane), "warning");
});

test("an open page cannot keep an expired verification green", () => {
  const lane = environment();
  const now = Date.now();
  lane.provenance.stale_after = new Date(now).toISOString();
  assert.equal(environmentOperationalTone(lane, now), "verified");
  assert.equal(environmentOperationalTone(lane, now + 1), "warning");
});

test("a recorded runtime mismatch stays red without an effective monitor check", () => {
  const lane = environment();
  lane.health_monitoring.checks = [];
  lane.topology.observed.placement.runtime_identity_status = "mismatch";
  assert.equal(environmentOperationalTone(lane), "danger");
});

test("advisory missing runtime identity is unverified rather than a failure", () => {
  const lane = environment();
  lane.provenance.freshness_status = "recorded";
  lane.topology.observed.placement.runtime_identity_status = "missing";
  assert.equal(environmentOperationalTone(lane), "warning");
});

test("product and lane badges expire together with the signal", () => {
  const lane = environment();
  const product = structuredClone(productsForFixture("products")[0]);
  product.environments = [lane];
  const expired = expireProductEvidence(product, Date.parse(lane.provenance.stale_after) + 1);
  assert.equal(expired.trust_state, "stale");
  assert.equal(expired.environments[0].trust_state, "stale");
  assert.equal(environmentOperationalTone(expired.environments[0]), "warning");
});


test("route proof expiry stays diagnostic while health and identity remain fresh", () => {
  const lane = environment();
  const deadline = Date.now();
  const sourceTrust = lane.topology.provider_recorded.trust_state;
  lane.topology.provider_recorded.provenance.stale_after = new Date(deadline).toISOString();
  assert.equal(environmentOperationalTone(lane, deadline), "verified");
  const expired = expireEnvironmentEvidence(lane, deadline + 1);
  assert.equal(expired.topology.provider_recorded.trust_state, "stale");
  assert.equal(expired.health_monitoring.checks[0].trust_state, "verified");
  assert.equal(environmentOperationalTone(lane, deadline + 1), "verified");
  assert.equal(lane.topology.provider_recorded.trust_state, sourceTrust);
});

test("missing and expired route authority cannot hide verified public health", () => {
  for (const trust_state of ["missing", "stale"]) {
    const lane = environment();
    lane.trust_state = trust_state;
    lane.topology.provider_recorded.trust_state = trust_state;
    lane.topology.provider_recorded.provenance.stale_after = new Date(Date.now() - 1).toISOString();
    const warning = { code: trust_state === "missing" ? "missing_route_authority" : "stale_route_authority", severity: "error", scope: "authority", detail: "Route proof unavailable", domain_name: "" };
    lane.topology.warnings = [warning];
    lane.warnings = [warning.detail];
    assert.equal(environmentOperationalTone(lane), "verified");
    lane.health_monitoring.checks[0].status = "fail";
    assert.equal(environmentOperationalTone(lane), "danger");
  }
});

test("a required public website cannot use private health as its public proof", () => {
  const lane = environment();
  lane.health_monitoring.checks[0].kind = "private_http";
  assert.equal(environmentOperationalTone(lane), "warning");
});

test("fresh passing HTTP cannot make absent runtime identity green", () => {
  for (const status of ["unchecked", "missing", "unverifiable"]) {
    const lane = environment();
    lane.topology.observed.placement.runtime_identity_status = status;
    assert.equal(environmentOperationalTone(lane), "warning");
  }
});

test("public green requires fresh DNS, named TLS and the expected public runtime", () => {
  for (const update of [
    { tls_domains: [] },
    { tls_domains: environment().topology.observed.tls_domains.map(domain => ({ ...domain, validated_address_count: 0 })) },
    { tls_domains: environment().topology.observed.tls_domains.map(domain => ({ ...domain, public_name_match: false })) },
    { tls_domains: environment().topology.observed.tls_domains.map(domain => ({ ...domain, days_remaining: null })) },
    { ingress: { ...environment().topology.observed.ingress, runtime_identity_status: "unchecked" } },
  ]) {
    const lane = environment();
    Object.assign(lane.topology.observed, update);
    assert.notEqual(environmentOperationalTone(lane), "verified");
  }
});

test("TLS proof expires independently and an old response cannot restore it", () => {
  const lane = environment();
  const source = productsForFixture("products")[0].environments[0].topology.observed.tls_domains[0];
  const domain = structuredClone(source);
  domain.status = "valid";
  domain.trust_state = "verified";
  domain.provenance.freshness_status = "verified";
  const deadline = Date.now();
  domain.stale_after = new Date(deadline).toISOString();
  domain.provenance.stale_after = domain.stale_after;
  lane.topology.observed.tls_domains = [domain];
  assert.equal(environmentOperationalTone(lane, deadline), "verified");
  const expired = expireEnvironmentEvidence(lane, deadline + 1);
  assert.equal(expired.topology.observed.tls_domains[0].trust_state, "stale");
  assert.equal(environmentOperationalTone(expired, deadline + 1), "warning");
  assert.equal(environmentOperationalTone(structuredClone(lane), deadline + 1), "warning");
  domain.stale_after = lane.provenance.stale_after;
  domain.provenance.stale_after = domain.stale_after;
  assert.equal(environmentOperationalTone(lane, deadline + 1), "verified");
});


test("explicit no-website applicability does not turn historical route expiry into a health gate", () => {
  const lane = environment();
  lane.topology.desired = { ...lane.topology.desired, public_website: "none" };
  lane.health_monitoring.monitoring_intent = "private";
  lane.health_monitoring.checks[0].kind = "private_http";
  lane.public_ingress.status = "not_expected";
  lane.topology.provider_recorded.provenance.stale_after = new Date(Date.now() - 1).toISOString();
  assert.equal(environmentOperationalTone(lane), "verified");
});

test("expired negative TLS observations remain failures", () => {
  const lane = environment();
  const domain = structuredClone(productsForFixture("products")[0].environments[0].topology.observed.tls_domains[0]);
  domain.status = "hostname_mismatch";
  domain.provenance.stale_after = new Date(Date.now() - 1).toISOString();
  lane.topology.observed.tls_domains = [domain];
  assert.equal(environmentOperationalTone(lane), "danger");
});
