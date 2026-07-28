from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal
import unittest

from control_plane.contracts.data_provenance import FreshnessStatus
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.lane_summary import LaunchplaneLaneSummary
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneMonitoringIntent,
)
from control_plane.contracts.product_topology_read_model import (
    build_product_environment_topology,
)
from control_plane.contracts.public_ingress_monitoring import (
    PublicIngressFailureCode,
    PublicIngressIncidentRecord,
    PublicIngressIncidentReminderStateRecord,
    PublicIngressObservationRecord,
    PublicIngressObservationStatus,
    PublicIngressRouteBindingSourceKind,
    PublicIngressTargetObservation,
    PublicIngressTlsObservation,
    PublicIngressTlsProbeEvidence,
    PublicIngressTlsRecordedEvidence,
    PublicIngressTlsStatus,
    build_public_ingress_incident_event_id,
    build_public_ingress_material_fingerprint,
    build_public_ingress_tls_check_name,
    public_ingress_material_fingerprint_sha256,
)
from control_plane.contracts.route_binding_record import (
    EnvironmentRouteBindingRecord,
    RouteBindingTerminationKind,
    RouteBindingTlsOwner,
)
from control_plane.contracts.runtime_identity import RuntimeIdentity, RuntimeIdentityStatus


_NOW = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)


def _runtime_identity(
    *,
    product: str = "example-site",
    artifact_id: str = "artifact-1",
    image_reference: str = "",
) -> RuntimeIdentity:
    return RuntimeIdentity(
        product=product,
        context="example-context",
        instance="prod",
        deployment_record_id="deployment-1",
        artifact_id=artifact_id,
        source_git_ref="refs/heads/main",
        image_reference=image_reference,
    )


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


def _strict_public_profile(
    *,
    product: str = "example-site",
    domain_name: str = "example.test",
) -> LaunchplaneProductProfileRecord:
    payload = _profile(product=product, domain_name=domain_name).model_dump(mode="json")
    payload["lanes"][0]["health_monitoring"] = {
        "monitoring_intent": "public",
        "checks": [
            {
                "name": "public-ingress",
                "kind": "public_http",
                "require_runtime_identity": True,
            }
        ],
    }
    return LaunchplaneProductProfileRecord.model_validate(payload)


def _route_binding(
    *,
    product: str = "example-site",
    domain_name: str = "example.test",
    target_name: str = "example-site-prod",
    ingress_provider: str = "npmplus",
    tls_owner: RouteBindingTlsOwner = "launchplane",
    source_kind: str = "service",
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
                "provider_evidence": (
                    {} if ingress_provider == "external" else {"host_id": "edge-host-private-456"}
                ),
            },
            "domains": ({"domain_name": domain_name, "role": "primary"},),
            "tls": {
                "owner": tls_owner,
                "provider_evidence": (
                    {"certificate_id": "certificate-private-789"}
                    if tls_owner not in {"none", "external"}
                    else {}
                ),
            },
            "source": {
                "source_kind": source_kind,
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
    recorded_source_kind: PublicIngressRouteBindingSourceKind = "service",
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
                source_kind=recorded_source_kind,
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


def _http_observation(
    *,
    product: str = "example-site",
    domain_name: str = "example.test",
    observed_at: str = "2026-07-14T11:30:00Z",
    runtime_identity_status: RuntimeIdentityStatus = "match",
    expected_runtime_identity: RuntimeIdentity | None = None,
    monitoring_intent: ProductLaneMonitoringIntent | Literal["legacy"] = "legacy",
) -> PublicIngressObservationRecord:
    expected_identity = expected_runtime_identity or _runtime_identity(product=product)
    observed_identity = expected_identity if runtime_identity_status == "match" else None
    status: PublicIngressObservationStatus = (
        "pass" if runtime_identity_status in {"match", "unchecked"} else "fail"
    )
    failure_code: PublicIngressFailureCode | None = (
        None if status == "pass" else "wrong_runtime_identity"
    )
    target = PublicIngressTargetObservation(
        target="health_url",
        url=f"https://{domain_name}/healthz",
        status=status,
        failure_code=failure_code,
        http_status=200,
        final_url=f"https://{domain_name}/healthz",
        runtime_identity_status=runtime_identity_status,
        runtime_identity_detail=(
            "Runtime identity matched expected deployment."
            if runtime_identity_status == "match"
            else ""
            if runtime_identity_status == "unchecked"
            else "Expected identity unavailable."
        ),
        observed_runtime_identity=observed_identity,
        summary="Public health route verified." if status == "pass" else "Runtime unverified.",
    )
    return PublicIngressObservationRecord(
        record_id=f"http-observation-{product}-{runtime_identity_status}",
        product=product,
        repository=f"example/{product}",
        driver_id="generic-web",
        context="example-context",
        instance="prod",
        check_name="public-ingress",
        check_kind="public_http",
        monitoring_intent=monitoring_intent,
        observed_at=observed_at,
        status=status,
        failure_code=failure_code,
        base_url=f"https://{domain_name}",
        health_url=f"https://{domain_name}/healthz",
        expected_runtime_identity=expected_identity,
        targets=(target,),
        summary=target.summary,
    )


def _lane_summary(
    *,
    identity: RuntimeIdentity | None = None,
    freshness_status: FreshnessStatus = "stale",
    runtime_identity_status: RuntimeIdentityStatus = "match",
    health_verified: bool = True,
    health_status: str = "pass",
) -> LaunchplaneLaneSummary:
    current_identity = identity or _runtime_identity()
    observed_identity = current_identity if runtime_identity_status == "match" else None
    return LaunchplaneLaneSummary.model_validate(
        {
            "context": "example-context",
            "instance": "prod",
            "inventory": {
                "context": "example-context",
                "instance": "prod",
                "artifact_identity": {"artifact_id": current_identity.artifact_id},
                "source_git_ref": current_identity.source_git_ref,
                "deploy": {
                    "target_name": "example-site-prod",
                    "target_type": "application",
                    "deploy_mode": "test",
                    "deployment_id": current_identity.deployment_record_id,
                    "status": "pass",
                    "started_at": "2026-07-14T09:55:00Z",
                    "finished_at": "2026-07-14T10:00:00Z",
                },
                "runtime_identity": current_identity.model_dump(mode="json"),
                "destination_health": {
                    "verified": health_verified,
                    "urls": ("https://example.test/healthz",),
                    "timeout_seconds": 30,
                    "status": health_status,
                    "runtime_identity_status": runtime_identity_status,
                    "runtime_identity_detail": "Recorded deployment runtime identity evidence.",
                    "observed_runtime_identity": (
                        observed_identity.model_dump(mode="json")
                        if observed_identity is not None
                        else None
                    ),
                },
                "updated_at": "2026-07-14T10:00:00Z",
                "deployment_record_id": current_identity.deployment_record_id,
            },
            "provider_target": _provider_target().model_dump(mode="json"),
            "provenance": {
                "source_kind": "record",
                "source_record_id": "environment-inventory-prod",
                "recorded_at": "2026-07-14T10:00:00Z",
                "refreshed_at": "2026-07-14T10:00:00Z",
                "freshness_status": freshness_status,
                "stale_after": (
                    "2026-07-14T10:30:00Z"
                    if freshness_status == "stale"
                    else "2099-01-01T00:00:00Z"
                ),
                "detail": "Launchplane environment inventory evidence.",
            },
        }
    )


def _tls_incident(
    *, product: str = "example-site", domain_name: str = "example.test"
) -> PublicIngressIncidentRecord:
    observation = _tls_observation(product=product, domain_name=domain_name)
    fingerprint = build_public_ingress_material_fingerprint(observation)
    fingerprint_sha256 = public_ingress_material_fingerprint_sha256(fingerprint)
    incident_id = f"tls-incident-{product}"
    event_id = build_public_ingress_incident_event_id(
        incident_id=incident_id,
        event="opened",
        material_fingerprint_sha256=fingerprint_sha256,
    )
    return PublicIngressIncidentRecord(
        schema_version=2,
        incident_id=incident_id,
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
        state_version=1,
        severity="critical",
        material_fingerprint=fingerprint,
        material_fingerprint_sha256=fingerprint_sha256,
        material_fingerprint_complete=True,
        latest_material_event_id=event_id,
        latest_material_event="opened",
        latest_material_event_at=observation.observed_at,
        notification_state="acknowledged",
        notification_state_changed_at="2026-07-14T11:01:00Z",
        notification_state_reason="operator_acknowledged",
        summary="TLS hostname mismatch is active.",
    )


class _TopologyStore:
    def __init__(
        self,
        *,
        route_binding: EnvironmentRouteBindingRecord | None,
        observations: tuple[PublicIngressObservationRecord, ...] = (),
        incidents: tuple[PublicIngressIncidentRecord, ...] = (),
        reminders: tuple[PublicIngressIncidentReminderStateRecord, ...] = (),
    ) -> None:
        self.route_binding = route_binding
        self.observations = observations
        self.incidents = incidents
        self.reminders = reminders

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

    def list_public_ingress_incident_reminder_state_records(
        self,
        *,
        incident_id: str = "",
        policy_id: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[PublicIngressIncidentReminderStateRecord, ...]:
        records = [
            record
            for record in self.reminders
            if (not incident_id or record.incident_id == incident_id)
            and (not policy_id or record.policy_id == policy_id)
            and (not status or record.status == status)
        ]
        records.sort(key=lambda record: (record.updated_at, record.reminder_state_id), reverse=True)
        return tuple(records if limit is None else records[:limit])


class ProductTopologyReadModelTests(unittest.TestCase):
    def test_private_monitoring_intent_suppresses_public_and_tls_observation_warnings(
        self,
    ) -> None:
        profile_payload = _profile().model_dump(mode="json")
        profile_payload["lanes"][0]["health_monitoring"] = {
            "monitoring_intent": "private",
            "checks": [
                {"name": "public-ingress", "kind": "public_http"},
                {
                    "name": "private-runtime",
                    "kind": "private_http",
                    "private_endpoint_key": "example-site-prod-runtime",
                },
            ],
        }
        profile = LaunchplaneProductProfileRecord.model_validate(profile_payload)
        failed_public_observation = _http_observation(runtime_identity_status="unverifiable")

        topology = build_product_environment_topology(
            record_store=_TopologyStore(
                route_binding=_route_binding(
                    ingress_provider="external",
                    tls_owner="external",
                    source_kind="operator",
                ),
                observations=(failed_public_observation,),
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

        warning_codes = {warning.code for warning in topology.warnings}
        self.assertEqual(topology.observed.ingress.monitoring_intent, "private")
        self.assertFalse(topology.observed.ingress.incident_eligible)
        self.assertEqual(topology.observed.ingress.status, "not_expected")
        self.assertNotIn("public_ingress_failure", warning_codes)
        self.assertNotIn("public_runtime_identity_unverified", warning_codes)
        self.assertNotIn("tls_observation_missing", warning_codes)

    def test_external_ingress_projects_fresh_public_runtime_proof(self) -> None:
        profile = _profile()
        route_binding = _route_binding(
            ingress_provider="external",
            tls_owner="external",
            source_kind="operator",
        )
        topology = build_product_environment_topology(
            record_store=_TopologyStore(
                route_binding=route_binding,
                observations=(
                    _http_observation(runtime_identity_status="unchecked"),
                    _http_observation(),
                    _tls_observation(
                        status="valid",
                        recorded_owner="external",
                        recorded_ingress_provider="external",
                        recorded_source_kind="operator",
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

        self.assertEqual(topology.provider_recorded.ingress.provider, "external")
        self.assertEqual(topology.provider_recorded.ingress.path, "edge_to_provider")
        self.assertEqual(topology.provider_recorded.tls.terminator, "external")
        self.assertEqual(topology.observed.ingress.trust_state, "verified")
        self.assertEqual(topology.observed.ingress.runtime_identity_status, "match")
        self.assertEqual(
            topology.observed.ingress.observed_runtime_identity,
            topology.observed.ingress.expected_runtime_identity,
        )
        warning_codes = {warning.code for warning in topology.warnings}
        self.assertIn("external_ingress_internals_unsupported", warning_codes)
        external_visibility_warning = next(
            warning
            for warning in topology.warnings
            if warning.code == "external_ingress_internals_unsupported"
        )
        self.assertEqual(external_visibility_warning.severity, "warning")
        self.assertNotIn("public_ingress_observation_missing", warning_codes)
        self.assertNotIn("stale_public_ingress_observation", warning_codes)
        self.assertNotIn("public_runtime_identity_unverified", warning_codes)

    def test_external_ingress_fails_closed_for_missing_stale_or_unverified_public_proof(
        self,
    ) -> None:
        profile = _profile()
        route_binding = _route_binding(
            ingress_provider="external",
            tls_owner="external",
            source_kind="operator",
        )
        missing = build_product_environment_topology(
            record_store=_TopologyStore(route_binding=route_binding),
            profile=profile,
            lane=profile.lanes[0],
            lane_summary=LaunchplaneLaneSummary(
                context="example-context",
                instance="prod",
                provider_target=_provider_target(),
            ),
            now=_NOW,
        )
        stale = build_product_environment_topology(
            record_store=_TopologyStore(
                route_binding=route_binding,
                observations=(_http_observation(observed_at="2026-07-14T08:00:00Z"),),
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
        unverified = build_product_environment_topology(
            record_store=_TopologyStore(
                route_binding=route_binding,
                observations=(_http_observation(runtime_identity_status="unverifiable"),),
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

        self.assertIn(
            "public_ingress_observation_missing",
            {warning.code for warning in missing.warnings},
        )
        missing_severity = {warning.code: warning.severity for warning in missing.warnings}
        self.assertEqual(missing_severity["tls_observation_missing"], "error")
        self.assertEqual(stale.observed.ingress.trust_state, "stale")
        self.assertIn(
            "stale_public_ingress_observation",
            {warning.code for warning in stale.warnings},
        )
        self.assertIn(
            "public_runtime_identity_unverified",
            {warning.code for warning in unverified.warnings},
        )

    def test_fresh_strict_public_runtime_proof_corroborates_stale_placement(self) -> None:
        profile = _strict_public_profile()
        identity = _runtime_identity()
        lane_summary = _lane_summary(identity=identity)
        topology = build_product_environment_topology(
            record_store=_TopologyStore(
                route_binding=_route_binding(),
                observations=(
                    _http_observation(
                        expected_runtime_identity=identity,
                        monitoring_intent="public",
                    ),
                    _tls_observation(status="valid"),
                ),
            ),
            profile=profile,
            lane=profile.lanes[0],
            lane_summary=lane_summary,
            now=_NOW,
        )

        self.assertEqual(lane_summary.provenance.freshness_status, "stale")
        self.assertEqual(topology.observed.placement.trust_state, "verified")
        self.assertEqual(topology.observed.placement.runtime_identity_status, "match")
        self.assertEqual(
            topology.observed.placement.expected_runtime_identity,
            topology.observed.placement.observed_runtime_identity,
        )
        self.assertEqual(topology.observed.placement.provenance.source_kind, "provider")
        self.assertEqual(
            topology.observed.placement.provenance.source_record_id,
            "http-observation-example-site-match",
        )
        self.assertEqual(topology.observed.trust_state, "verified")
        self.assertEqual(topology.trust_state, "recorded")

    def test_stale_public_runtime_proof_does_not_corroborate_placement(self) -> None:
        profile = _strict_public_profile()
        topology = build_product_environment_topology(
            record_store=_TopologyStore(
                route_binding=_route_binding(),
                observations=(
                    _http_observation(
                        observed_at="2026-07-14T09:30:00Z",
                        monitoring_intent="public",
                    ),
                    _tls_observation(status="valid"),
                ),
            ),
            profile=profile,
            lane=profile.lanes[0],
            lane_summary=_lane_summary(),
            now=_NOW,
        )

        self.assertEqual(topology.observed.ingress.trust_state, "stale")
        self.assertEqual(topology.observed.placement.trust_state, "stale")
        self.assertEqual(topology.observed.placement.provenance.source_kind, "record")

    def test_runtime_proof_does_not_replace_missing_or_unsupported_placement_trust(
        self,
    ) -> None:
        profile = _strict_public_profile()
        for freshness_status in ("missing", "unsupported"):
            with self.subTest(freshness_status=freshness_status):
                topology = build_product_environment_topology(
                    record_store=_TopologyStore(
                        route_binding=_route_binding(),
                        observations=(
                            _http_observation(monitoring_intent="public"),
                            _tls_observation(status="valid"),
                        ),
                    ),
                    profile=profile,
                    lane=profile.lanes[0],
                    lane_summary=_lane_summary(freshness_status=freshness_status),
                    now=_NOW,
                )

                self.assertEqual(
                    topology.observed.placement.trust_state,
                    freshness_status,
                )
                self.assertEqual(topology.observed.placement.provenance.source_kind, "record")

    def test_non_strict_public_runtime_proof_does_not_corroborate_placement(self) -> None:
        profile_payload = _strict_public_profile().model_dump(mode="json")
        profile_payload["lanes"][0]["health_monitoring"]["checks"][0][
            "require_runtime_identity"
        ] = False
        profile = LaunchplaneProductProfileRecord.model_validate(profile_payload)
        topology = build_product_environment_topology(
            record_store=_TopologyStore(
                route_binding=_route_binding(),
                observations=(
                    _http_observation(monitoring_intent="public"),
                    _tls_observation(status="valid"),
                ),
            ),
            profile=profile,
            lane=profile.lanes[0],
            lane_summary=_lane_summary(),
            now=_NOW,
        )

        self.assertEqual(topology.observed.ingress.runtime_identity_status, "match")
        self.assertEqual(topology.observed.placement.trust_state, "stale")

    def test_different_public_runtime_identity_does_not_corroborate_placement(self) -> None:
        profile = _strict_public_profile()
        topology = build_product_environment_topology(
            record_store=_TopologyStore(
                route_binding=_route_binding(),
                observations=(
                    _http_observation(
                        expected_runtime_identity=_runtime_identity(artifact_id="artifact-2"),
                        monitoring_intent="public",
                    ),
                    _tls_observation(status="valid"),
                ),
            ),
            profile=profile,
            lane=profile.lanes[0],
            lane_summary=_lane_summary(),
            now=_NOW,
        )

        self.assertEqual(topology.observed.ingress.runtime_identity_status, "match")
        self.assertEqual(topology.observed.placement.trust_state, "stale")

    def test_optional_runtime_identity_divergence_does_not_corroborate_placement(self) -> None:
        profile = _strict_public_profile()
        topology = build_product_environment_topology(
            record_store=_TopologyStore(
                route_binding=_route_binding(),
                observations=(
                    _http_observation(monitoring_intent="public"),
                    _tls_observation(status="valid"),
                ),
            ),
            profile=profile,
            lane=profile.lanes[0],
            lane_summary=_lane_summary(
                identity=_runtime_identity(
                    image_reference="ghcr.io/example/example-site@sha256:abc123"
                )
            ),
            now=_NOW,
        )

        self.assertEqual(topology.observed.ingress.runtime_identity_status, "match")
        self.assertEqual(topology.observed.placement.trust_state, "stale")

    def test_prelaunch_or_legacy_runtime_proof_does_not_corroborate_placement(self) -> None:
        strict_profile = _strict_public_profile()
        prelaunch_payload = strict_profile.model_dump(mode="json")
        prelaunch_payload["lanes"][0]["health_monitoring"]["monitoring_intent"] = "prelaunch"
        cases = (
            (
                "prelaunch_lane",
                LaunchplaneProductProfileRecord.model_validate(prelaunch_payload),
                _http_observation(monitoring_intent="public"),
            ),
            ("legacy_observation", strict_profile, _http_observation()),
        )
        for label, profile, observation in cases:
            with self.subTest(case=label):
                topology = build_product_environment_topology(
                    record_store=_TopologyStore(
                        route_binding=_route_binding(),
                        observations=(observation, _tls_observation(status="valid")),
                    ),
                    profile=profile,
                    lane=profile.lanes[0],
                    lane_summary=_lane_summary(),
                    now=_NOW,
                )

                self.assertEqual(topology.observed.placement.trust_state, "stale")

    def test_failing_public_runtime_proof_does_not_corroborate_placement(self) -> None:
        profile = _strict_public_profile()
        topology = build_product_environment_topology(
            record_store=_TopologyStore(
                route_binding=_route_binding(),
                observations=(
                    _http_observation(
                        runtime_identity_status="unverifiable",
                        monitoring_intent="public",
                    ),
                    _tls_observation(status="valid"),
                ),
            ),
            profile=profile,
            lane=profile.lanes[0],
            lane_summary=_lane_summary(),
            now=_NOW,
        )

        self.assertEqual(topology.observed.ingress.status, "fail")
        self.assertEqual(topology.observed.placement.trust_state, "stale")

    def test_failed_recorded_health_does_not_corroborate_placement(self) -> None:
        profile = _strict_public_profile()
        topology = build_product_environment_topology(
            record_store=_TopologyStore(
                route_binding=_route_binding(),
                observations=(
                    _http_observation(monitoring_intent="public"),
                    _tls_observation(status="valid"),
                ),
            ),
            profile=profile,
            lane=profile.lanes[0],
            lane_summary=_lane_summary(health_status="fail"),
            now=_NOW,
        )

        self.assertEqual(topology.observed.ingress.status, "pass")
        self.assertEqual(topology.observed.placement.runtime_identity_status, "match")
        self.assertEqual(topology.observed.placement.trust_state, "stale")

    def test_newer_equivalent_public_evidence_fences_older_strict_proof(self) -> None:
        profile = _strict_public_profile()
        topology = build_product_environment_topology(
            record_store=_TopologyStore(
                route_binding=_route_binding(),
                observations=(
                    _http_observation(
                        observed_at="2026-07-14T11:30:00Z",
                        monitoring_intent="public",
                    ),
                    _http_observation(
                        observed_at="2026-07-14T11:45:00Z",
                        runtime_identity_status="unchecked",
                        monitoring_intent="public",
                    ),
                    _tls_observation(status="valid"),
                ),
            ),
            profile=profile,
            lane=profile.lanes[0],
            lane_summary=_lane_summary(),
            now=_NOW,
        )

        self.assertEqual(topology.observed.ingress.runtime_identity_status, "match")
        self.assertEqual(topology.observed.placement.trust_state, "stale")

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
        incident = _tls_incident(
            product=profile.product,
            domain_name="cm-website.example.test",
        )
        reminder = PublicIngressIncidentReminderStateRecord(
            reminder_state_id="tls-reminder-cm-website",
            incident_id=incident.incident_id,
            policy_id="example-policy",
            status="suppressed",
            material_event_id=incident.latest_material_event_id,
            interval_seconds=21600,
            window_anchor_at=incident.latest_material_event_at,
            next_reminder_at="2026-07-14T18:00:00Z",
            updated_at="2026-07-14T12:00:00Z",
        )
        topology = build_product_environment_topology(
            record_store=_TopologyStore(
                route_binding=route_binding,
                observations=(observation,),
                incidents=(incident,),
                reminders=(reminder,),
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
        self.assertEqual(observed_tls.incident_severity, "critical")
        self.assertEqual(observed_tls.incident_notification_state, "acknowledged")
        self.assertEqual(observed_tls.incident_latest_event, "opened")
        self.assertEqual(observed_tls.incident_next_reminder_at, "")
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
