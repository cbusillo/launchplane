import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

import click

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


def _human_identity(*, github_id: int = 301, login: str = "human-301") -> GitHubHumanIdentity:
    return GitHubHumanIdentity(
        login=login,
        github_id=github_id,
        name="Human Owner",
        email="human-owner@example.com",
        organizations=frozenset(),
        teams=frozenset(),
        role="read_only",
    )


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
    human_session = session_manager.issue(identity or _human_identity())
    app = create_launchplane_fastapi_app(
        verifier=_StubVerifier(_identity()),
        authz_policy=_trusted_maintenance_authz_policy_record(actions=actions).policy,
        record_store_factory=lambda: store,
        human_session_manager=session_manager,
    )
    return app, session_manager, human_session


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


def _tenant_ui_classification_record(
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
    classification = _tenant_ui_classification_record()
    paths = TenantMergeEligibilityEvidenceInputs()
    decision = evaluate_tenant_merge_eligibility(
        candidate=candidate,
        classification_lookup=TenantRepositoryClassificationLookup(
            status="available",
            records=(classification,),
        ),
        evaluated_at=CLASSIFIED_AT,
    )
    admission = TenantAdmissionStatusReadModel(
        category="eligible",
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
        outcome="ready",
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
    async def test_read_only_evaluation_exposes_checks_without_retired_human_actions(
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
        self.assertEqual(read_model["evaluation"]["outcome"], "ready")
        self.assertEqual(
            read_model["evaluation"]["technical_checks"]["status"],
            "pass",
        )
        self.assertEqual(read_model["human_actions"], [])

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

    async def test_no_legacy_trusted_maintenance_route(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            store = _postgres_store(Path(tmp_dir))
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_authz_policy(
                    actions=(
                        "trusted_maintenance_policy.read",
                        "trusted_maintenance_policy.write",
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


if __name__ == "__main__":
    unittest.main()
