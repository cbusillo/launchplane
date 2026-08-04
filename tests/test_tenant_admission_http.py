import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

import click

from control_plane.contracts.repository_human_admission import (
    REPOSITORY_HUMAN_ROLE_POLICY_WRITE_ACTION,
    TENANT_TECHNICAL_HUMAN_WAIVER_WRITE_ACTION,
    RepositoryHumanRolePolicyRecord,
    build_repository_human_role_policy_record_id,
)
from control_plane.contracts.trusted_maintenance import (
    TRUSTED_MAINTENANCE_POLICY_READ_ACTION,
    TRUSTED_MAINTENANCE_POLICY_WRITE_ACTION,
    TrustedMaintenanceActorRule,
    TrustedMaintenanceAllowedEvent,
    TrustedMaintenancePolicyRecord,
    build_trusted_maintenance_policy_record_id,
)
from control_plane.contracts.authz_policy_record import (
    LaunchplaneAuthzPolicyRecord,
    authz_policy_sha256,
    build_authz_policy_record_id,
)
from control_plane.contracts.tenant_merge_eligibility import (
    TenantMergeEligibilityEvidenceInputs,
    TenantMergeCandidate,
    TenantRepositoryClassificationLookup,
    TenantRepositoryClassificationRecord,
    build_tenant_repository_classification_record_id,
    evaluate_tenant_merge_eligibility,
)
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import (
    GitHubHumanIdentity,
    GitHubHumanPolicyRule,
    LaunchplaneAuthzPolicy,
    TerminalAgentIdentity,
)
from control_plane.service_human_auth import HumanSessionManager, InMemoryHumanSessionStore
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.tenant_admission_controller import (
    TenantAdmissionControllerRunOnceResult,
    TenantAdmissionPullRequestFacts,
    TenantAdmissionRequiredTechnicalCheck,
    TenantAdmissionTechnicalCheckSignal,
    TenantAdmissionTechnicalChecks,
)
from control_plane.tenant_admission_status import TenantAdmissionStatusReadModel
from tests.http_app_test_support import (
    _asgi_get,
    _asgi_request,
    _browser_mutation_headers,
    _github_oauth_config,
)
from tests.support.auth import _StubVerifier, _identity
from tests.test_tenant_admission_status import _path_result

PRODUCT = "launchplane"
CONTEXT = "production"
REPOSITORY_ID = "1001"
REPOSITORY_OWNER_ID = "2001"
REPOSITORY = "example/tenant-site"
CLASSIFIED_AT = "2026-07-31T11:00:00Z"
SOURCE = "operator"
REASON = "initial classification"
PULL_REQUEST_NUMBER = 69
HEAD_SHA = "a" * 40
BASE_SHA = "b" * 40


class _TestPostgresRecordStore(PostgresRecordStore):
    @property
    def database_dialect_name(self) -> str:
        return "postgresql"


def _postgres_store(
    root: Path,
    *,
    actions: tuple[str, ...] = (
        "tenant_repository_classification.read",
        "tenant_repository_classification.write",
        "repository_human_role_policy.read",
        "repository_human_role_policy.write",
    ),
    authz_policy_record: LaunchplaneAuthzPolicyRecord | None = None,
) -> PostgresRecordStore:
    root.mkdir(parents=True, exist_ok=True)
    store = _TestPostgresRecordStore(
        database_url=f"sqlite+pysqlite:///{root / 'launchplane.sqlite3'}"
    )
    store.ensure_schema()
    store.seed_authz_policy_if_absent(
        authz_policy_record
        or LaunchplaneAuthzPolicyRecord(
            record_id="test-tenant-admission-authz-policy",
            revision=1,
            status="active",
            source="test",
            updated_at="2026-07-31T00:00:00Z",
            policy=_authz_policy(actions=actions),
        )
    )
    return store


def _waiver_human_identity(
    *, github_id: int = 301, login: str = "human-301"
) -> GitHubHumanIdentity:
    return GitHubHumanIdentity(
        login=login,
        github_id=github_id,
        name="Human Owner",
        email="human-owner@example.com",
        organizations=frozenset(),
        teams=frozenset(),
        role="read_only",
    )


def _waiver_session_app(
    store: object,
    *,
    identity: GitHubHumanIdentity | None = None,
) -> tuple[Any, HumanSessionManager, Any]:
    session_manager = HumanSessionManager(
        config=_github_oauth_config(),
        session_store=InMemoryHumanSessionStore(),
    )
    human_session = session_manager.issue(identity or _waiver_human_identity())
    app = create_launchplane_fastapi_app(
        verifier=_StubVerifier(_identity()),
        authz_policy=_authz_policy(actions=()),
        record_store_factory=lambda: store,
        human_session_manager=session_manager,
    )
    return app, session_manager, human_session


def _trusted_maintenance_session_app(
    store: object,
    *,
    actions: tuple[str, ...] = (TRUSTED_MAINTENANCE_POLICY_WRITE_ACTION,),
    identity: GitHubHumanIdentity | None = None,
) -> tuple[Any, HumanSessionManager, Any]:
    session_manager = HumanSessionManager(
        config=_github_oauth_config(),
        session_store=InMemoryHumanSessionStore(),
    )
    human_session = session_manager.issue(identity or _waiver_human_identity())
    app = create_launchplane_fastapi_app(
        verifier=_StubVerifier(_identity()),
        authz_policy=_trusted_maintenance_authz_policy_record(actions=actions).policy,
        record_store_factory=lambda: store,
        human_session_manager=session_manager,
    )
    return app, session_manager, human_session


def _waiver_authz_policy_record(
    *, github_ids: tuple[int, ...] = (301,)
) -> LaunchplaneAuthzPolicyRecord:
    policy = LaunchplaneAuthzPolicy(
        schema_version=2,
        github_humans=(
            GitHubHumanPolicyRule(
                managed_set_id="tenant-human.example",
                managed_rule_id="technical-waiver",
                github_ids=github_ids,
                roles=("read_only",),
                products=(PRODUCT,),
                contexts=(CONTEXT,),
                actions=(TENANT_TECHNICAL_HUMAN_WAIVER_WRITE_ACTION,),
            ),
        ),
    )
    digest = authz_policy_sha256(policy)
    return LaunchplaneAuthzPolicyRecord(
        record_id=build_authz_policy_record_id(revision=1, policy_sha256=digest),
        revision=1,
        status="active",
        source="test:tenant-technical-human-waiver-authz",
        updated_at="2026-07-31T00:00:00Z",
        policy_sha256=digest,
        policy=policy,
    )


def _trusted_maintenance_authz_policy_record(
    *,
    actions: tuple[str, ...] = (TRUSTED_MAINTENANCE_POLICY_WRITE_ACTION,),
    github_ids: tuple[int, ...] = (301,),
) -> LaunchplaneAuthzPolicyRecord:
    policy = LaunchplaneAuthzPolicy(
        schema_version=2,
        github_humans=(
            GitHubHumanPolicyRule(
                managed_set_id="tenant-human.example",
                managed_rule_id="trusted-maintenance-policy",
                github_ids=github_ids,
                roles=("read_only",),
                products=(PRODUCT,),
                contexts=(CONTEXT,),
                actions=actions,
            ),
        ),
    )
    digest = authz_policy_sha256(policy)
    return LaunchplaneAuthzPolicyRecord(
        record_id=build_authz_policy_record_id(revision=1, policy_sha256=digest),
        revision=1,
        status="active",
        source="test:trusted-maintenance-policy-authz",
        updated_at="2026-07-31T00:00:00Z",
        policy_sha256=digest,
        policy=policy,
    )


def _waiver_classification_record(
    *, kind: str = "tenant_ui"
) -> TenantRepositoryClassificationRecord:
    return TenantRepositoryClassificationRecord.model_validate(
        {
            "schema_version": 1,
            "repository_id": REPOSITORY_ID,
            "repository_owner_id": REPOSITORY_OWNER_ID,
            "repository": REPOSITORY,
            "product": PRODUCT,
            "context": CONTEXT,
            "classification_kind": kind,
            "classification_revision": 1,
            "classified_at": CLASSIFIED_AT,
            "source": SOURCE,
            "reason": REASON,
        }
    )


def _waiver_role_policy_record(
    *, repository_owner_github_ids: tuple[int, ...] = (301,)
) -> RepositoryHumanRolePolicyRecord:
    return RepositoryHumanRolePolicyRecord.model_validate(
        {
            "schema_version": 1,
            "record_id": build_repository_human_role_policy_record_id(
                repository_id=REPOSITORY_ID,
                product=PRODUCT,
                context=CONTEXT,
                role_policy_revision=1,
            ),
            "repository_id": REPOSITORY_ID,
            "repository_owner_id": REPOSITORY_OWNER_ID,
            "repository": REPOSITORY,
            "product": PRODUCT,
            "context": CONTEXT,
            "status": "active",
            "role_policy_revision": 1,
            "repository_owner_github_ids": repository_owner_github_ids,
            "manager_primary_github_ids": [501],
            "manager_backup_github_ids": [],
            "manager_delegations": [],
            "effective_at": CLASSIFIED_AT,
            "source": SOURCE,
            "reason": "current role policy",
        }
    )


def _write_filesystem_authz_policy(
    store: FilesystemRecordStore,
    record: LaunchplaneAuthzPolicyRecord,
) -> None:
    record_dir = store.state_dir / "launchplane_authz_policies"
    record_dir.mkdir(parents=True, exist_ok=True)
    (record_dir / f"{record.record_id}.json").write_text(
        json.dumps(record.model_dump(mode="json"), sort_keys=True),
        encoding="utf-8",
    )


def _seed_waiver_authority(
    store: object,
    *,
    classification: TenantRepositoryClassificationRecord | None = None,
    role_policy: RepositoryHumanRolePolicyRecord | None = None,
    authz_policy: LaunchplaneAuthzPolicyRecord | None = None,
) -> tuple[
    TenantRepositoryClassificationRecord,
    RepositoryHumanRolePolicyRecord,
    LaunchplaneAuthzPolicyRecord,
]:
    classification_record = classification or _waiver_classification_record()
    role_policy_record = role_policy or _waiver_role_policy_record()
    authz_policy_record = authz_policy or _waiver_authz_policy_record()
    store.write_tenant_repository_classification_record(classification_record)  # type: ignore[attr-defined]
    store.write_repository_human_role_policy_record(role_policy_record)  # type: ignore[attr-defined]
    if isinstance(store, PostgresRecordStore):
        store.seed_authz_policy_if_absent(authz_policy_record)
    elif isinstance(store, FilesystemRecordStore):
        _write_filesystem_authz_policy(store, authz_policy_record)
    else:
        raise TypeError("unsupported waiver test store")
    return classification_record, role_policy_record, authz_policy_record


def _trusted_maintenance_policy_payload(
    *,
    revision: int,
    mode: str = "apply",
    actor_github_id: int = 301,
    sender_github_ids: tuple[int, ...] = (301,),
    event_actions: tuple[str, ...] = ("synchronize",),
    expected_current_record_id: str = "",
    expected_current_policy_digest: str = "",
    supersedes_record_id: str | None = None,
    effective_at: str = CLASSIFIED_AT,
    reason: str = "initial trusted-maintenance policy",
) -> dict[str, object]:
    actor_rule = TrustedMaintenanceActorRule(
        actor_github_id=actor_github_id,
        actor_login="automation-301",
        sender_github_ids=sender_github_ids,
        sender_logins=("automation-sender",),
        allowed_events=(
            TrustedMaintenanceAllowedEvent(
                event_name="pull_request",
                actions=event_actions,
            ),
        ),
    )
    record = TrustedMaintenancePolicyRecord(
        record_id=build_trusted_maintenance_policy_record_id(
            repository_id=REPOSITORY_ID,
            product=PRODUCT,
            context=CONTEXT,
            policy_revision=revision,
        ),
        repository_id=REPOSITORY_ID,
        repository_owner_id=REPOSITORY_OWNER_ID,
        repository=REPOSITORY,
        product=PRODUCT,
        context=CONTEXT,
        policy_revision=revision,
        actor_rules=(actor_rule,),
        effective_at=effective_at,
        source=SOURCE,
        reason=reason,
        supersedes_record_id=supersedes_record_id,
    )

    return {
        "schema_version": 1,
        "mode": mode,
        "expected_current_record_id": expected_current_record_id,
        "expected_current_policy_digest": expected_current_policy_digest,
        "record": record.model_dump(mode="json"),
    }


def _waiver_apply_payload(
    *,
    classification: TenantRepositoryClassificationRecord,
    role_policy: RepositoryHumanRolePolicyRecord,
    authz_policy: LaunchplaneAuthzPolicyRecord,
    mode: str = "apply",
    action: str = "created",
    source_event_id: str = "comment-waiver-create",
    reason: str = "Owner reviewed exact technical waiver.",
    expected_current: dict[str, object] | None = None,
    expires_at: str = "",
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "mode": mode,
        "action": action,
        "candidate": {
            "product": PRODUCT,
            "context": CONTEXT,
            "repository_id": REPOSITORY_ID,
            "repository_owner_id": REPOSITORY_OWNER_ID,
            "repository": REPOSITORY,
            "pull_request_number": 17,
            "head_sha": "a" * 40,
        },
        "expected_authority": {
            "schema_version": 1,
            "classification_record_id": classification.record_id,
            "classification_digest": classification.classification_digest,
            "role_policy_record_id": role_policy.record_id,
            "role_policy_digest": role_policy.role_policy_digest,
            "authz_policy_record_id": authz_policy.record_id,
            "authz_policy_digest": authz_policy.policy_sha256,
        },
        "source_event_kind": "github_issue_comment",
        "source_event_id": source_event_id,
        "reason": reason,
    }
    if expected_current is not None:
        payload["expected_current"] = expected_current
    if expires_at:
        payload["expires_at"] = expires_at
    return payload


def _tenant_admission_evaluation_result() -> TenantAdmissionControllerRunOnceResult:
    candidate = TenantMergeCandidate(
        product=PRODUCT,
        context=CONTEXT,
        repository_id=REPOSITORY_ID,
        repository_owner_id=REPOSITORY_OWNER_ID,
        repository=REPOSITORY,
        pull_request_number=PULL_REQUEST_NUMBER,
        head_sha=HEAD_SHA,
    )
    classification = _waiver_classification_record()
    paths = TenantMergeEligibilityEvidenceInputs(
        trusted_maintenance=_path_result(
            kind="trusted_maintenance",
            state="pending",
            candidate=candidate,
            classification=classification,
        ),
        technical_human_waiver=_path_result(
            kind="technical_human_waiver",
            state="pending",
            candidate=candidate,
            classification=classification,
        ),
        manager_preview_approval=_path_result(
            kind="manager_preview_approval",
            state="pending",
            candidate=candidate,
            classification=classification,
        ),
    )
    decision = evaluate_tenant_merge_eligibility(
        candidate=candidate,
        classification_lookup=TenantRepositoryClassificationLookup(
            status="available",
            records=(classification,),
        ),
        evidence_inputs=paths,
        evaluated_at=CLASSIFIED_AT,
    )
    admission = TenantAdmissionStatusReadModel(
        category="pending",
        classification_status="available",
        classification_kind="tenant_ui",
        classification_revision=classification.classification_revision,
        classification_digest=classification.classification_digest,
        decision=decision,
        paths=paths,
        generated_at=CLASSIFIED_AT,
    )
    technical_checks = TenantAdmissionTechnicalChecks(
        head_sha=HEAD_SHA,
        base_sha=BASE_SHA,
        strict=False,
        status="pass",
        required_checks=(TenantAdmissionRequiredTechnicalCheck(name="ci-gate"),),
        signals=(
            TenantAdmissionTechnicalCheckSignal(
                source="check_run",
                name="ci-gate",
                app_id=1,
                state="pass",
            ),
        ),
        evaluated_at=CLASSIFIED_AT,
    )
    return TenantAdmissionControllerRunOnceResult(
        outcome="blocked",
        candidate=candidate,
        base_branch="main",
        merge_method="merge",
        pull_request_facts=TenantAdmissionPullRequestFacts(
            repository=REPOSITORY,
            pull_request_number=PULL_REQUEST_NUMBER,
            pull_request_url=f"https://example.invalid/{REPOSITORY}/pull/{PULL_REQUEST_NUMBER}",
            state="open",
            merged=False,
            draft=False,
            mergeable=True,
            head_sha=HEAD_SHA,
            base_branch="main",
            base_sha=BASE_SHA,
        ),
        admission=admission,
        technical_checks=technical_checks,
        detail="Tenant admission is pending and technical checks are pass for the exact current head.",
    )


class TenantAdmissionHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_only_evaluation_exposes_human_actions_and_technical_checks(
        self,
    ) -> None:
        evaluation = _tenant_admission_evaluation_result()
        query = urlencode(
            {
                "product": PRODUCT,
                "context": CONTEXT,
                "repository_id": REPOSITORY_ID,
                "repository_owner_id": REPOSITORY_OWNER_ID,
                "repository": REPOSITORY,
                "pull_request_number": PULL_REQUEST_NUMBER,
                "head_sha": HEAD_SHA,
                "base_branch": "main",
            }
        )
        with TemporaryDirectory() as tmp_dir:
            store = _postgres_store(Path(tmp_dir), actions=("tenant_admission.read",))
            with (
                patch(
                    "control_plane.http_app.resolve_launchplane_github_token",
                    return_value="github-token",
                ),
                patch(
                    "control_plane.http_routes.tenant_admission.evaluate_tenant_admission_candidate",
                    return_value=evaluation,
                ),
            ):
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_authz_policy(actions=("tenant_admission.read",)),
                    record_store_factory=lambda: store,
                )
                response = await _asgi_get(
                    app,
                    f"/v1/work-graph/tenant-admission/evaluation?{query}",
                    headers={"Authorization": "Bearer valid-token"},
                )

        self.assertEqual(response.status_code, 200)
        read_model = response.json()["read_model"]
        self.assertFalse(read_model["agent_authoring_allowed"])
        self.assertEqual(read_model["evaluation"]["outcome"], "blocked")
        self.assertEqual(
            read_model["evaluation"]["technical_checks"]["status"],
            "pass",
        )
        actions = {action["action_kind"]: action for action in read_model["human_actions"]}
        self.assertEqual(actions["manager_preview_approval"]["availability"], "available")
        self.assertEqual(actions["technical_human_waiver"]["availability"], "available")
        self.assertFalse(actions["technical_human_waiver"]["agent_authoring_allowed"])

    async def test_agent_context_includes_exact_tenant_admission_without_dropping_sections(
        self,
    ) -> None:
        evaluation = _tenant_admission_evaluation_result()
        query = urlencode(
            {
                "repository": REPOSITORY,
                "product": PRODUCT,
                "context": CONTEXT,
                "repository_id": REPOSITORY_ID,
                "repository_owner_id": REPOSITORY_OWNER_ID,
                "pull_request_number": PULL_REQUEST_NUMBER,
                "head_sha": HEAD_SHA,
                "base_branch": "main",
            }
        )
        with TemporaryDirectory() as tmp_dir:
            store = _postgres_store(
                Path(tmp_dir),
                actions=("product_environment.read", "tenant_admission.read"),
            )
            with (
                patch(
                    "control_plane.http_app.resolve_launchplane_github_token",
                    return_value="github-token",
                ),
                patch(
                    "control_plane.http_routes.products.evaluate_tenant_admission_candidate",
                    return_value=evaluation,
                ),
            ):
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_authz_policy(
                        actions=("product_environment.read", "tenant_admission.read")
                    ),
                    record_store_factory=lambda: store,
                )
                response = await _asgi_get(
                    app,
                    f"/v1/agent/context?{query}",
                    headers={"Authorization": "Bearer valid-token"},
                )

        self.assertEqual(response.status_code, 200)
        sections = response.json()["context"]["sections"]
        self.assertEqual(sections["tenant_admission"]["status"], "available")
        tenant_read_model = sections["tenant_admission"]["payload"]["evaluation"]
        self.assertFalse(tenant_read_model["agent_authoring_allowed"])
        self.assertEqual(tenant_read_model["evaluation"]["candidate"]["head_sha"], HEAD_SHA)
        self.assertEqual(sections["repo_product_mapping"]["status"], "available")
        self.assertEqual(sections["work_graph_snapshot"]["status"], "available")

    async def test_agent_context_rejects_incomplete_exact_candidate(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = _postgres_store(Path(tmp_dir), actions=("product_environment.read",))
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_authz_policy(actions=("product_environment.read",)),
                record_store_factory=lambda: store,
            )
            response = await _asgi_get(
                app,
                f"/v1/agent/context?{urlencode({'repository': REPOSITORY, 'head_sha': HEAD_SHA})}",
                headers={"Authorization": "Bearer valid-token"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_query")

    async def test_read_only_evaluation_reports_github_token_unavailable(self) -> None:
        query = urlencode(
            {
                "product": PRODUCT,
                "context": CONTEXT,
                "repository_id": REPOSITORY_ID,
                "repository_owner_id": REPOSITORY_OWNER_ID,
                "repository": REPOSITORY,
                "pull_request_number": PULL_REQUEST_NUMBER,
                "head_sha": HEAD_SHA,
                "base_branch": "main",
            }
        )
        with TemporaryDirectory() as tmp_dir:
            store = _postgres_store(Path(tmp_dir), actions=("tenant_admission.read",))
            with patch(
                "control_plane.http_app.resolve_launchplane_github_token",
                side_effect=click.ClickException("token unavailable"),
            ):
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_authz_policy(actions=("tenant_admission.read",)),
                    record_store_factory=lambda: store,
                )
                response = await _asgi_get(
                    app,
                    f"/v1/work-graph/tenant-admission/evaluation?{query}",
                    headers={"Authorization": "Bearer valid-token"},
                )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "github_token_unavailable")

    async def test_agent_context_preserves_other_sections_when_github_token_is_unavailable(
        self,
    ) -> None:
        query = urlencode(
            {
                "repository": REPOSITORY,
                "product": PRODUCT,
                "context": CONTEXT,
                "repository_id": REPOSITORY_ID,
                "repository_owner_id": REPOSITORY_OWNER_ID,
                "pull_request_number": PULL_REQUEST_NUMBER,
                "head_sha": HEAD_SHA,
                "base_branch": "main",
            }
        )
        actions = ("product_environment.read", "tenant_admission.read")
        with TemporaryDirectory() as tmp_dir:
            store = _postgres_store(Path(tmp_dir), actions=actions)
            with patch(
                "control_plane.http_app.resolve_launchplane_github_token",
                side_effect=click.ClickException("token unavailable"),
            ):
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_authz_policy(actions=actions),
                    record_store_factory=lambda: store,
                )
                response = await _asgi_get(
                    app,
                    f"/v1/agent/context?{query}",
                    headers={"Authorization": "Bearer valid-token"},
                )

        self.assertEqual(response.status_code, 200)
        sections = response.json()["context"]["sections"]
        self.assertEqual(sections["tenant_admission"]["status"], "unavailable")
        self.assertEqual(
            sections["tenant_admission"]["reason_code"],
            "tenant_admission_github_unavailable",
        )
        self.assertEqual(sections["repo_product_mapping"]["status"], "available")
        self.assertEqual(sections["work_graph_snapshot"]["status"], "available")

    def test_openapi_includes_read_only_tenant_admission_evaluation(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_authz_policy(actions=()),
                record_store_factory=lambda: FilesystemRecordStore(state_dir=Path(tmp_dir)),
            )
            route = app.openapi()["paths"]["/v1/work-graph/tenant-admission/evaluation"]["get"]

        self.assertEqual(route["operationId"], "read_tenant_admission_evaluation")
        self.assertIn("TenantAdmissionEvaluationReadResponse", json.dumps(route))
        parameter_names = {parameter["name"] for parameter in route["parameters"]}
        self.assertTrue(
            {
                "base_branch",
                "context",
                "head_sha",
                "merge_method",
                "product",
                "pull_request_number",
                "repository",
                "repository_id",
                "repository_owner_id",
            }.issubset(parameter_names),
        )

    async def test_initial_create_applies_revision_1(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = _postgres_store(Path(tmp_dir))
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_authz_policy(actions=("tenant_repository_classification.write",)),
                record_store_factory=lambda: store,
            )
            response = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-classifications/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "key-create-1",
                },
                payload=_apply_payload(revision=1),
            )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["result"]["status"], "applied")
        self.assertEqual(data["result"]["mode"], "apply")
        self.assertEqual(data["result"]["repository_id"], REPOSITORY_ID)
        self.assertEqual(data["result"]["classification_revision"], 1)
        expected_record_id = build_tenant_repository_classification_record_id(
            repository_id=REPOSITORY_ID, classification_revision=1
        )
        self.assertEqual(data["result"]["record_id"], expected_record_id)
        self.assertIsNone(data["result"]["supersedes_record_id"])

    async def test_dry_run_no_write(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = _postgres_store(Path(tmp_dir))
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_authz_policy(
                    actions=(
                        "tenant_repository_classification.read",
                        "tenant_repository_classification.write",
                    )
                ),
                record_store_factory=lambda: store,
            )
            dry_run_response = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-classifications/apply",
                headers={"Authorization": "Bearer valid-token"},
                payload=_apply_payload(revision=1, mode="dry_run"),
            )
            self.assertEqual(dry_run_response.status_code, 200)
            self.assertEqual(dry_run_response.json()["result"]["mode"], "dry_run")
            self.assertEqual(dry_run_response.json()["result"]["status"], "would_apply")

            read_response = await _asgi_get(
                app,
                f"/v1/work-graph/tenant-admission/repository-classification?repository_id={REPOSITORY_ID}",
                headers={"Authorization": "Bearer valid-token"},
            )

        self.assertEqual(read_response.status_code, 200)
        read_data = read_response.json()
        self.assertEqual(read_data["read_model"]["status"], "missing")
        self.assertIsNone(read_data["read_model"]["current_record"])
        self.assertEqual(read_data["read_model"]["history_count"], 0)

    async def test_revision_update(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = _postgres_store(Path(tmp_dir))
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_authz_policy(
                    actions=(
                        "tenant_repository_classification.read",
                        "tenant_repository_classification.write",
                    )
                ),
                record_store_factory=lambda: store,
            )

            res1 = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-classifications/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "key-rev1",
                },
                payload=_apply_payload(revision=1),
            )
            self.assertEqual(res1.status_code, 200)
            rev1_record_id = res1.json()["result"]["record_id"]

            res2 = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-classifications/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "key-rev2",
                },
                payload=_apply_payload(
                    revision=2,
                    kind="engineering",
                    supersedes_record_id=rev1_record_id,
                    expected_current_record_id=rev1_record_id,
                ),
            )
            self.assertEqual(res2.status_code, 200)
            res2_data = res2.json()
            self.assertEqual(res2_data["result"]["status"], "applied")
            self.assertEqual(res2_data["result"]["classification_revision"], 2)
            self.assertEqual(res2_data["result"]["supersedes_record_id"], rev1_record_id)

            read_res = await _asgi_get(
                app,
                f"/v1/work-graph/tenant-admission/repository-classification?repository_id={REPOSITORY_ID}",
                headers={"Authorization": "Bearer valid-token"},
            )

        self.assertEqual(read_res.status_code, 200)
        read_data = read_res.json()["read_model"]
        self.assertEqual(read_data["status"], "available")
        self.assertEqual(read_data["history_count"], 2)
        self.assertEqual(read_data["current_record"]["classification_revision"], 2)
        self.assertEqual(read_data["current_record"]["classification_kind"], "engineering")

    async def test_stale_expected_current_conflict(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = _postgres_store(Path(tmp_dir))
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_authz_policy(actions=("tenant_repository_classification.write",)),
                record_store_factory=lambda: store,
            )
            res1 = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-classifications/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "key-1",
                },
                payload=_apply_payload(revision=1),
            )
            rev1_id = res1.json()["result"]["record_id"]

            res2 = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-classifications/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "key-2",
                },
                payload=_apply_payload(
                    revision=2,
                    supersedes_record_id=rev1_id,
                    expected_current_record_id=rev1_id,
                ),
            )
            self.assertEqual(res2.status_code, 200)
            rev2_id = res2.json()["result"]["record_id"]

            conflict_res = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-classifications/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "key-3",
                },
                payload=_apply_payload(
                    revision=3,
                    supersedes_record_id=rev2_id,
                    expected_current_record_id=rev1_id,
                ),
            )

        self.assertEqual(conflict_res.status_code, 409)
        self.assertEqual(conflict_res.json()["error"]["code"], "classification_conflict")

    async def test_skipped_revision_and_supersedes_mismatch_rejection(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = _postgres_store(Path(tmp_dir))
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_authz_policy(actions=("tenant_repository_classification.write",)),
                record_store_factory=lambda: store,
            )
            res1 = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-classifications/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "key-rev1",
                },
                payload=_apply_payload(revision=1),
            )
            rev1_id = res1.json()["result"]["record_id"]

            skipped_res = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-classifications/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "key-skipped",
                },
                payload=_apply_payload(
                    revision=3,
                    supersedes_record_id=rev1_id,
                    expected_current_record_id=rev1_id,
                ),
            )
            self.assertEqual(skipped_res.status_code, 400)
            self.assertEqual(skipped_res.json()["error"]["code"], "invalid_sequence")

            mismatch_res = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-classifications/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "key-mismatch",
                },
                payload=_apply_payload(
                    revision=2,
                    supersedes_record_id="wrong-rev-id",
                    expected_current_record_id=rev1_id,
                ),
            )

        self.assertEqual(mismatch_res.status_code, 400)
        self.assertEqual(mismatch_res.json()["error"]["code"], "invalid_sequence")

    async def test_idempotent_replay(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = _postgres_store(Path(tmp_dir))
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_authz_policy(actions=("tenant_repository_classification.write",)),
                record_store_factory=lambda: store,
            )
            payload = _apply_payload(revision=1)
            headers = {
                "Authorization": "Bearer valid-token",
                "Idempotency-Key": "key-replay-100",
            }

            res1 = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-classifications/apply",
                headers=headers,
                payload=payload,
            )
            self.assertEqual(res1.status_code, 200)

            res2 = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-classifications/apply",
                headers=headers,
                payload=payload,
            )

        self.assertEqual(res2.status_code, 200)
        data = res2.json()
        self.assertTrue(data.get("replayed"))

    async def test_identical_payload_with_different_idempotency_key_conflicts(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = _postgres_store(Path(tmp_dir))
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_authz_policy(actions=("tenant_repository_classification.write",)),
                record_store_factory=lambda: store,
            )
            payload = _apply_payload(revision=1)
            first_response = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-classifications/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "key-replay-original",
                },
                payload=payload,
            )
            second_response = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-classifications/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "key-replay-different",
                },
                payload=payload,
            )

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 409)
        self.assertEqual(
            second_response.json()["error"]["code"],
            "classification_conflict",
        )

    async def test_same_idempotency_key_with_different_payload_conflicts(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = _postgres_store(Path(tmp_dir))
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_authz_policy(actions=("tenant_repository_classification.write",)),
                record_store_factory=lambda: store,
            )
            headers = {
                "Authorization": "Bearer valid-token",
                "Idempotency-Key": "key-reused-different-payload",
            }
            first_response = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-classifications/apply",
                headers=headers,
                payload=_apply_payload(revision=1),
            )
            second_response = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-classifications/apply",
                headers=headers,
                payload=_apply_payload(revision=1, kind="engineering"),
            )

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 409)
        self.assertEqual(
            second_response.json()["error"]["code"],
            "idempotency_key_reused",
        )

    async def test_terminal_agent_denial(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = _postgres_store(Path(tmp_dir))
            app = create_launchplane_fastapi_app(
                verifier=cast(
                    Any,
                    _StubVerifier(
                        cast(
                            Any,
                            TerminalAgentIdentity(
                                subject="local-owner-agent",
                                token_label="local-owner-token",
                            ),
                        )
                    ),
                ),
                authz_policy=_authz_policy(actions=("tenant_repository_classification.write",)),
                record_store_factory=lambda: store,
            )
            response = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-classifications/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "key-agent",
                },
                payload=_apply_payload(revision=1),
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_authz_denial(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = _postgres_store(Path(tmp_dir), actions=())
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_authz_policy(actions=()),
                record_store_factory=lambda: store,
            )
            read_res = await _asgi_get(
                app,
                f"/v1/work-graph/tenant-admission/repository-classification?repository_id={REPOSITORY_ID}",
                headers={"Authorization": "Bearer valid-token"},
            )
            write_res = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-classifications/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "key-no-auth",
                },
                payload=_apply_payload(revision=1),
            )

        self.assertEqual(read_res.status_code, 403)
        self.assertEqual(write_res.status_code, 403)

    async def test_missing_classification_read(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = _postgres_store(Path(tmp_dir))
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_authz_policy(actions=("tenant_repository_classification.read",)),
                record_store_factory=lambda: store,
            )
            response = await _asgi_get(
                app,
                "/v1/work-graph/tenant-admission/repository-classification?repository_id=9999",
                headers={"Authorization": "Bearer valid-token"},
            )

        self.assertEqual(response.status_code, 200)
        data = response.json()["read_model"]
        self.assertEqual(data["status"], "missing")
        self.assertEqual(data["repository_id"], "9999")
        self.assertIsNone(data["current_record"])
        self.assertEqual(data["history_count"], 0)

    async def test_immutable_repository_id_lookup(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = _postgres_store(Path(tmp_dir))
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_authz_policy(
                    actions=(
                        "tenant_repository_classification.read",
                        "tenant_repository_classification.write",
                    )
                ),
                record_store_factory=lambda: store,
            )
            await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-classifications/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "key-lookup",
                },
                payload=_apply_payload(revision=1),
            )

            read_res = await _asgi_get(
                app,
                f"/v1/work-graph/tenant-admission/repository-classification?repository_id={REPOSITORY_ID}",
                headers={"Authorization": "Bearer valid-token"},
            )

        self.assertEqual(read_res.status_code, 200)
        data = read_res.json()["read_model"]
        self.assertEqual(data["status"], "available")
        self.assertEqual(data["repository_id"], REPOSITORY_ID)
        self.assertEqual(data["history_count"], 1)
        self.assertEqual(data["current_record"]["repository_id"], REPOSITORY_ID)
        self.assertEqual(data["current_record"]["classification_revision"], 1)

    async def test_no_evaluate_or_evidence_ingress_route(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = _postgres_store(Path(tmp_dir))
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_authz_policy(
                    actions=(
                        "tenant_repository_classification.read",
                        "tenant_repository_classification.write",
                    )
                ),
                record_store_factory=lambda: store,
            )
            routes = [
                ("GET", "/v1/tenant-admission/evaluate"),
                ("POST", "/v1/tenant-admission/evaluate"),
                ("POST", "/v1/evidence/tenant-admission"),
                ("GET", "/v1/work-graph/tenant-admission/evaluate"),
            ]
            results = []
            for method, path in routes:
                res = await _asgi_request(
                    app,
                    method,
                    path,
                    headers={"Authorization": "Bearer valid-token"},
                )
                results.append(res.status_code)

        for status_code in results:
            self.assertEqual(status_code, 404)

    async def test_missing_db_capability_503(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = FilesystemRecordStore(Path(tmp_dir))
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_authz_policy(actions=("tenant_repository_classification.write",)),
                record_store_factory=lambda: store,
            )
            response = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-classifications/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "key-503",
                },
                payload=_apply_payload(revision=1),
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "database_storage_required")

    async def test_sqlite_backed_postgres_store_is_not_shared_authority(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = PostgresRecordStore(
                database_url=f"sqlite+pysqlite:///{Path(tmp_dir) / 'launchplane.sqlite3'}"
            )
            store.ensure_schema()
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_authz_policy(actions=("tenant_repository_classification.write",)),
                record_store_factory=lambda: store,
            )
            response = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-classifications/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "key-sqlite-503",
                },
                payload=_apply_payload(revision=1),
            )
            store.close()

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "database_storage_required")

    async def test_role_policy_dry_run_works_with_filesystem_rehearsal_store(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = FilesystemRecordStore(Path(tmp_dir))
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_authz_policy(actions=("repository_human_role_policy.write",)),
                record_store_factory=lambda: store,
            )
            response = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-human-role-policies/apply",
                headers={"Authorization": "Bearer valid-token"},
                payload=_role_policy_payload(revision=1, mode="dry_run"),
            )

        self.assertEqual(response.status_code, 202)
        data = response.json()
        self.assertEqual(data["result"]["status"], "would_apply")
        self.assertEqual(data["result"]["mode"], "dry_run")
        self.assertEqual(
            store.list_repository_human_role_policy_records(repository_id=REPOSITORY_ID),
            (),
        )

    async def test_role_policy_apply_revision_and_read_model(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = _postgres_store(Path(tmp_dir))
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_authz_policy(
                    actions=(
                        "repository_human_role_policy.read",
                        "repository_human_role_policy.write",
                    )
                ),
                record_store_factory=lambda: store,
            )

            res1 = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-human-role-policies/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "role-policy-rev1",
                },
                payload=_role_policy_payload(revision=1),
            )
            self.assertEqual(res1.status_code, 202)
            rev1 = res1.json()["result"]

            res2 = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-human-role-policies/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "role-policy-rev2",
                },
                payload=_role_policy_payload(
                    revision=2,
                    repository_owner_github_ids=(302,),
                    expected_current_record_id=rev1["record_id"],
                    expected_current_role_policy_digest=rev1["role_policy_digest"],
                    supersedes_record_id=rev1["record_id"],
                ),
            )
            self.assertEqual(res2.status_code, 202)
            read_res = await _asgi_get(
                app,
                (
                    "/v1/work-graph/tenant-admission/repository-human-role-policy"
                    f"?repository_id={REPOSITORY_ID}&product={PRODUCT}&context={CONTEXT}"
                ),
                headers={"Authorization": "Bearer valid-token"},
            )

        self.assertEqual(res2.json()["result"]["status"], "applied")
        self.assertEqual(res2.json()["result"]["role_policy_revision"], 2)
        self.assertEqual(read_res.status_code, 200)
        read_model = read_res.json()["read_model"]
        self.assertEqual(read_model["status"], "available")
        self.assertEqual(read_model["history_count"], 2)
        self.assertEqual(read_model["current_record"]["role_policy_revision"], 2)
        self.assertEqual(read_model["current_record"]["repository_owner_github_ids"], [302])

    async def test_role_policy_missing_read_requires_product_context_authz(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = _postgres_store(Path(tmp_dir))
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_authz_policy(actions=("repository_human_role_policy.read",)),
                record_store_factory=lambda: store,
            )
            response = await _asgi_get(
                app,
                (
                    "/v1/work-graph/tenant-admission/repository-human-role-policy"
                    "?repository_id=9999&product=launchplane&context=production"
                ),
                headers={"Authorization": "Bearer valid-token"},
            )

            denied = await _asgi_get(
                app,
                (
                    "/v1/work-graph/tenant-admission/repository-human-role-policy"
                    "?repository_id=9999&product=launchplane&context=unauthorized"
                ),
                headers={"Authorization": "Bearer valid-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["read_model"]["status"], "missing")
        self.assertEqual(denied.status_code, 403)

    async def test_role_policy_apply_requires_postgres_and_idempotency_key(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            filesystem_store = FilesystemRecordStore(Path(tmp_dir) / "fs")
            filesystem_app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_authz_policy(actions=("repository_human_role_policy.write",)),
                record_store_factory=lambda: filesystem_store,
            )
            sqlite_store = PostgresRecordStore(
                database_url=f"sqlite+pysqlite:///{Path(tmp_dir) / 'launchplane.sqlite3'}"
            )
            sqlite_store.ensure_schema()
            sqlite_app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_authz_policy(actions=("repository_human_role_policy.write",)),
                record_store_factory=lambda: sqlite_store,
            )

            missing_key = await _asgi_request(
                filesystem_app,
                "POST",
                "/v1/tenant-admission/repository-human-role-policies/apply",
                headers={"Authorization": "Bearer valid-token"},
                payload=_role_policy_payload(revision=1),
            )
            filesystem_response = await _asgi_request(
                filesystem_app,
                "POST",
                "/v1/tenant-admission/repository-human-role-policies/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "role-policy-fs",
                },
                payload=_role_policy_payload(revision=1),
            )
            sqlite_response = await _asgi_request(
                sqlite_app,
                "POST",
                "/v1/tenant-admission/repository-human-role-policies/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "role-policy-sqlite",
                },
                payload=_role_policy_payload(revision=1),
            )
            sqlite_store.close()

        self.assertEqual(missing_key.status_code, 400)
        self.assertEqual(missing_key.json()["error"]["code"], "idempotency_key_required")
        self.assertEqual(filesystem_response.status_code, 503)
        self.assertEqual(
            filesystem_response.json()["error"]["code"],
            "database_storage_required",
        )
        self.assertEqual(sqlite_response.status_code, 503)
        self.assertEqual(sqlite_response.json()["error"]["code"], "database_storage_required")

    async def test_role_policy_apply_rejects_terminal_agent_and_wrong_context_authz(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp_dir:
            terminal_store = _postgres_store(Path(tmp_dir) / "terminal")
            terminal_app = create_launchplane_fastapi_app(
                verifier=cast(
                    Any,
                    _StubVerifier(
                        cast(
                            Any,
                            TerminalAgentIdentity(
                                subject="local-owner-agent",
                                token_label="local-owner-token",
                            ),
                        )
                    ),
                ),
                authz_policy=_authz_policy(actions=("repository_human_role_policy.write",)),
                record_store_factory=lambda: terminal_store,
            )
            denied_store = _postgres_store(Path(tmp_dir) / "denied", actions=())
            denied_app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_authz_policy(actions=()),
                record_store_factory=lambda: denied_store,
            )
            terminal_response = await _asgi_request(
                terminal_app,
                "POST",
                "/v1/tenant-admission/repository-human-role-policies/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "role-policy-terminal",
                },
                payload=_role_policy_payload(revision=1),
            )
            denied_response = await _asgi_request(
                denied_app,
                "POST",
                "/v1/tenant-admission/repository-human-role-policies/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "role-policy-denied",
                },
                payload=_role_policy_payload(revision=1),
            )

        self.assertEqual(terminal_response.status_code, 403)
        self.assertEqual(denied_response.status_code, 403)

    async def test_role_policy_idempotency_replay_conflict_and_exact_replay(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = _postgres_store(Path(tmp_dir))
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_authz_policy(actions=("repository_human_role_policy.write",)),
                record_store_factory=lambda: store,
            )
            payload = _role_policy_payload(revision=1)
            headers = {
                "Authorization": "Bearer valid-token",
                "Idempotency-Key": "role-policy-replay",
            }
            first = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-human-role-policies/apply",
                headers=headers,
                payload=payload,
            )
            same_key = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-human-role-policies/apply",
                headers=headers,
                payload=payload,
            )
            changed_payload = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-human-role-policies/apply",
                headers=headers,
                payload=_role_policy_payload(revision=1, reason="changed payload"),
            )
            exact_replay = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-human-role-policies/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "role-policy-exact-replay-new-key",
                },
                payload=payload,
            )

        self.assertEqual(first.status_code, 202)
        self.assertEqual(same_key.status_code, 202)
        self.assertTrue(same_key.json().get("replayed"))
        self.assertEqual(changed_payload.status_code, 409)
        self.assertEqual(changed_payload.json()["error"]["code"], "idempotency_key_reused")
        self.assertEqual(exact_replay.status_code, 202)
        self.assertIsNone(exact_replay.json().get("replayed"))
        self.assertEqual(exact_replay.json()["result"]["status"], "replayed")

    async def test_role_policy_revision_two_exact_replay_with_new_key(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = _postgres_store(Path(tmp_dir))
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_authz_policy(actions=("repository_human_role_policy.write",)),
                record_store_factory=lambda: store,
            )
            revision_1_response = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-human-role-policies/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "role-policy-revision-1",
                },
                payload=_role_policy_payload(revision=1),
            )
            revision_1 = revision_1_response.json()["result"]
            revision_2_payload = _role_policy_payload(
                revision=2,
                repository_owner_github_ids=(302,),
                expected_current_record_id=revision_1["record_id"],
                expected_current_role_policy_digest=revision_1["role_policy_digest"],
                supersedes_record_id=revision_1["record_id"],
            )
            revision_2_response = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-human-role-policies/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "role-policy-revision-2",
                },
                payload=revision_2_payload,
            )
            replay_response = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-human-role-policies/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "role-policy-revision-2-replay",
                },
                payload=revision_2_payload,
            )

        self.assertEqual(revision_1_response.status_code, 202)
        self.assertEqual(revision_2_response.status_code, 202)
        self.assertEqual(replay_response.status_code, 202)
        self.assertEqual(replay_response.json()["result"]["status"], "replayed")

    async def test_trusted_maintenance_policy_read_apply_uses_separate_human_authz(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = _postgres_store(
                Path(tmp_dir),
                authz_policy_record=_trusted_maintenance_authz_policy_record(
                    actions=(
                        TRUSTED_MAINTENANCE_POLICY_READ_ACTION,
                        TRUSTED_MAINTENANCE_POLICY_WRITE_ACTION,
                    )
                ),
            )
            app, session_manager, human_session = _trusted_maintenance_session_app(
                store,
                actions=(
                    TRUSTED_MAINTENANCE_POLICY_READ_ACTION,
                    TRUSTED_MAINTENANCE_POLICY_WRITE_ACTION,
                ),
            )
            browser_headers = _browser_mutation_headers(session_manager, human_session)
            read_response = await _asgi_get(
                app,
                (
                    "/v1/work-graph/tenant-admission/trusted-maintenance-policy"
                    f"?repository_id={REPOSITORY_ID}&product={PRODUCT}&context={CONTEXT}"
                ),
                headers=browser_headers,
            )
            apply_response = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/trusted-maintenance-policies/apply",
                headers={
                    **_browser_mutation_headers(session_manager, human_session),
                    "Idempotency-Key": "trusted-maintenance-policy-create",
                },
                payload=_trusted_maintenance_policy_payload(revision=1),
            )
            read_after_apply = await _asgi_get(
                app,
                (
                    "/v1/work-graph/tenant-admission/trusted-maintenance-policy"
                    f"?repository_id={REPOSITORY_ID}&product={PRODUCT}&context={CONTEXT}"
                ),
                headers=_browser_mutation_headers(session_manager, human_session),
            )

        self.assertEqual(read_response.status_code, 200)
        self.assertEqual(read_response.json()["read_model"]["status"], "missing")
        self.assertEqual(apply_response.status_code, 202)
        self.assertEqual(apply_response.json()["result"]["status"], "applied")
        self.assertEqual(apply_response.json()["result"]["policy_revision"], 1)
        self.assertEqual(read_after_apply.status_code, 200)
        self.assertEqual(read_after_apply.json()["read_model"]["status"], "available")
        self.assertEqual(
            read_after_apply.json()["read_model"]["current_record"]["policy_revision"],
            1,
        )

    def test_openapi_includes_trusted_maintenance_policy_contract(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = _postgres_store(
                Path(tmp_dir),
                authz_policy_record=_trusted_maintenance_authz_policy_record(),
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_trusted_maintenance_authz_policy_record().policy,
                record_store_factory=lambda: store,
            )
            openapi = app.openapi()
            store.close()

        read_route = openapi["paths"]["/v1/work-graph/tenant-admission/trusted-maintenance-policy"][
            "get"
        ]
        apply_route = openapi["paths"]["/v1/tenant-admission/trusted-maintenance-policies/apply"][
            "post"
        ]

        self.assertEqual(read_route["operationId"], "read_trusted_maintenance_policy")
        self.assertEqual(apply_route["operationId"], "apply_trusted_maintenance_policy")
        self.assertEqual(
            read_route["responses"]["200"]["content"]["application/json"]["schema"],
            {"$ref": "#/components/schemas/TrustedMaintenancePolicyReadResponse"},
        )
        self.assertEqual(
            apply_route["requestBody"]["content"]["application/json"]["schema"],
            {"$ref": "#/components/schemas/TrustedMaintenancePolicyApplyEnvelope"},
        )
        self.assertEqual(
            apply_route["responses"]["202"]["content"]["application/json"]["schema"],
            {"$ref": "#/components/schemas/TrustedMaintenancePolicyApplyResponse"},
        )
        for status_code in ("409", "503"):
            self.assertEqual(
                read_route["responses"][status_code]["content"]["application/json"]["schema"],
                {"$ref": "#/components/schemas/LaunchplaneErrorResponse"},
            )
            self.assertEqual(
                apply_route["responses"][status_code]["content"]["application/json"]["schema"],
                {"$ref": "#/components/schemas/LaunchplaneErrorResponse"},
            )

    async def test_trusted_maintenance_policy_apply_requires_browser_human_and_csrf(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = _postgres_store(
                Path(tmp_dir),
                authz_policy_record=_trusted_maintenance_authz_policy_record(),
            )
            app, session_manager, human_session = _trusted_maintenance_session_app(store)
            bearer_only = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/trusted-maintenance-policies/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "trusted-bearer-only",
                },
                payload=_trusted_maintenance_policy_payload(revision=1),
            )
            missing_csrf_headers = _browser_mutation_headers(
                session_manager,
                human_session,
            )
            missing_csrf_headers.pop("X-CSRF-Token")
            missing_csrf = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/trusted-maintenance-policies/apply",
                headers={
                    **missing_csrf_headers,
                    "Idempotency-Key": "trusted-missing-csrf",
                },
                payload=_trusted_maintenance_policy_payload(revision=1),
            )

        self.assertEqual(bearer_only.status_code, 403)
        self.assertEqual(bearer_only.json()["error"]["code"], "authorization_denied")
        self.assertEqual(missing_csrf.status_code, 403)
        self.assertEqual(missing_csrf.json()["error"]["code"], "browser_mutation_denied")

    async def test_trusted_maintenance_policy_authz_is_separate_from_role_policy(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = _postgres_store(
                Path(tmp_dir),
                authz_policy_record=_trusted_maintenance_authz_policy_record(
                    actions=(REPOSITORY_HUMAN_ROLE_POLICY_WRITE_ACTION,)
                ),
            )
            app, session_manager, human_session = _trusted_maintenance_session_app(
                store,
                actions=(REPOSITORY_HUMAN_ROLE_POLICY_WRITE_ACTION,),
            )
            read_denied = await _asgi_get(
                app,
                (
                    "/v1/work-graph/tenant-admission/trusted-maintenance-policy"
                    f"?repository_id={REPOSITORY_ID}&product={PRODUCT}&context={CONTEXT}"
                ),
                headers=_browser_mutation_headers(session_manager, human_session),
            )
            write_denied = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/trusted-maintenance-policies/apply",
                headers={
                    **_browser_mutation_headers(session_manager, human_session),
                    "Idempotency-Key": "trusted-role-policy-action-only",
                },
                payload=_trusted_maintenance_policy_payload(revision=1),
            )

        self.assertEqual(read_denied.status_code, 403)
        self.assertEqual(write_denied.status_code, 403)

    async def test_trusted_maintenance_policy_apply_requires_postgres_and_key(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp_dir:
            filesystem_store = FilesystemRecordStore(Path(tmp_dir) / "fs")
            _write_filesystem_authz_policy(
                filesystem_store,
                _trusted_maintenance_authz_policy_record(),
            )
            (
                filesystem_app,
                filesystem_sessions,
                filesystem_session,
            ) = _trusted_maintenance_session_app(filesystem_store)
            sqlite_store = PostgresRecordStore(
                database_url=f"sqlite+pysqlite:///{Path(tmp_dir) / 'launchplane.sqlite3'}"
            )
            sqlite_store.ensure_schema()
            sqlite_store.seed_authz_policy_if_absent(_trusted_maintenance_authz_policy_record())
            sqlite_app, sqlite_sessions, sqlite_session = _trusted_maintenance_session_app(
                sqlite_store
            )

            missing_key = await _asgi_request(
                sqlite_app,
                "POST",
                "/v1/tenant-admission/trusted-maintenance-policies/apply",
                headers=_browser_mutation_headers(sqlite_sessions, sqlite_session),
                payload=_trusted_maintenance_policy_payload(revision=1),
            )
            filesystem_response = await _asgi_request(
                filesystem_app,
                "POST",
                "/v1/tenant-admission/trusted-maintenance-policies/apply",
                headers={
                    **_browser_mutation_headers(filesystem_sessions, filesystem_session),
                    "Idempotency-Key": "trusted-fs-apply",
                },
                payload=_trusted_maintenance_policy_payload(revision=1),
            )
            sqlite_response = await _asgi_request(
                sqlite_app,
                "POST",
                "/v1/tenant-admission/trusted-maintenance-policies/apply",
                headers={
                    **_browser_mutation_headers(sqlite_sessions, sqlite_session),
                    "Idempotency-Key": "trusted-sqlite-apply",
                },
                payload=_trusted_maintenance_policy_payload(revision=1),
            )
            sqlite_store.close()

        self.assertEqual(missing_key.status_code, 400)
        self.assertEqual(missing_key.json()["error"]["code"], "idempotency_key_required")
        self.assertEqual(filesystem_response.status_code, 503)
        self.assertEqual(
            filesystem_response.json()["error"]["code"],
            "database_storage_required",
        )
        self.assertEqual(sqlite_response.status_code, 503)
        self.assertEqual(sqlite_response.json()["error"]["code"], "database_storage_required")

    async def test_trusted_maintenance_policy_dry_run_and_idempotency_semantics(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp_dir:
            filesystem_store = FilesystemRecordStore(Path(tmp_dir) / "fs")
            _write_filesystem_authz_policy(
                filesystem_store,
                _trusted_maintenance_authz_policy_record(),
            )
            dry_app, dry_sessions, dry_session = _trusted_maintenance_session_app(filesystem_store)
            dry_run = await _asgi_request(
                dry_app,
                "POST",
                "/v1/tenant-admission/trusted-maintenance-policies/apply",
                headers=_browser_mutation_headers(dry_sessions, dry_session),
                payload=_trusted_maintenance_policy_payload(revision=1, mode="dry_run"),
            )
            dry_records = filesystem_store.list_trusted_maintenance_policy_records(
                repository_id=REPOSITORY_ID
            )

            store = _postgres_store(
                Path(tmp_dir) / "pg",
                authz_policy_record=_trusted_maintenance_authz_policy_record(),
            )
            app, session_manager, human_session = _trusted_maintenance_session_app(store)
            payload = _trusted_maintenance_policy_payload(revision=1)
            first = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/trusted-maintenance-policies/apply",
                headers={
                    **_browser_mutation_headers(session_manager, human_session),
                    "Idempotency-Key": "trusted-idempotency",
                },
                payload=payload,
            )
            same_key = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/trusted-maintenance-policies/apply",
                headers={
                    **_browser_mutation_headers(session_manager, human_session),
                    "Idempotency-Key": "trusted-idempotency",
                },
                payload=payload,
            )
            changed_same_key = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/trusted-maintenance-policies/apply",
                headers={
                    **_browser_mutation_headers(session_manager, human_session),
                    "Idempotency-Key": "trusted-idempotency",
                },
                payload=_trusted_maintenance_policy_payload(
                    revision=1,
                    reason="changed trusted-maintenance payload",
                ),
            )
            exact_replay = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/trusted-maintenance-policies/apply",
                headers={
                    **_browser_mutation_headers(session_manager, human_session),
                    "Idempotency-Key": "trusted-idempotency-new-key",
                },
                payload=payload,
            )

        self.assertEqual(dry_run.status_code, 202)
        self.assertEqual(dry_run.json()["result"]["status"], "would_apply")
        self.assertEqual(dry_run.json()["result"]["mode"], "dry_run")
        self.assertEqual(dry_records, ())
        self.assertEqual(first.status_code, 202)
        self.assertEqual(same_key.status_code, 202)
        self.assertTrue(same_key.json().get("replayed"))
        self.assertEqual(changed_same_key.status_code, 409)
        self.assertEqual(changed_same_key.json()["error"]["code"], "idempotency_key_reused")
        self.assertEqual(exact_replay.status_code, 202)
        self.assertIsNone(exact_replay.json().get("replayed"))
        self.assertEqual(exact_replay.json()["result"]["status"], "replayed")

    async def test_trusted_maintenance_policy_rejects_stale_expected_tip(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = _postgres_store(
                Path(tmp_dir),
                authz_policy_record=_trusted_maintenance_authz_policy_record(),
            )
            app, session_manager, human_session = _trusted_maintenance_session_app(store)
            revision_1_response = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/trusted-maintenance-policies/apply",
                headers={
                    **_browser_mutation_headers(session_manager, human_session),
                    "Idempotency-Key": "trusted-rev-1",
                },
                payload=_trusted_maintenance_policy_payload(revision=1),
            )
            revision_1 = revision_1_response.json()["result"]
            stale_response = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/trusted-maintenance-policies/apply",
                headers={
                    **_browser_mutation_headers(session_manager, human_session),
                    "Idempotency-Key": "trusted-stale-tip",
                },
                payload=_trusted_maintenance_policy_payload(
                    revision=2,
                    actor_github_id=302,
                    expected_current_record_id="wrong-record-id",
                    expected_current_policy_digest=revision_1["policy_digest"],
                    supersedes_record_id=revision_1["record_id"],
                ),
            )

        self.assertEqual(revision_1_response.status_code, 202)
        self.assertEqual(stale_response.status_code, 409)
        self.assertEqual(
            stale_response.json()["error"]["code"],
            "trusted_maintenance_policy_conflict",
        )

    async def test_role_policy_rejects_missing_stale_or_digest_drift_expected_tip(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = _postgres_store(Path(tmp_dir))
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_authz_policy(actions=("repository_human_role_policy.write",)),
                record_store_factory=lambda: store,
            )
            res1 = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-human-role-policies/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "role-policy-current-1",
                },
                payload=_role_policy_payload(revision=1),
            )
            rev1 = res1.json()["result"]
            missing_digest = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-human-role-policies/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "role-policy-missing-digest",
                },
                payload=_role_policy_payload(
                    revision=2,
                    expected_current_record_id=rev1["record_id"],
                    supersedes_record_id=rev1["record_id"],
                ),
            )
            stale_id = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-human-role-policies/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "role-policy-stale-id",
                },
                payload=_role_policy_payload(
                    revision=2,
                    expected_current_record_id="wrong-record-id",
                    expected_current_role_policy_digest=rev1["role_policy_digest"],
                    supersedes_record_id=rev1["record_id"],
                ),
            )
            digest_drift = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/repository-human-role-policies/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "role-policy-digest-drift",
                },
                payload=_role_policy_payload(
                    revision=2,
                    expected_current_record_id=rev1["record_id"],
                    expected_current_role_policy_digest="f" * 64,
                    supersedes_record_id=rev1["record_id"],
                ),
            )

        self.assertEqual(missing_digest.status_code, 400)
        self.assertEqual(missing_digest.json()["error"]["code"], "invalid_request")
        self.assertEqual(stale_id.status_code, 409)
        self.assertEqual(stale_id.json()["error"]["code"], "role_policy_conflict")
        self.assertEqual(digest_drift.status_code, 409)
        self.assertEqual(digest_drift.json()["error"]["code"], "role_policy_conflict")

    async def test_technical_human_waiver_dry_run_uses_browser_human_session_with_filesystem_store(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = FilesystemRecordStore(Path(tmp_dir))
            classification, role_policy, authz_policy = _seed_waiver_authority(store)
            app, session_manager, human_session = _waiver_session_app(store)

            response = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/technical-human-waivers/apply",
                headers=_browser_mutation_headers(session_manager, human_session),
                payload=_waiver_apply_payload(
                    classification=classification,
                    role_policy=role_policy,
                    authz_policy=authz_policy,
                    mode="dry_run",
                ),
            )
            events = store.list_tenant_technical_human_waiver_event_records(
                repository_id=REPOSITORY_ID,
                product=PRODUCT,
                context=CONTEXT,
            )

        self.assertEqual(response.status_code, 202)
        data = response.json()
        self.assertEqual(data["result"]["mode"], "dry_run")
        self.assertEqual(data["result"]["status"], "would_apply")
        self.assertTrue(data["result"]["dry_run"])
        self.assertEqual(data["result"]["path_result"]["state"], "satisfied")
        self.assertEqual(events, ())

    async def test_technical_human_waiver_apply_requires_browser_human_and_csrf(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = _postgres_store(
                Path(tmp_dir),
                authz_policy_record=_waiver_authz_policy_record(),
            )
            classification, role_policy, authz_policy = _seed_waiver_authority(store)
            payload = _waiver_apply_payload(
                classification=classification,
                role_policy=role_policy,
                authz_policy=authz_policy,
            )
            app, session_manager, human_session = _waiver_session_app(store)

            success = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/technical-human-waivers/apply",
                headers={
                    **_browser_mutation_headers(session_manager, human_session),
                    "Idempotency-Key": "waiver-browser-create",
                },
                payload=payload,
            )
            missing_csrf_session = session_manager.issue(_waiver_human_identity())
            missing_csrf_headers = _browser_mutation_headers(
                session_manager,
                missing_csrf_session,
            )
            missing_csrf_headers.pop("X-CSRF-Token")
            missing_csrf = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/technical-human-waivers/apply",
                headers={
                    **missing_csrf_headers,
                    "Idempotency-Key": "waiver-missing-csrf",
                },
                payload=payload,
            )
            bearer_only = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/technical-human-waivers/apply",
                headers={
                    "Authorization": "Bearer valid-token",
                    "Idempotency-Key": "waiver-bearer-only",
                },
                payload=payload,
            )

        self.assertEqual(success.status_code, 202)
        self.assertEqual(success.json()["result"]["status"], "applied")
        self.assertEqual(success.json()["result"]["path_result"]["state"], "satisfied")
        self.assertEqual(missing_csrf.status_code, 403)
        self.assertEqual(missing_csrf.json()["error"]["code"], "browser_mutation_denied")
        self.assertEqual(bearer_only.status_code, 403)
        self.assertEqual(bearer_only.json()["error"]["code"], "authorization_denied")

    async def test_technical_human_waiver_apply_requires_real_postgresql_store(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            filesystem_store = FilesystemRecordStore(Path(tmp_dir) / "fs")
            classification, role_policy, authz_policy = _seed_waiver_authority(filesystem_store)
            app, session_manager, human_session = _waiver_session_app(filesystem_store)
            filesystem_response = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/technical-human-waivers/apply",
                headers={
                    **_browser_mutation_headers(session_manager, human_session),
                    "Idempotency-Key": "waiver-fs-apply",
                },
                payload=_waiver_apply_payload(
                    classification=classification,
                    role_policy=role_policy,
                    authz_policy=authz_policy,
                ),
            )

            sqlite_store = PostgresRecordStore(
                database_url=f"sqlite+pysqlite:///{Path(tmp_dir) / 'launchplane.sqlite3'}"
            )
            sqlite_store.ensure_schema()
            classification, role_policy, authz_policy = _seed_waiver_authority(sqlite_store)
            app, session_manager, human_session = _waiver_session_app(sqlite_store)
            sqlite_response = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/technical-human-waivers/apply",
                headers={
                    **_browser_mutation_headers(session_manager, human_session),
                    "Idempotency-Key": "waiver-sqlite-apply",
                },
                payload=_waiver_apply_payload(
                    classification=classification,
                    role_policy=role_policy,
                    authz_policy=authz_policy,
                ),
            )
            sqlite_store.close()

        self.assertEqual(filesystem_response.status_code, 503)
        self.assertEqual(
            filesystem_response.json()["error"]["code"],
            "database_storage_required",
        )
        self.assertEqual(sqlite_response.status_code, 503)
        self.assertEqual(
            sqlite_response.json()["error"]["code"],
            "database_storage_required",
        )

    async def test_technical_human_waiver_apply_replays_same_numeric_id_after_login_rename(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = _postgres_store(
                Path(tmp_dir),
                authz_policy_record=_waiver_authz_policy_record(),
            )
            classification, role_policy, authz_policy = _seed_waiver_authority(store)
            payload = _waiver_apply_payload(
                classification=classification,
                role_policy=role_policy,
                authz_policy=authz_policy,
            )
            app, session_manager, human_session = _waiver_session_app(store)
            first = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/technical-human-waivers/apply",
                headers={
                    **_browser_mutation_headers(session_manager, human_session),
                    "Idempotency-Key": "waiver-login-rename",
                },
                payload=payload,
            )
            renamed_session = session_manager.issue(
                _waiver_human_identity(github_id=301, login="renamed-human")
            )
            replay = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/technical-human-waivers/apply",
                headers={
                    **_browser_mutation_headers(session_manager, renamed_session),
                    "Idempotency-Key": "waiver-login-rename",
                },
                payload=payload,
            )
            changed_payload = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/technical-human-waivers/apply",
                headers={
                    **_browser_mutation_headers(
                        session_manager,
                        session_manager.issue(_waiver_human_identity()),
                    ),
                    "Idempotency-Key": "waiver-login-rename",
                },
                payload=_waiver_apply_payload(
                    classification=classification,
                    role_policy=role_policy,
                    authz_policy=authz_policy,
                    reason="Changed body must not replay.",
                ),
            )
            events = store.list_tenant_technical_human_waiver_event_records(
                repository_id=REPOSITORY_ID,
                product=PRODUCT,
                context=CONTEXT,
            )

        self.assertEqual(first.status_code, 202)
        self.assertEqual(replay.status_code, 202)
        replay_data = replay.json()
        self.assertTrue(replay_data["replayed"])
        self.assertEqual(replay_data["original_trace_id"], first.json()["trace_id"])
        self.assertEqual(changed_payload.status_code, 409)
        self.assertEqual(changed_payload.json()["error"]["code"], "idempotency_key_reused")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].authorization.author_login, "human-301")

    async def test_technical_human_waiver_apply_rejects_current_authority_drift(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = _postgres_store(
                Path(tmp_dir),
                authz_policy_record=_waiver_authz_policy_record(),
            )
            classification, role_policy, authz_policy = _seed_waiver_authority(store)
            app, session_manager, human_session = _waiver_session_app(store)
            payload = _waiver_apply_payload(
                classification=classification,
                role_policy=role_policy,
                authz_policy=authz_policy,
            )
            stale_authority = cast(dict[str, object], payload["expected_authority"])
            stale_authority["classification_digest"] = "f" * 64

            response = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/technical-human-waivers/apply",
                headers={
                    **_browser_mutation_headers(session_manager, human_session),
                    "Idempotency-Key": "waiver-authority-drift",
                },
                payload=payload,
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "stale_authority")

    async def test_technical_human_waiver_apply_create_and_revoke(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = _postgres_store(
                Path(tmp_dir),
                authz_policy_record=_waiver_authz_policy_record(),
            )
            classification, role_policy, authz_policy = _seed_waiver_authority(store)
            app, session_manager, human_session = _waiver_session_app(store)
            create_response = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/technical-human-waivers/apply",
                headers={
                    **_browser_mutation_headers(session_manager, human_session),
                    "Idempotency-Key": "waiver-create-revoke",
                },
                payload=_waiver_apply_payload(
                    classification=classification,
                    role_policy=role_policy,
                    authz_policy=authz_policy,
                    source_event_id="comment-create-revoke",
                ),
            )
            create_result = create_response.json()["result"]
            revoke_response = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/technical-human-waivers/apply",
                headers={
                    **_browser_mutation_headers(session_manager, human_session),
                    "Idempotency-Key": "waiver-revoke",
                },
                payload=_waiver_apply_payload(
                    classification=classification,
                    role_policy=role_policy,
                    authz_policy=authz_policy,
                    action="revoked",
                    source_event_id="comment-revoke",
                    reason="Owner revoked exact waiver.",
                    expected_current={
                        "schema_version": 1,
                        "waiver_id": create_result["waiver_id"],
                        "event_digest": create_result["event_digest"],
                    },
                ),
            )

        self.assertEqual(create_response.status_code, 202)
        self.assertEqual(create_result["path_result"]["state"], "satisfied")
        self.assertEqual(revoke_response.status_code, 202)
        revoke_result = revoke_response.json()["result"]
        self.assertEqual(revoke_result["action"], "revoked")
        self.assertEqual(revoke_result["path_result"]["state"], "denied")

    async def test_technical_human_waiver_body_is_bounded_at_64_kib(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = _postgres_store(
                Path(tmp_dir),
                authz_policy_record=_waiver_authz_policy_record(),
            )
            app, session_manager, human_session = _waiver_session_app(store)
            response = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/technical-human-waivers/apply",
                headers={
                    **_browser_mutation_headers(session_manager, human_session),
                    "Content-Type": "application/json",
                    "Idempotency-Key": "waiver-body-limit",
                },
                raw_body=b"{" + (b" " * (64 * 1024)),
            )

        self.assertEqual(response.status_code, 413)
        self.assertEqual(
            response.json()["error"]["message"],
            "Tenant technical human waiver request body is too large.",
        )

    async def test_no_legacy_trusted_maintenance_route(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = _postgres_store(Path(tmp_dir))
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_authz_policy(
                    actions=(
                        "repository_human_role_policy.read",
                        "repository_human_role_policy.write",
                    )
                ),
                record_store_factory=lambda: store,
            )
            response = await _asgi_request(
                app,
                "GET",
                "/v1/work-graph/tenant-admission/trusted-maintenance",
                headers={"Authorization": "Bearer valid-token"},
            )

        self.assertEqual(response.status_code, 404)


def _authz_policy(*, actions: tuple[str, ...]) -> LaunchplaneAuthzPolicy:
    rules = []
    if actions:
        rules.append(
            {
                "repository": "every/verireel",
                "workflow_refs": [
                    "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                ],
                "event_names": ["pull_request"],
                "products": ["launchplane"],
                "contexts": ["launchplane", CONTEXT],
                "actions": list(actions),
            }
        )
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "schema_version": 2,
            "github_actions": rules,
        }
    )


def _apply_payload(
    *,
    revision: int,
    kind: str = "tenant_ui",
    mode: str = "apply",
    expected_current_record_id: str = "",
    supersedes_record_id: str | None = None,
) -> dict[str, object]:
    record: dict[str, object] = {
        "schema_version": 1,
        "repository_id": REPOSITORY_ID,
        "repository_owner_id": REPOSITORY_OWNER_ID,
        "repository": REPOSITORY,
        "product": PRODUCT,
        "context": CONTEXT,
        "classification_kind": kind,
        "classification_revision": revision,
        "classified_at": CLASSIFIED_AT,
        "source": SOURCE,
        "reason": REASON,
    }
    if supersedes_record_id is not None:
        record["supersedes_record_id"] = supersedes_record_id

    return {
        "schema_version": 1,
        "mode": mode,
        "expected_current_record_id": expected_current_record_id,
        "record": record,
    }


def _role_policy_payload(
    *,
    revision: int,
    mode: str = "apply",
    repository_owner_github_ids: tuple[int, ...] = (301,),
    manager_primary_github_ids: tuple[int, ...] = (501,),
    expected_current_record_id: str = "",
    expected_current_role_policy_digest: str = "",
    supersedes_record_id: str | None = None,
    effective_at: str = CLASSIFIED_AT,
    reason: str = "initial role policy",
) -> dict[str, object]:
    record: dict[str, object] = {
        "schema_version": 1,
        "record_id": build_repository_human_role_policy_record_id(
            repository_id=REPOSITORY_ID,
            product=PRODUCT,
            context=CONTEXT,
            role_policy_revision=revision,
        ),
        "repository_id": REPOSITORY_ID,
        "repository_owner_id": REPOSITORY_OWNER_ID,
        "repository": REPOSITORY,
        "product": PRODUCT,
        "context": CONTEXT,
        "status": "active",
        "role_policy_revision": revision,
        "repository_owner_github_ids": repository_owner_github_ids,
        "manager_primary_github_ids": manager_primary_github_ids,
        "manager_backup_github_ids": [],
        "manager_delegations": [],
        "effective_at": effective_at,
        "source": SOURCE,
        "reason": reason,
    }
    if supersedes_record_id is not None:
        record["supersedes_record_id"] = supersedes_record_id

    return {
        "schema_version": 1,
        "mode": mode,
        "expected_current_record_id": expected_current_record_id,
        "expected_current_role_policy_digest": expected_current_role_policy_digest,
        "record": record,
    }


if __name__ == "__main__":
    unittest.main()
