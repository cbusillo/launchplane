from __future__ import annotations

import unittest

from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.lane_summary import LaunchplaneLaneSummary
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductImageProfile,
    ProductLaneProfile,
    ProductPublicIngressMonitoringPolicy,
)
from control_plane.contracts.promotion_record import DeploymentEvidence
from control_plane.contracts.public_ingress_monitoring import PublicIngressObservationRecord
from control_plane.contracts.public_ingress_monitoring import PublicIngressTargetObservation
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.workflows.public_ingress_monitor import (
    HttpObservation,
    build_github_issue_notifier,
    discover_public_ingress_monitor_targets,
    run_public_ingress_monitor_once,
)


class _Store:
    def __init__(self, profiles: tuple[LaunchplaneProductProfileRecord, ...]) -> None:
        self._profiles = profiles
        self.records: list[PublicIngressObservationRecord] = []
        self.lane_summaries: dict[tuple[str, str], LaunchplaneLaneSummary] = {}

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
        limit: int | None = None,
    ) -> tuple[PublicIngressObservationRecord, ...]:
        records = [
            record
            for record in self.records
            if (not product or record.product == product)
            and (not context_name or record.context == context_name)
            and (not instance_name or record.instance == instance_name)
        ]
        records.sort(key=lambda record: (record.observed_at, record.record_id), reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def write_public_ingress_observation_record(
        self, record: PublicIngressObservationRecord
    ) -> None:
        self.records.append(record)


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
                public_ingress_monitoring=ProductPublicIngressMonitoringPolicy(
                    alert_issue_url="https://github.com/cbusillo/launchplane/issues/929"
                ),
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


def _record_notification(
    notifications: list[tuple[str, str]],
    record: PublicIngressObservationRecord,
    previous: PublicIngressObservationRecord | None,
) -> bool:
    notifications.append((record.status, previous.status if previous else "missing"))
    return True


def _capture_command(commands: list[list[str]], command: list[str]) -> object:
    commands.append(command)
    return object()


def _capture_request(requested_urls: list[str], url: str) -> HttpObservation:
    requested_urls.append(url)
    return HttpObservation(status_code=200, final_url=url, redirect_count=0)


class PublicIngressMonitorTests(unittest.TestCase):
    def test_discovers_generic_web_targets_by_default_and_derives_health_url(self) -> None:
        store = _Store((_profile(),))

        targets = discover_public_ingress_monitor_targets(store)

        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].base_url, "https://example.test")
        self.assertEqual(targets[0].health_url, "https://example.test/healthz")

    def test_discovers_inherited_generic_web_drivers(self) -> None:
        store = _Store((_profile(driver_id="odoo"),))

        targets = discover_public_ingress_monitor_targets(store)

        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].driver_id, "odoo")

    def test_disabled_lane_is_not_monitored(self) -> None:
        lane = ProductLaneProfile(
            instance="prod",
            context="example-site",
            base_url="https://example.test",
            public_ingress_monitoring=ProductPublicIngressMonitoringPolicy(enabled=False),
        )
        store = _Store((_profile(lane=lane),))

        self.assertEqual(discover_public_ingress_monitor_targets(store), ())

    def test_private_url_records_skipped_observation_without_request(self) -> None:
        lane = ProductLaneProfile(
            instance="prod",
            context="example-site",
            base_url="https://localhost:3000",
        )
        store = _Store((_profile(lane=lane),))
        requested_urls: list[str] = []

        result = run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-05-29T12:05:00Z",
            http_get=lambda url, _timeout: _capture_request(requested_urls, url),
        )

        self.assertEqual(result.skipped_count, 1)
        self.assertEqual(requested_urls, [])
        self.assertEqual(store.records[0].failure_code, "private_url")

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

    def test_notifies_on_failure_and_recovery_transitions(self) -> None:
        store = _Store((_profile(),))
        notifications: list[tuple[str, str]] = []

        run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-05-29T12:15:00Z",
            http_get=lambda url, _timeout: HttpObservation(
                status_code=200,
                final_url=url,
                redirect_count=0,
            ),
            notifier=lambda record, previous: _record_notification(notifications, record, previous),
        )
        run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-05-29T12:20:00Z",
            http_get=lambda url, _timeout: HttpObservation(
                status_code=503,
                final_url=url,
                redirect_count=0,
            ),
            notifier=lambda record, previous: _record_notification(notifications, record, previous),
        )
        run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-05-29T12:25:00Z",
            http_get=lambda url, _timeout: HttpObservation(
                status_code=200,
                final_url=url,
                redirect_count=0,
            ),
            notifier=lambda record, previous: _record_notification(notifications, record, previous),
        )

        self.assertEqual(notifications, [("fail", "pass"), ("pass", "fail")])
        self.assertTrue(store.records[1].notification_sent)
        self.assertTrue(store.records[2].notification_sent)

    def test_github_issue_notifier_comments_on_alert_issue(self) -> None:
        commands: list[list[str]] = []
        notifier = build_github_issue_notifier(
            runner=lambda command: _capture_command(commands, command)
        )
        record = PublicIngressObservationRecord(
            record_id="public-ingress-example-site-prod-20260529t122500z",
            product="example-site",
            context="example-site",
            instance="prod",
            observed_at="2026-05-29T12:25:00Z",
            status="fail",
            failure_code="http_error",
            base_url="https://example.test",
            targets=(
                PublicIngressTargetObservation(
                    target="base_url",
                    url="https://example.test",
                    status="fail",
                    failure_code="http_error",
                    summary="HTTP 503",
                ),
            ),
            notification_key="https://github.com/cbusillo/launchplane/issues/929",
            summary="Public ingress failed.",
        )

        self.assertTrue(notifier(record, None))
        self.assertEqual(commands[0][:4], ["gh", "issue", "comment", record.notification_key])


if __name__ == "__main__":
    unittest.main()
