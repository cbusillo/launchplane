import asyncio
import hashlib
import hmac
import io
import json
import os
import tempfile
import unittest
from collections.abc import Callable, Iterable, Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, Literal, cast
from unittest.mock import patch

from click import ClickException, Command
from click.testing import CliRunner
from a2wsgi import ASGIMiddleware
from pydantic import BaseModel, ConfigDict, Field

from control_plane.cli import main
from control_plane import live_target_runtime as control_plane_live_target_runtime
from control_plane import secrets as control_plane_secrets
from control_plane import service as control_plane_service
from control_plane.notifications import public_discord_url_error, public_url_error
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.every_code_notifications import (
    EveryCodeNotificationAttemptRecord,
    EveryCodeNotificationDestination,
    EveryCodeNotificationPolicyRecord,
)
from control_plane.contracts.every_code_preview_gate_record import EveryCodePreviewGateRecord
from control_plane.contracts.every_code_pr_feedback_record import EveryCodePrFeedbackRecord
from control_plane.contracts.every_code_work_request import (
    EveryCodeWorkRequestRecord,
    EveryCodeWorkRequestStatusUpdate,
    apply_every_code_work_request_status,
    requeue_every_code_work_request,
)
from control_plane.every_code_notifications_delivery import (
    deliver_every_code_blocked_notifications,
    deliver_every_code_discord_notification,
)
from control_plane.contracts.deployment_record import DeploymentRecord, ResolvedTargetEvidence
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.driver_descriptor import DriverActionDescriptor, DriverDescriptor
from control_plane.dokploy import DokploySourceOfTruth, DokployTargetDefinition
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.edge_endpoint_record import EdgeEndpointRecord
from control_plane.contracts.edge_endpoint_record import EdgeEndpointStatus
from control_plane.contracts.private_health_endpoint_record import PrivateHealthEndpointRecord
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.contracts.ingress_canary_route_record import IngressCanaryRouteRecord
from control_plane.contracts.merge_train_policy import MergeTrainPolicy
from control_plane.contracts.merge_train_policy import MergeTrainPolicyRecord
from control_plane.contracts.merge_train_policy import parse_merge_train_policy_toml
from control_plane.contracts.merge_train_batch import MergeTrainBatchCandidate
from control_plane.contracts.merge_train_batch import MergeTrainBatchCandidateRecord
from control_plane.contracts.merge_train_batch import MergeTrainBatchLandingPlan
from control_plane.contracts.merge_train_batch import build_merge_train_batch_candidate
from control_plane.contracts.merge_train_batch import build_merge_train_batch_candidate_record
from control_plane.merge_train_github import MergeTrainGitHubError
from control_plane.merge_train_github import MergeTrainGitHubStaleHeadError
from control_plane.contracts.merge_train_stack_collapse import (
    build_merge_train_stack_collapse_plan,
    build_merge_train_stack_collapse_plan_record,
    execute_merge_train_stack_collapse_plan,
)
from control_plane.contracts.merge_train_run_record import MergeTrainRunRecord
from control_plane.contracts.merge_train_run_record import build_merge_train_run_record
from control_plane.contracts.preview_generation_record import (
    PreviewGenerationRecord,
    PreviewPullRequestSummary,
)
from control_plane.contracts.preview_inventory_scan_record import PreviewInventoryScanRecord
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.promotion_record import (
    ArtifactIdentityReference,
    DeploymentEvidence,
    HealthcheckEvidence,
    PromotionRecord,
)
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.contracts.runtime_key_safety_policy import (
    RuntimeKeySafetyPolicyRecord,
    RuntimeSecretSafetyRule,
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
from control_plane.merge_train import MergeTrainDryRunSnapshot, MergeTrainPullRequestSnapshot
from control_plane.merge_train import MergeTrainCheckStatus
from control_plane.merge_train import build_merge_train_dry_run_result
from control_plane.merge_train import discover_merge_train_stack
from control_plane.service import (
    GenericWebPreviewVerificationRequest,
    create_launchplane_service_app as _create_launchplane_service_app,
    handle_every_code_github_webhook_request,
)
from control_plane.http_app import LaunchplaneAuthzPolicyRuntime
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.drivers import generic_web_preview_dispatch
from control_plane.service_auth import (
    GitHubActionsIdentity,
    GitHubHumanIdentity,
    LaunchplaneAuthzPolicy,
    LocalAdminIdentity,
    LocalAdminPolicyRule,
    LocalOperatorIdentity,
    LocalOperatorPolicyRule,
    TerminalAgentIdentity,
)
from control_plane.service_human_auth import (
    GITHUB_EMAILS_URL,
    GITHUB_ORGS_URL,
    GITHUB_TEAMS_URL,
    GITHUB_USER_URL,
    GitHubOAuthClient,
    GitHubOAuthConfig,
    HumanSessionManager,
    HumanSessionStore,
    InMemoryHumanSessionStore,
    load_github_oauth_config_from_env,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.verireel_preview_driver import (
    VeriReelPreviewDestroyResult,
    VeriReelPreviewInventoryItem,
    VeriReelPreviewInventoryResult,
    VeriReelPreviewRefreshConfigError,
    VeriReelPreviewRefreshResult,
    VeriReelPreviewRefreshTransportError,
)

from control_plane.workflows.verireel_app_maintenance import VeriReelAppMaintenanceResult
from control_plane.contracts.verireel_prod_backup_gate import VeriReelProdBackupGateResult
from control_plane.workflows.verireel_prod_promotion import VeriReelProdPromotionResult
from control_plane.workflows.verireel_prod_rollback import VeriReelProdRollbackResult
from control_plane.workflows.merge_train_worker import MergeTrainWorkerClients
from control_plane.workflows.merge_train_worker import run_merge_train_worker_step
from control_plane.workflows.verireel_stable_deploy import VeriReelStableDeployResult
from control_plane.workflows.verireel_environment import VeriReelStableEnvironmentResult
from control_plane.workflows.verireel_rollout import VeriReelRolloutVerificationResult
from control_plane.workflows.odoo_artifact_publish import OdooArtifactPublishResult
from control_plane.workflows.odoo_generic_web_post_deploy import (
    execute_odoo_generic_web_post_deploy,
)
from control_plane.workflows.odoo_stable_target_replacement import (
    OdooStableTargetReplacementPlan,
)
from tests.merge_train_policy_fixtures import build_test_merge_train_policy
from tests.merge_train_policy_fixtures import build_test_merge_train_policy_record
from control_plane.workflows.generic_web_promotion import GenericWebProdPromotionResult
from control_plane.workflows.generic_web_deploy import GenericWebDeployResult
from control_plane.workflows.dokploy_deploy import DokployComposeSourceRefDeployResult
from control_plane.workflows.generic_web_rollback import GenericWebRollbackApplyResult
from control_plane.workflows.generic_web_promotion_workflow import GenericWebPromotionWorkflowResult
from control_plane.workflows.generic_web_preview import (
    GenericWebPreviewDestroyResult,
    GenericWebPreviewRefreshRequest,
    GenericWebPreviewRefreshResult,
)
from control_plane.npmplus import NpmplusProxyHost, NpmplusProxyHostPayload
from control_plane.workflows.npmplus_ingress import (
    NpmplusIngressApplyRequest,
    NpmplusIngressApplyResult,
    NpmplusIngressRouteDesiredState,
)

StartResponse = Callable[[str, list[tuple[str, str]]], None]
WsgiApp = Callable[[dict[str, object], StartResponse], Iterable[bytes]]
CLI_MAIN = cast(Command, main)
TERMINAL_AGENT_AUTH_ENV = {
    "LAUNCHPLANE_TERMINAL_AGENT_READ_TOKEN": "terminal-read-token",
    "LAUNCHPLANE_TERMINAL_AGENT_SUBJECT": "local-owner-agent",
    "LAUNCHPLANE_TERMINAL_AGENT_TOKEN_LABEL": "local-owner-read",
}
LOCAL_OPERATOR_AUTH_ENV = {
    "LAUNCHPLANE_LOCAL_OPERATOR_TOKEN": "local-operator-token",
    "LAUNCHPLANE_LOCAL_OPERATOR_SUBJECT": "local-owner-agent",
    "LAUNCHPLANE_LOCAL_OPERATOR_TOKEN_LABEL": "local-owner-write",
}


class _StubVerifier:
    def __init__(self, identity: GitHubActionsIdentity):
        self.identity = identity

    def verify(self, token: str) -> GitHubActionsIdentity:
        if token != "valid-token":
            raise ValueError("OIDC bearer token is required.")
        return self.identity


_FAKE_DESCRIPTOR_DRIVER_ID = "fake-descriptor"
_FAKE_DESCRIPTOR_ROUTE_PATH = "/v1/drivers/fake-descriptor/ping"


class _FakeDescriptorDispatchEnvelope(control_plane_service._ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    context: str
    instance: str = ""
    value: str = ""


class _FakeDescriptorDispatchDriverResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    processed_value: str


def _fake_descriptor_dispatch_descriptor() -> DriverDescriptor:
    return DriverDescriptor(
        driver_id=_FAKE_DESCRIPTOR_DRIVER_ID,
        label="Fake descriptor dispatch",
        product="fake-descriptor",
        description="Test-only driver descriptor for descriptor dispatch coverage.",
        context_patterns=("fake-context",),
        provider_boundary="Test-only provider boundary.",
        actions=(
            DriverActionDescriptor(
                action_id="ping",
                label="Ping",
                description="Test descriptor-backed dispatch route.",
                safety="safe_write",
                scope="instance",
                method="POST",
                route_path=_FAKE_DESCRIPTOR_ROUTE_PATH,
                authz_action="fake_descriptor.ping",
                writes_records=("fake_descriptor_result",),
            ),
        ),
    )


def _fake_descriptor_dispatch_route(
    calls: list[tuple[_FakeDescriptorDispatchEnvelope, str]],
) -> control_plane_service._DescriptorDriverDispatchRoute[_FakeDescriptorDispatchEnvelope]:
    def context_resolver(
        request: _FakeDescriptorDispatchEnvelope,
    ) -> control_plane_service._DescriptorDriverDispatchContext:
        return control_plane_service._DescriptorDriverDispatchContext(
            product=request.product,
            context=request.context,
            instance=request.instance,
            require_profile=True,
        )

    def handler(
        request: _FakeDescriptorDispatchEnvelope,
        resolved_context: control_plane_service._ResolvedProductDriverContext,
        record_store: Any,
        control_plane_root_path: Path,
    ) -> control_plane_service._DescriptorDriverDispatchResult:
        del record_store, control_plane_root_path
        lane_instance = resolved_context.lane.instance if resolved_context.lane is not None else ""
        calls.append((request, lane_instance))
        return control_plane_service._DescriptorDriverDispatchResult(
            result={"request_id": f"fake-descriptor-{request.value}"},
            driver_result=_FakeDescriptorDispatchDriverResult(
                status="pass",
                processed_value=f"{request.value}:{lane_instance}",
            ),
        )

    return control_plane_service._DescriptorDriverDispatchRoute(
        execution_metadata=control_plane_service._DriverRouteExecutionMetadata(
            route_path=_FAKE_DESCRIPTOR_ROUTE_PATH,
            envelope_model=_FakeDescriptorDispatchEnvelope,
            denial_message="Workflow cannot execute fake descriptor dispatch.",
        ),
        context_resolver=context_resolver,
        handler=handler,
    )


class _StubGitHubOAuthClient:
    def __init__(self, identity: GitHubHumanIdentity):
        self.identity = identity
        self.code_verifier = ""

    def authorization_url(self, *, state: str, code_challenge: str) -> str:
        return f"https://github.example/authorize?state={state}&challenge={code_challenge}"

    def fetch_identity(
        self,
        *,
        code: str,
        code_verifier: str,
        authz_policy: LaunchplaneAuthzPolicy,
    ) -> GitHubHumanIdentity:
        self.code_verifier = code_verifier
        if code != "github-code":
            raise ValueError("unexpected code")
        return self.identity


class _FakeNpmplusIngressClient:
    def __init__(self, proxy_hosts: tuple[NpmplusProxyHost, ...] = ()) -> None:
        self.proxy_hosts = list(proxy_hosts)
        self.calls: list[str] = []
        self.next_id = 100

    def list_proxy_hosts(self) -> tuple[NpmplusProxyHost, ...]:
        self.calls.append("list")
        return tuple(self.proxy_hosts)

    def create_proxy_host(self, payload: NpmplusProxyHostPayload) -> NpmplusProxyHost:
        self.calls.append("create")
        created = NpmplusProxyHost.model_validate({"id": self.next_id, **payload.to_api_payload()})
        self.proxy_hosts.append(created)
        return created

    def update_proxy_host(
        self, *, host_id: int, payload: NpmplusProxyHostPayload
    ) -> NpmplusProxyHost:
        self.calls.append(f"update:{host_id}")
        updated = NpmplusProxyHost.model_validate({"id": host_id, **payload.to_api_payload()})
        self.proxy_hosts = [updated if host.id == host_id else host for host in self.proxy_hosts]
        return updated

    def disable_proxy_host(self, host_id: int) -> NpmplusProxyHost:
        self.calls.append(f"disable:{host_id}")
        return self._set_enabled(host_id=host_id, enabled=False)

    def enable_proxy_host(self, host_id: int) -> NpmplusProxyHost:
        self.calls.append(f"enable:{host_id}")
        return self._set_enabled(host_id=host_id, enabled=True)

    def _set_enabled(self, *, host_id: int, enabled: bool) -> NpmplusProxyHost:
        for index, host in enumerate(self.proxy_hosts):
            if host.id == host_id:
                updated = NpmplusProxyHost.model_validate(
                    {**host.model_dump(mode="json"), "enabled": enabled}
                )
                self.proxy_hosts[index] = updated
                return updated
        raise AssertionError(f"Unknown proxy host: {host_id}")


class _FakeIngressProvider:
    provider_id = "fake-ingress"
    delegated_executor = "control-plane.fake-ingress"

    def __init__(self, result: NpmplusIngressApplyResult) -> None:
        self.result = result
        self.requests: list[NpmplusIngressApplyRequest] = []

    def apply_route(
        self,
        *,
        request: NpmplusIngressApplyRequest,
    ) -> NpmplusIngressApplyResult:
        self.requests.append(request)
        return self.result


class _FakeGitHubResponse:
    def __init__(self, payload: object):
        self._payload = payload

    def json(self) -> object:
        return self._payload


class _FakeOAuth2Session:
    def __init__(self, payloads: dict[str, object]):
        self.payloads = payloads
        self.requested_urls: list[str] = []
        self.token_request: dict[str, str] = {}

    def fetch_token(self, url: str, *, code: str, code_verifier: str) -> None:
        self.token_request = {
            "url": url,
            "code": code,
            "code_verifier": code_verifier,
        }

    def get(self, url: str) -> _FakeGitHubResponse:
        self.requested_urls.append(url)
        return _FakeGitHubResponse(self.payloads[url])


class _FakeMergeTrainGitHubClient:
    land_batch_candidate_calls = 0
    cleanup_batch_candidate_ref_calls = 0

    def __init__(self, *, transport: object) -> None:
        self.transport = transport

    def add_pull_request_label(
        self, *, repository: str, pull_request_number: int, label: str
    ) -> None:
        return None

    def update_pull_request_branch(
        self, *, repository: str, pull_request_number: int, expected_head_sha: str
    ) -> None:
        return None

    def merge_pull_request(
        self,
        *,
        repository: str,
        pull_request_number: int,
        head_sha: str,
        merge_method: str,
    ) -> str:
        return f"merge-{pull_request_number}"

    def build_batch_candidate(
        self, *, candidate: MergeTrainBatchCandidate
    ) -> MergeTrainBatchCandidate:
        return candidate.model_copy(
            update={"candidate_sha": "candidate-built", "status": "ready_for_checks"}
        )

    def observe_batch_candidate_checks(
        self, *, candidate: MergeTrainBatchCandidate
    ) -> MergeTrainBatchCandidate:
        return candidate.model_copy(update={"required_checks_status": "pass", "status": "passed"})

    def land_batch_candidate(
        self, *, landing_plan: MergeTrainBatchLandingPlan
    ) -> MergeTrainBatchLandingPlan:
        type(self).land_batch_candidate_calls += 1
        return landing_plan.model_copy(
            update={
                "entries": tuple(
                    entry.model_copy(
                        update={
                            "status": "merged",
                            "merge_commit_sha": f"merge-{entry.pull_request_number}",
                        }
                    )
                    for entry in landing_plan.entries
                )
            }
        )

    def cleanup_batch_candidate_ref(self, *, landing_plan: MergeTrainBatchLandingPlan) -> bool:
        type(self).cleanup_batch_candidate_ref_calls += 1
        return True

    def merge_stack_child_into_parent(
        self,
        *,
        repository: str,
        child_head_sha: str,
        expected_parent_head_sha: str,
        parent_head_ref: str,
        collapse_id: str,
        child_pull_request_number: int,
        parent_pull_request_number: int,
    ) -> str:
        return f"stack-merge-{child_pull_request_number}-into-{parent_pull_request_number}"

    def comment_pull_request(self, *, repository: str, pull_request_number: int, body: str) -> str:
        return f"https://github.com/{repository}/pull/{pull_request_number}#issuecomment-1"

    def close_pull_request(
        self, *, repository: str, pull_request_number: int, expected_head_sha: str
    ) -> None:
        return None


class _FakeFailingMergeTrainGitHubClient(_FakeMergeTrainGitHubClient):
    def observe_batch_candidate_checks(
        self, *, candidate: MergeTrainBatchCandidate
    ) -> MergeTrainBatchCandidate:
        return candidate.model_copy(update={"required_checks_status": "fail", "status": "failed"})


class _StaleLandingMergeTrainGitHubClient(_FakeMergeTrainGitHubClient):
    def land_batch_candidate(
        self, *, landing_plan: MergeTrainBatchLandingPlan
    ) -> MergeTrainBatchLandingPlan:
        raise MergeTrainGitHubStaleHeadError(
            "Base branch moved outside the batch landing plan.", status_code=409
        )


class _UnavailableLandingMergeTrainGitHubClient(_FakeMergeTrainGitHubClient):
    def land_batch_candidate(
        self, *, landing_plan: MergeTrainBatchLandingPlan
    ) -> MergeTrainBatchLandingPlan:
        raise MergeTrainGitHubError(
            "GitHub API request failed for /repos/example/repo", status_code=503
        )


class _CleanupFailingMergeTrainGitHubClient(_FakeMergeTrainGitHubClient):
    cleanup_batch_candidate_ref_calls = 0

    def cleanup_batch_candidate_ref(self, *, landing_plan: MergeTrainBatchLandingPlan) -> bool:
        type(self).cleanup_batch_candidate_ref_calls += 1
        raise MergeTrainGitHubError("candidate ref cleanup unavailable", status_code=503)


class _CleanupAlreadyMissingMergeTrainGitHubClient(_FakeMergeTrainGitHubClient):
    cleanup_batch_candidate_ref_calls = 0

    def cleanup_batch_candidate_ref(self, *, landing_plan: MergeTrainBatchLandingPlan) -> bool:
        type(self).cleanup_batch_candidate_ref_calls += 1
        return False


class _CleanupFailingWithoutStatusMergeTrainGitHubClient(_FakeMergeTrainGitHubClient):
    cleanup_batch_candidate_ref_calls = 0

    def cleanup_batch_candidate_ref(self, *, landing_plan: MergeTrainBatchLandingPlan) -> bool:
        type(self).cleanup_batch_candidate_ref_calls += 1
        raise MergeTrainGitHubError("candidate ref cleanup network unavailable")


class _FailingChildDispositionMergeTrainGitHubClient(_FakeMergeTrainGitHubClient):
    def add_pull_request_label(
        self, *, repository: str, pull_request_number: int, label: str
    ) -> None:
        raise RuntimeError("label persistence unavailable")


class _StackCollapseWriteFailingFilesystemRecordStore(FilesystemRecordStore):
    def write_merge_train_stack_collapse_plan_record(self, record: object) -> Path:
        raise RuntimeError("stack collapse persistence unavailable")


class _CandidateReflowWriteFailingFilesystemRecordStore(FilesystemRecordStore):
    def write_merge_train_batch_candidate_record(self, record: object) -> Path:
        candidate_record = cast(MergeTrainBatchCandidateRecord, record)
        if "candidate-reflow" in candidate_record.source:
            raise RuntimeError("candidate reflow persistence unavailable")
        return super().write_merge_train_batch_candidate_record(candidate_record)


class _CandidateReflowSupersedeFailingFilesystemRecordStore(FilesystemRecordStore):
    def write_merge_train_batch_candidate_record(self, record: object) -> Path:
        candidate_record = cast(MergeTrainBatchCandidateRecord, record)
        if (
            candidate_record.status == "superseded"
            and "candidate-reflow" not in candidate_record.source
        ):
            raise RuntimeError("candidate supersession persistence unavailable")
        return super().write_merge_train_batch_candidate_record(candidate_record)


class _SameBatchIdReflowFilesystemRecordStore(FilesystemRecordStore):
    def write_merge_train_batch_candidate_record(self, record: object) -> Path:
        candidate_record = cast(MergeTrainBatchCandidateRecord, record)
        if "candidate-reflow" in candidate_record.source:
            records = self.list_merge_train_batch_candidate_records(
                repository=candidate_record.candidate.repository,
                base_branch=candidate_record.candidate.base_branch,
                status="active",
            )
            failed_record = next(
                record for record in records if record.candidate.status == "failed"
            )
            candidate_record = candidate_record.model_copy(
                update={
                    "candidate": candidate_record.candidate.model_copy(
                        update={"batch_id": failed_record.candidate.batch_id}
                    )
                }
            )
        return super().write_merge_train_batch_candidate_record(candidate_record)


class _NoopMergeTrainGitHubClient:
    def add_pull_request_label(
        self, *, repository: str, pull_request_number: int, label: str
    ) -> None:
        return None

    def update_pull_request_branch(
        self, *, repository: str, pull_request_number: int, expected_head_sha: str
    ) -> None:
        return None

    def merge_pull_request(
        self,
        *,
        repository: str,
        pull_request_number: int,
        head_sha: str,
        merge_method: str,
    ) -> str:
        return f"merge-{pull_request_number}"


class _FakeMergeTrainSnapshotReader:
    def __init__(self, *, transport: object) -> None:
        self.transport = transport

    def read_merge_train_snapshot(
        self, *, repository: str, base_branch: str
    ) -> MergeTrainDryRunSnapshot:
        return MergeTrainDryRunSnapshot(
            repository=repository,
            base_branch=base_branch,
            base_sha="current-base-main",
            pull_requests=(
                MergeTrainPullRequestSnapshot(
                    number=1,
                    url=f"https://github.com/{repository}/pull/1",
                    title="Ready PR",
                    created_at="2026-05-08T10:00:00Z",
                    labels=("ready-to-merge",),
                    actor_role="repo_admin",
                    head_sha="head-1",
                    head_ref="feature/root",
                    head_repository=repository,
                    base_ref=base_branch,
                    base_repository=repository,
                    base_sha="base-main",
                    mergeable="mergeable",
                    required_checks_status="pass",
                ),
            ),
        )


class _FakeExpandedMergeTrainSnapshotReader(_FakeMergeTrainSnapshotReader):
    def read_merge_train_snapshot(
        self, *, repository: str, base_branch: str
    ) -> MergeTrainDryRunSnapshot:
        base_snapshot = super().read_merge_train_snapshot(
            repository=repository, base_branch=base_branch
        )
        return base_snapshot.model_copy(
            update={
                "pull_requests": (
                    *base_snapshot.pull_requests,
                    MergeTrainPullRequestSnapshot(
                        number=2,
                        url=f"https://github.com/{repository}/pull/2",
                        title="Validation fix",
                        created_at="2026-05-08T10:05:00Z",
                        labels=("ready-to-merge",),
                        actor_role="repo_admin",
                        head_sha="head-2",
                        head_ref="feature/validation-fix",
                        head_repository=repository,
                        base_ref=base_branch,
                        base_repository=repository,
                        base_sha="base-main",
                        mergeable="mergeable",
                        required_checks_status="pass",
                    ),
                )
            }
        )


class _FakeEmptyMergeTrainSnapshotReader:
    def __init__(self, *, transport: object) -> None:
        self.transport = transport

    def read_merge_train_snapshot(
        self, *, repository: str, base_branch: str
    ) -> MergeTrainDryRunSnapshot:
        return MergeTrainDryRunSnapshot(
            repository=repository,
            base_branch=base_branch,
            base_sha="current-base-main",
            pull_requests=(),
        )


class _FakeStackedMergeTrainSnapshotReader:
    def __init__(self, *, transport: object) -> None:
        self.transport = transport

    def read_merge_train_snapshot(
        self, *, repository: str, base_branch: str
    ) -> MergeTrainDryRunSnapshot:
        return MergeTrainDryRunSnapshot(
            repository=repository,
            base_branch=base_branch,
            base_sha="current-base-main",
            pull_requests=(
                MergeTrainPullRequestSnapshot(
                    number=1,
                    url=f"https://github.com/{repository}/pull/1",
                    title="Root PR",
                    created_at="2026-05-08T10:00:00Z",
                    labels=("ready-to-merge",),
                    actor_role="repo_admin",
                    head_sha=self._root_head_sha(),
                    head_ref="feature/root",
                    head_repository=repository,
                    base_ref=base_branch,
                    base_repository=repository,
                    base_sha="base-main",
                    mergeable="mergeable",
                    required_checks_status="pass",
                ),
                MergeTrainPullRequestSnapshot(
                    number=2,
                    url=f"https://github.com/{repository}/pull/2",
                    title="Stacked child PR",
                    created_at="2026-05-08T11:00:00Z",
                    labels=(),
                    actor_role="repo_admin",
                    head_sha="head-child",
                    head_ref="feature/child",
                    head_repository=repository,
                    base_ref="feature/root",
                    base_repository=repository,
                    base_sha="head-root",
                    mergeable="mergeable",
                    required_checks_status="pending",
                ),
            ),
        )

    def _root_head_sha(self) -> str:
        return "head-root"


class _FakeCollapsedRootStackedMergeTrainSnapshotReader(_FakeStackedMergeTrainSnapshotReader):
    def _root_head_sha(self) -> str:
        return "stack-merge-2-into-1"


class _FakeMovedRootStackedMergeTrainSnapshotReader(_FakeStackedMergeTrainSnapshotReader):
    def read_merge_train_snapshot(
        self, *, repository: str, base_branch: str
    ) -> MergeTrainDryRunSnapshot:
        snapshot = super().read_merge_train_snapshot(repository=repository, base_branch=base_branch)
        return snapshot.model_copy(
            update={
                "pull_requests": tuple(
                    pull_request.model_copy(update={"head_sha": "moved-root-head"})
                    if pull_request.number == 1
                    else pull_request
                    for pull_request in snapshot.pull_requests
                )
            }
        )


class _FlakyEveryCodeNotificationStore:
    def __init__(self) -> None:
        self.policies: list[EveryCodeNotificationPolicyRecord] = []
        self.attempts: list[EveryCodeNotificationAttemptRecord] = []
        self.fail_delivered_write = False

    def list_every_code_notification_policy_records(
        self,
        *,
        repository: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[EveryCodeNotificationPolicyRecord, ...]:
        records = [
            policy
            for policy in self.policies
            if (not repository or policy.repository in {"", repository})
            and (not status or policy.status == status)
        ]
        return tuple(records[:limit] if limit is not None else records)

    def write_every_code_notification_policy_record(
        self, record: EveryCodeNotificationPolicyRecord
    ) -> object:
        self.policies.append(record)
        return record.policy_id

    def list_every_code_notification_attempt_records(
        self,
        *,
        request_id: str = "",
        event: str = "",
        destination_kind: str = "",
        limit: int | None = None,
    ) -> tuple[EveryCodeNotificationAttemptRecord, ...]:
        records = [
            attempt
            for attempt in self.attempts
            if (not request_id or attempt.request_id == request_id)
            and (not event or attempt.event == event)
            and (not destination_kind or attempt.destination_kind == destination_kind)
        ]
        return tuple(records[:limit] if limit is not None else records)

    def write_every_code_notification_attempt_record(
        self, record: EveryCodeNotificationAttemptRecord
    ) -> object:
        if self.fail_delivered_write and record.delivery_status == "delivered":
            raise RuntimeError("attempt write unavailable")
        self.attempts = [
            attempt for attempt in self.attempts if attempt.attempt_id != record.attempt_id
        ]
        self.attempts.append(record)
        return record.attempt_id


def _merge_train_service_policy() -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": "cbusillo/launchplane",
                    "workflow_refs": [
                        "cbusillo/launchplane/.github/workflows/merge-train.yml@refs/heads/main"
                    ],
                    "event_names": ["workflow_dispatch"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": ["merge_train.run_once"],
                }
            ]
        }
    )


def _local_operator_policy(
    *,
    actions: tuple[str, ...],
    products: tuple[str, ...] = ("*",),
    contexts: tuple[str, ...] = ("*",),
    subject: str = "local-owner-agent",
    token_label: str = "local-owner-write",
) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy(
        local_operators=(
            LocalOperatorPolicyRule(
                subjects=(subject,),
                token_labels=(token_label,),
                products=products,
                contexts=contexts,
                actions=actions,
            ),
        )
    )


def _local_admin_policy(
    *,
    actions: tuple[str, ...],
    products: tuple[str, ...] = ("launchplane",),
    contexts: tuple[str, ...] = ("launchplane",),
) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy(
        local_admins=(
            LocalAdminPolicyRule(
                subjects=("local-owner-admin",),
                token_labels=("local-owner-admin",),
                products=products,
                contexts=contexts,
                actions=actions,
            ),
        )
    )


def _merge_train_service_identity() -> GitHubActionsIdentity:
    return _identity(
        repository="cbusillo/launchplane",
        workflow_ref="cbusillo/launchplane/.github/workflows/merge-train.yml@refs/heads/main",
        event_name="workflow_dispatch",
    )


def _every_code_worker_policy(*, extra_actions: tuple[str, ...] = ()) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": "cbusillo/launchplane",
                    "workflow_refs": [
                        "cbusillo/launchplane/.github/workflows/every-code-worker.yml@refs/heads/main"
                    ],
                    "event_names": ["workflow_dispatch"],
                    "products": ["launchplane"],
                    "contexts": ["launchplane"],
                    "actions": [
                        "every_code_work_request.read",
                        "every_code_work_request.claim",
                        "every_code_work_request.update",
                        *extra_actions,
                    ],
                }
            ]
        }
    )


def _every_code_worker_identity() -> GitHubActionsIdentity:
    return _identity(
        repository="cbusillo/launchplane",
        workflow_ref="cbusillo/launchplane/.github/workflows/every-code-worker.yml@refs/heads/main",
        event_name="workflow_dispatch",
    )


def _seed_merge_train_policy(
    state_dir: Path, *, policy: MergeTrainPolicyRecord | None = None
) -> MergeTrainPolicyRecord:
    record = policy or build_test_merge_train_policy_record()
    FilesystemRecordStore(state_dir).write_merge_train_policy_record(record)
    return record


def _merge_train_policy_table(
    repository: str,
    base_branch: str = "main",
    *,
    scheduler_enabled: bool = False,
    scheduler_runner_mode: str = "controller",
    scheduler_mutate: bool = False,
) -> str:
    scheduler_table = ""
    if scheduler_enabled:
        scheduler_table = f"""
[policies.scheduler]
enabled = true
runner_mode = "{scheduler_runner_mode}"
mutate = {str(scheduler_mutate).lower()}
"""
    return f"""[[policies]]
repository = "{repository}"
base_branch = "{base_branch}"
enqueue_label = "ready-to-merge"
blocked_label = "merge-blocked"
stack_child_disposition_label = "stack-landed"
merge_method = "merge"
failure_policy = "pause_train"

[policies.enqueue]
label_required = true
allowed_actor_roles = ["repo_owner", "repo_admin"]

[policies.merge_identity]
kind = "github_actions_oidc"
name = "launchplane-merge-train"

[policies.service_authz]
action = "merge_train.run_once"
product = "launchplane"
context = "launchplane"

[policies.github_token]
env_var = "GH_TOKEN"
{scheduler_table}
"""


def _merge_train_policy_with_label(
    *, repository: str = "cbusillo/sellyouroutboard", enqueue_label: str = "ready-to-merge"
) -> MergeTrainPolicy:
    return parse_merge_train_policy_toml(
        f"""schema_version = 1

[[policies]]
repository = "{repository}"
base_branch = "main"
enqueue_label = "{enqueue_label}"
blocked_label = "merge-blocked"
stack_child_disposition_label = "stack-landed"
merge_method = "merge"
failure_policy = "pause_train"

[policies.enqueue]
label_required = true
allowed_actor_roles = ["repo_owner", "repo_admin"]

[policies.merge_identity]
kind = "github_actions_oidc"
name = "launchplane-merge-train"

[policies.service_authz]
action = "merge_train.run_once"
product = "launchplane"
context = "launchplane"

[policies.github_token]
env_var = "GH_TOKEN"
"""
    )


def _merge_train_run_record(
    *,
    recorded_at: str,
    required_checks_status: MergeTrainCheckStatus = "pass",
    mutate: bool = False,
) -> MergeTrainRunRecord:
    policy = build_test_merge_train_policy()
    snapshot = MergeTrainDryRunSnapshot(
        repository="cbusillo/sellyouroutboard",
        base_branch="main",
        pull_requests=(
            MergeTrainPullRequestSnapshot(
                number=1,
                url="https://github.com/cbusillo/sellyouroutboard/pull/1",
                title="Ready PR",
                created_at="2026-05-08T10:00:00Z",
                labels=("ready-to-merge",),
                actor_role="repo_admin",
                head_sha="head-1",
                base_ref="main",
                base_sha="base-main",
                mergeable="mergeable",
                required_checks_status=required_checks_status,
            ),
        ),
    )
    dry_run_result = build_merge_train_dry_run_result(policy=policy, snapshot=snapshot)
    worker_step_result = None
    if mutate:
        noop_client = _NoopMergeTrainGitHubClient()
        worker_step_result = run_merge_train_worker_step(
            policy=policy,
            snapshot=snapshot,
            clients=MergeTrainWorkerClients(
                label_client=noop_client,
                branch_client=noop_client,
                merge_client=noop_client,
            ),
        )
    return build_merge_train_run_record(
        recorded_at=recorded_at,
        trace_id="launchplane_req_merge_train_service_test",
        policy_sha256=policy.policy_sha256,
        snapshot=snapshot,
        dry_run_result=dry_run_result,
        worker_step_result=worker_step_result,
    )


def _seed_merge_train_batch_candidate_record(
    state_dir: Path,
    *,
    status: str = "planned",
    required_checks_status: str = "pending",
    candidate_sha: str = "",
    policy: MergeTrainPolicy | None = None,
    snapshot_reader: type[_FakeMergeTrainSnapshotReader] = _FakeMergeTrainSnapshotReader,
) -> MergeTrainBatchCandidateRecord:
    merge_train_policy = policy or build_test_merge_train_policy()
    snapshot = snapshot_reader(transport=object()).read_merge_train_snapshot(
        repository="cbusillo/sellyouroutboard",
        base_branch="main",
    )
    dry_run_result = build_merge_train_dry_run_result(
        policy=merge_train_policy,
        snapshot=snapshot,
    )
    candidate = build_merge_train_batch_candidate(
        dry_run_result=dry_run_result,
        base_sha=snapshot.base_sha,
        policy_sha256=merge_train_policy.policy_sha256,
        created_at="2026-05-13T21:00:00Z",
    ).model_copy(
        update={
            "status": status,
            "required_checks_status": required_checks_status,
            "candidate_sha": candidate_sha,
        }
    )
    record = build_merge_train_batch_candidate_record(
        candidate=candidate,
        source=f"test:{status}",
        updated_at="2026-05-13T21:00:00Z",
    )
    FilesystemRecordStore(state_dir).write_merge_train_batch_candidate_record(record)
    return record


def _mark_merge_train_batch_candidate_record_passed(
    state_dir: Path, *, record_id: str
) -> MergeTrainBatchCandidateRecord:
    store = FilesystemRecordStore(state_dir)
    existing_record = next(
        record
        for record in store.list_merge_train_batch_candidate_records(
            repository="cbusillo/sellyouroutboard",
            base_branch="main",
        )
        if record.record_id == record_id
    )
    candidate = existing_record.candidate.model_copy(
        update={
            "status": "passed",
            "required_checks_status": "pass",
            "candidate_sha": "candidate-built",
        }
    )
    passed_record = build_merge_train_batch_candidate_record(
        candidate=candidate,
        source="test:passed",
        updated_at="2026-05-13T21:05:00Z",
    )
    store.write_merge_train_batch_candidate_record(passed_record)
    return passed_record


def _seed_merge_train_stack_collapse_plan_record(
    state_dir: Path,
    *,
    policy: MergeTrainPolicy | None = None,
    snapshot_reader: type[
        _FakeStackedMergeTrainSnapshotReader
    ] = _FakeStackedMergeTrainSnapshotReader,
) -> str:
    merge_train_policy = policy or build_test_merge_train_policy()
    snapshot = snapshot_reader(transport=object()).read_merge_train_snapshot(
        repository="cbusillo/sellyouroutboard",
        base_branch="main",
    )
    dry_run_result = build_merge_train_dry_run_result(
        policy=merge_train_policy,
        snapshot=snapshot,
    )
    selected_pr = dry_run_result.selected_pr
    assert selected_pr is not None
    stack_discovery = discover_merge_train_stack(
        snapshot=snapshot,
        root_pull_request_number=selected_pr.number,
    )
    stack_collapse_plan = build_merge_train_stack_collapse_plan(
        discovery_result=stack_discovery,
        policy_key=dry_run_result.policy_key,
        policy_sha256=merge_train_policy.policy_sha256,
        created_at="2026-05-13T21:00:00Z",
    )
    record = build_merge_train_stack_collapse_plan_record(
        plan=stack_collapse_plan,
        source="test:plan",
        updated_at="2026-05-13T21:00:00Z",
    )
    FilesystemRecordStore(state_dir).write_merge_train_stack_collapse_plan_record(record)
    return record.record_id


def _seed_executed_merge_train_stack_collapse_plan_record(
    state_dir: Path,
    *,
    policy: MergeTrainPolicy | None = None,
    snapshot_reader: type[
        _FakeStackedMergeTrainSnapshotReader
    ] = _FakeStackedMergeTrainSnapshotReader,
) -> str:
    planned_record_id = _seed_merge_train_stack_collapse_plan_record(
        state_dir,
        policy=policy,
        snapshot_reader=snapshot_reader,
    )
    store = FilesystemRecordStore(state_dir)
    planned_record = next(
        record
        for record in store.list_merge_train_stack_collapse_plan_records(
            repository="cbusillo/sellyouroutboard", base_branch="main"
        )
        if record.record_id == planned_record_id
    )
    executed_plan = execute_merge_train_stack_collapse_plan(
        plan=planned_record.plan,
        branch_client=_FakeMergeTrainGitHubClient(transport=object()),
        updated_at="2026-05-13T21:02:00Z",
    )
    executed_record = build_merge_train_stack_collapse_plan_record(
        plan=executed_plan,
        source="test:execute",
        updated_at="2026-05-13T21:02:00Z",
    )
    store.write_merge_train_stack_collapse_plan_record(executed_record)
    return executed_record.record_id


def _seed_admitted_merge_train_stack_collapse_candidate(
    state_dir: Path,
    *,
    executed_record_id: str,
    policy: MergeTrainPolicy | None = None,
    snapshot_reader: type[
        _FakeStackedMergeTrainSnapshotReader
    ] = _FakeCollapsedRootStackedMergeTrainSnapshotReader,
) -> MergeTrainBatchCandidateRecord:
    merge_train_policy = policy or build_test_merge_train_policy()
    store = FilesystemRecordStore(state_dir)
    executed_record = next(
        record
        for record in store.list_merge_train_stack_collapse_plan_records(
            repository="cbusillo/sellyouroutboard", base_branch="main"
        )
        if record.record_id == executed_record_id
    )
    snapshot = snapshot_reader(transport=object()).read_merge_train_snapshot(
        repository="cbusillo/sellyouroutboard",
        base_branch="main",
    )
    root_pull_request = next(
        pull_request
        for pull_request in snapshot.pull_requests
        if pull_request.number == executed_record.plan.root_pull_request_number
    )
    dry_run_result = build_merge_train_dry_run_result(
        policy=merge_train_policy,
        snapshot=snapshot.model_copy(update={"pull_requests": (root_pull_request,)}),
    )
    candidate = build_merge_train_batch_candidate(
        dry_run_result=dry_run_result,
        base_sha=snapshot.base_sha,
        policy_sha256=merge_train_policy.policy_sha256,
        created_at="2026-05-13T21:03:00Z",
    )
    candidate_record = build_merge_train_batch_candidate_record(
        candidate=candidate,
        source="test:stack-collapse-admit",
        updated_at="2026-05-13T21:03:00Z",
    )
    store.write_merge_train_batch_candidate_record(candidate_record)
    return candidate_record


def _identity(
    *,
    repository: str = "every/verireel",
    workflow_ref: str = "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main",
    job_workflow_ref: str = "",
    event_name: str = "pull_request",
    ref: str = "refs/heads/main",
    environment: str = "",
) -> GitHubActionsIdentity:
    return GitHubActionsIdentity(
        repository=repository,
        repository_owner="every",
        workflow_ref=workflow_ref,
        job_workflow_ref=job_workflow_ref,
        ref=ref,
        ref_type="branch",
        event_name=event_name,
        environment=environment,
        subject="repo:every/verireel:pull_request",
        sha="6b3c9d7e8f901234567890abcdef1234567890ab",
        raw_claims={"repository": repository, "workflow_ref": workflow_ref},
    )


def _human_identity(*, role: Literal["read_only", "admin"] = "read_only") -> GitHubHumanIdentity:
    return GitHubHumanIdentity(
        login="alice",
        github_id=123,
        name="Alice Operator",
        email="alice@example.com",
        organizations=frozenset({"shinycomputers"}),
        teams=frozenset({"launchplane-readers", "shinycomputers/launchplane-readers"}),
        role=role,
    )


def _github_oauth_config() -> GitHubOAuthConfig:
    return GitHubOAuthConfig(
        client_id="client-id",
        client_secret="client-secret",
        public_url="https://launchplane.example",
        session_secret="test-session-secret",
        cookie_secure=False,
    )


def _signed_human_session_cookie(session_id: str, session_secret: str) -> str:
    signature = hmac.new(
        session_secret.encode("utf-8"), session_id.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"launchplane_session={session_id}.{signature}"


def _signed_in_cookie(
    session_store: HumanSessionStore,
    *,
    role: Literal["read_only", "admin"] = "read_only",
) -> str:
    session_manager = HumanSessionManager(
        config=_github_oauth_config(),
        session_store=session_store,
    )
    human_session = session_manager.issue(_human_identity(role=role))
    return session_manager.session_cookie_header(human_session)


def _fastapi_human_session_manager() -> HumanSessionManager:
    return HumanSessionManager(
        config=_github_oauth_config(),
        session_store=InMemoryHumanSessionStore(),
    )


def _fastapi_signed_in_cookie(
    session_manager: HumanSessionManager,
    *,
    role: Literal["read_only", "admin"] = "admin",
) -> str:
    human_session = session_manager.issue(_human_identity(role=role))
    return session_manager.session_cookie_header(human_session)


def _authz_policy_record_by_id(
    records: tuple[LaunchplaneAuthzPolicyRecord, ...], record_id: object
) -> LaunchplaneAuthzPolicyRecord:
    for record in records:
        if record.record_id == str(record_id):
            return record
    raise AssertionError(f"Authz policy record {record_id!r} was not found")


def _product_profile_payload(product: str = "sellyouroutboard") -> dict[str, object]:
    return {
        "schema_version": 1,
        "product": product,
        "display_name": "Sell Your Outboard",
        "repository": f"cbusillo/{product}",
        "driver_id": "generic-web",
        "image": {"repository": f"ghcr.io/cbusillo/{product}"},
        "runtime_port": 3000,
        "health_path": "/api/health",
        "lanes": (
            {
                "instance": "testing",
                "context": f"{product}-testing",
                "base_url": "https://testing.sellyouroutboard.com",
                "health_url": "https://testing.sellyouroutboard.com/api/health",
            },
        ),
        "preview": {
            "enabled": True,
            "context": f"{product}-testing",
            "slug_template": "pr-{number}",
        },
        "updated_at": "2026-04-30T21:30:00Z",
        "source": "test",
    }


def _odoo_preview_profile_payload(product: str = "odoo-tenant-cm") -> dict[str, object]:
    return {
        "schema_version": 1,
        "product": product,
        "display_name": "CM Odoo",
        "repository": f"cbusillo/{product}",
        "driver_id": "odoo",
        "image": {"repository": f"ghcr.io/cbusillo/{product}"},
        "runtime_port": 8069,
        "health_path": "/web/health",
        "lanes": (
            {
                "instance": "testing",
                "context": "cm",
                "base_url": "https://cm-testing.example.com",
                "health_url": "https://cm-testing.example.com/web/health",
            },
        ),
        "preview": {
            "enabled": True,
            "context": "cm",
            "slug_template": "pr-{number}",
            "app_name_prefix": "cm-odoo-preview",
        },
        "updated_at": "2026-05-09T12:00:00Z",
        "source": "test",
    }


def _odoo_profile_payload_with_prod_lane(
    product: str = "odoo-tenant-cm",
) -> dict[str, object]:
    payload = _odoo_preview_profile_payload(product)
    lanes = list(cast(tuple[dict[str, object], ...], payload["lanes"]))
    lanes.append(
        {
            "instance": "prod",
            "context": "cm",
            "base_url": "https://cm.example.com",
            "health_url": "https://cm.example.com/web/health",
        }
    )
    payload["lanes"] = tuple(lanes)
    return payload


def _write_odoo_preview_template_runtime_environment(
    *, store: Any, context: str = "cm", instance: str = "testing"
) -> None:
    store.write_runtime_environment_record(
        RuntimeEnvironmentRecord(
            scope="instance",
            context=context,
            instance=instance,
            env={"ODOO_DB_USER": "odoo"},
            updated_at="2026-05-09T12:30:00Z",
            source_label="test",
        )
    )
    with patch.dict(
        os.environ,
        {control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"},
        clear=True,
    ):
        for name, binding_key, value in (
            ("db-password", "ODOO_DB_PASSWORD", "template-db-secret"),
            ("master-password", "ODOO_MASTER_PASSWORD", "template-master-secret"),
            ("admin-password", "ODOO_ADMIN_PASSWORD", "template-admin-secret"),
        ):
            control_plane_secrets.write_secret_value(
                record_store=store,
                scope="context_instance",
                integration=control_plane_secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
                name=name,
                plaintext_value=value,
                binding_key=binding_key,
                context_name=context,
                instance_name=instance,
                actor="test",
                source_label="test",
            )


def _product_profile_payload_with_prod(product: str = "sellyouroutboard") -> dict[str, object]:
    payload = _product_profile_payload(product)
    lanes = list(cast(tuple[dict[str, object], ...], payload["lanes"]))
    lanes.append(
        {
            "instance": "prod",
            "context": f"{product}-testing",
            "base_url": "https://www.sellyouroutboard.com",
            "health_url": "https://www.sellyouroutboard.com/api/health",
        }
    )
    payload["lanes"] = tuple(lanes)
    return payload


def _live_target_runtime_profile_payload(
    *,
    product: str = "sellyouroutboard",
    context: str = "sellyouroutboard",
    instance: str = "prod",
    include_context_secret: bool = False,
) -> dict[str, object]:
    payload = _product_profile_payload(product)
    payload["lanes"] = (
        {
            "instance": instance,
            "context": context,
            "base_url": "https://www.sellyouroutboard.com",
            "health_url": "https://www.sellyouroutboard.com/api/health",
        },
    )
    expected_config: dict[str, object] = {
        "runtime_environment_keys": [
            {"key": "GOOGLE_ANALYTICS_MEASUREMENT_ID", "context": context, "instance": instance}
        ],
        "managed_secret_bindings": [],
    }
    if include_context_secret:
        expected_config["managed_secret_bindings"] = [
            {"binding_key": "CONTEXT_API_TOKEN", "context": context}
        ]
    payload["expected_config"] = expected_config
    return payload


def _product_profile_lanes(payload: dict[str, object]) -> tuple[dict[str, object], ...]:
    return cast(tuple[dict[str, object], ...], payload["lanes"])


def _product_config_secrets(payload: dict[str, object]) -> list[dict[str, object]]:
    return cast(list[dict[str, object]], payload["secrets"])


def _passed_healthcheck_evidence(url: str) -> HealthcheckEvidence:
    return HealthcheckEvidence(
        verified=True,
        urls=(url,),
        timeout_seconds=45,
        status="pass",
    )


def _generic_site_profile_payload(product: str = "example-site") -> dict[str, object]:
    return {
        "schema_version": 1,
        "product": product,
        "display_name": "Example Site",
        "repository": f"every/{product}",
        "driver_id": "generic-web",
        "image": {"repository": f"ghcr.io/every/{product}"},
        "runtime_port": 3000,
        "health_path": "/healthz",
        "lanes": (
            {
                "instance": "testing",
                "context": product,
                "base_url": f"https://testing.{product}.example",
                "health_url": f"https://testing.{product}.example/healthz",
            },
            {
                "instance": "prod",
                "context": product,
                "base_url": f"https://{product}.example",
                "health_url": f"https://{product}.example/healthz",
            },
        ),
        "preview": {
            "enabled": True,
            "context": product,
            "slug_template": "pr-{number}",
        },
        "expected_config": {
            "runtime_environment_keys": [
                {"key": "INTERNAL_CALLBACK_URL", "context": product, "instance": "prod"},
                {"key": "RESEND_FROM_EMAIL", "context": product, "instance": "prod"},
            ],
            "managed_secret_bindings": [
                {"binding_key": "SMTP_PASSWORD", "context": product, "instance": "prod"},
                {"binding_key": "RESEND_API_KEY", "context": product, "instance": "prod"},
            ],
        },
        "updated_at": "2026-05-02T22:30:00Z",
        "source": "test",
    }


def _sqlite_database_url(database_path: Path) -> str:
    return f"sqlite+pysqlite:///{database_path}"


def create_launchplane_service_app(
    **kwargs: object,
) -> Callable[[dict[str, object], Callable[[str, list[tuple[str, str]]], None]], list[bytes]]:
    if "database_url" not in kwargs and "local_record_store_for_tests" not in kwargs:
        state_dir = kwargs.get("state_dir")
        if not isinstance(state_dir, Path):
            raise AssertionError("service tests must pass a pathlib state_dir")
        kwargs["local_record_store_for_tests"] = FilesystemRecordStore(state_dir=state_dir)
    database_url = kwargs.get("database_url")
    if isinstance(database_url, str) and database_url.startswith("sqlite"):
        store = PostgresRecordStore(database_url=database_url)
        store.ensure_schema()
        store.close()
    factory = cast(Any, _create_launchplane_service_app)
    return cast(
        Callable[[dict[str, object], Callable[[str, list[tuple[str, str]]], None]], list[bytes]],
        factory(**kwargs),
    )


def create_launchplane_fastapi_wsgi_app(
    **kwargs: object,
) -> Callable[[dict[str, object], Callable[[str, list[tuple[str, str]]], None]], list[bytes]]:
    kwargs.pop("state_dir", None)
    database_url = kwargs.get("database_url")
    if isinstance(database_url, str) and database_url.startswith("sqlite"):
        store = PostgresRecordStore(database_url=database_url)
        store.ensure_schema()
        store.close()
    factory = cast(Any, create_launchplane_fastapi_app)
    app = factory(**kwargs)
    return cast(
        Callable[[dict[str, object], Callable[[str, list[tuple[str, str]]], None]], list[bytes]],
        ASGIMiddleware(app),
    )


def create_every_code_github_webhook_app(
    **kwargs: object,
) -> Callable[[dict[str, object], Callable[[str, list[tuple[str, str]]], None]], list[bytes]]:
    state_dir = kwargs.pop("state_dir", None)
    local_record_store = kwargs.pop("local_record_store_for_tests", None)
    if local_record_store is None and "database_url" not in kwargs and isinstance(state_dir, Path):
        local_record_store = FilesystemRecordStore(state_dir=state_dir)
    if local_record_store is not None:
        kwargs["record_store_factory"] = lambda: local_record_store
    kwargs["every_code_github_webhook_handler"] = handle_every_code_github_webhook_request
    return create_launchplane_fastapi_wsgi_app(**kwargs)


def create_launchplane_dokploy_target_setup_app(**kwargs: object) -> Any:
    state_dir = kwargs.pop("state_dir", None)
    local_record_store = kwargs.pop("local_record_store_for_tests", None)
    database_url = kwargs.get("database_url")
    if isinstance(database_url, str) and database_url.startswith("sqlite"):
        store = PostgresRecordStore(database_url=database_url)
        store.ensure_schema()
        store.close()
    if "record_store_factory" not in kwargs and not isinstance(database_url, str):
        if local_record_store is None:
            if not isinstance(state_dir, Path):
                raise AssertionError("service tests must pass a pathlib state_dir")
            local_record_store = FilesystemRecordStore(state_dir=state_dir)

        def record_store_factory() -> object:
            return local_record_store

        kwargs["record_store_factory"] = record_store_factory
    factory = cast(Any, create_launchplane_fastapi_app)
    return factory(**kwargs)


def _write_runtime_key_safety_policy(
    *,
    database_url: str,
    context_name: str = "sellyouroutboard-prod",
    instance_name: str = "prod",
    rules: tuple[RuntimeSecretSafetyRule, ...] | None = None,
) -> None:
    store = PostgresRecordStore(database_url=database_url)
    store.ensure_schema()
    try:
        store.write_runtime_key_safety_policy_record(
            RuntimeKeySafetyPolicyRecord(
                record_id="runtime-key-safety-policy-service-test",
                status="active",
                source="test",
                updated_at="2026-05-05T20:00:00Z",
                rules=rules
                or (
                    RuntimeSecretSafetyRule(
                        binding_key="SMTP_PASSWORD",
                        secret_class="prod_only",
                        allowed_contexts=(context_name,),
                        allowed_instances=(instance_name,),
                    ),
                ),
            )
        )
    finally:
        store.close()


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
    return {"schema_version": 1, "product": product, "audit": audit_record.model_dump(mode="json")}


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
    return {"schema_version": 1, "product": product, "audit": audit_record.model_dump(mode="json")}


def _write_dokploy_managed_secrets(*, store: PostgresRecordStore) -> None:
    control_plane_secrets.write_secret_value(
        record_store=store,
        scope="global",
        integration=control_plane_secrets.DOKPLOY_SECRET_INTEGRATION,
        name="host",
        plaintext_value="https://dokploy.example.com",
        binding_key="DOKPLOY_HOST",
        actor="test",
    )
    control_plane_secrets.write_secret_value(
        record_store=store,
        scope="global",
        integration=control_plane_secrets.DOKPLOY_SECRET_INTEGRATION,
        name="token",
        plaintext_value="dokploy-token",
        binding_key="DOKPLOY_TOKEN",
        actor="test",
    )


def _seed_tracked_target_records(
    *,
    database_url: str,
    context: str,
    instance: str,
    target_id: str,
    target_type: Literal["compose", "application"],
    target_name: str,
    domains: tuple[str, ...] = (),
    deploy_timeout_seconds: int | None = None,
) -> None:
    store = PostgresRecordStore(database_url=database_url)
    store.ensure_schema()
    try:
        store.write_dokploy_target_record(
            DokployTargetRecord(
                context=context,
                instance=instance,
                target_type=target_type,
                target_name=target_name,
                deploy_timeout_seconds=deploy_timeout_seconds,
                domains=domains,
                updated_at="2026-05-01T00:00:00Z",
                source_label="test",
            )
        )
        store.write_dokploy_target_id_record(
            DokployTargetIdRecord(
                context=context,
                instance=instance,
                target_id=target_id,
                updated_at="2026-05-01T00:00:00Z",
                source_label="test",
            )
        )
    finally:
        store.close()


def _product_config_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "mode": "dry-run",
        "product": "sellyouroutboard",
        "context": "sellyouroutboard-prod",
        "instance": "prod",
        "source_label": "product-config-api-test",
        "runtime_env": {
            "scope": "instance",
            "env": {
                "CONTACT_EMAIL_MODE": "smtp",
                "SELLYOUROUTBOARD_SITE_URL": "https://www.sellyouroutboard.com",
            },
        },
        "secrets": [
            {
                "name": "SMTP_PASSWORD",
                "binding_key": "SMTP_PASSWORD",
                "value": "smtp-secret-value",
                "scope": "context_instance",
                "description": "SMTP password",
            }
        ],
    }


def _npmplus_ingress_route_payload(
    *, mode: str = "dry-run", context: str = "reon-prod", **overrides: object
) -> dict[str, object]:
    route: dict[str, object] = {
        "domain_names": ["ingress-canary.example.test"],
        "forward_scheme": "http",
        "forward_host": "192.0.2.10",
        "forward_port": 8123,
        "certificate_id": 47,
    }
    route.update(overrides)
    return {
        "schema_version": 1,
        "product": "launchplane",
        "context": context,
        "ingress": {
            "mode": mode,
            "route": route,
            "reason": "test ingress route apply",
        },
    }


def _npmplus_proxy_host(**overrides: object) -> NpmplusProxyHost:
    payload = NpmplusIngressRouteDesiredState(
        domain_names=("ingress-canary.example.test",),
        forward_scheme="https",
        forward_host="100.73.170.113",
        forward_port=443,
        certificate_id=47,
    ).to_proxy_host_payload()
    return NpmplusProxyHost.model_validate(
        {"id": 79, **payload.model_dump(mode="json"), "enabled": True, **overrides}
    )


def _edge_endpoint_record(*, status: EdgeEndpointStatus = "active") -> EdgeEndpointRecord:
    return EdgeEndpointRecord(
        endpoint_key="cm-prod-dokploy",
        provider="dokploy",
        server_name="docker-cm-prod",
        upstream_host="100.73.170.113",
        upstream_host_kind="ip",
        upstream_scheme="https",
        upstream_port=443,
        status=status,
        updated_at="2026-06-07T00:00:00Z",
        source_label="test:edge-endpoint",
    )


def _edge_endpoint_apply_payload(*, mode: str = "dry-run") -> dict[str, object]:
    return {
        "schema_version": 1,
        "mode": mode,
        "endpoint": _edge_endpoint_record().model_dump(mode="json"),
        "reason": "test edge endpoint apply",
        "confirmation": "APPLY LAUNCHPLANE EDGE ENDPOINT" if mode == "apply" else "",
    }


def _private_health_endpoint_record(
    *, url: str = "http://10.0.0.5:8000/health"
) -> PrivateHealthEndpointRecord:
    return PrivateHealthEndpointRecord(
        endpoint_key="repairshopr-sync-prod-runtime",
        product="repairshopr-sync",
        context="repairshopr-sync",
        instance="prod",
        url=url,
        updated_at="2026-06-15T00:00:00Z",
        source_label="test:private-health-endpoint",
    )


def _private_health_endpoint_apply_payload(*, mode: str = "dry-run") -> dict[str, object]:
    return {
        "schema_version": 1,
        "mode": mode,
        "endpoint": _private_health_endpoint_record().model_dump(mode="json"),
        "reason": "test private health endpoint apply",
        "confirmation": "APPLY LAUNCHPLANE PRIVATE HEALTH ENDPOINT" if mode == "apply" else "",
    }


def _ingress_canary_route_record(*, status: str = "active") -> IngressCanaryRouteRecord:
    return IngressCanaryRouteRecord(
        canary_key="ingress-canary",
        product="launchplane",
        context="reon-prod",
        domain_name="ingress-canary.example.test",
        expected_host_id=78,
        edge_endpoint_key="cm-prod-dokploy",
        certificate_id=47,
        status=status,  # type: ignore[arg-type]
        updated_at="2026-06-11T00:00:00Z",
        source_label="test:ingress-canary-route",
    )


def _ingress_canary_route_record_apply_payload(*, mode: str = "dry-run") -> dict[str, object]:
    return {
        "schema_version": 1,
        "mode": mode,
        "route": _ingress_canary_route_record().model_dump(mode="json"),
        "reason": "test ingress canary route record apply",
        "confirmation": "APPLY LAUNCHPLANE INGRESS CANARY ROUTE RECORD" if mode == "apply" else "",
    }


def _meta_product_config_payload(
    *, mode: str = "dry-run", reason: str | None = None
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "mode": mode,
        "product": "sellyouroutboard",
        "context": "sellyouroutboard",
        "instance": "prod",
        "source_label": "product-config-ui-test",
        "runtime_env": {
            "scope": "instance",
            "env": {
                "NEXT_PUBLIC_META_PIXEL_ID": "123456789012345",
            },
        },
        "secrets": [
            {
                "name": "META_CONVERSIONS_API_TOKEN",
                "binding_key": "META_CONVERSIONS_API_TOKEN",
                "value": "meta-conversions-api-secret-value",
                "scope": "context_instance",
                "description": "Meta conversions API token",
            }
        ],
    }
    if reason is not None:
        payload["reason"] = reason
    return payload


def _github_webhook_body_signature(body_bytes: bytes, secret: str) -> str:
    signature = hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()
    return f"sha256={signature}"


def _github_webhook_signature(payload: Mapping[str, object], secret: str) -> str:
    body_bytes = json.dumps(payload).encode("utf-8")
    return _github_webhook_body_signature(body_bytes, secret)


def _every_code_github_issue_labeled_payload(
    *,
    label: str = "every-code",
    action: str = "labeled",
    repository: str = "cbusillo/code",
    issue_number: int = 123,
    issue_url: str = "",
    closed_at: str = "",
    state_reason: str = "",
) -> dict[str, object]:
    resolved_issue_url = (
        issue_url.strip() or f"https://github.com/{repository}/issues/{issue_number}"
    )
    issue_payload = {
        "number": issue_number,
        "html_url": resolved_issue_url,
        "title": "Wire local automation",
    }
    if closed_at.strip():
        issue_payload["closed_at"] = closed_at
    if state_reason.strip():
        issue_payload["state_reason"] = state_reason
    return {
        "action": action,
        "label": {"name": label},
        "repository": {"full_name": repository},
        "issue": issue_payload,
        "sender": {"login": "cbusillo"},
    }


def _every_code_github_pull_request_closed_payload(
    *,
    repository: str = "cbusillo/code",
    pr_number: int = 26,
    merged: bool = True,
    closed_at: str = "2026-05-06T16:20:00Z",
    body: str = "",
) -> dict[str, object]:
    return {
        "action": "closed",
        "repository": {"full_name": repository},
        "pull_request": {
            "number": pr_number,
            "html_url": f"https://github.com/{repository}/pull/{pr_number}",
            "merged": merged,
            "closed_at": closed_at,
            "body": body,
        },
        "sender": {"login": "cbusillo"},
    }


def _seed_every_code_work_request_record(
    state_dir: Path,
    *,
    request_id: str = "every-code-cbusillo-code-123-test",
) -> EveryCodeWorkRequestRecord:
    record = EveryCodeWorkRequestRecord(
        request_id=request_id,
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
    FilesystemRecordStore(state_dir).write_every_code_work_request_record(record)
    return record


def _claim_every_code_work_request_record(
    record_store: Any,
    request_id: str,
    *,
    host: str = "Chris-Studio",
) -> EveryCodeWorkRequestRecord:
    record = record_store.claim_every_code_work_request_record(
        request_id=request_id,
        host=host,
        claimed_at="2026-05-05T22:01:00Z",
    )
    if record is None:
        raise AssertionError(f"Every Code work request {request_id} was not queued")
    return cast(EveryCodeWorkRequestRecord, record)


def _every_code_claim_fixture_response(
    record: EveryCodeWorkRequestRecord,
) -> tuple[int, dict[str, Any]]:
    return (
        202,
        {
            "records": {"request_id": record.request_id, "state": record.state},
            "result": {"request": record.model_dump(mode="json")},
        },
    )


def _claim_every_code_work_request_in_filesystem(
    state_dir: Path,
    request_id: str,
    *,
    host: str = "Chris-Studio",
) -> tuple[int, dict[str, Any]]:
    record = _claim_every_code_work_request_record(
        FilesystemRecordStore(state_dir),
        request_id,
        host=host,
    )
    return _every_code_claim_fixture_response(record)


def _claim_every_code_work_request_in_postgres(
    database_url: str,
    request_id: str,
    *,
    host: str = "Chris-Studio",
) -> tuple[int, dict[str, Any]]:
    store = PostgresRecordStore(database_url=database_url)
    try:
        record = _claim_every_code_work_request_record(store, request_id, host=host)
    finally:
        store.close()
    return _every_code_claim_fixture_response(record)


def _update_every_code_work_request_status_record(
    record_store: Any,
    request_id: str,
    *,
    state: Literal["running", "done", "blocked"] = "running",
    host: str = "Chris-Studio",
    updated_at: str = "2026-05-05T22:02:00Z",
    result_pr_url: str = "",
    result_summary: str = "",
    error_message: str = "",
) -> EveryCodeWorkRequestRecord:
    record = record_store.read_every_code_work_request_record(request_id)
    updated = apply_every_code_work_request_status(
        record,
        EveryCodeWorkRequestStatusUpdate(
            state=state,
            host=host,
            updated_at=updated_at,
            result_pr_url=result_pr_url,
            result_summary=result_summary,
            error_message=error_message,
        ),
    )
    record_store.write_every_code_work_request_record(updated)
    return updated


def _every_code_status_fixture_response(
    record: EveryCodeWorkRequestRecord,
) -> tuple[int, dict[str, Any]]:
    return (
        202,
        {
            "records": {"request_id": record.request_id, "state": record.state},
            "result": {"request": record.model_dump(mode="json"), "notifications": []},
        },
    )


def _update_every_code_work_request_status_in_filesystem(
    state_dir: Path,
    request_id: str,
    *,
    state: Literal["running", "done", "blocked"] = "running",
    host: str = "Chris-Studio",
    updated_at: str = "2026-05-05T22:02:00Z",
    result_pr_url: str = "",
    result_summary: str = "",
    error_message: str = "",
) -> tuple[int, dict[str, Any]]:
    record = _update_every_code_work_request_status_record(
        FilesystemRecordStore(state_dir),
        request_id,
        state=state,
        host=host,
        updated_at=updated_at,
        result_pr_url=result_pr_url,
        result_summary=result_summary,
        error_message=error_message,
    )
    return _every_code_status_fixture_response(record)


def _update_every_code_work_request_status_in_postgres(
    database_url: str,
    request_id: str,
    *,
    state: Literal["running", "done", "blocked"] = "running",
    host: str = "Chris-Studio",
    updated_at: str = "2026-05-05T22:02:00Z",
    result_pr_url: str = "",
    result_summary: str = "",
    error_message: str = "",
) -> tuple[int, dict[str, Any]]:
    store = PostgresRecordStore(database_url=database_url)
    try:
        record = _update_every_code_work_request_status_record(
            store,
            request_id,
            state=state,
            host=host,
            updated_at=updated_at,
            result_pr_url=result_pr_url,
            result_summary=result_summary,
            error_message=error_message,
        )
    finally:
        store.close()
    return _every_code_status_fixture_response(record)


def _every_code_github_pr_comment_payload(
    *,
    repository: str = "cbusillo/code",
    pr_number: int = 26,
    body: str = "Please tighten this wording before merge.",
    comment_id: int = 1001,
    issue_body: str = "",
    sender: str = "cbusillo",
    sender_type: str = "User",
) -> dict[str, object]:
    return {
        "action": "created",
        "repository": {"full_name": repository},
        "issue": {
            "number": pr_number,
            "html_url": f"https://github.com/{repository}/pull/{pr_number}",
            "body": issue_body,
            "pull_request": {"url": f"https://api.github.com/repos/{repository}/pulls/{pr_number}"},
        },
        "comment": {
            "id": comment_id,
            "node_id": f"IC_kwDO_test_{comment_id}",
            "html_url": f"https://github.com/{repository}/pull/{pr_number}#issuecomment-{comment_id}",
            "body": body,
            "author_association": "OWNER",
            "user": {"login": sender, "type": sender_type},
        },
        "sender": {"login": sender, "type": sender_type},
    }


def _every_code_github_issue_comment_payload(
    *,
    repository: str = "cbusillo/code",
    issue_number: int = 123,
    issue_author: str = "Mbanks89",
    sender: str = "Mbanks89",
    body: str = "/preview ok",
    comment_id: int = 2001,
) -> dict[str, object]:
    return {
        "action": "created",
        "repository": {"full_name": repository},
        "issue": {
            "number": issue_number,
            "html_url": f"https://github.com/{repository}/issues/{issue_number}",
            "title": "Wire local automation",
            "user": {"login": issue_author},
        },
        "comment": {
            "id": comment_id,
            "node_id": f"IC_kwDO_issue_{comment_id}",
            "html_url": f"https://github.com/{repository}/issues/{issue_number}#issuecomment-{comment_id}",
            "body": body,
            "author_association": "CONTRIBUTOR",
            "user": {"login": sender, "type": "User"},
        },
        "sender": {"login": sender, "type": "User"},
    }


def _work_graph_snapshot_payload() -> dict[str, object]:
    return {
        "generated_at": "2026-05-06T01:45:00Z",
        "repos": [
            {
                "repository": "cbusillo/launchplane",
                "classification": "managed_runtime",
                "product": "launchplane",
                "display_name": "Launchplane",
            }
        ],
        "issues": [
            {
                "repository": "cbusillo/launchplane",
                "number": 190,
                "title": "Build operator work graph",
                "url": "https://github.com/cbusillo/launchplane/issues/190",
                "focus": "Now",
                "manager": "Code",
                "finish_line": "Ranked work queue is available to the operator UI.",
                "labels": ["plan", "plan:active"],
                "blocking": 2,
                "subissues_total": 2,
                "subissues_completed": 1,
                "check_state": "success",
                "deploy_state": "success",
            },
            {
                "repository": "cbusillo/launchplane",
                "number": 164,
                "title": "Absorb product orchestration",
                "url": "https://github.com/cbusillo/launchplane/issues/164",
                "state": "closed",
                "focus": "Done",
            },
        ],
    }


def _invoke_app(
    app: WsgiApp,
    *,
    method: str,
    path: str,
    query_string: str = "",
    payload: Mapping[str, object] | None = None,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    body_bytes = json.dumps(payload).encode("utf-8") if payload is not None else b""
    environ: dict[str, object] = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": query_string,
        "CONTENT_LENGTH": str(len(body_bytes)),
        "CONTENT_TYPE": "application/json" if payload is not None else "",
        "wsgi.input": io.BytesIO(body_bytes),
        "HTTP_AUTHORIZATION": authorization,
        "SERVER_NAME": "testserver",
        "SERVER_PORT": "80",
        "wsgi.url_scheme": "http",
    }
    for header_name, header_value in (headers or {}).items():
        environ[f"HTTP_{header_name.upper().replace('-', '_')}"] = header_value
    captured_status = ""

    def start_response(
        status: str,
        _response_headers: list[tuple[str, str]],
        _exc_info: object | None = None,
    ) -> None:
        nonlocal captured_status
        captured_status = status

    response_body = b"".join(app(environ, start_response))
    response_payload = json.loads(response_body.decode("utf-8"))
    assert isinstance(response_payload, dict)
    return int(captured_status.split(" ", 1)[0]), cast(dict[str, Any], response_payload)


def _write_github_planning_config(
    root: Path,
    *,
    repo_managers: dict[str, str] | None = None,
    default_manager: str = "@cellmechanic",
    path: str = ".code/github-planning.json",
) -> Path:
    config_path = root / Path(path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(
            {
                "workflow": {
                    "default_manager": default_manager,
                    "repo_managers": repo_managers or {},
                }
            }
        ),
        encoding="utf-8",
    )
    return config_path


def _invoke_raw_app(
    app: WsgiApp,
    *,
    method: str,
    path: str,
    authorization: str = "",
    query_string: str = "",
    headers: dict[str, str] | None = None,
    body_bytes: bytes = b"",
) -> tuple[int, dict[str, str], bytes]:
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": query_string,
        "CONTENT_LENGTH": str(len(body_bytes)),
        "wsgi.input": io.BytesIO(body_bytes),
        "HTTP_AUTHORIZATION": authorization,
        "SERVER_NAME": "testserver",
        "SERVER_PORT": "80",
        "wsgi.url_scheme": "http",
    }
    for header_name, header_value in (headers or {}).items():
        environ[f"HTTP_{header_name.upper().replace('-', '_')}"] = header_value
    captured_status = ""
    captured_headers: list[tuple[str, str]] = []

    def start_response(
        status: str,
        response_headers: list[tuple[str, str]],
        _exc_info: object | None = None,
    ) -> None:
        nonlocal captured_status, captured_headers
        captured_status = status
        captured_headers = response_headers

    response_body = b"".join(app(environ, start_response))
    return (
        int(captured_status.split(" ", 1)[0]),
        dict(captured_headers),
        response_body,
    )


@dataclass(frozen=True)
class _AsgiServiceTestResponse:
    status_code: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


async def _asgi_get_for_service_test(app: Any, path: str) -> _AsgiServiceTestResponse:
    return await _asgi_request_for_service_test(app, method="GET", path=path)


async def _asgi_request_for_service_test(
    app: Any,
    *,
    method: str,
    path: str,
    payload: Mapping[str, object] | None = None,
    authorization: str = "",
    headers: Mapping[str, str] | None = None,
) -> _AsgiServiceTestResponse:
    body = json.dumps(payload).encode("utf-8") if payload is not None else b""
    request_headers = [
        (key.lower().encode("ascii"), value.encode("latin-1"))
        for key, value in (headers or {}).items()
    ]
    if authorization:
        request_headers.append((b"authorization", authorization.encode("latin-1")))
    if body:
        request_headers.append((b"content-type", b"application/json"))
        request_headers.append((b"content-length", str(len(body)).encode("ascii")))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": request_headers,
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "root_path": "",
    }
    messages = [
        {"type": "http.request", "body": body, "more_body": False},
    ]
    sent: list[MutableMapping[str, Any]] = []

    async def receive() -> dict[str, Any]:
        if messages:
            return messages.pop(0)
        return {"type": "http.disconnect"}

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(message)

    await app(scope, receive, send)

    start = next(message for message in sent if message["type"] == "http.response.start")
    body = b"".join(
        message.get("body", b"") for message in sent if message["type"] == "http.response.body"
    )
    response_headers = {
        key.decode("latin-1"): value.decode("latin-1") for key, value in start.get("headers", [])
    }
    return _AsgiServiceTestResponse(
        status_code=start["status"],
        headers=response_headers,
        body=body,
    )


def _invoke_dokploy_target_setup_app(
    app: Any,
    *,
    method: str = "POST",
    path: str = "/v1/dokploy-targets/setup",
    payload: Mapping[str, object],
    authorization: str = "Bearer valid-token",
    headers: Mapping[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    if method != "POST" or path != "/v1/dokploy-targets/setup":
        raise AssertionError("Dokploy target setup tests must call the setup route")
    response = asyncio.run(
        _asgi_request_for_service_test(
            app,
            method=method,
            path=path,
            payload=payload,
            authorization=authorization,
            headers=headers,
        )
    )
    response_payload = response.json()
    assert isinstance(response_payload, dict)
    return response.status_code, cast(dict[str, Any], response_payload)


class GitHubHumanAuthTests(unittest.TestCase):
    def test_github_oauth_config_loads_bootstrap_admin_emails(self) -> None:
        with patch.dict(
            os.environ,
            {
                "LAUNCHPLANE_GITHUB_CLIENT_ID": "client-id",
                "LAUNCHPLANE_GITHUB_CLIENT_SECRET": "client-secret",
                "LAUNCHPLANE_PUBLIC_URL": "https://launchplane.example/",
                "LAUNCHPLANE_SESSION_SECRET": "session-secret",
                "LAUNCHPLANE_COOKIE_SECURE": "false",
                "LAUNCHPLANE_BOOTSTRAP_ADMIN_EMAILS": " Info@ShinyComputers.com, ops@example.com ",
            },
            clear=True,
        ):
            config = load_github_oauth_config_from_env()

        self.assertIsNotNone(config)
        assert config is not None
        self.assertFalse(config.cookie_secure)
        self.assertEqual(config.public_url, "https://launchplane.example")
        self.assertIn("user:email", config.scopes)
        self.assertEqual(
            config.bootstrap_admin_emails,
            frozenset({"info@shinycomputers.com", "ops@example.com"}),
        )

    def test_github_oauth_bootstrap_admin_can_use_verified_private_email(self) -> None:
        config = GitHubOAuthConfig(
            client_id="client-id",
            client_secret="client-secret",
            public_url="https://launchplane.example",
            session_secret="session-secret",
            bootstrap_admin_emails=frozenset({"info@shinycomputers.com"}),
        )
        oauth_session = _FakeOAuth2Session(
            {
                GITHUB_USER_URL: {
                    "login": "bootstrapper",
                    "id": 987,
                    "name": "Bootstrap Operator",
                    "email": None,
                },
                GITHUB_ORGS_URL: [],
                GITHUB_TEAMS_URL: [],
                GITHUB_EMAILS_URL: [
                    {
                        "email": "info@shinycomputers.com",
                        "primary": True,
                        "verified": True,
                    },
                    {
                        "email": "unverified@example.com",
                        "primary": False,
                        "verified": False,
                    },
                ],
            }
        )
        client = GitHubOAuthClient(config)

        with patch.object(GitHubOAuthClient, "_new_session", return_value=oauth_session):
            identity = client.fetch_identity(
                code="github-code",
                code_verifier="verifier",
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_humans": []}),
            )

        self.assertEqual(identity.login, "bootstrapper")
        self.assertEqual(identity.email, "info@shinycomputers.com")
        self.assertEqual(identity.role, "admin")
        self.assertIn(GITHUB_EMAILS_URL, oauth_session.requested_urls)

    def test_auth_session_family_legacy_wsgi_routes_are_retired(self) -> None:
        with TemporaryDirectory() as tmpdir:
            app = create_launchplane_service_app(
                state_dir=Path(tmpdir) / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                github_oauth_config=_github_oauth_config(),
            )

            responses = tuple(
                _invoke_app(app, method=method, path=path, authorization="")
                for method, path in (
                    ("GET", "/auth/github/login"),
                    ("GET", "/auth/github/callback"),
                    ("GET", "/v1/auth/session"),
                    ("POST", "/auth/logout"),
                )
            )

        for status_code, payload in responses:
            self.assertEqual(status_code, 404)
            self.assertEqual(payload["error"]["code"], "not_found")

    def test_human_session_does_not_authorize_post_mutations(self) -> None:
        policy = LaunchplaneAuthzPolicy.model_validate(
            {"github_humans": [{"logins": ["alice"], "roles": ["admin"]}]}
        )
        session_store = InMemoryHumanSessionStore()
        with TemporaryDirectory() as tmpdir:
            app = create_launchplane_service_app(
                state_dir=Path(tmpdir),
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                github_oauth_config=_github_oauth_config(),
                human_session_store=session_store,
            )
            cookie = _signed_in_cookie(session_store, role="admin")
            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/verireel/preview-inventory",
                payload={"schema_version": 1, "context": "verireel-testing"},
                authorization="",
                headers={"Cookie": cookie},
            )

        self.assertEqual(status_code, 401)
        self.assertEqual(payload["error"]["code"], "authentication_required")


class LaunchplaneServiceTests(unittest.TestCase):
    def test_npmplus_ingress_route_apply_legacy_wsgi_fallback_is_removed(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            app = create_launchplane_service_app(
                state_dir=Path(temporary_directory_name) / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                control_plane_root_path=Path(temporary_directory_name),
            )

            responses = tuple(
                _invoke_app(
                    app,
                    method=method,
                    path="/v1/drivers/ingress/route-apply",
                    payload={"schema_version": 1},
                )
                for method in ("GET", "POST")
            )

        for status_code, payload in responses:
            self.assertEqual(status_code, 404)
            self.assertEqual(payload["error"]["code"], "not_found")

    def test_endpoint_apply_legacy_wsgi_fallback_is_removed(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            app = create_launchplane_service_app(
                state_dir=Path(temporary_directory_name) / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                control_plane_root_path=Path(temporary_directory_name),
            )

            responses = tuple(
                _invoke_app(app, method=method, path=path, payload={"schema_version": 1})
                for method in ("GET", "POST")
                for path in (
                    "/v1/edge-endpoints/apply",
                    "/v1/private-health-endpoints/apply",
                )
            )

        for status_code, payload in responses:
            self.assertEqual(status_code, 404)
            self.assertEqual(payload["error"]["code"], "not_found")

    def test_ingress_canary_apply_legacy_wsgi_fallback_is_removed(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            app = create_launchplane_service_app(
                state_dir=Path(temporary_directory_name) / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                control_plane_root_path=Path(temporary_directory_name),
            )

            responses = tuple(
                _invoke_app(app, method=method, path=path, payload={"schema_version": 1})
                for method in ("GET", "POST")
                for path in (
                    "/v1/ingress/canary-routes/records/apply",
                    "/v1/ingress/canary-routes/apply",
                )
            )

        for status_code, payload in responses:
            self.assertEqual(status_code, 404)
            self.assertEqual(payload["error"]["code"], "not_found")

    def test_ingress_route_audit_record_reads_are_retired_from_legacy_wsgi_app(
        self,
    ) -> None:
        policy = LaunchplaneAuthzPolicy.model_validate({"github_actions": []})
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
                local_record_store_for_tests=FilesystemRecordStore(root / "state"),
            )

            list_status_code, list_payload = _invoke_app(
                app,
                method="GET",
                path="/v1/ingress/route-audits/records",
                query_string="product=launchplane&context=reon-prod",
            )
            read_status_code, read_payload = _invoke_app(
                app,
                method="GET",
                path="/v1/ingress/route-audits/records/ingress-route-audit-test",
                query_string="product=launchplane&context=reon-prod",
            )

        self.assertEqual(list_status_code, 404)
        self.assertEqual(read_status_code, 404)
        self.assertEqual(list_payload["error"]["code"], "not_found")
        self.assertEqual(read_payload["error"]["code"], "not_found")

    def test_public_ingress_monitor_run_once_legacy_wsgi_fallback_is_removed(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            app = create_launchplane_service_app(
                state_dir=Path(temporary_directory_name) / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                control_plane_root_path=Path(temporary_directory_name),
            )

            for method in ("GET", "POST"):
                with self.subTest(method=method):
                    status_code, payload = _invoke_app(
                        app,
                        method=method,
                        path="/v1/products/public-ingress-monitor/run-once",
                        payload={"schema_version": 1, "product": "launchplane"},
                    )

                    self.assertEqual(status_code, 404)
                    self.assertEqual(payload["error"]["code"], "not_found")

    def test_notification_policy_apply_legacy_wsgi_fallback_is_removed(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            app = create_launchplane_service_app(
                state_dir=Path(temporary_directory_name) / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                control_plane_root_path=Path(temporary_directory_name),
            )

            responses = tuple(
                _invoke_app(app, method=method, path=path, payload={"schema_version": 1})
                for method in ("GET", "POST")
                for path in (
                    "/v1/public-ingress/notification-policies/apply",
                    "/v1/every-code/notification-policies/apply",
                    "/v1/previews/pr-feedback/notification-policies/apply",
                )
            )

        for status_code, payload in responses:
            self.assertEqual(status_code, 404)
            self.assertEqual(payload["error"]["code"], "not_found")

    def test_preview_lifecycle_plan_legacy_wsgi_fallback_is_removed(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            app = create_launchplane_service_app(
                state_dir=Path(temporary_directory_name) / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                control_plane_root_path=Path(temporary_directory_name),
            )

            responses = tuple(
                _invoke_app(
                    app,
                    method=method,
                    path="/v1/previews/lifecycle-plan",
                    payload={"schema_version": 1},
                )
                for method in ("GET", "POST")
            )

        for status_code, payload in responses:
            self.assertEqual(status_code, 404)
            self.assertEqual(payload["error"]["code"], "not_found")

    def test_runtime_key_safety_policy_apply_legacy_wsgi_fallback_is_removed(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            app = create_launchplane_service_app(
                state_dir=Path(temporary_directory_name) / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                control_plane_root_path=Path(temporary_directory_name),
            )

            responses = tuple(
                _invoke_app(
                    app,
                    method=method,
                    path="/v1/runtime-key-safety/policies/apply",
                    payload={"schema_version": 1},
                )
                for method in ("GET", "POST")
            )

        for status_code, payload in responses:
            self.assertEqual(status_code, 404)
            self.assertEqual(payload["error"]["code"], "not_found")

    def test_every_code_blocked_status_posts_discord_notification(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        sent_payloads: list[tuple[str, dict[str, object]]] = []

        def send_discord(webhook_url: str, payload: dict[str, object]) -> None:
            sent_payloads.append((webhook_url, payload))

        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret,
                    control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: (
                        "test-master-key"
                    ),
                },
                clear=True,
            ),
        ):
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            app = create_every_code_github_webhook_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_every_code_worker_identity()),
                authz_policy=_every_code_worker_policy(
                    extra_actions=("every_code_notification_attempt.read",)
                ),
                control_plane_root_path=root,
                database_url=database_url,
            )
            store = PostgresRecordStore(database_url=database_url)
            try:
                secret_result = control_plane_secrets.write_secret_value(
                    record_store=store,
                    scope="context_instance",
                    integration="every-code-notifications",
                    name="discord webhook",
                    plaintext_value="https://discord.com/api/webhooks/test/webhook",
                    binding_key="DISCORD_WEBHOOK",
                    context_name="launchplane",
                    instance_name="every-code",
                    actor="test",
                    source_label="test",
                )
                store.write_every_code_notification_policy_record(
                    EveryCodeNotificationPolicyRecord(
                        policy_id="every-code-notification-discord",
                        repository="cbusillo/code",
                        status="enabled",
                        created_at="2026-06-14T18:10:00Z",
                        updated_at="2026-06-14T18:10:00Z",
                        source="test",
                        destinations=(
                            EveryCodeNotificationDestination(
                                destination_id="discord",
                                kind="discord",
                                discord_webhook_secret=str(secret_result["secret_id"]),
                            ),
                        ),
                    )
                )
            finally:
                store.close()

            webhook_payload = _every_code_github_issue_labeled_payload()
            create_status, create_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=webhook_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-blocked-notify",
                    "X-Hub-Signature-256": _github_webhook_signature(webhook_payload, secret),
                },
            )
            request_id = str(create_payload["records"]["request_id"])
            claim_status, _claim_payload = _claim_every_code_work_request_in_postgres(
                database_url,
                request_id,
            )
            store = PostgresRecordStore(database_url=database_url)
            try:
                blocked_record = _update_every_code_work_request_status_record(
                    store,
                    request_id,
                    state="blocked",
                    error_message="Every Code bot auth actor mismatch.",
                )
                notification_attempts = deliver_every_code_blocked_notifications(
                    record_store=store,
                    request=blocked_record,
                    attempted_at="2026-05-05T22:03:00Z",
                    discord_sender=send_discord,
                )
                attempts = store.list_every_code_notification_attempt_records(
                    request_id=request_id,
                    event="work_request_blocked",
                )
            finally:
                store.close()

        self.assertEqual(create_status, 202)
        self.assertEqual(claim_status, 202)
        self.assertEqual(blocked_record.state, "blocked")
        self.assertEqual(len(sent_payloads), 1)
        webhook_url, discord_payload = sent_payloads[0]
        self.assertEqual(webhook_url, "https://discord.com/api/webhooks/test/webhook")
        self.assertIn("embeds", discord_payload)
        self.assertEqual(notification_attempts[0].delivery_status, "delivered")
        self.assertEqual(attempts[0].delivery_status, "delivered")

    def test_every_code_blocked_status_records_discord_failure(self) -> None:
        secret = "launchplane-every-code-webhook-secret"

        def send_discord(_webhook_url: str, _payload: dict[str, object]) -> None:
            raise RuntimeError("discord unavailable")

        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret,
                    control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: (
                        "test-master-key"
                    ),
                },
                clear=True,
            ),
        ):
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            app = create_every_code_github_webhook_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_every_code_worker_identity()),
                authz_policy=_every_code_worker_policy(
                    extra_actions=("every_code_notification_attempt.read",)
                ),
                control_plane_root_path=root,
                database_url=database_url,
            )
            store = PostgresRecordStore(database_url=database_url)
            try:
                secret_result = control_plane_secrets.write_secret_value(
                    record_store=store,
                    scope="context_instance",
                    integration="every-code-notifications",
                    name="discord webhook",
                    plaintext_value="https://discord.com/api/webhooks/test/webhook",
                    binding_key="DISCORD_WEBHOOK",
                    context_name="launchplane",
                    instance_name="every-code",
                    actor="test",
                    source_label="test",
                )
                store.write_every_code_notification_policy_record(
                    EveryCodeNotificationPolicyRecord(
                        policy_id="every-code-notification-discord",
                        repository="cbusillo/code",
                        status="enabled",
                        created_at="2026-06-14T18:10:00Z",
                        updated_at="2026-06-14T18:10:00Z",
                        source="test",
                        destinations=(
                            EveryCodeNotificationDestination(
                                destination_id="discord",
                                kind="discord",
                                discord_webhook_secret=str(secret_result["secret_id"]),
                            ),
                        ),
                    )
                )
            finally:
                store.close()

            webhook_payload = _every_code_github_issue_labeled_payload(issue_number=124)
            create_status, create_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=webhook_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-blocked-notify-failed",
                    "X-Hub-Signature-256": _github_webhook_signature(webhook_payload, secret),
                },
            )
            request_id = str(create_payload["records"]["request_id"])
            claim_status, _claim_payload = _claim_every_code_work_request_in_postgres(
                database_url,
                request_id,
            )
            store = PostgresRecordStore(database_url=database_url)
            try:
                blocked_record = _update_every_code_work_request_status_record(
                    store,
                    request_id,
                    state="blocked",
                    error_message="Every Code bot claim comment failed.",
                )
                notification_attempts = deliver_every_code_blocked_notifications(
                    record_store=store,
                    request=blocked_record,
                    attempted_at="2026-05-05T22:03:00Z",
                    discord_sender=send_discord,
                )
                attempts = store.list_every_code_notification_attempt_records(
                    request_id=request_id,
                    event="work_request_blocked",
                )
            finally:
                store.close()

        self.assertEqual(create_status, 202)
        self.assertEqual(claim_status, 202)
        self.assertEqual(blocked_record.state, "blocked")
        self.assertEqual(notification_attempts[0].delivery_status, "failed")
        self.assertIn("discord unavailable", notification_attempts[0].error_message)
        self.assertEqual(attempts[0].delivery_status, "failed")
        self.assertIn("discord unavailable", attempts[0].error_message)

    def test_every_code_discord_delivery_keeps_pending_marker_on_attempt_write_failure(
        self,
    ) -> None:
        store = _FlakyEveryCodeNotificationStore()
        policy_record = EveryCodeNotificationPolicyRecord(
            policy_id="every-code-notification-discord",
            repository="cbusillo/code",
            status="enabled",
            created_at="2026-06-14T18:10:00Z",
            updated_at="2026-06-14T18:10:00Z",
            source="test",
            destinations=(
                EveryCodeNotificationDestination(
                    destination_id="discord",
                    kind="discord",
                    discord_webhook_secret="secret-discord-webhook",
                ),
            ),
        )
        store.write_every_code_notification_policy_record(policy_record)
        request_record = EveryCodeWorkRequestRecord(
            request_id="every-code-cbusillo-code-123-test",
            source="github_issue_label",
            state="blocked",
            repository="cbusillo/code",
            issue_number=123,
            issue_url="https://github.com/cbusillo/code/issues/123",
            trigger_label="every-code",
            queued_at="2026-06-14T18:00:00Z",
            updated_at="2026-06-14T18:10:00Z",
            claimed_at="2026-06-14T18:01:00Z",
            claimed_by_host="Chris-Studio",
            started_at="2026-06-14T18:10:00Z",
            finished_at="2026-06-14T18:10:00Z",
            error_message="Every Code bot claim comment failed.",
        )
        sent_payloads: list[tuple[str, dict[str, object]]] = []

        def send_discord(webhook_url: str, payload: dict[str, object]) -> None:
            sent_payloads.append((webhook_url, payload))

        store.fail_delivered_write = True
        first_attempt = deliver_every_code_discord_notification(
            notification_store=store,
            secret_resolver=lambda _secret_id: "https://discord.com/api/webhooks/test/webhook",
            request=request_record,
            policy=policy_record,
            destination=policy_record.destinations[0],
            attempted_at="2026-06-14T18:11:00Z",
            discord_sender=send_discord,
        )
        second_attempt = deliver_every_code_discord_notification(
            notification_store=store,
            secret_resolver=lambda _secret_id: "https://discord.com/api/webhooks/test/webhook",
            request=request_record,
            policy=policy_record,
            destination=policy_record.destinations[0],
            attempted_at="2026-06-14T18:12:00Z",
            discord_sender=send_discord,
        )

        self.assertEqual(len(sent_payloads), 1)
        self.assertEqual(first_attempt.delivery_status, "pending")
        self.assertEqual(second_attempt.delivery_status, "pending")
        self.assertEqual(store.attempts[0].delivery_status, "pending")

    def test_every_code_blocked_notification_requeues_get_distinct_attempts(self) -> None:
        store = _FlakyEveryCodeNotificationStore()
        policy_record = EveryCodeNotificationPolicyRecord(
            policy_id="every-code-notification-discord",
            repository="cbusillo/code",
            status="enabled",
            created_at="2026-06-14T18:00:00Z",
            updated_at="2026-06-14T18:00:00Z",
            destinations=(
                EveryCodeNotificationDestination(
                    destination_id="discord",
                    kind="discord",
                    discord_webhook_secret="secret-every-code-discord",
                ),
            ),
        )
        store.write_every_code_notification_policy_record(policy_record)
        first_blocked_request = EveryCodeWorkRequestRecord(
            request_id="every-code-cbusillo-code-123-test",
            source="github_issue_label",
            state="blocked",
            repository="cbusillo/code",
            issue_number=123,
            issue_url="https://github.com/cbusillo/code/issues/123",
            trigger_label="every-code",
            queued_at="2026-06-14T18:00:00Z",
            updated_at="2026-06-14T18:10:00Z",
            claimed_at="2026-06-14T18:01:00Z",
            claimed_by_host="Chris-Studio",
            started_at="2026-06-14T18:10:00Z",
            finished_at="2026-06-14T18:10:00Z",
            error_message="Every Code bot auth actor mismatch.",
        )
        requeued_request = requeue_every_code_work_request(
            first_blocked_request,
            queued_at="2026-06-14T18:00:00Z",
            trigger_actor="cbusillo",
        )
        second_blocked_request = requeued_request.model_copy(
            update={
                "state": "blocked",
                "updated_at": "2026-06-14T18:10:00Z",
                "claimed_at": "2026-06-14T18:21:00Z",
                "claimed_by_host": "Chris-Studio",
                "started_at": "2026-06-14T18:25:00Z",
                "finished_at": "2026-06-14T18:10:00Z",
                "error_message": "Every Code bot auth actor mismatch again.",
            }
        )
        sent_payloads: list[tuple[str, dict[str, object]]] = []

        def send_discord(webhook_url: str, payload: dict[str, object]) -> None:
            sent_payloads.append((webhook_url, payload))

        first_attempt = deliver_every_code_discord_notification(
            notification_store=store,
            secret_resolver=lambda _secret_id: "https://discord.com/api/webhooks/test/webhook",
            request=first_blocked_request,
            policy=policy_record,
            destination=policy_record.destinations[0],
            attempted_at="2026-06-14T18:11:00Z",
            discord_sender=send_discord,
        )
        repeated_attempt = deliver_every_code_discord_notification(
            notification_store=store,
            secret_resolver=lambda _secret_id: "https://discord.com/api/webhooks/test/webhook",
            request=first_blocked_request,
            policy=policy_record,
            destination=policy_record.destinations[0],
            attempted_at="2026-06-14T18:12:00Z",
            discord_sender=send_discord,
        )
        second_attempt = deliver_every_code_discord_notification(
            notification_store=store,
            secret_resolver=lambda _secret_id: "https://discord.com/api/webhooks/test/webhook",
            request=second_blocked_request,
            policy=policy_record,
            destination=policy_record.destinations[0],
            attempted_at="2026-06-14T18:31:00Z",
            discord_sender=send_discord,
        )

        self.assertEqual(len(sent_payloads), 2)
        self.assertEqual(first_attempt.delivery_status, "delivered")
        self.assertEqual(repeated_attempt.attempt_id, first_attempt.attempt_id)
        self.assertNotEqual(second_attempt.attempt_id, first_attempt.attempt_id)
        self.assertEqual(second_attempt.delivery_status, "delivered")
        self.assertEqual(len(store.attempts), 2)

    def test_public_webhook_url_guard_rejects_non_public_ip_literals(self) -> None:
        self.assertEqual(
            public_discord_url_error("http://169.254.169.254/latest/meta-data"),
            "private_url",
        )
        self.assertEqual(public_discord_url_error("http://224.0.0.1/hook"), "private_url")
        self.assertEqual(public_discord_url_error("http://0.0.0.0/hook"), "private_url")
        self.assertEqual(public_url_error("https://example.com/health"), "")
        self.assertEqual(public_url_error("https://93.184.216.34/health"), "")
        self.assertEqual(
            public_discord_url_error("https://93.184.216.34/api/webhooks/test/webhook"),
            "invalid_url",
        )
        self.assertEqual(public_discord_url_error("https://example.com/hook"), "invalid_url")
        self.assertEqual(
            public_discord_url_error("https://discord.com/api/webhooks/test/webhook"),
            "",
        )

    def test_merge_train_reads_are_retired_from_legacy_wsgi_app(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            _seed_merge_train_policy(state_dir)
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_merge_train_service_identity()),
                authz_policy=_merge_train_service_policy(),
                control_plane_root_path=Path(temporary_directory_name),
            )
            routes = (
                "/v1/work-graph/merge-train/admission",
                "/v1/work-graph/merge-train/controller/status",
                "/v1/work-graph/merge-train/policy-targets",
            )

            for route in routes:
                status_code, payload = _invoke_app(
                    app,
                    method="GET",
                    path=route,
                    query_string="repository=cbusillo/sellyouroutboard&base_branch=main",
                )
                self.assertEqual(status_code, 404)
                self.assertEqual(payload["error"]["code"], "not_found")

    def test_merge_train_policy_import_endpoint_writes_active_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
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
                            "actions": ["merge_train.policy_import"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_wsgi_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )
            record = build_test_merge_train_policy_record(
                repository="cbusillo/codex-skills",
                record_id="merge-train-policy-codex-skills-service-import",
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/merge-train/policies/import",
                payload={
                    "schema_version": 1,
                    "product": "launchplane",
                    "mode": "apply",
                    "reason": "Configure codex-skills merge train.",
                    "record": record.model_dump(mode="json"),
                },
                headers={"Idempotency-Key": "merge-train-policy:codex-skills"},
            )
            store = PostgresRecordStore(database_url=database_url)
            try:
                records = store.list_merge_train_policy_records(status="active")
            finally:
                store.close()

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["result"]["mode"], "apply")
        self.assertEqual(payload["result"]["record"]["policy_keys"], ["cbusillo/codex-skills:main"])
        self.assertEqual([stored.record_id for stored in records], [record.record_id])

    def test_merge_train_policy_import_endpoint_dry_run_does_not_write_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            identity = _identity(
                repository="cbusillo/launchplane",
                workflow_ref=(
                    "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main"
                ),
                event_name="workflow_dispatch",
            )
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
                            "actions": ["merge_train.policy_import"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_wsgi_app(
                state_dir=root / "state",
                verifier=_StubVerifier(identity),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )
            record = build_test_merge_train_policy_record(
                repository="cbusillo/codex-skills",
                record_id="merge-train-policy-codex-skills-service-dry-run",
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/merge-train/policies/import",
                payload={
                    "schema_version": 1,
                    "product": "launchplane",
                    "mode": "dry_run",
                    "record": record.model_dump(mode="json"),
                },
                headers={"Idempotency-Key": "merge-train-policy:dry-run"},
            )
            store = PostgresRecordStore(database_url=database_url)
            records = store.list_merge_train_policy_records(status="active")
            try:
                idempotency_record = store.read_idempotency_record(
                    scope="|".join(
                        (
                            identity.repository,
                            identity.workflow_ref,
                            identity.subject,
                        )
                    ),
                    route_path="/v1/merge-train/policies/import",
                    idempotency_key="merge-train-policy:dry-run",
                )
            except FileNotFoundError:
                idempotency_record = None
            finally:
                store.close()

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["result"]["mode"], "dry_run")
        self.assertEqual(records, ())
        self.assertIsNone(idempotency_record)

    def test_merge_train_policy_import_endpoint_human_admin_session_can_apply(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            session_store = InMemoryHumanSessionStore()
            session_manager = HumanSessionManager(
                config=_github_oauth_config(),
                session_store=session_store,
            )
            human_session = session_manager.issue(_human_identity(role="admin"))
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_humans": [
                        {
                            "logins": ["alice"],
                            "roles": ["admin"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["merge_train.policy_import"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_wsgi_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
                human_session_manager=session_manager,
            )
            record = build_test_merge_train_policy_record(
                repository="cbusillo/codex-skills",
                record_id="merge-train-policy-codex-skills-human-import",
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/merge-train/policies/import",
                payload={
                    "schema_version": 1,
                    "product": "launchplane",
                    "mode": "apply",
                    "reason": "Configure codex-skills merge train.",
                    "record": record.model_dump(mode="json"),
                },
                authorization="",
                headers={
                    "Cookie": session_manager.session_cookie_header(human_session),
                    "Idempotency-Key": "merge-train-policy:human-import",
                },
            )
            store = PostgresRecordStore(database_url=database_url)
            try:
                records = store.list_merge_train_policy_records(status="active")
            finally:
                store.close()

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["result"]["mode"], "apply")
        self.assertEqual([stored.record_id for stored in records], [record.record_id])

    def test_merge_train_policy_import_endpoint_rejects_self_deploy_authority(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
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
            app = create_launchplane_fastapi_wsgi_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )
            record = build_test_merge_train_policy_record(
                repository="cbusillo/codex-skills",
                record_id="merge-train-policy-codex-skills-self-deploy-denied",
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/merge-train/policies/import",
                payload={
                    "schema_version": 1,
                    "product": "launchplane",
                    "mode": "dry_run",
                    "record": record.model_dump(mode="json"),
                },
                headers={"Idempotency-Key": "merge-train-policy:self-deploy-denied"},
            )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_merge_train_policy_import_endpoint_rejects_non_launchplane_product(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/launchplane",
                            "workflow_refs": [
                                "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["launchplane", "other-product"],
                            "contexts": ["launchplane"],
                            "actions": ["merge_train.policy_import"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_wsgi_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )
            record = build_test_merge_train_policy_record(
                repository="cbusillo/codex-skills",
                record_id="merge-train-policy-codex-skills-wrong-product",
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/merge-train/policies/import",
                payload={
                    "schema_version": 1,
                    "product": "other-product",
                    "mode": "apply",
                    "reason": "Attempt cross-product policy import.",
                    "record": record.model_dump(mode="json"),
                },
                headers={"Idempotency-Key": "merge-train-policy:wrong-product"},
            )

        self.assertEqual(status_code, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")

    def test_merge_train_policy_import_endpoint_authorizes_before_storage_gate(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
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
            app = create_launchplane_fastapi_wsgi_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
                record_store_factory=lambda: FilesystemRecordStore(state_dir=root / "state"),
            )
            record = build_test_merge_train_policy_record(
                repository="cbusillo/codex-skills",
                record_id="merge-train-policy-codex-skills-authz-before-storage",
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/merge-train/policies/import",
                payload={
                    "schema_version": 1,
                    "product": "launchplane",
                    "mode": "dry_run",
                    "record": record.model_dump(mode="json"),
                },
            )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_merge_train_policy_import_endpoint_requires_database_storage(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
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
                            "actions": ["merge_train.policy_import"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_wsgi_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
                record_store_factory=lambda: FilesystemRecordStore(state_dir=root / "state"),
            )
            record = build_test_merge_train_policy_record(
                repository="cbusillo/codex-skills",
                record_id="merge-train-policy-codex-skills-db-required",
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/merge-train/policies/import",
                payload={
                    "schema_version": 1,
                    "product": "launchplane",
                    "mode": "apply",
                    "reason": "Configure codex-skills merge train.",
                    "record": record.model_dump(mode="json"),
                },
                headers={"Idempotency-Key": "merge-train-policy:db-required"},
            )

        self.assertEqual(status_code, 503)
        self.assertEqual(payload["error"]["code"], "database_required")

    def test_openapi_includes_merge_train_policy_import_contract(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=LaunchplaneAuthzPolicy(),
            record_store_factory=lambda: FilesystemRecordStore(state_dir=Path("unused")),
        )

        payload = app.openapi()

        route = payload["paths"]["/v1/merge-train/policies/import"]["post"]
        self.assertEqual(route["operationId"], "import_merge_train_policy")
        self.assertEqual(route["responses"]["202"]["description"], "Successful Response")
        request_schema = route["requestBody"]["content"]["application/json"]["schema"]
        self.assertEqual(request_schema["title"], "MergeTrainPolicyImportEnvelope")
        self.assertEqual(request_schema["additionalProperties"], False)

    def test_merge_train_policy_import_endpoint_is_retired_from_legacy_wsgi_app(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                control_plane_root_path=root,
            )
            record = build_test_merge_train_policy_record(
                repository="cbusillo/codex-skills",
                record_id="merge-train-policy-codex-skills-legacy-retired",
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/merge-train/policies/import",
                payload={
                    "schema_version": 1,
                    "product": "launchplane",
                    "mode": "dry_run",
                    "record": record.model_dump(mode="json"),
                },
            )

        self.assertEqual(status_code, 404)
        self.assertEqual(payload["error"]["code"], "not_found")

    def test_every_code_github_webhook_creates_work_request(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "github_actions": [
                    {
                        "repository": "cbusillo/launchplane",
                        "workflow_refs": [
                            "cbusillo/launchplane/.github/workflows/every-code-worker.yml@refs/heads/main"
                        ],
                        "event_names": ["workflow_dispatch"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": ["every_code_work_request.read"],
                    }
                ]
            }
        )
        identity = _identity(
            repository="cbusillo/launchplane",
            workflow_ref="cbusillo/launchplane/.github/workflows/every-code-worker.yml@refs/heads/main",
            event_name="workflow_dispatch",
        )
        webhook_payload = _every_code_github_issue_labeled_payload(label="EVERY-CODE")
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret,
                    "LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN": "dev-worker-token",
                },
            ),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            app = create_every_code_github_webhook_app(
                state_dir=state_dir,
                verifier=_StubVerifier(identity),
                authz_policy=policy,
                control_plane_root_path=Path(temporary_directory_name),
            )
            webhook_status, webhook_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=webhook_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-1",
                    "X-Hub-Signature-256": _github_webhook_signature(webhook_payload, secret),
                },
            )
            work_requests = FilesystemRecordStore(state_dir).list_every_code_work_request_records(
                state="queued"
            )

        self.assertEqual(webhook_status, 202)
        self.assertFalse(webhook_response["deduped"])
        self.assertEqual(webhook_response["records"]["state"], "queued")
        self.assertEqual(webhook_response["github_delivery_id"], "delivery-1")
        self.assertEqual(len(work_requests), 1)
        request = work_requests[0]
        self.assertEqual(request.source, "github_issue_label")
        self.assertEqual(request.repository, "cbusillo/code")
        self.assertEqual(request.issue_number, 123)
        self.assertEqual(request.trigger_label, "every-code")
        self.assertEqual(request.trigger_actor, "cbusillo")
        self.assertEqual(request.github_delivery_id, "delivery-1")

    def test_every_code_github_webhook_dedupes_existing_request(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "github_actions": [
                    {
                        "repository": "cbusillo/launchplane",
                        "workflow_refs": [
                            "cbusillo/launchplane/.github/workflows/every-code-worker.yml@refs/heads/main"
                        ],
                        "event_names": ["workflow_dispatch"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": [
                            "every_code_work_request.read",
                            "every_code_work_request.claim",
                        ],
                    }
                ]
            }
        )
        identity = _identity(
            repository="cbusillo/launchplane",
            workflow_ref="cbusillo/launchplane/.github/workflows/every-code-worker.yml@refs/heads/main",
            event_name="workflow_dispatch",
        )
        webhook_payload = _every_code_github_issue_labeled_payload()
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret,
                    "LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN": "dev-worker-token",
                },
            ),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            app = create_every_code_github_webhook_app(
                state_dir=state_dir,
                verifier=_StubVerifier(identity),
                authz_policy=policy,
                control_plane_root_path=Path(temporary_directory_name),
            )
            first_status, first_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=webhook_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-1",
                    "X-Hub-Signature-256": _github_webhook_signature(webhook_payload, secret),
                },
            )
            request_id = str(first_payload["records"]["request_id"])
            claim_status, _claim_payload = _claim_every_code_work_request_in_filesystem(
                state_dir,
                request_id,
            )
            second_status, second_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=webhook_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-2",
                    "X-Hub-Signature-256": _github_webhook_signature(webhook_payload, secret),
                },
            )
            stored_request = FilesystemRecordStore(state_dir).read_every_code_work_request_record(
                request_id
            )

        self.assertEqual(first_status, 202)
        self.assertEqual(claim_status, 202)
        self.assertEqual(second_status, 202)
        self.assertTrue(second_payload["deduped"])
        self.assertEqual(second_payload["records"]["state"], "claimed")
        self.assertEqual(stored_request.state, "claimed")
        self.assertEqual(stored_request.github_delivery_id, "delivery-1")

    def test_every_code_github_webhook_is_retired_from_legacy_wsgi_app(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        webhook_payload = _every_code_github_issue_labeled_payload()
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {"LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret},
            ),
        ):
            app = create_launchplane_service_app(
                state_dir=Path(temporary_directory_name) / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=Path(temporary_directory_name),
            )
            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=webhook_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-1",
                    "X-Hub-Signature-256": _github_webhook_signature(webhook_payload, secret),
                },
            )

        self.assertEqual(status_code, 404)
        self.assertEqual(payload["error"]["code"], "not_found")

    def test_every_code_summary_read_route_is_retired_from_legacy_wsgi_app(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            app = create_launchplane_service_app(
                state_dir=Path(temporary_directory_name) / "state",
                verifier=_StubVerifier(_every_code_worker_identity()),
                authz_policy=_every_code_worker_policy(),
                control_plane_root_path=Path(temporary_directory_name),
            )
            summary_status, summary_payload = _invoke_app(
                app,
                method="GET",
                path="/v1/every-code/summary",
                query_string="repository=cbusillo/code&issue_number=123&state=queued",
            )

        self.assertEqual(summary_status, 404)
        self.assertEqual(summary_payload["error"]["code"], "not_found")

    def test_every_code_work_request_read_routes_are_retired_from_legacy_wsgi_app(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            app = create_launchplane_service_app(
                state_dir=Path(temporary_directory_name) / "state",
                verifier=_StubVerifier(_every_code_worker_identity()),
                authz_policy=_every_code_worker_policy(),
                control_plane_root_path=Path(temporary_directory_name),
            )
            list_status, list_payload = _invoke_app(
                app,
                method="GET",
                path="/v1/every-code/work-requests",
            )
            read_status, read_payload = _invoke_app(
                app,
                method="GET",
                path="/v1/every-code/work-requests/every-code-cbusillo-code-123-test",
            )

        self.assertEqual(list_status, 404)
        self.assertEqual(read_status, 404)
        self.assertEqual(list_payload["error"]["code"], "not_found")
        self.assertEqual(read_payload["error"]["code"], "not_found")

    def test_preview_readiness_read_route_is_retired_from_legacy_wsgi_app(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_every_code_worker_identity()),
                authz_policy=_every_code_worker_policy(
                    extra_actions=("every_code_preview_gate.read",)
                ),
                control_plane_root_path=root,
            )
            status_code, payload = _invoke_app(
                app,
                method="GET",
                path="/v1/previews/readiness",
                query_string="repository=cbusillo/code&pr_number=31&status=blocked",
            )

        self.assertEqual(status_code, 404)
        self.assertEqual(payload["error"]["code"], "not_found")

    def test_every_code_auxiliary_read_routes_are_retired_from_legacy_wsgi_app(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            app = create_launchplane_service_app(
                state_dir=Path(temporary_directory_name) / "state",
                verifier=_StubVerifier(_every_code_worker_identity()),
                authz_policy=_every_code_worker_policy(
                    extra_actions=(
                        "every_code_preview_gate.read",
                        "every_code_notification_attempt.read",
                        "preview_pr_feedback_notification_attempt.read",
                    )
                ),
                control_plane_root_path=Path(temporary_directory_name),
            )
            responses = {
                path: _invoke_app(app, method="GET", path=path)
                for path in (
                    "/v1/every-code/pr-feedback",
                    "/v1/every-code/preview-gates",
                    "/v1/every-code/notification-attempts",
                    "/v1/previews/pr-feedback/notification-attempts",
                )
            }

        self.assertEqual(responses["/v1/every-code/pr-feedback"][0], 404)
        self.assertEqual(responses["/v1/every-code/preview-gates"][0], 404)
        self.assertEqual(responses["/v1/every-code/notification-attempts"][0], 404)
        self.assertEqual(
            responses["/v1/previews/pr-feedback/notification-attempts"][0],
            404,
        )

    def test_every_code_github_webhook_dedupes_finished_request(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "github_actions": [
                    {
                        "repository": "cbusillo/launchplane",
                        "workflow_refs": [
                            "cbusillo/launchplane/.github/workflows/every-code-worker.yml@refs/heads/main"
                        ],
                        "event_names": ["workflow_dispatch"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": [
                            "every_code_work_request.read",
                            "every_code_work_request.claim",
                            "every_code_work_request.update",
                        ],
                    }
                ]
            }
        )
        identity = _identity(
            repository="cbusillo/launchplane",
            workflow_ref="cbusillo/launchplane/.github/workflows/every-code-worker.yml@refs/heads/main",
            event_name="workflow_dispatch",
        )
        webhook_payload = _every_code_github_issue_labeled_payload()
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret,
                    "LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN": "dev-worker-token",
                },
            ),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            app = create_every_code_github_webhook_app(
                state_dir=state_dir,
                verifier=_StubVerifier(identity),
                authz_policy=policy,
                control_plane_root_path=Path(temporary_directory_name),
            )
            first_status, first_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=webhook_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-1",
                    "X-Hub-Signature-256": _github_webhook_signature(webhook_payload, secret),
                },
            )
            request_id = str(first_payload["records"]["request_id"])
            claim_status, _claim_payload = _claim_every_code_work_request_in_filesystem(
                state_dir,
                request_id,
            )
            done_status, _done_payload = _update_every_code_work_request_status_in_filesystem(
                state_dir,
                request_id,
                state="done",
                result_pr_url="https://github.com/cbusillo/code/pull/26",
            )
            second_status, second_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=webhook_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-2",
                    "X-Hub-Signature-256": _github_webhook_signature(webhook_payload, secret),
                },
            )

        self.assertEqual(first_status, 202)
        self.assertEqual(claim_status, 202)
        self.assertEqual(done_status, 202)
        self.assertEqual(second_status, 202)
        self.assertTrue(second_payload["deduped"])
        self.assertEqual(second_payload["records"]["state"], "done")
        self.assertEqual(
            second_payload["result"]["request"]["result_pr_url"],
            "https://github.com/cbusillo/code/pull/26",
        )

    def test_every_code_issue_close_marks_linked_request_done(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "github_actions": [
                    {
                        "repository": "cbusillo/launchplane",
                        "workflow_refs": [
                            "cbusillo/launchplane/.github/workflows/every-code-worker.yml@refs/heads/main"
                        ],
                        "event_names": ["workflow_dispatch"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": [
                            "every_code_work_request.read",
                            "every_code_work_request.claim",
                            "every_code_work_request.update",
                        ],
                    }
                ]
            }
        )
        identity = _identity(
            repository="cbusillo/launchplane",
            workflow_ref="cbusillo/launchplane/.github/workflows/every-code-worker.yml@refs/heads/main",
            event_name="workflow_dispatch",
        )
        issue_payload = _every_code_github_issue_labeled_payload()
        close_payload = _every_code_github_issue_labeled_payload(
            action="closed",
            closed_at="2026-05-06T16:20:00Z",
            state_reason="completed",
        )
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret,
                    "LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN": "dev-worker-token",
                },
            ),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            app = create_every_code_github_webhook_app(
                state_dir=state_dir,
                verifier=_StubVerifier(identity),
                authz_policy=policy,
                control_plane_root_path=Path(temporary_directory_name),
            )
            create_status, create_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=issue_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-issue",
                    "X-Hub-Signature-256": _github_webhook_signature(issue_payload, secret),
                },
            )
            request_id = str(create_payload["records"]["request_id"])
            _claim_every_code_work_request_in_filesystem(state_dir, request_id)
            _update_every_code_work_request_status_in_filesystem(
                state_dir,
                request_id,
                state="running",
            )

            close_status, closed_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=close_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-issue-close",
                    "X-Hub-Signature-256": _github_webhook_signature(close_payload, secret),
                },
            )

        self.assertEqual(create_status, 202)
        self.assertEqual(close_status, 202)
        self.assertEqual(closed_response["records"]["state"], "done")
        self.assertEqual(
            closed_response["result"]["request"]["finished_at"],
            "2026-05-06T16:20:00Z",
        )
        self.assertEqual(
            closed_response["result"]["request"]["result_summary"],
            "Source issue closed (completed): https://github.com/cbusillo/code/issues/123",
        )

    def test_every_code_pull_request_close_marks_linked_request_done(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "github_actions": [
                    {
                        "repository": "cbusillo/launchplane",
                        "workflow_refs": [
                            "cbusillo/launchplane/.github/workflows/every-code-worker.yml@refs/heads/main"
                        ],
                        "event_names": ["workflow_dispatch"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": [
                            "every_code_work_request.read",
                            "every_code_work_request.claim",
                            "every_code_work_request.update",
                        ],
                    }
                ]
            }
        )
        identity = _identity(
            repository="cbusillo/launchplane",
            workflow_ref="cbusillo/launchplane/.github/workflows/every-code-worker.yml@refs/heads/main",
            event_name="workflow_dispatch",
        )
        issue_payload = _every_code_github_issue_labeled_payload()
        pr_payload = _every_code_github_pull_request_closed_payload()
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret,
                    "LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN": "dev-worker-token",
                },
            ),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            app = create_every_code_github_webhook_app(
                state_dir=state_dir,
                verifier=_StubVerifier(identity),
                authz_policy=policy,
                control_plane_root_path=Path(temporary_directory_name),
            )
            create_status, create_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=issue_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-issue",
                    "X-Hub-Signature-256": _github_webhook_signature(issue_payload, secret),
                },
            )
            request_id = str(create_payload["records"]["request_id"])
            _claim_every_code_work_request_in_filesystem(state_dir, request_id)
            _update_every_code_work_request_status_in_filesystem(
                state_dir,
                request_id,
                state="running",
                result_pr_url="https://github.com/cbusillo/code/pull/26",
            )

            close_status, close_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=pr_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "delivery-pr-close",
                    "X-Hub-Signature-256": _github_webhook_signature(pr_payload, secret),
                },
            )

        self.assertEqual(create_status, 202)
        self.assertEqual(close_status, 202)
        self.assertEqual(close_payload["records"]["state"], "done")
        self.assertEqual(close_payload["result"]["request"]["finished_at"], "2026-05-06T16:20:00Z")
        self.assertEqual(close_payload["result"]["request"]["error_message"], "")
        self.assertEqual(
            close_payload["result"]["request"]["result_summary"],
            "Linked pull request merged: https://github.com/cbusillo/code/pull/26",
        )

    def test_every_code_pull_request_close_blocks_unmerged_linked_request(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "github_actions": [
                    {
                        "repository": "cbusillo/launchplane",
                        "workflow_refs": [
                            "cbusillo/launchplane/.github/workflows/every-code-worker.yml@refs/heads/main"
                        ],
                        "event_names": ["workflow_dispatch"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": [
                            "every_code_work_request.read",
                            "every_code_work_request.claim",
                            "every_code_work_request.update",
                        ],
                    }
                ]
            }
        )
        identity = _identity(
            repository="cbusillo/launchplane",
            workflow_ref="cbusillo/launchplane/.github/workflows/every-code-worker.yml@refs/heads/main",
            event_name="workflow_dispatch",
        )
        issue_payload = _every_code_github_issue_labeled_payload()
        pr_payload = _every_code_github_pull_request_closed_payload(merged=False)
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {"LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret},
            ),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            app = create_every_code_github_webhook_app(
                state_dir=state_dir,
                verifier=_StubVerifier(identity),
                authz_policy=policy,
                control_plane_root_path=Path(temporary_directory_name),
            )
            create_status, create_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=issue_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-issue",
                    "X-Hub-Signature-256": _github_webhook_signature(issue_payload, secret),
                },
            )
            request_id = str(create_payload["records"]["request_id"])
            _claim_every_code_work_request_in_filesystem(state_dir, request_id)
            _update_every_code_work_request_status_in_filesystem(
                state_dir,
                request_id,
                state="running",
                result_pr_url="https://github.com/cbusillo/code/pull/26",
            )

            close_status, close_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=pr_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "delivery-pr-close",
                    "X-Hub-Signature-256": _github_webhook_signature(pr_payload, secret),
                },
            )

        self.assertEqual(create_status, 202)
        self.assertEqual(close_status, 202)
        self.assertEqual(close_payload["records"]["state"], "blocked")
        self.assertIn("closed without merge", close_payload["result"]["request"]["error_message"])

    def test_every_code_pull_request_close_matches_issue_url_without_result_pr_url(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "github_actions": [
                    {
                        "repository": "cbusillo/launchplane",
                        "workflow_refs": [
                            "cbusillo/launchplane/.github/workflows/every-code-worker.yml@refs/heads/main"
                        ],
                        "event_names": ["workflow_dispatch"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": [
                            "every_code_work_request.read",
                            "every_code_work_request.claim",
                            "every_code_work_request.update",
                        ],
                    }
                ]
            }
        )
        identity = _identity(
            repository="cbusillo/launchplane",
            workflow_ref="cbusillo/launchplane/.github/workflows/every-code-worker.yml@refs/heads/main",
            event_name="workflow_dispatch",
        )
        issue_payload = _every_code_github_issue_labeled_payload(issue_number=64)
        pr_payload = _every_code_github_pull_request_closed_payload(
            pr_number=71,
            body="Closes #64",
        )
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {"LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret},
            ),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            app = create_every_code_github_webhook_app(
                state_dir=state_dir,
                verifier=_StubVerifier(identity),
                authz_policy=policy,
                control_plane_root_path=Path(temporary_directory_name),
            )
            create_status, create_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=issue_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-feedback-close-issue",
                    "X-Hub-Signature-256": _github_webhook_signature(issue_payload, secret),
                },
            )
            request_id = str(create_payload["records"]["request_id"])
            _claim_every_code_work_request_in_filesystem(state_dir, request_id)
            _update_every_code_work_request_status_in_filesystem(
                state_dir,
                request_id,
                state="running",
            )
            close_status, close_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=pr_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "delivery-feedback-close-pr",
                    "X-Hub-Signature-256": _github_webhook_signature(pr_payload, secret),
                },
            )

        self.assertEqual(create_status, 202)
        self.assertEqual(close_status, 202)
        self.assertEqual(close_payload["records"]["state"], "done")
        self.assertEqual(
            close_payload["result"]["request"]["result_pr_url"],
            "https://github.com/cbusillo/code/pull/71",
        )

    def test_every_code_pull_request_close_matches_all_linked_issue_urls(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "github_actions": [
                    {
                        "repository": "cbusillo/launchplane",
                        "workflow_refs": [
                            "cbusillo/launchplane/.github/workflows/every-code-worker.yml@refs/heads/main"
                        ],
                        "event_names": ["workflow_dispatch"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": ["every_code_work_request.read"],
                    }
                ]
            }
        )
        identity = _identity(
            repository="cbusillo/launchplane",
            workflow_ref="cbusillo/launchplane/.github/workflows/every-code-worker.yml@refs/heads/main",
            event_name="workflow_dispatch",
        )
        pr_payload = _every_code_github_pull_request_closed_payload(
            pr_number=71,
            body="Closes #64, closes #65",
        )
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret,
                    "LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN": "dev-worker-token",
                },
            ),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            app = create_every_code_github_webhook_app(
                state_dir=state_dir,
                verifier=_StubVerifier(identity),
                authz_policy=policy,
                control_plane_root_path=Path(temporary_directory_name),
            )
            for issue_number in (64, 65):
                issue_payload = _every_code_github_issue_labeled_payload(issue_number=issue_number)
                _invoke_app(
                    app,
                    method="POST",
                    path="/v1/every-code/github-webhook",
                    payload=issue_payload,
                    authorization="",
                    headers={
                        "X-GitHub-Event": "issues",
                        "X-GitHub-Delivery": f"delivery-multi-close-{issue_number}",
                        "X-Hub-Signature-256": _github_webhook_signature(issue_payload, secret),
                    },
                )

            close_status, close_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=pr_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "delivery-multi-close-pr",
                    "X-Hub-Signature-256": _github_webhook_signature(pr_payload, secret),
                },
            )
            persisted_requests = FilesystemRecordStore(
                state_dir
            ).list_every_code_work_request_records()

        self.assertEqual(close_status, 202)
        self.assertEqual(close_payload["result"]["closed_count"], 2)
        closed_requests = close_payload["result"]["requests"]
        self.assertEqual({request["issue_number"] for request in closed_requests}, {64, 65})
        self.assertTrue(all(request["state"] == "done" for request in closed_requests))
        self.assertTrue(
            all(
                request["claimed_by_host"] == "github-pull-request-close"
                for request in closed_requests
            )
        )
        self.assertEqual(
            {request.state for request in persisted_requests},
            {"done"},
        )

    def test_every_code_pull_request_close_does_not_match_issue_by_pr_number(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "github_actions": [
                    {
                        "repository": "cbusillo/launchplane",
                        "workflow_refs": [
                            "cbusillo/launchplane/.github/workflows/every-code-worker.yml@refs/heads/main"
                        ],
                        "event_names": ["workflow_dispatch"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": [
                            "every_code_work_request.read",
                            "every_code_work_request.claim",
                            "every_code_work_request.update",
                        ],
                    }
                ]
            }
        )
        identity = _identity(
            repository="cbusillo/launchplane",
            workflow_ref="cbusillo/launchplane/.github/workflows/every-code-worker.yml@refs/heads/main",
            event_name="workflow_dispatch",
        )
        issue_payload = _every_code_github_issue_labeled_payload(issue_number=26)
        pr_payload = _every_code_github_pull_request_closed_payload(pr_number=26)
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {"LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret},
            ),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            app = create_every_code_github_webhook_app(
                state_dir=state_dir,
                verifier=_StubVerifier(identity),
                authz_policy=policy,
                control_plane_root_path=Path(temporary_directory_name),
            )
            create_status, create_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=issue_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-pr-number-issue",
                    "X-Hub-Signature-256": _github_webhook_signature(issue_payload, secret),
                },
            )
            request_id = str(create_payload["records"]["request_id"])
            _claim_every_code_work_request_in_filesystem(state_dir, request_id)
            _update_every_code_work_request_status_in_filesystem(
                state_dir,
                request_id,
                state="running",
            )
            close_status, close_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=pr_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "delivery-pr-number-close",
                    "X-Hub-Signature-256": _github_webhook_signature(pr_payload, secret),
                },
            )

        self.assertEqual(create_status, 202)
        self.assertEqual(close_status, 202)
        self.assertTrue(close_payload["skipped"])
        self.assertEqual(close_payload["reason"], "linked_every_code_request_not_found")

    def test_every_code_pull_request_close_pages_beyond_newest_requests(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "github_actions": [
                    {
                        "repository": "cbusillo/launchplane",
                        "workflow_refs": [
                            "cbusillo/launchplane/.github/workflows/every-code-worker.yml@refs/heads/main"
                        ],
                        "event_names": ["workflow_dispatch"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": [
                            "every_code_work_request.read",
                            "every_code_work_request.claim",
                            "every_code_work_request.update",
                        ],
                    }
                ]
            }
        )
        identity = _identity(
            repository="cbusillo/launchplane",
            workflow_ref="cbusillo/launchplane/.github/workflows/every-code-worker.yml@refs/heads/main",
            event_name="workflow_dispatch",
        )
        target_pr_payload = _every_code_github_pull_request_closed_payload(pr_number=26)
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {"LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret},
            ),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            app = create_every_code_github_webhook_app(
                state_dir=state_dir,
                verifier=_StubVerifier(identity),
                authz_policy=policy,
                control_plane_root_path=Path(temporary_directory_name),
            )
            for issue_number in range(1, 103):
                issue_payload = _every_code_github_issue_labeled_payload(issue_number=issue_number)
                _create_status, create_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/every-code/github-webhook",
                    payload=issue_payload,
                    authorization="",
                    headers={
                        "X-GitHub-Event": "issues",
                        "X-GitHub-Delivery": f"delivery-page-issue-{issue_number}",
                        "X-Hub-Signature-256": _github_webhook_signature(issue_payload, secret),
                    },
                )
                request_id = str(create_payload["records"]["request_id"])
                _claim_every_code_work_request_in_filesystem(state_dir, request_id)
                _update_every_code_work_request_status_in_filesystem(
                    state_dir,
                    request_id,
                    state="running",
                    result_pr_url=f"https://github.com/cbusillo/code/pull/{issue_number}",
                )

            close_status, close_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=target_pr_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "delivery-page-pr-close",
                    "X-Hub-Signature-256": _github_webhook_signature(target_pr_payload, secret),
                },
            )

        self.assertEqual(close_status, 202)
        self.assertEqual(close_payload["records"]["state"], "done")
        self.assertEqual(
            close_payload["result"]["request"]["result_pr_url"],
            "https://github.com/cbusillo/code/pull/26",
        )

    def test_every_code_pull_request_close_ignores_unlinked_pr(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        identity = _identity(
            repository="cbusillo/launchplane",
            workflow_ref="cbusillo/launchplane/.github/workflows/every-code-worker.yml@refs/heads/main",
            event_name="workflow_dispatch",
        )
        pr_payload = _every_code_github_pull_request_closed_payload(pr_number=999)
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {"LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret},
            ),
        ):
            app = create_every_code_github_webhook_app(
                state_dir=Path(temporary_directory_name) / "state",
                verifier=_StubVerifier(identity),
                authz_policy=LaunchplaneAuthzPolicy(),
                control_plane_root_path=Path(temporary_directory_name),
            )
            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=pr_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "delivery-pr-close",
                    "X-Hub-Signature-256": _github_webhook_signature(pr_payload, secret),
                },
            )

        self.assertEqual(status_code, 202)
        self.assertTrue(payload["skipped"])
        self.assertEqual(payload["reason"], "linked_every_code_request_not_found")

    def test_every_code_pr_comment_webhook_records_feedback(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        issue_payload = _every_code_github_issue_labeled_payload()
        comment_payload = _every_code_github_pr_comment_payload()
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret,
                    "LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN": "dev-worker-token",
                },
            ),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            app = create_every_code_github_webhook_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                control_plane_root_path=Path(temporary_directory_name),
            )
            issue_status, issue_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=issue_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-issue",
                    "X-Hub-Signature-256": _github_webhook_signature(issue_payload, secret),
                },
            )
            request_id = issue_response["records"]["request_id"]
            claim_status, claim_payload = _claim_every_code_work_request_in_filesystem(
                state_dir,
                str(request_id),
            )
            status_status, _status_payload = _update_every_code_work_request_status_in_filesystem(
                state_dir,
                str(request_id),
                state="done",
                result_pr_url="https://github.com/cbusillo/code/pull/26",
                result_summary="Opened PR.",
                updated_at="2026-05-06T16:00:00Z",
            )
            feedback_status, feedback_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=comment_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issue_comment",
                    "X-GitHub-Delivery": "delivery-comment",
                    "X-Hub-Signature-256": _github_webhook_signature(comment_payload, secret),
                },
            )

        self.assertEqual(issue_status, 202)
        self.assertEqual(claim_status, 202)
        self.assertEqual(status_status, 202)
        self.assertEqual(claim_payload["result"]["request"]["state"], "claimed")
        self.assertEqual(feedback_status, 202)
        self.assertEqual(feedback_response["records"]["request_id"], request_id)
        feedback = feedback_response["result"]["feedback"]
        self.assertEqual(feedback["request_id"], request_id)
        self.assertEqual(feedback["feedback_kind"], "issue_comment")
        self.assertEqual(feedback["body"], "Please tighten this wording before merge.")

    def test_every_code_preview_ok_comment_marks_pr_ready_to_merge(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        issue_payload = _every_code_github_issue_labeled_payload(
            repository="cbusillo/sellyouroutboard",
            issue_number=82,
        )
        comment_payload = _every_code_github_issue_comment_payload(
            repository="cbusillo/sellyouroutboard",
            issue_number=82,
            body="/preview ok",
        )
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret,
                    "LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN": "dev-worker-token",
                },
            ),
            patch(
                "control_plane.service.resolve_launchplane_github_token",
                return_value="github-token",
            ),
            patch(
                "control_plane.workflows.preview_pr_feedback.github_api_request",
                side_effect=[
                    [{"name": "preview-approved"}],
                    {},
                    {},
                    [{"name": "ready-to-merge"}],
                    {"owner": {"login": "cbusillo", "type": "User"}},
                    {"assignees": [{"login": "cbusillo"}]},
                ],
            ) as github_request,
            patch(
                "control_plane.workflows.preview_pr_feedback.find_github_issue_comment_by_marker",
                return_value=None,
            ),
            patch(
                "control_plane.workflows.preview_pr_feedback.create_github_issue_comment",
                return_value={"id": 987},
            ) as create_comment,
        ):
            state_dir = Path(temporary_directory_name) / "state"
            app = create_every_code_github_webhook_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                control_plane_root_path=Path(temporary_directory_name),
            )
            issue_status, issue_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=issue_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-issue",
                    "X-Hub-Signature-256": _github_webhook_signature(issue_payload, secret),
                },
            )
            request_id = issue_response["records"]["request_id"]
            claim_status, _claim_payload = _claim_every_code_work_request_in_filesystem(
                state_dir,
                str(request_id),
            )
            status_status, _status_payload = _update_every_code_work_request_status_in_filesystem(
                state_dir,
                str(request_id),
                state="done",
                result_pr_url="https://github.com/cbusillo/sellyouroutboard/pull/88",
                result_summary="Opened PR.",
                updated_at="2026-05-07T12:40:00Z",
            )
            ok_status, ok_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=comment_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issue_comment",
                    "X-GitHub-Delivery": "delivery-preview-ok",
                    "X-Hub-Signature-256": _github_webhook_signature(comment_payload, secret),
                },
            )

        self.assertEqual(issue_status, 202)
        self.assertEqual(claim_status, 202)
        self.assertEqual(status_status, 202)
        self.assertEqual(ok_status, 202, ok_response)
        preview_validation = ok_response["result"]["preview_validation"]
        self.assertEqual(preview_validation["command"], "ok")
        self.assertEqual(preview_validation["merge_owner"], "cbusillo")
        self.assertEqual(
            github_request.call_args_list[3].kwargs["body"], {"labels": ["ready-to-merge"]}
        )
        self.assertEqual(
            github_request.call_args_list[5].kwargs["body"], {"assignees": ["cbusillo"]}
        )
        create_comment.assert_called_once()
        self.assertIn("@cbusillo", create_comment.call_args.kwargs["body"])

    def test_every_code_preview_validation_failure_returns_generic_webhook_response(
        self,
    ) -> None:
        secret = "launchplane-every-code-webhook-secret"
        comment_payload = _every_code_github_issue_comment_payload(
            repository="cbusillo/sellyouroutboard",
            issue_number=82,
            body="/preview ok",
        )
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {"LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret},
            ),
            patch(
                "control_plane.service.resolve_launchplane_github_token",
                side_effect=ClickException(
                    "Traceback (most recent call last): secret token leaked"
                ),
            ),
        ):
            app = create_every_code_github_webhook_app(
                state_dir=Path(temporary_directory_name) / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                control_plane_root_path=Path(temporary_directory_name),
            )
            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=comment_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issue_comment",
                    "X-GitHub-Delivery": "delivery-preview-validation-failed",
                    "X-Hub-Signature-256": _github_webhook_signature(comment_payload, secret),
                },
            )

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["status"], "accepted")
        self.assertIs(payload["skipped"], True)
        self.assertEqual(payload["reason"], "preview_validation_failed")
        self.assertNotIn("message", payload)
        self.assertNotIn("Traceback", json.dumps(payload, sort_keys=True))

    def test_every_code_preview_ok_allows_repo_owner_override(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        issue_payload = _every_code_github_issue_labeled_payload(
            repository="cbusillo/sellyouroutboard",
            issue_number=82,
        )
        comment_payload = _every_code_github_issue_comment_payload(
            repository="cbusillo/sellyouroutboard",
            issue_number=82,
            issue_author="Mbanks89",
            sender="cbusillo",
            body="/preview ok",
        )
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret,
                    "LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN": "dev-worker-token",
                },
            ),
            patch(
                "control_plane.service.resolve_launchplane_github_token",
                return_value="github-token",
            ),
            patch(
                "control_plane.workflows.preview_pr_feedback.github_api_request",
                side_effect=[
                    [{"name": "preview-approved"}],
                    {},
                    {},
                    [{"name": "ready-to-merge"}],
                    {"owner": {"login": "cbusillo", "type": "User"}},
                    {"assignees": [{"login": "cbusillo"}]},
                ],
            ),
            patch(
                "control_plane.workflows.preview_pr_feedback.find_github_issue_comment_by_marker",
                return_value=None,
            ),
            patch(
                "control_plane.workflows.preview_pr_feedback.create_github_issue_comment",
                return_value={"id": 987},
            ),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            app = create_every_code_github_webhook_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                control_plane_root_path=Path(temporary_directory_name),
            )
            issue_status, issue_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=issue_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-issue",
                    "X-Hub-Signature-256": _github_webhook_signature(issue_payload, secret),
                },
            )
            request_id = issue_response["records"]["request_id"]
            _claim_every_code_work_request_in_filesystem(state_dir, str(request_id))
            _update_every_code_work_request_status_in_filesystem(
                state_dir,
                str(request_id),
                state="done",
                result_pr_url="https://github.com/cbusillo/sellyouroutboard/pull/88",
                result_summary="Opened PR.",
                updated_at="2026-05-07T12:40:00Z",
            )
            ok_status, ok_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=comment_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issue_comment",
                    "X-GitHub-Delivery": "delivery-preview-owner-ok",
                    "X-Hub-Signature-256": _github_webhook_signature(comment_payload, secret),
                },
            )

        self.assertEqual(issue_status, 202)
        self.assertEqual(ok_status, 202, ok_response)
        self.assertEqual(ok_response["result"]["preview_validation"]["command"], "ok")

    def test_every_code_preview_comment_skips_untrusted_actor(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        issue_payload = _every_code_github_issue_labeled_payload(
            repository="cbusillo/sellyouroutboard",
            issue_number=82,
        )
        comment_payload = _every_code_github_issue_comment_payload(
            repository="cbusillo/sellyouroutboard",
            issue_number=82,
            issue_author="Mbanks89",
            sender="random-user",
            body="/preview ok",
        )
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret,
                    "LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN": "dev-worker-token",
                },
            ),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            app = create_every_code_github_webhook_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                control_plane_root_path=Path(temporary_directory_name),
            )
            _issue_status, _issue_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=issue_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-issue",
                    "X-Hub-Signature-256": _github_webhook_signature(issue_payload, secret),
                },
            )
            ok_status, ok_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=comment_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issue_comment",
                    "X-GitHub-Delivery": "delivery-preview-untrusted-ok",
                    "X-Hub-Signature-256": _github_webhook_signature(comment_payload, secret),
                },
            )

        self.assertEqual(ok_status, 202, ok_response)
        self.assertTrue(ok_response["skipped"])
        self.assertEqual(ok_response["reason"], "untrusted_actor")

    def test_every_code_preview_changes_routes_feedback_to_session(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        issue_payload = _every_code_github_issue_labeled_payload(
            repository="cbusillo/sellyouroutboard",
            issue_number=82,
        )
        comment_payload = _every_code_github_issue_comment_payload(
            repository="cbusillo/sellyouroutboard",
            issue_number=82,
            body="/preview changes The delete button still misses bulk uploads.",
        )
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret,
                    "LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN": "dev-worker-token",
                },
            ),
            patch(
                "control_plane.service.resolve_launchplane_github_token",
                return_value="github-token",
            ),
            patch(
                "control_plane.workflows.preview_pr_feedback.github_api_request",
                side_effect=[
                    [{"name": "preview-changes-requested"}],
                    {},
                    {},
                    {},
                ],
            ),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            app = create_every_code_github_webhook_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                control_plane_root_path=Path(temporary_directory_name),
            )
            issue_status, issue_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=issue_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-issue",
                    "X-Hub-Signature-256": _github_webhook_signature(issue_payload, secret),
                },
            )
            request_id = issue_response["records"]["request_id"]
            claim_status, _claim_payload = _claim_every_code_work_request_in_filesystem(
                state_dir,
                str(request_id),
            )
            status_status, _status_payload = _update_every_code_work_request_status_in_filesystem(
                state_dir,
                str(request_id),
                state="done",
                result_pr_url="https://github.com/cbusillo/sellyouroutboard/pull/88",
                result_summary="Opened PR.",
                updated_at="2026-05-07T12:40:00Z",
            )
            changes_status, changes_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=comment_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issue_comment",
                    "X-GitHub-Delivery": "delivery-preview-changes",
                    "X-Hub-Signature-256": _github_webhook_signature(comment_payload, secret),
                },
            )

        self.assertEqual(issue_status, 202)
        self.assertEqual(claim_status, 202)
        self.assertEqual(status_status, 202)
        self.assertEqual(changes_status, 202, changes_response)
        preview_validation = changes_response["result"]["preview_validation"]
        self.assertEqual(preview_validation["command"], "changes")
        feedback = preview_validation["feedback_id"]
        self.assertIn("every-code-pr-feedback-cbusillo-sellyouroutboard-88", feedback)

    def test_every_code_pr_comment_webhook_matches_linked_issue(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        issue_payload = _every_code_github_issue_labeled_payload(issue_number=67)
        comment_payload = _every_code_github_pr_comment_payload(
            pr_number=75,
            issue_body="Closes #67",
        )
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret,
                    "LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN": "dev-worker-token",
                },
            ),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            app = create_every_code_github_webhook_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                control_plane_root_path=Path(temporary_directory_name),
            )
            issue_status, issue_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=issue_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-issue",
                    "X-Hub-Signature-256": _github_webhook_signature(issue_payload, secret),
                },
            )
            request_id = issue_response["records"]["request_id"]
            claim_status, _claim_payload = _claim_every_code_work_request_in_filesystem(
                state_dir,
                str(request_id),
            )
            running_status, _running_payload = _update_every_code_work_request_status_in_filesystem(
                state_dir,
                str(request_id),
                state="running",
                result_summary="Visible tmux session.",
                updated_at="2026-05-06T16:00:00Z",
            )
            feedback_status, feedback_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=comment_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issue_comment",
                    "X-GitHub-Delivery": "delivery-comment",
                    "X-Hub-Signature-256": _github_webhook_signature(comment_payload, secret),
                },
            )

        self.assertEqual(issue_status, 202)
        self.assertEqual(claim_status, 202)
        self.assertEqual(running_status, 202)
        self.assertIn("records", feedback_response)
        self.assertEqual(feedback_status, 202, feedback_response)
        self.assertEqual(feedback_response["records"]["request_id"], request_id)
        feedback = feedback_response["result"]["feedback"]
        self.assertEqual(feedback["request_id"], request_id)
        self.assertEqual(feedback["pr_number"], 75)
        self.assertEqual(feedback["pr_url"], "https://github.com/cbusillo/code/pull/75")

    def test_every_code_pr_comment_webhook_allows_configured_manager(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        issue_payload = _every_code_github_issue_labeled_payload(
            repository="cbusillo/sellyouroutboard",
            issue_number=67,
        )
        comment_payload = _every_code_github_pr_comment_payload(
            repository="cbusillo/sellyouroutboard",
            pr_number=75,
            issue_body="Closes #67",
            sender="Mbanks89",
        )
        with (
            TemporaryDirectory() as temporary_directory_name,
            tempfile.TemporaryDirectory() as home_directory_name,
            patch.dict(
                os.environ,
                {
                    "HOME": home_directory_name,
                    "LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret,
                    "LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN": "dev-worker-token",
                },
            ),
        ):
            _write_github_planning_config(
                Path(home_directory_name),
                repo_managers={"cbusillo/sellyouroutboard": "@Mbanks89"},
            )
            state_dir = Path(temporary_directory_name) / "state"
            app = create_every_code_github_webhook_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                control_plane_root_path=Path(temporary_directory_name),
            )
            issue_status, issue_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=issue_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-issue",
                    "X-Hub-Signature-256": _github_webhook_signature(issue_payload, secret),
                },
            )
            request_id = issue_response["records"]["request_id"]
            _claim_every_code_work_request_in_filesystem(state_dir, str(request_id))
            _update_every_code_work_request_status_in_filesystem(
                state_dir,
                str(request_id),
                state="running",
                result_summary="Visible tmux session.",
                updated_at="2026-05-06T16:00:00Z",
            )
            feedback_status, feedback_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=comment_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issue_comment",
                    "X-GitHub-Delivery": "delivery-comment-manager",
                    "X-Hub-Signature-256": _github_webhook_signature(comment_payload, secret),
                },
            )

        self.assertEqual(issue_status, 202)
        self.assertEqual(feedback_status, 202, feedback_response)
        self.assertEqual(feedback_response["records"]["request_id"], request_id)
        self.assertEqual(feedback_response["result"]["feedback"]["actor"], "Mbanks89")

    def test_every_code_pr_comment_webhook_uses_second_planning_config_path(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        issue_payload = _every_code_github_issue_labeled_payload(
            repository="cbusillo/sellyouroutboard",
            issue_number=67,
        )
        comment_payload = _every_code_github_pr_comment_payload(
            repository="cbusillo/sellyouroutboard",
            pr_number=75,
            issue_body="Closes #67",
            sender="Mbanks89",
        )
        with (
            TemporaryDirectory() as temporary_directory_name,
            tempfile.TemporaryDirectory() as home_directory_name,
            patch.dict(
                os.environ,
                {
                    "HOME": home_directory_name,
                    "LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret,
                    "LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN": "dev-worker-token",
                },
            ),
        ):
            _write_github_planning_config(Path(home_directory_name), repo_managers={})
            _write_github_planning_config(
                Path(home_directory_name),
                path=".codex/github-planning.json",
                repo_managers={"cbusillo/sellyouroutboard": "@Mbanks89"},
            )
            state_dir = Path(temporary_directory_name) / "state"
            app = create_every_code_github_webhook_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                control_plane_root_path=Path(temporary_directory_name),
            )
            issue_status, issue_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=issue_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-issue-second-config",
                    "X-Hub-Signature-256": _github_webhook_signature(issue_payload, secret),
                },
            )
            request_id = issue_response["records"]["request_id"]
            _claim_every_code_work_request_in_filesystem(state_dir, str(request_id))
            _update_every_code_work_request_status_in_filesystem(
                state_dir,
                str(request_id),
                state="done",
                result_pr_url="https://github.com/cbusillo/sellyouroutboard/pull/75",
                result_summary="Opened PR.",
                updated_at="2026-05-07T12:40:00Z",
            )
            feedback_status, feedback_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=comment_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issue_comment",
                    "X-GitHub-Delivery": "delivery-comment-second-config",
                    "X-Hub-Signature-256": _github_webhook_signature(comment_payload, secret),
                },
            )

        self.assertEqual(issue_status, 202)
        self.assertEqual(feedback_status, 202, feedback_response)
        self.assertEqual(feedback_response["records"]["request_id"], request_id)
        self.assertEqual(feedback_response["result"]["feedback"]["actor"], "Mbanks89")

    def test_every_code_pr_comment_webhook_skips_untrusted_actor(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        issue_payload = _every_code_github_issue_labeled_payload(issue_number=67)
        comment_payload = _every_code_github_pr_comment_payload(
            pr_number=75,
            issue_body="Closes #67",
            sender="random-user",
        )
        with (
            TemporaryDirectory() as temporary_directory_name,
            tempfile.TemporaryDirectory() as home_directory_name,
            patch.dict(
                os.environ,
                {
                    "HOME": home_directory_name,
                    "LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret,
                    "LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN": "dev-worker-token",
                },
            ),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            _write_github_planning_config(Path(home_directory_name), repo_managers={})
            app = create_every_code_github_webhook_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                control_plane_root_path=Path(temporary_directory_name),
            )
            issue_status, issue_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=issue_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-issue",
                    "X-Hub-Signature-256": _github_webhook_signature(issue_payload, secret),
                },
            )
            request_id = issue_response["records"]["request_id"]
            feedback_status, feedback_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=comment_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issue_comment",
                    "X-GitHub-Delivery": "delivery-comment-untrusted",
                    "X-Hub-Signature-256": _github_webhook_signature(comment_payload, secret),
                },
            )
            feedback_records = FilesystemRecordStore(state_dir).list_every_code_pr_feedback_records(
                request_id=request_id
            )

        self.assertEqual(issue_status, 202)
        self.assertEqual(feedback_status, 202, feedback_response)
        self.assertTrue(feedback_response["skipped"])
        self.assertEqual(feedback_response["reason"], "untrusted_actor")
        self.assertEqual(feedback_records, ())

    def test_every_code_pr_comment_webhook_ignores_bot_feedback(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        issue_payload = _every_code_github_issue_labeled_payload(issue_number=67)
        comment_payload = _every_code_github_pr_comment_payload(
            pr_number=75,
            issue_body="Closes #67",
            body="Odoo preview refresh started for PR #75.",
            sender="github-actions[bot]",
            sender_type="Bot",
        )
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret,
                    "LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN": "dev-worker-token",
                },
            ),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            app = create_every_code_github_webhook_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                control_plane_root_path=Path(temporary_directory_name),
            )
            issue_status, issue_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=issue_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-issue",
                    "X-Hub-Signature-256": _github_webhook_signature(issue_payload, secret),
                },
            )
            request_id = issue_response["records"]["request_id"]
            _claim_status, _claim_payload = _claim_every_code_work_request_in_filesystem(
                state_dir,
                str(request_id),
            )
            _running_status, _running_payload = (
                _update_every_code_work_request_status_in_filesystem(
                    state_dir,
                    str(request_id),
                    state="running",
                    result_summary="Visible tmux session.",
                    updated_at="2026-05-06T16:00:00Z",
                )
            )
            feedback_status, feedback_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=comment_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issue_comment",
                    "X-GitHub-Delivery": "delivery-comment-bot",
                    "X-Hub-Signature-256": _github_webhook_signature(comment_payload, secret),
                },
            )
            feedback_records = FilesystemRecordStore(state_dir).list_every_code_pr_feedback_records(
                request_id=request_id
            )

        self.assertEqual(issue_status, 202)
        self.assertEqual(feedback_status, 202, feedback_response)
        self.assertTrue(feedback_response["skipped"])
        self.assertEqual(feedback_response["reason"], "automation_actor")
        self.assertEqual(feedback_records, ())

    def test_every_code_pr_comment_webhook_dedupes_feedback(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        issue_payload = _every_code_github_issue_labeled_payload()
        comment_payload = _every_code_github_pr_comment_payload(comment_id=2002)
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret,
                    "LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN": "dev-worker-token",
                },
            ),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            app = create_every_code_github_webhook_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                control_plane_root_path=Path(temporary_directory_name),
            )
            issue_status, issue_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=issue_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-issue",
                    "X-Hub-Signature-256": _github_webhook_signature(issue_payload, secret),
                },
            )
            request_id = issue_response["records"]["request_id"]
            _claim_every_code_work_request_in_filesystem(state_dir, str(request_id))
            _update_every_code_work_request_status_in_filesystem(
                state_dir,
                str(request_id),
                state="done",
                result_pr_url="https://github.com/cbusillo/code/pull/26",
                result_summary="Opened PR.",
                updated_at="2026-05-06T16:00:00Z",
            )
            first_status, first_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=comment_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issue_comment",
                    "X-GitHub-Delivery": "delivery-comment",
                    "X-Hub-Signature-256": _github_webhook_signature(comment_payload, secret),
                },
            )
            second_status, second_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=comment_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issue_comment",
                    "X-GitHub-Delivery": "delivery-comment",
                    "X-Hub-Signature-256": _github_webhook_signature(comment_payload, secret),
                },
            )

        self.assertEqual(issue_status, 202)
        self.assertEqual(first_status, 202)
        self.assertEqual(second_status, 202)
        self.assertEqual(
            first_response["result"]["feedback"]["feedback_id"],
            second_response["result"]["feedback_id"],
        )
        self.assertTrue(second_response["deduped"])

    def test_every_code_worker_pr_feedback_status_is_retired_from_legacy_wsgi_app(
        self,
    ) -> None:
        secret = "launchplane-every-code-webhook-secret"
        issue_payload = _every_code_github_issue_labeled_payload()
        comment_payload = _every_code_github_pr_comment_payload(comment_id=3003)
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret,
                    "LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN": "dev-worker-token",
                },
            ),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            webhook_app = create_every_code_github_webhook_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                control_plane_root_path=Path(temporary_directory_name),
            )
            _issue_status, issue_response = _invoke_app(
                webhook_app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=issue_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-issue",
                    "X-Hub-Signature-256": _github_webhook_signature(issue_payload, secret),
                },
            )
            request_id = issue_response["records"]["request_id"]
            _claim_every_code_work_request_in_filesystem(state_dir, str(request_id))
            _update_every_code_work_request_status_in_filesystem(
                state_dir,
                str(request_id),
                state="done",
                result_pr_url="https://github.com/cbusillo/code/pull/26",
                result_summary="Opened PR.",
                updated_at="2026-05-06T16:00:00Z",
            )
            _invoke_app(
                webhook_app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=comment_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issue_comment",
                    "X-GitHub-Delivery": "delivery-comment",
                    "X-Hub-Signature-256": _github_webhook_signature(comment_payload, secret),
                },
            )
            feedback_records = FilesystemRecordStore(state_dir).list_every_code_pr_feedback_records(
                request_id=request_id,
                status="pending",
            )
            feedback_id = feedback_records[0].feedback_id
            legacy_app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                control_plane_root_path=Path(temporary_directory_name),
            )
            status_status, status_response = _invoke_app(
                legacy_app,
                method="POST",
                path="/v1/every-code/pr-feedback/status",
                payload={
                    "feedback_id": feedback_id,
                    "request_id": request_id,
                    "status": "applied",
                },
                authorization="Bearer dev-worker-token",
            )
            pending_records = FilesystemRecordStore(state_dir).list_every_code_pr_feedback_records(
                request_id=request_id,
                status="pending",
            )

        self.assertEqual(len(feedback_records), 1)
        self.assertEqual(status_status, 404, status_response)
        self.assertEqual(status_response["error"]["code"], "not_found")
        self.assertEqual(len(pending_records), 1)
        self.assertEqual(pending_records[0].feedback_id, feedback_id)

    def test_every_code_worker_feedback_and_gate_writes_are_retired_from_legacy_wsgi_app(
        self,
    ) -> None:
        feedback_record = EveryCodePrFeedbackRecord(
            feedback_id="every-code-pr-feedback-cbusillo-code-26-check-failure-build",
            request_id="every-code-cbusillo-code-123-test",
            repository="cbusillo/code",
            pr_number=26,
            pr_url="https://github.com/cbusillo/code/pull/26",
            feedback_kind="issue_comment",
            github_delivery_id="check-failure-build",
            github_id="check-failure-build",
            actor="github-actions[bot]",
            body="GitHub check build failed on the Every Code PR branch.",
            html_url="https://github.com/cbusillo/code/actions/runs/1001/job/2002",
            received_at="2026-05-06T19:00:00Z",
        )
        gate_record = EveryCodePreviewGateRecord(
            gate_id="every-code-preview-gate-cbusillo-code-26-checks",
            request_id="every-code-cbusillo-code-123-test",
            repository="cbusillo/code",
            issue_number=123,
            issue_url="https://github.com/cbusillo/code/issues/123",
            pr_number=26,
            pr_url="https://github.com/cbusillo/code/pull/26",
            head_sha="abcdef1234567890",
            status="ready",
            created_at="2026-05-06T18:00:00Z",
            updated_at="2026-05-06T18:01:00Z",
            ready_at="2026-05-06T18:01:00Z",
            last_checked_at="2026-05-06T18:01:00Z",
        )
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": "ignored",
                    "LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN": "dev-worker-token",
                },
            ),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
            )
            feedback_status, feedback_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/pr-feedback",
                payload=feedback_record.model_dump(mode="json"),
                authorization="Bearer dev-worker-token",
            )
            gate_status, gate_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/preview-gates",
                payload=gate_record.model_dump(mode="json"),
                authorization="Bearer dev-worker-token",
            )
            feedback_records = FilesystemRecordStore(state_dir).list_every_code_pr_feedback_records(
                request_id=feedback_record.request_id,
                status="pending",
            )
            gate_records = FilesystemRecordStore(state_dir).list_every_code_preview_gate_records(
                request_id=gate_record.request_id,
                status="ready",
            )

        self.assertEqual(feedback_status, 404, feedback_response)
        self.assertEqual(feedback_response["error"]["code"], "not_found")
        self.assertEqual(gate_status, 404, gate_response)
        self.assertEqual(gate_response["error"]["code"], "not_found")
        self.assertEqual(feedback_records, ())
        self.assertEqual(gate_records, ())

    def test_every_code_work_request_rerun_is_retired_from_legacy_wsgi_app(self) -> None:
        policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "github_actions": [
                    {
                        "repository": "cbusillo/launchplane",
                        "workflow_refs": [
                            "cbusillo/launchplane/.github/workflows/every-code-worker.yml@refs/heads/main"
                        ],
                        "event_names": ["workflow_dispatch"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": ["every_code_work_request.rerun"],
                    }
                ]
            }
        )
        identity = _identity(
            repository="cbusillo/launchplane",
            workflow_ref="cbusillo/launchplane/.github/workflows/every-code-worker.yml@refs/heads/main",
            event_name="workflow_dispatch",
        )
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(identity),
                authz_policy=policy,
                control_plane_root_path=Path(temporary_directory_name),
            )
            request_id = _seed_every_code_work_request_record(state_dir).request_id
            _claim_every_code_work_request_in_filesystem(state_dir, str(request_id))
            _update_every_code_work_request_status_in_filesystem(
                state_dir,
                str(request_id),
                state="blocked",
                error_message="Needs another pass.",
                updated_at="2026-05-06T16:00:00Z",
            )
            rerun_status, rerun_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/work-requests/rerun",
                payload={
                    "request_id": request_id,
                    "trigger_actor": "ops",
                },
                headers={"Idempotency-Key": "every-code-rerun-code-123"},
            )
            stored_request = FilesystemRecordStore(state_dir).read_every_code_work_request_record(
                request_id
            )

        self.assertEqual(rerun_status, 404)
        self.assertEqual(rerun_response["error"]["code"], "not_found")
        self.assertEqual(stored_request.state, "blocked")
        self.assertEqual(stored_request.error_message, "Needs another pass.")

    def test_every_code_work_request_rerun_worker_token_is_retired_from_legacy_wsgi_app(
        self,
    ) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {"LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN": "dev-worker-token"},
            ),
        ):
            app = create_launchplane_service_app(
                state_dir=Path(temporary_directory_name) / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=Path(temporary_directory_name),
            )
            state_dir = Path(temporary_directory_name) / "state"
            request_id = _seed_every_code_work_request_record(state_dir).request_id
            _claim_every_code_work_request_in_filesystem(state_dir, request_id)
            _update_every_code_work_request_status_in_filesystem(
                state_dir,
                request_id,
                state="blocked",
                error_message="Needs another pass.",
                updated_at="2026-05-05T22:05:00Z",
            )

            rerun_status, rerun_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/work-requests/rerun",
                payload={
                    "request_id": request_id,
                    "trigger_actor": "ops",
                },
                authorization="Bearer dev-worker-token",
            )
            stored_request = FilesystemRecordStore(state_dir).read_every_code_work_request_record(
                request_id
            )

        self.assertEqual(rerun_status, 404)
        self.assertEqual(rerun_response["error"]["code"], "not_found")
        self.assertEqual(stored_request.state, "blocked")
        self.assertEqual(stored_request.error_message, "Needs another pass.")

    def test_every_code_github_webhook_rejects_invalid_signature(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        webhook_payload = _every_code_github_issue_labeled_payload()
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {"LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret},
            ),
        ):
            app = create_every_code_github_webhook_app(
                state_dir=Path(temporary_directory_name) / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=Path(temporary_directory_name),
            )
            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=webhook_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-1",
                    "X-Hub-Signature-256": "sha256=invalid",
                },
            )

        self.assertEqual(status_code, 401)
        self.assertEqual(payload["error"]["code"], "webhook_signature_invalid")

    def test_every_code_github_webhook_rejects_invalid_json_payload(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        body_bytes = b'{"action":'
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {"LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret},
            ),
        ):
            app = create_every_code_github_webhook_app(
                state_dir=Path(temporary_directory_name) / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=Path(temporary_directory_name),
            )
            status_code, _headers, response_body = _invoke_raw_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                authorization="",
                body_bytes=body_bytes,
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-invalid-json",
                    "X-Hub-Signature-256": _github_webhook_body_signature(
                        body_bytes,
                        secret,
                    ),
                },
            )
            payload = json.loads(response_body.decode("utf-8"))

        self.assertEqual(status_code, 400)
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "invalid_request")
        self.assertEqual(payload["error"]["message"], "GitHub webhook payload is invalid.")

    def test_every_code_github_webhook_rejects_malformed_issue_payload(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        webhook_payload = _every_code_github_issue_labeled_payload()
        webhook_payload["repository"] = {}
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {"LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret},
            ),
        ):
            app = create_every_code_github_webhook_app(
                state_dir=Path(temporary_directory_name) / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=Path(temporary_directory_name),
            )
            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=webhook_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-malformed-issue",
                    "X-Hub-Signature-256": _github_webhook_signature(webhook_payload, secret),
                },
            )

        self.assertEqual(status_code, 400)
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "invalid_request")
        self.assertEqual(payload["error"]["message"], "GitHub webhook payload is invalid.")

    def test_every_code_github_webhook_rejects_bool_issue_number(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        webhook_payload = _every_code_github_issue_labeled_payload()
        issue = cast(dict[str, object], webhook_payload["issue"])
        issue["number"] = True
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {"LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret},
            ),
        ):
            app = create_every_code_github_webhook_app(
                state_dir=Path(temporary_directory_name) / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=Path(temporary_directory_name),
            )
            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=webhook_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-bool-issue-number",
                    "X-Hub-Signature-256": _github_webhook_signature(webhook_payload, secret),
                },
            )

        self.assertEqual(status_code, 400)
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "invalid_request")
        self.assertEqual(payload["error"]["message"], "GitHub webhook payload is invalid.")

    def test_every_code_github_webhook_rejects_malformed_repository_name(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        webhook_payload = _every_code_github_issue_labeled_payload()
        repository = cast(dict[str, object], webhook_payload["repository"])
        repository["full_name"] = " cbusillo/code"
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {"LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret},
            ),
        ):
            app = create_every_code_github_webhook_app(
                state_dir=Path(temporary_directory_name) / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=Path(temporary_directory_name),
            )
            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=webhook_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-malformed-repository",
                    "X-Hub-Signature-256": _github_webhook_signature(webhook_payload, secret),
                },
            )

        self.assertEqual(status_code, 400)
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "invalid_request")
        self.assertEqual(payload["error"]["message"], "GitHub webhook payload is invalid.")

    def test_every_code_github_webhook_rejects_malformed_pull_request_repository(
        self,
    ) -> None:
        secret = "launchplane-every-code-webhook-secret"
        webhook_payload = _every_code_github_pull_request_closed_payload()
        repository = cast(dict[str, object], webhook_payload["repository"])
        repository["full_name"] = " cbusillo/code"
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {"LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret},
            ),
        ):
            app = create_every_code_github_webhook_app(
                state_dir=Path(temporary_directory_name) / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=Path(temporary_directory_name),
            )
            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=webhook_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "delivery-malformed-pr-repository",
                    "X-Hub-Signature-256": _github_webhook_signature(webhook_payload, secret),
                },
            )

        self.assertEqual(status_code, 400)
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "invalid_request")
        self.assertEqual(payload["error"]["message"], "GitHub webhook payload is invalid.")

    def test_every_code_github_webhook_rejects_malformed_pull_request_url(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        webhook_payload = _every_code_github_pull_request_closed_payload()
        pull_request = cast(dict[str, object], webhook_payload["pull_request"])
        pull_request["html_url"] = ""
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {"LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret},
            ),
        ):
            app = create_every_code_github_webhook_app(
                state_dir=Path(temporary_directory_name) / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=Path(temporary_directory_name),
            )
            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=webhook_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "delivery-malformed-pr-url",
                    "X-Hub-Signature-256": _github_webhook_signature(webhook_payload, secret),
                },
            )

        self.assertEqual(status_code, 400)
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "invalid_request")
        self.assertEqual(payload["error"]["message"], "GitHub webhook payload is invalid.")

    def test_every_code_github_webhook_rejects_malformed_feedback_payload(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        issue_payload = _every_code_github_issue_labeled_payload()
        comment_payload = _every_code_github_pr_comment_payload()
        comment = cast(dict[str, object], comment_payload["comment"])
        comment.pop("node_id")
        comment.pop("id")
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret,
                    "LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN": "dev-worker-token",
                },
            ),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            app = create_every_code_github_webhook_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                control_plane_root_path=Path(temporary_directory_name),
            )
            issue_status, issue_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=issue_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-issue",
                    "X-Hub-Signature-256": _github_webhook_signature(issue_payload, secret),
                },
            )
            request_id = str(issue_response["records"]["request_id"])
            _claim_every_code_work_request_in_filesystem(state_dir, request_id)
            _update_every_code_work_request_status_in_filesystem(
                state_dir,
                request_id,
                state="done",
                result_pr_url="https://github.com/cbusillo/code/pull/26",
                result_summary="Opened PR.",
                updated_at="2026-05-06T16:00:00Z",
            )
            feedback_status, feedback_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=comment_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issue_comment",
                    "X-GitHub-Delivery": "delivery-malformed-feedback",
                    "X-Hub-Signature-256": _github_webhook_signature(comment_payload, secret),
                },
            )
            feedback_records = FilesystemRecordStore(state_dir).list_every_code_pr_feedback_records(
                request_id=request_id
            )

        self.assertEqual(issue_status, 202)
        self.assertEqual(feedback_status, 400)
        self.assertEqual(feedback_response["status"], "rejected")
        self.assertEqual(feedback_response["error"]["code"], "invalid_request")
        self.assertEqual(
            feedback_response["error"]["message"], "GitHub webhook payload is invalid."
        )
        self.assertEqual(feedback_records, ())

    def test_every_code_github_webhook_rejects_malformed_feedback_repository(
        self,
    ) -> None:
        secret = "launchplane-every-code-webhook-secret"
        issue_payload = _every_code_github_issue_labeled_payload()
        comment_payload = _every_code_github_pr_comment_payload()
        repository = cast(dict[str, object], comment_payload["repository"])
        repository["full_name"] = " cbusillo/code"
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret,
                    "LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN": "dev-worker-token",
                },
            ),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            app = create_every_code_github_webhook_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                control_plane_root_path=Path(temporary_directory_name),
            )
            issue_status, issue_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=issue_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-issue",
                    "X-Hub-Signature-256": _github_webhook_signature(issue_payload, secret),
                },
            )
            request_id = str(issue_response["records"]["request_id"])
            _claim_every_code_work_request_in_filesystem(state_dir, request_id)
            _update_every_code_work_request_status_in_filesystem(
                state_dir,
                request_id,
                state="done",
                result_pr_url="https://github.com/cbusillo/code/pull/26",
                result_summary="Opened PR.",
                updated_at="2026-05-06T16:00:00Z",
            )
            feedback_status, feedback_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=comment_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issue_comment",
                    "X-GitHub-Delivery": "delivery-malformed-feedback-repository",
                    "X-Hub-Signature-256": _github_webhook_signature(comment_payload, secret),
                },
            )
            feedback_records = FilesystemRecordStore(state_dir).list_every_code_pr_feedback_records(
                request_id=request_id
            )

        self.assertEqual(issue_status, 202)
        self.assertEqual(feedback_status, 400)
        self.assertEqual(feedback_response["status"], "rejected")
        self.assertEqual(feedback_response["error"]["code"], "invalid_request")
        self.assertEqual(
            feedback_response["error"]["message"], "GitHub webhook payload is invalid."
        )
        self.assertEqual(feedback_records, ())

    def test_every_code_github_webhook_rejects_slug_unsafe_feedback_identity(
        self,
    ) -> None:
        secret = "launchplane-every-code-webhook-secret"
        issue_payload = _every_code_github_issue_labeled_payload()
        comment_payload = _every_code_github_pr_comment_payload()
        comment = cast(dict[str, object], comment_payload["comment"])
        comment["node_id"] = "---"
        comment.pop("id")
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret,
                    "LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN": "dev-worker-token",
                },
            ),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            app = create_every_code_github_webhook_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                control_plane_root_path=Path(temporary_directory_name),
            )
            issue_status, issue_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=issue_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-issue",
                    "X-Hub-Signature-256": _github_webhook_signature(issue_payload, secret),
                },
            )
            request_id = str(issue_response["records"]["request_id"])
            _claim_every_code_work_request_in_filesystem(state_dir, request_id)
            _update_every_code_work_request_status_in_filesystem(
                state_dir,
                request_id,
                state="done",
                result_pr_url="https://github.com/cbusillo/code/pull/26",
                result_summary="Opened PR.",
                updated_at="2026-05-06T16:00:00Z",
            )
            feedback_status, feedback_response = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=comment_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issue_comment",
                    "X-GitHub-Delivery": "delivery-slug-unsafe-feedback",
                    "X-Hub-Signature-256": _github_webhook_signature(comment_payload, secret),
                },
            )
            feedback_records = FilesystemRecordStore(state_dir).list_every_code_pr_feedback_records(
                request_id=request_id
            )

        self.assertEqual(issue_status, 202)
        self.assertEqual(feedback_status, 400)
        self.assertEqual(feedback_response["status"], "rejected")
        self.assertEqual(feedback_response["error"]["code"], "invalid_request")
        self.assertEqual(
            feedback_response["error"]["message"], "GitHub webhook payload is invalid."
        )
        self.assertEqual(feedback_records, ())

    def test_every_code_github_webhook_ignores_other_labels(self) -> None:
        secret = "launchplane-every-code-webhook-secret"
        webhook_payload = _every_code_github_issue_labeled_payload(label="bug")
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {"LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret},
            ),
        ):
            app = create_every_code_github_webhook_app(
                state_dir=Path(temporary_directory_name) / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=Path(temporary_directory_name),
            )
            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=webhook_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-1",
                    "X-Hub-Signature-256": _github_webhook_signature(webhook_payload, secret),
                },
            )

        self.assertEqual(status_code, 202)
        self.assertTrue(payload["skipped"])
        self.assertEqual(payload["reason"], "label_not_matched")

    def test_every_code_github_webhook_requires_configured_secret(self) -> None:
        webhook_payload = _every_code_github_issue_labeled_payload()
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {"LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": ""},
            ),
        ):
            app = create_every_code_github_webhook_app(
                state_dir=Path(temporary_directory_name) / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=Path(temporary_directory_name),
            )
            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/github-webhook",
                payload=webhook_payload,
                authorization="",
                headers={
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "delivery-1",
                    "X-Hub-Signature-256": _github_webhook_signature(
                        webhook_payload, "unused-secret"
                    ),
                },
            )

        self.assertEqual(status_code, 503)
        self.assertEqual(payload["error"]["code"], "webhook_secret_not_configured")

    def test_every_code_work_request_claim_is_retired_from_legacy_wsgi_app(self) -> None:
        policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "github_actions": [
                    {
                        "repository": "cbusillo/launchplane",
                        "workflow_refs": [
                            "cbusillo/launchplane/.github/workflows/every-code-worker.yml@refs/heads/main"
                        ],
                        "event_names": ["workflow_dispatch"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": [
                            "every_code_work_request.write",
                            "every_code_work_request.read",
                            "every_code_work_request.claim",
                            "every_code_work_request.update",
                        ],
                    }
                ]
            }
        )
        identity = _identity(
            repository="cbusillo/launchplane",
            workflow_ref="cbusillo/launchplane/.github/workflows/every-code-worker.yml@refs/heads/main",
            event_name="workflow_dispatch",
        )
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(identity),
                authz_policy=policy,
                control_plane_root_path=Path(temporary_directory_name),
            )
            seeded_request = _seed_every_code_work_request_record(state_dir)
            request_id = seeded_request.request_id
            queued_requests = FilesystemRecordStore(state_dir).list_every_code_work_request_records(
                state="queued"
            )
            claim_status, claim_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/work-requests/claim",
                payload={"request_id": request_id, "host": "Chris-Studio"},
                headers={"Idempotency-Key": "every-code-claim-code-123"},
            )
            stored_request = FilesystemRecordStore(state_dir).read_every_code_work_request_record(
                request_id
            )

        self.assertEqual(len(queued_requests), 1)
        self.assertEqual(claim_status, 404)
        self.assertEqual(claim_payload["error"]["code"], "not_found")
        self.assertEqual(stored_request.state, "queued")

    def test_every_code_work_request_claim_worker_token_is_retired_from_legacy_wsgi_app(
        self,
    ) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {"LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN": "worker-token"},
            ),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=Path(temporary_directory_name),
            )
            seeded_request = _seed_every_code_work_request_record(state_dir)
            request_id = seeded_request.request_id
            queued_requests = FilesystemRecordStore(state_dir).list_every_code_work_request_records(
                state="queued"
            )
            claim_status, claim_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/work-requests/claim",
                payload={"request_id": request_id, "host": "Chris-Studio"},
                authorization="Bearer worker-token",
            )
            stored_request = FilesystemRecordStore(state_dir).read_every_code_work_request_record(
                request_id
            )

        self.assertEqual(len(queued_requests), 1)
        self.assertEqual(claim_status, 404)
        self.assertEqual(claim_payload["error"]["code"], "not_found")
        self.assertEqual(stored_request.state, "queued")

    def test_every_code_work_request_status_is_retired_from_legacy_wsgi_app(self) -> None:
        policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "github_actions": [
                    {
                        "repository": "cbusillo/launchplane",
                        "workflow_refs": [
                            "cbusillo/launchplane/.github/workflows/every-code-worker.yml@refs/heads/main"
                        ],
                        "event_names": ["workflow_dispatch"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": ["every_code_work_request.update"],
                    }
                ]
            }
        )
        identity = _identity(
            repository="cbusillo/launchplane",
            workflow_ref="cbusillo/launchplane/.github/workflows/every-code-worker.yml@refs/heads/main",
            event_name="workflow_dispatch",
        )
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(identity),
                authz_policy=policy,
                control_plane_root_path=Path(temporary_directory_name),
            )
            seeded_request = _seed_every_code_work_request_record(state_dir)
            request_id = seeded_request.request_id
            _claim_every_code_work_request_in_filesystem(state_dir, request_id)

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/work-requests/status",
                payload={
                    "request_id": request_id,
                    "host": "Chris-Studio",
                    "state": "done",
                    "result_summary": "Opened PR.",
                    "updated_at": "2026-05-05T22:03:00Z",
                },
                headers={"Idempotency-Key": "every-code-status-code-123"},
            )
            stored_request = FilesystemRecordStore(state_dir).read_every_code_work_request_record(
                request_id
            )

        self.assertEqual(status_code, 404)
        self.assertEqual(payload["error"]["code"], "not_found")
        self.assertEqual(stored_request.state, "claimed")
        self.assertEqual(stored_request.result_summary, "")

    def test_every_code_work_request_status_worker_token_is_retired_from_legacy_wsgi_app(
        self,
    ) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {"LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN": "worker-token"},
            ),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=Path(temporary_directory_name),
            )
            seeded_request = _seed_every_code_work_request_record(state_dir)
            request_id = seeded_request.request_id
            _claim_every_code_work_request_in_filesystem(state_dir, request_id)

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/work-requests/status",
                payload={
                    "request_id": request_id,
                    "host": "Chris-Studio",
                    "state": "blocked",
                    "error_message": "Needs another pass.",
                    "updated_at": "2026-05-05T22:03:00Z",
                },
                authorization="Bearer worker-token",
            )
            stored_request = FilesystemRecordStore(state_dir).read_every_code_work_request_record(
                request_id
            )

        self.assertEqual(status_code, 404)
        self.assertEqual(payload["error"]["code"], "not_found")
        self.assertEqual(stored_request.state, "claimed")
        self.assertEqual(stored_request.error_message, "")

    def test_product_profile_routes_are_retired_from_legacy_wsgi_app(self) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {"LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN": "worker-token"},
            ),
        ):
            state_dir = Path(temporary_directory_name) / "state"
            FilesystemRecordStore(state_dir=state_dir).write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                control_plane_root_path=Path(temporary_directory_name),
            )
            list_status, list_payload = _invoke_app(
                app,
                method="GET",
                path="/v1/product-profiles",
                query_string="driver_id=generic-web",
                authorization="Bearer worker-token",
            )
            show_status, show_payload = _invoke_app(
                app,
                method="GET",
                path="/v1/product-profiles/sellyouroutboard",
                authorization="Bearer worker-token",
            )
            write_status, write_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/product-profiles",
                payload=_product_profile_payload(),
            )

        self.assertEqual(list_status, 404)
        self.assertEqual(list_payload["error"]["code"], "not_found")
        self.assertEqual(show_status, 404)
        self.assertEqual(show_payload["error"]["code"], "not_found")
        self.assertEqual(write_status, 404)
        self.assertEqual(write_payload["error"]["code"], "not_found")

    def test_every_code_worker_read_route_is_retired_before_authentication(self) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {"LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN": "worker-token"},
            ),
        ):
            app = create_launchplane_service_app(
                state_dir=Path(temporary_directory_name) / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=Path(temporary_directory_name),
            )
            status_code, payload = _invoke_app(
                app,
                method="GET",
                path="/v1/every-code/work-requests",
                authorization="",
            )

        self.assertEqual(status_code, 404)
        self.assertEqual(payload["error"]["code"], "not_found")

    def test_every_code_worker_read_routes_are_removed_from_legacy_worker_token_bypass(
        self,
    ) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                os.environ,
                {"LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN": "worker-token"},
            ),
        ):
            app = create_launchplane_service_app(
                state_dir=Path(temporary_directory_name) / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=Path(temporary_directory_name),
            )
            work_status, work_payload = _invoke_app(
                app,
                method="GET",
                path="/v1/every-code/work-requests",
                query_string="offset=-1",
                authorization="Bearer worker-token",
            )
            feedback_status, feedback_payload = _invoke_app(
                app,
                method="GET",
                path="/v1/every-code/pr-feedback",
                query_string="offset=-1",
                authorization="Bearer worker-token",
            )

        self.assertEqual(work_status, 404)
        self.assertEqual(work_payload["error"]["code"], "not_found")
        self.assertEqual(feedback_status, 404)
        self.assertEqual(feedback_payload["error"]["code"], "not_found")

    def test_every_code_work_request_create_is_retired_from_legacy_wsgi_app(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            app = create_launchplane_service_app(
                state_dir=Path(temporary_directory_name) / "state",
                verifier=_StubVerifier(_identity(repository="cbusillo/launchplane")),
                authz_policy=_every_code_worker_policy(),
                control_plane_root_path=Path(temporary_directory_name),
            )
            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/every-code/work-requests/create",
                payload={
                    "repository": "cbusillo/code",
                    "issue_number": 123,
                    "issue_url": "https://github.com/cbusillo/code/issues/123",
                },
            )

        self.assertEqual(status_code, 404)
        self.assertEqual(payload["error"]["code"], "not_found")

    def test_work_graph_reads_are_retired_from_legacy_wsgi_app(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            app = create_launchplane_service_app(
                state_dir=Path(temporary_directory_name) / "state",
                verifier=_StubVerifier(_identity(repository="cbusillo/launchplane")),
                authz_policy=LaunchplaneAuthzPolicy.model_validate(
                    {
                        "github_actions": [
                            {
                                "repository": "cbusillo/launchplane",
                                "workflow_refs": ["*"],
                                "event_names": ["workflow_dispatch"],
                                "products": ["launchplane", "example-site"],
                                "contexts": ["launchplane", "example-site"],
                                "actions": ["work_graph.rank", "product_environment.read"],
                            }
                        ]
                    }
                ),
                control_plane_root_path=Path(temporary_directory_name),
            )
            responses = [
                _invoke_app(app, method="GET", path="/v1/work-graph/snapshot"),
                _invoke_app(app, method="GET", path="/v1/work-graph/github/issues"),
                _invoke_app(
                    app,
                    method="POST",
                    path="/v1/work-graph/rank",
                    payload={"snapshot": _work_graph_snapshot_payload(), "limit": 1},
                ),
                _invoke_app(
                    app,
                    method="POST",
                    path="/v1/work-graph/github/issues/reconcile",
                    payload={"mode": "dry_run"},
                ),
            ]

        for status_code, payload in responses:
            self.assertEqual(status_code, 404)
            self.assertEqual(payload["error"]["code"], "not_found")

    def test_agent_context_routes_are_retired_from_legacy_wsgi_app(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=Path(temporary_directory_name),
                local_record_store_for_tests=FilesystemRecordStore(state_dir=state_dir),
            )

            mapping_status, mapping_payload = _invoke_app(
                app,
                method="GET",
                path="/v1/repo-product-mapping",
            )
            context_status, context_payload = _invoke_app(
                app,
                method="GET",
                path="/v1/agent/context",
            )

        self.assertEqual(mapping_status, 404)
        self.assertEqual(context_status, 404)
        self.assertEqual(mapping_payload["error"]["code"], "not_found")
        self.assertEqual(context_payload["error"]["code"], "not_found")

    def test_create_service_app_requires_database_url_without_explicit_local_test_store(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            with self.assertRaisesRegex(ValueError, "shared storage requires"):
                _create_launchplane_service_app(
                    state_dir=Path(temporary_directory_name) / "state",
                    verifier=_StubVerifier(_identity()),
                    authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                    control_plane_root_path=Path(temporary_directory_name),
                )

    def test_health_route_is_retired_from_legacy_wsgi_app(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=Path(temporary_directory_name),
                local_record_store_for_tests=FilesystemRecordStore(state_dir=state_dir),
            )

            status_code, payload = _invoke_app(
                app, method="GET", path="/v1/health", authorization=""
            )

            self.assertEqual(status_code, 404)
            self.assertEqual(payload["error"]["code"], "not_found")

    def test_service_runtime_routes_are_retired_from_legacy_wsgi_app(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=Path(temporary_directory_name),
                local_record_store_for_tests=FilesystemRecordStore(state_dir=state_dir),
            )

            runtime_status, runtime_payload = _invoke_app(
                app,
                method="GET",
                path="/v1/service/runtime",
                authorization="",
            )
            worker_status, worker_payload = _invoke_app(
                app,
                method="GET",
                path="/v1/service/odoo-workers/status",
                authorization="",
            )
            reconcile_status, reconcile_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/service/odoo-workers/reconcile",
                payload={},
                authorization="",
            )

        self.assertEqual(runtime_status, 404)
        self.assertEqual(worker_status, 404)
        self.assertEqual(reconcile_status, 404)
        self.assertEqual(runtime_payload["error"]["code"], "not_found")
        self.assertEqual(worker_payload["error"]["code"], "not_found")
        self.assertEqual(reconcile_payload["error"]["code"], "not_found")

    def test_target_logs_route_is_retired_from_legacy_wsgi_app(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=Path(temporary_directory_name),
                local_record_store_for_tests=FilesystemRecordStore(state_dir=state_dir),
            )

            status_code, payload = _invoke_app(
                app,
                method="GET",
                path="/v1/contexts/sellyouroutboard-testing/instances/testing/logs",
                authorization="",
            )

        self.assertEqual(status_code, 404)
        self.assertEqual(payload["error"]["code"], "not_found")

    def test_edge_endpoint_read_routes_are_retired_from_legacy_wsgi_app(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=Path(temporary_directory_name),
                local_record_store_for_tests=FilesystemRecordStore(state_dir=state_dir),
            )

            list_status_code, list_payload = _invoke_app(
                app,
                method="GET",
                path="/v1/edge-endpoints/records",
                authorization="",
            )
            read_status_code, read_payload = _invoke_app(
                app,
                method="GET",
                path="/v1/edge-endpoints/records/cm-prod-dokploy",
                authorization="",
            )

        self.assertEqual(list_status_code, 404)
        self.assertEqual(read_status_code, 404)
        self.assertEqual(list_payload["error"]["code"], "not_found")
        self.assertEqual(read_payload["error"]["code"], "not_found")

    def test_private_health_endpoint_read_routes_are_retired_from_legacy_wsgi_app(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=Path(temporary_directory_name),
                local_record_store_for_tests=FilesystemRecordStore(state_dir=state_dir),
            )

            list_status_code, list_payload = _invoke_app(
                app,
                method="GET",
                path="/v1/private-health-endpoints/records",
                authorization="",
            )
            read_status_code, read_payload = _invoke_app(
                app,
                method="GET",
                path="/v1/private-health-endpoints/records/repairshopr-sync-prod-runtime",
                authorization="",
            )

        self.assertEqual(list_status_code, 404)
        self.assertEqual(read_status_code, 404)
        self.assertEqual(list_payload["error"]["code"], "not_found")
        self.assertEqual(read_payload["error"]["code"], "not_found")

    def test_ingress_canary_route_read_routes_are_retired_from_legacy_wsgi_app(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=Path(temporary_directory_name),
                local_record_store_for_tests=FilesystemRecordStore(state_dir=state_dir),
            )

            list_status_code, list_payload = _invoke_app(
                app,
                method="GET",
                path="/v1/ingress/canary-routes/records",
                authorization="",
            )
            read_status_code, read_payload = _invoke_app(
                app,
                method="GET",
                path="/v1/ingress/canary-routes/records/ingress-canary",
                authorization="",
            )

        self.assertEqual(list_status_code, 404)
        self.assertEqual(read_status_code, 404)
        self.assertEqual(list_payload["error"]["code"], "not_found")
        self.assertEqual(read_payload["error"]["code"], "not_found")

    def test_service_serve_rejects_missing_database_url(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            policy_file = Path(temporary_directory_name) / "policy.toml"
            policy_file.write_text("schema_version = 1\n", encoding="utf-8")

            result = runner.invoke(
                CLI_MAIN,
                [
                    "service",
                    "serve",
                    "--state-dir",
                    str(Path(temporary_directory_name) / "state"),
                    "--policy-file",
                    str(policy_file),
                ],
            )

        self.assertEqual(result.exit_code, 1, msg=result.output)
        self.assertIn("refuses startup without --database-url", result.output)

    def test_service_serve_rejects_missing_audience(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            policy_file = Path(temporary_directory_name) / "policy.toml"
            policy_file.write_text("schema_version = 1\n", encoding="utf-8")

            result = runner.invoke(
                CLI_MAIN,
                [
                    "service",
                    "serve",
                    "--state-dir",
                    str(Path(temporary_directory_name) / "state"),
                    "--policy-file",
                    str(policy_file),
                    "--database-url",
                    f"sqlite+pysqlite:///{Path(temporary_directory_name) / 'state.sqlite3'}",
                ],
            )

        self.assertEqual(result.exit_code, 1, msg=result.output)
        self.assertIn("refuses startup without --audience", result.output)

    def test_service_serve_runs_fastapi_app_with_legacy_wsgi_fallback(self) -> None:
        fastapi_response: _AsgiServiceTestResponse | None = None
        legacy_response: _AsgiServiceTestResponse | None = None
        denied_config_response: _AsgiServiceTestResponse | None = None
        grant_response: _AsgiServiceTestResponse | None = None
        authorized_config_response: _AsgiServiceTestResponse | None = None

        def capture_uvicorn_run(app: Any, **_kwargs: object) -> None:
            nonlocal fastapi_response, legacy_response, denied_config_response
            nonlocal grant_response, authorized_config_response
            fastapi_response = asyncio.run(_asgi_get_for_service_test(app, "/openapi.json"))
            legacy_response = asyncio.run(_asgi_get_for_service_test(app, "/v1/health"))
            denied_config_response = asyncio.run(
                _asgi_request_for_service_test(
                    app,
                    method="GET",
                    path="/v1/products/example-site/environments/prod/config-status",
                    authorization="Bearer valid-token",
                )
            )
            grant_response = asyncio.run(
                _asgi_request_for_service_test(
                    app,
                    method="POST",
                    path="/v1/authz-policies/github-actions/grants",
                    authorization="Bearer valid-token",
                    payload={
                        "schema_version": 1,
                        "product": "launchplane",
                        "mode": "apply",
                        "reason": "Grant FastAPI config-status read for runtime policy refresh test.",
                        "related_issue": "cbusillo/launchplane#1323",
                        "grant": {
                            "repository": "cbusillo/launchplane",
                            "workflow_refs": [
                                "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["example-site"],
                            "contexts": ["example-site"],
                            "actions": ["product_environment.read"],
                            "source_label": "test:fastapi-runtime-policy-refresh",
                        },
                    },
                    headers={"Idempotency-Key": "authz-grant:fastapi-runtime-refresh"},
                )
            )
            authorized_config_response = asyncio.run(
                _asgi_request_for_service_test(
                    app,
                    method="GET",
                    path="/v1/products/example-site/environments/prod/config-status",
                    authorization="Bearer valid-token",
                )
            )

        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy_file = root / "policy.toml"
            policy_file.write_text(
                "\n".join(
                    (
                        "schema_version = 1",
                        "",
                        "[[github_actions]]",
                        'repository = "cbusillo/launchplane"',
                        "workflow_refs = [",
                        '  "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main",',
                        "]",
                        'event_names = ["workflow_dispatch"]',
                        'products = ["launchplane"]',
                        'contexts = ["launchplane"]',
                        'actions = ["authz_policy_grant.write"]',
                        "",
                    )
                ),
                encoding="utf-8",
            )
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_generic_site_profile_payload())
            )
            store.close()

            with (
                patch("uvicorn.run", side_effect=capture_uvicorn_run) as run_uvicorn,
                patch(
                    "control_plane.service_auth.GitHubOidcVerifier",
                    return_value=_StubVerifier(
                        _identity(
                            repository="cbusillo/launchplane",
                            workflow_ref=(
                                "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main"
                            ),
                            event_name="workflow_dispatch",
                        )
                    ),
                ),
            ):
                control_plane_service.serve_launchplane_service(
                    state_dir=root / "state",
                    policy_file=policy_file,
                    host="127.0.0.1",
                    port=8080,
                    audience="launchplane-service",
                    database_url=database_url,
                )

            run_uvicorn.assert_called_once()

        self.assertIsNotNone(fastapi_response)
        self.assertIsNotNone(legacy_response)
        self.assertIsNotNone(denied_config_response)
        self.assertIsNotNone(grant_response)
        self.assertIsNotNone(authorized_config_response)
        assert fastapi_response is not None
        assert legacy_response is not None
        assert denied_config_response is not None
        assert grant_response is not None
        assert authorized_config_response is not None
        self.assertEqual(fastapi_response.status_code, 200)
        self.assertIn(
            "/v1/products/{product}/environments/{environment}/config-status",
            fastapi_response.json()["paths"],
        )
        self.assertEqual(legacy_response.status_code, 200)
        self.assertEqual(legacy_response.json()["status"], "ok")
        self.assertEqual(legacy_response.json()["storage_backend"], "postgres")
        self.assertEqual(
            denied_config_response.status_code,
            403,
            msg=denied_config_response.body.decode("utf-8"),
        )
        self.assertEqual(grant_response.status_code, 202)
        self.assertEqual(grant_response.json()["result"]["changed"], True)
        self.assertEqual(authorized_config_response.status_code, 200)
        self.assertEqual(
            authorized_config_response.json()["config_status"]["product"], "example-site"
        )

    def test_ui_route_serves_static_shell_without_authentication(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            ui_root = root / "control_plane" / "ui_static"
            asset_root = ui_root / "assets"
            asset_root.mkdir(parents=True)
            (ui_root / "index.html").write_text(
                '<html><head><script type="module" src="/ui/assets/app.js"></script></head></html>',
                encoding="utf-8",
            )
            (asset_root / "app.js").write_text("console.log('launchplane ui');\n", encoding="utf-8")
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=root,
            )

            shell_status, shell_headers, shell_body = _invoke_raw_app(app, method="GET", path="/ui")
            asset_status, asset_headers, asset_body = _invoke_raw_app(
                app, method="GET", path="/ui/assets/app.js"
            )

        self.assertEqual(shell_status, 200)
        self.assertEqual(shell_headers["Content-Type"], "text/html")
        self.assertIn(b"/ui/assets/app.js", shell_body)
        self.assertEqual(shell_headers["Cache-Control"], "no-store")
        self.assertEqual(asset_status, 200)
        self.assertIn(asset_headers["Content-Type"], {"text/javascript", "application/javascript"})
        self.assertIn(b"launchplane ui", asset_body)
        self.assertEqual(asset_headers["Cache-Control"], "public, max-age=31536000, immutable")

    def test_root_route_serves_ui_shell_without_authentication(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            ui_root = root / "control_plane" / "ui_static"
            ui_root.mkdir(parents=True)
            (ui_root / "index.html").write_text(
                '<html><head><script type="module" src="/ui/assets/app.js"></script></head></html>',
                encoding="utf-8",
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=root,
            )

            status_code, headers, body = _invoke_raw_app(app, method="GET", path="/")

        self.assertEqual(status_code, 200)
        self.assertEqual(headers["Content-Type"], "text/html")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn(b"/ui/assets/app.js", body)

    def test_ui_route_falls_back_to_shell_for_nested_paths(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            ui_root = root / "control_plane" / "ui_static"
            ui_root.mkdir(parents=True)
            (ui_root / "index.html").write_text("<html>Launchplane UI</html>", encoding="utf-8")
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=root,
            )

            status_code, headers, body = _invoke_raw_app(
                app, method="GET", path="/ui/contexts/verireel"
            )

        self.assertEqual(status_code, 200)
        self.assertEqual(headers["Content-Type"], "text/html")
        self.assertIn(b"Launchplane UI", body)

    def test_ui_asset_route_rejects_parent_directory_segments(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            ui_root = root / "control_plane" / "ui_static"
            ui_root.mkdir(parents=True)
            (ui_root / "index.html").write_text("<html>Launchplane UI</html>", encoding="utf-8")
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=root,
            )

            status_code, headers, body = _invoke_raw_app(
                app, method="GET", path="/ui/assets/%2e%2e/index.html"
            )

        self.assertEqual(status_code, 404)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertNotIn(b"Launchplane UI", body)

    def test_descriptor_dispatch_fake_route_executes_authorized_handler(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            profile_payload = _product_profile_payload("fake-product")
            profile_payload["driver_id"] = _FAKE_DESCRIPTOR_DRIVER_ID
            profile_payload["lanes"] = (
                {
                    "instance": "testing",
                    "context": "fake-context",
                    "base_url": "https://fake.example.test",
                    "health_url": "https://fake.example.test/health",
                },
            )
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(profile_payload)
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
                            "products": ["fake-product"],
                            "contexts": ["fake-context"],
                            "actions": ["fake_descriptor.ping"],
                        }
                    ]
                }
            )
            calls: list[tuple[_FakeDescriptorDispatchEnvelope, str]] = []
            with (
                patch(
                    "control_plane.service.list_driver_descriptors",
                    return_value=(_fake_descriptor_dispatch_descriptor(),),
                ),
                patch(
                    "control_plane.service._descriptor_driver_dispatch_routes",
                    return_value={
                        _FAKE_DESCRIPTOR_ROUTE_PATH: _fake_descriptor_dispatch_route(calls)
                    },
                ),
                patch(
                    "control_plane.service._required_descriptor_driver_dispatch_route_paths",
                    return_value=frozenset(),
                ),
            ):
                app = create_launchplane_service_app(
                    state_dir=state_dir,
                    verifier=_StubVerifier(_identity()),
                    authz_policy=policy,
                    control_plane_root_path=root,
                )
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path=_FAKE_DESCRIPTOR_ROUTE_PATH,
                    payload={
                        "schema_version": 1,
                        "product": "fake-product",
                        "context": "fake-context",
                        "instance": "testing",
                        "value": "alpha",
                    },
                    headers={"Idempotency-Key": "fake-dispatch-alpha"},
                )

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["records"]["request_id"], "fake-descriptor-alpha")
        self.assertEqual(payload["result"], {"status": "pass", "processed_value": "alpha:testing"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], "testing")

    def test_descriptor_dispatch_fake_route_rejects_unauthorized_context(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            profile_payload = _product_profile_payload("fake-product")
            profile_payload["driver_id"] = _FAKE_DESCRIPTOR_DRIVER_ID
            profile_payload["lanes"] = (
                {
                    "instance": "testing",
                    "context": "fake-context",
                    "base_url": "https://fake.example.test",
                    "health_url": "https://fake.example.test/health",
                },
            )
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(profile_payload)
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
                            "products": ["fake-product"],
                            "contexts": ["other-context"],
                            "actions": ["fake_descriptor.ping"],
                        }
                    ]
                }
            )
            calls: list[tuple[_FakeDescriptorDispatchEnvelope, str]] = []
            with (
                patch(
                    "control_plane.service.list_driver_descriptors",
                    return_value=(_fake_descriptor_dispatch_descriptor(),),
                ),
                patch(
                    "control_plane.service._descriptor_driver_dispatch_routes",
                    return_value={
                        _FAKE_DESCRIPTOR_ROUTE_PATH: _fake_descriptor_dispatch_route(calls)
                    },
                ),
                patch(
                    "control_plane.service._required_descriptor_driver_dispatch_route_paths",
                    return_value=frozenset(),
                ),
            ):
                app = create_launchplane_service_app(
                    state_dir=state_dir,
                    verifier=_StubVerifier(_identity()),
                    authz_policy=policy,
                    control_plane_root_path=root,
                )
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path=_FAKE_DESCRIPTOR_ROUTE_PATH,
                    payload={
                        "schema_version": 1,
                        "product": "fake-product",
                        "context": "fake-context",
                        "instance": "testing",
                        "value": "alpha",
                    },
                    headers={"Idempotency-Key": "fake-dispatch-denied"},
                )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")
        self.assertEqual(calls, [])

    def test_descriptor_dispatch_fake_route_replays_idempotent_response(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            profile_payload = _product_profile_payload("fake-product")
            profile_payload["driver_id"] = _FAKE_DESCRIPTOR_DRIVER_ID
            profile_payload["lanes"] = (
                {
                    "instance": "testing",
                    "context": "fake-context",
                    "base_url": "https://fake.example.test",
                    "health_url": "https://fake.example.test/health",
                },
            )
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(profile_payload)
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
                            "products": ["fake-product"],
                            "contexts": ["fake-context"],
                            "actions": ["fake_descriptor.ping"],
                        }
                    ]
                }
            )
            calls: list[tuple[_FakeDescriptorDispatchEnvelope, str]] = []
            request_payload = {
                "schema_version": 1,
                "product": "fake-product",
                "context": "fake-context",
                "instance": "testing",
                "value": "alpha",
            }
            with (
                patch(
                    "control_plane.service.list_driver_descriptors",
                    return_value=(_fake_descriptor_dispatch_descriptor(),),
                ),
                patch(
                    "control_plane.service._descriptor_driver_dispatch_routes",
                    return_value={
                        _FAKE_DESCRIPTOR_ROUTE_PATH: _fake_descriptor_dispatch_route(calls)
                    },
                ),
                patch(
                    "control_plane.service._required_descriptor_driver_dispatch_route_paths",
                    return_value=frozenset(),
                ),
            ):
                app = create_launchplane_service_app(
                    state_dir=state_dir,
                    verifier=_StubVerifier(_identity()),
                    authz_policy=policy,
                    control_plane_root_path=root,
                )
                first_status_code, first_payload = _invoke_app(
                    app,
                    method="POST",
                    path=_FAKE_DESCRIPTOR_ROUTE_PATH,
                    payload=request_payload,
                    headers={"Idempotency-Key": "fake-dispatch-replay"},
                )
                second_status_code, second_payload = _invoke_app(
                    app,
                    method="POST",
                    path=_FAKE_DESCRIPTOR_ROUTE_PATH,
                    payload=request_payload,
                    headers={"Idempotency-Key": "fake-dispatch-replay"},
                )

        self.assertEqual(first_status_code, 202)
        self.assertEqual(second_status_code, 202)
        self.assertEqual(first_payload["records"], second_payload["records"])
        self.assertTrue(second_payload["replayed"])
        self.assertEqual(len(calls), 1)

    def test_descriptor_dispatch_route_requires_descriptor_declaration(self) -> None:
        calls: list[tuple[_FakeDescriptorDispatchEnvelope, str]] = []
        policy = LaunchplaneAuthzPolicy.model_validate({"github_actions": []})

        with (
            TemporaryDirectory() as temporary_directory_name,
            patch("control_plane.service.list_driver_descriptors", return_value=()),
            patch(
                "control_plane.service._descriptor_driver_dispatch_routes",
                return_value={_FAKE_DESCRIPTOR_ROUTE_PATH: _fake_descriptor_dispatch_route(calls)},
            ),
            patch(
                "control_plane.service._required_descriptor_driver_dispatch_route_paths",
                return_value=frozenset(),
            ),
        ):
            with self.assertRaisesRegex(
                ValueError,
                "must be declared by a driver descriptor",
            ):
                create_launchplane_service_app(
                    state_dir=Path(temporary_directory_name) / "state",
                    verifier=_StubVerifier(_identity()),
                    authz_policy=policy,
                    control_plane_root_path=Path(temporary_directory_name),
                )

    def test_unregistered_descriptor_driver_route_fails_closed(self) -> None:
        policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "github_actions": [
                    {
                        "repository": "every/verireel",
                        "workflow_refs": [
                            "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                        ],
                        "event_names": ["pull_request"],
                        "products": ["fake-product"],
                        "contexts": ["fake-context"],
                        "actions": ["fake_descriptor.ping", "preview_destroyed.write"],
                    }
                ]
            }
        )

        with (
            TemporaryDirectory() as temporary_directory_name,
            patch(
                "control_plane.service.list_driver_descriptors",
                return_value=(_fake_descriptor_dispatch_descriptor(),),
            ),
            patch(
                "control_plane.service._descriptor_driver_dispatch_routes",
                return_value={},
            ),
            patch(
                "control_plane.service._required_descriptor_driver_dispatch_route_paths",
                return_value=frozenset(),
            ),
        ):
            root = Path(temporary_directory_name)
            with self.assertRaisesRegex(
                ValueError,
                "POST driver descriptor routes must be registered for descriptor-backed dispatch",
            ):
                create_launchplane_service_app(
                    state_dir=root / "state-startup-fail",
                    verifier=_StubVerifier(_identity()),
                    authz_policy=policy,
                    control_plane_root_path=root,
                )

        with (
            TemporaryDirectory() as temporary_directory_name,
            patch(
                "control_plane.service.list_driver_descriptors",
                return_value=(_fake_descriptor_dispatch_descriptor(),),
            ),
            patch(
                "control_plane.service._descriptor_driver_dispatch_routes",
                return_value={},
            ),
            patch(
                "control_plane.service._required_descriptor_driver_dispatch_route_paths",
                return_value=frozenset(),
            ),
            patch(
                "control_plane.service._descriptor_driver_dispatch_exempt_route_paths",
                return_value=frozenset({_FAKE_DESCRIPTOR_ROUTE_PATH}),
            ),
        ):
            root = Path(temporary_directory_name)
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
            )
            status_code, payload = _invoke_app(
                app,
                method="POST",
                path=_FAKE_DESCRIPTOR_ROUTE_PATH,
                payload={
                    "schema_version": 1,
                    "product": "fake-product",
                    "destroy": {
                        "context": "fake-context",
                        "anchor_repo": "cbusillo/fake-product",
                        "anchor_pr_number": 1,
                        "anchor_pr_url": "https://github.com/cbusillo/fake-product/pull/1",
                    },
                },
                headers={"Idempotency-Key": "fake-unregistered-dispatch"},
            )

        self.assertEqual(status_code, 500)
        self.assertEqual(payload["error"]["code"], "driver_route_not_registered")

    def test_product_onboarding_endpoint_writes_full_launchplane_owned_bundle(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
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
                            "actions": ["product_onboarding.apply"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_wsgi_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/product-onboarding/apply",
                payload={
                    "schema_version": 1,
                    "product": "launchplane",
                    "manifest": {
                        "product": "discord-blue",
                        "display_name": "Discord Blue",
                        "repository": "cbusillo/discord-blue",
                        "driver_id": "generic-web",
                        "image_repository": "ghcr.io/cbusillo/discord-blue",
                        "runtime_port": 8787,
                        "health_path": "/health",
                        "lanes": [
                            {
                                "instance": "prod",
                                "context": "discord-blue",
                                "base_url": "https://discord-blue.example.test",
                            }
                        ],
                        "provider_targets": [
                            {
                                "context": "discord-blue",
                                "instance": "prod",
                                "target_id": "app-discord-blue",
                                "target_type": "application",
                                "target_name": "discord-blue",
                                "healthcheck_enabled": False,
                            }
                        ],
                        "runtime_environments": [
                            {
                                "scope": "instance",
                                "context": "discord-blue",
                                "instance": "prod",
                                "env": {"DISCORD_BLUE_STATE_DIR": "/var/lib/discord-blue"},
                            }
                        ],
                        "secret_bindings": [
                            {
                                "binding_key": "DISCORD_TOKEN",
                                "context": "discord-blue",
                                "instance": "prod",
                            }
                        ],
                        "updated_at": "2026-05-04T18:00:00Z",
                        "source_label": "test:discord-blue-onboarding",
                    },
                },
                headers={"Idempotency-Key": "product-onboarding-discord-blue"},
            )
            store = PostgresRecordStore(database_url=database_url)
            try:
                profile = store.read_product_profile_record("discord-blue")
                target = store.read_dokploy_target_record(
                    context_name="discord-blue", instance_name="prod"
                )
                target_id = store.read_dokploy_target_id_record(
                    context_name="discord-blue", instance_name="prod"
                )
                runtime_records = store.list_runtime_environment_records()
                secret_bindings = store.list_secret_bindings()
            finally:
                store.close()

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["records"]["product_profile"], "discord-blue")
        self.assertEqual(payload["records"]["provider_target_count"], "1")
        self.assertEqual(payload["records"]["provider_target_id_count"], "1")
        self.assertEqual(profile.driver_id, "generic-web")
        self.assertEqual(profile.runtime_port, 8787)
        self.assertEqual(target.target_type, "application")
        self.assertFalse(target.healthcheck_enabled)
        self.assertEqual(target_id.target_id, "app-discord-blue")
        self.assertEqual(
            runtime_records[0].env, {"DISCORD_BLUE_STATE_DIR": "/var/lib/discord-blue"}
        )
        self.assertEqual(secret_bindings[0].binding_key, "DISCORD_TOKEN")
        self.assertNotIn("secret_id", json.dumps(payload, sort_keys=True))
        self.assertNotIn("app-discord-blue", json.dumps(payload, sort_keys=True))
        self.assertNotIn("/var/lib/discord-blue", json.dumps(payload, sort_keys=True))
        self.assertNotIn("https://discord-blue.example.test", json.dumps(payload, sort_keys=True))
        self.assertNotIn("DISCORD_BLUE_STATE_DIR", json.dumps(payload, sort_keys=True))
        self.assertNotIn("DISCORD_TOKEN", json.dumps(payload, sort_keys=True))
        self.assertNotIn("test:discord-blue-onboarding", json.dumps(payload, sort_keys=True))

    def test_product_onboarding_endpoint_rejects_provider_target_conflict(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            try:
                store.ensure_schema()
                store.write_provider_target_record(
                    ProviderTargetRecord(
                        context="discord-blue",
                        instance="prod",
                        provider_id="dokploy",
                        target_category="application",
                        target_id="stale-app-discord-blue",
                        display_name="discord-blue",
                        provider_target_type="application",
                        provider_evidence={"project_name": "Discord Blue"},
                        updated_at="2026-05-04T17:59:00Z",
                        source_label="test:stale-provider-target",
                    )
                )
            finally:
                store.close()
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
                            "actions": ["product_onboarding.apply"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_wsgi_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/product-onboarding/apply",
                payload={
                    "schema_version": 1,
                    "product": "launchplane",
                    "manifest": {
                        "product": "discord-blue",
                        "display_name": "Discord Blue",
                        "repository": "cbusillo/discord-blue",
                        "driver_id": "generic-web",
                        "image_repository": "ghcr.io/cbusillo/discord-blue",
                        "runtime_port": 8787,
                        "health_path": "/health",
                        "lanes": [
                            {
                                "instance": "prod",
                                "context": "discord-blue",
                                "base_url": "https://discord-blue.example.test",
                            },
                        ],
                        "provider_targets": [
                            {
                                "context": "discord-blue",
                                "instance": "prod",
                                "target_id": "app-discord-blue",
                                "target_type": "application",
                                "target_name": "discord-blue",
                                "healthcheck_enabled": False,
                            }
                        ],
                        "updated_at": "2026-05-04T18:00:00Z",
                        "source_label": "test:discord-blue-onboarding",
                    },
                },
                headers={"Idempotency-Key": "product-onboarding-discord-blue-conflict"},
            )

        self.assertEqual(status_code, 400)
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "invalid_product_onboarding_manifest")
        self.assertIn("dual-write conflict", payload["error"]["message"])

    def test_product_onboarding_endpoint_rejects_self_deploy_authority(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
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
            app = create_launchplane_fastapi_wsgi_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/product-onboarding/apply",
                payload={
                    "schema_version": 1,
                    "product": "launchplane",
                    "manifest": {
                        "product": "discord-blue",
                        "display_name": "Discord Blue",
                        "repository": "cbusillo/discord-blue",
                        "driver_id": "generic-web",
                        "image_repository": "ghcr.io/cbusillo/discord-blue",
                        "runtime_port": 8787,
                        "health_path": "/health",
                        "lanes": [
                            {
                                "instance": "prod",
                                "context": "discord-blue",
                                "base_url": "https://discord-blue.example.test",
                            }
                        ],
                        "updated_at": "2026-05-04T18:00:00Z",
                        "source_label": "test:discord-blue-onboarding",
                    },
                },
                headers={"Idempotency-Key": "product-onboarding-self-deploy-denied"},
            )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_product_onboarding_endpoint_requires_database_storage(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/launchplane",
                            "workflow_refs": [
                                "cbusillo/launchplane/.github/workflows/product-onboarding.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["product_onboarding.apply"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_wsgi_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/product-onboarding.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
                record_store_factory=lambda: FilesystemRecordStore(state_dir=root / "state"),
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/product-onboarding/apply",
                payload={
                    "schema_version": 1,
                    "product": "launchplane",
                    "manifest": {
                        "product": "discord-blue",
                        "display_name": "Discord Blue",
                        "repository": "cbusillo/discord-blue",
                        "driver_id": "generic-web",
                        "image_repository": "ghcr.io/cbusillo/discord-blue",
                        "runtime_port": 8787,
                        "health_path": "/health",
                        "lanes": [
                            {
                                "instance": "prod",
                                "context": "discord-blue",
                                "base_url": "https://discord-blue.example.test",
                            }
                        ],
                        "updated_at": "2026-05-04T18:00:00Z",
                        "source_label": "test:discord-blue-onboarding",
                    },
                },
                headers={"Idempotency-Key": "product-onboarding-filesystem-denied"},
            )

        self.assertEqual(status_code, 503)
        self.assertEqual(payload["error"]["code"], "database_required")

    def test_product_onboarding_endpoint_checks_authz_before_database_storage(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/launchplane",
                            "workflow_refs": [
                                "cbusillo/launchplane/.github/workflows/product-onboarding.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["launchplane_service_deploy.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_wsgi_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/product-onboarding.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
                record_store_factory=lambda: FilesystemRecordStore(state_dir=root / "state"),
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/product-onboarding/apply",
                payload={
                    "schema_version": 1,
                    "product": "launchplane",
                    "manifest": {
                        "product": "discord-blue",
                        "display_name": "Discord Blue",
                        "repository": "cbusillo/discord-blue",
                        "driver_id": "generic-web",
                        "image_repository": "ghcr.io/cbusillo/discord-blue",
                        "runtime_port": 8787,
                        "health_path": "/health",
                        "lanes": [
                            {
                                "instance": "prod",
                                "context": "discord-blue",
                                "base_url": "https://discord-blue.example.test",
                            }
                        ],
                        "updated_at": "2026-05-04T18:00:00Z",
                        "source_label": "test:discord-blue-onboarding",
                    },
                },
                headers={"Idempotency-Key": "product-onboarding-authz-denied"},
            )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_openapi_includes_product_onboarding_contract(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=LaunchplaneAuthzPolicy(),
            record_store_factory=lambda: FilesystemRecordStore(state_dir=Path("unused")),
        )

        payload = app.openapi()

        route = payload["paths"]["/v1/product-onboarding/apply"]["post"]
        self.assertEqual(route["operationId"], "apply_product_onboarding")
        self.assertEqual(route["responses"]["202"]["description"], "Successful Response")
        request_schema = route["requestBody"]["content"]["application/json"]["schema"]
        self.assertEqual(request_schema["title"], "ProductOnboardingApplyEnvelope")
        self.assertEqual(request_schema["additionalProperties"], False)

    def test_product_onboarding_endpoint_is_retired_from_legacy_wsgi_app(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/product-onboarding/apply",
                payload={
                    "schema_version": 1,
                    "product": "launchplane",
                    "manifest": {
                        "product": "discord-blue",
                        "display_name": "Discord Blue",
                        "repository": "cbusillo/discord-blue",
                        "driver_id": "generic-web",
                        "lanes": [
                            {
                                "instance": "prod",
                                "context": "discord-blue",
                            }
                        ],
                    },
                },
            )

        self.assertEqual(status_code, 404)
        self.assertEqual(payload["error"]["code"], "not_found")

    def test_provider_target_operation_endpoint_backfills_route(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _seed_tracked_target_records(
                database_url=database_url,
                context="verireel",
                instance="testing",
                target_id="app-verireel-testing",
                target_type="application",
                target_name="ver-testing-app",
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/launchplane",
                            "workflow_refs": [
                                "cbusillo/launchplane/.github/workflows/provider-target-operations.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": [
                                "provider_target.audit",
                                "provider_target.backfill",
                            ],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_wsgi_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/provider-target-operations.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )

            dry_run_status, dry_run_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/provider-targets/operations",
                payload={
                    "schema_version": 1,
                    "mode": "backfill-dry-run",
                    "product": "launchplane",
                    "provider_id": "dokploy",
                    "context": "verireel",
                    "instance": "testing",
                },
                headers={"Idempotency-Key": "provider-target-verireel-testing-dry"},
            )
            dry_run_rerun_status, dry_run_rerun_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/provider-targets/operations",
                payload={
                    "schema_version": 1,
                    "mode": "backfill-dry-run",
                    "product": "launchplane",
                    "provider_id": "dokploy",
                    "context": "verireel",
                    "instance": "testing",
                },
                headers={"Idempotency-Key": "provider-target-verireel-testing-dry"},
            )
            apply_status, apply_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/provider-targets/operations",
                payload={
                    "schema_version": 1,
                    "mode": "backfill-apply",
                    "product": "launchplane",
                    "provider_id": "dokploy",
                    "context": "verireel",
                    "instance": "testing",
                    "reason": "Seed provider-target row for Phase Two cutover.",
                },
                headers={"Idempotency-Key": "provider-target-verireel-testing"},
            )
            audit_status, audit_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/provider-targets/operations",
                payload={
                    "schema_version": 1,
                    "mode": "audit",
                    "product": "launchplane",
                    "provider_id": "dokploy",
                    "context": "verireel",
                    "instance": "testing",
                },
            )
            store = PostgresRecordStore(database_url=database_url)
            try:
                provider_target = store.read_provider_target_record(
                    context_name="verireel", instance_name="testing"
                )
            finally:
                store.close()

        self.assertEqual(dry_run_status, 202)
        self.assertEqual(dry_run_payload["result"]["report"]["counts"], {"would-create": 1})
        self.assertEqual(dry_run_rerun_status, 202)
        self.assertNotIn("replayed", dry_run_rerun_payload)
        self.assertEqual(dry_run_rerun_payload["result"]["report"]["counts"], {"would-create": 1})
        self.assertEqual(apply_status, 202)
        self.assertEqual(apply_payload["result"]["report"]["counts"], {"created": 1})
        self.assertEqual(audit_status, 202)
        self.assertEqual(audit_payload["result"]["report"]["counts"], {"complete": 1})
        self.assertIsNotNone(provider_target)
        assert provider_target is not None
        self.assertEqual(provider_target.target_id, "app-verireel-testing")

    def test_dokploy_target_setup_endpoint_dry_run_does_not_persist(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            try:
                store.ensure_schema()
            finally:
                store.close()
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/launchplane",
                            "workflow_refs": [
                                "cbusillo/launchplane/.github/workflows/dokploy-target-setup.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["dokploy_target.setup"],
                        }
                    ]
                }
            )
            app = create_launchplane_dokploy_target_setup_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/dokploy-target-setup.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )
            with patch(
                "control_plane.dokploy_target_setup_http.control_plane_dokploy.read_dokploy_config",
                return_value=("https://dokploy.example.invalid", "token"),
            ):
                status_code, payload = _invoke_dokploy_target_setup_app(
                    app,
                    method="POST",
                    path="/v1/dokploy-targets/setup",
                    payload={
                        "schema_version": 1,
                        "mode": "dry-run",
                        "operation": "create-compose",
                        "product": "launchplane",
                        "context": "cm_website",
                        "instance": "testing",
                        "target_name": "cm-website-testing",
                        "project_name": "Odoo",
                        "environment_name": "production",
                        "server_id": "server-123",
                        "domains": ["cm-website-testing.shinycomputers.com"],
                        "runtime_port": 8069,
                        "deploy_timeout_seconds": 900,
                    },
                )
            store = PostgresRecordStore(database_url=database_url)
            try:
                target_records = store.list_dokploy_target_records()
            finally:
                store.close()

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["result"]["mode"], "dry-run")
        self.assertFalse(payload["result"]["applied"])
        self.assertEqual(
            payload["result"]["setup"]["target_id_record"]["target_id"],
            "planned-compose-id",
        )
        self.assertEqual(target_records, ())

    def test_dokploy_target_setup_endpoint_maps_click_exception_to_bad_request(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            try:
                store.ensure_schema()
            finally:
                store.close()
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/launchplane",
                            "workflow_refs": [
                                "cbusillo/launchplane/.github/workflows/dokploy-target-setup.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["dokploy_target.setup"],
                        }
                    ]
                }
            )
            app = create_launchplane_dokploy_target_setup_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/dokploy-target-setup.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )

            with patch(
                "control_plane.http_app.execute_dokploy_target_setup",
                side_effect=ClickException("Dokploy config missing."),
            ):
                status_code, payload = _invoke_dokploy_target_setup_app(
                    app,
                    method="POST",
                    path="/v1/dokploy-targets/setup",
                    payload={
                        "schema_version": 1,
                        "mode": "dry-run",
                        "operation": "create-compose",
                        "product": "launchplane",
                        "context": "cm_website",
                        "instance": "testing",
                        "target_name": "cm-website-testing",
                        "project_name": "Odoo",
                        "environment_name": "production",
                        "server_id": "server-123",
                    },
                )

        self.assertEqual(status_code, 400)
        self.assertEqual(payload["error"]["code"], "invalid_dokploy_target_setup")
        self.assertIn("Dokploy config missing.", payload["error"]["message"])

    def test_dokploy_target_inspect_read_is_retired_from_legacy_wsgi_app(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=root,
                local_record_store_for_tests=FilesystemRecordStore(root / "state"),
            )

            status_code, payload = _invoke_app(
                app,
                method="GET",
                path="/v1/dokploy-targets/inspect",
                query_string="context=cm_website&instance=prod",
            )

        self.assertEqual(status_code, 404)
        self.assertEqual(payload["error"]["code"], "not_found")

    def test_dokploy_target_setup_is_retired_from_legacy_wsgi_app(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=root,
                local_record_store_for_tests=FilesystemRecordStore(root / "state"),
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/dokploy-targets/setup",
                payload={
                    "schema_version": 1,
                    "mode": "dry-run",
                    "operation": "create-compose",
                    "product": "launchplane",
                    "context": "cm_website",
                    "instance": "testing",
                    "target_name": "cm-website-testing",
                    "server_id": "server-123",
                },
            )

        self.assertEqual(status_code, 404)
        self.assertEqual(payload["error"]["code"], "not_found")

    def test_dokploy_target_setup_endpoint_applies_compose_target(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            try:
                store.ensure_schema()
            finally:
                store.close()
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/launchplane",
                            "workflow_refs": [
                                "cbusillo/launchplane/.github/workflows/dokploy-target-setup.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["dokploy_target.setup"],
                        }
                    ]
                }
            )
            app = create_launchplane_dokploy_target_setup_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/dokploy-target-setup.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )
            provider_requests: list[tuple[str, dict[str, object]]] = []
            domain_routes: list[tuple[str, int]] = []

            def _mutate_provider(
                _host: str, _token: str, path: str, payload: dict[str, object]
            ) -> dict[str, object]:
                provider_requests.append((path, payload))
                if path == "/api/project.create":
                    return {"projectId": "project-123"}
                if path == "/api/environment.create":
                    return {"environmentId": "env-123"}
                if path == "/api/compose.create":
                    return {"composeId": "compose-123"}
                raise AssertionError(path)

            def _fetch_target(
                _host: str, _token: str, _target_type: str, _target_id: str
            ) -> dict[str, object]:
                return {
                    "name": "cm-website-testing",
                    "sourceType": "raw",
                    "composePath": "docker-compose.yml",
                    "environment": {"project": {"name": "Odoo"}},
                }

            def _ensure_domain(
                *,
                host: str,
                token: str,
                compose_id: str,
                domain_host: str,
                runtime_port: int,
                certificate_type: str = "none",
            ) -> str:
                del host, token, compose_id, certificate_type
                domain_routes.append((domain_host, runtime_port))
                return "domain-123"

            with (
                patch(
                    "control_plane.dokploy_target_setup_http.control_plane_dokploy.read_dokploy_config",
                    return_value=("https://dokploy.example.invalid", "token"),
                ),
                patch(
                    "control_plane.dokploy_target_setup_http.mutate_dokploy_payload_for_target_setup",
                    side_effect=_mutate_provider,
                ),
                patch(
                    "control_plane.dokploy_target_setup_http.fetch_dokploy_target_payload_for_setup",
                    side_effect=_fetch_target,
                ),
                patch(
                    "control_plane.dokploy_target_setup_http.control_plane_dokploy.ensure_compose_web_domain_route",
                    side_effect=_ensure_domain,
                ),
            ):
                status_code, payload = _invoke_dokploy_target_setup_app(
                    app,
                    method="POST",
                    path="/v1/dokploy-targets/setup",
                    payload={
                        "schema_version": 1,
                        "mode": "apply",
                        "operation": "create-compose",
                        "product": "launchplane",
                        "context": "cm_website",
                        "instance": "testing",
                        "target_name": "cm-website-testing",
                        "project_name": "Odoo",
                        "environment_name": "production",
                        "server_id": "server-123",
                        "domains": ["cm-website-testing.shinycomputers.com"],
                        "runtime_port": 8069,
                        "deploy_timeout_seconds": 900,
                        "confirmation": "APPLY DOKPLOY TARGET SETUP",
                        "reason": "Create cm website testing target.",
                    },
                    headers={"Idempotency-Key": "dokploy-target-setup-cm-website"},
                )
            store = PostgresRecordStore(database_url=database_url)
            try:
                target_record = store.read_dokploy_target_record(
                    context_name="cm_website", instance_name="testing"
                )
                target_id_record = store.read_dokploy_target_id_record(
                    context_name="cm_website", instance_name="testing"
                )
                provider_target = store.read_provider_target_record(
                    context_name="cm_website", instance_name="testing"
                )
            finally:
                store.close()

        self.assertEqual(status_code, 202)
        self.assertTrue(payload["result"]["applied"])
        self.assertEqual(payload["result"]["route_domain_ids"], ["domain-123"])
        self.assertEqual(
            [path for path, _payload in provider_requests],
            ["/api/project.create", "/api/environment.create", "/api/compose.create"],
        )
        compose_payload = provider_requests[-1][1]
        self.assertNotIn("sourceType", compose_payload)
        self.assertNotIn("composePath", compose_payload)
        self.assertEqual(domain_routes, [("cm-website-testing.shinycomputers.com", 8069)])
        self.assertEqual(target_record.target_type, "compose")
        self.assertEqual(target_record.target_name, "cm-website-testing")
        self.assertEqual(target_id_record.target_id, "compose-123")
        self.assertEqual(provider_target.target_id, "compose-123")

    def test_dokploy_target_setup_endpoint_replaces_provider_target_with_expected_authority(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            stale_provider_target = ProviderTargetRecord(
                context="cm_website",
                instance="prod",
                provider_id="dokploy",
                target_category="compose",
                target_id="compose-cm-prod",
                display_name="cm-prod",
                provider_target_type="compose",
                provider_evidence={"project_name": "Odoo"},
                updated_at="2026-06-14T03:53:56Z",
                source_label="test:stale-provider-target",
            )
            store = PostgresRecordStore(database_url=database_url)
            try:
                store.ensure_schema()
                store.write_provider_target_record(stale_provider_target)
            finally:
                store.close()
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/launchplane",
                            "workflow_refs": [
                                "cbusillo/launchplane/.github/workflows/dokploy-target-setup.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["dokploy_target.setup"],
                        }
                    ]
                }
            )
            app = create_launchplane_dokploy_target_setup_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/dokploy-target-setup.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )
            with (
                patch(
                    "control_plane.dokploy_target_setup_http.control_plane_dokploy.read_dokploy_config",
                    return_value=("https://dokploy.example.invalid", "token"),
                ),
                patch(
                    "control_plane.dokploy_target_setup_http.fetch_dokploy_target_payload_for_setup",
                    return_value={
                        "name": "odoo-tenant-cm-website-prod",
                        "sourceType": "raw",
                        "composePath": "docker-compose.yml",
                        "environment": {"project": {"name": "Odoo"}},
                    },
                ),
            ):
                status_code, payload = _invoke_dokploy_target_setup_app(
                    app,
                    method="POST",
                    path="/v1/dokploy-targets/setup",
                    payload={
                        "schema_version": 1,
                        "mode": "apply",
                        "operation": "adopt",
                        "product": "launchplane",
                        "context": "cm_website",
                        "instance": "prod",
                        "target_type": "compose",
                        "target_id": "compose-cm-website-prod",
                        "target_name": "odoo-tenant-cm-website-prod",
                        "project_name": "Odoo",
                        "domains": ["cm-website-prod.shinycomputers.com"],
                        "healthcheck_path": "/web/health",
                        "expected_current_provider_target": (
                            stale_provider_target.to_deployed_target_reference().model_dump(
                                mode="json"
                            )
                        ),
                        "confirmation": "APPLY DOKPLOY TARGET SETUP",
                        "reason": "Repair cm website prod target authority.",
                    },
                    headers={"Idempotency-Key": "dokploy-target-setup-cm-website-prod"},
                )
            store = PostgresRecordStore(database_url=database_url)
            try:
                target_record = store.read_dokploy_target_record(
                    context_name="cm_website", instance_name="prod"
                )
                target_id_record = store.read_dokploy_target_id_record(
                    context_name="cm_website", instance_name="prod"
                )
                provider_target = store.read_provider_target_record(
                    context_name="cm_website", instance_name="prod"
                )
            finally:
                store.close()

        self.assertEqual(status_code, 202)
        self.assertTrue(payload["result"]["applied"])
        self.assertEqual(target_record.target_name, "odoo-tenant-cm-website-prod")
        self.assertEqual(target_id_record.target_id, "compose-cm-website-prod")
        self.assertEqual(provider_target.target_id, "compose-cm-website-prod")

    def test_dokploy_target_setup_reconciles_existing_compose_domain_route(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _seed_tracked_target_records(
                database_url=database_url,
                context="cm_website",
                instance="testing",
                target_id="compose-cm-website-testing",
                target_type="compose",
                target_name="cm-website-testing",
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/launchplane",
                            "workflow_refs": [
                                "cbusillo/launchplane/.github/workflows/dokploy-target-setup.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["dokploy_target.setup"],
                        }
                    ]
                }
            )
            app = create_launchplane_dokploy_target_setup_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/dokploy-target-setup.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )
            domain_routes: list[tuple[str, str, int]] = []

            def _ensure_domain(
                *,
                host: str,
                token: str,
                compose_id: str,
                domain_host: str,
                runtime_port: int,
                certificate_type: str = "none",
            ) -> str:
                del host, token, certificate_type
                domain_routes.append((compose_id, domain_host, runtime_port))
                return "domain-cm-website-testing"

            with (
                patch(
                    "control_plane.dokploy_target_setup_http.control_plane_dokploy.read_dokploy_config",
                    return_value=("https://dokploy.example.invalid", "token"),
                ),
                patch(
                    "control_plane.dokploy_target_setup_http.control_plane_dokploy.ensure_compose_web_domain_route",
                    side_effect=_ensure_domain,
                ),
            ):
                status_code, payload = _invoke_dokploy_target_setup_app(
                    app,
                    method="POST",
                    path="/v1/dokploy-targets/setup",
                    payload={
                        "schema_version": 1,
                        "mode": "apply",
                        "operation": "reconcile-compose-domain",
                        "product": "launchplane",
                        "context": "cm_website",
                        "instance": "testing",
                        "domains": [
                            "cm-website-testing.shinycomputers.com",
                            "cm-website-alt.shinycomputers.com",
                            "cm-website-testing.shinycomputers.com",
                        ],
                        "runtime_port": 8069,
                        "confirmation": "APPLY DOKPLOY TARGET SETUP",
                        "reason": "Reconcile cm website testing compose domain route.",
                    },
                    headers={"Idempotency-Key": "dokploy-compose-domain-cm-website"},
                )
            store = PostgresRecordStore(database_url=database_url)
            try:
                target_record = store.read_dokploy_target_record(
                    context_name="cm_website",
                    instance_name="testing",
                )
            finally:
                store.close()

        self.assertEqual(status_code, 202)
        self.assertTrue(payload["result"]["applied"])
        self.assertEqual(payload["result"]["operation"], "reconcile-compose-domain")
        self.assertEqual(
            payload["result"]["route_domain_ids"],
            ["domain-cm-website-testing", "domain-cm-website-testing"],
        )
        self.assertNotIn("route_domain_ids", payload["result"]["setup"])
        self.assertEqual(
            domain_routes,
            [
                (
                    "compose-cm-website-testing",
                    "cm-website-testing.shinycomputers.com",
                    8069,
                ),
                (
                    "compose-cm-website-testing",
                    "cm-website-alt.shinycomputers.com",
                    8069,
                ),
            ],
        )
        self.assertEqual(
            target_record.domains,
            (
                "cm-website-testing.shinycomputers.com",
                "cm-website-alt.shinycomputers.com",
            ),
        )
        self.assertEqual(
            target_record.source_label,
            "service:dokploy-targets:setup:reconcile-compose-domain",
        )

    def test_dokploy_target_setup_reconcile_compose_domain_dry_run_does_not_mutate(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _seed_tracked_target_records(
                database_url=database_url,
                context="cm_website",
                instance="testing",
                target_id="compose-cm-website-testing",
                target_type="compose",
                target_name="cm-website-testing",
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/launchplane",
                            "workflow_refs": [
                                "cbusillo/launchplane/.github/workflows/dokploy-target-setup.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["dokploy_target.setup"],
                        }
                    ]
                }
            )
            app = create_launchplane_dokploy_target_setup_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/dokploy-target-setup.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )

            with (
                patch(
                    "control_plane.dokploy_target_setup_http.control_plane_dokploy.read_dokploy_config",
                    return_value=("https://dokploy.example.invalid", "token"),
                ),
                patch(
                    "control_plane.dokploy_target_setup_http.control_plane_dokploy.ensure_compose_web_domain_route"
                ) as ensure_domain,
            ):
                status_code, payload = _invoke_dokploy_target_setup_app(
                    app,
                    method="POST",
                    path="/v1/dokploy-targets/setup",
                    payload={
                        "schema_version": 1,
                        "mode": "dry-run",
                        "operation": "reconcile-compose-domain",
                        "product": "launchplane",
                        "context": "cm_website",
                        "instance": "testing",
                        "domains": ["cm-website-testing.shinycomputers.com"],
                        "runtime_port": 8069,
                    },
                )

        self.assertEqual(status_code, 202)
        self.assertFalse(payload["result"]["applied"])
        self.assertEqual(payload["result"]["route_domain_ids"], [])
        self.assertNotIn("route_domain_ids", payload["result"]["setup"])
        self.assertEqual(
            payload["result"]["setup"]["warnings"],
            ["dry run only; Dokploy compose domain routes were not reconciled"],
        )
        ensure_domain.assert_not_called()

    def test_dokploy_target_setup_prune_compose_domain_dry_run_reports_matches(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _seed_tracked_target_records(
                database_url=database_url,
                context="cm",
                instance="prod",
                target_id="compose-cm-prod",
                target_type="compose",
                target_name="cm-prod",
                domains=(
                    "cm-prod.shinycomputers.com",
                    "cm-website-prod.shinycomputers.com",
                ),
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/launchplane",
                            "workflow_refs": [
                                "cbusillo/launchplane/.github/workflows/dokploy-target-setup.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["dokploy_target.setup"],
                        }
                    ]
                }
            )
            app = create_launchplane_dokploy_target_setup_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/dokploy-target-setup.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )

            with (
                patch(
                    "control_plane.dokploy_target_setup_http.control_plane_dokploy.read_dokploy_config",
                    return_value=("https://dokploy.example.invalid", "token"),
                ),
                patch(
                    "control_plane.dokploy_target_setup_http.fetch_dokploy_compose_domains_for_target_setup",
                    return_value=(
                        {
                            "host": "cm-prod.shinycomputers.com",
                            "domainId": "domain-cm-prod",
                            "composeId": "compose-cm-prod",
                            "domainType": "compose",
                            "serviceName": "web",
                            "path": "/",
                            "internalPath": "/",
                        },
                        {
                            "host": "cm-website-prod.shinycomputers.com",
                            "domainId": "domain-cm-website-prod-on-cm",
                            "composeId": "compose-cm-prod",
                            "domainType": "compose",
                            "serviceName": "web",
                            "path": "/",
                            "internalPath": "/",
                        },
                    ),
                ),
                patch(
                    "control_plane.dokploy_target_setup_http.delete_dokploy_domain_for_target_setup"
                ) as delete_domain,
            ):
                status_code, payload = _invoke_dokploy_target_setup_app(
                    app,
                    method="POST",
                    path="/v1/dokploy-targets/setup",
                    payload={
                        "schema_version": 1,
                        "mode": "dry-run",
                        "operation": "prune-compose-domain",
                        "product": "launchplane",
                        "context": "cm",
                        "instance": "prod",
                        "domains": ["cm-website-prod.shinycomputers.com"],
                    },
                )

        self.assertEqual(status_code, 202)
        self.assertFalse(payload["result"]["applied"])
        self.assertEqual(payload["result"]["route_domain_ids"], [])
        self.assertEqual(
            payload["result"]["setup"]["matched_domain_ids"],
            ["domain-cm-website-prod-on-cm"],
        )
        self.assertEqual(payload["result"]["setup"]["deleted_domain_ids"], [])
        self.assertEqual(payload["result"]["setup"]["missing_domains"], [])
        self.assertIn(
            "dry run only; Dokploy compose domain routes were not pruned",
            payload["result"]["setup"]["warnings"],
        )
        delete_domain.assert_not_called()

    def test_dokploy_target_setup_prune_compose_domain_deletes_matching_domains(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _seed_tracked_target_records(
                database_url=database_url,
                context="cm",
                instance="prod",
                target_id="compose-cm-prod",
                target_type="compose",
                target_name="cm-prod",
                domains=(
                    "cm-prod.shinycomputers.com",
                    "cm-website-prod.shinycomputers.com",
                ),
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/launchplane",
                            "workflow_refs": [
                                "cbusillo/launchplane/.github/workflows/dokploy-target-setup.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["dokploy_target.setup"],
                        }
                    ]
                }
            )
            app = create_launchplane_dokploy_target_setup_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/dokploy-target-setup.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )
            deleted_domains: list[str] = []

            def _delete_domain(*, host: str, token: str, domain_id: str) -> None:
                del host, token
                deleted_domains.append(domain_id)

            with (
                patch(
                    "control_plane.dokploy_target_setup_http.control_plane_dokploy.read_dokploy_config",
                    return_value=("https://dokploy.example.invalid", "token"),
                ),
                patch(
                    "control_plane.dokploy_target_setup_http.fetch_dokploy_compose_domains_for_target_setup",
                    return_value=(
                        {
                            "host": "cm-prod.shinycomputers.com",
                            "domainId": "domain-cm-prod",
                            "composeId": "compose-cm-prod",
                            "domainType": "compose",
                            "serviceName": "web",
                            "path": "/",
                            "internalPath": "/",
                        },
                        {
                            "host": "cm-website-prod.shinycomputers.com",
                            "domainId": "domain-cm-website-prod-on-cm",
                            "composeId": "compose-cm-prod",
                            "domainType": "compose",
                            "serviceName": "web",
                            "path": "/",
                            "internalPath": "/",
                        },
                    ),
                ),
                patch(
                    "control_plane.dokploy_target_setup_http.delete_dokploy_domain_for_target_setup",
                    side_effect=_delete_domain,
                ),
            ):
                status_code, payload = _invoke_dokploy_target_setup_app(
                    app,
                    method="POST",
                    path="/v1/dokploy-targets/setup",
                    payload={
                        "schema_version": 1,
                        "mode": "apply",
                        "operation": "prune-compose-domain",
                        "product": "launchplane",
                        "context": "cm",
                        "instance": "prod",
                        "domains": ["cm-website-prod.shinycomputers.com"],
                        "confirmation": "APPLY DOKPLOY TARGET SETUP",
                        "reason": "Remove stale cm website domain from full CM prod target.",
                    },
                    headers={"Idempotency-Key": "dokploy-prune-cm-prod-website-domain"},
                )
            store = PostgresRecordStore(database_url=database_url)
            try:
                target_record = store.read_dokploy_target_record(
                    context_name="cm", instance_name="prod"
                )
            finally:
                store.close()

        self.assertEqual(status_code, 202)
        self.assertTrue(payload["result"]["applied"])
        self.assertEqual(payload["result"]["operation"], "prune-compose-domain")
        self.assertEqual(
            payload["result"]["setup"]["matched_domain_ids"],
            ["domain-cm-website-prod-on-cm"],
        )
        self.assertEqual(
            payload["result"]["setup"]["deleted_domain_ids"],
            ["domain-cm-website-prod-on-cm"],
        )
        self.assertEqual(deleted_domains, ["domain-cm-website-prod-on-cm"])
        self.assertEqual(target_record.domains, ("cm-prod.shinycomputers.com",))
        self.assertEqual(
            target_record.source_label,
            "service:dokploy-targets:setup:prune-compose-domain",
        )

    def test_dokploy_target_setup_prune_compose_domain_skips_unrelated_routes(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _seed_tracked_target_records(
                database_url=database_url,
                context="cm",
                instance="prod",
                target_id="compose-cm-prod",
                target_type="compose",
                target_name="cm-prod",
                domains=("cm-website-prod.shinycomputers.com",),
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/launchplane",
                            "workflow_refs": [
                                "cbusillo/launchplane/.github/workflows/dokploy-target-setup.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["dokploy_target.setup"],
                        }
                    ]
                }
            )
            app = create_launchplane_dokploy_target_setup_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/dokploy-target-setup.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )

            with (
                patch(
                    "control_plane.dokploy_target_setup_http.control_plane_dokploy.read_dokploy_config",
                    return_value=("https://dokploy.example.invalid", "token"),
                ),
                patch(
                    "control_plane.dokploy_target_setup_http.fetch_dokploy_compose_domains_for_target_setup",
                    return_value=(
                        {
                            "host": "cm-website-prod.shinycomputers.com",
                            "domainId": "domain-other-service",
                            "composeId": "compose-cm-prod",
                            "domainType": "compose",
                            "serviceName": "longpolling",
                            "path": "/",
                            "internalPath": "/",
                        },
                        {
                            "host": "cm-website-prod.shinycomputers.com",
                            "domainId": "domain-other-path",
                            "composeId": "compose-cm-prod",
                            "domainType": "compose",
                            "serviceName": "web",
                            "path": "/shop",
                            "internalPath": "/",
                        },
                    ),
                ),
                patch(
                    "control_plane.dokploy_target_setup_http.delete_dokploy_domain_for_target_setup"
                ) as delete_domain,
            ):
                status_code, payload = _invoke_dokploy_target_setup_app(
                    app,
                    method="POST",
                    path="/v1/dokploy-targets/setup",
                    payload={
                        "schema_version": 1,
                        "mode": "dry-run",
                        "operation": "prune-compose-domain",
                        "product": "launchplane",
                        "context": "cm",
                        "instance": "prod",
                        "domains": ["cm-website-prod.shinycomputers.com"],
                    },
                )

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["result"]["setup"]["matched_domain_ids"], [])
        self.assertEqual(
            payload["result"]["setup"]["missing_domains"],
            ["cm-website-prod.shinycomputers.com"],
        )
        delete_domain.assert_not_called()

    def test_dokploy_target_setup_reconcile_compose_domain_rejects_missing_records(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            store.close()
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/launchplane",
                            "workflow_refs": [
                                "cbusillo/launchplane/.github/workflows/dokploy-target-setup.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["dokploy_target.setup"],
                        }
                    ]
                }
            )
            app = create_launchplane_dokploy_target_setup_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/dokploy-target-setup.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )

            with patch(
                "control_plane.dokploy_target_setup_http.control_plane_dokploy.read_dokploy_config",
                return_value=("https://dokploy.example.invalid", "token"),
            ):
                status_code, payload = _invoke_dokploy_target_setup_app(
                    app,
                    method="POST",
                    path="/v1/dokploy-targets/setup",
                    payload={
                        "schema_version": 1,
                        "mode": "dry-run",
                        "operation": "reconcile-compose-domain",
                        "product": "launchplane",
                        "context": "cm_website",
                        "instance": "testing",
                        "domains": ["cm-website-testing.shinycomputers.com"],
                        "runtime_port": 8069,
                    },
                )

        self.assertEqual(status_code, 400)
        self.assertEqual(payload["error"]["code"], "invalid_dokploy_target_setup")
        self.assertIn("requires tracked target records", payload["error"]["message"])

    def test_dokploy_target_setup_reconcile_compose_domain_rejects_application_target(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _seed_tracked_target_records(
                database_url=database_url,
                context="cm_website",
                instance="testing",
                target_id="app-cm-website-testing",
                target_type="application",
                target_name="cm-website-testing",
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/launchplane",
                            "workflow_refs": [
                                "cbusillo/launchplane/.github/workflows/dokploy-target-setup.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["dokploy_target.setup"],
                        }
                    ]
                }
            )
            app = create_launchplane_dokploy_target_setup_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/dokploy-target-setup.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )

            with patch(
                "control_plane.dokploy_target_setup_http.control_plane_dokploy.read_dokploy_config",
                return_value=("https://dokploy.example.invalid", "token"),
            ):
                status_code, payload = _invoke_dokploy_target_setup_app(
                    app,
                    method="POST",
                    path="/v1/dokploy-targets/setup",
                    payload={
                        "schema_version": 1,
                        "mode": "dry-run",
                        "operation": "reconcile-compose-domain",
                        "product": "launchplane",
                        "context": "cm_website",
                        "instance": "testing",
                        "domains": ["cm-website-testing.shinycomputers.com"],
                        "runtime_port": 8069,
                    },
                )

        self.assertEqual(status_code, 400)
        self.assertEqual(payload["error"]["code"], "invalid_dokploy_target_setup")
        self.assertIn("requires a compose target", payload["error"]["message"])

    def test_dokploy_target_setup_endpoint_applies_adopt(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            try:
                store.ensure_schema()
            finally:
                store.close()
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/launchplane",
                            "workflow_refs": [
                                "cbusillo/launchplane/.github/workflows/dokploy-target-setup.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["dokploy_target.setup"],
                        }
                    ]
                }
            )
            app = create_launchplane_dokploy_target_setup_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/dokploy-target-setup.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )

            with (
                patch(
                    "control_plane.dokploy_target_setup_http.control_plane_dokploy.read_dokploy_config",
                    return_value=("https://dokploy.example.invalid", "token"),
                ),
                patch(
                    "control_plane.dokploy_target_setup_http.fetch_dokploy_target_payload_for_setup",
                    return_value={
                        "name": "existing-compose",
                        "sourceType": "raw",
                        "composePath": "docker-compose.yml",
                        "environment": {"project": {"name": "Odoo"}},
                    },
                ),
            ):
                status_code, payload = _invoke_dokploy_target_setup_app(
                    app,
                    method="POST",
                    path="/v1/dokploy-targets/setup",
                    payload={
                        "schema_version": 1,
                        "mode": "apply",
                        "operation": "adopt",
                        "product": "launchplane",
                        "context": "cm_website",
                        "instance": "testing",
                        "target_type": "compose",
                        "target_id": "compose-existing",
                        "target_name": "cm-website-testing",
                        "project_name": "Odoo",
                        "domains": ["cm-website-testing.shinycomputers.com"],
                        "confirmation": "APPLY DOKPLOY TARGET SETUP",
                        "reason": "Adopt cm website testing target.",
                    },
                    headers={"Idempotency-Key": "dokploy-target-setup-adopt"},
                )
            store = PostgresRecordStore(database_url=database_url)
            try:
                target_id_record = store.read_dokploy_target_id_record(
                    context_name="cm_website", instance_name="testing"
                )
                provider_target = store.read_provider_target_record(
                    context_name="cm_website", instance_name="testing"
                )
            finally:
                store.close()

        self.assertEqual(status_code, 202)
        self.assertTrue(payload["result"]["applied"])
        self.assertEqual(payload["result"]["operation"], "adopt")
        self.assertEqual(target_id_record.target_id, "compose-existing")
        self.assertEqual(provider_target.target_id, "compose-existing")

    def test_dokploy_target_setup_endpoint_applies_application_target(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            try:
                store.ensure_schema()
            finally:
                store.close()
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/launchplane",
                            "workflow_refs": [
                                "cbusillo/launchplane/.github/workflows/dokploy-target-setup.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["dokploy_target.setup"],
                        }
                    ]
                }
            )
            app = create_launchplane_dokploy_target_setup_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/dokploy-target-setup.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )
            provider_requests: list[tuple[str, dict[str, object]]] = []

            def _mutate_provider(
                _host: str, _token: str, path: str, payload: dict[str, object]
            ) -> dict[str, object]:
                provider_requests.append((path, payload))
                if path == "/api/application.create":
                    return {"applicationId": "app-123"}
                raise AssertionError(path)

            with (
                patch(
                    "control_plane.dokploy_target_setup_http.control_plane_dokploy.read_dokploy_config",
                    return_value=("https://dokploy.example.invalid", "token"),
                ),
                patch(
                    "control_plane.dokploy_target_setup_http.mutate_dokploy_payload_for_target_setup",
                    side_effect=_mutate_provider,
                ),
                patch(
                    "control_plane.dokploy_target_setup_http.fetch_dokploy_target_payload_for_setup",
                    return_value={
                        "name": "discord-blue-prod",
                        "environment": {"project": {"name": "Discord Blue"}},
                    },
                ),
            ):
                status_code, payload = _invoke_dokploy_target_setup_app(
                    app,
                    method="POST",
                    path="/v1/dokploy-targets/setup",
                    payload={
                        "schema_version": 1,
                        "mode": "apply",
                        "operation": "create-application",
                        "product": "launchplane",
                        "context": "discord-blue",
                        "instance": "prod",
                        "target_name": "discord-blue-prod",
                        "environment_id": "env-existing",
                        "confirmation": "APPLY DOKPLOY TARGET SETUP",
                        "reason": "Create discord target.",
                    },
                    headers={"Idempotency-Key": "dokploy-target-setup-app"},
                )
            store = PostgresRecordStore(database_url=database_url)
            try:
                provider_target = store.read_provider_target_record(
                    context_name="discord-blue", instance_name="prod"
                )
            finally:
                store.close()

        self.assertEqual(status_code, 202)
        self.assertTrue(payload["result"]["applied"])
        self.assertEqual(payload["result"]["operation"], "create-application")
        self.assertEqual(
            [path for path, _payload in provider_requests], ["/api/application.create"]
        )
        self.assertEqual(provider_target.target_id, "app-123")
        self.assertEqual(provider_target.provider_target_type, "application")

    def test_dokploy_target_setup_rejects_runtime_port_for_adopt(self) -> None:
        app = create_launchplane_dokploy_target_setup_app(
            state_dir=Path("/tmp") / "launchplane-test-state",
            verifier=_StubVerifier(
                _identity(
                    repository="cbusillo/launchplane",
                    workflow_ref=(
                        "cbusillo/launchplane/.github/workflows/dokploy-target-setup.yml@refs/heads/main"
                    ),
                    event_name="workflow_dispatch",
                )
            ),
            authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
            control_plane_root_path=Path("/tmp"),
        )

        status_code, payload = _invoke_dokploy_target_setup_app(
            app,
            method="POST",
            path="/v1/dokploy-targets/setup",
            payload={
                "schema_version": 1,
                "mode": "dry-run",
                "operation": "adopt",
                "product": "launchplane",
                "context": "cm_website",
                "instance": "testing",
                "target_type": "compose",
                "target_id": "compose-existing",
                "domains": ["cm-website-testing.shinycomputers.com"],
                "runtime_port": 8069,
            },
        )

        self.assertEqual(status_code, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")

    def test_dokploy_target_setup_rejects_runtime_port_without_domains(self) -> None:
        app = create_launchplane_dokploy_target_setup_app(
            state_dir=Path("/tmp") / "launchplane-test-state",
            verifier=_StubVerifier(
                _identity(
                    repository="cbusillo/launchplane",
                    workflow_ref=(
                        "cbusillo/launchplane/.github/workflows/dokploy-target-setup.yml@refs/heads/main"
                    ),
                    event_name="workflow_dispatch",
                )
            ),
            authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
            control_plane_root_path=Path("/tmp"),
        )

        status_code, payload = _invoke_dokploy_target_setup_app(
            app,
            method="POST",
            path="/v1/dokploy-targets/setup",
            payload={
                "schema_version": 1,
                "mode": "dry-run",
                "operation": "create-compose",
                "product": "launchplane",
                "context": "cm_website",
                "instance": "testing",
                "target_name": "cm-website-testing",
                "server_id": "server-123",
                "runtime_port": 8069,
            },
        )

        self.assertEqual(status_code, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")

    def test_dokploy_target_setup_endpoint_rejects_apply_without_authz(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            try:
                store.ensure_schema()
            finally:
                store.close()
            app = create_launchplane_dokploy_target_setup_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/dokploy-target-setup.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=root,
                database_url=database_url,
            )

            status_code, payload = _invoke_dokploy_target_setup_app(
                app,
                method="POST",
                path="/v1/dokploy-targets/setup",
                payload={
                    "schema_version": 1,
                    "mode": "apply",
                    "operation": "create-compose",
                    "product": "launchplane",
                    "context": "cm_website",
                    "instance": "testing",
                    "target_name": "cm-website-testing",
                    "project_name": "Odoo",
                    "environment_name": "production",
                    "server_id": "server-123",
                    "confirmation": "APPLY DOKPLOY TARGET SETUP",
                    "reason": "Create cm website testing target.",
                },
                headers={"Idempotency-Key": "dokploy-target-setup-denied"},
            )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_provider_target_operation_endpoint_rejects_apply_without_authz(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _seed_tracked_target_records(
                database_url=database_url,
                context="verireel",
                instance="testing",
                target_id="app-verireel-testing",
                target_type="application",
                target_name="ver-testing-app",
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/launchplane",
                            "workflow_refs": [
                                "cbusillo/launchplane/.github/workflows/provider-target-operations.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["provider_target.audit"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_wsgi_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/provider-target-operations.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/provider-targets/operations",
                payload={
                    "schema_version": 1,
                    "mode": "backfill-apply",
                    "product": "launchplane",
                    "provider_id": "dokploy",
                    "context": "verireel",
                    "instance": "testing",
                    "reason": "Seed provider-target row for Phase Two cutover.",
                },
                headers={"Idempotency-Key": "provider-target-denied"},
            )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_provider_target_operation_endpoint_rejects_apply_without_idempotency_key(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            _seed_tracked_target_records(
                database_url=database_url,
                context="verireel",
                instance="testing",
                target_id="app-verireel-testing",
                target_type="application",
                target_name="ver-testing-app",
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/launchplane",
                            "workflow_refs": [
                                "cbusillo/launchplane/.github/workflows/provider-target-operations.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": [
                                "provider_target.audit",
                                "provider_target.backfill",
                            ],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_wsgi_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/provider-target-operations.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/provider-targets/operations",
                payload={
                    "schema_version": 1,
                    "mode": "backfill-apply",
                    "product": "launchplane",
                    "provider_id": "dokploy",
                    "context": "verireel",
                    "instance": "testing",
                    "reason": "Seed provider-target row for Phase Two cutover.",
                },
            )
            store = PostgresRecordStore(database_url=database_url)
            try:
                provider_targets = store.list_provider_target_records()
            finally:
                store.close()

        self.assertEqual(status_code, 400)
        self.assertEqual(payload["error"]["code"], "idempotency_key_required")
        self.assertEqual(provider_targets, ())

    def test_provider_target_operation_endpoint_requires_database_storage(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/launchplane",
                            "workflow_refs": [
                                "cbusillo/launchplane/.github/workflows/provider-target-operations.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["provider_target.audit"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_wsgi_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/provider-target-operations.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
                record_store_factory=lambda: FilesystemRecordStore(state_dir=root / "state"),
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/provider-targets/operations",
                payload={
                    "schema_version": 1,
                    "mode": "audit",
                    "product": "launchplane",
                    "provider_id": "dokploy",
                    "context": "verireel",
                    "instance": "testing",
                },
            )

        self.assertEqual(status_code, 503)
        self.assertEqual(payload["error"]["code"], "database_required")

    def test_openapi_includes_provider_target_operation_contract(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=LaunchplaneAuthzPolicy(),
            record_store_factory=lambda: FilesystemRecordStore(state_dir=Path("unused")),
        )

        payload = app.openapi()

        route = payload["paths"]["/v1/provider-targets/operations"]["post"]
        self.assertEqual(route["operationId"], "run_provider_target_operations")
        self.assertEqual(route["responses"]["202"]["description"], "Successful Response")
        request_schema = route["requestBody"]["content"]["application/json"]["schema"]
        self.assertEqual(request_schema["title"], "ProviderTargetOperationEnvelope")
        self.assertEqual(request_schema["additionalProperties"], False)

    def test_provider_target_operation_endpoint_is_retired_from_legacy_wsgi_app(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/provider-targets/operations",
                payload={
                    "schema_version": 1,
                    "mode": "audit",
                    "product": "launchplane",
                    "provider_id": "dokploy",
                    "context": "verireel",
                    "instance": "testing",
                },
            )

        self.assertEqual(status_code, 404)
        self.assertEqual(payload["error"]["code"], "not_found")

    def test_product_environment_read_legacy_wsgi_routes_are_retired(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                control_plane_root_path=root,
            )

            responses = [
                _invoke_app(app, method="GET", path=path)
                for path in (
                    "/v1/products",
                    "/v1/products/example-site",
                    "/v1/products/example-site/activity",
                    "/v1/products/example-site/environments",
                    "/v1/products/example-site/environments/prod",
                )
            ]

        for status_code, payload in responses:
            self.assertEqual(status_code, 404)
            self.assertEqual(payload["status"], "rejected")
            self.assertEqual(payload["error"]["code"], "not_found")

    def test_product_environment_config_status_legacy_wsgi_route_is_retired(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="GET",
                path="/v1/products/example-site/environments/prod/config-status",
            )

        self.assertEqual(status_code, 404)
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "not_found")

    def test_terminal_agent_read_token_rejects_non_read_routes_even_if_policy_grants_action(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload_with_prod())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "terminal_agents": [
                        {
                            "subjects": ["local-owner-agent"],
                            "token_labels": ["local-owner-read"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["generic_web_prod_promotion.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch.dict(
                os.environ,
                TERMINAL_AGENT_AUTH_ENV,
                clear=True,
            ):
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/prod-promotion",
                    authorization="Bearer terminal-read-token",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "promotion": {
                            "schema_version": 1,
                            "product": "sellyouroutboard",
                            "artifact_id": "ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                            "source_git_ref": "abc123",
                        },
                    },
                )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")
        self.assertIn("can only read", payload["error"]["message"])

    def test_product_context_routes_are_retired_from_legacy_wsgi_app(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                store.write_product_profile_record(
                    LaunchplaneProductProfileRecord.model_validate(
                        _product_profile_payload_with_prod()
                    )
                )
            finally:
                store.close()
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["launchplane"],
                            "actions": ["product_profile.read"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )

            audit_status_code, audit_payload = _invoke_app(
                app,
                method="GET",
                path="/v1/product-profiles/sellyouroutboard/context-cutover-audit",
                query_string=(
                    "source_context=sellyouroutboard-testing&target_context=sellyouroutboard"
                ),
            )
            cutover_status_code, cutover_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/product-profiles/context-cutover/apply",
                payload={
                    "product": "sellyouroutboard",
                    "source_context": "sellyouroutboard-testing",
                    "target_context": "sellyouroutboard",
                    "mode": "dry-run",
                },
            )
            cleanup_status_code, cleanup_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/product-profiles/legacy-context-cleanup/apply",
                payload={
                    "product": "sellyouroutboard",
                    "source_context": "sellyouroutboard-testing",
                    "target_context": "sellyouroutboard",
                    "mode": "dry-run",
                },
            )

        for status_code, payload in (
            (audit_status_code, audit_payload),
            (cutover_status_code, cutover_payload),
            (cleanup_status_code, cleanup_payload),
        ):
            self.assertEqual(status_code, 404)
            self.assertEqual(payload["error"]["code"], "not_found")

    def test_agent_write_intent_evaluate_is_retired_from_legacy_wsgi_app(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                control_plane_root_path=Path(temporary_directory_name),
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/agent/write-intents/evaluate",
                payload={
                    "intent": "every_code_rerun",
                    "mode": "dry_run",
                    "product": "launchplane",
                    "context": "launchplane",
                    "source_url": "https://github.com/cbusillo/launchplane/issues/386",
                    "reason": "Check whether rerun can be requested safely.",
                },
            )
            records = FilesystemRecordStore(state_dir).list_agent_write_intent_records(
                product="launchplane",
                context_name="launchplane",
            )

        self.assertEqual(status_code, 404)
        self.assertEqual(payload["error"]["code"], "not_found")
        self.assertEqual(records, ())

    def test_product_config_apply_is_retired_from_legacy_wsgi_service(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({}),
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/product-config/apply",
                payload=_product_config_payload(),
                headers={"Idempotency-Key": "product-config-legacy-retired"},
            )

        self.assertEqual(status_code, 404)
        self.assertEqual(payload["error"]["code"], "not_found")

    def test_generic_web_deploy_route_uses_profile_lane_for_authorization(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
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
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["generic_web_deploy.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
            )
            driver_result = SimpleNamespace(deployment_record_id="deployment-syo-testing")

            with patch(
                "control_plane.drivers.generic_web_dispatch.execute_generic_web_deploy",
                return_value=driver_result,
            ) as deploy:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/deploy",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "deploy": {
                            "schema_version": 1,
                            "product": "sellyouroutboard",
                            "instance": "testing",
                            "artifact_id": "ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                            "source_git_ref": "abc123",
                        },
                    },
                    headers={"Idempotency-Key": "generic-web-deploy-syo-testing"},
                )

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["records"]["deployment_record_id"], "deployment-syo-testing")
        deploy.assert_called_once()
        _, kwargs = deploy.call_args
        self.assertEqual(kwargs["profile"].product, "sellyouroutboard")
        self.assertEqual(kwargs["lane"].context, "sellyouroutboard-testing")

    def test_generic_web_deploy_route_accepts_base_driver_product(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            profile_payload = _product_profile_payload()
            profile_payload["driver_id"] = "odoo"
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(profile_payload)
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/odoo-tenant-cm",
                            "workflow_refs": [
                                "cbusillo/odoo-tenant-cm/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["generic_web_deploy.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/odoo-tenant-cm",
                        workflow_ref=(
                            "cbusillo/odoo-tenant-cm/.github/workflows/preview-control-plane.yml"
                            "@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )
            driver_result = SimpleNamespace(deployment_record_id="deployment-odoo-testing")

            with patch(
                "control_plane.drivers.generic_web_dispatch.execute_generic_web_deploy",
                return_value=driver_result,
            ) as deploy:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/deploy",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "deploy": {
                            "schema_version": 1,
                            "product": "sellyouroutboard",
                            "instance": "testing",
                            "artifact_id": "ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                            "source_git_ref": "abc123",
                        },
                    },
                    headers={"Idempotency-Key": "generic-web-deploy-derived-driver"},
                )

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["records"]["deployment_record_id"], "deployment-odoo-testing")
        deploy.assert_called_once()
        _, kwargs = deploy.call_args
        self.assertEqual(kwargs["profile"].driver_id, "odoo")
        self.assertEqual(kwargs["lane"].context, "sellyouroutboard-testing")
        self.assertIs(
            kwargs["post_deploy_executor"],
            execute_odoo_generic_web_post_deploy,
        )

    def test_generic_web_deploy_route_keeps_literal_generic_products_without_post_deploy_adapter(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
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
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["generic_web_deploy.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
            )
            driver_result = SimpleNamespace(deployment_record_id="deployment-syo-testing")

            with patch(
                "control_plane.drivers.generic_web_dispatch.execute_generic_web_deploy",
                return_value=driver_result,
            ) as deploy:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/deploy",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "deploy": {
                            "schema_version": 1,
                            "product": "sellyouroutboard",
                            "instance": "testing",
                            "artifact_id": "ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                            "source_git_ref": "abc123",
                        },
                    },
                    headers={"Idempotency-Key": "generic-web-deploy-syo-no-adapter"},
                )

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["records"]["deployment_record_id"], "deployment-syo-testing")
        deploy.assert_called_once()
        self.assertIsNone(deploy.call_args.kwargs["post_deploy_executor"])

    def test_generic_web_deploy_route_replays_post_deploy_failure_after_deploy_pass(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
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
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["generic_web_deploy.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
            )
            driver_result = GenericWebDeployResult(
                deployment_record_id="deployment-syo-testing-post-deploy-failed",
                deploy_status="pass",
                deploy_started_at="2026-05-26T02:00:00Z",
                deploy_finished_at="2026-05-26T02:05:00Z",
                product="sellyouroutboard",
                context="sellyouroutboard-testing",
                instance="testing",
                target_name="syo-testing",
                target_id="app-syo-testing",
                target_category="application",
                provider_id="dokploy",
                provider_target_type="application",
                post_deploy_status="fail",
                error_message="post-deploy failed after deploy passed",
            )
            request_payload = {
                "schema_version": 1,
                "product": "sellyouroutboard",
                "deploy": {
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "instance": "testing",
                    "artifact_id": "ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                    "source_git_ref": "abc123",
                },
            }

            with patch(
                "control_plane.drivers.generic_web_dispatch.execute_generic_web_deploy",
                return_value=driver_result,
            ) as deploy:
                first_status_code, first_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/deploy",
                    payload=request_payload,
                    headers={"Idempotency-Key": "generic-web-deploy-post-deploy-failed"},
                )
                second_status_code, second_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/deploy",
                    payload=request_payload,
                    headers={"Idempotency-Key": "generic-web-deploy-post-deploy-failed"},
                )

        self.assertEqual(first_status_code, 202)
        self.assertEqual(second_status_code, 202)
        self.assertEqual(first_payload["records"], second_payload["records"])
        self.assertEqual(
            second_payload["records"]["deployment_record_id"], driver_result.deployment_record_id
        )
        self.assertTrue(second_payload["replayed"])
        deploy.assert_called_once()

    def test_generic_web_deploy_route_replay_scrubs_retired_target_type_alias(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
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
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["generic_web_deploy.execute"],
                        }
                    ]
                }
            )
            identity = _identity()
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(identity),
                authz_policy=policy,
                control_plane_root_path=root,
            )
            driver_result = GenericWebDeployResult(
                deployment_record_id="deployment-syo-testing-retired-alias",
                deploy_status="pass",
                deploy_started_at="2026-05-26T02:00:00Z",
                deploy_finished_at="2026-05-26T02:05:00Z",
                product="sellyouroutboard",
                context="sellyouroutboard-testing",
                instance="testing",
                target_name="syo-testing",
                target_id="app-syo-testing",
                target_category="application",
                provider_id="dokploy",
                provider_target_type="application",
            )
            request_payload = {
                "schema_version": 1,
                "product": "sellyouroutboard",
                "deploy": {
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "instance": "testing",
                    "artifact_id": "ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                    "source_git_ref": "abc123",
                },
            }

            with patch(
                "control_plane.drivers.generic_web_dispatch.execute_generic_web_deploy",
                return_value=driver_result,
            ) as deploy:
                first_status_code, first_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/deploy",
                    payload=request_payload,
                    headers={"Idempotency-Key": "generic-web-deploy-retired-alias"},
                )
                idempotency_record = store.read_idempotency_record(
                    scope="|".join(
                        (
                            identity.repository,
                            identity.workflow_ref or identity.job_workflow_ref,
                            identity.subject,
                        )
                    ),
                    route_path="/v1/drivers/generic-web/deploy",
                    idempotency_key="generic-web-deploy-retired-alias",
                )
                self.assertIsNotNone(idempotency_record)
                assert idempotency_record is not None
                legacy_response_payload = idempotency_record.response_payload
                legacy_result_payload = legacy_response_payload.get("result")
                self.assertIsInstance(legacy_result_payload, dict)
                assert isinstance(legacy_result_payload, dict)
                legacy_result_payload["target_type"] = "application"
                store.write_idempotency_record(
                    idempotency_record.model_copy(
                        update={"response_payload": legacy_response_payload}, deep=True
                    )
                )
                second_status_code, second_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/deploy",
                    payload=request_payload,
                    headers={"Idempotency-Key": "generic-web-deploy-retired-alias"},
                )

        self.assertEqual(first_status_code, 202)
        self.assertNotIn("target_type", first_payload["result"])
        self.assertEqual(second_status_code, 202)
        self.assertTrue(second_payload["replayed"])
        self.assertEqual(second_payload["result"]["target_category"], "application")
        self.assertEqual(second_payload["result"]["provider_target_type"], "application")
        self.assertNotIn("target_type", second_payload["result"])
        deploy.assert_called_once()

    def test_generic_web_deploy_route_rejects_unknown_base_driver_product(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            profile_payload = _product_profile_payload()
            profile_payload["driver_id"] = "missing-driver"
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(profile_payload)
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/odoo-tenant-cm",
                            "workflow_refs": [
                                "cbusillo/odoo-tenant-cm/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["generic_web_deploy.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/odoo-tenant-cm",
                        workflow_ref=(
                            "cbusillo/odoo-tenant-cm/.github/workflows/preview-control-plane.yml"
                            "@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.drivers.generic_web_dispatch.execute_generic_web_deploy"
            ) as deploy:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/deploy",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "deploy": {
                            "schema_version": 1,
                            "product": "sellyouroutboard",
                            "instance": "testing",
                            "artifact_id": "ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                            "source_git_ref": "abc123",
                        },
                    },
                )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "product_driver_mismatch")
        deploy.assert_not_called()

    def test_generic_web_source_ref_deploy_route_uses_distinct_authz_and_replays(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
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
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["generic_web_source_ref_deploy.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
            )
            driver_result = DokployComposeSourceRefDeployResult(
                context="sellyouroutboard-testing",
                instance="testing",
                target_id="compose-syo-testing",
                target_name="syo-testing-compose",
                source_git_ref="abc123",
                provider_source_ref="refs/heads/launchplane-deploy/abc123",
                original_source_ref="main",
                restored_source_ref="main",
                deploy_status="pass",
            )
            request_payload = {
                "schema_version": 1,
                "product": "sellyouroutboard",
                "deploy": {
                    "schema_version": 1,
                    "context": "sellyouroutboard-testing",
                    "instance": "testing",
                    "source_git_ref": "abc123",
                    "provider_source_ref": "refs/heads/launchplane-deploy/abc123",
                },
            }

            with (
                patch(
                    "control_plane.drivers.generic_web_dispatch.control_plane_dokploy.read_dokploy_config",
                    return_value=("https://dokploy.example", "token"),
                ),
                patch(
                    "control_plane.drivers.generic_web_dispatch.execute_dokploy_compose_source_ref_deploy",
                    return_value=driver_result,
                ) as deploy,
            ):
                first_status_code, first_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/source-ref-deploy",
                    payload=request_payload,
                    headers={"Idempotency-Key": "generic-web-source-ref-syo-testing"},
                )
                second_status_code, second_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/source-ref-deploy",
                    payload=request_payload,
                    headers={"Idempotency-Key": "generic-web-source-ref-syo-testing"},
                )

        self.assertEqual(first_status_code, 202)
        self.assertEqual(second_status_code, 202)
        self.assertEqual(first_payload["records"]["target_id"], "compose-syo-testing")
        self.assertEqual(first_payload["result"]["source_git_ref"], "abc123")
        self.assertEqual(
            first_payload["result"]["provider_source_ref"],
            "refs/heads/launchplane-deploy/abc123",
        )
        self.assertTrue(second_payload["replayed"])
        self.assertEqual(first_payload["result"], second_payload["result"])
        deploy.assert_called_once()
        _, kwargs = deploy.call_args
        self.assertEqual(kwargs["request"].context, "sellyouroutboard-testing")
        self.assertEqual(kwargs["request"].instance, "testing")

    def test_generic_web_deploy_route_accepts_padded_lane_context(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            profile_payload = _product_profile_payload()
            profile_payload["lanes"] = tuple(
                {**lane, "context": f"  {lane['context']}  "}
                for lane in _product_profile_lanes(profile_payload)
            )
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(profile_payload)
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
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["generic_web_deploy.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
            )
            driver_result = SimpleNamespace(deployment_record_id="deployment-syo-testing")

            with patch(
                "control_plane.drivers.generic_web_dispatch.execute_generic_web_deploy",
                return_value=driver_result,
            ) as deploy:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/deploy",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "deploy": {
                            "schema_version": 1,
                            "product": "sellyouroutboard",
                            "instance": "testing",
                            "artifact_id": "ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                            "source_git_ref": "abc123",
                        },
                    },
                    headers={"Idempotency-Key": "generic-web-deploy-syo-padded-context"},
                )

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["records"]["deployment_record_id"], "deployment-syo-testing")
        deploy.assert_called_once()

    def test_generic_web_rollback_plan_route_writes_plan_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload_with_prod())
            )
            deployment_record = DeploymentRecord(
                record_id="deployment-syo-prod-previous",
                artifact_identity=ArtifactIdentityReference(
                    artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:abc123"
                ),
                context="sellyouroutboard-testing",
                instance="prod",
                source_git_ref="abc123",
                destination_health=HealthcheckEvidence(status="pass"),
                resolved_target=ResolvedTargetEvidence(
                    target_type="application",
                    target_id="app-prod",
                    target_name="syo-prod-app",
                ),
                deploy=DeploymentEvidence(
                    target_name="syo-prod-app",
                    target_type="application",
                    deploy_mode="dokploy-application-api",
                    deployment_id="deployment-provider-1",
                    status="pass",
                    started_at="2026-05-25T12:00:00Z",
                    finished_at="2026-05-25T12:01:00Z",
                ),
            )
            store.write_deployment_record(deployment_record)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["generic_web_prod_rollback.plan"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/prod-rollback-plan",
                payload={
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "rollback_plan": {
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "instance": "prod",
                        "rollback_deployment_record_id": "deployment-syo-prod-previous",
                    },
                },
                headers={"Idempotency-Key": "generic-web-rollback-plan-syo-prod"},
            )

            plans = store.list_generic_web_rollback_plan_records(
                context_name="sellyouroutboard-testing",
                instance_name="prod",
                limit=1,
            )
            plan = plans[0]

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["records"]["generic_web_rollback_plan_id"], plan.plan_id)
        self.assertEqual(plan.status, "ready")
        self.assertEqual(plan.product, "sellyouroutboard")
        self.assertEqual(plan.context, "sellyouroutboard-testing")
        self.assertEqual(plan.rollback_deployment_record_id, "deployment-syo-prod-previous")

    def test_generic_web_rollback_plan_route_rejects_unauthorized_context(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload_with_prod())
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
                            "products": ["sellyouroutboard"],
                            "contexts": ["other-context"],
                            "actions": ["generic_web_prod_rollback.plan"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/prod-rollback-plan",
                payload={
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "rollback_plan": {
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "instance": "prod",
                        "rollback_deployment_record_id": "deployment-syo-prod-previous",
                    },
                },
            )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_generic_web_rollback_plan_route_rejects_unknown_lane(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload_with_prod())
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
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["generic_web_prod_rollback.plan"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/prod-rollback-plan",
                payload={
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "rollback_plan": {
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "instance": "missing",
                        "rollback_deployment_record_id": "deployment-syo-prod-previous",
                    },
                },
            )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "product_driver_mismatch")

    def test_generic_web_rollback_plan_route_replays_idempotent_response(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload_with_prod())
            )
            store.write_deployment_record(
                DeploymentRecord(
                    record_id="deployment-syo-prod-previous",
                    artifact_identity=ArtifactIdentityReference(
                        artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:abc123"
                    ),
                    context="sellyouroutboard-testing",
                    instance="prod",
                    source_git_ref="abc123",
                    destination_health=HealthcheckEvidence(status="pass"),
                    resolved_target=ResolvedTargetEvidence(
                        target_type="application",
                        target_id="app-prod",
                        target_name="syo-prod-app",
                    ),
                    deploy=DeploymentEvidence(
                        target_name="syo-prod-app",
                        target_type="application",
                        deploy_mode="dokploy-application-api",
                        deployment_id="deployment-provider-1",
                        status="pass",
                        started_at="2026-05-25T12:00:00Z",
                        finished_at="2026-05-25T12:01:00Z",
                    ),
                )
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
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["generic_web_prod_rollback.plan"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
            )
            request_payload = {
                "schema_version": 1,
                "product": "sellyouroutboard",
                "rollback_plan": {
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "instance": "prod",
                    "rollback_deployment_record_id": "deployment-syo-prod-previous",
                },
            }

            first_status_code, first_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/prod-rollback-plan",
                payload=request_payload,
                headers={"Idempotency-Key": "generic-web-rollback-plan-syo-prod"},
            )
            second_status_code, second_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/prod-rollback-plan",
                payload=request_payload,
                headers={"Idempotency-Key": "generic-web-rollback-plan-syo-prod"},
            )
            plans = store.list_generic_web_rollback_plan_records(
                context_name="sellyouroutboard-testing",
                instance_name="prod",
                limit=10,
            )

        self.assertEqual(first_status_code, 202)
        self.assertEqual(second_status_code, 202)
        self.assertEqual(first_payload["records"], second_payload["records"])
        self.assertTrue(second_payload["replayed"])
        self.assertEqual(len(plans), 1)

    def test_odoo_rollback_plan_alias_is_retired(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/odoo/prod-rollback-plan",
                payload={
                    "schema_version": 1,
                    "product": "odoo-tenant-cm",
                    "rollback_plan": {
                        "schema_version": 1,
                        "product": "odoo-tenant-cm",
                        "instance": "prod",
                        "rollback_deployment_record_id": "deployment-cm-prod-previous",
                    },
                },
                headers={"Idempotency-Key": "odoo-rollback-plan-cm-prod"},
            )

        self.assertEqual(status_code, 404)
        self.assertEqual(payload["error"]["code"], "not_found")

    def test_generic_web_rollback_route_applies_ready_plan(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload_with_prod())
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
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["generic_web_prod_rollback.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
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
                "control_plane.drivers.generic_web_dispatch.execute_generic_web_rollback",
                return_value=driver_result,
            ) as rollback:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/prod-rollback",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "rollback": {
                            "schema_version": 1,
                            "product": "sellyouroutboard",
                            "instance": "prod",
                            "rollback_deployment_record_id": "deployment-syo-prod-previous",
                        },
                    },
                    headers={"Idempotency-Key": "generic-web-rollback-syo-prod"},
                )

        self.assertEqual(status_code, 202)
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

    def test_generic_web_rollback_route_passes_odoo_post_deploy_adapter_for_odoo_profile(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(
                    _odoo_profile_payload_with_prod_lane()
                )
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/odoo-tenant-cm",
                            "workflow_refs": [
                                "cbusillo/odoo-tenant-cm/.github/workflows/deploy-odoo.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["odoo-tenant-cm"],
                            "contexts": ["cm"],
                            "actions": ["generic_web_prod_rollback.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/odoo-tenant-cm",
                        workflow_ref=(
                            "cbusillo/odoo-tenant-cm/.github/workflows/deploy-odoo.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
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
                "control_plane.drivers.generic_web_dispatch.execute_generic_web_rollback",
                return_value=driver_result,
            ) as rollback:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/prod-rollback",
                    payload={
                        "schema_version": 1,
                        "product": "odoo-tenant-cm",
                        "rollback": {
                            "schema_version": 1,
                            "product": "odoo-tenant-cm",
                            "instance": "prod",
                            "rollback_deployment_record_id": "deployment-cm-prod-previous",
                        },
                    },
                    headers={"Idempotency-Key": "generic-web-rollback-cm-prod"},
                )

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["records"]["deployment_record_id"], "deployment-cm-prod-rollback")
        self.assertEqual(payload["records"]["post_deploy_status"], "skipped")
        rollback.assert_called_once()
        self.assertIs(
            rollback.call_args.kwargs["post_deploy_executor"],
            execute_odoo_generic_web_post_deploy,
        )

    def test_generic_web_rollback_route_replays_idempotent_response_shape(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload_with_prod())
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
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["generic_web_prod_rollback.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
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
            request_payload = {
                "schema_version": 1,
                "product": "sellyouroutboard",
                "rollback": {
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "instance": "prod",
                    "rollback_deployment_record_id": "deployment-syo-prod-previous",
                },
            }

            with patch(
                "control_plane.drivers.generic_web_dispatch.execute_generic_web_rollback",
                return_value=driver_result,
            ) as rollback:
                first_status_code, first_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/prod-rollback",
                    payload=request_payload,
                    headers={"Idempotency-Key": "generic-web-rollback-replay-syo-prod"},
                )
                second_status_code, second_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/prod-rollback",
                    payload=request_payload,
                    headers={"Idempotency-Key": "generic-web-rollback-replay-syo-prod"},
                )

        self.assertEqual(first_status_code, 202)
        self.assertEqual(second_status_code, 202)
        self.assertEqual(first_payload["records"], second_payload["records"])
        self.assertEqual(second_payload["records"]["rollback_status"], "pass")
        self.assertEqual(second_payload["records"]["deploy_status"], "pass")
        self.assertEqual(second_payload["records"]["post_deploy_status"], "skipped")
        self.assertTrue(second_payload["replayed"])
        rollback.assert_called_once()

    def test_generic_web_rollback_route_rejects_unauthorized_context(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload_with_prod())
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
                            "products": ["sellyouroutboard"],
                            "contexts": ["other-context"],
                            "actions": ["generic_web_prod_rollback.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/prod-rollback",
                payload={
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "rollback": {
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "instance": "prod",
                        "rollback_deployment_record_id": "deployment-syo-prod-previous",
                    },
                },
            )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_generic_web_deploy_route_resolves_literal_generic_web_profile(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(
                    _product_profile_payload("generic-web")
                )
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
                            "products": ["generic-web"],
                            "contexts": ["generic-web-testing"],
                            "actions": ["generic_web_deploy.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
            )
            driver_result = SimpleNamespace(deployment_record_id="deployment-generic-web-testing")

            with patch(
                "control_plane.drivers.generic_web_dispatch.execute_generic_web_deploy",
                return_value=driver_result,
            ) as deploy:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/deploy",
                    payload={
                        "schema_version": 1,
                        "product": "generic-web",
                        "deploy": {
                            "schema_version": 1,
                            "product": "generic-web",
                            "instance": "testing",
                            "artifact_id": "ghcr.io/cbusillo/generic-web@sha256:abc123",
                            "source_git_ref": "abc123",
                        },
                    },
                    headers={"Idempotency-Key": "generic-web-deploy-literal-driver"},
                )

        self.assertEqual(status_code, 202)
        self.assertEqual(
            payload["records"]["deployment_record_id"], "deployment-generic-web-testing"
        )
        deploy.assert_called_once()
        _, kwargs = deploy.call_args
        self.assertEqual(kwargs["profile"].product, "generic-web")
        self.assertEqual(kwargs["lane"].context, "generic-web-testing")

    def test_generic_web_deploy_route_rejects_wrong_product_context(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
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
                            "products": ["sellyouroutboard"],
                            "contexts": ["different-context"],
                            "actions": ["generic_web_deploy.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/deploy",
                payload={
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "deploy": {
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "instance": "testing",
                        "artifact_id": "ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                        "source_git_ref": "abc123",
                    },
                },
            )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_generic_web_prod_promotion_route_executes_for_authorized_product_context(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload_with_prod())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/promote-prod.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["generic_web_prod_promotion.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/promote-prod.yml"
                            "@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.drivers.generic_web_dispatch.execute_generic_web_prod_promotion",
                return_value=GenericWebProdPromotionResult(
                    product="sellyouroutboard",
                    context="sellyouroutboard-testing",
                    from_instance="testing",
                    to_instance="prod",
                    artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                    promotion_record_id="promotion-syo-testing-to-prod",
                    deployment_record_id="deployment-syo-prod",
                    inventory_record_id="sellyouroutboard-testing-prod",
                    promotion_status="pass",
                    deployment_status="pass",
                    backup_status="skipped",
                    source_health_status="pass",
                    destination_health_status="pass",
                    target_name="syo-prod-app",
                    target_id="app-123",
                    target_category="application",
                    provider_id="dokploy",
                    provider_target_type="application",
                ),
            ) as execute_mock:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/prod-promotion",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "promotion": {
                            "schema_version": 1,
                            "product": "sellyouroutboard",
                            "artifact_id": "ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                            "source_git_ref": "abc123",
                        },
                    },
                    headers={"Idempotency-Key": "generic-web-prod-promotion-syo"},
                )

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(payload["records"]["promotion_record_id"], "promotion-syo-testing-to-prod")
        self.assertEqual(payload["records"]["deployment_record_id"], "deployment-syo-prod")
        self.assertEqual(payload["records"]["inventory_record_id"], "sellyouroutboard-testing-prod")
        self.assertEqual(payload["result"]["source_health_status"], "pass")
        self.assertEqual(payload["result"]["destination_health_status"], "pass")
        self.assertEqual(payload["result"]["target_category"], "application")
        self.assertEqual(payload["result"]["provider_id"], "dokploy")
        self.assertEqual(payload["result"]["provider_target_type"], "application")
        self.assertNotIn("target_type", payload["result"])
        execute_mock.assert_called_once()

    def test_generic_web_prod_promotion_route_replays_idempotent_response(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload_with_prod())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/promote-prod.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["generic_web_prod_promotion.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/promote-prod.yml"
                            "@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )
            request_payload = {
                "schema_version": 1,
                "product": "sellyouroutboard",
                "promotion": {
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "artifact_id": "ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                    "source_git_ref": "abc123",
                },
            }

            with patch(
                "control_plane.drivers.generic_web_dispatch.execute_generic_web_prod_promotion",
                return_value=GenericWebProdPromotionResult(
                    product="sellyouroutboard",
                    context="sellyouroutboard-testing",
                    from_instance="testing",
                    to_instance="prod",
                    artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                    promotion_record_id="promotion-syo-testing-to-prod",
                    deployment_record_id="deployment-syo-prod",
                    inventory_record_id="sellyouroutboard-testing-prod",
                    promotion_status="pass",
                    deployment_status="pass",
                    backup_status="skipped",
                    source_health_status="pass",
                    destination_health_status="pass",
                    target_name="syo-prod-app",
                    target_id="app-123",
                    target_category="application",
                    provider_id="dokploy",
                    provider_target_type="application",
                ),
            ) as execute_mock:
                first_status_code, first_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/prod-promotion",
                    payload=request_payload,
                    headers={"Idempotency-Key": "generic-web-prod-promotion-replay"},
                )
                second_status_code, second_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/prod-promotion",
                    payload=request_payload,
                    headers={"Idempotency-Key": "generic-web-prod-promotion-replay"},
                )

        self.assertEqual(first_status_code, 202)
        self.assertEqual(second_status_code, 202)
        self.assertEqual(first_payload["records"], second_payload["records"])
        self.assertEqual(first_payload["result"], second_payload["result"])
        self.assertTrue(second_payload["replayed"])
        self.assertNotIn("target_type", second_payload["result"])
        execute_mock.assert_called_once()

    def test_generic_web_prod_promotion_route_rejects_wrong_product_context(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload_with_prod())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/promote-prod.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["different-context"],
                            "actions": ["generic_web_prod_promotion.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/promote-prod.yml"
                            "@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/prod-promotion",
                payload={
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "promotion": {
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "artifact_id": "ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                        "source_git_ref": "abc123",
                    },
                },
            )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_generic_web_prod_promotion_route_accepts_base_driver_product(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            profile_payload = _product_profile_payload_with_prod()
            profile_payload["driver_id"] = "odoo"
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(profile_payload)
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/odoo-tenant-cm",
                            "workflow_refs": [
                                "cbusillo/odoo-tenant-cm/.github/workflows/promote-prod.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["generic_web_prod_promotion.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/odoo-tenant-cm",
                        workflow_ref=(
                            "cbusillo/odoo-tenant-cm/.github/workflows/promote-prod.yml"
                            "@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.drivers.generic_web_dispatch.execute_generic_web_prod_promotion",
                return_value=GenericWebProdPromotionResult(
                    product="sellyouroutboard",
                    context="sellyouroutboard-testing",
                    from_instance="testing",
                    to_instance="prod",
                    artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                    promotion_record_id="promotion-odoo-testing-to-prod",
                    deployment_record_id="deployment-odoo-prod",
                    inventory_record_id="sellyouroutboard-testing-prod",
                    promotion_status="pass",
                    deployment_status="pass",
                    backup_status="skipped",
                    source_health_status="pass",
                    destination_health_status="pass",
                    target_name="odoo-prod-app",
                    target_id="app-odoo",
                    target_category="application",
                    provider_id="dokploy",
                    provider_target_type="application",
                ),
            ) as execute_mock:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/prod-promotion",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "promotion": {
                            "schema_version": 1,
                            "product": "sellyouroutboard",
                            "artifact_id": "ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                            "source_git_ref": "abc123",
                        },
                    },
                    headers={"Idempotency-Key": "generic-web-prod-promotion-odoo"},
                )

        self.assertEqual(status_code, 202)
        self.assertEqual(
            payload["records"]["promotion_record_id"], "promotion-odoo-testing-to-prod"
        )
        self.assertEqual(payload["records"]["deployment_record_id"], "deployment-odoo-prod")
        self.assertEqual(payload["result"]["target_category"], "application")
        self.assertEqual(payload["result"]["provider_id"], "dokploy")
        self.assertEqual(payload["result"]["provider_target_type"], "application")
        self.assertNotIn("target_type", payload["result"])
        execute_mock.assert_called_once()

    def test_generic_web_prod_promotion_route_accepts_padded_lane_context(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            profile_payload = _product_profile_payload_with_prod()
            profile_payload["lanes"] = tuple(
                {**lane, "context": f"  {lane['context']}  "}
                for lane in _product_profile_lanes(profile_payload)
            )
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(profile_payload)
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/promote-prod.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["generic_web_prod_promotion.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/promote-prod.yml"
                            "@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.drivers.generic_web_dispatch.execute_generic_web_prod_promotion",
                return_value=GenericWebProdPromotionResult(
                    product="sellyouroutboard",
                    context="sellyouroutboard-testing",
                    from_instance="testing",
                    to_instance="prod",
                    artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                    promotion_record_id="promotion-syo-testing-to-prod",
                    deployment_record_id="deployment-syo-prod",
                    inventory_record_id="sellyouroutboard-testing-prod",
                    promotion_status="pass",
                    deployment_status="pass",
                    backup_status="skipped",
                    source_health_status="pass",
                    destination_health_status="pass",
                    target_name="syo-prod-app",
                    target_id="app-123",
                    target_category="application",
                    provider_id="dokploy",
                    provider_target_type="application",
                ),
            ) as execute_mock:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/prod-promotion",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "promotion": {
                            "schema_version": 1,
                            "product": "sellyouroutboard",
                            "artifact_id": "ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                            "source_git_ref": "abc123",
                        },
                    },
                    headers={"Idempotency-Key": "generic-web-prod-promotion-syo-padded"},
                )

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(payload["records"]["promotion_record_id"], "promotion-syo-testing-to-prod")
        self.assertEqual(payload["result"]["target_category"], "application")
        self.assertEqual(payload["result"]["provider_id"], "dokploy")
        self.assertEqual(payload["result"]["provider_target_type"], "application")
        self.assertNotIn("target_type", payload["result"])
        execute_mock.assert_called_once()

    def test_human_session_can_dry_run_generic_web_prod_promotion(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            session_store = InMemoryHumanSessionStore()
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload_with_prod())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_humans": [
                        {
                            "logins": ["alice"],
                            "roles": ["admin"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["generic_web_prod_promotion.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
                github_oauth_config=_github_oauth_config(),
                human_session_store=session_store,
            )
            cookie = _signed_in_cookie(session_store, role="admin")

            with patch(
                "control_plane.drivers.generic_web_dispatch.execute_generic_web_prod_promotion",
                return_value=GenericWebProdPromotionResult(
                    product="sellyouroutboard",
                    context="sellyouroutboard-testing",
                    from_instance="testing",
                    to_instance="prod",
                    artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                    promotion_record_id="promotion-syo-testing-to-prod",
                    deployment_record_id="",
                    inventory_record_id="",
                    promotion_status="pending",
                    deployment_status="skipped",
                    backup_status="skipped",
                    source_health_status="pending",
                    destination_health_status="pending",
                    dry_run=True,
                ),
            ) as execute_mock:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/prod-promotion",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "promotion": {
                            "schema_version": 1,
                            "product": "sellyouroutboard",
                            "artifact_id": "ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                            "source_git_ref": "abc123",
                            "dry_run": True,
                        },
                    },
                    authorization="",
                    headers={"Cookie": cookie},
                )

        self.assertEqual(status_code, 202)
        self.assertTrue(payload["result"]["dry_run"])
        self.assertEqual(payload["result"]["deployment_status"], "skipped")
        self.assertEqual(payload["records"]["deployment_record_id"], "")
        self.assertEqual(payload["records"]["inventory_record_id"], "")
        execute_mock.assert_called_once()

    def test_human_session_cannot_live_execute_generic_web_prod_promotion(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            session_store = InMemoryHumanSessionStore()
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload_with_prod())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_humans": [
                        {
                            "logins": ["alice"],
                            "roles": ["admin"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["generic_web_prod_promotion.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
                github_oauth_config=_github_oauth_config(),
                human_session_store=session_store,
            )
            cookie = _signed_in_cookie(session_store, role="admin")

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/prod-promotion",
                payload={
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "promotion": {
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "artifact_id": "ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                        "source_git_ref": "abc123",
                        "dry_run": False,
                    },
                },
                authorization="",
                headers={"Cookie": cookie},
            )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_human_session_cannot_replay_live_generic_web_prod_promotion(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            session_store = InMemoryHumanSessionStore()
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload_with_prod())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_humans": [
                        {
                            "logins": ["alice"],
                            "roles": ["admin"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["generic_web_prod_promotion.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
                github_oauth_config=_github_oauth_config(),
                human_session_store=session_store,
            )
            request_payload = {
                "schema_version": 1,
                "product": "sellyouroutboard",
                "promotion": {
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "artifact_id": "ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
                    "source_git_ref": "abc123",
                    "dry_run": False,
                },
            }
            idempotency_key = "generic-web-prod-promotion-human-live"
            store.write_idempotency_record(
                LaunchplaneIdempotencyRecord(
                    record_id="idempotency-generic-web-prod-promotion-human-live",
                    scope="|".join(("github-human", "alice", "123")),
                    route_path="/v1/drivers/generic-web/prod-promotion",
                    idempotency_key=idempotency_key,
                    request_fingerprint=control_plane_service._idempotency_request_fingerprint(
                        route_path="/v1/drivers/generic-web/prod-promotion",
                        payload=request_payload,
                    ),
                    response_status_code=202,
                    response_trace_id="generic-web-prod-promotion-human-live",
                    recorded_at="2026-06-05T22:00:00Z",
                    response_payload={
                        "status": "accepted",
                        "trace_id": "generic-web-prod-promotion-human-live",
                        "records": {"promotion_record_id": "promotion-live"},
                        "result": {"promotion_status": "pass", "dry_run": False},
                    },
                )
            )
            cookie = _signed_in_cookie(session_store, role="admin")

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/prod-promotion",
                payload=request_payload,
                authorization="",
                headers={"Cookie": cookie, "Idempotency-Key": idempotency_key},
            )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_human_session_can_dispatch_generic_web_promotion_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            session_store = InMemoryHumanSessionStore()
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload_with_prod())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_humans": [
                        {
                            "logins": ["alice"],
                            "roles": ["admin"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["generic_web_prod_promotion.dispatch"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
                github_oauth_config=_github_oauth_config(),
                human_session_store=session_store,
            )
            cookie = _signed_in_cookie(session_store, role="admin")

            with patch(
                "control_plane.drivers.generic_web_dispatch.dispatch_generic_web_promotion_workflow",
                return_value=GenericWebPromotionWorkflowResult(
                    product="sellyouroutboard",
                    context="sellyouroutboard-testing",
                    repository="cbusillo/sellyouroutboard",
                    workflow_id="promote-prod.yml",
                    ref="main",
                    dry_run=False,
                    bump="patch",
                    run_id=25237186636,
                    run_url="https://github.com/cbusillo/sellyouroutboard/actions/runs/25237186636",
                    run_status="queued",
                ),
            ) as dispatch_mock:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/prod-promotion-workflow",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "workflow": {
                            "schema_version": 1,
                            "product": "sellyouroutboard",
                            "context": "sellyouroutboard-testing",
                            "dry_run": False,
                            "bump": "patch",
                            "observe_timeout_seconds": 0,
                        },
                    },
                    authorization="",
                    headers={"Cookie": cookie},
                )

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["result"]["repository"], "cbusillo/sellyouroutboard")
        self.assertEqual(payload["result"]["workflow_id"], "promote-prod.yml")
        self.assertFalse(payload["result"]["dry_run"])
        self.assertEqual(payload["result"]["run_id"], 25237186636)
        self.assertEqual(payload["records"], {})
        dispatch_mock.assert_called_once()

    def test_human_session_dispatches_generic_web_promotion_workflow_with_padded_lane_context(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            session_store = InMemoryHumanSessionStore()
            profile_payload = _product_profile_payload_with_prod()
            profile_payload["lanes"] = tuple(
                {**lane, "context": f"  {lane['context']}  "}
                for lane in _product_profile_lanes(profile_payload)
            )
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(profile_payload)
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_humans": [
                        {
                            "logins": ["alice"],
                            "roles": ["admin"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["generic_web_prod_promotion.dispatch"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
                github_oauth_config=_github_oauth_config(),
                human_session_store=session_store,
            )
            cookie = _signed_in_cookie(session_store, role="admin")

            with patch(
                "control_plane.drivers.generic_web_dispatch.dispatch_generic_web_promotion_workflow",
                return_value=GenericWebPromotionWorkflowResult(
                    product="sellyouroutboard",
                    context="sellyouroutboard-testing",
                    repository="cbusillo/sellyouroutboard",
                    workflow_id="promote-prod.yml",
                    ref="main",
                    dry_run=False,
                    bump="patch",
                    run_id=25237186636,
                    run_url="https://github.com/cbusillo/sellyouroutboard/actions/runs/25237186636",
                    run_status="queued",
                ),
            ) as dispatch_mock:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/prod-promotion-workflow",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "workflow": {
                            "schema_version": 1,
                            "product": "sellyouroutboard",
                            "context": "sellyouroutboard-testing",
                            "dry_run": False,
                            "bump": "patch",
                            "observe_timeout_seconds": 0,
                        },
                    },
                    authorization="",
                    headers={"Cookie": cookie},
                )

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["result"]["run_id"], 25237186636)
        dispatch_mock.assert_called_once()

    def test_generic_web_promotion_workflow_rejects_unauthorized_human(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            session_store = InMemoryHumanSessionStore()
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload_with_prod())
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_humans": []}),
                control_plane_root_path=root,
                github_oauth_config=_github_oauth_config(),
                human_session_store=session_store,
            )
            cookie = _signed_in_cookie(session_store, role="admin")

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/prod-promotion-workflow",
                payload={
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "workflow": {
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "context": "sellyouroutboard-testing",
                        "dry_run": False,
                    },
                },
                authorization="",
                headers={"Cookie": cookie},
            )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_generic_web_promotion_workflow_accepts_base_driver_product(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            profile_payload = _product_profile_payload_with_prod()
            profile_payload["driver_id"] = "odoo"
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(profile_payload)
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/odoo-tenant-cm",
                            "workflow_refs": [
                                "cbusillo/odoo-tenant-cm/.github/workflows/promote-prod.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["generic_web_prod_promotion.dispatch"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/odoo-tenant-cm",
                        workflow_ref=(
                            "cbusillo/odoo-tenant-cm/.github/workflows/promote-prod.yml"
                            "@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.drivers.generic_web_dispatch.dispatch_generic_web_promotion_workflow",
                return_value=GenericWebPromotionWorkflowResult(
                    product="sellyouroutboard",
                    context="sellyouroutboard-testing",
                    repository="cbusillo/sellyouroutboard",
                    workflow_id="promote-prod.yml",
                    ref="main",
                    dry_run=False,
                    bump="patch",
                    run_id=25237186636,
                    run_url="https://github.com/cbusillo/sellyouroutboard/actions/runs/25237186636",
                    run_status="queued",
                ),
            ) as dispatch_mock:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/prod-promotion-workflow",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "workflow": {
                            "schema_version": 1,
                            "product": "sellyouroutboard",
                            "context": "sellyouroutboard-testing",
                            "dry_run": False,
                        },
                    },
                )

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["result"]["run_id"], 25237186636)
        dispatch_mock.assert_called_once()
        _, kwargs = dispatch_mock.call_args
        self.assertEqual(kwargs["profile"].driver_id, "odoo")

    def test_generic_web_promotion_workflow_rejects_unowned_context_before_authz(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            session_store = InMemoryHumanSessionStore()
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload_with_prod())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_humans": [
                        {
                            "logins": ["alice"],
                            "roles": ["admin"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["unowned-context"],
                            "actions": ["generic_web_prod_promotion.dispatch"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
                github_oauth_config=_github_oauth_config(),
                human_session_store=session_store,
            )
            cookie = _signed_in_cookie(session_store, role="admin")

            with patch(
                "control_plane.drivers.generic_web_dispatch.dispatch_generic_web_promotion_workflow"
            ) as dispatch_mock:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/prod-promotion-workflow",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "workflow": {
                            "schema_version": 1,
                            "product": "sellyouroutboard",
                            "context": "unowned-context",
                            "dry_run": False,
                        },
                    },
                    authorization="",
                    headers={"Cookie": cookie},
                )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "product_driver_mismatch")
        dispatch_mock.assert_not_called()

    def test_generic_web_promotion_workflow_rejects_token_unowned_context_before_authz(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload_with_prod())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/promote-prod.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["unowned-context"],
                            "actions": ["generic_web_prod_promotion.dispatch"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/promote-prod.yml"
                            "@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.drivers.generic_web_dispatch.dispatch_generic_web_promotion_workflow"
            ) as dispatch_mock:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/prod-promotion-workflow",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "workflow": {
                            "schema_version": 1,
                            "product": "sellyouroutboard",
                            "context": "unowned-context",
                            "dry_run": False,
                        },
                    },
                )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "product_driver_mismatch")
        dispatch_mock.assert_not_called()

    def test_generic_web_preview_desired_state_legacy_wsgi_route_is_retired(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity(repository="cbusillo/sellyouroutboard")),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/preview-desired-state",
                payload={
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "desired_state": {
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                    },
                },
            )

        self.assertEqual(status_code, 404)
        self.assertEqual(payload["error"]["code"], "not_found")

    def test_generic_web_preview_inventory_route_writes_scan_from_driver_result(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["preview_inventory.read"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml"
                            "@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )
            driver_result = SimpleNamespace(
                context="sellyouroutboard-testing",
                source="generic-web-preview-inventory",
                previews=(SimpleNamespace(previewSlug="pr-42"),),
            )

            with patch(
                "control_plane.drivers.generic_web_preview_dispatch.execute_generic_web_preview_inventory",
                return_value=driver_result,
            ):
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/preview-inventory",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "inventory": {
                            "schema_version": 1,
                            "product": "sellyouroutboard",
                        },
                    },
                )
            records = FilesystemRecordStore(
                state_dir=state_dir
            ).list_preview_inventory_scan_records(context_name="sellyouroutboard-testing")

        self.assertEqual(status_code, 202)
        self.assertEqual(
            payload["records"]["preview_inventory_scan_id"],
            records[0].scan_id,
        )
        self.assertEqual(records[0].source, "generic-web-preview-inventory")
        self.assertEqual(records[0].preview_slugs, ("pr-42",))

    def test_generic_web_preview_refresh_route_returns_driver_result(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["preview_refresh.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml"
                            "@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.drivers.generic_web_preview_dispatch.execute_generic_web_preview_refresh",
                return_value={
                    "refresh_status": "pass",
                    "refresh_started_at": "2026-05-03T15:00:00Z",
                    "refresh_finished_at": "2026-05-03T15:05:00Z",
                    "product": "sellyouroutboard",
                    "context": "sellyouroutboard-testing",
                    "preview_slug": "pr-42",
                    "application_name": "sellyouroutboard-pr-42",
                    "application_id": "app-preview",
                    "preview_url": "https://pr-42.example.test",
                },
            ) as refresh:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/preview-refresh",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "refresh": {
                            "schema_version": 1,
                            "product": "sellyouroutboard",
                            "preview_slug": "pr-42",
                            "preview_url": "https://pr-42.example.test",
                            "image_reference": "ghcr.io/cbusillo/sellyouroutboard:sha",
                        },
                    },
                    headers={"Idempotency-Key": "generic-web-preview-refresh:syo:pr-42"},
                )

                self.assertEqual(status_code, 202)
                self.assertEqual(payload["records"]["transition"], "verifying")
                self.assertEqual(payload["result"]["refresh_status"], "pass")
                self.assertEqual(payload["result"]["application_id"], "app-preview")
                store = FilesystemRecordStore(state_dir=state_dir)
                preview = store.read_preview_record(
                    "preview-sellyouroutboard-testing-sellyouroutboard-pr-42"
                )
                generation = store.read_preview_generation_record(
                    "preview-sellyouroutboard-testing-sellyouroutboard-pr-42-generation-0001"
                )
                self.assertEqual(preview.state, "pending")
                self.assertEqual(generation.state, "verifying")
                self.assertEqual(generation.deploy_status, "pass")
                self.assertEqual(generation.verify_status, "pending")
                refresh.assert_called_once()
                _, kwargs = refresh.call_args
                self.assertEqual(kwargs["profile"].product, "sellyouroutboard")
                self.assertEqual(kwargs["request"].preview_url, "https://pr-42.example.test")

    def test_generic_web_preview_refresh_mutation_builder_records_smoke_failure(
        self,
    ) -> None:
        profile = LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
        driver_result = GenericWebPreviewRefreshResult.model_validate(
            {
                "refresh_status": "fail",
                "refresh_started_at": "2026-05-03T15:00:00Z",
                "refresh_finished_at": "2026-05-03T15:05:00Z",
                "product": "sellyouroutboard",
                "context": "sellyouroutboard-testing",
                "preview_slug": "pr-42",
                "application_name": "sellyouroutboard-pr-42",
                "application_id": "app-preview",
                "preview_url": "https://pr-42.example.test",
                "smoke": {
                    "smoke_status": "fail",
                    "checked_at": "2026-05-03T15:04:55Z",
                    "checks": [
                        {
                            "check_id": "health",
                            "status": "fail",
                            "message": "Health check failed.",
                        }
                    ],
                    "failure_summary": "Smoke failed on /api/health.",
                },
            }
        )
        preview_request, generation_request = (
            control_plane_service._generic_web_preview_refresh_mutation_requests(
                request=GenericWebPreviewRefreshRequest.model_validate(
                    {
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "preview_slug": "pr-42",
                        "preview_url": "https://pr-42.request.example.test",
                        "image_reference": "ghcr.io/cbusillo/sellyouroutboard:sha",
                    }
                ),
                driver_result=driver_result,
                profile=profile,
            )
        )

        self.assertEqual(preview_request.state, "failed")
        self.assertEqual(preview_request.canonical_url, "https://pr-42.example.test")
        self.assertEqual(generation_request.state, "failed")
        self.assertEqual(generation_request.deploy_status, "pass")
        self.assertEqual(generation_request.verify_status, "fail")
        self.assertEqual(generation_request.failure_stage, "verify")
        self.assertEqual(generation_request.failure_summary, "Smoke failed on /api/health.")

    def test_generic_web_preview_refresh_route_accepts_omitted_preview_url(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["preview_refresh.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml"
                            "@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.drivers.generic_web_preview_dispatch.execute_generic_web_preview_refresh",
                return_value={
                    "refresh_status": "pass",
                    "refresh_started_at": "2026-05-03T15:00:00Z",
                    "refresh_finished_at": "2026-05-03T15:05:00Z",
                    "product": "sellyouroutboard",
                    "context": "sellyouroutboard-testing",
                    "preview_slug": "pr-42",
                    "application_name": "sellyouroutboard-pr-42",
                    "application_id": "app-preview",
                    "preview_url": "https://pr-42.syo-preview.example.test",
                },
            ) as refresh:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/preview-refresh",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "refresh": {
                            "schema_version": 1,
                            "product": "sellyouroutboard",
                            "preview_slug": "pr-42",
                            "image_reference": "ghcr.io/cbusillo/sellyouroutboard:sha",
                        },
                    },
                    headers={"Idempotency-Key": "generic-web-preview-refresh:syo:pr-42"},
                )

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["result"]["preview_url"], "https://pr-42.syo-preview.example.test")
        refresh.assert_called_once()
        _, kwargs = refresh.call_args
        self.assertEqual(kwargs["request"].preview_url, "")

    def test_generic_web_preview_refresh_route_persists_provider_failure_result(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["preview_refresh.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml"
                            "@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.drivers.generic_web_preview_dispatch.execute_generic_web_preview_refresh",
                return_value={
                    "refresh_status": "fail",
                    "refresh_started_at": "2026-05-03T15:00:00Z",
                    "refresh_finished_at": "2026-05-03T15:05:00Z",
                    "product": "sellyouroutboard",
                    "context": "sellyouroutboard-testing",
                    "preview_slug": "pr-42",
                    "application_name": "sellyouroutboard-pr-42",
                    "application_id": "app-preview",
                    "preview_url": "https://pr-42.example.test",
                    "error_message": "Dokploy API POST /api/application.update failed (500): provider exploded",
                },
            ):
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/preview-refresh",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "refresh": {
                            "schema_version": 1,
                            "product": "sellyouroutboard",
                            "preview_slug": "pr-42",
                            "preview_url": "https://pr-42.example.test",
                            "image_reference": "ghcr.io/cbusillo/sellyouroutboard:sha",
                        },
                    },
                    headers={"Idempotency-Key": "generic-web-preview-refresh:syo:pr-42"},
                )

                self.assertEqual(status_code, 202)
                self.assertEqual(payload["records"]["transition"], "failed")
                self.assertEqual(payload["result"]["refresh_status"], "fail")
                store = FilesystemRecordStore(state_dir=state_dir)
                preview = store.read_preview_record(
                    "preview-sellyouroutboard-testing-sellyouroutboard-pr-42"
                )
                generation = store.read_preview_generation_record(
                    "preview-sellyouroutboard-testing-sellyouroutboard-pr-42-generation-0001"
                )
                self.assertEqual(preview.state, "failed")
                self.assertEqual(generation.state, "failed")
                self.assertEqual(generation.deploy_status, "fail")
                self.assertEqual(generation.verify_status, "skipped")
                self.assertEqual(generation.failure_stage, "provision")
                self.assertEqual(
                    generation.failure_summary,
                    "Dokploy API POST /api/application.update failed (500): provider exploded",
                )

    def test_odoo_preview_refresh_route_is_not_supported(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            FilesystemRecordStore(state_dir=state_dir).write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_odoo_preview_profile_payload())
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/odoo-tenant-cm",
                        workflow_ref=(
                            "cbusillo/odoo-tenant-cm/.github/workflows/preview.yml@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=LaunchplaneAuthzPolicy.model_validate(
                    {
                        "github_actions": [
                            {
                                "repository": "cbusillo/odoo-tenant-cm",
                                "workflow_refs": [
                                    "cbusillo/odoo-tenant-cm/.github/workflows/preview.yml@refs/heads/main"
                                ],
                                "event_names": ["pull_request"],
                                "products": ["odoo-tenant-cm"],
                                "contexts": ["cm"],
                                "actions": ["preview_refresh.execute"],
                            }
                        ]
                    }
                ),
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/odoo/preview-refresh",
                payload={
                    "schema_version": 1,
                    "product": "odoo-tenant-cm",
                    "refresh": {
                        "schema_version": 1,
                        "product": "odoo-tenant-cm",
                        "preview_slug": "pr-42",
                        "image_reference": "ghcr.io/cbusillo/odoo-tenant-cm:sha",
                    },
                },
                headers={"Idempotency-Key": "odoo-preview-refresh:cm:42:sha"},
            )

            self.assertEqual(status_code, 404)
            self.assertEqual(payload["error"]["code"], "not_found")

    def test_odoo_preview_read_planning_alias_routes_are_retired(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=root,
            )

            for route_path in (
                "/v1/drivers/odoo/preview-desired-state",
                "/v1/drivers/odoo/preview-inventory",
                "/v1/drivers/odoo/preview-readiness",
                "/v1/drivers/odoo/preview-destroy",
            ):
                with self.subTest(route_path=route_path):
                    status_code, payload = _invoke_app(
                        app,
                        method="POST",
                        path=route_path,
                        payload={},
                    )

                    self.assertEqual(status_code, 404)
                    self.assertEqual(payload["error"]["code"], "not_found")

    def test_odoo_preview_verification_route_is_retired(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/odoo/preview-verification",
                payload={
                    "schema_version": 1,
                    "product": "odoo-tenant-cm",
                    "verification": {
                        "schema_version": 1,
                        "context": "cm",
                        "anchor_repo": "odoo-tenant-cm",
                        "anchor_pr_number": 42,
                        "verification_status": "pass",
                        "verified_at": "2026-05-09T15:08:00Z",
                        "checked_urls": [
                            "https://pr-42.cm-preview.example.test/web/health",
                            "https://pr-42.cm-preview.example.test/cell-mechanic",
                        ],
                        "timeout_seconds": 30,
                    },
                },
                headers={"Idempotency-Key": "odoo-preview-verification:cm:42:run-1"},
            )

            self.assertEqual(status_code, 404)
            self.assertEqual(payload["error"]["code"], "not_found")

    def test_generic_web_preview_verification_route_accepts_odoo_base_driver_profile(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_odoo_preview_profile_payload())
            )
            store.write_preview_record(
                PreviewRecord(
                    preview_id="preview-cm-odoo-tenant-cm-pr-42",
                    context="cm",
                    anchor_repo="odoo-tenant-cm",
                    anchor_pr_number=42,
                    anchor_pr_url="https://github.com/cbusillo/odoo-tenant-cm/pull/42",
                    preview_label="preview",
                    canonical_url="https://pr-42.cm-preview.example.test",
                    state="pending",
                    created_at="2026-05-09T15:00:00Z",
                    updated_at="2026-05-09T15:05:00Z",
                    eligible_at="2026-05-09T15:00:00Z",
                    active_generation_id="preview-cm-odoo-tenant-cm-pr-42-generation-0001",
                    latest_generation_id="preview-cm-odoo-tenant-cm-pr-42-generation-0001",
                    latest_manifest_fingerprint="odoo-preview-manifest-pr-42-abc123",
                )
            )
            store.write_preview_generation_record(
                PreviewGenerationRecord(
                    generation_id="preview-cm-odoo-tenant-cm-pr-42-generation-0001",
                    preview_id="preview-cm-odoo-tenant-cm-pr-42",
                    sequence=1,
                    state="verifying",
                    requested_reason="external_preview_refresh",
                    requested_at="2026-05-09T15:00:00Z",
                    started_at="2026-05-09T15:00:00Z",
                    resolved_manifest_fingerprint="odoo-preview-manifest-pr-42-abc123",
                    artifact_id="ghcr.io/cbusillo/odoo-tenant-cm:sha",
                    anchor_summary=PreviewPullRequestSummary(
                        repo="odoo-tenant-cm",
                        pr_number=42,
                        head_sha="abc123",
                        pr_url="https://github.com/cbusillo/odoo-tenant-cm/pull/42",
                    ),
                    deploy_status="pass",
                    verify_status="pending",
                    overall_health_status="pending",
                )
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/odoo-tenant-cm",
                            "workflow_refs": [
                                "cbusillo/odoo-tenant-cm/.github/workflows/preview.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["odoo-tenant-cm"],
                            "contexts": ["cm"],
                            "actions": ["preview_generation.write"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/odoo-tenant-cm",
                        workflow_ref=(
                            "cbusillo/odoo-tenant-cm/.github/workflows/preview.yml@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/preview-verification",
                payload={
                    "schema_version": 1,
                    "product": "odoo-tenant-cm",
                    "verification": {
                        "schema_version": 1,
                        "context": "cm",
                        "anchor_repo": "odoo-tenant-cm",
                        "anchor_pr_number": 42,
                        "verification_status": "pass",
                        "verified_at": "2026-05-09T15:08:00Z",
                        "checked_urls": ["https://pr-42.cm-preview.example.test/web/health"],
                        "timeout_seconds": 30,
                    },
                },
                headers={"Idempotency-Key": "generic-preview-verification:cm:42:run-1"},
            )

            self.assertEqual(status_code, 202)
            self.assertEqual(payload["records"]["transition"], "ready")
            self.assertEqual(payload["records"]["preview_state"], "active")
            self.assertEqual(payload["records"]["verification_status"], "pass")
            self.assertEqual(
                payload["records"]["generic_web_preview_verification"]["checked_urls"],
                ["https://pr-42.cm-preview.example.test/web/health"],
            )
            preview = store.read_preview_record("preview-cm-odoo-tenant-cm-pr-42")
            generation = store.read_preview_generation_record(
                "preview-cm-odoo-tenant-cm-pr-42-generation-0001"
            )
            self.assertEqual(preview.state, "active")
            self.assertEqual(preview.serving_generation_id, generation.generation_id)
            self.assertEqual(generation.state, "ready")
            self.assertEqual(generation.verify_status, "pass")
            self.assertEqual(generation.overall_health_status, "pass")

    def test_generic_web_preview_verification_route_does_not_require_lane(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            profile_payload = _product_profile_payload()
            profile_payload["lanes"] = ()
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(profile_payload)
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["preview_generation.write"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml"
                            "@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.drivers.generic_web_preview_dispatch._apply_generic_web_preview_verification_records",
                return_value={
                    "transition": "ready",
                    "preview_state": "active",
                    "verification_status": "pass",
                    "generic_web_preview_verification": {"checked_urls": ()},
                },
            ) as apply_records:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/preview-verification",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "verification": {
                            "schema_version": 1,
                            "context": "sellyouroutboard-testing",
                            "anchor_repo": "sellyouroutboard",
                            "anchor_pr_number": 42,
                            "verification_status": "pass",
                            "verified_at": "2026-05-09T15:08:00Z",
                        },
                    },
                    headers={"Idempotency-Key": "generic-preview-verification:syo:42:no-lane"},
                )

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["records"]["transition"], "ready")
        apply_records.assert_called_once()
        self.assertEqual(apply_records.call_args.kwargs["control_plane_root_path"], root)

    def test_generic_web_preview_verification_route_rejects_unauthorized_context(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["other-context"],
                            "actions": ["preview_generation.write"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml"
                            "@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.drivers.generic_web_preview_dispatch._apply_generic_web_preview_verification_records",
                return_value={"transition": "ready"},
            ) as apply_records:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/preview-verification",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "verification": {
                            "schema_version": 1,
                            "context": "sellyouroutboard-testing",
                            "anchor_repo": "sellyouroutboard",
                            "anchor_pr_number": 42,
                            "verification_status": "pass",
                            "verified_at": "2026-05-09T15:08:00Z",
                        },
                    },
                    headers={"Idempotency-Key": "generic-preview-verification:syo:42:denied"},
                )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")
        apply_records.assert_not_called()

    def test_generic_web_preview_verification_replay_revalidates_preview_profile(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            profile_payload = _product_profile_payload()
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(profile_payload)
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["preview_generation.write"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml"
                            "@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )
            request_payload = {
                "schema_version": 1,
                "product": "sellyouroutboard",
                "verification": {
                    "schema_version": 1,
                    "context": "sellyouroutboard-testing",
                    "anchor_repo": "sellyouroutboard",
                    "anchor_pr_number": 42,
                    "verification_status": "pass",
                    "verified_at": "2026-05-09T15:08:00Z",
                },
            }

            with patch(
                "control_plane.drivers.generic_web_preview_dispatch._apply_generic_web_preview_verification_records",
                return_value={"transition": "ready"},
            ) as apply_records:
                first_status_code, first_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/preview-verification",
                    payload=request_payload,
                    headers={"Idempotency-Key": "generic-preview-verification:syo:42:replay"},
                )
                disabled_payload = dict(profile_payload)
                disabled_payload["preview"] = {"enabled": False}
                store.write_product_profile_record(
                    LaunchplaneProductProfileRecord.model_validate(disabled_payload)
                )
                second_status_code, second_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/preview-verification",
                    payload=request_payload,
                    headers={"Idempotency-Key": "generic-preview-verification:syo:42:replay"},
                )

        self.assertEqual(first_status_code, 202)
        self.assertEqual(first_payload["records"]["transition"], "ready")
        self.assertEqual(second_status_code, 400)
        self.assertEqual(second_payload["error"]["code"], "invalid_request")
        apply_records.assert_called_once()

    def test_generic_web_preview_verification_request_accepts_explicit_url_collections(
        self,
    ) -> None:
        base_payload = {
            "schema_version": 1,
            "context": "cm",
            "anchor_repo": "odoo-tenant-cm",
            "anchor_pr_number": 42,
            "verification_status": "pass",
            "verified_at": "2026-05-09T15:08:00Z",
            "timeout_seconds": 30,
        }

        list_request = GenericWebPreviewVerificationRequest.model_validate(
            {
                **base_payload,
                "checked_urls": [" https://pr-42.cm-preview.example.test/web/health "],
            }
        )
        tuple_request = GenericWebPreviewVerificationRequest.model_validate(
            {
                **base_payload,
                "checked_urls": ("https://pr-42.cm-preview.example.test/cell-mechanic",),
            }
        )

        self.assertEqual(
            list_request.checked_urls,
            ("https://pr-42.cm-preview.example.test/web/health",),
        )
        self.assertEqual(
            tuple_request.checked_urls,
            ("https://pr-42.cm-preview.example.test/cell-mechanic",),
        )

    def test_generic_web_preview_verification_records_reject_missing_preview(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            request = GenericWebPreviewVerificationRequest.model_validate(
                {
                    "schema_version": 1,
                    "context": "cm",
                    "anchor_repo": "odoo-tenant-cm",
                    "anchor_pr_number": 42,
                    "verification_status": "pass",
                    "verified_at": "2026-05-09T15:08:00Z",
                }
            )

            with self.assertRaises(ClickException) as raised:
                generic_web_preview_dispatch._apply_generic_web_preview_verification_records(
                    control_plane_root_path=root,
                    record_store=store,
                    request=request,
                )

        self.assertEqual(
            str(raised.exception),
            "No Launchplane preview found for cm/odoo-tenant-cm/pr-42.",
        )

    def test_generic_web_preview_verification_records_reject_missing_generation(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_preview_record(
                PreviewRecord(
                    preview_id="preview-cm-odoo-tenant-cm-pr-42",
                    context="cm",
                    anchor_repo="odoo-tenant-cm",
                    anchor_pr_number=42,
                    anchor_pr_url="https://github.com/cbusillo/odoo-tenant-cm/pull/42",
                    preview_label="preview",
                    canonical_url="https://pr-42.cm-preview.example.test",
                    state="pending",
                    created_at="2026-05-09T15:00:00Z",
                    updated_at="2026-05-09T15:05:00Z",
                    eligible_at="2026-05-09T15:00:00Z",
                )
            )
            request = GenericWebPreviewVerificationRequest.model_validate(
                {
                    "schema_version": 1,
                    "context": "cm",
                    "anchor_repo": "odoo-tenant-cm",
                    "anchor_pr_number": 42,
                    "verification_status": "pass",
                    "verified_at": "2026-05-09T15:08:00Z",
                }
            )

            with self.assertRaises(ClickException) as raised:
                generic_web_preview_dispatch._apply_generic_web_preview_verification_records(
                    control_plane_root_path=root,
                    record_store=store,
                    request=request,
                )

        self.assertEqual(
            str(raised.exception),
            "No Launchplane preview generation found for preview-cm-odoo-tenant-cm-pr-42.",
        )

    def test_generic_web_preview_refresh_route_keeps_blocked_result_non_mutating(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["preview_refresh.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml"
                            "@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )
            request_payload = {
                "schema_version": 1,
                "product": "sellyouroutboard",
                "refresh": {
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "preview_slug": "pr-42",
                    "preview_url": "https://pr-42.example.test",
                    "image_reference": "ghcr.io/cbusillo/sellyouroutboard:sha",
                },
            }

            with patch(
                "control_plane.drivers.generic_web_preview_dispatch.execute_generic_web_preview_refresh",
                return_value={
                    "refresh_status": "blocked",
                    "refresh_started_at": "2026-05-03T15:00:00Z",
                    "refresh_finished_at": "2026-05-03T15:00:01Z",
                    "product": "sellyouroutboard",
                    "context": "sellyouroutboard-testing",
                    "preview_slug": "pr-42",
                    "application_name": "sellyouroutboard-pr-42",
                    "application_id": "",
                    "preview_url": "https://pr-42.example.test",
                    "error_message": "Generic web preview readiness blocked refresh.",
                },
            ) as refresh:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/preview-refresh",
                    payload=request_payload,
                    headers={"Idempotency-Key": "generic-web-preview-refresh:syo:42:sha"},
                )

                self.assertEqual(status_code, 202)
                self.assertEqual(payload["records"], {})
                self.assertEqual(payload["result"]["refresh_status"], "blocked")
                store = FilesystemRecordStore(state_dir=state_dir)
                self.assertEqual(store.list_preview_records(), ())
                self.assertIsNone(
                    store.read_idempotency_record(
                        scope=(
                            "github-actions:cbusillo/sellyouroutboard:"
                            "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml"
                            "@refs/heads/main"
                        ),
                        route_path="/v1/drivers/generic-web/preview-refresh",
                        idempotency_key="generic-web-preview-refresh:syo:42:sha",
                    )
                )
                refresh.assert_called_once()

    def test_generic_web_preview_refresh_retry_runs_again_after_blocked_result(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["preview_refresh.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml"
                            "@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )
            request_payload = {
                "schema_version": 1,
                "product": "sellyouroutboard",
                "refresh": {
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "preview_slug": "pr-42",
                    "preview_url": "https://pr-42.example.test",
                    "image_reference": "ghcr.io/cbusillo/sellyouroutboard:sha",
                },
            }

            with patch(
                "control_plane.drivers.generic_web_preview_dispatch.execute_generic_web_preview_refresh",
                side_effect=[
                    {
                        "refresh_status": "blocked",
                        "refresh_started_at": "2026-05-03T15:00:00Z",
                        "refresh_finished_at": "2026-05-03T15:00:01Z",
                        "product": "sellyouroutboard",
                        "context": "sellyouroutboard-testing",
                        "preview_slug": "pr-42",
                        "application_name": "sellyouroutboard-pr-42",
                        "application_id": "",
                        "preview_url": "https://pr-42.example.test",
                        "error_message": "Generic web preview readiness blocked refresh.",
                    },
                    {
                        "refresh_status": "pass",
                        "refresh_started_at": "2026-05-03T15:06:00Z",
                        "refresh_finished_at": "2026-05-03T15:10:00Z",
                        "product": "sellyouroutboard",
                        "context": "sellyouroutboard-testing",
                        "preview_slug": "pr-42",
                        "application_name": "sellyouroutboard-pr-42",
                        "application_id": "app-preview",
                        "preview_url": "https://pr-42.example.test",
                    },
                ],
            ) as refresh:
                first_status_code, first_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/preview-refresh",
                    payload=request_payload,
                    headers={"Idempotency-Key": "generic-web-preview-refresh:syo:42:sha"},
                )
                second_status_code, second_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/preview-refresh",
                    payload=request_payload,
                    headers={"Idempotency-Key": "generic-web-preview-refresh:syo:42:sha"},
                )

        self.assertEqual(first_status_code, 202)
        self.assertEqual(first_payload["result"]["refresh_status"], "blocked")
        self.assertEqual(second_status_code, 202)
        self.assertEqual(second_payload["result"]["refresh_status"], "pass")
        self.assertNotIn("replayed", second_payload)
        self.assertEqual(refresh.call_count, 2)

    def test_generic_web_preview_refresh_route_rejects_unparseable_slug_before_provider_mutation(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["preview_refresh.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml"
                            "@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.drivers.generic_web_preview_dispatch.execute_generic_web_preview_refresh"
            ) as refresh:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/preview-refresh",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "refresh": {
                            "schema_version": 1,
                            "product": "sellyouroutboard",
                            "preview_slug": "custom-preview",
                            "preview_url": "https://custom-preview.example.test",
                            "image_reference": "ghcr.io/cbusillo/sellyouroutboard:sha",
                        },
                    },
                    headers={"Idempotency-Key": "generic-web-preview-refresh:syo:custom"},
                )

        self.assertEqual(status_code, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")
        refresh.assert_not_called()

    def test_generic_web_preview_refresh_retry_runs_again_after_failed_result(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["preview_refresh.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml"
                            "@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )
            request_payload = {
                "schema_version": 1,
                "product": "sellyouroutboard",
                "refresh": {
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "preview_slug": "pr-42",
                    "preview_url": "https://pr-42.example.test",
                    "image_reference": "ghcr.io/cbusillo/sellyouroutboard:sha",
                },
            }

            with patch(
                "control_plane.drivers.generic_web_preview_dispatch.execute_generic_web_preview_refresh",
                side_effect=[
                    {
                        "refresh_status": "fail",
                        "refresh_started_at": "2026-05-03T15:00:00Z",
                        "refresh_finished_at": "2026-05-03T15:05:00Z",
                        "product": "sellyouroutboard",
                        "context": "sellyouroutboard-testing",
                        "preview_slug": "pr-42",
                        "application_name": "sellyouroutboard-pr-42",
                        "application_id": "app-preview",
                        "preview_url": "https://pr-42.example.test",
                        "error_message": "provider unavailable",
                    },
                    {
                        "refresh_status": "pass",
                        "refresh_started_at": "2026-05-03T15:06:00Z",
                        "refresh_finished_at": "2026-05-03T15:10:00Z",
                        "product": "sellyouroutboard",
                        "context": "sellyouroutboard-testing",
                        "preview_slug": "pr-42",
                        "application_name": "sellyouroutboard-pr-42",
                        "application_id": "app-preview",
                        "preview_url": "https://pr-42.example.test",
                    },
                ],
            ) as refresh:
                first_status_code, first_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/preview-refresh",
                    payload=request_payload,
                    headers={"Idempotency-Key": "generic-web-preview-refresh:syo:42:sha"},
                )
                second_status_code, second_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/preview-refresh",
                    payload=request_payload,
                    headers={"Idempotency-Key": "generic-web-preview-refresh:syo:42:sha"},
                )

        self.assertEqual(first_status_code, 202)
        self.assertEqual(first_payload["result"]["refresh_status"], "fail")
        self.assertEqual(second_status_code, 202)
        self.assertEqual(second_payload["result"]["refresh_status"], "pass")
        self.assertNotIn("replayed", second_payload)
        self.assertEqual(refresh.call_count, 2)

    def test_generic_web_preview_refresh_rejects_reused_key_for_changed_artifact(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["preview_refresh.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml"
                            "@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )
            first_request_payload = {
                "schema_version": 1,
                "product": "sellyouroutboard",
                "refresh": {
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "preview_slug": "pr-42",
                    "preview_url": "https://pr-42.example.test",
                    "image_reference": "ghcr.io/cbusillo/sellyouroutboard:sha-a",
                },
            }
            second_request_payload = json.loads(json.dumps(first_request_payload))
            second_request_payload["refresh"]["image_reference"] = (
                "ghcr.io/cbusillo/sellyouroutboard:sha-b"
            )

            with patch(
                "control_plane.drivers.generic_web_preview_dispatch.execute_generic_web_preview_refresh",
                return_value={
                    "refresh_status": "pass",
                    "refresh_started_at": "2026-05-03T15:00:00Z",
                    "refresh_finished_at": "2026-05-03T15:05:00Z",
                    "product": "sellyouroutboard",
                    "context": "sellyouroutboard-testing",
                    "preview_slug": "pr-42",
                    "application_name": "sellyouroutboard-pr-42",
                    "application_id": "app-preview",
                    "preview_url": "https://pr-42.example.test",
                },
            ) as refresh:
                first_status_code, _ = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/preview-refresh",
                    payload=first_request_payload,
                    headers={"Idempotency-Key": "generic-web-preview-refresh:syo:42:sha-a"},
                )
                second_status_code, second_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/preview-refresh",
                    payload=second_request_payload,
                    headers={"Idempotency-Key": "generic-web-preview-refresh:syo:42:sha-a"},
                )

        self.assertEqual(first_status_code, 202)
        self.assertEqual(second_status_code, 409)
        self.assertEqual(second_payload["error"]["code"], "idempotency_key_reused")
        refresh.assert_called_once()

    def test_generic_web_preview_readiness_route_returns_driver_result(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["preview_readiness.evaluate"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml"
                            "@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.drivers.generic_web_preview_dispatch.evaluate_generic_web_preview_readiness",
                return_value={
                    "readiness_status": "blocked",
                    "missing_template_env_keys": ["SMTP_HOST"],
                },
            ) as readiness:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/preview-readiness",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "readiness": {
                            "schema_version": 1,
                            "product": "sellyouroutboard",
                        },
                    },
                )

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["result"]["readiness_status"], "blocked")
        self.assertEqual(payload["result"]["missing_template_env_keys"], ["SMTP_HOST"])
        readiness.assert_called_once()
        _, kwargs = readiness.call_args
        self.assertEqual(kwargs["profile"].product, "sellyouroutboard")

    def test_generic_web_preview_destroy_route_returns_driver_result(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["preview_destroy.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml"
                            "@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.drivers.generic_web_preview_dispatch.execute_generic_web_preview_destroy",
                return_value=GenericWebPreviewDestroyResult(
                    destroy_status="pass",
                    destroy_started_at="2026-05-03T16:00:00Z",
                    destroy_finished_at="2026-05-03T16:00:02Z",
                    product="sellyouroutboard",
                    context="sellyouroutboard-testing",
                    preview_slug="pr-42",
                    application_name="sellyouroutboard-pr-42",
                    application_id="app-preview",
                ),
            ) as destroy:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/preview-destroy",
                    payload={
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "destroy": {
                            "schema_version": 1,
                            "product": "sellyouroutboard",
                            "preview_slug": "pr-42",
                            "destroy_reason": "external_preview_pull_request_closed",
                        },
                    },
                    headers={"Idempotency-Key": "generic-web-preview-destroy:syo:pr-42"},
                )

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["result"]["destroy_status"], "pass")
        self.assertEqual(payload["result"]["application_id"], "app-preview")
        destroy.assert_called_once()
        _, kwargs = destroy.call_args
        self.assertEqual(kwargs["profile"].product, "sellyouroutboard")
        self.assertEqual(kwargs["profile"].preview.context, "sellyouroutboard-testing")
        self.assertEqual(kwargs["request"].preview_slug, "pr-42")

    def test_generic_web_preview_destroy_replays_when_only_reason_changes(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["preview_destroy.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/preview-control-plane.yml"
                            "@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )
            first_request_payload = {
                "schema_version": 1,
                "product": "sellyouroutboard",
                "destroy": {
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "preview_slug": "pr-42",
                    "destroy_reason": "external_preview_pull_request_closed",
                },
            }
            second_request_payload = json.loads(json.dumps(first_request_payload))
            second_request_payload["destroy"]["destroy_reason"] = "janitor_backstop"

            with patch(
                "control_plane.drivers.generic_web_preview_dispatch.execute_generic_web_preview_destroy",
                return_value=GenericWebPreviewDestroyResult(
                    destroy_status="pass",
                    destroy_started_at="2026-05-03T16:00:00Z",
                    destroy_finished_at="2026-05-03T16:00:02Z",
                    product="sellyouroutboard",
                    context="sellyouroutboard-testing",
                    preview_slug="pr-42",
                    application_name="sellyouroutboard-pr-42",
                    application_id="app-preview",
                ),
            ) as destroy:
                first_status_code, first_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/preview-destroy",
                    payload=first_request_payload,
                    headers={"Idempotency-Key": "generic-web-preview-destroy:syo:42"},
                )
                second_status_code, second_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/generic-web/preview-destroy",
                    payload=second_request_payload,
                    headers={"Idempotency-Key": "generic-web-preview-destroy:syo:42"},
                )

        self.assertEqual(first_status_code, 202)
        self.assertEqual(second_status_code, 202)
        self.assertEqual(first_payload["result"], second_payload["result"])
        self.assertTrue(second_payload["replayed"])
        destroy.assert_called_once()

    def test_data_freshness_report_covers_visible_surfaces(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            for instance_name in ("prod", "testing"):
                store.write_environment_inventory(
                    EnvironmentInventory(
                        context="verireel",
                        instance=instance_name,
                        artifact_identity=ArtifactIdentityReference(
                            artifact_id=f"artifact-verireel-{instance_name}"
                        ),
                        source_git_ref="6b3c9d7e8f901234567890abcdef1234567890ab",
                        deploy=DeploymentEvidence(
                            target_name=f"verireel-{instance_name}",
                            target_type="application",
                            deploy_mode="runtime-provider-api",
                            deployment_id=f"provider-{instance_name}",
                            status="pass",
                            started_at="2026-04-20T15:30:00Z",
                            finished_at="2026-04-20T15:32:00Z",
                        ),
                        updated_at="2026-04-20T15:33:00Z",
                        deployment_record_id=f"deployment-verireel-{instance_name}",
                    )
                )
            store.write_preview_record(
                PreviewRecord(
                    preview_id="preview-verireel-testing-verireel-pr-123",
                    context="verireel-testing",
                    anchor_repo="verireel",
                    anchor_pr_number=123,
                    anchor_pr_url="https://github.com/every/verireel/pull/123",
                    preview_label="verireel/pr-123",
                    canonical_url="https://pr-123.ver-preview.shinycomputers.com",
                    state="active",
                    created_at="2026-04-20T10:00:00Z",
                    updated_at="2026-04-20T10:05:00Z",
                    eligible_at="2026-04-20T10:05:00Z",
                )
            )
            store.write_preview_generation_record(
                PreviewGenerationRecord(
                    generation_id="preview-verireel-testing-verireel-pr-123-generation-0001",
                    preview_id="preview-verireel-testing-verireel-pr-123",
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
            )

            result = runner.invoke(
                CLI_MAIN,
                [
                    "service",
                    "inspect-data-freshness",
                    "--local-inspection",
                    "--state-dir",
                    str(state_dir),
                    "--context",
                    "verireel",
                    "--preview-context",
                    "verireel-testing",
                ],
            )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["surface_count"], 3)
        self.assertEqual(payload["missing_provenance_count"], 0)
        self.assertEqual(
            {surface["name"] for surface in payload["surfaces"]},
            {
                "verireel/prod/lane",
                "verireel/testing/lane",
                "verireel-testing/preview-verireel-testing-verireel-pr-123",
            },
        )

    def test_data_freshness_report_requires_database_or_local_inspection(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            result = runner.invoke(
                CLI_MAIN,
                [
                    "service",
                    "inspect-data-freshness",
                    "--state-dir",
                    str(state_dir),
                    "--context",
                    "verireel",
                    "--preview-context",
                    "verireel-testing",
                ],
            )

        self.assertEqual(result.exit_code, 1, msg=result.output)
        self.assertIn("requires --database-url or LAUNCHPLANE_DATABASE_URL", result.output)
        self.assertIn("--local-inspection", result.output)

    def test_data_freshness_report_uses_empty_preview_inventory_scan(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_preview_inventory_scan_record(
                PreviewInventoryScanRecord(
                    scan_id="preview-inventory-scan-verireel-testing-20260420T100500Z",
                    context="verireel-testing",
                    scanned_at="2026-04-20T10:05:00Z",
                    source="verireel-preview-inventory",
                    status="pass",
                    preview_count=0,
                    preview_slugs=(),
                )
            )

            result = runner.invoke(
                CLI_MAIN,
                [
                    "service",
                    "inspect-data-freshness",
                    "--local-inspection",
                    "--state-dir",
                    str(state_dir),
                    "--context",
                    "verireel",
                    "--preview-context",
                    "verireel-testing",
                ],
            )

        self.assertEqual(result.exit_code, 1, msg=result.output)
        payload = json.loads(result.output.split("\nError:", maxsplit=1)[0])
        self.assertEqual(payload["status"], "rejected")
        preview_surface = next(
            surface
            for surface in payload["surfaces"]
            if surface["name"] == "verireel-testing/preview-inventory"
        )
        self.assertTrue(preview_surface["has_provenance"])
        self.assertEqual(
            preview_surface["source_record_id"],
            "preview-inventory-scan-verireel-testing-20260420T100500Z",
        )
        self.assertEqual(payload["missing_provenance_count"], 2)

    def test_authz_policy_grant_endpoint_writes_db_record_and_updates_runtime(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
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
                            "actions": ["authz_policy_grant.write"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_wsgi_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/authz-policies/github-actions/grants",
                payload={
                    "schema_version": 1,
                    "product": "launchplane",
                    "mode": "apply",
                    "reason": "Grant product profile read for SYO promotion diagnostics.",
                    "related_issue": "cbusillo/launchplane#83",
                    "grant": {
                        "repository": "cbusillo/launchplane",
                        "workflow_refs": [
                            "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main"
                        ],
                        "event_names": ["workflow_dispatch"],
                        "products": ["sellyouroutboard", "launchplane"],
                        "contexts": ["launchplane"],
                        "actions": ["product_profile.read"],
                        "source_label": "test:audit-grant",
                    },
                },
                headers={"Idempotency-Key": "authz-grant:audit"},
            )
            store = PostgresRecordStore(database_url=database_url)
            try:
                active_policy = _authz_policy_record_by_id(
                    store.list_authz_policy_records(status="active"),
                    payload["records"]["authz_policy_record_id"],
                )
            finally:
                store.close()
            repeat_status_code, repeat_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/authz-policies/github-actions/grants",
                payload={
                    "schema_version": 1,
                    "product": "launchplane",
                    "mode": "apply",
                    "reason": "Grant product profile read for SYO promotion diagnostics.",
                    "related_issue": "cbusillo/launchplane#83",
                    "grant": {
                        "repository": "cbusillo/launchplane",
                        "workflow_refs": [
                            "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main"
                        ],
                        "event_names": ["workflow_dispatch"],
                        "products": ["sellyouroutboard", "launchplane"],
                        "contexts": ["launchplane"],
                        "actions": ["product_profile.read"],
                        "source_label": "test:audit-grant",
                    },
                },
                headers={"Idempotency-Key": "authz-grant:audit-repeat"},
            )

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["records"]["authz_policy_record_id"], active_policy.record_id)
        self.assertEqual(payload["result"]["changed"], True)
        self.assertEqual(payload["result"]["mode"], "apply")
        self.assertEqual(payload["result"]["audit"]["related_issue"], "cbusillo/launchplane#83")
        self.assertEqual(
            active_policy.audit["reason"],
            "Grant product profile read for SYO promotion diagnostics.",
        )
        self.assertIn("workflow_refs", json.dumps(active_policy.audit, sort_keys=True))
        actions_operator = active_policy.audit["operator"]
        self.assertIsInstance(actions_operator, dict)
        assert isinstance(actions_operator, dict)
        self.assertEqual(actions_operator["type"], "github_actions")
        self.assertNotIn("workflow_refs", json.dumps(payload, sort_keys=True))
        self.assertTrue(
            active_policy.policy.allows(
                identity=_identity(
                    repository="cbusillo/launchplane",
                    workflow_ref=(
                        "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main"
                    ),
                    event_name="workflow_dispatch",
                ),
                action="product_profile.read",
                product="sellyouroutboard",
                context="launchplane",
            )
        )
        self.assertEqual(repeat_status_code, 202)
        self.assertEqual(
            repeat_payload["records"]["authz_policy_record_id"], active_policy.record_id
        )
        self.assertEqual(repeat_payload["result"]["changed"], False)

    def test_authz_policy_removal_endpoint_writes_db_record_and_updates_runtime(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
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
                            "actions": ["authz_policy_grant.write"],
                        },
                        {
                            "repository": "cbusillo/launchplane",
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["launchplane_service_deploy.execute"],
                        },
                    ]
                }
            )
            app = create_launchplane_fastapi_wsgi_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/authz-policies/github-actions/removals",
                payload={
                    "schema_version": 1,
                    "product": "launchplane",
                    "mode": "apply",
                    "reason": "Remove broad deploy authority after route narrowing.",
                    "related_issue": "cbusillo/launchplane#1049",
                    "removal": {
                        "repository": "cbusillo/launchplane",
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": ["launchplane_service_deploy.execute"],
                        "source_label": "test:authz-removal",
                    },
                },
                headers={"Idempotency-Key": "authz-removal:deploy-authority"},
            )
            store = PostgresRecordStore(database_url=database_url)
            try:
                active_policy = _authz_policy_record_by_id(
                    store.list_authz_policy_records(status="active"),
                    payload["records"]["authz_policy_record_id"],
                )
            finally:
                store.close()
            repeat_status_code, repeat_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/authz-policies/github-actions/removals",
                payload={
                    "schema_version": 1,
                    "product": "launchplane",
                    "mode": "apply",
                    "reason": "Remove broad deploy authority after route narrowing.",
                    "related_issue": "cbusillo/launchplane#1049",
                    "removal": {
                        "repository": "cbusillo/launchplane",
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": ["launchplane_service_deploy.execute"],
                        "source_label": "test:authz-removal",
                    },
                },
                headers={"Idempotency-Key": "authz-removal:deploy-authority-repeat"},
            )

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["records"]["authz_policy_record_id"], active_policy.record_id)
        self.assertEqual(payload["result"]["changed"], True)
        self.assertEqual(payload["result"]["diff"]["removed_rule_count"], 1)
        self.assertNotIn('"requested_removal":', json.dumps(payload, sort_keys=True))
        self.assertFalse(
            active_policy.policy.allows(
                identity=_identity(repository="cbusillo/launchplane"),
                action="launchplane_service_deploy.execute",
                product="launchplane",
                context="launchplane",
            )
        )
        self.assertEqual(repeat_status_code, 202)
        self.assertEqual(repeat_payload["result"]["changed"], False)

    def test_authz_policy_grant_endpoint_dry_run_does_not_write_or_reload(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
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
                            "actions": ["authz_policy_grant.write"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_wsgi_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/authz-policies/github-actions/grants",
                payload={
                    "schema_version": 1,
                    "product": "launchplane",
                    "mode": "dry_run",
                    "reason": "Inspect whether product profile read grant is needed.",
                    "related_issue": "cbusillo/launchplane#83",
                    "grant": {
                        "repository": "cbusillo/launchplane",
                        "workflow_refs": [
                            "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main"
                        ],
                        "event_names": ["workflow_dispatch"],
                        "products": ["sellyouroutboard"],
                        "contexts": ["launchplane"],
                        "actions": ["product_profile.read"],
                        "source_label": "test:dry-run-grant",
                    },
                },
                headers={"Idempotency-Key": "authz-grant:dry-run"},
            )
            store = PostgresRecordStore(database_url=database_url)
            try:
                active_records = store.list_authz_policy_records(status="active")
                dry_run_idempotency_record = store.read_idempotency_record(
                    scope=(
                        "cbusillo/launchplane|"
                        "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main|"
                        "repo:every/verireel:pull_request"
                    ),
                    route_path="/v1/authz-policies/github-actions/grants",
                    idempotency_key="authz-grant:dry-run",
                )
            finally:
                store.close()

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["result"]["mode"], "dry_run")
        self.assertEqual(payload["result"]["changed"], True)
        self.assertEqual(payload["result"]["diff"]["new_github_actions_rule_count"], 2)
        self.assertEqual(payload["result"]["audit"]["mode"], "dry_run")
        self.assertNotIn("requested_grant", payload["result"]["audit"])
        self.assertEqual(
            payload["result"]["audit"]["requested_grant_summary"]["workflow_ref_count"],
            1,
        )
        self.assertEqual(len(active_records), 1)
        self.assertIsNone(dry_run_idempotency_record)
        self.assertFalse(
            active_records[0].policy.allows(
                identity=_identity(
                    repository="cbusillo/launchplane",
                    workflow_ref=(
                        "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main"
                    ),
                    event_name="workflow_dispatch",
                ),
                action="product_profile.read",
                product="sellyouroutboard",
                context="launchplane",
            )
        )

    def test_authz_policy_removal_endpoint_dry_run_does_not_write_or_reload(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
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
                            "actions": ["authz_policy_grant.write"],
                        },
                        {
                            "repository": "cbusillo/launchplane",
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["launchplane_service_deploy.execute"],
                        },
                    ]
                }
            )
            app = create_launchplane_fastapi_wsgi_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/authz-policies/github-actions/removals",
                payload={
                    "schema_version": 1,
                    "product": "launchplane",
                    "mode": "dry_run",
                    "reason": "Inspect broad deploy authority removal.",
                    "related_issue": "cbusillo/launchplane#1049",
                    "removal": {
                        "repository": "cbusillo/launchplane",
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": ["launchplane_service_deploy.execute"],
                        "source_label": "test:authz-removal",
                    },
                },
                headers={"Idempotency-Key": "authz-removal:dry-run"},
            )
            store = PostgresRecordStore(database_url=database_url)
            try:
                active_records = store.list_authz_policy_records(status="active")
                dry_run_idempotency_record = store.read_idempotency_record(
                    scope=(
                        "cbusillo/launchplane|"
                        "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main|"
                        "repo:every/verireel:pull_request"
                    ),
                    route_path="/v1/authz-policies/github-actions/removals",
                    idempotency_key="authz-removal:dry-run",
                )
            finally:
                store.close()

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["result"]["mode"], "dry_run")
        self.assertEqual(payload["result"]["changed"], True)
        self.assertEqual(payload["result"]["diff"]["matched_rule_count"], 1)
        self.assertEqual(len(active_records), 1)
        self.assertIsNone(dry_run_idempotency_record)

    def test_authz_policy_grant_endpoint_allows_admin_human_session(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_humans": [
                        {
                            "logins": ["alice"],
                            "roles": ["admin"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["authz_policy_grant.write"],
                        }
                    ]
                }
            )
            session_manager = _fastapi_human_session_manager()
            app = create_launchplane_fastapi_wsgi_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
                human_session_manager=session_manager,
            )
            cookie = _fastapi_signed_in_cookie(session_manager)

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/authz-policies/github-actions/grants",
                payload={
                    "schema_version": 1,
                    "product": "launchplane",
                    "mode": "apply",
                    "reason": "Allow product profile reads for operator diagnostics.",
                    "related_issue": "cbusillo/launchplane#83",
                    "grant": {
                        "repository": "cbusillo/launchplane",
                        "workflow_refs": [
                            "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main"
                        ],
                        "event_names": ["workflow_dispatch"],
                        "products": ["sellyouroutboard"],
                        "contexts": ["launchplane"],
                        "actions": ["product_profile.read"],
                        "source_label": "test:human-grant",
                    },
                },
                authorization="",
                headers={
                    "Cookie": cookie,
                    "Idempotency-Key": "authz-grant:human-admin",
                },
            )
            store = PostgresRecordStore(database_url=database_url)
            try:
                active_policy = _authz_policy_record_by_id(
                    store.list_authz_policy_records(status="active"),
                    payload["records"]["authz_policy_record_id"],
                )
            finally:
                store.close()

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["result"]["changed"], True)
        self.assertEqual(payload["result"]["audit"]["operator"]["type"], "github_human")
        self.assertEqual(payload["result"]["audit"]["operator"]["login"], "alice")
        human_operator = active_policy.audit["operator"]
        self.assertIsInstance(human_operator, dict)
        assert isinstance(human_operator, dict)
        self.assertEqual(human_operator["type"], "github_human")

    def test_human_authz_policy_grant_endpoint_writes_db_record_and_updates_runtime(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_humans": [
                        {
                            "logins": ["alice"],
                            "roles": ["admin"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["authz_policy_grant.write"],
                        }
                    ]
                }
            )
            session_manager = _fastapi_human_session_manager()
            app = create_launchplane_fastapi_wsgi_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
                human_session_manager=session_manager,
            )
            cookie = _fastapi_signed_in_cookie(session_manager)

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/authz-policies/github-humans/grants",
                payload={
                    "schema_version": 1,
                    "product": "launchplane",
                    "mode": "apply",
                    "reason": "Allow SYO promotion workflow dispatch from the operator UI.",
                    "related_issue": "cbusillo/launchplane#153",
                    "grant": {
                        "logins": ["alice"],
                        "roles": ["admin"],
                        "products": ["sellyouroutboard"],
                        "contexts": ["sellyouroutboard", "launchplane"],
                        "actions": [
                            "generic_web_prod_promotion.dispatch",
                            "product_environment.read",
                        ],
                        "source_label": "test:human-promotion-grant",
                    },
                },
                authorization="",
                headers={
                    "Cookie": cookie,
                    "Idempotency-Key": "authz-human-grant:dispatch",
                },
            )
            repeat_status_code, repeat_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/authz-policies/github-humans/grants",
                payload={
                    "schema_version": 1,
                    "product": "launchplane",
                    "mode": "apply",
                    "reason": "Allow SYO promotion workflow dispatch from the operator UI.",
                    "related_issue": "cbusillo/launchplane#153",
                    "grant": {
                        "logins": ["alice"],
                        "roles": ["admin"],
                        "products": ["sellyouroutboard"],
                        "contexts": ["sellyouroutboard", "launchplane"],
                        "actions": [
                            "generic_web_prod_promotion.dispatch",
                            "product_environment.read",
                        ],
                        "source_label": "test:human-promotion-grant",
                    },
                },
                authorization="",
                headers={
                    "Cookie": cookie,
                    "Idempotency-Key": "authz-human-grant:dispatch-repeat",
                },
            )
            store = PostgresRecordStore(database_url=database_url)
            try:
                active_policy = _authz_policy_record_by_id(
                    store.list_authz_policy_records(status="active"),
                    payload["records"]["authz_policy_record_id"],
                )
            finally:
                store.close()

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["result"]["changed"], True)
        self.assertEqual(payload["result"]["diff"]["new_github_humans_rule_count"], 2)
        self.assertEqual(payload["result"]["audit"]["requested_grant_summary"]["login_count"], 1)
        self.assertNotIn(
            "alice",
            json.dumps(payload["result"]["audit"]["requested_grant_summary"], sort_keys=True),
        )
        self.assertEqual(repeat_status_code, 202)
        self.assertEqual(repeat_payload["result"]["changed"], False)
        self.assertTrue(
            active_policy.policy.allows(
                identity=_human_identity(role="admin"),
                action="generic_web_prod_promotion.dispatch",
                product="sellyouroutboard",
                context="sellyouroutboard",
            )
        )

    def test_human_authz_policy_grant_endpoint_dry_run_does_not_write_or_reload(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_humans": [
                        {
                            "logins": ["alice"],
                            "roles": ["admin"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["authz_policy_grant.write"],
                        }
                    ]
                }
            )
            session_manager = _fastapi_human_session_manager()
            authz_policy_runtime = LaunchplaneAuthzPolicyRuntime(policy)
            app = create_launchplane_fastapi_wsgi_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                authz_policy_runtime=authz_policy_runtime,
                control_plane_root_path=root,
                database_url=database_url,
                human_session_manager=session_manager,
            )
            profile_payload = _product_profile_payload_with_prod()
            profile_payload["lanes"] = tuple(
                {**lane, "context": "sellyouroutboard"}
                for lane in _product_profile_lanes(profile_payload)
            )
            store = PostgresRecordStore(database_url=database_url)
            try:
                store.write_authz_policy_record(
                    LaunchplaneAuthzPolicyRecord(
                        record_id="launchplane-authz-policy-human-dry-run-test",
                        source="test",
                        updated_at="2026-05-02T22:35:00Z",
                        policy=policy,
                    )
                )
                store.write_product_profile_record(
                    LaunchplaneProductProfileRecord.model_validate(profile_payload)
                )
            finally:
                store.close()
            cookie = _fastapi_signed_in_cookie(session_manager)

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/authz-policies/github-humans/grants",
                payload={
                    "schema_version": 1,
                    "product": "launchplane",
                    "mode": "dry_run",
                    "reason": "Inspect SYO operator promotion grant.",
                    "related_issue": "cbusillo/launchplane#153",
                    "grant": {
                        "logins": ["alice"],
                        "roles": ["admin"],
                        "products": ["sellyouroutboard"],
                        "contexts": ["sellyouroutboard"],
                        "actions": ["generic_web_prod_promotion.dispatch"],
                        "source_label": "test:human-promotion-grant",
                    },
                },
                authorization="",
                headers={
                    "Cookie": cookie,
                    "Idempotency-Key": "authz-human-grant:dry-run",
                },
            )
            store = PostgresRecordStore(database_url=database_url)
            try:
                active_records = store.list_authz_policy_records(status="active")
                dry_run_idempotency_record = store.read_idempotency_record(
                    scope="github-human:alice",
                    route_path="/v1/authz-policies/github-humans/grants",
                    idempotency_key="authz-human-grant:dry-run",
                )
            finally:
                store.close()

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["result"]["mode"], "dry_run")
        self.assertEqual(payload["result"]["changed"], True)
        self.assertEqual(len(active_records), 1)
        self.assertIsNone(dry_run_idempotency_record)
        self.assertFalse(
            active_records[0].policy.allows(
                identity=_human_identity(role="admin"),
                action="generic_web_prod_promotion.dispatch",
                product="sellyouroutboard",
                context="sellyouroutboard",
            )
        )
        self.assertFalse(
            authz_policy_runtime.policy.allows(
                identity=_human_identity(role="admin"),
                action="generic_web_prod_promotion.dispatch",
                product="sellyouroutboard",
                context="sellyouroutboard",
            )
        )

    def test_terminal_agent_authz_policy_grant_endpoint_writes_db_record_and_updates_runtime(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_humans": [
                        {
                            "logins": ["alice"],
                            "roles": ["admin"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["authz_policy_grant.write"],
                        }
                    ]
                }
            )
            session_manager = _fastapi_human_session_manager()
            app = create_launchplane_fastapi_wsgi_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
                human_session_manager=session_manager,
            )
            cookie = _fastapi_signed_in_cookie(session_manager)

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/authz-policies/terminal-agents/grants",
                payload={
                    "schema_version": 1,
                    "product": "launchplane",
                    "mode": "apply",
                    "reason": "Allow local terminal agent product context reads.",
                    "related_issue": "cbusillo/launchplane#426",
                    "grant": {
                        "subjects": ["local-owner-agent"],
                        "token_labels": ["local-owner-read"],
                        "products": ["sellyouroutboard"],
                        "contexts": ["sellyouroutboard"],
                        "actions": ["product_environment.read"],
                        "source_label": "test:terminal-agent-grant",
                    },
                },
                authorization="",
                headers={
                    "Cookie": cookie,
                    "Idempotency-Key": "authz-terminal-agent-grant:syo-read",
                },
            )
            repeat_status_code, repeat_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/authz-policies/terminal-agents/grants",
                payload={
                    "schema_version": 1,
                    "product": "launchplane",
                    "mode": "apply",
                    "reason": "Allow local terminal agent product context reads.",
                    "related_issue": "cbusillo/launchplane#426",
                    "grant": {
                        "subjects": ["local-owner-agent"],
                        "token_labels": ["local-owner-read"],
                        "products": ["sellyouroutboard"],
                        "contexts": ["sellyouroutboard"],
                        "actions": ["product_environment.read"],
                        "source_label": "test:terminal-agent-grant",
                    },
                },
                authorization="",
                headers={
                    "Cookie": cookie,
                    "Idempotency-Key": "authz-terminal-agent-grant:syo-read-repeat",
                },
            )
            store = PostgresRecordStore(database_url=database_url)
            try:
                active_policy = _authz_policy_record_by_id(
                    store.list_authz_policy_records(status="active"),
                    payload["records"]["authz_policy_record_id"],
                )
            finally:
                store.close()

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["result"]["changed"], True)
        self.assertEqual(payload["result"]["diff"]["new_terminal_agents_rule_count"], 1)
        self.assertEqual(payload["result"]["audit"]["requested_grant_summary"]["subject_count"], 1)
        self.assertNotIn(
            "local-owner-agent",
            json.dumps(payload["result"]["audit"]["requested_grant_summary"], sort_keys=True),
        )
        self.assertEqual(repeat_status_code, 202)
        self.assertEqual(repeat_payload["result"]["changed"], False)
        self.assertTrue(
            active_policy.policy.allows(
                identity=TerminalAgentIdentity(
                    subject="local-owner-agent", token_label="local-owner-read"
                ),
                action="product_environment.read",
                product="sellyouroutboard",
                context="sellyouroutboard",
            )
        )

    def test_terminal_agent_authz_policy_grant_endpoint_dry_run_does_not_write_or_reload(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_humans": [
                        {
                            "logins": ["alice"],
                            "roles": ["admin"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["authz_policy_grant.write"],
                        }
                    ]
                }
            )
            session_manager = _fastapi_human_session_manager()
            app = create_launchplane_fastapi_wsgi_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
                human_session_manager=session_manager,
            )
            store = PostgresRecordStore(database_url=database_url)
            try:
                store.write_product_profile_record(
                    LaunchplaneProductProfileRecord.model_validate(_generic_site_profile_payload())
                )
            finally:
                store.close()
            cookie = _fastapi_signed_in_cookie(session_manager)

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/authz-policies/terminal-agents/grants",
                payload={
                    "schema_version": 1,
                    "product": "launchplane",
                    "mode": "dry_run",
                    "reason": "Inspect terminal-agent product context grant.",
                    "related_issue": "cbusillo/launchplane#426",
                    "grant": {
                        "subjects": ["local-owner-agent"],
                        "token_labels": ["local-owner-read"],
                        "products": ["example-site"],
                        "contexts": ["example-site"],
                        "actions": ["product_environment.read"],
                        "source_label": "test:terminal-agent-grant",
                    },
                },
                authorization="",
                headers={
                    "Cookie": cookie,
                    "Idempotency-Key": "authz-terminal-agent-grant:dry-run",
                },
            )
            store = PostgresRecordStore(database_url=database_url)
            try:
                active_records = store.list_authz_policy_records(status="active")
                dry_run_idempotency_record = store.read_idempotency_record(
                    scope="github-human:alice",
                    route_path="/v1/authz-policies/terminal-agents/grants",
                    idempotency_key="authz-terminal-agent-grant:dry-run",
                )
            finally:
                store.close()

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["result"]["mode"], "dry_run")
        self.assertEqual(payload["result"]["changed"], True)
        self.assertEqual(len(active_records), 1)
        self.assertIsNone(dry_run_idempotency_record)
        self.assertFalse(
            active_records[0].policy.allows(
                identity=TerminalAgentIdentity(
                    subject="local-owner-agent", token_label="local-owner-read"
                ),
                action="product_environment.read",
                product="example-site",
                context="example-site",
            )
        )

    def test_local_operator_authz_policy_grant_endpoint_writes_db_record_and_updates_runtime(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_humans": [
                        {
                            "logins": ["alice"],
                            "roles": ["admin"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["authz_policy_grant.write"],
                        }
                    ]
                }
            )
            session_manager = _fastapi_human_session_manager()
            app = create_launchplane_fastapi_wsgi_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
                human_session_manager=session_manager,
            )
            cookie = _fastapi_signed_in_cookie(session_manager)

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/authz-policies/local-operators/grants",
                payload={
                    "schema_version": 1,
                    "product": "launchplane",
                    "mode": "apply",
                    "reason": "Allow local owner operator ingress notification config.",
                    "related_issue": "cbusillo/launchplane#929",
                    "grant": {
                        "subjects": ["local-owner-agent"],
                        "token_labels": ["local-owner-write"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": ["public_ingress_notification_policy.apply"],
                        "source_label": "test:local-operator-grant",
                    },
                },
                authorization="",
                headers={
                    "Cookie": cookie,
                    "Idempotency-Key": "authz-local-operator-grant:ingress",
                },
            )
            repeat_status_code, repeat_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/authz-policies/local-operators/grants",
                payload={
                    "schema_version": 1,
                    "product": "launchplane",
                    "mode": "apply",
                    "reason": "Allow local owner operator ingress notification config.",
                    "related_issue": "cbusillo/launchplane#929",
                    "grant": {
                        "subjects": ["local-owner-agent"],
                        "token_labels": ["local-owner-write"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": ["public_ingress_notification_policy.apply"],
                        "source_label": "test:local-operator-grant",
                    },
                },
                authorization="",
                headers={
                    "Cookie": cookie,
                    "Idempotency-Key": "authz-local-operator-grant:ingress-repeat",
                },
            )
            store = PostgresRecordStore(database_url=database_url)
            try:
                active_policy = _authz_policy_record_by_id(
                    store.list_authz_policy_records(status="active"),
                    payload["records"]["authz_policy_record_id"],
                )
            finally:
                store.close()

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["result"]["changed"], True)
        self.assertEqual(payload["result"]["diff"]["new_local_operators_rule_count"], 1)
        self.assertEqual(
            payload["result"]["audit"]["requested_grant_summary"]["principal_type"],
            "local_operator",
        )
        self.assertNotIn(
            "local-owner-agent",
            json.dumps(payload["result"]["audit"]["requested_grant_summary"], sort_keys=True),
        )
        self.assertEqual(repeat_status_code, 202)
        self.assertEqual(repeat_payload["result"]["changed"], False)
        self.assertTrue(
            active_policy.policy.allows(
                identity=LocalOperatorIdentity(
                    subject="local-owner-agent", token_label="local-owner-write"
                ),
                action="public_ingress_notification_policy.apply",
                product="launchplane",
                context="launchplane",
            )
        )

    def test_local_admin_authz_policy_grant_endpoint_writes_separate_rule(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_humans": [
                        {
                            "logins": ["alice"],
                            "roles": ["admin"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["authz_policy_grant.write"],
                        }
                    ]
                }
            )
            session_manager = _fastapi_human_session_manager()
            app = create_launchplane_fastapi_wsgi_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
                human_session_manager=session_manager,
            )
            cookie = _fastapi_signed_in_cookie(session_manager)

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/authz-policies/local-admins/grants",
                payload={
                    "schema_version": 1,
                    "product": "launchplane",
                    "mode": "apply",
                    "reason": "Allow local owner admin authz grants.",
                    "related_issue": "cbusillo/launchplane#929",
                    "grant": {
                        "subjects": ["local-owner-admin"],
                        "token_labels": ["local-owner-admin"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": ["launchplane_service_deploy.execute"],
                        "source_label": "test:local-admin-grant",
                    },
                },
                authorization="",
                headers={
                    "Cookie": cookie,
                    "Idempotency-Key": "authz-local-admin-grant:self-deploy",
                },
            )
            store = PostgresRecordStore(database_url=database_url)
            try:
                active_policy = _authz_policy_record_by_id(
                    store.list_authz_policy_records(status="active"),
                    payload["records"]["authz_policy_record_id"],
                )
            finally:
                store.close()

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["result"]["diff"]["new_local_admins_rule_count"], 1)
        self.assertEqual(
            payload["result"]["audit"]["requested_grant_summary"]["principal_type"],
            "local_admin",
        )
        self.assertTrue(
            active_policy.policy.allows(
                identity=LocalAdminIdentity(
                    subject="local-owner-admin", token_label="local-owner-admin"
                ),
                action="launchplane_service_deploy.execute",
                product="launchplane",
                context="launchplane",
            )
        )

    def test_authz_policy_grant_endpoint_apply_requires_reason(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/launchplane",
                            "actions": ["authz_policy_grant.write"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_wsgi_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity(repository="cbusillo/launchplane")),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/authz-policies/github-actions/grants",
                payload={
                    "schema_version": 1,
                    "product": "launchplane",
                    "mode": "apply",
                    "grant": {
                        "repository": "cbusillo/launchplane",
                        "actions": ["product_profile.read"],
                    },
                },
                headers={"Idempotency-Key": "authz-grant:no-reason"},
            )

        self.assertEqual(status_code, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")

    def test_authz_policy_grant_endpoint_rejects_without_policy_grant_permission(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/launchplane",
                            "actions": ["product_profile.read"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_wsgi_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/authz-policies/github-actions/grants",
                payload={
                    "schema_version": 1,
                    "product": "launchplane",
                    "mode": "apply",
                    "reason": "Attempt unauthorized grant.",
                    "grant": {
                        "repository": "cbusillo/launchplane",
                        "actions": ["product_profile.read"],
                    },
                },
                headers={"Idempotency-Key": "authz-grant:unauthorized"},
            )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_authz_policy_grant_endpoint_rejects_self_deploy_authority(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
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
            app = create_launchplane_fastapi_wsgi_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/authz-policies/github-actions/grants",
                payload={
                    "schema_version": 1,
                    "product": "launchplane",
                    "mode": "apply",
                    "reason": "Attempt policy grant with deploy authority.",
                    "grant": {
                        "repository": "cbusillo/launchplane",
                        "actions": ["product_profile.read"],
                    },
                },
                headers={"Idempotency-Key": "authz-grant:self-deploy-denied"},
            )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_authz_policy_removal_endpoint_rejects_self_deploy_authority(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
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
            app = create_launchplane_fastapi_wsgi_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/authz-policies/github-actions/removals",
                payload={
                    "schema_version": 1,
                    "product": "launchplane",
                    "mode": "apply",
                    "reason": "Attempt policy removal with deploy authority.",
                    "removal": {
                        "repository": "cbusillo/launchplane",
                        "actions": ["launchplane_service_deploy.execute"],
                    },
                },
                headers={"Idempotency-Key": "authz-removal:self-deploy-denied"},
            )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_live_target_runtime_api_dry_run_returns_redacted_delta(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                store.write_runtime_environment_record(
                    RuntimeEnvironmentRecord(
                        scope="instance",
                        context="sellyouroutboard",
                        instance="prod",
                        env={"GOOGLE_ANALYTICS_MEASUREMENT_ID": "G-9KRMER45KG"},
                        updated_at="2026-05-06T17:00:00Z",
                        source_label="test",
                    )
                )
                store.write_runtime_environment_record(
                    RuntimeEnvironmentRecord(
                        scope="global",
                        env={"ODOO_DB_PASSWORD": "must-not-sync"},
                        updated_at="2026-05-06T17:00:00Z",
                        source_label="test",
                    )
                )
                store.write_product_profile_record(
                    LaunchplaneProductProfileRecord.model_validate(
                        _live_target_runtime_profile_payload(include_context_secret=True)
                    )
                )
                _seed_tracked_target_records(
                    database_url=database_url,
                    context="sellyouroutboard",
                    instance="prod",
                    target_id="application-syo-prod",
                    target_type="application",
                    target_name="syo-prod-app",
                )
                with patch.dict(
                    os.environ,
                    {
                        "LAUNCHPLANE_DATABASE_URL": database_url,
                        control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key",
                    },
                    clear=True,
                ):
                    _write_dokploy_managed_secrets(store=store)
                    control_plane_secrets.write_secret_value(
                        record_store=store,
                        scope="context",
                        integration=control_plane_secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
                        name="context-api-token",
                        plaintext_value="context-secret-value",
                        binding_key="CONTEXT_API_TOKEN",
                        context_name="sellyouroutboard",
                        actor="test",
                        source_label="test",
                    )
                    store.write_runtime_key_safety_policy_record(
                        RuntimeKeySafetyPolicyRecord(
                            record_id="runtime-key-safety-policy-live-target-test",
                            status="active",
                            source="test",
                            updated_at="2026-05-05T20:00:00Z",
                            rules=(
                                RuntimeSecretSafetyRule(
                                    binding_key="CONTEXT_API_TOKEN",
                                    secret_class="prod_only",
                                    allowed_contexts=("sellyouroutboard",),
                                    allowed_instances=("prod",),
                                ),
                            ),
                        )
                    )
            finally:
                store.close()
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/launchplane",
                            "workflow_refs": [
                                "cbusillo/launchplane/.github/workflows/live-target-runtime.yml@refs/heads/main"
                            ],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard"],
                            "actions": ["live_target_runtime.plan"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_wsgi_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/live-target-runtime.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )

            with (
                patch.dict(
                    os.environ,
                    {
                        control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key",
                    },
                    clear=True,
                ),
                patch(
                    "control_plane.dokploy.fetch_dokploy_target_payload",
                    return_value={
                        "applicationId": "application-syo-prod",
                        "name": "syo-prod-app",
                        "env": "CONTACT_EMAIL_MODE=resend\n",
                    },
                ),
                patch("control_plane.dokploy.update_dokploy_target_env") as update_env,
            ):
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/live-target-runtime/apply",
                    payload={
                        "schema_version": 1,
                        "mode": "dry-run",
                        "product": "sellyouroutboard",
                        "context": "sellyouroutboard",
                        "instance": "prod",
                    },
                    headers={"Idempotency-Key": "live-target-runtime:dry-run"},
                )

        self.assertEqual(status_code, 202, msg=json.dumps(payload, indent=2, sort_keys=True))
        update_env.assert_not_called()
        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(payload["records"]["target_id"], "application-syo-prod")
        result = payload["result"]
        self.assertEqual(result["mode"], "dry-run")
        self.assertEqual(
            result["runtime_environment"]["missing_keys"],
            ["CONTEXT_API_TOKEN", "GOOGLE_ANALYTICS_MEASUREMENT_ID"],
        )
        self.assertEqual(
            result["runtime_key_safety"]["checked_binding_keys"], ["CONTEXT_API_TOKEN"]
        )
        self.assertEqual(result["runtime_key_safety"]["status"], "pass")
        self.assertNotIn("ODOO_DB_PASSWORD", result["runtime_environment"]["changed_keys"])
        self.assertNotIn("G-9KRMER45KG", json.dumps(payload))
        self.assertNotIn("must-not-sync", json.dumps(payload))
        self.assertNotIn("context-secret-value", json.dumps(payload))

    def test_live_target_runtime_api_requires_expected_managed_secret_values(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                store.write_runtime_environment_record(
                    RuntimeEnvironmentRecord(
                        scope="instance",
                        context="sellyouroutboard",
                        instance="prod",
                        env={"GOOGLE_ANALYTICS_MEASUREMENT_ID": "G-9KRMER45KG"},
                        updated_at="2026-05-06T17:00:00Z",
                        source_label="test",
                    )
                )
                store.write_product_profile_record(
                    LaunchplaneProductProfileRecord.model_validate(
                        _live_target_runtime_profile_payload(include_context_secret=True)
                    )
                )
                _seed_tracked_target_records(
                    database_url=database_url,
                    context="sellyouroutboard",
                    instance="prod",
                    target_id="application-syo-prod",
                    target_type="application",
                    target_name="syo-prod-app",
                )
            finally:
                store.close()
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/launchplane",
                            "workflow_refs": [
                                "cbusillo/launchplane/.github/workflows/live-target-runtime.yml@refs/heads/main"
                            ],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard"],
                            "actions": ["live_target_runtime.plan"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_wsgi_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/live-target-runtime.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )

            with (
                patch.dict(os.environ, {"LAUNCHPLANE_DATABASE_URL": database_url}, clear=True),
                patch(
                    "control_plane.dokploy.fetch_dokploy_target_payload",
                    return_value={
                        "applicationId": "application-syo-prod",
                        "name": "syo-prod-app",
                        "env": "CONTACT_EMAIL_MODE=resend\n",
                    },
                ),
                patch("control_plane.dokploy.update_dokploy_target_env") as update_env,
            ):
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/live-target-runtime/apply",
                    payload={
                        "schema_version": 1,
                        "mode": "dry-run",
                        "product": "sellyouroutboard",
                        "context": "sellyouroutboard",
                        "instance": "prod",
                    },
                    headers={"Idempotency-Key": "live-target-runtime:missing-secret"},
                )

        self.assertEqual(status_code, 400, msg=json.dumps(payload, indent=2, sort_keys=True))
        update_env.assert_not_called()
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["error"]["code"], "runtime_secret_values_missing")
        self.assertIn("CONTEXT_API_TOKEN", payload["error"]["message"])
        self.assertNotIn("G-9KRMER45KG", json.dumps(payload))

    def test_live_target_runtime_api_apply_updates_env_and_verifies(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                store.write_runtime_environment_record(
                    RuntimeEnvironmentRecord(
                        scope="instance",
                        context="sellyouroutboard",
                        instance="prod",
                        env={"GOOGLE_ANALYTICS_MEASUREMENT_ID": "G-9KRMER45KG"},
                        updated_at="2026-05-06T17:00:00Z",
                        source_label="test",
                    )
                )
                store.write_product_profile_record(
                    LaunchplaneProductProfileRecord.model_validate(
                        _live_target_runtime_profile_payload()
                    )
                )
                _seed_tracked_target_records(
                    database_url=database_url,
                    context="sellyouroutboard",
                    instance="prod",
                    target_id="application-syo-prod",
                    target_type="application",
                    target_name="syo-prod-app",
                )
                with patch.dict(
                    os.environ,
                    {
                        "LAUNCHPLANE_DATABASE_URL": database_url,
                        control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key",
                    },
                    clear=True,
                ):
                    _write_dokploy_managed_secrets(store=store)
            finally:
                store.close()
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/launchplane",
                            "workflow_refs": [
                                "cbusillo/launchplane/.github/workflows/live-target-runtime.yml@refs/heads/main"
                            ],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard"],
                            "actions": ["live_target_runtime.apply"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_wsgi_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/live-target-runtime.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )
            captured_env_updates: list[dict[str, object]] = []

            def fetch_target_payload(**_kwargs: object) -> dict[str, object]:
                env_text = "CONTACT_EMAIL_MODE=resend\n"
                if captured_env_updates:
                    env_text = str(captured_env_updates[-1]["env_text"])
                return {
                    "applicationId": "application-syo-prod",
                    "name": "syo-prod-app",
                    "env": env_text,
                }

            with (
                patch.dict(
                    os.environ,
                    {
                        "LAUNCHPLANE_DATABASE_URL": database_url,
                        control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key",
                    },
                    clear=True,
                ),
                patch(
                    "control_plane.dokploy.fetch_dokploy_target_payload",
                    side_effect=fetch_target_payload,
                ),
                patch(
                    "control_plane.dokploy.update_dokploy_target_env",
                    side_effect=lambda **kwargs: captured_env_updates.append(kwargs),
                ),
            ):
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/live-target-runtime/apply",
                    payload={
                        "schema_version": 1,
                        "mode": "apply",
                        "product": "sellyouroutboard",
                        "context": "sellyouroutboard",
                        "instance": "prod",
                    },
                    headers={"Idempotency-Key": "live-target-runtime:apply"},
                )

        self.assertEqual(status_code, 202, msg=json.dumps(payload, indent=2, sort_keys=True))
        self.assertEqual(len(captured_env_updates), 1)
        env_text = str(captured_env_updates[0]["env_text"])
        self.assertIn("GOOGLE_ANALYTICS_MEASUREMENT_ID=G-9KRMER45KG", env_text)
        self.assertNotIn("G-9KRMER45KG", json.dumps(payload))
        result = payload["result"]
        self.assertEqual(result["mode"], "apply")
        self.assertTrue(result["apply"]["env_updated"])
        self.assertEqual(result["apply"]["verification"]["status"], "pass")

    def test_live_target_runtime_api_apply_can_trigger_deploy(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                store.write_runtime_environment_record(
                    RuntimeEnvironmentRecord(
                        scope="instance",
                        context="sellyouroutboard",
                        instance="prod",
                        env={"GOOGLE_ANALYTICS_MEASUREMENT_ID": "G-9KRMER45KG"},
                        updated_at="2026-05-06T17:00:00Z",
                        source_label="test",
                    )
                )
                store.write_product_profile_record(
                    LaunchplaneProductProfileRecord.model_validate(
                        _live_target_runtime_profile_payload()
                    )
                )
                _seed_tracked_target_records(
                    database_url=database_url,
                    context="sellyouroutboard",
                    instance="prod",
                    target_id="application-syo-prod",
                    target_type="application",
                    target_name="syo-prod-app",
                    deploy_timeout_seconds=77,
                )
                with patch.dict(
                    os.environ,
                    {
                        "LAUNCHPLANE_DATABASE_URL": database_url,
                        control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key",
                    },
                    clear=True,
                ):
                    _write_dokploy_managed_secrets(store=store)
            finally:
                store.close()
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/launchplane",
                            "workflow_refs": [
                                "cbusillo/launchplane/.github/workflows/live-target-runtime.yml@refs/heads/main"
                            ],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard"],
                            "actions": ["live_target_runtime.apply"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_wsgi_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/live-target-runtime.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )

            with (
                patch.dict(
                    os.environ,
                    {
                        "LAUNCHPLANE_DATABASE_URL": database_url,
                        control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key",
                    },
                    clear=True,
                ),
                patch(
                    "control_plane.dokploy.fetch_dokploy_target_payload",
                    return_value={
                        "applicationId": "application-syo-prod",
                        "name": "syo-prod-app",
                        "env": "GOOGLE_ANALYTICS_MEASUREMENT_ID=G-9KRMER45KG\n",
                    },
                ),
                patch("control_plane.dokploy.update_dokploy_target_env") as update_env,
                patch(
                    "control_plane.dokploy.latest_deployment_for_target",
                    return_value={"deploymentId": "before"},
                ),
                patch("control_plane.dokploy.trigger_deployment") as trigger_deployment,
                patch(
                    "control_plane.dokploy.wait_for_target_deployment",
                    return_value="deployment=after status=done",
                ) as wait_for_target_deployment,
            ):
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/live-target-runtime/apply",
                    payload={
                        "schema_version": 1,
                        "mode": "apply",
                        "product": "sellyouroutboard",
                        "context": "sellyouroutboard",
                        "instance": "prod",
                        "deploy": True,
                        "no_cache": True,
                    },
                    headers={"Idempotency-Key": "live-target-runtime:deploy"},
                )

        self.assertEqual(status_code, 202, msg=json.dumps(payload, indent=2, sort_keys=True))
        update_env.assert_not_called()
        trigger_deployment.assert_called_once()
        self.assertEqual(trigger_deployment.call_args.kwargs["target_type"], "application")
        self.assertEqual(trigger_deployment.call_args.kwargs["target_id"], "application-syo-prod")
        self.assertTrue(trigger_deployment.call_args.kwargs["no_cache"])
        wait_for_target_deployment.assert_called_once()
        self.assertEqual(wait_for_target_deployment.call_args.kwargs["timeout_seconds"], 77)
        result = payload["result"]
        self.assertEqual(result["mode"], "apply")
        self.assertFalse(result["apply"]["env_updated"])
        self.assertTrue(result["deploy"]["triggered"])
        self.assertEqual(
            result["deploy"]["result"]["deployment_result"], "deployment=after status=done"
        )

    def test_live_target_runtime_apply_requires_database_for_key_safety(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            with (
                patch.dict(
                    os.environ,
                    {
                        control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"
                    },
                    clear=True,
                ),
                patch(
                    "control_plane.dokploy.read_control_plane_dokploy_source_of_truth",
                    return_value=DokploySourceOfTruth(
                        schema_version=1,
                        targets=(
                            DokployTargetDefinition(
                                context="sellyouroutboard",
                                instance="prod",
                                target_type="application",
                                target_name="syo-prod-app",
                                target_id="application-syo-prod",
                            ),
                        ),
                    ),
                ),
                patch(
                    "control_plane.runtime_environments.resolve_runtime_environment_values",
                    return_value={"GOOGLE_ANALYTICS_MEASUREMENT_ID": "G-9KRMER45KG"},
                ),
                patch(
                    "control_plane.dokploy.read_dokploy_config",
                    return_value=("https://dokploy.example.com", "dokploy-token"),
                ),
                patch(
                    "control_plane.dokploy.fetch_dokploy_target_payload",
                    return_value={
                        "applicationId": "application-syo-prod",
                        "name": "syo-prod-app",
                        "env": "CONTACT_EMAIL_MODE=resend\n",
                    },
                ),
                patch("control_plane.dokploy.update_dokploy_target_env") as update_env,
            ):
                with self.assertRaisesRegex(
                    control_plane_live_target_runtime.LiveTargetRuntimeError,
                    "LAUNCHPLANE_DATABASE_URL",
                ) as context:
                    control_plane_live_target_runtime.apply_live_target_runtime_environment(
                        control_plane_root=root,
                        context_name="sellyouroutboard",
                        instance_name="prod",
                        apply_changes=True,
                        deploy=False,
                        no_cache=False,
                        deploy_timeout_seconds=None,
                        deploy_trigger=(
                            control_plane_live_target_runtime.trigger_and_wait_for_dokploy_target_deploy
                        ),
                    )

        self.assertEqual(context.exception.code, "runtime_key_safety_unavailable")
        update_env.assert_not_called()

    def test_live_target_runtime_api_maps_runtime_key_safety_database_error(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/launchplane",
                            "workflow_refs": [
                                "cbusillo/launchplane/.github/workflows/live-target-runtime.yml@refs/heads/main"
                            ],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard"],
                            "actions": ["live_target_runtime.apply"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_wsgi_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/live-target-runtime.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )

            with patch(
                "control_plane.http_app.control_plane_live_target_runtime.apply_live_target_runtime_environment",
                side_effect=control_plane_live_target_runtime.LiveTargetRuntimeError(
                    "Live target runtime apply requires LAUNCHPLANE_DATABASE_URL for DB-backed runtime key-safety evaluation.",
                    code="runtime_key_safety_unavailable",
                ),
            ):
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/live-target-runtime/apply",
                    payload={
                        "schema_version": 1,
                        "mode": "apply",
                        "product": "sellyouroutboard",
                        "context": "sellyouroutboard",
                        "instance": "prod",
                    },
                    headers={"Idempotency-Key": "live-target-runtime:missing-db"},
                )

        self.assertEqual(status_code, 503)
        self.assertEqual(payload["error"]["code"], "runtime_key_safety_unavailable")
        self.assertIn("LAUNCHPLANE_DATABASE_URL", payload["error"]["message"])

    def test_live_target_runtime_api_apply_requires_apply_authorization(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/launchplane",
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard"],
                            "actions": ["live_target_runtime.plan"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_wsgi_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(repository="cbusillo/launchplane", event_name="workflow_dispatch")
                ),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/live-target-runtime/apply",
                payload={
                    "schema_version": 1,
                    "mode": "apply",
                    "product": "sellyouroutboard",
                    "context": "sellyouroutboard",
                    "instance": "prod",
                },
                headers={"Idempotency-Key": "live-target-runtime:unauthorized"},
            )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_live_target_runtime_apply_requires_database_storage(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/launchplane",
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard"],
                            "actions": ["live_target_runtime.plan"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_wsgi_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(repository="cbusillo/launchplane", event_name="workflow_dispatch")
                ),
                authz_policy=policy,
                control_plane_root_path=root,
                record_store_factory=lambda: FilesystemRecordStore(state_dir=root / "state"),
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/live-target-runtime/apply",
                payload={
                    "schema_version": 1,
                    "mode": "dry-run",
                    "product": "sellyouroutboard",
                    "context": "sellyouroutboard",
                    "instance": "prod",
                },
                headers={"Idempotency-Key": "live-target-runtime:filesystem-store"},
            )

        self.assertEqual(status_code, 503)
        self.assertEqual(payload["error"]["code"], "database_required")

    def test_openapi_includes_live_target_runtime_apply_contract(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_fastapi_wsgi_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=root,
                record_store_factory=lambda: FilesystemRecordStore(state_dir=root / "state"),
            )

            status_code, payload = _invoke_app(app, method="GET", path="/openapi.json")

        self.assertEqual(status_code, 200)
        route = payload["paths"]["/v1/live-target-runtime/apply"]["post"]
        self.assertEqual(route["operationId"], "apply_live_target_runtime")
        self.assertEqual(
            route["responses"]["202"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/AcceptedEvidenceResponse",
        )
        self.assertEqual(
            route["requestBody"]["content"]["application/json"]["schema"]["title"],
            "LiveTargetRuntimeApplyEnvelope",
        )
        for response_status in ("400", "401", "403", "409", "503"):
            self.assertIn(response_status, route["responses"])

    def test_live_target_runtime_apply_is_retired_from_legacy_wsgi_app(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate(
                    {
                        "github_actions": [
                            {
                                "repository": "cbusillo/launchplane",
                                "products": ["sellyouroutboard"],
                                "contexts": ["sellyouroutboard"],
                                "actions": ["live_target_runtime.apply"],
                            }
                        ]
                    }
                ),
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/live-target-runtime/apply",
                payload={
                    "schema_version": 1,
                    "mode": "apply",
                    "product": "sellyouroutboard",
                    "context": "sellyouroutboard",
                    "instance": "prod",
                },
                headers={"Idempotency-Key": "live-target-runtime:retired-wsgi"},
            )

        self.assertEqual(status_code, 404)
        self.assertEqual(payload["error"]["code"], "not_found")

    def test_evidence_ingress_routes_are_retired_from_legacy_wsgi_app(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=root,
            )

            for route_path in (
                "/v1/evidence/deployments",
                "/v1/evidence/backup-gates",
                "/v1/evidence/promotions",
                "/v1/evidence/previews/generations",
                "/v1/evidence/previews/destroyed",
                "/v1/evidence/runner-host-hygiene/audits",
                "/v1/evidence/runner-lane-registration/audits",
            ):
                with self.subTest(route_path=route_path):
                    status_code, payload = _invoke_app(
                        app,
                        method="POST",
                        path=route_path,
                        payload={"schema_version": 1},
                        headers={"Idempotency-Key": f"retired:{route_path}"},
                    )

                    self.assertEqual(status_code, 404)
                    self.assertEqual(payload["error"]["code"], "not_found")

    def test_odoo_stable_verification_route_is_retired(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name) / "state"
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/odoo/stable-verification",
                payload={
                    "schema_version": 1,
                    "product": "odoo-tenant-cm",
                    "verification": {
                        "schema_version": 1,
                        "context": "cm",
                        "instance": "testing",
                        "deployment_record_id": "deployment-20260420T153000Z-cm-testing",
                        "verification_status": "success",
                        "verified_at": "2026-04-20T15:35:00Z",
                        "checked_urls": ["https://cm-testing.example.com/web/health"],
                        "timeout_seconds": 45,
                    },
                },
                headers={"Idempotency-Key": "odoo-stable-verification:cm:testing:1"},
            )

            self.assertEqual(status_code, 404)
            self.assertEqual(payload["error"]["code"], "not_found")

    def test_generic_web_stable_verification_route_accepts_odoo_base_driver_profile(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_odoo_preview_profile_payload())
            )
            store.write_deployment_record(
                DeploymentRecord(
                    record_id="deployment-20260420T153000Z-cm-testing",
                    artifact_identity=ArtifactIdentityReference(
                        artifact_id="artifact-20260420-a1b2c3d4"
                    ),
                    context="cm",
                    instance="testing",
                    source_git_ref="6b3c9d7e8f901234567890abcdef1234567890ab",
                    deploy=DeploymentEvidence(
                        target_name="cm-testing",
                        target_type="compose",
                        deploy_mode="dokploy-compose-api",
                        deployment_id="delegated-compose-ship",
                        status="pass",
                    ),
                    destination_health=HealthcheckEvidence(status="pending"),
                )
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/tenant-cm",
                            "workflow_refs": [
                                "every/tenant-cm/.github/workflows/stable-smoke.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["odoo-tenant-cm"],
                            "contexts": ["cm"],
                            "actions": ["deployment.write"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="every/tenant-cm",
                        workflow_ref=(
                            "every/tenant-cm/.github/workflows/stable-smoke.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/stable-verification",
                payload={
                    "schema_version": 1,
                    "product": "odoo-tenant-cm",
                    "verification": {
                        "schema_version": 1,
                        "context": "cm",
                        "instance": "testing",
                        "deployment_record_id": "deployment-20260420T153000Z-cm-testing",
                        "verification_status": "success",
                        "verified_at": "2026-04-20T15:35:00Z",
                        "checked_urls": ["https://cm-testing.example.com/web/health"],
                        "timeout_seconds": 45,
                    },
                },
                headers={"Idempotency-Key": "generic-stable-verification:cm:testing:1"},
            )

            self.assertEqual(status_code, 202, msg=json.dumps(payload, indent=2))
            self.assertEqual(payload["status"], "accepted")
            self.assertEqual(
                payload["records"],
                {
                    "deployment_record_id": "deployment-20260420T153000Z-cm-testing",
                    "inventory_record_id": "cm-testing",
                },
            )
            deployment = store.read_deployment_record("deployment-20260420T153000Z-cm-testing")
            inventory = store.read_environment_inventory(context_name="cm", instance_name="testing")
            self.assertEqual(deployment.destination_health.status, "pass")
            self.assertEqual(
                deployment.destination_health.urls,
                ("https://cm-testing.example.com/web/health",),
            )
            self.assertEqual(inventory.deployment_record_id, deployment.record_id)

    def test_generic_web_stable_verification_records_runtime_identity_payload(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            runtime_identity = RuntimeIdentity(
                product="sellyouroutboard",
                context="sellyouroutboard-testing",
                instance="testing",
                deployment_record_id="deployment-20260420T153000Z-syo-testing",
                artifact_id="ghcr.io/every/sellyouroutboard@sha256:abc123",
                source_git_ref="6b3c9d7e8f901234567890abcdef1234567890ab",
                image_reference="ghcr.io/every/sellyouroutboard@sha256:abc123",
            )
            store.write_deployment_record(
                DeploymentRecord(
                    record_id=runtime_identity.deployment_record_id,
                    artifact_identity=ArtifactIdentityReference(
                        artifact_id=runtime_identity.artifact_id
                    ),
                    context=runtime_identity.context,
                    instance=runtime_identity.instance,
                    source_git_ref=runtime_identity.source_git_ref,
                    deploy=DeploymentEvidence(
                        target_name="sellyouroutboard-testing",
                        target_type="application",
                        deploy_mode="dokploy-application-image",
                        deployment_id="delegated-application-deploy",
                        status="pass",
                    ),
                    runtime_identity=runtime_identity,
                    destination_health=HealthcheckEvidence(status="pending"),
                )
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/stable-smoke.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["deployment.write"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/stable-smoke.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/stable-verification",
                payload={
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "verification": {
                        "schema_version": 1,
                        "context": runtime_identity.context,
                        "instance": runtime_identity.instance,
                        "deployment_record_id": runtime_identity.deployment_record_id,
                        "verification_status": "pass",
                        "verified_at": "2026-04-20T15:35:00Z",
                        "checked_urls": ["https://testing.example.com/health"],
                        "timeout_seconds": 45,
                        "health_payload": {
                            "status": "ok",
                            "version": runtime_identity.artifact_id,
                            "runtime_identity": runtime_identity.model_dump(mode="json"),
                        },
                    },
                },
                headers={"Idempotency-Key": "generic-stable-verification:syo:testing:identity"},
            )

            deployment = store.read_deployment_record(runtime_identity.deployment_record_id)

        self.assertEqual(status_code, 202, msg=json.dumps(payload, indent=2))
        self.assertEqual(deployment.destination_health.status, "pass")
        self.assertEqual(deployment.destination_health.structured_health.status, "pass")
        self.assertEqual(
            deployment.destination_health.structured_health.version,
            runtime_identity.artifact_id,
        )
        self.assertEqual(deployment.destination_health.runtime_identity_status, "match")
        self.assertEqual(deployment.destination_health.observed_runtime_identity, runtime_identity)

    def test_generic_web_stable_verification_fails_runtime_identity_mismatch(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            expected_identity = RuntimeIdentity(
                product="sellyouroutboard",
                context="sellyouroutboard-testing",
                instance="testing",
                deployment_record_id="deployment-20260420T153000Z-syo-testing",
                artifact_id="ghcr.io/every/sellyouroutboard@sha256:abc123",
                source_git_ref="6b3c9d7e8f901234567890abcdef1234567890ab",
                image_reference="ghcr.io/every/sellyouroutboard@sha256:abc123",
            )
            observed_identity = expected_identity.model_copy(
                update={"artifact_id": "ghcr.io/every/sellyouroutboard@sha256:stale"}
            )
            store.write_deployment_record(
                DeploymentRecord(
                    record_id=expected_identity.deployment_record_id,
                    artifact_identity=ArtifactIdentityReference(
                        artifact_id=expected_identity.artifact_id
                    ),
                    context=expected_identity.context,
                    instance=expected_identity.instance,
                    source_git_ref=expected_identity.source_git_ref,
                    deploy=DeploymentEvidence(
                        target_name="sellyouroutboard-testing",
                        target_type="application",
                        deploy_mode="dokploy-application-image",
                        deployment_id="delegated-application-deploy",
                        status="pass",
                    ),
                    runtime_identity=expected_identity,
                    destination_health=HealthcheckEvidence(status="pending"),
                )
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/stable-smoke.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["deployment.write"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/stable-smoke.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/stable-verification",
                payload={
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "verification": {
                        "schema_version": 1,
                        "context": expected_identity.context,
                        "instance": expected_identity.instance,
                        "deployment_record_id": expected_identity.deployment_record_id,
                        "verification_status": "pass",
                        "verified_at": "2026-04-20T15:35:00Z",
                        "checked_urls": ["https://testing.example.com/health"],
                        "timeout_seconds": 45,
                        "health_payload": {
                            "status": "ok",
                            "version": observed_identity.artifact_id,
                            "runtime_identity": observed_identity.model_dump(mode="json"),
                        },
                    },
                },
                headers={
                    "Idempotency-Key": "generic-stable-verification:syo:testing:identity-mismatch"
                },
            )

            deployment = store.read_deployment_record(expected_identity.deployment_record_id)

        self.assertEqual(status_code, 400, msg=json.dumps(payload, indent=2))
        self.assertEqual(payload["error"]["code"], "invalid_request")
        self.assertEqual(deployment.destination_health.status, "pending")

    def test_generic_web_stable_verification_fails_when_identity_payload_missing(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            expected_identity = RuntimeIdentity(
                product="sellyouroutboard",
                context="sellyouroutboard-testing",
                instance="testing",
                deployment_record_id="deployment-20260420T153000Z-syo-testing",
                artifact_id="ghcr.io/every/sellyouroutboard@sha256:abc123",
                source_git_ref="6b3c9d7e8f901234567890abcdef1234567890ab",
                image_reference="ghcr.io/every/sellyouroutboard@sha256:abc123",
            )
            store.write_deployment_record(
                DeploymentRecord(
                    record_id=expected_identity.deployment_record_id,
                    artifact_identity=ArtifactIdentityReference(
                        artifact_id=expected_identity.artifact_id
                    ),
                    context=expected_identity.context,
                    instance=expected_identity.instance,
                    source_git_ref=expected_identity.source_git_ref,
                    deploy=DeploymentEvidence(
                        target_name="sellyouroutboard-testing",
                        target_type="application",
                        deploy_mode="dokploy-application-image",
                        deployment_id="delegated-application-deploy",
                        status="pass",
                    ),
                    runtime_identity=expected_identity,
                    destination_health=HealthcheckEvidence(status="pending"),
                )
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/stable-smoke.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["deployment.write"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/stable-smoke.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/stable-verification",
                payload={
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "verification": {
                        "schema_version": 1,
                        "context": expected_identity.context,
                        "instance": expected_identity.instance,
                        "deployment_record_id": expected_identity.deployment_record_id,
                        "verification_status": "pass",
                        "verified_at": "2026-04-20T15:35:00Z",
                        "checked_urls": ["https://testing.example.com/health"],
                        "timeout_seconds": 45,
                    },
                },
                headers={
                    "Idempotency-Key": "generic-stable-verification:syo:testing:identity-missing-payload"
                },
            )

            deployment = store.read_deployment_record(expected_identity.deployment_record_id)

        self.assertEqual(status_code, 202, msg=json.dumps(payload, indent=2))
        self.assertEqual(deployment.destination_health.status, "fail")
        self.assertEqual(deployment.destination_health.runtime_identity_status, "missing")

    def test_generic_web_stable_verification_rejects_passing_payload_without_identity(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            expected_identity = RuntimeIdentity(
                product="sellyouroutboard",
                context="sellyouroutboard-testing",
                instance="testing",
                deployment_record_id="deployment-20260420T153000Z-syo-testing",
                artifact_id="ghcr.io/every/sellyouroutboard@sha256:abc123",
                source_git_ref="6b3c9d7e8f901234567890abcdef1234567890ab",
                image_reference="ghcr.io/every/sellyouroutboard@sha256:abc123",
            )
            store.write_deployment_record(
                DeploymentRecord(
                    record_id=expected_identity.deployment_record_id,
                    artifact_identity=ArtifactIdentityReference(
                        artifact_id=expected_identity.artifact_id
                    ),
                    context=expected_identity.context,
                    instance=expected_identity.instance,
                    source_git_ref=expected_identity.source_git_ref,
                    deploy=DeploymentEvidence(
                        target_name="sellyouroutboard-testing",
                        target_type="application",
                        deploy_mode="dokploy-application-image",
                        deployment_id="delegated-application-deploy",
                        status="pass",
                    ),
                    runtime_identity=expected_identity,
                    destination_health=HealthcheckEvidence(status="pending"),
                )
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/stable-smoke.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["deployment.write"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/stable-smoke.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/stable-verification",
                payload={
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "verification": {
                        "schema_version": 1,
                        "context": expected_identity.context,
                        "instance": expected_identity.instance,
                        "deployment_record_id": expected_identity.deployment_record_id,
                        "verification_status": "pass",
                        "verified_at": "2026-04-20T15:35:00Z",
                        "checked_urls": ["https://testing.example.com/health"],
                        "timeout_seconds": 45,
                        "health_payload": {"status": "ok"},
                    },
                },
                headers={
                    "Idempotency-Key": "generic-stable-verification:syo:testing:identity-missing"
                },
            )

            deployment = store.read_deployment_record(expected_identity.deployment_record_id)

        self.assertEqual(status_code, 400, msg=json.dumps(payload, indent=2))
        self.assertEqual(payload["error"]["code"], "invalid_request")
        self.assertEqual(deployment.destination_health.status, "pending")

    def test_generic_web_stable_verification_fails_structured_health_failure(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            store.write_deployment_record(
                DeploymentRecord(
                    record_id="deployment-20260420T153000Z-syo-testing",
                    artifact_identity=ArtifactIdentityReference(
                        artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:abc123"
                    ),
                    context="sellyouroutboard-testing",
                    instance="testing",
                    source_git_ref="6b3c9d7e8f901234567890abcdef1234567890ab",
                    deploy=DeploymentEvidence(
                        target_name="sellyouroutboard-testing",
                        target_type="application",
                        deploy_mode="dokploy-application-image",
                        deployment_id="delegated-application-deploy",
                        status="pass",
                    ),
                    destination_health=HealthcheckEvidence(status="pending"),
                )
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/sellyouroutboard",
                            "workflow_refs": [
                                "cbusillo/sellyouroutboard/.github/workflows/stable-smoke.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["sellyouroutboard"],
                            "contexts": ["sellyouroutboard-testing"],
                            "actions": ["deployment.write"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/sellyouroutboard",
                        workflow_ref=(
                            "cbusillo/sellyouroutboard/.github/workflows/stable-smoke.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/stable-verification",
                payload={
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "verification": {
                        "schema_version": 1,
                        "context": "sellyouroutboard-testing",
                        "instance": "testing",
                        "deployment_record_id": "deployment-20260420T153000Z-syo-testing",
                        "verification_status": "pass",
                        "verified_at": "2026-04-20T15:35:00Z",
                        "checked_urls": ["https://testing.example.com/health"],
                        "timeout_seconds": 45,
                        "health_payload": {
                            "status": "not_ready",
                            "version": "2026.04.20",
                            "summary": "last sync is stale",
                        },
                    },
                },
                headers={
                    "Idempotency-Key": "generic-stable-verification:syo:testing:structured-fail"
                },
            )

            deployment = store.read_deployment_record("deployment-20260420T153000Z-syo-testing")

        self.assertEqual(status_code, 202, msg=json.dumps(payload, indent=2))
        self.assertEqual(deployment.destination_health.status, "fail")
        self.assertEqual(deployment.destination_health.structured_health.status, "fail")
        self.assertEqual(deployment.destination_health.structured_health.version, "2026.04.20")
        self.assertEqual(
            deployment.destination_health.structured_health.detail,
            "last sync is stale",
        )

    def test_generic_web_stable_verification_requires_timeout_for_checked_urls(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_odoo_preview_profile_payload())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/tenant-cm",
                            "workflow_refs": [
                                "every/tenant-cm/.github/workflows/stable-smoke.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["odoo-tenant-cm"],
                            "contexts": ["cm"],
                            "actions": ["deployment.write"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="every/tenant-cm",
                        workflow_ref=(
                            "every/tenant-cm/.github/workflows/stable-smoke.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/stable-verification",
                payload={
                    "schema_version": 1,
                    "product": "odoo-tenant-cm",
                    "verification": {
                        "schema_version": 1,
                        "context": "cm",
                        "instance": "testing",
                        "deployment_record_id": "deployment-20260420T153000Z-cm-testing",
                        "verification_status": "pass",
                        "verified_at": "2026-04-20T15:35:00Z",
                        "checked_urls": ["https://cm-testing.example.com/web/health"],
                    },
                },
                headers={
                    "Idempotency-Key": "generic-stable-verification:cm:testing:missing-timeout"
                },
            )

        self.assertEqual(status_code, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")

    def test_generic_web_stable_verification_updates_linked_promotion_health(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_odoo_preview_profile_payload())
            )
            store.write_deployment_record(
                DeploymentRecord(
                    record_id="deployment-20260420T153000Z-cm-testing",
                    artifact_identity=ArtifactIdentityReference(
                        artifact_id="artifact-20260420-a1b2c3d4"
                    ),
                    context="cm",
                    instance="testing",
                    source_git_ref="6b3c9d7e8f901234567890abcdef1234567890ab",
                    deploy=DeploymentEvidence(
                        target_name="cm-testing",
                        target_type="compose",
                        deploy_mode="dokploy-compose-api",
                        deployment_id="delegated-compose-ship",
                        status="pass",
                    ),
                    destination_health=HealthcheckEvidence(status="pending"),
                )
            )
            store.write_promotion_record(
                PromotionRecord(
                    record_id="promotion-20260420T153500Z-cm-prod-to-testing",
                    artifact_identity=ArtifactIdentityReference(
                        artifact_id="artifact-20260420-a1b2c3d4"
                    ),
                    deployment_record_id="deployment-20260420T153000Z-cm-testing",
                    backup_record_id="backup-cm-prod-20260420T152500Z",
                    context="cm",
                    from_instance="prod",
                    to_instance="testing",
                    deploy=DeploymentEvidence(
                        target_name="cm-testing",
                        target_type="compose",
                        deploy_mode="dokploy-compose-api",
                        deployment_id="delegated-compose-promote",
                        status="pass",
                    ),
                    destination_health=HealthcheckEvidence(status="pending"),
                )
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/tenant-cm",
                            "workflow_refs": [
                                "every/tenant-cm/.github/workflows/stable-smoke.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["odoo-tenant-cm"],
                            "contexts": ["cm"],
                            "actions": ["deployment.write"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="every/tenant-cm",
                        workflow_ref=(
                            "every/tenant-cm/.github/workflows/stable-smoke.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/stable-verification",
                payload={
                    "schema_version": 1,
                    "product": "odoo-tenant-cm",
                    "verification": {
                        "schema_version": 1,
                        "context": "cm",
                        "instance": "testing",
                        "deployment_record_id": "deployment-20260420T153000Z-cm-testing",
                        "promotion_record_id": "promotion-20260420T153500Z-cm-prod-to-testing",
                        "verification_status": "fail",
                        "verified_at": "2026-04-20T15:35:00Z",
                    },
                },
                headers={"Idempotency-Key": "generic-stable-verification:cm:testing:promotion"},
            )

            deployment = store.read_deployment_record("deployment-20260420T153000Z-cm-testing")
            promotion = store.read_promotion_record("promotion-20260420T153500Z-cm-prod-to-testing")
            inventory = store.read_environment_inventory(context_name="cm", instance_name="testing")

        self.assertEqual(status_code, 202, msg=json.dumps(payload, indent=2))
        self.assertEqual(
            payload["records"]["deployment_record_id"],
            "deployment-20260420T153000Z-cm-testing",
        )
        self.assertEqual(
            payload["records"]["promotion_record_id"],
            "promotion-20260420T153500Z-cm-prod-to-testing",
        )
        self.assertEqual(payload["records"]["inventory_record_id"], "cm-testing")
        self.assertEqual(deployment.destination_health.status, "fail")
        self.assertEqual(promotion.destination_health.status, "fail")
        self.assertEqual(inventory.deployment_record_id, deployment.record_id)
        self.assertEqual(inventory.promotion_record_id, promotion.record_id)
        self.assertEqual(inventory.promoted_from_instance, "prod")

    def test_generic_web_stable_verification_evaluates_health_payload_runtime_identity(
        self,
    ) -> None:
        from control_plane.contracts.runtime_identity import RuntimeIdentity

        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_odoo_preview_profile_payload())
            )
            store.write_deployment_record(
                DeploymentRecord(
                    record_id="deployment-20260420T153000Z-cm-testing",
                    artifact_identity=ArtifactIdentityReference(
                        artifact_id="artifact-20260420-a1b2c3d4"
                    ),
                    context="cm",
                    instance="testing",
                    source_git_ref="6b3c9d7e8f901234567890abcdef1234567890ab",
                    deploy=DeploymentEvidence(
                        target_name="cm-testing",
                        target_type="compose",
                        deploy_mode="dokploy-compose-api",
                        deployment_id="delegated-compose-ship",
                        status="pass",
                    ),
                    destination_health=HealthcheckEvidence(status="pending"),
                    runtime_identity=RuntimeIdentity(
                        product="odoo-tenant-cm",
                        context="cm",
                        instance="testing",
                        environment_kind="stable",
                        deployment_record_id="deployment-20260420T153000Z-cm-testing",
                        artifact_id="artifact-20260420-a1b2c3d4",
                        source_git_ref="6b3c9d7e8f901234567890abcdef1234567890ab",
                        image_reference="artifact-20260420-a1b2c3d4",
                    ),
                )
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/tenant-cm",
                            "workflow_refs": [
                                "every/tenant-cm/.github/workflows/stable-smoke.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["odoo-tenant-cm"],
                            "contexts": ["cm"],
                            "actions": ["deployment.write"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="every/tenant-cm",
                        workflow_ref=(
                            "every/tenant-cm/.github/workflows/stable-smoke.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/stable-verification",
                payload={
                    "schema_version": 1,
                    "product": "odoo-tenant-cm",
                    "verification": {
                        "schema_version": 1,
                        "context": "cm",
                        "instance": "testing",
                        "deployment_record_id": "deployment-20260420T153000Z-cm-testing",
                        "verification_status": "pass",
                        "verified_at": "2026-04-20T15:35:00Z",
                        "health_payload": {
                            "launchplaneRuntimeIdentity": {
                                "product": "odoo-tenant-cm",
                                "context": "cm",
                                "instance": "testing",
                                "environment_kind": "stable",
                                "deployment_record_id": "deployment-20260420T153000Z-cm-testing",
                                "artifact_id": "artifact-20260420-a1b2c3d4",
                                "source_git_ref": "6b3c9d7e8f901234567890abcdef1234567890ab",
                                "image_reference": "artifact-20260420-a1b2c3d4",
                            }
                        },
                    },
                },
                headers={
                    "Idempotency-Key": "generic-stable-verification:cm:testing:health-payload"
                },
            )

            deployment = store.read_deployment_record("deployment-20260420T153000Z-cm-testing")

        self.assertEqual(status_code, 202, msg=json.dumps(payload, indent=2))
        self.assertEqual(deployment.destination_health.status, "pass")
        self.assertEqual(deployment.destination_health.runtime_identity_status, "match")

    def test_generic_web_stable_verification_rejects_mismatched_runtime_identity(
        self,
    ) -> None:
        from control_plane.contracts.runtime_identity import RuntimeIdentity

        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_odoo_preview_profile_payload())
            )
            store.write_deployment_record(
                DeploymentRecord(
                    record_id="deployment-20260420T153000Z-cm-testing",
                    artifact_identity=ArtifactIdentityReference(
                        artifact_id="artifact-20260420-a1b2c3d4"
                    ),
                    context="cm",
                    instance="testing",
                    source_git_ref="6b3c9d7e8f901234567890abcdef1234567890ab",
                    deploy=DeploymentEvidence(
                        target_name="cm-testing",
                        target_type="compose",
                        deploy_mode="dokploy-compose-api",
                        deployment_id="delegated-compose-ship",
                        status="pass",
                    ),
                    destination_health=HealthcheckEvidence(status="pending"),
                    runtime_identity=RuntimeIdentity(
                        product="odoo-tenant-cm",
                        context="cm",
                        instance="testing",
                        environment_kind="stable",
                        deployment_record_id="deployment-20260420T153000Z-cm-testing",
                        artifact_id="artifact-20260420-a1b2c3d4",
                        source_git_ref="6b3c9d7e8f901234567890abcdef1234567890ab",
                        image_reference="artifact-20260420-a1b2c3d4",
                    ),
                )
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/tenant-cm",
                            "workflow_refs": [
                                "every/tenant-cm/.github/workflows/stable-smoke.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["odoo-tenant-cm"],
                            "contexts": ["cm"],
                            "actions": ["deployment.write"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="every/tenant-cm",
                        workflow_ref=(
                            "every/tenant-cm/.github/workflows/stable-smoke.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/stable-verification",
                payload={
                    "schema_version": 1,
                    "product": "odoo-tenant-cm",
                    "verification": {
                        "schema_version": 1,
                        "context": "cm",
                        "instance": "testing",
                        "deployment_record_id": "deployment-20260420T153000Z-cm-testing",
                        "verification_status": "pass",
                        "verified_at": "2026-04-20T15:35:00Z",
                        "health_payload": {
                            "launchplaneRuntimeIdentity": {
                                "product": "odoo-tenant-cm",
                                "context": "cm",
                                "instance": "testing",
                                "environment_kind": "stable",
                                "deployment_record_id": "deployment-other",
                                "artifact_id": "artifact-20260420-a1b2c3d4",
                                "source_git_ref": "6b3c9d7e8f901234567890abcdef1234567890ab",
                                "image_reference": "artifact-20260420-a1b2c3d4",
                            }
                        },
                    },
                },
                headers={
                    "Idempotency-Key": "generic-stable-verification:cm:testing:identity-mismatch"
                },
            )

            deployment = store.read_deployment_record("deployment-20260420T153000Z-cm-testing")

        self.assertEqual(status_code, 400, msg=json.dumps(payload, indent=2))
        self.assertEqual(payload["error"]["code"], "invalid_request")
        self.assertEqual(deployment.destination_health.status, "pending")

    def test_generic_web_stable_verification_rejects_payload_without_expected_runtime_identity(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_odoo_preview_profile_payload())
            )
            store.write_deployment_record(
                DeploymentRecord(
                    record_id="deployment-20260420T153000Z-cm-testing",
                    artifact_identity=ArtifactIdentityReference(
                        artifact_id="artifact-20260420-a1b2c3d4"
                    ),
                    context="cm",
                    instance="testing",
                    source_git_ref="6b3c9d7e8f901234567890abcdef1234567890ab",
                    deploy=DeploymentEvidence(
                        target_name="cm-testing",
                        target_type="compose",
                        deploy_mode="dokploy-compose-api",
                        deployment_id="delegated-compose-ship",
                        status="pass",
                    ),
                    destination_health=HealthcheckEvidence(status="pending"),
                )
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/tenant-cm",
                            "workflow_refs": [
                                "every/tenant-cm/.github/workflows/stable-smoke.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["odoo-tenant-cm"],
                            "contexts": ["cm"],
                            "actions": ["deployment.write"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="every/tenant-cm",
                        workflow_ref=(
                            "every/tenant-cm/.github/workflows/stable-smoke.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/generic-web/stable-verification",
                payload={
                    "schema_version": 1,
                    "product": "odoo-tenant-cm",
                    "verification": {
                        "schema_version": 1,
                        "context": "cm",
                        "instance": "testing",
                        "deployment_record_id": "deployment-20260420T153000Z-cm-testing",
                        "verification_status": "pass",
                        "verified_at": "2026-04-20T15:35:00Z",
                        "health_payload": {
                            "launchplaneRuntimeIdentity": {
                                "product": "odoo-tenant-cm",
                                "context": "cm",
                                "instance": "testing",
                                "environment_kind": "stable",
                                "deployment_record_id": "deployment-20260420T153000Z-cm-testing",
                                "artifact_id": "artifact-20260420-a1b2c3d4",
                                "source_git_ref": "6b3c9d7e8f901234567890abcdef1234567890ab",
                                "image_reference": "artifact-20260420-a1b2c3d4",
                            }
                        },
                    },
                },
                headers={
                    "Idempotency-Key": "generic-stable-verification:cm:testing:no-expected-identity"
                },
            )

            deployment = store.read_deployment_record("deployment-20260420T153000Z-cm-testing")

        self.assertEqual(status_code, 400, msg=json.dumps(payload, indent=2))
        self.assertEqual(payload["error"]["code"], "invalid_request")
        self.assertEqual(deployment.destination_health.status, "pending")

    def test_verireel_testing_deploy_driver_executes_for_authorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/publish-image.yml@refs/heads/main"
                            ],
                            "event_names": ["push", "workflow_dispatch"],
                            "products": ["verireel"],
                            "contexts": ["verireel"],
                            "actions": ["verireel_testing_deploy.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/publish-image.yml@refs/heads/main"
                        ),
                        event_name="push",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.service.execute_verireel_stable_deploy",
                return_value=VeriReelStableDeployResult(
                    deployment_record_id="deployment-verireel-testing-run-12345-attempt-1",
                    deploy_status="pass",
                    deploy_started_at="2026-04-20T18:20:00Z",
                    deploy_finished_at="2026-04-20T18:21:15Z",
                    target_name="ver-testing-app",
                    target_id="testing-app-123",
                    target_category="application",
                    provider_id="dokploy",
                    provider_target_type="application",
                ),
            ) as execute_mock:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/verireel/testing-deploy",
                    payload={
                        "product": "verireel",
                        "deploy": {
                            "artifact_id": "ghcr.io/every/verireel-app:sha-abcdef1234567890",
                            "source_git_ref": "abcdef1234567890",
                        },
                    },
                    headers={"Idempotency-Key": "verireel-testing-deploy-run-12345"},
                )
                replay_status_code, replay_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/verireel/testing-deploy",
                    payload={
                        "product": "verireel",
                        "deploy": {
                            "artifact_id": "ghcr.io/every/verireel-app:sha-abcdef1234567890",
                            "source_git_ref": "abcdef1234567890",
                        },
                    },
                    headers={"Idempotency-Key": "verireel-testing-deploy-run-12345"},
                )

            self.assertEqual(status_code, 202)
            self.assertEqual(replay_status_code, 202)
            self.assertTrue(replay_payload["replayed"])
            self.assertEqual(payload["records"], replay_payload["records"])
            self.assertEqual(payload["status"], "accepted")
            self.assertEqual(
                payload["records"],
                {"deployment_record_id": "deployment-verireel-testing-run-12345-attempt-1"},
            )
            self.assertEqual(payload["result"]["deploy_status"], "pass")
            self.assertEqual(payload["result"]["target_id"], "testing-app-123")
            self.assertEqual(payload["result"]["target_category"], "application")
            self.assertEqual(payload["result"]["provider_id"], "dokploy")
            self.assertEqual(payload["result"]["provider_target_type"], "application")
            self.assertNotIn("target_type", payload["result"])
            execute_mock.assert_called_once()

    def test_verireel_testing_verification_driver_updates_deployment_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_deployment_record(
                DeploymentRecord(
                    record_id="deployment-verireel-testing-run-12345-attempt-1",
                    artifact_identity=ArtifactIdentityReference(
                        artifact_id="ghcr.io/every/verireel-app:sha-abcdef1234567890"
                    ),
                    context="verireel",
                    instance="testing",
                    source_git_ref="abcdef1234567890",
                    resolved_target=ResolvedTargetEvidence(
                        target_type="application",
                        target_id="testing-app-123",
                        target_name="ver-testing-app",
                    ),
                    deploy=DeploymentEvidence(
                        target_name="ver-testing-app",
                        target_type="application",
                        deploy_mode="dokploy-application-api",
                        deployment_id="testing-app-123",
                        status="pass",
                        started_at="2026-04-20T18:20:00Z",
                        finished_at="2026-04-20T18:21:15Z",
                    ),
                    destination_health=_passed_healthcheck_evidence(
                        "https://ver-testing.shinycomputers.com/api/health"
                    ),
                )
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/publish-image.yml@refs/heads/main"
                            ],
                            "event_names": ["push", "workflow_dispatch"],
                            "products": ["verireel"],
                            "contexts": ["verireel"],
                            "actions": ["deployment.write"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/publish-image.yml@refs/heads/main"
                        ),
                        event_name="push",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/verireel/testing-verification",
                payload={
                    "product": "verireel",
                    "verification": {
                        "deployment_record_id": "deployment-verireel-testing-run-12345-attempt-1",
                        "migration_status": "success",
                        "verification_status": "success",
                        "owner_routes_status": "success",
                    },
                },
                headers={"Idempotency-Key": "verireel-testing-verification-run-12345"},
            )
            replay_status_code, replay_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/verireel/testing-verification",
                payload={
                    "product": "verireel",
                    "verification": {
                        "deployment_record_id": "deployment-verireel-testing-run-12345-attempt-1",
                        "migration_status": "success",
                        "verification_status": "success",
                        "owner_routes_status": "success",
                    },
                },
                headers={"Idempotency-Key": "verireel-testing-verification-run-12345"},
            )

            self.assertEqual(status_code, 202)
            self.assertEqual(replay_status_code, 202)
            self.assertTrue(replay_payload["replayed"])
            self.assertEqual(payload["records"], replay_payload["records"])
            self.assertEqual(payload["status"], "accepted")
            self.assertEqual(
                payload["records"],
                {
                    "deployment_record_id": "deployment-verireel-testing-run-12345-attempt-1",
                    "inventory_record_id": "verireel-testing",
                },
            )
            deployment = store.read_deployment_record(
                "deployment-verireel-testing-run-12345-attempt-1"
            )
            inventory = store.read_environment_inventory(
                context_name="verireel",
                instance_name="testing",
            )
            self.assertEqual(deployment.post_deploy_update.status, "pass")
            self.assertEqual(deployment.destination_health.status, "pass")
            self.assertEqual(inventory.deployment_record_id, deployment.record_id)

    def test_verireel_testing_verification_driver_marks_product_check_failure(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_deployment_record(
                DeploymentRecord(
                    record_id="deployment-verireel-testing-run-12345-attempt-1",
                    artifact_identity=ArtifactIdentityReference(
                        artifact_id="ghcr.io/every/verireel-app:sha-abcdef1234567890"
                    ),
                    context="verireel",
                    instance="testing",
                    source_git_ref="abcdef1234567890",
                    deploy=DeploymentEvidence(
                        target_name="ver-testing-app",
                        target_type="application",
                        deploy_mode="dokploy-application-api",
                        deployment_id="testing-app-123",
                        status="pass",
                    ),
                    destination_health=_passed_healthcheck_evidence(
                        "https://ver-testing.shinycomputers.com/api/health"
                    ),
                )
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/publish-image.yml@refs/heads/main"
                            ],
                            "event_names": ["push", "workflow_dispatch"],
                            "products": ["verireel"],
                            "contexts": ["verireel"],
                            "actions": ["deployment.write"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/publish-image.yml@refs/heads/main"
                        ),
                        event_name="push",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, _payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/verireel/testing-verification",
                payload={
                    "product": "verireel",
                    "verification": {
                        "deployment_record_id": "deployment-verireel-testing-run-12345-attempt-1",
                        "migration_status": "success",
                        "verification_status": "failure",
                        "owner_routes_status": "success",
                    },
                },
            )

            self.assertEqual(status_code, 202)
            deployment = store.read_deployment_record(
                "deployment-verireel-testing-run-12345-attempt-1"
            )
            self.assertEqual(deployment.post_deploy_update.status, "pass")
            self.assertEqual(deployment.destination_health.status, "fail")

    def test_verireel_testing_verification_driver_rejects_unauthorized_workflow(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/publish-image.yml@refs/heads/main"
                            ],
                            "event_names": ["push", "workflow_dispatch"],
                            "products": ["verireel"],
                            "contexts": ["other-context"],
                            "actions": ["deployment.write"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/publish-image.yml@refs/heads/main"
                        ),
                        event_name="push",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/verireel/testing-verification",
                payload={
                    "product": "verireel",
                    "verification": {
                        "deployment_record_id": "deployment-verireel-testing-run-12345-attempt-1",
                        "migration_status": "success",
                        "verification_status": "success",
                        "owner_routes_status": "success",
                    },
                },
            )

            self.assertEqual(status_code, 403)
            self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_verireel_testing_deploy_driver_rejects_unauthorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/publish-image.yml@refs/heads/main"
                            ],
                            "event_names": ["push", "workflow_dispatch"],
                            "products": ["verireel"],
                            "contexts": ["verireel"],
                            "actions": ["deployment.write"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/publish-image.yml@refs/heads/main"
                        ),
                        event_name="push",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/verireel/testing-deploy",
                payload={
                    "product": "verireel",
                    "deploy": {
                        "artifact_id": "ghcr.io/every/verireel-app:sha-abcdef1234567890",
                        "source_git_ref": "abcdef1234567890",
                    },
                },
            )

            self.assertEqual(status_code, 403)
            self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_verireel_stable_environment_driver_executes_for_authorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/publish-image.yml@refs/heads/main"
                            ],
                            "event_names": ["push", "workflow_dispatch"],
                            "products": ["verireel"],
                            "contexts": ["verireel"],
                            "actions": ["verireel_stable_environment.read"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/publish-image.yml@refs/heads/main"
                        ),
                        event_name="push",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.service.resolve_verireel_stable_environment",
                return_value=VeriReelStableEnvironmentResult(
                    context="verireel",
                    instance="testing",
                    target_name="ver-testing-app",
                    target_type="application",
                    target_id="testing-app-123",
                    base_urls=("https://ver-testing.shinycomputers.com",),
                    primary_base_url="https://ver-testing.shinycomputers.com",
                    healthcheck_path="/api/health",
                    health_urls=("https://ver-testing.shinycomputers.com/api/health",),
                ),
            ) as resolve_mock:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/verireel/stable-environment",
                    payload={
                        "product": "verireel",
                        "environment": {"context": "verireel", "instance": "testing"},
                    },
                    headers={"Idempotency-Key": "verireel-stable-environment-read"},
                )
                replay_status_code, replay_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/verireel/stable-environment",
                    payload={
                        "product": "verireel",
                        "environment": {"context": "verireel", "instance": "testing"},
                    },
                    headers={"Idempotency-Key": "verireel-stable-environment-read"},
                )

            self.assertEqual(status_code, 202)
            self.assertEqual(replay_status_code, 202)
            self.assertNotIn("replayed", replay_payload)
            self.assertEqual(payload["status"], "accepted")
            self.assertEqual(payload["result"]["target_name"], "ver-testing-app")
            self.assertEqual(
                payload["result"]["primary_base_url"], "https://ver-testing.shinycomputers.com"
            )
            self.assertEqual(resolve_mock.call_count, 2)

    def test_verireel_runtime_verification_driver_executes_for_authorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/publish-image.yml@refs/heads/main"
                            ],
                            "event_names": ["push", "workflow_dispatch"],
                            "products": ["verireel"],
                            "contexts": ["verireel"],
                            "actions": ["verireel_stable_environment.read"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/publish-image.yml@refs/heads/main"
                        ),
                        event_name="push",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.service.execute_verireel_rollout_verification",
                return_value=VeriReelRolloutVerificationResult(
                    status="pass",
                    base_url="https://ver-testing.shinycomputers.com",
                    health_urls=("https://ver-testing.shinycomputers.com/api/health",),
                    started_at="2026-05-01T12:00:00Z",
                    finished_at="2026-05-01T12:00:05Z",
                ),
            ) as verify_mock:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/verireel/runtime-verification",
                    payload={
                        "product": "verireel",
                        "verification": {"context": "verireel", "instance": "testing"},
                    },
                    headers={"Idempotency-Key": "verireel-runtime-verification-read"},
                )
                replay_status_code, replay_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/verireel/runtime-verification",
                    payload={
                        "product": "verireel",
                        "verification": {"context": "verireel", "instance": "testing"},
                    },
                    headers={"Idempotency-Key": "verireel-runtime-verification-read"},
                )

            self.assertEqual(status_code, 202)
            self.assertEqual(replay_status_code, 202)
            self.assertNotIn("replayed", replay_payload)
            self.assertEqual(payload["status"], "accepted")
            self.assertEqual(payload["result"]["status"], "pass")
            self.assertEqual(
                payload["result"]["base_url"], "https://ver-testing.shinycomputers.com"
            )
            self.assertEqual(verify_mock.call_count, 2)

    def test_verireel_stable_read_drivers_reject_unauthorized_workflow(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/publish-image.yml@refs/heads/main"
                            ],
                            "event_names": ["push", "workflow_dispatch"],
                            "products": ["verireel"],
                            "contexts": ["other-context"],
                            "actions": ["verireel_stable_environment.read"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/publish-image.yml@refs/heads/main"
                        ),
                        event_name="push",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with (
                patch("control_plane.service.resolve_verireel_stable_environment") as resolve_mock,
                patch("control_plane.service.execute_verireel_rollout_verification") as verify_mock,
            ):
                environment_status_code, environment_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/verireel/stable-environment",
                    payload={
                        "product": "verireel",
                        "environment": {"context": "verireel", "instance": "testing"},
                    },
                )
                runtime_status_code, runtime_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/verireel/runtime-verification",
                    payload={
                        "product": "verireel",
                        "verification": {"context": "verireel", "instance": "testing"},
                    },
                )

            self.assertEqual(environment_status_code, 403)
            self.assertEqual(environment_payload["error"]["code"], "authorization_denied")
            self.assertEqual(runtime_status_code, 403)
            self.assertEqual(runtime_payload["error"]["code"], "authorization_denied")
            resolve_mock.assert_not_called()
            verify_mock.assert_not_called()

    def test_verireel_app_maintenance_driver_executes_for_authorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/publish-image.yml@refs/heads/main"
                            ],
                            "event_names": ["push", "workflow_dispatch"],
                            "products": ["verireel"],
                            "contexts": ["verireel"],
                            "actions": ["verireel_app_maintenance.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/publish-image.yml@refs/heads/main"
                        ),
                        event_name="push",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.service.execute_verireel_app_maintenance",
                return_value=VeriReelAppMaintenanceResult(
                    maintenance_status="pass",
                    action="migrate",
                    intent="stable-testing-migration",
                    context="verireel",
                    instance="testing",
                    application_name="ver-testing-app",
                    application_id="testing-app-123",
                    schedule_name="ver-apply-prisma-migrations",
                    started_at="2026-04-25T19:00:00Z",
                    finished_at="2026-04-25T19:01:00Z",
                ),
            ) as execute_mock:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/verireel/app-maintenance",
                    payload={
                        "product": "verireel",
                        "maintenance": {
                            "context": "verireel",
                            "instance": "testing",
                            "action": "migrate",
                            "intent": "stable-testing-migration",
                        },
                    },
                    headers={"Idempotency-Key": "verireel-app-maintenance-migrate"},
                )
                replay_status_code, replay_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/verireel/app-maintenance",
                    payload={
                        "product": "verireel",
                        "maintenance": {
                            "context": "verireel",
                            "instance": "testing",
                            "action": "migrate",
                            "intent": "stable-testing-migration",
                        },
                    },
                    headers={"Idempotency-Key": "verireel-app-maintenance-migrate"},
                )

            self.assertEqual(status_code, 202)
            self.assertEqual(payload["status"], "accepted")
            self.assertEqual(payload["result"]["maintenance_status"], "pass")
            self.assertEqual(payload["result"]["application_id"], "testing-app-123")
            self.assertEqual(replay_status_code, 202)
            self.assertEqual(replay_payload["status"], "accepted")
            self.assertTrue(replay_payload["replayed"])
            self.assertEqual(replay_payload["result"], payload["result"])
            execute_mock.assert_called_once()

    def test_verireel_app_maintenance_driver_accepts_stable_e2e_grant_intent(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/publish-image.yml@refs/heads/main"
                            ],
                            "event_names": ["push", "workflow_dispatch"],
                            "products": ["verireel"],
                            "contexts": ["verireel"],
                            "actions": ["verireel_app_maintenance.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/publish-image.yml@refs/heads/main"
                        ),
                        event_name="push",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.service.execute_verireel_app_maintenance",
                return_value=VeriReelAppMaintenanceResult(
                    maintenance_status="pass",
                    action="grant-sponsored",
                    intent="stable-testing-remote-e2e-grant-sponsored",
                    context="verireel",
                    instance="testing",
                    application_name="ver-testing-app",
                    application_id="testing-app-123",
                    schedule_name="ver-remote-e2e-grant-sponsored",
                    started_at="2026-04-25T19:00:00Z",
                    finished_at="2026-04-25T19:01:00Z",
                ),
            ) as execute_mock:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/verireel/app-maintenance",
                    payload={
                        "product": "verireel",
                        "maintenance": {
                            "context": "verireel",
                            "instance": "testing",
                            "action": "grant-sponsored",
                            "intent": "stable-testing-remote-e2e-grant-sponsored",
                            "email": "creator@example.com",
                        },
                    },
                )

            self.assertEqual(status_code, 202)
            self.assertEqual(payload["status"], "accepted")
            self.assertEqual(payload["result"]["maintenance_status"], "pass")
            execute_mock.assert_called_once()

    def test_verireel_app_maintenance_driver_rejects_unauthorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/publish-image.yml@refs/heads/main"
                            ],
                            "event_names": ["push", "workflow_dispatch"],
                            "products": ["verireel"],
                            "contexts": ["verireel"],
                            "actions": ["verireel_testing_deploy.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/publish-image.yml@refs/heads/main"
                        ),
                        event_name="push",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/verireel/app-maintenance",
                payload={
                    "product": "verireel",
                    "maintenance": {
                        "context": "verireel",
                        "instance": "testing",
                        "action": "migrate",
                        "intent": "stable-testing-migration",
                    },
                },
            )

            self.assertEqual(status_code, 403)
            self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_verireel_app_maintenance_driver_rejects_action_only_payload(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/publish-image.yml@refs/heads/main"
                            ],
                            "event_names": ["push", "workflow_dispatch"],
                            "products": ["verireel"],
                            "contexts": ["verireel"],
                            "actions": ["verireel_app_maintenance.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/publish-image.yml@refs/heads/main"
                        ),
                        event_name="push",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/verireel/app-maintenance",
                payload={
                    "product": "verireel",
                    "maintenance": {
                        "context": "verireel",
                        "instance": "testing",
                        "action": "migrate",
                    },
                },
            )

            self.assertEqual(status_code, 400)
            self.assertEqual(payload["error"]["code"], "invalid_request")
            self.assertEqual(payload["error"]["message"], "Request payload failed validation.")

    def test_verireel_preview_inventory_driver_executes_for_authorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-janitor.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["verireel"],
                            "contexts": ["verireel-testing"],
                            "actions": ["verireel_preview_inventory.read"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/preview-janitor.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.service.execute_verireel_preview_inventory",
                return_value=VeriReelPreviewInventoryResult(
                    context="verireel-testing",
                    previews=(
                        VeriReelPreviewInventoryItem(
                            applicationId="app-42",
                            applicationName="ver-preview-pr-42-app",
                            previewSlug="pr-42",
                        ),
                    ),
                ),
            ) as execute_mock:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/verireel/preview-inventory",
                    payload={
                        "product": "verireel",
                        "inventory": {"context": "verireel-testing"},
                    },
                )

            self.assertEqual(status_code, 202)
            self.assertEqual(payload["status"], "accepted")
            self.assertEqual(payload["result"]["previews"][0]["previewSlug"], "pr-42")
            execute_mock.assert_called_once()
            scan_records = FilesystemRecordStore(
                state_dir=root / "state"
            ).list_preview_inventory_scan_records(context_name="verireel-testing")

            self.assertEqual(len(scan_records), 1)
            self.assertEqual(scan_records[0].preview_count, 1)
            self.assertEqual(scan_records[0].preview_slugs, ("pr-42",))

    def test_verireel_preview_inventory_driver_does_not_replay_cached_inventory(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-janitor.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["verireel"],
                            "contexts": ["verireel-testing"],
                            "actions": ["verireel_preview_inventory.read"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/preview-janitor.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )
            request_payload: dict[str, object] = {
                "product": "verireel",
                "inventory": {"context": "verireel-testing"},
            }
            idempotency_key = "verireel-preview-inventory:verireel-testing"
            FilesystemRecordStore(state_dir=root / "state").write_idempotency_record(
                LaunchplaneIdempotencyRecord(
                    record_id="idempotency-stale-verireel-preview-inventory",
                    scope="|".join(
                        (
                            "every/verireel",
                            "every/verireel/.github/workflows/preview-janitor.yml@refs/heads/main",
                            "repo:every/verireel:pull_request",
                        )
                    ),
                    route_path="/v1/drivers/verireel/preview-inventory",
                    idempotency_key=idempotency_key,
                    request_fingerprint=control_plane_service._idempotency_request_fingerprint(
                        route_path="/v1/drivers/verireel/preview-inventory",
                        payload=request_payload,
                    ),
                    response_status_code=202,
                    response_trace_id="stale-verireel-preview-inventory",
                    recorded_at="2026-04-29T19:22:00Z",
                    response_payload={
                        "status": "accepted",
                        "trace_id": "stale-verireel-preview-inventory",
                        "records": {"preview_inventory_scan_id": "stale-scan"},
                        "result": {
                            "context": "verireel-testing",
                            "previews": [
                                {
                                    "applicationId": "stale-app",
                                    "applicationName": "stale-preview-app",
                                    "previewSlug": "stale",
                                }
                            ],
                        },
                    },
                )
            )

            with patch(
                "control_plane.service.execute_verireel_preview_inventory",
                side_effect=[
                    VeriReelPreviewInventoryResult(
                        context="verireel-testing",
                        previews=(
                            VeriReelPreviewInventoryItem(
                                applicationId="app-93",
                                applicationName="ver-preview-pr-93-app",
                                previewSlug="pr-93",
                            ),
                        ),
                    ),
                    VeriReelPreviewInventoryResult(context="verireel-testing", previews=()),
                ],
            ) as execute_mock:
                first_status_code, first_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/verireel/preview-inventory",
                    payload=request_payload,
                    headers={"Idempotency-Key": idempotency_key},
                )
                second_status_code, second_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/verireel/preview-inventory",
                    payload=request_payload,
                    headers={"Idempotency-Key": idempotency_key},
                )

            self.assertEqual(first_status_code, 202)
            self.assertEqual(second_status_code, 202)
            self.assertEqual(first_payload["result"]["previews"][0]["previewSlug"], "pr-93")
            self.assertEqual(second_payload["result"]["previews"], [])
            self.assertNotIn("replayed", first_payload)
            self.assertNotIn("replayed", second_payload)
            self.assertEqual(execute_mock.call_count, 2)

    def test_preview_lifecycle_plan_endpoint_records_report_only_plan(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_preview_inventory_scan_record(
                PreviewInventoryScanRecord(
                    scan_id="preview-inventory-scan-verireel-testing-20260429T192315Z",
                    context="verireel-testing",
                    scanned_at="2026-04-29T19:23:15Z",
                    source="verireel-preview-inventory",
                    status="pass",
                    preview_count=2,
                    preview_slugs=("pr-41", "pr-42"),
                )
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-janitor.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["verireel"],
                            "contexts": ["verireel-testing"],
                            "actions": ["preview_lifecycle.plan"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_wsgi_app(
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/preview-janitor.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
                record_store_factory=lambda: FilesystemRecordStore(state_dir=root / "state"),
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/previews/lifecycle-plan",
                payload={
                    "product": "verireel",
                    "context": "verireel-testing",
                    "source": "preview-janitor",
                    "desired_state_id": "preview-desired-state-verireel-testing-20260429T192314Z",
                    "desired_previews": [
                        {
                            "preview_slug": "pr-42",
                            "anchor_repo": "verireel",
                            "anchor_pr_number": 42,
                        },
                        {
                            "preview_slug": "pr-43",
                            "anchor_repo": "verireel",
                            "anchor_pr_number": 43,
                        },
                    ],
                },
            )

            plan_records = FilesystemRecordStore(
                state_dir=root / "state"
            ).list_preview_lifecycle_plan_records(context_name="verireel-testing")

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(payload["records"]["preview_lifecycle_plan_id"], plan_records[0].plan_id)
        self.assertEqual(
            payload["result"]["inventory_scan_id"],
            "preview-inventory-scan-verireel-testing-20260429T192315Z",
        )
        self.assertEqual(payload["result"]["keep_slugs"], ["pr-42"])
        self.assertEqual(
            payload["result"]["desired_state_id"],
            "preview-desired-state-verireel-testing-20260429T192314Z",
        )
        self.assertEqual(payload["result"]["orphaned_slugs"], ["pr-41"])
        self.assertEqual(payload["result"]["missing_slugs"], ["pr-43"])
        self.assertEqual(len(plan_records), 1)
        self.assertEqual(plan_records[0].source, "preview-janitor")

    def test_preview_lifecycle_plan_endpoint_rejects_unauthorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-janitor.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["verireel"],
                            "contexts": ["verireel-testing"],
                            "actions": ["verireel_preview_inventory.read"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_wsgi_app(
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/preview-janitor.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
                record_store_factory=lambda: FilesystemRecordStore(state_dir=root / "state"),
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/previews/lifecycle-plan",
                payload={
                    "product": "verireel",
                    "context": "verireel-testing",
                    "desired_previews": [{"preview_slug": "pr-42"}],
                },
            )

        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_preview_lifecycle_plan_endpoint_records_missing_inventory(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-janitor.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["verireel"],
                            "contexts": ["verireel-testing"],
                            "actions": ["preview_lifecycle.plan"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_wsgi_app(
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/preview-janitor.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
                record_store_factory=lambda: FilesystemRecordStore(state_dir=root / "state"),
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/previews/lifecycle-plan",
                payload={
                    "product": "verireel",
                    "context": "verireel-testing",
                    "desired_previews": [{"preview_slug": "pr-42"}],
                },
            )

        self.assertEqual(status_code, 202)
        self.assertEqual(payload["result"]["status"], "missing_inventory")
        self.assertEqual(payload["result"]["orphaned_slugs"], [])
        self.assertIn("has not recorded", payload["result"]["error_message"])

    def test_preview_lifecycle_plan_endpoint_replays_idempotent_request(self) -> None:
        request_payload = {
            "product": "verireel",
            "context": "verireel-testing",
            "source": "preview-janitor",
            "desired_previews": [{"preview_slug": "pr-42"}],
        }
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-janitor.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["verireel"],
                            "contexts": ["verireel-testing"],
                            "actions": ["preview_lifecycle.plan"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_wsgi_app(
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/preview-janitor.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
                record_store_factory=lambda: FilesystemRecordStore(state_dir=root / "state"),
            )

            first_status, first_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/previews/lifecycle-plan",
                payload=request_payload,
                headers={"Idempotency-Key": "preview-lifecycle-plan:42"},
            )
            replay_status, replay_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/previews/lifecycle-plan",
                payload=request_payload,
                headers={"Idempotency-Key": "preview-lifecycle-plan:42"},
            )
            plan_records = FilesystemRecordStore(
                state_dir=root / "state"
            ).list_preview_lifecycle_plan_records(context_name="verireel-testing")

        self.assertEqual(first_status, 202)
        self.assertEqual(replay_status, 202)
        self.assertTrue(replay_payload["replayed"])
        self.assertEqual(
            replay_payload["records"]["preview_lifecycle_plan_id"],
            first_payload["records"]["preview_lifecycle_plan_id"],
        )
        self.assertEqual(replay_payload["result"], first_payload["result"])
        self.assertEqual(len(plan_records), 1)

    def test_preview_lifecycle_plan_endpoint_rejects_idempotency_key_reuse(
        self,
    ) -> None:
        request_payload = {
            "product": "verireel",
            "context": "verireel-testing",
            "source": "preview-janitor",
            "desired_previews": [{"preview_slug": "pr-42"}],
        }
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-janitor.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["verireel"],
                            "contexts": ["verireel-testing"],
                            "actions": ["preview_lifecycle.plan"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_wsgi_app(
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/preview-janitor.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
                record_store_factory=lambda: FilesystemRecordStore(state_dir=root / "state"),
            )

            first_status, _first_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/previews/lifecycle-plan",
                payload=request_payload,
                headers={"Idempotency-Key": "preview-lifecycle-plan:reuse"},
            )
            conflict_status, conflict_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/previews/lifecycle-plan",
                payload={
                    **request_payload,
                    "desired_previews": [{"preview_slug": "pr-43"}],
                },
                headers={"Idempotency-Key": "preview-lifecycle-plan:reuse"},
            )

        self.assertEqual(first_status, 202)
        self.assertEqual(conflict_status, 409)
        self.assertEqual(conflict_payload["error"]["code"], "idempotency_key_reused")

    def test_preview_lifecycle_plan_endpoint_requires_plan_store(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-janitor.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["verireel"],
                            "contexts": ["verireel-testing"],
                            "actions": ["preview_lifecycle.plan"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_wsgi_app(
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/preview-janitor.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
                record_store_factory=object,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/previews/lifecycle-plan",
                payload={
                    "product": "verireel",
                    "context": "verireel-testing",
                    "desired_previews": [{"preview_slug": "pr-42"}],
                },
            )

        self.assertEqual(status_code, 503)
        self.assertEqual(payload["error"]["code"], "database_storage_required")
        self.assertIn("preview lifecycle plan applies", payload["error"]["message"])

    def test_preview_desired_state_endpoint_legacy_wsgi_route_is_retired(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity(repository="every/launchplane")),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({"github_actions": []}),
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/previews/desired-state",
                payload={
                    "product": "verireel",
                    "context": "verireel-testing",
                    "repository": "every/verireel",
                    "anchor_repo": "verireel",
                },
            )

        self.assertEqual(status_code, 404)
        self.assertEqual(payload["error"]["code"], "not_found")

    def test_preview_pr_feedback_endpoint_legacy_wsgi_route_is_retired(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/preview-control-plane.yml@refs/pull/42/merge"
                        ),
                        event_name="pull_request",
                    )
                ),
                authz_policy=LaunchplaneAuthzPolicy(),
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/previews/pr-feedback",
                payload={
                    "product": "verireel",
                    "context": "verireel-testing",
                    "source": "preview-control-plane",
                    "repository": "every/verireel",
                    "anchor_repo": "verireel",
                    "anchor_pr_number": 42,
                    "anchor_pr_url": "https://github.com/every/verireel/pull/42",
                    "status": "ready",
                },
            )

        self.assertEqual(status_code, 404)
        self.assertEqual(payload["error"]["code"], "not_found")

    def test_verireel_prod_deploy_driver_executes_for_authorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/promote-image.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["verireel"],
                            "contexts": ["verireel"],
                            "actions": ["verireel_prod_deploy.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/promote-image.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.service.execute_verireel_stable_deploy",
                return_value=VeriReelStableDeployResult(
                    deployment_record_id="deployment-verireel-prod-run-12345-attempt-1",
                    deploy_status="pass",
                    deploy_started_at="2026-04-20T19:20:00Z",
                    deploy_finished_at="2026-04-20T19:21:15Z",
                    target_name="ver-prod-app",
                    target_id="prod-app-123",
                    target_category="application",
                    provider_id="dokploy",
                    provider_target_type="application",
                ),
            ) as execute_mock:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/verireel/prod-deploy",
                    payload={
                        "product": "verireel",
                        "deploy": {
                            "instance": "prod",
                            "artifact_id": "ghcr.io/every/verireel-app:sha-abcdef1234567890",
                            "source_git_ref": "abcdef1234567890",
                        },
                    },
                    headers={"Idempotency-Key": "verireel-prod-deploy-run-12345"},
                )
                replay_status_code, replay_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/verireel/prod-deploy",
                    payload={
                        "product": "verireel",
                        "deploy": {
                            "instance": "prod",
                            "artifact_id": "ghcr.io/every/verireel-app:sha-abcdef1234567890",
                            "source_git_ref": "abcdef1234567890",
                        },
                    },
                    headers={"Idempotency-Key": "verireel-prod-deploy-run-12345"},
                )

            self.assertEqual(status_code, 202)
            self.assertEqual(replay_status_code, 202)
            self.assertTrue(replay_payload["replayed"])
            self.assertEqual(payload["records"], replay_payload["records"])
            self.assertEqual(payload["status"], "accepted")
            self.assertEqual(
                payload["records"],
                {"deployment_record_id": "deployment-verireel-prod-run-12345-attempt-1"},
            )
            self.assertEqual(payload["result"]["deploy_status"], "pass")
            self.assertEqual(payload["result"]["target_id"], "prod-app-123")
            self.assertEqual(payload["result"]["target_category"], "application")
            self.assertEqual(payload["result"]["provider_id"], "dokploy")
            self.assertEqual(payload["result"]["provider_target_type"], "application")
            self.assertNotIn("target_type", payload["result"])
            execute_mock.assert_called_once()

    def test_verireel_prod_deploy_driver_rejects_unauthorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/promote-image.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["verireel"],
                            "contexts": ["verireel"],
                            "actions": ["promotion.write"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/promote-image.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/verireel/prod-deploy",
                payload={
                    "product": "verireel",
                    "deploy": {
                        "instance": "prod",
                        "artifact_id": "ghcr.io/every/verireel-app:sha-abcdef1234567890",
                        "source_git_ref": "abcdef1234567890",
                    },
                },
            )

            self.assertEqual(status_code, 403)
            self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_verireel_prod_promotion_driver_executes_for_authorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/promote-image.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["verireel"],
                            "contexts": ["verireel"],
                            "actions": ["verireel_prod_promotion.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/promote-image.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.service.execute_verireel_prod_promotion",
                return_value=VeriReelProdPromotionResult(
                    promotion_record_id="promotion-verireel-testing-to-prod-run-12345-attempt-1",
                    deployment_record_id="deployment-verireel-prod-run-12345-attempt-1",
                    backup_record_id="backup-gate-verireel-prod-run-12345-attempt-1",
                    deploy_status="pass",
                    deploy_started_at="2026-04-21T18:20:00Z",
                    deploy_finished_at="2026-04-21T18:21:15Z",
                    target_name="ver-prod-app",
                    target_id="prod-app-123",
                    target_category="application",
                    provider_id="dokploy",
                    provider_target_type="application",
                ),
            ) as execute_mock:
                promotion_payload = {
                    "product": "verireel",
                    "promotion": {
                        "artifact_id": "ghcr.io/every/verireel-app:sha-abcdef1234567890",
                        "source_git_ref": "abcdef1234567890",
                        "backup_record_id": "backup-gate-verireel-prod-run-12345-attempt-1",
                        "promotion_record_id": "promotion-verireel-testing-to-prod-run-12345-attempt-1",
                        "source_health_status": "success",
                    },
                }
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/verireel/prod-promotion",
                    payload=promotion_payload,
                    headers={"Idempotency-Key": "verireel-prod-promotion-run-12345"},
                )
                replay_status_code, replay_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/verireel/prod-promotion",
                    payload=promotion_payload,
                    headers={"Idempotency-Key": "verireel-prod-promotion-run-12345"},
                )

            self.assertEqual(status_code, 202)
            self.assertEqual(replay_status_code, 202)
            self.assertEqual(replay_payload["status"], "accepted")
            self.assertTrue(replay_payload["replayed"])
            self.assertEqual(replay_payload["original_trace_id"], payload["trace_id"])
            self.assertEqual(replay_payload["records"], payload["records"])
            self.assertEqual(replay_payload["result"], payload["result"])
            self.assertEqual(payload["status"], "accepted")
            self.assertEqual(
                payload["records"],
                {
                    "promotion_record_id": "promotion-verireel-testing-to-prod-run-12345-attempt-1",
                    "deployment_record_id": "deployment-verireel-prod-run-12345-attempt-1",
                },
            )
            self.assertEqual(payload["result"]["deploy_status"], "pass")
            self.assertEqual(
                payload["result"]["promotion_record_id"],
                "promotion-verireel-testing-to-prod-run-12345-attempt-1",
            )
            self.assertEqual(payload["result"]["target_category"], "application")
            self.assertEqual(payload["result"]["provider_id"], "dokploy")
            self.assertEqual(payload["result"]["provider_target_type"], "application")
            self.assertNotIn("target_type", payload["result"])
            execute_mock.assert_called_once()
            request = execute_mock.call_args.kwargs["request"]
            self.assertEqual(request.source_health_status, "pass")

    def test_verireel_prod_promotion_route_accepts_product_profile_driver_id(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            profile_payload = _generic_site_profile_payload(product="video-site")
            profile_payload["display_name"] = "Video Site"
            profile_payload["driver_id"] = "verireel"
            profile_payload["preview"] = {
                "enabled": False,
                "context": "",
                "slug_template": "pr-{number}",
            }
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(profile_payload)
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/video-site",
                            "workflow_refs": [
                                "every/video-site/.github/workflows/promote-image.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["video-site"],
                            "contexts": ["video-site"],
                            "actions": ["verireel_prod_promotion.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="every/video-site",
                        workflow_ref=(
                            "every/video-site/.github/workflows/promote-image.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.service.execute_verireel_prod_promotion",
                return_value=VeriReelProdPromotionResult(
                    promotion_record_id="promotion-video-testing-to-prod-run-12345-attempt-1",
                    deployment_record_id="deployment-video-prod-run-12345-attempt-1",
                    backup_record_id="backup-gate-video-prod-run-12345-attempt-1",
                    deploy_status="pass",
                    deploy_started_at="2026-04-21T18:20:00Z",
                    deploy_finished_at="2026-04-21T18:21:15Z",
                    target_name="video-prod-app",
                    target_id="prod-app-123",
                    target_category="application",
                    provider_id="dokploy",
                    provider_target_type="application",
                ),
            ) as execute_mock:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/verireel/prod-promotion",
                    payload={
                        "product": "video-site",
                        "promotion": {
                            "context": "video-site",
                            "artifact_id": "ghcr.io/every/video-site:sha-abcdef1234567890",
                            "source_git_ref": "abcdef1234567890",
                            "backup_record_id": "backup-gate-video-prod-run-12345-attempt-1",
                            "promotion_record_id": "promotion-video-testing-to-prod-run-12345-attempt-1",
                        },
                    },
                )

            self.assertEqual(status_code, 202)
            self.assertEqual(
                payload["records"]["promotion_record_id"],
                "promotion-video-testing-to-prod-run-12345-attempt-1",
            )
            execute_mock.assert_called_once()

    def test_verireel_prod_promotion_route_rejects_unowned_target_lane(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            profile_payload = _generic_site_profile_payload(product="video-site")
            profile_payload["display_name"] = "Video Site"
            profile_payload["driver_id"] = "verireel"
            profile_payload["lanes"] = (
                {
                    "instance": "testing",
                    "context": "video-site",
                    "base_url": "https://testing.video.example",
                    "health_url": "https://testing.video.example/healthz",
                },
            )
            profile_payload["preview"] = {
                "enabled": False,
                "context": "",
                "slug_template": "pr-{number}",
            }
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(profile_payload)
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/video-site",
                            "workflow_refs": [
                                "every/video-site/.github/workflows/promote-image.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["video-site"],
                            "contexts": ["video-site"],
                            "actions": ["verireel_prod_promotion.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="every/video-site",
                        workflow_ref=(
                            "every/video-site/.github/workflows/promote-image.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch("control_plane.service.execute_verireel_prod_promotion") as execute_mock:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/verireel/prod-promotion",
                    payload={
                        "product": "video-site",
                        "promotion": {
                            "context": "video-site",
                            "artifact_id": "ghcr.io/every/video-site:sha-abcdef1234567890",
                            "source_git_ref": "abcdef1234567890",
                            "backup_record_id": "backup-gate-video-prod-run-12345-attempt-1",
                            "promotion_record_id": "promotion-video-testing-to-prod-run-12345-attempt-1",
                        },
                    },
                )

            self.assertEqual(status_code, 403)
            self.assertEqual(payload["error"]["code"], "product_driver_mismatch")
            execute_mock.assert_not_called()

    def test_odoo_target_replacement_plan_driver_reads_for_authorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_odoo_preview_profile_payload())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/launchplane",
                            "workflow_refs": [
                                "cbusillo/launchplane/.github/workflows/odoo-target-replacement-plan.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["odoo-tenant-cm"],
                            "contexts": ["cm"],
                            "actions": ["odoo_target_replacement_plan.read"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/odoo-target-replacement-plan.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.service.build_odoo_stable_target_replacement_plan",
                return_value=OdooStableTargetReplacementPlan(
                    plan_status="ready",
                    product="odoo-tenant-cm",
                    context="cm",
                    instance="testing",
                    strategy="recreate-in-place",
                    target_record_found=True,
                    target_id_record_found=True,
                    inventory_found=True,
                    expected_next_target_name="cm-testing",
                ),
            ) as plan_mock:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/odoo/target-replacement-plan",
                    payload={
                        "product": "odoo-tenant-cm",
                        "replacement": {
                            "product": "odoo-tenant-cm",
                            "instance": "testing",
                            "strategy": "recreate-in-place",
                            "allow_empty_data": True,
                        },
                    },
                )

            self.assertEqual(status_code, 202)
            self.assertEqual(payload["status"], "accepted")
            self.assertEqual(payload["result"]["plan_status"], "ready")
            self.assertEqual(payload["result"]["context"], "cm")
            plan_mock.assert_called_once()
            request = plan_mock.call_args.kwargs["request"]
            self.assertTrue(request.allow_empty_data)

    def test_odoo_target_replacement_plan_driver_rejects_unauthorized_workflow(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_odoo_preview_profile_payload())
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/odoo-target-replacement-plan.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=LaunchplaneAuthzPolicy.model_validate(
                    {
                        "github_actions": [
                            {
                                "repository": "cbusillo/launchplane",
                                "workflow_refs": [
                                    "cbusillo/launchplane/.github/workflows/odoo-target-replacement-plan.yml@refs/heads/main"
                                ],
                                "event_names": ["workflow_dispatch"],
                                "products": ["odoo-tenant-cm"],
                                "contexts": ["cm"],
                                "actions": ["deployment.write"],
                            }
                        ]
                    }
                ),
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/odoo/target-replacement-plan",
                payload={
                    "product": "odoo-tenant-cm",
                    "replacement": {
                        "product": "odoo-tenant-cm",
                        "instance": "testing",
                    },
                },
            )

            self.assertEqual(status_code, 403)
            self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_odoo_target_replacement_plan_recomputes_with_idempotency_key(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_odoo_preview_profile_payload())
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/odoo-target-replacement-plan.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=LaunchplaneAuthzPolicy.model_validate(
                    {
                        "github_actions": [
                            {
                                "repository": "cbusillo/launchplane",
                                "workflow_refs": [
                                    "cbusillo/launchplane/.github/workflows/odoo-target-replacement-plan.yml@refs/heads/main"
                                ],
                                "event_names": ["workflow_dispatch"],
                                "products": ["odoo-tenant-cm"],
                                "contexts": ["cm"],
                                "actions": ["odoo_target_replacement_plan.read"],
                            }
                        ]
                    }
                ),
                control_plane_root_path=root,
            )
            request_payload = {
                "product": "odoo-tenant-cm",
                "replacement": {
                    "product": "odoo-tenant-cm",
                    "instance": "testing",
                    "strategy": "recreate-in-place",
                },
            }

            with patch(
                "control_plane.service.build_odoo_stable_target_replacement_plan",
                side_effect=(
                    OdooStableTargetReplacementPlan(
                        plan_status="ready",
                        product="odoo-tenant-cm",
                        context="cm",
                        instance="testing",
                        strategy="recreate-in-place",
                        target_record_found=True,
                        target_id_record_found=True,
                        inventory_found=True,
                        expected_next_target_name="cm-testing-a",
                    ),
                    OdooStableTargetReplacementPlan(
                        plan_status="ready",
                        product="odoo-tenant-cm",
                        context="cm",
                        instance="testing",
                        strategy="recreate-in-place",
                        target_record_found=True,
                        target_id_record_found=True,
                        inventory_found=True,
                        expected_next_target_name="cm-testing-b",
                    ),
                ),
            ) as plan_mock:
                first_status_code, first_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/odoo/target-replacement-plan",
                    payload=request_payload,
                    headers={"Idempotency-Key": "replacement-plan-cm-testing"},
                )
                second_status_code, second_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/odoo/target-replacement-plan",
                    payload=request_payload,
                    headers={"Idempotency-Key": "replacement-plan-cm-testing"},
                )

            self.assertEqual(first_status_code, 202)
            self.assertEqual(second_status_code, 202)
            self.assertEqual(first_payload["result"]["expected_next_target_name"], "cm-testing-a")
            self.assertEqual(second_payload["result"]["expected_next_target_name"], "cm-testing-b")
            self.assertEqual(plan_mock.call_count, 2)

    def test_odoo_target_replacement_apply_driver_enqueues_operation_without_execution(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_odoo_preview_profile_payload())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/launchplane",
                            "workflow_refs": [
                                "cbusillo/launchplane/.github/workflows/odoo-target-replacement-apply.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["odoo-tenant-cm"],
                            "contexts": ["cm"],
                            "actions": ["odoo_target_replacement_apply.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/odoo-target-replacement-apply.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/odoo/target-replacement-apply",
                payload={
                    "product": "odoo-tenant-cm",
                    "replacement": {
                        "product": "odoo-tenant-cm",
                        "instance": "testing",
                        "strategy": "recreate-in-place",
                        "allow_empty_data": False,
                        "verify_health": True,
                        "verify_canonical": True,
                        "verify_logo": True,
                    },
                },
                headers={"Idempotency-Key": "apply-cm-testing"},
            )

            self.assertEqual(status_code, 202)
            self.assertEqual(payload["status"], "accepted")
            operation_id = payload["records"]["odoo_stable_target_replacement_operation_id"]
            self.assertTrue(str(operation_id).startswith("odoo-target-replacement-cm-testing-"))
            self.assertEqual(payload["result"]["status"], "pending")
            self.assertEqual(payload["result"]["phase"], "created")
            self.assertEqual(
                payload["result"]["poll_url"],
                f"/v1/drivers/odoo/target-replacement/operations/{operation_id}",
            )
            stored_operation = store.read_odoo_stable_target_replacement_operation_record(
                str(operation_id)
            )
            self.assertEqual(stored_operation.status, "pending")
            self.assertEqual(stored_operation.phase, "created")
            self.assertTrue(stored_operation.request.verify_health)
            self.assertFalse(stored_operation.request.allow_empty_data)
            self.assertEqual(stored_operation.started_at, "")
            self.assertEqual(stored_operation.finished_at, "")
            self.assertEqual(stored_operation.deployment_record_id, "")
            self.assertIsNone(stored_operation.result)

    def test_odoo_target_replacement_apply_driver_replays_existing_operation(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_odoo_preview_profile_payload())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/launchplane",
                            "workflow_refs": [
                                "cbusillo/launchplane/.github/workflows/odoo-target-replacement-apply.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["odoo-tenant-cm"],
                            "contexts": ["cm"],
                            "actions": ["odoo_target_replacement_apply.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/odoo-target-replacement-apply.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )
            request_payload = {
                "product": "odoo-tenant-cm",
                "replacement": {
                    "product": "odoo-tenant-cm",
                    "instance": "testing",
                    "strategy": "recreate-in-place",
                    "allow_empty_data": False,
                },
            }

            first_status, first_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/odoo/target-replacement-apply",
                payload=request_payload,
                headers={"Idempotency-Key": "apply-cm-testing"},
            )
            second_status, second_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/odoo/target-replacement-apply",
                payload=request_payload,
                headers={"Idempotency-Key": "apply-cm-testing"},
            )

            self.assertEqual(first_status, 202)
            self.assertEqual(second_status, 202)
            self.assertEqual(
                first_payload["records"]["odoo_stable_target_replacement_operation_id"],
                second_payload["records"]["odoo_stable_target_replacement_operation_id"],
            )

    def test_odoo_target_replacement_apply_driver_requires_idempotency_key(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_odoo_preview_profile_payload())
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/odoo-target-replacement-apply.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=LaunchplaneAuthzPolicy.model_validate(
                    {
                        "github_actions": [
                            {
                                "repository": "cbusillo/launchplane",
                                "workflow_refs": [
                                    "cbusillo/launchplane/.github/workflows/odoo-target-replacement-apply.yml@refs/heads/main"
                                ],
                                "event_names": ["workflow_dispatch"],
                                "products": ["odoo-tenant-cm"],
                                "contexts": ["cm"],
                                "actions": ["odoo_target_replacement_apply.execute"],
                            }
                        ]
                    }
                ),
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/odoo/target-replacement-apply",
                payload={
                    "product": "odoo-tenant-cm",
                    "replacement": {
                        "product": "odoo-tenant-cm",
                        "instance": "testing",
                        "strategy": "recreate-in-place",
                    },
                },
            )

            self.assertEqual(status_code, 400)
            self.assertEqual(payload["error"]["code"], "idempotency_key_required")

    def test_odoo_target_replacement_apply_driver_rejects_reused_idempotency_key(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_odoo_preview_profile_payload())
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/odoo-target-replacement-apply.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=LaunchplaneAuthzPolicy.model_validate(
                    {
                        "github_actions": [
                            {
                                "repository": "cbusillo/launchplane",
                                "workflow_refs": [
                                    "cbusillo/launchplane/.github/workflows/odoo-target-replacement-apply.yml@refs/heads/main"
                                ],
                                "event_names": ["workflow_dispatch"],
                                "products": ["odoo-tenant-cm"],
                                "contexts": ["cm"],
                                "actions": ["odoo_target_replacement_apply.execute"],
                            }
                        ]
                    }
                ),
                control_plane_root_path=root,
            )

            first_status, _ = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/odoo/target-replacement-apply",
                payload={
                    "product": "odoo-tenant-cm",
                    "replacement": {
                        "product": "odoo-tenant-cm",
                        "instance": "testing",
                        "strategy": "recreate-in-place",
                        "allow_empty_data": False,
                    },
                },
                headers={"Idempotency-Key": "apply-cm-testing"},
            )
            second_status, second_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/odoo/target-replacement-apply",
                payload={
                    "product": "odoo-tenant-cm",
                    "replacement": {
                        "product": "odoo-tenant-cm",
                        "instance": "testing",
                        "strategy": "recreate-in-place",
                        "allow_empty_data": True,
                    },
                },
                headers={"Idempotency-Key": "apply-cm-testing"},
            )

            self.assertEqual(first_status, 202)
            self.assertEqual(second_status, 409)
            self.assertEqual(second_payload["error"]["code"], "idempotency_key_reused")

    def test_odoo_target_replacement_apply_driver_scopes_replay_to_caller(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            profile_payload = _odoo_profile_payload_with_prod_lane()
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(profile_payload)
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/launchplane",
                            "workflow_refs": [
                                "cbusillo/launchplane/.github/workflows/odoo-target-replacement-apply.yml@refs/heads/main",
                                "cbusillo/launchplane/.github/workflows/odoo-target-replacement-apply-other.yml@refs/heads/main",
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["odoo-tenant-cm"],
                            "contexts": ["cm"],
                            "actions": ["odoo_target_replacement_apply.execute"],
                        }
                    ]
                }
            )
            first_app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/odoo-target-replacement-apply.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )
            second_app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/odoo-target-replacement-apply-other.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            first_status, first_payload = _invoke_app(
                first_app,
                method="POST",
                path="/v1/drivers/odoo/target-replacement-apply",
                payload={
                    "product": "odoo-tenant-cm",
                    "replacement": {
                        "product": "odoo-tenant-cm",
                        "instance": "testing",
                        "strategy": "recreate-in-place",
                        "allow_empty_data": False,
                    },
                },
                headers={"Idempotency-Key": "shared-target-replacement-key"},
            )
            first_operation_id = first_payload["records"][
                "odoo_stable_target_replacement_operation_id"
            ]
            first_operation = store.read_odoo_stable_target_replacement_operation_record(
                first_operation_id
            )
            store.write_odoo_stable_target_replacement_operation_record(
                first_operation.model_copy(
                    update={
                        "status": "pass",
                        "phase": "completed",
                        "finished_at": "2026-05-17T00:05:00Z",
                        "updated_at": "2026-05-17T00:05:00Z",
                    }
                )
            )
            second_status, second_payload = _invoke_app(
                second_app,
                method="POST",
                path="/v1/drivers/odoo/target-replacement-apply",
                payload={
                    "product": "odoo-tenant-cm",
                    "replacement": {
                        "product": "odoo-tenant-cm",
                        "instance": "prod",
                        "strategy": "recreate-in-place",
                        "allow_empty_data": False,
                    },
                },
                headers={"Idempotency-Key": "shared-target-replacement-key"},
            )

            self.assertEqual(first_status, 202)
            self.assertEqual(second_status, 202)
            self.assertNotEqual(
                first_payload["records"]["odoo_stable_target_replacement_operation_id"],
                second_payload["records"]["odoo_stable_target_replacement_operation_id"],
            )

    def test_odoo_target_replacement_apply_driver_blocks_second_active_lane_operation(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_odoo_preview_profile_payload())
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/launchplane",
                            "workflow_refs": [
                                "cbusillo/launchplane/.github/workflows/odoo-target-replacement-apply.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["odoo-tenant-cm"],
                            "contexts": ["cm"],
                            "actions": ["odoo_target_replacement_apply.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/odoo-target-replacement-apply.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )
            request_payload = {
                "product": "odoo-tenant-cm",
                "replacement": {
                    "product": "odoo-tenant-cm",
                    "instance": "testing",
                    "strategy": "recreate-in-place",
                    "allow_empty_data": False,
                },
            }

            first_status, _ = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/odoo/target-replacement-apply",
                payload=request_payload,
                headers={"Idempotency-Key": "apply-cm-testing-1"},
            )
            second_status, second_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/odoo/target-replacement-apply",
                payload=request_payload,
                headers={"Idempotency-Key": "apply-cm-testing-2"},
            )

            self.assertEqual(first_status, 202)
            self.assertEqual(second_status, 409)
            self.assertEqual(
                second_payload["error"]["code"],
                "odoo_stable_target_replacement_operation_active",
            )

    def test_odoo_target_replacement_apply_driver_rejects_unauthorized_workflow(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_odoo_preview_profile_payload())
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=(
                            "cbusillo/launchplane/.github/workflows/odoo-target-replacement-apply.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=LaunchplaneAuthzPolicy.model_validate(
                    {
                        "github_actions": [
                            {
                                "repository": "cbusillo/launchplane",
                                "workflow_refs": [
                                    "cbusillo/launchplane/.github/workflows/odoo-target-replacement-apply.yml@refs/heads/main"
                                ],
                                "event_names": ["workflow_dispatch"],
                                "products": ["odoo-tenant-cm"],
                                "contexts": ["cm"],
                                "actions": ["odoo_target_replacement_plan.read"],
                            }
                        ]
                    }
                ),
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/odoo/target-replacement-apply",
                payload={
                    "product": "odoo-tenant-cm",
                    "replacement": {
                        "product": "odoo-tenant-cm",
                        "instance": "testing",
                    },
                },
                headers={"Idempotency-Key": "apply-cm-testing"},
            )

            self.assertEqual(status_code, 403)
            self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_odoo_artifact_publish_driver_writes_manifest_for_authorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/tenant-opw",
                            "workflow_refs": [
                                "every/tenant-opw/.github/workflows/odoo-artifact-publish.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["odoo"],
                            "contexts": ["opw"],
                            "actions": ["odoo_artifact_publish.write"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="every/tenant-opw",
                        workflow_ref=(
                            "every/tenant-opw/.github/workflows/odoo-artifact-publish.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.service.ingest_odoo_artifact_publish_evidence",
                return_value=OdooArtifactPublishResult(
                    status="pass",
                    context="opw",
                    instance="testing",
                    artifact_id="artifact-opw-new",
                    image_repository="ghcr.io/cbusillo/odoo-tenant-opw",
                    image_digest="sha256:new",
                    source_commit="2719b363e1a434d890b2d75f0cb4ef629bc3a012",
                ),
            ) as execute_mock:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/odoo/artifact-publish",
                    payload={
                        "product": "odoo",
                        "publish": {
                            "context": "opw",
                            "instance": "testing",
                            "manifest": {
                                "artifact_id": "artifact-opw-new",
                                "source_commit": "2719b363e1a434d890b2d75f0cb4ef629bc3a012",
                                "enterprise_base_digest": "sha256:enterprise",
                                "image": {
                                    "repository": "ghcr.io/cbusillo/odoo-tenant-opw",
                                    "digest": "sha256:new",
                                },
                            },
                        },
                    },
                )

            self.assertEqual(status_code, 202)
            self.assertEqual(payload["status"], "accepted")
            self.assertEqual(payload["records"]["artifact_id"], "artifact-opw-new")
            self.assertEqual(payload["result"]["status"], "pass")
            execute_mock.assert_called_once()

    def test_odoo_artifact_publish_driver_replays_idempotent_response(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/tenant-opw",
                            "workflow_refs": [
                                "every/tenant-opw/.github/workflows/odoo-artifact-publish.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["odoo"],
                            "contexts": ["opw"],
                            "actions": ["odoo_artifact_publish.write"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="every/tenant-opw",
                        workflow_ref=(
                            "every/tenant-opw/.github/workflows/odoo-artifact-publish.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )
            request_payload = {
                "product": "odoo",
                "publish": {
                    "context": "opw",
                    "instance": "testing",
                    "manifest": {
                        "artifact_id": "artifact-opw-new",
                        "source_commit": "2719b363e1a434d890b2d75f0cb4ef629bc3a012",
                        "enterprise_base_digest": "sha256:enterprise",
                        "image": {
                            "repository": "ghcr.io/cbusillo/odoo-tenant-opw",
                            "digest": "sha256:new",
                        },
                    },
                },
            }

            with patch(
                "control_plane.service.ingest_odoo_artifact_publish_evidence",
                return_value=OdooArtifactPublishResult(
                    status="pass",
                    context="opw",
                    instance="testing",
                    artifact_id="artifact-opw-new",
                    image_repository="ghcr.io/cbusillo/odoo-tenant-opw",
                    image_digest="sha256:new",
                    source_commit="2719b363e1a434d890b2d75f0cb4ef629bc3a012",
                ),
            ) as execute_mock:
                first_status_code, first_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/odoo/artifact-publish",
                    payload=request_payload,
                    headers={"Idempotency-Key": "odoo-artifact-publish-opw"},
                )
                second_status_code, second_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/odoo/artifact-publish",
                    payload=request_payload,
                    headers={"Idempotency-Key": "odoo-artifact-publish-opw"},
                )

            self.assertEqual(first_status_code, 202)
            self.assertEqual(second_status_code, 202)
            self.assertEqual(first_payload["records"], second_payload["records"])
            self.assertTrue(second_payload["replayed"])
            execute_mock.assert_called_once()

    def test_odoo_artifact_publish_driver_rejects_unauthorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="every/tenant-opw",
                        workflow_ref=(
                            "every/tenant-opw/.github/workflows/odoo-artifact-publish.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=LaunchplaneAuthzPolicy.model_validate(
                    {
                        "github_actions": [
                            {
                                "repository": "every/tenant-opw",
                                "workflow_refs": [
                                    "every/tenant-opw/.github/workflows/odoo-artifact-publish.yml@refs/heads/main"
                                ],
                                "event_names": ["workflow_dispatch"],
                                "products": ["odoo"],
                                "contexts": ["opw"],
                                "actions": ["odoo_post_deploy.execute"],
                            }
                        ]
                    }
                ),
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/odoo/artifact-publish",
                payload={
                    "product": "odoo",
                    "publish": {
                        "context": "opw",
                        "instance": "testing",
                        "manifest": {
                            "artifact_id": "artifact-opw-new",
                            "source_commit": "2719b363e1a434d890b2d75f0cb4ef629bc3a012",
                            "enterprise_base_digest": "sha256:enterprise",
                            "image": {
                                "repository": "ghcr.io/cbusillo/odoo-tenant-opw",
                                "digest": "sha256:new",
                            },
                        },
                    },
                },
            )

            self.assertEqual(status_code, 403)
            self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_verireel_prod_promotion_driver_rejects_unauthorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/promote-image.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=LaunchplaneAuthzPolicy.model_validate(
                    {
                        "github_actions": [
                            {
                                "repository": "every/verireel",
                                "workflow_refs": [
                                    "every/verireel/.github/workflows/promote-image.yml@refs/heads/main"
                                ],
                                "event_names": ["workflow_dispatch"],
                                "products": ["verireel"],
                                "contexts": ["verireel"],
                                "actions": ["promotion.write"],
                            }
                        ]
                    }
                ),
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/verireel/prod-promotion",
                payload={
                    "product": "verireel",
                    "promotion": {
                        "artifact_id": "ghcr.io/every/verireel-app:sha-abcdef1234567890",
                        "source_git_ref": "abcdef1234567890",
                        "backup_record_id": "backup-gate-verireel-prod-run-12345-attempt-1",
                        "promotion_record_id": "promotion-verireel-testing-to-prod-run-12345-attempt-1",
                    },
                },
            )

            self.assertEqual(status_code, 403)
            self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_verireel_prod_rollback_driver_executes_for_authorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/promote-image.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["verireel"],
                            "contexts": ["verireel"],
                            "actions": ["verireel_prod_rollback.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/promote-image.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.service.execute_verireel_prod_rollback",
                return_value=VeriReelProdRollbackResult(
                    promotion_record_id="promotion-verireel-testing-to-prod-run-12345-attempt-1",
                    backup_record_id="backup-gate-verireel-prod-run-12345-attempt-1",
                    snapshot_name="ver-predeploy-20260421-180000",
                    rollback_status="pass",
                    rollback_health_status="pass",
                    rollback_started_at="2026-04-21T18:20:00Z",
                    rollback_finished_at="2026-04-21T18:21:00Z",
                ),
            ) as execute_mock:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/verireel/prod-rollback",
                    payload={
                        "product": "verireel",
                        "rollback": {
                            "promotion_record_id": "promotion-verireel-testing-to-prod-run-12345-attempt-1",
                            "backup_record_id": "backup-gate-verireel-prod-run-12345-attempt-1",
                        },
                    },
                    headers={"Idempotency-Key": "verireel-prod-rollback"},
                )
                replay_status_code, replay_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/verireel/prod-rollback",
                    payload={
                        "product": "verireel",
                        "rollback": {
                            "promotion_record_id": "promotion-verireel-testing-to-prod-run-12345-attempt-1",
                            "backup_record_id": "backup-gate-verireel-prod-run-12345-attempt-1",
                        },
                    },
                    headers={"Idempotency-Key": "verireel-prod-rollback"},
                )

            self.assertEqual(status_code, 202)
            self.assertEqual(payload["status"], "accepted")
            self.assertEqual(
                payload["records"],
                {
                    "promotion_record_id": "promotion-verireel-testing-to-prod-run-12345-attempt-1",
                    "backup_record_id": "backup-gate-verireel-prod-run-12345-attempt-1",
                },
            )
            self.assertEqual(payload["result"]["rollback_status"], "pass")
            self.assertEqual(replay_status_code, 202)
            self.assertEqual(replay_payload["status"], "accepted")
            self.assertTrue(replay_payload["replayed"])
            self.assertEqual(replay_payload["original_trace_id"], payload["trace_id"])
            self.assertEqual(replay_payload["result"], payload["result"])
            execute_mock.assert_called_once()

    def test_verireel_driver_route_accepts_product_profile_driver_id(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            profile_payload = _generic_site_profile_payload(product="video-site")
            profile_payload["display_name"] = "Video Site"
            profile_payload["driver_id"] = "verireel"
            profile_payload["lanes"] = (
                {
                    "instance": "testing",
                    "context": "video-site",
                    "base_url": "https://testing.video.example",
                    "health_url": "https://testing.video.example/api/health",
                },
                {
                    "instance": "prod",
                    "context": "video-site",
                    "base_url": "https://video.example",
                    "health_url": "https://video.example/api/health",
                },
            )
            profile_payload["preview"] = {
                "enabled": False,
                "context": "",
                "slug_template": "pr-{number}",
            }
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(profile_payload)
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/video-site",
                            "workflow_refs": [
                                "every/video-site/.github/workflows/promote-image.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["video-site"],
                            "contexts": ["video-site"],
                            "actions": ["verireel_prod_rollback.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="every/video-site",
                        workflow_ref=(
                            "every/video-site/.github/workflows/promote-image.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.service.execute_verireel_prod_rollback",
                return_value=VeriReelProdRollbackResult(
                    promotion_record_id="promotion-video-testing-to-prod-run-12345-attempt-1",
                    backup_record_id="backup-gate-video-prod-run-12345-attempt-1",
                    snapshot_name="video-predeploy-20260421-180000",
                    rollback_status="pass",
                    rollback_health_status="pass",
                    rollback_started_at="2026-04-21T18:20:00Z",
                    rollback_finished_at="2026-04-21T18:21:00Z",
                ),
            ) as execute_mock:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/verireel/prod-rollback",
                    payload={
                        "product": "video-site",
                        "rollback": {
                            "context": "video-site",
                            "instance": "prod",
                            "promotion_record_id": "promotion-video-testing-to-prod-run-12345-attempt-1",
                            "backup_record_id": "backup-gate-video-prod-run-12345-attempt-1",
                        },
                    },
                )

            self.assertEqual(status_code, 202)
            self.assertEqual(
                payload["records"]["promotion_record_id"],
                "promotion-video-testing-to-prod-run-12345-attempt-1",
            )
            execute_mock.assert_called_once()

    def test_verireel_driver_route_rejects_unowned_profile_lane(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            profile_payload = _generic_site_profile_payload(product="video-site")
            profile_payload["display_name"] = "Video Site"
            profile_payload["driver_id"] = "verireel"
            profile_payload["lanes"] = (
                {
                    "instance": "testing",
                    "context": "video-site",
                    "base_url": "https://testing.video.example",
                    "health_url": "https://testing.video.example/api/health",
                },
                {
                    "instance": "prod",
                    "context": "video-site",
                    "base_url": "https://video.example",
                    "health_url": "https://video.example/api/health",
                },
            )
            profile_payload["preview"] = {
                "enabled": False,
                "context": "",
                "slug_template": "pr-{number}",
            }
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(profile_payload)
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/video-site",
                            "workflow_refs": [
                                "every/video-site/.github/workflows/promote-image.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["video-site"],
                            "contexts": ["other-site"],
                            "actions": ["verireel_prod_rollback.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        repository="every/video-site",
                        workflow_ref=(
                            "every/video-site/.github/workflows/promote-image.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch("control_plane.service.execute_verireel_prod_rollback") as execute_mock:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/verireel/prod-rollback",
                    payload={
                        "product": "video-site",
                        "rollback": {
                            "context": "other-site",
                            "instance": "prod",
                            "promotion_record_id": "promotion-video-testing-to-prod-run-12345-attempt-1",
                            "backup_record_id": "backup-gate-video-prod-run-12345-attempt-1",
                        },
                    },
                )

            self.assertEqual(status_code, 403)
            self.assertEqual(payload["error"]["code"], "product_driver_mismatch")
            execute_mock.assert_not_called()

    def test_verireel_prod_rollback_driver_rejects_unauthorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/promote-image.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=LaunchplaneAuthzPolicy.model_validate(
                    {
                        "github_actions": [
                            {
                                "repository": "every/verireel",
                                "workflow_refs": [
                                    "every/verireel/.github/workflows/promote-image.yml@refs/heads/main"
                                ],
                                "event_names": ["workflow_dispatch"],
                                "products": ["verireel"],
                                "contexts": ["verireel"],
                                "actions": ["promotion.write"],
                            }
                        ]
                    }
                ),
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/verireel/prod-rollback",
                payload={
                    "product": "verireel",
                    "rollback": {
                        "promotion_record_id": "promotion-verireel-testing-to-prod-run-12345-attempt-1",
                        "backup_record_id": "backup-gate-verireel-prod-run-12345-attempt-1",
                    },
                },
            )

            self.assertEqual(status_code, 403)
            self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_driver_unexpected_error_returns_json_response(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/promote-image.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=LaunchplaneAuthzPolicy.model_validate(
                    {
                        "github_actions": [
                            {
                                "repository": "every/verireel",
                                "workflow_refs": [
                                    "every/verireel/.github/workflows/promote-image.yml@refs/heads/main"
                                ],
                                "event_names": ["workflow_dispatch"],
                                "products": ["verireel"],
                                "contexts": ["verireel"],
                                "actions": ["verireel_prod_rollback.execute"],
                            }
                        ]
                    }
                ),
                control_plane_root_path=root,
            )

            with (
                patch(
                    "control_plane.service.execute_verireel_prod_rollback",
                    side_effect=RuntimeError("driver exploded"),
                ),
                patch("control_plane.service._LOGGER.exception"),
            ):
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/verireel/prod-rollback",
                    payload={
                        "product": "verireel",
                        "rollback": {
                            "promotion_record_id": "promotion-verireel-testing-to-prod-run-12345-attempt-1",
                            "backup_record_id": "backup-gate-verireel-prod-run-12345-attempt-1",
                        },
                    },
                )

            self.assertEqual(status_code, 500)
            self.assertEqual(payload["status"], "rejected")
            self.assertEqual(payload["error"]["code"], "internal_error")
            self.assertIn("trace_id", payload)

    def test_verireel_prod_backup_gate_driver_executes_for_authorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/promote-image.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["verireel"],
                            "contexts": ["verireel"],
                            "actions": ["verireel_prod_backup_gate.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/promote-image.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.service.enqueue_verireel_prod_backup_gate",
                return_value=VeriReelProdBackupGateResult(
                    backup_record_id="backup-gate-verireel-prod-run-12345-attempt-1",
                    backup_status="pass",
                    backup_started_at="2026-04-25T00:15:00Z",
                    backup_finished_at="2026-04-25T00:16:00Z",
                    snapshot_name="ver-predeploy-20260425-001500",
                ),
            ) as execute_mock:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/verireel/prod-backup-gate",
                    payload={
                        "product": "verireel",
                        "backup_gate": {
                            "backup_record_id": "backup-gate-verireel-prod-run-12345-attempt-1"
                        },
                    },
                )

            self.assertEqual(status_code, 202)
            self.assertEqual(payload["status"], "accepted")
            self.assertEqual(
                payload["records"],
                {
                    "backup_gate_record_id": "backup-gate-verireel-prod-run-12345-attempt-1",
                },
            )
            self.assertEqual(payload["result"]["backup_status"], "pass")
            execute_mock.assert_called_once()
            self.assertEqual(
                execute_mock.call_args.kwargs["request"].backup_record_id,
                "backup-gate-verireel-prod-run-12345-attempt-1",
            )

    def test_verireel_prod_backup_gate_retry_runs_again_after_pending_result(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/promote-image.yml@refs/heads/main"
                            ],
                            "event_names": ["workflow_dispatch"],
                            "products": ["verireel"],
                            "contexts": ["verireel"],
                            "actions": ["verireel_prod_backup_gate.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/promote-image.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )
            backup_gate_payload = {
                "product": "verireel",
                "backup_gate": {
                    "backup_record_id": "backup-gate-verireel-prod-run-12345-attempt-1"
                },
            }

            with patch(
                "control_plane.service.enqueue_verireel_prod_backup_gate",
                return_value=VeriReelProdBackupGateResult(
                    backup_record_id="backup-gate-verireel-prod-run-12345-attempt-1",
                    backup_status="pending",
                ),
            ) as execute_mock:
                first_status_code, first_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/verireel/prod-backup-gate",
                    payload=backup_gate_payload,
                    headers={"Idempotency-Key": "verireel-prod-backup-gate-pending"},
                )
                second_status_code, second_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/verireel/prod-backup-gate",
                    payload=backup_gate_payload,
                    headers={"Idempotency-Key": "verireel-prod-backup-gate-pending"},
                )

            self.assertEqual(first_status_code, 202)
            self.assertEqual(second_status_code, 202)
            self.assertEqual(first_payload["result"]["backup_status"], "pending")
            self.assertEqual(second_payload["result"]["backup_status"], "pending")
            self.assertNotIn("replayed", first_payload)
            self.assertNotIn("replayed", second_payload)
            self.assertEqual(execute_mock.call_count, 2)

    def test_verireel_prod_backup_gate_driver_rejects_unauthorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/promote-image.yml@refs/heads/main"
                        ),
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=LaunchplaneAuthzPolicy.model_validate(
                    {
                        "github_actions": [
                            {
                                "repository": "every/verireel",
                                "workflow_refs": [
                                    "every/verireel/.github/workflows/promote-image.yml@refs/heads/main"
                                ],
                                "event_names": ["workflow_dispatch"],
                                "products": ["verireel"],
                                "contexts": ["verireel"],
                                "actions": ["promotion.write"],
                            }
                        ]
                    }
                ),
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/verireel/prod-backup-gate",
                payload={
                    "product": "verireel",
                    "backup_gate": {
                        "backup_record_id": "backup-gate-verireel-prod-run-12345-attempt-1"
                    },
                },
            )

            self.assertEqual(status_code, 403)
            self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_verireel_preview_refresh_driver_executes_for_authorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["verireel"],
                            "contexts": ["verireel-testing"],
                            "actions": ["verireel_preview_refresh.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.service.execute_verireel_preview_refresh",
                return_value=VeriReelPreviewRefreshResult(
                    refresh_status="pass",
                    refresh_started_at="2026-04-21T01:30:00Z",
                    refresh_finished_at="2026-04-21T01:34:00Z",
                    application_name="ver-preview-pr-123-app",
                    application_id="preview-app-123",
                    preview_url="https://pr-123.ver-preview.shinycomputers.com",
                ),
            ) as execute_mock:
                refresh_payload = {
                    "product": "verireel",
                    "refresh": {
                        "anchor_pr_number": 123,
                        "anchor_pr_url": "https://github.com/every/verireel/pull/123",
                        "anchor_head_sha": "6b3c9d7e8f901234567890abcdef1234567890ab",
                        "preview_slug": "pr-123",
                        "preview_url": "https://pr-123.ver-preview.shinycomputers.com",
                        "image_reference": "ghcr.io/every/verireel-app:pr-123-sha-6b3c9d7",
                    },
                }
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/verireel/preview-refresh",
                    payload=refresh_payload,
                    headers={"Idempotency-Key": "verireel-preview-refresh-pr-123"},
                )
                replay_status_code, replay_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/verireel/preview-refresh",
                    payload=refresh_payload,
                    headers={"Idempotency-Key": "verireel-preview-refresh-pr-123"},
                )

            self.assertEqual(status_code, 202)
            self.assertEqual(replay_status_code, 202)
            self.assertEqual(replay_payload["status"], "accepted")
            self.assertTrue(replay_payload["replayed"])
            self.assertEqual(replay_payload["original_trace_id"], payload["trace_id"])
            self.assertEqual(replay_payload["records"], payload["records"])
            self.assertEqual(replay_payload["result"], payload["result"])
            self.assertEqual(payload["status"], "accepted")
            self.assertEqual(
                payload["records"]["preview_id"], "preview-verireel-testing-verireel-pr-123"
            )
            self.assertEqual(
                payload["records"]["generation_id"],
                "preview-verireel-testing-verireel-pr-123-generation-0001",
            )
            self.assertEqual(payload["records"]["transition"], "verifying")
            self.assertEqual(payload["result"]["refresh_status"], "pass")
            self.assertEqual(payload["result"]["application_id"], "preview-app-123")
            store = FilesystemRecordStore(state_dir=state_dir)
            preview = store.read_preview_record("preview-verireel-testing-verireel-pr-123")
            generation = store.read_preview_generation_record(
                "preview-verireel-testing-verireel-pr-123-generation-0001"
            )
            self.assertEqual(preview.state, "pending")
            self.assertEqual(preview.canonical_url, "https://pr-123.ver-preview.shinycomputers.com")
            self.assertEqual(generation.state, "verifying")
            self.assertEqual(generation.deploy_status, "pass")
            self.assertEqual(generation.verify_status, "pending")
            self.assertEqual(
                generation.resolved_manifest_fingerprint,
                "verireel-preview-manifest-pr-123-6b3c9d7",
            )
            execute_mock.assert_called_once()

    def test_verireel_preview_refresh_driver_rejects_unauthorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["verireel"],
                            "contexts": ["verireel-testing"],
                            "actions": ["preview_generation.write"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/verireel/preview-refresh",
                payload={
                    "product": "verireel",
                    "refresh": {
                        "anchor_pr_number": 123,
                        "anchor_pr_url": "https://github.com/every/verireel/pull/123",
                        "anchor_head_sha": "6b3c9d7e8f901234567890abcdef1234567890ab",
                        "preview_slug": "pr-123",
                        "preview_url": "https://pr-123.ver-preview.shinycomputers.com",
                        "image_reference": "ghcr.io/every/verireel-app:pr-123-sha-6b3c9d7",
                    },
                },
            )

            self.assertEqual(status_code, 403)
            self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_verireel_preview_refresh_driver_writes_failed_generation_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["verireel"],
                            "contexts": ["verireel-testing"],
                            "actions": ["verireel_preview_refresh.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.service.execute_verireel_preview_refresh",
                return_value=VeriReelPreviewRefreshResult(
                    refresh_status="fail",
                    refresh_started_at="2026-04-21T01:30:00Z",
                    refresh_finished_at="2026-04-21T01:34:00Z",
                    application_name="ver-preview-pr-123-app",
                    application_id="preview-app-123",
                    preview_url="https://pr-123.ver-preview.shinycomputers.com",
                    error_message="Dokploy update failed.",
                ),
            ):
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/verireel/preview-refresh",
                    payload={
                        "product": "verireel",
                        "refresh": {
                            "anchor_pr_number": 123,
                            "anchor_pr_url": "https://github.com/every/verireel/pull/123",
                            "anchor_head_sha": "6b3c9d7e8f901234567890abcdef1234567890ab",
                            "preview_slug": "pr-123",
                            "preview_url": "https://pr-123.ver-preview.shinycomputers.com",
                            "image_reference": "ghcr.io/every/verireel-app:pr-123-sha-6b3c9d7",
                        },
                    },
                )

            self.assertEqual(status_code, 202)
            self.assertEqual(payload["records"]["transition"], "failed")
            store = FilesystemRecordStore(state_dir=state_dir)
            preview = store.read_preview_record("preview-verireel-testing-verireel-pr-123")
            generation = store.read_preview_generation_record(
                "preview-verireel-testing-verireel-pr-123-generation-0001"
            )
            self.assertEqual(preview.state, "failed")
            self.assertEqual(generation.state, "failed")
            self.assertEqual(generation.deploy_status, "fail")
            self.assertEqual(generation.verify_status, "skipped")
            self.assertEqual(generation.failure_stage, "provision")
            self.assertEqual(generation.failure_summary, "Dokploy update failed.")

    def test_verireel_preview_refresh_config_error_is_recorded_as_failed_generation(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["verireel"],
                            "contexts": ["verireel-testing"],
                            "actions": ["verireel_preview_refresh.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.service.execute_verireel_preview_refresh",
                side_effect=VeriReelPreviewRefreshConfigError(
                    "Missing LAUNCHPLANE_PREVIEW_BASE_URL in Launchplane runtime-environment records for verireel-testing."
                ),
            ):
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/verireel/preview-refresh",
                    payload={
                        "schema_version": 1,
                        "product": "verireel",
                        "refresh": {
                            "schema_version": 1,
                            "context": "verireel-testing",
                            "anchor_repo": "verireel",
                            "anchor_pr_number": 189,
                            "anchor_pr_url": "https://github.com/every/verireel/pull/189",
                            "anchor_head_sha": "bd6ef6afb3c6c9cc3359cf98ac613eca414ac4fe",
                            "preview_slug": "pr-189",
                            "image_reference": "ghcr.io/every/verireel-app:pr-189-sha-bd6ef6a",
                        },
                    },
                    headers={
                        "Idempotency-Key": "verireel-preview-refresh:verireel:verireel-testing:verireel:189:bd6ef6a"
                    },
                )

            self.assertEqual(status_code, 202)
            self.assertEqual(payload["status"], "accepted")
            self.assertEqual(payload["records"]["transition"], "failed")
            self.assertEqual(payload["result"]["refresh_status"], "fail")
            self.assertEqual(
                payload["result"]["error_message"],
                "Missing LAUNCHPLANE_PREVIEW_BASE_URL in Launchplane runtime-environment records for verireel-testing.",
            )
            self.assertEqual(
                payload["result"]["preview_url"],
                "https://pr-189.preview-config-missing.launchplane.invalid",
            )
            store = FilesystemRecordStore(state_dir=state_dir)
            preview = store.read_preview_record("preview-verireel-testing-verireel-pr-189")
            generation = store.read_preview_generation_record(
                "preview-verireel-testing-verireel-pr-189-generation-0001"
            )
            self.assertEqual(preview.state, "failed")
            self.assertEqual(
                preview.canonical_url,
                "https://pr-189.preview-config-missing.launchplane.invalid",
            )
            self.assertEqual(generation.state, "failed")
            self.assertEqual(generation.deploy_status, "fail")
            self.assertEqual(generation.verify_status, "skipped")
            self.assertEqual(generation.failure_stage, "provision")
            self.assertEqual(
                generation.failure_summary,
                "Missing LAUNCHPLANE_PREVIEW_BASE_URL in Launchplane runtime-environment records for verireel-testing.",
            )

    def test_verireel_preview_refresh_transport_error_rejects_without_records(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["verireel"],
                            "contexts": ["verireel-testing"],
                            "actions": ["verireel_preview_refresh.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.service.execute_verireel_preview_refresh",
                side_effect=VeriReelPreviewRefreshTransportError(
                    "Dokploy API GET /api/application.one request failed: timed out"
                ),
            ):
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/verireel/preview-refresh",
                    payload={
                        "schema_version": 1,
                        "product": "verireel",
                        "refresh": {
                            "schema_version": 1,
                            "context": "verireel-testing",
                            "anchor_repo": "verireel",
                            "anchor_pr_number": 189,
                            "anchor_pr_url": "https://github.com/every/verireel/pull/189",
                            "anchor_head_sha": "bd6ef6afb3c6c9cc3359cf98ac613eca414ac4fe",
                            "preview_slug": "pr-189",
                            "image_reference": "ghcr.io/every/verireel-app:pr-189-sha-bd6ef6a",
                        },
                    },
                    headers={
                        "Idempotency-Key": "verireel-preview-refresh:verireel:verireel-testing:verireel:189:bd6ef6a"
                    },
                )

            self.assertEqual(status_code, 502)
            self.assertEqual(payload["status"], "rejected")
            self.assertEqual(payload["error"]["code"], "preview_refresh_backend_unavailable")
            store = FilesystemRecordStore(state_dir=state_dir)
            with self.assertRaises(FileNotFoundError):
                store.read_preview_record("preview-verireel-testing-verireel-pr-189")

    def test_verireel_preview_refresh_payload_validation_still_rejects_bad_slug(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["verireel"],
                            "contexts": ["verireel-testing"],
                            "actions": ["verireel_preview_refresh.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/verireel/preview-refresh",
                payload={
                    "schema_version": 1,
                    "product": "verireel",
                    "refresh": {
                        "schema_version": 1,
                        "context": "verireel-testing",
                        "anchor_repo": "verireel",
                        "anchor_pr_number": 189,
                        "anchor_pr_url": "https://github.com/every/verireel/pull/189",
                        "anchor_head_sha": "bd6ef6afb3c6c9cc3359cf98ac613eca414ac4fe",
                        "preview_slug": "pr-190",
                        "image_reference": "ghcr.io/every/verireel-app:pr-189-sha-bd6ef6a",
                    },
                },
            )

            self.assertEqual(status_code, 400)
            self.assertEqual(payload["status"], "rejected")
            self.assertEqual(payload["error"]["code"], "invalid_request")
            self.assertEqual(payload["error"]["message"], "Request payload failed validation.")

    def test_verireel_preview_verification_driver_marks_latest_generation_ready(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_preview_record(
                PreviewRecord(
                    preview_id="preview-verireel-testing-verireel-pr-123",
                    context="verireel-testing",
                    anchor_repo="verireel",
                    anchor_pr_number=123,
                    anchor_pr_url="https://github.com/every/verireel/pull/123",
                    preview_label="preview",
                    canonical_url="https://pr-123.ver-preview.shinycomputers.com",
                    state="pending",
                    created_at="2026-04-21T01:30:00Z",
                    updated_at="2026-04-21T01:34:00Z",
                    eligible_at="2026-04-21T01:30:00Z",
                    active_generation_id="preview-verireel-testing-verireel-pr-123-generation-0001",
                    latest_generation_id="preview-verireel-testing-verireel-pr-123-generation-0001",
                    latest_manifest_fingerprint="verireel-preview-manifest-pr-123-6b3c9d7",
                )
            )
            store.write_preview_generation_record(
                PreviewGenerationRecord(
                    generation_id="preview-verireel-testing-verireel-pr-123-generation-0001",
                    preview_id="preview-verireel-testing-verireel-pr-123",
                    sequence=1,
                    state="verifying",
                    requested_reason="external_preview_refresh",
                    requested_at="2026-04-21T01:30:00Z",
                    started_at="2026-04-21T01:30:00Z",
                    resolved_manifest_fingerprint="verireel-preview-manifest-pr-123-6b3c9d7",
                    artifact_id="ghcr.io/every/verireel-app:pr-123-sha-6b3c9d7",
                    anchor_summary=PreviewPullRequestSummary(
                        repo="verireel",
                        pr_number=123,
                        head_sha="6b3c9d7e8f901234567890abcdef1234567890ab",
                        pr_url="https://github.com/every/verireel/pull/123",
                    ),
                    deploy_status="pass",
                    verify_status="pending",
                    overall_health_status="pending",
                )
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
                            "products": ["verireel"],
                            "contexts": ["verireel-testing"],
                            "actions": ["preview_generation.write"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/verireel/preview-verification",
                payload={
                    "product": "verireel",
                    "verification": {
                        "anchor_pr_number": 123,
                        "verification_status": "pass",
                        "verified_at": "2026-04-21T01:38:00Z",
                    },
                },
                headers={"Idempotency-Key": "verireel-preview-verification-pr-123"},
            )
            replay_status_code, replay_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/verireel/preview-verification",
                payload={
                    "product": "verireel",
                    "verification": {
                        "anchor_pr_number": 123,
                        "verification_status": "pass",
                        "verified_at": "2026-04-21T01:38:00Z",
                    },
                },
                headers={"Idempotency-Key": "verireel-preview-verification-pr-123"},
            )

            self.assertEqual(status_code, 202)
            self.assertEqual(replay_status_code, 202)
            self.assertTrue(replay_payload["replayed"])
            self.assertEqual(payload["records"], replay_payload["records"])
            self.assertEqual(payload["records"]["transition"], "ready")
            preview = store.read_preview_record("preview-verireel-testing-verireel-pr-123")
            generation = store.read_preview_generation_record(
                "preview-verireel-testing-verireel-pr-123-generation-0001"
            )
            self.assertEqual(preview.state, "active")
            self.assertEqual(preview.serving_generation_id, generation.generation_id)
            self.assertEqual(generation.state, "ready")
            self.assertEqual(generation.verify_status, "pass")
            self.assertEqual(generation.overall_health_status, "pass")
            self.assertEqual(generation.ready_at, "2026-04-21T01:38:00Z")

    def test_verireel_preview_destroy_driver_executes_for_authorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_preview_record(
                PreviewRecord(
                    preview_id="preview-verireel-testing-verireel-pr-123",
                    context="verireel-testing",
                    anchor_repo="verireel",
                    anchor_pr_number=123,
                    anchor_pr_url="https://github.com/every/verireel/pull/123",
                    preview_label="preview",
                    canonical_url="https://pr-123.ver-preview.shinycomputers.com",
                    state="active",
                    created_at="2026-04-21T01:30:00Z",
                    updated_at="2026-04-21T01:34:00Z",
                    eligible_at="2026-04-21T01:30:00Z",
                    active_generation_id="preview-verireel-testing-verireel-pr-123-generation-0001",
                    serving_generation_id="preview-verireel-testing-verireel-pr-123-generation-0001",
                    latest_generation_id="preview-verireel-testing-verireel-pr-123-generation-0001",
                    latest_manifest_fingerprint="verireel-preview-manifest-pr-123-6b3c9d7",
                )
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-cleanup.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["verireel"],
                            "contexts": ["verireel-testing"],
                            "actions": ["verireel_preview_destroy.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/preview-cleanup.yml@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.service.execute_verireel_preview_destroy",
                return_value=VeriReelPreviewDestroyResult(
                    destroy_status="pass",
                    destroy_started_at="2026-04-21T01:35:00Z",
                    destroy_finished_at="2026-04-21T01:36:00Z",
                    application_name="ver-preview-pr-123-app",
                    application_id="preview-app-123",
                    preview_url="https://pr-123.ver-preview.shinycomputers.com",
                ),
            ) as execute_mock:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/verireel/preview-destroy",
                    payload={
                        "product": "verireel",
                        "destroy": {
                            "anchor_pr_number": 123,
                            "preview_slug": "pr-123",
                            "destroy_reason": "external_preview_pull_request_closed",
                        },
                    },
                    headers={"Idempotency-Key": "verireel-preview-destroy-pr-123"},
                )
                replay_status_code, replay_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/verireel/preview-destroy",
                    payload={
                        "product": "verireel",
                        "destroy": {
                            "anchor_pr_number": 123,
                            "preview_slug": "pr-123",
                            "destroy_reason": "external_preview_janitor_cleanup_completed",
                        },
                    },
                    headers={"Idempotency-Key": "verireel-preview-destroy-pr-123"},
                )

            self.assertEqual(status_code, 202)
            self.assertEqual(payload["status"], "accepted")
            self.assertEqual(
                payload["records"]["preview_id"], "preview-verireel-testing-verireel-pr-123"
            )
            self.assertEqual(payload["records"]["transition"], "destroyed")
            self.assertEqual(payload["result"]["destroy_status"], "pass")
            self.assertEqual(payload["result"]["application_id"], "preview-app-123")
            self.assertEqual(replay_status_code, 202)
            self.assertEqual(replay_payload["status"], "accepted")
            self.assertTrue(replay_payload["replayed"])
            self.assertEqual(replay_payload["original_trace_id"], payload["trace_id"])
            self.assertEqual(replay_payload["records"], payload["records"])
            self.assertEqual(replay_payload["result"], payload["result"])
            preview = store.read_preview_record("preview-verireel-testing-verireel-pr-123")
            self.assertEqual(preview.state, "destroyed")
            self.assertEqual(preview.destroy_reason, "external_preview_pull_request_closed")
            self.assertEqual(preview.destroyed_at, "2026-04-21T01:36:00Z")
            execute_mock.assert_called_once()

    def test_verireel_preview_destroy_driver_executes_for_authorized_janitor_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-janitor.yml@refs/heads/main"
                            ],
                            "event_names": ["schedule", "workflow_dispatch"],
                            "products": ["verireel"],
                            "contexts": ["verireel-testing"],
                            "actions": [
                                "verireel_preview_destroy.execute",
                                "preview_destroyed.write",
                            ],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/preview-janitor.yml@refs/heads/main"
                        ),
                        event_name="schedule",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.service.execute_verireel_preview_destroy",
                return_value=VeriReelPreviewDestroyResult(
                    destroy_status="pass",
                    destroy_started_at="2026-04-24T13:00:00Z",
                    destroy_finished_at="2026-04-24T13:01:00Z",
                    application_name="ver-preview-pr-72-app",
                    application_id="preview-app-72",
                    preview_url="https://pr-72.ver-preview.shinycomputers.com",
                ),
            ) as execute_mock:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/verireel/preview-destroy",
                    payload={
                        "product": "verireel",
                        "destroy": {
                            "context": "verireel-testing",
                            "anchor_repo": "verireel",
                            "anchor_pr_number": 72,
                            "preview_slug": "pr-72",
                            "destroy_reason": "external_preview_janitor_cleanup_completed",
                        },
                    },
                )

            self.assertEqual(status_code, 202)
            self.assertEqual(payload["status"], "accepted")
            self.assertEqual(payload["result"]["destroy_status"], "pass")
            self.assertEqual(payload["result"]["application_name"], "ver-preview-pr-72-app")
            execute_mock.assert_called_once()

    def test_verireel_preview_destroy_driver_rejects_unauthorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "every/verireel",
                            "workflow_refs": [
                                "every/verireel/.github/workflows/preview-cleanup.yml@refs/heads/main"
                            ],
                            "event_names": ["pull_request"],
                            "products": ["verireel"],
                            "contexts": ["verireel-testing"],
                            "actions": ["preview_destroyed.write"],
                        }
                    ]
                }
            )
            app = create_launchplane_service_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        workflow_ref=(
                            "every/verireel/.github/workflows/preview-cleanup.yml@refs/heads/main"
                        ),
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/verireel/preview-destroy",
                payload={
                    "product": "verireel",
                    "destroy": {
                        "anchor_pr_number": 123,
                        "preview_slug": "pr-123",
                        "destroy_reason": "external_preview_pull_request_closed",
                    },
                },
            )

            self.assertEqual(status_code, 403)
            self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_single_record_read_routes_are_retired_from_legacy_wsgi_app(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            app = create_launchplane_service_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy.model_validate({}),
                control_plane_root_path=root,
            )

            deployment_status_code, deployment_payload = _invoke_app(
                app,
                method="GET",
                path="/v1/deployments/deployment-20260420T153000Z-opw-testing",
            )
            promotion_status_code, promotion_payload = _invoke_app(
                app,
                method="GET",
                path="/v1/promotions/promotion-20260420T153500Z-opw-testing-to-prod",
            )
            inventory_status_code, inventory_payload = _invoke_app(
                app,
                method="GET",
                path="/v1/inventory/opw/testing",
            )
            preview_status_code, preview_payload = _invoke_app(
                app,
                method="GET",
                path="/v1/previews/preview-opw-opw-pr-42",
            )
            preview_history_status_code, preview_history_payload = _invoke_app(
                app,
                method="GET",
                path="/v1/previews/preview-opw-opw-pr-42/history",
            )
            recent_operations_status_code, recent_operations_payload = _invoke_app(
                app,
                method="GET",
                path="/v1/contexts/opw/operations/recent",
            )
            context_secrets_status_code, context_secrets_payload = _invoke_app(
                app,
                method="GET",
                path="/v1/contexts/opw/secrets",
            )
            instance_secrets_status_code, instance_secrets_payload = _invoke_app(
                app,
                method="GET",
                path="/v1/contexts/opw/instances/prod/secrets",
            )
            secret_status_code, secret_payload = _invoke_app(
                app,
                method="GET",
                path="/v1/secrets/secret-runtime-environment-github-webhook-secret-opw",
            )

        self.assertEqual(deployment_status_code, 404)
        self.assertEqual(deployment_payload["status"], "rejected")
        self.assertEqual(deployment_payload["error"]["code"], "not_found")
        self.assertEqual(promotion_status_code, 404)
        self.assertEqual(promotion_payload["status"], "rejected")
        self.assertEqual(promotion_payload["error"]["code"], "not_found")
        self.assertEqual(inventory_status_code, 404)
        self.assertEqual(inventory_payload["status"], "rejected")
        self.assertEqual(inventory_payload["error"]["code"], "not_found")
        self.assertEqual(preview_status_code, 404)
        self.assertEqual(preview_payload["status"], "rejected")
        self.assertEqual(preview_payload["error"]["code"], "not_found")
        self.assertEqual(preview_history_status_code, 404)
        self.assertEqual(preview_history_payload["status"], "rejected")
        self.assertEqual(preview_history_payload["error"]["code"], "not_found")
        self.assertEqual(recent_operations_status_code, 404)
        self.assertEqual(recent_operations_payload["status"], "rejected")
        self.assertEqual(recent_operations_payload["error"]["code"], "not_found")
        self.assertEqual(context_secrets_status_code, 404)
        self.assertEqual(context_secrets_payload["status"], "rejected")
        self.assertEqual(context_secrets_payload["error"]["code"], "not_found")
        self.assertEqual(instance_secrets_status_code, 404)
        self.assertEqual(instance_secrets_payload["status"], "rejected")
        self.assertEqual(instance_secrets_payload["error"]["code"], "not_found")
        self.assertEqual(secret_status_code, 404)
        self.assertEqual(secret_payload["status"], "rejected")
        self.assertEqual(secret_payload["error"]["code"], "not_found")
