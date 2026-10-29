import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from control_plane import secrets as control_plane_secrets
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import (
    BearerIdentityConfig,
    LaunchplaneAuthzPolicy,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from tests.http_app_test_support import (
    _asgi_get,
    _asgi_request,
    _every_code_read_policy,
    _get_every_code_notification_attempts,
    _get_every_code_pr_feedback,
    _get_every_code_preview_gates,
    _get_every_code_work_request,
    _get_every_code_work_requests,
    _get_preview_pr_feedback_notification_attempts,
    _get_preview_readiness,
    _MissingProductReadStore,
    _RejectingVerifier,
    _seed_every_code_read_records,
)
from tests.support.auth import _identity, _StubVerifier
from tests.support.http import request
from tests.test_engineering_review_run import _pending_record as _pending_review_record


class _EngineeringReviewReadStore:
    def __init__(self) -> None:
        self.record = _pending_review_record()[0]

    def read_engineering_review_run_record(self, run_id: str):  # type: ignore[no-untyped-def]
        if run_id != self.record.run_id:
            raise FileNotFoundError(run_id)
        return self.record

    def list_engineering_review_run_records(self, **_filters: object):  # type: ignore[no-untyped-def]
        return (self.record,)


class _EngineeringReviewWorkerStore(_EngineeringReviewReadStore):
    def __init__(self) -> None:
        super().__init__()
        self.claimed_identity: tuple[str, str] | None = None

    def claim_engineering_review_run_record(
        self,
        *,
        run_id: str,
        worker_runtime_id: str,
        worker_host: str,
    ) -> object:
        if run_id != self.record.run_id:
            raise FileNotFoundError(run_id)
        self.claimed_identity = (worker_runtime_id, worker_host)
        return self.record.model_copy(
            update={
                "state": "claimed",
                "claimed_at": "2026-08-06T00:01:00Z",
                "updated_at": "2026-08-06T00:01:00Z",
                "lease_expires_at": "2026-08-06T00:31:00Z",
                "claim_attempt": 1,
                "fencing_token": 1,
            }
        )

    def list_engineering_review_authority_records(self, **_filters: object):  # type: ignore[no-untyped-def]
        return ()

    def start_engineering_review_run_record(self, **_payload: object):  # type: ignore[no-untyped-def]
        raise AssertionError("start is not expected")

    def submit_engineering_review_run_record(self, _submission: object):  # type: ignore[no-untyped-def]
        raise AssertionError("submit is not expected")

    def fail_engineering_review_run_record(self, **_payload: object):  # type: ignore[no-untyped-def]
        raise AssertionError("fail is not expected")

    def expire_stale_engineering_review_run_records(self, **_payload: object):  # type: ignore[no-untyped-def]
        return ()


class FastApiEveryCodeReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_retired_execution_posts_leave_historical_requests_unchanged(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory) / "state")
            seeded = _seed_every_code_read_records(store)
            before = store.list_every_code_work_request_records()
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy(),
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
                record_store_factory=lambda: store,
            )
            for path in (
                "/v1/every-code/work-requests/create",
                "/v1/every-code/work-requests/claim",
                "/v1/every-code/work-requests/status",
                "/v1/every-code/work-requests/heartbeat",
                "/v1/every-code/work-requests/recover-stale",
                "/v1/every-code/work-requests/rerun",
                "/v1/every-code/pr-feedback",
                "/v1/every-code/pr-feedback/status",
                "/v1/every-code/preview-gates",
                "/v1/every-code/notification-policies/apply",
            ):
                with self.subTest(path=path):
                    response = await request(
                        app,
                        "POST",
                        path,
                        headers={"Authorization": "Bearer worker-token"},
                        payload={"request_id": seeded["request_id"]},
                    )
                    self.assertIn(response.status_code, {404, 405})
            self.assertEqual(store.list_every_code_work_request_records(), before)

    async def test_every_code_read_routes_return_native_payloads(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            seeded = _seed_every_code_read_records(store)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_every_code_read_policy(),
                record_store_factory=lambda: store,
            )

            readiness_response = await _get_preview_readiness(
                app,
                repository="cbusillo/code",
                pr_number="31",
                status="blocked",
            )
            list_response = await _get_every_code_work_requests(
                app,
                state="queued",
                repository="cbusillo/code",
            )
            read_response = await _get_every_code_work_request(
                app,
                seeded["request_id"],
            )
            feedback_response = await _get_every_code_pr_feedback(
                app,
                request_id=seeded["request_id"],
                repository="cbusillo/code",
                pr_number="31",
                status="pending",
            )
            gates_response = await _get_every_code_preview_gates(
                app,
                request_id=seeded["request_id"],
                repository="cbusillo/code",
                pr_number="31",
                status="blocked",
            )
            notification_response = await _get_every_code_notification_attempts(
                app,
                request_id=seeded["request_id"],
                event="work_request_blocked",
                destination_kind="discord",
            )
            preview_notification_response = await _get_preview_pr_feedback_notification_attempts(
                app,
                feedback_id=seeded["preview_feedback_id"],
                event="delivery_skipped",
                destination_kind="discord",
            )

        for response in (
            readiness_response,
            list_response,
            read_response,
            feedback_response,
            gates_response,
            notification_response,
            preview_notification_response,
        ):
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["status"], "ok")
            self.assertIn("trace_id", response.json())

        readiness = readiness_response.json()["readiness"]
        self.assertEqual(readiness["repository"], "cbusillo/code")
        self.assertEqual(readiness["pr_number"], 31)
        self.assertEqual(readiness["status_filter"], "blocked")
        self.assertEqual(readiness["items"][0]["gate_id"], seeded["gate_id"])
        self.assertEqual(readiness["items"][0]["readiness_status"], "needs_attention")

        self.assertEqual(list_response.json()["requests"][0]["request_id"], seeded["request_id"])
        self.assertEqual(read_response.json()["request"]["request_id"], seeded["request_id"])
        self.assertEqual(
            feedback_response.json()["feedback"][0]["feedback_id"], seeded["feedback_id"]
        )
        self.assertEqual(gates_response.json()["gates"][0]["gate_id"], seeded["gate_id"])
        self.assertEqual(
            notification_response.json()["attempts"][0]["attempt_id"],
            seeded["notification_attempt_id"],
        )
        self.assertEqual(
            preview_notification_response.json()["attempts"][0]["attempt_id"],
            seeded["preview_notification_attempt_id"],
        )

    async def test_every_code_worker_token_reads_without_policy(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            seeded = _seed_every_code_read_records(store)
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            responses = (
                await _get_preview_readiness(
                    app,
                    repository="cbusillo/code",
                    authorization="Bearer worker-token",
                ),
                await _get_every_code_work_request(
                    app,
                    seeded["request_id"],
                    authorization="Bearer worker-token",
                ),
                await _get_every_code_pr_feedback(
                    app,
                    request_id=seeded["request_id"],
                    authorization="Bearer worker-token",
                ),
            )
            preview_notification_response = await _get_preview_pr_feedback_notification_attempts(
                app,
                feedback_id=seeded["preview_feedback_id"],
                authorization="Bearer worker-token",
            )

        for response in responses:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["status"], "ok")
        self.assertEqual(preview_notification_response.status_code, 401)
        self.assertEqual(
            preview_notification_response.json()["error"]["code"],
            "authentication_required",
        )

    async def test_every_code_reads_reject_unauthorized_identity(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            _seed_every_code_read_records(store)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
            )

            responses = (
                await _get_every_code_work_requests(app),
                await _get_preview_readiness(app),
                await _get_every_code_notification_attempts(app),
                await _get_preview_pr_feedback_notification_attempts(app),
            )

        for response in responses:
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_every_code_reads_require_store_capability(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_every_code_read_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        responses = (
            await _get_every_code_work_requests(app),
            await _get_every_code_notification_attempts(app),
            await _get_preview_pr_feedback_notification_attempts(app),
        )

        for response in responses:
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["error"]["code"], "database_storage_required")

    async def test_every_code_reads_reject_invalid_query_values(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            _seed_every_code_read_records(store)
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            offset_response = await _get_every_code_work_requests(
                app,
                offset="-1",
                authorization="Bearer worker-token",
            )
            pr_number_response = await _get_every_code_pr_feedback(
                app,
                pr_number="not-a-number",
                authorization="Bearer worker-token",
            )
            status_response = await _get_preview_readiness(
                app,
                status="unknown",
                authorization="Bearer worker-token",
            )

        self.assertEqual(offset_response.status_code, 400)
        self.assertEqual(offset_response.json()["error"]["code"], "invalid_payload")
        self.assertIn("offset must be non-negative", offset_response.json()["error"]["message"])
        self.assertEqual(pr_number_response.status_code, 400)
        self.assertEqual(pr_number_response.json()["error"]["code"], "invalid_payload")
        self.assertEqual(status_response.status_code, 400)
        self.assertEqual(status_response.json()["error"]["code"], "invalid_payload")

    async def test_openapi_includes_every_code_read_contracts(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_every_code_read_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        expected_routes = {
            "/v1/previews/readiness": (
                "read_preview_readiness",
                "PreviewReadinessResponse",
            ),
            "/v1/every-code/work-requests": (
                "list_every_code_work_requests",
                "EveryCodeWorkRequestRecordsResponse",
            ),
            "/v1/every-code/work-requests/{request_id}": (
                "read_every_code_work_request",
                "EveryCodeWorkRequestRecordResponse",
            ),
            "/v1/every-code/pr-feedback": (
                "list_every_code_pr_feedback",
                "EveryCodePrFeedbackRecordsResponse",
            ),
            "/v1/every-code/preview-gates": (
                "list_every_code_preview_gates",
                "EveryCodePreviewGateRecordsResponse",
            ),
            "/v1/every-code/notification-attempts": (
                "list_every_code_notification_attempts",
                "EveryCodeNotificationAttemptRecordsResponse",
            ),
            "/v1/previews/pr-feedback/notification-attempts": (
                "list_preview_pr_feedback_notification_attempts",
                "PreviewPrFeedbackNotificationAttemptRecordsResponse",
            ),
        }
        for path, (operation_id, response_model_name) in expected_routes.items():
            route = openapi["paths"][path]["get"]
            self.assertEqual(route["operationId"], operation_id)
            self.assertEqual(
                route["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
                f"#/components/schemas/{response_model_name}",
            )
            self.assertFalse(
                openapi["components"]["schemas"][response_model_name]["additionalProperties"]
            )
            for status_code in ("400", "401", "403", "404", "503"):
                self.assertIn(
                    "LaunchplaneErrorResponse", json.dumps(route["responses"][status_code])
                )

    async def test_engineering_review_reads_redact_credential_material(self) -> None:
        store = _EngineeringReviewReadStore()
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=LaunchplaneAuthzPolicy(),
            record_store_factory=lambda: store,
            bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
        )

        response = await _asgi_get(
            app,
            f"/v1/engineering-review-runs/{store.record.run_id}",
            headers={"Authorization": "Bearer worker-token"},
        )

        self.assertEqual(response.status_code, 200)
        response_text = response.text
        self.assertNotIn("credential_hash", response_text)
        self.assertNotIn("credential_ciphertext", response_text)
        self.assertNotIn("credential_key_id", response_text)
        self.assertEqual(response.json()["run"]["rollout_mode"], "shadow")
        self.assertFalse(response.json()["run"]["authoritative"])

    async def test_engineering_review_claim_uses_server_bound_worker_identity(self) -> None:
        store = _EngineeringReviewWorkerStore()
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=LaunchplaneAuthzPolicy(),
            record_store_factory=lambda: store,
            bearer_identity_config=BearerIdentityConfig(
                every_code_worker_token="worker-token",
                engineering_review_worker_runtime_id="controlled-worker-1",
                engineering_review_worker_host="controlled-worker.example.test",
            ),
        )

        with patch.object(
            control_plane_secrets,
            "_decrypt_secret_value",
            return_value="scoped-review-credential",
        ):
            response = await _asgi_request(
                app,
                "POST",
                "/v1/engineering-review-runs/claim",
                headers={"Authorization": "Bearer worker-token"},
                payload={"run_id": store.record.run_id},
            )
            forged = await _asgi_request(
                app,
                "POST",
                "/v1/engineering-review-runs/claim",
                headers={"Authorization": "Bearer worker-token"},
                payload={
                    "run_id": store.record.run_id,
                    "worker_runtime_id": "attacker-runtime",
                    "worker_host": "attacker.example.test",
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            store.claimed_identity,
            ("controlled-worker-1", "controlled-worker.example.test"),
        )
        self.assertEqual(response.json()["assignment"]["credential"], "scoped-review-credential")
        self.assertEqual(forged.status_code, 400, forged.text)

    async def test_engineering_review_openapi_has_scoped_write_routes_and_redaction(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=LaunchplaneAuthzPolicy(),
            record_store_factory=_EngineeringReviewReadStore,
            bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
        )

        response = await _asgi_get(app, "/openapi.json")

        self.assertEqual(response.status_code, 200)
        openapi = response.json()
        self.assertEqual(
            openapi["paths"]["/v1/engineering-review-runs/{run_id}"]["get"]["operationId"],
            "read_engineering_review_run",
        )
        self.assertEqual(
            openapi["paths"]["/v1/engineering-review-runs"]["get"]["operationId"],
            "list_engineering_review_runs",
        )
        self.assertEqual(
            openapi["paths"]["/v1/engineering-review-runs/create"]["post"]["operationId"],
            "create_engineering_review_runs",
        )
        self.assertEqual(
            openapi["paths"]["/v1/engineering-review-runs/complete"]["post"]["operationId"],
            "complete_engineering_review_run",
        )
        self.assertEqual(
            openapi["paths"]["/v1/engineering-review-decisions/evaluate"]["post"]["operationId"],
            "evaluate_engineering_review_decision",
        )
        self.assertEqual(
            openapi["paths"]["/v1/engineering-review-decisions/project"]["post"]["operationId"],
            "project_engineering_review_decision",
        )
        create_schema = json.dumps(
            openapi["components"]["schemas"]["EngineeringReviewRunCreateRequest"]
        )
        self.assertIn("work_request_id", create_schema)
        for caller_field in ("model_id", "model_family", "review_slot", "repository", "head_sha"):
            self.assertNotIn(caller_field, create_schema)
        decision_request_schema = json.dumps(
            openapi["components"]["schemas"]["EngineeringReviewDecisionRequest"]
        )
        self.assertIn("work_request_id", decision_request_schema)
        for caller_field in (
            "reviewer",
            "model_id",
            "model_family",
            "review_slot",
            "head_sha",
            "tree_sha",
            "decision_binding_sha256",
        ):
            self.assertNotIn(caller_field, decision_request_schema)
        review_schemas = json.dumps(openapi["components"]["schemas"])
        self.assertNotIn("credential_hash", review_schemas)
        self.assertNotIn("credential_ciphertext", review_schemas)
        self.assertNotIn("credential_key_id", review_schemas)
