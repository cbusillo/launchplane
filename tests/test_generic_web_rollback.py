import unittest
from typing import Literal, cast
from unittest.mock import patch

from pydantic import ValidationError

from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.deployment_record import DeploymentRecord, ResolvedTargetEvidence
from control_plane.contracts.environment_inventory import EnvironmentInventory
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
from control_plane.workflows.generic_web_deploy import (
    GenericWebDeployResult,
    GenericWebPostDeployExecutor,
)
from control_plane.workflows.generic_web_rollback import execute_generic_web_rollback
from control_plane.workflows.ship import build_deployment_record


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
    runtime_identity = RuntimeIdentity(
        product="sellyouroutboard",
        context=context,
        instance=instance,
        deployment_record_id="deployment-syo-prod-previous",
        artifact_id=artifact_id,
        source_git_ref=source_git_ref,
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
        self.assertEqual(plan.backup_gate.status, "skipped")

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
        self.assertEqual(deploy_request.source_git_ref, "abc123")
        self.assertEqual(deploy_request.timeout_seconds, 90)
        self.assertTrue(deploy_request.no_cache)

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


if __name__ == "__main__":
    unittest.main()
