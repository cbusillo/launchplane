import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
import unittest
from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal, cast
from unittest.mock import patch

from click import ClickException, Command
from click.testing import CliRunner

from control_plane.cli import main
from control_plane import live_target_runtime as control_plane_live_target_runtime
from control_plane import secrets as control_plane_secrets
from control_plane.notifications import public_discord_url_error, public_url_error
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.every_code_notifications import (
    EveryCodeNotificationAttemptRecord,
    EveryCodeNotificationDestination,
    EveryCodeNotificationPolicyRecord,
)
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
from control_plane.contracts.authz_policy_record import (
    LaunchplaneAuthzPolicyRecord,
    authz_policy_sha256,
    build_authz_policy_record_id,
)
from control_plane.contracts.driver_descriptor import DriverActionDescriptor, DriverDescriptor
from control_plane.dokploy import DokploySourceOfTruth, DokployTargetDefinition
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.contracts.merge_train_policy import MergeTrainPolicy
from control_plane.contracts.merge_train_policy import parse_merge_train_policy_toml
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
)
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
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
from control_plane.every_code_github_webhook import handle_every_code_github_webhook_request
from control_plane.http_app import LaunchplaneAuthzPolicyRuntime
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.http_app import idempotency_request_fingerprint
from control_plane.service_auth import (
    GitHubActionsIdentity,
    GitHubHumanIdentity,
    LaunchplaneAuthzPolicy,
    LocalAdminPolicyRule,
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
    LaunchplaneHumanSession,
    build_browser_mutation_request_headers,
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
from control_plane.workflows.verireel_stable_deploy import VeriReelStableDeployResult
from control_plane.workflows.verireel_environment import VeriReelStableEnvironmentResult
from control_plane.workflows.verireel_rollout import VeriReelRolloutVerificationResult
from control_plane.workflows.odoo_artifact_publish import OdooArtifactPublishResult
from tests.merge_train_policy_fixtures import build_test_merge_train_policy_record
from tests.support.http import get as http_get
from tests.support.http import request as http_request
from tests.support.auth import (
    _StubVerifier,
    _identity,
)
from tests.support.profiles import (
    _product_profile_payload,
    _odoo_preview_profile_payload,
    _generic_site_profile_payload,
)
from tests.support.stores import (
    _sqlite_database_url,
    _seed_tracked_target_records,
)

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
GITHUB_REPOSITORY_IDS = {
    "repository_id": "1001",
    "repository_owner_id": "2001",
}


_FAKE_DESCRIPTOR_DRIVER_ID = "fake-descriptor"
_FAKE_DESCRIPTOR_ROUTE_PATH = "/v1/drivers/fake-descriptor/ping"


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


def _fastapi_browser_mutation_headers(
    session_manager: HumanSessionManager,
    cookie: str,
) -> dict[str, str]:
    human_session = session_manager.read_cookie(cookie)
    assert human_session is not None
    return {
        "Cookie": cookie,
        **build_browser_mutation_request_headers(
            origin=session_manager.public_origin,
            csrf_token=session_manager.csrf_token(human_session),
        ),
    }


def _authz_policy_record_by_id(
    records: tuple[LaunchplaneAuthzPolicyRecord, ...], record_id: object
) -> LaunchplaneAuthzPolicyRecord:
    for record in records:
        if record.record_id == str(record_id):
            return record
    raise AssertionError(f"Authz policy record {record_id!r} was not found")


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


def _passed_healthcheck_evidence(url: str) -> HealthcheckEvidence:
    return HealthcheckEvidence(
        verified=True,
        urls=(url,),
        timeout_seconds=45,
        status="pass",
    )


def create_launchplane_fastapi_test_app(**kwargs: object) -> Any:
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


class _HostedAuthzPolicyStore(PostgresRecordStore):
    @property
    def database_dialect_name(self) -> str:
        return "postgresql"

    def write_authz_policy_record(self, record: LaunchplaneAuthzPolicyRecord) -> None:
        active_records = self.list_authz_policy_records(status="active", limit=1)
        if not active_records:
            seeded_record = record.model_copy(
                update={
                    "record_id": build_authz_policy_record_id(
                        revision=1,
                        policy_sha256=record.policy_sha256,
                    ),
                    "revision": 1,
                }
            )
            self.seed_authz_policy_if_absent(seeded_record)
            return
        current_record = active_records[0]
        replacement_record = record.model_copy(
            update={
                "record_id": build_authz_policy_record_id(
                    revision=current_record.revision + 1,
                    policy_sha256=record.policy_sha256,
                ),
                "revision": current_record.revision + 1,
            }
        )
        result = self.compare_and_write_authz_policy_record(
            expected_record=current_record,
            replacement_record=replacement_record,
        )
        if result.status != "written":
            raise AssertionError(f"test authz policy write failed: {result.status}")


def create_every_code_github_webhook_app(**kwargs: object) -> Any:
    state_dir = kwargs.pop("state_dir", None)
    local_record_store = kwargs.pop("local_record_store_for_tests", None)
    if local_record_store is None and "database_url" not in kwargs and isinstance(state_dir, Path):
        local_record_store = FilesystemRecordStore(state_dir=state_dir)
    if local_record_store is not None:
        kwargs["record_store_factory"] = lambda: local_record_store
    kwargs["every_code_github_webhook_handler"] = handle_every_code_github_webhook_request
    return create_launchplane_fastapi_test_app(**kwargs)


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
            fencing_token=record.fencing_token,
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


def _invoke_app(
    app: Any,
    *,
    method: str,
    path: str,
    query_string: str = "",
    payload: Mapping[str, object] | None = None,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    response = asyncio.run(
        _http_request_for_service_test(
            app,
            method=method,
            path=path,
            query_string=query_string,
            payload=payload,
            authorization=authorization,
            headers=headers,
        )
    )
    response_payload = response.json()
    assert isinstance(response_payload, dict)
    return response.status_code, cast(dict[str, Any], response_payload)


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


def _invoke_http_app(
    app: Any,
    *,
    method: str,
    path: str,
    authorization: str = "",
    query_string: str = "",
    headers: dict[str, str] | None = None,
    body_bytes: bytes = b"",
) -> tuple[int, dict[str, str], bytes]:
    response = asyncio.run(
        _http_request_for_service_test(
            app,
            method=method,
            path=path,
            query_string=query_string,
            authorization=authorization,
            headers=headers,
            body_bytes=body_bytes,
        )
    )
    return response.status_code, dict(response.headers), response.content


async def _http_get_for_service_test(app: Any, path: str) -> Any:
    return await http_get(app, path)


async def _http_request_for_service_test(
    app: Any,
    *,
    method: str,
    path: str,
    query_string: str = "",
    payload: Mapping[str, object] | None = None,
    authorization: str = "",
    headers: Mapping[str, str] | None = None,
    body_bytes: bytes | None = None,
) -> Any:
    if payload is not None and body_bytes is not None:
        raise AssertionError("service tests must pass either payload or body_bytes")
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    request_path = f"{path}?{query_string}" if query_string else path
    return await http_request(
        app,
        method,
        request_path,
        headers=request_headers,
        payload=payload,
        raw_body=body_bytes,
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
        _http_request_for_service_test(
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

    def test_human_session_does_not_authorize_post_mutations(self) -> None:
        policy = LaunchplaneAuthzPolicy.model_validate(
            {"github_humans": [{"logins": ["alice"], "roles": ["admin"]}]}
        )
        session_manager = _fastapi_human_session_manager()
        with TemporaryDirectory() as tmpdir:
            app = create_launchplane_fastapi_test_app(
                state_dir=Path(tmpdir) / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                human_session_manager=session_manager,
            )
            cookie = _fastapi_signed_in_cookie(session_manager, role="admin")
            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/drivers/verireel/testing-verification",
                payload={
                    "schema_version": 1,
                    "product": "verireel",
                    "verification": {
                        "schema_version": 1,
                        "deployment_record_id": "deployment-verireel-testing-run-42-attempt-1",
                        "migration_status": "success",
                        "verification_status": "success",
                        "owner_routes_status": "success",
                    },
                },
                authorization="",
                headers={"Cookie": cookie},
            )

        self.assertEqual(status_code, 401)
        self.assertEqual(payload["error"]["code"], "authentication_required")

    def test_human_session_role_is_revalidated_against_active_policy(self) -> None:
        admin_policy = LaunchplaneAuthzPolicy.model_validate(
            {"github_humans": [{"logins": ["alice"], "roles": ["admin"]}]}
        )
        session_manager = _fastapi_human_session_manager()
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            store = _HostedAuthzPolicyStore(database_url=database_url)
            store.ensure_schema()
            admin_record = LaunchplaneAuthzPolicyRecord(
                record_id="authz-human-admin",
                source="test:admin",
                updated_at="2026-07-18T00:00:00Z",
                policy=admin_policy,
            )
            store.write_authz_policy_record(admin_record)
            authz_policy_runtime = LaunchplaneAuthzPolicyRuntime(
                admin_policy,
                policy_sha256=admin_record.policy_sha256,
                source="db",
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=admin_policy,
                authz_policy_runtime=authz_policy_runtime,
                human_session_manager=session_manager,
                record_store_factory=lambda: store,
            )
            cookie = _fastapi_signed_in_cookie(session_manager, role="admin")
            read_only_policy = LaunchplaneAuthzPolicy.model_validate(
                {"github_humans": [{"logins": ["alice"], "roles": ["read_only"]}]}
            )
            store.write_authz_policy_record(
                LaunchplaneAuthzPolicyRecord(
                    record_id="authz-human-read-only",
                    source="test:demoted",
                    updated_at="2026-07-18T00:01:00Z",
                    policy=read_only_policy,
                )
            )

            demoted_status, demoted_payload = _invoke_app(
                app,
                method="GET",
                path="/v1/auth/session",
                authorization="",
                headers={"Cookie": cookie},
            )
            revoked_policy = LaunchplaneAuthzPolicy()
            store.write_authz_policy_record(
                LaunchplaneAuthzPolicyRecord(
                    record_id="authz-human-revoked",
                    source="test:revoked",
                    updated_at="2026-07-18T00:02:00Z",
                    policy=revoked_policy,
                )
            )
            try:
                revoked_status, revoked_payload = _invoke_app(
                    app,
                    method="GET",
                    path="/v1/auth/session",
                    authorization="",
                    headers={"Cookie": cookie},
                )
            finally:
                store.close()

        self.assertEqual(demoted_status, 200)
        self.assertEqual(demoted_payload["identity"]["role"], "read_only")
        self.assertEqual(revoked_status, 401)
        self.assertEqual(revoked_payload["error"]["code"], "authentication_required")

    def test_hosted_human_session_requires_fresh_github_authorization_claims(self) -> None:
        now = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
        policy = LaunchplaneAuthzPolicy.model_validate(
            {"github_humans": [{"logins": ["alice"], "roles": ["admin"]}]}
        )
        session_store = InMemoryHumanSessionStore()
        session_manager = HumanSessionManager(
            config=_github_oauth_config(),
            session_store=session_store,
            now=lambda: now,
        )
        stale_session = LaunchplaneHumanSession(
            session_id="stale-github-claims",
            identity=_human_identity(role="admin"),
            created_at=now - timedelta(hours=24),
            expires_at=now + timedelta(days=13),
        )
        session_store.write_session(stale_session)
        cookie = session_manager.session_cookie_header(stale_session)

        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            store = _HostedAuthzPolicyStore(database_url=database_url)
            store.ensure_schema()
            policy_record = LaunchplaneAuthzPolicyRecord(
                record_id="authz-human-current",
                source="test:current",
                updated_at="2026-07-18T12:00:00Z",
                policy=policy,
            )
            store.write_authz_policy_record(policy_record)
            app = create_launchplane_fastapi_test_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                authz_policy_runtime=LaunchplaneAuthzPolicyRuntime(
                    policy,
                    policy_sha256=policy_record.policy_sha256,
                    source="db",
                ),
                human_session_manager=session_manager,
                record_store_factory=lambda: store,
            )
            try:
                status_code, payload = _invoke_app(
                    app,
                    method="GET",
                    path="/v1/auth/session",
                    authorization="",
                    headers={"Cookie": cookie},
                )
            finally:
                store.close()

        self.assertEqual(status_code, 401)
        self.assertEqual(payload["error"]["code"], "authentication_required")
        self.assertIsNone(session_store.read_session(stale_session.session_id))


class LaunchplaneServiceTests(unittest.TestCase):
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
            app = create_launchplane_fastapi_test_app(
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
            app = create_launchplane_fastapi_test_app(
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
            session_manager = _fastapi_human_session_manager()
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
            app = create_launchplane_fastapi_test_app(
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
            cookie = session_manager.session_cookie_header(human_session)

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
                    **_fastapi_browser_mutation_headers(session_manager, cookie),
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
            app = create_launchplane_fastapi_test_app(
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
            app = create_launchplane_fastapi_test_app(
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
            app = create_launchplane_fastapi_test_app(
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
            app = create_launchplane_fastapi_test_app(
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

    def test_preview_pr_feedback_hydrates_ready_url_from_preview_record(self) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch(
                "control_plane.workflows.preview_pr_feedback.resolve_launchplane_github_token",
                return_value="github-token",
            ),
            patch(
                "control_plane.workflows.preview_pr_feedback.find_github_issue_comment_by_marker",
                return_value=None,
            ),
            patch(
                "control_plane.workflows.preview_pr_feedback.github_api_request",
                return_value={"user": {"login": "author"}, "head": {"ref": "pr-42"}},
            ),
            patch(
                "control_plane.workflows.preview_pr_feedback.create_github_issue_comment",
                return_value={"id": 987, "html_url": "https://github.example/comment"},
            ) as create_comment,
        ):
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            store.write_preview_record(
                PreviewRecord(
                    preview_id="preview-syo-pr-42",
                    context="sellyouroutboard-testing",
                    anchor_repo="sellyouroutboard",
                    anchor_pr_number=42,
                    anchor_pr_url="https://github.com/cbusillo/sellyouroutboard/pull/42",
                    preview_label="preview",
                    canonical_url="https://pr-42.syo-preview.example.test",
                    state="active",
                    created_at="2026-05-03T15:00:00Z",
                    updated_at="2026-05-03T15:05:00Z",
                    eligible_at="2026-05-03T15:00:00Z",
                    active_generation_id="generation-syo-pr-42",
                    serving_generation_id="generation-syo-pr-42",
                    latest_generation_id="generation-syo-pr-42",
                )
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
            app = create_launchplane_fastapi_test_app(
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
            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/previews/pr-feedback",
                payload={
                    "schema_version": 1,
                    "product": "sellyouroutboard",
                    "source": "workflow",
                    "repository": "cbusillo/sellyouroutboard",
                    "anchor_repo": "sellyouroutboard",
                    "anchor_pr_number": 42,
                    "anchor_pr_url": "https://github.com/cbusillo/sellyouroutboard/pull/42",
                    "status": "ready",
                    "run_url": "https://github.com/cbusillo/sellyouroutboard/actions/runs/42",
                },
                headers={"Idempotency-Key": "preview-pr-feedback-ready-hydrate-url"},
            )

        self.assertEqual(status_code, 202, payload)
        self.assertEqual(
            payload["result"]["preview_url"],
            "https://pr-42.syo-preview.example.test",
        )
        self.assertEqual(payload["result"]["delivery_status"], "delivered", payload)
        create_comment.assert_called_once()
        self.assertIn(
            "https://pr-42.syo-preview.example.test",
            create_comment.call_args.kwargs["body"],
        )

    def test_preview_pr_feedback_ready_requires_active_preview_url(self) -> None:
        with (
            TemporaryDirectory() as temporary_directory_name,
            patch(
                "control_plane.workflows.preview_pr_feedback.resolve_launchplane_github_token",
                return_value="github-token",
            ),
            patch(
                "control_plane.workflows.preview_pr_feedback.find_github_issue_comment_by_marker",
                return_value=None,
            ),
            patch(
                "control_plane.workflows.preview_pr_feedback.github_api_request",
                return_value={"user": {"login": "author"}, "head": {"ref": "pr-42"}},
            ),
            patch(
                "control_plane.workflows.preview_pr_feedback.create_github_issue_comment",
                return_value={"id": 987, "html_url": "https://github.example/comment"},
            ) as create_comment,
        ):
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_product_profile_payload())
            )
            store.write_preview_record(
                PreviewRecord(
                    preview_id="preview-syo-pr-42",
                    context="sellyouroutboard-testing",
                    anchor_repo="sellyouroutboard",
                    anchor_pr_number=42,
                    anchor_pr_url="https://github.com/cbusillo/sellyouroutboard/pull/42",
                    preview_label="preview",
                    canonical_url="https://failed-pr-42.syo-preview.example.test",
                    state="failed",
                    created_at="2026-05-03T15:00:00Z",
                    updated_at="2026-05-03T15:05:00Z",
                    eligible_at="2026-05-03T15:00:00Z",
                    latest_generation_id="generation-syo-pr-42",
                )
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
            app = create_launchplane_fastapi_test_app(
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
                "source": "workflow",
                "repository": "cbusillo/sellyouroutboard",
                "anchor_repo": "sellyouroutboard",
                "anchor_pr_number": 42,
                "anchor_pr_url": "https://github.com/cbusillo/sellyouroutboard/pull/42",
                "status": "ready",
                "run_url": "https://github.com/cbusillo/sellyouroutboard/actions/runs/42",
            }
            headers = {"Idempotency-Key": "preview-pr-feedback-ready-retry-after-active"}

            failed_status, failed_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/previews/pr-feedback",
                payload=request_payload,
                headers=headers,
            )
            store.write_preview_record(
                PreviewRecord(
                    preview_id="preview-syo-pr-42",
                    context="sellyouroutboard-testing",
                    anchor_repo="sellyouroutboard",
                    anchor_pr_number=42,
                    anchor_pr_url="https://github.com/cbusillo/sellyouroutboard/pull/42",
                    preview_label="preview",
                    canonical_url="https://pr-42.syo-preview.example.test",
                    state="active",
                    created_at="2026-05-03T15:00:00Z",
                    updated_at="2026-05-03T15:06:00Z",
                    eligible_at="2026-05-03T15:00:00Z",
                    active_generation_id="generation-syo-pr-42",
                    serving_generation_id="generation-syo-pr-42",
                    latest_generation_id="generation-syo-pr-42",
                )
            )
            ready_status, ready_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/previews/pr-feedback",
                payload=request_payload,
                headers=headers,
            )

        self.assertEqual(failed_status, 409, failed_payload)
        self.assertEqual(failed_payload["error"]["code"], "preview_url_unavailable")
        self.assertEqual(ready_status, 202, ready_payload)
        self.assertEqual(
            ready_payload["result"]["preview_url"],
            "https://pr-42.syo-preview.example.test",
        )
        create_comment.assert_called_once()

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

    def test_service_export_openapi_writes_canonical_schema(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            output_path = Path(temporary_directory_name) / "openapi.json"

            result = runner.invoke(
                CLI_MAIN,
                [
                    "service",
                    "export-openapi",
                    "--output",
                    str(output_path),
                ],
            )

            payload = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertEqual(result.output.strip(), str(output_path))
        self.assertIn("/v1/drivers", payload["paths"])
        self.assertIn(
            "/v1/products/{product}/environments/{environment}/config-status",
            payload["paths"],
        )
        self.assertNotIn('"examples"', json.dumps(payload, sort_keys=True))

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
            app = create_launchplane_fastapi_test_app(
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
            app = create_launchplane_fastapi_test_app(
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
            app = create_launchplane_fastapi_test_app(
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
            app = create_launchplane_fastapi_test_app(
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
            app = create_launchplane_fastapi_test_app(
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
            app = create_launchplane_fastapi_test_app(
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
                "control_plane.dokploy_target_setup_http.dokploy_source.read_dokploy_config",
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
                    "control_plane.dokploy_target_setup_http.dokploy_source.read_dokploy_config",
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
                    "control_plane.dokploy_target_setup_http.dokploy_compose.ensure_compose_web_domain_route",
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
                    "control_plane.dokploy_target_setup_http.dokploy_source.read_dokploy_config",
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
                    "control_plane.dokploy_target_setup_http.dokploy_source.read_dokploy_config",
                    return_value=("https://dokploy.example.invalid", "token"),
                ),
                patch(
                    "control_plane.dokploy_target_setup_http.dokploy_compose.ensure_compose_web_domain_route",
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
                    "control_plane.dokploy_target_setup_http.dokploy_source.read_dokploy_config",
                    return_value=("https://dokploy.example.invalid", "token"),
                ),
                patch(
                    "control_plane.dokploy_target_setup_http.dokploy_compose.ensure_compose_web_domain_route"
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
                    "control_plane.dokploy_target_setup_http.dokploy_source.read_dokploy_config",
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
                    "control_plane.dokploy_target_setup_http.dokploy_source.read_dokploy_config",
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
                    "control_plane.dokploy_target_setup_http.dokploy_source.read_dokploy_config",
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
                "control_plane.dokploy_target_setup_http.dokploy_source.read_dokploy_config",
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
                "control_plane.dokploy_target_setup_http.dokploy_source.read_dokploy_config",
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
                    "control_plane.dokploy_target_setup_http.dokploy_source.read_dokploy_config",
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
                    "control_plane.dokploy_target_setup_http.dokploy_source.read_dokploy_config",
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
            app = create_launchplane_fastapi_test_app(
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
            app = create_launchplane_fastapi_test_app(
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
            app = create_launchplane_fastapi_test_app(
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

    def test_odoo_preview_refresh_route_is_not_supported(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            FilesystemRecordStore(state_dir=state_dir).write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(_odoo_preview_profile_payload())
            )
            app = create_launchplane_fastapi_test_app(
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
            self.assertEqual(payload["status"], "rejected")
            self.assertEqual(payload["error"]["code"], "not_found")

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

    def test_service_refreshes_factory_backed_authz_policy_before_authorization(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            workflow_ref = (
                "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main"
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/launchplane",
                            "workflow_refs": [workflow_ref],
                            "event_names": ["workflow_dispatch"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["launchplane_service.read"],
                        }
                    ]
                }
            )
            store = _HostedAuthzPolicyStore(database_url=database_url)
            store.ensure_schema()
            current_record = LaunchplaneAuthzPolicyRecord(
                record_id="seed",
                source="test:initial",
                updated_at="2026-07-18T00:00:00Z",
                policy_sha256=authz_policy_sha256(policy),
                policy=policy,
            )
            store.write_authz_policy_record(current_record)
            authz_policy_runtime = LaunchplaneAuthzPolicyRuntime(
                policy,
                policy_sha256=current_record.policy_sha256,
                source="db",
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=workflow_ref,
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                authz_policy_runtime=authz_policy_runtime,
                control_plane_root_path=root,
                record_store_factory=lambda: store,
            )
            initial_status_code, initial_payload = _invoke_app(
                app,
                method="GET",
                path="/v1/service/runtime",
            )
            replacement_policy = policy.model_copy(
                update={
                    "schema_version": 2,
                    "github_actions": (),
                }
            )
            replacement_record = LaunchplaneAuthzPolicyRecord(
                record_id=build_authz_policy_record_id(
                    revision=2,
                    policy_sha256=authz_policy_sha256(replacement_policy),
                ),
                revision=2,
                source="test:replacement",
                updated_at="2026-07-18T00:01:00Z",
                policy_sha256=authz_policy_sha256(replacement_policy),
                policy=replacement_policy,
            )
            store.write_authz_policy_record(replacement_record)
            try:
                status_code, payload = _invoke_app(
                    app,
                    method="GET",
                    path="/v1/service/runtime",
                )
            finally:
                store.close()

        self.assertEqual(initial_status_code, 200)
        self.assertEqual(
            initial_payload["runtime"]["authz_policy_sha256"], current_record.policy_sha256
        )
        self.assertEqual(initial_payload["runtime"]["database_schema_revision"], "")
        self.assertEqual(
            initial_payload["runtime"]["compatible_database_schema_revisions"],
            ["f4c6e8a0b2d4"],
        )
        self.assertEqual(
            initial_payload["runtime"]["schema_migration_target_revision"],
            "f4c6e8a0b2d4",
        )
        self.assertEqual(status_code, 403)
        self.assertEqual(payload["error"]["code"], "authorization_denied")
        self.assertEqual(authz_policy_runtime.policy_sha256, replacement_record.policy_sha256)

    def test_service_authz_refresh_failure_returns_service_unavailable(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/launchplane",
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["launchplane_service.read"],
                        }
                    ]
                }
            )
            store = _HostedAuthzPolicyStore(database_url=database_url)
            store.ensure_schema()
            store.write_authz_policy_record(
                LaunchplaneAuthzPolicyRecord(
                    record_id="seed",
                    source="test:initial",
                    updated_at="2026-07-18T00:00:00Z",
                    policy=policy,
                )
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity(repository="cbusillo/launchplane")),
                authz_policy=policy,
                authz_policy_runtime=LaunchplaneAuthzPolicyRuntime(policy),
                control_plane_root_path=root,
                record_store_factory=lambda: store,
            )
            try:
                with patch.object(
                    store,
                    "list_authz_policy_records",
                    side_effect=RuntimeError("database unavailable"),
                ):
                    status_code, payload = _invoke_app(
                        app,
                        method="GET",
                        path="/v1/service/runtime",
                    )
            finally:
                store.close()

        self.assertEqual(status_code, 503)
        self.assertEqual(payload["error"]["code"], "authz_policy_unavailable")

    def test_managed_authz_reconcile_migrates_policy_and_rejects_stale_digest(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            workflow_ref = (
                "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main"
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/launchplane",
                            "workflow_refs": [workflow_ref],
                            "event_names": ["workflow_dispatch"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["authz_policy_grant.write"],
                        },
                        {
                            "repository": "cbusillo/launchplane",
                            "workflow_refs": [workflow_ref],
                            "event_names": ["workflow_dispatch"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["product_profile.read"],
                        },
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=workflow_ref,
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )
            dry_run_payload = {
                "schema_version": 2,
                "product": "launchplane",
                "mode": "dry_run",
                "managed_set_id": "operator.launchplane",
                "schema_migration": "migrate_v1_to_v2",
                "unmanaged_adoption": "adopt_matching",
                "reason": "Adopt the active service grant into managed policy authority.",
                "related_issue": "cbusillo/launchplane#1774",
                "desired_policy": {
                    "schema_version": 2,
                    "github_actions": [
                        {
                            "managed_set_id": "operator.launchplane",
                            "managed_rule_id": "profile.read",
                            "repository": "cbusillo/launchplane",
                            "repository_id": "1001",
                            "repository_owner_id": "2001",
                            "workflow_refs": [workflow_ref],
                            "event_names": ["workflow_dispatch"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["product_profile.read"],
                        }
                    ],
                },
            }

            dry_run_status, dry_run_response = _invoke_app(
                app,
                method="POST",
                path="/v1/authz-policies/managed-rule-sets/reconcile",
                payload=dry_run_payload,
            )
            request_payload = {
                **dry_run_payload,
                "mode": "apply",
                "reviewed_plan_sha256": dry_run_response["result"]["diff"]["plan_sha256"],
            }
            missing_key_status, missing_key_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/authz-policies/managed-rule-sets/reconcile",
                payload=request_payload,
            )
            status_code, payload = _invoke_app(
                app,
                method="POST",
                path="/v1/authz-policies/managed-rule-sets/reconcile",
                payload=request_payload,
                headers={"Idempotency-Key": "managed-authz-reconcile"},
            )
            replay_status, replay_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/authz-policies/managed-rule-sets/reconcile",
                payload=request_payload,
                headers={"Idempotency-Key": "managed-authz-reconcile"},
            )
            conflict_status, conflict_payload = _invoke_app(
                app,
                method="POST",
                path="/v1/authz-policies/managed-rule-sets/reconcile",
                payload=request_payload,
                headers={"Idempotency-Key": "managed-authz-reconcile-conflict"},
            )
            active_status, active_payload = _invoke_app(
                app,
                method="GET",
                path="/v1/authz-policies/active",
            )
            store = PostgresRecordStore(database_url=database_url)
            try:
                active_records = store.list_authz_policy_records(status="active")
                superseded_records = store.list_authz_policy_records(status="superseded")
            finally:
                store.close()

        self.assertEqual(dry_run_status, 202)
        self.assertEqual(missing_key_status, 400)
        self.assertEqual(missing_key_payload["error"]["code"], "idempotency_key_required")
        self.assertEqual(status_code, 202)
        self.assertEqual(replay_status, 202)
        self.assertEqual(replay_payload["records"], payload["records"])
        self.assertEqual(replay_payload["result"], payload["result"])
        self.assertTrue(payload["result"]["changed"])
        self.assertTrue(payload["result"]["diff"]["schema_migrated"])
        self.assertEqual(payload["result"]["diff"]["adopted_rule_count"], 1)
        self.assertEqual(conflict_status, 409)
        self.assertEqual(conflict_payload["error"]["code"], "authz_policy_conflict")
        self.assertEqual(active_status, 200)
        self.assertEqual(active_payload["policy"]["revision"], 2)
        self.assertEqual(active_payload["policy"]["managed_rule_count"], 1)
        self.assertEqual(active_payload["policy"]["unmanaged_rule_count"], 1)
        self.assertEqual(
            active_payload["policy"]["github_actions_privileged_unpinned_reusable_rule_count"],
            1,
        )
        self.assertEqual(
            active_payload["policy"]["managed_rules"][0]["managed_rule_id"],
            "profile.read",
        )
        self.assertNotIn("repository", active_payload["policy"]["managed_rules"][0])
        self.assertEqual(len(active_records), 1)
        self.assertEqual(active_records[0].policy.schema_version, 2)
        self.assertEqual(
            {
                rule.managed_rule_id
                for rule in active_records[0].policy.github_actions
                if rule.managed_rule_id is not None
            },
            {"profile.read"},
        )
        self.assertEqual(len(superseded_records), 1)

    def test_managed_authz_reconcile_noop_completes_replay_without_policy_history(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            workflow_ref = (
                "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main"
            )
            managed_rule = {
                "managed_set_id": "operator.launchplane",
                "managed_rule_id": "profile.read",
                "repository": "cbusillo/launchplane",
                "repository_id": "1001",
                "repository_owner_id": "2001",
                "workflow_refs": [workflow_ref],
                "event_names": ["workflow_dispatch"],
                "products": ["launchplane"],
                "contexts": ["launchplane"],
                "actions": ["product_profile.read"],
            }
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "schema_version": 2,
                    "github_actions": [
                        {
                            "repository": "cbusillo/launchplane",
                            "workflow_refs": [workflow_ref],
                            "event_names": ["workflow_dispatch"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["authz_policy_grant.write"],
                        },
                        managed_rule,
                    ],
                }
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=workflow_ref,
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                control_plane_root_path=root,
                database_url=database_url,
            )
            dry_run_payload = {
                "schema_version": 2,
                "product": "launchplane",
                "mode": "dry_run",
                "managed_set_id": "operator.launchplane",
                "reason": "Confirm the managed set is converged.",
                "desired_policy": {
                    "schema_version": 2,
                    "github_actions": [managed_rule],
                },
            }
            dry_run_status, dry_run_response = _invoke_app(
                app,
                method="POST",
                path="/v1/authz-policies/managed-rule-sets/reconcile",
                payload=dry_run_payload,
            )
            apply_payload = {
                **dry_run_payload,
                "mode": "apply",
                "reviewed_plan_sha256": dry_run_response["result"]["diff"]["plan_sha256"],
            }
            apply_status, apply_response = _invoke_app(
                app,
                method="POST",
                path="/v1/authz-policies/managed-rule-sets/reconcile",
                payload=apply_payload,
                headers={"Idempotency-Key": "managed-authz-noop"},
            )
            replay_status, replay_response = _invoke_app(
                app,
                method="POST",
                path="/v1/authz-policies/managed-rule-sets/reconcile",
                payload=apply_payload,
                headers={"Idempotency-Key": "managed-authz-noop"},
            )
            store = PostgresRecordStore(database_url=database_url)
            try:
                active_records = store.list_authz_policy_records(status="active")
                superseded_records = store.list_authz_policy_records(status="superseded")
            finally:
                store.close()

        self.assertEqual(dry_run_status, 202)
        self.assertFalse(dry_run_response["result"]["changed"])
        self.assertEqual(apply_status, 202)
        self.assertFalse(apply_response["result"]["changed"])
        self.assertEqual(replay_status, 202)
        self.assertEqual(replay_response["result"], apply_response["result"])
        self.assertEqual(len(active_records), 1)
        self.assertEqual(active_records[0].revision, 1)
        self.assertEqual(superseded_records, ())

    def test_managed_authz_reconcile_allows_admin_human_session(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "schema_version": 2,
                    "github_humans": [
                        {
                            "logins": ["alice"],
                            "roles": ["admin"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["authz_policy_grant.write"],
                        }
                    ],
                }
            )
            session_manager = _fastapi_human_session_manager()
            app = create_launchplane_fastapi_test_app(
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
                path="/v1/authz-policies/managed-rule-sets/reconcile",
                payload={
                    "schema_version": 2,
                    "product": "launchplane",
                    "mode": "dry_run",
                    "managed_set_id": "operator.empty",
                    "desired_policy": {"schema_version": 2},
                },
                authorization="",
                headers=_fastapi_browser_mutation_headers(session_manager, cookie),
            )

        self.assertEqual(status_code, 202)
        self.assertFalse(payload["result"]["changed"])
        self.assertEqual(payload["result"]["audit"]["operator"], {"type": "github_human"})

    def test_managed_authz_reconcile_rejects_non_admin_workflow_authority(self) -> None:
        for action in ("product_profile.read", "launchplane_service_deploy.execute"):
            with self.subTest(action=action), TemporaryDirectory() as temporary_directory_name:
                root = Path(temporary_directory_name)
                database_url = _sqlite_database_url(root / "launchplane.sqlite3")
                policy = LaunchplaneAuthzPolicy.model_validate(
                    {
                        "schema_version": 2,
                        "github_actions": [
                            {
                                "repository": "cbusillo/launchplane",
                                "actions": [action],
                            }
                        ],
                    }
                )
                app = create_launchplane_fastapi_test_app(
                    state_dir=root / "state",
                    verifier=_StubVerifier(
                        _identity(
                            repository="cbusillo/launchplane",
                            workflow_ref=(
                                "cbusillo/launchplane/.github/workflows/"
                                "deploy-launchplane.yml@refs/heads/main"
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
                    path="/v1/authz-policies/managed-rule-sets/reconcile",
                    payload={
                        "schema_version": 2,
                        "product": "launchplane",
                        "mode": "dry_run",
                        "managed_set_id": "operator.empty",
                        "desired_policy": {"schema_version": 2},
                    },
                )

            self.assertEqual(status_code, 403)
            self.assertEqual(payload["error"]["code"], "authorization_denied")

    def test_exact_authz_write_routes_are_not_registered(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_fastapi_test_app(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                control_plane_root_path=root,
                database_url=_sqlite_database_url(root / "launchplane.sqlite3"),
            )

            paths = set(app.openapi()["paths"])

        self.assertIn("/v1/authz-policies/managed-rule-sets/reconcile", paths)
        self.assertIn("/v1/authz-policies/active", paths)
        self.assertTrue(
            {
                "/v1/authz-policies/github-actions/grants",
                "/v1/authz-policies/github-actions/removals",
                "/v1/authz-policies/github-humans/grants",
                "/v1/authz-policies/terminal-agents/grants",
                "/v1/authz-policies/local-operators/grants",
                "/v1/authz-policies/local-admins/grants",
            }.isdisjoint(paths)
        )

    def test_service_refreshes_active_authz_policy_revision_before_authorization(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(root / "launchplane.sqlite3")
            workflow_ref = (
                "cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main"
            )
            policy = LaunchplaneAuthzPolicy.model_validate(
                {
                    "github_actions": [
                        {
                            "repository": "cbusillo/launchplane",
                            "workflow_refs": [workflow_ref],
                            "event_names": ["workflow_dispatch"],
                            "products": ["launchplane"],
                            "contexts": ["launchplane"],
                            "actions": ["launchplane_service.read"],
                        }
                    ]
                }
            )
            store = _HostedAuthzPolicyStore(database_url=database_url)
            store.ensure_schema()
            current_record = store.seed_authz_policy_if_absent(
                LaunchplaneAuthzPolicyRecord(
                    record_id="seed",
                    source="test:initial",
                    updated_at="2026-07-18T00:00:00Z",
                    policy=policy,
                )
            )
            authz_policy_runtime = LaunchplaneAuthzPolicyRuntime(
                policy,
                policy_sha256=current_record.policy_sha256,
                source="db",
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=root / "state",
                verifier=_StubVerifier(
                    _identity(
                        repository="cbusillo/launchplane",
                        workflow_ref=workflow_ref,
                        event_name="workflow_dispatch",
                    )
                ),
                authz_policy=policy,
                authz_policy_runtime=authz_policy_runtime,
                control_plane_root_path=root,
                record_store_factory=lambda: store,
            )
            replacement_policy = policy.model_copy(update={"schema_version": 2})
            replacement_record = LaunchplaneAuthzPolicyRecord(
                record_id=build_authz_policy_record_id(
                    revision=2,
                    policy_sha256=authz_policy_sha256(replacement_policy),
                ),
                revision=2,
                source="test:replacement",
                updated_at="2026-07-18T00:01:00Z",
                policy=replacement_policy,
            )
            store.compare_and_write_authz_policy_record(
                expected_record=current_record,
                replacement_record=replacement_record,
            )
            try:
                status_code, payload = _invoke_app(
                    app,
                    method="GET",
                    path="/v1/service/runtime",
                )
            finally:
                store.close()

        self.assertEqual(status_code, 200)
        self.assertEqual(
            payload["runtime"]["authz_policy_sha256"], replacement_record.policy_sha256
        )
        self.assertEqual(authz_policy_runtime.policy_sha256, replacement_record.policy_sha256)

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
            app = create_launchplane_fastapi_test_app(
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
                    "control_plane.dokploy.api.fetch_dokploy_target_payload",
                    return_value={
                        "applicationId": "application-syo-prod",
                        "name": "syo-prod-app",
                        "env": "CONTACT_EMAIL_MODE=resend\n",
                    },
                ),
                patch("control_plane.dokploy.api.update_dokploy_target_env") as update_env,
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
            app = create_launchplane_fastapi_test_app(
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
                    "control_plane.dokploy.api.fetch_dokploy_target_payload",
                    return_value={
                        "applicationId": "application-syo-prod",
                        "name": "syo-prod-app",
                        "env": "CONTACT_EMAIL_MODE=resend\n",
                    },
                ),
                patch("control_plane.dokploy.api.update_dokploy_target_env") as update_env,
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
            app = create_launchplane_fastapi_test_app(
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
                    "control_plane.dokploy.api.fetch_dokploy_target_payload",
                    side_effect=fetch_target_payload,
                ),
                patch(
                    "control_plane.dokploy.api.update_dokploy_target_env",
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
            app = create_launchplane_fastapi_test_app(
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
                    "control_plane.dokploy.api.fetch_dokploy_target_payload",
                    return_value={
                        "applicationId": "application-syo-prod",
                        "name": "syo-prod-app",
                        "env": "GOOGLE_ANALYTICS_MEASUREMENT_ID=G-9KRMER45KG\n",
                    },
                ),
                patch("control_plane.dokploy.api.update_dokploy_target_env") as update_env,
                patch(
                    "control_plane.dokploy.api.latest_deployment_for_target",
                    return_value={"deploymentId": "before"},
                ),
                patch("control_plane.dokploy.api.trigger_deployment") as trigger_deployment,
                patch(
                    "control_plane.dokploy.api.wait_for_target_deployment",
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
                    "control_plane.dokploy.source.read_control_plane_dokploy_source_of_truth",
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
                    "control_plane.dokploy.source.read_dokploy_config",
                    return_value=("https://dokploy.example.com", "dokploy-token"),
                ),
                patch(
                    "control_plane.dokploy.api.fetch_dokploy_target_payload",
                    return_value={
                        "applicationId": "application-syo-prod",
                        "name": "syo-prod-app",
                        "env": "CONTACT_EMAIL_MODE=resend\n",
                    },
                ),
                patch("control_plane.dokploy.api.update_dokploy_target_env") as update_env,
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
            app = create_launchplane_fastapi_test_app(
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
            app = create_launchplane_fastapi_test_app(
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
            app = create_launchplane_fastapi_test_app(
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
            app = create_launchplane_fastapi_test_app(
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
            app = create_launchplane_fastapi_test_app(
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
                "control_plane.verireel_nonprod_http.execute_verireel_stable_deploy",
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

    def test_verireel_testing_deploy_replay_scrubs_retired_target_type_alias(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
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
            identity = _identity(
                workflow_ref=("every/verireel/.github/workflows/publish-image.yml@refs/heads/main"),
                event_name="push",
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(identity),
                authz_policy=policy,
                control_plane_root_path=root,
            )
            request_payload = {
                "product": "verireel",
                "deploy": {
                    "artifact_id": "ghcr.io/every/verireel-app:sha-abcdef1234567890",
                    "source_git_ref": "abcdef1234567890",
                },
            }

            with patch(
                "control_plane.verireel_nonprod_http.execute_verireel_stable_deploy",
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
                first_status_code, first_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/verireel/testing-deploy",
                    payload=request_payload,
                    headers={"Idempotency-Key": "verireel-testing-deploy-retired-alias"},
                )
                idempotency_record = store.read_idempotency_record(
                    scope="|".join(
                        (
                            identity.repository,
                            identity.workflow_ref or identity.job_workflow_ref,
                            identity.subject,
                        )
                    ),
                    route_path="/v1/drivers/verireel/testing-deploy",
                    idempotency_key="verireel-testing-deploy-retired-alias",
                )
                self.assertIsNotNone(idempotency_record)
                assert idempotency_record is not None
                legacy_response_payload = idempotency_record.response_payload
                legacy_result_payload = legacy_response_payload.get("result")
                self.assertIsInstance(legacy_result_payload, dict)
                assert isinstance(legacy_result_payload, dict)
                legacy_result_payload["target_type"] = "application"
                legacy_records_payload = legacy_response_payload.get("records")
                self.assertIsInstance(legacy_records_payload, dict)
                assert isinstance(legacy_records_payload, dict)
                legacy_records_payload["target_type"] = "application"
                store.write_idempotency_record(
                    idempotency_record.model_copy(
                        update={"response_payload": legacy_response_payload}, deep=True
                    )
                )
                second_status_code, second_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/verireel/testing-deploy",
                    payload=request_payload,
                    headers={"Idempotency-Key": "verireel-testing-deploy-retired-alias"},
                )

            self.assertEqual(first_status_code, 202)
            self.assertNotIn("target_type", first_payload["result"])
            self.assertEqual(second_status_code, 202)
            self.assertTrue(second_payload["replayed"])
            self.assertEqual(second_payload["result"]["target_category"], "application")
            self.assertEqual(second_payload["result"]["provider_target_type"], "application")
            self.assertNotIn("target_type", second_payload["records"])
            self.assertNotIn("target_type", second_payload["result"])
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
            app = create_launchplane_fastapi_test_app(
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
                record_store_factory=lambda: FilesystemRecordStore(state_dir=state_dir),
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
            app = create_launchplane_fastapi_test_app(
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
                record_store_factory=lambda: FilesystemRecordStore(state_dir=state_dir),
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
            app = create_launchplane_fastapi_test_app(
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
                record_store_factory=lambda: FilesystemRecordStore(state_dir=root / "state"),
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
            app = create_launchplane_fastapi_test_app(
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
            app = create_launchplane_fastapi_test_app(
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
                record_store_factory=lambda: FilesystemRecordStore(state_dir=root / "state"),
            )

            with patch(
                "control_plane.verireel_read_http.resolve_verireel_stable_environment",
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
            app = create_launchplane_fastapi_test_app(
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
                record_store_factory=lambda: FilesystemRecordStore(state_dir=root / "state"),
            )

            with patch(
                "control_plane.verireel_read_http.execute_verireel_rollout_verification",
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
            app = create_launchplane_fastapi_test_app(
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
                record_store_factory=lambda: FilesystemRecordStore(state_dir=root / "state"),
            )

            with (
                patch(
                    "control_plane.verireel_read_http.resolve_verireel_stable_environment"
                ) as resolve_mock,
                patch(
                    "control_plane.verireel_read_http.execute_verireel_rollout_verification"
                ) as verify_mock,
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
            app = create_launchplane_fastapi_test_app(
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
                "control_plane.verireel_nonprod_http.execute_verireel_app_maintenance",
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
            app = create_launchplane_fastapi_test_app(
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
                "control_plane.verireel_nonprod_http.execute_verireel_app_maintenance",
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
            app = create_launchplane_fastapi_test_app(
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

    def test_verireel_app_maintenance_rejects_unowned_product_lane(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            profile_payload = _product_profile_payload("verireel-alias")
            profile_payload["driver_id"] = "verireel"
            profile_payload["repository"] = "every/verireel"
            profile_payload["lanes"] = (
                {
                    "instance": "testing",
                    "context": "verireel",
                    "base_url": "https://ver-testing.shinycomputers.com",
                    "health_url": "https://ver-testing.shinycomputers.com/api/health",
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
                                "every/verireel/.github/workflows/publish-image.yml@refs/heads/main"
                            ],
                            "event_names": ["push", "workflow_dispatch"],
                            "products": ["verireel-alias"],
                            "contexts": ["verireel-testing"],
                            "actions": ["verireel_app_maintenance.execute"],
                        }
                    ]
                }
            )
            app = create_launchplane_fastapi_test_app(
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
                "control_plane.verireel_nonprod_http.execute_verireel_app_maintenance"
            ) as execute_mock:
                status_code, payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/verireel/app-maintenance",
                    payload={
                        "product": "verireel-alias",
                        "maintenance": {
                            "context": "verireel-testing",
                            "instance": "preview",
                            "action": "grant-sponsored",
                            "intent": "remote-e2e-grant-sponsored",
                            "email": "creator@example.com",
                            "preview_slug": "pr-72",
                        },
                    },
                )

            self.assertEqual(status_code, 403)
            self.assertEqual(payload["error"]["code"], "product_driver_mismatch")
            execute_mock.assert_not_called()

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
            app = create_launchplane_fastapi_test_app(
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
            self.assertEqual(payload["error"]["message"], "Launchplane request validation failed.")

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
            app = create_launchplane_fastapi_test_app(
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

            with patch(
                "control_plane.verireel_read_http.execute_verireel_preview_inventory",
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
            app = create_launchplane_fastapi_test_app(
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
                    request_fingerprint=idempotency_request_fingerprint(
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
                "control_plane.verireel_read_http.execute_verireel_preview_inventory",
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
            app = create_launchplane_fastapi_test_app(
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
            app = create_launchplane_fastapi_test_app(
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
            app = create_launchplane_fastapi_test_app(
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
            app = create_launchplane_fastapi_test_app(
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
            app = create_launchplane_fastapi_test_app(
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
            app = create_launchplane_fastapi_test_app(
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
            app = create_launchplane_fastapi_test_app(
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
                "control_plane.verireel_prod_http.execute_verireel_stable_deploy",
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
            app = create_launchplane_fastapi_test_app(
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

    def test_verireel_prod_deploy_replay_scrubs_retired_target_type_alias(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
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
            identity = _identity(
                workflow_ref="every/verireel/.github/workflows/promote-image.yml@refs/heads/main",
                event_name="workflow_dispatch",
            )
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(identity),
                authz_policy=policy,
                control_plane_root_path=root,
            )
            request_payload = {
                "product": "verireel",
                "deploy": {
                    "instance": "prod",
                    "artifact_id": "ghcr.io/every/verireel-app:sha-abcdef1234567890",
                    "source_git_ref": "abcdef1234567890",
                },
            }

            with patch(
                "control_plane.verireel_prod_http.execute_verireel_stable_deploy",
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
                first_status_code, first_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/verireel/prod-deploy",
                    payload=request_payload,
                    headers={"Idempotency-Key": "verireel-prod-deploy-retired-alias"},
                )
                idempotency_record = store.read_idempotency_record(
                    scope="|".join(
                        (
                            identity.repository,
                            identity.workflow_ref or identity.job_workflow_ref,
                            identity.subject,
                        )
                    ),
                    route_path="/v1/drivers/verireel/prod-deploy",
                    idempotency_key="verireel-prod-deploy-retired-alias",
                )
                self.assertIsNotNone(idempotency_record)
                assert idempotency_record is not None
                legacy_response_payload = idempotency_record.response_payload
                legacy_result_payload = legacy_response_payload.get("result")
                self.assertIsInstance(legacy_result_payload, dict)
                assert isinstance(legacy_result_payload, dict)
                legacy_result_payload["target_type"] = "application"
                legacy_records_payload = legacy_response_payload.get("records")
                self.assertIsInstance(legacy_records_payload, dict)
                assert isinstance(legacy_records_payload, dict)
                legacy_records_payload["target_type"] = "application"
                store.write_idempotency_record(
                    idempotency_record.model_copy(
                        update={"response_payload": legacy_response_payload}, deep=True
                    )
                )
                second_status_code, second_payload = _invoke_app(
                    app,
                    method="POST",
                    path="/v1/drivers/verireel/prod-deploy",
                    payload=request_payload,
                    headers={"Idempotency-Key": "verireel-prod-deploy-retired-alias"},
                )

            self.assertEqual(first_status_code, 202)
            self.assertNotIn("target_type", first_payload["result"])
            self.assertEqual(second_status_code, 202)
            self.assertTrue(second_payload["replayed"])
            self.assertEqual(second_payload["result"]["target_category"], "application")
            self.assertEqual(second_payload["result"]["provider_target_type"], "application")
            self.assertNotIn("target_type", second_payload["records"])
            self.assertNotIn("target_type", second_payload["result"])
            execute_mock.assert_called_once()

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
            app = create_launchplane_fastapi_test_app(
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
                "control_plane.verireel_prod_http.execute_verireel_prod_promotion",
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
            app = create_launchplane_fastapi_test_app(
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
                "control_plane.verireel_prod_http.execute_verireel_prod_promotion",
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
            app = create_launchplane_fastapi_test_app(
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
                "control_plane.verireel_prod_http.execute_verireel_prod_promotion"
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

            self.assertEqual(status_code, 403)
            self.assertEqual(payload["error"]["code"], "product_driver_mismatch")
            execute_mock.assert_not_called()

    def test_verireel_prod_promotion_route_rejects_unowned_source_lane_first(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            profile_payload = _generic_site_profile_payload(product="video-site")
            profile_payload["display_name"] = "Video Site"
            profile_payload["driver_id"] = "verireel"
            profile_payload["lanes"] = (
                {
                    "instance": "prod",
                    "context": "video-site",
                    "base_url": "https://video.example",
                    "health_url": "https://video.example/healthz",
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
            app = create_launchplane_fastapi_test_app(
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
                "control_plane.verireel_prod_http.execute_verireel_prod_promotion"
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

            self.assertEqual(status_code, 403)
            self.assertEqual(payload["error"]["code"], "product_driver_mismatch")
            execute_mock.assert_not_called()

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
            app = create_launchplane_fastapi_test_app(
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
                "control_plane.odoo_artifact_publish_http.ingest_odoo_artifact_publish_evidence",
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
            app = create_launchplane_fastapi_test_app(
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
                "control_plane.odoo_artifact_publish_http.ingest_odoo_artifact_publish_evidence",
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

    def test_odoo_artifact_publish_driver_does_not_replay_failed_result(self) -> None:
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
            app = create_launchplane_fastapi_test_app(
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
                "control_plane.odoo_artifact_publish_http.ingest_odoo_artifact_publish_evidence",
                return_value=OdooArtifactPublishResult(
                    status="fail",
                    context="opw",
                    instance="testing",
                    artifact_id="artifact-opw-new",
                    image_repository="ghcr.io/cbusillo/odoo-tenant-opw",
                    image_digest="sha256:new",
                    source_commit="2719b363e1a434d890b2d75f0cb4ef629bc3a012",
                    error_message="manifest write failed",
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
            self.assertEqual(first_payload["result"]["status"], "fail")
            self.assertEqual(second_payload["result"]["status"], "fail")
            self.assertNotIn("replayed", second_payload)
            self.assertEqual(execute_mock.call_count, 2)

    def test_odoo_artifact_publish_driver_rejects_unauthorized_workflow(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            app = create_launchplane_fastapi_test_app(
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
            app = create_launchplane_fastapi_test_app(
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
            app = create_launchplane_fastapi_test_app(
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
                "control_plane.verireel_prod_http.execute_verireel_prod_rollback",
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
            app = create_launchplane_fastapi_test_app(
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
                "control_plane.verireel_prod_http.execute_verireel_prod_rollback",
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
            app = create_launchplane_fastapi_test_app(
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
                "control_plane.verireel_prod_http.execute_verireel_prod_rollback"
            ) as execute_mock:
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
            app = create_launchplane_fastapi_test_app(
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
            app = create_launchplane_fastapi_test_app(
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
                    "control_plane.verireel_prod_http.execute_verireel_prod_rollback",
                    side_effect=RuntimeError("driver exploded"),
                ),
                patch("control_plane.http_app._LOGGER.exception"),
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
            app = create_launchplane_fastapi_test_app(
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
                "control_plane.verireel_prod_http.enqueue_verireel_prod_backup_gate",
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
            app = create_launchplane_fastapi_test_app(
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
                "control_plane.verireel_prod_http.enqueue_verireel_prod_backup_gate",
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
            app = create_launchplane_fastapi_test_app(
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
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.verireel_read_http.execute_verireel_preview_refresh",
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
            app = create_launchplane_fastapi_test_app(
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
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.verireel_read_http.execute_verireel_preview_refresh",
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
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.verireel_read_http.execute_verireel_preview_refresh",
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
            app = create_launchplane_fastapi_test_app(
                state_dir=state_dir,
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
            )

            with patch(
                "control_plane.verireel_read_http.execute_verireel_preview_refresh",
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
            app = create_launchplane_fastapi_test_app(
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
            self.assertEqual(payload["error"]["message"], "Launchplane request validation failed.")

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
            app = create_launchplane_fastapi_test_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=policy,
                control_plane_root_path=root,
                record_store_factory=lambda: FilesystemRecordStore(state_dir=state_dir),
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
            app = create_launchplane_fastapi_test_app(
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
                "control_plane.verireel_read_http.execute_verireel_preview_destroy",
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
            app = create_launchplane_fastapi_test_app(
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
                "control_plane.verireel_read_http.execute_verireel_preview_destroy",
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
            app = create_launchplane_fastapi_test_app(
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
