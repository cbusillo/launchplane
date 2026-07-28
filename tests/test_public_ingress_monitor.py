from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from typing import Callable, Literal, cast
from unittest.mock import patch
from urllib.request import Request

from click.testing import CliRunner

from control_plane.cli import main as launchplane_cli
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.lane_summary import LaunchplaneLaneSummary
from control_plane.contracts.outbox_delivery import OutboxDeliveryRecord
from control_plane.contracts.product_health_monitoring_migration import (
    canonical_health_check_record_token,
)
from control_plane.contracts.private_health_endpoint_record import PrivateHealthEndpointRecord
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductImageProfile,
    ProductLaneProfile,
    ProductLaneHealthMonitoringPolicy,
    ProductLaneHealthCheck,
)
from control_plane.contracts.promotion_record import DeploymentEvidence
from control_plane.contracts.route_binding_record import EnvironmentRouteBindingRecord
from control_plane.contracts.route_binding_record import RouteBindingDomain
from control_plane.contracts.route_binding_record import RouteBindingIngress
from control_plane.contracts.route_binding_record import RouteBindingProviderTarget
from control_plane.contracts.route_binding_record import RouteBindingSource
from control_plane.contracts.route_binding_record import RouteBindingTls
from control_plane.contracts.public_ingress_monitoring import PublicIngressObservationRecord
from control_plane.contracts.public_ingress_monitoring import PublicIngressIncidentRecord
from control_plane.contracts.public_ingress_monitoring import PublicIngressIncidentEventRecord
from control_plane.contracts.public_ingress_monitoring import (
    PublicIngressIncidentReminderStateRecord,
)
from control_plane.contracts.public_ingress_monitoring import PublicIngressCheckKind
from control_plane.contracts.public_ingress_monitoring import PublicIngressHealthCheckKind
from control_plane.contracts.public_ingress_monitoring import (
    PublicIngressNotificationAttemptRecord,
)
from control_plane.contracts.public_ingress_monitoring import PublicIngressNotificationDestination
from control_plane.contracts.public_ingress_monitoring import PublicIngressNotificationPolicyRecord
from control_plane.contracts.public_ingress_monitoring import build_public_ingress_lane_incident_id
from control_plane.contracts.public_ingress_monitoring import build_public_ingress_observation_id
from control_plane.contracts.public_ingress_monitoring import (
    public_ingress_incident_record_sha256,
)
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.workflows.public_ingress_monitor import (
    HttpObservation,
    PublicIngressNotificationDriverSet,
    _deliver_github_issue_notification,
    _gh_issue_client,
    _incident_event,
    check_public_ingress_target,
    deliver_public_ingress_notification_outbox_delivery,
    discover_public_ingress_monitor_targets,
    reconcile_public_ingress_incident,
    run_public_ingress_monitor_once,
)
from control_plane.outbound_http import PublicHttpDestinationError
from control_plane.outbound_http import PublicTlsCertificate
from control_plane.outbound_http import PublicTlsProbeResult
from control_plane.outbox_worker import run_outbox_worker_once
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.stores import _sqlite_database_url


def _public_health_monitoring(
    *, require_runtime_identity: bool = False, recovery_observation_threshold: int = 1
) -> ProductLaneHealthMonitoringPolicy:
    return ProductLaneHealthMonitoringPolicy(
        monitoring_intent="public",
        checks=(
            ProductLaneHealthCheck(
                name="public-ingress",
                require_runtime_identity=require_runtime_identity,
                recovery_observation_threshold=recovery_observation_threshold,
            ),
        ),
    )


class _Store:
    def __init__(self, profiles: tuple[LaunchplaneProductProfileRecord, ...]) -> None:
        self._profiles = profiles
        self.records: list[PublicIngressObservationRecord] = []
        self.incidents: list[PublicIngressIncidentRecord] = []
        self.incident_events: list[PublicIngressIncidentEventRecord] = []
        self.reminder_states: list[PublicIngressIncidentReminderStateRecord] = []
        self.notification_policies: list[PublicIngressNotificationPolicyRecord] = []
        self.notification_attempts: list[PublicIngressNotificationAttemptRecord] = []
        self.lane_summaries: dict[tuple[str, str], LaunchplaneLaneSummary] = {}
        self.private_health_endpoints: dict[str, PrivateHealthEndpointRecord] = {}
        self.route_bindings: list[EnvironmentRouteBindingRecord] = []

    def list_product_profile_records(
        self, *, driver_id: str = ""
    ) -> tuple[LaunchplaneProductProfileRecord, ...]:
        if driver_id:
            return tuple(profile for profile in self._profiles if profile.driver_id == driver_id)
        return self._profiles

    def read_lane_summary(self, *, context_name: str, instance_name: str) -> LaunchplaneLaneSummary:
        return self.lane_summaries[(context_name, instance_name)]

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
            for record in self.records
            if (not product or record.product == product)
            and (not context_name or record.context == context_name)
            and (not instance_name or record.instance == instance_name)
            and (
                not check_name
                or canonical_health_check_record_token(record.check_name)
                == canonical_health_check_record_token(check_name)
            )
            and (not check_kind or record.check_kind == check_kind)
        ]
        records.sort(key=lambda record: (record.observed_at, record.record_id), reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def write_public_ingress_observation_record(
        self, record: PublicIngressObservationRecord
    ) -> None:
        self.records.append(record)

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
        incidents = [
            incident
            for incident in self.incidents
            if (not product or incident.product == product)
            and (not context_name or incident.context == context_name)
            and (not instance_name or incident.instance == instance_name)
            and (
                not check_name
                or canonical_health_check_record_token(incident.check_name)
                == canonical_health_check_record_token(check_name)
            )
            and (not check_kind or incident.check_kind == check_kind)
            and (not status or incident.status == status)
        ]
        incidents.sort(
            key=lambda incident: (incident.opened_at, incident.incident_id), reverse=True
        )
        if limit is not None:
            incidents = incidents[:limit]
        return tuple(incidents)

    def write_public_ingress_incident_record(self, record: PublicIngressIncidentRecord) -> None:
        self.incidents = [
            incident for incident in self.incidents if incident.incident_id != record.incident_id
        ]
        self.incidents.append(record)

    def list_public_ingress_incident_event_records(
        self,
        *,
        incident_id: str = "",
        event: str = "",
        limit: int | None = None,
    ) -> tuple[PublicIngressIncidentEventRecord, ...]:
        records = [
            record
            for record in self.incident_events
            if (not incident_id or record.incident_id == incident_id)
            and (not event or record.event == event)
        ]
        records.sort(key=lambda record: (record.occurred_at, record.event_id), reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def write_public_ingress_incident_event_record(
        self, record: PublicIngressIncidentEventRecord
    ) -> None:
        self.incident_events = [
            event for event in self.incident_events if event.event_id != record.event_id
        ]
        self.incident_events.append(record)

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
            for record in self.reminder_states
            if (not incident_id or record.incident_id == incident_id)
            and (not policy_id or record.policy_id == policy_id)
            and (not status or record.status == status)
        ]
        records.sort(key=lambda record: (record.updated_at, record.reminder_state_id), reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def write_public_ingress_incident_reminder_state_record(
        self, record: PublicIngressIncidentReminderStateRecord
    ) -> None:
        self.reminder_states = [
            state
            for state in self.reminder_states
            if state.reminder_state_id != record.reminder_state_id
        ]
        self.reminder_states.append(record)

    def list_public_ingress_notification_policy_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        instance_name: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[PublicIngressNotificationPolicyRecord, ...]:
        policies = [
            policy
            for policy in self.notification_policies
            if (not product or policy.product in {"", product})
            and (not context_name or policy.context in {"", context_name})
            and (not instance_name or policy.instance in {"", instance_name})
            and (not status or policy.status == status)
        ]
        policies.sort(key=lambda policy: (policy.updated_at, policy.policy_id), reverse=True)
        if limit is not None:
            policies = policies[:limit]
        return tuple(policies)

    def list_public_ingress_notification_attempt_records(
        self,
        *,
        incident_id: str = "",
        event: str = "",
        destination_kind: str = "",
        limit: int | None = None,
    ) -> tuple[PublicIngressNotificationAttemptRecord, ...]:
        attempts = [
            attempt
            for attempt in self.notification_attempts
            if (not incident_id or attempt.incident_id == incident_id)
            and (not event or attempt.event == event)
            and (not destination_kind or attempt.destination_kind == destination_kind)
        ]
        attempts.sort(key=lambda attempt: (attempt.attempted_at, attempt.attempt_id), reverse=True)
        if limit is not None:
            attempts = attempts[:limit]
        return tuple(attempts)

    def write_public_ingress_notification_attempt_record(
        self, record: PublicIngressNotificationAttemptRecord
    ) -> None:
        self.notification_attempts = [
            attempt
            for attempt in self.notification_attempts
            if attempt.attempt_id != record.attempt_id
        ]
        self.notification_attempts.append(record)

    def read_private_health_endpoint_record(self, endpoint_key: str) -> PrivateHealthEndpointRecord:
        try:
            return self.private_health_endpoints[endpoint_key]
        except KeyError as error:
            raise FileNotFoundError(endpoint_key) from error

    def list_route_binding_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        instance_name: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[EnvironmentRouteBindingRecord, ...]:
        records = [
            record
            for record in self.route_bindings
            if (not product or record.product == product)
            and (not context_name or record.context == context_name)
            and (not instance_name or record.instance == instance_name)
            and (not status or record.status == status)
        ]
        records.sort(key=lambda record: (record.updated_at, record.binding_key), reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)


def _profile(
    *, driver_id: str = "generic-web", lane: ProductLaneProfile | None = None
) -> LaunchplaneProductProfileRecord:
    return LaunchplaneProductProfileRecord(
        product="example-site",
        display_name="Example Site",
        repository="cbusillo/example-site",
        driver_id=driver_id,
        image=ProductImageProfile(repository="ghcr.io/cbusillo/example-site"),
        runtime_port=3000,
        health_path="/healthz",
        lanes=(
            lane
            or ProductLaneProfile(
                instance="prod",
                context="example-site",
                base_url="https://example.test",
                health_monitoring=_public_health_monitoring(),
            ),
        ),
        updated_at="2026-05-29T12:00:00Z",
        source="test",
    )


def _identity() -> RuntimeIdentity:
    return RuntimeIdentity(
        product="example-site",
        context="example-site",
        instance="prod",
        deployment_record_id="deploy-1",
        artifact_id="ghcr.io/cbusillo/example-site@sha256:abc123",
        source_git_ref="abc123",
    )


def _route_binding(
    *,
    product: str = "example-site",
    context: str = "example-site",
    instance: str = "prod",
    domains: tuple[tuple[str, Literal["primary", "alias"]], ...] = (("example.test", "primary"),),
    tls_owner: Literal["launchplane", "provider", "external", "none"] = "launchplane",
) -> EnvironmentRouteBindingRecord:
    return EnvironmentRouteBindingRecord(
        product=product,
        context=context,
        instance=instance,
        provider_target=RouteBindingProviderTarget(
            provider_id="dokploy",
            target_category="compose",
            provider_target_type="compose",
            target_name="example-site-prod",
            provider_evidence={"target_record": f"{context}:{instance}"},
        ),
        ingress=RouteBindingIngress(
            provider="npmplus",
            endpoint_key="example-edge",
            termination_kind="edge",
            provider_evidence={"audit_record": "audit-1"},
        ),
        domains=tuple(
            RouteBindingDomain(domain_name=domain_name, role=role) for domain_name, role in domains
        ),
        tls=RouteBindingTls(
            owner=tls_owner,
            provider_evidence={"audit_record": "audit-1", "provider_certificate_ref": "101"},
        ),
        source=RouteBindingSource(
            source_kind="service",
            source_label="test",
            source_record_ids=("route-binding:test",),
            refreshed_at="2026-07-14T12:00:00Z",
            freshness_status="recorded",
            stale_after="2026-07-14T14:00:00Z",
        ),
        updated_at="2026-07-14T12:00:00Z",
    )


def _tls_certificate(
    *,
    days_remaining: int = 45,
    public_name_match: bool = True,
    public_name_match_source: Literal["san", "subject", "none"] = "san",
    presented_name_evidence: tuple[str, ...] = ("example.test",),
) -> PublicTlsCertificate:
    return PublicTlsCertificate(
        issuer="CN=Example Issuer",
        subject="CN=example.test",
        not_before="2026-07-01T00:00:00Z",
        not_after="2026-08-30T00:00:00Z",
        days_remaining=days_remaining,
        public_name_match=public_name_match,
        public_name_match_source=public_name_match_source,
        presented_san_count=1,
        presented_name_evidence=presented_name_evidence,
    )


def _constant_tls_probe(
    probe_result: PublicTlsProbeResult,
) -> Callable[[str, int], PublicTlsProbeResult]:
    def tls_get(_domain: str, _timeout: int) -> PublicTlsProbeResult:
        return probe_result

    return tls_get


def _notification_policy(
    *destinations: PublicIngressNotificationDestination,
) -> PublicIngressNotificationPolicyRecord:
    return PublicIngressNotificationPolicyRecord(
        policy_id="public-ingress-notifications-example-site",
        product="example-site",
        context="example-site",
        instance="prod",
        status="enabled",
        destinations=destinations,
        created_at="2026-05-29T12:00:00Z",
        updated_at="2026-05-29T12:00:00Z",
        source="test",
    )


def _capture_github_call(
    calls: list[tuple[str, dict[str, object]]], action: str, payload: dict[str, object]
) -> dict[str, object]:
    calls.append((action, payload))
    return {
        "url": "https://github.com/cbusillo/launchplane/issues/123",
        "id": f"github-{action}",
    }


class _GitHubResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def __enter__(self) -> _GitHubResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


def _capture_request(requested_urls: list[str], url: str) -> HttpObservation:
    requested_urls.append(url)
    return HttpObservation(status_code=200, final_url=url, redirect_count=0)


def _add_private_endpoint(
    store: _Store,
    endpoint_key: str = "example-site-prod-runtime",
    *,
    product: str = "example-site",
    context: str = "example-site",
    instance: str = "prod",
    status: Literal["active", "disabled"] = "active",
    url: str = "http://10.0.0.5:8080/health",
) -> None:
    store.private_health_endpoints[endpoint_key] = PrivateHealthEndpointRecord(
        endpoint_key=endpoint_key,
        product=product,
        context=context,
        instance=instance,
        url=url,
        status=status,
        updated_at="2026-05-29T12:00:00Z",
    )


class PublicIngressMonitorCliTests(unittest.TestCase):
    def test_public_ingress_monitor_command_is_retired(self) -> None:
        result = CliRunner().invoke(launchplane_cli, ["public-ingress-monitor", "run-once"])

        self.assertNotEqual(result.exit_code, 0, result.output)
        self.assertIn("No such command 'public-ingress-monitor'", result.output)


class PublicIngressMonitorTests(unittest.TestCase):
    def test_discovers_generic_web_targets_by_default_and_derives_health_url(self) -> None:
        store = _Store((_profile(),))

        targets = discover_public_ingress_monitor_targets(store)

        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].base_url, "https://example.test")
        self.assertEqual(targets[0].health_url, "https://example.test/healthz")

    def test_discovers_tls_targets_for_each_active_route_binding_domain(self) -> None:
        store = _Store((_profile(),))
        store.route_bindings.append(
            _route_binding(domains=(("example.test", "primary"), ("www.example.test", "alias")))
        )

        targets = discover_public_ingress_monitor_targets(store)

        tls_targets = [target for target in targets if target.check_kind == "tls"]
        self.assertEqual(
            [target.tls_domain_name for target in tls_targets], ["example.test", "www.example.test"]
        )
        self.assertEqual([target.tls_domain_role for target in tls_targets], ["primary", "alias"])

    def test_tls_aliases_use_collision_resistant_record_identities(self) -> None:
        store = _Store((_profile(),))
        store.route_bindings.append(
            _route_binding(
                domains=(
                    ("tls-foo.bar.example", "primary"),
                    ("tls-foo-bar.example", "alias"),
                )
            )
        )

        targets = [
            target
            for target in discover_public_ingress_monitor_targets(store)
            if target.check_kind == "tls"
        ]
        record_ids = {
            build_public_ingress_observation_id(
                product=target.product,
                context=target.context,
                instance=target.instance,
                observed_at="2026-07-14T12:00:00Z",
                check_name=target.check_name,
            )
            for target in targets
        }
        incident_ids = {
            build_public_ingress_lane_incident_id(
                product=target.product,
                context=target.context,
                instance=target.instance,
                check_name=target.check_name,
            )
            for target in targets
        }

        self.assertEqual(len(record_ids), 2)
        self.assertEqual(len(incident_ids), 2)

    def test_discovers_inherited_generic_web_drivers(self) -> None:
        store = _Store(
            (
                _profile(
                    driver_id="odoo",
                ).model_copy(update={"health_path": "/cm-website/health"}),
            )
        )

        targets = discover_public_ingress_monitor_targets(store)

        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].driver_id, "odoo")
        self.assertEqual(targets[0].health_url, "https://example.test/launchplane/health")

    def test_discovers_odoo_targets_with_stale_derived_health_url(self) -> None:
        lane = ProductLaneProfile(
            instance="prod",
            context="example-site",
            base_url="https://example.test/",
            health_url="HTTPS://EXAMPLE.TEST/cm-website/health/",
            health_monitoring=_public_health_monitoring(),
        )
        store = _Store(
            (
                _profile(driver_id="odoo", lane=lane).model_copy(
                    update={"health_path": "/cm-website/health"}
                ),
            )
        )

        targets = discover_public_ingress_monitor_targets(store)

        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].health_url, "https://example.test/launchplane/health")

    def test_discovers_odoo_targets_with_explicit_health_url_override(self) -> None:
        lane = ProductLaneProfile(
            instance="prod",
            context="example-site",
            base_url="https://example.test",
            health_url="https://internal.example.test/web/health",
            health_monitoring=_public_health_monitoring(),
        )
        store = _Store(
            (
                _profile(driver_id="odoo", lane=lane).model_copy(
                    update={"health_path": "/cm-website/health"}
                ),
            )
        )

        targets = discover_public_ingress_monitor_targets(store)

        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].health_url, "https://internal.example.test/web/health")

    def test_disabled_lane_is_not_monitored(self) -> None:
        lane = ProductLaneProfile(
            instance="prod",
            context="example-site",
            base_url="https://example.test",
            health_monitoring=ProductLaneHealthMonitoringPolicy(
                monitoring_intent="prelaunch",
                checks=(),
            ),
        )
        store = _Store((_profile(lane=lane),))

        self.assertEqual(discover_public_ingress_monitor_targets(store), ())

    def test_private_url_records_failed_observation_without_request(self) -> None:
        lane = ProductLaneProfile(
            instance="prod",
            context="example-site",
            base_url="https://localhost:3000",
            health_monitoring=_public_health_monitoring(),
        )
        store = _Store((_profile(lane=lane),))
        requested_urls: list[str] = []

        result = run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-05-29T12:05:00Z",
            http_get=lambda url, _timeout: _capture_request(requested_urls, url),
        )

        self.assertEqual(result.fail_count, 1)
        self.assertEqual(requested_urls, [])
        self.assertEqual(store.records[0].status, "fail")
        self.assertEqual(store.records[0].failure_code, "private_url")
        self.assertEqual(result.open_incident_count, 1)

    def test_prelaunch_public_failure_stays_visible_without_opening_incident(self) -> None:
        lane = ProductLaneProfile(
            instance="prod",
            context="example-site",
            base_url="https://example.test",
            health_monitoring=ProductLaneHealthMonitoringPolicy(
                monitoring_intent="prelaunch",
                checks=(ProductLaneHealthCheck(name="public-ingress"),),
            ),
        )
        store = _Store((_profile(lane=lane),))

        result = run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-07-27T16:40:00Z",
            http_get=lambda url, _timeout: HttpObservation(
                status_code=404,
                final_url=url,
                redirect_count=0,
            ),
        )

        self.assertEqual(result.fail_count, 1)
        self.assertEqual(result.open_incident_count, 0)
        self.assertEqual(store.incidents, [])
        self.assertEqual(store.records[0].purpose, "probe")
        self.assertEqual(store.records[0].monitoring_intent, "prelaunch")

    def test_public_to_prelaunch_resolves_incident_without_false_recovery(self) -> None:
        store = _Store((_profile(),))
        run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-07-27T16:41:00Z",
            http_get=lambda url, _timeout: HttpObservation(
                status_code=404,
                final_url=url,
                redirect_count=0,
            ),
        )
        prelaunch_lane = ProductLaneProfile(
            instance="prod",
            context="example-site",
            base_url="https://example.test",
            health_monitoring=ProductLaneHealthMonitoringPolicy(
                monitoring_intent="prelaunch",
                checks=(ProductLaneHealthCheck(name="public-ingress"),),
            ),
        )
        store._profiles = (
            _profile(lane=prelaunch_lane).model_copy(update={"updated_at": "2026-07-27T16:41:30Z"}),
        )

        result = run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-07-27T16:42:00Z",
            http_get=lambda url, _timeout: HttpObservation(
                status_code=404,
                final_url=url,
                redirect_count=0,
            ),
        )

        self.assertEqual(result.resolved_incident_count, 1)
        self.assertEqual(result.reconciliation_count, 1)
        self.assertEqual(store.incidents[0].status, "resolved")
        self.assertEqual(
            store.incidents[0].resolution_reason,
            "monitoring_intent_changed",
        )
        probe = next(record for record in result.records if record.purpose == "probe")
        reconciliation = next(
            record for record in result.records if record.purpose == "reconciliation"
        )
        self.assertEqual(probe.status, "fail")
        self.assertEqual(reconciliation.status, "skipped")
        self.assertNotEqual(probe.record_id, reconciliation.record_id)
        self.assertEqual(
            store.incidents[0].resolved_observation_id,
            reconciliation.record_id,
        )

    def test_public_http_timeout_records_connection_timeout(self) -> None:
        store = _Store((_profile(),))

        result = run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-05-29T12:05:30Z",
            http_get=lambda _url, _timeout: (_ for _ in ()).throw(TimeoutError("timed out")),
        )

        self.assertEqual(result.fail_count, 1)
        self.assertEqual(store.records[0].failure_code, "connection_timeout")

    def test_tls_valid_result_records_passing_observation(self) -> None:
        store = _Store((_profile(),))
        store.route_bindings.append(_route_binding())

        result = run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-07-14T12:10:00Z",
            http_get=lambda url, _timeout: HttpObservation(
                status_code=200,
                final_url=url,
                redirect_count=0,
            ),
            tls_get=lambda _domain, _timeout: PublicTlsProbeResult(
                status="valid",
                failure_code=None,
                summary="TLS certificate is valid.",
                validated_address_count=2,
                certificate=_tls_certificate(),
            ),
        )

        self.assertEqual(result.pass_count, 2)
        tls_record = next(record for record in store.records if record.check_kind == "tls")
        self.assertEqual(tls_record.status, "pass")
        self.assertIsNone(tls_record.failure_code)
        self.assertIsNotNone(tls_record.targets[0].tls)
        assert tls_record.targets[0].tls is not None
        self.assertEqual(tls_record.targets[0].tls.status, "valid")
        self.assertEqual(tls_record.targets[0].tls.recorded.owner, "launchplane")
        self.assertEqual(tls_record.targets[0].tls.recorded.domain_role, "primary")
        self.assertEqual(tls_record.targets[0].tls.probe.validated_address_count, 2)

    def test_tls_expiring_result_opens_incident(self) -> None:
        store = _Store((_profile(),))
        store.route_bindings.append(_route_binding())

        result = run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-07-14T12:11:00Z",
            http_get=lambda url, _timeout: HttpObservation(
                status_code=200,
                final_url=url,
                redirect_count=0,
            ),
            tls_get=lambda _domain, _timeout: PublicTlsProbeResult(
                status="expiring",
                failure_code="tls_expiring",
                summary="TLS certificate expires within 7 day(s).",
                validated_address_count=1,
                certificate=_tls_certificate(days_remaining=7),
            ),
        )

        self.assertEqual(result.fail_count, 1)
        tls_record = next(record for record in store.records if record.check_kind == "tls")
        self.assertEqual(tls_record.failure_code, "tls_expiring")
        self.assertEqual(store.incidents[0].check_kind, "tls")
        self.assertEqual(store.incidents[0].failure_code, "tls_expiring")

    def test_tls_probe_timeout_records_failure_without_aborting_monitor(self) -> None:
        store = _Store((_profile(),))
        store.route_bindings.append(_route_binding())

        result = run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-07-14T12:11:30Z",
            http_get=lambda url, _timeout: HttpObservation(
                status_code=200,
                final_url=url,
                redirect_count=0,
            ),
            tls_get=lambda _domain, _timeout: (_ for _ in ()).throw(TimeoutError("timed out")),
        )

        self.assertEqual(result.pass_count, 1)
        self.assertEqual(result.fail_count, 1)
        tls_record = next(record for record in store.records if record.check_kind == "tls")
        self.assertEqual(tls_record.failure_code, "connection_timeout")
        self.assertIsNotNone(tls_record.targets[0].tls)
        assert tls_record.targets[0].tls is not None
        self.assertEqual(tls_record.targets[0].tls.status, "unreachable")
        self.assertEqual(store.incidents[0].check_kind, "tls")

    def test_tls_failure_mappings_are_deterministic(self) -> None:
        cases = (
            (
                PublicTlsProbeResult(
                    status="expired",
                    failure_code="tls_expired",
                    summary="TLS certificate is expired.",
                    certificate=_tls_certificate(days_remaining=-1),
                ),
                "fail",
                "tls_expired",
            ),
            (
                PublicTlsProbeResult(
                    status="hostname_mismatch",
                    failure_code="tls_hostname_mismatch",
                    summary="hostname mismatch",
                    certificate=_tls_certificate(
                        public_name_match=False,
                        public_name_match_source="none",
                        presented_name_evidence=(),
                    ),
                ),
                "fail",
                "tls_hostname_mismatch",
            ),
            (
                PublicTlsProbeResult(
                    status="untrusted",
                    failure_code="tls_chain_failure",
                    summary="chain failure",
                    certificate=_tls_certificate(),
                ),
                "fail",
                "tls_chain_failure",
            ),
            (
                PublicTlsProbeResult(
                    status="self_signed",
                    failure_code="tls_self_signed",
                    summary="self signed",
                    certificate=_tls_certificate(),
                ),
                "fail",
                "tls_self_signed",
            ),
            (
                PublicTlsProbeResult(
                    status="unreachable",
                    failure_code="dns_failure",
                    summary="dns failed",
                ),
                "fail",
                "dns_failure",
            ),
            (
                PublicTlsProbeResult(
                    status="unsupported",
                    failure_code="tls_unsupported",
                    summary="unsupported protocol",
                ),
                "skipped",
                "tls_unsupported",
            ),
            (
                PublicTlsProbeResult(
                    status="unknown",
                    failure_code="private_url",
                    summary="private destination",
                ),
                "fail",
                "private_url",
            ),
            (
                PublicTlsProbeResult(
                    status="unknown",
                    failure_code="invalid_url",
                    summary="invalid destination",
                ),
                "fail",
                "invalid_url",
            ),
        )
        for probe_result, expected_status, expected_failure_code in cases:
            with self.subTest(status=probe_result.status):
                store = _Store((_profile(),))
                store.route_bindings.append(_route_binding())

                run_public_ingress_monitor_once(
                    record_store=store,
                    checked_at="2026-07-14T12:12:00Z",
                    http_get=lambda url, _timeout: HttpObservation(
                        status_code=200,
                        final_url=url,
                        redirect_count=0,
                    ),
                    tls_get=_constant_tls_probe(probe_result),
                )

                tls_record = next(record for record in store.records if record.check_kind == "tls")
                self.assertEqual(tls_record.status, expected_status)
                self.assertEqual(tls_record.failure_code, expected_failure_code)

    def test_tls_failure_after_skipped_probe_updates_open_incident(self) -> None:
        store = _Store((_profile(),))
        store.route_bindings.append(_route_binding())
        failure = PublicTlsProbeResult(
            status="unreachable",
            failure_code="dns_failure",
            summary="dns failed",
        )
        unsupported = PublicTlsProbeResult(
            status="unsupported",
            failure_code="tls_unsupported",
            summary="unsupported protocol",
        )

        for checked_at, probe_result in (
            ("2026-07-14T12:13:00Z", failure),
            ("2026-07-14T12:14:00Z", unsupported),
            ("2026-07-14T12:15:00Z", failure),
        ):
            run_public_ingress_monitor_once(
                record_store=store,
                checked_at=checked_at,
                http_get=lambda url, _timeout: HttpObservation(
                    status_code=200,
                    final_url=url,
                    redirect_count=0,
                ),
                tls_get=_constant_tls_probe(probe_result),
            )

        tls_incident = next(
            incident for incident in store.incidents if incident.check_kind == "tls"
        )
        self.assertEqual(_incident_event(incident=tls_incident), "opened")
        self.assertEqual(
            [
                event.event
                for event in store.incident_events
                if event.incident_id == tls_incident.incident_id
            ],
            ["opened"],
        )

    def test_tls_notification_policy_scope_and_health_kind_alias(self) -> None:
        destination = PublicIngressNotificationDestination(
            destination_id="github-main",
            kind="github_issue",
            github_repository="cbusillo/launchplane",
            github_label="public-ingress",
        )
        policy = _notification_policy(destination).model_copy(update={"check_kind": "tls"})
        incident = PublicIngressIncidentRecord(
            incident_id="public-ingress-incident-example-site-prod-tls",
            product="example-site",
            repository="cbusillo/example-site",
            driver_id="generic-web",
            context="example-site",
            instance="prod",
            check_name="tls-example-test-deadbeef0000",
            check_kind="tls",
            status="open",
            opened_at="2026-07-14T12:13:00Z",
            opened_observation_id="observation-1",
            latest_observation_id="observation-1",
            latest_observed_at="2026-07-14T12:13:00Z",
            failure_code="dns_failure",
            summary="dns failed",
        )

        self.assertTrue(policy.matches(incident))
        self.assertEqual(PublicIngressHealthCheckKind, PublicIngressCheckKind)

    def test_tls_incident_resolves_after_recovery(self) -> None:
        store = _Store((_profile(),))
        store.route_bindings.append(_route_binding())

        first_failure = run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-07-14T12:13:00Z",
            http_get=lambda url, _timeout: HttpObservation(
                status_code=200,
                final_url=url,
                redirect_count=0,
            ),
            tls_get=lambda _domain, _timeout: PublicTlsProbeResult(
                status="hostname_mismatch",
                failure_code="tls_hostname_mismatch",
                summary="hostname mismatch",
                certificate=_tls_certificate(
                    public_name_match=False,
                    public_name_match_source="none",
                    presented_name_evidence=(),
                ),
            ),
        )
        recovery = run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-07-14T12:14:00Z",
            http_get=lambda url, _timeout: HttpObservation(
                status_code=200,
                final_url=url,
                redirect_count=0,
            ),
            tls_get=lambda _domain, _timeout: PublicTlsProbeResult(
                status="valid",
                failure_code=None,
                summary="TLS certificate is valid.",
                certificate=_tls_certificate(),
            ),
        )

        self.assertEqual(first_failure.fail_count, 1)
        self.assertEqual(recovery.pass_count, 2)
        tls_incidents = [incident for incident in store.incidents if incident.check_kind == "tls"]
        self.assertEqual(tls_incidents[-1].status, "resolved")

    def test_public_http_destination_policy_failure_records_private_url(self) -> None:
        store = _Store((_profile(),))

        with patch(
            "control_plane.workflows.public_ingress_monitor.request_public_http",
            side_effect=PublicHttpDestinationError(
                "private_url",
                "redirect resolved to a private address",
            ),
        ):
            result = run_public_ingress_monitor_once(
                record_store=store,
                checked_at="2026-05-29T12:05:45Z",
            )

        self.assertEqual(result.fail_count, 1)
        self.assertEqual(store.records[0].failure_code, "private_url")
        self.assertIn("private address", store.records[0].targets[0].summary)

    def test_private_http_check_requests_private_health_endpoint_url(self) -> None:
        lane = ProductLaneProfile(
            instance="prod",
            context="example-site",
            health_monitoring=ProductLaneHealthMonitoringPolicy(
                monitoring_intent="private",
                checks=(
                    ProductLaneHealthCheck(
                        name="private-runtime",
                        kind="private_http",
                        private_endpoint_key="example-site-prod-runtime",
                    ),
                ),
            ),
        )
        store = _Store((_profile(lane=lane),))
        _add_private_endpoint(store)
        requested_urls: list[str] = []

        def reject_public_client(_url: str, _timeout: int) -> HttpObservation:
            raise AssertionError("private health checks must not use the public HTTP client")

        result = run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-05-29T12:06:00Z",
            http_get=reject_public_client,
            private_http_get=lambda url, _timeout: _capture_request(requested_urls, url),
        )

        self.assertEqual(result.pass_count, 1)
        self.assertEqual(requested_urls, ["http://10.0.0.5:8080/health"])
        self.assertEqual(store.records[0].check_name, "private-runtime")
        self.assertEqual(store.records[0].check_kind, "private_http")
        self.assertEqual(store.records[0].targets[0].target, "private_health_url")

    def test_public_to_private_reconciles_public_incident_and_keeps_private_probe(self) -> None:
        store = _Store((_profile(),))
        run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-07-27T16:43:00Z",
            http_get=lambda url, _timeout: HttpObservation(
                status_code=500,
                final_url=url,
                redirect_count=0,
            ),
        )
        private_lane = ProductLaneProfile(
            instance="prod",
            context="example-site",
            base_url="https://example.test",
            health_monitoring=ProductLaneHealthMonitoringPolicy(
                monitoring_intent="private",
                checks=(
                    ProductLaneHealthCheck(name="public-ingress"),
                    ProductLaneHealthCheck(
                        name="private-runtime",
                        kind="private_http",
                        private_endpoint_key="example-site-prod-runtime",
                    ),
                ),
            ),
        )
        store._profiles = (
            _profile(lane=private_lane).model_copy(update={"updated_at": "2026-07-27T16:43:30Z"}),
        )
        _add_private_endpoint(store)
        private_urls: list[str] = []

        result = run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-07-27T16:44:00Z",
            http_get=lambda _url, _timeout: (_ for _ in ()).throw(
                AssertionError("private intent must suppress public probes")
            ),
            private_http_get=lambda url, _timeout: _capture_request(private_urls, url),
        )

        self.assertEqual(result.pass_count, 1)
        self.assertEqual(result.reconciliation_count, 1)
        self.assertEqual(result.resolved_incident_count, 1)
        self.assertEqual(private_urls, ["http://10.0.0.5:8080/health"])
        private_probe = next(record for record in result.records if record.purpose == "probe")
        reconciliation = next(
            record for record in result.records if record.purpose == "reconciliation"
        )
        self.assertEqual(private_probe.check_kind, "private_http")
        self.assertEqual(private_probe.status, "pass")
        self.assertEqual(reconciliation.check_kind, "public_http")
        self.assertEqual(reconciliation.status, "skipped")
        self.assertEqual(store.incidents[0].resolution_reason, "monitoring_intent_changed")

    def test_private_http_check_resolves_private_endpoint_record(self) -> None:
        lane = ProductLaneProfile(
            instance="prod",
            context="example-site",
            health_monitoring=ProductLaneHealthMonitoringPolicy(
                monitoring_intent="private",
                checks=(
                    ProductLaneHealthCheck(
                        name="private-runtime",
                        kind="private_http",
                        private_endpoint_key="example-site-prod-runtime",
                    ),
                ),
            ),
        )
        store = _Store((_profile(lane=lane),))
        _add_private_endpoint(store)
        requested_urls: list[str] = []

        result = run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-05-29T12:06:30Z",
            private_http_get=lambda url, _timeout: _capture_request(requested_urls, url),
        )

        self.assertEqual(result.pass_count, 1)
        self.assertEqual(requested_urls, ["http://10.0.0.5:8080/health"])
        self.assertEqual(store.records[0].health_url, "http://10.0.0.5:8080/health")
        self.assertEqual(store.records[0].targets[0].target, "private_health_url")

    def test_prelaunch_private_failure_remains_incident_eligible(self) -> None:
        lane = ProductLaneProfile(
            instance="prod",
            context="example-site",
            health_monitoring=ProductLaneHealthMonitoringPolicy(
                monitoring_intent="prelaunch",
                checks=(
                    ProductLaneHealthCheck(
                        name="private-runtime",
                        kind="private_http",
                        private_endpoint_key="example-site-prod-runtime",
                    ),
                ),
            ),
        )
        store = _Store((_profile(lane=lane),))
        _add_private_endpoint(store)

        result = run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-07-27T17:40:00Z",
            private_http_get=lambda url, _timeout: HttpObservation(
                status_code=503,
                final_url=url,
                redirect_count=0,
            ),
        )

        self.assertEqual(result.fail_count, 1)
        self.assertEqual(result.open_incident_count, 1)
        self.assertEqual(store.incidents[0].check_kind, "private_http")

    def test_private_http_endpoint_reference_fails_closed_when_missing(self) -> None:
        lane = ProductLaneProfile(
            instance="prod",
            context="example-site",
            health_monitoring=ProductLaneHealthMonitoringPolicy(
                monitoring_intent="private",
                checks=(
                    ProductLaneHealthCheck(
                        name="private-runtime",
                        kind="private_http",
                        private_endpoint_key="missing-runtime",
                    ),
                ),
            ),
        )
        store = _Store((_profile(lane=lane),))
        requested_urls: list[str] = []

        result = run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-05-29T12:06:40Z",
            http_get=lambda url, _timeout: _capture_request(requested_urls, url),
        )

        self.assertEqual(result.fail_count, 1)
        self.assertEqual(requested_urls, [])
        self.assertEqual(store.records[0].failure_code, "private_endpoint_not_found")
        self.assertEqual(store.records[0].targets[0].url, "private-endpoint://missing-runtime")
        material_fingerprint = store.records[0].material_fingerprint
        self.assertIsNotNone(material_fingerprint)
        assert material_fingerprint is not None
        self.assertEqual(
            material_fingerprint.route_authority.kind,
            "private_endpoint",
        )
        self.assertEqual(store.incidents[0].failure_code, "private_endpoint_not_found")

    def test_private_http_endpoint_reference_fails_closed_when_disabled(self) -> None:
        lane = ProductLaneProfile(
            instance="prod",
            context="example-site",
            health_monitoring=ProductLaneHealthMonitoringPolicy(
                monitoring_intent="private",
                checks=(
                    ProductLaneHealthCheck(
                        name="private-runtime",
                        kind="private_http",
                        private_endpoint_key="disabled-runtime",
                    ),
                ),
            ),
        )
        store = _Store((_profile(lane=lane),))
        _add_private_endpoint(store, "disabled-runtime", status="disabled")

        result = run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-05-29T12:06:50Z",
            http_get=lambda url, _timeout: _capture_request([], url),
        )

        self.assertEqual(result.fail_count, 1)
        self.assertEqual(store.records[0].failure_code, "private_endpoint_disabled")

    def test_private_http_endpoint_reference_fails_closed_when_lane_mismatched(self) -> None:
        lane = ProductLaneProfile(
            instance="prod",
            context="example-site",
            health_monitoring=ProductLaneHealthMonitoringPolicy(
                monitoring_intent="private",
                checks=(
                    ProductLaneHealthCheck(
                        name="private-runtime",
                        kind="private_http",
                        private_endpoint_key="wrong-lane-runtime",
                    ),
                ),
            ),
        )
        store = _Store((_profile(lane=lane),))
        _add_private_endpoint(store, "wrong-lane-runtime", instance="testing")

        result = run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-05-29T12:07:00Z",
            http_get=lambda url, _timeout: _capture_request([], url),
        )

        self.assertEqual(result.fail_count, 1)
        self.assertEqual(store.records[0].failure_code, "private_endpoint_mismatch")

    def test_private_http_endpoint_reference_fails_closed_when_product_mismatched(
        self,
    ) -> None:
        lane = ProductLaneProfile(
            instance="prod",
            context="example-site",
            health_monitoring=ProductLaneHealthMonitoringPolicy(
                monitoring_intent="private",
                checks=(
                    ProductLaneHealthCheck(
                        name="private-runtime",
                        kind="private_http",
                        private_endpoint_key="wrong-product-runtime",
                    ),
                ),
            ),
        )
        store = _Store((_profile(lane=lane),))
        _add_private_endpoint(store, "wrong-product-runtime", product="other-product")

        result = run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-05-29T12:07:05Z",
            http_get=lambda url, _timeout: _capture_request([], url),
        )

        self.assertEqual(result.fail_count, 1)
        self.assertEqual(store.records[0].failure_code, "private_endpoint_mismatch")

    def test_non_generic_profile_discovers_private_checks_but_not_public_checks(self) -> None:
        lane = ProductLaneProfile(
            instance="prod",
            context="example-site",
            base_url="https://example.test",
            health_monitoring=ProductLaneHealthMonitoringPolicy(
                monitoring_intent="public",
                checks=(
                    ProductLaneHealthCheck(name="public-ingress"),
                    ProductLaneHealthCheck(
                        name="private-runtime",
                        kind="private_http",
                        private_endpoint_key="example-site-prod-runtime",
                    ),
                ),
            ),
        )
        store = _Store((_profile(driver_id="custom-worker", lane=lane),))
        _add_private_endpoint(store)

        targets = discover_public_ingress_monitor_targets(store)

        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].check_name, "private-runtime")
        self.assertEqual(targets[0].check_kind, "private_http")

    def test_provider_check_fails_closed_until_provider_monitor_is_wired(self) -> None:
        lane = ProductLaneProfile(
            instance="prod",
            context="example-site",
            health_monitoring=ProductLaneHealthMonitoringPolicy(
                monitoring_intent="prelaunch",
                checks=(
                    ProductLaneHealthCheck(
                        name="provider-target",
                        kind="provider",
                        provider="dokploy",
                        provider_check="target-health",
                    ),
                ),
            ),
        )
        store = _Store((_profile(lane=lane),))
        requested_urls: list[str] = []

        result = run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-05-29T12:07:00Z",
            http_get=lambda url, _timeout: _capture_request(requested_urls, url),
        )

        self.assertEqual(result.fail_count, 1)
        self.assertEqual(requested_urls, [])
        self.assertEqual(store.records[0].check_name, "provider-target")
        self.assertEqual(store.records[0].check_kind, "provider")
        self.assertEqual(store.records[0].failure_code, "provider_check_unavailable")
        material_fingerprint = store.records[0].material_fingerprint
        self.assertIsNotNone(material_fingerprint)
        assert material_fingerprint is not None
        self.assertEqual(
            material_fingerprint.route_authority.kind,
            "provider",
        )
        self.assertEqual(store.incidents[0].check_name, "provider-target")

    def test_multiple_lane_checks_write_separate_records_and_incidents(self) -> None:
        lane = ProductLaneProfile(
            instance="prod",
            context="example-site",
            base_url="https://example.test",
            health_monitoring=ProductLaneHealthMonitoringPolicy(
                monitoring_intent="public",
                checks=(
                    ProductLaneHealthCheck(name="public-ingress"),
                    ProductLaneHealthCheck(
                        name="private-runtime",
                        kind="private_http",
                        private_endpoint_key="example-site-prod-runtime",
                    ),
                ),
            ),
        )
        store = _Store((_profile(lane=lane),))
        _add_private_endpoint(store)

        def failing_get(url: str, _timeout: int) -> HttpObservation:
            return HttpObservation(
                status_code=503,
                final_url=url,
                redirect_count=0,
            )

        result = run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-05-29T12:08:00Z",
            http_get=failing_get,
            private_http_get=failing_get,
        )

        self.assertEqual(result.fail_count, 2)
        self.assertEqual(len(store.records), 2)
        self.assertEqual(len({record.record_id for record in store.records}), 2)
        self.assertEqual(len(store.incidents), 2)
        self.assertEqual(
            {incident.check_name for incident in store.incidents},
            {"public-ingress", "private-runtime"},
        )

    def test_compares_runtime_identity_when_lane_evidence_exists(self) -> None:
        identity = _identity()
        store = _Store((_profile(),))
        store.lane_summaries[("example-site", "prod")] = LaunchplaneLaneSummary(
            context="example-site",
            instance="prod",
            inventory=EnvironmentInventory(
                context="example-site",
                instance="prod",
                source_git_ref="abc123",
                deploy=DeploymentEvidence(
                    target_name="example-site-prod",
                    target_type="application",
                    deploy_mode="git",
                    status="pass",
                ),
                runtime_identity=identity,
                updated_at="2026-05-29T12:10:00Z",
                deployment_record_id="deploy-1",
            ),
        )

        result = run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-05-29T12:10:00Z",
            http_get=lambda url, _timeout: HttpObservation(
                status_code=200,
                final_url=url,
                redirect_count=0,
                payload={"runtime_identity": identity.model_dump(mode="json")},
            ),
        )

        self.assertEqual(result.pass_count, 1)
        health = store.records[0].targets[-1]
        self.assertEqual(health.runtime_identity_status, "match")

    def test_missing_runtime_identity_is_advisory_until_lane_requires_it(self) -> None:
        identity = _identity()
        store = _Store((_profile(),))
        store.lane_summaries[("example-site", "prod")] = LaunchplaneLaneSummary(
            context="example-site",
            instance="prod",
            inventory=EnvironmentInventory(
                context="example-site",
                instance="prod",
                source_git_ref="abc123",
                deploy=DeploymentEvidence(
                    target_name="example-site-prod",
                    target_type="application",
                    deploy_mode="git",
                    status="pass",
                ),
                runtime_identity=identity,
                updated_at="2026-05-29T12:10:00Z",
                deployment_record_id="deploy-1",
            ),
        )

        result = run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-05-29T12:10:00Z",
            http_get=lambda url, _timeout: HttpObservation(
                status_code=200,
                final_url=url,
                redirect_count=0,
                payload={"status": "ok", "revision": "abc123"},
            ),
        )

        self.assertEqual(result.pass_count, 1)
        health = store.records[0].targets[-1]
        self.assertEqual(health.runtime_identity_status, "missing")
        self.assertEqual(health.status, "pass")
        self.assertEqual(store.incidents, [])

    def test_strict_lane_fails_when_runtime_identity_is_missing(self) -> None:
        identity = _identity()
        lane = ProductLaneProfile(
            instance="prod",
            context="example-site",
            base_url="https://example.test",
            health_monitoring=_public_health_monitoring(require_runtime_identity=True),
        )
        store = _Store((_profile(lane=lane),))
        store.lane_summaries[("example-site", "prod")] = LaunchplaneLaneSummary(
            context="example-site",
            instance="prod",
            inventory=EnvironmentInventory(
                context="example-site",
                instance="prod",
                source_git_ref="abc123",
                deploy=DeploymentEvidence(
                    target_name="example-site-prod",
                    target_type="application",
                    deploy_mode="git",
                    status="pass",
                ),
                runtime_identity=identity,
                updated_at="2026-05-29T12:10:00Z",
                deployment_record_id="deploy-1",
            ),
        )

        result = run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-05-29T12:10:00Z",
            http_get=lambda url, _timeout: HttpObservation(
                status_code=200,
                final_url=url,
                redirect_count=0,
                payload={"status": "ok", "revision": "abc123"},
            ),
        )

        self.assertEqual(result.fail_count, 1)
        health = store.records[0].targets[-1]
        self.assertEqual(health.runtime_identity_status, "missing")
        self.assertEqual(health.status, "fail")
        self.assertEqual(store.incidents[0].status, "open")

    def test_strict_lane_fails_when_expected_runtime_identity_is_unavailable(self) -> None:
        lane = ProductLaneProfile(
            instance="prod",
            context="example-site",
            base_url="https://example.test",
            health_monitoring=_public_health_monitoring(require_runtime_identity=True),
        )
        store = _Store((_profile(lane=lane),))

        result = run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-05-29T12:10:00Z",
            http_get=lambda url, _timeout: HttpObservation(
                status_code=200,
                final_url=url,
                redirect_count=0,
                payload={"status": "ok", "revision": "abc123"},
            ),
        )

        self.assertEqual(result.fail_count, 1)
        health = store.records[0].targets[-1]
        self.assertEqual(health.runtime_identity_status, "unverifiable")
        self.assertEqual(health.failure_code, "wrong_runtime_identity")
        self.assertEqual(health.status, "fail")
        self.assertEqual(store.incidents[0].status, "open")

    def test_runtime_identity_mismatch_fails_even_when_advisory(self) -> None:
        expected_identity = _identity()
        observed_identity = expected_identity.model_copy(
            update={"deployment_record_id": "deploy-other"}
        )
        store = _Store((_profile(),))
        store.lane_summaries[("example-site", "prod")] = LaunchplaneLaneSummary(
            context="example-site",
            instance="prod",
            inventory=EnvironmentInventory(
                context="example-site",
                instance="prod",
                source_git_ref="abc123",
                deploy=DeploymentEvidence(
                    target_name="example-site-prod",
                    target_type="application",
                    deploy_mode="git",
                    status="pass",
                ),
                runtime_identity=expected_identity,
                updated_at="2026-05-29T12:10:00Z",
                deployment_record_id="deploy-1",
            ),
        )

        result = run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-05-29T12:10:00Z",
            http_get=lambda url, _timeout: HttpObservation(
                status_code=200,
                final_url=url,
                redirect_count=0,
                payload={"runtime_identity": observed_identity.model_dump(mode="json")},
            ),
        )

        self.assertEqual(result.fail_count, 1)
        health = store.records[0].targets[-1]
        self.assertEqual(health.runtime_identity_status, "mismatch")
        self.assertEqual(health.status, "fail")
        self.assertIn("deployment_record_id", health.runtime_identity_detail)

    def test_observations_do_not_use_standing_issue_notification_keys(self) -> None:
        store = _Store((_profile(),))

        run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-05-29T12:15:00Z",
            http_get=lambda url, _timeout: HttpObservation(
                status_code=200,
                final_url=url,
                redirect_count=0,
            ),
        )
        run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-05-29T12:20:00Z",
            http_get=lambda url, _timeout: HttpObservation(
                status_code=503,
                final_url=url,
                redirect_count=0,
            ),
        )
        run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-05-29T12:25:00Z",
            http_get=lambda url, _timeout: HttpObservation(
                status_code=200,
                final_url=url,
                redirect_count=0,
            ),
        )

        self.assertEqual([record.status for record in store.records], ["pass", "fail", "pass"])
        self.assertTrue(all(record.notification_key == "" for record in store.records))
        self.assertTrue(all(not record.notification_sent for record in store.records))

    def test_opens_updates_and_resolves_public_ingress_incident(self) -> None:
        store = _Store((_profile(),))

        first_failure = run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-05-29T12:20:00Z",
            http_get=lambda url, _timeout: HttpObservation(
                status_code=503,
                final_url=url,
                redirect_count=0,
            ),
        )
        second_failure = run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-05-29T12:25:00Z",
            http_get=lambda url, _timeout: HttpObservation(
                status_code=500,
                final_url=url,
                redirect_count=0,
            ),
        )
        recovery = run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-05-29T12:30:00Z",
            http_get=lambda url, _timeout: HttpObservation(
                status_code=200,
                final_url=url,
                redirect_count=0,
            ),
        )

        self.assertEqual(first_failure.open_incident_count, 1)
        self.assertEqual(second_failure.open_incident_count, 1)
        self.assertEqual(recovery.resolved_incident_count, 1)
        self.assertEqual(len(store.incidents), 1)
        incident = store.incidents[0]
        self.assertEqual(incident.status, "resolved")
        self.assertEqual(incident.opened_observation_id, store.records[0].record_id)
        self.assertEqual(incident.latest_observation_id, store.records[2].record_id)
        self.assertEqual(incident.resolved_observation_id, store.records[2].record_id)

    def test_reuses_lane_scoped_public_ingress_incident_for_repeated_failures(self) -> None:
        store = _Store((_profile(),))
        incident_id = build_public_ingress_lane_incident_id(
            product="example-site",
            context="example-site",
            instance="prod",
        )
        store.incidents.append(
            PublicIngressIncidentRecord(
                incident_id=incident_id,
                product="example-site",
                repository="cbusillo/example-site",
                driver_id="generic-web",
                context="example-site",
                instance="prod",
                status="open",
                opened_at="2026-05-29T12:20:00Z",
                opened_observation_id="public-ingress-example-site-prod-20260529t122000z",
                latest_observation_id="public-ingress-example-site-prod-20260529t122000z",
                latest_observed_at="2026-05-29T12:20:00Z",
                failure_code="http_error",
                summary="Public ingress failed.",
            )
        )

        result = run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-05-29T12:25:00Z",
            http_get=lambda url, _timeout: HttpObservation(
                status_code=500,
                final_url=url,
                redirect_count=0,
            ),
        )

        self.assertEqual(result.open_incident_count, 1)
        self.assertEqual(len(store.incidents), 1)
        self.assertEqual(store.incidents[0].incident_id, incident_id)
        self.assertEqual(store.incidents[0].opened_at, "2026-05-29T12:20:00Z")
        self.assertEqual(store.incidents[0].latest_observed_at, "2026-05-29T12:25:00Z")

    def test_equivalent_failure_churn_updates_evidence_without_material_event(self) -> None:
        store = _Store((_profile(),))

        first = run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-05-29T12:20:00Z",
            http_get=lambda url, _timeout: HttpObservation(
                status_code=503,
                final_url=url,
                redirect_count=0,
            ),
        )
        second = run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-05-29T12:25:00Z",
            http_get=lambda url, _timeout: HttpObservation(
                status_code=500,
                final_url=url,
                redirect_count=0,
            ),
        )

        self.assertEqual(first.incident_events[0].event, "opened")
        self.assertEqual(second.incident_events, ())
        self.assertEqual([event.event for event in store.incident_events], ["opened"])
        self.assertEqual(len(store.records), 2)
        self.assertEqual(store.records[0].incident_id, store.records[1].incident_id)
        self.assertEqual(
            store.records[0].material_fingerprint_sha256,
            store.records[1].material_fingerprint_sha256,
        )
        self.assertEqual(store.incidents[0].latest_observation_id, store.records[1].record_id)

    def test_material_failure_change_emits_one_immediate_update(self) -> None:
        store = _Store((_profile(),))

        run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-05-29T12:20:00Z",
            http_get=lambda url, _timeout: HttpObservation(
                status_code=503,
                final_url=url,
                redirect_count=0,
            ),
        )

        def redirect_failure(_url: str, _timeout: int) -> HttpObservation:
            raise RuntimeError("redirect loop")

        changed = run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-05-29T12:25:00Z",
            http_get=redirect_failure,
        )

        self.assertEqual([event.event for event in store.incident_events], ["opened", "updated"])
        self.assertEqual(changed.incident_events[0].reason, "material_change")
        self.assertEqual(store.incidents[0].failure_code, "redirect_loop")

    def test_route_authority_change_is_material_even_with_same_failure_code(self) -> None:
        store = _Store((_profile(),))
        run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-05-29T12:20:00Z",
            http_get=lambda url, _timeout: HttpObservation(
                status_code=503,
                final_url=url,
                redirect_count=0,
            ),
        )
        store._profiles = (
            _profile(
                lane=ProductLaneProfile(
                    instance="prod",
                    context="example-site",
                    base_url="https://replacement.example.test",
                    health_monitoring=_public_health_monitoring(),
                )
            ),
        )

        run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-05-29T12:25:00Z",
            http_get=lambda url, _timeout: HttpObservation(
                status_code=503,
                final_url=url,
                redirect_count=0,
            ),
        )

        self.assertEqual([event.event for event in store.incident_events], ["opened", "updated"])
        self.assertNotEqual(
            store.records[0].material_fingerprint_sha256,
            store.records[1].material_fingerprint_sha256,
        )

    def test_tls_severity_change_is_material(self) -> None:
        store = _Store((_profile(),))
        store.route_bindings.append(_route_binding())
        for checked_at, probe_result in (
            (
                "2026-07-14T12:00:00Z",
                PublicTlsProbeResult(
                    status="expiring",
                    failure_code="tls_expiring",
                    summary="certificate expiring",
                    certificate=_tls_certificate(days_remaining=7),
                ),
            ),
            (
                "2026-07-14T12:05:00Z",
                PublicTlsProbeResult(
                    status="expired",
                    failure_code="tls_expired",
                    summary="certificate expired",
                    certificate=_tls_certificate(days_remaining=-1),
                ),
            ),
        ):
            run_public_ingress_monitor_once(
                record_store=store,
                checked_at=checked_at,
                http_get=lambda url, _timeout: HttpObservation(
                    status_code=200,
                    final_url=url,
                    redirect_count=0,
                ),
                tls_get=_constant_tls_probe(probe_result),
            )

        tls_incident_ids = {
            incident.incident_id for incident in store.incidents if incident.check_kind == "tls"
        }
        tls_events = [
            event for event in store.incident_events if event.incident_id in tls_incident_ids
        ]
        self.assertEqual([event.event for event in tls_events], ["opened", "updated"])
        self.assertEqual([event.severity for event in tls_events], ["warning", "critical"])

    def test_policy_reminder_window_is_bounded_and_idempotent(self) -> None:
        store = _Store((_profile(),))
        store.notification_policies.append(
            _notification_policy(
                PublicIngressNotificationDestination(
                    destination_id="discord-ops",
                    kind="discord",
                    discord_webhook_secret="discord-webhook",
                )
            ).model_copy(update={"reminder_interval_seconds": 900})
        )
        discord_posts: list[tuple[str, dict[str, object]]] = []
        drivers = PublicIngressNotificationDriverSet(
            discord_sender=lambda webhook_url, payload: discord_posts.append(
                (webhook_url, payload)
            ),
            secret_resolver=lambda secret_name: (
                "https://discord.com/api/webhooks/test/webhook"
                if secret_name == "discord-webhook"
                else ""
            ),
        )

        for checked_at in (
            "2026-05-29T12:00:00Z",
            "2026-05-29T12:05:00Z",
            "2026-05-29T12:15:00Z",
            "2026-05-29T12:15:00Z",
        ):
            run_public_ingress_monitor_once(
                record_store=store,
                checked_at=checked_at,
                http_get=lambda url, _timeout: HttpObservation(
                    status_code=503,
                    final_url=url,
                    redirect_count=0,
                ),
                notification_drivers=drivers,
            )

        self.assertEqual([event.event for event in store.incident_events], ["opened", "reminder"])
        self.assertEqual(len(store.notification_attempts), 2)
        self.assertEqual(len(discord_posts), 2)
        reminder_state = store.reminder_states[0]
        self.assertEqual(reminder_state.last_window_index, 1)
        self.assertEqual(reminder_state.next_reminder_at, "2026-05-29T12:30:00Z")
        self.assertTrue(all(attempt.incident_event_id for attempt in store.notification_attempts))

    def test_notification_policy_rejects_unbounded_reminder_cadence(self) -> None:
        destination = PublicIngressNotificationDestination(
            destination_id="github-main",
            kind="github_issue",
            github_repository="cbusillo/launchplane",
            github_label="public-ingress",
        )
        for reminder_interval_seconds in (899, 604801):
            with self.subTest(reminder_interval_seconds=reminder_interval_seconds):
                with self.assertRaises(ValueError):
                    PublicIngressNotificationPolicyRecord.model_validate(
                        {
                            **_notification_policy(destination).model_dump(mode="json"),
                            "reminder_interval_seconds": reminder_interval_seconds,
                        }
                    )

    def test_acknowledgement_suppresses_reminders_and_material_change_reactivates(self) -> None:
        store = _Store((_profile(),))
        store.notification_policies.append(
            _notification_policy(
                PublicIngressNotificationDestination(
                    destination_id="github-main",
                    kind="github_issue",
                    github_repository="cbusillo/launchplane",
                    github_label="public-ingress",
                )
            ).model_copy(update={"reminder_interval_seconds": 900})
        )
        run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-05-29T12:00:00Z",
            http_get=lambda url, _timeout: HttpObservation(
                status_code=503,
                final_url=url,
                redirect_count=0,
            ),
        )
        incident = store.incidents[0].model_copy(
            update={
                "notification_state": "acknowledged",
                "notification_state_changed_at": "2026-05-29T12:01:00Z",
                "notification_state_reason": "operator_acknowledged",
            }
        )
        store.write_public_ingress_incident_record(incident)

        run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-05-29T12:15:00Z",
            http_get=lambda url, _timeout: HttpObservation(
                status_code=500,
                final_url=url,
                redirect_count=0,
            ),
        )
        self.assertEqual([event.event for event in store.incident_events], ["opened"])
        self.assertEqual(store.reminder_states[0].status, "suppressed")

        def redirect_failure(_url: str, _timeout: int) -> HttpObservation:
            raise RuntimeError("redirect loop")

        run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-05-29T12:16:00Z",
            http_get=redirect_failure,
        )
        updated = store.incidents[0]
        self.assertEqual(updated.notification_state, "active")
        self.assertEqual(store.incident_events[-1].event, "updated")
        self.assertTrue(store.incident_events[-1].delivery_eligible)

    def test_silence_suppresses_updates_but_never_resolution(self) -> None:
        store = _Store((_profile(),))
        run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-05-29T12:00:00Z",
            http_get=lambda url, _timeout: HttpObservation(
                status_code=503,
                final_url=url,
                redirect_count=0,
            ),
        )
        store.write_public_ingress_incident_record(
            store.incidents[0].model_copy(
                update={
                    "notification_state": "silenced",
                    "notification_state_changed_at": "2026-05-29T12:01:00Z",
                    "notification_state_reason": "bounded_rehearsal",
                    "silenced_until": "2026-05-29T18:00:00Z",
                }
            )
        )

        def redirect_failure(_url: str, _timeout: int) -> HttpObservation:
            raise RuntimeError("redirect loop")

        run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-05-29T13:00:00Z",
            http_get=redirect_failure,
        )
        update_event = store.incident_events[-1]
        self.assertEqual(update_event.event, "updated")
        self.assertFalse(update_event.delivery_eligible)
        self.assertEqual(update_event.suppression_reason, "silenced")

        run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-05-29T14:00:00Z",
            http_get=lambda url, _timeout: HttpObservation(
                status_code=200,
                final_url=url,
                redirect_count=0,
            ),
        )
        resolution_event = store.incident_events[-1]
        self.assertEqual(resolution_event.event, "resolved")
        self.assertTrue(resolution_event.delivery_eligible)

    def test_recovery_threshold_requires_consecutive_passing_observations(self) -> None:
        lane = ProductLaneProfile(
            instance="prod",
            context="example-site",
            base_url="https://example.test",
            health_monitoring=_public_health_monitoring(recovery_observation_threshold=2),
        )
        store = _Store((_profile(lane=lane),))
        run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-05-29T12:00:00Z",
            http_get=lambda url, _timeout: HttpObservation(
                status_code=503,
                final_url=url,
                redirect_count=0,
            ),
        )
        first_pass = run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-05-29T12:05:00Z",
            http_get=lambda url, _timeout: HttpObservation(
                status_code=200,
                final_url=url,
                redirect_count=0,
            ),
        )
        second_pass = run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-05-29T12:10:00Z",
            http_get=lambda url, _timeout: HttpObservation(
                status_code=200,
                final_url=url,
                redirect_count=0,
            ),
        )

        self.assertEqual(first_pass.resolved_incident_count, 0)
        self.assertEqual(first_pass.incidents[0].consecutive_recovery_observations, 1)
        self.assertEqual(second_pass.resolved_incident_count, 1)
        self.assertEqual(store.incidents[0].resolution_reason, "recovered")

    def test_reopened_failure_creates_new_incident_occurrence(self) -> None:
        store = _Store((_profile(),))

        def status_observer(status_code: int) -> Callable[[str, float], HttpObservation]:
            def observe(url: str, _timeout: float) -> HttpObservation:
                return HttpObservation(
                    status_code=status_code,
                    final_url=url,
                    redirect_count=0,
                )

            return observe

        for checked_at, status_code in (
            ("2026-05-29T12:00:00Z", 503),
            ("2026-05-29T12:05:00Z", 200),
            ("2026-05-29T12:10:00Z", 503),
        ):
            run_public_ingress_monitor_once(
                record_store=store,
                checked_at=checked_at,
                http_get=status_observer(status_code),
            )

        self.assertEqual(len(store.incidents), 2)
        self.assertEqual(len({incident.incident_id for incident in store.incidents}), 2)
        self.assertEqual({incident.status for incident in store.incidents}, {"open", "resolved"})

    def test_resolves_all_open_public_ingress_incidents_on_recovery(self) -> None:
        store = _Store((_profile(),))
        store.incidents.extend(
            (
                PublicIngressIncidentRecord(
                    incident_id="public-ingress-incident-example-site-prod-1",
                    product="example-site",
                    repository="cbusillo/example-site",
                    driver_id="generic-web",
                    context="example-site",
                    instance="prod",
                    status="open",
                    opened_at="2026-05-29T12:20:00Z",
                    opened_observation_id="public-ingress-example-site-prod-20260529t122000z",
                    latest_observation_id="public-ingress-example-site-prod-20260529t122000z",
                    latest_observed_at="2026-05-29T12:20:00Z",
                    failure_code="http_error",
                    summary="Public ingress failed.",
                ),
                PublicIngressIncidentRecord(
                    incident_id="public-ingress-incident-example-site-prod-2",
                    product="example-site",
                    repository="cbusillo/example-site",
                    driver_id="generic-web",
                    context="example-site",
                    instance="prod",
                    status="open",
                    opened_at="2026-05-29T12:21:00Z",
                    opened_observation_id="public-ingress-example-site-prod-20260529t122100z",
                    latest_observation_id="public-ingress-example-site-prod-20260529t122100z",
                    latest_observed_at="2026-05-29T12:21:00Z",
                    failure_code="http_error",
                    summary="Public ingress failed again.",
                ),
            )
        )

        result = run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-05-29T12:30:00Z",
            http_get=lambda url, _timeout: HttpObservation(
                status_code=200,
                final_url=url,
                redirect_count=0,
            ),
        )

        self.assertEqual(result.resolved_incident_count, 2)
        self.assertEqual(len(store.incidents), 2)
        self.assertTrue(all(incident.status == "resolved" for incident in store.incidents))
        recovered = next(
            incident for incident in store.incidents if incident.resolution_reason == "recovered"
        )
        duplicate = next(
            incident
            for incident in store.incidents
            if incident.resolution_reason == "duplicate_reconciled"
        )
        self.assertEqual(recovered.resolved_observation_id, store.records[-1].record_id)
        self.assertNotEqual(duplicate.resolved_observation_id, store.records[-1].record_id)

    def test_policy_driven_incident_notifications_record_attempts(self) -> None:
        store = _Store((_profile(),))
        store.notification_policies.append(
            _notification_policy(
                PublicIngressNotificationDestination(
                    destination_id="github-main",
                    kind="github_issue",
                    github_repository="cbusillo/launchplane",
                    github_label="public-ingress",
                ),
                PublicIngressNotificationDestination(
                    destination_id="email-ops",
                    kind="email",
                    email_to=("ops@example.test",),
                    email_from="launchplane@example.test",
                    smtp_host="smtp.example.test",
                    smtp_username_secret="smtp-username",
                    smtp_password_secret="smtp-password",
                ),
                PublicIngressNotificationDestination(
                    destination_id="discord-ops",
                    kind="discord",
                    discord_webhook_secret="discord-webhook",
                ),
            )
        )
        github_calls: list[tuple[str, dict[str, object]]] = []
        email_subjects: list[str] = []
        discord_posts: list[tuple[str, dict[str, object]]] = []
        drivers = PublicIngressNotificationDriverSet(
            github_client=lambda action, payload: _capture_github_call(
                github_calls, action, payload
            ),
            email_sender=lambda _destination, message: email_subjects.append(
                str(message["Subject"])
            ),
            discord_sender=lambda webhook_url, payload: discord_posts.append(
                (webhook_url, payload)
            ),
            secret_resolver=lambda secret_name: {
                "discord-webhook": "https://discord.com/api/webhooks/test/webhook",
                "smtp-username": "launchplane",
                "smtp-password": "secret",
            }.get(secret_name, ""),
        )

        result = run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-05-29T12:25:00Z",
            http_get=lambda _url, _timeout: HttpObservation(
                status_code=503,
                final_url="https://example.test",
                redirect_count=0,
            ),
            notification_drivers=drivers,
        )

        self.assertEqual(result.open_incident_count, 1)
        self.assertEqual(result.delivery_attempt_count, 3)
        self.assertEqual(
            {attempt.destination_kind for attempt in store.notification_attempts},
            {"github_issue", "email", "discord"},
        )
        self.assertTrue(
            all(attempt.delivery_status == "delivered" for attempt in store.notification_attempts)
        )
        self.assertEqual(github_calls[0][0], "create")
        self.assertEqual(email_subjects, ["[Launchplane] Public ingress opened: example-site/prod"])
        self.assertEqual(discord_posts[0][0], "https://discord.com/api/webhooks/test/webhook")

    def test_postgres_monitor_writes_github_notifications_to_outbox(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            store.write_product_profile_record(_profile())
            store.write_public_ingress_notification_policy_record(
                _notification_policy(
                    PublicIngressNotificationDestination(
                        destination_id="github-main",
                        kind="github_issue",
                        github_repository="cbusillo/launchplane",
                        github_label="public-ingress",
                    )
                )
            )

            result = run_public_ingress_monitor_once(
                record_store=store,
                checked_at="2026-05-29T12:25:00Z",
                http_get=lambda _url, _timeout: HttpObservation(
                    status_code=503,
                    final_url="https://example.test",
                    redirect_count=0,
                ),
            )
            outbox_rows = store.list_outbox_delivery_records(
                states=("pending",), kind="public_ingress_notification"
            )
            attempts = store.list_public_ingress_notification_attempt_records()
            observations = store.list_public_ingress_observation_records()
            incidents = store.list_public_ingress_incident_records()
            store.close()

        self.assertEqual(result.open_incident_count, 1)
        self.assertEqual(result.delivery_attempt_count, 0)
        self.assertEqual(len(observations), 1)
        self.assertEqual(len(incidents), 1)
        self.assertEqual(attempts, ())
        self.assertEqual(len(outbox_rows), 1)
        self.assertEqual(outbox_rows[0].aggregate_id, incidents[0].incident_id)
        self.assertIn(
            "launchplane-public-ingress-notification", str(outbox_rows[0].payload["body"])
        )
        self.assertEqual(
            outbox_rows[0].payload["destination"],
            {
                "destination_id": "github-main",
                "kind": "github_issue",
                "status": "enabled",
                "github_repository": "cbusillo/launchplane",
                "github_issue_number": None,
                "github_label": "public-ingress",
            },
        )

    def test_postgres_outbox_dedupes_equivalent_failures_and_reminder_replay(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            store.write_product_profile_record(_profile())
            store.write_public_ingress_notification_policy_record(
                _notification_policy(
                    PublicIngressNotificationDestination(
                        destination_id="github-main",
                        kind="github_issue",
                        github_repository="cbusillo/launchplane",
                        github_label="public-ingress",
                    )
                ).model_copy(update={"reminder_interval_seconds": 900})
            )

            for checked_at in (
                "2026-07-27T12:00:00Z",
                "2026-07-27T12:05:00Z",
                "2026-07-27T12:15:00Z",
                "2026-07-27T12:15:00Z",
            ):
                run_public_ingress_monitor_once(
                    record_store=store,
                    checked_at=checked_at,
                    http_get=lambda _url, _timeout: HttpObservation(
                        status_code=503,
                        final_url="https://example.test",
                        redirect_count=0,
                    ),
                )

            outbox_rows = store.list_outbox_delivery_records(
                states=("pending",), kind="public_ingress_notification"
            )
            events = store.list_public_ingress_incident_event_records()
            reminders = store.list_public_ingress_incident_reminder_state_records()
            observations = store.list_public_ingress_observation_records()
            store.close()

        self.assertEqual([event.event for event in reversed(events)], ["opened", "reminder"])
        self.assertEqual(len(outbox_rows), 2)
        self.assertEqual(len({row.dedupe_key for row in outbox_rows}), 2)
        self.assertEqual(len(observations), 3)
        self.assertEqual(len(reminders), 1)
        self.assertEqual(reminders[0].last_window_index, 1)
        attempt_payloads = [cast(dict[str, object], row.payload["attempt"]) for row in outbox_rows]
        self.assertEqual(
            {str(attempt["incident_event_id"]) for attempt in attempt_payloads},
            {event.event_id for event in events},
        )

    def test_reminder_outbox_recovers_incident_issue_by_stable_marker(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_product_profile_record(_profile())
            store.write_public_ingress_notification_policy_record(
                _notification_policy(
                    PublicIngressNotificationDestination(
                        destination_id="github-main",
                        kind="github_issue",
                        github_repository="cbusillo/launchplane",
                        github_label="public-ingress",
                    )
                ).model_copy(update={"reminder_interval_seconds": 900})
            )
            for checked_at in ("2026-07-27T12:00:00Z", "2026-07-27T12:15:00Z"):
                run_public_ingress_monitor_once(
                    record_store=store,
                    checked_at=checked_at,
                    http_get=lambda _url, _timeout: HttpObservation(
                        status_code=503,
                        final_url="https://example.test",
                        redirect_count=0,
                    ),
                )
            reminder_record = next(
                record
                for record in store.list_outbox_delivery_records(
                    states=("pending",), kind="public_ingress_notification"
                )
                if cast(dict[str, object], record.payload["attempt"])["event"] == "reminder"
            )
            store.close()

        calls: list[tuple[str, dict[str, object]]] = []

        def github_client(action: str, payload: dict[str, object]) -> dict[str, object]:
            calls.append((action, payload))
            if action == "find_marker":
                return {
                    "url": "https://github.com/cbusillo/launchplane/issues/123",
                    "id": "issue-123",
                }
            self.assertEqual(action, "comment")
            return {
                "url": "https://github.com/cbusillo/launchplane/issues/123#issuecomment-1",
                "id": "comment-1",
            }

        delivered = deliver_public_ingress_notification_outbox_delivery(
            record=reminder_record,
            drivers=PublicIngressNotificationDriverSet(github_client=github_client),
            mark_provider_started=lambda _key, _provider: None,
        )

        self.assertEqual([action for action, _payload in calls], ["find_marker", "comment"])
        self.assertEqual(delivered.state, "delivered")
        self.assertEqual(delivered.action, "commented_issue")

    def test_postgres_transition_fences_same_timestamp_monitoring_authority(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            store.write_product_profile_record(_profile())
            target = discover_public_ingress_monitor_targets(store)[0]
            prelaunch_lane = ProductLaneProfile(
                instance="prod",
                context="example-site",
                base_url="https://example.test",
                health_monitoring=ProductLaneHealthMonitoringPolicy(
                    monitoring_intent="prelaunch",
                    checks=(ProductLaneHealthCheck(name="public-ingress"),),
                ),
            )
            store.write_product_profile_record(_profile(lane=prelaunch_lane))
            observation = check_public_ingress_target(
                target=target,
                checked_at="2026-07-27T16:45:30Z",
                timeout_seconds=10,
                http_get=lambda url, _timeout: HttpObservation(
                    status_code=503,
                    final_url=url,
                    redirect_count=0,
                ),
                tls_get=lambda _domain, _timeout: (_ for _ in ()).throw(
                    AssertionError("HTTP target must not use TLS probe")
                ),
            )
            incidents = reconcile_public_ingress_incident(
                record_store=store,
                record=observation,
                write_records=False,
            )

            write_result = store.write_public_ingress_transition_with_outbox(
                observation=observation,
                incidents=incidents,
                expected_profile_sha256=target.profile_sha256,
            )
            stored_observations = store.list_public_ingress_observation_records()
            stored_incidents = store.list_public_ingress_incident_records()
            store.close()

        self.assertEqual(write_result.status, "authority_changed")
        self.assertEqual(len(stored_observations), 1)
        self.assertEqual(stored_incidents, ())

    def test_postgres_transition_fences_private_endpoint_authority_change(self) -> None:
        lane = ProductLaneProfile(
            instance="prod",
            context="example-site",
            health_monitoring=ProductLaneHealthMonitoringPolicy(
                monitoring_intent="private",
                checks=(
                    ProductLaneHealthCheck(
                        name="private-runtime",
                        kind="private_http",
                        private_endpoint_key="example-site-prod-runtime",
                    ),
                ),
            ),
        )
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            store.write_product_profile_record(_profile(lane=lane))
            store.write_private_health_endpoint_record(
                PrivateHealthEndpointRecord(
                    endpoint_key="example-site-prod-runtime",
                    product="example-site",
                    context="example-site",
                    instance="prod",
                    url="http://10.0.0.5:8080/health",
                    updated_at="2026-07-27T17:48:00Z",
                )
            )
            target = discover_public_ingress_monitor_targets(store)[0]
            store.write_private_health_endpoint_record(
                PrivateHealthEndpointRecord(
                    endpoint_key="example-site-prod-runtime",
                    product="example-site",
                    context="example-site",
                    instance="prod",
                    url="http://10.0.0.6:8080/health",
                    updated_at="2026-07-27T17:48:00Z",
                )
            )
            observation = check_public_ingress_target(
                target=target,
                checked_at="2026-07-27T17:48:30Z",
                timeout_seconds=10,
                http_get=lambda url, _timeout: HttpObservation(
                    status_code=503,
                    final_url=url,
                    redirect_count=0,
                ),
                tls_get=lambda _domain, _timeout: self.fail(
                    "private target must not use TLS probe"
                ),
            )
            incidents = reconcile_public_ingress_incident(
                record_store=store,
                record=observation,
                write_records=False,
            )

            write_result = store.write_public_ingress_transition_with_outbox(
                observation=observation,
                incidents=incidents,
                expected_profile_sha256=target.profile_sha256,
                expected_private_endpoint_key=target.private_endpoint_key,
                expected_private_endpoint_sha256=target.private_endpoint_sha256,
            )
            stored_incidents = store.list_public_ingress_incident_records()
            store.close()

        self.assertEqual(write_result.status, "authority_changed")
        self.assertEqual(stored_incidents, ())

    def test_postgres_transition_fences_unversioned_notification_state_change(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            store.write_product_profile_record(_profile())
            run_public_ingress_monitor_once(
                record_store=store,
                checked_at="2026-07-27T17:48:00Z",
                http_get=lambda url, _timeout: HttpObservation(
                    status_code=503,
                    final_url=url,
                    redirect_count=0,
                ),
            )
            stale_incident = store.list_public_ingress_incident_records(status="open")[0]
            stale_observation = store.list_public_ingress_observation_records()[0]
            store.write_public_ingress_incident_record(
                stale_incident.model_copy(
                    update={
                        "notification_state": "acknowledged",
                        "notification_state_changed_at": "2026-07-27T17:48:30Z",
                        "notification_state_reason": "operator_acknowledged",
                    }
                )
            )
            next_observation = stale_observation.model_copy(
                update={
                    "record_id": "public-ingress-example-site-prod-20260727t174900z",
                    "observed_at": "2026-07-27T17:49:00Z",
                    "incident_event_id": "",
                }
            )
            planned_incident = stale_incident.model_copy(
                update={
                    "latest_observation_id": next_observation.record_id,
                    "latest_observed_at": next_observation.observed_at,
                    "state_version": stale_incident.state_version + 1,
                }
            )

            write_result = store.write_public_ingress_transition_with_outbox(
                observation=next_observation,
                incidents=(planned_incident,),
                expected_open_incident_id=stale_incident.incident_id,
                expected_open_incident_state_version=stale_incident.state_version,
                expected_open_incident_sha256=public_ingress_incident_record_sha256(stale_incident),
            )
            stored_incident = store.list_public_ingress_incident_records(status="open")[0]
            stored_observation = next(
                observation
                for observation in store.list_public_ingress_observation_records()
                if observation.record_id == next_observation.record_id
            )
            store.close()

        self.assertEqual(write_result.status, "incident_changed")
        self.assertEqual(stored_incident.notification_state, "acknowledged")
        self.assertEqual(stored_incident.state_version, stale_incident.state_version)
        self.assertEqual(stored_observation.incident_id, "")

    def test_postgres_transition_fences_route_binding_authority_change(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            store.write_product_profile_record(_profile())
            store.write_route_binding_record(_route_binding())
            target = next(
                target
                for target in discover_public_ingress_monitor_targets(store)
                if target.check_kind == "tls"
            )
            store.write_route_binding_record(
                _route_binding(domains=(("replacement.example.test", "primary"),))
            )
            observation = check_public_ingress_target(
                target=target,
                checked_at="2026-07-27T17:49:30Z",
                timeout_seconds=10,
                http_get=lambda _url, _timeout: self.fail("TLS target must not use HTTP probe"),
                tls_get=lambda _domain, _timeout: PublicTlsProbeResult(
                    status="unreachable",
                    failure_code="tls_failure",
                    summary="TLS probe failed.",
                ),
            )
            incidents = reconcile_public_ingress_incident(
                record_store=store,
                record=observation,
                write_records=False,
            )

            write_result = store.write_public_ingress_transition_with_outbox(
                observation=observation,
                incidents=incidents,
                expected_profile_sha256=target.profile_sha256,
                expected_route_binding_sha256=target.route_binding_sha256,
            )
            stored_incidents = store.list_public_ingress_incident_records()
            store.close()

        self.assertEqual(write_result.status, "authority_changed")
        self.assertEqual(stored_incidents, ())

    def test_postgres_mixed_notification_policy_keeps_direct_email_and_discord(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            store.write_product_profile_record(_profile())
            store.write_public_ingress_notification_policy_record(
                _notification_policy(
                    PublicIngressNotificationDestination(
                        destination_id="github-main",
                        kind="github_issue",
                        github_repository="cbusillo/launchplane",
                        github_label="public-ingress",
                    ),
                    PublicIngressNotificationDestination(
                        destination_id="email-ops",
                        kind="email",
                        email_to=("ops@example.test",),
                        email_from="launchplane@example.test",
                        smtp_host="smtp.example.test",
                        smtp_username_secret="smtp-username",
                        smtp_password_secret="smtp-password",
                    ),
                    PublicIngressNotificationDestination(
                        destination_id="discord-ops",
                        kind="discord",
                        discord_webhook_secret="discord-webhook",
                    ),
                )
            )
            email_subjects: list[str] = []
            discord_posts: list[tuple[str, dict[str, object]]] = []
            drivers = PublicIngressNotificationDriverSet(
                github_client=lambda _action, _payload: self.fail(
                    "GitHub notifications must remain queued"
                ),
                email_sender=lambda _destination, message: email_subjects.append(
                    str(message["Subject"])
                ),
                discord_sender=lambda webhook_url, payload: discord_posts.append(
                    (webhook_url, payload)
                ),
                secret_resolver=lambda secret_name: {
                    "discord-webhook": "https://discord.com/api/webhooks/test/webhook",
                    "smtp-username": "launchplane",
                    "smtp-password": "secret",
                }.get(secret_name, ""),
            )

            result = run_public_ingress_monitor_once(
                record_store=store,
                checked_at="2026-05-29T12:25:00Z",
                http_get=lambda _url, _timeout: HttpObservation(
                    status_code=503,
                    final_url="https://example.test",
                    redirect_count=0,
                ),
                notification_drivers=drivers,
            )
            outbox_rows = store.list_outbox_delivery_records(
                states=("pending",),
                kind="public_ingress_notification",
            )
            attempts = store.list_public_ingress_notification_attempt_records()
            store.close()

        self.assertEqual(result.delivery_attempt_count, 2)
        self.assertEqual(len(outbox_rows), 1)
        self.assertEqual(
            {attempt.destination_kind for attempt in attempts},
            {"email", "discord"},
        )
        self.assertEqual(email_subjects, ["[Launchplane] Public ingress opened: example-site/prod"])
        self.assertEqual(len(discord_posts), 1)

    def test_close_reconciliation_ensures_issue_is_closed_after_marker_comment(self) -> None:
        marker = "attempt-public-ingress-resolved"
        record = OutboxDeliveryRecord(
            delivery_id="outbox-public-ingress-close",
            kind="public_ingress_notification",
            state="running",
            aggregate_type="public_ingress_incident",
            aggregate_id="incident-example-site-prod",
            dedupe_key=f"public_ingress_notification:{marker}",
            created_at="2026-05-29T12:25:00Z",
            updated_at="2026-05-29T12:25:01Z",
            next_attempt_at="2026-05-29T12:25:00Z",
            lease_owner="worker-b",
            lease_expires_at="2026-05-29T12:30:00Z",
            attempt=2,
            provider_operation_key=f"public_ingress_notification:{marker}:close",
            provider_id="github",
            payload={
                "attempt": {
                    "attempt_id": marker,
                    "incident_id": "incident-example-site-prod",
                    "event": "resolved",
                    "policy_id": "public-ingress-notifications-example-site",
                    "destination_id": "github-main",
                    "destination_kind": "github_issue",
                    "attempted_at": "2026-05-29T12:25:00Z",
                    "observation_id": "observation-example-site-prod",
                },
                "destination": {
                    "destination_id": "github-main",
                    "kind": "github_issue",
                    "status": "enabled",
                    "github_repository": "cbusillo/launchplane",
                    "github_issue_number": None,
                    "github_label": "public-ingress",
                },
                "body": f"Resolved\n<!-- launchplane-public-ingress-notification:{marker} -->",
                "existing_issue_url": "https://github.com/cbusillo/launchplane/issues/123",
                "marker": marker,
            },
        )
        actions: list[str] = []

        def github_client(action: str, _payload: dict[str, object]) -> dict[str, object]:
            actions.append(action)
            if action == "find_marker":
                return {
                    "url": "https://github.com/cbusillo/launchplane/issues/123#issuecomment-1",
                    "id": "comment-1",
                }
            if action == "ensure_closed":
                return {
                    "url": "https://github.com/cbusillo/launchplane/issues/123",
                    "id": "issue-123",
                }
            self.fail(f"unexpected GitHub action {action}")

        result = deliver_public_ingress_notification_outbox_delivery(
            record=record,
            drivers=PublicIngressNotificationDriverSet(github_client=github_client),
            mark_provider_started=lambda _provider_operation_key, _provider_id: self.fail(
                "existing provider marker must not be replaced"
            ),
        )

        self.assertEqual(actions, ["find_marker", "ensure_closed"])
        self.assertEqual(result.state, "delivered")
        self.assertEqual(result.action, "closed_issue")
        self.assertEqual(result.external_id, "issue-123")

    def test_resolved_outbox_without_prior_issue_is_terminally_skipped(self) -> None:
        marker = "public-ingress-resolved-no-prior-issue"
        record = OutboxDeliveryRecord(
            delivery_id="outbox-public-ingress-resolved-no-prior-issue",
            kind="public_ingress_notification",
            state="running",
            aggregate_type="public_ingress_incident",
            aggregate_id="incident-example-site-prod",
            dedupe_key=f"public_ingress_notification:{marker}",
            created_at="2026-07-27T17:47:00Z",
            updated_at="2026-07-27T17:47:00Z",
            next_attempt_at="2026-07-27T17:47:00Z",
            lease_owner="worker-a",
            lease_expires_at="2026-07-27T17:52:00Z",
            payload={
                "attempt": {
                    "attempt_id": marker,
                    "incident_id": "incident-example-site-prod",
                    "event": "resolved",
                    "policy_id": "public-ingress-notifications-example-site",
                    "destination_id": "github-main",
                    "destination_kind": "github_issue",
                    "attempted_at": "2026-07-27T17:47:00Z",
                    "observation_id": "observation-example-site-prod",
                },
                "destination": {
                    "destination_id": "github-main",
                    "kind": "github_issue",
                    "status": "enabled",
                    "github_repository": "cbusillo/launchplane",
                    "github_issue_number": None,
                    "github_label": "public-ingress",
                },
                "body": f"Resolved\n<!-- launchplane-public-ingress-notification:{marker} -->",
                "existing_issue_url": "",
                "marker": marker,
            },
        )

        result = deliver_public_ingress_notification_outbox_delivery(
            record=record,
            drivers=PublicIngressNotificationDriverSet(
                github_client=lambda _action, _payload: self.fail(
                    "resolution without a prior issue must not call GitHub"
                )
            ),
            mark_provider_started=lambda _key, _provider: self.fail(
                "skipped resolution must not start a provider operation"
            ),
        )

        self.assertEqual(result.state, "delivered")
        self.assertEqual(result.action, "skipped_no_prior_issue")
        attempt_result = result.payload["attempt_result"]
        self.assertIsInstance(attempt_result, dict)
        assert isinstance(attempt_result, dict)
        self.assertEqual(attempt_result["delivery_status"], "skipped")

    def test_github_ensure_closed_patches_open_issue_without_duplicate_comment(self) -> None:
        with patch(
            "control_plane.workflows.public_ingress_monitor._github_api_request",
            side_effect=(
                {
                    "state": "open",
                    "html_url": "https://github.com/cbusillo/launchplane/issues/123",
                    "node_id": "issue-123",
                },
                {
                    "state": "closed",
                    "html_url": "https://github.com/cbusillo/launchplane/issues/123",
                    "node_id": "issue-123",
                },
            ),
        ) as request_mock:
            response = _gh_issue_client(
                "ensure_closed",
                {
                    "repository": "cbusillo/launchplane",
                    "issue_url": "https://github.com/cbusillo/launchplane/issues/123",
                },
                token="github-token",
            )

        self.assertEqual(response["id"], "issue-123")
        self.assertEqual(
            [call.kwargs["method"] for call in request_mock.call_args_list],
            ["GET", "PATCH"],
        )
        self.assertEqual(
            request_mock.call_args_list[1].kwargs["body"],
            {"state": "closed", "state_reason": "completed"},
        )

    def test_transient_github_notification_failure_schedules_retry(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            store.write_product_profile_record(_profile())
            store.write_public_ingress_notification_policy_record(
                _notification_policy(
                    PublicIngressNotificationDestination(
                        destination_id="github-main",
                        kind="github_issue",
                        github_repository="cbusillo/launchplane",
                        github_label="public-ingress",
                    )
                )
            )
            run_public_ingress_monitor_once(
                record_store=store,
                checked_at="2026-05-29T12:25:00Z",
                http_get=lambda _url, _timeout: HttpObservation(
                    status_code=503,
                    final_url="https://example.test",
                    redirect_count=0,
                ),
            )
            delivery = store.list_outbox_delivery_records(
                states=("pending",),
                kind="public_ingress_notification",
            )[0]

            def github_client(
                _action: str,
                _payload: dict[str, object],
            ) -> dict[str, object]:
                raise RuntimeError("temporary GitHub failure")

            with patch.object(
                store,
                "_database_mutation_timestamp",
                side_effect=lambda _session: "2026-05-29T12:25:01Z",
            ):
                worker_result = run_outbox_worker_once(
                    record_store=store,
                    control_plane_root=Path("."),
                    lease_owner="worker-a",
                    notification_drivers=PublicIngressNotificationDriverSet(
                        github_client=github_client
                    ),
                )
            loaded = store.read_outbox_delivery_record(delivery.delivery_id)
            attempts = store.list_public_ingress_notification_attempt_records()
            store.close()

        self.assertEqual(worker_result.status, "pending")
        self.assertEqual(loaded.state, "pending")
        self.assertEqual(loaded.error_code, "github_provider_error")
        self.assertEqual(loaded.next_attempt_at, "2026-05-29T12:25:06Z")
        self.assertEqual(attempts, ())

    def test_notification_policies_can_scope_to_health_check_kind(self) -> None:
        lane = ProductLaneProfile(
            instance="prod",
            context="example-site",
            base_url="https://example.test",
            health_monitoring=ProductLaneHealthMonitoringPolicy(
                monitoring_intent="public",
                checks=(
                    ProductLaneHealthCheck(name="public-ingress"),
                    ProductLaneHealthCheck(
                        name="private-runtime",
                        kind="private_http",
                        private_endpoint_key="example-site-prod-runtime",
                    ),
                ),
            ),
        )
        store = _Store((_profile(lane=lane),))
        _add_private_endpoint(store)
        store.notification_policies.extend(
            (
                PublicIngressNotificationPolicyRecord(
                    policy_id="public-health-notifications",
                    product="example-site",
                    context="example-site",
                    instance="prod",
                    check_kind="public_http",
                    destinations=(
                        PublicIngressNotificationDestination(
                            destination_id="public-discord",
                            kind="discord",
                            discord_webhook_secret="public-discord-webhook",
                        ),
                    ),
                    created_at="2026-05-29T12:00:00Z",
                    updated_at="2026-05-29T12:00:00Z",
                    source="test",
                ),
                PublicIngressNotificationPolicyRecord(
                    policy_id="private-health-notifications",
                    product="example-site",
                    context="example-site",
                    instance="prod",
                    check_kind="private_http",
                    destinations=(
                        PublicIngressNotificationDestination(
                            destination_id="private-discord",
                            kind="discord",
                            discord_webhook_secret="private-discord-webhook",
                        ),
                    ),
                    created_at="2026-05-29T12:00:00Z",
                    updated_at="2026-05-29T12:00:00Z",
                    source="test",
                ),
            )
        )
        discord_posts: list[tuple[str, dict[str, object]]] = []
        drivers = PublicIngressNotificationDriverSet(
            discord_sender=lambda webhook_url, payload: discord_posts.append(
                (webhook_url, payload)
            ),
            secret_resolver=lambda secret_name: {
                "public-discord-webhook": "https://discord.com/api/webhooks/public/webhook",
                "private-discord-webhook": "https://discord.com/api/webhooks/private/webhook",
            }.get(secret_name, ""),
        )

        result = run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-05-29T12:25:00Z",
            http_get=lambda _url, _timeout: HttpObservation(
                status_code=503,
                final_url="https://example.test",
                redirect_count=0,
            ),
            private_http_get=lambda _url, _timeout: HttpObservation(
                status_code=503,
                final_url="http://10.0.0.5:8080/health",
                redirect_count=0,
            ),
            notification_drivers=drivers,
        )

        self.assertEqual(result.open_incident_count, 2)
        self.assertEqual(result.delivery_attempt_count, 2)
        self.assertEqual(
            {attempt.destination_id for attempt in store.notification_attempts},
            {"public-discord", "private-discord"},
        )
        self.assertEqual(
            {post[0] for post in discord_posts},
            {
                "https://discord.com/api/webhooks/public/webhook",
                "https://discord.com/api/webhooks/private/webhook",
            },
        )

    def test_policy_delivery_failures_are_recorded_per_destination(self) -> None:
        store = _Store((_profile(),))
        store.notification_policies.append(
            _notification_policy(
                PublicIngressNotificationDestination(
                    destination_id="email-ops",
                    kind="email",
                    email_to=("ops@example.test",),
                    email_from="launchplane@example.test",
                    smtp_host="smtp.example.test",
                    smtp_username_secret="smtp-username",
                    smtp_password_secret="smtp-password",
                ),
                PublicIngressNotificationDestination(
                    destination_id="discord-ops",
                    kind="discord",
                    discord_webhook_secret="discord-webhook",
                ),
            )
        )
        drivers = PublicIngressNotificationDriverSet(
            email_sender=lambda _destination, _message: (_ for _ in ()).throw(
                RuntimeError("smtp unavailable")
            ),
            discord_sender=lambda _webhook_url, _payload: None,
            secret_resolver=lambda secret_name: {
                "discord-webhook": "https://discord.com/api/webhooks/test/webhook",
                "smtp-username": "launchplane",
                "smtp-password": "secret",
            }.get(secret_name, ""),
        )

        result = run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-05-29T12:25:00Z",
            http_get=lambda _url, _timeout: HttpObservation(
                status_code=503,
                final_url="https://example.test",
                redirect_count=0,
            ),
            notification_drivers=drivers,
        )

        self.assertEqual(result.delivery_attempt_count, 2)
        statuses = {
            attempt.destination_kind: attempt.delivery_status
            for attempt in store.notification_attempts
        }
        self.assertEqual(statuses, {"email": "failed", "discord": "delivered"})
        failed = next(
            attempt
            for attempt in store.notification_attempts
            if attempt.delivery_status == "failed"
        )
        self.assertIn("smtp unavailable", failed.error_message)

    def test_github_incident_delivery_closes_created_issue_on_recovery(self) -> None:
        store = _Store((_profile(),))
        store.notification_policies.append(
            _notification_policy(
                PublicIngressNotificationDestination(
                    destination_id="github-main",
                    kind="github_issue",
                    github_repository="cbusillo/launchplane",
                    github_label="public-ingress",
                )
            )
        )
        github_calls: list[tuple[str, dict[str, object]]] = []
        drivers = PublicIngressNotificationDriverSet(
            github_client=lambda action, payload: _capture_github_call(
                github_calls, action, payload
            )
        )
        run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-05-29T12:25:00Z",
            http_get=lambda _url, _timeout: HttpObservation(
                status_code=503,
                final_url="https://example.test",
                redirect_count=0,
            ),
            notification_drivers=drivers,
        )
        recovery = run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-05-29T12:30:00Z",
            http_get=lambda url, _timeout: HttpObservation(
                status_code=200,
                final_url=url,
                redirect_count=0,
            ),
            notification_drivers=drivers,
        )

        self.assertEqual(recovery.resolved_incident_count, 1)
        self.assertEqual([call[0] for call in github_calls], ["create", "close"])
        self.assertEqual(
            github_calls[1][1]["issue_url"],
            "https://github.com/cbusillo/launchplane/issues/123",
        )

    def test_github_resolution_without_prior_issue_is_skipped(self) -> None:
        store = _Store((_profile(),))
        failure = run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-07-27T17:45:00Z",
            notify=False,
            http_get=lambda url, _timeout: HttpObservation(
                status_code=503,
                final_url=url,
                redirect_count=0,
            ),
        )
        incident = failure.incidents[0].model_copy(
            update={
                "status": "resolved",
                "resolved_at": "2026-07-27T17:46:00Z",
                "resolved_observation_id": failure.records[0].record_id,
                "resolution_reason": "monitoring_intent_changed",
            }
        )
        destination = PublicIngressNotificationDestination(
            destination_id="github-main",
            kind="github_issue",
            github_repository="cbusillo/launchplane",
            github_label="public-ingress",
        )

        delivery = _deliver_github_issue_notification(
            destination=destination,
            event="resolved",
            incident=incident,
            observation=failure.records[0],
            previous_attempts=(),
            github_client=lambda _action, _payload: self.fail(
                "resolution without a prior issue must not call GitHub"
            ),
        )

        self.assertEqual(delivery.delivery_status, "skipped")
        self.assertEqual(delivery.action, "no_prior_issue")

    def test_default_github_incident_driver_uses_managed_token_api(self) -> None:
        requests: list[Request] = []

        def fake_urlopen(request: Request, timeout: int) -> _GitHubResponse:
            requests.append(request)
            self.assertEqual(timeout, 15)
            return _GitHubResponse(
                {
                    "html_url": "https://github.com/cbusillo/launchplane/issues/123",
                    "id": 123,
                }
            )

        store = _Store((_profile(),))
        store.notification_policies.append(
            _notification_policy(
                PublicIngressNotificationDestination(
                    destination_id="github-main",
                    kind="github_issue",
                    github_repository="cbusillo/launchplane",
                    github_label="public-ingress",
                )
            )
        )
        drivers = PublicIngressNotificationDriverSet()

        with (
            patch.dict(
                "os.environ",
                {"LAUNCHPLANE_PUBLIC_INGRESS_GITHUB_TOKEN": "managed-token"},
                clear=True,
            ),
            patch("control_plane.workflows.public_ingress_monitor.urlopen", fake_urlopen),
        ):
            result = run_public_ingress_monitor_once(
                record_store=store,
                checked_at="2026-05-29T12:25:00Z",
                http_get=lambda _url, _timeout: HttpObservation(
                    status_code=503,
                    final_url="https://example.test",
                    redirect_count=0,
                ),
                notification_drivers=drivers,
            )

        self.assertEqual(result.delivery_attempt_count, 1)
        self.assertEqual(store.notification_attempts[0].delivery_status, "delivered")
        request = requests[0]
        self.assertEqual(
            getattr(request, "full_url"),
            "https://api.github.com/repos/cbusillo/launchplane/issues",
        )
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer managed-token")


if __name__ == "__main__":
    unittest.main()
