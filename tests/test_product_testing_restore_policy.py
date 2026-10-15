from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import patch

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.http_routes.product_testing_restore_policy import ROUTE
from control_plane.product_testing_restore_policy import (
    TestingRestorePolicyRequest,
    build_testing_restore_policy_plan,
)
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.storage.postgres import PostgresRecordStore, ProductProfileCompareWriteResult
from tests.http_app_test_support import _local_operator_bearer_config, _RejectingVerifier
from tests.support.auth import scoped_agent_transport
from tests.support.http import request
from tests.support.profiles import _odoo_profile_payload_with_prod_lane


def profile() -> LaunchplaneProductProfileRecord:
    payload = _odoo_profile_payload_with_prod_lane("example-site")
    payload["production_use"] = "live"
    return LaunchplaneProductProfileRecord.model_validate(payload)


class TestingRestorePolicyTests(unittest.TestCase):
    def test_existing_authority_and_its_backup_or_empty_rebuild_eligibility_are_preserved(
        self,
    ) -> None:
        for authority in ("resettable", "restorable", "authoritative"):
            with self.subTest(authority=authority):
                before = profile()
                testing = next(lane for lane in before.lanes if lane.instance == "testing")
                policy = testing.odoo_data_policy.model_copy(update={"data_authority": authority})
                before = before.model_copy(
                    update={
                        "lanes": tuple(
                            lane.model_copy(update={"odoo_data_policy": policy})
                            if lane == testing
                            else lane
                            for lane in before.lanes
                        )
                    }
                )
                plan, _ = build_testing_restore_policy_plan(
                    before, TestingRestorePolicyRequest(context=testing.context, reason="Restore")
                )
                self.assertEqual(plan.policy_after.data_authority, policy.data_authority)
                self.assertEqual(
                    plan.policy_after.destructive_replacement_proof_required,
                    policy.destructive_replacement_proof_required,
                )
                for source in policy.allowed_rebuild_sources:
                    self.assertTrue(plan.policy_after.allows_rebuild_source(source))
                self.assertTrue(plan.policy_after.allows_rebuild_source("upstream_restore"))

    def test_plan_changes_only_testing_data_policy_and_binds_the_source_profile(self) -> None:
        before = profile()
        testing = next(lane for lane in before.lanes if lane.instance == "testing")
        body = TestingRestorePolicyRequest(context=testing.context, reason="Restore testing")
        plan, after = build_testing_restore_policy_plan(before, body)
        expected = before.model_dump(mode="json")
        for lane in expected["lanes"]:
            if lane["instance"] == "testing":
                lane["odoo_data_policy"] = plan.policy_after.model_dump(mode="json")
        self.assertEqual(after.model_dump(mode="json"), expected)
        self.assertTrue(plan.policy_after.allows_rebuild_source("upstream_restore"))
        self.assertTrue(plan.policy_after.requires_runtime_identity)
        changed, _ = build_testing_restore_policy_plan(
            before.model_copy(update={"source": "concurrent-change"}), body
        )
        self.assertNotEqual(changed.plan_sha256, plan.plan_sha256)
        repeated, _ = build_testing_restore_policy_plan(after, body)
        self.assertFalse(repeated.changed)

    def test_unowned_context_missing_source_and_unreviewed_apply_refuse(self) -> None:
        before = profile()
        with self.assertRaises(ValueError):
            build_testing_restore_policy_plan(
                before, TestingRestorePolicyRequest(context="another-lane", reason="Restore")
            )
        without_source = before.model_copy(
            update={"lanes": tuple(lane for lane in before.lanes if lane.instance != "prod")}
        )
        with self.assertRaises(ValueError):
            build_testing_restore_policy_plan(
                without_source,
                TestingRestorePolicyRequest(context=before.lanes[0].context, reason="Restore"),
            )
        with self.assertRaises(ValueError):
            TestingRestorePolicyRequest(context="example", reason="Restore", mode="apply")


class TestingRestorePolicyHttpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = PostgresRecordStore(
            database_url=f"sqlite+pysqlite:///{Path(temporary.name) / 'state.db'}"
        )
        self.store.ensure_schema()
        self.addCleanup(self.store.close)
        self.before = profile()
        self.store.write_product_profile_record(self.before)
        self.context = next(
            lane.context for lane in self.before.lanes if lane.instance == "testing"
        )
        self.app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=LaunchplaneAuthzPolicy(),
            bearer_identity_config=_local_operator_bearer_config(token_label="local-owner-write"),
            record_store_factory=lambda: self.store,
        )

    async def post(self, payload: Mapping[str, object], key: str = "") -> Any:
        return await request(
            self.app,
            "POST",
            ROUTE.format(product=self.before.product),
            headers={"Authorization": "Bearer local-operator-token", "Idempotency-Key": key},
            payload=dict(payload),
        )

    async def test_review_apply_replay_and_readback_preserve_production_and_other_authority(
        self,
    ) -> None:
        draft = {"context": self.context, "reason": "Prepare sanitized upstream restore"}
        dry = await self.post(draft)
        self.assertEqual(dry.status_code, 202, dry.text)
        self.assertEqual(self.store.read_product_profile_record(self.before.product), self.before)
        body = {
            **draft,
            "mode": "apply",
            "reviewed_plan_sha256": dry.json()["result"]["plan_sha256"],
        }
        self.assertEqual((await self.post(body)).status_code, 400)
        applied = await self.post(body, "restore-policy")
        self.assertEqual(applied.status_code, 202, applied.text)
        after = self.store.read_product_profile_record(self.before.product)
        expected = self.before.model_dump(mode="json")
        for lane in expected["lanes"]:
            if lane["instance"] == "testing":
                lane["odoo_data_policy"] = applied.json()["result"]["policy_after"]
        expected.update(updated_at=after.updated_at, source=after.source)
        self.assertEqual(after.model_dump(mode="json"), expected)
        replay = await self.post(body, "restore-policy")
        self.assertTrue(replay.json()["replayed"])
        self.assertEqual(replay.json()["original_trace_id"], applied.json()["trace_id"])
        conflict = await self.post({**body, "reason": "Different intent"}, "restore-policy")
        self.assertEqual(conflict.json()["error"]["code"], "idempotency_key_reused")

    async def test_production_client_and_arbitrary_profile_changes_never_reach_a_write(
        self,
    ) -> None:
        draft = {"context": self.context, "reason": "Restore testing"}
        with patch.object(self.store, "compare_and_write_product_profile_record") as write:
            extras: tuple[dict[str, object], ...] = (
                {"instance": "prod"},
                {"source_context": "another-product"},
                {"lanes": []},
                {"runtime_env": {"DANGEROUS": "fixture"}},
            )
            for extra in extras:
                with self.subTest(extra=extra):
                    self.assertEqual((await self.post({**draft, **extra})).status_code, 400)
            with scoped_agent_transport():
                self.assertEqual((await self.post(draft)).status_code, 403)
            write.assert_not_called()
        self.assertEqual(self.store.read_product_profile_record(self.before.product), self.before)

    async def test_stale_review_and_concurrent_production_profile_change_are_not_overwritten(
        self,
    ) -> None:
        draft = {"context": self.context, "reason": "Restore testing"}
        dry = await self.post(draft)
        body = {
            **draft,
            "mode": "apply",
            "reviewed_plan_sha256": dry.json()["result"]["plan_sha256"],
        }
        production = next(lane for lane in self.before.lanes if lane.instance == "prod")
        changed_production = production.model_copy(
            update={
                "odoo_data_policy": production.odoo_data_policy.model_copy(
                    update={
                        "requires_runtime_identity": not production.odoo_data_policy.requires_runtime_identity
                    }
                )
            }
        )
        raced = self.before.model_copy(
            update={
                "lanes": tuple(
                    changed_production if lane == production else lane for lane in self.before.lanes
                )
            }
        )
        original = self.store.compare_and_write_product_profile_record

        def race(**kwargs: Any) -> ProductProfileCompareWriteResult:
            self.store.write_product_profile_record(raced)
            return original(**kwargs)

        with patch.object(self.store, "compare_and_write_product_profile_record", side_effect=race):
            result = await self.post(body, "racing-policy")
        self.assertEqual(result.json()["error"]["code"], "stale")
        self.assertEqual(self.store.read_product_profile_record(self.before.product), raced)
        self.assertEqual((await self.post(body, "stale-policy")).json()["error"]["code"], "stale")
