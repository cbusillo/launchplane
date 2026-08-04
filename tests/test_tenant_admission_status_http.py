from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from control_plane.contracts.merge_train_controller_state import (
    MergeTrainControllerAdoptionRejectedError,
)
from control_plane.contracts.tenant_merge_eligibility import (
    TenantMergeCandidate,
    TenantRepositoryClassificationKind,
    TenantRepositoryClassificationRecord,
)
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.tenant_admission_controller import (
    TenantAdmissionControllerRunOnceResult,
    TenantAdmissionControllerStaleCandidateError,
    TenantAdmissionPullRequestFacts,
)
from control_plane.tenant_admission_projection import TenantAdmissionProjectionError
from tests.http_app_test_support import _asgi_get, _asgi_request
from tests.support.auth import _StubVerifier, _identity
from tests.test_tenant_admission_http import (
    CONTEXT,
    PRODUCT,
    REPOSITORY,
    REPOSITORY_ID,
    REPOSITORY_OWNER_ID,
    _authz_policy,
    _postgres_store,
)


HEAD_SHA = "a" * 40
PULL_REQUEST_NUMBER = 17


class TenantAdmissionStatusHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_controller_run_once_requires_scoped_authorization(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = _postgres_store(Path(temporary_directory_name), actions=())
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_authz_policy(actions=()),
                record_store_factory=lambda: store,
            )
            response = await _asgi_request(
                app,
                "POST",
                "/v1/work-graph/tenant-admission/controller/run-once",
                headers={"Authorization": "Bearer valid-token"},
                payload=_controller_payload(),
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_controller_run_once_returns_public_result(self) -> None:
        expected_result = _controller_result()
        with TemporaryDirectory() as temporary_directory_name:
            store = _postgres_store(
                Path(temporary_directory_name),
                actions=("tenant_admission.controller.run_once",),
            )
            with (
                patch(
                    "control_plane.http_app.resolve_launchplane_github_token",
                    return_value="managed-token",
                ),
                patch(
                    "control_plane.http_routes.tenant_admission."
                    "execute_tenant_admission_controller_run_once",
                    return_value=expected_result,
                ) as execute,
            ):
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_authz_policy(actions=("tenant_admission.controller.run_once",)),
                    record_store_factory=lambda: store,
                )
                response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/work-graph/tenant-admission/controller/run-once",
                    headers={"Authorization": "Bearer valid-token"},
                    payload=_controller_payload(),
                )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["result"]["outcome"], "not_applicable")
        self.assertEqual(execute.call_args.kwargs["token"], "managed-token")
        self.assertFalse(execute.call_args.kwargs["request"].mutate)

    async def test_controller_run_once_maps_stale_candidate_to_conflict(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = _postgres_store(
                Path(temporary_directory_name),
                actions=("tenant_admission.controller.run_once",),
            )
            with (
                patch(
                    "control_plane.http_app.resolve_launchplane_github_token",
                    return_value="managed-token",
                ),
                patch(
                    "control_plane.http_routes.tenant_admission."
                    "execute_tenant_admission_controller_run_once",
                    side_effect=TenantAdmissionControllerStaleCandidateError("head moved"),
                ),
            ):
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_authz_policy(actions=("tenant_admission.controller.run_once",)),
                    record_store_factory=lambda: store,
                )
                response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/work-graph/tenant-admission/controller/run-once",
                    headers={"Authorization": "Bearer valid-token"},
                    payload=_controller_payload(mutate=True),
                )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["error"]["code"],
            "tenant_admission_stale_candidate",
        )

    async def test_controller_run_once_preserves_foreign_reconciliation_state(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = _postgres_store(
                Path(temporary_directory_name),
                actions=("tenant_admission.controller.run_once",),
            )
            with (
                patch(
                    "control_plane.http_app.resolve_launchplane_github_token",
                    return_value="managed-token",
                ),
                patch(
                    "control_plane.http_routes.tenant_admission."
                    "execute_tenant_admission_controller_run_once",
                    side_effect=MergeTrainControllerAdoptionRejectedError(
                        "state belongs to a different active action"
                    ),
                ),
            ):
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_authz_policy(actions=("tenant_admission.controller.run_once",)),
                    record_store_factory=lambda: store,
                )
                response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/work-graph/tenant-admission/controller/run-once",
                    headers={"Authorization": "Bearer valid-token"},
                    payload=_controller_payload(mutate=True),
                )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["error"]["code"],
            "tenant_admission_reconciliation_required",
        )

    async def test_reads_engineering_status_from_numeric_classification(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = _postgres_store(
                Path(temporary_directory_name),
                actions=("tenant_admission.read",),
            )
            store.write_tenant_repository_classification_record(_classification(kind="engineering"))
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_authz_policy(actions=("tenant_admission.read",)),
                record_store_factory=lambda: store,
            )
            response = await _asgi_get(
                app,
                "/v1/work-graph/tenant-admission/status?" + urlencode(_candidate_payload()),
                headers={"Authorization": "Bearer valid-token"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["read_model"]["category"], "engineering")
        self.assertEqual(
            payload["read_model"]["decision"]["reason_code"],
            "engineering_normal_flow",
        )

    async def test_status_read_requires_scoped_authorization(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = _postgres_store(Path(temporary_directory_name), actions=())
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_authz_policy(actions=()),
                record_store_factory=lambda: store,
            )
            response = await _asgi_get(
                app,
                "/v1/work-graph/tenant-admission/status?" + urlencode(_candidate_payload()),
                headers={"Authorization": "Bearer valid-token"},
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_reconcile_verifies_current_github_identity_and_skips_engineering_projection(
        self,
    ) -> None:
        calls: list[dict[str, object]] = []

        def github_api(**kwargs: object) -> object:
            calls.append(kwargs)
            return _pull_request_payload()

        with TemporaryDirectory() as temporary_directory_name:
            store = _postgres_store(
                Path(temporary_directory_name),
                actions=("tenant_admission.reconcile",),
            )
            store.write_tenant_repository_classification_record(_classification(kind="engineering"))
            with (
                patch(
                    "control_plane.http_app.resolve_launchplane_github_token",
                    return_value="managed-token",
                ),
                patch("control_plane.http_app.github_api_request", side_effect=github_api),
            ):
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_authz_policy(actions=("tenant_admission.reconcile",)),
                    record_store_factory=lambda: store,
                )
            response = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/status/reconcile",
                headers={"Authorization": "Bearer valid-token"},
                payload={"schema_version": 1, "candidate": _candidate_payload()},
            )

        self.assertEqual(response.status_code, 202)
        result = response.json()["result"]
        self.assertEqual(result["read_model"]["category"], "engineering")
        self.assertEqual(result["write_result"]["status"], "not_required")
        self.assertEqual(len(calls), 1)

    async def test_reconcile_rejects_stale_github_head_before_projection(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = _postgres_store(
                Path(temporary_directory_name),
                actions=("tenant_admission.reconcile",),
            )
            store.write_tenant_repository_classification_record(_classification())
            with (
                patch(
                    "control_plane.http_app.resolve_launchplane_github_token",
                    return_value="managed-token",
                ),
                patch(
                    "control_plane.http_app.github_api_request",
                    return_value=_pull_request_payload(head_sha="b" * 40),
                ),
            ):
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_authz_policy(actions=("tenant_admission.reconcile",)),
                    record_store_factory=lambda: store,
                )
            response = await _asgi_request(
                app,
                "POST",
                "/v1/tenant-admission/status/reconcile",
                headers={"Authorization": "Bearer valid-token"},
                payload={"schema_version": 1, "candidate": _candidate_payload()},
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["error"]["code"],
            "tenant_admission_stale_candidate",
        )

    async def test_projection_delivery_failure_is_retryable_and_not_success(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = _postgres_store(
                Path(temporary_directory_name),
                actions=("tenant_admission.reconcile",),
            )
            store.write_tenant_repository_classification_record(_classification())
            with (
                patch(
                    "control_plane.http_app.resolve_launchplane_github_token",
                    return_value="managed-token",
                ),
                patch(
                    "control_plane.http_app.github_api_request",
                    return_value=_pull_request_payload(),
                ),
                patch(
                    "control_plane.http_routes.tenant_admission.write_tenant_admission_projection",
                    side_effect=TenantAdmissionProjectionError("delivery failed"),
                ),
            ):
                app = create_launchplane_fastapi_app(
                    verifier=_StubVerifier(_identity()),
                    authz_policy=_authz_policy(actions=("tenant_admission.reconcile",)),
                    record_store_factory=lambda: store,
                )
                response = await _asgi_request(
                    app,
                    "POST",
                    "/v1/tenant-admission/status/reconcile",
                    headers={"Authorization": "Bearer valid-token"},
                    payload={"schema_version": 1, "candidate": _candidate_payload()},
                )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["error"]["code"],
            "tenant_admission_projection_unavailable",
        )


def _classification(
    *, kind: TenantRepositoryClassificationKind = "tenant_ui"
) -> TenantRepositoryClassificationRecord:
    return TenantRepositoryClassificationRecord(
        repository_id=REPOSITORY_ID,
        repository_owner_id=REPOSITORY_OWNER_ID,
        repository=REPOSITORY,
        product=PRODUCT,
        context=CONTEXT,
        classification_kind=kind,
        classification_revision=1,
        classified_at="2026-07-31T11:00:00Z",
        source="test:tenant-admission-status-http",
        reason="HTTP status test classification",
    )


def _candidate_payload() -> dict[str, object]:
    return {
        "product": PRODUCT,
        "context": CONTEXT,
        "repository_id": REPOSITORY_ID,
        "repository_owner_id": REPOSITORY_OWNER_ID,
        "repository": REPOSITORY,
        "pull_request_number": PULL_REQUEST_NUMBER,
        "head_sha": HEAD_SHA,
    }


def _controller_payload(*, mutate: bool = False) -> dict[str, object]:
    return {
        "schema_version": 1,
        "candidate": _candidate_payload(),
        "base_branch": "main",
        "merge_method": "merge",
        "mutate": mutate,
    }


def _controller_result() -> TenantAdmissionControllerRunOnceResult:
    candidate = TenantMergeCandidate.model_validate(_candidate_payload())
    facts = TenantAdmissionPullRequestFacts(
        repository=REPOSITORY,
        pull_request_number=PULL_REQUEST_NUMBER,
        pull_request_url=f"https://github.com/{REPOSITORY}/pull/{PULL_REQUEST_NUMBER}",
        state="open",
        merged=False,
        head_sha=HEAD_SHA,
        base_branch="main",
        base_sha="b" * 40,
    )
    return TenantAdmissionControllerRunOnceResult(
        outcome="not_applicable",
        candidate=candidate,
        base_branch="main",
        merge_method="merge",
        pull_request_facts=facts,
        detail="Engineering repositories retain their existing merge flow.",
    )


def _pull_request_payload(*, head_sha: str = HEAD_SHA) -> dict[str, object]:
    return {
        "number": PULL_REQUEST_NUMBER,
        "state": "open",
        "html_url": f"https://github.com/{REPOSITORY}/pull/{PULL_REQUEST_NUMBER}",
        "base": {
            "repo": {
                "id": int(REPOSITORY_ID),
                "full_name": REPOSITORY,
                "owner": {"id": int(REPOSITORY_OWNER_ID)},
            }
        },
        "head": {"sha": head_sha},
    }
