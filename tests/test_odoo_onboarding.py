from typing import Any

from control_plane.odoo_onboarding import OdooOnboardingIntent
from tests.support.auth import scoped_agent_transport
from tests.support.http import request
from tests.test_director_testing_target_creation import _DirectorTestingSetupFixture


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
