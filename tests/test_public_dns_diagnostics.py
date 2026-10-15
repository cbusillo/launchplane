from datetime import datetime, timedelta, timezone
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneHealthCheck,
)
from control_plane.contracts.public_dns import PublicDnsExpectedTarget
from control_plane.contracts.public_dns import PublicDnsObservation
from control_plane.contracts.public_ingress_monitoring import health_monitor_interval_seconds
from control_plane.contracts.product_topology_read_model import build_product_environment_topology
from control_plane.product_health_monitoring import (
    ProductHealthMonitoringApplyRequest,
    build_product_health_monitoring_plan,
    updated_product_health_monitoring_profile,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.public_ingress_monitor import (
    HttpObservation,
    run_public_ingress_monitor_once,
)
from tests.support.stores import _sqlite_database_url
from tests.test_public_ingress_monitor import _profile, _Store


_NOW = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
_EXPECTED = PublicDnsExpectedTarget(public_name="example.test", addresses=("8.8.8.8",))


def _dns_profile() -> LaunchplaneProductProfileRecord:
    payload = _profile().model_dump(mode="json")
    payload["lanes"][0]["health_monitoring"]["checks"][0]["expected_dns_targets"] = [
        _EXPECTED.model_dump(mode="json")
    ]
    return LaunchplaneProductProfileRecord.model_validate(payload)


class PublicDnsDiagnosticTests(unittest.TestCase):
    def test_a_second_wrong_answer_for_the_same_host_is_not_hidden(self) -> None:
        profile = _dns_profile()
        store = _Store((profile,))
        answers = iter([_EXPECTED.addresses, ("1.1.1.1",)])
        run_public_ingress_monitor_once(
            record_store=store,
            checked_at=_NOW.isoformat(),
            notify=False,
            http_get=lambda url, timeout: HttpObservation(200, url, 0),
            dns_get=lambda url: next(answers),
        )
        topology = build_product_environment_topology(
            record_store=store,
            profile=profile,
            lane=profile.lanes[0],
            lane_summary=None,
            now=_NOW,
        )
        self.assertEqual(topology.observed.dns_targets[0].status, "mismatch")
        self.assertEqual(topology.observed.ingress.status, "pass")

    def test_stored_evidence_cannot_claim_a_match_for_wrong_answers(self) -> None:
        with self.assertRaisesRegex(ValidationError, "disagrees"):
            PublicDnsObservation(
                public_name=_EXPECTED.public_name,
                status="match",
                expected_addresses=_EXPECTED.addresses,
                observed_addresses=("1.1.1.1",),
            )

    def test_probe_persistence_and_topology_separate_dns_from_http_health(self) -> None:
        for addresses, expected_status in (
            (("8.8.8.8",), "match"),
            (("1.1.1.1",), "mismatch"),
            (("8.8.8.8", "1.1.1.1"), "mismatch"),
            ((), "unavailable"),
        ):
            with self.subTest(addresses=addresses), TemporaryDirectory() as directory:
                profile = _dns_profile()
                store = PostgresRecordStore(
                    database_url=_sqlite_database_url(Path(directory) / "db")
                )
                store.ensure_schema()
                store.write_product_profile_record(profile)
                result = run_public_ingress_monitor_once(
                    record_store=store,
                    checked_at=_NOW.isoformat(),
                    notify=False,
                    http_get=lambda url, timeout: HttpObservation(200, url, 0),
                    dns_get=lambda url: addresses,
                )
                self.assertTrue(all(record.status == "pass" for record in result.records))
                self.assertFalse(result.incidents)
                persisted = store.list_public_ingress_observation_records(product=profile.product)
                self.assertEqual(persisted[0].targets[0].dns.status, expected_status)  # type: ignore[union-attr]
                topology = build_product_environment_topology(
                    record_store=store,
                    profile=profile,
                    lane=profile.lanes[0],
                    lane_summary=None,
                    now=_NOW,
                )
                self.assertEqual(topology.observed.dns_targets[0].status, expected_status)
                self.assertEqual(topology.observed.ingress.status, "pass")
                store.close()

    def test_real_resolution_rejects_private_mixed_answers_and_reports_failed_resolution(
        self,
    ) -> None:
        for answers, failure in (
            (((socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 443)),), ""),
            (
                (
                    (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 443)),
                    (
                        socket.AF_INET,
                        socket.SOCK_STREAM,
                        socket.IPPROTO_TCP,
                        "",
                        ("127.0.0.1", 443),
                    ),
                ),
                "private_url",
            ),
            ((), "dns_failure"),
        ):
            with self.subTest(failure=failure), patch("socket.getaddrinfo", return_value=answers):
                result = run_public_ingress_monitor_once(
                    record_store=_Store((_dns_profile(),)),
                    checked_at=_NOW.isoformat(),
                    notify=False,
                    http_get=lambda url, timeout: HttpObservation(200, url, 0),
                )
                dns = result.records[0].targets[0].dns
                self.assertIsNotNone(dns)
                assert dns is not None
                self.assertEqual(dns.failure_code, failure)
                self.assertEqual(dns.status, "unavailable" if failure else "match")
        with patch("socket.getaddrinfo", side_effect=socket.gaierror("fixture DNS failed")):
            result = run_public_ingress_monitor_once(
                record_store=_Store((_dns_profile(),)),
                checked_at=_NOW.isoformat(),
                notify=False,
                http_get=lambda url, timeout: HttpObservation(200, url, 0),
            )
        self.assertEqual(result.records[0].targets[0].dns.failure_code, "dns_failure")  # type: ignore[union-attr]

    def test_missing_expectations_do_not_probe_or_infer_from_route_or_runtime(self) -> None:
        profile = _profile()
        store = _Store((profile,))
        with patch("socket.getaddrinfo", side_effect=AssertionError("unexpected resolution")):
            run_public_ingress_monitor_once(
                record_store=store,
                checked_at=_NOW.isoformat(),
                notify=False,
                http_get=lambda url, timeout: HttpObservation(200, url, 0),
            )
        topology = build_product_environment_topology(
            record_store=store,
            profile=profile,
            lane=profile.lanes[0],
            lane_summary=None,
            now=_NOW,
        )
        diagnostic = topology.observed.dns_targets[0]
        self.assertEqual(diagnostic.status, "missing_expectation")
        self.assertEqual(diagnostic.provenance.freshness_status, "missing")
        self.assertFalse(diagnostic.observed_addresses)
        self.assertNotIn("dns", store.records[0].targets[0].model_dump(mode="json"))

    def test_deadline_and_changed_expectations_invalidate_prior_match(self) -> None:
        profile = _dns_profile()
        store = _Store((profile,))
        run_public_ingress_monitor_once(
            record_store=store,
            checked_at=_NOW.isoformat(),
            notify=False,
            http_get=lambda url, timeout: HttpObservation(200, url, 0),
            dns_get=lambda url: _EXPECTED.addresses,
        )
        deadline = _NOW + timedelta(
            seconds=health_monitor_interval_seconds(profile.lanes[0].instance)
        )
        changed = profile.model_dump(mode="json")
        changed["lanes"][0]["health_monitoring"]["checks"][0]["expected_dns_targets"][0][
            "addresses"
        ] = ["1.1.1.1"]
        changed_profile = LaunchplaneProductProfileRecord.model_validate(changed)
        for current, time, status in (
            (profile, deadline - timedelta(microseconds=1), "match"),
            (profile, deadline, "stale"),
            (profile, _NOW - timedelta(seconds=1), "stale"),
            (changed_profile, _NOW, "stale"),
        ):
            with self.subTest(status=status, time=time):
                topology = build_product_environment_topology(
                    record_store=store,
                    profile=current,
                    lane=current.lanes[0],
                    lane_summary=None,
                    now=time,
                )
                self.assertEqual(topology.observed.dns_targets[0].status, status)

    def test_expectations_validate_scope_and_public_addresses(self) -> None:
        for name, addresses in (
            ("example.test", ("127.0.0.1",)),
            ("example.test", ("10.0.0.1",)),
            ("example.test", ("::ffff:127.0.0.1",)),
            ("example.test", ("224.0.0.1",)),
            ("example.test", ()),
            ("*.example.test", ("8.8.8.8",)),
            ("8.8.8.8", ("8.8.8.8",)),
        ):
            with self.subTest(name=name, addresses=addresses), self.assertRaises(ValidationError):
                PublicDnsExpectedTarget(public_name=name, addresses=addresses)
        with self.assertRaises(ValidationError):
            ProductLaneHealthCheck(
                name="private", kind="private_http", expected_dns_targets=(_EXPECTED,)
            )
        with self.assertRaises(ValidationError):
            ProductLaneHealthCheck(name="public", expected_dns_targets=(_EXPECTED, _EXPECTED))

    def test_review_plan_binds_expectations_and_omission_preserves_them(self) -> None:
        profile = _dns_profile()
        payload = dict(
            product=profile.product,
            context=profile.lanes[0].context,
            instance="prod",
            check_name="public-ingress",
            monitoring_intent="public",
            reason="DNS comparison",
        )
        omitted = ProductHealthMonitoringApplyRequest.model_validate(payload)
        plan = build_product_health_monitoring_plan(profile=profile, request=omitted)
        self.assertEqual(plan.requested_expected_dns_targets, (_EXPECTED,))
        clear = ProductHealthMonitoringApplyRequest.model_validate(
            {**payload, "expected_dns_targets": []}
        )
        cleared_plan = build_product_health_monitoring_plan(profile=profile, request=clear)
        self.assertNotEqual(plan.plan_sha256, cleared_plan.plan_sha256)
        saved = updated_product_health_monitoring_profile(
            profile=profile, request=omitted, updated_at=_NOW.isoformat()
        )
        self.assertEqual(
            saved.lanes[0].health_monitoring.checks[0].expected_dns_targets, (_EXPECTED,)
        )
        cleared = updated_product_health_monitoring_profile(
            profile=profile, request=clear, updated_at=_NOW.isoformat()
        )
        self.assertFalse(cleared.lanes[0].health_monitoring.checks[0].expected_dns_targets)
        wrong_scope = PublicDnsExpectedTarget(public_name="other.test", addresses=("8.8.8.8",))
        request = ProductHealthMonitoringApplyRequest.model_validate(
            {
                **payload,
                "expected_dns_targets": [wrong_scope.model_dump(mode="json")],
            }
        )
        with self.assertRaisesRegex(ValueError, "host probed"):
            build_product_health_monitoring_plan(profile=profile, request=request)
