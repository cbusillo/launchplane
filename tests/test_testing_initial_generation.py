from typing import Any
import unittest
from unittest.mock import patch

from control_plane.odoo_testing_usage import OdooTestingUsage
from control_plane.testing_replacement_resources import (
    ROUTE,
    TestingReplacementResourcesRequest,
)
from tests.support.auth import scoped_agent_transport
from tests.support.http import request
from tests import test_testing_replacement_resources as resource_tests
from tests.test_testing_replacement_bootstrap import FreshTestingBootstrapScenario


class InitialTestingGenerationHttpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.fixture = resource_tests.TestingReplacementResourceHttpTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.store
        self.success = self.fixture.operation.model_copy(
            update={
                "status": "pass",
                "error_code": "",
                "error_message": "",
                "phase": "completed",
                "finished_at": self.fixture.operation.updated_at,
            }
        )
        self.store.write_odoo_stable_target_replacement_operation_record(self.success)
        from tests.support.initial_testing import configure_initial_restore

        configure_initial_restore(self.store, self.fixture.profile.product, self.fixture.context)
        self.credentials = self.enterContext(
            patch(
                "control_plane.testing_replacement_bootstrap.require_distinct_testing_credentials"
            )
        )
        self.usage = self.enterContext(
            patch(
                "control_plane.testing_initial_generation.read_odoo_testing_usage",
                return_value=OdooTestingUsage("unused", False, False, "complete"),
            )
        )

    async def post(self, *, key: str = "initial-original", **changes: object) -> Any:
        return await request(
            self.fixture.app,
            "POST",
            ROUTE.format(product=self.fixture.profile.product),
            headers={"Authorization": "Bearer local-operator-token", "Idempotency-Key": key},
            payload={
                "context": self.fixture.context,
                "initial_generation": True,
                "reason": "Prepare an independent sanitized testing copy",
                **changes,
            },
        )

    async def test_successful_legacy_lane_plans_without_changing_history_or_authority(self) -> None:
        before = self.store.read_dokploy_target_id_record(
            context_name=self.fixture.context, instance_name="testing"
        )
        response = await self.post()
        self.assertEqual(response.status_code, 202, response.text)
        result = response.json()["result"]
        preparation = self.store.read_disposable_resource_preparation(result["operation_id"])
        self.assertIsNotNone(preparation)
        assert preparation is not None
        self.assertFalse(preparation.initial_apply_started_at)
        self.assertEqual(
            self.store.read_dokploy_target_id_record(
                context_name=self.fixture.context, instance_name="testing"
            ),
            before,
        )
        self.assertEqual(
            self.store.read_odoo_stable_target_replacement_operation_record(
                self.success.operation_id
            ),
            self.success,
        )
        with self.assertRaises(FileNotFoundError):
            self.store.read_odoo_stable_target_replacement_operation_record(result["operation_id"])
        self.assertIsNone(
            self.store.read_fresh_odoo_testing_generation(
                self.fixture.profile.product, self.fixture.context
            )
        )
        self.assertEqual((await self.post()).json()["result"], result)
        self.assertEqual((await self.post(reason="changed intent")).status_code, 409)
        self.assertEqual(
            (await self.post(key="changed-key", operation_id=result["operation_id"])).status_code,
            409,
        )

    async def test_positive_use_production_wrong_lane_and_client_remain_refused(self) -> None:
        self.usage.return_value = OdooTestingUsage("in_use", True, True, "complete")
        self.assertEqual((await self.post()).status_code, 409)
        self.usage.return_value = OdooTestingUsage("unused", False, False, "complete")
        for changes in (
            {"instance": "prod"},
            {"context": "another"},
            {"initial_generation": False, "operation_id": self.success.operation_id},
        ):
            self.assertGreaterEqual((await self.post(**changes)).status_code, 400)
        with scoped_agent_transport():
            self.assertEqual((await self.post()).status_code, 403)
        self.assertEqual((await self.post(key="")).status_code, 409)

    async def test_unactivated_plan_does_not_reserve_the_original_hostname(self) -> None:
        from control_plane.contracts.dokploy_target_record import DokployTargetStaffTestingHold

        target = self.store.read_dokploy_target_record(
            context_name=self.fixture.context, instance_name="testing"
        )
        target = target.model_copy(update={"domains": ("original-testing.example.invalid",)})
        self.store.write_dokploy_target_record(target)
        planned = await self.post()
        self.assertEqual(planned.status_code, 202, planned.text)
        held = target.model_copy(
            update={
                "policies": target.policies.model_copy(
                    update={
                        "staff_testing_hold": DokployTargetStaffTestingHold(
                            reason="staff using original testing"
                        )
                    }
                )
            }
        )
        self.store.write_dokploy_target_record(held)
        self.assertEqual(
            self.store.read_dokploy_target_record(
                context_name=self.fixture.context, instance_name="testing"
            ),
            held,
        )

    async def test_stopped_runtime_qualifies_positive_use_not_found_repair(
        self,
    ) -> None:
        from datetime import datetime, timezone
        from tests.support.down_lanes import record_lane_health

        self.usage.return_value = OdooTestingUsage(
            "in_use", True, True, "serving_container_stopped"
        )
        self.assertEqual((await self.post()).status_code, 409)
        record_lane_health(
            self.store,
            self.fixture.profile.product,
            self.fixture.context,
            datetime.now(timezone.utc),
            status=404,
        )
        # A subsequent plan must capture the changed monitoring configuration.
        response = await self.post(key="stopped-runtime")
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(self.usage.return_value.usage, "in_use")
        self.usage.return_value = OdooTestingUsage(
            "in_use", True, True, "service_ambiguous_or_stopped"
        )
        self.assertEqual((await self.post(key="ambiguous-runtime")).status_code, 409)

    async def test_missing_copy_prerequisite_refuses_before_allocation_effects(self) -> None:
        from control_plane.testing_replacement_bootstrap import BootstrapPrerequisiteCode

        self.credentials.side_effect = ValueError("fixture unavailable local credentials")
        response = await self.post()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(
            response.json()["error"]["code"], BootstrapPrerequisiteCode.CREDENTIALS_UNVERIFIED
        )
        self.assertNotIn("fixture unavailable local credentials", response.text)
        self.usage.assert_not_called()
        self.assertEqual(
            self.store.read_odoo_stable_target_replacement_operation_record(
                self.success.operation_id
            ),
            self.success,
        )
        with self.store.odoo_synchronous_lane_reservation(
            product=self.fixture.profile.product, context=self.fixture.context, instance="testing"
        ) as owner:
            self.assertIsNone(owner)

    async def test_initial_refusals_identify_the_failed_checkpoint_without_private_details(
        self,
    ) -> None:
        from control_plane.testing_initial_generation import InitialTestingPrerequisiteCode

        scenarios = (
            (
                "control_plane.storage.postgres.PostgresRecordStore.prepare_initial_testing_generation",
                InitialTestingPrerequisiteCode.PREPARATION_UNAVAILABLE,
            ),
            (
                "control_plane.testing_initial_generation.require_initial_bootstrap_prerequisites",
                InitialTestingPrerequisiteCode.COPY_PREREQUISITE_UNVERIFIED,
            ),
            (
                "control_plane.testing_initial_generation.require_initial_usage",
                InitialTestingPrerequisiteCode.USAGE_UNVERIFIED,
            ),
        )
        for target, code in scenarios:
            self.usage.reset_mock()
            with (
                self.subTest(checkpoint=code),
                patch(target, side_effect=ValueError("private topology and credential detail")),
            ):
                response = await self.post()
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(response.json()["error"]["code"], code)
                self.assertNotIn("private topology and credential detail", response.text)
                self.usage.assert_not_called()
                self.assertIsNone(
                    self.store.read_fresh_odoo_testing_generation(
                        self.fixture.profile.product, self.fixture.context
                    )
                )
                with self.store.odoo_synchronous_lane_reservation(
                    product=self.fixture.profile.product,
                    context=self.fixture.context,
                    instance="testing",
                ) as owner:
                    self.assertIsNone(owner)

    async def test_provider_usage_denial_is_not_reclassified_as_safety_unknown(self) -> None:
        from control_plane.dokploy.native_copy_stream import NativeSourceAuthorizationDenied

        self.usage.side_effect = NativeSourceAuthorizationDenied("private-provider-denial")
        response = await self.post()
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")
        self.assertNotIn("private-provider-denial", response.text)

    async def test_stranded_operation_and_original_target_drift_do_not_admit_initial_apply(
        self,
    ) -> None:
        response = await self.post()
        self.assertEqual(response.status_code, 202, response.text)
        result = response.json()["result"]
        context = self.fixture.context
        target = self.store.read_dokploy_target_id_record(
            context_name=context, instance_name="testing"
        )
        self.store.write_dokploy_target_id_record(
            target.model_copy(update={"target_id": "changed"})
        )
        self.assertEqual((await self.post()).status_code, 409)
        self.store.write_dokploy_target_id_record(target)
        from control_plane.contracts.odoo_stable_target_replacement_operation import (
            build_odoo_stable_target_replacement_operation_id,
        )

        original = self.fixture.operation
        held_key = original.idempotency_key + ":stranded-original"
        held = original.model_copy(
            update={
                "operation_id": build_odoo_stable_target_replacement_operation_id(
                    product=original.product,
                    context=original.context,
                    instance=original.instance,
                    created_at=original.created_at,
                    idempotency_key=held_key,
                    idempotency_scope=original.idempotency_scope,
                ),
                "idempotency_key": held_key,
                "status": "reconciliation_required",
                "finished_at": "",
                "result": None,
                "error_code": "provider_outcome_unknown",
                "error_message": "Fixture original remains unverified.",
            }
        )
        self.store.write_odoo_stable_target_replacement_operation_record(held)
        self.assertEqual((await self.post()).status_code, 409)
        preparation = self.store.read_disposable_resource_preparation(result["operation_id"])
        assert preparation is not None
        self.assertFalse(preparation.initial_apply_started_at)


class InitialTestingBootstrapScenario(FreshTestingBootstrapScenario):
    def initial_bootstrap(self) -> None:
        self.patches()
        self.review()
        with patch.object(self.store, "supersede_disposable_testing_replacement") as supersede:
            outcome = self.apply()
        self.assertEqual(outcome.response_status_code, 202)
        supersede.assert_not_called()
        result = self.adapter().readback()
        self.assertTrue(result["reconcile_requested"])
        generation = self.store.read_fresh_odoo_testing_generation(
            self.profile.product, self.fixture.context
        )
        assert generation is not None
        self.assertFalse(generation.completed_at)
        self.assertTrue(generation.reconcile_requested_at)
        self.assertEqual(
            self.store.read_odoo_stable_target_replacement_operation_record(
                self.legacy_operation.operation_id
            ),
            self.legacy_operation,
        )
        with self.assertRaises(FileNotFoundError):
            self.store.read_odoo_stable_target_replacement_operation_record(generation.operation_id)
        queued = self.store.read_product_reconcile_request(f"{self.profile.product}:testing")
        self.store.request_fresh_testing_reconcile(generation)
        self.assertEqual(self.store.read_product_reconcile_request(queued.target_key), queued)
        self.assertEqual(self.route_writes, 1)

    def apply_fence_blocks_another_executor_and_preserves_original_key(self) -> None:
        with self.store.odoo_synchronous_lane_reservation(
            product=self.profile.product, context=self.fixture.context, instance="testing"
        ) as owner:
            self.assertIsNotNone(owner)
            assert owner is not None
            self.assertEqual(owner.operation_id, self.fixture.operation.operation_id)
        with self.assertRaises(ValueError):
            self.store.prepare_initial_testing_generation(
                product=self.profile.product,
                request=TestingReplacementResourcesRequest(
                    context=self.fixture.context,
                    operation_id="another",
                    initial_generation=True,
                    reason="another",
                ),
                prepared_by="fixture",
            )

    def incomplete_generation_cannot_be_claimed_complete_for_walkthrough(self) -> None:
        self.initial_bootstrap()
        from control_plane.odoo_walkthrough import (
            plan_walkthrough,
            WalkthroughPrerequisiteError,
            WalkthroughPrerequisite,
        )
        from control_plane.contracts.odoo_walkthrough import OdooWalkthroughRequest
        from control_plane.contracts.runtime_identity import RuntimeIdentity
        from control_plane.odoo_runtime_reads import OdooRuntimeSelection

        selection = OdooRuntimeSelection(
            RuntimeIdentity(
                product=self.profile.product,
                context=self.fixture.context,
                instance="testing",
                deployment_record_id="fixture",
                artifact_id="fixture",
                source_git_ref="a" * 40,
            ),
            "https://fresh.example.invalid",
        )
        with patch("control_plane.odoo_walkthrough.select_stable_runtime", return_value=selection):
            with self.assertRaises(WalkthroughPrerequisiteError) as refused:
                plan_walkthrough(
                    self.store,
                    self.fixture.root,
                    OdooWalkthroughRequest(
                        product=self.profile.product,
                        context=self.fixture.context,
                        task_id="1" * 32,
                        operation="provision",
                    ),
                    "fixture",
                )
        self.assertEqual(refused.exception.code, WalkthroughPrerequisite.GENERATION_INCOMPLETE)
