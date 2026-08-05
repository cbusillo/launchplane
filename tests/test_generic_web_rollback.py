import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal, cast
from unittest.mock import patch

from fastapi import FastAPI
from pydantic import ValidationError

from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.deployment_record import DeploymentRecord, ResolvedTargetEvidence
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.generic_web_rollback import GenericWebRollbackBlocker
from control_plane.contracts.generic_web_rollback import (
    GenericWebRollbackPlanRequest,
    build_generic_web_rollback_plan,
    execute_generic_web_rollback_plan,
)
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductImageProfile,
    ProductLaneProfile,
)
from control_plane.contracts.promotion_record import ArtifactIdentityReference, HealthcheckEvidence
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.contracts.ship_request import ShipRequest
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import GitHubActionsIdentity, LaunchplaneAuthzPolicy
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.workflows.generic_web_deploy import (
    GenericWebDeployResult,
    GenericWebPostDeployExecutor,
)
from control_plane.workflows.generic_web_rollback import (
    GenericWebRollbackApplyResult,
    execute_generic_web_rollback,
)
from control_plane.workflows.odoo_generic_web_post_deploy import (
    execute_odoo_generic_web_post_deploy,
)
from control_plane.workflows.ship import build_deployment_record
from tests.http_app_test_support import (
    _asgi_get,
    _post_generic_web_rollback,
    _post_generic_web_rollback_plan,
)
from tests.support.auth import _StubVerifier, _identity


class _GenericWebRollbackStore:
    def __init__(self, profile: LaunchplaneProductProfileRecord) -> None:
        self.profile = profile
        self.deployments: dict[str, DeploymentRecord] = {}
        self.backup_gates: dict[str, BackupGateRecord] = {}
        self.rollback_plans: list[object] = []

    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord:
        if product != self.profile.product:
            raise FileNotFoundError(product)
        return self.profile

    def read_deployment_record(self, record_id: str) -> DeploymentRecord:
        try:
            return self.deployments[record_id]
        except KeyError as exc:
            raise FileNotFoundError(record_id) from exc

    def read_backup_gate_record(self, record_id: str) -> BackupGateRecord:
        try:
            return self.backup_gates[record_id]
        except KeyError as exc:
            raise FileNotFoundError(record_id) from exc

    def write_generic_web_rollback_plan_record(self, record: object) -> None:
        self.rollback_plans.append(record)

    def write_deployment_record(self, record: DeploymentRecord) -> None:
        self.deployments[record.record_id] = record

    def write_environment_inventory(self, record: EnvironmentInventory) -> None:
        _ = record
        return None


def _profile() -> LaunchplaneProductProfileRecord:
    return LaunchplaneProductProfileRecord(
        product="sellyouroutboard",
        display_name="SellYourOutboard.com",
        repository="cbusillo/sellyouroutboard",
        driver_id="generic-web",
        image=ProductImageProfile(repository="ghcr.io/cbusillo/sellyouroutboard"),
        runtime_port=3000,
        health_path="/api/health",
        lanes=(
            ProductLaneProfile(
                instance="testing",
                context="sellyouroutboard-testing",
                base_url="https://testing.sellyouroutboard.com",
            ),
            ProductLaneProfile(
                instance="prod",
                context="sellyouroutboard-testing",
                base_url="https://www.sellyouroutboard.com",
            ),
        ),
        updated_at="2026-05-01T21:00:00Z",
        source="test",
    )


def _inherited_profile() -> LaunchplaneProductProfileRecord:
    return _profile().model_copy(update={"driver_id": "odoo", "product": "cm"})


def _request(**overrides: object) -> GenericWebRollbackPlanRequest:
    payload: dict[str, object] = {
        "product": "sellyouroutboard",
        "instance": "prod",
        "rollback_deployment_record_id": "deployment-syo-prod-previous",
    }
    payload.update(overrides)
    return GenericWebRollbackPlanRequest.model_validate(payload)


def _deployment_record(**overrides: object) -> DeploymentRecord:
    artifact_id = cast(
        str, overrides.pop("artifact_id", "ghcr.io/cbusillo/sellyouroutboard@sha256:abc123")
    )
    source_git_ref = cast(str, overrides.pop("source_git_ref", "abc123"))
    context = cast(str, overrides.pop("context", "sellyouroutboard-testing"))
    instance = cast(str, overrides.pop("instance", "prod"))
    destination_health = overrides.pop("destination_health", HealthcheckEvidence(status="pass"))
    deploy_status = cast(
        Literal["pending", "pass", "fail", "skipped"], overrides.pop("deploy_status", "pass")
    )
    runtime_identity = cast(RuntimeIdentity | None, overrides.pop("runtime_identity", None))
    if runtime_identity is None:
        runtime_identity = RuntimeIdentity(
            product="sellyouroutboard",
            context=context,
            instance=instance,
            deployment_record_id="deployment-syo-prod-previous",
            artifact_id=artifact_id,
            source_git_ref=source_git_ref,
            image_reference="ghcr.io/cbusillo/sellyouroutboard:sha-abc123",
        )
    ship_request = ShipRequest(
        artifact_id=artifact_id,
        context=context,
        instance=instance,
        source_git_ref=source_git_ref,
        target_name="syo-prod-app",
        target_type="application",
        provider_id="dokploy",
        target_category="application",
        provider_target_type="application",
        deploy_mode="dokploy-application-api",
        verify_health=False,
        destination_health=HealthcheckEvidence(status="skipped"),
    )
    deployment = build_deployment_record(
        request=ship_request,
        record_id="deployment-syo-prod-previous",
        deployment_id="control-plane-dokploy",
        deployment_status=deploy_status,
        started_at="2026-05-01T21:00:00Z",
        finished_at="2026-05-01T21:01:00Z",
        resolved_target=ResolvedTargetEvidence(
            target_type="application",
            target_id="app-123",
            target_name="syo-prod-app",
        ),
        runtime_identity=runtime_identity,
    )
    return deployment.model_copy(
        update={
            "artifact_identity": ArtifactIdentityReference(artifact_id=artifact_id),
            "destination_health": destination_health,
            **overrides,
        }
    )


def _backup_gate(**overrides: object) -> BackupGateRecord:
    payload: dict[str, object] = {
        "record_id": "backup-syo-prod-pass",
        "context": "sellyouroutboard-testing",
        "instance": "prod",
        "created_at": "2026-05-01T20:59:00Z",
        "source": "test",
        "required": True,
        "status": "pass",
        "evidence": {"snapshot": "syo-prod-20260501"},
    }
    payload.update(overrides)
    return BackupGateRecord.model_validate(payload)


def _rollback_route_policy(
    *actions: str,
    product: str = "sellyouroutboard",
    context: str = "sellyouroutboard-testing",
    repository: str = "every/verireel",
    workflow_ref: str = "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main",
    event_name: str = "pull_request",
) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": repository,
                    "workflow_refs": [workflow_ref],
                    "event_names": [event_name],
                    "products": [product],
                    "contexts": [context],
                    "actions": list(actions),
                }
            ]
        }
    )


def _rollback_plan_payload(**overrides: object) -> dict[str, object]:
    rollback_plan: dict[str, object] = {
        "schema_version": 1,
        "product": "sellyouroutboard",
        "instance": "prod",
        "rollback_deployment_record_id": "deployment-syo-prod-previous",
    }
    rollback_plan.update(cast(dict[str, object], overrides.pop("rollback_plan", {})))
    payload: dict[str, object] = {
        "schema_version": 1,
        "product": "sellyouroutboard",
        "rollback_plan": rollback_plan,
    }
    payload.update(overrides)
    return payload


def _rollback_payload(**overrides: object) -> dict[str, object]:
    rollback: dict[str, object] = {
        "schema_version": 1,
        "product": "sellyouroutboard",
        "instance": "prod",
        "rollback_deployment_record_id": "deployment-syo-prod-previous",
    }
    rollback.update(cast(dict[str, object], overrides.pop("rollback", {})))
    payload: dict[str, object] = {
        "schema_version": 1,
        "product": "sellyouroutboard",
        "rollback": rollback,
    }
    payload.update(overrides)
    return payload


def _fastapi_rollback_identity(
    *,
    repository: str = "every/verireel",
    workflow_ref: str = "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main",
    event_name: str = "pull_request",
) -> GitHubActionsIdentity:
    return _identity(
        repository=repository,
        workflow_ref=workflow_ref,
        event_name=event_name,
    )


def _odoo_inherited_profile() -> LaunchplaneProductProfileRecord:
    return _profile().model_copy(
        update={
            "product": "odoo-tenant-cm",
            "driver_id": "odoo",
            "lanes": (
                ProductLaneProfile(
                    instance="prod",
                    context="cm",
                    base_url="https://cm.example.com",
                ),
            ),
        }
    )


class GenericWebRollbackPlanTests(unittest.TestCase):
    def test_builds_ready_plan_from_previous_good_deployment_record(self) -> None:
        store = _GenericWebRollbackStore(_profile())
        store.deployments["deployment-syo-prod-previous"] = _deployment_record()

        plan = build_generic_web_rollback_plan(record_store=store, request=_request())

        self.assertEqual(plan.status, "ready")
        self.assertEqual(plan.context, "sellyouroutboard-testing")
        self.assertEqual(
            plan.artifact_identity,
            ArtifactIdentityReference(
                artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:abc123"
            ),
        )
        self.assertIsNotNone(plan.planned_deploy)
        planned_deploy = plan.planned_deploy
        assert planned_deploy is not None
        self.assertEqual(
            planned_deploy.artifact_id,
            "ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
        )
        self.assertEqual(planned_deploy.source_git_ref, "abc123")
        self.assertEqual(
            planned_deploy.deploy_reference,
            "ghcr.io/cbusillo/sellyouroutboard:sha-abc123",
        )
        self.assertEqual(plan.backup_gate.status, "skipped")

    def test_plan_blocks_application_digest_without_deploy_reference(self) -> None:
        artifact_id = "ghcr.io/cbusillo/sellyouroutboard@sha256:abc123"
        store = _GenericWebRollbackStore(_profile())
        store.deployments["deployment-syo-prod-previous"] = _deployment_record(
            runtime_identity=RuntimeIdentity(
                product="sellyouroutboard",
                context="sellyouroutboard-testing",
                instance="prod",
                deployment_record_id="deployment-syo-prod-previous",
                artifact_id=artifact_id,
                source_git_ref="abc123",
            )
        )

        plan = build_generic_web_rollback_plan(record_store=store, request=_request())

        self.assertEqual(plan.status, "blocked")
        self.assertIsNone(plan.planned_deploy)
        self.assertIn("missing_deploy_reference", [blocker.code for blocker in plan.blockers])

    def test_plan_preserves_provider_deploy_reference_for_digest_identity(self) -> None:
        artifact_id = "ghcr.io/cbusillo/sellyouroutboard@sha256:abc123"
        deploy_reference = "ghcr.io/cbusillo/sellyouroutboard:sha-abcdef1234567890"
        store = _GenericWebRollbackStore(_profile())
        store.deployments["deployment-syo-prod-previous"] = _deployment_record(
            runtime_identity=RuntimeIdentity(
                product="sellyouroutboard",
                context="sellyouroutboard-testing",
                instance="prod",
                deployment_record_id="deployment-syo-prod-previous",
                artifact_id=artifact_id,
                source_git_ref="abc123",
                image_reference=deploy_reference,
            )
        )

        plan = build_generic_web_rollback_plan(record_store=store, request=_request())

        self.assertIsNotNone(plan.planned_deploy)
        assert plan.planned_deploy is not None
        self.assertEqual(plan.planned_deploy.artifact_id, artifact_id)
        self.assertEqual(plan.planned_deploy.deploy_reference, deploy_reference)

    def test_execute_writes_ready_plan_record(self) -> None:
        store = _GenericWebRollbackStore(_profile())
        store.deployments["deployment-syo-prod-previous"] = _deployment_record()

        plan = execute_generic_web_rollback_plan(record_store=store, request=_request())

        self.assertEqual(plan.status, "ready")
        self.assertEqual(store.rollback_plans, [plan])

    def test_builds_plan_for_generic_web_based_driver(self) -> None:
        store = _GenericWebRollbackStore(_inherited_profile())
        store.deployments["deployment-syo-prod-previous"] = _deployment_record()

        plan = build_generic_web_rollback_plan(
            record_store=store,
            request=_request(product="cm"),
        )

        self.assertEqual(plan.status, "ready")
        self.assertEqual(plan.product, "cm")
        self.assertEqual(plan.instance, "prod")

    def test_execute_apply_writes_plan_before_deploying_previous_artifact(self) -> None:
        store = _GenericWebRollbackStore(_profile())
        store.deployments["deployment-syo-prod-previous"] = _deployment_record()
        deploy_result = GenericWebDeployResult(
            deployment_record_id="deployment-syo-prod-rollback",
            deploy_status="pass",
            deploy_started_at="2026-05-25T12:00:00Z",
            deploy_finished_at="2026-05-25T12:01:00Z",
            product="sellyouroutboard",
            context="sellyouroutboard-testing",
            instance="prod",
            target_name="syo-prod-app",
            target_category="application",
            provider_id="dokploy",
            provider_target_type="application",
            target_id="app-prod",
        )

        with patch(
            "control_plane.workflows.generic_web_rollback.execute_generic_web_deploy",
            return_value=deploy_result,
        ) as deploy:
            result = execute_generic_web_rollback(
                control_plane_root=__import__("pathlib").Path("/tmp/launchplane"),
                record_store=store,
                request=_request(timeout_seconds=90, no_cache=True),
            )

        self.assertEqual(result.rollback_status, "pass")
        self.assertEqual(result.deploy_status, "pass")
        self.assertEqual(result.deployment_record_id, "deployment-syo-prod-rollback")
        self.assertEqual(len(store.rollback_plans), 1)
        deploy.assert_called_once()
        deploy_request = deploy.call_args.kwargs["request"]
        self.assertEqual(deploy_request.product, "sellyouroutboard")
        self.assertEqual(deploy_request.instance, "prod")
        self.assertEqual(
            deploy_request.artifact_id,
            "ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
        )
        self.assertEqual(
            deploy_request.deploy_reference,
            "ghcr.io/cbusillo/sellyouroutboard:sha-abc123",
        )
        self.assertEqual(deploy_request.source_git_ref, "abc123")
        self.assertEqual(deploy_request.timeout_seconds, 90)
        self.assertTrue(deploy_request.no_cache)

    def test_execute_apply_passes_rollback_deploy_reference(self) -> None:
        artifact_id = "ghcr.io/cbusillo/sellyouroutboard@sha256:abc123"
        deploy_reference = "ghcr.io/cbusillo/sellyouroutboard:sha-abcdef1234567890"
        store = _GenericWebRollbackStore(_profile())
        store.deployments["deployment-syo-prod-previous"] = _deployment_record(
            runtime_identity=RuntimeIdentity(
                product="sellyouroutboard",
                context="sellyouroutboard-testing",
                instance="prod",
                deployment_record_id="deployment-syo-prod-previous",
                artifact_id=artifact_id,
                source_git_ref="abc123",
                image_reference=deploy_reference,
            )
        )

        with patch(
            "control_plane.workflows.generic_web_rollback.execute_generic_web_deploy",
            return_value=GenericWebDeployResult(
                deployment_record_id="deployment-syo-prod-rollback",
                deploy_status="pass",
                deploy_started_at="2026-05-25T12:00:00Z",
                deploy_finished_at="2026-05-25T12:01:00Z",
                product="sellyouroutboard",
                context="sellyouroutboard-testing",
                instance="prod",
                target_name="syo-prod-app",
                target_category="application",
                provider_id="dokploy",
                provider_target_type="application",
                target_id="app-prod",
            ),
        ) as deploy:
            execute_generic_web_rollback(
                control_plane_root=Path("/tmp/launchplane"),
                record_store=store,
                request=_request(),
            )

        deploy_request = deploy.call_args.kwargs["request"]
        self.assertEqual(deploy_request.artifact_id, artifact_id)
        self.assertEqual(deploy_request.deploy_reference, deploy_reference)

    def test_execute_apply_forwards_post_deploy_extension_to_generic_deploy(self) -> None:
        store = _GenericWebRollbackStore(_profile())
        store.deployments["deployment-syo-prod-previous"] = _deployment_record()
        deploy_result = GenericWebDeployResult(
            deployment_record_id="deployment-syo-prod-rollback",
            deploy_status="pass",
            deploy_started_at="2026-05-25T12:00:00Z",
            deploy_finished_at="2026-05-25T12:01:00Z",
            product="sellyouroutboard",
            context="sellyouroutboard-testing",
            instance="prod",
            target_name="syo-prod-app",
            target_category="application",
            provider_id="dokploy",
            provider_target_type="application",
            target_id="app-prod",
            post_deploy_status="pass",
        )

        def post_deploy_executor() -> None:
            return None

        with patch(
            "control_plane.workflows.generic_web_rollback.execute_generic_web_deploy",
            return_value=deploy_result,
        ) as deploy:
            result = execute_generic_web_rollback(
                control_plane_root=__import__("pathlib").Path("/tmp/launchplane"),
                record_store=store,
                request=_request(),
                post_deploy_executor=cast(GenericWebPostDeployExecutor, post_deploy_executor),
            )

        self.assertEqual(result.rollback_status, "pass")
        deploy.assert_called_once()
        self.assertIs(deploy.call_args.kwargs["post_deploy_executor"], post_deploy_executor)

    def test_execute_apply_fails_when_post_deploy_extension_fails(self) -> None:
        store = _GenericWebRollbackStore(_profile())
        store.deployments["deployment-syo-prod-previous"] = _deployment_record()
        deploy_result = GenericWebDeployResult(
            deployment_record_id="deployment-syo-prod-rollback",
            deploy_status="pass",
            deploy_started_at="2026-05-25T12:00:00Z",
            deploy_finished_at="2026-05-25T12:01:00Z",
            product="sellyouroutboard",
            context="sellyouroutboard-testing",
            instance="prod",
            target_name="syo-prod-app",
            target_category="application",
            provider_id="dokploy",
            provider_target_type="application",
            target_id="app-prod",
            post_deploy_status="fail",
            error_message="post deploy failed",
        )

        with patch(
            "control_plane.workflows.generic_web_rollback.execute_generic_web_deploy",
            return_value=deploy_result,
        ):
            result = execute_generic_web_rollback(
                control_plane_root=__import__("pathlib").Path("/tmp/launchplane"),
                record_store=store,
                request=_request(),
            )

        self.assertEqual(result.rollback_status, "fail")
        self.assertEqual(result.deploy_status, "pass")
        self.assertEqual(result.error_message, "post deploy failed")

    def test_execute_apply_returns_blocked_without_deploying(self) -> None:
        store = _GenericWebRollbackStore(_profile())

        with patch(
            "control_plane.workflows.generic_web_rollback.execute_generic_web_deploy"
        ) as deploy:
            result = execute_generic_web_rollback(
                control_plane_root=__import__("pathlib").Path("/tmp/launchplane"),
                record_store=store,
                request=_request(),
            )

        self.assertEqual(result.rollback_status, "blocked")
        self.assertEqual(result.deploy_status, "skipped")
        self.assertEqual(result.deployment_record_id, "")
        self.assertEqual([blocker.code for blocker in result.blockers], ["missing_rollback_target"])
        self.assertEqual(len(store.rollback_plans), 1)
        deploy.assert_not_called()

    def test_blocks_missing_rollback_target(self) -> None:
        store = _GenericWebRollbackStore(_profile())

        plan = build_generic_web_rollback_plan(record_store=store, request=_request())

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(plan.planned_deploy, None)
        self.assertEqual([blocker.code for blocker in plan.blockers], ["missing_rollback_target"])

    def test_blocks_failed_health_evidence(self) -> None:
        store = _GenericWebRollbackStore(_profile())
        store.deployments["deployment-syo-prod-previous"] = _deployment_record(
            destination_health=HealthcheckEvidence(
                verified=True,
                urls=("https://www.sellyouroutboard.com/api/health",),
                timeout_seconds=60,
                status="fail",
            )
        )

        plan = build_generic_web_rollback_plan(record_store=store, request=_request())

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(plan.target_health.status, "fail")
        self.assertEqual([blocker.code for blocker in plan.blockers], ["health_evidence_failed"])

    def test_blocks_backup_gate_required_but_missing(self) -> None:
        store = _GenericWebRollbackStore(_profile())
        store.deployments["deployment-syo-prod-previous"] = _deployment_record()

        plan = build_generic_web_rollback_plan(
            record_store=store,
            request=_request(backup_required=True, backup_record_id="backup-syo-prod-pass"),
        )

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(plan.backup_record_id, "backup-syo-prod-pass")
        self.assertEqual([blocker.code for blocker in plan.blockers], ["backup_gate_missing"])

    def test_request_rejects_backup_required_without_record_id(self) -> None:
        with self.assertRaises(ValidationError) as caught:
            _request(backup_required=True)

        self.assertIn("requires backup_record_id", str(caught.exception))

    def test_allows_passing_backup_gate_for_destination_lane(self) -> None:
        store = _GenericWebRollbackStore(_profile())
        store.deployments["deployment-syo-prod-previous"] = _deployment_record()
        store.backup_gates["backup-syo-prod-pass"] = _backup_gate()

        plan = build_generic_web_rollback_plan(
            record_store=store,
            request=_request(backup_required=True, backup_record_id="backup-syo-prod-pass"),
        )

        self.assertEqual(plan.status, "ready")
        self.assertEqual(plan.backup_gate.status, "pass")
        self.assertEqual(plan.backup_gate.evidence, {"snapshot": "syo-prod-20260501"})

    def test_blocks_mutable_artifact_reference(self) -> None:
        store = _GenericWebRollbackStore(_profile())
        store.deployments["deployment-syo-prod-previous"] = _deployment_record(
            artifact_id="ghcr.io/cbusillo/sellyouroutboard:latest"
        )

        plan = build_generic_web_rollback_plan(record_store=store, request=_request())

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(
            [blocker.code for blocker in plan.blockers], ["mutable_artifact_reference"]
        )


class FastApiGenericWebRollbackTests(unittest.IsolatedAsyncioTestCase):
    def _app(
        self,
        *,
        root: Path,
        store: FilesystemRecordStore,
        policy: LaunchplaneAuthzPolicy,
        identity: GitHubActionsIdentity | None = None,
    ) -> FastAPI:
        return create_launchplane_fastapi_app(
            verifier=_StubVerifier(identity or _fastapi_rollback_identity()),
            authz_policy=policy,
            record_store_factory=lambda: store,
            control_plane_root_path=root,
        )

    def _write_profile_and_previous_deployment(
        self,
        store: FilesystemRecordStore,
        *,
        profile: LaunchplaneProductProfileRecord | None = None,
        deployment: DeploymentRecord | None = None,
    ) -> None:
        store.write_product_profile_record(profile or _profile())
        store.write_deployment_record(deployment or _deployment_record())

    async def test_rollback_plan_route_writes_plan_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            self._write_profile_and_previous_deployment(store)
            app = self._app(
                root=root,
                store=store,
                policy=_rollback_route_policy("generic_web_prod_rollback.plan"),
            )

            response = await _post_generic_web_rollback_plan(
                app,
                _rollback_plan_payload(),
                idempotency_key="generic-web-rollback-plan-syo-prod",
            )
            plans = store.list_generic_web_rollback_plan_records(
                context_name="sellyouroutboard-testing",
                instance_name="prod",
                limit=1,
            )

        payload = response.json()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(len(plans), 1)
        self.assertEqual(payload["records"]["generic_web_rollback_plan_id"], plans[0].plan_id)
        self.assertEqual(payload["result"]["status"], "ready")
        self.assertEqual(plans[0].product, "sellyouroutboard")
        self.assertEqual(plans[0].context, "sellyouroutboard-testing")

    async def test_rollback_plan_route_rejects_unknown_lane(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_product_profile_record(_profile())
            app = self._app(
                root=root,
                store=store,
                policy=_rollback_route_policy("generic_web_prod_rollback.plan"),
            )

            response = await _post_generic_web_rollback_plan(
                app,
                _rollback_plan_payload(rollback_plan={"instance": "missing"}),
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "product_driver_mismatch")

    async def test_rollback_plan_route_rejects_non_generic_web_profile(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_product_profile_record(
                _profile().model_copy(update={"driver_id": "ingress"})
            )
            app = self._app(
                root=root,
                store=store,
                policy=_rollback_route_policy("generic_web_prod_rollback.plan"),
            )

            response = await _post_generic_web_rollback_plan(
                app,
                _rollback_plan_payload(),
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "product_driver_mismatch")

    async def test_rollback_plan_route_reports_missing_profile_dependency(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            app = self._app(
                root=root,
                store=store,
                policy=_rollback_route_policy("generic_web_prod_rollback.plan"),
            )

            response = await _post_generic_web_rollback_plan(
                app,
                _rollback_plan_payload(),
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "driver_route_dependency_not_found")

    async def test_rollback_plan_route_rejects_unauthorized_context(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_product_profile_record(_profile())
            app = self._app(
                root=root,
                store=store,
                policy=_rollback_route_policy(
                    "generic_web_prod_rollback.plan", context="other-context"
                ),
            )

            response = await _post_generic_web_rollback_plan(
                app,
                _rollback_plan_payload(),
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_rollback_plan_route_replays_idempotent_response(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            self._write_profile_and_previous_deployment(store)
            app = self._app(
                root=root,
                store=store,
                policy=_rollback_route_policy("generic_web_prod_rollback.plan"),
            )
            request_payload = _rollback_plan_payload()

            first = await _post_generic_web_rollback_plan(
                app,
                request_payload,
                idempotency_key="generic-web-rollback-plan-replay-syo-prod",
            )
            second = await _post_generic_web_rollback_plan(
                app,
                request_payload,
                idempotency_key="generic-web-rollback-plan-replay-syo-prod",
            )
            plans = store.list_generic_web_rollback_plan_records(
                context_name="sellyouroutboard-testing",
                instance_name="prod",
                limit=10,
            )

        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)
        self.assertEqual(first.json()["records"], second.json()["records"])
        self.assertTrue(second.json()["replayed"])
        self.assertEqual(len(plans), 1)

    async def test_rollback_plan_route_does_not_cache_blocked_result(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_product_profile_record(_profile())
            app = self._app(
                root=root,
                store=store,
                policy=_rollback_route_policy("generic_web_prod_rollback.plan"),
            )
            request_payload = _rollback_plan_payload()

            first = await _post_generic_web_rollback_plan(
                app,
                request_payload,
                idempotency_key="generic-web-rollback-plan-blocked-syo-prod",
            )
            second = await _post_generic_web_rollback_plan(
                app,
                request_payload,
                idempotency_key="generic-web-rollback-plan-blocked-syo-prod",
            )
            plans = store.list_generic_web_rollback_plan_records(
                context_name="sellyouroutboard-testing",
                instance_name="prod",
                limit=10,
            )

        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)
        self.assertEqual(first.json()["result"]["status"], "blocked")
        self.assertNotIn("replayed", second.json())
        self.assertEqual(len(plans), 1)

    async def test_rollback_plan_route_returns_404_for_workflow_file_miss(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            self._write_profile_and_previous_deployment(store)
            app = self._app(
                root=root,
                store=store,
                policy=_rollback_route_policy("generic_web_prod_rollback.plan"),
            )

            with patch(
                "control_plane.generic_web_rollback_http.execute_generic_web_rollback_plan",
                side_effect=FileNotFoundError("missing rollback source"),
            ):
                response = await _post_generic_web_rollback_plan(
                    app,
                    _rollback_plan_payload(),
                )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "not_found")

    async def test_rollback_route_applies_ready_plan(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_product_profile_record(_profile())
            app = self._app(
                root=root,
                store=store,
                policy=_rollback_route_policy("generic_web_prod_rollback.execute"),
            )
            driver_result = GenericWebRollbackApplyResult(
                plan_id="generic-web-rollback-syo-prod",
                deployment_record_id="deployment-syo-prod-rollback",
                rollback_status="pass",
                deploy_status="pass",
                product="sellyouroutboard",
                context="sellyouroutboard-testing",
                instance="prod",
                rollback_deployment_record_id="deployment-syo-prod-previous",
            )

            with patch(
                "control_plane.generic_web_rollback_http.execute_generic_web_rollback",
                return_value=driver_result,
            ) as rollback:
                response = await _post_generic_web_rollback(
                    app,
                    _rollback_payload(),
                    idempotency_key="generic-web-rollback-syo-prod",
                )

        payload = response.json()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(
            payload["records"]["generic_web_rollback_plan_id"],
            "generic-web-rollback-syo-prod",
        )
        self.assertEqual(payload["records"]["deployment_record_id"], "deployment-syo-prod-rollback")
        self.assertEqual(payload["records"]["rollback_status"], "pass")
        self.assertEqual(payload["records"]["deploy_status"], "pass")
        self.assertEqual(payload["records"]["post_deploy_status"], "skipped")
        rollback.assert_called_once()
        self.assertEqual(rollback.call_args.kwargs["request"].product, "sellyouroutboard")
        self.assertIsNone(rollback.call_args.kwargs["post_deploy_executor"])

    async def test_rollback_route_reports_missing_profile_dependency(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            app = self._app(
                root=root,
                store=store,
                policy=_rollback_route_policy("generic_web_prod_rollback.execute"),
            )

            response = await _post_generic_web_rollback(app, _rollback_payload())

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "driver_route_dependency_not_found")

    async def test_rollback_route_returns_404_for_workflow_file_miss(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_product_profile_record(_profile())
            app = self._app(
                root=root,
                store=store,
                policy=_rollback_route_policy("generic_web_prod_rollback.execute"),
            )

            with patch(
                "control_plane.generic_web_rollback_http.execute_generic_web_rollback",
                side_effect=FileNotFoundError("missing rollback plan"),
            ):
                response = await _post_generic_web_rollback(app, _rollback_payload())

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "not_found")

    async def test_rollback_route_passes_odoo_post_deploy_adapter_for_odoo_profile(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_product_profile_record(_odoo_inherited_profile())
            identity = _fastapi_rollback_identity(
                repository="cbusillo/odoo-tenant-cm",
                workflow_ref=(
                    "cbusillo/odoo-tenant-cm/.github/workflows/deploy-odoo.yml@refs/heads/main"
                ),
                event_name="workflow_dispatch",
            )
            app = self._app(
                root=root,
                store=store,
                identity=identity,
                policy=_rollback_route_policy(
                    "generic_web_prod_rollback.execute",
                    product="odoo-tenant-cm",
                    context="cm",
                    repository="cbusillo/odoo-tenant-cm",
                    workflow_ref=(
                        "cbusillo/odoo-tenant-cm/.github/workflows/deploy-odoo.yml@refs/heads/main"
                    ),
                    event_name="workflow_dispatch",
                ),
            )
            driver_result = GenericWebRollbackApplyResult(
                plan_id="generic-web-rollback-cm-prod",
                deployment_record_id="deployment-cm-prod-rollback",
                rollback_status="pass",
                deploy_status="pass",
                product="odoo-tenant-cm",
                context="cm",
                instance="prod",
                rollback_deployment_record_id="deployment-cm-prod-previous",
            )

            with patch(
                "control_plane.generic_web_rollback_http.execute_generic_web_rollback",
                return_value=driver_result,
            ) as rollback:
                response = await _post_generic_web_rollback(
                    app,
                    _rollback_payload(
                        product="odoo-tenant-cm",
                        rollback={
                            "product": "odoo-tenant-cm",
                            "rollback_deployment_record_id": "deployment-cm-prod-previous",
                        },
                    ),
                    idempotency_key="generic-web-rollback-cm-prod",
                )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(
            response.json()["records"]["deployment_record_id"], "deployment-cm-prod-rollback"
        )
        rollback.assert_called_once()
        self.assertIs(
            rollback.call_args.kwargs["post_deploy_executor"],
            execute_odoo_generic_web_post_deploy,
        )

    async def test_rollback_route_replays_idempotent_response_shape(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_product_profile_record(_profile())
            app = self._app(
                root=root,
                store=store,
                policy=_rollback_route_policy("generic_web_prod_rollback.execute"),
            )
            driver_result = GenericWebRollbackApplyResult(
                plan_id="generic-web-rollback-syo-prod",
                deployment_record_id="deployment-syo-prod-rollback",
                rollback_status="pass",
                deploy_status="pass",
                product="sellyouroutboard",
                context="sellyouroutboard-testing",
                instance="prod",
                rollback_deployment_record_id="deployment-syo-prod-previous",
            )
            request_payload = _rollback_payload()

            with patch(
                "control_plane.generic_web_rollback_http.execute_generic_web_rollback",
                return_value=driver_result,
            ) as rollback:
                first = await _post_generic_web_rollback(
                    app,
                    request_payload,
                    idempotency_key="generic-web-rollback-replay-syo-prod",
                )
                second = await _post_generic_web_rollback(
                    app,
                    request_payload,
                    idempotency_key="generic-web-rollback-replay-syo-prod",
                )

        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)
        self.assertEqual(first.json()["records"], second.json()["records"])
        self.assertEqual(second.json()["records"]["rollback_status"], "pass")
        self.assertEqual(second.json()["records"]["deploy_status"], "pass")
        self.assertEqual(second.json()["records"]["post_deploy_status"], "skipped")
        self.assertTrue(second.json()["replayed"])
        rollback.assert_called_once()

    async def test_rollback_route_does_not_cache_blocked_result(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_product_profile_record(_profile())
            app = self._app(
                root=root,
                store=store,
                policy=_rollback_route_policy("generic_web_prod_rollback.execute"),
            )
            driver_result = GenericWebRollbackApplyResult(
                plan_id="generic-web-rollback-syo-prod-blocked",
                rollback_status="blocked",
                product="sellyouroutboard",
                context="sellyouroutboard-testing",
                instance="prod",
                rollback_deployment_record_id="deployment-syo-prod-previous",
                blockers=(
                    GenericWebRollbackBlocker(
                        code="missing_rollback_target",
                        message="No rollback deployment record found.",
                    ),
                ),
            )
            request_payload = _rollback_payload()

            with patch(
                "control_plane.generic_web_rollback_http.execute_generic_web_rollback",
                return_value=driver_result,
            ) as rollback:
                first = await _post_generic_web_rollback(
                    app,
                    request_payload,
                    idempotency_key="generic-web-rollback-blocked-syo-prod",
                )
                second = await _post_generic_web_rollback(
                    app,
                    request_payload,
                    idempotency_key="generic-web-rollback-blocked-syo-prod",
                )

        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)
        self.assertEqual(first.json()["records"]["rollback_status"], "blocked")
        self.assertNotIn("replayed", second.json())
        self.assertEqual(rollback.call_count, 2)

    async def test_rollback_route_does_not_cache_failed_deploy_result(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_product_profile_record(_profile())
            app = self._app(
                root=root,
                store=store,
                policy=_rollback_route_policy("generic_web_prod_rollback.execute"),
            )
            driver_result = GenericWebRollbackApplyResult(
                plan_id="generic-web-rollback-syo-prod-fail",
                deployment_record_id="deployment-syo-prod-rollback",
                rollback_status="fail",
                deploy_status="fail",
                product="sellyouroutboard",
                context="sellyouroutboard-testing",
                instance="prod",
                rollback_deployment_record_id="deployment-syo-prod-previous",
                error_message="deploy failed",
            )
            request_payload = _rollback_payload()

            with patch(
                "control_plane.generic_web_rollback_http.execute_generic_web_rollback",
                return_value=driver_result,
            ) as rollback:
                first = await _post_generic_web_rollback(
                    app,
                    request_payload,
                    idempotency_key="generic-web-rollback-fail-syo-prod",
                )
                second = await _post_generic_web_rollback(
                    app,
                    request_payload,
                    idempotency_key="generic-web-rollback-fail-syo-prod",
                )

        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)
        self.assertEqual(first.json()["records"]["deploy_status"], "fail")
        self.assertNotIn("replayed", second.json())
        self.assertEqual(rollback.call_count, 2)

    async def test_rollback_route_caches_post_deploy_failure_after_deploy_passes(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_product_profile_record(_profile())
            app = self._app(
                root=root,
                store=store,
                policy=_rollback_route_policy("generic_web_prod_rollback.execute"),
            )
            driver_result = GenericWebRollbackApplyResult(
                plan_id="generic-web-rollback-syo-prod-post-deploy-fail",
                deployment_record_id="deployment-syo-prod-rollback",
                rollback_status="fail",
                deploy_status="pass",
                post_deploy_status="fail",
                product="sellyouroutboard",
                context="sellyouroutboard-testing",
                instance="prod",
                rollback_deployment_record_id="deployment-syo-prod-previous",
                error_message="post deploy failed",
            )
            request_payload = _rollback_payload()

            with patch(
                "control_plane.generic_web_rollback_http.execute_generic_web_rollback",
                return_value=driver_result,
            ) as rollback:
                first = await _post_generic_web_rollback(
                    app,
                    request_payload,
                    idempotency_key="generic-web-rollback-post-deploy-fail-syo-prod",
                )
                second = await _post_generic_web_rollback(
                    app,
                    request_payload,
                    idempotency_key="generic-web-rollback-post-deploy-fail-syo-prod",
                )

        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)
        self.assertEqual(second.json()["records"]["post_deploy_status"], "fail")
        self.assertTrue(second.json()["replayed"])
        rollback.assert_called_once()

    async def test_rollback_route_rejects_unauthorized_context(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_product_profile_record(_profile())
            app = self._app(
                root=root,
                store=store,
                policy=_rollback_route_policy(
                    "generic_web_prod_rollback.execute", context="other-context"
                ),
            )

            response = await _post_generic_web_rollback(app, _rollback_payload())

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_routes_are_in_openapi(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            app = self._app(
                root=root,
                store=store,
                policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
            )

            response = await _asgi_get(app, "/openapi.json")

        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertIn("/v1/drivers/generic-web/prod-rollback-plan", payload["paths"])
        self.assertIn("/v1/drivers/generic-web/prod-rollback", payload["paths"])


if __name__ == "__main__":
    unittest.main()
