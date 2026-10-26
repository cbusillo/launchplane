from __future__ import annotations
from tests.support.auth import scoped_agent_test


import asyncio
from contextlib import redirect_stderr
from contextlib import contextmanager
from collections.abc import Callable, Iterator
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from dataclasses import replace
import json
import hashlib
import io
from pathlib import Path
import runpy
import sys
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import patch

from control_plane.contracts.artifact_identity import (
    ArtifactIdentityManifest,
    ArtifactImageReference,
)
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.deployment_record import DeploymentRecord, ResolvedTargetEvidence
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.odoo_target_replacement_failures import deploy_blocked_code
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import (
    DokployTargetRecord,
    DokployTargetStaffTestingHold,
)
from control_plane.contracts.lane_service_restart import SERVICE_RESTART_ROUTE
from control_plane.contracts.product_environment_read_model import build_product_activity_read_model
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.promotion_record import (
    ArtifactIdentityReference,
    DeploymentEvidence,
    RecordFailure,
)
from control_plane.contracts.release_review import (
    ReleaseChecklist,
    ReleaseReviewDecisionRecord,
    ReleaseVersion,
)
from control_plane.contracts.runtime_identity import (
    RUNTIME_IDENTITY_ENV_KEY,
    RuntimeIdentity,
    runtime_identity_env,
)
from control_plane.service_auth import (
    BearerIdentityConfig,
    GitHubHumanIdentity,
    LaunchplaneAuthzPolicy,
)
from control_plane.dokploy.api import DokployRequestFailed
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.generic_web_preview import serialize_generic_web_product_operation
from tests.support.auth import StubVerifier, identity, local_operator_policy
from tests.support.http import request as http_request
from tests.support.profiles import _odoo_profile_payload_with_prod_lane
from tests.support.disposable_lanes import record_safety
from tests.support.down_lanes import record_lane_health
from tests.test_service import create_launchplane_fastapi_test_app
from control_plane.http_app import LaunchplaneAuthzPolicyRuntime
from tests.test_odoo_prod_promotion_operation import _operation


def seed_lane(
    store: PostgresRecordStore,
    *,
    instance: str = "testing",
    image: str = "ghcr.io/example/site@sha256:" + "b" * 64,
) -> RuntimeIdentity:
    store.write_product_profile_record(
        LaunchplaneProductProfileRecord.model_validate(_odoo_profile_payload_with_prod_lane())
    )
    target = DokployTargetRecord(
        context="cm",
        instance=instance,
        target_type="compose",
        target_name="isolated-site",
        updated_at="2026-10-10T00:00:00Z",
    )
    target_id = DokployTargetIdRecord(
        context="cm",
        instance=instance,
        target_id=f"isolated-compose-{instance}",
        updated_at=target.updated_at,
    )
    store.write_dokploy_target_record(target)
    store.write_dokploy_target_id_record(target_id)
    store.write_provider_target_record(
        ProviderTargetRecord.from_dokploy_records(target_record=target, target_id_record=target_id)
    )
    repository, digest = image.split("@", 1)
    manifest = ArtifactIdentityManifest(
        artifact_id="isolated-artifact",
        source_commit="c" * 40,
        enterprise_base_digest="sha256:" + "d" * 64,
        image=ArtifactImageReference(repository=repository, digest=digest),
    )
    store.write_artifact_manifest(manifest)
    expected = RuntimeIdentity(
        product="odoo-tenant-cm",
        context="cm",
        instance=instance,
        deployment_record_id=f"isolated-deploy-{instance}",
        artifact_id=manifest.artifact_id,
        source_git_ref=manifest.source_commit,
        image_reference=image,
    )
    store.write_deployment_record(
        DeploymentRecord(
            record_id=expected.deployment_record_id,
            context="cm",
            instance=instance,
            source_git_ref=manifest.source_commit,
            artifact_identity=ArtifactIdentityReference(artifact_id=manifest.artifact_id),
            resolved_target=ResolvedTargetEvidence(
                target_type="compose", target_id=target_id.target_id, target_name=target.target_name
            ),
            runtime_identity=expected,
            deploy=DeploymentEvidence(
                target_name=target.target_name,
                target_type="compose",
                deploy_mode="compose",
                status="pass",
                started_at="2026-10-10T00:00:00Z",
                finished_at="2026-10-10T00:01:00Z",
            ),
        )
    )
    deployment = store.read_deployment_record(expected.deployment_record_id)
    store.write_environment_inventory(
        EnvironmentInventory(
            context=expected.context,
            instance=expected.instance,
            source_git_ref=expected.source_git_ref,
            artifact_identity=deployment.artifact_identity,
            runtime_identity=expected,
            deploy=deployment.deploy,
            updated_at=deployment.deploy.finished_at,
            deployment_record_id=deployment.record_id,
        )
    )
    return expected


class RestartProvider:
    def __init__(self, expected: RuntimeIdentity) -> None:
        self.container_id = "a" * 64
        self.writes: list[dict[str, Any]] = []
        self.duplicate = False
        self.unknown = False
        self.after_bad_image = False
        self.rejection: int | None = None
        self.remote_command_failed = False
        self.restart_started_at = "2026-10-10T00:02:00Z"
        self.transient_after_reads = 0
        self.database: dict[str, Any] | None = None
        self.config: dict[str, Any] = {
            "Id": self.container_id,
            "Image": "sha256:" + "e" * 64,
            "Config": {
                "Image": expected.image_reference,
                "Env": [f"{k}={v}" for k, v in runtime_identity_env(expected).items()],
                "Labels": {
                    "com.docker.compose.project": "isolated-site",
                    "com.docker.compose.service": "web",
                    "com.docker.compose.oneoff": "False",
                },
            },
            "State": {
                "Running": True,
                "StartedAt": "2026-10-10T00:01:00Z",
                "Health": {"Status": "unhealthy"},
            },
            "HostConfig": {},
            "Mounts": [],
        }

    def request(self, **kwargs: Any) -> Any:
        path = kwargs["path"]
        if path == "/api/compose.one":
            return {
                "composeId": kwargs["query"]["composeId"],
                "appName": "isolated-site",
                "serverId": "isolated-server",
            }
        if path == "/api/docker.getContainersByAppNameMatch":
            if self.writes and self.transient_after_reads:
                self.transient_after_reads -= 1
                raise DokployRequestFailed(
                    method="GET",
                    path=path,
                    detail="transient inventory unavailable",
                    status_code=502,
                )
            return (
                ([{"containerId": "d" * 64}] if self.database is not None else [])
                + [{"containerId": self.container_id}]
                + ([{"containerId": "f" * 64}] if self.duplicate else [])
            )
        if path == "/api/docker.getConfig":
            if self.database is not None and kwargs["query"]["containerId"] == "d" * 64:
                value = deepcopy(self.database)
                value["Id"] = kwargs["query"]["containerId"]
                return [value]
            value = deepcopy(self.config)
            value["Id"] = kwargs["query"]["containerId"]
            return [value]
        if path == "/api/docker.restartContainer":
            self.writes.append(kwargs)
            if self.rejection is not None:
                raise DokployRequestFailed(
                    method="POST",
                    path=path,
                    detail="private credential=must-not-leak",
                    status_code=self.rejection,
                    remote_command_failed=self.remote_command_failed,
                )
            restarted = (
                self.database if kwargs["payload"]["containerId"] == "d" * 64 else self.config
            )
            assert restarted is not None
            restarted["State"].update(
                Running=True, StartedAt=self.restart_started_at, Health={"Status": "healthy"}
            )
            if self.after_bad_image:
                self.config["Image"] = "sha256:" + "1" * 64
            if self.unknown:
                raise OSError("private credential=must-not-leak")
            return {}
        raise AssertionError(path)


class ServiceRestartTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.store = PostgresRecordStore(
            database_url="sqlite+pysqlite:///" + str(self.root / "state.db")
        )
        self.store.ensure_schema()
        self.addCleanup(self.store.close)
        self.expected = seed_lane(self.store)
        record_safety(self.store, self.expected.product, self.expected.context, "testing")
        self.provider = RestartProvider(self.expected)
        self.payload: dict[str, object] = {
            "product": "odoo-tenant-cm",
            "context": "cm",
            "instance": "testing",
            "service": "web",
            "reason": "Recover an isolated unhealthy web worker.",
            "mode": "dry-run",
        }
        self.app = self.create_app()
        self.addCleanup(patch.stopall)
        patch(
            "control_plane.dokploy.source.read_dokploy_config",
            return_value=("https://provider.example", "fake-provider-token"),
        ).start()
        patch(
            "control_plane.dokploy.api.dokploy_request", side_effect=self.provider.request
        ).start()
        self.health = patch(
            "control_plane.lane_service_restart.wait_for_runtime_identity_healthcheck_with_retry"
        ).start()

    def create_app(
        self,
        *,
        actions: tuple[str, ...] = ("live_target_runtime.plan", "live_target_runtime.apply"),
        contexts: tuple[str, ...] = ("cm",),
        subject: str = "local-owner-agent",
        github_admin: int | None = None,
        policy_runtime: LaunchplaneAuthzPolicyRuntime | None = None,
    ) -> Any:
        return create_launchplane_fastapi_test_app(
            local_record_store_for_tests=self.store,
            authz_policy_runtime=policy_runtime,
            state_dir=self.root / "state",
            control_plane_root_path=self.root,
            verifier=StubVerifier(identity()),
            authz_policy=LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_humans": [
                        {
                            "github_ids": [github_admin],
                            "roles": ["admin"],
                            "actions": ["live_target_runtime.plan", "live_target_runtime.apply"],
                            "products": ["odoo-tenant-cm"],
                            "contexts": ["cm"],
                        }
                    ]
                }
            )
            if github_admin is not None
            else local_operator_policy(
                actions=actions, products=("odoo-tenant-cm",), contexts=contexts, subject=subject
            ),
            bearer_identity_config=BearerIdentityConfig(
                local_operator_token="isolated-token",
                local_operator_subject=subject,
                local_operator_token_label="local-owner-write",
            ),
        )

    def invoke(self, *, key: str = "", token: str = "isolated-token") -> tuple[int, dict[str, Any]]:
        response = asyncio.run(
            http_request(
                self.app,
                "POST",
                SERVICE_RESTART_ROUTE,
                payload=self.payload,
                headers={"Authorization": f"Bearer {token}", "Idempotency-Key": key},
            )
        )
        return response.status_code, response.json()

    def review(self) -> dict[str, Any]:
        status, response = self.invoke()
        self.assertEqual(status, 200, response)
        self.payload.update(mode="apply", reviewed_plan_sha256=response["result"]["plan_sha256"])
        return response

    def record_newer_attempt(
        self, *, code: str, uncertain: bool = False, status: str = "fail"
    ) -> None:
        current = self.store.read_deployment_record(self.expected.deployment_record_id)
        self.store.write_deployment_record(
            DeploymentRecord.model_validate(
                {
                    **current.model_dump(mode="json"),
                    "record_id": "newer-attempt",
                    "runtime_source": {"provider_outcome": "uncertain"} if uncertain else {},
                    "failure": RecordFailure(code=code, description="Deploy refused.").model_dump(),
                    "deploy": {
                        **current.deploy.model_dump(mode="json"),
                        "status": status,
                        "started_at": "2026-10-10T00:02:00Z",
                        "finished_at": "2026-10-10T00:03:00Z",
                    },
                }
            )
        )

    def select_stopped_database(self) -> None:
        self.payload["service"] = "database"
        self.provider.config["State"]["Running"] = False
        self.provider.database = deepcopy(self.provider.config)
        self.provider.database.update(Id="d" * 64, Created="2026-10-10T00:00:30Z")
        self.provider.database["Config"].update(Image="postgres:17", Env=[])
        self.provider.database["Config"]["Labels"]["com.docker.compose.service"] = "database"
        self.provider.database["Mounts"] = [{"Type": "volume", "Name": "isolated-data"}]

    def test_pre_provider_failure_keeps_inventory_artifact_restartable(self) -> None:
        self.record_newer_attempt(code=deploy_blocked_code("retained_database_credential_changed"))
        reviewed = self.review()
        self.assertEqual(
            reviewed["result"]["plan"]["deployment_record_id"], self.expected.deployment_record_id
        )
        self.assertEqual(self.invoke(key="inventory-artifact")[1]["result"]["status"], "pass")
        self.assertEqual(len(self.provider.writes), 1)

    def test_newer_provider_effect_or_success_refuses_restart(self) -> None:
        for code, uncertain, status in (
            ("deploy_failed", False, "fail"),
            ("deploy_blocked.unknown_check", False, "fail"),
            (deploy_blocked_code("compose_keys_missing"), True, "fail"),
            (deploy_blocked_code("compose_keys_missing"), False, "pass"),
        ):
            with self.subTest(code=code, uncertain=uncertain, status=status):
                self.record_newer_attempt(code=code, uncertain=uncertain, status=status)
                status_code, response = self.invoke()
                self.assertEqual(status_code, 409)
                self.assertIn(
                    self.store.read_deployment_record("newer-attempt").record_id,
                    response["error"]["message"],
                )
                self.assertEqual(response["error"]["reason"], "newer_deployment")
                self.assertEqual(response["details"]["blocking_deployment_id"], "newer-attempt")
                self.assertEqual(self.provider.writes, [])

    def test_unfinished_newer_attempt_refuses_despite_sorting_after_inventory(self) -> None:
        self.record_newer_attempt(
            code=deploy_blocked_code("compose_keys_missing"), status="pending"
        )
        attempt = self.store.read_deployment_record("newer-attempt")
        self.store.write_deployment_record(
            attempt.model_copy(
                update={"deploy": attempt.deploy.model_copy(update={"finished_at": ""})}
            )
        )
        self.assertEqual(self.invoke()[0], 409)
        self.assertEqual(self.provider.writes, [])

    def test_completed_history_without_start_keeps_current_artifact_restartable(self) -> None:
        original = self.store.read_deployment_record(self.expected.deployment_record_id)
        self.store.write_deployment_record(
            original.model_copy(
                update={
                    "record_id": "historical-activation-image",
                    "deploy": original.deploy.model_copy(
                        update={"started_at": "", "finished_at": "2026-10-09T23:59:00Z"}
                    ),
                }
            )
        )
        self.review()
        self.assertEqual(self.invoke(key="historical-activation")[1]["result"]["status"], "pass")
        self.assertEqual(len(self.provider.writes), 1)

    def test_unplaceable_or_malformed_history_refuses_without_dispatch(self) -> None:
        original = self.store.read_deployment_record(self.expected.deployment_record_id)
        for started_at, finished_at in (
            ("", ""),
            ("2026-10-09T23:58:00", "2026-10-09T23:59:00Z"),
            ("2026-10-09T23:58:00Z", "invalid"),
        ):
            with self.subTest(started_at=started_at, finished_at=finished_at):
                self.store.write_deployment_record(
                    original.model_copy(
                        update={
                            "record_id": "unplaceable-history",
                            "deploy": original.deploy.model_copy(
                                update={"started_at": started_at, "finished_at": finished_at}
                            ),
                        }
                    )
                )
                self.assertEqual(self.invoke()[0], 409)
                self.assertEqual(self.provider.writes, [])

    def test_overlapping_attempt_and_missing_current_start_refuse(self) -> None:
        current = self.store.read_deployment_record(self.expected.deployment_record_id)
        self.store.write_deployment_record(
            current.model_copy(
                update={"deploy": current.deploy.model_copy(update={"started_at": ""})}
            )
        )
        self.assertEqual(self.invoke()[0], 409)
        self.store.write_deployment_record(current)
        self.store.write_deployment_record(
            current.model_copy(
                update={
                    "record_id": "overlapping-deployment",
                    "deploy": current.deploy.model_copy(
                        update={
                            "started_at": "2026-10-09T23:59:00Z",
                            "finished_at": current.deploy.started_at,
                        }
                    ),
                }
            )
        )
        self.assertEqual(self.invoke()[0], 409)
        self.assertEqual(self.provider.writes, [])

    def test_blocked_code_after_post_deploy_effect_refuses_restart(self) -> None:
        self.record_newer_attempt(code=deploy_blocked_code("backup_restore_proof_required"))
        attempt = self.store.read_deployment_record("newer-attempt")
        self.store.write_deployment_record(
            attempt.model_copy(
                update={
                    "post_deploy_update": attempt.post_deploy_update.model_copy(
                        update={"attempted": True, "status": "fail"}
                    )
                }
            )
        )
        self.assertEqual(self.invoke()[0], 409)
        self.assertEqual(self.provider.writes, [])

    def test_inventory_identity_must_agree_with_record_and_container(self) -> None:
        inventory = self.store.read_environment_inventory(
            context_name="cm", instance_name="testing"
        )
        self.store.write_environment_inventory(
            inventory.model_copy(
                update={
                    "runtime_identity": self.expected.model_copy(update={"artifact_id": "other"})
                }
            )
        )
        self.assertEqual(self.invoke()[0], 409)
        self.store.write_environment_inventory(inventory)
        self.record_newer_attempt(code=deploy_blocked_code("compose_keys_missing"))
        self.provider.config["Config"]["Image"] = "ghcr.io/example/new@sha256:" + "9" * 64
        self.assertEqual(self.invoke()[0], 409)
        self.assertEqual(self.provider.writes, [])

    def test_stopped_database_restarts_before_web_with_unchanged_mounts_and_image(self) -> None:
        self.select_stopped_database()
        reviewed = self.review()
        status, response = self.invoke(key="database-first")
        self.assertEqual(status, 200, response)
        self.assertEqual(response["result"]["status"], "pass")
        before = reviewed["result"]["plan"]["before"]
        after = response["result"]["after"]
        for field in ("container_id", "image_id", "image_reference", "configuration_sha256"):
            self.assertEqual(after[field], before[field])
        self.assertNotEqual(after["started_at"], before["started_at"])
        self.assertFalse(self.provider.config["State"]["Running"])
        self.assertEqual(self.invoke(key="database-first")[1]["replayed"], True)
        self.health.assert_not_called()
        self.payload.update(service="web", mode="dry-run")
        self.payload.pop("reviewed_plan_sha256")
        self.review()
        self.assertEqual(self.invoke(key="web-after-database")[1]["result"]["status"], "pass")
        self.assertEqual(
            [write["payload"]["containerId"] for write in self.provider.writes],
            [before["container_id"], self.provider.container_id],
        )
        self.health.assert_called_once()

    def test_database_lost_reply_reconciles_running_container_without_second_post(self) -> None:
        self.select_stopped_database()
        self.review()
        self.provider.unknown = True
        self.assertEqual(self.invoke(key="database-lost-reply")[0], 409)
        assert self.provider.database is not None
        self.provider.database["State"]["StartedAt"] = datetime.now(timezone.utc).isoformat()
        self.record_newer_attempt(code=deploy_blocked_code("compose_keys_missing"))
        self.payload["mode"] = "reconcile"
        status, response = self.invoke(key="database-lost-reply")
        self.assertEqual(status, 200, response)
        self.assertEqual(response["result"]["status"], "pass")
        self.assertEqual(len(self.provider.writes), 1)

    def test_database_requires_stopped_deployment_bound_container_and_matching_web(self) -> None:
        mutations: tuple[Callable[[dict[str, Any]], object], ...] = (
            lambda database: database["State"].update(Running=True),
            lambda database: database.update(Created="2026-10-10T00:02:00Z"),
            lambda database: database.pop("Created"),
            lambda database: database["State"].pop("Health"),
            lambda database: database["Config"]["Labels"].update(
                {"com.docker.compose.project": "other"}
            ),
            lambda database: database["Config"]["Labels"].update(
                {"com.docker.compose.oneoff": "True"}
            ),
            lambda database: self.provider.config["Config"].update(Env=[]),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                self.provider.config = RestartProvider(self.expected).config
                self.select_stopped_database()
                assert self.provider.database is not None
                mutate(self.provider.database)
                self.assertEqual(self.invoke()[0], 409)
                self.assertEqual(self.provider.writes, [])

    def test_two_database_containers_refuse_without_dispatch(self) -> None:
        self.select_stopped_database()
        assert self.provider.database is not None
        original_request = self.provider.request

        def request_with_duplicate_database(**kwargs: Any) -> Any:
            response = original_request(**kwargs)
            if kwargs["path"] == "/api/docker.getContainersByAppNameMatch":
                return [*response, {"containerId": "f" * 64}]
            if (
                kwargs["path"] == "/api/docker.getConfig"
                and kwargs["query"]["containerId"] == "f" * 64
            ):
                value = deepcopy(self.provider.database)
                assert value is not None
                value["Id"] = "f" * 64
                return [value]
            return response

        with patch(
            "control_plane.dokploy.api.dokploy_request", side_effect=request_with_duplicate_database
        ):
            self.assertEqual(self.invoke()[0], 409)
        self.assertEqual(self.provider.writes, [])

    def test_new_provider_effect_after_review_refuses_apply(self) -> None:
        self.review()
        self.record_newer_attempt(code="deploy_failed")
        self.assertEqual(self.invoke(key="provider-changed")[0], 409)
        self.assertEqual(self.provider.writes, [])

    def test_database_started_since_review_refuses_before_dispatch(self) -> None:
        self.select_stopped_database()
        self.review()
        assert self.provider.database is not None
        self.provider.database["State"]["Running"] = True
        self.assertEqual(self.invoke(key="database-already-running")[0], 409)
        self.assertEqual(self.provider.writes, [])

    def test_database_dependency_excludes_generic_web_and_production(self) -> None:
        self.select_stopped_database()
        profile = self.store.read_product_profile_record(self.expected.product)
        self.store.write_product_profile_record(
            profile.model_copy(update={"driver_id": "generic-web"})
        )
        self.assertEqual(self.invoke()[0], 409)
        self.store.write_product_profile_record(profile)
        expected = seed_lane(self.store, instance="prod")
        self.provider.config = RestartProvider(expected).config
        self.payload["instance"] = "prod"
        self.record_acceptance(expected)
        self.payload["service"] = "web"
        status, response = self.invoke()
        self.assertEqual(status, 200, response)
        self.select_stopped_database()
        self.assertEqual(self.invoke()[0], 409)
        self.assertEqual(self.provider.writes, [])

    def test_route_preserves_fixed_planner_refusal(self) -> None:
        from control_plane.lane_service_restart import ServiceRestartRefused

        error = ServiceRestartRefused(
            "current_deployment_unsettled", "A different diagnostic sentence."
        )
        with patch(
            "control_plane.http_routes.lane_service_restart.plan_service_restart", side_effect=error
        ):
            status, response = self.invoke()
        self.assertEqual(status, 409)
        self.assertEqual(response["error"]["code"], "restart_refused")
        self.assertEqual(response["error"]["message"], str(error))
        self.assertEqual(response["error"]["reason"], error.reason)

    def test_legacy_container_identity_keeps_its_reviewed_restart_digest(self) -> None:
        # A container deployed before the two-slot contract has no slot fields.
        # Keep the historical wire shape independent of new model defaults.
        legacy_identity = {
            "schema_version": 1,
            "product": self.expected.product,
            "context": self.expected.context,
            "instance": self.expected.instance,
            "environment_kind": "stable",
            "deployment_record_id": self.expected.deployment_record_id,
            "artifact_id": self.expected.artifact_id,
            "source_git_ref": self.expected.source_git_ref,
            "image_reference": self.expected.image_reference,
            "release_tuple_id": "",
            "preview_id": "",
            "preview_generation_id": "",
            "deployed_at": "",
        }
        legacy_json = json.dumps(legacy_identity, sort_keys=True, separators=(",", ":"))
        self.provider.config["Config"]["Env"] = [RUNTIME_IDENTITY_ENV_KEY + "=" + legacy_json]
        reviewed = self.review()
        self.assertEqual(
            reviewed["result"]["plan"]["before"]["runtime_identity_sha256"],
            hashlib.sha256(legacy_json.encode()).hexdigest(),
        )
        self.assertEqual(self.invoke(key="legacy-restart")[1]["result"]["status"], "pass")
        self.assertEqual(len(self.provider.writes), 1)

    def test_restart_replays_and_activity_retains_actor_reason_and_identities(self) -> None:
        reviewed = self.review()
        self.assertEqual(self.provider.writes, [])
        status, result = self.invoke(key="restart-1")
        self.assertEqual(status, 200, result)
        self.assertEqual(result["result"]["status"], "pass")
        self.assertNotEqual(
            result["result"]["after"]["started_at"],
            reviewed["result"]["plan"]["before"]["started_at"],
        )
        self.assertEqual(
            self.provider.writes[0]["payload"],
            {"containerId": self.provider.container_id, "serverId": "isolated-server"},
        )
        self.assertEqual(self.invoke(key="restart-1")[1]["replayed"], True)
        self.assertEqual(len(self.provider.writes), 1)
        self.health.assert_called_once()
        activity = build_product_activity_read_model(
            record_store=self.store, product="odoo-tenant-cm"
        )
        event = next(event for event in activity.events if event.event_type == "service_restart")
        self.assertEqual(event.status, "pass")
        self.assertIn("Recover an isolated unhealthy web worker", event.summary)
        self.assertIn("local-owner-agent", event.summary)
        self.assertIn(self.provider.container_id, event.summary)

    def test_director_restarts_qualified_testing_service_without_a_write_grant(self) -> None:
        self.app = self.create_app(
            policy_runtime=LaunchplaneAuthzPolicyRuntime(LaunchplaneAuthzPolicy())
        )
        self.payload["service"] = "script-runner"
        self.provider.config["Config"]["Labels"]["com.docker.compose.service"] = "script-runner"
        self.review()
        status, response = self.invoke(key="standing-restart")
        self.assertEqual(status, 200, response)
        self.assertEqual(response["result"]["status"], "pass")
        record_safety(
            self.store, self.expected.product, self.expected.context, "testing", data="only_copy"
        )
        self.assertTrue(self.invoke(key="standing-restart")[1]["replayed"])
        self.assertEqual(len(self.provider.writes), 1)

    def test_stopped_restart_preserves_data_without_disposable_qualification(self) -> None:
        self.app = self.create_app(
            policy_runtime=LaunchplaneAuthzPolicyRuntime(LaunchplaneAuthzPolicy())
        )
        for service in ("database", "web", "script-runner"):
            for facts in (
                None,
                {"data": "unknown", "production_resources": "unknown"},
                {"data": "only_copy"},
            ):
                with self.subTest(service=service, facts=facts):
                    self.payload.update(mode="dry-run", reviewed_plan_sha256="")
                    self.payload["service"] = service
                    self.provider.config["Config"]["Labels"]["com.docker.compose.service"] = "web"
                    self.provider.config["State"].update(
                        Running=False, StartedAt="2026-10-10T00:01:00Z"
                    )
                    if service == "database":
                        self.select_stopped_database()
                    else:
                        self.provider.database = None
                        self.provider.config["Config"]["Labels"]["com.docker.compose.service"] = (
                            service
                        )
                    self.store.clear_disposable_lane_safety(
                        self.expected.product, self.expected.context, "testing"
                    )
                    if facts is not None:
                        record_safety(
                            self.store,
                            self.expected.product,
                            self.expected.context,
                            "testing",
                            **facts,
                        )
                    reviewed = self.review()
                    status, response = self.invoke(key=f"preserving-{service}-{facts}")
                    self.assertEqual(status, 200, response)
                    self.assertEqual(response["result"]["status"], "pass")
                    self.assertEqual(response["result"]["plan"], reviewed["result"]["plan"])
                    self.assertEqual(
                        response["result"]["after"]["configuration_sha256"],
                        reviewed["result"]["plan"]["before"]["configuration_sha256"],
                    )
                    self.assertEqual(
                        self.provider.writes[-1]["payload"]["containerId"],
                        reviewed["result"]["plan"]["before"]["container_id"],
                    )

    def test_running_restart_keeps_disposable_qualification(self) -> None:
        self.app = self.create_app(
            policy_runtime=LaunchplaneAuthzPolicyRuntime(LaunchplaneAuthzPolicy())
        )
        self.review()
        now = datetime.now(timezone.utc)
        unsafe_facts: tuple[dict[str, object], ...] = (
            {"usage": "in_use"},
            {"data": "only_copy"},
            {"data": "unknown"},
            {"production_resources": "shared"},
            {"production_resources": "unknown"},
            {"binding_sha256": "0" * 64},
            {
                "observed_at": now - timedelta(minutes=2),
                "expires_at": now - timedelta(minutes=1),
            },
        )
        for index, changes in enumerate(unsafe_facts):
            with self.subTest(changes=changes):
                record_safety(
                    self.store, self.expected.product, self.expected.context, "testing", **changes
                )
                status, response = self.invoke(key=f"unsafe-restart-{index}")
                self.assertEqual(status, 409, response)
                self.assertEqual(response["error"]["code"], "disposable_lane_safety_unknown")
                self.assertEqual(self.provider.writes, [])
        self.store.clear_disposable_lane_safety(
            self.expected.product, self.expected.context, "testing"
        )
        status, response = self.invoke(key="missing-safety")
        self.assertEqual(status, 409, response)
        self.assertEqual(response["error"]["code"], "disposable_lane_safety_unknown")
        self.assertEqual(self.provider.writes, [])

    def test_unhealthy_web_with_missing_docker_route_recovers_before_identity_verification(
        self,
    ) -> None:
        self.app = self.create_app(
            policy_runtime=LaunchplaneAuthzPolicyRuntime(LaunchplaneAuthzPolicy())
        )
        self.provider.config["State"]["Health"]["Status"] = "unhealthy"
        record_lane_health(
            self.store,
            self.expected.product,
            self.expected.context,
            datetime.now(timezone.utc),
            status=404,
        )
        record_safety(
            self.store,
            self.expected.product,
            self.expected.context,
            "testing",
            usage="in_use",
            data="unknown",
            production_resources="unknown",
        )
        self.review()
        status, response = self.invoke(key="unhealthy-web-recovery")
        self.assertEqual(status, 200, response)
        self.assertEqual(response["result"]["status"], "pass")
        self.assertEqual(len(self.provider.writes), 1)

    def test_stopped_restart_refuses_known_production_sharing(self) -> None:
        self.provider.config["State"]["Running"] = False
        self.review()
        for resources in ("shared", "separation_ready"):
            with self.subTest(resources=resources):
                record_safety(
                    self.store,
                    self.expected.product,
                    self.expected.context,
                    "testing",
                    production_resources=resources,
                )
                status, response = self.invoke(key=f"shared-{resources}")
                self.assertEqual(status, 409, response)
                self.assertEqual(response["error"]["code"], "disposable_lane_safety_unknown")
                self.assertEqual(self.provider.writes, [])

    def test_stopped_restart_resolves_positive_use_only_with_recorded_downtime(self) -> None:
        self.provider.config["State"]["Running"] = False
        self.review()
        record_safety(
            self.store, self.expected.product, self.expected.context, "testing", usage="in_use"
        )
        status, response = self.invoke(key="protected-use")
        self.assertEqual(status, 409, response)
        self.assertEqual(response["error"]["code"], "disposable_lane_safety_unknown")
        self.assertEqual(self.provider.writes, [])
        record_lane_health(
            self.store,
            self.expected.product,
            self.expected.context,
            datetime.now(timezone.utc),
            status=404,
        )
        self.payload.update(mode="dry-run", reviewed_plan_sha256="", service="script-runner")
        self.provider.config["Config"]["Labels"]["com.docker.compose.service"] = "script-runner"
        self.review()
        status, response = self.invoke(key="stopped-non-web")
        self.assertEqual(status, 409, response)
        self.assertEqual(response["error"]["code"], "disposable_lane_safety_unknown")
        self.assertEqual(self.provider.writes, [])
        self.payload.update(mode="dry-run", reviewed_plan_sha256="", service="database")
        self.provider.config["Config"]["Labels"]["com.docker.compose.service"] = "web"
        self.select_stopped_database()
        self.provider.config["State"]["Running"] = True
        self.provider.config["State"]["Health"]["Status"] = "healthy"
        self.review()
        status, response = self.invoke(key="database-with-running-web")
        self.assertEqual(status, 409, response)
        self.assertEqual(response["error"]["code"], "disposable_lane_safety_unknown")
        self.assertEqual(self.provider.writes, [])
        self.provider.config["State"]["Running"] = False
        self.payload.update(mode="dry-run", reviewed_plan_sha256="")
        self.review()
        status, response = self.invoke(key="down-database-restart")
        self.assertEqual(status, 200, response)
        self.assertEqual(response["result"]["status"], "pass")
        self.payload.update(mode="dry-run", reviewed_plan_sha256="", service="web")
        self.review()
        status, response = self.invoke(key="down-restart")
        self.assertEqual(status, 200, response)
        self.assertEqual(response["result"]["status"], "pass")

    def test_director_grant_does_not_bypass_testing_hold_or_target_alias(self) -> None:
        self.provider.config["State"]["Running"] = False
        self.review()
        target = self.store.read_dokploy_target_record(
            context_name=self.expected.context, instance_name="testing"
        )
        for purpose in ("staff_testing", "isolation_staging"):
            target.policies.staff_testing_hold = DokployTargetStaffTestingHold(
                reason="Staff is testing.", recorded_by="fixture-admin", purpose=purpose
            )
            self.store.write_dokploy_target_record(target)
            record_safety(self.store, self.expected.product, self.expected.context, "testing")
            status, response = self.invoke(key=f"held-restart-{purpose}")
            self.assertEqual(status, 409, response)
            self.assertEqual(response["error"]["code"], "disposable_lane_safety_unknown")
        target.policies.staff_testing_hold = None
        self.store.write_dokploy_target_record(target)
        record_safety(self.store, self.expected.product, self.expected.context, "testing")
        current = self.store.read_dokploy_target_id_record(
            context_name=self.expected.context, instance_name="testing"
        )
        self.store.write_dokploy_target_id_record(current.model_copy(update={"instance": "prod"}))
        status, response = self.invoke(key="aliased-restart")
        self.assertEqual(status, 409, response)
        self.assertEqual(response["error"]["code"], "disposable_lane_safety_unknown")
        self.assertEqual(self.provider.writes, [])

    @scoped_agent_test
    def test_scoped_agent_cannot_inherit_director_restart_permission(self) -> None:
        self.app = self.create_app(actions=("live_target_runtime.plan",))
        self.review()
        self.assertEqual(self.invoke(key="scoped-restart")[0], 403)
        self.assertEqual(self.provider.writes, [])

    def test_standing_restart_permission_excludes_production_and_terminal_transport(self) -> None:
        self.app = self.create_app(
            policy_runtime=LaunchplaneAuthzPolicyRuntime(LaunchplaneAuthzPolicy())
        )
        self.review()
        self.assertEqual(self.invoke(key="terminal-restart", token="valid-token")[0], 403)
        self.payload["instance"] = "prod"
        self.assertEqual(self.invoke(key="production-restart")[0], 403)
        self.assertEqual(self.provider.writes, [])

    @scoped_agent_test
    def test_denied_scope_and_terminal_identity_never_inspect_or_restart(self) -> None:
        self.app = self.create_app(contexts=("other-context",))
        with patch(
            "control_plane.http_routes.lane_service_restart.plan_service_restart",
            side_effect=AssertionError("denied read"),
        ):
            self.assertEqual(self.invoke()[0], 403)
            self.assertEqual(self.invoke(token="valid-token")[0], 403)
        self.assertEqual(self.provider.writes, [])

    @scoped_agent_test
    def test_live_apply_grant_revocation_is_seen_without_restarting_the_service(self) -> None:
        runtime = LaunchplaneAuthzPolicyRuntime(
            local_operator_policy(
                actions=("live_target_runtime.plan", "live_target_runtime.apply"),
                products=(self.expected.product,),
                contexts=(self.expected.context,),
            )
        )
        self.app = self.create_app(policy_runtime=runtime)
        self.review()
        runtime.update(
            local_operator_policy(
                actions=("live_target_runtime.plan",),
                products=(self.expected.product,),
                contexts=(self.expected.context,),
            ),
            policy_sha256="1" * 64,
            source="test:revoked",
        )
        with patch(
            "control_plane.http_routes.lane_service_restart.plan_service_restart",
            side_effect=AssertionError("revoked provider read"),
        ):
            self.assertEqual(self.invoke(key="revoked")[0], 403)
        self.assertEqual(self.provider.writes, [])

    def test_stale_container_configuration_and_duplicate_service_refuse_without_writes(
        self,
    ) -> None:
        self.review()
        self.provider.config["Config"]["Env"].append("EXAMPLE_VALUE=changed")
        self.assertEqual(self.invoke(key="stale")[0], 409)
        self.provider.duplicate = True
        self.payload["mode"] = "dry-run"
        self.payload.pop("reviewed_plan_sha256")
        self.assertEqual(self.invoke()[0], 409)
        self.assertEqual(self.provider.writes, [])

    def test_inspected_membership_overrules_a_name_match(self) -> None:
        self.provider.config["Config"]["Labels"]["com.docker.compose.project"] = "unrelated-project"
        self.assertEqual(self.invoke()[0], 409)
        self.assertEqual(self.provider.writes, [])

    def test_unknown_post_is_not_repeated_by_same_or_new_key_and_is_visible(self) -> None:
        self.review()
        self.provider.unknown = True
        status, response = self.invoke(key="unknown")
        self.assertEqual(status, 409, response)
        self.assertNotIn("must-not-leak", json.dumps(response))
        self.assertEqual(self.invoke(key="unknown")[0], 409)
        # Reviewing again cannot bypass the held target with a new key.
        self.payload["mode"] = "dry-run"
        self.payload.pop("reviewed_plan_sha256")
        self.review()
        self.assertEqual(self.invoke(key="new-key")[0], 409)
        self.assertEqual(len(self.provider.writes), 1)
        event = next(
            event
            for event in build_product_activity_read_model(
                record_store=self.store, product="odoo-tenant-cm"
            ).events
            if event.event_type == "service_restart"
        )
        self.assertEqual(event.status, "unknown")
        self.assertIn("unverified", event.summary)

    def test_health_identity_failure_is_recorded_and_replayed_without_second_restart(self) -> None:
        self.review()
        self.provider.after_bad_image = True
        status, response = self.invoke(key="bad-after")
        self.assertEqual(status, 200, response)
        self.assertEqual(response["result"]["status"], "fail")
        self.assertEqual(self.invoke(key="bad-after")[1]["result"]["status"], "fail")
        self.assertEqual(len(self.provider.writes), 1)

    def test_lost_reply_recovers_fresh_healthy_identity_without_restarting_again(self) -> None:
        self.app = self.create_app(
            policy_runtime=LaunchplaneAuthzPolicyRuntime(LaunchplaneAuthzPolicy())
        )
        self.review()
        self.provider.unknown = True
        self.assertEqual(self.invoke(key="lost-reply")[0], 409)
        record_safety(
            self.store, self.expected.product, self.expected.context, "testing", usage="in_use"
        )
        self.provider.config["State"]["StartedAt"] = datetime.now(timezone.utc).isoformat()
        status, response = self.invoke(key="lost-reply")
        self.assertEqual(status, 200, response)
        self.assertEqual(response["result"]["status"], "pass")
        self.assertIn("no additional restart", response["result"]["error_message"])
        self.assertEqual(len(self.provider.writes), 1)

    def test_real_pending_release_owner_refuses_before_provider_inspection(self) -> None:
        self.store.write_odoo_prod_promotion_operation_record(_operation())
        self.payload["instance"] = "prod"
        self.assertEqual(self.invoke()[0], 409)
        self.assertEqual(self.provider.writes, [])

    def record_acceptance(self, expected: RuntimeIdentity) -> ReleaseReviewDecisionRecord:
        version = ReleaseVersion(
            artifact_id=expected.artifact_id, source_commit=expected.source_git_ref
        )
        decision = ReleaseReviewDecisionRecord(
            record_id="isolated-acceptance",
            product=expected.product,
            checklist_digest="1" * 64,
            checklist=ReleaseChecklist(
                product=expected.product,
                repository="example/site",
                owner_github_id="123",
                testing_url="https://testing.example.com",
                production=version,
                candidate=version,
                items=(),
            ),
            decision="accepted",
            actor_github_id="123",
            actor_github_login="example-client",
            decided_at="2026-10-10T00:00:00Z",
        )
        self.store.write_release_review_decision_record(decision)
        return decision

    def test_production_requires_acceptance_of_the_current_artifact(self) -> None:
        expected = seed_lane(self.store, instance="prod")
        self.provider.config["Config"]["Env"] = [
            f"{k}={v}" for k, v in runtime_identity_env(expected).items()
        ]
        self.payload["instance"] = "prod"
        self.assertEqual(self.invoke()[0], 409)
        decision = self.record_acceptance(expected)
        status, response = self.invoke()
        self.assertEqual(status, 200, response)
        self.assertEqual(response["result"]["plan"]["acceptance_record_id"], decision.record_id)
        self.assertEqual(self.provider.writes, [])

    def test_ambiguous_start_time_and_missing_http_identity_refuse_before_effect(self) -> None:
        for started_at in ("unavailable", "2026-10-10T00:00:00", "0001-01-01T00:00:00Z"):
            with self.subTest(started_at=started_at):
                self.provider.config["State"]["StartedAt"] = started_at
                self.assertEqual(self.invoke()[0], 409)
        self.provider.config["State"]["StartedAt"] = "2026-10-10T00:01:00Z"
        profile = self.store.read_product_profile_record(self.expected.product)
        self.store.write_product_profile_record(
            profile.model_copy(
                update={
                    "lanes": tuple(
                        lane.model_copy(update={"health_url": ""}) for lane in profile.lanes
                    )
                }
            )
        )
        self.assertEqual(self.invoke()[0], 409)
        self.assertEqual(self.provider.writes, [])

    def test_definite_provider_rejection_records_failure_replays_and_releases_fence(self) -> None:
        self.review()
        self.provider.rejection = 403
        status, response = self.invoke(key="rejected")
        self.assertEqual(status, 200, response)
        self.assertEqual(response["result"]["status"], "fail")
        self.assertNotIn("must-not-leak", json.dumps(response))
        self.assertTrue(self.invoke(key="rejected")[1]["replayed"])
        self.assertEqual(len(self.provider.writes), 1)
        self.provider.rejection = None
        self.assertEqual(self.invoke(key="new-after-rejection")[1]["result"]["status"], "pass")

    def test_activity_restores_original_request_and_reconcile_never_starts_new_effect(self) -> None:
        self.review()
        self.provider.unknown = True
        self.assertEqual(self.invoke(key="lost-tab")[0], 409)
        event = next(
            event
            for event in build_product_activity_read_model(
                record_store=self.store, product=self.expected.product
            ).events
            if event.event_type == "service_restart"
        )
        self.assertIsNotNone(event.restart_recovery)
        assert event.restart_recovery is not None
        self.payload = event.restart_recovery.request.model_dump(mode="json")
        self.payload["mode"] = "reconcile"
        # Even a correctly reviewed request cannot use reconciliation to create a new operation.
        self.assertEqual(self.invoke(key="missing-original")[0], 404)
        self.app = self.create_app(subject="different-authorized-operator")
        self.assertEqual(self.invoke(key=event.restart_recovery.idempotency_key)[0], 409)
        self.app = self.create_app(
            policy_runtime=LaunchplaneAuthzPolicyRuntime(LaunchplaneAuthzPolicy())
        )
        record_safety(
            self.store, self.expected.product, self.expected.context, "testing", usage="in_use"
        )
        self.provider.config["State"]["Restarting"] = True
        status, response = self.invoke(key=event.restart_recovery.idempotency_key)
        self.assertEqual(status, 409, response)
        self.assertEqual(response["error"]["code"], "mutation_reconciliation_required")
        self.provider.config["State"]["Restarting"] = False
        self.provider.config["State"]["StartedAt"] = datetime.now(timezone.utc).isoformat()
        self.assertEqual(
            self.invoke(key=event.restart_recovery.idempotency_key)[1]["result"]["status"], "pass"
        )
        self.assertEqual(len(self.provider.writes), 1)

    def test_partial_remote_command_failure_remains_unknown_and_fenced(self) -> None:
        self.review()
        self.provider.rejection = 400
        self.provider.remote_command_failed = True
        self.assertEqual(self.invoke(key="partial-command")[0], 409)
        self.assertEqual(self.invoke(key="partial-command")[0], 409)
        self.assertEqual(len(self.provider.writes), 1)

    def test_transient_verification_read_retries_without_another_restart(self) -> None:
        self.review()
        self.provider.transient_after_reads = 1
        status, response = self.invoke(key="transient-read")
        self.assertEqual(status, 200, response)
        self.assertEqual(response["result"]["status"], "pass")
        self.assertEqual(self.provider.transient_after_reads, 0)
        self.assertEqual(len(self.provider.writes), 1)

    def test_github_login_rename_preserves_original_principal_recovery(self) -> None:
        self.app = self.create_app(github_admin=101)
        human = [
            GitHubHumanIdentity(
                login="original-name",
                github_id=101,
                name="",
                email="",
                organizations=frozenset(),
                teams=frozenset(),
                role="admin",
            )
        ]
        route = next(
            route
            for route in self.app.routes
            if getattr(route, "path", "") == SERVICE_RESTART_ROUTE
        )
        self.app.dependency_overrides[route.dependant.dependencies[0].call] = lambda: human[0]
        self.review()
        self.provider.unknown = True
        self.assertEqual(self.invoke(key="renamed-account")[0], 409)
        human[0] = replace(human[0], login="new-name")
        self.payload["mode"] = "reconcile"
        self.provider.config["State"]["StartedAt"] = datetime.now(timezone.utc).isoformat()
        self.assertEqual(self.invoke(key="renamed-account")[1]["result"]["status"], "pass")
        self.assertEqual(len(self.provider.writes), 1)

    def test_later_verified_replacement_settles_old_unknown_without_repeating_it(self) -> None:
        self.review()
        original_payload = dict(self.payload)
        self.provider.unknown = True
        self.assertEqual(self.invoke(key="old-container")[0], 409)
        original = self.store.read_deployment_record(self.expected.deployment_record_id)
        replacement_identity = self.expected.model_copy(
            update={"deployment_record_id": "isolated-replacement"}
        )
        self.store.write_deployment_record(
            original.model_copy(
                update={
                    "record_id": replacement_identity.deployment_record_id,
                    "runtime_identity": replacement_identity,
                    "deploy": original.deploy.model_copy(
                        update={
                            "started_at": "2026-10-10T00:03:00Z",
                            "finished_at": "2026-10-10T00:04:00Z",
                        }
                    ),
                }
            )
        )
        inventory = self.store.read_environment_inventory(
            context_name="cm", instance_name="testing"
        )
        replacement = self.store.read_deployment_record(replacement_identity.deployment_record_id)
        self.store.write_environment_inventory(
            inventory.model_copy(
                update={
                    "deployment_record_id": replacement.record_id,
                    "runtime_identity": replacement_identity,
                    "deploy": replacement.deploy,
                    "updated_at": replacement.deploy.finished_at,
                }
            )
        )
        self.provider.config["Config"]["Env"] = [
            f"{k}={v}" for k, v in runtime_identity_env(replacement_identity).items()
        ]
        self.payload["mode"] = "reconcile"
        # A new deployment record alone cannot settle an effect on the same container.
        self.assertEqual(self.invoke(key="old-container")[0], 409)
        self.provider.container_id = "f" * 64
        self.provider.config["Id"] = self.provider.container_id
        self.provider.config["State"]["StartedAt"] = "2026-10-10T00:03:00Z"
        # A different authorized admin can recover the current replacement even
        # while the original account's old-container receipt remains unknown.
        self.app = self.create_app(subject="replacement-admin")
        self.provider.unknown = False
        self.provider.restart_started_at = "2026-10-10T00:05:00Z"
        self.payload["mode"] = "dry-run"
        self.payload.pop("reviewed_plan_sha256")
        self.review()
        self.assertEqual(self.invoke(key="current-container")[1]["result"]["status"], "pass")
        self.assertEqual(
            self.provider.writes[-1]["payload"]["containerId"], self.provider.container_id
        )
        self.assertEqual(len(self.provider.writes), 2)
        self.assertTrue(self.store.list_held_provider_target_reservations())
        self.app = self.create_app()
        self.payload = {**original_payload, "mode": "reconcile"}
        status, response = self.invoke(key="old-container")
        self.assertEqual(status, 200, response)
        self.assertEqual(response["result"]["status"], "unknown")
        self.assertEqual(
            response["records"]["superseding_deployment_record_id"],
            replacement_identity.deployment_record_id,
        )
        self.assertEqual(len(self.provider.writes), 2)
        self.assertFalse(self.store.list_held_provider_target_reservations())

    def test_redacted_reason_activity_handle_matches_the_original_request(self) -> None:
        self.payload["reason"] = "Recover worker credential=must-not-leak"
        self.review()
        self.provider.unknown = True
        self.assertEqual(self.invoke(key="safe-reason")[0], 409)
        event = next(
            event
            for event in build_product_activity_read_model(
                record_store=self.store, product=self.expected.product
            ).events
            if event.event_type == "service_restart"
        )
        assert event.restart_recovery is not None
        self.assertNotIn("must-not-leak", event.model_dump_json())
        self.payload = event.restart_recovery.request.model_dump(mode="json")
        self.payload["mode"] = "reconcile"
        self.provider.config["State"]["StartedAt"] = datetime.now(timezone.utc).isoformat()
        self.assertEqual(self.invoke(key=event.restart_recovery.idempotency_key)[0], 200)

    def test_helper_refuses_existing_evidence_before_loading_transport_or_requesting(self) -> None:
        helper = runpy.run_path(
            str(Path(__file__).resolve().parents[1] / "scripts" / "restart-lane-service.py")
        )["main"]
        evidence = self.root / "review.json"
        evidence.write_text("retained review")
        args = [
            "restart-lane-service.py",
            "dry-run",
            "--operator-helper",
            "/unavailable/launchplane-write-action.py",
            "--product",
            "example",
            "--context",
            "example",
            "--instance",
            "testing",
            "--service",
            "web",
            "--reason",
            "Recover worker.",
            "--evidence-file",
            str(evidence),
        ]
        stderr = io.StringIO()
        with (
            patch.object(sys, "argv", args),
            patch("runpy.run_path", side_effect=AssertionError("transport loaded")),
            redirect_stderr(stderr),
            self.assertRaises(SystemExit) as exited,
        ):
            helper()
        self.assertEqual(exited.exception.code, 2)
        self.assertEqual(evidence.read_text(), "retained review")

    def use_generic_web(self) -> None:
        profile = self.store.read_product_profile_record(self.expected.product)
        self.store.write_product_profile_record(
            profile.model_copy(update={"driver_id": "generic-web"})
        )
        record_safety(self.store, self.expected.product, self.expected.context, "testing")

    def test_generic_web_restarts_current_artifact_and_replays_without_another_effect(self) -> None:
        self.use_generic_web()
        self.provider.config["State"]["Running"] = False
        self.store.clear_disposable_lane_safety(
            self.expected.product, self.expected.context, "testing"
        )
        reviewed = self.review()
        status, response = self.invoke(key="generic-restart")
        self.assertEqual(status, 200, response)
        self.assertEqual(response["result"]["status"], "pass")
        self.assertEqual(response["result"]["plan"], reviewed["result"]["plan"])
        self.assertTrue(self.invoke(key="generic-restart")[1]["replayed"])
        self.assertEqual(len(self.provider.writes), 1)
        self.health.assert_called_once()

    def test_generic_web_rechecks_reviewed_configuration_under_release_exclusion(self) -> None:
        self.use_generic_web()
        self.review()
        with patch.object(self.store, "serialize_preview_refresh") as serialize:
            with serialize_generic_web_product_operation(
                record_store=self.store, product=self.expected.product
            ):
                pass
        deploy_lock = serialize.call_args.kwargs["preview_id"]

        @contextmanager
        def release_finishes(**kwargs: object) -> Iterator[None]:
            if kwargs["preview_id"] == deploy_lock and not kwargs["shared"]:
                self.provider.config["Mounts"].append({"Name": "replacement-volume"})
            yield

        with patch.object(self.store, "serialize_preview_refresh", side_effect=release_finishes):
            status, response = self.invoke(key="generic-release-race")
        self.assertEqual(status, 409, response)
        self.assertEqual(self.provider.writes, [])

    def test_generic_web_production_agent_with_no_apply_authority_never_inspects(self) -> None:
        self.use_generic_web()
        self.payload.update(instance="prod", mode="apply", reviewed_plan_sha256="a" * 64)
        self.app = self.create_app(
            policy_runtime=LaunchplaneAuthzPolicyRuntime(LaunchplaneAuthzPolicy())
        )
        with patch(
            "control_plane.http_routes.lane_service_restart.plan_service_restart",
            side_effect=AssertionError("denied provider read"),
        ):
            status, response = self.invoke(key="generic-production")
        self.assertEqual(status, 403, response)
        self.assertEqual(response["error"]["code"], "authorization_denied")
        self.assertEqual(self.provider.writes, [])

    def test_generic_web_refuses_missing_health_or_current_artifact_identity(self) -> None:
        self.use_generic_web()
        self.provider.config["Config"]["Labels"]["com.docker.compose.service"] = "db"
        self.payload["service"] = "db"
        self.provider.config["State"].pop("Health")
        self.assertEqual(self.invoke()[0], 409)
        self.provider.config["State"]["Health"] = {"Status": "unhealthy"}
        self.provider.config["Config"]["Env"] = []
        self.assertEqual(self.invoke()[0], 409)
        self.assertEqual(self.provider.writes, [])

    def test_generic_web_unknown_restart_reconciles_without_a_second_effect(self) -> None:
        self.use_generic_web()
        self.review()
        self.provider.unknown = True
        self.assertEqual(self.invoke(key="generic-unknown")[0], 409)
        self.assertEqual(self.invoke(key="generic-unknown")[0], 409)
        self.payload["mode"] = "reconcile"
        self.provider.config["State"]["StartedAt"] = datetime.now(timezone.utc).isoformat()
        status, response = self.invoke(key="generic-unknown")
        self.assertEqual(status, 200, response)
        self.assertEqual(response["result"]["status"], "pass")
        self.assertEqual(len(self.provider.writes), 1)
