from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from typing import Literal
from unittest.mock import patch
from urllib.request import Request

from click.testing import CliRunner

from control_plane.cli import main as launchplane_cli
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.lane_summary import LaunchplaneLaneSummary
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
from control_plane.contracts.public_ingress_monitoring import PublicIngressObservationRecord
from control_plane.contracts.public_ingress_monitoring import PublicIngressIncidentRecord
from control_plane.contracts.public_ingress_monitoring import (
    PublicIngressNotificationAttemptRecord,
)
from control_plane.contracts.public_ingress_monitoring import PublicIngressNotificationDestination
from control_plane.contracts.public_ingress_monitoring import PublicIngressNotificationPolicyRecord
from control_plane.contracts.public_ingress_monitoring import build_public_ingress_lane_incident_id
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.workflows.public_ingress_monitor import (
    HttpObservation,
    PublicIngressNotificationDriverSet,
    discover_public_ingress_monitor_targets,
    run_public_ingress_monitor_once,
)
from control_plane.outbound_http import PublicHttpDestinationError
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.stores import _sqlite_database_url


def _public_health_monitoring(
    *, require_runtime_identity: bool = False
) -> ProductLaneHealthMonitoringPolicy:
    return ProductLaneHealthMonitoringPolicy(
        checks=(
            ProductLaneHealthCheck(
                name="public-ingress",
                require_runtime_identity=require_runtime_identity,
            ),
        )
    )


class _Store:
    def __init__(self, profiles: tuple[LaunchplaneProductProfileRecord, ...]) -> None:
        self._profiles = profiles
        self.records: list[PublicIngressObservationRecord] = []
        self.incidents: list[PublicIngressIncidentRecord] = []
        self.notification_policies: list[PublicIngressNotificationPolicyRecord] = []
        self.notification_attempts: list[PublicIngressNotificationAttemptRecord] = []
        self.lane_summaries: dict[tuple[str, str], LaunchplaneLaneSummary] = {}
        self.private_health_endpoints: dict[str, PrivateHealthEndpointRecord] = {}

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
            health_monitoring=ProductLaneHealthMonitoringPolicy(checks=()),
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

    def test_public_http_timeout_records_connection_timeout(self) -> None:
        store = _Store((_profile(),))

        result = run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-05-29T12:05:30Z",
            http_get=lambda _url, _timeout: (_ for _ in ()).throw(TimeoutError("timed out")),
        )

        self.assertEqual(result.fail_count, 1)
        self.assertEqual(store.records[0].failure_code, "connection_timeout")

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
                checks=(
                    ProductLaneHealthCheck(
                        name="private-runtime",
                        kind="private_http",
                        private_endpoint_key="example-site-prod-runtime",
                    ),
                )
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

    def test_private_http_check_resolves_private_endpoint_record(self) -> None:
        lane = ProductLaneProfile(
            instance="prod",
            context="example-site",
            health_monitoring=ProductLaneHealthMonitoringPolicy(
                checks=(
                    ProductLaneHealthCheck(
                        name="private-runtime",
                        kind="private_http",
                        private_endpoint_key="example-site-prod-runtime",
                    ),
                )
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

    def test_private_http_endpoint_reference_fails_closed_when_missing(self) -> None:
        lane = ProductLaneProfile(
            instance="prod",
            context="example-site",
            health_monitoring=ProductLaneHealthMonitoringPolicy(
                checks=(
                    ProductLaneHealthCheck(
                        name="private-runtime",
                        kind="private_http",
                        private_endpoint_key="missing-runtime",
                    ),
                )
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
        self.assertEqual(store.incidents[0].failure_code, "private_endpoint_not_found")

    def test_private_http_endpoint_reference_fails_closed_when_disabled(self) -> None:
        lane = ProductLaneProfile(
            instance="prod",
            context="example-site",
            health_monitoring=ProductLaneHealthMonitoringPolicy(
                checks=(
                    ProductLaneHealthCheck(
                        name="private-runtime",
                        kind="private_http",
                        private_endpoint_key="disabled-runtime",
                    ),
                )
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
                checks=(
                    ProductLaneHealthCheck(
                        name="private-runtime",
                        kind="private_http",
                        private_endpoint_key="wrong-lane-runtime",
                    ),
                )
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
                checks=(
                    ProductLaneHealthCheck(
                        name="private-runtime",
                        kind="private_http",
                        private_endpoint_key="wrong-product-runtime",
                    ),
                )
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
                checks=(
                    ProductLaneHealthCheck(name="public-ingress"),
                    ProductLaneHealthCheck(
                        name="private-runtime",
                        kind="private_http",
                        private_endpoint_key="example-site-prod-runtime",
                    ),
                )
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
                checks=(
                    ProductLaneHealthCheck(
                        name="provider-target",
                        kind="provider",
                        provider="dokploy",
                        provider_check="target-health",
                    ),
                )
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
        self.assertEqual(store.incidents[0].check_name, "provider-target")

    def test_multiple_lane_checks_write_separate_records_and_incidents(self) -> None:
        lane = ProductLaneProfile(
            instance="prod",
            context="example-site",
            base_url="https://example.test",
            health_monitoring=ProductLaneHealthMonitoringPolicy(
                checks=(
                    ProductLaneHealthCheck(name="public-ingress"),
                    ProductLaneHealthCheck(
                        name="private-runtime",
                        kind="private_http",
                        private_endpoint_key="example-site-prod-runtime",
                    ),
                )
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
        self.assertTrue(
            all(
                incident.resolved_observation_id == store.records[-1].record_id
                for incident in store.incidents
            )
        )

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

    def test_notification_policies_can_scope_to_health_check_kind(self) -> None:
        lane = ProductLaneProfile(
            instance="prod",
            context="example-site",
            base_url="https://example.test",
            health_monitoring=ProductLaneHealthMonitoringPolicy(
                checks=(
                    ProductLaneHealthCheck(name="public-ingress"),
                    ProductLaneHealthCheck(
                        name="private-runtime",
                        kind="private_http",
                        private_endpoint_key="example-site-prod-runtime",
                    ),
                )
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
