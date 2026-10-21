from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal
import unittest
from unittest.mock import patch

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.promotion_record import DeploymentEvidence
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.dokploy_target_setup_http import (
    CREATE_COMPOSE_SOURCE_LABEL,
    DokployTargetSetupEnvelope,
)
from control_plane.testing_event_deploy_recovery import testing_event_execution_lock_name
from control_plane.testing_lane_hold import (
    TestingHoldApplyRequest,
    apply_testing_hold_plan,
    build_testing_hold_plan,
    read_staff_testing_hold,
)
from control_plane.workflows.generic_web_deploy_provider import (
    dokploy_generic_web_provider_target_key,
)
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
    def database_url(self) -> str:
        return f"sqlite+pysqlite:///{self.root / 'state.db'}"

    def setUp(self) -> None:
        self.enterContext(
            patch("control_plane.workflows.dokploy_target_adoption.require_bridge_network_capacity")
        )
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.root = root
        self.store = PostgresRecordStore(database_url=self.database_url())
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

    async def post(self, *, idempotency_key: str = "first-testing-setup", **changes: Any) -> Any:
        return await request(
            self.app,
            "POST",
            "/v1/dokploy-targets/setup",
            payload={**self.payload, **changes},
            headers={
                "Authorization": "Bearer local-operator-token",
                "Idempotency-Key": idempotency_key,
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

    def register_testing_lane(self) -> None:
        if all(lane.instance != "testing" for lane in self.profile.lanes):
            self.profile = self.profile.model_copy(
                update={
                    "lanes": (
                        *self.profile.lanes,
                        self.profile.lanes[0].model_copy(update={"instance": "testing"}),
                    )
                }
            )
            self.store.write_product_profile_record(self.profile)

    def apply_testing_hold(self, purpose: Literal["staff_testing", "isolation_staging"]) -> None:
        """Hold the lane through the supported path, which relabels its target record."""
        dry_run = TestingHoldApplyRequest(
            product=self.profile.product,
            context=self.context,
            instance="testing",
            hold=True,
            purpose=purpose,
            reason="Stage the application lane.",
        )
        plan, _ = build_testing_hold_plan(
            record_store=self.store, request=dry_run, actor="operator"
        )
        apply_testing_hold_plan(
            record_store=self.store,
            request=dry_run.model_copy(
                update={"mode": "apply", "reviewed_plan_sha256": plan.plan_sha256}
            ),
            actor="operator",
        )

    def register_unused_testing_compose(
        self,
        *,
        created_by: str = CREATE_COMPOSE_SOURCE_LABEL,
        completed_source: bool = False,
        hold_purpose: Literal["staff_testing", "isolation_staging"] | None = "isolation_staging",
        source_type: Literal["raw", "github"] = "raw",
    ) -> ProviderTargetRecord:
        self.register_testing_lane()
        target = self.production.model_copy(
            update={"instance": "testing", "source_type": source_type, "source_label": created_by}
        )
        if completed_source:
            target = target.model_copy(update={"source_type": "git", "custom_git_branch": "a" * 40})
        target_id = self.production_id.model_copy(
            update={
                "instance": "testing",
                "target_id": "empty-testing-compose",
                "source_label": created_by,
            }
        )
        provider = ProviderTargetRecord.from_dokploy_records(
            target_record=target, target_id_record=target_id
        )
        self.store.write_dokploy_target_record(target)
        self.store.write_dokploy_target_id_record(target_id)
        self.store.write_provider_target_record(provider)
        if hold_purpose:
            self.apply_testing_hold(hold_purpose)
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
        created = {
            "application": {
                "applicationId": "created-application",
                "name": self.payload["target_name"],
                "environmentId": "created-environment",
                "serverId": self.payload["server_id"],
            },
            "compose": {
                "composeId": "created-compose",
                "name": "new-testing-target",
                "environmentId": "created-environment",
                "serverId": self.payload["server_id"],
                "sourceType": "github",
                "composePath": "./docker-compose.yml",
            },
        }
        self.created_targets = created
        self.enterContext(
            patch(
                "control_plane.dokploy_target_setup_http.fetch_dokploy_target_payload_for_setup",
                side_effect=lambda _host, _token, target_type, target_id: {
                    **created[target_type],
                    f"{target_type}Id": target_id,
                },
            )
        )

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

    async def test_director_creates_holds_then_replaces_an_unused_testing_target(self) -> None:
        compose = await self.post(
            operation="create-compose", target_name="new-testing-target", idempotency_key="compose"
        )
        self.assertEqual(compose.status_code, 202, compose.text)
        self.register_testing_lane()
        self.apply_testing_hold("isolation_staging")
        self.record_testing_deploy("fail")
        current = self.store.read_provider_target_record(
            context_name=self.context, instance_name="testing"
        )
        self.provider.reset_mock()
        plan = await self.post(
            mode="dry-run", state_volume_path="/var/lib/example", **self.expected(current)
        )
        self.assertEqual(plan.status_code, 202, plan.text)
        self.provider.assert_not_called()
        result = await self.post(**self.expected(current), idempotency_key="application")
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
        # The hold moves with the lane, so a deploy queued behind the lock still waits.
        hold = read_staff_testing_hold(record_store=self.store, context=self.context)
        self.assertIsNotNone(hold)
        assert hold is not None
        self.assertEqual(hold.purpose, "isolation_staging")
        self.assert_production_unchanged()

    async def test_replacement_requires_complete_live_empty_source_evidence(self) -> None:
        provider = self.register_unused_testing_compose(source_type="github")
        for changes in (
            {"repository": "configured-repository"},
            {"customGitSSHKeyId": "bound-key"},
            {"composeFile": "services: {}"},
            {"composeId": "different-compose"},
            {"environmentId": None},
        ):
            with self.subTest(changes=changes):
                with patch(
                    "control_plane.dokploy_target_setup_http.fetch_dokploy_target_payload_for_setup",
                    return_value={
                        **self.created_targets["compose"],
                        "composeId": provider.target_id,
                        **changes,
                    },
                ):
                    for storage in (False, True):
                        result = await self.post(
                            mode="dry-run" if storage else "apply",
                            state_volume_path="/var/lib/example" if storage else "",
                            **self.expected(provider),
                        )
                        self.assertEqual(result.status_code, 409 if storage else 400, result.text)
                self.provider.assert_not_called()
        with patch(
            "control_plane.dokploy_target_setup_http.fetch_dokploy_target_payload_for_setup",
            side_effect=TimeoutError("fixture unavailable"),
        ):
            for storage in (False, True):
                result = await self.post(
                    mode="dry-run" if storage else "apply",
                    state_volume_path="/var/lib/example" if storage else "",
                    **self.expected(provider),
                )
                self.assertEqual(result.status_code, 503, result.text)
                self.assertEqual(
                    result.json()["error"]["code"], "testing_target_source_evidence_unavailable"
                )
        self.provider.assert_not_called()
        self.assertEqual(self.current_testing_target_id(), provider.target_id)
        self.assert_production_unchanged()

    async def test_changed_or_unavailable_source_after_storage_plan_refuses_before_effects(
        self,
    ) -> None:
        provider = self.register_unused_testing_compose()
        intent = {**self.expected(provider), "state_volume_path": "/var/lib/example"}
        dry = await self.post(mode="dry-run", **intent)
        self.assertEqual(dry.status_code, 202, dry.text)
        plan = dry.json()["result"]
        self.created_targets["compose"]["repository"] = "configured-after-review"
        result = await self.post(
            **intent,
            state_volume_plan_id=plan["state_volume_plan_id"],
            expected_plan_sha256=plan["plan_sha256"],
        )
        self.assertEqual(result.status_code, 409, result.text)
        self.created_targets["compose"].pop("repository")
        with patch(
            "control_plane.dokploy_target_setup_http.fetch_dokploy_target_payload_for_setup",
            side_effect=TimeoutError("fixture unavailable after review"),
        ):
            unavailable = await self.post(
                **intent,
                state_volume_plan_id=plan["state_volume_plan_id"],
                expected_plan_sha256=plan["plan_sha256"],
            )
        self.assertEqual(unavailable.status_code, 503, unavailable.text)
        self.assertEqual(
            unavailable.json()["error"]["code"], "testing_target_source_evidence_unavailable"
        )
        self.provider.assert_not_called()
        self.assertEqual(self.current_testing_target_id(), provider.target_id)
        self.assert_production_unchanged()

    async def test_source_configured_during_replacement_stops_the_next_create(self) -> None:
        provider = self.register_unused_testing_compose()

        def create_project(*_args: Any) -> dict[str, str]:
            self.created_targets["compose"]["repository"] = "configured-during-create"
            return {"projectId": "created-project"}

        self.provider.side_effect = create_project
        result = await self.post(**self.expected(provider))
        self.assertEqual(result.status_code, 400, result.text)
        self.provider.assert_called_once()
        self.assertEqual(self.provider.call_args.args[2], PROJECT_CREATE_PATH)
        self.assertEqual(self.current_testing_target_id(), provider.target_id)
        self.assert_production_unchanged()

    async def test_replacement_needs_positive_evidence_nobody_used_the_lane(self) -> None:
        inventory = EnvironmentInventory(
            context=self.context,
            instance="testing",
            source_git_ref="abc123",
            deploy=DeploymentEvidence(
                target_name="empty-testing-compose",
                target_type="compose",
                deploy_mode="dokploy-compose-api",
                status="fail",
            ),
            updated_at=self.profile.updated_at,
            deployment_record_id="testing-deploy-fail",
        )
        for name, registration, served in (
            ("adopted target", {"created_by": "service:dokploy-targets:setup:adopt"}, False),
            ("source completed", {"completed_source": True}, False),
            ("staff testing", {"hold_purpose": "staff_testing"}, False),
            ("no hold", {"hold_purpose": None}, False),
            ("has served", {}, True),
        ):
            with self.subTest(name):
                provider = self.register_unused_testing_compose(**registration)
                if served:
                    self.store.write_environment_inventory(inventory)
                result = await self.post(**self.expected(provider))
                self.assertEqual(result.status_code, 400, result.text)
                self.provider.assert_not_called()
                self.assertEqual(self.current_testing_target_id(), provider.target_id)

    async def test_replacement_waits_for_a_running_testing_deploy(self) -> None:
        provider = self.register_unused_testing_compose()
        lock_name = testing_event_execution_lock_name(
            dokploy_generic_web_provider_target_key(provider)
        )
        with self.store.testing_replacement_execution_lock(lock_name) as held:
            self.assertIsNotNone(held)
            result = await self.post(**self.expected(provider))
        self.assertEqual(result.status_code, 400, result.text)
        self.provider.assert_not_called()
        self.assertEqual(self.current_testing_target_id(), provider.target_id)

    async def test_a_deploy_passing_mid_replacement_keeps_the_current_target(self) -> None:
        provider = self.register_unused_testing_compose()
        created = {
            PROJECT_CREATE_PATH: {"projectId": "created-project"},
            ENVIRONMENT_CREATE_PATH: {"environmentId": "created-environment"},
            APPLICATION_CREATE_PATH: {"applicationId": "created-application"},
        }

        def create(_host: str, _token: str, path: str, _payload: object) -> dict[str, str]:
            if path == APPLICATION_CREATE_PATH:
                self.record_testing_deploy("pass")
            return created[path]

        self.provider.side_effect = create
        result = await self.post(**self.expected(provider))
        self.assertEqual(result.status_code, 400, result.text)
        self.assertEqual(self.current_testing_target_id(), provider.target_id)
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
