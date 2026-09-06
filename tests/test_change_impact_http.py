from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
import unittest

from fastapi import FastAPI, HTTPException

from control_plane.change_impact_github import (
    ChangeImpactRepositoryEvidenceError,
    ChangeImpactRepositoryEvidenceStaleError,
)
from control_plane.change_impact_service import ChangeImpactRepositoryEvidenceProvider
from control_plane.contracts.change_impact import (
    ChangeImpactChangedFileEvidence,
    ChangeImpactComponentRule,
    ChangeImpactPolicyRecord,
    ChangeImpactProductScope,
    ChangeImpactRepositoryEvidence,
    ChangeImpactStoredEvidence,
    ChangeImpactTarget,
    ChangeImpactTargetReference,
)
from control_plane.http_routes.change_impact import (
    CHANGE_IMPACT_EVALUATION_ROUTE,
    CHANGE_IMPACT_POLICY_APPLY_ROUTE,
    CHANGE_IMPACT_POLICY_READ_ROUTE,
    ChangeImpactReadRouteDependencies,
    ChangeImpactWriteRouteDependencies,
    register_change_impact_read_routes,
    register_change_impact_write_routes,
)
from control_plane.http_app import _BOUNDED_REQUEST_BODY_CONTRACTS
from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from control_plane.service_auth import (
    GitHubActionsIdentity,
    GitHubHumanIdentity,
    LaunchplaneIdentity,
    LocalOperatorIdentity,
    action_safety,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.http import lifespan_client


REPOSITORY_ID = "1001"
REPOSITORY_OWNER_ID = "2001"
REPOSITORY = "example/shared-addons"
HEAD_SHA = "a" * 40
TREE_SHA = "b" * 40
MERGE_SHA = "e" * 40


def _human_identity() -> GitHubHumanIdentity:
    return GitHubHumanIdentity(
        login="human",
        github_id=1001,
        name="Human",
        email="",
        organizations=frozenset(),
        teams=frozenset(),
        role="admin",
    )


def _actions_identity(
    *,
    repository: str = REPOSITORY,
    repository_id: str = REPOSITORY_ID,
    head_sha: str = MERGE_SHA,
) -> GitHubActionsIdentity:
    owner = repository.split("/", maxsplit=1)[0]
    return GitHubActionsIdentity(
        repository=repository,
        repository_owner=owner,
        workflow_ref=f"{repository}/.github/workflows/ci.yml@refs/heads/main",
        job_workflow_ref="",
        ref="refs/pull/2000/merge",
        ref_type="branch",
        event_name="pull_request",
        environment="",
        subject=f"repo:{repository}:pull_request",
        sha=head_sha,
        raw_claims={},
        repository_id=repository_id,
        repository_owner_id=REPOSITORY_OWNER_ID,
    )


def _policy() -> ChangeImpactPolicyRecord:
    return ChangeImpactPolicyRecord(
        repository_id=REPOSITORY_ID,
        repository_owner_id=REPOSITORY_OWNER_ID,
        repository=REPOSITORY,
        policy_revision=1,
        component_rules=(
            ChangeImpactComponentRule(
                component="generic-web-runtime",
                path_prefixes=("src/runtime",),
                affected_products=(
                    ChangeImpactProductScope(product="generic-web-a", system="web"),
                ),
                review_tier="routine",
                reason="Runtime code reaches product A.",
            ),
            ChangeImpactComponentRule(
                component="sensitive-auth",
                path_prefixes=("control_plane/service_auth.py",),
                affected_products=(),
                review_tier="sensitive",
                reason="Service authentication is sensitive engineering work.",
            ),
        ),
        effective_at="2026-08-06T00:00:00Z",
        source="test",
        reason="HTTP policy contract.",
    )


def _repository_evidence(path: str = "src/runtime/app.py") -> ChangeImpactRepositoryEvidence:
    return ChangeImpactRepositoryEvidence(
        target=ChangeImpactTarget(
            repository_id=REPOSITORY_ID,
            repository_owner_id=REPOSITORY_OWNER_ID,
            repository=REPOSITORY,
            pull_request_number=2000,
            head_sha=HEAD_SHA,
            tree_sha=TREE_SHA,
        ),
        merge_commit_sha=MERGE_SHA,
        changed_files=(ChangeImpactChangedFileEvidence(path=path),),
    )


class _EvidenceProvider(ChangeImpactRepositoryEvidenceProvider):
    def __init__(
        self,
        evidence: ChangeImpactRepositoryEvidence | Exception,
    ) -> None:
        self.evidence = evidence
        self.calls: list[ChangeImpactTargetReference] = []

    def resolve(self, target: ChangeImpactTargetReference) -> ChangeImpactRepositoryEvidence:
        self.calls.append(target)
        if isinstance(self.evidence, Exception):
            raise self.evidence
        return self.evidence


class _ChangeImpactStore(FilesystemRecordStore):
    def __init__(
        self,
        root: Path,
        stored_evidence: tuple[ChangeImpactStoredEvidence, ...] = (),
    ) -> None:
        super().__init__(root)
        self._stored_evidence = stored_evidence
        self.last_evidence_lookup: tuple[str, int, str, str] | None = None

    def list_change_impact_stored_evidence(
        self,
        *,
        repository_id: str,
        pull_request_number: int,
        head_sha: str,
        tree_sha: str,
    ) -> tuple[ChangeImpactStoredEvidence, ...]:
        self.last_evidence_lookup = (
            repository_id,
            pull_request_number,
            head_sha,
            tree_sha,
        )
        return self._stored_evidence


class _AuditedChangeImpactStore(PostgresRecordStore):
    def __init__(self, root: Path) -> None:
        super().__init__(database_url=f"sqlite+pysqlite:///{root / 'policy.sqlite'}")
        self.ensure_schema()
        self.last_evidence_lookup: tuple[str, int, str, str] | None = None

    def list_change_impact_stored_evidence(
        self,
        *,
        repository_id: str,
        pull_request_number: int,
        head_sha: str,
        tree_sha: str,
    ) -> tuple[ChangeImpactStoredEvidence, ...]:
        self.last_evidence_lookup = (repository_id, pull_request_number, head_sha, tree_sha)
        return (_stored_dependency(),)


def _stored_dependency() -> ChangeImpactStoredEvidence:
    return ChangeImpactStoredEvidence(
        record_id="dependency-record-1",
        component="generic-web-runtime",
        affected_products=(ChangeImpactProductScope(product="generic-web-a", system="web"),),
        kind="dependency",
        reason="Launchplane storage maps the component to product A.",
    )


def _http_error(
    *,
    status_code: int,
    trace_id: str,
    code: str,
    message: str,
    authz: dict[str, object] | None = None,
) -> HTTPException:
    detail: dict[str, object] = {
        "status_code": status_code,
        "trace_id": trace_id,
        "code": code,
        "message": message,
    }
    if authz is not None:
        detail["authz"] = authz
    return HTTPException(status_code=status_code, detail=detail)


def _app(
    *,
    store: object,
    provider: ChangeImpactRepositoryEvidenceProvider,
    identity: LaunchplaneIdentity | None = None,
) -> FastAPI:
    resolved_identity = identity or _human_identity()
    common = ReadRouteDependencies(
        read_identity=lambda: resolved_identity,
        get_record_store=lambda: store,
        next_trace_id=lambda: "trace-change-impact",
        authorization_allows=lambda **_: True,
        http_error=_http_error,
        error_response_model=dict,  # type: ignore[arg-type]
    )
    writes = ChangeImpactWriteRouteDependencies(
        read_write_identity=lambda: resolved_identity,
        get_record_store=lambda: store,
        next_trace_id=lambda: "trace-change-impact-write",
        authorization_allows=lambda **_: True,
        http_error=_http_error,
        error_response_model=dict,  # type: ignore[arg-type]
    )
    app = FastAPI()
    registrar = cast(ApiRouteRegistrar, app)
    register_change_impact_read_routes(
        registrar,
        dependencies=ChangeImpactReadRouteDependencies(
            common=common,
            read_evaluation_identity=lambda: resolved_identity,
            repository_evidence_provider=provider,
        ),
    )
    register_change_impact_write_routes(registrar, dependencies=writes)
    return app


class ChangeImpactHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_audited_v2_policy_dry_run_is_available_but_activation_is_blocked(self) -> None:
        from tests.test_change_impact_v2 import _rule, _v2_policy

        with TemporaryDirectory() as directory:
            store = _AuditedChangeImpactStore(Path(directory))
            self.addCleanup(store.close)
            app = _app(
                store=store,
                provider=_EvidenceProvider(_repository_evidence()),
                identity=LocalOperatorIdentity(subject="operator:test", token_label="test"),
            )
            policy = _v2_policy(_rule("docs", "docs"))
            async with lifespan_client(app) as client:
                dry_run = await client.post(
                    CHANGE_IMPACT_POLICY_APPLY_ROUTE,
                    json={"mode": "dry_run", "record": policy.model_dump()},
                )
                applied = await client.post(
                    CHANGE_IMPACT_POLICY_APPLY_ROUTE,
                    json={"mode": "apply", "record": policy.model_dump()},
                )
                ambiguous = _v2_policy(_rule("first", "docs"), _rule("second", "docs"))
                invalid = await client.post(
                    CHANGE_IMPACT_POLICY_APPLY_ROUTE,
                    json={"mode": "dry_run", "record": ambiguous.model_dump()},
                )
            self.assertEqual(dry_run.status_code, 202, dry_run.text)
            self.assertEqual(dry_run.json()["result"]["attribution_status"], "not_applied")
            self.assertIsNone(dry_run.json()["result"]["audit"])
            self.assertEqual(applied.status_code, 409, applied.text)
            self.assertEqual(invalid.status_code, 409, invalid.text)
            self.assertEqual(store.list_change_impact_policy_records(), ())

    async def test_evaluation_exposes_server_derived_coverage_gaps(self) -> None:
        with TemporaryDirectory() as directory:
            store = _ChangeImpactStore(Path(directory))
            store.write_change_impact_policy_record(_policy())
            provider = _EvidenceProvider(_repository_evidence("unmapped/file.py"))
            app = _app(store=store, provider=provider)

            async with lifespan_client(app) as client:
                response = await client.post(
                    CHANGE_IMPACT_EVALUATION_ROUTE,
                    json={"target": {"repository": REPOSITORY, "pull_request_number": 2000}},
                )
            self.assertEqual(response.status_code, 200, response.text)
            evaluation = response.json()["evaluation"]
            self.assertEqual(evaluation["reason_code"], "policy_coverage_incomplete")
            self.assertEqual(evaluation["status"], "unknown")
            self.assertEqual(evaluation["owner_impact"], "unknown")
            self.assertEqual(
                evaluation["coverage"],
                {
                    "state": "incomplete",
                    "unmatched_path_count": 1,
                    "unmatched_path_samples": ["unmapped/file.py"],
                    "truncated": False,
                },
            )

    async def test_apply_read_and_server_derived_evaluate_are_authoritative(self) -> None:
        with TemporaryDirectory() as directory:
            store = _AuditedChangeImpactStore(Path(directory))
            self.addCleanup(store.close)
            provider = _EvidenceProvider(_repository_evidence())
            app = _app(
                store=store,
                provider=provider,
                identity=LocalOperatorIdentity(subject="operator:test", token_label="test"),
            )
            policy = _policy()

            async with lifespan_client(app) as client:
                applied = await client.post(
                    CHANGE_IMPACT_POLICY_APPLY_ROUTE,
                    json={"schema_version": 1, "mode": "apply", "record": policy.model_dump()},
                )
                self.assertEqual(applied.status_code, 202, applied.text)

                read_response = await client.get(
                    CHANGE_IMPACT_POLICY_READ_ROUTE,
                    params={"repository_id": REPOSITORY_ID},
                )
                self.assertEqual(read_response.status_code, 200, read_response.text)
                self.assertNotIn("authoritative", read_response.json()["read_model"])

                evaluated = await client.post(
                    CHANGE_IMPACT_EVALUATION_ROUTE,
                    json={
                        "schema_version": 1,
                        "target": {
                            "schema_version": 1,
                            "repository": REPOSITORY,
                            "pull_request_number": 2000,
                        },
                        "metadata": {"request_id": "request-1", "reason": "impact check"},
                    },
                )
                self.assertEqual(evaluated.status_code, 200, evaluated.text)
                evaluation = evaluated.json()["evaluation"]
                self.assertEqual(evaluation["status"], "success")
                self.assertEqual(evaluation["engineering_review_tier"], "routine")
                self.assertEqual(evaluation["required_engineering_review_count"], 1)
                self.assertEqual(evaluation["owner_impact"], "required")
                self.assertNotIn("authoritative", evaluation)

            self.assertEqual(len(provider.calls), 1)
            self.assertEqual(
                store.last_evidence_lookup,
                (REPOSITORY_ID, 2000, HEAD_SHA, TREE_SHA),
            )

    async def test_callers_cannot_inject_or_omit_changed_file_authority(self) -> None:
        with TemporaryDirectory() as directory:
            store = _ChangeImpactStore(Path(directory))
            store.write_change_impact_policy_record(_policy())
            provider = _EvidenceProvider(_repository_evidence("control_plane/service_auth.py"))
            app = _app(store=store, provider=provider)

            async with lifespan_client(app) as client:
                injected = await client.post(
                    CHANGE_IMPACT_EVALUATION_ROUTE,
                    json={
                        "target": {
                            "repository": REPOSITORY,
                            "pull_request_number": 2000,
                        },
                        "changed_files": [{"path": ".github/workflows/ci.yml"}],
                        "dependency_evidence": [
                            {
                                "source": "reviewer_validation",
                                "component": "sensitive-auth",
                                "reason": "caller supplied",
                            }
                        ],
                    },
                )
                self.assertEqual(injected.status_code, 422, injected.text)
                self.assertEqual(provider.calls, [])

                evaluated = await client.post(
                    CHANGE_IMPACT_EVALUATION_ROUTE,
                    json={
                        "target": {
                            "repository": REPOSITORY,
                            "pull_request_number": 2000,
                        }
                    },
                )
                self.assertEqual(evaluated.status_code, 200, evaluated.text)
                evaluation = evaluated.json()["evaluation"]
                self.assertEqual(evaluation["status"], "success")
                self.assertEqual(evaluation["engineering_review_tier"], "sensitive")
                self.assertEqual(evaluation["required_engineering_review_count"], 2)

    async def test_github_actions_callers_are_bound_to_oidc_repo_and_head(self) -> None:
        with TemporaryDirectory() as directory:
            store = _ChangeImpactStore(Path(directory))
            store.write_change_impact_policy_record(_policy())

            wrong_repo_provider = _EvidenceProvider(_repository_evidence())
            wrong_repo_app = _app(
                store=store,
                provider=wrong_repo_provider,
                identity=_actions_identity(repository="example/other", repository_id="9999"),
            )
            async with lifespan_client(wrong_repo_app) as client:
                denied = await client.post(
                    CHANGE_IMPACT_EVALUATION_ROUTE,
                    json={
                        "target": {
                            "repository": REPOSITORY,
                            "pull_request_number": 2000,
                        }
                    },
                )
                self.assertEqual(denied.status_code, 403, denied.text)
                self.assertEqual(wrong_repo_provider.calls, [])

            stale_head_app = _app(
                store=store,
                provider=_EvidenceProvider(_repository_evidence("control_plane/service_auth.py")),
                identity=_actions_identity(head_sha="c" * 40),
            )
            async with lifespan_client(stale_head_app) as client:
                stale = await client.post(
                    CHANGE_IMPACT_EVALUATION_ROUTE,
                    json={
                        "target": {
                            "repository": REPOSITORY,
                            "pull_request_number": 2000,
                        }
                    },
                )
                self.assertEqual(stale.status_code, 200, stale.text)
                self.assertEqual(stale.json()["evaluation"]["status"], "stale_head")
                self.assertEqual(
                    stale.json()["evaluation"]["required_engineering_review_count"],
                    2,
                )

    async def test_provider_stale_and_unavailable_fail_closed(self) -> None:
        with TemporaryDirectory() as directory:
            store = _ChangeImpactStore(Path(directory))
            request = {
                "target": {
                    "repository": REPOSITORY,
                    "pull_request_number": 2000,
                }
            }
            stale_app = _app(
                store=store,
                provider=_EvidenceProvider(
                    ChangeImpactRepositoryEvidenceStaleError("target changed")
                ),
            )
            async with lifespan_client(stale_app) as client:
                stale = await client.post(CHANGE_IMPACT_EVALUATION_ROUTE, json=request)
                self.assertEqual(stale.status_code, 409, stale.text)

            unavailable_app = _app(
                store=store,
                provider=_EvidenceProvider(ChangeImpactRepositoryEvidenceError("provider failed")),
            )
            async with lifespan_client(unavailable_app) as client:
                unavailable = await client.post(CHANGE_IMPACT_EVALUATION_ROUTE, json=request)
                self.assertEqual(unavailable.status_code, 503, unavailable.text)

    def test_openapi_request_has_no_caller_evidence_fields(self) -> None:
        with TemporaryDirectory() as directory:
            app = _app(
                store=_ChangeImpactStore(Path(directory)),
                provider=_EvidenceProvider(_repository_evidence()),
            )
            schema = app.openapi()

        operation = schema["paths"][CHANGE_IMPACT_EVALUATION_ROUTE]["post"]
        request_ref = operation["requestBody"]["content"]["application/json"]["schema"]["$ref"]
        request_name = request_ref.rsplit("/", maxsplit=1)[1]
        request_schema = schema["components"]["schemas"][request_name]
        self.assertEqual(
            set(request_schema["properties"]),
            {"schema_version", "target", "metadata"},
        )
        target_ref = request_schema["properties"]["target"]["$ref"]
        target_name = target_ref.rsplit("/", maxsplit=1)[1]
        target_schema = schema["components"]["schemas"][target_name]
        self.assertEqual(
            set(target_schema["properties"]),
            {"schema_version", "repository", "pull_request_number"},
        )
        request_contract = json.dumps(request_schema, sort_keys=True)
        for forbidden in (
            "changed_files",
            "dependency_evidence",
            "expected_head_sha",
            "expected_tree_sha",
            "sensitive_areas",
            "reviewer_validation",
            "claimed_policy_revision",
            "claimed_policy_digest",
        ):
            self.assertNotIn(forbidden, request_contract)

        self.assertIn(CHANGE_IMPACT_POLICY_READ_ROUTE, schema["paths"])
        self.assertIn(CHANGE_IMPACT_POLICY_APPLY_ROUTE, schema["paths"])
        self.assertEqual(action_safety("change_impact_policy.write"), "policy_admin")
        self.assertEqual(action_safety("change_impact_policy.read"), "read")
        self.assertEqual(action_safety("change_impact_evaluation.read"), "read")

    def test_evaluation_route_has_small_json_body_guard(self) -> None:
        self.assertEqual(
            _BOUNDED_REQUEST_BODY_CONTRACTS[CHANGE_IMPACT_EVALUATION_ROUTE],
            ("Change impact evaluation", 16 * 1024, True, True),
        )


if __name__ == "__main__":
    unittest.main()
