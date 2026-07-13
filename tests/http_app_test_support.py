import hashlib
import json
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from unittest.mock import patch
from urllib.parse import urlencode

from fastapi import FastAPI
from httpx2 import AsyncClient, Response
from jwt import InvalidTokenError

from control_plane import secrets as control_plane_secrets
from control_plane.contracts.agent_write_intent import (
    AgentWriteIntentRecord,
    AgentWriteIntentRequest,
    build_agent_write_intent_record_id,
    evaluate_agent_write_intent,
)
from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.deployment_record import DeploymentRecord, ResolvedTargetEvidence
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.every_code_notifications import (
    EveryCodeNotificationAttemptRecord,
    EveryCodeNotificationDestination,
    EveryCodeNotificationPolicyRecord,
)
from control_plane.contracts.every_code_pr_feedback_record import EveryCodePrFeedbackRecord
from control_plane.contracts.every_code_preview_gate_record import EveryCodePreviewGateRecord
from control_plane.contracts.every_code_work_request import (
    EveryCodeWorkRequestRecord,
)
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.contracts.ingress_route_audit_record import (
    IngressRouteAuditOperation,
    IngressRouteAuditRecord,
)
from control_plane.contracts.merge_train_policy import (
    MergeTrainPolicyRecord,
)
from control_plane.contracts.odoo_stable_bootstrap_operation import (
    OdooStableBootstrapOperationRecord,
)
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.contracts.preview_generation_record import (
    PreviewGenerationRecord,
    PreviewGenerationState,
    PreviewPullRequestSummary,
)
from control_plane.contracts.preview_lifecycle_cleanup_record import (
    PreviewLifecycleCleanupRecord,
)
from control_plane.contracts.preview_lifecycle_plan_record import (
    PreviewLifecyclePlanRecord,
)
from control_plane.contracts.preview_pr_feedback_notifications import (
    PreviewPrFeedbackNotificationAttemptRecord,
    PreviewPrFeedbackNotificationDestination,
    PreviewPrFeedbackNotificationPolicyRecord,
)
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.promotion_record import (
    ArtifactIdentityReference,
    DeploymentEvidence,
    PromotionRecord,
)
from control_plane.contracts.public_ingress_monitoring import (
    PublicIngressIncidentRecord,
    PublicIngressNotificationDestination,
    PublicIngressNotificationPolicyRecord,
)
from control_plane.contracts.runner_host_hygiene import (
    RunnerHostHygieneApplyAuditRecord,
    RunnerHostHygieneApplyPolicy,
    RunnerHostHygieneApplyRequest,
    RunnerHostHygieneObservation,
    RunnerHostHygienePolicy,
    evaluate_runner_host_hygiene,
    plan_runner_host_hygiene_apply,
)
from control_plane.contracts.runner_lane_inventory import build_runner_lane_inventory
from control_plane.contracts.runner_lane_registration import (
    RunnerLaneRegistrationAuditRecord,
    RunnerLaneRegistrationPolicy,
    RunnerLaneRegistrationRequest,
    plan_runner_lane_registration,
)
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.runtime_key_safety_policy import (
    RuntimeKeySafetyPolicyRecord,
    RuntimeSecretSafetyRule,
)
from control_plane.contracts.secret_record import SecretBinding
from control_plane.merge_train import MergeTrainDryRunSnapshot
from control_plane.merge_train_github import MergeTrainGitHubError, MergeTrainGitHubStaleHeadError
from control_plane.service_auth import (
    BearerIdentityConfig,
    GitHubActionsIdentity,
    GitHubHumanIdentity,
    LaunchplaneAuthzPolicy,
    agent_authz_audit,
)
from control_plane.service_human_auth import (
    GitHubOAuthConfig,
    HumanSessionManager,
    LaunchplaneHumanSession,
    build_browser_mutation_request_headers,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.auth import _identity
from tests.support.http import get as http_get
from tests.support.http import request as http_request
from tests.support.merge_train import (
    _FakeMergeTrainGitHubClient,
    _FakeMergeTrainSnapshotReader,
    _FakeStackedMergeTrainSnapshotReader,
)
from tests.support.profiles import (
    _generic_site_profile_payload,
    _product_profile_payload_with_prod,
)


class _CountingMergeTrainSnapshotReader(_FakeMergeTrainSnapshotReader):
    read_calls = 0

    def read_merge_train_snapshot(
        self, *, repository: str, base_branch: str
    ) -> MergeTrainDryRunSnapshot:
        type(self).read_calls += 1
        return super().read_merge_train_snapshot(repository=repository, base_branch=base_branch)


class _StaleMergeTrainSnapshotReader(_FakeMergeTrainSnapshotReader):
    def read_merge_train_snapshot(
        self, *, repository: str, base_branch: str
    ) -> MergeTrainDryRunSnapshot:
        raise MergeTrainGitHubStaleHeadError(
            "Base branch moved outside the merge train snapshot.", status_code=409
        )


class _UnavailableMergeTrainSnapshotReader(_FakeMergeTrainSnapshotReader):
    def read_merge_train_snapshot(
        self, *, repository: str, base_branch: str
    ) -> MergeTrainDryRunSnapshot:
        raise MergeTrainGitHubError(
            "GitHub API request failed for /repos/example/repo", status_code=503
        )


class _UnsupportedStackMergeTrainSnapshotReader(_FakeStackedMergeTrainSnapshotReader):
    def read_merge_train_snapshot(
        self, *, repository: str, base_branch: str
    ) -> MergeTrainDryRunSnapshot:
        snapshot = super().read_merge_train_snapshot(repository=repository, base_branch=base_branch)
        return snapshot.model_copy(
            update={
                "pull_requests": tuple(
                    pull_request.model_copy(update={"head_ref": "feature/root"})
                    if pull_request.number == 2
                    else pull_request
                    for pull_request in snapshot.pull_requests
                )
            }
        )


class _UnavailableWorkerMergeTrainGitHubClient(_FakeMergeTrainGitHubClient):
    def merge_pull_request(
        self,
        *,
        repository: str,
        pull_request_number: int,
        head_sha: str,
        merge_method: str,
    ) -> str:
        raise MergeTrainGitHubError(
            "GitHub API request failed for /repos/example/repo/pulls/1/merge",
            status_code=503,
        )


class _CountingBatchCandidateMergeTrainSnapshotReader(_FakeMergeTrainSnapshotReader):
    read_calls = 0

    def read_merge_train_snapshot(
        self, *, repository: str, base_branch: str
    ) -> MergeTrainDryRunSnapshot:
        type(self).read_calls += 1
        return super().read_merge_train_snapshot(repository=repository, base_branch=base_branch)


class _UnavailableBatchCandidateMergeTrainGitHubClient(_FakeMergeTrainGitHubClient):
    def build_batch_candidate(self, *, candidate: Any) -> Any:
        raise MergeTrainGitHubError(
            "GitHub API request failed for /repos/example/repo/git/refs",
            status_code=503,
        )


class _UnexpectedBatchCandidateMergeTrainGitHubClient(_FakeMergeTrainGitHubClient):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("batch candidate plan mode should not create a GitHub client")


class _StackCollapseWithoutBatchCandidateStore:
    backend_name = "test-stack-collapse-without-batch-candidate"

    def __init__(self, *, state_dir: Path) -> None:
        self.delegate = FilesystemRecordStore(state_dir=state_dir)

    def close(self) -> None:
        return None

    def read_idempotency_record(
        self,
        *,
        scope: str,
        route_path: str,
        idempotency_key: str,
    ) -> Any:
        return self.delegate.read_idempotency_record(
            scope=scope,
            route_path=route_path,
            idempotency_key=idempotency_key,
        )

    def write_idempotency_record(self, record: LaunchplaneIdempotencyRecord) -> object:
        return self.delegate.write_idempotency_record(record)

    def list_merge_train_policy_records(
        self, *, status: str = "", limit: int | None = None
    ) -> tuple[MergeTrainPolicyRecord, ...]:
        return self.delegate.list_merge_train_policy_records(status=status, limit=limit)

    def write_merge_train_stack_collapse_plan_record(self, record: Any) -> object:
        return self.delegate.write_merge_train_stack_collapse_plan_record(record)

    def list_merge_train_stack_collapse_plan_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[Any, ...]:
        return self.delegate.list_merge_train_stack_collapse_plan_records(
            repository=repository,
            base_branch=base_branch,
            status=status,
            limit=limit,
        )


class _BatchLandingWithoutLandingPlanStore:
    backend_name = "test-batch-landing-without-landing-plan"

    def __init__(self, *, state_dir: Path) -> None:
        self.delegate = FilesystemRecordStore(state_dir=state_dir)

    def close(self) -> None:
        return None

    def read_idempotency_record(
        self,
        *,
        scope: str,
        route_path: str,
        idempotency_key: str,
    ) -> Any:
        return self.delegate.read_idempotency_record(
            scope=scope,
            route_path=route_path,
            idempotency_key=idempotency_key,
        )

    def write_idempotency_record(self, record: LaunchplaneIdempotencyRecord) -> object:
        return self.delegate.write_idempotency_record(record)

    def list_merge_train_policy_records(
        self, *, status: str = "", limit: int | None = None
    ) -> tuple[MergeTrainPolicyRecord, ...]:
        return self.delegate.list_merge_train_policy_records(status=status, limit=limit)

    def write_merge_train_batch_candidate_record(self, record: Any) -> object:
        return self.delegate.write_merge_train_batch_candidate_record(record)

    def list_merge_train_batch_candidate_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[Any, ...]:
        return self.delegate.list_merge_train_batch_candidate_records(
            repository=repository,
            base_branch=base_branch,
            status=status,
            limit=limit,
        )

    def write_merge_train_stack_collapse_plan_record(self, record: Any) -> object:
        return self.delegate.write_merge_train_stack_collapse_plan_record(record)

    def list_merge_train_stack_collapse_plan_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[Any, ...]:
        return self.delegate.list_merge_train_stack_collapse_plan_records(
            repository=repository,
            base_branch=base_branch,
            status=status,
            limit=limit,
        )


def _product_environment_read_policy(
    *,
    context: str = "launchplane",
    contexts: tuple[str, ...] | None = None,
    products: tuple[str, ...] = ("example-site",),
) -> LaunchplaneAuthzPolicy:
    allowed_contexts = contexts if contexts is not None else (context,)
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": "every/verireel",
                    "workflow_refs": [
                        "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                    ],
                    "event_names": ["pull_request"],
                    "products": list(products),
                    "contexts": list(allowed_contexts),
                    "actions": ["product_environment.read"],
                }
            ]
        }
    )


def _work_graph_read_policy(
    *,
    products: tuple[str, ...] = ("launchplane", "example-site"),
    contexts: tuple[str, ...] = ("launchplane", "example-site"),
) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": "every/verireel",
                    "workflow_refs": [
                        "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                    ],
                    "event_names": ["pull_request"],
                    "products": list(products),
                    "contexts": list(contexts),
                    "actions": ["work_graph.rank", "product_environment.read"],
                }
            ]
        }
    )


def _github_human_work_graph_rank_policy() -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_humans": [
                {
                    "logins": ["example-operator"],
                    "roles": ["admin"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": ["work_graph.rank"],
                }
            ]
        }
    )


def _terminal_agent_work_graph_rank_policy() -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "terminal_agents": [
                {
                    "subjects": ["local-owner-agent"],
                    "token_labels": ["local-owner-read"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": ["work_graph.rank"],
                }
            ]
        }
    )


def _terminal_agent_merge_train_pr_feedback_policy() -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "terminal_agents": [
                {
                    "subjects": ["local-owner-agent"],
                    "token_labels": ["local-owner-read"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": ["merge_train.pr_feedback"],
                }
            ]
        }
    )


def _terminal_agent_merge_train_run_once_policy() -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "terminal_agents": [
                {
                    "subjects": ["local-owner-agent"],
                    "token_labels": ["local-owner-read"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": ["merge_train.run_once"],
                }
            ]
        }
    )


def _local_operator_work_graph_rank_policy() -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "local_operators": [
                {
                    "subjects": ["local-owner-agent"],
                    "token_labels": ["local-owner-read"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": ["work_graph.rank"],
                }
            ]
        }
    )


def _local_admin_work_graph_rank_policy() -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "local_admins": [
                {
                    "subjects": ["local-owner-agent"],
                    "token_labels": ["local-owner-admin"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": ["work_graph.rank"],
                }
            ]
        }
    )


def _driver_read_policy(*, context: str = "launchplane") -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "local_operators": [
                {
                    "subjects": ["local-owner-agent"],
                    "token_labels": ["local-owner-read"],
                    "products": ["launchplane"],
                    "contexts": [context],
                    "actions": ["driver.read"],
                }
            ]
        }
    )


def _backup_gate_write_identity() -> GitHubActionsIdentity:
    return _identity(
        repository="every/example-site",
        workflow_ref="every/example-site/.github/workflows/backup-gate.yml@refs/heads/main",
        event_name="workflow_dispatch",
    )


def _backup_gate_write_policy(*, context: str) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": "every/example-site",
                    "workflow_refs": [
                        "every/example-site/.github/workflows/backup-gate.yml@refs/heads/main"
                    ],
                    "event_names": ["workflow_dispatch"],
                    "products": ["example-site"],
                    "contexts": [context],
                    "actions": ["backup_gate.write"],
                }
            ]
        }
    )


def _github_human_backup_gate_write_policy(*, context: str) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_humans": [
                {
                    "logins": ["example-operator"],
                    "roles": ["admin"],
                    "products": ["example-site"],
                    "contexts": [context],
                    "actions": ["backup_gate.write"],
                }
            ]
        }
    )


def _terminal_agent_backup_gate_write_policy(*, context: str) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "terminal_agents": [
                {
                    "subjects": ["local-owner-agent"],
                    "token_labels": ["local-owner-read"],
                    "products": ["example-site"],
                    "contexts": [context],
                    "actions": ["backup_gate.write"],
                }
            ]
        }
    )


def _backup_gate_evidence_payload(
    *, record_id: str = "backup-gate-example-site-prod"
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "product": "example-site",
        "backup_gate": {
            "record_id": record_id,
            "context": "example-site",
            "instance": "prod",
            "created_at": "2026-04-21T18:05:00Z",
            "source": "example-site-prod-gate",
            "status": "pass",
            "evidence": {
                "snapshot_name": "snapshot-example-site-prod",
                "manifest_path": "scratch/prod-gates/snapshot-example-site-prod.json",
            },
        },
    }


def _promotion_write_identity() -> GitHubActionsIdentity:
    return _identity(
        repository="every/example-site",
        workflow_ref="every/example-site/.github/workflows/promote-prod.yml@refs/heads/main",
        event_name="workflow_dispatch",
    )


def _promotion_write_policy(*, context: str) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": "every/example-site",
                    "workflow_refs": [
                        "every/example-site/.github/workflows/promote-prod.yml@refs/heads/main"
                    ],
                    "event_names": ["workflow_dispatch"],
                    "products": ["example-site"],
                    "contexts": [context],
                    "actions": ["promotion.write"],
                }
            ]
        }
    )


def _public_ingress_monitor_identity() -> GitHubActionsIdentity:
    return _identity(
        repository="cbusillo/launchplane",
        workflow_ref="cbusillo/launchplane/.github/workflows/public-ingress-monitor.yml@refs/heads/main",
        event_name="schedule",
    )


def _public_ingress_monitor_policy() -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": "cbusillo/launchplane",
                    "workflow_refs": [
                        "cbusillo/launchplane/.github/workflows/public-ingress-monitor.yml@refs/heads/main"
                    ],
                    "event_names": ["schedule"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": ["public_ingress_monitor.run_once"],
                }
            ]
        }
    )


def _public_ingress_incident(
    *, context: str = "launchplane", instance: str = "prod"
) -> PublicIngressIncidentRecord:
    return PublicIngressIncidentRecord(
        incident_id=f"public-ingress-incident-{context}-{instance}",
        product="launchplane",
        context=context,
        instance=instance,
        status="open",
        opened_at="2026-05-29T12:00:00Z",
        opened_observation_id="public-ingress-observation-opened",
        latest_observation_id="public-ingress-observation-latest",
        latest_observed_at="2026-05-29T12:00:00Z",
        failure_code="http_error",
        summary="Public ingress failed.",
    )


def _github_human_promotion_write_policy(*, context: str) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_humans": [
                {
                    "logins": ["example-operator"],
                    "roles": ["admin"],
                    "products": ["example-site"],
                    "contexts": [context],
                    "actions": ["promotion.write"],
                }
            ]
        }
    )


def _terminal_agent_promotion_write_policy(*, context: str) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "terminal_agents": [
                {
                    "subjects": ["local-owner-agent"],
                    "token_labels": ["local-owner-read"],
                    "products": ["example-site"],
                    "contexts": [context],
                    "actions": ["promotion.write"],
                }
            ]
        }
    )


def _promotion_evidence_payload(
    *,
    record_id: str = "promotion-example-site-testing-to-prod",
    link_deployment: bool = True,
) -> dict[str, object]:
    promotion: dict[str, object] = {
        "record_id": record_id,
        "artifact_identity": {"artifact_id": "artifact-example-site-prod"},
        "backup_record_id": "backup-example-site-prod-20260420T155000Z",
        "context": "example-site",
        "from_instance": "testing",
        "to_instance": "prod",
        "backup_gate": {
            "required": True,
            "status": "pass",
            "evidence": {"recorded_by": "launchplane-service"},
        },
        "deploy": {
            "target_name": "example-site-prod",
            "target_type": "application",
            "deploy_mode": "runtime-provider-api",
            "deployment_id": "provider-deployment-example-site-prod",
            "status": "pass",
            "started_at": "2026-04-20T16:05:00Z",
            "finished_at": "2026-04-20T16:08:30Z",
        },
        "destination_health": {
            "verified": True,
            "urls": ["https://example.invalid/health"],
            "timeout_seconds": 45,
            "status": "pass",
        },
    }
    if link_deployment:
        promotion["deployment_record_id"] = "deployment-example-site-prod"
    return {
        "schema_version": 1,
        "product": "example-site",
        "promotion": promotion,
    }


def _promotion_evidence_store(
    state_dir: Path,
    *,
    deployment_context: str = "example-site",
    deployment_instance: str = "prod",
    artifact_id: str = "artifact-example-site-prod",
) -> FilesystemRecordStore:
    store = FilesystemRecordStore(state_dir=state_dir)
    store.write_deployment_record(
        DeploymentRecord(
            record_id="deployment-example-site-prod",
            artifact_identity=ArtifactIdentityReference(artifact_id=artifact_id),
            context=deployment_context,
            instance=deployment_instance,
            source_git_ref="6b3c9d7e8f901234567890abcdef1234567890ab",
            resolved_target=ResolvedTargetEvidence(
                target_type="application",
                target_id="target-example-site-prod",
                target_name="example-site-prod",
            ),
            deploy=DeploymentEvidence(
                target_name="example-site-prod",
                target_type="application",
                deploy_mode="runtime-provider-api",
                deployment_id="provider-deployment-example-site-prod",
                status="pass",
                started_at="2026-04-20T16:05:00Z",
                finished_at="2026-04-20T16:08:30Z",
            ),
        )
    )
    return store


def _preview_generation_write_identity() -> GitHubActionsIdentity:
    return _identity(
        repository="every/example-site",
        workflow_ref="every/example-site/.github/workflows/preview-control-plane.yml@refs/heads/main",
        event_name="pull_request",
    )


def _preview_generation_write_policy(*, context: str) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": "every/example-site",
                    "workflow_refs": [
                        "every/example-site/.github/workflows/preview-control-plane.yml@refs/heads/main"
                    ],
                    "event_names": ["pull_request"],
                    "products": ["example-site"],
                    "contexts": [context],
                    "actions": ["preview_generation.write"],
                }
            ]
        }
    )


def _github_human_preview_generation_write_policy(*, context: str) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_humans": [
                {
                    "logins": ["example-operator"],
                    "roles": ["admin"],
                    "products": ["example-site"],
                    "contexts": [context],
                    "actions": ["preview_generation.write"],
                }
            ]
        }
    )


def _terminal_agent_preview_generation_write_policy(*, context: str) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "terminal_agents": [
                {
                    "subjects": ["local-owner-agent"],
                    "token_labels": ["local-owner-read"],
                    "products": ["example-site"],
                    "contexts": [context],
                    "actions": ["preview_generation.write"],
                }
            ]
        }
    )


def _preview_destroyed_write_identity() -> GitHubActionsIdentity:
    return _identity(
        repository="every/example-site",
        workflow_ref="every/example-site/.github/workflows/preview-control-plane.yml@refs/heads/main",
        event_name="pull_request",
    )


def _preview_destroyed_write_policy(*, context: str) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": "every/example-site",
                    "workflow_refs": [
                        "every/example-site/.github/workflows/preview-control-plane.yml@refs/heads/main"
                    ],
                    "event_names": ["pull_request"],
                    "products": ["example-site"],
                    "contexts": [context],
                    "actions": ["preview_destroyed.write"],
                }
            ]
        }
    )


def _github_human_preview_destroyed_write_policy(*, context: str) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_humans": [
                {
                    "logins": ["example-operator"],
                    "roles": ["admin"],
                    "products": ["example-site"],
                    "contexts": [context],
                    "actions": ["preview_destroyed.write"],
                }
            ]
        }
    )


def _terminal_agent_preview_destroyed_write_policy(*, context: str) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "terminal_agents": [
                {
                    "subjects": ["local-owner-agent"],
                    "token_labels": ["local-owner-read"],
                    "products": ["example-site"],
                    "contexts": [context],
                    "actions": ["preview_destroyed.write"],
                }
            ]
        }
    )


def _preview_generation_evidence_payload(*, anchor_pr_number: int = 42) -> dict[str, object]:
    pr_url = f"https://github.com/every/example-site/pull/{anchor_pr_number}"
    return {
        "schema_version": 1,
        "product": "example-site",
        "preview": {
            "schema_version": 1,
            "context": "example-site",
            "anchor_repo": "example-site",
            "anchor_pr_number": anchor_pr_number,
            "anchor_pr_url": pr_url,
            "canonical_url": f"https://pr-{anchor_pr_number}.example.invalid",
            "state": "active",
            "updated_at": "2026-04-16T08:10:00Z",
            "eligible_at": "2026-04-16T08:10:00Z",
        },
        "generation": {
            "schema_version": 1,
            "context": "example-site",
            "anchor_repo": "example-site",
            "anchor_pr_number": anchor_pr_number,
            "anchor_pr_url": pr_url,
            "anchor_head_sha": "abcdef1234567890abcdef1234567890abcdef12",
            "state": "ready",
            "requested_reason": "external_preview_refresh",
            "requested_at": "2026-04-16T08:02:00Z",
            "ready_at": "2026-04-16T08:10:00Z",
            "finished_at": "2026-04-16T08:10:00Z",
            "resolved_manifest_fingerprint": f"example-preview-pr-{anchor_pr_number}-abcdef",
            "artifact_id": "ghcr.io/every/example-site:pr-42-abcdef",
            "deploy_status": "pass",
            "verify_status": "pass",
            "overall_health_status": "pass",
        },
    }


def _preview_record_for_destroy(*, anchor_pr_number: int = 42) -> PreviewRecord:
    preview_id = f"preview-example-site-example-site-pr-{anchor_pr_number}"
    return PreviewRecord(
        preview_id=preview_id,
        context="example-site",
        anchor_repo="example-site",
        anchor_pr_number=anchor_pr_number,
        anchor_pr_url=f"https://github.com/every/example-site/pull/{anchor_pr_number}",
        preview_label=f"example-site/example-site/pr-{anchor_pr_number}",
        canonical_url=f"https://pr-{anchor_pr_number}.example.invalid",
        state="active",
        created_at="2026-04-16T08:00:00Z",
        updated_at="2026-04-16T08:10:00Z",
        eligible_at="2026-04-16T08:00:00Z",
        active_generation_id=f"{preview_id}-generation-0001",
        serving_generation_id=f"{preview_id}-generation-0001",
        latest_generation_id=f"{preview_id}-generation-0001",
        latest_manifest_fingerprint=f"example-preview-pr-{anchor_pr_number}-abcdef",
    )


def _preview_read_record(*, anchor_pr_number: int = 42) -> PreviewRecord:
    return _preview_record_for_destroy(anchor_pr_number=anchor_pr_number)


def _preview_generation_read_record(
    *,
    anchor_pr_number: int = 42,
    sequence: int = 1,
    state: PreviewGenerationState = "ready",
) -> PreviewGenerationRecord:
    preview_id = f"preview-example-site-example-site-pr-{anchor_pr_number}"
    generation_id = f"{preview_id}-generation-{sequence:04d}"
    return PreviewGenerationRecord(
        generation_id=generation_id,
        preview_id=preview_id,
        sequence=sequence,
        state=state,
        requested_reason="external_preview_refresh",
        requested_at="2026-04-16T08:02:00Z",
        ready_at="2026-04-16T08:10:00Z" if state == "ready" else "",
        finished_at="2026-04-16T08:10:00Z" if state == "ready" else "",
        resolved_manifest_fingerprint=f"example-preview-pr-{anchor_pr_number}-abcdef",
        artifact_id=f"ghcr.io/every/example-site:pr-{anchor_pr_number}-abcdef",
        anchor_summary=PreviewPullRequestSummary(
            repo="example-site",
            pr_number=anchor_pr_number,
            head_sha="abcdef1234567890abcdef1234567890abcdef12",
            pr_url=f"https://github.com/every/example-site/pull/{anchor_pr_number}",
        ),
        deploy_status="pass" if state == "ready" else "pending",
        verify_status="pass" if state == "ready" else "pending",
        overall_health_status="pass" if state == "ready" else "pending",
    )


def _write_recent_operations_records(store: FilesystemRecordStore) -> None:
    store.write_environment_inventory(_environment_inventory_read_record())
    store.write_deployment_record(_deployment_read_record())
    store.write_promotion_record(_promotion_read_record())
    store.write_preview_record(_preview_read_record())


def _write_secret_status_records(database_url: str) -> dict[str, str]:
    store = PostgresRecordStore(database_url=database_url)
    store.ensure_schema()
    with patch.dict(
        "os.environ",
        {control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"},
        clear=True,
    ):
        global_secret = control_plane_secrets.write_secret_value(
            record_store=store,
            scope="global",
            integration=control_plane_secrets.DOKPLOY_SECRET_INTEGRATION,
            name="token",
            plaintext_value="global-token",
            binding_key="DOKPLOY_TOKEN",
            actor="test",
        )
        context_secret = control_plane_secrets.write_secret_value(
            record_store=store,
            scope="context",
            integration=control_plane_secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
            name="GITHUB_WEBHOOK_SECRET",
            plaintext_value="plain-secret-value-alpha",
            binding_key="GITHUB_WEBHOOK_SECRET",
            context_name="example-site",
            actor="test",
        )
        instance_secret = control_plane_secrets.write_secret_value(
            record_store=store,
            scope="context_instance",
            integration=control_plane_secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
            name="SMTP_PASSWORD",
            plaintext_value="plain-secret-value-beta",
            binding_key="SMTP_PASSWORD",
            context_name="example-site",
            instance_name="prod",
            actor="test",
        )
        other_instance_secret = control_plane_secrets.write_secret_value(
            record_store=store,
            scope="context_instance",
            integration=control_plane_secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
            name="SMTP_PASSWORD",
            plaintext_value="plain-secret-value-gamma",
            binding_key="SMTP_PASSWORD",
            context_name="example-site",
            instance_name="testing",
            actor="test",
        )
    store.close()
    return {
        "global": str(global_secret["secret_id"]),
        "context": str(context_secret["secret_id"]),
        "instance": str(instance_secret["secret_id"]),
        "other_instance": str(other_instance_secret["secret_id"]),
    }


def _preview_destroyed_evidence_payload(*, anchor_pr_number: int = 42) -> dict[str, object]:
    return {
        "schema_version": 1,
        "product": "example-site",
        "destroy": {
            "schema_version": 1,
            "context": "example-site",
            "anchor_repo": "example-site",
            "anchor_pr_number": anchor_pr_number,
            "destroyed_at": "2026-04-16T09:04:00Z",
            "destroy_reason": "external_preview_cleanup_completed",
        },
    }


def _runner_host_hygiene_audit_write_identity() -> GitHubActionsIdentity:
    return _identity(
        repository="cbusillo/launchplane",
        workflow_ref=(
            "cbusillo/launchplane/.github/workflows/runner-host-hygiene.yml@refs/heads/main"
        ),
        event_name="workflow_dispatch",
    )


def _runner_host_hygiene_audit_write_policy() -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": "cbusillo/launchplane",
                    "workflow_refs": [
                        "cbusillo/launchplane/.github/workflows/runner-host-hygiene.yml@refs/heads/main"
                    ],
                    "event_names": ["workflow_dispatch"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": ["runner_host_hygiene_audit.write"],
                }
            ]
        }
    )


def _github_human_runner_host_hygiene_audit_write_policy() -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_humans": [
                {
                    "logins": ["example-operator"],
                    "roles": ["admin"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": ["runner_host_hygiene_audit.write"],
                }
            ]
        }
    )


def _terminal_agent_runner_host_hygiene_audit_write_policy() -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "terminal_agents": [
                {
                    "subjects": ["local-owner-agent"],
                    "token_labels": ["local-owner-read"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": ["runner_host_hygiene_audit.write"],
                }
            ]
        }
    )


def _runner_host_hygiene_audit_payload(
    *,
    audit_record_key: str = "runner-host-hygiene/2026-05-23/chris-testing",
    product: str = "launchplane",
) -> dict[str, object]:
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
        mutate=False,
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
    audit_record = RunnerHostHygieneApplyAuditRecord(
        audit_record_key=audit_record_key,
        status="planned",
        request=request,
        plan=plan,
        pre_apply_report=report,
        message="planned runner host hygiene apply; no host mutation was executed",
    )
    return {
        "schema_version": 1,
        "product": product,
        "audit": audit_record.model_dump(mode="json"),
    }


def _runner_lane_registration_audit_write_identity() -> GitHubActionsIdentity:
    return _identity(
        repository="cbusillo/launchplane",
        workflow_ref=(
            "cbusillo/launchplane/.github/workflows/runner-lane-registration.yml@refs/heads/main"
        ),
        event_name="workflow_dispatch",
    )


def _runner_lane_registration_audit_write_policy() -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": "cbusillo/launchplane",
                    "workflow_refs": [
                        "cbusillo/launchplane/.github/workflows/runner-lane-registration.yml@refs/heads/main"
                    ],
                    "event_names": ["workflow_dispatch"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": ["runner_lane_registration_audit.write"],
                }
            ]
        }
    )


def _github_human_runner_lane_registration_audit_write_policy() -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_humans": [
                {
                    "logins": ["example-operator"],
                    "roles": ["admin"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": ["runner_lane_registration_audit.write"],
                }
            ]
        }
    )


def _terminal_agent_runner_lane_registration_audit_write_policy() -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "terminal_agents": [
                {
                    "subjects": ["local-owner-agent"],
                    "token_labels": ["local-owner-read"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": ["runner_lane_registration_audit.write"],
                }
            ]
        }
    )


def _runner_lane_registration_audit_payload(
    *,
    audit_record_key: str = "runner-lane-registration/2026-06-08/cm-website/dry-run",
    product: str = "launchplane",
) -> dict[str, object]:
    inventory = build_runner_lane_inventory(
        repository="cbusillo/odoo-tenant-cm-website",
        observed_at="2026-06-08T17:30:00Z",
        lanes=(),
    )
    request = RunnerLaneRegistrationRequest(
        repository="cbusillo/odoo-tenant-cm-website",
        host_name="chris-testing",
        lane_name="cm-website-runner-1",
        registration_root="/opt/actions-runners",
        labels=("self-hosted", "launchplane", "launchplane-managed"),
        mutate=False,
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
    audit_record = RunnerLaneRegistrationAuditRecord(
        audit_record_key=audit_record_key,
        status="planned",
        request=request,
        plan=plan,
        pre_inventory=inventory,
        message="planned runner lane registration; no host mutation was executed",
    )
    return {
        "schema_version": 1,
        "product": product,
        "audit": audit_record.model_dump(mode="json"),
    }


def _deployment_write_identity() -> GitHubActionsIdentity:
    return _identity(
        repository="every/example-site",
        workflow_ref="every/example-site/.github/workflows/deploy-prod.yml@refs/heads/main",
        event_name="workflow_dispatch",
    )


def _deployment_write_policy(*, context: str) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": "every/example-site",
                    "workflow_refs": [
                        "every/example-site/.github/workflows/deploy-prod.yml@refs/heads/main"
                    ],
                    "event_names": ["workflow_dispatch"],
                    "products": ["example-site"],
                    "contexts": [context],
                    "actions": ["deployment.write"],
                }
            ]
        }
    )


def _record_read_policy(
    *,
    action: str,
    context: str,
    extra_actions: tuple[str, ...] = (),
) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": "every/verireel",
                    "workflow_refs": [
                        "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                    ],
                    "event_names": ["pull_request"],
                    "products": ["launchplane"],
                    "contexts": [context],
                    "actions": [action, *extra_actions],
                }
            ]
        }
    )


def _private_health_endpoint_read_policy() -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": "every/verireel",
                    "workflow_refs": [
                        "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                    ],
                    "event_names": ["pull_request"],
                    "products": ["repairshopr-sync"],
                    "contexts": ["repairshopr-sync"],
                    "actions": ["private_health_endpoint.read"],
                }
            ]
        }
    )


def _github_human_deployment_write_policy(*, context: str) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_humans": [
                {
                    "logins": ["example-operator"],
                    "roles": ["admin"],
                    "products": ["example-site"],
                    "contexts": [context],
                    "actions": ["deployment.write"],
                }
            ]
        }
    )


def _terminal_agent_deployment_write_policy(*, context: str) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "terminal_agents": [
                {
                    "subjects": ["local-owner-agent"],
                    "token_labels": ["local-owner-read"],
                    "products": ["example-site"],
                    "contexts": [context],
                    "actions": ["deployment.write"],
                }
            ]
        }
    )


def _deployment_evidence_payload(
    *, record_id: str = "deployment-example-site-prod"
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "product": "example-site",
        "deployment": {
            "record_id": record_id,
            "artifact_identity": {"artifact_id": "artifact-example-site-prod"},
            "context": "example-site",
            "instance": "prod",
            "source_git_ref": "6b3c9d7e8f901234567890abcdef1234567890ab",
            "resolved_target": {
                "target_type": "application",
                "target_id": "target-example-site-prod",
                "target_name": "example-site-prod",
            },
            "deploy": {
                "target_name": "example-site-prod",
                "target_type": "application",
                "deploy_mode": "runtime-provider-api",
                "deployment_id": "provider-deployment-example-site-prod",
                "status": "pass",
                "started_at": "2026-04-20T15:30:00Z",
                "finished_at": "2026-04-20T15:32:00Z",
            },
            "post_deploy_update": {
                "attempted": True,
                "status": "pass",
                "detail": "Update completed.",
            },
            "destination_health": {
                "verified": True,
                "urls": ["https://example.invalid/health"],
                "timeout_seconds": 45,
                "status": "pass",
            },
        },
    }


def _deployment_read_record() -> DeploymentRecord:
    payload = _deployment_evidence_payload()["deployment"]
    return DeploymentRecord.model_validate(payload)


def _promotion_read_record() -> PromotionRecord:
    payload = _promotion_evidence_payload()["promotion"]
    return PromotionRecord.model_validate(payload)


def _environment_inventory_read_record() -> EnvironmentInventory:
    deployment = _deployment_read_record()
    return EnvironmentInventory(
        context=deployment.context,
        instance=deployment.instance,
        artifact_identity=deployment.artifact_identity,
        source_git_ref=deployment.source_git_ref,
        deploy=deployment.deploy,
        post_deploy_update=deployment.post_deploy_update,
        destination_health=deployment.destination_health,
        updated_at="2026-04-20T15:33:00Z",
        deployment_record_id=deployment.record_id,
    )


def _github_human_driver_read_policy(*, context: str = "launchplane") -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_humans": [
                {
                    "logins": ["example-operator"],
                    "roles": ["admin"],
                    "products": ["launchplane"],
                    "contexts": [context],
                    "actions": ["driver.read"],
                }
            ]
        }
    )


def _driver_context_store(state_dir: Path) -> FilesystemRecordStore:
    store = FilesystemRecordStore(state_dir=state_dir)
    store.write_product_profile_record(
        LaunchplaneProductProfileRecord.model_validate(_generic_site_profile_payload())
    )
    store.write_deployment_record(
        DeploymentRecord(
            record_id="deployment-example-site-testing",
            artifact_identity=ArtifactIdentityReference(
                artifact_id="ghcr.io/every/example-site@sha256:abc123"
            ),
            context="example-site",
            instance="testing",
            source_git_ref="6b3c9d7e8f901234567890abcdef1234567890ab",
            resolved_target=ResolvedTargetEvidence(
                target_type="application",
                target_id="target-example-site-testing",
                target_name="example-site-testing",
            ),
            deploy=DeploymentEvidence(
                target_name="example-site-testing",
                target_type="application",
                deploy_mode="runtime-provider-api",
                deployment_id="provider-deployment-example-site-testing",
                status="pass",
                started_at="2026-04-20T15:30:00Z",
                finished_at="2026-04-20T15:32:00Z",
            ),
        )
    )
    return store


def _write_context_cutover_audit_records(database_url: str) -> None:
    store = PostgresRecordStore(database_url=database_url)
    store.ensure_schema()
    try:
        store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(_product_profile_payload_with_prod())
        )
        store.write_runtime_environment_record(
            RuntimeEnvironmentRecord(
                scope="instance",
                context="sellyouroutboard-testing",
                instance="prod",
                env={"TAWK_PROPERTY_ID": "property-legacy"},
                updated_at="2026-05-01T00:03:00Z",
                source_label="legacy",
            )
        )
        store.write_runtime_environment_record(
            RuntimeEnvironmentRecord(
                scope="instance",
                context="sellyouroutboard",
                instance="prod",
                env={"TAWK_WIDGET_ID": "widget-canonical"},
                updated_at="2026-05-01T00:04:00Z",
                source_label="operator:mistake",
            )
        )
        with patch.dict(
            "os.environ",
            {control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"},
            clear=True,
        ):
            control_plane_secrets.write_secret_value(
                record_store=store,
                scope="context_instance",
                integration=control_plane_secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
                name="smtp-password",
                plaintext_value="smtp-password-secret",
                binding_key="SMTP_PASSWORD",
                context_name="sellyouroutboard-testing",
                instance_name="prod",
                actor="test",
            )
    finally:
        store.close()


def _seed_product_environment_read_records(database_url: str) -> None:
    store = PostgresRecordStore(database_url=database_url)
    store.ensure_schema()
    try:
        store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(_generic_site_profile_payload())
        )
        store.write_runtime_environment_record(
            RuntimeEnvironmentRecord(
                scope="instance",
                context="example-site",
                instance="prod",
                env={"INTERNAL_CALLBACK_URL": "https://internal.example-site.invalid"},
                updated_at="2026-05-02T22:32:00Z",
                source_label="test",
            )
        )
        store.write_dokploy_target_record(
            DokployTargetRecord(
                context="example-site",
                instance="prod",
                target_type="application",
                target_name="example-site-prod",
                updated_at="2026-05-02T22:33:00Z",
                source_label="test",
            )
        )
        store.write_dokploy_target_id_record(
            DokployTargetIdRecord(
                context="example-site",
                instance="prod",
                target_id="app-prod-123",
                updated_at="2026-05-02T22:33:00Z",
                source_label="test",
            )
        )
        store.write_provider_target_record(
            ProviderTargetRecord(
                context="example-site",
                instance="prod",
                provider_id="dokploy",
                target_category="application",
                target_id="app-prod-123",
                display_name="example-site-prod",
                provider_target_type="application",
                updated_at="2026-05-02T22:33:00Z",
                source_label="test",
            )
        )
        with patch.dict(
            "os.environ",
            {control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"},
            clear=True,
        ):
            control_plane_secrets.write_secret_value(
                record_store=store,
                scope="context_instance",
                integration=control_plane_secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
                name="SMTP_PASSWORD",
                plaintext_value="super-secret-password",
                binding_key="SMTP_PASSWORD",
                context_name="example-site",
                instance_name="prod",
                actor="test",
            )
        policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "github_actions": [
                    {
                        "repository": "every/verireel",
                        "workflow_refs": [
                            "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                        ],
                        "event_names": ["pull_request"],
                        "products": ["example-site"],
                        "contexts": ["launchplane"],
                        "actions": ["product_environment.read"],
                    }
                ]
            }
        )
        store.write_authz_policy_record(
            LaunchplaneAuthzPolicyRecord(
                record_id="launchplane-authz-policy-product-environment-read-test",
                source="test",
                updated_at="2026-05-02T22:35:00Z",
                policy=policy,
            )
        )
    finally:
        store.close()


def _seed_dokploy_target_inspect_records(database_url: str) -> None:
    store = PostgresRecordStore(database_url=database_url)
    store.ensure_schema()
    try:
        store.write_dokploy_target_record(
            DokployTargetRecord(
                context="cm_website",
                instance="prod",
                target_type="compose",
                target_name="cm-prod",
                project_name="odoo",
                updated_at="2026-06-14T00:00:00Z",
                source_label="test",
            )
        )
        store.write_dokploy_target_id_record(
            DokployTargetIdRecord(
                context="cm_website",
                instance="prod",
                target_id="compose-cm-prod",
                updated_at="2026-06-14T00:00:00Z",
                source_label="test",
            )
        )
        store.write_provider_target_record(
            ProviderTargetRecord(
                context="cm_website",
                instance="prod",
                provider_id="dokploy",
                target_category="compose",
                target_id="compose-cm-prod",
                display_name="cm-prod",
                provider_target_type="compose",
                provider_evidence={"project_name": "odoo"},
                updated_at="2026-06-14T00:00:00Z",
                source_label="test",
            )
        )
    finally:
        store.close()


def _seed_empty_agent_context_read_store(database_url: str) -> None:
    store = PostgresRecordStore(database_url=database_url)
    store.ensure_schema()
    store.close()


def _seed_agent_context_read_records(database_url: str) -> None:
    store = PostgresRecordStore(database_url=database_url)
    store.ensure_schema()
    try:
        store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(_generic_site_profile_payload())
        )
        request_id = "every-code-every-example-site-190-test"
        store.write_every_code_work_request_record(
            EveryCodeWorkRequestRecord(
                request_id=request_id,
                source="manual",
                state="queued",
                repository="every/example-site",
                issue_number=190,
                issue_url="https://github.com/every/example-site/issues/190",
                issue_title="Build operator chooser",
                trigger_label="every-code",
                trigger_actor="cbusillo",
                queued_at="2026-05-06T02:00:00Z",
                updated_at="2026-05-06T02:00:00Z",
            )
        )
        store.write_every_code_work_request_record(
            EveryCodeWorkRequestRecord(
                request_id="every-code-cbusillo-tooling-12-test",
                source="manual",
                state="queued",
                repository="cbusillo/tooling",
                issue_number=12,
                issue_url="https://github.com/cbusillo/tooling/issues/12",
                issue_title="Support repo follow-up",
                trigger_label="every-code",
                trigger_actor="cbusillo",
                queued_at="2026-05-08T18:00:00Z",
                updated_at="2026-05-08T18:00:00Z",
            )
        )
        store.write_every_code_preview_gate_record(
            EveryCodePreviewGateRecord(
                gate_id="every-code-preview-gate-every-example-site-190-31",
                request_id=request_id,
                repository="every/example-site",
                issue_number=190,
                issue_url="https://github.com/every/example-site/issues/190",
                pr_number=31,
                pr_url="https://github.com/every/example-site/pull/31",
                head_sha="abcdef1234567890",
                status="ready",
                created_at="2026-05-08T18:00:00Z",
                updated_at="2026-05-08T18:01:00Z",
                ready_at="2026-05-08T18:01:00Z",
                last_checked_at="2026-05-08T18:01:00Z",
            )
        )
    finally:
        store.close()


def _seed_every_code_read_records(store: Any) -> dict[str, str]:
    request_id = "every-code-cbusillo-code-123-test"
    gate_id = "every-code-preview-gate-cbusillo-code-31-test"
    feedback_id = "every-code-pr-feedback-cbusillo-code-31-review-1"
    preview_feedback_id = "preview-feedback-cbusillo-code-31-skipped"
    notification_attempt_id = "every-code-notification-cbusillo-code-123-test"
    preview_notification_attempt_id = "preview-pr-feedback-notification-cbusillo-code-31-skipped"
    store.write_every_code_work_request_record(
        EveryCodeWorkRequestRecord(
            request_id=request_id,
            source="manual",
            state="queued",
            repository="cbusillo/code",
            issue_number=123,
            issue_url="https://github.com/cbusillo/code/issues/123",
            issue_title="Finish v2 Every Code read cutover",
            trigger_label="every-code",
            trigger_actor="cbusillo",
            queued_at="2026-06-18T12:00:00Z",
            updated_at="2026-06-18T12:00:00Z",
        )
    )
    store.write_every_code_work_request_record(
        EveryCodeWorkRequestRecord(
            request_id="every-code-cbusillo-other-456-test",
            source="manual",
            state="done",
            repository="cbusillo/other",
            issue_number=456,
            issue_url="https://github.com/cbusillo/other/issues/456",
            issue_title="Unrelated completed request",
            trigger_label="every-code",
            trigger_actor="cbusillo",
            queued_at="2026-06-17T12:00:00Z",
            claimed_at="2026-06-17T12:01:00Z",
            claimed_by_host="test-host",
            started_at="2026-06-17T12:02:00Z",
            finished_at="2026-06-17T12:03:00Z",
            updated_at="2026-06-17T12:03:00Z",
            result_pr_url="https://github.com/cbusillo/other/pull/4",
        )
    )
    store.write_every_code_preview_gate_record(
        EveryCodePreviewGateRecord(
            gate_id=gate_id,
            request_id=request_id,
            repository="cbusillo/code",
            issue_number=123,
            issue_url="https://github.com/cbusillo/code/issues/123",
            pr_number=31,
            pr_url="https://github.com/cbusillo/code/pull/31",
            head_sha="abcdef1234567890",
            status="blocked",
            created_at="2026-06-18T12:05:00Z",
            updated_at="2026-06-18T12:06:00Z",
            blocked_at="2026-06-18T12:06:00Z",
            last_checked_at="2026-06-18T12:06:00Z",
            blocked_reason="Required preview checks failed.",
        )
    )
    store.write_every_code_pr_feedback_record(
        EveryCodePrFeedbackRecord(
            feedback_id=feedback_id,
            request_id=request_id,
            repository="cbusillo/code",
            pr_number=31,
            pr_url="https://github.com/cbusillo/code/pull/31",
            feedback_kind="pull_request_review_comment",
            github_delivery_id="delivery-feedback-1",
            github_node_id="PRRC_kwDOTest123",
            actor="reviewer",
            author_association="MEMBER",
            body="Please tighten the FastAPI read route coverage.",
            html_url="https://github.com/cbusillo/code/pull/31#discussion_r1",
            received_at="2026-06-18T12:07:00Z",
            status="pending",
        )
    )
    store.write_every_code_notification_attempt_record(
        EveryCodeNotificationAttemptRecord(
            attempt_id=notification_attempt_id,
            request_id=request_id,
            event="work_request_blocked",
            policy_id="every-code-notification-discord",
            destination_id="discord",
            destination_kind="discord",
            delivery_status="delivered",
            attempted_at="2026-06-18T12:08:00Z",
            action="posted_discord",
        )
    )
    store.write_preview_pr_feedback_notification_attempt_record(
        PreviewPrFeedbackNotificationAttemptRecord(
            attempt_id=preview_notification_attempt_id,
            feedback_id=preview_feedback_id,
            event="delivery_skipped",
            policy_id="preview-pr-feedback-notification-discord",
            destination_id="discord",
            destination_kind="discord",
            delivery_status="delivered",
            attempted_at="2026-06-18T12:09:00Z",
            action="posted_discord",
        )
    )
    return {
        "request_id": request_id,
        "gate_id": gate_id,
        "feedback_id": feedback_id,
        "preview_feedback_id": preview_feedback_id,
        "notification_attempt_id": notification_attempt_id,
        "preview_notification_attempt_id": preview_notification_attempt_id,
    }


def _seed_every_code_claim_request(store: Any) -> EveryCodeWorkRequestRecord:
    record = EveryCodeWorkRequestRecord(
        request_id="every-code-cbusillo-code-123-test",
        source="manual",
        state="queued",
        repository="cbusillo/code",
        issue_number=123,
        issue_url="https://github.com/cbusillo/code/issues/123",
        issue_title="Wire local automation",
        trigger_label="every-code",
        trigger_actor="cbusillo",
        queued_at="2026-05-05T22:00:00Z",
        updated_at="2026-05-05T22:00:00Z",
    )
    store.write_every_code_work_request_record(record)
    return record


def _every_code_read_policy() -> LaunchplaneAuthzPolicy:
    return _record_read_policy(
        action="every_code_work_request.read",
        context="launchplane",
        extra_actions=(
            "every_code_preview_gate.read",
            "every_code_pr_feedback.read",
            "every_code_notification_attempt.read",
            "preview_pr_feedback_notification_attempt.read",
        ),
    )


def _every_code_work_request_write_policy() -> LaunchplaneAuthzPolicy:
    return _record_read_policy(
        action="every_code_work_request.write",
        context="launchplane",
    )


def _every_code_work_request_claim_policy() -> LaunchplaneAuthzPolicy:
    return _record_read_policy(
        action="every_code_work_request.claim",
        context="launchplane",
    )


def _every_code_work_request_status_policy() -> LaunchplaneAuthzPolicy:
    return _record_read_policy(
        action="every_code_work_request.update",
        context="launchplane",
    )


def _every_code_work_request_rerun_policy() -> LaunchplaneAuthzPolicy:
    return _record_read_policy(
        action="every_code_work_request.rerun",
        context="launchplane",
    )


_AGENT_WRITE_INTENT_SOURCE_URL = "https://github.com/cbusillo/launchplane/issues/386"


def _agent_write_intent_policy(
    *, actions: tuple[str, ...], product: str, context: str
) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": "every/verireel",
                    "workflow_refs": [
                        "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                    ],
                    "event_names": ["pull_request"],
                    "products": [product],
                    "contexts": [context],
                    "actions": list(actions),
                }
            ]
        }
    )


def _terminal_agent_write_intent_policy() -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "terminal_agents": [
                {
                    "subjects": ["local-owner-agent"],
                    "token_labels": ["local-owner-read"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": ["every_code_work_request.rerun"],
                }
            ]
        }
    )


def _agent_write_intent_payload(
    *,
    intent: str,
    mode: str,
    product: str,
    context: str,
    source_url: str = _AGENT_WRITE_INTENT_SOURCE_URL,
    reason: str = "Evaluate agent write intent.",
    secret_bindings: list[str] | None = None,
    destination: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "intent": intent,
        "mode": mode,
        "product": product,
        "context": context,
        "source_url": source_url,
        "reason": reason,
    }
    if secret_bindings is not None:
        payload["secret_bindings"] = secret_bindings
    if destination is not None:
        payload["destination"] = destination
    return payload


def _seed_agent_write_intent_secret_binding(store: Any, *, binding_instance: str) -> None:
    store.write_runtime_key_safety_policy_record(
        RuntimeKeySafetyPolicyRecord(
            record_id="runtime-key-safety-policy-write-intent-test",
            status="active",
            source="test",
            updated_at="2026-05-05T20:00:00Z",
            rules=(
                RuntimeSecretSafetyRule(
                    binding_key="SMTP_PASSWORD",
                    secret_class="prod_only",
                    allowed_contexts=("sellyouroutboard",),
                    allowed_instances=("prod",),
                ),
            ),
        )
    )
    store.write_secret_binding(
        SecretBinding(
            binding_id="secret-smtp-password-binding-smtp-password",
            secret_id="secret-smtp-password",
            integration=control_plane_secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
            binding_key="SMTP_PASSWORD",
            context="sellyouroutboard",
            instance=binding_instance,
            created_at="2026-05-05T20:00:00Z",
            updated_at="2026-05-05T20:00:00Z",
        )
    )


def _seed_every_code_rerun_intent(
    store: Any,
    *,
    source_url: str = "https://github.com/cbusillo/code/issues/123",
    context: str = "launchplane",
    idempotency_key: str = "",
    recorded_at: str = "",
    authorized: bool = True,
) -> AgentWriteIntentRecord:
    resolved_recorded_at = recorded_at or datetime.now(timezone.utc).replace(
        microsecond=0
    ).isoformat().replace("+00:00", "Z")
    request = AgentWriteIntentRequest(
        intent="every_code_rerun",
        mode="apply",
        product="launchplane",
        context=context,
        source_url=source_url,
        idempotency_key=idempotency_key,
        reason="Approved rerun for blocked Every Code request.",
    )
    audit = agent_authz_audit(
        identity=_identity(),
        action="every_code_work_request.rerun",
        product="launchplane",
        context=context,
        decision="allowed" if authorized else "denied",
        reason_code="authorized" if authorized else "authorization_denied",
        policy_source="test",
        policy_sha256="test-policy-sha256",
    )
    evaluation = evaluate_agent_write_intent(
        request=request,
        authorized=authorized,
        audit=audit,
    )
    record = AgentWriteIntentRecord(
        record_id=build_agent_write_intent_record_id(
            recorded_at=resolved_recorded_at,
            trace_id="launchplane_req_every_code_rerun_test",
            request=request,
            evaluation=evaluation,
        ),
        recorded_at=resolved_recorded_at,
        trace_id="launchplane_req_every_code_rerun_test",
        idempotency_key=idempotency_key,
        request=request,
        evaluation=evaluation,
    )
    store.write_agent_write_intent_record(record)
    return record


def _every_code_work_request_create_payload(*, issue_number: int = 123) -> dict[str, object]:
    return {
        "repository": "cbusillo/code",
        "issue_number": issue_number,
        "issue_url": f"https://github.com/cbusillo/code/issues/{issue_number}",
        "issue_title": "Wire local automation",
        "trigger_label": "every-code",
        "trigger_actor": "cbusillo",
        "source": "manual",
        "queued_at": "2026-05-05T22:00:00Z",
    }


def _every_code_pr_feedback_payload() -> dict[str, object]:
    return {
        "feedback_id": "every-code-pr-feedback-cbusillo-code-31-review-1",
        "request_id": "every-code-cbusillo-code-123-test",
        "repository": "cbusillo/code",
        "pr_number": 31,
        "pr_url": "https://github.com/cbusillo/code/pull/31",
        "feedback_kind": "pull_request_review_comment",
        "github_delivery_id": "delivery-feedback-1",
        "github_node_id": "PRRC_kwDOTest123",
        "actor": "reviewer",
        "author_association": "MEMBER",
        "body": "Please tighten the FastAPI write route coverage.",
        "html_url": "https://github.com/cbusillo/code/pull/31#discussion_r1",
        "received_at": "2026-06-18T12:07:00Z",
        "status": "pending",
    }


def _every_code_preview_gate_payload() -> dict[str, object]:
    return {
        "gate_id": "every-code-preview-gate-cbusillo-code-31-test",
        "request_id": "every-code-cbusillo-code-123-test",
        "repository": "cbusillo/code",
        "issue_number": 123,
        "issue_url": "https://github.com/cbusillo/code/issues/123",
        "pr_number": 31,
        "pr_url": "https://github.com/cbusillo/code/pull/31",
        "head_sha": "abcdef1234567890",
        "status": "ready",
        "created_at": "2026-06-18T12:05:00Z",
        "updated_at": "2026-06-18T12:06:00Z",
        "ready_at": "2026-06-18T12:06:00Z",
        "last_checked_at": "2026-06-18T12:06:00Z",
    }


def _notification_policy_apply_policy(
    *, action: str, product: str, context: str
) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": "every/verireel",
                    "workflow_refs": [
                        "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                    ],
                    "event_names": ["pull_request"],
                    "products": [product],
                    "contexts": [context],
                    "actions": [action],
                }
            ]
        }
    )


def _generic_web_preview_desired_state_policy(*, context: str) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": "cbusillo/sellyouroutboard",
                    "workflow_refs": [
                        "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml@refs/heads/main"
                    ],
                    "event_names": ["pull_request"],
                    "products": ["sellyouroutboard"],
                    "contexts": [context],
                    "actions": ["preview_desired_state.discover"],
                }
            ]
        }
    )


def _generic_web_preview_desired_state_identity() -> GitHubActionsIdentity:
    return _identity(
        repository="cbusillo/sellyouroutboard",
        workflow_ref=(
            "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml@refs/heads/main"
        ),
    )


def _runtime_key_safety_policy_apply_policy(*, action: str) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": "every/verireel",
                    "workflow_refs": [
                        "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                    ],
                    "event_names": ["pull_request"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": [action],
                }
            ]
        }
    )


def _github_human_runtime_key_safety_policy_apply_policy() -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_humans": [
                {
                    "logins": ["example-operator"],
                    "roles": ["admin"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": ["runtime_key_safety.write"],
                }
            ]
        }
    )


def _runtime_key_safety_policy_apply_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "product": "launchplane",
        "source_label": "test:runtime-key-safety-policy",
        "rules": [
            {
                "binding_key": "SMTP_PASSWORD",
                "secret_class": "prod_only",
                "allowed_contexts": ["sellyouroutboard"],
                "allowed_instances": ["prod"],
            },
            {
                "binding_key": "RESEND_API_KEY",
                "secret_class": "prod_only",
                "allowed_contexts": ["sellyouroutboard"],
                "allowed_instances": ["prod"],
            },
        ],
    }


def _github_human_ingress_route_policy(
    *, action: str, product: str, context: str
) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_humans": [
                {
                    "logins": ["example-operator"],
                    "roles": ["admin"],
                    "products": [product],
                    "contexts": [context],
                    "actions": [action],
                }
            ]
        }
    )


def _public_ingress_notification_policy_record(
    *, policy_id: str = "public-ingress-notification-launchplane"
) -> PublicIngressNotificationPolicyRecord:
    return PublicIngressNotificationPolicyRecord(
        policy_id=policy_id,
        product="launchplane",
        context="launchplane",
        status="enabled",
        created_at="2026-05-29T12:00:00Z",
        updated_at="2026-05-29T12:00:00Z",
        source="test",
        destinations=(
            PublicIngressNotificationDestination(
                destination_id="discord",
                kind="discord",
                discord_webhook_secret="secret-discord-webhook",
            ),
        ),
    )


def _every_code_notification_policy_record(
    *, policy_id: str = "every-code-notification-launchplane"
) -> EveryCodeNotificationPolicyRecord:
    return EveryCodeNotificationPolicyRecord(
        policy_id=policy_id,
        repository="cbusillo/code",
        status="enabled",
        created_at="2026-06-14T18:00:00Z",
        updated_at="2026-06-14T18:00:00Z",
        source="test",
        destinations=(
            EveryCodeNotificationDestination(
                destination_id="discord",
                kind="discord",
                discord_webhook_secret="secret-discord-webhook",
            ),
        ),
    )


def _preview_pr_feedback_notification_policy_record(
    *,
    policy_id: str = "preview-pr-feedback-notification-syo",
    product: str = "sellyouroutboard",
    context: str = "sellyouroutboard",
    repository: str = "cbusillo/sellyouroutboard",
) -> PreviewPrFeedbackNotificationPolicyRecord:
    return PreviewPrFeedbackNotificationPolicyRecord(
        policy_id=policy_id,
        product=product,
        context=context,
        repository=repository,
        status="enabled",
        created_at="2026-06-15T17:10:00Z",
        updated_at="2026-06-15T17:10:00Z",
        source="test",
        destinations=(
            PreviewPrFeedbackNotificationDestination(
                destination_id="discord",
                kind="discord",
                discord_webhook_secret="secret-discord-webhook",
            ),
        ),
    )


def _preview_pr_feedback_identity() -> GitHubActionsIdentity:
    return _identity(
        repository="every/verireel",
        workflow_ref=(
            "every/verireel/.github/workflows/preview-control-plane.yml@refs/pull/42/merge"
        ),
        event_name="pull_request",
    )


def _preview_pr_feedback_policy(*, action: str) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": "every/verireel",
                    "workflow_refs": [
                        "every/verireel/.github/workflows/preview-control-plane.yml@refs/pull/42/merge"
                    ],
                    "event_names": ["pull_request"],
                    "products": ["verireel"],
                    "contexts": ["verireel-testing"],
                    "actions": [action],
                }
            ]
        }
    )


def _preview_pr_feedback_payload(
    *,
    status: str = "ready",
    context: str | None = "verireel-testing",
    dry_run: bool = False,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "product": "verireel",
        "source": "preview-control-plane",
        "repository": "every/verireel",
        "anchor_repo": "verireel",
        "anchor_pr_number": 42,
        "anchor_pr_url": "https://github.com/every/verireel/pull/42",
        "status": status,
        "preview_url": "https://pr-42.preview.example",
        "immutable_image_reference": "ghcr.io/every/verireel:pr-42-a1b2c3d4",
        "refresh_image_reference": "ghcr.io/every/verireel:preview-pr-42",
        "revision": "a1b2c3d4",
        "run_url": "https://github.com/every/verireel/actions/runs/123",
    }
    if context is not None:
        payload["context"] = context
    if dry_run:
        payload["dry_run"] = True
    return payload


def _ingress_route_audit_record(
    *,
    record_id: str = "ingress-route-audit-test",
    product: str = "launchplane",
    context: str = "reon-prod",
    mode: Literal["dry-run", "apply"] = "dry-run",
    status: Literal["pending", "planned", "applied", "unchanged"] = "planned",
    dry_run: bool = True,
    provider_host_id: int | None = 78,
    trace_id: str = "trace-audit-1",
    idempotency_key: str = "audit-key-1",
    recorded_at: str = "2026-06-01T00:00:00Z",
) -> IngressRouteAuditRecord:
    return IngressRouteAuditRecord(
        record_id=record_id,
        product=product,
        context=context,
        mode=mode,
        status=status,
        dry_run=dry_run,
        requested_domains=("app.example.com",),
        edge_endpoint_key="edge-app",
        expected_host_id=None,
        provider_host_id=provider_host_id,
        operations=(
            IngressRouteAuditOperation(
                action="create",
                host_id=provider_host_id,
                domain_names=("app.example.com",),
                requires_apply=mode == "dry-run",
                change_categories=("create",),
            ),
        ),
        trace_id=trace_id,
        idempotency_key=idempotency_key,
        reason="test",
        recorded_at=recorded_at,
    )


def _ingress_route_audit_read_policy(*, contexts: tuple[str, ...]) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": "every/verireel",
                    "workflow_refs": [
                        "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                    ],
                    "event_names": ["pull_request"],
                    "products": ["launchplane"],
                    "contexts": list(contexts),
                    "actions": ["ingress_route.plan"],
                }
            ]
        }
    )


def _product_profile_read_policy(*, product: str) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": "every/verireel",
                    "workflow_refs": [
                        "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                    ],
                    "event_names": ["pull_request"],
                    "products": [product],
                    "contexts": ["launchplane"],
                    "actions": ["product_profile.read"],
                }
            ]
        }
    )


def _product_profile_write_policy(*, product: str) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": "every/verireel",
                    "workflow_refs": [
                        "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                    ],
                    "event_names": ["pull_request"],
                    "products": [product],
                    "contexts": ["launchplane"],
                    "actions": ["product_profile.write"],
                }
            ]
        }
    )


def _product_expected_config_policy(*, product: str = "sellyouroutboard") -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": "every/verireel",
                    "workflow_refs": [
                        "every/verireel/.github/workflows/product-expected-config.yml@refs/heads/main"
                    ],
                    "event_names": ["workflow_dispatch"],
                    "products": [product],
                    "contexts": ["launchplane"],
                    "actions": ["product_profile.expected_config.apply"],
                }
            ]
        }
    )


def _product_preview_tls_policy() -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": "every/verireel",
                    "workflow_refs": [
                        "every/verireel/.github/workflows/product-preview-tls.yml@refs/heads/main"
                    ],
                    "event_names": ["workflow_dispatch"],
                    "products": ["odoo-product"],
                    "contexts": ["launchplane"],
                    "actions": ["product_profile.preview_tls.apply"],
                }
            ]
        }
    )


def _product_config_policy(
    *,
    action: str,
    product: str = "sellyouroutboard",
    context: str = "sellyouroutboard-prod",
) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": "every/verireel",
                    "workflow_refs": [
                        "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                    ],
                    "event_names": ["pull_request"],
                    "products": [product],
                    "contexts": [context],
                    "actions": [action],
                }
            ]
        }
    )


def _github_human_product_config_policy(
    *,
    action: str,
    product: str = "sellyouroutboard",
    context: str = "sellyouroutboard",
    role: str = "admin",
) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_humans": [
                {
                    "logins": ["example-operator"],
                    "roles": [role],
                    "products": [product],
                    "contexts": [context],
                    "actions": [action],
                }
            ]
        }
    )


def _local_operator_product_environment_read_policy(*, context: str) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "local_operators": [
                {
                    "subjects": ["local-owner-agent"],
                    "token_labels": ["local-owner-read"],
                    "products": ["example-site"],
                    "contexts": [context],
                    "actions": ["product_environment.read"],
                }
            ]
        }
    )


def _local_operator_launchplane_service_read_policy() -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "local_operators": [
                {
                    "subjects": ["local-owner-agent"],
                    "token_labels": ["local-owner-read"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": ["launchplane_service.read"],
                }
            ]
        }
    )


def _local_operator_launchplane_service_reconcile_policy() -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "local_operators": [
                {
                    "subjects": ["local-owner-agent"],
                    "token_labels": ["local-owner-write"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": ["launchplane_service.reconcile_odoo_workers"],
                }
            ]
        }
    )


def _github_actions_launchplane_service_reconcile_policy() -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": "every/verireel",
                    "workflow_refs": [
                        "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                    ],
                    "event_names": ["pull_request"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": ["launchplane_service.reconcile_odoo_workers"],
                }
            ]
        }
    )


def _pending_odoo_stable_bootstrap_record() -> OdooStableBootstrapOperationRecord:
    return OdooStableBootstrapOperationRecord.model_validate(
        {
            "operation_id": "bootstrap-cm-testing",
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


def _stale_odoo_stable_bootstrap_record() -> OdooStableBootstrapOperationRecord:
    return OdooStableBootstrapOperationRecord.model_validate(
        {
            "operation_id": "bootstrap-cm-testing",
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
            "status": "running",
            "phase": "created",
            "created_at": "2026-05-17T00:00:00Z",
            "updated_at": "2026-05-17T00:01:00Z",
            "started_at": "2026-05-17T00:01:00Z",
            "lease_owner": "old-worker",
            "lease_expires_at": "2000-01-01T00:00:00Z",
            "heartbeat_at": "2000-01-01T00:00:00Z",
            "attempt": 1,
        }
    )


def _running_odoo_stable_bootstrap_record() -> OdooStableBootstrapOperationRecord:
    return OdooStableBootstrapOperationRecord.model_validate(
        {
            "operation_id": "operation-cm-testing",
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
            "status": "running",
            "phase": "running",
            "created_at": "2026-05-17T00:00:00Z",
            "updated_at": "2026-05-17T00:01:00Z",
            "started_at": "2026-05-17T00:01:00Z",
        }
    )


def _running_odoo_target_replacement_record() -> OdooStableTargetReplacementOperationRecord:
    return OdooStableTargetReplacementOperationRecord.model_validate(
        {
            "operation_id": "operation-cm-testing",
            "product": "odoo-tenant-cm",
            "context": "cm",
            "instance": "testing",
            "idempotency_key": "apply-cm-testing",
            "request_fingerprint": "fingerprint-123",
            "request": {
                "schema_version": 1,
                "product": "odoo-tenant-cm",
                "instance": "testing",
                "strategy": "recreate-in-place",
                "allow_empty_data": False,
            },
            "status": "running",
            "phase": "running",
            "created_at": "2026-05-17T00:00:00Z",
            "updated_at": "2026-05-17T00:01:00Z",
            "started_at": "2026-05-17T00:01:00Z",
        }
    )


def _odoo_operation_status_identity() -> GitHubActionsIdentity:
    return _identity(
        repository="cbusillo/launchplane",
        workflow_ref=(
            "cbusillo/launchplane/.github/workflows/odoo-operation-status.yml@refs/heads/main"
        ),
        event_name="workflow_dispatch",
    )


def _odoo_operation_status_policy(
    *,
    action: str = "",
    actions: tuple[str, ...] = (),
    products: tuple[str, ...] = ("odoo-tenant-cm",),
    contexts: tuple[str, ...] = ("cm",),
) -> LaunchplaneAuthzPolicy:
    resolved_actions = actions or (action,)
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": "cbusillo/launchplane",
                    "workflow_refs": [
                        "cbusillo/launchplane/.github/workflows/odoo-operation-status.yml@refs/heads/main"
                    ],
                    "event_names": ["workflow_dispatch"],
                    "products": list(products),
                    "contexts": list(contexts),
                    "actions": list(resolved_actions),
                }
            ]
        }
    )


def _terminal_agent_product_environment_read_policy(*, context: str) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "terminal_agents": [
                {
                    "subjects": ["local-owner-agent"],
                    "token_labels": ["local-owner-read"],
                    "products": ["example-site"],
                    "contexts": [context],
                    "actions": ["product_environment.read"],
                }
            ]
        }
    )


def _terminal_agent_launchplane_read_policy() -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "terminal_agents": [
                {
                    "subjects": ["local-owner-agent"],
                    "token_labels": ["local-owner-read"],
                    "products": ["launchplane", "example-site"],
                    "contexts": ["launchplane", "example-site"],
                    "actions": ["product_environment.read"],
                }
            ]
        }
    )


def _terminal_agent_launchplane_service_reconcile_policy() -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "terminal_agents": [
                {
                    "subjects": ["local-owner-agent"],
                    "token_labels": ["local-owner-read"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": ["launchplane_service.reconcile_odoo_workers"],
                }
            ]
        }
    )


def _local_operator_artifact_protection_policy(
    *,
    products: tuple[str, ...] = ("*",),
    contexts: tuple[str, ...] = ("*",),
) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "local_operators": [
                {
                    "subjects": ["local-owner-agent"],
                    "token_labels": ["local-owner-read"],
                    "products": list(products),
                    "contexts": list(contexts),
                    "actions": ["artifact_protection.read"],
                }
            ]
        }
    )


def _github_human_artifact_protection_policy(
    *,
    products: tuple[str, ...] = ("*",),
    contexts: tuple[str, ...] = ("*",),
) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_humans": [
                {
                    "logins": ["example-operator"],
                    "roles": ["admin"],
                    "products": list(products),
                    "contexts": list(contexts),
                    "actions": ["artifact_protection.read"],
                }
            ]
        }
    )


def _github_oauth_config() -> GitHubOAuthConfig:
    return GitHubOAuthConfig(
        client_id="example-client-id",
        client_secret="example-client-secret",
        public_url="https://launchplane.example",
        session_secret="example-session-secret",
        cookie_secure=False,
    )


def _github_human_identity(*, role: Literal["read_only", "admin"] = "admin") -> GitHubHumanIdentity:
    return GitHubHumanIdentity(
        login="example-operator",
        github_id=123,
        name="Example Operator",
        email="operator@example.com",
        organizations=frozenset({"example-org"}),
        teams=frozenset({"example-org/launchplane-operators"}),
        role=role,
    )


def _browser_mutation_headers(
    session_manager: HumanSessionManager,
    session: LaunchplaneHumanSession,
) -> dict[str, str]:
    return {
        "Cookie": session_manager.session_cookie_header(session),
        **build_browser_mutation_request_headers(
            origin=session_manager.public_origin,
            csrf_token=session_manager.csrf_token(session),
        ),
    }


def _local_operator_bearer_config(*, token_label: str = "local-owner-read") -> BearerIdentityConfig:
    return BearerIdentityConfig(
        local_operator_token="local-operator-token",
        local_operator_subject="local-owner-agent",
        local_operator_token_label=token_label,
    )


type _AsgiResponse = Response


async def _get_config_status(
    app: FastAPI,
    *,
    product: str = "example-site",
    environment: str = "prod",
    authorization: str = "Bearer valid-token",
) -> _AsgiResponse:
    headers = {"Authorization": authorization} if authorization else {}
    return await _asgi_get(
        app,
        f"/v1/products/{product}/environments/{environment}/config-status",
        headers=headers,
    )


async def _get_repo_product_mapping(
    app: FastAPI,
    *,
    authorization: str = "Bearer valid-token",
) -> _AsgiResponse:
    headers = {"Authorization": authorization} if authorization else {}
    return await _asgi_get(app, "/v1/repo-product-mapping", headers=headers)


async def _get_agent_context(
    app: FastAPI,
    *,
    repository: str = "",
    authorization: str = "Bearer valid-token",
) -> _AsgiResponse:
    headers = {"Authorization": authorization} if authorization else {}
    suffix = f"?{urlencode({'repository': repository})}" if repository else ""
    return await _asgi_get(app, f"/v1/agent/context{suffix}", headers=headers)


async def _get_work_graph_snapshot(
    app: FastAPI,
    *,
    authorization: str = "Bearer valid-token",
) -> _AsgiResponse:
    headers = {"Authorization": authorization} if authorization else {}
    return await _asgi_get(app, "/v1/work-graph/snapshot", headers=headers)


async def _post_work_graph_rank(
    app: FastAPI,
    *,
    payload: dict[str, object],
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    return await _asgi_request(
        app,
        "POST",
        "/v1/work-graph/rank",
        headers=request_headers,
        payload=payload,
    )


async def _get_work_graph_issue_inbox(
    app: FastAPI,
    *,
    authorization: str = "Bearer valid-token",
) -> _AsgiResponse:
    headers = {"Authorization": authorization} if authorization else {}
    return await _asgi_get(app, "/v1/work-graph/github/issues", headers=headers)


async def _post_work_graph_issue_inbox_reconcile(
    app: FastAPI,
    *,
    payload: dict[str, object],
    authorization: str = "Bearer valid-token",
) -> _AsgiResponse:
    headers = {"Authorization": authorization} if authorization else {}
    return await _asgi_request(
        app,
        "POST",
        "/v1/work-graph/github/issues/reconcile",
        headers=headers,
        payload=payload,
    )


async def _get_every_code_summary(
    app: FastAPI,
    *,
    repository: str = "",
    issue_number: str = "",
    state: str = "",
    limit: str = "",
    offset: str = "",
    authorization: str = "Bearer valid-token",
) -> _AsgiResponse:
    headers = {"Authorization": authorization} if authorization else {}
    params = _query_params(
        repository=repository,
        issue_number=issue_number,
        state=state,
        limit=limit,
        offset=offset,
    )
    suffix = f"?{urlencode(params)}" if params else ""
    return await _asgi_get(app, f"/v1/every-code/summary{suffix}", headers=headers)


async def _get_preview_readiness(
    app: FastAPI,
    *,
    repository: str = "",
    pr_number: str = "",
    status: str = "",
    limit: str = "",
    offset: str = "",
    authorization: str = "Bearer valid-token",
) -> _AsgiResponse:
    headers = {"Authorization": authorization} if authorization else {}
    params = _query_params(
        repository=repository,
        pr_number=pr_number,
        status=status,
        limit=limit,
        offset=offset,
    )
    suffix = f"?{urlencode(params)}" if params else ""
    return await _asgi_get(app, f"/v1/previews/readiness{suffix}", headers=headers)


async def _get_every_code_work_requests(
    app: FastAPI,
    *,
    state: str = "",
    repository: str = "",
    limit: str = "",
    offset: str = "",
    authorization: str = "Bearer valid-token",
) -> _AsgiResponse:
    headers = {"Authorization": authorization} if authorization else {}
    params = _query_params(
        state=state,
        repository=repository,
        limit=limit,
        offset=offset,
    )
    suffix = f"?{urlencode(params)}" if params else ""
    return await _asgi_get(app, f"/v1/every-code/work-requests{suffix}", headers=headers)


async def _get_every_code_work_request(
    app: FastAPI,
    request_id: str,
    *,
    authorization: str = "Bearer valid-token",
) -> _AsgiResponse:
    headers = {"Authorization": authorization} if authorization else {}
    return await _asgi_get(
        app,
        f"/v1/every-code/work-requests/{request_id}",
        headers=headers,
    )


async def _post_agent_write_intent_evaluate(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/agent/write-intents/evaluate",
        headers=request_headers,
        payload=payload,
    )


async def _post_every_code_work_request_create(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/every-code/work-requests/create",
        headers=request_headers,
        payload=payload,
    )


async def _post_every_code_work_request_claim(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/every-code/work-requests/claim",
        headers=request_headers,
        payload=payload,
    )


async def _post_every_code_work_request_status(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/every-code/work-requests/status",
        headers=request_headers,
        payload=payload,
    )


async def _post_every_code_work_request_rerun(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/every-code/work-requests/rerun",
        headers=request_headers,
        payload=payload,
    )


async def _get_every_code_pr_feedback(
    app: FastAPI,
    *,
    request_id: str = "",
    repository: str = "",
    pr_number: str = "",
    status: str = "",
    limit: str = "",
    offset: str = "",
    authorization: str = "Bearer valid-token",
) -> _AsgiResponse:
    headers = {"Authorization": authorization} if authorization else {}
    params = _query_params(
        request_id=request_id,
        repository=repository,
        pr_number=pr_number,
        status=status,
        limit=limit,
        offset=offset,
    )
    suffix = f"?{urlencode(params)}" if params else ""
    return await _asgi_get(app, f"/v1/every-code/pr-feedback{suffix}", headers=headers)


async def _post_every_code_pr_feedback(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
) -> _AsgiResponse:
    headers = {"Authorization": authorization} if authorization else {}
    return await _asgi_request(
        app,
        "POST",
        "/v1/every-code/pr-feedback",
        headers=headers,
        payload=payload,
    )


async def _post_every_code_pr_feedback_status(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
) -> _AsgiResponse:
    headers = {"Authorization": authorization} if authorization else {}
    return await _asgi_request(
        app,
        "POST",
        "/v1/every-code/pr-feedback/status",
        headers=headers,
        payload=payload,
    )


async def _get_every_code_preview_gates(
    app: FastAPI,
    *,
    request_id: str = "",
    repository: str = "",
    pr_number: str = "",
    status: str = "",
    limit: str = "",
    offset: str = "",
    authorization: str = "Bearer valid-token",
) -> _AsgiResponse:
    headers = {"Authorization": authorization} if authorization else {}
    params = _query_params(
        request_id=request_id,
        repository=repository,
        pr_number=pr_number,
        status=status,
        limit=limit,
        offset=offset,
    )
    suffix = f"?{urlencode(params)}" if params else ""
    return await _asgi_get(app, f"/v1/every-code/preview-gates{suffix}", headers=headers)


async def _post_every_code_preview_gate(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
) -> _AsgiResponse:
    headers = {"Authorization": authorization} if authorization else {}
    return await _asgi_request(
        app,
        "POST",
        "/v1/every-code/preview-gates",
        headers=headers,
        payload=payload,
    )


async def _get_every_code_notification_attempts(
    app: FastAPI,
    *,
    request_id: str = "",
    event: str = "",
    destination_kind: str = "",
    limit: str = "",
    authorization: str = "Bearer valid-token",
) -> _AsgiResponse:
    headers = {"Authorization": authorization} if authorization else {}
    params = _query_params(
        request_id=request_id,
        event=event,
        destination_kind=destination_kind,
        limit=limit,
    )
    suffix = f"?{urlencode(params)}" if params else ""
    return await _asgi_get(
        app,
        f"/v1/every-code/notification-attempts{suffix}",
        headers=headers,
    )


async def _get_preview_pr_feedback_notification_attempts(
    app: FastAPI,
    *,
    feedback_id: str = "",
    event: str = "",
    destination_kind: str = "",
    limit: str = "",
    authorization: str = "Bearer valid-token",
) -> _AsgiResponse:
    headers = {"Authorization": authorization} if authorization else {}
    params = _query_params(
        feedback_id=feedback_id,
        event=event,
        destination_kind=destination_kind,
        limit=limit,
    )
    suffix = f"?{urlencode(params)}" if params else ""
    return await _asgi_get(
        app,
        f"/v1/previews/pr-feedback/notification-attempts{suffix}",
        headers=headers,
    )


async def _post_preview_pr_feedback(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
) -> _AsgiResponse:
    headers = {"Authorization": authorization} if authorization else {}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/previews/pr-feedback",
        headers=headers,
        payload=payload,
    )


async def _post_merge_train_pr_feedback(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
) -> _AsgiResponse:
    headers = {"Authorization": authorization} if authorization else {}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/work-graph/merge-train/pr-feedback",
        headers=headers,
        payload=payload,
    )


async def _post_merge_train_run_once(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
) -> _AsgiResponse:
    headers = {"Authorization": authorization} if authorization else {}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/work-graph/merge-train/run-once",
        headers=headers,
        payload=payload,
    )


async def _post_merge_train_batch_candidate_run_once(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
) -> _AsgiResponse:
    headers = {"Authorization": authorization} if authorization else {}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/work-graph/merge-train/batch-candidate/run-once",
        headers=headers,
        payload=payload,
    )


async def _post_merge_train_controller_run_once(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
) -> _AsgiResponse:
    headers = {"Authorization": authorization} if authorization else {}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/work-graph/merge-train/controller/run-once",
        headers=headers,
        payload=payload,
    )


async def _post_merge_train_batch_landing_run_once(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    capture_server_error_response: bool = False,
) -> _AsgiResponse:
    headers = {"Authorization": authorization} if authorization else {}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/work-graph/merge-train/batch-landing/run-once",
        headers=headers,
        payload=payload,
        capture_server_error_response=capture_server_error_response,
    )


async def _post_merge_train_stack_collapse_run_once(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
) -> _AsgiResponse:
    headers = {"Authorization": authorization} if authorization else {}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/work-graph/merge-train/stack-collapse/run-once",
        headers=headers,
        payload=payload,
    )


def _query_params(**values: str) -> dict[str, str]:
    return {key: value for key, value in values.items() if value != ""}


async def _get_dokploy_target_inspect(
    app: FastAPI,
    *,
    context: str = "",
    instance: str = "",
    target_type: str = "",
    target_id: str = "",
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers.setdefault("Authorization", authorization)
    params = _query_params(
        context=context,
        instance=instance,
        target_type=target_type,
        target_id=target_id,
    )
    suffix = f"?{urlencode(params)}" if params else ""
    return await _asgi_get(app, f"/v1/dokploy-targets/inspect{suffix}", headers=request_headers)


async def _post_launchplane_self_deploy(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/drivers/launchplane/self-deploy",
        headers=request_headers,
        payload=payload,
    )


async def _post_odoo_artifact_publish_inputs(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/drivers/odoo/artifact-publish-inputs",
        headers=request_headers,
        payload=payload,
    )


async def _post_odoo_artifact_publish(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/drivers/odoo/artifact-publish",
        headers=request_headers,
        payload=payload,
    )


async def _post_odoo_preview_apply_inputs(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/drivers/odoo/preview-apply-inputs",
        headers=request_headers,
        payload=payload,
    )


async def _post_odoo_preview_apply(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/drivers/odoo/preview-apply",
        headers=request_headers,
        payload=payload,
    )


async def _post_odoo_post_deploy(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/drivers/odoo/post-deploy",
        headers=request_headers,
        payload=payload,
    )


async def _post_odoo_app_maintenance(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/drivers/odoo/app-maintenance",
        headers=request_headers,
        payload=payload,
    )


async def _post_odoo_config_parameter_override(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/drivers/odoo/config-parameter-override",
        headers=request_headers,
        payload=payload,
    )


async def _post_odoo_website_bootstrap_override(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/drivers/odoo/website-bootstrap-override",
        headers=request_headers,
        payload=payload,
    )


async def _post_odoo_prod_backup_gate(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/drivers/odoo/prod-backup-gate",
        headers=request_headers,
        payload=payload,
    )


async def _post_odoo_prod_rollback(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/drivers/odoo/prod-rollback",
        headers=request_headers,
        payload=payload,
    )


async def _post_generic_web_rollback_plan(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/drivers/generic-web/prod-rollback-plan",
        headers=request_headers,
        payload=payload,
    )


async def _post_generic_web_rollback(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/drivers/generic-web/prod-rollback",
        headers=request_headers,
        payload=payload,
    )


async def _post_odoo_stable_bootstrap(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/drivers/odoo/stable-bootstrap",
        headers=request_headers,
        payload=payload,
    )


async def _post_odoo_target_replacement_plan(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/drivers/odoo/target-replacement-plan",
        headers=request_headers,
        payload=payload,
    )


async def _post_odoo_target_replacement_apply(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/drivers/odoo/target-replacement-apply",
        headers=request_headers,
        payload=payload,
    )


async def _post_odoo_prod_promotion_inputs(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/drivers/odoo/prod-promotion-inputs",
        headers=request_headers,
        payload=payload,
    )


async def _post_odoo_prod_promotion(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/drivers/odoo/prod-promotion",
        headers=request_headers,
        payload=payload,
    )


async def _post_odoo_prod_promotion_run(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/drivers/odoo/prod-promotion-run",
        headers=request_headers,
        payload=payload,
    )


async def _get_products(
    app: FastAPI,
    *,
    authorization: str = "Bearer valid-token",
) -> _AsgiResponse:
    headers = {"Authorization": authorization} if authorization else {}
    return await _asgi_get(app, "/v1/products", headers=headers)


async def _get_product(
    app: FastAPI,
    product: str = "example-site",
    *,
    authorization: str = "Bearer valid-token",
) -> _AsgiResponse:
    headers = {"Authorization": authorization} if authorization else {}
    return await _asgi_get(app, f"/v1/products/{product}", headers=headers)


async def _get_product_activity(
    app: FastAPI,
    product: str = "example-site",
    *,
    authorization: str = "Bearer valid-token",
) -> _AsgiResponse:
    headers = {"Authorization": authorization} if authorization else {}
    return await _asgi_get(app, f"/v1/products/{product}/activity", headers=headers)


async def _get_product_environments(
    app: FastAPI,
    product: str = "example-site",
    *,
    authorization: str = "Bearer valid-token",
) -> _AsgiResponse:
    headers = {"Authorization": authorization} if authorization else {}
    return await _asgi_get(app, f"/v1/products/{product}/environments", headers=headers)


async def _get_product_environment(
    app: FastAPI,
    product: str = "example-site",
    environment: str = "prod",
    *,
    authorization: str = "Bearer valid-token",
) -> _AsgiResponse:
    headers = {"Authorization": authorization} if authorization else {}
    return await _asgi_get(
        app,
        f"/v1/products/{product}/environments/{environment}",
        headers=headers,
    )


async def _get_protected_artifacts(
    app: FastAPI,
    *,
    product: str,
    context: str = "",
    authorization: str = "Bearer local-operator-token",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    params: dict[str, str] = {}
    if product:
        params["product"] = product
    if context:
        params["context"] = context
    query_string = urlencode(params)
    suffix = f"?{query_string}" if query_string else ""
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    return await _asgi_get(
        app,
        f"/v1/artifacts/protected{suffix}",
        headers=request_headers,
    )


async def _get_driver_descriptors(
    app: FastAPI,
    *,
    authorization: str = "Bearer local-operator-token",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    return await _asgi_get(app, "/v1/drivers", headers=request_headers)


async def _get_driver_descriptor(
    app: FastAPI,
    driver_id: str,
    *,
    authorization: str = "Bearer local-operator-token",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    return await _asgi_get(app, f"/v1/drivers/{driver_id}", headers=request_headers)


async def _get_driver_context_view(
    app: FastAPI,
    context: str,
    *,
    authorization: str = "Bearer local-operator-token",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    return await _asgi_get(
        app,
        f"/v1/contexts/{context}/driver-view",
        headers=request_headers,
    )


async def _get_driver_instance_view(
    app: FastAPI,
    context: str,
    instance: str,
    *,
    authorization: str = "Bearer local-operator-token",
) -> _AsgiResponse:
    headers = {"Authorization": authorization} if authorization else {}
    return await _asgi_get(
        app,
        f"/v1/contexts/{context}/instances/{instance}/driver-view",
        headers=headers,
    )


async def _get_tracked_target_logs(
    app: FastAPI,
    context: str,
    instance: str,
    *,
    lines: str = "",
    source: str = "",
    since: str = "",
    search: str = "",
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    params = {}
    if lines:
        params["lines"] = lines
    if source:
        params["source"] = source
    if since:
        params["since"] = since
    if search:
        params["search"] = search
    suffix = f"?{urlencode(params)}" if params else ""
    return await _asgi_get(
        app,
        f"/v1/contexts/{context}/instances/{instance}/logs{suffix}",
        headers=request_headers,
    )


async def _get_edge_endpoint_records(
    app: FastAPI,
    *,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
    limit: str = "",
    provider: str = "",
    status: str = "",
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    params = {}
    if limit:
        params["limit"] = limit
    if provider:
        params["provider"] = provider
    if status:
        params["status"] = status
    suffix = f"?{urlencode(params)}" if params else ""
    return await _asgi_get(
        app,
        f"/v1/edge-endpoints/records{suffix}",
        headers=request_headers,
    )


async def _get_edge_endpoint_record(
    app: FastAPI,
    endpoint_key: str,
    *,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    return await _asgi_get(
        app,
        f"/v1/edge-endpoints/records/{endpoint_key}",
        headers=request_headers,
    )


async def _get_private_health_endpoint_records(
    app: FastAPI,
    *,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
    product: str = "repairshopr-sync",
    context: str = "repairshopr-sync",
    instance: str = "",
    status: str = "",
    limit: str = "",
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    params = {}
    if product:
        params["product"] = product
    if context:
        params["context"] = context
    if instance:
        params["instance"] = instance
    if status:
        params["status"] = status
    if limit:
        params["limit"] = limit
    suffix = f"?{urlencode(params)}" if params else ""
    return await _asgi_get(
        app,
        f"/v1/private-health-endpoints/records{suffix}",
        headers=request_headers,
    )


async def _get_private_health_endpoint_record(
    app: FastAPI,
    endpoint_key: str,
    *,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
    product: str = "repairshopr-sync",
    context: str = "repairshopr-sync",
    instance: str = "prod",
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    params = {}
    if product:
        params["product"] = product
    if context:
        params["context"] = context
    if instance:
        params["instance"] = instance
    suffix = f"?{urlencode(params)}" if params else ""
    return await _asgi_get(
        app,
        f"/v1/private-health-endpoints/records/{endpoint_key}{suffix}",
        headers=request_headers,
    )


async def _get_ingress_canary_route_records(
    app: FastAPI,
    *,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
    product: str = "",
    context: str = "",
    status: str = "",
    limit: str = "",
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    params = {}
    if product:
        params["product"] = product
    if context:
        params["context"] = context
    if status:
        params["status"] = status
    if limit:
        params["limit"] = limit
    suffix = f"?{urlencode(params)}" if params else ""
    return await _asgi_get(
        app,
        f"/v1/ingress/canary-routes/records{suffix}",
        headers=request_headers,
    )


async def _get_ingress_canary_route_record(
    app: FastAPI,
    canary_key: str,
    *,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    return await _asgi_get(
        app,
        f"/v1/ingress/canary-routes/records/{canary_key}",
        headers=request_headers,
    )


def _ingress_canary_route_apply_payload(
    *,
    product: str = "launchplane",
    context: str = "reon-prod",
    canary_key: str = "ingress-canary",
    reason: str = "test canary apply",
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "product": product,
        "context": context,
        "canary_key": canary_key,
        "reason": reason,
    }


async def _get_ingress_route_audit_records(
    app: FastAPI,
    *,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
    product: str = "",
    context: str = "",
    status: str = "",
    mode: str = "",
    provider_host_id: str = "",
    trace_id: str = "",
    idempotency_key: str = "",
    limit: str = "",
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    params = _query_params(
        product=product,
        context=context,
        status=status,
        mode=mode,
        provider_host_id=provider_host_id,
        trace_id=trace_id,
        idempotency_key=idempotency_key,
        limit=limit,
    )
    suffix = f"?{urlencode(params)}" if params else ""
    return await _asgi_get(
        app,
        f"/v1/ingress/route-audits/records{suffix}",
        headers=request_headers,
    )


async def _get_ingress_route_audit_record(
    app: FastAPI,
    record_id: str,
    *,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
    product: str = "",
    context: str = "",
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    params = _query_params(product=product, context=context)
    suffix = f"?{urlencode(params)}" if params else ""
    return await _asgi_get(
        app,
        f"/v1/ingress/route-audits/records/{record_id}{suffix}",
        headers=request_headers,
    )


async def _get_deployment_record(
    app: FastAPI,
    record_id: str,
    *,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    return await _asgi_get(app, f"/v1/deployments/{record_id}", headers=request_headers)


async def _get_promotion_record(
    app: FastAPI,
    record_id: str,
    *,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    return await _asgi_get(app, f"/v1/promotions/{record_id}", headers=request_headers)


async def _get_environment_inventory(
    app: FastAPI,
    context: str,
    instance: str,
    *,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    return await _asgi_get(
        app,
        f"/v1/inventory/{context}/{instance}",
        headers=request_headers,
    )


async def _get_recent_operations(
    app: FastAPI,
    context: str,
    *,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    return await _asgi_get(
        app,
        f"/v1/contexts/{context}/operations/recent",
        headers=request_headers,
    )


async def _get_product_profiles(
    app: FastAPI,
    *,
    driver_id: str = "",
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    suffix = f"?{urlencode({'driver_id': driver_id})}" if driver_id else ""
    return await _asgi_get(app, f"/v1/product-profiles{suffix}", headers=request_headers)


async def _get_product_profile(
    app: FastAPI,
    product: str = "sellyouroutboard",
    *,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    return await _asgi_get(
        app,
        f"/v1/product-profiles/{product}",
        headers=request_headers,
    )


async def _post_product_profile(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/product-profiles",
        headers=request_headers,
        payload=payload,
    )


async def _post_product_expected_config(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/product-profiles/expected-config/apply",
        headers=request_headers,
        payload=payload,
    )


async def _post_product_preview_tls(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/product-profiles/preview-tls/apply",
        headers=request_headers,
        payload=payload,
    )


def _preview_desired_state_payload(*, label: str = "preview") -> dict[str, object]:
    return {
        "product": "verireel",
        "context": "verireel-testing",
        "source": "launchplane-preview-lifecycle",
        "repository": "every/verireel",
        "label": label,
        "anchor_repo": "verireel",
    }


async def _post_preview_desired_state(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/previews/desired-state",
        headers=request_headers,
        payload=payload,
    )


def _generic_web_preview_desired_state_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "product": "sellyouroutboard",
        "desired_state": {
            "schema_version": 1,
            "product": "sellyouroutboard",
        },
    }


async def _post_generic_web_preview_desired_state(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/drivers/generic-web/preview-desired-state",
        headers=request_headers,
        payload=payload,
    )


async def _post_product_config_apply(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
    raw_body: bytes | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/product-config/apply",
        headers=request_headers,
        payload=payload,
        raw_body=raw_body,
    )


async def _post_context_cutover_apply(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
    raw_body: bytes | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/product-profiles/context-cutover/apply",
        headers=request_headers,
        payload=payload,
        raw_body=raw_body,
    )


async def _post_legacy_context_cleanup_apply(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
    raw_body: bytes | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/product-profiles/legacy-context-cleanup/apply",
        headers=request_headers,
        payload=payload,
        raw_body=raw_body,
    )


async def _get_context_cutover_audit(
    app: FastAPI,
    *,
    product: str = "sellyouroutboard",
    source_context: str = "sellyouroutboard-testing",
    target_context: str = "sellyouroutboard",
    preview_context: str = "",
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    params = {
        "source_context": source_context,
        "target_context": target_context,
    }
    if preview_context:
        params["preview_context"] = preview_context
    return await _asgi_get(
        app,
        f"/v1/product-profiles/{product}/context-cutover-audit?{urlencode(params)}",
        headers=request_headers,
    )


async def _get_context_secret_statuses(
    app: FastAPI,
    context: str,
    *,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    return await _asgi_get(
        app,
        f"/v1/contexts/{context}/secrets",
        headers=request_headers,
    )


async def _get_instance_secret_statuses(
    app: FastAPI,
    context: str,
    instance: str,
    *,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    return await _asgi_get(
        app,
        f"/v1/contexts/{context}/instances/{instance}/secrets",
        headers=request_headers,
    )


async def _get_secret_status(
    app: FastAPI,
    secret_id: str,
    *,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    return await _asgi_get(app, f"/v1/secrets/{secret_id}", headers=request_headers)


async def _get_preview_record(
    app: FastAPI,
    preview_id: str,
    *,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    return await _asgi_get(app, f"/v1/previews/{preview_id}", headers=request_headers)


async def _get_preview_history(
    app: FastAPI,
    preview_id: str,
    *,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    return await _asgi_get(
        app,
        f"/v1/previews/{preview_id}/history",
        headers=request_headers,
    )


async def _post_backup_gate_evidence(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/evidence/backup-gates",
        headers=request_headers,
        payload=payload,
    )


async def _post_public_ingress_monitor(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/products/public-ingress-monitor/run-once",
        headers=request_headers,
        payload=payload,
    )


async def _post_promotion_evidence(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/evidence/promotions",
        headers=request_headers,
        payload=payload,
    )


async def _post_runtime_key_safety_policy_apply(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/runtime-key-safety/policies/apply",
        headers=request_headers,
        payload=payload,
    )


async def _post_preview_generation_evidence(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/evidence/previews/generations",
        headers=request_headers,
        payload=payload,
    )


async def _post_preview_destroyed_evidence(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/evidence/previews/destroyed",
        headers=request_headers,
        payload=payload,
    )


async def _post_runner_host_hygiene_audit_evidence(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/evidence/runner-host-hygiene/audits",
        headers=request_headers,
        payload=payload,
    )


async def _post_runner_lane_registration_audit_evidence(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/evidence/runner-lane-registration/audits",
        headers=request_headers,
        payload=payload,
    )


async def _post_deployment_evidence(
    app: FastAPI,
    payload: dict[str, object],
    *,
    authorization: str = "Bearer valid-token",
    idempotency_key: str = "",
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    if idempotency_key:
        request_headers["Idempotency-Key"] = idempotency_key
    return await _asgi_request(
        app,
        "POST",
        "/v1/evidence/deployments",
        headers=request_headers,
        payload=payload,
    )


async def _asgi_get(
    target: FastAPI | AsyncClient,
    path: str,
    *,
    headers: dict[str, str] | None = None,
) -> _AsgiResponse:
    return await http_get(target, path, headers=headers)


async def _asgi_request(
    target: FastAPI | AsyncClient,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    payload: dict[str, object] | None = None,
    raw_body: bytes | None = None,
    capture_server_error_response: bool = False,
) -> _AsgiResponse:
    return await http_request(
        target,
        method,
        path,
        headers=headers,
        payload=payload,
        raw_body=raw_body,
        capture_server_error_response=capture_server_error_response,
    )


class _StubFastApiGitHubOAuthClient:
    def __init__(
        self,
        identity: GitHubHumanIdentity,
        *,
        fail_fetch: bool = False,
        permission_error: bool = False,
    ) -> None:
        self.identity = identity
        self.fail_fetch = fail_fetch
        self.permission_error = permission_error
        self.authorization_state = ""
        self.code_verifier = ""

    def authorization_url(self, *, state: str, code_challenge: str) -> str:
        self.authorization_state = state
        return f"https://github.example/authorize?state={state}&challenge={code_challenge}"

    def fetch_identity(
        self,
        *,
        code: str,
        code_verifier: str,
        authz_policy: LaunchplaneAuthzPolicy,
    ) -> GitHubHumanIdentity:
        del authz_policy
        self.code_verifier = code_verifier
        if self.permission_error:
            raise PermissionError("not authorized")
        if self.fail_fetch or code != "github-code":
            raise ValueError("unexpected code")
        return self.identity


class _MissingProductReadStore:
    pass


class _MergeTrainPolicyOnlyStore:
    backend_name = "test-merge-train-policy-only"

    def __init__(self, *, state_dir: Path) -> None:
        self.delegate = FilesystemRecordStore(state_dir=state_dir)

    def close(self) -> None:
        return None

    def read_idempotency_record(
        self,
        *,
        scope: str,
        route_path: str,
        idempotency_key: str,
    ) -> Any:
        return self.delegate.read_idempotency_record(
            scope=scope,
            route_path=route_path,
            idempotency_key=idempotency_key,
        )

    def write_idempotency_record(self, record: LaunchplaneIdempotencyRecord) -> object:
        return self.delegate.write_idempotency_record(record)

    def list_merge_train_policy_records(
        self, *, status: str = "", limit: int | None = None
    ) -> tuple[MergeTrainPolicyRecord, ...]:
        return self.delegate.list_merge_train_policy_records(status=status, limit=limit)


class _PreviewLifecycleSweepProfileOnlyStore:
    backend_name = "test-preview-lifecycle-sweep-profile-only"

    def __init__(self, profile: LaunchplaneProductProfileRecord) -> None:
        self.profile = profile

    def close(self) -> None:
        return None

    def list_product_profile_records(self) -> tuple[LaunchplaneProductProfileRecord, ...]:
        return (self.profile,)


class _PreviewLifecycleCleanupPlanOnlyStore:
    backend_name = "test-preview-lifecycle-cleanup-plan-only"

    def __init__(self, *, state_dir: Path) -> None:
        self.delegate = FilesystemRecordStore(state_dir=state_dir)
        self.cleanup_records: list[PreviewLifecycleCleanupRecord] = []

    def close(self) -> None:
        return None

    def read_idempotency_record(
        self,
        *,
        scope: str,
        route_path: str,
        idempotency_key: str,
    ) -> Any:
        return self.delegate.read_idempotency_record(
            scope=scope,
            route_path=route_path,
            idempotency_key=idempotency_key,
        )

    def write_idempotency_record(self, record: LaunchplaneIdempotencyRecord) -> object:
        return self.delegate.write_idempotency_record(record)

    def list_preview_lifecycle_plan_records(
        self, *, context_name: str = "", limit: int | None = 25
    ) -> tuple[PreviewLifecyclePlanRecord, ...]:
        return self.delegate.list_preview_lifecycle_plan_records(
            context_name=context_name,
            limit=limit,
        )

    def write_preview_lifecycle_cleanup_record(
        self, record: PreviewLifecycleCleanupRecord
    ) -> object:
        self.cleanup_records.append(record)
        return f"preview-lifecycle-cleanup://{record.cleanup_id}"


class _ConcurrentProductConfigDryRunMarkerStore:
    def __init__(
        self, *, after_write: Literal["matching", "missing", "mismatched"] = "matching"
    ) -> None:
        self.after_write = after_write
        self.read_calls = 0
        self.write_calls = 0
        self._stored_record: LaunchplaneIdempotencyRecord | None = None

    def read_idempotency_record(
        self,
        *,
        scope: str,
        route_path: str,
        idempotency_key: str,
    ) -> LaunchplaneIdempotencyRecord | None:
        self.read_calls += 1
        if self._stored_record is None:
            return None
        if (
            self._stored_record.scope != scope
            or self._stored_record.route_path != route_path
            or self._stored_record.idempotency_key != idempotency_key
        ):
            return None
        return self._stored_record

    def write_idempotency_record(self, record: LaunchplaneIdempotencyRecord) -> None:
        self.write_calls += 1
        if self.after_write == "matching":
            self._stored_record = record
        elif self.after_write == "mismatched":
            self._stored_record = record.model_copy(
                update={"request_fingerprint": f"mismatched-{record.request_fingerprint}"}
            )
        else:
            self._stored_record = None
        raise RuntimeError("simulated duplicate dry-run marker write")


class _EmptyStore:
    backend_name = "test-empty"

    def close(self) -> None:
        return None


class _SecretStatusProbeStore:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def read_secret_record(self, secret_id: str) -> object:
        self.calls.append(f"read_secret_record:{secret_id}")
        raise FileNotFoundError(secret_id)

    def list_secret_records(
        self,
        *,
        integration: str = "",
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[object, ...]:
        del integration, context_name, instance_name, limit
        self.calls.append("list_secret_records")
        return ()

    def read_secret_version(self, version_id: str) -> object:
        self.calls.append(f"read_secret_version:{version_id}")
        raise FileNotFoundError(version_id)

    def list_secret_versions(self, *, secret_id: str) -> tuple[object, ...]:
        self.calls.append(f"list_secret_versions:{secret_id}")
        return ()

    def list_secret_bindings(
        self,
        *,
        integration: str = "",
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[object, ...]:
        del integration, context_name, instance_name, limit
        self.calls.append("list_secret_bindings")
        return ()

    def list_secret_audit_events(self, *, secret_id: str) -> tuple[object, ...]:
        self.calls.append(f"list_secret_audit_events:{secret_id}")
        return ()


class _RecentOperationsProbeStore:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def list_environment_inventory(self) -> tuple[EnvironmentInventory, ...]:
        self.calls.append("list_environment_inventory")
        return ()

    def list_deployment_records(
        self,
        *,
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[DeploymentRecord, ...]:
        del context_name, instance_name, limit
        self.calls.append("list_deployment_records")
        return ()

    def list_promotion_records(
        self,
        *,
        context_name: str = "",
        from_instance_name: str = "",
        to_instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[PromotionRecord, ...]:
        del context_name, from_instance_name, to_instance_name, limit
        self.calls.append("list_promotion_records")
        return ()

    def list_preview_records(
        self,
        *,
        context_name: str = "",
        anchor_repo: str = "",
        anchor_pr_number: int | None = None,
        limit: int | None = None,
    ) -> tuple[PreviewRecord, ...]:
        del context_name, anchor_repo, anchor_pr_number, limit
        self.calls.append("list_preview_records")
        return ()


class _PreviewRecordOnlyStore:
    def __init__(self, record: PreviewRecord) -> None:
        self._record = record

    def read_preview_record(self, preview_id: str) -> PreviewRecord:
        if preview_id != self._record.preview_id:
            raise FileNotFoundError(f"No preview record found for {preview_id}.")
        return self._record


class _PreviewHistoryProbeStore:
    def __init__(self, record: PreviewRecord) -> None:
        self._record = record
        self.list_preview_generation_calls = 0

    def read_preview_record(self, preview_id: str) -> PreviewRecord:
        if preview_id != self._record.preview_id:
            raise FileNotFoundError(f"No preview record found for {preview_id}.")
        return self._record

    def list_preview_generation_records(
        self,
        *,
        preview_id: str = "",
        limit: int | None = None,
    ) -> tuple[PreviewGenerationRecord, ...]:
        del preview_id, limit
        self.list_preview_generation_calls += 1
        return ()


class _BackupGateEvidenceOnlyStore:
    def __init__(self) -> None:
        self.backup_gate_records: dict[str, dict[str, Any]] = {}

    def write_backup_gate_record(self, record: BackupGateRecord) -> None:
        self.backup_gate_records[record.record_id] = record.model_dump(mode="json")


class _IdempotencyOnlyBackupGateReplayStore:
    def __init__(self) -> None:
        self.read_idempotency_calls = 0
        self.write_backup_gate_calls = 0
        self._stored_record: Any | None = None
        self.write_backup_gate_record: Callable[[BackupGateRecord], None] | None = (
            self._write_backup_gate_record
        )

    def read_idempotency_record(
        self,
        *,
        scope: str,
        route_path: str,
        idempotency_key: str,
    ) -> Any:
        self.read_idempotency_calls += 1
        if self._stored_record is None:
            return None
        if (
            self._stored_record.scope != scope
            or self._stored_record.route_path != route_path
            or self._stored_record.idempotency_key != idempotency_key
        ):
            return None
        return self._stored_record

    def write_idempotency_record(self, record: Any) -> None:
        self._stored_record = record

    def _write_backup_gate_record(self, record: BackupGateRecord) -> None:
        self.write_backup_gate_calls += 1


class _PublicIngressMonitorIdempotencyReplayStore:
    def __init__(self, *, payload: dict[str, object], idempotency_key: str) -> None:
        self.read_idempotency_calls = 0
        self._payload = payload
        self._idempotency_key = idempotency_key

    def read_idempotency_record(
        self,
        *,
        scope: str,
        route_path: str,
        idempotency_key: str,
    ) -> LaunchplaneIdempotencyRecord | None:
        self.read_idempotency_calls += 1
        if (
            route_path != "/v1/products/public-ingress-monitor/run-once"
            or idempotency_key != self._idempotency_key
        ):
            return None
        return LaunchplaneIdempotencyRecord(
            record_id="idempotency-launchplane_req_original",
            scope=scope,
            route_path=route_path,
            idempotency_key=idempotency_key,
            request_fingerprint=hashlib.sha256(
                json.dumps(self._payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
            response_status_code=202,
            response_trace_id="launchplane_req_original",
            recorded_at="2026-05-29T12:00:00Z",
            response_payload={
                "status": "accepted",
                "trace_id": "launchplane_req_original",
                "records": {},
                "result": {"target_count": 1},
            },
        )

    def write_idempotency_record(self, record: LaunchplaneIdempotencyRecord) -> None:
        raise AssertionError("idempotent replay must not write a new record")


class _ProductProfileReplayOnlyStore:
    def __init__(self, *, payload: dict[str, object], idempotency_key: str) -> None:
        self.read_idempotency_calls = 0
        self._payload = payload
        self._idempotency_key = idempotency_key

    def read_idempotency_record(
        self,
        *,
        scope: str,
        route_path: str,
        idempotency_key: str,
    ) -> LaunchplaneIdempotencyRecord | None:
        self.read_idempotency_calls += 1
        if route_path != "/v1/product-profiles" or idempotency_key != self._idempotency_key:
            return None
        return LaunchplaneIdempotencyRecord(
            record_id="idempotency-launchplane_req_original",
            scope=scope,
            route_path=route_path,
            idempotency_key=idempotency_key,
            request_fingerprint=hashlib.sha256(
                json.dumps(self._payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
            response_status_code=202,
            response_trace_id="launchplane_req_original",
            recorded_at="2026-05-29T12:00:00Z",
            response_payload={
                "status": "accepted",
                "trace_id": "launchplane_req_original",
                "records": {"product_profile": "sellyouroutboard"},
            },
        )

    def write_idempotency_record(self, record: LaunchplaneIdempotencyRecord) -> None:
        raise AssertionError("idempotent replay must not write a new record")


class _EveryCodeClaimReplayOnlyStore:
    def __init__(self, *, payload: dict[str, object], idempotency_key: str) -> None:
        self.read_idempotency_calls = 0
        self.claim_calls = 0
        self._payload = payload
        self._idempotency_key = idempotency_key

    def read_idempotency_record(
        self,
        *,
        scope: str,
        route_path: str,
        idempotency_key: str,
    ) -> LaunchplaneIdempotencyRecord | None:
        self.read_idempotency_calls += 1
        if (
            route_path != "/v1/every-code/work-requests/claim"
            or idempotency_key != self._idempotency_key
        ):
            return None
        return LaunchplaneIdempotencyRecord(
            record_id="idempotency-launchplane_req_original",
            scope=scope,
            route_path=route_path,
            idempotency_key=idempotency_key,
            request_fingerprint=hashlib.sha256(
                json.dumps(self._payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
            response_status_code=202,
            response_trace_id="launchplane_req_original",
            recorded_at="2026-05-29T12:00:00Z",
            response_payload={
                "status": "accepted",
                "trace_id": "launchplane_req_original",
                "records": {
                    "request_id": "every-code-cbusillo-code-123-test",
                    "state": "claimed",
                },
                "result": {
                    "request": {
                        "request_id": "every-code-cbusillo-code-123-test",
                        "state": "claimed",
                        "claimed_by_host": "Runner-Host",
                    }
                },
            },
        )

    def write_idempotency_record(self, record: LaunchplaneIdempotencyRecord) -> None:
        raise AssertionError("idempotent replay must not write a new record")

    def claim_every_code_work_request_record(
        self,
        *,
        request_id: str,
        host: str,
        claimed_at: str,
    ) -> EveryCodeWorkRequestRecord | None:
        del request_id, host, claimed_at
        self.claim_calls += 1
        raise AssertionError("idempotent replay must not claim a record")


class _EveryCodeStatusReplayOnlyStore:
    def __init__(self, *, payload: dict[str, object], idempotency_key: str) -> None:
        self.read_idempotency_calls = 0
        self.read_calls = 0
        self.write_calls = 0
        self._payload = payload
        self._idempotency_key = idempotency_key

    def read_idempotency_record(
        self,
        *,
        scope: str,
        route_path: str,
        idempotency_key: str,
    ) -> LaunchplaneIdempotencyRecord | None:
        self.read_idempotency_calls += 1
        if (
            route_path != "/v1/every-code/work-requests/status"
            or idempotency_key != self._idempotency_key
        ):
            return None
        return LaunchplaneIdempotencyRecord(
            record_id="idempotency-launchplane_req_original",
            scope=scope,
            route_path=route_path,
            idempotency_key=idempotency_key,
            request_fingerprint=hashlib.sha256(
                json.dumps(self._payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
            response_status_code=202,
            response_trace_id="launchplane_req_original",
            recorded_at="2026-05-29T12:00:00Z",
            response_payload={
                "status": "accepted",
                "trace_id": "launchplane_req_original",
                "records": {
                    "request_id": "every-code-cbusillo-code-123-test",
                    "state": "done",
                },
                "result": {
                    "request": {
                        "request_id": "every-code-cbusillo-code-123-test",
                        "state": "done",
                        "result_pr_url": "https://github.com/cbusillo/code/pull/26",
                    },
                    "notifications": [],
                },
            },
        )

    def write_idempotency_record(self, record: LaunchplaneIdempotencyRecord) -> None:
        raise AssertionError("idempotent replay must not write a new record")

    def read_every_code_work_request_record(self, request_id: str) -> EveryCodeWorkRequestRecord:
        del request_id
        self.read_calls += 1
        raise AssertionError("idempotent replay must not read a work request")

    def write_every_code_work_request_record(self, record: EveryCodeWorkRequestRecord) -> None:
        del record
        self.write_calls += 1
        raise AssertionError("idempotent replay must not write a work request")


class _EveryCodeRerunReplayOnlyStore:
    def __init__(self, *, payload: dict[str, object], idempotency_key: str) -> None:
        self.read_idempotency_calls = 0
        self.read_calls = 0
        self.write_calls = 0
        self._payload = payload
        self._idempotency_key = idempotency_key

    def read_idempotency_record(
        self,
        *,
        scope: str,
        route_path: str,
        idempotency_key: str,
    ) -> LaunchplaneIdempotencyRecord | None:
        self.read_idempotency_calls += 1
        if (
            route_path != "/v1/every-code/work-requests/rerun"
            or idempotency_key != self._idempotency_key
        ):
            return None
        return LaunchplaneIdempotencyRecord(
            record_id="idempotency-launchplane_req_original",
            scope=scope,
            route_path=route_path,
            idempotency_key=idempotency_key,
            request_fingerprint=hashlib.sha256(
                json.dumps(self._payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
            response_status_code=202,
            response_trace_id="launchplane_req_original",
            recorded_at="2026-05-29T12:00:00Z",
            response_payload={
                "status": "accepted",
                "trace_id": "launchplane_req_original",
                "records": {
                    "request_id": "every-code-cbusillo-code-123-test",
                    "state": "queued",
                    "agent_write_intent_record_id": "agent-write-intent-test",
                },
                "result": {
                    "request": {
                        "request_id": "every-code-cbusillo-code-123-test",
                        "state": "queued",
                        "trigger_actor": "cbusillo",
                    }
                },
            },
        )

    def write_idempotency_record(self, record: LaunchplaneIdempotencyRecord) -> None:
        del record
        raise AssertionError("idempotent replay must not write a new record")

    def read_every_code_work_request_record(self, request_id: str) -> EveryCodeWorkRequestRecord:
        del request_id
        self.read_calls += 1
        raise AssertionError("idempotent replay must not read a work request")

    def write_every_code_work_request_record(self, record: EveryCodeWorkRequestRecord) -> None:
        del record
        self.write_calls += 1
        raise AssertionError("idempotent replay must not write a work request")

    def read_agent_write_intent_record(self, record_id: str) -> AgentWriteIntentRecord:
        del record_id
        raise AssertionError("idempotent replay must not read write-intent evidence")

    def list_agent_write_intent_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        status: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[AgentWriteIntentRecord, ...]:
        del product, context_name, status, limit, offset
        raise AssertionError("idempotent replay must not list write-intent evidence")


class _AgentWriteIntentEvaluateReplayOnlyStore:
    def __init__(self, *, payload: dict[str, object], idempotency_key: str) -> None:
        self.read_idempotency_calls = 0
        self._payload = payload
        self._idempotency_key = idempotency_key

    def read_idempotency_record(
        self,
        *,
        scope: str,
        route_path: str,
        idempotency_key: str,
    ) -> LaunchplaneIdempotencyRecord | None:
        self.read_idempotency_calls += 1
        if (
            route_path != "/v1/agent/write-intents/evaluate"
            or idempotency_key != self._idempotency_key
        ):
            return None
        return LaunchplaneIdempotencyRecord(
            record_id="idempotency-launchplane_req_original",
            scope=scope,
            route_path=route_path,
            idempotency_key=idempotency_key,
            request_fingerprint=hashlib.sha256(
                json.dumps(self._payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
            response_status_code=202,
            response_trace_id="launchplane_req_original",
            recorded_at="2026-05-29T12:00:00Z",
            response_payload={
                "status": "accepted",
                "trace_id": "launchplane_req_original",
                "records": {},
                "result": {
                    "intent": {
                        "status": "allowed",
                        "intent": "every_code_rerun",
                    },
                    "record": {
                        "record_id": "agent-write-intent-original",
                        "recorded_at": "2026-05-29T12:00:00Z",
                    },
                },
            },
        )

    def write_idempotency_record(self, record: LaunchplaneIdempotencyRecord) -> None:
        del record
        raise AssertionError("idempotent replay must not write a new record")

    def write_agent_write_intent_record(self, record: AgentWriteIntentRecord) -> None:
        del record
        raise AssertionError("idempotent replay must not write write-intent evidence")


class _ProductContextApplyReplayOnlyStore:
    def __init__(
        self,
        *,
        route_path: str,
        payload: dict[str, object],
        idempotency_key: str,
    ) -> None:
        self.read_idempotency_calls = 0
        self._route_path = route_path
        self._payload = payload
        self._idempotency_key = idempotency_key

    def read_idempotency_record(
        self,
        *,
        scope: str,
        route_path: str,
        idempotency_key: str,
    ) -> LaunchplaneIdempotencyRecord | None:
        self.read_idempotency_calls += 1
        if route_path != self._route_path or idempotency_key != self._idempotency_key:
            return None
        return LaunchplaneIdempotencyRecord(
            record_id="idempotency-launchplane_req_original",
            scope=scope,
            route_path=route_path,
            idempotency_key=idempotency_key,
            request_fingerprint=hashlib.sha256(
                json.dumps(self._payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
            response_status_code=202,
            response_trace_id="launchplane_req_original",
            recorded_at="2026-05-29T12:00:00Z",
            response_payload={
                "status": "accepted",
                "trace_id": "launchplane_req_original",
                "records": {"product_profile": "sellyouroutboard"},
            },
        )

    def write_idempotency_record(self, record: LaunchplaneIdempotencyRecord) -> None:
        raise AssertionError("idempotent replay must not write a new record")


class _IdempotencyOnlyRunnerHostHygieneAuditReplayStore:
    def __init__(self) -> None:
        self.read_idempotency_calls = 0
        self.write_runner_host_hygiene_audit_calls = 0
        self._stored_record: Any | None = None
        self.write_runner_host_hygiene_audit_record: (
            Callable[[RunnerHostHygieneApplyAuditRecord], None] | None
        ) = self._write_runner_host_hygiene_audit_record

    def read_idempotency_record(
        self,
        *,
        scope: str,
        route_path: str,
        idempotency_key: str,
    ) -> Any:
        self.read_idempotency_calls += 1
        if self._stored_record is None:
            return None
        if (
            self._stored_record.scope != scope
            or self._stored_record.route_path != route_path
            or self._stored_record.idempotency_key != idempotency_key
        ):
            return None
        return self._stored_record

    def write_idempotency_record(self, record: Any) -> None:
        self._stored_record = record

    def _write_runner_host_hygiene_audit_record(
        self,
        record: RunnerHostHygieneApplyAuditRecord,
    ) -> None:
        self.write_runner_host_hygiene_audit_calls += 1


class _IdempotencyOnlyRunnerLaneRegistrationAuditReplayStore:
    def __init__(self) -> None:
        self.read_idempotency_calls = 0
        self.write_runner_lane_registration_audit_calls = 0
        self._stored_record: Any | None = None
        self.write_runner_lane_registration_audit_record: (
            Callable[[RunnerLaneRegistrationAuditRecord], None] | None
        ) = self._write_runner_lane_registration_audit_record

    def read_idempotency_record(
        self,
        *,
        scope: str,
        route_path: str,
        idempotency_key: str,
    ) -> Any:
        self.read_idempotency_calls += 1
        if self._stored_record is None:
            return None
        if (
            self._stored_record.scope != scope
            or self._stored_record.route_path != route_path
            or self._stored_record.idempotency_key != idempotency_key
        ):
            return None
        return self._stored_record

    def write_idempotency_record(self, record: Any) -> None:
        self._stored_record = record

    def _write_runner_lane_registration_audit_record(
        self,
        record: RunnerLaneRegistrationAuditRecord,
    ) -> None:
        self.write_runner_lane_registration_audit_calls += 1


class _PromotionEvidenceOnlyStore:
    def __init__(self) -> None:
        self.promotion_records: dict[str, dict[str, Any]] = {}

    def write_promotion_record(self, record: PromotionRecord) -> None:
        self.promotion_records[record.record_id] = record.model_dump(mode="json")


class _IdempotencyOnlyPromotionReplayStore:
    def __init__(self) -> None:
        self.read_idempotency_calls = 0
        self.write_promotion_calls = 0
        self._stored_record: Any | None = None
        self.write_promotion_record: Callable[[PromotionRecord], None] | None = (
            self._write_promotion_record
        )

    def read_idempotency_record(
        self,
        *,
        scope: str,
        route_path: str,
        idempotency_key: str,
    ) -> Any:
        self.read_idempotency_calls += 1
        if self._stored_record is None:
            return None
        if (
            self._stored_record.scope != scope
            or self._stored_record.route_path != route_path
            or self._stored_record.idempotency_key != idempotency_key
        ):
            return None
        return self._stored_record

    def write_idempotency_record(self, record: Any) -> None:
        self._stored_record = record

    def _write_promotion_record(self, record: PromotionRecord) -> None:
        self.write_promotion_calls += 1


class _IdempotencyOnlyPreviewGenerationReplayStore:
    def __init__(self) -> None:
        self.read_idempotency_calls = 0
        self.write_preview_generation_evidence_calls = 0
        self._stored_record: Any | None = None
        self._preview_records: dict[str, Any] = {}
        self._generation_records: dict[str, Any] = {}
        self.write_preview_generation_evidence_records: Callable[..., tuple[str, str]] | None = (
            self._write_preview_generation_evidence_records
        )

    def read_idempotency_record(
        self,
        *,
        scope: str,
        route_path: str,
        idempotency_key: str,
    ) -> Any:
        self.read_idempotency_calls += 1
        if self._stored_record is None:
            return None
        if (
            self._stored_record.scope != scope
            or self._stored_record.route_path != route_path
            or self._stored_record.idempotency_key != idempotency_key
        ):
            return None
        return self._stored_record

    def write_idempotency_record(self, record: Any) -> None:
        self._stored_record = record

    def list_preview_records(
        self,
        *,
        context_name: str = "",
        anchor_repo: str = "",
        anchor_pr_number: int | None = None,
        limit: int | None = None,
    ) -> tuple[Any, ...]:
        records = [
            record
            for record in self._preview_records.values()
            if (not context_name or record.context == context_name)
            and (not anchor_repo or record.anchor_repo == anchor_repo)
            and (anchor_pr_number is None or record.anchor_pr_number == anchor_pr_number)
        ]
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def write_preview_record(self, record: Any) -> str:
        self._preview_records[record.preview_id] = record
        return f"preview://{record.preview_id}"

    def list_preview_generation_records(
        self, *, preview_id: str = "", limit: int | None = None
    ) -> tuple[Any, ...]:
        records = [
            record
            for record in self._generation_records.values()
            if not preview_id or record.preview_id == preview_id
        ]
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def write_preview_generation_record(self, record: Any) -> str:
        self._generation_records[record.generation_id] = record
        return f"generation://{record.generation_id}"

    def _write_preview_generation_evidence_records(
        self,
        *,
        preview_record: Any,
        generation_record: Any,
    ) -> tuple[str, str]:
        self.write_preview_generation_evidence_calls += 1
        generation_path = self.write_preview_generation_record(generation_record)
        preview_path = self.write_preview_record(preview_record)
        return generation_path, preview_path


class _IdempotencyOnlyPreviewDestroyedReplayStore:
    def __init__(self) -> None:
        self.read_idempotency_calls = 0
        self.write_preview_record_calls = 0
        self._stored_record: Any | None = None
        self._preview_records: dict[str, Any] = {}
        self.write_preview_record: Callable[[Any], str] | None = self._write_preview_record

    def seed_preview(self, record: PreviewRecord) -> None:
        self._preview_records[record.preview_id] = record

    def read_idempotency_record(
        self,
        *,
        scope: str,
        route_path: str,
        idempotency_key: str,
    ) -> Any:
        self.read_idempotency_calls += 1
        if self._stored_record is None:
            return None
        if (
            self._stored_record.scope != scope
            or self._stored_record.route_path != route_path
            or self._stored_record.idempotency_key != idempotency_key
        ):
            return None
        return self._stored_record

    def write_idempotency_record(self, record: Any) -> None:
        self._stored_record = record

    def list_preview_records(
        self,
        *,
        context_name: str = "",
        anchor_repo: str = "",
        anchor_pr_number: int | None = None,
        limit: int | None = None,
    ) -> tuple[Any, ...]:
        records = [
            record
            for record in self._preview_records.values()
            if (not context_name or record.context == context_name)
            and (not anchor_repo or record.anchor_repo == anchor_repo)
            and (anchor_pr_number is None or record.anchor_pr_number == anchor_pr_number)
        ]
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def _write_preview_record(self, record: Any) -> str:
        self.write_preview_record_calls += 1
        self._preview_records[record.preview_id] = record
        return f"preview://{record.preview_id}"


class _FailingOnceIdempotencyPreviewGenerationStore(FilesystemRecordStore):
    def __init__(self, *, state_dir: Path) -> None:
        super().__init__(state_dir=state_dir)
        self.fail_next_idempotency_write = True

    def write_idempotency_record(self, record: LaunchplaneIdempotencyRecord) -> Path:
        if self.fail_next_idempotency_write:
            self.fail_next_idempotency_write = False
            raise RuntimeError("idempotency write failed")
        return super().write_idempotency_record(record)


class _DeploymentEvidenceOnlyStore:
    def __init__(self) -> None:
        self.deployment_records: dict[str, dict[str, Any]] = {}
        self.environment_inventories: list[dict[str, Any]] = []

    def write_deployment_record(self, record: DeploymentRecord) -> None:
        self.deployment_records[record.record_id] = record.model_dump(mode="json")

    def write_environment_inventory(self, inventory: Any) -> None:
        self.environment_inventories.append(inventory.model_dump(mode="json"))


class _IdempotencyOnlyReplayStore:
    def __init__(self) -> None:
        self.read_idempotency_calls = 0
        self.write_deployment_calls = 0
        self.write_environment_inventory_calls = 0
        self._stored_record: Any | None = None
        self.write_deployment_record: Callable[[DeploymentRecord], None] | None = (
            self._write_deployment_record
        )
        self.write_environment_inventory: Callable[[Any], None] | None = (
            self._write_environment_inventory
        )

    def read_idempotency_record(
        self,
        *,
        scope: str,
        route_path: str,
        idempotency_key: str,
    ) -> Any:
        self.read_idempotency_calls += 1
        if self._stored_record is None:
            return None
        if (
            self._stored_record.scope != scope
            or self._stored_record.route_path != route_path
            or self._stored_record.idempotency_key != idempotency_key
        ):
            return None
        return self._stored_record

    def write_idempotency_record(self, record: Any) -> None:
        self._stored_record = record

    def _write_deployment_record(self, record: DeploymentRecord) -> None:
        self.write_deployment_calls += 1

    def _write_environment_inventory(self, inventory: Any) -> None:
        self.write_environment_inventory_calls += 1


class _RejectingVerifier:
    def verify(self, token: str) -> GitHubActionsIdentity:
        raise InvalidTokenError("signature verification failed")
