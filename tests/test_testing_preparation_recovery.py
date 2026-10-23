from copy import deepcopy
from typing import Any
import unittest
from unittest.mock import patch

from control_plane.provider_operations import (
    run_durable_provider_operation,
    ProviderMutationUnknownError,
)
from control_plane.dokploy import post_deploy
from control_plane.contracts.testing_activation import TestingActivationRefusal
from control_plane.testing_separation_resources import (
    ROUTE,
    STORAGE_PROGRAM_NAME,
    SeparationResourcesAdapter,
    SeparationResourcesRequest,
    SeparationResourcesRefusal,
    parse_preparation_receipt,
    preparation_binding,
)
from tests import test_testing_application_flow as application_flow


class PreparationRecoveryTests(unittest.TestCase):
    def application_fixture(self) -> application_flow.ApplicationFlowTests:
        fixture = application_flow.ApplicationFlowTests()
        fixture.no_original = True
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def setUp(self) -> None:
        self.fixture = self.application_fixture()
        self.retained_data = (self.fixture.old_backing / "keep").read_bytes()
        self.store = self.fixture.store
        self.body = SeparationResourcesRequest(
            context=self.fixture.context,
            application=self.fixture.specification,
            mode="apply",
            reason="Prepare isolated application state.",
        )
        self.body.reviewed_plan_sha256 = preparation_binding(
            self.store,
            self.fixture.product,
            self.fixture.context,
            self.fixture.actor,
            self.fixture.root,
            self.fixture.specification,
        )[0]
        self.adapter = SeparationResourcesAdapter(
            self.fixture.root,
            self.store,
            self.fixture.product,
            self.body,
            self.fixture.actor,
            "preparation-recovery-fixture",
        )
        name = self.fixture.payloads[self.fixture.old_id]["appName"]
        self.fixture.tasks[name + "-task"]["Spec"]["ContainerSpec"]["Env"].append(
            "DOCKER_HOST=tcp://fixture.invalid"
        )
        self.capability_calls: list[str] = []
        self.capability_status = "error"
        self.lose_reply = False
        read_markers = post_deploy._read_odoo_post_deploy_log_markers
        self.enterContext(
            patch.object(
                post_deploy,
                "_read_odoo_post_deploy_log_markers",
                side_effect=lambda **kwargs: (
                    {"log_available": "false"} if self.lose_reply else read_markers(**kwargs)
                ),
            )
        )
        self.enterContext(
            patch(
                "control_plane.testing_application_host.post_deploy.run_manual_data_schedule",
                side_effect=self.capability,
            )
        )
        self.storage = self.enterContext(
            patch(
                "control_plane.testing_separation_resources.run_preparation_schedule",
                side_effect=ValueError("Storage effects are forbidden in this fixture."),
            )
        )

    def capability(self, **kwargs: Any) -> dict[str, str]:
        self.old_effect = kwargs["before_provider_mutation"]
        markers = self.fixture.host_schedule(**kwargs)
        identifier = "capability-" + str(len(self.capability_calls))
        self.capability_calls.append(identifier)
        self.fixture.schedules[identifier] = {
            **deepcopy(kwargs["schedule_payload"]),
            "scheduleId": identifier,
            "applicationId": kwargs["schedule_lookup_id"],
        }
        self.fixture.schedule_history[identifier] = [
            {
                "scheduleId": identifier,
                "deploymentId": identifier + "-execution",
                "status": self.capability_status
                if markers.get("testing_resources_ok") == "false"
                else "done",
                "startedAt": self.fixture.old.now(-1),
                "finishedAt": self.fixture.old.now(),
                "logs": [key + "=" + value for key, value in markers.items()],
            }
        ]
        if self.lose_reply:
            raise OSError("Original capability reply lost.")
        if markers.get("testing_resources_ok") == "false":
            raise post_deploy.OdooPostDeployReadbackFailure(
                "Capability program refused.", evidence=markers
            )
        return markers

    def run_preparation(self, key: str = "prepare", **options: Any) -> Any:
        return run_durable_provider_operation(
            store=self.store,
            scope=self.fixture.scope,
            route_path=ROUTE.format(product=self.fixture.product),
            idempotency_key=key,
            request_fingerprint="a" * 64,
            lease_owner="preparation-worker",
            response_trace_id="preparation-recovery-fixture",
            adapter=self.adapter,
            **options,
        )

    def run_legacy_preparation(self, phase: str) -> Any:
        checkpoint = self.store.checkpoint_mutation_provider_effect
        with patch.object(
            self.store,
            "checkpoint_mutation_provider_effect",
            side_effect=lambda **kwargs: checkpoint(**{**kwargs, "effect_phase": phase}),
        ):
            result = self.run_preparation()
        self.assertEqual(result.record.provider_effect_phase, phase)
        return result

    def assert_negative(self, result: Any) -> None:
        self.assertEqual(result.status, "completed", result)
        receipt = result.response_payload["result"]
        self.assertIs(receipt["applied"], False)
        self.assertIs(receipt["resources_verified"], False)
        self.assertIsInstance(parse_preparation_receipt(receipt), SeparationResourcesRefusal)
        self.assertNotIn("volumes", receipt)
        from control_plane.contracts.testing_activation import TestingActivationRequest
        from control_plane.testing_activation import preparation_resources
        from control_plane.integration_allowances import target_record_sha256

        request = TestingActivationRequest(
            context=self.fixture.context,
            preparation_key=result.record.idempotency_key,
            interlock_record_sha256=target_record_sha256(self.fixture.retained),
            source_commit="a" * 40,
            volume_keys={},
            port_keys=(),
            reason="Negative preparation must not activate.",
            application=self.fixture.specification,
        )
        with self.assertRaises(TestingActivationRefusal):
            preparation_resources(self.store, self.fixture.product, request, self.fixture.scope)
        self.storage.assert_not_called()
        self.assertFalse(self.store.list_product_reconcile_requests())
        self.assertEqual(
            self.store.read_dokploy_target_record(
                context_name=self.fixture.context, instance_name="testing"
            ),
            self.fixture.retained,
        )
        self.assertEqual((self.fixture.old_backing / "keep").read_bytes(), self.retained_data)

    def test_first_determinate_capability_refusal_completes_without_storage(self) -> None:
        self.assert_negative(self.run_preparation())
        self.assertEqual(self.run_preparation().status, "replayed")
        self.assert_negative(self.run_preparation("future-key"))

    def test_original_key_recovers_lost_refusal_without_dispatching_another_program(self) -> None:
        self.lose_reply = True
        self.capability_status = "done"
        held = self.run_legacy_preparation(post_deploy.ODOO_POST_DEPLOY_SCHEDULE_TRIGGER)
        self.assertEqual(held.status, "reconcile_required", held)
        self.assertEqual(self.run_preparation("new-key").status, "target_busy")
        original = held.record
        assert original is not None
        old_effect = self.old_effect
        self.lose_reply = False
        calls = list(self.capability_calls)
        self.storage.reset_mock()
        self.assert_negative(self.run_preparation())
        self.assertEqual(self.capability_calls, calls)
        completed = self.store.read_idempotency_record(
            scope=self.fixture.scope,
            route_path=ROUTE.format(product=self.fixture.product),
            idempotency_key="prepare",
        )
        assert completed is not None
        self.assertEqual(completed.record_id, original.record_id)
        self.assertEqual(completed.provider_effect_started_at, original.provider_effect_started_at)
        self.assertEqual(self.run_preparation().status, "replayed")
        with self.assertRaises(RuntimeError):
            old_effect(post_deploy.ODOO_POST_DEPLOY_SCHEDULE_UPSERT)
        self.storage.assert_not_called()

    def test_incomplete_proof_after_reacquisition_cannot_resume_forward_work(self) -> None:
        self.lose_reply = True
        self.assertEqual(self.run_preparation().status, "reconcile_required")
        self.lose_reply = False
        inspect = self.adapter.inspection_refusal
        calls = list(self.capability_calls)
        proof_count = 0

        def lost_proof(*args: Any) -> Any:
            nonlocal proof_count
            proof_count += 1
            return inspect(*args) if proof_count == 1 else None

        self.storage.reset_mock()
        apply = self.adapter.apply
        failures = []

        def capture_failure(*args: Any) -> Any:
            try:
                return apply(*args)
            except Exception as error:
                failures.append(error)
                raise

        with (
            patch.object(self.adapter, "inspection_refusal", side_effect=lost_proof),
            patch.object(self.adapter, "apply", side_effect=capture_failure),
        ):
            self.assertEqual(self.run_preparation().status, "reconcile_required")
        self.assertTrue(failures)
        self.assertTrue(
            all(isinstance(error, ProviderMutationUnknownError) for error in failures), failures
        )
        self.assertEqual(self.capability_calls, calls)
        self.storage.assert_not_called()

    def test_legacy_upsert_checkpoint_cannot_prove_exclusion_of_a_delayed_storage_request(
        self,
    ) -> None:
        self.lose_reply = True
        self.assertEqual(
            self.run_legacy_preparation(post_deploy.ODOO_POST_DEPLOY_SCHEDULE_UPSERT).status,
            "reconcile_required",
        )
        self.lose_reply = False
        calls = list(self.capability_calls)
        self.assertEqual(self.run_preparation().status, "reconcile_required")
        self.assertEqual(self.capability_calls, calls)

    def test_uncertain_child_or_changed_authority_keeps_the_original_fence(self) -> None:
        self.lose_reply = True
        original = self.run_preparation()
        self.assertEqual(original.status, "reconcile_required")
        self.lose_reply = False
        schedules = deepcopy(self.fixture.schedules)
        histories = deepcopy(self.fixture.schedule_history)
        identifier = self.capability_calls[-1]
        mutations = (
            lambda: self.fixture.schedules[identifier].update(enabled=True),
            lambda: self.fixture.schedules[identifier].update(script="unrelated program"),
            lambda: self.fixture.schedules.update({"duplicate": deepcopy(schedules[identifier])}),
            lambda: self.fixture.schedule_history[identifier][0].update(status="running"),
            lambda: self.fixture.schedule_history[identifier][0].pop("logs"),
            lambda: self.fixture.schedule_history[identifier].append(
                {**histories[identifier][0], "deploymentId": "another-execution"}
            ),
            lambda: self.fixture.schedules.update(
                {
                    "later-work": {
                        **schedules[identifier],
                        "name": schedules[identifier]["name"] + " readback",
                    }
                }
            ),
            lambda: self.fixture.schedules.update(
                {"storage-work": {**schedules[identifier], "name": STORAGE_PROGRAM_NAME}}
            ),
        )
        for mutate in mutations:
            self.fixture.schedules = deepcopy(schedules)
            self.fixture.schedule_history = deepcopy(histories)
            mutate()
            calls = list(self.capability_calls)
            with self.subTest(
                programs=self.fixture.schedules, history=self.fixture.schedule_history
            ):
                result = self.run_preparation()
                self.assertEqual(result.status, "reconcile_required", result)
                self.assertEqual(result.record.record_id, original.record.record_id)
                self.assertEqual(self.capability_calls, calls)
                self.assertEqual(self.run_preparation("escape-key").status, "target_busy")

    def test_changed_hold_or_busy_original_executor_cannot_complete_a_refusal(self) -> None:
        from contextlib import nullcontext

        self.lose_reply = True
        self.assertEqual(self.run_preparation().status, "reconcile_required")
        self.lose_reply = False
        calls = list(self.capability_calls)
        with patch.object(
            self.store, "testing_replacement_execution_lock", return_value=nullcontext(None)
        ):
            self.assertEqual(self.run_preparation().status, "reconcile_required")
        changed = self.fixture.retained.model_copy(deep=True)
        assert changed.policies.staff_testing_hold is not None
        changed.policies.staff_testing_hold.recorded_by = "foreign-staff-session"
        self.store.write_dokploy_target_record(changed)
        self.assertEqual(self.run_preparation().status, "reconcile_required")
        self.assertEqual(self.capability_calls, calls)

    def test_completed_negative_receipt_is_readable_without_provider_access(self) -> None:
        import asyncio
        from control_plane.http_app import create_launchplane_fastapi_app
        from control_plane.service_auth import BearerIdentityConfig
        from tests.support.auth import StubVerifier, identity, local_operator_policy
        from tests.support.http import request

        result = self.run_preparation()
        self.assert_negative(result)
        app = create_launchplane_fastapi_app(
            verifier=StubVerifier(identity()),
            authz_policy=local_operator_policy(actions=()),
            record_store_factory=lambda: self.store,
            control_plane_root_path=self.fixture.root,
            bearer_identity_config=BearerIdentityConfig(
                local_operator_token="fixture",
                local_operator_subject="local-owner-agent",
                local_operator_token_label="local-owner-write",
            ),
        )
        with (
            patch(
                "control_plane.dokploy.api.dokploy_request",
                side_effect=AssertionError("Provider access forbidden."),
            ),
            patch(
                "control_plane.dokploy.api.fetch_dokploy_target_payload",
                side_effect=AssertionError("Provider access forbidden."),
            ),
        ):
            response = asyncio.run(
                request(
                    app,
                    "POST",
                    ROUTE.format(product=self.fixture.product),
                    headers={"Authorization": "Bearer fixture", "Idempotency-Key": "prepare"},
                    payload={**self.body.model_dump(mode="json"), "mode": "receipt"},
                )
            )
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["result"], result.response_payload["result"])
