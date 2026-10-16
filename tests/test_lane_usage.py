from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from control_plane.contracts.dokploy_target_record import (
    DokployTargetRecord,
    DokployTargetStaffTestingHold,
)
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.product_profile_record import ProductLaneHealthCheck
from control_plane.contracts.private_health_endpoint_record import PrivateHealthEndpointRecord
from control_plane.disposable_lanes import require_disposable_lane, DisposableLaneSafetyUnknown
from control_plane.lane_usage import LaneUsageDecision, evaluate_lane_usage
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.disposable_lanes import record_safety, prepare_held_rebuild
from tests.support.down_lanes import record_lane_health
from tests.support.profiles import (
    _generic_site_profile_payload,
    _odoo_profile_payload_with_prod_lane,
)


class LaneUsageTests(unittest.TestCase):
    database_url = ""

    def setUp(self) -> None:
        root = Path(self.enterContext(TemporaryDirectory()))
        self.store = PostgresRecordStore(
            database_url=self.database_url or f"sqlite+pysqlite:///{root / 'state.db'}"
        )
        self.store.ensure_schema()
        self.addCleanup(self.store.close)
        self.now = datetime.now(timezone.utc)
        self.product = "example-site"
        self.seed(_generic_site_profile_payload())

    def seed(self, payload: dict[str, object]) -> None:
        profile = LaunchplaneProductProfileRecord.model_validate(payload)
        self.product = profile.product
        self.store.write_product_profile_record(profile)
        self.context = next(lane.context for lane in profile.lanes if lane.instance == "testing")
        for lane in profile.lanes:
            self.store.write_dokploy_target_record(
                DokployTargetRecord(
                    context=lane.context,
                    instance=lane.instance,
                    updated_at=profile.updated_at,
                )
            )
            self.store.write_dokploy_target_id_record(
                DokployTargetIdRecord(
                    context=lane.context,
                    instance=lane.instance,
                    updated_at=profile.updated_at,
                    target_id=f"{profile.product}-{lane.instance}",
                )
            )

    def decision(self, usage: str = "unknown", instance: str = "testing") -> LaneUsageDecision:
        return evaluate_lane_usage(
            self.store,
            profile=self.store.read_product_profile_record(self.product),
            target=self.store.read_dokploy_target_record(
                context_name=self.context, instance_name=instance
            ),
            target_id=self.store.read_dokploy_target_id_record(
                context_name=self.context, instance_name=instance
            ),
            instance=instance,
            usage=usage,
            now=self.now,
        )

    def health(self, *, status: int = 503, instance: str = "testing") -> tuple[str, ...]:
        return record_lane_health(
            self.store, self.product, self.context, self.now, status=status, instance=instance
        )

    def require(self) -> str:
        return require_disposable_lane(
            self.store, product=self.product, context=self.context, instance="testing", now=self.now
        )

    def test_down_without_hold_allows_unknown_and_historical_automated_use_across_drivers(
        self,
    ) -> None:
        for payload in (
            _generic_site_profile_payload(),
            _odoo_profile_payload_with_prod_lane("example-odoo"),
        ):
            with self.subTest(driver=payload["driver_id"]):
                self.seed(payload)
                ids = self.health()
                for usage in ("unknown", "in_use"):
                    decision = self.decision(usage)
                    self.assertFalse(decision.protected)
                    self.assertEqual(set(decision.observation_ids), set(ids))
                    self.assertLess(
                        datetime.fromisoformat(decision.down_since),
                        datetime.fromisoformat(decision.down_until),
                    )

    def test_current_staff_hold_always_blocks_down_lane(self) -> None:
        self.health()
        target = self.store.read_dokploy_target_record(
            context_name=self.context, instance_name="testing"
        )
        hold = DokployTargetStaffTestingHold(
            reason="Staff checking work", recorded_by="staff", recorded_at=self.now.isoformat()
        )
        self.store.write_dokploy_target_record(
            target.model_copy(
                update={"policies": target.policies.model_copy(update={"staff_testing_hold": hold})}
            )
        )
        self.assertTrue(self.decision().protected)

    def test_serving_unknown_and_production_remain_protected(self) -> None:
        self.health(status=200)
        self.assertTrue(self.decision().protected)
        self.health(instance="prod")
        self.assertTrue(self.decision(instance="prod").protected)
        self.assertFalse(self.decision("unused", instance="prod").protected)

    def test_access_errors_do_not_prove_a_lane_unavailable_to_its_users(self) -> None:
        for status in (401, 403, 404):
            with self.subTest(status=status):
                self.health(status=status)
                self.assertTrue(self.decision().protected)

    def test_new_success_cancels_the_down_qualification_immediately(self) -> None:
        self.health()
        self.assertFalse(self.decision().protected)
        self.now += timedelta(seconds=1)
        self.health(status=200)
        self.assertTrue(self.decision().protected)

    def test_foreign_url_or_single_failure_cannot_qualify_downtime(self) -> None:
        self.health()
        records = self.store.list_public_ingress_observation_records(product=self.product)
        latest = records[0]
        # Two records at one instant are not an interval.
        self.store.write_public_ingress_observation_record(
            records[-1].model_copy(
                update={
                    "observed_at": latest.observed_at,
                }
            )
        )
        self.assertTrue(self.decision().protected)
        self.health()
        for record in self.store.list_public_ingress_observation_records(product=self.product):
            self.store.write_public_ingress_observation_record(
                record.model_copy(
                    update={
                        "health_url": "https://foreign.example.com/healthz",
                    }
                )
            )
        self.assertTrue(self.decision().protected)

    def test_missing_stale_single_probe_or_changed_recipient_cannot_qualify_down(self) -> None:
        self.assertTrue(self.decision().protected)
        self.health()
        self.now += timedelta(hours=1)
        self.assertTrue(self.decision().protected)
        self.health()
        target = self.store.read_dokploy_target_id_record(
            context_name=self.context, instance_name="testing"
        )
        self.store.write_dokploy_target_id_record(
            target.model_copy(
                update={
                    "target_id": "replacement",
                    "updated_at": self.now.isoformat(),
                }
            )
        )
        self.assertTrue(self.decision().protected)

    def test_real_disposable_guard_allows_down_unknown_but_keeps_custody_and_isolation(
        self,
    ) -> None:
        self.health()
        record_safety(
            self.store,
            self.product,
            self.context,
            "testing",
            usage="unknown",
            observed_at=self.now,
            expires_at=self.now + timedelta(minutes=5),
        )
        self.assertTrue(self.require())
        for facts in (
            {"data": "only_copy"},
            {"data": "unknown"},
            {"production_resources": "shared"},
        ):
            with self.subTest(facts=facts):
                record_safety(
                    self.store,
                    self.product,
                    self.context,
                    "testing",
                    usage="unknown",
                    observed_at=self.now,
                    expires_at=self.now + timedelta(minutes=5),
                    **facts,
                )
                with self.assertRaises(DisposableLaneSafetyUnknown):
                    self.require()

    def test_locked_successor_adapter_consumes_the_service_downtime_rule(self) -> None:
        operation = prepare_held_rebuild(self.store, self.product, self.context)
        target_id = self.store.read_dokploy_target_id_record(
            context_name=self.context, instance_name="testing"
        )
        self.store.write_dokploy_target_id_record(
            target_id.model_copy(
                update={
                    "target_id": "isolated-successor",
                    "updated_at": (self.now - timedelta(hours=1)).isoformat(),
                }
            )
        )
        self.health()
        record_safety(
            self.store,
            self.product,
            self.context,
            "testing",
            usage="unknown",
            replaces_operation_id=operation.operation_id,
            replaced_target_id=target_id.target_id,
            observed_at=self.now,
            expires_at=self.now + timedelta(minutes=5),
        )
        self.assertTrue(self.store.supersede_disposable_testing_replacement(operation))
        superseded = self.store.read_odoo_stable_target_replacement_operation_record(
            operation.operation_id
        )
        self.assertEqual(superseded.status, "superseded")
        self.assertIsNone(superseded.recovery_journal)
        self.assertIsNotNone(superseded.disposable_supersession)

    def test_successful_target_inside_failed_aggregate_does_not_mean_down(self) -> None:
        self.health()
        records = self.store.list_public_ingress_observation_records(product=self.product)
        for record in records:
            probe = record.targets[0].model_copy(
                update={"status": "pass", "failure_code": None, "http_status": 200}
            )
            self.store.write_public_ingress_observation_record(
                record.model_copy(update={"targets": (probe, *record.targets[1:])})
            )
        self.assertTrue(self.decision().protected)

    def test_private_serving_or_unresolved_endpoint_protects_a_publicly_down_lane(self) -> None:
        self.health()
        profile = self.store.read_product_profile_record(self.product)
        lane = next(lane for lane in profile.lanes if lane.instance == "testing")
        private = ProductLaneHealthCheck(
            name="private-health", kind="private_http", private_endpoint_key="fixture-private"
        )
        replacement = lane.model_copy(
            update={
                "health_monitoring": lane.health_monitoring.model_copy(
                    update={"checks": (*lane.health_monitoring.checks, private)}
                )
            }
        )
        self.store.write_product_profile_record(
            profile.model_copy(
                update={
                    "lanes": tuple(replacement if item == lane else item for item in profile.lanes)
                }
            )
        )
        self.assertTrue(self.decision().protected)
        self.store.write_private_health_endpoint_record(
            PrivateHealthEndpointRecord(
                endpoint_key=private.private_endpoint_key,
                product=self.product,
                context=self.context,
                instance="testing",
                url="http://10.20.30.40:8069/web/health",
                updated_at=profile.updated_at,
            )
        )
        record_lane_health(self.store, self.product, self.context, self.now, configure=False)
        self.assertFalse(self.decision().protected)
        for record in self.store.list_public_ingress_observation_records(
            product=self.product, check_kind="private_http"
        ):
            self.store.write_public_ingress_observation_record(
                record.model_copy(
                    update={
                        "status": "pass",
                        "failure_code": None,
                        "targets": tuple(
                            probe.model_copy(
                                update={"status": "pass", "failure_code": None, "http_status": 200}
                            )
                            for probe in record.targets
                        ),
                    }
                )
            )
        self.assertTrue(self.decision().protected)
