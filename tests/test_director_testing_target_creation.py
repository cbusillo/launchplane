from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal
import unittest
from unittest.mock import patch

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.promotion_record import DeploymentEvidence
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.dokploy_target_setup_http import DokployTargetSetupEnvelope
from control_plane.workflows.dokploy_target_adoption import (
    APPLICATION_CREATE_PATH,
    COMPOSE_CREATE_PATH,
    ENVIRONMENT_CREATE_PATH,
    PROJECT_CREATE_PATH,
)
from control_plane.service_auth import (
    BearerIdentityConfig,
    LaunchplaneAuthzPolicy,
    TerminalAgentPolicyRule,
)
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import _local_operator_bearer_config, _RejectingVerifier
from tests.support.auth import scoped_agent_transport
from tests.support.http import request
from tests.support.profiles import _generic_site_profile_payload


class _DirectorTestingSetupFixture(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.root = root
        self.store = PostgresRecordStore(database_url=f"sqlite+pysqlite:///{root / 'state.db'}")
        self.store.ensure_schema()
        self.addCleanup(self.store.close)
        profile = LaunchplaneProductProfileRecord.model_validate(_generic_site_profile_payload())
        self.profile = profile.model_copy(
            update={
                "production_use": "live",
                "lanes": tuple(lane for lane in profile.lanes if lane.instance == "prod"),
            }
        )
        self.store.write_product_profile_record(self.profile)
        self.context = self.profile.lanes[0].context
        self.production = DokployTargetRecord(
            context=self.context, instance="prod", updated_at=self.profile.updated_at
        )
        self.production_id = DokployTargetIdRecord(
            context=self.context,
            instance="prod",
            target_id="production-compose",
            updated_at=self.profile.updated_at,
        )
        self.production_provider = ProviderTargetRecord.from_dokploy_records(
            target_record=self.production, target_id_record=self.production_id
        )
        self.store.write_dokploy_target_record(self.production)
        self.store.write_dokploy_target_id_record(self.production_id)
        self.store.write_provider_target_record(self.production_provider)
        self.app = self.make_app(_local_operator_bearer_config())
        self.payload: dict[str, Any] = {
            "mode": "apply",
            "operation": "create-compose",
            "context": self.context,
            "instance": "testing",
            "target_name": "new-testing-target",
            "project_name": "new-testing-project",
            "environment_name": "testing",
            "server_id": "testing-server",
            "healthcheck_path": "/health",
            "reason": "Stage isolated empty testing resources.",
        }
        self.payload["confirmation"] = DokployTargetSetupEnvelope.model_validate(
            self.payload
        ).expected_confirmation
        self.provider = self.enterContext(
            patch(
                "control_plane.dokploy_target_setup_http.mutate_dokploy_payload_for_target_setup",
                side_effect=lambda _host, _token, path, _payload: {
                    PROJECT_CREATE_PATH: {"projectId": "created-project"},
                    ENVIRONMENT_CREATE_PATH: {"environmentId": "created-environment"},
                    COMPOSE_CREATE_PATH: {"composeId": "created-compose"},
                    APPLICATION_CREATE_PATH: {"applicationId": "created-application"},
                }[path],
            )
        )
        self.enterContext(
            patch(
                "control_plane.dokploy_target_setup_http.dokploy_source.read_dokploy_config",
                return_value=("https://provider.example.invalid", "fixture-token"),
            )
        )
        self.enterContext(
            patch(
                "control_plane.dokploy_target_setup_http.fetch_dokploy_target_payload_for_setup",
                return_value={
                    "composeId": "created-compose",
                    "name": self.payload["target_name"],
                    "environmentId": "created-environment",
                    "serverId": self.payload["server_id"],
                    "sourceType": "raw",
                },
            )
        )

    def make_app(
        self, config: BearerIdentityConfig, policy: LaunchplaneAuthzPolicy | None = None
    ) -> Any:
        return create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=policy or LaunchplaneAuthzPolicy(),
            bearer_identity_config=config,
            record_store_factory=lambda: self.store,
            control_plane_root_path=self.root,
        )

    async def post(self, **changes: Any) -> Any:
        return await request(
            self.app,
            "POST",
            "/v1/dokploy-targets/setup",
            payload={**self.payload, **changes},
            headers={
                "Authorization": "Bearer local-operator-token",
                "Idempotency-Key": "first-testing-setup",
            },
        )

    def assert_production_unchanged(self) -> None:
        self.assertEqual(self.store.read_product_profile_record(self.profile.product), self.profile)
        self.assertEqual(
            self.store.read_dokploy_target_record(context_name=self.context, instance_name="prod"),
            self.production,
        )
        self.assertEqual(
            self.store.read_provider_target_record(context_name=self.context, instance_name="prod"),
            self.production_provider,
        )
        self.assertEqual(self.store.list_deployment_records(instance_name="prod"), ())
        self.assertEqual(self.store.list_promotion_records(), ())


class DirectorTestingTargetCreationTests(_DirectorTestingSetupFixture):
    async def test_director_stages_new_empty_target_without_grants_and_replays(self) -> None:
        dry = await self.post(mode="dry-run")
        self.assertEqual(dry.status_code, 202, dry.text)
        self.provider.assert_not_called()
        first = await self.post()
        self.assertEqual(first.status_code, 202, first.text)
        effects = self.provider.call_args_list.copy()
        self.assertEqual(
            [call.args[2] for call in effects],
            [PROJECT_CREATE_PATH, ENVIRONMENT_CREATE_PATH, COMPOSE_CREATE_PATH],
        )
        self.assertEqual(
            self.store.read_provider_target_record(
                context_name=self.context, instance_name=self.payload["instance"]
            ).target_id,
            "created-compose",
        )
        replay = await self.post()
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertEqual(self.provider.call_args_list, effects)
        self.assert_production_unchanged()

    async def test_existing_resources_production_and_executable_inputs_are_refused(self) -> None:
        for changes in (
            {"instance": "prod"},
            {"instance": "production"},
            {"instance": "pr-42"},
            {"operation": "adopt", "target_id": self.production_id.target_id},
            {"project_id": "production-project"},
            {"environment_id": "production-environment"},
            {"target_id": self.production_id.target_id},
            {
                "expected_current_provider_target": self.production_provider.to_deployed_target_reference().model_dump(
                    mode="json"
                )
            },
            {"domains": ["production.example.invalid"]},
            {"runtime_port": 8787, "domains": ["testing.example.invalid"]},
            {"source_type": "git"},
            {"custom_git_branch": "main", "compose_path": "docker-compose.yml"},
        ):
            with self.subTest(changes=changes):
                result = await self.post(**changes)
                self.assertIn(result.status_code, (400, 403, 422), result.text)
                self.provider.assert_not_called()
                self.assert_production_unchanged()

    async def test_scoped_agent_cannot_use_director_first_setup(self) -> None:
        with scoped_agent_transport():
            result = await self.post()
        self.assertEqual(result.status_code, 403, result.text)
        self.provider.assert_not_called()

    async def test_read_role_can_still_plan_a_target_with_routes_without_apply_authority(
        self,
    ) -> None:
        result = await self.post(
            mode="dry-run", runtime_port=8787, domains=["testing.example.invalid"]
        )
        self.assertEqual(result.status_code, 202, result.text)
        self.assertFalse(result.json()["result"]["applied"])
        self.provider.assert_not_called()
        self.assert_production_unchanged()

    async def test_director_terminal_stages_testing_but_cannot_create_production(self) -> None:
        self.app = self.make_app(
            _local_operator_bearer_config().model_copy(
                update={
                    "local_operator_token": "",
                    "terminal_agent_token": "local-operator-token",
                    "terminal_agent_subject": "fixture-director",
                    "terminal_agent_token_label": "fixture-terminal",
                }
            ),
            LaunchplaneAuthzPolicy(
                schema_version=2,
                terminal_agents=(
                    TerminalAgentPolicyRule(
                        subjects=("fixture-director",),
                        token_labels=("fixture-terminal",),
                        products=(self.profile.product,),
                        contexts=(self.context,),
                        instances=("testing", "prod"),
                        actions=("dokploy_target.lane_setup",),
                    ),
                ),
            ),
        )
        denied = await self.post(instance="prod")
        self.assertEqual(denied.status_code, 403, denied.text)
        self.provider.assert_not_called()
        source = await self.post(custom_git_branch="main", compose_path="docker-compose.yml")
        self.assertEqual(source.status_code, 400, source.text)
        self.provider.assert_not_called()
        result = await self.post()
        self.assertEqual(result.status_code, 202, result.text)
        self.assert_production_unchanged()

    async def test_partial_target_authority_and_registered_lane_are_not_first_setup(self) -> None:
        target = self.production.model_copy(update={"instance": "testing"})
        self.store.write_dokploy_target_record(target)
        result = await self.post()
        self.assertEqual(result.status_code, 400, result.text)
        self.provider.assert_not_called()
        self.store.delete_dokploy_target_record(expected_record=target)
        self.store.write_product_profile_record(
            self.profile.model_copy(
                update={
                    "lanes": (
                        *self.profile.lanes,
                        self.profile.lanes[0].model_copy(update={"instance": "testing"}),
                    )
                }
            )
        )
        result = await self.post()
        self.assertEqual(result.status_code, 400, result.text)
        self.provider.assert_not_called()

    async def test_owner_change_between_creates_stops_before_next_provider_effect(self) -> None:
        def create_project(*_args: Any) -> dict[str, str]:
            self.store.write_product_profile_record(
                self.profile.model_copy(
                    update={"product": "other-product", "repository": "example/other-product"}
                )
            )
            return {"projectId": "created-project"}

        self.provider.side_effect = create_project
        result = await self.post()
        self.assertEqual(result.status_code, 400, result.text)
        self.provider.assert_called_once()
        self.assert_production_unchanged()


class DirectorTestingApplicationLaneTests(_DirectorTestingSetupFixture):
    """The conventional application shape, including replacing a target that never ran."""

    def setUp(self) -> None:
        super().setUp()
        self.payload = {
            **self.payload,
            "operation": "create-application",
            "target_name": "new-testing-app",
        }
        self.payload["confirmation"] = DokployTargetSetupEnvelope.model_validate(
            self.payload
        ).expected_confirmation
        self.enterContext(
            patch(
                "control_plane.dokploy_target_setup_http.fetch_dokploy_target_payload_for_setup",
                return_value={
                    "applicationId": "created-application",
                    "name": self.payload["target_name"],
                    "environmentId": "created-environment",
                    "serverId": self.payload["server_id"],
                },
            )
        )

    def register_unused_testing_compose(self) -> ProviderTargetRecord:
        self.profile = self.profile.model_copy(
            update={
                "lanes": (
                    *self.profile.lanes,
                    self.profile.lanes[0].model_copy(update={"instance": "testing"}),
                )
            }
        )
        self.store.write_product_profile_record(self.profile)
        target = self.production.model_copy(update={"instance": "testing"})
        target_id = self.production_id.model_copy(
            update={"instance": "testing", "target_id": "empty-testing-compose"}
        )
        provider = ProviderTargetRecord.from_dokploy_records(
            target_record=target, target_id_record=target_id
        )
        self.store.write_dokploy_target_record(target)
        self.store.write_dokploy_target_id_record(target_id)
        self.store.write_provider_target_record(provider)
        return provider

    def record_testing_deploy(self, status: Literal["pass", "fail"]) -> None:
        self.store.write_deployment_record(
            DeploymentRecord(
                record_id=f"testing-deploy-{status}",
                context=self.context,
                instance="testing",
                source_git_ref="abc123",
                deploy=DeploymentEvidence(
                    target_name="empty-testing-compose",
                    target_type="compose",
                    deploy_mode="dokploy-compose-api",
                    status=status,
                ),
            )
        )

    def current_testing_target_id(self) -> str:
        return self.store.read_provider_target_record(
            context_name=self.context, instance_name="testing"
        ).target_id

    def expected(self, provider: ProviderTargetRecord) -> dict[str, Any]:
        return {
            "expected_current_provider_target": provider.to_deployed_target_reference().model_dump(
                mode="json"
            )
        }

    async def test_director_creates_a_first_application_lane_without_grants(self) -> None:
        dry = await self.post(mode="dry-run")
        self.assertEqual(dry.status_code, 202, dry.text)
        self.provider.assert_not_called()
        first = await self.post()
        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(
            [call.args[2] for call in self.provider.call_args_list],
            [PROJECT_CREATE_PATH, ENVIRONMENT_CREATE_PATH, APPLICATION_CREATE_PATH],
        )
        self.assertEqual(self.current_testing_target_id(), "created-application")
        self.assert_production_unchanged()

    async def test_director_replaces_a_testing_target_that_never_deployed(self) -> None:
        provider = self.register_unused_testing_compose()
        self.record_testing_deploy("fail")
        result = await self.post(**self.expected(provider))
        self.assertEqual(result.status_code, 202, result.text)
        self.assertEqual(
            [call.args[2] for call in self.provider.call_args_list],
            [PROJECT_CREATE_PATH, ENVIRONMENT_CREATE_PATH, APPLICATION_CREATE_PATH],
        )
        replaced = self.store.read_provider_target_record(
            context_name=self.context, instance_name="testing"
        )
        self.assertEqual(
            (replaced.target_id, replaced.provider_target_type),
            ("created-application", "application"),
        )
        self.assert_production_unchanged()

    async def test_replacement_needs_the_exact_target_of_a_lane_that_never_ran(self) -> None:
        provider = self.register_unused_testing_compose()
        stale = provider.model_copy(update={"target_id": "another-compose"})
        for changes in ({}, self.expected(stale)):
            with self.subTest(changes=changes):
                result = await self.post(**changes)
                self.assertEqual(result.status_code, 400, result.text)
        self.record_testing_deploy("pass")
        result = await self.post(**self.expected(provider))
        self.assertEqual(result.status_code, 400, result.text)
        self.provider.assert_not_called()
        self.assertEqual(self.current_testing_target_id(), provider.target_id)
        self.assert_production_unchanged()

    async def test_production_existing_placement_and_routes_are_refused(self) -> None:
        for changes in (
            {"instance": "prod"},
            {"instance": "pr-42"},
            {"project_id": "production-project"},
            {"environment_id": "production-environment"},
            {"server_id": ""},
            {"domains": ["testing.example.invalid"]},
            self.expected(self.production_provider),
        ):
            with self.subTest(changes=changes):
                result = await self.post(**changes)
                self.assertIn(result.status_code, (400, 403, 422), result.text)
        self.provider.assert_not_called()
        self.assert_production_unchanged()

    async def test_partial_target_authority_without_the_exact_target_is_refused(self) -> None:
        target = self.production.model_copy(update={"instance": "testing"})
        self.store.write_dokploy_target_record(target)
        result = await self.post()
        self.assertEqual(result.status_code, 400, result.text)
        self.provider.assert_not_called()

    async def test_scoped_agent_cannot_use_director_first_setup(self) -> None:
        with scoped_agent_transport():
            result = await self.post()
        self.assertEqual(result.status_code, 403, result.text)
        self.provider.assert_not_called()
