from unittest import TestCase
from unittest.mock import Mock, patch
from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any
from pathlib import Path

from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane import runner_lane_service as service
from control_plane.contracts.runner_queue_wait import build_runner_queue_wait_job
from control_plane.contracts.outbox_delivery import OutboxDeliveryRecord
from control_plane.storage.postgres import OutboxWithIdempotencyRequest
from control_plane.workflows.runner_lane_ephemeral import AddonRunnerRequest
from tests.test_runner_lane_service import installation_token


class LifecycleWorkflowSourceTests(TestCase):
    def test_launcher_failure_and_original_run_recovery_are_visible_without_redispatch(
        self,
    ) -> None:
        request = AddonRunnerRequest(repository="example/repo", run_id=12, run_attempt=2, job_id=35)
        delivery = service._addon_runner_delivery(request, "example/control", "refs/heads/main")
        delivery = delivery.model_copy(
            update={
                "state": "delivered",
                "action": "dispatched_workflow",
                "external_id": "78",
                "provider_operation_key": "original-dispatch-marker",
            }
        )
        run = {
            "id": 78,
            "status": "completed",
            "conclusion": "failure",
            "event": "workflow_dispatch",
            "display_title": delivery.payload["expected_run_title"],
            "path": service.RUNNER_LANE_LIFECYCLE_WORKFLOW_PATH,
            "head_branch": delivery.payload["ref"],
        }
        store = Mock()
        with (
            patch.object(
                service,
                "launchplane_github_token",
                side_effect=lambda **_: nullcontext("synthetic-dispatch-token"),
            ),
            patch.object(service, "github_api_request", return_value=run) as api,
        ):
            with self.assertLogs(service.__name__, level="WARNING"):
                service.observe_addon_launcher(store, delivery, control_plane_root=Path("."))
            failed = store.write_outbox_delivery_record.call_args.args[0]
            self.assertEqual(failed.state, "failed")
            self.assertEqual(failed.external_id, delivery.external_id)
            self.assertEqual(failed.provider_operation_key, delivery.provider_operation_key)
            self.assertEqual(failed.payload["run_conclusion"], "failure")
            store.write_outbox_delivery_record.reset_mock()
            service.observe_addon_launcher(store, failed, control_plane_root=Path("."))
            store.write_outbox_delivery_record.assert_not_called()
            run["conclusion"] = "success"
            service.observe_addon_launcher(store, failed, control_plane_root=Path("."))
            recovered = store.write_outbox_delivery_record.call_args.args[0]
            self.assertEqual(recovered.state, "delivered")
            self.assertFalse(recovered.error_code)
            self.assertTrue(all(call.kwargs["method"] == "GET" for call in api.call_args_list))
        store.enqueue_outbox_delivery_with_idempotency.assert_not_called()

    def test_a_missing_recipe_does_not_block_another_job_and_existing_dispatch_is_reused(
        self,
    ) -> None:
        deliveries: dict[str, OutboxDeliveryRecord] = {}
        store = Mock()
        store.list_product_profile_records.return_value = (
            SimpleNamespace(is_active=True, repository="example/repo"),
        )

        def read_existing(delivery_id: str) -> object:
            if delivery_id not in deliveries:
                raise FileNotFoundError(delivery_id)
            return deliveries[delivery_id]

        def enqueue(request: OutboxWithIdempotencyRequest) -> OutboxDeliveryRecord:
            deliveries[request.delivery.delivery_id] = request.delivery
            return request.delivery

        store.read_outbox_delivery_record.side_effect = read_existing
        store.enqueue_outbox_delivery_with_idempotency.side_effect = enqueue
        jobs = tuple(
            build_runner_queue_wait_job(
                github_id=job_id,
                run_id=12,
                repository="example/repo",
                workflow_name="Build",
                job_name="addon-validation",
                status="queued",
                labels=("lp-addon-12-2",),
            )
            for job_id in (34, 35)
        )
        reader = Mock()
        reader.read_runner_queue_wait.return_value = SimpleNamespace(jobs=jobs)

        def read_job(**kwargs: Any) -> dict[str, object]:
            request = kwargs["request"]
            assert isinstance(request, AddonRunnerRequest)
            if request.job_id == 34:
                raise ValueError("Missing recipe for this older job")
            return {"status": "queued"}

        with (
            patch.object(service, "read_active_authz_policy_record", return_value=self.policy()),
            patch.object(
                service,
                "runner_lifecycle_token",
                return_value=nullcontext(SimpleNamespace(token=installation_token())),
            ),
            patch.object(service, "GitHubRunnerQueueWaitReader", return_value=reader),
            patch.object(service, "read_addon_job", side_effect=read_job),
        ):
            with self.assertLogs(service.__name__, level="WARNING"):
                first = service.request_addon_runner_containers(store, control_plane_root=Path("."))
                second = service.request_addon_runner_containers(
                    store, control_plane_root=Path(".")
                )
        self.assertEqual(len(first), 1)
        self.assertEqual(second, ())
        self.assertEqual(len(deliveries), 1)
        delivery = next(iter(deliveries.values()))
        inputs = delivery.payload["inputs"]
        assert isinstance(inputs, dict)
        self.assertEqual(inputs["job_id"], "35")
        self.assertEqual(delivery.payload["repository"], "example/control")

    def policy(self, **changes: object) -> LaunchplaneAuthzPolicyRecord:
        rule = {
            "repository": "example/control",
            "workflow_refs": [
                "example/control/.github/workflows/runner-lane-registration.yml@refs/heads/main"
            ],
            "event_names": ["workflow_dispatch"],
            "actions": ["runner_lane_registration_audit.write"],
            "products": ["launchplane"],
            "contexts": ["launchplane"],
            **changes,
        }
        return LaunchplaneAuthzPolicyRecord(
            record_id="fixture-policy",
            source="fixture",
            updated_at="2026-11-08T17:00:00Z",
            policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": [rule]}),
        )

    def test_resolves_retained_workflow_from_its_existing_exact_authorization(self) -> None:
        with patch.object(service, "read_active_authz_policy_record", return_value=self.policy()):
            source = service.runner_lifecycle_workflow_source(Mock())
        self.assertEqual(source, ("example/control", "refs/heads/main"))

    def test_missing_wildcard_wrong_action_and_wrong_dispatch_bindings_are_not_invented(
        self,
    ) -> None:
        for changes in (
            {
                "workflow_refs": [
                    "example/control/.github/workflows/runner-lane-registration.yml@refs/heads/*"
                ]
            },
            {"actions": ["runner_lane_inventory.read"]},
            {"event_names": ["push"]},
            {"refs": ["refs/heads/other"]},
        ):
            with self.subTest(changes=changes):
                with patch.object(
                    service, "read_active_authz_policy_record", return_value=self.policy(**changes)
                ):
                    with self.assertRaises(LookupError):
                        service.runner_lifecycle_workflow_source(Mock())
