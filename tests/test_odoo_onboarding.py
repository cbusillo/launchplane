from copy import deepcopy
from typing import Any, cast
from unittest.mock import patch

from sqlalchemy import event
from sqlalchemy.engine import Engine

from control_plane.contracts.product_onboarding_manifest import ProductOnboardingManifest
from control_plane.contracts.product_profile_record import product_profile_record_sha256
from control_plane.odoo_onboarding import OdooOnboardingIntent
from control_plane.service_auth import LaunchplaneAuthzPolicy, LocalOperatorPolicyRule
from control_plane.workflows.product_onboarding import build_product_profile_record
from tests.http_app_test_support import _local_operator_bearer_config
from tests.support.auth import scoped_agent_transport
from tests.support.http import request
from tests.test_director_testing_target_creation import _DirectorTestingSetupFixture
from tests.test_product_onboarding import _manifest_payload
from tests.test_odoo_cutover_lane_authority import lane_authority


class NativeOdooOnboardingTests(_DirectorTestingSetupFixture):
    def setUp(self) -> None:
        super().setUp()
        self.intent = OdooOnboardingIntent(
            product="odoo-tenant-demo",
            display_name="Odoo Demo",
            repository="example/odoo-tenant-demo",
            repository_id="123",
            repository_owner_id="456",
            image_repository="ghcr.io/example/odoo-tenant-demo",
            context="demo",
            preview_base_url="https://demo-preview.example.invalid",
        )
        self.review = {
            "product": "launchplane",
            "mode": "dry_run",
            "odoo": self.intent.model_dump(),
        }

    async def records(self, *, idempotency_key: str = "odoo-records", **changes: Any) -> Any:
        headers = {"Authorization": "Bearer local-operator-token"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return await request(
            self.app,
            "POST",
            "/v1/product-onboarding/apply",
            payload={**self.review, **changes},
            headers=headers,
        )

    async def test_director_registers_records_then_stages_the_first_testing_target(self) -> None:
        planned = await self.records(idempotency_key="")
        self.assertEqual(planned.status_code, 202, planned.text)
        apply = {"mode": "apply", "reviewed_plan_sha256": planned.json()["result"]["plan_sha256"]}
        self.assertEqual((await self.records(idempotency_key="", **apply)).status_code, 400)
        mismatched = await self.records(**{**apply, "reviewed_plan_sha256": "0" * 64})
        self.assertEqual(mismatched.status_code, 409, mismatched.text)
        with scoped_agent_transport():
            self.assertEqual((await self.records(**apply)).status_code, 403)
        self.assertEqual(len(self.store.list_product_profile_records()), 1)

        applied = await self.records(**apply)
        self.assertEqual(applied.status_code, 202, applied.text)
        self.assertTrue((await self.records(**apply)).json()["replayed"])
        again = await self.records(idempotency_key="other-records", **apply)
        self.assertEqual(again.status_code, 400, again.text)
        profile = self.store.read_product_profile_record(self.intent.product)
        self.assertEqual(profile.driver_id, "odoo")
        self.assertEqual(profile.repository_id, "123")
        self.assertEqual(
            [(lane.context, lane.instance) for lane in profile.lanes], [("demo", "testing")]
        )
        self.assertTrue(profile.preview.enabled)
        self.assertEqual(profile.preview.context, "demo")
        self.provider.assert_not_called()

        staged = await self.post(context="demo")
        self.assertEqual(staged.status_code, 202, staged.text)
        target = self.store.read_dokploy_target_record(context_name="demo", instance_name="testing")
        self.assertEqual(target.target_type, "compose")
        self.assert_production_unchanged()

    async def test_a_context_owned_by_another_product_is_refused(self) -> None:
        owner = self.profile.model_copy(
            update={
                "product": "other-product",
                "lanes": tuple(
                    lane.model_copy(update={"context": "demo"}) for lane in self.profile.lanes
                ),
            }
        )
        self.store.write_product_profile_record(owner)
        refused = await self.records()
        self.assertEqual(refused.status_code, 400, refused.text)
        self.assertIn("unowned", refused.text)
        self.assertEqual(
            {record.product for record in self.store.list_product_profile_records()},
            {self.profile.product, owner.product},
        )


class AdvancedManifestPlanningTests(_DirectorTestingSetupFixture):
    def setUp(self) -> None:
        super().setUp()
        payload = {
            **_manifest_payload(),
            "driver_id": "odoo",
            "provider_targets": [],
            "runtime_environments": [],
            "secret_bindings": [],
            "updated_at": "",
        }
        lanes = cast(list[dict[str, Any]], payload["lanes"])
        lanes[1]["odoo_data_policy"] = {}
        lanes[1]["odoo_prelaunch_rebuild"] = {}
        self.manifest = ProductOnboardingManifest.model_validate(payload)
        self.existing = build_product_profile_record(
            manifest=self.manifest, updated_at="2026-01-01T00:00:00Z"
        )
        self.existing.lanes[1].odoo_cutover = lane_authority()
        self.store.write_product_profile_record(self.existing)
        desired = deepcopy(payload)
        cast(list[dict[str, Any]], desired["lanes"])[1]["odoo_data_policy"] = {
            "data_authority": "resettable",
            "allowed_rebuild_sources": ["empty"],
            "requires_runtime_identity": True,
        }
        self.review = {"product": "launchplane", "mode": "dry_run", "manifest": desired}

    async def records(self, *, key: str = "", **changes: Any) -> Any:
        return await request(
            self.app,
            "POST",
            "/v1/product-onboarding/apply",
            payload={**self.review, **changes},
            headers={"Authorization": "Bearer local-operator-token", "Idempotency-Key": key},
        )

    async def test_existing_product_policy_can_be_reviewed_without_writes(self) -> None:
        def reject_writes(
            _connection: Any,
            _cursor: Any,
            statement: str,
            _parameters: Any,
            _context: Any,
            _executemany: bool,
        ) -> None:
            self.assertNotIn(
                statement.lstrip().split()[0].upper(),
                {"INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP", "ALTER"},
                statement,
            )

        event.listen(Engine, "before_cursor_execute", reject_writes)
        try:
            with (
                patch.object(
                    self.store,
                    "write_product_authority_bundle",
                    side_effect=AssertionError("plan wrote authority"),
                ),
                patch.object(
                    self.store,
                    "read_secret_version",
                    side_effect=AssertionError("plan read credentials"),
                ),
                patch.object(
                    self.store,
                    "read_secret_record",
                    side_effect=AssertionError("plan read credentials"),
                ),
                patch(
                    "control_plane.workflows.product_onboarding.utc_now_timestamp",
                    side_effect=("2026-01-02T00:00:00Z", "2026-01-03T00:00:00Z"),
                ),
            ):
                planned = await self.records(key="plan-does-not-persist")
                repeated = await self.records(key="plan-does-not-persist")
        finally:
            event.remove(Engine, "before_cursor_execute", reject_writes)
        self.provider.assert_not_called()
        self.assertEqual(
            self.store.read_product_profile_record(self.existing.product), self.existing
        )
        self.assertEqual(planned.status_code, 202, planned.text)
        result = planned.json()["result"]
        self.assertFalse(result["applied"])
        self.assertEqual(result["plan_sha256"], repeated.json()["result"]["plan_sha256"])
        self.assertEqual(
            result["current_profile_sha256"], product_profile_record_sha256(self.existing)
        )
        self.assertEqual(result["changed_profile_fields"], ["lanes"])
        self.assertEqual(
            result["lane_changes"],
            [
                {
                    "context": self.existing.lanes[1].context,
                    "instance": "prod",
                    "fields": ["odoo_data_policy"],
                }
            ],
        )
        self.assertNotIn(lane_authority().database_uuid, planned.text)
        self.assertNotIn(lane_authority().prepared_origin, planned.text)

    def use_policy(self, *, product: str, actions: tuple[str, ...]) -> None:
        self.app = self.make_app(
            _local_operator_bearer_config(),
            LaunchplaneAuthzPolicy(
                local_operators=(
                    LocalOperatorPolicyRule(
                        subjects=("local-owner-agent",),
                        token_labels=("local-owner-read",),
                        products=(product,),
                        contexts=("launchplane",),
                        actions=actions,
                    ),
                )
            ),
        )

    async def test_planning_preserves_reader_scope_and_apply_refusal(self) -> None:
        self.use_policy(product=self.existing.product, actions=("product_profile.read",))
        with scoped_agent_transport():
            planned = await self.records()
            self.assertEqual(planned.status_code, 202, planned.text)
            refused = await self.records(
                mode="apply", reviewed_plan_sha256=planned.json()["result"]["plan_sha256"]
            )
            self.assertEqual(refused.status_code, 403, refused.text)
            other_manifest = {
                **cast(dict[str, Any], self.review["manifest"]),
                "product": "other-product",
            }
            other = await self.records(manifest=other_manifest)
            self.assertEqual(other.status_code, 403, other.text)
            foreign_owner = self.profile.model_copy(
                update={
                    "product": "other-product",
                    "lanes": (
                        self.profile.lanes[0].model_copy(
                            update={"context": self.existing.lanes[1].context}
                        ),
                    ),
                }
            )
            self.store.write_product_profile_record(foreign_owner)
            other_context = await self.records()
            self.assertEqual(other_context.status_code, 403, other_context.text)
        self.assertEqual((await self.records(product=self.existing.product)).status_code, 400)
        self.assertEqual((await self.records(mode="apply")).status_code, 403)
        self.assertEqual(
            self.store.read_product_profile_record(self.existing.product), self.existing
        )

    async def test_review_binds_desired_and_current_records_and_keeps_original_replay(self) -> None:
        planned = await self.records()
        digest = planned.json()["result"]["plan_sha256"]
        self.use_policy(product="launchplane", actions=("product_onboarding.apply",))
        changed = {
            **cast(dict[str, Any], self.review["manifest"]),
            "display_name": "Changed desired",
        }
        mismatch = await self.records(
            mode="apply", manifest=changed, reviewed_plan_sha256=digest, key="reviewed-change"
        )
        self.assertEqual(mismatch.status_code, 409, mismatch.text)
        current = self.existing.model_copy(update={"display_name": "Concurrent name"})
        self.store.write_product_profile_record(current)
        stale = await self.records(mode="apply", reviewed_plan_sha256=digest, key="reviewed-change")
        self.assertEqual(stale.status_code, 409, stale.text)
        refreshed = await self.records()
        refreshed_digest = refreshed.json()["result"]["plan_sha256"]
        self.assertNotEqual(refreshed_digest, digest)
        applied = await self.records(
            mode="apply", reviewed_plan_sha256=refreshed_digest, key="reviewed-change"
        )
        self.assertEqual(applied.status_code, 202, applied.text)
        replay = await self.records(
            mode="apply", reviewed_plan_sha256=refreshed_digest, key="reviewed-change"
        )
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertTrue(replay.json()["replayed"])
        stored = self.store.read_product_profile_record(self.existing.product)
        desired_profile = build_product_profile_record(
            manifest=ProductOnboardingManifest.model_validate(self.review["manifest"]),
            existing_profile=current,
            updated_at=stored.updated_at,
        )
        self.assertEqual(stored, desired_profile)
        self.assertEqual(stored.lanes[1].odoo_cutover, self.existing.lanes[1].odoo_cutover)
        self.assertEqual(stored.release_on_acceptance, self.existing.release_on_acceptance)
        self.provider.assert_not_called()

    async def test_commit_race_keeps_current_authority_and_writes_no_receipt(self) -> None:
        planned = await self.records()
        self.use_policy(product="launchplane", actions=("product_onboarding.apply",))
        write_bundle = self.store.write_product_authority_bundle
        concurrent = self.existing.model_copy(update={"display_name": "Concurrent edit"})

        def race(bundle: Any) -> None:
            self.store.write_product_profile_record(concurrent)
            write_bundle(bundle)

        with patch.object(self.store, "write_product_authority_bundle", side_effect=race):
            refused = await self.records(
                mode="apply",
                reviewed_plan_sha256=planned.json()["result"]["plan_sha256"],
                key="racing-change",
            )
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertEqual(refused.json()["error"]["code"], "product_profile_changed")
        self.assertEqual(self.store.read_product_profile_record(self.existing.product), concurrent)
        refreshed = await self.records()
        applied = await self.records(
            mode="apply",
            reviewed_plan_sha256=refreshed.json()["result"]["plan_sha256"],
            key="racing-change",
        )
        self.assertEqual(applied.status_code, 202, applied.text)
        self.assertFalse(applied.json().get("replayed", False))
