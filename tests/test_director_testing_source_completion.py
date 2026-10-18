"""Director transport uses the existing empty-source operation, without a grant."""

from copy import deepcopy
from typing import Any
import unittest
from unittest.mock import patch

from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.dokploy_target_setup_http import DokployTargetSetupEnvelope
from control_plane.service_auth import LaunchplaneAuthzPolicy
from tests.http_app_test_support import _local_operator_bearer_config, _RejectingVerifier
from tests.support.auth import scoped_agent_transport
from tests.support.disposable_lanes import record_safety
from tests.support.http import request
from tests import test_dokploy_compose_source_setup as source_fixture


class DirectorTestingSourceCompletionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.fixture = source_fixture.ComposeSourceSetupTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.store
        self.store.write_dokploy_target_record(
            self.fixture.target.model_copy(update={"domains": ()})
        )
        self.fixture.live.update(
            composeType="docker-compose",
            composeStatus="idle",
            isolatedDeployment=False,
            randomize=False,
            serviceNetworks=[],
            mounts=[],
            domains=[],
        )
        record_safety(self.store, "sample", "sample", "testing")
        self.inventory = self.enterContext(
            patch("control_plane.dokploy.api.dokploy_request", return_value=[])
        )
        self.app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=LaunchplaneAuthzPolicy(),
            bearer_identity_config=_local_operator_bearer_config().model_copy(
                update={
                    "local_operator_token": "",
                    "terminal_agent_token": "local-operator-token",
                    "terminal_agent_subject": "fixture-director",
                    "terminal_agent_token_label": "fixture-terminal",
                }
            ),
            record_store_factory=lambda: self.store,
            control_plane_root_path=self.fixture.root,
        )
        self.payload: dict[str, Any] = {
            "operation": "complete-compose-source",
            "context": "sample",
            "instance": "testing",
            "custom_git_branch": "a" * 40,
            "compose_path": "compose.runtime.yml",
            "mode": "apply",
            "reason": "Complete isolated source",
        }
        self.payload["confirmation"] = DokployTargetSetupEnvelope.model_validate(
            self.payload
        ).expected_confirmation
        self.in_source_transaction = False
        complete = self.store.complete_dokploy_compose_source

        def commit(**kwargs: Any) -> None:
            self.in_source_transaction = True
            try:
                complete(**kwargs)
            finally:
                self.in_source_transaction = False

        self.enterContext(
            patch.object(self.store, "complete_dokploy_compose_source", side_effect=commit)
        )

    async def post(self, *, key: str = "source-test", **changes: Any) -> Any:
        return await request(
            self.app,
            "POST",
            "/v1/dokploy-targets/setup",
            payload={**self.payload, **changes},
            headers={
                "Authorization": "Bearer local-operator-token",
                "Idempotency-Key": key,
            },
        )

    async def test_director_completes_immutable_source_and_replays_without_deployment(self) -> None:
        self.fixture.live["autoDeploy"] = True
        dry = await self.post(mode="dry-run")
        self.assertEqual(dry.status_code, 202, dry.text)
        self.assertEqual(self.fixture.calls, [])
        applied = await self.post()
        self.assertEqual(applied.status_code, 202, applied.text)
        self.assertEqual(len(self.fixture.calls), 1)
        self.assertEqual(self.fixture.live["customGitBranch"], self.payload["custom_git_branch"])
        self.assertIs(self.fixture.live["autoDeploy"], False)
        replay = await self.post()
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertEqual(len(self.fixture.calls), 1)
        self.assertEqual(self.store.list_deployment_records(), ())

    async def test_unknown_usage_alone_does_not_block_isolated_source_completion(self) -> None:
        record_safety(self.store, "sample", "sample", "testing", usage="unknown")
        dry = await self.post(mode="dry-run")
        self.assertEqual(dry.status_code, 202, dry.text)
        self.assertEqual(self.fixture.calls, [])
        applied = await self.post()
        self.assertEqual(applied.status_code, 202, applied.text)
        self.assertEqual(len(self.fixture.calls), 1)
        self.assertIs(self.fixture.live["autoDeploy"], False)
        self.assertEqual(self.store.list_deployment_records(), ())
        evidence = self.store.read_disposable_lane_safety("sample", "sample", "testing")
        assert evidence is not None
        self.assertEqual(evidence.usage, "unknown")

    async def test_positive_use_needed_data_or_shared_isolation_prevents_provider_write(
        self,
    ) -> None:
        for changes in (
            {"usage": "in_use"},
            {"data": "only_copy"},
            {"production_resources": "shared"},
        ):
            with self.subTest(changes=changes):
                record_safety(self.store, "sample", "sample", "testing", **changes)
                response = await self.post(key=str(changes))
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(self.fixture.calls, [])

    async def test_branch_or_shared_provider_placement_is_refused(self) -> None:
        dry = await self.post(mode="dry-run", custom_git_branch="main")
        self.assertEqual(dry.status_code, 400, dry.text)
        response = await self.post(custom_git_branch="main")
        self.assertEqual(response.status_code, 400, response.text)
        baseline = deepcopy(self.fixture.live)
        for changes in (
            {"mounts": [{"type": "bind"}]},
            {"composeStatus": "running"},
            {"domains": [{"host": "public.invalid"}]},
        ):
            with self.subTest(changes=changes):
                self.fixture.live = {**baseline, **changes}
                response = await self.post()
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(self.fixture.calls, [])

    async def test_scoped_transport_and_production_aliases_stay_refused(self) -> None:
        with scoped_agent_transport():
            response = await self.post()
        self.assertEqual(response.status_code, 403, response.text)
        response = await self.post(instance="prod")
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.fixture.calls, [])

    async def test_unknown_provider_inventory_never_reaches_update(self) -> None:
        self.inventory.return_value = {"items": [], "truncated": True}
        response = await self.post()
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.fixture.calls, [])

    async def test_provider_drift_during_inventory_refuses_update(self) -> None:
        def inventory(**_kwargs: Any) -> list[Any]:
            if self.in_source_transaction:
                self.fixture.live["serverId"] = "moved-server"
            return []

        self.inventory.side_effect = inventory
        response = await self.post()
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.fixture.calls, [])

    async def test_safety_drift_before_effect_refuses_update(self) -> None:
        read_safety = self.store.read_disposable_lane_safety
        inventory_observed = False

        def inventory(**_kwargs: Any) -> list[Any]:
            nonlocal inventory_observed
            inventory_observed = self.in_source_transaction
            return []

        self.inventory.side_effect = inventory

        def safety(*args: Any, **kwargs: Any) -> Any:
            result = read_safety(*args, **kwargs)
            if self.in_source_transaction and inventory_observed:
                assert result is not None
                return result.model_copy(update={"data": "only_copy"})
            return result

        self.enterContext(
            patch.object(self.store, "read_disposable_lane_safety", side_effect=safety)
        )
        response = await self.post()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.fixture.calls, [])

    async def test_unresolved_lane_operation_refuses_source_update(self) -> None:
        from tests.support.disposable_lanes import prepare_held_rebuild

        prepare_held_rebuild(self.store, "sample", "sample", prepare=False)
        response = await self.post()
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.fixture.calls, [])

    async def test_matching_physical_reservation_refuses_update(self) -> None:
        from control_plane.workflows.generic_web_deploy_provider import (
            generic_web_provider_target_key,
        )

        target_key = generic_web_provider_target_key(
            provider_id="dokploy",
            context="sample",
            instance="testing",
            target_type="compose",
            target_id=self.fixture.target_id.target_id,
        )
        reserved = self.store.reserve_mutation(
            scope="fixture",
            route_path="/fixture",
            idempotency_key="other-operation",
            request_fingerprint="fixture",
            lease_owner="worker",
            reconciliation_key="fixture",
            provider_target_key=target_key,
        )
        self.assertEqual(reserved.status, "acquired")
        response = await self.post()
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.fixture.calls, [])

    async def test_active_provider_work_refuses_update(self) -> None:
        for blocked_path, inventory in (
            (
                "/api/deployment.allByType",
                [
                    {
                        "composeId": self.fixture.target_id.target_id,
                        "deploymentId": "running",
                        "status": "running",
                    }
                ],
            ),
            (
                "/api/deployment.queueList",
                [{"data": {"composeId": self.fixture.target_id.target_id}, "state": "active"}],
            ),
            ("/api/schedule.list", [{"enabled": False}]),
        ):
            with self.subTest(inventory=inventory):
                self.inventory.side_effect = lambda **kwargs: (
                    inventory if kwargs["path"] == blocked_path else []
                )
                response = await self.post()
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(self.fixture.calls, [])

    async def test_reservation_appearing_during_final_inventory_refuses_update(self) -> None:
        from control_plane.workflows.generic_web_deploy_provider import (
            generic_web_provider_target_key,
        )

        target_key = generic_web_provider_target_key(
            provider_id="dokploy",
            context="sample",
            instance="testing",
            target_type="compose",
            target_id=self.fixture.target_id.target_id,
        )
        self.store.reserve_mutation(
            scope="fixture",
            route_path="/fixture",
            idempotency_key="racing-operation",
            request_fingerprint="fixture",
            lease_owner="worker",
            reconciliation_key="fixture",
            provider_target_key=target_key,
        )
        held = self.store.list_held_provider_target_reservations()
        visible = False

        def inventory(**_kwargs: Any) -> list[Any]:
            nonlocal visible
            visible = self.in_source_transaction
            return []

        self.inventory.side_effect = inventory
        with patch.object(
            self.store,
            "list_held_provider_target_reservations",
            side_effect=lambda: held if visible else (),
        ):
            response = await self.post()
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.fixture.calls, [])

    async def test_lost_executor_lock_is_coded_without_source_write(self) -> None:
        from contextlib import contextmanager
        from collections.abc import Iterator, Callable

        @contextmanager
        def lock(_name: str) -> Iterator[Callable[[], None]]:
            def check() -> None:
                if self.in_source_transaction:
                    raise RuntimeError("fixture lost executor lock")

            yield check

        with patch.object(self.store, "testing_replacement_execution_lock", side_effect=lock):
            response = await self.post()
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.fixture.calls, [])

    async def test_retained_custody_outlives_nonblocking_operation(self) -> None:
        from tests.support.disposable_lanes import prepare_held_rebuild

        operation = prepare_held_rebuild(self.store, "sample", "sample")
        self.store.write_odoo_stable_target_replacement_operation_record(
            operation.model_copy(update={"status": "completed"})
        )
        response = await self.post()
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.fixture.calls, [])

    async def test_exclusion_failure_is_coded_before_any_write(self) -> None:
        self.inventory.side_effect = RuntimeError("fixture lost exclusion connection")
        response = await self.post()
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.fixture.calls, [])

    async def test_partial_provider_write_keeps_records_and_refuses_duplicate_effect(self) -> None:
        before = self.store.read_dokploy_target_record(
            context_name="sample", instance_name="testing"
        )

        def lost_response(*args: Any) -> Any:
            self.fixture.mutate(*args)
            raise RuntimeError("fixture lost response after source change")

        with patch(
            "control_plane.dokploy_target_setup_http.mutate_dokploy_payload_for_target_setup",
            side_effect=lost_response,
        ):
            response = await self.post()
        self.assertEqual(response.status_code, 502, response.text)
        self.assertEqual(response.json()["error"]["code"], "dokploy_source_partial_outcome")
        self.assertEqual(
            self.store.read_dokploy_target_record(context_name="sample", instance_name="testing"),
            before,
        )
        for key in ("source-test", "different-key"):
            refused = await self.post(key=key)
            self.assertEqual(refused.status_code, 400, refused.text)
        self.assertEqual(len(self.fixture.calls), 1)

    async def test_active_testing_executor_refuses_without_writes(self) -> None:
        from contextlib import nullcontext

        with patch.object(
            self.store, "testing_replacement_execution_lock", return_value=nullcontext(None)
        ):
            response = await self.post()
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.fixture.calls, [])
