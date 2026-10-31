from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from control_plane.contracts.data_provenance import DataProvenance
from control_plane.contracts.product_environment_read_model import ProductEnvironmentSummary
from control_plane.contracts.product_lane_status import ProductLaneSignal, build_product_lane_status
from control_plane.contracts.product_incident_read_model import ProductIncidentSummary


def environment(now: datetime) -> ProductEnvironmentSummary:
    proof = DataProvenance(
        source_kind="record",
        freshness_status="verified",
        stale_after=(now + timedelta(minutes=5)).isoformat(),
    ).model_dump()
    return ProductEnvironmentSummary.model_validate(
        {
            "environment": "testing",
            "context": "example-testing",
            "trust_state": "recorded",
            "provenance": proof,
            "health_monitoring": {
                "monitoring_intent": "public",
                "public_incident_eligible": True,
                "provenance": proof,
                "checks": [
                    {
                        "name": "public",
                        "kind": "public_http",
                        "enabled": True,
                        "probe_effective": True,
                        "incident_eligible": True,
                        "status": "pass",
                        "trust_state": "verified",
                        "provenance": proof,
                    }
                ],
            },
            "topology": {
                "desired": {"domains": [{"domain_name": "example.test", "tls_expected": True}]},
                "observed": {
                    "trust_state": "verified",
                    "placement": {
                        "runtime_identity_status": "match",
                        "trust_state": "verified",
                        "provenance": proof,
                    },
                    "ingress": {
                        "probe_effective": True,
                        "status": "pass",
                        "runtime_identity_status": "match",
                        "trust_state": "verified",
                        "provenance": proof,
                    },
                    "tls_domains": [
                        {
                            "domain_name": "example.test",
                            "status": "valid",
                            "trust_state": "verified",
                            "provenance": proof,
                            "public_name_match": True,
                            "validated_address_count": 1,
                            "days_remaining": 30,
                        }
                    ],
                },
            },
        }
    )


def incident() -> ProductIncidentSummary:
    return ProductIncidentSummary.model_construct(
        check_name="delivery",
        check_kind="provider",
        severity="warning",
        failure_layer="provider",
        failure_code="production_delivery_failed",
        incident_id="incident",
    )


class ProductLaneStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime.now(timezone.utc)
        self.lane = environment(self.now)

    def signal(self) -> ProductLaneSignal:
        return build_product_lane_status(self.lane, now=self.now).current

    def test_healthy_lane_and_read_model_round_trip_share_status(self) -> None:
        self.assertEqual(self.signal().tone, "verified")
        payload = self.lane.model_dump(mode="json")
        self.assertEqual(payload["lane_status"]["current"]["tone"], "verified")
        payload["lane_status"]["current"]["tone"] = "danger"
        restored = ProductEnvironmentSummary.model_validate(payload)
        assert restored.lane_status is not None
        self.assertEqual(restored.lane_status.current.tone, "verified")

    def test_health_and_identity_failures_name_every_cause(self) -> None:
        check = self.lane.health_monitoring.checks[0]
        for code in (
            "dns_failure",
            "tls_hostname_mismatch",
            "wrong_runtime_identity",
            "http_error",
        ):
            with self.subTest(code=code):
                check.status, check.failure_code = "fail", code
                signal = self.signal()
                self.assertEqual(signal.tone, "danger")
                self.assertTrue(signal.attention_reasons)
                self.assertTrue(
                    all(reason in signal.description for reason in signal.attention_reasons)
                )
        check.probe_effective = False
        self.assertNotEqual(self.signal().tone, "danger")
        self.lane.topology.observed.placement.runtime_identity_status = "mismatch"
        self.assertEqual(self.signal().tone, "danger")
        self.assertIn("Runtime identity", self.signal().description)

    def test_open_incidents_remain_red_after_a_passing_check(self) -> None:
        self.lane.health_monitoring.checks[0].incident_status = "open"
        self.assertEqual(self.signal().tone, "danger")
        self.lane.health_monitoring.checks[0].incident_status = ""
        self.lane.health_monitoring.open_incidents = (incident(),)
        self.assertEqual(self.signal().tone, "danger")
        self.lane.environment = "prod"
        self.assertEqual(self.signal().tone, "warning")
        self.assertTrue(self.signal().release_healthy)
        self.assertFalse(self.signal().attention_reasons)
        self.assertTrue(self.signal().warning_reasons)
        self.lane.health_monitoring.checks[0].status = "fail"
        self.assertEqual(self.signal().tone, "danger")
        self.assertFalse(self.signal().warning_reasons)

    def test_monitored_production_unknown_is_red(self) -> None:
        self.lane.environment = "prod"
        for state in ("unchecked", "missing", "unverifiable"):
            with self.subTest(state=state):
                self.lane.topology.observed.placement.runtime_identity_status = state
                self.assertEqual(self.signal().tone, "danger")
                self.lane.health_monitoring.monitoring_intent = "prelaunch"
                self.assertEqual(self.signal().tone, "warning")
                self.lane.health_monitoring.monitoring_intent = "public"
        self.lane.topology.observed.placement.runtime_identity_status = "match"
        self.lane.health_monitoring.checks = ()
        self.assertEqual(self.signal().tone, "danger")

    def test_recovery_failures_are_warnings_until_business_health_fails(self) -> None:
        self.lane.environment = "prod"
        recovery = self.lane.health_monitoring.checks[0].model_copy(
            update={"advisory": True, "name": "Recovery", "status": "fail"}
        )
        self.lane.health_monitoring.checks += (recovery,)
        self.assertEqual(self.signal().tone, "warning")
        self.assertTrue(self.signal().release_healthy)
        self.lane.health_monitoring.open_incidents = (incident(),)
        self.assertEqual(self.signal().tone, "warning")
        self.lane.health_monitoring.checks[0].status = "fail"
        self.assertEqual(self.signal().tone, "danger")

    def test_negative_tls_remains_red_after_expiry_and_expiring_is_yellow(self) -> None:
        domain = self.lane.topology.observed.tls_domains[0]
        for state in ("expired", "hostname_mismatch", "untrusted", "self_signed", "unreachable"):
            with self.subTest(state=state):
                domain.status = state
                domain.provenance.stale_after = (self.now - timedelta(seconds=1)).isoformat()
                self.assertEqual(self.signal().tone, "danger")
                self.assertIn("Certificate", self.signal().description)
                domain.advisory = True
                self.assertNotEqual(self.signal().tone, "danger")
                domain.advisory = False
        domain.provenance.stale_after = self.lane.provenance.stale_after
        domain.status = "expiring"
        self.assertEqual(self.signal().tone, "warning")
        self.assertNotIn("Certificate", self.signal().description)

    def test_public_green_requires_named_tls_and_public_runtime(self) -> None:
        topology = self.lane.topology
        for key, value in (
            ("validated_address_count", 0),
            ("public_name_match", False),
            ("days_remaining", None),
        ):
            domain = topology.observed.tls_domains[0]
            before = getattr(domain, key)
            setattr(domain, key, value)
            self.assertEqual(self.signal().tone, "warning")
            setattr(domain, key, before)
        topology.observed.ingress.runtime_identity_status = "unchecked"
        self.assertEqual(self.signal().tone, "warning")
        topology.observed.ingress.runtime_identity_status = "match"
        self.lane.health_monitoring.checks[0].kind = "private_http"
        self.assertEqual(self.signal().tone, "warning")
        topology.desired.public_website = "none"
        self.assertEqual(self.signal().tone, "verified")
        self.lane.health_monitoring.checks[0].status = "fail"
        self.assertEqual(self.signal().tone, "danger")

    def test_route_authority_is_diagnostic_and_other_warnings_affect_health(self) -> None:
        from control_plane.contracts.product_topology_read_model import ProductTopologyWarning

        for code in (
            "missing_route_authority",
            "stale_route_authority",
            "external_ingress_internals_unsupported",
        ):
            warning = ProductTopologyWarning(
                code=code, severity="error", scope="authority", detail="Route proof unavailable"
            )
            self.lane.topology.warnings = (warning,)
            self.lane.warnings = (warning.detail,)
            self.assertEqual(self.signal().tone, "verified")
        self.lane.warnings += ("Integration credential unverified",)
        self.assertEqual(self.signal().tone, "warning")
        self.assertIn("Integration credential unverified", self.signal().description)

    def test_expiry_transitions_match_fresh_calculation_without_mutating_input(self) -> None:
        earlier = self.now + timedelta(seconds=30)
        self.lane.topology.observed.tls_domains[0].provenance.stale_after = earlier.isoformat()
        status = build_product_lane_status(self.lane, now=self.now)
        self.assertEqual(status.current.tone, "verified")
        self.assertEqual(status.transitions[0].after, earlier.isoformat())
        self.assertEqual(status.transitions[0].signal.sidebar_tone, "stale")
        for transition in status.transitions:
            after = datetime.fromisoformat(transition.after) + timedelta(microseconds=1)
            self.assertEqual(
                transition.signal, build_product_lane_status(self.lane, now=after).current
            )
        self.assertEqual(self.lane.topology.observed.tls_domains[0].trust_state, "verified")
        self.lane.health_monitoring.checks[0].status = "fail"
        self.assertTrue(
            all(
                item.signal.tone == "danger"
                for item in build_product_lane_status(self.lane, now=self.now).transitions
            )
        )

    def test_private_unmonitored_prelaunch_keeps_recorded_state(self) -> None:
        self.lane.topology.desired.public_website = "none"
        self.lane.health_monitoring.monitoring_intent = "prelaunch"
        self.lane.health_monitoring.checks = ()
        self.assertEqual(self.signal().tone, "recorded")
        self.lane.provenance.freshness_status = "missing"
        self.assertEqual(self.signal().tone, "missing")
