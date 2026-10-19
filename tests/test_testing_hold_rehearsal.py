from contextlib import nullcontext, contextmanager
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from httpx2 import Response

from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_record import DokployTargetStaffTestingHold
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.http_routes.mutation_support import idempotency_scope
from control_plane.service_auth import (
    BearerIdentityConfig,
    LaunchplaneAuthzPolicy,
    LocalOperatorIdentity,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.testing_lane_hold import TESTING_HOLD_APPLY_ROUTE
from control_plane.workflows.generic_web_deploy_provider import generic_web_provider_target_key
from tests.support.disposable_lanes import record_safety, prepare_held_rebuild
from tests.support.http import request
from tests.support.auth import scoped_agent_transport
from tests.support.stores import sqlite_database_url
from tests.support.profiles import _odoo_profile_payload_with_prod_lane
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
    def database_url(self) -> str:
        return sqlite_database_url(self.root / "state.db")

    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = PostgresRecordStore(database_url=self.database_url())
        self.store.ensure_schema()
        self.addCleanup(self.store.close)
        self.store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(_odoo_profile_payload_with_prod_lane())
        )
        hold_fixtures._seed_target(self.store)
        hold_fixtures._seed_target(self.store, instance="prod")
        # SQLite proves HTTP behavior only, not PostgreSQL cross-connection exclusion.
        if self.store.database_dialect_name != "postgresql":
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
        policy = LaunchplaneAuthzPolicy.model_validate(
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
        )
        # Real service startup requires a DB-native policy, including an empty
        # policy proving that Director rehearsal needs no matching grant.
        active = self.store.list_authz_policy_records(status="active")
        revision = active[0].revision + 1 if active else 1
        record = LaunchplaneAuthzPolicyRecord(
            record_id=f"hold-rehearsal-fixture-policy-{revision}",
            source="fixture",
            updated_at=_TIMESTAMP,
            revision=revision,
            policy=policy,
        )
        if active:
            self.store.compare_and_write_authz_policy_record(
                expected_record=active[0], replacement_record=record
            )
        else:
            self.store.seed_authz_policy_if_absent(record)
        return create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=policy,
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
            payload={"rehearsal": True, **payload},
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

    async def test_unknown_usage_alone_allows_rehearsal_hold_and_lift(self) -> None:
        record_safety(self.store, _PRODUCT, _CONTEXT, "testing", usage="unknown")
        await self.set_hold()
        lifted = await self.reviewed(hold=False, key="unknown-lift")
        self.assertEqual(lifted.status_code, 202, lifted.text)
        self.assertTrue(lifted.json()["result"]["reconcile_requested"])
        self.assertIsNone(_read_hold(self.store))
        evidence = self.store.read_disposable_lane_safety(_PRODUCT, _CONTEXT, "testing")
        assert evidence is not None
        self.assertEqual(evidence.usage, "unknown")

    async def test_new_protection_and_unsafe_facts_block_lift_even_with_write_grant(self) -> None:
        await self.set_hold()
        original = self.store.read_disposable_lane_safety(_PRODUCT, _CONTEXT, "testing")
        assert original is not None
        self.app = self.make_app(explicit_grant=True)
        for changes in (
            {"usage": "in_use"},
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
        record_safety(self.store, _PRODUCT, _CONTEXT, "testing", data="only_copy")
        for explicit_grant in (False, True):
            with self.subTest(explicit_grant=explicit_grant):
                self.app = self.make_app(explicit_grant=explicit_grant)
                plan = await self.post(_payload())
                self.assertEqual(plan.status_code, 202, plan.text)
                denied = await self.post(
                    _payload(
                        mode="apply", reviewed_plan_sha256=plan.json()["result"]["plan_sha256"]
                    ),
                    f"needed-copy-set-{explicit_grant}",
                )
                self.assertEqual(denied.status_code, 409, denied.text)
        forged = await self.post(_payload() | {"rehearsal": {"binding_sha256": "a" * 64}})
        self.assertEqual(forged.status_code, 400, forged.text)
        self.assertIsNone(_read_hold(self.store))
        self.assertEqual(self.store.list_product_reconcile_requests(), ())

    async def test_protection_arriving_before_the_authority_lock_returns_a_refusal(self) -> None:
        self.app = self.make_app(explicit_grant=True)
        plan = await self.post(_payload())
        original_guard = self.store.serialize_disposable_testing_resources

        @contextmanager
        def changed_before_lock() -> Iterator[object]:
            record_safety(self.store, _PRODUCT, _CONTEXT, "testing", usage="in_use")
            with original_guard() as checkpoint:
                yield checkpoint

        with patch.object(
            self.store, "serialize_disposable_testing_resources", side_effect=changed_before_lock
        ):
            denied = await self.post(
                _payload(mode="apply", reviewed_plan_sha256=plan.json()["result"]["plan_sha256"]),
                "new-protection",
            )
        self.assertEqual(denied.status_code, 409, denied.text)
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

    async def test_no_original_hold_cannot_request_a_new_reconcile(self) -> None:
        denied = await self.reviewed(hold=False, key="no-original-hold")
        self.assertEqual(denied.status_code, 409, denied.text)
        self.assertIsNone(_read_hold(self.store))
        self.assertEqual(self.store.list_product_reconcile_requests(), ())

    async def test_default_hold_stays_ordinary_even_on_qualified_unused_testing(self) -> None:
        self.app = self.make_app(explicit_grant=True)
        plan = await self.post(_payload() | {"rehearsal": False})
        ordinary = await self.post(
            _payload(mode="apply", reviewed_plan_sha256=plan.json()["result"]["plan_sha256"])
            | {"rehearsal": False},
            "ordinary-staff",
        )
        self.assertEqual(ordinary.status_code, 202, ordinary.text)
        hold = _read_hold(self.store)
        assert hold is not None
        self.assertIsNone(hold.rehearsal)
        denied = await self.reviewed(hold=False, key="ordinary-cannot-lift")
        self.assertEqual(denied.status_code, 409, denied.text)
        self.assertEqual(_read_hold(self.store), hold)

    async def test_ordinary_reassertion_with_identical_reason_removes_rehearsal(self) -> None:
        await self.set_hold()
        marked = self.store.read_dokploy_target_record(
            context_name=_CONTEXT, instance_name="testing"
        )
        assert marked.policies.staff_testing_hold is not None
        for actor in ("staff-administrator", marked.policies.staff_testing_hold.recorded_by):
            with self.subTest(actor=actor):
                self.store.write_dokploy_target_record(marked)
                hold_fixtures._apply(self.store, actor=actor)
                hold = _read_hold(self.store)
                assert hold is not None
                self.assertEqual(hold.recorded_by, actor)
                self.assertIsNone(hold.rehearsal)
                denied = await self.reviewed(hold=False, key=actor)
                self.assertEqual(denied.status_code, 409, denied.text)
                self.assertEqual(_read_hold(self.store), hold)

    async def test_provider_only_alias_and_current_provider_mismatch_refuse(self) -> None:
        await self.set_hold()
        for instance, target_id in (("prod", "rehearsal-target"), ("testing", "different-target")):
            with self.subTest(instance=instance):
                if instance == "testing":
                    prior = self.store.read_provider_target_record(
                        context_name=_CONTEXT, instance_name="prod"
                    )
                    self.store.write_provider_target_record(
                        prior.model_copy(update={"target_id": "unrelated-production-target"})
                    )
                self.store.write_provider_target_record(
                    ProviderTargetRecord(
                        context=_CONTEXT,
                        instance=instance,
                        provider_id="dokploy",
                        target_category="compose",
                        provider_target_type="compose",
                        target_id=target_id,
                        display_name="Fixture recipient",
                        updated_at=_TIMESTAMP,
                    )
                )
                denied = await self.reviewed(hold=False, key=instance)
                self.assertEqual(denied.status_code, 409, denied.text)
                self.assertIsNotNone(_read_hold(self.store))
                self.assertEqual(self.store.list_product_reconcile_requests(), ())

    async def test_failure_before_atomic_commit_preserves_hold_and_retries_original_key(
        self,
    ) -> None:
        await self.set_hold()
        before = _read_hold(self.store)
        plan = await self.post(_payload(hold=False))
        payload = _payload(
            hold=False, mode="apply", reviewed_plan_sha256=plan.json()["result"]["plan_sha256"]
        )
        with patch.object(
            self.store,
            "_fold_product_reconcile_request",
            side_effect=RuntimeError("fixture DB failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "fixture DB failure"):
                await self.post(payload, "atomic-lift")
        self.assertEqual(_read_hold(self.store), before)
        self.assertEqual(self.store.list_product_reconcile_requests(), ())
        self.assertIsNone(
            self.store.read_idempotency_record(
                scope=idempotency_scope(
                    LocalOperatorIdentity(subject="director-fixture", token_label="fixture")
                ),
                route_path=TESTING_HOLD_APPLY_ROUTE,
                idempotency_key="atomic-lift",
            )
        )
        retried = await self.post(payload, "atomic-lift")
        self.assertEqual(retried.status_code, 202, retried.text)
        self.assertTrue(retried.json()["result"]["reconcile_requested"])

    async def test_lost_response_after_atomic_commit_replays_after_safety_expiry(self) -> None:
        await self.set_hold()
        plan = await self.post(_payload(hold=False))
        payload = _payload(
            hold=False, mode="apply", reviewed_plan_sha256=plan.json()["result"]["plan_sha256"]
        )
        original = self.store.commit_testing_hold

        def lost_response(**kwargs: object) -> object:
            original(**kwargs)  # type: ignore[arg-type]
            raise RuntimeError("fixture lost response")

        with patch.object(self.store, "commit_testing_hold", side_effect=lost_response):
            with self.assertRaisesRegex(RuntimeError, "fixture lost response"):
                await self.post(payload, "lost-receipt")
        self.assertIsNone(_read_hold(self.store))
        queued = self.store.list_product_reconcile_requests()
        self.assertTrue(queued)
        evidence = self.store.read_disposable_lane_safety(_PRODUCT, _CONTEXT, "testing")
        assert evidence is not None
        self.store.write_disposable_lane_safety(
            evidence.model_copy(
                update={
                    "observed_at": evidence.observed_at - timedelta(hours=1),
                    "expires_at": evidence.expires_at - timedelta(hours=1),
                }
            )
        )
        replayed = await self.post(payload, "lost-receipt")
        self.assertEqual(replayed.status_code, 202, replayed.text)
        self.assertTrue(replayed.json()["replayed"])
        self.assertTrue(replayed.json()["result"]["reconcile_requested"])
        self.assertEqual(self.store.list_product_reconcile_requests(), queued)
