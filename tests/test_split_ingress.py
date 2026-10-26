import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import patch
from urllib.parse import urlsplit

from pydantic import ValidationError

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.product_operational_readiness import (
    build_product_operational_readiness,
)
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneHealthCheck,
)
from control_plane.contracts.product_topology_read_model import build_product_environment_topology
from control_plane.contracts.public_dns import PublicDnsExpectedTarget
from control_plane.contracts.route_binding_record import (
    EnvironmentRouteBindingRecord,
    redacted_route_binding_record,
    route_binding_record_sha256,
)
from control_plane.http_app import RouteBindingReconcileEnvelope, create_launchplane_fastapi_app
from control_plane.contracts.runtime_identity import RuntimeIdentityStatus
from control_plane.contracts.public_ingress_monitoring import (
    PublicIngressObservationRecord,
    PublicIngressTargetObservation,
    health_monitor_freshness_seconds,
)
from control_plane.lane_runtime_verification import (
    read_check_runtime_verification,
    read_lane_runtime_verification,
)
from control_plane.outbound_http import (
    PublicHttpResponse,
    PublicHttpDestinationError,
    PublicHttpDestinationErrorCode,
    PublicTlsProbeResult,
    PublicTlsCertificate,
)
from control_plane.route_binding_reconcile import (
    RouteBindingExpectedCurrent,
    RouteBindingReconcileRequest,
    is_evidence_only_route_binding_refresh,
    plan_route_binding_reconcile,
)
from control_plane.split_ingress import (
    split_ingress_check_name,
    split_ingress_custody_hosts,
    split_ingress_check_base_url,
    split_ingress_monitoring_lane,
    split_ingress_monitoring_policy,
)
from control_plane.workflows.public_ingress_monitor import (
    PublicIngressMonitorTarget,
    check_public_ingress_target,
    discover_public_ingress_monitor_targets,
    fetch_public_tls,
    fetch_public_ingress_url,
)
from tests.support.auth import _identity, _StubVerifier
from tests.support.http import request
from tests.test_browser_route_repair import _BrowserRouteRepairFixture
from tests.test_product_operational_readiness import _inputs
from tests.test_product_topology_read_model import (
    _NOW,
    _http_observation,
    _lane_summary,
    _strict_public_profile,
    _TopologyStore,
)
from tests.test_route_binding_record import (
    _dokploy_target_record,
    _ingress_audit_record,
    _route_binding_policy,
    _route_binding_record,
)


def split_binding() -> EnvironmentRouteBindingRecord:
    payload = _route_binding_record(domain="business.example.test").model_dump(mode="json")
    payload["domains"].extend(
        [
            {"domain_name": "www.business.example.test", "role": "alias"},
            {"domain_name": "operator.example.test", "role": "alias"},
        ]
    )
    payload["external_ingress"] = {
        "domains": ["business.example.test", "www.business.example.test"],
        "custodian": "Office proxy operator",
        "evidence_url": "https://example.test/evidence/office-proxy",
        "attested_at": payload["updated_at"],
    }
    return EnvironmentRouteBindingRecord.model_validate(payload)


def split_observation(
    binding: EnvironmentRouteBindingRecord,
    profile: LaunchplaneProductProfileRecord,
    domain: str,
    *,
    status: RuntimeIdentityStatus = "match",
) -> PublicIngressObservationRecord:
    observed = _http_observation(
        domain_name=domain, monitoring_intent="public", runtime_identity_status=status
    )
    targets = observed.targets
    base = split_ingress_check_base_url(profile.lanes[0], observed.targets[0].url)
    if base:
        targets += (
            PublicIngressTargetObservation(
                target="base_url",
                url=base,
                final_url=base,
                status="pass",
                http_status=200,
                summary="Homepage is healthy.",
            ),
        )
    return observed.model_copy(
        update={
            "record_id": domain,
            "check_name": split_ingress_check_name(domain),
            "targets": targets,
        }
    )


class SplitIngressTests(unittest.TestCase):
    def test_split_projection_retains_checks_dns_and_recovery_policy(self) -> None:
        profile = _strict_public_profile(domain_name="business.example.test")
        lane = profile.lanes[0]
        binding = split_binding()
        effective = split_ingress_monitoring_lane(profile, lane, binding)
        self.assertEqual(
            {check.name for check in effective.health_monitoring.checks},
            {split_ingress_check_name(domain.domain_name) for domain in binding.domains},
        )
        self.assertTrue(
            all(check.require_runtime_identity for check in effective.health_monitoring.checks)
        )
        self.assertEqual(
            split_ingress_custody_hosts(binding, "https://outside.example.test/healthz"), ()
        )
        expectation = PublicDnsExpectedTarget(
            public_name=binding.domains[0].domain_name, addresses=("93.184.216.34",)
        )
        configured = lane.health_monitoring.checks[0].model_copy(
            update={"expected_dns_targets": (expectation,), "recovery_observation_threshold": 2}
        )
        lane = lane.model_copy(
            update={
                "health_monitoring": lane.health_monitoring.model_copy(
                    update={"checks": (configured,)}
                )
            }
        )
        profile = profile.model_copy(update={"lanes": (lane,)})
        first = split_ingress_monitoring_lane(profile, lane, binding)
        second = split_ingress_monitoring_lane(profile, first, binding)
        self.assertEqual(first, second)
        self.assertEqual(
            first.health_monitoring.checks[0].expected_dns_targets, configured.expected_dns_targets
        )
        self.assertTrue(
            all(
                check.recovery_observation_threshold == configured.recovery_observation_threshold
                for check in first.health_monitoring.checks
            )
        )
        contradictory = configured.model_copy(
            update={
                "name": "conflicting",
                "expected_dns_targets": (
                    expectation.model_copy(update={"addresses": ("1.1.1.1",)}),
                ),
            }
        )
        lane = lane.model_copy(
            update={
                "health_monitoring": lane.health_monitoring.model_copy(
                    update={"checks": (configured, contradictory)}
                )
            }
        )
        profile = profile.model_copy(update={"lanes": (lane,)})
        self.assertTrue(
            split_ingress_monitoring_policy(
                profile,
                lane,
                tuple(d.domain_name for d in binding.domains),
                expectation.public_name,
            ).conflict
        )

    def test_policy_normalizes_equivalent_https_authorities_without_widening_custody(self) -> None:
        profile = _strict_public_profile(domain_name="business.example.test")
        lane = profile.lanes[0]
        domains = tuple(d.domain_name for d in split_binding().domains)
        for authority, accepted in (
            ("Business.example.test", True),
            ("business.example.test:443", True),
            ("business.example.test:8443", False),
            ("user@business.example.test", False),
        ):
            url = f"https://{authority}/healthz"
            configured = lane.model_copy(
                update={
                    "health_url": url,
                    "health_monitoring": lane.health_monitoring.model_copy(
                        update={
                            "checks": (
                                lane.health_monitoring.checks[0].model_copy(update={"url": url}),
                            ),
                        }
                    ),
                }
            )
            candidate = profile.model_copy(update={"lanes": (configured,)})
            policy = split_ingress_monitoring_policy(candidate, configured, domains, domains[0])
            self.assertEqual(not policy.conflict, accepted, authority)
        insecure = lane.health_monitoring.checks[0].model_copy(
            update={"url": "http://business.example.test/healthz"}
        )
        configured = lane.model_copy(
            update={
                "health_monitoring": lane.health_monitoring.model_copy(
                    update={"checks": (insecure,)},
                )
            }
        )
        self.assertTrue(
            split_ingress_monitoring_policy(
                profile.model_copy(update={"lanes": (configured,)}),
                configured,
                domains,
                domains[0],
            ).conflict
        )

    def test_canonical_redirect_proof_and_custody_renewal_are_scoped_per_check(self) -> None:
        binding = split_binding().model_copy(
            update={"product": "example-site", "context": "example-context", "instance": "prod"}
        )
        profile = _strict_public_profile(domain_name="business.example.test")
        summary = _lane_summary()
        lane = split_ingress_monitoring_lane(profile, profile.lanes[0], binding)
        observations = tuple(
            split_observation(binding, profile, domain.domain_name) for domain in binding.domains
        )
        first = observations[0].targets[0]
        canonical = observations[1].targets[0].url
        redirected = observations[0].model_copy(
            update={
                "targets": (
                    first.model_copy(
                        update={
                            "final_url": canonical,
                            "redirect_count": 1,
                            "visited_urls": (first.url, canonical),
                        }
                    ),
                    *observations[0].targets[1:],
                )
            }
        )
        store = _TopologyStore(route_binding=binding, observations=(redirected, *observations[1:]))

        def status(check_name: str, authority: EnvironmentRouteBindingRecord) -> str:
            check = next(
                check for check in lane.health_monitoring.checks if check.name == check_name
            )
            return read_check_runtime_verification(
                record_store=store,
                profile=profile,
                lane=lane,
                summary=summary,
                check=check,
                binding=authority,
                now=_NOW,
            ).provenance.freshness_status

        self.assertEqual(status(redirected.check_name, binding), "verified")
        excursion = redirected.model_copy(
            update={
                "targets": (
                    first.model_copy(
                        update={
                            "redirect_count": 2,
                            "visited_urls": (first.url, observations[2].targets[0].url, first.url),
                        }
                    ),
                    *observations[0].targets[1:],
                )
            }
        )
        store.observations = (excursion, *observations[1:])
        self.assertNotEqual(status(redirected.check_name, binding), "verified")
        assert binding.external_ingress is not None
        renewed = binding.model_copy(
            update={
                "external_ingress": binding.external_ingress.model_copy(
                    update={"attested_at": _NOW.isoformat()}
                )
            }
        )
        self.assertNotEqual(status(observations[0].check_name, renewed), "verified")
        self.assertEqual(status(observations[2].check_name, renewed), "verified")
        replacement = binding.model_copy(
            update={
                "external_ingress": binding.external_ingress.model_copy(
                    update={
                        "custodian": "Successor at the same timestamp",
                        "attested_at": observations[1].observed_at,
                    }
                )
            }
        )
        self.assertNotEqual(status(observations[1].check_name, replacement), "verified")
        self.assertEqual(status(observations[2].check_name, replacement), "verified")

        queried = lane.model_copy(update={"health_url": f"{lane.health_url}?scope=runtime"})
        derived = split_ingress_monitoring_lane(profile, queried, binding)
        self.assertTrue(
            all(
                urlsplit(c.url).query == urlsplit(queried.health_url).query
                for c in derived.health_monitoring.checks
                if c.name in {o.check_name for o in observations}
            )
        )

    def test_probe_passes_custody_to_transport_and_retains_redirect_chain(self) -> None:
        summary = _lane_summary()
        assert summary.inventory is not None
        identity = summary.inventory.runtime_identity
        assert identity is not None
        initial = "https://business.example.test/healthz"
        canonical = "https://www.business.example.test/healthz"
        hosts = ("business.example.test", "www.business.example.test")
        target = PublicIngressMonitorTarget(
            product="example-site",
            repository="example/site",
            driver_id="generic-web",
            context="example-context",
            instance="prod",
            base_url="",
            health_url=initial,
            check_name=split_ingress_check_name(hosts[0]),
            check_kind="public_http",
            profile_sha256="",
            monitoring_intent="public",
            incident_eligible=True,
            expected_runtime_identity=identity,
            require_runtime_identity=True,
            allowed_https_hosts=hosts,
        )
        response = PublicHttpResponse(
            status_code=200,
            final_url=canonical,
            redirect_count=1,
            visited_urls=(initial, canonical),
            headers=(("content-type", "application/json"),),
            body=json.dumps({"runtime_identity": identity.model_dump(mode="json")}).encode(),
        )

        def legacy_http_get(_url: str, _timeout: int) -> Any:
            raise AssertionError("A legacy transport cannot handle custody-bound probes")

        with patch(
            "control_plane.workflows.public_ingress_monitor.request_public_http",
            return_value=response,
        ) as transport:
            observation = check_public_ingress_target(
                target=target,
                checked_at=_NOW.isoformat(),
                timeout_seconds=5,
                http_get=fetch_public_ingress_url,
                tls_get=fetch_public_tls,
            )
        self.assertEqual(observation.status, "pass")
        self.assertEqual(observation.targets[0].visited_urls, response.visited_urls)
        self.assertEqual(transport.call_args.kwargs["allowed_https_hosts"], hosts)
        with patch(
            "control_plane.workflows.public_ingress_monitor.request_public_http"
        ) as no_network:
            refused = check_public_ingress_target(
                target=replace(
                    target,
                    expected_dns_targets=(
                        PublicDnsExpectedTarget(public_name=hosts[0], addresses=("93.184.216.34",)),
                    ),
                ),
                checked_at=_NOW.isoformat(),
                timeout_seconds=5,
                dns_get=lambda _host: self.fail("Refused transport must not resolve DNS"),
                http_get=legacy_http_get,
                tls_get=fetch_public_tls,
            )
        self.assertEqual(refused.status, "fail")
        no_network.assert_not_called()
        dns_target = PublicDnsExpectedTarget(public_name=hosts[0], addresses=("93.184.216.34",))
        failures: tuple[tuple[PublicHttpDestinationErrorCode, tuple[str, ...]], ...] = (
            ("invalid_url", hosts),
            ("private_url", ()),
        )
        for code, custody in failures:

            def dns_get(_url: str) -> tuple[str, ...]:
                if code == "private_url":
                    raise PublicHttpDestinationError(code, "Resolved to a private address")
                return dns_target.addresses

            with patch(
                "control_plane.workflows.public_ingress_monitor.request_public_http",
                side_effect=PublicHttpDestinationError(code, "Redirect destination refused"),
            ):
                failed = check_public_ingress_target(
                    target=replace(
                        target, allowed_https_hosts=custody, expected_dns_targets=(dns_target,)
                    ),
                    checked_at=_NOW.isoformat(),
                    timeout_seconds=5,
                    http_get=fetch_public_ingress_url,
                    tls_get=fetch_public_tls,
                    dns_get=dns_get,
                )
            dns = failed.targets[0].dns
            assert dns is not None
            self.assertEqual(failed.failure_code, code)
            if custody:
                self.assertEqual(dns.observed_addresses, dns_target.addresses)
            else:
                self.assertEqual(dns.failure_code, code)
        homepage = "https://business.example.test"
        with patch(
            "control_plane.workflows.public_ingress_monitor.request_public_http",
            side_effect=lambda url, **_kwargs: (
                replace(
                    response, status_code=500, final_url=url, redirect_count=0, visited_urls=(url,)
                )
                if url == homepage
                else response
            ),
        ):
            unhealthy = check_public_ingress_target(
                target=replace(target, base_url=homepage),
                checked_at=_NOW.isoformat(),
                timeout_seconds=5,
                http_get=fetch_public_ingress_url,
                tls_get=fetch_public_tls,
            )
        self.assertEqual(unhealthy.status, "fail")
        self.assertEqual(
            {t.url: t.status for t in unhealthy.targets}, {homepage: "fail", initial: "pass"}
        )
        profile = _strict_public_profile(domain_name=hosts[0])
        root_lane = profile.lanes[0].model_copy(update={"health_url": homepage + "/"})
        self.assertEqual(split_ingress_check_base_url(root_lane, homepage + "/"), "")

    def test_readers_reject_profile_changes_and_cross_custody_redirects(self) -> None:
        binding = split_binding().model_copy(
            update={"product": "example-site", "context": "example-context", "instance": "prod"}
        )
        profile = _strict_public_profile(domain_name="business.example.test")
        summary = _lane_summary()
        observations = tuple(
            split_observation(binding, profile, domain.domain_name) for domain in binding.domains
        )
        original = _http_observation(
            domain_name="business.example.test", monitoring_intent="public"
        )
        store = _TopologyStore(route_binding=binding, observations=(*observations, original))
        for changed_profile, changed_target in (
            (profile.model_copy(update={"updated_at": _NOW.isoformat()}), {}),
            (profile, {"final_url": "https://operator.example.test/healthz", "redirect_count": 1}),
            (profile, {"final_url": "https://business.example.test/healthz", "redirect_count": 2}),
        ):
            with self.subTest(profile=changed_profile.updated_at, target=changed_target):
                store.observations = (
                    observations[0].model_copy(
                        update={
                            "targets": tuple(
                                target.model_copy(update=changed_target)
                                for target in observations[0].targets
                            )
                        }
                    ),
                    *observations[1:],
                    original,
                )
                topology = build_product_environment_topology(
                    record_store=store,
                    profile=changed_profile,
                    lane=changed_profile.lanes[0],
                    lane_summary=summary,
                    now=_NOW,
                )
                verification = read_lane_runtime_verification(
                    record_store=store,
                    profile=changed_profile,
                    lane=changed_profile.lanes[0],
                    summary=summary,
                    now=_NOW,
                )
                self.assertNotEqual(verification.provenance.freshness_status, "verified")
                self.assertTrue(
                    any(
                        w.domain_name == "business.example.test"
                        and w.scope == "observation"
                        and w.severity == "error"
                        for w in topology.warnings
                    )
                )

    def test_business_and_operator_hosts_retain_distinct_custody(self) -> None:
        binding = split_binding()
        self.assertEqual(binding.ingress_for_domain("business.example.test").provider, "external")
        self.assertEqual(binding.tls_for_domain("www.business.example.test").owner, "external")
        self.assertEqual(binding.ingress_for_domain("operator.example.test"), binding.ingress)
        self.assertEqual(binding.tls_for_domain("operator.example.test"), binding.tls)
        public = redacted_route_binding_record(binding)
        self.assertEqual(public.external_ingress, binding.external_ingress)
        self.assertEqual(public.record_sha256, route_binding_record_sha256(binding))

    def test_external_scope_cannot_hide_or_replace_managed_hosts(self) -> None:
        payload = split_binding().model_dump(mode="json")
        for domains in (["unknown.example.test"], [d["domain_name"] for d in payload["domains"]]):
            with self.subTest(domains=domains), self.assertRaises(ValidationError):
                EnvironmentRouteBindingRecord.model_validate(
                    {
                        **payload,
                        "external_ingress": {**payload["external_ingress"], "domains": domains},
                    }
                )
        with self.assertRaises(ValidationError):
            EnvironmentRouteBindingRecord.model_validate(
                {
                    **payload,
                    "external_ingress": {
                        **payload["external_ingress"],
                        "evidence_url": "https://example.test/report?token=private",
                    },
                }
            )

    def test_unknown_host_has_no_inferred_authority(self) -> None:
        with self.assertRaises(ValueError):
            split_binding().ingress_for_domain("unknown.example.test")

    def test_one_failed_or_old_host_cannot_borrow_another_paths_pass(self) -> None:
        binding = split_binding().model_copy(
            update={
                "product": "example-site",
                "context": "example-context",
                "instance": "prod",
            }
        )
        profile = _strict_public_profile(domain_name="business.example.test")
        summary = _lane_summary()
        observations = tuple(
            split_observation(binding, profile, domain.domain_name) for domain in binding.domains
        )
        original = _http_observation(
            domain_name="business.example.test", monitoring_intent="public"
        )
        store = _TopologyStore(route_binding=binding, observations=(*observations, original))

        def failing_hosts() -> set[str]:
            topology = build_product_environment_topology(
                record_store=store,
                profile=profile,
                lane=profile.lanes[0],
                lane_summary=summary,
                now=_NOW,
            )
            readiness = build_product_operational_readiness(replace(_inputs(), topology=topology))
            if any(
                warning.scope == "observation"
                and warning.severity == "error"
                and warning.domain_name
                for warning in topology.warnings
            ):
                self.assertEqual(
                    next(
                        dimension.state
                        for dimension in readiness.dimensions
                        if dimension.dimension == "topology"
                    ),
                    "blocked",
                )
            return {
                warning.domain_name
                for warning in topology.warnings
                if warning.scope == "observation"
                and warning.severity == "error"
                and warning.domain_name
            }

        self.assertEqual(failing_hosts(), set())
        verification = read_lane_runtime_verification(
            record_store=store, profile=profile, lane=profile.lanes[0], summary=summary, now=_NOW
        )
        self.assertEqual(verification.provenance.freshness_status, "verified")
        store.route_binding = binding
        failed = split_observation(
            binding, profile, "business.example.test", status="mismatch"
        ).model_copy(
            update={
                "check_name": observations[0].check_name,
                "record_id": "business-failure",
            }
        )
        stale = observations[1].model_copy(
            update={
                "observed_at": (
                    _NOW - timedelta(seconds=health_monitor_freshness_seconds(binding.instance) + 1)
                ).isoformat()
            }
        )
        store.observations = (failed, stale, observations[2], original)
        self.assertEqual(failing_hosts(), {"business.example.test", "www.business.example.test"})
        verification = read_lane_runtime_verification(
            record_store=store, profile=profile, lane=profile.lanes[0], summary=summary, now=_NOW
        )
        self.assertNotEqual(verification.provenance.freshness_status, "verified")


class SplitIngressBrowserTests(_BrowserRouteRepairFixture):
    def seed_split(self) -> dict[str, Any]:
        payload = self.seed_binding()
        self.target = _dokploy_target_record(
            domains=("app.example.test", "www.example.test", "operator.example.test")
        )
        self.store.write_dokploy_target_record(self.target)
        self.store.write_provider_target_record(
            ProviderTargetRecord.from_dokploy_records(
                target_record=self.target,
                target_id_record=self.target_id,
            )
        )
        self.store.write_ingress_route_audit_record(
            _ingress_audit_record(
                record_id="audit-alias",
                domains=("operator.example.test",),
            )
        )
        profile = self.store.read_product_profile_record("example-product").model_dump(mode="json")
        profile["driver_id"] = "generic-web"
        profile["lanes"][0]["base_url"] = "https://app.example.test"
        profile["lanes"][0]["health_url"] = "https://app.example.test/healthz"
        profile["lanes"][0]["health_monitoring"]["monitoring_intent"] = "public"
        self.store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(profile)
        )
        return {
            **payload,
            "external_ingress": {
                "domains": ["app.example.test", "www.example.test"],
                "custodian": "Business proxy operator",
                "evidence_url": "https://example.test/report",
            },
        }

    async def test_conflicting_dns_policy_blocks_admission_and_existing_observations(self) -> None:
        payload = self.seed_split()
        route = "/v1/route-bindings/reconcile"
        review = (await self.post(route, payload)).json()["review"]
        binding = EnvironmentRouteBindingRecord.model_validate(
            {
                key: value
                for key, value in review["proposed_record"].items()
                if key != "record_sha256"
            }
        )
        profile = self.store.read_product_profile_record(binding.product)
        lane = profile.lanes[0]
        checks = tuple(
            ProductLaneHealthCheck(
                name=f"dns-{index}",
                kind="public_http",
                url=lane.health_url,
                expected_dns_targets=(
                    PublicDnsExpectedTarget(
                        public_name="app.example.test",
                        addresses=(address,),
                    ),
                ),
            )
            for index, address in enumerate(("93.184.216.34", "1.1.1.1"))
        )
        lane = lane.model_copy(
            update={
                "health_monitoring": lane.health_monitoring.model_copy(
                    update={"checks": checks},
                )
            }
        )
        profile = profile.model_copy(update={"lanes": (lane,)})
        self.store.write_product_profile_record(profile)
        blocked = (await self.post(route, payload)).json()["review"]
        self.assertEqual(blocked["status"], "blocked")
        self.assertIn(
            "split_ingress_monitoring_policy_conflict",
            {finding["code"] for finding in blocked["findings"]},
        )
        self.store.write_route_binding_record(binding)
        targets = discover_public_ingress_monitor_targets(self.store, profiles=(profile,))
        target = next(
            t for t in targets if t.check_kind == "public_http" and t.health_url == lane.health_url
        )

        def no_network(*_args: Any, **_kwargs: Any) -> Any:
            self.fail("Conflicting DNS policy must not connect or resolve DNS")

        observed = check_public_ingress_target(
            target=target,
            checked_at=_NOW.isoformat(),
            timeout_seconds=5,
            http_get=no_network,
            tls_get=no_network,
            dns_get=no_network,
        )
        self.assertEqual(observed.status, "fail")
        self.assertEqual(observed.failure_code, "invalid_url")
        topology = build_product_environment_topology(
            record_store=self.store,
            profile=profile,
            lane=lane,
            lane_summary=None,
            now=_NOW,
        )
        self.assertTrue(
            any(w.code == "split_ingress_monitoring_policy_conflict" for w in topology.warnings)
        )

    async def test_additional_public_checks_require_a_coverage_decision(self) -> None:
        payload = self.seed_split()
        profile = self.store.read_product_profile_record(payload["product"])
        lane = profile.lanes[0]
        for url in ("https://app.example.test/checkout", "https://outside.example.test/healthz"):
            extra = ProductLaneHealthCheck(name="checkout", kind="public_http", url=url)
            configured = lane.model_copy(
                update={
                    "health_monitoring": lane.health_monitoring.model_copy(
                        update={"checks": (*lane.health_monitoring.checks, extra)},
                    )
                }
            )
            changed = profile.model_copy(update={"lanes": (configured,)})
            self.store.write_product_profile_record(changed)
            review = (await self.post("/v1/route-bindings/reconcile", payload)).json()["review"]
            self.assertEqual(review["status"], "blocked")
            self.assertIn(
                "split_ingress_monitoring_policy_conflict", {f["code"] for f in review["findings"]}
            )
            disabled = extra.model_copy(update={"enabled": False})
            configured = configured.model_copy(
                update={
                    "health_monitoring": configured.health_monitoring.model_copy(
                        update={"checks": (*lane.health_monitoring.checks, disabled)},
                    )
                }
            )
            self.store.write_product_profile_record(
                profile.model_copy(update={"lanes": (configured,)})
            )
            review = (await self.post("/v1/route-bindings/reconcile", payload)).json()["review"]
            self.assertEqual(review["status"], "ready", review)

    async def test_signed_review_saves_split_and_all_readers_keep_each_host(self) -> None:
        payload = self.seed_split()
        route = "/v1/route-bindings/reconcile"
        response = await self.post(route, payload)
        self.assertEqual(response.status_code, 202, response.text)
        review = response.json()["review"]
        self.assertEqual(review["status"], "ready", review)
        self.assertEqual(set(review["proposed_domains"]), set(self.target.domains))
        applied = await self.post(
            route,
            {
                **payload,
                "external_ingress": review["proposed_record"]["external_ingress"],
                "mode": "apply",
                "reason": "Record both existing ingress paths.",
                "confirmation": review["expected_confirmation"],
                "browser_review": {
                    "product": payload["product"],
                    "reviewed_plan_sha256": review["plan_sha256"],
                },
            },
            "split-existing-routes",
        )
        self.assertEqual(applied.status_code, 202, applied.text)
        current = self.store.read_route_binding_record(
            product=payload["product"],
            context_name=payload["context"],
            instance_name=payload["instance"],
        )
        assert current.external_ingress is not None
        self.assertEqual(
            current.external_ingress.domains, tuple(payload["external_ingress"]["domains"])
        )
        profile = self.store.read_product_profile_record(payload["product"])
        topology = build_product_environment_topology(
            record_store=self.store, profile=profile, lane=profile.lanes[0], lane_summary=None
        )
        self.assertEqual(
            {domain.domain_name for domain in topology.desired.domains}, set(self.target.domains)
        )
        self.assertFalse(
            {
                "domain_divergence",
                "missing_route_authority",
                "ingress_ownership_unknown",
                "tls_ownership_unknown",
            }
            & {warning.code for warning in topology.warnings}
        )
        self.assertEqual(
            {
                warning.domain_name
                for warning in topology.warnings
                if warning.code == "public_ingress_observation_missing" and warning.domain_name
            },
            set(self.target.domains),
        )
        hosts = {domain.domain_name: domain for domain in topology.provider_recorded.domains}
        assert hosts["app.example.test"].ingress is not None
        assert hosts["operator.example.test"].ingress is not None
        self.assertEqual(hosts["app.example.test"].ingress.provider, "external")
        self.assertEqual(hosts["operator.example.test"].ingress.provider, current.ingress.provider)
        targets = discover_public_ingress_monitor_targets(self.store, profiles=(profile,))
        strict = {
            target.check_name: target
            for target in targets
            if target.check_kind == "public_http"
            and target.check_name
            in {split_ingress_check_name(host) for host in self.target.domains}
        }
        self.assertEqual(
            set(strict), {split_ingress_check_name(host) for host in self.target.domains}
        )
        self.assertTrue(
            all(
                target.require_runtime_identity and target.route_binding_sha256
                for target in strict.values()
            )
        )
        self.assertEqual(
            {
                target.tls_domain_name: target.tls_owner
                for target in targets
                if target.check_kind == "tls"
            },
            {
                "app.example.test": "external",
                "www.example.test": "external",
                "operator.example.test": current.tls.owner,
            },
        )
        replay = await self.post(
            route, {**payload, "external_ingress": review["proposed_record"]["external_ingress"]}
        )
        self.assertEqual(replay.json()["review"]["status"], "unchanged")
        machine = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_route_binding_policy(action="route_binding.apply"),
            record_store_factory=lambda: self.store,
        )
        refresh_payload = {
            "product": current.product,
            "context": current.context,
            "instance": current.instance,
            "expected_current": {
                "state": "present",
                "record_sha256": route_binding_record_sha256(current),
            },
            "mode": "apply",
            "reason": "Refresh unchanged managed evidence.",
            "confirmation": review["expected_confirmation"],
        }
        refreshed = await request(
            machine,
            "POST",
            route,
            payload=refresh_payload,
            headers={
                "Authorization": "Bearer valid-token",
                "Idempotency-Key": "split-machine-refresh",
            },
        )
        self.assertEqual(refreshed.status_code, 202, refreshed.text)
        self.assertEqual(
            self.store.read_route_binding_record(
                product=current.product,
                context_name=current.context,
                instance_name=current.instance,
            ).external_ingress,
            current.external_ingress,
        )

    async def test_external_scope_change_after_review_cannot_apply(self) -> None:
        payload = self.seed_split()
        review = (await self.post("/v1/route-bindings/reconcile", payload)).json()["review"]
        response = await self.post(
            "/v1/route-bindings/reconcile",
            {
                **payload,
                "external_ingress": {
                    **payload["external_ingress"],
                    "domains": ["www.example.test"],
                },
                "mode": "apply",
                "reason": "Changed custody after review.",
                "confirmation": review["expected_confirmation"],
                "browser_review": {
                    "product": payload["product"],
                    "reviewed_plan_sha256": review["plan_sha256"],
                },
            },
            "split-stale-scope",
        )
        self.assertEqual(response.status_code, 409, response.text)
        with self.assertRaises(FileNotFoundError):
            self.store.read_route_binding_record(
                product=payload["product"],
                context_name=payload["context"],
                instance_name=payload["instance"],
            )

    async def test_signed_review_can_change_or_relinquish_custody_with_managed_proof(self) -> None:
        payload = self.seed_split()
        route = "/v1/route-bindings/reconcile"

        async def save(candidate: dict[str, Any], key: str) -> dict[str, Any]:
            with patch(
                "control_plane.http_app.utc_now_timestamp", return_value="2026-10-26T20:00:00Z"
            ):
                review = (await self.post(route, candidate)).json()["review"]
            self.assertEqual(review["status"], "ready", review)
            proposed = review["proposed_record"]
            apply_payload = {
                **candidate,
                "mode": "apply",
                "reason": "Record the reviewed custody.",
                "external_ingress": proposed.get("external_ingress"),
                "expected_current": review["expected_current"],
                "confirmation": review["expected_confirmation"],
                "browser_review": {
                    "product": payload["product"],
                    "reviewed_plan_sha256": review["plan_sha256"],
                },
            }
            with patch(
                "control_plane.http_app.utc_now_timestamp", return_value="2026-10-26T20:00:01Z"
            ):
                applied = await self.post(route, apply_payload, key)
            self.assertEqual(applied.status_code, 202, applied.text)
            saved_record: dict[str, Any] = applied.json()["review"]["proposed_record"]
            if saved_record.get("external_ingress"):
                self.assertEqual(
                    saved_record["external_ingress"]["attested_at"], "2026-10-26T20:00:01Z"
                )
            return saved_record

        await save(payload, "create-split-custody")
        changed = {
            **payload,
            "external_ingress": {
                **payload["external_ingress"],
                "custodian": "Successor custodian",
                "evidence_url": "https://example.test/new-report",
                "attested_at": "2025-01-01T00:00:00Z",
            },
        }
        updated = await save(changed, "update-split-custody")
        self.assertEqual(
            updated["external_ingress"]["custodian"], changed["external_ingress"]["custodian"]
        )
        blocked = (await self.post(route, {**payload, "external_ingress": None})).json()["review"]
        self.assertEqual(blocked["status"], "blocked")
        self.store.write_ingress_route_audit_record(
            _ingress_audit_record(record_id="audit-all-managed", domains=self.target.domains)
        )
        managed = await save({**payload, "external_ingress": None}, "relinquish-split-custody")
        self.assertNotIn("external_ingress", managed)

    async def test_http_only_managed_alias_is_a_blocked_plan(self) -> None:
        payload = self.seed_split()
        self.store.write_ingress_route_audit_record(
            _ingress_audit_record(
                record_id="audit-alias",
                domains=("operator.example.test",),
                tls_owner="none",
                provider_certificate_ref="",
            )
        )
        response = await self.post("/v1/route-bindings/reconcile", payload)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(
            [finding["code"] for finding in response.json()["review"]["findings"]],
            ["managed_tls_ownership_required"],
        )

    async def test_custody_review_cannot_absorb_unrelated_authority_changes(self) -> None:
        payload = self.seed_split()
        initial = plan_route_binding_reconcile(
            record_store=self.store,
            request=RouteBindingReconcileRequest.model_validate(
                {
                    **{
                        key: value
                        for key, value in payload.items()
                        if key not in {"mode", "browser_review"}
                    },
                    "evaluated_at": "2026-07-20T00:00:00Z",
                    "allow_authority_change": True,
                }
            ),
        )
        assert initial.record is not None
        current = initial.record
        for changes in (
            {
                "provider_target": current.provider_target.model_copy(
                    update={"target_name": "different-destination"}
                )
            },
            {"ingress": current.ingress.model_copy(update={"endpoint_key": "different-edge"})},
            {"tls": current.tls.model_copy(update={"owner": "launchplane"})},
            {"status": "disabled"},
        ):
            with self.subTest(changes=changes):
                changed = current.model_copy(update=changes)
                with patch.object(self.store, "read_route_binding_record", return_value=changed):
                    plan = plan_route_binding_reconcile(
                        record_store=self.store,
                        request=RouteBindingReconcileRequest(
                            product=current.product,
                            context=current.context,
                            instance=current.instance,
                            expected_current=RouteBindingExpectedCurrent(
                                state="present", record_sha256=route_binding_record_sha256(changed)
                            ),
                            external_ingress=current.external_ingress,
                            allow_authority_change=True,
                            evaluated_at="2026-07-20T00:00:00Z",
                        ),
                    )
                self.assertEqual(plan.status, "conflict", plan)
                self.assertEqual(plan.operation, "none")

    async def test_managed_apply_grant_cannot_establish_split_custody(self) -> None:
        payload = self.seed_split()
        payload.pop("browser_review")
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_route_binding_policy(action="route_binding.apply"),
            record_store_factory=lambda: self.store,
        )
        response = await request(
            app,
            "POST",
            "/v1/route-bindings/reconcile",
            payload={
                **payload,
                "mode": "apply",
                "reason": "Recognize split custody.",
                "confirmation": RouteBindingReconcileEnvelope.model_validate(
                    payload
                ).expected_confirmation,
            },
            headers={
                "Authorization": "Bearer valid-token",
                "Idempotency-Key": "split-machine-apply",
            },
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_refresh_preserves_external_custody_and_missing_alias_audit_blocks(self) -> None:
        payload = self.seed_split()
        evaluated_at = "2026-07-20T00:00:00Z"
        external = {**payload["external_ingress"], "attested_at": "2026-07-01T00:00:00Z"}
        planned = plan_route_binding_reconcile(
            record_store=self.store,
            request=RouteBindingReconcileRequest.model_validate(
                {
                    **{
                        key: value
                        for key, value in payload.items()
                        if key not in {"mode", "browser_review"}
                    },
                    "external_ingress": external,
                    "evaluated_at": evaluated_at,
                    "allow_authority_change": True,
                }
            ),
        )
        self.assertEqual(planned.status, "ready", planned.findings)
        assert planned.record is not None
        assert planned.record.external_ingress is not None
        self.assertEqual(planned.record.external_ingress.attested_at, evaluated_at)
        with patch.object(self.store, "read_route_binding_record", return_value=planned.record):
            refresh = plan_route_binding_reconcile(
                record_store=self.store,
                request=RouteBindingReconcileRequest(
                    product=planned.record.product,
                    context=planned.record.context,
                    instance=planned.record.instance,
                    expected_current=RouteBindingExpectedCurrent(
                        state="present", record_sha256=route_binding_record_sha256(planned.record)
                    ),
                    evaluated_at=(
                        datetime.fromisoformat(
                            planned.record.source.stale_after.replace("Z", "+00:00")
                        )
                        + timedelta(seconds=1)
                    ).isoformat(),
                ),
            )
        self.assertTrue(is_evidence_only_route_binding_refresh(refresh))
        assert refresh.record is not None
        self.assertEqual(refresh.record.external_ingress, planned.record.external_ingress)
        self.store.write_ingress_route_audit_record(
            _ingress_audit_record(record_id="audit-alias", domains=("foreign.example.test",))
        )
        missing = await self.post("/v1/route-bindings/reconcile", payload)
        self.assertEqual(
            [finding["code"] for finding in missing.json()["review"]["findings"]],
            ["ingress_audit_missing"],
        )

    async def test_current_stored_sources_verify_without_a_refresh_write(self) -> None:
        payload = self.seed_split()
        plan = plan_route_binding_reconcile(
            record_store=self.store,
            request=RouteBindingReconcileRequest.model_validate(
                {
                    **{
                        key: value
                        for key, value in payload.items()
                        if key not in {"mode", "browser_review"}
                    },
                    "evaluated_at": "2026-07-20T00:00:00Z",
                    "allow_authority_change": True,
                    "external_ingress": {
                        **payload["external_ingress"],
                        "attested_at": "2025-07-01T00:00:00Z",
                    },
                }
            ),
        )
        assert plan.record is not None
        current = plan.record
        self.store.write_route_binding_record(current)
        evaluated = datetime.fromisoformat(
            current.source.stale_after.replace("Z", "+00:00")
        ) + timedelta(seconds=1)
        profile = self.store.read_product_profile_record(current.product)

        def topology() -> Any:
            return build_product_environment_topology(
                record_store=self.store,
                profile=profile,
                lane=profile.lanes[0],
                lane_summary=None,
                now=evaluated,
            )

        def tls_get(host: str, _timeout: int) -> PublicTlsProbeResult:
            return PublicTlsProbeResult(
                status="valid",
                failure_code=None,
                summary="Valid certificate.",
                validated_address_count=1,
                certificate=PublicTlsCertificate(
                    issuer="Fixture CA",
                    subject=f"CN={host}",
                    not_before=(evaluated - timedelta(days=1)).isoformat(),
                    not_after=(evaluated + timedelta(days=100)).isoformat(),
                    days_remaining=100,
                    public_name_match=True,
                    public_name_match_source="san",
                    presented_san_count=1,
                    presented_name_evidence=(host,),
                ),
            )

        records = []
        for target in discover_public_ingress_monitor_targets(self.store, profiles=(profile,)):
            if target.check_kind != "tls":
                continue
            record = check_public_ingress_target(
                target=target,
                checked_at=evaluated.isoformat(),
                timeout_seconds=5,
                http_get=fetch_public_ingress_url,
                tls_get=tls_get,
            )
            self.store.write_public_ingress_observation_record(record)
            records.append(record)
        self.assertEqual(
            {r.targets[0].tls.public_name for r in records if r.targets[0].tls is not None},
            {d.domain_name for d in current.domains},
        )
        observed = topology()
        self.assertEqual(observed.provider_recorded.trust_state, "recorded")
        self.assertFalse(any(w.code == "stale_route_authority" for w in observed.warnings))
        self.assertEqual(
            self.store.read_route_binding_record(
                product=current.product,
                context_name=current.context,
                instance_name=current.instance,
            ),
            current,
        )
        profile = profile.model_copy(
            update={
                "display_name": "Renamed only",
                "updated_at": (evaluated - timedelta(seconds=1)).isoformat(),
            }
        )
        self.store.write_product_profile_record(profile)
        self.assertFalse(any(w.code == "stale_route_authority" for w in topology().warnings))
        original = records[0]
        tls_target = original.targets[0]
        assert tls_target.tls is not None
        for versions in (
            {},
            {
                **tls_target.tls.recorded.source_versions,
                current.source.source_record_ids[0]: "older-version",
            },
        ):
            altered = tls_target.tls.model_copy(
                update={
                    "recorded": tls_target.tls.recorded.model_copy(
                        update={"source_versions": versions}
                    )
                }
            )
            self.store.write_public_ingress_observation_record(
                original.model_copy(
                    update={"targets": (tls_target.model_copy(update={"tls": altered}),)}
                )
            )
            self.assertTrue(
                any(
                    w.code == "stale_route_authority"
                    and w.domain_name == tls_target.tls.public_name
                    for w in topology().warnings
                )
            )
        stale_tls = tls_target.tls.model_copy(
            update={
                "probe": tls_target.tls.probe.model_copy(
                    update={"stale_after": (evaluated - timedelta(seconds=1)).isoformat()}
                )
            }
        )
        self.store.write_public_ingress_observation_record(
            original.model_copy(
                update={"targets": (tls_target.model_copy(update={"tls": stale_tls}),)}
            )
        )
        self.assertTrue(any(w.code == "stale_tls_observation" for w in topology().warnings))
        self.store.write_public_ingress_observation_record(original)
        assert current.external_ingress is not None
        changed_custody = current.model_copy(
            update={
                "external_ingress": current.external_ingress.model_copy(
                    update={
                        "custodian": "Successor, same timestamp",
                        "attested_at": evaluated.isoformat(),
                    }
                )
            }
        )
        self.store.write_route_binding_record(changed_custody)
        self.assertEqual(
            {w.domain_name for w in topology().warnings if w.code == "stale_route_authority"},
            set(current.external_ingress.domains),
        )
        self.store.write_route_binding_record(current)
        with patch.object(self.store, "read_provider_target_record", side_effect=FileNotFoundError):
            self.assertTrue(any(w.code == "stale_route_authority" for w in topology().warnings))
        edge = next(
            e
            for e in self.store.list_edge_endpoint_records()
            if e.endpoint_key == current.ingress.endpoint_key
        )
        self.store.write_edge_endpoint_record(
            edge.model_copy(update={"updated_at": evaluated.isoformat()})
        )
        changed = topology()
        self.assertEqual(changed.provider_recorded.trust_state, "stale")
        self.assertTrue(any(w.code == "stale_route_authority" for w in changed.warnings))

    async def test_commit_fences_monitoring_but_allows_unrelated_profile_edits(self) -> None:
        payload = self.seed_split()
        route = "/v1/route-bindings/reconcile"
        original = self.store.reconcile_route_binding_record
        profile = self.store.read_product_profile_record(payload["product"])
        changed_lane = profile.lanes[0].model_copy(
            update={"health_url": "https://app.example.test/changed"}
        )
        for index, (changed, expected_status) in enumerate(
            (
                (profile.model_copy(update={"lanes": (changed_lane,)}), 409),
                (profile.model_copy(update={"display_name": "Changed after review"}), 202),
            )
        ):
            with self.subTest(expected_status=expected_status):
                self.store.write_product_profile_record(profile)
                review = (await self.post(route, payload)).json()["review"]

                def change_profile(*args: Any, **kwargs: Any) -> Any:
                    self.store.write_product_profile_record(changed)
                    return original(*args, **kwargs)

                with patch.object(
                    self.store, "reconcile_route_binding_record", side_effect=change_profile
                ):
                    response = await self.post(
                        route,
                        {
                            **payload,
                            "mode": "apply",
                            "reason": "Record both routes.",
                            "confirmation": review["expected_confirmation"],
                            "browser_review": {
                                "product": payload["product"],
                                "reviewed_plan_sha256": review["plan_sha256"],
                            },
                        },
                        f"split-profile-changed-{index}",
                    )
                self.assertEqual(response.status_code, expected_status, response.text)
                if expected_status == 409:
                    self.assertEqual(response.json()["error"]["code"], "stale")
                    with self.assertRaises(FileNotFoundError):
                        self.store.read_route_binding_record(
                            product=payload["product"],
                            context_name=payload["context"],
                            instance_name=payload["instance"],
                        )
