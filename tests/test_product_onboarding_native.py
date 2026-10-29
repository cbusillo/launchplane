from tests.http_app_test_support import (
    _browser_mutation_headers,
    _github_human_identity,
)
from tests.support.http import lifespan_client
from tests.test_onboarding_target_allocation import _OnboardingTargetAllocationFixture


class NativeProductOnboardingTests(_OnboardingTargetAllocationFixture):
    async def test_admin_reviews_and_applies_records_without_caller_grants(self) -> None:
        allocation = await self.plan()
        allocated = await self.post()
        self.assertEqual(allocated.status_code, 202, allocated.text)
        target = self.store.read_dokploy_target_record(
            context_name=self.intent.testing_context, instance_name="testing"
        )
        target_id = self.store.read_dokploy_target_id_record(
            context_name=target.context, instance_name=target.instance
        )
        reader = self.manager.issue(_github_human_identity(role="read_only"))
        review = {
            "product": "launchplane",
            "mode": "dry_run",
            "generic_web": self.intent.model_dump(mode="json"),
        }
        original_policy = self.policy.model_dump(mode="json")
        async with lifespan_client(self.app) as client:
            for headers in (
                {"Cookie": self.manager.session_cookie_header(self.session)},
                _browser_mutation_headers(self.manager, reader),
            ):
                refused = await client.post(
                    "/v1/product-onboarding/apply", json=review, headers=headers
                )
                self.assertEqual(refused.status_code, 403, refused.text)
            planned = await client.post(
                "/v1/product-onboarding/apply",
                json=review,
                headers=_browser_mutation_headers(self.manager, self.session),
            )
            self.assertEqual(planned.status_code, 202, planned.text)
            self.assertEqual(self.store.list_product_profile_records(), (self.profile,))
            applied_request = {
                **review,
                "mode": "apply",
                "reviewed_plan_sha256": planned.json()["result"]["plan_sha256"],
                "resolved_target_id": target_id.target_id,
                "allocation_id": allocation["allocation_id"],
            }
            headers = {
                **_browser_mutation_headers(self.manager, self.session),
                "Idempotency-Key": "native-onboarding-records",
            }
            changed_intent = self.intent.model_copy(update={"runtime_port": 9090})
            revised_plan = await client.post(
                "/v1/product-onboarding/apply",
                json={**review, "generic_web": changed_intent.model_dump(mode="json")},
                headers=_browser_mutation_headers(self.manager, self.session),
            )
            self.assertEqual(revised_plan.status_code, 202, revised_plan.text)
            headers.update(_browser_mutation_headers(self.manager, self.session))
            changed = await client.post(
                "/v1/product-onboarding/apply",
                json={
                    **applied_request,
                    "generic_web": changed_intent.model_dump(mode="json"),
                    "reviewed_plan_sha256": revised_plan.json()["result"]["plan_sha256"],
                },
                headers=headers,
            )
            self.assertEqual(changed.status_code, 400, changed.text)
            headers.update(_browser_mutation_headers(self.manager, self.session))
            applied = await client.post(
                "/v1/product-onboarding/apply", json=applied_request, headers=headers
            )
            self.assertEqual(applied.status_code, 202, applied.text)
            headers.update(_browser_mutation_headers(self.manager, self.session))
            replayed = await client.post(
                "/v1/product-onboarding/apply", json=applied_request, headers=headers
            )
            self.assertEqual(replayed.status_code, 202, replayed.text)
            self.assertTrue(replayed.json()["replayed"])
        profile = self.store.read_product_profile_record(self.intent.product)
        self.assertEqual(profile.repository_id, self.intent.repository_id)
        adopted = self.store.read_dokploy_target_record(
            context_name=target.context, instance_name=target.instance
        )
        self.assertEqual(adopted.target_name, target.target_name)
        self.assertEqual(adopted.project_name, target.project_name)
        self.assertEqual(self.policy.model_dump(mode="json"), original_policy)
        self.assert_production_unchanged()
