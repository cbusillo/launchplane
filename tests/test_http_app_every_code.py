import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import (
    Any,
    cast,
)
from unittest.mock import patch

from a2wsgi import WSGIMiddleware
from starlette.types import ASGIApp

from control_plane import secrets as control_plane_secrets
from control_plane.contracts.every_code_notifications import (
    EveryCodeNotificationDestination,
    EveryCodeNotificationPolicyRecord,
)
from control_plane.contracts.every_code_pr_feedback_record import EveryCodePrFeedbackRecord
from control_plane.contracts.every_code_work_request import (
    EveryCodeWorkRequestStatusUpdate,
    apply_every_code_work_request_status,
)
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import (
    BearerIdentityConfig,
    LaunchplaneAuthzPolicy,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from tests.async_case import AsyncTestCase
from tests.http_app_test_support import (
    _asgi_get,
    _asgi_request,
    _every_code_pr_feedback_payload,
    _every_code_preview_gate_payload,
    _every_code_read_policy,
    _every_code_work_request_claim_policy,
    _every_code_work_request_create_payload,
    _every_code_work_request_rerun_policy,
    _every_code_work_request_status_policy,
    _every_code_work_request_write_policy,
    _EveryCodeClaimReplayOnlyStore,
    _EveryCodeRerunReplayOnlyStore,
    _EveryCodeStatusReplayOnlyStore,
    _get_every_code_notification_attempts,
    _get_every_code_pr_feedback,
    _get_every_code_preview_gates,
    _get_every_code_summary,
    _get_every_code_work_request,
    _get_every_code_work_requests,
    _get_preview_pr_feedback_notification_attempts,
    _get_preview_readiness,
    _MissingProductReadStore,
    _post_every_code_pr_feedback,
    _post_every_code_pr_feedback_status,
    _post_every_code_preview_gate,
    _post_every_code_work_request_claim,
    _post_every_code_work_request_create,
    _post_every_code_work_request_rerun,
    _post_every_code_work_request_status,
    _RejectingVerifier,
    _seed_every_code_claim_request,
    _seed_every_code_read_records,
    _seed_every_code_rerun_intent,
)
from tests.test_service import (
    _identity,
    _sqlite_database_url,
    _StubVerifier,
    create_launchplane_service_app,
)


class FastApiEveryCodeReadTests(AsyncTestCase):
    async def test_every_code_work_request_create_queues_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_every_code_work_request_write_policy(),
                record_store_factory=lambda: store,
            )

            response = await _post_every_code_work_request_create(
                app,
                _every_code_work_request_create_payload(),
                idempotency_key="every-code-create-code-123",
            )
            stored_requests = store.list_every_code_work_request_records(state="queued")

        payload = response.json()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(payload["records"]["state"], "queued")
        self.assertEqual(payload["result"]["request"]["state"], "queued")
        self.assertEqual(payload["records"]["request_id"], stored_requests[0].request_id)
        self.assertEqual(stored_requests[0].repository, "cbusillo/code")

    async def test_every_code_work_request_create_replays_idempotency(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_every_code_work_request_write_policy(),
                record_store_factory=lambda: store,
            )
            payload = _every_code_work_request_create_payload()

            first_response = await _post_every_code_work_request_create(
                app,
                payload,
                idempotency_key="every-code-create-code-123",
            )
            second_response = await _post_every_code_work_request_create(
                app,
                payload,
                idempotency_key="every-code-create-code-123",
            )
            stored_requests = store.list_every_code_work_request_records(state="queued")

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 202)
        self.assertTrue(second_response.json()["replayed"])
        self.assertEqual(
            second_response.json()["original_trace_id"],
            first_response.json()["trace_id"],
        )
        self.assertEqual(len(stored_requests), 1)

    async def test_every_code_work_request_create_rejects_reused_idempotency_key(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_every_code_work_request_write_policy(),
                record_store_factory=lambda: store,
            )

            first_response = await _post_every_code_work_request_create(
                app,
                _every_code_work_request_create_payload(),
                idempotency_key="every-code-create-code-123",
            )
            conflict_response = await _post_every_code_work_request_create(
                app,
                _every_code_work_request_create_payload(issue_number=124),
                idempotency_key="every-code-create-code-123",
            )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(conflict_response.status_code, 409)
        self.assertEqual(conflict_response.json()["error"]["code"], "idempotency_key_reused")

    async def test_every_code_work_request_create_rejects_unauthorized_identity(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
            )

            response = await _post_every_code_work_request_create(
                app,
                _every_code_work_request_create_payload(),
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_every_code_work_request_create_rejects_invalid_record_payload(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_every_code_work_request_write_policy(),
                record_store_factory=lambda: store,
            )

            response = await _post_every_code_work_request_create(
                app,
                {
                    **_every_code_work_request_create_payload(),
                    "repository": "cbusillo-code",
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")

    async def test_every_code_work_request_create_rejects_worker_token(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=_every_code_work_request_write_policy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            response = await _post_every_code_work_request_create(
                app,
                _every_code_work_request_create_payload(),
                authorization="Bearer worker-token",
            )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "authentication_required")

    async def test_every_code_work_request_create_requires_store_capability(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_every_code_work_request_write_policy(),
            record_store_factory=lambda: _MissingProductReadStore(),
        )

        response = await _post_every_code_work_request_create(
            app,
            _every_code_work_request_create_payload(),
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "database_storage_required")

    async def test_every_code_work_request_claim_accepts_worker_token(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            seeded = _seed_every_code_claim_request(store)
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            response = await _post_every_code_work_request_claim(
                app,
                {"request_id": seeded.request_id, "host": "Chris-Studio"},
                authorization="Bearer worker-token",
                idempotency_key="every-code-worker-claim",
            )
            stored_request = store.read_every_code_work_request_record(seeded.request_id)

        payload = response.json()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(payload["records"]["request_id"], seeded.request_id)
        self.assertEqual(payload["records"]["state"], "claimed")
        self.assertEqual(payload["result"]["request"]["state"], "claimed")
        self.assertEqual(payload["result"]["request"]["claimed_by_host"], "Chris-Studio")
        self.assertEqual(stored_request.state, "claimed")
        self.assertEqual(stored_request.claimed_by_host, "Chris-Studio")

    async def test_every_code_work_request_claim_accepts_authorized_identity(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            seeded = _seed_every_code_claim_request(store)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_every_code_work_request_claim_policy(),
                record_store_factory=lambda: store,
            )

            response = await _post_every_code_work_request_claim(
                app,
                {"request_id": seeded.request_id, "host": "Runner-Host"},
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["result"]["request"]["claimed_by_host"], "Runner-Host")

    async def test_every_code_work_request_claim_replays_authorized_idempotency(
        self,
    ) -> None:
        payload: dict[str, object] = {
            "request_id": "every-code-cbusillo-code-123-test",
            "host": "Runner-Host",
        }
        store = _EveryCodeClaimReplayOnlyStore(
            payload=payload,
            idempotency_key="every-code-claim-replay",
        )
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_every_code_work_request_claim_policy(),
            record_store_factory=lambda: store,
        )

        response = await _post_every_code_work_request_claim(
            app,
            payload,
            idempotency_key="every-code-claim-replay",
        )

        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.json()["replayed"])
        self.assertEqual(response.json()["original_trace_id"], "launchplane_req_original")
        self.assertEqual(store.read_idempotency_calls, 1)
        self.assertEqual(store.claim_calls, 0)

    async def test_every_code_work_request_claim_rejects_missing_worker_token(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            seeded = _seed_every_code_claim_request(store)
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            response = await _post_every_code_work_request_claim(
                app,
                {"request_id": seeded.request_id, "host": "Chris-Studio"},
                authorization="",
            )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "authentication_required")

    async def test_every_code_work_request_claim_rejects_unauthorized_identity(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            seeded = _seed_every_code_claim_request(store)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
            )

            response = await _post_every_code_work_request_claim(
                app,
                {"request_id": seeded.request_id, "host": "Chris-Studio"},
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_every_code_work_request_claim_rejects_invalid_payload(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            response = await _post_every_code_work_request_claim(
                app,
                {"request_id": "", "host": "Chris-Studio"},
                authorization="Bearer worker-token",
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_payload")

    async def test_every_code_work_request_claim_returns_not_found(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            response = await _post_every_code_work_request_claim(
                app,
                {"request_id": "every-code-cbusillo-code-123-test", "host": "Chris-Studio"},
                authorization="Bearer worker-token",
            )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "not_found")

    async def test_every_code_work_request_claim_rejects_already_claimed(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            seeded = _seed_every_code_claim_request(store)
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            first_response = await _post_every_code_work_request_claim(
                app,
                {"request_id": seeded.request_id, "host": "Chris-Studio"},
                authorization="Bearer worker-token",
            )
            second_response = await _post_every_code_work_request_claim(
                app,
                {"request_id": seeded.request_id, "host": "Other-Host"},
                authorization="Bearer worker-token",
            )

        self.assertEqual(first_response.status_code, 202)
        self.assertEqual(second_response.status_code, 409)
        self.assertEqual(second_response.json()["error"]["code"], "work_request_already_claimed")

    async def test_every_code_work_request_claim_requires_store_capability(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=LaunchplaneAuthzPolicy(),
            record_store_factory=lambda: _MissingProductReadStore(),
            bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
        )

        response = await _post_every_code_work_request_claim(
            app,
            {"request_id": "every-code-cbusillo-code-123-test", "host": "Chris-Studio"},
            authorization="Bearer worker-token",
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "database_storage_required")

    async def test_every_code_work_request_status_accepts_worker_token(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            seeded = _seed_every_code_claim_request(store)
            claimed = store.claim_every_code_work_request_record(
                request_id=seeded.request_id,
                host="Chris-Studio",
                claimed_at="2026-05-05T22:01:00Z",
            )
            if claimed is None:
                raise AssertionError("expected seeded request to be claimable")
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            response = await _post_every_code_work_request_status(
                app,
                {
                    "request_id": seeded.request_id,
                    "host": "Chris-Studio",
                    "state": "running",
                    "updated_at": "2026-05-05T22:02:00Z",
                },
                authorization="Bearer worker-token",
            )
            stored_request = store.read_every_code_work_request_record(seeded.request_id)

        payload = response.json()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(payload["records"]["request_id"], seeded.request_id)
        self.assertEqual(payload["records"]["state"], "running")
        self.assertEqual(payload["result"]["request"]["state"], "running")
        self.assertEqual(payload["result"]["request"]["started_at"], "2026-05-05T22:02:00Z")
        self.assertEqual(stored_request.state, "running")

    async def test_every_code_work_request_status_accepts_authorized_identity(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            seeded = _seed_every_code_claim_request(store)
            claimed = store.claim_every_code_work_request_record(
                request_id=seeded.request_id,
                host="Runner-Host",
                claimed_at="2026-05-05T22:01:00Z",
            )
            if claimed is None:
                raise AssertionError("expected seeded request to be claimable")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_every_code_work_request_status_policy(),
                record_store_factory=lambda: store,
            )

            response = await _post_every_code_work_request_status(
                app,
                {
                    "request_id": seeded.request_id,
                    "host": "Runner-Host",
                    "state": "done",
                    "result_pr_url": "https://github.com/cbusillo/code/pull/26",
                    "result_summary": "Opened a PR with the requested fix.",
                },
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["records"]["state"], "done")
        self.assertEqual(
            response.json()["result"]["request"]["result_pr_url"],
            "https://github.com/cbusillo/code/pull/26",
        )

    async def test_every_code_work_request_status_replays_authorized_idempotency(self) -> None:
        payload: dict[str, object] = {
            "request_id": "every-code-cbusillo-code-123-test",
            "host": "Runner-Host",
            "state": "done",
            "result_pr_url": "https://github.com/cbusillo/code/pull/26",
        }
        store = _EveryCodeStatusReplayOnlyStore(
            payload=payload,
            idempotency_key="every-code-status-replay",
        )
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_every_code_work_request_status_policy(),
            record_store_factory=lambda: store,
        )

        response = await _post_every_code_work_request_status(
            app,
            payload,
            idempotency_key="every-code-status-replay",
        )

        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.json()["replayed"])
        self.assertEqual(response.json()["original_trace_id"], "launchplane_req_original")
        self.assertEqual(store.read_idempotency_calls, 1)
        self.assertEqual(store.read_calls, 0)
        self.assertEqual(store.write_calls, 0)

    async def test_every_code_work_request_status_rejects_missing_worker_token(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            seeded = _seed_every_code_claim_request(store)
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            response = await _post_every_code_work_request_status(
                app,
                {"request_id": seeded.request_id, "host": "Chris-Studio", "state": "running"},
                authorization="",
            )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "authentication_required")

    async def test_every_code_work_request_status_rejects_unauthorized_identity(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            seeded = _seed_every_code_claim_request(store)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
            )

            response = await _post_every_code_work_request_status(
                app,
                {"request_id": seeded.request_id, "host": "Chris-Studio", "state": "running"},
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")

    async def test_every_code_work_request_status_rejects_invalid_payload(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            seeded = _seed_every_code_claim_request(store)
            claimed = store.claim_every_code_work_request_record(
                request_id=seeded.request_id,
                host="Chris-Studio",
                claimed_at="2026-05-05T22:01:00Z",
            )
            if claimed is None:
                raise AssertionError("expected seeded request to be claimable")
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            response = await _post_every_code_work_request_status(
                app,
                {"request_id": seeded.request_id, "host": "Other-Host", "state": "running"},
                authorization="Bearer worker-token",
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_payload")

    async def test_every_code_work_request_status_returns_not_found(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            response = await _post_every_code_work_request_status(
                app,
                {
                    "request_id": "every-code-cbusillo-code-123-test",
                    "host": "Chris-Studio",
                    "state": "running",
                },
                authorization="Bearer worker-token",
            )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "not_found")

    async def test_every_code_work_request_status_requires_store_capability(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=LaunchplaneAuthzPolicy(),
            record_store_factory=lambda: _MissingProductReadStore(),
            bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
        )

        response = await _post_every_code_work_request_status(
            app,
            {
                "request_id": "every-code-cbusillo-code-123-test",
                "host": "Chris-Studio",
                "state": "running",
            },
            authorization="Bearer worker-token",
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "database_storage_required")

    async def test_every_code_work_request_status_sends_blocked_notifications(self) -> None:
        sent_payloads: list[tuple[str, dict[str, object]]] = []

        def send_discord(webhook_url: str, payload: dict[str, object]) -> None:
            sent_payloads.append((webhook_url, payload))

        with (
            TemporaryDirectory() as temporary_directory_name,
            patch.dict(
                "os.environ",
                {
                    control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: (
                        "test-master-key"
                    ),
                },
                clear=True,
            ),
        ):
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(
                    Path(temporary_directory_name) / "launchplane.sqlite3"
                )
            )
            try:
                store.ensure_schema()
                seeded = _seed_every_code_claim_request(store)
                claimed = store.claim_every_code_work_request_record(
                    request_id=seeded.request_id,
                    host="Chris-Studio",
                    claimed_at="2026-05-05T22:01:00Z",
                )
                if claimed is None:
                    raise AssertionError("expected seeded request to be claimable")
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
                app = create_launchplane_fastapi_app(
                    verifier=_RejectingVerifier(),
                    authz_policy=LaunchplaneAuthzPolicy(),
                    record_store_factory=lambda: store,
                    bearer_identity_config=BearerIdentityConfig(
                        every_code_worker_token="worker-token"
                    ),
                    every_code_discord_sender=send_discord,
                )

                response = await _post_every_code_work_request_status(
                    app,
                    {
                        "request_id": seeded.request_id,
                        "host": "Chris-Studio",
                        "state": "blocked",
                        "error_message": "Every Code bot auth actor mismatch.",
                    },
                    authorization="Bearer worker-token",
                )
                attempts = store.list_every_code_notification_attempt_records(
                    request_id=seeded.request_id,
                    event="work_request_blocked",
                )
            finally:
                store.close()

        payload = response.json()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(payload["records"]["state"], "blocked")
        self.assertEqual(len(sent_payloads), 1)
        webhook_url, discord_payload = sent_payloads[0]
        self.assertEqual(webhook_url, "https://discord.com/api/webhooks/test/webhook")
        self.assertIn("embeds", discord_payload)
        self.assertEqual(payload["result"]["notifications"][0]["delivery_status"], "delivered")
        self.assertEqual(attempts[0].delivery_status, "delivered")

    async def test_every_code_work_request_rerun_accepts_worker_token(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            seeded = _seed_every_code_claim_request(store)
            claimed = store.claim_every_code_work_request_record(
                request_id=seeded.request_id,
                host="Chris-Studio",
                claimed_at="2026-05-05T22:01:00Z",
            )
            if claimed is None:
                raise AssertionError("expected seeded request to be claimable")
            blocked = apply_every_code_work_request_status(
                claimed,
                EveryCodeWorkRequestStatusUpdate(
                    state="blocked",
                    host="Chris-Studio",
                    updated_at="2026-05-05T22:05:00Z",
                    result_pr_url="https://github.com/cbusillo/code/pull/26",
                    result_summary="Detached session went stale.",
                    error_message="Detached session went stale.",
                ),
            )
            store.write_every_code_work_request_record(blocked)
            intent = _seed_every_code_rerun_intent(
                store,
                idempotency_key="every-code-rerun-intent:123",
            )
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            response = await _post_every_code_work_request_rerun(
                app,
                {
                    "request_id": seeded.request_id,
                    "trigger_actor": "cbusillo",
                    "source_url": "https://github.com/cbusillo/code/issues/123",
                    "agent_write_intent_record_id": intent.record_id,
                },
                authorization="Bearer worker-token",
                idempotency_key="every-code-rerun-intent:123",
            )
            stored_request = store.read_every_code_work_request_record(seeded.request_id)

        payload = response.json()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(payload["records"]["request_id"], seeded.request_id)
        self.assertEqual(payload["records"]["state"], "queued")
        self.assertEqual(payload["records"]["agent_write_intent_record_id"], intent.record_id)
        self.assertEqual(payload["result"]["request"]["trigger_actor"], "cbusillo")
        self.assertEqual(stored_request.state, "queued")
        self.assertEqual(stored_request.claimed_by_host, "")
        self.assertEqual(stored_request.result_pr_url, "")
        self.assertEqual(stored_request.error_message, "")

    async def test_every_code_work_request_rerun_accepts_authorized_identity(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            seeded = _seed_every_code_claim_request(store)
            claimed = store.claim_every_code_work_request_record(
                request_id=seeded.request_id,
                host="Runner-Host",
                claimed_at="2026-05-05T22:01:00Z",
            )
            if claimed is None:
                raise AssertionError("expected seeded request to be claimable")
            done = apply_every_code_work_request_status(
                claimed,
                EveryCodeWorkRequestStatusUpdate(
                    state="done",
                    host="Runner-Host",
                    updated_at="2026-05-05T22:05:00Z",
                    result_pr_url="https://github.com/cbusillo/code/pull/26",
                ),
            )
            store.write_every_code_work_request_record(done)
            intent = _seed_every_code_rerun_intent(store)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_every_code_work_request_rerun_policy(),
                record_store_factory=lambda: store,
            )

            response = await _post_every_code_work_request_rerun(
                app,
                {
                    "request_id": seeded.request_id,
                    "trigger_actor": "ops",
                    "agent_write_intent_record_id": intent.record_id,
                },
                idempotency_key="every-code-rerun-code-123",
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["records"]["state"], "queued")
        self.assertEqual(response.json()["result"]["request"]["trigger_actor"], "ops")

    async def test_every_code_work_request_rerun_uses_matching_intent_evidence(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            seeded = _seed_every_code_claim_request(store)
            claimed = store.claim_every_code_work_request_record(
                request_id=seeded.request_id,
                host="Chris-Studio",
                claimed_at="2026-05-05T22:01:00Z",
            )
            if claimed is None:
                raise AssertionError("expected seeded request to be claimable")
            blocked = apply_every_code_work_request_status(
                claimed,
                EveryCodeWorkRequestStatusUpdate(
                    state="blocked",
                    host="Chris-Studio",
                    updated_at="2026-05-05T22:05:00Z",
                    error_message="Needs another pass.",
                ),
            )
            store.write_every_code_work_request_record(blocked)
            intent = _seed_every_code_rerun_intent(store)
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            response = await _post_every_code_work_request_rerun(
                app,
                {"request_id": seeded.request_id, "trigger_actor": "ops"},
                authorization="Bearer worker-token",
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(
            response.json()["records"]["agent_write_intent_record_id"],
            intent.record_id,
        )
        self.assertEqual(response.json()["records"]["state"], "queued")

    async def test_every_code_work_request_rerun_replays_authorized_idempotency(
        self,
    ) -> None:
        payload: dict[str, object] = {
            "request_id": "every-code-cbusillo-code-123-test",
            "agent_write_intent_record_id": "agent-write-intent-test",
        }
        store = _EveryCodeRerunReplayOnlyStore(
            payload=payload,
            idempotency_key="every-code-rerun-replay",
        )
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_every_code_work_request_rerun_policy(),
            record_store_factory=lambda: store,
        )

        response = await _post_every_code_work_request_rerun(
            app,
            payload,
            idempotency_key="every-code-rerun-replay",
        )

        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.json()["replayed"])
        self.assertEqual(response.json()["original_trace_id"], "launchplane_req_original")
        self.assertEqual(store.read_idempotency_calls, 1)
        self.assertEqual(store.read_calls, 0)
        self.assertEqual(store.write_calls, 0)

    async def test_every_code_work_request_rerun_rejects_invalid_inputs(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            seeded = _seed_every_code_claim_request(store)
            claimed = store.claim_every_code_work_request_record(
                request_id=seeded.request_id,
                host="Chris-Studio",
                claimed_at="2026-05-05T22:01:00Z",
            )
            if claimed is None:
                raise AssertionError("expected seeded request to be claimable")
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            active_response = await _post_every_code_work_request_rerun(
                app,
                {"request_id": seeded.request_id},
                authorization="Bearer worker-token",
            )
            invalid_response = await _post_every_code_work_request_rerun(
                app,
                {"request_id": ""},
                authorization="Bearer worker-token",
            )

        self.assertEqual(active_response.status_code, 409)
        self.assertEqual(active_response.json()["error"]["code"], "agent_write_intent_required")
        self.assertEqual(invalid_response.status_code, 400)
        self.assertEqual(invalid_response.json()["error"]["code"], "invalid_payload")

    async def test_every_code_work_request_rerun_validation_error_omits_records(
        self,
    ) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=LaunchplaneAuthzPolicy(),
            record_store_factory=lambda: _MissingProductReadStore(),
            bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
        )

        response = await _asgi_request(
            app,
            "POST",
            "/v1/every-code/work-requests/rerun",
            headers={
                "Authorization": "Bearer worker-token",
                "Content-Type": "application/json",
            },
            raw_body=b'{"request_id":',
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_request")
        self.assertNotIn("records", response.json())

    async def test_every_code_work_request_rerun_rejects_non_terminal_request(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            seeded = _seed_every_code_claim_request(store)
            intent = _seed_every_code_rerun_intent(store)
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            response = await _post_every_code_work_request_rerun(
                app,
                {
                    "request_id": seeded.request_id,
                    "agent_write_intent_record_id": intent.record_id,
                },
                authorization="Bearer worker-token",
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_payload")

    async def test_every_code_work_request_rerun_requires_write_intent_evidence(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            seeded = _seed_every_code_claim_request(store)
            claimed = store.claim_every_code_work_request_record(
                request_id=seeded.request_id,
                host="Chris-Studio",
                claimed_at="2026-05-05T22:01:00Z",
            )
            if claimed is None:
                raise AssertionError("expected seeded request to be claimable")
            blocked = apply_every_code_work_request_status(
                claimed,
                EveryCodeWorkRequestStatusUpdate(
                    state="blocked",
                    host="Chris-Studio",
                    updated_at="2026-05-05T22:05:00Z",
                    error_message="Needs another pass.",
                ),
            )
            store.write_every_code_work_request_record(blocked)
            wrong_source_intent = _seed_every_code_rerun_intent(
                store,
                source_url="https://github.com/cbusillo/code/issues/999",
            )
            wrong_context_intent = _seed_every_code_rerun_intent(
                store,
                context="other-context",
            )
            stale_intent = _seed_every_code_rerun_intent(
                store,
                recorded_at="2026-01-01T00:00:00Z",
            )
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            missing_response = await _post_every_code_work_request_rerun(
                app,
                {"request_id": seeded.request_id},
                authorization="Bearer worker-token",
            )
            mismatch_response = await _post_every_code_work_request_rerun(
                app,
                {
                    "request_id": seeded.request_id,
                    "source_url": "https://github.com/cbusillo/code/issues/123",
                    "agent_write_intent_record_id": wrong_source_intent.record_id,
                },
                authorization="Bearer worker-token",
            )
            mismatch_without_source_response = await _post_every_code_work_request_rerun(
                app,
                {
                    "request_id": seeded.request_id,
                    "agent_write_intent_record_id": wrong_source_intent.record_id,
                },
                authorization="Bearer worker-token",
            )
            wrong_context_response = await _post_every_code_work_request_rerun(
                app,
                {
                    "request_id": seeded.request_id,
                    "agent_write_intent_record_id": wrong_context_intent.record_id,
                },
                authorization="Bearer worker-token",
            )
            stale_response = await _post_every_code_work_request_rerun(
                app,
                {
                    "request_id": seeded.request_id,
                    "agent_write_intent_record_id": stale_intent.record_id,
                },
                authorization="Bearer worker-token",
            )
            not_found_response = await _post_every_code_work_request_rerun(
                app,
                {
                    "request_id": seeded.request_id,
                    "agent_write_intent_record_id": "agent-write-intent-missing",
                },
                authorization="Bearer worker-token",
            )

        self.assertEqual(missing_response.status_code, 409)
        self.assertEqual(missing_response.json()["error"]["code"], "agent_write_intent_required")
        self.assertEqual(mismatch_response.status_code, 409)
        self.assertEqual(
            mismatch_response.json()["error"]["code"],
            "agent_write_intent_source_mismatch",
        )
        self.assertEqual(
            mismatch_response.json()["records"]["agent_write_intent_record_id"],
            wrong_source_intent.record_id,
        )
        self.assertEqual(mismatch_without_source_response.status_code, 409)
        self.assertEqual(
            mismatch_without_source_response.json()["error"]["code"],
            "agent_write_intent_source_mismatch",
        )
        self.assertEqual(wrong_context_response.status_code, 409)
        self.assertEqual(
            wrong_context_response.json()["error"]["code"],
            "agent_write_intent_scope_mismatch",
        )
        self.assertEqual(stale_response.status_code, 409)
        self.assertEqual(stale_response.json()["error"]["code"], "agent_write_intent_stale")
        self.assertEqual(not_found_response.status_code, 404)
        self.assertEqual(
            not_found_response.json()["error"]["code"],
            "agent_write_intent_not_found",
        )

    async def test_every_code_work_request_rerun_returns_not_found_for_missing_request(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            intent = _seed_every_code_rerun_intent(store)
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            response = await _post_every_code_work_request_rerun(
                app,
                {
                    "request_id": "every-code-cbusillo-code-123-missing",
                    "agent_write_intent_record_id": intent.record_id,
                },
                authorization="Bearer worker-token",
            )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "not_found")

    async def test_every_code_work_request_rerun_requires_store_capability(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=LaunchplaneAuthzPolicy(),
            record_store_factory=lambda: _MissingProductReadStore(),
            bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
        )

        response = await _post_every_code_work_request_rerun(
            app,
            {"request_id": "every-code-cbusillo-code-123-test"},
            authorization="Bearer worker-token",
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "database_storage_required")
        self.assertNotIn("records", response.json())

    async def test_every_code_pr_feedback_write_stores_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            response = await _post_every_code_pr_feedback(
                app,
                _every_code_pr_feedback_payload(),
                authorization="Bearer worker-token",
            )
            records = store.list_every_code_pr_feedback_records(
                request_id="every-code-cbusillo-code-123-test",
                status="pending",
            )

        payload = response.json()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(payload["records"]["feedback_id"], records[0].feedback_id)
        self.assertEqual(payload["result"]["feedback"]["feedback_id"], records[0].feedback_id)
        self.assertEqual(len(records), 1)

    async def test_every_code_pr_feedback_write_rejects_missing_worker_token(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_every_code_read_policy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            response = await _post_every_code_pr_feedback(
                app,
                _every_code_pr_feedback_payload(),
            )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "authentication_required")

    async def test_every_code_pr_feedback_write_rejects_invalid_payload(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            response = await _post_every_code_pr_feedback(
                app,
                {**_every_code_pr_feedback_payload(), "repository": "cbusillo-code"},
                authorization="Bearer worker-token",
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_payload")

    async def test_every_code_pr_feedback_write_requires_store_capability(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=LaunchplaneAuthzPolicy(),
            record_store_factory=lambda: _MissingProductReadStore(),
            bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
        )

        response = await _post_every_code_pr_feedback(
            app,
            _every_code_pr_feedback_payload(),
            authorization="Bearer worker-token",
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "database_storage_required")

    async def test_every_code_pr_feedback_status_write_updates_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            feedback_record = EveryCodePrFeedbackRecord.model_validate(
                _every_code_pr_feedback_payload()
            )
            store.write_every_code_pr_feedback_record(feedback_record)
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            response = await _post_every_code_pr_feedback_status(
                app,
                {
                    "feedback_id": feedback_record.feedback_id,
                    "request_id": feedback_record.request_id,
                    "status": "applied",
                },
                authorization="Bearer worker-token",
            )
            applied_records = store.list_every_code_pr_feedback_records(
                request_id=feedback_record.request_id,
                status="applied",
            )

        payload = response.json()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(payload["records"]["feedback_id"], feedback_record.feedback_id)
        self.assertEqual(payload["records"]["status"], "applied")
        self.assertEqual(payload["result"]["feedback"]["status"], "applied")
        self.assertEqual(len(applied_records), 1)
        self.assertEqual(applied_records[0].feedback_id, feedback_record.feedback_id)

    async def test_every_code_pr_feedback_status_write_rejects_missing_worker_token(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            feedback_record = EveryCodePrFeedbackRecord.model_validate(
                _every_code_pr_feedback_payload()
            )
            store.write_every_code_pr_feedback_record(feedback_record)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_every_code_read_policy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            response = await _post_every_code_pr_feedback_status(
                app,
                {
                    "feedback_id": feedback_record.feedback_id,
                    "request_id": feedback_record.request_id,
                    "status": "applied",
                },
            )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "authentication_required")

    async def test_every_code_pr_feedback_status_write_rejects_invalid_payload(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            response = await _post_every_code_pr_feedback_status(
                app,
                {
                    "feedback_id": "",
                    "request_id": "every-code-cbusillo-code-123-test",
                    "status": "applied",
                },
                authorization="Bearer worker-token",
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_payload")

    async def test_every_code_pr_feedback_status_write_returns_not_found(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            response = await _post_every_code_pr_feedback_status(
                app,
                {
                    "feedback_id": "missing-feedback",
                    "request_id": "every-code-cbusillo-code-123-test",
                    "status": "applied",
                },
                authorization="Bearer worker-token",
            )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "not_found")

    async def test_every_code_pr_feedback_status_write_rejects_final_feedback(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            feedback_record = EveryCodePrFeedbackRecord.model_validate(
                {**_every_code_pr_feedback_payload(), "status": "applied"}
            )
            store.write_every_code_pr_feedback_record(feedback_record)
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            response = await _post_every_code_pr_feedback_status(
                app,
                {
                    "feedback_id": feedback_record.feedback_id,
                    "request_id": feedback_record.request_id,
                    "status": "ignored",
                },
                authorization="Bearer worker-token",
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "feedback_already_final")

    async def test_every_code_pr_feedback_status_write_requires_store_capability(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=LaunchplaneAuthzPolicy(),
            record_store_factory=lambda: _MissingProductReadStore(),
            bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
        )

        response = await _post_every_code_pr_feedback_status(
            app,
            {
                "feedback_id": "feedback-1",
                "request_id": "every-code-cbusillo-code-123-test",
                "status": "applied",
            },
            authorization="Bearer worker-token",
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "database_storage_required")

    async def test_every_code_preview_gate_write_stores_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            response = await _post_every_code_preview_gate(
                app,
                _every_code_preview_gate_payload(),
                authorization="Bearer worker-token",
            )
            records = store.list_every_code_preview_gate_records(
                request_id="every-code-cbusillo-code-123-test",
                status="ready",
            )

        payload = response.json()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(payload["records"]["gate_id"], records[0].gate_id)
        self.assertEqual(payload["result"]["gate"]["gate_id"], records[0].gate_id)
        self.assertEqual(len(records), 1)

    async def test_every_code_preview_gate_write_rejects_missing_worker_token(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_every_code_read_policy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            response = await _post_every_code_preview_gate(
                app,
                _every_code_preview_gate_payload(),
            )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "authentication_required")

    async def test_every_code_preview_gate_write_rejects_invalid_payload(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            app = create_launchplane_fastapi_app(
                verifier=_RejectingVerifier(),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: store,
                bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
            )

            response = await _post_every_code_preview_gate(
                app,
                {**_every_code_preview_gate_payload(), "repository": "cbusillo-code"},
                authorization="Bearer worker-token",
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_payload")

    async def test_every_code_preview_gate_write_requires_store_capability(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=LaunchplaneAuthzPolicy(),
            record_store_factory=lambda: _MissingProductReadStore(),
            bearer_identity_config=BearerIdentityConfig(every_code_worker_token="worker-token"),
        )

        response = await _post_every_code_preview_gate(
            app,
            _every_code_preview_gate_payload(),
            authorization="Bearer worker-token",
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "database_storage_required")

    async def test_every_code_read_routes_return_native_payloads(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            seeded = _seed_every_code_read_records(store)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_every_code_read_policy(),
                record_store_factory=lambda: store,
            )

            summary_response = await _get_every_code_summary(
                app,
                repository="cbusillo/code",
                issue_number="123",
                state="queued",
                limit="1",
                offset="0",
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
            summary_response,
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

        summary = summary_response.json()["summary"]
        self.assertEqual(summary["repository"], "cbusillo/code")
        self.assertEqual(summary["issue_number"], 123)
        self.assertEqual(summary["state_filter"], "queued")
        self.assertEqual(summary["summaries"][0]["request_id"], seeded["request_id"])
        self.assertEqual(summary["summaries"][0]["summary_status"], "active")

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
                await _get_every_code_summary(
                    app,
                    repository="cbusillo/code",
                    authorization="Bearer worker-token",
                ),
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

        for response in responses:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["status"], "ok")

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
            "/v1/every-code/summary": (
                "read_every_code_summary",
                "EveryCodeSummaryResponse",
            ),
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
        create_route = openapi["paths"]["/v1/every-code/work-requests/create"]["post"]
        self.assertEqual(create_route["operationId"], "create_every_code_work_request")
        self.assertEqual(
            create_route["responses"]["202"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/AcceptedEvidenceResponse",
        )
        for status_code in ("400", "401", "403", "409", "503"):
            self.assertIn(
                "LaunchplaneErrorResponse", json.dumps(create_route["responses"][status_code])
            )
        work_request_status_route = openapi["paths"]["/v1/every-code/work-requests/status"]["post"]
        self.assertEqual(
            work_request_status_route["operationId"],
            "write_every_code_work_request_status",
        )
        self.assertEqual(
            work_request_status_route["requestBody"]["content"]["application/json"]["schema"][
                "title"
            ],
            "EveryCodeWorkRequestStatusEnvelope",
        )
        self.assertFalse(
            work_request_status_route["requestBody"]["content"]["application/json"]["schema"][
                "additionalProperties"
            ]
        )
        self.assertEqual(
            set(
                work_request_status_route["requestBody"]["content"]["application/json"]["schema"][
                    "required"
                ]
            ),
            {"request_id", "host", "state"},
        )
        self.assertEqual(
            work_request_status_route["responses"]["202"]["content"]["application/json"]["schema"][
                "$ref"
            ],
            "#/components/schemas/AcceptedEvidenceResponse",
        )
        for status_code in ("400", "401", "403", "404", "409", "503"):
            self.assertIn(
                "LaunchplaneErrorResponse",
                json.dumps(work_request_status_route["responses"][status_code]),
            )
        worker_write_routes = {
            "/v1/every-code/pr-feedback": "write_every_code_pr_feedback",
            "/v1/every-code/preview-gates": "write_every_code_preview_gate",
        }
        for path, operation_id in worker_write_routes.items():
            route = openapi["paths"][path]["post"]
            self.assertEqual(route["operationId"], operation_id)
            self.assertEqual(
                route["responses"]["202"]["content"]["application/json"]["schema"]["$ref"],
                "#/components/schemas/AcceptedEvidenceResponse",
            )
            for status_code in ("400", "401", "403", "503"):
                self.assertIn(
                    "LaunchplaneErrorResponse", json.dumps(route["responses"][status_code])
                )
            self.assertNotIn("409", route["responses"])
        status_route = openapi["paths"]["/v1/every-code/pr-feedback/status"]["post"]
        self.assertEqual(status_route["operationId"], "write_every_code_pr_feedback_status")
        self.assertEqual(
            status_route["responses"]["202"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/AcceptedEvidenceResponse",
        )
        for status_code in ("400", "401", "403", "404", "409", "503"):
            self.assertIn(
                "LaunchplaneErrorResponse", json.dumps(status_route["responses"][status_code])
            )

    async def test_fastapi_every_code_reads_precede_legacy_wsgi_fallback(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            seeded = _seed_every_code_read_records(store)
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_every_code_read_policy(),
                record_store_factory=lambda: store,
            )
            legacy_app = create_launchplane_service_app(
                state_dir=root / "legacy-state",
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                control_plane_root_path=root,
            )
            app.mount("/", cast(ASGIApp, WSGIMiddleware(cast(Any, legacy_app))))

            work_request_response = await _get_every_code_work_request(
                app,
                seeded["request_id"],
            )
            notification_response = await _get_preview_pr_feedback_notification_attempts(
                app,
                feedback_id=seeded["preview_feedback_id"],
            )

        self.assertEqual(work_request_response.status_code, 200)
        self.assertEqual(notification_response.status_code, 200)
