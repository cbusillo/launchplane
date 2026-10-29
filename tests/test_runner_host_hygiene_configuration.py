import unittest
import os
from control_plane import secrets
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from unittest.mock import Mock, patch

from fastapi import FastAPI
from pydantic import ValidationError

from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.secret_record import SecretScope
from control_plane.http_app import LaunchplaneErrorResponse, _launchplane_http_error
from control_plane.http_routes.runner_host_hygiene_configuration import (
    DISPATCH_ROUTE,
    METADATA_ROUTE,
    register_runner_host_hygiene_configuration_routes,
)
from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from control_plane.runner_host_hygiene_configuration import (
    PRIVATE_KEY_BINDING,
    SETTINGS_KEY,
    RunnerHostHygieneConfiguration,
    RunnerHostHygieneDispatchIntent,
    build_runner_host_hygiene_dispatch,
    load_runner_host_hygiene_configuration,
    require_current_hygiene_configuration,
)
from control_plane.runner_host_hygiene_metadata import (
    READ_PERMISSIONS,
    RunnerHostHygieneMetadataRequest,
    read_runner_host_hygiene_metadata,
)
from control_plane import runner_host_hygiene_metadata as metadata
from control_plane.service_auth import GitHubActionsIdentity
from control_plane.service_runtime_settings import ServiceRuntimeSettingRequest
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.runtime_environments import RuntimeEnvironmentRecordStore
from control_plane.workflows import runner_host_hygiene_service_executor as client
from control_plane.workflows.runner_host_hygiene_executor import RunnerHostHygieneExecutorRequest
from control_plane.workflows.runner_host_hygiene_executor import RunnerHostHygieneExecutorResult
from tests.support.http import request as http_request
from tests.support.stores import sqlite_database_url


def configuration() -> RunnerHostHygieneConfiguration:
    return RunnerHostHygieneConfiguration.model_validate(
        {
            "host_name": "synthetic-host",
            "execution_lane": "ops",
            "service_user": "ops-user",
            "repository_scope": "example/control",
            "github_app_client_id": "synthetic-app",
            "retained_warm_builders": ["warm"],
            "github_idle_bindings": [
                {
                    "repository_scope": "example/other",
                    "execution_lane": "build",
                    "runner_name": "build-runner",
                    "service_unit": "build.service",
                },
                {
                    "repository_scope": "example/control",
                    "execution_lane": "ops",
                    "runner_name": "ops-runner",
                    "service_unit": "ops.service",
                },
            ],
            "runner_workdir_roots": [{"key": "work", "path": "/srv/runners"}],
            "generated_run_cache_roots": [
                {
                    "key": "generated",
                    "path": "/srv/generated",
                    "source_repository": "example/other",
                    "high_water_bytes": 200,
                    "low_water_bytes": 100,
                    "minimum_age_hours": 24,
                    "cooldown_hours": 24,
                }
            ],
            "buildkit_cache_budgets": {"warm": 1024},
            "default_cache_budget_bytes": 16 * 1024**3,
            "allowed_buildkit_state_volumes": ["buildx_buildkit_retired0_state"],
        }
    )


def workflow_identity() -> GitHubActionsIdentity:
    return GitHubActionsIdentity(
        repository="example/control",
        repository_owner="example",
        workflow_ref="hygiene",
        job_workflow_ref="",
        ref="refs/heads/main",
        ref_type="branch",
        event_name="workflow_dispatch",
        environment="",
        subject="synthetic",
        sha="synthetic",
        raw_claims={"run_id": "42"},
    )


class HygieneConfigurationFixture(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = PostgresRecordStore(
            database_url=sqlite_database_url(Path(directory.name) / "state.sqlite3")
        )
        self.store.ensure_schema()
        self.addCleanup(self.store.close)
        self.configuration = configuration()
        self.write_configuration()

    def write_configuration(self, updated_at: str = "2026-10-29T00:00:00Z") -> None:
        self.store.write_runtime_environment_record(
            RuntimeEnvironmentRecord(
                scope="context",
                context="launchplane",
                updated_at=updated_at,
                env={SETTINGS_KEY: self.configuration.model_dump_json()},
            )
        )

    def dispatch(self, **intent: object) -> tuple[RunnerHostHygieneExecutorRequest, ...]:
        configured, digest = load_runner_host_hygiene_configuration(self.store)
        return build_runner_host_hygiene_dispatch(
            configuration=configured,
            configuration_sha256=digest,
            intent=RunnerHostHygieneDispatchIntent.model_validate(
                {
                    "audit_record_key": "audit/current",
                    "current_runner_name": "ops-runner",
                    **intent,
                }
            ),
        ).requests


class HygieneConfigurationTests(HygieneConfigurationFixture):
    def test_only_one_exact_service_key_binding_can_supply_the_app_key(self) -> None:
        with patch.dict(
            os.environ, {secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "synthetic-master"}
        ):
            scopes: tuple[tuple[SecretScope, str], ...] = (
                ("global", ""),
                ("context", "launchplane"),
            )
            for scope, context in scopes:
                secrets.write_secret_value(
                    record_store=self.store,
                    scope=scope,
                    integration=secrets.LAUNCHPLANE_SERVICE_SECRET_INTEGRATION,
                    name="hygiene-key",
                    plaintext_value="synthetic-private-key",
                    binding_key=PRIVATE_KEY_BINDING,
                    context_name=context,
                )
                if scope == "global":
                    with self.assertRaises(ValueError):
                        metadata._secret_value(self.store)
            self.assertEqual(metadata._secret_value(self.store), "synthetic-private-key")
            secrets.write_secret_value(
                record_store=self.store,
                scope="context",
                context_name="launchplane",
                integration=secrets.LAUNCHPLANE_SERVICE_SECRET_INTEGRATION,
                name="ambiguous-key",
                plaintext_value="another-synthetic-key",
                binding_key=PRIVATE_KEY_BINDING,
            )
            with self.assertRaises(ValueError):
                metadata._secret_value(self.store)

    def test_consumer_attempts_every_action_and_retains_terminal_acceptance(self) -> None:
        intent = RunnerHostHygieneDispatchIntent(
            mode="maintenance",
            audit_record_key="audit/current",
            current_runner_name="ops-runner",
        )
        dispatch = build_runner_host_hygiene_dispatch(
            configuration=self.configuration,
            configuration_sha256="a" * 64,
            intent=intent,
        )
        accepted = RunnerHostHygieneExecutorResult(
            status="completed",
            audit_record_key="synthetic",
            planned_response={},
            terminal_response={"accepted": True, "maintenance_completed": True},
            message="done",
        )
        with (
            TemporaryDirectory() as directory,
            patch(
                "control_plane.workflows.runner_host_hygiene_service_executor.validate_local_executor_environment"
            ),
            patch(
                "control_plane.workflows.runner_host_hygiene_service_executor.execute_runner_host_hygiene_executor",
                side_effect=[ValueError("blocked"), accepted, accepted, accepted],
            ) as execute,
        ):
            results = client.execute_service_hygiene_dispatch(
                intent=intent,
                post=lambda path, payload: dispatch.model_dump(mode="json"),
                execution_lane=self.configuration.execution_lane,
                repository_scope=self.configuration.repository_scope,
                service_url="https://service.invalid",
                bearer_token_provider=lambda: "synthetic",
                audit_spool_root=Path(directory),
                artifact_directory=Path(directory),
            )
        self.assertEqual(execute.call_count, len(dispatch.requests))
        self.assertEqual(results[0]["status"], "failed")
        self.assertEqual(results[-1]["terminal_response"], accepted.terminal_response)

    def test_record_roundtrip_and_revision_bound_reads(self) -> None:
        configured, digest = load_runner_host_hygiene_configuration(self.store)
        self.assertEqual(configured, self.configuration)
        self.assertEqual(require_current_hygiene_configuration(self.store, digest), configured)
        self.write_configuration("2026-10-29T00:01:00Z")
        with self.assertRaises(ValueError):
            require_current_hygiene_configuration(self.store, digest)
        request = ServiceRuntimeSettingRequest(
            key=SETTINGS_KEY,
            value=configured.model_dump_json(),
            reason="Synthetic source qualification",
        )
        self.assertEqual(
            RunnerHostHygieneConfiguration.model_validate_json(request.value), configured
        )

    def test_missing_or_ambiguous_authority_never_falls_back(self) -> None:
        store = Mock()
        for records in ((), self.store.list_runtime_environment_records() * 2):
            with self.subTest(records=len(records)):
                store.list_runtime_environment_records.return_value = records
                with self.assertRaises(ValueError):
                    load_runner_host_hygiene_configuration(
                        cast(RuntimeEnvironmentRecordStore, cast(object, store))
                    )

    def test_existing_executor_guards_reject_invalid_configuration(self) -> None:
        invalid_changes: tuple[dict[str, object], ...] = (
            {"default_cache_budget_bytes": 1},
            {"buildkit_cache_budgets": {"default": 1}},
            {"runner_workdir_roots": [{"key": "work", "path": "/srv/../root"}]},
            {"github_idle_bindings": []},
            {"private_key": "not-configuration"},
        )
        for changes in invalid_changes:
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                RunnerHostHygieneConfiguration.model_validate(
                    {**self.configuration.model_dump(), **changes}
                )

    def test_schedule_and_manual_actions_preserve_configuration(self) -> None:
        (report,) = self.dispatch()
        self.assertFalse(report.mutate)
        self.assertEqual(report.max_used_space_bytes, self.configuration.default_cache_budget_bytes)
        maintenance = self.dispatch(mode="maintenance")
        self.assertEqual(
            [item.action for item in maintenance],
            [
                "prune_docker_cache",
                "prune_dangling_images",
                "prune_docker_cache",
                "prune_generated_run_cache",
            ],
        )
        self.assertEqual(len({item.audit_record_key for item in maintenance}), len(maintenance))
        for item in maintenance:
            self.assertTrue(item.mutate)
            self.assertEqual(item.github_idle_bindings, self.configuration.github_idle_bindings)
            self.assertEqual(item.retained_warm_builders, self.configuration.retained_warm_builders)
            self.assertEqual(
                item.buildkit_cache_budgets,
                {
                    **self.configuration.buildkit_cache_budgets,
                    "default": self.configuration.default_cache_budget_bytes,
                },
            )
        (manual,) = self.dispatch(
            mode="manual",
            action="remove_buildkit_state_volumes",
            target_buildkit_state_volumes=["buildx_buildkit_retired0_state"],
        )
        self.assertEqual(
            manual.allowed_buildkit_state_volumes, self.configuration.allowed_buildkit_state_volumes
        )
        with self.assertRaises(ValueError):
            self.dispatch(mode="manual", target_buildkit_builder="unconfigured")
        with self.assertRaises(ValidationError):
            self.dispatch(mode="maintenance", mutate=True)

    def test_service_reader_preserves_binding_order_and_scopes_runs(self) -> None:
        (request,) = self.dispatch()
        calls: list[dict[str, object]] = []
        _, digest = load_runner_host_hygiene_configuration(self.store)

        def post(_path: str, payload: dict[str, object]) -> object:
            calls.append(payload)
            selected = RunnerHostHygieneMetadataRequest.model_validate(payload)
            return {
                "configuration_sha256": digest,
                "run_states": {run_id: "completed" for run_id in selected.run_ids},
            }

        idle, runs = client.build_service_hygiene_readers(
            request=request, configuration_sha256=digest, post=post
        )
        idle("example/control", "ops", "ops-runner", True)
        self.assertEqual(
            self.configuration.github_idle_bindings[
                RunnerHostHygieneMetadataRequest.model_validate(calls[-1]).binding_index
            ].runner_name,
            "ops-runner",
        )
        self.assertEqual(runs("example/other", (21, 22)), {21: "completed", 22: "completed"})
        with self.assertRaises(ValueError):
            runs("unconfigured/repo", (21,))
        with self.assertRaises(ValueError):
            idle("example/control", "ops", "ops-runner", False)


class HygieneMetadataTests(HygieneConfigurationFixture):
    def test_scope_is_verified_and_token_stays_inside_service(self) -> None:
        configured = configuration()
        calls: list[dict[str, object]] = []

        def api(**call: object) -> object:
            calls.append(call)
            path = call["path"]
            assert isinstance(path, str)
            if path == "/app":
                return {"id": 8, "client_id": configured.github_app_client_id}
            if path.endswith("/installation"):
                return {
                    "id": 9,
                    "app_id": 8,
                    "account": {"login": "example"},
                    "permissions": READ_PERMISSIONS,
                }
            if path.endswith("/access_tokens"):
                return {
                    "token": "service-private-token",
                    "permissions": READ_PERMISSIONS,
                    "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
                }
            return None

        request = RunnerHostHygieneMetadataRequest(
            configuration_sha256="a" * 64,
            current_runner_name="ops-runner",
            kind="run_states",
            cache_key="generated",
            run_ids=(21,),
        )
        with (
            patch.object(metadata, "_secret_value", return_value="service-private-key"),
            patch(
                "control_plane.runner_host_hygiene_metadata.jwt.encode",
                return_value="service-private-jwt",
            ),
            patch("control_plane.runner_host_hygiene_metadata.github_api_request", side_effect=api),
            patch(
                "control_plane.runner_host_hygiene_metadata.read_github_app_installation_repositories",
                return_value=frozenset({"example/control", "example/other"}),
            ) as scope,
            patch(
                "control_plane.runner_host_hygiene_metadata.build_github_run_state_reader",
                return_value=lambda repository, ids: {run_id: "completed" for run_id in ids},
            ),
        ):
            result = read_runner_host_hygiene_metadata(
                configuration=configured, store=self.store, request=request, current_run_id=42
            )
            self.assertEqual(result.run_states, {21: "completed"})
            self.assertNotIn("service-private", result.model_dump_json())
            self.assertEqual(calls[-1]["method"], "DELETE")
            scope.return_value = frozenset({"example/control", "example/other", "example/extra"})
            with self.assertRaises(ValueError):
                read_runner_host_hygiene_metadata(
                    configuration=configured, store=self.store, request=request, current_run_id=42
                )
            self.assertEqual(calls[-1]["method"], "DELETE")


class HygieneRouteTests(HygieneConfigurationFixture, unittest.IsolatedAsyncioTestCase):
    async def test_authorized_workflow_gets_dispatch_without_secrets(self) -> None:
        identity = workflow_identity()
        allowed = True

        def allows(**_kwargs: object) -> bool:
            return allowed

        app = FastAPI()
        register_runner_host_hygiene_configuration_routes(
            cast(ApiRouteRegistrar, cast(object, app)),
            dependencies=ReadRouteDependencies(
                read_identity=lambda: identity,
                get_record_store=lambda: self.store,
                next_trace_id=lambda: "test-trace",
                authorization_allows=allows,
                http_error=_launchplane_http_error,
                error_response_model=LaunchplaneErrorResponse,
            ),
        )
        payload = {"audit_record_key": "audit/current", "current_runner_name": "ops-runner"}
        response = await http_request(app, "POST", DISPATCH_ROUTE, payload=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["execution_lane"], self.configuration.execution_lane)
        self.assertNotIn("private_key", response.text)
        allowed = False
        denied = await http_request(app, "POST", DISPATCH_ROUTE, payload=payload)
        self.assertEqual(denied.status_code, 403)
        allowed = True
        identity = replace(identity, repository="example/other")
        wrong = await http_request(app, "POST", DISPATCH_ROUTE, payload=payload)
        self.assertEqual(wrong.status_code, 409)
        identity = workflow_identity()
        with patch.object(metadata, "_secret_value") as secret_read:
            stale = await http_request(
                app,
                "POST",
                METADATA_ROUTE,
                payload={
                    "configuration_sha256": "0" * 64,
                    "current_runner_name": "ops-runner",
                    "kind": "idle",
                },
            )
            self.assertEqual(stale.status_code, 409)
            secret_read.assert_not_called()
