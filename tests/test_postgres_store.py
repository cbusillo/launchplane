import unittest
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal
from unittest.mock import Mock, patch

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from click.testing import CliRunner
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql.schema import Index

from control_plane.cli import main
from control_plane.contracts.artifact_identity import (
    ArtifactIdentityManifest,
    ArtifactImageReference,
)
from control_plane.contracts.agent_write_intent import (
    AgentWriteIntentEvaluation,
    AgentWriteIntentRecord,
    AgentWriteIntentRequest,
    build_agent_write_intent_record_id,
)
from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.deploy_target import (
    DeployedTargetReference,
    DeployTargetCategory,
    ProviderTargetRecord,
)
from control_plane.contracts.deployment_record import DeploymentRecord, ResolvedTargetEvidence
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.edge_endpoint_record import EdgeEndpointRecord
from control_plane.contracts.edge_endpoint_record import EdgeEndpointStatus
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.every_code_preview_gate_record import EveryCodePreviewGateRecord
from control_plane.contracts.every_code_pr_feedback_record import EveryCodePrFeedbackRecord
from control_plane.contracts.every_code_work_request import EveryCodeWorkRequestRecord
from control_plane.contracts.generic_web_rollback import (
    GenericWebRollbackDeployPlan,
    GenericWebRollbackPlanRecord,
)
from control_plane.contracts.ingress_canary_route_record import IngressCanaryRouteRecord
from control_plane.contracts.ingress_route_audit_record import (
    IngressRouteAuditOperation,
    IngressRouteAuditRecord,
)
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.contracts.idempotency_record import build_launchplane_idempotency_record_id
from control_plane.contracts.private_health_endpoint_record import PrivateHealthEndpointRecord
from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchCandidate,
    MergeTrainBatchCandidateRecord,
    MergeTrainBatchEntry,
    MergeTrainBatchLandingPlanRecord,
    MergeTrainBatchRecordStatus,
    build_merge_train_batch_candidate_ref,
    build_merge_train_batch_id,
    build_merge_train_batch_landing_plan,
)
from control_plane.contracts.merge_train_stack_collapse import (
    MergeTrainStackCollapseEntry,
    MergeTrainStackCollapseMutation,
    MergeTrainStackCollapsePlan,
    MergeTrainStackCollapsePlanRecord,
    MergeTrainStackCollapseRecordStatus,
    build_merge_train_stack_collapse_id,
)
from control_plane.contracts.merge_train_policy import MergeTrainPolicyRecord
from control_plane.contracts.merge_train_run_record import (
    MergeTrainRunRecord,
    build_merge_train_run_record,
)
from control_plane.merge_train import (
    MergeTrainDryRunSnapshot,
    MergeTrainPullRequestSnapshot,
    build_merge_train_dry_run_result,
)
from control_plane.contracts.odoo_instance_override_record import OdooConfigParameterOverride
from control_plane.contracts.odoo_instance_override_record import OdooInstanceOverrideRecord
from control_plane.contracts.odoo_instance_override_record import OdooOverrideValue
from control_plane.contracts.odoo_stable_bootstrap_operation import (
    OdooStableBootstrapOperationRecord,
)
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.contracts.preview_desired_state_record import PreviewDesiredStateRecord
from control_plane.contracts.preview_enablement_record import PreviewEnablementRecord
from control_plane.contracts.preview_generation_record import (
    PreviewGenerationRecord,
    PreviewPullRequestSummary,
)
from control_plane.contracts.preview_inventory_scan_record import PreviewInventoryScanRecord
from control_plane.contracts.preview_lifecycle_cleanup_record import (
    PreviewLifecycleCleanupRecord,
    PreviewLifecycleCleanupResult,
)
from control_plane.contracts.preview_lifecycle_plan_record import (
    PreviewLifecycleDesiredPreview,
    PreviewLifecyclePlanRecord,
)
from control_plane.contracts.preview_pr_feedback_record import PreviewPrFeedbackRecord
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductImageProfile,
    ProductLaneProfile,
    ProductPreviewProfile,
)
from control_plane.contracts.promotion_record import (
    ArtifactIdentityReference,
    BackupGateEvidence,
    DeploymentEvidence,
    HealthcheckEvidence,
    PromotionRecord,
)
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord
from control_plane.contracts.runtime_environment_record import (
    RuntimeEnvironmentRecord,
    RuntimeEnvironmentScope,
)
from control_plane.contracts.runtime_key_safety_policy import (
    RuntimeKeySafetyPolicyRecord,
    RuntimeSecretSafetyRule,
)
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyAuditRecord
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyAuditStatus
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyPolicy
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyRequest
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneObservation
from control_plane.contracts.runner_host_hygiene import RunnerHostHygienePolicy
from control_plane.contracts.runner_host_hygiene import evaluate_runner_host_hygiene
from control_plane.contracts.runner_host_hygiene import plan_runner_host_hygiene_apply
from control_plane.contracts.runner_lane_inventory import build_runner_lane_inventory
from control_plane.contracts.runner_lane_registration import RunnerLaneRegistrationAuditRecord
from control_plane.contracts.runner_lane_registration import RunnerLaneRegistrationAuditStatus
from control_plane.contracts.runner_lane_registration import RunnerLaneRegistrationPolicy
from control_plane.contracts.runner_lane_registration import RunnerLaneRegistrationRequest
from control_plane.contracts.runner_lane_registration import plan_runner_lane_registration
from control_plane.contracts.secret_record import (
    SecretAuditEvent,
    SecretBinding,
    SecretRecord,
    SecretVersion,
)
from control_plane.service_auth import (
    GitHubActionsIdentity,
    GitHubHumanIdentity,
    LaunchplaneAuthzPolicy,
    agent_authz_audit,
)
from control_plane.service_human_auth import LaunchplaneHumanSession
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.factory import build_shared_record_store
from control_plane.storage.postgres import Base, PostgresRecordStore
from tests.merge_train_policy_fixtures import build_test_merge_train_policy
from tests.merge_train_policy_fixtures import build_test_merge_train_policy_with_codex_skills

REPO_ROOT = Path(__file__).resolve().parents[1]


def _sqlite_database_url(database_path: Path) -> str:
    return f"sqlite+pysqlite:///{database_path}"


def _product_profile_record(
    *, product: str = "sellyouroutboard"
) -> LaunchplaneProductProfileRecord:
    return LaunchplaneProductProfileRecord(
        product=product,
        display_name="SellYourOutboard.com",
        repository="cbusillo/sellyouroutboard",
        driver_id="generic-web",
        image=ProductImageProfile(repository="ghcr.io/cbusillo/sellyouroutboard"),
        runtime_port=3000,
        health_path="/api/health",
        lanes=(
            ProductLaneProfile(
                instance="testing",
                context=product,
                base_url="https://sellyouroutboard-testing.shinycomputers.com",
                health_url="https://sellyouroutboard-testing.shinycomputers.com/api/health",
            ),
        ),
        preview=ProductPreviewProfile(
            enabled=True,
            context=f"{product}-testing",
            slug_template="pr-{number}",
        ),
        updated_at="2026-04-30T20:00:00Z",
        source="operator:test",
    )


def _alembic_config(database_url: str) -> AlembicConfig:
    config = AlembicConfig(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _deployment_record(*, record_id: str, started_at: str, finished_at: str) -> DeploymentRecord:
    return DeploymentRecord(
        record_id=record_id,
        artifact_identity=ArtifactIdentityReference(artifact_id="artifact-20260420-a1b2c3d4"),
        context="opw",
        instance="testing",
        source_git_ref="6b3c9d7e8f901234567890abcdef1234567890ab",
        resolved_target=ResolvedTargetEvidence(
            target_type="compose",
            target_id="compose-123",
            target_name="opw-testing",
        ),
        deploy=DeploymentEvidence(
            target_name="opw-testing",
            target_type="compose",
            deploy_mode="dokploy-compose-api",
            deployment_id="dokploy-1",
            status="pass",
            started_at=started_at,
            finished_at=finished_at,
        ),
    )


def _deployment_record_with_target_id(
    *, record_id: str, target_id: str, started_at: str, finished_at: str
) -> DeploymentRecord:
    return DeploymentRecord(
        record_id=record_id,
        artifact_identity=ArtifactIdentityReference(artifact_id="artifact-20260420-a1b2c3d4"),
        context="opw",
        instance="testing",
        source_git_ref="6b3c9d7e8f901234567890abcdef1234567890ab",
        resolved_target=ResolvedTargetEvidence(
            target_type="compose",
            target_id=target_id,
            target_name="opw-testing",
        ),
        deploy=DeploymentEvidence(
            target_name="opw-testing",
            target_type="compose",
            deploy_mode="dokploy-compose-api",
            deployment_id="dokploy-1",
            status="pass",
            started_at=started_at,
            finished_at=finished_at,
        ),
    )


def _generic_web_rollback_plan_record(
    *, plan_id: str, created_at: str
) -> GenericWebRollbackPlanRecord:
    return GenericWebRollbackPlanRecord(
        plan_id=plan_id,
        product="sellyouroutboard",
        context="sellyouroutboard-testing",
        instance="prod",
        status="ready",
        rollback_deployment_record_id="deployment-syo-prod-previous",
        artifact_identity=ArtifactIdentityReference(
            artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:abc123"
        ),
        planned_deploy=GenericWebRollbackDeployPlan(
            product="sellyouroutboard",
            instance="prod",
            artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
            source_git_ref="abc123",
        ),
        source_git_ref="abc123",
        backup_gate=BackupGateEvidence(required=False, status="skipped"),
        target_health=HealthcheckEvidence(status="pass"),
        created_at=created_at,
        summary="generic web rollback plan is ready",
    )


def _runner_host_hygiene_audit_record(
    *,
    audit_record_key: str,
    status: RunnerHostHygieneApplyAuditStatus = "planned",
    message: str = "planned runner host hygiene apply; no host mutation was executed",
) -> RunnerHostHygieneApplyAuditRecord:
    report = evaluate_runner_host_hygiene(
        policy=RunnerHostHygienePolicy(required_warm_builders=("odoo-docker-chris-testing",)),
        observation=RunnerHostHygieneObservation(
            host_name="chris-testing",
            observed_at="2026-05-23T13:00:00Z",
            free_disk_bytes=500,
            warm_builders=("odoo-docker-chris-testing",),
        ),
    )
    request = RunnerHostHygieneApplyRequest(
        action="prune_docker_cache",
        host_name="chris-testing",
        mutate=True,
        retained_warm_builders=("odoo-docker-chris-testing",),
        audit_record_key=audit_record_key,
    )
    plan = plan_runner_host_hygiene_apply(
        policy=RunnerHostHygieneApplyPolicy(
            approved_hosts=("chris-testing",),
            required_retained_warm_builders=("odoo-docker-chris-testing",),
            allow_docker_cache_prune=True,
        ),
        request=request,
        report=report,
    )
    return RunnerHostHygieneApplyAuditRecord(
        audit_record_key=audit_record_key,
        status=status,
        request=request,
        plan=plan,
        pre_apply_report=report,
        post_apply_report=report if status != "planned" else None,
        message=message,
    )


def _runner_lane_registration_audit_record(
    *,
    audit_record_key: str,
    status: RunnerLaneRegistrationAuditStatus = "planned",
    message: str = "planned runner lane registration; no host mutation was executed",
    repository: str = "cbusillo/odoo-tenant-cm-website",
    host_name: str = "chris-testing",
) -> RunnerLaneRegistrationAuditRecord:
    inventory = build_runner_lane_inventory(
        repository=repository,
        observed_at="2026-06-08T17:30:00Z",
        lanes=(),
    )
    request = RunnerLaneRegistrationRequest(
        repository=repository,
        host_name=host_name,
        lane_name="cm-website-runner-1",
        registration_root="/opt/actions-runners",
        labels=("self-hosted", "launchplane", "launchplane-managed"),
        mutate=True,
        audit_record_key=audit_record_key,
    )
    plan = plan_runner_lane_registration(
        policy=RunnerLaneRegistrationPolicy(
            allowed_repositories=("cbusillo/odoo-tenant-cm-website",),
            approved_hosts=("chris-testing",),
            allowed_registration_roots=("/opt/actions-runners",),
        ),
        request=request,
        inventory=inventory,
    )
    return RunnerLaneRegistrationAuditRecord(
        audit_record_key=audit_record_key,
        status=status,
        request=request,
        plan=plan,
        pre_inventory=inventory,
        post_inventory=inventory if status != "planned" else None,
        message=message,
    )


def _artifact_manifest() -> ArtifactIdentityManifest:
    return ArtifactIdentityManifest(
        artifact_id="artifact-20260420-a1b2c3d4",
        source_commit="a1b2c3d4",
        enterprise_base_digest="sha256:enterprisebase123",
        image=ArtifactImageReference(
            repository="ghcr.io/cbusillo/odoo-tenant-opw",
            digest="sha256:image123",
        ),
    )


def _promotion_record(*, record_id: str) -> PromotionRecord:
    return PromotionRecord(
        record_id=record_id,
        artifact_identity=ArtifactIdentityReference(artifact_id="artifact-20260420-a1b2c3d4"),
        deployment_record_id="deployment-20260420T153000Z-opw-testing",
        backup_record_id="backup-opw-prod-20260420T160000Z",
        context="opw",
        from_instance="testing",
        to_instance="prod",
        deploy=DeploymentEvidence(
            target_name="opw-prod",
            target_type="compose",
            deploy_mode="dokploy-compose-api",
            deployment_id="dokploy-2",
            status="pass",
            started_at="2026-04-20T16:05:00Z",
            finished_at="2026-04-20T16:07:00Z",
        ),
    )


def _inventory_record() -> EnvironmentInventory:
    return EnvironmentInventory(
        context="opw",
        instance="testing",
        artifact_identity=ArtifactIdentityReference(artifact_id="artifact-20260420-a1b2c3d4"),
        source_git_ref="6b3c9d7e8f901234567890abcdef1234567890ab",
        deploy=DeploymentEvidence(
            target_name="opw-testing",
            target_type="compose",
            deploy_mode="dokploy-compose-api",
            deployment_id="dokploy-1",
            status="pass",
            started_at="2026-04-20T15:30:00Z",
            finished_at="2026-04-20T15:32:00Z",
        ),
        updated_at="2026-04-20T15:33:00Z",
        deployment_record_id="deployment-20260420T153000Z-opw-testing",
    )


def _dokploy_target_id_record(
    *, context: str = "opw", instance: str = "prod", target_id: str = "compose-123"
) -> DokployTargetIdRecord:
    return DokployTargetIdRecord(
        context=context,
        instance=instance,
        target_id=target_id,
        updated_at="2026-04-21T18:30:00Z",
        source_label="import:test",
    )


def _dokploy_target_record(
    *, context: str = "opw", instance: str = "prod", project_name: str = ""
) -> DokployTargetRecord:
    return DokployTargetRecord(
        context=context,
        instance=instance,
        project_name=project_name,
        target_type="compose",
        target_name=f"{context}-{instance}",
        source_git_ref="origin/main",
        source_type="git",
        custom_git_url="git@github.com:every/odoo-opw.git",
        custom_git_branch=instance,
        compose_path="./docker-compose.yml",
        domains=(f"https://{instance}.example.com",),
        updated_at="2026-04-21T18:30:00Z",
        source_label="import:test",
    )


def _edge_endpoint_record(
    *,
    endpoint_key: str = "cm-prod-dokploy",
    upstream_host: str = "100.73.170.113",
    status: EdgeEndpointStatus = "active",
) -> EdgeEndpointRecord:
    return EdgeEndpointRecord(
        endpoint_key=endpoint_key,
        provider="dokploy",
        server_name="docker-cm-prod",
        upstream_host=upstream_host,
        upstream_host_kind="ip",
        upstream_scheme="https",
        upstream_port=443,
        status=status,
        updated_at="2026-06-07T00:00:00Z",
        source_label="test:edge-endpoint",
    )


def _private_health_endpoint_record(
    *,
    endpoint_key: str = "repairshopr-sync-prod-runtime",
    product: str = "repairshopr-sync",
    context: str = "repairshopr-sync",
    instance: str = "prod",
    status: Literal["active", "disabled"] = "active",
) -> PrivateHealthEndpointRecord:
    return PrivateHealthEndpointRecord(
        endpoint_key=endpoint_key,
        product=product,
        context=context,
        instance=instance,
        url="http://10.0.0.5:8000/health",
        status=status,
        updated_at="2026-06-15T00:00:00Z",
        source_label="test:private-health-endpoint",
    )


def _ingress_canary_route_record(
    *,
    canary_key: str = "ingress-canary",
    status: str = "active",
) -> IngressCanaryRouteRecord:
    return IngressCanaryRouteRecord(
        canary_key=canary_key,
        product="launchplane",
        context="reon-prod",
        domain_name="ingress-canary.example.test",
        expected_host_id=78,
        edge_endpoint_key="ingress-canary-edge",
        certificate_id=47,
        status=status,  # type: ignore[arg-type]
        updated_at="2026-06-11T00:00:00Z",
        source_label="test:ingress-canary",
    )


def _provider_target_record(
    *,
    context: str = "syo",
    instance: str = "prod",
    provider_id: str = "dokploy",
    target_category: DeployTargetCategory = "application",
    target_id: str = "app-syo-prod",
    provider_target_type: str = "application",
    updated_at: str = "2026-04-21T18:35:00Z",
) -> ProviderTargetRecord:
    return ProviderTargetRecord(
        context=context,
        instance=instance,
        provider_id=provider_id,
        target_category=target_category,
        target_id=target_id,
        display_name=f"{context}-{instance}",
        provider_target_type=provider_target_type,
        provider_evidence={"project_name": f"{context}-project"},
        updated_at=updated_at,
        source_label="test:provider-target",
    )


def _every_code_work_request(
    *,
    request_id: str = "every-code-cbusillo-code-123-test",
    state: str = "queued",
    updated_at: str = "2026-05-05T22:00:00Z",
) -> EveryCodeWorkRequestRecord:
    payload = {
        "request_id": request_id,
        "source": "manual",
        "state": state,
        "repository": "cbusillo/code",
        "issue_number": 123,
        "issue_url": "https://github.com/cbusillo/code/issues/123",
        "trigger_label": "every-code",
        "queued_at": "2026-05-05T22:00:00Z",
        "updated_at": updated_at,
    }
    if state in {"claimed", "running", "done", "blocked"}:
        payload["claimed_at"] = "2026-05-05T22:01:00Z"
        payload["claimed_by_host"] = "Chris-Studio"
    if state in {"running", "done"}:
        payload["started_at"] = "2026-05-05T22:02:00Z"
    if state in {"done", "blocked"}:
        payload["finished_at"] = "2026-05-05T22:03:00Z"
    if state == "blocked":
        payload["error_message"] = "checkout missing"
    return EveryCodeWorkRequestRecord.model_validate(payload)


def _runtime_environment_record(
    *,
    scope: RuntimeEnvironmentScope = "instance",
    context: str = "opw",
    instance: str = "local",
    env: dict[str, str | int | float | bool] | None = None,
) -> RuntimeEnvironmentRecord:
    return RuntimeEnvironmentRecord(
        scope=scope,
        context=context if scope != "global" else "",
        instance=instance if scope == "instance" else "",
        env=env or {"ODOO_DB_PASSWORD": "local-secret"},
        updated_at="2026-04-21T18:30:00Z",
        source_label="import:test",
    )


def _odoo_instance_override_record(
    *, context: str = "opw", instance: str = "prod"
) -> OdooInstanceOverrideRecord:
    return OdooInstanceOverrideRecord(
        context=context,
        instance=instance,
        config_parameters=(
            OdooConfigParameterOverride(
                key="web.base.url",
                value=OdooOverrideValue(
                    source="literal", value=f"https://{context}-{instance}.example.com"
                ),
            ),
        ),
        updated_at="2026-04-21T18:30:00Z",
        source_label="test",
    )


def _preview_record(*, preview_id: str, updated_at: str, pr_number: int) -> PreviewRecord:
    return PreviewRecord(
        preview_id=preview_id,
        context="verireel-testing",
        anchor_repo="verireel",
        anchor_pr_number=pr_number,
        anchor_pr_url=f"https://github.com/every/verireel/pull/{pr_number}",
        preview_label=f"verireel/pr-{pr_number}",
        canonical_url=f"https://pr-{pr_number}.ver-preview.shinycomputers.com",
        state="active",
        created_at="2026-04-20T10:00:00Z",
        updated_at=updated_at,
        eligible_at=updated_at,
    )


def _preview_generation_record(*, generation_id: str, preview_id: str) -> PreviewGenerationRecord:
    return PreviewGenerationRecord(
        generation_id=generation_id,
        preview_id=preview_id,
        sequence=1,
        state="ready",
        requested_reason="external_preview_refresh",
        requested_at="2026-04-20T10:01:00Z",
        ready_at="2026-04-20T10:05:00Z",
        finished_at="2026-04-20T10:05:00Z",
        resolved_manifest_fingerprint="preview-manifest-123",
        artifact_id="ghcr.io/every/verireel-app:pr-123",
        anchor_summary=PreviewPullRequestSummary(
            repo="verireel",
            pr_number=123,
            head_sha="6b3c9d7e8f901234567890abcdef1234567890ab",
            pr_url="https://github.com/every/verireel/pull/123",
        ),
        deploy_status="pass",
        verify_status="pass",
        overall_health_status="pass",
    )


def _preview_enablement_record(
    *, record_id: str, updated_at: str, pr_number: int
) -> PreviewEnablementRecord:
    return PreviewEnablementRecord(
        record_id=record_id,
        context="verireel-testing",
        anchor_repo="verireel",
        anchor_pr_number=pr_number,
        anchor_pr_url=f"https://github.com/every/verireel/pull/{pr_number}",
        anchor_head_sha="6b3c9d7e8f901234567890abcdef1234567890ab",
        action="labeled",
        pr_state="open",
        updated_at=updated_at,
        label_enabled=True,
        action_label="preview",
        request_metadata_status="valid",
        request_metadata_baseline_channel="testing",
    )


def _backup_gate_record() -> BackupGateRecord:
    return BackupGateRecord(
        record_id="backup-opw-prod-20260420T160000Z",
        context="opw",
        instance="prod",
        created_at="2026-04-20T16:00:00Z",
        source="prod-gate",
        status="pass",
        evidence={"snapshot": "opw-predeploy-20260420-160000"},
    )


def _release_tuple_record() -> ReleaseTupleRecord:
    return ReleaseTupleRecord(
        tuple_id="opw-testing-artifact-20260420-a1b2c3d4",
        context="opw",
        channel="testing",
        artifact_id="artifact-20260420-a1b2c3d4",
        repo_shas={"tenant-opw": "a1b2c3d4", "shared-addons": "abcdef1"},
        deployment_record_id="deployment-20260420T153000Z-opw-testing",
        provenance="ship",
        minted_at="2026-04-20T15:33:00Z",
    )


def _secret_record(*, secret_id: str, updated_at: str, current_version_id: str) -> SecretRecord:
    return SecretRecord(
        secret_id=secret_id,
        scope="context_instance",
        integration="dokploy",
        name="api_token",
        context="opw",
        instance="testing",
        description="Dokploy API token",
        current_version_id=current_version_id,
        created_at="2026-04-20T18:00:00Z",
        updated_at=updated_at,
        updated_by="launchplane-bootstrap",
    )


def _secret_version(*, version_id: str, secret_id: str, created_at: str) -> SecretVersion:
    return SecretVersion(
        version_id=version_id,
        secret_id=secret_id,
        created_at=created_at,
        created_by="launchplane-bootstrap",
        ciphertext="gAAAAABo-bootstrap-ciphertext",
    )


def _secret_binding(*, binding_id: str, secret_id: str, updated_at: str) -> SecretBinding:
    return SecretBinding(
        binding_id=binding_id,
        secret_id=secret_id,
        integration="dokploy",
        binding_key="DOKPLOY_TOKEN",
        context="opw",
        instance="testing",
        created_at="2026-04-20T18:00:00Z",
        updated_at=updated_at,
    )


def _secret_audit_event(*, event_id: str, secret_id: str, recorded_at: str) -> SecretAuditEvent:
    return SecretAuditEvent(
        event_id=event_id,
        secret_id=secret_id,
        event_type="imported",
        recorded_at=recorded_at,
        actor="launchplane-bootstrap",
        detail="Imported existing Dokploy secret",
        metadata={"source": "dokploy.env"},
    )


def _human_session(*, session_id: str = "session-1") -> LaunchplaneHumanSession:
    created_at = datetime.now(timezone.utc).replace(microsecond=0)
    return LaunchplaneHumanSession(
        session_id=session_id,
        created_at=created_at,
        expires_at=created_at + timedelta(hours=12),
        identity=GitHubHumanIdentity(
            login="alice",
            github_id=123,
            name="Alice Operator",
            email="alice@example.com",
            organizations=frozenset({"shinycomputers"}),
            teams=frozenset({"shinycomputers/launchplane-admins"}),
            role="admin",
        ),
    )


def _agent_write_intent_record(
    *, record_id: str = "", recorded_at: str = "2026-05-08T20:55:00Z"
) -> AgentWriteIntentRecord:
    identity = GitHubActionsIdentity(
        repository="every/verireel",
        repository_owner="every",
        workflow_ref="every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main",
        job_workflow_ref="",
        ref="refs/heads/main",
        ref_type="branch",
        event_name="pull_request",
        environment="",
        subject="repo:every/verireel:ref:refs/heads/main",
        sha="abc123",
        raw_claims={},
    )
    request = AgentWriteIntentRequest(
        intent="every_code_rerun",
        mode="dry_run",
        product="launchplane",
        context="launchplane",
        source_url="https://github.com/cbusillo/launchplane/issues/386",
        reason="Check whether rerun can be requested safely.",
    )
    audit = agent_authz_audit(
        identity=identity,
        action="every_code_work_request.write",
        product="launchplane",
        context="launchplane",
        decision="allowed",
        reason_code="authorized",
        policy_source="test",
        policy_sha256="abc123",
    )
    evaluation = AgentWriteIntentEvaluation(
        intent="every_code_rerun",
        mode="dry_run",
        status="allowed",
        authz_action="every_code_work_request.write",
        product="launchplane",
        context="launchplane",
        source_url="https://github.com/cbusillo/launchplane/issues/386",
        safe_to_execute=False,
        next_action="Review the dry-run result before requesting apply authority.",
        reason_code="authorized",
        audit=audit,
    )
    resolved_record_id = record_id or build_agent_write_intent_record_id(
        recorded_at=recorded_at,
        trace_id="launchplane_req_test_write_intent",
        request=request,
        evaluation=evaluation,
    )
    return AgentWriteIntentRecord(
        record_id=resolved_record_id,
        recorded_at=recorded_at,
        trace_id="launchplane_req_test_write_intent",
        idempotency_key="intent-eval-1",
        request=request,
        evaluation=evaluation,
    )


def _merge_train_run_record(*, recorded_at: str = "2026-05-09T02:05:00Z") -> MergeTrainRunRecord:
    policy = build_test_merge_train_policy()
    snapshot = MergeTrainDryRunSnapshot(
        repository="cbusillo/sellyouroutboard",
        base_branch="main",
        pull_requests=(
            MergeTrainPullRequestSnapshot(
                number=42,
                url="https://github.com/cbusillo/sellyouroutboard/pull/42",
                title="Ready merge train PR",
                created_at="2026-05-09T01:00:00Z",
                labels=("ready-to-merge",),
                actor_role="repo_admin",
                head_sha="head-42",
                base_ref="main",
                base_sha="base-main",
                mergeable="mergeable",
                required_checks_status="pass",
            ),
        ),
    )
    dry_run_result = build_merge_train_dry_run_result(policy=policy, snapshot=snapshot)
    return build_merge_train_run_record(
        recorded_at=recorded_at,
        trace_id="launchplane_req_merge_train_test",
        policy_sha256=policy.policy_sha256,
        snapshot=snapshot,
        dry_run_result=dry_run_result,
    )


def _merge_train_batch_candidate_record(
    *,
    record_id: str = "merge-train-batch-candidate-20260514T010000Z-active",
    status: MergeTrainBatchRecordStatus = "active",
    updated_at: str = "2026-05-14T01:00:00Z",
) -> MergeTrainBatchCandidateRecord:
    repository = "example/merge-train-repo"
    base_branch = "main"
    base_sha = "base-main"
    entries = (
        MergeTrainBatchEntry(
            pull_request_number=10,
            position=1,
            head_sha="head-10",
            title="First queued change",
            url="https://github.com/example/merge-train-repo/pull/10",
        ),
        MergeTrainBatchEntry(
            pull_request_number=11,
            position=2,
            head_sha="head-11",
            title="Second queued change",
            url="https://github.com/example/merge-train-repo/pull/11",
        ),
    )
    batch_id = build_merge_train_batch_id(
        repository=repository,
        base_branch=base_branch,
        base_sha=base_sha,
        entry_head_shas=tuple(entry.head_sha for entry in entries),
    )
    return MergeTrainBatchCandidateRecord(
        record_id=record_id,
        status=status,
        source="test",
        updated_at=updated_at,
        candidate=MergeTrainBatchCandidate(
            batch_id=batch_id,
            repository=repository,
            base_branch=base_branch,
            base_sha=base_sha,
            policy_key=f"{repository}:{base_branch}",
            policy_sha256="policy-digest",
            candidate_ref=build_merge_train_batch_candidate_ref(
                repository=repository,
                base_branch=base_branch,
                batch_id=batch_id,
            ),
            candidate_sha="candidate-sha",
            status="passed",
            entries=entries,
            required_checks_status="pass",
            created_at="2026-05-14T00:59:00Z",
            updated_at=updated_at,
        ),
    )


def _merge_train_batch_landing_plan_record(
    *,
    record_id: str = "merge-train-batch-landing-plan-20260514T010500Z-active",
    status: MergeTrainBatchRecordStatus = "active",
    updated_at: str = "2026-05-14T01:05:00Z",
) -> MergeTrainBatchLandingPlanRecord:
    landing_plan = build_merge_train_batch_landing_plan(
        candidate=_merge_train_batch_candidate_record(updated_at=updated_at).candidate,
        merge_method="squash",
        created_at=updated_at,
    )
    return MergeTrainBatchLandingPlanRecord(
        record_id=record_id,
        status=status,
        source="test",
        updated_at=updated_at,
        landing_plan=landing_plan,
    )


def _merge_train_stack_collapse_plan_record(
    *,
    record_id: str = "merge-train-stack-collapse-plan-20260514T013000Z-active",
    status: MergeTrainStackCollapseRecordStatus = "active",
    updated_at: str = "2026-05-14T01:30:00Z",
) -> MergeTrainStackCollapsePlanRecord:
    repository = "example/merge-train-repo"
    base_branch = "main"
    entries = (
        MergeTrainStackCollapseEntry(
            pull_request_number=10,
            position=1,
            head_sha="head-10",
            head_ref="feature/root",
            base_sha="base-10",
            base_ref="main",
        ),
        MergeTrainStackCollapseEntry(
            pull_request_number=11,
            position=2,
            head_sha="head-11",
            head_ref="feature/child",
            base_sha="base-11",
            base_ref="feature/root",
        ),
    )
    collapse_id = build_merge_train_stack_collapse_id(
        repository=repository,
        base_branch=base_branch,
        root_pull_request_number=10,
        entry_head_shas=tuple(entry.head_sha for entry in entries),
    )
    return MergeTrainStackCollapsePlanRecord(
        record_id=record_id,
        status=status,
        source="test",
        updated_at=updated_at,
        plan=MergeTrainStackCollapsePlan(
            collapse_id=collapse_id,
            repository=repository,
            base_branch=base_branch,
            root_pull_request_number=10,
            root_initial_head_sha="head-10",
            root_head_ref="feature/root",
            policy_key=f"{repository}:{base_branch}",
            policy_sha256="policy-digest",
            entries=entries,
            mutations=(
                MergeTrainStackCollapseMutation(
                    child_pull_request_number=11,
                    parent_pull_request_number=10,
                    child_head_sha="head-11",
                    expected_parent_head_sha="head-10",
                    parent_head_ref="feature/root",
                ),
            ),
            created_at="2026-05-14T01:29:00Z",
            updated_at=updated_at,
        ),
    )


class PostgresRecordStoreTests(unittest.TestCase):
    def test_postgres_metadata_index_names_fit_identifier_limit(self) -> None:
        index_names = tuple(
            index.name
            for table in Base.metadata.tables.values()
            for index in table.indexes
            if isinstance(index, Index) and index.name is not None
        )

        too_long_index_names = tuple(
            index_name for index_name in index_names if len(index_name) > 63
        )

        self.assertEqual(too_long_index_names, ())

    def test_alembic_baseline_creates_schema_used_by_record_store(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            alembic_command.upgrade(_alembic_config(database_url), "head")

            store = PostgresRecordStore(database_url=database_url)
            manifest = _artifact_manifest()
            ingress_route_audit = IngressRouteAuditRecord(
                record_id="ingress-route-audit-alembic",
                product="launchplane",
                context="reon-prod",
                mode="apply",
                status="unchanged",
                dry_run=False,
                requested_domains=("ingress-canary.example.test",),
                expected_host_id=78,
                provider_host_id=78,
                operations=(
                    IngressRouteAuditOperation(
                        action="no-op",
                        host_id=78,
                        domain_names=("ingress-canary.example.test",),
                        requires_apply=False,
                    ),
                ),
                trace_id="launchplane_req_alembic",
                idempotency_key="ingress-canary-apply",
                reason="Apply unchanged canary route.",
                recorded_at="2026-05-31T12:05:00Z",
            )
            store.write_artifact_manifest(manifest)
            store.write_ingress_route_audit_record(ingress_route_audit)
            provider_target = _provider_target_record(context="reon", instance="prod")
            store.write_provider_target_record(provider_target)
            edge_endpoint = _edge_endpoint_record()
            store.write_edge_endpoint_record(edge_endpoint)
            private_health_endpoint = _private_health_endpoint_record()
            store.write_private_health_endpoint_record(private_health_endpoint)
            ingress_canary_route = _ingress_canary_route_record()
            store.write_ingress_canary_route_record(ingress_canary_route)
            inspect_engine = create_engine(database_url)
            inspector = inspect(inspect_engine)
            table_names = set(inspector.get_table_names())
            edge_endpoint_columns = {
                column["name"] for column in inspector.get_columns("launchplane_edge_endpoints")
            }
            ingress_canary_route_columns = {
                column["name"]
                for column in inspector.get_columns("launchplane_ingress_canary_routes")
            }
            private_health_endpoint_columns = {
                column["name"]
                for column in inspector.get_columns("launchplane_private_health_endpoints")
            }
            provider_target_columns = {
                column["name"] for column in inspector.get_columns("launchplane_provider_targets")
            }
            provider_target_indexes = {
                index["name"] for index in inspector.get_indexes("launchplane_provider_targets")
            }
            loaded = store.read_artifact_manifest(manifest.artifact_id)
            loaded_provider_target = store.read_provider_target_record(
                context_name="reon",
                instance_name="prod",
            )
            loaded_edge_endpoint = store.read_edge_endpoint_record(edge_endpoint.endpoint_key)
            loaded_private_health_endpoint = store.read_private_health_endpoint_record(
                private_health_endpoint.endpoint_key
            )
            loaded_ingress_canary_route = store.read_ingress_canary_route_record(
                ingress_canary_route.canary_key
            )
            audit_records = store.list_ingress_route_audit_records(
                product="launchplane", context_name="reon-prod"
            )
            store.close()
            inspect_engine.dispose()

        self.assertEqual(loaded.artifact_id, manifest.artifact_id)
        self.assertEqual(loaded.image.digest, "sha256:image123")
        self.assertEqual(loaded_provider_target.target_id, provider_target.target_id)
        self.assertEqual(loaded_edge_endpoint.upstream_host, edge_endpoint.upstream_host)
        self.assertEqual(
            loaded_private_health_endpoint.url,
            "http://10.0.0.5:8000/health",
        )
        self.assertEqual(loaded_ingress_canary_route.domain_name, "ingress-canary.example.test")
        self.assertIn("launchplane_edge_endpoints", table_names)
        self.assertIn("launchplane_private_health_endpoints", table_names)
        self.assertIn("launchplane_ingress_canary_routes", table_names)
        self.assertGreaterEqual(
            edge_endpoint_columns,
            {
                "endpoint_key",
                "provider",
                "server_name",
                "upstream_host",
                "upstream_scheme",
                "upstream_port",
                "status",
                "updated_at",
                "payload",
            },
        )
        self.assertGreaterEqual(
            private_health_endpoint_columns,
            {
                "endpoint_key",
                "product",
                "context",
                "instance",
                "url",
                "status",
                "updated_at",
                "payload",
            },
        )
        self.assertGreaterEqual(
            ingress_canary_route_columns,
            {
                "canary_key",
                "product",
                "context",
                "domain_name",
                "expected_host_id",
                "edge_endpoint_key",
                "certificate_id",
                "status",
                "updated_at",
                "payload",
            },
        )
        self.assertIn("launchplane_provider_targets", table_names)
        self.assertGreaterEqual(
            provider_target_columns,
            {
                "context",
                "instance",
                "provider_id",
                "target_category",
                "target_id",
                "display_name",
                "provider_target_type",
                "updated_at",
                "payload",
            },
        )
        self.assertIn("launchplane_provider_targets_provider_idx", provider_target_indexes)
        self.assertIn("launchplane_provider_targets_updated_idx", provider_target_indexes)
        self.assertEqual(
            [record.record_id for record in audit_records], [ingress_route_audit.record_id]
        )

    def test_verify_schema_rejects_empty_database_without_creating_tables(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)

            with self.assertRaisesRegex(RuntimeError, "missing required table"):
                store.verify_schema()

            self.assertEqual(inspect(store._engine).get_table_names(), [])
            store.close()

    def test_verify_schema_rejects_missing_columns_without_creating_them(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            alembic_command.upgrade(_alembic_config(database_url), "head")
            engine = create_engine(database_url)
            with engine.begin() as connection:
                connection.execute(
                    text("ALTER TABLE launchplane_artifact_manifests DROP COLUMN payload")
                )
            engine.dispose()
            store = PostgresRecordStore(database_url=database_url)

            with self.assertRaisesRegex(
                RuntimeError,
                "missing required column.*launchplane_artifact_manifests.payload",
            ):
                store.verify_schema()

            artifact_columns = {
                column["name"]
                for column in inspect(store._engine).get_columns("launchplane_artifact_manifests")
            }
            self.assertNotIn("payload", artifact_columns)
            store.close()

    def test_shared_record_store_verifies_existing_schema_without_creating_it(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )

            with self.assertRaisesRegex(RuntimeError, "Run Alembic migrations"):
                build_shared_record_store(database_url=database_url)

            alembic_command.upgrade(_alembic_config(database_url), "head")
            store = build_shared_record_store(database_url=database_url)
            manifest = _artifact_manifest()
            store.write_artifact_manifest(manifest)
            loaded = store.read_artifact_manifest(manifest.artifact_id)
            store.close()

        self.assertEqual(loaded.artifact_id, manifest.artifact_id)

    def test_alembic_head_repairs_stamped_schema_missing_odoo_replacement_scope(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            config = _alembic_config(database_url)
            alembic_command.upgrade(config, "head")
            engine = create_engine(database_url)
            with engine.begin() as connection:
                connection.execute(
                    text("DROP INDEX launchplane_odoo_replacement_operation_idempotency_idx")
                )
                connection.execute(
                    text(
                        "ALTER TABLE "
                        "launchplane_odoo_stable_target_replacement_operations "
                        "DROP COLUMN idempotency_scope"
                    )
                )
                connection.execute(text("DELETE FROM alembic_version"))
                connection.execute(
                    text("INSERT INTO alembic_version (version_num) VALUES (:version_num)"),
                    {"version_num": "b1c3d5e7f9a1"},
                )
            engine.dispose()

            alembic_command.upgrade(config, "head")
            store = PostgresRecordStore(database_url=database_url)
            store.verify_schema()

            repaired_columns = {
                column["name"]
                for column in inspect(store._engine).get_columns(
                    "launchplane_odoo_stable_target_replacement_operations"
                )
            }
            store.close()

        self.assertIn("idempotency_scope", repaired_columns)

    def test_runner_host_hygiene_audit_downgrade_tolerates_missing_table(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            config = _alembic_config(database_url)
            alembic_command.upgrade(config, "c2d4e6f8a0b2")
            alembic_command.stamp(config, "c3e5f7a9b1d2")

            alembic_command.downgrade(config, "c2d4e6f8a0b2")

            engine = create_engine(database_url)
            table_names = set(inspect(engine).get_table_names())
            engine.dispose()

        self.assertNotIn("launchplane_runner_host_hygiene_audits", table_names)

    def test_alembic_baseline_downgrades_to_empty_schema(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            database_url = _sqlite_database_url(database_path)
            config = _alembic_config(database_url)

            alembic_command.upgrade(config, "head")
            alembic_command.downgrade(config, "base")

            store = PostgresRecordStore(database_url=database_url)
            with self.assertRaises(SQLAlchemyError):
                store.list_artifact_manifests()
            store.close()

    def test_artifact_manifests_round_trip(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=_sqlite_database_url(database_path))
            store.ensure_schema()

            manifest = _artifact_manifest()
            store.write_artifact_manifest(manifest)
            loaded = store.read_artifact_manifest(manifest.artifact_id)
            listed = store.list_artifact_manifests()

            self.assertEqual(loaded.artifact_id, manifest.artifact_id)
            self.assertEqual(loaded.image.repository, "ghcr.io/cbusillo/odoo-tenant-opw")
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0].image.digest, "sha256:image123")

    def test_artifacts_cli_uses_database_store_when_configured(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            input_path = Path(temporary_directory_name) / "artifact.json"
            input_path.write_text(
                _artifact_manifest().model_dump_json(),
                encoding="utf-8",
            )
            runner = CliRunner()

            write_result = runner.invoke(
                main,
                [
                    "artifacts",
                    "write",
                    "--database-url",
                    database_url,
                    "--input-file",
                    str(input_path),
                ],
            )
            self.assertEqual(write_result.exit_code, 0, write_result.output)

            show_result = runner.invoke(
                main,
                [
                    "artifacts",
                    "show",
                    "--database-url",
                    database_url,
                    "--artifact-id",
                    "artifact-20260420-a1b2c3d4",
                ],
            )
            self.assertEqual(show_result.exit_code, 0, show_result.output)
            self.assertIn("ghcr.io/cbusillo/odoo-tenant-opw", show_result.output)

    def test_dokploy_target_records_round_trip(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=_sqlite_database_url(database_path))
            store.ensure_schema()

            record = _dokploy_target_record()
            store.write_dokploy_target_record(record)
            loaded = store.read_dokploy_target_record(context_name="opw", instance_name="prod")
            listed = store.list_dokploy_target_records()

            self.assertEqual(loaded.target_name, "opw-prod")
            self.assertEqual(loaded.compose_path, "./docker-compose.yml")
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0].context, "opw")

    def test_provider_target_records_require_physical_authority(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=_sqlite_database_url(database_path))
            store.ensure_schema()

            store.write_dokploy_target_record(
                _dokploy_target_record(
                    context="syo",
                    instance="prod",
                    project_name="syo-prod-project",
                )
            )
            store.write_dokploy_target_id_record(
                _dokploy_target_id_record(
                    context="syo",
                    instance="prod",
                    target_id="app-syo-prod",
                )
            )

            with self.assertRaises(FileNotFoundError):
                store.read_provider_target_record(context_name="syo", instance_name="prod")
            listed = store.list_provider_target_records()
            filtered = store.list_provider_target_records(provider_id=" DOKPLOY ")
            store.close()

        self.assertEqual(listed, ())
        self.assertEqual(filtered, ())

    def test_provider_target_records_round_trip_from_physical_storage(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=_sqlite_database_url(database_path))
            store.ensure_schema()

            record = _provider_target_record(
                context="verireel",
                instance="prod",
                provider_id=" Dokploy ",
                target_id="app-verireel-prod",
            )
            store.write_provider_target_record(record)

            loaded = store.read_provider_target_record(
                context_name="verireel",
                instance_name="prod",
            )
            listed = store.list_provider_target_records()
            filtered = store.list_provider_target_records(provider_id=" DOKPLOY ")
            store.close()

        self.assertEqual(loaded, record)
        self.assertEqual(listed, (record,))
        self.assertEqual(filtered, (record,))

    def test_create_provider_target_record_if_absent_refuses_existing_route(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=_sqlite_database_url(database_path))
            store.ensure_schema()

            record = _provider_target_record(context="verireel", instance="prod")
            changed_record = _provider_target_record(
                context="verireel",
                instance="prod",
                target_id="app-verireel-prod-new",
            )
            first_status = store.create_provider_target_record_if_absent(record)
            second_status = store.create_provider_target_record_if_absent(changed_record)
            loaded = store.read_provider_target_record(
                context_name="verireel", instance_name="prod"
            )
            store.close()

        self.assertEqual(first_status, "created")
        self.assertEqual(second_status, "exists")
        self.assertEqual(loaded, record)

    def test_delete_provider_target_record_uses_current_authority_match(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=_sqlite_database_url(database_path))
            store.ensure_schema()

            record = _provider_target_record(context="verireel", instance="prod")
            changed_record = _provider_target_record(
                context="verireel",
                instance="prod",
                target_id="app-verireel-prod-new",
            )
            store.write_provider_target_record(record)

            changed_status = store.delete_provider_target_record(expected_record=changed_record)
            loaded_after_changed = store.read_provider_target_record(
                context_name="verireel",
                instance_name="prod",
            )
            deleted_status = store.delete_provider_target_record(expected_record=record)
            missing_status = store.delete_provider_target_record(expected_record=record)
            store.close()

        self.assertEqual(changed_status, "changed")
        self.assertEqual(loaded_after_changed, record)
        self.assertEqual(deleted_status, "deleted")
        self.assertEqual(missing_status, "missing")

    def test_provider_target_physical_storage_precedes_dokploy_projection(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=_sqlite_database_url(database_path))
            store.ensure_schema()

            physical_record = _provider_target_record(
                context="syo",
                instance="prod",
                target_id="physical-app-syo-prod",
            )
            store.write_provider_target_record(physical_record)
            store.write_dokploy_target_record(
                _dokploy_target_record(context="syo", instance="prod")
            )
            store.write_dokploy_target_id_record(
                _dokploy_target_id_record(
                    context="syo",
                    instance="prod",
                    target_id="projected-app-syo-prod",
                )
            )

            loaded = store.read_provider_target_record(context_name="syo", instance_name="prod")
            listed = store.list_provider_target_records()
            summary = store.read_lane_summary(context_name="syo", instance_name="prod")
            store.close()

        self.assertEqual(loaded, physical_record)
        self.assertEqual(listed, (physical_record,))
        self.assertEqual(summary.provider_target, physical_record)

    def test_provider_target_filter_suppresses_shadowed_dokploy_projection(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=_sqlite_database_url(database_path))
            store.ensure_schema()

            physical_record = _provider_target_record(
                context="syo",
                instance="prod",
                provider_id="future-provider",
                target_id="physical-syo-prod",
            )
            store.write_provider_target_record(physical_record)
            store.write_dokploy_target_record(
                _dokploy_target_record(context="syo", instance="prod")
            )
            store.write_dokploy_target_id_record(
                _dokploy_target_id_record(
                    context="syo",
                    instance="prod",
                    target_id="legacy-dokploy-syo-prod",
                )
            )

            dokploy_records = store.list_provider_target_records(provider_id="dokploy")
            future_provider_records = store.list_provider_target_records(
                provider_id="future-provider"
            )
            store.close()

        self.assertEqual(dokploy_records, ())
        self.assertEqual(future_provider_records, (physical_record,))

    def test_read_lane_summary_does_not_project_provider_target_from_dokploy_pair(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=_sqlite_database_url(database_path))
            store.ensure_schema()

            store.write_dokploy_target_record(
                _dokploy_target_record(context="syo", instance="prod")
            )
            store.write_dokploy_target_id_record(
                _dokploy_target_id_record(
                    context="syo",
                    instance="prod",
                    target_id="legacy-dokploy-syo-prod",
                )
            )

            summary = store.read_lane_summary(context_name="syo", instance_name="prod")
            store.close()

        self.assertIsNone(summary.provider_target)
        self.assertIsNotNone(summary.dokploy_target)
        self.assertIsNotNone(summary.dokploy_target_id)

    def test_provider_target_list_returns_only_physical_records(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=_sqlite_database_url(database_path))
            store.ensure_schema()

            physical_record = _provider_target_record(
                context="verireel",
                instance="prod",
                target_id="app-verireel-prod",
            )
            store.write_provider_target_record(physical_record)
            store.write_dokploy_target_record(
                _dokploy_target_record(context="syo", instance="prod")
            )
            store.write_dokploy_target_id_record(
                _dokploy_target_id_record(
                    context="syo",
                    instance="prod",
                    target_id="app-syo-prod",
                )
            )
            store.write_dokploy_target_record(
                _dokploy_target_record(context="incomplete", instance="prod")
            )

            listed = store.list_provider_target_records()
            filtered = store.list_provider_target_records(provider_id="dokploy")
            store.close()

        self.assertEqual(
            [(record.context, record.instance, record.target_id) for record in listed],
            [
                ("verireel", "prod", "app-verireel-prod"),
            ],
        )
        self.assertEqual(filtered, listed)

    def test_provider_target_list_skips_incomplete_dokploy_pairs(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=_sqlite_database_url(database_path))
            store.ensure_schema()

            store.write_dokploy_target_record(
                _dokploy_target_record(context="syo", instance="prod")
            )

            self.assertEqual(store.list_provider_target_records(), ())
            with self.assertRaises(FileNotFoundError):
                store.read_provider_target_record(context_name="syo", instance_name="prod")
            store.close()

    def test_read_lane_summary_keeps_deployment_target_evidence_separate(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=_sqlite_database_url(database_path))
            store.ensure_schema()
            store.write_deployment_record(
                _deployment_record_with_target_id(
                    record_id="deployment-20260420T153000Z-opw-testing",
                    target_id="old-compose-id",
                    started_at="2026-04-20T15:30:00Z",
                    finished_at="2026-04-20T15:32:00Z",
                )
            )
            store.write_provider_target_record(
                _provider_target_record(
                    context="opw",
                    instance="testing",
                    target_id="current-compose-id",
                )
            )

            summary = store.read_lane_summary(context_name="opw", instance_name="testing")
            store.close()

        provider_target = summary.provider_target
        assert provider_target is not None
        self.assertEqual(provider_target.target_id, "current-compose-id")
        deployed_target = summary.deployed_target
        assert deployed_target is not None
        self.assertEqual(deployed_target.target_id, "old-compose-id")

    def test_idempotency_records_round_trip(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=_sqlite_database_url(database_path))
            store.ensure_schema()

            record = LaunchplaneIdempotencyRecord(
                record_id=build_launchplane_idempotency_record_id(
                    response_trace_id="launchplane_req_123",
                ),
                scope="every/verireel|workflow|repo:every/verireel:pull_request",
                route_path="/v1/evidence/previews/generations",
                idempotency_key="preview-generation:verireel:verireel-testing:verireel:35:abcdef",
                request_fingerprint="fingerprint-123",
                response_status_code=202,
                response_trace_id="launchplane_req_123",
                recorded_at="2026-04-21T01:00:00Z",
                response_payload={"status": "accepted", "records": {"preview_id": "preview-35"}},
            )

            store.write_idempotency_record(record)
            loaded = store.read_idempotency_record(
                scope=record.scope,
                route_path=record.route_path,
                idempotency_key=record.idempotency_key,
            )

            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded.request_fingerprint, "fingerprint-123")
            self.assertEqual(loaded.response_payload["records"]["preview_id"], "preview-35")

    def test_every_code_work_requests_round_trip_list_and_claim_once(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=_sqlite_database_url(database_path))
            store.ensure_schema()

            older_record = _every_code_work_request(
                request_id="every-code-cbusillo-code-122-test",
                updated_at="2026-05-05T21:00:00Z",
            )
            newer_record = _every_code_work_request()
            store.write_every_code_work_request_record(older_record)
            store.write_every_code_work_request_record(newer_record)
            created_duplicate, duplicate_created = (
                store.create_every_code_work_request_record_if_absent(
                    newer_record.model_copy(update={"updated_at": "2026-05-05T23:00:00Z"})
                )
            )

            listed = store.list_every_code_work_request_records(state="queued")
            offset_listed = store.list_every_code_work_request_records(
                state="queued",
                limit=1,
                offset=1,
            )
            claimed = store.claim_every_code_work_request_record(
                request_id=newer_record.request_id,
                host="Chris-Studio",
                claimed_at="2026-05-05T22:01:00Z",
            )
            second_claim = store.claim_every_code_work_request_record(
                request_id=newer_record.request_id,
                host="Other-Host",
                claimed_at="2026-05-05T22:02:00Z",
            )
            loaded = store.read_every_code_work_request_record(newer_record.request_id)

        self.assertEqual(
            [record.request_id for record in listed],
            [newer_record.request_id, older_record.request_id],
        )
        self.assertEqual([record.request_id for record in offset_listed], [older_record.request_id])
        self.assertFalse(duplicate_created)
        self.assertEqual(created_duplicate.updated_at, newer_record.updated_at)
        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertEqual(claimed.state, "claimed")
        self.assertEqual(claimed.claimed_by_host, "Chris-Studio")
        self.assertIsNone(second_claim)
        self.assertEqual(loaded.state, "claimed")

    def test_every_code_pr_feedback_round_trip_and_filter(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=_sqlite_database_url(database_path))
            store.ensure_schema()
            older_record = EveryCodePrFeedbackRecord(
                feedback_id="every-code-pr-feedback-cbusillo-code-26-old",
                request_id="every-code-cbusillo-code-123-test",
                repository="cbusillo/code",
                pr_number=26,
                pr_url="https://github.com/cbusillo/code/pull/26",
                feedback_kind="issue_comment",
                github_delivery_id="delivery-old",
                github_id="100",
                actor="cbusillo",
                body="Please adjust the README wording.",
                received_at="2026-05-06T17:00:00Z",
            )
            newer_record = older_record.model_copy(
                update={
                    "feedback_id": "every-code-pr-feedback-cbusillo-code-26-new",
                    "github_delivery_id": "delivery-new",
                    "github_id": "101",
                    "received_at": "2026-05-06T18:00:00Z",
                }
            )
            store.write_every_code_pr_feedback_record(older_record)
            store.write_every_code_pr_feedback_record(newer_record)

            listed = store.list_every_code_pr_feedback_records(
                repository="cbusillo/code",
                pr_number=26,
                status="pending",
            )
            offset_listed = store.list_every_code_pr_feedback_records(
                repository="cbusillo/code",
                pr_number=26,
                status="pending",
                limit=1,
                offset=1,
            )

        self.assertEqual(
            [record.feedback_id for record in listed],
            [newer_record.feedback_id, older_record.feedback_id],
        )
        self.assertEqual(
            [record.feedback_id for record in offset_listed], [older_record.feedback_id]
        )

    def test_human_sessions_round_trip_and_delete(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=_sqlite_database_url(database_path))
            store.ensure_schema()

            session = _human_session()
            store.write_session(session)
            loaded = store.read_session(session.session_id)
            store.delete_session(session.session_id)
            deleted = store.read_session(session.session_id)

        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.identity.login, "alice")
        self.assertEqual(loaded.identity.role, "admin")
        self.assertEqual(loaded.identity.teams, frozenset({"shinycomputers/launchplane-admins"}))
        self.assertIsNone(deleted)

    def test_expired_human_session_reads_as_missing(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=_sqlite_database_url(database_path))
            store.ensure_schema()
            created_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
            expired_session = LaunchplaneHumanSession(
                session_id="expired-session",
                created_at=created_at,
                expires_at=created_at + timedelta(minutes=1),
                identity=_human_session().identity,
            )

            store.write_session(expired_session)
            loaded = store.read_session(expired_session.session_id)

        self.assertIsNone(loaded)

    def test_storage_import_core_records_command_reports_counts(self) -> None:
        runner = CliRunner()
        postgres_store = Mock()
        postgres_store.import_core_records_from_filesystem.return_value = {
            "backup_gates": 0,
            "deployments": 1,
            "promotions": 0,
            "inventory": 1,
            "preview_records": 0,
            "preview_generations": 0,
            "release_tuples": 0,
        }

        with TemporaryDirectory() as temporary_directory_name:
            with patch(
                "control_plane.cli_storage_secrets.PostgresRecordStore",
                return_value=postgres_store,
            ) as store_class:
                result = runner.invoke(
                    main,
                    [
                        "storage",
                        "import-core-records",
                        "--state-dir",
                        temporary_directory_name,
                        "--database-url",
                        "postgresql://launchplane:test@db/launchplane",
                    ],
                )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        store_class.assert_called_once_with(
            database_url="postgresql://launchplane:test@db/launchplane"
        )
        postgres_store.ensure_schema.assert_called_once_with()
        postgres_store.import_core_records_from_filesystem.assert_called_once()
        self.assertIn('"deployments": 1', result.output)

    def test_write_and_read_deployment_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )

            store.ensure_schema()
            store.write_deployment_record(
                _deployment_record(
                    record_id="deployment-20260420T153000Z-opw-testing",
                    started_at="2026-04-20T15:30:00Z",
                    finished_at="2026-04-20T15:32:00Z",
                )
            )
            loaded_record = store.read_deployment_record("deployment-20260420T153000Z-opw-testing")
            store.close()

        self.assertEqual(store.backend_name, "postgres")
        self.assertEqual(loaded_record.context, "opw")
        resolved_target = loaded_record.resolved_target
        assert resolved_target is not None
        self.assertEqual(resolved_target.target_id, "compose-123")
        deployed_target = loaded_record.deployed_target
        assert deployed_target is not None
        self.assertEqual(deployed_target.provider_id, "dokploy")
        self.assertEqual(deployed_target.target_category, "compose")

    def test_write_and_read_provider_neutral_deployment_target(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            record = DeploymentRecord(
                record_id="deployment-20260420T153000Z-syo-prod",
                artifact_identity=ArtifactIdentityReference(
                    artifact_id="artifact-20260420-a1b2c3d4"
                ),
                context="syo",
                instance="prod",
                source_git_ref="6b3c9d7e8f901234567890abcdef1234567890ab",
                deployed_target=DeployedTargetReference(
                    provider_id="fake-cloud",
                    target_category="service",
                    target_id="svc-123",
                    display_name="syo-prod-service",
                    provider_target_type="managed-service",
                ),
                deploy=DeploymentEvidence(
                    target_name="syo-prod-service",
                    target_type="application",
                    deploy_mode="fake-cloud-service-api",
                    provider_id="fake-cloud",
                    target_category="service",
                    provider_target_type="managed-service",
                    deployment_id="deploy-123",
                    status="pass",
                    started_at="2026-04-20T15:30:00Z",
                    finished_at="2026-04-20T15:32:00Z",
                ),
            )

            store.write_deployment_record(record)
            loaded_record = store.read_deployment_record(record.record_id)
            store.close()

        self.assertIsNone(loaded_record.resolved_target)
        deployed_target = loaded_record.deployed_target
        assert deployed_target is not None
        self.assertEqual(deployed_target.provider_id, "fake-cloud")
        self.assertEqual(deployed_target.target_category, "service")
        self.assertEqual(deployed_target.target_id, "svc-123")
        self.assertEqual(loaded_record.deploy.provider_id, "fake-cloud")
        self.assertEqual(loaded_record.deploy.target_category, "service")
        self.assertEqual(loaded_record.deploy.provider_target_type, "managed-service")

    def test_write_and_read_resolved_target_uses_deploy_provider_id(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            record = DeploymentRecord(
                record_id="deployment-20260420T153000Z-syo-prod",
                artifact_identity=ArtifactIdentityReference(
                    artifact_id="artifact-20260420-a1b2c3d4"
                ),
                context="syo",
                instance="prod",
                source_git_ref="6b3c9d7e8f901234567890abcdef1234567890ab",
                resolved_target=ResolvedTargetEvidence(
                    target_type="application",
                    target_id="svc-123",
                    target_name="syo-prod-service",
                ),
                deploy=DeploymentEvidence(
                    target_name="syo-prod-service",
                    target_type="application",
                    deploy_mode="fake-cloud-service-api",
                    provider_id="fake-cloud",
                    target_category="service",
                    provider_target_type="managed-service",
                    deployment_id="deploy-123",
                    status="pass",
                    started_at="2026-04-20T15:30:00Z",
                    finished_at="2026-04-20T15:32:00Z",
                ),
            )

            store.write_deployment_record(record)
            loaded_record = store.read_deployment_record(record.record_id)
            store.close()

        deployed_target = loaded_record.deployed_target
        assert deployed_target is not None
        self.assertEqual(deployed_target.provider_id, "fake-cloud")
        self.assertEqual(deployed_target.provider_target_type, "managed-service")
        self.assertEqual(deployed_target.target_id, "svc-123")

    def test_write_and_list_generic_web_rollback_plan_records(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_generic_web_rollback_plan_record(
                _generic_web_rollback_plan_record(
                    plan_id="rollback-plan-older",
                    created_at="2026-05-01T21:00:00Z",
                )
            )
            store.write_generic_web_rollback_plan_record(
                _generic_web_rollback_plan_record(
                    plan_id="rollback-plan-newer",
                    created_at="2026-05-01T22:00:00Z",
                )
            )
            listed_records = store.list_generic_web_rollback_plan_records(
                context_name="sellyouroutboard-testing", instance_name="prod"
            )
            store.close()

        self.assertEqual(
            [record.plan_id for record in listed_records],
            ["rollback-plan-newer", "rollback-plan-older"],
        )

    def test_list_preview_records_filters_and_limits(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()

            store.write_preview_record(
                _preview_record(
                    preview_id="preview-verireel-testing-verireel-pr-101",
                    updated_at="2026-04-20T10:01:00Z",
                    pr_number=101,
                )
            )
            store.write_preview_record(
                _preview_record(
                    preview_id="preview-verireel-testing-verireel-pr-102",
                    updated_at="2026-04-20T10:03:00Z",
                    pr_number=102,
                )
            )
            store.write_preview_record(
                _preview_record(
                    preview_id="preview-verireel-testing-verireel-pr-103",
                    updated_at="2026-04-20T10:02:00Z",
                    pr_number=103,
                )
            )

            listed_records = store.list_preview_records(
                context_name="verireel-testing",
                anchor_repo="verireel",
                limit=2,
            )
            store.close()

        self.assertEqual(
            [record.preview_id for record in listed_records],
            [
                "preview-verireel-testing-verireel-pr-102",
                "preview-verireel-testing-verireel-pr-103",
            ],
        )

    def test_write_read_and_list_preview_enablement_records(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()

            older_record = _preview_enablement_record(
                record_id="verireel-testing-verireel-pr-101",
                updated_at="2026-04-20T10:01:00Z",
                pr_number=101,
            )
            newer_record = _preview_enablement_record(
                record_id="verireel-testing-verireel-pr-102",
                updated_at="2026-04-20T10:03:00Z",
                pr_number=102,
            )
            closed_record = _preview_enablement_record(
                record_id="verireel-testing-verireel-pr-103",
                updated_at="2026-04-20T10:02:00Z",
                pr_number=103,
            ).model_copy(update={"pr_state": "closed"})

            store.write_preview_enablement_record(older_record)
            store.write_preview_enablement_record(newer_record)
            store.write_preview_enablement_record(closed_record)
            read_record = store.read_preview_enablement_record(newer_record.record_id)
            listed_records = store.list_preview_enablement_records(
                context_name="verireel-testing",
                anchor_repo="verireel",
                pr_state="open",
                limit=2,
            )
            store.close()

        self.assertEqual(read_record.anchor_pr_number, 102)
        self.assertEqual(
            [record.record_id for record in listed_records],
            ["verireel-testing-verireel-pr-102", "verireel-testing-verireel-pr-101"],
        )

    def test_preview_summaries_include_latest_generation(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()

            preview = _preview_record(
                preview_id="preview-verireel-testing-verireel-pr-123",
                updated_at="2026-04-20T10:05:00Z",
                pr_number=123,
            )
            store.write_preview_record(preview)
            store.write_preview_generation_record(
                _preview_generation_record(
                    generation_id="preview-verireel-testing-verireel-pr-123-generation-0001",
                    preview_id=preview.preview_id,
                )
            )
            store.write_preview_generation_record(
                _preview_generation_record(
                    generation_id="preview-verireel-testing-verireel-pr-123-generation-0002",
                    preview_id=preview.preview_id,
                ).model_copy(
                    update={
                        "sequence": 2,
                        "requested_at": "2026-04-20T10:06:00Z",
                        "ready_at": "2026-04-20T10:08:00Z",
                        "finished_at": "2026-04-20T10:08:00Z",
                        "artifact_id": "artifact-verireel-pr-123-bbbbbbbb",
                    }
                )
            )

            summary = store.read_preview_summary(preview_id=preview.preview_id)
            listed_summaries = store.list_preview_summaries(
                context_name="verireel-testing",
                anchor_repo="verireel",
                generation_limit=1,
            )
            store.close()

        latest_generation = summary.latest_generation
        assert latest_generation is not None
        self.assertEqual(summary.preview.preview_id, preview.preview_id)
        self.assertEqual(
            latest_generation.generation_id,
            "preview-verireel-testing-verireel-pr-123-generation-0002",
        )
        self.assertEqual(len(summary.recent_generations), 2)
        self.assertEqual(len(listed_summaries), 1)
        self.assertEqual(len(listed_summaries[0].recent_generations), 1)
        listed_latest_generation = listed_summaries[0].latest_generation
        assert listed_latest_generation is not None
        self.assertEqual(listed_latest_generation.sequence, 2)

    def test_preview_inventory_scan_records_round_trip(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_preview_inventory_scan_record(
                PreviewInventoryScanRecord(
                    scan_id="preview-inventory-scan-verireel-testing-20260420T100500Z",
                    context="verireel-testing",
                    scanned_at="2026-04-20T10:05:00Z",
                    source="verireel-preview-inventory",
                    status="pass",
                    preview_count=2,
                    preview_slugs=("pr-123", "pr-124"),
                )
            )
            store.write_preview_inventory_scan_record(
                PreviewInventoryScanRecord(
                    scan_id="preview-inventory-scan-verireel-testing-20260420T100600Z",
                    context="verireel-testing",
                    scanned_at="2026-04-20T10:06:00Z",
                    source="verireel-preview-inventory",
                    status="pass",
                    preview_count=0,
                    preview_slugs=(),
                )
            )
            listed_records = store.list_preview_inventory_scan_records(
                context_name="verireel-testing",
                limit=1,
            )
            store.close()

        self.assertEqual(len(listed_records), 1)
        self.assertEqual(
            listed_records[0].scan_id,
            "preview-inventory-scan-verireel-testing-20260420T100600Z",
        )
        self.assertEqual(listed_records[0].preview_count, 0)

    def test_authz_policy_records_round_trip(self) -> None:
        policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "github_actions": [
                    {
                        "repository": "cbusillo/launchplane",
                        "workflow_refs": [
                            "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main"
                        ],
                        "event_names": ["workflow_dispatch"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": ["launchplane_service_deploy.execute"],
                    }
                ]
            }
        )
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_authz_policy_record(
                LaunchplaneAuthzPolicyRecord(
                    record_id="launchplane-authz-policy-20260420T100500Z-test",
                    status="active",
                    source="test",
                    updated_at="2026-04-20T10:05:00Z",
                    policy=policy,
                )
            )
            listed_records = store.list_authz_policy_records(status="active", limit=1)
            store.close()

        self.assertEqual(len(listed_records), 1)
        self.assertEqual(
            listed_records[0].record_id, "launchplane-authz-policy-20260420T100500Z-test"
        )
        self.assertEqual(
            listed_records[0].policy.github_actions[0].repository, "cbusillo/launchplane"
        )

    def test_runtime_key_safety_policy_records_round_trip(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_runtime_key_safety_policy_record(
                RuntimeKeySafetyPolicyRecord(
                    record_id="runtime-key-safety-policy-20260505T200000Z-test",
                    status="active",
                    source="test",
                    updated_at="2026-05-05T20:00:00Z",
                    rules=(
                        RuntimeSecretSafetyRule(
                            binding_key="SHOPIFY_ACCESS_TOKEN",
                            secret_class="testing",
                            allowed_contexts=("opw",),
                            allowed_instances=("testing",),
                        ),
                    ),
                )
            )
            listed_records = store.list_runtime_key_safety_policy_records(status="active", limit=1)
            store.close()

        self.assertEqual(len(listed_records), 1)
        self.assertEqual(
            listed_records[0].record_id,
            "runtime-key-safety-policy-20260505T200000Z-test",
        )
        self.assertEqual(listed_records[0].rules[0].secret_class, "testing")

    def test_runtime_key_safety_cli_imports_lists_and_evaluates_policy(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            policy_file = Path(temporary_directory_name) / "runtime-key-safety.json"
            policy_file.write_text(
                json.dumps(
                    {
                        "updated_at": "2026-05-05T20:00:00Z",
                        "rules": [
                            {
                                "binding_key": "SHOPIFY_ACCESS_TOKEN",
                                "secret_class": "testing",
                                "allowed_contexts": ["opw"],
                                "allowed_instances": ["testing"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            store.write_secret_binding(
                SecretBinding(
                    binding_id="binding-shopify-token",
                    secret_id="secret-shopify-token",
                    integration="runtime_environment",
                    binding_key="SHOPIFY_ACCESS_TOKEN",
                    context="opw",
                    instance="testing",
                    created_at="2026-05-05T20:00:00Z",
                    updated_at="2026-05-05T20:00:00Z",
                )
            )
            store.close()
            runner = CliRunner()

            import_result = runner.invoke(
                main,
                [
                    "runtime-key-safety",
                    "import-policy",
                    "--database-url",
                    database_url,
                    "--policy-file",
                    str(policy_file),
                    "--source-label",
                    "test",
                ],
            )
            list_result = runner.invoke(
                main,
                [
                    "runtime-key-safety",
                    "list-policies",
                    "--database-url",
                    database_url,
                    "--status",
                    "active",
                ],
            )
            evaluate_result = runner.invoke(
                main,
                [
                    "runtime-key-safety",
                    "evaluate",
                    "--database-url",
                    database_url,
                    "--context",
                    "opw",
                    "--instance",
                    "testing",
                    "--environment-class",
                    "testing",
                    "--binding-key",
                    "SHOPIFY_ACCESS_TOKEN",
                ],
            )

        self.assertEqual(import_result.exit_code, 0, import_result.output)
        self.assertIn('"rule_count": 1', import_result.output)
        self.assertEqual(list_result.exit_code, 0, list_result.output)
        self.assertIn('"count": 1', list_result.output)
        self.assertEqual(evaluate_result.exit_code, 0, evaluate_result.output)
        self.assertIn('"status": "pass"', evaluate_result.output)

    def test_merge_train_policy_cli_imports_and_lists_policy(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            policy_file = Path(temporary_directory_name) / "merge-train-policy.toml"
            policy_file.write_text(
                """
schema_version = 1

[[policies]]
repository = "cbusillo/codex-skills"
base_branch = "main"
enqueue_label = "ready-to-merge"
blocked_label = "merge-blocked"
merge_method = "merge"
failure_policy = "pause_train"
[policies.enqueue]
label_required = true
allowed_actor_roles = ["repo_owner", "repo_admin"]
[policies.merge_identity]
kind = "github_actions_oidc"
name = "launchplane-merge-train"
[policies.github_token]
env_var = "GH_TOKEN"
""".strip(),
                encoding="utf-8",
            )
            runner = CliRunner()

            import_result = runner.invoke(
                main,
                [
                    "merge-train-policies",
                    "import-policy",
                    "--database-url",
                    database_url,
                    "--apply",
                    "--policy-file",
                    str(policy_file),
                    "--source-label",
                    "test",
                ],
            )
            list_result = runner.invoke(
                main,
                [
                    "merge-train-policies",
                    "list",
                    "--database-url",
                    database_url,
                    "--status",
                    "active",
                ],
            )

        self.assertEqual(import_result.exit_code, 0, import_result.output)
        self.assertIn('"repository_count": 1', import_result.output)
        self.assertIn('"cbusillo/codex-skills:main"', import_result.output)
        self.assertEqual(list_result.exit_code, 0, list_result.output)
        self.assertIn('"count": 1', list_result.output)
        self.assertIn('"cbusillo/codex-skills:main"', list_result.output)

    def test_product_profile_records_round_trip(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_product_profile_record(_product_profile_record())
            store.write_product_profile_record(_product_profile_record(product="internal-tool"))
            loaded_record = store.read_product_profile_record("sellyouroutboard")
            listed_records = store.list_product_profile_records(driver_id="generic-web")
            store.close()

        self.assertEqual(loaded_record.driver_id, "generic-web")
        self.assertEqual(loaded_record.image.repository, "ghcr.io/cbusillo/sellyouroutboard")
        self.assertEqual(loaded_record.preview.context, "sellyouroutboard-testing")
        self.assertEqual(
            [record.product for record in listed_records], ["internal-tool", "sellyouroutboard"]
        )

    def test_product_profiles_cli_upserts_lists_and_shows_records(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            lanes_json = json.dumps(
                [
                    {
                        "instance": "testing",
                        "context": "sellyouroutboard-testing",
                        "base_url": "https://testing.sellyouroutboard.com",
                        "health_url": "https://testing.sellyouroutboard.com/api/health",
                    }
                ]
            )
            runner = CliRunner()

            upsert_result = runner.invoke(
                main,
                [
                    "product-profiles",
                    "upsert",
                    "--database-url",
                    database_url,
                    "--product",
                    "sellyouroutboard",
                    "--display-name",
                    "SellYourOutboard.com",
                    "--repository",
                    "cbusillo/sellyouroutboard",
                    "--image-repository",
                    "ghcr.io/cbusillo/sellyouroutboard",
                    "--runtime-port",
                    "3000",
                    "--health-path",
                    "/api/health",
                    "--lanes-json",
                    lanes_json,
                    "--preview-enabled",
                    "--preview-context",
                    "sellyouroutboard-testing",
                    "--preview-enable-label",
                    "preview",
                    "--updated-at",
                    "2026-04-30T22:00:00Z",
                    "--source-label",
                    "test",
                ],
            )
            self.assertEqual(upsert_result.exit_code, 0, upsert_result.output)

            list_result = runner.invoke(
                main,
                [
                    "product-profiles",
                    "list",
                    "--database-url",
                    database_url,
                    "--driver-id",
                    "generic-web",
                ],
            )
            show_result = runner.invoke(
                main,
                [
                    "product-profiles",
                    "show",
                    "--database-url",
                    database_url,
                    "--product",
                    "sellyouroutboard",
                ],
            )

        self.assertEqual(list_result.exit_code, 0, list_result.output)
        self.assertIn('"count": 1', list_result.output)
        self.assertIn('"product": "sellyouroutboard"', list_result.output)
        self.assertEqual(show_result.exit_code, 0, show_result.output)
        self.assertIn('"preview_context": "sellyouroutboard-testing"', list_result.output)
        self.assertIn('"preview_enable_label": "preview"', list_result.output)
        self.assertIn('"enable_label": "preview"', show_result.output)
        self.assertIn('"health_path": "/api/health"', show_result.output)

    def test_preview_lifecycle_plan_records_round_trip(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_preview_lifecycle_plan_record(
                PreviewLifecyclePlanRecord(
                    plan_id="preview-lifecycle-plan-verireel-testing-20260420T100500Z",
                    product="verireel",
                    context="verireel-testing",
                    planned_at="2026-04-20T10:05:00Z",
                    source="preview-janitor",
                    status="pass",
                    inventory_scan_id="preview-inventory-scan-verireel-testing-20260420T100500Z",
                    desired_previews=(PreviewLifecycleDesiredPreview(preview_slug="pr-123"),),
                    desired_slugs=("pr-123",),
                    actual_slugs=("pr-122", "pr-123"),
                    keep_slugs=("pr-123",),
                    orphaned_slugs=("pr-122",),
                )
            )
            listed_records = store.list_preview_lifecycle_plan_records(
                context_name="verireel-testing",
                limit=1,
            )
            store.close()

        self.assertEqual(len(listed_records), 1)
        self.assertEqual(
            listed_records[0].plan_id,
            "preview-lifecycle-plan-verireel-testing-20260420T100500Z",
        )
        self.assertEqual(listed_records[0].orphaned_slugs, ("pr-122",))

    def test_preview_desired_state_records_round_trip(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_preview_desired_state_record(
                PreviewDesiredStateRecord(
                    desired_state_id="preview-desired-state-verireel-testing-20260420T100500Z",
                    product="verireel",
                    context="verireel-testing",
                    source="launchplane-preview-lifecycle",
                    discovered_at="2026-04-20T10:05:00Z",
                    repository="every/verireel",
                    label="preview",
                    anchor_repo="verireel",
                    status="pass",
                    desired_count=1,
                    desired_previews=(PreviewLifecycleDesiredPreview(preview_slug="pr-123"),),
                )
            )
            listed_records = store.list_preview_desired_state_records(
                context_name="verireel-testing",
                limit=1,
            )
            store.close()

        self.assertEqual(len(listed_records), 1)
        self.assertEqual(
            listed_records[0].desired_state_id,
            "preview-desired-state-verireel-testing-20260420T100500Z",
        )
        self.assertEqual(listed_records[0].desired_count, 1)
        self.assertEqual(listed_records[0].desired_previews[0].preview_slug, "pr-123")

    def test_preview_lifecycle_cleanup_records_round_trip(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_preview_lifecycle_cleanup_record(
                PreviewLifecycleCleanupRecord(
                    cleanup_id="preview-lifecycle-cleanup-verireel-testing-20260420T100500Z",
                    product="verireel",
                    context="verireel-testing",
                    plan_id="preview-lifecycle-plan-verireel-testing-20260420T100500Z",
                    inventory_scan_id="preview-inventory-scan-verireel-testing-20260420T100500Z",
                    requested_at="2026-04-20T10:05:00Z",
                    source="preview-janitor",
                    apply=True,
                    status="pass",
                    planned_slugs=("pr-122",),
                    destroyed_slugs=("pr-122",),
                    results=(
                        PreviewLifecycleCleanupResult(
                            preview_slug="pr-122",
                            anchor_repo="verireel",
                            anchor_pr_number=122,
                            status="destroyed",
                            application_name="ver-preview-pr-122-app",
                            application_id="app-122",
                            preview_url="https://pr-122.preview.example",
                        ),
                    ),
                )
            )
            listed_records = store.list_preview_lifecycle_cleanup_records(
                context_name="verireel-testing",
                limit=1,
            )
            store.close()

        self.assertEqual(len(listed_records), 1)
        self.assertEqual(
            listed_records[0].cleanup_id,
            "preview-lifecycle-cleanup-verireel-testing-20260420T100500Z",
        )
        self.assertEqual(
            listed_records[0].plan_id,
            "preview-lifecycle-plan-verireel-testing-20260420T100500Z",
        )
        self.assertEqual(listed_records[0].destroyed_slugs, ("pr-122",))
        self.assertEqual(listed_records[0].results[0].application_id, "app-122")

    def test_runner_host_hygiene_audit_records_round_trip(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            older_record = _runner_host_hygiene_audit_record(
                audit_record_key="runner-host-hygiene/2026-05-23/chris-testing"
            )
            newer_record = _runner_host_hygiene_audit_record(
                audit_record_key="runner-host-hygiene/2026-05-24/chris-testing"
            )
            failed_record = _runner_host_hygiene_audit_record(
                audit_record_key="runner-host-hygiene/2026-05-25/chris-testing",
                status="failed",
                message="post-apply evidence reported low disk",
            )

            store.write_runner_host_hygiene_audit_record(older_record)
            store.write_runner_host_hygiene_audit_record(newer_record)
            store.write_runner_host_hygiene_audit_record(failed_record)
            planned_records = store.list_runner_host_hygiene_audit_records(
                host_name="Chris-Testing",
                status="planned",
            )
            limited_records = store.list_runner_host_hygiene_audit_records(limit=1)
            store.close()

        self.assertEqual(
            [record.audit_record_key for record in planned_records],
            [newer_record.audit_record_key, older_record.audit_record_key],
        )
        self.assertEqual(
            [record.audit_record_key for record in limited_records],
            [failed_record.audit_record_key],
        )
        self.assertEqual(limited_records[0].message, "post-apply evidence reported low disk")

    def test_runner_lane_registration_audit_records_round_trip(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            older_record = _runner_lane_registration_audit_record(
                audit_record_key="runner-lane-registration/2026-06-08/cm-website/old",
                host_name="Chris-Testing",
            )
            newer_record = _runner_lane_registration_audit_record(
                audit_record_key="runner-lane-registration/2026-06-09/cm-website/new"
            )
            failed_record = _runner_lane_registration_audit_record(
                audit_record_key="runner-lane-registration/2026-06-10/cm-website/failed",
                status="failed",
                message="post-registration inventory did not show the lane",
            )

            store.write_runner_lane_registration_audit_record(older_record)
            store.write_runner_lane_registration_audit_record(newer_record)
            store.write_runner_lane_registration_audit_record(failed_record)
            planned_records = store.list_runner_lane_registration_audit_records(
                repository="CBUSILLO/ODOO-TENANT-CM-WEBSITE",
                host_name="Chris-Testing",
                status="planned",
            )
            limited_records = store.list_runner_lane_registration_audit_records(limit=1)
            store.close()

        self.assertEqual(
            [record.audit_record_key for record in planned_records],
            [newer_record.audit_record_key, older_record.audit_record_key],
        )
        self.assertEqual(
            [record.audit_record_key for record in limited_records],
            [failed_record.audit_record_key],
        )
        self.assertEqual(
            limited_records[0].message,
            "post-registration inventory did not show the lane",
        )

    def test_preview_pr_feedback_records_round_trip(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_preview_pr_feedback_record(
                PreviewPrFeedbackRecord(
                    feedback_id="preview-pr-feedback-verireel-testing-pr-123-20260420T100800Z",
                    product="verireel",
                    context="verireel-testing",
                    source="preview-control-plane",
                    requested_at="2026-04-20T10:08:00Z",
                    repository="every/verireel",
                    anchor_repo="verireel",
                    anchor_pr_number=123,
                    anchor_pr_url="https://github.com/every/verireel/pull/123",
                    status="ready",
                    marker="<!-- verireel-preview-control -->",
                    comment_markdown="<!-- verireel-preview-control -->\nPreview ready.",
                    preview_url="https://pr-123.preview.example",
                    immutable_image_reference="ghcr.io/every/verireel:pr-123-a1b2c3d4",
                    refresh_image_reference="ghcr.io/every/verireel:preview-pr-123",
                    revision="a1b2c3d4",
                    run_url="https://github.com/every/verireel/actions/runs/123",
                    delivery_status="delivered",
                    delivery_action="updated_comment",
                    comment_id=456,
                    comment_url="https://github.com/every/verireel/pull/123#issuecomment-456",
                )
            )
            listed_records = store.list_preview_pr_feedback_records(
                context_name="verireel-testing",
                limit=1,
            )
            store.close()

        self.assertEqual(len(listed_records), 1)
        self.assertEqual(
            listed_records[0].feedback_id,
            "preview-pr-feedback-verireel-testing-pr-123-20260420T100800Z",
        )
        self.assertEqual(listed_records[0].delivery_status, "delivered")
        self.assertEqual(listed_records[0].comment_id, 456)

    def test_every_code_preview_gate_records_round_trip(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_every_code_preview_gate_record(
                EveryCodePreviewGateRecord(
                    gate_id="every-code-preview-gate-cbusillo-code-26-abcdef",
                    request_id="every-code-cbusillo-code-123-test",
                    repository="cbusillo/code",
                    issue_number=123,
                    issue_url="https://github.com/cbusillo/code/issues/123",
                    issue_author="octocat",
                    pr_number=26,
                    pr_url="https://github.com/cbusillo/code/pull/26",
                    head_sha="abcdef1234567890",
                    status="blocked",
                    created_at="2026-05-06T17:00:00Z",
                    updated_at="2026-05-06T18:00:00Z",
                    blocked_at="2026-05-06T18:00:00Z",
                    blocked_reason="Checks did not pass: static_checks",
                )
            )
            listed_records = store.list_every_code_preview_gate_records(
                request_id="every-code-cbusillo-code-123-test",
                status="blocked",
            )
            store.close()

        self.assertEqual(len(listed_records), 1)
        self.assertEqual(
            listed_records[0].gate_id,
            "every-code-preview-gate-cbusillo-code-26-abcdef",
        )
        self.assertEqual(listed_records[0].head_sha, "abcdef1234567890")
        self.assertEqual(listed_records[0].blocked_reason, "Checks did not pass: static_checks")

    def test_agent_write_intent_records_round_trip(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            record = _agent_write_intent_record()
            store.write_agent_write_intent_record(record)
            loaded_record = store.read_agent_write_intent_record(record.record_id)
            listed_records = store.list_agent_write_intent_records(
                status="allowed",
                product="launchplane",
                context_name="launchplane",
            )
            store.close()

        self.assertEqual(loaded_record.record_id, record.record_id)
        self.assertEqual(loaded_record.evaluation.intent, "every_code_rerun")
        self.assertEqual(len(listed_records), 1)
        self.assertEqual(listed_records[0].trace_id, "launchplane_req_test_write_intent")
        self.assertEqual(listed_records[0].evaluation.audit.reason_code, "authorized")

    def test_merge_train_run_records_round_trip(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            record = _merge_train_run_record()
            store.write_merge_train_run_record(record)
            store.write_merge_train_run_record(
                _merge_train_run_record(recorded_at="2026-05-09T02:06:00Z")
            )
            loaded_record = store.read_merge_train_run_record(record.run_id)
            latest_record = store.latest_merge_train_run_record(
                repository="cbusillo/sellyouroutboard",
                base_branch="main",
            )
            listed_records = store.list_merge_train_run_records(
                repository="cbusillo/sellyouroutboard",
                base_branch="main",
                mode="dry_run",
                status="merge",
            )
            store.close()

        self.assertEqual(loaded_record.run_id, record.run_id)
        self.assertEqual(loaded_record.selected_pr_number, 42)
        self.assertEqual(loaded_record.selected_head_sha, "head-42")
        self.assertEqual(loaded_record.policy_key, "cbusillo/sellyouroutboard:main")
        self.assertEqual(latest_record.recorded_at if latest_record else "", "2026-05-09T02:06:00Z")
        self.assertEqual(len(listed_records), 2)
        self.assertEqual(listed_records[0].recorded_at, "2026-05-09T02:06:00Z")

    def test_merge_train_policy_records_round_trip(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            policy = build_test_merge_train_policy_with_codex_skills()
            older_record = MergeTrainPolicyRecord(
                record_id="merge-train-policy-20260513T200000Z-old",
                status="superseded",
                source="test",
                updated_at="2026-05-13T20:00:00Z",
                policy=policy,
            )
            active_record = MergeTrainPolicyRecord(
                record_id="merge-train-policy-20260513T210000Z-active",
                status="active",
                source="test",
                updated_at="2026-05-13T21:00:00Z",
                policy=policy,
            )
            store.write_merge_train_policy_record(older_record)
            store.write_merge_train_policy_record(active_record)
            listed_records = store.list_merge_train_policy_records(status="active")
            store.close()

        self.assertEqual([record.record_id for record in listed_records], [active_record.record_id])
        self.assertEqual(listed_records[0].policy_sha256, policy.policy_sha256)
        self.assertEqual(
            listed_records[0]
            .policy.find_repository_policy(repository="cbusillo/codex-skills", base_branch="main")
            .blocked_label,
            "merge-blocked",
        )

    def test_merge_train_batch_candidate_records_round_trip(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            older_record = _merge_train_batch_candidate_record(
                record_id="merge-train-batch-candidate-20260514T005900Z-old",
                status="superseded",
                updated_at="2026-05-14T00:59:00Z",
            )
            active_record = _merge_train_batch_candidate_record()
            store.write_merge_train_batch_candidate_record(older_record)
            store.write_merge_train_batch_candidate_record(active_record)
            listed_records = store.list_merge_train_batch_candidate_records(
                repository="example/merge-train-repo",
                base_branch="main",
                status="active",
            )
            store.close()

        self.assertEqual([record.record_id for record in listed_records], [active_record.record_id])
        self.assertEqual(listed_records[0].candidate.entries[1].pull_request_number, 11)
        self.assertEqual(listed_records[0].candidate.required_checks_status, "pass")

    def test_merge_train_batch_landing_plan_records_round_trip(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            older_record = _merge_train_batch_landing_plan_record(
                record_id="merge-train-batch-landing-plan-20260514T005900Z-old",
                status="superseded",
                updated_at="2026-05-14T00:59:00Z",
            )
            active_record = _merge_train_batch_landing_plan_record()
            store.write_merge_train_batch_landing_plan_record(older_record)
            store.write_merge_train_batch_landing_plan_record(active_record)
            listed_records = store.list_merge_train_batch_landing_plan_records(
                repository="example/merge-train-repo",
                base_branch="main",
                status="active",
            )
            store.close()

        self.assertEqual([record.record_id for record in listed_records], [active_record.record_id])
        self.assertEqual(listed_records[0].landing_plan.entries[0].merge_method, "squash")
        self.assertEqual(listed_records[0].landing_plan.entries[1].pull_request_number, 11)

    def test_merge_train_stack_collapse_plan_records_round_trip(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            older_record = _merge_train_stack_collapse_plan_record(
                record_id="merge-train-stack-collapse-plan-20260514T012900Z-old",
                status="superseded",
                updated_at="2026-05-14T01:29:00Z",
            )
            active_record = _merge_train_stack_collapse_plan_record()
            store.write_merge_train_stack_collapse_plan_record(older_record)
            store.write_merge_train_stack_collapse_plan_record(active_record)
            listed_records = store.list_merge_train_stack_collapse_plan_records(
                repository="example/merge-train-repo",
                base_branch="main",
                status="active",
            )
            store.close()

        self.assertEqual([record.record_id for record in listed_records], [active_record.record_id])
        self.assertEqual(listed_records[0].plan.intent_source, "root_ready_to_merge")
        self.assertEqual(listed_records[0].plan.mutations[0].parent_pull_request_number, 10)

    def test_write_and_list_dokploy_target_id_records(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_dokploy_target_id_record(
                _dokploy_target_id_record(context="opw", instance="prod", target_id="compose-123")
            )
            store.write_dokploy_target_id_record(
                _dokploy_target_id_record(context="cm", instance="testing", target_id="compose-456")
            )
            loaded_record = store.read_dokploy_target_id_record(
                context_name="opw", instance_name="prod"
            )
            listed_records = store.list_dokploy_target_id_records()
            store.close()

        self.assertEqual(loaded_record.target_id, "compose-123")
        self.assertEqual(
            [(record.context, record.instance) for record in listed_records],
            [("cm", "testing"), ("opw", "prod")],
        )

    def test_write_read_and_list_edge_endpoint_records(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_edge_endpoint_record(_edge_endpoint_record())
            store.write_edge_endpoint_record(
                _edge_endpoint_record(endpoint_key="disabled-edge", status="disabled")
            )
            loaded_record = store.read_edge_endpoint_record("cm-prod-dokploy")
            active_records = store.list_edge_endpoint_records(provider="dokploy", status="active")
            store.close()

        self.assertEqual(loaded_record.server_name, "docker-cm-prod")
        self.assertEqual(loaded_record.upstream_host, "100.73.170.113")
        self.assertEqual(loaded_record.upstream_scheme, "https")
        self.assertEqual(loaded_record.upstream_port, 443)
        self.assertEqual([record.endpoint_key for record in active_records], ["cm-prod-dokploy"])

    def test_write_read_and_list_private_health_endpoint_records(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_private_health_endpoint_record(_private_health_endpoint_record())
            store.write_private_health_endpoint_record(
                _private_health_endpoint_record(
                    endpoint_key="disabled-runtime",
                    status="disabled",
                )
            )
            loaded_record = store.read_private_health_endpoint_record(
                "repairshopr-sync-prod-runtime"
            )
            active_records = store.list_private_health_endpoint_records(
                product="repairshopr-sync",
                context_name="repairshopr-sync",
                instance_name="prod",
                status="active",
            )
            store.close()

        self.assertEqual(loaded_record.product, "repairshopr-sync")
        self.assertEqual(loaded_record.context, "repairshopr-sync")
        self.assertEqual(loaded_record.instance, "prod")
        self.assertEqual(loaded_record.url, "http://10.0.0.5:8000/health")
        self.assertEqual(
            [record.endpoint_key for record in active_records],
            ["repairshopr-sync-prod-runtime"],
        )

    def test_edge_endpoint_record_rejects_hostname_upstream(self) -> None:
        with self.assertRaises(ValueError):
            _edge_endpoint_record(upstream_host="dokploy.shiny")

    def test_write_and_list_runtime_environment_records(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_runtime_environment_record(
                _runtime_environment_record(
                    scope="global", env={"ODOO_MASTER_PASSWORD": "shared-master"}
                )
            )
            store.write_runtime_environment_record(
                _runtime_environment_record(
                    scope="instance",
                    context="opw",
                    instance="local",
                    env={"ODOO_DB_PASSWORD": "local-secret"},
                )
            )
            listed_records = store.list_runtime_environment_records()
            store.close()

        self.assertEqual(
            [(record.scope, record.context, record.instance) for record in listed_records],
            [("global", "", ""), ("instance", "opw", "local")],
        )

    def test_read_lane_summary_uses_repository_queries_for_gui_state(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_artifact_manifest(_artifact_manifest())
            store.write_environment_inventory(_inventory_record())
            store.write_deployment_record(
                _deployment_record(
                    record_id="deployment-20260420T153000Z-opw-testing",
                    started_at="2026-04-20T15:30:00Z",
                    finished_at="2026-04-20T15:32:00Z",
                )
            )
            store.write_release_tuple_record(_release_tuple_record())
            store.write_dokploy_target_id_record(
                _dokploy_target_id_record(context="opw", instance="testing")
            )
            store.write_dokploy_target_record(
                _dokploy_target_record(context="opw", instance="testing")
            )
            store.write_provider_target_record(
                _provider_target_record(
                    context="opw",
                    instance="testing",
                    target_category="compose",
                    target_id="compose-123",
                    provider_target_type="compose",
                )
            )
            store.write_runtime_environment_record(
                _runtime_environment_record(
                    scope="global",
                    env={"ODOO_MASTER_PASSWORD": "shared-master"},
                )
            )
            store.write_runtime_environment_record(
                _runtime_environment_record(
                    scope="context",
                    context="opw",
                    env={"ODOO_DB_USER": "opw"},
                )
            )
            store.write_runtime_environment_record(
                _runtime_environment_record(
                    scope="instance",
                    context="opw",
                    instance="testing",
                    env={"ODOO_DB_NAME": "opw-testing"},
                )
            )
            store.write_odoo_instance_override_record(
                _odoo_instance_override_record(context="opw", instance="testing")
            )
            store.write_secret_binding(
                _secret_binding(
                    binding_id="binding-dokploy-token",
                    secret_id="secret-dokploy-token",
                    updated_at="2026-04-20T18:07:00Z",
                )
            )

            summary = store.read_lane_summary(context_name="opw", instance_name="testing")
            store.close()

        self.assertEqual(summary.context, "opw")
        self.assertEqual(summary.instance, "testing")
        inventory = summary.inventory
        assert inventory is not None
        inventory_artifact_identity = inventory.artifact_identity
        assert inventory_artifact_identity is not None
        self.assertEqual(inventory_artifact_identity.artifact_id, "artifact-20260420-a1b2c3d4")
        artifact_manifest = summary.artifact_manifest
        assert artifact_manifest is not None
        self.assertEqual(artifact_manifest.artifact_id, "artifact-20260420-a1b2c3d4")
        self.assertEqual(artifact_manifest.image.repository, "ghcr.io/cbusillo/odoo-tenant-opw")
        release_tuple = summary.release_tuple
        assert release_tuple is not None
        self.assertEqual(release_tuple.channel, "testing")
        latest_deployment = summary.latest_deployment
        assert latest_deployment is not None
        self.assertEqual(latest_deployment.record_id, "deployment-20260420T153000Z-opw-testing")
        self.assertIsNone(summary.latest_promotion)
        self.assertIsNone(summary.latest_backup_gate)
        dokploy_target_id = summary.dokploy_target_id
        assert dokploy_target_id is not None
        self.assertEqual(dokploy_target_id.target_id, "compose-123")
        dokploy_target = summary.dokploy_target
        assert dokploy_target is not None
        self.assertEqual(dokploy_target.target_name, "opw-testing")
        provider_target = summary.provider_target
        assert provider_target is not None
        self.assertEqual(provider_target.provider_id, "dokploy")
        self.assertEqual(provider_target.target_category, "compose")
        self.assertEqual(provider_target.target_id, "compose-123")
        deployed_target = summary.deployed_target
        assert deployed_target is not None
        self.assertEqual(deployed_target.provider_id, "dokploy")
        self.assertEqual(deployed_target.target_category, "compose")
        self.assertEqual(deployed_target.target_id, "compose-123")
        self.assertEqual(
            [
                (record.scope, record.context, record.instance)
                for record in summary.runtime_environment_records
            ],
            [("global", "", ""), ("context", "opw", ""), ("instance", "opw", "testing")],
        )
        odoo_instance_override = summary.odoo_instance_override
        assert odoo_instance_override is not None
        self.assertEqual(odoo_instance_override.config_parameters[0].key, "web.base.url")
        self.assertEqual(summary.secret_bindings[0].binding_key, "DOKPLOY_TOKEN")

    def test_write_read_and_list_odoo_instance_override_records(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            store.write_odoo_instance_override_record(
                _odoo_instance_override_record(context="opw", instance="prod")
            )
            store.write_odoo_instance_override_record(
                _odoo_instance_override_record(context="cm", instance="testing")
            )
            loaded_record = store.read_odoo_instance_override_record(
                context_name="opw", instance_name="prod"
            )
            listed_records = store.list_odoo_instance_override_records()
            store.close()

        self.assertEqual(loaded_record.config_parameters[0].key, "web.base.url")
        self.assertEqual(
            [(record.context, record.instance) for record in listed_records],
            [("cm", "testing"), ("opw", "prod")],
        )

    def test_secret_records_round_trip_and_find_latest(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()

            older_record = _secret_record(
                secret_id="secret-dokploy-token",
                updated_at="2026-04-20T18:05:00Z",
                current_version_id="secret-version-0001",
            )
            newer_record = _secret_record(
                secret_id="secret-dokploy-token",
                updated_at="2026-04-20T18:07:00Z",
                current_version_id="secret-version-0002",
            )
            older_version = _secret_version(
                version_id="secret-version-0001",
                secret_id=older_record.secret_id,
                created_at="2026-04-20T18:05:00Z",
            )
            newer_version = _secret_version(
                version_id="secret-version-0002",
                secret_id=newer_record.secret_id,
                created_at="2026-04-20T18:07:00Z",
            )
            binding = _secret_binding(
                binding_id="binding-dokploy-token",
                secret_id=newer_record.secret_id,
                updated_at="2026-04-20T18:07:00Z",
            )
            audit_event = _secret_audit_event(
                event_id="audit-secret-import-0001",
                secret_id=newer_record.secret_id,
                recorded_at="2026-04-20T18:07:30Z",
            )

            store.write_secret_record(older_record)
            store.write_secret_version(older_version)
            store.write_secret_record(newer_record)
            store.write_secret_version(newer_version)
            store.write_secret_binding(binding)
            store.write_secret_audit_event(audit_event)

            found_record = store.find_secret_record(
                scope="context_instance",
                integration="dokploy",
                name="api_token",
                context="opw",
                instance="testing",
            )
            listed_records = store.list_secret_records(
                integration="dokploy", context_name="opw", instance_name="testing"
            )
            listed_versions = store.list_secret_versions(secret_id=newer_record.secret_id)
            listed_bindings = store.list_secret_bindings(
                integration="dokploy",
                context_name="opw",
                instance_name="testing",
            )
            listed_events = store.list_secret_audit_events(secret_id=newer_record.secret_id)
            self.assertIsNotNone(found_record)
            assert found_record is not None
            self.assertEqual(found_record.secret_id, newer_record.secret_id)
            self.assertEqual(
                store.read_secret_record(newer_record.secret_id).current_version_id,
                "secret-version-0002",
            )
            self.assertEqual(
                store.read_secret_version("secret-version-0002").secret_id, newer_record.secret_id
            )
            self.assertEqual(
                [record.secret_id for record in listed_records], [newer_record.secret_id]
            )
            self.assertEqual(
                [version.version_id for version in listed_versions],
                ["secret-version-0002", "secret-version-0001"],
            )
            self.assertEqual(
                [item.binding_id for item in listed_bindings], ["binding-dokploy-token"]
            )
            self.assertEqual(
                [item.event_id for item in listed_events], ["audit-secret-import-0001"]
            )
            store.close()

    def test_import_core_records_from_filesystem(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            store.ensure_schema()
            filesystem_store = FilesystemRecordStore(
                state_dir=Path(temporary_directory_name) / "state"
            )
            filesystem_store.write_backup_gate_record(_backup_gate_record())
            filesystem_store.write_deployment_record(
                _deployment_record(
                    record_id="deployment-20260420T153000Z-opw-testing",
                    started_at="2026-04-20T15:30:00Z",
                    finished_at="2026-04-20T15:32:00Z",
                )
            )
            filesystem_store.write_promotion_record(
                _promotion_record(record_id="promotion-20260420T160500Z-opw-testing-to-prod")
            )
            filesystem_store.write_environment_inventory(_inventory_record())
            filesystem_store.write_artifact_manifest(_artifact_manifest())
            filesystem_store.write_odoo_instance_override_record(_odoo_instance_override_record())
            filesystem_store.write_preview_record(
                _preview_record(
                    preview_id="preview-verireel-testing-verireel-pr-123",
                    updated_at="2026-04-20T10:05:00Z",
                    pr_number=123,
                )
            )
            filesystem_store.write_preview_enablement_record(
                _preview_enablement_record(
                    record_id="verireel-testing-verireel-pr-123",
                    updated_at="2026-04-20T10:05:30Z",
                    pr_number=123,
                )
            )
            filesystem_store.write_preview_generation_record(
                _preview_generation_record(
                    generation_id="preview-verireel-testing-verireel-pr-123-generation-0001",
                    preview_id="preview-verireel-testing-verireel-pr-123",
                )
            )
            filesystem_store.write_preview_inventory_scan_record(
                PreviewInventoryScanRecord(
                    scan_id="preview-inventory-scan-verireel-testing-20260420T100500Z",
                    context="verireel-testing",
                    scanned_at="2026-04-20T10:05:00Z",
                    source="verireel-preview-inventory",
                    status="pass",
                    preview_count=1,
                    preview_slugs=("pr-123",),
                )
            )
            filesystem_store.write_preview_desired_state_record(
                PreviewDesiredStateRecord(
                    desired_state_id="preview-desired-state-verireel-testing-20260420T100550Z",
                    product="verireel",
                    context="verireel-testing",
                    source="launchplane-preview-lifecycle",
                    discovered_at="2026-04-20T10:05:50Z",
                    repository="every/verireel",
                    label="preview",
                    anchor_repo="verireel",
                    status="pass",
                    desired_count=1,
                    desired_previews=(PreviewLifecycleDesiredPreview(preview_slug="pr-123"),),
                )
            )
            filesystem_store.write_preview_lifecycle_plan_record(
                PreviewLifecyclePlanRecord(
                    plan_id="preview-lifecycle-plan-verireel-testing-20260420T100600Z",
                    product="verireel",
                    context="verireel-testing",
                    planned_at="2026-04-20T10:06:00Z",
                    source="preview-janitor",
                    status="pass",
                    inventory_scan_id="preview-inventory-scan-verireel-testing-20260420T100500Z",
                    desired_previews=(PreviewLifecycleDesiredPreview(preview_slug="pr-123"),),
                    desired_slugs=("pr-123",),
                    actual_slugs=("pr-123",),
                    keep_slugs=("pr-123",),
                )
            )
            filesystem_store.write_preview_lifecycle_cleanup_record(
                PreviewLifecycleCleanupRecord(
                    cleanup_id="preview-lifecycle-cleanup-verireel-testing-20260420T100700Z",
                    product="verireel",
                    context="verireel-testing",
                    plan_id="preview-lifecycle-plan-verireel-testing-20260420T100600Z",
                    inventory_scan_id="preview-inventory-scan-verireel-testing-20260420T100500Z",
                    requested_at="2026-04-20T10:07:00Z",
                    source="preview-janitor",
                    apply=False,
                    status="report_only",
                )
            )
            filesystem_store.write_preview_pr_feedback_record(
                PreviewPrFeedbackRecord(
                    feedback_id="preview-pr-feedback-verireel-testing-pr-123-20260420T100800Z",
                    product="verireel",
                    context="verireel-testing",
                    source="preview-control-plane",
                    requested_at="2026-04-20T10:08:00Z",
                    repository="every/verireel",
                    anchor_repo="verireel",
                    anchor_pr_number=123,
                    anchor_pr_url="https://github.com/every/verireel/pull/123",
                    status="ready",
                    marker="<!-- verireel-preview-control -->",
                    comment_markdown="<!-- verireel-preview-control -->\nPreview ready.",
                    delivery_status="skipped",
                    error_message="Launchplane runtime records do not expose GITHUB_TOKEN for this context",
                )
            )
            filesystem_store.write_runner_host_hygiene_audit_record(
                _runner_host_hygiene_audit_record(
                    audit_record_key="runner-host-hygiene/2026-05-23/chris-testing"
                )
            )
            filesystem_store.write_runner_lane_registration_audit_record(
                _runner_lane_registration_audit_record(
                    audit_record_key="runner-lane-registration/2026-06-08/cm-website/import"
                )
            )
            filesystem_store.write_release_tuple_record(_release_tuple_record())
            filesystem_store.write_product_profile_record(_product_profile_record())
            filesystem_store.write_runtime_key_safety_policy_record(
                RuntimeKeySafetyPolicyRecord(
                    record_id="runtime-key-safety-policy-20260505T200000Z-test",
                    status="active",
                    source="test",
                    updated_at="2026-05-05T20:00:00Z",
                    rules=(
                        RuntimeSecretSafetyRule(
                            binding_key="SHOPIFY_ACCESS_TOKEN",
                            secret_class="testing",
                        ),
                    ),
                )
            )
            filesystem_store.write_merge_train_policy_record(
                MergeTrainPolicyRecord(
                    record_id="merge-train-policy-20260513T210000Z-active",
                    source="test",
                    updated_at="2026-05-13T21:00:00Z",
                    policy=build_test_merge_train_policy_with_codex_skills(),
                )
            )
            filesystem_store.write_merge_train_run_record(_merge_train_run_record())
            filesystem_store.write_merge_train_batch_candidate_record(
                _merge_train_batch_candidate_record()
            )
            filesystem_store.write_merge_train_batch_landing_plan_record(
                _merge_train_batch_landing_plan_record()
            )
            filesystem_store.write_merge_train_stack_collapse_plan_record(
                _merge_train_stack_collapse_plan_record()
            )
            filesystem_store.write_odoo_stable_bootstrap_operation_record(
                OdooStableBootstrapOperationRecord.model_validate(
                    {
                        "operation_id": "odoo-stable-bootstrap-cm-testing-test",
                        "product": "odoo-tenant-cm",
                        "context": "cm",
                        "instance": "testing",
                        "idempotency_key": "bootstrap-cm-testing",
                        "request_fingerprint": "fingerprint-123",
                        "request": {
                            "schema_version": 1,
                            "product": "odoo-tenant-cm",
                            "context": "cm",
                            "instance": "testing",
                            "confirmation": "bootstrap cm testing",
                        },
                        "status": "pending",
                        "phase": "created",
                        "created_at": "2026-05-17T00:00:00Z",
                        "updated_at": "2026-05-17T00:00:00Z",
                    }
                )
            )
            filesystem_store.write_odoo_stable_target_replacement_operation_record(
                OdooStableTargetReplacementOperationRecord.model_validate(
                    {
                        "operation_id": "odoo-target-replacement-cm-testing-test",
                        "product": "odoo-tenant-cm",
                        "context": "cm",
                        "instance": "testing",
                        "idempotency_key": "replacement-cm-testing",
                        "request_fingerprint": "fingerprint-123",
                        "request": {
                            "schema_version": 1,
                            "product": "odoo-tenant-cm",
                            "instance": "testing",
                            "strategy": "recreate-in-place",
                            "allow_empty_data": False,
                        },
                        "status": "pending",
                        "phase": "created",
                        "created_at": "2026-05-17T00:00:00Z",
                        "updated_at": "2026-05-17T00:00:00Z",
                    }
                )
            )

            counts = store.import_core_records_from_filesystem(filesystem_store)
            self.assertEqual(
                counts,
                {
                    "artifacts": 1,
                    "authz_policies": 0,
                    "backup_gates": 1,
                    "deployments": 1,
                    "promotions": 1,
                    "inventory": 1,
                    "odoo_instance_overrides": 1,
                    "product_profiles": 1,
                    "preview_records": 1,
                    "preview_enablement": 1,
                    "preview_generations": 1,
                    "preview_desired_states": 1,
                    "preview_inventory_scans": 1,
                    "preview_lifecycle_cleanups": 1,
                    "preview_lifecycle_plans": 1,
                    "preview_pr_feedback": 1,
                    "runner_host_hygiene_audits": 1,
                    "runner_lane_registration_audits": 1,
                    "every_code_preview_gates": 0,
                    "agent_write_intents": 0,
                    "merge_train_pr_feedback": 0,
                    "merge_train_batch_candidates": 1,
                    "merge_train_batch_landing_plans": 1,
                    "merge_train_stack_collapse_plans": 1,
                    "merge_train_policies": 1,
                    "merge_train_runs": 1,
                    "odoo_stable_bootstrap_operations": 1,
                    "odoo_stable_target_replacement_operations": 1,
                    "release_tuples": 1,
                    "runtime_key_safety_policies": 1,
                },
            )
            self.assertEqual(
                store.read_promotion_record(
                    "promotion-20260420T160500Z-opw-testing-to-prod"
                ).to_instance,
                "prod",
            )
            self.assertEqual(
                store.read_preview_generation_record(
                    "preview-verireel-testing-verireel-pr-123-generation-0001"
                ).state,
                "ready",
            )
            self.assertEqual(
                store.read_preview_enablement_record(
                    "verireel-testing-verireel-pr-123"
                ).request_metadata_baseline_channel,
                "testing",
            )
            self.assertEqual(
                store.list_preview_inventory_scan_records(
                    context_name="verireel-testing",
                    limit=1,
                )[0].scan_id,
                "preview-inventory-scan-verireel-testing-20260420T100500Z",
            )
            self.assertEqual(
                store.list_preview_desired_state_records(
                    context_name="verireel-testing",
                    limit=1,
                )[0].desired_state_id,
                "preview-desired-state-verireel-testing-20260420T100550Z",
            )
            self.assertEqual(
                store.list_odoo_stable_bootstrap_operation_records(
                    context_name="cm",
                    instance_name="testing",
                    statuses=("pending",),
                    limit=1,
                )[0].operation_id,
                "odoo-stable-bootstrap-cm-testing-test",
            )
            self.assertEqual(
                store.list_odoo_stable_target_replacement_operation_records(
                    context_name="cm",
                    instance_name="testing",
                    statuses=("pending",),
                    limit=1,
                )[0].operation_id,
                "odoo-target-replacement-cm-testing-test",
            )
            self.assertEqual(
                store.list_preview_lifecycle_plan_records(
                    context_name="verireel-testing",
                    limit=1,
                )[0].plan_id,
                "preview-lifecycle-plan-verireel-testing-20260420T100600Z",
            )
            self.assertEqual(
                store.list_preview_lifecycle_cleanup_records(
                    context_name="verireel-testing",
                    limit=1,
                )[0].cleanup_id,
                "preview-lifecycle-cleanup-verireel-testing-20260420T100700Z",
            )
            self.assertEqual(
                store.list_preview_pr_feedback_records(
                    context_name="verireel-testing",
                    limit=1,
                )[0].feedback_id,
                "preview-pr-feedback-verireel-testing-pr-123-20260420T100800Z",
            )
            self.assertEqual(
                store.list_runner_host_hygiene_audit_records(
                    host_name="chris-testing",
                    limit=1,
                )[0].audit_record_key,
                "runner-host-hygiene/2026-05-23/chris-testing",
            )
            self.assertEqual(
                store.list_runner_lane_registration_audit_records(
                    repository="cbusillo/odoo-tenant-cm-website",
                    limit=1,
                )[0].audit_record_key,
                "runner-lane-registration/2026-06-08/cm-website/import",
            )
            self.assertEqual(
                store.list_runtime_key_safety_policy_records(status="active", limit=1)[0]
                .rules[0]
                .binding_key,
                "SHOPIFY_ACCESS_TOKEN",
            )
            store.close()
