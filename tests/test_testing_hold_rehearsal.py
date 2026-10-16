from contextlib import nullcontext
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from httpx2 import Response

from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetStaffTestingHold
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import BearerIdentityConfig, LaunchplaneAuthzPolicy
from control_plane.testing_lane_hold import TESTING_HOLD_APPLY_ROUTE
from control_plane.workflows.generic_web_deploy_provider import generic_web_provider_target_key
from tests.support.disposable_lanes import record_safety, prepare_held_rebuild
from tests.support.http import request
from tests.support.auth import scoped_agent_transport
from tests.http_app_test_support import _RejectingVerifier
from tests import test_testing_lane_hold as hold_fixtures
from tests.test_testing_lane_hold import (
    _PRODUCT,
    _CONTEXT,
    _TIMESTAMP,
    _payload,
    _read_hold,
)


class DirectorHoldRehearsalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = hold_fixtures.TestingHoldRouteTests._store(self, self.root)  # type: ignore[arg-type]
        # SQLite proves HTTP behavior only, not PostgreSQL cross-connection exclusion.
        guard = patch.object(
            self.store, "serialize_disposable_testing_resources", side_effect=nullcontext
        )
        guard.start()
        self.addCleanup(guard.stop)
        self.store.write_dokploy_target_id_record(
            DokployTargetIdRecord(
                context=_CONTEXT,
                instance="testing",
                target_id="rehearsal-target",
                updated_at=_TIMESTAMP,
            )
        )
        record_safety(self.store, _PRODUCT, _CONTEXT, "testing")
        self.app = self.make_app()

    def make_app(self, *, explicit_grant: bool = False) -> FastAPI:
        return create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=LaunchplaneAuthzPolicy.model_validate(
                {
                    "schema_version": 2,
                    "local_operators": [
                        {
                            "subjects": ["director-fixture"],
                            "token_labels": ["fixture"],
                            "products": [_PRODUCT],
                            "contexts": [_CONTEXT],
                            "instances": ["testing"],
                            "actions": ["product_config.apply"],
                        }
                    ],
                }
                if explicit_grant
                else {}
            ),
            record_store_factory=lambda: self.store,
            control_plane_root_path=self.root,
            bearer_identity_config=BearerIdentityConfig(
                local_operator_token="fixture-token",
                local_operator_subject="director-fixture",
                local_operator_token_label="fixture",
            ),
        )

    async def post(self, payload: dict[str, object], key: str = "") -> Response:
        return await request(
            self.app,
            "POST",
            TESTING_HOLD_APPLY_ROUTE,
            payload=payload,
            headers={
                "Authorization": "Bearer fixture-token",
                "Idempotency-Key": key,
            },
        )

    async def reviewed(self, *, hold: bool, key: str) -> Response:
        plan = await self.post(_payload(hold=hold))
        self.assertEqual(plan.status_code, 202, plan.text)
        return await self.post(
            _payload(
                hold=hold,
                mode="apply",
                reviewed_plan_sha256=plan.json()["result"]["plan_sha256"],
            ),
            key,
        )

    async def set_hold(self) -> None:
        response = await self.reviewed(hold=True, key="set")
        self.assertEqual(response.status_code, 202, response.text)
        self.assertTrue(response.json()["result"]["read_back_matches"])

    async def test_qualified_hold_and_lift_without_grants_requests_reconcile_and_replays(
        self,
    ) -> None:
        previous_requests = self.store.list_product_reconcile_requests()
        for driver in ("odoo", "generic-web"):
            with self.subTest(driver=driver):
                profile = self.store.read_product_profile_record(_PRODUCT)
                self.store.write_product_profile_record(
                    profile.model_copy(update={"driver_id": driver})
                )
                record_safety(self.store, _PRODUCT, _CONTEXT, "testing")
                plan = await self.post(_payload())
                self.assertEqual(plan.status_code, 202, plan.text)
                self.assertIsNone(_read_hold(self.store))
                self.assertFalse(plan.json()["result"]["applied"])
                self.assertFalse(plan.json()["result"]["reconcile_requested"])
                self.assertEqual(
                    self.store.list_product_reconcile_requests(),
                    previous_requests,
                )
                set_response = await self.post(
                    _payload(
                        mode="apply", reviewed_plan_sha256=plan.json()["result"]["plan_sha256"]
                    ),
                    f"set-{driver}",
                )
                self.assertEqual(set_response.status_code, 202, set_response.text)
                stored = _read_hold(self.store)
                assert stored is not None
                self.assertEqual(stored.purpose, "staff_testing")
                self.assertIsNotNone(stored.rehearsal)
                lift = await self.post(_payload(hold=False))
                self.assertIsNotNone(_read_hold(self.store))
                self.assertFalse(lift.json()["result"]["reconcile_requested"])
                payload = _payload(
                    hold=False,
                    mode="apply",
                    reviewed_plan_sha256=lift.json()["result"]["plan_sha256"],
                )
                applied = await self.post(payload, f"lift-{driver}")
                self.assertEqual(applied.status_code, 202, applied.text)
                self.assertTrue(applied.json()["result"]["reconcile_requested"])
                self.assertIsNone(_read_hold(self.store))
                replay = await self.post(payload, f"lift-{driver}")
                self.assertEqual(replay.status_code, 202, replay.text)
                self.assertEqual(replay.json()["result"], applied.json()["result"])
                previous_requests = self.store.list_product_reconcile_requests()
                self.assertEqual([r.product for r in previous_requests], [_PRODUCT])

    async def test_lift_preserves_actual_foreign_and_staging_holds(self) -> None:
        for purpose, actor in (
            ("staff_testing", "director-fixture"),
            ("staff_testing", "staff"),
            ("isolation_staging", "director-fixture"),
        ):
            with self.subTest(purpose=purpose, actor=actor):
                target = self.store.read_dokploy_target_record(
                    context_name=_CONTEXT, instance_name="testing"
                )
                hold = DokployTargetStaffTestingHold.model_validate(
                    {"reason": "Actual use", "purpose": purpose, "recorded_by": actor}
                )
                self.store.write_dokploy_target_record(
                    target.model_copy(
                        update={
                            "policies": target.policies.model_copy(
                                update={"staff_testing_hold": hold}
                            )
                        }
                    )
                )
                record_safety(self.store, _PRODUCT, _CONTEXT, "testing")
                if purpose == "staff_testing":
                    result = await self.reviewed(hold=False, key=f"lift-{actor}")
                    self.assertEqual(result.status_code, 409, result.text)
                else:
                    result = await self.post(_payload(hold=False))
                    self.assertEqual(result.status_code, 409, result.text)
                self.assertEqual(_read_hold(self.store), hold)
                self.assertEqual(self.store.list_product_reconcile_requests(), ())

    async def test_qualified_lift_with_existing_write_authority(self) -> None:
        self.app = self.make_app(explicit_grant=True)
        await self.set_hold()
        lifted = await self.reviewed(hold=False, key="ordinary-lift")
        self.assertEqual(lifted.status_code, 202, lifted.text)
        self.assertTrue(lifted.json()["result"]["reconcile_requested"])
        self.assertIsNone(_read_hold(self.store))

    async def test_new_protection_and_unknown_facts_block_lift_even_with_write_grant(self) -> None:
        await self.set_hold()
        original = self.store.read_disposable_lane_safety(_PRODUCT, _CONTEXT, "testing")
        assert original is not None
        self.app = self.make_app(explicit_grant=True)
        for changes in (
            {"usage": "in_use"},
            {"usage": "unknown"},
            {"data": "only_copy"},
            {"data": "unknown"},
            {"production_resources": "shared"},
            {"production_resources": "separation_ready"},
            {"production_resources": "unknown"},
            {
                "observed_at": original.observed_at - timedelta(hours=1),
                "expires_at": original.expires_at - timedelta(hours=1),
            },
        ):
            with self.subTest(changes=changes):
                self.store.write_disposable_lane_safety(original.model_copy(update=changes))
                result = await self.reviewed(hold=False, key=str(changes))
                self.assertEqual(result.status_code, 409, result.text)
                self.assertIsNotNone(_read_hold(self.store))
                self.assertEqual(self.store.list_product_reconcile_requests(), ())

    async def test_drift_after_lift_review_preserves_hold(self) -> None:
        await self.set_hold()
        plan = await self.post(_payload(hold=False))
        target = self.store.read_dokploy_target_record(
            context_name=_CONTEXT, instance_name="testing"
        )
        self.store.write_dokploy_target_record(
            target.model_copy(update={"domains": ("changed.example.test",)})
        )
        result = await self.post(
            _payload(
                hold=False, mode="apply", reviewed_plan_sha256=plan.json()["result"]["plan_sha256"]
            ),
            "drift",
        )
        self.assertEqual(result.status_code, 409, result.text)
        self.assertIsNotNone(_read_hold(self.store))
        self.assertEqual(self.store.list_product_reconcile_requests(), ())

    async def test_unresolved_odoo_operation_preserves_hold(self) -> None:
        await self.set_hold()
        prepare_held_rebuild(self.store, _PRODUCT, _CONTEXT, prepare=False)
        result = await self.reviewed(hold=False, key="unknown-odoo")
        self.assertEqual(result.status_code, 409, result.text)
        self.assertIsNotNone(_read_hold(self.store))
        self.assertEqual(self.store.list_product_reconcile_requests(), ())

    async def test_unresolved_provider_reservation_preserves_hold(self) -> None:
        await self.set_hold()
        key = generic_web_provider_target_key(
            provider_id="dokploy",
            context=_CONTEXT,
            instance="testing",
            target_type="compose",
            target_id="rehearsal-target",
        )
        reserved = self.store.reserve_mutation(
            scope="original-caller",
            route_path="/original-operation",
            idempotency_key="original-key",
            request_fingerprint="original-request",
            provider_target_key=key,
            reconciliation_key="original-binding",
            lease_owner="original-worker",
        )
        assert reserved.record is not None
        changed = self.store.mark_mutation_reconcile_required(
            reservation=reserved.record,
            reconciliation_key="original-binding",
        )
        reservation = changed.record
        assert reservation is not None
        result = await self.reviewed(hold=False, key="unknown-provider")
        self.assertEqual(result.status_code, 409, result.text)
        self.assertEqual(
            self.store.read_idempotency_record(
                scope=reservation.scope,
                route_path=reservation.route_path,
                idempotency_key=reservation.idempotency_key,
            ),
            reservation,
        )
        self.assertIsNotNone(_read_hold(self.store))
        self.assertEqual(self.store.list_product_reconcile_requests(), ())

    async def test_foreign_rehearsal_actor_and_shared_target_cannot_lift(self) -> None:
        await self.set_hold()
        target = self.store.read_dokploy_target_record(
            context_name=_CONTEXT, instance_name="testing"
        )
        hold = target.policies.staff_testing_hold
        assert hold is not None
        self.store.write_dokploy_target_record(
            target.model_copy(
                update={
                    "policies": target.policies.model_copy(
                        update={
                            "staff_testing_hold": hold.model_copy(
                                update={"recorded_by": "other-director"}
                            )
                        }
                    ),
                }
            )
        )
        denied = await self.reviewed(hold=False, key="foreign")
        self.assertEqual(denied.status_code, 409, denied.text)
        self.store.write_dokploy_target_record(target)
        self.store.write_dokploy_target_id_record(
            DokployTargetIdRecord(
                context=_CONTEXT,
                instance="prod",
                target_id="rehearsal-target",
                updated_at=_TIMESTAMP,
            )
        )
        denied = await self.reviewed(hold=False, key="shared")
        self.assertEqual(denied.status_code, 409, denied.text)
        self.assertIsNotNone(_read_hold(self.store))
        self.assertEqual(self.store.list_product_reconcile_requests(), ())

    async def test_scoped_identity_cannot_inherit_rehearsal_permission(self) -> None:
        await self.set_hold()
        plan = await self.post(_payload(hold=False))
        with scoped_agent_transport():
            denied = await self.post(
                _payload(
                    hold=False,
                    mode="apply",
                    reviewed_plan_sha256=plan.json()["result"]["plan_sha256"],
                ),
                "scoped",
            )
        self.assertEqual(denied.status_code, 403, denied.text)
        self.assertIsNotNone(_read_hold(self.store))
        self.assertEqual(self.store.list_product_reconcile_requests(), ())

    async def test_unqualified_set_cannot_fabricate_rehearsal_provenance(self) -> None:
        record_safety(self.store, _PRODUCT, _CONTEXT, "testing", usage="unknown")
        plan = await self.post(_payload())
        self.assertEqual(plan.status_code, 202, plan.text)
        denied = await self.post(
            _payload(mode="apply", reviewed_plan_sha256=plan.json()["result"]["plan_sha256"]),
            "unknown-set",
        )
        self.assertEqual(denied.status_code, 409, denied.text)
        forged = await self.post(_payload() | {"rehearsal": {"binding_sha256": "a" * 64}})
        self.assertEqual(forged.status_code, 400, forged.text)
        self.assertIsNone(_read_hold(self.store))
        self.assertEqual(self.store.list_product_reconcile_requests(), ())

    async def test_production_and_unreviewed_lift_refuse(self) -> None:
        result = await self.post(
            _payload(instance="prod", mode="apply", reviewed_plan_sha256="f" * 64), "prod"
        )
        self.assertNotEqual(result.status_code, 202, result.text)
        await self.set_hold()
        result = await self.post(
            _payload(hold=False, mode="apply", reviewed_plan_sha256="f" * 64), "unreviewed"
        )
        self.assertEqual(result.status_code, 409, result.text)
        self.assertIsNotNone(_read_hold(self.store))
        self.assertEqual(self.store.list_product_reconcile_requests(), ())
