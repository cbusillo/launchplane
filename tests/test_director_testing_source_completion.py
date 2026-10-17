"""Director transport uses the existing empty-source operation, without a grant."""

from copy import deepcopy
from typing import Any
import unittest
from unittest.mock import patch

from control_plane.http_app import create_launchplane_fastapi_app
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
            bearer_identity_config=_local_operator_bearer_config(),
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
            "confirmation": "APPLY DOKPLOY TARGET SETUP",
            "reason": "Complete isolated source",
        }

    async def post(self, **changes: Any) -> Any:
        return await request(
            self.app,
            "POST",
            "/v1/dokploy-targets/setup",
            payload={**self.payload, **changes},
            headers={
                "Authorization": "Bearer local-operator-token",
                "Idempotency-Key": "source-test",
            },
        )

    async def test_director_completes_immutable_source_and_replays_without_deployment(self) -> None:
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
        dry = await self.post(mode="dry-run")
        self.assertEqual(dry.status_code, 202, dry.text)
        self.assertEqual(self.fixture.calls, [])
        applied = await self.post()
        self.assertEqual(applied.status_code, 202, applied.text)
        self.assertEqual([path for path, _ in self.fixture.calls], ["/api/compose.update"])
        self.assertEqual(self.fixture.live["customGitBranch"], self.payload["custom_git_branch"])
        self.assertIs(self.fixture.live["autoDeploy"], False)
        replay = await self.post()
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertEqual(len(self.fixture.calls), 1)
        self.assertEqual(self.store.list_deployment_records(), ())

    async def test_unknown_use_data_or_isolation_never_reaches_provider_write(self) -> None:
        record_safety(self.store, "sample", "sample", "testing", usage="unknown")
        dry = await self.post(mode="dry-run")
        self.assertEqual(dry.status_code, 202, dry.text)
        self.assertEqual(self.fixture.calls, [])
        for changes in (
            {"usage": "unknown"},
            {"usage": "in_use"},
            {"data": "only_copy"},
            {"production_resources": "shared"},
        ):
            with self.subTest(changes=changes):
                record_safety(self.store, "sample", "sample", "testing", **changes)
                response = await self.post()
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(self.fixture.calls, [])

    async def test_branch_or_shared_provider_placement_is_refused(self) -> None:
        response = await self.post(custom_git_branch="main")
        self.assertEqual(response.status_code, 400, response.text)
        baseline = deepcopy(self.fixture.live)
        for changes in (
            {"mounts": [{"type": "bind"}]},
            {"autoDeploy": True},
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
        count = 0

        def inventory(**_kwargs: Any) -> list[Any]:
            nonlocal count
            count += 1
            if count > 3:
                self.fixture.live["serverId"] = "moved-server"
            return []

        self.inventory.side_effect = inventory
        response = await self.post()
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.fixture.calls, [])

    async def test_safety_drift_before_effect_refuses_update(self) -> None:
        count = 0

        def inventory(**_kwargs: Any) -> list[Any]:
            nonlocal count
            count += 1
            if count == 3:
                record_safety(self.store, "sample", "sample", "testing", data="only_copy")
            return []

        self.inventory.side_effect = inventory
        response = await self.post()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.fixture.calls, [])

    async def test_active_testing_executor_refuses_without_writes(self) -> None:
        from contextlib import nullcontext

        with patch.object(
            self.store, "testing_replacement_execution_lock", return_value=nullcontext(None)
        ):
            response = await self.post()
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.fixture.calls, [])
