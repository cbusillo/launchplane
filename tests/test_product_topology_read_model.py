from __future__ import annotations

from datetime import datetime, timezone
import unittest

from control_plane.contracts.data_provenance import FreshnessStatus
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.lane_summary import LaunchplaneLaneSummary
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.product_topology_read_model import (
    build_product_environment_topology,
)
from control_plane.contracts.public_ingress_monitoring import (
    PublicIngressFailureCode,
    PublicIngressIncidentRecord,
    PublicIngressObservationRecord,
    PublicIngressObservationStatus,
    PublicIngressTargetObservation,
    PublicIngressTlsObservation,
    PublicIngressTlsProbeEvidence,
    PublicIngressTlsRecordedEvidence,
    PublicIngressTlsStatus,
    build_public_ingress_tls_check_name,
)
from control_plane.contracts.route_binding_record import (
    EnvironmentRouteBindingRecord,
    RouteBindingTerminationKind,
    RouteBindingTlsOwner,
)


_NOW = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)


def _profile(
    *,
    product: str = "example-site",
    driver_id: str = "generic-web",
    domain_name: str = "example.test",
) -> LaunchplaneProductProfileRecord:
    return LaunchplaneProductProfileRecord.model_validate(
        {
            "product": product,
            "display_name": "Example Site",
            "repository": f"example/{product}",
            "driver_id": driver_id,
            "image": {"repository": f"ghcr.io/example/{product}"},
            "runtime_port": 3000,
            "health_path": "/healthz",
            "lanes": (
                {
                    "instance": "prod",
                    "context": "example-context",
                    "base_url": f"https://{domain_name}",
                    "health_url": f"https://{domain_name}/healthz",
                },
            ),
            "updated_at": "2026-07-14T10:00:00Z",
            "source": "test",
        }
    )


def _route_binding(
    *,
    product: str = "example-site",
    domain_name: str = "example.test",
    target_name: str = "example-site-prod",
    ingress_provider: str = "npmplus",
    tls_owner: RouteBindingTlsOwner = "launchplane",
    freshness_status: str = "recorded",
    stale_after: str = "2099-01-01T00:00:00Z",
) -> EnvironmentRouteBindingRecord:
    return EnvironmentRouteBindingRecord.model_validate(
        {
            "product": product,
            "context": "example-context",
            "instance": "prod",
            "provider_target": {
                "provider_id": "dokploy",
                "target_category": "compose",
                "provider_target_type": "compose",
                "target_name": target_name,
                "provider_evidence": {
                    "host_id": "provider-host-private-123",
                    "project_name": "provider-project-private",
                },
            },
            "ingress": {
                "provider": ingress_provider,
                "endpoint_key": "public-edge",
                "termination_kind": "edge",
                "provider_evidence": {"host_id": "edge-host-private-456"},
            },
            "domains": ({"domain_name": domain_name, "role": "primary"},),
            "tls": {
                "owner": tls_owner,
                "provider_evidence": (
                    {"certificate_id": "certificate-private-789"} if tls_owner != "none" else {}
                ),
            },
            "source": {
                "source_kind": "service",
                "source_label": "provider reconciliation",
                "source_record_ids": ("provider-target-record", "ingress-audit-record"),
                "refreshed_at": "2026-07-14T10:30:00Z",
                "freshness_status": freshness_status,
                "stale_after": stale_after,
            },
            "updated_at": "2026-07-14T10:30:00Z",
        }
    )


def _provider_target(
    *, target_name: str = "example-site-prod", provider_id: str = "dokploy"
) -> ProviderTargetRecord:
    return ProviderTargetRecord(
        context="example-context",
        instance="prod",
        provider_id=provider_id,
        target_category="compose",
        target_id="provider-target-private-123",
        display_name=target_name,
        provider_target_type="compose",
        provider_evidence={"server_host_id": "provider-server-private-456"},
        updated_at="2026-07-14T10:25:00Z",
        source_label="test",
    )


def _tls_observation(
    *,
    product: str = "example-site",
    domain_name: str = "example.test",
    status: PublicIngressTlsStatus = "hostname_mismatch",
    recorded_owner: RouteBindingTlsOwner = "launchplane",
    recorded_ingress_provider: str = "npmplus",
    recorded_termination_kind: RouteBindingTerminationKind = "edge",
    probe_stale_after: str = "2099-01-01T00:00:00Z",
) -> PublicIngressObservationRecord:
    failure_codes: dict[PublicIngressTlsStatus, PublicIngressFailureCode] = {
        "hostname_mismatch": "tls_hostname_mismatch",
        "expired": "tls_expired",
        "expiring": "tls_expiring",
        "untrusted": "tls_chain_failure",
        "self_signed": "tls_self_signed",
        "unreachable": "connection_timeout",
        "unknown": "tls_failure",
        "unsupported": "tls_unsupported",
    }
    failure_code = failure_codes.get(status)
    observation_status: PublicIngressObservationStatus = (
        "pass" if status == "valid" else "skipped" if status == "unsupported" else "fail"
    )
    certificate_state = status in {
        "valid",
        "expiring",
        "expired",
        "hostname_mismatch",
        "untrusted",
        "self_signed",
    }
    tls = PublicIngressTlsObservation.model_validate(
        {
            "status": status,
            "public_name": domain_name,
            "issuer": "Example Public CA" if certificate_state else "",
            "subject": "CN=wrong.example.test" if certificate_state else "",
            "not_before": "2026-01-01T00:00:00Z" if certificate_state else "",
            "not_after": "2027-01-01T00:00:00Z" if certificate_state else "",
            "days_remaining": 171 if certificate_state else None,
            "public_name_match": status in {"valid", "expiring", "expired"},
            "public_name_match_source": (
                "san" if status in {"valid", "expiring", "expired"} else "none"
            ),
            "presented_san_count": 1 if certificate_state else 0,
            "presented_name_evidence": ("wrong.example.test",) if certificate_state else (),
            "recorded": PublicIngressTlsRecordedEvidence(
                domain_name=domain_name,
                domain_role="primary",
                owner=recorded_owner,
                ingress_provider=recorded_ingress_provider,
                termination_kind=recorded_termination_kind,
                source_kind="service",
                source_label="provider reconciliation",
                source_record_ids=("provider-target-record",),
                refreshed_at="2026-07-14T10:30:00Z",
                recorded_at="2026-07-14T10:30:00Z",
                freshness_status="recorded",
                stale_after="2099-01-01T00:00:00Z",
                provider_evidence={"certificate_id": "certificate-private-789"},
            ),
            "probe": PublicIngressTlsProbeEvidence(
                observed_at="2026-07-14T11:00:00Z",
                validated_address_count=2,
                sni_hostname=domain_name,
                freshness_status="verified" if status == "valid" else "recorded",
                stale_after=probe_stale_after,
            ),
        }
    )
    target = PublicIngressTargetObservation(
        target="tls_domain",
        url=f"https://{domain_name}/",
        status=observation_status,
        failure_code=failure_code,
        tls=tls,
        summary=f"TLS status for {domain_name}: {status}",
    )
    return PublicIngressObservationRecord(
        record_id=f"tls-observation-{product}-{status}",
        product=product,
        repository=f"example/{product}",
        driver_id="generic-web",
        context="example-context",
        instance="prod",
        check_name=build_public_ingress_tls_check_name(domain_name),
        check_kind="tls",
        observed_at="2026-07-14T11:00:00Z",
        status=observation_status,
        failure_code=failure_code,
        targets=(target,),
        summary=f"TLS status for {domain_name}: {status}",
    )


def _tls_incident(
    *, product: str = "example-site", domain_name: str = "example.test"
) -> PublicIngressIncidentRecord:
    observation = _tls_observation(product=product, domain_name=domain_name)
    return PublicIngressIncidentRecord(
        incident_id=f"tls-incident-{product}",
        product=product,
        repository=f"example/{product}",
        driver_id="generic-web",
        context="example-context",
        instance="prod",
        check_name=observation.check_name,
        check_kind="tls",
        status="open",
        opened_at="2026-07-14T11:00:00Z",
        opened_observation_id=observation.record_id,
        latest_observation_id=observation.record_id,
        latest_observed_at=observation.observed_at,
        failure_code="tls_hostname_mismatch",
        summary="TLS hostname mismatch is active.",
    )


class _TopologyStore:
    def __init__(
        self,
        *,
        route_binding: EnvironmentRouteBindingRecord | None,
        observations: tuple[PublicIngressObservationRecord, ...] = (),
        incidents: tuple[PublicIngressIncidentRecord, ...] = (),
    ) -> None:
        self.route_binding = route_binding
        self.observations = observations
        self.incidents = incidents

    def read_route_binding_record(
        self, *, product: str, context_name: str, instance_name: str
    ) -> EnvironmentRouteBindingRecord:
        record = self.route_binding
        if (
            record is None
            or record.product != product
            or record.context != context_name
            or record.instance != instance_name
        ):
            raise FileNotFoundError(f"{product}/{context_name}/{instance_name}")
        return record

    def list_public_ingress_observation_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        instance_name: str = "",
        check_name: str = "",
        check_kind: str = "",
        limit: int | None = None,
    ) -> tuple[PublicIngressObservationRecord, ...]:
        records = [
            record
            for record in self.observations
            if (not product or record.product == product)
            and (not context_name or record.context == context_name)
            and (not instance_name or record.instance == instance_name)
            and (not check_name or record.check_name == check_name)
            and (not check_kind or record.check_kind == check_kind)
        ]
        records.sort(key=lambda record: (record.observed_at, record.record_id), reverse=True)
        return tuple(records if limit is None else records[:limit])

    def list_public_ingress_incident_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        instance_name: str = "",
        check_name: str = "",
        check_kind: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[PublicIngressIncidentRecord, ...]:
        records = [
            record
            for record in self.incidents
            if (not product or record.product == product)
            and (not context_name or record.context == context_name)
            and (not instance_name or record.instance == instance_name)
            and (not check_name or record.check_name == check_name)
            and (not check_kind or record.check_kind == check_kind)
            and (not status or record.status == status)
        ]
        records.sort(key=lambda record: (record.opened_at, record.incident_id), reverse=True)
        return tuple(records if limit is None else records[:limit])


class ProductTopologyReadModelTests(unittest.TestCase):
    def test_projection_diagnoses_cm_website_tls_hostname_mismatch(self) -> None:
        profile = _profile(
            product="odoo-tenant-cm-website",
            driver_id="odoo",
            domain_name="cm-website.example.test",
        )
        route_binding = _route_binding(
            product=profile.product,
            domain_name="cm-website.example.test",
            target_name="cm-website-prod",
        )
        observation = _tls_observation(
            product=profile.product,
            domain_name="cm-website.example.test",
        )
        topology = build_product_environment_topology(
            record_store=_TopologyStore(
                route_binding=route_binding,
                observations=(observation,),
                incidents=(
                    _tls_incident(
                        product=profile.product,
                        domain_name="cm-website.example.test",
                    ),
                ),
            ),
            profile=profile,
            lane=profile.lanes[0],
            lane_summary=LaunchplaneLaneSummary(
                context="example-context",
                instance="prod",
                provider_target=_provider_target(target_name="cm-website-prod"),
            ),
            now=_NOW,
        )

        self.assertEqual(topology.desired.trust_state, "recorded")
        self.assertEqual(topology.provider_recorded.trust_state, "recorded")
        self.assertEqual(topology.observed.trust_state, "verified")
        self.assertEqual(topology.provider_recorded.placement.target_name, "cm-website-prod")
        self.assertEqual(topology.provider_recorded.ingress.path, "edge_to_provider")
        self.assertEqual(topology.provider_recorded.tls.owner, "launchplane")
        observed_tls = topology.observed.tls_domains[0]
        self.assertEqual(observed_tls.status, "hostname_mismatch")
        self.assertEqual(observed_tls.failure_code, "tls_hostname_mismatch")
        self.assertEqual(observed_tls.presented_name_evidence, ("wrong.example.test",))
        self.assertEqual(observed_tls.incident_status, "open")
        self.assertIn("certificate binding", observed_tls.likely_failure_cause)
        self.assertIn("tls_mismatch", {warning.code for warning in topology.warnings})
        serialized = topology.model_dump_json()
        self.assertNotIn("provider-target-private-123", serialized)
        self.assertNotIn("provider-host-private-123", serialized)
        self.assertNotIn("edge-host-private-456", serialized)
        self.assertNotIn("certificate-private-789", serialized)

    def test_missing_route_authority_does_not_synthesize_provider_topology(self) -> None:
        profile = _profile()
        topology = build_product_environment_topology(
            record_store=_TopologyStore(route_binding=None),
            profile=profile,
            lane=profile.lanes[0],
            lane_summary=LaunchplaneLaneSummary(
                context="example-context",
                instance="prod",
                provider_target=_provider_target(),
            ),
            now=_NOW,
        )

        self.assertEqual(topology.provider_recorded.authority_status, "missing")
        self.assertEqual(topology.provider_recorded.placement.provider, "")
        self.assertEqual(topology.provider_recorded.placement.target_name, "")
        self.assertEqual(topology.provider_recorded.trust_state, "missing")
        warning_codes = {warning.code for warning in topology.warnings}
        self.assertIn("missing_route_authority", warning_codes)
        self.assertIn("ingress_ownership_unknown", warning_codes)
        self.assertIn("tls_ownership_unknown", warning_codes)

    def test_projection_warns_for_divergence_staleness_and_unknown_ownership(self) -> None:
        profile = _profile(domain_name="desired.example.test")
        route_binding = _route_binding(
            domain_name="recorded.example.test",
            target_name="recorded-target",
            tls_owner="none",
            stale_after="2026-07-14T11:00:00Z",
        )
        observation = _tls_observation(
            domain_name="recorded.example.test",
            status="valid",
            recorded_owner="provider",
            recorded_ingress_provider="other-edge",
            probe_stale_after="2026-07-14T11:30:00Z",
        )
        topology = build_product_environment_topology(
            record_store=_TopologyStore(
                route_binding=route_binding,
                observations=(observation,),
            ),
            profile=profile,
            lane=profile.lanes[0],
            lane_summary=LaunchplaneLaneSummary(
                context="example-context",
                instance="prod",
                provider_target=_provider_target(target_name="different-target"),
            ),
            now=_NOW,
        )

        self.assertEqual(topology.provider_recorded.trust_state, "stale")
        self.assertEqual(topology.observed.tls_domains[0].trust_state, "stale")
        warning_codes = {warning.code for warning in topology.warnings}
        self.assertTrue(
            {
                "domain_divergence",
                "placement_divergence",
                "stale_route_authority",
                "tls_ownership_unknown",
                "ingress_divergence",
                "tls_ownership_divergence",
                "stale_tls_observation",
                "tls_observation_missing",
            }.issubset(warning_codes)
        )

    def test_observed_tls_trust_states_cover_verified_unsupported_stale_and_missing(self) -> None:
        cases: tuple[tuple[PublicIngressTlsStatus, str, FreshnessStatus], ...] = (
            ("valid", "2099-01-01T00:00:00Z", "verified"),
            ("unsupported", "2099-01-01T00:00:00Z", "unsupported"),
            ("valid", "2026-07-14T11:30:00Z", "stale"),
        )
        for status, stale_after, expected_trust_state in cases:
            with self.subTest(status=status, stale_after=stale_after):
                profile = _profile()
                topology = build_product_environment_topology(
                    record_store=_TopologyStore(
                        route_binding=_route_binding(),
                        observations=(
                            _tls_observation(
                                status=status,
                                probe_stale_after=stale_after,
                            ),
                        ),
                    ),
                    profile=profile,
                    lane=profile.lanes[0],
                    lane_summary=LaunchplaneLaneSummary(
                        context="example-context",
                        instance="prod",
                        provider_target=_provider_target(),
                    ),
                    now=_NOW,
                )
                self.assertEqual(
                    topology.observed.tls_domains[0].trust_state,
                    expected_trust_state,
                )

        profile = _profile()
        missing = build_product_environment_topology(
            record_store=_TopologyStore(route_binding=_route_binding()),
            profile=profile,
            lane=profile.lanes[0],
            lane_summary=LaunchplaneLaneSummary(
                context="example-context",
                instance="prod",
                provider_target=_provider_target(),
            ),
            now=_NOW,
        )
        self.assertEqual(missing.observed.trust_state, "missing")

    def test_projection_shell_is_shared_across_product_drivers(self) -> None:
        for driver_id in ("generic-web", "odoo", "verireel"):
            with self.subTest(driver_id=driver_id):
                product = f"example-{driver_id}"
                profile = _profile(product=product, driver_id=driver_id)
                topology = build_product_environment_topology(
                    record_store=_TopologyStore(
                        route_binding=_route_binding(product=product),
                    ),
                    profile=profile,
                    lane=profile.lanes[0],
                    lane_summary=LaunchplaneLaneSummary(
                        context="example-context",
                        instance="prod",
                        provider_target=_provider_target(),
                    ),
                    now=_NOW,
                )
                self.assertEqual(topology.provider_recorded.placement.provider, "dokploy")
                self.assertEqual(topology.provider_recorded.ingress.provider, "npmplus")
                self.assertEqual(topology.provider_recorded.tls.owner, "launchplane")


if __name__ == "__main__":
    unittest.main()
